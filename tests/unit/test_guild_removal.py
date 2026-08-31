"""Removing the bot from a server takes that server's data with it (#543).

`on_guild_remove` dropped the install-metadata row and nothing else, so guild
configuration, storm sign-ups, event state and every other guild-scoped table
survived an uninstall indefinitely with Discord IDs still in them. Discord's
Developer Policy asks for the opposite on `GUILD_DELETE`.

**The rule here is not the rule for a person, and the difference is the whole
design.** A personal removal strips who did it and keeps what they
contributed, because a reading of the game outlives its author. A guild
removal *deletes*, because the rows are about that server and mean nothing
without it.

Two exceptions, and both are load-bearing:

- `premium_assignments` is keyed on the subscriber, not the server. Deleting
  it would take a paid licence from someone who did nothing.
- Champion Duel's readings are scrubbed, not deleted. Other alliances
  contributed to the same tournament; only the attribution is this server's.
"""

from __future__ import annotations

import re

import pytest

import champion_duel_db as cd
import config
from config import GuildConfig

GUILD = 606060
OTHER_GUILD = 707070
SUBSCRIBER = 8080


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    """The Champion Duel database is a separate file with its own init."""
    monkeypatch.setattr(cd, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    cd.init_db()
    return None


def seed_config(guild_id, channel_id=1234):
    config.save_config(GuildConfig(guild_id=guild_id, leadership_channel_id=channel_id))


def rows(table, where="", params=()):
    with config._get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM {table} {('WHERE ' + where) if where else ''}",  # noqa: S608
                params,
            ).fetchall()
        ]


# ── The spec matches the schema ───────────────────────────────────────────────
#
# Same guard the personal removal carries, for the same reason: the spec was
# compiled by reading the tree, and a wrong column name in a delete path is the
# worst place for one. SQLite would raise halfway through a live removal.

_IDENT = re.compile(r"\b([a-z_][a-z0-9_]*)\s*(?==)")


def columns_of(table):
    with config._get_conn() as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_every_table_the_guild_removal_names_exists(temp_db):
    with config._get_conn() as conn:
        live = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    named = {t for t, _ in config._GUILD_REMOVAL_DELETES}
    assert named <= live, f"removal names tables that do not exist: {sorted(named - live)}"


def test_every_column_the_guild_removal_reads_exists(temp_db):
    checked = 0
    for table, where in config._GUILD_REMOVAL_DELETES:
        live = columns_of(table)
        for name in _IDENT.findall(where):
            assert name in live, f"{table}.{name} does not exist"
            checked += 1
    assert checked >= 25, "the spec got smaller than the rule it implements"


def test_the_spec_covers_every_guild_scoped_table(temp_db):
    """The finding was tables nobody remembered. A spec compiled by hand goes
    stale the first time somebody adds a table, so this asks the schema."""
    with config._get_conn() as conn:
        live = [
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        ]
    guild_scoped = {t for t in live if "guild_id" in columns_of(t)}
    named = {t for t, _ in config._GUILD_REMOVAL_DELETES}
    # `premium_assignments` is deliberately outside the spec: it belongs to the
    # subscriber. Anything else appearing here is a table nobody sorted.
    missed = guild_scoped - named - {"premium_assignments"}
    assert not missed, f"guild-scoped tables the removal ignores: {sorted(missed)}"


# ── What goes ─────────────────────────────────────────────────────────────────


def test_a_guilds_configuration_is_deleted(temp_db):
    seed_config(GUILD)

    result = config.purge_guild_data(GUILD, apply=True)

    assert result["deleted"].get("guild_configs")
    assert rows("guild_configs", "guild_id = ?", (GUILD,)) == []


def test_another_server_is_untouched(temp_db):
    seed_config(GUILD)
    seed_config(OTHER_GUILD, 5678)

    config.purge_guild_data(GUILD, apply=True)

    assert rows("guild_configs", "guild_id = ?", (OTHER_GUILD,))


def test_the_dry_run_counts_and_changes_nothing(temp_db):
    seed_config(GUILD)

    preview = config.purge_guild_data(GUILD)

    assert preview["applied"] is False
    assert preview["deleted"].get("guild_configs")
    assert rows("guild_configs", "guild_id = ?", (GUILD,)), "a preview must not write"


# ── What stays ────────────────────────────────────────────────────────────────


def test_a_paid_licence_survives_the_server_dropping_the_bot(temp_db):
    """The row is keyed on the subscriber. Deleting it would take a licence
    from someone who did nothing, and `/premium assign` already copes with a
    guild it cannot see."""
    config.set_premium_assignment(SUBSCRIBER, GUILD)

    config.purge_guild_data(GUILD, apply=True)

    assert config.get_premium_assignment_for_user(SUBSCRIBER) == GUILD


def test_premium_is_not_even_named_in_the_spec(temp_db):
    named = {t for t, _ in config._GUILD_REMOVAL_DELETES}
    assert "premium_assignments" not in named


# ── The Champion Duel side scrubs rather than deletes ─────────────────────────


def test_the_champion_duel_spec_matches_its_schema(cd_db):
    with cd._get_conn() as conn:
        live = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    named = {t for t, _ in cd._GUILD_REMOVAL_DELETES} | {t for t, _, _ in cd._GUILD_REMOVAL_SCRUBS}
    assert named <= live, f"names tables that do not exist: {sorted(named - live)}"


def test_a_reading_keeps_its_content_and_loses_its_server(cd_db):
    """Other alliances contributed to the same tournament. Only the
    attribution is this server's."""
    with cd._get_conn() as conn:
        conn.execute(
            "INSERT INTO import_log "
            "(door, created_at, actor_discord_id, actor_name, actor_guild_id) "
            "VALUES ('discord', '2026-08-30', '1', 'someone', ?)",
            (str(GUILD),),
        )
        conn.commit()

    result = cd.purge_guild_data(GUILD, apply=True)

    assert result["scrubbed"].get("import_log") == 1
    with cd._get_conn() as conn:
        row = conn.execute("SELECT * FROM import_log").fetchone()
    assert row["actor_guild_id"] is None
    assert row["actor_discord_id"] == "1", "the reading itself is not this server's to remove"


def test_a_session_does_not_keep_working_after_the_bot_is_removed():
    assert ("sessions", "writer_guild_id = :gid") in cd._GUILD_REMOVAL_DELETES
    assert ("auth_codes", "writer_guild_id = :gid") in cd._GUILD_REMOVAL_DELETES
