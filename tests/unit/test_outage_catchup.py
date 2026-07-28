"""Unit tests for the outage catch-up digest (#227).

Covers the surface-independent core (detection, time/format helpers, digest
rendering, the interactive view) and each surface adapter's
missed/catchable/dedup gating.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

import outage_catchup as oc

ET = ZoneInfo("America/New_York")
UTC = timezone.utc


def _cfg(tz="America/New_York", leadership_channel_id=999, setup_complete=1):
    c = MagicMock()
    c.timezone = tz
    c.leadership_channel_id = leadership_channel_id
    c.setup_complete = setup_complete
    return c


# ── Detection ────────────────────────────────────────────────────────────────


class TestDetection:
    def test_widest_gap_sets_start(self):
        now = datetime(2026, 6, 5, 12, 10, tzinfo=UTC)
        snap = {
            "shiny_post": now - timedelta(hours=2),
            "survey_reminder": now - timedelta(hours=1),
            "train_reminder": now - timedelta(minutes=90),
            "storm_signup": now - timedelta(minutes=1),
        }
        w = oc.detect_outage_window(snap, now)
        assert w is not None
        assert w.start == now - timedelta(hours=2)  # earliest gapped
        assert w.end == now

    def test_no_gap_returns_none(self):
        now = datetime(2026, 6, 5, 12, 10, tzinfo=UTC)
        snap = {k: now - timedelta(minutes=1) for k in oc.HEARTBEAT_LOOPS}
        assert oc.detect_outage_window(snap, now) is None

    def test_fresh_db_all_none_returns_none(self):
        now = datetime(2026, 6, 5, 12, 10, tzinfo=UTC)
        snap = {k: None for k in oc.HEARTBEAT_LOOPS}
        assert oc.detect_outage_window(snap, now) is None

    def test_threshold_boundary_just_under_is_not_outage(self):
        now = datetime(2026, 6, 5, 12, 10, tzinfo=UTC)
        snap = {k: now - timedelta(minutes=5) for k in oc.HEARTBEAT_LOOPS}
        # exactly 5m is not > threshold
        assert oc.detect_outage_window(snap, now) is None

    def test_single_gapped_loop_still_detects(self):
        now = datetime(2026, 6, 5, 12, 10, tzinfo=UTC)
        snap = {k: now - timedelta(minutes=1) for k in oc.HEARTBEAT_LOOPS}
        snap["storm_signup"] = now - timedelta(hours=3)
        w = oc.detect_outage_window(snap, now)
        assert w is not None
        assert w.start == now - timedelta(hours=3)

    def test_none_loop_ignored_for_start(self):
        now = datetime(2026, 6, 5, 12, 10, tzinfo=UTC)
        snap = {
            "shiny_post": None,
            "survey_reminder": now - timedelta(hours=1),
            "train_reminder": None,
            "storm_signup": now - timedelta(hours=2),
        }
        w = oc.detect_outage_window(snap, now)
        assert w.start == now - timedelta(hours=2)


# ── Time / format helpers ────────────────────────────────────────────────────


class TestFormatting:
    def test_fmt_clock_no_tz(self):
        assert oc.fmt_clock(datetime(2026, 6, 5, 8, 10, tzinfo=ET), with_tz=False) == "8:10 AM"

    def test_fmt_clock_with_tz(self):
        assert oc.fmt_clock(datetime(2026, 6, 5, 8, 10, tzinfo=ET), with_tz=True) == "8:10 AM EDT"

    def test_fmt_clock_midnight_and_noon(self):
        assert oc.fmt_clock(datetime(2026, 6, 5, 0, 0, tzinfo=ET), with_tz=False) == "12:00 AM"
        assert oc.fmt_clock(datetime(2026, 6, 5, 12, 0, tzinfo=ET), with_tz=False) == "12:00 PM"

    def test_humanize(self):
        assert oc.humanize_duration(timedelta(hours=5, minutes=40)) == "about 5h 40m"
        assert oc.humanize_duration(timedelta(hours=3)) == "about 3h"
        assert oc.humanize_duration(timedelta(minutes=42)) == "about 42m"

    def test_was_missed_inside_window(self):
        w = oc.OutageWindow(
            start=datetime(2026, 6, 5, 6, 30, tzinfo=UTC),
            end=datetime(2026, 6, 5, 12, 10, tzinfo=UTC),
        )
        sched = datetime(2026, 6, 5, 4, 0, tzinfo=ET)  # 08:00 UTC — inside
        assert oc._was_missed(sched, w) is True

    def test_was_missed_before_window(self):
        w = oc.OutageWindow(
            start=datetime(2026, 6, 5, 6, 30, tzinfo=UTC),
            end=datetime(2026, 6, 5, 12, 10, tzinfo=UTC),
        )
        sched = datetime(2026, 6, 5, 1, 0, tzinfo=ET)  # 05:00 UTC — before
        assert oc._was_missed(sched, w) is False

    def test_was_missed_after_window(self):
        w = oc.OutageWindow(
            start=datetime(2026, 6, 5, 6, 30, tzinfo=UTC),
            end=datetime(2026, 6, 5, 12, 10, tzinfo=UTC),
        )
        sched = datetime(2026, 6, 5, 9, 0, tzinfo=ET)  # 13:00 UTC — after
        assert oc._was_missed(sched, w) is False


# ── Digest rendering ─────────────────────────────────────────────────────────


class TestRenderDigest:
    def _window(self):
        # 02:30 -> 08:10 ET, rendered in ET
        return oc.OutageWindow(
            start=datetime(2026, 6, 5, 6, 30, tzinfo=UTC),  # 02:30 EDT
            end=datetime(2026, 6, 5, 12, 10, tzinfo=UTC),  # 08:10 EDT
        )

    def test_header_copy(self):
        body = oc.render_digest(self._window(), ET, [])
        assert "📋 It looks like we were offline from roughly 2:30 AM to 8:10 AM EDT" in body
        assert "(about 5h 40m)" in body
        assert "sorry about that!" in body
        assert "Items that are still relevant if you want to send them today:" in body

    def test_item_row(self):
        item = oc.MissedItem(
            surface="shiny_post",
            title="Shiny tasks post",
            scheduled_local=datetime(2026, 6, 5, 7, 0, tzinfo=ET),
            destination="sent to #announcements",
            fire=AsyncMock(return_value=True),
        )
        body = oc.render_digest(self._window(), ET, [item])
        assert "☐ Shiny tasks post (scheduled for 7:00 AM, sent to #announcements)" in body

    def test_no_em_dashes(self):
        item = oc.MissedItem(
            surface="shiny_post",
            title="Shiny tasks post",
            scheduled_local=datetime(2026, 6, 5, 7, 0, tzinfo=ET),
            destination="sent to #announcements",
            fire=AsyncMock(return_value=True),
        )
        body = oc.render_digest(self._window(), ET, [item])
        assert "—" not in body


# ── Shiny adapter ────────────────────────────────────────────────────────────


class TestScanShiny:
    def _window(self):
        return oc.OutageWindow(
            start=datetime(2026, 6, 5, 6, 30, tzinfo=UTC),  # 02:30 EDT
            end=datetime(2026, 6, 5, 12, 10, tzinfo=UTC),  # 08:10 EDT
        )

    def _scfg(self, **over):
        base = {
            "enabled": 1,
            "channel_id": 555,
            "post_time": "07:00",  # 07:00 EDT — inside the window
            "server_min": 100,
            "server_max": 200,
            "message_template": "",
            "last_posted_date": "",
        }
        base.update(over)
        return base

    @pytest.mark.asyncio
    async def test_missed_and_catchable_surfaces_item(self):
        bot = MagicMock()
        bot.get_channel.return_value = MagicMock(name="announcements")
        bot.get_channel.return_value.name = "announcements"
        guild = MagicMock(id=1)
        with (
            patch("config.get_shiny_tasks_config", return_value=self._scfg()),
            patch("config.get_shiny_task_servers_in_range", return_value=[{"server_number": 150}]),
            patch("shiny_tasks.build_announcement_for_guild", return_value="✨ shiny body"),
        ):
            items = await oc.scan_shiny(bot, guild, _cfg(), self._window())
        assert len(items) == 1
        assert items[0].title == "Shiny tasks post"
        assert items[0].destination == "sent to #announcements"

    @pytest.mark.asyncio
    async def test_disabled_returns_nothing(self):
        bot, guild = MagicMock(), MagicMock(id=1)
        with patch("config.get_shiny_tasks_config", return_value=self._scfg(enabled=0)):
            assert await oc.scan_shiny(bot, guild, _cfg(), self._window()) == []

    @pytest.mark.asyncio
    async def test_already_posted_today_returns_nothing(self):
        bot, guild = MagicMock(), MagicMock(id=1)
        with patch(
            "config.get_shiny_tasks_config",
            return_value=self._scfg(last_posted_date="2026-06-05"),
        ):
            assert await oc.scan_shiny(bot, guild, _cfg(), self._window()) == []

    @pytest.mark.asyncio
    async def test_scheduled_after_window_not_surfaced(self):
        # post_time 09:00 EDT is after we came back at 08:10 — not yet due.
        bot, guild = MagicMock(), MagicMock(id=1)
        with patch("config.get_shiny_tasks_config", return_value=self._scfg(post_time="09:00")):
            assert await oc.scan_shiny(bot, guild, _cfg(), self._window()) == []

    @pytest.mark.asyncio
    async def test_no_shinies_today_stamps_and_skips(self):
        bot, guild = MagicMock(), MagicMock(id=1)
        with (
            patch("config.get_shiny_tasks_config", return_value=self._scfg()),
            patch("config.get_shiny_task_servers_in_range", return_value=[]),
            patch("shiny_tasks.build_announcement_for_guild", return_value=None),
            patch("config.mark_shiny_tasks_posted") as mark,
        ):
            items = await oc.scan_shiny(bot, guild, _cfg(), self._window())
        assert items == []
        mark.assert_called_once_with(1, "2026-06-05")

    @pytest.mark.asyncio
    async def test_fire_posts_and_stamps(self):
        chan = AsyncMock()
        chan.name = "announcements"
        bot = MagicMock()
        bot.get_channel.return_value = chan
        guild = MagicMock(id=1)
        with (
            patch("config.get_shiny_tasks_config", return_value=self._scfg()),
            patch("config.get_shiny_task_servers_in_range", return_value=[{"server_number": 150}]),
            patch("shiny_tasks.build_announcement_for_guild", return_value="✨ body"),
        ):
            items = await oc.scan_shiny(bot, guild, _cfg(), self._window())
            with patch("config.mark_shiny_tasks_posted") as mark:
                ok = await items[0].fire()
        assert ok is True
        chan.send.assert_awaited_once_with("✨ body")
        mark.assert_called_once_with(1, "2026-06-05")

    @pytest.mark.asyncio
    async def test_shiny_cycle_uses_server_date_not_guild_local_date(self):
        """#364 regression: a post_time near the guild's local midnight can
        already be past the Last War server (UTC-2) reset, in which case
        `today` handed to build_announcement_for_guild must be the *next*
        server day, not the guild-local calendar day `scheduled` carries —
        same #330 bug class as the live shiny loop."""
        from datetime import date

        # 21:30 EDT -> 02:30 EDT window straddling the 22:00 post time on
        # Fri 2026-06-05 local / Sat 2026-06-06 Server Time (UTC-2).
        window = oc.OutageWindow(
            start=datetime(2026, 6, 6, 1, 30, tzinfo=UTC),  # 21:30 EDT 6/5
            end=datetime(2026, 6, 6, 2, 30, tzinfo=UTC),  # 22:30 EDT 6/5
        )
        bot = MagicMock()
        bot.get_channel.return_value = MagicMock(name="announcements")
        guild = MagicMock(id=1)
        build_mock = MagicMock(return_value="✨ body")
        with (
            patch("config.get_shiny_tasks_config", return_value=self._scfg(post_time="22:00")),
            patch("config.get_shiny_task_servers_in_range", return_value=[{"server_number": 150}]),
            patch("shiny_tasks.build_announcement_for_guild", build_mock),
        ):
            await oc.scan_shiny(bot, guild, _cfg(), window)

        build_mock.assert_called_once()
        assert build_mock.call_args.kwargs["today"] == date(2026, 6, 6)


