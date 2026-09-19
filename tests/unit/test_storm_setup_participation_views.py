"""The participation step's six views, clicked.

`tests/integration/test_storm_setup_participation.py` answers each view by
attribute, so it never runs a callback. Before the move to
`storm_setup_participation.py` (#611 round 2) the views were defined inline
and could not be constructed alone. Each click is held to the exact
acknowledgement the inline class rendered and to the attribute it set.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import storm_setup_participation as sp  # noqa: E402


async def _click(item, values=None):
    """Run `item`'s callback (setting a select's values first) and return
    what it edited through `safe_edit_response`."""
    edited = {}

    async def _edit(inter, **kw):
        edited.update(kw)

    if values is not None:
        item._values = list(values)
    inter = MagicMock()
    inter.response = AsyncMock()
    with patch("wizard_registry.safe_edit_response", side_effect=_edit):
        await item.callback(inter)
    return edited


def _select(view):
    return next(c for c in view.children if hasattr(c, "options"))


def _button(view, label):
    return next(c for c in view.children if getattr(c, "label", None) == label)


PRESETS = [
    {"key": "a", "label": "Alpha", "emoji": "✅", "description": "first", "default_checked": True},
    {"key": "b", "label": "Beta", "emoji": "📊", "description": "second"},
]


class TestPresetPickerView:
    def _view(self, is_premium):
        return sp._PresetPickerView(
            PRESETS,
            default_checked={"a"},
            premium_only=lambda p: p["key"] == "b",
            is_premium=is_premium,
        )

    def test_free_tier_marks_the_premium_preset(self):
        sel = _select(self._view(False))
        assert [o.label for o in sel.options] == ["✅ Alpha", "💎 📊 Beta"]
        assert [o.description for o in sel.options] == ["first", "💎 Premium · second"]
        assert [o.default for o in sel.options] == [True, False]
        assert sel.max_values == 2 and sel.min_values == 0
        assert sel.placeholder == "Pick the preset questions you want…"

    def test_premium_shows_it_plain(self):
        assert [o.label for o in _select(self._view(True)).options] == ["✅ Alpha", "📊 Beta"]

    @pytest.mark.asyncio
    async def test_selecting_updates_the_set_and_defers(self):
        v = self._view(True)
        sel = _select(v)
        sel._values = ["b"]
        inter = MagicMock()
        inter.response = AsyncMock()
        await sel.callback(inter)
        assert v.selected == {"b"}
        inter.response.defer.assert_awaited_once()
        assert not v.is_finished()

    @pytest.mark.parametrize(
        "label, action", [("✅ Add picked presets", "add"), ("↩️ Skip presets", "skip")]
    )
    @pytest.mark.asyncio
    async def test_add_and_skip(self, label, action):
        v = self._view(True)
        edited = await _click(_button(v, label))
        assert v.action == action and v.selected == {"a"}
        assert edited == {"view": v}
        assert all(c.disabled for c in v.children) and v.is_finished()


class TestBuilderView:
    QS = [{"key": "x", "label": "First"}, {"key": "y", "label": "Second"}]

    def test_no_questions_means_buttons_only(self):
        v = sp._BuilderView([])
        assert [getattr(c, "label", None) for c in v.children] == ["➕ Add question", "✅ Done"]

    def test_questions_add_edit_and_remove_selects(self):
        v = sp._BuilderView(self.QS)
        selects = [c for c in v.children if hasattr(c, "options")]
        assert [s.placeholder for s in selects] == ["✏️ Edit a question…", "🗑️ Remove a question…"]
        assert [o.label for o in selects[0].options] == ["Edit: First", "Edit: Second"]
        assert [o.label for o in selects[1].options] == ["Remove: First", "Remove: Second"]

    @pytest.mark.asyncio
    async def test_edit_and_delete_record_the_index(self):
        v = sp._BuilderView(self.QS)
        selects = [c for c in v.children if hasattr(c, "options")]
        await _click(selects[0], ["1"])
        assert v.action == "edit" and v.edit_idx == 1 and v.is_finished()
        v = sp._BuilderView(self.QS)
        selects = [c for c in v.children if hasattr(c, "options")]
        await _click(selects[1], ["0"])
        assert v.action == "delete" and v.del_idx == 0

    @pytest.mark.parametrize("label, action", [("➕ Add question", "add"), ("✅ Done", "done")])
    @pytest.mark.asyncio
    async def test_add_and_done(self, label, action):
        v = sp._BuilderView([])
        await _click(_button(v, label))
        assert v.action == action and all(c.disabled for c in v.children)


class TestTypeView:
    @pytest.mark.asyncio
    async def test_picking_a_type_names_it(self):
        import discord

        v = sp._TypeView(
            [
                discord.SelectOption(
                    label="🔢 Numeric: number with optional min/max", value="numeric"
                )
            ]
        )
        edited = await _click(_select(v), ["numeric"])
        assert v.selected == "numeric"
        assert edited["content"] == "✅ Type: **🔢 Numeric: number with optional min/max**"
        assert _select(v).disabled and v.is_finished()


class TestPrefillView:
    @pytest.mark.asyncio
    async def test_choices(self):
        v = sp._PrefillView()
        assert [c.label for c in v.children] == [
            "🗳️ Pre-fill from Discord poll signups",
            "✏️ Manual selection only",
        ]
        edited = await _click(v.children[0])
        assert v.selected == "discord_poll"
        assert edited["content"] == "✅ Pre-fill source: **Discord poll signups**"
        v = sp._PrefillView()
        edited = await _click(v.children[1])
        assert v.selected == ""
        assert edited["content"] == "✅ Pre-fill source: **Manual selection only**"


class TestSourceView:
    @pytest.mark.asyncio
    async def test_options_and_pick(self):
        v = sp._SourceView([{"key": "sat_out", "label": "Who sat out?"}, {"key": "k2"}])
        sel = _select(v)
        assert [(o.label, o.value) for o in sel.options] == [
            ("Who sat out?", "sat_out"),
            ("k2", "k2"),
        ]
        assert sel.placeholder == "Pick the source roster multi-select question…"
        edited = await _click(sel, ["sat_out"])
        assert v.selected == "sat_out"
        assert edited["content"] == "✅ Source question: `sat_out`"


class TestShowDuringLogView:
    @pytest.mark.asyncio
    async def test_choices(self):
        v = sp._ShowDuringLogView()
        assert [c.label for c in v.children] == [
            "📋 Show counts per member during the log",
            "🔍 Only show in the Trends Viewer",
        ]
        edited = await _click(v.children[0])
        assert v.selected is True and edited["content"] == "✅ Show during the log: **Yes**"
        v = sp._ShowDuringLogView()
        edited = await _click(v.children[1])
        assert v.selected is False and edited["content"] == "✅ Show during the log: **No**"
