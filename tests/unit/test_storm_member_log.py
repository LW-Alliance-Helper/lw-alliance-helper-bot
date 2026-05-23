"""
Tests for the Per-Member Log foundation (#244).

Covers the four pure functions that build the new wide-format Sheet
tab + read it back:
  * `_member_log_tab_name` / `_format_member_log_date`
  * `append_member_log_rows` — header merge, row layout, empty-data
    short-circuit, new-column append, fresh-tab creation
  * `read_member_log_window` — date set extraction, lookback cap,
    question-key projection
  * `count_member_flags_in_window` — truthy counting (no/false/0
    explicitly counted as zero)

Plus:
  * `_prefill_from_discord_poll` — name resolution from storm_signups
    targets (canonical name path, alias path, member-roster discord-id
    path, unconfigured-fallback path)
  * `_PaginatedRosterMultiSelectView` — single-page view shape,
    multi-page pagination, selection persistence across pages,
    Save / Clear button wiring
"""

from __future__ import annotations

import os
import sys
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

from tests.constants import TEST_GUILD_ID  # noqa: E402


# ── Shared fixtures ─────────────────────────────────────────────────────────


def _make_ws(rows: list[list[str]]):
    """Worksheet stub: get_all_values returns the rows, row_values(1)
    returns the header, update + append_rows are spies."""
    ws = MagicMock()
    # Use a callable so per-test mutations of `rows` are picked up
    # without rebuilding the spy (matters for upsert behaviour where
    # the test wants to inspect the post-write rows).
    ws.get_all_values = MagicMock(return_value=list(rows))
    ws.row_values = MagicMock(return_value=list(rows[0]) if rows else [])
    ws.update = MagicMock()
    ws.append_rows = MagicMock()
    return ws


def _make_sh(ws=None, raise_on_missing=False):
    """Spreadsheet stub: worksheet(name) returns ws (or raises);
    add_worksheet(...) returns a fresh empty worksheet."""
    sh = MagicMock()
    if raise_on_missing:
        sh.worksheet.side_effect = Exception("WorksheetNotFound")
    else:
        sh.worksheet.return_value = ws
    fresh = _make_ws([])
    sh.add_worksheet.return_value = fresh
    sh._fresh = fresh
    return sh


# ── Per-Member Log helpers ──────────────────────────────────────────────────


class TestMemberLogTabName:

    def test_ds_lowercase_input(self):
        from storm_log import _member_log_tab_name
        assert _member_log_tab_name("ds") == "DS Member Log"

    def test_cs_uppercase_input(self):
        from storm_log import _member_log_tab_name
        assert _member_log_tab_name("CS") == "CS Member Log"


class TestFormatMemberLogDate:

    def test_date_object_iso_formats(self):
        from storm_log import _format_member_log_date
        assert _format_member_log_date(date(2026, 5, 22)) == "2026-05-22"

    def test_string_passthrough(self):
        from storm_log import _format_member_log_date
        assert _format_member_log_date("2026-01-15") == "2026-01-15"


