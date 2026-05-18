"""
Manual roster builder for Desert Storm and Canyon Storm (#128).

Opened from the `👁️ View sign-ups + set up teams` officer view via
its "Apply preset" picker (hub-restructure #187; legacy
`/desertstorm strategy apply` slash subcommand pre-#125). Leadership
picks the team, the bot loads the named preset + member rules + roster
powers, and the builder enforces per-zone power floors as members are
assigned.

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

import asyncio
import io
import logging
from typing import Optional

import discord

from storm_event_hub import (
    HUB_COMMAND,
    HUB_BTN_VIEW_SIGNUPS,
    HUB_BTN_PAST_ROSTERS,
)

logger = logging.getLogger(__name__)


# ── Power-data reader ────────────────────────────────────────────────────────
#
# Pulls each member's configured power value off the alliance roster
# Sheet so the eligibility gate has something to filter against. Reads
# the column header configured by the storm setup wizard
# under "Power Metric Column" — falling back to the design rule of
# "exclude unknown power, never silently coerce to zero."

def _read_power_column_header(guild_id: int, event_type: str) -> str:
    """Return the human-readable header text for the configured power
    column (row 1 of the roster Sheet at the configured letter), with
    the Your/My-stripping rule applied so `Your Power` reads naturally
    in DMs as `your Power` (not `your Your Power`).

    Used by the power-refresh DM (#138) to tell members which power
    value the bot is checking — leadership picks the column by letter
    (Rule C / #165) but members need to see the header label so they
    know what to update on the sheet.

    Returns `""` when the sheet/config isn't readable, the column is
    out of range, or the header cell is blank. Callers fall back to
    generic wording in that case.
    """
    import config
    try:
        roster_cfg = config.get_member_roster_config(guild_id)
    except Exception:
        return ""
    if not roster_cfg.get("enabled"):
        return ""
    try:
        structured = config.get_structured_storm_config(guild_id, event_type)
    except Exception:
        return ""
    power_letter = (structured.get("power_metric_column") or "B").strip().upper()
    power_col = config.power_column_letter_to_index(power_letter)
    try:
        ws = config.get_member_roster_sheet(
            guild_id, roster_cfg.get("tab_name") or "Member Roster",
        )
        header_row = ws.row_values(1)
    except Exception:
        return ""
    if not (0 <= power_col < len(header_row)):
        return ""
    raw = header_row[power_col].strip()
    if not raw:
        return ""
    # Strip leading "Your"/"My" so the DM reads "your Power" not
    # "your Your Power" / "your My Squad Power".
    lowered = raw.lower()
    if lowered.startswith("your "):
        raw = raw[5:].strip()
    elif lowered.startswith("my "):
        raw = raw[3:].strip()
    return raw


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

    power_letter = (structured.get("power_metric_column") or "B").strip().upper()
    power_col = config.power_column_letter_to_index(power_letter)

    if not roster_cfg.get("enabled"):
        errors.append(
            "member-roster sync isn't enabled — without /members sync the "
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
    # Power column is a configured letter (Rule C / #165) — A=0, B=1, etc.
    # If the configured letter sits past the end of the header row,
    # surface a soft warning + treat power as unreadable for every row.
    power_col_header = (
        header[power_col].strip()
        if 0 <= power_col < len(header) else ""
    )
    if not power_col_header:
        errors.append(
            f"power column {power_letter} doesn't exist in your roster "
            f"Sheet header (or is blank). Re-run the setup wizard's Power "
            f"Metric Column step to pick a different column."
        )
        logger.warning(
            "[STORM ROSTER] power column letter %r resolves to index %d, "
            "which is past the header row (len=%d) for guild=%s event=%s. "
            "Header: %s",
            power_letter, power_col, len(header),
            guild_id, event_type, header,
        )
    # Prefer the bot-maintained presence column when present. Falls
    # back to the legacy `not_on_discord` column for back-compat with
    # alliances that haven't synced under the new bot version yet.
    presence_col = _find_col("is this user in discord?")
    not_disc_col = _find_col("not_on_discord")
    if not_disc_col < 0:
        not_disc_col = _find_col("not on discord")

    # Diagnostic logging — team-test feedback flagged "matching by name
    # not Discord ID" and "power not reading even when in the sheet."
    # Surface the exact column resolution so a single log line answers
    # which column the bot is looking at.
    logger.info(
        "[STORM ROSTER] guild=%s event=%s column resolution: "
        "id_col=%d (cfg discord_id_col=%d), name_col=%d (cfg display_col=%d), "
        "power_col=%d (letter %s, header %r), "
        "presence_col=%d, not_disc_col=%d, header=%s",
        guild_id, event_type,
        id_col, int(roster_cfg.get("discord_id_col", 0)),
        name_col, int(roster_cfg.get("display_col", roster_cfg.get("name_col", 1))),
        power_col, power_letter, power_col_header,
        presence_col, not_disc_col, header,
    )

    truthy = {"1", "true", "yes", "y", "x", "t"}
    has_not_col = not_disc_col >= 0
    has_presence_col = presence_col >= 0
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

        # Non-Discord detection. Resolution order:
        #   1. New "Is this user in Discord?" column (bot-maintained,
        #      Yes/No values) wins when present and non-blank.
        #   2. Legacy explicit `not_on_discord` column (alliance-
        #      managed truthy flag) wins next, for back-compat with
        #      alliances on older bot versions.
        #   3. ID-diff inference fills the gap when neither column
        #      gives a definitive answer.
        if has_presence_col:
            presence_cell = _cell(presence_col).lower()
            if presence_cell == "yes":
                members[discord_id or name] = {
                    "key":            discord_id or name,
                    "name":           name or discord_id,
                    "discord_id":     discord_id,
                    "power":          power_val,
                    "not_on_discord": False,
                }
                continue
            if presence_cell == "no":
                key = discord_id or name
                members[key] = {
                    "key":            key,
                    "name":           name or discord_id,
                    "discord_id":     discord_id,
                    "power":          power_val,
                    "not_on_discord": True,
                }
                continue
            # Blank / unknown value → fall through to legacy + inference.
        explicit_set = (
            _cell(not_disc_col).lower() in truthy if has_not_col else False
        )
        inferred = False
        if not explicit_set:
            if not discord_id:
                inferred = True
            elif not discord_id.isdigit():
                # Non-numeric placeholder ("TBD", "abc"): treat as
                # non-Discord per the #139 spec. Matches the officer
                # view's reader so the two paths can't disagree.
                inferred = True
            elif guild is not None:
                try:
                    member = guild.get_member(int(discord_id))
                except (TypeError, ValueError):
                    member = None
                # Bots aren't real alliance members. If the roster
                # Sheet maps an ID to a bot (admin pasted the wrong
                # ID), treat it as a stale match rather than counting
                # the bot as a Discord member.
                if member is None or member.bot:
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
        # Per-zone assignments: {zone_name: [member_key, ...]}.
        # For phase-aware presets (#152), this is the Phase 1 dict;
        # Phase 2 lives in `assignments_p2`, Phase 3 (CS / 3-phase
        # presets) in `assignments_p3`. For flat presets, only
        # `assignments` is used and the other phases stay empty.
        self.assignments: dict[str, list[str]] = {z.zone: [] for z in preset.zones}
        self.assignments_p2: dict[str, list[str]] = {z.zone: [] for z in preset.zones}
        self.assignments_p3: dict[str, list[str]] = {z.zone: [] for z in preset.zones}
        # The currently-selected phase in the UI (1, 2, or 3). Flat
        # presets pin to 1.
        self.selected_phase: int = 1
        # Flat sub pool. In `paired` mode, subs lives in
        # `paired_subs` instead — this list stays empty.
        self.subs: list[str] = []
        # Paired-mode pairings: {primary_key: sub_key}. Only populated
        # when sub_mode == "paired"; the embed + writer branch on
        # presence of this dict. Each phase carries its own pairing map
        # so a member can have a different sub per phase.
        self.paired_subs: dict[str, str] = {}
        self.paired_subs_p2: dict[str, str] = {}
        self.paired_subs_p3: dict[str, str] = {}
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
        # Each phase has its own override set.
        self.below_floor_overrides: set[str] = set()
        self.below_floor_overrides_p2: set[str] = set()
        self.below_floor_overrides_p3: set[str] = set()
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

    @property
    def is_phase_aware(self) -> bool:
        """True iff the loaded preset has 2+ phases. Phase-aware
        sessions surface per-phase sub-slots per zone in the builder
        and write phase-grouped mail. Flat presets ignore the phase-2
        and phase-3 attributes entirely."""
        return self.phase_count >= 2

    @property
    def phase_count(self) -> int:
        """How many phases the loaded preset declares. 0 means flat;
        2 or 3 means phase-aware. Reads through to
        `preset.phase_count` (with `getattr` for backward compat with
        older PresetBuffer instances that pre-date the field)."""
        return int(getattr(self.preset, "phase_count", 0))

    def assignments_for_phase(self, phase: int) -> dict[str, list[str]]:
        """Return the assignment dict for a given phase. Centralised so
        downstream code doesn't branch on the `_p2` / `_p3` attribute
        names."""
        if phase == 2:
            return self.assignments_p2
        if phase == 3:
            return self.assignments_p3
        return self.assignments

    def paired_subs_for_phase(self, phase: int) -> dict[str, str]:
        if phase == 2:
            return self.paired_subs_p2
        if phase == 3:
            return self.paired_subs_p3
        return self.paired_subs

    def below_floor_overrides_for_phase(self, phase: int) -> set[str]:
        if phase == 2:
            return self.below_floor_overrides_p2
        if phase == 3:
            return self.below_floor_overrides_p3
        return self.below_floor_overrides

    def iter_phases(self) -> list[int]:
        """Phases this session iterates over. Flat presets yield [1];
        2-phase presets yield [1, 2]; 3-phase presets yield [1, 2, 3]."""
        if self.phase_count >= 3:
            return [1, 2, 3]
        if self.phase_count >= 2:
            return [1, 2]
        return [1]

    def floor_for_zone(self, zone_name: str) -> int:
        """Per-team min_power for this zone. Team A uses min_power_a;
        Team B uses min_power_b. Applies identically to DS and CS
        post-Rule A (#166)."""
        z = self.preset.find_zone(zone_name)
        if z is None:
            return 0
        if self.team == "B":
            return int(z.min_power_b or 0)
        return int(z.min_power_a or 0)

    def assigned_member_keys(self) -> set[str]:
        """Every member currently slotted somewhere — any phase, any
        zone, the flat sub pool, OR any paired-sub seat. Used by
        callsites that want a global "anywhere on the roster" check.

        For phase-aware presets this unions both phases, which is the
        conservative default (won't double-assign within either phase).
        Per-phase eligibility filtering uses
        `assigned_member_keys_in_phase` instead — that's what lets a
        member assigned to Phase 1 at zone A be picked for Phase 2 at
        zone B (the migration use case)."""
        keys: set[str] = set()
        for phase in self.iter_phases():
            for zone_members in self.assignments_for_phase(phase).values():
                keys.update(zone_members)
            keys.update(self.paired_subs_for_phase(phase).values())
        keys.update(self.subs)
        return keys

    def assigned_member_keys_in_phase(self, phase: int) -> set[str]:
        """Members slotted in the given phase only. The picker uses this
        so a Phase 1 assignment doesn't lock a member out of a Phase 2
        slot in a phase-aware preset.

        Sub pool is event-level (not phase-scoped) so it's always
        excluded — a member sitting in the global sub pool is unavailable
        for primary assignment in either phase."""
        keys: set[str] = set()
        for zone_members in self.assignments_for_phase(phase).values():
            keys.update(zone_members)
        keys.update(self.paired_subs_for_phase(phase).values())
        keys.update(self.subs)
        return keys

    def has_existing_assignments(self) -> bool:
        """True iff the session carries any roster data the auto-fill
        would clobber — at least one zone has a primary, the subs pool
        is non-empty, or any phase has a pairing. Used by the
        auto-fill button to gate the destructive-confirm prompt
        (Decision #9 / #171). A freshly-opened session reads False so
        first-click auto-fill skips the confirm."""
        for phase in self.iter_phases():
            for zone_members in self.assignments_for_phase(phase).values():
                if zone_members:
                    return True
            if self.paired_subs_for_phase(phase):
                return True
        if self.subs:
            return True
        return False

    def unpaired_primaries(self) -> list[str]:
        """In paired mode, the list of zone members who don't yet have
        a paired sub for the *currently selected phase*. Order matches
        zone-then-roster order so the UI prompt is deterministic.
        Returns [] when sub_mode=pool."""
        if not self.is_paired:
            return []
        assignments = self.assignments_for_phase(self.selected_phase)
        pairings = self.paired_subs_for_phase(self.selected_phase)
        unpaired = []
        for zone in self.preset.zones:
            for key in assignments.get(zone.zone, []):
                if key not in pairings:
                    unpaired.append(key)
        return unpaired

    def zone_member_count(self, zone_name: str, phase: int | None = None) -> int:
        """Member count at this zone. `phase=None` returns the count for
        the currently selected phase (Phase 1 on flat presets)."""
        phase = phase if phase is not None else self.selected_phase
        return len(self.assignments_for_phase(phase).get(zone_name, []))

    def zone_capacity(self, zone_name: str, phase: int | None = None) -> int:
        """Per-zone capacity. On flat presets, returns `max_players` and
        ignores `phase`. On phase-aware presets, returns the matching
        per-phase cap (defaults to the selected phase)."""
        z = self.preset.find_zone(zone_name)
        if z is None:
            return 0
        if not self.is_phase_aware:
            return int(z.max_players)
        phase = phase if phase is not None else self.selected_phase
        return z.max_for_phase(phase)

    def prune_stale_pairings(self) -> None:
        """Drop paired-sub entries whose primary is no longer in a zone.

        Called after Unassign / Move-to-subs. Without this, an unassigned
        primary's old pairing would linger and surface stale data in
        the embed + the rosters_tab write. Walks both phases for
        phase-aware sessions."""
        for phase in self.iter_phases():
            primaries_in_zones: set[str] = set()
            for zone_members in self.assignments_for_phase(phase).values():
                primaries_in_zones.update(zone_members)
            pairings = self.paired_subs_for_phase(phase)
            for primary in list(pairings.keys()):
                if primary not in primaries_in_zones:
                    del pairings[primary]

    def prune_stale_overrides(self) -> None:
        """Drop override entries for members no longer in any zone.

        The override flag captures "officer assigned this member below
        the floor" at the moment of assignment. If they're later
        unassigned (zone cleared) or moved to subs (subs don't carry
        the flag), the entry shouldn't survive — otherwise a later
        re-assignment without the toggle would still mark the slot.
        Walks both phases for phase-aware sessions.
        """
        for phase in self.iter_phases():
            currently_in_zones: set[str] = set()
            for zone_members in self.assignments_for_phase(phase).values():
                currently_in_zones.update(zone_members)
            overrides = self.below_floor_overrides_for_phase(phase)
            overrides &= currently_in_zones
            # Replace contents in place — same set object so external
            # references stay valid.
            if phase == 3:
                self.below_floor_overrides_p3 = overrides
            elif phase == 2:
                self.below_floor_overrides_p2 = overrides
            else:
                self.below_floor_overrides = overrides


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

    Per Decision #7 (#173): a per_member rule whose subject isn't in
    tonight's roster is a silent no-op — the rule means "if this
    member is in tonight's event, do X." They're not in tonight's
    event, so nothing to apply and nothing to report. The prior
    audit's `roster_errors` warning ("per_member rule(s) reference
    roster names that aren't in the current roster") is gone.
    """
    # per_member zone rules — pin a specific member to a specific zone
    # if they exist on the roster.
    for rule in session.per_member_rules:
        if rule.sub_type != "zone":
            continue
        subject = rule.subject.strip()
        match_key = _resolve_per_member_subject(session.members, subject)
        if match_key is None:
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


# ── Auto-fill (#134) ─────────────────────────────────────────────────────────


def _auto_fill_session(session: RosterBuilderSession) -> dict:
    """Auto-fill the roster from member rules + power-based greedy fill.

    Resets the current roster (assignments + subs + override flags)
    before filling, so a re-click of the button is "redo from scratch"
    rather than "stack onto current state."

    Algorithm, in order:
      1. per_member zone rules — pin members to their named zone if
         capacity, the member is in the signed-up pool, and the zone
         exists in the preset. **Applied to Phase 1 only** on phase-
         aware presets; the rule model doesn't yet carry a phase
         dimension (#152 v1 simplification). Phase 2 greedy fill picks
         up the same members later if they're eligible there too.
      2. Greedy fill — runs once per phase. For each zone in priority
         order (lowest int first; priority=0 sorts last), fills the
         phase's remaining slots from the eligibility-gated pool. The
         per-phase eligibility filter (#152) lets a Phase 1-assigned
         member still appear in the Phase 2 picker for the migration
         case.
      3. Spillover — unassigned members with known power go into the
         event-level sub pool. Power-unknown members are reported as
         gaps so the officer can decide who to override below the
         floor.

    Returns the summary dict (also stored on `session.auto_fill_summary`).

    The fill is officer-correctable — every assignment can be tweaked
    via the picker before Approve & Post.
    """
    # ── Reset state ── auto-fill is "redo from scratch".
    # Phase-aware: clear every phase's dicts. Flat: only phase 1 is
    # touched.
    for phase in session.iter_phases():
        for zone in list(session.assignments_for_phase(phase).keys()):
            session.assignments_for_phase(phase)[zone] = []
    session.subs = []
    session.paired_subs.clear()
    session.paired_subs_p2.clear()
    session.paired_subs_p3.clear()
    session.below_floor_overrides.clear()
    session.below_floor_overrides_p2.clear()
    session.below_floor_overrides_p3.clear()

    summary = {
        "per_member_rules_applied": 0,
        "power_band_rules_applied": 0,
        "auto_filled_by_power":     0,
        # Decision #14 (#171): track each auto-pair explicitly so the
        # summary can list `Alice ↔ Bob, Carol ↔ Dan` instead of a
        # bare count. Officers edit auto-paired subs most often, so
        # visibility matters.
        "auto_paired_subs":         [],  # list[str] each "PrimaryName ↔ SubName"
        "gaps":                     [],  # member names with no parseable power
        "conflicts":                [],  # short strings: rule application failures
    }

    # Remember the officer's UI cursor; we mutate it while filling each
    # phase so capacity / member-count helpers resolve correctly, then
    # restore at the end.
    original_phase = session.selected_phase

    # ── 1. per_member zone rules ── (Phase 1 only on phase-aware)
    # Per Decision #7 (#173): if the rule's subject isn't in tonight's
    # roster the rule is a silent no-op — nothing to apply, nothing to
    # report. Only the other conflict shapes (unknown zone, full zone,
    # already-pinned-elsewhere) still surface in the summary.
    session.selected_phase = 1
    for rule in session.per_member_rules:
        if rule.sub_type != "zone":
            continue
        subject = rule.subject.strip()
        zone = rule.value.strip()
        match_key = _resolve_per_member_subject(session.members, subject)
        if match_key is None:
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
        # Cross-phase duplicate check: pinned member can't already be
        # assigned in any phase or in the sub pool.
        if match_key in session.assigned_member_keys():
            summary["conflicts"].append(
                f"{subject} pinned to multiple zones"
            )
            continue
        session.assignments_for_phase(1)[zone].append(match_key)
        summary["per_member_rules_applied"] += 1
        member = session.members.get(match_key)
        if member is not None and member.get("power") is None:
            session.below_floor_overrides_for_phase(1).add(match_key)

    # ── 2. Greedy fill by zone priority, per phase ──
    # On flat presets we order once by the single `priority` field.
    # On phase-aware presets the order is per-phase, so a preset can
    # prioritise Power Tower in Phase 1 and Virus Lab in Phase 3.
    # priority=0 means "no priority set" → sorts to the end via 9999.
    def _phase_priority_key(p):
        def key(z):
            prio = z.priority_for_phase(p) if session.is_phase_aware else z.priority
            return prio if prio > 0 else 9999
        return key

    for phase in session.iter_phases():
        session.selected_phase = phase
        phase_assignments = session.assignments_for_phase(phase)
        zones_sorted = sorted(session.preset.zones, key=_phase_priority_key(phase))
        for z in zones_sorted:
            remaining = session.zone_capacity(z.zone) - session.zone_member_count(z.zone)
            if remaining <= 0:
                continue
            eligible_keys, _below = _eligible_member_keys_for_zone(session, z.zone)
            if not eligible_keys:
                continue
            for key in eligible_keys[:remaining]:
                phase_assignments[z.zone].append(key)
                summary["auto_filled_by_power"] += 1
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
    # Paired-sub pairing runs per phase: each phase's unpaired primaries
    # get the strongest remaining sub for that phase. Subs are
    # event-level so a member paired in phase 1 isn't pickable for
    # phase 2 pairing (avoids double-booking the same sub seat).
    if session.is_paired:
        for phase in session.iter_phases():
            session.selected_phase = phase
            phase_assignments = session.assignments_for_phase(phase)
            phase_pairings = session.paired_subs_for_phase(phase)
            unpaired = session.unpaired_primaries()
            for primary_key in unpaired:
                primary_zone = None
                for zone, zmembers in phase_assignments.items():
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
                sub_key = eligible_sub_keys[0]
                phase_pairings[primary_key] = sub_key
                primary_m = session.members.get(primary_key)
                sub_m = session.members.get(sub_key)
                primary_name = primary_m["name"] if primary_m else primary_key
                sub_name = sub_m["name"] if sub_m else sub_key
                summary["auto_paired_subs"].append(
                    f"{primary_name} ↔ {sub_name}"
                )

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
            session.subs.append(key)

    session.selected_phase = original_phase
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


def _zone_status_glyph(count: int, cap: int) -> str:
    """Color-coded fill marker for a zone slot.
      —  zone has no capacity in this phase (e.g. center zones in Phase 1)
      ⬜ empty
      🟡 partially filled
      ✅ at or above capacity
    """
    if cap <= 0 and count == 0:
        return "—"
    if count == 0:
        return "⬜"
    if count < cap:
        return "🟡"
    return "✅"


def _format_zone_member_list(
    session: "RosterBuilderSession", member_keys: list[str], phase: int,
) -> str:
    """Render the comma-separated member list for one zone in one phase.

    In paired mode, paired primaries render with `+ sub <name>` and
    unpaired primaries with the `⚠️` glyph so the officer can spot
    missing pairings at a glance. The pairing lookup is phase-scoped —
    a primary in Phase 1 with no sub paired for Phase 1 still flags
    even if Phase 2 has a pairing.
    """
    names: list[str] = []
    pairings = session.paired_subs_for_phase(phase)
    for k in member_keys:
        m = session.members.get(k)
        primary_label = m["name"] if m else f"<unknown:{k}>"
        if session.is_paired:
            sub_key = pairings.get(k)
            if sub_key:
                sub_m = session.members.get(sub_key)
                sub_label = sub_m["name"] if sub_m else f"<unknown:{sub_key}>"
                names.append(f"{primary_label} + sub {sub_label}")
            else:
                names.append(f"{primary_label} ⚠️")
        else:
            names.append(primary_label)
    return ", ".join(names) if names else "(empty)"


def _render_zone_line(session: RosterBuilderSession, zone_name: str) -> str:
    """Render one zone's row in the builder embed.

    Flat presets render as a single line: `{status} **Zone** (n/cap): names`.

    Phase-aware presets (#172 / Rule L) render per-zone-per-phase: a
    bolded zone header followed by one indented line per phase, each
    showing that phase's count, capacity, and member list. The header's
    status glyph reflects the currently-selected phase so the picker /
    assign actions match what's coloured red/yellow/green.
    """
    z = session.preset.find_zone(zone_name)
    if z is None:
        return f"• {zone_name} (?/?)"

    from storm_icons import zone_emoji_prefix
    icon = zone_emoji_prefix(zone_name)  # "" until #158 emojis upload.
    marker = " ←" if zone_name == session.selected_zone else ""

    if session.is_phase_aware:
        # Header status reflects the selected phase. Toggling phases
        # via the Phase nav recolors the header so the officer can see
        # "what's full in the phase I'm editing right now."
        sel_count = session.zone_member_count(zone_name)
        sel_cap = session.zone_capacity(zone_name)
        header_status = _zone_status_glyph(sel_count, sel_cap)
        header = f"{header_status} {icon}**{zone_name}**{marker}"

        phase_lines: list[str] = []
        for p in session.iter_phases():
            members = session.assignments_for_phase(p).get(zone_name, [])
            cap = int(z.max_for_phase(p))
            count = len(members)
            names = _format_zone_member_list(session, members, phase=p)
            # Box-drawing prefix ("   └ ") visually nests the phase
            # row under the zone header without relying on Discord's
            # inconsistent leading-space rendering in embed bodies.
            phase_lines.append(f"   └ Stage {p}: {count}/{cap} · {names}")
        return "\n".join([header] + phase_lines)

    # Flat preset — single-line shape unchanged from pre-#172.
    sel_count = session.zone_member_count(zone_name)
    sel_cap = int(z.max_players)
    status = _zone_status_glyph(sel_count, sel_cap)
    member_keys = session.assignments_for_phase(session.selected_phase).get(zone_name, [])
    names_part = _format_zone_member_list(session, member_keys, phase=session.selected_phase)
    return f"{status} {icon}**{zone_name}** ({sel_count}/{sel_cap}){marker}: {names_part}"


def _render_builder_embed(session: RosterBuilderSession) -> discord.Embed:
    event_label = "Desert Storm" if session.event_type == "DS" else "Canyon Storm"
    if session.event_type == "DS":
        team_label = f": Team {session.team}"
    elif session.preset.faction and session.preset.faction != "Either":
        team_label = f": {session.preset.faction}"
    else:
        team_label = ""
    title = f"🛡️ Roster Builder: {session.preset.name}{team_label}"

    lines: list[str] = []
    lines.append(f"🗺️ {event_label}")
    if session.event_type == "DS":
        floor_label = "Min A" if session.team == "A" else "Min B"
        lines.append(f"⚖️ Enforcing **{floor_label}** minimum for this team")
    # Phase-aware (#152): surface the active phase prominently so an
    # officer can see at a glance which phase the picker + assign
    # buttons will mutate.
    if session.is_phase_aware:
        lines.append(
            f"🔀 Editing **Stage {session.selected_phase}** "
            f"_(use the Stage buttons below to switch)_"
        )
    lines.append("")
    if session.is_paired:
        lines.append("**📋 Zones** _(paired mode: each primary has a dedicated sub)_")
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
                f"⚠️ **Unpaired primaries ({len(unpaired)})**: {unpaired_names}. "
                f"Click **🔁 Pair subs** to attach a sub to any of them. "
                f"Subs may not cover every primary; that's expected."
            )
        # Surface the available subs pool — paired subs live inline
        # against each primary, but auto-fill or manual add can leave
        # extra subs in `session.subs` that don't belong to any
        # primary yet. Without this line the officer can't tell those
        # exist from the embed.
        sub_names = [
            session.members[k]["name"] for k in session.subs if k in session.members
        ]
        if sub_names:
            lines.append(
                f"🪑 **Available subs ({len(sub_names)})**: "
                f"{', '.join(sub_names)}. Pair via **🔁 Pair subs** "
                f"or leave as bench."
            )
    else:
        sub_names = [
            session.members[k]["name"] for k in session.subs if k in session.members
        ]
        if sub_names:
            lines.append(f"🪑 **Subs ({len(sub_names)})**: {', '.join(sub_names)}")
        else:
            lines.append("🪑 **Subs**: _(none)_")
    lines.append("")

    # Sum across every phase the preset declares. On flat presets this
    # collapses to the original Phase 1 / max_players counts; on phase-
    # aware presets the gauge sums P1+P2(+P3) assignments and the
    # per-phase capacities so the readout matches reality (the prior
    # code summed only Phase 1 and divided by `max_players` which is
    # unset for phase-aware zones — produced "Filled: 2 / 0").
    # Per Rule L (#172), phase-aware presets surface per-phase counts
    # so each phase's fill state is visible at a glance.
    if session.is_phase_aware:
        per_phase = []
        for p in session.iter_phases():
            assigned = sum(
                len(zone_members)
                for zone_members in session.assignments_for_phase(p).values()
            )
            cap = sum(
                int(z.max_for_phase(p)) for z in session.preset.zones
            )
            per_phase.append(f"S{p}: {assigned}/{cap}")
        lines.append(f"📊 **Filled:** {', '.join(per_phase)}")
    else:
        total_assigned = sum(
            len(zone_members)
            for zone_members in session.assignments_for_phase(1).values()
        )
        total_capacity = session.preset.total_capacity()
        lines.append(f"📊 **Filled:** {total_assigned} / {total_capacity}")

    selected = session.selected_zone
    if selected:
        from storm_icons import zone_emoji_prefix
        active_icon = zone_emoji_prefix(selected)
        preset_floor = session.floor_for_zone(selected)
        effective_floor = _effective_floor_for_zone(session, selected)
        from storm_strategy import format_power
        if effective_floor != preset_floor:
            # A power_band Member Rule lowered the effective minimum for
            # this zone — surface both so leadership can tell at a
            # glance which rule is in play.
            lines.append(
                f"🎯 **Active zone:** {active_icon}**{selected}** · minimum "
                f"**{format_power(effective_floor) if effective_floor else '(none)'}** "
                f"_(preset minimum {format_power(preset_floor)} relaxed by power_band rule)_"
            )
        else:
            lines.append(
                f"🎯 **Active zone:** {active_icon}**{selected}** · minimum "
                f"**{format_power(effective_floor) if effective_floor else '(none)'}**"
            )
        if session.show_below_floor:
            lines.append("👁️ Members below minimum visible in the picker.")
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
            f"• Members slotted via a band-relaxed minimum: **{af['power_band_rules_applied']}**"
        )
        lines.append(
            f"• Auto-filled by power: **{af['auto_filled_by_power']}**"
        )
        # Decision #14 (#171): auto-paired subs render explicitly so
        # officers can see who got paired with whom — pairing is the
        # highest-edit candidate, so visibility matters more than
        # brevity. The bare count surfaces nothing actionable.
        paired = af.get("auto_paired_subs") or []
        if paired:
            lines.append(
                f"• Auto-paired subs ({len(paired)}): {', '.join(paired)}"
            )
        # Decision #8 (#171): no truncation. Officers need every gap +
        # every conflict listed so they can make slotting decisions
        # manually — `(+N more)` hid exactly the entries they needed.
        if af["gaps"]:
            lines.append(
                f"• Gaps (power unknown, not slotted): **{len(af['gaps'])}**: "
                f"{', '.join(af['gaps'])}"
            )
        if af["conflicts"]:
            lines.append(
                f"• Conflicts: **{len(af['conflicts'])}**: "
                f"{'; '.join(af['conflicts'])}"
            )
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
    # Per-phase eligibility (#152): a member sitting in a Phase 1 slot
    # is still pickable for Phase 2 (the migration case), so we only
    # exclude members already in THIS phase's slots. Falls back to the
    # union for flat presets via assigned_member_keys_in_phase(1).
    assigned = session.assigned_member_keys_in_phase(session.selected_phase)
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

        # Row layout adapts to phase-aware (#152). Phase-aware presets
        # reserve row 0 for the Phase 1 / Phase 2 nav buttons, pushing
        # every other component down by one row. Flat presets keep the
        # original 4-row layout (zone, member, actions, finalisation).
        zone_row = 1 if s.is_phase_aware else 0
        member_row = 2 if s.is_phase_aware else 1
        action_row = 3 if s.is_phase_aware else 2
        final_row = 4 if s.is_phase_aware else 3

        # Row 0 — Phase navigation (phase-aware presets only). Walks
        # `iter_phases()` so 3-phase presets get a Phase 3 button.
        # Hardcoding `(1, 2)` left CS officers unable to edit Phase 3
        # manually even though auto-fill placed members there.
        if s.is_phase_aware:
            for phase in s.iter_phases():
                btn = discord.ui.Button(
                    label=f"Stage {phase}"
                          + (" •" if phase == s.selected_phase else ""),
                    style=(discord.ButtonStyle.primary
                           if phase == s.selected_phase
                           else discord.ButtonStyle.secondary),
                    row=0,
                )

                def _make_callback(p):
                    async def _on_phase(inter: discord.Interaction):
                        if not await self._guard_owner(inter):
                            return
                        s.selected_phase = p
                        await self._refresh(inter)
                    return _on_phase

                btn.callback = _make_callback(phase)
                self.add_item(btn)

        # Row N — zone selector. Label shows the SELECTED phase's
        # capacity (e.g. "Info Center (2/4)") on phase-aware presets so
        # the dropdown stays scannable; the embed line still shows both
        # phases' counts for the broader view.
        if s.preset.zones:
            def _zone_option_label(z):
                count = s.zone_member_count(z.zone)
                cap = s.zone_capacity(z.zone)
                if s.is_phase_aware:
                    return f"S{s.selected_phase}: {z.zone} ({count}/{cap})"[:100]
                return f"{z.zone} ({count}/{cap})"[:100]
            zone_options = [
                discord.SelectOption(
                    label=_zone_option_label(z),
                    value=z.zone[:100],
                    default=(z.zone == s.selected_zone),
                )
                for z in s.preset.zones[:_MAX_DROPDOWN_OPTIONS]
            ]
            zone_select = discord.ui.Select(
                placeholder="Pick a zone to edit…",
                min_values=1, max_values=1,
                options=zone_options,
                row=zone_row,
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
                description = "below minimum" if is_below else None
                options.append(discord.SelectOption(
                    label=label, value=value, description=description,
                ))
            placeholder = (
                f"Pick a member for {s.selected_zone or 'a zone'}…"
                if eligible or s.show_below_floor else
                "No eligible members. Toggle below-minimum override"
            )
            # Surface overflow so the officer knows the dropdown is
            # truncated — Discord caps Select options at 25 and a
            # silent drop hides the rest of the eligible pool.
            overflow = len(pool) - len(options)
            if overflow > 0:
                placeholder = f"{placeholder} (+{overflow} more)"
            member_select = discord.ui.Select(
                placeholder=placeholder[:150],
                min_values=1, max_values=1,
                options=options,
                row=member_row,
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
                        "⚠️ Toggle the below-minimum override to assign this member.",
                        ephemeral=True,
                    )
                    return
                # Record the override for the audit trail — anyone in
                # `below` at assign time was assigned despite being
                # below the effective floor (or having unknown power).
                # Phase-aware (#152): both the assignment and the
                # override flag write into the currently selected
                # phase's dicts, so a member added to Phase 2 doesn't
                # show up on Phase 1's audit trail.
                if key in below:
                    s.below_floor_overrides_for_phase(s.selected_phase).add(key)
                s.assignments_for_phase(s.selected_phase)[s.selected_zone].append(key)
                # Any manual edit invalidates the auto-fill summary —
                # the names and counts the officer is reading no longer
                # describe what's currently on the roster.
                s.auto_fill_summary = None
                # In paired mode (#168), pairings are explicit — the
                # officer pairs primaries with subs via the 🔁 Pair subs
                # button, not an auto-prompt after every primary
                # assignment. This matches the spec that some primaries
                # won't get a sub (e.g. 10 subs, 20 primaries) so a
                # forced prompt after every primary was always wrong.
                await self._refresh(inter)

            member_select.callback = _on_member
            self.add_item(member_select)

        # Row 2 — action buttons
        toggle_label = (
            "👁️ Hide members below minimum" if s.show_below_floor
            else "👁️ Show members below minimum"
        )
        toggle_btn = discord.ui.Button(
            label=toggle_label, style=discord.ButtonStyle.secondary, row=action_row,
        )

        async def _toggle(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            s.show_below_floor = not s.show_below_floor
            await self._refresh(inter)

        toggle_btn.callback = _toggle
        self.add_item(toggle_btn)

        unassign_btn = discord.ui.Button(
            label="↩️ Remove current zone assignees", style=discord.ButtonStyle.secondary, row=action_row,
        )

        async def _unassign(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            if not s.selected_zone:
                await inter.response.send_message("⚠️ Pick a zone first.", ephemeral=True)
                return
            # Phase-aware (#152): only the selected phase's assignment
            # is cleared. The other phase's slots stay intact so an
            # officer can refine one phase without nuking the other.
            s.assignments_for_phase(s.selected_phase)[s.selected_zone] = []
            s.prune_stale_overrides()
            s.prune_stale_pairings()
            s.auto_fill_summary = None
            await self._refresh(inter)

        unassign_btn.callback = _unassign
        self.add_item(unassign_btn)

        move_to_subs_btn = discord.ui.Button(
            label="🪑 Add all unassigned to Subs", style=discord.ButtonStyle.secondary, row=action_row,
        )

        async def _move_to_subs(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            # Bulk move: every member in this team's available pool who
            # isn't already a primary in any phase + isn't already in
            # subs (or paired with a primary, in paired mode) goes to
            # the subs pool. Subs have no minimum power filter — this
            # is a pure pool transfer, not an eligibility check.
            primaries: set[str] = set()
            for phase in s.iter_phases():
                for zone_members in s.assignments_for_phase(phase).values():
                    primaries.update(zone_members)
            already_subs = set(s.subs)
            paired_keys = set(s.paired_subs.values()) if s.is_paired else set()
            to_move = [
                key for key in s.members.keys()
                if key not in primaries
                and key not in already_subs
                and key not in paired_keys
            ]
            if not to_move:
                await inter.response.send_message(
                    "⚠️ No unassigned members to move. Everyone in this "
                    "team's pool is already assigned as a primary or sub.",
                    ephemeral=True,
                )
                return
            s.subs.extend(to_move)
            s.prune_stale_overrides()
            s.prune_stale_pairings()
            s.auto_fill_summary = None
            await self._refresh(inter)

        move_to_subs_btn.callback = _move_to_subs
        self.add_item(move_to_subs_btn)

        # Pair subs button (paired mode only). Opens an ephemeral view
        # with a running pair list + Primary Select + Sub Select +
        # Assign + Done. Picking a pair writes immediately; the unpair
        # affordance is on the same view so officers can fix mistakes
        # without flipping screens. Replaces the per-primary auto-prompt
        # + separate Re-pair flow that #168 retired.
        if s.is_paired:
            pair_btn = discord.ui.Button(
                label="🔁 Pair subs", style=discord.ButtonStyle.secondary, row=action_row,
            )

            async def _pair_subs(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                await _open_pair_subs_view(inter, self)

            pair_btn.callback = _pair_subs
            self.add_item(pair_btn)

        # Row 3 — finalisation. Structured-mode adds an Approve & Post
        # button that fires the rosters_tab write + auto-post; free
        # tier gets Generate-mail-only (officer copies manually).
        if s.is_structured:
            auto_fill_btn = discord.ui.Button(
                label="🎯 Auto-fill",
                style=discord.ButtonStyle.primary, row=action_row,
            )

            async def _run_auto_fill(inter: discord.Interaction):
                """Actually execute the auto-fill + refresh. Pulled out
                so both the direct first-click path AND the
                post-confirm path can call it through the same code."""
                # Wrap the algorithm in explicit logging so when team-
                # test reports "auto-fill skipped after the first
                # power-unknown" we have a trail in Railway logs.
                logger.info(
                    "[STORM AUTO-FILL] start: guild=%s event=%s team=%s "
                    "preset=%s pool_size=%d (members=%s)",
                    s.guild_id, s.event_type, s.team,
                    s.preset.name, len(s.members),
                    [
                        {"key": k, "name": m.get("name"),
                         "discord_id": m.get("discord_id"),
                         "power": m.get("power"),
                         "not_on_discord": m.get("not_on_discord")}
                        for k, m in list(s.members.items())[:20]
                    ],
                )
                try:
                    summary = _auto_fill_session(s)
                except Exception as e:
                    logger.exception(
                        "[STORM AUTO-FILL] crashed for guild=%s event=%s: %s",
                        s.guild_id, s.event_type, e,
                    )
                    await inter.response.send_message(
                        f"⚠️ Auto-fill hit an unexpected error: `{type(e).__name__}: {str(e)[:120]}`. "
                        f"Please share this message with the bot maintainer; logs have details.",
                        ephemeral=True,
                    )
                    return
                logger.info(
                    "[STORM AUTO-FILL] done: guild=%s event=%s summary=%s "
                    "assignments=%s subs=%d paired=%d",
                    s.guild_id, s.event_type, summary,
                    {z: names for z, names in s.assignments.items() if names},
                    len(s.subs), len(s.paired_subs),
                )
                await self._refresh(inter)

            async def _auto_fill(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                # Decision #9 (#171): destructive re-runs prompt for
                # confirmation. A fresh session (no assignments, subs,
                # or pairings) skips the prompt and runs straight away.
                if not s.has_existing_assignments():
                    await _run_auto_fill(inter)
                    return
                confirm_view = _AutoFillConfirmView(parent_view=self)
                await inter.response.send_message(
                    "⚠️ **Re-run auto-fill?** This will reset every "
                    "assignment, sub pairing, and override on this team. "
                    "Manual edits you've made since the last auto-fill "
                    "will be lost.",
                    view=confirm_view, ephemeral=True,
                )
                try:
                    confirm_view.message = await inter.original_response()
                except discord.HTTPException:
                    confirm_view.message = None

            auto_fill_btn.callback = _auto_fill
            self.add_item(auto_fill_btn)

            approve_btn = discord.ui.Button(
                label="✅ Approve & Post",
                style=discord.ButtonStyle.success, row=final_row,
            )

            async def _approve(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                await _finalize_structured_roster(inter, self)

            approve_btn.callback = _approve
            self.add_item(approve_btn)

            preview_btn = discord.ui.Button(
                label="📄 Preview mail", style=discord.ButtonStyle.secondary, row=final_row,
            )

            async def _preview(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                await _send_mail_preview(inter, s)

            preview_btn.callback = _preview
            self.add_item(preview_btn)
        else:
            mail_btn = discord.ui.Button(
                label="📄 Generate mail", style=discord.ButtonStyle.primary, row=final_row,
            )

            async def _gen_mail(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                await _send_mail_preview(inter, s)

            mail_btn.callback = _gen_mail
            self.add_item(mail_btn)

            save_preset_btn = discord.ui.Button(
                label="💾 Save as preset", style=discord.ButtonStyle.success, row=final_row,
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
        render_label = (
            "🖼️ Generate DS assignments image"
            if s.event_type == "DS"
            else "🖼️ Generate CS assignments image"
        )
        render_btn = discord.ui.Button(
            label=render_label, style=discord.ButtonStyle.secondary, row=final_row,
        )

        async def _render(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            await _render_and_attach(inter, s)

        render_btn.callback = _render
        self.add_item(render_btn)

        cancel_label = "❌ Cancel" if s.is_structured else "✅ Done"
        done_btn = discord.ui.Button(
            label=cancel_label, style=discord.ButtonStyle.danger, row=final_row,
        )

        async def _done(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            for item in self.children:
                item.disabled = True
            await inter.response.edit_message(
                content=("Roster builder cancelled. Nothing posted."
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


def _zone_of_primary(session: RosterBuilderSession, primary_key: str) -> str:
    """Return the zone a primary is currently assigned to, or the
    session's selected_zone as a fallback. The re-pair flow needs
    the primary's actual zone (not whatever the officer last
    clicked on) so the sub-eligibility check enforces the right floor.

    Searches the selected phase first (so a primary assigned in both
    P1 and P2 returns the phase the officer is currently editing),
    then falls back to walking every other phase — without that walk
    the lookup misses primaries assigned only to Phase 2 or Phase 3
    and degrades to `selected_zone`, applying the wrong eligibility
    floor to the paired sub."""
    selected_phase = session.selected_phase
    selected_zones = session.assignments_for_phase(selected_phase)
    for zone_name, keys in selected_zones.items():
        if primary_key in keys:
            return zone_name
    for phase in session.iter_phases():
        if phase == selected_phase:
            continue
        for zone_name, keys in session.assignments_for_phase(phase).items():
            if primary_key in keys:
                return zone_name
    return session.selected_zone or ""


class _AutoFillConfirmView(discord.ui.View):
    """Confirm/cancel prompt for re-running auto-fill on a session that
    already has manual edits (#171 / Decision #9).

    First-click auto-fill on a fresh session skips this view entirely.
    Confirming runs auto-fill on the parent session and refreshes the
    parent builder view via its captured message handle (the ephemeral
    confirm interaction has already consumed its own response slot, so
    the main view can't be edited through this interaction).
    """

    def __init__(self, *, parent_view: "RosterBuilderView"):
        super().__init__(timeout=120)
        self.parent_view = parent_view
        self.message: Optional[discord.Message] = None

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.parent_view.session.user_id:
            await inter.response.send_message(
                "⛔ Only the builder's owner can confirm.", ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="🎯 Re-run auto-fill",
                       style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction,
                      _btn: discord.ui.Button):
        if not await self._guard_owner(inter):
            return
        if self.is_finished():
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        s = self.parent_view.session
        try:
            summary = _auto_fill_session(s)
        except Exception as e:
            logger.exception(
                "[STORM AUTO-FILL] confirm-path crashed for guild=%s event=%s: %s",
                s.guild_id, s.event_type, e,
            )
            try:
                await inter.response.edit_message(
                    content=(
                        f"⚠️ Auto-fill hit an unexpected error: "
                        f"`{type(e).__name__}: {str(e)[:120]}`. Logs have "
                        f"details."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass
            return
        logger.info(
            "[STORM AUTO-FILL] confirm-path done: guild=%s event=%s summary=%s",
            s.guild_id, s.event_type, summary,
        )
        try:
            await inter.response.edit_message(
                content="🎯 Auto-fill re-run complete. Main view refreshed.",
                view=self,
            )
        except discord.HTTPException:
            pass
        # Refresh the main builder view via its captured message handle,
        # not through this interaction — the ephemeral confirm and the
        # main builder live on separate messages.
        try:
            if self.parent_view.message is not None:
                self.parent_view._rebuild()
                await self.parent_view.message.edit(
                    embed=_render_builder_embed(s),
                    view=self.parent_view,
                )
        except discord.HTTPException:
            pass

    @discord.ui.button(label="↩️ Cancel",
                       style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction,
                     _btn: discord.ui.Button):
        if not await self._guard_owner(inter):
            return
        if self.is_finished():
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(
                content="↩️ Auto-fill cancelled. Your edits are intact.",
                view=self,
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def _open_pair_subs_view(
    interaction: discord.Interaction,
    main_view: "RosterBuilderView",
) -> None:
    """Open the combined Pair-Subs ephemeral view (#168).

    Replaces the old auto-fire-after-each-primary picker (`_PairedSubPickerView`)
    and the separate Re-pair flow (`_RepairPrimaryPickerView`) with a single
    persistent view: running pair list at top, Primary Select + Sub Select +
    Assign + Unpair + Done buttons below. Subs that exceed available primaries
    (or vice versa) are expected — the view's body copy spells out the ratio.
    """
    s = main_view.session
    if not s.is_paired:
        try:
            await interaction.response.send_message(
                "⚠️ This view is only available in paired-sub mode.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        return

    view = _PairSubsView(main_view=main_view)
    try:
        await interaction.response.send_message(
            content=view.render_content(), view=view, ephemeral=True,
        )
        view.message = await interaction.original_response()
    except discord.HTTPException as e:
        logger.warning(
            "[STORM BUILDER] pair-subs view failed to send (guild=%s): %s",
            s.guild_id, e,
        )


class _PairSubsView(discord.ui.View):
    """Combined Primary + Sub picker with a running pair list.

    Renders the message content as the running pair list (one row per
    pairing, blank when no pairings exist yet); the embed/view body
    carries the two Selects + action buttons. Pairings are written
    immediately on Assign — there's no separate Save step. The Unpair
    button opens a single-Select dropdown of currently-paired primaries
    and clears the chosen pair on submit.

    Phase-aware (#152): operates on `session.selected_phase`. The
    primary's phase comes from `assignments_for_phase`; the sub binds
    to the same phase via `paired_subs_for_phase`.
    """

    def __init__(self, *, main_view: "RosterBuilderView"):
        super().__init__(timeout=600)
        self.main_view = main_view
        self.message: Optional[discord.Message] = None
        self.selected_primary: str | None = None
        self.selected_sub: str | None = None
        # Toggle: when True, the view shows an Unpair-select instead of
        # the Primary-select so the officer can pick which pair to drop.
        self.unpair_mode = False
        self.selected_unpair_primary: str | None = None
        self._build_components()

    # ── Data helpers ─────────────────────────────────────────────────
    def _phase_assignments(self) -> dict[str, list[str]]:
        s = self.main_view.session
        return s.assignments_for_phase(s.selected_phase)

    def _phase_pairings(self) -> dict[str, str]:
        s = self.main_view.session
        return s.paired_subs_for_phase(s.selected_phase)

    def _unpaired_primaries(self) -> list[tuple[str, str]]:
        """[(primary_key, zone_name), …] of primaries without a paired
        sub in the selected phase, sorted by zone-then-roster order."""
        pairings = self._phase_pairings()
        out: list[tuple[str, str]] = []
        for z in self.main_view.session.preset.zones:
            for key in self._phase_assignments().get(z.zone, []):
                if key not in pairings:
                    out.append((key, z.zone))
        return out

    def _available_subs(self) -> list[str]:
        """Members in the team's subs pool not yet paired with a
        primary in the selected phase. Mirrors the spec's "hides already-
        paired subs" rule."""
        paired_sub_keys = set(self._phase_pairings().values())
        s = self.main_view.session
        return [k for k in s.subs if k not in paired_sub_keys]

    def _pair_rows(self) -> list[tuple[str, str, str]]:
        """[(primary_name, sub_name, zone), …] for the running pair
        list, in zone-then-roster order so the rendering is deterministic."""
        s = self.main_view.session
        pairings = self._phase_pairings()
        out: list[tuple[str, str, str]] = []
        for z in s.preset.zones:
            for primary_key in self._phase_assignments().get(z.zone, []):
                sub_key = pairings.get(primary_key)
                if not sub_key:
                    continue
                p_m = s.members.get(primary_key)
                s_m = s.members.get(sub_key)
                p_name = p_m["name"] if p_m else primary_key
                s_name = s_m["name"] if s_m else sub_key
                out.append((p_name, s_name, z.zone))
        return out

    # ── Rendering ────────────────────────────────────────────────────
    def render_content(self) -> str:
        s = self.main_view.session
        unpaired = self._unpaired_primaries()
        primary_count = len(self._phase_assignments_flat())
        sub_pool = len(s.subs)
        header = (
            f"🔁 **Pair subs**: Stage {s.selected_phase}\n"
            f"You have **{sub_pool} sub{'s' if sub_pool != 1 else ''}** "
            f"and **{primary_count} primar{'ies' if primary_count != 1 else 'y'}**. "
            f"not every primary will get a sub."
        )
        pair_rows = self._pair_rows()
        if pair_rows:
            pair_block = "\n".join(
                f"• **{p}** → **{s_name}**  _({zone})_"
                for p, s_name, zone in pair_rows
            )
            pairs_line = f"\n\n**Current pairings ({len(pair_rows)}):**\n{pair_block}"
        else:
            pairs_line = "\n\n_No pairings yet._"
        if unpaired:
            unpaired_block = ", ".join(
                self.main_view.session.members[k]["name"]
                for k, _zone in unpaired
                if k in self.main_view.session.members
            )
            unpaired_line = (
                f"\n\n⚠️ **Unpaired primaries ({len(unpaired)}):** {unpaired_block}"
            )
        else:
            unpaired_line = ""
        return header + pairs_line + unpaired_line

    def _phase_assignments_flat(self) -> list[str]:
        out: list[str] = []
        for keys in self._phase_assignments().values():
            out.extend(keys)
        return out

    # ── Component layout ─────────────────────────────────────────────
    def _build_components(self):
        self.clear_items()
        if self.unpair_mode:
            self._build_unpair_components()
        else:
            self._build_assign_components()

    def _build_assign_components(self):
        unpaired = self._unpaired_primaries()
        available_subs = self._available_subs()
        s = self.main_view.session

        if unpaired:
            options = []
            for primary_key, zone in unpaired[:_MAX_DROPDOWN_OPTIONS]:
                m = s.members.get(primary_key)
                name = m["name"] if m else primary_key
                options.append(discord.SelectOption(
                    label=name[:100],
                    value=primary_key[:100],
                    description=zone[:100],
                    default=(primary_key == self.selected_primary),
                ))
            primary_select = discord.ui.Select(
                placeholder="Pick an unpaired primary…",
                min_values=1, max_values=1, options=options, row=0,
            )

            async def _on_primary(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                self.selected_primary = primary_select.values[0]
                self._build_components()
                try:
                    await inter.response.edit_message(
                        content=self.render_content(), view=self,
                    )
                except discord.HTTPException:
                    pass

            primary_select.callback = _on_primary
            self.add_item(primary_select)

        if available_subs:
            options = []
            for sub_key in available_subs[:_MAX_DROPDOWN_OPTIONS]:
                m = s.members.get(sub_key)
                if not m:
                    continue
                options.append(discord.SelectOption(
                    label=_format_member_label(m)[:100],
                    value=sub_key[:100],
                    default=(sub_key == self.selected_sub),
                ))
            if options:
                sub_select = discord.ui.Select(
                    placeholder="Pick a sub…",
                    min_values=1, max_values=1, options=options, row=1,
                )

                async def _on_sub(inter: discord.Interaction):
                    if not await self._guard_owner(inter):
                        return
                    self.selected_sub = sub_select.values[0]
                    self._build_components()
                    try:
                        await inter.response.edit_message(
                            content=self.render_content(), view=self,
                        )
                    except discord.HTTPException:
                        pass

                sub_select.callback = _on_sub
                self.add_item(sub_select)

        assign_btn = discord.ui.Button(
            label="✅ Assign pair", style=discord.ButtonStyle.primary, row=2,
            disabled=not (self.selected_primary and self.selected_sub),
        )
        assign_btn.callback = self._on_assign
        self.add_item(assign_btn)

        unpair_btn = discord.ui.Button(
            label="🔄 Unpair…", style=discord.ButtonStyle.secondary, row=2,
            disabled=not bool(self._phase_pairings()),
        )
        unpair_btn.callback = self._enter_unpair_mode
        self.add_item(unpair_btn)

        done_btn = discord.ui.Button(
            label="✔ Done", style=discord.ButtonStyle.secondary, row=2,
        )
        done_btn.callback = self._on_done
        self.add_item(done_btn)

    def _build_unpair_components(self):
        s = self.main_view.session
        pairings = self._phase_pairings()
        # Build [(primary_key, primary_name, sub_name, zone), …] in the
        # same zone-then-roster order as the running pair list.
        rows: list[tuple[str, str, str, str]] = []
        for z in s.preset.zones:
            for primary_key in self._phase_assignments().get(z.zone, []):
                sub_key = pairings.get(primary_key)
                if not sub_key:
                    continue
                p_m = s.members.get(primary_key)
                s_m = s.members.get(sub_key)
                p_name = p_m["name"] if p_m else primary_key
                s_name = s_m["name"] if s_m else sub_key
                rows.append((primary_key, p_name, s_name, z.zone))

        if rows:
            options = [
                discord.SelectOption(
                    label=f"{p_name} → {s_name}"[:100],
                    value=p_key[:100],
                    description=zone[:100],
                    default=(p_key == self.selected_unpair_primary),
                )
                for p_key, p_name, s_name, zone in rows[:_MAX_DROPDOWN_OPTIONS]
            ]
            unpair_select = discord.ui.Select(
                placeholder="Pick a pair to unpair…",
                min_values=1, max_values=1, options=options, row=0,
            )

            async def _on_pick(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                self.selected_unpair_primary = unpair_select.values[0]
                self._build_components()
                try:
                    await inter.response.edit_message(
                        content=self.render_content(), view=self,
                    )
                except discord.HTTPException:
                    pass

            unpair_select.callback = _on_pick
            self.add_item(unpair_select)

        confirm_btn = discord.ui.Button(
            label="🔄 Confirm unpair", style=discord.ButtonStyle.danger, row=1,
            disabled=not self.selected_unpair_primary,
        )
        confirm_btn.callback = self._on_confirm_unpair
        self.add_item(confirm_btn)

        back_btn = discord.ui.Button(
            label="↩️ Back", style=discord.ButtonStyle.secondary, row=1,
        )
        back_btn.callback = self._exit_unpair_mode
        self.add_item(back_btn)

    # ── Callbacks ────────────────────────────────────────────────────
    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.main_view.session.user_id:
            await inter.response.send_message(
                "⛔ Only the builder's owner can pair subs.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_assign(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        if not (self.selected_primary and self.selected_sub):
            await inter.response.send_message(
                "⚠️ Pick a primary and a sub before assigning.",
                ephemeral=True,
            )
            return
        s = self.main_view.session
        phase = s.selected_phase
        s.paired_subs_for_phase(phase)[self.selected_primary] = self.selected_sub
        # Below-floor capture: subs that are below the primary's zone
        # floor still pair, but the rosters_tab write should mark the
        # slot as an override. Find the zone the primary is in.
        primary_zone = _zone_of_primary(s, self.selected_primary)
        if primary_zone:
            _eligible, below = _eligible_member_keys_for_zone(s, primary_zone)
            if self.selected_sub in below:
                s.below_floor_overrides_for_phase(phase).add(self.selected_sub)
        s.auto_fill_summary = None

        self.selected_primary = None
        self.selected_sub = None
        self._build_components()
        try:
            await inter.response.edit_message(
                content=self.render_content(), view=self,
            )
        except discord.HTTPException:
            pass
        # Re-render the main view so the new pairing is visible there too.
        try:
            if self.main_view.message:
                self.main_view._rebuild()
                await self.main_view.message.edit(
                    embed=_render_builder_embed(s), view=self.main_view,
                )
        except discord.HTTPException:
            pass

    async def _enter_unpair_mode(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        self.unpair_mode = True
        self.selected_unpair_primary = None
        self._build_components()
        try:
            await inter.response.edit_message(
                content=self.render_content(), view=self,
            )
        except discord.HTTPException:
            pass

    async def _exit_unpair_mode(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        self.unpair_mode = False
        self.selected_unpair_primary = None
        self._build_components()
        try:
            await inter.response.edit_message(
                content=self.render_content(), view=self,
            )
        except discord.HTTPException:
            pass

    async def _on_confirm_unpair(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        if not self.selected_unpair_primary:
            await inter.response.send_message(
                "⚠️ Pick a pair to unpair.", ephemeral=True,
            )
            return
        s = self.main_view.session
        phase = s.selected_phase
        s.paired_subs_for_phase(phase).pop(self.selected_unpair_primary, None)
        s.auto_fill_summary = None
        self.selected_unpair_primary = None
        self.unpair_mode = False
        self._build_components()
        try:
            await inter.response.edit_message(
                content=self.render_content(), view=self,
            )
        except discord.HTTPException:
            pass
        try:
            if self.main_view.message:
                self.main_view._rebuild()
                await self.main_view.message.edit(
                    embed=_render_builder_embed(s), view=self.main_view,
                )
        except discord.HTTPException:
            pass

    async def _on_done(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        for item in self.children:
            item.disabled = True
        self.stop()
        try:
            await inter.response.edit_message(view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
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
        # Preserves the session's phase shape — without phase_count +
        # per-phase capacities, saving a phase-aware roster as a preset
        # would silently strip it to flat and lose the phase-migration
        # data the officer just built.
        import storm_strategy as ss
        new_zones = []
        for z in s.preset.zones:
            # For each phase, prefer the live count if the officer
            # filled any slots there, otherwise inherit the preset's
            # capacity so re-applying produces the same shape.
            def _cap_for(phase: int) -> int:
                cur = s.zone_member_count(z.zone, phase=phase)
                if cur > 0:
                    return cur
                return int(z.max_for_phase(phase) or 0)

            flat_count = s.zone_member_count(z.zone, phase=1)
            new_zones.append(ss.ZoneRow(
                zone=z.zone,
                max_players=(
                    flat_count if flat_count > 0 else int(z.max_players)
                ),
                max_phase1=_cap_for(1),
                max_phase2=_cap_for(2),
                max_phase3=_cap_for(3),
                min_power_a=int(z.min_power_a or 0),
                min_power_b=int(z.min_power_b or 0),
                priority=int(z.priority or 0),
                priority_phase1=int(z.priority_phase1 or 0),
                priority_phase2=int(z.priority_phase2 or 0),
                priority_phase3=int(z.priority_phase3 or 0),
            ))
        buf = ss.PresetBuffer(
            name=name, event_type=s.event_type, zones=new_zones,
            faction=s.preset.faction,
            phase_count=s.preset.phase_count,
        )
        ok = await asyncio.to_thread(
            ss.save_preset, s.guild_id, s.event_type, buf,
        )
        if ok:
            await inter.response.send_message(
                f"✅ Saved roster as preset **{name}**.", ephemeral=True,
            )
        else:
            await inter.response.send_message(
                "⚠️ Couldn't save preset. Check that your Sheet is configured "
                "and the bot has edit access.",
                ephemeral=True,
            )


# ── Mail generation ──────────────────────────────────────────────────────────


_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024


async def _render_and_attach(
    inter: discord.Interaction, session: RosterBuilderSession,
) -> None:
    """Render the current roster as a PNG, post it publicly in the
    invoking channel so other leaders can see + reference it, and
    follow up with an ephemeral action bar (Download / Save to history
    / Post to channel) that only the clicking officer sees.

    Pillow import lives inside the handler so the builder module
    doesn't pay the import cost unless render is actually invoked.
    The CPU-bound Pillow encode runs in a thread executor so a 30-slot
    PNG doesn't blow the 3-second interaction token or stall the
    gateway heartbeat for other guilds.

    Failure modes:
      * Pillow not installed → renderer raises `RuntimeError`; surface
        a one-line ephemeral. Officer continues with text-only mail.
      * Other Pillow error → log + ephemeral "could not render."
      * Bot can't post in the channel → log + ephemeral with the
        recovery hint ("check channel permissions").
    """
    import asyncio

    # Defer first so the encode + upload have time. `thinking=True`
    # because we're about to post a public message + ephemeral action
    # bar — the spinner is the right idle UI.
    try:
        await inter.response.defer(ephemeral=True, thinking=True)
    except discord.HTTPException as e:
        logger.warning(
            "[STORM RENDER] defer failed (guild=%s): %s",
            session.guild_id, e,
        )
        return

    try:
        import storm_renderer
        roster_data = storm_renderer.roster_from_session(session)
        png_bytes = await asyncio.to_thread(storm_renderer.render, roster_data)
    except RuntimeError as e:
        logger.warning(
            "[STORM RENDER] Pillow not available (guild=%s event=%s): %s",
            session.guild_id, session.event_type, e,
        )
        await inter.followup.send(
            "⚠️ Image render isn't available. The host is missing Pillow. "
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
            "⚠️ Couldn't render the roster image. See bot logs.",
            ephemeral=True,
        )
        return

    if len(png_bytes) > _MAX_ATTACHMENT_BYTES:
        logger.warning(
            "[STORM RENDER] PNG exceeded 25MB (size=%d guild=%s event=%s)",
            len(png_bytes), session.guild_id, session.event_type,
        )
        await inter.followup.send(
            "⚠️ Rendered roster image is too large to attach "
            f"({len(png_bytes) // (1024 * 1024)} MB > 25 MB Discord limit). "
            "Use the text-template mail instead.",
            ephemeral=True,
        )
        return

    filename = (
        f"{session.event_type.lower()}-roster"
        + (f"-{session.event_date}" if session.event_date else "")
        + (f"-team-{session.team}" if session.team else "")
        + ".png"
    )

    # Public post in the invoking channel. Other leaders in the
    # channel can see + save the image directly; Discord hosts it
    # durably for as long as the message exists.
    public_msg: Optional[discord.Message] = None
    if inter.channel is not None:
        try:
            public_msg = await inter.channel.send(
                file=discord.File(io.BytesIO(png_bytes), filename=filename),
            )
        except discord.Forbidden:
            logger.warning(
                "[STORM RENDER] no perms to post in channel=%s guild=%s",
                getattr(inter.channel, "id", None), session.guild_id,
            )
            # Fall through to ephemeral-only delivery so the officer
            # still gets the image — they just don't get the public copy.
        except discord.HTTPException as e:
            logger.warning(
                "[STORM RENDER] failed to post public image (guild=%s): %s",
                session.guild_id, e,
            )

    if public_msg is None:
        # No public post → simpler ephemeral delivery, no action bar
        # (the action bar's value is acting on a public message).
        await inter.followup.send(
            content=(
                "🖼️ Roster image attached (couldn't post publicly; check "
                "the bot's permissions in this channel):"
            ),
            file=discord.File(io.BytesIO(png_bytes), filename=filename),
            ephemeral=True,
        )
        return

    # Ephemeral action bar — only the clicking officer sees it.
    view = _RenderActionView(
        owner_id=inter.user.id,
        png_bytes=png_bytes,
        filename=filename,
        guild_id=session.guild_id,
        event_type=session.event_type,
        event_date=session.event_date or "",
        team=session.team or "",
        public_channel_id=public_msg.channel.id,
        public_message_id=public_msg.id,
    )
    await inter.followup.send(
        content=(
            f"🖼️ Roster image posted above. Pick an action below; only "
            f"you'll see this prompt."
        ),
        view=view,
        ephemeral=True,
    )


class _RenderActionView(discord.ui.View):
    """Three-button ephemeral action bar shown after a public roster
    image is posted. Each button operates on the same `png_bytes`
    snapshot captured at render time so subsequent actions reflect the
    image the officer just saw — not whatever the session looks like
    seconds later if they keep tweaking."""

    def __init__(
        self, *, owner_id: int, png_bytes: bytes, filename: str,
        guild_id: int, event_type: str, event_date: str, team: str,
        public_channel_id: int, public_message_id: int,
    ):
        super().__init__(timeout=900)  # 15 min — enough for a coffee + caption typing
        self.owner_id = owner_id
        self.png_bytes = png_bytes
        self.filename = filename
        self.guild_id = guild_id
        self.event_type = event_type
        self.event_date = event_date
        self.team = team
        self.public_channel_id = public_channel_id
        self.public_message_id = public_message_id

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message(
                "⛔ These actions are for the officer who rendered the image.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="📥 Download", style=discord.ButtonStyle.secondary)
    async def download_btn(
        self, inter: discord.Interaction, _btn: discord.ui.Button,
    ):
        """DM the image to the officer. DMs have native save-to-device
        UI on every Discord client (right-click on desktop, long-press
        → save to camera roll on mobile), which is more discoverable
        than the channel attachment menu."""
        try:
            dm = await inter.user.create_dm()
            await dm.send(
                content=(
                    f"📥 Here's the roster image you asked to download "
                    f"(from {self.event_type} on {self.event_date or 'today'}). "
                    f"Right-click → Save image, or tap → save on mobile."
                ),
                file=discord.File(io.BytesIO(self.png_bytes), filename=self.filename),
            )
        except discord.Forbidden:
            await inter.response.send_message(
                "⚠️ I can't DM you. Your privacy settings block bot DMs. "
                "Right-click the image in the channel and use Save image instead.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            logger.warning(
                "[STORM RENDER] download DM failed user=%s guild=%s: %s",
                inter.user.id, self.guild_id, e,
            )
            await inter.response.send_message(
                f"⚠️ DM send failed: {e}. Right-click the channel image to save.",
                ephemeral=True,
            )
            return
        await inter.response.send_message(
            "📥 Sent to your DMs. Check your direct messages with the bot.",
            ephemeral=True,
        )

    @discord.ui.button(label="💾 Save to history", style=discord.ButtonStyle.primary)
    async def save_btn(
        self, inter: discord.Interaction, _btn: discord.ui.Button,
    ):
        """Store the (channel, message) pointer so the history browser
        can offer a `📷 View image` button on this event. Image bytes
        live in Discord; we just remember where."""
        import config
        if not self.event_date:
            await inter.response.send_message(
                "⚠️ Can't save to history without an event date. Open the "
                f"roster via `{HUB_COMMAND[self.event_type]}` → "
                f"**{HUB_BTN_VIEW_SIGNUPS}** so the event date is set.",
                ephemeral=True,
            )
            return
        try:
            await asyncio.to_thread(
                config.save_roster_image_ref,
                self.guild_id, self.event_type, self.event_date, self.team,
                self.public_channel_id, self.public_message_id, inter.user.id,
            )
        except Exception as e:
            logger.exception(
                "[STORM RENDER] save_roster_image_ref failed guild=%s: %s",
                self.guild_id, e,
            )
            await inter.response.send_message(
                "⚠️ Couldn't save to history. See bot logs.",
                ephemeral=True,
            )
            return
        await inter.response.send_message(
            f"💾 Saved. The image is now linked from "
            f"`{HUB_COMMAND[self.event_type]}` → **{HUB_BTN_PAST_ROSTERS}** "
            f"for this event date "
            f"(stays available until the original message is deleted).",
            ephemeral=True,
        )

    @discord.ui.button(label="📢 Post to channel...", style=discord.ButtonStyle.secondary)
    async def post_btn(
        self, inter: discord.Interaction, _btn: discord.ui.Button,
    ):
        """Open a channel picker; once picked, prompt for an optional
        caption, then re-post the image into the chosen channel."""
        picker = _PostToChannelPicker(
            owner_id=self.owner_id,
            png_bytes=self.png_bytes,
            filename=self.filename,
            event_type=self.event_type,
            event_date=self.event_date,
        )
        await inter.response.send_message(
            content=(
                "📢 Pick a channel to post this image to. You'll get a "
                "modal to add an optional caption."
            ),
            view=picker,
            ephemeral=True,
        )


class _PostToChannelPicker(discord.ui.View):
    """Ephemeral channel-select view. On selection, opens the caption
    modal; the modal's submit handler actually posts the image."""

    def __init__(
        self, *, owner_id: int, png_bytes: bytes, filename: str,
        event_type: str, event_date: str,
    ):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.png_bytes = png_bytes
        self.filename = filename
        self.event_type = event_type
        self.event_date = event_date

        select = discord.ui.ChannelSelect(
            channel_types=[
                discord.ChannelType.text,
                discord.ChannelType.public_thread,
                discord.ChannelType.private_thread,
                discord.ChannelType.news,
            ],
            placeholder="Channel to post to...",
            min_values=1, max_values=1,
        )

        async def _on_pick(picker_inter: discord.Interaction):
            if picker_inter.user.id != self.owner_id:
                await picker_inter.response.send_message(
                    "⛔ Not for you.", ephemeral=True,
                )
                return
            picked = select.values[0]
            modal = _PostCaptionModal(
                channel_id=picked.id,
                channel_mention=picked.mention,
                png_bytes=self.png_bytes,
                filename=self.filename,
            )
            await picker_inter.response.send_modal(modal)
            # Stop the picker view — modal carries the rest of the flow.
            self.stop()

        select.callback = _on_pick
        self.add_item(select)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message("⛔ Not for you.", ephemeral=True)
            return False
        return True


class _PostCaptionModal(discord.ui.Modal):
    """Optional caption + Post button. On submit, fetches the chosen
    channel and posts the image with the caption as the message body."""

    def __init__(
        self, *, channel_id: int, channel_mention: str,
        png_bytes: bytes, filename: str,
    ):
        super().__init__(title="Post roster image to channel")
        self.channel_id = channel_id
        self.channel_mention = channel_mention
        self.png_bytes = png_bytes
        self.filename = filename
        self.caption = discord.ui.TextInput(
            label="Caption (optional)",
            placeholder="e.g. Saturday's Desert Storm: final assignments",
            required=False,
            max_length=1500,
            style=discord.TextStyle.paragraph,
        )
        self.add_item(self.caption)

    async def on_submit(self, inter: discord.Interaction) -> None:
        channel = inter.guild.get_channel_or_thread(self.channel_id) if inter.guild else None
        if channel is None:
            await inter.response.send_message(
                "⚠️ Couldn't resolve that channel. It may have been "
                "deleted between picker and submit. Try again.",
                ephemeral=True,
            )
            return
        try:
            await channel.send(
                content=self.caption.value or None,
                file=discord.File(io.BytesIO(self.png_bytes), filename=self.filename),
            )
        except discord.Forbidden:
            await inter.response.send_message(
                f"⚠️ I don't have permission to post in {self.channel_mention}. "
                f"Check the channel's permissions and try a different channel.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            logger.warning(
                "[STORM RENDER] post-to-channel failed channel=%s: %s",
                self.channel_id, e,
            )
            await inter.response.send_message(
                f"⚠️ Discord refused the post: {e}.", ephemeral=True,
            )
            return
        await inter.response.send_message(
            f"📢 Posted to {self.channel_mention}.", ephemeral=True,
        )


def _mail_zone_and_sub_lists(
    session: RosterBuilderSession,
    phase: int = 1,
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

    `phase` (#152): 1 or 2. Selects which phase's assignments and
    paired-sub map to render. Flat presets ignore this and always
    return phase-1 data. The global sub pool is event-level (not
    per-phase) and is only attached to phase 1's return — phase 2
    returns an empty sub list to avoid duplicating subs across blocks.
    """
    zones_for_mail: dict[str, list[str]] = {}
    is_paired = (session.sub_mode == "paired")
    assignments = session.assignments_for_phase(phase)
    pairings = session.paired_subs_for_phase(phase)
    for zone_name, keys in assignments.items():
        if not keys:
            continue
        names: list[str] = []
        for k in keys:
            m = session.members.get(k)
            if m is None:
                continue
            label = m["name"]
            if is_paired:
                sub_key = pairings.get(k)
                if sub_key is not None:
                    sub_m = session.members.get(sub_key)
                    if sub_m is not None:
                        label = f"{label} + sub {sub_m['name']}"
            names.append(label)
        if names:
            zones_for_mail[zone_name] = names

    # Overflow / pool subs render in the global sub block. In paired
    # mode these are the unmatched leftovers (`session.subs`), distinct
    # from the inline paired subs above. Subs are event-level so they
    # only attach to the phase-1 return; phase 2 callers get an empty
    # list and don't double-render them.
    if phase == 1:
        sub_names = [
            session.members[k]["name"]
            for k in session.subs
            if k in session.members
        ]
    else:
        sub_names = []
    return zones_for_mail, sub_names


def _build_mail_for_phase(
    session: RosterBuilderSession, phase: int,
) -> str:
    """Build the mail body for a single phase, delegating to the
    event-specific storm.build_*_mail. Phase 2 callers pass an empty
    sub list so the global sub block only renders once (with phase 1)."""
    import storm
    zones_for_mail, sub_names = _mail_zone_and_sub_lists(session, phase=phase)
    if session.event_type == "DS":
        return storm.build_ds_mail(
            team=session.team or "A",
            zones=zones_for_mail,
            subs=sub_names,
            time_key="1",
            guild_id=session.guild_id,
        )
    # CS mail builder doesn't take subs as a separate arg — they're
    # part of the zones dict under CS_SUBS_KEY.
    cs_zones = dict(zones_for_mail)
    if sub_names:
        try:
            cs_zones[storm.CS_SUBS_KEY] = sub_names
        except AttributeError:
            pass
    return storm.build_cs_mail(
        team=session.team or "A",
        z=cs_zones,
        time_key="1",
        guild_id=session.guild_id,
    )


def _build_mail_body(session: RosterBuilderSession) -> str:
    """Top-level mail builder. Flat presets emit one block. Phase-aware
    presets (#152) emit one block per phase separated by `Phase N`
    headers so leadership can copy-paste the full event into one mail.

    Walks `session.iter_phases()` so a 2-phase preset emits Phase 1 + 2
    and a 3-phase preset emits Phase 1 + 2 + 3. Pre-#152-extension the
    builder hardcoded Phase 1 + Phase 2 only; 3-phase Phase 3 was
    silently dropped from the mailed roster while auto-fill + rosters_tab
    still recorded those slots.
    """
    if not session.is_phase_aware:
        return _build_mail_for_phase(session, phase=1)

    blocks: list[str] = []
    for phase in session.iter_phases():
        body = _build_mail_for_phase(session, phase=phase)
        blocks.append(f"**Stage {phase}**\n\n{body}")
    return "\n\n".join(blocks)


async def _send_mail_preview(
    inter: discord.Interaction, session: RosterBuilderSession,
) -> None:
    """Build the text-template mail from the current roster and post a
    preview ephemerally. Officer copies it into the alliance's mail
    system manually (no auto-post in v1)."""
    mail = _build_mail_body(session)

    # Truncate to fit a Discord message — keep within 1900 chars so the
    # code-fence framing stays under 2000.
    preview = mail if len(mail) <= 1900 else mail[:1880] + "\n…(truncated)"
    await inter.response.send_message(
        "📄 **Mail preview**. Copy and paste into your alliance's mail system:\n"
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
        # Cache pre-pass (see apply_preset) — keeps the non-Discord
        # inference path inside `_read_roster_powers` honest under a
        # cold cache.
        try:
            import member_roster
            await member_roster._ensure_member_cache(interaction.guild)
        except Exception as e:
            logger.warning(
                "[STORM STRUCTURED] guild.chunk() pre-pass failed for "
                "guild=%s: %s",
                s.guild_id, e,
            )
        fresh_members, _refresh_errors = await asyncio.to_thread(
            _read_roster_powers,
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

    # Build mail — `_build_mail_body` honors paired sub_mode (paired
    # subs render inline as "Alice + sub Bob") and phase-aware presets
    # (two phase blocks under "**Phase 1**" / "**Phase 2**" headers).
    mail = _build_mail_body(s)

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
    # don't roll back the Discord post. Off the event loop because
    # `_write_rosters_tab` does a multi-cell gspread `update` that
    # can block for 1-2 seconds under load.
    write_errors = await asyncio.to_thread(_write_rosters_tab, s)

    # Close out the view.
    for item in view.children:
        item.disabled = True

    # Build the officer-facing summary based on the post outcome.
    if post_status == "posted_ok":
        summary_lines = ["✅ Roster posted.",
                         f"📬 Mail sent to {posted_to_mention}."]
    elif post_status == "no_channel":
        setup_cmd = "/setup → ⚔️ Desert Storm" if s.event_type == "DS" else "/setup → 🏜️ Canyon Storm"
        summary_lines = [
            "✅ Roster recorded.",
            "⚠️ No post channel is configured. Mail was built but not "
            f"sent. Run `{setup_cmd}` to pick one, or copy the mail "
            "manually below.",
        ]
    elif post_status == "channel_gone":
        summary_lines = [
            "✅ Roster recorded.",
            f"⚠️ The configured post channel (<#{post_channel_id}>) is "
            f"deleted or the bot can't see it. Re-run setup to pick a new "
            f"channel. Mail preview below.",
        ]
    else:  # send_failed
        summary_lines = [
            "✅ Roster recorded.",
            f"⚠️ The configured post channel <#{post_channel_id}> rejected "
            f"the send: `{(post_error or 'unknown error')[:120]}`. Check "
            f"the bot's permissions in that channel. Mail preview below.",
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

    view._release_session_lock()
    view.stop()


_ROSTERS_HEADER = [
    "Event Date", "Team", "Stage", "Zone", "Member", "Role",
    "Power at Assignment", "Discord ID", "Override Below Minimum",
    "Paired With", "Posted At (UTC)",
]

# Pre-Rule-B Sheet column names → their post-rename header. The
# `_write_rosters_tab` header migration uses this to copy data from
# the old column when a sheet still carries the legacy name. Readers
# (`storm_attendance` + `storm_history`) also fall through to the
# legacy name if the new one isn't present.
_LEGACY_HEADER_ALIASES: dict[str, str] = {
    "Override Below Minimum": "Override Below Floor",
}


def _write_rosters_tab(session: RosterBuilderSession) -> list[str]:
    """Append one row per slot to the alliance's configured rosters_tab.
    Returns a list of soft error strings (empty on success).

    The `Override Below Minimum` column captures whether the officer
    explicitly assigned the member below the effective zone minimum —
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
        return ["No rosters tab configured. Sheet write skipped."]

    try:
        sh = config.get_spreadsheet(session.guild_id)
    except Exception as e:
        return [f"spreadsheet open failed: {e}"]
    if sh is None:
        return ["spreadsheet not configured. Sheet write skipped."]

    try:
        ws = config.get_or_create_worksheet(
            sh, tab, header_row=_ROSTERS_HEADER,
            rows=2000, cols=len(_ROSTERS_HEADER),
        )
    except Exception as e:
        return [f"rosters tab create/open failed: {e}"]
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
        # Three header migrations to handle:
        # - "Paired With" column (added in #132)
        # - "Phase" column (added in #152) — inserted at position 2
        #   between Team and Zone, so EVERY existing data row needs a
        #   blank cell shifted in at position 2 to keep header-name
        #   lookups (e.g. `header.index("Zone")` → row[3]) honest.
        # - "Override Below Floor" → "Override Below Minimum" rename
        #   (Rule B follow-up). The migration uses _LEGACY_HEADER_ALIASES
        #   to find the old column's data and re-emit it under the new
        #   name, so officers don't lose existing "yes" flags.
        # Without the row-rewrite, an old row's Zone string would sit
        # under the new "Phase" column and Zone would read as Member,
        # corrupting every downstream read.
        needs_header_migration = existing and (
            "Paired With" not in existing
            or "Stage" not in existing
            or "Override Below Minimum" not in existing
        )
        if needs_header_migration:
            try:
                old_header = list(existing)
                old_idx = {c: i for i, c in enumerate(old_header)}
                # Translate each existing data row into the new column
                # order via name lookup, defaulting missing cells to "".
                rewritten_rows: list[list[str]] = [list(_ROSTERS_HEADER)]
                for row in (all_values[1:] if all_values else []):
                    new_row: list[str] = []
                    for col_name in _ROSTERS_HEADER:
                        if col_name == "Stage" and "Stage" not in old_idx:
                            # Old rows pre-date phase support — they
                            # represent a flat (single-phase) roster.
                            # Write "1" so loaders can join on phase
                            # without seeing blanks across the wire.
                            new_row.append("1")
                            continue
                        idx = old_idx.get(col_name, -1)
                        # Fall through to the legacy column name if the
                        # new name isn't on the sheet (header rename
                        # migration — see _LEGACY_HEADER_ALIASES).
                        if idx < 0:
                            legacy_name = _LEGACY_HEADER_ALIASES.get(col_name)
                            if legacy_name:
                                idx = old_idx.get(legacy_name, -1)
                        if 0 <= idx < len(row):
                            new_row.append(str(row[idx]))
                        else:
                            new_row.append("")
                    rewritten_rows.append(new_row)
                # `ws.clear()` is reliable + atomic-from-the-reader's-
                # perspective (gspread queues both calls back-to-back).
                ws.clear()
                ws.update("A1", rewritten_rows, value_input_option="RAW")
            except Exception as e:
                errors.append(
                    f"rosters tab header migration failed (data still "
                    f"appended, but readers may not see new columns): {e}"
                )

    from config import _utcnow_iso
    posted_at = _utcnow_iso()
    rows: list[list[str]] = []
    # Iterate phases the session knows about. Flat presets yield [1]
    # only — same row shape as before, with "1" written in the Phase
    # column for traceability (and a literal "1" matches the implicit
    # phase a flat preset represents). Phase-aware presets yield both
    # phases so a Phase 2-only zone (Arsenal / Silo / Mercenary Factory
    # on a phase-migration alliance) gets its rows written too.
    for phase in session.iter_phases():
        phase_assignments = session.assignments_for_phase(phase)
        phase_pairings = session.paired_subs_for_phase(phase)
        phase_overrides = session.below_floor_overrides_for_phase(phase)
        phase_cell = str(phase)
        for z in session.preset.zones:
            for key in phase_assignments.get(z.zone, []):
                m = session.members.get(key)
                if not m:
                    continue
                power = m.get("power")
                override = "yes" if key in phase_overrides else ""
                rows.append([
                    session.event_date or "",
                    session.team or "",
                    phase_cell,
                    z.zone,
                    m["name"],
                    "primary",
                    str(power) if power is not None else "unknown",
                    m.get("discord_id") or "",
                    override,
                    "",  # Paired With — primary rows leave blank.
                    posted_at,
                ])
                if session.is_paired:
                    sub_key = phase_pairings.get(key)
                    if sub_key:
                        sub_m = session.members.get(sub_key)
                        if sub_m:
                            sub_power = sub_m.get("power")
                            sub_override = (
                                "yes" if sub_key in phase_overrides else ""
                            )
                            rows.append([
                                session.event_date or "",
                                session.team or "",
                                phase_cell,
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
    # paired-with reference. Subs are event-level (no phase scope) so
    # they don't repeat per phase — written once with the Phase cell
    # left blank to distinguish from primary/paired-sub rows.
    for key in session.subs:
        m = session.members.get(key)
        if not m:
            continue
        power = m.get("power")
        rows.append([
            session.event_date or "",
            session.team or "",
            "",  # Phase — sub-pool rows are event-level, not phase-scoped.
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

    preset = await asyncio.to_thread(
        ss.load_preset, interaction.guild_id, event_type, preset_name,
    )
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

    # Team picker — skip if caller already passed team_override, or
    # if the alliance is configured single-team (the team is implicit).
    # Applies identically to DS and CS post-Rule A (#166).
    team = team_override or ""
    if not team:
        import config as _config
        cfg = _config.get_storm_config(interaction.guild_id, event_type) or {}
        teams_setting = (cfg.get("teams") or "both").strip()
        if teams_setting == "A":
            team = "A"
        elif teams_setting == "B":
            team = "B"
        else:
            team_view = _TeamPickerView(interaction.user.id)
            team_view.message = await interaction.followup.send(
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

    # Ensure the guild member cache is populated so `guild.get_member`
    # inside `_read_roster_powers` doesn't false-positive-infer real
    # members as not-on-Discord on a cold cache. Silently tolerates
    # the SERVER MEMBERS INTENT being off — the reader's own thin-
    # cache warning still fires in that case.
    try:
        import member_roster
        await member_roster._ensure_member_cache(interaction.guild)
    except Exception as e:
        logger.warning(
            "[STORM BUILDER] guild.chunk() pre-pass failed for guild=%s: %s",
            interaction.guild_id, e,
        )

    # Load powers + rules. Passes the live guild so the reader can
    # infer non-Discord status for rows with stale or blank Discord IDs
    # (#139) — explicit `not_on_discord` column still wins. gspread off
    # the event loop.
    members, roster_errors = await asyncio.to_thread(
        _read_roster_powers,
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
        # Lenient match: an on-behalf vote's target_member_id may be a
        # Discord ID (`str(member.id)`) when the picker resolved the
        # picked name to a live member, OR the picked name itself when
        # the row was misclassified as not_on_discord. The roster's
        # `members` dict keys by Discord ID for Discord members and by
        # name for non-Discord rows. Match a signup against either
        # form: a key match (the normal case) OR a name match against
        # the member's display name. Without the name-match leg, a
        # stale name-keyed vote for a Discord member leaks past the
        # filter and the builder reports "no signed-up members" even
        # though the bucket-map embed shows the vote.
        signup_keys_ci = {k.lower() for k in signup_keys if k}
        def _is_signed_up(key: str, m: dict) -> bool:
            if key in signup_keys:
                return True
            mname = (m.get("name") or "").strip().lower()
            return bool(mname and mname in signup_keys_ci)
        members = {k: v for k, v in members.items() if _is_signed_up(k, v)}
        # Surface signup keys we couldn't reconcile to any roster row.
        matched_names_ci = {
            (m.get("name") or "").strip().lower()
            for m in members.values()
        }
        missing = {
            sk for sk in signup_keys
            if sk not in members and sk.lower() not in matched_names_ci
        }
        if missing:
            roster_errors.append(
                f"{len(missing)} signed-up member(s) couldn't be matched to a "
                f"roster row: {', '.join(sorted(missing))[:200]}"
            )
        if not members:
            from storm_date_helpers import format_event_date
            await interaction.followup.send(
                f"⚠️ No signed-up members match team **{team or 'A'}** for "
                f"event **{format_event_date(event_date)}**. Run "
                f"`{HUB_COMMAND[event_type]}` and click "
                f"**{HUB_BTN_VIEW_SIGNUPS}** to see who's voted, or run "
                f"the apply flow without an event date to use the full "
                f"roster.",
                ephemeral=True,
            )
            return
        if before_count and before_count == len(members):
            # Defensive — everyone on roster voted; not really an error.
            pass

    rules = await asyncio.to_thread(
        smr.list_rules, interaction.guild_id, event_type,
    )
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
            from storm_date_helpers import format_event_date
            await interaction.followup.send(
                f"⚠️ Another officer (<@{holder}>) is already building "
                f"**Team {team or 'roster'}** for event **{format_event_date(event_date)}**. "
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
        self.message: Optional[discord.Message] = None

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

    async def on_timeout(self) -> None:
        """Strip the buttons after the 2-minute window. Officer can
        re-run the slash command to re-open the picker."""
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
