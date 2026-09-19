"""
Reading an alliance's roster Sheet into the per-member records the storm
roster builder, the sign-up buttons, the write-back and the buddy system
all work from: who is on the roster, their resolved name, their Discord
ID, their squad power, whether they are on Discord, and (when the
stale-power nudge is configured) when their power was last updated.

Split out of `storm_roster_builder.py` in the #589 step 11 refactor.
`storm_roster_builder` imports every public name here back under its old
underscore name, so `storm_roster_builder._read_roster_powers` and the
index helpers `buddy.py` imports keep working unchanged. Nothing here
imports `storm_roster_builder`; `config` and the name resolver are
imported at call time, as before, so `patch("config.X")` keeps its target.

Shape:

* `resolve_columns` turns the header row and the three configs into a
  `RosterColumns`: which column holds what, whether power lives on this
  tab or another, and which column matches rows across tabs. Pure.
* `parse_member_row` turns one sheet row into a member record (or
  nothing), deciding the name cascade, the power cell and the
  not-on-Discord verdict. Pure apart from the live-guild lookup.
* `read_roster_powers` reads the configs and the sheet, runs those two
  over every row, then lays the cross-tab power and last-updated
  overlays on top. Its return shape and every soft error are unchanged.

`tests/unit/test_storm_roster_powers.py` and the reader tests in
`tests/unit/test_storm_roster_builder.py` were written against the
function this replaced and hold this one to it.
"""

from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_TRUTHY = {"1", "true", "yes", "y", "x", "t"}
_STALE_PREVIEW = 5


# ── The power column's header, for DM copy ───────────────────────────────────


def read_power_column_header(guild_id: int, event_type: str) -> str:
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
    # Honour the configured Power Data Source tab (#256). Empty
    # `power_metric_tab` keeps the pre-flexibility behaviour where
    # power lives on the Member Roster row itself; non-empty points
    # the lookup at a separate tab (e.g. `Squad Powers`), so the
    # header label members see in the DM matches the actual column
    # the bot is reading.
    configured_power_tab = (structured.get("power_metric_tab") or "").strip()
    tab_to_read = configured_power_tab or (roster_cfg.get("tab_name") or "Member Roster")
    try:
        ws = config.get_member_roster_sheet(guild_id, tab_to_read)
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


# ── Cross-tab indexes ────────────────────────────────────────────────────────


def build_cross_tab_power_index(
    guild_id: int,
    tab_name: str,
    power_col: int,
    match_col: int,
) -> tuple[dict[str, int], dict[str, list[int]], list[str]]:
    """Read a power source tab and build two parallel lookup indexes.

    Returns `(power_by_id, power_by_name, errors)`:
      * `power_by_id` keys on the digit value found in `match_col`
        (so alliances who match by Discord ID get an O(1) lookup).
      * `power_by_name` keys on the lowercased text value in
        `match_col`. Stored as a list so the lookup can flag
        multi-match rows as ambiguous and decline to guess.

    Used by `read_roster_powers` when the alliance pointed storm at
    a power tab that's distinct from the Member Roster.
    """
    import config
    from storm_strategy import parse_power

    power_by_id: dict[str, int] = {}
    power_by_name: dict[str, list[int]] = {}
    errors: list[str] = []

    try:
        ws = config.get_member_roster_sheet(guild_id, tab_name)
    except Exception as e:
        errors.append(f"power-source tab {tab_name!r} open failed: {e}")
        return {}, {}, errors

    try:
        values = ws.get_all_values()
    except Exception as e:
        errors.append(f"power-source tab {tab_name!r} read failed: {e}")
        return {}, {}, errors

    if not values:
        return {}, {}, errors

    # Skip the header row when building the index — header cells in
    # the match column shouldn't match a Discord ID or a name.
    for row in values[1:]:
        match_cell = row[match_col].strip() if 0 <= match_col < len(row) else ""
        power_cell = row[power_col].strip() if 0 <= power_col < len(row) else ""
        if not (match_cell and power_cell):
            continue
        parsed = parse_power(power_cell)
        if parsed is None:
            continue
        power_val = int(parsed)
        if match_cell.isdigit():
            # Last-writer-wins for duplicate Discord IDs; in practice
            # no alliance has two rows for the same Discord ID, but
            # if they do the last row's number wins (matches the
            # Member-Roster-keyed-by-ID behaviour).
            power_by_id[match_cell] = power_val
        else:
            power_by_name.setdefault(match_cell.lower(), []).append(power_val)

    return power_by_id, power_by_name, errors


