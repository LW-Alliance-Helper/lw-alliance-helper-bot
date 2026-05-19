"""
storm.py — Desert Storm + Canyon Storm mail generation

Reached via the `📄 Generate mail` button on `/desertstorm` and
`/canyonstorm` (hub-restructure #187). Pre-#187 this was a top-level
slash subcommand (`/desertstorm draft`).

Flow:
  1. Pick Team (A or B)
  2. Pick Time (option 1 or option 2 from the saved storm config)
  3. Mail template — bot shows the team's saved template and offers buttons:
       • Use as-is — skip straight to preview
       • Edit     — leadership pastes the edited block; the parsed
                    assignments are saved to the sheet, but the mail itself
                    is **not** posted yet ("Template saved (not posted)")
  4. Preview — final review with "Post & Copy" (posts to the configured
     post-channel and prints a copyable code block in the leadership
     channel) and "Cancel".

Assignments are persisted in the DS Assignments tab of the Google Sheet.
Sheet structure:
  Section headers in col A: DS_A_ZONES, DS_A_SUBS, DS_B_ZONES, DS_B_SUBS
  Data rows follow each header (col A = zone/name, col B = members/sub)
"""

import asyncio
import json
import os
import discord
from config import get_config
from storm_event_hub import HUB_COMMAND, HUB_BTN_DRAFT
import wizard_registry

WIZARD_TIMEOUT = 600  # 10 minutes


# ── Default assignments ────────────────────────────────────────────────────────
# DS rosters start empty per team. Leadership fills them in via the
# `📄 Generate mail` button on `/desertstorm`; the saved sheet is the
# source of truth thereafter.

DEFAULTS = {
    "A": ({}, []),
    "B": ({}, []),
}


# ── Canonical DS zone structure ────────────────────────────────────────────────
# Desert Storm zones are game-defined and identical across every alliance.
# This is the single source of truth for build_ds_template, parse_ds_template,
# and build_ds_mail — any zone name not in this list is treated as a typo
# (parser rejects it, builder doesn't render it).
DS_ZONE_STRUCTURE: list[str] = [
    "Nuclear Silo",
    "Oil Refinery I",
    "Oil Refinery II",
    "Science Hub",
    "Info Center",
    "Field Hospital I",
    "Field Hospital II",
    "Field Hospital III",
    "Field Hospital IV",
    "Arsenal",
    "Mercenary Factory",
]


# ── Canonical team-seat counts (#219) ──────────────────────────────────────────
# Last War defines every DS and CS team as 20 starters + 10 subs. Alliances
# do not customise these numbers. The Premium auto-fill in
# storm_roster_builder relies on this split to decide who lands in zones vs
# the sub pool, independent of preset zone capacity (which is allowed to
# exceed the team size so officers can place the same person in multiple
# stages without enforcement).
DS_TEAM_STARTERS = 20
DS_TEAM_SUBS     = 10
CS_TEAM_STARTERS = 20
CS_TEAM_SUBS     = 10


def team_seats(event_type: str) -> tuple[int, int]:
    """Return (starters, subs) for the given storm event type.

    `event_type` is "DS" or "CS". Anything else falls back to the DS
    numbers since both event types share the same split today.
    """
    if event_type == "CS":
        return CS_TEAM_STARTERS, CS_TEAM_SUBS
    return DS_TEAM_STARTERS, DS_TEAM_SUBS


def _non_canonical_ds_zones(zones: dict) -> dict:
    """Return {zone_name: members} entries from `zones` that aren't in
    DS_ZONE_STRUCTURE and have a non-empty value. Used by the draft flow to
    warn leadership when saved sheet data contains typo zone names that will
    be dropped on next save."""
    canonical = set(DS_ZONE_STRUCTURE)
    return {k: v for k, v in zones.items() if k not in canonical and v}


def _non_canonical_cs_zones(zones: dict) -> dict:
    """CS analogue of _non_canonical_ds_zones. Skips the subs key (renders
    via {subs}, not as a zone)."""
    canonical = {k for _, k, _ in CS_ZONE_STRUCTURE} | {CS_SUBS_KEY}
    return {k: v for k, v in zones.items() if k not in canonical and v}


def _split_legacy_subs(value: str) -> list[str]:
    """Flatten a legacy inline subs string into a list of names.

    Pre-#37 DS data was `Starter - Sub` tuples (we keep only the sub) and
    pre-#37 CS data was a single cell with names joined by commas,
    ampersands, or dashes. Both shapes flatten through this helper.
    """
    import re
    parts: list[str] = []
    for chunk in re.split(r"\s*[,\-&]\s*", str(value)):
        name = chunk.strip()
        if name:
            parts.append(name)
    return parts



# ── Google Sheets persistence ──────────────────────────────────────────────────

def _get_spreadsheet(guild_id: int = None):
    from config import get_spreadsheet
    return get_spreadsheet(guild_id)


def load_ds_assignments(team: str, guild_id: int = None) -> tuple[dict, list]:
    """
    Load saved DS assignments for the given team ("A" or "B").
    Falls back to defaults if nothing is saved yet.

    `guild_id` resolves the per-guild spreadsheet + tab name. When
    omitted, falls back to the env-var SPREADSHEET_ID and the default
    tab name "DS Assignments" — preserves the legacy single-guild
    behavior for callers that haven't been migrated yet.
    """
    zone_key = f"DS_{team}_ZONES"
    sub_key  = f"DS_{team}_SUBS"

    try:
        from config import get_config
        cfg  = get_config(guild_id) if guild_id else None
        sh   = _get_spreadsheet(guild_id)
        ws   = sh.worksheet(cfg.tab_ds_assignments if cfg else "DS Assignments")
        rows = ws.get_all_values()

        zones   = {}
        subs    = []
        section = None

        for row in rows:
            if not row or not row[0].strip():
                continue
            key = row[0].strip()

            if key == zone_key:
                section = "zones"
                continue
            if key == sub_key:
                section = "subs"
                continue
            # Stop reading this team's section when hitting another team's header
            if key.startswith("DS_") and key not in (zone_key, sub_key):
                section = None
                continue

            if section == "zones" and len(row) >= 2:
                zones[key] = row[1].strip()
            elif section == "subs":
                # Sheet rows under DS_*_SUBS are one column post-#37 (just
                # the sub name). Legacy two-column rows carried a starter
                # in col A and the sub in col B — keep col B only since
                # the starter wasn't the sub. Fall through to col A when
                # col B is empty (single-column row from the new shape).
                col_a = row[0].strip() if row else ""
                col_b = row[1].strip() if len(row) >= 2 else ""
                name = col_b or col_a
                if name:
                    subs.append(name)

        if zones:
            print(f"[STORM] Loaded Team {team} assignments ({len(zones)} zones, {len(subs)} subs)")
            return zones, subs
        else:
            print(f"[STORM] No saved Team {team} assignments — using defaults")
            default_zones, default_subs = DEFAULTS[team]
            return dict(default_zones), list(default_subs)

    except Exception as e:
        from config import describe_sheet_error
        print(
            f"[STORM] Error loading Team {team} assignments: "
            f"{describe_sheet_error(e, guild_id=guild_id, tab='DS Assignments')}"
        )
        default_zones, default_subs = DEFAULTS[team]
        return dict(default_zones), list(default_subs)


