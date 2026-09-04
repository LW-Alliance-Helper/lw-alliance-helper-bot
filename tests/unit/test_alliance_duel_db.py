"""VS scores in the bot's own database, keyed to the alliance (#544).

Every score used to live in one alliance's Google Sheet and nowhere else, so
nothing an alliance recorded ever helped another one: sixteen alliances play a
league and each of them learns about the four they personally faced.

What is worth testing here is not that SQLite stores numbers but the rules
around the store, and there are three:

- **Identity is the alliance, never the guild.** Rows survive the Discord
  server that recorded them, which is the entire reason this exists.
- **Nothing overwrites a value with a blank.** The sheet stays authoritative,
  so a screen that happens not to know a number must not erase somebody else's
  record of it.
- **A removal scrubs the attribution and keeps the scores.** The other fifteen
  alliances contributed to the same league.
"""

from __future__ import annotations

import datetime as _dt

import pytest

import alliance_duel as ad
import alliance_duel_db as vsdb

GUILD = 606060
OTHER_GUILD = 707070

LEAGUE = ad.LeagueKey("S35", "Diamond", "12 - 2")
OTHER_LEAGUE = ad.LeagueKey("S36", "Diamond", "12 - 2")
MONDAY = _dt.date(2026, 8, 3)


@pytest.fixture(autouse=True)
def vs_db(tmp_path, monkeypatch):
    monkeypatch.setattr(vsdb, "DB_PATH", str(tmp_path / "alliance_duel.sqlite3"))
    vsdb.init_db()
    return None


def _key(tag, warzone="1234"):
    return ad.AllianceKey.of(tag, warzone)


def _row(tag, week=1, league=LEAGUE, **kw):
    return ad.AllianceWeek(
        league=league,
        week=week,
        alliance=_key(tag),
        week_date=kw.pop("week_date", MONDAY),
        tag_display=kw.pop("tag_display", tag),
        **kw,
    )


def _actor(guild_id=GUILD, discord_id="42", name="Someone"):
    return {"discord_user_id": discord_id, "discord_name": name, "guild_id": guild_id}


def _stored(tag, warzone="1234"):
    found = vsdb.weeks_for_alliance(_key(tag, warzone))
    return found[0] if found else None


# ── Identity ──────────────────────────────────────────────────────────────────


def test_a_score_is_stored_against_the_alliance_not_the_server():
    vsdb.record_weeks([_row("QQQ", week_score=7, week_outcome="W")], actor=_actor())

    row = _stored("QQQ")
    assert row["week_score"] == 7
    assert row["tag"] == "qqq" and row["warzone"] == "1234"


def test_the_same_tag_in_two_warzones_is_two_alliances():
    """`AllianceKey` is (tag, warzone) precisely because a bracket draws from
    more than one warzone. Merging them would pool two alliances' records with
    no way to unpick it afterwards."""
    vsdb.record_weeks(
        [
            _row("KTI", week_score=7),
            ad.AllianceWeek(league=LEAGUE, week=1, alliance=_key("KTI", "5678"), week_score=6),
        ],
        actor=_actor(),
    )

    assert _stored("KTI", "1234")["week_score"] == 7
    assert _stored("KTI", "5678")["week_score"] == 6


def test_a_tag_written_any_of_the_ways_the_game_prints_it_is_one_alliance():
    vsdb.record_weeks([_row("QQQ", week_score=7)], actor=_actor())
    vsdb.record_weeks(
        [ad.AllianceWeek(league=LEAGUE, week=1, alliance=_key("[qqq]"), members=98)],
        actor=_actor(),
    )

    found = vsdb.weeks_for_alliance(_key("QQQ"))
    assert len(found) == 1, "the same alliance was stored twice"
    assert found[0]["week_score"] == 7 and found[0]["members"] == 98


def test_a_row_with_no_usable_identity_is_skipped_rather_than_raised_on():
    """This runs behind an entry surface that already refused what it could.
    One unreadable row must not cost the officer the fifteen good ones."""
    good = _row("QQQ", week_score=7)
    bad = ad.AllianceWeek(league=LEAGUE, week=1, alliance=None)

    result = vsdb.record_weeks([good, bad, _row("ZZZ", week_score=6)], actor=_actor())

    assert result == {"written": 2, "skipped": 1}


