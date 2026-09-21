"""Pool benchmark tests (concurrency plan C5).

The measurement itself spawns real worker processes and is run by hand; these
cover the parts that can be wrong quietly — the thread split, the fixture, the
seeding, and the report math.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from worker.pool_benchmark import (
    build_parser,
    count_outputs_on_disk,
    resolve_threads,
    seed_jobs,
    summarise,
    synthesise_fixture,
)


def _job(status: str, started: int | None, completed: int | None, worker: str | None) -> dict:
    return {
        "status": status,
        "started_at": started,
        "completed_at": completed,
        "error_code": None,
        "worker_call_id": worker,
        "output_rows": 3,
    }


def test_thread_split_matches_the_launcher_arithmetic() -> None:
    assert resolve_threads(4, 1) == 4
    assert resolve_threads(4, 2) == 2
    assert resolve_threads(8, 4) == 2
    # Never zero, however the pool was configured.
    assert resolve_threads(2, 8) == 1
    assert resolve_threads(1, 4) == 1


def test_parser_defaults_are_the_documented_ones() -> None:
    args = build_parser().parse_args([])
    assert (args.pool, args.jobs, args.duration) == (2, 2, 20.0)
    assert args.mode == "vocals_instrumental"
    assert args.output_format == "mp3"
    assert args.threads is None  # cores/pool unless asked


def test_fixture_is_a_deterministic_stereo_wav(tmp_path: Path) -> None:
    import soundfile as sf

    first = synthesise_fixture(tmp_path / "a.wav", 1.0)
    second = synthesise_fixture(tmp_path / "b.wav", 1.0)
    info = sf.info(str(first))
    assert (info.frames, info.samplerate, info.channels) == (44_100, 44_100, 2)
    # Seeded, so two runs of the benchmark separate identical audio.
    assert first.read_bytes() == second.read_bytes()


def test_fixture_length_follows_the_duration(tmp_path: Path) -> None:
    import soundfile as sf

    half = sf.info(str(synthesise_fixture(tmp_path / "half.wav", 0.5)))
    assert half.frames == 22_050


def test_seeding_creates_a_claimable_job_per_fixture(tmp_path: Path) -> None:
    clip = synthesise_fixture(tmp_path / "fixture.wav", 0.2)
    job_ids = seed_jobs(tmp_path, 3, clip, "vocals_instrumental", "mp3")

    assert len(job_ids) == len(set(job_ids)) == 3
    connection = sqlite3.connect(tmp_path / "stemify.sqlite3")
    try:
        rows = connection.execute("SELECT id, status, mode, output_format FROM jobs").fetchall()
        uploads = connection.execute("SELECT COUNT(*) FROM uploads").fetchone()[0]
    finally:
        connection.close()
    assert len(rows) == 3
    assert all(row[1] == "queued" and row[2] == "vocals_instrumental" and row[3] == "mp3" for row in rows)
    assert uploads == 3
    # Each job's source exists, so a worker can actually claim and process it.
    for job_id in job_ids:
        assert (tmp_path / "sources" / job_id / "fixture.wav").is_file()


def test_summarise_counts_observed_concurrency_not_the_pool_size() -> None:
    jobs = {
        "a": _job("completed", 1_000, 11_000, "worker_1"),
        "b": _job("completed", 5_000, 19_000, "worker_2"),
    }
    report = summarise(jobs, wall_seconds=20.0, audio_seconds=40.0, outputs_on_disk=2)

    assert report["completed"] == 2
    assert report["distinct_workers"] == 2
    # b started before a finished: real overlap, visible in the database.
    assert report["concurrent_starts_observed"] == 1
    assert report["per_job_seconds"] == [10.0, 14.0]
    assert report["median_job_seconds"] == 14.0
    assert report["realtime_factor"] == 0.5
    assert report["outputs_on_disk"] == 2


def test_summarise_reports_no_overlap_for_sequential_jobs() -> None:
    jobs = {
        "a": _job("completed", 1_000, 11_000, "worker_1"),
        "b": _job("completed", 11_500, 21_000, "worker_1"),
    }
    report = summarise(jobs, 20.0, 40.0, 2)
    assert report["concurrent_starts_observed"] == 0
    assert report["distinct_workers"] == 1


def test_summarise_survives_a_failed_and_an_unstarted_job() -> None:
    jobs = {
        "a": _job("completed", 1_000, 11_000, "worker_1"),
        "b": _job("failed", 2_000, 4_000, "worker_2"),
        "c": _job("queued", None, None, None),
    }
    report = summarise(jobs, 12.0, 60.0, 1)
    assert (report["completed"], report["failed"], report["jobs"]) == (1, 1, 3)
    # The unstarted job contributes no window, so it cannot invent concurrency.
    assert report["concurrent_starts_observed"] == 1
    assert report["per_job_seconds"] == [2.0, 10.0]


def test_summarise_handles_an_empty_run() -> None:
    report = summarise({}, 5.0, 0.0, 0)
    assert report["jobs"] == 0
    assert report["per_job_seconds"] == []
    assert report["median_job_seconds"] is None
    assert report["realtime_factor"] is None


def test_outputs_on_disk_counts_result_directories(tmp_path: Path) -> None:
    (tmp_path / "results" / "job_a").mkdir(parents=True)
    assert count_outputs_on_disk(tmp_path, ["job_a", "job_b"]) == 1


def test_report_is_json_serialisable(tmp_path: Path) -> None:
    """The tool's whole output is a JSON document, so it must round-trip."""
    clip = synthesise_fixture(tmp_path / "fixture.wav", 0.1)
    job_ids = seed_jobs(tmp_path, 1, clip, "vocals_instrumental", "mp3")
    jobs = {"jobs": {job_ids[0]: _job("completed", 1_000, 9_000, "worker_1")}}
    report = summarise(jobs["jobs"], 10.0, 20.0, 1)
    assert json.loads(json.dumps(report)) == report


@pytest.mark.parametrize("pool", [1, 2, 4])
def test_pool_sizes_keep_a_positive_thread_budget(pool: int) -> None:
    assert resolve_threads(4, pool) >= 1
