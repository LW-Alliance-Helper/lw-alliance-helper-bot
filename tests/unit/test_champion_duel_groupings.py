"""Groupings: the 16 warzones drawn together.

Champion Duel timing and structure are per grouping, not global. Everything
built before this assumed there was one, which was true of the imported draw
and false as a product: about 50 alliances use the bot and that draw covers
roughly two of them.

The failure being prevented is silent. An officer in warzone 1500 recording an
opponent as "Group D" landed that player in the imported grouping's Group D,
because a group letter was a bare TEXT meaning the same thing everywhere.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

import champion_duel_db as db


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return None


def started_so_today_is(phase: str) -> str:
    """A start date putting today in `phase`, computed rather than hardcoded so
    these do not start failing on a date nobody chose."""
    first_day = {key: first for key, first, _ in db.PHASES}[phase]
    return (db._server_today() - timedelta(days=first_day)).isoformat()


# ── Identity ──────────────────────────────────────────────────────────────────


def test_the_warzone_line_parses_the_way_the_game_prints_it(cd_db):
    """Copied off a phone screen. Rejecting it over a separator would be a
    validation failure with nothing wrong behind it."""
    line = "#773 , #800 , #744 , #677 , #681 , #804 , #699 , #736"
    assert db.parse_warzones(line) == ["677", "681", "699", "736", "744", "773", "800", "804"]

    # Same sixteen, any way they are typed.
    assert db.parse_warzones("800 773") == db.parse_warzones("#773,#800")


def test_the_set_is_the_identity_not_the_order(cd_db):
    """The game lists them in an arbitrary order, so two people entering the
    same grouping must not produce two groupings."""
    a = db.create_grouping("773, 800, 744", started_so_today_is("qualifiers"))
    assert a["warzones"] == ["744", "773", "800"]


def test_a_warzone_resolves_to_its_grouping(cd_db):
    """One number a member knows without looking anything up. A warzone is in
    at most one grouping per Champion Duel, so it is enough on its own."""
    made = db.create_grouping("773, 800, 744", started_so_today_is("qualifiers"))

    assert db.find_grouping_by_warzone("800")["id"] == made["id"]
    assert db.find_grouping_by_warzone("#800")["id"] == made["id"]
    assert db.find_grouping_by_warzone(800)["id"] == made["id"]
    assert db.find_grouping_by_warzone("1500") is None


def test_repeats_survive_when_validation_asks_for_them(cd_db):
    """Sixteen numbers with one typed twice dedupe to sixteen, and the count
    check would then pass on a grouping that is short one warzone."""
    assert db.parse_warzones("773 800 773") == ["773", "800"]
    assert db.parse_warzones("773 800 773", unique=False) == ["773", "800", "773"]


def test_a_warzone_in_two_concurrent_groupings_is_a_contradiction(cd_db):
    """A warzone is drawn into exactly one grouping per Champion Duel, so an
    overlap that is not the whole set means one of the two entries is wrong."""
    mine = db.create_grouping("773, 800, 744", started_so_today_is("qualifiers"))

    overlaps = db.overlapping_groupings(["800", "1500"], started_so_today_is("qualifiers"))

    assert [(g["id"], z) for g, z in overlaps] == [(mine["id"], "800")]


def test_the_same_sixteen_next_season_is_not_a_conflict(cd_db):
    """Groupings a whole event apart are different Champion Duels. They share
    warzones by design, and refusing that would block every new season."""
    db.create_grouping("773, 800", "2026-01-01")

    assert db.overlapping_groupings(["773", "800"], "2026-08-04") == []


def test_an_overlap_stands_when_either_side_has_no_dates(cd_db):
    """We cannot show the two are separate events, and a false stop costs one
    message where a false pass costs a grouping nobody can untangle."""
    db.create_grouping("773, 800")  # imported before anyone read its dates

    overlaps = db.overlapping_groupings(["773", "1500"], "2026-08-04")

    assert [z for _, z in overlaps] == ["773"]


# ── Reading a pasted group listing ────────────────────────────────────────────


def test_a_score_keeps_its_thousands_separators(cd_db):
    """The whole reason for splitting on three commas. People type `33,500,000`
    because that is what the card shows, and a naive split makes it 33."""
    row = db.parse_placement_line("Kestrel, 738, 1, 33,500,000")

    assert row["name"] == "Kestrel"
    assert row["server"] == "738"
    assert row["rank"] == 1
    assert row["score"] == 33_500_000
    assert row["problem"] is None


def test_a_line_can_stop_early(cd_db):
    """Only the name is required. An alliance recording just its own members'
    placements knows the rank and often not the score."""
    assert db.parse_placement_line("Wren")["name"] == "Wren"
    assert db.parse_placement_line("Wren")["server"] is None

    partial = db.parse_placement_line("Wren, 744, 25")
    assert (partial["server"], partial["rank"], partial["score"]) == ("744", 25, None)


def test_the_alliance_tag_is_kept_rather_than_discarded(cd_db):
    """`normalize_name` already ignores it for matching, so this is only about
    not throwing away the one field we would otherwise have to ask for."""
    row = db.parse_placement_line("[OGV]Kestrel, 738, 1")

    assert row["name"] == "Kestrel"
    assert row["alliance"] == "OGV"


def test_the_game_formatting_is_tolerated(cd_db):
    row = db.parse_placement_line("Kestrel, #738, 1")
    assert row["server"] == "738"


def test_a_name_with_a_comma_is_flagged_not_mangled(cd_db):
    """Its second half lands in the warzone slot. That is not recoverable here,
    so it goes to the reconcile view for a human rather than being guessed at."""
    row = db.parse_placement_line("Smith, Jr, 738, 1")

    assert row["problem"] == "bad_server"
    assert row["server"] is None


def test_non_numeric_ranks_and_scores_are_flagged(cd_db):
    assert db.parse_placement_line("Wren, 744, first")["problem"] == "bad_rank"
    assert db.parse_placement_line("Wren, 744, 1, lots")["problem"] == "bad_score"


def test_a_paste_drops_blank_lines_and_keeps_the_rest(cd_db):
    rows = db.parse_placement_lines("Kestrel, 738, 1\n\n  \nWren, 744, 25\n")

    assert [r["name"] for r in rows] == ["Kestrel", "Wren"]


# ── The collision this exists to stop ─────────────────────────────────────────


def test_two_groupings_can_both_have_a_group_d(cd_db):
    """The whole point. A group letter is not an identity; the row is."""
    mine = db.create_grouping("738, 800", started_so_today_is("semifinals"))
    theirs = db.create_grouping("1500, 1501", started_so_today_is("semifinals"))

    db.import_registrants([{"name": "AlphaOne", "server": "738"}])
    db.import_registrants([{"name": "Stranger", "server": "1500"}])
    alpha = db.resolve_registrant("AlphaOne", "738")["id"]
    stranger = db.resolve_registrant("Stranger", "1500")["id"]

    db.set_stage(alpha, "semifinals", grp="D", rank=1, grouping_id=mine["id"])
    db.set_stage(stranger, "semifinals", grp="D", rank=1, grouping_id=theirs["id"])

    mine_d = db.get_roster(group="D", grouping_id=mine["id"], stage="semifinals")
    theirs_d = db.get_roster(group="D", grouping_id=theirs["id"], stage="semifinals")

    assert [p["display_name"] for p in mine_d] == ["AlphaOne"]
    assert [p["display_name"] for p in theirs_d] == ["Stranger"]


def test_group_counts_do_not_span_groupings(cd_db):
    """A count over every grouping describes several tournaments at once and
    belongs to none of them."""
    mine = db.create_grouping("738", started_so_today_is("qualifiers"))
    theirs = db.create_grouping("1500", started_so_today_is("qualifiers"))
    db.import_registrants(
        [{"name": "AlphaOne", "group": "M", "server": "738"}],
        stage="qualifiers",
        grouping_id=mine["id"],
    )
    db.import_registrants(
        [{"name": "Stranger", "group": "M", "server": "1500"}],
        stage="qualifiers",
        grouping_id=theirs["id"],
    )

    assert db.get_groups(grouping_id=mine["id"]) == [{"group": "M", "registrants": 1}]
    assert db.get_groups(grouping_id=theirs["id"]) == [{"group": "M", "registrants": 1}]


def test_a_round_without_a_resolvable_grouping_is_refused(cd_db):
    """Guessing files 1600 players into another alliance's tournament."""
    db.create_grouping("738", started_so_today_is("qualifiers"))
    db.create_grouping("1500", started_so_today_is("qualifiers"))

    with pytest.raises(ValueError, match="grouping"):
        db.import_registrants([{"name": "Nobody", "group": "M"}], stage="qualifiers")


