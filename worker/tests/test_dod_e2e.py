"""Definition-of-Done end-to-end test (plan Section 23) — real engine.

Runs the complete local workflow with the real htdemucs checkpoint (no stubs):
a seeded upload -> worker claim -> validation -> Demucs separation -> encoding
-> packaging -> published outputs, then verifies every Section 23 criterion
that is machine-checkable. Skips cleanly when the model stack or ffmpeg is
missing; a short fixture keeps runtime to about a minute.

Human-only DoD items (listening quality, UI ergonomics) are explicitly out of
scope here and tracked in docs/BENCHMARKS.md.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import zipfile
from pathlib import Path

import pytest

from worker.database import JobQueue
from worker.input_audio import _require_ffmpeg_tool
from worker.models.demucs import _LOADED_MODELS

try:
    FFMPEG = _require_ffmpeg_tool("ffmpeg")
    import demucs  # noqa: F401
    import torch  # noqa: F401

    ENGINE_AVAILABLE = True
except Exception:  # noqa: BLE001
    ENGINE_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not ENGINE_AVAILABLE, reason="real separation engine (torch/demucs/ffmpeg) not available"
)


def _make_music_like_mp3(path: Path, seconds: float = 3.0) -> None:
    """A two-tone fixture with decay envelopes: closer to music than a sine."""
    subprocess.run(
        [
            FFMPEG,
            "-v", "error",
            "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
            "-filter_complex", "[0:a][1:a]amix=inputs=2,afade=t=out:st=%.1f:d=0.5" % (seconds - 0.5),
            "-ar", "44100", "-ac", "2", "-c:a", "libmp3lame", "-b:a", "128k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


@pytest.fixture(scope="session")
def shared_model_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One model cache for the whole session so the 80MB checkpoint downloads once.

    Reuses the app's on-disk checkpoint (data/models) when it is complete, so
    offline or throttled machines never touch the network for these tests;
    otherwise falls back to a session temp cache (the historical behavior).
    """
    repo_root = Path(__file__).resolve().parents[2]
    app_checkpoint = (
        repo_root / "data" / "models" / "hub" / "checkpoints" / "955717e8-8726e21a.th"
    )
    if app_checkpoint.is_file() and app_checkpoint.stat().st_size >= 84_141_911:
        return repo_root / "data" / "models"
    return tmp_path_factory.mktemp("dod_models")


@pytest.fixture()
def seeded_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    shared_model_dir: Path,
) -> tuple[JobQueue, str, Path]:
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    # Point TORCH_HOME at the session-shared cache: each test still gets a fresh
    # data dir, but the checkpoint is not re-downloaded per test.
    monkeypatch.setenv("TORCH_HOME", str(shared_model_dir))
    monkeypatch.delenv("STEMIFY_MODEL_DIR", raising=False)
    queue = JobQueue(data_dir=str(tmp_path / "data"))
    source = tmp_path / "song.mp3"
    _make_music_like_mp3(source)
    object_key = "sources/upl_dod/song.mp3"
    destination = queue.data_dir / object_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)
    connection = sqlite3.connect(queue.database_path)
    try:
        connection.execute(
            "INSERT INTO jobs (id, owner_key, source_type, source_object_key, source_filename, "
            "mode, output_format) VALUES (?, 'owner_dod', 'upload', ?, 'song.mp3', "
            "'vocals_instrumental', 'mp3')",
            ("job_" + "d" * 32, object_key),
        )
        connection.commit()
    finally:
        connection.close()
    return queue, "job_" + "d" * 32, tmp_path


