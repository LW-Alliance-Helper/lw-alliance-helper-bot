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

In-Discord editor flow:
  /desert_storm strategy create name:"…"  → opens editor seeded with
                                            canonical DS zones
  /desert_storm strategy edit name:"…"    → loads from Sheet → editor
  /desert_storm strategy list             → embed of saved presets
  /desert_storm strategy delete name:"…"  → confirm → remove

Editor state is buffered in memory on the View. Discord's interaction
token expires after 15 minutes; that's a natural session bound, so no
SQLite session table is needed for v1.
"""

from __future__ import annotations

import logging
import re

import discord
from discord import app_commands
from discord.ext import commands

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
    `min_power_a` for code simplicity)."""

    __slots__ = ("zone", "max_players", "min_power_a", "min_power_b", "priority")

    def __init__(self, zone: str, max_players: int = 0,
                 min_power_a: int = 0, min_power_b: int = 0,
                 priority: int = 0):
        self.zone        = zone
        self.max_players = _safe_int(max_players)
        self.min_power_a = _safe_int(min_power_a)
        self.min_power_b = _safe_int(min_power_b)
        self.priority    = _safe_int(priority)

    def render_line(self, event_type: str, teams: str = "both") -> str:
        """One-line summary for the editor embed. DS rendering respects
        the alliance's configured teams (#148) so single-team alliances
        see only their team's floor."""
        prio = f" [P{self.priority}]" if self.priority else ""
        if event_type == "CS":
            return (
                f"• {self.zone:<20} (Max: {self.max_players})  "
                f"Min: {format_power(self.min_power_a)}{prio}"
            )
        if teams == "A":
            return (
                f"• {self.zone:<20} (Max: {self.max_players})  "
                f"Min: {format_power(self.min_power_a)}{prio}"
            )
        if teams == "B":
            return (
                f"• {self.zone:<20} (Max: {self.max_players})  "
                f"Min: {format_power(self.min_power_b)}{prio}"
            )
        return (
            f"• {self.zone:<20} (Max: {self.max_players})  "
            f"Min A: {format_power(self.min_power_a)} · "
            f"Min B: {format_power(self.min_power_b)}{prio}"
        )


class PresetBuffer:
    """Mutable preset state held by the editor view. Persists to Sheet on
    Save Preset."""

    def __init__(self, name: str, event_type: str,
                 zones: list[ZoneRow] | None = None,
                 faction: str = "Either"):
        self.name       = name
        self.event_type = event_type.upper()
        self.zones      = list(zones or [])
        self.faction    = faction  # CS only; ignored for DS
        self.dirty      = False    # tracks unsaved changes for the banner

    def total_capacity(self) -> int:
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
            existing.min_power_a = row.min_power_a
            existing.min_power_b = row.min_power_b
            existing.priority    = row.priority
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
              "Min Power A", "Min Power B", "Priority"]
