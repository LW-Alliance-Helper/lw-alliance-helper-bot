"""
Tests for storm_history.py (#135).

Covers the Sheet readers (list event dates, load roster, load attendance)
and the renderers (event embed + history list embed). The slash command
+ button view are integration territory.
"""

import pytest
from unittest.mock import patch

import storm_history as sh
from tests.unit.test_config import TEST_GUILD_ID


class _FakeWorksheet:
    def __init__(self, title: str, rows: list[list[str]] | None = None):
        self.title = title
        self._rows = list(rows or [])

    def get_all_values(self):
        return [list(r) for r in self._rows]


class _FakeSpreadsheet:
    def __init__(self):
        self._tabs: dict[str, _FakeWorksheet] = {}

    def worksheet(self, title: str):
        if title not in self._tabs:
            raise Exception("not found")
        return self._tabs[title]


@pytest.fixture
def fake_env(seeded_db):
    import config

    fake = _FakeSpreadsheet()

    # Two events on the rosters tab.
    # Header order mirrors storm_roster_builder._ROSTERS_HEADER from
    # production: `Override Below Minimum` comes BEFORE `Posted At (UTC)`,
    # not after. The prior fixture order was column-index-fragile.
    rosters = _FakeWorksheet("DS Rosters", [
        ["Event Date", "Team", "Zone", "Member", "Role",
         "Power at Assignment", "Discord ID", "Override Below Minimum",
         "Posted At (UTC)"],
        ["2026-05-18", "A", "Power Tower",  "Alice", "primary", "412000000", "1001", "",    ""],
        ["2026-05-18", "A", "Power Tower",  "Bob",   "primary", "350000000", "1002", "",    ""],
        ["2026-05-18", "A", "Nuclear Silo", "Carol", "primary", "280000000", "1003", "",    ""],
        ["2026-05-18", "A", "",             "Dan",   "sub",     "220000000", "1004", "",    ""],
        ["2026-05-11", "A", "Power Tower",  "Alice", "primary", "400000000", "1001", "",    ""],
        ["2026-05-11", "A", "Power Tower",  "Erin",  "primary", "190000000", "1005", "yes", ""],
        # Bad date — should be filtered out of list_event_dates.
        ["2026-13-50", "A", "Power Tower",  "Z",     "primary", "1",         "9",    "",    ""],
    ])
    fake._tabs["DS Rosters"] = rosters

    attendance = _FakeWorksheet("DS Attendance", [
        ["Event Date", "Team", "Zone", "Member", "Status",
         "Recorded By", "Recorded At (UTC)"],
        ["2026-05-18", "A", "Power Tower",  "Alice", "attended",      "999", ""],
        ["2026-05-18", "A", "Power Tower",  "Bob",   "no_show",       "999", ""],
        ["2026-05-18", "A", "Nuclear Silo", "Carol", "sub_activated", "999", ""],
    ])
    fake._tabs["DS Attendance"] = attendance

    for et in ("DS", "CS"):
        config.save_storm_config(
            TEST_GUILD_ID, et, tab_name=f"{et} Tab", mail_template="x",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, et, structured_flow_enabled=True,
        )

    with patch.object(config, "get_spreadsheet", return_value=fake):
        yield fake, TEST_GUILD_ID


# ── Loaders ──────────────────────────────────────────────────────────────────


class TestListEventDates:
    def test_returns_unique_descending(self, fake_env):
        fake, gid = fake_env
        dates, errors = sh.list_event_dates(gid, "DS", limit=8)
        assert errors == []
        # Two distinct dates, newer first.
        assert dates == ["2026-05-18", "2026-05-11"]

    def test_limit_truncates(self, fake_env):
        fake, gid = fake_env
        dates, _ = sh.list_event_dates(gid, "DS", limit=1)
        assert dates == ["2026-05-18"]

    def test_empty_when_no_tab(self, fake_env):
        fake, gid = fake_env
        del fake._tabs["DS Rosters"]
        dates, errors = sh.list_event_dates(gid, "DS", limit=8)
        assert dates == []
        assert errors == []  # missing tab = no history yet, not an error


