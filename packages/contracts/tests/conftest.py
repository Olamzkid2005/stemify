"""Shared fixtures: load every contract schema into a referencing registry.

Cross-file $ref targets such as "common.schema.json#/$defs/..." resolve through
the registry using each schema's $id as its base URI.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"

SCHEMA_FILES = (
    "common.schema.json",
    "job-request.schema.json",
    "job-status.schema.json",
    "job-events.schema.json",
    "output-manifest.schema.json",
)


def _load_registry() -> Registry:
    resources = []
    for name in SCHEMA_FILES:
        schema = json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012)))
    return Registry().with_resources(resources)


@pytest.fixture(scope="session")
def registry() -> Registry:
    return _load_registry()


def validator_for(name: str, registry: Registry) -> Draft202012Validator:
    schema = json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))
    return Draft202012Validator(
        schema,
        registry=registry,
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
