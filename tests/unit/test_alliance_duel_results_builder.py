"""The past-week results builder: pick alliances and scores from dropdowns, add
each match to a list, and save the week at once."""

import discord
import pytest

import alliance_duel as ad
import alliance_duel_entry as entry
import alliance_duel_hub as hub
import alliance_duel_results_builder as builder

LEAGUE = ad.LeagueKey("S35", "Diamond", "12 - 2")
TAGS = [f"T{i:02d}" for i in range(1, ad.BRACKET_SIZE + 1)]
OWNER = 7


def _key(tag):
    return ad.AllianceKey.of(tag, "999")


def _state(week=1):
    rows = [
        ad.AllianceWeek(
            league=LEAGUE,
            week=week,
            alliance=_key(tag),
            ranking=rank,
            tag_display=tag,
            warzone_display="999",
        )
        for rank, tag in enumerate(TAGS, start=1)
    ]
    cfg = {
        "guild_id": 1,
        "enabled": 1,
        "tab_name": "Alliance Duel (VS)",
        "own_tag": TAGS[0],
        "own_warzone": "999",
        "tracking_mode": ad.MODE_FULL_BRACKET,
    }
    return hub.HubState(1, cfg, rows)


class _Response:
    def __init__(self):
        self.edited = None
        self.sent = None
        self.modal = None
        self.deferred = False

    async def edit_message(self, **kw):
        self.edited = kw

    async def send_message(self, content=None, **kw):
        self.sent = (content, kw)

    async def send_modal(self, modal):
        self.modal = modal

    async def defer(self, **kw):
        self.deferred = True


class _Interaction:
    def __init__(self, values=None):
        self.data = {"values": values or []}
        self.user = type("U", (), {"id": OWNER})()
        self.response = _Response()
        self.followed = []
        self.deleted = False

        async def _send(content=None, **kw):
            self.followed.append(content)

        self.followup = type("F", (), {"send": staticmethod(_send)})()

    async def delete_original_response(self):
        self.deleted = True

    async def original_response(self):
        return object()


def _selects(view):
    return [c for c in view.children if isinstance(c, discord.ui.Select)]


def _buttons(view):
    return {c.label: c for c in view.children if isinstance(c, discord.ui.Button)}


async def _pick(view, attr, value):
    setter = view._setter(attr, alliance=attr in ("first", "second"))
    await setter(_Interaction([str(value)]))


async def _add_match(view, first, x, second, y):
    await _pick(view, "first", view.roster.index(_key(first)))
    await _pick(view, "first_score", x)
    await _pick(view, "second", view.roster.index(_key(second)))
    await _pick(view, "second_score", y)
    await view._add(_Interaction())


@pytest.fixture
def _captured(monkeypatch):
    written = []

    async def _fake(state, rows, **kw):
        written.extend(rows)
        return ""

    monkeypatch.setattr(entry, "save_rows", _fake)
    return written


async def test_the_screen_is_four_dropdowns_then_one_row_of_buttons():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)

    assert [s.row for s in _selects(view)] == [0, 1, 2, 3]
    assert len(_selects(view)[1].options) == ad.WEEK_POINTS_TOTAL + 1
    assert set(_buttons(view)) == {
        builder.VS_BTN_BUILDER_ADD,
        builder.VS_BTN_BUILDER_SAVE,
        builder.VS_BTN_BUILDER_REMOVE,
        builder.VS_BTN_BUILDER_TEXT_BOX,
    }
    assert all(b.row == 4 for b in _buttons(view).values())
    assert sum(b.style == discord.ButtonStyle.primary for b in _buttons(view).values()) == 1


async def test_save_and_remove_wait_until_there_is_a_match():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)

    assert _buttons(view)[builder.VS_BTN_BUILDER_SAVE].disabled
    assert _buttons(view)[builder.VS_BTN_BUILDER_REMOVE].disabled
    assert not _buttons(view)[builder.VS_BTN_BUILDER_ADD].disabled


async def test_an_alliance_picked_on_one_side_is_not_offered_on_the_other():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)

    await _pick(view, "first", 0)

    second = _selects(view)[2]
    assert view.roster[0] == view.first
    assert str(0) not in [o.value for o in second.options]
    assert [o.value for o in _selects(view)[0].options if o.default] == ["0"]


async def test_a_score_names_its_alliance_once_that_alliance_is_picked():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)
    tag = view.state.display_name(view.roster[0])

    await _pick(view, "first", 0)
    await _pick(view, "first_score", 9)

    score = _selects(view)[1]
    assert score.placeholder == entry.VS_SCORE_LABEL.format(tag=tag)
    assert [o.label for o in score.options if o.default] == [f"{tag} 9"]


async def test_adding_needs_both_alliances_and_both_scores():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)
    await _pick(view, "first", 0)
    interaction = _Interaction()

    await view._add(interaction)

    assert interaction.response.sent[0] == builder.VS_BUILDER_INCOMPLETE
    assert view.matches == []


async def test_adding_refuses_scores_that_do_not_make_a_week():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)
    await _pick(view, "first", 0)
    await _pick(view, "first_score", 9)
    await _pick(view, "second", 1)
    await _pick(view, "second_score", 5)
    interaction = _Interaction()

    await view._add(interaction)

    assert "9 and 5" in interaction.response.sent[0]
    assert view.matches == []


