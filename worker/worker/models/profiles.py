"""Model profile allowlist (plan Sections 12.2, 14.3).

Only profiles defined here may be loaded; checkpoints are verified against the
recorded checksum, and nothing is downloaded outside the configured cache.
"""

from __future__ import annotations

import os

from worker.errors import ErrorCode
from worker.models.base import ModelProfile, SeparationError

DEFAULT_PROFILE_ID = "demucs_default"

# htdemucs (demucs 4.0.1 default): a single hybrid-transformer checkpoint served
# from https://dl.fbaipublicfiles.com/demucs/ and loaded through torch.hub with
# check_hash=True. Code license: MIT (facebookresearch/demucs). Re-check the
# model license before any redistribution or commercial use (plan Section 14.3).
DEFAULT_PROFILE = ModelProfile(
    profile_id=DEFAULT_PROFILE_ID,
    model_id="htdemucs",
    checkpoint_checksum="8726e21a",
    checkpoint_identifier="hybrid_transformer/955717e8-8726e21a.th",
    revision="demucs==4.0.1",
    sample_rate=44100,
    channels=2,
    model_stems=("drums", "bass", "other", "vocals"),
    # full_stems serves the non-vocal rhythm-section split (drums, bass and the
    # residual instrumental bed) from this same checkpoint. The experimental
    # htdemucs_6s variant was dropped: its guitar/piano sources were not
    # accurate enough to ship, and the shared stems did not sound different.
    supported_modes=("vocals_instrumental", "full_stems"),
    instrumental_policy="mixture_minus_vocals",
    device_policy="auto",
    chunk_length_seconds=None,  # model default segment
    overlap=0.4,
    inference_shifts=2,
    precision="float32",
    license_reference="MIT (facebookresearch/demucs), model weights MIT",
)

# drumsep (inagoy/drumsep, 2022): a hybrid-demucs checkpoint trained to split a
# DRUM RECORDING into kick / snare / cymbals / toms. It is not served from the
# demucs remote index: the artifact is a single 49469ca8.th file (Google Drive,
# per the project's own install script) loaded through demucs' local-repo API.
# Code license: MIT (inagoy/drumsep). Checksum note: the artifact has no
# publisher-published hash, so checkpoint_checksum was EMPTY until pinned from
# a first verified download on the reference machine (the adapter prints the
# sha256 it sees and refuses to load once a hash has been recorded). Pinned
# from the verified download on the reference machine (2026-09-09).
# Fetch on a new machine:
#   curl -L -C - -o data/models/drumsep/49469ca8.th \
#     "https://drive.usercontent.google.com/download?id=1-Dm666ScPkg8Gt2-lK3Ua0xOudWHZBGC&export=download&confirm=t"
DRUMSEP_PROFILE = ModelProfile(
    profile_id="drumsep",
    model_id="drumsep",
    checkpoint_checksum="aefaa854",
    checkpoint_identifier="49469ca8.th",
    revision="inagoy/drumsep (2022)",
    sample_rate=44100,
    channels=2,
    model_stems=("drums_kick", "drums_snare", "drums_cymbals", "drums_toms"),
    supported_modes=("drum_breakdown",),
    instrumental_policy="direct_model_output",
    device_policy="auto",
    chunk_length_seconds=None,  # model default segment
    overlap=0.4,
    inference_shifts=2,
    precision="float32",
    license_reference="MIT (inagoy/drumsep), model weights by the drumsep authors",
)

MODEL_PROFILES: dict[str, ModelProfile] = {
    DEFAULT_PROFILE.profile_id: DEFAULT_PROFILE,
    DRUMSEP_PROFILE.profile_id: DRUMSEP_PROFILE,
}

# STEMIFY_QUALITY presets (docs/BENCHMARKS.md): runtime inference settings that
# override a profile's pinned overlap/shifts. The profile fields remain the
# allowlisted "balanced" defaults; the preset is the operator's speed/quality
# dial. Each extra shift is one more full inference pass (passes = shifts + 1),
# and higher overlap adds chunks, so time grows roughly linearly with both.
QUALITY_PRESETS: dict[str, tuple[float, int]] = {
    "fast": (0.25, 0),  # demucs defaults: 1 pass, minimal stitching
    "balanced": (0.4, 2),  # the tuned defaults from the 2026-09 quality pass
    "best": (0.45, 5),  # 6 passes, tightest stitching
}
DEFAULT_QUALITY = "balanced"


