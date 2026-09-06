"""Unit tests for the push / save week declaration (#407).

Three things this has to get right, and they are the three the ticket says
matter more than the feature itself:

- **A save in week 1 is not a neutral resource decision.** Week 1 outweighs
  every later week combined, so its winners and losers separate permanently.
  The surface has to answer "who do we end up facing?" outright rather than
  warning vaguely.
- **A hypothetical never renders as a projection.** The assumed outcome carries
  its own source so it cannot be mistaken for evidence.
- **Intent partitions the accuracy sample rather than filtering it.** An
  alliance can declare a push and still lose, and that case is the most
  valuable row in the set, not an excluded one.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import alliance_duel as ad
import alliance_duel_entry as entry
import alliance_duel_hub as hub
import config_health


LEAGUE = ad.LeagueKey("S35", "Diamond", "12 - 2")
# Anchored to the current duel week, not pinned: the code under test resolves
# the live week against the real clock, so an absolute Monday quietly stops
# being live — this one did, on Monday 2026-08-17. Server time rather than
# `date.today()`: the two disagree for a couple of hours around every UTC-2
# rollover, and on the Sunday/Monday one that disagreement is a whole week,
# because `week_monday` sends Sunday back rather than forward.
MONDAY = ad.week_monday(ad.server_today())
OWN_TAG, OWN_WZ = "US", "1234"
OWN = ad.AllianceKey.of(OWN_TAG, OWN_WZ)


def _key(tag: str) -> ad.AllianceKey:
    return ad.AllianceKey.of(tag, OWN_WZ)


def _row(tag, week=1, ranking=None, week_date=MONDAY, **kw):
    return ad.AllianceWeek(
        league=LEAGUE,
        week=week,
        alliance=_key(tag),
        ranking=ranking,
        week_date=week_date,
        tag_display=tag,
        **kw,
    )


def _bracket(week=1, **per_alliance):
    tags = [OWN_TAG] + [f"A{i:02d}" for i in range(2, ad.BRACKET_SIZE + 1)]
    return [
        _row(tag, week=week, ranking=ranking, **per_alliance.get(tag, {}))
        for ranking, tag in enumerate(tags, start=1)
    ]


def _week_one_played_around_us(rows):
    """Record every week-1 result except our own, higher ranking winning.

    Our own match is left open on purpose: that is the state an alliance is
    actually in when it asks "what happens if we save this week?", and it is
    the only state where the answer is more than "not worked out yet".
    """
    pairing = ad.compute_week_pairing(rows, 1)
    assert isinstance(pairing, ad.WeekPairing), pairing
    by_key = {r.alliance: r for r in rows if r.week == 1}
    rankings = {r.alliance: r.ranking for r in rows if r.week == 1}
    for match in pairing.matches:
        if OWN in (match.a, match.b):
            by_key[match.a].opponent = match.b
            by_key[match.b].opponent = match.a
            continue
        winner = match.a if rankings[match.a] < rankings[match.b] else match.b
        loser = match.other(winner)
        by_key[winner].opponent, by_key[loser].opponent = loser, winner
        by_key[winner].week_outcome, by_key[loser].week_outcome = "W", "L"
    return rows


def _state(rows, **cfg_over):
    cfg = {
        "guild_id": 1,
        "enabled": 1,
        "tab_name": "Alliance Duel (VS)",
        "own_tag": OWN_TAG,
        "own_warzone": OWN_WZ,
        "tracking_mode": ad.MODE_FULL_BRACKET,
        "day_theme_channel_id": 999,
    }
    cfg.update(cfg_over)
    return hub.HubState(1, cfg, rows)


def _text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{f.name}\n{f.value}" for f in embed.fields]
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


@pytest.fixture(autouse=True)
def _no_config_health_db(monkeypatch):
    monkeypatch.setattr(config_health, "problems_for_subjects", lambda *a, **k: [])


# ── The assumed-outcome walk ──────────────────────────────────────────────────


def test_saving_and_winning_lead_to_different_opponents():
    """The two branches of the question, against the same bracket. Without the
    assumption there is no week-2 step at all here, because our own week 1 is
    still open: that unresolved match is exactly what the declaration is
    about."""
    rows = _week_one_played_around_us(_bracket())
    won = ad.project_own_path(OWN, rows, assume={1: (OWN, "W")})
    saved = ad.project_own_path(OWN, rows, assume={1: (OWN, "L")})
    assert not isinstance(won, ad.BracketIncomplete)
    assert not isinstance(saved, ad.BracketIncomplete)

    won_week2 = next(s for s in won.steps if s.week == 2)
    saved_week2 = next(s for s in saved.steps if s.week == 2)
    assert won_week2.opponent != saved_week2.opponent


def test_without_an_assumption_the_open_week_simply_blocks():
    rows = _week_one_played_around_us(_bracket())
    plain = ad.project_own_path(OWN, rows)
    assert not isinstance(plain, ad.BracketIncomplete)
    assert plain.is_blocked
    assert not [s for s in plain.steps if s.week == 2 and s.opponent]


def test_the_assumption_outranks_a_recorded_result():
    """The question is explicitly counterfactual: the caller knows what the
    sheet says and is asking what the other branch looks like."""
    rows = _bracket()
    for row in rows:
        if row.alliance == OWN:
            row.week_outcome = "W"
            row.opponent = _key("A16")
    saved = ad.project_own_path(OWN, rows, assume={1: (OWN, "L")})
    step = next(s for s in saved.steps if s.week == 1)
    assert step.outcome == "L"
    assert step.outcome_source == ad.SOURCE_ASSUMED


def test_a_hypothetical_never_leaks_into_an_ordinary_projection():
    projection = ad.project_own_path(OWN, _bracket())
    sources = {s.outcome_source for s in projection.steps} | {s.source for s in projection.steps}
    assert ad.SOURCE_ASSUMED not in sources


def test_the_assumption_only_applies_to_the_alliance_it_names():
    """A save is our own call. It must not silently decide anyone else's week."""
    rows = _bracket()
    other = _key("A05")
    saved = ad.project_own_path(other, rows, assume={1: (OWN, "L")})
    plain = ad.project_own_path(other, rows)
    assert [s.opponent for s in saved.steps][:1] == [s.opponent for s in plain.steps][:1]


