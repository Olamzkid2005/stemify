"""Task 14 integration tests: YouTube jobs flow through the worker loop.

The yt-dlp subprocess is stubbed with a real local MP3 (no network); the
download stage, validation, separation (stubbed engine), packaging, and all
database transitions are real. Skips cleanly when ffmpeg is unavailable.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from test_pipeline_integration import install_stub_engine, make_tone_mp3, read_row

from worker.database import JobQueue
from worker.errors import ErrorCode
from worker.input_audio import _require_ffmpeg_tool

try:
    FFMPEG = _require_ffmpeg_tool("ffmpeg")
except Exception:  # noqa: BLE001
    FFMPEG = None

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")


def seed_youtube_job(
    tmp_path: Path, url: str | None = "https://www.youtube.com/watch?v=abc123"
) -> tuple[JobQueue, str]:
    queue = JobQueue(data_dir=str(tmp_path / "data"))
    connection = sqlite3.connect(queue.database_path)
    try:
        connection.execute(
            "INSERT INTO jobs (id, owner_key, source_type, source_url, "
            "mode, output_format) VALUES (?, 'owner_test', 'youtube', ?, "
            "'vocals_instrumental', 'mp3')",
            ("job_" + "c" * 32, url),
        )
        connection.commit()
    finally:
        connection.close()
    return queue, "job_" + "c" * 32


def stub_yt_dlp(monkeypatch: pytest.MonkeyPatch, media: Path) -> None:
    """Replace the yt-dlp subprocess with a copy of a real media file.

    Only yt-dlp-style invocations (identified by the fixed --no-playlist flag)
    are intercepted; ffprobe/ffmpeg calls stay real.
    """
    import shutil as shutil_module
    import subprocess as subprocess_module

    real_run = subprocess_module.run
    real_which = shutil_module.which

    def fake_run(args: Any, **kwargs: Any) -> Any:
        class _Completed:
            returncode = 0
            stderr = b""
            stdout = b""

        if isinstance(args, (list, tuple)) and "--no-playlist" in args:
            url_index = args.index("--") + 1
            _ = args[url_index]  # allowlist guarantees this is the validated URL
            # Mirror the fixed template: %(id)s.%(ext)s inside the --paths dir.
            dest_dir = Path(args[args.index("--paths") + 1])
            (dest_dir / "abc123.mp3").write_bytes(media.read_bytes())
            return _Completed()
        return real_run(args, **kwargs)

    def selective_which(name: str, *args: Any, **kwargs: Any) -> Any:
        # Fake only yt-dlp; ffprobe/ffmpeg must resolve for real because
        # shutil is a shared module object.
        return "yt-dlp" if name == "yt-dlp" else real_which(name, *args, **kwargs)

    monkeypatch.setattr("worker.youtube.shutil.which", selective_which)
    monkeypatch.setattr("worker.youtube.subprocess.run", fake_run)


def test_youtube_job_completes_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from worker.job_loop import process_job

    install_stub_engine(monkeypatch)
    queue, job_id = seed_youtube_job(tmp_path)

    source = tmp_path / "fixture.mp3"
    make_tone_mp3(source)
    stub_yt_dlp(monkeypatch, source)

    job = queue.claim_next_queued_job()
    assert job is not None
    assert job.source_type == "youtube"
    assert job.source_url == "https://www.youtube.com/watch?v=abc123"

    process_job(queue, job)

    row = read_row(
        queue.database_path, "SELECT status, stage, progress FROM jobs WHERE id = ?", (job_id,)
    )[0]
    assert row[0] == "completed"
    assert row[1] == "completed"
    assert row[2] == 100

    outputs = read_row(
        queue.database_path, "SELECT stem_key FROM job_outputs WHERE job_id = ?", (job_id,)
    )
    assert sorted(r[0] for r in outputs) == ["archive", "instrumental", "vocals"]
    queue.close()


def test_youtube_job_without_url_fails_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from worker.job_loop import process_job

    queue, job_id = seed_youtube_job(tmp_path, url=None)
    job = queue.claim_next_queued_job()
    assert job is not None

    process_job(queue, job)

    row = read_row(queue.database_path, "SELECT status, error_code FROM jobs WHERE id = ?", (job_id,))[0]
    assert row[0] == "failed"
    assert row[1] == ErrorCode.DOWNLOAD_FAILED.value
    queue.close()


def test_youtube_job_with_disallowed_url_fails_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from worker.job_loop import process_job

    queue, job_id = seed_youtube_job(tmp_path, url="https://evil.example.com/watch?v=x")
    job = queue.claim_next_queued_job()
    assert job is not None

    def explode(args: Any, **kwargs: Any) -> None:
        if isinstance(args, (list, tuple)) and "--no-playlist" in args:
            raise AssertionError("subprocess must not run for disallowed URLs")
        raise AssertionError("no subprocess at all should run here")

    monkeypatch.setattr("worker.youtube.subprocess.run", explode)

    process_job(queue, job)

    row = read_row(queue.database_path, "SELECT status, error_code FROM jobs WHERE id = ?", (job_id,))[0]
    assert row[0] == "failed"
    assert row[1] == ErrorCode.DOWNLOAD_FAILED.value
    queue.close()


def test_youtube_job_disabled_flag_fails_safely(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from worker.job_loop import process_job

    monkeypatch.setenv("STEMIFY_YOUTUBE_ENABLED", "0")
    queue, job_id = seed_youtube_job(tmp_path)
    job = queue.claim_next_queued_job()
    assert job is not None

    process_job(queue, job)

    row = read_row(queue.database_path, "SELECT status, error_code FROM jobs WHERE id = ?", (job_id,))[0]
    assert row[0] == "failed"
    assert row[1] == ErrorCode.DOWNLOAD_FAILED.value
    queue.close()