class TestLoadEventRoster:
    def test_filters_to_event_date(self, fake_env):
        fake, gid = fake_env
        slots, errors = sh.load_event_roster(gid, "DS", "2026-05-18")
        assert errors == []
        names = {s["member"] for s in slots}
        assert names == {"Alice", "Bob", "Carol", "Dan"}
        # Erin (on 2026-05-11) not included.
        assert "Erin" not in names

    def test_captures_override_below_floor(self, fake_env):
        fake, gid = fake_env
        slots, _errs = sh.load_event_roster(gid, "DS", "2026-05-11")
        erin = next(s for s in slots if s["member"] == "Erin")
        assert erin["override_below_floor"] is True
        alice = next(s for s in slots if s["member"] == "Alice")
        assert alice["override_below_floor"] is False

    def test_captures_role(self, fake_env):
        fake, gid = fake_env
        slots, _errs = sh.load_event_roster(gid, "DS", "2026-05-18")
        dan = next(s for s in slots if s["member"] == "Dan")
        assert dan["role"] == "sub"

    def test_missing_event_returns_empty(self, fake_env):
        fake, gid = fake_env
        slots, _errs = sh.load_event_roster(gid, "DS", "2099-01-01")
        assert slots == []


class TestLoadEventAttendance:
    def test_returns_keyed_dict(self, fake_env):
        fake, gid = fake_env
        att, errors = sh.load_event_attendance(gid, "DS", "2026-05-18")
        assert errors == []
        # Keys are normalized: member name case-folded so a stray case
        # difference between rosters_tab and attendance_tab doesn't
        # silently break the overlay.
        assert att[sh._attendance_join_key("A", "Power Tower",  "Alice")] == "attended"
        assert att[sh._attendance_join_key("A", "Power Tower",  "Bob")]   == "no_show"
        assert att[sh._attendance_join_key("A", "Nuclear Silo", "Carol")] == "sub_activated"

    def test_join_key_is_whitespace_and_case_tolerant(self, fake_env):
        """A stray case/whitespace difference between the two Sheet tabs
        used to silently kill the overlay. The normalized join key
        bridges the gap."""
        fake, gid = fake_env
        att, _ = sh.load_event_attendance(gid, "DS", "2026-05-18")
        # Roster row might have "alice" while attendance has "Alice" —
        # the join key normalizes both to the same form.
        assert att[sh._attendance_join_key("A", "Power Tower", "  ALICE  ")] == "attended"

    def test_missing_attendance_returns_empty(self, fake_env):
        fake, gid = fake_env
        del fake._tabs["DS Attendance"]
        att, errors = sh.load_event_attendance(gid, "DS", "2026-05-18")
        assert att == {}
        assert errors == []


# ── Renderers ────────────────────────────────────────────────────────────────


def _embed_body(embed) -> str:
    """Concatenate description + all field values into one string for
    `in` assertions. The renderer now splits per-team into add_field
    calls (each capped at 1024 chars) instead of one giant description,
    so tests can't just look at embed.description."""
    parts = [embed.description or ""]
    for f in embed.fields:
        parts.append(f.name or "")
        parts.append(f.value or "")
    return "\n".join(parts)


