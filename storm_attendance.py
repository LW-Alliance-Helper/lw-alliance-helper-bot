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

_ATTENDANCE_HEADER = [
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
) -> tuple[dict[tuple[str, str, str], dict], list[str]]:
    """Load existing attendance rows for an event so re-running the
    command pre-fills the picker. Returns
    `({(team, zone, member): row}, errors)` keyed for fast lookup."""
    import config
    errors: list[str] = []
    try:
        sh = config.get_spreadsheet(guild_id)
    except Exception as e:
        return {}, [f"spreadsheet open failed: {e}"]
    if sh is None:
        return {}, []

    tab = _attendance_tab_name(guild_id, event_type)
    if not tab:
        return {}, []
    try:
        ws = config.get_or_create_worksheet(sh, tab, header_row=_ATTENDANCE_HEADER)
    except Exception:
        return {}, []  # tab create/open failed → no existing attendance

    try:
        values = ws.get_all_values()
    except Exception as e:
        return {}, [f"attendance read failed: {e}"]

    if not values or len(values) < 2:
        return {}, []

    header = [c.strip() for c in values[0]]

    def _col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            return -1

    date_col   = _col("Event Date")
    team_col   = _col("Team")
    zone_col   = _col("Zone")
    member_col = _col("Member")
    status_col = _col("Status")
    by_col     = _col("Recorded By")
    at_col     = _col("Recorded At (UTC)")

    out: dict[tuple[str, str, str], dict] = {}
    for row in values[1:]:
        def _cell(idx: int) -> str:
            return row[idx].strip() if 0 <= idx < len(row) else ""
        if _cell(date_col) != event_date:
            continue
        key = (_cell(team_col), _cell(zone_col), _cell(member_col))
        out[key] = {
            "status":       _cell(status_col),
            "recorded_by":  _cell(by_col),
            "recorded_at":  _cell(at_col),
        }
    return out, errors