def save_ds_assignments(team: str, zones: dict, subs: list,
                        guild_id: int = None):
    """
    Save DS assignments for one team without affecting the other team's data.
    Reads the full sheet, replaces this team's sections, and rewrites.

    `guild_id` resolves the per-guild spreadsheet + tab name; when
    omitted, falls back to env-var SPREADSHEET_ID and tab "DS Assignments".
    """
    zone_key = f"DS_{team}_ZONES"
    sub_key  = f"DS_{team}_SUBS"
    other    = "B" if team == "A" else "A"
    other_zone_key = f"DS_{other}_ZONES"
    other_sub_key  = f"DS_{other}_SUBS"

    try:
        from config import get_config
        cfg = get_config(guild_id) if guild_id else None
        sh  = _get_spreadsheet(guild_id)
        ws  = sh.worksheet(cfg.tab_ds_assignments if cfg else "DS Assignments")

        # Load the other team's current data so we don't lose it
        other_zones, other_subs = load_ds_assignments(other, guild_id=guild_id)

        # Rebuild the full sheet with both teams
        rows = []

        # Team A first, then Team B — alphabetical for consistency
        for t, t_zones, t_subs in [
            ("A", zones if team == "A" else other_zones,
                  subs  if team == "A" else other_subs),
            ("B", zones if team == "B" else other_zones,
                  subs  if team == "B" else other_subs),
        ]:
            rows.append([f"DS_{t}_ZONES", ""])
            for zone, members in t_zones.items():
                rows.append([zone, members])
            rows.append(["", ""])
            rows.append([f"DS_{t}_SUBS", ""])
            for sub in t_subs:
                # Flatten any transitional `(starter, sub)` tuple to the
                # sub name only; otherwise emit the sub string as-is.
                if isinstance(sub, tuple) and len(sub) >= 2:
                    name = str(sub[1])
                else:
                    name = str(sub)
                if name:
                    rows.append([name])
            rows.append(["", ""])  # blank separator between teams

        ws.clear()
        ws.update("A1", rows, value_input_option="USER_ENTERED")
        print(f"[STORM] Team {team} assignments saved ({len(zones)} zones, {len(subs)} sub pairs)")

    except Exception as e:
        from config import describe_sheet_error
        print(
            f"[STORM] Error saving Team {team} assignments: "
            f"{describe_sheet_error(e, guild_id=guild_id, tab='DS Assignments')}"
        )


# ── Template builder & parser ──────────────────────────────────────────────────

def build_ds_template(zones: dict, subs: list) -> str:
    """Render the editable template for DS draft.

    Walks DS_ZONE_STRUCTURE in canonical order so leadership always sees a
    labeled grid — every zone renders, with `Zone: ` blank when unassigned.
    Non-canonical zone keys in `zones` are silently skipped (they'll be
    dropped on the next save; the draft flow surfaces them separately).

    Subs render as a flat list, one name per line. Legacy `(starter, sub)`
    tuples flatten to just the sub on emit so transitional in-memory data
    doesn't surface the deprecated pair shape to leadership.
    """
    lines = ["ZONE ASSIGNMENTS"]
    for zone in DS_ZONE_STRUCTURE:
        lines.append(f"{zone}: {zones.get(zone, '')}")
    lines.append("")
    lines.append("SUBS")
    for sub in subs:
        if isinstance(sub, tuple) and len(sub) >= 2:
            lines.append(str(sub[1]))
        elif sub:
            lines.append(str(sub))
    return "\n".join(lines)


def parse_ds_template(text: str) -> tuple[dict, list, list]:
    """Parse the edited DS template. Returns (zones, subs, errors).

    Zone names must match DS_ZONE_STRUCTURE — non-canonical names go to
    `errors` and are NOT added to the zones dict. Matching is
    case-insensitive against the canonical list to forgive minor casing
    differences without permitting typos.

    Subs return as a flat ``list[str]``. The `SUBS` header (post-#37) is the
    canonical form; the legacy `SUB PAIRS (Starter - Sub)` header is still
    accepted for backward compatibility — each pair line keeps only the sub
    side (right of ` - `), since the starter was never the actual sub.
    """
    canonical_by_lower = {z.lower(): z for z in DS_ZONE_STRUCTURE}
    canonical_list     = ", ".join(DS_ZONE_STRUCTURE)
    zones: dict = {}
    subs: list[str] = []
    errors: list[str] = []
    section = None
    subs_legacy_paired = False

    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper() == "ZONE ASSIGNMENTS":
            section = "zones"
            continue
        if line.upper() == "SUBS":
            section = "subs"
            subs_legacy_paired = False
            continue
        if line.upper().startswith("SUB PAIRS"):
            section = "subs"
            subs_legacy_paired = True
            continue

        if section == "zones":
            if ":" in line:
                zone, _, members = line.partition(":")
                zone_stripped = zone.strip()
                canonical = canonical_by_lower.get(zone_stripped.lower())
                if canonical is None:
                    errors.append(
                        f"Unknown zone `{zone_stripped}`. Must be one of: "
                        f"{canonical_list}"
                    )
                else:
                    zones[canonical] = members.strip()
            else:
                errors.append(f"Could not parse zone line: {line}")
        elif section == "subs":
            if subs_legacy_paired and " - " in line:
                _, _, sub_name = line.partition(" - ")
                sub_name = sub_name.strip()
                if sub_name:
                    subs.append(sub_name)
            else:
                subs.append(line)

    return zones, subs, errors


