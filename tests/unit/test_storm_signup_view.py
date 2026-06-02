"""
Tests for the SignupView persistence layer (#123).

Covers the custom_id encoder / parser and the View construction (every
button gets a stable custom_id under Discord's 100-char limit). The
actual click handler is integration territory — its data path is covered
by `test_record_vote_round_trip` in test_config.py.
"""

from unittest.mock import MagicMock

import pytest

import storm_signup_view as sv

from tests.unit.test_config import TEST_GUILD_ID


def _vote_buttons(view):
    """Return only the vote buttons on a SignupView (filters out the
    leadership 'View sign-ups' button added in #258)."""
    return [c for c in view.children if (c.custom_id or "").startswith("signup:")]


def _vote_codes(view):
    """Parse vote codes off every vote button on a SignupView."""
    return [sv.parse_custom_id(c.custom_id)["vote"] for c in _vote_buttons(view)]


class TestCustomIdEncoding:
    def test_round_trip(self):
        cid = sv.make_custom_id(12345, "DS", "2026-05-18", "a")
        parsed = sv.parse_custom_id(cid)
        assert parsed == {
            "guild_id": 12345,
            "event_type": "ds",
            "event_date": "2026-05-18",
            "vote": "a",
        }

    def test_event_type_normalised_lowercase(self):
        # encoder lowercases; parser only accepts lowercase
        assert sv.make_custom_id(1, "CS", "2026-01-01", "either").endswith(":either")
        parsed = sv.parse_custom_id("signup:1:cs:2026-01-01:either")
        assert parsed["event_type"] == "cs"

    def test_all_valid_votes_accepted(self):
        for v in ("a", "b", "either", "cannot"):
            cid = sv.make_custom_id(1, "DS", "2026-01-01", v)
            assert sv.parse_custom_id(cid)["vote"] == v

    def test_unknown_event_type_rejected(self):
        # Parser rejects unknown event_types to keep dispatch tight.
        assert sv.parse_custom_id("signup:1:xx:2026-01-01:a") is None

    def test_unknown_vote_rejected(self):
        assert sv.parse_custom_id("signup:1:ds:2026-01-01:bogus") is None

    def test_malformed_returns_none(self):
        for bad in (
            "",
            "signup",
            "signup:foo",
            "signup:not_int:ds:2026-01-01:a",
            "other:1:ds:2026-01-01:a",
            "signup:1:ds:2026-01-01:a:extra",
        ):
            assert sv.parse_custom_id(bad) is None

    def test_custom_id_under_discord_limit(self):
        # Discord caps custom_id at 100 chars. Worst realistic case is a
        # 19-digit guild_id (Snowflake ceiling).
        cid = sv.make_custom_id(9999999999999999999, "DS", "2026-12-31", "either")
        assert len(cid) <= 100


class TestSignupViewConstruction:
    def test_four_buttons_with_stable_custom_ids(self):
        view = sv.SignupView(
            12345, "DS", "2026-05-18", time_a_label="9pm ET", time_b_label="4pm ET"
        )
        # 4 vote buttons + 1 leadership "View sign-ups" button (#258)
        assert len(view.children) == 5
        # Each vote button has a parseable custom_id
        vote_buttons = [c for c in view.children if (c.custom_id or "").startswith("signup:")]
        votes_found = set()
        for btn in vote_buttons:
            parsed = sv.parse_custom_id(btn.custom_id)
            assert parsed is not None
            assert parsed["guild_id"] == 12345
            assert parsed["event_type"] == "ds"
            assert parsed["event_date"] == "2026-05-18"
            votes_found.add(parsed["vote"])
        # Cover all four vote codes
        assert votes_found == {"a", "b", "either", "cannot"}

    def test_persistent_view_timeout_is_none(self):
        # discord.py requires timeout=None for persistent Views; this is
        # what makes the View survive bot restarts.
        view = sv.SignupView(1, "DS", "2026-05-18")
        assert view.timeout is None

    def test_default_labels_when_not_provided(self):
        view = sv.SignupView(1, "DS", "2026-05-18")
        # We don't pin exact strings (those are UX copy), just that the
        # configurable A/B buttons reference the defaults.
        labels = [c.label for c in view.children]
        assert any("Team A" in lab for lab in labels)
        assert any("Team B" in lab for lab in labels)

    def test_custom_labels_propagate(self):
        view = sv.SignupView(1, "DS", "2026-05-18", time_a_label="9pm ET", time_b_label="4pm ET")
        labels = [c.label for c in view.children]
        assert any("9pm ET" in lab for lab in labels)
        assert any("4pm ET" in lab for lab in labels)

    def test_cs_default_renders_four_buttons(self):
        """Rule A / #166: CS supports teams=both/A/B just like DS. With
        the default teams="both" CS renders all 4 vote buttons (a, b,
        either, cannot) plus the leadership view button."""
        view = sv.SignupView(12345, "CS", "2026-05-18")
        codes = sorted(_vote_codes(view))
        assert codes == ["a", "b", "cannot", "either"]

    def test_cs_force_all_buttons_renders_four(self):
        """`_force_all_buttons=True` keeps all 4 button handlers
        registered so a pre-hotfix CS post (which has all 4 buttons
        already rendered in Discord) stays clickable after a bot
        restart — discord.py routes by custom_id matching."""
        view = sv.SignupView(12345, "CS", "2026-05-18", _force_all_buttons=True)
        codes = sorted(_vote_codes(view))
        assert codes == ["a", "b", "cannot", "either"]

    def test_force_all_buttons_noop_for_ds(self):
        """DS always has all 4 vote buttons regardless of the flag."""
        view_default = sv.SignupView(1, "DS", "2026-05-18")
        view_forced = sv.SignupView(1, "DS", "2026-05-18", _force_all_buttons=True)
        assert len(_vote_buttons(view_default)) == 4
        assert len(_vote_buttons(view_forced)) == 4

    def test_ds_teams_a_renders_a_plus_cannot(self):
        """#148 single-team DS: alliance opted into Team A only.
        SignupView renders A + Cannot vote buttons (no B / Either)."""
        view = sv.SignupView(1, "DS", "2026-05-18", teams="A")
        assert sorted(_vote_codes(view)) == ["a", "cannot"]

    def test_ds_teams_b_renders_b_plus_cannot(self):
        view = sv.SignupView(1, "DS", "2026-05-18", teams="B")
        assert sorted(_vote_codes(view)) == ["b", "cannot"]

    def test_ds_teams_both_renders_all_four(self):
        """`teams="both"` is the default and matches the current behaviour."""
        view = sv.SignupView(1, "DS", "2026-05-18", teams="both")
        assert len(_vote_buttons(view)) == 4

    def test_force_all_buttons_overrides_single_team_setting(self):
        """Re-registration always reattaches all 4 vote-button handlers
        regardless of the current `teams` setting — a pre-config-change
        post may have all 4 buttons rendered, and the View needs
        routes for every persisted custom_id."""
        view = sv.SignupView(1, "DS", "2026-05-18", teams="A", _force_all_buttons=True)
        assert len(_vote_buttons(view)) == 4

    def test_cs_respects_teams_setting(self):
        """Rule A / #166: CS reads `teams` like DS. teams=A gates the
        view to a + cannot only; teams=B gates to b + cannot."""
        view = sv.SignupView(1, "CS", "2026-05-18", teams="A")
        assert sorted(_vote_codes(view)) == ["a", "cannot"]
        view = sv.SignupView(1, "CS", "2026-05-18", teams="B")
        assert sorted(_vote_codes(view)) == ["b", "cannot"]

    def test_garbage_teams_value_falls_back_to_both(self):
        """A schema-drift sentinel: if `teams` reads as something other
        than the three valid values, the View falls back to the
        permissive default rather than rendering zero vote buttons."""
        view = sv.SignupView(1, "DS", "2026-05-18", teams="garbage")
        assert len(_vote_buttons(view)) == 4

    def test_empty_time_label_renders_bare_team_name(self):
        """The doubled-label bug: when `time_a_label=""`, the button
        should render as `🅰️ Team A`, not `🅰️ Team A: ` (trailing
        colon) and definitely not `🅰️ Team A: Team A`."""
        view = sv.SignupView(1, "DS", "2026-05-18", time_a_label="", time_b_label="")
        labels = [c.label for c in view.children]
        assert any(lab == "🅰️ Team A" for lab in labels), f"expected bare '🅰️ Team A' in {labels}"
        assert any(lab == "🅱️ Team B" for lab in labels), f"expected bare '🅱️ Team B' in {labels}"
        # Nothing renders as a doubled label.
        assert not any("Team A: Team A" in lab for lab in labels)
        assert not any("Team B: Team B" in lab for lab in labels)


