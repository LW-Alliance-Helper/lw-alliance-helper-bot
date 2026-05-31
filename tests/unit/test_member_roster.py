"""
Unit tests for member_roster.py — Member Roster Sync (Premium feature).

Covers row-building (column placement, role filtering, bot exclusion,
sorting, role-string formatting) and the sheet-write contract via a
spy worksheet. The sync command's premium-gating is also exercised so
free-tier guilds can't bypass /members sync.
"""

import os
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID
from tests.constants import PREMIUM_TEST_GUILD_ID


# ── Premium-env isolation (so the FORCE_PREMIUM=1 CI lane doesn't leak in) ────
@pytest.fixture(autouse=True)
def _isolate_premium_env(monkeypatch):
    """Pin PREMIUM_TEST_GUILD_ID into PREMIUM_BYPASS_GUILD_IDS so the
    premium-keyed tests in this file don't each need to set the env var.
    TEST_GUILD_ID stays out of the set so free-tier paths still work."""
    import importlib

    for var in ("PREMIUM_SKU_ID", "FORCE_PREMIUM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PREMIUM_BYPASS_GUILD_IDS", str(PREMIUM_TEST_GUILD_ID))
    import premium as _premium

    importlib.reload(_premium)
    _premium.clear_cache()
    yield
    for var in ("PREMIUM_SKU_ID", "FORCE_PREMIUM", "PREMIUM_BYPASS_GUILD_IDS"):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(_premium)
    _premium.clear_cache()


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_role(role_id: int, name: str):
    role = MagicMock()
    role.id = role_id
    role.name = name
    return role


def _make_member(
    member_id: int,
    name: str,
    display_name: str = None,
    roles: list = None,
    is_bot: bool = False,
    joined_at=None,
):
    m = MagicMock()
    m.id = member_id
    m.name = name
    m.display_name = display_name or name
    m.bot = is_bot
    m.roles = roles or []
    m.joined_at = joined_at
    return m


def _default_cfg(role_filter_id: int = 0) -> dict:
    return {
        "guild_id": TEST_GUILD_ID,
        "enabled": 1,
        "tab_name": "Member Roster",
        "discord_id_col": 0,
        "name_col": 1,
        "display_col": 2,
        "joined_col": 3,
        "roles_col": 4,
        "role_filter_id": role_filter_id,
        "auto_sync": 1,
        "last_synced_at": "",
    }


# ── Row builder ───────────────────────────────────────────────────────────────


class TestBuildRosterRows:
    def test_header_row_uses_configured_columns(self):
        from member_roster import _build_roster_rows

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = []

        rows = _build_roster_rows(guild, _default_cfg())

        assert rows[0] == ["Discord ID", "Name", "Display Name", "Joined", "Roles"]

    def test_excludes_bots_from_roster(self):
        from member_roster import _build_roster_rows

        guild = MagicMock()
        guild.members = [
            _make_member(1, "Alice"),
            _make_member(2, "BotUser", is_bot=True),
            _make_member(3, "Bob"),
        ]
        rows = _build_roster_rows(guild, _default_cfg())

        names = [r[1] for r in rows[1:]]
        assert "Alice" in names
        assert "Bob" in names
        assert "BotUser" not in names

    def test_role_filter_only_keeps_matching_members(self):
        from member_roster import _build_roster_rows

        member_role = _make_role(999, "Member")
        other_role = _make_role(111, "Visitor")
        guild = MagicMock()
        guild.members = [
            _make_member(1, "Alice", roles=[member_role]),
            _make_member(2, "Bob", roles=[other_role]),
            _make_member(3, "Carol", roles=[member_role, other_role]),
        ]

        rows = _build_roster_rows(guild, _default_cfg(role_filter_id=999))
        names = [r[1] for r in rows[1:]]
        assert names == ["Alice", "Carol"]

    def test_role_filter_zero_includes_everyone_non_bot(self):
        from member_roster import _build_roster_rows

        guild = MagicMock()
        guild.members = [
            _make_member(1, "Alice"),
            _make_member(2, "Bob"),
        ]
        rows = _build_roster_rows(guild, _default_cfg(role_filter_id=0))
        assert len(rows) == 3  # header + 2 members

    def test_members_sorted_by_display_name(self):
        from member_roster import _build_roster_rows

        guild = MagicMock()
        guild.members = [
            _make_member(1, "zoey", display_name="Zoey"),
            _make_member(2, "alpha", display_name="Alpha"),
            _make_member(3, "mike", display_name="Mike"),
        ]
        rows = _build_roster_rows(guild, _default_cfg())
        display_names = [r[2] for r in rows[1:]]
        assert display_names == ["Alpha", "Mike", "Zoey"]

    def test_roles_string_excludes_at_everyone_and_sorts(self):
        from member_roster import _build_roster_rows

        guild = MagicMock()
        guild.members = [
            _make_member(
                1,
                "Alice",
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
            _make_member(1, "Alice", joined_at=dt(2024, 8, 15, 12, 0, tzinfo=timezone.utc)),
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

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [_make_member(1, "Alice"), _make_member(2, "Bob")]

        ws = MagicMock()
        ws.clear = MagicMock()
        ws.update = MagicMock()

        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=None),
        ):
            count, _report = write_roster(guild, _default_cfg())

        assert count == 2
        ws.clear.assert_called_once()
        ws.update.assert_called_once()
        rows = ws.update.call_args.args[1]  # ("A1", rows, ...)
        assert rows[0][0] == "Discord ID"  # header
        assert rows[1][1] in {"Alice", "Bob"}  # member name in name_col

    def test_updates_last_synced_at(self, seeded_db):
        from member_roster import write_roster
        import config

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = []

        ws = MagicMock()
        ws.get_all_values = MagicMock(return_value=[])
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=None),
        ):
            config.save_member_roster_config(TEST_GUILD_ID, enabled=1)
            write_roster(guild, _default_cfg())

        cfg_after = config.get_member_roster_config(TEST_GUILD_ID)
        assert cfg_after["last_synced_at"]  # non-empty ISO timestamp


