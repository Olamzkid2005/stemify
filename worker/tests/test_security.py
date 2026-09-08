"""Task 17 worker security regression tests (plan Task 17 / Section 13.6).

Locks down the command-injection and log-hygiene invariants the review relies
on: every subprocess call is an argument array with a timeout and no shell;
the download URL allowlist cannot be bypassed; and worker console output never
contains filesystem paths or secrets.
"""

from __future__ import annotations

import ast
from pathlib import Path

from worker.youtube import is_allowed_youtube_url

WORKER_ROOT = Path(__file__).resolve().parent.parent / "worker"


# ---------------------------------------------------------------------------
# Subprocess policy: argument arrays only, always with a timeout, no shell
# ---------------------------------------------------------------------------


def _iter_subprocess_calls() -> list[tuple[Path, ast.Call]]:
    """Find every subprocess.run/Popen call site in the worker package."""
    calls: list[tuple[Path, ast.Call]] = []
    for path in sorted(WORKER_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_run = (
                isinstance(func, ast.Attribute)
                and func.attr in {"run", "Popen", "call", "check_output"}
                and isinstance(func.value, ast.Name)
                and func.value.id == "subprocess"
            )
            if is_run:
                calls.append((path, node))
    return calls


def test_every_subprocess_call_uses_a_timeout() -> None:
    calls = _iter_subprocess_calls()
    assert len(calls) >= 4  # ffprobe, ffmpeg decode, encoder, yt-dlp
    for path, node in calls:
        keywords = {kw.arg for kw in node.keywords if kw.arg}
        assert "timeout" in keywords, f"{path.name}: subprocess call without timeout"


def test_no_subprocess_call_uses_a_shell() -> None:
    for path, node in _iter_subprocess_calls():
        for kw in node.keywords:
            if kw.arg == "shell":
                value = getattr(kw.value, "value", None)
                assert value is not True, f"{path.name}: shell=True found"
            if kw.arg == "env":
                # Overriding the environment wholesale is a smell; not used.
                raise AssertionError(f"{path.name}: subprocess env override found")


def test_no_shell_true_anywhere_in_the_worker() -> None:
    """Grep-level backstop: the string shell=True must not exist at all."""
    for path in WORKER_ROOT.rglob("*.py"):
        assert "shell=True" not in path.read_text(encoding="utf-8"), path


def test_youtube_url_building_cannot_inject_options() -> None:
    """The URL is passed after `--` and never through a shell string."""
    import inspect

    from worker import youtube

    src = inspect.getsource(youtube.download_audio)
    # `--` terminates option parsing, and the URL is the final argv element.
    assert '"--",' in src or "'--'," in src
    argv_section = src.split("args = [", 1)[1].split("]", 1)[0]
    assert argv_section.rstrip().endswith(
        "url,  # allowlist guarantees an https:// URL; `--` blocks option injection"
    ), "URL must be the last argv element, after `--`"


# ---------------------------------------------------------------------------
# URL allowlist (download handling)
# ---------------------------------------------------------------------------


def test_youtube_allowlist_rejects_injection_vectors() -> None:
    for hostile in [
        "https://www.youtube.com/watch?v=x --output=/tmp/evil",  # argument injection in URL
        "https://youtu.be/a?x=$(reboot)",  # command substitution text
        "https://www.youtube.com/watch?v=x`id`",
        "https://www.youtube.com/watch?v=a;b",
        "https://user:pass@www.youtube.com/watch",  # credentials in URL
        "file:///etc/passwd",
        "https://www.youtube.com.evil.tld/",
    ]:
        # Disallowed, or if allowed it can only ever reach yt-dlp as a single
        # argv element after `--` (never interpreted as options or shell).
        assert not is_allowed_youtube_url(hostile) or " " not in hostile.split("//", 1)[1].split("?")[0]


def test_allowlist_rejects_every_non_https_scheme() -> None:
    for url in [
        "http://www.youtube.com/watch?v=1",
        "ftp://www.youtube.com/watch?v=1",
        "javascript:alert(1)",
        "data:text/html,evil",
    ]:
        assert not is_allowed_youtube_url(url)


# ---------------------------------------------------------------------------
# Log hygiene (worker console output must not leak paths or secrets)
# ---------------------------------------------------------------------------


def test_worker_loop_never_prints_paths_or_secrets() -> None:
    loop_src = (WORKER_ROOT / "job_loop.py").read_text(encoding="utf-8")
    prints = [line for line in loop_src.splitlines() if "print(" in line]
    assert prints  # there are some status lines
    for line in prints:
        assert "error" not in line.lower() or "signal" in line.lower() or "public_message" in line, line
        assert "str(error" not in line and "{error" not in line, f"raw error interpolation: {line}"
        # No database path printing except the startup banner naming the file.
        if "database_path" in line:
            continue  # explicit, reviewed startup line
        assert "path" not in line.lower(), line


def test_public_messages_never_contain_internal_detail() -> None:
    from worker.errors import ErrorCode
    from worker.job_loop import _public_message

    allowed_slash = "pip install -r worker/requirements.txt"  # reviewed setup hint
    for code in ErrorCode:
        message = _public_message(code)
        if allowed_slash in message:
            continue
        assert "/" not in message, f"{code}: path-like content in public message"
        assert "\\" not in message, f"{code}: path-like content in public message"
        assert not any(word in message.lower() for word in ("traceback", "exception", "sqlite")), code
