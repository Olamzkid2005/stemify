"""Spotify plan milestone S1: link policy, kill switch and metadata probe.

Pure unit tests, no network: the module's single network seam
(`worker.spotify._request_json`) is stubbed, and the tests that must prove
"nothing leaves the machine" assert that the seam was never called at all.
Audio acquisition (librespot) is milestone S2 and is not covered here.
"""

from __future__ import annotations

import base64
import urllib.error
from typing import Any

import pytest

from worker.errors import ErrorCode
from worker.spotify import (
    DEFAULT_HTTP_TIMEOUT_SECONDS,
    TrackMetadata,
    fetch_track_metadata,
    is_allowed_spotify_url,
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
