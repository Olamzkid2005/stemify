"""Drumsep adapter (roadmap Phase B): kick/snare/cymbals/toms subdivision.

The drumsep checkpoint (inagoy/drumsep, MIT) is a hybrid-demucs model served
as a single .th file, NOT part of the demucs remote index. It loads through
demucs' local-repo API (`get_model(name, repo=directory)`) and runs through
the same `apply_model` path as the main adapter, so tensor handling, progress
boundaries, and cancellation semantics mirror worker.models.demucs.

The model's internal source names are not pinned by any published spec, so
mapping is name-based first (kick/bombo, snare/redoblante, cymbals/platillos,
toms) with positional fallback (the drumsep README's documented output order).
A wrong mapping would produce wrongly-labeled stems, so the name path is
strict: only exact known names map by name; everything else falls back to
order and records nothing.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from worker.errors import ErrorCode
from worker.models.base import SeparationError
from worker.models.demucs import (
    _configure_model_cache,
    _default_model_dir,
    _import_demucs_pretrained,
    _import_numpy,
    _import_torch,
    _validate_stem,
    _validate_waveform,
    _verify_checkpoint_checksum,
    _weights_only_compat,
    resolve_device,
)
from worker.models.profiles import DRUMSEP_PROFILE, get_profile, validate_profile

if TYPE_CHECKING:  # pragma: no cover
    from worker.models.base import ModelProfile

from collections.abc import Callable

ProgressCallback = Callable[[float], None]
CancellationChecker = Callable[[], bool]

# The checkpoint lives in its own local-repo directory (not the torch.hub
# checkpoint layout): <model_dir>/drumsep/49469ca8.th, and
# get_model(name, repo=that directory) resolves it.
REPO_SUBDIR = "drumsep"

# Exact model-source-name -> stem-key mapping (English + the Spanish names
# drumsep's README documents). Anything unrecognized falls back to position.
_SOURCE_NAME_MAP: dict[str, str] = {
    "kick": "drums_kick",
    "bombo": "drums_kick",
    "snare": "drums_snare",
    "redoblante": "drums_snare",
    "cymbals": "drums_cymbals",
    "platillos": "drums_cymbals",
    "toms": "drums_toms",
}


def _repo_dir(model_dir: Any) -> Any:
    return model_dir / REPO_SUBDIR


def _checkpoint_path(model_dir: Any) -> Any:
    return _repo_dir(model_dir) / DRUMSEP_PROFILE.checkpoint_identifier


def _source_key_order(model_sources: list[str]) -> list[str]:
    """Map model source order onto our stem keys (name-first, positional fallback)."""
    by_name = [_SOURCE_NAME_MAP.get(name.strip().lower()) for name in model_sources]
    if all(by_name) and sorted(by_name) == sorted(DRUMSEP_PROFILE.model_stems):
        return by_name  # type: ignore[arg-type]
    if len(model_sources) == len(DRUMSEP_PROFILE.model_stems):
        return list(DRUMSEP_PROFILE.model_stems)
    raise SeparationError(
        ErrorCode.INFERENCE_FAILED,
        f"drumsep model emitted {len(model_sources)} sources; expected 4 known drum parts",
    )


def separate(
    waveform: Any,
    profile: ModelProfile | None = None,
    mode: str = "drum_breakdown",
    progress_callback: ProgressCallback | None = None,
    cancellation_checker: CancellationChecker | None = None,
) -> dict[str, Any]:
    """Split a canonical stereo DRUMS waveform into drum-part stems.

    Mirrors worker.models.demucs.separate() so the pipeline can dispatch on
    mode with an identical call shape.
    """
    profile = profile or get_profile("drumsep")
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

    resolved = resolve_device(profile.device_policy)
    model_dir = _default_model_dir()
    _configure_model_cache(model_dir)
    repo = _repo_dir(model_dir)
    checkpoint = _checkpoint_path(model_dir)
    if not checkpoint.is_file():
        raise SeparationError(
            ErrorCode.MODEL_LOAD_FAILED,
            "the drum subdivision model is not installed; see docs/ROADMAP.md Phase B "
            f"for the one-time download (expected at {checkpoint})",
        )
    if profile.checkpoint_checksum:
        _verify_checkpoint_checksum(profile, checkpoint)

    demucs_pretrained = _import_demucs_pretrained()
    try:
        with _weights_only_compat():
            # Upstream (inagoy/drumsep's own script) loads the model by the
            # CHECKPOINT FILE STEM ("demucs --repo model -n 49469ca8"), not by
            # a bag name — demucs' LocalRepo indexes .th files by stem, and no
            # drumsep.yaml exists. model_id ("drumsep") stays as the manifest
            # label; the repo lookup must use the signature.
            model = demucs_pretrained.get_model(checkpoint.stem, repo=repo)
        model.to(resolved)
        model.eval()
    except SeparationError:
        raise
    except Exception as error:
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, f"failed to load model: {error}") from error

    # Bootstrap aid: with no pinned checksum yet, surface the hash of the file
    # that was just loaded so the reference machine can pin it in profiles.py.
    if not profile.checkpoint_checksum:
        import hashlib

        sha = hashlib.sha256()
        with checkpoint.open("rb") as handle:
            for block in iter(lambda: handle.read(2**20), b""):
                sha.update(block)
        print(
            f"drumsep: pin this checkpoint checksum in worker/worker/models/profiles.py: {sha.hexdigest()[:8]}",
            file=sys.stderr,
        )

    th = _import_torch()
    tensor: Any = None
    try:
        tensor = th.from_numpy(waveform.astype("float32")).to(resolved)[None]
        if progress_callback:
            progress_callback(0.0)
        from demucs.apply import apply_model

        with th.no_grad():
            estimates = apply_model(
                model,
                tensor,
                device=resolved,
                shifts=0,
                split=True,
                overlap=profile.overlap,
                segment=profile.chunk_length_seconds,
                progress=False,
            )[0]
        if cancellation_checker and cancellation_checker():
            raise SeparationError(ErrorCode.CANCELED, "canceled during separation")
        if progress_callback:
            progress_callback(1.0)
    except SeparationError:
        raise
    except RuntimeError as error:
        if "out of memory" in str(error).lower():
            if resolved == "cuda":
                th.cuda.empty_cache()
            raise SeparationError(ErrorCode.GPU_OUT_OF_MEMORY, "the device ran out of memory") from error
        raise SeparationError(ErrorCode.INFERENCE_FAILED, "drum subdivision failed unexpectedly") from error
    finally:
        del tensor

    stems_np = estimates.cpu().numpy()
    del estimates
    if resolved == "cuda":
        th.cuda.empty_cache()

    keys = _source_key_order(list(model.sources))
    result: dict[str, Any] = {}
    for index, stem_key in enumerate(keys):
        stem = stems_np[index]
        _validate_stem(stem, waveform, profile, np)
        result[stem_key] = stem
    return result
