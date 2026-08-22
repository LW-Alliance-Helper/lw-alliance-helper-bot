"""Champion Duel side of a data-removal request (#517).

The privacy policy says people can ask for their information to be removed.
Until this, nothing in this database could action that except by hand-editing
SQLite, and hand-editing is not a promise anyone should be making.

Two shapes, and which one a table gets is decided by what its rows are. A
reading somebody contributed keeps the reading and loses the name on it -- the
game shows that reading to anyone, and deleting it would take it from the
alliances it was contributed for. A row that only says "this person signed in"
has nothing left after a scrub, so it goes.

Champion Duel *player* records are out of scope by the #499 decision. These
tests assert that too: a removal must not touch `registrants.display_name`, or
a Discord-keyed request would start deleting players who never used the bot.
"""

from __future__ import annotations

import re

import pytest

import champion_duel_db as db

REQUESTER = "5150"
BYSTANDER = "9001"


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return None


def actor(discord_id, name="Scout", guild_id="777"):
    return {"discord_user_id": discord_id, "discord_name": name, "guild_id": guild_id}


def rows(table, where="", params=()):
    with db._get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM {table} {('WHERE ' + where) if where else ''}",  # noqa: S608
                params,
            ).fetchall()
        ]


# ── The spec matches the schema ───────────────────────────────────────────────
#
# The operating rule was compiled by reading the tree, and reading is not the
# same as checking. A wrong column name in a delete path is the worst possible
# place for one: SQLite would raise on a live removal, halfway through.

_IDENT = re.compile(r"\b([a-z_][a-z0-9_]*)\s*(?==)")


def columns_of(table):
    with db._get_conn() as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_every_table_the_removal_names_exists(cd_db):
    with db._get_conn() as conn:
        live = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    named = {t for t, _ in db._REMOVAL_DELETES} | {t for t, _, _ in db._REMOVAL_SCRUBS}
    assert named <= live, f"removal names tables that do not exist: {sorted(named - live)}"


def test_every_column_the_removal_writes_or_reads_exists(cd_db):
    """Both halves of every scrub, and every delete predicate. `CASE WHEN` and
    bare literals fall out of the identifier match, which is why the pattern
    anchors on an assignment or comparison rather than on any word."""
    checked = 0
    for table, where in db._REMOVAL_DELETES:
        live = columns_of(table)
        for name in _IDENT.findall(where):
            assert name in live, f"{table}.{name} does not exist"
            checked += 1
    for table, sets, where in db._REMOVAL_SCRUBS:
        live = columns_of(table)
        for name in _IDENT.findall(sets) + _IDENT.findall(where):
            assert name in live, f"{table}.{name} does not exist"
            checked += 1
    assert checked >= 15, "the spec got smaller than the rule it implements"


def test_no_scrub_predicate_overlaps_a_delete_predicate(cd_db):
    """A preview and the run it previews must not disagree about a row. They
    would if one pass counted a row the other removed."""
    deleted_tables = {t for t, _ in db._REMOVAL_DELETES}
    scrubbed_tables = {t for t, _, _ in db._REMOVAL_SCRUBS}
    assert not (deleted_tables & scrubbed_tables)


# ── Records a person wrote keep the contribution ──────────────────────────────


def test_an_edit_keeps_its_values_and_loses_its_author(cd_db):
    player = db.upsert_registrant("Hawkmoth", server="1500", origin="imported")
    db.set_squad(player["id"], 1, "Tank", 42.5, actor=actor(REQUESTER))

    db.purge_user_data(REQUESTER, apply=True)

    edit = rows("edits")[0]
    assert edit["new_value"] == "Tank"
    assert edit["actor_discord_id"] == ""
    assert edit["actor_name"] is None
    assert edit["actor_guild_id"] is None


def test_the_squad_reading_itself_survives(cd_db):
    """The whole point of scrubbing rather than deleting. This is a number the
    game shows anyone, contributed for the alliances that use it."""
    player = db.upsert_registrant("Hawkmoth", server="1500", origin="imported")
    db.set_squad(player["id"], 1, "Tank", 42.5, actor=actor(REQUESTER))

    db.purge_user_data(REQUESTER, apply=True)

    squad = rows("squads")[0]
    assert squad["squad_type"] == "Tank"
    assert squad["power"] == 42.5
    assert squad["updated_by"] is None