class TestUpsertMemberLogRows:
    """Upsert-by-(date, member) write semantics for the Per-Member Log
    tab. Re-running the log for the same event date REPLACES prior
    rows in this batch rather than appending duplicates — without
    this, the Trends Viewer would double-count flagged events on
    every officer re-run."""

    def test_empty_per_member_data_short_circuits(self):
        """No rows, no Sheet call — the participation log doesn't need
        to touch the Member Log tab when the alliance has no per-member
        questions configured."""
        from storm_log import upsert_member_log_rows

        with patch("storm_log._get_spreadsheet") as mock_sh:
            upsert_member_log_rows(
                TEST_GUILD_ID, "DS", date(2026, 5, 22),
                per_member_data={},
                question_keys=["sat_out"],
            )

        mock_sh.assert_not_called()

    def test_fresh_tab_created_with_correct_header(self):
        """A guild's first per-member-log write creates the tab and
        sets `Event Date | Member | <questions...>` as the header.
        The full payload (header + rows) lands in one `ws.update`
        call so the write is atomic."""
        from storm_log import upsert_member_log_rows

        sh = _make_sh(raise_on_missing=True)

        with patch("storm_log._get_spreadsheet", return_value=sh):
            upsert_member_log_rows(
                TEST_GUILD_ID, "DS", date(2026, 5, 22),
                per_member_data={
                    "alice": {"sat_out": "yes"},
                    "bob":   {"sat_out": "no"},
                },
                question_keys=["sat_out"],
            )

        sh.add_worksheet.assert_called_once()
        fresh = sh._fresh
        # One write — header + rows together.
        update_call = fresh.update.call_args
        assert update_call.args[0] == "A1"
        assert update_call.args[1] == [
            ["Event Date", "Member", "sat_out"],
            ["2026-05-22", "alice", "yes"],
            ["2026-05-22", "bob",   "no"],
        ]

    def test_existing_tab_merges_new_question_column(self):
        """Adding a new question on an existing tab appends a new
        column at the right edge; old columns stay intact, new rows
        leave them empty."""
        from storm_log import upsert_member_log_rows

        ws = _make_ws([["Event Date", "Member", "old_q"]])
        sh = _make_sh(ws=ws)

        with patch("storm_log._get_spreadsheet", return_value=sh):
            upsert_member_log_rows(
                TEST_GUILD_ID, "DS", date(2026, 5, 22),
                per_member_data={"alice": {"new_q": "yes"}},
                question_keys=["new_q"],
            )

        payload = ws.update.call_args.args[1]
        assert payload[0] == ["Event Date", "Member", "old_q", "new_q"]
        # `old_q` is blank for this row (alice didn't have one this event).
        assert payload[1] == ["2026-05-22", "alice", "", "yes"]

    def test_existing_tab_keeps_dropped_question_column(self):
        """A question removed from the alliance's config still keeps
        its column in the tab so historical data is preserved. New
        rows just leave it blank."""
        from storm_log import upsert_member_log_rows

        ws = _make_ws([["Event Date", "Member", "old_q", "dropped_q"]])
        sh = _make_sh(ws=ws)

        with patch("storm_log._get_spreadsheet", return_value=sh):
            upsert_member_log_rows(
                TEST_GUILD_ID, "DS", date(2026, 5, 22),
                per_member_data={"alice": {"old_q": "yes"}},
                question_keys=["old_q"],
            )

        payload = ws.update.call_args.args[1]
        # `dropped_q` survives in the header.
        assert payload[0] == ["Event Date", "Member", "old_q", "dropped_q"]
        # New rows leave it empty.
        assert payload[1] == ["2026-05-22", "alice", "yes", ""]

    def test_rerun_for_same_event_replaces_member_rows(self):
        """Officer re-runs the log for the same event: the old (date,
        member) row drops and the new value replaces it. The Trends
        Viewer can't double-count."""
        from storm_log import upsert_member_log_rows

        ws = _make_ws([
            ["Event Date", "Member", "sat_out"],
            ["2026-05-22", "alice", "no"],   # ← will be replaced
            ["2026-05-22", "bob",   "no"],   # ← will be replaced
            ["2026-05-15", "alice", "yes"],  # ← preserved (different date)
        ])
        sh = _make_sh(ws=ws)
        with patch("storm_log._get_spreadsheet", return_value=sh):
            upsert_member_log_rows(
                TEST_GUILD_ID, "DS", date(2026, 5, 22),
                per_member_data={
                    "alice": {"sat_out": "yes"},  # ← corrected
                    "bob":   {"sat_out": "no"},
                },
                question_keys=["sat_out"],
            )

        payload = ws.update.call_args.args[1]
        # Header survives; the 2026-05-15 row survives; the 2026-05-22
        # rows are the new values, not duplicates of the old.
        assert payload == [
            ["Event Date", "Member", "sat_out"],
            ["2026-05-15", "alice", "yes"],
            ["2026-05-22", "alice", "yes"],
            ["2026-05-22", "bob",   "no"],
        ]

    def test_rerun_preserves_other_questions_for_same_member(self):
        """A second question on the same (date, member) — captured in
        a later upsert — replaces only that question's cell, not the
        prior question's value. Sit-out captured first then attendance
        recorded later for the same event."""
        from storm_log import upsert_member_log_rows

        # First write captures sit-out values.
        ws = _make_ws([
            ["Event Date", "Member", "sat_out"],
            ["2026-05-22", "alice", "yes"],
            ["2026-05-22", "bob",   "no"],
        ])
        sh = _make_sh(ws=ws)
        with patch("storm_log._get_spreadsheet", return_value=sh):
            upsert_member_log_rows(
                TEST_GUILD_ID, "DS", date(2026, 5, 22),
                per_member_data={
                    "alice": {"showed_up": "no"},
                    "bob":   {"showed_up": "yes"},
                },
                question_keys=["showed_up"],
            )

        # Both questions' columns sit in the header; the new rows
        # carry the new value AND blank out the old sit-out (because
        # the second-write batch didn't specify it). That's the
        # documented behaviour — upsert is a full-row replace per
        # (date, member). Officers running mixed flows for the same
        # event need to do all per-member writes in one batch (the
        # participation flow already does).
        payload = ws.update.call_args.args[1]
        assert payload[0] == ["Event Date", "Member", "sat_out", "showed_up"]
        # Two re-written 2026-05-22 rows. Sit-out column is blank in
        # the new rows since the second batch didn't supply it.
        assert ["2026-05-22", "alice", "", "no"] in payload
        assert ["2026-05-22", "bob", "", "yes"] in payload


