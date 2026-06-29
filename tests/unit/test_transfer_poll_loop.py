"""
Tests for `TransferCog._poll_guild` / `TransferCog.poll` — the per-minute
Transfer Management watcher loop (#16).

`transfer.py`'s pure logic (compute_poll_diff, filters, identity hashing) is
covered exhaustively in test_transfer_core.py. This file pins the *loop body*
that wires it to Discord and the DB, which had no coverage:

  * Due-gating — a guild still inside its poll interval is skipped entirely
    (no sheet read, no post, no state write).
  * Premium re-check at poll time — a lapsed subscriber's watcher goes quiet
    without its config row being touched (the row is kept, not deleted).
  * Happy path — a new applicant posts one notice, captures view.message for
    timeout cleanup, and persists the new last-seen state + poll timestamp.
  * A status change on a previously-seen row posts a status-change notice.
  * A sheet read failure advances the poll clock (so a broken sheet backs off
    to the configured interval instead of being hammered every minute) but
    keeps the seen-state intact and posts nothing.
  * Each clean tick of the loop stamps the `transfer_poll` heartbeat (#227
    outage-catchup observability).
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import transfer  # noqa: E402
import transfer_cog  # noqa: E402

GUILD_ID = 4242
CHAN_ID = 9001
NOW = datetime(2026, 6, 24, 12, 0, tzinfo=timezone.utc)

HEADER = ["Name", "Power", "Confirmed"]
COLUMN_MAP = {"name": "Name", "identity_extra": [], "status": ["Confirmed"], "display": ["Power"]}
HIDX = transfer.header_index(HEADER)


# ── Fixtures and helpers ─────────────────────────────────────────────────────


def _make_cog(channel=None):
    """A TransferCog without `__init__` (which would start the live loop). The
    mocked bot resolves the notification channel via `get_channel`."""
    cog = transfer_cog.TransferCog.__new__(transfer_cog.TransferCog)
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=channel)
    cog.bot = bot
    return cog


def _cfg(**over):
    cfg = {
        "guild_id": GUILD_ID,
        "last_polled_at": "",  # never polled → due
        "poll_frequency_minutes": 30,
        "alliance_sheet_id": "SHEET",
        "alliance_sheet_tab": "Applicants",
        "alliance_column_map_json": json.dumps(COLUMN_MAP),
        "notification_filter_json": "",
        "last_seen_state_json": "{}",
        "notification_channel_id": CHAN_ID,
        "notification_style": "each",
        "notify_on_delete": 0,
        "writeback_enabled": 0,
    }
    cfg.update(over)
    return cfg


def _channel():
    chan = AsyncMock()
    chan.send = AsyncMock(return_value=MagicMock(name="sent_msg"))
    return chan


@contextmanager
def _env(*, read_return=(HEADER, []), read_exc=None, is_premium=True):
    """Patch the loop's external edges: the Sheets read, the source-copy pull
    (isolated to 0 here — exercised in test_transfer_core), the poll-time
    premium check, and the two config writers (spied, not hitting a DB)."""
    read = MagicMock()
    if read_exc is not None:
        read.side_effect = read_exc
    else:
        read.return_value = read_return
    fields_spy = MagicMock()
    field_spy = MagicMock()
    with (
        patch("transfer_sheets.read_sheet", read),
        patch("transfer_cog.copy_sources", AsyncMock(return_value=0)),
        patch("premium.is_premium", AsyncMock(return_value=is_premium)),
        patch("config.update_transfer_config_fields", fields_spy),
        patch("config.update_transfer_config_field", field_spy),
    ):
        yield {"read": read, "fields": fields_spy, "field": field_spy}


# ── Due-gating ───────────────────────────────────────────────────────────────


class TestPollDueGating:
    @pytest.mark.asyncio
    async def test_not_due_skips_everything(self):
        """Inside the interval (polled 5 min ago, freq 30) → no read, no post,
        no state write. Most ticks of the per-minute loop hit this branch."""
        chan = _channel()
        cog = _make_cog(chan)
        cfg = _cfg(last_polled_at=(NOW - timedelta(minutes=5)).isoformat())

        with _env() as env:
            await cog._poll_guild(cfg, NOW)

        env["read"].assert_not_called()
        chan.send.assert_not_called()
        env["fields"].assert_not_called()
        env["field"].assert_not_called()


# ── Premium re-check ─────────────────────────────────────────────────────────


class TestPollPremiumGate:
    @pytest.mark.asyncio
    async def test_lapsed_subscriber_goes_quiet_without_touching_config(self):
        chan = _channel()
        cog = _make_cog(chan)
        cfg = _cfg()  # due (never polled)

        with _env(is_premium=False) as env:
            await cog._poll_guild(cfg, NOW)

        env["read"].assert_not_called()  # bails before the sheet read
        chan.send.assert_not_called()
        env["fields"].assert_not_called()
        env["field"].assert_not_called()


# ── Happy path: new applicant ────────────────────────────────────────────────


class TestPollNewApplicant:
    @pytest.mark.asyncio
    async def test_new_applicant_posts_notice_and_persists_state(self):
        chan = _channel()
        cog = _make_cog(chan)
        rows = [["Bad Pew", "199M", ""]]

        with _env(read_return=(HEADER, rows)) as env:
            await cog._poll_guild(_cfg(), NOW)

        chan.send.assert_awaited_once()
        embed = chan.send.await_args.kwargs["embed"]
        assert "Bad Pew" in embed.title
        assert embed.title.startswith("📥")

        # The notice's view.message must be captured for on_timeout cleanup.
        view = chan.send.await_args.kwargs["view"]
        assert view.message is chan.send.return_value

        # Seen-state + poll clock persisted in one write.
        env["fields"].assert_called_once()
        kwargs = env["fields"].call_args.kwargs
        assert kwargs["last_polled_at"] == NOW.isoformat()
        assert "Bad Pew" in kwargs["last_seen_state_json"]

    @pytest.mark.asyncio
    async def test_first_seen_row_is_bookmarked_even_when_filtered_out(self):
        """A filter that excludes the row still bookmarks it (no re-fire if the
        filter later loosens) and posts nothing."""
        chan = _channel()
        cog = _make_cog(chan)
        rows = [["Low Power", "10M", ""]]  # "10M" coerces to 10,000,000 — below the gate
        filt = json.dumps({"and": [{"column": "Power", "op": ">=", "value": 100_000_000}]})

        with _env(read_return=(HEADER, rows)) as env:
            await cog._poll_guild(_cfg(notification_filter_json=filt), NOW)

        chan.send.assert_not_called()
        env["fields"].assert_called_once()
        assert "Low Power" in env["fields"].call_args.kwargs["last_seen_state_json"]


# ── Status change on a previously-seen row ───────────────────────────────────


class TestPollStatusChange:
    @pytest.mark.asyncio
    async def test_status_change_posts_change_notice(self):
        chan = _channel()
        cog = _make_cog(chan)
        rows = [["Bad Pew", "199M", "TRUE"]]
        h = transfer.row_identity(rows[0], HIDX, COLUMN_MAP)
        prior = {h: {"name": "Bad Pew", "status": {"Confirmed": ""}}}

        with _env(read_return=(HEADER, rows)):
            await cog._poll_guild(_cfg(last_seen_state_json=json.dumps(prior)), NOW)

        chan.send.assert_awaited_once()
        embed = chan.send.await_args.kwargs["embed"]
        assert "status changed" in embed.title.lower()
        assert "Bad Pew" in embed.title


# ── Sheet read failure ───────────────────────────────────────────────────────


class TestPollSheetReadFailure:
    @pytest.mark.asyncio
    async def test_read_failure_advances_clock_keeps_state_no_post(self):
        chan = _channel()
        cog = _make_cog(chan)

        with _env(read_exc=RuntimeError("gspread boom")) as env:
            await cog._poll_guild(_cfg(), NOW)

        chan.send.assert_not_called()
        # Clock advanced via the single-field writer; seen-state untouched.
        env["field"].assert_called_once_with(GUILD_ID, "last_polled_at", NOW.isoformat())
        env["fields"].assert_not_called()


# ── Heartbeat (#227 outage-catchup) ──────────────────────────────────────────


class TestPollHeartbeat:
    @pytest.mark.asyncio
    async def test_loop_tick_stamps_transfer_poll_heartbeat(self):
        cog = _make_cog()
        with (
            patch("config.get_transfer_enabled_guilds", return_value=[]),
            patch("config.stamp_loop_heartbeat") as stamp,
        ):
            await type(cog).poll.coro(cog)
        stamp.assert_called_once_with("transfer_poll")


# ── copy_sources blank-cell enrichment (#9) ──────────────────────────────────


class TestCopySourcesEnrich:
    def _cfg(self, **over):
        cfg = {
            "guild_id": GUILD_ID,
            "alliance_sheet_id": "A",
            "alliance_sheet_tab": "T",
            "alliance_column_map_json": json.dumps({"name": "Name"}),
            "copied_state_json": "[]",
            "source_enrich_blanks": 1,
            "server_wide_enabled": 1,
            "server_wide_sheet_id": "S",
            "server_wide_sheet_tab": "ST",
            "server_wide_column_map_json": json.dumps({"name": "Name"}),
            "server_wide_filter_json": "",
            "alliance_form_enabled": 0,
        }
        cfg.update(over)
        return cfg

    @pytest.mark.asyncio
    async def test_existing_person_enriched_not_duplicated(self):
        def fake_read(sheet_id, tab):
            if sheet_id == "S":  # source
                return (["Name", "Power"], [["Bad Pew", "199M"]])
            return (["Name", "Power"], [["Bad Pew", ""]])  # alliance (blank Power)

        append, update = MagicMock(), MagicMock()
        with (
            patch("transfer_sheets.read_sheet", MagicMock(side_effect=fake_read)),
            patch("transfer_sheets.append_rows", append),
            patch("transfer_sheets.update_cells", update),
            patch("config.update_transfer_config_field", MagicMock()),
        ):
            report = await transfer_cog.copy_sources(self._cfg(), ["Name", "Power"])

        assert report["copied"] == 0  # already on the list → not appended
        append.assert_not_called()
        update.assert_called_once()
        assert update.call_args.args[2] == [(2, 1, "199M")]  # blank Power filled

    @pytest.mark.asyncio
    async def test_new_person_appended_and_existing_enriched(self):
        def fake_read(sheet_id, tab):
            if sheet_id == "S":
                return (["Name", "Power"], [["Bad Pew", "199M"], ["New Guy", "50M"]])
            return (["Name", "Power"], [["Bad Pew", ""]])

        append, update = MagicMock(), MagicMock()
        with (
            patch("transfer_sheets.read_sheet", MagicMock(side_effect=fake_read)),
            patch("transfer_sheets.append_rows", append),
            patch("transfer_sheets.update_cells", update),
            patch("config.update_transfer_config_field", MagicMock()),
        ):
            report = await transfer_cog.copy_sources(self._cfg(), ["Name", "Power"])

        assert report["copied"] == 1  # only New Guy is new
        assert report["sources"][0]["read"] == 2  # both source rows have a name
        assert append.call_args.args[2] == [["New Guy", "50M"]]
        assert update.call_args.args[2] == [(2, 1, "199M")]  # Bad Pew enriched

    @pytest.mark.asyncio
    async def test_enrich_off_no_blank_fill_but_still_dedups_against_sheet(self):
        # Enrich off: the alliance sheet is still read (to dedup the pull against
        # who's already on it), but no blank-cell writes happen.
        def fake_read(sheet_id, tab):
            if sheet_id == "S":  # source
                return (["Name", "Power"], [["Bad Pew", "199M"], ["New Guy", "50M"]])
            return (["Name", "Power"], [["Bad Pew", ""]])  # alliance: Bad Pew already on it

        append, update = MagicMock(), MagicMock()
        with (
            patch("transfer_sheets.read_sheet", MagicMock(side_effect=fake_read)),
            patch("transfer_sheets.append_rows", append),
            patch("transfer_sheets.update_cells", update),
            patch("config.update_transfer_config_field", MagicMock()),
        ):
            report = await transfer_cog.copy_sources(
                self._cfg(source_enrich_blanks=0), ["Name", "Power"]
            )

        update.assert_not_called()  # enrich off → no blank-fill writes
        assert report["copied"] == 1  # New Guy appended; Bad Pew deduped (already on sheet)
        assert append.call_args.args[2] == [["New Guy", "50M"]]
        assert report["sources"][0]["skipped_on_sheet"] == 1
