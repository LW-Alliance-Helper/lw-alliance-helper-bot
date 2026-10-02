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
            "DS",
            guild_id=TEST_GUILD_ID,
            override_a_idx=2,
            override_b_idx=2,
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
            "DS",
            guild_id=TEST_GUILD_ID,
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


class TestPostSignupUsesTheServerDay:
    """#726: a storm is a game day, so the manual post's "is this in the
    past?" check runs on the server day, like the roster builder."""

    async def _run(self, event_date):
        from unittest.mock import AsyncMock

        inter = MagicMock()
        inter.guild_id = TEST_GUILD_ID
        inter.response.send_message = AsyncMock()
        gate = AsyncMock(return_value=(False, None))
        with (
            patch("time_helpers.server_today", return_value=_dt.date(2026, 5, 14)),
            patch("storm_permissions.is_leader_or_admin", return_value=True),
            patch("storm_permissions.ensure_premium_structured", gate),
        ):
            await ssp.handle_post_signup(MagicMock(), inter, "DS", event_date)
        return inter.response.send_message, gate

    @pytest.mark.asyncio
    async def test_the_server_day_itself_is_not_past(self):
        sent, gate = await self._run("2026-05-14")
        sent.assert_not_awaited()
        gate.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_the_day_before_the_server_day_is_past(self):
        sent, gate = await self._run("2026-05-13")
        assert "is in the past" in sent.await_args.args[0]
        gate.assert_not_awaited()


class TestRegistrationEmbedTeamsGate:
    """The teams-gate now lives on `SignupView` (button rendering),
    not the embed body — Kevin's first-sweep _edited preferred shape
    keeps the embed minimal. These tests confirm the embed accepts
    every `teams` value without crashing; the actual `teams` gating
    of which buttons render lives in `test_storm_signup_view.py`."""

    @pytest.mark.parametrize("teams", ["both", "A", "B"])
    def test_embed_accepts_teams_arg_without_emitting_times(self, teams):
        embed = ssp._build_registration_embed(
            "DS",
            "2026-05-18",
            "9pm ET",
            "4pm ET",
            teams=teams,
        )
        body = (embed.description or "") + "\n".join(f.value or "" for f in embed.fields)
        assert "9pm ET" not in body
        assert "4pm ET" not in body
        assert "Select your availability for Desert Storm" in embed.description

    @pytest.mark.parametrize("teams", ["both", "A", "B"])
    def test_cs_embed_accepts_teams_arg(self, teams):
        embed = ssp._build_registration_embed(
            "CS",
            "2026-05-18",
            "10am ET",
            "9pm ET",
            teams=teams,
        )
        body = (embed.description or "") + "\n".join(f.value or "" for f in embed.fields)
        assert "10am ET" not in body
        assert "9pm ET" not in body
        assert "Select your availability for Canyon Storm" in embed.description


class TestPostRegistrationForceFlag:
    """#265: `force=True` lets leadership re-post a sign-up message even
    when one already exists for the event. `force=False` (default) keeps
    the auto-scheduler tick idempotent so the daily fire doesn't drop a
    duplicate post on top of one it already created."""

    def _guild_with_channel(self, gid=TEST_GUILD_ID, channel_id=555):
        """Stub guild that owns a stub channel — enough for
        `post_registration` to reach the `has_registration_post` guard."""
        from unittest.mock import AsyncMock

        channel = MagicMock()
        channel.send = AsyncMock(return_value=MagicMock(id=999_000))
        guild = MagicMock()
        guild.id = gid
        guild.get_channel = MagicMock(return_value=channel)
        return guild, channel

    @pytest.mark.asyncio
    async def test_default_returns_already_posted_when_one_exists(self, seeded_db):
        """Scheduler-style call (no `force=`) skips when a row exists."""
        import config

        # Configure storm + Team A/B slots so we'd otherwise get past
        # `missing_slot_labels`. The `has_registration_post` check fires
        # before slot resolution though, so we don't strictly need it —
        # but seeding makes the test resilient if the guard moves later.
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, 2)
        config.save_structured_storm_config(
            TEST_GUILD_ID,
            "DS",
            structured_flow_enabled=True,
            signup_channel_id=555,
        )
        config.record_storm_registration_post(
            TEST_GUILD_ID,
            "DS",
            "2026-05-29",
            channel_id=555,
            message_id=4001,
        )
        guild, channel = self._guild_with_channel()
        result = await ssp.post_registration(
            bot=MagicMock(),
            guild=guild,
            event_type="DS",
            event_date="2026-05-29",
        )
        assert result["status"] == "already_posted"
        # No new message went out — channel.send was never called.
        channel.send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_true_bypasses_guard_and_posts_again(self, seeded_db):
        """Leadership-triggered repost (force=True) sends a fresh
        message and records a second row — votes still aggregate on
        the same (guild, event_type, event_date) on storm_signups."""
        import config

        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, 2)
        config.save_structured_storm_config(
            TEST_GUILD_ID,
            "DS",
            structured_flow_enabled=True,
            signup_channel_id=555,
        )
        config.record_storm_registration_post(
            TEST_GUILD_ID,
            "DS",
            "2026-05-29",
            channel_id=555,
            message_id=4001,
        )
        guild, channel = self._guild_with_channel()
        # Stub the sent-message id so the recorder writes a second row.
        sent_message = MagicMock()
        sent_message.id = 7777
        from unittest.mock import AsyncMock

        channel.send = AsyncMock(return_value=sent_message)
        result = await ssp.post_registration(
            bot=MagicMock(),
            guild=guild,
            event_type="DS",
            event_date="2026-05-29",
            force=True,
        )
        assert result["status"] == "ok"
        channel.send.assert_awaited_once()
        # Both message_ids land in the recent-posts list — the
        # persistent-View re-registration path will re-attach handlers
        # to every live message on startup.
        recents = config.get_recent_storm_registration_posts(within_days=365)
        msg_ids = {r["message_id"] for r in recents if r["event_date"] == "2026-05-29"}
        assert msg_ids == {4001, 7777}
