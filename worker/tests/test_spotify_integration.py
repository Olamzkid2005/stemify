"""Spotify plan S3 integration tests: Spotify jobs flow through the worker loop.

The supervised fetch child is stubbed with a real Ogg Vorbis fixture — the
container Spotify itself serves, so the validation ladder sees the same shape it
would in production. The download stage, validation, separation (stubbed
engine), packaging, and every database transition are real. No network and no
Spotify account: skips cleanly when ffmpeg is unavailable.

Mirrors `test_youtube_integration.py`, which is the same seam for the other link
source.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_pipeline_integration import install_stub_engine, read_row

from worker.database import JobQueue
from worker.errors import ErrorCode
from worker.input_audio import _require_ffmpeg_tool
from worker.spotify import TrackMetadata

try:
    FFMPEG = _require_ffmpeg_tool("ffmpeg")
except Exception:  # noqa: BLE001
    FFMPEG = None

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")

TRACK_ID = "4cOdK2wGLETKBW3PvgPWqT"
TRACK_URI = f"spotify:track:{TRACK_ID}"
ALBUM_URL = "https://open.spotify.com/album/4cOdK2wGLETKBW3PvgPWqT"
JOB_ID = "job_" + "f" * 32


def make_tone_ogg(path: Path, seconds: float = 1.0) -> None:
    """A real Ogg Vorbis fixture — the format the fetch child actually writes."""
    cmd = [
        FFMPEG,  # type: ignore[list-item]
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={seconds}",
        "-ar",
        "44100",
        "-ac",
        "2",
        "-c:a",
        "libvorbis",
        "-q:a",
        "8",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def seed_spotify_job(tmp_path: Path, url: str | None = TRACK_URI) -> tuple[JobQueue, str]:
    queue = JobQueue(data_dir=str(tmp_path / "data"))
    connection = sqlite3.connect(queue.database_path)
    try:
        connection.execute(
            "INSERT INTO jobs (id, owner_key, source_type, source_url, "
            "mode, output_format) VALUES (?, 'owner_test', 'spotify', ?, "
            "'vocals_instrumental', 'mp3')",
            (JOB_ID, url),
        )
        connection.commit()
    finally:
        connection.close()
    return queue, JOB_ID


def stub_fetch(
    monkeypatch: pytest.MonkeyPatch,
    media: Path,
    title: str | None = "Test Artist - Test Song",
    fetched_metadata: TrackMetadata | None = None,
) -> None:
    """Replace the supervised fetch and the metadata probe.

    `worker.spotify.download_audio` is the only seam that reaches the network
    (it supervises the fetch child), so patching it leaves the validation
    ladder, ffmpeg, and the separation path real. `resolve_title` is patched too
    so the naming assertion cannot depend on network metadata.

    `fetched_metadata` is what the real child reports from its own session; the
    stub delivers it through the same callback, so the naming fallback is
    exercised rather than assumed.
    """
    reported = fetched_metadata or TrackMetadata(
        track_id=TRACK_ID,
        title="Tease Me",
        artist="Zaylevelten",
        album="Tease Me",
        duration_ms=240_000,
    )

    def fake_download(
        url: str,
        dest_dir: Path,
        progress_callback: Any = None,
        on_track_metadata: Any = None,
        artwork_path: Any = None,
    ) -> Path:
        if on_track_metadata is not None:
            on_track_metadata(reported)
        if progress_callback is not None:
            progress_callback(50.0)
        if artwork_path is not None:
            # What the real child writes: a verified JPEG in the job's results
            # directory, so the web app can serve it without any network call.
            cover = Path(artwork_path)
            cover.parent.mkdir(parents=True, exist_ok=True)
            cover.write_bytes(b"\xff\xd8\xff\xe0" + b"cover")

        dest = Path(dest_dir) / f"{TRACK_ID}.ogg"
        dest.write_bytes(media.read_bytes())
        return dest

    monkeypatch.setattr("worker.spotify.download_audio", fake_download)
    monkeypatch.setattr("worker.spotify.resolve_title", lambda url: title)


def offline_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    """Guarantee the best-effort metadata probe stays offline in these tests."""
    monkeypatch.delenv("STEMIFY_SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("STEMIFY_SPOTIFY_CLIENT_SECRET", raising=False)


def fail_if_fetched(monkeypatch: pytest.MonkeyPatch, reason: str) -> None:
    def explode(*args: Any, **kwargs: Any) -> None:
        raise AssertionError(reason)

    monkeypatch.setattr("worker.spotify.run_grouped", explode)


def test_spotify_job_completes_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from worker.job_loop import process_job

    install_stub_engine(monkeypatch)
    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "1")
    queue, job_id = seed_spotify_job(tmp_path)

    media = tmp_path / f"{TRACK_ID}.ogg"
    make_tone_ogg(media)
    stub_fetch(monkeypatch, media)

    job = queue.claim_next_queued_job()
    assert job is not None
    assert job.source_type == "spotify"
    assert job.source_url == TRACK_URI

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

    # Naming (roadmap A2): the metadata probe names the job after the song.
    name, album = read_row(
        queue.database_path,
        "SELECT source_filename, source_album FROM jobs WHERE id = ?",
        (job_id,),
    )[0]
    assert name == "Test Artist - Test Song.ogg"
    # Richer metadata: the album rides on the job and the cover sits beside the
    # stems, where retention deletes it with them.
    assert album == "Tease Me"
    assert (queue.data_dir / "results" / job_id / "artwork.jpg").read_bytes().startswith(
        b"\xff\xd8\xff"
    )
    queue.close()


def test_the_fetch_names_the_job_when_the_probe_finds_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Premium-login-only machine: no Web API app, still a named job.

    `resolve_title` needs an operator-configured app id/secret and is None on
    this machine, which used to leave every Spotify job named after nothing at
    all. The fetch child reads the same fields from its own session.
    """
    from worker.job_loop import process_job

    install_stub_engine(monkeypatch)
    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "1")
    queue, job_id = seed_spotify_job(tmp_path)

    media = tmp_path / f"{TRACK_ID}.ogg"
    make_tone_ogg(media)
    stub_fetch(monkeypatch, media, title=None)

    job = queue.claim_next_queued_job()
    assert job is not None
    process_job(queue, job)

    name = read_row(
        queue.database_path, "SELECT source_filename FROM jobs WHERE id = ?", (job_id,)
    )[0][0]
    assert name == "Zaylevelten - Tease Me.ogg"
    queue.close()


