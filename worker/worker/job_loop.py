"""Local worker loop (plan Section 12.1): poll, claim, process, sleep.

Run directly with:

    python -m worker.job_loop

One loop iteration claims at most one job. Upload jobs are validated with the
Task 8 ladder (ffprobe, limits, canonical decode); separation is still a stub
until Task 9, so a claimed job either fails validation-safe or stops at the
stub with a stable error code. Nothing is ever left in `processing`.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys
import time
from collections.abc import Iterator

from worker.database import ClaimedJob, JobQueue
from worker.errors import ErrorCode
from worker.input_audio import InputAudioError, JobTempDir, prepare_source
from worker.stages import Stage

DEFAULT_POLL_MS = 500


def _poll_seconds() -> float:
    raw = os.environ.get("STEMIFY_WORKER_POLL_MS")
    try:
        return max(0.05, int(raw) / 1000) if raw else DEFAULT_POLL_MS / 1000
    except ValueError:
        return DEFAULT_POLL_MS / 1000


@contextlib.contextmanager
def _graceful_shutdown() -> Iterator[None]:
    """Stop the loop after the current job when SIGINT/SIGTERM arrives."""
    stopping = {"flag": False}

    def request_stop(signum: int, _frame: object) -> None:
        stopping["flag"] = True
        print(f"worker: signal {signum} received; stopping after current job", flush=True)

    previous = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):  # not on the main thread / not supported
            previous[sig] = signal.signal(sig, request_stop)
    try:
        yield
    finally:
        for sig, handler in previous.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(sig, handler)


def process_job(queue: JobQueue, job: ClaimedJob) -> None:
    """Run one attempt. Every path ends in a terminal or still-queued state."""
    try:
        if job.source_type == "youtube":
            queue.fail_job(job.id, ErrorCode.DOWNLOAD_FAILED, "YouTube sources are not supported yet.")
            return

        queue.update_progress(job.id, Stage.VALIDATING, 15)

        if not job.source_object_key:
            queue.fail_job(job.id, ErrorCode.INVALID_AUDIO, "The uploaded file reference is missing.")
            return

        # Objects live at <data_dir>/<object_key> (web LocalStorage layout).
        source = queue.data_dir / job.source_object_key
        with JobTempDir() as job_dir:
            inbox = job_dir / "source"
            inbox.mkdir()
            staged = inbox / (source.name or "source")
            try:
                staged.symlink_to(source.resolve())
            except OSError:  # filesystems without symlink support (e.g. Windows)
                import shutil

                shutil.copy(source, staged)

            queue.update_progress(job.id, Stage.PREPARING_AUDIO, 25)
            prepare_source(staged, job_dir)

        queue.fail_job(
            job.id,
            ErrorCode.MODEL_LOAD_FAILED,
            "Stem separation is not enabled in this build yet. Your file passed all checks.",
        )
    except InputAudioError as error:
        queue.fail_job(job.id, error.code, _public_message(error.code))
    except Exception:
        queue.fail_job(job.id, ErrorCode.UNKNOWN, "Something went wrong while processing this job.")


def _public_message(code: ErrorCode) -> str:
    """User-safe messages only — never raw paths or stderr (plan Section 7.5)."""
    if code == ErrorCode.INVALID_AUDIO:
        return "This file does not look like playable audio, or it is damaged."
    if code == ErrorCode.LIMIT_EXCEEDED:
        return "This file is over the size or duration limit."
    return "Something went wrong while processing this job."


def run(stop_after_iterations: int | None = None) -> None:
    queue = JobQueue()
    queue.recover_stale_processing_jobs()
    poll_seconds = _poll_seconds()
    print(f"worker: polling {queue.database_path} every {poll_seconds:.2f}s", flush=True)
    try:
        iterations = 0
        with _graceful_shutdown():
            while not stop_after_iterations or iterations < stop_after_iterations:
                iterations += 1
                job = queue.claim_next_queued_job()
                if job is None:
                    time.sleep(poll_seconds)
                    continue
                print(f"worker: claimed job {job.id}", flush=True)
                process_job(queue, job)
                print(f"worker: finished job {job.id}", flush=True)
    finally:
        queue.close()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(130)
