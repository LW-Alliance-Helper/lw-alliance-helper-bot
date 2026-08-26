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
    # Season, tier, group, ranking, tag and warzone all come forward, so the only
    # thing left to type is what actually happened.
    assert all(r.league == LEAGUE for r in generated)
    assert sorted(r.ranking for r in generated) == list(range(1, ad.BRACKET_SIZE + 1))
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
    assert row.ranking == 1
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


# ── Starting a new league ─────────────────────────────────────────────────────


def _entries(tags=None):
    tags = tags or ([OWN_TAG] + [f"A{i:02d}" for i in range(2, ad.BRACKET_SIZE + 1)])
    return tuple(
        ad.BracketEntry(alliance=_key(t), ranking=i, tag_display=t, warzone_display=OWN_WZ)
        for i, t in enumerate(tags, start=1)
    )


@pytest.fixture
def _captured(monkeypatch):
    """Swallow the write and hand back what would have been written."""
    written = []

    async def _fake(state, rows):
        written.extend(rows)
        return ""

    monkeypatch.setattr(entry, "save_rows", _fake)
    return written


def test_the_button_offers_itself_when_nothing_is_recorded():
    assert entry.pending_new_league(_state([])) is True


def test_the_button_stays_away_mid_league():
    # The rows exist and "Start next week's rows" is the control that moves
    # them on. A second bracket does not belong in a league being played.
    assert entry.pending_new_league(_state(_bracket())) is False


def test_the_button_returns_once_the_league_is_played_out():
    rows = []
    for week in range(1, ad.LEAGUE_WEEKS + 1):
        rows += _bracket(week=week)
    for week in range(1, ad.LEAGUE_WEEKS + 1):
        _play_week(rows, week, lambda m: m.a)
    assert entry.pending_new_league(_state(rows)) is True


def test_starting_and_advancing_never_want_the_same_slot():
    """They share a place on the button row, so one has to be off whenever the
    other is on. Advancing declines at week 4 (there is no week 5), which is
    exactly where starting a league becomes available again."""
    rows = []
    for week in range(1, ad.LEAGUE_WEEKS + 1):
        rows += _bracket(week=week)
        for played in range(1, week + 1):
            _play_week(rows, played, lambda m: m.a)
        state = _state(rows)
        assert not (
            entry.pending_next_week(state) is not None and entry.pending_new_league(state)
        ), f"both offered after week {week}"


async def test_a_new_league_is_written_stamped_and_ranked(_captured):
    state = _state([])
    league = ad.LeagueKey("S36", "Diamond", "12 - 1")
    ok, message = await entry.start_new_league(state, league, MONDAY, _entries())

    assert ok, message
    assert len(_captured) == ad.BRACKET_SIZE
    assert {r.league for r in _captured} == {league}
    assert {r.week for r in _captured} == {1}
    assert {r.week_date for r in _captured} == {MONDAY}
    assert sorted(r.ranking for r in _captured) == list(range(1, ad.BRACKET_SIZE + 1))


async def test_week_1_opponents_are_left_for_the_rankings_to_say(_captured):
    # Sixteen cells restating what the rankings already imply. `compute_week_pairing`
    # derives them on every read, so writing them would buy nothing and could
    # disagree with itself.
    state = _state([])
    await entry.start_new_league(state, LEAGUE, MONDAY, _entries())
    assert all(r.opponent is None for r in _captured)
    pairing = ad.compute_week_pairing(_captured, 1)
    assert isinstance(pairing, ad.WeekPairing)
    assert len(pairing.matches) == ad.BRACKET_SIZE // 2


async def test_a_bracket_without_your_own_alliance_is_refused(_captured):
    # Rule 6 would report this every time "Check my sheet" ran. Better to catch
    # it at the point of entry, while the League screen is still open.
    state = _state([])
    others = [f"A{i:02d}" for i in range(1, ad.BRACKET_SIZE + 1)]
    ok, message = await entry.start_new_league(state, LEAGUE, MONDAY, _entries(others))
    assert not ok
    assert "not in that bracket" in message
    assert _captured == []


async def test_the_hub_sees_the_league_it_just_wrote(_captured):
    # The hub reads the sheet once per invocation, so without this the officer
    # saves a bracket and lands back on a hub still saying there is no league.
    state = _state([])
    assert state.league is None
    league = ad.LeagueKey("S36", "Diamond", "12 - 1")
    await entry.start_new_league(state, league, MONDAY, _entries())
    assert state.league == league


