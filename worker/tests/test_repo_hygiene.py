"""Task 17 repo hygiene: runtime data and model weights are never committed.

Acceptance item from plan Task 17: "Runtime data and model weights are not
committed." This test walks the tracked tree (via git ls-files) and the
.gitignore rules, and fails if any runtime artifact sneaks into version
control or if the ignore rules regress.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return [name for name in result.stdout.decode("utf-8").split("\0") if name]


def test_no_runtime_data_or_model_weights_are_tracked() -> None:
    tracked = _tracked_files()
    forbidden_suffixes = (
        ".th",  # torch checkpoints
        ".ckpt",
        ".sqlite3",  # local job database
        ".db",
        ".mp3", ".wav", ".flac", ".ogg", ".m4a",  # audio fixtures/results
        ".zip",  # results archives
        ".log",  # worker logs
    )
    offenders = [
        name
        for name in tracked
        if name.lower().endswith(forbidden_suffixes)
        or "/data/" in f"/{name}"
        or name.startswith("data/")
    ]
    assert offenders == [], f"runtime artifacts tracked in git: {offenders[:10]}"


def test_no_env_files_or_secrets_are_tracked() -> None:
    tracked = _tracked_files()
    offenders = [
        name
        for name in tracked
        if name == ".env"
        or (name.startswith(".env.") and name != ".env.example")
        or "credential" in name.lower()
        or "secret" in name.lower()
    ]
    assert offenders == [], f"env/secret files tracked: {offenders}"


def test_gitignore_covers_the_critical_namespaces() -> None:
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    # Each line is one non-negotiable ignore rule (plan Section 17.4).
    for required in ("data/", "*.th", ".env"):
        assert required in gitignore, f".gitignore lost the rule for {required}"


def test_no_target_checkpoint_inside_the_repo_tree() -> None:
    """A downloaded checkpoint inside the working tree must be ignored, never staged."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", "worker/data/models/hybrid_transformer/955717e8-8726e21a.th"],
        cwd=REPO_ROOT,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, "model checkpoint path is not gitignored"


def test_source_licenses_are_recorded() -> None:
    """License references must be documented (plan Sections 14.3, 17)."""
    profiles = (REPO_ROOT / "worker" / "worker" / "models" / "profiles.py").read_text(
        encoding="utf-8"
    )
    assert "license_reference" in profiles
    assert "MIT" in profiles  # demucs code and htdemucs weights
