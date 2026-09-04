"""Unit tests for the `/vs` hub and scout profile (#402).

The embed builders are pure functions over a `HubState`, so everything here
runs against literal rows with no Discord gateway, no interaction and no
Sheets call. `HubState` takes an already-loaded row list precisely so this is
possible, and so the sheet is read once per invocation rather than per view.

Two things these guard beyond rendering:

- **The read-quota rule.** #269 had storm screens blowing the Sheets read
  limit on quick click-through. A test asserts no button callback reaches for
  the sheet.
- **What the copy promises.** A blank cell must not render as a zero, an
  unassessed matchup must not read like a call, and history has to keep the
  tier each meeting happened in.
"""

import datetime as _dt

import discord
import pytest

import alliance_duel as ad
import alliance_duel_hub as hub
import alliance_duel_ui as ad_ui
import config_health


LEAGUE = ad.LeagueKey("S35", "Diamond", "12 - 2")
OLD_LEAGUE = ad.LeagueKey("S34", "Gold", "9 - 1")
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


def _cfg(**kw):
    base = {
        "guild_id": 1,
        "enabled": 1,
        "tab_name": "Alliance Duel (VS)",
        "own_tag": OWN_TAG,
        "own_warzone": OWN_WZ,
        "tracking_mode": ad.MODE_FULL_BRACKET,
    }
    base.update(kw)
    return base


def _row(tag, week=1, ranking=None, league=LEAGUE, week_date=MONDAY, **kw):
    return ad.AllianceWeek(
        league=league,
        week=week,
        alliance=_key(tag),
        ranking=ranking,
        week_date=week_date,
        tag_display=tag,
        **kw,
    )


def _bracket_rows(week=1, **per_alliance):
    """Sixteen ranked alliances for one week, own alliance at ranking 1."""
    tags = [OWN_TAG] + [f"A{i:02d}" for i in range(2, ad.BRACKET_SIZE + 1)]
    rows = []
    for ranking, tag in enumerate(tags, start=1):
        rows.append(_row(tag, week=week, ranking=ranking, **per_alliance.get(tag, {})))
    return rows


def _state(rows, **cfg_kw):
    return hub.HubState(1, _cfg(**cfg_kw), rows)


@pytest.fixture(autouse=True)
def _no_recorded_sheet_problems(monkeypatch):
    """The hub embed asks config_health whether the tab is currently broken,
    which is a DB read. Stubbed to "nothing wrong" for the whole module so
    every other test asserts on copy rather than on fixture state; the one
    test that wants a problem sets its own."""
    monkeypatch.setattr(config_health, "problems_for_subjects", lambda *a, **k: [])


def _problem(detail: str) -> config_health.Problem:
    return config_health.Problem(
        guild_id=1,
        subject=hub.ad_setup.VS_SHEET_SUBJECT,
        kind="TAB_MISSING",
        signature="sig",
        detail=detail,
        first_seen_at="2026-08-12T00:00:00",
        notified_at=None,
        resolved_at=None,
    )


def _text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{f.name}\n{f.value}" for f in embed.fields]
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


# ── Loaded state ──────────────────────────────────────────────────────────────


def test_state_resolves_the_live_week_from_the_sheet_dates():
    state = _state(_bracket_rows())
    assert state.league == LEAGUE
    assert state.week == 1
    assert state.own == OWN


def test_state_falls_back_to_the_latest_league_between_seasons():
    # A sheet whose last league finished months ago still has something to show.
    rows = _bracket_rows(week=1)
    for row in rows:
        row.week_date = _dt.date(2026, 3, 2)
    state = _state(rows)
    assert state.week is None
    assert state.league == LEAGUE


def test_an_unset_own_alliance_is_a_state_not_a_crash():
    state = _state(_bracket_rows(), own_tag="", own_warzone="")
    assert state.own is None
    assert "which alliance is yours" in _text(hub.hub_embed(state))


# ── Hub embed ─────────────────────────────────────────────────────────────────


