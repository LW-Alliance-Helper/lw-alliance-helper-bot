"""League identity in the shared store (#658).

The game reuses a league's label: "S36 Diamond 12-1" is this bracket now and
a later set of warzones' bracket a month on, and two brackets may carry it at
once. So the store never joins on the label. These pin the rules Kevin settled
on 28 Sep:

- labels are standardized however they were typed (`S37`, `12-1`);
- a stored row is one alliance's one week, whatever label it came under;
- rows are the same league when they share a label within one league's four
  weeks and one server recorded them together, or a reported pairing joins
  them, and grouping spreads through those links;
- `/vs` finds its league by who is in it and when, so a reused label a month
  later never reaches it.

And the one-time rebuild of a store still keyed on the label, which has to
bring every server's rows and days across intact.
"""

from __future__ import annotations

import datetime as _dt
import sqlite3

import pytest

import alliance_duel as ad
import alliance_duel_db as vsdb
import vs_labels

MONDAY = _dt.date(2026, 9, 7)
LATER = MONDAY + _dt.timedelta(weeks=9)  # the next set of warzones' turn
LABEL = ad.LeagueKey("S36", "Diamond", "12-1")


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(vsdb, "DB_PATH", str(tmp_path / "alliance_duel.sqlite3"))
    vsdb.init_db()
    return vsdb


def _key(tag, warzone="999"):
    return ad.AllianceKey.of(tag, warzone)


def _row(tag, week=1, *, start=MONDAY, league=LABEL, warzone="999", **kw):
    return ad.AllianceWeek(
        league=league,
        week=week,
        alliance=_key(tag, warzone),
        week_date=start + _dt.timedelta(weeks=week - 1),
        tag_display=tag,
        **kw,
    )


def _league_of(tag, week_date, warzone="999"):
    with vsdb._get_conn() as conn:
        found = conn.execute(
            "SELECT league_id FROM alliance_weeks WHERE tag = ? AND warzone = ? AND week_date = ?",
            (*vsdb._key_parts(_key(tag, warzone)), week_date.isoformat()),
        ).fetchone()
    return found[0] if found else None


def _count():
    with vsdb._get_conn() as conn:
        return conn.execute("SELECT COUNT(*) FROM alliance_weeks").fetchone()[0]


# ── Standardized labels ───────────────────────────────────────────────────────


@pytest.mark.parametrize("typed", ["S37", "s37", "37", "S 37", "Season 37", "season37", "S037"])
def test_every_way_of_typing_a_season_is_the_one_the_game_shows(typed):
    assert vs_labels.standard_season(typed) == "S37"


@pytest.mark.parametrize("typed", ["12-1", "12 - 1", "12 -1", "12– 1", "12 — 1"])
def test_a_group_has_no_spaces_around_its_dash(typed):
    assert vs_labels.standard_group(typed) == "12-1"


def test_a_tier_takes_the_games_capitalization():
    assert vs_labels.standard_tier("diamond") == "Diamond"


def test_anything_unrecognised_is_kept_as_typed_rather_than_guessed_at():
    assert vs_labels.standard_season("Spring cup") == "Spring cup"
    assert vs_labels.standard_tier("Platinum") == "Platinum"


def test_two_spellings_are_one_league():
    assert ad.LeagueKey("s36", "diamond", "12 - 1") == LABEL
    assert ad.LeagueKey.of("Season 36", "Diamond", "12–1") == LABEL


def test_a_stored_event_key_keeps_its_week_and_day():
    assert vs_labels.standard_key("S36|Diamond|12 - 1|2|3") == "S36|Diamond|12-1|2|3"
    assert vs_labels.standard_key("S36|Diamond|12 - 1") == "S36|Diamond|12-1"


# ── One row per alliance per week ─────────────────────────────────────────────


