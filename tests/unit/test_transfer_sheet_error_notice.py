"""
Tests for the stuck-watcher notice (#413) — telling an alliance their transfer
watcher has stopped because of a sheet problem they own.

The production incident: an intake-form tab was renamed without updating the
bot's setup, so the poll loop raised `WorksheetNotFound` every 30 minutes for
9 days (445 Sentry events), pulled nothing, and told nobody.

Covered here:

  * `sheet_error_signature` / `should_notify_sheet_error` — the pure dedup
    logic: one post per problem, a daily re-nudge while it stays unfixed, and
    an immediate post when the problem *changes*.
  * `sheet_problem_kind` — which gspread failures are the alliance's to fix
    (missing tab, missing sheet, no access) versus transient or a bot bug (429,
    5xx, non-gspread). Only the former alerts.
  * A missing alliance tab posts one leadership-channel notice, records the
    signature, and does NOT capture to Sentry (the #285 / #286 treatment this
    loop never adopted).
  * The same failure on a later poll stays quiet; a *different* failure posts
    again.
  * A missing-tab notice lists the tabs that do exist, so a rename is obvious.
  * A broken optional source (server-wide / intake form) alerts even though
    the alliance sheet itself reads fine.
  * A clean poll after a recorded problem posts a recovery line and clears all
    three stored fields.
  * A genuine bug (non-gspread) still pages Sentry and raises no user notice.
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import gspread
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import transfer  # noqa: E402
import transfer_cog  # noqa: E402

GUILD_ID = 4242
NOTIFY_CHAN_ID = 9001
LEADERSHIP_CHAN_ID = 7001
NOW = datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc)

HEADER = ["Name", "Power", "Confirmed"]
COLUMN_MAP = {"name": "Name", "identity_extra": [], "status": ["Confirmed"], "display": ["Power"]}


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
        "sheet_error_signature": "",
        "sheet_error_detail": "",
        "sheet_error_notified_at": "",
    }
    cfg.update(over)
    return cfg


def _channel():
    chan = AsyncMock()
    chan.send = AsyncMock(return_value=MagicMock(name="sent_msg"))
    return chan


def _make_cog(notify_chan=None, leadership_chan=None):
    """A TransferCog without `__init__` (which would start the live loop), whose
    bot resolves the notification channel and the leadership channel to two
    different mocks so we can assert *where* a notice landed."""
    cog = transfer_cog.TransferCog.__new__(transfer_cog.TransferCog)
    bot = MagicMock()
    mapping = {NOTIFY_CHAN_ID: notify_chan, LEADERSHIP_CHAN_ID: leadership_chan}
    bot.get_channel = MagicMock(side_effect=lambda cid: mapping.get(cid))
    cog.bot = bot
    return cog


@contextmanager
def _env(*, read_exc=None, stored=None, tab_names=None, leadership_channel_id=LEADERSHIP_CHAN_ID):
    """Patch the loop's edges. `stored` is the transfer config row the notice
    path reads back (i.e. what a *previous* poll recorded); `tab_names` is what
    listing the spreadsheet's tabs returns."""
    read = MagicMock()
    if read_exc is not None:
        read.side_effect = read_exc
    else:
        read.return_value = (HEADER, [])

    core_cfg = MagicMock()
    core_cfg.leadership_channel_id = leadership_channel_id

    fields_spy = MagicMock()
    field_spy = MagicMock()
    capture_spy = MagicMock()
    with (
        patch("transfer_sheets.read_sheet", read),
        patch(
            "transfer_sheets.list_tab_names",
            MagicMock(return_value=list(tab_names or [])),
        ),
        patch(
            "transfer_cog.copy_sources",
            AsyncMock(return_value={"copied": 0, "enriched": 0, "sources": []}),
        ),
        patch("premium.is_premium", AsyncMock(return_value=True)),
        patch("config.get_config", MagicMock(return_value=core_cfg)),
        patch("config.get_transfer_config", MagicMock(return_value=stored or _cfg())),
        patch("config.update_transfer_config_fields", fields_spy),
        patch("config.update_transfer_config_field", field_spy),
        patch("transfer_cog._capture", capture_spy),
    ):
        yield {
            "read": read,
            "fields": fields_spy,
            "field": field_spy,
            "capture": capture_spy,
        }


def _saved(fields_spy) -> dict:
    """Merge every `update_transfer_config_fields` kwarg set into one dict."""
    out = {}
    for call in fields_spy.call_args_list:
        out.update(call.kwargs)
    return out


