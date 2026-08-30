"""Unit tests for the VS path view (#403) and the entry paths (#404).

The write surfaces are exercised without Discord or Sheets: `save_rows` is the
only thing that touches gspread, and it is patched. What is worth testing here
is not that gspread was called but that the *rules around* the call hold:

- every modal defers before any sheet round-trip (the #76 bug class)
- a blank field leaves the existing value alone instead of clearing it
- the loaded snapshot reflects a write, since the hub reads once per invocation
- the day a score is filed under comes from server time, not guild-local
"""

import asyncio
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

    embed = hub.path_embed(_state(rows))
    text = _text(embed)
    assert "Week 1:" in text and "Week 4:" in text
    assert hub.VS_LABEL_RECORDED in text
    # Every week is recorded, so there is no open week to fork on and
    # nothing outstanding for the footer to ask for. Offering "if you win"
    # on a week the game has already decided invites planning around a
    # result that has already landed.
    assert hub.VS_PATH_IF_WIN not in text
    assert embed.footer.text is None


def test_a_blocked_path_names_the_matches_never_says_not_enough_data():
    """The blockers live on the preview now: they are per-branch, and a
    combined list would send someone to scout for a route they are not on."""
    rows = _bracket() + _bracket(week=2)
    _play_week(rows, 1, winner_of=lambda m: m.a)
    text = _text(hub.path_preview_embed(_state(rows), "W"))

    assert " vs " in text
    for vague in ("not enough data", "insufficient", "unavailable"):
        assert vague not in text.lower()


def test_the_blocking_set_becomes_the_scouting_list():
    rows = _bracket() + _bracket(week=2)
    _play_week(rows, 1, winner_of=lambda m: m.a)
    text = _text(hub.path_preview_embed(_state(rows), "W"))

    assert hub.VS_PATH_BLOCKED_SCOUTABLE in text
    # Match-shaped, and each line asks for what that match is short of. Both
    # sides short of the same three collapses rather than saying it twice.
    assert "both need power, members and gift level" in text
    assert " vs " in text
    # Not "go scout fifteen alliances": the lineage produces a short set.
    listed = [line for line in text.splitlines() if line.startswith("· ")]
    assert 0 < len(listed) <= 8


def test_the_path_says_how_firmly_each_step_is_known():
    """A confirmed result and a coin-flip estimate reaching the same
    conclusion are not the same claim and must not render alike."""
    strong = {"power": 900_000_000, "members": 100, "gift_level": 25}
    weak = {"power": 100_000_000, "members": 40, "gift_level": 5}
    # Our own pair plus the two feeding week 2, so the opponent that week
    # is reachable by estimate rather than left unnamed.
    rows = _bracket(**{OWN_TAG: strong, "A02": weak, "A03": strong, "A04": weak}) + _bracket(week=2)
    text = _text(hub.path_embed(_state(rows)))
    assert hub.VS_LABEL_BOT in text
    for leak in ("SOURCE_", "confirmed'", "estimated'"):
        assert leak not in text


def test_the_path_forks_on_this_week_and_shows_both_branches():
    """The fork is the screen: "who do we play if we lose" is the question it
    exists to answer, so both branches are projected, not described."""
    rows = _bracket() + _bracket(week=2)
    text = _text(hub.path_embed(_state(rows)))
    assert hub.VS_PATH_IF_WIN in text
    assert hub.VS_PATH_IF_LOSE in text


def test_an_unnamed_week_is_counted_on_the_path_and_named_on_the_preview():
    """The path has two branches to fit and no room, so it counts. The preview
    has one branch and room for the answer itself."""
    rows = _bracket() + _bracket(week=2)
    assert "one of 2 alliances" in _text(hub.path_embed(_state(rows)))

    preview = _text(hub.path_preview_embed(_state(rows), "W"))
    assert "one of 2 alliances" not in preview
    assert "one of A03, A04" in preview


