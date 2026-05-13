"""
Manual roster builder for Desert Storm and Canyon Storm (#128).

`/ds_strategy apply name:<preset>` (and the CS equivalent) opens an
interactive roster builder. Leadership picks the team, the bot loads
the named preset + member rules + roster powers, and the builder
enforces per-zone power floors as members are assigned.

v1 scope:
  * Manual assignment via zone + member dropdowns (no auto-fill)
  * Eligibility gate filters the member picker by the active team's
    per-zone power floor; below-floor members surface via an explicit
    override toggle
  * Power-band Member Rules + per-member zone rules pre-applied at
    session open
  * Subs in `pool` mode only — paired-mode UI deferred
  * Generate text-template mail via `build_ds_mail` / `build_cs_mail`
  * Save current roster as a preset (delegates to storm_strategy.save_preset)

Deferred to follow-ups:
  * Pillow PNG render
  * `sub_mode=paired` inline sub picker
  * Premium auto-fill from preset + greedy power fill (that's #129's
    Premium variant)
"""

from __future__ import annotations

import logging
from typing import Optional

import discord

logger = logging.getLogger(__name__)


# ── Power-data reader ────────────────────────────────────────────────────────
#
# Pulls each member's configured power value off the alliance roster
# Sheet so the eligibility gate has something to filter against. Reads
# the column header configured at `/setup_desertstorm` (or canyon)
# under "Power Metric Column" — falling back to the design rule of
# "exclude unknown power, never silently coerce to zero."

def _read_roster_powers(
    guild_id: int, event_type: str,
) -> tuple[dict[str, dict], list[str]]:
    """Read the alliance's roster Sheet and return:

        ({key: {"name": str, "discord_id": str, "power": int | None,
                "not_on_discord": bool}, ...},
         errors)

    `key` is a stable lookup string — Discord ID when present (for the
    common case), the roster name otherwise (for non-Discord members).
    The same key is used by `target_member_id` in storm_signups, so the
    roster builder can resolve a vote row to a roster entry without a
    second lookup.

    `power` is `None` when the configured Power Metric Column is missing
    from the Sheet, or the cell doesn't parse as a power value. The
    builder treats `None` as "below any floor" — it surfaces the
    member with a "power unknown" label and only the explicit override
    toggle assigns them.

    Errors are returned soft so the slash command can surface a one-line
    warning without aborting the builder entirely.
    """
    import config
    from storm_strategy import parse_power

    errors: list[str] = []

    try:
        roster_cfg = config.get_member_roster_config(guild_id)
    except Exception as e:
        return {}, [f"roster-config read failed: {e}"]

    try:
        structured = config.get_structured_storm_config(guild_id, event_type)
    except Exception as e:
        return {}, [f"structured-config read failed: {e}"]

    power_col_name = (structured.get("power_column_name") or "").strip()
    if not power_col_name:
        errors.append(
            "no power metric column configured — every member will read as "
            "'power unknown'. Run /setup_desertstorm or /setup_canyonstorm "
            "to set the Power Metric Column."
        )

    if not roster_cfg.get("enabled"):
        errors.append(
            "member-roster sync isn't enabled — without /sync_members the "
            "builder can't see your alliance's roster."
        )
        return {}, errors

    try:
        ws = config.get_member_roster_sheet(
            guild_id, roster_cfg.get("tab_name") or "Member Roster",
        )
    except Exception as e:
        errors.append(f"roster-sheet open failed: {e}")
        return {}, errors

    try:
        values = ws.get_all_values()
    except Exception as e:
        errors.append(f"roster-sheet read failed: {e}")
        return {}, errors

    if not values:
        return {}, errors

    header = [c.strip() for c in values[0]]

    def _find_col(name: str) -> int:
        target = name.strip().lower()
        for idx, cell in enumerate(header):
            if cell.strip().lower() == target:
                return idx
        return -1

    id_col      = int(roster_cfg.get("discord_id_col", 0))
    name_col    = int(roster_cfg.get("display_col", roster_cfg.get("name_col", 1)))
    power_col   = _find_col(power_col_name) if power_col_name else -1
    not_disc_col = _find_col("not_on_discord")
    if not_disc_col < 0:
        not_disc_col = _find_col("not on discord")

    if power_col_name and power_col < 0:
        errors.append(
            f"power column '{power_col_name}' not found in your roster Sheet "
            f"header. Add it (or change the configured column at setup) so "
            f"the eligibility gate has something to read."
        )

    truthy = {"1", "true", "yes", "y", "x", "t"}
    members: dict[str, dict] = {}
    for row in values[1:]:
        def _cell(idx: int) -> str:
            if idx < 0 or idx >= len(row):
                return ""
            return str(row[idx]).strip()

        discord_id = _cell(id_col)
        name       = _cell(name_col)
        if not (discord_id or name):
            continue

        # Parse the power cell. Blank → None (not zero). Garbage → None
        # plus a single log warning; we don't surface every row as an
        # error to leadership.
        power_val: Optional[int] = None
        if power_col >= 0:
            raw_power = _cell(power_col)
            if raw_power:
                parsed = parse_power(raw_power)
                if parsed is None:
                    logger.warning(
                        "[STORM ROSTER] couldn't parse power %r for member %r "
                        "(guild=%s event=%s)",
                        raw_power, name or discord_id, guild_id, event_type,
                    )
                else:
                    power_val = int(parsed)

        not_on_discord = (
            _cell(not_disc_col).lower() in truthy if not_disc_col >= 0 else False
        )

        key = discord_id or name
        if not key:
            continue
        members[key] = {
            "key":            key,
            "name":           name or discord_id,
            "discord_id":     discord_id,
            "power":          power_val,
            "not_on_discord": not_on_discord,
        }

    return members, errors


