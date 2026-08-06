"""The #414 sibling sites: train, member roster, and storm DS/CS assignments.

#413 fixed the transfer watcher. The same shape sat in three other features,
each swallowing a sheet failure into a `print` and handing back a fallback
that reads as ordinary data:

  * `train.load_schedule` returns `{}` — "nothing is scheduled", not "I
    couldn't look".
  * `member_roster` auto-sync fires on member-join/leave/role-change with
    nobody watching, and paged Sentry for problems the alliance owns.
  * `storm.load_ds_assignments` / `load_cs_assignments` fall back to defaults
    and return normally — the sharpest of the three. DS defaults are empty, so
    it reads as "nobody assigned yet"; CS defaults are populated, so a renamed
    tab yields a full-looking zone layout that isn't the alliance's.

These pin the recording, the recovery, and the fact that a transient failure
or a bot bug still does not alarm the alliance.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import gspread
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import config_health  # noqa: E402
import member_roster  # noqa: E402
import storm  # noqa: E402
import train  # noqa: E402
from tests.constants import TEST_GUILD_ID  # noqa: E402


def _api_error(status: int) -> gspread.exceptions.APIError:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = {}
    return gspread.exceptions.APIError(resp)


def _problem(subject):
    return next((p for p in config_health.problems(TEST_GUILD_ID) if p.subject == subject), None)


# ── The shared classifier ────────────────────────────────────────────────────


class TestSharedClassifier:
    """Moved out of transfer_cog in #414 once four features needed it."""

    def test_missing_tab(self):
        assert (
            config_health.sheet_problem_kind(gspread.exceptions.WorksheetNotFound("x"))
            == config_health.MISSING_TAB
        )

    def test_rate_limit_is_not_alertable(self):
        """429 clears itself, so telling the alliance would be a false alarm."""
        assert config_health.sheet_problem_kind(_api_error(429)) is None

    def test_bot_bug_is_not_alertable(self):
        assert config_health.sheet_problem_kind(RuntimeError("boom")) is None

    def test_transfer_cog_still_exposes_it(self):
        """Its own call sites and tests keep reading in transfer terms."""
        import transfer_cog

        assert transfer_cog.sheet_problem_kind is config_health.sheet_problem_kind


class TestRecordSheetFailure:
    def test_records_and_reports_true(self, temp_db):
        assert (
            config_health.record_sheet_failure(
                TEST_GUILD_ID,
                "test.subject",
                gspread.exceptions.WorksheetNotFound("Roster"),
                tab="Roster",
            )
            is True
        )
        assert _problem("test.subject") is not None

    def test_transient_records_nothing_and_reports_false(self, temp_db):
        assert (
            config_health.record_sheet_failure(TEST_GUILD_ID, "test.subject", _api_error(429))
            is False
        )
        assert config_health.problems(TEST_GUILD_ID) == []

    def test_missing_tab_detail_names_the_tab(self, temp_db):
        config_health.record_sheet_failure(
            TEST_GUILD_ID,
            "test.subject",
            gspread.exceptions.WorksheetNotFound("Roster"),
            tab="Roster",
        )
        assert "`Roster`" in _problem("test.subject").detail

    def test_no_guild_id_records_nothing(self, temp_db):
        """The legacy single-guild call paths pass guild_id=None; there is
        nobody to attribute the problem to."""
        assert (
            config_health.record_sheet_failure(
                None, "test.subject", gspread.exceptions.WorksheetNotFound("x")
            )
            is False
        )


# ── Train Schedule ───────────────────────────────────────────────────────────

TRAIN_SUBJECT = "train.schedule"