def test_the_footer_asks_for_the_right_work_per_cause():
    """A single "enter them" would send somebody to the predictions screen to
    find a match the bot would happily have predicted off numbers nobody typed."""
    rows = _bracket() + _bracket(week=2)
    footer = hub.path_embed(_state(rows)).footer.text
    # Nothing is scouted, so every blocker is waiting on numbers and the
    # prediction half has nothing to ask for.
    assert "power, members and gift level" in footer
    assert "your prediction" not in footer


def test_a_fully_recorded_alliance_is_never_sent_to_scout_it_again():
    """The defect the split exists to fix: a match blocked because the model
    declined used to land both its sides on a list headed "Scout these first",
    each marked already recorded, pointing at the one action that cannot help."""
    level = {"power": 500_000_000, "members": 80, "gift_level": 20}
    rows = _bracket(**{tag: dict(level) for tag in ("A03", "A04")}) + _bracket(week=2)
    text = _text(hub.path_preview_embed(_state(rows), "W"))

    block = text.split(hub.VS_PATH_BLOCKED_SCOUTABLE)[-1]
    scouting = block.split(hub.VS_PATH_BLOCKED_UNDECIDED)[0]
    assert "A03 vs A04" not in scouting
    assert "A03 vs A04" in text.split(hub.VS_PATH_BLOCKED_UNDECIDED)[-1]


def test_the_path_view_offers_both_previews_and_a_way_to_scout():
    view = hub.VSPathView(_state(_bracket()), owner_id=7)
    labels = {item.label for item in view.children}
    assert hub.VS_BTN_PREVIEW_WIN in labels
    assert hub.VS_BTN_PREVIEW_LOSE in labels
    # Scout hangs off the path because the path is where someone finds out an
    # alliance decides their week and knows nothing about them.
    assert hub.VS_BTN_SCOUT in labels


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


# ── Predictions on the rest of the league (#403, screen 2) ────────────────────


class _FakeMember:
    def __init__(self, display_name):
        self.display_name = display_name


class _FakeGuild:
    def __init__(self, members=None):
        self._members = members or {}

    def get_member(self, uid):
        return self._members.get(uid)


class _FakeResponse:
    def __init__(self):
        self.edited = None
        self.sent = None

    async def edit_message(self, **kw):
        self.edited = kw

    async def send_message(self, content=None, **kw):
        self.sent = content

    async def defer(self, **kw):
        pass


class _FakeInteraction:
    def __init__(self, values=None, user_id=99, guild=None):
        self.data = {"values": values or []}
        self.user = type("U", (), {"id": user_id})()
        self.guild = guild
        self.response = _FakeResponse()
        self.followed = []

        async def _send(content=None, **kw):
            self.followed.append(content)

        self.followup = type("F", (), {"send": staticmethod(_send)})()


def _picked(rows, tag, winner_tag, opponent_tag, by="", week=1):
    for row in rows:
        if row.week == week and row.alliance == _key(tag):
            row.picked = "W" if tag == winner_tag else "L"
            row.picked_by = by
            row.opponent = _key(opponent_tag)


def test_your_own_match_is_not_on_the_predictions_screen():
    """It has a Picked flow on the scout card, where the head-to-head and the
    opponent's numbers are already on screen. Asking twice splits one answer."""
    state = _state(_bracket())
    matches = entry.week_matches(state, 1)
    assert matches
    assert all(OWN not in (m.a, m.b) for m in matches)


def test_a_discord_prediction_credits_a_live_mention_and_a_sheet_one_its_text():
    rows = _bracket()
    _picked(rows, "A03", "A03", "A04", by="123456789012345678")
    _picked(rows, "A05", "A05", "A06", by="Sarah")
    text = _text(entry.predictions_embed(_state(rows), 1, {}))

    assert "<@123456789012345678>" in text
    assert "Sarah" in text


def test_a_prediction_with_nobody_named_shows_nothing_rather_than_an_apology():
    """Kevin's rule, 2026-08-27. `Picked By` is a column most people never
    fill in, so this is the common case for anything typed into the sheet."""
    rows = _bracket()
    _picked(rows, "A03", "A03", "A04", by="")
    text = _text(entry.predictions_embed(_state(rows), 1, {}))

    assert "A03 v A04" in text
    for apology in ("from the sheet", "unknown", "someone"):
        assert apology not in text.lower()


