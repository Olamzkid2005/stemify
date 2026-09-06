"""Task 8 tests: validation ladder, decode, temp-dir cleanup, stable codes.

Fixtures are generated locally with ffmpeg (sine tones and noise) — no
copyrighted material. Tests skip cleanly when ffmpeg is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from worker.errors import ErrorCode
from worker.input_audio import (
    JobTempDir,
    InputAudioError,
    _require_ffmpeg_tool,
    prepare_source,
    run_ffprobe,
    validate_source,
)

try:
    FFMPEG = _require_ffmpeg_tool("ffmpeg")
except InputAudioError:
    FFMPEG = None
pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")


def make_audio(path: Path, seconds: float, kind: str = "sine") -> None:
    """Generate a small fixture audio file with ffmpeg."""
    if kind == "sine":
        src = f"sine=frequency=440:duration={seconds}"
    elif kind == "noise":
        src = f"anoisesrc=d={seconds}:c=pink"
    else:
        raise ValueError(kind)
    cmd = [
        FFMPEG,  # type: ignore[list-item]
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        src,
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(path),
    ]
    subprocess.run(cmd, check=True, capture_output=True)


def test_temp_dir_cleans_up_on_success_and_failure() -> None:
    with JobTempDir() as job_dir:
        assert job_dir.exists()
        survivor = job_dir / "x.txt"
        survivor.write_text("data")
    assert not survivor.exists()

    with pytest.raises(RuntimeError):
        with JobTempDir() as job_dir2:
            raise RuntimeError("boom")
    assert not job_dir2.exists()


def test_valid_fixture_passes_validation_and_decode(tmp_path: Path) -> None:
    src = tmp_path / "tone.mp3"
    make_audio(src, 1.0)

    with JobTempDir() as job_dir:
        inside = job_dir / "tone.mp3"
        shutil.copy(src, inside)
        canonical, probe = prepare_source(inside, job_dir)
        assert canonical.exists()
        assert canonical.stat().st_size > 44  # WAV header + samples
        assert 0.5 < probe.duration_seconds < 2.0

        # The canonical WAV itself probes as 44.1 kHz stereo float32.
        wav_probe = run_ffprobe(canonical)
        assert wav_probe.sample_rate == 44100
        assert wav_probe.channels == 2
        assert wav_probe.codec_name == "pcm_f32le"


def test_non_audio_file_is_rejected(tmp_path: Path) -> None:
    fake = tmp_path / "not-audio.mp3"
    fake.write_bytes(b"\x00" * 1024)
    with pytest.raises(InputAudioError) as excinfo:
        validate_source(fake)
    assert excinfo.value.code == ErrorCode.INVALID_AUDIO


def test_bad_extension_is_rejected(tmp_path: Path) -> None:
    evil = tmp_path / "payload.exe"
    evil.write_bytes(b"MZ\x90\x00")
    with pytest.raises(InputAudioError) as excinfo:
        validate_source(evil)
    assert excinfo.value.code == ErrorCode.INVALID_AUDIO


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    empty = tmp_path / "empty.mp3"
    empty.write_bytes(b"")
    with pytest.raises(InputAudioError) as excinfo:
        validate_source(empty)
    assert excinfo.value.code == ErrorCode.INVALID_AUDIO


def test_over_duration_real(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Write a 9-second file and shrink the limit via monkeypatched constant."""
    import worker.input_audio as mod

    long = tmp_path / "long.wav"
    cmd = [
        FFMPEG,  # type: ignore[list-item]
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=9",
        str(long),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    monkeypatch.setattr(mod, "MAX_DURATION_SECONDS", 5.0)
    with pytest.raises(InputAudioError) as excinfo:
        mod.validate_source(long)
    assert excinfo.value.code == ErrorCode.LIMIT_EXCEEDED


def test_oversize_file_is_limit_exceeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = tmp_path / "tone.mp3"
    make_audio(src, 0.5)
    import worker.input_audio as mod

    monkeypatch.setattr(mod, "MAX_FILE_BYTES", 10)
    with pytest.raises(InputAudioError) as excinfo:
        mod.validate_source(src)
    assert excinfo.value.code == ErrorCode.LIMIT_EXCEEDED


def test_source_outside_job_dir_is_rejected(tmp_path: Path) -> None:
    src = tmp_path / "outside.mp3"
    make_audio(src, 0.5)
    with JobTempDir() as job_dir:
        with pytest.raises(InputAudioError) as excinfo:
            prepare_source(src, job_dir)
    assert excinfo.value.code == ErrorCode.INVALID_AUDIO
