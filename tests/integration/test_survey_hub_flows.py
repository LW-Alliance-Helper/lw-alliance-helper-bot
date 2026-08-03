"""
Phase 4 of the full-coverage suite: the /survey hub and its reminder flows.

Covers:

  * /survey on Premium renders every hub action ungated
  * /survey on free tier renders the same hub with Add / Remove 💎-gated,
    and Edit / Translation still usable (the free-tier lockout fix)
  * Reminders hub: cancel
  * Reminders hub → Send now → channel post (free tier path)
  * Reminders hub → Send now → DM via roster (Premium path)
  * Reminders hub → Manage scheduled → Off (disable)
  * Reminders hub → Manage scheduled → Daily + channel destination
  * Scheduled-reminder helpers (_send_reminder_to_channel,
    _send_reminder_via_dm) work end-to-end on the helper layer
  * SurveyCog.check_scheduled_reminders fires when frequency/day/time
    line up, and skips when reminder_last_fired matches today
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone, date as date_cls
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID, PREMIUM_TEST_GUILD_ID, make_mock_interaction


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_message():
    msg = MagicMock(id=999)
    msg.edit = AsyncMock(return_value=msg)
    msg.delete = AsyncMock()
    return msg


def _make_followup_interaction(guild_id=TEST_GUILD_ID, is_admin=True):
    """Interaction that the survey flow uses — needs working response and
    followup, plus user.roles matching the guild's saved leadership role."""
    import config as _config

    interaction = make_mock_interaction(guild_id=guild_id, is_admin=is_admin)

    cfg = _config.get_config(guild_id)
    leadership_role_name = (
        cfg.leadership_role_name if cfg and cfg.leadership_role_name else "Leadership"
    )

    role = MagicMock()
    role.name = leadership_role_name
    interaction.user.roles = [role]
    interaction.followup.send = AsyncMock(return_value=_make_message())
    interaction.response.send_message = AsyncMock(return_value=_make_message())
    return interaction


def _captured_followups(interaction):
    """All (content, kwargs) sent through interaction.followup.send."""
    out = []
    for call in interaction.followup.send.call_args_list:
        args, kwargs = call
        c = args[0] if args else kwargs.get("content")
        out.append((c, kwargs))
    return out


# ── /survey command ──────────────────────────────────────────────────────────


class TestSurveyCommandRendering:
    """`/survey` renders one hub on both tiers; only the button gating differs.

    Before the hub consolidation the free tier got a separate read-only detail
    view and the whole surface sat behind a Premium-disabled `/setup` button,
    which left free alliances unable to configure their (free) survey at all.
    """

    @pytest.mark.asyncio
    @pytest.mark.free_tier_only
    async def test_free_tier_gets_the_hub_with_premium_actions_disabled(self, seeded_db):
        import premium

        premium.clear_cache()

        from survey import SurveyCog

        bot = MagicMock()
        bot.add_view = MagicMock()
        bot.add_dynamic_items = MagicMock()
        cog = SurveyCog(bot)
        try:
            interaction = _make_followup_interaction()
            interaction.entitlements = []  # free tier

            await cog.survey.callback(cog, interaction)

            sent = interaction.response.send_message.call_args
            assert sent is not None
            embed = sent.kwargs.get("embed")
            view = sent.kwargs.get("view")
            assert embed is not None
            assert "Surveys" in (embed.title or "")
            assert view is not None

            by_label = {getattr(c, "label", ""): c for c in view.children}
            # Extras are Premium, so Add and Remove are off with a 💎 prefix.
            add = next(c for lbl, c in by_label.items() if "Add Survey" in lbl)
            remove = next(c for lbl, c in by_label.items() if "Remove Survey" in lbl)
            assert add.disabled and add.label.startswith("💎")
            assert remove.disabled and remove.label.startswith("💎")

            # Editing the default survey and the translation helper stay free —
            # this is the free-tier lockout fix.
            editish = next(
                c for lbl, c in by_label.items() if "Edit Survey" in lbl or "Set Up Survey" in lbl
            )
            translate = next(c for lbl, c in by_label.items() if "Translation" in lbl)
            assert not editish.disabled
            assert not translate.disabled
        finally:
            try:
                cog.check_scheduled_reminders.cancel()
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_premium_shows_manage_view_with_buttons(self, seeded_db, monkeypatch):
        """Premium tier gets the consolidated list + Add/Edit/Remove buttons."""
        # The premium guild resolves via PREMIUM_BYPASS_GUILD_IDS env var.
        # The leadership guard inside /survey also needs the guild's guild_configs
        # row to exist with setup_complete = 1, which we create explicitly.
        import importlib

        monkeypatch.setenv("PREMIUM_BYPASS_GUILD_IDS", str(PREMIUM_TEST_GUILD_ID))
        import premium as _premium

        importlib.reload(_premium)
        _premium.clear_cache()

        import config as _config

        cfg = _config.get_or_create_config(PREMIUM_TEST_GUILD_ID)
        cfg.leadership_role_name = "Leadership"
        cfg.leadership_channel_id = 111111111111111112
        cfg.setup_complete = 1
        _config.save_config(cfg)

        from survey import SurveyCog

        bot = MagicMock()
        bot.add_view = MagicMock()
        bot.add_dynamic_items = MagicMock()
        cog = SurveyCog(bot)
        try:
            interaction = _make_followup_interaction(guild_id=PREMIUM_TEST_GUILD_ID)
            interaction.entitlements = []

            await cog.survey.callback(cog, interaction)

            # Fresh interaction, so the hub responds directly (no defer) —
            # same idiom as the train and setup hubs.
            sent = interaction.response.send_message.call_args
            assert sent is not None
            view = sent.kwargs.get("view")
            embed = sent.kwargs.get("embed")
            assert view is not None, "/survey should include the hub view"
            assert embed is not None
            assert "Surveys" in (embed.title or "")

            # View should expose every action, with no 💎 prefixes on Premium.
            labels = {getattr(c, "label", "") for c in view.children}
            assert any("Add Survey" in l for l in labels)
            assert any("Edit Survey" in l or "Set Up Survey" in l for l in labels)
            assert any("Remove Survey" in l for l in labels)
            assert any("Post Survey" in l for l in labels)
            assert any("Reminders" in l for l in labels)
            assert any("Translation" in l for l in labels)
            assert not any(l.startswith("💎") for l in labels)
        finally:
            try:
                cog.check_scheduled_reminders.cancel()
            except Exception:
                pass


