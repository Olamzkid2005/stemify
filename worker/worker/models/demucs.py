"""Demucs adapter (plan Task 9 / Sections 12.2-12.3, 12.5).

The rest of the worker only sees numpy waveforms: torch and demucs imports are
lazy, so this module imports cleanly without the model stack.

Inference policy (fixed for this adapter):
- Uses demucs' own maintained chunked inference path (apply_model, split=True),
  shifts=0, float32. No custom stitching.
- 4.0.1 apply_model exposes no per-chunk callback, so progress_callback fires
  at stage boundaries (0.0 before, 1.0 after) and cancellation is checked
  before and after inference. Coarse on purpose; see plan Section 13.1.
- Instrumental policy per profile (demucs_default): mixture minus vocals,
  which is mixture-consistent by construction.
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
from worker.models.profiles import get_profile, get_profile_for_mode, validate_profile

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
) -> dict[str, np.ndarray]:
    """Separate a canonical stereo waveform into named numpy stems (plan 12.5)."""
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

    tensor: Any = None
    try:
        tensor = th.from_numpy(waveform.astype("float32")).to(device)[None]  # (1, channels, samples)
        if progress_callback:
            progress_callback(0.0)
        from demucs.apply import apply_model

        with th.no_grad():
            estimates = apply_model(
                model,
                tensor,
                device=device,
                shifts=0,
                split=True,
                overlap=profile.overlap,
                segment=profile.chunk_length_seconds,
                progress=False,
            )[0]  # (sources, channels, samples)
        if cancellation_checker and cancellation_checker():
            raise SeparationError(ErrorCode.CANCELED, "canceled during separation")
        if progress_callback:
            progress_callback(1.0)
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
        result.update(stems)
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