# ── Session state ────────────────────────────────────────────────────────────


class RosterBuilderSession:
    """In-memory state for one officer's roster build. Lives on the
    View; not persisted (Discord interaction tokens expire in 15 min
    anyway — resuming would mean reloading from scratch).

    `event_date` is None in free-tier "manual apply" mode (the officer
    is building a roster for whatever event they're prepping; they
    copy the mail themselves). When set, the session is in structured
    mode (#129) — the member pool is already pre-filtered to signed-up
    members for this team, and finalisation posts the mail to the
    configured channel AND writes a row per slot to `rosters_tab`.
    """

    def __init__(
        self,
        guild_id: int,
        user_id: int,
        event_type: str,
        team: str,                              # "A" / "B" / "" (CS uses faction)
        preset,                                 # storm_strategy.PresetBuffer
        members: dict[str, dict],
        per_member_rules: list,
        power_band_rules: list,
        *,
        event_date: Optional[str] = None,
    ):
        self.guild_id = guild_id
        self.user_id  = user_id
        self.event_type = event_type
        self.team     = team
        self.preset   = preset
        self.members  = members
        self.event_date = event_date
        # Per-zone assignments: {zone_name: [member_key, ...]}
        self.assignments: dict[str, list[str]] = {z.zone: [] for z in preset.zones}
        # Flat sub pool (sub_mode=pool; paired UI deferred).
        self.subs: list[str] = []
        # The currently-selected zone in the UI; defaults to the first zone.
        self.selected_zone: str = preset.zones[0].zone if preset.zones else ""
        # Officer-toggled override: show below-floor members in the
        # picker for the current zone. Resets to off on zone change.
        self.show_below_floor: bool = False
        self.per_member_rules = per_member_rules
        self.power_band_rules = power_band_rules
        # Errors surfaced from the roster read; the builder shows a
        # one-line warning when any are present.
        self.roster_errors: list[str] = []

    @property
    def is_structured(self) -> bool:
        return bool(self.event_date)

    def floor_for_zone(self, zone_name: str) -> int:
        """Per-team min_power for this zone. DS uses min_power_a/b; CS
        uses min_power_a as the single floor (storm_strategy stores it
        there for CS too)."""
        z = self.preset.find_zone(zone_name)
        if z is None:
            return 0
        if self.event_type == "DS" and self.team == "B":
            return int(z.min_power_b or 0)
        return int(z.min_power_a or 0)

    def assigned_member_keys(self) -> set[str]:
        """Every member currently slotted somewhere (any zone or subs)."""
        keys: set[str] = set()
        for zone_members in self.assignments.values():
            keys.update(zone_members)
        keys.update(self.subs)
        return keys

    def zone_member_count(self, zone_name: str) -> int:
        return len(self.assignments.get(zone_name, []))

    def zone_capacity(self, zone_name: str) -> int:
        z = self.preset.find_zone(zone_name)
        return int(z.max_players) if z else 0


def _apply_rules_to_session(session: RosterBuilderSession) -> None:
    """Pre-assign members based on Member Rules before the officer
    starts manual work. Only fires at session open.

    Surfaces a soft warning into `session.roster_errors` for per_member
    rules whose subject doesn't match any roster row — usually a member
    rename between rule creation and apply. Without this warning the
    rule silently no-ops and leadership has no idea why their rule isn't
    firing.
    """
    unmatched_subjects: list[str] = []

    # per_member zone rules — pin a specific member to a specific zone
    # if they exist on the roster.
    for rule in session.per_member_rules:
        if rule.sub_type != "zone":
            continue
        # Resolve subject (member name) → roster key
        subject = rule.subject.strip()
        match_key = None
        for k, m in session.members.items():
            if m["name"].strip().lower() == subject.lower():
                match_key = k
                break
        if match_key is None:
            if subject and subject not in unmatched_subjects:
                unmatched_subjects.append(subject)
            continue
        zone = rule.value.strip()
        if not session.preset.find_zone(zone):
            continue
        # Skip if zone is already full or member already assigned somewhere.
        if session.zone_member_count(zone) >= session.zone_capacity(zone):
            continue
        if match_key in session.assigned_member_keys():
            continue
        session.assignments[zone].append(match_key)

    # Surface unmatched per_member rules so leadership knows to clean
    # them up. Cap the list to keep the embed legible.
    if unmatched_subjects:
        preview = ", ".join(unmatched_subjects[:5])
        extra = f" (+{len(unmatched_subjects) - 5} more)" if len(unmatched_subjects) > 5 else ""
        session.roster_errors.append(
            f"per_member rule(s) reference roster names that aren't in the "
            f"current roster — rename or remove them: {preview}{extra}"
        )