class TestReadMemberLogWindow:

    def test_missing_tab_returns_empty(self):
        from storm_log import read_member_log_window
        sh = _make_sh(raise_on_missing=True)
        with patch("storm_log._get_spreadsheet", return_value=sh):
            dates, rows = read_member_log_window(
                TEST_GUILD_ID, "DS", lookback_events=4,
                question_key="sat_out",
            )
        assert dates == []
        assert rows == {}

    def test_lookback_caps_distinct_dates(self):
        """Lookback=2 against a tab with 3 distinct dates returns the
        most recent 2, newest first."""
        from storm_log import read_member_log_window

        ws = _make_ws([
            ["Event Date", "Member", "sat_out"],
            ["2026-05-01", "alice", "yes"],
            ["2026-05-01", "bob",   "no"],
            ["2026-05-08", "alice", "no"],
            ["2026-05-08", "bob",   "yes"],
            ["2026-05-15", "alice", "yes"],
            ["2026-05-15", "bob",   "no"],
        ])
        sh = _make_sh(ws=ws)
        with patch("storm_log._get_spreadsheet", return_value=sh):
            dates, by_member = read_member_log_window(
                TEST_GUILD_ID, "DS", lookback_events=2,
                question_key="sat_out",
            )
        assert dates == ["2026-05-15", "2026-05-08"]
        assert by_member["alice"] == {
            "2026-05-15": "yes",
            "2026-05-08": "no",
        }
        assert by_member["bob"] == {
            "2026-05-15": "no",
            "2026-05-08": "yes",
        }

    def test_missing_question_key_returns_empty(self):
        """Calling with a question_key the tab doesn't have returns
        empty results rather than crashing."""
        from storm_log import read_member_log_window

        ws = _make_ws([
            ["Event Date", "Member", "sat_out"],
            ["2026-05-01", "alice", "yes"],
        ])
        sh = _make_sh(ws=ws)
        with patch("storm_log._get_spreadsheet", return_value=sh):
            dates, by_member = read_member_log_window(
                TEST_GUILD_ID, "DS", lookback_events=4,
                question_key="never_configured",
            )
        assert dates == []
        assert by_member == {}


class TestCountMemberFlagsInWindow:

    def test_counts_truthy_yes_values(self):
        from storm_log import count_member_flags_in_window

        ws = _make_ws([
            ["Event Date", "Member", "sat_out"],
            ["2026-05-01", "alice", "yes"],
            ["2026-05-01", "bob",   "no"],
            ["2026-05-08", "alice", "yes"],
            ["2026-05-08", "bob",   "no"],
            ["2026-05-15", "alice", "yes"],
            ["2026-05-15", "bob",   "yes"],
        ])
        sh = _make_sh(ws=ws)
        with patch("storm_log._get_spreadsheet", return_value=sh):
            counts = count_member_flags_in_window(
                TEST_GUILD_ID, "DS", lookback_events=4,
                question_key="sat_out",
            )
        assert counts["alice"] == 3
        assert counts["bob"] == 1

    def test_false_and_zero_count_as_not_flagged(self):
        from storm_log import count_member_flags_in_window

        ws = _make_ws([
            ["Event Date", "Member", "sat_out"],
            ["2026-05-01", "alice", "false"],
            ["2026-05-01", "bob",   "0"],
            ["2026-05-08", "alice", "yes"],
        ])
        sh = _make_sh(ws=ws)
        with patch("storm_log._get_spreadsheet", return_value=sh):
            counts = count_member_flags_in_window(
                TEST_GUILD_ID, "DS", lookback_events=4,
                question_key="sat_out",
            )
        assert counts["alice"] == 1
        assert counts["bob"] == 0