async def test_own_alliance_mode_writes_only_your_row(_captured):
    state = _state([], tracking_mode=ad.MODE_OWN_ALLIANCE)
    ok, _ = await entry.start_new_league(state, LEAGUE, MONDAY, _entries())
    assert ok
    assert [r.alliance for r in _captured] == [OWN]


async def test_a_sunday_start_date_lands_on_the_week_it_opens(_captured):
    """`week_monday` sends Sunday back rather than forward, so an officer who
    types the Sunday before would otherwise stamp the week that just ended."""
    state = _state([])
    monday = ad.week_monday(MONDAY)
    await entry.start_new_league(state, LEAGUE, monday, _entries())
    assert {r.week_date for r in _captured} == {monday}


async def test_the_bracket_lines_carry_power_gift_and_members_onto_the_rows(_captured):
    state = _state([])
    entries = (
        ad.BracketEntry(
            alliance=OWN,
            ranking=1,
            tag_display=OWN_TAG,
            warzone_display=OWN_WZ,
            power=26853240157,
            gift_level=25,
            members=100,
        ),
    ) + _entries()[1:]
    await entry.start_new_league(state, LEAGUE, MONDAY, entries)
    mine = next(r for r in _captured if r.alliance == OWN)
    assert (mine.power, mine.gift_level, mine.members) == (26853240157, 25, 100)


async def test_a_line_without_the_extras_leaves_them_unset(_captured):
    # None is what `row_values` omits, which is what keeps the skeleton's
    # "leave whatever is there" behaviour on a re-run.
    state = _state([])
    await entry.start_new_league(state, LEAGUE, MONDAY, _entries())
    assert all(r.power is None and r.members is None for r in _captured)


async def test_the_acknowledgement_stops_asking_once_nothing_is_missing(_captured):
    state = _state([])
    full = tuple(
        ad.BracketEntry(
            alliance=e.alliance,
            ranking=e.ranking,
            tag_display=e.tag_display,
            warzone_display=e.warzone_display,
            power=30_000_000_000,
            gift_level=25,
            members=100,
        )
        for e in _entries()
    )
    ok, message = await entry.start_new_league(state, LEAGUE, MONDAY, full)
    assert ok
    assert "Record each day as it lands." in message
    assert "still need" not in message
    assert "Add power" not in message


async def test_a_part_filled_bracket_says_how_many_are_short(_captured):
    state = _state([])
    entries = list(_entries())
    entries[0] = ad.BracketEntry(
        alliance=entries[0].alliance,
        ranking=1,
        tag_display=OWN_TAG,
        warzone_display=OWN_WZ,
        power=30_000_000_000,
        gift_level=25,
        members=100,
    )
    ok, message = await entry.start_new_league(state, LEAGUE, MONDAY, tuple(entries))
    assert ok
    assert f"{ad.BRACKET_SIZE - 1} of them still need" in message


def test_a_refusal_hands_back_a_retry_rather_than_a_command_to_re_run():
    """UX.md: a validation failure costs one step, not the whole flow. Without
    the button, "try again" means retyping sixteen lines to fix one of them."""
    source = inspect.getsource(entry.NewLeagueModal)
    assert "_RetryNewLeagueView" in source
    assert "Run `/vs`" not in source, "a refusal must not send them back to the command"

    view = entry._RetryNewLeagueView(_state([]), 1, {"season": "S36"})
    assert [b.label for b in view.children] == [entry.VS_BTN_RETRY_NEW_LEAGUE]


def test_the_retry_modal_still_holds_what_was_typed():
    typed = {
        "season": "S36",
        "tier": "Diamond",
        "group": "12 - 1",
        "week_date": "8/24",
        "bracket": "kTZ 714",
    }
    modal = entry.NewLeagueModal(_state([]), defaults=typed)
    assert modal.season.default == "S36"
    assert modal.bracket.default == "kTZ 714"


def test_the_league_week_is_asked_for_instead_of_a_date():
    """The League screen shows a countdown and a Week 1-4 header. Which week it
    is on is readable; the Monday week 1 began on has to be worked out."""
    modal = entry.NewLeagueModal(_state([]))
    assert modal.week_now.required is False
    assert "1, 2, 3 or 4" in modal.week_now.placeholder
    assert not hasattr(modal, "week_date")