# ── What the declaration surface says ─────────────────────────────────────────


def test_a_week_one_save_is_named_as_permanent():
    text = _text(entry.declaration_embed(_state(_bracket()), 1))
    assert "never meet again" in text or "does not come back" in text


def test_a_later_week_save_is_not_dressed_up_as_permanent():
    rows = _bracket() + _bracket(week=2)
    text = _text(entry.declaration_embed(_state(rows), 2))
    assert "never meet again" not in text


def test_the_save_consequence_names_actual_opponents():
    """ "You would then face X" is the whole point: the ticket asks for the
    answer rather than for leadership to infer it from a warning."""
    rows = _week_one_played_around_us(_bracket())
    text = _text(entry.declaration_embed(_state(rows), 1))
    assert "You would then face" in text


def test_an_existing_declaration_is_read_back():
    rows = _bracket(US={"intent": ad.INTENT_SAVE})
    text = _text(entry.declaration_embed(_state(rows), 1))
    assert "saving for a later week" in text


def test_the_surface_says_nothing_is_announced_without_asking():
    text = _text(entry.declaration_embed(_state(_bracket()), 1))
    assert "unless you ask" in text


def test_own_alliance_mode_shows_no_consequence_rather_than_a_second_upsell():
    rows = [_row(OWN_TAG, ranking=1)]
    embed = entry.declaration_embed(_state(rows, tracking_mode=ad.MODE_OWN_ALLIANCE), 1)
    assert not any(f.name == "If you save this week" for f in embed.fields)


def test_the_declaration_surface_carries_no_em_dashes():
    assert "—" not in _text(entry.declaration_embed(_state(_bracket()), 1))


# ── The buttons ───────────────────────────────────────────────────────────────


def test_neither_call_is_presented_as_the_recommended_one():
    """The bot has no opinion about whether an alliance should spend or bank."""
    view = entry.DeclarationView(_state(_bracket()), 1, owner_id=7)
    assert not [c for c in view.children if c.style is discord.ButtonStyle.primary]
    assert not [c for c in view.children if c.style is discord.ButtonStyle.success]


def test_the_declaration_already_recorded_is_disabled():
    rows = _bracket(US={"intent": ad.INTENT_PUSH})
    view = entry.DeclarationView(_state(rows), 1, owner_id=7)
    push = next(c for c in view.children if c.label == entry.VS_BTN_PUSH)
    save = next(c for c in view.children if c.label == entry.VS_BTN_SAVE)
    assert push.disabled is True
    assert save.disabled is False


def test_clear_is_dead_until_there_is_something_to_clear():
    view = entry.DeclarationView(_state(_bracket()), 1, owner_id=7)
    clear = next(c for c in view.children if c.label == entry.VS_BTN_CLEAR_INTENT)
    assert clear.disabled is True


