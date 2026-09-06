"""Guard against drift between worker enums and the shared contract schemas.

If this test fails, update worker enums and packages/contracts/schemas in the
same change — the schemas are the contract.
"""

from __future__ import annotations

import json
from pathlib import Path

from worker.errors import ErrorCode
from worker.stages import Stage, Status

COMMON_SCHEMA = (
    Path(__file__).resolve().parents[2]
    / "packages"
    / "contracts"
    / "schemas"
    / "common.schema.json"
)


def _enum_values(name: str) -> list[str]:
    schema = json.loads(COMMON_SCHEMA.read_text(encoding="utf-8"))
    values = schema["$defs"][name]["enum"]
    return sorted(values)


def test_error_codes_match_contract() -> None:
    assert sorted(code.value for code in ErrorCode) == _enum_values("publicErrorCode")


def test_statuses_match_contract() -> None:
    assert sorted(status.value for status in Status) == _enum_values("jobStatus")


def test_stages_match_contract() -> None:
    schema_stages = set(_enum_values("jobStage")) - {"completed"}
    assert sorted(stage.value for stage in Stage) == sorted(schema_stages)
