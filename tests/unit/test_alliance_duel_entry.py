"""Unit tests for the VS path view (#403) and the entry paths (#404).

The write surfaces are exercised without Discord or Sheets: `save_rows` is the
only thing that touches gspread, and it is patched. What is worth testing here
is not that gspread was called but that the *rules around* the call hold:

- every modal defers before any sheet round-trip (the #76 bug class)
- a blank field leaves the existing value alone instead of clearing it
- the loaded snapshot reflects a write, since the hub reads once per invocation
- the day a score is filed under comes from server time, not guild-local
"""

import datetime as _dt
import inspect

import pytest

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


def _row(tag, week=1, seed=None, week_date=MONDAY, **kw):
    return ad.AllianceWeek(
        league=LEAGUE,
        week=week,
        alliance=_key(tag),
        seed=seed,
        week_date=week_date,
        tag_display=tag,
        **kw,
    )


def _bracket(week=1, **per_alliance):
    tags = [OWN_TAG] + [f"A{i:02d}" for i in range(2, ad.BRACKET_SIZE + 1)]
    return [
        _row(tag, week=week, seed=seed, **per_alliance.get(tag, {}))
        for seed, tag in enumerate(tags, start=1)
    ]


def _state(rows, **cfg_kw):
    cfg = {
        "guild_id": 1,
        "enabled": 1,
        "tab_name": "Alliance Duel (VS)",
        "own_tag": OWN_TAG,
        "own_warzone": OWN_WZ,
        "tracking_mode": ad.MODE_FULL_BRACKET,
    }
    cfg.update(cfg_kw)
    return hub.HubState(1, cfg, rows)


@pytest.fixture(autouse=True)
def _no_recorded_sheet_problems(monkeypatch):
    monkeypatch.setattr(config_health, "problems_for_subjects", lambda *a, **k: [])


def _text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{f.name}\n{f.value}" for f in embed.fields]
    return "\n".join(parts)


def _play_week(rows, week, winner_of):
    pairing = ad.compute_week_pairing(rows, week)
    assert isinstance(pairing, ad.WeekPairing), pairing
    for match in pairing.matches:
        winner = winner_of(match)
        loser = match.other(winner)
        for side, other, outcome in ((winner, loser, "W"), (loser, winner, "L")):
            for row in rows:
                if row.week == week and row.alliance == side:
                    row.opponent = other
                    row.week_outcome = outcome


# ── My path (#403) ────────────────────────────────────────────────────────────


def test_the_path_names_every_opponent_once_the_league_is_played_out():
    rows = _bracket()
    for week in range(2, ad.LEAGUE_WEEKS + 1):
        rows += _bracket(week=week)
    for week in range(1, ad.LEAGUE_WEEKS + 1):
        _play_week(rows, week, winner_of=lambda m: m.a)

    text = _text(hub.path_embed(_state(rows)))
    assert "Week 1:" in text and "Week 4:" in text
    assert "recorded result" in text
    assert "What is blocking it" not in text


def test_a_blocked_path_names_the_matches_never_says_not_enough_data():
    rows = _bracket() + _bracket(week=2)
    _play_week(rows, 1, winner_of=lambda m: m.a)
    text = _text(hub.path_embed(_state(rows)))

    assert "What is blocking it" in text
    assert " vs " in text
    assert "week 2" in text
    for vague in ("not enough data", "insufficient", "unavailable"):
        assert vague not in text.lower()


def test_the_blocking_set_becomes_the_scouting_list():
    rows = _bracket() + _bracket(week=2)
    _play_week(rows, 1, winner_of=lambda m: m.a)
    text = _text(hub.path_embed(_state(rows)))

    assert "Scout these first" in text
    assert "needs power, members and gift level" in text
    # Not "go scout fifteen alliances": the lineage produces a short set.
    listed = [line for line in text.splitlines() if line.startswith("· [")]
    assert 0 < len(listed) <= 8


def test_the_path_says_how_firmly_each_step_is_known():
    """A confirmed result and a coin-flip estimate reaching the same
    conclusion are not the same claim and must not render alike."""
    rows = _bracket(
        **{
            OWN_TAG: {"power": 900_000_000, "members": 100, "gift_level": 25},
            "A02": {"power": 100_000_000, "members": 40, "gift_level": 5},
        }
    ) + _bracket(week=2)
    text = _text(hub.path_embed(_state(rows)))
    assert "estimated from stats" in text
    for leak in ("SOURCE_", "confirmed'", "estimated'"):
        assert leak not in text