def _embeds(chan) -> list:
    return [c.kwargs["embed"] for c in chan.send.call_args_list if "embed" in c.kwargs]


def _embed_text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{f.name} {f.value}" for f in embed.fields]
    return "\n".join(parts)


# ── Pure dedup logic ─────────────────────────────────────────────────────────


class TestSignature:
    def test_same_problem_same_signature(self):
        a = transfer.sheet_error_signature("alliance", "missing_tab", "Form Responses 1")
        b = transfer.sheet_error_signature("alliance", "missing_tab", "Form Responses 1")
        assert a == b

    def test_scope_kind_and_tab_all_distinguish(self):
        base = transfer.sheet_error_signature("alliance", "missing_tab", "Applicants")
        assert base != transfer.sheet_error_signature("server_wide", "missing_tab", "Applicants")
        assert base != transfer.sheet_error_signature("alliance", "no_access", "Applicants")
        assert base != transfer.sheet_error_signature("alliance", "missing_tab", "Other")

    def test_scope_round_trips_for_the_recovery_notice(self):
        sig = transfer.sheet_error_signature("alliance_form", "missing_tab", "Form Responses 1")
        assert transfer.sheet_error_scope(sig) == "alliance_form"

    def test_scope_of_no_signature_is_blank(self):
        assert transfer.sheet_error_scope("") == ""


class TestShouldNotify:
    SIG = "alliance|missing_tab|Applicants"

    def test_first_problem_notifies(self):
        assert transfer.should_notify_sheet_error("", "", self.SIG, NOW) is True

    def test_same_problem_inside_quiet_window_stays_silent(self):
        """The whole point: 48 polls a day must not be 48 posts a day."""
        an_hour_ago = (NOW - timedelta(hours=1)).isoformat()
        assert transfer.should_notify_sheet_error(self.SIG, an_hour_ago, self.SIG, NOW) is False

    def test_same_problem_renudges_after_a_day(self):
        stale = (NOW - timedelta(hours=25)).isoformat()
        assert transfer.should_notify_sheet_error(self.SIG, stale, self.SIG, NOW) is True

    def test_different_problem_notifies_immediately(self):
        an_hour_ago = (NOW - timedelta(hours=1)).isoformat()
        assert (
            transfer.should_notify_sheet_error(
                self.SIG, an_hour_ago, "alliance|no_access|Applicants", NOW
            )
            is True
        )

    def test_unparseable_timestamp_notifies_rather_than_wedging_silent(self):
        assert transfer.should_notify_sheet_error(self.SIG, "not-a-date", self.SIG, NOW) is True

    def test_no_problem_never_notifies(self):
        assert transfer.should_notify_sheet_error("", "", "", NOW) is False


# ── Which failures are the alliance's to fix ─────────────────────────────────


class TestSheetProblemKind:
    def test_missing_tab(self):
        exc = gspread.exceptions.WorksheetNotFound("Form Responses 1")
        assert transfer_cog.sheet_problem_kind(exc) == transfer_cog.MISSING_TAB

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
        """429 clears itself. Alerting on it would be a false alarm, even
        though `is_user_config_sheet_error` counts it as alliance-owned."""
        assert transfer_cog.sheet_problem_kind(_api_error(429)) is None

    def test_server_error_is_not_alertable(self):
        assert transfer_cog.sheet_problem_kind(_api_error(500)) is None

    def test_bot_bug_is_not_alertable(self):
        assert transfer_cog.sheet_problem_kind(RuntimeError("boom")) is None


# ── The copy tells you the right thing to go do ──────────────────────────────


class TestFixInstruction:
    def test_no_access_points_at_sharing_not_at_setup(self):
        """Re-picking the sheet doesn't fix a permissions problem, so a 403
        must not send leadership into the setup wizard as step one."""
        text = transfer_cog._fix_instruction(transfer_cog.NO_ACCESS)
        assert "Share" in text
        assert text.index("Share") < text.index("/transfers")

    def test_missing_tab_offers_both_ways_round(self):
        text = transfer_cog._fix_instruction(transfer_cog.MISSING_TAB)
        assert "rename the tab back" in text
        assert "/transfers" in text

    def test_missing_sheet_points_at_re_picking_the_spreadsheet(self):
        assert "re-pick the spreadsheet" in transfer_cog._fix_instruction(
            transfer_cog.MISSING_SHEET
        )

    def test_every_kind_promises_a_follow_up(self):
        """The alliance needs to know the bot will confirm the fix worked,
        otherwise they can't tell a fixed sheet from a still-broken one."""
        for kind in (transfer_cog.MISSING_TAB, transfer_cog.MISSING_SHEET, transfer_cog.NO_ACCESS):
            assert "post here as soon as" in transfer_cog._fix_instruction(kind)

    def test_missing_tab_reason_names_the_tab(self):
        reason = transfer_cog._problem_reason(transfer_cog.MISSING_TAB, "Form Responses 1")
        assert "Form Responses 1" in reason

    def test_missing_tab_reason_survives_an_unknown_tab(self):
        assert transfer_cog._problem_reason(transfer_cog.MISSING_TAB, "")