def _window_friday():
    # 02:30 -> 08:10 EDT on Fri 2026-06-05
    return oc.OutageWindow(
        start=datetime(2026, 6, 5, 6, 30, tzinfo=UTC),
        end=datetime(2026, 6, 5, 12, 10, tzinfo=UTC),
    )


# ── Survey adapter ───────────────────────────────────────────────────────────


class TestScanSurvey:
    def _entry(self, **over):
        base = {
            "guild_id": 1,
            "survey_id": "default",
            "reminder_frequency": "daily",
            "reminder_time": "07:00",  # 07:00 EDT — inside window
            "reminder_day_of_week": 4,  # Friday
            "reminder_channel_id": 777,
            "reminder_use_dm": 0,
            "reminder_message": "",
            "reminder_last_fired": "",
            "survey_name": "weekly check-in",
        }
        base.update(over)
        return base

    @pytest.mark.asyncio
    async def test_channel_reminder_surfaces(self):
        bot = MagicMock()
        ch = MagicMock()
        ch.name = "surveys"
        bot.get_channel.return_value = ch
        with (
            patch("config.list_scheduled_survey_reminders", return_value=[self._entry()]),
            patch("config.get_survey", return_value={"survey_name": "weekly check-in"}),
            patch("survey._default_reminder_body", return_value="body"),
        ):
            items = await oc.scan_survey(bot, MagicMock(id=1), _cfg(), _window_friday())
        assert len(items) == 1
        assert items[0].title == 'Survey reminder "weekly check-in"'
        assert items[0].destination == "sent to #surveys"

    @pytest.mark.asyncio
    async def test_dm_reminder_title_and_destination(self):
        bot = MagicMock()
        with (
            patch(
                "config.list_scheduled_survey_reminders",
                return_value=[self._entry(reminder_use_dm=1, reminder_channel_id=0)],
            ),
            patch("config.get_survey", return_value={"survey_name": "weekly check-in"}),
            patch("survey._default_reminder_body", return_value="body"),
        ):
            items = await oc.scan_survey(bot, MagicMock(id=1), _cfg(), _window_friday())
        assert items[0].title == 'Survey reminder "weekly check-in": DM to your roster'
        assert items[0].destination == "sent individually to members"

    @pytest.mark.asyncio
    async def test_already_fired_skipped(self):
        bot = MagicMock()
        with patch(
            "config.list_scheduled_survey_reminders",
            return_value=[self._entry(reminder_last_fired="2026-06-05")],
        ):
            assert await oc.scan_survey(bot, MagicMock(id=1), _cfg(), _window_friday()) == []

    @pytest.mark.asyncio
    async def test_weekly_wrong_day_skipped(self):
        bot = MagicMock()
        with patch(
            "config.list_scheduled_survey_reminders",
            return_value=[self._entry(reminder_frequency="weekly", reminder_day_of_week=0)],
        ):
            # window day is Friday (4), target is Monday (0)
            assert await oc.scan_survey(bot, MagicMock(id=1), _cfg(), _window_friday()) == []

    @pytest.mark.asyncio
    async def test_other_guild_ignored(self):
        bot = MagicMock()
        with patch(
            "config.list_scheduled_survey_reminders", return_value=[self._entry(guild_id=999)]
        ):
            assert await oc.scan_survey(bot, MagicMock(id=1), _cfg(), _window_friday()) == []

    @pytest.mark.asyncio
    async def test_dm_fire_rechecks_premium_and_skips_when_lapsed(self):
        bot = MagicMock()
        with (
            patch(
                "config.list_scheduled_survey_reminders",
                return_value=[self._entry(reminder_use_dm=1, reminder_channel_id=0)],
            ),
            patch("config.get_survey", return_value={"survey_name": "x"}),
            patch("survey._default_reminder_body", return_value="body"),
        ):
            items = await oc.scan_survey(bot, MagicMock(id=1), _cfg(), _window_friday())
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=False)),
            patch("survey._send_reminder_via_dm", new=AsyncMock()) as dm_send,
            patch("config.update_survey_reminder_last_fired") as stamp,
        ):
            ok = await items[0].fire()
        assert ok is False
        dm_send.assert_not_awaited()
        stamp.assert_not_called()

    @pytest.mark.asyncio
    async def test_channel_fire_posts_and_stamps(self):
        bot = MagicMock()
        ch = MagicMock()
        ch.name = "surveys"
        bot.get_channel.return_value = ch
        with (
            patch("config.list_scheduled_survey_reminders", return_value=[self._entry()]),
            patch("config.get_survey", return_value={"survey_name": "x"}),
            patch("survey._default_reminder_body", return_value="body"),
        ):
            items = await oc.scan_survey(bot, MagicMock(id=1), _cfg(), _window_friday())
        with (
            patch("survey._send_reminder_to_channel", new=AsyncMock(return_value=True)) as send,
            patch("config.update_survey_reminder_last_fired") as stamp,
        ):
            ok = await items[0].fire()
        assert ok is True
        send.assert_awaited_once()
        stamp.assert_called_once_with(1, "default", "2026-06-05")


