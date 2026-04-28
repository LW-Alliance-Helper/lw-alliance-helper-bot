"""
Unit tests for member_roster.py — Member Roster Sync (Premium feature).

Covers row-building (column placement, role filtering, bot exclusion,
sorting, role-string formatting) and the sheet-write contract via a
spy worksheet. The sync command's premium-gating is also exercised so
free-tier guilds can't bypass /sync_members.
"""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID
from config import OGV_GUILD_ID


# ── Premium-env isolation (so the FORCE_PREMIUM=1 CI lane doesn't leak in) ────
@pytest.fixture(autouse=True)
def _isolate_premium_env(monkeypatch):
    import importlib
    for var in ("PREMIUM_SKU_ID", "FORCE_PREMIUM", "PREMIUM_TEST_GUILD_IDS"):
        monkeypatch.delenv(var, raising=False)
    import premium as _premium
    importlib.reload(_premium)
    _premium.clear_cache()
    yield
    for var in ("PREMIUM_SKU_ID", "FORCE_PREMIUM", "PREMIUM_TEST_GUILD_IDS"):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(_premium)
    _premium.clear_cache()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_role(role_id: int, name: str):
    role      = MagicMock()
    role.id   = role_id
    role.name = name
    return role


def _make_member(
    member_id: int, name: str, display_name: str = None,
    roles: list = None, is_bot: bool = False, joined_at=None,
):
    m              = MagicMock()
    m.id           = member_id
    m.name         = name
    m.display_name = display_name or name
    m.bot          = is_bot
    m.roles        = roles or []
    m.joined_at    = joined_at
    return m


def _default_cfg(role_filter_id: int = 0) -> dict:
    return {
        "guild_id":       TEST_GUILD_ID,
        "enabled":        1,
        "tab_name":       "Member Roster",
        "discord_id_col": 0,
        "name_col":       1,
        "display_col":    2,
        "joined_col":     3,
        "roles_col":      4,
        "role_filter_id": role_filter_id,
        "auto_sync":      1,
        "last_synced_at": "",
    }


# ── Row builder ───────────────────────────────────────────────────────────────

class TestBuildRosterRows:

    def test_header_row_uses_configured_columns(self):
        from member_roster import _build_roster_rows

        guild       = MagicMock()
        guild.id    = TEST_GUILD_ID
        guild.members = []

        rows = _build_roster_rows(guild, _default_cfg())

        assert rows[0] == ["Discord ID", "Name", "Display Name", "Joined", "Roles"]

    def test_excludes_bots_from_roster(self):
        from member_roster import _build_roster_rows

        guild         = MagicMock()
        guild.members = [
            _make_member(1, "Alice"),
            _make_member(2, "BotUser", is_bot=True),
            _make_member(3, "Bob"),
        ]
        rows = _build_roster_rows(guild, _default_cfg())

        names = [r[1] for r in rows[1:]]
        assert "Alice" in names
        assert "Bob"   in names
        assert "BotUser" not in names

    def test_role_filter_only_keeps_matching_members(self):
        from member_roster import _build_roster_rows

        member_role = _make_role(999, "Member")
        other_role  = _make_role(111, "Visitor")
        guild       = MagicMock()
        guild.members = [
            _make_member(1, "Alice", roles=[member_role]),
            _make_member(2, "Bob",   roles=[other_role]),
            _make_member(3, "Carol", roles=[member_role, other_role]),
        ]

        rows = _build_roster_rows(guild, _default_cfg(role_filter_id=999))
        names = [r[1] for r in rows[1:]]
        assert names == ["Alice", "Carol"]

    def test_role_filter_zero_includes_everyone_non_bot(self):
        from member_roster import _build_roster_rows

        guild         = MagicMock()
        guild.members = [
            _make_member(1, "Alice"),
            _make_member(2, "Bob"),
        ]
        rows = _build_roster_rows(guild, _default_cfg(role_filter_id=0))
        assert len(rows) == 3   # header + 2 members

    def test_members_sorted_by_display_name(self):
        from member_roster import _build_roster_rows

        guild         = MagicMock()
        guild.members = [
            _make_member(1, "zoey",  display_name="Zoey"),
            _make_member(2, "alpha", display_name="Alpha"),
            _make_member(3, "mike",  display_name="Mike"),
        ]
        rows = _build_roster_rows(guild, _default_cfg())
        display_names = [r[2] for r in rows[1:]]
        assert display_names == ["Alpha", "Mike", "Zoey"]

    def test_roles_string_excludes_at_everyone_and_sorts(self):
        from member_roster import _build_roster_rows

        guild = MagicMock()
        guild.members = [
            _make_member(
                1, "Alice",
                roles=[
                    _make_role(0, "@everyone"),
                    _make_role(1, "Zeta"),
                    _make_role(2, "Alpha"),
                ],
            ),
        ]
        rows = _build_roster_rows(guild, _default_cfg())
        # roles col is index 4
        assert rows[1][4] == "Alpha, Zeta"

    def test_joined_date_formatted_yyyy_mm_dd(self):
        from member_roster import _build_roster_rows
        from datetime import datetime as dt

        guild = MagicMock()
        guild.members = [
            _make_member(1, "Alice",
                joined_at=dt(2024, 8, 15, 12, 0, tzinfo=timezone.utc)),
        ]
        rows = _build_roster_rows(guild, _default_cfg())
        # joined_col is index 3
        assert rows[1][3] == "2024-08-15"

    def test_joined_date_blank_when_unknown(self):
        from member_roster import _build_roster_rows

        guild = MagicMock()
        guild.members = [_make_member(1, "Alice", joined_at=None)]
        rows = _build_roster_rows(guild, _default_cfg())
        assert rows[1][3] == ""