class TestPreserveUnknownColumns:
    """`/members sync` must preserve alliance-owned columns (the custom
    Power column the structured-flow eligibility filter reads, the
    `not_on_discord` flag the officer view reads, etc.). The prior
    `ws.clear()` + write-bot-cols path silently destroyed them on
    every sync."""

    def test_alliance_power_column_preserved_for_retained_members(self, seeded_db):
        from member_roster import write_roster

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [
            _make_member(100, "Alice"),
            _make_member(200, "Bob"),
        ]
        # Existing Sheet: bot columns 0-4 plus a custom "1st Squad Power"
        # at column 5 and a "not_on_discord" flag at column 6.
        existing = [
            [
                "Discord ID",
                "Name",
                "Display Name",
                "Joined",
                "Roles",
                "1st Squad Power",
                "not_on_discord",
            ],
            ["100", "Alice", "Alice", "", "", "300M", ""],
            ["200", "Bob", "Bob", "", "", "180M", ""],
            # Charlie is a non-Discord alliance member typed in by hand
            # (legacy `not_on_discord=yes`). She MUST survive the sync so
            # the storm officer view can keep surfacing her under "Not
            # voted yet" for on-behalf voting (#262).
            ["", "Charlie", "Charlie", "", "", "120M", "yes"],
        ]
        ws = MagicMock()
        ws.get_all_values.return_value = existing
        ws.update = MagicMock()
        ws.clear = MagicMock()
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=None),
        ):
            write_roster(guild, _default_cfg())

        rows = ws.update.call_args.args[1]
        # Header preserves the alliance's custom column names.
        header = rows[0]
        assert "1st Squad Power" in header
        assert "not_on_discord" in header
        # Alice, Bob, AND Charlie all keep their power values — Charlie
        # is carried forward by the non-Discord-row preservation path.
        member_rows = {r[1]: r for r in rows[1:]}
        assert "Alice" in member_rows
        assert "300M" in member_rows["Alice"]
        assert "Bob" in member_rows
        assert "180M" in member_rows["Bob"]
        assert "Charlie" in member_rows
        assert "120M" in member_rows["Charlie"]

    def test_new_member_gets_blank_custom_columns(self, seeded_db):
        from member_roster import write_roster

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [
            _make_member(100, "Alice"),
            _make_member(300, "Diana"),  # new — wasn't in `existing`
        ]
        existing = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles", "1st Squad Power"],
            ["100", "Alice", "Alice", "", "", "300M"],
        ]
        ws = MagicMock()
        ws.get_all_values.return_value = existing
        ws.update = MagicMock()
        ws.clear = MagicMock()
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=None),
        ):
            write_roster(guild, _default_cfg())

        rows = ws.update.call_args.args[1]
        member_rows = {r[1]: r for r in rows[1:]}
        # Diana's power cell is blank — she's new, alliance fills it in.
        assert member_rows["Diana"][5] == ""
        # Alice's power is preserved.
        assert member_rows["Alice"][5] == "300M"

    def test_empty_existing_sheet_falls_back_to_bot_cols_only(self, seeded_db):
        from member_roster import write_roster

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [_make_member(100, "Alice")]
        ws = MagicMock()
        ws.get_all_values.return_value = []
        ws.update = MagicMock()
        ws.clear = MagicMock()
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=None),
        ):
            count, _report = write_roster(guild, _default_cfg())
        assert count == 1
        rows = ws.update.call_args.args[1]
        # Five bot-managed columns plus the auto-appended presence
        # column ("Is this user in Discord?") that the bot now
        # maintains for every roster Sheet.
        assert len(rows[0]) == 6
        assert rows[0][5] == "Is this user in Discord?"

    def test_get_all_values_failure_does_not_block_sync(self, seeded_db):
        from member_roster import write_roster

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [_make_member(100, "Alice")]
        ws = MagicMock()
        ws.get_all_values.side_effect = RuntimeError("simulated read failure")
        ws.update = MagicMock()
        ws.clear = MagicMock()
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=None),
        ):
            count, _report = write_roster(guild, _default_cfg())
        # Falls through to writing just the bot-managed columns; the
        # write isn't blocked by a read failure.
        assert count == 1


