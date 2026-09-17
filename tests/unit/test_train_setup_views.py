"""The train wizard's views, clicked.

`tests/integration/test_train_setup.py` answers each view by attribute;
before the move to `train_setup.py` / `train_setup_rotation.py` (#611
round 2) most of these classes were inline and could not be constructed
alone. Each control is held to the attribute it sets and the
acknowledgement it renders.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import train_setup as ts  # noqa: E402
import train_setup_rotation as tr_setup  # noqa: E402


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


def _labels(view):
    return [getattr(c, "label", None) for c in view.children]


class TestToneDefaultView:
    @pytest.mark.asyncio
    async def test_fresh_and_keep_current(self):
        v = ts.ToneDefaultView(["Funny", "Serious"])
        assert _labels(v) == [None]
        assert [o.label for o in _select(v).options] == ["Funny", "Serious"]
        edited = await _click(_select(v), ["Serious"])
        assert v.selected == "Serious" and edited["content"] == "✅ Default tone: **Serious**"
        v = ts.ToneDefaultView(["Funny", "Serious"], current="Funny")
        assert _labels(v)[0] == "Keep current: Funny"
        edited = await _click(v.children[0])
        assert v.selected == "Funny" and edited["content"] == "✅ Keeping default tone: **Funny**"

    def test_a_removed_default_gets_no_keep_button(self):
        v = ts.ToneDefaultView(["Funny"], current="Serious")
        assert _labels(v) == [None]


class TestTemplateListView:
    def test_disabled_controls(self):
        v = ts.TemplateListView(count=1, at_cap=True)
        b = {c.label: c for c in v.children}
        assert b["➕ Add"].disabled and b["🗑️ Delete"].disabled
        assert not b["✏️ Edit"].disabled
        v = ts.TemplateListView(count=0, at_cap=False)
        assert all(c.disabled for c in v.children if c.label != "➕ Add")

    @pytest.mark.parametrize(
        "label, action",
        [
            ("➕ Add", "add"),
            ("✏️ Edit", "edit"),
            ("⭐ Set Default", "default"),
            ("🗑️ Delete", "delete"),
            ("✅ Done", "done"),
        ],
    )
    @pytest.mark.asyncio
    async def test_each_button(self, label, action):
        v = ts.TemplateListView(count=3, at_cap=False)
        await _click(next(c for c in v.children if c.label == label))
        assert v.action == action and all(c.disabled for c in v.children) and v.is_finished()


class TestPickView:
    @pytest.mark.asyncio
    async def test_pick(self):
        v = ts.PickView([{"name": "A"}, {"name": "B"}])
        assert [o.label for o in _select(v).options] == ["A", "B"]
        await _click(_select(v), ["1"])
        assert v.idx == 1 and v.is_finished()


class TestWeekdaySelectView:
    @pytest.mark.asyncio
    async def test_fresh_and_keep(self):
        v = tr_setup._WeekdaySelectView()
        assert _labels(v) == [None]
        edited = await _click(_select(v), ["2"])
        assert v.selected == 2 and edited["content"] == "✅ Draft day: **Wednesday**"
        v = tr_setup._WeekdaySelectView(current=6)
        assert _labels(v)[0] == "Keep current: Sunday"
        edited = await _click(v.children[0])
        assert v.selected == 6 and edited["content"] == "✅ Draft day: **Sunday**"


class TestRosterView:
    @pytest.mark.asyncio
    async def test_keep_and_set(self):
        v = tr_setup._RosterView(cur_tab="Member Roster", cur_col_letter="B")
        assert _labels(v) == ["⚙️ Set roster tab + column", "Keep current"]
        await _click(v.children[1])
        assert v.choice == "keep" and v.modal_out is None

        v = tr_setup._RosterView(cur_tab="Member Roster", cur_col_letter="B")
        inter = MagicMock()
        inter.response = AsyncMock()
        inter.edit_original_response = AsyncMock()

        async def _send_modal(modal):
            assert (modal._tab.default, modal._col.default) == ("Member Roster", "B")
            modal._tab._value = " Members "
            modal._col._value = "d"
            await modal.on_submit(MagicMock(response=AsyncMock()))

        inter.response.send_modal = AsyncMock(side_effect=_send_modal)
        await v.children[0].callback(inter)
        assert v.choice == "set" and v.modal_out == ("Members", 3)

    @pytest.mark.asyncio
    async def test_an_unreadable_letter_defaults_to_column_b_and_a_dismissed_modal_keeps(self):
        v = tr_setup._RosterView(cur_tab="Member Roster", cur_col_letter="B")
        inter = MagicMock()
        inter.response = AsyncMock()
        inter.edit_original_response = AsyncMock()

        async def _send_modal(modal):
            modal._tab._value = ""
            modal._col._value = "??"
            await modal.on_submit(MagicMock(response=AsyncMock()))

        inter.response.send_modal = AsyncMock(side_effect=_send_modal)
        await v.children[0].callback(inter)
        assert v.modal_out == ("Member Roster", 1)

        v = tr_setup._RosterView(cur_tab="Member Roster", cur_col_letter="B")
        inter.response.send_modal = AsyncMock(side_effect=lambda modal: modal.stop())
        await v.children[0].callback(inter)
        assert v.choice == "keep" and v.modal_out is None


class TestTabsChoiceView:
    @pytest.mark.asyncio
    async def test_labels_and_choices(self):
        v = tr_setup._TabsChoiceView(
            saved_custom=False, history_tab="H", member_rules_tab="M", day_rules_tab="D"
        )
        assert _labels(v) == ["✅ Use default", "✏️ Define my own"]
        await _click(v.children[0])
        assert v.choice == "default"
        v = tr_setup._TabsChoiceView(
            saved_custom=True, history_tab="H", member_rules_tab="M", day_rules_tab="D"
        )
        assert _labels(v) == ["Keep current", "↩️ Use default", "✏️ Define my own"]
        await _click(v.children[0])
        assert v.choice == "keep"

    @pytest.mark.asyncio
    async def test_define_my_own_blank_fields_fall_back_to_defaults(self):
        v = tr_setup._TabsChoiceView(
            saved_custom=False, history_tab="H", member_rules_tab="M", day_rules_tab="D"
        )
        inter = MagicMock()
        inter.response = AsyncMock()
        inter.edit_original_response = AsyncMock()

        async def _send_modal(modal):
            assert (modal._h.default, modal._m.default, modal._d.default) == ("H", "M", "D")
            modal._h._value = "Hist"
            modal._m._value = ""
            modal._d._value = " Days "
            await modal.on_submit(MagicMock(response=AsyncMock()))

        inter.response.send_modal = AsyncMock(side_effect=_send_modal)
        await v.children[1].callback(inter)
        assert v.choice == "custom" and v.modal_out == ("Hist", "Train Member Rules", "Days")


class TestRuleRoleAttachView:
    @pytest.mark.asyncio
    async def test_attach_needs_both_picks_then_accumulates(self):
        v = tr_setup._RuleRoleAttachView({})
        assert v.summary() == "_No roles attached yet._"
        attach = next(
            c for c in v.children if getattr(c, "label", None) == "Attach role to this rule"
        )
        inter = MagicMock()
        inter.response = AsyncMock()
        await attach.callback(inter)
        inter.response.send_message.assert_awaited_once_with(
            "Pick both a rule type and a role first, then hit Attach.", ephemeral=True
        )
        v._rule_sel._values = ["vs"]
        await v._rule_sel.callback(inter)
        role = MagicMock(id=4242)
        role.name = "Fighters"
        v._role_sel._values = [role]
        await v._role_sel.callback(inter)
        await attach.callback(inter)
        assert v.rule_type_roles == {"vs": 4242}
        content = inter.response.edit_message.await_args.kwargs["content"]
        assert content.startswith("✅ **VS (you assign)** days will pull from **@Fighters**.")
        assert "• VS (you assign) → <@&4242>" in content
        done = next(c for c in v.children if getattr(c, "label", None) == "✅ Done")
        await done.callback(inter)
        assert v.done and v.is_finished()