def save_attendance(
    guild_id: int, event_type: str, event_date: str,
    *,
    statuses: dict[tuple[str, str, str], str],
    officer_id: int,
    prior_existing: Optional[dict[tuple[str, str, str], dict]] = None,
) -> list[str]:
    """Replace this event's attendance rows on the Sheet with the
    officer's current state. Returns soft errors (empty on success).

    Write strategy is write-then-blank-trailing (atomic-ish), NOT
    clear-then-write. The prior implementation called `ws.clear()`
    before `ws.update(...)`; if the update raised (rate limit, 5xx,
    token expiry), the alliance lost their ENTIRE attendance history.

    Write order now:
      1. Read existing values (already done above).
      2. Compose the new full payload in memory.
      3. `ws.update("A1", kept, ...)` — overwrites in place.
      4. If the new payload is shorter than the old, blank the trailing
         rows.
    A failure between (3) and (4) leaves stale data in trailing rows but
    the alliance's primary attendance history is intact.

    `prior_existing` (if provided) is the snapshot of attendance rows
    loaded at view-open time. Slots that were recorded then but are no
    longer in `statuses` (because the underlying roster changed) are
    CARRIED FORWARD so a roster edit between attendance sessions doesn't
    silently drop prior recorded attendance.

    `recorded_by` semantics: preserved from `prior_existing` when the
    status is unchanged (so officer B saving without editing doesn't
    overwrite officer A's audit row); current officer when the status
    was edited or is new.
    """
    import config
    from config import _utcnow_iso

    errors: list[str] = []
    try:
        sh = config.get_spreadsheet(guild_id)
    except Exception as e:
        return [f"spreadsheet open failed: {e}"]
    if sh is None:
        return ["spreadsheet not configured"]

    tab = _attendance_tab_name(guild_id, event_type)
    if not tab:
        return ["no attendance tab configured"]

    ws = config.get_or_create_worksheet(sh, tab, header_row=_ATTENDANCE_HEADER)

    try:
        all_values = ws.get_all_values()
    except Exception as e:
        return [f"attendance read-before-write failed: {e}"]

    header = all_values[0] if all_values else list(_ATTENDANCE_HEADER)
    date_idx = 0  # Always col 0 by convention; check header for safety
    if header and "Event Date" in header:
        date_idx = header.index("Event Date")
    team_idx   = header.index("Team")   if "Team"   in header else 1
    zone_idx   = header.index("Zone")   if "Zone"   in header else 2
    member_idx = header.index("Member") if "Member" in header else 3

    old_row_count = len(all_values)
    kept = [header]
    for row in all_values[1:] if all_values else []:
        if row and len(row) > date_idx and str(row[date_idx]).strip() == event_date:
            continue
        kept.append(row)

    # Carry forward any prior attendance rows whose slot isn't in the
    # current `statuses` — that's a roster-edit-after-attendance case.
    # Without this, removing a slot in the roster silently drops the
    # recorded attendance for that slot.
    prior = prior_existing or {}
    carried_forward = 0
    for prior_key, prior_row in prior.items():
        if prior_key in statuses:
            continue
        if not prior_row.get("status"):
            continue
        team, zone, member = prior_key
        kept.append([
            event_date, team, zone, member,
            prior_row["status"],
            prior_row.get("recorded_by") or "",
            prior_row.get("recorded_at") or "",
        ])
        carried_forward += 1

    recorded_at = _utcnow_iso()
    for (team, zone, member), status in statuses.items():
        if not status:
            continue  # Skip unrecorded slots.
        # Preserve recorded_by + recorded_at when the status is unchanged
        # so a second officer's save doesn't silently rewrite the first
        # officer's audit row.
        prior_row = prior.get((team, zone, member))
        if prior_row and prior_row.get("status") == status:
            row_recorded_by = prior_row.get("recorded_by") or str(officer_id)
            row_recorded_at = prior_row.get("recorded_at") or recorded_at
        else:
            row_recorded_by = str(officer_id)
            row_recorded_at = recorded_at
        kept.append([
            event_date, team, zone, member, status,
            row_recorded_by, row_recorded_at,
        ])

    # Atomic-ish write: overwrite in place, then blank the trailing rows
    # that the new payload didn't reach.
    try:
        ws.update("A1", kept, value_input_option="RAW")
    except Exception as e:
        # The Sheet is untouched if the update raised before any write
        # — prior history is intact.
        errors.append(f"attendance write failed (prior data intact): {e}")
        return errors

    new_row_count = len(kept)
    if new_row_count < old_row_count:
        try:
            blanks = [[""] * len(header) for _ in range(old_row_count - new_row_count)]
            ws.update(
                f"A{new_row_count + 1}", blanks, value_input_option="RAW",
            )
        except Exception as e:
            # Soft error — the new data is written; only stale trailing
            # rows remain, which is recoverable on the next save.
            errors.append(
                f"attendance trailing-blank failed (new data written, "
                f"stale rows {new_row_count + 1}..{old_row_count} may remain): {e}"
            )

    if carried_forward:
        logger.info(
            "[STORM ATTENDANCE] carried forward %d attendance row(s) for "
            "slots removed from current roster (guild=%s event=%s/%s)",
            carried_forward, guild_id, event_type, event_date,
        )
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
        title=f"📋 {label} Attendance — {format_event_date(session.event_date)}",
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
            label = f"{slot['member']} — {slot['zone'] or 'sub'}"
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
            await self._refresh(inter)

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
            await self._refresh(inter)

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
                await self._refresh(inter)

            async def _next(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                s.page = min(s.total_pages() - 1, s.page + 1)
                self.selected_key = None
                await self._refresh(inter)

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
            await self._refresh(inter)
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
                "⚠️ Attendance partially saved — " + errors[0],
                ephemeral=True,
            )
            return
        recorded = (
            counts.get(STATUS_ATTENDED, 0)
            + counts.get(STATUS_NO_SHOW, 0)
        )
        from storm_date_helpers import format_event_date
        await inter.followup.send(
            f"✅ Saved attendance for **{format_event_date(s.event_date)}** — "
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
            await inter.response.send_message(
                "⛔ Only the officer who opened this view can record "
                "attendance.", ephemeral=True,
            )
            return False
        return True

    async def _refresh(self, inter: discord.Interaction):
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

    # gspread reads off the event loop. Two parallel Sheet fetches
    # in one go via `asyncio.gather` so the user-facing wait is one
    # round-trip, not two stacked sequentially.
    slots_task = asyncio.to_thread(
        load_rostered_slots, interaction.guild_id, et, date_clean,
    )
    attendance_task = asyncio.to_thread(
        load_attendance, interaction.guild_id, et, date_clean,
    )
    (slots, slot_errors), (existing, attendance_errors) = await asyncio.gather(
        slots_task, attendance_task,
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
            "⚠️ Read existing attendance had issues — see bot logs. "
            "You can still record fresh entries below."
        )
    msg = await interaction.followup.send(content=content, embed=embed, view=view)
    view.message = msg