# ── Sheet write ───────────────────────────────────────────────────────────────

class TestWriteRoster:

    def test_clears_then_writes_header_plus_members(self, seeded_db):
        from member_roster import write_roster
        import config

        guild         = MagicMock()
        guild.id      = TEST_GUILD_ID
        guild.members = [_make_member(1, "Alice"), _make_member(2, "Bob")]

        ws = MagicMock()
        ws.clear  = MagicMock()
        ws.update = MagicMock()

        with patch("member_roster.get_member_roster_sheet", return_value=ws):
            count = write_roster(guild, _default_cfg())

        assert count == 2
        ws.clear.assert_called_once()
        ws.update.assert_called_once()
        rows = ws.update.call_args.args[1]   # ("A1", rows, ...)
        assert rows[0][0] == "Discord ID"     # header
        assert rows[1][1] in {"Alice", "Bob"} # member name in name_col

    def test_updates_last_synced_at(self, seeded_db):
        from member_roster import write_roster
        import config

        guild         = MagicMock()
        guild.id      = TEST_GUILD_ID
        guild.members = []

        ws = MagicMock()
        with patch("member_roster.get_member_roster_sheet", return_value=ws):
            config.save_member_roster_config(TEST_GUILD_ID, enabled=1)
            write_roster(guild, _default_cfg())

        cfg_after = config.get_member_roster_config(TEST_GUILD_ID)
        assert cfg_after["last_synced_at"]   # non-empty ISO timestamp


# ── /sync_members premium gate ────────────────────────────────────────────────

class TestSyncMembersGate:

    @pytest.mark.asyncio
    async def test_free_tier_sees_premium_locked(self, seeded_db):
        from member_roster import MemberRosterCog
        import premium
        premium.clear_cache()

        bot = MagicMock()
        cog = MemberRosterCog(bot)

        interaction = AsyncMock()
        interaction.guild_id        = TEST_GUILD_ID
        interaction.entitlements    = []
        interaction.user            = MagicMock()
        interaction.user.guild_permissions.administrator = True
        interaction.response.send_message = AsyncMock()

        await cog.sync_members.callback(cog, interaction)

        call  = interaction.response.send_message.call_args
        embed = call.kwargs.get("embed")
        assert embed is not None
        assert "Premium" in embed.title

    @pytest.mark.asyncio
    async def test_non_admin_blocked_regardless_of_tier(self, seeded_db):
        from member_roster import MemberRosterCog
        import premium
        premium.clear_cache()

        bot = MagicMock()
        cog = MemberRosterCog(bot)

        interaction = AsyncMock()
        interaction.guild_id     = OGV_GUILD_ID   # always-premium
        interaction.entitlements = []
        interaction.user         = MagicMock()
        interaction.user.guild_permissions.administrator = False
        interaction.response.send_message = AsyncMock()

        await cog.sync_members.callback(cog, interaction)

        call    = interaction.response.send_message.call_args
        content = call.args[0] if call.args else call.kwargs.get("content")
        # The gate now allows leadership-or-admin (rather than admin-only),
        # so the rejection text mentions both. Either is acceptable.
        lowered = (content or "").lower()
        assert "leadership" in lowered or "administrators" in lowered

    @pytest.mark.asyncio
    async def test_premium_admin_with_unconfigured_roster_gets_setup_hint(self, seeded_db):
        """OGV is premium but roster_config.enabled=0 → asks them to /setup_members."""
        from member_roster import MemberRosterCog
        import premium
        premium.clear_cache()

        bot = MagicMock()
        cog = MemberRosterCog(bot)

        interaction = AsyncMock()
        interaction.guild_id     = OGV_GUILD_ID
        interaction.entitlements = []
        interaction.user         = MagicMock()
        interaction.user.guild_permissions.administrator = True
        interaction.response.send_message = AsyncMock()

        await cog.sync_members.callback(cog, interaction)

        call    = interaction.response.send_message.call_args
        content = call.args[0] if call.args else call.kwargs.get("content")
        assert "setup_members" in (content or "")


# ── Discord ID lookup ─────────────────────────────────────────────────────────

class TestLookupDiscordId:

    def test_finds_id_by_display_name(self, seeded_db, monkeypatch):
        import config
        config.save_member_roster_config(TEST_GUILD_ID, enabled=1, tab_name="Members")

        ws = MagicMock()
        ws.get_all_values = MagicMock(return_value=[
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["111", "alice_user", "Alice",   "", ""],
            ["222", "bob_user",   "Bob",     "", ""],
        ])
        monkeypatch.setattr(config, "get_member_roster_sheet", lambda gid, tab: ws)

        assert config.lookup_discord_id_for_name(TEST_GUILD_ID, "Alice") == "111"
        assert config.lookup_discord_id_for_name(TEST_GUILD_ID, "alice") == "111"   # case-insensitive
        assert config.lookup_discord_id_for_name(TEST_GUILD_ID, "Bob")   == "222"

    def test_returns_none_when_disabled(self, seeded_db):
        import config
        # roster config defaults to enabled=0
        assert config.lookup_discord_id_for_name(TEST_GUILD_ID, "Alice") is None

    def test_returns_none_when_no_match(self, seeded_db, monkeypatch):
        import config
        config.save_member_roster_config(TEST_GUILD_ID, enabled=1)

        ws = MagicMock()
        ws.get_all_values = MagicMock(return_value=[
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["111", "alice_user", "Alice", "", ""],
        ])
        monkeypatch.setattr(config, "get_member_roster_sheet", lambda gid, tab: ws)

        assert config.lookup_discord_id_for_name(TEST_GUILD_ID, "Nobody") is None
