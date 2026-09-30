"""Unit tests for Edit league and removing an alliance (#651).

Removing is the one delete in Alliance Duel (VS), so most of what is pinned
here is its narrowness: the current league only, never the officer's own
alliance, never a row another server recorded, and a pairing that still names
the removed alliance only cleared when the officer asks for it.
"""

import datetime as _dt
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import alliance_duel as ad
import alliance_duel_hub as hub
import alliance_duel_league_edit as edit
import config_health

LEAGUE = ad.LeagueKey("S35", "Diamond", "12 - 2")
OLD_LEAGUE = ad.LeagueKey("S34", "Gold", "3 - 1")
MONDAY = ad.week_monday(ad.server_today())
OWN_TAG, WZ = "Fre3", "1234"


def _key(tag: str) -> ad.AllianceKey:
    return ad.AllianceKey.of(tag, WZ)


def _row(tag, week=1, ranking=None, league=LEAGUE, **kw):
    # A league's weeks are a week apart, and the earlier league two months back.
    start = MONDAY if league == LEAGUE else MONDAY - _dt.timedelta(weeks=8)
    return ad.AllianceWeek(
        league=league,
        week=week,
        alliance=_key(tag),
        ranking=ranking,
        week_date=start + _dt.timedelta(weeks=week - 1),
        tag_display=tag,
        **kw,
    )


def _state(rows):
    cfg = {
        "guild_id": 1,
        "tab_name": "Alliance Duel (VS)",
        "own_tag": OWN_TAG,
        "own_warzone": WZ,
        "tracking_mode": ad.MODE_FULL_BRACKET,
    }
    return hub.HubState(1, cfg, rows)


def _grid(rows):
    header = list(ad.SHEET_COLUMNS)
    hidx = {name: i for i, name in enumerate(header)}
    grid = [header]
    for row in rows:
        line = [""] * len(header)
        for name, value in ad.row_values(row).items():
            line[hidx[name]] = value
        grid.append(line)
    return grid


@pytest.fixture(autouse=True)
def _no_recorded_sheet_problems(monkeypatch):
    monkeypatch.setattr(config_health, "problems_for_subjects", lambda *a, **k: [])


def _typo_league():
    """Our alliance, two real ones, and LlON: a typo of LION paired with A03."""
    return [
        _row(OWN_TAG, ranking=1),
        _row("LION", ranking=2),
        _row("A03", ranking=3, opponent=_key("LlON")),
        _row("LlON", opponent=_key("A03")),
        _row("A03", week=2, ranking=3, opponent=_key("LlON")),
        _row("LlON", week=2, opponent=_key("A03")),
    ]


# ── The Sheet: planning the delete and the clear ──────────────────────────────


def test_a_removal_finds_every_week_of_that_alliance_in_this_league_only():
    rows = _typo_league() + [_row("LlON", league=OLD_LEAGUE)]
    numbers = ad.plan_remove_alliance(_grid(rows), LEAGUE, _key("LlON"))
    # Grid row 1 is the header, so list index i sits on sheet row i + 2.
    assert numbers == (5, 7)


def test_row_deletes_go_bottom_up_in_one_request():
    worksheet = MagicMock()
    worksheet.id = 42
    ad.apply_row_deletes(worksheet, [3, 4, 5, 9])
    requests = worksheet.spreadsheet.batch_update.call_args.args[0]["requests"]
    ranges = [r["deleteDimension"]["range"] for r in requests]
    assert [(r["startIndex"], r["endIndex"]) for r in ranges] == [(8, 9), (2, 5)]
    assert all(r["sheetId"] == 42 and r["dimension"] == "ROWS" for r in ranges)


def test_no_rows_means_no_request():
    worksheet = MagicMock()
    ad.apply_row_deletes(worksheet, [])
    worksheet.spreadsheet.batch_update.assert_not_called()


def test_clearing_blanks_only_the_opponent_cells_that_still_name_it():
    rows = _typo_league()
    rows[4].opponent = _key("LION")  # week 2 re-paired in the meantime
    keys = [ad.RowKey(LEAGUE, 1, _key("A03")), ad.RowKey(LEAGUE, 2, _key("A03"))]
    plan = ad.plan_clear_opponent(_grid(rows), keys, _key("LlON"))

    header = list(ad.SHEET_COLUMNS)
    letters = {
        ad.transfer.col_index_to_letter(header.index(ad.COL_OPPONENT_TAG)),
        ad.transfer.col_index_to_letter(header.index(ad.COL_OPPONENT_WARZONE)),
    }
    assert {u.a1 for u in plan.updates} == {f"{letter}4" for letter in letters}
    assert all(u.value == "" for u in plan.updates)
    assert plan.appends == ()