class TestCsStaleVoteReject:
    """Single-team CS alliances (teams=A or teams=B) reject votes for
    the other team — same defense-in-depth shape as the DS guard.
    Pre-Rule-A behaviour where CS *always* rejected b/either is
    obsolete; CS with teams=both now accepts every vote like DS."""

    @pytest.mark.asyncio
    async def test_cs_b_vote_rejected_when_team_a_only(self, seeded_db):
        from unittest.mock import AsyncMock, MagicMock, patch
        import config

        # Seed CS config with teams=A so the b vote is invalid.
        config.save_storm_config(
            TEST_GUILD_ID,
            "CS",
            tab_name="CS Tab",
            mail_template="",
            timezone="America/New_York",
            log_channel_id=0,
            teams="A",
        )
        cid = sv.make_custom_id(TEST_GUILD_ID, "CS", "2026-05-18", "b")
        interaction = MagicMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.data = {"custom_id": cid}
        interaction.user.id = 42
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=True)),
            patch("config.record_storm_vote") as record,
        ):
            await sv._handle_signup_click(interaction, "b")
        # The polite reject fires BEFORE record_storm_vote.
        record.assert_not_called()
        interaction.response.send_message.assert_awaited_once()
        body = interaction.response.send_message.await_args.args[0]
        assert "Team A only" in body

    @pytest.mark.asyncio
    async def test_cs_either_vote_rejected_when_single_team(self, seeded_db):
        from unittest.mock import AsyncMock, MagicMock, patch
        import config

        config.save_storm_config(
            TEST_GUILD_ID,
            "CS",
            tab_name="CS Tab",
            mail_template="",
            timezone="America/New_York",
            log_channel_id=0,
            teams="A",
        )
        cid = sv.make_custom_id(TEST_GUILD_ID, "CS", "2026-05-18", "either")
        interaction = MagicMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.data = {"custom_id": cid}
        interaction.user.id = 42
        interaction.response.send_message = AsyncMock()
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=True)),
            patch("config.record_storm_vote") as record,
        ):
            await sv._handle_signup_click(interaction, "either")
        record.assert_not_called()

    @pytest.mark.asyncio
    async def test_cs_a_vote_proceeds(self, seeded_db):
        """Sanity — `a` and `cannot` on CS still route through to the
        record path."""
        from unittest.mock import AsyncMock, MagicMock, patch

        cid = sv.make_custom_id(TEST_GUILD_ID, "CS", "2026-05-18", "a")
        interaction = MagicMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.data = {"custom_id": cid}
        interaction.user.id = 42
        interaction.channel_id = 0
        interaction.message = None
        interaction.client = MagicMock()
        interaction.user.display_name = "Alice"
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.guild = None  # short-circuits the chunk pre-pass
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=True)),
            patch("storm_signup_view._mirror_vote_to_sheet"),
            patch("storm_signup_view._maybe_send_power_refresh_dm", new=AsyncMock()),
            patch("config.record_storm_vote") as record,
        ):
            await sv._handle_signup_click(interaction, "a")
        record.assert_called_once()

    @pytest.mark.asyncio
    async def test_ds_b_vote_still_proceeds(self, seeded_db):
        """The reject only fires on CS — DS Team B remains valid."""
        from unittest.mock import AsyncMock, MagicMock, patch

        cid = sv.make_custom_id(TEST_GUILD_ID, "DS", "2026-05-18", "b")
        interaction = MagicMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.data = {"custom_id": cid}
        interaction.user.id = 42
        interaction.channel_id = 0
        interaction.message = None
        interaction.client = MagicMock()
        interaction.user.display_name = "Alice"
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.guild = None
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=True)),
            patch("storm_signup_view._mirror_vote_to_sheet"),
            patch("storm_signup_view._maybe_send_power_refresh_dm", new=AsyncMock()),
            patch("config.record_storm_vote") as record,
        ):
            await sv._handle_signup_click(interaction, "b")
        record.assert_called_once()