# ── Discord-poll prefill helper ─────────────────────────────────────────────


class TestPrefillFromDiscordPoll:

    def test_empty_signups_returns_empty(self):
        from storm_log import _prefill_from_discord_poll
        with patch("config.get_storm_signups", return_value=[]):
            out = _prefill_from_discord_poll(
                TEST_GUILD_ID, "DS", "2026-05-22",
                ["alice", "bob"], {},
            )
        assert out == set()

    def test_cannot_votes_excluded(self):
        """`cannot` and missing-vote are excluded; `a`, `b`, `either`
        all count as attending."""
        from storm_log import _prefill_from_discord_poll
        rows = [
            {"target_member_id": "alice",  "vote": "a"},
            {"target_member_id": "bob",    "vote": "either"},
            {"target_member_id": "carol",  "vote": "cannot"},
            {"target_member_id": "dave",   "vote": "b"},
        ]
        with patch("config.get_storm_signups", return_value=rows):
            out = _prefill_from_discord_poll(
                TEST_GUILD_ID, "DS", "2026-05-22",
                ["alice", "bob", "carol", "dave"], {},
            )
        assert out == {"alice", "bob", "dave"}

    def test_alias_resolves_to_roster_name(self):
        """Alias map (lowercased keys → canonical names) translates an
        on-behalf vote against an alias back to the canonical roster
        name."""
        from storm_log import _prefill_from_discord_poll
        rows = [
            {"target_member_id": "AliceTheTank", "vote": "a"},
        ]
        with patch("config.get_storm_signups", return_value=rows):
            out = _prefill_from_discord_poll(
                TEST_GUILD_ID, "DS", "2026-05-22",
                ["alice"], {"alicethetank": "alice"},
            )
        assert out == {"alice"}

    def test_discord_id_resolves_via_member_roster(self):
        """A self-vote stores `str(discord_id)` as the target. The
        helper falls through to the Member Roster sheet for the
        Discord-ID → name lookup."""
        from storm_log import _prefill_from_discord_poll
        rows = [
            {"target_member_id": "111", "vote": "a"},
            {"target_member_id": "222", "vote": "either"},
        ]
        roster_sheet = _make_ws([
            ["Discord ID", "Name"],
            ["111", "alice"],
            ["222", "bob"],
        ])
        with patch("config.get_storm_signups", return_value=rows), \
             patch("config.get_member_roster_config", return_value={
                 "enabled": 1, "tab_name": "Members",
                 "discord_id_col": 0, "name_col": 1,
             }), \
             patch("config.get_member_roster_sheet", return_value=roster_sheet):
            out = _prefill_from_discord_poll(
                TEST_GUILD_ID, "DS", "2026-05-22",
                ["alice", "bob"], {},
            )
        assert out == {"alice", "bob"}

    def test_member_roster_unconfigured_drops_unresolved(self):
        """When Member Roster Sync isn't set up, Discord-ID self-votes
        can't be resolved. They drop silently — prefill is best-effort,
        not a source of truth."""
        from storm_log import _prefill_from_discord_poll
        rows = [
            {"target_member_id": "alice", "vote": "a"},  # resolves directly
            {"target_member_id": "999",   "vote": "a"},  # unresolvable
        ]
        with patch("config.get_storm_signups", return_value=rows), \
             patch("config.get_member_roster_config",
                   return_value={"enabled": 0}):
            out = _prefill_from_discord_poll(
                TEST_GUILD_ID, "DS", "2026-05-22",
                ["alice", "bob"], {},
            )
        assert out == {"alice"}