def build_last_updated_index(
    guild_id: int,
    tab_name: str,
    last_updated_col: int,
    match_col: int,
) -> tuple[dict[str, _dt.date], dict[str, list[_dt.date]], list[str]]:
    """Read a last-updated source tab and build two parallel lookup
    indexes mirroring `build_cross_tab_power_index` but storing
    `datetime.date` values instead of ints.

    DD/MM vs MM/DD ambiguity is resolved per-column: we scan every
    non-blank value first to detect the column-wide format (if any
    value has its first slash component > 12 the column locks to
    DMY), then parse every value with that flag. Per-row format
    detection would be wrong — a column of MDY values where today's
    date happens to be 5/3/2026 has no `> 12` first component but
    is still MDY for the whole column.

    Used by `read_roster_powers` when the stale-power DM nudge
    (#255) is configured with a non-empty `power_last_updated_tab`.
    Same-tab and cross-tab cases both go through this helper —
    skipping the small saving of reusing the power read's `values`
    in same-tab case keeps the read path one branch.
    """
    import config
    from storm_date_helpers import (
        parse_last_updated,
        detect_last_updated_dmy_first,
    )

    by_id: dict[str, _dt.date] = {}
    by_name: dict[str, list[_dt.date]] = {}
    errors: list[str] = []

    try:
        ws = config.get_member_roster_sheet(guild_id, tab_name)
    except Exception as e:
        errors.append(f"last-updated source tab {tab_name!r} open failed: {e}")
        return {}, {}, errors

    try:
        values = ws.get_all_values()
    except Exception as e:
        errors.append(f"last-updated source tab {tab_name!r} read failed: {e}")
        return {}, {}, errors

    if not values:
        return {}, {}, errors

    # Format detection pass — collect every non-blank cell in the
    # configured column, then run the column-wide heuristic.
    raw_cells: list[str] = []
    for row in values[1:]:
        if 0 <= last_updated_col < len(row):
            cell = row[last_updated_col].strip()
            if cell:
                raw_cells.append(cell)
    dmy_first = detect_last_updated_dmy_first(raw_cells)

    # Parse pass. Match column same convention as power: header skipped.
    for row in values[1:]:
        match_cell = row[match_col].strip() if 0 <= match_col < len(row) else ""
        ts_cell = row[last_updated_col].strip() if 0 <= last_updated_col < len(row) else ""
        if not (match_cell and ts_cell):
            continue
        parsed = parse_last_updated(ts_cell, dmy_first=dmy_first)
        if parsed is None:
            continue
        if match_cell.isdigit():
            by_id[match_cell] = parsed
        else:
            by_name.setdefault(match_cell.lower(), []).append(parsed)

    return by_id, by_name, errors


def lookup_last_updated_in_index(
    member: dict,
    by_id: dict[str, _dt.date],
    by_name: dict[str, list[_dt.date]],
) -> Optional[_dt.date]:
    """Resolve this member's last-updated date from the cross-tab
    indexes. Mirrors `lookup_power_in_index` — ID match wins, name
    falls back, multi-match names return None (ambiguous)."""
    discord_id = (member.get("discord_id") or "").strip()
    if discord_id and discord_id.isdigit() and discord_id in by_id:
        return by_id[discord_id]
    name = (member.get("name") or "").strip().lower()
    if name and name in by_name:
        hits = by_name[name]
        if len(hits) == 1:
            return hits[0]
    return None