def test_the_hub_leads_with_the_matchup_someone_opened_it_for():
    rows = _bracket_rows()
    rows[0].opponent = _key("A02")
    state = _state(rows)
    text = _text(hub.hub_embed(state))
    assert "US" in text and "A02" in text
    assert "S35" in text and "Diamond" in text


def test_the_hub_shows_the_running_split_and_what_clinches_it():
    rows = _bracket_rows()
    rows[0].opponent = _key("A02")
    rows[0].day_outcomes = {1: "W", 2: "W", 3: "L", 4: "W"}
    text = _text(hub.hub_embed(_state(rows)))
    assert "**5-2**" in text
    # The single most actionable mid-week sentence: which day ends it.
    assert "clinches it" in text
    assert "day 5 (2 pts)" in text


def test_an_empty_tab_says_what_to_do_rather_than_rendering_nothing():
    text = _text(hub.hub_embed(_state([])))
    assert "no league in it yet" in text
    assert hub.VS_BTN_SETUP in text


def test_a_broken_tab_is_visible_on_the_hub_not_only_in_the_channel_notice(monkeypatch):
    """The leadership notice can be scrolled past, and the hub is where someone
    goes to ask "is this working?" (the #413 reasoning, generalised in
    #414/#379)."""
    monkeypatch.setattr(
        config_health,
        "problems_for_subjects",
        lambda *a, **k: [_problem("I couldn't find a tab called Alliance Duel (VS).")],
    )
    embed = hub.hub_embed(_state(_bracket_rows()))
    text = _text(embed)
    assert "I can't read" in text
    assert "last good read" in text
    assert embed.color == discord.Color.red()


def test_own_alliance_mode_is_reported_as_a_choice_not_a_gap():
    state = _state(_bracket_rows(), tracking_mode=ad.MODE_OWN_ALLIANCE)
    text = _text(hub.hub_embed(state))
    assert "just your alliance" in text
    assert "widen" in text
    for alarm in ("error", "missing", "incomplete", "⚠️"):
        assert alarm not in text.lower()


# ── Bracket view ──────────────────────────────────────────────────────────────


def test_the_bracket_renders_a_blank_cell_as_unknown_never_as_zero():
    rows = _bracket_rows(**{OWN_TAG: {"power": 400_000_000, "members": 92, "gift_level": 18}})
    text = _text(hub.bracket_embed(_state(rows), 1))
    assert "400M" in text
    assert "92 members" in text
    # Fifteen alliances have nothing recorded, and none of them may read as 0.
    assert f"{hub.NOT_ENTERED} members" in text
    assert "0 members" not in text
    assert "means not entered" in text


def test_the_bracket_is_ordered_by_ranking_like_the_in_game_screen():
    rows = _bracket_rows()
    text = _text(hub.bracket_embed(_state(rows), 1))
    positions = [text.index(tag) for tag in (OWN_TAG, "A02", "A03")]
    assert positions == sorted(positions)


def test_the_bracket_marks_which_row_is_yours():
    text = _text(hub.bracket_embed(_state(_bracket_rows()), 1))
    assert "⬅️" in text


# ── Week view ─────────────────────────────────────────────────────────────────


def test_the_week_view_puts_your_own_matchup_first():
    rows = _bracket_rows()
    text = _text(hub.week_embed(_state(rows), 1))
    assert text.index("US") < text.index("A03")


def test_a_confirmed_result_outranks_every_projection():
    rows = _bracket_rows(
        **{
            OWN_TAG: {"power": 400_000_000, "members": 92, "gift_level": 18},
            "A02": {"power": 100_000_000, "members": 40, "gift_level": 4},
        }
    )
    rows[0].opponent = _key("A02")
    rows[0].week_outcome = "L"
    rows[0].week_score = 5
    text = _text(hub.week_embed(_state(rows), 1))
    # The stats say we walk it; the recorded result says we lost. Result wins.
    assert "✅ A02 took it (5-8)" in text
    assert "Estimated: US" not in text


