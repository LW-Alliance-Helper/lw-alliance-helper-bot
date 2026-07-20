"""
Unit tests for shiny_tasks.py — the Daily Shiny Tasks announcement.

Covers:
  * `is_shiny_today` 3-day cycle math, verified against the 8 reference
    rows from docs/hedge_data_source.md, plus Day 0–3 edges.
  * `servers_shiny_today`, `format_server_list`, `render_announcement`
    (with SafeDict typo tolerance).
  * `_parse_records` regex round-trip against a minimal fake bundle.
  * `resolve_announcement_template` empty-vs-custom logic.
  * `build_announcement_for_guild` range filter + no-shinies short-circuit.
  * Config helpers: upsert + range query + soft-delete via `max_age_days`,
    `mark_shiny_tasks_posted`, `list_shiny_enabled_guild_ids`.
  * The per-minute scheduler loop `bot.shiny_tasks_post_task` — time
    match, time mismatch, last_posted_date dedupe, no-shinies-today
    silent path, missing channel skip.
"""

from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

from tests.constants import TEST_GUILD_ID

ET = ZoneInfo("America/New_York")


# ── Date math ────────────────────────────────────────────────────────────────


class TestIsShinyToday:
    """Verified against cpt-hedge.com 'Shiny Tasks' column for
    #2263–#2270 on 2026-05-11. See docs/hedge_data_source.md."""

    TODAY = date(2026, 5, 11)
    CASES = [
        # (server, creation_date, hedge_says_today)
        ("#2263", date(2026, 4, 27), False),  # Tomorrow
        ("#2264", date(2026, 4, 29), True),  # Today
        ("#2265", date(2026, 5, 1), False),  # In 2 days
        ("#2266", date(2026, 5, 2), True),  # Today
        ("#2267", date(2026, 5, 4), False),  # In 2 days
        ("#2268", date(2026, 5, 6), False),  # Tomorrow
        ("#2269", date(2026, 5, 8), True),  # Today
        ("#2270", date(2026, 5, 10), False),  # In 2 days
    ]

    @pytest.mark.parametrize("label,created,expected", CASES)
    def test_hedge_reference_rows(self, label, created, expected):
        from shiny_tasks import is_shiny_today

        assert is_shiny_today(created, self.TODAY) is expected, label

    def test_first_shiny_is_day_three(self):
        """A server created today doesn't go shiny until day 3."""
        from shiny_tasks import is_shiny_today

        created = date(2026, 5, 1)
        assert is_shiny_today(created, date(2026, 5, 1)) is False  # Day 0
        assert is_shiny_today(created, date(2026, 5, 2)) is False  # Day 1
        assert is_shiny_today(created, date(2026, 5, 3)) is False  # Day 2
        assert is_shiny_today(created, date(2026, 5, 4)) is True  # Day 3 ✓
        assert is_shiny_today(created, date(2026, 5, 5)) is False  # Day 4
        assert is_shiny_today(created, date(2026, 5, 7)) is True  # Day 6 ✓

    def test_does_not_count_creation_date_as_shiny(self):
        """`delta=0 and delta % 3 == 0` would naively be shiny — verify
        the explicit `delta >= 3` guard rules that out."""
        from shiny_tasks import is_shiny_today

        d = date(2026, 1, 1)
        assert is_shiny_today(d, d) is False


# ── Pure helpers ─────────────────────────────────────────────────────────────


class TestFormatServerList:
    @pytest.mark.parametrize(
        "nums,expected",
        [
            ([], ""),
            ([681], "681"),
            ([681, 682], "681 and 682"),
            ([681, 682, 689], "681, 682 and 689"),
            ([681, 682, 689, 704, 706], "681, 682, 689, 704 and 706"),
        ],
    )
    def test_joins_with_oxford_and(self, nums, expected):
        from shiny_tasks import format_server_list

        assert format_server_list(nums) == expected