_CS_HEADER = ["Preset Name", "Zone", "Max Players",
              "Min Power", "Priority", "Faction"]

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
    # unconfigured guilds. Catch broadly so /ds_strategy commands don't
    # die with an unhandled traceback on a guild that hasn't run setup.
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
    try:
        ws = sh.worksheet(tab_name)
    except Exception:
        ws = sh.add_worksheet(title=tab_name, rows=1000, cols=max(8, len(header)))
        ws.append_row(header, value_input_option="RAW")
    return ws


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
    for r in rows:
        zone_name = str(r.get("Zone", "")).strip()
        src = f"preset={name!r} zone={zone_name!r} event={event_type}"
        if event_type == "DS":
            min_a, _ = _parse_power_cell(r.get("Min Power A", ""), source=src + " col=Min Power A")
            min_b, _ = _parse_power_cell(r.get("Min Power B", ""), source=src + " col=Min Power B")
            zones.append(ZoneRow(
                zone=zone_name,
                max_players=_safe_int(r.get("Max Players", 0)),
                min_power_a=min_a,
                min_power_b=min_b,
                priority=_safe_int(r.get("Priority", 0)),
            ))
        else:
            min_p, _ = _parse_power_cell(r.get("Min Power", ""), source=src + " col=Min Power")
            zones.append(ZoneRow(
                zone=zone_name,
                max_players=_safe_int(r.get("Max Players", 0)),
                min_power_a=min_p,
                min_power_b=0,
                priority=_safe_int(r.get("Priority", 0)),
            ))
            row_faction = str(r.get("Faction", "")).strip()
            if row_faction:
                faction = row_faction
    return PresetBuffer(name=name, event_type=event_type, zones=zones, faction=faction)


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
    # Filter: keep header + non-matching rows.
    kept = [header]
    for row in all_values[1:]:  # skip existing header row
        if not row:
            continue
        if str(row[0]).strip().lower() != buf.name.lower():
            kept.append(row)
    # Append buffer rows.
    for z in buf.zones:
        if event_type == "DS":
            kept.append([
                buf.name, z.zone, str(z.max_players),
                str(z.min_power_a), str(z.min_power_b), str(z.priority),
            ])
        else:
            kept.append([
                buf.name, z.zone, str(z.max_players),
                str(z.min_power_a), str(z.priority), buf.faction,
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


def _resolve_ds_teams(guild_id: int) -> str:
    """Read the alliance's configured DS teams ('both' | 'A' | 'B') from
    `guild_storm_config`. Falls back to 'both' on a missing row or
    config-read failure — that's the historical behaviour, so the gate
    is invisible to alliances that haven't run setup since #148."""
    try:
        import config
        saved = (config.get_storm_config(int(guild_id), "DS") or {}).get("teams") or "both"
    except Exception:
        return "both"
    return saved if saved in ("both", "A", "B") else "both"


def _build_editor_embed(buf: PresetBuffer, team_size_hint: int = _TEAM_SIZE_HINT,
                        *, teams: str = "both") -> discord.Embed:
    label = "Desert Storm" if buf.event_type == "DS" else "Canyon Storm"
    title = f"🛡️ Editing Preset: {buf.name}"
    desc_lines = [f"🗺️ Event: {label}"]
    if buf.event_type == "DS" and teams in ("A", "B"):
        # Surface the gate on the embed too — without this, an officer
        # opening a single-team preset would see only one floor in the
        # rows and wonder if their setup is broken.
        desc_lines.append(f"👥 Teams: **Team {teams} only** (floors shown match)")
    if buf.event_type == "CS":
        desc_lines.append(f"⚙️ Faction: {buf.faction}")
    desc_lines.append("")
    if buf.zones:
        desc_lines.append("📋 **Zones:**")
        for z in buf.zones:
            desc_lines.append(z.render_line(buf.event_type, teams=teams))
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
        desc_lines.append("⚠️ *Unsaved changes — hit Save Preset to commit.*")
    return discord.Embed(
        title=title,
        description="\n".join(desc_lines),
        color=discord.Color.blurple(),
    )


class _ZoneEditModal(discord.ui.Modal):
    """Modal for editing one zone's max/min/priority. Branches DS vs CS
    on field count, and on DS branches further on whether the alliance
    has both teams configured (#148) — Team A-only or Team B-only
    alliances see only the floor that matters."""

    def __init__(self, view: "_PresetEditorView", zone_name: str):
        super().__init__(title=f"Edit Zone: {zone_name}"[:45])
        self._view = view
        self._zone_name = zone_name
        existing = view.buf.find_zone(zone_name) or ZoneRow(zone=zone_name)

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
            getattr(view, "teams", "both") if view.buf.event_type == "DS" else "both"
        )

        if view.buf.event_type == "DS":
            self.power_a_input = None
            self.power_b_input = None
            if self._teams in ("both", "A"):
                self.power_a_input = discord.ui.TextInput(
                    label="Min Power Team A",
                    placeholder="e.g. 300M",
                    default=format_power(existing.min_power_a) if existing.min_power_a else "",
                    required=False, max_length=12,
                )
                self.add_item(self.power_a_input)
            if self._teams in ("both", "B"):
                self.power_b_input = discord.ui.TextInput(
                    label="Min Power Team B",
                    placeholder="e.g. 180M",
                    default=format_power(existing.min_power_b) if existing.min_power_b else "",
                    required=False, max_length=12,
                )
                self.add_item(self.power_b_input)
            self.power_input = None
        else:
            self.power_input = discord.ui.TextInput(
                label="Min Power",
                placeholder="e.g. 250M",
                default=format_power(existing.min_power_a) if existing.min_power_a else "",
                required=False, max_length=12,
            )
            self.add_item(self.power_input)
            self.power_a_input = None
            self.power_b_input = None

        self.priority_input = discord.ui.TextInput(
            label="Priority (1 = highest; ties OK)",
            placeholder="e.g. 1 — same number across zones is fine",
            default=str(existing.priority or ""),
            required=False, max_length=3,
        )
        self.add_item(self.priority_input)

        self.remove_input = discord.ui.TextInput(
            label="Type 'remove' to drop this zone",
            placeholder="leave blank to keep this zone",
            required=False, max_length=10,
        )
        self.add_item(self.remove_input)

    async def on_submit(self, interaction: discord.Interaction):
        if (self.remove_input.value or "").strip().lower() == "remove":
            self._view.buf.remove_zone(self._zone_name)
            await self._view.refresh(interaction, message=f"🗑️ Removed **{self._zone_name}**.")
            return

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
        # a typo would otherwise persist as a "no floor" entry and the
        # eligibility filter would pass below-floor members through it.
        existing = self._view.buf.find_zone(self._zone_name) or ZoneRow(zone=self._zone_name)
        if self._view.buf.event_type == "DS":
            # Hidden inputs (single-team alliances) preserve the stored
            # value rather than overwriting to 0 — keeps the door open for
            # an alliance to flip to two-team mode without losing prior
            # floor values.
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
                    "Leave blank for no floor.",
                    ephemeral=True,
                )
                return
        else:
            min_a, bad = _parse_power_cell(self.power_input.value or "")
            min_b = 0
            if bad:
                await interaction.response.send_message(
                    f"⚠️ Couldn't parse `{self.power_input.value}` as a power "
                    f"value. Try `250M`, `1.2B`, or `300000000`. Leave blank "
                    f"for no floor.",
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
            min_power_a=min_a, min_power_b=min_b,
            priority=priority,
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
                        min_power_a=min_a, min_power_b=min_b,
                        priority=priority,
                    ),
                )
                apply_view.message = await interaction.followup.send(
                    content=(
                        f"💡 **{self._zone_name}** has similar zones in this preset: "
                        f"{', '.join(siblings)}. Pick any to copy these same "
                        f"Max / Min / Priority values to, or skip."
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
            placeholder="Choose siblings to apply to…",
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
                    min_power_a=self._values.min_power_a,
                    min_power_b=self._values.min_power_b,
                    priority=self._values.priority,
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


class _AddZoneModal(discord.ui.Modal, title="Add Zone to Preset"):
    """For alliances who want to extend the canonical zone list with a
    custom name (rare; mostly handled by the canonical list, but useful
    for special-case zones)."""

    def __init__(self, view: "_PresetEditorView"):
        super().__init__()
        self._view = view
        self.zone_input = discord.ui.TextInput(
            label="Zone name",
            placeholder="e.g. Power Tower",
            required=True, max_length=40,
        )
        self.add_item(self.zone_input)

    async def on_submit(self, interaction: discord.Interaction):
        name = (self.zone_input.value or "").strip()
        if not name:
            await interaction.response.send_message(
                "⚠️ Zone name is required.",
                ephemeral=True,
            )
            return
        if self._view.buf.find_zone(name):
            await interaction.response.send_message(
                f"⚠️ Zone **{name}** is already in this preset.",
                ephemeral=True,
            )
            return
        self._view.buf.upsert_zone(ZoneRow(zone=name, max_players=0))
        await self._view.refresh(interaction, message=f"➕ Added **{name}**.")


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
        # Uniqueness check excluding the current name.
        existing = [
            p.lower() for p in list_presets(self._view.guild_id, self._view.buf.event_type)
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
        # Snapshot the alliance's configured-teams choice (#148) at open
        # time. Used by the embed renderer + zone modal so single-team
        # alliances see only their team's Min Power floor. Resolved once
        # rather than re-read on every modal open / refresh.
        self.teams = _resolve_ds_teams(guild_id) if buf.event_type == "DS" else "both"
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
                await inter.response.send_modal(_ZoneEditModal(self, zone_name))

            zone_select.callback = _on_select
            self.add_item(zone_select)

        # Action buttons
        add_btn   = discord.ui.Button(label="➕ Add zone", style=discord.ButtonStyle.secondary)
        rename_btn = discord.ui.Button(label="✏️ Rename", style=discord.ButtonStyle.secondary)
        save_btn  = discord.ui.Button(
            label="💾 Save preset",
            style=discord.ButtonStyle.success,
            disabled=not self.buf.dirty,
        )
        cancel_btn = discord.ui.Button(label="🔙 Abandon", style=discord.ButtonStyle.danger)

        async def _add(inter):
            if inter.user.id != self.user_id:
                await inter.response.send_message("⛔ Only the editor's owner can change this preset.", ephemeral=True); return
            await inter.response.send_modal(_AddZoneModal(self))
        add_btn.callback = _add

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
            ok = save_preset(self.guild_id, self.buf.event_type, self.buf)
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

        self.add_item(add_btn)
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
        hint = (
            "/ds_strategy edit" if self.buf.event_type == "DS"
            else "/cs_strategy edit"
        )
        await expire_view_message(self.message, command_hint=hint)


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
        existing = [p.lower() for p in list_presets(interaction.guild_id, self.event_type)]
        if name.lower() in existing:
            await interaction.response.send_message(
                f"⚠️ A preset named **{name}** already exists. "
                f"Use `/{self.name.replace('_', ' ')} edit name:\"{name}\"` to modify it.",
                ephemeral=True,
            )
            return
        buf = seed_default_preset(name, self.event_type)
        buf.dirty = True
        await _open_editor(interaction, self.event_type, buf)

    async def _edit(self, interaction: discord.Interaction, name: str):
        if not await _deny_if_not_leader(interaction):
            return
        buf = load_preset(interaction.guild_id, self.event_type, name)
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
        names = list_presets(interaction.guild_id, self.event_type)
        label = "Desert Storm" if self.event_type == "DS" else "Canyon Storm"
        if not names:
            await interaction.response.send_message(
                f"📋 No {label} strategy presets saved yet. "
                f"Use the create command to make one.",
                ephemeral=True,
            )
            return
        embed = discord.Embed(
            title=f"📋 {label} — Strategy Presets",
            description="\n".join(f"• **{n}**" for n in names),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    async def _delete(self, interaction: discord.Interaction, name: str):
        if not await _deny_if_not_leader(interaction):
            return

        class _ConfirmDelete(discord.ui.View):
            def __init__(self_, owner_id: int):
                super().__init__(timeout=60)
                self_.owner_id = owner_id
                self_.confirmed = None
                self_.message: discord.Message | None = None

            @discord.ui.button(label="🗑️ Delete preset", style=discord.ButtonStyle.danger)
            async def yes(self_, inter: discord.Interaction, btn: discord.ui.Button):
                if inter.user.id != self_.owner_id:
                    await inter.response.send_message(
                        "⛔ Only the user who ran the command can confirm.",
                        ephemeral=True,
                    )
                    return
                self_.confirmed = True
                for item in self_.children: item.disabled = True
                await inter.response.edit_message(view=self_)
                self_.stop()

            @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
            async def no(self_, inter: discord.Interaction, btn: discord.ui.Button):
                if inter.user.id != self_.owner_id:
                    await inter.response.send_message(
                        "⛔ Only the user who ran the command can cancel.",
                        ephemeral=True,
                    )
                    return
                self_.confirmed = False
                for item in self_.children: item.disabled = True
                await inter.response.edit_message(view=self_)
                self_.stop()

            async def on_timeout(self_) -> None:
                """Strip the confirm prompt on timeout so the buttons
                don't surface 'Interaction failed' after the 60-second
                window. Treats no-decision as cancel."""
                for item in self_.children:
                    item.disabled = True
                if self_.message is not None:
                    try:
                        await self_.message.edit(view=self_)
                    except discord.HTTPException:
                        pass

        view = _ConfirmDelete(interaction.user.id)
        await interaction.response.send_message(
            f"⚠️ Delete preset **{name}**? This removes all rows for this preset from your "
            f"Sheet. Can't be undone.",
            view=view,
            ephemeral=True,
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            view.message = None
        await view.wait()
        if not view.confirmed:
            await interaction.followup.send("✅ Delete cancelled.", ephemeral=True)
            return
        ok = delete_preset(interaction.guild_id, self.event_type, name)
        if ok:
            await interaction.followup.send(
                f"🗑️ Deleted preset **{name}**.",
                ephemeral=False,
            )
        else:
            await interaction.followup.send(
                f"⚠️ Couldn't find preset **{name}** to delete (or Sheet write failed).",
                ephemeral=True,
            )


def _build_ds_group() -> _StrategyGroup:
    grp = _StrategyGroup(
        name="ds_strategy",
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
        date="Optional — show a specific date (YYYY-MM-DD). Omit to list recent events.",
    )
    async def ds_history(interaction: discord.Interaction, date: str | None = None):
        from storm_history import open_history
        await open_history(interaction, "DS", date)

    return grp


def _build_cs_group() -> _StrategyGroup:
    grp = _StrategyGroup(
        name="cs_strategy",
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
        date="Optional — show a specific date (YYYY-MM-DD). Omit to list recent events.",
    )
    async def cs_history(interaction: discord.Interaction, date: str | None = None):
        from storm_history import open_history
        await open_history(interaction, "CS", date)

    return grp


class StormStrategyCog(commands.Cog):
    """Cog wrapping the DS / CS strategy slash command groups."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ds_group = _build_ds_group()
        self.cs_group = _build_cs_group()
        bot.tree.add_command(self.ds_group)
        bot.tree.add_command(self.cs_group)

    async def cog_unload(self):
        # When the cog reloads (rare), remove the groups so re-registration
        # doesn't double them up.
        try:
            self.bot.tree.remove_command(self.ds_group.name)
            self.bot.tree.remove_command(self.cs_group.name)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(StormStrategyCog(bot))