def lookup_power_in_index(
    member: dict,
    power_by_id: dict[str, int],
    power_by_name: dict[str, list[int]],
) -> Optional[int]:
    """Resolve this member's power from the cross-tab indexes.

    Discord ID match wins when both halves (member + index entry)
    are digit strings. Falls back to case-insensitive name match
    against the member's display name. Multi-match names return
    None — ambiguous matches must not silently pick the wrong
    member's power, especially for a floor-gated builder.
    """
    discord_id = (member.get("discord_id") or "").strip()
    if discord_id and discord_id.isdigit() and discord_id in power_by_id:
        return power_by_id[discord_id]
    name = (member.get("name") or "").strip().lower()
    if name:
        matches = power_by_name.get(name, [])
        if len(matches) == 1:
            return matches[0]
    return None


# ── Column resolution ────────────────────────────────────────────────────────


def find_header_column(header: list[str], name: str) -> int:
    """Index of the header cell equal to `name`, case-insensitive and
    trimmed, or -1."""
    target = name.strip().lower()
    for idx, cell in enumerate(header):
        if cell.strip().lower() == target:
            return idx
    return -1


def col_letter(value) -> str:
    """A single column letter A-Z, upper-cased, or ''."""
    letter = (value or "").strip().upper()
    return letter if len(letter) == 1 and "A" <= letter <= "Z" else ""


@dataclass(frozen=True)
class RosterColumns:
    """Where everything lives on the roster Sheet, resolved once per read.

    `same_power_tab` is True when power is read from the Member Roster
    itself (the default) and the power cell is parsed inline per row;
    False when the alliance pointed storm at another tab (the Survey's
    Squad Powers, a custom export), in which case every member's power
    is None until the cross-tab overlay fills it in.
    """

    header: list[str]
    id_col: int
    name_col: int
    username_col: int
    display_col: int
    part_alias_col: int
    power_col: int
    power_letter: str
    power_col_header: str
    same_power_tab: bool
    power_tab: str
    member_roster_tab: str
    cross_tab_match_col: int
    presence_col: int
    not_disc_col: int

    @property
    def power_tab_for_logging(self) -> str:
        return self.power_tab if not self.same_power_tab else self.member_roster_tab

    @property
    def power_column_missing(self) -> bool:
        """The configured power letter points past the header, or at a
        blank header cell, on the Member Roster tab. Cross-tab columns
        are validated by the overlay instead."""
        return self.same_power_tab and not self.power_col_header


def resolve_columns(
    header: list[str],
    roster_cfg: dict,
    structured: dict,
    participation_cfg: dict,
) -> RosterColumns:
    import config

    power_letter = (structured.get("power_metric_column") or "B").strip().upper()
    power_col = config.power_column_letter_to_index(power_letter)

    member_roster_tab = roster_cfg.get("tab_name") or "Member Roster"
    configured_power_tab = (structured.get("power_metric_tab") or "").strip()
    same_power_tab = not (configured_power_tab and configured_power_tab != member_roster_tab)

    # Match column for cross-tab lookups. Empty `power_match_column`
    # falls back to the Member Roster's discord_id_col.
    match_letter = col_letter(structured.get("power_match_column"))
    if match_letter:
        cross_tab_match_col = config.power_column_letter_to_index(match_letter)
    else:
        cross_tab_match_col = int(roster_cfg.get("discord_id_col", 0))

    id_col = int(roster_cfg.get("discord_id_col", 0))
    display_col = int(roster_cfg.get("display_col", roster_cfg.get("name_col", 1)))
    # Display-name column. Alliances that overwrote the bot-managed
    # Display Name column with their own data (typically the power
    # column) would otherwise see power values where names should be.
    # The participation flow's Alias Column step already asks which
    # column has the alias, so honour it when it is set; otherwise the
    # member roster sync's `display_col`.
    part_alias_col = participation_cfg.get("roster_alias_col", -1)
    if isinstance(part_alias_col, int) and part_alias_col >= 0:
        name_col = part_alias_col
    else:
        name_col = display_col
    # The underlying Name column (typically B, the Discord username) is
    # the second tier of the name cascade, so a hand-typed row with only
    # column B filled still resolves to a real name (#268).
    username_col = int(roster_cfg.get("name_col", 1))

    if same_power_tab:
        power_col_header = header[power_col].strip() if 0 <= power_col < len(header) else ""
    else:
        power_col_header = ""  # logged as N/A; validated by the overlay

    # The bot-maintained presence column wins when present; the legacy
    # `not_on_discord` column is the back-compat fallback for alliances
    # that haven't synced under the new bot version yet.
    presence_col = find_header_column(header, "is this user in discord?")
    not_disc_col = find_header_column(header, "not_on_discord")
    if not_disc_col < 0:
        not_disc_col = find_header_column(header, "not on discord")

    return RosterColumns(
        header=header,
        id_col=id_col,
        name_col=name_col,
        username_col=username_col,
        display_col=display_col,
        part_alias_col=part_alias_col if isinstance(part_alias_col, int) else -1,
        power_col=power_col,
        power_letter=power_letter,
        power_col_header=power_col_header,
        same_power_tab=same_power_tab,
        power_tab=configured_power_tab,
        member_roster_tab=member_roster_tab,
        cross_tab_match_col=cross_tab_match_col,
        presence_col=presence_col,
        not_disc_col=not_disc_col,
    )


