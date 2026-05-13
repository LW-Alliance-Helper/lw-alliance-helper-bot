"""
Tests for storm_signup_post.py (#124).

The slash command itself is integration territory (needs a live
interaction). These tests cover the pure-function helpers — time-label
rendering and registration-embed construction.
"""

import datetime as _dt

import storm_signup_post as ssp


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