def test_another_spelling_of_the_same_week_updates_the_row_not_a_second_one():
    vsdb.record_weeks([_row("ABC", power=100)], actor={"guild_id": 1})
    vsdb.record_weeks(
        [_row("ABC", league=ad.LeagueKey("S35", "Diamond", "12-1"), members=90)],
        actor={"guild_id": 2},
    )
    assert _count() == 1
    stored = vsdb.weeks_for_alliance(_key("ABC"))[0]
    # The latest label wins, and neither write erased the other's number.
    assert (stored["season"], stored["power"], stored["members"]) == ("S35", 100, 90)


def test_a_corrected_date_moves_the_row():
    vsdb.record_weeks([_row("ABC", week_score=7)], actor={"guild_id": 1})
    moved = _row("ABC")
    moved.week_date = MONDAY + _dt.timedelta(days=7)
    vsdb.record_weeks([moved], actor={"guild_id": 1})
    assert _count() == 1
    assert vsdb.weeks_for_alliance(_key("ABC"))[0]["week_date"] == moved.week_date.isoformat()


def test_an_undated_row_keeps_its_label_key():
    undated = _row("ABC")
    undated.week_date = None
    vsdb.record_weeks([undated, undated], actor={"guild_id": 1})
    assert _count() == 1


# ── Which rows are one league ─────────────────────────────────────────────────


def _bracket(tags, *, start=MONDAY, weeks=4, **kw):
    return [_row(tag, week, start=start, **kw) for tag in tags for week in range(1, weeks + 1)]


def test_a_new_leagues_skeleton_is_one_league_before_anyone_plays():
    tags = [f"T{i:02d}" for i in range(16)]
    vsdb.record_weeks(_bracket(tags, weeks=1), actor={"guild_id": 1})
    assert len({_league_of(t, MONDAY) for t in tags}) == 1


def test_the_same_label_a_month_later_is_a_different_league():
    """Kevin's case: the next set of warzones reaches S36 12-1."""
    vsdb.record_weeks(_bracket(["ABC", "DEF"]), actor={"guild_id": 1})
    vsdb.record_weeks(_bracket(["GHI", "JKL"], start=LATER, warzone="1500"), actor={"guild_id": 2})

    assert _league_of("ABC", MONDAY) != _league_of("GHI", LATER, "1500")


def test_two_brackets_under_one_label_at_once_stay_apart():
    vsdb.record_weeks(_bracket(["ABC", "DEF"]), actor={"guild_id": 1})
    vsdb.record_weeks(_bracket(["GHI", "JKL"], warzone="1500"), actor={"guild_id": 2})

    assert _league_of("ABC", MONDAY) != _league_of("GHI", MONDAY, "1500")


def test_a_reported_pairing_joins_two_servers_whatever_label_each_used():
    vsdb.record_weeks(
        [_row("ABC", opponent=_key("GHI"))] + _bracket(["DEF"], weeks=1), actor={"guild_id": 1}
    )
    vsdb.record_weeks(
        [_row("GHI", league=ad.LeagueKey("S36", "Diamond", "12-2"), warzone="999")],
        actor={"guild_id": 2},
    )
    assert _league_of("GHI", MONDAY) == _league_of("ABC", MONDAY) == _league_of("DEF", MONDAY)


def test_grouping_spreads_through_links_written_in_any_order():
    # GHI's row lands first, alone; the pairing that ties it in comes later.
    vsdb.record_weeks([_row("GHI")], actor={"guild_id": 2})
    vsdb.record_weeks(_bracket(["ABC", "DEF"], weeks=1), actor={"guild_id": 1})
    assert _league_of("GHI", MONDAY) != _league_of("ABC", MONDAY)

    vsdb.record_weeks([_row("DEF", opponent=_key("GHI"))], actor={"guild_id": 1})
    assert _league_of("GHI", MONDAY) == _league_of("ABC", MONDAY)