# ── The /transfers hub surface ───────────────────────────────────────────────


class TestHubProblemField:
    def _embed(self, cfg):
        import discord

        import transfers_hub

        embed = discord.Embed(title="🔁 Transfer Management", color=discord.Color.blurple())
        transfers_hub._add_sheet_problem_field(embed, cfg)
        return embed

    def test_recorded_problem_is_visible_and_recoloured(self):
        """A leadership post can be scrolled past; the hub is where someone
        checks "is this actually running?"."""
        import discord

        embed = self._embed(
            {
                "sheet_error_signature": transfer.sheet_error_signature(
                    "alliance_form", transfer_cog.MISSING_TAB, "Form Responses 1"
                ),
                "sheet_error_detail": "That spreadsheet no longer has a tab named `x`.",
            }
        )
        assert embed.color == discord.Color.red()
        text = _embed_text(embed)
        assert "intake form" in text  # names which sheet
        assert "Setup Transfers" in text  # and the way out

    def test_healthy_config_adds_nothing(self):
        import discord

        embed = self._embed({"sheet_error_signature": "", "sheet_error_detail": ""})
        assert embed.fields == []
        assert embed.color == discord.Color.blurple()

    def test_signature_without_a_detail_still_warns(self):
        """A row written by an older build (or a partial write) must not render
        a blank warning."""
        embed = self._embed(
            {
                "sheet_error_signature": transfer.sheet_error_signature(
                    "alliance", transfer_cog.MISSING_TAB, "Applicants"
                ),
                "sheet_error_detail": "",
            }
        )
        assert len(embed.fields) == 1
        assert "transfer sheet" in _embed_text(embed)


# ── The poll loop: alerting ──────────────────────────────────────────────────


class TestPollAlertsOnMissingTab:
    @pytest.mark.asyncio
    async def test_posts_to_leadership_records_signature_and_skips_sentry(self):
        notify_chan, leadership_chan = _channel(), _channel()
        cog = _make_cog(notify_chan, leadership_chan)
        exc = gspread.exceptions.WorksheetNotFound("Applicants")

        with _env(read_exc=exc) as env:
            await cog._poll_guild(_cfg(), NOW)

        # The alert goes to leadership, not to the recruiting channel.
        assert len(_embeds(leadership_chan)) == 1
        notify_chan.send.assert_not_called()

        embed = _embeds(leadership_chan)[0]
        assert "stuck" in (embed.title or "").lower()
        text = _embed_text(embed)
        assert "renamed" in text  # names the likely cause
        assert "/transfers" in text  # and the fix path

        saved = _saved(env["fields"])
        assert saved["sheet_error_signature"] == transfer.sheet_error_signature(
            "alliance", transfer_cog.MISSING_TAB, "Applicants"
        )
        assert saved["sheet_error_notified_at"] == NOW.isoformat()
        assert saved["sheet_error_detail"]

        # The Sentry flood this issue is about.
        env["capture"].assert_not_called()
        # Clock still advances, so a broken sheet backs off to the interval.
        env["field"].assert_called_once_with(GUILD_ID, "last_polled_at", NOW.isoformat())

    @pytest.mark.asyncio
    async def test_notice_lists_the_tabs_that_do_exist(self):
        """The rename is only obvious if you can see the current names."""
        leadership_chan = _channel()
        cog = _make_cog(_channel(), leadership_chan)
        exc = gspread.exceptions.WorksheetNotFound("Form Responses 1")

        with _env(read_exc=exc, tab_names=["Roster", "Form Responses 2"]):
            await cog._poll_guild(_cfg(alliance_sheet_tab="Form Responses 1"), NOW)

        text = _embed_text(_embeds(leadership_chan)[0])
        assert "Form Responses 2" in text
        assert "Roster" in text

    @pytest.mark.asyncio
    async def test_notice_survives_an_unlistable_spreadsheet(self):
        """A deleted spreadsheet can't be listed either — send the notice
        anyway, minus the hint."""
        leadership_chan = _channel()
        cog = _make_cog(_channel(), leadership_chan)

        with _env(read_exc=gspread.exceptions.WorksheetNotFound("Applicants")) as env:
            with patch(
                "transfer_sheets.list_tab_names", MagicMock(side_effect=RuntimeError("gone"))
            ):
                await cog._poll_guild(_cfg(), NOW)
            assert len(_embeds(leadership_chan)) == 1
            assert _saved(env["fields"])["sheet_error_signature"]

    @pytest.mark.asyncio
    async def test_no_leadership_channel_still_records_for_the_hub(self):
        cog = _make_cog(_channel(), None)

        with _env(
            read_exc=gspread.exceptions.SpreadsheetNotFound(), leadership_channel_id=0
        ) as env:
            await cog._poll_guild(_cfg(), NOW)

        saved = _saved(env["fields"])
        assert saved["sheet_error_signature"] == transfer.sheet_error_signature(
            "alliance", transfer_cog.MISSING_SHEET, "Applicants"
        )

    @pytest.mark.asyncio
    async def test_send_failure_still_records_so_it_does_not_retry_every_poll(self):
        leadership_chan = _channel()
        leadership_chan.send = AsyncMock(
            side_effect=__import__("discord").Forbidden(MagicMock(status=403), "no perms")
        )
        cog = _make_cog(_channel(), leadership_chan)

        with _env(read_exc=gspread.exceptions.WorksheetNotFound("Applicants")) as env:
            await cog._poll_guild(_cfg(), NOW)

        assert _saved(env["fields"])["sheet_error_notified_at"] == NOW.isoformat()