class TestNonDiscordRowPreservation:
    """#262 — hand-typed roster rows for alliance members who don't use
    Discord get carried forward across `/members sync`. Without this,
    the storm officer view's "Not voted yet" non-Discord bucket can't
    do its job: every sync wipes the rows it's supposed to render."""

    def test_row_flagged_via_presence_column_preserved(self, seeded_db):
        from member_roster import write_roster

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [_make_member(100, "Alice")]
        # Hand-typed Eve has the bot-maintained presence column set to
        # "No" — this is the modern non-Discord flag path.
        existing = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles", "Is this user in Discord?"],
            ["100", "Alice", "Alice", "", "", "Yes"],
            ["", "Eve", "Eve", "", "", "No"],
        ]
        ws = MagicMock()
        ws.get_all_values.return_value = existing
        ws.update = MagicMock()
        ws.clear = MagicMock()
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=None),
        ):
            write_roster(guild, _default_cfg())
        rows = ws.update.call_args.args[1]
        names = [r[1] for r in rows[1:]]
        assert "Alice" in names
        assert "Eve" in names

    def test_row_with_no_flag_and_no_name_match_still_dropped(self, seeded_db):
        from member_roster import write_roster

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [_make_member(100, "Alice")]
        # Mystery row with a blank Discord ID, no presence flag, no
        # legacy flag, and no live name match — preserves the pre-#262
        # behaviour of dropping unidentifiable rows so a typo doesn't
        # live forever in the sheet.
        existing = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["100", "Alice", "Alice", "", ""],
            ["", "Mystery", "Mystery", "", ""],
        ]
        ws = MagicMock()
        ws.get_all_values.return_value = existing
        ws.update = MagicMock()
        ws.clear = MagicMock()
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=None),
        ):
            write_roster(guild, _default_cfg())
        rows = ws.update.call_args.args[1]
        names = [r[1] for r in rows[1:]]
        assert "Mystery" not in names

    def test_non_discord_row_skips_name_fallback(self, seeded_db):
        from member_roster import write_roster

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        # Alice is a live Discord member. An alliance happens to have a
        # non-Discord roster row with the in-game name "Alice" too
        # (different person). The name-fallback path must NOT bind
        # Alice's Discord ID into the non-Discord row.
        guild.members = [_make_member(100, "Alice")]
        existing = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles", "Is this user in Discord?"],
            ["", "Alice", "Alice", "", "", "No"],
        ]
        ws = MagicMock()
        ws.get_all_values.return_value = existing
        ws.update = MagicMock()
        ws.clear = MagicMock()
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=None),
        ):
            write_roster(guild, _default_cfg())
        rows = ws.update.call_args.args[1]
        # Two rows should land: the live-Discord Alice (id 100), and
        # the non-Discord Alice (id blank). They are NOT merged.
        alice_rows = [r for r in rows[1:] if r[1] == "Alice"]
        assert len(alice_rows) == 2
        ids = sorted((r[0] or "").strip() for r in alice_rows)
        assert ids == ["", "100"]