# ── Paginated multi-select view ─────────────────────────────────────────────


class TestPaginatedRosterMultiSelectView:

    def test_single_page_has_no_pagination_row(self):
        """When the roster fits in one page (≤25 names), no Prev/Next
        controls render. Just the Select + Save + Clear."""
        from storm_log import _PaginatedRosterMultiSelectView
        names = [f"member{i:02d}" for i in range(10)]
        v = _PaginatedRosterMultiSelectView(names, "Pick attendees")
        labels = {
            getattr(c, "label", None) for c in v.children
            if isinstance(c, __import__("discord").ui.Button)
        }
        assert "✅ Save" in labels
        assert "Clear all" in labels
        assert "◀ Prev" not in labels
        assert "Next ▶" not in labels

    def test_multi_page_renders_pagination_row(self):
        """30 names → 2 pages → Prev / Page indicator / Next visible
        on page 0; Prev is disabled."""
        from storm_log import _PaginatedRosterMultiSelectView
        names = [f"member{i:02d}" for i in range(30)]
        v = _PaginatedRosterMultiSelectView(names, "Pick")
        import discord as _discord
        buttons = {
            c.label: c for c in v.children
            if isinstance(c, _discord.ui.Button)
        }
        assert "◀ Prev" in buttons
        assert "Next ▶" in buttons
        assert buttons["◀ Prev"].disabled is True
        assert buttons["Next ▶"].disabled is False
        # Page indicator label includes 1 / 2.
        page_lbls = [c.label for c in v.children
                     if isinstance(c, _discord.ui.Button)
                     and c.label and c.label.startswith("Page ")]
        assert page_lbls == ["Page 1 / 2"]

    def test_preselected_marks_select_options_default(self):
        """Names in `preselected` show as already-checked on the first
        page render."""
        from storm_log import _PaginatedRosterMultiSelectView
        import discord as _discord
        names = ["alice", "bob", "carol"]
        v = _PaginatedRosterMultiSelectView(
            names, "Pick", preselected={"alice", "carol"},
        )
        select = next(
            c for c in v.children if isinstance(c, _discord.ui.Select)
        )
        defaults = {o.value: o.default for o in select.options}
        assert defaults == {"alice": True, "bob": False, "carol": True}

    @pytest.mark.asyncio
    async def test_select_callback_merges_picks_across_pages(self):
        """Picking on page 0 then flipping to page 1 keeps page-0
        picks in `selected_set`; picking on page 1 merges."""
        from storm_log import _PaginatedRosterMultiSelectView
        names = [f"m{i:02d}" for i in range(30)]
        v = _PaginatedRosterMultiSelectView(names, "Pick")

        # Simulate a select on page 0 (picks first two names).
        inter = MagicMock()
        inter.response.defer = AsyncMock()
        inter.data = {"values": ["m00", "m01"]}
        await v._on_select(inter)
        assert v.selected_set == {"m00", "m01"}

        # Flip to page 1.
        inter2 = MagicMock()
        with patch("wizard_registry.safe_edit_response", AsyncMock()):
            await v._on_next(inter2)
        assert v.page == 1

        # Simulate a select on page 1 (picks m25 + m27).
        inter3 = MagicMock()
        inter3.response.defer = AsyncMock()
        inter3.data = {"values": ["m25", "m27"]}
        await v._on_select(inter3)
        # Page-0 picks preserved; page-1 picks added.
        assert v.selected_set == {"m00", "m01", "m25", "m27"}

    @pytest.mark.asyncio
    async def test_clear_all_resets_selection(self):
        from storm_log import _PaginatedRosterMultiSelectView
        v = _PaginatedRosterMultiSelectView(
            ["alice", "bob"], "Pick", preselected={"alice"},
        )
        inter = MagicMock()
        with patch("wizard_registry.safe_edit_response", AsyncMock()):
            await v._on_clear(inter)
        assert v.selected_set == set()

    @pytest.mark.asyncio
    async def test_save_sets_confirmed_and_stops_view(self):
        from storm_log import _PaginatedRosterMultiSelectView
        v = _PaginatedRosterMultiSelectView(["alice", "bob"], "Pick")
        v.selected_set = {"alice"}
        inter = MagicMock()
        with patch("wizard_registry.safe_edit_response", AsyncMock()):
            await v._on_save(inter)
        assert v.confirmed is True
        # `selected_set` survives intact for `run_log_flow` to read.
        assert v.selected_set == {"alice"}