# ── Mail builder ───────────────────────────────────────────────────────────────

def build_ds_mail(team: str, zones: dict, subs: list, time_key: str,
                  guild_id: int = None, template_name: str | None = None) -> str:
    """Build DS mail using a guild's stored template (named or default)."""
    from config import (
        get_storm_template, format_storm_slot, get_storm_slot_for_key,
    )
    if guild_id:
        template = get_storm_template(guild_id, "DS", template_name)
    else:
        from config import get_storm_config
        template = (get_storm_config(guild_id, "DS") or {}).get("mail_template") or ""

    # `time_key` is "1" or "2" from TimeSelectView. Tests pass arbitrary text
    # like "18:00 Server Time" — fall through to that string verbatim so
    # build_X_mail stays composable in test fixtures.
    slot = get_storm_slot_for_key("DS", time_key) if guild_id else None
    if slot is not None:
        h, m = slot
        time_str = format_storm_slot(h, m, guild_id)
    else:
        time_str = time_key

    # Walk DS_ZONE_STRUCTURE in canonical order so the mail reads consistently
    # for every alliance. Skip zones with no members. Any non-canonical keys
    # left in `zones` (legacy fixtures, in-memory test data) emit at the end
    # with the raw key as a fallback label, so nothing silently disappears.
    zone_lines: list[str] = []
    rendered: set[str] = set()

    def _emit_members(members):
        if isinstance(members, list):
            zone_lines.append("\n".join(str(m) for m in members))
        else:
            zone_lines.append(str(members))
        zone_lines.append("")

    from storm_icons import zone_emoji_prefix
    for zone in DS_ZONE_STRUCTURE:
        members = zones.get(zone)
        if not members or members == "(open)":
            rendered.add(zone)
            continue
        # #158: prefix the zone header with its emoji. No-op until the
        # emojis upload; safe to land before art for every zone exists.
        zone_lines.append(f"{zone_emoji_prefix(zone)}**{zone}**")
        _emit_members(members)
        rendered.add(zone)

    extra = [(k, v) for k, v in zones.items() if k not in rendered and v and v != "(open)"]
    for key, members in extra:
        zone_lines.append(f"{zone_emoji_prefix(key)}**{key}**")
        _emit_members(members)

    zones_block = "\n".join(zone_lines).strip()

    if isinstance(subs, list) and subs:
        # Subs are list[str] post-#37. Legacy in-memory list[tuple] data
        # flattens to just the sub side (right of the pair) so the mail
        # never surfaces the deprecated paired shape.
        names = []
        for s in subs:
            if isinstance(s, tuple) and len(s) >= 2:
                names.append(str(s[1]))
            elif s:
                names.append(str(s))
        subs_block = "\n".join(names) if names else "(none)"
    elif subs:
        subs_block = str(subs)
    else:
        subs_block = "(none)"

    if template:
        return template.format(
            alliance_name="Alliance",
            zones=zones_block,
            subs=subs_block,
            time=time_str,
        )

    # Fallback plain format
    return "\n".join([
        "**Desert Storm**",
        "",
        "**Zone Assignments**",
        zones_block,
        "",
        "**Sub Pairs**",
        subs_block,
        "",
        f"**Time:** {time_str}",
    ])


# ── UI Views ───────────────────────────────────────────────────────────────────

class TeamSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected = None

    @discord.ui.button(label="Team A", style=discord.ButtonStyle.primary)
    async def pick_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "A"
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Team B", style=discord.ButtonStyle.success)
    async def pick_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.selected = "B"
        await interaction.response.defer()
        self.stop()


class TemplateUseEditView(discord.ui.View):
    """Shown after time selection — leadership picks Use as-is or Edit."""

    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.choice = None  # "use" | "edit" | None (timeout)

    @discord.ui.button(label="✅ Use as-is", style=discord.ButtonStyle.success)
    async def use(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = "use"
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary)
    async def edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.choice = "edit"
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


async def _post_and_copy(channel, post_channel_id: int, event_label: str,
                          team: str, mail: str):
    """
    Post the finalized mail to the configured post-channel (if set) and
    always send a copyable code block back into the leadership channel.
    """
    posted_to = None
    if post_channel_id:
        try:
            target = channel.guild.get_channel(post_channel_id) if channel.guild else None
            if target is None and getattr(channel, "_state", None):
                target = channel._state.get_channel(post_channel_id)
            if target is not None:
                await target.send(mail)
                posted_to = target.mention
        except Exception as e:
            print(f"[STORM] Failed to post mail to channel {post_channel_id}: {e}")

    suffix = f" (also posted to {posted_to})" if posted_to else ""
    await channel.send(
        f"✅ **{event_label} Team {team} mail, ready to copy{suffix}:**\n"
        f"```\n{mail}\n```"
    )


class TimeSelectView(discord.ui.View):
    """Dynamic time select — buttons built from the game-defined storm
    times (DS_SERVER_TIMES / CS_SERVER_TIMES) rendered against the guild's
    timezone."""
    def __init__(self, event_type: str = "DS", guild_id: int = None):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.selected = None
        from config import get_storm_slot_labels
        labels = get_storm_slot_labels(event_type, guild_id)

        b1 = discord.ui.Button(label=labels[0][:80], style=discord.ButtonStyle.secondary)
        b2 = discord.ui.Button(label=labels[1][:80], style=discord.ButtonStyle.secondary)

        async def pick_1(interaction: discord.Interaction):
            self.selected = "1"
            await interaction.response.defer()
            self.stop()
        async def pick_2(interaction: discord.Interaction):
            self.selected = "2"
            await interaction.response.defer()
            self.stop()

        b1.callback = pick_1
        b2.callback = pick_2
        self.add_item(b1)
        self.add_item(b2)