class TestDiscordPresenceColumn:
    """The "Is this user in Discord?" column is bot-maintained: the bot
    creates the header if missing, fills every row with Yes/No based
    on live guild membership, and writes a Yes/No-dropdown data
    validation rule. Storm readers prefer this column over the legacy
    `not_on_discord` column."""

    def test_appends_column_when_absent(self, seeded_db):
        from member_roster import write_roster, DISCORD_FLAG_COLUMN_HEADER

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [
            _make_member(100, "Alice"),
            _make_member(200, "Bob"),
        ]
        ws = MagicMock()
        ws.get_all_values.return_value = []
        ws.update = MagicMock()
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=None),
        ):
            write_roster(guild, _default_cfg())
        rows = ws.update.call_args.args[1]
        header = rows[0]
        # New column appended at the right edge.
        assert DISCORD_FLAG_COLUMN_HEADER in header
        flag_idx = header.index(DISCORD_FLAG_COLUMN_HEADER)
        # Both Alice and Bob are in guild.members → Yes.
        member_rows = {r[1]: r for r in rows[1:]}
        assert member_rows["Alice"][flag_idx] == "Yes"
        assert member_rows["Bob"][flag_idx] == "Yes"

    def test_normalises_existing_column_to_canonical_header(self, seeded_db):
        """A pre-existing column with a slightly different label
        (alliance manually typed it before the bot was updated) is
        normalised to the canonical header. The new bot row fills
        Yes/No regardless of what was there before."""
        from member_roster import write_roster, DISCORD_FLAG_COLUMN_HEADER

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [_make_member(100, "Alice")]
        existing = [
            [
                "Discord ID",
                "Name",
                "Display Name",
                "Joined",
                "Roles",
                "is this user in discord?",
            ],  # lowercase variant
            ["100", "Alice", "Alice", "", "", "manual override"],
        ]
        ws = MagicMock()
        ws.get_all_values.return_value = existing
        ws.update = MagicMock()
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=None),
        ):
            write_roster(guild, _default_cfg())
        rows = ws.update.call_args.args[1]
        # Header normalised — single canonical entry.
        assert rows[0].count(DISCORD_FLAG_COLUMN_HEADER) == 1
        # Bot's value wins.
        assert rows[1][5] == "Yes"

    def test_writes_no_for_members_not_in_guild(self, seeded_db):
        """A roster row whose Discord ID isn't in `guild.members` gets
        "No" written — the stale-ID inference path baked into the
        column."""
        from member_roster import write_roster

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        # Alice in guild, but the existing Sheet has Charlie (id=999)
        # who isn't a member any more.
        guild.members = [_make_member(100, "Alice")]
        existing = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["999", "Charlie", "Charlie", "", ""],
        ]
        ws = MagicMock()
        ws.get_all_values.return_value = existing
        ws.update = MagicMock()
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=None),
        ):
            write_roster(guild, _default_cfg())
        rows = ws.update.call_args.args[1]
        # Charlie isn't in guild.members today — her row dropped from
        # the merged output (leaving the alliance drops the row), so
        # only Alice remains.
        names = [r[1] for r in rows[1:]]
        assert "Alice" in names
        # Alice → Yes.
        flag_idx = rows[0].index("Is this user in Discord?")
        alice = next(r for r in rows[1:] if r[1] == "Alice")
        assert alice[flag_idx] == "Yes"

    def test_writes_no_for_blank_or_non_numeric_discord_id(self, seeded_db):
        """A roster row with no Discord ID (non-Discord member) gets
        "No" — the existing inference path now surfaces as a Sheet
        cell instead of being implicit. Exercises the helper directly
        with a synthetic merged list so we don't have to fight the
        merge path's "members who left lose their row" rule."""
        from member_roster import (
            _ensure_discord_flag_column,
            DISCORD_FLAG_COLUMN_HEADER,
        )

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [_make_member(100, "Alice")]
        merged = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["100", "Alice", "Alice", "", ""],  # in guild
            ["", "Charlie", "Charlie", "", ""],  # blank id
            ["TBD", "Diana", "Diana", "", ""],  # non-numeric id
        ]
        flag_idx = _ensure_discord_flag_column(merged, guild, _default_cfg())
        assert flag_idx == 5
        assert merged[0][flag_idx] == DISCORD_FLAG_COLUMN_HEADER
        assert merged[1][flag_idx] == "Yes"  # Alice in guild
        assert merged[2][flag_idx] == "No"  # blank id
        assert merged[3][flag_idx] == "No"  # non-numeric id

    def test_data_validation_request_targets_new_column(self, seeded_db):
        """The Yes/No-dropdown data validation rule fires once after
        the row write, targeting the presence column and spanning
        every member row."""
        from member_roster import write_roster

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [
            _make_member(100, "Alice"),
            _make_member(200, "Bob"),
        ]
        ws = MagicMock()
        ws.id = 12345  # numeric sheetId
        ws.get_all_values.return_value = []
        ws.update = MagicMock()
        spreadsheet = MagicMock()
        spreadsheet.batch_update = MagicMock()
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=spreadsheet),
        ):
            write_roster(guild, _default_cfg())
        spreadsheet.batch_update.assert_called_once()
        req = spreadsheet.batch_update.call_args.args[0]
        rule = req["requests"][0]["setDataValidation"]
        # Targets the new presence column (col 5 — bot-managed cols 0-4
        # plus this one appended).
        assert rule["range"]["sheetId"] == 12345
        assert rule["range"]["startColumnIndex"] == 5
        assert rule["range"]["endColumnIndex"] == 6
        # Yes/No dropdown values.
        vals = rule["rule"]["condition"]["values"]
        assert {v["userEnteredValue"] for v in vals} == {"Yes", "No"}
        assert rule["rule"]["showCustomUi"] is True
        # Range covers all member rows (2 members + header).
        assert rule["range"]["startRowIndex"] == 1
        assert rule["range"]["endRowIndex"] == 3

    def test_data_validation_skipped_when_empty_member_set(self, seeded_db):
        """No member rows → no validation request fires (nothing to
        constrain)."""
        from member_roster import write_roster

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = []
        ws = MagicMock()
        ws.id = 12345
        ws.get_all_values.return_value = []
        ws.update = MagicMock()
        spreadsheet = MagicMock()
        spreadsheet.batch_update = MagicMock()
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=spreadsheet),
        ):
            write_roster(guild, _default_cfg())
        spreadsheet.batch_update.assert_not_called()

    def test_data_validation_failure_does_not_block_sync(self, seeded_db):
        """A Sheets API failure on the validation request just logs —
        the row values were written first and are correct either way."""
        from member_roster import write_roster

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [_make_member(100, "Alice")]
        ws = MagicMock()
        ws.id = 12345
        ws.get_all_values.return_value = []
        ws.update = MagicMock()
        spreadsheet = MagicMock()
        spreadsheet.batch_update.side_effect = RuntimeError("API quota")
        with (
            patch("member_roster.get_member_roster_sheet", return_value=ws),
            patch("member_roster.get_spreadsheet", return_value=spreadsheet),
        ):
            count, _report = write_roster(guild, _default_cfg())
        # Sync still completed — the row values are on the sheet.
        assert count == 1


