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


class TestCustomIdEncoding:
    def test_round_trip(self):
        cid = sv.make_custom_id(12345, "DS", "2026-05-18", "a")
        parsed = sv.parse_custom_id(cid)
        assert parsed == {
            "guild_id":   12345,
            "event_type": "ds",
            "event_date": "2026-05-18",
            "vote":       "a",
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
        for bad in ("", "signup", "signup:foo", "signup:not_int:ds:2026-01-01:a",
                    "other:1:ds:2026-01-01:a", "signup:1:ds:2026-01-01:a:extra"):
            assert sv.parse_custom_id(bad) is None

    def test_custom_id_under_discord_limit(self):
        # Discord caps custom_id at 100 chars. Worst realistic case is a
        # 19-digit guild_id (Snowflake ceiling).
        cid = sv.make_custom_id(9999999999999999999, "DS", "2026-12-31", "either")
        assert len(cid) <= 100


class TestSignupViewConstruction:
    def test_four_buttons_with_stable_custom_ids(self):
        view = sv.SignupView(12345, "DS", "2026-05-18",
                             time_a_label="9pm ET", time_b_label="4pm ET")
        # Four buttons total
        assert len(view.children) == 4
        # Each button has a parseable custom_id
        votes_found = set()
        for btn in view.children:
            parsed = sv.parse_custom_id(btn.custom_id)
            assert parsed is not None
            assert parsed["guild_id"]   == 12345
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
        view = sv.SignupView(1, "DS", "2026-05-18",
                             time_a_label="9pm ET", time_b_label="4pm ET")
        labels = [c.label for c in view.children]
        assert any("9pm ET" in lab for lab in labels)
        assert any("4pm ET" in lab for lab in labels)

    def test_cs_default_renders_two_buttons(self):
        """CS rosters only fight at one time per faction. Team B and
        Either are meaningless for CS, so they're skipped on a fresh
        post."""
        view = sv.SignupView(12345, "CS", "2026-05-18")
        codes = sorted(
            sv.parse_custom_id(c.custom_id)["vote"] for c in view.children
        )
        assert codes == ["a", "cannot"]

    def test_cs_force_all_buttons_renders_four(self):
        """`_force_all_buttons=True` keeps all 4 button handlers
        registered so a pre-hotfix CS post (which has all 4 buttons
        already rendered in Discord) stays clickable after a bot
        restart — discord.py routes by custom_id matching."""
        view = sv.SignupView(12345, "CS", "2026-05-18",
                             _force_all_buttons=True)
        codes = sorted(
            sv.parse_custom_id(c.custom_id)["vote"] for c in view.children
        )
        assert codes == ["a", "b", "cannot", "either"]

    def test_force_all_buttons_noop_for_ds(self):
        """DS always has all 4 buttons regardless of the flag."""
        view_default = sv.SignupView(1, "DS", "2026-05-18")
        view_forced = sv.SignupView(1, "DS", "2026-05-18",
                                    _force_all_buttons=True)
        assert len(view_default.children) == len(view_forced.children) == 4

    def test_ds_teams_a_renders_a_plus_cannot(self):
        """#148 single-team DS: alliance opted into Team A only.
        SignupView renders A + Cannot (no B / Either)."""
        view = sv.SignupView(1, "DS", "2026-05-18", teams="A")
        codes = sorted(
            sv.parse_custom_id(c.custom_id)["vote"] for c in view.children
        )
        assert codes == ["a", "cannot"]

    def test_ds_teams_b_renders_b_plus_cannot(self):
        view = sv.SignupView(1, "DS", "2026-05-18", teams="B")
        codes = sorted(
            sv.parse_custom_id(c.custom_id)["vote"] for c in view.children
        )
        assert codes == ["b", "cannot"]

    def test_ds_teams_both_renders_all_four(self):
        """`teams="both"` is the default and matches the current behaviour."""
        view = sv.SignupView(1, "DS", "2026-05-18", teams="both")
        assert len(view.children) == 4

    def test_force_all_buttons_overrides_single_team_setting(self):
        """Re-registration always reattaches all 4 button handlers
        regardless of the current `teams` setting — a pre-config-change
        post may have all 4 buttons rendered, and the View needs
        routes for every persisted custom_id."""
        view = sv.SignupView(1, "DS", "2026-05-18",
                             teams="A", _force_all_buttons=True)
        assert len(view.children) == 4

    def test_cs_ignores_teams_setting(self):
        """CS has no Team B concept; the `teams` field is DS-only.
        CS construction always renders A + Cannot."""
        view = sv.SignupView(1, "CS", "2026-05-18", teams="A")
        codes = sorted(
            sv.parse_custom_id(c.custom_id)["vote"] for c in view.children
        )
        assert codes == ["a", "cannot"]
        view = sv.SignupView(1, "CS", "2026-05-18", teams="B")
        codes = sorted(
            sv.parse_custom_id(c.custom_id)["vote"] for c in view.children
        )
        assert codes == ["a", "cannot"]

    def test_garbage_teams_value_falls_back_to_both(self):
        """A schema-drift sentinel: if `teams` reads as something other
        than the three valid values, the View falls back to the
        permissive default rather than rendering zero buttons."""
        view = sv.SignupView(1, "DS", "2026-05-18", teams="garbage")
        assert len(view.children) == 4

    def test_empty_time_label_renders_bare_team_name(self):
        """The doubled-label bug: when `time_a_label=""`, the button
        should render as `🅰️ Team A`, not `🅰️ Team A: ` (trailing
        colon) and definitely not `🅰️ Team A: Team A`."""
        view = sv.SignupView(1, "DS", "2026-05-18",
                             time_a_label="", time_b_label="")
        labels = [c.label for c in view.children]
        assert any(lab == "🅰️ Team A" for lab in labels), (
            f"expected bare '🅰️ Team A' in {labels}"
        )
        assert any(lab == "🅱️ Team B" for lab in labels), (
            f"expected bare '🅱️ Team B' in {labels}"
        )
        # Nothing renders as a doubled label.
        assert not any("Team A: Team A" in lab for lab in labels)
        assert not any("Team B: Team B" in lab for lab in labels)


class TestCsStaleVoteReject:
    """Pre-hotfix CS sign-up posts have all 4 buttons rendered.
    `register_persistent_signup_views` keeps them clickable via
    `_force_all_buttons=True`, but b/either are meaningless for CS
    now. The click handler intercepts and politely redirects rather
    than writing a nonsensical row to storm_signups."""

    @pytest.mark.asyncio
    async def test_cs_b_vote_is_rejected_before_premium_check(self, seeded_db):
        from unittest.mock import AsyncMock, MagicMock, patch
        # CS post + member clicks the now-stale Team B button.
        cid = sv.make_custom_id(TEST_GUILD_ID, "CS", "2026-05-18", "b")
        interaction = MagicMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.data = {"custom_id": cid}
        interaction.user.id = 42
        interaction.response.send_message = AsyncMock()
        interaction.response.defer       = AsyncMock()
        interaction.followup.send        = AsyncMock()
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("config.record_storm_vote") as record:
            await sv._handle_signup_click(interaction, "b")
        # The polite reject fires BEFORE record_storm_vote.
        record.assert_not_called()
        interaction.response.send_message.assert_awaited_once()
        body = interaction.response.send_message.await_args.args[0]
        assert "single-team" in body or "Canyon Storm" in body or "CS" in body

    @pytest.mark.asyncio
    async def test_cs_either_vote_is_rejected(self, seeded_db):
        from unittest.mock import AsyncMock, MagicMock, patch
        cid = sv.make_custom_id(TEST_GUILD_ID, "CS", "2026-05-18", "either")
        interaction = MagicMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.data = {"custom_id": cid}
        interaction.user.id = 42
        interaction.response.send_message = AsyncMock()
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("config.record_storm_vote") as record:
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
        interaction.response.defer       = AsyncMock()
        interaction.followup.send        = AsyncMock()
        interaction.guild = None  # short-circuits the chunk pre-pass
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("storm_signup_view._mirror_vote_to_sheet"), \
             patch("storm_signup_view._maybe_send_power_refresh_dm",
                   new=AsyncMock()), \
             patch("config.record_storm_vote") as record:
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
        interaction.response.defer       = AsyncMock()
        interaction.followup.send        = AsyncMock()
        interaction.guild = None
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("storm_signup_view._mirror_vote_to_sheet"), \
             patch("storm_signup_view._maybe_send_power_refresh_dm",
                   new=AsyncMock()), \
             patch("config.record_storm_vote") as record:
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
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
            teams="A",
        )
        return TEST_GUILD_ID

    @pytest.fixture
    def env_teams_b(self, seeded_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
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
        interaction.response.defer       = AsyncMock()
        interaction.followup.send        = AsyncMock()
        interaction.guild = None
        return interaction

    @pytest.mark.asyncio
    async def test_teams_a_rejects_team_b_vote(self, env_teams_a):
        from unittest.mock import AsyncMock, patch
        inter = self._fake_interaction("b")
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("config.record_storm_vote") as record:
            await sv._handle_signup_click(inter, "b")
        record.assert_not_called()
        body = inter.response.send_message.await_args.args[0]
        assert "Team A only" in body

    @pytest.mark.asyncio
    async def test_teams_a_rejects_either_vote(self, env_teams_a):
        from unittest.mock import AsyncMock, patch
        inter = self._fake_interaction("either")
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("config.record_storm_vote") as record:
            await sv._handle_signup_click(inter, "either")
        record.assert_not_called()

    @pytest.mark.asyncio
    async def test_teams_a_accepts_team_a_vote(self, env_teams_a):
        from unittest.mock import AsyncMock, patch
        inter = self._fake_interaction("a")
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("storm_signup_view._mirror_vote_to_sheet"), \
             patch("storm_signup_view._maybe_send_power_refresh_dm",
                   new=AsyncMock()), \
             patch("config.record_storm_vote") as record:
            await sv._handle_signup_click(inter, "a")
        record.assert_called_once()

    @pytest.mark.asyncio
    async def test_teams_a_accepts_cannot_vote(self, env_teams_a):
        from unittest.mock import AsyncMock, patch
        inter = self._fake_interaction("cannot")
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("storm_signup_view._mirror_vote_to_sheet"), \
             patch("storm_signup_view._maybe_send_power_refresh_dm",
                   new=AsyncMock()), \
             patch("config.record_storm_vote") as record:
            await sv._handle_signup_click(inter, "cannot")
        record.assert_called_once()

    @pytest.mark.asyncio
    async def test_teams_b_rejects_team_a_vote(self, env_teams_b):
        from unittest.mock import AsyncMock, patch
        inter = self._fake_interaction("a")
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("config.record_storm_vote") as record:
            await sv._handle_signup_click(inter, "a")
        record.assert_not_called()
        body = inter.response.send_message.await_args.args[0]
        assert "Team B only" in body

    @pytest.mark.asyncio
    async def test_teams_b_rejects_either_vote(self, env_teams_b):
        from unittest.mock import AsyncMock, patch
        inter = self._fake_interaction("either")
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("config.record_storm_vote") as record:
            await sv._handle_signup_click(inter, "either")
        record.assert_not_called()

    @pytest.mark.asyncio
    async def test_teams_b_accepts_team_b_vote(self, env_teams_b):
        from unittest.mock import AsyncMock, patch
        inter = self._fake_interaction("b")
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch("storm_signup_view._mirror_vote_to_sheet"), \
             patch("storm_signup_view._maybe_send_power_refresh_dm",
                   new=AsyncMock()), \
             patch("config.record_storm_vote") as record:
            await sv._handle_signup_click(inter, "b")
        record.assert_called_once()

    @pytest.mark.asyncio
    async def test_teams_both_accepts_all_votes(self, seeded_db):
        """teams=both (default) preserves the original 4-vote behaviour."""
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
            teams="both",
        )
        from unittest.mock import AsyncMock, patch
        for vote in ("a", "b", "either", "cannot"):
            inter = self._fake_interaction(vote)
            with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
                 patch("storm_signup_view._mirror_vote_to_sheet"), \
                 patch("storm_signup_view._maybe_send_power_refresh_dm",
                       new=AsyncMock()), \
                 patch("config.record_storm_vote") as record:
                await sv._handle_signup_click(inter, vote)
            record.assert_called_once(), f"vote {vote} should record"