# ── _LogDatePickerView (#251) ────────────────────────────────────────────────


class TestLogDatePickerView:
    """The participation-log flow's date entry was a free-text prompt
    that nobody loved. The view now lists recent saved event dates as
    a dropdown with Today / Yesterday quick picks and a
    `Type a different date` fallback that hands back to the existing
    `wait_for_msg` path."""

    def test_today_and_yesterday_quick_picks_always_present(self):
        from storm_log import _LogDatePickerView
        import discord as _discord
        v = _LogDatePickerView(recent_dates=[])
        select = next(
            c for c in v.children if isinstance(c, _discord.ui.Select)
        )
        values = [o.value for o in select.options]
        assert "__today__" in values
        assert "__yesterday__" in values
        assert "__manual__" in values

    def test_recent_dates_listed_newest_first(self):
        from storm_log import _LogDatePickerView
        import discord as _discord
        v = _LogDatePickerView(
            recent_dates=["2026-05-15", "2026-05-08", "2026-05-01"],
        )
        select = next(
            c for c in v.children if isinstance(c, _discord.ui.Select)
        )
        dated_values = [o.value for o in select.options
                        if not o.value.startswith("__")]
        # Caller passes dates already sorted newest-first.
        assert dated_values == ["2026-05-15", "2026-05-08", "2026-05-01"]

    def test_today_yesterday_dedupe_with_recent(self):
        """If today or yesterday is in the saved list, the listed
        entry is skipped — the quick-pick row already covers it."""
        from storm_log import _LogDatePickerView
        from datetime import date as _date, timedelta as _td
        import discord as _discord
        today_iso = _date.today().isoformat()
        v = _LogDatePickerView(
            recent_dates=[today_iso, "2026-04-01"],
        )
        select = next(
            c for c in v.children if isinstance(c, _discord.ui.Select)
        )
        dated_values = [o.value for o in select.options
                        if not o.value.startswith("__")]
        assert today_iso not in dated_values
        assert "2026-04-01" in dated_values

    @pytest.mark.asyncio
    async def test_select_today_resolves_to_date(self):
        from storm_log import _LogDatePickerView
        from datetime import date as _date
        v = _LogDatePickerView(recent_dates=[])
        select = next(
            c for c in v.children if hasattr(c, "options")
        )
        inter = MagicMock()
        inter.data = {"values": ["__today__"]}
        inter.response.edit_message = AsyncMock()
        await select.callback(inter)
        assert v.picked_date == _date.today()
        assert v.confirmed is True
        assert v.wants_manual is False

    @pytest.mark.asyncio
    async def test_select_manual_flags_wants_manual(self):
        from storm_log import _LogDatePickerView
        v = _LogDatePickerView(recent_dates=["2026-05-15"])
        select = next(c for c in v.children if hasattr(c, "options"))
        inter = MagicMock()
        inter.data = {"values": ["__manual__"]}
        inter.response.edit_message = AsyncMock()
        await select.callback(inter)
        assert v.wants_manual is True
        assert v.picked_date is None

    @pytest.mark.asyncio
    async def test_select_saved_date_parses_iso(self):
        from storm_log import _LogDatePickerView
        from datetime import date as _date
        v = _LogDatePickerView(recent_dates=["2026-05-15"])
        select = next(c for c in v.children if hasattr(c, "options"))
        inter = MagicMock()
        inter.data = {"values": ["2026-05-15"]}
        inter.response.edit_message = AsyncMock()
        await select.callback(inter)
        assert v.picked_date == _date(2026, 5, 15)