class StormApprovalView(discord.ui.View):
    def __init__(self, bot, team: str, mail: str, zones: dict, subs: list,
                 time_key: str, post_channel_id: int = 0):
        super().__init__(timeout=3600)
        self.bot             = bot
        self.team            = team
        self.mail            = mail
        self.zones           = zones
        self.subs            = subs
        self.time_key        = time_key
        self.post_channel_id = post_channel_id

    async def _disable(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="✅ Looks Good: Post & Copy", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable(interaction)
        await _post_and_copy(
            interaction.channel, self.post_channel_id,
            "Desert Storm", self.team, self.mail,
        )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable(interaction)
        await interaction.followup.send("❌ Draft cancelled.", ephemeral=True)
        self.stop()


# ── Core wizard flow ───────────────────────────────────────────────────────────

async def _pick_storm_template(bot, channel, guild_id: int | None, event_type: str):
    """
    For premium guilds with more than one saved storm mail template, prompt
    leadership to pick which template to use for this draft.

    Returns:
      * a template name string the caller should pass to build_*_mail, or
      * `None` to use the guild's default template, or
      * `False` if the picker timed out and the caller should bail.
    """
    if guild_id is None:
        return None
    import premium
    from config import get_storm_template_names
    if not await premium.is_premium(guild_id, bot=bot):
        return None
    names = get_storm_template_names(guild_id, event_type)
    if len(names) <= 1:
        return None

    class StormTemplatePickView(discord.ui.View):
        def __init__(self, options: list[str]):
            super().__init__(timeout=120)
            self.selected: str | None = None
            sel = discord.ui.Select(
                placeholder="Pick a saved template…",
                options=[discord.SelectOption(label=n[:100], value=n) for n in options],
            )
            async def _cb(inter):
                self.selected = sel.values[0]
                sel.disabled  = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content=f"✅ Template: **{self.selected}**", view=self,
                )
                self.stop()
            sel.callback = _cb
            self.add_item(sel)

    view = StormTemplatePickView(names)
    await channel.send(
        "💎 You have multiple saved templates. Pick one for this draft:",
        view=view,
    )
    await view.wait()
    if view.selected is None:
        await channel.send(
            f"⏰ Template picker timed out. Run `{HUB_COMMAND[event_type]}` "
            f"and click **{HUB_BTN_DRAFT}** to start over."
        )
        return False
    return view.selected


async def run_ds_draft_flow(bot, channel, user, team: str,
                             current_zones: dict, current_subs: list):
    """
    Step 2-4 of the `📄 Generate mail` flow (DS):

      Step 2 — Pick Time
      Step 3 — Show template, choose Use as-is / Edit
               (Edit asks the user to paste the edited block; the parsed
               assignments are saved to the sheet but the mail is **not**
               posted yet.)
      Step 4 — Preview the rendered mail with Post & Copy / Cancel

    `current_zones` / `current_subs` are the team's last saved assignments
    loaded from the sheet — used as the starting template.
    """
    guild_id = getattr(getattr(channel, "guild", None), "id", None)

    # ── Step 2: Pick Time ─────────────────────────────────────────────────────
    time_msg  = await channel.send("**Step 2 of 4: Pick Time**\n⏰ What time is Desert Storm this week?")
    time_view = TimeSelectView(event_type="DS", guild_id=guild_id)
    await time_msg.edit(view=time_view)
    await time_view.wait()
    try:
        await time_msg.delete()
    except discord.HTTPException:
        pass
    if time_view.selected is None:
        await channel.send(
            f"⏰ Timed out. Run `{HUB_COMMAND['DS']}` and click "
            f"**{HUB_BTN_DRAFT}** to start again."
        )
        return
    time_key = time_view.selected

    # ── Step 3: Mail Template — Use as-is or Edit ─────────────────────────────
    stale = _non_canonical_ds_zones(current_zones)
    if stale:
        stale_lines = "\n".join(f"• `{k}`: {v}" for k, v in stale.items())
        await channel.send(
            "ℹ️ Your saved data has zones that aren't on the canonical "
            "Desert Storm list. They'll be dropped on the next save:\n"
            f"{stale_lines}\n"
            "Re-enter assignments under the correct zone name in the "
            "template below."
        )

    template = build_ds_template(current_zones, current_subs)
    use_view = TemplateUseEditView()
    await channel.send(
        f"**Step 3 of 4: Mail Template (Team {team})**\n"
        f"Here is the saved template for **Team {team}**:\n"
        f"```\n{template}\n```\n"
        f"Use it as-is, or edit it before posting?",
        view=use_view,
    )
    await use_view.wait()
    if use_view.choice is None:
        await channel.send(
            f"⏰ Timed out. Run `{HUB_COMMAND['DS']}` and click "
            f"**{HUB_BTN_DRAFT}** to start again."
        )
        return

    zones, subs = current_zones, current_subs

    if use_view.choice == "edit":
        def check(m):
            return m.author == user and m.channel == channel

        prompt = await channel.send(
            f"✏️ {user.mention}, copy the block above, make your edits, and paste it back below.\n"
            f"*(10 minutes to respond; type `cancel` to stop)*"
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=WIZARD_TIMEOUT)
        except asyncio.TimeoutError:
            await channel.send(
            f"⏰ Timed out. Run `{HUB_COMMAND['DS']}` and click "
            f"**{HUB_BTN_DRAFT}** to start again."
        )
            try:
                await prompt.delete()
            except discord.HTTPException:
                pass
            return

        try:
            await prompt.delete()
        except discord.HTTPException:
            pass

        if reply.content.strip().lower() == "cancel":
            await channel.send("❌ Draft cancelled.")
            return

        edited_zones, edited_subs, errors = parse_ds_template(reply.content)
        if not edited_zones:
            await channel.send(
                "⚠️ Could not parse any zone assignments. "
                "Make sure the format matches the template and re-run "
                f"`{HUB_COMMAND['DS']}` → **{HUB_BTN_DRAFT}** to try again."
            )
            return
        if errors:
            await channel.send(
                "⚠️ Some lines were skipped:\n" + "\n".join(f"• {e}" for e in errors)
            )

        zones, subs = edited_zones, edited_subs

        # Save the edited assignments now so they become next week's default,
        # but make it explicit we have NOT posted the mail yet.
        await asyncio.get_event_loop().run_in_executor(
            None, save_ds_assignments, team, zones, subs, guild_id,
        )
        await channel.send(
            f"💾 **Team {team} template saved (not posted).** "
            f"Review the preview below before sending it out."
        )

    # ── Step 4: Preview + Post & Copy ─────────────────────────────────────────
    template_name = await _pick_storm_template(bot, channel, guild_id, "DS")
    if template_name is False:
        return  # picker timed out

    mail = build_ds_mail(
        team, zones, subs, time_key,
        guild_id=guild_id, template_name=template_name,
    )

    from config import get_storm_config
    storm_cfg       = get_storm_config(guild_id, "DS") if guild_id else {}
    post_channel_id = int(storm_cfg.get("post_channel_id") or 0)

    approval_view = StormApprovalView(
        bot=bot, team=team, mail=mail,
        zones=zones, subs=subs, time_key=time_key,
        post_channel_id=post_channel_id,
    )
    await channel.send(
        f"**Step 4 of 4: Preview**\n"
        f"📬 **Desert Storm Team {team} mail preview:**\n\n{mail}\n\nDoes this look right?",
        view=approval_view,
    )