def test_an_unassessed_matchup_never_reads_like_a_call():
    text = _text(hub.week_embed(_state(_bracket_rows()), 1))
    assert "Not assessed" in text
    for word in ("favored", "easy", "likely"):
        assert word not in text.lower()


def test_a_projected_matchup_names_the_evidence_it_rests_on():
    rows = _bracket_rows(
        **{
            OWN_TAG: {"power": 400_000_000, "members": 92, "gift_level": 22},
            "A02": {"power": 200_000_000, "members": 60, "gift_level": 10},
        }
    )
    text = _text(hub.week_embed(_state(rows), 1))
    assert "Estimated: US favored" in text


def test_own_alliance_mode_still_shows_the_matchup_it_recorded():
    """No bracket to pair, but the guild typed who they faced. Refusing to
    show that would be the tracker arguing with a deliberate choice (#448)."""
    rows = [_row(OWN_TAG, ranking=1, opponent=_key("A02")), _row("A02", ranking=2)]
    state = _state(rows, tracking_mode=ad.MODE_OWN_ALLIANCE)
    text = _text(hub.week_embed(state, 1))
    assert "US" in text and "A02" in text


# ── Scout profile ─────────────────────────────────────────────────────────────


def test_the_scout_profile_orders_facts_before_inference():
    rows = _bracket_rows(
        **{
            OWN_TAG: {"power": 400_000_000, "members": 92, "gift_level": 22},
            "A02": {"power": 200_000_000, "members": 60, "gift_level": 10},
        }
    )
    embed = ad_ui.scout_embed(_state(rows), _key("A02"))
    names = [f.name for f in embed.fields]
    assert names.index("Recorded") < names.index("Head to head") < names.index("Projection")


def test_the_scout_projection_carries_the_capacity_ceiling_caveat():
    rows = _bracket_rows(
        **{
            OWN_TAG: {"power": 400_000_000, "members": 92, "gift_level": 22},
            "A02": {"power": 200_000_000, "members": 60, "gift_level": 10},
        }
    )
    embed = ad_ui.scout_embed(_state(rows), _key("A02"))
    projection = next(f.value for f in embed.fields if f.name == "Projection")
    assert "capacity ceiling" in projection
    assert "Duel tech" in projection


def test_a_tier_0_alliance_says_what_is_missing_rather_than_guessing():
    rows = _bracket_rows(**{OWN_TAG: {"power": 400_000_000, "members": 92, "gift_level": 22}})
    embed = ad_ui.scout_embed(_state(rows), _key("A02"))
    recorded = next(f.value for f in embed.fields if f.name == "Recorded")
    assert "Power, members and gift level are all needed" in recorded


def test_the_profile_says_how_old_its_numbers_are():
    rows = _bracket_rows(**{"A02": {"power": 200_000_000, "members": 60, "gift_level": 10}})
    embed = ad_ui.scout_embed(_state(rows), _key("A02"))
    recorded = next(f.value for f in embed.fields if f.name == "Recorded")
    assert "Last updated" in recorded and "day" in recorded


def test_power_trajectory_is_reported_as_an_observation():
    rows = _bracket_rows(**{"A02": {"power": 200_000_000, "members": 60, "gift_level": 10}})
    rows.append(
        _row("A02", week=2, ranking=2, week_date=MONDAY + _dt.timedelta(days=28), power=260_000_000)
    )
    embed = ad_ui.scout_embed(_state(rows), _key("A02"))
    recorded = next(f.value for f in embed.fields if f.name == "Recorded")
    assert "Power up 30%" in recorded
    # An observation, never a verdict about how active they are.
    for verdict in ("active", "dying", "strong alliance"):
        assert verdict not in recorded.lower()


# ── Head to head ──────────────────────────────────────────────────────────────