def test_own_alliance_mode_gets_the_upsell_not_an_error():
    view = hub.VSHubView(None, _state(_bracket(), tracking_mode=ad.MODE_OWN_ALLIANCE), owner_id=7)
    labels = {item.label for item in view.children}
    assert hub.VS_BTN_PATH in labels


def test_the_path_without_an_own_alliance_says_where_to_set_it():
    text = _text(hub.path_embed(_state(_bracket(), own_tag="", own_warzone="")))
    assert "which alliance is yours" in text
    assert "/setup" in text


# ── Next week's rows (#404) ───────────────────────────────────────────────────


def test_next_week_rows_carry_identity_forward_and_predict_the_opponent():
    rows = _bracket()
    _play_week(rows, 1, winner_of=lambda m: m.a)

    generated = ad.next_week_rows(rows, 1)
    assert len(generated) == ad.BRACKET_SIZE
    assert {r.week for r in generated} == {2}
    # Season, tier, group, seed, tag and warzone all come forward, so the only
    # thing left to type is what actually happened.
    assert all(r.league == LEAGUE for r in generated)
    assert sorted(r.seed for r in generated) == list(range(1, ad.BRACKET_SIZE + 1))
    assert all(r.tag_display for r in generated)
    # The predicted pairing is written rather than left blank: a correction is
    # the signal that the pairing algorithm needs a look.
    assert all(r.opponent is not None for r in generated)


def test_next_week_rows_advance_the_week_date_by_seven_days():
    rows = _bracket()
    _play_week(rows, 1, winner_of=lambda m: m.a)
    assert {r.week_date for r in ad.next_week_rows(rows, 1)} == {MONDAY + _dt.timedelta(days=7)}


def test_next_week_rows_decline_before_the_results_are_in():
    assert ad.next_week_rows(_bracket(), 1) == []


def test_the_advance_button_only_appears_when_it_would_do_something():
    """A control that cannot change state is worse than no control: the user
    spends a click and some trust finding out it does nothing."""
    fresh = _state(_bracket())
    assert entry.pending_next_week(fresh) is None
    assert entry.VS_BTN_NEXT_WEEK not in {
        item.label for item in hub.VSHubView(None, fresh, owner_id=7).children
    }

    rows = _bracket()
    _play_week(rows, 1, winner_of=lambda m: m.a)
    decided = _state(rows)
    assert entry.pending_next_week(decided) == 1
    assert entry.VS_BTN_NEXT_WEEK in {
        item.label for item in hub.VSHubView(None, decided, owner_id=7).children
    }


def test_the_advance_button_stays_away_once_next_week_exists():
    rows = _bracket()
    _play_week(rows, 1, winner_of=lambda m: m.a)
    rows += _bracket(week=2)
    assert entry.pending_next_week(_state(rows)) is None


def test_own_alliance_mode_is_never_offered_bracket_generation():
    rows = _bracket()
    _play_week(rows, 1, winner_of=lambda m: m.a)
    state = _state(rows, tracking_mode=ad.MODE_OWN_ALLIANCE)
    assert entry.pending_next_week(state) is None


def test_next_week_rows_stop_at_the_end_of_the_league():
    rows = _bracket(week=ad.LEAGUE_WEEKS)
    _play_week(rows, ad.LEAGUE_WEEKS, winner_of=lambda m: m.a)
    assert ad.next_week_rows(rows, ad.LEAGUE_WEEKS) == []


# ── Which day a score is filed under ──────────────────────────────────────────


def test_the_logged_day_comes_from_the_sheet_dates_not_the_local_clock():
    state = _state(_bracket())
    week, day = entry.target_day(state)
    assert week == 1
    assert day in range(1, 7)


def test_sunday_closes_the_week_so_it_logs_enemy_buster():
    """Sunday's entry covers Saturday's day 6. Resolving this on guild-local
    time instead of server time is the #330 / #318 bug class."""
    rows = _bracket()
    state = _state(rows)
    state.live = ad.LiveWeek(LEAGUE, 1, MONDAY, None)
    assert entry.target_day(state) == (1, 6)


def test_no_live_week_means_nothing_to_log():
    state = _state(_bracket())
    state.live = None
    assert entry.target_day(state) is None
    view = hub.VSHubView(None, state, owner_id=7)
    disabled = {item.label: item.disabled for item in view.children}
    assert disabled[entry.VS_BTN_LOG_SCORE] is True


# ── Writing ───────────────────────────────────────────────────────────────────


def test_a_write_is_reflected_in_the_loaded_snapshot():
    """The hub reads once per invocation (#269), so a write that only reached
    the sheet would render as a failed save on the next button click."""
    state = _state(_bracket())
    written = ad.AllianceWeek(
        league=LEAGUE, week=1, alliance=OWN, power=500_000_000, members=99, gift_level=20
    )
    entry._patch_snapshot(state, [written])

    profile = state.profiles[OWN]
    assert profile.power == 500_000_000
    assert profile.members == 99
    assert profile.is_tier_1