# ── Guards ─────────────────────────────────────────────────────────────────────

async def _guard(interaction: discord.Interaction) -> bool:
    cfg = get_config(interaction.guild_id)
    if not cfg or not cfg.setup_complete:
        await interaction.response.send_message(
            "⚙️ This bot hasn't been set up yet. Run `/setup` to get started.", ephemeral=True
        )
        return False
    if cfg.leadership_role_name not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message(
            f"⛔ You need the **{cfg.leadership_role_name}** role to use this command.", ephemeral=True
        )
        return False
    return True


# ── Slash command handlers ────────────────────────────────────────────────────
#
# Wired from the `📄 Generate mail` button on the `/desertstorm` and
# `/canyonstorm` event hubs (storm_event_hub.py). This module exposes
# the handler bodies so the hub stays a thin dispatcher.


async def handle_storm_draft(bot, interaction: discord.Interaction, event_type: str) -> None:
    if not await _guard(interaction):
        return

    await interaction.response.defer(ephemeral=True)
    channel = interaction.channel
    if channel is None:
        await interaction.followup.send("⚠️ Could not find the channel.", ephemeral=True)
        return

    is_ds = event_type == "DS"
    icon = "🔥" if is_ds else "⚡"
    label = "Desert Storm" if is_ds else "Canyon Storm"
    parent = "desertstorm" if is_ds else "canyonstorm"

    # Step 1: Pick team
    team_msg = await channel.send(
        f"{icon} **{label} Draft** started by {interaction.user.mention}\n\n"
        f"**Step 1 of 4: Pick Team**\nWhich team are you drafting for?"
    )
    team_view = TeamSelectView()
    await team_msg.edit(view=team_view)
    await team_view.wait()
    try:
        await team_msg.delete()
    except discord.HTTPException:
        pass

    if team_view.selected is None:
        event_type = "DS" if is_ds else "CS"
        await channel.send(
            f"⏰ Timed out. Run `{HUB_COMMAND[event_type]}` and click "
            f"**{HUB_BTN_DRAFT}** to start again."
        )
        await interaction.followup.send("⏰ Timed out.", ephemeral=True)
        return

    team = team_view.selected

    # Load the team's saved assignments so they become the starting template.
    if is_ds:
        zones, subs = await asyncio.get_event_loop().run_in_executor(
            None, load_ds_assignments, team, interaction.guild_id,
        )
    else:
        zones = await asyncio.get_event_loop().run_in_executor(
            None, load_cs_assignments, team, interaction.guild_id,
        )

    await interaction.followup.send(f"✅ Team {team} selected.", ephemeral=True)

    # Steps 2-4: Time → Template (Use as-is / Edit) → Preview (Post & Copy)
    if is_ds:
        await run_ds_draft_flow(bot, channel, interaction.user, team, zones, subs)
    else:
        await run_cs_draft_flow(bot, channel, interaction.user, team, zones)


async def handle_storm_overview(bot, interaction: discord.Interaction, event_type: str) -> None:
    if not await _guard(interaction):
        return
    await _show_storm_overview(interaction, event_type)


async def _show_storm_overview(interaction: discord.Interaction, event_type: str):
    """Render the storm config + the current roster mail template for the given event type."""
    await interaction.response.defer(ephemeral=True)
    from config import get_storm_config, get_config

    label    = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    icon     = "⚔️" if event_type == "DS" else "🏜️"
    cmd_name = "desertstorm" if event_type == "DS" else "canyonstorm"
    setup_cmd = "setup_desertstorm" if event_type == "DS" else "setup_canyonstorm"

    cfg  = get_config(interaction.guild_id)
    scfg = get_storm_config(interaction.guild_id, event_type)
    log_channel_id = (cfg.ds_log_channel_id if event_type == "DS" else cfg.cs_log_channel_id) if cfg else 0

    embed = discord.Embed(
        title=f"{icon} {label}",
        color=discord.Color.dark_red() if event_type == "DS" else discord.Color.gold(),
    )
    from config import get_storm_slot_labels
    slot_labels = get_storm_slot_labels(event_type, interaction.guild_id)

    embed.add_field(name="Sheet Tab",   value=scfg.get("tab_name", "*not set*"),                        inline=False)
    embed.add_field(name="Log Channel", value=f"<#{log_channel_id}>" if log_channel_id else "*not set*", inline=False)
    embed.add_field(name="Time Option 1", value=slot_labels[0], inline=False)
    embed.add_field(name="Time Option 2", value=slot_labels[1], inline=False)

    # Build the rendered mail template — same templating used by /[event]_draft
    try:
        if event_type == "DS":
            zones, subs = await asyncio.get_event_loop().run_in_executor(
                None, load_ds_assignments, "A", interaction.guild_id,
            )
            template = build_ds_template(zones, subs)
        else:
            zones = await asyncio.get_event_loop().run_in_executor(
                None, load_cs_assignments, "A", interaction.guild_id,
            )
            template = build_cs_template(zones)
        # Discord field value cap is 1024 chars
        preview = template[:1000] + ("\n…" if len(template) > 1000 else "")
        embed.add_field(name="Current Mail Template (Team A)", value=f"```\n{preview}\n```", inline=False)
    except Exception as e:
        embed.add_field(name="Current Mail Template", value=f"⚠️ Could not load: {e}", inline=False)

    embed.set_footer(text=f"Run /{setup_cmd} to update. Run /{cmd_name} draft to generate a draft.")
    await interaction.followup.send(embed=embed, ephemeral=True)

# ══════════════════════════════════════════════════════════════════════════════
# CANYON STORM (CS)
# ══════════════════════════════════════════════════════════════════════════════



# ── CS Defaults ───────────────────────────────────────────────────────────────

