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

import io
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
    guild_id: int, event_type: str, *, guild=None,
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
    has_not_col = not_disc_col >= 0
    members: dict[str, dict] = {}
    stale_ids: list[str] = []
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

        # Non-Discord detection (#139). Explicit alliance column wins
        # when set; inference fills the gap when the column is absent
        # or empty:
        #   * blank discord_id  → non-Discord
        #   * discord_id set but not in guild → non-Discord (stale ID
        #     — member's left the server)
        explicit_set = (
            _cell(not_disc_col).lower() in truthy if has_not_col else False
        )
        inferred = False
        if not explicit_set:
            if not discord_id:
                inferred = True
            elif guild is not None and discord_id.isdigit():
                try:
                    member = guild.get_member(int(discord_id))
                except (TypeError, ValueError):
                    member = None
                if member is None:
                    inferred = True
                    stale_ids.append(f"{name or '?'} (id {discord_id})")
        not_on_discord = explicit_set or inferred

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

    if stale_ids:
        preview = ", ".join(stale_ids[:5])
        extra = f" (+{len(stale_ids) - 5} more)" if len(stale_ids) > 5 else ""
        errors.append(
            "stale Discord IDs on roster (member likely left the server): "
            f"{preview}{extra}"
        )
        logger.warning(
            "[STORM ROSTER] stale roster Discord IDs for guild=%s event=%s: %s",
            guild_id, event_type, "; ".join(stale_ids),
        )

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
        sub_mode: str = "pool",
    ):
        self.guild_id = guild_id
        self.user_id  = user_id
        self.event_type = event_type
        self.team     = team
        self.preset   = preset
        self.members  = members
        self.event_date = event_date
        # `sub_mode` reflects the alliance's per-event-type config.
        # `pool` — flat sub list; any sub can cover any primary.
        # `paired` — each primary is paired with a specific sub; the
        #            builder UI prompts for the sub immediately after
        #            primary assignment.
        # Unknown values normalise to `pool` (defense in depth — the
        # storage layer already validates, but a hand-edited DB row
        # shouldn't crash the builder).
        self.sub_mode = sub_mode if sub_mode in ("pool", "paired") else "pool"
        # Per-zone assignments: {zone_name: [member_key, ...]}
        self.assignments: dict[str, list[str]] = {z.zone: [] for z in preset.zones}
        # Flat sub pool. In `paired` mode, subs lives in
        # `paired_subs` instead — this list stays empty.
        self.subs: list[str] = []
        # Paired-mode pairings: {primary_key: sub_key}. Only populated
        # when sub_mode == "paired"; the embed + writer branch on
        # presence of this dict.
        self.paired_subs: dict[str, str] = {}
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
        # Member keys that were assigned via the below-floor override
        # toggle (i.e. their power was below the zone's effective floor,
        # or their power was unknown). Captured at assign time so the
        # rosters_tab write can flag the slot for post-event review.
        self.below_floor_overrides: set[str] = set()
        # Auto-fill summary (#134) — populated by _auto_fill_session when
        # the officer clicks the auto-fill button. The embed renderer
        # surfaces this in place of the empty-state hint so leadership
        # knows what rules applied, what got filled, what gapped.
        # None until auto-fill runs at least once.
        self.auto_fill_summary: dict | None = None

    @property
    def is_structured(self) -> bool:
        return bool(self.event_date)

    @property
    def is_paired(self) -> bool:
        return self.sub_mode == "paired"

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
        """Every member currently slotted somewhere — any zone, the
        flat sub pool, OR any paired-sub seat. The eligibility filter
        uses this to exclude already-placed members from the picker."""
        keys: set[str] = set()
        for zone_members in self.assignments.values():
            keys.update(zone_members)
        keys.update(self.subs)
        keys.update(self.paired_subs.values())
        return keys

    def unpaired_primaries(self) -> list[str]:
        """In paired mode, the list of zone members who don't yet have
        a paired sub. Order matches zone-then-roster order so the UI
        prompt is deterministic. Returns [] when sub_mode=pool."""
        if not self.is_paired:
            return []
        unpaired = []
        for zone in self.preset.zones:
            for key in self.assignments.get(zone.zone, []):
                if key not in self.paired_subs:
                    unpaired.append(key)
        return unpaired

    def zone_member_count(self, zone_name: str) -> int:
        return len(self.assignments.get(zone_name, []))

    def zone_capacity(self, zone_name: str) -> int:
        z = self.preset.find_zone(zone_name)
        return int(z.max_players) if z else 0

    def prune_stale_pairings(self) -> None:
        """Drop paired-sub entries whose primary is no longer in a zone.

        Called after Unassign / Move-to-subs. Without this, an unassigned
        primary's old pairing would linger and surface stale data in
        the embed + the rosters_tab write."""
        primaries_in_zones: set[str] = set()
        for zone_members in self.assignments.values():
            primaries_in_zones.update(zone_members)
        # Filter the pairing map.
        self.paired_subs = {
            primary: sub
            for primary, sub in self.paired_subs.items()
            if primary in primaries_in_zones
        }

    def prune_stale_overrides(self) -> None:
        """Drop override entries for members no longer in any zone.

        The override flag captures "officer assigned this member below
        the floor" at the moment of assignment. If they're later
        unassigned (zone cleared) or moved to subs (subs don't carry
        the flag), the entry shouldn't survive — otherwise a later
        re-assignment without the toggle would still mark the slot.
        """
        currently_in_zones: set[str] = set()
        for zone_members in self.assignments.values():
            currently_in_zones.update(zone_members)
        self.below_floor_overrides &= currently_in_zones


def _resolve_per_member_subject(
    members: dict[str, dict], subject: str,
) -> str | None:
    """Three-way subject resolution for per_member rules.

    Per the #134 audit + #136 Discord-ID-keying refactor, a rule's
    subject can be:
      * a roster display name ("Alice") — match `m["name"]`
      * a roster key (Discord ID or roster name acting as key)
      * a Discord ID stored as the `discord_id` field on a member dict
        whose key is the roster name (non-Discord rows with numeric
        handles, or pre-#136 rows where key != discord_id)

    Returns the matching member key, or None if no path resolves. Single
    canonical helper so `_apply_rules_to_session` and `_auto_fill_session`
    can't drift on the lookup logic — the original audit found
    `_apply_rules_to_session` only did the name match, which silently
    dropped every Discord-ID-keyed rule at session open (and falsely
    surfaced them in `session.roster_errors` as "stale rules").
    """
    if not subject:
        return None
    target = subject.lower()
    for k, m in members.items():
        if m["name"].strip().lower() == target:
            return k
        if k == subject:
            return k
        if m.get("discord_id") == subject:
            return k
    return None


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
        subject = rule.subject.strip()
        match_key = _resolve_per_member_subject(session.members, subject)
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