def test_the_announce_button_is_absent_with_nothing_to_announce():
    view = entry.DeclarationView(_state(_bracket()), 1, owner_id=7)
    assert entry.VS_BTN_ANNOUNCE not in {c.label for c in view.children}


def test_the_announce_button_is_absent_without_a_members_channel():
    """A control that could not post anywhere is a control that cannot change
    anything."""
    rows = _bracket(US={"intent": ad.INTENT_SAVE})
    view = entry.DeclarationView(_state(rows, day_theme_channel_id=0), 1, owner_id=7)
    assert entry.VS_BTN_ANNOUNCE not in {c.label for c in view.children}


def test_the_announce_button_appears_once_both_exist():
    rows = _bracket(US={"intent": ad.INTENT_SAVE})
    view = entry.DeclarationView(_state(rows), 1, owner_id=7)
    assert entry.VS_BTN_ANNOUNCE in {c.label for c in view.children}


async def test_declaring_writes_only_the_intent():
    """Non-clobbering, like every other write in this module: the row carries
    identity plus the one field the officer actually set."""
    state = _state(_bracket())
    captured = {}

    async def _fake_save(_state, rows, **kw):
        captured["rows"] = rows
        return ""

    view = entry.DeclarationView(state, 1, owner_id=7)
    interaction = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    view.message = None

    with patch.object(entry, "save_rows", _fake_save):
        await view._make_declare(ad.INTENT_SAVE)(interaction)

    written = captured["rows"][0]
    assert written.intent == ad.INTENT_SAVE
    assert written.day_scores == {}
    assert written.power is None
    interaction.response.defer.assert_awaited_once()


# ── The member announcement ───────────────────────────────────────────────────


def test_the_announcement_tells_members_what_to_do_with_their_own_resources():
    save = _text(entry.announcement_embed(2, ad.INTENT_SAVE))
    push = _text(entry.announcement_embed(2, ad.INTENT_PUSH))
    assert "Bank" in save
    assert "Spend" in push


def test_the_announcement_carries_no_jargon_a_member_would_not_know():
    for intent in (ad.INTENT_PUSH, ad.INTENT_SAVE):
        text = _text(entry.announcement_embed(2, intent)).lower()
        for jargon in ("cohort", "bracket", "ranking", "projection", "intent"):
            assert jargon not in text


def test_the_announcement_carries_no_em_dashes():
    for intent in (ad.INTENT_PUSH, ad.INTENT_SAVE):
        assert "—" not in _text(entry.announcement_embed(2, intent))


# ── The accuracy partition ────────────────────────────────────────────────────


def _week(week, intent, outcome):
    return _row(OWN_TAG, week=week, intent=intent, week_outcome=outcome)


def test_a_declared_push_that_lost_is_the_cleanest_signal_not_an_exclusion():
    part = ad.partition_by_intent([_week(1, ad.INTENT_PUSH, "L")])
    assert part.declared_push and not part.excluded
    assert len(part.sample) == 1


def test_a_declared_save_comes_out_of_the_accuracy_number():
    part = ad.partition_by_intent([_week(1, ad.INTENT_SAVE, "L")])
    assert part.sample == ()
    assert len(part.excluded) == 1


def test_saving_and_winning_anyway_is_surfaced_rather_than_only_dropped():
    """It means the opponent was far weaker than modelled, or the save was
    never really executed. Either way it says something."""
    part = ad.partition_by_intent([_week(1, ad.INTENT_SAVE, "W")])
    assert len(part.saved_and_won) == 1
    assert part.saved_and_won[0] in part.excluded


def test_undeclared_weeks_count_but_are_tracked_as_an_assumption():
    part = ad.partition_by_intent(
        [_week(1, None, "W"), _week(2, ad.INTENT_PUSH, "W"), _week(3, None, "L")]
    )
    assert len(part.sample) == 3
    assert part.rests_on_assumption == 2


def test_a_week_that_has_not_happened_is_not_evidence():
    part = ad.partition_by_intent([_week(1, ad.INTENT_PUSH, None)])
    assert part.sample == ()
    assert part.excluded == ()


def test_the_sample_comes_back_in_week_order():
    part = ad.partition_by_intent(
        [_week(3, ad.INTENT_PUSH, "W"), _week(1, None, "L"), _week(2, ad.INTENT_PUSH, "W")]
    )
    assert [r.week for r in part.sample] == [1, 2, 3]