def test_head_to_head_recovers_meetings_from_either_side_of_the_pairing():
    rows = [
        _row(OWN_TAG, ranking=1, opponent=_key("A02"), week_outcome="W", week_score=8),
        _row("A02", ranking=2),  # their row never got its Opponent filled in
    ]
    history = ad.head_to_head(rows, OWN, _key("A02"))
    assert len(history.meetings) == 1
    assert history.record == "1-0"
    assert history.meetings[0].score == (8, 5)


def test_head_to_head_never_counts_an_unrecorded_week_as_a_loss():
    rows = [_row(OWN_TAG, ranking=1, opponent=_key("A02"))]
    history = ad.head_to_head(rows, OWN, _key("A02"))
    assert history.record == "0-0"
    assert history.unrecorded == 1


def test_head_to_head_is_newest_first():
    rows = [
        _row(OWN_TAG, league=OLD_LEAGUE, week_date=_dt.date(2026, 5, 4), opponent=_key("A02")),
        _row(OWN_TAG, opponent=_key("A02")),
    ]
    history = ad.head_to_head(rows, OWN, _key("A02"))
    assert [m.league.season for m in history.meetings] == ["S35", "S34"]


@pytest.mark.parametrize(
    "previous_tier,current_tier,expected",
    [("Silver", "Diamond", "promoted"), ("Diamond", "Gold", "relegated")],
)
def test_tier_movement_between_meetings_is_surfaced(previous_tier, current_tier, expected):
    """Game-adjudicated, so harder evidence than any proxy this tracker
    computes, and worth saying out loud rather than averaging away."""
    old = ad.LeagueKey("S34", previous_tier, "9 - 1")
    history = ad.HeadToHead(
        OWN,
        _key("A02"),
        (
            ad.Meeting(
                old, 1, _dt.date(2026, 5, 4), _row(OWN_TAG, league=old, opponent=_key("A02"))
            ),
        ),
    )
    movement = history.tier_movement(current_tier)
    assert movement is not None
    assert movement[0] == previous_tier
    assert (movement[1] > 0) == (expected == "promoted")


def test_an_unrecognised_tier_is_not_silently_ranked():
    old = ad.LeagueKey("S34", "Bronze", "9 - 1")
    history = ad.HeadToHead(
        OWN,
        _key("A02"),
        (ad.Meeting(old, 1, MONDAY, _row(OWN_TAG, league=old, opponent=_key("A02"))),),
    )
    assert history.tier_movement("Diamond") is None


def test_the_history_block_keeps_the_tier_on_every_meeting():
    rows = [
        _row(
            OWN_TAG,
            league=OLD_LEAGUE,
            week_date=_dt.date(2026, 5, 4),
            opponent=_key("A02"),
            week_outcome="L",
            week_score=4,
        ),
        _row(OWN_TAG, ranking=1, opponent=_key("A02"), week_outcome="W", week_score=9),
        _row("A02", ranking=2),
    ]
    embed = ad_ui.scout_embed(_state(rows), _key("A02"))
    block = next(f.value for f in embed.fields if f.name == "Head to head")
    assert "**1-1**" in block
    # Each meeting keeps the tier it was earned in rather than being averaged
    # into one record: a win a tier down is weaker evidence about today.
    assert "S34 Gold" in block and "S35 Diamond" in block


def test_tier_movement_is_only_claimed_when_something_actually_moved():
    """ "Since you last met" means exactly that. Having already met them inside
    the current bracket, nothing has moved, and saying otherwise would invent
    a promotion out of an older meeting further down the list."""
    rows = [
        _row(
            OWN_TAG,
            league=OLD_LEAGUE,
            week_date=_dt.date(2026, 5, 4),
            opponent=_key("A02"),
            week_outcome="L",
        ),
        _row(OWN_TAG, ranking=1, opponent=_key("A02"), week_outcome="W"),
        _row("A02", ranking=2),
    ]
    block = _history_block(rows)
    assert "promoted" not in block and "relegated" not in block


