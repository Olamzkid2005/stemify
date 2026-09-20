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


def test_job_request_accepts_spotify_source(registry) -> None:
    validator = validator_for("job-request.schema.json", registry)
    validator.validate(
        {
            "source": {"type": "spotify", "url": "spotify:track:4cOdK2wGLETKBW3PvgPWqT"},
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "quality": "balanced",
            "idempotencyKey": "0123456789abcdef",
        }
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"mode": "karaoke"},
        {"outputFormat": "aac"},
        {"idempotencyKey": "short"},
        {"quality": "ultra"},
        {"source": {"type": "upload", "uploadId": UPLOAD_ID, "objectKey": "../../etc/passwd", "filename": "x.mp3"}},
        {"source": {"type": "youtube", "url": "not-a-url"}},
        # Single tracks only: albums and playlists are the batch follow-up.
        {"source": {"type": "spotify", "url": "https://open.spotify.com/album/4cOdK2wGLETKBW3PvgPWqT"}},
        {"source": {"type": "spotify", "url": "spotify:track:notatrackid"}},
        {"source": {"type": "spotify", "url": "https://evil.example.com/track/4cOdK2wGLETKBW3PvgPWqT"}},
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


def test_job_status_accepts_unresolved_link_filename(registry) -> None:
    """A link job can be visible before its optional title probe resolves."""
    validator = validator_for("job-status.schema.json", registry)
    validator.validate(
        {
            "jobId": JOB_ID,
            "status": "queued",
            "stage": "starting",
            "progress": 0,
            "source": {"type": "youtube", "filename": None},
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:00:00Z",
        }
    )


def test_job_status_accepts_stage_timings(registry) -> None:
    """Per-stage elapsed timing: closed spans plus the running one, which has no
    endedAt because the worker is still inside that stage."""
    validator = validator_for("job-status.schema.json", registry)
    validator.validate(
        {
            "jobId": JOB_ID,
            "status": "processing",
            "stage": "separating",
            "progress": 61,
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:04:05Z",
            "stageTimings": [
                {
                    "stage": "starting",
                    "startedAt": "2026-09-06T00:00:00Z",
                    "endedAt": "2026-09-06T00:00:12Z",
                },
                {"stage": "separating", "startedAt": "2026-09-06T00:01:00Z"},
            ],
        }
    )


def test_job_status_rejects_unknown_stage_in_timings(registry) -> None:
    validator = validator_for("job-status.schema.json", registry)
    assert_invalid(
        validator,
        {
            "jobId": JOB_ID,
            "status": "processing",
            "stage": "separating",
            "progress": 61,
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:04:05Z",
            "stageTimings": [
                {"stage": "encoding_stems", "startedAt": "2026-09-06T00:01:00Z"},
            ],
        },
    )


def test_job_status_accepts_spotify_source(registry) -> None:
    validator = validator_for("job-status.schema.json", registry)
    validator.validate(
        {
            "jobId": JOB_ID,
            "status": "processing",
            "stage": "downloading",
            "progress": 15,
            "progressMessage": "Streaming audio from Spotify",
            "source": {"type": "spotify", "filename": "Artist - Song.ogg"},
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:01:12Z",
        }
    )


def test_job_status_accepts_richer_source_metadata(registry) -> None:
    """Album and cover are optional, and the cover URL is a local route.

    The worker saves the cover into the job's results directory and this app
    serves it, so a status response must never point the browser at Spotify's
    CDN. Both fields are absent for uploads and whenever a lookup failed.
    """
    validator = validator_for("job-status.schema.json", registry)
    validator.validate(
        {
            "jobId": JOB_ID,
            "status": "processing",
            "stage": "separating",
            "progress": 40,
            "userStage": "Separating stems",
            "progressMessage": "Running the separation model",
            "source": {
                "type": "spotify",
                "filename": "Olamide - Owotabua.ogg",
                "album": "Owotabua",
                "artworkUrl": f"/api/jobs/{JOB_ID}/artwork",
            },
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "workerRunning": True,
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:01:12Z",
        }
    )


def test_job_status_rejects_a_remote_artwork_url(registry) -> None:
    """A CDN URL would leak a third-party request out of the UI."""
    validator = validator_for("job-status.schema.json", registry)
    assert_invalid(
        validator,
        {
            "jobId": JOB_ID,
            "status": "processing",
            "stage": "separating",
            "progress": 40,
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "source": {
                "type": "spotify",
                "filename": "Olamide - Owotabua.ogg",
                "artworkUrl": "https://i.scdn.co/image/deadbeef",
            },
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:01:12Z",
        },
    )


def test_job_status_accepts_analysis_and_failure_text(registry) -> None:
    """Fields the API already returns: they must not make a response invalid."""
    validator = validator_for("job-status.schema.json", registry)
    validator.validate(
        {
            "jobId": JOB_ID,
            "status": "completed",
            "stage": "completed",
            "progress": 100,
            "userStage": "Completed",
            "mode": "full_stems",
            "outputFormat": "mp3",
            "source": {"type": "upload", "filename": "song.mp3"},
            "stems": [
                {"id": "drums", "label": "Drums", "durationSeconds": 200.0},
            ],
            "downloadUrl": f"/api/jobs/{JOB_ID}/downloads?kind=zip",
            "expiresAt": "2026-09-07T00:00:00Z",
            "analysis": {"bpm": 98.4, "key": "G# minor", "camelot": "1A"},
            "workerRunning": True,
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:03:00Z",
        }
    )
    validator.validate(
        {
            "jobId": JOB_ID,
            "status": "failed",
            "stage": "downloading",
            "progress": 12,
            "mode": "vocals_instrumental",
            "outputFormat": "mp3",
            "source": {"type": "spotify", "filename": None},
            "errorCode": "DOWNLOAD_FAILED",
            "errorMessage": "Spotify input is switched off on this machine.",
            "createdAt": "2026-09-06T00:00:00Z",
            "updatedAt": "2026-09-06T00:00:01Z",
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
    # "guitar" became a valid stem key with the 6-stem model; use a name
    # outside the stemKey enum to keep testing the rejection path.
    bad["output"]["stems"][0]["name"] = "cowbell"
    assert_invalid(validator, bad)
