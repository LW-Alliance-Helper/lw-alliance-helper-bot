"""
Tests for `bot.growth_task` — the @tasks.loop(hours=1) coroutine that
walks every configured guild every hour and runs the growth snapshot if
the schedule says it's due.

The unit tests for `compute_next_snapshot` (test_growth.py) cover the
date math in isolation. These tests cover the **fire decision** as it
actually runs in production: enable check, monthly day-of-month + hour
match, interval epoch math + hour match, and that disabled guilds are
skipped without crashing the loop.

The audit flagged this loop as untested. A regression here means
scheduled snapshots silently never fire — a guild's growth-tracking
data quietly stops accumulating.
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date as date_cls, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

from tests.constants import TEST_GUILD_ID

ET = ZoneInfo("America/New_York")


def _save_growth(
    guild_id: int,
    *,
    enabled: int = 1,
    frequency: str = "monthly",
    snapshot_day: int = 1,
    snapshot_interval: int = 30,
):
    """Insert a guild_growth_config row with the minimum fields that
    growth_task reads."""
    from config import save_growth_config

    save_growth_config(
        guild_id,
        enabled=enabled,
        tab_source="Squad Powers",
        name_col="A",
        metrics=[{"col": "B", "label": "Power"}],
        tab_growth="Growth Tracking",
        snapshot_frequency=frequency,
        snapshot_day=snapshot_day,
        snapshot_interval=snapshot_interval,
        data_start_row=2,
    )


def _mark_setup_complete(guild_id: int):
    """growth_task reads only guilds where setup_complete = 1."""
    import config

    cfg = config.get_or_create_config(guild_id)
    cfg.setup_complete = 1
    config.save_config(cfg)


async def _run_task_with_now(now_dt: datetime):
    """Invoke the underlying growth_task coroutine with a patched
    `datetime.now` so we control which hour/day the loop sees."""
    import bot

    fake_dt_module = MagicMock()
    fake_dt_module.now = MagicMock(return_value=now_dt)

    spy = MagicMock()  # spies on `_run_growth_snapshot_inner` calls
    with patch("bot.datetime", fake_dt_module), patch("growth._run_growth_snapshot_inner", spy):
        await bot.growth_task.coro()

    return spy


# ── Monthly ──────────────────────────────────────────────────────────────────


class TestGrowthTaskMonthly:
    @pytest.mark.asyncio
    async def test_fires_when_day_and_hour_match(self, seeded_db):
        _save_growth(TEST_GUILD_ID, frequency="monthly", snapshot_day=15)
        _mark_setup_complete(TEST_GUILD_ID)

        spy = await _run_task_with_now(datetime(2026, 5, 15, 22, 0, tzinfo=ET))
        spy.assert_called_once_with(TEST_GUILD_ID)

    @pytest.mark.asyncio
    async def test_skips_when_hour_is_wrong(self, seeded_db):
        """Day matches but hour=21 → no fire. The loop runs hourly so
        this case happens 23 times per snapshot day."""
        _save_growth(TEST_GUILD_ID, frequency="monthly", snapshot_day=15)
        _mark_setup_complete(TEST_GUILD_ID)

        spy = await _run_task_with_now(datetime(2026, 5, 15, 21, 0, tzinfo=ET))
        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_day_is_wrong(self, seeded_db):
        _save_growth(TEST_GUILD_ID, frequency="monthly", snapshot_day=15)
        _mark_setup_complete(TEST_GUILD_ID)

        spy = await _run_task_with_now(datetime(2026, 5, 14, 22, 0, tzinfo=ET))
        spy.assert_not_called()


# ── Interval (anchor 2026-01-01) ─────────────────────────────────────────────


class TestGrowthTaskInterval:
    @pytest.mark.asyncio
    async def test_fires_on_epoch_at_22(self, seeded_db):
        """Jan 1 2026 is the epoch — every interval starts firing here."""
        _save_growth(TEST_GUILD_ID, frequency="interval", snapshot_interval=14)
        _mark_setup_complete(TEST_GUILD_ID)

        # bot.growth_task uses _date.today() rather than now.date() for
        # the day comparison. Patch that to land on the epoch.
        with (
            patch("bot.datetime", _frozen_datetime(datetime(2026, 1, 1, 22, 0, tzinfo=ET))),
            patch("growth._run_growth_snapshot_inner") as spy,
            _frozen_today(date_cls(2026, 1, 1)),
        ):
            from bot import growth_task

            await growth_task.coro()

        spy.assert_called_once_with(TEST_GUILD_ID)

    @pytest.mark.asyncio
    async def test_fires_at_next_multiple(self, seeded_db):
        """Interval=14 → next fire after 2026-01-01 is 2026-01-15."""
        _save_growth(TEST_GUILD_ID, frequency="interval", snapshot_interval=14)
        _mark_setup_complete(TEST_GUILD_ID)

        with (
            patch("bot.datetime", _frozen_datetime(datetime(2026, 1, 15, 22, 0, tzinfo=ET))),
            patch("growth._run_growth_snapshot_inner") as spy,
            _frozen_today(date_cls(2026, 1, 15)),
        ):
            from bot import growth_task

            await growth_task.coro()
        spy.assert_called_once_with(TEST_GUILD_ID)

    @pytest.mark.asyncio
    async def test_skips_on_non_multiple_day(self, seeded_db):
        _save_growth(TEST_GUILD_ID, frequency="interval", snapshot_interval=14)
        _mark_setup_complete(TEST_GUILD_ID)

        # Jan 5 → 4 days into a 14-day cycle, no fire.
        with (
            patch("bot.datetime", _frozen_datetime(datetime(2026, 1, 5, 22, 0, tzinfo=ET))),
            patch("growth._run_growth_snapshot_inner") as spy,
            _frozen_today(date_cls(2026, 1, 5)),
        ):
            from bot import growth_task

            await growth_task.coro()
        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_hour_is_wrong_even_on_multiple_day(self, seeded_db):
        _save_growth(TEST_GUILD_ID, frequency="interval", snapshot_interval=14)
        _mark_setup_complete(TEST_GUILD_ID)

        with (
            patch("bot.datetime", _frozen_datetime(datetime(2026, 1, 15, 21, 0, tzinfo=ET))),
            patch("growth._run_growth_snapshot_inner") as spy,
            _frozen_today(date_cls(2026, 1, 15)),
        ):
            from bot import growth_task

            await growth_task.coro()
        spy.assert_not_called()


# ── Per-guild gating ─────────────────────────────────────────────────────────


class TestGrowthTaskGating:
    @pytest.mark.asyncio
    async def test_disabled_guild_does_not_fire(self, seeded_db):
        _save_growth(TEST_GUILD_ID, enabled=0, frequency="monthly", snapshot_day=15)
        _mark_setup_complete(TEST_GUILD_ID)

        spy = await _run_task_with_now(datetime(2026, 5, 15, 22, 0, tzinfo=ET))
        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_setup_incomplete_guild_skipped(self, seeded_db):
        """The SQL `WHERE setup_complete = 1` filter excludes pre-launch
        guilds — no snapshot until the admin finishes /setup."""
        import config

        cfg = config.get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = 0
        config.save_config(cfg)
        _save_growth(TEST_GUILD_ID, frequency="monthly", snapshot_day=15)

        spy = await _run_task_with_now(datetime(2026, 5, 15, 22, 0, tzinfo=ET))
        spy.assert_not_called()

    @pytest.mark.asyncio
    async def test_one_guild_fire_does_not_block_others(self, seeded_db):
        """Two configured guilds; A's snapshot raises, B's still runs.
        Crash isolation is critical: a single broken guild can't shut
        down the daily growth task for everyone else."""
        import config

        # Guild A — will raise inside the snapshot
        cfg_a = config.get_or_create_config(TEST_GUILD_ID)
        cfg_a.setup_complete = 1
        config.save_config(cfg_a)
        _save_growth(TEST_GUILD_ID, frequency="monthly", snapshot_day=15)

        # Guild B — should still get its snapshot
        gid_b = TEST_GUILD_ID + 1
        cfg_b = config.get_or_create_config(gid_b)
        cfg_b.setup_complete = 1
        config.save_config(cfg_b)
        _save_growth(gid_b, frequency="monthly", snapshot_day=15)

        seen = []

        def fake_run(gid):
            if gid == TEST_GUILD_ID:
                raise RuntimeError("boom")
            seen.append(gid)

        import bot as bot_mod

        fake_dt = _frozen_datetime(datetime(2026, 5, 15, 22, 0, tzinfo=ET))
        with (
            patch("bot.datetime", fake_dt),
            patch("growth._run_growth_snapshot_inner", side_effect=fake_run),
        ):
            await bot_mod.growth_task.coro()

        assert gid_b in seen, (
            f"Guild B's snapshot must still fire after Guild A raised. seen={seen}"
        )


# ── DB read error path ───────────────────────────────────────────────────────


class TestGrowthTaskDbErrorHandling:
    @pytest.mark.asyncio
    async def test_returns_quietly_when_db_read_fails(self, seeded_db):
        """If the SQLite read raises, the loop should swallow + log
        (and capture to Sentry) rather than crash the whole tasks loop."""
        import bot

        with (
            patch("sqlite3.connect", side_effect=RuntimeError("disk gone")),
            patch("growth._run_growth_snapshot_inner") as spy,
        ):
            # Should not raise.
            await bot.growth_task.coro()

        spy.assert_not_called()


# ── Sentry-noise handling for sheet-config errors (#285 / #286) ───────────────


class TestGrowthTaskSheetErrorSentryNoise:
    """A guild that deleted its sheet or revoked the service account's access
    is an operational condition the alliance owns — the loop must log + skip
    it, not page Sentry (regressions of #285 / #286). A genuinely unexpected
    error still captures."""

    @pytest.mark.asyncio
    async def test_deleted_sheet_does_not_capture_to_sentry(self, seeded_db):
        import gspread
        import bot as bot_mod

        cfg = None
        import config

        cfg = config.get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = 1
        config.save_config(cfg)
        _save_growth(TEST_GUILD_ID, frequency="monthly", snapshot_day=15)

        def boom(gid):
            raise gspread.exceptions.SpreadsheetNotFound()

        fake_dt = _frozen_datetime(datetime(2026, 5, 15, 22, 0, tzinfo=ET))
        with (
            patch("bot.datetime", fake_dt),
            patch("growth._run_growth_snapshot_inner", side_effect=boom),
            patch("bot.sentry_sdk") as sentry,
        ):
            await bot_mod.growth_task.coro()

        sentry.capture_exception.assert_not_called()
        sentry.add_breadcrumb.assert_called_once()

    @pytest.mark.asyncio
    async def test_unexpected_error_still_captures(self, seeded_db):
        import bot as bot_mod
        import config

        cfg = config.get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = 1
        config.save_config(cfg)
        _save_growth(TEST_GUILD_ID, frequency="monthly", snapshot_day=15)

        def boom(gid):
            raise RuntimeError("genuinely unexpected")

        fake_dt = _frozen_datetime(datetime(2026, 5, 15, 22, 0, tzinfo=ET))
        with (
            patch("bot.datetime", fake_dt),
            patch("growth._run_growth_snapshot_inner", side_effect=boom),
            patch("bot.sentry_sdk") as sentry,
        ):
            await bot_mod.growth_task.coro()

        sentry.capture_exception.assert_called_once()


# ── Test helpers ─────────────────────────────────────────────────────────────


def _frozen_datetime(fixed: datetime):
    """A drop-in replacement for `datetime` whose `.now(...)` always
    returns the fixed instant. Other classmethods still delegate."""
    fake = MagicMock(wraps=datetime)
    fake.now = MagicMock(return_value=fixed)
    return fake


from contextlib import contextmanager


@contextmanager
def _frozen_today(fixed: date_cls):
    """The interval branch reads `_date.today()` from a `from datetime
    import date as _date` inside the function. Patching `datetime.date`
    on the module wouldn't catch the local alias, so we patch the
    `date` symbol on the `datetime` module instead."""
    import datetime as dt_mod

    fake_date_cls = MagicMock(wraps=dt_mod.date)
    fake_date_cls.today = MagicMock(return_value=fixed)
    with patch("datetime.date", fake_date_cls):
        yield