class TestPollAlertDedup:
    @pytest.mark.asyncio
    async def test_same_problem_next_poll_stays_quiet(self):
        """445 events became 1 post. This is the test that pins that."""
        leadership_chan = _channel()
        cog = _make_cog(_channel(), leadership_chan)
        sig = transfer.sheet_error_signature("alliance", transfer_cog.MISSING_TAB, "Applicants")
        already = _cfg(
            sheet_error_signature=sig,
            sheet_error_detail="stale detail",
            sheet_error_notified_at=(NOW - timedelta(hours=1)).isoformat(),
        )

        with _env(
            read_exc=gspread.exceptions.WorksheetNotFound("Applicants"), stored=already
        ) as env:
            await cog._poll_guild(_cfg(), NOW)

        leadership_chan.send.assert_not_called()
        saved = _saved(env["fields"])
        assert saved["sheet_error_signature"] == sig
        assert "sheet_error_notified_at" not in saved  # quiet window preserved

    @pytest.mark.asyncio
    async def test_same_problem_renudges_the_next_day(self):
        leadership_chan = _channel()
        cog = _make_cog(_channel(), leadership_chan)
        sig = transfer.sheet_error_signature("alliance", transfer_cog.MISSING_TAB, "Applicants")
        already = _cfg(
            sheet_error_signature=sig,
            sheet_error_notified_at=(NOW - timedelta(hours=25)).isoformat(),
        )

        with _env(read_exc=gspread.exceptions.WorksheetNotFound("Applicants"), stored=already):
            await cog._poll_guild(_cfg(), NOW)

        assert len(_embeds(leadership_chan)) == 1

    @pytest.mark.asyncio
    async def test_a_different_problem_posts_immediately(self):
        """They fixed the tab name, but the sheet is now unshared. That's news."""
        leadership_chan = _channel()
        cog = _make_cog(_channel(), leadership_chan)
        already = _cfg(
            sheet_error_signature=transfer.sheet_error_signature(
                "alliance", transfer_cog.MISSING_TAB, "Applicants"
            ),
            sheet_error_notified_at=(NOW - timedelta(minutes=5)).isoformat(),
        )

        with _env(read_exc=_api_error(403), stored=already) as env:
            await cog._poll_guild(_cfg(), NOW)

        assert len(_embeds(leadership_chan)) == 1
        assert "Editor" in _embed_text(_embeds(leadership_chan)[0])
        assert _saved(env["fields"])["sheet_error_signature"] == transfer.sheet_error_signature(
            "alliance", transfer_cog.NO_ACCESS, "Applicants"
        )