def test_a_promotion_since_the_last_meeting_is_called_out():
    rows = [
        _row(
            OWN_TAG,
            league=OLD_LEAGUE,
            week_date=_dt.date(2026, 5, 4),
            opponent=_key("A02"),
            week_outcome="L",
        ),
        _row(OWN_TAG, ranking=1),
        _row("A02", ranking=2),
    ]
    block = _history_block(rows)
    assert "were in **Gold**" in block
    assert "**Diamond** now" in block
    assert "promoted" in block


def _history_block(rows) -> str:
    embed = ad_ui.scout_embed(_state(rows), _key("A02"))
    return next(f.value for f in embed.fields if f.name == "Head to head")


def test_never_faced_says_so_plainly():
    embed = ad_ui.scout_embed(_state(_bracket_rows()), _key("A02"))
    block = next(f.value for f in embed.fields if f.name == "Head to head")
    assert "never faced" in block


# ── Scout picker ──────────────────────────────────────────────────────────────


def test_the_picker_offers_this_weeks_opponent_first():
    rows = _bracket_rows()
    rows[0].opponent = _key("A07")
    options = ad_ui._scout_options(_state(rows))
    assert options[0].label.startswith("A07")
    assert options[0].description == "This week's opponent"


def test_the_picker_says_when_you_meet_them_and_on_which_branch():
    """Someone opens Scout because the path told them an alliance decides
    their week. The picker should not make them remember which one."""
    rows = _bracket_rows()
    by = {r.alliance: r for r in rows}
    tags = [OWN_TAG] + [f"A{i:02d}" for i in range(2, ad.BRACKET_SIZE + 1)]
    # Settle every week-1 match but the guild's own, so its result forks the
    # bracket and the two branches name different week-2 opponents.
    for a, b in zip(tags[0::2], tags[1::2]):
        if OWN_TAG in (a, b):
            by[_key(a)].opponent, by[_key(b)].opponent = _key(b), _key(a)
            continue
        by[_key(a)].week_outcome, by[_key(a)].week_score = "W", 9
        by[_key(b)].week_outcome, by[_key(b)].week_score = "L", 4
        by[_key(a)].opponent, by[_key(b)].opponent = _key(b), _key(a)

    described = {o.label: o.description for o in ad_ui._scout_options(_state(rows))}
    week_two = [d for d in described.values() if "week 2" in d]

    assert any(d.endswith("you meet them in week 2 if you win") for d in week_two)
    assert any(d.endswith("you meet them in week 2 if you lose") for d in week_two)
    assert all(d.startswith("Ranking ") for d in week_two)


def test_the_branch_drops_off_when_both_sides_reach_them_the_same_week():
    """`if you win` about something that happens either way is worse than
    saying nothing. With week 1 unrecorded the pool is the same on both
    branches, which is the S36 board's own finding."""
    rows = _bracket_rows()
    rows[0].opponent = _key("A02")
    described = [o.description for o in ad_ui._scout_options(_state(rows))]

    assert any("could be your week" in d for d in described)
    assert not any("if you win" in d or "if you lose" in d for d in described)


def test_the_picker_never_exceeds_discords_select_limit():
    rows = _bracket_rows() + _bracket_rows(week=2)
    options = ad_ui._scout_options(_state(rows))
    assert 0 < len(options) <= ad_ui.MAX_SELECT_OPTIONS


# ── The read-quota rule (#269) ────────────────────────────────────────────────


#: Every way a VS surface can reach the sheet. Named here so the guard below
#: keeps working as readers get extracted: `#405` pulled the read out of
#: `handle_vs_hub` into `read_tab_once`, which the score prompt's persistent
#: button also uses, since it cannot hold a snapshot across a restart.
_SHEET_READERS = ("load_rows", "read_tab_once")


