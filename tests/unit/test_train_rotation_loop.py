"""
Tests for `TrainCog.check_rotation` — the #55 background loop driving the
weekly draft and the daily conductor confirmation.

The loop is the riskiest untested surface: a mis-fire either spams a channel
or silently stops the alliance's daily train post. Coverage:

  * Weekly draft posts only on the configured draft day + time.
  * Daily confirmation posts only at the configured reminder time, and only
    when today has a `scheduled` history row (not already posted / skipped).
  * `rotation_enabled = 0` → the loop does nothing for that guild.
  * Per-day dedup sets prevent a double-post.
"""

import os
import sys
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import train_rotation as tr

GUILD_ID = 12345
LEAD_CHAN = 1111
PUBLIC_CHAN = 2222
ET = ZoneInfo("America/New_York")

# Rotation reuses the train reminder_time for both the weekly draft (on the
# draft day) and the daily confirmation, so both fire at 18:00 here.
SUNDAY_6PM = datetime(2026, 6, 7, 18, 0, tzinfo=ET)  # 2026-06-07 is a Sunday
MONDAY_6PM = datetime(2026, 6, 1, 18, 0, tzinfo=ET)  # 2026-06-01 is a Monday


def _make_cog():
    import train_cog

    bot = MagicMock()
    bot.guilds = []
    cog = train_cog.TrainCog.__new__(train_cog.TrainCog)
    cog.bot = bot
    cog.last_rotation_date = None
    cog.rotation_draft_fired = set()
    cog.rotation_confirm_fired = set()
    return cog


def _cfg():
    cfg = MagicMock()
    cfg.guild_id = GUILD_ID
    cfg.setup_complete = True
    cfg.leadership_channel_id = LEAD_CHAN
    cfg.timezone = "America/New_York"
    cfg.leadership_role_name = "Leadership"
    return cfg


def _tcfg(**over):
    base = {
        "rotation_enabled": 1,
        "history_tab": "Train History",
        "member_rules_tab": "Train Member Rules",
        "day_rules_tab": "Train Day Rules",
        "rotation_public_channel_id": PUBLIC_CHAN,
        "weekly_draft_day": 6,  # Sunday
        # Rotation reuses the train reminder channel + time.
        "reminder_channel_id": LEAD_CHAN,
        "reminder_time": "18:00",
        "rule_type_roles": {},
        "counted_reasons": "",
        "active_schedule_preset": "Standard Week",
    }
    base.update(over)
    return base


def _guild():
    g = MagicMock()
    g.id = GUILD_ID
    g.name = "Test Alliance"
    return g


async def _run(cog, *, now, tcfg=None, history=None, draft=None, channel=None):
    import train_cog

    cog.bot.guilds = [_guild()]
    chan = channel or AsyncMock()
    cog.bot.get_channel = MagicMock(return_value=chan)

    fake_dt = MagicMock(wraps=datetime)
    fake_dt.now = MagicMock(return_value=now)

    draft = (
        draft
        if draft is not None
        else [tr.DraftDay("2026-06-08", 0, tr.RULE_AUTO, "Alice", "auto")]
    )

    with (
        patch("train_cog.datetime", fake_dt),
        patch("config.get_config", return_value=_cfg()),
        patch("config.get_train_config", return_value=tcfg or _tcfg()),
        patch("train_rotation_ui.regenerate_week", MagicMock(return_value=draft)),
        patch("train_rotation.load_history", MagicMock(return_value=history or [])),
    ):
        await type(cog).check_rotation.coro(cog)
    return chan


# ── Weekly draft ─────────────────────────────────────────────────────────────


async def test_weekly_draft_posts_on_day_and_time():
    cog = _make_cog()
    chan = await _run(cog, now=SUNDAY_6PM)
    assert chan.send.await_count >= 1
    assert GUILD_ID in cog.rotation_draft_fired


