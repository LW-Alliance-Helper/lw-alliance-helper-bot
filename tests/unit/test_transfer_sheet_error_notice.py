"""
Tests for the transfer watcher's half of the stuck-watcher notice (#413).

The production incident: an intake-form tab was renamed without updating the
bot's setup, so the poll loop raised `WorksheetNotFound` every 30 minutes for
9 days (445 Sentry events), pulled nothing, and told nobody.

The notice *mechanism* moved to config_health in #414 / #379 and is covered by
test_config_health.py: dedup, the daily re-nudge, the digest, the recovery
line, and where a notice lands. What's left here is the part that stayed
transfer-specific, and the wiring between the two:

  * `sheet_problem_kind` — which gspread failures are the alliance's to fix
    (missing tab, missing sheet, no access) versus transient or a bot bug (429,
    5xx, non-gspread). Only the former records anything.
  * The reason copy, which names the tab and lists the tabs that *do* exist so
    a rename is obvious on sight.
  * The tab listing costs a network round-trip, so it happens once per new
    problem rather than on every poll.
  * A poll that can't read the alliance sheet records against the right
    subject, and does NOT capture to Sentry (the #285 / #286 treatment this
    loop never adopted).
  * Every broken optional source records, not just the first — under #413's
    single notice slot a second broken source stayed hidden until the first was
    fixed.
  * A clean read clears the alliance scope; a genuine bug still pages Sentry
    and records nothing.
  * The `/transfers` hub renders whatever config_health is holding.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import gspread
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import config_health  # noqa: E402
import transfer  # noqa: E402
import transfer_cog  # noqa: E402
import transfers_hub  # noqa: E402

GUILD_ID = 4242
NOTIFY_CHAN_ID = 9001
NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

HEADER = ["Name", "Power", "Confirmed"]
COLUMN_MAP = {"name": "Name", "identity_extra": [], "status": ["Confirmed"], "display": ["Power"]}

ALLIANCE_SUBJECT = "transfer.alliance"
FORM_SUBJECT = "transfer.alliance_form"


# ── Helpers ──────────────────────────────────────────────────────────────────


def _api_error(status: int) -> gspread.exceptions.APIError:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {}
    return gspread.exceptions.APIError(resp)


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
        "notification_channel_id": NOTIFY_CHAN_ID,
        "notification_style": "each",
        "notify_on_delete": 0,
        "writeback_enabled": 0,
        "alliance_form_sheet_id": "FORM_SHEET",
        "alliance_form_sheet_tab": "Form Responses 1",
        "server_wide_sheet_id": "SHARED_SHEET",
        "server_wide_sheet_tab": "Shared",
    }
    cfg.update(over)
    return cfg


def _make_cog():
    """A TransferCog without `__init__`, which would start the live loop."""
    cog = transfer_cog.TransferCog.__new__(transfer_cog.TransferCog)
    cog.bot = MagicMock()
    cog.bot.get_channel = MagicMock(return_value=None)
    return cog


@contextmanager
def _env(*, read_exc=None, tab_names=None, list_exc=None, sources=None):
    """Patch the loop's edges. Recording goes to the real (temp) DB, so tests
    assert on stored state rather than on a spy."""
    read = MagicMock()
    if read_exc is not None:
        read.side_effect = read_exc
    else:
        read.return_value = (HEADER, [])

    lister = MagicMock()
    if list_exc is not None:
        lister.side_effect = list_exc
    else:
        lister.return_value = list(tab_names or [])

    capture_spy = MagicMock()
    report = {"copied": 0, "enriched": 0, "sources": list(sources or [])}
    with (
        patch("transfer_sheets.read_sheet", read),
        patch("transfer_sheets.list_tab_names", lister),
        patch("transfer_cog.copy_sources", AsyncMock(return_value=report)),
        patch("premium.is_premium", AsyncMock(return_value=True)),
        patch("config.update_transfer_config_field", MagicMock()),
        patch("config.update_transfer_config_fields", MagicMock()),
        patch("transfer_cog._capture", capture_spy),
    ):
        yield {"read": read, "capture": capture_spy, "lister": lister}


def _stored(subject: str):
    return next((p for p in config_health.problems(GUILD_ID) if p.subject == subject), None)


# ── Classification ───────────────────────────────────────────────────────────


class TestSheetProblemKind:
    def test_missing_tab(self):
        assert (
            transfer_cog.sheet_problem_kind(gspread.exceptions.WorksheetNotFound("x"))
            == transfer_cog.MISSING_TAB
        )

    def test_missing_spreadsheet(self):
        assert (
            transfer_cog.sheet_problem_kind(gspread.exceptions.SpreadsheetNotFound())
            == transfer_cog.MISSING_SHEET
        )

    def test_api_404_is_missing_spreadsheet(self):
        assert transfer_cog.sheet_problem_kind(_api_error(404)) == transfer_cog.MISSING_SHEET

    def test_api_403_is_no_access(self):
        assert transfer_cog.sheet_problem_kind(_api_error(403)) == transfer_cog.NO_ACCESS

    def test_rate_limit_is_not_alertable(self):
        """429 clears itself, so telling the alliance would be a false alarm.
        Deliberately different from is_user_config_sheet_error, which counts it
        as alliance-owned for Sentry purposes."""
        assert transfer_cog.sheet_problem_kind(_api_error(429)) is None

    def test_server_error_is_not_alertable(self):
        assert transfer_cog.sheet_problem_kind(_api_error(500)) is None

    def test_bot_bug_is_not_alertable(self):
        assert transfer_cog.sheet_problem_kind(RuntimeError("boom")) is None


# ── Subject registration ─────────────────────────────────────────────────────


class TestSubjectRegistration:
    def test_every_watched_sheet_has_copy(self):
        """An unregistered subject renders as "part of your setup", which would
        make the notice useless for naming which sheet to go fix."""
        for scope, label in transfer.SHEET_SCOPE_LABELS.items():
            subject = config_health.get_subject(f"transfer.{scope}")
            assert subject.label == label
            assert subject.fix_hub == transfers_hub.TRANSFERS_HUB_CMD
            assert subject.fix_btn == transfers_hub.SETUP_TRANSFERS_BTN


# ── Reason copy ──────────────────────────────────────────────────────────────


class TestProblemReason:
    def test_missing_tab_reason_names_the_tab(self):
        text = transfer_cog._problem_reason(transfer_cog.MISSING_TAB, "Applicants")
        assert "Applicants" in text

    def test_missing_tab_reason_survives_an_unknown_tab(self):
        text = transfer_cog._problem_reason(transfer_cog.MISSING_TAB, "")
        assert "the tab I was told to watch" in text

    def test_tabs_hint_is_included_when_known(self):
        """The fix is obvious the moment you can see the real names."""
        text = transfer_cog._problem_reason(transfer_cog.MISSING_TAB, "Applicants", "`A`, `B`")
        assert "`A`, `B`" in text

    def test_no_access_reason_talks_about_permission(self):
        text = transfer_cog._problem_reason(transfer_cog.NO_ACCESS, "")
        assert "permission" in text


# ── Recording from the poll ──────────────────────────────────────────────────


class TestPollRecordsProblems:
    @pytest.mark.asyncio
    async def test_missing_tab_records_against_the_alliance_subject(self, temp_db):
        cog = _make_cog()
        with _env(read_exc=gspread.exceptions.WorksheetNotFound("Applicants")) as env:
            await cog._poll_guild(_cfg(), NOW)

        problem = _stored(ALLIANCE_SUBJECT)
        assert problem is not None
        assert problem.kind == transfer_cog.MISSING_TAB
        assert "Applicants" in problem.detail
        env["capture"].assert_not_called()

    @pytest.mark.asyncio
    async def test_notice_lists_the_tabs_that_do_exist(self, temp_db):
        cog = _make_cog()
        with _env(
            read_exc=gspread.exceptions.WorksheetNotFound("Applicants"),
            tab_names=["Roster", "Applicants 2026"],
        ):
            await cog._poll_guild(_cfg(), NOW)

        assert "`Applicants 2026`" in _stored(ALLIANCE_SUBJECT).detail

    @pytest.mark.asyncio
    async def test_tabs_are_listed_once_per_problem_not_once_per_poll(self, temp_db):
        """The listing is a network round-trip. #413 paid it only when actually
        posting; the equivalent here is only when the problem is new."""
        cog = _make_cog()
        with _env(
            read_exc=gspread.exceptions.WorksheetNotFound("Applicants"), tab_names=["Roster"]
        ) as env:
            await cog._poll_guild(_cfg(), NOW)
            await cog._poll_guild(_cfg(), NOW)
            await cog._poll_guild(_cfg(), NOW)

        assert env["lister"].call_count == 1

    @pytest.mark.asyncio
    async def test_unlistable_spreadsheet_still_records(self, temp_db):
        cog = _make_cog()
        with _env(
            read_exc=gspread.exceptions.WorksheetNotFound("Applicants"),
            list_exc=RuntimeError("gone"),
        ):
            await cog._poll_guild(_cfg(), NOW)

        assert _stored(ALLIANCE_SUBJECT) is not None

    @pytest.mark.asyncio
    async def test_rate_limit_records_nothing(self, temp_db):
        cog = _make_cog()
        with _env(read_exc=_api_error(429)):
            await cog._poll_guild(_cfg(), NOW)

        assert config_health.problems(GUILD_ID) == []

    @pytest.mark.asyncio
    async def test_bot_bug_pages_sentry_and_records_nothing(self, temp_db):
        cog = _make_cog()
        with _env(read_exc=RuntimeError("boom")) as env:
            await cog._poll_guild(_cfg(), NOW)

        assert config_health.problems(GUILD_ID) == []
        env["capture"].assert_called_once()


class TestSourceProblems:
    @pytest.mark.asyncio
    async def test_broken_source_records_even_when_the_alliance_sheet_is_fine(self, temp_db):
        cog = _make_cog()
        sources = [
            {
                "prefix": "alliance_form",
                "exc": gspread.exceptions.WorksheetNotFound("Form Responses 1"),
            }
        ]
        with _env(sources=sources):
            await cog._poll_guild(_cfg(), NOW)

        assert _stored(FORM_SUBJECT) is not None
        assert _stored(ALLIANCE_SUBJECT) is None

    @pytest.mark.asyncio
    async def test_two_broken_sources_both_record(self, temp_db):
        """Under #413's single notice slot the second stayed hidden until the
        first was fixed."""
        cog = _make_cog()
        sources = [
            {"prefix": "alliance_form", "exc": gspread.exceptions.WorksheetNotFound("Form")},
            {"prefix": "server_wide", "exc": gspread.exceptions.SpreadsheetNotFound()},
        ]
        with _env(sources=sources):
            await cog._poll_guild(_cfg(), NOW)

        subjects = {p.subject for p in config_health.problems(GUILD_ID)}
        assert subjects == {FORM_SUBJECT, "transfer.server_wide"}

    @pytest.mark.asyncio
    async def test_fixing_one_source_does_not_silence_the_other(self, temp_db):
        cog = _make_cog()
        both = [
            {"prefix": "alliance_form", "exc": gspread.exceptions.WorksheetNotFound("Form")},
            {"prefix": "server_wide", "exc": gspread.exceptions.SpreadsheetNotFound()},
        ]
        with _env(sources=both):
            await cog._poll_guild(_cfg(), NOW)
        with _env(sources=both[:1]):
            await cog._poll_guild(_cfg(), NOW)

        subjects = {p.subject for p in config_health.problems(GUILD_ID)}
        assert subjects == {FORM_SUBJECT}


class TestRecovery:
    @pytest.mark.asyncio
    async def test_clean_poll_clears_the_alliance_problem(self, temp_db):
        cog = _make_cog()
        with _env(read_exc=gspread.exceptions.WorksheetNotFound("Applicants")):
            await cog._poll_guild(_cfg(), NOW)
        assert _stored(ALLIANCE_SUBJECT) is not None

        with _env():
            await cog._poll_guild(_cfg(), NOW)
        assert _stored(ALLIANCE_SUBJECT) is None

    @pytest.mark.asyncio
    async def test_healthy_poll_records_nothing(self, temp_db):
        cog = _make_cog()
        with _env():
            await cog._poll_guild(_cfg(), NOW)
        assert config_health.problems(GUILD_ID) == []


# ── Hub surface ──────────────────────────────────────────────────────────────


class TestHubProblemField:
    def _embed(self, guild_id):
        import discord

        embed = discord.Embed(title="t", color=discord.Color.blurple())
        transfers_hub._add_sheet_problem_field(embed, guild_id)
        return embed

    def test_recorded_problem_is_visible_and_recoloured(self, temp_db):
        """A channel post can be missed; the hub is where someone goes to ask
        "is this working?"."""
        import discord

        config_health.record(
            GUILD_ID, ALLIANCE_SUBJECT, transfer_cog.MISSING_TAB, "tab `Applicants` is gone"
        )
        embed = self._embed(GUILD_ID)
        assert embed.color == discord.Color.red()
        text = "\n".join(f"{f.name} {f.value}" for f in embed.fields)
        assert "your transfer sheet" in text
        assert "Applicants" in text
        assert transfers_hub.SETUP_TRANSFERS_BTN in text

    def test_healthy_config_adds_nothing(self, temp_db):
        embed = self._embed(GUILD_ID)
        assert embed.fields == []

    def test_every_broken_sheet_shows_not_just_one(self, temp_db):
        config_health.record(GUILD_ID, ALLIANCE_SUBJECT, transfer_cog.MISSING_TAB, "a")
        config_health.record(GUILD_ID, FORM_SUBJECT, transfer_cog.MISSING_SHEET, "b")
        assert len(self._embed(GUILD_ID).fields) == 2

    def test_a_problem_without_detail_still_warns(self, temp_db):
        config_health.record(GUILD_ID, ALLIANCE_SUBJECT, transfer_cog.NO_ACCESS, "")
        text = "\n".join(f"{f.name} {f.value}" for f in self._embed(GUILD_ID).fields)
        assert "your transfer sheet" in text
        assert "permission" in text


if __name__ == "__main__":
    pytest.main([__file__])
