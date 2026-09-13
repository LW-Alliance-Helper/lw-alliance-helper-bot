"""
`db_timings`: the per-helper timing of config database calls behind the
step 10 decision on #589, and the `/admin db_timings` reading of it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

import db_timings  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_counters():
    db_timings.reset()
    yield
    db_timings.reset()


# ── the recorder ─────────────────────────────────────────────────────────────


def test_records_per_helper_with_loop_and_off_loop_counts():
    db_timings.record("get_config", 0.4, True)
    db_timings.record("get_config", 0.6, False)
    db_timings.record("get_buddy_config", 3.0, True)

    snap = db_timings.snapshot()
    by = {h["helper"]: h for h in snap["helpers"]}
    assert by["get_config"]["calls"] == 2
    assert by["get_config"]["on_loop"] == 1
    assert by["get_config"]["avg_ms"] == pytest.approx(0.5)
    assert by["get_config"]["max_ms"] == 0.6
    assert by["get_config"]["loop_ms"] == pytest.approx(0.4)


def test_helpers_come_back_slowest_on_the_loop_first():
    db_timings.record("cheap_but_busy", 0.2, True)
    db_timings.record("cheap_but_busy", 0.2, True)
    db_timings.record("one_slow_call", 40.0, True)
    db_timings.record("slow_but_off_loop", 90.0, False)

    order = [h["helper"] for h in db_timings.snapshot()["helpers"]]
    assert order == ["one_slow_call", "cheap_but_busy", "slow_but_off_loop"]


def test_buckets_split_at_one_five_twenty_and_a_hundred_ms():
    for ms in (0.5, 4.9, 5.0, 19.9, 20.0, 99.9, 100.0, 250.0):
        db_timings.record("h", ms, True)
    (h,) = db_timings.snapshot()["helpers"]
    assert h["buckets"] == [1, 1, 2, 2, 2]


def test_a_slow_call_is_kept_and_warned_once_per_window(caplog, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(db_timings.time, "time", lambda: clock[0])
    with caplog.at_level("WARNING", logger="db_timings"):
        db_timings.record("get_config", 80.0, True)
        db_timings.record("get_config", 90.0, True)  # inside the window: no second warning
        clock[0] += db_timings.WARN_EVERY_S
        db_timings.record("get_config", 70.0, False)  # window elapsed: warned again
    warnings = [r for r in caplog.records if "[DB TIMINGS]" in r.getMessage()]
    assert len(warnings) == 2
    assert "on the event loop" in warnings[0].getMessage()
    assert "off the loop" in warnings[1].getMessage()
    slow = db_timings.snapshot()["recent_slow"]
    assert [ms for _, _, ms, _ in slow] == [80.0, 90.0, 70.0]


def test_a_fast_call_is_neither_kept_nor_warned(caplog):
    with caplog.at_level("WARNING", logger="db_timings"):
        db_timings.record("get_config", 2.0, True)
    assert not [r for r in caplog.records if "[DB TIMINGS]" in r.getMessage()]
    assert db_timings.snapshot()["recent_slow"] == []


def test_reset_clears_everything_and_restarts_the_clock():
    db_timings.record("get_config", 80.0, True)
    before = db_timings.snapshot()["since"]
    time.sleep(0.01)
    db_timings.reset()
    snap = db_timings.snapshot()
    assert snap["helpers"] == [] and snap["recent_slow"] == []
    assert snap["since"] > before


@pytest.mark.asyncio
async def test_on_event_loop_tells_the_loop_thread_from_a_worker():
    assert db_timings.on_event_loop() is True
    assert await asyncio.to_thread(db_timings.on_event_loop) is False


# ── the hook in config._get_conn ─────────────────────────────────────────────


def _config_on(tmp_path, monkeypatch):
    import config

    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "timed.db"))
    config.init_db()
    db_timings.reset()
    return config


def test_a_config_read_is_recorded_under_its_helper(tmp_path, monkeypatch):
    config = _config_on(tmp_path, monkeypatch)

    config.get_config(4242)
    config.get_config(4242)

    by = {h["helper"]: h for h in db_timings.snapshot()["helpers"]}
    assert by["get_config"]["calls"] == 2
    assert by["get_config"]["on_loop"] == 0, "no loop is running in a plain test"
    assert 0 < by["get_config"]["max_ms"] < 5_000


@pytest.mark.asyncio
async def test_a_read_on_the_loop_counts_as_on_the_loop(tmp_path, monkeypatch):
    config = _config_on(tmp_path, monkeypatch)

    config.get_config(4242)  # straight from the coroutine: blocks the loop
    await asyncio.to_thread(config.get_config, 4242)  # handed off: does not

    (h,) = [h for h in db_timings.snapshot()["helpers"] if h["helper"] == "get_config"]
    assert (h["calls"], h["on_loop"]) == (2, 1)


def test_a_connection_records_once_even_when_closed_after_the_with(tmp_path, monkeypatch):
    config = _config_on(tmp_path, monkeypatch)

    with config._get_conn() as conn:
        conn.execute("SELECT 1").fetchone()
    conn.close()

    by = {h["helper"]: h for h in db_timings.snapshot()["helpers"]}
    assert by["test_a_connection_records_once_even_when_closed_after_the_with"]["calls"] == 1


# ── /admin db_timings ────────────────────────────────────────────────────────


@pytest.fixture
def admin_module():
    import bot  # noqa: F401  (binds bot_state.bot, which bot_admin reads at import)
    import bot_admin

    return bot_admin


def _owner_interaction():
    inter = MagicMock()
    inter.user.id = 1
    inter.response.send_message = AsyncMock()
    return inter


def test_render_names_the_slowest_on_the_loop_first_and_lists_recent_slow(admin_module):
    db_timings.record("get_config", 0.4, True)
    db_timings.record("get_config", 61.0, True)
    db_timings.record("get_buddy_config", 2.0, False)

    text = admin_module.render_db_timings(db_timings.snapshot(), now=time.time())

    assert text.startswith("📊 Config database timings since ")
    assert "3 calls, 2 on the event loop" in text
    lines = text.splitlines()
    first_row = next(l for l in lines if l.startswith("get_"))
    assert first_row.startswith("get_config")
    assert "Recent calls over 50 ms" in text
    assert "get_config" in text and "61 ms  on the loop" in text


def test_render_with_nothing_recorded_says_so(admin_module):
    text = admin_module.render_db_timings(db_timings.snapshot(), now=time.time())
    assert text.endswith("Nothing recorded yet.")


@pytest.mark.asyncio
async def test_command_is_owner_only(admin_module, monkeypatch):
    monkeypatch.setattr(admin_module, "_require_bot_owner", AsyncMock(return_value=False))
    inter = _owner_interaction()
    await admin_module.admin_db_timings_slash.callback(inter)
    inter.response.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_command_shows_the_table_ephemerally_and_can_reset(admin_module, monkeypatch):
    monkeypatch.setattr(admin_module, "_require_bot_owner", AsyncMock(return_value=True))
    db_timings.record("get_config", 0.4, True)
    inter = _owner_interaction()

    await admin_module.admin_db_timings_slash.callback(inter, reset=True)

    inter.response.send_message.assert_awaited_once()
    args, kwargs = inter.response.send_message.await_args
    assert kwargs["ephemeral"] is True
    assert "get_config" in args[0] and args[0].endswith("Counters reset.")
    assert db_timings.snapshot()["helpers"] == []