async def test_an_added_match_joins_the_plain_list_and_leaves_the_dropdowns():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)

    await _add_match(view, "T01", 9, "T02", 4)

    assert view.matches == [(_key("T01"), 9, _key("T02"), 4)]
    assert view.first is None and view.second_score is None
    offered = {o.label for o in _selects(view)[0].options}
    assert "T01" not in offered and "T02" not in offered
    field = view.embed().fields[0]
    assert field.value == "T01 9 - 4 T02"
    assert "*" not in field.value
    assert field.name == builder.VS_BUILDER_ADDED.format(n=1, total=ad.BRACKET_SIZE // 2)


async def test_an_empty_list_says_so():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)

    assert view.embed().fields[0].value == builder.VS_BUILDER_EMPTY


async def test_removing_swaps_to_a_dropdown_of_the_added_matches():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)
    await _add_match(view, "T01", 9, "T02", 4)
    await _add_match(view, "T03", 13, "T04", 0)

    await view._start_remove(_Interaction())

    [select] = _selects(view)
    assert [o.label for o in select.options] == ["T01 9 - 4 T02", "T03 13 - 0 T04"]
    assert set(_buttons(view)) == {
        builder.VS_BTN_BUILDER_REMOVE_SELECTED,
        builder.VS_BTN_BUILDER_BACK,
    }
    assert _buttons(view)[builder.VS_BTN_BUILDER_REMOVE_SELECTED].disabled


async def test_the_selected_match_is_the_one_removed():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)
    await _add_match(view, "T01", 9, "T02", 4)
    await _add_match(view, "T03", 13, "T04", 0)
    await view._start_remove(_Interaction())

    await view._pick_doomed(_Interaction(["0"]))
    assert not _buttons(view)[builder.VS_BTN_BUILDER_REMOVE_SELECTED].disabled
    await view._remove_selected(_Interaction())

    assert view.matches == [(_key("T03"), 13, _key("T04"), 0)]
    assert not view.removing
    assert len(_selects(view)) == 4
    # The removed match's alliances are free to be entered again.
    assert "T01" in {o.label for o in _selects(view)[0].options}


async def test_backing_out_of_remove_keeps_every_match():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)
    await _add_match(view, "T01", 9, "T02", 4)
    await view._start_remove(_Interaction())
    await view._pick_doomed(_Interaction(["0"]))

    await view._leave_remove(_Interaction())

    assert len(view.matches) == 1
    assert not view.removing and view.doomed is None


async def test_a_full_week_has_no_dropdowns_left_and_nothing_to_add():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)
    for i in range(0, ad.BRACKET_SIZE, 2):
        await _add_match(view, TAGS[i], 9, TAGS[i + 1], 4)

    assert _selects(view) == []
    assert _buttons(view)[builder.VS_BTN_BUILDER_ADD].disabled
    assert not _buttons(view)[builder.VS_BTN_BUILDER_SAVE].disabled


async def test_save_writes_both_sides_of_every_match_and_confirms(_captured):
    view = builder.ResultsBuilderView(_state(), 1, OWNER)
    await _add_match(view, "T01", 9, "T02", 4)
    await _add_match(view, "T03", 4, "T04", 9)
    interaction = _Interaction()

    await view._save(interaction)

    assert len(_captured) == 4
    by_alliance = {r.alliance: r for r in _captured}
    assert by_alliance[_key("T01")].week_score == 9 and by_alliance[_key("T01")].week_outcome == "W"
    assert by_alliance[_key("T02")].week_score == 4 and by_alliance[_key("T02")].week_outcome == "L"
    assert by_alliance[_key("T04")].week_outcome == "W"
    assert by_alliance[_key("T01")].opponent == _key("T02")
    assert interaction.followed[0].startswith("✅ Saved 2 results:")
    assert "T01 beat T02 9-4" in interaction.followed[0]
    assert "T04 beat T03 9-4" in interaction.followed[0]
    assert interaction.deleted
    assert view.is_finished()


async def test_a_failed_save_keeps_the_list_on_screen(monkeypatch):
    async def _fail(state, rows, **kw):
        return "I couldn't write to your tab."

    monkeypatch.setattr(entry, "save_rows", _fail)
    view = builder.ResultsBuilderView(_state(), 1, OWNER)
    await _add_match(view, "T01", 9, "T02", 4)
    interaction = _Interaction()

    await view._save(interaction)

    assert interaction.followed == ["⚠️ I couldn't write to your tab."]
    assert not interaction.deleted and not view.is_finished()
    assert len(view.matches) == 1


async def test_the_text_box_button_opens_the_past_week_box():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)
    interaction = _Interaction()

    await view._text_box(interaction)

    modal = interaction.response.modal
    assert isinstance(modal, entry.OtherResultsModal) and modal.backfill


async def test_choosing_a_week_opens_the_builder_in_place():
    state = _state()
    picker = entry.BackfillWeekPickerView(state, OWNER)
    interaction = _Interaction()

    await next(c for c in picker.children if c.label == "Week 1").callback(interaction)

    edited = interaction.response.edited
    assert isinstance(edited["view"], builder.ResultsBuilderView)
    assert edited["content"] is None
    assert edited["embed"].title == entry.VS_RESULTS_MODAL_TITLE.format(week=1)
    assert picker.is_finished()


async def test_both_states_fit_discords_five_rows():
    view = builder.ResultsBuilderView(_state(), 1, OWNER)
    assert len(view.to_components()) == 5

    await _add_match(view, "T01", 9, "T02", 4)
    await view._start_remove(_Interaction())

    assert len(view.to_components()) == 2