class TestRenderEventEmbed:
    def test_renders_attendance_glyphs(self):
        slots = [
            {"team": "A", "zone": "Power Tower", "member": "Alice",
             "role": "primary", "power": "412000000",
             "discord_id": "1", "override_below_floor": False},
        ]
        attendance = {
            sh._attendance_join_key("A", "Power Tower", "Alice"): "attended",
        }
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance=attendance,
        )
        body = _embed_body(embed)
        assert "Alice" in body
        assert "✅" in body

    def test_no_attendance_falls_through(self):
        slots = [
            {"team": "A", "zone": "Power Tower", "member": "Alice",
             "role": "primary", "power": "412000000",
             "discord_id": "1", "override_below_floor": False},
        ]
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance={},
        )
        # Renders an unrecorded marker, not crash.
        assert "—" in _embed_body(embed)
        # Footer hints how to record under the new parent-group command tree.
        assert "/desertstorm attendance" in (embed.footer.text or "")

    def test_below_floor_override_not_rendered(self):
        """Decision #6 (#171): the Override Below Minimum flag stays on
        the rosters_tab Sheet for post-event audit, but the history
        embed no longer surfaces it — the same drop the attendance UI
        made. Officers reviewing history don't need the build-time
        override flagged."""
        slots = [
            {"team": "A", "phase": "", "zone": "Power Tower", "member": "Erin",
             "role": "primary", "power": "190000000",
             "discord_id": "5", "override_below_floor": True,
             "paired_with": ""},
        ]
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-11",
            slots=slots, attendance={},
        )
        assert "override" not in _embed_body(embed).lower()
        assert "⚠️" not in _embed_body(embed)

    def test_empty_slots_message(self):
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=[], attendance={},
        )
        assert "No structured roster" in (embed.description or "")

    def test_grouping_by_team_and_zone(self):
        slots = [
            {"team": "A", "zone": "Power Tower",  "member": "Alice",
             "role": "primary", "power": "", "discord_id": "1", "override_below_floor": False},
            {"team": "A", "zone": "Nuclear Silo", "member": "Bob",
             "role": "primary", "power": "", "discord_id": "2", "override_below_floor": False},
        ]
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance={},
        )
        # Team renders as a separate field, not in the description.
        field_names = [f.name for f in embed.fields]
        assert "Team A" in field_names
        body = _embed_body(embed)
        assert "Power Tower" in body
        assert "Nuclear Silo" in body

    def test_teams_render_in_sorted_order(self):
        """Iteration was previously dict-insertion-order dependent —
        Team B could render before Team A if the Sheet rows happened to
        be in that order. New behaviour sorts teams alphabetically."""
        slots = [
            # B first in the input → A should still render first.
            {"team": "B", "zone": "Power Tower", "member": "Bob",
             "role": "primary", "power": "", "discord_id": "2", "override_below_floor": False},
            {"team": "A", "zone": "Power Tower", "member": "Alice",
             "role": "primary", "power": "", "discord_id": "1", "override_below_floor": False},
        ]
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance={},
        )
        field_names = [f.name for f in embed.fields]
        assert field_names == ["Team A", "Team B"]

    def test_phase_aware_event_groups_members_by_phase(self):
        """#172 / Rule L: when any primary slot carries a Phase value,
        history renders per-zone-per-phase — each zone header gets sub-
        rows for Phase 1, Phase 2, etc., and members fall under the
        phase they were rostered into."""
        slots = [
            {"team": "A", "phase": "1", "zone": "Power Tower",
             "member": "Alice", "role": "primary", "power": "",
             "discord_id": "1", "override_below_floor": False,
             "paired_with": ""},
            {"team": "A", "phase": "2", "zone": "Power Tower",
             "member": "Bob", "role": "primary", "power": "",
             "discord_id": "2", "override_below_floor": False,
             "paired_with": ""},
            {"team": "A", "phase": "3", "zone": "Power Tower",
             "member": "Carol", "role": "primary", "power": "",
             "discord_id": "3", "override_below_floor": False,
             "paired_with": ""},
        ]
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance={},
        )
        body = _embed_body(embed)
        # Phase headers appear, and the per-phase members are listed
        # under them.
        assert "**Stage 1**" in body
        assert "**Stage 2**" in body
        assert "**Stage 3**" in body
        # Each phase's member shows up after its phase header.
        p1 = body.index("**Stage 1**")
        p2 = body.index("**Stage 2**")
        p3 = body.index("**Stage 3**")
        assert "Alice" in body[p1:p2]
        assert "Bob"   in body[p2:p3]
        assert "Carol" in body[p3:]

    def test_flat_event_does_not_render_phase_headers(self):
        slots = [
            {"team": "A", "phase": "", "zone": "Power Tower",
             "member": "Alice", "role": "primary", "power": "",
             "discord_id": "1", "override_below_floor": False,
             "paired_with": ""},
        ]
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance={},
        )
        body = _embed_body(embed)
        assert "Stage 1" not in body
        assert "Stage 2" not in body

    def test_power_rendered_via_format_power(self):
        """Raw `"412000000"` should display as `"412M"` for the human
        readers — the prior renderer showed the digits verbatim."""
        slots = [
            {"team": "A", "zone": "Power Tower", "member": "Alice",
             "role": "primary", "power": "412000000",
             "discord_id": "1", "override_below_floor": False},
        ]
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance={},
        )
        body = _embed_body(embed)
        assert "412M" in body
        # No raw 9-digit pile.
        assert "412000000" not in body

    def test_power_unknown_sentinel_dropped(self):
        slots = [
            {"team": "A", "zone": "Power Tower", "member": "Erin",
             "role": "primary", "power": "unknown",
             "discord_id": "5", "override_below_floor": False},
        ]
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance={},
        )
        body = _embed_body(embed)
        # The sentinel itself isn't surfaced — just no power readout.
        assert "unknown" not in body
        assert "Erin" in body

    def test_footer_summary_counts(self):
        slots = [
            {"team": "A", "zone": "Z", "member": f"M{i}", "role": "primary",
             "power": "", "discord_id": str(i), "override_below_floor": False}
            for i in range(3)
        ]
        attendance = {
            sh._attendance_join_key("A", "Z", "M0"): "attended",
            sh._attendance_join_key("A", "Z", "M1"): "no_show",
            sh._attendance_join_key("A", "Z", "M2"): "sub_activated",
        }
        embed = sh.render_event_embed(
            event_type="DS", event_date="2026-05-18",
            slots=slots, attendance=attendance,
        )
        # Rule K (#171): footer drops 🔄 Sub activated. Legacy
        # `sub_activated` rows render as `—` and don't count toward
        # the recorded total, so `recorded` matches ✅ + ❌.
        footer = embed.footer.text or ""
        assert "✅ 1" in footer
        assert "❌ 1" in footer
        assert "🔄" not in footer
        assert "sub_activated" not in footer
        assert "recorded 2 of 3" in footer


