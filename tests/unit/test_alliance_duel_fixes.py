"""Unit tests for the after-save check and its fix buttons (#651).

Two things are worth pinning here. The routing: every finding a screen can fix
gets exactly one button, and a finding no screen can reach (an earlier league,
a match the daily score screen does not cover) gets none rather than a button
that writes into the wrong place. And the diff: after a save, only what that
save created is reported, and a failing check never costs the officer the save.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import alliance_duel as ad
import alliance_duel_entry as entry
import alliance_duel_fixes as fixes
import alliance_duel_hub as hub
import alliance_duel_league_edit as edit
import alliance_duel_results_builder as builder
import config_health

LEAGUE = ad.LeagueKey("S35", "Diamond", "12 - 2")
OLD_LEAGUE = ad.LeagueKey("S34", "Gold", "3 - 1")
MONDAY = ad.week_monday(ad.server_today())
OWN_TAG, WZ = "Fre3", "1234"


def _key(tag: str) -> ad.AllianceKey:
    return ad.AllianceKey.of(tag, WZ)


OWN = _key(OWN_TAG)


def _row(tag, week=1, ranking=None, **kw):
    return ad.AllianceWeek(
        league=LEAGUE,
        week=week,
        alliance=_key(tag),
        ranking=ranking,
        week_date=MONDAY,
        tag_display=tag,
        **kw,
    )


def _bracket():
    tags = [OWN_TAG] + [f"A{i:02d}" for i in range(2, ad.BRACKET_SIZE + 1)]
    return [_row(tag, ranking=r) for r, tag in enumerate(tags, start=1)]


def _state(rows, **cfg_kw):
    cfg = {
        "guild_id": 1,
        "enabled": 1,
        "tab_name": "Alliance Duel (VS)",
        "own_tag": OWN_TAG,
        "own_warzone": WZ,
        "tracking_mode": ad.MODE_FULL_BRACKET,
    }
    cfg.update(cfg_kw)
    return hub.HubState(1, cfg, rows)


def _finding(rule, *, week=1, day=None, tag=None, league=LEAGUE):
    return ad.Finding(
        rule=rule,
        severity=ad.SEVERITY_ERROR,
        message=f"rule {rule}",
        alliance=_key(tag) if tag else None,
        league=league,
        week=week,
        day=day,
        alliance_display=tag or "",
    )


@pytest.fixture(autouse=True)
def _no_recorded_sheet_problems(monkeypatch):
    monkeypatch.setattr(config_health, "problems_for_subjects", lambda *a, **k: [])


# ── Which screen fixes what ───────────────────────────────────────────────────


def _labels(state, findings):
    return [t.label for t in fixes.fix_targets(state, findings)]


@pytest.mark.parametrize("rule", [1, 2, 3, 4])
def test_scores_outcomes_and_pairings_open_that_weeks_results(rule):
    state = _state(_bracket())
    assert _labels(state, [_finding(rule, week=2, tag="A03")]) == ["Enter week 2 results"]


def test_one_button_per_screen_not_per_finding():
    state = _state(_bracket())
    findings = [_finding(1, week=2, tag="A03"), _finding(4, week=2, tag="A05")]
    assert _labels(state, findings) == ["Enter week 2 results"]


def test_a_day_score_opens_the_daily_score_on_that_day():
    state = _state(_bracket())
    assert _labels(state, [_finding(8, week=1, day=3, tag=OWN_TAG)]) == [
        "✏️ Enter day 3 score (week 1)"
    ]


def test_a_day_score_on_our_opponent_opens_it_too():
    rows = _bracket()
    rows[0].opponent = _key("A09")
    state = _state(rows)
    assert _labels(state, [_finding(8, week=1, day=2, tag="A09")]) == [
        "✏️ Enter day 2 score (week 1)"
    ]


def test_a_day_score_the_daily_screen_cannot_reach_gets_no_button():
    # The daily score screen writes our row and our opponent's, nobody else's.
    state = _state(_bracket())
    assert _labels(state, [_finding(8, week=1, day=2, tag="A05")]) == []


def test_a_rank_opens_that_alliance_on_that_week():
    state = _state(_bracket())
    assert _labels(state, [_finding(5, week=1, tag="A03")]) == ["🔢 Set A03's rank (week 1)"]


def test_contradicting_predictions_open_that_weeks_predictions():
    state = _state(_bracket())
    assert _labels(state, [_finding(7, week=3, tag="A03")]) == ["Enter predictions for week 3"]


def test_a_missing_own_alliance_opens_add_or_edit():
    state = _state(_bracket())
    assert _labels(state, [_finding(6, week=None, tag=OWN_TAG)]) == ["➕ Add or edit alliance"]


def test_too_many_alliances_opens_remove():
    state = _state(_bracket())
    assert _labels(state, [_finding(9, week=None)]) == [edit.VS_BTN_REMOVE_ALLIANCE]


def test_an_earlier_league_gets_no_button():
    # Every entry screen writes into the current league, and the labels do
    # not name one, so a button here would write into the wrong league.
    state = _state(_bracket())
    assert _labels(state, [_finding(1, week=2, tag="A03", league=OLD_LEAGUE)]) == []


# ── Each button opens its screen ──────────────────────────────────────────────


def _interaction():
    inter = MagicMock()
    inter.user.id = 7
    inter.response.send_modal = AsyncMock()
    inter.response.send_message = AsyncMock()
    inter.original_response = AsyncMock(return_value=MagicMock())
    return inter


async def test_the_results_button_opens_that_weeks_builder():
    inter = _interaction()
    await fixes.open_fix(inter, _state(_bracket()), fixes.FixTarget("results", "x", week=1))
    view = inter.response.send_message.call_args.kwargs["view"]
    assert isinstance(view, builder.ResultsBuilderView)
    assert view.week == 1


async def test_the_score_button_opens_the_daily_score_on_its_day():
    inter = _interaction()
    target = fixes.FixTarget("score", "x", week=1, day=4)
    await fixes.open_fix(inter, _state(_bracket()), target)
    modal = inter.response.send_modal.call_args.args[0]
    assert isinstance(modal, entry.ScoreModal)
    assert (modal.week, modal.day) == (1, 4)


async def test_the_rank_button_opens_that_alliances_rank():
    inter = _interaction()
    target = fixes.FixTarget("rank", "x", week=1, alliance=_key("A03"))
    await fixes.open_fix(inter, _state(_bracket()), target)
    modal = inter.response.send_modal.call_args.args[0]
    assert isinstance(modal, entry.AllianceRankModal)
    assert modal.alliance == _key("A03")


async def test_the_predictions_button_opens_that_week():
    inter = _interaction()
    await fixes.open_fix(inter, _state(_bracket()), fixes.FixTarget("predictions", "x", week=2))
    view = inter.response.send_message.call_args.kwargs["view"]
    assert isinstance(view, entry.PredictionsView)
    assert view.week == 2


async def test_the_add_button_opens_add_or_edit_alliance():
    inter = _interaction()
    await fixes.open_fix(inter, _state(_bracket()), fixes.FixTarget("add", "x"))
    assert isinstance(inter.response.send_modal.call_args.args[0], entry.AllianceModal)


async def test_the_remove_button_opens_the_remove_picker():
    inter = _interaction()
    with patch("alliance_duel_league_edit.open_remove_picker", new=AsyncMock()) as opened:
        await fixes.open_fix(inter, _state(_bracket()), fixes.FixTarget("remove", "x"))
    opened.assert_awaited_once()


# ── After a save ──────────────────────────────────────────────────────────────


def test_the_after_save_message_uses_the_signed_off_wording():
    finding = _finding(1, week=2, tag="A03")
    embed = fixes.after_save_embed([finding])
    assert embed.title == "⚠️ Something in that save doesn't add up"
    assert embed.description == (
        "Your save went through. If this is a mistake, the button below opens the screen "
        "that fixes it.\n\n⚠️ **Week 2, A03**: rule 1"
    )


async def test_nothing_is_posted_when_the_save_created_nothing():
    state = _state(_bracket())
    state.new_findings = []
    inter = _interaction()
    inter.followup.send = AsyncMock()
    await fixes.send_new_findings(inter, state)
    inter.followup.send.assert_not_awaited()


async def test_new_findings_are_posted_with_their_buttons_once():
    state = _state(_bracket())
    state.new_findings = [_finding(1, week=1, tag="A03")]
    inter = _interaction()
    inter.followup.send = AsyncMock(return_value=MagicMock())
    await fixes.send_new_findings(inter, state)

    kwargs = inter.followup.send.call_args.kwargs
    assert kwargs["ephemeral"] is True
    assert [c.label for c in kwargs["view"].children] == ["Enter week 1 results"]
    # Consumed: a second screen reusing the state does not repeat it.
    assert state.new_findings == []


async def test_a_finding_with_no_screen_is_posted_without_buttons():
    state = _state(_bracket())
    state.new_findings = [_finding(1, week=1, tag="A03", league=OLD_LEAGUE)]
    inter = _interaction()
    inter.followup.send = AsyncMock()
    await fixes.send_new_findings(inter, state)
    assert "view" not in inter.followup.send.call_args.kwargs


async def test_a_failing_follow_up_never_raises_into_a_confirmed_save(caplog):
    state = _state(_bracket())
    state.new_findings = [_finding(1, week=1, tag="A03")]
    inter = _interaction()
    inter.followup.send = AsyncMock(side_effect=RuntimeError("discord down"))
    await fixes.send_new_findings(inter, state)
    assert "after-save check failed" in caplog.text


# ── save_rows keeps only what the save created ────────────────────────────────


@pytest.fixture
def _sheet_takes_it(monkeypatch):
    """Let `save_rows` reach the end without a Google Sheet or central store."""
    import config as _config

    class _Plan:
        unmapped_columns = ()

    class _Sheet:
        def get_all_values(self):
            return []

    monkeypatch.setattr(_config, "get_spreadsheet", lambda gid: object())
    monkeypatch.setattr(entry.ad_setup, "ensure_tab", lambda *a, **k: _Sheet())
    monkeypatch.setattr(entry.ad, "plan_upsert", lambda *a, **k: _Plan())
    monkeypatch.setattr(entry.ad, "apply_upsert", lambda *a, **k: None)
    monkeypatch.setattr(entry, "_mirror_centrally", AsyncMock())


def _paired_bracket(ours=None, theirs=5):
    rows = _bracket()
    a02, a03 = rows[1], rows[2]
    a02.opponent, a03.opponent = a03.alliance, a02.alliance
    a02.week_score, a03.week_score = ours, theirs
    return rows


async def test_a_save_that_breaks_a_match_reports_it(_sheet_takes_it):
    state = _state(_paired_bracket(ours=None, theirs=5))
    row = entry._row_for_write(state, _key("A02"), 1)
    row.week_score = 9  # 9 + 5 is 14, not 13

    assert await entry.save_rows(state, [row]) == ""
    assert [f.rule for f in state.new_findings] == [1]


async def test_a_problem_that_was_already_there_is_not_repeated(_sheet_takes_it):
    state = _state(_paired_bracket(ours=9, theirs=5))
    row = entry._row_for_write(state, _key("A02"), 1)
    row.power = 300_000_000  # nothing to do with the bad pair

    assert await entry.save_rows(state, [row]) == ""
    assert state.new_findings == []


async def test_a_failing_check_never_costs_the_save(_sheet_takes_it, caplog):
    state = _state(_bracket())
    row = entry._row_for_write(state, _key("A02"), 1)
    row.power = 300_000_000
    with patch.object(fixes, "state_findings", side_effect=RuntimeError("bug")):
        assert await entry.save_rows(state, [row]) == ""
    assert state.new_findings == []
    assert state.profiles[_key("A02")].power == 300_000_000
    assert "check before save failed" in caplog.text


# ── The screens post it under their own confirmation ──────────────────────────


async def test_a_rank_save_posts_what_it_created_after_confirming():
    state = _state(_bracket())
    modal = entry.AllianceRankModal(state, 1, _key("A03"))
    modal.rank._value = "4"
    order = []
    inter = _interaction()
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock(side_effect=lambda *a, **k: order.append("confirm"))
    with (
        patch.object(entry, "save_rows", AsyncMock(return_value="")),
        patch.object(
            fixes, "send_new_findings", AsyncMock(side_effect=lambda *a: order.append("check"))
        ),
    ):
        await modal.on_submit(inter)
    assert order == ["confirm", "check"]


async def test_a_failed_save_posts_no_check():
    state = _state(_bracket())
    modal = entry.AllianceRankModal(state, 1, _key("A03"))
    modal.rank._value = "4"
    inter = _interaction()
    inter.response.defer = AsyncMock()
    inter.followup.send = AsyncMock()
    with (
        patch.object(entry, "save_rows", AsyncMock(return_value="tab gone")),
        patch.object(fixes, "send_new_findings", AsyncMock()) as check,
    ):
        await modal.on_submit(inter)
    check.assert_not_awaited()


def test_every_interactive_save_is_followed_by_the_check():
    """A source check, so a new entry screen cannot quietly skip it.

    Every `save_rows(..., actor=interaction)` in the entry screens is a person
    saving something, and each must hand what it created to the check. The
    notes and Known-read modals write nothing a check reads, so they are the
    named exceptions.
    """
    import inspect
    import re

    exempt = {"AllianceDetailsModal", "KnownModal", "DeclarationView"}
    missing = []
    for module in (entry, builder):
        for name, cls in inspect.getmembers(module, inspect.isclass):
            if cls.__module__ != module.__name__ or name in exempt:
                continue
            source = inspect.getsource(cls)
            for chunk in re.split(r"\n    (?:async )?def ", source):
                if "save_rows(" in chunk and "actor=interaction" in chunk:
                    if "send_new_findings" not in chunk:
                        missing.append(f"{module.__name__}.{name}")
    assert missing == []