# ── Embed rendering ──────────────────────────────────────────────────────────


def _format_member_label(member: dict) -> str:
    name = member["name"]
    power = member.get("power")
    if power is None:
        suffix = " (power unknown)"
    else:
        from storm_strategy import format_power
        suffix = f" ({format_power(power)})"
    if member.get("not_on_discord"):
        suffix += " ¹"
    return f"{name}{suffix}"


def _render_zone_line(session: RosterBuilderSession, zone_name: str) -> str:
    z = session.preset.find_zone(zone_name)
    if z is None:
        return f"• {zone_name} (?/?)"
    count = session.zone_member_count(zone_name)
    cap   = int(z.max_players)
    if count == 0:
        status = "⬜"
    elif count < cap:
        status = "🟡"
    else:
        status = "✅"
    member_keys = session.assignments.get(zone_name, [])
    names = []
    for k in member_keys:
        m = session.members.get(k)
        if m:
            names.append(m["name"])
        else:
            # Member was on roster at session open but isn't now (rare).
            names.append(f"<unknown:{k}>")
    if names:
        names_part = ", ".join(names)
    else:
        names_part = "(empty)"
    marker = " ←" if zone_name == session.selected_zone else ""
    return f"{status} **{zone_name}** ({count}/{cap}){marker}: {names_part}"


def _render_builder_embed(session: RosterBuilderSession) -> discord.Embed:
    event_label = "Desert Storm" if session.event_type == "DS" else "Canyon Storm"
    if session.event_type == "DS":
        team_label = f" — Team {session.team}"
    elif session.preset.faction and session.preset.faction != "Either":
        team_label = f" — {session.preset.faction}"
    else:
        team_label = ""
    title = f"🛡️ Roster Builder: {session.preset.name}{team_label}"

    lines: list[str] = []
    lines.append(f"🗺️ {event_label}")
    if session.event_type == "DS":
        floor_label = "Min A" if session.team == "A" else "Min B"
        lines.append(f"⚖️ Enforcing **{floor_label}** floors for this team")
    lines.append("")
    lines.append("**📋 Zones**")
    for z in session.preset.zones:
        lines.append(_render_zone_line(session, z.zone))
    lines.append("")
    sub_names = [session.members[k]["name"] for k in session.subs if k in session.members]
    if sub_names:
        lines.append(f"🪑 **Subs ({len(sub_names)})**: {', '.join(sub_names)}")
    else:
        lines.append("🪑 **Subs**: _(none)_")
    lines.append("")

    total_assigned = sum(len(v) for v in session.assignments.values())
    total_capacity = sum(int(z.max_players) for z in session.preset.zones)
    lines.append(f"📊 **Filled:** {total_assigned} / {total_capacity}")

    selected = session.selected_zone
    if selected:
        preset_floor = session.floor_for_zone(selected)
        effective_floor = _effective_floor_for_zone(session, selected)
        from storm_strategy import format_power
        if effective_floor != preset_floor:
            # A power_band Member Rule lowered the effective floor for
            # this zone — surface both so leadership can tell at a
            # glance which rule is in play.
            lines.append(
                f"🎯 **Active zone:** **{selected}** — floor "
                f"**{format_power(effective_floor) if effective_floor else '(none)'}** "
                f"_(preset floor {format_power(preset_floor)} relaxed by power_band rule)_"
            )
        else:
            lines.append(
                f"🎯 **Active zone:** **{selected}** — floor "
                f"**{format_power(effective_floor) if effective_floor else '(none)'}**"
            )
        if session.show_below_floor:
            lines.append("👁️ Below-floor members visible in the picker.")
    has_unknown = any(m.get("power") is None for m in session.members.values())
    if has_unknown:
        lines.append(
            "_Members with no parseable power read as 'power unknown'; "
            "toggle the override to assign them anyway._"
        )

    if session.roster_errors:
        lines.append("")
        lines.append("⚠️ " + session.roster_errors[0])

    embed = discord.Embed(
        title=title,
        description="\n".join(lines),
        color=discord.Color.gold() if session.event_type == "DS" else discord.Color.orange(),
    )
    if any(m.get("not_on_discord") for m in session.members.values()):
        embed.set_footer(text="¹ Not on Discord")
    return embed


# ── Eligibility helpers ──────────────────────────────────────────────────────