def test_an_alliances_four_weeks_are_one_league():
    vsdb.record_weeks(_bracket(["ABC"]), actor=None)
    assert len({_league_of("ABC", MONDAY + _dt.timedelta(weeks=w)) for w in range(4)}) == 1


def test_an_undated_row_joins_its_own_servers_league():
    vsdb.record_weeks(_bracket(["ABC"], weeks=1), actor={"guild_id": 1})
    undated = _row("DEF", week=3)
    undated.week_date = None
    vsdb.record_weeks([undated], actor={"guild_id": 1})

    with vsdb._get_conn() as conn:
        league = conn.execute(
            "SELECT league_id FROM alliance_weeks WHERE week_date IS NULL"
        ).fetchone()[0]
    assert league == _league_of("ABC", MONDAY)


def test_an_undated_row_with_nothing_of_its_own_stays_alone():
    vsdb.record_weeks(_bracket(["ABC"], weeks=1), actor={"guild_id": 1})
    undated = _row("DEF", week=3)
    undated.week_date = None
    vsdb.record_weeks([undated], actor={"guild_id": 2})

    with vsdb._get_conn() as conn:
        league = conn.execute(
            "SELECT league_id FROM alliance_weeks WHERE week_date IS NULL"
        ).fetchone()[0]
    assert league != _league_of("ABC", MONDAY)


# ── Reading a league ──────────────────────────────────────────────────────────


def test_a_league_is_found_by_who_is_in_it_never_by_its_label():
    vsdb.record_weeks(_bracket(["ABC", "DEF"]), actor={"guild_id": 1})
    vsdb.record_weeks(_bracket(["GHI", "JKL"], start=LATER, warzone="1500"), actor={"guild_id": 2})

    found = {r.alliance for r in vsdb.rows_for_bracket([(_key("ABC"), MONDAY)])}
    assert found == {_key("ABC"), _key("DEF")}


def test_a_server_finds_its_league_before_its_own_rows_are_stored():
    """First `/vs` open: the hub reads before it contributes."""
    vsdb.record_weeks(_bracket(["ABC", "DEF"], weeks=1), actor={"guild_id": 1})
    found = {r.alliance for r in vsdb.rows_for_bracket([(_key("DEF"), MONDAY)])}
    assert _key("ABC") in found


# ── Bringing a label-keyed store forward ──────────────────────────────────────


_V1_TABLE = """
    CREATE TABLE alliance_weeks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tag TEXT NOT NULL, warzone TEXT NOT NULL, season TEXT NOT NULL,
        tier TEXT NOT NULL DEFAULT '', grp TEXT NOT NULL DEFAULT '',
        week INTEGER NOT NULL, week_date TEXT, ranking INTEGER,
        tag_display TEXT, warzone_display TEXT, power INTEGER, members INTEGER,
        gift_level INTEGER, opponent_tag TEXT, opponent_warzone TEXT,
        week_score INTEGER, week_outcome TEXT, actor_discord_id TEXT,
        actor_name TEXT, actor_guild_id TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        UNIQUE (tag, warzone, season, tier, grp, week)
    )
"""
_V1_DAYS = """
    CREATE TABLE alliance_week_days (
        week_id INTEGER NOT NULL REFERENCES alliance_weeks(id) ON DELETE CASCADE,
        day INTEGER NOT NULL, score INTEGER, outcome TEXT,
        PRIMARY KEY (week_id, day)
    )
"""