# ── One row → one member ─────────────────────────────────────────────────────


def _cell_reader(row: list) -> Callable[[int], str]:
    def cell(idx: int) -> str:
        if idx < 0 or idx >= len(row):
            return ""
        return str(row[idx]).strip()

    return cell


def _row_power(cols: RosterColumns, cell, *, who: str, guild_id: int, event_type: str):
    """The power cell, parsed, when power lives on this tab. Blank is
    None (not zero); garbage is None plus one log line, not a
    leadership-facing error per row."""
    from storm_strategy import parse_power

    if not (cols.same_power_tab and cols.power_col >= 0):
        return None
    raw_power = cell(cols.power_col)
    if not raw_power:
        return None
    parsed = parse_power(raw_power)
    if parsed is None:
        logger.warning(
            "[STORM ROSTER] couldn't parse power %r for member %r (guild=%s event=%s)",
            raw_power,
            who,
            guild_id,
            event_type,
        )
        return None
    return int(parsed)


def not_on_discord(cols: RosterColumns, cell, discord_id: str, guild) -> tuple[bool, bool]:
    """`(not_on_discord, stale)`. Resolution order:
    1. The "Is this user in Discord?" column (bot-maintained Yes/No)
       when present and definitive.
    2. The legacy explicit `not_on_discord` flag.
    3. Inference: no ID, or a non-numeric placeholder ("TBD"), is
       non-Discord (#139); with a live guild, an ID that resolves to
       nobody or to a bot is non-Discord *and* stale."""
    if cols.presence_col >= 0:
        presence = cell(cols.presence_col).lower()
        if presence == "yes":
            return False, False
        if presence == "no":
            return True, False
    if cols.not_disc_col >= 0 and cell(cols.not_disc_col).lower() in _TRUTHY:
        return True, False
    if not discord_id or not discord_id.isdigit():
        return True, False
    if guild is None:
        return False, False
    try:
        member = guild.get_member(int(discord_id))
    except (TypeError, ValueError):
        member = None
    if member is None or member.bot:
        return True, True
    return False, False


