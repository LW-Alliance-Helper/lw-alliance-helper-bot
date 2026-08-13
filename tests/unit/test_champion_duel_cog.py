"""Champion Duel admin cog — access guard, date handling, CSV shape.

The command bodies are exercised through their callbacks with a faked
interaction, following the repo's pattern of testing `task_name.coro(...)`
directly rather than standing up a gateway.
"""

from __future__ import annotations

import csv
import io
from unittest.mock import AsyncMock, MagicMock

import pytest

import champion_duel_db as db
from champion_duel_cog import BROWSE_MAX, ChampionDuelAdmin, _describe, _parse_day

ADMIN_ID = 111
OUTSIDER_ID = 222

KEV = {"discord_user_id": str(ADMIN_ID), "discord_name": "Kevin", "guild_id": "999"}


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    path = str(tmp_path / "champion_duel.sqlite3")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    db.import_registrants([{"name": "AlphaOne", "group": "M", "rank": 1}])
    return path


@pytest.fixture
def admin_env(monkeypatch):
    monkeypatch.setenv("CHAMPION_DUEL_ADMIN_IDS", str(ADMIN_ID))


def _interaction(user_id=ADMIN_ID):
    """A stand-in for discord.Interaction covering only what the cog touches."""
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.user.display_name = "Kevin"
    interaction.guild_id = 999
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


# ── Date handling ─────────────────────────────────────────────────────────────


def test_end_date_covers_the_whole_day():
    """A same-day range must not come back empty.

    Timestamps compare as text, so an inclusive end has to be the day's last
    instant. Midnight would make `export X X` silently return nothing, which
    reads as 'no edits that day' rather than 'your range had zero width'.
    """
    start = _parse_day("2026-08-12", end_of_day=False)
    end = _parse_day("2026-08-12", end_of_day=True)
    assert start < end
    assert start.startswith("2026-08-12T00:00")
    assert "23:59:59" in end
    # A timestamp from the middle of that day falls inside the range.
    assert start < "2026-08-12T13:45:00+00:00" < end


@pytest.mark.parametrize("bad", ["12/08/2026", "not-a-date", "", "2026-13-45", None])
def test_bad_dates_rejected(bad):
    assert _parse_day(bad, end_of_day=False) is None


# ── Access guard ──────────────────────────────────────────────────────────────


async def test_non_admin_is_refused_and_told_why(cd_db, admin_env):
    cog = ChampionDuelAdmin(MagicMock())
    interaction = _interaction(user_id=OUTSIDER_ID)
    assert await cog._guard(interaction) is False
    # Refusal is explicit: a silent no-op reads as a broken command.
    interaction.response.send_message.assert_awaited_once()
    assert "CHAMPION_DUEL_ADMIN_IDS" in interaction.response.send_message.call_args.args[0]


async def test_admin_passes_guard(cd_db, admin_env):
    cog = ChampionDuelAdmin(MagicMock())
    assert await cog._guard(_interaction()) is True


async def test_unset_env_admits_nobody(cd_db, monkeypatch):
    """A misconfigured deploy must close the surface, not open it."""
    monkeypatch.delenv("CHAMPION_DUEL_ADMIN_IDS", raising=False)
    cog = ChampionDuelAdmin(MagicMock())
    assert await cog._guard(_interaction()) is False


# ── Export ────────────────────────────────────────────────────────────────────


async def test_export_produces_readable_csv(cd_db, admin_env):
    db.set_squad("AlphaOne", 1, squad_type="Tank", power=1_000, actor=KEV)
    cog = ChampionDuelAdmin(MagicMock())
    interaction = _interaction()

    await cog.export.callback(cog, interaction, "2000-01-01", "2099-01-01")

    kwargs = interaction.followup.send.call_args.kwargs
    payload = kwargs["file"].fp.read().decode("utf-8-sig")
    rows = list(csv.DictReader(io.StringIO(payload)))
    assert rows, "export produced no rows"
    assert rows[0]["player_key"] == "alphaone"
    assert rows[0]["actor_discord_id"] == str(ADMIN_ID)
    # Excel needs the BOM to render non-Latin player names correctly.
    assert payload != payload.lstrip("﻿") or payload.startswith("id,")


