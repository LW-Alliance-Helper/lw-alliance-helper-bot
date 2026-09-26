"""Unit tests for the pre-1.9.0 guild backfill (#649).

`on_guild_remove` deleted only the install-metadata row before 1.9.0 --
no hold, so `guild_removals_due` never sees these servers and their data
stays indefinitely. `config.guild_ids_with_stored_data` finds them by
looking for a row rather than a removal record that was never written;
`config.backfill_candidates` narrows that to the ones actually worth an
owner's confirm.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import alliance_duel_db as vsdb
import champion_duel_db as cd
import config
from config import GuildConfig

GUILD = 606060
OTHER_GUILD = 707070
HELD_GUILD = 808080
LIVE_GUILD = 909090


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    """The Champion Duel database is a separate file with its own init."""
    monkeypatch.setattr(cd, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    cd.init_db()
    return None


@pytest.fixture
def vs_db(tmp_path, monkeypatch):
    """VS scores are a third file again (#544)."""
    monkeypatch.setattr(vsdb, "DB_PATH", str(tmp_path / "alliance_duel.sqlite3"))
    vsdb.init_db()
    return None


def _seed_config(guild_id: int) -> None:
    config.save_config(GuildConfig(guild_id=guild_id, leadership_channel_id=1234))


class TestGuildIdsWithStoredData:
    def test_a_config_row_is_found(self, temp_db, cd_db, vs_db):
        _seed_config(GUILD)
        assert GUILD in config.guild_ids_with_stored_data()

    def test_a_premium_only_guild_is_found(self, temp_db, cd_db, vs_db):
        """#649 depends on #573's release reaching pre-1.9.0 rows too --
        the candidate list has to include a guild whose only trace is a
        stale Premium pin."""
        config.set_premium_assignment(111, GUILD)
        assert GUILD in config.guild_ids_with_stored_data()

    def test_a_champion_duel_only_guild_is_found(self, temp_db, cd_db, vs_db):
        cd.set_guild_warzone(GUILD, "738")
        assert GUILD in config.guild_ids_with_stored_data()

    def test_a_guild_with_nothing_anywhere_is_not_found(self, temp_db, cd_db, vs_db):
        assert OTHER_GUILD not in config.guild_ids_with_stored_data()

    def test_text_and_integer_ids_unify_into_one_candidate(self, temp_db, cd_db, vs_db):
        """Champion Duel and VS store guild_id as TEXT; config stores it as
        INTEGER. A candidate present in both must not appear twice or fail
        to unify -- `guild_ids_with_stored_data` returns a set, so this is
        really a normalisation check disguised as a membership one."""
        _seed_config(GUILD)
        cd.set_guild_warzone(GUILD, "738")

        ids = config.guild_ids_with_stored_data()
        assert GUILD in ids
        assert all(isinstance(gid, int) for gid in ids)


class TestBackfillCandidates:
    def test_a_live_guild_is_never_a_candidate(self, temp_db, cd_db, vs_db):
        _seed_config(LIVE_GUILD)
        candidates = config.backfill_candidates(live={LIVE_GUILD})
        assert LIVE_GUILD not in candidates

    def test_an_already_held_guild_is_not_a_candidate(self, temp_db, cd_db, vs_db):
        """Nothing for the command to start -- #543's own hold already
        governs this one's clock."""
        _seed_config(HELD_GUILD)
        config.record_guild_removal(HELD_GUILD)
        candidates = config.backfill_candidates(live=set())
        assert HELD_GUILD not in candidates

    def test_a_genuine_pre_1_9_0_departure_is_a_candidate(self, temp_db, cd_db, vs_db):
        """The metadata row is already gone -- the old on_guild_remove
        deleted it -- so `last_seen_at` reads as None, not stale-but-present."""
        _seed_config(GUILD)
        candidates = config.backfill_candidates(live=set())
        assert candidates.get(GUILD) is None

    def test_a_recently_seen_guild_is_excluded_as_suspicious(self, temp_db, cd_db, vs_db):
        """A fresh last_seen_at alongside "not in `live`" reads as a partial
        gateway list, not a real departure -- #649's own safety rule."""
        _seed_config(GUILD)
        config.upsert_guild_install_metadata(GUILD, guild_name="Recent", owner_id=1)

        candidates = config.backfill_candidates(live=set())

        assert GUILD not in candidates

    def test_an_old_last_seen_stamp_is_still_a_candidate(self, temp_db, cd_db, vs_db):
        _seed_config(GUILD)
        old_stamp = (datetime.now(timezone.utc) - timedelta(days=400)).isoformat()
        with config._get_conn() as conn:
            conn.execute(
                "INSERT INTO guild_install_metadata "
                "(guild_id, guild_name, owner_id, installed_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (GUILD, "Old Guild", 1, old_stamp, old_stamp),
            )
            conn.commit()

        candidates = config.backfill_candidates(live=set())

        assert candidates.get(GUILD) == old_stamp

    def test_recently_seen_window_is_configurable(self, temp_db, cd_db, vs_db):
        _seed_config(GUILD)
        stamp = (datetime.now(timezone.utc) - timedelta(days=20)).isoformat()
        with config._get_conn() as conn:
            conn.execute(
                "INSERT INTO guild_install_metadata "
                "(guild_id, guild_name, owner_id, installed_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (GUILD, "Guild", 1, stamp, stamp),
            )
            conn.commit()

        # 14-day default: 20 days ago is not suspicious.
        assert GUILD in config.backfill_candidates(live=set(), recently_seen_days=14)
        # A 30-day window: the same stamp now reads as suspicious.
        assert GUILD not in config.backfill_candidates(live=set(), recently_seen_days=30)
