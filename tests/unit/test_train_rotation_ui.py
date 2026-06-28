"""
Unit tests for train_rotation_ui.py — the pure helpers and embed builders.

The interactive Views need Discord interaction mocking and are exercised at the
integration layer; here we cover the deterministic bits: week_start_for,
roster-name resolution, and that every embed builder renders without raising.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import discord
import pytest

import train_rotation as tr
import train_rotation_ui as ui


def test_week_start_for_returns_monday():
    # 2026-06-03 is a Wednesday → Monday is 2026-06-01.
    assert ui.week_start_for(date(2026, 6, 3)) == date(2026, 6, 1)
    # A Monday maps to itself.
    assert ui.week_start_for(date(2026, 6, 1)) == date(2026, 6, 1)
    # A Sunday maps back to the prior Monday.
    assert ui.week_start_for(date(2026, 6, 7)) == date(2026, 6, 1)


def test_resolve_roster_name_exact_and_substring():
    state = ui.RotationState(
        cfg={},
        roster=[{"name": "Alice", "discord_id": "1"}, {"name": "Bob Smith", "discord_id": "2"}],
        eligible_pool=["Alice", "Bob Smith"],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=set(),
    )
    assert ui._resolve_roster_name(state, "alice") == "Alice"  # case-insensitive exact
    assert ui._resolve_roster_name(state, "smith") == "Bob Smith"  # unique substring
    assert ui._resolve_roster_name(state, "Nonmember") == "Nonmember"  # passthrough


def test_resolve_roster_name_ambiguous_passes_through():
    state = ui.RotationState(
        cfg={},
        roster=[{"name": "Alex"}, {"name": "Alexa"}],
        eligible_pool=[],
        role_pools={},
        member_rules=[],
        history=[],
        counted_reasons=set(),
    )
    # "ale" is a substring of both with no exact match → ambiguous → use as typed.
    assert ui._resolve_roster_name(state, "ale") == "ale"
    # An exact (case-insensitive) match still resolves even though "alex" is
    # also a substring of "Alexa".
    assert ui._resolve_roster_name(state, "alex") == "Alex"


def test_resolve_name_from_list():
    names = ["Alice", "Bob Smith"]
    assert ui._resolve_name_from_list(names, "alice") == "Alice"  # exact, case-insensitive
    assert ui._resolve_name_from_list(names, "smith") == "Bob Smith"  # unique substring
    assert ui._resolve_name_from_list(names, "Nonmember") == "Nonmember"  # off-roster passthrough


def test_load_rotation_state_gates_role_pools_on_premium():
    # Role pools are built only for Premium guilds (#337); free tier gets an
    # empty role-pool map and role_rules_enabled False so the draft falls back
    # to the full roster.
    roster = [{"name": "Alice", "discord_id": "111"}]
    cfg = {
        "rule_type_roles": {"vs": 555},
        "member_rules_tab": "",
        "history_tab": "",
        "counted_reasons": "",
    }
    bot = MagicMock()
    guild = MagicMock()
    bot.get_guild.return_value = guild
    role = MagicMock()
    role_member = MagicMock()
    role_member.id = 111  # matches Alice's roster discord_id
    role.members = [role_member]
    guild.get_role.return_value = role

    with (
        patch("config.get_train_config", return_value=cfg),
        patch("train_rotation.load_roster_members", return_value=roster),
        patch("train_rotation.load_member_rules", return_value=[]),
        patch("train_rotation.load_history", return_value=[]),
        patch("train_rotation.canonicalize_history", side_effect=lambda h, r: h),
        patch("train_rotation.parse_counted_reasons", return_value=set()),
        patch("train_rotation_ui._resolve_leadership_role", return_value=None),
    ):
        premium_state = ui.load_rotation_state(bot, 1, is_premium=True)
        free_state = ui.load_rotation_state(bot, 1, is_premium=False)

    assert premium_state.role_pools.get("vs") == ["Alice"]
    assert premium_state.role_rules_enabled is True
    assert free_state.role_pools == {}
    assert free_state.role_rules_enabled is False


def test_roster_picker_populated_has_dropdown_empty_is_type_only():
    async def _noop(_name):
        return None

    populated = ui._RosterPickerView(
        ["Alice", "Bob"], current="", prompt="?", modal_title="x", on_commit=_noop
    )
    assert any(isinstance(c, discord.ui.Select) for c in populated.children)  # roster dropdown
    empty = ui._RosterPickerView([], current="", prompt="?", modal_title="x", on_commit=_noop)
    assert not any(isinstance(c, discord.ui.Select) for c in empty.children)  # type-a-name only


def test_roster_picker_toggle_swaps_role_and_full_roster():
    async def _noop(_name):
        return None

    p = ui._RosterPickerView(
        ["Bob", "Carol"],
        current="",
        prompt="?",
        modal_title="x",
        on_commit=_noop,
        full_names=["Alice", "Bob", "Carol"],
        scope="\nrole",
        full_scope="\nfull",
    )
    labels = [getattr(c, "label", "") or "" for c in p.children]
    assert any("full roster" in low.lower() for low in labels)  # toggle present
    assert p.names == ["Bob", "Carol"]  # starts on the role pool
    p.showing_full = True
    assert p.names == ["Alice", "Bob", "Carol"]  # toggled to the full roster


def test_roster_picker_no_toggle_when_full_equals_filtered():
    async def _noop(_name):
        return None

    # full_names equal to names (an auto/manual day) → nothing to switch to.
    p = ui._RosterPickerView(
        ["Alice", "Bob"],
        current="",
        prompt="?",
        modal_title="x",
        on_commit=_noop,
        full_names=["Alice", "Bob"],
    )
    labels = [getattr(c, "label", "") or "" for c in p.children]
    assert not any("full roster" in low.lower() for low in labels)


def test_assign_pool_filters_to_role_for_role_days():
    state = ui.RotationState(
        cfg={},
        roster=[{"name": "Alice"}, {"name": "Bob"}, {"name": "Carol"}],
        eligible_pool=["Alice", "Bob", "Carol"],
        role_pools={tr.RULE_LEADERSHIP: ["Bob", "Carol"]},
        member_rules=[],
        history=[],
        counted_reasons=set(),
    )
    # A Leadership day offers only the leadership pool, with a scope note.
    names, scope = ui._assign_pool_for_day(state, tr.RULE_LEADERSHIP)
    assert names == ["Bob", "Carol"]
    assert "Leadership" in scope
    # An auto day (no role pool) offers the full roster and no scope note.
    names, scope = ui._assign_pool_for_day(state, tr.RULE_AUTO)
    assert names == ["Alice", "Bob", "Carol"]
    assert scope == ""


def test_default_draft_week_jumps_to_upcoming_on_draft_day():
    # Draft day = Sunday (6). On Sunday, default to next week; other days, current.
    assert ui.default_draft_week(date(2026, 6, 7), 6) == date(2026, 6, 8)  # Sun → next Monday
    assert ui.default_draft_week(date(2026, 6, 3), 6) == date(2026, 6, 1)  # Wed → current Monday


def _draft():
    return [
        tr.DraftDay("2026-06-01", 0, tr.RULE_AUTO, "Alice", "auto"),
        tr.DraftDay("2026-06-05", 4, tr.RULE_VS, None, "vs", needs_picking=True),
        tr.DraftDay("2026-06-07", 6, tr.RULE_BIRTHDAY, "Eve", "birthday", note="birthday 🎂"),
    ]


def test_weekly_draft_embed_renders():
    embed = ui.build_weekly_draft_embed(_draft(), date(2026, 6, 1), "Standard Week")
    assert "Week of" in embed.title
    assert "Standard Week" in [f.value for f in embed.fields]
    # the VS-with-no-role day is manual by design → compact "✏️ Manual" marker
    assert "✏️ Manual" in embed.description


def test_weekly_draft_embed_mentions_ids_names_fallback_no_codeblock():
    draft = [
        tr.DraftDay(
            "2026-06-01", 0, tr.RULE_LEADERSHIP, "pinkcatboi", "leadership", discord_id="111"
        ),
        tr.DraftDay("2026-06-02", 1, tr.RULE_AUTO, "OffRoster", "auto"),  # no id → plain name
        tr.DraftDay("2026-06-03", 2, tr.RULE_VS, None, "vs", needs_picking=True),  # manual marker
    ]
    desc = ui.build_weekly_draft_embed(draft, date(2026, 6, 1), "Standard Week").description
    assert "<@111>" in desc  # known ID → @mention
    assert "OffRoster" in desc  # no ID → plain name
    assert "Leadership" in desc and "Auto" in desc  # short labels, not "Auto (fair rotation)"
    assert "(fair rotation)" not in desc
    assert "```" not in desc  # no code block (so mentions render)
    assert "✏️ Manual" in desc


def test_weekly_draft_embed_requires_selection_for_unresolved_auto():
    # An auto day that couldn't resolve (empty pool) shows the ⚠️ warning,
    # distinct from a manual day's clean "Manual assignment".
    draft = [tr.DraftDay("2026-06-01", 0, tr.RULE_AUTO, None, "auto", needs_picking=True)]
    embed = ui.build_weekly_draft_embed(draft, date(2026, 6, 1), "Standard Week")
    assert tr.NEEDS_PICKING_LABEL in embed.description


def test_conductor_cell_distinguishes_states():
    auto_empty = tr.DraftDay("2026-06-01", 0, tr.RULE_AUTO, None, "auto", needs_picking=True)
    manual = tr.DraftDay("2026-06-02", 1, tr.RULE_MANUAL, None, "manual", needs_picking=True)
    vs_norole = tr.DraftDay("2026-06-03", 2, tr.RULE_VS, None, "vs", needs_picking=True)
    assert ui._conductor_cell(auto_empty) == tr.NEEDS_PICKING_LABEL
    assert ui._conductor_cell(manual) == ui.MANUAL_LABEL
    assert ui._conductor_cell(vs_norole) == ui.MANUAL_LABEL


def test_preset_editor_embed_dirty_banner():
    preset = tr.SchedulePreset.default("Standard Week")
    clean = ui.build_preset_editor_embed(preset, dirty=False)
    dirty = ui.build_preset_editor_embed(preset, dirty=True)
    assert "Unsaved changes" not in (clean.description or "")
    assert "Unsaved changes" in dirty.description


def test_daily_confirm_embed_with_and_without_member():
    with_m = ui.build_daily_confirm_embed(_draft()[0])
    assert "Alice" in with_m.description
    # _draft()[1] is a VS (manual) day → "Manual assignment" prompt
    without = ui.build_daily_confirm_embed(_draft()[1])
    assert ui.MANUAL_LABEL in without.description


def test_public_post_embed_includes_blurb_and_image():
    embed = ui.build_public_post_embed(
        _draft()[0], blurb="Great driving!", image_url="https://x/y.png"
    )
    assert "Alice" in embed.description
    assert any("Great driving" in (f.value or "") for f in embed.fields)
    assert embed.image.url == "https://x/y.png"


def test_assignment_logs_embed_empty():
    embed = ui.build_assignment_logs_embed([], [])
    assert "No assignments logged" in embed.description


def test_assignment_logs_embed_sections():
    # tally is the pre-built (name, count, last) list from tr.member_tally.
    tally = [
        ("Alice", 5, "2026-06-08"),
        ("Bob", 2, "2026-06-01"),
        ("Zoe", 0, ""),  # never driven — should head "Fewest"
    ]
    posted = [
        tr.HistoryRow("2026-06-08", "Alice", "auto", tr.STATUS_POSTED),
        tr.HistoryRow("2026-06-01", "Bob", "vs", tr.STATUS_POSTED),
    ]
    embed = ui.build_assignment_logs_embed(tally, posted)
    fields = {f.name: f.value for f in embed.fields}
    most = fields["🔝 Most trains"]
    assert most.index("Alice") < most.index("Bob")  # most-driven first
    assert "5 trains" in most
    fewest = fields["🔻 Fewest trains"]
    assert "Zoe" in fewest and "never" in fewest  # never-driven roster member surfaces
    assert "2026-06-08" in fields["🕒 Most recent"]
    assert "3 conductor" in (embed.footer.text or "")


def test_history_page_member_paginates_and_sorts():
    tally = [(f"M{i:02d}", i, "") for i in range(20)]  # 20 members, M19 = most trains
    p0 = ui.build_history_page_embed(tally, [], mode="member", sort_key=tr.TALLY_SORT_MOST, page=0)
    assert "Page 1 of 2" in p0.description  # 20 rows / 15 per page → 2 pages
    assert "M19" in p0.description  # most trains on page 1
    p1 = ui.build_history_page_embed(tally, [], mode="member", sort_key=tr.TALLY_SORT_MOST, page=1)
    assert "Page 2 of 2" in p1.description
    assert "M00" in p1.description  # fewest trains trails to page 2


def test_history_page_date_mode_sort_direction():
    posted = [
        tr.HistoryRow("2026-06-01", "Bob", "auto", tr.STATUS_POSTED),
        tr.HistoryRow("2026-06-08", "Alice", "auto", tr.STATUS_POSTED),
    ]
    newest = ui.build_history_page_embed([], posted, mode="date", sort_key="newest", page=0)
    assert newest.description.index("2026-06-08") < newest.description.index("2026-06-01")
    oldest = ui.build_history_page_embed([], posted, mode="date", sort_key="oldest", page=0)
    assert oldest.description.index("2026-06-01") < oldest.description.index("2026-06-08")


def test_assignment_logs_view_swaps_components_by_mode():
    tally = [("Alice", 3, "2026-06-08"), ("Zoe", 0, "")]
    posted = [tr.HistoryRow("2026-06-08", "Alice", "auto", tr.STATUS_POSTED)]
    view = ui.AssignmentLogsView(owner_id=1, tally=tally, posted=posted)
    # summary mode → just the View-all button
    labels = [getattr(c, "label", None) for c in view.children]
    assert ui.AssignmentLogsView.BTN_VIEW_ALL in labels
    # pager (member) mode → toggle + sort select + prev/next/back
    view.mode = "member"
    view._sync()
    labels = [getattr(c, "label", None) for c in view.children]
    assert ui.AssignmentLogsView.BTN_BY_MEMBER in labels
    assert ui.AssignmentLogsView.BTN_BY_DATE in labels
    assert ui.AssignmentLogsView.BTN_BACK in labels
    assert any(isinstance(c, discord.ui.Select) for c in view.children)


def test_assignment_logs_view_empty_hides_view_all():
    view = ui.AssignmentLogsView(owner_id=1, tally=[], posted=[])
    labels = [getattr(c, "label", None) for c in view.children]
    assert ui.AssignmentLogsView.BTN_VIEW_ALL not in labels


class TestLoadWeekDraft:
    """load_week_draft reconstructs an editable draft from scheduled history
    rows, so /train draft_week reopens the week without re-rolling."""

    def test_reconstructs_from_scheduled_rows(self):
        week_start = date(2026, 6, 1)
        history = [
            tr.HistoryRow("2026-06-01", "Alice", "auto", tr.STATUS_SCHEDULED),
            tr.HistoryRow("2026-06-03", "Charlie", "leadership", tr.STATUS_SCHEDULED),
            tr.HistoryRow("2026-05-25", "Old", "auto", tr.STATUS_POSTED),  # prior week, ignored
        ]
        with (
            patch("config.get_train_config", return_value={"history_tab": "H"}),
            patch("train_rotation.load_history", return_value=history),
        ):
            draft = ui.load_week_draft(MagicMock(), 1, week_start)
        assert len(draft) == 7
        mon = next(d for d in draft if d.date == "2026-06-01")
        assert mon.member == "Alice"
        wed = next(d for d in draft if d.date == "2026-06-03")
        assert wed.member == "Charlie"
        # A day with no scheduled row is a needs-picking gap, not crash.
        tue = next(d for d in draft if d.date == "2026-06-02")
        assert tue.member is None and tue.needs_picking

    def test_posted_rows_survive_reopen(self):
        week_start = date(2026, 6, 1)
        history = [
            tr.HistoryRow("2026-06-01", "Alice", "auto", tr.STATUS_POSTED),  # already drove
        ]
        with (
            patch("config.get_train_config", return_value={"history_tab": "H"}),
            patch("train_rotation.load_history", return_value=history),
        ):
            draft = ui.load_week_draft(MagicMock(), 1, week_start)
        mon = next(d for d in draft if d.date == "2026-06-01")
        assert mon.member == "Alice"  # posted day keeps its conductor, not needs-picking

    def test_falls_back_to_regenerate_when_no_scheduled_rows(self):
        week_start = date(2026, 6, 1)
        sentinel = [tr.DraftDay("2026-06-01", 0, tr.RULE_AUTO, "Gen", "auto")]
        with (
            patch("config.get_train_config", return_value={"history_tab": "H"}),
            patch("train_rotation.load_history", return_value=[]),
            patch("train_rotation_ui.regenerate_week", return_value=sentinel) as regen,
        ):
            draft = ui.load_week_draft(MagicMock(), 1, week_start)
        regen.assert_called_once()
        assert draft is sentinel