def test_no_button_callback_reads_the_sheet():
    """1.5.1 had to fix storm screens blowing the Sheets read limit on quick
    click-through. The hub loads once and passes the snapshot down, so a
    callback reaching for the sheet is the regression to catch.

    Asserted against the callbacks themselves rather than against a count of
    the word in the module, which stopped meaning anything once a named reader
    existed for other surfaces to share.
    """
    import inspect

    view_classes = [
        obj
        for module in (hub, ad_ui)
        for _, obj in inspect.getmembers(module, inspect.isclass)
        if issubclass(obj, discord.ui.View) and obj.__module__ in (hub.__name__, ad_ui.__name__)
    ]
    assert view_classes, "no views found to check"

    for cls in view_classes:
        for name, member in inspect.getmembers(cls, inspect.isfunction):
            if member.__module__ not in (hub.__name__, ad_ui.__name__):
                continue  # inherited from discord.py
            source = inspect.getsource(member)
            for reader in _SHEET_READERS:
                assert reader not in source, f"{cls.__name__}.{name} reads the sheet"


def test_the_hub_reads_the_tab_in_exactly_one_place():
    """The other half of the rule: one reader, so "once per invocation" is a
    property of the code rather than of everyone remembering."""
    import inspect

    assert inspect.getsource(hub).count("ad_setup.load_rows") == 1


def test_the_hub_view_holds_the_snapshot_rather_than_the_guild_id_alone():
    view = hub.VSHubView(None, _state(_bracket_rows()), owner_id=7)
    assert isinstance(view.state, hub.HubState)
    assert view.state.rows


def test_buttons_are_disabled_rather_than_erroring_on_an_empty_tab():
    view = hub.VSHubView(None, _state([]), owner_id=7)
    labels = {item.label: item.disabled for item in view.children}
    assert labels[hub.VS_BTN_BRACKET] is True
    assert labels[hub.VS_BTN_WEEK] is True
    # Setup always works: it is the way out of an empty tab.
    assert labels[hub.VS_BTN_SETUP] is False


# ── Copy conventions ──────────────────────────────────────────────────────────


def test_no_surface_leaks_an_internal_name_or_an_em_dash():
    rows = _bracket_rows(
        **{
            OWN_TAG: {"power": 400_000_000, "members": 92, "gift_level": 22},
            "A02": {"power": 200_000_000, "members": 60, "gift_level": 10},
        }
    )
    rows[0].opponent = _key("A02")
    state = _state(rows)
    embeds = [
        hub.hub_embed(state),
        hub.bracket_embed(state, 1),
        hub.week_embed(state, 1),
        ad_ui.scout_embed(state, _key("A02")),
    ]
    for embed in embeds:
        text = _text(embed)
        assert "—" not in text
        assert "guild" not in text.lower()
        for leak in ("tracking_mode", "AllianceWeek", "AllianceKey", "COL_", "SOURCE_"):
            assert leak not in text


def test_scout_stops_saying_if_you_win_about_a_week_already_played():
    """A week whose result is recorded is not a branch. Forking there would
    override a confirmed result, since assumptions outrank confirmed by
    design, and describe a route that cannot happen."""
    rows = _bracket_rows() + _bracket_rows(week=2)
    tags = [OWN_TAG] + [f"A{i:02d}" for i in range(2, ad.BRACKET_SIZE + 1)]
    by = {(r.week, r.alliance): r for r in rows}
    for a, b in zip(tags[0::2], tags[1::2]):
        for outcome, (x, y) in (("W", (a, b)), ("L", (b, a))):
            row = by[(1, _key(x))]
            row.week_outcome, row.week_score, row.opponent = (
                outcome,
                9 if outcome == "W" else 4,
                _key(y),
            )

    described = [o.description for o in ad_ui._scout_options(_state(rows))]

    # Week 1 is settled, so nothing may hang a branch off it.
    assert not any("week 1" in d for d in described)
    assert all("if you win" not in d or "week 1" not in d for d in described)