# Empty placeholders — alliances fill these via the `📄 Generate mail`
# button on `/canyonstorm`.
# Keys must match the canonical CS_ZONE_STRUCTURE (defined later in this
# file) plus the CS_SUBS_KEY for {subs}; the existing test suite asserts
# that property, so any key drift will fail loudly in CI.
DEFAULT_CS_B = {
    "s1_power_tower":    "",
    "s1_dc1":            "",
    "s1_dc2":            "",
    "s1_sw1":            "",
    "s1_sw2":            "",
    "s1_sw3":            "",
    "s1_sw4":            "",
    "s2_ds1":            "",
    "s2_ds2":            "",
    "s2_sf1":            "",
    "s2_sf2":            "",
    "s3_virus_lab":      "",
    "s3_power_tower":    "",
    "s3_dc1":            "",
    "s3_dc2":            "",
    "s3_ds1":            "",
    "s3_ds2":            "",
    "s3_sf1":            "",
    "s3_sf2":            "",
    "s3_pop_pair1":      "",
}

DEFAULT_CS_A = {k: "" for k in DEFAULT_CS_B}

CS_DEFAULTS = {"A": DEFAULT_CS_A, "B": DEFAULT_CS_B}

# ── CS Sheets persistence ──────────────────────────────────────────────────────

def load_cs_assignments(team: str, guild_id: int = None) -> dict:
    zone_key = f"CS_{team}_ZONES"
    try:
        from config import get_config
        cfg = get_config(guild_id)
        sh   = _get_spreadsheet(guild_id)
        ws   = sh.worksheet(cfg.tab_ds_assignments if cfg else "DS Assignments")
        rows = ws.get_all_values()
        zones   = {}
        section = None
        for row in rows:
            if not row or not row[0].strip():
                continue
            key = row[0].strip()
            if key == zone_key:
                section = "zones"
                continue
            if key.startswith("CS_") or key.startswith("DS_"):
                if key != zone_key:
                    section = None
                    continue
            if section == "zones" and len(row) >= 2:
                raw = row[1].strip()
                if key == CS_SUBS_KEY:
                    # CS subs flatten to a list[str] in memory. Legacy
                    # inline strings (commas / dashes / ampersands) split
                    # via the shared helper so older saved data round-trips
                    # cleanly with the new multi-line `Subs` template.
                    zones[key] = _split_legacy_subs(raw) if raw else []
                else:
                    zones[key] = raw
        if zones:
            print(f"[STORM] Loaded CS Team {team} assignments ({len(zones)} zones)")
            return zones
        else:
            print(f"[STORM] No saved CS Team {team} assignments — using defaults")
            return dict(CS_DEFAULTS[team])
    except Exception as e:
        from config import describe_sheet_error
        print(
            f"[STORM] Error loading CS Team {team} assignments: "
            f"{describe_sheet_error(e, guild_id=guild_id, tab='DS Assignments')}"
        )
        return dict(CS_DEFAULTS[team])


def save_cs_assignments(team: str, zones: dict, guild_id: int = None):
    """Save CS assignments for one team without affecting DS or the other CS team."""
    try:
        from config import get_config
        cfg = get_config(guild_id) if guild_id else None
        sh  = _get_spreadsheet(guild_id)
        ws  = sh.worksheet(cfg.tab_ds_assignments if cfg else "DS Assignments")
        existing = ws.get_all_values()

        # Rebuild full sheet: preserve all DS and CS rows, replace this team's CS section
        other_cs = "B" if team == "A" else "A"
        other_cs_zones = load_cs_assignments(other_cs, guild_id=guild_id)
        ds_a_zones, ds_a_subs = load_ds_assignments("A", guild_id=guild_id)
        ds_b_zones, ds_b_subs = load_ds_assignments("B", guild_id=guild_id)

        rows = []
        for t, t_zones, t_subs in [("A", ds_a_zones, ds_a_subs), ("B", ds_b_zones, ds_b_subs)]:
            rows.append([f"DS_{t}_ZONES", ""])
            for z, m in t_zones.items():
                rows.append([z, m])
            rows.append(["", ""])
            rows.append([f"DS_{t}_SUBS", ""])
            for sub in t_subs:
                if isinstance(sub, tuple) and len(sub) >= 2:
                    name = str(sub[1])
                else:
                    name = str(sub)
                if name:
                    rows.append([name])
            rows.append(["", ""])

        for t, t_zones in [("A", zones if team == "A" else other_cs_zones),
                            ("B", zones if team == "B" else other_cs_zones)]:
            rows.append([f"CS_{t}_ZONES", ""])
            for z, m in t_zones.items():
                if isinstance(m, list):
                    m = ", ".join(str(x) for x in m if x)
                rows.append([z, m])
            rows.append(["", ""])

        ws.clear()
        ws.update("A1", rows, value_input_option="USER_ENTERED")
        print(f"[STORM] CS Team {team} assignments saved ({len(zones)} zones)")
    except Exception as e:
        from config import describe_sheet_error
        print(
            f"[STORM] Error saving CS Team {team} assignments: "
            f"{describe_sheet_error(e, guild_id=guild_id, tab='DS Assignments')}"
        )


# ── CS Template builder & parser ───────────────────────────────────────────────

# Canonical CS zone layout: each entry is (stage, key, label) in display order.
# Single source of truth for build_cs_template, parse_cs_template, and
# build_cs_mail. Excludes 's3_pop_pair1' which is handled separately as the
# {subs} placeholder rather than a zone.
CS_ZONE_STRUCTURE: list[tuple[int, str, str]] = [
    (1, "s1_power_tower", "Power Tower"),
    (1, "s1_dc1",         "Data Center 1"),
    (1, "s1_dc2",         "Data Center 2"),
    (1, "s1_sw1",         "Sample Warehouse 1"),
    (1, "s1_sw2",         "Sample Warehouse 2"),
    (1, "s1_sw3",         "Sample Warehouse 3"),
    (1, "s1_sw4",         "Sample Warehouse 4"),
    (2, "s2_ds1",         "Defense System 1"),
    (2, "s2_ds2",         "Defense System 2"),
    (2, "s2_sf1",         "Serum Factory 1"),
    (2, "s2_sf2",         "Serum Factory 2"),
    (3, "s3_virus_lab",   "Virus Lab"),
    (3, "s3_power_tower", "Power Tower"),
    (3, "s3_dc1",         "Data Center 1"),
    (3, "s3_dc2",         "Data Center 2"),
    (3, "s3_ds1",         "Defense System 1"),
    (3, "s3_ds2",         "Defense System 2"),
    (3, "s3_sf1",         "Serum Factory 1"),
    (3, "s3_sf2",         "Serum Factory 2"),
]