class TestDsSingleTeamStaleVoteReject:
    """#148 single-team DS: a `teams=A` alliance with a stale 4-button
    post in the channel should reject Team B / Either clicks. Same
    shape as the CS reject — defense-in-depth for the config-drift
    case where the alliance flipped `teams` after a post went live."""

    @pytest.fixture
    def env_teams_a(self, seeded_db):
        import config

        config.save_storm_config(
            TEST_GUILD_ID,
            "DS",
            tab_name="DS Tab",
            mail_template="",
            timezone="America/New_York",
            log_channel_id=0,
            teams="A",
        )
        return TEST_GUILD_ID

    @pytest.fixture
    def env_teams_b(self, seeded_db):
        import config

        config.save_storm_config(
            TEST_GUILD_ID,
            "DS",
            tab_name="DS Tab",
            mail_template="",
            timezone="America/New_York",
            log_channel_id=0,
            teams="B",
        )
        return TEST_GUILD_ID

    def _fake_interaction(self, vote: str, event_type: str = "DS"):
        from unittest.mock import AsyncMock, MagicMock

        cid = sv.make_custom_id(TEST_GUILD_ID, event_type, "2026-05-18", vote)
        interaction = MagicMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.data = {"custom_id": cid}
        interaction.user.id = 42
        interaction.channel_id = 0
        interaction.message = None
        interaction.client = MagicMock()
        interaction.user.display_name = "Alice"
        interaction.response.send_message = AsyncMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.guild = None
        return interaction

    @pytest.mark.asyncio
    async def test_teams_a_rejects_team_b_vote(self, env_teams_a):
        from unittest.mock import AsyncMock, patch

        inter = self._fake_interaction("b")
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=True)),
            patch("config.record_storm_vote") as record,
        ):
            await sv._handle_signup_click(inter, "b")
        record.assert_not_called()
        body = inter.response.send_message.await_args.args[0]
        assert "Team A only" in body

    @pytest.mark.asyncio
    async def test_teams_a_rejects_either_vote(self, env_teams_a):
        from unittest.mock import AsyncMock, patch

        inter = self._fake_interaction("either")
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=True)),
            patch("config.record_storm_vote") as record,
        ):
            await sv._handle_signup_click(inter, "either")
        record.assert_not_called()

    @pytest.mark.asyncio
    async def test_teams_a_accepts_team_a_vote(self, env_teams_a):
        from unittest.mock import AsyncMock, patch

        inter = self._fake_interaction("a")
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=True)),
            patch("storm_signup_view._mirror_vote_to_sheet"),
            patch("storm_signup_view._maybe_send_power_refresh_dm", new=AsyncMock()),
            patch("config.record_storm_vote") as record,
        ):
            await sv._handle_signup_click(inter, "a")
        record.assert_called_once()

    @pytest.mark.asyncio
    async def test_teams_a_accepts_cannot_vote(self, env_teams_a):
        from unittest.mock import AsyncMock, patch

        inter = self._fake_interaction("cannot")
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=True)),
            patch("storm_signup_view._mirror_vote_to_sheet"),
            patch("storm_signup_view._maybe_send_power_refresh_dm", new=AsyncMock()),
            patch("config.record_storm_vote") as record,
        ):
            await sv._handle_signup_click(inter, "cannot")
        record.assert_called_once()

    @pytest.mark.asyncio
    async def test_teams_b_rejects_team_a_vote(self, env_teams_b):
        from unittest.mock import AsyncMock, patch

        inter = self._fake_interaction("a")
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=True)),
            patch("config.record_storm_vote") as record,
        ):
            await sv._handle_signup_click(inter, "a")
        record.assert_not_called()
        body = inter.response.send_message.await_args.args[0]
        assert "Team B only" in body

    @pytest.mark.asyncio
    async def test_teams_b_rejects_either_vote(self, env_teams_b):
        from unittest.mock import AsyncMock, patch

        inter = self._fake_interaction("either")
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=True)),
            patch("config.record_storm_vote") as record,
        ):
            await sv._handle_signup_click(inter, "either")
        record.assert_not_called()

    @pytest.mark.asyncio
    async def test_teams_b_accepts_team_b_vote(self, env_teams_b):
        from unittest.mock import AsyncMock, patch

        inter = self._fake_interaction("b")
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=True)),
            patch("storm_signup_view._mirror_vote_to_sheet"),
            patch("storm_signup_view._maybe_send_power_refresh_dm", new=AsyncMock()),
            patch("config.record_storm_vote") as record,
        ):
            await sv._handle_signup_click(inter, "b")
        record.assert_called_once()

    @pytest.mark.asyncio
    async def test_teams_both_accepts_all_votes(self, seeded_db):
        """teams=both (default) preserves the original 4-vote behaviour."""
        import config

        config.save_storm_config(
            TEST_GUILD_ID,
            "DS",
            tab_name="DS Tab",
            mail_template="",
            timezone="America/New_York",
            log_channel_id=0,
            teams="both",
        )
        from unittest.mock import AsyncMock, patch

        for vote in ("a", "b", "either", "cannot"):
            inter = self._fake_interaction(vote)
            with (
                patch("premium.is_premium", new=AsyncMock(return_value=True)),
                patch("storm_signup_view._mirror_vote_to_sheet"),
                patch("storm_signup_view._maybe_send_power_refresh_dm", new=AsyncMock()),
                patch("config.record_storm_vote") as record,
            ):
                await sv._handle_signup_click(inter, vote)
            record.assert_called_once(), f"vote {vote} should record"