def parse_member_row(
    row: list,
    cols: RosterColumns,
    guild,
    *,
    guild_id: int,
    event_type: str,
) -> tuple[Optional[dict], Optional[str]]:
    """One sheet row → `(member, stale_label)`. `member` is None for a
    row with no ID and no name; `stale_label` names a member whose
    Discord ID no longer resolves, for the soft error."""
    from storm_officer_view import _resolve_member_name

    cell = _cell_reader(row)
    discord_id = cell(cols.id_col)
    display_value = cell(cols.name_col)
    username_value = cell(cols.username_col)
    if not (discord_id or display_value or username_value):
        return None, None
    # Display Name → Name → live Discord member → discord_id (#268).
    name = _resolve_member_name(discord_id, display_value, username_value, guild)
    power = _row_power(cols, cell, who=name or discord_id, guild_id=guild_id, event_type=event_type)
    off_discord, stale = not_on_discord(cols, cell, discord_id, guild)
    key = discord_id or name
    if not key:
        return None, None
    member = {
        "key": key,
        "name": name,
        "discord_id": discord_id,
        "power": power,
        "not_on_discord": off_discord,
    }
    return member, (f"{name or '?'} (id {discord_id})" if stale else None)


def stale_ids_notice(stale_ids: list[str]) -> str:
    preview = ", ".join(stale_ids[:_STALE_PREVIEW])
    extra = f" (+{len(stale_ids) - _STALE_PREVIEW} more)" if len(stale_ids) > _STALE_PREVIEW else ""
    return f"stale Discord IDs on roster (member likely left the server): {preview}{extra}"


# ── Overlays ─────────────────────────────────────────────────────────────────


def _overlay_cross_tab_power(
    members: dict[str, dict], cols: RosterColumns, *, guild_id: int, event_type: str
) -> list[str]:
    """When power lives on another tab, every member's power is still
    None here; build the ID + name indexes from that tab and resolve
    each member. Returns the overlay's soft errors."""
    power_by_id, power_by_name, errors = build_cross_tab_power_index(
        guild_id, cols.power_tab, cols.power_col, cols.cross_tab_match_col
    )
    matched_count = 0
    for m in members.values():
        resolved = lookup_power_in_index(m, power_by_id, power_by_name)
        if resolved is not None:
            m["power"] = resolved
            matched_count += 1
    logger.info(
        "[STORM ROSTER] cross-tab power overlay: tab=%r matched=%d/%d "
        "(by_id=%d, by_name=%d) guild=%s event=%s",
        cols.power_tab,
        matched_count,
        len(members),
        len(power_by_id),
        len(power_by_name),
        guild_id,
        event_type,
    )
    return errors


def _overlay_last_updated(
    members: dict[str, dict],
    structured: dict,
    cols: RosterColumns,
    *,
    guild_id: int,
    event_type: str,
) -> list[str]:
    """The stale-power nudge (#255) needs each member's last-updated
    timestamp. An empty `power_last_updated_tab` skips the overlay
    entirely (alliances without the stale check pay nothing); an empty
    match column reuses the power source's. Members not found keep
    `last_updated: None`, which the click handler reads as "skip the
    stale check for this row". Returns the overlay's soft errors."""
    import config

    lu_tab = (structured.get("power_last_updated_tab") or "").strip()
    lu_col_letter = col_letter(structured.get("power_last_updated_column"))
    if not (lu_tab and lu_col_letter and members):
        return []
    lu_col = config.power_column_letter_to_index(lu_col_letter)
    lu_match_letter = col_letter(structured.get("power_last_updated_match_column"))
    if lu_match_letter:
        lu_match_col = config.power_column_letter_to_index(lu_match_letter)
    else:
        lu_match_col = cols.cross_tab_match_col
    lu_by_id, lu_by_name, errors = build_last_updated_index(guild_id, lu_tab, lu_col, lu_match_col)
    lu_matched = 0
    for m in members.values():
        ts = lookup_last_updated_in_index(m, lu_by_id, lu_by_name)
        m["last_updated"] = ts
        if ts is not None:
            lu_matched += 1
    logger.info(
        "[STORM ROSTER] last-updated overlay: tab=%r col=%s matched=%d/%d "
        "(by_id=%d, by_name=%d) guild=%s event=%s",
        lu_tab,
        lu_col_letter,
        lu_matched,
        len(members),
        len(lu_by_id),
        len(lu_by_name),
        guild_id,
        event_type,
    )
    return errors


# ── The read ─────────────────────────────────────────────────────────────────


