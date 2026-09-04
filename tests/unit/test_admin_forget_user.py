"""`/admin forget_user` — the surface that actions a removal request (#517).

The privacy policy promises that we action requests. Until this there was no
route to action one: `/admin forget_guild` clears a guild's install record and
`champion_duel_db.purge_expired` clears expired sessions, and between them they
reach none of what the bot holds about a person.

Kevin works in UIs, not the terminal, and that decides the shape rather than
just the convenience: a script he has to run in a Railway shell is a route that
does not get used. So this is a slash command with a preview, a confirm, and a
receipt, the same three beats as `forget_guild` next door.

The receipt is the part the promise rests on. A removal nobody can audit is a
removal nobody can trust.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import champion_duel_db as cd_db
import config

OWNER_ID = 111
REQUESTER = 5150
GUILD = 424242
DATE = "2026-08-22"


@pytest.fixture
def cd_temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(cd_db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    cd_db.init_db()
    return None


@pytest.fixture(autouse=True)
def vs_temp_db(tmp_path, monkeypatch):
    """VS scores are the third store a person can appear in (#544).

    Autouse, unlike the two above. A removal test that leaves one store on its
    real path does not fail loudly -- the store reports an error, the operator
    is told a database could not be reached, and the assertion that breaks is
    about a button rather than about the database. Every test in this file
    wants all three stubbed.
    """
    import alliance_duel_db as vs_db

    monkeypatch.setattr(vs_db, "DB_PATH", str(tmp_path / "alliance_duel.sqlite3"))
    vs_db.init_db()
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
    """The callback, with the owner check stubbed to pass and the user lookup
    stubbed so no test reaches for the network."""
    monkeypatch.setattr(admin_module, "_require_bot_owner", AsyncMock(return_value=True))
    monkeypatch.setattr(admin_module.bot, "get_user", MagicMock(return_value=None))
    monkeypatch.setattr(admin_module.bot, "fetch_user", AsyncMock(side_effect=Exception("no net")))
    return admin_module.admin_forget_user_slash.callback


def interaction(user_id=OWNER_ID):
    inter = MagicMock()
    inter.user.id = user_id
    inter.response.send_message = AsyncMock()
    inter.response.defer = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.edit_original_response = AsyncMock()
    inter.followup.send = AsyncMock()
    return inter


def followup(inter):
    return inter.followup.send.call_args.kwargs


def receipt(inter):
    """What the operator is left looking at after pressing the danger button.
    Written by `edit_original_response`, not by the first response, because the
    run is acknowledged before it starts."""
    return inter.edit_original_response.call_args.kwargs


def seed_both_databases():
    """One row of each kind: a record about the person, and one they wrote."""
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, str(REQUESTER), "a")
    player = cd_db.upsert_registrant("Hawkmoth", server="1500", origin="imported")
    cd_db.set_squad(
        player["id"],
        1,
        "Tank",
        42.5,
        actor={"discord_user_id": str(REQUESTER), "discord_name": "Kevin"},
    )


# ── The gate ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_caller_who_is_not_the_owner_gets_nothing(admin_module, monkeypatch, temp_db):
    monkeypatch.setattr(admin_module, "_require_bot_owner", AsyncMock(return_value=False))
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, str(REQUESTER), "a")
    inter = interaction(user_id=999)

    await admin_module.admin_forget_user_slash.callback(inter, str(REQUESTER))

    assert inter.response.defer.await_count == 0
    assert len(config.get_storm_signups(GUILD, "DS", DATE)) == 1


@pytest.mark.asyncio
async def test_a_user_id_that_is_not_a_number_is_refused_before_any_query(command, temp_db):
    inter = interaction()

    await command(inter, "kevin")

    inter.response.send_message.assert_awaited_once()
    assert "valid integer user ID" in inter.response.send_message.call_args.args[0]
    assert inter.response.defer.await_count == 0


# ── Preview ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_preview_offers_a_confirm_and_changes_nothing(command, temp_db, cd_temp_db):
    seed_both_databases()
    inter = interaction()

    await command(inter, str(REQUESTER))

    sent = followup(inter)
    assert sent["view"] is not None
    assert sent["embed"].title == "Data removal preview"
    assert len(config.get_storm_signups(GUILD, "DS", DATE)) == 1


@pytest.mark.asyncio
async def test_the_preview_names_both_databases(command, temp_db, cd_temp_db):
    """Two SQLite files, one request. A report covering one of them would read
    as a complete removal."""
    seed_both_databases()
    inter = interaction()

    await command(inter, str(REQUESTER))

    fields = {f.name: f.value for f in followup(inter)["embed"].fields}
    assert "`storm_signups` 1" in fields["To delete (2)"]
    assert "`squads` 1" in fields["To scrub (3)"]


@pytest.mark.asyncio
async def test_an_id_with_nothing_behind_it_gets_no_confirm_button(command, temp_db, cd_temp_db):
    """Nothing to confirm, so nothing to click. An armed danger button over an
    empty result invites a second guess at the ID."""
    inter = interaction()

    await command(inter, "404404")

    sent = followup(inter)
    assert "view" not in sent
    assert sent["embed"].footer.text == "Nothing held under this ID in either database."


# ── Confirm ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_confirming_removes_from_both_databases(command, admin_module, temp_db, cd_temp_db):
    seed_both_databases()
    inter = interaction()
    await command(inter, str(REQUESTER))
    view = followup(inter)["view"]

    press = interaction()
    await view.confirm.callback(press)

    assert config.get_storm_signups(GUILD, "DS", DATE) == []
    with cd_db._get_conn() as conn:
        squad = conn.execute("SELECT * FROM squads").fetchone()
    assert squad["updated_by"] is None
    assert squad["power"] == 42.5


@pytest.mark.asyncio
async def test_the_receipt_reports_what_it_touched(command, temp_db, cd_temp_db):
    seed_both_databases()
    inter = interaction()
    await command(inter, str(REQUESTER))
    view = followup(inter)["view"]

    press = interaction()
    await view.confirm.callback(press)

    embed = receipt(press)["embed"]
    assert embed.title == "Data removal"
    fields = {f.name: f.value for f in embed.fields}
    assert "`storm_signups` 1" in fields["Deleted (2)"]
    assert "`squads` 1" in fields["Scrubbed (3)"]


@pytest.mark.asyncio
async def test_the_buttons_are_disabled_once_it_has_run(command, temp_db, cd_temp_db):
    """There is no undo. A live-looking button after the run is an invitation
    to press it again."""
    seed_both_databases()
    inter = interaction()
    await command(inter, str(REQUESTER))
    view = followup(inter)["view"]

    await view.confirm.callback(interaction())

    assert all(item.disabled for item in view.children)


@pytest.mark.asyncio
async def test_cancelling_leaves_everything_where_it_was(command, temp_db, cd_temp_db):
    seed_both_databases()
    inter = interaction()
    await command(inter, str(REQUESTER))
    view = followup(inter)["view"]

    press = interaction()
    await view.cancel.callback(press)

    assert len(config.get_storm_signups(GUILD, "DS", DATE)) == 1
    assert "Canceled" in press.response.edit_message.call_args.kwargs["content"]


@pytest.mark.asyncio
async def test_only_the_operator_who_started_it_can_confirm(command, temp_db, cd_temp_db):
    seed_both_databases()
    inter = interaction()
    await command(inter, str(REQUESTER))
    view = followup(inter)["view"]

    someone_else = interaction(user_id=777)
    allowed = await view.interaction_check(someone_else)

    assert allowed is False
    assert len(config.get_storm_signups(GUILD, "DS", DATE)) == 1


@pytest.mark.asyncio
async def test_the_premium_cache_is_cleared_after_a_run(command, temp_db, cd_temp_db):
    """Deleting the assignment row drops that guild's Premium, and a cached
    True would keep serving paid features to a guild that no longer has it."""
    import premium

    config.set_premium_assignment(REQUESTER, GUILD)
    premium._cache_set(GUILD, True)
    inter = interaction()
    await command(inter, str(REQUESTER))
    view = followup(inter)["view"]

    await view.confirm.callback(interaction())

    assert premium._cache_get(GUILD) is None


# ── One database failing must not look like both succeeding ───────────────────


@pytest.mark.asyncio
async def test_a_champion_duel_failure_is_reported_not_swallowed(
    command, admin_module, monkeypatch, temp_db, cd_temp_db
):
    seed_both_databases()
    monkeypatch.setattr(
        cd_db, "purge_user_data", MagicMock(side_effect=RuntimeError("no such table: edits"))
    )
    inter = interaction()

    await command(inter, str(REQUESTER))

    fields = {f.name: f.value for f in followup(inter)["embed"].fields}
    assert "no such table: edits" in fields["Champion Duel database"]


@pytest.mark.asyncio
async def test_the_config_half_still_runs_and_still_says_so(
    command, admin_module, monkeypatch, temp_db, cd_temp_db
):
    """The worst outcome available here is a removal that half-happened and
    reported nothing."""
    seed_both_databases()
    inter = interaction()
    await command(inter, str(REQUESTER))
    view = followup(inter)["view"]
    monkeypatch.setattr(
        cd_db, "purge_user_data", MagicMock(side_effect=RuntimeError("disk I/O error"))
    )

    press = interaction()
    await view.confirm.callback(press)

    embed = receipt(press)["embed"]
    fields = {f.name: f.value for f in embed.fields}
    assert "`storm_signups` 1" in fields["Deleted (2)"]
    assert "disk I/O error" in fields["Champion Duel database"]
    assert config.get_storm_signups(GUILD, "DS", DATE) == []


@pytest.mark.asyncio
async def test_zero_is_refused_like_any_other_bad_id(command, temp_db):
    """Zero is the scrub sentinel on three columns and the recorded owner of a
    guild nobody captured. A run for it would match rows belonging to no person
    and inflate the counts that are the only check the ID was right."""
    config.upsert_guild_install_metadata(guild_id=GUILD, guild_name="Wind Runners", owner_id=0)
    inter = interaction()

    await command(inter, "0")

    inter.response.send_message.assert_awaited_once()
    assert inter.response.defer.await_count == 0
    assert config.get_guild_install_metadata(GUILD)["guild_name"] == "Wind Runners"


@pytest.mark.asyncio
async def test_the_run_is_acknowledged_before_it_starts(command, temp_db, cd_temp_db):
    """Two SQLite files and a rewrite of every roster draft can outrun the
    three seconds Discord allows. Acknowledging after the work buys a removal
    that happened with no receipt and buttons still live."""
    seed_both_databases()
    inter = interaction()
    await command(inter, str(REQUESTER))
    view = followup(inter)["view"]

    press = interaction()
    await view.confirm.callback(press)

    press.response.edit_message.assert_awaited_once()
    assert "embed" not in press.response.edit_message.call_args.kwargs
    press.edit_original_response.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_config_side_failure_is_reported_too(
    command, admin_module, monkeypatch, temp_db, cd_temp_db
):
    """Both halves are caught, not just the Champion Duel one. A failure with
    no reporting path is what turns a half-done removal into a "done"."""
    seed_both_databases()
    monkeypatch.setattr(
        config, "purge_user_data", MagicMock(side_effect=RuntimeError("database is locked"))
    )
    inter = interaction()

    await command(inter, str(REQUESTER))

    fields = {f.name: f.value for f in followup(inter)["embed"].fields}
    assert "database is locked" in fields["Guild config database"]
    assert "`squads` 1" in fields["To scrub (3)"]


@pytest.mark.asyncio
async def test_an_unreadable_database_still_offers_the_run(
    command, admin_module, monkeypatch, temp_db, cd_temp_db
):
    """A database that could not be read has not said the person is absent
    from it. Withholding the confirm would make an outage look like an answer."""
    monkeypatch.setattr(
        cd_db, "purge_user_data", MagicMock(side_effect=RuntimeError("unable to open database"))
    )
    inter = interaction()

    await command(inter, str(REQUESTER))

    sent = followup(inter)
    assert sent["view"] is not None
    assert sent["embed"].footer.text is None
