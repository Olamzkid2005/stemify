"""Task 10 tests: manifest construction and ZIP packaging.

Pure Python — no ffmpeg, torch, or model stack required. The manifest must
match packages/contracts/schemas/output-manifest.schema.json, which the
contracts tests exercise independently.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from worker.encoding import OutputError
from worker.errors import ErrorCode
from worker.packaging import (
    ARCHIVE_STEM_KEY,
    MANIFEST_NAME,
    STEM_LABELS,
    build_manifest,
    build_zip,
    write_manifest,
)

JOB_ID = "job_a1b2c3d4e5f60718"
SHA256 = "a" * 64


def _stems() -> list[dict[str, Any]]:
    return [
        {"name": "vocals", "durationSeconds": 1.0, "sha256": SHA256},
        {"name": "instrumental", "durationSeconds": 1.0, "sha256": SHA256},
    ]


def test_manifest_matches_shared_schema() -> None:
    manifest = build_manifest(
        job_id=JOB_ID,
        source_display_name="song.mp3",
        source_duration_seconds=1.0,
        source_sample_rate=44100,
        source_channels=2,
        mode="vocals_instrumental",
        output_format="mp3",
        stems=_stems(),
        model_id="htdemucs",
        model_revision="demucs==4.0.1",
        mixture_consistency=True,
    )
    assert manifest["schemaVersion"] == 1
    assert manifest["separation"]["mixtureConsistency"] is True
    assert [stem["name"] for stem in manifest["output"]["stems"]] == ["vocals", "instrumental"]

    # Validate against the real schema when jsonschema is installed, using the
    # same registry-based $ref resolution the contracts package uses.
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
    except ImportError:
        pytest.skip("jsonschema not installed")
    schemas_dir = Path(__file__).resolve().parents[2] / "packages" / "contracts" / "schemas"
    resources = []
    for name in ("common.schema.json", "output-manifest.schema.json"):
        schema = json.loads((schemas_dir / name).read_text(encoding="utf-8"))
        resources.append(
            (schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012))
        )
    validator = Draft202012Validator(
        json.loads((schemas_dir / "output-manifest.schema.json").read_text(encoding="utf-8")),
        registry=Registry().with_resources(resources),
    )
    validator.validate(manifest)


def test_manifest_rejects_bad_job_id() -> None:
    with pytest.raises(OutputError) as excinfo:
        build_manifest(
            job_id="short",
            source_display_name="song.mp3",
            source_duration_seconds=1.0,
            source_sample_rate=44100,
            source_channels=2,
            mode="vocals_instrumental",
            output_format="mp3",
            stems=_stems(),
            model_id="htdemucs",
            model_revision="demucs==4.0.1",
            mixture_consistency=True,
        )
    assert excinfo.value.code == ErrorCode.OUTPUT_VALIDATION_FAILED


def test_manifest_rejects_unknown_stem_name() -> None:
    stems = _stems()
    stems[0]["name"] = "guitar"
    with pytest.raises(OutputError) as excinfo:
        build_manifest(
            job_id=JOB_ID,
            source_display_name="song.mp3",
            source_duration_seconds=1.0,
            source_sample_rate=44100,
            source_channels=2,
            mode="vocals_instrumental",
            output_format="mp3",
            stems=stems,
            model_id="htdemucs",
            model_revision="demucs==4.0.1",
            mixture_consistency=True,
        )
    assert excinfo.value.code == ErrorCode.OUTPUT_VALIDATION_FAILED


def test_manifest_rejects_bad_sha256() -> None:
    stems = _stems()
    stems[0]["sha256"] = "not-hex"
    with pytest.raises(OutputError) as excinfo:
        build_manifest(
            job_id=JOB_ID,
            source_display_name="song.mp3",
            source_duration_seconds=1.0,
            source_sample_rate=44100,
            source_channels=2,
            mode="vocals_instrumental",
            output_format="mp3",
            stems=stems,
            model_id="htdemucs",
            model_revision="demucs==4.0.1",
            mixture_consistency=True,
        )
    assert excinfo.value.code == ErrorCode.OUTPUT_VALIDATION_FAILED


def test_manifest_rejects_out_of_range_duration() -> None:
    with pytest.raises(OutputError) as excinfo:
        build_manifest(
            job_id=JOB_ID,
            source_display_name="song.mp3",
            source_duration_seconds=0.0,
            source_sample_rate=44100,
            source_channels=2,
            mode="vocals_instrumental",
            output_format="mp3",
            stems=_stems(),
            model_id="htdemucs",
            model_revision="demucs==4.0.1",
            mixture_consistency=True,
        )
    assert excinfo.value.code == ErrorCode.OUTPUT_VALIDATION_FAILED


def test_zip_contains_stems_and_manifest(tmp_path: Path) -> None:
    stem_a = tmp_path / "vocals.mp3"
    stem_b = tmp_path / "instrumental.mp3"
    stem_a.write_bytes(b"vocals-bytes")
    stem_b.write_bytes(b"instrumental-bytes")
    manifest = build_manifest(
        job_id=JOB_ID,
        source_display_name="song.mp3",
        source_duration_seconds=1.0,
        source_sample_rate=44100,
        source_channels=2,
        mode="vocals_instrumental",
        output_format="mp3",
        stems=_stems(),
        model_id="htdemucs",
        model_revision="demucs==4.0.1",
        mixture_consistency=True,
    )
    dest = tmp_path / "results.zip"

    build_zip([("vocals", stem_a), ("instrumental", stem_b)], manifest, dest)

    with zipfile.ZipFile(dest) as archive:
        names = archive.namelist()
        assert names == ["vocals.mp3", "instrumental.mp3", MANIFEST_NAME]
        embedded = json.loads(archive.read(MANIFEST_NAME))
        assert embedded["jobId"] == JOB_ID
        # Deterministic timestamps (plan acceptance).
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in archive.infolist())


def test_zip_is_deterministic(tmp_path: Path) -> None:
    stem_a = tmp_path / "vocals.mp3"
    stem_a.write_bytes(b"vocals-bytes")
    manifest = build_manifest(
        job_id=JOB_ID,
        source_display_name="song.mp3",
        source_duration_seconds=1.0,
        source_sample_rate=44100,
        source_channels=2,
        mode="vocals_instrumental",
        output_format="mp3",
        stems=_stems()[:1],
        model_id="htdemucs",
        model_revision="demucs==4.0.1",
        mixture_consistency=True,
    )
    zip_one = tmp_path / "one.zip"
    zip_two = tmp_path / "two.zip"
    build_zip([("vocals", stem_a)], manifest, zip_one)
    build_zip([("vocals", stem_a)], manifest, zip_two)
    assert zip_one.read_bytes() == zip_two.read_bytes()


def test_zip_rejects_wrong_stem_extension(tmp_path: Path) -> None:
    stem = tmp_path / "vocals.wav"  # manifest says mp3
    stem.write_bytes(b"wav-bytes")
    manifest = build_manifest(
        job_id=JOB_ID,
        source_display_name="song.mp3",
        source_duration_seconds=1.0,
        source_sample_rate=44100,
        source_channels=2,
        mode="vocals_instrumental",
        output_format="mp3",
        stems=_stems()[:1],
        model_id="htdemucs",
        model_revision="demucs==4.0.1",
        mixture_consistency=True,
    )
    dest = tmp_path / "results.zip"
    with pytest.raises(OutputError) as excinfo:
        build_zip([("vocals", stem)], manifest, dest)
    assert excinfo.value.code == ErrorCode.OUTPUT_VALIDATION_FAILED
    assert not dest.exists()  # partial archive never left behind


def test_write_manifest_is_deterministic(tmp_path: Path) -> None:
    manifest = build_manifest(
        job_id=JOB_ID,
        source_display_name="song.mp3",
        source_duration_seconds=1.0,
        source_sample_rate=44100,
        source_channels=2,
        mode="vocals_instrumental",
        output_format="mp3",
        stems=_stems(),
        model_id="htdemucs",
        model_revision="demucs==4.0.1",
        mixture_consistency=True,
    )
    dest = tmp_path / "sub" / "manifest.json"
    write_manifest(manifest, dest)
    first = dest.read_bytes()
    write_manifest(manifest, dest)
    assert dest.read_bytes() == first
    assert json.loads(dest.read_text(encoding="utf-8")) == manifest


def test_labels_cover_contract_stem_keys() -> None:
    for stem_key in ("vocals", "instrumental", "drums", "bass", "other"):
        assert STEM_LABELS[stem_key]
    assert ARCHIVE_STEM_KEY not in STEM_LABELS  # the ZIP is not a previewable stem