# ── Resolution ────────────────────────────────────────────────────────────────


def test_a_payload_finds_its_own_grouping_by_warzone(cd_db):
    """A semifinal payload carries the same warzones as its qualifier draw but
    only the advancers, so matching on the exact set would fork a second
    grouping over one event every time."""
    first = db.import_registrants(
        [
            {"name": "A", "group": "M", "server": "738"},
            {"name": "B", "group": "M", "server": "800"},
        ],
        stage="qualifiers",
    )
    second = db.import_registrants(
        [{"name": "A", "group": "D", "server": "738"}], stage="semifinals"
    )

    assert second["grouping_id"] == first["grouping_id"]
    assert len(db.list_groupings()) == 1


def test_a_guild_resolves_through_its_own_warzone(cd_db):
    made = db.create_grouping("738, 800", started_so_today_is("qualifiers"))
    db.set_guild_warzone("999", "738", discord_id="111")

    assert db.resolve_grouping_for_guild("999")["id"] == made["id"]


def test_a_map_manager_link_resolves_without_asking(cd_db):
    """`guild_alliance_mappings.server` is an INTEGER there and TEXT here, and
    the boundary is the kind of thing that silently matches nothing."""
    made = db.create_grouping("738, 800", started_so_today_is("qualifiers"))

    resolved = db.resolve_grouping_for_guild("999", fallback_warzone=738)

    assert resolved["id"] == made["id"]
    assert db.get_guild_warzone("999") is None, "an inference is not a pin"


