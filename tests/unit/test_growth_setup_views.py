"""The growth wizard's views and modal, clicked.

`tests/integration/test_growth_setup.py` answers each view by attribute;
before the move to `growth_setup.py` (#611 round 2) these classes were
inline and could not be constructed alone. Each control is held to the
attribute it sets.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import growth_setup as gs  # noqa: E402


async def _click(item, values=None):
    edited = {}

    async def _edit(inter, **kw):
        edited.update(kw)

    if values is not None:
        item._values = list(values)
    inter = MagicMock()
    inter.response = AsyncMock()
    with patch("wizard_registry.safe_edit_response", side_effect=_edit):
        await item.callback(inter)
    return edited, inter


def _buttons(view):
    return {c.label: c for c in view.children if getattr(c, "label", None)}


async def _submit(modal, label, col):
    modal.children[0]._value = label
    modal.children[1]._value = col
    inter = MagicMock()
    inter.response.defer = AsyncMock()
    await modal.on_submit(inter)


class TestMetricModal:
    def test_shape_and_prefill(self):
        m = gs.MetricModal()
        assert m.title == "Metric"
        assert [(c.label, c.placeholder, c.default, c.max_length) for c in m.children] == [
            ("Label", "e.g. 1st Squad Power, THP, Total Kills", "", 100),
            ("Column letter", "e.g. E", "", 2),
        ]
        assert not m.is_usable
        m = gs.MetricModal("THP", "E")
        assert [c.default for c in m.children] == ["THP", "E"]

    @pytest.mark.asyncio
    async def test_submit_strips_and_uppercases(self):
        m = gs.MetricModal()
        await _submit(m, "  THP ", " e ")
        assert (m.label_value, m.col_value, m.is_usable) == ("THP", "E", True)

    @pytest.mark.parametrize("label, col", [("", "E"), ("THP", ""), ("THP", "12"), ("THP", "E1")])
    @pytest.mark.asyncio
    async def test_unusable_submissions(self, label, col):
        m = gs.MetricModal()
        await _submit(m, label, col)
        assert not m.is_usable


class TestMetricsActionView:
    def test_disabled_controls(self):
        v = gs.MetricsActionView([], at_cap=False)
        assert [(c.label, c.disabled, c.row) for c in v.children] == [
            ("➕ Add Metric", False, 0),
            ("✏️ Edit Metric", True, 0),
            ("🗑️ Delete Metric", True, 0),
            ("✅ Done", True, 1),
        ]
        v = gs.MetricsActionView([{"label": "THP", "col": "E"}], at_cap=True)
        assert [(c.label, c.disabled) for c in v.children] == [
            ("➕ Add Metric", True),
            ("✏️ Edit Metric", False),
            ("🗑️ Delete Metric", False),
            ("✅ Done", False),
        ]

    @pytest.mark.parametrize(
        "label, choice",
        [("✏️ Edit Metric", "edit"), ("🗑️ Delete Metric", "delete"), ("✅ Done", "done")],
    )
    @pytest.mark.asyncio
    async def test_buttons_defer(self, label, choice):
        v = gs.MetricsActionView([{"label": "THP", "col": "E"}], at_cap=False)
        _, inter = await _click(_buttons(v)[label])
        assert v.choice == choice and v.is_finished()
        inter.response.defer.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_add_opens_the_modal_and_appends_a_usable_metric(self):
        metrics = []
        v = gs.MetricsActionView(metrics, at_cap=False)
        modal = MagicMock(is_usable=True, label_value="THP", col_value="E", wait=AsyncMock())
        inter = MagicMock()
        inter.response.send_modal = AsyncMock()
        with patch("growth_setup.MetricModal", return_value=modal):
            await _buttons(v)["➕ Add Metric"].callback(inter)
        inter.response.send_modal.assert_awaited_once_with(modal)
        assert metrics == [{"label": "THP", "col": "E"}] and v.choice == "loop"

    @pytest.mark.asyncio
    async def test_add_with_a_dismissed_modal_appends_nothing(self):
        metrics = []
        v = gs.MetricsActionView(metrics, at_cap=False)
        modal = MagicMock(is_usable=False, wait=AsyncMock())
        inter = MagicMock()
        inter.response.send_modal = AsyncMock()
        with patch("growth_setup.MetricModal", return_value=modal):
            await _buttons(v)["➕ Add Metric"].callback(inter)
        assert metrics == [] and v.choice == "loop" and v.is_finished()


class TestPickMetricView:
    @pytest.mark.asyncio
    async def test_options_and_pick(self):
        v = gs.PickMetricView([{"label": "THP", "col": "E"}, {"label": "x" * 120, "col": "F"}])
        assert v.select.placeholder == "Choose a metric..."
        assert [(o.label, o.value, o.description) for o in v.select.options] == [
            ("THP", "0", "Column E"),
            ("x" * 100, "1", "Column F"),
        ]
        assert (v.select.min_values, v.select.max_values) == (1, 1)
        edited, _ = await _click(v.select, ["1"])
        assert v.index == 1 and edited["view"] is v and v.select.disabled and v.is_finished()


class TestEditLaunchView:
    @pytest.mark.asyncio
    async def test_opens_the_prefilled_modal(self):
        v = gs.EditLaunchView({"label": "THP", "col": "E"})
        assert [c.label for c in v.children] == ["✏️ Edit values"]
        assert [c.default for c in v.modal.children] == ["THP", "E"]
        assert v.confirmed is False
        inter = MagicMock()
        inter.response.send_modal = AsyncMock()
        with patch.object(v.modal, "wait", AsyncMock()):
            await _buttons(v)["✏️ Edit values"].callback(inter)
        inter.response.send_modal.assert_awaited_once_with(v.modal)
        assert v.confirmed is True and v.is_finished()


class TestFrequencyView:
    def test_custom_is_locked_on_the_free_tier(self):
        v = gs.FrequencyView(custom_unlocked=False)
        assert [(c.label, c.disabled) for c in v.children] == [
            ("📅 Monthly (1st of each month)", False),
            ("🔁 Custom interval (every X days) 💎", True),
        ]
        assert not gs.FrequencyView(custom_unlocked=True).children[1].disabled

    @pytest.mark.asyncio
    async def test_buttons(self):
        v = gs.FrequencyView(custom_unlocked=True)
        edited, _ = await _click(_buttons(v)["📅 Monthly (1st of each month)"])
        assert v.selected == "monthly" and edited == {
            "content": "✅ Frequency: **Monthly**",
            "view": v,
        }
        v = gs.FrequencyView(custom_unlocked=True)
        edited, _ = await _click(_buttons(v)["🔁 Custom interval (every X days) 💎"])
        assert v.selected == "interval" and edited == {"view": v}
        assert all(c.disabled for c in v.children) and v.is_finished()
