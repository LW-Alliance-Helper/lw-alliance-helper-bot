"""
storm_strategy.py — data/I/O layer for Desert Storm and Canyon Storm
strategy presets (#126), split from the Discord UI in #371.

Alliances define named "presets" — saved zone layouts with capacities,
per-team power floors, and priorities. The same layout gets re-used
each week instead of being hand-built.

Storage shape (Sheet, alliance-owned, source of truth):

  DS Strategies columns:
    Preset Name | Zone | Max Players | Min Power A | Min Power B | Priority

  CS Strategies columns:
    Preset Name | Zone | Max Players | Min Power A | Min Power B | Priority | Faction

Preset names are unique per (guild, event_type). Rows for one preset
share the Preset Name value.

The editor UI (view classes, modals, the slash-command entry points) lives
in storm_strategy_ui.py, which imports this module as `ss` for the data
layer. Nothing in this file touches discord.py -- ZoneRow, PresetBuffer,
and the load/save/list/delete Sheet I/O are all plain data, independently
testable and reusable (api/routes/sheets.py's MM-facing endpoints use them
directly without pulling in any Discord UI).
"""

from __future__ import annotations

import logging
import re

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
                value,
                source,
            )
        return 0, True
    return parsed, False


# ── Preset data model ────────────────────────────────────────────────────────
#
# A preset is a list of ZoneRow entries plus a name + (CS only) faction.
# Stored on disk as Sheet rows; buffered in memory during editing.


class ZoneRow:
    """One zone in a strategy preset. Same shape for DS and CS. Both
    event types carry per-team power floors (`min_power_a` /
    `min_power_b`); single-team alliances simply leave the unused
    field at 0.

    Phase fields (`max_phase1..3`, `priority_phase1..3`) are only read
    when the parent `PresetBuffer.phase_count >= 2` (see #152). On
    flat presets they default to 0 and are ignored by the renderer +
    mail builder. The `priority` field is the single-mode (flat)
    priority; in phase-aware mode each phase has its own priority.
    """

    __slots__ = (
        "zone",
        "max_players",
        "max_phase1",
        "max_phase2",
        "max_phase3",
        "min_power_a",
        "min_power_b",
        "priority",
        "priority_phase1",
        "priority_phase2",
        "priority_phase3",
    )

    def __init__(
        self,
        zone: str,
        max_players: int = 0,
        max_phase1: int = 0,
        max_phase2: int = 0,
        max_phase3: int = 0,
        min_power_a: int = 0,
        min_power_b: int = 0,
        priority: int = 0,
        priority_phase1: int = 0,
        priority_phase2: int = 0,
        priority_phase3: int = 0,
    ):
        self.zone = zone
        self.max_players = _safe_int(max_players)
        self.max_phase1 = _safe_int(max_phase1)
        self.max_phase2 = _safe_int(max_phase2)
        self.max_phase3 = _safe_int(max_phase3)
        self.min_power_a = _safe_int(min_power_a)
        self.min_power_b = _safe_int(min_power_b)
        self.priority = _safe_int(priority)
        self.priority_phase1 = _safe_int(priority_phase1)
        self.priority_phase2 = _safe_int(priority_phase2)
        self.priority_phase3 = _safe_int(priority_phase3)

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

    def render_line(self, event_type: str, teams: str = "both", phase_count: int = 0) -> str:
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
                f"Min A: {format_power(self.min_power_a)} · Min B: {format_power(self.min_power_b)}"
            )

        from storm_icons import zone_emoji_prefix

        icon = zone_emoji_prefix(self.zone)  # "" until #158 emojis upload

        if phase_count >= 2:
            # Per-zone-per-phase rendering: header line with the zone +
            # team minimums (which are per-team, not per-phase, so they
            # belong on the header), then one row per phase showing
            # capacity and any non-zero per-phase priority.
            header = f"• {icon}**{self.zone}**: {mins}"
            phase_lines: list[str] = []
            phase_prios = [
                self.priority_phase1,
                self.priority_phase2,
                self.priority_phase3,
            ][:phase_count]
            phase_caps = [
                self.max_phase1,
                self.max_phase2,
                self.max_phase3,
            ][:phase_count]
            for idx, (cap, prio) in enumerate(zip(phase_caps, phase_prios), start=1):
                prio_suffix = f" (priority {prio})" if prio else ""
                phase_lines.append(f"   └ Stage {idx}: cap {cap}{prio_suffix}")
            return "\n".join([header] + phase_lines)

        # Flat preset — single-line shape unchanged from pre-#172.
        cap = f"Max: {self.max_players}"
        prio = f" [P{self.priority}]" if self.priority else ""
        return f"• {icon}{self.zone:<20} ({cap})  {mins}{prio}"


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

    def __init__(
        self,
        name: str,
        event_type: str,
        zones: list[ZoneRow] | None = None,
        faction: str = "Either",
        phase_count: int = 0,
        uses_phases: bool | None = None,
    ):
        self.name = name
        self.event_type = event_type.upper()
        self.zones = list(zones or [])
        self.faction = faction  # CS only; ignored for DS
        # Back-compat: pre-3-phase code passed `uses_phases=True` for
        # 2-phase presets. Translate it if the caller didn't also pass
        # an explicit phase_count.
        if uses_phases and not phase_count:
            phase_count = 2
        self.phase_count = int(phase_count) if int(phase_count) in self._VALID_PHASE_COUNTS else 0
        self.dirty = False  # tracks unsaved changes for the banner

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
            existing.max_players = row.max_players
            existing.max_phase1 = row.max_phase1
            existing.max_phase2 = row.max_phase2
            existing.max_phase3 = row.max_phase3
            existing.min_power_a = row.min_power_a
            existing.min_power_b = row.min_power_b
            existing.priority = row.priority
            existing.priority_phase1 = row.priority_phase1
            existing.priority_phase2 = row.priority_phase2
            existing.priority_phase3 = row.priority_phase3
        self.dirty = True

    def remove_zone(self, zone_name: str) -> bool:
        before = len(self.zones)
        self.zones = [z for z in self.zones if z.zone.lower() != zone_name.lower()]
        if len(self.zones) != before:
            self.dirty = True
            return True
        return False