# ── Birthday adapter ─────────────────────────────────────────────────────────


class TestScanBirthday:
    def _bcfg(self, **over):
        base = {
            "enabled": 1,
            "reminders_enabled": 1,
            "reminder_time": "07:00",
            "reminder_channel_id": 321,
            "tab_name": "Birthdays",
            "dm_message": "",
        }
        base.update(over)
        return base

    @pytest.mark.asyncio
    async def test_surfaces_todays_birthdays(self):
        bot = MagicMock()
        ch = MagicMock()
        ch.name = "general"
        bot.get_channel.return_value = ch
        members = [
            {"name": "Sally", "month": 6, "day": 5, "discord_id": None},
            {"name": "Bob", "month": 1, "day": 1, "discord_id": None},
        ]
        with (
            patch("config.get_birthday_config", return_value=self._bcfg()),
            patch("train.load_birthdays", return_value=members),
            patch("train_cog.DEFAULT_BIRTHDAY_DM", "hb {name}"),
        ):
            items = await oc.scan_birthday(bot, MagicMock(id=1), _cfg(), _window_friday())
        assert len(items) == 1
        assert items[0].title == "Birthday announcement: Sally"
        assert items[0].destination == "sent to #general"

    @pytest.mark.asyncio
    async def test_no_birthdays_today_no_row(self):
        bot = MagicMock()
        bot.get_channel.return_value = MagicMock(name="general")
        with (
            patch("config.get_birthday_config", return_value=self._bcfg()),
            patch("train.load_birthdays", return_value=[{"name": "Bob", "month": 1, "day": 1}]),
        ):
            assert await oc.scan_birthday(bot, MagicMock(id=1), _cfg(), _window_friday()) == []

    @pytest.mark.asyncio
    async def test_reminders_disabled_skipped(self):
        bot = MagicMock()
        with patch("config.get_birthday_config", return_value=self._bcfg(reminders_enabled=0)):
            assert await oc.scan_birthday(bot, MagicMock(id=1), _cfg(), _window_friday()) == []

    @pytest.mark.asyncio
    async def test_fire_posts_each_birthday(self):
        ch = AsyncMock()
        ch.name = "general"
        bot = MagicMock()
        bot.get_channel.return_value = ch
        members = [{"name": "Sally", "month": 6, "day": 5, "discord_id": 42}]
        with (
            patch("config.get_birthday_config", return_value=self._bcfg()),
            patch("train.load_birthdays", return_value=members),
            patch("train_cog.DEFAULT_BIRTHDAY_DM", "hb {name}"),
            patch("train_cog._render_dm_body", return_value="hb Sally"),
        ):
            items = await oc.scan_birthday(bot, MagicMock(id=1), _cfg(), _window_friday())
            with patch("dm.send_dm_to_id", new=AsyncMock()) as dm_send:
                ok = await items[0].fire()
        assert ok is True
        ch.send.assert_awaited_once_with("🎂 Today is <@42>'s birthday!")
        dm_send.assert_awaited_once()