# ── The shared store: this server's copies only ───────────────────────────────


@pytest.fixture
def central(tmp_path, monkeypatch):
    import alliance_duel_db as vsdb

    monkeypatch.setattr(vsdb, "DB_PATH", str(tmp_path / "alliance_duel.sqlite3"))
    vsdb.init_db()
    return vsdb


def test_removal_takes_this_servers_copies_and_leaves_everyone_elses(central):
    ours = _row("LlON", day_scores={1: 500})
    central.record_weeks([ours, _row("LlON", league=OLD_LEAGUE)], actor={"guild_id": 1})
    central.record_weeks([_row("LlON", week=2)], actor={"guild_id": 2})

    assert central.remove_alliance_from_league(_key("LlON"), LEAGUE, guild_id=1) == 1

    left = central.weeks_for_alliance(_key("LlON"))
    assert {(r["season"], r["week"], r["actor_guild_id"]) for r in left} == {
        ("S34", 1, "1"),  # an earlier league
        ("S35", 2, "2"),  # another server's reading
    }
    with central._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM alliance_week_days").fetchone()[0] == 0


def test_clearing_takes_this_servers_pairing_only(central):
    central.record_weeks([_row("A03", opponent=_key("LlON"))], actor={"guild_id": 1})
    central.record_weeks([_row("A03", week=2, opponent=_key("LlON"))], actor={"guild_id": 2})

    pairs = [(_key("A03"), 1, MONDAY), (_key("A03"), 2, MONDAY + _dt.timedelta(weeks=1))]
    assert central.clear_opponent(pairs, LEAGUE, _key("LlON"), guild_id=1) == 1

    by_week = {r["week"]: r["opponent_tag"] for r in central.weeks_for_alliance(_key("A03"))}
    assert by_week == {1: None, 2: _key("LlON").tag}


# ── Picking and confirming ────────────────────────────────────────────────────


def test_the_picker_offers_everyone_but_us_ranked_first():
    state = _state(_typo_league())
    tags = [state.display_name(a) for a, _rank in edit.removable_alliances(state)]
    assert tags == ["LION", "A03", "LlON"]


def test_pairings_name_who_is_still_recorded_against_it_and_when():
    state = _state(_typo_league())
    assert edit.pairings_with(state, _key("LlON")) == {_key("A03"): [1, 2]}


@pytest.mark.parametrize(
    "weeks, said", [([1], "week 1"), ([2, 1], "weeks 1 and 2"), ([1, 2, 3], "weeks 1, 2 and 3")]
)
def test_weeks_read_as_a_phrase(weeks, said):
    assert edit.weeks_phrase(weeks) == said


def test_the_confirm_step_warns_about_a_pairing_before_anything_happens():
    embed = edit.confirm_embed(_state(_typo_league()), _key("LlON"))
    assert embed.title == "🗑️ Remove LlON from S35 Diamond 12 - 2?"
    assert "for weeks 1 and 2 of this League" in embed.description
    assert embed.fields[0].name == "Still paired with LlON"
    assert "**A03** is recorded against **LlON** in weeks 1 and 2." in embed.fields[0].value


def test_an_unpaired_alliance_has_no_warning():
    embed = edit.confirm_embed(_state(_typo_league()), _key("LION"))
    assert embed.fields == []


def test_edit_league_offers_details_add_and_remove():
    view = edit.EditLeagueView(_state(_typo_league()), owner_id=7)
    assert [c.label for c in view.children] == [
        "✏️ Edit league details",
        "➕ Add or edit alliance",
        "🗑️ Remove an alliance",
    ]
    assert not view.children[2].disabled


def test_remove_is_off_when_there_is_nobody_but_us():
    view = edit.EditLeagueView(_state([_row(OWN_TAG, ranking=1)]), owner_id=7)
    assert view.children[2].disabled


async def test_the_picker_says_so_when_there_is_nobody_to_remove():
    inter = MagicMock()
    inter.response.send_message = AsyncMock()
    await edit.open_remove_picker(inter, _state([_row(OWN_TAG, ranking=1)]))
    assert inter.response.send_message.call_args.args[0] == (
        "There's no other alliance in **S35 Diamond 12 - 2** to remove."
    )


def _interaction():
    inter = MagicMock()
    inter.user.id = 7
    inter.response.defer = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.followup.send = AsyncMock(return_value=MagicMock())
    return inter


