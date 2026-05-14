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
        if not d or d in seen:
            continue
        # Filter out malformed dates so a typo in the Sheet (e.g.
        # "2026-13-50") doesn't surface a button that crashes the
        # date-detail renderer downstream.
        try:
            _dt.date.fromisoformat(d)
        except ValueError:
            continue
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
    paired_col = _col("Paired With")

    # Truthy values for the override column. Officers may hand-edit
    # the Sheet — accept the same set the bot would write plus the
    # standard yes-aliases. Matches the set used by
    # `storm_officer_view._read_roster_rows` +
    # `storm_roster_builder._read_roster_powers` +
    # `storm_attendance.load_rostered_slots` so a literal in any of
    # those columns is interpreted the same way.
    truthy = {"yes", "y", "1", "true", "t", "x"}

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
            "override_below_floor": _cell(ovr_col).lower() in truthy,
            # `paired_with` is the primary's name on sub rows when
            # sub_mode=paired; blank for primary rows and pool-mode subs.
            # Older rosters_tab data without the column reads as "".
            "paired_with": _cell(paired_col),
        })
    return slots, errors


def _attendance_join_key(team: str, zone: str, member: str) -> tuple[str, str, str]:
    """Normalize the (team, zone, member) join key so a stray
    whitespace or case difference between rosters_tab and
    attendance_tab doesn't silently kill the attendance overlay.

    Member names are case-folded too — officers occasionally hand-edit
    one tab without updating the other, and "alice" / "Alice" should
    not be treated as two different members for the join.
    """
    return (
        (team or "").strip(),
        (zone or "").strip(),
        (member or "").strip().casefold(),
    )


def load_event_attendance(
    guild_id: int, event_type: str, event_date: str,
) -> tuple[dict[tuple[str, str, str], str], list[str]]:
    """Return `{normalized_key: status}` for this event's attendance
    rows. Missing tab → empty dict (no errors).

    Keys use `_attendance_join_key` so the join with rosters_tab is
    whitespace/case-tolerant; callers that look up by member must
    normalize the same way.
    """
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
        key = _attendance_join_key(
            _cell(team_col), _cell(zone_col), _cell(member_col),
        )
        out[key] = _cell(status_col)
    return out, errors


# ── Renderers ────────────────────────────────────────────────────────────────


_STATUS_GLYPH = {
    "attended":      "✅",
    "no_show":       "❌",
    "sub_activated": "🔄",
    "":              "—",
}


def _format_power_display(raw: str) -> str:
    """Render a stored power string (`"412000000"` / `"unknown"` /
    `""`) for human display. Numeric values get the canonical
    250M / 1.2B shape via `storm_strategy.format_power`; the sentinel
    `"unknown"` and blanks are dropped so the embed line stays clean."""
    if not raw or raw == "unknown":
        return ""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return f" — {raw}"
    from storm_strategy import format_power
    return f" — {format_power(n)}"