# ── Train reminder adapter ───────────────────────────────────────────────────


class TestScanTrainReminder:
    def _tcfg(self, **over):
        base = {
            "rotation_enabled": 0,
            "reminders_enabled": 1,
            "reminder_time": "07:00",
            "reminder_channel_id": 555,
            "dm_message": "",
        }
        base.update(over)
        return base

    @pytest.mark.asyncio
    async def test_surfaces_when_conductor_scheduled(self):
        bot = MagicMock()
        ch = MagicMock()
        ch.name = "train"
        bot.get_channel.return_value = ch
        with (
            patch("config.get_train_config", return_value=self._tcfg()),
            patch("train.load_schedule", return_value={"2026-06-05": {"name": "alice"}}),
        ):
            items = await oc.scan_train_reminder(bot, MagicMock(id=1), _cfg(), _window_friday())
        assert len(items) == 1
        assert items[0].title == "Train reminder: alice"

    @pytest.mark.asyncio
    async def test_rotation_guild_skipped(self):
        bot = MagicMock()
        with patch("config.get_train_config", return_value=self._tcfg(rotation_enabled=1)):
            assert (
                await oc.scan_train_reminder(bot, MagicMock(id=1), _cfg(), _window_friday()) == []
            )

    @pytest.mark.asyncio
    async def test_no_conductor_today_no_row(self):
        bot = MagicMock()
        bot.get_channel.return_value = MagicMock()
        with (
            patch("config.get_train_config", return_value=self._tcfg()),
            patch("train.load_schedule", return_value={}),
        ):
            assert (
                await oc.scan_train_reminder(bot, MagicMock(id=1), _cfg(), _window_friday()) == []
            )

    @pytest.mark.asyncio
    async def test_fire_uses_current_hub_hint(self):
        ch = AsyncMock()
        ch.name = "train"
        bot = MagicMock()
        bot.get_channel.return_value = ch
        with (
            patch("config.get_train_config", return_value=self._tcfg()),
            patch("train.load_schedule", return_value={"2026-06-05": {"name": "alice"}}),
        ):
            items = await oc.scan_train_reminder(bot, MagicMock(id=1), _cfg(), _window_friday())
            with (
                patch("dm.mention_or_name", new=AsyncMock(return_value="**alice**")),
                patch("dm.send_dm", new=AsyncMock()),
                patch("train_cog._render_dm_body", return_value="x"),
                patch("train_cog.DEFAULT_TRAIN_DM", "x {name}"),
            ):
                ok = await items[0].fire()
        assert ok is True
        sent = ch.send.await_args.args[0]
        assert "/train` → 📋 Schedule overview → 📋 Generate Prompt" in sent
        assert "/train overview" not in sent

    @pytest.mark.asyncio
    async def test_schedule_lookup_uses_server_date_not_guild_local_date(self):
        """#364 regression: same #318 bug class as the live train loop — a
        22:00-local reminder time is already past the Last War server
        (UTC-2) reset, so the schedule lookup must key on the *next*
        server day, not the guild-local calendar day."""
        window = oc.OutageWindow(
            start=datetime(2026, 6, 6, 1, 30, tzinfo=UTC),  # 21:30 EDT 6/5
            end=datetime(2026, 6, 6, 2, 30, tzinfo=UTC),  # 22:30 EDT 6/5
        )
        bot = MagicMock()
        ch = MagicMock()
        ch.name = "train"
        bot.get_channel.return_value = ch
        with (
            patch("config.get_train_config", return_value=self._tcfg(reminder_time="22:00")),
            # Only the server-date (6/6) row exists — a guild-local-date (6/5)
            # lookup would find nothing and wrongly surface no reminder.
            patch("train.load_schedule", return_value={"2026-06-06": {"name": "bob"}}),
        ):
            items = await oc.scan_train_reminder(bot, MagicMock(id=1), _cfg(), window)

        assert len(items) == 1
        assert items[0].title == "Train reminder: bob"


