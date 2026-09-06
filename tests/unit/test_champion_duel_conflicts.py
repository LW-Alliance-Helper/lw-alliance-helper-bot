"""Resolving two groupings that claim the same warzone.

A warzone is drawn into exactly one set of Participating Warzones per Champion
Duel, so two groupings sharing one inside an event window is a contradiction.
The member-facing half shipped first: both lists side by side, a retry for the
caller's own, and for the other one an instruction to reach us on the Community
Server. Nothing was behind that instruction.

The merge is the one operation here that cannot be undone, so most of what
follows is about what it must not destroy.
"""

from __future__ import annotations

import pytest

import champion_duel_db as db


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return None


def _sixteen(first: int) -> list[str]:
    return [str(first + i) for i in range(16)]


def _conflicting_pair(started="2026-08-04"):
    """Two groupings sharing one warzone, inside one event window."""
    a = db.create_grouping(_sixteen(700), started, origin="member")
    b = db.create_grouping(["715"] + _sixteen(900)[:15], started, origin="member")
    return a, b


# ── Detection ─────────────────────────────────────────────────────────────────


def test_two_groupings_sharing_a_warzone_are_a_conflict(cd_db):
    a, b = _conflicting_pair()

    conflicts = db.find_grouping_conflicts()

    assert len(conflicts) == 1
    assert {conflicts[0]["a"]["id"], conflicts[0]["b"]["id"]} == {a["id"], b["id"]}
    assert conflicts[0]["shared"] == ["715"]


def test_the_same_sixteen_entered_twice_is_agreement_not_a_conflict(cd_db):
    """Two people entering the same set is the normal way a second alliance
    joins one. The entry path already folds them together."""
    db.create_grouping(_sixteen(700), "2026-08-04", origin="member")
    db.create_grouping(_sixteen(700), "2026-08-04", origin="member")

    assert db.find_grouping_conflicts() == []


def test_the_same_warzone_next_season_is_not_a_conflict(cd_db):
    """A warzone is redrawn every Champion Duel by design. Flagging that would
    report a conflict on every alliance, every season."""
    db.create_grouping(_sixteen(700), "2026-06-02", origin="member")
    db.create_grouping(["715"] + _sixteen(900)[:15], "2026-08-04", origin="member")

    assert db.find_grouping_conflicts() == []


def test_a_conflict_carries_the_counts_the_decision_turns_on(cd_db):
    """The operator's whole question is which to keep, and that turns on which
    holds real data rather than on which was entered first."""
    a, b = _conflicting_pair()
    group = db.get_or_create_group(a["id"], "semifinals", "H")
    reg = db.upsert_registrant("Alpha", server="700")
    db.set_placement(group["id"], reg["id"], seed_rank=1, rank=1)

    pair = db.find_grouping_conflicts()[0]
    side = "a" if pair["a"]["id"] == a["id"] else "b"

    assert pair[f"{side}_counts"] == {
        "groups": 1,
        "players": 1,
        "results": 1,
        "guilds": 0,
    }


# ── Merging ───────────────────────────────────────────────────────────────────


def test_a_merge_moves_players_the_target_does_not_have(cd_db):
    a, b = _conflicting_pair()
    keep = db.get_or_create_group(a["id"], "semifinals", "H")
    fold = db.get_or_create_group(b["id"], "semifinals", "H")
    kept = db.upsert_registrant("Alpha", server="700")
    moved = db.upsert_registrant("Beta", server="900")
    db.set_placement(keep["id"], kept["id"], seed_rank=1)
    db.set_placement(fold["id"], moved["id"], seed_rank=2)

    result = db.merge_groupings(b["id"], a["id"], actor="tester")

    names = {m["display_name"] for m in db.get_group_members(keep["id"])}
    assert names == {"Alpha", "Beta"}
    assert result["players"] == 1


