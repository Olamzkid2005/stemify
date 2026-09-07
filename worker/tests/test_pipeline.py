"""Worker test for the shared stage definitions."""

from worker.errors import ErrorCode
from worker.pipeline import PIPELINE_STAGES
from worker.stages import TERMINAL_STATUSES, Stage, Status


def test_pipeline_stages_are_defined_in_order() -> None:
    assert PIPELINE_STAGES == (
        Stage.STARTING,
        Stage.DOWNLOADING,
        Stage.VALIDATING,
        Stage.PREPARING_AUDIO,
        Stage.SEPARATING,
        Stage.ENCODING,
        Stage.PACKAGING,
        Stage.CLEANUP,
    )


def test_only_terminal_statuses_are_terminal() -> None:
    assert TERMINAL_STATUSES == frozenset(
        {Status.COMPLETED, Status.FAILED, Status.CANCELED, Status.EXPIRED}
    )
    assert Status.QUEUED not in TERMINAL_STATUSES
    assert ErrorCode.UNKNOWN  # error codes are importable and non-empty
