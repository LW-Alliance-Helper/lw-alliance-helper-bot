"""
Post-event attendance tracking (#133 — Step 7 of the #38 8-step flow).

Reached via the `📋 Record attendance` button on `/desertstorm` and
`/canyonstorm` (hub-restructure #187; legacy
`/desertstorm attendance event_date:YYYY-MM-DD` subcommand pre-#187).
Opens an officer view that lets leadership mark who actually showed
for each assigned slot. Writes one row per slot to the alliance's
configured `attendance_tab` Sheet.

Without this, the structured-flow data ([#129](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/129)) never closes the loop —
the bot knows who was *assigned* but not who *showed*. Blocks the
future no-show / priority-tagging feature and the [#56](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/56)
Member Stats Lookup's "attended N storms" calculation.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from typing import Optional

import discord

from messages import DENY_NOT_OWNER
from storm_event_hub import (
    HUB_COMMAND,
    HUB_BTN_VIEW_SIGNUPS,
    HUB_BTN_POST_SIGNUP,
    HUB_BTN_ATTENDANCE,
)

logger = logging.getLogger(__name__)


# ── Status codes + labels ────────────────────────────────────────────────────
#
# Decision #5 / Rule K (#171): the attendance UI is `✅ / ❌ / —` only.
# `STATUS_SUB_ACTIVATED` ("🔄 Sub activated") used to be a third option
# the officer could pick; it's dropped from the UI entirely. The Sheet
# may still carry legacy `sub_activated` rows from before the change —
# the reader tolerates them (they render as `—`) but the writer never
# produces them again. The constant + label are kept for that
# back-compat read path only.

STATUS_ATTENDED      = "attended"
STATUS_NO_SHOW       = "no_show"
STATUS_SUB_ACTIVATED = "sub_activated"  # legacy — read-only, never written
STATUS_UNRECORDED    = ""

_STATUS_LABELS = {
    STATUS_ATTENDED:      "✅ Attended",
    STATUS_NO_SHOW:       "❌ Did not attend",
    STATUS_SUB_ACTIVATED: "—",  # legacy renders as unrecorded
    STATUS_UNRECORDED:    "—",
}
# Statuses the officer can pick. `sub_activated` is read-only.
_VALID_STATUSES = (STATUS_ATTENDED, STATUS_NO_SHOW)


# ── Sheet I/O ────────────────────────────────────────────────────────────────
#
# Post-#245 the bot writes attendance to the unified `<DS|CS> Member
# Log` tab (#244) instead of the per-slot legacy attendance tab. The
# legacy schema is kept here as a reference for any officer reading
# the alliance's pre-cutover history; the bot no longer writes to it.

_LEGACY_ATTENDANCE_HEADER = [
    "Event Date", "Team", "Zone", "Member", "Status",
    "Recorded By", "Recorded At (UTC)",
]


def _attendance_tab_name(guild_id: int, event_type: str) -> str:
    import config
    structured = config.get_structured_storm_config(guild_id, event_type)
    return structured.get("attendance_tab") or config.default_structured_tab(
        event_type, "attendance_tab"
    )


def _rosters_tab_name(guild_id: int, event_type: str) -> str:
    import config
    structured = config.get_structured_storm_config(guild_id, event_type)
    return structured.get("rosters_tab") or config.default_structured_tab(
        event_type, "rosters_tab"
    )


# Tab open/create uses `config.get_or_create_worksheet` so every
# structured-flow tab (Sign-Ups, Rosters, Attendance, Strategies,
# Member Rules) auto-creates on first touch with the documented
# "The bot creates and maintains this tab if it doesn't exist."
# semantic. See config.get_or_create_worksheet for the shared helper.


def load_rostered_slots(
    guild_id: int, event_type: str, event_date: str,
) -> tuple[list[dict], list[str]]:
    """Read assigned slots for an event from rosters_tab. Returns
    `(slots, errors)`. Each slot is `{team, zone, member, discord_id,
    role}` — primary + sub rows both included so attendance can flag
    no-show subs the same way."""
    import config
    errors: list[str] = []
    try:
        sh = config.get_spreadsheet(guild_id)
    except Exception as e:
        return [], [f"spreadsheet open failed: {e}"]
    if sh is None:
        return [], ["spreadsheet not configured"]

    tab = _rosters_tab_name(guild_id, event_type)
    if not tab:
        return [], ["no rosters tab configured"]

    try:
        ws = config.get_or_create_worksheet(sh, tab)
    except Exception as e:
        return [], [f"rosters tab open failed: {e}"]

    try:
        values = ws.get_all_values()
    except Exception as e:
        return [], [f"rosters read failed: {e}"]

    if not values or len(values) < 2:
        return [], []

    header = [c.strip() for c in values[0]]

    def _col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            return -1

    date_col = _col("Event Date")
    team_col = _col("Team")
    zone_col = _col("Zone")
    member_col = _col("Member")
    role_col = _col("Role")
    id_col = _col("Discord ID")
    override_col = _col("Override Below Minimum")
    # Legacy header alias: dev/staging sheets created before the
    # Rule B header rename still carry "Override Below Floor". Fall
    # through so existing flagged rows continue to render correctly
    # until the next rosters_tab write triggers the header migration
    # in storm_roster_builder._write_rosters_tab.
    if override_col < 0:
        override_col = _col("Override Below Floor")

    # Truthy values for the override column. Officers occasionally edit
    # the Sheet by hand — accept the usual yes-set rather than only the
    # literal "yes" that the bot writes. Matches the set used by
    # `storm_officer_view._read_roster_rows` + `storm_roster_builder
    # ._read_roster_powers` so an officer who writes the same literal
    # in either Sheet gets the same interpretation.
    truthy = {"yes", "y", "1", "true", "t", "x"}

    slots: list[dict] = []
    # Dedupe by `(team, zone, member)` so a phase-aware preset with a
    # member playing the same zone across multiple phases (the typical
    # "Alice stays at Power Tower for all phases" case) shows up
    # ONCE in the attendance picker. Phase-migration members (Alice
    # P1 Power Tower → P2 Nuclear Silo) remain as two separate slots
    # because their (team, zone) keys differ — officers can mark
    # attendance per zone, useful for alliances that track partial
    # attendance across phase transitions. Attendance itself stays
    # phase-blind per #152 spec ("phase-specific attendance" is out
    # of scope).
    seen: set[tuple[str, str, str]] = set()
    for row in values[1:]:
        def _cell(idx: int) -> str:
            return row[idx].strip() if 0 <= idx < len(row) else ""
        if _cell(date_col) != event_date:
            continue
        member = _cell(member_col)
        if not member:
            continue
        key = (_cell(team_col), _cell(zone_col), member)
        if key in seen:
            continue
        seen.add(key)
        slots.append({
            "team":       _cell(team_col),
            "zone":       _cell(zone_col),
            "member":     member,
            "discord_id": _cell(id_col),
            "role":       _cell(role_col) or "primary",
            "override_below_floor": _cell(override_col).lower() in truthy,
        })
    return slots, errors


def load_attendance(
    guild_id: int, event_type: str, event_date: str,
    *,
    slots: Optional[list[dict]] = None,
) -> tuple[dict[tuple[str, str, str], dict], list[str]]:
    """Load existing attendance for an event so re-running the picker
    pre-fills with what's already recorded. Returns
    `({(team, zone, member): row}, errors)` keyed for fast lookup.

    Post-#245 source: the unified `DS Member Log` / `CS Member Log`
    tab. The Member Log is keyed by `(event_date, member)` with a
    `showed_up` column; this function expands each member's flag back
    across their assigned slots so the picker UI can pre-check.

    `slots` is the rostered-slot list for this event (from
    `load_rostered_slots`). Without it, no expansion is possible and
    the function returns an empty dict.

    Pre-cutover events (those whose attendance was recorded against
    the legacy `DS Attendance` / `CS Attendance` tab) will not
    pre-fill — per the #245 ticket this is a clean cutover with no
    backfill. Officers re-recording an old event will see "unrecorded"
    for every slot and can re-mark as needed; the legacy tab data
    remains visible on the Sheet for reference.
    """
    import storm_log

    errors: list[str] = []
    if not slots:
        return {}, errors

    try:
        member_flags = _read_member_log_for_date(
            guild_id, event_type, event_date,
        )
    except Exception as e:
        return {}, [f"member-log read failed: {e}"]

    if not member_flags:
        return {}, errors

    flag_to_status = {"yes": STATUS_ATTENDED, "no": STATUS_NO_SHOW}
    out: dict[tuple[str, str, str], dict] = {}
    for slot in slots:
        member = slot.get("member") or ""
        if not member:
            continue
        flag = member_flags.get(member, "")
        status = flag_to_status.get(flag)
        if not status:
            continue
        key = (slot.get("team", ""), slot.get("zone", ""), member)
        out[key] = {
            "status":       status,
            "recorded_by":  "",   # Audit fields no longer captured.
            "recorded_at":  "",
        }
    _ = storm_log  # storm_log import kept for future audit-log hooks
    return out, errors


def _read_member_log_for_date(
    guild_id: int, event_type: str, event_date: str,
) -> dict[str, str]:
    """Read the `showed_up` column for the given event from the
    Per-Member Log tab. Returns `{member: flag}` where flag is
    `"yes"` / `"no"` / `""`. Empty dict when the tab doesn't exist
    or has no rows for this date."""
    import config
    import storm_log

    try:
        sh = config.get_spreadsheet(guild_id)
    except Exception:
        return {}
    if sh is None:
        return {}

    tab = storm_log._member_log_tab_name(event_type)
    try:
        ws = sh.worksheet(tab)
    except Exception:
        return {}
    try:
        all_values = ws.get_all_values()
    except Exception:
        return {}
    if not all_values or len(all_values) < 2:
        return {}

    header = all_values[0]
    if len(header) < 2 or header[:2] != ["Event Date", "Member"]:
        return {}
    try:
        col_idx = header.index(storm_log.ATTENDANCE_QUESTION_KEY)
    except ValueError:
        return {}

    out: dict[str, str] = {}
    for row in all_values[1:]:
        if len(row) < 2:
            continue
        if row[0] != event_date:
            continue
        member = row[1]
        if not member:
            continue
        out[member] = row[col_idx] if col_idx < len(row) else ""
    return out


def _collapse_slot_statuses_to_member_flag(
    statuses: dict[tuple[str, str, str], str],
) -> dict[str, str]:
    """Roll up per-slot attendance statuses to one `showed_up` value
    per member (#245).

    The attendance UI is per-slot (a member playing two zones across
    teams has two status entries), but the Per-Member Log keys rows
    by member. Aggregation rule:
      - `attended` on ANY slot → "yes"
      - everything else (no_show, unrecorded, legacy `sub_activated`)
        → "" (empty)

    The asymmetry is deliberate. A member marked `no_show` may
    actually have *sat out* — the officer didn't unassign them in the
    roster builder, so they appear in the attendance picker, but
    they weren't really registered to play. Writing "no" for that
    case inflates the no-show count in Trends Viewer queries like
    "How many times did members not show up?" — the count includes
    sit-outs as no-shows, which isn't what the officer is asking.
    Leaving the cell blank for no_show / unrecorded keeps the
    showed_up column as a presence indicator: `yes` = confirmed
    attended; blank = either didn't attend, sat out, or wasn't
    recorded. Trends queries against showed_up should use `< 1` or
    similar to find members who never had a `yes` in the window.

    Result feeds `storm_log.upsert_member_log_rows` with one entry per
    member found in `statuses`.
    """
    by_member: dict[str, list[str]] = {}
    for (_team, _zone, member), status in statuses.items():
        if not member:
            continue
        by_member.setdefault(member, []).append(status or "")
    flags: dict[str, str] = {}
    for member, vals in by_member.items():
        if any(v == "attended" for v in vals):
            flags[member] = "yes"
        else:
            flags[member] = ""
    return flags


def save_attendance(
    guild_id: int, event_type: str, event_date: str,
    *,
    statuses: dict[tuple[str, str, str], str],
    officer_id: int,
    prior_existing: Optional[dict[tuple[str, str, str], dict]] = None,
) -> list[str]:
    """Persist this event's attendance for the Trends Viewer (#246).

    Per #245 this writes ONLY to the Per-Member Log tab — the legacy
    per-slot `DS Attendance` / `CS Attendance` tab is no longer
    appended to by the bot. Existing data on the legacy tab is
    preserved (officers can still read it from the Sheet), but new
    attendance lands in the unified `DS Member Log` / `CS Member Log`
    so the Trends Viewer can include attendance counts alongside the
    other per-member questions configured in #244.

    Status aggregation collapses per-slot rows to one per-member
    `showed_up` value (see `_collapse_slot_statuses_to_member_flag`).
    `prior_existing` is unused after the cutover — the upsert in
    `storm_log.upsert_member_log_rows` replaces rows for this
    (event_date, member) cleanly on every save.

    Returns soft errors (empty on success).
    """
    import storm_log

    errors: list[str] = []
    flags = _collapse_slot_statuses_to_member_flag(statuses)
    if not flags:
        # Nothing to record — UI surfaces this as "no slots recorded".
        return errors

    per_member_data = {
        member: {storm_log.ATTENDANCE_QUESTION_KEY: value}
        for member, value in flags.items()
    }

    try:
        storm_log.upsert_member_log_rows(
            guild_id, event_type, event_date,
            per_member_data,
            [storm_log.ATTENDANCE_QUESTION_KEY],
        )
    except Exception as e:
        errors.append(f"member-log write failed (prior data intact): {e}")
        return errors

    recorded_count = sum(1 for v in flags.values() if v)
    logger.info(
        "[STORM ATTENDANCE] %d member(s) recorded to Member Log "
        "(guild=%s event=%s/%s)",
        recorded_count, guild_id, event_type, event_date,
    )
    # `officer_id` and `prior_existing` no longer drive the Sheet
    # write (the legacy `recorded_by` audit column rides on the now-
    # frozen legacy tab). Officer attribution moves to bot logs.
    _ = officer_id, prior_existing
    return errors


# ── Officer view ─────────────────────────────────────────────────────────────


class _AttendanceSession:
    """In-memory state for one officer's attendance recording session."""

    def __init__(
        self,
        *,
        guild_id: int,
        user_id: int,
        event_type: str,
        event_date: str,
        slots: list[dict],
        existing: dict[tuple[str, str, str], dict],
    ):
        self.guild_id   = guild_id
        self.user_id    = user_id
        self.event_type = event_type
        self.event_date = event_date
        self.slots      = slots
        # statuses keyed by (team, zone, member) → status code.
        self.statuses: dict[tuple[str, str, str], str] = {}
        for slot in slots:
            key = (slot["team"], slot["zone"], slot["member"])
            prior = existing.get(key)
            self.statuses[key] = prior.get("status") if prior else STATUS_UNRECORDED
        # Snapshot of attendance rows present when this session opened.
        # Used by save_attendance to (a) preserve recorded_by/recorded_at
        # for rows the officer didn't actually edit and (b) carry forward
        # any prior rows whose slot is no longer in `slots` (roster edit
        # between attendance sessions).
        self.existing  = dict(existing)
        self.page      = 0
        self.per_page  = 25  # Discord Select option limit.

    def slot_key(self, slot: dict) -> tuple[str, str, str]:
        return (slot["team"], slot["zone"], slot["member"])

    def total_pages(self) -> int:
        if not self.slots:
            return 1
        return (len(self.slots) + self.per_page - 1) // self.per_page

    def page_slots(self) -> list[dict]:
        start = self.page * self.per_page
        return self.slots[start:start + self.per_page]

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in _VALID_STATUSES}
        out[STATUS_UNRECORDED] = 0
        for status in self.statuses.values():
            out[status] = out.get(status, 0) + 1
        return out


