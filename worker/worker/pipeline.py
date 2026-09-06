"""One job's pipeline orchestration: retrieve, validate, separate, encode, upload.

Implementation arrives with Tasks 8-10 of the product plan. This module currently
exposes the pipeline stage order so callers and tests share one definition.
"""

from __future__ import annotations

from worker.stages import Stage

PIPELINE_STAGES: tuple[Stage, ...] = (
    Stage.DOWNLOADING,
    Stage.VALIDATING,
    Stage.PREPARING_AUDIO,
    Stage.SEPARATING,
    Stage.ENCODING,
    Stage.UPLOADING_RESULTS,
    Stage.CLEANUP,
)