def test_spotify_job_without_url_fails_safely(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from worker.job_loop import process_job

    queue, job_id = seed_spotify_job(tmp_path, url=None)
    job = queue.claim_next_queued_job()
    assert job is not None

    process_job(queue, job)

    row = read_row(
        queue.database_path, "SELECT status, error_code FROM jobs WHERE id = ?", (job_id,)
    )[0]
    assert row[0] == "failed"
    assert row[1] == ErrorCode.DOWNLOAD_FAILED.value
    queue.close()


def test_spotify_disabled_reports_the_operator_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The kill switch is default-off, and the public text has to say so.

    The generic DOWNLOAD_FAILED wording would leave the user with no idea the
    feature is simply not switched on here.
    """
    from worker.job_loop import process_job

    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "0")
    offline_metadata(monkeypatch)
    queue, job_id = seed_spotify_job(tmp_path)
    job = queue.claim_next_queued_job()
    assert job is not None
    fail_if_fetched(monkeypatch, "no fetch child may start while Spotify input is off")

    process_job(queue, job)

    row = read_row(
        queue.database_path,
        "SELECT status, error_code, error_message_public FROM jobs WHERE id = ?",
        (job_id,),
    )[0]
    assert row[0] == "failed"
    assert row[1] == ErrorCode.DOWNLOAD_FAILED.value
    assert "switched off" in row[2]
    queue.close()


def test_spotify_without_credentials_names_the_setup_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Enabled but not signed in: the message must point at the setup step."""
    from worker.job_loop import process_job

    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "1")
    monkeypatch.setenv("STEMIFY_SPOTIFY_CREDENTIALS_FILE", str(tmp_path / "absent.json"))
    offline_metadata(monkeypatch)
    queue, job_id = seed_spotify_job(tmp_path)
    job = queue.claim_next_queued_job()
    assert job is not None
    fail_if_fetched(monkeypatch, "no fetch child may start without credentials")

    process_job(queue, job)

    row = read_row(
        queue.database_path,
        "SELECT status, error_code, error_message_public FROM jobs WHERE id = ?",
        (job_id,),
    )[0]
    assert row[0] == "failed"
    assert row[1] == ErrorCode.DOWNLOAD_FAILED.value
    assert "not set up" in row[2]
    queue.close()


def test_spotify_disallowed_link_never_starts_a_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker re-validates the link (plan Section 8), so an album link fails
    before anything is spawned — even with credentials in place."""
    from worker.job_loop import process_job

    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "1")
    credentials = tmp_path / "creds.json"
    credentials.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("STEMIFY_SPOTIFY_CREDENTIALS_FILE", str(credentials))
    offline_metadata(monkeypatch)
    queue, job_id = seed_spotify_job(tmp_path, url=ALBUM_URL)
    job = queue.claim_next_queued_job()
    assert job is not None
    fail_if_fetched(monkeypatch, "a disallowed link must fail before the fetch")

    process_job(queue, job)

    row = read_row(
        queue.database_path, "SELECT status, error_code FROM jobs WHERE id = ?", (job_id,)
    )[0]
    assert row[0] == "failed"
    assert row[1] == ErrorCode.DOWNLOAD_FAILED.value
    queue.close()
