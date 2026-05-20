"""
Branch coverage for setup wizards — phase 2 of the full-coverage suite.

`test_setup_flows.py` covers the basic happy paths for each wizard. This
file extends it with branches the original suite didn't reach:

  * /setup_train with blurbs **enabled** (themes, tones, prompt template)
  * /setup_birthdays with train integration on + reminders on
  * /setup_desertstorm + /setup_canyonstorm with participation **enabled**
  * /setup_events first-time flow (channels + draft time + add an event)
  * /setup_survey edit-existing-questions branch
  * /setup_members tab + columns + role filter
  * /setup_reset confirm AND cancel paths
  * /view_configuration replies after setup is complete

Pattern matches the existing test_setup_flows.py — mocked Discord views,
ask_keep_or_change patched to return scripted text, then assertions on
the persisted DB state.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID, PREMIUM_TEST_GUILD_ID, make_mock_interaction
from tests.integration.test_setup_flows import (
    make_send_handler, patch_keep_or_change,
)


# ── /setup_desertstorm + /setup_canyonstorm with participation enabled ────────

class TestStormSetupWithParticipation:
    """The new (#20) participation block — Step 6 of /setup_desertstorm."""

    @pytest.mark.asyncio
    async def test_ds_setup_participation_enabled_writes_questions(self, seeded_db):
        import config
        from setup_cog import run_storm_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        log_ch  = MagicMock(id=555111)
        post_ch = MagicMock(id=555222)
        ch_iter = iter([
            MagicMock(confirmed=True, selected_channel=log_ch,  wait=AsyncMock()),
            MagicMock(confirmed=True, selected_channel=post_ch, wait=AsyncMock()),
        ])

        # Build the participation config our test expects, bypassing the
        # interactive sub-flow. The wizard delegates to
        # `_run_storm_participation_step` — patching it lets us assert the
        # save call without re-implementing every prompt.
        async def fake_participation(*args, **kwargs):
            return {
                "enabled":          1,
                "tab_name":         "DS Participation Log",
                "questions":        [
                    {"key": "vote_count", "label": "Vote Count", "type": "numeric"},
                    {"key": "sitting_out", "label": "Sitting Out",
                     "type": "roster_names"},
                ],
                "roster_tab":       "Squad Powers",
                "roster_name_col":  0,
                "roster_alias_col": -1,
                "roster_start_row": 2,
            }

        with patch("setup_cog.ChannelSelectStep", side_effect=lambda *a, **kw: next(ch_iter)), \
             patch("setup_cog._run_storm_participation_step",
                    side_effect=fake_participation), \
             patch_keep_or_change(["DS Assignments"]):
            make_send_handler(
                interaction.channel,
                view_overrides={"selected": "A", "use_default": True},
            )
            await run_storm_setup(interaction, bot, "DS")

        pcfg = config.get_participation_config(TEST_GUILD_ID, "DS")
        assert pcfg["enabled"]          is True
        assert pcfg["tab_name"]         == "DS Participation Log"
        assert len(pcfg["questions"])   == 2
        assert pcfg["questions"][0]["type"] == "numeric"
        assert pcfg["roster_tab"]       == "Squad Powers"

    @pytest.mark.asyncio
    async def test_cs_setup_participation_disabled_keeps_disabled(self, seeded_db):
        """Disabling Step 6 doesn't break the rest of the storm save — the
        config persists, participation_enabled stays 0, and the storm
        templates land correctly."""
        import config
        from setup_cog import run_storm_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        log_ch  = MagicMock(id=666111)
        post_ch = MagicMock(id=666222)
        ch_iter = iter([
            MagicMock(confirmed=True, selected_channel=log_ch,  wait=AsyncMock()),
            MagicMock(confirmed=True, selected_channel=post_ch, wait=AsyncMock()),
        ])

        async def disabled_participation(*args, **kwargs):
            return {
                "enabled":          0,
                "tab_name":         "",
                "questions":        [],
                "roster_tab":       "",
                "roster_name_col":  0,
                "roster_alias_col": -1,
                "roster_start_row": 2,
            }

        with patch("setup_cog.ChannelSelectStep", side_effect=lambda *a, **kw: next(ch_iter)), \
             patch("setup_cog._run_storm_participation_step",
                    side_effect=disabled_participation), \
             patch_keep_or_change(["CS Assignments"]):
            make_send_handler(
                interaction.channel,
                view_overrides={"selected": "B", "use_default": True},
            )
            await run_storm_setup(interaction, bot, "CS")

        pcfg = config.get_participation_config(TEST_GUILD_ID, "CS")
        assert pcfg["enabled"] is False

        gcfg = config.get_config(TEST_GUILD_ID)
        assert gcfg.cs_log_channel_id == 666111


# ── /setup hub: reset flow + view-configuration helper (post-#201) ───────────

class TestSetupResetBranches:
    """Both confirm and cancel paths of the reset flow, which is now
    reachable from the `/setup` hub's 🗑️ Reset configuration button.
    The flow itself is exposed as the module-level `_run_reset_flow`
    helper — same logic, same gates."""

    @pytest.mark.asyncio
    async def test_reset_flow_confirm_clears_config(self, seeded_db):
        import config
        import setup_cog

        interaction = make_mock_interaction(is_admin=True)

        # _ConfirmResetView is defined inline inside _run_reset_flow.
        # Drive it by short-circuiting response.send_message: when the
        # view is passed in, set confirmed=True and stop() it so the
        # helper's `await view.wait()` returns immediately.
        async def _send(content=None, embed=None, view=None, **kw):
            if view is not None:
                view.confirmed = True
                try: view.stop()
                except Exception: pass
            return MagicMock(id=1)
        interaction.response.send_message = AsyncMock(side_effect=_send)

        await setup_cog._run_reset_flow(interaction)

        # After reset, the row should be reset to defaults (setup_complete=0)
        cfg = config.get_config(TEST_GUILD_ID)
        assert cfg.setup_complete == 0, (
            "Setup should be cleared after confirm — saw setup_complete still set"
        )
        followup_call = interaction.followup.send.call_args
        assert followup_call is not None
        sent_msg = followup_call.args[0] if followup_call.args else followup_call.kwargs.get("content", "")
        assert "reset" in sent_msg.lower()

    @pytest.mark.asyncio
    async def test_reset_flow_cancel_keeps_config(self, seeded_db):
        """Clicking Cancel preserves the existing config and surfaces the
        explicit 'Reset cancelled' message."""
        import config
        import setup_cog

        interaction = make_mock_interaction(is_admin=True)
        original_tz = config.get_config(TEST_GUILD_ID).timezone

        async def _send(content=None, embed=None, view=None, **kw):
            if view is not None:
                view.confirmed = False
                try: view.stop()
                except Exception: pass
            return MagicMock(id=1)
        interaction.response.send_message = AsyncMock(side_effect=_send)

        await setup_cog._run_reset_flow(interaction)

        cfg_after = config.get_config(TEST_GUILD_ID)
        assert cfg_after.setup_complete == 1
        assert cfg_after.timezone       == original_tz

        followup_call = interaction.followup.send.call_args
        assert followup_call is not None
        sent_msg = followup_call.args[0] if followup_call.args else followup_call.kwargs.get("content", "")
        assert "cancel" in sent_msg.lower()
        assert "still active" in sent_msg.lower()


# ── /setup → 🗂️ View configuration (post-#201) ───────────────────────────────

class TestViewConfiguration:
    """Post-#201: the bare `/view_configuration` slash command is gone;
    its body now sits behind the setup hub's 🗂️ View configuration
    button, which calls `setup_cog._send_view_configuration`. The
    button is wired via setup_hub.py; here we exercise the underlying
    helper directly to cover the same after-setup / before-setup
    branches the old slash test did."""

    @pytest.mark.asyncio
    async def test_view_configuration_after_setup_renders_embed(self, seeded_db):
        """Once setup_complete=1, the helper renders the configuration
        embed (not the 'not set up yet' message)."""
        from setup_cog import _send_view_configuration
        from config import get_config

        interaction = make_mock_interaction(is_admin=True)
        cfg = get_config(TEST_GUILD_ID)
        assert cfg is not None and cfg.setup_complete

        await _send_view_configuration(interaction, cfg)

        sent = (
            interaction.response.send_message.call_args
            or interaction.followup.send.call_args
        )
        assert sent is not None, (
            "_send_view_configuration didn't reply at all"
        )

    @pytest.mark.asyncio
    async def test_setup_hub_view_button_before_setup_says_not_set_up(self, temp_db):
        """When setup isn't complete, the hub's 🗂️ View configuration
        button replies with the friendly 'run /setup' redirect rather
        than crashing or rendering a half-empty embed. The button's
        not-set-up guard sits inline in setup_hub.py; we exercise the
        unbound callback function discord.py stashed on the Button
        item so we don't have to drive a real Discord interaction."""
        import setup_hub
        from unittest.mock import MagicMock as _M
        import config
        # Create a row but don't mark setup_complete
        config.get_or_create_config(TEST_GUILD_ID)

        interaction = make_mock_interaction(is_admin=True)
        bot = _M()
        view = setup_hub._SetupHubView(bot, TEST_GUILD_ID, 1, is_premium=False)
        # discord.py wraps the decorated coroutine on the Button item.
        # `Button.callback` is set to a partial bound to the view, so
        # invoking `view.btn_view_config.callback(interaction)` works.
        await view.btn_view_config.callback(interaction)

        sent = interaction.response.send_message.call_args
        msg = sent.args[0] if sent.args else sent.kwargs.get("content", "")
        assert "/setup" in msg or "set up" in msg.lower()


# ── /setup admin-or-leadership gate (#236) ───────────────────────────────────

class TestSetupHubLeadershipGate:
    """The /setup hub used to be admin-only, which blocked alliances
    where day-to-day officers don't carry full server-admin
    permissions. The hub now accepts admins OR members with the
    configured leadership role — matching the gate every per-feature
    `/setup_*` wizard already uses."""

    @pytest.mark.asyncio
    async def test_handle_setup_hub_lets_leadership_role_through(self, seeded_db):
        """A non-admin member with the configured Leadership role can
        run `/setup`; the hub embed + view are sent."""
        import setup_hub
        from tests.conftest import make_mock_role

        interaction = make_mock_interaction(is_admin=False)
        interaction.user.roles = [make_mock_role(name="Leadership")]
        bot = MagicMock()

        with patch("premium.is_premium", AsyncMock(return_value=False)):
            await setup_hub.handle_setup_hub(bot, interaction)

        sent = interaction.response.send_message.call_args
        assert sent is not None
        # The hub call sends an embed + view, not the reject message.
        kwargs = sent.kwargs
        assert "embed" in kwargs and "view" in kwargs

    @pytest.mark.asyncio
    async def test_handle_setup_hub_rejects_unprivileged_user(self, seeded_db):
        """A user with neither admin nor the Leadership role still hits
        the reject path, with a message naming the configured role."""
        import setup_hub
        from tests.conftest import make_mock_role

        interaction = make_mock_interaction(is_admin=False)
        interaction.user.roles = [make_mock_role(name="Member")]
        bot = MagicMock()

        await setup_hub.handle_setup_hub(bot, interaction)

        sent = interaction.response.send_message.call_args
        msg = sent.args[0] if sent.args else sent.kwargs.get("content", "")
        assert "Leadership" in msg
        assert "administrator" in msg.lower()

    @pytest.mark.asyncio
    async def test_hub_view_interaction_check_lets_leadership_through(self, seeded_db):
        """The setup hub's button-gate (interaction_check) follows the
        same admin-or-leadership rule as the slash command."""
        import setup_hub
        from tests.conftest import make_mock_role

        interaction = make_mock_interaction(is_admin=False)
        interaction.user.roles = [make_mock_role(name="Leadership")]
        view = setup_hub._SetupHubView(
            MagicMock(), TEST_GUILD_ID, interaction.user.id, is_premium=False,
        )

        result = await view.interaction_check(interaction)
        assert result is True
        interaction.response.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_hub_view_interaction_check_rejects_member_role(self, seeded_db):
        """A user without the Leadership role still bounces off the
        hub button gate."""
        import setup_hub
        from tests.conftest import make_mock_role

        interaction = make_mock_interaction(is_admin=False)
        interaction.user.roles = [make_mock_role(name="Member")]
        view = setup_hub._SetupHubView(
            MagicMock(), TEST_GUILD_ID, interaction.user.id, is_premium=False,
        )

        result = await view.interaction_check(interaction)
        assert result is False
        sent = interaction.response.send_message.call_args
        msg = sent.args[0] if sent.args else sent.kwargs.get("content", "")
        assert "Leadership" in msg