# Key used as the {subs} placeholder rather than rendered as a zone.
# The sheet storage key stays `s3_pop_pair1` for backward compatibility with
# existing alliance sheets; only the human-facing label was renamed to `Subs`
# in #37 (the prior `Pop Pairs (last 30 sec)` wasn't game terminology — that
# was a label one alliance carried over to their mail templates).
CS_SUBS_KEY = "s3_pop_pair1"
CS_SUBS_LABEL = "Subs"
# Legacy label retained as a parser alias so older saved templates still
# round-trip without manual reformatting.
CS_SUBS_LEGACY_LABELS = {"pop pairs (last 30 sec)", "pop pairs"}


def build_cs_template(z: dict) -> str:
    """Render the editable CS template.

    Zones render per-stage with `Label: members` lines. Subs render as a
    multi-line section below the `Subs` header (one name per line),
    matching the DS template's `SUBS` shape post-#37. Legacy in-memory
    string values flatten via `_split_legacy_subs` so older saved data
    still renders cleanly without manual reformatting.
    """
    lines: list[str] = []
    last_stage: int | None = None
    for stage, key, label in CS_ZONE_STRUCTURE:
        if stage != last_stage:
            if last_stage is not None:
                lines.append("")
            lines.append(f"STAGE {stage}")
            last_stage = stage
        lines.append(f"{label}: {z.get(key, '')}")
    # Subs as a multi-line section after the stages.
    lines.append("")
    lines.append(CS_SUBS_LABEL)
    subs_value = z.get(CS_SUBS_KEY, "")
    if isinstance(subs_value, list):
        for name in subs_value:
            if name:
                lines.append(str(name))
    elif subs_value:
        for name in _split_legacy_subs(str(subs_value)):
            lines.append(name)
    return "\n".join(lines)


def parse_cs_template(text: str) -> tuple[dict, list]:
    """Parse the edited CS template. Returns (zones, errors).

    Zones are returned keyed by canonical CS_ZONE_STRUCTURE keys. Subs come
    back at ``zones[CS_SUBS_KEY]`` as a ``list[str]``. The `Subs` section
    header (no colon, multi-line below) is the canonical form; legacy
    headers `Pop Pairs (last 30 sec)` and `Pop Pairs` are still accepted,
    as is the legacy inline form `<header>: <names>` which flattens via
    `_split_legacy_subs` (commas, dashes, ampersands all split).
    """
    zones: dict = {}
    errors: list[str] = []
    stage = None
    section = None  # None | "subs"
    subs_list: list[str] = []

    # Build the (label_lower, stage) → key lookup from the canonical structure
    # so the parser stays in sync with the builder.
    key_map: dict[str, dict[int, str]] = {}
    for st, key, label in CS_ZONE_STRUCTURE:
        key_map.setdefault(label.lower(), {})[st] = key

    subs_header_labels = {CS_SUBS_LABEL.lower()} | CS_SUBS_LEGACY_LABELS

    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        if line.upper() == "STAGE 1":
            stage = 1; section = None; continue
        if line.upper() == "STAGE 2":
            stage = 2; section = None; continue
        if line.upper() == "STAGE 3":
            stage = 3; section = None; continue

        # Subs section header (no colon): `Subs` or legacy `Pop Pairs …`.
        if line.lower() in subs_header_labels:
            section = "subs"
            stage = None
            continue

        if ":" in line:
            label, _, value = line.partition(":")
            label_lower = label.strip().lower()
            # Legacy inline subs (e.g. `Pop Pairs (last 30 sec): Alice & Bob`).
            if label_lower in subs_header_labels:
                subs_list.extend(_split_legacy_subs(value))
                section = None
                stage = None
                continue
            if label_lower in key_map and stage in key_map[label_lower]:
                field = key_map[label_lower][stage]
                zones[field] = value.strip()
            else:
                errors.append(f"Unrecognized line in Stage {stage}: {line}")
        elif section == "subs":
            subs_list.append(line)
        else:
            errors.append(f"Could not parse: {line}")

    zones[CS_SUBS_KEY] = subs_list
    return zones, errors


# ── CS Mail builder ────────────────────────────────────────────────────────────

def build_cs_mail(team: str, z: dict, time_key: str, guild_id: int = None,
                  template_name: str | None = None) -> str:
    """Build CS mail using a guild's stored template (named or default)."""
    from config import (
        get_storm_template, format_storm_slot, get_storm_slot_for_key,
    )
    if guild_id:
        template = get_storm_template(guild_id, "CS", template_name)
    else:
        from config import get_storm_config
        template = (get_storm_config(guild_id, "CS") or {}).get("mail_template") or ""

    slot = get_storm_slot_for_key("CS", time_key) if guild_id else None
    if slot is not None:
        h, m = slot
        time_str = format_storm_slot(h, m, guild_id)
    else:
        time_str = time_key

    # Build zones block in canonical order with full labels and stage headers.
    # Walk CS_ZONE_STRUCTURE first so familiar slots render with their proper
    # names ("Data Center 1", not "Dc1"). Anything left in `z` that isn't part
    # of the canonical structure (legacy data, custom test fixtures) is then
    # emitted at the end with the raw key as a fallback label, so nothing ever
    # silently disappears.
    zone_lines: list[str] = []
    last_stage: int | None = None
    rendered: set[str] = set()

    def _emit_members(members):
        if isinstance(members, list):
            zone_lines.append("\n".join(str(m) for m in members))
        else:
            zone_lines.append(str(members))
        zone_lines.append("")

    from storm_icons import zone_emoji_prefix
    for stage, key, label in CS_ZONE_STRUCTURE:
        members = z.get(key)
        if not members or members == "(open)":
            rendered.add(key)
            continue
        if stage != last_stage:
            # `_emit_members` already trailed a blank, so the next stage header
            # only needs one extra blank — no double-blank between stages.
            zone_lines.append(f"**Stage {stage}**")
            last_stage = stage
        # #158: prefix the zone header with its emoji. No-op until the
        # emojis upload.
        zone_lines.append(f"{zone_emoji_prefix(label)}**{label}**")
        _emit_members(members)
        rendered.add(key)

    # Fallback for non-canonical keys (e.g. legacy fixtures). Skip the subs key.
    rendered.add(CS_SUBS_KEY)
    extra = [(k, v) for k, v in z.items() if k not in rendered and v and v != "(open)"]
    if extra:
        if zone_lines:
            zone_lines.append("")
        for key, members in extra:
            zone_lines.append(f"{zone_emoji_prefix(key)}**{key}**")
            _emit_members(members)

    zones_block = "\n".join(zone_lines).strip()

    if isinstance(z.get(CS_SUBS_KEY), list):
        subs_block = "\n".join(str(s) for s in z[CS_SUBS_KEY])
    else:
        subs_block = z.get(CS_SUBS_KEY, "(none)") or "(none)"

    if template:
        return template.format(
            alliance_name="Alliance",
            zones=zones_block,
            subs=subs_block,
            time=time_str,
        )

    return "\n".join([
        "**Canyon Storm**",
        "",
        "**Zone Assignments**",
        zones_block,
        "",
        "**Subs**",
        subs_block,
        "",
        f"**Time:** {time_str}",
    ])


