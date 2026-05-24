"""
Tests for `storm_log._send_storm_reminder` — the Premium-only command
behind /desertstorm remind and /canyonstorm remind. DMs every roster
member with a participation reminder for the upcoming storm.

Audit gap #4 — the entire flow (roster fetch → row iteration → DM
loop → sent/skipped tally) was untested. Only the underlying
`dm.send_dm_to_id` helper had unit coverage. A regression in this
loop fails silently — no DMs go out, leadership sees a misleading
success number, members miss the reminder.

The suite covers:
  * Free-tier guild → premium upsell embed, no roster touched.
  * Premium + roster disabled → "Run /setup_members first" message.
  * Happy path → sheet read, DM each non-empty Discord ID, summary
    counts match what was actually sent.
  * DM failures (send_dm_to_id returns False) → counted as skipped.
  * Empty Discord ID cells → counted as skipped, no DM attempted.
  * Sheet read error → friendly followup, no crash.
  * DS vs CS label text shows up in the DM body.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

from tests.constants import TEST_GUILD_ID, PREMIUM_TEST_GUILD_ID


# ── Premium-env isolation (mirrors test_member_roster.py's pattern) ─────────

@pytest.fixture(autouse=True)
def _isolate_premium_env(monkeypatch):
    """Pin PREMIUM_TEST_GUILD_ID into PREMIUM_BYPASS_GUILD_IDS so the
    premium-keyed tests resolve as premium without hitting Discord
    entitlements. TEST_GUILD_ID stays out of the bypass set so free-
    tier paths still take effect for those tests."""
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


# ── Helpers ─────────────────────────────────────────────────────────────────

def _make_interaction(guild_id: int = PREMIUM_TEST_GUILD_ID):
    interaction                       = MagicMock()
    interaction.guild_id              = guild_id
    interaction.entitlements          = []
    interaction.user                  = MagicMock()
    interaction.user.id               = 9001
    interaction.user.guild_permissions = MagicMock(administrator=True)
    interaction.response              = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.defer        = AsyncMock()
    interaction.followup              = MagicMock()
    interaction.followup.send         = AsyncMock()
    return interaction


def _make_sheet(rows: list[list[str]]):
    ws = MagicMock()
    ws.get_all_values = MagicMock(return_value=rows)
    return ws


def _enabled_roster_cfg(discord_id_col: int = 0, tab_name: str = "Member Roster"):
    return {
        "enabled":        1,
        "tab_name":       tab_name,
        "discord_id_col": discord_id_col,
        "name_col":       1,
        "display_col":    2,
        "joined_col":     3,
        "roles_col":      4,
        "role_filter_id": 0,
        "auto_sync":      0,
        "last_synced_at": "",
    }


# Bypass _guard so tests don't need to set up channel categories / roles,
# and stub config.get_storm_config so tests don't need a seeded SQLite DB
# just to read `dm_reminder_message`. Tests that want a custom DM template
# can still wrap their own `patch("config.get_storm_config", ...)` — the
# inner patch wins under unittest.mock's stacking rules.
@pytest.fixture
def _bypass_guard():
    with patch("storm_log._guard", AsyncMock(return_value=True)), \
         patch("config.get_storm_config", return_value={"dm_reminder_message": ""}):
        yield


# ── Premium gate ─────────────────────────────────────────────────────────────

class TestStormReminderPremiumGate:

    @pytest.mark.asyncio
    @pytest.mark.free_tier_only
    async def test_free_tier_guild_sees_upsell(self, _bypass_guard):
        from storm_log import _send_storm_reminder
        interaction = _make_interaction(guild_id=TEST_GUILD_ID)

        bot = MagicMock()
        await _send_storm_reminder(bot, interaction, "DS")

        # Premium-locked embed surfaces; nothing else gets touched.
        interaction.response.send_message.assert_called_once()
        kwargs = interaction.response.send_message.call_args.kwargs
        embed  = kwargs.get("embed")
        assert embed is not None
        assert "Premium" in embed.title


# ── Roster-disabled branch ───────────────────────────────────────────────────

class TestStormReminderRosterDisabled:

    @pytest.mark.asyncio
    async def test_premium_with_roster_disabled_shows_setup_hint(self, _bypass_guard):
        from storm_log import _send_storm_reminder
        interaction = _make_interaction()

        with patch(
            "config.get_member_roster_config",
            return_value={"enabled": 0, "tab_name": "", "discord_id_col": 0},
        ):
            await _send_storm_reminder(MagicMock(), interaction, "DS")

        msg = interaction.response.send_message.call_args.args[0]
        # Post-#201: the /setup_members slash command folded into the
        # /setup hub's `👥 Member Sync` button. The reminder copy now
        # points there.
        assert "Member Sync" in msg
        assert "/setup" in msg


# ── Happy path ───────────────────────────────────────────────────────────────

class TestStormReminderHappyPath:

    @pytest.mark.asyncio
    async def test_dms_each_member_with_nonempty_discord_id(self, _bypass_guard):
        """Roster has 3 valid IDs and 1 empty cell → 3 DMs attempted,
        1 skipped. send_dm_to_id is mocked to always succeed."""
        from storm_log import _send_storm_reminder
        interaction = _make_interaction()

        sheet_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["111", "alice", "Alice", "", ""],
            ["222", "bob",   "Bob",   "", ""],
            ["",    "ghost", "Ghost", "", ""],   # empty ID — skip
            ["333", "carol", "Carol", "", ""],
        ]

        send_spy = AsyncMock(return_value=True)

        with patch("config.get_member_roster_config", return_value=_enabled_roster_cfg()), \
             patch("config.get_member_roster_sheet", return_value=_make_sheet(sheet_rows)), \
             patch("dm.send_dm_to_id", send_spy):
            await _send_storm_reminder(MagicMock(), interaction, "DS")

        # Three DMs (one per non-empty ID).
        assert send_spy.await_count == 3
        ids_dmed = [call.args[2] for call in send_spy.await_args_list]
        assert ids_dmed == ["111", "222", "333"]

        # Followup tallies.
        followup_msg = interaction.followup.send.call_args.args[0]
        assert "Sent 3" in followup_msg
        assert "1 skipped" in followup_msg

    @pytest.mark.asyncio
    async def test_failed_dm_counts_as_skipped(self, _bypass_guard):
        """When `send_dm_to_id` returns False (DMs disabled, etc.),
        the row counts as skipped, not sent."""
        from storm_log import _send_storm_reminder
        interaction = _make_interaction()

        sheet_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["111", "alice", "Alice", "", ""],
            ["222", "bob",   "Bob",   "", ""],
        ]

        # Bob's DM fails.
        send_spy = AsyncMock(side_effect=lambda bot, gid, did, **kw: did != "222")

        with patch("config.get_member_roster_config", return_value=_enabled_roster_cfg()), \
             patch("config.get_member_roster_sheet", return_value=_make_sheet(sheet_rows)), \
             patch("dm.send_dm_to_id", send_spy):
            await _send_storm_reminder(MagicMock(), interaction, "DS")

        followup_msg = interaction.followup.send.call_args.args[0]
        assert "Sent 1" in followup_msg
        assert "1 skipped" in followup_msg

    @pytest.mark.asyncio
    async def test_singular_message_when_one_dm_sent(self, _bypass_guard):
        """`Sent 1 reminder DM.` (no `s`). Tiny detail but tested
        because the format string conditionalizes the plural."""
        from storm_log import _send_storm_reminder
        interaction = _make_interaction()

        sheet_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["111", "alice", "Alice", "", ""],
        ]

        with patch("config.get_member_roster_config", return_value=_enabled_roster_cfg()), \
             patch("config.get_member_roster_sheet", return_value=_make_sheet(sheet_rows)), \
             patch("dm.send_dm_to_id", AsyncMock(return_value=True)):
            await _send_storm_reminder(MagicMock(), interaction, "DS")

        followup_msg = interaction.followup.send.call_args.args[0]
        # "DM" not "DMs" when sent == 1.
        assert "Sent 1 **Desert Storm** reminder DM." in followup_msg


# ── Event type label ─────────────────────────────────────────────────────────

class TestStormReminderEventTypeLabel:

    @pytest.mark.asyncio
    async def test_ds_label_in_dm_body(self, _bypass_guard):
        from storm_log import _send_storm_reminder
        interaction = _make_interaction()

        sheet_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["111", "alice", "Alice", "", ""],
        ]
        send_spy = AsyncMock(return_value=True)
        with patch("config.get_member_roster_config", return_value=_enabled_roster_cfg()), \
             patch("config.get_member_roster_sheet", return_value=_make_sheet(sheet_rows)), \
             patch("dm.send_dm_to_id", send_spy):
            await _send_storm_reminder(MagicMock(), interaction, "DS")

        body = send_spy.await_args.kwargs["content"]
        assert "Desert Storm" in body
        assert "Canyon Storm" not in body

    @pytest.mark.asyncio
    async def test_cs_label_in_dm_body(self, _bypass_guard):
        from storm_log import _send_storm_reminder
        interaction = _make_interaction()

        sheet_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["111", "alice", "Alice", "", ""],
        ]
        send_spy = AsyncMock(return_value=True)
        with patch("config.get_member_roster_config", return_value=_enabled_roster_cfg()), \
             patch("config.get_member_roster_sheet", return_value=_make_sheet(sheet_rows)), \
             patch("dm.send_dm_to_id", send_spy):
            await _send_storm_reminder(MagicMock(), interaction, "CS")

        body = send_spy.await_args.kwargs["content"]
        assert "Canyon Storm" in body
        assert "Desert Storm" not in body


# ── Custom DM-reminder template ──────────────────────────────────────────────

class TestStormReminderCustomTemplate:
    """When an alliance configures `dm_reminder_message` via
    /setup_desertstorm or /setup_canyonstorm, the reminder uses that
    text instead of the bot's hardcoded default — and `{name}` gets
    substituted with the member's roster name."""

    @pytest.mark.asyncio
    async def test_custom_template_replaces_default(self, _bypass_guard):
        from storm_log import _send_storm_reminder
        interaction = _make_interaction()

        sheet_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["111", "alice", "Alice", "", ""],
        ]
        custom = "Hey {name}, suit up — DS in 30 minutes!"
        send_spy = AsyncMock(return_value=True)
        with patch("config.get_member_roster_config", return_value=_enabled_roster_cfg()), \
             patch("config.get_member_roster_sheet", return_value=_make_sheet(sheet_rows)), \
             patch("config.get_storm_config", return_value={"dm_reminder_message": custom}), \
             patch("dm.send_dm_to_id", send_spy):
            await _send_storm_reminder(MagicMock(), interaction, "DS")

        body = send_spy.await_args.kwargs["content"]
        assert body == "Hey alice, suit up — DS in 30 minutes!"
        # Default copy must NOT leak through.
        assert "Please confirm your participation" not in body

    @pytest.mark.asyncio
    async def test_each_member_gets_their_own_name_substituted(self, _bypass_guard):
        """Multi-row roster — every DM should reflect the row's own name."""
        from storm_log import _send_storm_reminder
        interaction = _make_interaction()

        sheet_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["111", "alice", "Alice", "", ""],
            ["222", "bob",   "Bob",   "", ""],
            ["333", "carol", "Carol", "", ""],
        ]
        send_spy = AsyncMock(return_value=True)
        custom = "Reminder for {name}"
        with patch("config.get_member_roster_config", return_value=_enabled_roster_cfg()), \
             patch("config.get_member_roster_sheet", return_value=_make_sheet(sheet_rows)), \
             patch("config.get_storm_config", return_value={"dm_reminder_message": custom}), \
             patch("dm.send_dm_to_id", send_spy):
            await _send_storm_reminder(MagicMock(), interaction, "DS")

        bodies = [c.kwargs["content"] for c in send_spy.await_args_list]
        assert bodies == [
            "Reminder for alice",
            "Reminder for bob",
            "Reminder for carol",
        ]

    @pytest.mark.asyncio
    async def test_typo_in_template_does_not_crash_reminder(self, _bypass_guard):
        """User puts `{nme}` in their template by accident. The DM
        sends with literal `{nme}` text instead of crashing the loop."""
        from storm_log import _send_storm_reminder
        interaction = _make_interaction()

        sheet_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["111", "alice", "Alice", "", ""],
        ]
        broken = "Hey {nme}, ready?"
        send_spy = AsyncMock(return_value=True)
        with patch("config.get_member_roster_config", return_value=_enabled_roster_cfg()), \
             patch("config.get_member_roster_sheet", return_value=_make_sheet(sheet_rows)), \
             patch("config.get_storm_config", return_value={"dm_reminder_message": broken}), \
             patch("dm.send_dm_to_id", send_spy):
            await _send_storm_reminder(MagicMock(), interaction, "DS")

        body = send_spy.await_args.kwargs["content"]
        assert body == "Hey {nme}, ready?"
        # The DM still went out (no crash, count == 1).
        assert send_spy.await_count == 1