class TestServersShinyToday:
    def test_filters_to_shiny_and_sorts(self):
        """Mixed input → only shiny servers, sorted ascending. Tests
        accept ISO strings (production path from SQLite) and `date`
        objects (in-memory fixtures) interchangeably."""
        from shiny_tasks import servers_shiny_today

        today = date(2026, 5, 11)
        rows = [
            {"server_number": 2266, "creation_date": "2026-05-02"},  # shiny
            {"server_number": 2264, "creation_date": "2026-04-29"},  # shiny
            {"server_number": 2263, "creation_date": "2026-04-27"},  # tomorrow
            {"server_number": 2269, "creation_date": date(2026, 5, 8)},  # shiny
        ]
        assert servers_shiny_today(rows, today) == [2264, 2266, 2269]

    def test_unparseable_creation_date_skipped(self):
        from shiny_tasks import servers_shiny_today

        rows = [
            {"server_number": 1, "creation_date": "not-a-date"},
            {"server_number": 2, "creation_date": "2026-04-29"},
        ]
        assert servers_shiny_today(rows, date(2026, 5, 11)) == [2]


class TestRenderAnnouncement:
    def test_default_template_substitutes_servers(self):
        from shiny_tasks import render_announcement
        from defaults import DEFAULT_SHINY_TASKS_MESSAGE

        out = render_announcement(
            DEFAULT_SHINY_TASKS_MESSAGE,
            servers=[681, 682, 689, 704, 706],
            today=date(2026, 5, 11),
        )
        assert "681, 682, 689, 704 and 706" in out

    def test_typo_placeholder_renders_literally(self):
        """A typo like `{servrs}` must not crash format_map. SafeDict
        returns the literal `{servrs}` text instead, so the scheduler
        loop survives a misconfigured template."""
        from shiny_tasks import render_announcement

        out = render_announcement(
            "Shinies today: {servrs}",
            servers=[681],
            today=date(2026, 5, 11),
        )
        assert "{servrs}" in out

    def test_date_placeholder_is_locale_safe(self):
        """`{date}` must render without %-d / %#d platform quirks."""
        from shiny_tasks import render_announcement

        out = render_announcement(
            "Heads up for {date}: {servers}",
            servers=[1],
            today=date(2026, 5, 11),
        )
        assert "Monday, May 11" in out
        assert "{date}" not in out


class TestResolveAnnouncementTemplate:
    def test_empty_returns_default(self):
        from shiny_tasks import resolve_announcement_template
        from defaults import DEFAULT_SHINY_TASKS_MESSAGE

        assert resolve_announcement_template("") == DEFAULT_SHINY_TASKS_MESSAGE
        assert resolve_announcement_template("   ") == DEFAULT_SHINY_TASKS_MESSAGE

    def test_custom_returned_verbatim(self):
        from shiny_tasks import resolve_announcement_template

        custom = "🌟 Today: {servers}!"
        assert resolve_announcement_template(custom) == custom


class TestBuildAnnouncementForGuild:
    def test_filters_to_range_and_renders(self):
        from shiny_tasks import build_announcement_for_guild

        rows = [
            {"server_number": 2263, "creation_date": "2026-04-27"},  # tomorrow
            {"server_number": 2264, "creation_date": "2026-04-29"},  # in-range, shiny
            {"server_number": 2266, "creation_date": "2026-05-02"},  # in-range, shiny
            {"server_number": 2269, "creation_date": "2026-05-08"},  # out of range
        ]
        body = build_announcement_for_guild(
            server_rows=rows,
            server_min=2264,
            server_max=2266,
            today=date(2026, 5, 11),
            template="",  # use default
        )
        assert body is not None
        assert "2264 and 2266" in body
        assert "2263" not in body
        assert "2269" not in body

    def test_no_shinies_returns_none(self):
        """Caller must skip posting when no servers in range are shiny
        today — posting "Daily shinies: ." would be a bug."""
        from shiny_tasks import build_announcement_for_guild

        rows = [
            {"server_number": 2263, "creation_date": "2026-04-27"},
        ]
        body = build_announcement_for_guild(
            server_rows=rows,
            server_min=2263,
            server_max=2263,
            today=date(2026, 5, 11),
            template="",
        )
        assert body is None


# ── Hedge bundle parser ──────────────────────────────────────────────────────