# ── Auto-fill (#134) ─────────────────────────────────────────────────────────


def _auto_fill_session(session: RosterBuilderSession) -> dict:
    """Auto-fill the roster from member rules + power-based greedy fill.

    Resets the current roster (assignments + subs + override flags)
    before filling, so a re-click of the button is "redo from scratch"
    rather than "stack onto current state."

    Algorithm, in order:
      1. per_member zone rules — pin members to their named zone if
         capacity, the member is in the signed-up pool, and the zone
         exists in the preset.
      2. Greedy fill — for each zone in priority order (lowest int
         first; priority=0 sorts last), fill remaining slots from the
         eligibility-gated pool (uses `_eligible_member_keys_for_zone`,
         which respects power_band rule relaxation).
      3. Spillover — unassigned members with known power go into the
         sub pool. Power-unknown members are reported as gaps so the
         officer can decide who to override below the floor.

    Returns the summary dict (also stored on `session.auto_fill_summary`).

    Pool-source contract: this function ONLY auto-fills members present
    in `session.members`. In structured mode, the upstream
    `open_roster_builder` narrows that dict to signed-up members for
    the team BEFORE constructing the session — so this function inherits
    the signup-pool filter transitively. In free-tier mode, all roster
    members are eligible. Tests in TestAutoFillRespectsMembersDict pin
    this contract.

    The fill is officer-correctable — every assignment can be tweaked
    via the picker before Approve & Post.
    """
    # Reset state — auto-fill is "redo from scratch".
    for zone in list(session.assignments.keys()):
        session.assignments[zone] = []
    session.subs = []
    session.paired_subs = {}
    session.below_floor_overrides.clear()

    summary = {
        "per_member_rules_applied": 0,
        "power_band_rules_applied": 0,
        "auto_filled_by_power":     0,
        "auto_paired_subs":         0,
        "gaps":                     [],  # member names with no parseable power
        "conflicts":                [],  # short strings: rule application failures
    }

    # ── 1. per_member zone rules ──
    for rule in session.per_member_rules:
        if rule.sub_type != "zone":
            continue
        subject = rule.subject.strip()
        zone = rule.value.strip()
        # Same three-way resolution `_apply_rules_to_session` uses, so
        # this consumer and the session-open pre-application can't drift.
        match_key = _resolve_per_member_subject(session.members, subject)
        if match_key is None:
            # The opener pass (`_apply_rules_to_session`) already warned
            # via `session.roster_errors`, but auto-fill is the consumer
            # actually trying to place this rule. Record a conflict so
            # the summary's "Per-member rules applied: N" lines up
            # against the number of rules officers see in the editor.
            if subject:
                summary["conflicts"].append(
                    f"per_member subject not on roster: {subject}"
                )
            continue
        if not session.preset.find_zone(zone):
            summary["conflicts"].append(
                f"per_member rule names unknown zone: {zone}"
            )
            continue
        if session.zone_member_count(zone) >= session.zone_capacity(zone):
            summary["conflicts"].append(
                f"{zone} full when pinning {subject}"
            )
            continue
        if match_key in session.assigned_member_keys():
            summary["conflicts"].append(
                f"{subject} pinned to multiple zones"
            )
            continue
        session.assignments[zone].append(match_key)
        summary["per_member_rules_applied"] += 1
        # A per_member pin of a power-unknown member is an officer
        # decision to assign below the floor — record it in the
        # override set so the rosters_tab `Override Below Floor`
        # column lights up for that slot. Without this, an auto-fill
        # decision is silently weaker than the equivalent manual
        # assignment via the toggle.
        member = session.members.get(match_key)
        if member is not None and member.get("power") is None:
            session.below_floor_overrides.add(match_key)

    # ── 2. Greedy fill by zone priority ──
    # priority=0 means "no priority set" → sort to the end.
    def _priority_key(z) -> int:
        return z.priority if z.priority > 0 else 9999

    zones_sorted = sorted(session.preset.zones, key=_priority_key)
    for z in zones_sorted:
        remaining = z.max_players - session.zone_member_count(z.zone)
        if remaining <= 0:
            continue
        eligible_keys, _below = _eligible_member_keys_for_zone(session, z.zone)
        if not eligible_keys:
            continue
        # eligible_keys is already sorted high-power-first AND
        # name-tiebroken, so the result is deterministic across reads.
        for key in eligible_keys[:remaining]:
            session.assignments[z.zone].append(key)
            summary["auto_filled_by_power"] += 1
            # Did this member's power fall below the preset's per-team
            # floor but pass the band-relaxed effective floor? If so,
            # they're in via a power_band rule — count them honestly.
            preset_floor = session.floor_for_zone(z.zone)
            effective_floor = _effective_floor_for_zone(session, z.zone)
            member_power = session.members[key].get("power")
            if (
                member_power is not None
                and effective_floor < preset_floor
                and member_power < preset_floor
            ):
                summary["power_band_rules_applied"] += 1

    # ── 3. Spillover (or pair) ──
    # In paired mode, leftover known-power members get assigned as
    # paired subs for unpaired primaries (in zone-priority order →
    # highest-power-first to keep parity with the primary fill). In
    # pool mode, leftovers go into the flat sub pool.
    if session.is_paired:
        unpaired = session.unpaired_primaries()
        # Walk unpaired primaries in zone-priority order (already the
        # order returned by unpaired_primaries since it iterates zones
        # in preset order; auto-fill already greedy-filled in priority
        # order, so this lines up). For each, pair with the next eligible
        # member NOT already placed.
        for primary_key in unpaired:
            # Find the zone the primary is in so we can apply the right
            # floor to the sub's eligibility.
            primary_zone = None
            for zone, zmembers in session.assignments.items():
                if primary_key in zmembers:
                    primary_zone = zone
                    break
            if not primary_zone:
                continue
            eligible_sub_keys, _below = _eligible_member_keys_for_zone(
                session, primary_zone,
            )
            if not eligible_sub_keys:
                continue
            # Pair with the strongest eligible candidate. Count this in
            # `auto_paired_subs` (not `auto_filled_by_power`) so the
            # summary distinguishes primaries from paired subs —
            # otherwise the embed reads "Auto-filled by power: 8" when
            # really 4 primaries + 4 paired subs were placed.
            session.paired_subs[primary_key] = eligible_sub_keys[0]
            summary["auto_paired_subs"] += 1

        # Anything else known-power → still surface as "available subs"
        # the officer might want to swap in manually. Use the flat
        # `subs` list as a holding area in paired mode (rendered as a
        # diagnostic in the embed only when non-empty).
        assigned = session.assigned_member_keys()
        for key, m in session.members.items():
            if key in assigned:
                continue
            if m.get("power") is None:
                summary["gaps"].append(m["name"])
                continue
            session.subs.append(key)
    else:
        assigned = session.assigned_member_keys()
        for key, m in session.members.items():
            if key in assigned:
                continue
            if m.get("power") is None:
                summary["gaps"].append(m["name"])
                continue
            # Known-power leftovers → sub pool.
            session.subs.append(key)

    session.auto_fill_summary = summary
    return summary


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
        primary_label = m["name"] if m else f"<unknown:{k}>"
        if session.is_paired:
            sub_key = session.paired_subs.get(k)
            if sub_key:
                sub_m = session.members.get(sub_key)
                sub_label = sub_m["name"] if sub_m else f"<unknown:{sub_key}>"
                names.append(f"{primary_label} + sub {sub_label}")
            else:
                # Unpaired primary in paired mode — flagged so the
                # officer can see a sub still needs to be picked.
                names.append(f"{primary_label} ⚠️")
        else:
            names.append(primary_label)
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
    if session.is_paired:
        lines.append("**📋 Zones** _(paired mode — each primary has a dedicated sub)_")
    else:
        lines.append("**📋 Zones**")
    for z in session.preset.zones:
        lines.append(_render_zone_line(session, z.zone))
    lines.append("")
    if session.is_paired:
        unpaired = session.unpaired_primaries()
        if unpaired:
            unpaired_names = ", ".join(
                session.members[k]["name"] for k in unpaired
                if k in session.members
            )
            lines.append(
                f"⚠️ **Unpaired primaries ({len(unpaired)})**: {unpaired_names} — "
                f"pick a sub for each via the picker."
            )
        else:
            lines.append("🪑 **Sub pairings**: complete for every primary.")
    else:
        sub_names = [
            session.members[k]["name"] for k in session.subs if k in session.members
        ]
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

    af = session.auto_fill_summary
    if af is not None:
        lines.append("")
        lines.append("🎯 **Auto-fill summary**")
        lines.append(
            f"• Per-member rules applied: **{af['per_member_rules_applied']}**"
        )
        lines.append(
            f"• Members slotted via a band-relaxed floor: **{af['power_band_rules_applied']}**"
        )
        lines.append(
            f"• Auto-filled by power: **{af['auto_filled_by_power']}**"
        )
        # Separate paired-sub count so the auto-filled total isn't
        # inflated by the paired-sub count for paired-mode alliances.
        # Defaults to 0 for pool mode where it's always 0 anyway.
        if af.get("auto_paired_subs"):
            lines.append(
                f"• Auto-paired subs: **{af['auto_paired_subs']}**"
            )
        if af["gaps"]:
            preview = ", ".join(af["gaps"][:5])
            extra = f" (+{len(af['gaps']) - 5} more)" if len(af["gaps"]) > 5 else ""
            lines.append(
                f"• Gaps (power unknown, not slotted): **{len(af['gaps'])}** — "
                f"{preview}{extra}"
            )
        if af["conflicts"]:
            preview = "; ".join(af["conflicts"][:3])
            extra = f" (+{len(af['conflicts']) - 3} more)" if len(af["conflicts"]) > 3 else ""
            lines.append(f"• Conflicts: **{len(af['conflicts'])}** — {preview}{extra}")
        else:
            lines.append("• Conflicts: **0**")

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
    # Sort eligible high-power-first; tie-break on name so the order is
    # content-deterministic (Python's sort is stable, so equal-power
    # members would otherwise fall back to dict-insertion order — fine
    # in practice but a regression trap if the upstream roster read ever
    # reorders rows).
    def _power_then_name(k: str) -> tuple[int, str]:
        m = session.members[k]
        return (-(m.get("power") or 0), m.get("name") or "")
    eligible.sort(key=_power_then_name)
    below.sort(key=_power_then_name)
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
                # Record the override for the audit trail — anyone in
                # `below` at assign time was assigned despite being
                # below the effective floor (or having unknown power).
                if key in below:
                    s.below_floor_overrides.add(key)
                s.assignments[s.selected_zone].append(key)
                # Any manual edit invalidates the auto-fill summary —
                # the names and counts the officer is reading no longer
                # describe what's currently on the roster.
                s.auto_fill_summary = None
                # In paired mode, immediately prompt for the paired sub
                # so the pairing happens in the same step as the primary
                # assignment. The picker fires ephemerally so it doesn't
                # crowd the main view, and on close it re-renders the
                # main view to surface the new pairing.
                if s.is_paired:
                    await _open_paired_sub_picker(inter, self, primary_key=key)
                    return
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
            s.prune_stale_overrides()
            s.prune_stale_pairings()
            s.auto_fill_summary = None
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
            s.prune_stale_overrides()
            s.prune_stale_pairings()
            s.auto_fill_summary = None
            await self._refresh(inter)

        move_to_subs_btn.callback = _move_to_subs
        self.add_item(move_to_subs_btn)

        # Row 3 — finalisation. Structured-mode adds an Approve & Post
        # button that fires the rosters_tab write + auto-post; free
        # tier gets Generate-mail-only (officer copies manually).
        if s.is_structured:
            auto_fill_btn = discord.ui.Button(
                label="🎯 Auto-fill",
                style=discord.ButtonStyle.primary, row=2,
            )

            async def _auto_fill(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                _auto_fill_session(s)
                await self._refresh(inter)

            auto_fill_btn.callback = _auto_fill
            self.add_item(auto_fill_btn)

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

        # Image render — available in both modes. Posts a PNG attachment
        # alongside the main message. Pillow import happens lazily inside
        # the handler so the builder doesn't pay the import cost unless
        # the button's clicked.
        render_btn = discord.ui.Button(
            label="🖼️ Render image", style=discord.ButtonStyle.secondary, row=3,
        )

        async def _render(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            await _render_and_attach(inter, s)

        render_btn.callback = _render
        self.add_item(render_btn)

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
            self._release_session_lock()
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

    def _release_session_lock(self) -> None:
        """Drop the structured-mode build lock. Safe in free-tier mode
        (the helper is a no-op when no lock was claimed). Called from
        Cancel/Done, Approve, and on_timeout."""
        s = self.session
        if not s.is_structured:
            return
        try:
            import config
            config.release_storm_session(
                s.guild_id, s.event_type, s.event_date or "", s.team or "",
            )
        except Exception as e:
            logger.warning(
                "[STORM BUILDER] release_storm_session failed for "
                "guild=%s event=%s/%s team=%s: %s",
                s.guild_id, s.event_type, s.event_date, s.team, e,
            )

    async def on_timeout(self) -> None:
        """Strip the view + release the session lock when the builder
        times out. Without this:
          - Buttons silently 404 with "Interaction failed" — same
            CLAUDE.md auto-post-view contract that other auto-posted
            views in this project respect.
          - The session lock would stick until process restart, which
            blocks legitimate re-opens for the same event indefinitely.
        """
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        self._release_session_lock()


async def _open_paired_sub_picker(
    interaction: discord.Interaction,
    main_view: "RosterBuilderView",
    *,
    primary_key: str,
) -> None:
    """Fire the ephemeral sub-picker after a primary assignment in
    paired mode. Lets the officer either pair a sub immediately or
    skip and pair later from the main view."""
    s = main_view.session
    primary = s.members.get(primary_key)
    primary_label = primary["name"] if primary else f"<{primary_key}>"

    # Eligibility for the paired sub matches the primary's zone:
    # subs cover the no-show, so they must meet the same per-team
    # floor for that zone.
    eligible, below = _eligible_member_keys_for_zone(s, s.selected_zone)
    pool = list(eligible)
    if s.show_below_floor:
        pool.extend(below)
    # Cap at the 25-option Discord limit. If the alliance has more
    # eligible candidates, officer can either toggle the below-floor
    # override OR skip + pair later via the main view (deferred TODO).
    pool = pool[:_MAX_DROPDOWN_OPTIONS]

    picker = _PairedSubPickerView(
        main_view=main_view,
        primary_key=primary_key,
        pool=pool,
        zone_name=s.selected_zone,
    )
    content = (
        f"🪑 Pick a sub for **{primary_label}** at **{s.selected_zone}**, "
        f"or skip and pair them later."
    )
    if not pool:
        content = (
            f"🪑 No eligible subs found for **{primary_label}** at "
            f"**{s.selected_zone}**. Skip and pair them later, or toggle "
            f"the below-floor override on the main view to widen the pool."
        )
    try:
        await interaction.response.send_message(
            content=content, view=picker, ephemeral=True,
        )
        picker.message = await interaction.original_response()
    except discord.HTTPException as e:
        logger.warning(
            "[STORM BUILDER] paired sub picker failed to send "
            "(guild=%s primary=%s): %s",
            s.guild_id, primary_key, e,
        )
        # Best-effort fallback: refresh the main view so the unpaired
        # primary is at least visible in the embed.
        try:
            if main_view.message:
                main_view._rebuild()
                await main_view.message.edit(
                    embed=_render_builder_embed(s), view=main_view,
                )
        except discord.HTTPException:
            pass


class _PairedSubPickerView(discord.ui.View):
    """Ephemeral picker shown after a primary assignment in paired mode."""

    def __init__(
        self,
        *,
        main_view: "RosterBuilderView",
        primary_key: str,
        pool: list[str],
        zone_name: str,
    ):
        super().__init__(timeout=300)
        self.main_view  = main_view
        self.primary_key = primary_key
        self.zone_name  = zone_name
        self.message: Optional[discord.Message] = None

        if pool:
            s = main_view.session
            options = []
            for k in pool:
                m = s.members.get(k)
                if not m:
                    continue
                label = _format_member_label(m)[:100]
                options.append(discord.SelectOption(label=label, value=k[:100]))
            if options:
                sel = discord.ui.Select(
                    placeholder="Pick a paired sub…",
                    min_values=1, max_values=1,
                    options=options,
                )
                sel.callback = self._make_pick_callback()
                self.add_item(sel)

        skip_btn = discord.ui.Button(
            label="↩️ Skip — pair later",
            style=discord.ButtonStyle.secondary,
        )
        skip_btn.callback = self._make_skip_callback()
        self.add_item(skip_btn)

    def _make_pick_callback(self):
        async def _cb(inter: discord.Interaction):
            if inter.user.id != self.main_view.session.user_id:
                await inter.response.send_message(
                    "⛔ Only the builder's owner can pair subs.",
                    ephemeral=True,
                )
                return
            if self.is_finished():
                return
            # Find the Select component to read its value.
            sub_key = None
            for child in self.children:
                if isinstance(child, discord.ui.Select):
                    sub_key = child.values[0] if child.values else None
                    break
            if not sub_key:
                await inter.response.send_message(
                    "⚠️ Couldn't read the picked sub. Try again.",
                    ephemeral=True,
                )
                return
            self.stop()

            s = self.main_view.session
            s.paired_subs[self.primary_key] = sub_key
            # If the sub was below-floor at pick time, capture it as an
            # override too so the rosters_tab write flags it.
            _eligible, below = _eligible_member_keys_for_zone(s, self.zone_name)
            if sub_key in below:
                s.below_floor_overrides.add(sub_key)
            # Manual edit invalidates the auto-fill summary.
            s.auto_fill_summary = None

            for item in self.children:
                item.disabled = True
            primary_m = s.members.get(self.primary_key)
            sub_m = s.members.get(sub_key)
            primary_name = primary_m["name"] if primary_m else self.primary_key
            sub_name = sub_m["name"] if sub_m else sub_key
            try:
                await inter.response.edit_message(
                    content=f"✅ Paired **{sub_name}** with **{primary_name}**.",
                    view=self,
                )
            except discord.HTTPException:
                pass
            # Re-render the main view so the new pairing surfaces.
            try:
                if self.main_view.message:
                    self.main_view._rebuild()
                    await self.main_view.message.edit(
                        embed=_render_builder_embed(s), view=self.main_view,
                    )
            except discord.HTTPException:
                pass
        return _cb

    def _make_skip_callback(self):
        async def _cb(inter: discord.Interaction):
            if inter.user.id != self.main_view.session.user_id:
                await inter.response.send_message(
                    "⛔ Only the builder's owner can skip.",
                    ephemeral=True,
                )
                return
            if self.is_finished():
                return
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                await inter.response.edit_message(
                    content="↩️ Skipped — you can pair this primary later.",
                    view=self,
                )
            except discord.HTTPException:
                pass
            # Re-render the main view so the ⚠️ marker on the unpaired
            # primary is visible.
            try:
                if self.main_view.message:
                    self.main_view._rebuild()
                    await self.main_view.message.edit(
                        embed=_render_builder_embed(self.main_view.session),
                        view=self.main_view,
                    )
            except discord.HTTPException:
                pass
        return _cb

    async def on_timeout(self):
        """Strip the view AND refresh the main builder view so the
        primary appears with its ⚠️ "no sub paired" marker. Without the
        main-view refresh, the officer sees the pre-assignment state
        until they click another button — confusing UX when the
        picker timed out silently after a primary assignment.

        Same auto-post-view contract the audit pass codified for the
        rest of the storm flow.
        """
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
        # Refresh main view so the unpaired-primary state is visible.
        if self.main_view.message is not None:
            try:
                self.main_view._rebuild()
                await self.main_view.message.edit(
                    embed=_render_builder_embed(self.main_view.session),
                    view=self.main_view,
                )
            except discord.HTTPException:
                pass


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


async def _render_and_attach(
    inter: discord.Interaction, session: RosterBuilderSession,
) -> None:
    """Render the current roster as a PNG and attach it ephemerally.
    Pillow import lives inside the handler so the builder module
    doesn't pay the import cost unless render is actually invoked.

    Defers immediately and runs the CPU-bound Pillow render in a
    thread executor — without this, a 30-slot PNG encode would blow
    the 3-second interaction token AND stall the gateway heartbeat
    for every other guild while it runs. Same pattern the rest of
    the storm flow uses for slow I/O (1.1.7 hotfix for /train).

    Failure modes:
      * Pillow not installed → renderer raises `RuntimeError`; surface
        a one-line ephemeral. Officer continues with text-only mail.
      * Other Pillow error → log + ephemeral "could not render."
    """
    import asyncio

    # Defer first so the encode + upload have time. `thinking=False`
    # because the followup carries the file directly (no spinner UX).
    try:
        await inter.response.defer(ephemeral=True, thinking=False)
    except discord.HTTPException as e:
        logger.warning(
            "[STORM RENDER] defer failed (guild=%s): %s",
            session.guild_id, e,
        )
        # Interaction is probably dead — bail. No followup is reachable.
        return

    try:
        import storm_renderer
        roster_data = storm_renderer.roster_from_session(session)
        # Pillow encode is CPU-bound; off the event loop so other
        # guilds' heartbeat doesn't stall on a multi-second render.
        png_bytes = await asyncio.to_thread(storm_renderer.render, roster_data)
    except RuntimeError as e:
        # Pillow missing — degrade gracefully.
        logger.warning(
            "[STORM RENDER] Pillow not available (guild=%s event=%s): %s",
            session.guild_id, session.event_type, e,
        )
        await inter.followup.send(
            "⚠️ Image render isn't available — the host is missing Pillow. "
            "Use the text-template mail in the meantime.",
            ephemeral=True,
        )
        return
    except Exception as e:
        logger.exception(
            "[STORM RENDER] failed for guild=%s event=%s: %s",
            session.guild_id, session.event_type, e,
        )
        await inter.followup.send(
            "⚠️ Couldn't render the roster image — see bot logs.",
            ephemeral=True,
        )
        return

    filename = (
        f"{session.event_type.lower()}-roster"
        + (f"-{session.event_date}" if session.event_date else "")
        + (f"-team-{session.team}" if session.team else "")
        + ".png"
    )
    file = discord.File(io.BytesIO(png_bytes), filename=filename)
    await inter.followup.send(
        content="🖼️ Roster image attached:",
        file=file,
        ephemeral=True,
    )


def _mail_zone_and_sub_lists(
    session: RosterBuilderSession,
) -> tuple[dict[str, list[str]], list[str]]:
    """Return `(zones_for_mail, sub_names)` honoring the session's
    sub_mode.

    Pool mode: primaries go under each zone; `session.subs` is the flat
    sub list. Unchanged behaviour.

    Paired mode: primaries go under each zone with "Alice + sub Bob"
    formatting (so the mail reads the way the embed does), and the
    flat sub list is the overflow pool (`session.subs`) only — paired
    subs are inline, not in the global sub block. The Critical audit
    finding was that paired subs were silently invisible in the mail
    because `session.subs` was the only sub source the mail builder
    saw, and `session.subs` is empty for paired-only rosters.
    """
    zones_for_mail: dict[str, list[str]] = {}
    is_paired = (session.sub_mode == "paired")
    for zone_name, keys in session.assignments.items():
        if not keys:
            continue
        names: list[str] = []
        for k in keys:
            m = session.members.get(k)
            if m is None:
                continue
            label = m["name"]
            if is_paired:
                sub_key = session.paired_subs.get(k)
                if sub_key is not None:
                    sub_m = session.members.get(sub_key)
                    if sub_m is not None:
                        label = f"{label} + sub {sub_m['name']}"
            names.append(label)
        if names:
            zones_for_mail[zone_name] = names

    # Overflow / pool subs render in the global sub block. In paired
    # mode these are the unmatched leftovers (`session.subs`), distinct
    # from the inline paired subs above.
    sub_names = [
        session.members[k]["name"]
        for k in session.subs
        if k in session.members
    ]
    return zones_for_mail, sub_names


async def _send_mail_preview(
    inter: discord.Interaction, session: RosterBuilderSession,
) -> None:
    """Build the text-template mail from the current roster and post a
    preview ephemerally. Officer copies it into the alliance's mail
    system manually (no auto-post in v1)."""
    import storm
    zones_for_mail, sub_names = _mail_zone_and_sub_lists(session)

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

    # Refresh powers from the roster Sheet at finalise time so
    # `power_at_assignment` in the rosters_tab write reflects the value
    # at the moment of approval, not the (potentially 15-minute-stale)
    # value captured when the builder was opened. Powers for members
    # whose row is gone are left as None — better than reading the
    # builder-open snapshot, which is what the audit flagged.
    try:
        fresh_members, _refresh_errors = _read_roster_powers(
            s.guild_id, s.event_type, guild=interaction.guild,
        )
        for key, m in s.members.items():
            fresh = fresh_members.get(key)
            if fresh is not None:
                m["power"] = fresh.get("power")
    except Exception as e:
        logger.warning(
            "[STORM STRUCTURED] roster re-read for power snapshot failed "
            "(guild=%s event=%s): %s",
            s.guild_id, s.event_date, e,
        )

    # Build mail — `_mail_zone_and_sub_lists` honors paired sub_mode so
    # paired subs render inline ("Alice + sub Bob") instead of being
    # silently dropped from the mail.
    zones_for_mail, sub_names = _mail_zone_and_sub_lists(s)

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

    # Distinguish three outcomes for the officer-facing summary:
    #   no_channel    — alliance never configured a post channel
    #   channel_gone  — channel_id is set but the channel was deleted /
    #                   the bot can't see it
    #   send_failed   — channel resolved but the API rejected the send
    #                   (perms, rate limit, etc.)
    #   posted_ok     — happy path
    post_status: str
    post_error: Optional[str] = None
    posted_to_mention: Optional[str] = None
    if not post_channel_id:
        post_status = "no_channel"
    elif post_channel is None:
        post_status = "channel_gone"
    else:
        try:
            await post_channel.send(mail)
            posted_to_mention = post_channel.mention
            post_status = "posted_ok"
        except Exception as e:
            post_status = "send_failed"
            post_error = str(e)
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

    # Build the officer-facing summary based on the post outcome.
    if post_status == "posted_ok":
        summary_lines = ["✅ Roster posted.",
                         f"📬 Mail sent to {posted_to_mention}."]
    elif post_status == "no_channel":
        summary_lines = [
            "✅ Roster recorded.",
            "⚠️ No post channel is configured — mail was built but not "
            "sent. Run `/setup_desertstorm` (or `/setup_canyonstorm`) to "
            "pick one, or copy the mail manually below.",
        ]
    elif post_status == "channel_gone":
        summary_lines = [
            "✅ Roster recorded.",
            f"⚠️ The configured post channel (<#{post_channel_id}>) is "
            f"deleted or the bot can't see it. Re-run setup to pick a new "
            f"channel — mail preview below.",
        ]
    else:  # send_failed
        summary_lines = [
            "✅ Roster recorded.",
            f"⚠️ The configured post channel <#{post_channel_id}> rejected "
            f"the send: `{(post_error or 'unknown error')[:120]}`. Check "
            f"the bot's permissions in that channel — mail preview below.",
        ]
    if write_errors:
        summary_lines.append("⚠️ " + write_errors[0])

    # Slim public ack on the original builder message.
    try:
        if view.message:
            await view.message.edit(
                content="✅ Structured roster approved and posted.",
                embed=_render_builder_embed(s),
                view=view,
            )
    except discord.HTTPException:
        pass

    # Officer-facing details (ephemeral). Include the mail preview when
    # we didn't auto-post (so the officer can copy it manually).
    detail = "\n".join(summary_lines)
    if post_status != "posted_ok":
        preview = mail if len(mail) <= 1800 else mail[:1780] + "\n…(truncated)"
        detail += f"\n\n```\n{preview}\n```"
    await interaction.followup.send(detail, ephemeral=True)

    # Faction roles button (#137) — CS-only, only when the alliance
    # actually has a Judicator role configured AND at least one
    # member rule flags a Judicator candidate. Skipping the offer when
    # the roster has no candidates avoids noise.
    if s.event_type == "CS" and post_status == "posted_ok":
        try:
            await _maybe_offer_faction_roles(interaction, s)
        except Exception as e:
            logger.warning(
                "[STORM FACTION ROLES] offer failed for guild=%s event=%s: %s",
                s.guild_id, s.event_date, e,
            )

    view._release_session_lock()
    view.stop()


async def _maybe_offer_faction_roles(
    interaction: discord.Interaction,
    session: RosterBuilderSession,
) -> None:
    """Post the Apply Faction Roles offer after a CS Approve & Post,
    when the alliance has a Judicator role configured AND at least
    one Judicator candidate is on the just-approved roster."""
    import config
    structured = config.get_structured_storm_config(session.guild_id, "CS")
    role_id = int(structured.get("judicator_role_id") or 0)
    if not role_id:
        return  # alliance didn't configure a role — silent skip

    # Find Judicator candidates among the assigned roster members.
    judicator_keys = _find_judicator_candidates(session)
    if not judicator_keys:
        return  # no candidates to apply to — silent skip

    view = _FactionRolesView(
        session=session,
        judicator_role_id=role_id,
        candidate_keys=judicator_keys,
    )
    candidate_names = [
        session.members[k]["name"] for k in judicator_keys
        if k in session.members
    ]
    content = (
        f"⚔️ **Apply Faction Roles?**\n"
        f"Matchmaking will reveal your faction post-roster. When you know "
        f"it's **Rulebringers**, click below to apply the configured "
        f"Judicator role to your candidates: "
        f"{', '.join(candidate_names)}."
    )
    try:
        msg = await interaction.followup.send(
            content=content, view=view, ephemeral=True,
        )
        view.message = msg
    except discord.HTTPException as e:
        logger.warning(
            "[STORM FACTION ROLES] offer-send failed (guild=%s event=%s): %s",
            session.guild_id, session.event_date, e,
        )


def _find_judicator_candidates(session: RosterBuilderSession) -> list[str]:
    """Return member keys for assigned members flagged as Judicator
    candidates via per_member.special_role=judicator rules.

    Matches subjects against both Discord ID (numeric subject) and
    display name (non-Discord subject) — the same two-form resolution
    auto-fill uses. Returns assigned keys only (subs not included for
    v1)."""
    assigned: set[str] = set()
    for zone_members in session.assignments.values():
        assigned.update(zone_members)
    # Paired subs are NOT applied — the spec says primary candidates
    # only; the sub didn't end up in the active slot.
    candidates: list[str] = []
    for rule in session.per_member_rules:
        if rule.sub_type != "special_role" or rule.value.strip().lower() != "judicator":
            continue
        subject = rule.subject.strip()
        # Resolve via the same patterns as the auto-fill pin code.
        for k, m in session.members.items():
            if k not in assigned:
                continue
            if (
                m["name"].strip().lower() == subject.lower()
                or k == subject
                or m.get("discord_id") == subject
            ):
                if k not in candidates:
                    candidates.append(k)
                break
    return candidates


class _FactionRolesView(discord.ui.View):
    """Two-button view: pick the faction, then apply (Rulebringers) or
    acknowledge (Dawnbreakers — no role to apply)."""

    def __init__(
        self,
        *,
        session: RosterBuilderSession,
        judicator_role_id: int,
        candidate_keys: list[str],
    ):
        super().__init__(timeout=1800)  # 30 min — alliance may be mid-match
        self.session = session
        self.judicator_role_id = judicator_role_id
        self.candidate_keys = candidate_keys
        self.message: Optional[discord.Message] = None

        rb_btn = discord.ui.Button(
            label="⚔️ Rulebringers — apply Judicator",
            style=discord.ButtonStyle.success,
        )
        rb_btn.callback = self._make_apply_callback()
        self.add_item(rb_btn)

        db_btn = discord.ui.Button(
            label="🛡️ Dawnbreakers — no role to apply",
            style=discord.ButtonStyle.secondary,
        )
        db_btn.callback = self._make_noop_callback()
        self.add_item(db_btn)

    def _make_apply_callback(self):
        async def _cb(inter: discord.Interaction):
            if inter.user.id != self.session.user_id:
                await inter.response.send_message(
                    "⛔ Only the officer who approved the roster can apply faction roles.",
                    ephemeral=True,
                )
                return
            if self.is_finished():
                return
            self.stop()

            await inter.response.defer(ephemeral=True, thinking=True)

            guild = inter.guild
            if guild is None:
                await inter.followup.send(
                    "⚠️ Couldn't resolve the guild.", ephemeral=True,
                )
                return

            role = guild.get_role(self.judicator_role_id)
            if role is None:
                await inter.followup.send(
                    f"⚠️ The configured Judicator role (<@&{self.judicator_role_id}>) "
                    f"no longer exists or the bot can't see it. Re-run "
                    f"`/setup_canyonstorm` to pick a new one.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            # Up-front preflight: bot needs Manage Roles AND its top
            # role must sit above the Judicator role in the hierarchy.
            # Without this preflight, every per-member `add_roles` call
            # would raise Forbidden and the officer would see N
            # identical error rows. One clear "fix your bot perms"
            # message before the loop is more actionable.
            me = guild.me
            if me is None:
                await inter.followup.send(
                    "⚠️ Couldn't resolve the bot's own member in this guild.",
                    ephemeral=True,
                )
                return
            if not me.guild_permissions.manage_roles:
                await inter.followup.send(
                    "⛔ I don't have **Manage Roles** in this server, so I "
                    "can't apply the Judicator role. Grant the permission "
                    "to my role and try again.",
                    ephemeral=True,
                )
                return
            if role.position >= me.top_role.position:
                await inter.followup.send(
                    f"⛔ The Judicator role (<@&{role.id}>) sits at or above "
                    f"my own role in the hierarchy, so Discord won't let me "
                    f"assign it. In **Server Settings → Roles**, move my "
                    f"role above the Judicator role and try again.",
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            applied: list[str] = []
            skipped_no_member: list[str] = []
            skipped_already: list[str] = []
            failed: list[tuple[str, str]] = []

            for key in self.candidate_keys:
                m = self.session.members.get(key)
                if not m:
                    continue
                discord_id_str = m.get("discord_id") or ""
                # Subject was a Discord ID directly?
                if not discord_id_str and key.isdigit():
                    discord_id_str = key
                if not discord_id_str:
                    skipped_no_member.append(m["name"])
                    continue
                try:
                    member = guild.get_member(int(discord_id_str))
                except (TypeError, ValueError):
                    skipped_no_member.append(m["name"])
                    continue
                if member is None:
                    skipped_no_member.append(m["name"])
                    continue
                if role in member.roles:
                    skipped_already.append(m["name"])
                    continue
                try:
                    await member.add_roles(
                        role, reason="Storm faction roles: Judicator (Rulebringers)",
                    )
                    applied.append(m["name"])
                except discord.Forbidden:
                    failed.append((m["name"], "missing permission"))
                except discord.HTTPException as e:
                    failed.append((m["name"], str(e)[:80]))

            for item in self.children:
                item.disabled = True
            try:
                await inter.message.edit(view=self)
            except discord.HTTPException:
                pass

            # Header reflects whether any roles actually applied vs. all
            # candidates were already-had / off-Discord — "✅ Judicator
            # role applied" lies when zero applied.
            if applied:
                summary_lines = ["✅ Judicator role applied:"]
                summary_lines.append(f"  • Applied to: {', '.join(applied)}")
            else:
                summary_lines = ["ℹ️ Judicator role apply — nothing to apply:"]
            if skipped_already:
                summary_lines.append(
                    f"  • Already had the role: {', '.join(skipped_already)}"
                )
            if skipped_no_member:
                summary_lines.append(
                    f"  • Not on Discord / not in server: {', '.join(skipped_no_member)}"
                )
            if failed:
                fail_str = "; ".join(f"{n} ({why})" for n, why in failed)
                summary_lines.append(f"  • Failed: {fail_str}")
            if not (applied or skipped_already or skipped_no_member or failed):
                summary_lines = [
                    "ℹ️ No role applications needed — all candidates either "
                    "already had the role or weren't on Discord."
                ]
            await inter.followup.send("\n".join(summary_lines), ephemeral=True)
        return _cb

    def _make_noop_callback(self):
        async def _cb(inter: discord.Interaction):
            if inter.user.id != self.session.user_id:
                await inter.response.send_message(
                    "⛔ Only the officer who approved the roster can resolve faction roles.",
                    ephemeral=True,
                )
                return
            if self.is_finished():
                return
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                await inter.response.edit_message(
                    content="🛡️ Dawnbreakers acknowledged — no role to apply.",
                    view=self,
                )
            except discord.HTTPException:
                pass
        return _cb

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


_ROSTERS_HEADER = [
    "Event Date", "Team", "Zone", "Member", "Role",
    "Power at Assignment", "Discord ID", "Override Below Floor",
    "Paired With", "Posted At (UTC)",
]


def _write_rosters_tab(session: RosterBuilderSession) -> list[str]:
    """Append one row per slot to the alliance's configured rosters_tab.
    Returns a list of soft error strings (empty on success).

    The `Override Below Floor` column captures whether the officer
    explicitly assigned the member below the effective zone floor —
    so post-event review (attendance, no-show tagging) can flag the
    decision. Subs don't carry the flag (the eligibility gate is
    primary-only). If a previously-flagged member was later unassigned
    and never re-assigned, no row is written for them, so stale flags
    can't survive.
    """
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
    else:
        # Header migration: alliances created their rosters_tab before
        # `Paired With` was added in #132. New writes still produce 10
        # cells per row, but their stored header is the old 9-column
        # shape — so `storm_history.load_event_roster` would do
        # `header.index("Paired With")` → ValueError → `paired_with` is
        # silently empty. Detect the older header and rewrite it in
        # place before appending new data. Use `get_all_values()[0]`
        # rather than `row_values(1)` so the fake worksheet in tests
        # doesn't need a new method.
        try:
            all_values = ws.get_all_values()
            existing = all_values[0] if all_values else []
        except Exception as e:
            existing = []
            errors.append(f"rosters tab header read failed: {e}")
        if existing and "Paired With" not in existing:
            try:
                ws.update("A1", [_ROSTERS_HEADER], value_input_option="RAW")
            except Exception as e:
                errors.append(
                    f"rosters tab header migration failed (data still "
                    f"appended, but readers may not see Paired With): {e}"
                )

    from config import _utcnow_iso
    posted_at = _utcnow_iso()
    rows: list[list[str]] = []
    for z in session.preset.zones:
        for key in session.assignments.get(z.zone, []):
            m = session.members.get(key)
            if not m:
                continue
            power = m.get("power")
            override = "yes" if key in session.below_floor_overrides else ""
            rows.append([
                session.event_date or "",
                session.team or "",
                z.zone,
                m["name"],
                "primary",
                str(power) if power is not None else "unknown",
                m.get("discord_id") or "",
                override,
                "",  # Paired With — primary rows leave blank.
                posted_at,
            ])
            # In paired mode, the sub partnered with this primary is
            # written as its own row immediately after, so post-event
            # review can see "Alice (primary) at Power Tower → Bob (sub
            # paired)" in row order.
            if session.is_paired:
                sub_key = session.paired_subs.get(key)
                if sub_key:
                    sub_m = session.members.get(sub_key)
                    if sub_m:
                        sub_power = sub_m.get("power")
                        sub_override = (
                            "yes" if sub_key in session.below_floor_overrides else ""
                        )
                        rows.append([
                            session.event_date or "",
                            session.team or "",
                            z.zone,
                            sub_m["name"],
                            "sub",
                            str(sub_power) if sub_power is not None else "unknown",
                            sub_m.get("discord_id") or "",
                            sub_override,
                            m["name"],  # Paired With → the primary
                            posted_at,
                        ])
    # Pool-mode subs (or paired-mode overflow) — written without a
    # paired-with reference.
    for key in session.subs:
        m = session.members.get(key)
        if not m:
            continue
        power = m.get("power")
        # Subs aren't subject to the per-zone floor; don't propagate the
        # override flag to the sub slot.
        rows.append([
            session.event_date or "",
            session.team or "",
            "",
            m["name"],
            "sub",
            str(power) if power is not None else "unknown",
            m.get("discord_id") or "",
            "",
            "",  # Paired With — pool subs have no specific primary.
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

    # Load powers + rules. Passes the live guild so the reader can
    # infer non-Discord status for rows with stale or blank Discord IDs
    # (#139) — explicit `not_on_discord` column still wins.
    members, roster_errors = _read_roster_powers(
        interaction.guild_id, event_type, guild=interaction.guild,
    )

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

    # Structured mode: claim the per-(guild, event_type, event_date,
    # team) build slot so a second officer can't independently build
    # the same team for the same event in parallel.
    if is_structured:
        import config
        ok, holder = config.claim_storm_session(
            interaction.guild_id, event_type, event_date, team,
            interaction.user.id,
        )
        if not ok:
            await interaction.followup.send(
                f"⚠️ Another officer (<@{holder}>) is already building "
                f"**Team {team or 'roster'}** for event **{event_date}**. "
                f"Wait for them to finish, or coordinate before re-opening.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

    # Read the per-event-type sub_mode from structured config so the
    # builder branches on `pool` vs `paired` correctly. Storage layer
    # normalises unknowns to "pool"; defense in depth, the session
    # __init__ re-normalises too.
    import config
    structured_cfg = config.get_structured_storm_config(
        interaction.guild_id, event_type,
    )
    sub_mode = structured_cfg.get("sub_mode") or "pool"

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
        sub_mode=sub_mode,
    )
    # Seed errors from the roster read FIRST so _apply_rules_to_session
    # can append its own (e.g. unmatched per_member subjects) without
    # being clobbered.
    session.roster_errors = list(roster_errors)
    _apply_rules_to_session(session)

    view = RosterBuilderView(session)
    embed = _render_builder_embed(session)
    try:
        msg = await interaction.followup.send(embed=embed, view=view)
        view.message = msg
    except discord.HTTPException as e:
        # If the followup send fails after claiming the session lock,
        # release the lock so the next attempt isn't blocked.
        logger.warning(
            "[STORM BUILDER] failed to send builder view (guild=%s event=%s): %s",
            interaction.guild_id, event_date, e,
        )
        view._release_session_lock()
        raise


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