class TestTrainSchedule:
    def test_missing_tab_on_load_is_recorded(self, seeded_db):
        with patch(
            "train._get_train_sheet",
            MagicMock(side_effect=gspread.exceptions.WorksheetNotFound("Train Schedule")),
        ):
            assert train.load_schedule(TEST_GUILD_ID) == {}
        assert _problem(TRAIN_SUBJECT) is not None

    def test_a_clean_load_clears_it(self, seeded_db):
        config_health.record(TEST_GUILD_ID, TRAIN_SUBJECT, config_health.MISSING_TAB, "gone")
        ws = MagicMock()
        ws.get_all_values.return_value = [["Date", "Name"], ["2026-08-05", "alice"]]
        with patch("train._get_train_sheet", MagicMock(return_value=ws)):
            assert train.load_schedule(TEST_GUILD_ID)
        assert _problem(TRAIN_SUBJECT) is None

    def test_an_empty_but_readable_sheet_is_not_a_problem(self, seeded_db):
        """The bug being fixed: an empty dict used to mean both "nothing
        scheduled" and "I couldn't read it"."""
        ws = MagicMock()
        ws.get_all_values.return_value = [["Date", "Name"]]
        with patch("train._get_train_sheet", MagicMock(return_value=ws)):
            assert train.load_schedule(TEST_GUILD_ID) == {}
        assert _problem(TRAIN_SUBJECT) is None

    def test_a_failed_save_is_recorded(self, seeded_db):
        with patch(
            "train._get_train_sheet",
            MagicMock(side_effect=gspread.exceptions.SpreadsheetNotFound()),
        ):
            train.save_schedule({"2026-08-05": {"name": "alice"}}, guild_id=TEST_GUILD_ID)
        assert _problem(TRAIN_SUBJECT).kind == config_health.MISSING_SHEET

    def test_a_rate_limit_does_not_alarm(self, seeded_db):
        with patch("train._get_train_sheet", MagicMock(side_effect=_api_error(429))):
            train.load_schedule(TEST_GUILD_ID)
        assert _problem(TRAIN_SUBJECT) is None

    def test_blurb_log_failure_is_recorded(self, seeded_db):
        with patch(
            "train._get_train_sheet",
            MagicMock(side_effect=gspread.exceptions.WorksheetNotFound("Train Schedule")),
        ):
            assert train.load_blurb_log(TEST_GUILD_ID) == set()
        assert _problem(TRAIN_SUBJECT) is not None

    def test_subject_copy_is_registered(self):
        assert config_health.get_subject(TRAIN_SUBJECT).label == "your Train Schedule tab"


# ── Member Roster ────────────────────────────────────────────────────────────

ROSTER_SUBJECT = "roster.sheet"


class TestRosterAutoSync:
    def _cog(self):
        cog = member_roster.MemberRosterCog.__new__(member_roster.MemberRosterCog)
        cog.bot = MagicMock()
        return cog

    @pytest.fixture
    def guild(self):
        g = MagicMock()
        g.id = TEST_GUILD_ID
        return g

    @pytest.mark.asyncio
    async def test_alliance_owned_failure_records_and_skips_sentry(self, seeded_db, guild):
        """Before #414 this paged Sentry on every member event, which is what
        buries real bugs under one guild's renamed tab."""
        capture = MagicMock()
        with (
            patch("premium.feature_gate", AsyncMock(return_value=True)),
            patch(
                "member_roster.get_member_roster_config",
                MagicMock(return_value={"enabled": 1, "auto_sync": 1, "tab_name": "Member Roster"}),
            ),
            patch("member_roster._ensure_member_cache", AsyncMock()),
            patch(
                "member_roster.write_roster",
                MagicMock(side_effect=gspread.exceptions.WorksheetNotFound("Member Roster")),
            ),
            patch("sentry_sdk.capture_exception", capture),
        ):
            await self._cog()._auto_sync_if_enabled(guild)

        assert _problem(ROSTER_SUBJECT) is not None
        capture.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_genuine_bug_still_pages(self, seeded_db, guild):
        capture = MagicMock()
        with (
            patch("premium.feature_gate", AsyncMock(return_value=True)),
            patch(
                "member_roster.get_member_roster_config",
                MagicMock(return_value={"enabled": 1, "auto_sync": 1, "tab_name": "Member Roster"}),
            ),
            patch("member_roster._ensure_member_cache", AsyncMock()),
            patch(
                "member_roster.write_roster", MagicMock(side_effect=RuntimeError("schema drift"))
            ),
            patch("sentry_sdk.capture_exception", capture),
        ):
            await self._cog()._auto_sync_if_enabled(guild)

        assert _problem(ROSTER_SUBJECT) is None
        capture.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_clean_sync_clears_it(self, seeded_db, guild):
        config_health.record(TEST_GUILD_ID, ROSTER_SUBJECT, config_health.MISSING_TAB, "gone")
        with (
            patch("premium.feature_gate", AsyncMock(return_value=True)),
            patch(
                "member_roster.get_member_roster_config",
                MagicMock(return_value={"enabled": 1, "auto_sync": 1, "tab_name": "Member Roster"}),
            ),
            patch("member_roster._ensure_member_cache", AsyncMock()),
            patch("member_roster.write_roster", MagicMock(return_value=(5, {}))),
        ):
            await self._cog()._auto_sync_if_enabled(guild)

        assert _problem(ROSTER_SUBJECT) is None

    def test_subject_copy_is_registered(self):
        assert config_health.get_subject(ROSTER_SUBJECT).label == "your Member Roster tab"