async def test_weekly_draft_skips_wrong_time():
    cog = _make_cog()
    chan = await _run(cog, now=datetime(2026, 6, 7, 9, 0, tzinfo=ET))  # Sunday, wrong hour
    # Wrong time → no draft; 9am isn't the 8am confirm time either.
    assert chan.send.await_count == 0
    assert GUILD_ID not in cog.rotation_draft_fired


async def test_weekly_draft_dedup_prevents_double_post():
    cog = _make_cog()
    cog.last_rotation_date = SUNDAY_6PM.date()  # same day → no midnight reset
    cog.rotation_draft_fired = {GUILD_ID}  # already fired today
    chan = await _run(cog, now=SUNDAY_6PM)
    assert chan.send.await_count == 0


async def test_rotation_disabled_does_nothing():
    cog = _make_cog()
    chan = await _run(cog, now=SUNDAY_6PM, tcfg=_tcfg(rotation_enabled=0))
    assert chan.send.await_count == 0
    assert GUILD_ID not in cog.rotation_draft_fired


# ── Daily confirmation ───────────────────────────────────────────────────────


async def test_daily_confirm_posts_when_scheduled_row_exists():
    cog = _make_cog()
    history = [tr.HistoryRow("2026-06-01", "Alice", "auto", tr.STATUS_SCHEDULED)]
    chan = await _run(cog, now=MONDAY_6PM, history=history)
    assert chan.send.await_count >= 1
    assert GUILD_ID in cog.rotation_confirm_fired


async def test_daily_confirm_skips_when_already_posted():
    cog = _make_cog()
    history = [tr.HistoryRow("2026-06-01", "Alice", "auto", tr.STATUS_POSTED)]
    chan = await _run(cog, now=MONDAY_6PM, history=history)
    # Already posted today → no confirmation message.
    assert chan.send.await_count == 0


async def test_daily_confirm_skips_when_no_row_for_today():
    cog = _make_cog()
    history = [tr.HistoryRow("2026-05-30", "Alice", "auto", tr.STATUS_SCHEDULED)]
    chan = await _run(cog, now=MONDAY_6PM, history=history)
    assert chan.send.await_count == 0


async def test_daily_confirm_dedup_prevents_double_post():
    cog = _make_cog()
    cog.last_rotation_date = MONDAY_6PM.date()  # same day → no midnight reset
    cog.rotation_confirm_fired = {GUILD_ID}
    history = [tr.HistoryRow("2026-06-01", "Alice", "auto", tr.STATUS_SCHEDULED)]
    chan = await _run(cog, now=MONDAY_6PM, history=history)
    assert chan.send.await_count == 0


async def test_daily_confirm_targets_in_game_day_at_evening_reset():
    """Regression for the 'train a day behind' bug (#318): a 10pm-local reminder
    fires at the in-game server reset (~2h before local midnight), which is
    already the next in-game day. The confirmation must announce that day's
    conductor — tomorrow's calendar row — not the local-today row that just
    ended."""
    import train_rotation_ui as ui

    cog = _make_cog()
    # 10pm ET Tue 2026-06-02 == 00:00 server time (UTC-2) Wed 2026-06-03.
    tue_10pm = datetime(2026, 6, 2, 22, 0, tzinfo=ET)
    history = [
        tr.HistoryRow("2026-06-02", "Alice", "auto", tr.STATUS_SCHEDULED),  # local today
        tr.HistoryRow("2026-06-03", "Bob", "auto", tr.STATUS_SCHEDULED),  # in-game today
    ]
    captured = {}

    def _capture_embed(dd):
        captured["dd"] = dd
        return MagicMock()

    with patch.object(ui, "build_daily_confirm_embed", side_effect=_capture_embed):
        chan = await _run(cog, now=tue_10pm, tcfg=_tcfg(reminder_time="22:00"), history=history)

    assert chan.send.await_count >= 1
    assert captured["dd"].date == "2026-06-03"
    assert captured["dd"].member == "Bob"