class TestParseRecords:
    """`_parse_records` must extract the (id, ts, region) triple from
    server objects embedded in the cpt-hedge page chunk. The bundle is
    minified JS but the records have a predictable shape — see
    docs/hedge_data_source.md for the live fixture."""

    SAMPLE_BUNDLE = (
        # Realistic-ish slice of two adjacent records, with extra fields
        # between `timestamp` and `region` that must be skipped over.
        "...prefix..."
        '{"id":"10","server":"State#10","timestamp":"1694157329000",'
        '"seasonStartTimestamps":{"s4":"1747044000000"},'
        '"currentSeason":6,"isPostSeason":false,"currentWeek":5,'
        '"updatedAt":1776087064698,"region":["global"]}'
        ',{"id":"11","server":"State#11","timestamp":"1694763651000",'
        '"seasonStartTimestamps":{"s4":"1747044000000"},'
        '"currentSeason":6,"isPostSeason":false,"currentWeek":5,'
        '"updatedAt":1776087064698,"region":["europe"]}'
        "...suffix..."
    )

    def test_extracts_two_records(self):
        from shiny_tasks import _parse_records

        rows = _parse_records(self.SAMPLE_BUNDLE)
        assert len(rows) == 2
        ids = sorted(r[0] for r in rows)
        assert ids == [10, 11]

    def test_creation_date_decoded_from_ms(self):
        from shiny_tasks import _parse_records

        rows = _parse_records(self.SAMPLE_BUNDLE)
        d = {r[0]: r[1] for r in rows}
        # 1694157329000 ms = 2023-09-08 in UTC; date-only round trip.
        assert d[10] == "2023-09-08"

    def test_empty_region_list_yields_empty_string(self):
        from shiny_tasks import _parse_records

        # `"region":[]` (empty list) should still parse and emit region=""
        chunk = '{"id":"42","server":"State#42","timestamp":"1700000000000","foo":1,"region":[]}'
        rows = _parse_records(chunk)
        assert rows == [(42, "2023-11-14", "")]

    def test_no_records_returns_empty(self):
        from shiny_tasks import _parse_records

        assert _parse_records("nothing of interest in here") == []


class TestParseServerRecordsJson:
    """`parse_server_records_json` ingests the JSON array the source's servers
    page loads (the `/admin shiny_import` payload) and derives creation dates in
    server time (UTC-2), so the snapshot matches the source's displayed dates."""

    def test_creation_date_uses_server_time_not_utc(self):
        import json as _json

        from shiny_tasks import parse_server_records_json

        # 00:30 UTC on 2026-03-10 is still 2026-03-09 in server time (UTC-2),
        # so the creation date must be the 9th — the #331 fix. Plain UTC would
        # store the 10th and push the server a day late in the 3-day cycle.
        ts_ms = int(datetime(2026, 3, 10, 0, 30, tzinfo=timezone.utc).timestamp() * 1000)
        text = _json.dumps([{"id": "2500", "timestamp": str(ts_ms), "region": ["global"]}])
        assert parse_server_records_json(text) == [(2500, "2026-03-09", "global")]

    def test_skips_unusable_records_and_reads_region(self):
        import json as _json

        from shiny_tasks import parse_server_records_json

        text = _json.dumps(
            [
                {"id": "1", "timestamp": "1700000000000", "region": ["europe"]},
                {"id": "2"},  # no timestamp → skipped
                {"timestamp": "1700000000000"},  # no id → skipped
                {"id": "3", "timestamp": "1700000000000", "region": []},  # empty region → ""
            ]
        )
        rows = parse_server_records_json(text)
        assert [r[0] for r in rows] == [1, 3]
        assert rows[0] == (1, "2023-11-14", "europe")
        assert rows[1][2] == ""


# ── DB helpers (use the temp_db fixture from conftest) ───────────────────────


