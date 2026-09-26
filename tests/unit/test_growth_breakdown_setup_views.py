"""The Growth Breakdown wizard's views and modals, clicked.

`tests/integration/test_growth_breakdown_setup.py` answers each view by
attribute; before the move to `growth_breakdown_setup.py` (#611 round 2)
these classes were inline and could not be constructed alone. Each
control is held to the attribute it sets.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import growth_breakdown_setup as gb  # noqa: E402


async def _click(item, values=None):
    edited = {}

    async def _edit(inter, **kw):
        edited.update(kw)

    if values is not None:
        item._values = list(values)
    inter = MagicMock()
    inter.response = AsyncMock()
    inter.edit_original_response = AsyncMock()
    with patch("wizard_registry.safe_edit_response", side_effect=_edit):
        await item.callback(inter)
    return edited


def _select(view):
    return next(c for c in view.children if hasattr(c, "options"))


def _buttons(view):
    return {c.label: c for c in view.children if getattr(c, "label", None)}


def _submit(modal, values: dict):
    """Fill a modal's inputs by label and fire on_submit."""
    for item in modal.children:
        if item.label in values:
            item._value = values[item.label]
    inter = MagicMock()
    inter.response.defer = AsyncMock()
    return modal.on_submit(inter)


class TestBucketFilterView:
    def test_fresh_shape(self):
        """Nothing saved: Use default, then the picker as Define my own."""
        v = gb.BucketFilterView([])
        sel = _select(v)
        assert [o.value for o in sel.options] == ["increased", "steady", "low", "none", "decline"]
        assert (sel.min_values, sel.max_values, sel.row) == (1, 5, 1)
        assert [(b.label, b.style.name, b.row) for b in _buttons(v).values()] == [
            ("✅ Use default: All but No Change", "success", 0),
        ]

    def test_saved_filter_shape(self):
        """A saved filter: Keep current first, Use default beside it, the
        way `ask_keep_or_change` lays out a saved value."""
        v = gb.BucketFilterView(["low", "decline"])
        assert [(b.label, b.style.name, b.row) for b in _buttons(v).values()] == [
            ("Keep current: Low, Decline", "success", 0),
            ("↩️ Use default: All but No Change", "secondary", 0),
        ]
        assert _select(v).row == 1

    @pytest.mark.asyncio
    async def test_controls(self):
        v = gb.BucketFilterView(["low"])
        await _click(_buttons(v)["Keep current: Low"])
        assert v.selected == ["low"] and v.is_finished()
        v = gb.BucketFilterView(["low"])
        await _click(_buttons(v)["↩️ Use default: All but No Change"])
        assert v.selected == [] and all(c.disabled for c in v.children)
        v = gb.BucketFilterView([])
        await _click(_select(v), ["none", "decline"])
        assert v.selected == ["none", "decline"]
        v = gb.BucketFilterView([])
        await _click(_buttons(v)["✅ Use default: All but No Change"])
        assert v.selected == [] and all(c.disabled for c in v.children)


class TestThresholdsModal:
    def test_defaults_prefill(self):
        m = gb.ThresholdsModal({})
        assert m.title == "Custom Thresholds (%)"
        assert [(c.label, c.default) for c in m.children] == [
            ("Increased ≥", "20.0"),
            ("Steady ≥", "10.0"),
            ("Low ≥", "5.0"),
            ("None ≥", "0.0"),
        ]
        m = gb.ThresholdsModal({"increased": 25, "steady": 12.5})
        assert [c.default for c in m.children][:2] == ["25", "12.5"]

    @pytest.mark.asyncio
    async def test_submit_parses_floats_or_none(self):
        m = gb.ThresholdsModal({})
        await _submit(m, {"Increased ≥": "30", "Steady ≥": "15", "Low ≥": "2.5", "None ≥": "0"})
        assert m.values_out == {"increased": 30.0, "steady": 15.0, "low": 2.5, "none": 0.0}
        m = gb.ThresholdsModal({})
        await _submit(m, {"Increased ≥": "lots", "Steady ≥": "15", "Low ≥": "2", "None ≥": "0"})
        assert m.values_out is None


