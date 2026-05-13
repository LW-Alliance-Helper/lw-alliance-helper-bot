"""
Tests for storm_signup_post.py (#124).

The slash command itself is integration territory (needs a live
interaction). These tests cover the pure-function helpers — time-label
rendering and registration-embed construction.
"""

import datetime as _dt
from unittest.mock import MagicMock, patch

import storm_signup_post as ssp

from tests.unit.test_config import TEST_GUILD_ID


class TestSlotLabels:
    """_slot_labels delegates to config.get_storm_slot_labels (already
    covered in detail by its own tests). These verify the wiring."""

    def test_ds_returns_two_non_empty_labels(self):
        a, b = ssp._slot_labels("DS", guild_id=12345)
        # Both labels should include the "server time" annotation that
        # config.format_storm_slot adds.
        assert a and b
        assert "server time" in a
        assert "server time" in b

    def test_cs_returns_only_first_label(self):
        a, b = ssp._slot_labels("CS", guild_id=12345)
        assert a
        assert b == ""

    def test_unknown_event_returns_safe_default(self):
        a, b = ssp._slot_labels("XX", guild_id=12345)
        # Helper falls through gracefully — never crashes.
        assert isinstance(a, str)
        assert isinstance(b, str)


class TestRegistrationEmbed:
    def test_embed_has_event_name_and_date(self):
        embed = ssp._build_registration_embed("DS", "2026-05-18", "9pm ET", "4pm ET")
        # Title should mention Desert Storm and the date.
        assert "Desert Storm" in embed.title
        assert "May" in embed.title or "2026" in embed.title

    def test_embed_includes_both_time_options_for_ds(self):
        embed = ssp._build_registration_embed("DS", "2026-05-18", "9pm ET", "4pm ET")
        # Description or fields should reference both times.
        body = "\n".join([embed.description or ""] +
                         [f.value or "" for f in embed.fields])
        assert "9pm ET" in body
        assert "4pm ET" in body

    def test_embed_skips_empty_time_options(self):
        embed = ssp._build_registration_embed("CS", "2026-05-18", "9pm ET", "")
        body = "\n".join([f.value or "" for f in embed.fields])
        assert "9pm ET" in body
        # No stray empty bullet
        assert "**\n" not in body

    def test_cs_uses_orange_color(self):
        # Just a sanity check that the two events have distinct visual
        # treatments. Not strictly required, but loud-failure surface.
        ds = ssp._build_registration_embed("DS", "2026-05-18", "9pm ET", "4pm ET")
        cs = ssp._build_registration_embed("CS", "2026-05-18", "9pm ET", "")
        assert ds.color != cs.color


class TestTodayInGuildTz:
    """Past-date validation should compare against TODAY in the alliance's
    configured timezone, not the host's. Railway is UTC, so an east-of-UTC
    alliance entering their event date near midnight their time would
    otherwise see the date flagged as already past."""

    def test_uses_guild_timezone_when_configured(self, seeded_db):
        import config
        cfg = config.get_config(TEST_GUILD_ID)
        cfg.timezone = "America/New_York"
        config.save_config(cfg)

        # Pin "now" to a fixed UTC instant — 03:00 UTC on May 13 — and
        # verify the helper resolves to May 12 in ET (23:00 the prior
        # day). The helper calls `_dt.datetime.now(tz)`, so the mock
        # converts the fixed UTC instant into the requested tz.
        fixed_utc = _dt.datetime(2026, 5, 13, 3, 0, tzinfo=_dt.timezone.utc)

        class _FrozenDatetime:
            @staticmethod
            def now(tz):
                return fixed_utc.astimezone(tz)

        with patch("storm_signup_post._dt") as mock_dt:
            mock_dt.datetime = _FrozenDatetime
            mock_dt.timezone = _dt.timezone
            mock_dt.date = _dt.date
            result = ssp._today_in_guild_tz(TEST_GUILD_ID)
        assert result == _dt.date(2026, 5, 12)

    def test_falls_back_to_utc_when_no_timezone(self, seeded_db):
        import config
        cfg = config.get_config(TEST_GUILD_ID)
        cfg.timezone = ""
        config.save_config(cfg)
        result = ssp._today_in_guild_tz(TEST_GUILD_ID)
        assert result == _dt.datetime.now(_dt.timezone.utc).date()

    def test_falls_back_to_utc_when_guild_id_missing(self, seeded_db):
        result = ssp._today_in_guild_tz(None)
        assert result == _dt.datetime.now(_dt.timezone.utc).date()

    def test_invalid_tz_string_does_not_crash(self, seeded_db):
        import config
        cfg = config.get_config(TEST_GUILD_ID)
        cfg.timezone = "Not/A_Real_Zone"
        config.save_config(cfg)
        # Should fall through to UTC, not raise ZoneInfoNotFoundError.
        result = ssp._today_in_guild_tz(TEST_GUILD_ID)
        assert isinstance(result, _dt.date)
