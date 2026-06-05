"""Regression tests for setup_cog._has_leadership_or_admin.

#271: invoking the helper in a DM context (interaction.user is a
discord.User with no guild_permissions / roles) raised AttributeError.
/setup now carries @app_commands.guild_only(), but the helper is also
reachable from in-guild hub buttons, so it guards defensively and must
return False rather than raise for any non-Member / no-guild context.
"""

from unittest.mock import MagicMock, patch

import discord

from setup_cog import _has_leadership_or_admin


def _role(name):
    r = MagicMock()
    r.name = name
    return r


def _member(*, admin=False, roles=()):
    m = MagicMock(spec=discord.Member)
    perms = MagicMock()
    perms.administrator = admin
    m.guild_permissions = perms
    m.roles = [_role(n) for n in roles]
    return m


def _interaction(user, *, guild=object()):
    inter = MagicMock(spec=discord.Interaction)
    inter.user = user
    inter.guild = guild
    inter.guild_id = 123
    return inter


class TestMemberContext:
    def test_administrator_returns_true(self):
        inter = _interaction(_member(admin=True))
        # Admin short-circuits before any config lookup.
        assert _has_leadership_or_admin(inter) is True

    def test_leadership_role_returns_true(self):
        inter = _interaction(_member(admin=False, roles=("R5 Leadership",)))
        cfg = MagicMock(leadership_role_name="R5 Leadership")
        with patch("setup_cog.get_config", return_value=cfg):
            assert _has_leadership_or_admin(inter) is True

    def test_non_leadership_member_returns_false(self):
        inter = _interaction(_member(admin=False, roles=("Member",)))
        cfg = MagicMock(leadership_role_name="R5 Leadership")
        with patch("setup_cog.get_config", return_value=cfg):
            assert _has_leadership_or_admin(inter) is False


class TestNonMemberContext:
    """The #271 regression: a DM interaction (no guild) must not raise."""

    def test_dm_context_guild_none_returns_false(self):
        # In a DM, interaction.guild is None and interaction.user is a
        # discord.User with no guild_permissions. The guild guard must
        # short-circuit to False before guild_permissions is touched.
        user = MagicMock(spec=discord.User)
        inter = _interaction(user, guild=None)
        assert _has_leadership_or_admin(inter) is False