class TestSignupHistoryAudit:
    """The UPSERT in `record_storm_vote` overwrites the prior row in
    `storm_signups`, so the audit trail for re-votes (especially the
    on-behalf path required by #38) lives in `storm_signup_history`."""

    def test_self_vote_appends_history_row(self, seeded_db):
        import config

        config.record_storm_vote(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            voter_user_id=42,
            target_member_id="42",
            vote="a",
        )
        history = config.get_storm_signup_history(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
        )
        assert len(history) == 1
        assert history[0]["voter_user_id"] == 42
        assert history[0]["vote"] == "a"
        assert history[0]["is_on_behalf"] is False
        assert history[0]["voted_at"].endswith("+00:00")

    def test_revote_preserves_prior_vote_in_history(self, seeded_db):
        import config

        # First vote — A
        config.record_storm_vote(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            voter_user_id=42,
            target_member_id="42",
            vote="a",
        )
        # Officer overrides — B
        config.record_storm_vote(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            voter_user_id=999,
            target_member_id="42",
            vote="b",
            is_on_behalf=True,
        )
        # Member changes their mind — Either
        config.record_storm_vote(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            voter_user_id=42,
            target_member_id="42",
            vote="either",
        )
        history = config.get_storm_signup_history(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            target_member_id="42",
        )
        # All three votes preserved, newest first.
        assert [h["vote"] for h in history] == ["either", "b", "a"]
        # Officer's intervention is intact.
        officer_entry = next(h for h in history if h["voter_user_id"] == 999)
        assert officer_entry["is_on_behalf"] is True

    def test_storm_signups_holds_only_latest(self, seeded_db):
        """Sanity-check the contract: the current row in `storm_signups`
        reflects the latest vote; full history lives in the audit table."""
        import config

        config.record_storm_vote(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            voter_user_id=42,
            target_member_id="42",
            vote="a",
        )
        config.record_storm_vote(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            voter_user_id=42,
            target_member_id="42",
            vote="cannot",
        )
        current = config.get_member_vote(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            "42",
        )
        assert current["vote"] == "cannot"
        history = config.get_storm_signup_history(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
        )
        assert len(history) == 2


class TestPersistentViewRegistration:
    """Bot-restart-resiliency: `register_persistent_signup_views` walks
    every recent registration post and rebinds a SignupView to its
    `message_id`. Without this, every post-restart button click fails
    with "Interaction failed."""

    def test_walks_recent_posts_and_calls_add_view(self, seeded_db):
        import datetime as _dt

        import config

        # Two posts comfortably inside the 14-day window. Dates are
        # computed relative to UTC today (the basis used by
        # `get_recent_storm_registration_posts`) so they never age out;
        # 2 and 9 days back keep both clear of the local-vs-UTC boundary
        # skew at the cutoff edge.
        _utc_today = _dt.datetime.now(_dt.timezone.utc).date()
        d_recent = (_utc_today - _dt.timedelta(days=2)).isoformat()
        d_older = (_utc_today - _dt.timedelta(days=9)).isoformat()
        config.record_storm_registration_post(
            TEST_GUILD_ID,
            "DS",
            d_older,
            channel_id=100,
            message_id=200,
            time_a_label="9pm ET",
            time_b_label="4pm ET",
        )
        config.record_storm_registration_post(
            TEST_GUILD_ID,
            "CS",
            d_recent,
            channel_id=100,
            message_id=300,
            time_a_label="12pm ET",
        )

        bot = MagicMock()
        added = sv.register_persistent_signup_views(bot)
        assert added == 2
        # add_view called once per post, keyed by message_id.
        message_ids = {call.kwargs.get("message_id") for call in bot.add_view.call_args_list}
        assert message_ids == {200, 300}
        # Each registered View carries the configured labels.
        for call in bot.add_view.call_args_list:
            view = call.args[0]
            assert isinstance(view, sv.SignupView)
            assert view.timeout is None

    def test_add_view_failure_is_logged_not_raised(self, seeded_db):
        import datetime as _dt

        import config

        # In-window date (relative to UTC today) so the post is actually
        # walked and `add_view` fires — otherwise this test passes
        # vacuously (count == 0 because nothing was in range) and stops
        # exercising the add_view-raises path it was written to cover.
        _utc_today = _dt.datetime.now(_dt.timezone.utc).date()
        d_recent = (_utc_today - _dt.timedelta(days=2)).isoformat()
        config.record_storm_registration_post(
            TEST_GUILD_ID,
            "DS",
            d_recent,
            channel_id=100,
            message_id=200,
        )
        bot = MagicMock()
        bot.add_view.side_effect = RuntimeError("simulated")
        # Should not raise even when add_view blows up.
        count = sv.register_persistent_signup_views(bot)
        assert count == 0


