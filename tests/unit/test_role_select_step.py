"""
Tests for `setup_cog.RoleSelectStep` — the role picker used in `/setup`
and (after #80 / #94) every other `/setup_*` wizard that takes a role.

The view was refactored from decorator-style buttons to all-manual
buttons so we can conditionally render a Keep-current button when the
wizard passes a `current_id` that still resolves to a live role.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# setup_cog imports premium → bot.py-adjacent; needs a fake DISCORD_TOKEN.
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")


def _make_role(name: str, role_id: int) -> MagicMock:
    role = MagicMock(spec=discord.Role)
    role.id = role_id
    role.name = name
    return role


def _make_guild(roles: list[MagicMock] | None = None) -> MagicMock:
    guild = MagicMock(spec=discord.Guild)
    role_list = roles or []
    roles_by_id = {r.id: r for r in role_list}
    guild.get_role = lambda i: roles_by_id.get(i)
    guild.roles = role_list
    return guild


def _labels(view) -> list[str]:
    return [c.label for c in view.children if getattr(c, "label", None)]


# ── Default (no current_id) path ──────────────────────────────────────────────


class TestDefaultRendering:
    """No `current_id` → the view renders just the select + create button,
    same as the pre-#94 behavior."""

    def test_no_current_id_renders_select_and_create_only(self):
        from setup_cog import RoleSelectStep

        view = RoleSelectStep("Pick a role...")
        labels = _labels(view)
        assert "➕ Create a new role" in labels
        assert not any("Keep current" in lbl for lbl in labels)
        # Exactly one select.
        selects = [c for c in view.children if isinstance(c, discord.ui.RoleSelect)]
        assert len(selects) == 1

    def test_is_current_stale_false_when_no_current_id(self):
        from setup_cog import RoleSelectStep

        view = RoleSelectStep("Pick a role...")
        assert view.is_current_stale is False


# ── Keep-current button (#94) ────────────────────────────────────────────────


class TestKeepCurrentRole:
    def test_current_id_resolves_renders_keep_button(self):
        from setup_cog import RoleSelectStep

        member = _make_role("Member", 42)
        guild = _make_guild([member])
        view = RoleSelectStep("Pick a role...", current_id=42, guild=guild)
        labels = _labels(view)
        assert any("Keep current" in lbl and "Member" in lbl for lbl in labels)
        assert view.is_current_stale is False

    def test_current_id_zero_treated_as_unset(self):
        from setup_cog import RoleSelectStep

        guild = _make_guild([_make_role("Member", 42)])
        view = RoleSelectStep("Pick a role...", current_id=0, guild=guild)
        labels = _labels(view)
        assert not any("Keep current" in lbl for lbl in labels)
        assert view.is_current_stale is False

    def test_stale_current_id_flags_is_current_stale(self):
        from setup_cog import RoleSelectStep

        guild = _make_guild([])  # role was deleted
        view = RoleSelectStep(
            "Pick a role...",
            current_id=99,
            current_name="Member",
            guild=guild,
        )
        labels = _labels(view)
        assert not any("Keep current" in lbl for lbl in labels)
        assert view.is_current_stale is True
        # Picker still renders.
        assert any(isinstance(c, discord.ui.RoleSelect) for c in view.children)

    @pytest.mark.asyncio
    async def test_clicking_keep_sets_selected_role_and_stops(self):
        from setup_cog import RoleSelectStep

        member = _make_role("Member", 42)
        guild = _make_guild([member])
        view = RoleSelectStep("Pick a role...", current_id=42, guild=guild)
        keep_btn = next(
            c
            for c in view.children
            if isinstance(c, discord.ui.Button) and "Keep current" in (c.label or "")
        )
        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        await keep_btn.callback(inter)

        assert view.selected_role is member
        assert view.confirmed is True
        assert view.is_finished()

    def test_no_guild_means_no_keep_button_even_with_id(self):
        """`guild` is needed to resolve current_id. Without it the keep
        button simply doesn't render — graceful degradation."""
        from setup_cog import RoleSelectStep

        view = RoleSelectStep("Pick a role...", current_id=42)
        labels = _labels(view)
        assert not any("Keep current" in lbl for lbl in labels)

    def test_long_role_name_clipped_to_80_chars(self):
        from setup_cog import RoleSelectStep

        long_name = "x" * 200
        long_role = _make_role(long_name, 42)
        guild = _make_guild([long_role])
        view = RoleSelectStep("Pick a role...", current_id=42, guild=guild)
        keep_btn = next(
            c
            for c in view.children
            if isinstance(c, discord.ui.Button) and "Keep current" in (c.label or "")
        )
        assert len(keep_btn.label) <= 80


# ── Name-based fallback (#106) ───────────────────────────────────────────────


class TestNameFallback:
    """Old guilds stored the leadership role by name only — the
    `leadership_role_id` column defaults to 0 after migration. Without
    a name-based fallback, re-running /setup on those guilds shows no
    Keep-current button. The fallback resolves the saved name against
    `guild.roles` so the button renders anyway."""

    def test_zero_id_with_name_resolves_via_guild_roles(self):
        from setup_cog import RoleSelectStep

        leader = _make_role("Leadership", 77)
        guild = _make_guild([leader, _make_role("Member", 88)])
        view = RoleSelectStep(
            "Pick a role...",
            current_id=0,  # migration default
            current_name="Leadership",  # the only thing we knew
            guild=guild,
        )
        labels = _labels(view)
        assert any("Keep current" in lbl and "Leadership" in lbl for lbl in labels)
        assert view.is_current_stale is False

    def test_id_misses_but_name_hits_falls_back(self):
        """Stale id (role recreated, new id) but name still matches —
        prefer the name match over showing a 'deleted' warning."""
        from setup_cog import RoleSelectStep

        leader = _make_role("Leadership", 99)
        guild = _make_guild([leader])
        view = RoleSelectStep(
            "Pick a role...",
            current_id=11,  # stale id
            current_name="Leadership",
            guild=guild,
        )
        labels = _labels(view)
        assert any("Keep current" in lbl for lbl in labels)
        assert view.is_current_stale is False

    def test_id_zero_and_name_misses_flags_stale(self):
        from setup_cog import RoleSelectStep

        guild = _make_guild([_make_role("OtherRole", 1)])
        view = RoleSelectStep(
            "Pick a role...",
            current_id=0,
            current_name="Leadership",
            guild=guild,
        )
        labels = _labels(view)
        assert not any("Keep current" in lbl for lbl in labels)
        assert view.is_current_stale is True

    def test_id_zero_and_no_name_is_not_stale(self):
        """Truly new guild — no saved value of any kind. Don't warn."""
        from setup_cog import RoleSelectStep

        guild = _make_guild([])
        view = RoleSelectStep(
            "Pick a role...",
            current_id=0,
            current_name="",
            guild=guild,
        )
        assert view.is_current_stale is False

    @pytest.mark.asyncio
    async def test_name_fallback_keep_returns_resolved_role(self):
        """Clicking the keep button from the name-fallback path still
        gives the caller a real Role object, not a stub."""
        from setup_cog import RoleSelectStep

        leader = _make_role("Leadership", 77)
        guild = _make_guild([leader])
        view = RoleSelectStep(
            "Pick a role...",
            current_id=0,
            current_name="Leadership",
            guild=guild,
        )
        keep_btn = next(
            c
            for c in view.children
            if isinstance(c, discord.ui.Button) and "Keep current" in (c.label or "")
        )
        inter = MagicMock()
        inter.response.edit_message = AsyncMock()
        await keep_btn.callback(inter)
        assert view.selected_role is leader
        assert view.confirmed is True