def read_roster_powers(
    guild_id: int,
    event_type: str,
    *,
    guild=None,
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

    The Power Data Source is configurable per (guild, event_type)
    via `power_metric_tab` + `power_match_column` on the structured
    storm config. Empty `power_metric_tab` falls back to the Member
    Roster tab. Cross-tab reads build a Discord-ID-keyed and a
    name-keyed index, matched per member by `lookup_power_in_index`.

    Errors are returned soft so the slash command can surface a one-line
    warning without aborting the builder entirely.
    """
    import config

    errors: list[str] = []

    try:
        roster_cfg = config.get_member_roster_config(guild_id)
    except Exception as e:
        return {}, [f"roster-config read failed: {e}"]
    try:
        structured = config.get_structured_storm_config(guild_id, event_type)
    except Exception as e:
        return {}, [f"structured-config read failed: {e}"]

    if not roster_cfg.get("enabled"):
        errors.append(
            "member-roster sync isn't enabled — without /members sync the "
            "builder can't see your alliance's roster."
        )
        return {}, errors

    try:
        # Short-TTL cached read so rapid officer clicks don't each fire a
        # Sheets read and blow the 60/min quota (#269).
        values = config.read_member_roster_values(
            guild_id, roster_cfg.get("tab_name") or "Member Roster"
        )
    except Exception as e:
        errors.append(f"roster-sheet read failed: {e}")
        return {}, errors
    if not values:
        return {}, errors

    header = [c.strip() for c in values[0]]
    participation_cfg = config.get_participation_config(guild_id, event_type)
    cols = resolve_columns(header, roster_cfg, structured, participation_cfg)

    if cols.power_column_missing:
        errors.append(
            f"power column {cols.power_letter} doesn't exist in your roster "
            f"Sheet header (or is blank). Re-run the setup wizard's Power "
            f"Data Source step to pick a different column."
        )
        logger.warning(
            "[STORM ROSTER] power column letter %r resolves to index %d, "
            "which is past the header row (len=%d) for guild=%s event=%s. "
            "Header: %s",
            cols.power_letter,
            cols.power_col,
            len(header),
            guild_id,
            event_type,
            header,
        )
    # One log line answers "which column is the bot looking at" — the
    # team-test questions were "matching by name not Discord ID" and
    # "power not reading even when in the sheet".
    logger.info(
        "[STORM ROSTER] guild=%s event=%s column resolution: "
        "id_col=%d (cfg discord_id_col=%d), name_col=%d "
        "(participation roster_alias_col=%d, display_col=%d), "
        "power_col=%d (letter %s, header %r, tab %r same=%s match_col=%d), "
        "presence_col=%d, not_disc_col=%d, header=%s",
        guild_id,
        event_type,
        cols.id_col,
        int(roster_cfg.get("discord_id_col", 0)),
        cols.name_col,
        cols.part_alias_col,
        cols.display_col,
        cols.power_col,
        cols.power_letter,
        cols.power_col_header,
        cols.power_tab_for_logging,
        cols.same_power_tab,
        cols.cross_tab_match_col,
        cols.presence_col,
        cols.not_disc_col,
        header,
    )

    members: dict[str, dict] = {}
    stale_ids: list[str] = []
    for row in values[1:]:
        member, stale = parse_member_row(row, cols, guild, guild_id=guild_id, event_type=event_type)
        if member is None:
            continue
        members[member["key"]] = member
        if stale:
            stale_ids.append(stale)

    if stale_ids:
        errors.append(stale_ids_notice(stale_ids))
        logger.warning(
            "[STORM ROSTER] stale roster Discord IDs for guild=%s event=%s: %s",
            guild_id,
            event_type,
            "; ".join(stale_ids),
        )

    if not cols.same_power_tab and members:
        errors.extend(
            _overlay_cross_tab_power(members, cols, guild_id=guild_id, event_type=event_type)
        )
    errors.extend(
        _overlay_last_updated(members, structured, cols, guild_id=guild_id, event_type=event_type)
    )
    return members, errors
