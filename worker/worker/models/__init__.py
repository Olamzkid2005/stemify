"""Model adapter layer (plan Task 9 / Section 12.2).

The rest of the worker talks to this package only; Demucs tensor details stay
inside worker.models.demucs.
"""

from worker.models.base import ModelProfile, SeparationError
from worker.models.profiles import DEFAULT_PROFILE_ID, MODEL_PROFILES, get_profile, validate_profile

__all__ = [
    "DEFAULT_PROFILE_ID",
    "MODEL_PROFILES",
    "ModelProfile",
    "SeparationError",
    "get_profile",
    "validate_profile",
]
