"""Task 10 tests: encoders and output validation.

Uses generated sine-wave fixtures only. Skips cleanly when ffmpeg is
unavailable, matching test_input_audio.py.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from worker.encoding import (
    FORMAT_EXTENSIONS,
    FORMAT_MIME_TYPES,
    OutputError,
    encode_stem,
    sha256_file,
    validate_encoded_output,
)
from worker.errors import ErrorCode
from worker.input_audio import (
    JobTempDir,
    _require_ffmpeg_tool,
    decode_to_canonical_wav,
    prepare_source,
)

try:
    FFMPEG = _require_ffmpeg_tool("ffmpeg")
except Exception:  # noqa: BLE001 - mirrors test_input_audio's availability gate
    FFMPEG = None

pytestmark = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg not available")


def make_canonical_wav(dest: Path, seconds: float) -> None:
    """Generate a 44.1 kHz stereo float32 WAV master fixture."""
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
        "pcm_f32le",
        "-f",
        "wav",
        str(dest),
    ]
    import subprocess

    subprocess.run(cmd, check=True, capture_output=True)


@pytest.mark.parametrize("output_format", ["mp3", "wav", "flac", "ogg", "m4a"])
def test_encode_stem_produces_valid_output(tmp_path: Path, output_format: str) -> None:
    master = tmp_path / "master.wav"
    make_canonical_wav(master, 1.0)
    dest = tmp_path / f"vocals.{FORMAT_EXTENSIONS[output_format]}"

    encode_stem(master, dest, output_format)

    probe = validate_encoded_output(dest, 1.0)
    assert probe.sample_rate == 44100
    assert probe.channels == 2
    assert dest.suffix == f".{FORMAT_EXTENSIONS[output_format]}"
    assert FORMAT_MIME_TYPES[output_format].startswith("audio/")


def test_encode_rejects_unknown_format(tmp_path: Path) -> None:
    master = tmp_path / "master.wav"
    make_canonical_wav(master, 0.5)
    with pytest.raises(OutputError) as excinfo:
        encode_stem(master, tmp_path / "x.aac", "aac")
    assert excinfo.value.code == ErrorCode.OUTPUT_ENCODING_FAILED


def test_validate_rejects_duration_drift(tmp_path: Path) -> None:
    master = tmp_path / "master.wav"
    make_canonical_wav(master, 1.0)
    encoded = tmp_path / "vocals.mp3"
    encode_stem(master, encoded, "mp3")
    with pytest.raises(OutputError) as excinfo:
        validate_encoded_output(encoded, 30.0)  # master claimed 30 s
    assert excinfo.value.code == ErrorCode.OUTPUT_VALIDATION_FAILED


def test_sha256_file_is_stable(tmp_path: Path) -> None:
    sample = tmp_path / "sample.bin"
    sample.write_bytes(b"stemify")
    assert sha256_file(sample) == sha256_file(sample)
    assert len(sha256_file(sample)) == 64


def test_encode_from_job_pipeline_master(tmp_path: Path) -> None:
    """End-to-end slice: uploaded fixture -> canonical master -> encoded stem."""
    source = tmp_path / "tone.mp3"
    cmd = [
        FFMPEG,  # type: ignore[list-item]
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=0.5",
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(source),
    ]
    import subprocess

    subprocess.run(cmd, check=True, capture_output=True)

    with JobTempDir() as job_dir:
        inside = job_dir / "tone.mp3"
        shutil.copy(source, inside)
        canonical, probe = prepare_source(inside, job_dir)
        assert canonical.name == "input.wav"
        stem = job_dir / "outputs" / "vocals.mp3"
        encode_stem(canonical, stem, "mp3")
        result = validate_encoded_output(stem, probe.duration_seconds)
        assert result.has_audio
        assert decode_to_canonical_wav is not None  # canonical decode precedes encode
