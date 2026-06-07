"""
Unit tests for train_rotation.py — the Sheet I/O layer.

A FakeWS models the exact gspread surface train_rotation uses
(get_all_values / batch_clear / update("A2", ...)). config.get_spreadsheet
and config.get_or_create_worksheet are patched to hand back a single shared
FakeWS so load → save → load round-trips through real module logic.
"""

from datetime import date
from unittest.mock import patch

import pytest

import train_rotation as tr
from train_rotation import DayRule, DraftDay, SchedulePreset

GID = 12345


class FakeWS:
    """In-memory worksheet. Models the batch_clear + update('A2', rows) cycle
    that train_rotation._rewrite performs."""

    def __init__(self, header, body=None):
        self.header = list(header)
        self.rows = [list(header)] + [list(r) for r in (body or [])]

    def get_all_values(self):
        return [list(r) for r in self.rows]

    def batch_clear(self, ranges):
        self.rows = [list(self.header)]

    def update(self, rng, values, value_input_option=None):
        self.rows = [list(self.header)] + [list(r) for r in values]


@pytest.fixture
def patched_tab():
    """Patch the gspread client so train_rotation talks to a shared FakeWS.

    Yields a dict mapping tab_name → FakeWS; the worksheet for a tab is
    created with the right header on first touch."""
    sheets: dict[str, FakeWS] = {}
    headers = {
        "Train History": tr.HISTORY_HEADER,
        "Train Member Rules": tr.MEMBER_RULES_HEADER,
        "Train Day Rules": tr.DAY_RULES_HEADER,
    }

    def fake_get_or_create(sh, tab_name, header_row=None, rows=None, cols=None):
        if tab_name not in sheets:
            sheets[tab_name] = FakeWS(header_row or headers.get(tab_name, []))
        return sheets[tab_name]

    with (
        patch("config.get_spreadsheet", return_value=object()),
        patch("config.get_or_create_worksheet", side_effect=fake_get_or_create),
    ):
        yield sheets


# ── History ──────────────────────────────────────────────────────────────────


def test_history_write_draft_and_load_roundtrip(patched_tab):
    draft = [
        DraftDay("2026-06-01", 0, tr.RULE_AUTO, "Alice", "auto"),
        DraftDay("2026-06-02", 1, tr.RULE_VS, None, "vs", needs_picking=True),
    ]
    assert tr.write_draft_rows(GID, "Train History", draft) is True
    loaded = tr.load_history(GID, "Train History")
    assert len(loaded) == 2
    assert loaded[0].member == "Alice"
    assert loaded[0].status == tr.STATUS_SCHEDULED
    assert loaded[1].member == ""  # needs-picking row written with empty member


def test_history_write_stamps_discord_id_from_roster(patched_tab):
    # The bot resolves the conductor's Discord ID from the roster on write, so
    # the row carries the identity key (name is only the fallback).
    roster = [{"name": "Alice", "discord_id": "999"}]
    with patch("train_rotation.load_roster_members", return_value=roster):
        tr.write_draft_rows(
            GID, "Train History", [DraftDay("2026-06-01", 0, tr.RULE_AUTO, "Alice", "auto")]
        )
        tr.set_day_status(
            GID,
            "Train History",
            "2026-06-02",
            member="Alice",
            reason="auto",
            status=tr.STATUS_POSTED,
        )
    loaded = {h.date: h for h in tr.load_history(GID, "Train History")}
    assert loaded["2026-06-01"].discord_id == "999"  # stamped on the scheduled draft row
    assert loaded["2026-06-02"].discord_id == "999"  # and on the confirmed/posted row


def test_history_write_draft_replaces_week_keeps_other_rows(patched_tab):
    # Seed an older posted row outside the draft week.
    tr.set_day_status(
        GID,
        "Train History",
        "2026-05-25",
        member="OldDriver",
        reason="auto",
        status=tr.STATUS_POSTED,
        posted_at="2026-05-25T10:00",
    )
    draft = [DraftDay("2026-06-01", 0, tr.RULE_AUTO, "Alice", "auto")]
    tr.write_draft_rows(GID, "Train History", draft)
    # Re-run the same draft week with a different member — should replace, not dup.
    draft2 = [DraftDay("2026-06-01", 0, tr.RULE_AUTO, "Bob", "auto")]
    tr.write_draft_rows(GID, "Train History", draft2)

    loaded = tr.load_history(GID, "Train History")
    jun1 = [h for h in loaded if h.date == "2026-06-01"]
    assert len(jun1) == 1
    assert jun1[0].member == "Bob"
    # The older posted row survived.
    assert any(h.date == "2026-05-25" and h.status == tr.STATUS_POSTED for h in loaded)


def test_set_day_status_confirm_updates_in_place(patched_tab):
    draft = [DraftDay("2026-06-01", 0, tr.RULE_AUTO, "Alice", "auto")]
    tr.write_draft_rows(GID, "Train History", draft)
    tr.set_day_status(
        GID,
        "Train History",
        "2026-06-01",
        member="Alice",
        reason="auto",
        status=tr.STATUS_POSTED,
        posted_at="2026-06-01T09:00",
    )
    loaded = tr.load_history(GID, "Train History")
    rows = [h for h in loaded if h.date == "2026-06-01"]
    assert len(rows) == 1
    assert rows[0].status == tr.STATUS_POSTED
    assert rows[0].posted_at == "2026-06-01T09:00"


def test_set_day_status_appends_when_no_existing_row(patched_tab):
    tr.set_day_status(
        GID,
        "Train History",
        "2026-06-09",
        member="New",
        reason="manual",
        status=tr.STATUS_POSTED,
    )
    loaded = tr.load_history(GID, "Train History")
    assert len(loaded) == 1
    assert loaded[0].member == "New"