# ── The sheet stays authoritative ─────────────────────────────────────────────


def test_a_second_write_that_knows_less_does_not_erase_what_the_first_knew():
    vsdb.record_weeks([_row("QQQ", week_score=7, power=5_000_000, members=100)], actor=_actor())
    vsdb.record_weeks([_row("QQQ", week_outcome="W")], actor=_actor())

    row = _stored("QQQ")
    assert row["week_score"] == 7, "a blank overwrote a recorded score"
    assert row["power"] == 5_000_000
    assert row["members"] == 100
    assert row["week_outcome"] == "W"


def test_a_later_reading_replaces_an_earlier_one_for_the_same_field():
    """Non-clobbering is about blanks, not about staleness. A newer number for
    the same alliance-week is a correction and must win."""
    vsdb.record_weeks([_row("QQQ", power=5_000_000)], actor=_actor())
    vsdb.record_weeks([_row("QQQ", power=5_400_000)], actor=_actor())

    assert _stored("QQQ")["power"] == 5_400_000


def test_each_league_week_is_its_own_row():
    for week in (1, 2, 3):
        vsdb.record_weeks([_row("QQQ", week=week, week_score=week)], actor=_actor())
    vsdb.record_weeks([_row("QQQ", week=1, league=OTHER_LEAGUE, week_score=9)], actor=_actor())

    found = vsdb.weeks_for_alliance(_key("QQQ"))
    assert len(found) == 4
    assert {r["week_score"] for r in found} == {1, 2, 3, 9}


# ── Days ──────────────────────────────────────────────────────────────────────


def test_day_scores_and_outcomes_round_trip():
    vsdb.record_weeks(
        [_row("QQQ", day_scores={1: 120, 2: 95}, day_outcomes={1: "W", 2: "L"})],
        actor=_actor(),
    )

    row = _stored("QQQ")
    assert row["day_scores"] == {1: 120, 2: 95}
    assert row["day_outcomes"] == {1: "W", 2: "L"}


def test_a_day_nobody_mentioned_is_absent_rather_than_zero():
    """A blank cell must not render as a zero. Day 3 unrecorded is a real state
    and it is not a score of nothing."""
    vsdb.record_weeks([_row("QQQ", day_scores={1: 120})], actor=_actor())

    row = _stored("QQQ")
    assert 3 not in row["day_scores"]
    assert row["day_scores"] == {1: 120}


def test_a_later_day_write_leaves_the_days_it_says_nothing_about_alone():
    vsdb.record_weeks([_row("QQQ", day_scores={1: 120, 2: 95})], actor=_actor())
    vsdb.record_weeks([_row("QQQ", day_scores={3: 88})], actor=_actor())

    assert _stored("QQQ")["day_scores"] == {1: 120, 2: 95, 3: 88}


def test_an_outcome_can_land_after_the_score_without_replacing_it():
    """The screens record them independently: a score arrives the evening it is
    played and the outcome can arrive a day later."""
    vsdb.record_weeks([_row("QQQ", day_scores={1: 120})], actor=_actor())
    vsdb.record_weeks([_row("QQQ", day_outcomes={1: "W"})], actor=_actor())

    row = _stored("QQQ")
    assert row["day_scores"] == {1: 120}
    assert row["day_outcomes"] == {1: "W"}


# ── Reading ───────────────────────────────────────────────────────────────────


def test_a_league_reads_back_every_alliance_in_it():
    """The point of the feature: an alliance can now see what fifteen others
    recorded, not just the four it played."""
    vsdb.record_weeks(
        [_row(tag, ranking=i) for i, tag in enumerate(["AAA", "BBB", "CCC"], start=1)],
        actor=_actor(),
    )

    found = vsdb.weeks_for_league(LEAGUE, week=1)
    assert [r["tag"] for r in found] == ["aaa", "bbb", "ccc"]


def test_an_alliance_nobody_recorded_reads_as_nothing_rather_than_an_error():
    assert vsdb.weeks_for_alliance(_key("NOPE")) == []
    assert vsdb.weeks_for_league(OTHER_LEAGUE) == []


