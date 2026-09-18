"""The Shiny Tasks wizard's server-range modal and its launcher.

`tests/integration/test_shiny_tasks_setup.py` fills the modal by
attribute; before the move to `shiny_tasks_setup.py` (#611 round 2) the
class was inline and could not be constructed alone.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import shiny_tasks_setup as ss  # noqa: E402


class TestServerRangeModal:
    def test_shape_and_prefill(self):
        m = ss.ServerRangeModal()
        assert m.title == "Server Range"
        assert [
            (c.label, c.placeholder, c.default, c.max_length, c.required) for c in m.children
        ] == [
            ("Lowest reachable server number", "e.g. 677", "", 5, True),
            ("Highest reachable server number", "e.g. 804", "", 5, True),
        ]
        assert (m.min_value, m.max_value, m.value) == (None, None, "")
        m = ss.ServerRangeModal("677", "804")
        assert [c.default for c in m.children] == ["677", "804"]

    def test_value_renders_what_is_known(self):
        m = ss.ServerRangeModal()
        m.min_value = "681"
        assert m.value == "681 – ?"
        m.max_value = "799"
        assert m.value == "681 – 799"

    @pytest.mark.asyncio
    async def test_submit_strips_both_fields(self):
        m = ss.ServerRangeModal()
        m.children[0]._value = " 677 "
        m.children[1]._value = "804\n"
        inter = MagicMock()
        inter.response.defer = AsyncMock()
        await m.on_submit(inter)
        assert (m.min_value, m.max_value, m.value) == ("677", "804", "677 – 804")
        assert m.is_finished()


class TestRangeLauncher:
    def test_fresh_has_only_the_renamed_enter_button(self):
        v = ss._range_launcher(0, 0)
        assert [c.label for c in v.children] == ["✏️ Enter Server Numbers"]
        assert [c.default for c in v.modal.children] == ["", ""]

    def test_saved_range_adds_keep_current(self):
        v = ss._range_launcher(677, 804)
        assert [c.label for c in v.children] == [
            "✏️ Enter Server Numbers",
            "Keep current: 677 – 804",
        ]
        assert [c.default for c in v.modal.children] == ["677", "804"]

    @pytest.mark.parametrize("lo, hi", [(900, 800), (0, 5), ("abc", "804"), ("", "")])
    def test_an_unusable_saved_range_gets_no_keep_button(self, lo, hi):
        v = ss._range_launcher(lo, hi)
        assert [c.label for c in v.children] == ["✏️ Enter Server Numbers"]

    @pytest.mark.asyncio
    async def test_keep_current_fills_the_modal(self):
        v = ss._range_launcher(677, 804)
        keep = next(c for c in v.children if c.label.startswith("Keep current"))
        inter = MagicMock()
        inter.response = AsyncMock()
        await keep.callback(inter)
        assert (v.modal.min_value, v.modal.max_value) == ("677", "804")
        assert v.confirmed is True and v.is_finished()
