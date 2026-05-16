"""
Strategy preset editor for Desert Storm and Canyon Storm (#126).

Alliances define named "presets" — saved zone layouts with capacities,
per-team power floors (DS), and priorities. The same layout gets re-used
each week instead of being hand-built.

Storage shape (Sheet, alliance-owned, source of truth):

  DS Strategies columns:
    Preset Name | Zone | Max Players | Min Power A | Min Power B | Priority

  CS Strategies columns:
    Preset Name | Zone | Max Players | Min Power | Priority | Faction

Preset names are unique per (guild, event_type). Rows for one preset
share the Preset Name value.

In-Discord editor flow (DS form shown — CS variants live under
`/canyonstorm strategy …`):
  /desertstorm strategy create name:"…"  → opens editor seeded with
                                           canonical DS zones
  /desertstorm strategy edit name:"…"    → loads from Sheet → editor
  /desertstorm strategy list             → embed of saved presets
  /desertstorm strategy delete name:"…"  → confirm → remove

Editor state is buffered in memory on the View. Discord's interaction
token expires after 15 minutes; that's a natural session bound, so no
SQLite session table is needed for v1.
"""

from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord import app_commands

logger = logging.getLogger(__name__)


# ── Zone-family detection (apply-to-similar, #149) ──────────────────────────
#
# Strip a trailing space-separated roman or arabic numeral from a zone name
# so "Field Hospital II" / "Sample Warehouse 3" / "Data Center 1" all resolve
# to their building-family prefix. Used by the editor's apply-to-similar
# follow-up to detect when an edited zone has copy-eligible siblings in the
# same preset.

_ZONE_TAIL_RE = re.compile(r"\s+(?:[IVXLCDM]+|\d+)$", re.IGNORECASE)


def _zone_family_prefix(zone_name: str) -> str:
    """Return the building-family prefix of a zone name, or the input
    unchanged when there's no numeric suffix to strip. Inputs are
    case-preserved; matches are case-insensitive on the numeric tail."""
    if not zone_name:
        return ""
    return _ZONE_TAIL_RE.sub("", zone_name).strip()


def _sibling_zone_names(zones: "list[ZoneRow]", zone_name: str) -> "list[str]":
    """Return the names of zones in `zones` that share `zone_name`'s
    family prefix (other than `zone_name` itself). Returns [] when the
    zone has no numeric suffix (i.e. it's a one-of-a-kind building like
    Arsenal or Virus Lab)."""
    prefix = _zone_family_prefix(zone_name)
    if not prefix or prefix.lower() == zone_name.strip().lower():
        return []
    siblings: list[str] = []
    target = zone_name.strip().lower()
    for z in zones:
        candidate = (z.zone or "").strip()
        if not candidate or candidate.lower() == target:
            continue
        if _zone_family_prefix(candidate).lower() == prefix.lower():
            siblings.append(candidate)
    return siblings


# ── Power magnitude parsing ──────────────────────────────────────────────────
#
# Alliances type "250M", "1.2B", "300,000,000" etc. The roster Sheet
# values match the same convention. Parsing follows survey.py's
# magnitude-aware shorthand (#64).

def parse_power(raw: str) -> int | None:
    """Parse a power value into an integer. Returns None on garbage.
    Accepts: '250M', '1.2B', '300,000,000', '300000000', '300', empty."""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("_", "").lower()
    if not s:
        return 0
    multiplier = 1
    if s.endswith("k"):
        multiplier, s = 1_000, s[:-1]
    elif s.endswith("m"):
        multiplier, s = 1_000_000, s[:-1]
    elif s.endswith("b"):
        multiplier, s = 1_000_000_000, s[:-1]
    try:
        value = float(s) * multiplier
    except ValueError:
        return None
    return int(round(value))


def format_power(value: int) -> str:
    """Render a power value for display. 250000000 → '250M'."""
    if not value or value < 1000:
        return str(value or 0)
    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.1f}".rstrip("0").rstrip(".") + "B"
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}".rstrip("0").rstrip(".") + "K"
    return str(value)


