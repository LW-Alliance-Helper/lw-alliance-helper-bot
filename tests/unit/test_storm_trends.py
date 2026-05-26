"""
Tests for the Storm Trends Viewer (#246).

Pure-Python query function (`query_member_log`) gets the deepest
coverage — fixture rows, every operator, the team-plan filter, and
the truncated/empty result branches. The view + handler get
lightweight render-shape checks; the gspread layer is exercised
indirectly via storm_log's read functions which are already covered
in `test_storm_member_log.py`.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

from tests.constants import TEST_GUILD_ID  # noqa: E402


def _make_ws(rows: list[list[str]]):
    ws = MagicMock()
    ws.get_all_values = MagicMock(return_value=list(rows))
    return ws


def _make_sh(ws):
    sh = MagicMock()
    sh.worksheet.return_value = ws
    return sh


def _patch_member_log(rows: list[list[str]]):
    """Patch `storm_log._get_spreadsheet` so `read_member_log_window`
    and `count_member_flags_in_window` see the fake rows."""
    return patch(
        "storm_log._get_spreadsheet",
        return_value=_make_sh(_make_ws(rows)),
    )


# ── Pure query function ────────────────────────────────────────────────────


class TestQueryMemberLog:

    def test_greater_than_or_equal_filters_correctly(self):
        from storm_trends import query_member_log
        rows = [
            ["Event Date", "Member", "sat_out"],
            ["2026-05-01", "alice", "yes"],
            ["2026-05-01", "bob",   "no"],
            ["2026-05-01", "carol", "no"],
            ["2026-05-08", "alice", "yes"],
            ["2026-05-08", "bob",   "no"],
            ["2026-05-08", "carol", "yes"],
            ["2026-05-15", "alice", "yes"],
            ["2026-05-15", "bob",   "yes"],
            ["2026-05-15", "carol", "no"],
        ]
        with _patch_member_log(rows):
            out = query_member_log(
                TEST_GUILD_ID, "DS",
                question_key="sat_out",
                operator_sym=">=",
                threshold=2,
                lookback_events=4,
            )
        # alice: 3, bob: 1, carol: 1.
        # Threshold 2, op ≥ → alice only.
        names = [r["member"] for r in out["results"]]
        assert names == ["alice"]
        assert out["results"][0]["count"] == 3
        assert out["results"][0]["last_flagged"] == "2026-05-15"
        assert out["total_events_captured"] == 3
        assert out["truncated"] is False

    def test_greater_than_strict(self):
        from storm_trends import query_member_log
        rows = [
            ["Event Date", "Member", "sat_out"],
            ["2026-05-01", "alice", "yes"],
            ["2026-05-08", "alice", "yes"],
            ["2026-05-01", "bob",   "yes"],
            ["2026-05-08", "bob",   "yes"],
        ]
        with _patch_member_log(rows):
            out = query_member_log(
                TEST_GUILD_ID, "DS",
                question_key="sat_out",
                operator_sym=">",
                threshold=2,
                lookback_events=4,
            )
        # Both have count=2; > 2 → none.
        assert out["results"] == []

    def test_equal_to(self):
        from storm_trends import query_member_log
        rows = [
            ["Event Date", "Member", "sat_out"],
            ["2026-05-01", "alice", "yes"],
            ["2026-05-08", "alice", "no"],
            ["2026-05-01", "bob",   "yes"],
            ["2026-05-08", "bob",   "yes"],
        ]
        with _patch_member_log(rows):
            out = query_member_log(
                TEST_GUILD_ID, "DS",
                question_key="sat_out",
                operator_sym="==",
                threshold=1,
                lookback_events=4,
            )
        names = [r["member"] for r in out["results"]]
        assert names == ["alice"]

    def test_less_than_includes_zeros(self):
        """`< 1` returns everyone with zero flagged events too."""
        from storm_trends import query_member_log
        rows = [
            ["Event Date", "Member", "sat_out"],
            ["2026-05-01", "alice", "yes"],
            ["2026-05-01", "bob",   "no"],
        ]
        with _patch_member_log(rows):
            out = query_member_log(
                TEST_GUILD_ID, "DS",
                question_key="sat_out",
                operator_sym="<",
                threshold=1,
                lookback_events=4,
            )
        # alice=1, bob=0; < 1 → bob.
        names = [r["member"] for r in out["results"]]
        assert names == ["bob"]
        assert out["results"][0]["count"] == 0
        # No truthy values for bob → last_flagged stays empty.
        assert out["results"][0]["last_flagged"] == ""

    def test_lookback_limits_window(self):
        """Lookback=2 caps the query to the most-recent 2 distinct
        event dates — earlier flags don't count toward the total."""
        from storm_trends import query_member_log
        rows = [
            ["Event Date", "Member", "sat_out"],
            ["2026-05-01", "alice", "yes"],  # outside lookback=2
            ["2026-05-08", "alice", "yes"],
            ["2026-05-15", "alice", "yes"],
            ["2026-05-01", "bob",   "yes"],
            ["2026-05-08", "bob",   "no"],
            ["2026-05-15", "bob",   "no"],
        ]
        with _patch_member_log(rows):
            out = query_member_log(
                TEST_GUILD_ID, "DS",
                question_key="sat_out",
                operator_sym=">=",
                threshold=1,
                lookback_events=2,
            )
        # alice: 2 in window (05-08 + 05-15); bob: 0 in window.
        # ≥ 1 → alice only.
        names = [r["member"] for r in out["results"]]
        assert names == ["alice"]
        assert out["results"][0]["count"] == 2

    def test_results_sorted_by_count_desc_then_member_asc(self):
        from storm_trends import query_member_log
        rows = [["Event Date", "Member", "sat_out"]]
        for ev in ("2026-05-01", "2026-05-08"):
            rows.append([ev, "alice", "yes"])
            rows.append([ev, "bob",   "yes"])
            rows.append([ev, "carol", "yes"])
        # carol only on one event.
        rows.append(["2026-05-15", "alice", "yes"])
        rows.append(["2026-05-15", "bob",   "yes"])
        with _patch_member_log(rows):
            out = query_member_log(
                TEST_GUILD_ID, "DS",
                question_key="sat_out",
                operator_sym=">=",
                threshold=1,
                lookback_events=4,
            )
        names = [r["member"] for r in out["results"]]
        # alice & bob each 3, carol 2 → ties sorted by name asc.
        assert names == ["alice", "bob", "carol"]

    def test_empty_result_set(self):
        from storm_trends import query_member_log
        rows = [
            ["Event Date", "Member", "sat_out"],
            ["2026-05-01", "alice", "no"],
            ["2026-05-01", "bob",   "no"],
        ]
        with _patch_member_log(rows):
            out = query_member_log(
                TEST_GUILD_ID, "DS",
                question_key="sat_out",
                operator_sym=">=",
                threshold=1,
                lookback_events=4,
            )
        assert out["results"] == []
        assert out["total_events_captured"] == 1

    def test_no_events_captured_yet(self):
        from storm_trends import query_member_log
        rows = [["Event Date", "Member", "sat_out"]]  # header only
        with _patch_member_log(rows):
            out = query_member_log(
                TEST_GUILD_ID, "DS",
                question_key="sat_out",
                operator_sym=">=",
                threshold=1,
                lookback_events=4,
            )
        assert out["results"] == []
        assert out["total_events_captured"] == 0

    def test_truncation_at_25_rows(self):
        from storm_trends import query_member_log
        rows = [["Event Date", "Member", "sat_out"]]
        for i in range(30):
            rows.append(["2026-05-15", f"member{i:02d}", "yes"])
        with _patch_member_log(rows):
            out = query_member_log(
                TEST_GUILD_ID, "DS",
                question_key="sat_out",
                operator_sym=">=",
                threshold=1,
                lookback_events=4,
            )
        assert len(out["results"]) == 25
        assert out["truncated"] is True

    def test_team_filter_narrows_to_plan_members(self):
        """With a saved team-A plan, the team_filter=A constrains the
        match list to plan members only."""
        from storm_trends import query_member_log
        rows = [
            ["Event Date", "Member", "sat_out"],
            ["2026-05-01", "alice", "yes"],
            ["2026-05-01", "bob",   "yes"],
            ["2026-05-01", "carol", "yes"],
        ]
        with _patch_member_log(rows), \
             patch("storm_trends._team_plan_member_set",
                   return_value={"alice", "bob"}):
            out = query_member_log(
                TEST_GUILD_ID, "DS",
                question_key="sat_out",
                operator_sym=">=",
                threshold=1,
                lookback_events=4,
                team_filter="A",
            )
        names = [r["member"] for r in out["results"]]
        assert names == ["alice", "bob"]

    def test_team_filter_noop_when_no_plan(self):
        """No saved team plan → team filter no-ops and every match
        appears regardless of `A`/`B`."""
        from storm_trends import query_member_log
        rows = [
            ["Event Date", "Member", "sat_out"],
            ["2026-05-01", "alice", "yes"],
            ["2026-05-01", "bob",   "yes"],
        ]
        with _patch_member_log(rows), \
             patch("storm_trends._team_plan_member_set", return_value=None):
            out = query_member_log(
                TEST_GUILD_ID, "DS",
                question_key="sat_out",
                operator_sym=">=",
                threshold=1,
                lookback_events=4,
                team_filter="A",
            )
        names = [r["member"] for r in out["results"]]
        assert names == ["alice", "bob"]

    def test_invalid_operator_returns_empty(self):
        from storm_trends import query_member_log
        with _patch_member_log([["Event Date", "Member", "sat_out"]]):
            out = query_member_log(
                TEST_GUILD_ID, "DS",
                question_key="sat_out",
                operator_sym="garbage",
                threshold=1,
                lookback_events=4,
            )
        assert out["results"] == []


