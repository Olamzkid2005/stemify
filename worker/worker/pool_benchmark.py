"""Worker pool benchmark (concurrency plan C5).

Answers one question with numbers instead of arithmetic: **does running two
worker processes at once actually finish more work?** `worker.benchmark`
measures a single process; this measures the pool, the way `start.sh` runs it —
N worker processes, one job each, one shared SQLite queue, a thread budget split
across them.

    PYTHONPATH="$PWD/.runtime" python -m worker.pool_benchmark --pool 2 --jobs 4 --duration 20

Everything is offline and deterministic: fixtures are synthesized tones written
to a throwaway data directory, the model checkpoint is copied from the project's
model directory, and the jobs are seeded exactly as the web app seeds them
(uploads row + queued jobs row). The report is a JSON document on stdout, so two
runs can be diffed; use `--out` to keep it.

The number to compare is *total wall time for the whole set*, plus the honest
per-job time: a pool of 2 on a 4-thread laptop makes each job slower and both
finish sooner than one-after-the-other, which is the trade the default pool size
is chosen on (docs/BENCHMARKS.md).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from worker.database import JobQueue

DEFAULT_JOBS = 2
DEFAULT_DURATION = 20.0
DEFAULT_TIMEOUT_SECONDS = 3600
TERMINAL = {"completed", "failed", "canceled", "expired"}
SAMPLE_RATE = 44_100


def resolve_threads(cores: int, pool: int) -> int:
    """Threads per worker: the launcher's split, floored at one."""
    return max(1, cores // max(1, pool))


def synthesise_fixture(path: Path, duration_seconds: float, sample_rate: int = SAMPLE_RATE) -> Path:
    """A deterministic stereo tone with a little noise, written as a real WAV.

    Seeded, so two runs of the same command separate identical audio; a pure
    tone would be easier to separate than real music and flatter the numbers.
    """
    import numpy as np
    import soundfile as sf

    rng = np.random.default_rng(0)
    frames = int(sample_rate * duration_seconds)
    t = np.arange(frames) / sample_rate
    # Different pitches per channel so the model cannot collapse the stereo image.
    left = 0.30 * np.sin(2 * np.pi * 220.0 * t) + 0.05 * rng.standard_normal(frames)
    right = 0.30 * np.sin(2 * np.pi * 330.0 * t) + 0.05 * rng.standard_normal(frames)
    stereo = np.stack([left, right], axis=1).astype("float32")
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), stereo, sample_rate)
    return path


def seed_jobs(data_dir: Path, count: int, clip: Path, mode: str, output_format: str) -> list[str]:
    """Queue `count` uploads the way the web app does, and return their ids."""
    JobQueue(data_dir=str(data_dir)).close()  # create the schema first
    connection = sqlite3.connect(data_dir / "stemify.sqlite3")
    job_ids: list[str] = []
    try:
        for index in range(count):
            job_id = f"job_{index:032x}"
            object_key = f"sources/{job_id}/fixture.wav"
            destination = data_dir / object_key
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(clip, destination)
            connection.execute(
                "INSERT INTO uploads (id, owner_key, filename, object_key, size_bytes, expires_at)"
                " VALUES (?, 'owner_pool_bench', 'fixture.wav', ?, ?, NULL)",
                (f"upl_{job_id[4:36]}", object_key, destination.stat().st_size),
            )
            connection.execute(
                "INSERT INTO jobs (id, owner_key, source_type, source_object_key, source_filename,"
                " mode, output_format) VALUES (?, 'owner_pool_bench', 'upload', ?,"
                " 'fixture.wav', ?, ?)",
                (job_id, object_key, mode, output_format),
            )
            job_ids.append(job_id)
        connection.commit()
    finally:
        connection.close()
    return job_ids


def _read_jobs(database: Path, job_ids: list[str]) -> dict[str, dict]:
    placeholders = ", ".join("?" for _ in job_ids)
    connection = sqlite3.connect(database)
    try:
        rows = connection.execute(
            "SELECT id, status, started_at, completed_at, error_code, worker_call_id"
            f" FROM jobs WHERE id IN ({placeholders})",
            job_ids,
        ).fetchall()
        outputs = dict(
            connection.execute(
                "SELECT job_id, COUNT(*) FROM job_outputs"
                f" WHERE job_id IN ({placeholders}) GROUP BY job_id",
                job_ids,
            ).fetchall()
        )
    finally:
        connection.close()
    return {
        row[0]: {
            "status": row[1],
            "started_at": row[2],
            "completed_at": row[3],
            "error_code": row[4],
            "worker_call_id": row[5],
            "output_rows": outputs.get(row[0], 0),
        }
        for row in rows
    }