def canonical_zones_for(event_type: str) -> list[str]:
    """Canonical zone DISPLAY NAMES per event type (#35 + #178).

    DS is a flat list of distinct zones — return as-is.

    CS's `CS_ZONE_STRUCTURE` is `[(stage_num, internal_key, display_name)]`
    where the same building can appear across stages (e.g. Power Tower
    in Stage 1 AND Stage 3 → `s1_power_tower` + `s3_power_tower` both
    surfacing as "Power Tower"). Pre-#178 this helper returned the
    internal keys, which leaked into the autocomplete dropdown +
    seed_default_preset + InlinePowerBandView's Zone Select as
    `s1_power_tower`-style strings that don't match what officers see
    anywhere else in the UI.

    Post-#178: return DEDUPED display names. The preset model carries
    per-phase capacities (`max_phase1` / `max_phase2` / `max_phase3`)
    so one "Power Tower" ZoneRow with per-phase data subsumes the two
    pre-#178 entries cleanly.
    """
    import storm

    if event_type == "DS":
        return list(storm.DS_ZONE_STRUCTURE)
    # Dedupe by display name, preserving insertion order (Stage 1 entries
    # come first; later-stage repeats are dropped).
    seen: dict[str, None] = {}
    for _, _key, display in storm.CS_ZONE_STRUCTURE:
        if display not in seen:
            seen[display] = None
    return list(seen.keys())


# Pre-#178 alliances stored CS preset Zone cells (and `per_member`
# zone-rule Value cells) as internal keys like `s1_power_tower`. Post-
# #178 the canonical form is display names. This translation map +
# helper let the readers fall through legacy values to the new form
# at load time so existing dev-staged data keeps working without a
# Sheet rewrite. Built from `CS_ZONE_STRUCTURE` lazily so the import
# stays one-way (storm_strategy → storm; not back).
_LEGACY_CS_ZONE_TRANSLATION: dict[str, str] | None = None