# ── Storm sign-up adapter ────────────────────────────────────────────────────


class TestScanStormSignup:
    def _window_monday(self):
        # Mon 2026-06-01, 02:30 -> 08:10 EDT
        return oc.OutageWindow(
            start=datetime(2026, 6, 1, 6, 30, tzinfo=UTC),
            end=datetime(2026, 6, 1, 12, 10, tzinfo=UTC),
        )

    def _row(self, **over):
        base = {"guild_id": 1, "event_type": "DS", "poll_day_of_week": 0, "signup_time": "07:00"}
        base.update(over)
        return base

    @pytest.mark.asyncio
    async def test_surfaces_when_due_and_catchable(self):
        bot = MagicMock()
        ch = MagicMock()
        ch.name = "storm-signup"
        bot.get_channel.return_value = ch
        with (
            patch("config.get_scheduled_storm_rows", return_value=[self._row()]),
            patch(
                "config.get_structured_storm_config",
                return_value={"structured_flow_enabled": 1, "signup_channel_id": 888},
            ),
            patch("storm_date_helpers.next_event_date", return_value="2026-06-05"),
        ):
            items = await oc.scan_storm_signup(bot, MagicMock(id=1), _cfg(), self._window_monday())
        assert len(items) == 1
        assert items[0].title == "Desert Storm sign-up poll"
        assert items[0].destination == "sent to #storm-signup"

    @pytest.mark.asyncio
    async def test_not_enabled_skipped(self):
        bot = MagicMock()
        with (
            patch("config.get_scheduled_storm_rows", return_value=[self._row()]),
            patch(
                "config.get_structured_storm_config", return_value={"structured_flow_enabled": 0}
            ),
            patch("storm_date_helpers.next_event_date", return_value="2026-06-05"),
        ):
            assert (
                await oc.scan_storm_signup(bot, MagicMock(id=1), _cfg(), self._window_monday())
                == []
            )

    @pytest.mark.asyncio
    async def test_inside_final_day_skipped(self):
        bot = MagicMock()
        bot.get_channel.return_value = MagicMock()
        with (
            patch("config.get_scheduled_storm_rows", return_value=[self._row()]),
            patch(
                "config.get_structured_storm_config",
                return_value={"structured_flow_enabled": 1, "signup_channel_id": 888},
            ),
            # event is the same day as the scheduled poll -> within final-day window
            patch("storm_date_helpers.next_event_date", return_value="2026-06-01"),
        ):
            assert (
                await oc.scan_storm_signup(bot, MagicMock(id=1), _cfg(), self._window_monday())
                == []
            )

    @pytest.mark.asyncio
    async def test_fire_rechecks_premium(self):
        bot = MagicMock()
        ch = MagicMock()
        ch.name = "s"
        bot.get_channel.return_value = ch
        with (
            patch("config.get_scheduled_storm_rows", return_value=[self._row()]),
            patch(
                "config.get_structured_storm_config",
                return_value={"structured_flow_enabled": 1, "signup_channel_id": 888},
            ),
            patch("storm_date_helpers.next_event_date", return_value="2026-06-05"),
        ):
            items = await oc.scan_storm_signup(bot, MagicMock(id=1), _cfg(), self._window_monday())
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=False)),
            patch("storm_signup_post.post_registration", new=AsyncMock()) as post,
        ):
            ok = await items[0].fire()
        assert ok is False
        post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fire_posts_when_premium(self):
        bot = MagicMock()
        ch = MagicMock()
        ch.name = "s"
        bot.get_channel.return_value = ch
        with (
            patch("config.get_scheduled_storm_rows", return_value=[self._row()]),
            patch(
                "config.get_structured_storm_config",
                return_value={"structured_flow_enabled": 1, "signup_channel_id": 888},
            ),
            patch("storm_date_helpers.next_event_date", return_value="2026-06-05"),
        ):
            items = await oc.scan_storm_signup(bot, MagicMock(id=1), _cfg(), self._window_monday())
        with (
            patch("premium.is_premium", new=AsyncMock(return_value=True)),
            patch(
                "storm_signup_post.post_registration", new=AsyncMock(return_value={"status": "ok"})
            ) as post,
        ):
            ok = await items[0].fire()
        assert ok is True
        post.assert_awaited_once()