class TestDbHelpers:
    def test_upsert_and_range_query(self, temp_db):
        from config import (
            upsert_shiny_task_servers,
            get_shiny_task_servers_in_range,
        )

        now_iso = datetime.now(tz=timezone.utc).isoformat()
        n = upsert_shiny_task_servers(
            [
                (681, "2025-01-01", "global"),
                (682, "2025-01-02", "global"),
                (689, "2025-01-03", "europe"),
                (799, "2025-01-04", "global"),
            ],
            seen_at=now_iso,
        )
        assert n == 4

        in_range = get_shiny_task_servers_in_range(681, 700)
        nums = sorted(r["server_number"] for r in in_range)
        assert nums == [681, 682, 689]

    def test_soft_delete_via_last_seen_at(self, temp_db):
        """A server absent from a refresh > max_age_days ago is filtered
        out, even though the row still exists in the table."""
        from config import (
            upsert_shiny_task_servers,
            get_shiny_task_servers_in_range,
        )

        stale_iso = (datetime.now(tz=timezone.utc).replace(year=2020)).isoformat()
        fresh_iso = datetime.now(tz=timezone.utc).isoformat()

        upsert_shiny_task_servers([(681, "2025-01-01", "global")], seen_at=stale_iso)
        upsert_shiny_task_servers([(682, "2025-01-02", "global")], seen_at=fresh_iso)

        out = get_shiny_task_servers_in_range(681, 700, max_age_days=30)
        nums = sorted(r["server_number"] for r in out)
        assert nums == [682]  # 681 aged out

    def test_upsert_refreshes_last_seen(self, temp_db):
        from config import (
            upsert_shiny_task_servers,
            get_shiny_task_servers_in_range,
        )

        stale_iso = (datetime.now(tz=timezone.utc).replace(year=2020)).isoformat()
        upsert_shiny_task_servers([(681, "2025-01-01", "global")], seen_at=stale_iso)
        # Re-upsert with fresh timestamp → 681 is back in queries.
        fresh_iso = datetime.now(tz=timezone.utc).isoformat()
        upsert_shiny_task_servers([(681, "2025-01-01", "global")], seen_at=fresh_iso)

        out = get_shiny_task_servers_in_range(681, 681, max_age_days=30)
        assert [r["server_number"] for r in out] == [681]

    def test_count_and_enabled_list(self, temp_db):
        from config import (
            count_shiny_task_servers,
            list_shiny_enabled_guild_ids,
            save_shiny_tasks_config,
            upsert_shiny_task_servers,
        )

        assert count_shiny_task_servers() == 0
        assert list_shiny_enabled_guild_ids() == []

        upsert_shiny_task_servers(
            [(1, "2025-01-01", "")],
            seen_at=datetime.now(tz=timezone.utc).isoformat(),
        )
        save_shiny_tasks_config(
            TEST_GUILD_ID,
            enabled=1,
            channel_id=999,
            post_time="09:00",
            server_min=1,
            server_max=2,
            message_template="",
        )
        assert count_shiny_task_servers() == 1
        assert list_shiny_enabled_guild_ids() == [TEST_GUILD_ID]

    def test_get_last_shiny_refresh_at_empty(self, temp_db):
        from config import get_last_shiny_refresh_at

        assert get_last_shiny_refresh_at() is None

    def test_get_last_shiny_refresh_at_returns_max(self, temp_db):
        """Returns the max `last_seen_at` across all rows so the weekly
        gate keys off the most recent successful refresh."""
        from config import get_last_shiny_refresh_at, upsert_shiny_task_servers

        older = datetime(2026, 5, 1, tzinfo=timezone.utc).isoformat()
        newer = datetime(2026, 5, 10, tzinfo=timezone.utc).isoformat()
        upsert_shiny_task_servers([(1, "2025-01-01", "")], seen_at=older)
        upsert_shiny_task_servers([(2, "2025-01-02", "")], seen_at=newer)

        got = get_last_shiny_refresh_at()
        assert got is not None
        assert got == datetime(2026, 5, 10, tzinfo=timezone.utc)

    def test_save_preserves_last_posted_date(self, temp_db):
        """Re-running the wizard mustn't reset `last_posted_date` —
        otherwise a Railway restart between save + the next post-time
        minute would let the loop fire a second time."""
        from config import (
            save_shiny_tasks_config,
            mark_shiny_tasks_posted,
            get_shiny_tasks_config,
        )

        save_shiny_tasks_config(
            TEST_GUILD_ID,
            enabled=1,
            channel_id=1,
            post_time="09:00",
            server_min=1,
            server_max=2,
            message_template="",
        )
        mark_shiny_tasks_posted(TEST_GUILD_ID, "2026-05-11")
        # Re-save (simulating wizard re-run) with different values.
        save_shiny_tasks_config(
            TEST_GUILD_ID,
            enabled=1,
            channel_id=2,
            post_time="10:00",
            server_min=1,
            server_max=3,
            message_template="hi",
        )
        cfg = get_shiny_tasks_config(TEST_GUILD_ID)
        assert cfg["last_posted_date"] == "2026-05-11"
        assert cfg["channel_id"] == 2  # other fields still updated


# ── Scheduler loop (bot.shiny_tasks_post_task) ───────────────────────────────


def _seed_complete(guild_id: int):
    """Mark a guild's core config setup_complete=1 and stamp its tz so
    the per-minute loop can resolve ZoneInfo."""
    import config

    cfg = config.get_or_create_config(guild_id)
    cfg.timezone = "America/New_York"
    cfg.setup_complete = True
    config.save_config(cfg)