def _render_embed(session: _AttendanceSession) -> discord.Embed:
    from storm_date_helpers import format_event_date

    label = "Desert Storm" if session.event_type == "DS" else "Canyon Storm"
    embed = discord.Embed(
        title=f"📋 {label} Attendance: {format_event_date(session.event_date)}",
        color=discord.Color.gold() if session.event_type == "DS"
              else discord.Color.orange(),
    )

    if not session.slots:
        hub_cmd = HUB_COMMAND[session.event_type]
        embed.description = (
            f"_No roster slots found for this event. Run `{hub_cmd}` "
            f"and click **{HUB_BTN_VIEW_SIGNUPS}** to build a structured "
            f"roster first; attendance only applies to structured-flow "
            f"rosters._"
        )
        return embed

    # Group by team for readability. Sort team keys alphabetically so
    # rendering is stable across runs (DS / CS-teams=both → A, B;
    # single-team config → A or B; legacy pre-#166 CS data → "").
    teams: dict[str, list[dict]] = {}
    for slot in session.slots:
        teams.setdefault(slot["team"] or "(no team)", []).append(slot)

    from storm_icons import zone_emoji_prefix
    lines: list[str] = []
    for team in sorted(teams.keys()):
        team_slots = teams[team]
        lines.append(f"\n**Team {team}**" if team and team != "(no team)" else "\n**Roster**")
        for slot in team_slots:
            status = session.statuses.get(session.slot_key(slot), STATUS_UNRECORDED)
            label_status = _STATUS_LABELS.get(status, "—")
            zone = slot.get("zone") or ""
            # #158: prefix the zone in the per-slot row with its emoji
            # icon — no-op until the emojis upload. Sub slots have an
            # empty zone and just show "(sub)".
            zone_part = f" ({zone_emoji_prefix(zone)}{zone})" if zone else " (sub)"
            role_marker = " 🪑" if slot.get("role") == "sub" else ""
            # Decision #6 (#171): the Override Below Minimum glyph + the
            # trailing "Assigned below the zone minimum at build time"
            # line are dropped from the attendance UI. The Sheet still
            # records the flag for post-event audit, but officers
            # recording attendance don't need it surfaced.
            lines.append(
                f"{label_status} {slot['member']}{zone_part}{role_marker}"
            )

    # Rule K (#171): footer counts collapse from ✅ N · ❌ N · 🔄 N · — N
    # to ✅ N · ❌ N · — N. Any legacy `sub_activated` rows roll into
    # the unrecorded bucket since the UI renders them as `—`.
    counts = session.counts()
    unrecorded = (
        counts.get(STATUS_UNRECORDED, 0)
        + counts.get(STATUS_SUB_ACTIVATED, 0)
    )
    summary = (
        f"✅ {counts.get(STATUS_ATTENDED, 0)}  ·  "
        f"❌ {counts.get(STATUS_NO_SHOW, 0)}  ·  "
        f"— {unrecorded}"
    )
    embed.description = "\n".join(lines)
    embed.set_footer(text=summary)
    return embed