def test_every_match_offers_both_directions():
    state = _state(_bracket())
    options = entry.prediction_options(state, 1, {})
    assert len(options) == 2 * len(entry.week_matches(state, 1))
    assert len(options) <= 25


def test_the_option_matching_a_prediction_names_who_made_it():
    rows = _bracket()
    _picked(rows, "A03", "A03", "A04", by="42")
    guild = _FakeGuild({42: _FakeMember("Kevin")})
    described = {
        o.label: o.description for o in entry.prediction_options(_state(rows), 1, {}, guild)
    }
    assert described["A03 beats A04"] == "Predicted by Kevin"
    assert described["A04 beats A03"] == "Replaces the prediction of A03"


def test_a_predictor_who_has_left_says_nothing_never_no_prediction_yet():
    """There *is* a prediction; we just cannot name who made it. Saying "no
    prediction yet" would be flatly false."""
    rows = _bracket()
    _picked(rows, "A03", "A03", "A04", by="42")
    described = {
        o.label: o.description for o in entry.prediction_options(_state(rows), 1, {}, _FakeGuild())
    }
    assert described["A03 beats A04"] is None


@pytest.mark.asyncio
async def test_staging_writes_nothing_until_save(_captured):
    state = _state(_bracket())
    view = entry.PredictionsView(state, 1, owner_id=99)
    await view._staged(_FakeInteraction(values=["0:a"]))

    assert view.staged
    assert _captured == []


@pytest.mark.asyncio
async def test_saving_writes_one_row_per_match_not_two(_captured):
    """`_MatchResolver` reads a Picked call off either side, so a second row
    would double the sheet traffic and give rule 7 two cells to disagree
    about."""
    state = _state(_bracket())
    view = entry.PredictionsView(state, 1, owner_id=99)
    await view._staged(_FakeInteraction(values=["0:a", "1:b"]))
    await view._save(_FakeInteraction(user_id=7))

    assert len(_captured) == 2
    assert all(row.picked == "W" for row in _captured)
    assert all(row.picked_by == "7" for row in _captured)
    assert len({row.alliance for row in _captured}) == 2


@pytest.mark.asyncio
async def test_the_confirmation_names_every_call_in_week_order(_captured):
    """Kevin's call, 2026-08-29. A count alone hides a mis-tap, so each winner
    is spelled out. Week order, not tap order, so the list reads down the embed
    the person is looking at rather than replaying how they got there."""
    state = _state(_bracket())
    view = entry.PredictionsView(state, 1, owner_id=99)
    # Staged later-match-first on purpose, so tap order and week order differ.
    await view._staged(_FakeInteraction(values=["2:a", "0:b"]))
    interaction = _FakeInteraction(user_id=7)
    await view._save(interaction)

    matches = entry.week_matches(state, 1)
    first = f"{state.display_name(matches[0].b)} over {state.display_name(matches[0].a)}"
    third = f"{state.display_name(matches[2].a)} over {state.display_name(matches[2].b)}"
    said = interaction.followed[0]

    assert "Saved 2 predictions" in said
    assert first in said and third in said
    assert said.index(first) < said.index(third)


@pytest.mark.asyncio
async def test_one_prediction_is_not_pluralised(_captured):
    state = _state(_bracket())
    view = entry.PredictionsView(state, 1, owner_id=99)
    await view._staged(_FakeInteraction(values=["0:a"]))
    interaction = _FakeInteraction(user_id=7)
    await view._save(interaction)

    match = entry.week_matches(state, 1)[0]
    assert interaction.followed[0] == (
        f"✅ Saved 1 prediction: {state.display_name(match.a)} over {state.display_name(match.b)}"
    )