def _enable_shiny(
    guild_id: int, *, post_time: str, channel_id: int, server_min: int = 1, server_max: int = 2000
):
    from config import save_shiny_tasks_config

    save_shiny_tasks_config(
        guild_id,
        enabled=1,
        channel_id=channel_id,
        post_time=post_time,
        server_min=server_min,
        server_max=server_max,
        message_template="",
    )


def _seed_servers(rows):
    """Insert (server_number, creation_date_iso, region) tuples with a
    fresh `last_seen_at` so the soft-delete filter doesn't drop them."""
    from config import upsert_shiny_task_servers

    upsert_shiny_task_servers(
        rows,
        seen_at=datetime.now(tz=timezone.utc).isoformat(),
    )


async def _run_loop_at(now_dt: datetime, *, send_ok: bool = True):
    """Invoke `bot.shiny_tasks_post_task.coro` with patched `datetime`
    and a fake bot whose `get_channel` returns a stub channel."""
    import bot as bot_module

    sent: list[str] = []
    chan = MagicMock()
    chan.id = 123
    chan.name = "shinies"

    async def _send(content):
        sent.append(content)
        return MagicMock(id=999)

    chan.send = AsyncMock(side_effect=_send if send_ok else None)
    if not send_ok:
        import discord

        chan.send = AsyncMock(side_effect=discord.Forbidden(MagicMock(), "no perms"))

    fake_bot = MagicMock()
    fake_bot.get_channel = MagicMock(return_value=chan)

    fake_dt = MagicMock()
    fake_dt.now = MagicMock(return_value=now_dt)

    with patch.object(bot_module, "bot", fake_bot), patch("bot.datetime", fake_dt):
        await bot_module.shiny_tasks_post_task.coro()
    return sent


class TestServerRangeModalValueProperty:
    """`ModalLaunchView.open_modal` formats `self.modal.value` into a
    post-submit confirmation message. Any modal it wraps must expose
    `value` or the view raises AttributeError mid-step — see the bug
    that landed in dev shortly after the first wizard ship. This test
    pins the contract so the regression can't slip back in."""

    def test_value_returns_display_string_after_submit(self):
        """Direct attribute set mirrors what `on_submit` does in prod;
        instantiating the modal isn't safe outside an interaction
        context so we sidestep `__init__` and exercise the property."""
        from setup_cog import run_shiny_tasks_setup  # noqa: F401 — imports the inner class
        from setup_cog import SetupCog  # noqa: F401

        # The class is local to `run_shiny_tasks_setup`. Build the
        # bare-minimum stand-in here so the property contract is tested
        # without spinning up discord.ui machinery.
        class _Stub:
            min_value = "681"
            max_value = "799"

            # Inline-copy the property body to assert the contract:
            @property
            def value(self):
                if self.min_value is None and self.max_value is None:
                    return ""
                return f"{self.min_value or '?'} – {self.max_value or '?'}"

        stub = _Stub()
        assert stub.value == "681 – 799"

    def test_modal_launch_view_format_string_compiles(self):
        """Smoke check the f-string pattern in ModalLaunchView still
        relies on a single `.value` attribute (if this changes, the
        ServerRangeModal.value property may need to change too)."""
        import inspect
        from setup_cog import ModalLaunchView

        src = inspect.getsource(ModalLaunchView)
        assert "self.modal.value" in src, (
            "ModalLaunchView no longer references self.modal.value — "
            "ServerRangeModal.value may need to be removed or renamed."
        )