def _legacy_cs_zone_translation() -> dict[str, str]:
    """`{internal_key: display_name}` for every CS zone. Cached on first
    call. Lower-cased keys for case-insensitive lookups in
    `_translate_legacy_cs_zone`."""
    global _LEGACY_CS_ZONE_TRANSLATION
    if _LEGACY_CS_ZONE_TRANSLATION is None:
        import storm

        _LEGACY_CS_ZONE_TRANSLATION = {
            key.lower(): display for _, key, display in storm.CS_ZONE_STRUCTURE
        }
    return _LEGACY_CS_ZONE_TRANSLATION


def _translate_legacy_cs_zone(name: str) -> str:
    """If `name` matches a CS internal key (`s1_power_tower` etc.),
    return the canonical display name (`Power Tower`). Otherwise
    return `name` unchanged.

    Pre-#178 alliance Sheet data may carry internal keys in the Zone
    column on `strategies_tab` and in the Value column of per_member
    zone rules. This helper is the single translation point at every
    read boundary — `load_preset` for CS presets, `list_rules` for CS
    per-member zone rules. Once the translated data is re-saved (next
    Save Preset or set_member_zone), the Sheet carries the canonical
    display name and the translator becomes a no-op for that row.
    """
    if not name:
        return name
    return _legacy_cs_zone_translation().get(name.strip().lower(), name)


def seed_default_preset(name: str, event_type: str) -> PresetBuffer:
    """Build a fresh preset buffer pre-populated with canonical zones."""
    zones = [ZoneRow(zone=name, max_players=0) for name in canonical_zones_for(event_type)]
    return PresetBuffer(name=name, event_type=event_type, zones=zones)


# ── Sheet I/O ────────────────────────────────────────────────────────────────


_DS_HEADER = [
    "Preset Name",
    "Zone",
    "Max Players",
    "Max Stage 1",
    "Max Stage 2",
    "Max Stage 3",
    "Min Power A",
    "Min Power B",
    "Priority",
    "Priority Stage 1",
    "Priority Stage 2",
    "Priority Stage 3",
    "Stage Count",
]
_CS_HEADER = [
    "Preset Name",
    "Zone",
    "Max Players",
    "Max Stage 1",
    "Max Stage 2",
    "Max Stage 3",
    "Min Power A",
    "Min Power B",
    "Priority",
    "Priority Stage 1",
    "Priority Stage 2",
    "Priority Stage 3",
    "Faction",
    "Stage Count",
]

# Truthy strings the legacy `Use Phases` column might carry. Used only
# to read pre-3-phase preset data — new writes always use the
# `Phase Count` int column.
_TRUE_STRINGS = {"true", "yes", "1", "y", "on", "phases"}


def _parse_phase_count(row: dict) -> int:
    """Resolve a row's phase_count from the new Stage Count column,
    falling back to the legacy Use Phases boolean if Stage Count is
    missing. Unknown / unparseable values clamp to 0 (flat)."""
    raw = row.get("Stage Count", "")
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
    return cfg.get("strategies_tab") or config.default_structured_tab(event_type, "strategies_tab")