# ── Event draft adapter ──────────────────────────────────────────────────────


class TestScanEventDraft:
    def _draft(self, draft_h=7, event_h=18, **over):
        base = {
            "event_list": [
                {
                    "key": "ds",
                    "name": "Desert Storm",
                    "dt": datetime(2026, 6, 5, event_h, 0, tzinfo=ET),
                    "blurb": "",
                }
            ],
            "draft_dt": datetime(2026, 6, 5, draft_h, 0, tzinfo=ET),
            "event_date": datetime(2026, 6, 5).date(),
            "event_key": "event-1-2026-06-05",
            "draft_channel_id": 444,
            "announcement_channel_id": 555,
            "five_min_warning": True,
        }
        base.update(over)
        return base

    @pytest.mark.asyncio
    async def test_surfaces_missed_catchable_draft(self):
        bot = MagicMock()
        ch = MagicMock()
        ch.name = "event-drafts"
        bot.get_channel.return_value = ch
        with patch("scheduler.iter_guild_event_drafts", return_value=[self._draft()]):
            items = await oc.scan_event_draft(bot, MagicMock(id=1), _cfg(), _window_friday())
        assert len(items) == 1
        assert items[0].title == "Event draft: Desert Storm"
        assert items[0].destination == "sent to #event-drafts"

    @pytest.mark.asyncio
    async def test_too_late_for_draft_skipped(self):
        # event starts 08:30 EDT; we're back at 08:10, inside the 30-min cutoff
        bot = MagicMock()
        bot.get_channel.return_value = MagicMock()
        with patch("scheduler.iter_guild_event_drafts", return_value=[self._draft(event_h=8)]):
            # event_h=8 -> 08:00 start, draft 07:00; 08:10 > 07:30 -> too late
            items = await oc.scan_event_draft(bot, MagicMock(id=1), _cfg(), _window_friday())
        assert items == []

    @pytest.mark.asyncio
    async def test_draft_after_window_not_missed(self):
        # draft posts 12:00, after we came back at 08:10 -> not missed
        bot = MagicMock()
        bot.get_channel.return_value = MagicMock()
        with patch("scheduler.iter_guild_event_drafts", return_value=[self._draft(draft_h=12)]):
            items = await oc.scan_event_draft(bot, MagicMock(id=1), _cfg(), _window_friday())
        assert items == []

    @pytest.mark.asyncio
    async def test_fire_calls_post_editor(self):
        bot = MagicMock()
        ch = MagicMock()
        ch.name = "event-drafts"
        bot.get_channel.return_value = ch
        with patch("scheduler.iter_guild_event_drafts", return_value=[self._draft()]):
            items = await oc.scan_event_draft(bot, MagicMock(id=1), _cfg(), _window_friday())
        with patch("scheduler.post_editor", new=AsyncMock()) as pe:
            ok = await items[0].fire()
        assert ok is True
        pe.assert_awaited_once()
        # event_key threaded through to the editor post
        assert pe.await_args.args[2] == "event-1-2026-06-05"