class TestPollTransientAndBugs:
    @pytest.mark.asyncio
    async def test_rate_limit_does_not_alarm_the_alliance(self):
        leadership_chan = _channel()
        cog = _make_cog(_channel(), leadership_chan)

        with _env(read_exc=_api_error(429)) as env:
            await cog._poll_guild(_cfg(), NOW)

        leadership_chan.send.assert_not_called()
        env["capture"].assert_not_called()  # alliance-owned per #285/#286
        env["fields"].assert_not_called()

    @pytest.mark.asyncio
    async def test_bot_bug_still_pages_sentry_and_posts_nothing(self):
        leadership_chan = _channel()
        cog = _make_cog(_channel(), leadership_chan)

        with _env(read_exc=RuntimeError("real bug")) as env:
            await cog._poll_guild(_cfg(), NOW)

        leadership_chan.send.assert_not_called()
        env["capture"].assert_called_once()


# ── Optional sources ─────────────────────────────────────────────────────────


class TestSourceProblems:
    @pytest.mark.asyncio
    async def test_broken_source_alerts_even_when_the_alliance_sheet_is_fine(self):
        """The reported incident: the alliance sheet read fine, the intake
        form's tab was the renamed one."""
        leadership_chan = _channel()
        cog = _make_cog(_channel(), leadership_chan)
        exc = gspread.exceptions.WorksheetNotFound("Form Responses 1")
        src_report = {
            "copied": 0,
            "enriched": 0,
            "sources": [{"prefix": "alliance_form", "error": "missing tab", "exc": exc}],
        }
        cfg = _cfg(
            alliance_form_enabled=1,
            alliance_form_sheet_id="FORM_SHEET",
            alliance_form_sheet_tab="Form Responses 1",
        )

        with _env(tab_names=["Form Responses 2"]) as env:
            with patch("transfer_cog.copy_sources", AsyncMock(return_value=src_report)):
                await cog._poll_guild(cfg, NOW)

        assert len(_embeds(leadership_chan)) == 1
        text = _embed_text(_embeds(leadership_chan)[0])
        assert "intake form" in text  # named in the alliance's terms
        assert "Form Responses 2" in text
        assert _saved(env["fields"])["sheet_error_signature"] == transfer.sheet_error_signature(
            "alliance_form", transfer_cog.MISSING_TAB, "Form Responses 1"
        )


# ── Recovery ─────────────────────────────────────────────────────────────────


class TestRecovery:
    @pytest.mark.asyncio
    async def test_clean_poll_after_a_problem_says_so_and_clears(self):
        leadership_chan = _channel()
        cog = _make_cog(_channel(), leadership_chan)
        broken = _cfg(
            sheet_error_signature=transfer.sheet_error_signature(
                "alliance", transfer_cog.MISSING_TAB, "Applicants"
            ),
            sheet_error_detail="That spreadsheet no longer has a tab named `Applicants`.",
            sheet_error_notified_at=(NOW - timedelta(hours=2)).isoformat(),
        )

        with _env() as env:
            await cog._poll_guild(broken, NOW)

        recovery = [e for e in _embeds(leadership_chan) if "again" in (e.title or "")]
        assert len(recovery) == 1
        saved = _saved(env["fields"])
        assert saved["sheet_error_signature"] == ""
        assert saved["sheet_error_detail"] == ""
        assert saved["sheet_error_notified_at"] == ""

    @pytest.mark.asyncio
    async def test_recovery_names_the_source_that_came_back(self):
        leadership_chan = _channel()
        cog = _make_cog(_channel(), leadership_chan)
        broken = _cfg(
            sheet_error_signature=transfer.sheet_error_signature(
                "server_wide", transfer_cog.MISSING_TAB, "Transfers"
            ),
            sheet_error_notified_at=(NOW - timedelta(hours=2)).isoformat(),
        )

        with _env():
            await cog._poll_guild(broken, NOW)

        recovery = [e for e in _embeds(leadership_chan) if "again" in (e.title or "")]
        assert "shared sheet" in _embed_text(recovery[0])

    @pytest.mark.asyncio
    async def test_healthy_poll_posts_no_recovery_noise(self):
        leadership_chan = _channel()
        cog = _make_cog(_channel(), leadership_chan)

        with _env() as env:
            await cog._poll_guild(_cfg(), NOW)

        leadership_chan.send.assert_not_called()
        assert "sheet_error_signature" not in _saved(env["fields"])