# ── Member rules ─────────────────────────────────────────────────────────────


def test_member_rule_set_load_upsert(patched_tab):
    tr.set_member_rule(GID, "Train Member Rules", "Alice", tr.MEMBER_RULE_OPT_OUT)
    tr.set_member_rule(GID, "Train Member Rules", "Bob", tr.MEMBER_RULE_SKIP_UNTIL, "2026-07-01")
    # Upsert: change Bob's skip date — should not duplicate.
    tr.set_member_rule(GID, "Train Member Rules", "Bob", tr.MEMBER_RULE_SKIP_UNTIL, "2026-08-01")

    rules = tr.load_member_rules(GID, "Train Member Rules")
    bob = [r for r in rules if r.member == "Bob"]
    assert len(bob) == 1
    assert bob[0].value == "2026-08-01"
    assert len(rules) == 2


def test_member_rule_clear_specific_type(patched_tab):
    tr.set_member_rule(GID, "Train Member Rules", "Bob", tr.MEMBER_RULE_OPT_OUT)
    tr.set_member_rule(GID, "Train Member Rules", "Bob", tr.MEMBER_RULE_SKIP_UNTIL, "2026-07-01")
    tr.clear_member_rule(GID, "Train Member Rules", "Bob", tr.MEMBER_RULE_OPT_OUT)
    rules = tr.load_member_rules(GID, "Train Member Rules")
    assert len(rules) == 1
    assert rules[0].rule_type == tr.MEMBER_RULE_SKIP_UNTIL


def test_member_rule_clear_all_for_member(patched_tab):
    tr.set_member_rule(GID, "Train Member Rules", "Bob", tr.MEMBER_RULE_OPT_OUT)
    tr.set_member_rule(GID, "Train Member Rules", "Bob", tr.MEMBER_RULE_SKIP_UNTIL, "2026-07-01")
    tr.set_member_rule(GID, "Train Member Rules", "Carol", tr.MEMBER_RULE_OPT_OUT)
    tr.clear_member_rule(GID, "Train Member Rules", "Bob")
    rules = tr.load_member_rules(GID, "Train Member Rules")
    assert len(rules) == 1
    assert rules[0].member == "Carol"


# ── Presets ──────────────────────────────────────────────────────────────────


def test_preset_save_load_roundtrip(patched_tab):
    preset = SchedulePreset.default("Standard Week")
    preset.days[2] = DayRule(2, tr.RULE_LEADERSHIP)
    preset.days[4] = DayRule(4, tr.RULE_SPECIFIC, specific_member="Captain")
    assert tr.save_preset(GID, "Train Day Rules", preset) is True

    loaded = tr.load_preset(GID, "Train Day Rules", "Standard Week")
    assert loaded is not None
    assert len(loaded.days) == 7
    assert loaded.days[2].rule_type == tr.RULE_LEADERSHIP
    assert loaded.days[4].rule_type == tr.RULE_SPECIFIC
    assert loaded.days[4].specific_member == "Captain"
    assert loaded.days[0].rule_type == tr.RULE_AUTO  # default unchanged


def test_preset_list_and_multiple_presets(patched_tab):
    tr.save_preset(GID, "Train Day Rules", SchedulePreset.default("Standard Week"))
    tr.save_preset(GID, "Train Day Rules", SchedulePreset.default("VS Save Week"))
    names = tr.list_presets(GID, "Train Day Rules")
    assert set(names) == {"Standard Week", "VS Save Week"}


def test_preset_save_replaces_only_its_own_rows(patched_tab):
    tr.save_preset(GID, "Train Day Rules", SchedulePreset.default("Standard Week"))
    tr.save_preset(GID, "Train Day Rules", SchedulePreset.default("VS Save Week"))
    # Re-save Standard Week with an edit; VS Save Week must remain intact.
    edited = SchedulePreset.default("Standard Week")
    edited.days[0] = DayRule(0, tr.RULE_VS)
    tr.save_preset(GID, "Train Day Rules", edited)

    assert set(tr.list_presets(GID, "Train Day Rules")) == {"Standard Week", "VS Save Week"}
    vs = tr.load_preset(GID, "Train Day Rules", "VS Save Week")
    assert vs.days[0].rule_type == tr.RULE_AUTO  # untouched
    std = tr.load_preset(GID, "Train Day Rules", "Standard Week")
    assert std.days[0].rule_type == tr.RULE_VS


def test_preset_delete(patched_tab):
    tr.save_preset(GID, "Train Day Rules", SchedulePreset.default("Standard Week"))
    tr.save_preset(GID, "Train Day Rules", SchedulePreset.default("Holiday Week"))
    assert tr.delete_preset(GID, "Train Day Rules", "Holiday Week") is True
    assert tr.list_presets(GID, "Train Day Rules") == ["Standard Week"]
    assert tr.load_preset(GID, "Train Day Rules", "Holiday Week") is None


# ── Graceful degradation ─────────────────────────────────────────────────────


def test_no_sheet_configured_returns_empty():
    with patch("config.get_spreadsheet", return_value=None):
        assert tr.load_history(GID, "Train History") == []
        assert tr.load_member_rules(GID, "Train Member Rules") == []
        assert tr.list_presets(GID, "Train Day Rules") == []
        assert tr.load_preset(GID, "Train Day Rules", "x") is None
        assert tr.write_draft_rows(GID, "Train History", []) is False
        assert tr.save_preset(GID, "Train Day Rules", SchedulePreset.default()) is False


def test_empty_tab_name_returns_none_safely():
    # No patching needed — empty tab short-circuits before any gspread call.
    assert tr.load_history(GID, "") == []
    assert tr.list_presets(GID, "") == []