# ── Guild removal (#543) ──────────────────────────────────────────────────────


def test_a_removal_keeps_the_scores_and_loses_the_server():
    vsdb.record_weeks(
        [_row("QQQ", week_score=7, day_scores={1: 120})],
        actor=_actor(guild_id=GUILD),
    )

    result = vsdb.purge_guild_data(GUILD, apply=True)

    assert result["scrubbed"].get("alliance_weeks") == 1
    assert result["deleted"] == {}, "a score was deleted; the league is not this server's"
    row = _stored("QQQ")
    assert row["week_score"] == 7, "the reading went with the attribution"
    assert row["day_scores"] == {1: 120}
    assert row["actor_guild_id"] is None
    assert row["actor_discord_id"] is None
    assert row["actor_name"] is None


def test_another_servers_contribution_is_untouched():
    vsdb.record_weeks([_row("QQQ", week_score=7)], actor=_actor(guild_id=GUILD))
    vsdb.record_weeks([_row("ZZZ", week_score=6)], actor=_actor(guild_id=OTHER_GUILD))

    vsdb.purge_guild_data(GUILD, apply=True)

    assert _stored("ZZZ")["actor_guild_id"] == str(OTHER_GUILD)


def test_the_dry_run_counts_and_changes_nothing():
    vsdb.record_weeks([_row("QQQ", week_score=7)], actor=_actor(guild_id=GUILD))

    preview = vsdb.purge_guild_data(GUILD, apply=False)

    assert preview["scrubbed"].get("alliance_weeks") == 1
    assert preview["applied"] is False
    assert _stored("QQQ")["actor_guild_id"] == str(GUILD)


def test_the_preview_and_the_run_agree():
    for tag in ("AAA", "BBB", "CCC"):
        vsdb.record_weeks([_row(tag, week_score=7)], actor=_actor(guild_id=GUILD))

    preview = vsdb.purge_guild_data(GUILD, apply=False)
    run = vsdb.purge_guild_data(GUILD, apply=True)

    assert preview["scrubbed"] == run["scrubbed"]
    assert preview["deleted"] == run["deleted"]


def test_a_row_can_live_with_no_attribution_at_all():
    """A backfill has no author, and the scrub leaves rows in exactly that
    state. If an unattributed row could not exist, a removal would be creating
    an impossible one."""
    vsdb.record_weeks([_row("QQQ", week_score=7)], actor=None)

    row = _stored("QQQ")
    assert row["week_score"] == 7
    assert row["actor_guild_id"] is None


def test_a_scrubbed_row_survives_a_second_sweep():
    vsdb.record_weeks([_row("QQQ", week_score=7)], actor=_actor(guild_id=GUILD))
    vsdb.purge_guild_data(GUILD, apply=True)

    again = vsdb.purge_guild_data(GUILD, apply=True)

    assert again["scrubbed"] == {}
    assert _stored("QQQ")["week_score"] == 7


# ── The spec matches the schema ───────────────────────────────────────────────


def test_the_spec_covers_every_table_that_names_a_server():
    """The same guard both other databases carry. A spec compiled by hand goes
    stale the first time somebody adds a table, so this asks the schema."""
    with vsdb._get_conn() as conn:
        live = [
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        ]

        def columns_of(table):
            return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}

        attributed = {t for t in live if any(c.endswith("guild_id") for c in columns_of(t))}

    named = {t for t, _ in vsdb._GUILD_REMOVAL_DELETES} | {
        t for t, _, _ in vsdb._GUILD_REMOVAL_SCRUBS
    }
    missed = attributed - named
    assert not missed, f"tables naming a server that the removal ignores: {sorted(missed)}"


def test_every_table_the_spec_names_exists():
    with vsdb._get_conn() as conn:
        live = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    named = {t for t, _ in vsdb._GUILD_REMOVAL_DELETES} | {
        t for t, _, _ in vsdb._GUILD_REMOVAL_SCRUBS
    }
    assert named <= live, f"names tables that do not exist: {sorted(named - live)}"


def test_init_db_is_rerunnable():
    vsdb.record_weeks([_row("QQQ", week_score=7)], actor=_actor())
    vsdb.init_db()

    assert _stored("QQQ")["week_score"] == 7
