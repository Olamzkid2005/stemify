"""Contract validation tests: valid fixtures pass, invalid fixtures are rejected."""

from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator

from tests.conftest import validator_for

JOB_ID = "job_a1b2c3d4e5f60718"
EVENT_ID = "evt_a1b2c3d4e5f60718"
UPLOAD_ID = "upl_a1b2c3d4e5f60718"
OBJECT_KEY = f"sources/{UPLOAD_ID}/input.mp3"
SHA256 = "a" * 64


def assert_invalid(validator: Draft202012Validator, instance) -> None:
    assert list(validator.iter_errors(instance)), f"expected rejection: {instance!r}"


# --- job-request ---------------------------------------------------------


def test_job_request_accepts_upload_source(registry) -> None:
    validator = validator_for("job-request.schema.json", registry)
    validator.validate(
        {
            "source": {
                "type": "upload",
                "uploadId": UPLOAD_ID,
                "objectKey": OBJECT_KEY,
                "filename": "song.mp3",
            },
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "idempotencyKey": "0123456789abcdef",
        }
    )


def test_job_request_accepts_youtube_source(registry) -> None:
    validator = validator_for("job-request.schema.json", registry)
    validator.validate(
        {
            "source": {"type": "youtube", "url": "https://www.youtube.com/watch?v=abc123"},
            "mode": "full_stems",
            "outputFormat": "wav",
            "idempotencyKey": "0123456789abcdef",
        }
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"mode": "karaoke"},
        {"outputFormat": "aac"},
        {"idempotencyKey": "short"},
        {"source": {"type": "upload", "uploadId": UPLOAD_ID, "objectKey": "../../etc/passwd", "filename": "x.mp3"}},
        {"source": {"type": "youtube", "url": "not-a-url"}},
    ],
)
def test_job_request_rejects_invalid_variants(registry, patch) -> None:
    validator = validator_for("job-request.schema.json", registry)
    base = {
        "source": {"type": "youtube", "url": "https://youtu.be/abc123"},
        "mode": "vocals_instrumental",
        "outputFormat": "mp3",
        "idempotencyKey": "0123456789abcdef",
    }
    assert_invalid(validator, {**base, **patch})


# --- job-status ----------------------------------------------------------


def test_job_status_accepts_processing(registry) -> None:
    validator = validator_for("job-status.schema.json", registry)
    validator.validate(
        {
            "jobId": JOB_ID,
            "status": "processing",
            "stage": "separating",
            "progress": 54,
            "source": {"filename": "song.mp3"},
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:01:12Z",
        }
    )


def test_job_status_accepts_completed_with_stems(registry) -> None:
    validator = validator_for("job-status.schema.json", registry)
    validator.validate(
        {
            "jobId": JOB_ID,
            "status": "completed",
            "stage": "completed",
            "progress": 100,
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "stems": [
                {"id": "vocals", "label": "Vocals", "durationSeconds": 214.2},
                {"id": "instrumental", "label": "Instrumental", "durationSeconds": 214.2},
            ],
            "downloadUrl": f"/api/jobs/{JOB_ID}/downloads?kind=zip",
            "expiresAt": "2026-09-07T00:00:00Z",
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:05:00Z",
        }
    )


def test_job_status_rejects_completed_without_stems(registry) -> None:
    validator = validator_for("job-status.schema.json", registry)
    assert_invalid(
        validator,
        {
            "jobId": JOB_ID,
            "status": "completed",
            "stage": "completed",
            "progress": 100,
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:05:00Z",
        },
    )


def test_job_status_rejects_failed_without_error_code(registry) -> None:
    validator = validator_for("job-status.schema.json", registry)
    assert_invalid(
        validator,
        {
            "jobId": JOB_ID,
            "status": "failed",
            "stage": "separating",
            "progress": 40,
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:05:00Z",
        },
    )


def test_job_status_rejects_progress_out_of_range(registry) -> None:
    validator = validator_for("job-status.schema.json", registry)
    assert_invalid(
        validator,
        {
            "jobId": JOB_ID,
            "status": "processing",
            "stage": "separating",
            "progress": 101,
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:01:12Z",
        },
    )


# --- job-events ----------------------------------------------------------