@pytest.fixture
def label_keyed_store(tmp_path, monkeypatch):
    """A store as 1.9.0 left it, with one alliance-week split by two spellings."""
    path = str(tmp_path / "old.sqlite3")
    monkeypatch.setattr(vsdb, "DB_PATH", path)
    conn = sqlite3.connect(path)
    conn.execute(_V1_TABLE)
    conn.execute(_V1_DAYS)
    rows = [
        # id 1: server 1, the old spelling, the earlier write.
        (
            "abc",
            "999",
            "S36",
            "Diamond",
            "12 - 1",
            1,
            MONDAY.isoformat(),
            100,
            None,
            "1",
            "2026-09-08T00:00:00",
            "2026-09-08T00:00:00",
        ),
        # id 2: server 2, the same alliance-week spelled the game's way, later.
        (
            "abc",
            "999",
            "S36",
            "Diamond",
            "12-1",
            1,
            MONDAY.isoformat(),
            None,
            90,
            "2",
            "2026-09-09T00:00:00",
            "2026-09-09T00:00:00",
        ),
        # id 3: the same label a league later, somebody else entirely.
        (
            "ghi",
            "1500",
            "S36",
            "Diamond",
            "12-1",
            1,
            LATER.isoformat(),
            5,
            None,
            "3",
            "2026-11-09T00:00:00",
            "2026-11-09T00:00:00",
        ),
    ]
    conn.executemany(
        "INSERT INTO alliance_weeks (tag, warzone, season, tier, grp, week, week_date, power, "
        "members, actor_guild_id, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.executemany(
        "INSERT INTO alliance_week_days (week_id, day, score, outcome) VALUES (?,?,?,?)",
        [(1, 1, 500, "W"), (2, 2, 700, None)],
    )
    conn.commit()
    conn.close()
    return path


def test_the_rebuild_merges_what_the_label_split_and_keeps_the_days(label_keyed_store):
    vsdb.init_db()

    assert _count() == 2
    abc = vsdb.weeks_for_alliance(_key("ABC"))[0]
    assert (abc["grp"], abc["power"], abc["members"]) == ("12-1", 100, 90)
    # The first server to record it keeps the attribution.
    assert abc["actor_guild_id"] == "1"
    assert abc["day_scores"] == {1: 500, 2: 700}
    assert abc["day_outcomes"] == {1: "W"}


def test_the_rebuild_keeps_a_reused_label_apart(label_keyed_store):
    vsdb.init_db()
    assert _league_of("ABC", MONDAY) != _league_of("GHI", LATER, "1500")
    assert _league_of("ABC", MONDAY) is not None


def test_the_rebuild_runs_once(label_keyed_store):
    vsdb.init_db()
    vsdb.init_db()
    assert _count() == 2
    with vsdb._get_conn() as conn:
        assert vsdb._LABEL_KEYED_MARKER not in vsdb._table_sql(conn, "alliance_weeks")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not any("label_keyed" in t for t in tables)


# ── Stored prompts and announcements (#634's refusal, #409's dedup) ───────────


def test_stored_prompts_and_announcements_take_the_standard_spelling(temp_db):
    import config

    config.init_db()
    with config._get_conn() as conn:
        conn.execute(
            "INSERT INTO vs_score_prompt_posts (guild_id, channel_id, message_id, league_season, "
            "league_tier, league_group, week, duel_day, server_date, posted_at) "
            "VALUES (1, 2, 3, 's36', 'Diamond', '12 - 1', 1, 1, '2026-09-07', 'x')"
        )
        for key in ("S36|Diamond|12 - 1|1|2", "S36|Diamond|12 - 1", "S36|Diamond|12-1"):
            conn.execute(
                "INSERT INTO vs_event_posts (guild_id, kind, event_key, posted_at) "
                "VALUES (1, 'recap', ?, 'x')",
                (key,),
            )
        conn.commit()

    config.init_db()

    with config._get_conn() as conn:
        prompt = conn.execute("SELECT * FROM vs_score_prompt_posts").fetchone()
        keys = sorted(r[0] for r in conn.execute("SELECT event_key FROM vs_event_posts"))
    assert (prompt["league_season"], prompt["league_group"]) == ("S36", "12-1")
    # The old spelling of an already-recorded announcement is dropped, not
    # renamed into a collision.
    assert keys == ["S36|Diamond|12-1", "S36|Diamond|12-1|1|2"]
