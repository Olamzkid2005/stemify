"""Task 11 integration tests: the full job pipeline over real SQLite.

The separation engine is stubbed (no torch/demucs/model download); encoding,
packaging, and all database state transitions are real. Skips cleanly when
ffmpeg is unavailable (encoding needs it).
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

import worker.models.demucs as demucs_module
from worker.database import JobQueue
from worker.errors import ErrorCode
from worker.input_audio import _require_ffmpeg_tool

try:
    FFMPEG = _require_ffmpeg_tool("ffmpeg")
except Exception:  # noqa: BLE001
    FFMPEG = None

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")


def make_tone_mp3(path: Path, seconds: float = 1.0) -> None:
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
        "libmp3lame",
        "-b:a",
        "128k",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def install_stub_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace separation with a deterministic split; everything else is real."""
    import numpy as np

    class _FakeTorch:
        float32 = np.float32

        class cuda:
            @staticmethod
            def is_available() -> bool:
                return False

            @staticmethod
            def empty_cache() -> None:
                return None

        class no_grad:
            def __enter__(self) -> "object":
                return self

            def __exit__(self, *args: object) -> None:
                return None

        @staticmethod
        def from_numpy(array: Any) -> Any:
            return array

    class _FakeModel:
        sources = ["drums", "bass", "other", "vocals"]

        def to(self, device: object) -> "_FakeModel":
            return self

        def eval(self) -> "_FakeModel":
            return self

    def fake_apply_model(model: Any, tensor: Any, **kwargs: Any) -> Any:
        # tensor is a numpy array here (FakeTorch.from_numpy is identity);
        # shape (1, channels, samples). Return zeros per source: the stub's
        # "vocals" is silence, so instrumental == mixture (mixture-consistent).
        array = np.asarray(tensor)
        sources = len(model.sources)
        return np.zeros((1, sources, array.shape[1], array.shape[2]), dtype=np.float32)

    fake_apply_module = types.ModuleType("demucs.apply")
    fake_apply_module.apply_model = fake_apply_model  # type: ignore[attr-defined]
    fake_demucs_module = types.ModuleType("demucs")
    fake_demucs_module.apply = fake_apply_module  # type: ignore[attr-defined]
    fake_pretrained_module = types.ModuleType("demucs.pretrained")
    fake_pretrained_module.get_model = lambda name: _FakeModel()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "demucs", fake_demucs_module)
    monkeypatch.setitem(sys.modules, "demucs.apply", fake_apply_module)
    monkeypatch.setitem(sys.modules, "demucs.pretrained", fake_pretrained_module)

    monkeypatch.setattr(demucs_module, "_import_torch", lambda: _FakeTorch)
    monkeypatch.setattr(
        demucs_module,
        "_import_demucs_pretrained",
        lambda: types.SimpleNamespace(get_model=lambda name: _FakeModel()),
    )

    # Real hashlib verifies a real file; write a checkpoint and patch nothing.
    # Instead, patch the checksum verifier to accept the fixture file.
    monkeypatch.setattr(demucs_module, "_verify_checkpoint_checksum", lambda profile, path: None)


def seed_job_with_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[JobQueue, str]:
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path / "data"))
    queue = JobQueue(data_dir=str(tmp_path / "data"))
    source = tmp_path / "song.mp3"
    make_tone_mp3(source)
    object_key = "sources/upl_test/song.mp3"
    destination = queue.data_dir / object_key
    destination.parent.mkdir(parents=True, exist_ok=True)
    source.replace(destination)
    connection = sqlite3.connect(queue.database_path)
    try:
        connection.execute(
            "INSERT INTO jobs (id, owner_key, source_type, source_object_key, source_filename, "
            "mode, output_format) VALUES (?, 'owner_test', 'upload', ?, 'song.mp3', "
            "'vocals_instrumental', 'mp3')",
            ("job_" + "a" * 32, object_key),
        )
        connection.commit()
    finally:
        connection.close()
    return queue, "job_" + "a" * 32


def read_row(db_path: Path, sql: str, params: tuple = ()) -> Any:
    connection = sqlite3.connect(db_path)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


def test_full_pipeline_completes_a_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from worker.job_loop import process_job

    install_stub_engine(monkeypatch)
    queue, job_id = seed_job_with_source(tmp_path, monkeypatch)
    job = queue.claim_next_queued_job()
    assert job is not None

    process_job(queue, job)

    rows = read_row(queue.database_path, "SELECT status, stage, progress, error_code FROM jobs WHERE id = ?", (job_id,))
    assert rows[0][0] == "completed"
    assert rows[0][1] == "completed"
    assert rows[0][2] == 100
    assert rows[0][3] is None

    outputs = read_row(
        queue.database_path,
        "SELECT stem_key, label, relative_path, mime_type FROM job_outputs WHERE job_id = ? ORDER BY stem_key",
        (job_id,),
    )
    keys = [row[0] for row in outputs]
    assert keys == ["archive", "instrumental", "vocals"]
    assert all(row[2].startswith(f"results/{job_id}/") for row in outputs)

    # The published files exist and are non-empty.
    for row in outputs:
        published = queue.data_dir / row[2]
        assert published.is_file() and published.stat().st_size > 0, row[2]

    # The ZIP contains exactly the stems plus the manifest.
    import zipfile

    zip_path = queue.data_dir / f"results/{job_id}/stems.zip"
    with zipfile.ZipFile(zip_path) as archive:
        assert sorted(archive.namelist()) == ["instrumental.mp3", "manifest.json", "vocals.mp3"]
    queue.close()


def test_cancelled_job_finalizes_as_canceled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Plan Section 9.5: the final state is canceled, never failed/completed."""
    from worker.job_loop import process_job

    install_stub_engine(monkeypatch)
    queue, job_id = seed_job_with_source(tmp_path, monkeypatch)
    job = queue.claim_next_queued_job()
    assert job is not None
    queue._connection.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
    queue._connection.commit()

    process_job(queue, job)

    row = read_row(queue.database_path, "SELECT status, error_code FROM jobs WHERE id = ?", (job_id,))[0]
    assert row[0] == "canceled"
    assert row[1] == ErrorCode.CANCELED.value
    assert not read_row(queue.database_path, "SELECT 1 FROM job_outputs WHERE job_id = ?", (job_id,))
    queue.close()


def test_missing_source_fails_without_leaving_processing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from worker.job_loop import process_job

    queue = JobQueue(data_dir=str(tmp_path / "data"))
    connection = sqlite3.connect(queue.database_path)
    try:
        connection.execute(
            "INSERT INTO jobs (id, owner_key, source_type, source_object_key, mode, output_format) "
            "VALUES (?, 'owner_test', 'upload', 'sources/upl_missing/song.mp3', "
            "'vocals_instrumental', 'mp3')",
            ("job_" + "b" * 32,),
        )
        connection.commit()
    finally:
        connection.close()
    job = queue.claim_next_queued_job()
    assert job is not None

    process_job(queue, job)

    row = read_row(queue.database_path, "SELECT status, error_code FROM jobs WHERE id = ?", (job.id,))[0]
    assert row[0] == "failed"
    assert row[1] in {ErrorCode.INVALID_AUDIO.value, ErrorCode.UNKNOWN.value}
    queue.close()