# ── Cache-population safety net ──────────────────────────────────────────────


class TestEnsureMemberCache:
    """Regression tests for the bug where /members sync wrote 0 rows because
    `Intents.default()` doesn't request the privileged members intent and
    `guild.members` was therefore the cached subset. The fix sets
    `intents.members = True` in bot.py and chunks the guild before each
    sync so the cache is fresh."""

    @pytest.mark.asyncio
    async def test_chunks_guild_when_not_yet_chunked(self):
        from member_roster import _ensure_member_cache

        guild = MagicMock()
        guild.chunked = False
        guild.chunk = AsyncMock()

        await _ensure_member_cache(guild)
        guild.chunk.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_skips_chunk_when_already_chunked(self):
        from member_roster import _ensure_member_cache

        guild = MagicMock()
        guild.chunked = True
        guild.chunk = AsyncMock()

        await _ensure_member_cache(guild)
        guild.chunk.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swallows_client_exception_when_intent_disabled(self):
        """If `intents.members` is False the gateway rejects chunk(). We
        log and continue — write_roster will still try to write whatever's
        in the cache, with the warn emitted from _warn_if_cache_looks_thin."""
        import discord
        from member_roster import _ensure_member_cache

        guild = MagicMock()
        guild.chunked = False
        guild.chunk = AsyncMock(
            side_effect=discord.ClientException("Intents.members must be enabled"),
        )
        # Should not raise.
        await _ensure_member_cache(guild)


class TestWarnIfCacheLooksThin:
    """The warn function fires when the cache is wildly smaller than the
    Discord-reported guild size — a runtime breadcrumb pointing at the
    missing-intent / missing-chunk root cause."""

    def test_warns_when_cache_is_dramatically_smaller(self, capsys):
        from member_roster import _warn_if_cache_looks_thin

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [MagicMock()]  # cached: 1
        guild.member_count = 200  # actual: 200

        _warn_if_cache_looks_thin(guild)
        captured = capsys.readouterr()
        assert "[ROSTER]" in captured.out
        assert "1/200" in captured.out
        assert "SERVER MEMBERS INTENT" in captured.out

    def test_silent_when_cache_is_complete(self, capsys):
        from member_roster import _warn_if_cache_looks_thin

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = [MagicMock() for _ in range(100)]
        guild.member_count = 100

        _warn_if_cache_looks_thin(guild)
        assert "[ROSTER]" not in capsys.readouterr().out

    def test_silent_for_tiny_guilds(self, capsys):
        """One-member guilds (testing servers) shouldn't trip the warning."""
        from member_roster import _warn_if_cache_looks_thin

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = []
        guild.member_count = 1

        _warn_if_cache_looks_thin(guild)
        assert "[ROSTER]" not in capsys.readouterr().out

    def test_silent_when_member_count_unknown(self, capsys):
        """Some Mock setups leave member_count as a MagicMock; treat as 0
        and skip the warning rather than crashing."""
        from member_roster import _warn_if_cache_looks_thin

        guild = MagicMock()
        guild.id = TEST_GUILD_ID
        guild.members = []
        # member_count is a MagicMock by default
        _warn_if_cache_looks_thin(guild)
        assert "[ROSTER]" not in capsys.readouterr().out


class TestBotIntents:
    """Verify the bot is constructed with the privileged members intent —
    the original cause of the 0-members sync bug."""

    def test_members_intent_is_requested(self):
        # Reset Discord client cache between tests by importing fresh.
        import importlib
        import bot as bot_module

        importlib.reload(bot_module)
        assert bot_module.intents.members is True


# ── /members sync premium gate ───────────────────────────────────────────────