class TestRenderHistoryListEmbed:
    def test_empty_list_message(self):
        embed = sh.render_history_list_embed("DS", [])
        assert "No structured rosters" in (embed.description or "")

    def test_populated_list_has_clickable_hint(self):
        embed = sh.render_history_list_embed("DS", ["2026-05-18", "2026-05-11"])
        # The actual buttons are on the View, not the embed; the embed
        # just sets up context.
        assert "Click" in (embed.description or "")


class TestListEventDatesFiltersMalformed:
    def test_malformed_date_dropped_from_list(self, fake_env):
        fake, gid = fake_env
        dates, _ = sh.list_event_dates(gid, "DS", limit=8)
        # "2026-13-50" is malformed and must not surface as a button —
        # rendering it would crash the date-detail renderer downstream.
        assert "2026-13-50" not in dates
        # Valid dates are still present.
        assert "2026-05-18" in dates


class TestDateButtonStaysActive:
    """Audit fix M1: the date-list buttons used to disable on the first
    click, blocking the officer from hopping between dates. They now
    stay active so multiple dates can be opened in sequence."""

    @pytest.mark.asyncio
    async def test_button_callback_does_not_disable_other_buttons(self, fake_env):
        from unittest.mock import AsyncMock, MagicMock
        fake, gid = fake_env
        view = sh._HistoryListView(
            guild_id=gid, user_id=42, event_type="DS",
            dates=["2026-05-18", "2026-05-11"],
        )
        # Capture pre-state of the buttons.
        pre_disabled = [b.disabled for b in view.children]
        assert pre_disabled == [False, False]

        # Drive the first button's callback.
        inter = MagicMock()
        inter.user = MagicMock(); inter.user.id = 42
        inter.response = MagicMock()
        inter.response.defer = AsyncMock()
        inter.followup = MagicMock()
        inter.followup.send = AsyncMock()
        await view.children[0].callback(inter)

        # After the click, the buttons are STILL active — officer can
        # click another date next.
        post_disabled = [b.disabled for b in view.children]
        assert post_disabled == [False, False]
        # The detail followup went out ephemerally.
        inter.followup.send.assert_awaited_once()
        sent_kwargs = inter.followup.send.await_args.kwargs
        assert sent_kwargs.get("ephemeral") is True


class TestOpenHistoryEphemeralConsistency:
    """Audit fix M2: all three render paths (direct-date, list,
    date-button) must post ephemerally so the entire history surface
    stays officer-only and consistent.

    Driving the full `open_history` through discord.py's permission
    plumbing in a unit test is awkward (it isinstance-checks
    `discord.Member`). Instead we cover the contract by inspecting the
    call sites directly via grep at test-write time, plus the
    date-button-callback ephemeral test below (which IS unit-testable).
    """

    @pytest.mark.asyncio
    async def test_date_button_callback_is_ephemeral(self, fake_env):
        """The third render path (date-button click) sends ephemerally."""
        from unittest.mock import AsyncMock, MagicMock
        fake, gid = fake_env
        view = sh._HistoryListView(
            guild_id=gid, user_id=42, event_type="DS",
            dates=["2026-05-18"],
        )
        inter = MagicMock()
        inter.user = MagicMock(); inter.user.id = 42
        inter.response = MagicMock()
        inter.response.defer = AsyncMock()
        inter.followup = MagicMock()
        inter.followup.send = AsyncMock()
        await view.children[0].callback(inter)
        inter.response.defer.assert_awaited_once()
        defer_kwargs = inter.response.defer.await_args.kwargs
        assert defer_kwargs.get("ephemeral") is True
        followup_kwargs = inter.followup.send.await_args.kwargs
        assert followup_kwargs.get("ephemeral") is True

    def test_direct_date_path_uses_ephemeral_followup(self):
        """Grep guard: the direct-date render path in `open_history`
        must use `ephemeral=True` on its followup.send. The audit
        flagged this as inconsistent across the three paths."""
        import inspect
        src = inspect.getsource(sh.open_history)
        # All three followup.send calls in open_history must have
        # ephemeral=True. Count the substring occurrences as a tripwire.
        assert src.count("ephemeral=True") >= 3, (
            "Expected every followup.send in open_history to carry "
            "ephemeral=True; the audit fix is to keep all three render "
            "paths officer-only."
        )