class _AttendanceView(discord.ui.View):
    """Member-select + ✅/❌ + Save (#171 / Decision #5).

    The view's two action buttons branch on whether a slot is currently
    selected in the dropdown:
      - No selection: `[✅ Mark all unrecorded as attended]` /
        `[❌ Mark all unrecorded as did not attend]` bulk-apply to every
        slot whose status is `STATUS_UNRECORDED`.
      - With selection: `[✅ Mark as attended]` /
        `[❌ Mark as did not attend]` write directly to the picked slot.

    Empty-state (no slots) hides the action buttons entirely — there's
    nothing to mark, and surfacing dead buttons would be misleading.

    The pre-#171 status-picker ephemeral (`_StatusPickerView`) is gone;
    the three-state pick (✅/❌/🔄) shrinks to a two-state in-place
    action because 🔄 Sub activated was dropped from the UI.
    """

    def __init__(self, session: _AttendanceSession):
        super().__init__(timeout=900)
        self.session = session
        self.selected_key: Optional[tuple[str, str, str]] = None
        self.message: Optional[discord.Message] = None
        self._build()

    def _selected_slot_label(self) -> str:
        """Member + zone for the currently-selected slot, or empty if
        none is selected. Used by the action button labels so the
        officer can see which slot they're about to mark."""
        if self.selected_key is None:
            return ""
        _team, _zone, member = self.selected_key
        return member

    def _build(self):
        self.clear_items()
        s = self.session
        slots = s.page_slots()

        if not slots:
            # Empty state — no action buttons. The render_embed body
            # already explains why and how to fix.
            return

        # Row 0 — slot select.
        options = []
        for slot in slots:
            key = s.slot_key(slot)
            cur = s.statuses.get(key, STATUS_UNRECORDED)
            label = f"{slot['member']}: {slot['zone'] or 'sub'}"
            desc = f"current: {_STATUS_LABELS.get(cur, '—')}"
            # Stable value: encoded as team|zone|member; max 100 chars.
            value = f"{slot['team']}|{slot['zone']}|{slot['member']}"[:100]
            options.append(discord.SelectOption(
                label=label[:100],
                value=value,
                description=desc[:100],
                default=(key == self.selected_key),
            ))
        picker = discord.ui.Select(
            placeholder=(
                f"Picked: {self._selected_slot_label()}"
                if self.selected_key else
                "Pick a slot to record attendance "
                "(or use the bulk-mark buttons below)…"
            ),
            min_values=1, max_values=1,
            options=options, row=0,
        )

        async def _on_pick(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            raw = picker.values[0]
            parts = raw.split("|", 2)
            if len(parts) != 3:
                await inter.response.send_message(
                    "⚠️ Internal error: couldn't parse slot key.",
                    ephemeral=True,
                )
                return
            self.selected_key = (parts[0], parts[1], parts[2])
            await self._redraw(inter)

        picker.callback = _on_pick
        self.add_item(picker)

        # Row 1 — ✅ / ❌ action buttons. Labels and behaviour branch on
        # whether a slot is currently selected in the picker.
        if self.selected_key is not None:
            mark_attended_label = "✅ Mark as attended"
            mark_no_show_label = "❌ Mark as did not attend"
        else:
            mark_attended_label = "✅ Mark all unrecorded as attended"
            mark_no_show_label = "❌ Mark all unrecorded as did not attend"

        attended_btn = discord.ui.Button(
            label=mark_attended_label,
            style=discord.ButtonStyle.success, row=1,
        )
        attended_btn.callback = self._make_mark_callback(STATUS_ATTENDED)
        self.add_item(attended_btn)

        no_show_btn = discord.ui.Button(
            label=mark_no_show_label,
            style=discord.ButtonStyle.danger, row=1,
        )
        no_show_btn.callback = self._make_mark_callback(STATUS_NO_SHOW)
        self.add_item(no_show_btn)

        # Row 2 — Clear (only enabled with a selection) so officers can
        # walk back a mistake on the currently-picked slot.
        clear_btn = discord.ui.Button(
            label="↩️ Clear selection",
            style=discord.ButtonStyle.secondary, row=2,
            disabled=self.selected_key is None,
        )

        async def _on_clear(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            if self.selected_key is not None:
                s.statuses[self.selected_key] = STATUS_UNRECORDED
            self.selected_key = None
            await self._redraw(inter)

        clear_btn.callback = _on_clear
        self.add_item(clear_btn)

        # Row 3 — pagination (only rendered when total slots > one page).
        if s.total_pages() > 1:
            prev_btn = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary, row=3,
                disabled=s.page == 0,
            )
            next_btn = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.secondary, row=3,
                disabled=s.page >= s.total_pages() - 1,
            )

            async def _prev(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                s.page = max(0, s.page - 1)
                self.selected_key = None
                await self._redraw(inter)

            async def _next(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                s.page = min(s.total_pages() - 1, s.page + 1)
                self.selected_key = None
                await self._redraw(inter)

            prev_btn.callback = _prev
            next_btn.callback = _next
            self.add_item(prev_btn)
            self.add_item(next_btn)

        # Row 4 — Save.
        save_btn = discord.ui.Button(
            label="💾 Save attendance",
            style=discord.ButtonStyle.primary, row=4,
        )
        save_btn.callback = self._on_save
        self.add_item(save_btn)

    def _make_mark_callback(self, status: str):
        async def _cb(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            s = self.session
            if self.selected_key is not None:
                # Single-slot write.
                s.statuses[self.selected_key] = status
                # Clear selection so the officer's next click goes to
                # the bulk-mode buttons by default — this matches the
                # "advance through the list" workflow.
                self.selected_key = None
            else:
                # Bulk-mark every unrecorded slot.
                for key, current in s.statuses.items():
                    if not current:
                        s.statuses[key] = status
            await self._redraw(inter)
        return _cb

    async def _on_save(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        s = self.session
        await inter.response.defer(ephemeral=True, thinking=True)
        errors = await asyncio.to_thread(
            save_attendance,
            s.guild_id, s.event_type, s.event_date,
            statuses=s.statuses,
            officer_id=inter.user.id,
            prior_existing=s.existing,
        )
        counts = s.counts()
        if errors:
            await inter.followup.send(
                "⚠️ Attendance partially saved: " + errors[0],
                ephemeral=True,
            )
            return
        recorded = (
            counts.get(STATUS_ATTENDED, 0)
            + counts.get(STATUS_NO_SHOW, 0)
        )
        from storm_date_helpers import format_event_date
        await inter.followup.send(
            f"✅ Saved attendance for **{format_event_date(s.event_date)}**: "
            f"{recorded} slot(s) recorded "
            f"(✅ {counts.get(STATUS_ATTENDED, 0)}, "
            f"❌ {counts.get(STATUS_NO_SHOW, 0)}).",
            ephemeral=True,
        )
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        self.stop()

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.session.user_id:
            await inter.response.send_message(DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _redraw(self, inter: discord.Interaction):
        self._build()
        await inter.response.edit_message(
            embed=_render_embed(self.session), view=self,
        )

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ── Slash command ────────────────────────────────────────────────────────────


# ── Hub button handler ───────────────────────────────────────────────────────
#
# Wired from the `📋 Record attendance` button on the `/desertstorm`
# and `/canyonstorm` event hubs (storm_event_hub.py). This module
# exposes the handler body so the hub stays a thin dispatcher.


async def handle_storm_attendance(
    bot,
    interaction: discord.Interaction,
    event_type: str,
    event_date: Optional[str] = None,
) -> None:
    from storm_permissions import (
        is_leader_or_admin, deny_non_leader, ensure_premium_structured,
    )
    from storm_date_helpers import (
        parse_event_date, most_recent_event_date, format_event_date,
    )
    if not is_leader_or_admin(interaction):
        await deny_non_leader(interaction)
        return

    et = event_type
    hub_cmd = HUB_COMMAND[et]
    raw_input = (event_date or "").strip()
    if not raw_input:
        date_clean = most_recent_event_date(interaction.guild_id, et)
        if not date_clean:
            await interaction.response.send_message(
                f"⚠️ No posted {('Desert Storm' if et == 'DS' else 'Canyon Storm')} "
                f"events on record. Run `{hub_cmd}` and click "
                f"**{HUB_BTN_POST_SIGNUP}** to build a roster before "
                f"recording attendance, or pass `event_date` explicitly.",
                ephemeral=True,
            )
            return
    else:
        parsed = parse_event_date(raw_input)
        if parsed is None:
            await interaction.response.send_message(
                f"⚠️ `{event_date}` isn't a date I can parse. Try `May 18`, "
                f"`5/18`, `2026-05-18`, `yesterday`, or `today`.",
                ephemeral=True,
            )
            return
        date_clean = parsed.isoformat()

    ok, _structured = await ensure_premium_structured(
        interaction, et,
        bot=bot,
        feature_label=f"the **{HUB_BTN_ATTENDANCE}** button on `{hub_cmd}`",
    )
    if not ok:
        return

    await interaction.response.defer(thinking=True)

    # gspread reads off the event loop. Post-#245 the attendance read
    # needs the slots list to expand Member Log flags back into the
    # per-slot picker shape, so the rosters read sequences before the
    # member-log read (one extra round-trip per officer command — the
    # ~30-member alliance overhead is negligible).
    slots, slot_errors = await asyncio.to_thread(
        load_rostered_slots, interaction.guild_id, et, date_clean,
    )
    existing, attendance_errors = await asyncio.to_thread(
        load_attendance,
        interaction.guild_id, et, date_clean,
        slots=slots,
    )

    if not slots:
        msg_lines = [
            f"⚠️ No structured roster found for **{format_event_date(date_clean)}** "
            f"({'Desert Storm' if et == 'DS' else 'Canyon Storm'})."
        ]
        if slot_errors:
            msg_lines.append("Details: " + slot_errors[0])
        else:
            msg_lines.append(
                "Attendance is only recordable for events with a "
                f"structured roster posted via the **{HUB_BTN_VIEW_SIGNUPS}** "
                f"button on `{hub_cmd}`."
            )
        await interaction.followup.send("\n".join(msg_lines), ephemeral=True)
        return

    session = _AttendanceSession(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        event_type=et,
        event_date=date_clean,
        slots=slots,
        existing=existing,
    )
    view = _AttendanceView(session)
    embed = _render_embed(session)
    content = None
    if attendance_errors:
        logger.warning(
            "[STORM ATTENDANCE] attendance read errors for guild=%s: %s",
            interaction.guild_id, "; ".join(attendance_errors),
        )
        content = (
            "⚠️ Read existing attendance had issues. See bot logs. "
            "You can still record fresh entries below."
        )
    msg = await interaction.followup.send(content=content, embed=embed, view=view)
    view.message = msg