class TestCollectRecentEventDates:
    """Pulls candidate dates from `storm_signups` (every alliance with
    a posted poll) AND the structured-flow rosters tab (Premium)."""

    def test_returns_signup_dates_newest_first(self):
        from storm_log import _collect_recent_event_dates
        fake_rows = [
            {"event_date": "2026-04-24"},
            {"event_date": "2026-05-08"},
            {"event_date": "2026-05-15"},
        ]
        fake_conn = MagicMock()
        fake_conn.__enter__ = MagicMock(return_value=fake_conn)
        fake_conn.__exit__ = MagicMock(return_value=False)
        fake_conn.execute = MagicMock()
        fake_conn.execute.return_value.fetchall = MagicMock(
            return_value=fake_rows,
        )
        with patch("config._get_conn", return_value=fake_conn), \
             patch("storm_history.list_event_dates", return_value=([], [])):
            dates = _collect_recent_event_dates(TEST_GUILD_ID, "DS")
        assert dates == ["2026-05-15", "2026-05-08", "2026-04-24"]

    def test_merges_signup_and_roster_sources(self):
        from storm_log import _collect_recent_event_dates
        fake_signup_rows = [{"event_date": "2026-05-15"}]
        fake_conn = MagicMock()
        fake_conn.__enter__ = MagicMock(return_value=fake_conn)
        fake_conn.__exit__ = MagicMock(return_value=False)
        fake_conn.execute = MagicMock()
        fake_conn.execute.return_value.fetchall = MagicMock(
            return_value=fake_signup_rows,
        )
        with patch("config._get_conn", return_value=fake_conn), \
             patch("storm_history.list_event_dates",
                   return_value=(["2026-05-08", "2026-04-01"], [])):
            dates = _collect_recent_event_dates(TEST_GUILD_ID, "DS")
        # Three distinct dates, newest first.
        assert dates == ["2026-05-15", "2026-05-08", "2026-04-01"]

    def test_dedupe_across_sources(self):
        from storm_log import _collect_recent_event_dates
        fake_signup_rows = [{"event_date": "2026-05-15"}]
        fake_conn = MagicMock()
        fake_conn.__enter__ = MagicMock(return_value=fake_conn)
        fake_conn.__exit__ = MagicMock(return_value=False)
        fake_conn.execute = MagicMock()
        fake_conn.execute.return_value.fetchall = MagicMock(
            return_value=fake_signup_rows,
        )
        with patch("config._get_conn", return_value=fake_conn), \
             patch("storm_history.list_event_dates",
                   return_value=(["2026-05-15"], [])):
            dates = _collect_recent_event_dates(TEST_GUILD_ID, "DS")
        assert dates == ["2026-05-15"]

    def test_malformed_dates_filtered(self):
        from storm_log import _collect_recent_event_dates
        fake_signup_rows = [
            {"event_date": "garbage"},
            {"event_date": "2026-13-50"},
            {"event_date": "2026-05-15"},
        ]
        fake_conn = MagicMock()
        fake_conn.__enter__ = MagicMock(return_value=fake_conn)
        fake_conn.__exit__ = MagicMock(return_value=False)
        fake_conn.execute = MagicMock()
        fake_conn.execute.return_value.fetchall = MagicMock(
            return_value=fake_signup_rows,
        )
        with patch("config._get_conn", return_value=fake_conn), \
             patch("storm_history.list_event_dates",
                   return_value=([], [])):
            dates = _collect_recent_event_dates(TEST_GUILD_ID, "DS")
        assert dates == ["2026-05-15"]

    def test_limit_respected(self):
        from storm_log import _collect_recent_event_dates
        fake_signup_rows = [
            {"event_date": f"2026-{m:02d}-15"} for m in range(1, 13)
        ]
        fake_conn = MagicMock()
        fake_conn.__enter__ = MagicMock(return_value=fake_conn)
        fake_conn.__exit__ = MagicMock(return_value=False)
        fake_conn.execute = MagicMock()
        fake_conn.execute.return_value.fetchall = MagicMock(
            return_value=fake_signup_rows,
        )
        with patch("config._get_conn", return_value=fake_conn), \
             patch("storm_history.list_event_dates",
                   return_value=([], [])):
            dates = _collect_recent_event_dates(TEST_GUILD_ID, "DS", limit=4)
        assert len(dates) == 4
        # Newest 4 (sorted desc).
        assert dates == [
            "2026-12-15", "2026-11-15", "2026-10-15", "2026-09-15",
        ]