def test_full_local_workflow_end_to_end(seeded_job) -> None:
    """Plan Section 23: the entire core workflow on one real job."""
    from worker.job_loop import process_job

    queue, job_id, _tmp = seeded_job
    job = queue.claim_next_queued_job()
    assert job is not None

    process_job(queue, job)

    row = queue._connection.execute(
        "SELECT status, stage, progress, error_code FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert row[0] == "completed", f"job must complete: {row}"
    assert row[1] == "completed"
    assert row[2] == 100
    assert row[3] is None

    # 1. Every generated stem has valid timing and decodes (validated at encode
    #    time by validate_encoded_output; here we re-verify the ZIP).
    outputs = queue._connection.execute(
        "SELECT stem_key, relative_path, size_bytes, sha256, duration_seconds "
        "FROM job_outputs WHERE job_id = ? ORDER BY stem_key",
        (job_id,),
    ).fetchall()
    keys = [o[0] for o in outputs]
    assert keys == ["archive", "instrumental", "vocals"]
    for _stem_key, relative_path, size_bytes, sha, _duration in outputs:
        published = queue.data_dir / relative_path
        assert published.is_file() and published.stat().st_size == size_bytes > 0
        assert _sha256(published) == sha  # recorded checksum matches the file

    # 2. The ZIP contains exactly the stems plus the manifest.
    zip_path = queue.data_dir / f"results/{job_id}/stems.zip"
    with zipfile.ZipFile(zip_path) as archive:
        assert sorted(archive.namelist()) == ["instrumental.mp3", "manifest.json", "vocals.mp3"]
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["separation"]["mode"] == "vocals_instrumental"
        stem_names = {stem["name"] for stem in manifest["output"]["stems"]}
        assert stem_names == {"vocals", "instrumental"}
        # Model version and processing settings are recorded (Section 23).
        assert manifest["separation"]["modelId"] == "htdemucs"
        assert manifest["output"]["format"] == "mp3"

    # 3. Results expire after the retention window (stamped at completion).
    expires_at = queue._connection.execute(
        "SELECT expires_at FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()[0]
    assert expires_at is not None

    # 4. A restart preserves the completed job and never re-claims it.
    queue.recover_stale_processing_jobs()
    assert queue._connection.execute(
        "SELECT status FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()[0] == "completed"
    assert queue.claim_next_queued_job() is None
    queue.close()


def test_cancellation_never_produces_a_completed_job(seeded_job) -> None:
    """Section 23: cancellation and restart do not create misleading completions."""
    from worker.job_loop import process_job

    queue, job_id, _tmp = seeded_job
    job = queue.claim_next_queued_job()
    assert job is not None

    # Request cancellation before the job is claimed work... it is already
    # processing, so the stage-boundary check must stop it before completion.
    queue._connection.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
    queue._connection.commit()

    process_job(queue, job)

    status, error_code = queue._connection.execute(
        "SELECT status, error_code FROM jobs WHERE id = ?", (job_id,)
    ).fetchone()
    assert status == "canceled"
    assert error_code == "CANCELED"
    assert not queue._connection.execute(
        "SELECT 1 FROM job_outputs WHERE job_id = ?", (job_id,)
    ).fetchone()
    queue.close()


def test_cached_model_is_reused_across_jobs(seeded_job) -> None:
    """Warm model reuse: a second job must not reload the checkpoint."""
    from worker.job_loop import process_job

    queue, _job_id, tmp = seeded_job
    job = queue.claim_next_queued_job()
    assert job is not None
    process_job(queue, job)
    assert len(_LOADED_MODELS) == 1

    # Second job on the same queue: the in-process cache must serve the model.
    source = tmp / "second.mp3"
    _make_music_like_mp3(source)
    object_key = "sources/upl_dod2/second.mp3"
    destination = queue.data_dir / object_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    connection = sqlite3.connect(queue.database_path)
    try:
        connection.execute(
            "INSERT INTO jobs (id, owner_key, source_type, source_object_key, source_filename, "
            "mode, output_format) VALUES (?, 'owner_dod', 'upload', ?, 'second.mp3', "
            "'vocals_instrumental', 'mp3')",
            ("job_" + "e" * 32, object_key),
        )
        connection.commit()
    finally:
        connection.close()

    before = dict(_LOADED_MODELS)
    job2 = queue.claim_next_queued_job()
    assert job2 is not None
    process_job(queue, job2)
    assert queue._connection.execute(
        "SELECT status FROM jobs WHERE id = ?", (job2.id,)
    ).fetchone()[0] == "completed"
    assert _LOADED_MODELS == before  # same model object, no reload
    queue.close()