# ── Sheet read failure ───────────────────────────────────────────────────────

class TestStormReminderSheetError:

    @pytest.mark.asyncio
    async def test_followup_explains_when_sheet_read_fails(self, _bypass_guard):
        """get_member_roster_sheet raises (e.g. tab missing). Loop
        bails with a friendly followup, no DMs attempted."""
        from storm_log import _send_storm_reminder
        interaction = _make_interaction()

        send_spy = AsyncMock(return_value=True)
        with patch("config.get_member_roster_config", return_value=_enabled_roster_cfg()), \
             patch("config.get_member_roster_sheet",
                   side_effect=RuntimeError("tab not found")), \
             patch("dm.send_dm_to_id", send_spy):
            await _send_storm_reminder(MagicMock(), interaction, "DS")

        send_spy.assert_not_called()
        followup_msg = interaction.followup.send.call_args.args[0]
        assert "Could not read the roster sheet" in followup_msg


# ── Defensive: short rows ────────────────────────────────────────────────────

class TestStormReminderShortRows:

    @pytest.mark.asyncio
    async def test_row_shorter_than_did_col_is_skipped(self, _bypass_guard):
        """If the configured discord_id_col is past the end of a row
        (sheet got trimmed somehow), skip the row instead of indexing
        out-of-range."""
        from storm_log import _send_storm_reminder
        interaction = _make_interaction()

        sheet_rows = [
            ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
            ["111", "alice", "Alice", "", ""],
            ["onlyonecol"],   # too short — discord_id_col=2 doesn't fit
        ]

        send_spy = AsyncMock(return_value=True)
        with patch("config.get_member_roster_config",
                   return_value=_enabled_roster_cfg(discord_id_col=2)), \
             patch("config.get_member_roster_sheet",
                   return_value=_make_sheet(sheet_rows)), \
             patch("dm.send_dm_to_id", send_spy):
            await _send_storm_reminder(MagicMock(), interaction, "DS")

        # Only the well-formed row gets processed (col 2 = "Alice" — but
        # "Alice" is not a discord_id, just demonstrating no crash).
        # The short row is skipped silently.
        assert send_spy.await_count <= 1  # at most the Alice row
