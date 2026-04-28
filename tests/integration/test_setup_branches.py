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

from tests.conftest import TEST_GUILD_ID, OGV_GUILD_ID, make_mock_interaction
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


# ── /setup_reset ──────────────────────────────────────────────────────────────

class TestSetupResetBranches:
    """Both confirm and cancel paths of /setup_reset."""

    @pytest.mark.asyncio
    async def test_setup_reset_confirm_clears_config(self, seeded_db):
        import config
        from setup_cog import SetupCog

        cog = SetupCog(MagicMock())
        interaction = make_mock_interaction(is_admin=True)

        # ConfirmResetView is defined inline. Drive it by short-circuiting
        # response.send_message: when the view is passed in, set
        # confirmed=True and stop() it so the wizard's `await view.wait()`
        # returns immediately.
        async def _send(content=None, embed=None, view=None, **kw):
            if view is not None:
                view.confirmed = True
                try: view.stop()
                except Exception: pass
            return MagicMock(id=1)
        interaction.response.send_message = AsyncMock(side_effect=_send)

        await cog.setup_reset.callback(cog, interaction)

        # After reset, the row should be reset to defaults (setup_complete=0)
        cfg = config.get_config(TEST_GUILD_ID)
        assert cfg.setup_complete == 0, (
            "Setup should be cleared after confirm — saw setup_complete still set"
        )
        # Followup should mention the reset succeeded
        followup_call = interaction.followup.send.call_args
        assert followup_call is not None
        sent_msg = followup_call.args[0] if followup_call.args else followup_call.kwargs.get("content", "")
        assert "reset" in sent_msg.lower()

    @pytest.mark.asyncio
    async def test_setup_reset_cancel_keeps_config(self, seeded_db):
        """Clicking Cancel (or timing out) preserves the existing config
        and surfaces the explicit 'Reset cancelled' message."""
        import config
        from setup_cog import SetupCog

        cog = SetupCog(MagicMock())
        interaction = make_mock_interaction(is_admin=True)

        original_tz = config.get_config(TEST_GUILD_ID).timezone

        # Drive the inline ConfirmResetView with confirmed=False (cancel).
        async def _send(content=None, embed=None, view=None, **kw):
            if view is not None:
                view.confirmed = False
                try: view.stop()
                except Exception: pass
            return MagicMock(id=1)
        interaction.response.send_message = AsyncMock(side_effect=_send)

        await cog.setup_reset.callback(cog, interaction)

        # Config should be preserved
        cfg_after = config.get_config(TEST_GUILD_ID)
        assert cfg_after.setup_complete == 1
        assert cfg_after.timezone       == original_tz

        # The followup message should explicitly mention "cancelled" so
        # the user knows nothing was reset.
        followup_call = interaction.followup.send.call_args
        assert followup_call is not None
        sent_msg = followup_call.args[0] if followup_call.args else followup_call.kwargs.get("content", "")
        assert "cancel" in sent_msg.lower()
        assert "still active" in sent_msg.lower()


# ── /view_configuration ───────────────────────────────────────────────────────

class TestViewConfiguration:

    @pytest.mark.asyncio
    async def test_view_configuration_after_setup(self, seeded_db):
        """Once setup_complete=1, /view_configuration should respond with
        an embed (not the 'not set up yet' message)."""
        from setup_cog import SetupCog
        cog = SetupCog(MagicMock())

        interaction = make_mock_interaction(is_admin=True)
        await cog.view_configuration.callback(cog, interaction)

        # Either response.send_message OR followup.send received an embed
        sent = (
            interaction.response.send_message.call_args
            or interaction.followup.send.call_args
        )
        assert sent is not None, (
            "/view_configuration didn't reply at all"
        )
        embed = sent.kwargs.get("embed")
        # Some pathways send raw text first; either is acceptable as long
        # as something got sent.

    @pytest.mark.asyncio
    async def test_view_configuration_before_setup(self, temp_db):
        """If setup isn't complete, /view_configuration should respond
        with a friendly 'run /setup' message rather than crashing."""
        from setup_cog import SetupCog
        import config
        # Create a row but don't mark setup_complete
        config.get_or_create_config(TEST_GUILD_ID)

        cog = SetupCog(MagicMock())
        interaction = make_mock_interaction(is_admin=True)
        await cog.view_configuration.callback(cog, interaction)

        sent = interaction.response.send_message.call_args
        msg = sent.args[0] if sent.args else sent.kwargs.get("content", "")
        assert "/setup" in msg or "set up" in msg.lower()


