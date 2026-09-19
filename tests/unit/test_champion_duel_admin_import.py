"""`/admin champion_duel_import` — the attachment path into the same importer.

Covers what the HTTP route's tests cannot: that the file is parsed defensively,
that a caller who isn't the bot owner gets nothing, and that the two doors reach
the same data-layer functions rather than a second implementation.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import champion_duel_db as db

ADMIN_ID = 111


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return None


@pytest.fixture
def admin_module():
    """`bot_admin`, which binds `bot_state.bot` at import time.

    Importing `bot` first is what populates it — `bot_admin` does
    `bot = bot_state.bot` and then registers its group on that bot's tree, so
    importing it alone gets None and fails at the bottom of the module.
    """
    import bot  # noqa: F401  (sets bot_state.bot as a side effect)
    import bot_admin

    return bot_admin


@pytest.fixture
def command(admin_module, monkeypatch):
    """The callback, with the owner check stubbed to pass."""
    monkeypatch.setattr(admin_module, "_require_bot_owner", AsyncMock(return_value=True))
    return admin_module.admin_champion_duel_import_slash.callback


def _interaction(user_id=ADMIN_ID):
    interaction = MagicMock()
    interaction.user.id = user_id
    interaction.user.display_name = "Kevin"
    interaction.guild_id = 999
    interaction.response.send_message = AsyncMock()
    interaction.response.defer = AsyncMock()
    interaction.followup.send = AsyncMock()
    return interaction


def _file(payload, filename="payload.json"):
    attachment = MagicMock()
    attachment.filename = filename
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    attachment.read = AsyncMock(return_value=body)
    return attachment


def _sent(interaction):
    call = interaction.followup.send.call_args
    return (call.args[0] if call.args else call.kwargs.get("content")) or ""


def _payload():
    return {
        "registrants": [{"name": "AlphaOne", "group": "M", "rank": 1, "server": "738"}],
        "squads": [
            {
                "name": "AlphaOne",
                "server": "738",
                "slot": slot,
                "type": t,
                "power": p,
                "source": "estimated",
            }
            for slot, (t, p) in enumerate(
                zip(("Tank", "Missile", "Aircraft"), (40, 30, 20)), start=1
            )
        ],
        "orders": [{"name": "AlphaOne", "server": "738", "slots": ["Tank", "Missile", "Aircraft"]}],
        "profiles": [
            {
                "name": "AlphaOne",
                "server": "738",
                "profile": {"types": ["Aircraft", "Tank", "Missile"], "mixed": [0]},
            }
        ],
    }


# ── The happy path reaches the same data layer as the route ───────────────────


async def test_import_loads_every_section(cd_db, command):
    interaction = _interaction()
    await command(interaction, _file(_payload()))

    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    assert player is not None
    assert len(player["squads"]) == 3
    assert len(player["orders"]) == 1
    assert player["profile"] == {"types": ["Aircraft", "Tank", "Missile"], "mixed": [0]}
    assert "Imported" in _sent(interaction)


async def test_the_import_is_logged(cd_db, command):
    """The log is part of the ask, not a nice-to-have: it is what gives us a
    population we can track. Both doors write to it, because an import through
    one is the same event as an import through the other."""
    await command(_interaction(), _file(_payload()))

    logged = db.list_imports()
    assert logged["total"] == 1
    row = logged["imports"][0]
    assert row["door"] == "discord"
    assert row["registrants"] == 1 and row["squads"] == 3 and row["profiles"] == 1
    assert row["actor_discord_id"] == str(ADMIN_ID)


async def test_a_payload_of_profiles_alone_is_accepted(cd_db, command):
    """Every section is optional and this one is the newest, so a payload from
    a simulator run that fitted nothing else must not read as an empty file."""
    payload = {"profiles": _payload()["profiles"]}
    db.import_registrants([{"name": "AlphaOne", "server": "738"}])
    interaction = _interaction()

    await command(interaction, _file(payload))

    assert "Imported" in _sent(interaction)
    player = db.get_player("AlphaOne", server="738", include_scouting=True)
    assert player["profile"] is not None


async def test_rerunning_does_not_double_the_orders(cd_db, command):
    """Same guarantee the route has, because it is the same function: repeats
    in order history are the weight a prediction samples."""
    for _ in range(2):
        await command(_interaction(), _file(_payload()))

    rid = db.resolve_registrant("AlphaOne", server="738")["id"]
    assert db.most_common_order(rid)["total"] == 1


async def test_an_estimate_still_cannot_overwrite_an_observation(cd_db, command):
    await command(_interaction(), _file(_payload()))
    rid = db.resolve_registrant("AlphaOne", server="738")["id"]
    db.set_squad(
        rid,
        1,
        squad_type="Tank",
        power=41_000_000,
        actor={"discord_user_id": "1"},
        source="observed",
    )

    interaction = _interaction()
    await command(interaction, _file(_payload()))

    squad = next(
        s
        for s in db.get_player("AlphaOne", server="738", include_scouting=True)["squads"]
        if s["slot"] == 1
    )
    assert squad["source"] == "observed" and squad["power"] == 41_000_000


# ── The file is not trusted ───────────────────────────────────────────────────


async def test_non_utf8_is_refused_by_name(cd_db, command):
    interaction = _interaction()
    await command(interaction, _file(b"\xff\xfe\x00garbage", filename="roster.xlsx"))
    msg = _sent(interaction)
    assert "roster.xlsx" in msg and "UTF-8" in msg


async def test_malformed_json_is_refused(cd_db, command):
    interaction = _interaction()
    await command(interaction, _file(b"{not json", filename="payload.json"))
    assert "Couldn't parse" in _sent(interaction)


@pytest.mark.parametrize("body", [{}, {"nothing": []}, [1, 2, 3], {"registrants": "AlphaOne"}])
async def test_a_payload_with_no_usable_section_is_refused(cd_db, command, body):
    """An empty or wrong-shaped file is a mistake, not a no-op success — it
    would otherwise report a cheerful import of nothing."""
    interaction = _interaction()
    await command(interaction, _file(body))
    assert "none of" in _sent(interaction)


async def test_problem_rows_are_attached_not_truncated(cd_db, command):
    """A roster refresh can produce hundreds; inlining them hides the ones
    nobody has read yet."""
    payload = _payload()
    payload["squads"].append(
        {"name": "WhoDis", "server": "738", "slot": 1, "type": "Tank", "power": 1}
    )
    interaction = _interaction()
    await command(interaction, _file(payload))

    kwargs = interaction.followup.send.call_args.kwargs
    assert kwargs["file"].filename.endswith(".txt")
    assert "didn't land" in _sent(interaction)


# ── Access ────────────────────────────────────────────────────────────────────


async def test_a_non_owner_gets_nothing(cd_db, admin_module, monkeypatch):
    """Stronger than the service key it replaces: bot-owner, not an id in an
    env var."""
    monkeypatch.setattr(admin_module, "_require_bot_owner", AsyncMock(return_value=False))
    interaction = _interaction(user_id=222)
    await admin_module.admin_champion_duel_import_slash.callback(interaction, _file(_payload()))

    interaction.followup.send.assert_not_awaited()
    assert db.get_player("AlphaOne", server="738") is None


def test_the_round_picker_matches_the_data_layer(admin_module):
    """The choices are spelled out on the decorator because it runs at import
    time and `champion_duel_db` is imported inside the callback. That
    duplication is only safe while this holds."""
    choices = admin_module.admin_champion_duel_import_slash._params["round"].choices
    assert {c.value: c.name for c in choices} == db.STAGE_LABELS