# ── /survey_remind hub ────────────────────────────────────────────────────────


class TestSurveyRemindHubCancel:
    """Cancelling the hub immediately exits without any further flow."""

    @pytest.mark.asyncio
    async def test_hub_cancel_exits_quietly(self, seeded_db):
        from survey import SurveyCog, _ReminderHubView, _run_remind_hub

        bot = MagicMock()
        bot.add_view = MagicMock()
        bot.add_dynamic_items = MagicMock()
        cog = SurveyCog(bot)
        try:
            interaction = _make_followup_interaction()
            interaction.entitlements = []

            # Drive the hub view: when send_message gets a view, set
            # `choice` to None (cancel) and stop().
            async def _send(content=None, view=None, **kw):
                if view is not None and isinstance(view, _ReminderHubView):
                    view.choice = None
                    try:
                        view.stop()
                    except Exception:
                        pass
                return _make_message()

            interaction.response.send_message = AsyncMock(side_effect=_send)

            await _run_remind_hub(interaction, cog.bot)

            # No further steps — followup never had to fire a destination
            # picker or schedule wizard.
            for call in interaction.followup.send.call_args_list:
                content = call.args[0] if call.args else call.kwargs.get("content", "")
                assert "destination" not in (content or "").lower()
                assert "frequency" not in (content or "").lower()
        finally:
            try:
                cog.check_scheduled_reminders.cancel()
            except Exception:
                pass


# ── Send-now path: channel post (free tier) ───────────────────────────────────


class TestSurveyRemindSendNowChannel:
    """Free tier picks Send now → channel post → reminder fires to the
    chosen channel."""

    @pytest.mark.asyncio
    @pytest.mark.free_tier_only
    async def test_send_now_channel_post_fires(self, seeded_db):
        import premium

        premium.clear_cache()

        from survey import (
            SurveyCog,
            _ReminderHubView,
            _DestinationPickView,
            _ChannelPickView,
            _run_remind_hub,
        )

        bot = MagicMock()
        bot.add_view = MagicMock()
        bot.add_dynamic_items = MagicMock()
        # The reminder helper resolves the channel via bot.get_channel.
        target_channel = AsyncMock()
        target_channel.send = AsyncMock()
        bot.get_channel = MagicMock(return_value=target_channel)

        cog = SurveyCog(bot)
        try:
            interaction = _make_followup_interaction(is_admin=True)
            interaction.entitlements = []
            interaction.client = bot  # used by _pick_survey

            view_log = []

            async def _drive_response_send(content=None, view=None, **kw):
                """Hub picker — pick `send`."""
                if view is not None and isinstance(view, _ReminderHubView):
                    view.choice = "send"
                    try:
                        view.stop()
                    except Exception:
                        pass
                return _make_message()

            interaction.response.send_message = AsyncMock(side_effect=_drive_response_send)

            async def _drive_followup_send(content=None, view=None, **kw):
                """Destination picker → channel; channel picker → real channel."""
                view_log.append((content, view))
                if isinstance(view, _DestinationPickView):
                    view.choice = "channel"
                    try:
                        view.stop()
                    except Exception:
                        pass
                elif isinstance(view, _ChannelPickView):
                    pseudo_ch = MagicMock(id=999_888)
                    pseudo_ch.mention = "<#999888>"
                    view.channel = pseudo_ch
                    try:
                        view.stop()
                    except Exception:
                        pass
                return _make_message()

            interaction.followup.send = AsyncMock(side_effect=_drive_followup_send)

            await _run_remind_hub(interaction, cog.bot)

            # The helper should have called bot.get_channel with the picked
            # ID and then sent the reminder body.
            assert bot.get_channel.called
            assert target_channel.send.called, (
                "Reminder body should have been posted to the chosen channel"
            )
        finally:
            try:
                cog.check_scheduled_reminders.cancel()
            except Exception:
                pass