# ── Question discovery ─────────────────────────────────────────────────────


class TestListTrendableQuestions:

    def test_attendance_question_always_present(self):
        """The `showed_up` attendance question is always in the list,
        even when participation tracking is unconfigured — officers
        who only use /attendance still get a trends entry."""
        from storm_trends import list_trendable_questions
        with patch("config.get_participation_config",
                   return_value={"questions": []}):
            items = list_trendable_questions(TEST_GUILD_ID, "DS")
        keys = [q["key"] for q in items]
        assert "showed_up" in keys
        assert items[0]["source"] == "attendance"

    def test_roster_multi_select_questions_included(self):
        from storm_trends import list_trendable_questions
        with patch("config.get_participation_config", return_value={
            "questions": [
                {"key": "sat_out", "label": "Who sat out?",
                 "type": "roster_multi_select"},
                {"key": "didnt_vote", "label": "Who didn't vote?",
                 "type": "roster_multi_select"},
                {"key": "free_text", "label": "Notes",
                 "type": "text"},  # skipped — not trendable
            ],
        }):
            items = list_trendable_questions(TEST_GUILD_ID, "DS")
        labels = [q["label"] for q in items]
        # attendance is always first, then the configured questions.
        assert any("Who sat out?" in lab for lab in labels)
        assert any("Who didn't vote?" in lab for lab in labels)
        assert not any("Notes" in lab for lab in labels)

    def test_derived_count_questions_excluded(self):
        """Derived count writes the integer count to the Sheet, not a
        yes/no flag — querying "events flagged" on top doesn't make
        sense. Officers who want to trend a derived count should
        query its source question instead. The Trends Viewer keeps
        derived count out of the question list."""
        from storm_trends import list_trendable_questions
        with patch("config.get_participation_config", return_value={
            "questions": [
                {"key": "sit_out_streak", "label": "Sit-out streak",
                 "type": "derived_count"},
            ],
        }):
            items = list_trendable_questions(TEST_GUILD_ID, "DS")
        keys = [q["key"] for q in items]
        assert "sit_out_streak" not in keys


