"""Spotify plan milestones S1 and S2.5: link policy, metadata probe, login.

Pure unit tests, no network: the module's single network seam
(`worker.spotify._request_json`) is stubbed, and the tests that must prove
"nothing leaves the machine" assert that the seam was never called at all.
Audio acquisition is S2 (`tests/test_spotify_fetch.py`) and the operator login
is S2.5; the login tests here stub the client library, so they pin the
credential location and guard rails without a Spotify account or a browser.
"""

from __future__ import annotations

import argparse
import base64
import builtins
import sys
import urllib.error
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from worker.errors import ErrorCode
from worker.spotify import (
    CACHE_DIR_NAME,
    CREDENTIALS_FILE_ENV,
    DEFAULT_CREDENTIALS_FILENAME,
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    SpotifyError,
    TrackMetadata,
    credentials_file,
    credentials_path,
    fetch_track_metadata,
    is_allowed_spotify_url,
    login,
    parse_track_id,
    resolve_title,
    spotify_credentials,
    spotify_enabled,
    unavailable_error,
)

TRACK_ID = "4cOdK2wGLETKBW3PvgPWqT"
TRACK_URL = f"https://open.spotify.com/track/{TRACK_ID}"

TRACK_PAYLOAD = {
    "name": "Tease Me",
    "artists": [{"name": "Zaylevelten"}],
    "album": {"name": "Tease Me"},
    "duration_ms": 214000,
}


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests exercise the configured path; the kill switch tests undo this."""
    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "1")
    monkeypatch.setenv("STEMIFY_SPOTIFY_CLIENT_ID", "client-id")
    monkeypatch.setenv("STEMIFY_SPOTIFY_CLIENT_SECRET", "client-secret")


def stub_metadata(
    monkeypatch: pytest.MonkeyPatch,
    handler: Any,
) -> list[dict[str, Any]]:
    """Replace the network seam and record every call it would have made."""
    calls: list[dict[str, Any]] = []

    def fake_request(
        url: str,
        *,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        calls.append({"url": url, "data": data, "headers": headers or {}})
        return handler(url, data)

    monkeypatch.setattr("worker.spotify._request_json", fake_request)
    return calls


def _token_then(payload: Any, *, token: Any = "access-token") -> Any:
    """Handler that answers the token request, then anything else with payload."""

    def handler(url: str, data: Any) -> Any:
        if "accounts.spotify.com" in url:
            return {"access_token": token} if token is not None else {}
        return payload

    return handler


# ---------------------------------------------------------------------------
# Link allowlist (worker-side re-validation)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        f"https://open.spotify.com/track/{TRACK_ID}",
        f"https://open.spotify.com/track/{TRACK_ID}?si=abc123",  # query is ignored
        f"https://open.spotify.com/intl-de/track/{TRACK_ID}",  # locale prefix
        f"https://open.spotify.com/intl-pt-BR/track/{TRACK_ID}",
        f"spotify:track:{TRACK_ID}",
    ],
)
def test_allowlist_accepts_track_links_and_uris(value: str) -> None:
    assert is_allowed_spotify_url(value)
    assert parse_track_id(value) == TRACK_ID


@pytest.mark.parametrize(
    "value",
    [
        f"http://open.spotify.com/track/{TRACK_ID}",  # not https
        "https://open.spotify.com/",  # no track
        "https://open.spotify.com/track/",  # missing id
        f"https://open.spotify.com/album/{TRACK_ID}",  # v1 is single-track
        f"https://open.spotify.com/playlist/{TRACK_ID}",
        f"https://open.spotify.com/user/someone/track/{TRACK_ID}",
        "https://spotify.link/abc123",  # short link needs a redirect to resolve
        f"spotify:album:{TRACK_ID}",
        f"spotify:track:{TRACK_ID[:-1]}",  # 21 chars
        f"spotify:track:{TRACK_ID}0",  # 23 chars
        f"https://open.spotify.com/track/{TRACK_ID[:-1]}!",
        "https://evil.example.com/track/" + TRACK_ID,  # wrong host
        "https://open.spotify.com.evil.com/track/" + TRACK_ID,  # suffix spoof
        "https://user@evil.com@open.spotify.com/track/" + TRACK_ID,  # userinfo trick
        "https://open.spotify.com/track/" + "x" * 2100,  # over 2048 chars
        "not a url at all",
        "",
    ],
)
def test_allowlist_rejects_everything_else(value: str) -> None:
    assert not is_allowed_spotify_url(value)
    assert parse_track_id(value) is None


def test_allowlist_rejects_non_string() -> None:
    assert not is_allowed_spotify_url(None)  # type: ignore[arg-type]
    assert not is_allowed_spotify_url(123)  # type: ignore[arg-type]


def test_track_ids_keep_their_case() -> None:
    """Spotify ids are case-sensitive base-62: never normalise them."""
    mixed = "1A2b3C4d5E6f7G8h9I0jKl"
    assert parse_track_id(f"spotify:track:{mixed}") == mixed
    assert parse_track_id(f"spotify:track:{mixed.lower()}") != mixed


# ---------------------------------------------------------------------------
# Kill switch and operator credentials
# ---------------------------------------------------------------------------


def test_kill_switch_defaults_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Spotify stays off until the operator configures it (unlike YouTube)."""
    monkeypatch.delenv("STEMIFY_SPOTIFY_ENABLED", raising=False)
    assert spotify_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes"])
