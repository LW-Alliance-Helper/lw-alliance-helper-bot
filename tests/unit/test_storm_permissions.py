"""
Tests for `storm_permissions.is_leader_or_admin` and
`storm_permissions.ensure_premium_structured`.

These two helpers fan out across every storm cog, so getting them
right matters more than any individual cog's own tests.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import storm_permissions

from tests.unit.test_config import TEST_GUILD_ID


def _fake_admin_interaction(guild_id: int = TEST_GUILD_ID,
                            *, admin: bool = False,
                            role_names: list[str] | None = None,
                            in_guild: bool = True) -> MagicMock:
    """Build an `interaction.user` shaped object suitable for the helpers.

    `role_names` is the list of role names the member carries — matched
    against `cfg.leadership_role_name` in `is_leader_or_admin`.
    """
    inter = MagicMock(spec=discord.Interaction)
    inter.guild_id = guild_id if in_guild else None
    if in_guild:
        member = MagicMock(spec=discord.Member)
        perms = MagicMock()
        perms.administrator = admin
        member.guild_permissions = perms
        member.roles = [MagicMock(name=n) for n in (role_names or [])]
        for role, name in zip(member.roles, (role_names or [])):
            role.name = name
        inter.user = member
    else:
        inter.user = MagicMock(spec=discord.User)
    return inter


class TestIsLeaderOrAdmin:
    def test_admin_passes(self, seeded_db):
        inter = _fake_admin_interaction(admin=True)
        assert storm_permissions.is_leader_or_admin(inter) is True

    def test_no_leadership_role_configured_blocks_non_admin(self, seeded_db):
        # `seeded_db` includes a leadership_role_name; null it out.
        import config
        cfg = config.get_config(TEST_GUILD_ID)
        cfg.leadership_role_name = ""
        config.save_config(cfg)

        inter = _fake_admin_interaction(role_names=["Officer"])
        assert storm_permissions.is_leader_or_admin(inter) is False

    def test_matching_leadership_role_name_passes(self, seeded_db):
        import config
        cfg = config.get_config(TEST_GUILD_ID)
        cfg.leadership_role_name = "Officer"
        config.save_config(cfg)

        inter = _fake_admin_interaction(role_names=["Officer", "Other"])
        assert storm_permissions.is_leader_or_admin(inter) is True

    def test_dm_returns_false_safely(self, seeded_db):
        """No `discord.Member` in DMs — helper must not raise."""
        inter = _fake_admin_interaction(in_guild=False)
        assert storm_permissions.is_leader_or_admin(inter) is False


class TestEnsurePremiumStructured:
    @pytest.mark.asyncio
    async def test_non_premium_returns_false_and_sends_upgrade(self, seeded_db):
        inter = _fake_admin_interaction()
        inter.response = MagicMock()
        inter.response.is_done.return_value = False
        inter.response.send_message = AsyncMock()
        with patch("premium.is_premium", new=AsyncMock(return_value=False)):
            ok, structured = await storm_permissions.ensure_premium_structured(
                inter, "DS",
            )
        assert ok is False
        assert structured is None
        inter.response.send_message.assert_awaited_once()
        sent = inter.response.send_message.await_args.args[0]
        assert "Premium" in sent

    @pytest.mark.asyncio
    async def test_premium_but_flag_off_returns_false(self, seeded_db):
        inter = _fake_admin_interaction()
        inter.response = MagicMock()
        inter.response.is_done.return_value = False
        inter.response.send_message = AsyncMock()
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch(
                 "config.get_structured_storm_config",
                 return_value={"structured_flow_enabled": False},
             ):
            ok, structured = await storm_permissions.ensure_premium_structured(
                inter, "DS",
            )
        assert ok is False
        assert structured is None
        sent = inter.response.send_message.await_args.args[0]
        assert "/setup_desertstorm" in sent

    @pytest.mark.asyncio
    async def test_premium_and_flag_on_returns_structured_cfg(self, seeded_db):
        inter = _fake_admin_interaction()
        inter.response = MagicMock()
        inter.response.is_done.return_value = False
        inter.response.send_message = AsyncMock()
        with patch("premium.is_premium", new=AsyncMock(return_value=True)), \
             patch(
                 "config.get_structured_storm_config",
                 return_value={"structured_flow_enabled": True, "signups_tab": "DS Signups"},
             ):
            ok, structured = await storm_permissions.ensure_premium_structured(
                inter, "DS",
            )
        assert ok is True
        assert structured == {"structured_flow_enabled": True, "signups_tab": "DS Signups"}
        inter.response.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_post_defer_uses_followup(self, seeded_db):
        """If the interaction is already deferred, the failure message
        must go through `followup.send` instead of `response.send_message`."""
        inter = _fake_admin_interaction()
        inter.response = MagicMock()
        inter.response.is_done.return_value = True
        inter.followup = MagicMock()
        inter.followup.send = AsyncMock()
        with patch("premium.is_premium", new=AsyncMock(return_value=False)):
            ok, _ = await storm_permissions.ensure_premium_structured(
                inter, "CS",
            )
        assert ok is False
        inter.followup.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_dm_returns_false(self, seeded_db):
        inter = _fake_admin_interaction(in_guild=False)
        inter.response = MagicMock()
        inter.response.is_done.return_value = False
        inter.response.send_message = AsyncMock()
        ok, _ = await storm_permissions.ensure_premium_structured(inter, "DS")
        assert ok is False
        sent = inter.response.send_message.await_args.args[0]
        assert "server" in sent.lower()
