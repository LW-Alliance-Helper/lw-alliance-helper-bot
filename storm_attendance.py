"""
Post-event attendance tracking (#133 — Step 7 of the #38 8-step flow).

`/storm_attendance event_type:DS|CS event_date:YYYY-MM-DD` opens an
officer view that lets leadership mark who actually showed for each
assigned slot. Writes one row per slot to the alliance's configured
`attendance_tab` Sheet.

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
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


# ── Status codes + labels ────────────────────────────────────────────────────

STATUS_ATTENDED      = "attended"
STATUS_NO_SHOW       = "no_show"
STATUS_SUB_ACTIVATED = "sub_activated"
STATUS_UNRECORDED    = ""

_STATUS_LABELS = {
    STATUS_ATTENDED:      "✅ Attended",
    STATUS_NO_SHOW:       "❌ No-show",
    STATUS_SUB_ACTIVATED: "🔄 Sub activated",
    STATUS_UNRECORDED:    "—",
}
_VALID_STATUSES = (STATUS_ATTENDED, STATUS_NO_SHOW, STATUS_SUB_ACTIVATED)


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


def _open_or_create_tab(sh, tab_name: str, header: list[str]):
    """Return the worksheet, creating it (with header) if missing."""
    try:
        return sh.worksheet(tab_name)
    except Exception:
        ws = sh.add_worksheet(title=tab_name, rows=2000, cols=max(8, len(header)))
        ws.append_row(header, value_input_option="RAW")
        return ws


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
        ws = sh.worksheet(tab)
    except Exception:
        return [], [f"rosters tab '{tab}' doesn't exist yet — post a "
                    f"structured roster first via /storm_signups"]

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
    override_col = _col("Override Below Floor")

    # Truthy values for the override column. Officers occasionally edit
    # the Sheet by hand — accept the usual yes-set rather than only the
    # literal "yes" that the bot writes. Matches the set used by
    # `storm_officer_view._read_roster_rows` + `storm_roster_builder
    # ._read_roster_powers` so an officer who writes the same literal
    # in either Sheet gets the same interpretation.
    truthy = {"yes", "y", "1", "true", "t", "x"}

    slots: list[dict] = []
    for row in values[1:]:
        def _cell(idx: int) -> str:
            return row[idx].strip() if 0 <= idx < len(row) else ""
        if _cell(date_col) != event_date:
            continue
        member = _cell(member_col)
        if not member:
            continue
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
        ws = sh.worksheet(tab)
    except Exception:
        return {}, []  # tab not yet created → no existing attendance

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

    ws = _open_or_create_tab(sh, tab, _ATTENDANCE_HEADER)

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
    label = "Desert Storm" if session.event_type == "DS" else "Canyon Storm"
    embed = discord.Embed(
        title=f"📋 {label} Attendance — {session.event_date}",
        color=discord.Color.gold() if session.event_type == "DS"
              else discord.Color.orange(),
    )

    if not session.slots:
        embed.description = (
            "_No roster slots found for this event. Run `/storm_signups` "
            "and build a structured roster first; attendance only applies "
            "to structured-flow rosters._"
        )
        return embed

    # Group by team for readability. Sort teams alphabetically so CS
    # (single "" key) and DS (A, B) render predictably across runs.
    teams: dict[str, list[dict]] = {}
    for slot in session.slots:
        teams.setdefault(slot["team"] or "(no team)", []).append(slot)

    has_overrides = any(slot.get("override_below_floor") for slot in session.slots)

    lines: list[str] = []
    for team in sorted(teams.keys()):
        team_slots = teams[team]
        lines.append(f"\n**Team {team}**" if team and team != "(no team)" else "\n**Roster**")
        for slot in team_slots:
            status = session.statuses.get(session.slot_key(slot), STATUS_UNRECORDED)
            label_status = _STATUS_LABELS.get(status, "?")
            zone_part = f" ({slot['zone']})" if slot.get("zone") else " (sub)"
            role_marker = " 🪑" if slot.get("role") == "sub" else ""
            # Surface the audit-trail flag from the rosters_tab so
            # leadership sees which assignments were below-floor at the
            # build time when recording who showed.
            override_marker = " ⚠️" if slot.get("override_below_floor") else ""
            lines.append(
                f"{label_status} {slot['member']}{zone_part}{role_marker}{override_marker}"
            )
    if has_overrides:
        lines.append("\n_⚠️ Assigned below the zone floor at build time._")

    counts = session.counts()
    summary = (
        f"✅ {counts.get(STATUS_ATTENDED, 0)}  ·  "
        f"❌ {counts.get(STATUS_NO_SHOW, 0)}  ·  "
        f"🔄 {counts.get(STATUS_SUB_ACTIVATED, 0)}  ·  "
        f"— {counts.get(STATUS_UNRECORDED, 0)}"
    )
    embed.description = "\n".join(lines)
    embed.set_footer(text=summary)
    return embed


class _AttendanceView(discord.ui.View):
    def __init__(self, session: _AttendanceSession):
        super().__init__(timeout=900)
        self.session = session
        self.message: Optional[discord.Message] = None
        self._build()

    def _build(self):
        self.clear_items()
        s = self.session
        slots = s.page_slots()

        if slots:
            options = []
            for slot in slots:
                key = s.slot_key(slot)
                cur = s.statuses.get(key, STATUS_UNRECORDED)
                label = f"{slot['member']} — {slot['zone'] or 'sub'}"
                desc = f"current: {_STATUS_LABELS.get(cur, '?')}"
                # Stable value: encoded as team|zone|member; max 100 chars.
                value = f"{slot['team']}|{slot['zone']}|{slot['member']}"[:100]
                options.append(discord.SelectOption(
                    label=label[:100],
                    value=value,
                    description=desc[:100],
                ))
            picker = discord.ui.Select(
                placeholder="Pick a slot to record attendance…",
                min_values=1, max_values=1,
                options=options,
            )

            async def _on_pick(inter: discord.Interaction):
                if inter.user.id != s.user_id:
                    await inter.response.send_message(
                        "⛔ Only the officer who opened this view can record attendance.",
                        ephemeral=True,
                    )
                    return
                raw = picker.values[0]
                parts = raw.split("|", 2)
                if len(parts) != 3:
                    await inter.response.send_message(
                        "⚠️ Internal error: couldn't parse slot key.",
                        ephemeral=True,
                    )
                    return
                team, zone, member = parts
                key = (team, zone, member)
                picker_view = _StatusPickerView(self, key)
                await inter.response.send_message(
                    f"Record attendance for **{member}** ({zone or 'sub'}):",
                    view=picker_view,
                    ephemeral=True,
                )
                try:
                    picker_view.message = await inter.original_response()
                except discord.HTTPException:
                    picker_view.message = None

            picker.callback = _on_pick
            self.add_item(picker)

        # Bulk-mark helpers.
        mark_all_attended = discord.ui.Button(
            label="✅ Mark unrecorded → Attended",
            style=discord.ButtonStyle.success, row=2,
        )

        async def _bulk_attended(inter: discord.Interaction):
            if inter.user.id != s.user_id:
                await inter.response.send_message(
                    "⛔ Only the officer can use this view.", ephemeral=True,
                )
                return
            for key, status in s.statuses.items():
                if not status:
                    s.statuses[key] = STATUS_ATTENDED
            await self._refresh(inter)

        mark_all_attended.callback = _bulk_attended
        self.add_item(mark_all_attended)

        # Pagination
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
                if inter.user.id != s.user_id:
                    await inter.response.send_message("⛔ Only the officer can paginate.", ephemeral=True); return
                s.page = max(0, s.page - 1)
                await self._refresh(inter)

            async def _next(inter: discord.Interaction):
                if inter.user.id != s.user_id:
                    await inter.response.send_message("⛔ Only the officer can paginate.", ephemeral=True); return
                s.page = min(s.total_pages() - 1, s.page + 1)
                await self._refresh(inter)

            prev_btn.callback = _prev
            next_btn.callback = _next
            self.add_item(prev_btn)
            self.add_item(next_btn)

        save_btn = discord.ui.Button(
            label="💾 Save attendance", style=discord.ButtonStyle.primary, row=4,
        )

        async def _save(inter: discord.Interaction):
            if inter.user.id != s.user_id:
                await inter.response.send_message("⛔ Only the officer can save.", ephemeral=True); return
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
                + counts.get(STATUS_SUB_ACTIVATED, 0)
            )
            await inter.followup.send(
                f"✅ Saved attendance for **{s.event_date}** — "
                f"{recorded} slot(s) recorded "
                f"(✅ {counts.get(STATUS_ATTENDED, 0)}, "
                f"❌ {counts.get(STATUS_NO_SHOW, 0)}, "
                f"🔄 {counts.get(STATUS_SUB_ACTIVATED, 0)}).",
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

        save_btn.callback = _save
        self.add_item(save_btn)

    async def _refresh(self, inter: discord.Interaction):
        self._build()
        await inter.response.edit_message(embed=_render_embed(self.session), view=self)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class _StatusPickerView(discord.ui.View):
    """Three-button picker shown ephemerally after the officer picks a
    slot. Records the chosen status onto the parent session."""

    def __init__(self, parent: _AttendanceView, key: tuple[str, str, str]):
        super().__init__(timeout=120)
        self._parent = parent
        self._key = key
        self.message: discord.Message | None = None

        for status in _VALID_STATUSES:
            btn = discord.ui.Button(
                label=_STATUS_LABELS[status],
                style={
                    STATUS_ATTENDED:      discord.ButtonStyle.success,
                    STATUS_NO_SHOW:       discord.ButtonStyle.danger,
                    STATUS_SUB_ACTIVATED: discord.ButtonStyle.secondary,
                }[status],
            )
            btn.callback = self._make_callback(status)
            self.add_item(btn)

        clear_btn = discord.ui.Button(
            label="↩️ Clear", style=discord.ButtonStyle.secondary,
        )
        clear_btn.callback = self._make_callback(STATUS_UNRECORDED)
        self.add_item(clear_btn)

    def _make_callback(self, status: str):
        async def _cb(inter: discord.Interaction):
            if inter.user.id != self._parent.session.user_id:
                await inter.response.send_message(
                    "⛔ Only the officer can record attendance.", ephemeral=True,
                )
                return
            self._parent.session.statuses[self._key] = status
            for item in self.children:
                item.disabled = True
            await inter.response.edit_message(
                content=f"✅ {self._key[2]} → **{_STATUS_LABELS.get(status, '?')}**",
                view=self,
            )
            # Update the main view too.
            try:
                if self._parent.message:
                    self._parent._build()
                    await self._parent.message.edit(
                        embed=_render_embed(self._parent.session),
                        view=self._parent,
                    )
            except discord.HTTPException:
                pass
            self.stop()
        return _cb

    async def on_timeout(self) -> None:
        """Strip the buttons after the 2-minute window so a stale
        click doesn't surface 'Interaction failed'. The parent
        attendance view remains active — the officer can re-pick the
        same slot to re-open the picker."""
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ── Slash command ────────────────────────────────────────────────────────────


class StormAttendanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="storm_attendance",
        description="Record who showed for an assigned storm event",
    )
    @app_commands.describe(
        event_type="Which event's attendance to record",
        event_date="Date of the event (YYYY-MM-DD)",
    )
    @app_commands.choices(event_type=[
        app_commands.Choice(name="Desert Storm", value="DS"),
        app_commands.Choice(name="Canyon Storm", value="CS"),
    ])
    @app_commands.guild_only()
    async def storm_attendance(
        self,
        interaction: discord.Interaction,
        event_type: app_commands.Choice[str],
        event_date: str,
    ):
        from storm_permissions import (
            is_leader_or_admin, deny_non_leader, ensure_premium_structured,
        )
        if not is_leader_or_admin(interaction):
            await deny_non_leader(interaction)
            return

        et = event_type.value
        date_clean = event_date.strip()
        try:
            _dt.date.fromisoformat(date_clean)
        except ValueError:
            await interaction.response.send_message(
                f"⚠️ `{event_date}` isn't a valid date. Use `YYYY-MM-DD`.",
                ephemeral=True,
            )
            return

        ok, _structured = await ensure_premium_structured(
            interaction, et,
            bot=self.bot,
            feature_label="`/storm_attendance`",
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
                f"⚠️ No structured roster found for **{date_clean}** "
                f"({'Desert Storm' if et == 'DS' else 'Canyon Storm'})."
            ]
            if slot_errors:
                msg_lines.append("Details: " + slot_errors[0])
            else:
                msg_lines.append(
                    "Attendance is only recordable for events with a "
                    "structured roster posted via `/storm_signups`."
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


async def setup(bot: commands.Bot):
    await bot.add_cog(StormAttendanceCog(bot))