def test_a_players_own_record_is_untouched(cd_db):
    """#499: no player-keyed removal route. A player is a name the game shows
    everyone, and a Discord-keyed request has no business deleting one."""
    player = db.upsert_registrant("Hawkmoth", server="1500", origin="imported")
    db.set_squad(player["id"], 1, "Tank", 42.5, actor=actor(REQUESTER))

    db.purge_user_data(REQUESTER, apply=True)

    kept = rows("registrants")[0]
    assert kept["display_name"] == "Hawkmoth"
    assert kept["server"] == "1500"
    assert kept["id"] == player["id"]


def test_somebody_elses_attribution_is_left_alone(cd_db):
    player = db.upsert_registrant("Hawkmoth", server="1500", origin="imported")
    db.set_squad(player["id"], 1, "Tank", 42.5, actor=actor(REQUESTER))
    db.set_squad(player["id"], 2, "Missile", 40.0, actor=actor(BYSTANDER, name="Wren"))

    db.purge_user_data(REQUESTER, apply=True)

    theirs = rows("squads", "slot = 2")[0]
    assert theirs["updated_by"] == BYSTANDER
    survivors = {e["actor_discord_id"] for e in rows("edits")}
    assert BYSTANDER in survivors


def test_a_registrant_they_added_keeps_the_player_and_loses_the_adder(cd_db):
    db.upsert_registrant("Kestrel", server="1500", actor=actor(REQUESTER))

    db.purge_user_data(REQUESTER, apply=True)

    kept = rows("registrants")[0]
    assert kept["display_name"] == "Kestrel"
    assert kept["added_by"] is None


def test_an_order_they_recorded_keeps_the_order(cd_db):
    player = db.upsert_registrant("Hawkmoth", server="1500", origin="imported")
    db.add_order(player["id"], ["Tank", "Missile", "Aircraft"], actor=actor(REQUESTER))

    db.purge_user_data(REQUESTER, apply=True)

    order = rows("order_history")[0]
    assert (order["slot1"], order["slot2"], order["slot3"]) == ("Tank", "Missile", "Aircraft")
    assert order["created_by"] is None


def test_an_import_they_ran_keeps_its_counts(cd_db):
    """`import_log` answers "how big is the population we track". Deleting the
    row would take the count with the name."""
    db.record_import(
        door="discord",
        results={"registrants": {"total": 128}},
        actor=actor(REQUESTER, name="Kevin", guild_id="777"),
    )

    db.purge_user_data(REQUESTER, apply=True)

    logged = rows("import_log")[0]
    assert logged["registrants"] == 128
    assert logged["door"] == "discord"
    assert logged["actor_discord_id"] is None
    assert logged["actor_name"] is None
    assert logged["actor_guild_id"] is None


def test_a_disagreement_keeps_which_value_won(cd_db):
    player = db.upsert_registrant("Hawkmoth", server="1500", origin="imported")
    db.set_squad(player["id"], 1, "Tank", 42.5, actor=actor(BYSTANDER))
    db.record_disagreement(
        player["id"],
        target="squad",
        slot=1,
        rows=[{"field": "power", "held": 42.5, "offered": 44.0}],
        chose="held",
        actor=actor(REQUESTER),
    )

    db.purge_user_data(REQUESTER, apply=True)

    call = rows("disagreements")[0]
    assert call["chose"] == "held"
    assert call["held_value"] == "42.5"
    assert call["actor_discord_id"] == ""
    assert call["actor_name"] is None


def test_a_grouping_they_created_keeps_its_dates(cd_db):
    db.create_grouping(warzones=["1500", "1501"], started_on="2026-08-01", discord_id=REQUESTER)

    db.purge_user_data(REQUESTER, apply=True)

    grouping = rows("groupings")[0]
    assert grouping["started_on"] == "2026-08-01"
    assert grouping["created_by_discord_id"] is None
    assert grouping["created_by_guild_id"] is None


def test_a_guilds_warzone_survives_the_person_who_set_it(cd_db):
    """A warzone is the guild's durable fact. The person who typed it is not."""
    db.set_guild_warzone("777", "1500", discord_id=REQUESTER)

    db.purge_user_data(REQUESTER, apply=True)

    pinned = rows("guild_warzone")[0]
    assert pinned["warzone"] == "1500"
    assert pinned["set_by_discord_id"] is None