def test_the_guilds_own_answer_beats_an_inference(cd_db):
    mine = db.create_grouping("738", started_so_today_is("qualifiers"))
    db.create_grouping("1500", started_so_today_is("qualifiers"))
    db.set_guild_warzone("999", "738")

    assert db.resolve_grouping_for_guild("999", fallback_warzone=1500)["id"] == mine["id"]


def test_an_unknown_warzone_resolves_to_nothing_rather_than_guessing(cd_db):
    """The normal state for a new alliance: their grouping does not exist until
    somebody enters it."""
    db.create_grouping("738", started_so_today_is("qualifiers"))

    assert db.resolve_grouping_for_guild("999", fallback_warzone=1500) is None


def test_a_warzone_is_confirmed_once_per_champion_duel(cd_db):
    """An alliance that moves warzone still resolves, silently and wrongly: the
    old number keeps being drawn into somebody's grouping. So the answer is
    re-confirmed per grouping rather than trusted forever."""
    first = db.create_grouping("738", started_so_today_is("results"))
    db.set_guild_warzone("999", "738", confirmed_grouping_id=first["id"])
    assert db.needs_warzone_confirmation("999", first["id"]) is False

    next_duel = db.create_grouping("738, 900", started_so_today_is("signup"))
    assert db.needs_warzone_confirmation("999", next_duel["id"]) is True

    db.set_guild_warzone("999", "738", confirmed_grouping_id=next_duel["id"])
    assert db.needs_warzone_confirmation("999", next_duel["id"]) is False


