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
    # Full-stem mode is only enabled once Task 18 benchmarks pass on the
    # supported local hardware (plan Sections 4.3, 18).
    supported_modes=("vocals_instrumental",),
    instrumental_policy="mixture_minus_vocals",
    device_policy="auto",
    chunk_length_seconds=None,  # model default segment
    overlap=0.25,
    precision="float32",
    license_reference="MIT (facebookresearch/demucs), model weights MIT",
)

# htdemucs_6s (demucs 4.0.1 experimental 6-source variant): same hybrid-transformer
# family as the default profile, adding real piano and guitar sources. Served
# from the same dl.fbaipublicfiles.com root and loaded through torch.hub with
# check_hash=True (checkpoint identity: hybrid_transformer/5c90dfd2-34c22ccb.th,
# from demucs 4.0.1's demucs/remote/files.txt).
SIX_STEM_PROFILE = ModelProfile(
    profile_id="demucs_6s",
    model_id="htdemucs_6s",
    checkpoint_checksum="34c22ccb",
    checkpoint_identifier="hybrid_transformer/5c90dfd2-34c22ccb.th",
    revision="demucs==4.0.1",
    sample_rate=44100,
    channels=2,
    model_stems=("drums", "bass", "other", "vocals", "guitar", "piano"),
    supported_modes=("vocals_instrumental", "full_stems"),
    instrumental_policy="mixture_minus_vocals",
    device_policy="auto",
    chunk_length_seconds=None,  # model default segment
    overlap=0.25,
    precision="float32",
    license_reference="MIT (facebookresearch/demucs), model weights MIT",
)

MODEL_PROFILES: dict[str, ModelProfile] = {
    DEFAULT_PROFILE.profile_id: DEFAULT_PROFILE,
    SIX_STEM_PROFILE.profile_id: SIX_STEM_PROFILE,
}

_VALID_MODES = frozenset({"vocals_instrumental", "full_stems"})
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
    registration order: default 4-stem first, then the 6-stem model) that
    supports the mode is chosen, so full_stems jobs resolve to the 6-stem
    profile without any per-job configuration.
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
    if not profile.model_stems or "vocals" not in profile.model_stems:
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
