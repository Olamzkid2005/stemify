"""Demucs adapter (plan Task 9 / Sections 12.2-12.3, 12.5).

The rest of the worker only sees numpy waveforms: torch and demucs imports are
lazy, so this module imports cleanly without the model stack.

Inference policy (fixed for this adapter):
- Uses demucs' chunked inference math (segment/overlap triangle-weight blending),
  profile-tuned overlap and shifts, float32. Both of apply_model's outer paths —
  the shift trick and the chunked split pass — are replicated here rather than
  delegated, so every finished chunk can fire the progress callback: apply_model
  4.0.1 exposes no per-chunk hook and only reports at its start and end, which
  left the UI parked at 30% for the whole separation. The replication is guarded:
  if demucs' internals drift, the drift check falls back to plain apply_model
  (coarse progress, identical audio) instead of producing wrong output. See
  _apply_model_with_progress and _apply_path_matches_installed.
- Instrumental policy per profile (demucs_default): mixture minus vocals for
  `vocals_instrumental`, and the residual (mixture minus vocals, drums, bass)
  for `full_stems`; both are mixture-consistent by construction.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

from worker.errors import ErrorCode
from worker.models.base import ModelProfile, SeparationError
from worker.models.profiles import (
    get_profile,
    get_profile_for_mode,
    resolve_quality,
    validate_profile,
)

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np

ProgressCallback = Callable[[float], None]
CancellationChecker = Callable[[], bool]

# Warm model reuse within the worker process (plan Task 9 acceptance).
_LOADED_MODELS: dict[tuple[str, str], tuple[Any, str]] = {}

_INSTALL_HINT = (
    "The separation engine is not installed. "
    "Run: pip install -r worker/requirements.txt"
)

# Gross clipping/overflow bound for sanity checks; normalised audio stays well
# below this even for mixture-minus-vocals stems.
MAX_ABSOLUTE_AMPLITUDE = 8.0


# ---------------------------------------------------------------------------
# Chunked inference with per-chunk progress
#
# demucs 4.0.1's apply_model(shifts, split=True) is one blocking call that
# reports only its start and end (no per-chunk callback), which left the job
# progress parked for the entire separation. Both branches of its outer loop —
# the shift trick (random time-shifts, averaged) and the split pass (stride/
# offset chunking, triangle window, weighted accumulation) — are replicated
# below so a callback can fire per finished chunk. _apply_path_matches_installed
# verifies the replication against the live demucs source before trusting it,
# and any mismatch or introspection failure falls back to plain apply_model:
# a coarse bar is recoverable, wrong audio is not.
# ---------------------------------------------------------------------------


def _resolve_segment_seconds(model: Any, segment: float | None) -> float:
    """The effective chunk length in seconds for one split pass.

    Precedence mirrors apply_model: an explicit value (the profile's
    chunk_length_seconds) wins; otherwise the model's own segment. A
    BagOfModels carries no segment of its own — demucs dispatches to each
    sub-model, which uses its own .segment — so the effective length is the min
    across the bag, mirroring BagOfModels.max_allowed_segment. Passing a bag
    straight to `model.segment` raises AttributeError, so this must not assume
    the attribute exists.
    """
    if segment is not None:
        return float(segment)
    own_segment = getattr(model, "segment", None)
    if own_segment is not None:
        return float(own_segment)
    sub_segments = [
        float(sub.segment)
        for sub in getattr(model, "models", [])
        if getattr(sub, "segment", None) is not None
    ]
    if not sub_segments:
        raise SeparationError(
            ErrorCode.INFERENCE_FAILED,
            "cannot determine the model's chunk length for chunked inference",
        )
    return min(sub_segments)


def _chunk_offsets(length: int, segment_length: int, overlap: float) -> list[int]:
    """Chunk start offsets for one full split pass over the waveform.

    `range(0, length, stride)` in demucs' apply_model. The stride quantizes to
    whole samples exactly as demucs does, so the last offset can start less than
    a full segment from the end; that tail chunk is the padded one demucs' own
    path also produces.
    """
    stride = int((1 - overlap) * segment_length)
    return list(range(0, length, stride))


def _run_split_pass(
    demucs_apply: Any,
    model: Any,
    th: Any,
    mix: Any,
    *,
    segment: float,
    segment_length: int,
    overlap: float,
    device: str,
    report: Callable[[int, int], None],
) -> Any:
    """One triangle-weighted split pass over `mix` (demucs' split branch).

    Mirrors apply_model(shifts=0, split=True) line for line: chunk the input by
    stride, run each chunk through demucs' own non-split apply_model (segment
    padding and center-trim included, with the original `segment` in seconds —
    demucs passes its seconds value straight through), accumulate with the
    triangle window, and normalize per sample. `report(done, total)` fires
    after each finished chunk.
    """
    from demucs.apply import TensorChunk

    length = mix.shape[-1]
    offsets = _chunk_offsets(length, segment_length, overlap)
    total = max(1, len(offsets))

    out = th.zeros(
        mix.shape[0],
        len(model.sources),
        mix.shape[1],
        length,
        device=mix.device,
    )
    sum_weight = th.zeros(length, device=mix.device)

    # The triangle window, copied from demucs' split branch;
    # transition_power=1 is apply_model's default and what our calls pin.
    weight = th.cat(
        [
            th.arange(1, segment_length // 2 + 1, device=device),
            th.arange(segment_length - segment_length // 2, 0, -1, device=device),
        ]
    )
    weight = (weight / weight.max()) ** 1.0

    for done, offset in enumerate(offsets, start=1):
        chunk = TensorChunk(mix, offset, segment_length)
        chunk_out = demucs_apply.apply_model(
            model,
            chunk,
            shifts=0,
            split=False,
            overlap=overlap,
            segment=segment,
            device=device,
            progress=False,
        )
        chunk_length = chunk_out.shape[-1]
        out[..., offset : offset + segment_length] += (weight[:chunk_length] * chunk_out).to(mix.device)
        sum_weight[offset : offset + segment_length] += weight[:chunk_length].to(mix.device)
        report(done, total)

    assert sum_weight.min() > 0
    out /= sum_weight
    return out


def _apply_model_with_progress(
    model: Any,
    th: Any,
    tensor: Any,
    *,
    device: str,
    shifts: int,
    overlap: float,
    segment: float | None,
    progress_callback: ProgressCallback | None,
    on_chunk_done: Callable[[], None] | None = None,
) -> Any:
    """apply_model(shifts, split=True) with a callback after each finished chunk.

    Replicates demucs' shifts branch (random time-shift trick averaged over
    `shifts` passes — each pass runs the replicated split path, so the callback
    scales across all passes) and its split branch. demucs 4.0.1 exposes no
    per-chunk hook, so delegation would report only start and end. Before the
    first chunk, _apply_path_matches_installed compares the installed demucs
    source against this replication; on any mismatch it falls back to plain
    apply_model (progress in two steps, audio unchanged).
    """
    import random

    import demucs.apply as demucs_apply

    if not _apply_path_matches_installed():
        if progress_callback:
            progress_callback(0.0)
        result = demucs_apply.apply_model(
            model,
            tensor,
            shifts=shifts,
            split=True,
            overlap=overlap,
            segment=segment,
            device=device,
            progress=False,
        )
        if progress_callback:
            progress_callback(1.0)
        return result

    def _report(done: int, total: int, pass_index: int = 0, pass_count: int = 1) -> None:
        if progress_callback:
            fraction = (pass_index + done / total) / pass_count
            progress_callback(min(1.0, fraction))
        if on_chunk_done is not None:
            on_chunk_done()

    segment_length = int(model.samplerate * _resolve_segment_seconds(model, segment))

    shift_count = int(shifts)
    if shift_count > 0:
        # demucs' shifts branch: pad by 0.5s on both sides, then average
        # `shifts` randomly-shifted full passes. Each pass covers every chunk
        # position, so per-chunk progress is scaled across all passes.
        length = tensor.shape[-1]
        max_shift = int(0.5 * model.samplerate)
        from demucs.apply import TensorChunk

        mix_chunk = TensorChunk(tensor)
        padded_mix = mix_chunk.padded(length + 2 * max_shift)
        out: Any = None
        for pass_index in range(shift_count):
            offset = random.randint(0, max_shift)
            shifted = TensorChunk(padded_mix, offset, length + max_shift - offset)
            pass_out = _run_split_pass(
                demucs_apply,
                model,
                th,
                shifted,
                segment=segment,
                segment_length=segment_length,
                overlap=overlap,
                device=device,
                report=lambda done, total, p=pass_index: _report(done, total, p, shift_count),
            )
            shifted_out = pass_out[..., max_shift - offset :]
            # Accumulate like demucs (`out += shifted_out`): the first pass
            # materialises the buffer, later passes add in place. A functional
            # add would hold a whole extra copy of the track per pass (hundreds
            # of MB for a full song) on top of the output being built.
            if out is None:
                out = shifted_out.clone()
            else:
                out.add_(shifted_out)
            # Release both references: the slice is a view, so dropping only
            # pass_out would keep the previous pass's buffer alive through the
            # next pass's computation (a full track copy held for nothing).
            del shifted_out, pass_out
        out.div_(shift_count)
        return out

    return _run_split_pass(
        demucs_apply,
        model,
        th,
        tensor,
        segment=segment,
        segment_length=segment_length,
        overlap=overlap,
        device=device,
        report=_report,
    )


def _apply_path_matches_installed() -> bool:
    """True when the installed demucs still matches what we replicate.

    Every marker below changes the *output math* of the replicated branch; if
    any is missing the replication would silently produce wrong audio, so the
    caller falls back to apply_model itself.
    """
    try:
        import inspect

        import demucs.apply as demucs_apply

        source = inspect.getsource(demucs_apply.apply_model)
    except Exception:  # noqa: BLE001 - any introspection failure means: fall back
        return False
    split_markers = (
        "sum_weight = th.zeros(length, device=mix.device)",
        "out = th.zeros(batch, len(model.sources), channels, length, device=mix.device)",
        "segment_length: int = int(model.samplerate * segment)",
        "stride = int((1 - overlap) * segment_length)",
        "offsets = range(0, length, stride)",
        "weight = th.cat([th.arange(1, segment_length // 2 + 1, device=device),",
        "th.arange(segment_length - segment_length // 2, 0, -1, device=device)])",
        "weight = (weight / weight.max())**transition_power",
        "chunk = TensorChunk(mix, offset, segment_length)",
        "weight[:chunk_length] * chunk_out).to(mix.device)",
        "sum_weight[offset:offset + segment_length] += weight[:chunk_length].to(mix.device)",
        "out /= sum_weight",
    )
    shifts_markers = (
        "max_shift = int(0.5 * model.samplerate)",
        "padded_mix = mix.padded(length + 2 * max_shift)",
        "offset = random.randint(0, max_shift)",
        "shifted = TensorChunk(padded_mix, offset, length + max_shift - offset)",
        "out += shifted_out[..., max_shift - offset:]",
        "out /= shifts",
    )
    return all(marker in source for marker in split_markers + shifts_markers)


@contextlib.contextmanager
def _weights_only_compat() -> Iterator[None]:
    """Force weights_only=False on torch.load for demucs checkpoint loads.

    torch >= 2.6 resolves an unset weights_only to True, which rejects the
    pickled objects inside demucs 4.0.1 checkpoints (they were serialized with
    torch <= 2.5 defaults). demucs 4.0.1 calls torch.load without passing the
    flag, so patch it for the duration of the load call only.

    This is safe here because checkpoint integrity is enforced independently:
    torch.hub downloads with check_hash=True and _verify_checkpoint_checksum
    re-hashes the cached file against the allowlisted profile checksum.
    """
    try:
        import torch
    except ImportError:
        # Lazy-import contract: no torch in the environment is handled by the
        # _import_torch path; nothing to patch here.
        yield
        return
    if tuple(int(p) for p in torch.__version__.split("+", 1)[0].split(".")[:2]) < (2, 6):
        yield
        return

    original_load = torch.load

    def _load_without_weights_only(*args: Any, **kwargs: Any) -> Any:
        kwargs["weights_only"] = False
        return original_load(*args, **kwargs)

    torch.load = _load_without_weights_only  # type: ignore[assignment]
    try:
        yield
    finally:
        torch.load = original_load  # type: ignore[assignment]


def resolve_device(requested: str | None = None) -> str:
    """Resolve STEMIFY_DEVICE (auto|cpu|cuda) to a concrete torch device (plan 12.3)."""
    policy = (requested or os.environ.get("STEMIFY_DEVICE") or "auto").strip().lower()
    if policy == "cpu":
        return "cpu"
    if policy in ("auto", "cuda"):
        torch = _import_torch()
        available = torch.cuda.is_available()
        if policy == "cuda" and not available:
            raise SeparationError(
                ErrorCode.MODEL_LOAD_FAILED,
                "STEMIFY_DEVICE=cuda was requested but no CUDA device is available; "
                "install CUDA-enabled torch or set STEMIFY_DEVICE=cpu",
            )
        return "cuda" if available else "cpu"
    raise SeparationError(
        ErrorCode.MODEL_LOAD_FAILED, f"unknown device {policy!r}; expected auto, cpu, or cuda"
    )


def load_model(
    profile: ModelProfile | None = None,
    device: str | None = None,
    model_dir: Path | None = None,
) -> tuple[Any, str]:
    """Load (or reuse) the profile's model on the resolved device.

    Weights cache under STEMIFY_MODEL_DIR (default data/models) via TORCH_HOME,
    and the cached checkpoint checksum is re-verified on every load.
    """
    profile = profile or get_profile(os.environ.get("STEMIFY_MODEL_PROFILE", "demucs_default"))
    validate_profile(profile)
    resolved = resolve_device(device or profile.device_policy)

    cache_key = (profile.profile_id, resolved, str(_checkpoint_path(profile)))
    if cache_key in _LOADED_MODELS:
        return _LOADED_MODELS[cache_key]

    _configure_model_cache(model_dir or _default_model_dir())
    demucs_pretrained = _import_demucs_pretrained()
    try:
        with _weights_only_compat():
            model = demucs_pretrained.get_model(profile.model_id)
        model.to(resolved)
        model.eval()
    except SeparationError:
        raise
    except Exception as error:
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, f"failed to load model: {error}") from error

    _verify_checkpoint_checksum(profile, _checkpoint_path(profile))
    _LOADED_MODELS[cache_key] = (model, resolved)
    return _LOADED_MODELS[cache_key]


def separate(
    waveform: np.ndarray,
    profile: ModelProfile | None = None,
    mode: str = "vocals_instrumental",
    progress_callback: ProgressCallback | None = None,
    cancellation_checker: CancellationChecker | None = None,
    quality: str | None = None,
) -> dict[str, np.ndarray]:
    """Separate a canonical stereo waveform into named numpy stems (plan 12.5).

    `progress_callback` receives a 0.0-1.0 fraction as inference advances —
    per finished chunk on the split path (see _apply_model_with_progress), and
    just the two boundary values when demucs' internals have drifted and the
    fallback path ran. Cancellation is honored before inference and at every
    chunk boundary on the split path.
    """
    profile = profile or get_profile_for_mode(mode)
    validate_profile(profile)
    if mode not in profile.supported_modes:
        raise SeparationError(
            ErrorCode.MODEL_LOAD_FAILED,
            f"mode {mode!r} is not enabled for profile {profile.profile_id!r}",
        )

    np = _import_numpy()

    _validate_waveform(waveform, profile, np)
    if cancellation_checker and cancellation_checker():
        raise SeparationError(ErrorCode.CANCELED, "canceled before separation")

    model, device = load_model(profile)
    th = _import_torch()

    # Per-job quality preset (or STEMIFY_QUALITY env fallback) overrides the
    # profile's pinned inference settings; balanced uses the pinned values.
    _quality_name, quality_overlap, quality_shifts = resolve_quality(quality)

    tensor: Any = None
    try:
        tensor = th.from_numpy(waveform.astype("float32")).to(device)[None]  # (1, channels, samples)

        def _check_canceled() -> None:
            if cancellation_checker and cancellation_checker():
                raise SeparationError(ErrorCode.CANCELED, "canceled during separation")

        with th.no_grad():
            estimates = _apply_model_with_progress(
                model,
                th,
                tensor,
                device=device,
                shifts=quality_shifts,
                overlap=quality_overlap,
                segment=profile.chunk_length_seconds,
                progress_callback=progress_callback,
                on_chunk_done=_check_canceled,
            )[0]  # (sources, channels, samples)
        # Cancellation between chunks is checked by _check_canceled; this
        # covers the fallback path, which runs apply_model as one call.
        if cancellation_checker and cancellation_checker():
            raise SeparationError(ErrorCode.CANCELED, "canceled during separation")
    except SeparationError:
        raise
    except RuntimeError as error:
        if "out of memory" in str(error).lower():
            if device == "cuda":
                th.cuda.empty_cache()
            raise SeparationError(ErrorCode.GPU_OUT_OF_MEMORY, "the device ran out of memory") from error
        raise SeparationError(ErrorCode.INFERENCE_FAILED, "separation failed unexpectedly") from error
    finally:
        del tensor

    stems_np = estimates.cpu().numpy()
    del estimates
    if device == "cuda":
        th.cuda.empty_cache()
    stems: dict[str, np.ndarray] = {}
    for stem_name in profile.model_stems:
        index = model.sources.index(stem_name)
        stem = stems_np[index]
        _validate_stem(stem, waveform, profile, np)
        stems[stem_name] = stem

    result: dict[str, np.ndarray] = {}
    if mode == "vocals_instrumental":
        if profile.instrumental_policy != "mixture_minus_vocals":  # pragma: no cover - allowlisted
            raise SeparationError(ErrorCode.INFERENCE_FAILED, "unknown instrumental policy")
        result["vocals"] = stems["vocals"]
        result["instrumental"] = waveform - stems["vocals"]  # mixture-consistent by construction
        _validate_stem(result["instrumental"], waveform, profile, np)
    else:  # full_stems
        # Non-vocal rhythm-section split: drums and bass are the model's own
        # stems, and the instrumental bed is the residual (mixture minus
        # vocals/drums/bass), so the three outputs never overlap.
        if profile.instrumental_policy != "mixture_minus_vocals":  # pragma: no cover - allowlisted
            raise SeparationError(ErrorCode.INFERENCE_FAILED, "unknown instrumental policy")
        for stem_name in ("drums", "bass"):
            result[stem_name] = stems[stem_name]
        result["instrumental"] = waveform - stems["vocals"] - stems["drums"] - stems["bass"]
        _validate_stem(result["instrumental"], waveform, profile, np)
    return result


def model_metadata(profile: ModelProfile | None = None) -> dict[str, Any]:
    """Safe-to-record metadata for manifests and diagnostics (plan Sections 12.7, 19.1)."""
    profile = profile or get_profile(os.environ.get("STEMIFY_MODEL_PROFILE", "demucs_default"))
    return {
        "profile_id": profile.profile_id,
        "model_id": profile.model_id,
        "revision": profile.revision,
        "checkpoint": profile.checkpoint_identifier,
        "sample_rate": profile.sample_rate,
        "channels": profile.channels,
        "model_stems": list(profile.model_stems),
        "instrumental_policy": profile.instrumental_policy,
        "precision": profile.precision,
        "license_reference": profile.license_reference,
    }


def _import_torch() -> Any:
    try:
        import torch
    except ImportError as error:
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, _INSTALL_HINT) from error
    return torch


def _import_numpy() -> Any:
    try:
        import numpy
    except ImportError as error:
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, _INSTALL_HINT) from error
    return numpy


def _import_demucs_pretrained() -> Any:
    try:
        from demucs import pretrained
    except ImportError as error:
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, _INSTALL_HINT) from error
    return pretrained


def _default_model_dir() -> Path:
    data_dir = Path(os.environ.get("STEMIFY_DATA_DIR") or Path.cwd() / "data")
    return data_dir / "models"


def _configure_model_cache(model_dir: Path) -> None:
    """Point torch.hub at the local model cache before demucs downloads (plan 5.4)."""
    model_dir.mkdir(parents=True, exist_ok=True)
    os.environ["TORCH_HOME"] = str(model_dir)


def _checkpoint_path(profile: ModelProfile) -> Path:
    model_dir = Path(os.environ.get("TORCH_HOME") or _default_model_dir())
    return model_dir / "hub" / "checkpoints" / Path(profile.checkpoint_identifier).name


def _verify_checkpoint_checksum(profile: ModelProfile, checkpoint: Path) -> None:
    """Re-verify the cached checkpoint against the profile checksum (plan 14.3)."""
    if not checkpoint.is_file():
        raise SeparationError(
            ErrorCode.MODEL_LOAD_FAILED,
            f"checkpoint {profile.checkpoint_identifier} is missing after load",
        )
    sha = hashlib.sha256()
    with checkpoint.open("rb") as handle:
        for block in iter(lambda: handle.read(2**20), b""):
            sha.update(block)
    if not sha.hexdigest().startswith(profile.checkpoint_checksum):
        raise SeparationError(
            ErrorCode.MODEL_LOAD_FAILED,
            f"checkpoint {checkpoint.name} failed checksum verification",
        )


def _validate_waveform(waveform: np.ndarray, profile: ModelProfile, np: Any) -> None:
    if waveform.ndim != 2 or waveform.shape[0] != profile.channels:
        raise SeparationError(
            ErrorCode.INVALID_AUDIO,
            f"expected shape ({profile.channels}, samples), got {waveform.shape}",
        )
    if waveform.shape[1] == 0:
        raise SeparationError(ErrorCode.INVALID_AUDIO, "waveform is empty")
    if not np.isfinite(waveform).all():
        raise SeparationError(ErrorCode.INVALID_AUDIO, "waveform contains NaN or infinity")
    if float(np.abs(waveform).max()) > MAX_ABSOLUTE_AMPLITUDE:
        raise SeparationError(ErrorCode.INVALID_AUDIO, "waveform amplitude is out of range")


def _validate_stem(stem: np.ndarray, mixture: np.ndarray, profile: ModelProfile, np: Any) -> None:
    """Shape, sample-count, and value validation for every stem (plan 12.5)."""
    if stem.shape != mixture.shape:
        raise SeparationError(ErrorCode.INFERENCE_FAILED, "stem does not match the input shape")
    if not np.isfinite(stem).all():
        raise SeparationError(ErrorCode.INFERENCE_FAILED, "stem contains NaN or infinity")
    if float(np.abs(stem).max()) > MAX_ABSOLUTE_AMPLITUDE:
        raise SeparationError(ErrorCode.INFERENCE_FAILED, "stem amplitude is out of range")