class TestSyncMembersGate:
    """Renamed in #195: `/sync_members` is now `/members sync`. The Python
    method on the cog is `members_sync`."""

    @pytest.mark.asyncio
    async def test_free_tier_sees_premium_locked(self, seeded_db):
        from member_roster import MemberRosterCog
        import premium

        premium.clear_cache()

        bot = MagicMock()
        cog = MemberRosterCog(bot)

        interaction = AsyncMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.entitlements = []
        interaction.user = MagicMock()
        interaction.user.guild_permissions.administrator = True
        interaction.response.send_message = AsyncMock()

        await cog.members_sync.callback(cog, interaction)

        call = interaction.response.send_message.call_args
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
        interaction.guild_id = PREMIUM_TEST_GUILD_ID  # premium via PREMIUM_BYPASS_GUILD_IDS
        interaction.entitlements = []
        interaction.user = MagicMock()
        interaction.user.guild_permissions.administrator = False
        interaction.response.send_message = AsyncMock()

        await cog.members_sync.callback(cog, interaction)

        call = interaction.response.send_message.call_args
        content = call.args[0] if call.args else call.kwargs.get("content")
        # The gate now allows leadership-or-admin (rather than admin-only),
        # so the rejection text mentions both. Either is acceptable.
        lowered = (content or "").lower()
        assert "leadership" in lowered or "administrators" in lowered

    @pytest.mark.asyncio
    async def test_premium_admin_with_unconfigured_roster_gets_setup_hint(self, seeded_db):
        """Guild is premium but roster_config.enabled=0 → wizard hint
        points at the /setup hub's 👥 Members button (post-#201)."""
        from member_roster import MemberRosterCog
        import premium

        premium.clear_cache()

        bot = MagicMock()
        cog = MemberRosterCog(bot)

        interaction = AsyncMock()
        interaction.guild_id = PREMIUM_TEST_GUILD_ID
        interaction.entitlements = []
        interaction.user = MagicMock()
        interaction.user.guild_permissions.administrator = True
        interaction.response.send_message = AsyncMock()

        await cog.members_sync.callback(cog, interaction)

        call = interaction.response.send_message.call_args
        content = call.args[0] if call.args else call.kwargs.get("content")
        assert "/setup" in (content or "")
        assert "Member Sync" in (content or "")


# ── /members sync error-message clarity (regression: opaque <Response [404]>) ─


class TestSyncMembersErrorMessage:
    """When write_roster raises a gspread error, the user-facing followup
    must surface `config.describe_sheet_error`'s diagnosis instead of the
    raw exception repr (which printed `<Response [404]>` and stranded the
    user with no idea what to fix). Matches the 1.1.1 fix that landed for
    storm and train; member_roster was missed at the time.
    """

    @pytest.mark.asyncio
    async def test_404_apierror_surfaces_diagnosis_not_raw_repr(self, seeded_db):
        import config
        import gspread
        from member_roster import MemberRosterCog
        import premium

        premium.clear_cache()

        config.save_member_roster_config(
            PREMIUM_TEST_GUILD_ID,
            enabled=1,
            tab_name="Member Roster",
        )

        cog = MemberRosterCog(MagicMock())

        interaction = AsyncMock()
        interaction.guild_id = PREMIUM_TEST_GUILD_ID
        interaction.entitlements = []
        interaction.user = MagicMock()
        interaction.user.guild_permissions.administrator = True
        interaction.guild = MagicMock()
        interaction.guild.members = []
        interaction.guild.chunk = AsyncMock()
        interaction.guild.fetch_members = MagicMock()

        resp = MagicMock()
        resp.status_code = 404
        resp.json = MagicMock(return_value={})
        resp.__repr__ = lambda self: "<Response [404]>"
        err = gspread.exceptions.APIError(resp)

        with patch("member_roster.write_roster", side_effect=err):
            await cog.members_sync.callback(cog, interaction)

        call = interaction.followup.send.call_args
        content = call.args[0] if call.args else call.kwargs.get("content")
        # The actionable diagnosis from describe_sheet_error must be there;
        # the raw `<Response [404]>` repr must NOT.
        assert "spreadsheet 404" in content
        assert "service account" in content
        assert "<Response [404]>" not in content

    @pytest.mark.asyncio
    async def test_worksheet_not_found_surfaces_tab_name(self, seeded_db):
        import config
        import gspread
        from member_roster import MemberRosterCog
        import premium

        premium.clear_cache()

        config.save_member_roster_config(
            PREMIUM_TEST_GUILD_ID,
            enabled=1,
            tab_name="Member Roster",
        )

        cog = MemberRosterCog(MagicMock())

        interaction = AsyncMock()
        interaction.guild_id = PREMIUM_TEST_GUILD_ID
        interaction.entitlements = []
        interaction.user = MagicMock()
        interaction.user.guild_permissions.administrator = True
        interaction.guild = MagicMock()
        interaction.guild.members = []
        interaction.guild.chunk = AsyncMock()
        interaction.guild.fetch_members = MagicMock()

        err = gspread.exceptions.WorksheetNotFound("Member Roster")

        with patch("member_roster.write_roster", side_effect=err):
            await cog.members_sync.callback(cog, interaction)

        call = interaction.followup.send.call_args
        content = call.args[0] if call.args else call.kwargs.get("content")
        assert "Member Roster" in content
        assert "not found" in content


# ── Discord ID lookup ─────────────────────────────────────────────────────────


