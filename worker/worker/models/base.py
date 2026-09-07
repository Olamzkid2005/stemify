"""Shared model-profile and error types (plan Section 12.2).

Pure Python: importing this module must not require torch, demucs, or numpy so
the worker control plane and tests stay dependency-light.
"""

from __future__ import annotations

from dataclasses import dataclass

from worker.errors import ErrorCode


@dataclass(frozen=True)
class ModelProfile:
    """Pinned description of one separation model (plan Section 12.2).

    A profile is immutable data: the adapter loads exactly what it names, from
    the exact checkpoint, and refuses anything not allowlisted here.
    """

    profile_id: str
    model_id: str
    # First 8 hex digits of the checkpoint sha256, as embedded in the published
    # filename (verified by torch.hub check_hash and re-verified at load time).
    checkpoint_checksum: str
    # Path under the model release root, e.g. "hybrid_transformer/955717e8-8726e21a.th".
    checkpoint_identifier: str
    revision: str
    sample_rate: int
    channels: int
    # Stems the model itself outputs, in model order.
    model_stems: tuple[str, ...]
    # Modes this profile may serve; full_stems stays disabled until benchmarks pass.
    supported_modes: tuple[str, ...]
    # Fixed per profile (plan Section 12.5): how the instrumental stem is derived.
    instrumental_policy: str
    device_policy: str
    # None -> use the model's own chunk length. overlap is the fraction shared
    # between consecutive chunks.
    chunk_length_seconds: float | None
    overlap: float
    precision: str
    license_reference: str


class SeparationError(Exception):
    """Model-layer failure carrying a stable public error code."""

    def __init__(self, code: ErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
