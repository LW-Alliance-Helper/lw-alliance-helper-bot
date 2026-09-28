"""The Shiny Tasks wizard's warzone-group picker (#604).

Step 3 was a two-field modal for the lowest and highest server number
until one alliance typed 1-2308 and its post outgrew what Discord will
send. It is a pick from the game's warzone groups now: a select plus a
confirm button, with Keep current when the saved range is one of the
groups. `tests/integration/test_shiny_tasks_setup.py` answers it by
attribute; these click it.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import shiny_tasks  # noqa: E402
import shiny_tasks_setup as ss  # noqa: E402


def _inter():
    inter = MagicMock()
    inter.response = AsyncMock()
    inter.response.is_done = MagicMock(return_value=False)
    return inter


def _buttons(view):
    return [c for c in view.children if hasattr(c, "label") and not hasattr(c, "options")]


class TestShape:
    def test_every_group_is_an_option(self):
        v = ss.WarzoneGroupView(42)
        select = v._select
        assert select.placeholder == "Pick your warzone group..."
        assert len(select.options) == len(shiny_tasks.WARZONE_GROUPS) == 18
        labels = [o.label for o in select.options]
        assert labels[0] == "Warzones 1 – 164"
        assert labels[5] == "Warzones 677 – 804"
        assert labels[-1] == "Warzones 2213 – 2308"
        # The size line was left out on sign-off (2026-09-27): not needed.
        assert all(o.description is None for o in select.options)

    def test_fresh_has_no_keep_button_and_confirm_starts_disabled(self):
        v = ss.WarzoneGroupView(42)
        assert [b.label for b in _buttons(v)] == ["✅ Use this group"]
        assert _buttons(v)[0].disabled is True

    def test_a_saved_group_gets_keep_current(self):
        v = ss.WarzoneGroupView(42, current=(677, 804))
        assert [b.label for b in _buttons(v)] == ["Keep current: 677 – 804", "✅ Use this group"]

    def test_only_the_person_who_ran_the_wizard_can_use_it(self):
        assert ss.WarzoneGroupView(42).owner_id == 42


class TestClicks:
    @pytest.mark.asyncio
    async def test_pick_then_confirm(self):
        v = ss.WarzoneGroupView(42)
        v._select._values = ["805-932"]
        await v._select.callback(_inter())
        confirm = _buttons(v)[0]
        assert confirm.disabled is False and not v.is_finished()
        inter = _inter()
        await confirm.callback(inter)
        assert v.selected == (805, 932) and v.confirmed is True and v.is_finished()
        assert all(c.disabled for c in v.children)
        assert inter.response.edit_message.await_args.kwargs["content"] == (
            "✅ Warzone group: **805 – 932**"
        )

    @pytest.mark.asyncio
    async def test_keep_current(self):
        v = ss.WarzoneGroupView(42, current=(677, 804))
        inter = _inter()
        await _buttons(v)[0].callback(inter)
        assert v.selected == (677, 804) and v.confirmed is True and v.is_finished()
        assert inter.response.edit_message.await_args.kwargs["content"] == (
            "✅ Keeping warzones: **677 – 804**"
        )


class TestSavedRange:
    @pytest.mark.parametrize(
        "lo, hi, expected",
        [
            (677, 804, (677, 804)),
            ("677", "804", (677, 804)),
            (700, 760, None),  # a narrower slice from before the groups
            (1, 2308, None),
            (0, 0, None),
            ("", "", None),
        ],
    )
    def test_group_for_range(self, lo, hi, expected):
        assert shiny_tasks.group_for_range(lo, hi) == expected