# ── Results text rendering ─────────────────────────────────────────────────


class TestRenderResultsText:

    def test_uses_words_not_symbols_for_operator(self):
        """Style memory: "operators use words" — text output should
        say "greater than or equal to", not "≥"."""
        from storm_trends import render_results_text
        text = render_results_text(
            event_type="DS",
            question_label="Who sat out?",
            operator_sym=">=",
            threshold=3,
            lookback_events=8,
            team_filter="both",
            query_out={"results": [], "total_events_captured": 8,
                       "truncated": False},
        )
        assert "greater than or equal to" in text
        assert "≥" not in text

    def test_team_filter_callout(self):
        from storm_trends import render_results_text
        text_a = render_results_text(
            event_type="DS",
            question_label="X",
            operator_sym=">=",
            threshold=1,
            lookback_events=4,
            team_filter="A",
            query_out={"results": [], "total_events_captured": 0,
                       "truncated": False},
        )
        assert "Team A only" in text_a

    def test_results_table_lines(self):
        from storm_trends import render_results_text
        text = render_results_text(
            event_type="DS",
            question_label="Who sat out?",
            operator_sym=">=",
            threshold=2,
            lookback_events=4,
            team_filter="both",
            query_out={
                "results": [
                    {"member": "alice", "count": 3, "last_flagged": "2026-05-15"},
                    {"member": "bob",   "count": 2, "last_flagged": "2026-05-08"},
                ],
                "total_events_captured": 4,
                "truncated": False,
            },
        )
        assert "alice: 3" in text
        assert "bob: 2" in text
        assert "2026-05-15" in text