def _get_or_create_strategies_worksheet(guild_id: int, event_type: str):
    """Returns the worksheet, creating it (with header row) if missing.
    Returns None if the guild has no Sheet configured (or `gspread`
    raised opening it — unconfigured / bad creds / deleted spreadsheet)."""
    import config

    # `config.get_spreadsheet` raises rather than returning None for
    # unconfigured guilds. Catch broadly so the strategy preset surface
    # doesn't die with an unhandled traceback on a guild that hasn't
    # run setup.
    try:
        sh = config.get_spreadsheet(guild_id)
    except Exception as e:
        logger.warning(
            "[STORM STRATEGY] get_spreadsheet failed for guild=%s: %s",
            guild_id,
            e,
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
        sh,
        tab_name,
        header_row=header,
        rows=1000,
        cols=max(8, len(header)),
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
        logger.warning(
            "[STORM STRATEGY] load_preset failed for guild=%s event=%s name=%s: %s",
            guild_id,
            event_type,
            name,
            e,
        )
        return None
    rows = [r for r in records if str(r.get("Preset Name", "")).strip().lower() == name.lower()]
    if not rows:
        return None
    # CS legacy-key migration (#178): existing dev/staging alliances
    # carry rows like `s1_power_tower`/`s3_power_tower` that both
    # surface as the same display name "Power Tower". Translate per
    # row at read time, then merge per-display-name rows by taking the
    # max of every numeric field so the per-phase data from the old
    # multi-row shape collapses cleanly into one ZoneRow.
    zones: list[ZoneRow] = []
    zones_by_name: dict[str, ZoneRow] = {}  # key = display name lower-case
    faction = "Either"
    phase_count = 0
    for r in rows:
        raw_zone = str(r.get("Zone", "")).strip()
        if event_type == "CS":
            zone_name = _translate_legacy_cs_zone(raw_zone)
        else:
            zone_name = raw_zone
        src = f"preset={name!r} zone={zone_name!r} event={event_type}"
        # `Phase Count` (or legacy `Use Phases`) is denormalised across
        # every row of a preset. Take the max seen so partial-edit
        # states still resolve coherently.
        phase_count = max(phase_count, _parse_phase_count(r))
        if event_type == "DS":
            min_a, _ = _parse_power_cell(r.get("Min Power A", ""), source=src + " col=Min Power A")
            min_b, _ = _parse_power_cell(r.get("Min Power B", ""), source=src + " col=Min Power B")
            zr = ZoneRow(
                zone=zone_name,
                max_players=_safe_int(r.get("Max Players", 0)),
                max_phase1=_safe_int(r.get("Max Stage 1", 0)),
                max_phase2=_safe_int(r.get("Max Stage 2", 0)),
                max_phase3=_safe_int(r.get("Max Stage 3", 0)),
                min_power_a=min_a,
                min_power_b=min_b,
                priority=_safe_int(r.get("Priority", 0)),
                priority_phase1=_safe_int(r.get("Priority Stage 1", 0)),
                priority_phase2=_safe_int(r.get("Priority Stage 2", 0)),
                priority_phase3=_safe_int(r.get("Priority Stage 3", 0)),
            )
            zones.append(zr)
        else:
            # CS used to carry a single `Min Power` column before per-
            # team floors landed; fall back to that legacy cell when
            # the new `Min Power A` column is absent so existing
            # alliance sheets keep their saved data.
            if r.get("Min Power A", "") != "":
                min_a, _ = _parse_power_cell(
                    r.get("Min Power A", ""), source=src + " col=Min Power A"
                )
            else:
                min_a, _ = _parse_power_cell(r.get("Min Power", ""), source=src + " col=Min Power")
            min_b, _ = _parse_power_cell(r.get("Min Power B", ""), source=src + " col=Min Power B")
            zr = ZoneRow(
                zone=zone_name,
                max_players=_safe_int(r.get("Max Players", 0)),
                max_phase1=_safe_int(r.get("Max Stage 1", 0)),
                max_phase2=_safe_int(r.get("Max Stage 2", 0)),
                max_phase3=_safe_int(r.get("Max Stage 3", 0)),
                min_power_a=min_a,
                min_power_b=min_b,
                priority=_safe_int(r.get("Priority", 0)),
                priority_phase1=_safe_int(r.get("Priority Stage 1", 0)),
                priority_phase2=_safe_int(r.get("Priority Stage 2", 0)),
                priority_phase3=_safe_int(r.get("Priority Stage 3", 0)),
            )
            # Merge-on-load: when two legacy rows translate to the
            # same display name, fold the second into the first by
            # taking max() of every numeric field. The pre-#178 model
            # stored separate rows for `s1_power_tower` (Phase 1 data)
            # and `s3_power_tower` (Phase 3 data); after translation
            # both surface as "Power Tower" and need to land in one
            # ZoneRow that carries BOTH phases' caps + priorities.
            key = zone_name.strip().lower()
            existing = zones_by_name.get(key)
            if existing is None:
                zones_by_name[key] = zr
                zones.append(zr)
            else:
                existing.max_players = max(existing.max_players, zr.max_players)
                existing.max_phase1 = max(existing.max_phase1, zr.max_phase1)
                existing.max_phase2 = max(existing.max_phase2, zr.max_phase2)
                existing.max_phase3 = max(existing.max_phase3, zr.max_phase3)
                existing.min_power_a = max(existing.min_power_a, zr.min_power_a)
                existing.min_power_b = max(existing.min_power_b, zr.min_power_b)
                existing.priority = max(existing.priority, zr.priority)
                existing.priority_phase1 = max(existing.priority_phase1, zr.priority_phase1)
                existing.priority_phase2 = max(existing.priority_phase2, zr.priority_phase2)
                existing.priority_phase3 = max(existing.priority_phase3, zr.priority_phase3)
            row_faction = str(r.get("Faction", "")).strip()
            if row_faction:
                faction = row_faction
    return PresetBuffer(
        name=name, event_type=event_type, zones=zones, faction=faction, phase_count=phase_count
    )


def list_presets(guild_id: int, event_type: str) -> list[str]:
    """Return preset names defined for this guild + event type."""
    ws = _get_or_create_strategies_worksheet(guild_id, event_type)
    if ws is None:
        return []
    try:
        records = ws.get_all_records()
    except Exception as e:
        logger.warning(
            "[STORM STRATEGY] list_presets failed for guild=%s event=%s: %s",
            guild_id,
            event_type,
            e,
        )
        return []
    seen: dict[str, None] = {}
    for r in records:
        name = str(r.get("Preset Name", "")).strip()
        if name and name not in seen:
            seen[name] = None
    return list(seen)


def list_strategies(guild_id: int, event_type: str) -> list[dict]:
    """Named strategies for MM's planner dropdown (PHASE8 §4).

    The bot keys presets by name, so the stable `id` MM stores and passes back is
    the preset name (renaming a preset invalidates a stored selection — MM
    re-fetches and the officer re-picks). Returns `[{ id, name }]`, or `[]` when
    none are configured. Never raises.
    """
    try:
        names = list_presets(guild_id, event_type)
    except Exception as e:  # noqa: BLE001 — never break the API on a strategy read
        logger.warning(
            "[STORM STRATEGY] list_strategies failed guild=%s event=%s: %s",
            guild_id,
            event_type,
            e,
        )
        return []
    return [{"id": n, "name": n} for n in names]


def zone_rules_for(guild_id: int, event_type: str, strategy_id: str | None = None) -> list[dict]:
    """Per-zone rules for MM's planner overlay (PHASE8 §4, display only).

    Resolves `strategy_id` (the preset name) to a saved strategy preset;
    omitted/blank falls back to the first preset. The bot does NOT gate on these;
    MM overlays them per zone and owns the leadership-only + export-exclusion
    behaviour, so the bot just serves the rules. Returns
    `[{ zone, min_a, min_b, min_players, max_players, priority }]` for each zone
    with any rule set, or `[]` when nothing resolves.

    `min_a` / `min_b` are the per-team power floors (single-team alliances leave
    one at 0). **`min_players` is always 0** — the bot's strategy model has no
    per-zone minimum player count, only a `max_players` cap. Phase-aware presets
    fall back to the largest phase cap / first non-zero phase priority for the
    flat `max_players` / `priority` MM v1 consumes. Never raises.
    """
    try:
        name = (strategy_id or "").strip()
        if not name:
            names = list_presets(guild_id, event_type)
            if not names:
                return []
            name = names[0]
        preset = load_preset(guild_id, event_type, name)
        if preset is None:
            return []
        rules: list[dict] = []
        for z in preset.zones:
            min_a = int(getattr(z, "min_power_a", 0) or 0)
            min_b = int(getattr(z, "min_power_b", 0) or 0)
            max_players = int(getattr(z, "max_players", 0) or 0)
            if not max_players:  # phase-aware preset: caps live per phase
                max_players = max(
                    int(getattr(z, "max_phase1", 0) or 0),
                    int(getattr(z, "max_phase2", 0) or 0),
                    int(getattr(z, "max_phase3", 0) or 0),
                )
            priority = int(getattr(z, "priority", 0) or 0)
            if not priority:  # phase-aware preset: first non-zero per-phase
                priority = next(
                    (
                        p
                        for p in (
                            int(getattr(z, "priority_phase1", 0) or 0),
                            int(getattr(z, "priority_phase2", 0) or 0),
                            int(getattr(z, "priority_phase3", 0) or 0),
                        )
                        if p
                    ),
                    0,
                )
            if not (min_a or min_b or max_players or priority):
                continue
            rules.append(
                {
                    "zone": z.zone,
                    "min_a": min_a,
                    "min_b": min_b,
                    "min_players": 0,  # bot has no per-zone minimum player count
                    "max_players": max_players,
                    "priority": priority,
                }
            )
        return rules
    except Exception as e:  # noqa: BLE001 — never break the API on a strategy read
        logger.warning(
            "[STORM STRATEGY] zone_rules_for failed guild=%s event=%s: %s",
            guild_id,
            event_type,
            e,
        )
        return []


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
        logger.warning(
            "[STORM STRATEGY] save_preset read-back failed for guild=%s event=%s: %s",
            guild_id,
            event_type,
            e,
        )
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
        round-trips into the new `Stage Count` column on the next
        save. Legacy CS `Min Power` (single column) maps onto the
        new `Min Power A` slot — `Min Power B` defaults to empty
        until leadership sets a Team B floor."""
        out: list[str] = []
        legacy_uses_phases = (
            _parse_uses_phases(row[old_header_idx["Use Phases"]])
            if "Use Phases" in old_header_idx and old_header_idx["Use Phases"] < len(row)
            else False
        )
        for col_name in header:
            if col_name == "Stage Count" and "Stage Count" not in old_header_idx:
                out.append("2" if legacy_uses_phases else "0")
                continue
            if (
                col_name == "Min Power A"
                and "Min Power A" not in old_header_idx
                and "Min Power" in old_header_idx
            ):
                legacy_idx = old_header_idx["Min Power"]
                if 0 <= legacy_idx < len(row):
                    out.append(str(row[legacy_idx]))
                else:
                    out.append("")
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
            kept.append(
                [
                    buf.name,
                    z.zone,
                    str(z.max_players),
                    str(z.max_phase1),
                    str(z.max_phase2),
                    str(z.max_phase3),
                    str(z.min_power_a),
                    str(z.min_power_b),
                    str(z.priority),
                    str(z.priority_phase1),
                    str(z.priority_phase2),
                    str(z.priority_phase3),
                    phase_count_cell,
                ]
            )
        else:
            kept.append(
                [
                    buf.name,
                    z.zone,
                    str(z.max_players),
                    str(z.max_phase1),
                    str(z.max_phase2),
                    str(z.max_phase3),
                    str(z.min_power_a),
                    str(z.min_power_b),
                    str(z.priority),
                    str(z.priority_phase1),
                    str(z.priority_phase2),
                    str(z.priority_phase3),
                    buf.faction,
                    phase_count_cell,
                ]
            )

    try:
        ws.clear()
        ws.update("A1", kept, value_input_option="RAW")
    except Exception as e:
        logger.warning(
            "[STORM STRATEGY] save_preset write failed for guild=%s event=%s name=%s: %s",
            guild_id,
            event_type,
            buf.name,
            e,
        )
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
        logger.warning(
            "[STORM STRATEGY] delete_preset read failed for guild=%s event=%s: %s",
            guild_id,
            event_type,
            e,
        )
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
        logger.warning(
            "[STORM STRATEGY] delete_preset write failed for guild=%s event=%s name=%s: %s",
            guild_id,
            event_type,
            name,
            e,
        )
        return False
    return True