def _effective_floor_for_zone(
    session: RosterBuilderSession, zone_name: str,
) -> int:
    """The power threshold a member must meet to be eligible for this
    zone, accounting for both the preset's per-team floor AND any
    `power_band` Member Rules that apply.

    Semantics: a `power_band` rule of "≥ X → Zone Y" *grants*
    eligibility to members at or above X for Zone Y. If multiple bands
    apply to the same zone, the LOWEST threshold wins (most permissive
    is what leadership intended when adding it). If any band's
    threshold is lower than the preset's floor, the band effectively
    lowers the floor for that zone. The reverse — a band stricter than
    the preset — has no effect, because the preset floor is already
    the gate.
    """
    preset_floor = session.floor_for_zone(zone_name)
    band_floor: Optional[int] = None
    target = zone_name.strip().lower()
    for band in session.power_band_rules:
        if band.value.strip().lower() != target:
            continue
        try:
            threshold = int(band.subject)
        except (TypeError, ValueError):
            continue
        if band_floor is None or threshold < band_floor:
            band_floor = threshold
    if band_floor is None:
        return preset_floor
    return min(preset_floor, band_floor)


def _eligible_member_keys_for_zone(
    session: RosterBuilderSession, zone_name: str,
) -> tuple[list[str], list[str]]:
    """Return (eligible_keys, below_floor_keys). Both exclude already-
    assigned members. `eligible` excludes power-unknown unless
    `show_below_floor` is on; below-floor is included only when the
    override toggle is on.

    The effective floor is the lower of (preset floor, lowest matching
    power_band rule threshold) — so power_band rules can grant
    eligibility paths that the preset alone wouldn't permit. See
    `_effective_floor_for_zone` for the rationale.
    """
    floor = _effective_floor_for_zone(session, zone_name)
    assigned = session.assigned_member_keys()
    eligible: list[str] = []
    below: list[str] = []
    for key, m in session.members.items():
        if key in assigned:
            continue
        power = m.get("power")
        if power is None:
            below.append(key)
            continue
        if power >= floor:
            eligible.append(key)
        else:
            below.append(key)
    # Sort eligible high-power-first so officers see strongest options at top.
    eligible.sort(
        key=lambda k: -(session.members[k].get("power") or 0),
    )
    below.sort(
        key=lambda k: -(session.members[k].get("power") or 0),
    )
    return eligible, below


# ── Builder View ─────────────────────────────────────────────────────────────


_MAX_DROPDOWN_OPTIONS = 25  # Discord limit per Select