# ── Orchestrator ─────────────────────────────────────────────────────────────


class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_no_outage_posts_nothing(self):
        bot = MagicMock()
        now = datetime(2026, 6, 5, 12, 10, tzinfo=UTC)
        snap = {k: now - timedelta(minutes=1) for k in oc.HEARTBEAT_LOOPS}
        with patch.object(oc, "_post_digest", new=AsyncMock()) as pd:
            await oc.run_catchup_scan(bot, snap, now)
        pd.assert_not_called()

    @pytest.mark.asyncio
    async def test_guild_without_items_gets_no_digest(self):
        guild = MagicMock(id=1)
        bot = MagicMock()
        bot.guilds = [guild]
        now = datetime(2026, 6, 5, 12, 10, tzinfo=UTC)
        snap = {k: now - timedelta(hours=2) for k in oc.HEARTBEAT_LOOPS}
        with (
            patch("config.get_config", return_value=_cfg()),
            patch.object(oc, "_scan_guild", new=AsyncMock(return_value=[])),
            patch.object(oc, "_post_digest", new=AsyncMock()) as pd,
        ):
            await oc.run_catchup_scan(bot, snap, now)
        pd.assert_not_called()

    @pytest.mark.asyncio
    async def test_guild_with_items_gets_digest(self):
        guild = MagicMock(id=1)
        bot = MagicMock()
        bot.guilds = [guild]
        now = datetime(2026, 6, 5, 12, 10, tzinfo=UTC)
        snap = {k: now - timedelta(hours=2) for k in oc.HEARTBEAT_LOOPS}
        item = oc.MissedItem(
            "shiny_post",
            "Shiny tasks post",
            datetime(2026, 6, 5, 7, 0, tzinfo=ET),
            "sent to #x",
            AsyncMock(return_value=True),
        )
        with (
            patch("config.get_config", return_value=_cfg()),
            patch.object(oc, "_scan_guild", new=AsyncMock(return_value=[item])),
            patch.object(oc, "_post_digest", new=AsyncMock()) as pd,
        ):
            await oc.run_catchup_scan(bot, snap, now)
        pd.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_unconfigured_guild_skipped(self):
        guild = MagicMock(id=1)
        bot = MagicMock()
        bot.guilds = [guild]
        now = datetime(2026, 6, 5, 12, 10, tzinfo=UTC)
        snap = {k: now - timedelta(hours=2) for k in oc.HEARTBEAT_LOOPS}
        with (
            patch("config.get_config", return_value=_cfg(setup_complete=0)),
            patch.object(oc, "_scan_guild", new=AsyncMock(return_value=[])) as sg,
        ):
            await oc.run_catchup_scan(bot, snap, now)
        sg.assert_not_called()