def resolve_quality(preset: str | None = None) -> tuple[str, float, int]:
    """Resolve a quality preset to (preset_name, overlap, inference_shifts).

    An explicit preset (per-job column, validated app-side) wins; otherwise
    STEMIFY_QUALITY decides; otherwise the balanced default. Unknown values
    fail loudly with MODEL_LOAD_FAILED instead of silently processing at a
    surprise quality (mirrors resolve_device's strictness).
    """
    raw = (preset or os.environ.get("STEMIFY_QUALITY") or DEFAULT_QUALITY).strip().lower()
    quality = QUALITY_PRESETS.get(raw)
    if quality is None:
        raise SeparationError(
            ErrorCode.MODEL_LOAD_FAILED,
            f"unknown quality preset {raw!r}; expected one of {sorted(QUALITY_PRESETS)}",
        )
    return raw, quality[0], quality[1]

_VALID_MODES = frozenset({"vocals_instrumental", "full_stems", "drum_breakdown"})
_VALID_DEVICES = frozenset({"auto", "cpu", "cuda"})
_INSTRUMENTAL_POLICIES = frozenset({"mixture_minus_vocals", "direct_model_output"})


def validate_profile(profile: ModelProfile) -> None:
    """Reject any profile that is not exactly as allowlisted (raises SeparationError)."""
    expected = MODEL_PROFILES.get(profile.profile_id)
    if expected is None or profile != expected:
        raise SeparationError(
            ErrorCode.MODEL_LOAD_FAILED,
            f"model profile {profile.profile_id!r} is not allowlisted; "
            f"expected one of {sorted(MODEL_PROFILES)}",
        )
    _check_invariants(profile)


def get_profile(profile_id: str) -> ModelProfile:
    """Return the allowlisted profile for profile_id or raise MODEL_LOAD_FAILED."""
    profile = MODEL_PROFILES.get(profile_id)
    if profile is None:
        raise SeparationError(
            ErrorCode.MODEL_LOAD_FAILED,
            f"unknown model profile {profile_id!r}; expected one of {sorted(MODEL_PROFILES)}",
        )
    return profile


def get_profile_for_mode(mode: str) -> ModelProfile:
    """Return the allowlisted profile that serves the requested mode.

    STEMIFY_MODEL_PROFILE stays an explicit override: when the selected profile
    supports the mode it is used. Otherwise the first allowlisted profile (in
    registration order) that supports the mode is chosen: vocals_instrumental
    and full_stems -> the default 4-stem model, drum_breakdown -> drumsep.
    """
    selected = get_profile(os.environ.get("STEMIFY_MODEL_PROFILE", DEFAULT_PROFILE_ID))
    if mode in selected.supported_modes:
        return selected
    for profile in MODEL_PROFILES.values():
        if mode in profile.supported_modes:
            return profile
    raise SeparationError(
        ErrorCode.MODEL_LOAD_FAILED,
        f"no allowlisted profile supports mode {mode!r}",
    )


def _check_invariants(profile: ModelProfile) -> None:
    """Structural sanity; the allowlist equality above is the real gate."""
    if profile.checkpoint_checksum.lower() != profile.checkpoint_checksum:
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, "checkpoint checksum must be lowercase hex")
    # Only whole-track modes must carry a vocals stem; the drum-subdivision
    # profile operates on the drums stem and has no vocals output.
    serves_whole_track = "vocals_instrumental" in profile.supported_modes or "full_stems" in profile.supported_modes
    if serves_whole_track and (not profile.model_stems or "vocals" not in profile.model_stems):
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, "profile must include a vocals stem")
    if not set(profile.supported_modes) <= _VALID_MODES:
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, "unsupported separation mode in profile")
    if "full_stems" in profile.supported_modes and len(profile.model_stems) < 4:
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, "full stems requires a four-stem profile")
    if profile.device_policy not in _VALID_DEVICES:
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, "unsupported device policy")
    if profile.instrumental_policy not in _INSTRUMENTAL_POLICIES:
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, "unsupported instrumental policy")
    if profile.sample_rate <= 0 or profile.channels not in (1, 2):
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, "unsupported sample rate or channel layout")
    if not 0 <= profile.overlap < 0.5:
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, "overlap must be in [0, 0.5)")
    if profile.inference_shifts < 0 or profile.inference_shifts > 5:
        raise SeparationError(ErrorCode.MODEL_LOAD_FAILED, "inference_shifts must be in [0, 5]")