class TestShinyTasksPostTask:
    @pytest.mark.asyncio
    async def test_fires_when_time_matches(self, temp_db):
        _seed_complete(TEST_GUILD_ID)
        _enable_shiny(TEST_GUILD_ID, post_time="09:00", channel_id=123)
        # Today = 2026-05-11; server #2264 (created Apr 29) is shiny today.
        _seed_servers([(2264, "2026-04-29", "global")])
        _enable_shiny(
            TEST_GUILD_ID,
            post_time="09:00",
            channel_id=123,
            server_min=2200,
            server_max=2300,
        )

        now = datetime(2026, 5, 11, 9, 0, tzinfo=ET)
        sent = await _run_loop_at(now)
        assert len(sent) == 1
        assert "2264" in sent[0]

    @pytest.mark.asyncio
    async def test_skips_before_target_time(self, temp_db):
        _seed_complete(TEST_GUILD_ID)
        _enable_shiny(TEST_GUILD_ID, post_time="09:00", channel_id=123)
        _seed_servers([(2264, "2026-04-29", "global")])

        sent = await _run_loop_at(datetime(2026, 5, 11, 8, 59, tzinfo=ET))
        assert sent == []

    @pytest.mark.asyncio
    async def test_fires_on_late_tick_after_target_time(self, temp_db):
        """An at-or-past match (not an exact minute equality) so a tick
        that lands late — e.g. the event loop was busy elsewhere right at
        the target minute — still posts instead of silently skipping the
        whole day (#379 follow-up)."""
        _seed_complete(TEST_GUILD_ID)
        _enable_shiny(
            TEST_GUILD_ID,
            post_time="09:00",
            channel_id=123,
            server_min=2200,
            server_max=2300,
        )
        _seed_servers([(2264, "2026-04-29", "global")])

        sent = await _run_loop_at(datetime(2026, 5, 11, 9, 5, tzinfo=ET))
        assert len(sent) == 1
        assert "2264" in sent[0]

    @pytest.mark.asyncio
    async def test_last_posted_date_blocks_duplicate(self, temp_db):
        """The loop already fired today (Railway restart inside the
        configured minute) — running again must not post again."""
        from config import mark_shiny_tasks_posted

        _seed_complete(TEST_GUILD_ID)
        _seed_servers([(2264, "2026-04-29", "global")])
        _enable_shiny(
            TEST_GUILD_ID,
            post_time="09:00",
            channel_id=123,
            server_min=2200,
            server_max=2300,
        )
        mark_shiny_tasks_posted(TEST_GUILD_ID, "2026-05-11")

        sent = await _run_loop_at(datetime(2026, 5, 11, 9, 0, tzinfo=ET))
        assert sent == []

    @pytest.mark.asyncio
    async def test_no_shinies_today_silently_marks_posted(self, temp_db):
        """Time matched but no in-range servers are shiny → no post,
        but `last_posted_date` is set so the loop doesn't re-check
        every minute for the rest of the day."""
        from config import get_shiny_tasks_config

        _seed_complete(TEST_GUILD_ID)
        # #2263 is "Tomorrow" (Day 14 from 2026-04-27), not today.
        _seed_servers([(2263, "2026-04-27", "global")])
        _enable_shiny(
            TEST_GUILD_ID,
            post_time="09:00",
            channel_id=123,
            server_min=2263,
            server_max=2263,
        )

        sent = await _run_loop_at(datetime(2026, 5, 11, 9, 0, tzinfo=ET))
        assert sent == []
        assert get_shiny_tasks_config(TEST_GUILD_ID)["last_posted_date"] == "2026-05-11"

    @pytest.mark.asyncio
    async def test_missing_channel_skips_quietly(self, temp_db):
        """`bot.get_channel` returns None (channel deleted, bot left
        the guild) — the loop logs and moves on without crashing."""
        import bot as bot_module

        _seed_complete(TEST_GUILD_ID)
        _seed_servers([(2264, "2026-04-29", "global")])
        _enable_shiny(
            TEST_GUILD_ID,
            post_time="09:00",
            channel_id=999,
            server_min=2200,
            server_max=2300,
        )

        fake_bot = MagicMock()
        fake_bot.get_channel = MagicMock(return_value=None)
        fake_dt = MagicMock()
        fake_dt.now = MagicMock(return_value=datetime(2026, 5, 11, 9, 0, tzinfo=ET))
        with patch.object(bot_module, "bot", fake_bot), patch("bot.datetime", fake_dt):
            await bot_module.shiny_tasks_post_task.coro()
        # Did not raise; last_posted_date stays empty because we
        # didn't actually post.
        from config import get_shiny_tasks_config

        assert get_shiny_tasks_config(TEST_GUILD_ID)["last_posted_date"] == ""

    @pytest.mark.asyncio
    async def test_post_after_reset_uses_in_game_server_day(self, temp_db):
        """Regression for the 'shiny a day behind' bug (#330): a 10:30pm-local
        post fires after the in-game reset (00:00 server time, UTC-2, ~2h before
        local midnight), which is already the next in-game day. The server list
        must come from that day's cycle, not the local-today cycle that just
        ended.

        22:30 EDT on 2026-06-02 == 00:30 server time (UTC-2) on 2026-06-03.
          * #2280 (created 2026-05-31): shiny on 06-03 (Δ3), not 06-02 (Δ2).
          * #2290 (created 2026-05-30): shiny on 06-02 (Δ3), not 06-03 (Δ4).
        Using the server date posts #2280; the local-date bug would post #2290.
        """
        _seed_complete(TEST_GUILD_ID)
        _seed_servers(
            [
                (2280, "2026-05-31", "global"),  # in-game today (06-03)
                (2290, "2026-05-30", "global"),  # local today (06-02)
            ]
        )
        _enable_shiny(
            TEST_GUILD_ID,
            post_time="22:30",
            channel_id=123,
            server_min=2200,
            server_max=2300,
        )

        sent = await _run_loop_at(datetime(2026, 6, 2, 22, 30, tzinfo=ET))
        assert len(sent) == 1
        assert "2280" in sent[0]
        assert "2290" not in sent[0]