def test_a_blank_field_leaves_the_existing_value_alone():
    rows = _bracket(**{OWN_TAG: {"power": 400_000_000, "members": 92, "gift_level": 18}})
    state = _state(rows)
    # Only members was retyped; power and gift level came through as None.
    entry._patch_snapshot(state, [ad.AllianceWeek(league=LEAGUE, week=1, alliance=OWN, members=95)])
    profile = state.profiles[OWN]
    assert profile.members == 95
    assert profile.power == 400_000_000
    assert profile.gift_level == 18


def test_a_new_alliance_is_appended_rather_than_dropped():
    state = _state(_bracket())
    new = _key("ZZ9")
    entry._patch_snapshot(state, [ad.AllianceWeek(league=LEAGUE, week=1, alliance=new, members=40)])
    assert new in state.profiles


def test_row_for_write_carries_identity_without_dragging_stale_values():
    """A write row is built fresh from identity only. Copying the loaded row
    wholesale would re-send every value the user did not touch, which is how a
    non-clobbering upsert quietly starts clobbering."""
    rows = _bracket(**{OWN_TAG: {"power": 400_000_000, "members": 92}})
    row = entry._row_for_write(_state(rows), OWN, 1)
    assert row.alliance == OWN
    assert row.seed == 1
    assert row.week_date == MONDAY
    assert row.power is None
    assert row.members is None


# ── The conventions these surfaces have to hold ───────────────────────────────


def test_every_modal_defers_before_touching_the_sheet():
    """CLAUDE.md 1.1.7 (#76): a slow gspread call expires the 3-second
    interaction token and the submit dies with NotFound 10062."""
    modals = [
        obj
        for obj in vars(entry).values()
        if inspect.isclass(obj) and issubclass(obj, __import__("discord").ui.Modal)
    ]
    assert modals, "no modals found, the scan is wrong"
    for modal in modals:
        source = inspect.getsource(modal.on_submit)
        assert "response.defer" in source, modal.__name__
        defer_at = source.index("response.defer")
        for call in ("save_rows", "get_spreadsheet"):
            if call in source:
                assert source.index(call) > defer_at, f"{modal.__name__} touches the sheet first"


def test_the_day_outcome_is_derived_from_the_two_scores():
    """The higher day score takes the day. That is the game's rule, so it is
    safe to derive rather than ask for twice, and the ack names it."""
    source = inspect.getsource(entry.ScoreModal.on_submit)
    assert 'outcome = "W" if ours > theirs else ("L" if ours < theirs else None)' in source


def test_picked_calls_made_through_discord_record_their_author():
    source = inspect.getsource(entry.ScoutActionsView._picker)
    assert "picked_by" in source
    assert "interaction.user.id" in source


def test_the_two_pick_buttons_read_as_a_pair_with_no_preferred_answer():
    """They differ only by which side they name, so neither takes a glyph and
    neither takes `success`. Styling one green would make the bot look like it
    had an opinion about the alliance's own call."""
    rows = _bracket()
    rows[0].opponent = _key("A02")
    view = entry.ScoutActionsView(_state(rows), _key("A02"), owner_id=7)
    picks = [i for i in view.children if i.label in (entry.VS_BTN_PICK_WIN, entry.VS_BTN_PICK_LOSS)]

    assert len(picks) == 2
    assert {p.style for p in picks} == {__import__("discord").ButtonStyle.secondary}
    for pick in picks:
        assert pick.label[0].isalpha(), "a parameter choice takes no emoji"


def test_a_picked_call_is_only_offered_on_the_alliance_you_are_playing():
    """A picked call is a call on one specific match, not a standing opinion
    about an alliance you are not facing."""
    rows = _bracket()
    rows[0].opponent = _key("A02")
    view = entry.ScoutActionsView(_state(rows), _key("A09"), owner_id=7)
    assert entry.VS_BTN_PICK_WIN not in {i.label for i in view.children}


def test_alliance_supplied_text_is_clamped_before_it_reaches_a_title():
    rows = _bracket()
    rows[1].tag_display = "T" * 400
    rows[1].name = "N" * 400
    state = _state(rows)
    assert len(state.display_name(_key("A02"))) < 100


def test_entry_copy_carries_no_em_dashes_and_no_internals():
    strings = [
        value
        for name, value in vars(entry).items()
        if isinstance(value, str) and not name.startswith("__")
    ]
    assert strings
    for text in strings:
        assert "—" not in text
        assert "guild" not in text.lower()