class TestRosterImageLinksView:
    """`[📷 View image]` buttons attached to the history embed when a
    saved roster-image pointer exists. Resolves the message at click
    time so deletion is detected gracefully."""

    def test_ds_two_teams_render_two_buttons(self, fake_env):
        fake, gid = fake_env
        refs = [
            {"team": "A", "channel_id": 900, "message_id": 1001},
            {"team": "B", "channel_id": 900, "message_id": 2002},
        ]
        view = sh._RosterImageLinksView(
            owner_id=42, guild_id=gid,
            event_type="DS", event_date="2026-05-18",
            refs=refs,
        )
        labels = [b.label for b in view.children]
        assert labels == ["📷 View Team A image", "📷 View Team B image"]

    def test_cs_single_button_no_team_suffix(self, fake_env):
        fake, gid = fake_env
        refs = [{"team": "", "channel_id": 900, "message_id": 5555}]
        view = sh._RosterImageLinksView(
            owner_id=42, guild_id=gid,
            event_type="CS", event_date="2026-05-18",
            refs=refs,
        )
        assert len(view.children) == 1
        assert view.children[0].label == "📷 View image"

    @pytest.mark.asyncio
    async def test_deleted_message_prunes_pointer_and_warns(self, fake_env):
        """Click-time fetch on a 404'd message → friendly warning +
        the stale pointer auto-deletes so it doesn't keep showing up."""
        import config
        from unittest.mock import AsyncMock, MagicMock
        import discord

        fake, gid = fake_env
        # Save a pointer the test can later observe being deleted.
        config.save_roster_image_ref(
            gid, "DS", "2026-05-18", "A",
            channel_id=900, message_id=1001, user_id=42,
        )

        refs = config.list_roster_image_refs(gid, "DS", "2026-05-18")
        view = sh._RosterImageLinksView(
            owner_id=42, guild_id=gid,
            event_type="DS", event_date="2026-05-18",
            refs=refs,
        )

        # Fake interaction with a channel that 404s on fetch_message.
        channel = MagicMock()
        channel.fetch_message = AsyncMock(
            side_effect=discord.NotFound(MagicMock(status=404), "gone"),
        )
        inter = MagicMock()
        inter.user = MagicMock(); inter.user.id = 42
        inter.guild = MagicMock()
        inter.guild.get_channel_or_thread = MagicMock(return_value=channel)
        inter.response = MagicMock()
        inter.response.send_message = AsyncMock()

        await view.children[0].callback(inter)

        # Warning was surfaced.
        inter.response.send_message.assert_awaited_once()
        args, kwargs = inter.response.send_message.await_args
        assert kwargs.get("ephemeral") is True
        assert "no longer be found" in args[0] or "no longer be found" in kwargs.get("content", "")

        # Stale pointer pruned — second history open won't re-offer it.
        assert config.list_roster_image_refs(gid, "DS", "2026-05-18") == []

    @pytest.mark.asyncio
    async def test_owner_only_button_interaction_check(self, fake_env):
        from unittest.mock import AsyncMock, MagicMock
        fake, gid = fake_env
        refs = [{"team": "A", "channel_id": 900, "message_id": 1001}]
        view = sh._RosterImageLinksView(
            owner_id=42, guild_id=gid,
            event_type="DS", event_date="2026-05-18",
            refs=refs,
        )
        inter = MagicMock()
        inter.user = MagicMock(); inter.user.id = 999   # not the owner
        inter.response = MagicMock()
        inter.response.send_message = AsyncMock()

        allowed = await view.interaction_check(inter)
        assert allowed is False
        inter.response.send_message.assert_awaited_once()