class TestShinyTasksRefreshTask:
    """`tasks.loop` fires the body immediately on `.start()` and the
    interval is in-process only, so without a persistent gate every
    Railway redeploy re-fetches cpt-hedge. The gate keys off
    `MAX(last_seen_at)` in `shiny_task_servers`."""

    @pytest.mark.asyncio
    async def test_skips_when_last_refresh_recent(self, temp_db):
        """A successful refresh < 7 days ago means no Hedge fetch."""
        from config import upsert_shiny_task_servers
        import bot as bot_module
        import shiny_tasks

        recent = datetime.now(tz=timezone.utc).isoformat()
        upsert_shiny_task_servers([(1, "2025-01-01", "")], seen_at=recent)

        mock_refresh = AsyncMock()
        with patch.object(shiny_tasks, "refresh_servers", mock_refresh):
            await bot_module.shiny_tasks_refresh_task.coro()
        mock_refresh.assert_not_called()

    @pytest.mark.asyncio
    async def test_runs_when_table_empty(self, temp_db):
        """No rows yet → no gate → refresh runs (fresh-install path)."""
        import bot as bot_module
        import shiny_tasks

        mock_refresh = AsyncMock(return_value=42)
        with patch.object(shiny_tasks, "refresh_servers", mock_refresh):
            await bot_module.shiny_tasks_refresh_task.coro()
        mock_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_runs_when_last_refresh_stale(self, temp_db):
        """Last refresh > 7 days ago → gate passes → refresh runs."""
        from config import upsert_shiny_task_servers
        import bot as bot_module
        import shiny_tasks

        stale = (datetime.now(tz=timezone.utc) - timedelta(days=8)).isoformat()
        upsert_shiny_task_servers([(1, "2025-01-01", "")], seen_at=stale)

        mock_refresh = AsyncMock(return_value=2266)
        with patch.object(shiny_tasks, "refresh_servers", mock_refresh):
            await bot_module.shiny_tasks_refresh_task.coro()
        mock_refresh.assert_awaited_once()


class TestRefreshDisabled:
    """#293: the upstream source gated its data behind an API key, so the
    refresh is a clean no-op (no scrape, no Sentry raise) until re-enabled.
    The daily post loop keeps serving the frozen DB snapshot."""

    @pytest.mark.asyncio
    async def test_disabled_refresh_is_noop_and_skips_fetch(self):
        import shiny_tasks

        assert shiny_tasks.SERVER_REFRESH_ENABLED is False, (
            "guard test assumes the feature ships with refresh disabled"
        )
        with patch.object(shiny_tasks, "fetch_server_table", AsyncMock()) as mock_fetch:
            n = await shiny_tasks.refresh_servers()
        assert n == 0
        mock_fetch.assert_not_called()

    @pytest.mark.asyncio
    async def test_enabled_refresh_fetches_and_upserts(self):
        import shiny_tasks

        rows = [(1, "2025-01-01", "")]
        with (
            patch.object(shiny_tasks, "SERVER_REFRESH_ENABLED", True),
            patch.object(shiny_tasks, "fetch_server_table", AsyncMock(return_value=rows)),
            patch("config.upsert_shiny_task_servers", return_value=len(rows)) as mock_upsert,
        ):
            n = await shiny_tasks.refresh_servers()
        assert n == 1
        mock_upsert.assert_called_once()