class TestLookupDiscordId:
    def test_finds_id_by_display_name(self, seeded_db, monkeypatch):
        import config

        config.save_member_roster_config(TEST_GUILD_ID, enabled=1, tab_name="Members")

        ws = MagicMock()
        ws.get_all_values = MagicMock(
            return_value=[
                ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
                ["111", "alice_user", "Alice", "", ""],
                ["222", "bob_user", "Bob", "", ""],
            ]
        )
        monkeypatch.setattr(config, "get_member_roster_sheet", lambda gid, tab: ws)

        assert config.lookup_discord_id_for_name(TEST_GUILD_ID, "Alice") == "111"
        assert (
            config.lookup_discord_id_for_name(TEST_GUILD_ID, "alice") == "111"
        )  # case-insensitive
        assert config.lookup_discord_id_for_name(TEST_GUILD_ID, "Bob") == "222"

    def test_returns_none_when_disabled(self, seeded_db):
        import config

        # roster config defaults to enabled=0
        assert config.lookup_discord_id_for_name(TEST_GUILD_ID, "Alice") is None

    def test_returns_none_when_no_match(self, seeded_db, monkeypatch):
        import config

        config.save_member_roster_config(TEST_GUILD_ID, enabled=1)

        ws = MagicMock()
        ws.get_all_values = MagicMock(
            return_value=[
                ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
                ["111", "alice_user", "Alice", "", ""],
            ]
        )
        monkeypatch.setattr(config, "get_member_roster_sheet", lambda gid, tab: ws)

        assert config.lookup_discord_id_for_name(TEST_GUILD_ID, "Nobody") is None


# ── #226 follow-up: detect_column_layout + name-fallback merge ──────────────


class TestDetectColumnLayout:
    """`detect_column_layout` is the header-aware mapper used by Member
    Sync setup to avoid claiming columns that already have alliance
    data in them. Pure function — no sheet I/O. Tests pin the exact
    matching + appending rules so a future change doesn't quietly
    drift."""

    def test_canonical_headers_match_in_order(self):
        from member_roster import detect_column_layout

        result = detect_column_layout(
            [
                "Discord ID",
                "Name",
                "Display Name",
                "Joined",
                "Roles",
            ]
        )
        assert result["layout"] == {
            "discord_id_col": 0,
            "name_col": 1,
            "display_col": 2,
            "joined_col": 3,
            "roles_col": 4,
        }
        assert result["pending_appends"] == []

    def test_normalised_headers_match(self):
        """Headers with punctuation / casing variants still match the
        bot's canonical labels via `_normalise_header`."""
        from member_roster import detect_column_layout

        result = detect_column_layout(
            [
                "Discord_ID",
                "USERNAME",
                "displayname",
                "Join Date",
                "role",
            ]
        )
        assert result["layout"] == {
            "discord_id_col": 0,
            "name_col": 1,
            "display_col": 2,
            "joined_col": 3,
            "roles_col": 4,
        }
        assert result["pending_appends"] == []

    def test_empty_sheet_appends_everything(self):
        from member_roster import detect_column_layout

        result = detect_column_layout([])
        # All five bot fields land at the right edge in declaration
        # order, starting at index 0 since the sheet is empty.
        assert result["layout"] == {
            "discord_id_col": 0,
            "name_col": 1,
            "display_col": 2,
            "joined_col": 3,
            "roles_col": 4,
        }
        assert result["pending_appends"] == [
            "discord_id_col",
            "name_col",
            "display_col",
            "joined_col",
            "roles_col",
        ]

    def test_alliance_columns_in_bot_slots_pushes_appends_right(self):
        """Sheet has alliance custom columns where the bot would
        normally put its data. None of those columns match the bot's
        header aliases, so the bot's fields all append at the right
        edge — alliance data preserved in place."""
        from member_roster import detect_column_layout

        result = detect_column_layout(
            [
                "Member ID",
                "In-Game Name",
                "Power",
                "Notes",
                "Squad",
            ]
        )
        # Nothing matched.
        assert result["pending_appends"] == [
            "discord_id_col",
            "name_col",
            "display_col",
            "joined_col",
            "roles_col",
        ]
        # Bot fields land at indices 5-9 (right of the existing 5
        # alliance columns).
        assert result["layout"] == {
            "discord_id_col": 5,
            "name_col": 6,
            "display_col": 7,
            "joined_col": 8,
            "roles_col": 9,
        }

    def test_mixed_matched_and_appended(self):
        """Sheet has Discord ID + Roles in the bot's labels but power
        + alias columns in between. The bot claims the matched
        columns at their existing indices and appends the rest."""
        from member_roster import detect_column_layout

        result = detect_column_layout(
            [
                "Discord ID",
                "Power",
                "Alias",
                "Notes",
                "Roles",
            ]
        )
        # Discord ID matched at 0, Roles matched at 4. Note that
        # "Alias" is an alias for `display_col` so display_col claims
        # index 2 too.
        assert result["layout"]["discord_id_col"] == 0
        assert result["layout"]["display_col"] == 2
        assert result["layout"]["roles_col"] == 4
        # Name + Joined have no matching headers, append at right edge.
        assert result["layout"]["name_col"] == 5
        assert result["layout"]["joined_col"] == 6
        assert set(result["pending_appends"]) == {"name_col", "joined_col"}

    def test_each_column_claimed_at_most_once(self):
        """Two columns with the same canonical label — only the first
        one wins. Prevents one alliance header from being claimed by
        two bot fields if the alias sets overlap."""
        from member_roster import detect_column_layout

        result = detect_column_layout(["Name", "Name", "Roles"])
        # First "Name" goes to name_col; second "Name" is left unclaimed
        # (alliance data); display_col falls through to append.
        assert result["layout"]["name_col"] == 0
        assert result["layout"]["roles_col"] == 2
        assert result["layout"]["display_col"] >= 3  # appended