def test_an_opponent_the_bot_guessed_is_labelled_above_the_fork_too():
    """`This week: A03` was rendering bare when nobody had recorded the match
    that decides it, one line above a branch where a guess carries
    `Bot prediction`. The disclaimer skips this week, so nothing said so.

    Own wins week 1 and joins the winners, who pair by seed as (1,3)(5,7)...
    Leaving seeds 3 v 4 unrecorded makes own's week-2 opponent an estimate,
    which is exactly the state the finding describes.
    """
    tags = [OWN_TAG] + [f"A{i:02d}" for i in range(2, ad.BRACKET_SIZE + 1)]
    # The undecided pair needs a gap the model will actually call. All three
    # metrics have thresholds, and a few percent reads as even -- which would
    # leave the opponent *unknown* rather than estimated, and prove nothing.
    profiles = {t: {"power": 30_000_000_000, "members": 98, "gift_level": 30} for t in tags}
    profiles[tags[2]] = {"power": 60_000_000_000, "members": 100, "gift_level": 40}
    profiles[tags[3]] = {"power": 15_000_000_000, "members": 70, "gift_level": 12}
    rows = _bracket_rows(**profiles) + _bracket_rows(week=2, **profiles)
    by = {(r.week, r.alliance): r for r in rows}
    undecided = (tags[2], tags[3])
    for a, b in zip(tags[0::2], tags[1::2]):
        by[(1, _key(a))].opponent, by[(1, _key(b))].opponent = _key(b), _key(a)
        if (a, b) == undecided:
            continue
        by[(1, _key(a))].week_outcome, by[(1, _key(a))].week_score = "W", 9
        by[(1, _key(b))].week_outcome, by[(1, _key(b))].week_score = "L", 4

    text = _text(hub.path_embed(_state(rows)))

    assert hub.VS_LABEL_BOT in text, "the guessed opponent has to say so"


def test_a_recorded_week_does_not_say_recorded_result_twice():
    """The score line already carries it. Labelling the opponent as well reads
    as a stutter, which is why the played block leaves confirmed bare."""
    rows = _bracket_rows()
    tags = [OWN_TAG] + [f"A{i:02d}" for i in range(2, ad.BRACKET_SIZE + 1)]
    by = {r.alliance: r for r in rows}
    for a, b in zip(tags[0::2], tags[1::2]):
        by[_key(a)].week_outcome, by[_key(a)].week_score = "W", 9
        by[_key(b)].week_outcome, by[_key(b)].week_score = "L", 4
        by[_key(a)].opponent, by[_key(b)].opponent = _key(b), _key(a)

    played = hub._played_block(_state(rows), ad.project_own_path(OWN, rows), None)
    for line in played:
        assert line.count(hub.VS_LABEL_RECORDED) <= 1


# -- Timeouts, found by `/code-review` on the rebase, 2026-09-04 --------------


def test_every_view_the_hub_opens_records_the_message_it_lives_on():
    """`on_timeout` edits `self.message`, so a view that never records it times
    out silently and leaves buttons looking live long after they stopped
    working. Asserted across the module rather than against one call site: this
    was already right in five places and wrong in the sixth, which is exactly
    the shape a per-site test misses.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(hub))
    offenders = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        sends_a_view = any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("send_message", "send")
            and any(kw.arg == "view" for kw in node.keywords)
            for node in ast.walk(fn)
        )
        if not sends_a_view:
            continue
        records = any(
            isinstance(target, ast.Attribute) and target.attr == "message"
            for node in ast.walk(fn)
            if isinstance(node, ast.Assign)
            for target in node.targets
        )
        if not records:
            offenders.append(fn.name)

    assert not offenders, f"views sent without recording their message: {offenders}"


# ── Shared scouting (#544) ────────────────────────────────────────────────────
#
# The payoff half: an alliance can see what fifteen others recorded, not just
# the four they played. The rules these hold are provenance and precedence.


@pytest.fixture
def shared_store(tmp_path, monkeypatch):
    import alliance_duel_db as vsdb

    monkeypatch.setattr(vsdb, "DB_PATH", str(tmp_path / "alliance_duel.sqlite3"))
    vsdb.init_db()
    return vsdb


def _record_shared(vsdb, state, tag, **kw):
    """Somebody else's guild recorded this alliance."""
    vsdb.record_weeks(
        [
            ad.AllianceWeek(
                league=state.league,
                week=1,
                alliance=ad.AllianceKey.of(tag, "1234"),
                tag_display=tag,
                **kw,
            )
        ],
        actor={"guild_id": 999999},
    )


