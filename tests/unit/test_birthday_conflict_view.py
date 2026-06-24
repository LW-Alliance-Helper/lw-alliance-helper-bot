"""
Tests for `train_cog.BirthdayConflictView` — the interactive birthday→train
conflict alert that replaced the old fire-and-forget text post.

The view lets leadership resolve a scheduling conflict in place:
  * 📅 a dropdown of open days places the member and writes the schedule,
  * 📋 Show next 7 days renders an ephemeral read-only window,
  * 🙈 Ignore persists a dismissal so the daily re-post stops.

These exercise the callbacks directly (not through Discord) with fake
interaction / message objects, patching the Sheets and SQLite boundaries.
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

GUILD_ID = 12345


def _conflict():
    return {
        "name": "Swaggy",
        "discord_id": None,
        "bday_iso": "2026-07-03",
        "bday_fmt": "Friday, July 3",
        "taken": ["Jul 3 (Sylvia)", "Jul 2 (Walrus)", "Jul 4 (DSP)"],
        "open_dates": [
            "2026-07-05",
            "2026-07-06",
            "2026-07-07",
            "2026-07-08",
            "2026-07-09",
            "2026-07-10",
        ],
        "key": "name:swaggy|2026-07-03",
    }


def _make_view(conflicts=None):
    import train_cog

    return train_cog.BirthdayConflictView(MagicMock(), GUILD_ID, conflicts or [_conflict()])


def _fake_interaction(role: str = "Leadership"):
    inter = MagicMock()
    role_obj = MagicMock()
    role_obj.name = role
    inter.user.roles = [role_obj]
    inter.response.defer = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


def _leadership_cfg():
    cfg = MagicMock()
    cfg.leadership_role_name = "Leadership"
    return cfg


class TestViewConstruction:
    def test_place_dropdown_offers_open_days(self):
        view = _make_view()
        # Select + Show + Ignore.
        selects = [c for c in view.children if hasattr(c, "options")]
        assert len(selects) == 1
        opts = selects[0].options
        assert len(opts) == 6
        # Encodes "<conflict index>|<iso>" and shows the member name.
        assert opts[0].value == "0|2026-07-05"
        assert "Swaggy" in opts[0].label

    def test_no_open_days_drops_the_dropdown(self):
        c = _conflict()
        c["open_dates"] = []
        view = _make_view([c])
        assert not [child for child in view.children if hasattr(child, "options")]
        # Show + Ignore buttons still present.
        assert len([child for child in view.children if hasattr(child, "label")]) == 2


class TestPlace:
    @pytest.mark.asyncio
    async def test_place_writes_schedule_and_marks_resolved(self):
        view = _make_view()
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        view._select = SimpleNamespace(values=["0|2026-07-05"])

        saved = {}

        def _save(schedule, guild_id):
            saved.update(schedule)

        inter = _fake_interaction()
        with (
            patch("train_cog.get_config", return_value=_leadership_cfg()),
            patch("train_cog.load_schedule", return_value={}),
            patch("train_cog.save_schedule", side_effect=_save),
        ):
            await view._on_place(inter)

        # Wrote the member onto the chosen open day.
        assert saved["2026-07-05"]["name"] == "Swaggy"
        assert saved["2026-07-05"]["theme"] == "Birthday"
        # Last conflict resolved → alert edited to the resolved summary, no view.
        assert view.conflicts == []
        edit_kwargs = view.message.edit.await_args.kwargs
        assert edit_kwargs["view"] is None
        assert "resolved" in edit_kwargs["content"].lower()

    @pytest.mark.asyncio
    async def test_place_rejects_a_slot_taken_since_posting(self):
        view = _make_view()
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        view._select = SimpleNamespace(values=["0|2026-07-05"])

        inter = _fake_interaction()
        with (
            patch("train_cog.get_config", return_value=_leadership_cfg()),
            patch("train_cog.load_schedule", return_value={"2026-07-05": {"name": "Latecomer"}}),
            patch("train_cog.save_schedule") as mock_save,
        ):
            await view._on_place(inter)

        mock_save.assert_not_called()
        # Conflict stays open; user told to pick another day.
        assert len(view.conflicts) == 1
        warn = inter.followup.send.await_args.args[0]
        assert "Latecomer" in warn

    @pytest.mark.asyncio
    async def test_place_blocked_for_non_leadership(self):
        view = _make_view()
        view._select = SimpleNamespace(values=["0|2026-07-05"])

        inter = _fake_interaction(role="Member")
        with (
            patch("train_cog.get_config", return_value=_leadership_cfg()),
            patch("train_cog.save_schedule") as mock_save,
        ):
            await view._on_place(inter)

        mock_save.assert_not_called()
        inter.response.send_message.assert_awaited()
        assert "Leadership" in inter.response.send_message.await_args.args[0]


class TestIgnore:
    @pytest.mark.asyncio
    async def test_ignore_persists_keys_and_clears_alert(self):
        view = _make_view()
        view.message = MagicMock()
        view.message.edit = AsyncMock()

        inter = _fake_interaction()
        with (
            patch("train_cog.get_config", return_value=_leadership_cfg()),
            patch("config.mark_conflict_ignored") as mock_mark,
        ):
            await view._on_ignore(inter)

        mock_mark.assert_called_once_with(GUILD_ID, "name:swaggy|2026-07-03")
        edit_kwargs = view.message.edit.await_args.kwargs
        assert edit_kwargs["view"] is None
        assert "dismissed" in edit_kwargs["content"].lower()