def test_worker_event_accepts_progress(registry) -> None:
    validator = validator_for("job-events.schema.json", registry)
    validator.validate(
        {
            "jobId": JOB_ID,
            "eventId": EVENT_ID,
            "status": "processing",
            "stage": "encoding",
            "progress": 82,
            "workerCallId": "call_12345678",
        }
    )


def test_worker_event_accepts_completed_with_outputs(registry) -> None:
    validator = validator_for("job-events.schema.json", registry)
    validator.validate(
        {
            "jobId": JOB_ID,
            "eventId": EVENT_ID,
            "status": "completed",
            "stage": "completed",
            "progress": 100,
            "workerCallId": "call_12345678",
            "outputs": [
                {
                    "stemKey": "vocals",
                    "label": "Vocals",
                    "objectKey": f"results/{JOB_ID}/vocals.mp3",
                    "mimeType": "audio/mpeg",
                    "sizeBytes": 3400000,
                    "durationSeconds": 214.2,
                    "sha256": SHA256,
                }
            ],
        }
    )


def test_worker_event_accepts_failed_with_error(registry) -> None:
    validator = validator_for("job-events.schema.json", registry)
    validator.validate(
        {
            "jobId": JOB_ID,
            "eventId": EVENT_ID,
            "status": "failed",
            "stage": "separating",
            "progress": 40,
            "error": {"code": "MODEL_LOAD_FAILED", "diagnosticReference": "diag_12345678"},
        }
    )


def test_worker_event_rejects_completed_without_outputs(registry) -> None:
    validator = validator_for("job-events.schema.json", registry)
    assert_invalid(
        validator,
        {"jobId": JOB_ID, "eventId": EVENT_ID, "status": "completed", "stage": "completed", "progress": 100},
    )


def test_worker_event_rejects_completed_with_error(registry) -> None:
    validator = validator_for("job-events.schema.json", registry)
    assert_invalid(
        validator,
        {
            "jobId": JOB_ID,
            "eventId": EVENT_ID,
            "status": "completed",
            "stage": "completed",
            "progress": 100,
            "outputs": [
                {
                    "stemKey": "vocals",
                    "label": "Vocals",
                    "objectKey": f"results/{JOB_ID}/vocals.mp3",
                    "mimeType": "audio/mpeg",
                    "sizeBytes": 3400000,
                    "durationSeconds": 214.2,
                    "sha256": SHA256,
                }
            ],
            "error": {"code": "UNKNOWN"},
        },
    )


def test_worker_event_rejects_unknown_error_code(registry) -> None:
    validator = validator_for("job-events.schema.json", registry)
    assert_invalid(
        validator,
        {
            "jobId": JOB_ID,
            "eventId": EVENT_ID,
            "status": "failed",
            "stage": "separating",
            "progress": 10,
            "error": {"code": "EXPLODED"},
        },
    )


# --- output-manifest -----------------------------------------------------


def _manifest() -> dict:
    return {
        "schemaVersion": 1,
        "jobId": JOB_ID,
        "source": {
            "displayName": "song.mp3",
            "durationSeconds": 214.2,
            "sampleRate": 44100,
            "channels": 2,
        },
        "separation": {
            "mode": "vocals_instrumental",
            "modelId": "htdemucs",
            "modelRevision": "main",
            "mixtureConsistency": False,
        },
        "output": {
            "format": "mp3",
            "stems": [
                {"name": "vocals", "durationSeconds": 214.2, "sha256": SHA256},
                {"name": "instrumental", "durationSeconds": 214.2, "sha256": SHA256},
            ],
        },
    }


def test_output_manifest_accepts_valid_manifest(registry) -> None:
    validator = validator_for("output-manifest.schema.json", registry)
    validator.validate(_manifest())


def test_output_manifest_rejects_wrong_schema_version(registry) -> None:
    validator = validator_for("output-manifest.schema.json", registry)
    assert_invalid(validator, {**_manifest(), "schemaVersion": 2})


def test_output_manifest_rejects_unknown_stem_name(registry) -> None:
    validator = validator_for("output-manifest.schema.json", registry)
    bad = _manifest()
    bad["output"]["stems"][0]["name"] = "guitar"
    assert_invalid(validator, bad)