@pytest.mark.asyncio
async def test_a_scout_card_falls_back_to_what_other_alliances_recorded(shared_store):
    state = _state([_row("US", week=1)])
    _record_shared(shared_store, state, "ZZZ", power=5_000_000, members=100, gift_level=40)
    await hub.attach_shared(state)

    text = _text(ad_ui.scout_embed(state, ad.AllianceKey.of("ZZZ", "1234")))

    assert "Nothing recorded yet" not in text
    assert "5M" in text and "100 members" in text


@pytest.mark.asyncio
async def test_a_borrowed_record_says_so(shared_store):
    """A number another alliance recorded must never read as though this
    alliance recorded it."""
    state = _state([_row("US", week=1)])
    _record_shared(shared_store, state, "ZZZ", power=5_000_000)
    await hub.attach_shared(state)

    text = _text(ad_ui.scout_embed(state, ad.AllianceKey.of("ZZZ", "1234")))

    assert ad_ui.VS_SHARED_HEADING in text
    names = [f.name for f in ad_ui.scout_embed(state, ad.AllianceKey.of("ZZZ", "1234")).fields]
    assert "Recorded" not in names, "somebody else's numbers were labelled as ours"


@pytest.mark.asyncio
async def test_our_own_sheet_wins_over_the_shared_record(shared_store):
    """The sheet is authoritative. Quietly preferring somebody else's number
    over the one the officer typed would leave them no way to tell which they
    were reading."""
    state = _state([_row("ZZZ", week=1, power=1_000_000)])
    _record_shared(shared_store, state, "ZZZ", power=9_000_000)
    await hub.attach_shared(state)

    assert state.shared_only(ad.AllianceKey.of("ZZZ", "1234")) is None
    text = _text(ad_ui.scout_embed(state, ad.AllianceKey.of("ZZZ", "1234")))
    assert "1M" in text and "9M" not in text


@pytest.mark.asyncio
async def test_shared_rows_never_reach_the_rows_a_write_reads(shared_store):
    """`row_for` feeds `_row_for_write`, which builds what goes back to this
    guild's own tab. A shared row reaching it would copy another alliance's
    record into this alliance's sheet as though they had typed it."""
    state = _state([_row("US", week=1)])
    _record_shared(shared_store, state, "ZZZ", power=5_000_000)
    await hub.attach_shared(state)

    assert state.shared, "nothing was loaded, so this proves nothing"
    assert state.row_for(ad.AllianceKey.of("ZZZ", "1234"), 1) is None
    assert all(r.alliance != ad.AllianceKey.of("ZZZ", "1234") for r in state.rows)
    assert all(r.alliance != ad.AllianceKey.of("ZZZ", "1234") for r in state.league_rows())


@pytest.mark.asyncio
async def test_an_unreadable_store_costs_a_card_and_not_the_hub(monkeypatch):
    """A second copy that will not open is not worth interrupting somebody for.
    Their own sheet is unaffected by it."""
    import alliance_duel_db as vsdb

    monkeypatch.setattr(vsdb, "DB_PATH", "/nowhere/that/exists/x.sqlite3")
    state = _state([_row("US", week=1)])

    await hub.attach_shared(state)

    assert state.shared == []
    assert _text(hub.hub_embed(state))


@pytest.mark.asyncio
async def test_nothing_anywhere_still_says_so(shared_store):
    state = _state([_row("US", week=1)])
    await hub.attach_shared(state)

    text = _text(ad_ui.scout_embed(state, ad.AllianceKey.of("NOPE", "1234")))

    assert ad_ui.VS_SHARED_NOTHING in text
