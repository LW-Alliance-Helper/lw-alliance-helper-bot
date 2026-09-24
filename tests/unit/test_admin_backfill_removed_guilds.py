"""`/admin backfill_removed_guilds` — the owner-only backfill for servers
that left before 1.9.0's removal hold existed (#649).

Same three beats as `/admin forget_user` next door: preview, confirm,
receipt. Deletes nothing itself -- confirming only starts a 30-day hold,
identical to a real removal's; the daily sweep purges it once the hold
ends.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import config

OWNER_ID = 111
GUILD = 424242
LIVE_GUILD = 555555


@pytest.fixture
def admin_module():
    """Importing `bot` first populates `bot_state.bot`, which `bot_admin`
    binds at import time -- see `test_admin_forget_user.py`'s identical
    fixture for the full reasoning."""
    import bot  # noqa: F401

    import bot_admin

    return bot_admin


@pytest.fixture
def command(admin_module, monkeypatch):
    monkeypatch.setattr(admin_module, "_require_bot_owner", AsyncMock(return_value=True))
    return admin_module.admin_backfill_removed_guilds_slash.callback


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


class TestTheGate:
    @pytest.mark.asyncio
    async def test_a_non_owner_gets_nothing(self, command, admin_module, monkeypatch):
        monkeypatch.setattr(admin_module, "_require_bot_owner", AsyncMock(return_value=False))
        inter = interaction(user_id=999)

        await command(inter)

        assert inter.response.defer.await_count == 0


class TestNotReadyYet:
    @pytest.mark.asyncio
    async def test_refuses_to_guess_when_not_connected(self, command, admin_module, monkeypatch):
        monkeypatch.setattr(admin_module, "live_guild_ids", AsyncMock(return_value=None))
        inter = interaction()

        await command(inter)

        body = followup(inter).get("content") or inter.followup.send.call_args.args[0]
        assert "not fully connected" in body.lower()


class TestPreview:
    @pytest.mark.asyncio
    async def test_no_candidates_says_so(
        self, command, admin_module, monkeypatch, temp_db, cd_temp_db, vs_temp_db
    ):
        monkeypatch.setattr(admin_module, "live_guild_ids", AsyncMock(return_value=set()))
        inter = interaction()

        await command(inter)

        body = followup(inter).get("content") or inter.followup.send.call_args.args[0]
        assert "no servers found" in body.lower()

    @pytest.mark.asyncio
    async def test_a_live_guild_never_appears_as_a_candidate(
        self, command, admin_module, monkeypatch, temp_db, cd_temp_db, vs_temp_db
    ):
        from config import GuildConfig, save_config

        save_config(GuildConfig(guild_id=LIVE_GUILD, leadership_channel_id=1))
        monkeypatch.setattr(admin_module, "live_guild_ids", AsyncMock(return_value={LIVE_GUILD}))
        inter = interaction()

        await command(inter)

        body = followup(inter).get("content") or inter.followup.send.call_args.args[0]
        assert "no servers found" in body.lower()

    @pytest.mark.asyncio
    async def test_a_genuine_candidate_is_previewed_not_actioned(
        self, command, admin_module, monkeypatch, temp_db, cd_temp_db, vs_temp_db
    ):
        from config import GuildConfig, save_config

        save_config(GuildConfig(guild_id=GUILD, leadership_channel_id=1))
        monkeypatch.setattr(admin_module, "live_guild_ids", AsyncMock(return_value=set()))
        inter = interaction()

        await command(inter)

        embed = followup(inter)["embed"]
        assert str(GUILD) in embed.description
        assert config.guild_removal_held_since(GUILD) is None, "preview must not start anything"
        view = followup(inter)["view"]
        assert isinstance(view, admin_module._BackfillConfirm)


class TestConfirmAndCancel:
    @pytest.mark.asyncio
    async def test_confirm_starts_a_hold_for_every_candidate(
        self, admin_module, temp_db, cd_temp_db, vs_temp_db
    ):
        view = admin_module._BackfillConfirm({GUILD: None, LIVE_GUILD: "2020-01-01"}, OWNER_ID)
        inter = interaction()

        await view.confirm.callback(inter)

        assert config.guild_removal_held_since(GUILD) is not None
        assert config.guild_removal_held_since(LIVE_GUILD) is not None
        body = inter.edit_original_response.call_args.kwargs["content"]
        assert "2" in body

    @pytest.mark.asyncio
    async def test_cancel_starts_nothing(self, admin_module, temp_db, cd_temp_db, vs_temp_db):
        view = admin_module._BackfillConfirm({GUILD: None}, OWNER_ID)
        inter = interaction()

        await view.cancel.callback(inter)

        assert config.guild_removal_held_since(GUILD) is None


@pytest.fixture
def cd_temp_db(tmp_path, monkeypatch):
    import champion_duel_db as cd_db

    monkeypatch.setattr(cd_db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    cd_db.init_db()
    return None


@pytest.fixture
def vs_temp_db(tmp_path, monkeypatch):
    import alliance_duel_db as vs_db

    monkeypatch.setattr(vs_db, "DB_PATH", str(tmp_path / "alliance_duel.sqlite3"))
    vs_db.init_db()
    return None


class TestLiveGuildIds:
    """The safety check #649 requires before either the command or the daily
    sweep trusts a guild list: `bot.guilds` alone can be partial for the
    2 seconds after startup discord.py's `guild_ready_timeout` allows."""

    @pytest.mark.asyncio
    async def test_not_ready_returns_none(self, admin_module, monkeypatch):
        monkeypatch.setattr(admin_module.bot, "is_ready", MagicMock(return_value=False))

        assert await admin_module.live_guild_ids() is None

    @pytest.mark.asyncio
    async def test_unions_the_gateway_cache_with_a_live_fetch(self, admin_module, monkeypatch):
        async def _fake_fetch_guilds(limit=None):
            for gid in (2, 3):
                yield MagicMock(id=gid)

        monkeypatch.setattr(admin_module.bot, "is_ready", MagicMock(return_value=True))
        # `guilds` is a read-only property on the real discord.py Bot class,
        # not an instance attribute -- overridden at the class level for the
        # life of this test, same as patching any other property.
        monkeypatch.setattr(
            type(admin_module.bot),
            "guilds",
            property(lambda self: [MagicMock(id=1), MagicMock(id=2)]),
        )
        monkeypatch.setattr(admin_module.bot, "fetch_guilds", _fake_fetch_guilds)

        assert await admin_module.live_guild_ids() == {1, 2, 3}
