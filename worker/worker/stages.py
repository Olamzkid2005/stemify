"""Progress stage definitions shared by the worker pipeline.

Stage order and the public progress mapping live in the product plan (Section 14).
The downloading stage applies only to YouTube sources; upload jobs start at validating.
"""

from __future__ import annotations

from enum import StrEnum


class Stage(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    VALIDATING = "validating"
    PREPARING_AUDIO = "preparing_audio"
    SEPARATING = "separating"
    ENCODING = "encoding"
    UPLOADING_RESULTS = "uploading_results"
    CLEANUP = "cleanup"


class Status(StrEnum):
    """Public job statuses (Section 30.4). No job moves backward from a terminal state."""

    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    EXPIRED = "expired"


TERMINAL_STATUSES: frozenset[Status] = frozenset(
    {Status.COMPLETED, Status.FAILED, Status.CANCELED, Status.EXPIRED}
)