@pytest.mark.asyncio
async def test_a_second_press_writes_nothing_and_says_so(_captured):
    """The screen is edited on save, so Save goes dead — but component state
    can be stale, and a dead-end acknowledgment beats `Saved 0 predictions:`."""
    state = _state(_bracket())
    view = entry.PredictionsView(state, 1, owner_id=99)
    await view._staged(_FakeInteraction(values=["0:a"]))
    await view._save(_FakeInteraction(user_id=7))
    written_once = list(_captured)

    second = _FakeInteraction(user_id=7)
    await view._save(second)

    assert list(_captured) == written_once
    assert second.followed == [entry.PREDICT_NOTHING_TO_SAVE]


@pytest.mark.asyncio
async def test_two_presses_racing_write_one_set_of_rows(monkeypatch):
    """`save_rows` awaits on a thread. Staging is taken and cleared before the
    write precisely so the press that lands during it finds nothing to do."""
    written = []
    started = asyncio.Event()

    async def _slow(state, rows):
        written.extend(rows)
        started.set()
        await asyncio.sleep(0.05)
        return ""

    monkeypatch.setattr(entry, "save_rows", _slow)
    state = _state(_bracket())
    view = entry.PredictionsView(state, 1, owner_id=99)
    await view._staged(_FakeInteraction(values=["0:a", "1:b"]))

    first = asyncio.create_task(view._save(_FakeInteraction(user_id=7)))
    await started.wait()
    second = _FakeInteraction(user_id=7)
    await view._save(second)
    await first

    assert len(written) == 2
    assert second.followed == [entry.PREDICT_NOTHING_TO_SAVE]


@pytest.mark.asyncio
async def test_a_failed_write_hands_the_staging_back(monkeypatch):
    """Nothing was written, so the screen is still true. Losing the staging
    would make the retry cost every tap again."""

    async def _refuse(state, rows):
        return "I couldn't write to your tab: nope"

    monkeypatch.setattr(entry, "save_rows", _refuse)
    state = _state(_bracket())
    view = entry.PredictionsView(state, 1, owner_id=99)
    await view._staged(_FakeInteraction(values=["0:a", "1:b"]))
    before = dict(view.staged)

    interaction = _FakeInteraction(user_id=7)
    await view._save(interaction)

    assert view.staged == before
    assert "couldn't write" in (interaction.followed[0] or "")


@pytest.mark.asyncio
async def test_picking_both_directions_is_refused_and_names_the_match(_captured):
    """Discord does not report which was tapped last, so guessing would be
    inventing an answer."""
    state = _state(_bracket())
    view = entry.PredictionsView(state, 1, owner_id=99)
    interaction = _FakeInteraction(values=["0:a", "0:b"])
    await view._staged(interaction)

    assert not view.staged
    assert _captured == []
    match = entry.week_matches(state, 1)[0]
    assert state.display_name(match.a) in (interaction.response.sent or "")


@pytest.mark.asyncio
async def test_save_is_dead_until_something_is_staged(_captured):
    view = entry.PredictionsView(_state(_bracket()), 1, owner_id=99)
    saves = [i for i in view.children if getattr(i, "label", "") == entry.VS_BTN_SAVE_PREDICTIONS]
    assert saves and saves[0].disabled

    await view._staged(_FakeInteraction(values=["0:a"]))
    saves = [i for i in view.children if getattr(i, "label", "") == entry.VS_BTN_SAVE_PREDICTIONS]
    assert saves and not saves[0].disabled


# ── Screen 3: entering results (#404) ─────────────────────────────────────────


def test_every_duel_day_shows_even_the_ones_nobody_entered():
    """The gaps are the point of the screen: four blank days is the thing it
    exists to tell you."""
    rows = _bracket(**{OWN_TAG: {"opponent": _key("A02")}})
    text = _text(entry.results_embed(_state(rows), 1))

    for day in range(1, 7):
        assert f"Day {day} " in text
    for theme in ("Radar Training", "Base Expansion", "Enemy Buster"):
        assert theme in text


