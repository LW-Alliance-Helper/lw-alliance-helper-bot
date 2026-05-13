"""
Roster history browser (#135 — Step 8 of #38).

`/ds_strategy roster_history [date]` and `/cs_strategy roster_history
[date]` (registered via storm_strategy's existing app_commands.Group)
let leadership browse past structured rosters with attendance overlaid.

Without `date`, lists the most recent 8 events with `[View]` buttons.
With `date`, renders that event's roster directly.

Data sources: `rosters_tab` (set by [#129](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/129) Approve & Post) +
`attendance_tab` ([#133](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/133)). Read-only — corrections route through
re-running the build and re-recording attendance.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

import discord

logger = logging.getLogger(__name__)


# ── Sheet readers ────────────────────────────────────────────────────────────


def _rosters_tab_name(guild_id: int, event_type: str) -> str:
    import config
    cfg = config.get_structured_storm_config(guild_id, event_type)
    return cfg.get("rosters_tab") or config.default_structured_tab(
        event_type, "rosters_tab"
    )


def _attendance_tab_name(guild_id: int, event_type: str) -> str:
    import config
    cfg = config.get_structured_storm_config(guild_id, event_type)
    return cfg.get("attendance_tab") or config.default_structured_tab(
        event_type, "attendance_tab"
    )


def _read_tab_values(guild_id: int, tab_name: str) -> tuple[list[list[str]], list[str]]:
    """Generic Sheet-tab reader. Returns `(rows, errors)`. Missing tab
    returns `([], [])` so callers degrade gracefully."""
    import config
    try:
        sh = config.get_spreadsheet(guild_id)
    except Exception as e:
        return [], [f"spreadsheet open failed: {e}"]
    if sh is None:
        return [], []
    try:
        ws = sh.worksheet(tab_name)
    except Exception:
        return [], []
    try:
        return ws.get_all_values(), []
    except Exception as e:
        return [], [f"read {tab_name} failed: {e}"]


def list_event_dates(
    guild_id: int, event_type: str, *, limit: int = 8,
) -> tuple[list[str], list[str]]:
    """Return the most-recent N event dates (descending) for which a
    structured roster exists. Returns `(dates, errors)`."""
    tab = _rosters_tab_name(guild_id, event_type)
    if not tab:
        return [], []
    rows, errors = _read_tab_values(guild_id, tab)
    if errors or not rows:
        return [], errors
    header = [c.strip() for c in rows[0]]
    try:
        date_col = header.index("Event Date")
    except ValueError:
        return [], ["rosters tab missing 'Event Date' header"]

    seen: dict[str, None] = {}
    for row in rows[1:]:
        if date_col >= len(row):
            continue
        d = row[date_col].strip()
        if d and d not in seen:
            seen[d] = None
    dates = sorted(seen.keys(), reverse=True)[:limit]
    return dates, []


def load_event_roster(
    guild_id: int, event_type: str, event_date: str,
) -> tuple[list[dict], list[str]]:
    """Return all rosters_tab rows for an event date as a list of dicts.
    Each row: `{team, zone, member, role, power, discord_id,
    override_below_floor}`. Returns `([], errors_or_empty)` when the
    tab or event doesn't exist."""
    tab = _rosters_tab_name(guild_id, event_type)
    rows, errors = _read_tab_values(guild_id, tab) if tab else ([], [])
    if not rows:
        return [], errors

    header = [c.strip() for c in rows[0]]

    def _col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            return -1

    date_col   = _col("Event Date")
    team_col   = _col("Team")
    zone_col   = _col("Zone")
    member_col = _col("Member")
    role_col   = _col("Role")
    power_col  = _col("Power at Assignment")
    id_col     = _col("Discord ID")
    ovr_col    = _col("Override Below Floor")

    slots: list[dict] = []
    for row in rows[1:]:
        def _cell(idx: int) -> str:
            return row[idx].strip() if 0 <= idx < len(row) else ""
        if _cell(date_col) != event_date:
            continue
        slots.append({
            "team":     _cell(team_col),
            "zone":     _cell(zone_col),
            "member":   _cell(member_col),
            "role":     _cell(role_col) or "primary",
            "power":    _cell(power_col),
            "discord_id": _cell(id_col),
            "override_below_floor": _cell(ovr_col).lower() == "yes",
        })
    return slots, errors