class RosterBuilderView(discord.ui.View):
    """Stateful builder UI. State lives on `self.session`. The view
    rebuilds its components every time state changes so dropdown
    options reflect the current zone + eligibility."""

    def __init__(self, session: RosterBuilderSession):
        super().__init__(timeout=900)
        self.session = session
        self.message: Optional[discord.Message] = None
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        s = self.session

        # Row 0 — zone selector
        if s.preset.zones:
            zone_options = [
                discord.SelectOption(
                    label=f"{z.zone} ({s.zone_member_count(z.zone)}/{int(z.max_players)})"[:100],
                    value=z.zone[:100],
                    default=(z.zone == s.selected_zone),
                )
                for z in s.preset.zones[:_MAX_DROPDOWN_OPTIONS]
            ]
            zone_select = discord.ui.Select(
                placeholder="Pick a zone to edit…",
                min_values=1, max_values=1,
                options=zone_options,
            )

            async def _on_zone(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                s.selected_zone = zone_select.values[0]
                s.show_below_floor = False
                await self._refresh(inter)

            zone_select.callback = _on_zone
            self.add_item(zone_select)

        # Row 1 — member picker (eligibility-gated)
        eligible, below = _eligible_member_keys_for_zone(s, s.selected_zone)
        pool = list(eligible)
        if s.show_below_floor:
            pool.extend(below)
        if pool:
            options: list[discord.SelectOption] = []
            seen_values: set[str] = set()
            for k in pool[:_MAX_DROPDOWN_OPTIONS]:
                m = s.members[k]
                value = k[:100]
                if value in seen_values:
                    continue
                seen_values.add(value)
                label = _format_member_label(m)[:100]
                is_below = k in below
                description = "below floor" if is_below else None
                options.append(discord.SelectOption(
                    label=label, value=value, description=description,
                ))
            placeholder = (
                f"Pick a member for {s.selected_zone or 'a zone'}…"
                if eligible or s.show_below_floor else
                "No eligible members — toggle below-floor override"
            )
            member_select = discord.ui.Select(
                placeholder=placeholder[:150],
                min_values=1, max_values=1,
                options=options,
            )

            async def _on_member(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                if not s.selected_zone:
                    await inter.response.send_message(
                        "⚠️ Pick a zone first.", ephemeral=True,
                    )
                    return
                key = member_select.values[0]
                # Capacity check.
                cap = s.zone_capacity(s.selected_zone)
                if s.zone_member_count(s.selected_zone) >= cap:
                    await inter.response.send_message(
                        f"⚠️ **{s.selected_zone}** is already full ({cap} members). "
                        f"Unassign someone before adding another.",
                        ephemeral=True,
                    )
                    return
                # Override confirmation for below-floor.
                if key in below and not s.show_below_floor:
                    # Shouldn't happen since the option isn't in the pool,
                    # but be defensive.
                    await inter.response.send_message(
                        "⚠️ Toggle the below-floor override to assign this member.",
                        ephemeral=True,
                    )
                    return
                s.assignments[s.selected_zone].append(key)
                await self._refresh(inter)

            member_select.callback = _on_member
            self.add_item(member_select)

        # Row 2 — action buttons
        toggle_label = (
            "👁️ Hide below-floor" if s.show_below_floor
            else "👁️ Show below-floor"
        )
        toggle_btn = discord.ui.Button(
            label=toggle_label, style=discord.ButtonStyle.secondary, row=2,
        )

        async def _toggle(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            s.show_below_floor = not s.show_below_floor
            await self._refresh(inter)

        toggle_btn.callback = _toggle
        self.add_item(toggle_btn)

        unassign_btn = discord.ui.Button(
            label="↩️ Unassign current zone", style=discord.ButtonStyle.secondary, row=2,
        )

        async def _unassign(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            if not s.selected_zone:
                await inter.response.send_message("⚠️ Pick a zone first.", ephemeral=True)
                return
            s.assignments[s.selected_zone] = []
            await self._refresh(inter)

        unassign_btn.callback = _unassign
        self.add_item(unassign_btn)

        move_to_subs_btn = discord.ui.Button(
            label="🪑 Last to subs", style=discord.ButtonStyle.secondary, row=2,
        )

        async def _move_to_subs(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            members_in_zone = s.assignments.get(s.selected_zone, [])
            if not members_in_zone:
                await inter.response.send_message(
                    "⚠️ No members in this zone to move.", ephemeral=True,
                )
                return
            moved = members_in_zone.pop()
            s.subs.append(moved)
            await self._refresh(inter)

        move_to_subs_btn.callback = _move_to_subs
        self.add_item(move_to_subs_btn)

        # Row 3 — finalisation. Structured-mode adds an Approve & Post
        # button that fires the rosters_tab write + auto-post; free
        # tier gets Generate-mail-only (officer copies manually).
        if s.is_structured:
            approve_btn = discord.ui.Button(
                label="✅ Approve & Post",
                style=discord.ButtonStyle.success, row=3,
            )

            async def _approve(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                await _finalize_structured_roster(inter, self)

            approve_btn.callback = _approve
            self.add_item(approve_btn)

            preview_btn = discord.ui.Button(
                label="📄 Preview mail", style=discord.ButtonStyle.secondary, row=3,
            )

            async def _preview(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                await _send_mail_preview(inter, s)

            preview_btn.callback = _preview
            self.add_item(preview_btn)
        else:
            mail_btn = discord.ui.Button(
                label="📄 Generate mail", style=discord.ButtonStyle.primary, row=3,
            )

            async def _gen_mail(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                await _send_mail_preview(inter, s)

            mail_btn.callback = _gen_mail
            self.add_item(mail_btn)

            save_preset_btn = discord.ui.Button(
                label="💾 Save as preset", style=discord.ButtonStyle.success, row=3,
            )

            async def _save_preset(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                await inter.response.send_modal(_SaveAsPresetModal(self))

            save_preset_btn.callback = _save_preset
            self.add_item(save_preset_btn)

        cancel_label = "❌ Cancel" if s.is_structured else "✅ Done"
        done_btn = discord.ui.Button(
            label=cancel_label, style=discord.ButtonStyle.danger, row=3,
        )

        async def _done(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            for item in self.children:
                item.disabled = True
            await inter.response.edit_message(
                content=("Roster builder cancelled — nothing posted."
                         if s.is_structured else "Roster builder closed."),
                embed=_render_builder_embed(s),
                view=self,
            )
            self.stop()

        done_btn.callback = _done
        self.add_item(done_btn)

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.session.user_id:
            await inter.response.send_message(
                "⛔ Only the officer who opened this builder can use it.",
                ephemeral=True,
            )
            return False
        return True

    async def _refresh(self, inter: discord.Interaction) -> None:
        self._rebuild()
        await inter.response.edit_message(
            embed=_render_builder_embed(self.session), view=self,
        )


class _SaveAsPresetModal(discord.ui.Modal, title="Save as preset"):
    def __init__(self, view: RosterBuilderView):
        super().__init__()
        self._view = view
        self.preset_name = discord.ui.TextInput(
            label="Preset name (overwrites if exists)",
            default=view.session.preset.name,
            required=True, max_length=60,
        )
        self.add_item(self.preset_name)

    async def on_submit(self, inter: discord.Interaction):
        name = (self.preset_name.value or "").strip()
        if not name:
            await inter.response.send_message(
                "⚠️ Preset name is required.", ephemeral=True,
            )
            return
        s = self._view.session
        # Build a fresh PresetBuffer with zone capacities = current
        # filled counts (so re-applying the preset reproduces this roster).
        import storm_strategy as ss
        new_zones = []
        for z in s.preset.zones:
            cur_count = s.zone_member_count(z.zone)
            new_zones.append(ss.ZoneRow(
                zone=z.zone,
                max_players=cur_count if cur_count > 0 else int(z.max_players),
                min_power_a=int(z.min_power_a or 0),
                min_power_b=int(z.min_power_b or 0),
                priority=int(z.priority or 0),
            ))
        buf = ss.PresetBuffer(
            name=name, event_type=s.event_type, zones=new_zones,
            faction=s.preset.faction,
        )
        ok = ss.save_preset(s.guild_id, s.event_type, buf)
        if ok:
            await inter.response.send_message(
                f"✅ Saved roster as preset **{name}**.", ephemeral=True,
            )
        else:
            await inter.response.send_message(
                "⚠️ Couldn't save preset — check that your Sheet is configured "
                "and the bot has edit access.",
                ephemeral=True,
            )


# ── Mail generation ──────────────────────────────────────────────────────────


async def _send_mail_preview(
    inter: discord.Interaction, session: RosterBuilderSession,
) -> None:
    """Build the text-template mail from the current roster and post a
    preview ephemerally. Officer copies it into the alliance's mail
    system manually (no auto-post in v1)."""
    import storm
    zones_for_mail: dict[str, list[str]] = {}
    for zone_name, keys in session.assignments.items():
        names = [session.members[k]["name"] for k in keys if k in session.members]
        if names:
            zones_for_mail[zone_name] = names
    sub_names = [session.members[k]["name"] for k in session.subs if k in session.members]

    if session.event_type == "DS":
        mail = storm.build_ds_mail(
            team=session.team or "A",
            zones=zones_for_mail,
            subs=sub_names,
            time_key="1",
            guild_id=session.guild_id,
        )
    else:
        # CS mail builder doesn't take subs as a separate arg — they're
        # part of the zones dict under CS_SUBS_KEY.
        cs_zones = dict(zones_for_mail)
        if sub_names:
            try:
                cs_zones[storm.CS_SUBS_KEY] = sub_names
            except AttributeError:
                pass
        mail = storm.build_cs_mail(
            team=session.team or "A",
            z=cs_zones,
            time_key="1",
            guild_id=session.guild_id,
        )

    # Truncate to fit a Discord message — keep within 1900 chars so the
    # code-fence framing stays under 2000.
    preview = mail if len(mail) <= 1900 else mail[:1880] + "\n…(truncated)"
    await inter.response.send_message(
        "📄 **Mail preview** — copy and paste into your alliance's mail system:\n"
        f"```\n{preview}\n```",
        ephemeral=True,
    )


# ── Slash command wiring ─────────────────────────────────────────────────────


def _signup_filter_keys(
    guild_id: int, event_type: str, event_date: str, team: str,
) -> set[str]:
    """Return the set of target_member_id values that voted in a way
    compatible with the target team. DS team A pool = voted A or
    Either; team B pool = voted B or Either. CS pool = voted A or
    Either (CS has one slot per faction).

    The on-behalf path stores votes against the roster member name as
    target_member_id, while self-votes store the Discord ID. Both are
    string keys that match `members` in `_read_roster_powers`.
    """
    import config
    rows = config.get_storm_signups(guild_id, event_type, event_date)
    accept_a = team in ("A", "")
    accept_b = team == "B"
    out: set[str] = set()
    for r in rows:
        vote = r.get("vote", "")
        if vote == "either":
            out.add(r["target_member_id"])
        elif accept_a and vote == "a":
            out.add(r["target_member_id"])
        elif accept_b and vote == "b":
            out.add(r["target_member_id"])
    return out


async def _finalize_structured_roster(
    interaction: discord.Interaction, view: RosterBuilderView,
) -> None:
    """Approve & Post: posts the structured mail to the configured
    post channel and writes one row per slot to rosters_tab."""
    import config
    import storm

    s = view.session
    await interaction.response.defer(ephemeral=True, thinking=True)

    # Build mail.
    zones_for_mail: dict[str, list[str]] = {}
    for zone_name, keys in s.assignments.items():
        names = [s.members[k]["name"] for k in keys if k in s.members]
        if names:
            zones_for_mail[zone_name] = names
    sub_names = [s.members[k]["name"] for k in s.subs if k in s.members]

    if s.event_type == "DS":
        mail = storm.build_ds_mail(
            team=s.team or "A",
            zones=zones_for_mail,
            subs=sub_names,
            time_key="1",
            guild_id=s.guild_id,
        )
    else:
        cs_zones = dict(zones_for_mail)
        if sub_names:
            try:
                cs_zones[storm.CS_SUBS_KEY] = sub_names
            except AttributeError:
                pass
        mail = storm.build_cs_mail(
            team=s.team or "A",
            z=cs_zones,
            time_key="1",
            guild_id=s.guild_id,
        )

    cfg = config.get_storm_config(s.guild_id, s.event_type)
    post_channel_id = int(cfg.get("post_channel_id") or 0)
    post_channel = None
    if post_channel_id and interaction.guild:
        post_channel = interaction.guild.get_channel(post_channel_id)

    posted_to_mention = None
    if post_channel is not None:
        try:
            await post_channel.send(mail)
            posted_to_mention = post_channel.mention
        except Exception as e:
            logger.warning(
                "[STORM STRUCTURED] failed to post mail to channel=%s guild=%s: %s",
                post_channel_id, s.guild_id, e,
            )

    # Sheet write — one row per slot. Best-effort; failures log but
    # don't roll back the Discord post.
    write_errors = _write_rosters_tab(s)

    # Close out the view.
    for item in view.children:
        item.disabled = True
    summary_lines = ["✅ Roster posted."]
    if posted_to_mention:
        summary_lines.append(f"📬 Mail sent to {posted_to_mention}.")
    else:
        summary_lines.append(
            "⚠️ No post channel is configured — mail was built but not sent. "
            "Run setup to pick one, or copy the mail manually below."
        )
    if write_errors:
        summary_lines.append("⚠️ " + write_errors[0])
    # Slim public ack in the channel.
    try:
        if view.message:
            await view.message.edit(
                content="✅ Structured roster approved and posted.",
                embed=_render_builder_embed(s),
                view=view,
            )
    except discord.HTTPException:
        pass
    # Officer-facing details (ephemeral).
    detail = "\n".join(summary_lines)
    if not posted_to_mention:
        preview = mail if len(mail) <= 1800 else mail[:1780] + "\n…(truncated)"
        detail += f"\n\n```\n{preview}\n```"
    await interaction.followup.send(detail, ephemeral=True)
    view.stop()


_ROSTERS_HEADER = [
    "Event Date", "Team", "Zone", "Member", "Role",
    "Power at Assignment", "Discord ID", "Posted At (UTC)",
]


def _write_rosters_tab(session: RosterBuilderSession) -> list[str]:
    """Append one row per slot to the alliance's configured rosters_tab.
    Returns a list of soft error strings (empty on success)."""
    import datetime as _dt
    import config

    errors: list[str] = []
    structured = config.get_structured_storm_config(session.guild_id, session.event_type)
    tab = structured.get("rosters_tab") or config.default_structured_tab(
        session.event_type, "rosters_tab"
    )
    if not tab:
        return ["No rosters tab configured — Sheet write skipped."]

    try:
        sh = config.get_spreadsheet(session.guild_id)
    except Exception as e:
        return [f"spreadsheet open failed: {e}"]
    if sh is None:
        return ["spreadsheet not configured — Sheet write skipped."]

    try:
        ws = sh.worksheet(tab)
    except Exception:
        try:
            ws = sh.add_worksheet(title=tab, rows=2000, cols=len(_ROSTERS_HEADER))
            ws.append_row(_ROSTERS_HEADER, value_input_option="RAW")
        except Exception as e:
            return [f"rosters tab create failed: {e}"]

    posted_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    rows: list[list[str]] = []
    for z in session.preset.zones:
        for key in session.assignments.get(z.zone, []):
            m = session.members.get(key)
            if not m:
                continue
            power = m.get("power")
            rows.append([
                session.event_date or "",
                session.team or "",
                z.zone,
                m["name"],
                "primary",
                str(power) if power is not None else "unknown",
                m.get("discord_id") or "",
                posted_at,
            ])
    for key in session.subs:
        m = session.members.get(key)
        if not m:
            continue
        power = m.get("power")
        rows.append([
            session.event_date or "",
            session.team or "",
            "",
            m["name"],
            "sub",
            str(power) if power is not None else "unknown",
            m.get("discord_id") or "",
            posted_at,
        ])

    if not rows:
        return errors  # Nothing to write; treat as success.

    try:
        ws.append_rows(rows, value_input_option="RAW")
    except Exception as e:
        errors.append(f"rosters tab append failed: {e}")
    return errors


async def open_roster_builder(
    interaction: discord.Interaction,
    event_type: str,
    preset_name: str,
    *,
    event_date: Optional[str] = None,
    team_override: Optional[str] = None,
) -> None:
    """Open the roster builder for a named preset.

    Free-tier "apply" mode (event_date=None): officer builds a roster
    from the full alliance roster and copies the mail manually. Routed
    through storm_strategy's apply subcommand.

    Structured mode (event_date set): Premium-only. The member pool is
    pre-filtered to members who signed up matching this team. The
    builder gets `[Approve & Post]` which writes rosters_tab and
    auto-posts the mail. Routed through the officer view's
    `[Set up Team A/B]` buttons.

    `team_override` skips the team picker (used by structured mode
    when the officer already picked a team in the officer view).
    """
    from storm_permissions import (
        is_leader_or_admin,
        deny_non_leader,
        ensure_premium_structured,
    )
    import storm_strategy as ss
    import storm_member_rules as smr

    if not is_leader_or_admin(interaction):
        await deny_non_leader(interaction)
        return

    if not interaction.guild_id:
        await interaction.response.send_message(
            "⚠️ This command must be used inside a server.", ephemeral=True,
        )
        return

    is_structured = bool(event_date)
    if is_structured:
        ok, _structured = await ensure_premium_structured(
            interaction, event_type,
            feature_label="The structured roster builder",
        )
        if not ok:
            return

    preset = ss.load_preset(interaction.guild_id, event_type, preset_name)
    if preset is None:
        msg = (f"⚠️ No preset named **{preset_name}**. Use the list command "
               f"to see saved presets.")
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    if not preset.zones:
        msg = (f"⚠️ Preset **{preset_name}** has no zones yet. Edit it first "
               f"to add zones before applying.")
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    # Defer (if we haven't already via ensure_premium_structured) so the
    # roster Sheet read + member-rules read have headroom.
    if not interaction.response.is_done():
        await interaction.response.defer(thinking=True)

    # Team picker for DS — skip if caller already passed team_override.
    team = team_override or ""
    if event_type == "DS" and not team:
        team_view = _TeamPickerView(interaction.user.id)
        await interaction.followup.send(
            f"Build roster for **Team A** or **Team B** with preset "
            f"**{preset_name}**?",
            view=team_view, ephemeral=True,
        )
        await team_view.wait()
        if team_view.selected is None:
            await interaction.followup.send(
                "⏰ Timed out. Run the apply command again.", ephemeral=True,
            )
            return
        team = team_view.selected

    # Load powers + rules.
    members, roster_errors = _read_roster_powers(interaction.guild_id, event_type)

    # Structured-mode pool filter: keep only members who signed up
    # compatible with this team. Unknown signups (not on roster) are
    # surfaced as a soft warning but don't gate the builder.
    if is_structured:
        signup_keys = _signup_filter_keys(
            interaction.guild_id, event_type, event_date, team,
        )
        before_count = len(members)
        members = {k: v for k, v in members.items() if k in signup_keys}
        # Surface members who voted but aren't on the roster (likely
        # spelling drift between roster and on-behalf vote).
        missing = signup_keys - set(members.keys())
        if missing:
            roster_errors.append(
                f"{len(missing)} signed-up member(s) couldn't be matched to a "
                f"roster row: {', '.join(sorted(missing))[:200]}"
            )
        if not members:
            await interaction.followup.send(
                f"⚠️ No signed-up members match team **{team or 'A'}** for "
                f"event **{event_date}**. Check `/storm_signups` to see who's "
                f"voted, or run the apply flow without an event date to use "
                f"the full roster.",
                ephemeral=True,
            )
            return
        if before_count and before_count == len(members):
            # Defensive — everyone on roster voted; not really an error.
            pass

    rules = smr.list_rules(interaction.guild_id, event_type)
    per_member = [r for r in rules if r.rule_type == "per_member"]
    power_band = [r for r in rules if r.rule_type == "power_band"]

    session = RosterBuilderSession(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        event_type=event_type,
        team=team,
        preset=preset,
        members=members,
        per_member_rules=per_member,
        power_band_rules=power_band,
        event_date=event_date,
    )
    # Seed errors from the roster read FIRST so _apply_rules_to_session
    # can append its own (e.g. unmatched per_member subjects) without
    # being clobbered.
    session.roster_errors = list(roster_errors)
    _apply_rules_to_session(session)

    view = RosterBuilderView(session)
    embed = _render_builder_embed(session)
    msg = await interaction.followup.send(embed=embed, view=view)
    view.message = msg


class _TeamPickerView(discord.ui.View):
    """Two-button picker for DS team. Only the invoking user can click."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.selected: Optional[str] = None

        a = discord.ui.Button(label="🅰️ Team A", style=discord.ButtonStyle.primary)
        b = discord.ui.Button(label="🅱️ Team B", style=discord.ButtonStyle.success)

        async def _pick_a(inter: discord.Interaction):
            if inter.user.id != self.owner_id:
                await inter.response.send_message(
                    "⛔ Only the officer who started the apply can pick.",
                    ephemeral=True,
                )
                return
            self.selected = "A"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(
                content="✅ Team A selected.", view=self,
            )
            self.stop()

        async def _pick_b(inter: discord.Interaction):
            if inter.user.id != self.owner_id:
                await inter.response.send_message(
                    "⛔ Only the officer who started the apply can pick.",
                    ephemeral=True,
                )
                return
            self.selected = "B"
            for item in self.children: item.disabled = True
            await inter.response.edit_message(
                content="✅ Team B selected.", view=self,
            )
            self.stop()

        a.callback = _pick_a
        b.callback = _pick_b
        self.add_item(a)
        self.add_item(b)