def render_event_embed(
    *,
    event_type: str,
    event_date: str,
    slots: list[dict],
    attendance: dict[tuple[str, str, str], str],
) -> discord.Embed:
    from storm_date_helpers import format_event_date

    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    date_pretty = format_event_date(event_date)

    embed = discord.Embed(
        title=f"📜 {label} Roster — {date_pretty}",
        color=discord.Color.dark_gold() if event_type == "DS"
              else discord.Color.dark_orange(),
    )

    if not slots:
        embed.description = (
            "_No structured roster found for this date. Check the date "
            "format or run `/storm_signups` + Approve & Post to build a "
            "roster for this event._"
        )
        return embed

    # Group by team → zone → list of members. Iterate teams + zones in
    # sorted order so the embed renders the same regardless of how
    # rosters_tab rows happen to be ordered.
    teams: dict[str, dict[str, list[dict]]] = {}
    for slot in slots:
        team = slot["team"] or "(no team)"
        zone = slot["zone"] or "(sub pool)"
        teams.setdefault(team, {}).setdefault(zone, []).append(slot)

    total_recorded = 0
    total_attended = 0
    total_no_show = 0
    total_sub_activated = 0

    # Per-team `add_field` (each capped at 1024 chars) instead of one
    # giant `embed.description` — a 30+ slot roster with status + power
    # markers can blow Discord's 4096-char description limit.
    for team in sorted(teams.keys()):
        zones = teams[team]
        team_lines: list[str] = []
        for zone in sorted(zones.keys()):
            members = zones[zone]
            team_lines.append(f"__{zone}__")
            for slot in members:
                key = _attendance_join_key(
                    slot["team"], slot["zone"], slot["member"],
                )
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
                power_part = _format_power_display(slot.get("power", ""))
                override = " ⚠️ override" if slot.get("override_below_floor") else ""
                # Role marker: a sub paired with a specific primary
                # surfaces "paired with X" so the pairing is visible
                # in the history; a pool sub shows the generic "(sub)".
                if slot.get("role") == "sub":
                    paired = slot.get("paired_with") or ""
                    role_marker = f" (sub, paired with {paired})" if paired else " (sub)"
                else:
                    role_marker = ""
                team_lines.append(
                    f"{glyph} {slot['member']}{role_marker}{power_part}{override}"
                )

        team_name = "Roster" if (team in ("", "(no team)")) else f"Team {team}"
        body = "\n".join(team_lines)
        # Field value cap is 1024 chars. Truncate with a trailing marker
        # so leadership knows to look in the Sheet for the rest.
        if len(body) > 1020:
            body = body[:980].rsplit("\n", 1)[0] + "\n_…trimmed; see Sheet for full list_"
        embed.add_field(name=team_name, value=body, inline=False)

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
            text="Attendance not yet recorded. Run /storm_attendance to add it."
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
    embed.description = "Click a date below to view the roster + attendance."
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

        from storm_date_helpers import format_event_date_compact
        for date_str in dates:
            btn = discord.ui.Button(
                label=format_event_date_compact(date_str),
                style=discord.ButtonStyle.secondary,
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
            # Send the event-detail embed as an ephemeral followup but
            # KEEP the date buttons active so the officer can hop between
            # dates. The prior implementation disabled every button on
            # the first click, making the list view effectively one-shot.
            await inter.response.defer(ephemeral=True, thinking=True)
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
            await inter.followup.send(embed=embed, ephemeral=True)
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

    # Defer once we've cleared the permission/premium checks. All three
    # render paths (direct-date, list, date-button click) send their
    # embeds ephemerally so the entire roster-history surface stays
    # officer-only — the prior implementation was inconsistent.
    await interaction.response.defer(ephemeral=True, thinking=True)

    if event_date:
        from storm_date_helpers import parse_event_date
        parsed = parse_event_date(event_date.strip())
        if parsed is None:
            await interaction.followup.send(
                f"⚠️ `{event_date}` isn't a date I can parse. Try `May 18`, "
                f"`5/18`, `2026-05-18`, or `yesterday`.",
                ephemeral=True,
            )
            return
        date_clean = parsed.isoformat()
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
        await interaction.followup.send(
            content=content, embed=embed, ephemeral=True,
        )
        return

    # No date → list view.
    dates, _list_errors = list_event_dates(
        interaction.guild_id, event_type, limit=8,
    )
    embed = render_history_list_embed(event_type, dates)
    if dates:
        view = _HistoryListView(
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            event_type=event_type,
            dates=dates,
        )
        msg = await interaction.followup.send(
            embed=embed, view=view, ephemeral=True,
        )
        view.message = msg
    else:
        # Skip constructing a view when there are no buttons to render
        # — the empty embed is enough and avoids a phantom timeout
        # registration with no children.
        await interaction.followup.send(embed=embed, ephemeral=True)