async def test_export_rejects_reversed_range(cd_db, admin_env):
    cog = ChampionDuelAdmin(MagicMock())
    interaction = _interaction()
    await cog.export.callback(cog, interaction, "2026-08-12", "2026-08-01")
    assert "after the end date" in interaction.followup.send.call_args.args[0]
    assert "file" not in interaction.followup.send.call_args.kwargs


async def test_export_with_no_matches_says_so(cd_db, admin_env):
    cog = ChampionDuelAdmin(MagicMock())
    interaction = _interaction()
    await cog.export.callback(cog, interaction, "1999-01-01", "1999-01-02")
    assert "No edits" in interaction.followup.send.call_args.args[0]


# ── Revert ────────────────────────────────────────────────────────────────────


async def test_revert_conflict_explains_rather_than_clobbers(cd_db, admin_env):
    db.set_squad("AlphaOne", 1, squad_type="Tank", actor=KEV)
    stale = db.set_squad("AlphaOne", 1, squad_type="Missile", actor=KEV)["edit_ids"][0]
    db.set_squad("AlphaOne", 1, squad_type="Aircraft", actor=KEV)

    cog = ChampionDuelAdmin(MagicMock())
    interaction = _interaction()
    await cog.revert.callback(cog, interaction, stale, False)

    msg = interaction.followup.send.call_args.args[0]
    assert "wasn't reverted" in msg and "Aircraft" in msg and "force" in msg
    # The value on disk is untouched.
    assert db.get_player("AlphaOne", include_scouting=True)["squads"][0]["squad_type"] == "Aircraft"


async def test_revert_succeeds_and_appends(cd_db, admin_env):
    db.set_squad("AlphaOne", 1, squad_type="Tank", actor=KEV)
    edit_id = db.set_squad("AlphaOne", 1, squad_type="Missile", actor=KEV)["edit_ids"][0]

    cog = ChampionDuelAdmin(MagicMock())
    interaction = _interaction()
    before = db.list_edits()["total"]
    await cog.revert.callback(cog, interaction, edit_id, False)

    assert "Reverted" in interaction.followup.send.call_args.args[0]
    assert db.get_player("AlphaOne", include_scouting=True)["squads"][0]["squad_type"] == "Tank"
    assert db.list_edits()["total"] == before + 1, "revert should append, never delete"


async def test_revert_unknown_edit(cd_db, admin_env):
    cog = ChampionDuelAdmin(MagicMock())
    interaction = _interaction()
    await cog.revert.callback(cog, interaction, 99999, False)
    assert "No edit" in interaction.followup.send.call_args.args[0]


# ── Browse ────────────────────────────────────────────────────────────────────


async def test_edits_listing_is_capped(cd_db, admin_env):
    for i in range(30):
        db.set_squad("AlphaOne", 1, power=1000 + i, actor=KEV)
    cog = ChampionDuelAdmin(MagicMock())
    interaction = _interaction()
    await cog.edits.callback(cog, interaction, None, None, 999)

    embed = interaction.followup.send.call_args.kwargs["embed"]
    assert len(embed.description.splitlines()) <= BROWSE_MAX
    # The footer has to point at the export, since that's the real browsing tool.
    assert "export" in embed.footer.text


def test_describe_renders_a_revert_marker():
    line = _describe(
        {
            "id": 7,
            "player_key": "alphaone",
            "slot": 1,
            "field": "squad_type",
            "old_value": "Tank",
            "new_value": "Missile",
            "actor_discord_id": "111",
            "created_at": "2026-08-12T10:00:00+00:00",
            "revert_of": 3,
            "target": "squad",
        }
    )
    assert "#7" in line and "revert of #3" in line and "<@111>" in line
