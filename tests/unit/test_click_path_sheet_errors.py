"""Click paths tell the officer about the alliance's own Sheet problems (#677).

A deleted, unshared or renamed Sheet is the alliance's to fix. On these click
paths it used to reach Sentry (through discord.py's view error handler or a
raw capture) while the officer saw nothing. Each case checks both halves:
an alliance-owned error is explained and kept out of Sentry, and anything
else still raises, so a real bug still pages.
"""

from __future__ import annotations

import os
import sys
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import gspread
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

GUILD_ID = 1234


def _interaction():
    inter = MagicMock()
    inter.guild_id = GUILD_ID
    inter.user.id = 42
    inter.response.is_done = MagicMock(return_value=False)
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock(
        side_effect=lambda **_: inter.response.is_done.configure_mock(return_value=True)
    )
    inter.followup.send = AsyncMock(return_value=MagicMock())
    return inter


def _raises(exc):
    def _fn(*_a, **_k):
        raise exc

    return _fn


def _followup_text(inter) -> str:
    call = inter.followup.send.await_args
    return call.args[0] if call.args else call.kwargs.get("content", "")


# ── Train rotation draft: the ◀ / ▶ week arrows ──────────────────────────────


class TestDraftWeekArrows:
    def _view(self):
        import train_rotation_ui_draft as draft_ui

        return draft_ui.WeeklyDraftView(MagicMock(), GUILD_ID, [], date(2026, 9, 21), "Standard")

    async def test_unshared_sheet_is_explained_and_the_week_stays_put(self):
        import train_rotation_ui as ui

        view = self._view()
        inter = _interaction()
        with (
            patch.object(ui, "_is_leader", return_value=True),
            patch.object(ui, "load_week_draft", new=_raises(PermissionError())),
            patch("sentry_sdk.capture_exception") as capture,
        ):
            await view._shift_week(inter, 7)
        assert "Couldn't load this week's draft" in _followup_text(inter)
        assert inter.followup.send.await_args.kwargs.get("ephemeral") is True
        assert view.week_start == date(2026, 9, 21)
        capture.assert_not_called()

    async def test_a_bug_still_raises(self):
        import train_rotation_ui as ui

        view = self._view()
        with (
            patch.object(ui, "_is_leader", return_value=True),
            patch.object(ui, "load_week_draft", new=_raises(ValueError("bug"))),
            pytest.raises(ValueError),
        ):
            await view._shift_week(_interaction(), 7)


# ── Train presets: Edit → pick a preset ──────────────────────────────────────


class TestTrainPresetEditPicker:
    async def _pick(self, load_preset):
        import train_hub

        view = train_hub.PresetsManageView(MagicMock(), GUILD_ID, 42, "Day Rules")
        opener = _interaction()
        with patch.object(train_hub.tr, "list_presets", return_value=["Standard Week"]):
            await view._edit(opener)
        picker = opener.followup.send.await_args.kwargs["view"]
        inter = _interaction()
        with (
            patch.object(train_hub.tr, "load_preset", new=load_preset),
            patch.object(train_hub.ui, "post_preset_editor", new=AsyncMock()) as post,
        ):
            await picker._on_pick(inter, "Standard Week")
        return inter, post

    async def test_missing_tab_is_explained(self):
        inter, post = await self._pick(_raises(gspread.exceptions.WorksheetNotFound("Day Rules")))
        assert "Couldn't load your saved pattern" in _followup_text(inter)
        post.assert_not_awaited()

    async def test_a_bug_still_raises(self):
        with pytest.raises(ValueError):
            await self._pick(_raises(ValueError("bug")))


# ── Transfer notices: the status write-back ──────────────────────────────────


class TestTransferWriteBack:
    def _view(self):
        import transfer_cog

        return transfer_cog._WriteConfirmView(
            name="Player One",
            decision={"column": "Status", "kind": "yesno"},
            writeback={"sheet_id": "sid", "tab": "Transfers", "column_map": {}, "hash": "h"},
        )

    async def _write(self, exc):
        import transfer_cog

        view = self._view()
        with (
            patch.object(
                transfer_cog.transfer_sheets, "read_sheet", return_value=(["Name", "Status"], [])
            ),
            patch.object(transfer_cog.transfer, "find_row_index", return_value=0),
            patch.object(transfer_cog.transfer_sheets, "write_cell", new=_raises(exc)),
            patch.object(transfer_cog, "_capture") as capture,
        ):
            await view._write(_interaction(), "TRUE", "Yes")
        return capture

    async def test_revoked_edit_access_is_not_captured(self):
        capture = await self._write(PermissionError())
        capture.assert_not_called()

    async def test_a_bug_is_still_captured(self):
        capture = await self._write(ValueError("bug"))
        capture.assert_called_once()


# ── Storm member rules: opening the rules tab ────────────────────────────────


class TestStormRulesWorksheet:
    async def test_unshared_sheet_reads_as_no_sheet(self):
        import storm_member_rules as smr

        with (
            patch.object(smr, "_rules_tab_name", return_value="DS Member Rules"),
            patch("config.get_spreadsheet", new=_raises(PermissionError())),
        ):
            assert smr._get_or_create_rules_worksheet(GUILD_ID, "DS") is None
            assert smr.list_rules(GUILD_ID, "DS") == []

    async def test_a_bug_still_raises(self):
        import storm_member_rules as smr

        with (
            patch.object(smr, "_rules_tab_name", return_value="DS Member Rules"),
            patch("config.get_spreadsheet", new=_raises(ValueError("bug"))),
            pytest.raises(ValueError),
        ):
            smr._get_or_create_rules_worksheet(GUILD_ID, "DS")


# ── Storm participation log: the derived-count question ──────────────────────


class TestDerivedCountQuestion:
    def _walk(self):
        return SimpleNamespace(
            guild_id=GUILD_ID,
            event_type="DS",
            ensure_roster=AsyncMock(),
            channel=SimpleNamespace(send=AsyncMock()),
            record_per_member=MagicMock(),
        )

    def _question(self):
        return SimpleNamespace(
            key="missed",
            label="Missed events",
            header="**Missed events**",
            raw={"source_question_key": "attended", "lookback_events": 4},
        )

    async def test_deleted_sheet_skips_the_question_with_a_note(self):
        import storm_log
        import storm_log_flow

        w = self._walk()
        with patch.object(
            storm_log,
            "count_member_flags_in_window",
            new=_raises(gspread.exceptions.SpreadsheetNotFound()),
        ):
            result = await storm_log_flow._q_derived_count(w, self._question())
        assert result is None
        note = w.channel.send.await_args.args[0]
        assert "Couldn't read past events for `Missed events`" in note
        w.record_per_member.assert_not_called()

    async def test_a_bug_still_raises(self):
        import storm_log
        import storm_log_flow

        with (
            patch.object(storm_log, "count_member_flags_in_window", new=_raises(ValueError("x"))),
            pytest.raises(ValueError),
        ):
            await storm_log_flow._q_derived_count(self._walk(), self._question())