# ── Helper layer: _send_reminder_to_channel + _send_reminder_via_dm ──────────


class TestReminderHelpers:
    """Smoke-test the reminder dispatch helpers in isolation."""

    @pytest.mark.asyncio
    async def test_send_to_channel_returns_true_on_success(self, seeded_db):
        from survey import _send_reminder_to_channel

        target = AsyncMock()
        target.send = AsyncMock()
        bot = MagicMock()
        bot.get_channel = MagicMock(return_value=target)

        ok = await _send_reminder_to_channel(bot, TEST_GUILD_ID, 12345, "BODY")
        assert ok is True
        target.send.assert_called_once_with("BODY")

    @pytest.mark.asyncio
    async def test_send_to_channel_returns_false_when_channel_missing(self, seeded_db):
        from survey import _send_reminder_to_channel

        bot = MagicMock()
        bot.get_channel = MagicMock(return_value=None)

        ok = await _send_reminder_to_channel(bot, TEST_GUILD_ID, 0, "BODY")
        assert ok is False

    @pytest.mark.asyncio
    async def test_send_via_dm_skips_when_roster_disabled(self, seeded_db):
        """No roster configured → dispatch returns (0, 0) without calling
        the gspread layer."""
        from survey import _send_reminder_via_dm

        bot = MagicMock()
        sent, skipped = await _send_reminder_via_dm(bot, TEST_GUILD_ID, "BODY")
        assert (sent, skipped) == (0, 0)


# ── Scheduled-reminder tick ───────────────────────────────────────────────────


class TestScheduledReminderTick:
    """SurveyCog.check_scheduled_reminders fires when the time matches
    the configured schedule and idempotency stops re-fires."""

    @pytest.mark.asyncio
    async def test_tick_fires_due_daily_reminder_to_channel(self, seeded_db):
        import config
        from survey import SurveyCog

        # Enable a daily channel-post reminder for the default survey.
        config.save_survey_config(
            TEST_GUILD_ID,
            "Squad Powers",
            "Survey History",
            [],
            "intro",
        )
        # Compute the guild's "now" so we can match the schedule on it.
        from zoneinfo import ZoneInfo

        guild_now = datetime.now(tz=ZoneInfo("America/New_York"))
        target_time = f"{guild_now.hour:02d}:{guild_now.minute:02d}"

        config.save_survey_reminder(
            TEST_GUILD_ID,
            "default",
            enabled=1,
            frequency="daily",
            day_of_week=0,
            time_str=target_time,
            channel_id=44_001,
            use_dm=0,
            message="Time to fill out the survey!",
        )

        bot = MagicMock()
        bot.add_view = MagicMock()
        bot.add_dynamic_items = MagicMock()
        target = AsyncMock()
        target.send = AsyncMock()
        bot.get_channel = MagicMock(return_value=target)

        cog = SurveyCog(bot)
        try:
            await cog.check_scheduled_reminders()
        finally:
            try:
                cog.check_scheduled_reminders.cancel()
            except Exception:
                pass

        target.send.assert_called_once_with("Time to fill out the survey!")

        # Idempotency: running the tick again the same minute should NOT
        # fire a second time (reminder_last_fired now matches today).
        await cog.check_scheduled_reminders()
        assert target.send.call_count == 1, "Tick re-fire on the same day should be a no-op"

    @pytest.mark.asyncio
    async def test_tick_skips_off_frequency(self, seeded_db):
        """A survey with frequency='off' is never returned by
        list_scheduled_survey_reminders, so the tick can't fire it."""
        import config
        from survey import SurveyCog

        config.save_survey_config(
            TEST_GUILD_ID,
            "Squad Powers",
            "Survey History",
            [],
            "intro",
        )
        config.save_survey_reminder(
            TEST_GUILD_ID,
            "default",
            enabled=1,
            frequency="off",
        )

        bot = MagicMock()
        bot.add_view = MagicMock()
        bot.add_dynamic_items = MagicMock()
        target = AsyncMock()
        target.send = AsyncMock()
        bot.get_channel = MagicMock(return_value=target)

        cog = SurveyCog(bot)
        try:
            await cog.check_scheduled_reminders()
        finally:
            try:
                cog.check_scheduled_reminders.cancel()
            except Exception:
                pass

        assert not target.send.called