def _safe_int(value, default: int = 0) -> int:
    """Coerce a Sheet cell (str / int / float / None / blank) to int.

    Returns `default` for None, empty string, and garbage strings —
    instead of raising ValueError. The previous `int(value or 0)` idiom
    raised on garbage strings ("abc" is truthy, falls through to int()).
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default


def _parse_power_cell(value, *, source: str = "") -> tuple[int, bool]:
    """Parse a Sheet cell into (power, was_garbage).

    Blank → (0, False) — alliance hasn't set a floor; not an error.
    "250M" → (250_000_000, False) — happy path.
    "tbd"  → (0, True) — couldn't parse; caller decides whether to
              warn the user or refuse the save.

    The previous `parse_power(...) or 0` idiom in load/save paths
    couldn't distinguish "blank" from "unparseable," so a typo'd
    Sheet cell silently became a `0` floor — directly contradicting
    the design's "exclude unknown power, don't coerce to zero" rule.
    """
    if value is None or value == "":
        return 0, False
    parsed = parse_power(value)
    if parsed is None:
        if source:
            logger.warning(
                "[STORM STRATEGY] couldn't parse power cell %r at %s — "
                "treating as 0; alliance should fix the Sheet entry.",
                value, source,
            )
        return 0, True
    return parsed, False


# ── Preset data model ────────────────────────────────────────────────────────
#
# A preset is a list of ZoneRow entries plus a name + (CS only) faction.
# Stored on disk as Sheet rows; buffered in memory during editing.

class ZoneRow:
    """One zone in a strategy preset. Same shape for DS and CS; the
    `min_power_b` field is unused for CS (single floor stored in
    `min_power_a` for code simplicity).

    Phase fields (`max_phase1..3`, `priority_phase1..3`) are only read
    when the parent `PresetBuffer.phase_count >= 2` (see #152). On
    flat presets they default to 0 and are ignored by the renderer +
    mail builder. The `priority` field is the single-mode (flat)
    priority; in phase-aware mode each phase has its own priority.
    """

    __slots__ = (
        "zone", "max_players",
        "max_phase1", "max_phase2", "max_phase3",
        "min_power_a", "min_power_b",
        "priority",
        "priority_phase1", "priority_phase2", "priority_phase3",
    )

    def __init__(self, zone: str, max_players: int = 0,
                 max_phase1: int = 0, max_phase2: int = 0, max_phase3: int = 0,
                 min_power_a: int = 0, min_power_b: int = 0,
                 priority: int = 0,
                 priority_phase1: int = 0,
                 priority_phase2: int = 0,
                 priority_phase3: int = 0):
        self.zone             = zone
        self.max_players      = _safe_int(max_players)
        self.max_phase1       = _safe_int(max_phase1)
        self.max_phase2       = _safe_int(max_phase2)
        self.max_phase3       = _safe_int(max_phase3)
        self.min_power_a      = _safe_int(min_power_a)
        self.min_power_b      = _safe_int(min_power_b)
        self.priority         = _safe_int(priority)
        self.priority_phase1  = _safe_int(priority_phase1)
        self.priority_phase2  = _safe_int(priority_phase2)
        self.priority_phase3  = _safe_int(priority_phase3)

    def max_for_phase(self, phase: int) -> int:
        """Return the max-player cap for a given phase. Phase 0 (flat)
        returns `max_players`; phase 1/2/3 return the matching
        `max_phase*` field."""
        if phase == 1:
            return int(self.max_phase1)
        if phase == 2:
            return int(self.max_phase2)
        if phase == 3:
            return int(self.max_phase3)
        return int(self.max_players)

    def priority_for_phase(self, phase: int) -> int:
        """Return the priority for a given phase. Phase 0 (flat) returns
        `priority`; phase 1/2/3 returns the matching
        `priority_phase*`. Phase-aware lookup falls back to the flat
        `priority` if the per-phase value is 0, so a preset that
        doesn't bother filling per-phase priorities still gets a
        coherent ordering."""
        per_phase = 0
        if phase == 1:
            per_phase = self.priority_phase1
        elif phase == 2:
            per_phase = self.priority_phase2
        elif phase == 3:
            per_phase = self.priority_phase3
        else:
            return int(self.priority)
        return int(per_phase) if per_phase else int(self.priority)

    def render_line(self, event_type: str, teams: str = "both",
                    phase_count: int = 0) -> str:
        """Summary for the editor embed. Respects the alliance's
        configured teams (#148 + Rule A / #166) so single-team alliances
        see only their team's minimum.

        Flat presets (`phase_count == 0`) render as a single line.
        Phase-aware presets (#172 / Rule L) break the capacity readout
        into one indented per-phase row beneath a zone header line so
        each phase's cap + per-phase priority is visible at a glance.
        """
        del event_type  # Both DS and CS render the same shape per Rule A.
        if teams == "A":
            mins = f"Min: {format_power(self.min_power_a)}"
        elif teams == "B":
            mins = f"Min: {format_power(self.min_power_b)}"
        else:
            mins = (
                f"Min A: {format_power(self.min_power_a)} · "
                f"Min B: {format_power(self.min_power_b)}"
            )

        if phase_count >= 2:
            # Per-zone-per-phase rendering: header line with the zone +
            # team minimums (which are per-team, not per-phase, so they
            # belong on the header), then one row per phase showing
            # capacity and any non-zero per-phase priority.
            header = f"• **{self.zone}** — {mins}"
            phase_lines: list[str] = []
            phase_prios = [
                self.priority_phase1, self.priority_phase2, self.priority_phase3,
            ][:phase_count]
            phase_caps = [
                self.max_phase1, self.max_phase2, self.max_phase3,
            ][:phase_count]
            for idx, (cap, prio) in enumerate(zip(phase_caps, phase_prios), start=1):
                prio_suffix = f" (priority {prio})" if prio else ""
                phase_lines.append(
                    f"   └ Phase {idx}: cap {cap}{prio_suffix}"
                )
            return "\n".join([header] + phase_lines)

        # Flat preset — single-line shape unchanged from pre-#172.
        cap = f"Max: {self.max_players}"
        prio = f" [P{self.priority}]" if self.priority else ""
        return f"• {self.zone:<20} ({cap})  {mins}{prio}"


class PresetBuffer:
    """Mutable preset state held by the editor view. Persists to Sheet on
    Save Preset.

    `phase_count` (#152): selects the phase model for this preset.
      - 0 → flat. Single per-zone slot capped by `max_players`. Phase
        fields on each zone are ignored.
      - 2 → two phases (Phase 1 / Phase 2). Each zone's slot is split
        into two sub-slots capped by `max_phase1` / `max_phase2`.
      - 3 → three phases. Same as 2 plus `max_phase3` + a Phase 3
        priority. Used by CS where Stage 1 / 2 / 3 each open
        different buildings.

    The flag is per-preset so the same alliance can run a phase-aware
    preset one week and a flat preset the next.

    `uses_phases` is a backward-compat alias for `phase_count >= 2` —
    keeps the older boolean check working at every callsite.
    """

    # Allowed phase_count values. 1 is treated as flat for tolerance
    # but new presets only ever write 0, 2, or 3.
    _VALID_PHASE_COUNTS = (0, 1, 2, 3)

    def __init__(self, name: str, event_type: str,
                 zones: list[ZoneRow] | None = None,
                 faction: str = "Either",
                 phase_count: int = 0,
                 uses_phases: bool | None = None):
        self.name        = name
        self.event_type  = event_type.upper()
        self.zones       = list(zones or [])
        self.faction     = faction       # CS only; ignored for DS
        # Back-compat: pre-3-phase code passed `uses_phases=True` for
        # 2-phase presets. Translate it if the caller didn't also pass
        # an explicit phase_count.
        if uses_phases and not phase_count:
            phase_count = 2
        self.phase_count = (
            int(phase_count) if int(phase_count) in self._VALID_PHASE_COUNTS else 0
        )
        self.dirty       = False         # tracks unsaved changes for the banner

    @property
    def uses_phases(self) -> bool:
        """True when the preset has 2 or more phases. Kept for the
        many call-sites that branch on a boolean without caring about
        the exact phase count."""
        return self.phase_count >= 2

    @uses_phases.setter
    def uses_phases(self, value: bool) -> None:
        """Setting uses_phases collapses to a 2-phase preset (the most
        common phase-aware case). Callers that want 3 phases set
        `phase_count = 3` directly."""
        if value and self.phase_count < 2:
            self.phase_count = 2
        elif not value:
            self.phase_count = 0

    def total_capacity(self) -> int:
        """Sum of per-zone capacities. Phase-aware presets sum each
        phase's max since a member can occupy slots in multiple phases
        (the migration case)."""
        if self.phase_count >= 2:
            total = 0
            for z in self.zones:
                total += z.max_phase1 + z.max_phase2
                if self.phase_count >= 3:
                    total += z.max_phase3
            return total
        return sum(z.max_players for z in self.zones)

    def find_zone(self, zone_name: str) -> ZoneRow | None:
        for z in self.zones:
            if z.zone.lower() == zone_name.lower():
                return z
        return None

    def upsert_zone(self, row: ZoneRow) -> None:
        existing = self.find_zone(row.zone)
        if existing is None:
            self.zones.append(row)
        else:
            existing.max_players      = row.max_players
            existing.max_phase1       = row.max_phase1
            existing.max_phase2       = row.max_phase2
            existing.max_phase3       = row.max_phase3
            existing.min_power_a      = row.min_power_a
            existing.min_power_b      = row.min_power_b
            existing.priority         = row.priority
            existing.priority_phase1  = row.priority_phase1
            existing.priority_phase2  = row.priority_phase2
            existing.priority_phase3  = row.priority_phase3
        self.dirty = True

    def remove_zone(self, zone_name: str) -> bool:
        before = len(self.zones)
        self.zones = [z for z in self.zones if z.zone.lower() != zone_name.lower()]
        if len(self.zones) != before:
            self.dirty = True
            return True
        return False


def canonical_zones_for(event_type: str) -> list[str]:
    """Canonical zone list per event type (#35). DS is a flat list, CS
    is grouped by stage but we flatten for preset purposes."""
    import storm
    if event_type == "DS":
        return list(storm.DS_ZONE_STRUCTURE)
    # CS: [(stage_num, zone_name, faction)]
    return [name for _, name, _ in storm.CS_ZONE_STRUCTURE]


def seed_default_preset(name: str, event_type: str) -> PresetBuffer:
    """Build a fresh preset buffer pre-populated with canonical zones."""
    zones = [ZoneRow(zone=name, max_players=0) for name in canonical_zones_for(event_type)]
    return PresetBuffer(name=name, event_type=event_type, zones=zones)


# ── Sheet I/O ────────────────────────────────────────────────────────────────


_DS_HEADER = ["Preset Name", "Zone", "Max Players",
              "Max Phase 1", "Max Phase 2", "Max Phase 3",
              "Min Power A", "Min Power B",
              "Priority",
              "Priority Phase 1", "Priority Phase 2", "Priority Phase 3",
              "Phase Count"]
_CS_HEADER = ["Preset Name", "Zone", "Max Players",
              "Max Phase 1", "Max Phase 2", "Max Phase 3",
              "Min Power",
              "Priority",
              "Priority Phase 1", "Priority Phase 2", "Priority Phase 3",
              "Faction", "Phase Count"]

# Truthy strings the legacy `Use Phases` column might carry. Used only
# to read pre-3-phase preset data — new writes always use the
# `Phase Count` int column.
_TRUE_STRINGS = {"true", "yes", "1", "y", "on", "phases"}


def _parse_phase_count(row: dict) -> int:
    """Resolve a row's phase_count from the new Phase Count column,
    falling back to the legacy Use Phases boolean if Phase Count is
    missing. Unknown / unparseable values clamp to 0 (flat)."""
    raw = row.get("Phase Count", "")
    if raw not in ("", None):
        try:
            val = int(str(raw).strip())
        except (TypeError, ValueError):
            val = 0
        if val in (2, 3):
            return val
        return 0
    # Legacy fallback — old presets only ever did 2-phase.
    legacy = str(row.get("Use Phases", "") or "").strip().lower()
    return 2 if legacy in _TRUE_STRINGS else 0


def _parse_uses_phases(raw: object) -> bool:
    """Legacy helper retained for the tests that exercise the old
    Use Phases column parsing directly. Production code reads phase
    count via `_parse_phase_count`."""
    return str(raw or "").strip().lower() in _TRUE_STRINGS

# Canonical team size used by the editor's capacity gauge and the
# Save-time over-capacity guard. DS and CS both run 30-slot teams in
# the current game version; making this a single constant means
# alliances who run smaller sub-teams won't be blocked by mistake.
# (If/when teams move to alliance-configurable sizing, swap this for
# a per-guild config field — same callsites.)
_TEAM_SIZE_HINT = 30


def _strategies_tab_name(guild_id: int, event_type: str) -> str:
    import config
    cfg = config.get_structured_storm_config(guild_id, event_type)
    return cfg.get("strategies_tab") or config.default_structured_tab(
        event_type, "strategies_tab"
    )


def _get_or_create_strategies_worksheet(guild_id: int, event_type: str):
    """Returns the worksheet, creating it (with header row) if missing.
    Returns None if the guild has no Sheet configured (or `gspread`
    raised opening it — unconfigured / bad creds / deleted spreadsheet)."""
    import config
    # `config.get_spreadsheet` raises rather than returning None for
    # unconfigured guilds. Catch broadly so /desertstorm strategy commands
    # don't die with an unhandled traceback on a guild that hasn't run setup.
    try:
        sh = config.get_spreadsheet(guild_id)
    except Exception as e:
        logger.warning(
            "[STORM STRATEGY] get_spreadsheet failed for guild=%s: %s",
            guild_id, e,
        )
        return None
    if sh is None:
        return None
    tab_name = _strategies_tab_name(guild_id, event_type)
    if not tab_name:
        return None
    header = _DS_HEADER if event_type == "DS" else _CS_HEADER
    import config
    return config.get_or_create_worksheet(
        sh, tab_name, header_row=header,
        rows=1000, cols=max(8, len(header)),
    )


def load_preset(guild_id: int, event_type: str, name: str) -> PresetBuffer | None:
    """Load a named preset from the alliance's strategies tab. Returns
    None if the preset doesn't exist or the Sheet isn't configured."""
    ws = _get_or_create_strategies_worksheet(guild_id, event_type)
    if ws is None:
        return None
    try:
        records = ws.get_all_records()
    except Exception as e:
        logger.warning("[STORM STRATEGY] load_preset failed for guild=%s event=%s name=%s: %s",
                       guild_id, event_type, name, e)
        return None
    rows = [r for r in records if str(r.get("Preset Name", "")).strip().lower() == name.lower()]
    if not rows:
        return None
    zones: list[ZoneRow] = []
    faction = "Either"
    phase_count = 0
    for r in rows:
        zone_name = str(r.get("Zone", "")).strip()
        src = f"preset={name!r} zone={zone_name!r} event={event_type}"
        # `Phase Count` (or legacy `Use Phases`) is denormalised across
        # every row of a preset. Take the max seen so partial-edit
        # states still resolve coherently.
        phase_count = max(phase_count, _parse_phase_count(r))
        if event_type == "DS":
            min_a, _ = _parse_power_cell(r.get("Min Power A", ""), source=src + " col=Min Power A")
            min_b, _ = _parse_power_cell(r.get("Min Power B", ""), source=src + " col=Min Power B")
            zones.append(ZoneRow(
                zone=zone_name,
                max_players=_safe_int(r.get("Max Players", 0)),
                max_phase1=_safe_int(r.get("Max Phase 1", 0)),
                max_phase2=_safe_int(r.get("Max Phase 2", 0)),
                max_phase3=_safe_int(r.get("Max Phase 3", 0)),
                min_power_a=min_a,
                min_power_b=min_b,
                priority=_safe_int(r.get("Priority", 0)),
                priority_phase1=_safe_int(r.get("Priority Phase 1", 0)),
                priority_phase2=_safe_int(r.get("Priority Phase 2", 0)),
                priority_phase3=_safe_int(r.get("Priority Phase 3", 0)),
            ))
        else:
            min_p, _ = _parse_power_cell(r.get("Min Power", ""), source=src + " col=Min Power")
            zones.append(ZoneRow(
                zone=zone_name,
                max_players=_safe_int(r.get("Max Players", 0)),
                max_phase1=_safe_int(r.get("Max Phase 1", 0)),
                max_phase2=_safe_int(r.get("Max Phase 2", 0)),
                max_phase3=_safe_int(r.get("Max Phase 3", 0)),
                min_power_a=min_p,
                min_power_b=0,
                priority=_safe_int(r.get("Priority", 0)),
                priority_phase1=_safe_int(r.get("Priority Phase 1", 0)),
                priority_phase2=_safe_int(r.get("Priority Phase 2", 0)),
                priority_phase3=_safe_int(r.get("Priority Phase 3", 0)),
            ))
            row_faction = str(r.get("Faction", "")).strip()
            if row_faction:
                faction = row_faction
    return PresetBuffer(name=name, event_type=event_type, zones=zones,
                        faction=faction, phase_count=phase_count)


def list_presets(guild_id: int, event_type: str) -> list[str]:
    """Return preset names defined for this guild + event type."""
    ws = _get_or_create_strategies_worksheet(guild_id, event_type)
    if ws is None:
        return []
    try:
        records = ws.get_all_records()
    except Exception as e:
        logger.warning("[STORM STRATEGY] list_presets failed for guild=%s event=%s: %s",
                       guild_id, event_type, e)
        return []
    seen: dict[str, None] = {}
    for r in records:
        name = str(r.get("Preset Name", "")).strip()
        if name and name not in seen:
            seen[name] = None
    return list(seen)


def save_preset(guild_id: int, event_type: str, buf: PresetBuffer) -> bool:
    """Persist a preset to the Sheet. Replaces all rows for this preset
    name with the buffer's current zones. Returns True on success."""
    ws = _get_or_create_strategies_worksheet(guild_id, event_type)
    if ws is None:
        return False

    # Read all rows; keep those NOT matching this preset name. Then
    # append the buffer's rows. The replace strategy avoids tracking
    # row indexes per zone.
    try:
        all_values = ws.get_all_values()
    except Exception as e:
        logger.warning("[STORM STRATEGY] save_preset read-back failed for guild=%s event=%s: %s",
                       guild_id, event_type, e)
        return False

    header = _DS_HEADER if event_type == "DS" else _CS_HEADER
    # Map sibling preset rows from their OLD header shape into the new
    # column order. Without this remap, a tab that already had presets
    # written under the pre-#152 header (6 columns for DS, or the
    # interim Use-Phases shape) would silently mis-align: the new
    # 13-col header gets written over the tab, but each sibling row
    # keeps its old cells in their old positions — `row[3]` was
    # `Min Power A` under the old header but is `Max Phase 1` under
    # the new one. The next `load_preset` then reads the old power
    # value as a phase capacity (data corruption).
    old_header = [str(c).strip() for c in (all_values[0] if all_values else [])]
    old_header_idx = {name: idx for idx, name in enumerate(old_header)}

    def _translate(row: list) -> list[str]:
        """Re-emit one preset row in the new column order. Cells
        missing from the old header default to an empty string so
        `_safe_int` / `_parse_phase_count` fall through to their
        defaults (0 / 0). Legacy `Use Phases` (truthy → phase_count
        = 2) is honoured here too so an interim 2-phase preset
        round-trips into the new `Phase Count` column on the next
        save."""
        out: list[str] = []
        legacy_uses_phases = (
            _parse_uses_phases(row[old_header_idx["Use Phases"]])
            if "Use Phases" in old_header_idx
               and old_header_idx["Use Phases"] < len(row)
            else False
        )
        for col_name in header:
            if col_name == "Phase Count" and "Phase Count" not in old_header_idx:
                out.append("2" if legacy_uses_phases else "0")
                continue
            idx = old_header_idx.get(col_name, -1)
            if 0 <= idx < len(row):
                out.append(str(row[idx]))
            else:
                out.append("")
        return out

    # Filter: keep header + non-matching rows, translated to new shape.
    kept = [header]
    for row in all_values[1:]:  # skip existing header row
        if not row:
            continue
        if str(row[0]).strip().lower() != buf.name.lower():
            kept.append(_translate(row))
    # Append buffer rows.
    phase_count_cell = str(buf.phase_count)
    for z in buf.zones:
        if event_type == "DS":
            kept.append([
                buf.name, z.zone, str(z.max_players),
                str(z.max_phase1), str(z.max_phase2), str(z.max_phase3),
                str(z.min_power_a), str(z.min_power_b),
                str(z.priority),
                str(z.priority_phase1), str(z.priority_phase2), str(z.priority_phase3),
                phase_count_cell,
            ])
        else:
            kept.append([
                buf.name, z.zone, str(z.max_players),
                str(z.max_phase1), str(z.max_phase2), str(z.max_phase3),
                str(z.min_power_a),
                str(z.priority),
                str(z.priority_phase1), str(z.priority_phase2), str(z.priority_phase3),
                buf.faction,
                phase_count_cell,
            ])

    try:
        ws.clear()
        ws.update("A1", kept, value_input_option="RAW")
    except Exception as e:
        logger.warning("[STORM STRATEGY] save_preset write failed for guild=%s event=%s name=%s: %s",
                       guild_id, event_type, buf.name, e)
        return False
    buf.dirty = False
    return True


def delete_preset(guild_id: int, event_type: str, name: str) -> bool:
    """Remove all rows for a named preset. Returns True if any rows
    were removed; False if the preset wasn't found."""
    ws = _get_or_create_strategies_worksheet(guild_id, event_type)
    if ws is None:
        return False
    try:
        all_values = ws.get_all_values()
    except Exception as e:
        logger.warning("[STORM STRATEGY] delete_preset read failed for guild=%s event=%s: %s",
                       guild_id, event_type, e)
        return False

    header = _DS_HEADER if event_type == "DS" else _CS_HEADER
    kept = [header]
    removed = False
    for row in all_values[1:]:
        if not row:
            continue
        if str(row[0]).strip().lower() == name.lower():
            removed = True
            continue
        kept.append(row)
    if not removed:
        return False
    try:
        ws.clear()
        ws.update("A1", kept, value_input_option="RAW")
    except Exception as e:
        logger.warning("[STORM STRATEGY] delete_preset write failed for guild=%s event=%s name=%s: %s",
                       guild_id, event_type, name, e)
        return False
    return True


# ── In-Discord editor ────────────────────────────────────────────────────────


def _resolve_storm_teams(guild_id: int, event_type: str) -> str:
    """Read the alliance's configured teams ('both' | 'A' | 'B') from
    `guild_storm_config` for the given event type. Falls back to 'both'
    on a missing row or config-read failure — that's the historical
    behaviour, so the gate is invisible to alliances that haven't run
    setup since #148. Applies identically to DS and CS per Rule A /
    #166."""
    try:
        import config
        saved = (config.get_storm_config(int(guild_id), event_type) or {}).get("teams") or "both"
    except Exception:
        return "both"
    return saved if saved in ("both", "A", "B") else "both"


def _resolve_ds_teams(guild_id: int) -> str:
    """Back-compat alias — pre-#166 callers."""
    return _resolve_storm_teams(guild_id, "DS")


def _build_editor_embed(buf: PresetBuffer, team_size_hint: int = _TEAM_SIZE_HINT,
                        *, teams: str = "both") -> discord.Embed:
    label = "Desert Storm" if buf.event_type == "DS" else "Canyon Storm"
    title = f"🛡️ Editing Preset: {buf.name}"
    desc_lines = [f"🗺️ Event: {label}"]
    if teams in ("A", "B"):
        # Surface the gate on the embed too — without this, an officer
        # opening a single-team preset would see only one minimum in
        # the rows and wonder if their setup is broken. Applies to
        # both DS and CS per Rule A.
        desc_lines.append(f"👥 Teams: **Team {teams} only** (minimums shown match)")
    if buf.event_type == "CS":
        desc_lines.append(f"⚙️ Faction: {buf.faction}")
    # Phase mode line surfaces the toggle state so officers can
    # eyeball which mode the preset is in without scanning the
    # dropdown.
    if buf.phase_count == 0:
        mode_label = "Flat"
    elif buf.phase_count == 2:
        mode_label = "2 Phases (P1 + P2)"
    elif buf.phase_count == 3:
        mode_label = "3 Phases (P1 + P2 + P3)"
    else:
        mode_label = f"{buf.phase_count} Phases"
    desc_lines.append(f"🔀 Mode: **{mode_label}**")
    desc_lines.append("")
    if buf.zones:
        desc_lines.append("📋 **Zones:**")
        for z in buf.zones:
            desc_lines.append(z.render_line(
                buf.event_type, teams=teams,
                phase_count=buf.phase_count,
            ))
    else:
        desc_lines.append("*No zones in this preset yet.*")
    desc_lines.append("")
    # Capacity vs. team-size is informational. Alliances often build
    # in flex room (Mercenary + Arsenal both open when center opens,
    # subs absorb no-shows, etc.) so over-30 is normal, not an error.
    # Under-30 still gets the ⚠️ since under-staffing is the
    # interesting case.
    cap = buf.total_capacity()
    if cap < team_size_hint:
        glyph = "⚠️"
    elif cap == team_size_hint:
        glyph = "✅"
    else:
        glyph = "ℹ️"
    desc_lines.append(
        f"📊 Capacity: **{cap}** (team size {team_size_hint}; flex room is fine) {glyph}"
    )
    if buf.dirty:
        desc_lines.append("⚠️ *Unsaved changes — Save preset to save your changes.*")
    return discord.Embed(
        title=title,
        description="\n".join(desc_lines),
        color=discord.Color.blurple(),
    )


class _ZoneEditModal(discord.ui.Modal):
    """Modal for editing one zone on a **flat** preset.

    Phase-aware presets (#152) use the multi-step
    `_open_zone_phase_wizard` flow instead — Discord modals cap at 5
    fields, which isn't enough for `Max P1`, `Max P2`, `Max P3`,
    `Min A`, `Min B`, `Priority P1..3` in one shot.

    Branches DS vs CS on field count, and on DS branches further on
    whether the alliance has both teams configured (#148). Single-team
    alliances see only the floor that matters.

    There's no "remove zone" field — clearing a zone's numeric fields
    and saving leaves the zone in the list with zero capacity, which
    the roster builder skips. Removal-as-a-side-effect is intentional;
    the modal stays focused on values, not deletion.
    """

    def __init__(self, view: "_PresetEditorView", zone_name: str):
        super().__init__(title=f"Edit Zone: {zone_name}"[:45])
        self._view = view
        self._zone_name = zone_name
        existing = view.buf.find_zone(zone_name) or ZoneRow(zone=zone_name)
        # Single Max Players field — phase-aware editing routes through
        # the wizard flow, not this modal.
        self.max_input = discord.ui.TextInput(
            label="Max Players",
            placeholder="e.g. 4",
            default=str(existing.max_players or ""),
            required=False, max_length=4,
        )
        self.add_item(self.max_input)

        # Resolve which DS teams the alliance runs so the modal only
        # asks for the relevant floors. The parent view snapshotted this
        # at open time; reading from there keeps modal + embed in sync
        # and avoids a second config read per modal open.
        self._teams = (
            getattr(view, "teams", "both")
        )

        # Two-team alliances (teams=both) show per-team minimums; the
        # single-team variants (teams=A or teams=B) show one. Applies
        # identically to DS and CS per Rule A / #166.
        self.power_a_input = None
        self.power_b_input = None
        self.power_input = None
        if self._teams == "both":
            self.power_a_input = discord.ui.TextInput(
                label="Min Power Team A",
                placeholder="e.g. 300M",
                default=format_power(existing.min_power_a) if existing.min_power_a else "",
                required=False, max_length=12,
            )
            self.add_item(self.power_a_input)
            self.power_b_input = discord.ui.TextInput(
                label="Min Power Team B",
                placeholder="e.g. 180M",
                default=format_power(existing.min_power_b) if existing.min_power_b else "",
                required=False, max_length=12,
            )
            self.add_item(self.power_b_input)
        elif self._teams == "A":
            self.power_a_input = discord.ui.TextInput(
                label="Min Power Team A",
                placeholder="e.g. 300M",
                default=format_power(existing.min_power_a) if existing.min_power_a else "",
                required=False, max_length=12,
            )
            self.add_item(self.power_a_input)
        else:  # teams == "B"
            self.power_b_input = discord.ui.TextInput(
                label="Min Power Team B",
                placeholder="e.g. 180M",
                default=format_power(existing.min_power_b) if existing.min_power_b else "",
                required=False, max_length=12,
            )
            self.add_item(self.power_b_input)
            self.power_a_input = None
            self.power_b_input = None

        self.priority_input = discord.ui.TextInput(
            label="Priority (1 = highest; ties OK)",
            placeholder="e.g. 1 — same number across zones is fine",
            default=str(existing.priority or ""),
            required=False, max_length=3,
        )
        self.add_item(self.priority_input)

    async def on_submit(self, interaction: discord.Interaction):
        # Preserve existing phase values when editing a flat preset —
        # the officer might toggle phases back on later.
        existing = self._view.buf.find_zone(self._zone_name) or ZoneRow(zone=self._zone_name)
        max_phase1 = existing.max_phase1
        max_phase2 = existing.max_phase2
        max_phase3 = existing.max_phase3
        priority_phase1 = existing.priority_phase1
        priority_phase2 = existing.priority_phase2
        priority_phase3 = existing.priority_phase3
        try:
            max_players = int((self.max_input.value or "0").strip() or 0)
        except ValueError:
            await interaction.response.send_message(
                f"⚠️ Max Players must be a number — got `{self.max_input.value}`. "
                f"Try again.",
                ephemeral=True,
            )
            return

        # Refuse garbage in the power fields rather than silently zeroing —
        # a typo would otherwise persist as a "no minimum" entry and
        # the eligibility filter would pass below-minimum members
        # through it. Hidden inputs (single-team alliances) preserve
        # the stored value rather than overwriting to 0 — keeps the
        # door open for an alliance to flip to two-team mode without
        # losing prior minimum values. Applies identically to DS + CS
        # per Rule A / #166.
        if self.power_a_input is not None:
            min_a, bad_a = _parse_power_cell(self.power_a_input.value or "")
        else:
            min_a, bad_a = (existing.min_power_a or 0), False
        if self.power_b_input is not None:
            min_b, bad_b = _parse_power_cell(self.power_b_input.value or "")
        else:
            min_b, bad_b = (existing.min_power_b or 0), False
        if bad_a or bad_b:
            await interaction.response.send_message(
                "⚠️ One of the power values didn't parse. "
                "Use formats like `300M`, `1.2B`, or `300000000`. "
                "Leave blank for no minimum.",
                ephemeral=True,
            )
            return

        try:
            priority = int((self.priority_input.value or "0").strip() or 0)
        except ValueError:
            await interaction.response.send_message(
                f"⚠️ Priority must be a number — got `{self.priority_input.value}`. "
                f"Try again.",
                ephemeral=True,
            )
            return

        self._view.buf.upsert_zone(ZoneRow(
            zone=self._zone_name,
            max_players=max_players,
            max_phase1=max_phase1, max_phase2=max_phase2, max_phase3=max_phase3,
            min_power_a=min_a, min_power_b=min_b,
            priority=priority,
            priority_phase1=priority_phase1,
            priority_phase2=priority_phase2,
            priority_phase3=priority_phase3,
        ))
        siblings = _sibling_zone_names(self._view.buf.zones, self._zone_name)
        await self._view.refresh(
            interaction,
            message=f"✏️ Updated **{self._zone_name}**.",
        )
        # Apply-to-similar follow-up (#149): when the edited zone has
        # numbered siblings in this preset, offer to copy these same
        # values to any of them. Best-effort — a failure here must not
        # invalidate the edit that already succeeded above.
        if siblings:
            try:
                apply_view = _ApplyToSimilarView(
                    editor_view=self._view,
                    source_zone=self._zone_name,
                    sibling_names=siblings,
                    values=ZoneRow(
                        zone=self._zone_name,
                        max_players=max_players,
                        max_phase1=max_phase1, max_phase2=max_phase2, max_phase3=max_phase3,
                        min_power_a=min_a, min_power_b=min_b,
                        priority=priority,
                        priority_phase1=priority_phase1,
                        priority_phase2=priority_phase2,
                        priority_phase3=priority_phase3,
                    ),
                )
                apply_view.message = await interaction.followup.send(
                    content=(
                        f"💡 **{self._zone_name}** has similar zones in this preset: "
                        f"{', '.join(siblings)}. Would you like to apply the "
                        f"same settings to these as well?"
                    ),
                    view=apply_view,
                    ephemeral=True,
                )
            except discord.HTTPException as e:
                logger.warning(
                    "[STORM STRATEGY] apply-to-similar follow-up couldn't be "
                    "posted for zone=%s in preset=%s: %s",
                    self._zone_name, self._view.buf.name, e,
                )


# ── Phase-aware zone edit wizard (#152) ──────────────────────────────────────
#
# Multi-step modal flow for phase-aware presets. Discord allows at most
# 5 components in a single modal, which isn't enough to ask for every
# value a 3-phase DS-both preset needs (Max P1/P2/P3, Min A, Min B,
# Priority P1/P2/P3 = 9 fields). Splitting into three pages — capacity,
# power floors, priorities — keeps each modal under the cap and reads
# clearly: officers see "set my Phase 1 / 2 / 3 capacities", "set my
# Team A / B floors", "set my Phase 1 / 2 / 3 priorities" instead of
# one big form.
#
# State threads through each page via the parent editor view's
# `_pending_zone_edit` attribute (keyed by zone name). Once page 3
# submits, the accumulated values land on the PresetBuffer in one
# upsert_zone call and the editor embed refreshes. Apply-to-similar
# (#149) still fires after the final page like the flat flow.


def _stash_pending_edit(view, zone_name: str) -> dict:
    """Get or create the per-zone wizard state stash on the editor."""
    stash = getattr(view, "_pending_zone_edits", None)
    if stash is None:
        stash = {}
        view._pending_zone_edits = stash
    if zone_name not in stash:
        existing = view.buf.find_zone(zone_name) or ZoneRow(zone=zone_name)
        # Seed from the saved row so a wizard cancel mid-flow leaves
        # nothing changed downstream.
        stash[zone_name] = {
            "max_phase1": existing.max_phase1,
            "max_phase2": existing.max_phase2,
            "max_phase3": existing.max_phase3,
            "min_power_a": existing.min_power_a,
            "min_power_b": existing.min_power_b,
            "priority_phase1": existing.priority_phase1,
            "priority_phase2": existing.priority_phase2,
            "priority_phase3": existing.priority_phase3,
        }
    return stash[zone_name]


def _clear_pending_edit(view, zone_name: str) -> None:
    stash = getattr(view, "_pending_zone_edits", None) or {}
    stash.pop(zone_name, None)


class _ZonePhaseCapacityModal(discord.ui.Modal):
    """Page 1/3 of the phase-aware wizard — capacities per phase."""

    def __init__(self, view: "_PresetEditorView", zone_name: str):
        phase_count = int(getattr(view.buf, "phase_count", 2) or 2)
        super().__init__(title=f"{zone_name} — Capacity ({phase_count}P)"[:45])
        self._view = view
        self._zone_name = zone_name
        self._phase_count = phase_count
        pending = _stash_pending_edit(view, zone_name)

        self.max_phase1_input = discord.ui.TextInput(
            label="Max Phase 1",
            placeholder="e.g. 4 (leave 0 to skip Phase 1 at this zone)",
            default=str(pending["max_phase1"] or ""),
            required=False, max_length=4,
        )
        self.add_item(self.max_phase1_input)
        self.max_phase2_input = discord.ui.TextInput(
            label="Max Phase 2",
            placeholder="e.g. 2 (leave 0 to skip Phase 2 at this zone)",
            default=str(pending["max_phase2"] or ""),
            required=False, max_length=4,
        )
        self.add_item(self.max_phase2_input)
        if phase_count >= 3:
            self.max_phase3_input = discord.ui.TextInput(
                label="Max Phase 3",
                placeholder="e.g. 3 (leave 0 to skip Phase 3 at this zone)",
                default=str(pending["max_phase3"] or ""),
                required=False, max_length=4,
            )
            self.add_item(self.max_phase3_input)
        else:
            self.max_phase3_input = None

    async def on_submit(self, interaction: discord.Interaction):
        pending = _stash_pending_edit(self._view, self._zone_name)
        # Validate each field; the wizard refuses to advance on any
        # parse error so officers fix it before moving on.
        for field, key in (
            (self.max_phase1_input, "max_phase1"),
            (self.max_phase2_input, "max_phase2"),
            (self.max_phase3_input, "max_phase3"),
        ):
            if field is None:
                continue
            try:
                pending[key] = int((field.value or "0").strip() or 0)
            except ValueError:
                await interaction.response.send_message(
                    f"⚠️ {field.label} must be a number — got `{field.value}`. "
                    f"Reopen the zone to retry.",
                    ephemeral=True,
                )
                return
        # Post the Next-button follow-up to advance to page 2.
        view = _ZoneWizardNextView(
            self._view, self._zone_name,
            next_page="floors",
            label="Next → Power Minimums",
        )
        await interaction.response.send_message(
            content=(
                f"✅ Capacities recorded for **{self._zone_name}**. "
                f"Click **Next** to set the power minimums."
            ),
            view=view, ephemeral=True,
        )
        view.message = await interaction.original_response()


class _ZonePhaseFloorsModal(discord.ui.Modal):
    """Page 2/3 of the phase-aware wizard — power minimums. Field
    shape branches on the alliance's teams config (both = A+B, A
    only or B only = one input). Applies identically to DS and CS
    per Rule A / #166."""

    def __init__(self, view: "_PresetEditorView", zone_name: str):
        super().__init__(title=f"{zone_name} — Power Minimums"[:45])
        self._view = view
        self._zone_name = zone_name
        pending = _stash_pending_edit(view, zone_name)
        self._teams = getattr(view, "teams", "both")

        self.power_input = None
        self.power_a_input = None
        self.power_b_input = None
        if self._teams in ("both", "A"):
            self.power_a_input = discord.ui.TextInput(
                label="Min Power Team A",
                placeholder="e.g. 300M",
                default=format_power(pending["min_power_a"])
                        if pending["min_power_a"] else "",
                required=False, max_length=12,
            )
            self.add_item(self.power_a_input)
        if self._teams in ("both", "B"):
            self.power_b_input = discord.ui.TextInput(
                label="Min Power Team B",
                placeholder="e.g. 180M",
                default=format_power(pending["min_power_b"])
                        if pending["min_power_b"] else "",
                required=False, max_length=12,
            )
            self.add_item(self.power_b_input)

    async def on_submit(self, interaction: discord.Interaction):
        pending = _stash_pending_edit(self._view, self._zone_name)
        if self.power_a_input is not None:
            val, bad = _parse_power_cell(self.power_a_input.value or "")
            if bad:
                await interaction.response.send_message(
                    f"⚠️ Min Power Team A didn't parse — got "
                    f"`{self.power_a_input.value}`. Use `300M`, `1.2B`, or "
                    f"`300000000`.",
                    ephemeral=True,
                )
                return
            pending["min_power_a"] = val
        if self.power_b_input is not None:
            val, bad = _parse_power_cell(self.power_b_input.value or "")
            if bad:
                await interaction.response.send_message(
                    f"⚠️ Min Power Team B didn't parse — got "
                    f"`{self.power_b_input.value}`. Use `300M`, `1.2B`, or "
                    f"`300000000`.",
                    ephemeral=True,
                )
                return
            pending["min_power_b"] = val

        view = _ZoneWizardNextView(
            self._view, self._zone_name,
            next_page="priority",
            label="Next → Priority Per Phase",
        )
        await interaction.response.send_message(
            content=(
                f"✅ Minimums recorded for **{self._zone_name}**. "
                f"Click **Next** to set the per-phase auto-fill priorities."
            ),
            view=view, ephemeral=True,
        )
        view.message = await interaction.original_response()


class _ZonePhasePriorityModal(discord.ui.Modal):
    """Page 3/3 of the phase-aware wizard — priority per phase.
    Submitting this page finalises the edit and refreshes the editor."""

    def __init__(self, view: "_PresetEditorView", zone_name: str):
        phase_count = int(getattr(view.buf, "phase_count", 2) or 2)
        super().__init__(title=f"{zone_name} — Priority ({phase_count}P)"[:45])
        self._view = view
        self._zone_name = zone_name
        self._phase_count = phase_count
        pending = _stash_pending_edit(view, zone_name)

        self.prio_p1_input = discord.ui.TextInput(
            label="Priority Phase 1 (1 = highest)",
            placeholder="leave blank for no priority",
            default=str(pending["priority_phase1"] or ""),
            required=False, max_length=3,
        )
        self.add_item(self.prio_p1_input)
        self.prio_p2_input = discord.ui.TextInput(
            label="Priority Phase 2",
            placeholder="leave blank for no priority",
            default=str(pending["priority_phase2"] or ""),
            required=False, max_length=3,
        )
        self.add_item(self.prio_p2_input)
        if phase_count >= 3:
            self.prio_p3_input = discord.ui.TextInput(
                label="Priority Phase 3",
                placeholder="leave blank for no priority",
                default=str(pending["priority_phase3"] or ""),
                required=False, max_length=3,
            )
            self.add_item(self.prio_p3_input)
        else:
            self.prio_p3_input = None

    async def on_submit(self, interaction: discord.Interaction):
        pending = _stash_pending_edit(self._view, self._zone_name)
        for field, key in (
            (self.prio_p1_input, "priority_phase1"),
            (self.prio_p2_input, "priority_phase2"),
            (self.prio_p3_input, "priority_phase3"),
        ):
            if field is None:
                continue
            try:
                pending[key] = int((field.value or "0").strip() or 0)
            except ValueError:
                await interaction.response.send_message(
                    f"⚠️ {field.label} must be a number — got `{field.value}`. "
                    f"Reopen the zone to retry.",
                    ephemeral=True,
                )
                return

        # Finalise: write the accumulated values to the PresetBuffer.
        existing = self._view.buf.find_zone(self._zone_name) or ZoneRow(zone=self._zone_name)
        self._view.buf.upsert_zone(ZoneRow(
            zone=self._zone_name,
            # max_players + flat priority preserved from the existing
            # row so toggling back to flat doesn't lose those values.
            max_players=existing.max_players,
            priority=existing.priority,
            max_phase1=pending["max_phase1"],
            max_phase2=pending["max_phase2"],
            max_phase3=pending["max_phase3"],
            min_power_a=pending["min_power_a"],
            min_power_b=pending["min_power_b"],
            priority_phase1=pending["priority_phase1"],
            priority_phase2=pending["priority_phase2"],
            priority_phase3=pending["priority_phase3"],
        ))
        _clear_pending_edit(self._view, self._zone_name)
        siblings = _sibling_zone_names(self._view.buf.zones, self._zone_name)
        await self._view.refresh(
            interaction,
            message=f"✏️ Updated **{self._zone_name}** ({self._phase_count}-phase).",
        )
        # Apply-to-similar follow-up — same offer as the flat flow.
        if siblings:
            try:
                apply_view = _ApplyToSimilarView(
                    editor_view=self._view,
                    source_zone=self._zone_name,
                    sibling_names=siblings,
                    values=ZoneRow(
                        zone=self._zone_name,
                        max_players=existing.max_players,
                        max_phase1=pending["max_phase1"],
                        max_phase2=pending["max_phase2"],
                        max_phase3=pending["max_phase3"],
                        min_power_a=pending["min_power_a"],
                        min_power_b=pending["min_power_b"],
                        priority=existing.priority,
                        priority_phase1=pending["priority_phase1"],
                        priority_phase2=pending["priority_phase2"],
                        priority_phase3=pending["priority_phase3"],
                    ),
                )
                apply_view.message = await interaction.followup.send(
                    content=(
                        f"💡 **{self._zone_name}** has similar zones in this preset: "
                        f"{', '.join(siblings)}. Would you like to apply the "
                        f"same settings to these as well?"
                    ),
                    view=apply_view, ephemeral=True,
                )
            except discord.HTTPException as e:
                logger.warning(
                    "[STORM STRATEGY] apply-to-similar follow-up couldn't be "
                    "posted for zone=%s in preset=%s: %s",
                    self._zone_name, self._view.buf.name, e,
                )


class _ZoneWizardNextView(discord.ui.View):
    """One-button bridge between wizard pages. The button opens the
    next page's modal so the multi-step flow doesn't need an outer
    coordinator object."""

    def __init__(self, editor_view: "_PresetEditorView",
                 zone_name: str, *, next_page: str, label: str):
        super().__init__(timeout=300)
        self._editor = editor_view
        self._zone_name = zone_name
        self._next_page = next_page
        self.message: discord.Message | None = None

        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)

        async def _go(inter: discord.Interaction):
            if inter.user.id != editor_view.user_id:
                await inter.response.send_message(
                    "⛔ Only the editor's owner can advance the wizard.",
                    ephemeral=True,
                )
                return
            if next_page == "floors":
                await inter.response.send_modal(
                    _ZonePhaseFloorsModal(editor_view, zone_name)
                )
            elif next_page == "priority":
                await inter.response.send_modal(
                    _ZonePhasePriorityModal(editor_view, zone_name)
                )
            else:
                await inter.response.send_message(
                    f"⚠️ Unknown wizard step `{next_page}`.",
                    ephemeral=True,
                )
                return
            # Disable our button so officers don't double-click into
            # two open modals on the next page.
            for item in self.children:
                item.disabled = True
            try:
                if self.message:
                    await self.message.edit(view=self)
            except discord.HTTPException:
                pass
            self.stop()

        btn.callback = _go
        self.add_item(btn)


class _ApplyToSimilarView(discord.ui.View):
    """Follow-up view shown after a zone edit when the preset contains
    sibling zones (same building-family prefix, #149). Officer ticks
    which siblings should receive the same Max / Min / Priority and
    clicks Apply; values land via the parent editor's PresetBuffer
    and the embed refreshes."""

    def __init__(
        self, *, editor_view: "_PresetEditorView", source_zone: str,
        sibling_names: list[str], values: "ZoneRow",
    ):
        super().__init__(timeout=300)
        self._editor = editor_view
        self._source = source_zone
        self._values = values
        self.message: discord.Message | None = None
        # Default to nothing selected — officers reach for apply-to-similar
        # to opt INTO bulk copies; pre-checking every sibling would surprise
        # the cautious case where they only want one or two.
        self._selected: list[str] = []

        select = discord.ui.Select(
            placeholder="Select zones",
            min_values=0, max_values=min(len(sibling_names), 25),
            options=[
                discord.SelectOption(label=name[:100], value=name[:100])
                for name in sibling_names[:25]
            ],
        )

        async def _on_select(inter: discord.Interaction):
            if inter.user.id != self._editor.user_id:
                await inter.response.send_message(
                    "⛔ Only the officer who opened the editor can pick siblings.",
                    ephemeral=True,
                )
                return
            self._selected = list(select.values)
            # Defer silently — the choice is captured; the Apply button
            # commits. No need to re-render the message.
            try:
                await inter.response.defer()
            except discord.HTTPException:
                pass

        select.callback = _on_select
        self.add_item(select)

        apply_btn = discord.ui.Button(
            label="Apply to selected", style=discord.ButtonStyle.success,
        )

        async def _apply(inter: discord.Interaction):
            if inter.user.id != self._editor.user_id:
                await inter.response.send_message(
                    "⛔ Only the editor's owner can apply changes.",
                    ephemeral=True,
                )
                return
            if not self._selected:
                await inter.response.send_message(
                    "⚠️ Pick at least one sibling from the dropdown first, "
                    "or use Skip to dismiss.",
                    ephemeral=True,
                )
                return
            applied: list[str] = []
            for sibling in self._selected:
                self._editor.buf.upsert_zone(ZoneRow(
                    zone=sibling,
                    max_players=self._values.max_players,
                    max_phase1=self._values.max_phase1,
                    max_phase2=self._values.max_phase2,
                    max_phase3=self._values.max_phase3,
                    min_power_a=self._values.min_power_a,
                    min_power_b=self._values.min_power_b,
                    priority=self._values.priority,
                    priority_phase1=self._values.priority_phase1,
                    priority_phase2=self._values.priority_phase2,
                    priority_phase3=self._values.priority_phase3,
                ))
                applied.append(sibling)
            for item in self.children:
                item.disabled = True
            try:
                await inter.response.edit_message(
                    content=(
                        f"✅ Copied **{self._source}** settings to "
                        f"{len(applied)} sibling(s): {', '.join(applied)}."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass
            # Re-render the editor embed so the new values surface on
            # the parent view too. Best-effort — the data is already in
            # the buffer so a render failure doesn't lose work.
            try:
                if self._editor.message:
                    await self._editor.message.edit(
                        embed=_build_editor_embed(self._editor.buf, teams=self._editor.teams),
                        view=self._editor,
                    )
            except discord.HTTPException:
                pass
            self.stop()

        apply_btn.callback = _apply
        self.add_item(apply_btn)

        skip_btn = discord.ui.Button(
            label="Skip", style=discord.ButtonStyle.secondary,
        )

        async def _skip(inter: discord.Interaction):
            if inter.user.id != self._editor.user_id:
                await inter.response.send_message(
                    "⛔ Only the editor's owner can dismiss this prompt.",
                    ephemeral=True,
                )
                return
            for item in self.children:
                item.disabled = True
            try:
                await inter.response.edit_message(
                    content="OK — only the edited zone was changed.",
                    view=self,
                )
            except discord.HTTPException:
                pass
            self.stop()

        skip_btn.callback = _skip
        self.add_item(skip_btn)

    async def on_timeout(self) -> None:
        """Strip the picker on timeout so a click on a stale option
        doesn't surface 'Interaction failed'."""
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class _RenameModal(discord.ui.Modal, title="Rename Preset"):
    def __init__(self, view: "_PresetEditorView"):
        super().__init__()
        self._view = view
        self.new_name = discord.ui.TextInput(
            label="New preset name",
            default=view.buf.name,
            required=True, max_length=60,
        )
        self.add_item(self.new_name)

    async def on_submit(self, interaction: discord.Interaction):
        new = (self.new_name.value or "").strip()
        if not new:
            await interaction.response.send_message(
                "⚠️ A preset name is required.",
                ephemeral=True,
            )
            return
        # Uniqueness check excluding the current name. gspread off the
        # event loop — `list_presets` reads the whole presets tab.
        all_presets = await asyncio.to_thread(
            list_presets, self._view.guild_id, self._view.buf.event_type,
        )
        existing = [
            p.lower() for p in all_presets
            if p.lower() != self._view.buf.name.lower()
        ]
        if new.lower() in existing:
            await interaction.response.send_message(
                f"⚠️ A preset named **{new}** already exists. Pick a different name.",
                ephemeral=True,
            )
            return
        old = self._view.buf.name
        self._view.buf.name = new
        self._view.buf.dirty = True
        await self._view.refresh(interaction, message=f"✏️ Renamed **{old}** → **{new}**.")


class _PresetEditorView(discord.ui.View):
    """Editor view. State held in `self.buf`; persisted to Sheet on Save."""

    def __init__(self, guild_id: int, user_id: int, buf: PresetBuffer):
        super().__init__(timeout=900)  # 15 min — Discord's interaction token max
        self.guild_id = guild_id
        self.user_id  = user_id
        self.buf      = buf
        self.cancelled = False
        self.message: discord.Message | None = None
        # Snapshot the alliance's configured-teams choice (#148 +
        # Rule A / #166) at open time. Used by the embed renderer +
        # zone modal so single-team alliances see only their team's
        # Min Power. Resolved once rather than re-read on every modal
        # open / refresh. Applies identically to DS and CS.
        self.teams = _resolve_storm_teams(guild_id, buf.event_type)
        self._rebuild_components()

    def _rebuild_components(self):
        self.clear_items()

        # Zone selector — single-select for editing. Discord limits 25 options.
        if self.buf.zones:
            zone_select = discord.ui.Select(
                placeholder="Edit a zone…",
                min_values=1, max_values=1,
                options=[
                    discord.SelectOption(label=z.zone[:100], value=z.zone[:100])
                    for z in self.buf.zones[:25]
                ],
            )

            async def _on_select(inter: discord.Interaction):
                if inter.user.id != self.user_id:
                    await inter.response.send_message(
                        "⛔ Only the editor's owner can change this preset.",
                        ephemeral=True,
                    )
                    return
                zone_name = zone_select.values[0]
                # Phase-aware presets get the multi-step wizard since
                # all 6+ fields don't fit in one modal. Flat presets
                # use the single-modal flow.
                if self.buf.phase_count >= 2:
                    await inter.response.send_modal(
                        _ZonePhaseCapacityModal(self, zone_name)
                    )
                else:
                    await inter.response.send_modal(
                        _ZoneEditModal(self, zone_name)
                    )

            zone_select.callback = _on_select
            self.add_item(zone_select)

        # Phase-mode select (#152) — replaces the old binary toggle so
        # 3-phase CS presets can opt in. Selecting a different option
        # mutates buf.phase_count, marks dirty, and re-renders. Stored
        # phase capacities + assignments aren't touched on a mode flip
        # so an officer can move between modes without losing data.
        phase_mode_select = discord.ui.Select(
            placeholder="🔀 Phase mode",
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(
                    label="Flat (no phases)",
                    value="0",
                    description="Single per-zone slot — Max Players only.",
                    default=self.buf.phase_count == 0,
                ),
                discord.SelectOption(
                    label="2 Phases",
                    value="2",
                    description="DS-style migration: Phase 1 → Phase 2.",
                    default=self.buf.phase_count == 2,
                ),
                discord.SelectOption(
                    label="3 Phases",
                    value="3",
                    description="CS-style stages: Phase 1 → 2 → 3.",
                    default=self.buf.phase_count == 3,
                ),
            ],
        )

        async def _on_phase_mode(inter: discord.Interaction):
            if inter.user.id != self.user_id:
                await inter.response.send_message(
                    "⛔ Only the editor's owner can change this preset.",
                    ephemeral=True,
                )
                return
            new_count = int(phase_mode_select.values[0])
            if new_count == self.buf.phase_count:
                # Officer re-picked the same option — silent ack so the
                # dropdown doesn't appear frozen.
                try:
                    await inter.response.defer()
                except discord.HTTPException:
                    pass
                return
            old_count = int(self.buf.phase_count or 0)
            # Seed newly-active phase capacities from the most recent
            # populated phase so the officer doesn't have to walk every
            # zone through the wizard just to enable a new phase. Goes
            # both directions:
            #   flat → 2:   max_phase1 ← max_players, max_phase2 ← max_phase1
            #   flat → 3:   same as flat → 2, plus max_phase3 ← max_phase2
            #   2 → 3:      max_phase3 ← max_phase2 (and priorities follow)
            #   3 → 2:      no auto-clear; phase 3 data stays orphaned but
            #               doesn't render (re-toggling restores it)
            seeded = 0
            for z in self.buf.zones:
                if new_count >= 2 and int(z.max_phase1 or 0) == 0:
                    z.max_phase1 = int(z.max_players or 0)
                    if int(z.priority_phase1 or 0) == 0:
                        z.priority_phase1 = int(z.priority or 0)
                    seeded += 1
                if new_count >= 2 and int(z.max_phase2 or 0) == 0:
                    z.max_phase2 = int(z.max_phase1 or z.max_players or 0)
                    if int(z.priority_phase2 or 0) == 0:
                        z.priority_phase2 = int(
                            z.priority_phase1 or z.priority or 0
                        )
                    seeded += 1
                if new_count >= 3 and int(z.max_phase3 or 0) == 0:
                    z.max_phase3 = int(
                        z.max_phase2 or z.max_phase1 or z.max_players or 0
                    )
                    if int(z.priority_phase3 or 0) == 0:
                        z.priority_phase3 = int(
                            z.priority_phase2 or z.priority_phase1
                            or z.priority or 0
                        )
                    seeded += 1
            self.buf.phase_count = new_count
            self.buf.dirty = True
            label = "Flat" if new_count == 0 else f"{new_count}-phase"
            seeded_note = (
                f" Seeded {seeded} per-zone capacity/priority value(s) "
                f"from prior values; edit any zone to override."
                if seeded and old_count < new_count else ""
            )
            # Mode-toggle copy reframes the persistence contract from
            # "flip back any time" (vague) to "re-select the same mode"
            # (concrete) per #174 / Decision #13's polish notes.
            restore_label = (
                "Flat" if old_count == 0 else f"{old_count}-phase"
            )
            await self.refresh(
                inter,
                message=(
                    f"🔀 Switched to **{label}** mode. Capacities + "
                    f"assignments are kept. Re-select **{restore_label}** "
                    f"mode to restore without data loss." + seeded_note
                ),
            )

        phase_mode_select.callback = _on_phase_mode

        # Action buttons. Decision #13 (#174): the [➕ Add zone]
        # affordance is removed entirely — zones come from
        # DS_ZONE_STRUCTURE / CS_ZONE_STRUCTURE, which are
        # game-defined. Alliances configure max-players / minimum
        # power / priority for the canonical zones only; they don't
        # get to invent new ones.
        rename_btn = discord.ui.Button(label="✏️ Rename preset", style=discord.ButtonStyle.secondary)
        save_btn  = discord.ui.Button(
            label="💾 Save preset",
            style=discord.ButtonStyle.success,
            disabled=not self.buf.dirty,
        )
        cancel_btn = discord.ui.Button(label="🔙 Abandon this preset", style=discord.ButtonStyle.danger)

        async def _rename(inter):
            if inter.user.id != self.user_id:
                await inter.response.send_message("⛔ Only the editor's owner can change this preset.", ephemeral=True); return
            await inter.response.send_modal(_RenameModal(self))
        rename_btn.callback = _rename

        async def _save(inter):
            if inter.user.id != self.user_id:
                await inter.response.send_message("⛔ Only the editor's owner can save this preset.", ephemeral=True); return
            # Capacity over the team-size hint is normal — alliances
            # build in flex room. The editor embed already shows the
            # capacity vs. 30 line so officers can see at a glance
            # whether they're over or under; the save path doesn't
            # block on it.
            await inter.response.defer()
            ok = await asyncio.to_thread(
                save_preset, self.guild_id, self.buf.event_type, self.buf,
            )
            if ok:
                for item in self.children:
                    item.disabled = True
                msg = (
                    f"✅ Saved preset **{self.buf.name}** "
                    f"({len(self.buf.zones)} zones, capacity {self.buf.total_capacity()})."
                )
                try:
                    await inter.followup.send(msg, ephemeral=False)
                except discord.HTTPException:
                    pass
                if self.message:
                    try:
                        await self.message.edit(
                            embed=_build_editor_embed(self.buf, teams=self.teams),
                            view=self,
                        )
                    except discord.HTTPException:
                        pass
                self.stop()
            else:
                await inter.followup.send(
                    "⚠️ Could not save preset — check that your Google Sheet is configured "
                    "and that the bot has edit access. See logs for details.",
                    ephemeral=True,
                )
        save_btn.callback = _save

        async def _cancel(inter):
            if inter.user.id != self.user_id:
                await inter.response.send_message("⛔ Only the editor's owner can abandon this preset.", ephemeral=True); return
            self.cancelled = True
            for item in self.children:
                item.disabled = True
            try:
                await inter.response.edit_message(
                    content="🔙 Abandoned. Changes were not saved.",
                    embed=_build_editor_embed(self.buf, teams=self.teams),
                    view=self,
                )
            except discord.HTTPException:
                pass
            self.stop()
        cancel_btn.callback = _cancel

        self.add_item(phase_mode_select)
        self.add_item(rename_btn)
        self.add_item(save_btn)
        self.add_item(cancel_btn)

    async def refresh(self, interaction: discord.Interaction, message: str | None = None):
        self._rebuild_components()
        embed = _build_editor_embed(self.buf, teams=self.teams)
        content = message or None
        try:
            if interaction.response.is_done():
                if self.message:
                    await self.message.edit(content=content, embed=embed, view=self)
            else:
                await interaction.response.edit_message(content=content, embed=embed, view=self)
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        """Strip the editor + append the canonical timeout notice. The
        editor is posted publicly (so multiple leadership members can
        see the edit progress) with a 15-minute interaction-token
        window; without this hook, buttons silently 404 with
        'Interaction failed' after timeout."""
        from wizard_registry import expire_view_message
        parent = "desertstorm" if self.buf.event_type == "DS" else "canyonstorm"
        await expire_view_message(self.message, command_hint=f"/{parent} strategy edit")


# ── Cog + slash command groups ───────────────────────────────────────────────


async def _deny_if_not_leader(interaction: discord.Interaction) -> bool:
    """Return True iff the caller is admin/leadership. Sends the standard
    denial ephemeral on the False branch."""
    from storm_permissions import is_leader_or_admin, deny_non_leader
    if is_leader_or_admin(interaction):
        return True
    await deny_non_leader(interaction)
    return False


async def _open_editor(interaction: discord.Interaction, event_type: str, buf: PresetBuffer):
    view = _PresetEditorView(interaction.guild_id, interaction.user.id, buf)
    embed = _build_editor_embed(buf, teams=view.teams)
    await interaction.response.send_message(embed=embed, view=view)
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        view.message = None


async def open_editor_followup(
    interaction: discord.Interaction, event_type: str, buf: PresetBuffer,
):
    """Open the preset editor via the interaction's followup (rather than
    the initial response). Used by the setup wizard's `_offer_inline_create`
    branch — the button click already consumed `interaction.response`, so
    the editor has to land as a followup.
    """
    view = _PresetEditorView(interaction.guild_id, interaction.user.id, buf)
    embed = _build_editor_embed(buf, teams=view.teams)
    msg = await interaction.followup.send(embed=embed, view=view)
    view.message = msg


# ── Inline list-view actions (#169 — Rule M) ─────────────────────────────────


class _CreatePresetNameModal(discord.ui.Modal, title="Create strategy preset"):
    """Captures the new preset's name and opens the editor.

    Triggered by the list view's [➕ Create] button — same validation +
    seeding as the `/<parent> strategy create` slash command but reachable
    without leaving the list surface.
    """

    def __init__(self, event_type: str):
        super().__init__()
        self.event_type = event_type
        self.preset_name = discord.ui.TextInput(
            label="Preset name",
            placeholder="e.g. Standard Desert",
            required=True,
            max_length=60,
        )
        self.add_item(self.preset_name)

    async def on_submit(self, interaction: discord.Interaction):
        if not await _deny_if_not_leader(interaction):
            return
        name = (self.preset_name.value or "").strip()
        parent = "desertstorm" if self.event_type == "DS" else "canyonstorm"
        if not name:
            await interaction.response.send_message(
                "⚠️ Pick a preset name (e.g. `Standard Desert`).", ephemeral=True,
            )
            return
        existing = [
            p.lower() for p in await asyncio.to_thread(
                list_presets, interaction.guild_id, self.event_type,
            )
        ]
        if name.lower() in existing:
            await interaction.response.send_message(
                f"⚠️ A preset named **{name}** already exists. Use the "
                f"Edit button on the list (or "
                f"`/{parent} strategy edit name:\"{name}\"`) to modify it.",
                ephemeral=True,
            )
            return
        buf = seed_default_preset(name, self.event_type)
        buf.dirty = True
        await _open_editor(interaction, self.event_type, buf)


class _ConfirmDeleteView(discord.ui.View):
    """Confirm/cancel buttons for a delete operation. Reused by both the
    `/<parent> strategy delete` slash command and the list view's Delete
    flow."""

    def __init__(self, owner_id: int):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self.confirmed: bool | None = None
        self.message: discord.Message | None = None

    @discord.ui.button(label="🗑️ Delete preset", style=discord.ButtonStyle.danger)
    async def yes(self, inter: discord.Interaction, btn: discord.ui.Button):
        if inter.user.id != self.owner_id:
            await inter.response.send_message(
                "⛔ Only the user who ran the command can confirm.",
                ephemeral=True,
            )
            return
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, inter: discord.Interaction, btn: discord.ui.Button):
        if inter.user.id != self.owner_id:
            await inter.response.send_message(
                "⛔ Only the user who ran the command can cancel.",
                ephemeral=True,
            )
            return
        self.confirmed = False
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def _run_delete_with_confirm(
    interaction: discord.Interaction,
    event_type: str,
    name: str,
    *,
    via_followup: bool,
) -> None:
    """Confirm-then-delete sequence used by both the slash command (initial
    response) and the list-view picker (followup). The `via_followup` flag
    flips between `interaction.response.send_message` and
    `interaction.followup.send` for the confirm prompt — the rest of the
    flow uses followup either way."""
    view = _ConfirmDeleteView(interaction.user.id)
    prompt = (
        f"⚠️ Delete preset **{name}**? This removes all rows for this preset "
        f"from your Sheet. Can't be undone."
    )
    if via_followup:
        view.message = await interaction.followup.send(
            prompt, view=view, ephemeral=True,
        )
    else:
        await interaction.response.send_message(
            prompt, view=view, ephemeral=True,
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            view.message = None
    await view.wait()
    if not view.confirmed:
        await interaction.followup.send("✅ Delete cancelled.", ephemeral=True)
        return
    ok = await asyncio.to_thread(
        delete_preset, interaction.guild_id, event_type, name,
    )
    if ok:
        await interaction.followup.send(
            f"🗑️ Deleted preset **{name}**.", ephemeral=False,
        )
    else:
        await interaction.followup.send(
            f"⚠️ Couldn't find preset **{name}** to delete (or Sheet write failed).",
            ephemeral=True,
        )


class _StrategyListView(discord.ui.View):
    """Inline Create / Edit / Delete actions for `/<parent> strategy list`.

    Rule M: every list surface ends with action buttons — no dead-end
    summary that forces officers to remember the slash subcommand names.
    Empty state and populated state share the same view; Edit and Delete
    are just disabled when no presets exist.
    """

    def __init__(self, owner_id: int, event_type: str, names: list[str]):
        super().__init__(timeout=600)
        self.owner_id = owner_id
        self.event_type = event_type
        self.names = names
        self.message: discord.Message | None = None
        self._build_components()

    def _build_components(self):
        self.clear_items()

        create_btn = discord.ui.Button(
            label="➕ Create", style=discord.ButtonStyle.primary, row=0,
        )

        async def _on_create(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            await inter.response.send_modal(_CreatePresetNameModal(self.event_type))

        create_btn.callback = _on_create
        self.add_item(create_btn)

        edit_btn = discord.ui.Button(
            label="✏️ Edit", style=discord.ButtonStyle.secondary, row=0,
            disabled=not self.names,
        )

        async def _on_edit(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            picker = _PresetPickerView(
                owner_id=self.owner_id,
                event_type=self.event_type,
                names=self.names,
                action="edit",
            )
            await inter.response.send_message(
                "✏️ Pick a preset to edit.", view=picker, ephemeral=True,
            )
            try:
                picker.message = await inter.original_response()
            except discord.HTTPException:
                pass

        edit_btn.callback = _on_edit
        self.add_item(edit_btn)

        delete_btn = discord.ui.Button(
            label="🗑️ Delete", style=discord.ButtonStyle.danger, row=0,
            disabled=not self.names,
        )

        async def _on_delete(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            picker = _PresetPickerView(
                owner_id=self.owner_id,
                event_type=self.event_type,
                names=self.names,
                action="delete",
            )
            await inter.response.send_message(
                "🗑️ Pick a preset to delete.", view=picker, ephemeral=True,
            )
            try:
                picker.message = await inter.original_response()
            except discord.HTTPException:
                pass

        delete_btn.callback = _on_delete
        self.add_item(delete_btn)

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message(
                "⛔ Only the officer who ran the command can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class _PresetPickerView(discord.ui.View):
    """Ephemeral preset Select for the list view's Edit / Delete buttons.
    Action picks the destination flow — `edit` opens the editor, `delete`
    runs the confirm + delete sequence. Discord's Select option cap is 25;
    the picker shows the first 25 alphabetically and warns about overflow."""

    def __init__(
        self,
        *,
        owner_id: int,
        event_type: str,
        names: list[str],
        action: str,
    ):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.event_type = event_type
        self.action = action
        self.message: discord.Message | None = None
        self._build_components(names)

    def _build_components(self, names: list[str]):
        sorted_names = sorted(names, key=str.lower)
        capped = sorted_names[:25]
        options = [
            discord.SelectOption(label=n[:100], value=n[:100]) for n in capped
        ]
        sel = discord.ui.Select(
            placeholder=f"Pick a preset to {self.action}…",
            min_values=1, max_values=1, options=options,
        )
        sel.callback = self._make_pick_callback(sel)
        self.add_item(sel)

        cancel_btn = discord.ui.Button(
            label="↩️ Cancel", style=discord.ButtonStyle.secondary,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    def _make_pick_callback(self, sel: discord.ui.Select):
        async def _cb(inter: discord.Interaction):
            if inter.user.id != self.owner_id:
                await inter.response.send_message(
                    "⛔ Only the officer who ran the command can pick.",
                    ephemeral=True,
                )
                return
            name = sel.values[0]
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                await inter.response.edit_message(view=self)
            except discord.HTTPException:
                pass
            if self.action == "edit":
                buf = await asyncio.to_thread(
                    load_preset, inter.guild_id, self.event_type, name,
                )
                if buf is None:
                    await inter.followup.send(
                        f"⚠️ No preset named **{name}** (it may have been "
                        f"deleted in another session). Rerun the list "
                        f"command to refresh.",
                        ephemeral=True,
                    )
                    return
                await open_editor_followup(inter, self.event_type, buf)
            elif self.action == "delete":
                await _run_delete_with_confirm(
                    inter, self.event_type, name, via_followup=True,
                )
        return _cb

    async def _on_cancel(self, inter: discord.Interaction):
        if inter.user.id != self.owner_id:
            await inter.response.send_message(
                "⛔ Only the officer who ran the command can cancel.",
                ephemeral=True,
            )
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(view=self)
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


class _StrategyGroup(app_commands.Group):
    """Shared shape for DS and CS strategy slash command groups."""

    def __init__(self, *, name: str, description: str, event_type: str):
        super().__init__(name=name, description=description)
        self.event_type = event_type

    async def _create(self, interaction: discord.Interaction, name: str):
        if not await _deny_if_not_leader(interaction):
            return
        name = name.strip()
        if not name:
            await interaction.response.send_message(
                "⚠️ Pick a preset name (e.g. `Standard Desert`).", ephemeral=True,
            )
            return
        existing = [
            p.lower() for p in await asyncio.to_thread(
                list_presets, interaction.guild_id, self.event_type,
            )
        ]
        if name.lower() in existing:
            parent = "desertstorm" if self.event_type == "DS" else "canyonstorm"
            await interaction.response.send_message(
                f"⚠️ A preset named **{name}** already exists. "
                f"Use `/{parent} strategy edit name:\"{name}\"` to modify it.",
                ephemeral=True,
            )
            return
        buf = seed_default_preset(name, self.event_type)
        buf.dirty = True
        await _open_editor(interaction, self.event_type, buf)

    async def _edit(self, interaction: discord.Interaction, name: str):
        if not await _deny_if_not_leader(interaction):
            return
        buf = await asyncio.to_thread(
            load_preset, interaction.guild_id, self.event_type, name,
        )
        if buf is None:
            await interaction.response.send_message(
                f"⚠️ No preset named **{name}**. Use the list command to see saved presets.",
                ephemeral=True,
            )
            return
        await _open_editor(interaction, self.event_type, buf)

    async def _list(self, interaction: discord.Interaction):
        if not await _deny_if_not_leader(interaction):
            return
        names = await asyncio.to_thread(
            list_presets, interaction.guild_id, self.event_type,
        )
        label = "Desert Storm" if self.event_type == "DS" else "Canyon Storm"
        if not names:
            description = (
                f"*No {label} strategy presets saved yet.* Click **➕ Create** "
                f"below to make one."
            )
        else:
            description = "\n".join(f"• **{n}**" for n in names)
        embed = discord.Embed(
            title=f"📋 {label} — Strategy Presets",
            description=description,
            color=discord.Color.blurple(),
        )
        view = _StrategyListView(
            owner_id=interaction.user.id,
            event_type=self.event_type,
            names=names,
        )
        await interaction.response.send_message(embed=embed, view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            view.message = None

    async def _delete(self, interaction: discord.Interaction, name: str):
        if not await _deny_if_not_leader(interaction):
            return
        await _run_delete_with_confirm(
            interaction, self.event_type, name, via_followup=False,
        )


def build_ds_strategy_group() -> _StrategyGroup:
    grp = _StrategyGroup(
        name="strategy",
        description="Manage Desert Storm strategy presets",
        event_type="DS",
    )

    @grp.command(name="create", description="Create a new DS strategy preset")
    @app_commands.describe(name="A short name for the preset (e.g. 'Standard Desert')")
    async def create(interaction: discord.Interaction, name: str):
        await grp._create(interaction, name)

    @grp.command(name="edit", description="Edit an existing DS strategy preset")
    @app_commands.describe(name="The saved preset to open")
    async def edit(interaction: discord.Interaction, name: str):
        await grp._edit(interaction, name)

    @grp.command(name="list", description="List saved DS strategy presets")
    async def listing(interaction: discord.Interaction):
        await grp._list(interaction)

    @grp.command(name="delete", description="Delete a DS strategy preset")
    @app_commands.describe(name="The saved preset to delete")
    async def delete(interaction: discord.Interaction, name: str):
        await grp._delete(interaction, name)

    @grp.command(name="apply",
                 description="Open the manual roster builder for a saved DS preset")
    @app_commands.describe(name="The preset to apply (use the list command to see saved presets)")
    async def apply(interaction: discord.Interaction, name: str):
        # Late-bound to break load-order coupling between
        # storm_strategy and storm_roster_builder; same pattern the
        # audit used between storm_member_rules and storm_strategy.
        from storm_roster_builder import open_roster_builder
        await open_roster_builder(interaction, "DS", name.strip())

    @grp.command(name="roster_history",
                 description="Browse past DS rosters with attendance overlaid")
    @app_commands.describe(
        date="Optional — show a specific date (May 18, 5/18, 2026-05-18, yesterday). Omit to list recent events.",
    )
    async def ds_history(interaction: discord.Interaction, date: str | None = None):
        from storm_history import open_history
        await open_history(interaction, "DS", date)

    return grp


def build_cs_strategy_group() -> _StrategyGroup:
    grp = _StrategyGroup(
        name="strategy",
        description="Manage Canyon Storm strategy presets",
        event_type="CS",
    )

    @grp.command(name="create", description="Create a new CS strategy preset")
    @app_commands.describe(name="A short name for the preset (e.g. 'Rulebringers Plan')")
    async def create(interaction: discord.Interaction, name: str):
        await grp._create(interaction, name)

    @grp.command(name="edit", description="Edit an existing CS strategy preset")
    @app_commands.describe(name="The saved preset to open")
    async def edit(interaction: discord.Interaction, name: str):
        await grp._edit(interaction, name)

    @grp.command(name="list", description="List saved CS strategy presets")
    async def listing(interaction: discord.Interaction):
        await grp._list(interaction)

    @grp.command(name="delete", description="Delete a CS strategy preset")
    @app_commands.describe(name="The saved preset to delete")
    async def delete(interaction: discord.Interaction, name: str):
        await grp._delete(interaction, name)

    @grp.command(name="apply",
                 description="Open the manual roster builder for a saved CS preset")
    @app_commands.describe(name="The preset to apply (use the list command to see saved presets)")
    async def apply(interaction: discord.Interaction, name: str):
        from storm_roster_builder import open_roster_builder
        await open_roster_builder(interaction, "CS", name.strip())

    @grp.command(name="roster_history",
                 description="Browse past CS rosters with attendance overlaid")
    @app_commands.describe(
        date="Optional — show a specific date (May 18, 5/18, 2026-05-18, yesterday). Omit to list recent events.",
    )
    async def cs_history(interaction: discord.Interaction, date: str | None = None):
        from storm_history import open_history
        await open_history(interaction, "CS", date)

    return grp


# The strategy groups are registered by `storm_commands_root` as subgroups
# under the `/desertstorm` and `/canyonstorm` parents. This module exposes
# `build_ds_strategy_group` / `build_cs_strategy_group` for that root cog
# to call; no slash commands are registered here directly.
