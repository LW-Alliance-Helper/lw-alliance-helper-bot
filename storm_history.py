"""
Roster history browser (#135 — Step 8 of #38).

`/desertstorm strategy roster_history [date]` and `/canyonstorm strategy roster_history
[date]` (registered via storm_strategy's existing app_commands.Group)
let leadership browse past structured rosters with attendance overlaid.

Without `date`, lists the most recent 8 events with `[View]` buttons.
With `date`, renders that event's roster directly.

Data sources: `rosters_tab` (set by [#129](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/129) Approve & Post) +
`attendance_tab` ([#133](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/133)). Read-only — corrections route through
re-running the build and re-recording attendance.
"""

from __future__ import annotations

import asyncio
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
    phase_col  = _col("Phase")        # #152 — empty for sub-pool rows
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
            "phase":    _cell(phase_col),     # "1"/"2" for phased rows, "" otherwise
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

    parent = "desertstorm" if event_type == "DS" else "canyonstorm"
    if not slots:
        embed.description = (
            "_No structured roster found for this date. Check the date "
            f"format or run `/{parent} signups` + Approve & Post to build "
            "a roster for this event._"
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
            text=f"Attendance not yet recorded. Run /{parent} attendance to add it."
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
    parent = "desertstorm" if event_type == "DS" else "canyonstorm"
    if not dates:
        embed.description = (
            f"_No structured rosters posted yet. Use `/{parent} signups` to build "
            "a roster + Approve & Post, and it'll show up here._"
        )
        return embed
    embed.description = "Click a date below to view the roster + attendance."
    return embed


# ── Officer view ─────────────────────────────────────────────────────────────


class _RosterImageLinksView(discord.ui.View):
    """`[📷 View Team A image]` / `[📷 View Team B image]` (or just
    `[📷 View image]` for CS / single-team DS) buttons attached below
    the event-detail embed. Each click fetches the saved message at
    runtime; on `discord.NotFound` (the image was deleted from the
    original channel), the officer gets a friendly explanation + the
    pointer is auto-removed so the stale link doesn't keep appearing.

    Image bytes live in Discord — we just remember (channel_id,
    message_id) and resolve at click time. No CDN URLs are stored
    (they expire); no bytes are persisted server-side.
    """

    def __init__(
        self, *,
        owner_id: int, guild_id: int, event_type: str, event_date: str,
        refs: list[dict],
    ):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.event_type = event_type
        self.event_date = event_date

        for ref in refs:
            team = ref.get("team") or ""
            if event_type == "DS" and team:
                label = f"📷 View Team {team} image"
            else:
                label = "📷 View image"
            btn = discord.ui.Button(
                label=label, style=discord.ButtonStyle.secondary,
            )
            btn.callback = self._make_callback(ref)
            self.add_item(btn)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message(
                "⛔ Only the officer who opened this view can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    def _make_callback(self, ref: dict):
        async def _cb(inter: discord.Interaction):
            channel_id = int(ref["channel_id"])
            message_id = int(ref["message_id"])
            team = ref.get("team") or ""

            channel = inter.guild.get_channel_or_thread(channel_id) if inter.guild else None
            if channel is None:
                # Channel was deleted or the bot lost access. Treat as
                # a deleted-image case — same friendly message.
                await self._handle_missing(inter, team, reason="channel")
                return
            try:
                msg = await channel.fetch_message(message_id)
            except discord.NotFound:
                await self._handle_missing(inter, team, reason="message")
                return
            except discord.Forbidden:
                await inter.response.send_message(
                    f"⚠️ The bot lost access to the channel where the image "
                    f"was posted ({channel.mention if channel else 'unknown'}). "
                    f"Re-render to save a new copy.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException as e:
                logger.warning(
                    "[STORM HISTORY] image fetch failed guild=%s msg=%s: %s",
                    self.guild_id, message_id, e,
                )
                await inter.response.send_message(
                    f"⚠️ Couldn't fetch the saved image: {e}.", ephemeral=True,
                )
                return

            link = msg.jump_url
            await inter.response.send_message(
                f"📷 [Open the saved roster image]({link}) "
                f"(posted in {channel.mention}).",
                ephemeral=True,
            )

        return _cb

    async def _handle_missing(
        self, inter: discord.Interaction, team: str, *, reason: str,
    ) -> None:
        """The saved message was deleted (or the channel is gone).
        Drop the stale pointer so the button stops appearing on future
        history opens, and tell the officer what happened."""
        import config
        try:
            await asyncio.to_thread(
                config.delete_roster_image_ref,
                self.guild_id, self.event_type, self.event_date, team,
            )
        except Exception as e:
            logger.warning(
                "[STORM HISTORY] delete_roster_image_ref failed: %s", e,
            )
        parent = "desertstorm" if self.event_type == "DS" else "canyonstorm"
        team_label = f" for Team {team}" if (self.event_type == "DS" and team) else ""
        what = "channel" if reason == "channel" else "image"
        await inter.response.send_message(
            f"⚠️ The saved roster {what}{team_label} can no longer be "
            f"found — it was deleted from the original channel. The link "
            f"has been cleared. To save a new image: open the roster "
            f"builder, click 🖼️ Render image, then 💾 Save to history.",
            ephemeral=True,
        )


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
            # Parallel Sheet reads off the event loop — gspread blocks
            # for a network round-trip each. SQLite image-refs lookup
            # is cheap but fans out here so the await is in one place.
            import config
            slots_task = asyncio.to_thread(
                load_event_roster, self.guild_id, self.event_type, date_str,
            )
            attendance_task = asyncio.to_thread(
                load_event_attendance, self.guild_id, self.event_type, date_str,
            )
            image_refs_task = asyncio.to_thread(
                config.list_roster_image_refs,
                self.guild_id, self.event_type, date_str,
            )
            (slots, _slot_errs), (attendance, _att_errs), image_refs = await asyncio.gather(
                slots_task, attendance_task, image_refs_task,
            )
            embed = render_event_embed(
                event_type=self.event_type,
                event_date=date_str,
                slots=slots,
                attendance=attendance,
            )
            child_view: Optional[discord.ui.View] = None
            if image_refs:
                child_view = _RosterImageLinksView(
                    owner_id=self.user_id,
                    guild_id=self.guild_id,
                    event_type=self.event_type,
                    event_date=date_str,
                    refs=image_refs,
                )
            await inter.followup.send(
                embed=embed,
                view=child_view or discord.utils.MISSING,
                ephemeral=True,
            )
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
    parent = "desertstorm" if event_type == "DS" else "canyonstorm"
    ok, _structured = await ensure_premium_structured(
        interaction, event_type,
        feature_label=f"`/{parent} strategy roster_history`",
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
        # Parallel Sheet reads off the event loop — gspread blocks for
        # a network round-trip each. Image refs come from SQLite and
        # are cheap, but we batch with the Sheet reads anyway so the
        # await fans out in one place.
        import config
        slots_task = asyncio.to_thread(
            load_event_roster, interaction.guild_id, event_type, date_clean,
        )
        attendance_task = asyncio.to_thread(
            load_event_attendance, interaction.guild_id, event_type, date_clean,
        )
        image_refs_task = asyncio.to_thread(
            config.list_roster_image_refs,
            interaction.guild_id, event_type, date_clean,
        )
        (slots, slot_errors), (attendance, _att_errs), image_refs = await asyncio.gather(
            slots_task, attendance_task, image_refs_task,
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
        view: Optional[discord.ui.View] = None
        if image_refs:
            view = _RosterImageLinksView(
                owner_id=interaction.user.id,
                guild_id=interaction.guild_id,
                event_type=event_type,
                event_date=date_clean,
                refs=image_refs,
            )
        await interaction.followup.send(
            content=content, embed=embed, view=view or discord.utils.MISSING,
            ephemeral=True,
        )
        return

    # No date → list view. gspread off the event loop.
    dates, _list_errors = await asyncio.to_thread(
        list_event_dates, interaction.guild_id, event_type, limit=8,
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