def test_kill_switch_enables(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", value)
    assert spotify_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "", "off"])
def test_kill_switch_stays_off_for_anything_else(
    value: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", value)
    assert spotify_enabled() is False


def test_credentials_require_both_halves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("STEMIFY_SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("STEMIFY_SPOTIFY_CLIENT_SECRET", raising=False)
    assert spotify_credentials() is None

    monkeypatch.setenv("STEMIFY_SPOTIFY_CLIENT_ID", "only-id")
    assert spotify_credentials() is None

    monkeypatch.delenv("STEMIFY_SPOTIFY_CLIENT_ID")
    monkeypatch.setenv("STEMIFY_SPOTIFY_CLIENT_SECRET", "only-secret")
    assert spotify_credentials() is None


def test_credentials_are_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STEMIFY_SPOTIFY_CLIENT_ID", "  id  ")
    monkeypatch.setenv("STEMIFY_SPOTIFY_CLIENT_SECRET", "  secret  ")
    assert spotify_credentials() == ("id", "secret")


# ---------------------------------------------------------------------------
# Metadata probe
# ---------------------------------------------------------------------------


def test_metadata_lookup_uses_the_two_fixed_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token first, then the track; both fixed URLs, token in the header."""
    calls = stub_metadata(monkeypatch, _token_then(TRACK_PAYLOAD))

    metadata = fetch_track_metadata(TRACK_ID)

    assert metadata == TrackMetadata(
        track_id=TRACK_ID,
        title="Tease Me",
        artist="Zaylevelten",
        album="Tease Me",
        duration_ms=214000,
    )
    assert [call["url"] for call in calls] == [
        "https://accounts.spotify.com/api/token",
        f"https://api.spotify.com/v1/tracks/{TRACK_ID}",
    ]
    assert calls[0]["data"] == {"grant_type": "client_credentials"}
    expected_basic = base64.b64encode(b"client-id:client-secret").decode("ascii")
    assert calls[0]["headers"]["Authorization"] == f"Basic {expected_basic}"
    assert calls[1]["headers"]["Authorization"] == "Bearer access-token"


def test_http_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo, or an absurdly low override, must not disable the timeout."""
    from worker.spotify import _http_timeout_seconds

    monkeypatch.setenv("STEMIFY_SPOTIFY_HTTP_TIMEOUT_SECONDS", "nonsense")
    assert _http_timeout_seconds() == DEFAULT_HTTP_TIMEOUT_SECONDS
    monkeypatch.setenv("STEMIFY_SPOTIFY_HTTP_TIMEOUT_SECONDS", "1")
    assert _http_timeout_seconds() == 5
    monkeypatch.setenv("STEMIFY_SPOTIFY_HTTP_TIMEOUT_SECONDS", "45")
    assert _http_timeout_seconds() == 45


def test_disabled_feature_never_touches_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "0")
    calls = stub_metadata(monkeypatch, _token_then(TRACK_PAYLOAD))

    assert fetch_track_metadata(TRACK_ID) is None
    assert resolve_title(TRACK_URL) is None
    assert calls == []


def test_missing_credentials_never_touch_the_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("STEMIFY_SPOTIFY_CLIENT_ID", raising=False)
    monkeypatch.delenv("STEMIFY_SPOTIFY_CLIENT_SECRET", raising=False)
    calls = stub_metadata(monkeypatch, _token_then(TRACK_PAYLOAD))

    assert fetch_track_metadata(TRACK_ID) is None
    assert resolve_title(TRACK_URL) is None
    assert calls == []


def test_invalid_track_id_is_not_looked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = stub_metadata(monkeypatch, _token_then(TRACK_PAYLOAD))
    assert fetch_track_metadata("../../etc/passwd") is None
    assert calls == []


def test_token_failure_stops_before_the_track_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = stub_metadata(monkeypatch, _token_then(TRACK_PAYLOAD, token=None))
    assert fetch_track_metadata(TRACK_ID) is None
    assert len(calls) == 1


@pytest.mark.parametrize(
    "payload",
    [
        None,  # track request failed
        {},
        {"name": ""},
        {"name": "   "},
        {"name": 42},
        {"name": None},
    ],
)
def test_unusable_track_payload_returns_none(
    payload: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub_metadata(monkeypatch, _token_then(payload))
    assert fetch_track_metadata(TRACK_ID) is None


def test_optional_fields_degrade_instead_of_failing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_metadata(
        monkeypatch,
        _token_then(
            {
                "name": "Untitled Demo",
                "artists": "not-a-list",  # API shape drift
                "album": None,
                "duration_ms": "soon",
            }
        ),
    )
    metadata = fetch_track_metadata(TRACK_ID)
    assert metadata == TrackMetadata(
        track_id=TRACK_ID,
        title="Untitled Demo",
        artist="",
        album="",
        duration_ms=0,
    )


def test_multiple_artists_are_joined(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_metadata(
        monkeypatch,
        _token_then({"name": "Song", "artists": [{"name": "A"}, {"name": "B"}, {"nope": 1}]}),
    )
    assert resolve_title(TRACK_URL) == "A, B - Song"


def test_network_failure_is_contained(monkeypatch: pytest.MonkeyPatch) -> None:
    """Naming is best-effort: an unreachable API must not fail anything."""

    def explode(*args: Any, **kwargs: Any) -> Any:
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr("worker.spotify.urllib.request.urlopen", explode)
    assert fetch_track_metadata(TRACK_ID) is None
    assert resolve_title(TRACK_URL) is None


# ---------------------------------------------------------------------------
# Naming (roadmap A2)
# ---------------------------------------------------------------------------


def test_resolve_title_names_the_job_after_the_track(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_metadata(monkeypatch, _token_then(TRACK_PAYLOAD))
    assert resolve_title(TRACK_URL) == "Zaylevelten - Tease Me"


def test_resolve_title_without_an_artist_is_just_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_metadata(monkeypatch, _token_then({"name": "Lone Demo"}))
    assert resolve_title(TRACK_URL) == "Lone Demo"


def test_resolve_title_sanitizes_illegal_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_metadata(
        monkeypatch,
        _token_then(
            {
                "name": 'AC/DC: Back\x00 in Black  "live"',
                "artists": [{"name": "AC/DC"}],
            }
        ),
    )
    name = resolve_title(TRACK_URL)
    # Filesystem-illegal characters become spaces, whitespace collapses, and
    # the artist prefix is kept: "AC/DC" cannot survive as a filename.
    assert name == "AC DC - AC DC Back in Black live"


def test_resolve_title_caps_a_very_long_name(monkeypatch: pytest.MonkeyPatch) -> None:
    stub_metadata(monkeypatch, _token_then({"name": "x" * 500}))
    name = resolve_title(TRACK_URL)
    assert name is not None
    assert len(name) <= 80


def test_unusable_name_falls_back_to_the_track_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_metadata(monkeypatch, _token_then({"name": "///"}))
    assert resolve_title(TRACK_URL) is None


def test_disallowed_links_are_never_looked_up(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = stub_metadata(monkeypatch, _token_then(TRACK_PAYLOAD))
    assert resolve_title("https://open.spotify.com/playlist/" + TRACK_ID) is None
    assert resolve_title("https://open.spotify.com.evil.com/track/" + TRACK_ID) is None
    assert calls == []


# ---------------------------------------------------------------------------
# Failure surface for later milestones
# ---------------------------------------------------------------------------


def test_unavailable_error_distinguishes_disabled_from_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "0")
    disabled = unavailable_error()
    assert disabled.code == ErrorCode.DOWNLOAD_FAILED
    assert "disabled" in str(disabled)

    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "1")
    unconfigured = unavailable_error()
    assert unconfigured.code == ErrorCode.DOWNLOAD_FAILED
    assert "not configured" in str(unconfigured)


# ---------------------------------------------------------------------------
# Operator login and credential location (milestone S2.5)
# ---------------------------------------------------------------------------


def test_credentials_path_defaults_under_the_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv(CREDENTIALS_FILE_ENV, raising=False)
    monkeypatch.setenv("STEMIFY_DATA_DIR", str(tmp_path))
    assert credentials_path() == tmp_path / DEFAULT_CREDENTIALS_FILENAME


def test_credentials_path_honors_the_operator_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    override = tmp_path / "elsewhere" / "login.json"
    monkeypatch.setenv(CREDENTIALS_FILE_ENV, str(override))
    assert credentials_path() == override


def test_credentials_file_reports_the_same_location_the_login_writes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """One path, not two guesses: the fetch reads exactly where the login wrote."""
    target = tmp_path / "creds.json"
    monkeypatch.setenv(CREDENTIALS_FILE_ENV, str(target))
    assert credentials_file() is None  # not logged in yet
    target.write_text("{}", encoding="utf-8")
    assert credentials_file() == credentials_path() == target


def _block_librespot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `import librespot` fail, whatever is installed on the machine."""
    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "librespot" or name.startswith("librespot."):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)


def install_fake_login(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception | None = None,
    write_credentials: bool = True,
) -> dict[str, Any]:
    """Inject a minimal client library that records how the login configured it."""
    seen: dict[str, Any] = {}

    class _ConfigurationBuilder:
        def __init__(self) -> None:
            self.settings: dict[str, Any] = {}

        def set_store_credentials(self, value: Any) -> Any:
            self.settings["store_credentials"] = value
            return self

        def set_stored_credential_file(self, value: Any) -> Any:
            self.settings["stored_credentials_file"] = value
            return self

        def set_cache_dir(self, value: Any) -> Any:
            self.settings["cache_dir"] = value
            return self

        def build(self) -> Any:
            return SimpleNamespace(**self.settings)

    class _Builder:
        def __init__(self, configuration: Any) -> None:
            seen["configuration"] = configuration

        def oauth(self, callback: Any) -> Any:
            seen["callback"] = callback
            return self

        def create(self) -> Any:
            if error is not None:
                raise error
            seen["callback"]("https://accounts.spotify.com/authorize?fake=1")
            if write_credentials:
                # What the real library does on a successful authenticate():
                # write the reusable credentials to the configured file.
                Path(seen["configuration"].stored_credentials_file).write_text(
                    '{"username": "operator", "credentials": "SECRET-BLOB", "type": 1}',
                    encoding="utf-8",
                )
            return SimpleNamespace(close=lambda: seen.__setitem__("closed", True))

    session = SimpleNamespace(
        Builder=_Builder,
        Configuration=SimpleNamespace(Builder=_ConfigurationBuilder),
    )
    core = ModuleType("librespot.core")
    core.Session = session  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "librespot", ModuleType("librespot"))
    monkeypatch.setitem(sys.modules, "librespot.core", core)
    return seen


def test_login_without_the_client_library_points_at_the_install_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "creds.json"
    monkeypatch.setenv(CREDENTIALS_FILE_ENV, str(target))
    _block_librespot(monkeypatch)

    with pytest.raises(SpotifyError) as excinfo:
        login(open_browser=False)

    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED
    assert "requirements-spotify.txt" in str(excinfo.value)
    assert not target.exists()


def test_login_writes_where_the_fetch_reads_and_never_prints_the_secret(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seen = install_fake_login(monkeypatch)
    target = tmp_path / "data" / DEFAULT_CREDENTIALS_FILENAME
    monkeypatch.setenv(CREDENTIALS_FILE_ENV, str(target))

    path = login(open_browser=False)

    assert path == target
    assert target.is_file()
    configuration = seen["configuration"]
    assert configuration.store_credentials is True
    assert configuration.stored_credentials_file == str(target)
    # The session cache must not land in the process cwd (the client library's
    # default) or a fetch would litter stream data next to the source.
    assert configuration.cache_dir == str(target.parent / CACHE_DIR_NAME)
    assert seen.get("closed") is True

    captured = capsys.readouterr()
    assert "accounts.spotify.com/authorize" in captured.out  # the operator gets the URL
    assert "SECRET-BLOB" not in captured.out + captured.err


def test_login_reports_a_failed_sign_in(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_fake_login(monkeypatch, error=RuntimeError("bad login"))
    target = tmp_path / "creds.json"
    monkeypatch.setenv(CREDENTIALS_FILE_ENV, str(target))

    with pytest.raises(SpotifyError) as excinfo:
        login(open_browser=False)

    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED
    assert "sign-in failed" in str(excinfo.value)
    assert not target.exists()


def test_login_refuses_an_empty_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A flow that returns without storing anything must not look like success."""
    install_fake_login(monkeypatch, write_credentials=False)
    target = tmp_path / "creds.json"
    monkeypatch.setenv(CREDENTIALS_FILE_ENV, str(target))

    with pytest.raises(SpotifyError) as excinfo:
        login(open_browser=False)

    assert excinfo.value.code == ErrorCode.DOWNLOAD_FAILED
    assert not target.exists()
    assert "no credentials" in str(excinfo.value)


# ---------------------------------------------------------------------------
# CLI wiring (milestone S2.5)
# ---------------------------------------------------------------------------


def test_cli_exposes_spotify_login() -> None:
    from worker.cli import build_parser

    args = build_parser().parse_args(["spotify-login", "--no-browser"])
    assert args.no_browser is True
    assert callable(args.func)


def test_spotify_login_command_reports_where_credentials_went(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from worker import cli

    target = tmp_path / "creds.json"
    seen: dict[str, Any] = {}

    def fake_login(*, open_browser: bool = True) -> Path:
        seen["open_browser"] = open_browser
        return target

    monkeypatch.setattr("worker.spotify.login", fake_login)
    code = cli.command_spotify_login(argparse.Namespace(no_browser=True))

    assert code == 0
    assert seen["open_browser"] is False  # --no-browser is honored
    assert str(target) in capsys.readouterr().out


def test_spotify_login_command_fails_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from worker import cli

    def failing_login(**kwargs: Any) -> Path:
        raise SpotifyError(ErrorCode.DOWNLOAD_FAILED, "the Spotify client library is not installed")

    monkeypatch.setattr("worker.spotify.login", failing_login)
    code = cli.command_spotify_login(argparse.Namespace(no_browser=True))

    captured = capsys.readouterr()
    assert code == 1
    assert "DOWNLOAD_FAILED" in captured.err
    assert "Traceback" not in captured.err


def test_health_reports_what_spotify_input_is_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from worker import cli

    assert cli._spotify_status({"librespot": False}).startswith("unavailable")

    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "0")
    assert "disabled" in cli._spotify_status({"librespot": True})

    monkeypatch.setenv("STEMIFY_SPOTIFY_ENABLED", "1")
    monkeypatch.setenv(CREDENTIALS_FILE_ENV, str(tmp_path / "missing.json"))
    assert "signed out" in cli._spotify_status({"librespot": True})

    present = tmp_path / "creds.json"
    present.write_text("{}", encoding="utf-8")
    monkeypatch.setenv(CREDENTIALS_FILE_ENV, str(present))
    assert cli._spotify_status({"librespot": True}) == "ready"