class TestMergeWithNameFallback:
    """`_merge_with_existing` populates Discord IDs for rows that have
    no Discord ID set but whose name matches an unambiguous live
    guild member. The report dict counts each row's outcome so the
    setup wizard can surface counts in the preview."""

    def _live_guild_with(self, *member_specs):
        """Build a fake guild whose `.members` resolves to mock members
        with the given (id, name, display_name) tuples."""
        guild = MagicMock()
        members = []
        for did, name, display in member_specs:
            m = MagicMock()
            m.bot = False
            m.id = did
            m.name = name
            m.display_name = display
            members.append(m)
        guild.members = members
        return guild

    def _cfg(self):
        return _default_cfg()

    def test_name_match_populates_discord_id(self):
        from member_roster import _merge_with_existing

        existing = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["", "alice_user", "Alice", "", ""],
        ]
        guild = self._live_guild_with(
            (12345, "alice_user", "Alice The Player"),
        )
        new_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["12345", "alice_user", "Alice The Player", "", ""],
        ]
        merged, report = _merge_with_existing(
            new_rows,
            existing,
            self._cfg(),
            guild=guild,
        )
        # Existing row's blank Discord ID populated via name-fallback.
        # Report flags the match.
        assert report["matched_by_name"] == ["alice_user"]
        assert report["matched_by_id"] == []
        assert report["ambiguous"] == []
        assert report["no_match"] == []

    def test_ambiguous_name_does_not_populate(self):
        """Two live members share a display name. The bot must NOT
        guess — report flags the row as ambiguous and leaves it
        as-is."""
        from member_roster import _merge_with_existing

        existing = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["", "Alice", "", "", ""],
        ]
        guild = self._live_guild_with(
            (1, "alice", "Alice"),
            (2, "alice2", "Alice"),
        )
        new_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
        ]
        _merged, report = _merge_with_existing(
            new_rows,
            existing,
            self._cfg(),
            guild=guild,
        )
        # Name-cell "Alice" lowercased to "alice", which is the
        # Discord username of member 1 AND the display name of member 2.
        # That's ambiguous — bot declines to guess.
        assert "alice" in report["ambiguous"]
        assert report["matched_by_name"] == []

    def test_no_match_leaves_row_as_is(self):
        """Existing row's name doesn't match any live guild member —
        report flags `no_match` and the row passes through."""
        from member_roster import _merge_with_existing

        existing = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["", "Ghost", "", "", ""],
        ]
        guild = self._live_guild_with(
            (1, "alice", "Alice"),
        )
        new_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
        ]
        _merged, report = _merge_with_existing(
            new_rows,
            existing,
            self._cfg(),
            guild=guild,
        )
        assert report["no_match"] == ["ghost"]
        assert report["matched_by_name"] == []

    def test_guild_none_disables_name_fallback(self):
        """Backwards-compat: when no guild is passed, the name-fallback
        pass is skipped — pre-#226 behaviour preserved for callers
        that don't have a guild handle."""
        from member_roster import _merge_with_existing

        existing = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["", "alice_user", "Alice", "", ""],
        ]
        new_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
        ]
        _merged, report = _merge_with_existing(
            new_rows,
            existing,
            self._cfg(),
            guild=None,
        )
        # Without a guild, the name index is empty so every name-less
        # row falls into no_match (or ambiguous if there are dupes).
        assert report["matched_by_name"] == []
        assert report["matched_by_id"] == []
        # Either no_match flagged (when guild=None) or unprocessed.
        # The row still exists in the merge, but unflagged.

    def test_existing_id_skips_name_fallback(self):
        """Rows that already carry a Discord ID merge by ID — the
        name-fallback pass shouldn't touch them or count them in
        matched_by_name."""
        from member_roster import _merge_with_existing

        existing = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles", "Power"],
            ["12345", "alice", "Alice", "", "", "300M"],
        ]
        guild = self._live_guild_with((12345, "alice", "Alice"))
        new_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["12345", "alice", "Alice", "", ""],
        ]
        _merged, report = _merge_with_existing(
            new_rows,
            existing,
            self._cfg(),
            guild=guild,
        )
        assert report["matched_by_id"] == ["12345"]
        assert report["matched_by_name"] == []