def summarise(
    jobs: dict[str, dict],
    wall_seconds: float,
    audio_seconds: float,
    outputs_on_disk: int,
) -> dict:
    """The report math, kept pure so it is testable without running a pool."""
    windows = [
        (job["started_at"] or 0, job["completed_at"] or 0)
        for job in jobs.values()
        if job["started_at"]
    ]
    windows.sort()
    overlaps = 0
    for index in range(1, len(windows)):
        # Each job that starts before the previous one finished is concurrency
        # actually observed in the database, not just assumed from the pool size.
        if windows[index][0] < windows[index - 1][1]:
            overlaps += 1
    completed = sum(1 for job in jobs.values() if job["status"] == "completed")
    per_job = sorted(
        (job["completed_at"] - job["started_at"]) / 1000
        for job in jobs.values()
        if job["started_at"] and job["completed_at"]
    )
    return {
        "jobs": len(jobs),
        "completed": completed,
        "failed": sum(1 for job in jobs.values() if job["status"] == "failed"),
        "wall_seconds": round(wall_seconds, 1),
        "audio_seconds": audio_seconds,
        "realtime_factor": round(wall_seconds / audio_seconds, 2) if audio_seconds else None,
        "per_job_seconds": [round(value, 1) for value in per_job],
        "median_job_seconds": round(per_job[len(per_job) // 2], 1) if per_job else None,
        "concurrent_starts_observed": overlaps,
        "distinct_workers": len({job["worker_call_id"] for job in jobs.values() if job["worker_call_id"]}),
        "output_rows": sum(job["output_rows"] for job in jobs.values()),
        "outputs_on_disk": outputs_on_disk,
    }


def count_outputs_on_disk(data_dir: Path, job_ids: list[str]) -> int:
    return sum(1 for job_id in job_ids if (data_dir / "results" / job_id).is_dir())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Measure a worker pool on this machine.")
    parser.add_argument("--pool", type=int, default=2, help="worker processes to run at once")
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help="jobs to queue")
    parser.add_argument(
        "--duration",
        type=float,
        default=DEFAULT_DURATION,
        help="seconds of audio per fixture (keep the total under a few minutes)",
    )
    parser.add_argument("--mode", default="vocals_instrumental")
    parser.add_argument("--format", default="mp3", dest="output_format")
    parser.add_argument("--threads", type=int, default=None, help="threads per worker (default cores/pool)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--data-dir", default=None, help="keep the run's data here instead of a temp dir")
    parser.add_argument("--model-dir", default=None, help="where to copy the checkpoint from")
    parser.add_argument("--out", default=None, help="also write the JSON report to this file")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="keep the run's data directory (implied by --data-dir)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.pool < 1 or args.jobs < 1:
        print("--pool and --jobs must be at least 1", file=sys.stderr)
        return 2

    repo_root = Path(__file__).resolve().parents[2]
    # Where the checkpoint already is: the project's model directory, unless the
    # run's own data directory was given (then it is reused, not re-copied).
    model_source = Path(
        args.model_dir
        or os.environ.get("STEMIFY_MODEL_DIR")
        or (Path(os.environ.get("STEMIFY_DATA_DIR") or repo_root / "data") / "models")
    )
    data_dir = Path(args.data_dir) if args.data_dir else Path(tempfile.mkdtemp(prefix="stemify-pool-bench-"))
    cores = os.cpu_count() or 1
    threads = args.threads or resolve_threads(cores, args.pool)

    work = data_dir / "fixtures"
    clip = synthesise_fixture(work / "fixture.wav", args.duration)
    job_ids = seed_jobs(data_dir, args.jobs, clip, args.mode, args.output_format)

    if model_source.is_dir():
        target = data_dir / "models"
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        shutil.copytree(model_source, target)
    else:
        print(f"note: no model directory at {model_source}; the first worker will download it", file=sys.stderr)

    # The slots inherit this process's environment with these four keys set —
    # the same way `start.sh` exports them — rather than a rebuilt environment
    # dict: dropping PATH out of a child's env is exactly the failure mode the
    # security test forbids, and this tool has no reason to copy the env.
    os.environ.update(
        {
            "STEMIFY_DATA_DIR": str(data_dir),
            "STEMIFY_WORKER_THREADS": str(threads),
            "STEMIFY_WORKER_POLL_MS": "200",
            "PYTHONUNBUFFERED": "1",
        }
    )

    logs = []
    processes = []
    started = time.time()
    for slot in range(args.pool):
        # Not a context manager: the child keeps writing to this handle until it
        # exits, which is after this loop (closed in the finally below).
        handle = open(data_dir / f"worker-{slot + 1}.log", "wb")  # noqa: SIM115
        logs.append(handle)
        processes.append(
            # The caller owns the deadline here: the wait loop bounds the run and
            # the finally block terminates (then kills) every slot. Marked in-line
            # for tests/test_security.py's subprocess audit.
            subprocess.Popen(  # timeout: caller-owned; see the comment above
                [sys.executable, "-m", "worker.job_loop"],
                cwd=str(Path(__file__).resolve().parent.parent),
                stdout=handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
        )
    print(
        f"pool={args.pool} jobs={args.jobs} threads/worker={threads} cores={cores} "
        f"audio={args.duration}s each -> {data_dir}",
        flush=True,
    )

    deadline = time.time() + args.timeout
    try:
        while time.time() < deadline:
            jobs = _read_jobs(data_dir / "stemify.sqlite3", job_ids)
            if jobs and all(job["status"] in TERMINAL for job in jobs.values()):
                break
            time.sleep(2)
        else:
            print(f"FAIL: jobs did not finish within {args.timeout}s", file=sys.stderr)
            return 1
        wall_seconds = time.time() - started
        jobs = _read_jobs(data_dir / "stemify.sqlite3", job_ids)
    finally:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                process.kill()
        for handle in logs:
            handle.close()

    report = {
        "environment": {
            "cores": cores,
            "pool": args.pool,
            "threads_per_worker": threads,
            "python": sys.version.split()[0],
            "mode": args.mode,
            "output_format": args.output_format,
        },
        "results": summarise(jobs, wall_seconds, args.duration * args.jobs, count_outputs_on_disk(data_dir, job_ids)),
    }
    text = json.dumps(report, indent=2)
    print(text)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")

    failed = [job_id for job_id, job in jobs.items() if job["status"] != "completed"]
    # A run costs a few hundred MB (the model copy plus fixtures and results), so
    # a successful run cleans up after itself — and a failed one deliberately
    # does not, because the slot logs are then the whole point.
    if args.data_dir or args.keep or failed:
        print(f"data directory: {data_dir}")
    else:
        shutil.rmtree(data_dir, ignore_errors=True)
        print("data directory removed (use --keep or --data-dir to inspect a run)")

    if failed:
        print(f"FAIL: {len(failed)} job(s) did not complete: {failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
