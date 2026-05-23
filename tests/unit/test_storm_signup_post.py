"""
Tests for storm_signup_post.py (#124).

The slash command itself is integration territory (needs a live
interaction). These tests cover the pure-function helpers — time-label
rendering and registration-embed construction.
"""

import datetime as _dt
from unittest.mock import MagicMock, patch

import pytest

import storm_signup_post as ssp

from tests.unit.test_config import TEST_GUILD_ID


class TestSlotLabels:
    """_slot_labels is now team-ordered (#251). Returns the labels for
    Team A first, Team B second, driven by `team_a_slot_index` /
    `team_b_slot_index` on `guild_storm_config` (or a per-event
    override from `storm_registration_posts`). Empty strings come back
    when the alliance hasn't picked a slot for that team yet — the
    sign-up post creation flow uses that as the "missing_slot_labels"
    signal."""

    def test_returns_empty_when_no_mapping_set(self, seeded_db):
        """Without team-slot picks, both labels are empty (the gate
        for `missing_slot_labels` in `post_registration`)."""
        a, b = ssp._slot_labels("DS", guild_id=TEST_GUILD_ID)
        assert a == ""
        assert b == ""

    def test_returns_team_ordered_labels_after_mapping_saved(self, seeded_db):
        """Saved mapping picks slot 1 for Team A and slot 2 for Team B —
        labels come back in TEAM order matching the picks."""
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, 2)
        slot_labels = config.get_storm_slot_labels("DS", TEST_GUILD_ID)
        a, b = ssp._slot_labels("DS", guild_id=TEST_GUILD_ID)
        assert a == slot_labels[0]
        assert b == slot_labels[1]
        assert "server time" in a
        assert "server time" in b

    def test_both_teams_can_share_a_slot(self, seeded_db):
        """When both teams pick the same slot, both labels return the
        same string — the invariant 3-button SignupView layout still
        renders, just with identical Team A and Team B labels."""
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "CS", 2, 2)
        slot_labels = config.get_storm_slot_labels("CS", TEST_GUILD_ID)
        a, b = ssp._slot_labels("CS", guild_id=TEST_GUILD_ID)
        assert a == slot_labels[1]
        assert b == slot_labels[1]
        assert a == b

    def test_override_pins_label_for_single_render(self, seeded_db):
        """Override indices win over the saved guild default — the
        per-week officer-pick path for the `📣 Post sign-up poll`
        confirmation flow."""
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, 2)
        slot_labels = config.get_storm_slot_labels("DS", TEST_GUILD_ID)
        # Override Team A to slot 2 for this render only.
        a, b = ssp._slot_labels(
            "DS", guild_id=TEST_GUILD_ID,
            override_a_idx=2, override_b_idx=2,
        )
        assert a == slot_labels[1]
        assert b == slot_labels[1]

    def test_partial_override_fills_other_team_from_default(self, seeded_db):
        """Only Team A overridden — Team B's label still comes from the
        saved default. Lets the override flow ask only the running team
        without blanking the unchanged side."""
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, 2)
        slot_labels = config.get_storm_slot_labels("DS", TEST_GUILD_ID)
        a, b = ssp._slot_labels(
            "DS", guild_id=TEST_GUILD_ID,
            override_a_idx=2,  # only override A
        )
        assert a == slot_labels[1]
        assert b == slot_labels[1]  # saved B = slot 2

    def test_unknown_event_returns_safe_default(self, seeded_db):
        a, b = ssp._slot_labels("XX", guild_id=TEST_GUILD_ID)
        # Helper falls through gracefully — never crashes.
        assert isinstance(a, str)
        assert isinstance(b, str)


class TestRegistrationEmbed:
    def test_embed_has_event_name_and_date(self):
        embed = ssp._build_registration_embed("DS", "2026-05-18", "9pm ET", "4pm ET")
        # Title should mention Desert Storm and the date.
        assert "Desert Storm" in embed.title
        assert "May" in embed.title or "2026" in embed.title

    def test_embed_describes_vote_rules(self):
        """Per Kevin's first-sweep _edited preference, the embed body
        is the simpler 'Select your availability!' + vote-replacement
        disclaimer. The slot times live on the buttons (SignupView),
        not in the embed body."""
        embed = ssp._build_registration_embed("DS", "2026-05-18", "9pm ET", "4pm ET")
        assert "Select your availability for Desert Storm" in embed.description
        assert "Only 1 vote can be recorded" in embed.description
        assert "replace the first vote" in embed.description

    def test_embed_does_not_include_time_field(self):
        """The slot labels are rendered on the SignupView's buttons
        only — they don't appear in the embed body or fields anymore.
        Drop confirms Kevin's first-sweep preferred shape."""
        embed = ssp._build_registration_embed("DS", "2026-05-18", "9pm ET", "4pm ET")
        # No fields at all on the post-realignment embed.
        assert embed.fields == []
        body = embed.description or ""
        assert "9pm ET" not in body
        assert "4pm ET" not in body

    def test_embed_does_not_include_footer(self):
        """Kevin's first-sweep preferred shape drops the "Vote
        recorded with timestamp — leadership uses /<parent> signups"
        footer. Officers learn about the signups command from setup
        prose + the walkthrough tour."""
        embed = ssp._build_registration_embed("DS", "2026-05-18", "9pm ET", "4pm ET")
        assert embed.footer.text is None or embed.footer.text == ""

    def test_cs_uses_orange_color(self):
        # Just a sanity check that the two events have distinct visual
        # treatments. Not strictly required, but loud-failure surface.
        ds = ssp._build_registration_embed("DS", "2026-05-18", "9pm ET", "4pm ET")
        cs = ssp._build_registration_embed("CS", "2026-05-18", "10am ET", "9pm ET")
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


class TestRegistrationEmbedTeamsGate:
    """The teams-gate now lives on `SignupView` (button rendering),
    not the embed body — Kevin's first-sweep _edited preferred shape
    keeps the embed minimal. These tests confirm the embed accepts
    every `teams` value without crashing; the actual `teams` gating
    of which buttons render lives in `test_storm_signup_view.py`."""

    @pytest.mark.parametrize("teams", ["both", "A", "B"])
    def test_embed_accepts_teams_arg_without_emitting_times(self, teams):
        embed = ssp._build_registration_embed(
            "DS", "2026-05-18", "9pm ET", "4pm ET", teams=teams,
        )
        body = (embed.description or "") + "\n".join(
            f.value or "" for f in embed.fields
        )
        assert "9pm ET" not in body
        assert "4pm ET" not in body
        assert "Select your availability for Desert Storm" in embed.description

    @pytest.mark.parametrize("teams", ["both", "A", "B"])
    def test_cs_embed_accepts_teams_arg(self, teams):
        embed = ssp._build_registration_embed(
            "CS", "2026-05-18", "10am ET", "9pm ET", teams=teams,
        )
        body = (embed.description or "") + "\n".join(
            f.value or "" for f in embed.fields
        )
        assert "10am ET" not in body
        assert "9pm ET" not in body
        assert "Select your availability for Canyon Storm" in embed.description