async def test_cancel_removes_nothing():
    view = edit.RemoveConfirmView(_state(_typo_league()), 7, _key("LlON"))
    inter = _interaction()
    with patch.object(edit, "remove_alliance", AsyncMock()) as removing:
        await view._cancel(inter)
    removing.assert_not_awaited()
    assert inter.response.edit_message.call_args.kwargs["content"] == "Nothing was removed."


async def test_a_removal_offers_to_repair_the_pairing_it_leaves():
    view = edit.RemoveConfirmView(_state(_typo_league()), 7, _key("LlON"))
    inter = _interaction()
    with patch.object(edit, "remove_alliance", AsyncMock(return_value="")):
        await view._confirm(inter)

    inter.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
    text = inter.followup.send.call_args.args[0]
    assert text.startswith("✅ Removed **LlON** from **S35 Diamond 12 - 2**.")
    assert "**A03** still has **LlON** as their opponent in weeks 1 and 2." in text
    repair = inter.followup.send.call_args.kwargs["view"]
    assert [c.label for c in repair.children] == [
        "Enter week 1 results",
        "Enter week 2 results",
        "Leave them unpaired",
    ]


async def test_a_failed_removal_says_why_and_offers_nothing_more():
    view = edit.RemoveConfirmView(_state(_typo_league()), 7, _key("LlON"))
    inter = _interaction()
    failed = "⚠️ I couldn't remove it from your tab: gone"
    with patch.object(edit, "remove_alliance", AsyncMock(return_value=failed)):
        await view._confirm(inter)
    assert inter.followup.send.call_args.args[0] == failed
    assert "view" not in inter.followup.send.call_args.kwargs


# ── Doing it ──────────────────────────────────────────────────────────────────


class _FakeSheet:
    def __init__(self, grid):
        self.grid = grid
        self.deleted = None
        self.updates = None

    def get_all_values(self):
        return self.grid


@pytest.fixture
def sheet(monkeypatch):
    import config

    holder = {}

    def _make(rows):
        fake = _FakeSheet(_grid(rows))
        holder["sheet"] = fake
        monkeypatch.setattr(config, "get_spreadsheet", lambda gid: object())
        monkeypatch.setattr(edit.ad_setup, "ensure_tab", lambda *a, **k: fake)
        monkeypatch.setattr(
            ad, "apply_row_deletes", lambda ws, numbers: setattr(ws, "deleted", tuple(numbers))
        )
        monkeypatch.setattr(ad, "apply_upsert", lambda ws, plan: setattr(ws, "updates", plan))
        return fake

    return _make


async def test_removing_deletes_the_rows_and_updates_the_snapshot(sheet, central):
    rows = _typo_league()
    fake = sheet(rows)
    state = _state(rows)

    assert await edit.remove_alliance(state, _key("LlON")) == ""
    assert fake.deleted == (5, 7)
    assert _key("LlON") not in {r.alliance for r in state.rows}
    assert _key("LlON") not in state.profiles


async def test_a_sheet_failure_is_reported_and_recorded_not_raised(monkeypatch):
    import config

    def _boom(gid):
        raise RuntimeError("no access")

    monkeypatch.setattr(config, "get_spreadsheet", _boom)
    state = _state(_typo_league())
    with patch.object(config_health, "record_sheet_failure") as recorded:
        problem = await edit.remove_alliance(state, _key("LlON"))
    assert problem.startswith("⚠️ I couldn't remove it from your tab:")
    recorded.assert_called_once()
    # Nothing left the snapshot, since nothing left the Sheet.
    assert _key("LlON") in {r.alliance for r in state.rows}


async def test_leaving_them_unpaired_clears_the_opponent(sheet, central):
    rows = _typo_league()
    fake = sheet(rows)
    state = _state(rows)
    paired = edit.pairings_with(state, _key("LlON"))

    assert await edit.unpair(state, LEAGUE, _key("LlON"), paired) == ""
    assert len(fake.updates.updates) == 4  # two cells on each of two rows
    assert all(r.opponent is None for r in state.rows if r.alliance == _key("A03"))


async def test_the_unpair_button_says_who_it_left_unpaired():
    state = _state(_typo_league())
    paired = edit.pairings_with(state, _key("LlON"))
    view = edit.RepairView(state, 7, _key("LlON"), LEAGUE, paired, "LlON")
    inter = _interaction()
    with patch.object(edit, "unpair", AsyncMock(return_value="")):
        await view._unpair(inter)
    assert inter.followup.send.call_args.args[0] == "✅ Left **A03** (weeks 1 and 2) unpaired."
