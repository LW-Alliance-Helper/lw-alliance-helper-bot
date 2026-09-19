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

import alliance_duel_db as vsdb
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


@pytest.fixture
def vs_db(tmp_path, monkeypatch):
    """VS scores are a third file again (#544). The sweep spans all three, so
    a sweep test that stubs two of them is testing a purge that cannot run."""
    monkeypatch.setattr(vsdb, "DB_PATH", str(tmp_path / "alliance_duel.sqlite3"))
    vsdb.init_db()
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
    # Two deliberate exclusions. `premium_assignments` belongs to the
    # subscriber, not the server. `guild_removals` is the hold record itself:
    # the sweep clears it after the purge, so putting it in the spec would
    # have the purge delete its own bookkeeping mid-run.
    # Anything else appearing here is a table nobody sorted.
    missed = guild_scoped - named - {"premium_assignments", "guild_removals"}
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


def cd_columns_of(table):
    with cd._get_conn() as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_the_champion_duel_spec_matches_its_schema(cd_db):
    with cd._get_conn() as conn:
        live = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    named = {t for t, _ in cd._GUILD_REMOVAL_DELETES} | {t for t, _, _ in cd._GUILD_REMOVAL_SCRUBS}
    assert named <= live, f"names tables that do not exist: {sorted(named - live)}"


def test_the_champion_duel_spec_covers_every_table_that_names_a_server(cd_db):
    """The mirror of the `config` coverage test, which this side never had.

    That one asks the live schema which tables carry a `guild_id` and fails if
    one is unsorted. This one only ever checked the other direction -- that
    every *named* table exists -- so a new table carrying a server's id would
    have passed it in silence, and the attribution would have survived a
    removal with nothing to say so.

    The spec covers everything today; this is what keeps it that way. The
    column is matched by suffix because this side names them for their role
    (`actor_guild_id`, `writer_guild_id`) rather than plain `guild_id`.
    """
    with cd._get_conn() as conn:
        live = [
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        ]
    attributed = {t for t in live if any(c.endswith("guild_id") for c in cd_columns_of(t))}
    named = {t for t, _ in cd._GUILD_REMOVAL_DELETES} | {t for t, _, _ in cd._GUILD_REMOVAL_SCRUBS}

    missed = attributed - named
    assert not missed, f"tables naming a server that the removal ignores: {sorted(missed)}"


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


# ── The hold ──────────────────────────────────────────────────────────────────
#
# An admin who kicks the bot and re-adds it an hour later would otherwise lose
# every wizard they ever ran, and that is far more common than a deliberate
# goodbye.

from datetime import datetime, timedelta, timezone  # noqa: E402


def test_a_removal_starts_a_hold_rather_than_deleting(temp_db):
    seed_config(GUILD)

    config.record_guild_removal(GUILD)

    assert config.guild_removal_held_since(GUILD) is not None
    assert rows("guild_configs", "guild_id = ?", (GUILD,)), "the hold must not delete"


def test_coming_back_cancels_the_hold(temp_db):
    config.record_guild_removal(GUILD)

    assert config.clear_guild_removal(GUILD) is True
    assert config.guild_removal_held_since(GUILD) is None


def test_a_second_delivery_does_not_restart_the_clock(temp_db):
    """Discord can deliver `GUILD_DELETE` more than once. A flapping
    connection restarting the window would hold data indefinitely, which is
    what a bounded window exists to prevent."""
    old = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
    config.record_guild_removal(GUILD, when=old)
    config.record_guild_removal(GUILD)

    assert config.guild_removal_held_since(GUILD) == old


def test_nothing_is_due_inside_the_window(temp_db):
    config.record_guild_removal(GUILD)

    assert config.guild_removals_due() == []


def test_a_hold_that_has_run_out_is_due(temp_db):
    stale = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    config.record_guild_removal(GUILD, when=stale)

    assert config.guild_removals_due() == [GUILD]


def test_due_servers_come_back_oldest_first(temp_db):
    older = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    newer = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    config.record_guild_removal(OTHER_GUILD, when=newer)
    config.record_guild_removal(GUILD, when=older)

    assert config.guild_removals_due() == [GUILD, OTHER_GUILD]


def test_an_unreadable_stamp_is_due_rather_than_kept_forever(temp_db):
    """Written by something that is gone. Keeping it would be the failure mode
    the window exists to rule out."""
    config.record_guild_removal(GUILD, when="not a date")

    assert config.guild_removals_due() == [GUILD]


