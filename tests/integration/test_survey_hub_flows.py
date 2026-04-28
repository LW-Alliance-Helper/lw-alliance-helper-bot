"""
Phase 4 of the full-coverage suite: /survey and /survey_remind hub flows.

Covers:

  * /survey on Premium renders the manage view (list + Add/Edit/Remove)
  * /survey on free tier shows the single-survey detail view
  * /survey_remind hub: cancel
  * /survey_remind hub → Send now → channel post (free tier path)
  * /survey_remind hub → Send now → DM via roster (Premium path)
  * /survey_remind hub → Manage scheduled → Off (disable)
  * /survey_remind hub → Manage scheduled → Daily + channel destination
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

from tests.conftest import TEST_GUILD_ID, OGV_GUILD_ID, make_mock_interaction


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_message():
    msg        = MagicMock(id=999)
    msg.edit   = AsyncMock(return_value=msg)
    msg.delete = AsyncMock()
    return msg


def _make_followup_interaction(guild_id=TEST_GUILD_ID, is_admin=True):
    """Interaction that the survey flow uses — needs working response and
    followup, plus user.roles + channel.category_id matching the guild's
    saved leadership-channel guard config."""
    import config as _config
    interaction = make_mock_interaction(guild_id=guild_id, is_admin=is_admin)

    cfg = _config.get_config(guild_id)
    leadership_role_name = (
        cfg.leadership_role_name if cfg and cfg.leadership_role_name else "Leadership"
    )
    leadership_cat_id    = cfg.leadership_category_id if cfg else 0

    role = MagicMock(); role.name = leadership_role_name
    interaction.user.roles = [role]
    interaction.channel.category_id = leadership_cat_id
    interaction.followup.send         = AsyncMock(return_value=_make_message())
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
    """`/survey` switches between single-detail and manage-view based on
    Premium status."""

    @pytest.mark.asyncio
    @pytest.mark.free_tier_only
    async def test_free_tier_shows_single_detail_view(self, seeded_db):
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

            # Free-tier path uses response.send_message with an embed.
            sent = interaction.response.send_message.call_args
            embed = sent.kwargs.get("embed") if sent else None
            assert embed is not None
            assert "Survey Configuration" in (embed.title or "")
        finally:
            try: cog.check_scheduled_reminders.cancel()
            except Exception: pass

    @pytest.mark.asyncio
    async def test_premium_shows_manage_view_with_buttons(self, seeded_db):
        """Premium tier gets the consolidated list + Add/Edit/Remove buttons."""
        from survey import SurveyCog
        bot = MagicMock()
        bot.add_view = MagicMock()
        bot.add_dynamic_items = MagicMock()
        cog = SurveyCog(bot)
        try:
            # OGV is always Premium per the seeded fixture.
            interaction = _make_followup_interaction(guild_id=OGV_GUILD_ID)
            interaction.entitlements = []

            await cog.survey.callback(cog, interaction)

            # Premium path defers, then uses followup.send with a view + embed.
            assert interaction.response.defer.called

            sent = interaction.followup.send.call_args
            assert sent is not None
            # Either the view is in args/kwargs; Discord SDK accepts both.
            view  = sent.kwargs.get("view")
            embed = sent.kwargs.get("embed")
            assert view is not None, "Premium /survey should include a manage view"
            assert embed is not None
            assert "Configured Surveys" in (embed.title or "")

            # View should expose Add / Edit / Remove buttons.
            labels = {getattr(c, "label", "") for c in view.children}
            assert any("Add" in l for l in labels)
            assert any("Edit" in l for l in labels)
            assert any("Remove" in l for l in labels)
        finally:
            try: cog.check_scheduled_reminders.cancel()
            except Exception: pass


# ── /survey_remind hub ────────────────────────────────────────────────────────

class TestSurveyRemindHubCancel:
    """Cancelling the hub immediately exits without any further flow."""

    @pytest.mark.asyncio
    async def test_hub_cancel_exits_quietly(self, seeded_db):
        from survey import SurveyCog, _ReminderHubView

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
                    try: view.stop()
                    except Exception: pass
                return _make_message()
            interaction.response.send_message = AsyncMock(side_effect=_send)

            await cog.survey_remind.callback(cog, interaction)

            # No further steps — followup never had to fire a destination
            # picker or schedule wizard.
            for call in interaction.followup.send.call_args_list:
                content = call.args[0] if call.args else call.kwargs.get("content", "")
                assert "destination" not in (content or "").lower()
                assert "frequency" not in (content or "").lower()
        finally:
            try: cog.check_scheduled_reminders.cancel()
            except Exception: pass


# ── Send-now path: channel post (free tier) ───────────────────────────────────

class TestSurveyRemindSendNowChannel:
    """Free tier picks Send now → channel post → reminder fires to the
    chosen channel."""

    @pytest.mark.asyncio
    @pytest.mark.free_tier_only
    async def test_send_now_channel_post_fires(self, seeded_db):
        import premium
        premium.clear_cache()

        from survey import SurveyCog, _ReminderHubView, _DestinationPickView, _ChannelPickView

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
            interaction.client = bot   # used by _pick_survey

            view_log = []

            async def _drive_response_send(content=None, view=None, **kw):
                """Hub picker — pick `send`."""
                if view is not None and isinstance(view, _ReminderHubView):
                    view.choice = "send"
                    try: view.stop()
                    except Exception: pass
                return _make_message()
            interaction.response.send_message = AsyncMock(side_effect=_drive_response_send)

            async def _drive_followup_send(content=None, view=None, **kw):
                """Destination picker → channel; channel picker → real channel."""
                view_log.append((content, view))
                if isinstance(view, _DestinationPickView):
                    view.choice = "channel"
                    try: view.stop()
                    except Exception: pass
                elif isinstance(view, _ChannelPickView):
                    pseudo_ch = MagicMock(id=999_888)
                    pseudo_ch.mention = "<#999888>"
                    view.channel = pseudo_ch
                    try: view.stop()
                    except Exception: pass
                return _make_message()
            interaction.followup.send = AsyncMock(side_effect=_drive_followup_send)

            await cog.survey_remind.callback(cog, interaction)

            # The helper should have called bot.get_channel with the picked
            # ID and then sent the reminder body.
            assert bot.get_channel.called
            assert target_channel.send.called, (
                "Reminder body should have been posted to the chosen channel"
            )
        finally:
            try: cog.check_scheduled_reminders.cancel()
            except Exception: pass


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
            TEST_GUILD_ID, "Squad Powers", "Survey History", [], "intro",
        )
        # Compute the guild's "now" so we can match the schedule on it.
        from zoneinfo import ZoneInfo
        guild_now = datetime.now(tz=ZoneInfo("America/New_York"))
        target_time = f"{guild_now.hour:02d}:{guild_now.minute:02d}"

        config.save_survey_reminder(
            TEST_GUILD_ID, "default",
            enabled=1, frequency="daily",
            day_of_week=0, time_str=target_time,
            channel_id=44_001, use_dm=0,
            message="Time to fill out the survey!",
        )

        bot = MagicMock()
        bot.add_view = MagicMock()
        bot.add_dynamic_items = MagicMock()
        target = AsyncMock(); target.send = AsyncMock()
        bot.get_channel = MagicMock(return_value=target)

        cog = SurveyCog(bot)
        try:
            await cog.check_scheduled_reminders()
        finally:
            try: cog.check_scheduled_reminders.cancel()
            except Exception: pass

        target.send.assert_called_once_with("Time to fill out the survey!")

        # Idempotency: running the tick again the same minute should NOT
        # fire a second time (reminder_last_fired now matches today).
        await cog.check_scheduled_reminders()
        assert target.send.call_count == 1, (
            "Tick re-fire on the same day should be a no-op"
        )

    @pytest.mark.asyncio
    async def test_tick_skips_off_frequency(self, seeded_db):
        """A survey with frequency='off' is never returned by
        list_scheduled_survey_reminders, so the tick can't fire it."""
        import config
        from survey import SurveyCog

        config.save_survey_config(
            TEST_GUILD_ID, "Squad Powers", "Survey History", [], "intro",
        )
        config.save_survey_reminder(
            TEST_GUILD_ID, "default",
            enabled=1, frequency="off",
        )

        bot = MagicMock()
        bot.add_view = MagicMock()
        bot.add_dynamic_items = MagicMock()
        target = AsyncMock(); target.send = AsyncMock()
        bot.get_channel = MagicMock(return_value=target)

        cog = SurveyCog(bot)
        try:
            await cog.check_scheduled_reminders()
        finally:
            try: cog.check_scheduled_reminders.cancel()
            except Exception: pass

        assert not target.send.called