def test_a_played_day_names_the_verdict_and_never_abbreviates_a_score():
    """The game prints these in full, so we do -- the same rule power follows."""
    rows = _bracket(
        **{
            OWN_TAG: {
                "opponent": _key("A02"),
                "day_scores": {1: 1204000000},
                "day_outcomes": {1: "W"},
            },
            "A02": {"opponent": OWN, "day_scores": {1: 980000000}},
        }
    )
    text = _text(entry.results_embed(_state(rows), 1))

    assert "1,204,000,000" in text
    assert "980,000,000" in text
    assert entry.VS_RESULTS_WON in text
    assert "1.2b" not in text


def test_a_day_with_scores_but_no_verdict_says_nothing_rather_than_guessing():
    """`ScoreModal` only calls a day once it has both sides."""
    rows = _bracket(**{OWN_TAG: {"opponent": _key("A02"), "day_scores": {1: 500}}})
    lines = entry.own_day_lines(_state(rows), 1, OWN, _key("A02"))

    assert lines[0] == "Day 1 Radar Training"
    assert entry.VS_RESULTS_WON not in lines[0]
    assert entry.VS_RESULTS_LOST not in lines[0]


def test_a_half_recorded_week_split_fills_in_from_thirteen():
    """A matchup's two week scores total 13, which is already a validation
    rule, so one side is enough and the match reads whole."""
    match = entry.week_matches(_state(_bracket()), 1)[0]
    rows = _bracket()
    for row in rows:
        if row.alliance == match.a:
            row.week_score = 5
    lines = entry.rest_of_league_lines(_state(rows), 1)

    # 5 for one side derives 8 for the other, and 8 won, so 8 leads.
    assert any(" 8 - 5 " in line for line in lines)


def test_a_recorded_match_puts_its_winner_first():
    """Match Record leads with the winner -- all eight rows of a real week,
    three of them against seed order. Reading the two screens side by side is
    the job, and a line needing a mental flip is a line that gets misread."""
    state = _state(_bracket())
    match = entry.week_matches(state, 1)[0]
    loser, winner = match.a, match.b  # seed order puts the loser first
    rows = _bracket()
    for row in rows:
        if row.alliance == winner:
            row.week_score = 9
        elif row.alliance == loser:
            row.week_score = 4
    line = entry.rest_of_league_lines(_state(rows), 1)[0]

    assert line == (f"{state.display_name(winner)} 9 - 4 {state.display_name(loser)}")


def test_an_unrecorded_match_keeps_seed_order():
    """No winner yet, so there is nothing to lead with."""
    state = _state(_bracket())
    match = entry.week_matches(state, 1)[0]
    line = entry.rest_of_league_lines(state, 1)[0]

    assert line.startswith(state.display_name(match.a))


def test_your_own_match_is_not_in_the_rest_of_the_league():
    """It is the field above, at a completely different grain."""
    state = _state(_bracket(**{OWN_TAG: {"opponent": _key("A02")}}))
    own = state.display_name(OWN)

    assert all(own not in line.split() for line in entry.rest_of_league_lines(state, 1))


def test_the_day_picker_carries_what_each_day_already_holds():
    """The hub's score button only ever offers today, which is no use on a
    screen showing four days nobody entered."""
    rows = _bracket(
        **{OWN_TAG: {"opponent": _key("A02"), "day_outcomes": {1: "W"}, "day_scores": {1: 5}}}
    )
    options = entry.day_options(_state(rows), 1)

    assert len(options) == 6
    assert options[0].description == entry.VS_RESULTS_WON
    assert options[1].description == entry.VS_RESULTS_NOT_ENTERED


def test_scored_days_get_air_and_empty_ones_stay_packed():
    """Straight off the mockup: a three-line block is separated, a list of
    what is missing is not."""
    rows = _bracket(
        **{
            OWN_TAG: {"opponent": _key("A02"), "day_scores": {1: 5}, "day_outcomes": {1: "W"}},
        }
    )
    lines = entry.own_day_lines(_state(rows), 1, OWN, _key("A02"))

    assert lines[2] == ""
    assert "" not in lines[3:]

    bare = entry.own_day_lines(
        _state(_bracket(**{OWN_TAG: {"opponent": _key("A02")}})), 1, OWN, _key("A02")
    )
    assert "" not in bare
