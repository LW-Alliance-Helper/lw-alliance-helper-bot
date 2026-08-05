"""Sentry noise filter (#377, #378, #416).

The point of these is that the filter stays *narrow*. Dropping too much is a
silent failure mode nobody notices until a real regression goes unreported, so
roughly half of these assert that ordinary errors still get through.
"""

from __future__ import annotations

import discord
import pytest

import sentry_filter


def _hint(exc: BaseException) -> dict:
    """A before_send hint shaped the way sentry_sdk builds it."""
    return {"exc_info": (type(exc), exc, exc.__traceback__)}


def _raised(exc: BaseException) -> BaseException:
    """Give ``exc`` a real traceback, as it would have in production."""
    try:
        raise exc
    except BaseException as e:  # noqa: BLE001 - re-raised shape is the point
        return e


def _connection_closed(code: int) -> discord.ConnectionClosed:
    """ConnectionClosed's signature has moved around across discord.py versions."""
    exc = discord.ConnectionClosed.__new__(discord.ConnectionClosed)
    Exception.__init__(exc, f"Shard ID None WebSocket closed with {code}")
    exc.code = code
    exc.reason = ""
    exc.shard_id = None
    return exc


class TestDropped:
    def test_login_failure_is_dropped(self):
        """#377: a rejected token is a deploy problem, not a code bug."""
        assert sentry_filter.drop_reason(discord.LoginFailure("bad token")) == "login-rejected"

    def test_gateway_4004_is_dropped(self):
        """#378: same root cause as #377, reported down the gateway path."""
        assert sentry_filter.drop_reason(_connection_closed(4004)) == "gateway-auth-rejected"

    def test_discord_5xx_is_dropped(self):
        """#416: Discord's outage. discord.py does not retry a 503."""
        resp = type("R", (), {"status": 503, "reason": "Service Unavailable"})()
        exc = discord.DiscordServerError(resp, "upstream connect error")
        assert sentry_filter.drop_reason(exc) == "discord-5xx"

    def test_login_failure_found_through_the_cause_chain(self):
        """discord.py raises LoginFailure *from* the 401, and which one the SDK
        reports has changed between versions."""
        try:
            try:
                raise ValueError("401 Unauthorized")
            except ValueError as inner:
                raise discord.LoginFailure("Improper token has been passed.") from inner
        except discord.LoginFailure as outer:
            wrapper = RuntimeError("wrapped")
            wrapper.__cause__ = outer
            assert sentry_filter.drop_reason(wrapper) == "login-rejected"


class TestKept:
    def test_ordinary_exception_is_kept(self):
        assert sentry_filter.drop_reason(ValueError("a real bug")) is None

    def test_none_is_kept(self):
        """A non-exception event (capture_message) has no exc_info to inspect."""
        assert sentry_filter.drop_reason(None) is None

    def test_gateway_close_other_than_4004_is_kept(self):
        """4000 and 4008 are ours to look at; only 4004 is an auth rejection."""
        assert sentry_filter.drop_reason(_connection_closed(4000)) is None

    def test_forbidden_is_kept(self):
        """403 is a permissions bug we can act on, not an upstream blip."""
        resp = type("R", (), {"status": 403, "reason": "Forbidden"})()
        assert sentry_filter.drop_reason(discord.Forbidden(resp, "Missing Permissions")) is None

    def test_plain_http_401_is_kept(self):
        """Only a 401 that became a LoginFailure is boot noise. A 401 anywhere
        else is a real problem, so matching on status alone would over-drop."""
        resp = type("R", (), {"status": 401, "reason": "Unauthorized"})()
        assert sentry_filter.drop_reason(discord.HTTPException(resp, "401: Unauthorized")) is None

    def test_not_found_is_kept(self):
        resp = type("R", (), {"status": 404, "reason": "Not Found"})()
        assert sentry_filter.drop_reason(discord.NotFound(resp, "Unknown Channel")) is None


class TestBeforeSend:
    def test_drops_by_returning_none(self):
        event = {"event_id": "abc"}
        exc = _raised(discord.LoginFailure("bad token"))
        assert sentry_filter.before_send(event, _hint(exc)) is None

    def test_passes_the_event_through_untouched(self):
        event = {"event_id": "abc"}
        exc = _raised(ValueError("a real bug"))
        assert sentry_filter.before_send(event, _hint(exc)) is event

    def test_event_without_exc_info_is_kept(self):
        """capture_message events have no exception attached."""
        event = {"event_id": "abc", "message": "something happened"}
        assert sentry_filter.before_send(event, {}) is event
        assert sentry_filter.before_send(event, None) is event

    def test_a_broken_hint_still_sends(self):
        """Failing open matters: a hook that throws costs the event and the
        diagnosis of why it went missing."""
        event = {"event_id": "abc"}

        class Exploding:
            def get(self, _key, _default=None):
                raise RuntimeError("boom")

        assert sentry_filter.before_send(event, Exploding()) is event

    def test_fatal_drop_is_logged_loudly(self, caplog):
        """Sentry no longer sees a dead bot, so the log has to."""
        exc = _raised(discord.LoginFailure("bad token"))
        with caplog.at_level("DEBUG", logger="sentry_filter"):
            sentry_filter.before_send({}, _hint(exc))
        assert any(r.levelname == "CRITICAL" for r in caplog.records)
        assert "DISCORD_TOKEN" in caplog.text

    def test_transient_drop_is_logged_quietly(self, caplog):
        resp = type("R", (), {"status": 503, "reason": "Service Unavailable"})()
        exc = _raised(discord.DiscordServerError(resp, "upstream connect error"))
        with caplog.at_level("DEBUG", logger="sentry_filter"):
            sentry_filter.before_send({}, _hint(exc))
        assert [r.levelname for r in caplog.records] == ["WARNING"]


def test_bot_wires_the_filter_into_sentry_init():
    """The filter is only worth anything if it's actually installed. Guards
    against the init and this module drifting apart."""
    source = (__import__("pathlib").Path(sentry_filter.__file__).parent / "bot.py").read_text(
        encoding="utf-8"
    )
    assert "before_send=sentry_before_send" in source
    assert "from sentry_filter import before_send as sentry_before_send" in source


if __name__ == "__main__":
    pytest.main([__file__])
