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


# --- job-list ------------------------------------------------------------


def _list_item(**patch) -> dict:
    item = {
        "jobId": JOB_ID,
        "status": "processing",
        "userStage": "Separating stems",
        "progressMessage": "Running the separation model",
        "progress": 30,
        "mode": "vocals_instrumental",
        "outputFormat": "mp3",
        "source": {"type": "upload", "filename": "song.mp3"},
        "createdAt": "2026-09-06T00:00:00Z",
        "updatedAt": "2026-09-06T00:01:12Z",
        "statusUrl": f"/api/jobs/{JOB_ID}",
    }
    item.update(patch)
    return item


def _pool(**patch) -> dict:
    """The live pool summary that rides on every list response."""
    pool = {
        "configured": 2,
        "running": 2,
        "threadsPerWorker": 2,
        "ramPerWorkerMb": 1024,
        "ramTotalMb": 2048,
        "freeRamMb": 8192,
    }
    pool.update(patch)
    return pool


def _list_body(jobs=None, pool=None) -> dict:
    return {"jobs": [] if jobs is None else jobs, "pool": _pool() if pool is None else pool}


def test_job_list_accepts_an_empty_list(registry) -> None:
    """A browser that has never submitted anything is a normal case."""
    validator = validator_for("job-list.schema.json", registry)
    validator.validate(_list_body())


def test_job_list_accepts_a_waiting_job_with_its_position(registry) -> None:
    validator = validator_for("job-list.schema.json", registry)
    validator.validate(
        _list_body(
            [
                _list_item(
                    status="queued",
                    progress=0,
                    userStage="Preparing audio",
                    progressMessage="Waiting for a worker",
                    queuePosition=3,
                )
            ]
        )
    )


def test_job_list_accepts_active_and_finished_together(registry) -> None:
    """The real shape: a running job, a waiting one and a finished one."""
    validator = validator_for("job-list.schema.json", registry)
    validator.validate(
        _list_body(
            [
                _list_item(),
                _list_item(status="queued", progress=0, queuePosition=1),
                _list_item(
                    status="completed",
                    progress=100,
                    userStage="Completed",
                    source={
                        "type": "spotify",
                        "filename": "Olamide - Owotabua.ogg",
                        "artworkUrl": f"/api/jobs/{JOB_ID}/artwork",
                    },
                ),
            ]
        )
    )


def test_job_list_rejects_a_queued_job_without_a_position(registry) -> None:
    """"Queued — N ahead" is the point of the list; without a position it is guesswork."""
    validator = validator_for("job-list.schema.json", registry)
    assert_invalid(validator, _list_body([_list_item(status="queued", progress=0)]))


def test_job_list_rejects_a_position_on_a_running_job(registry) -> None:
    validator = validator_for("job-list.schema.json", registry)
    assert_invalid(validator, _list_body([_list_item(queuePosition=2)]))


def test_job_list_rejects_a_remote_artwork_url(registry) -> None:
    validator = validator_for("job-list.schema.json", registry)
    assert_invalid(
        validator,
        _list_body(
            [
                _list_item(
                    source={
                        "type": "spotify",
                        "filename": "Artist - Song.ogg",
                        "artworkUrl": "https://i.scdn.co/image/deadbeef",
                    }
                )
            ]
        ),
    )


@pytest.mark.parametrize(
    "patch",
    [
        {"status": "finished"},
        {"progress": 101},
        {"queuePosition": 0},
        {"userStage": ""},
        {"statusUrl": "https://evil.example.com/api/jobs/job_a1b2c3d4e5f60718"},
        {"source": {"type": "upload", "filename": "song.mp3", "ownerKey": "leak"}},
        {"unknownField": True},
    ],
)
def test_job_list_rejects_invalid_items(registry, patch) -> None:
    validator = validator_for("job-list.schema.json", registry)
    assert_invalid(validator, _list_body([_list_item(**patch)]))


def test_job_list_rejects_more_than_the_cap(registry) -> None:
    validator = validator_for("job-list.schema.json", registry)
    assert_invalid(validator, _list_body([_list_item() for _ in range(11)]))


def test_job_list_accepts_a_pool_whose_free_memory_could_not_be_read(registry) -> None:
    """A platform that will not report memory says so, instead of reporting zero."""
    validator = validator_for("job-list.schema.json", registry)
    validator.validate(_list_body(pool=_pool(freeRamMb=None)))


def test_job_list_rejects_a_pool_without_free_memory(registry) -> None:
    """Required and nullable: omitting the field is not the same as "unknown"."""
    validator = validator_for("job-list.schema.json", registry)
    pool = _pool()
    del pool["freeRamMb"]
    assert_invalid(validator, _list_body(pool=pool))


def test_job_list_rejects_a_missing_pool(registry) -> None:
    """The home page reads the pool off this response; a list without it is broken."""
    validator = validator_for("job-list.schema.json", registry)
    assert_invalid(validator, {"jobs": []})


def test_job_list_accepts_a_pool_without_a_thread_budget(registry) -> None:
    """A worker loop run by hand has no thread budget: omit it, never guess it."""
    validator = validator_for("job-list.schema.json", registry)
    validator.validate(_list_body(pool=_pool(threadsPerWorker=None)))


def test_job_list_accepts_a_pool_with_no_worker_running(registry) -> None:
    """Every worker crashed: `running` is 0 and the pool is still configured."""
    validator = validator_for("job-list.schema.json", registry)
    validator.validate(_list_body(pool=_pool(running=0)))


@pytest.mark.parametrize(
    "patch",
    [
        {"configured": 0},
        {"running": -1},
        {"threadsPerWorker": 0},
        {"configured": "2"},
        {"ramPerWorkerMb": 0},
        {"ramTotalMb": -1},
        # Negative free memory is nonsense, and "enough RAM" is not the schema's
        # call to make: the consumer compares it against ramTotalMb.
        {"freeRamMb": -1},
        {"freeRamMb": "8192"},
        # Worker internals stay on the server: the strip needs counts, not pids.
        {"pid": 4242},
    ],
)
def test_job_list_rejects_nonsense_pool_numbers(registry, patch) -> None:
    validator = validator_for("job-list.schema.json", registry)
    assert_invalid(validator, _list_body(pool=_pool(**patch)))


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