# ── Storm DS/CS assignments ──────────────────────────────────────────────────

STORM_SUBJECT = "storm.assignments"


class TestStormAssignments:
    def test_unreadable_ds_sheet_is_recorded_though_it_returns_normally(self, seeded_db):
        """The heart of #414's storm case: the call returns a usable value, so
        without this record nothing anywhere looks wrong."""
        with patch(
            "storm._get_spreadsheet",
            MagicMock(side_effect=gspread.exceptions.WorksheetNotFound("DS Assignments")),
        ):
            zones, subs = storm.load_ds_assignments("A", guild_id=TEST_GUILD_ID)

        assert (zones, subs) == ({}, [])  # DS falls back to empty, not an error
        assert _problem(STORM_SUBJECT) is not None
        assert storm.assignments_unreadable(TEST_GUILD_ID) is True

    def test_unreadable_cs_sheet_returns_real_defaults(self, seeded_db):
        """CS is the sharper version: CS_DEFAULTS is populated, so a failed
        read hands back a full-looking zone layout that is not the alliance's."""
        with patch(
            "storm._get_spreadsheet",
            MagicMock(side_effect=gspread.exceptions.WorksheetNotFound("DS Assignments")),
        ):
            zones = storm.load_cs_assignments("A", guild_id=TEST_GUILD_ID)

        assert zones  # populated defaults, indistinguishable from saved data
        assert storm.assignments_unreadable(TEST_GUILD_ID) is True

    def test_no_saved_assignments_is_not_a_problem(self, seeded_db):
        """An empty tab legitimately means "use defaults" and must not be
        confused with a tab that could not be read."""
        ws = MagicMock()
        ws.get_all_values.return_value = []
        sh = MagicMock()
        sh.worksheet.return_value = ws
        with patch("storm._get_spreadsheet", MagicMock(return_value=sh)):
            storm.load_ds_assignments("A", guild_id=TEST_GUILD_ID)

        assert _problem(STORM_SUBJECT) is None
        assert storm.assignments_unreadable(TEST_GUILD_ID) is False

    def test_a_clean_read_clears_it(self, seeded_db):
        config_health.record(TEST_GUILD_ID, STORM_SUBJECT, config_health.MISSING_TAB, "gone")
        ws = MagicMock()
        ws.get_all_values.return_value = []
        sh = MagicMock()
        sh.worksheet.return_value = ws
        with patch("storm._get_spreadsheet", MagicMock(return_value=sh)):
            storm.load_ds_assignments("A", guild_id=TEST_GUILD_ID)

        assert _problem(STORM_SUBJECT) is None

    def test_cs_shares_the_same_subject(self, seeded_db):
        """DS and CS live on one tab, so one broken tab is one notice."""
        with patch(
            "storm._get_spreadsheet",
            MagicMock(side_effect=gspread.exceptions.WorksheetNotFound("DS Assignments")),
        ):
            storm.load_cs_assignments("A", guild_id=TEST_GUILD_ID)
            storm.load_ds_assignments("A", guild_id=TEST_GUILD_ID)

        assert len(config_health.problems(TEST_GUILD_ID)) == 1

    def test_a_failed_save_is_recorded(self, seeded_db):
        with patch(
            "storm._get_spreadsheet",
            MagicMock(side_effect=gspread.exceptions.SpreadsheetNotFound()),
        ):
            storm.save_ds_assignments("A", {"z": "a"}, [], guild_id=TEST_GUILD_ID)

        assert _problem(STORM_SUBJECT).kind == config_health.MISSING_SHEET

    def test_a_rate_limit_does_not_alarm(self, seeded_db):
        with patch("storm._get_spreadsheet", MagicMock(side_effect=_api_error(429))):
            storm.load_ds_assignments("A", guild_id=TEST_GUILD_ID)

        assert _problem(STORM_SUBJECT) is None

    def test_unreadable_is_false_without_a_guild(self, seeded_db):
        assert storm.assignments_unreadable(None) is False

    def test_a_transient_failure_leaves_the_draft_unwarned(self, seeded_db):
        """assignments_unreadable drives an in-draft warning, so a 429 must not
        trip it — the draft really is built from saved data most of the time."""
        with patch("storm._get_spreadsheet", MagicMock(side_effect=_api_error(429))):
            storm.load_ds_assignments("A", guild_id=TEST_GUILD_ID)
        assert storm.assignments_unreadable(TEST_GUILD_ID) is False

    def test_subject_copy_is_registered(self):
        assert config_health.get_subject(STORM_SUBJECT).label == "your DS/CS Assignments tab"


if __name__ == "__main__":
    pytest.main([__file__])