def test_a_merge_never_overwrites_the_grouping_being_kept(cd_db):
    """The operator chose which one survives. A merge that rewrote values
    inside it would make that choice mean something other than what it said."""
    a, b = _conflicting_pair()
    keep = db.get_or_create_group(a["id"], "semifinals", "H")
    fold = db.get_or_create_group(b["id"], "semifinals", "H")
    reg = db.upsert_registrant("Alpha", server="700")
    db.set_placement(keep["id"], reg["id"], seed_rank=1, rank=1)
    db.set_placement(fold["id"], reg["id"], seed_rank=8, rank=8)

    result = db.merge_groupings(b["id"], a["id"], actor="tester")

    row = db.get_group_members(keep["id"])[0]
    assert (row["seed_rank"], row["rank"]) == (1, 1)
    assert result["unchanged"] == 1


def test_a_merge_does_not_glue_the_wrong_list_onto_the_survivor(cd_db):
    """The premise of a conflict is that one of the two lists is wrong, so
    folding it in keeps the wrong answer. A union would also break the
    16-warzone invariant the entry path enforces and every member surface
    renders against, and would raise a fresh conflict the moment the alliance
    genuinely drawn into those warzones entered its real set."""
    a, b = _conflicting_pair()

    result = db.merge_groupings(b["id"], a["id"], actor="tester")

    kept = db.get_grouping(a["id"])["warzones"]
    assert sorted(kept, key=int) == sorted(_sixteen(700), key=int)
    assert len(kept) == db.GROUPING_SIZE
    assert result["dropped_warzones"] == sorted(_sixteen(900)[:15], key=int)


def test_the_source_fills_gaps_the_target_left_empty(cd_db):
    """The case that makes a merge worth doing, and the one a row-at-a-time
    merge silently destroys: the target holds the draw, the source holds the
    standings. Dropping the source's row would throw the results away, and
    this cannot be undone."""
    a, b = _conflicting_pair()
    keep = db.get_or_create_group(a["id"], "semifinals", "H")
    fold = db.get_or_create_group(b["id"], "semifinals", "H")
    reg = db.upsert_registrant("Alpha", server="700")
    db.set_placement(keep["id"], reg["id"], seed_rank=1)
    db.set_placement(fold["id"], reg["id"], rank=3, score=1234)

    result = db.merge_groupings(b["id"], a["id"], actor="tester")

    row = db.get_group_members(keep["id"])[0]
    assert (row["seed_rank"], row["rank"], row["score"]) == (1, 3, 1234)
    assert result["filled"] == 1


def test_a_guild_the_survivor_actually_contains_is_repointed(cd_db):
    """A guild pinned to the grouping that disappears would resolve to nothing
    and silently lose its Champion Duel."""
    a, b = _conflicting_pair()
    db.set_guild_warzone("999", "715", confirmed_grouping_id=b["id"])

    result = db.merge_groupings(b["id"], a["id"], actor="tester")

    assert result["guilds"] == 1
    assert db.get_guild_warzone("999")["confirmed_grouping_id"] == a["id"]


def test_a_guild_the_survivor_does_not_contain_is_unpinned_not_repointed(cd_db):
    """Their warzone was in the list that turned out to be wrong. Pointing them
    at a Champion Duel they are not in would be a second wrong answer dressed
    as a fix. Clearing the pin lets them re-resolve by warzone on the next
    read, which is the self-healing path the column sits beside."""
    a, b = _conflicting_pair()
    db.set_guild_warzone("999", "915", confirmed_grouping_id=b["id"])

    result = db.merge_groupings(b["id"], a["id"], actor="tester")

    assert (result["guilds"], result["unpinned"]) == (0, 1)
    assert db.get_guild_warzone("999")["confirmed_grouping_id"] is None


def test_merging_knockouts_does_not_leave_a_stray_empty_field(cd_db):
    """The knockouts have no letter, and SQLite treats every NULL in a UNIQUE
    index as distinct, so the old `INSERT OR IGNORE` never collided there. Each
    call made another knockout row and an unordered read decided which one a
    placement landed in."""
    a, b = _conflicting_pair()
    db.get_or_create_group(a["id"], "knockouts", None)
    fold = db.get_or_create_group(b["id"], "knockouts", None)
    reg = db.upsert_registrant("Beta", server="900")
    db.set_placement(fold["id"], reg["id"], rank=9)

    db.merge_groupings(b["id"], a["id"], actor="tester")

    landed = db.get_or_create_group(a["id"], "knockouts", None)
    assert [m["display_name"] for m in db.get_group_members(landed["id"])] == ["Beta"]
    assert db.recorded_stages(a["id"]) == ["knockouts"]