# ── Digest view ──────────────────────────────────────────────────────────────


class TestCatchupView:
    def _items(self, n=2):
        return [
            oc.MissedItem(
                surface=f"s{i}",
                title=f"Item {i}",
                scheduled_local=datetime(2026, 6, 5, 7, i, tzinfo=ET),
                destination="sent to #x",
                fire=AsyncMock(return_value=True),
            )
            for i in range(n)
        ]

    def test_view_builds_select_with_all_options(self):
        items = self._items(3)
        view = oc.OutageCatchupView(items)
        selects = [c for c in view.children if isinstance(c, discord_select_type())]
        assert len(selects) == 1
        assert len(selects[0].options) == 3
        assert selects[0].max_values == 3

    @pytest.mark.asyncio
    async def test_send_all_fires_every_item(self):
        items = self._items(2)
        view = oc.OutageCatchupView(items)
        view.message = AsyncMock()
        view.message.content = "digest"
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        await view.send_all.callback(interaction)
        for it in items:
            it.fire.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_selected_fires_only_selected(self):
        items = self._items(3)
        view = oc.OutageCatchupView(items)
        view.message = AsyncMock()
        view.message.content = "digest"
        view._selected = {1}
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        await view.send_selected.callback(interaction)
        items[0].fire.assert_not_awaited()
        items[1].fire.assert_awaited_once()
        items[2].fire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_send_selected_with_no_selection_warns(self):
        items = self._items(2)
        view = oc.OutageCatchupView(items)
        view.message = AsyncMock()
        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()
        await view.send_selected.callback(interaction)
        interaction.response.send_message.assert_awaited_once()
        items[0].fire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_dismiss_fires_nothing(self):
        items = self._items(2)
        view = oc.OutageCatchupView(items)
        view.message = AsyncMock()
        view.message.content = "digest"
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        await view.dismiss.callback(interaction)
        for it in items:
            it.fire.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_double_action_guarded(self):
        items = self._items(2)
        view = oc.OutageCatchupView(items)
        view.message = AsyncMock()
        view.message.content = "digest"
        i1 = MagicMock()
        i1.response.defer = AsyncMock()
        await view.send_all.callback(i1)
        # second action should be refused
        i2 = MagicMock()
        i2.response.send_message = AsyncMock()
        await view.send_selected.callback(i2)
        # each fire still only called once from the first action
        for it in items:
            it.fire.assert_awaited_once()


def discord_select_type():
    import discord

    return discord.ui.Select


# needed by view tests that reference discord at module level
import discord  # noqa: E402
