"""Local worker loop (plan Sections 12.1, 9.3): poll, claim, process, sleep.

Run directly with:

    python -m worker.job_loop

One loop iteration claims at most one job and runs it through the full local
pipeline: validation (Task 8), Demucs separation (Task 9), encoding and
packaging (Task 10), and output persistence (this task). Nothing is ever left
in `processing`; cancellation is honored at stage boundaries.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from worker.database import ClaimedJob, JobQueue
from worker.encoding import OutputError
from worker.errors import ErrorCode
from worker.input_audio import InputAudioError, JobTempDir, prepare_source
from worker.models.base import SeparationError
from worker.pipeline import (
    encode_stems_stage,
    package_stage,
    run_separation_stage,
)
from worker.stages import Stage
from worker.youtube import download_audio

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


def _fail(queue: JobQueue, job_id: str, error: Exception, fallback: ErrorCode) -> None:
    """Map any pipeline error to a terminal failed state with a safe message."""
    if isinstance(error, (InputAudioError, SeparationError, OutputError)):
        code = error.code
    else:
        code = fallback
    queue.fail_job(job_id, code, _public_message(code))


def _cancellation_checker(queue: JobQueue, job_id: str):
    def check() -> bool:
        return queue.is_cancel_requested(job_id)

    return check


def _raise_if_canceled(queue: JobQueue, job_id: str) -> None:
    """Stage-boundary cancellation check (plan Section 9.5)."""
    if queue.is_cancel_requested(job_id):
        raise SeparationError(ErrorCode.CANCELED, "canceled at a stage boundary")


def _publish_outputs(queue: JobQueue, job: ClaimedJob, output_rows: list[dict], job_output_dir: Path) -> None:
    """Move validated artifacts into results/ and record job_outputs rows.

    Runs in one short transaction window per row; a crash between rows leaves
    the job processing (recovered at startup), never half-completed.
    """
    results_root = queue.data_dir / "results" / job.id
    results_root.mkdir(parents=True, exist_ok=True)
    for row in output_rows:
        source = job_output_dir / Path(row["relative_path"]).name
        dest = results_root / Path(row["relative_path"]).name
        shutil.move(str(source), str(dest))
        queue.record_output(
            job_id=job.id,
            stem_key=row["stem_key"],
            label=row["label"],
            relative_path=row["relative_path"],
            mime_type=row["mime_type"],
            size_bytes=row["size_bytes"],
            duration_seconds=row["duration_seconds"],
            sha256=row["sha256"],
            expires_at=row["expires_at"],
        )


def process_job(queue: JobQueue, job: ClaimedJob) -> None:
    """Run one attempt. Every path ends in a terminal or still-queued state."""
    job_output_dir = None
    try:
        staged: Path | None = None
        source: Path | None = None

        if job.source_type == "youtube":
            # Optional YouTube input (plan Task 14 / Section 9.6): the worker
            # downloads with controlled yt-dlp arguments into the job temp
            # directory, then the normal validation/separation path runs on it.
            if not job.source_url:
                queue.fail_job(job.id, ErrorCode.DOWNLOAD_FAILED, _public_message(ErrorCode.DOWNLOAD_FAILED))
                return
        elif not job.source_object_key:
            queue.fail_job(job.id, ErrorCode.INVALID_AUDIO, _public_message(ErrorCode.INVALID_AUDIO))
            return

        # Upload objects live at <data_dir>/<object_key> (web LocalStorage layout).
        if job.source_object_key:
            source = queue.data_dir / job.source_object_key

        with JobTempDir() as job_dir:
            if job.source_type == "youtube":
                queue.update_progress(job.id, Stage.DOWNLOADING, 10)
                download_dir = job_dir / "download"
                download_dir.mkdir()
                staged = download_audio(job.source_url, download_dir)
            else:
                queue.update_progress(job.id, Stage.VALIDATING, 15)
                inbox = job_dir / "source"
                inbox.mkdir()
                staged = inbox / (job.source_filename or (source.name if source else "source"))
                try:
                    staged.symlink_to(source.resolve())
                except OSError:  # filesystems without symlink support (e.g. Windows)
                    shutil.copy(source, staged)

            queue.update_progress(job.id, Stage.PREPARING_AUDIO, 25)
            canonical_wav, probe = prepare_source(staged, job_dir)

            stems = run_separation_stage(
                canonical_wav,
                job_dir,
                job.mode,
                progress_callback=lambda stage, progress: queue.update_progress(job.id, stage, progress),
                cancellation_checker=_cancellation_checker(queue, job.id),
            )
            _raise_if_canceled(queue, job.id)

            encoded = encode_stems_stage(
                stems,
                job_dir,
                job.output_format,
                progress_callback=lambda stage, progress: queue.update_progress(job.id, stage, progress),
            )
            del stems
            _raise_if_canceled(queue, job.id)

            from worker.models.profiles import get_profile

            profile = get_profile(os.environ.get("STEMIFY_MODEL_PROFILE", "demucs_default"))
            packaged = package_stage(
                job_id=job.id,
                encoded_stems=encoded,
                job_dir=job_dir,
                mode=job.mode,
                output_format=job.output_format,
                source_display_name=(
                    job.source_filename
                    or (source.name if source else staged.name if staged else "source")
                ),
                source_duration_seconds=probe.duration_seconds,
                source_sample_rate=probe.sample_rate,
                source_channels=probe.channels,
                profile=profile,
                expires_at_ms=job.expires_at,
                progress_callback=lambda stage, progress: queue.update_progress(job.id, stage, progress),
            )

            job_output_dir = job_dir / "outputs"
            _publish_outputs(queue, job, packaged.output_rows, job_output_dir)

        if queue.complete_job(job.id):
            print(f"worker: job {job.id} completed", flush=True)
    except InputAudioError as error:
        queue.fail_job(job.id, error.code, _public_message(error.code))
    except SeparationError as error:
        if error.code == ErrorCode.CANCELED:
            queue.cancel_processing_job(job.id)
        else:
            queue.fail_job(job.id, error.code, _public_message(error.code))
    except OutputError as error:
        queue.fail_job(job.id, error.code, _public_message(error.code))
    except Exception:  # noqa: BLE001 - deliberate catch-all: any unexpected error must end the job as UNKNOWN, never leave it processing
        queue.fail_job(job.id, ErrorCode.UNKNOWN, _public_message(ErrorCode.UNKNOWN))


def _public_message(code: ErrorCode) -> str:
    """User-safe messages only — never raw paths or stderr (plan Section 7.5)."""
    if code == ErrorCode.INVALID_AUDIO:
        return "This file does not look like playable audio, or it is damaged."
    if code == ErrorCode.LIMIT_EXCEEDED:
        return "This file is over the size or duration limit."
    if code == ErrorCode.CANCELED:
        return "This job was canceled."
    if code == ErrorCode.DOWNLOAD_FAILED:
        return "The download failed, or the source is not supported."
    if code == ErrorCode.MODEL_LOAD_FAILED:
        return "The separation engine is unavailable. Run: pip install -r worker/requirements.txt"
    if code == ErrorCode.GPU_OUT_OF_MEMORY:
        return "The device ran out of memory. Try a shorter file or CPU processing."
    if code == ErrorCode.OUTPUT_ENCODING_FAILED:
        return "We could not encode the separated stems."
    if code == ErrorCode.OUTPUT_VALIDATION_FAILED:
        return "A generated file did not pass validation."
    return "Something went wrong while processing this job."


HEARTBEAT_INTERVAL_MS = 5000


def _heartbeat_loop(queue: JobQueue, stop: threading.Event) -> None:
    """Write the liveness row every few seconds (plan Section 19.2).

    Runs on a daemon thread so the heartbeat keeps ticking while a long
    separation blocks the main loop; the web UI reads this row to show its
    non-sensitive worker-unavailable state.
    """
    while not stop.is_set():
        try:
            queue.write_heartbeat()
        except Exception:  # noqa: BLE001,S110 - heartbeat must never kill the worker; retried next tick
            pass
        stop.wait(HEARTBEAT_INTERVAL_MS / 1000)


def run(stop_after_iterations: int | None = None) -> None:
    queue = JobQueue()
    queue.recover_stale_processing_jobs()
    queue.write_heartbeat()
    poll_seconds = _poll_seconds()
    print(f"worker: polling {queue.database_path} every {poll_seconds:.2f}s", flush=True)
    stop = threading.Event()
    heartbeat = threading.Thread(target=_heartbeat_loop, args=(queue, stop), daemon=True)
    heartbeat.start()
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
        stop.set()
        heartbeat.join(timeout=HEARTBEAT_INTERVAL_MS / 1000)
        queue.close()


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(130)
