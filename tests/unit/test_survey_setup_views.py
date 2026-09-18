"""The survey wizard's views, clicked.

`tests/integration/test_survey_setup.py` answers each view by attribute;
before the move to `survey_setup.py` (#611 round 2) most of these classes
were inline and could not be constructed alone. Each control is held to
the attribute it sets and the acknowledgement it renders.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import survey_setup as ss  # noqa: E402


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


def _select(view, nth=0):
    return [c for c in view.children if hasattr(c, "options")][nth]


def _buttons(view):
    return {c.label: c for c in view.children if getattr(c, "label", None)}


class TestIntroChoiceView:
    @pytest.mark.parametrize("label, choice", [("Keep current", "keep"), ("✏️ Edit", "edit")])
    @pytest.mark.asyncio
    async def test_buttons(self, label, choice):
        v = ss.IntroChoiceView()
        assert list(_buttons(v)) == ["Keep current", "✏️ Edit"]
        edited = await _click(_buttons(v)[label])
        assert v.intro_choice == choice and edited["view"] is v
        assert all(c.disabled for c in v.children) and v.is_finished()


class TestQuestionStartView:
    def test_options_follow_what_exists(self):
        both = ss.QuestionStartView(has_template_qs=True, has_existing_qs=True, template_name="T")
        assert list(_buttons(both)) == [
            "✅ Use these questions",
            "✏️ Edit these questions",
            "✏️ Edit this survey's questions",
            "♻️ Start from scratch",
        ]
        only_existing = ss.QuestionStartView(
            has_template_qs=False, has_existing_qs=True, template_name="T"
        )
        assert list(_buttons(only_existing)) == [
            "✏️ Edit this survey's questions",
            "♻️ Start from scratch",
        ]
        only_template = ss.QuestionStartView(
            has_template_qs=True, has_existing_qs=False, template_name="T"
        )
        assert list(_buttons(only_template)) == [
            "✅ Use these questions",
            "✏️ Edit these questions",
            "♻️ Start from scratch",
        ]

    @pytest.mark.parametrize(
        "label, choice, ack",
        [
            (
                "✅ Use these questions",
                "template_asis",
                "✅ Using the Squad Power Survey questions.",
            ),
            (
                "✏️ Edit these questions",
                "template_edit",
                "✏️ Loading the template's questions so you can edit them...",
            ),
            ("✏️ Edit this survey's questions", "edit", "✏️ Entering edit mode..."),
            ("♻️ Start from scratch", "scratch", "🔄 Starting from scratch..."),
        ],
    )
    @pytest.mark.asyncio
    async def test_buttons(self, label, choice, ack):
        v = ss.QuestionStartView(
            has_template_qs=True, has_existing_qs=True, template_name="Squad Power Survey"
        )
        edited = await _click(_buttons(v)[label])
        assert v.choice == choice and edited["content"] == ack
        assert all(c.disabled for c in v.children)


class TestQuestionListView:
    QS = [{"label": "Time Zone"}, {"label": "Role"}]

    def test_no_pickers_without_questions(self):
        v = ss.QuestionListView([])
        assert not [c for c in v.children if hasattr(c, "options")]
        assert list(_buttons(v)) == ["➕ Add Question", "✅ Finish Survey Setup"]

    def test_pickers_list_every_question(self):
        v = ss.QuestionListView(self.QS)
        edit, delete = _select(v, 0), _select(v, 1)
        assert edit.placeholder == "✏️ Edit a question..."
        assert [(o.label, o.value) for o in edit.options] == [
            ("Edit: Time Zone", "0"),
            ("Edit: Role", "1"),
        ]
        assert delete.placeholder == "🗑️ Delete a question..."
        assert [(o.label, o.value) for o in delete.options] == [
            ("Delete: Time Zone", "0"),
            ("Delete: Role", "1"),
        ]
        assert (edit.row, delete.row) == (0, 1)
        assert all(b.row == 2 for b in _buttons(v).values())

    @pytest.mark.asyncio
    async def test_edit_and_delete_record_the_index(self):
        v = ss.QuestionListView(self.QS)
        await _click(_select(v, 0), ["1"])
        assert (v.action, v.edit_index, v.del_index) == ("edit", 1, None)
        v = ss.QuestionListView(self.QS)
        await _click(_select(v, 1), ["0"])
        assert (v.action, v.edit_index, v.del_index) == ("delete", None, 0)
        assert all(c.disabled for c in v.children)

    @pytest.mark.parametrize(
        "label, action", [("➕ Add Question", "add"), ("✅ Finish Survey Setup", "finish")]
    )
    @pytest.mark.asyncio
    async def test_buttons(self, label, action):
        v = ss.QuestionListView(self.QS)
        edited = await _click(_buttons(v)[label])
        assert v.action == action and edited["view"] is v and v.is_finished()


class TestTypeView:
    @pytest.mark.asyncio
    async def test_select_names_the_type(self):
        import discord

        opts = [discord.SelectOption(label=l, value=v) for l, v in ss._FREE_TYPE_OPTIONS]
        v = ss.TypeView(opts)
        sel = _select(v)
        assert sel.placeholder == "Select answer type..."
        edited = await _click(sel, ["numeric"])
        assert v.selected == "numeric" and edited["content"] == "✅ Type: **Numeric**"
        assert sel.disabled and v.is_finished()

    @pytest.mark.asyncio
    async def test_unknown_type_echoes_its_value(self):
        import discord

        v = ss.TypeView([discord.SelectOption(label="x", value="odd")])
        edited = await _click(_select(v), ["odd"])
        assert edited["content"] == "✅ Type: **odd**"


class TestMagnitudeView:
    @pytest.mark.parametrize(
        "value, pretty",
        [
            ("raw", "Exact number"),
            ("K", "Thousands (K)"),
            ("M", "Millions (M)"),
            ("B", "Billions (B)"),
        ],
    )
    @pytest.mark.asyncio
    async def test_select_names_the_scale(self, value, pretty):
        v = ss.MagnitudeView()
        sel = _select(v)
        assert sel.placeholder == "Select number scale..."
        assert [o.value for o in sel.options] == ["raw", "K", "M", "B"]
        edited = await _click(sel, [value])
        assert v.selected == value and edited["content"] == f"✅ Scale: **{pretty}**"


class TestStartChoiceView:
    @pytest.mark.parametrize(
        "label, choice",
        [("➕ Start from a template", "template"), ("✏️ Start from scratch", "scratch")],
    )
    @pytest.mark.asyncio
    async def test_buttons(self, label, choice):
        v = ss.StartChoiceView()
        edited = await _click(_buttons(v)[label])
        assert v.start_choice == choice and edited["view"] is v and v.is_finished()


class TestTemplatePickView:
    @pytest.mark.asyncio
    async def test_select_names_the_template(self):
        v = ss.TemplatePickView()
        sel = _select(v)
        assert sel.placeholder == "Pick a template..."
        assert [o.value for o in sel.options] == ["squad_power"]
        assert sel.options[0].label == "Squad Power Survey" and sel.options[0].emoji.name == "⚔️"
        edited = await _click(sel, ["squad_power"])
        assert v.selected == "squad_power"
        assert edited["content"] == "✅ Template: **Squad Power Survey**" and sel.disabled


class TestNameChoiceView:
    @pytest.mark.asyncio
    async def test_use_the_suggested_name(self):
        v = ss.NameChoiceView("Squad Power Survey")
        assert list(_buttons(v)) == ["✅ Use this name: Squad Power Survey", "✏️ Name it myself"]
        edited = await _click(_buttons(v)["✅ Use this name: Squad Power Survey"])
        assert (v.survey_name, v.answered) == ("Squad Power Survey", True)
        assert edited["content"] == "✅ Name: **Squad Power Survey**" and v.is_finished()

    def test_a_long_suggestion_is_cut_to_the_label_limit(self):
        v = ss.NameChoiceView("x" * 100)
        assert len(list(_buttons(v))[0]) == 80

    @pytest.mark.parametrize(
        "typed, expected",
        [
            ("  My Survey  ", "My Survey"),
            ("", "Suggested"),
            (None, "Suggested"),
            ("y" * 70, "y" * 60),
        ],
    )
    @pytest.mark.asyncio
    async def test_name_it_myself_opens_a_modal(self, typed, expected):
        modal = MagicMock(value=typed, wait=AsyncMock())
        v = ss.NameChoiceView("Suggested")
        inter = MagicMock()
        inter.response.send_modal = AsyncMock()
        inter.message.edit = AsyncMock()
        with patch("survey_setup.TextInputModal", return_value=modal) as cls:
            await _buttons(v)["✏️ Name it myself"].callback(inter)
        assert cls.call_args.args == ("Survey Name", "What should this survey be called?")
        assert cls.call_args.kwargs == {"default": "Suggested"}
        inter.response.send_modal.assert_awaited_once_with(modal)
        assert (v.survey_name, v.answered) == (expected, True)
        assert inter.message.edit.await_args.kwargs["content"] == f"✅ Name: **{expected}**"
        assert all(c.disabled for c in v.children) and v.is_finished()

    @pytest.mark.asyncio
    async def test_an_uneditable_message_still_finishes(self):
        import discord

        modal = MagicMock(value="Named", wait=AsyncMock())
        v = ss.NameChoiceView("Suggested")
        inter = MagicMock()
        inter.response.send_modal = AsyncMock()
        inter.message.edit = AsyncMock(side_effect=discord.HTTPException(MagicMock(), "gone"))
        with patch("survey_setup.TextInputModal", return_value=modal):
            await _buttons(v)["✏️ Name it myself"].callback(inter)
        assert v.survey_name == "Named" and v.is_finished()


class TestRemoveViews:
    EXTRAS = [
        {"survey_id": "vp", "survey_name": "VP Buff"},
        {"survey_id": "nameless", "survey_name": ""},
    ]

    @pytest.mark.asyncio
    async def test_pick_opens_the_confirm(self):
        v = ss._RemovePickView(1, self.EXTRAS)
        sel = _select(v)
        assert sel.placeholder == "Pick a survey to remove…"
        assert [(o.label, o.value) for o in sel.options] == [
            ("VP Buff", "vp"),
            ("nameless", "nameless"),
        ]
        edited = await _click(sel, ["vp"])
        assert edited["content"] == "⚠️ Confirm: remove **VP Buff**?"
        assert isinstance(edited["view"], ss._ConfirmRemoveView)
        assert edited["view"].target == self.EXTRAS[0] and edited["view"].guild_id == 1
        assert sel.disabled and v.is_finished()

    @pytest.mark.asyncio
    async def test_confirm_deletes_and_reports(self):
        v = ss._ConfirmRemoveView(1, self.EXTRAS[0])
        assert list(_buttons(v)) == ["🗑️ Remove", "❌ Cancel"]
        with patch("config.delete_extra_survey", return_value=True) as delete:
            edited = await _click(_buttons(v)["🗑️ Remove"])
        delete.assert_called_once_with(1, "vp")
        assert edited == {"content": "🗑️ Removed **VP Buff**.", "view": None}
        with patch("config.delete_extra_survey", return_value=False):
            edited = await _click(_buttons(ss._ConfirmRemoveView(1, self.EXTRAS[0]))["🗑️ Remove"])
        assert edited["content"] == "⚠️ Could not remove that survey."

    @pytest.mark.asyncio
    async def test_cancel(self):
        v = ss._ConfirmRemoveView(1, self.EXTRAS[0])
        edited = await _click(_buttons(v)["❌ Cancel"])
        assert edited == {"content": "❌ Canceled. No surveys removed.", "view": None}
        assert v.is_finished()


class TestEditPickView:
    SURVEYS = [
        {"survey_id": "default", "survey_name": "Default", "template": "squad_power"},
        {"survey_id": "vp", "survey_name": "VP Buff", "template": "scratch"},
    ]

    @pytest.mark.asyncio
    async def test_options_and_dispatch(self):
        interaction, bot = MagicMock(), MagicMock()
        v = ss._EditPickView(interaction, bot, self.SURVEYS)
        sel = _select(v)
        assert sel.placeholder == "Pick a survey to edit…"
        assert [(o.label, o.value) for o in sel.options] == [
            ("Default", "default"),
            ("VP Buff", "vp"),
        ]
        with patch("survey_setup.run_survey_setup", AsyncMock()) as run:
            edited = await _click(sel, ["vp"])
        assert edited["content"] == "✏️ Editing **VP Buff**…" and edited["view"] is v
        assert run.await_args.args == (interaction, bot)
        assert run.await_args.kwargs == dict(
            target_survey_id="vp", target_survey_name="VP Buff", template="scratch"
        )

    @pytest.mark.asyncio
    async def test_default_dispatches_with_no_id(self):
        v = ss._EditPickView(MagicMock(), MagicMock(), self.SURVEYS)
        with patch("survey_setup.run_survey_setup", AsyncMock()) as run:
            edited = await _click(_select(v), ["default"])
        assert edited["content"] == "✏️ Editing **Default**…"
        assert run.await_args.kwargs["target_survey_id"] is None
        assert run.await_args.kwargs["template"] == "squad_power"

    @pytest.mark.asyncio
    async def test_an_unknown_pick_edits_by_id(self):
        v = ss._EditPickView(MagicMock(), MagicMock(), self.SURVEYS)
        with patch("survey_setup.run_survey_setup", AsyncMock()) as run:
            edited = await _click(_select(v), ["ghost"])
        assert edited["content"] == "✏️ Editing **ghost**…"
        assert run.await_args.kwargs == dict(
            target_survey_id="ghost", target_survey_name=None, template=None
        )


class TestSurveyConfiguredView:
    """The Post and Edit buttons are clicked in
    `tests/unit/test_survey_templates.py`; this holds the shape."""

    def test_shape(self):
        from survey_hub import SURVEY_HUB_BTN_EDIT, SURVEY_HUB_BTN_POST

        v = ss.SurveyConfiguredView(
            MagicMock(),
            guild_id=1,
            survey_id=None,
            survey_name="Default",
            template_key="squad_power",
            owner_id=7,
        )
        assert list(_buttons(v)) == [SURVEY_HUB_BTN_POST, SURVEY_HUB_BTN_EDIT]
        assert v.timeout == ss.SURVEY_CONFIRM_VIEW_TIMEOUT == 900
        assert v.timeout_hint == "`/survey`" and v.owner_id == 7 and v.message is None