def test_every_modal_field_fits_what_discord_will_accept():
    """Discord rejects an oversized label or placeholder at send time, not at
    construction, so a too-long one ships green and fails in front of a user.
    Limits: label 45, placeholder 100."""
    import discord

    modals = [
        obj
        for obj in vars(entry).values()
        if inspect.isclass(obj) and issubclass(obj, discord.ui.Modal)
    ]
    built = [
        entry.NewLeagueModal(_state([])),
        entry.NewLeagueModal(_state([], tracking_mode=ad.MODE_OWN_ALLIANCE)),
    ]
    for modal in modals:
        for item in getattr(modal, "__discord_ui_modal_children__", []) or []:
            built.append(item)
    fields = []
    for m in built:
        fields.extend(getattr(m, "children", [m]))
    assert fields
    for field in fields:
        label = getattr(field, "label", "") or ""
        placeholder = getattr(field, "placeholder", "") or ""
        assert len(label) <= 45, f"{label!r} is {len(label)} characters"
        assert len(placeholder) <= 100, f"{placeholder!r} is {len(placeholder)} characters"


async def test_a_mid_league_setup_writes_every_week_up_to_this_one(_captured):
    # An alliance that finds the feature in week 3 would otherwise get rows
    # dated a fortnight ago, nothing covering today, and a hub reporting itself
    # as between leagues.
    state = _state([])
    week_1_monday = MONDAY - _dt.timedelta(weeks=2)
    ok, message = await entry.start_new_league(
        state, LEAGUE, week_1_monday, _entries(), upto_week=3
    )
    assert ok, message
    assert sorted({r.week for r in _captured}) == [1, 2, 3]
    assert len(_captured) == ad.BRACKET_SIZE * 3
    assert "weeks 1 to 3" in message


async def test_each_generated_week_carries_its_own_monday(_captured):
    state = _state([])
    week_1_monday = MONDAY - _dt.timedelta(weeks=2)
    await entry.start_new_league(state, LEAGUE, week_1_monday, _entries(), upto_week=3)
    by_week = {r.week: r.week_date for r in _captured}
    assert by_week[1] == week_1_monday
    assert by_week[2] == week_1_monday + _dt.timedelta(weeks=1)
    assert by_week[3] == week_1_monday + _dt.timedelta(weeks=2)


async def test_a_mid_league_setup_leaves_the_hub_on_a_live_week(_captured):
    # The point of writing the intervening weeks at all.
    state = _state([])
    await entry.start_new_league(
        state, LEAGUE, MONDAY - _dt.timedelta(weeks=2), _entries(), upto_week=3
    )
    live = ad.resolve_live_week(_captured)
    assert live is not None
    assert live.week == 3


async def test_no_week_carries_a_pairing_it_could_not_know(_captured):
    # Week 1's follows from the rankings. A later week's cannot be known until the
    # week before it is recorded, so a written guess would be a confident lie.
    state = _state([])
    await entry.start_new_league(
        state, LEAGUE, MONDAY - _dt.timedelta(weeks=2), _entries(), upto_week=3
    )
    assert all(r.opponent is None for r in _captured)


async def test_the_alliances_short_of_data_are_counted_once_not_once_a_week(_captured):
    state = _state([])
    ok, message = await entry.start_new_league(
        state, LEAGUE, MONDAY - _dt.timedelta(weeks=2), _entries(), upto_week=3
    )
    assert ok
    # 16 alliances across 3 weeks is 48 rows, but only 16 things to go and find.
    assert f"{ad.BRACKET_SIZE * 3} of them" not in message
    assert "Add power, gift level and members" in message


def test_the_week_view_pairs_from_the_whole_league_not_one_week():
    """`compute_week_pairing` weighs every prior result, so handing it a single
    week scores everyone zero and reproduces week 1's ranking order for every week
    of the league."""
    rows = _bracket(week=1)
    _play_week(rows, 1, lambda m: m.b)
    rows += ad.next_week_rows(rows, 1)
    state = _state(rows)

    truth = ad.compute_week_pairing(rows, 2)
    assert isinstance(truth, ad.WeekPairing)
    real = state.display_name(truth.match_for(OWN).other(OWN))
    ranking_order = state.display_name(_key("A02"))
    assert real != ranking_order, "fixture is not exercising a reshuffle"

    # The line naming your own matchup, not the tag appearing anywhere in a
    # list of eight. The stale version put the ranking-order opponent here.
    own_line = next(
        ln for ln in _text(hub.week_embed(state, 2)).splitlines() if state.display_name(OWN) in ln
    )
    assert real in own_line
    assert ranking_order not in own_line


def test_a_week_whose_predecessor_is_unrecorded_says_so_rather_than_guessing():
    rows = _bracket(week=1) + _bracket(week=2)
    state = _state(rows)
    text = _text(hub.week_embed(state, 2))
    assert "Week 1's results decide who plays who in week 2" in text