def load_event_attendance(
    guild_id: int, event_type: str, event_date: str,
) -> tuple[dict[tuple[str, str, str], str], list[str]]:
    """Return `{(team, zone, member): status}` for this event's
    attendance rows. Missing tab → empty dict (no errors)."""
    tab = _attendance_tab_name(guild_id, event_type)
    rows, errors = _read_tab_values(guild_id, tab) if tab else ([], [])
    if not rows:
        return {}, errors

    header = [c.strip() for c in rows[0]]

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

    out: dict[tuple[str, str, str], str] = {}
    for row in rows[1:]:
        def _cell(idx: int) -> str:
            return row[idx].strip() if 0 <= idx < len(row) else ""
        if _cell(date_col) != event_date:
            continue
        key = (_cell(team_col), _cell(zone_col), _cell(member_col))
        out[key] = _cell(status_col)
    return out, errors


# ── Renderers ────────────────────────────────────────────────────────────────


_STATUS_GLYPH = {
    "attended":      "✅",
    "no_show":       "❌",
    "sub_activated": "🔄",
    "":              "—",
}


def render_event_embed(
    *,
    event_type: str,
    event_date: str,
    slots: list[dict],
    attendance: dict[tuple[str, str, str], str],
) -> discord.Embed:
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    try:
        d = _dt.date.fromisoformat(event_date)
        date_pretty = d.strftime("%A, %B %d, %Y")
    except ValueError:
        date_pretty = event_date

    embed = discord.Embed(
        title=f"📜 {label} Roster — {date_pretty}",
        color=discord.Color.dark_gold() if event_type == "DS"
              else discord.Color.dark_orange(),
    )

    if not slots:
        embed.description = (
            "_No structured roster found for this date. Either nothing was "
            "posted via `/storm_signups`, or attendance was tracked under a "
            "different event type._"
        )
        return embed

    # Group by team → zone → list of members.
    teams: dict[str, dict[str, list[dict]]] = {}
    for slot in slots:
        team = slot["team"] or "(no team)"
        zone = slot["zone"] or "(sub pool)"
        teams.setdefault(team, {}).setdefault(zone, []).append(slot)

    lines: list[str] = []
    total_recorded = 0
    total_attended = 0
    total_no_show = 0
    total_sub_activated = 0

    for team, zones in teams.items():
        lines.append(f"\n**Team {team}**" if team and team != "(no team)" else "\n**Roster**")
        for zone, members in zones.items():
            lines.append(f"__{zone}__")
            for slot in members:
                key = (slot["team"], slot["zone"], slot["member"])
                status = attendance.get(key, "")
                glyph = _STATUS_GLYPH.get(status, "—")
                if status:
                    total_recorded += 1
                    if status == "attended":
                        total_attended += 1
                    elif status == "no_show":
                        total_no_show += 1
                    elif status == "sub_activated":
                        total_sub_activated += 1
                power_part = f" — {slot['power']}" if slot.get("power") and slot["power"] != "unknown" else ""
                override = " ⚠️ override" if slot.get("override_below_floor") else ""
                role_marker = " (sub)" if slot.get("role") == "sub" else ""
                lines.append(f"{glyph} {slot['member']}{role_marker}{power_part}{override}")

    embed.description = "\n".join(lines)

    if total_recorded > 0:
        embed.set_footer(
            text=(
                f"Attendance: ✅ {total_attended}  ·  "
                f"❌ {total_no_show}  ·  "
                f"🔄 {total_sub_activated}  "
                f"(recorded {total_recorded} of {len(slots)} slots)"
            )
        )
    else:
        embed.set_footer(
            text=f"Attendance not yet recorded. Run /storm_attendance to add it."
        )
    return embed