# ── View rendering ─────────────────────────────────────────────────────────


class TestTrendsView:

    def _state(self, *, questions=None):
        from storm_trends import _TrendsState
        return _TrendsState(
            guild_id=TEST_GUILD_ID,
            user_id=42,
            event_type="DS",
            questions=questions if questions is not None else [
                {"key": "sat_out", "label": "Who sat out?",
                 "source": "participation"},
                {"key": "showed_up", "label": "Did this member show up?",
                 "source": "attendance"},
            ],
        )

    def test_view_empty_when_no_questions(self):
        from storm_trends import _TrendsView
        state = self._state(questions=[])
        view = _TrendsView(state)
        # No questions → no selects/buttons.
        assert view.children == []

    def test_view_renders_all_selects_and_buttons(self):
        from storm_trends import _TrendsView
        import discord as _discord
        view = _TrendsView(self._state())
        selects = [c for c in view.children if isinstance(c, _discord.ui.Select)]
        buttons = [c for c in view.children if isinstance(c, _discord.ui.Button)]
        # Question, operator, threshold, lookback = 4 selects.
        assert len(selects) == 4
        # Team toggle + View trends + Copy as text = 3 buttons.
        labels = {b.label for b in buttons}
        assert any("Teams: both" in lab for lab in labels)
        assert "🔍 View trends" in labels
        assert "📋 Copy as text" in labels

    def test_copy_button_disabled_before_first_query(self):
        from storm_trends import _TrendsView
        import discord as _discord
        view = _TrendsView(self._state())
        copy_btn = next(
            c for c in view.children
            if isinstance(c, _discord.ui.Button)
            and c.label and "Copy" in c.label
        )
        assert copy_btn.disabled is True

    def test_copy_button_enabled_after_query(self):
        from storm_trends import _TrendsView
        import discord as _discord
        state = self._state()
        state.last_query = {"results": [], "total_events_captured": 0,
                            "truncated": False}
        view = _TrendsView(state)
        copy_btn = next(
            c for c in view.children
            if isinstance(c, _discord.ui.Button)
            and c.label and "Copy" in c.label
        )
        assert copy_btn.disabled is False

    @pytest.mark.asyncio
    async def test_team_cycle_advances_filter(self):
        from storm_trends import _TrendsView
        view = _TrendsView(self._state())
        assert view.state.team_filter == "both"

        inter = MagicMock()
        inter.user.id = 42
        inter.response.edit_message = AsyncMock()
        await view._on_team_cycle(inter)
        assert view.state.team_filter == "A"
        await view._on_team_cycle(inter)
        assert view.state.team_filter == "B"
        await view._on_team_cycle(inter)
        assert view.state.team_filter == "both"

    @pytest.mark.asyncio
    async def test_owner_guard_blocks_others(self):
        from storm_trends import _TrendsView
        view = _TrendsView(self._state())
        inter = MagicMock()
        inter.user.id = 999  # not the owner (42)
        inter.response.send_message = AsyncMock()
        ok = await view._guard(inter)
        assert ok is False
        inter.response.send_message.assert_called_once()
        assert "Only the user who opened this view" in inter.response.send_message.call_args.args[0]


# ── Builder embed shape ────────────────────────────────────────────────────


class TestBuilderEmbed:

    def test_empty_state_explains_what_to_do(self):
        from storm_trends import _TrendsState, _render_builder_embed
        state = _TrendsState(
            guild_id=TEST_GUILD_ID, user_id=42,
            event_type="DS", questions=[],
        )
        embed = _render_builder_embed(state)
        body = embed.description or ""
        assert "Roster multi-select" in body
        assert "attendance" in body.lower()

    def test_renders_default_query(self):
        from storm_trends import _TrendsState, _render_builder_embed
        state = _TrendsState(
            guild_id=TEST_GUILD_ID, user_id=42,
            event_type="DS",
            questions=[{"key": "sat_out", "label": "Who sat out?",
                        "source": "participation"}],
        )
        embed = _render_builder_embed(state)
        body = embed.description or ""
        assert "Who sat out?" in body
        assert "greater than or equal to" in body
        assert "**3**" in body  # default threshold
        assert "8" in body      # default lookback