# ── Records about a person go whole ───────────────────────────────────────────


def test_a_session_is_deleted_not_scrubbed(cd_db):
    """Nothing survives scrubbing a row whose entire content is "this person
    signed in"."""
    db.create_session(REQUESTER, discord_name="Kevin")
    db.create_session(BYSTANDER, discord_name="Wren")

    result = db.purge_user_data(REQUESTER, apply=True)

    assert result["deleted"]["sessions"] == 1
    remaining = rows("sessions")
    assert [s["discord_user_id"] for s in remaining] == [BYSTANDER]


def test_an_unredeemed_auth_code_goes_too(cd_db):
    db.create_auth_code(REQUESTER, discord_name="Kevin")

    db.purge_user_data(REQUESTER, apply=True)

    assert rows("auth_codes") == []


# ── Preview ───────────────────────────────────────────────────────────────────


def test_a_preview_changes_nothing(cd_db):
    player = db.upsert_registrant("Hawkmoth", server="1500", origin="imported")
    db.set_squad(player["id"], 1, "Tank", 42.5, actor=actor(REQUESTER))
    db.create_session(REQUESTER)

    preview = db.purge_user_data(REQUESTER)

    assert preview["applied"] is False
    assert preview["deleted"]["sessions"] == 1
    assert preview["scrubbed"]["squads"] == 1
    assert rows("sessions") != []
    assert rows("squads")[0]["updated_by"] == REQUESTER


def test_a_preview_counts_what_the_run_then_touches(cd_db):
    """The reason the two share their predicates. A preview that promised
    something the run did not do would be worth less than no preview."""
    player = db.upsert_registrant("Hawkmoth", server="1500", origin="imported")
    db.set_squad(player["id"], 1, "Tank", 42.5, actor=actor(REQUESTER))
    db.add_order(player["id"], ["Tank", "Missile", "Aircraft"], actor=actor(REQUESTER))
    db.create_session(REQUESTER)
    db.set_guild_warzone("777", "1500", discord_id=REQUESTER)

    preview = db.purge_user_data(REQUESTER)
    applied = db.purge_user_data(REQUESTER, apply=True)

    assert preview["deleted"] == applied["deleted"]
    assert preview["scrubbed"] == applied["scrubbed"]


def test_an_unknown_id_reports_nothing_rather_than_failing(cd_db):
    db.upsert_registrant("Hawkmoth", server="1500", origin="imported")

    result = db.purge_user_data("404404", apply=True)

    assert result == {"deleted": {}, "scrubbed": {}, "applied": True}


def test_a_blank_id_is_refused_before_it_reaches_a_query(cd_db):
    """An empty actor column is what a scrub leaves behind. A removal run for
    "" would scrub every already-scrubbed row a second time and report it."""
    player = db.upsert_registrant("Hawkmoth", server="1500", origin="imported")
    db.set_squad(player["id"], 1, "Tank", 42.5, actor=actor(REQUESTER))
    db.purge_user_data(REQUESTER, apply=True)

    result = db.purge_user_data("", apply=True)

    assert result == {"deleted": {}, "scrubbed": {}, "applied": True}


def test_running_it_twice_is_a_no_op_the_second_time(cd_db):
    player = db.upsert_registrant("Hawkmoth", server="1500", origin="imported")
    db.set_squad(player["id"], 1, "Tank", 42.5, actor=actor(REQUESTER))
    db.create_session(REQUESTER)

    db.purge_user_data(REQUESTER, apply=True)
    again = db.purge_user_data(REQUESTER, apply=True)

    assert again["deleted"] == {}
    assert again["scrubbed"] == {}


def test_an_integer_user_id_reaches_the_text_columns(cd_db):
    """Every Discord id in this database is TEXT because it arrives from a
    modal or an OAuth callback. A caller holding an int must still match."""
    player = db.upsert_registrant("Hawkmoth", server="1500", origin="imported")
    db.set_squad(player["id"], 1, "Tank", 42.5, actor=actor(REQUESTER))

    result = db.purge_user_data(int(REQUESTER), apply=True)

    assert result["scrubbed"]["squads"] == 1