def test_asking_for_the_same_knockout_field_twice_returns_one_row(cd_db):
    """Same NULL-label flaw, at its source. Every caller of the group view and
    every knockout placement goes through here."""
    a, _ = _conflicting_pair()

    first = db.get_or_create_group(a["id"], "knockouts", None)
    second = db.get_or_create_group(a["id"], "knockouts", None)

    assert first["id"] == second["id"]


def test_a_merge_leaves_nothing_of_the_grouping_it_folded(cd_db):
    a, b = _conflicting_pair()
    fold = db.get_or_create_group(b["id"], "semifinals", "H")
    reg = db.upsert_registrant("Beta", server="900")
    db.set_placement(fold["id"], reg["id"], seed_rank=2)

    db.merge_groupings(b["id"], a["id"], actor="tester")

    assert db.get_grouping(b["id"]) is None
    assert db.get_group_members(fold["id"]) == []


def test_a_merge_carries_the_servers_that_can_read_it(cd_db):
    """A server that was sent a Champion Duel has no warzone in it, so its
    `grouping_readers` row is the only path back. `ON DELETE CASCADE` would
    take that silently, which is the dead end the table exists to close."""
    a, b = _conflicting_pair()
    db.note_grouping_reader(b["id"], "999")
    db.note_grouping_reader(a["id"], "888")

    moved = db.merge_groupings(b["id"], a["id"], actor="tester")

    assert moved["readers"] == 1
    assert [g["id"] for g in db.groupings_readable_by(None, "999")] == [a["id"]]
    assert [g["id"] for g in db.groupings_readable_by(None, "888")] == [a["id"]]


def test_a_merge_does_not_duplicate_a_server_that_could_read_both(cd_db):
    """Sent both, or in one and sent the other. Two sources, one row."""
    a, b = _conflicting_pair()
    db.note_grouping_reader(a["id"], "999")
    db.note_grouping_reader(b["id"], "999")

    moved = db.merge_groupings(b["id"], a["id"], actor="tester")

    assert moved["readers"] == 0, "already a reader of the survivor"
    assert [g["id"] for g in db.groupings_readable_by(None, "999")] == [a["id"]]


def test_a_merge_resolves_the_conflict_it_was_called_for(cd_db):
    a, b = _conflicting_pair()

    db.merge_groupings(b["id"], a["id"], actor="tester")

    assert db.find_grouping_conflicts() == []


def test_rounds_only_one_side_played_survive_the_merge(cd_db):
    """The folded grouping may hold a whole round the kept one never had. That
    is exactly the data a merge exists to preserve."""
    a, b = _conflicting_pair()
    db.get_or_create_group(a["id"], "semifinals", "H")
    fold = db.get_or_create_group(b["id"], "qualifiers", "M")
    reg = db.upsert_registrant("Beta", server="900")
    db.set_placement(fold["id"], reg["id"], seed_rank=3)

    db.merge_groupings(b["id"], a["id"], actor="tester")

    assert db.recorded_stages(a["id"]) == ["qualifiers", "semifinals"]
    landed = db.get_or_create_group(a["id"], "qualifiers", "M")
    assert [m["display_name"] for m in db.get_group_members(landed["id"])] == ["Beta"]


def test_a_grouping_cannot_be_merged_into_itself(cd_db):
    a, _ = _conflicting_pair()

    with pytest.raises(db.MergeRefused):
        db.merge_groupings(a["id"], a["id"], actor="tester")


def test_merging_something_already_gone_refuses_rather_than_half_running(cd_db):
    """Two operators on the same conflict list is the case that produces this,
    and a half-applied merge is worse than a refusal."""
    a, b = _conflicting_pair()
    db.merge_groupings(b["id"], a["id"], actor="first")

    with pytest.raises(db.MergeRefused):
        db.merge_groupings(b["id"], a["id"], actor="second")