class TestPowerRefreshDmNudge:
    """Audit gap: `_maybe_send_power_refresh_dm` was uncovered. These
    tests pin the new INSERT-first ordering (race-tight), the
    HTTPException back-out (so transient errors retry), and the
    cooldown short-circuit (no Sheet read on re-votes)."""

    @pytest.fixture
    def env(self, seeded_db):
        """Premium-flag-on guild with the power-refresh DM toggle on,
        plus a roster fixture where the voter (`42`) has unparseable
        power so the nudge is in scope."""
        import config

        config.save_storm_config(
            TEST_GUILD_ID,
            "DS",
            tab_name="DS Tab",
            mail_template="",
            timezone="America/New_York",
            log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID,
            "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_refresh_dm_enabled=True,
        )
        return TEST_GUILD_ID

    def _fake_interaction(self, *, send_raises=None, user_id=42):
        from unittest.mock import AsyncMock

        inter = MagicMock()
        inter.guild = MagicMock()
        inter.guild.id = TEST_GUILD_ID
        inter.user = MagicMock()
        inter.user.id = user_id
        if send_raises is None:
            inter.user.send = AsyncMock()
        else:
            inter.user.send = AsyncMock(side_effect=send_raises)
        return inter

    def _patch_roster(self, voter_power):
        from unittest.mock import patch

        return patch(
            "storm_roster_builder._read_roster_powers",
            return_value=(
                {
                    "42": {
                        "key": "42",
                        "name": "Alice",
                        "discord_id": "42",
                        "power": voter_power,
                        "not_on_discord": False,
                    }
                },
                [],
            ),
        )

    @pytest.mark.asyncio
    async def test_disabled_flag_short_circuits(self, env, seeded_db):
        import config

        config.save_structured_storm_config(
            TEST_GUILD_ID,
            "DS",
            structured_flow_enabled=True,
            power_refresh_dm_enabled=False,
        )
        inter = self._fake_interaction()
        await sv._maybe_send_power_refresh_dm(
            inter,
            env,
            "DS",
            "2026-05-18",
            42,
        )
        inter.user.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_voter_not_on_roster_skipped(self, env):
        from unittest.mock import patch

        inter = self._fake_interaction()
        with patch(
            "storm_roster_builder._read_roster_powers",
            return_value=({}, []),
        ):
            await sv._maybe_send_power_refresh_dm(
                inter,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        inter.user.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_power_parseable_no_dm(self, env):
        inter = self._fake_interaction()
        with self._patch_roster(voter_power=412_000_000):
            await sv._maybe_send_power_refresh_dm(
                inter,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        inter.user.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_power_unparseable_sends_dm_and_records(self, env):
        import config

        inter = self._fake_interaction()
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        inter.user.send.assert_awaited_once()
        body = inter.user.send.await_args.args[0]
        # Post-Rule C (#165): DM body uses a generic "power value"
        # phrase rather than naming the column header — the column is
        # configured as a letter, not a header string.
        assert "power value" in body.lower()
        # Cooldown row recorded — subsequent re-vote is silent.
        assert config.has_power_refresh_dm_been_sent(
            env,
            "DS",
            "2026-05-18",
            42,
        )

    @pytest.mark.asyncio
    async def test_cooldown_prevents_double_dm_on_revote(self, env):
        inter1 = self._fake_interaction()
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter1,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        assert inter1.user.send.await_count == 1
        # Second re-vote → cooldown short-circuits, no second DM.
        inter2 = self._fake_interaction()
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter2,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        inter2.user.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_forbidden_keeps_cooldown_row(self, env):
        """DM-Forbidden = member's DMs are off; don't retry on every
        re-vote."""
        import config
        import discord as _d

        inter = self._fake_interaction(
            send_raises=_d.Forbidden(
                response=MagicMock(status=403),
                message="DMs disabled",
            ),
        )
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        # Cooldown is recorded so we don't hit the Sheet read again.
        assert config.has_power_refresh_dm_been_sent(
            env,
            "DS",
            "2026-05-18",
            42,
        )

    @pytest.mark.asyncio
    async def test_httpexception_backs_out_cooldown(self, env):
        """Transient HTTP error = the DM didn't land; back out the
        cooldown row so the next re-vote retries. Audit Major M1."""
        import config
        import discord as _d

        inter = self._fake_interaction(
            send_raises=_d.HTTPException(
                response=MagicMock(status=503),
                message="Service unavailable",
            ),
        )
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        # Cooldown was backed out — next click can retry.
        assert not config.has_power_refresh_dm_been_sent(
            env,
            "DS",
            "2026-05-18",
            42,
        )

    @pytest.mark.asyncio
    async def test_simultaneous_clicks_only_one_dms(self, env):
        """Audit Major M2: two clicks within the same second both pass
        the SELECT before either INSERTs. INSERT-first ordering means
        only the first sees a fresh row → only the first DMs."""
        from unittest.mock import patch

        inter_a = self._fake_interaction()
        inter_b = self._fake_interaction()

        # Drive both serially but they each see the same starting
        # state (no cooldown row yet). The first INSERT-first wins.
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter_a,
                env,
                "DS",
                "2026-05-18",
                42,
            )
            await sv._maybe_send_power_refresh_dm(
                inter_b,
                env,
                "DS",
                "2026-05-18",
                42,
            )

        # Exactly one DM was sent.
        assert inter_a.user.send.await_count == 1
        assert inter_b.user.send.await_count == 0


class TestStalePowerDmNudge:
    """#255 — when `power_refresh_stale_days > 0` and the source is
    configured, the nudge fires when the voter's power value is still
    parseable but older than the threshold. Cooldown is shared with
    the existing missing-power nudge."""

    @pytest.fixture
    def env(self, seeded_db):
        """Premium-flag-on guild with both the master toggle and stale
        check on, threshold 7 days, source configured."""
        import config

        config.save_storm_config(
            TEST_GUILD_ID,
            "DS",
            tab_name="DS Tab",
            mail_template="",
            timezone="America/New_York",
            log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID,
            "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_refresh_dm_enabled=True,
            power_last_updated_tab="Squad Powers",
            power_last_updated_column="C",
            power_refresh_stale_days=7,
        )
        return TEST_GUILD_ID

    def _fake_interaction(self, *, send_raises=None, user_id=42):
        from unittest.mock import AsyncMock

        inter = MagicMock()
        inter.guild = MagicMock()
        inter.guild.id = TEST_GUILD_ID
        inter.user = MagicMock()
        inter.user.id = user_id
        if send_raises is None:
            inter.user.send = AsyncMock()
        else:
            inter.user.send = AsyncMock(side_effect=send_raises)
        return inter

    def _patch_roster(self, *, voter_power, voter_last_updated):
        """Patch the roster reader to return a single member whose
        power is parseable but whose `last_updated` lets us drive the
        stale check."""
        from unittest.mock import patch

        return patch(
            "storm_roster_builder._read_roster_powers",
            return_value=(
                {
                    "42": {
                        "key": "42",
                        "name": "Alice",
                        "discord_id": "42",
                        "power": voter_power,
                        "not_on_discord": False,
                        "last_updated": voter_last_updated,
                    }
                },
                [],
            ),
        )

    @pytest.mark.asyncio
    async def test_stale_power_sends_dm(self, env):
        """Power is parseable but the timestamp is older than 7 days
        → DM fires. Body names the days-stale figure."""
        import datetime as _dt
        import config

        inter = self._fake_interaction()
        old = _dt.date.today() - _dt.timedelta(days=30)
        with self._patch_roster(
            voter_power=412_000_000,
            voter_last_updated=old,
        ):
            await sv._maybe_send_power_refresh_dm(
                inter,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        inter.user.send.assert_awaited_once()
        body = inter.user.send.await_args.args[0]
        assert "30" in body
        assert "days ago" in body.lower()
        # Cooldown shared with the missing-power path.
        assert config.has_power_refresh_dm_been_sent(
            env,
            "DS",
            "2026-05-18",
            42,
        )

    @pytest.mark.asyncio
    async def test_fresh_power_no_dm(self, env):
        """Power present + timestamp inside the threshold → no DM."""
        import datetime as _dt

        inter = self._fake_interaction()
        fresh = _dt.date.today() - _dt.timedelta(days=2)
        with self._patch_roster(
            voter_power=412_000_000,
            voter_last_updated=fresh,
        ):
            await sv._maybe_send_power_refresh_dm(
                inter,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        inter.user.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_exactly_at_threshold_sends_dm(self, env):
        """Boundary: a value that's exactly `stale_days` old should
        DM. The comparison is inclusive (>=)."""
        import datetime as _dt

        inter = self._fake_interaction()
        edge = _dt.date.today() - _dt.timedelta(days=7)
        with self._patch_roster(
            voter_power=412_000_000,
            voter_last_updated=edge,
        ):
            await sv._maybe_send_power_refresh_dm(
                inter,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        inter.user.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_last_updated_skipped_silently(self, env):
        """Power is present, source configured, but this member's
        row didn't have a parseable timestamp → silent skip. We do
        NOT punish members for an alliance-side data-quality issue."""
        inter = self._fake_interaction()
        with self._patch_roster(
            voter_power=412_000_000,
            voter_last_updated=None,
        ):
            await sv._maybe_send_power_refresh_dm(
                inter,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        inter.user.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_stale_check_disabled_when_days_zero(self, env, seeded_db):
        """`power_refresh_stale_days = 0` disables the stale branch
        regardless of last_updated. The missing-power branch still
        runs as before (covered in TestPowerRefreshDmNudge)."""
        import config
        import datetime as _dt

        config.save_structured_storm_config(
            TEST_GUILD_ID,
            "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_refresh_dm_enabled=True,
            power_last_updated_tab="Squad Powers",
            power_last_updated_column="C",
            power_refresh_stale_days=0,  # disabled
        )
        inter = self._fake_interaction()
        old = _dt.date.today() - _dt.timedelta(days=30)
        with self._patch_roster(
            voter_power=412_000_000,
            voter_last_updated=old,
        ):
            await sv._maybe_send_power_refresh_dm(
                inter,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        inter.user.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_missing_power_takes_priority_over_stale_check(self, env):
        """When power is None AND a last_updated is set, the missing
        branch fires (not the stale branch). DM copy should match the
        missing-power wording, not the stale-days wording."""
        import datetime as _dt

        inter = self._fake_interaction()
        old = _dt.date.today() - _dt.timedelta(days=30)
        with self._patch_roster(
            voter_power=None,
            voter_last_updated=old,
        ):
            await sv._maybe_send_power_refresh_dm(
                inter,
                env,
                "DS",
                "2026-05-18",
                42,
            )
        inter.user.send.assert_awaited_once()
        body = inter.user.send.await_args.args[0]
        # The "days ago" phrasing belongs to the stale branch — must
        # not appear when the trigger reason is missing power.
        assert "days ago" not in body.lower()


class TestClearPowerRefreshDmSent:
    """The new `clear_power_refresh_dm_sent` helper backs out a row
    after a transient HTTPException."""

    def test_round_trip(self, seeded_db):
        import config

        config.record_power_refresh_dm_sent(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            42,
        )
        assert config.has_power_refresh_dm_been_sent(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            42,
        )
        ok = config.clear_power_refresh_dm_sent(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            42,
        )
        assert ok
        assert not config.has_power_refresh_dm_been_sent(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            42,
        )

    def test_clear_nothing_is_safe(self, seeded_db):
        import config

        # Clearing a non-existent row is a no-op, not an exception.
        assert not config.clear_power_refresh_dm_sent(
            TEST_GUILD_ID,
            "DS",
            "2026-05-18",
            999,
        )


# ── #258 + #259 — poll-style ack, leadership view button, DM ack ─────────────


class TestViewSignupsCustomId:
    """The 'View sign-ups' button (#258) uses a distinct custom_id
    prefix so the existing vote-button parser doesn't have to special-
    case a non-vote action."""

    def test_round_trip(self):
        cid = sv.make_view_signups_custom_id(12345, "DS", "2026-05-18")
        parsed = sv.parse_view_signups_custom_id(cid)
        assert parsed == {
            "guild_id": 12345,
            "event_type": "ds",
            "event_date": "2026-05-18",
        }

    def test_distinct_prefix_from_vote_custom_id(self):
        view_cid = sv.make_view_signups_custom_id(1, "DS", "2026-05-18")
        vote_cid = sv.make_custom_id(1, "DS", "2026-05-18", "a")
        # Cross-parsing must fail in both directions so the click
        # dispatcher can't accidentally route one as the other.
        assert sv.parse_custom_id(view_cid) is None
        assert sv.parse_view_signups_custom_id(vote_cid) is None

    def test_unknown_event_type_rejected(self):
        assert sv.parse_view_signups_custom_id("signup_view:1:xx:2026-01-01") is None

    def test_malformed_returns_none(self):
        for bad in (
            "",
            "signup_view",
            "signup_view:not_int:ds:2026-01-01",
            "other:1:ds:2026-01-01",
            "signup_view:1:ds:2026-01-01:extra",
        ):
            assert sv.parse_view_signups_custom_id(bad) is None

    def test_under_discord_limit(self):
        # Snowflake ceiling (19-digit guild_id) + longest event_type + ISO date.
        cid = sv.make_view_signups_custom_id(9999999999999999999, "DS", "2026-12-31")
        assert len(cid) <= 100


class TestSignupViewLeadershipButton:
    """The 'View sign-ups' button must always appear on every SignupView
    regardless of single-team / `_force_all_buttons` configuration, so
    leadership can pull the breakdown from any sign-up post without
    leaving the channel."""

    def test_view_signups_button_present_on_default_view(self):
        view = sv.SignupView(1, "DS", "2026-05-18")
        view_buttons = [c for c in view.children if (c.custom_id or "").startswith("signup_view:")]
        assert len(view_buttons) == 1
        btn = view_buttons[0]
        assert "View sign-ups" in btn.label
        # Renders on row=1 so it doesn't crowd the vote buttons.
        assert btn.row == 1

    def test_view_signups_button_present_on_single_team(self):
        # teams=A renders 2 vote buttons + the leadership button.
        view = sv.SignupView(1, "DS", "2026-05-18", teams="A")
        assert len(_vote_buttons(view)) == 2
        leadership = [c for c in view.children if (c.custom_id or "").startswith("signup_view:")]
        assert len(leadership) == 1

    def test_view_signups_button_custom_id_encodes_post_identity(self):
        view = sv.SignupView(12345, "CS", "2026-05-18")
        leadership = next(
            c for c in view.children if (c.custom_id or "").startswith("signup_view:")
        )
        parsed = sv.parse_view_signups_custom_id(leadership.custom_id)
        assert parsed == {
            "guild_id": 12345,
            "event_type": "cs",
            "event_date": "2026-05-18",
        }


class TestPollStyleAck:
    """#258 — vote click sends an ephemeral poll-style embed showing
    total votes per bucket with the voter's bucket marked ✓."""

    def _record(self, gid, vote, voter_id, *, et="DS", date="2026-05-18"):
        import config

        config.record_storm_vote(
            gid,
            et,
            date,
            voter_user_id=voter_id,
            target_member_id=str(voter_id),
            vote=vote,
            is_on_behalf=False,
            channel_id=0,
            message_id=0,
        )

    def test_first_vote_shows_solo_count(self, seeded_db):
        gid = TEST_GUILD_ID
        self._record(gid, "a", 100)
        embed = sv._render_vote_poll_embed(
            gid,
            "DS",
            "2026-05-18",
            voter_vote="a",
            voter_vote_label="Team A",
        )
        assert "Team A" in embed.title
        assert "✅" in embed.title
        assert "**Total votes:** 1" in embed.description

    def test_marks_voter_bucket_with_checkmark(self, seeded_db):
        gid = TEST_GUILD_ID
        self._record(gid, "a", 100)
        self._record(gid, "b", 101)
        embed = sv._render_vote_poll_embed(
            gid,
            "DS",
            "2026-05-18",
            voter_vote="a",
            voter_vote_label="Team A: 9pm ET",
        )
        # The ✓ marker sits on the voter's row inside the code block.
        # Strip the code-block fences and find the Team A line.
        body = embed.description
        team_a_lines = [line for line in body.split("\n") if line.startswith("Team A")]
        assert team_a_lines, body
        assert "✓" in team_a_lines[0]
        team_b_lines = [line for line in body.split("\n") if line.startswith("Team B")]
        assert team_b_lines, body
        assert "✓" not in team_b_lines[0]

    def test_total_sums_every_bucket(self, seeded_db):
        gid = TEST_GUILD_ID
        for i, vote in enumerate(("a", "a", "b", "either", "cannot")):
            self._record(gid, vote, 100 + i)
        embed = sv._render_vote_poll_embed(
            gid,
            "DS",
            "2026-05-18",
            voter_vote="cannot",
            voter_vote_label="Cannot participate",
        )
        assert "**Total votes:** 5" in embed.description

    def test_single_team_a_excludes_b_and_either(self, seeded_db):
        """teams=A renders only Team A + Can't in the embed bars — Team B
        / Either don't apply on single-team alliances and would confuse
        members otherwise."""
        gid = TEST_GUILD_ID
        self._record(gid, "a", 100)
        embed = sv._render_vote_poll_embed(
            gid,
            "DS",
            "2026-05-18",
            voter_vote="a",
            voter_vote_label="Team A",
            teams_setting="A",
        )
        body = embed.description
        assert "Team A" in body
        assert "Can't" in body
        assert "Team B" not in body
        assert "Either" not in body

    def test_single_team_b_excludes_a_and_either(self, seeded_db):
        gid = TEST_GUILD_ID
        self._record(gid, "b", 100)
        embed = sv._render_vote_poll_embed(
            gid,
            "DS",
            "2026-05-18",
            voter_vote="b",
            voter_vote_label="Team B",
            teams_setting="B",
        )
        body = embed.description
        assert "Team B" in body
        assert "Can't" in body
        assert "Team A" not in body
        assert "Either" not in body

    def test_zero_count_bucket_renders_without_blocks(self, seeded_db):
        """A bucket with no votes renders as a label + count `0` (no
        bar blocks). Keeps the layout consistent regardless of which
        buckets have activity."""
        gid = TEST_GUILD_ID
        self._record(gid, "a", 100)
        embed = sv._render_vote_poll_embed(
            gid,
            "DS",
            "2026-05-18",
            voter_vote="a",
            voter_vote_label="Team A",
        )
        body = embed.description
        team_b_lines = [line for line in body.split("\n") if line.startswith("Team B")]
        assert team_b_lines
        # 0 count rendered with no bar blocks.
        assert "█" not in team_b_lines[0]
        assert "0" in team_b_lines[0]

    def test_cs_uses_orange_color(self, seeded_db):
        """Color matches the event type so members on multi-event
        alliances can distinguish DS ack (gold) from CS ack (orange)."""
        gid = TEST_GUILD_ID
        self._record(gid, "a", 100, et="CS")
        embed = sv._render_vote_poll_embed(
            gid,
            "CS",
            "2026-05-18",
            voter_vote="a",
            voter_vote_label="Team A",
        )
        import discord

        assert embed.color == discord.Color.orange()


class TestHandleViewSignupsClick:
    """#258 — leadership-only click handler for the View sign-ups
    button. Non-leadership gets a polite ephemeral rejection (the
    button is visible to everyone because Discord can't hide per-user)."""

    def _interaction(self, *, is_leader: bool):
        from unittest.mock import AsyncMock

        inter = MagicMock()
        inter.guild_id = TEST_GUILD_ID
        inter.guild = MagicMock()
        inter.guild.id = TEST_GUILD_ID
        member = MagicMock()
        member.guild_permissions.administrator = is_leader
        # Cast as Member instance so storm_permissions.is_leader_or_admin
        # doesn't bail on the DMs path.
        import discord

        member.__class__ = discord.Member
        inter.user = member
        inter.response.is_done.return_value = False
        inter.response.send_message = AsyncMock()
        inter.response.defer = AsyncMock()
        inter.followup.send = AsyncMock()
        return inter

    @pytest.mark.asyncio
    async def test_non_leader_gets_rejection_ephemeral(self, seeded_db):
        inter = self._interaction(is_leader=False)
        await sv._handle_view_signups_click(
            inter,
            TEST_GUILD_ID,
            "ds",
            "2026-05-18",
        )
        inter.response.send_message.assert_awaited_once()
        body = inter.response.send_message.await_args.args[0]
        assert "Leadership only" in body
        # The leadership-only short-circuit MUST NOT defer or call
        # `followup.send`. Reserving the interaction with `defer` would
        # leave the user staring at a `thinking...` chip even though
        # we already know we're going to reject the click.
        inter.response.defer.assert_not_called()
        inter.followup.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_leader_gets_breakdown_embed(self, seeded_db):
        from unittest.mock import patch

        inter = self._interaction(is_leader=True)
        fake_embed = MagicMock()
        # Patch the two officer-view helpers — we don't need to drive
        # the full bucket-build pipeline here, just confirm the handler
        # delegates to them and sends the resulting embed.
        with (
            patch("storm_officer_view._build_bucket_map", return_value=({"a": []}, [])),
            patch("storm_officer_view._render_embed", return_value=fake_embed),
        ):
            await sv._handle_view_signups_click(
                inter,
                TEST_GUILD_ID,
                "ds",
                "2026-05-18",
            )
        inter.response.defer.assert_awaited_once()
        inter.followup.send.assert_awaited_once()
        kwargs = inter.followup.send.await_args.kwargs
        assert kwargs.get("embed") is fake_embed
        assert kwargs.get("ephemeral") is True


class TestPowerDmAckPrefix:
    """#259 — the power-refresh DM (and any other error DM in the
    signup flow) leads with '✅ Your vote was recorded.' so members
    don't mistake the error for a failed vote."""

    def _env(self):
        import config

        config.save_storm_config(
            TEST_GUILD_ID,
            "DS",
            tab_name="DS Tab",
            mail_template="",
            timezone="America/New_York",
            log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID,
            "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_refresh_dm_enabled=True,
        )
        return TEST_GUILD_ID

    def _interaction(self):
        from unittest.mock import AsyncMock

        inter = MagicMock()
        inter.guild = MagicMock()
        inter.guild.id = TEST_GUILD_ID
        inter.user = MagicMock()
        inter.user.id = 42
        inter.user.send = AsyncMock()
        return inter

    def _patch_roster(self, voter_power):
        from unittest.mock import patch

        return patch(
            "storm_roster_builder._read_roster_powers",
            return_value=(
                {
                    "42": {
                        "key": "42",
                        "name": "Alice",
                        "discord_id": "42",
                        "power": voter_power,
                        "not_on_discord": False,
                    }
                },
                [],
            ),
        )

    @pytest.mark.asyncio
    async def test_dm_body_leads_with_vote_recorded(self, seeded_db):
        gid = self._env()
        inter = self._interaction()
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter,
                gid,
                "DS",
                "2026-05-18",
                42,
            )
        inter.user.send.assert_awaited_once()
        body = inter.user.send.await_args.args[0]
        # The first non-blank line acknowledges the vote.
        assert body.startswith("✅ Your vote was recorded."), body

    @pytest.mark.asyncio
    async def test_header_variant_also_leads_with_vote_recorded(self, seeded_db):
        from unittest.mock import patch

        gid = self._env()
        inter = self._interaction()
        with (
            self._patch_roster(voter_power=None),
            patch("storm_roster_builder._read_power_column_header", return_value="Squad Power"),
        ):
            await sv._maybe_send_power_refresh_dm(
                inter,
                gid,
                "DS",
                "2026-05-18",
                42,
            )
        body = inter.user.send.await_args.args[0]
        assert body.startswith("✅ Your vote was recorded."), body
        # Header still surfaces so the member knows which column to fix.
        assert "Squad Power" in body