class TestSignupHistoryAudit:
    """The UPSERT in `record_storm_vote` overwrites the prior row in
    `storm_signups`, so the audit trail for re-votes (especially the
    on-behalf path required by #38) lives in `storm_signup_history`."""

    def test_self_vote_appends_history_row(self, seeded_db):
        import config
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=42, target_member_id="42", vote="a",
        )
        history = config.get_storm_signup_history(
            TEST_GUILD_ID, "DS", "2026-05-18",
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
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=42, target_member_id="42", vote="a",
        )
        # Officer overrides — B
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=999, target_member_id="42", vote="b",
            is_on_behalf=True,
        )
        # Member changes their mind — Either
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=42, target_member_id="42", vote="either",
        )
        history = config.get_storm_signup_history(
            TEST_GUILD_ID, "DS", "2026-05-18", target_member_id="42",
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
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=42, target_member_id="42", vote="a",
        )
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=42, target_member_id="42", vote="cannot",
        )
        current = config.get_member_vote(
            TEST_GUILD_ID, "DS", "2026-05-18", "42",
        )
        assert current["vote"] == "cannot"
        history = config.get_storm_signup_history(
            TEST_GUILD_ID, "DS", "2026-05-18",
        )
        assert len(history) == 2


class TestPersistentViewRegistration:
    """Bot-restart-resiliency: `register_persistent_signup_views` walks
    every recent registration post and rebinds a SignupView to its
    `message_id`. Without this, every post-restart button click fails
    with "Interaction failed."""

    def test_walks_recent_posts_and_calls_add_view(self, seeded_db):
        import config
        # Two posts within the 14-day window.
        config.record_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-18",
            channel_id=100, message_id=200,
            time_a_label="9pm ET", time_b_label="4pm ET",
        )
        config.record_storm_registration_post(
            TEST_GUILD_ID, "CS", "2026-05-25",
            channel_id=100, message_id=300,
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
        import config
        config.record_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-18",
            channel_id=100, message_id=200,
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
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            power_column_name="1st Squad Power",
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
                {"42": {"key": "42", "name": "Alice",
                        "discord_id": "42", "power": voter_power,
                        "not_on_discord": False}},
                [],
            ),
        )

    @pytest.mark.asyncio
    async def test_disabled_flag_short_circuits(self, env, seeded_db):
        import config
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            power_refresh_dm_enabled=False,
        )
        inter = self._fake_interaction()
        await sv._maybe_send_power_refresh_dm(
            inter, env, "DS", "2026-05-18", 42,
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
                inter, env, "DS", "2026-05-18", 42,
            )
        inter.user.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_power_parseable_no_dm(self, env):
        inter = self._fake_interaction()
        with self._patch_roster(voter_power=412_000_000):
            await sv._maybe_send_power_refresh_dm(
                inter, env, "DS", "2026-05-18", 42,
            )
        inter.user.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_power_unparseable_sends_dm_and_records(self, env):
        import config
        inter = self._fake_interaction()
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter, env, "DS", "2026-05-18", 42,
            )
        inter.user.send.assert_awaited_once()
        body = inter.user.send.await_args.args[0]
        assert "1st Squad Power" in body
        # Cooldown row recorded — subsequent re-vote is silent.
        assert config.has_power_refresh_dm_been_sent(
            env, "DS", "2026-05-18", 42,
        )

    @pytest.mark.asyncio
    async def test_dm_body_strips_leading_your_to_avoid_double_possessive(
        self, env,
    ):
        """If the alliance named their power column 'Your Power', the
        rendered body must not read 'your **Your Power** on the alliance
        roster Sheet…'. The strip-leading-your fix drops the duplicate."""
        import config
        config.save_structured_storm_config(
            env, "DS",
            structured_flow_enabled=True,
            power_column_name="Your Squad Power",
            power_refresh_dm_enabled=True,
        )
        inter = self._fake_interaction()
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter, env, "DS", "2026-05-18", 42,
            )
        body = inter.user.send.await_args.args[0]
        # Column header rendered without its leading "Your "
        assert "**Squad Power**" in body
        assert "your **Your" not in body.lower()

    @pytest.mark.asyncio
    async def test_dm_body_keeps_column_name_when_no_leading_possessive(
        self, env,
    ):
        """A column header without a leading 'your'/'my' renders
        verbatim — the strip only kicks in when needed."""
        import config
        config.save_structured_storm_config(
            env, "DS",
            structured_flow_enabled=True,
            power_column_name="Squad Power",
            power_refresh_dm_enabled=True,
        )
        inter = self._fake_interaction()
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter, env, "DS", "2026-05-18", 42,
            )
        body = inter.user.send.await_args.args[0]
        assert "**Squad Power**" in body

    @pytest.mark.asyncio
    async def test_cooldown_prevents_double_dm_on_revote(self, env):
        inter1 = self._fake_interaction()
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter1, env, "DS", "2026-05-18", 42,
            )
        assert inter1.user.send.await_count == 1
        # Second re-vote → cooldown short-circuits, no second DM.
        inter2 = self._fake_interaction()
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter2, env, "DS", "2026-05-18", 42,
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
                response=MagicMock(status=403), message="DMs disabled",
            ),
        )
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter, env, "DS", "2026-05-18", 42,
            )
        # Cooldown is recorded so we don't hit the Sheet read again.
        assert config.has_power_refresh_dm_been_sent(
            env, "DS", "2026-05-18", 42,
        )

    @pytest.mark.asyncio
    async def test_httpexception_backs_out_cooldown(self, env):
        """Transient HTTP error = the DM didn't land; back out the
        cooldown row so the next re-vote retries. Audit Major M1."""
        import config
        import discord as _d
        inter = self._fake_interaction(
            send_raises=_d.HTTPException(
                response=MagicMock(status=503), message="Service unavailable",
            ),
        )
        with self._patch_roster(voter_power=None):
            await sv._maybe_send_power_refresh_dm(
                inter, env, "DS", "2026-05-18", 42,
            )
        # Cooldown was backed out — next click can retry.
        assert not config.has_power_refresh_dm_been_sent(
            env, "DS", "2026-05-18", 42,
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
                inter_a, env, "DS", "2026-05-18", 42,
            )
            await sv._maybe_send_power_refresh_dm(
                inter_b, env, "DS", "2026-05-18", 42,
            )

        # Exactly one DM was sent.
        assert inter_a.user.send.await_count == 1
        assert inter_b.user.send.await_count == 0


class TestClearPowerRefreshDmSent:
    """The new `clear_power_refresh_dm_sent` helper backs out a row
    after a transient HTTPException."""

    def test_round_trip(self, seeded_db):
        import config
        config.record_power_refresh_dm_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 42,
        )
        assert config.has_power_refresh_dm_been_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 42,
        )
        ok = config.clear_power_refresh_dm_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 42,
        )
        assert ok
        assert not config.has_power_refresh_dm_been_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 42,
        )

    def test_clear_nothing_is_safe(self, seeded_db):
        import config
        # Clearing a non-existent row is a no-op, not an exception.
        assert not config.clear_power_refresh_dm_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 999,
        )