def render_history_list_embed(
    event_type: str, dates: list[str],
) -> discord.Embed:
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    embed = discord.Embed(
        title=f"📜 {label} — Recent Rosters",
        color=discord.Color.dark_gold() if event_type == "DS"
              else discord.Color.dark_orange(),
    )
    if not dates:
        embed.description = (
            "_No structured rosters posted yet. Use `/storm_signups` to build "
            "a roster + Approve & Post, and it'll show up here._"
        )
        return embed
    lines = ["Click a date below to view the roster + attendance."]
    embed.description = "\n".join(lines)
    return embed


# ── Officer view ─────────────────────────────────────────────────────────────


class _HistoryListView(discord.ui.View):
    """Lists recent event dates as buttons. Click → re-renders the
    embed for that event."""

    def __init__(
        self,
        *,
        guild_id: int, user_id: int, event_type: str,
        dates: list[str],
    ):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id  = user_id
        self.event_type = event_type
        self.message: Optional[discord.Message] = None

        for date_str in dates:
            btn = discord.ui.Button(
                label=date_str, style=discord.ButtonStyle.secondary,
            )
            btn.callback = self._make_callback(date_str)
            self.add_item(btn)

    def _make_callback(self, date_str: str):
        async def _cb(inter: discord.Interaction):
            if inter.user.id != self.user_id:
                await inter.response.send_message(
                    "⛔ Only the officer who opened this view can switch dates.",
                    ephemeral=True,
                )
                return
            await inter.response.defer(thinking=True)
            slots, _slot_errs = load_event_roster(
                self.guild_id, self.event_type, date_str,
            )
            attendance, _att_errs = load_event_attendance(
                self.guild_id, self.event_type, date_str,
            )
            embed = render_event_embed(
                event_type=self.event_type,
                event_date=date_str,
                slots=slots,
                attendance=attendance,
            )
            for item in self.children:
                item.disabled = True
            await inter.followup.send(embed=embed, ephemeral=True)
            if self.message:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass
        return _cb

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ── Entry point invoked by storm_strategy slash commands ────────────────────


async def open_history(
    interaction: discord.Interaction, event_type: str, event_date: str | None,
) -> None:
    """Top-level handler called from the `roster_history` subcommand on
    each strategy group in storm_strategy."""
    from storm_permissions import (
        is_leader_or_admin, deny_non_leader, ensure_premium_structured,
    )
    if not is_leader_or_admin(interaction):
        await deny_non_leader(interaction)
        return
    if not interaction.guild_id:
        await interaction.response.send_message(
            "⚠️ This command must be used inside a server.", ephemeral=True,
        )
        return
    ok, _structured = await ensure_premium_structured(
        interaction, event_type,
        feature_label="`/" + ("ds" if event_type == "DS" else "cs") + "_strategy roster_history`",
    )
    if not ok:
        return

    # Defer once we've cleared the permission/premium checks.
    await interaction.response.defer(thinking=True)

    if event_date:
        date_clean = event_date.strip()
        try:
            _dt.date.fromisoformat(date_clean)
        except ValueError:
            await interaction.followup.send(
                f"⚠️ `{event_date}` isn't a valid date. Use `YYYY-MM-DD`.",
                ephemeral=True,
            )
            return
        slots, slot_errors = load_event_roster(
            interaction.guild_id, event_type, date_clean,
        )
        attendance, _att_errs = load_event_attendance(
            interaction.guild_id, event_type, date_clean,
        )
        embed = render_event_embed(
            event_type=event_type,
            event_date=date_clean,
            slots=slots,
            attendance=attendance,
        )
        content = None
        if slot_errors:
            content = "⚠️ Read had soft errors — see bot logs."
            logger.warning(
                "[STORM HISTORY] roster read errors guild=%s date=%s: %s",
                interaction.guild_id, date_clean, "; ".join(slot_errors),
            )
        await interaction.followup.send(content=content, embed=embed)
        return

    # No date → list view.
    dates, list_errors = list_event_dates(
        interaction.guild_id, event_type, limit=8,
    )
    embed = render_history_list_embed(event_type, dates)
    view = _HistoryListView(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        event_type=event_type,
        dates=dates,
    )
    msg = await interaction.followup.send(embed=embed, view=view if dates else None)
    view.message = msg