def test_next_season_resolves_itself_once_somebody_enters_it(cd_db):
    """Why the guild's *warzone* is stored rather than its grouping: nothing
    needs re-pinning when the event comes round again."""
    db.set_guild_warzone("999", "738")
    assert db.resolve_grouping_for_guild("999") is None

    new_duel = db.create_grouping("738, 1200", started_so_today_is("signup"))

    assert db.resolve_grouping_for_guild("999")["id"] == new_duel["id"]


# ── The draw and the standings are different numbers ──────────────────────────


def test_recording_the_standings_does_not_erase_the_draw(cd_db):
    """Every player has a rank from the moment a group is drawn -- the seed
    position -- and a different one after it is played. Writing one must never
    destroy the other; that is the same failure groupings exist to stop."""
    made = db.create_grouping("738", started_so_today_is("semifinals"))
    db.import_registrants([{"name": "AlphaOne", "server": "738"}])
    rid = db.resolve_registrant("AlphaOne", "738")["id"]
    group = db.get_or_create_group(made["id"], "semifinals", "D")

    db.set_placement(group["id"], rid, seed_rank=3, recording="draw")
    db.set_placement(group["id"], rid, rank=22, score=15_900_000, recording="final")

    row = db.get_group_members(group["id"])[0]
    assert row["seed_rank"] == 3
    assert row["rank"] == 22
    assert row["score"] == 15_900_000


def test_a_second_entry_that_knows_less_does_not_blank_what_we_had(cd_db):
    made = db.create_grouping("738", started_so_today_is("semifinals"))
    db.import_registrants([{"name": "AlphaOne", "server": "738"}])
    rid = db.resolve_registrant("AlphaOne", "738")["id"]
    group = db.get_or_create_group(made["id"], "semifinals", "D")
    db.set_placement(group["id"], rid, seed_rank=3, rank=1, score=40_000_000)

    db.set_placement(group["id"], rid)

    row = db.get_group_members(group["id"])[0]
    assert (row["seed_rank"], row["rank"], row["score"]) == (3, 1, 40_000_000)


# ── Migration ─────────────────────────────────────────────────────────────────


def test_the_pre_grouping_draw_becomes_a_real_grouping(tmp_path, monkeypatch):
    """Everything imported before groupings existed belongs to one, because a
    grouping is what the importer had no concept of."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "legacy.sqlite3"))
    db.init_db()
    with db._get_conn() as conn:
        conn.execute("DELETE FROM groupings")
        for i, (name, server) in enumerate(
            [("AlphaOne", "738"), ("BetaTwo", "800"), ("Stranger", "1500")]
        ):
            origin = "self_reported" if name == "Stranger" else "imported"
            conn.execute(
                "INSERT INTO registrants (player_key, display_name, server, grp, rank, "
                "origin, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (name.lower(), name, server, "M", i + 1, origin, "2026-08-01", "2026-08-01"),
            )

    db.init_db()

    groupings = db.list_groupings()
    assert len(groupings) == 1
    # Seeded from imported registrants only. A self-reported row carries the
    # warzone of whoever met them, and pulling it in would make another
    # alliance's warzone resolve to this grouping forever.
    assert groupings[0]["warzones"] == ["738", "800"]
    assert groupings[0]["origin"] == "imported"

    placed = db.get_groups(stage="qualifiers", grouping_id=groupings[0]["id"])
    assert placed == [{"group": "M", "registrants": 2}], "the self-reported placement is dropped"


def test_the_migration_runs_once(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "once.sqlite3"))
    db.init_db()
    db.import_registrants([{"name": "AlphaOne", "group": "M", "server": "738"}], stage="qualifiers")

    db.init_db()
    db.init_db()

    assert len(db.list_groupings()) == 1
