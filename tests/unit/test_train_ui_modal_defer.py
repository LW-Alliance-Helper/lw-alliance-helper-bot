"""
Tests for `train_ui.AddEntryModal` / `UpdateEntryModal` deferring their
Discord interaction before the Google Sheets round-trip.

Sentry #76 (`NotFound 10062 Unknown interaction` at `on_submit`) was the
3-second initial-response window expiring during a slow `load_schedule`
/ `save_schedule` gspread call. The fix defers immediately after the
cheap date-parse check, then uses `followup.send` for the success
message. These tests pin that ordering.

The early "could not parse date" branch must remain on `send_message` —
it runs before any I/O, and switching it to followup would require an
extra defer round-trip for no benefit.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# train_ui imports discord which expects a token-shaped env var on import.
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")


GUILD_ID = 12345


def _make_interaction():
    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = 9001
    interaction.response = MagicMock()
    interaction.response.defer = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup = MagicMock()
    interaction.followup.send = AsyncMock()
    return interaction


# ── AddEntryModal ─────────────────────────────────────────────────────────────


class TestAddEntryModalDefer:
    @pytest.mark.asyncio
    async def test_defers_before_sheet_io_and_uses_followup(self):
        from train_ui import AddEntryModal

        interaction = _make_interaction()
        modal = AddEntryModal(MagicMock(), GUILD_ID, blurbs_enabled=False)
        modal.date_input._value = "4/5"
        modal.name_input._value = "Frank"

        call_order: list[str] = []
        interaction.response.defer.side_effect = lambda **kw: call_order.append("defer")

        def fake_load(*_a, **_kw):
            call_order.append("load")
            return {}

        def fake_save(*_a, **_kw):
            call_order.append("save")

        with (
            patch("train_ui.load_schedule", side_effect=fake_load),
            patch("train_ui.save_schedule", side_effect=fake_save),
        ):
            await modal.on_submit(interaction)

        # Defer must come before either sheet call — that's the whole point.
        assert call_order == ["defer", "load", "save"]
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        interaction.response.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_parse_failure_path_still_uses_send_message(self):
        """Unparseable date short-circuits before any I/O — no defer
        needed, and `send_message` lets the error post in a single round-trip."""
        from train_ui import AddEntryModal

        interaction = _make_interaction()
        modal = AddEntryModal(MagicMock(), GUILD_ID, blurbs_enabled=False)
        modal.date_input._value = "not-a-date-xyz"
        modal.name_input._value = "Frank"

        with (
            patch("train_ui.load_schedule") as mock_load,
            patch("train_ui.save_schedule") as mock_save,
        ):
            await modal.on_submit(interaction)

        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_called()
        interaction.followup.send.assert_not_called()
        mock_load.assert_not_called()
        mock_save.assert_not_called()


# ── UpdateEntryModal ──────────────────────────────────────────────────────────


class TestUpdateEntryModalDefer:
    @pytest.mark.asyncio
    async def test_defers_before_sheet_io_and_uses_followup(self):
        from train_ui import UpdateEntryModal

        original_entry = {
            "name": "OldName",
            "theme": "",
            "tone": "",
            "notes": "",
            "prompt_retrieved": False,
        }
        interaction = _make_interaction()
        modal = UpdateEntryModal(
            MagicMock(),
            GUILD_ID,
            blurbs_enabled=False,
            original_date_iso="2026-04-05",
            original_entry=original_entry,
        )
        modal.date_input._value = "4/5"
        modal.name_input._value = "NewName"

        call_order: list[str] = []
        interaction.response.defer.side_effect = lambda **kw: call_order.append("defer")

        def fake_load(*_a, **_kw):
            call_order.append("load")
            return {"2026-04-05": original_entry}

        def fake_save(*_a, **_kw):
            call_order.append("save")

        with (
            patch("train_ui.load_schedule", side_effect=fake_load),
            patch("train_ui.save_schedule", side_effect=fake_save),
        ):
            await modal.on_submit(interaction)

        assert call_order == ["defer", "load", "save"]
        interaction.response.defer.assert_awaited_once_with(ephemeral=True)
        interaction.followup.send.assert_awaited_once()
        interaction.response.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_parse_failure_path_still_uses_send_message(self):
        from train_ui import UpdateEntryModal

        original_entry = {
            "name": "OldName",
            "theme": "",
            "tone": "",
            "notes": "",
            "prompt_retrieved": False,
        }
        interaction = _make_interaction()
        modal = UpdateEntryModal(
            MagicMock(),
            GUILD_ID,
            blurbs_enabled=False,
            original_date_iso="2026-04-05",
            original_entry=original_entry,
        )
        modal.date_input._value = "not-a-date-xyz"
        modal.name_input._value = "NewName"

        with (
            patch("train_ui.load_schedule") as mock_load,
            patch("train_ui.save_schedule") as mock_save,
        ):
            await modal.on_submit(interaction)

        interaction.response.send_message.assert_awaited_once()
        interaction.response.defer.assert_not_called()
        interaction.followup.send.assert_not_called()
        mock_load.assert_not_called()
        mock_save.assert_not_called()