def test_a_naive_stamp_is_read_as_utc(temp_db):
    """Compared in Python rather than SQL precisely so this is a decision
    rather than a lexicographic accident."""
    naive = (datetime.now(timezone.utc) - timedelta(days=31)).replace(tzinfo=None).isoformat()
    config.record_guild_removal(GUILD, when=naive)

    assert config.guild_removals_due() == [GUILD]


# ── The sweep ─────────────────────────────────────────────────────────────────


def test_the_sweep_leaves_a_server_inside_its_window_alone(temp_db, cd_db, vs_db):
    seed_config(GUILD)
    config.record_guild_removal(GUILD)

    result = config.sweep_guild_removals(apply=True)

    assert result["guilds"] == []
    assert rows("guild_configs", "guild_id = ?", (GUILD,))


def test_the_sweep_purges_a_server_past_its_window(temp_db, cd_db, vs_db):
    seed_config(GUILD)
    config.record_guild_removal(
        GUILD, when=(datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    )

    result = config.sweep_guild_removals(apply=True)

    assert result["guilds"] == [GUILD]
    assert result["config"]["deleted"].get("guild_configs")
    assert rows("guild_configs", "guild_id = ?", (GUILD,)) == []


def test_a_swept_server_is_not_swept_again(temp_db, cd_db, vs_db):
    """Both databases have to succeed before the hold is cleared, so this also
    pins that a completed sweep is not retried."""
    seed_config(GUILD)
    config.record_guild_removal(
        GUILD, when=(datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    )
    config.sweep_guild_removals(apply=True)

    assert config.sweep_guild_removals(apply=True)["guilds"] == []
    assert config.guild_removal_held_since(GUILD) is None


def test_a_dry_sweep_counts_and_keeps_the_hold(temp_db, cd_db, vs_db):
    """The hold row is cleared last and only on a real run, so a purge that
    fails partway is retried rather than forgotten."""
    seed_config(GUILD)
    config.record_guild_removal(
        GUILD, when=(datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    )

    preview = config.sweep_guild_removals()

    assert preview["applied"] is False
    assert preview["guilds"] == [GUILD]
    assert rows("guild_configs", "guild_id = ?", (GUILD,)), "a preview must not write"
    assert config.guild_removal_held_since(GUILD) is not None


def test_a_failing_second_database_keeps_the_hold_for_a_retry(temp_db, cd_db, vs_db, monkeypatch):
    """One database must not block the other, but a half-done purge must not
    be recorded as done either. The hold survives so the next sweep retries."""
    import champion_duel_db

    def _boom(*_a, **_k):
        raise RuntimeError("champion duel database is unreachable")

    monkeypatch.setattr(champion_duel_db, "purge_guild_data", _boom)
    seed_config(GUILD)
    config.record_guild_removal(
        GUILD, when=(datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    )

    result = config.sweep_guild_removals(apply=True)

    assert result["guilds"] == [], "a purge that threw is not a purge that happened"
    assert result["failed"] == [GUILD]
    assert rows("guild_configs", "guild_id = ?", (GUILD,)) == [], "config still purges"
    assert config.guild_removal_held_since(GUILD) is not None, "retry on the next sweep"


def test_install_metadata_now_survives_the_removal_and_goes_with_the_purge(temp_db, cd_db, vs_db):
    """`on_guild_remove` used to delete this row immediately, which was the
    only thing it did. Under a hold it has to outlive the removal, or support
    loses the one record that identifies a guild from a logged id -- during
    exactly the window someone might ask why the bot left."""
    config.upsert_guild_install_metadata(GUILD, guild_name="Zebra Zone", owner_id=1)
    config.record_guild_removal(
        GUILD, when=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    )

    assert config.get_guild_install_metadata(GUILD) is not None, "held, not deleted"

    config.record_guild_removal(GUILD)  # idempotent, does not restart the clock
    with config._get_conn() as conn:
        conn.execute(
            "UPDATE guild_removals SET removed_at = ? WHERE guild_id = ?",
            ((datetime.now(timezone.utc) - timedelta(days=31)).isoformat(), GUILD),
        )
        conn.commit()
    config.sweep_guild_removals(apply=True)

    assert config.get_guild_install_metadata(GUILD) is None


def test_a_server_the_bot_is_still_in_is_never_purged(temp_db, cd_db, vs_db):
    """`on_guild_join` is not dispatched for a server re-added while the bot
    was disconnected -- that arrives in the READY burst. Without this a live
    server's data goes thirty days after it came back."""
    seed_config(GUILD)
    config.record_guild_removal(
        GUILD, when=(datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    )

    result = config.sweep_guild_removals(apply=True, installed={GUILD})

    assert result["rejoined"] == [GUILD]
    assert result["guilds"] == []
    assert rows("guild_configs", "guild_id = ?", (GUILD,)), "a live server keeps its setup"
    assert config.guild_removal_held_since(GUILD) is None, "and the stale hold is cancelled"


def test_one_servers_failure_does_not_block_the_rest(temp_db, cd_db, vs_db, monkeypatch):
    """Due guilds come oldest first, so an unguarded failure on the oldest
    would block every other server every day."""
    real = config.purge_guild_data

    def _selective(gid, **kw):
        if gid == GUILD:
            raise RuntimeError("database is locked")
        return real(gid, **kw)

    monkeypatch.setattr(config, "purge_guild_data", _selective)
    seed_config(GUILD)
    seed_config(OTHER_GUILD, 5678)
    older = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
    newer = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    config.record_guild_removal(GUILD, when=older)
    config.record_guild_removal(OTHER_GUILD, when=newer)

    result = config.sweep_guild_removals(apply=True, installed=set())

    assert result["failed"] == [GUILD]
    assert result["guilds"] == [OTHER_GUILD]
    assert rows("guild_configs", "guild_id = ?", (OTHER_GUILD,)) == []


def test_the_preview_and_the_run_agree_about_the_cascade(cd_db):
    """`pick_meetings` cascades off `pick_slates` and has no `guild_id` of its
    own, so it is the one table a count taken after the delete would miss."""
    with cd._get_conn() as conn:
        conn.execute(
            "INSERT INTO pick_slates (guild_id, play_on, card_no, created_at, updated_at) "
            "VALUES (?, '2026-08-31', 1, 'now', 'now')",
            (str(GUILD),),
        )
        slate_id = conn.execute("SELECT id FROM pick_slates").fetchone()["id"]
        players = []
        for name in ("Zebra", "Quiet"):
            conn.execute(
                "INSERT INTO registrants (player_key, display_name, created_at, updated_at) "
                "VALUES (?, ?, 'now', 'now')",
                (name.lower(), name),
            )
            players.append(conn.execute("SELECT last_insert_rowid() AS i").fetchone()["i"])
        conn.execute(
            "INSERT INTO pick_meetings (slate_id, position, a_id, b_id) VALUES (?, 1, ?, ?)",
            (slate_id, *players),
        )
        conn.commit()

    preview = cd.purge_guild_data(GUILD)
    applied = cd.purge_guild_data(GUILD, apply=True)

    assert preview["deleted"]["pick_meetings"] == 1
    assert applied["deleted"]["pick_meetings"] == 1, "a real run must report it too"
    with cd._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM pick_meetings").fetchone()[0] == 0


def test_sessions_are_revoked_at_removal_not_at_the_end_of_the_hold(cd_db):
    """`SESSION_TTL` is itself 30 days, so deferring this to the purge means a
    write-capable session outlives the removal for as long as it would have
    lived anyway."""
    with cd._get_conn() as conn:
        conn.execute(
            "INSERT INTO sessions "
            "(token_hash, discord_user_id, writer_guild_id, can_write, created_at, expires_at) "
            "VALUES ('hash', '1', ?, 1, 'now', 'later')",
            (str(GUILD),),
        )
        conn.commit()

    assert cd.revoke_guild_sessions(GUILD) == 1
    with cd._get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_the_sweep_scrubs_the_vs_scores_and_keeps_them(temp_db, cd_db, vs_db):
    """The third store (#544) has to be reached by the same one entry point.
    A purge nobody wired in is the failure this whole sweep exists to stop, and
    it would not announce itself: the guild would be reported as purged."""
    import alliance_duel as ad

    seed_config(GUILD)
    vsdb.record_weeks(
        [
            ad.AllianceWeek(
                league=ad.LeagueKey("S35", "Diamond", "12 - 2"),
                week=1,
                alliance=ad.AllianceKey.of("QQQ", "1234"),
                week_score=7,
            )
        ],
        actor={"discord_user_id": "42", "discord_name": "Someone", "guild_id": GUILD},
    )
    config.record_guild_removal(
        GUILD, when=(datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
    )

    result = config.sweep_guild_removals(apply=True)

    assert result["guilds"] == [GUILD]
    assert result["alliance_duel"]["scrubbed"].get("alliance_weeks") == 1
    stored = vsdb.weeks_for_alliance(ad.AllianceKey.of("QQQ", "1234"))
    assert stored[0]["week_score"] == 7, "the league went with the attribution"
    assert stored[0]["actor_guild_id"] is None