# ── CS Approval view ───────────────────────────────────────────────────────────

class CSApprovalView(discord.ui.View):
    def __init__(self, bot, team: str, mail: str, zones: dict, time_key: str,
                 post_channel_id: int = 0):
        super().__init__(timeout=3600)
        self.bot             = bot
        self.team            = team
        self.mail            = mail
        self.zones           = zones
        self.time_key        = time_key
        self.post_channel_id = post_channel_id

    async def _disable(self, interaction: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="✅ Looks Good: Post & Copy", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable(interaction)
        await _post_and_copy(
            interaction.channel, self.post_channel_id,
            "Canyon Storm", self.team, self.mail,
        )
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self._disable(interaction)
        await interaction.followup.send("❌ Draft cancelled.", ephemeral=True)
        self.stop()


# ── CS Core wizard flow ────────────────────────────────────────────────────────

async def run_cs_draft_flow(bot, channel, user, team: str, current_zones: dict):
    """
    Step 2-4 of the `📄 Generate mail` flow (CS): Time → Template (Use as-is / Edit) →
    Preview (Post & Copy). See `run_ds_draft_flow` for the shape.
    """
    guild_id = getattr(getattr(channel, "guild", None), "id", None)

    # ── Step 2: Pick Time ─────────────────────────────────────────────────────
    time_msg  = await channel.send("**Step 2 of 4: Pick Time**\n⏰ What time is Canyon Storm this week?")
    time_view = TimeSelectView(event_type="CS", guild_id=guild_id)
    await time_msg.edit(view=time_view)
    await time_view.wait()
    try:
        await time_msg.delete()
    except discord.HTTPException:
        pass
    if time_view.selected is None:
        await channel.send(
            f"⏰ Timed out. Run `{HUB_COMMAND['CS']}` and click "
            f"**{HUB_BTN_DRAFT}** to start again."
        )
        return
    time_key = time_view.selected

    # ── Step 3: Mail Template — Use as-is or Edit ─────────────────────────────
    stale = _non_canonical_cs_zones(current_zones)
    if stale:
        stale_lines = "\n".join(f"• `{k}`: {v}" for k, v in stale.items())
        await channel.send(
            "ℹ️ Your saved data has zones that aren't on the canonical "
            "Canyon Storm list. They'll be dropped on the next save:\n"
            f"{stale_lines}\n"
            "Re-enter assignments under the correct zone name in the "
            "template below."
        )

    template = build_cs_template(current_zones)
    use_view = TemplateUseEditView()
    await channel.send(
        f"**Step 3 of 4: Mail Template (Team {team})**\n"
        f"Here is the saved template for **Team {team}**:\n"
        f"```\n{template}\n```\n"
        f"Use it as-is, or edit it before posting?",
        view=use_view,
    )
    await use_view.wait()
    if use_view.choice is None:
        await channel.send(
            f"⏰ Timed out. Run `{HUB_COMMAND['CS']}` and click "
            f"**{HUB_BTN_DRAFT}** to start again."
        )
        return

    zones = current_zones

    if use_view.choice == "edit":
        def check(m):
            return m.author == user and m.channel == channel

        prompt = await channel.send(
            f"✏️ {user.mention}, copy the block above, make your edits, and paste it back below.\n"
            f"*(10 minutes to respond; type `cancel` to stop)*"
        )
        try:
            reply = await bot.wait_for("message", check=check, timeout=WIZARD_TIMEOUT)
        except asyncio.TimeoutError:
            await channel.send(
            f"⏰ Timed out. Run `{HUB_COMMAND['CS']}` and click "
            f"**{HUB_BTN_DRAFT}** to start again."
        )
            try:
                await prompt.delete()
            except discord.HTTPException:
                pass
            return

        try:
            await prompt.delete()
        except discord.HTTPException:
            pass

        if reply.content.strip().lower() == "cancel":
            await channel.send("❌ Draft cancelled.")
            return

        edited_zones, errors = parse_cs_template(reply.content)
        if not edited_zones:
            await channel.send(
                "⚠️ Could not parse any assignments. "
                "Make sure the format matches the template and re-run "
                f"`{HUB_COMMAND['CS']}` → **{HUB_BTN_DRAFT}** to try again."
            )
            return
        if errors:
            await channel.send(
                "⚠️ Some lines were skipped:\n" + "\n".join(f"• {e}" for e in errors)
            )

        zones = edited_zones

        await asyncio.get_event_loop().run_in_executor(
            None, save_cs_assignments, team, zones, guild_id,
        )
        await channel.send(
            f"💾 **Team {team} template saved (not posted).** "
            f"Review the preview below before sending it out."
        )

    # ── Step 4: Preview + Post & Copy ─────────────────────────────────────────
    template_name = await _pick_storm_template(bot, channel, guild_id, "CS")
    if template_name is False:
        return

    mail = build_cs_mail(
        team, zones, time_key,
        guild_id=guild_id, template_name=template_name,
    )

    from config import get_storm_config
    storm_cfg       = get_storm_config(guild_id, "CS") if guild_id else {}
    post_channel_id = int(storm_cfg.get("post_channel_id") or 0)

    approval_view = CSApprovalView(
        bot=bot, team=team, mail=mail, zones=zones,
        time_key=time_key, post_channel_id=post_channel_id,
    )
    await channel.send(
        f"**Step 4 of 4: Preview**\n"
        f"📬 **Canyon Storm Team {team} mail preview:**\n\n{mail}\n\nDoes this look right?",
        view=approval_view,
    )