class TestLabelsModal:
    def test_prefill(self):
        m = gb.LabelsModal({"none": "Stalled"})
        assert m.title == "Custom Bucket Labels"
        assert [(c.label, c.default, c.placeholder) for c in m.children] == [
            ("Increased", "Increased", "e.g. 'Increased'"),
            ("Steady", "Steady", "e.g. 'Steady'"),
            ("Low", "Low", "e.g. 'Low'"),
            ("No Change", "Stalled", "e.g. 'No Change'"),
            ("Decline", "Decline", "e.g. 'Decline'"),
        ]

    @pytest.mark.asyncio
    async def test_submit_strips(self):
        m = gb.LabelsModal({})
        await _submit(m, {"Increased": "  Up  ", "Decline": ""})
        assert m.values_out["increased"] == "Up" and m.values_out["decline"] == ""
        assert m.values_out["steady"] == "Steady"


class TestChoiceViews:
    @pytest.mark.parametrize(
        "cls, keep",
        [
            (gb.ThresholdsChoiceView, "Keep current values"),
            (gb.LabelsChoiceView, "Keep current labels"),
        ],
    )
    def test_shape_follows_saved_values(self, cls, keep):
        fresh = cls({})
        assert list(_buttons(fresh)) == ["✅ Use defaults", "✏️ Customize"]
        saved = cls({"increased": 1})
        assert list(_buttons(saved)) == [keep, "↩️ Use defaults", "✏️ Customize"]
        assert _buttons(saved)["↩️ Use defaults"].style.name == "secondary"
        assert fresh._modal_values is None

    @pytest.mark.parametrize("cls", [gb.ThresholdsChoiceView, gb.LabelsChoiceView])
    @pytest.mark.asyncio
    async def test_keep_and_defaults(self, cls):
        v = cls({"increased": 1})
        await _click(list(_buttons(v).values())[0])
        assert v.choice == "keep" and v.is_finished()
        v = cls({})
        await _click(_buttons(v)["✅ Use defaults"])
        assert v.choice == "defaults" and all(c.disabled for c in v.children)

    @pytest.mark.asyncio
    async def test_customize_opens_the_modal_and_reads_it(self):
        v = gb.ThresholdsChoiceView({"increased": 25})
        modal = MagicMock(values_out={"increased": 30.0}, wait=AsyncMock())
        inter = MagicMock()
        inter.response.send_modal = AsyncMock()
        inter.edit_original_response = AsyncMock()
        with patch.object(v, "_modal", return_value=modal):
            await _buttons(v)["✏️ Customize"].callback(inter)
        inter.response.send_modal.assert_awaited_once_with(modal)
        assert v.choice == "customize" and v._modal_values == {"increased": 30.0}
        assert all(c.disabled for c in v.children) and v.is_finished()

    @pytest.mark.asyncio
    async def test_a_dismissed_or_invalid_modal_leaves_no_choice(self):
        v = gb.LabelsChoiceView({})
        modal = MagicMock(values_out=None, wait=AsyncMock())
        inter = MagicMock()
        inter.response.send_modal = AsyncMock()
        inter.edit_original_response = AsyncMock(side_effect=RuntimeError("gone"))
        with patch.object(v, "_modal", return_value=modal):
            await _buttons(v)["✏️ Customize"].callback(inter)
        assert v.choice is None and v._modal_values is None and v.is_finished()

    def test_each_view_builds_its_own_modal(self):
        assert isinstance(gb.ThresholdsChoiceView({})._modal(), gb.ThresholdsModal)
        assert isinstance(gb.LabelsChoiceView({})._modal(), gb.LabelsModal)
