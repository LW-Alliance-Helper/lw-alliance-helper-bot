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
import datetime as _dt
import io
import logging
import re
from typing import Optional

import discord

from messages import CANCEL_BACKPEDAL, DENY_NOT_OWNER, PREMIUM_LOCKED_INLINE
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


def _build_cross_tab_power_index(
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

    Used by `_read_roster_powers` when the alliance pointed storm at
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


def _build_last_updated_index(
    guild_id: int,
    tab_name: str,
    last_updated_col: int,
    match_col: int,
) -> tuple[dict[str, "_dt.date"], dict[str, list["_dt.date"]], list[str]]:
    """Read a last-updated source tab and build two parallel lookup
    indexes mirroring `_build_cross_tab_power_index` but storing
    `datetime.date` values instead of ints.

    DD/MM vs MM/DD ambiguity is resolved per-column: we scan every
    non-blank value first to detect the column-wide format (if any
    value has its first slash component > 12 the column locks to
    DMY), then parse every value with that flag. Per-row format
    detection would be wrong — a column of MDY values where today's
    date happens to be 5/3/2026 has no `> 12` first component but
    is still MDY for the whole column.

    Used by `_read_roster_powers` when the stale-power DM nudge
    (#255) is configured with a non-empty `power_last_updated_tab`.
    Same-tab and cross-tab cases both go through this helper —
    skipping the small saving of reusing the power read's `values`
    in same-tab case keeps the read path one branch.
    """
    import datetime as _dt
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


def _lookup_last_updated_in_index(
    member: dict,
    by_id: dict[str, "_dt.date"],
    by_name: dict[str, list["_dt.date"]],
) -> "Optional[_dt.date]":
    """Resolve this member's last-updated date from the cross-tab
    indexes. Mirrors `_lookup_power_in_index` — ID match wins, name
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


def _lookup_power_in_index(
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


def _read_roster_powers(
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
    Roster tab (preserving the pre-flexibility default).
    Cross-tab reads build a Discord-ID-keyed and a name-keyed index,
    matched by `_lookup_power_in_index` for each member.

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

    # Power Data Source resolution. `same_power_tab` is True when the
    # alliance is reading power from the Member Roster (default), False
    # when they pointed storm at a different tab (e.g., the Survey's
    # "Squad Powers" tab or a custom external tab). Same-tab keeps the
    # existing inline power-parse in the member loop below. Cross-tab
    # skips inline parsing and runs `_build_cross_tab_power_index` +
    # `_lookup_power_in_index` after the loop.
    member_roster_tab = roster_cfg.get("tab_name") or "Member Roster"
    configured_power_tab = (structured.get("power_metric_tab") or "").strip()
    if configured_power_tab and configured_power_tab != member_roster_tab:
        same_power_tab = False
        power_tab_for_logging = configured_power_tab
    else:
        same_power_tab = True
        power_tab_for_logging = member_roster_tab

    # Match column for cross-tab lookups. Empty `power_match_column`
    # falls back to the Member Roster's discord_id_col (existing
    # behaviour). Letter on the configured power tab when set.
    configured_match_letter = (structured.get("power_match_column") or "").strip().upper()
    if len(configured_match_letter) == 1 and "A" <= configured_match_letter <= "Z":
        cross_tab_match_col = config.power_column_letter_to_index(
            configured_match_letter,
        )
    else:
        cross_tab_match_col = int(roster_cfg.get("discord_id_col", 0))

    if not roster_cfg.get("enabled"):
        errors.append(
            "member-roster sync isn't enabled — without /members sync the "
            "builder can't see your alliance's roster."
        )
        return {}, errors

    try:
        ws = config.get_member_roster_sheet(
            guild_id,
            roster_cfg.get("tab_name") or "Member Roster",
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

    id_col = int(roster_cfg.get("discord_id_col", 0))
    # Display-name column resolution. Alliances that overwrote the
    # bot-managed Display Name column (default C) with their own data
    # — typically the power column — would otherwise have the
    # structured roster builder render power values where members'
    # names should appear. Honour the participation tracking flow's
    # Alias Column step (Step 6.4) when it's been configured: that
    # picker already asks officers which column has the alias, so
    # plumbing it through here avoids forcing them to re-answer the
    # same question elsewhere in setup. Falls back to the member
    # roster sync's `display_col` for alliances who haven't enabled
    # participation tracking or who left the alias picker disabled.
    participation_cfg = config.get_participation_config(guild_id, event_type)
    part_alias_col = participation_cfg.get("roster_alias_col", -1)
    if isinstance(part_alias_col, int) and part_alias_col >= 0:
        name_col = part_alias_col
    else:
        name_col = int(roster_cfg.get("display_col", roster_cfg.get("name_col", 1)))
    # Underlying Name column (typically B = Discord username). Used as
    # the second tier of the name fallback cascade so hand-typed rows
    # with only column B populated still resolve to a real name instead
    # of falling straight to the raw Discord ID (#268).
    username_col = int(roster_cfg.get("name_col", 1))
    # Power column is a configured letter (Rule C / #165) — A=0, B=1,
    # etc. Validate only when the power data lives on the Member
    # Roster tab (same_power_tab=True); for cross-tab reads the
    # column lives on a different sheet and is validated inside
    # `_build_cross_tab_power_index`.
    if same_power_tab:
        power_col_header = header[power_col].strip() if 0 <= power_col < len(header) else ""
        if not power_col_header:
            errors.append(
                f"power column {power_letter} doesn't exist in your roster "
                f"Sheet header (or is blank). Re-run the setup wizard's Power "
                f"Data Source step to pick a different column."
            )
            logger.warning(
                "[STORM ROSTER] power column letter %r resolves to index %d, "
                "which is past the header row (len=%d) for guild=%s event=%s. "
                "Header: %s",
                power_letter,
                power_col,
                len(header),
                guild_id,
                event_type,
                header,
            )
    else:
        power_col_header = ""  # logged later as N/A
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
    # which column the bot is looking at. `part_alias_col < 0` means
    # the bot fell back to member_roster_config.display_col;
    # same_power_tab=False means the alliance pointed storm at a
    # different power tab and the inline power-parse is skipped.
    logger.info(
        "[STORM ROSTER] guild=%s event=%s column resolution: "
        "id_col=%d (cfg discord_id_col=%d), name_col=%d "
        "(participation roster_alias_col=%d, display_col=%d), "
        "power_col=%d (letter %s, header %r, tab %r same=%s match_col=%d), "
        "presence_col=%d, not_disc_col=%d, header=%s",
        guild_id,
        event_type,
        id_col,
        int(roster_cfg.get("discord_id_col", 0)),
        name_col,
        part_alias_col,
        int(roster_cfg.get("display_col", roster_cfg.get("name_col", 1))),
        power_col,
        power_letter,
        power_col_header,
        power_tab_for_logging,
        same_power_tab,
        cross_tab_match_col,
        presence_col,
        not_disc_col,
        header,
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
        display_value = _cell(name_col)
        username_value = _cell(username_col)
        # Resolve the human-readable name with a fallback cascade:
        # Display Name → Name → live Discord member → discord_id (#268).
        # Pre-#268 only checked the primary `name_col` and fell straight
        # to discord_id, so hand-typed rows that only filled in Name
        # rendered as the raw Discord ID (or as the alliance's
        # workaround text typed into the ID column).
        from storm_officer_view import _resolve_member_name

        resolved_name = _resolve_member_name(
            discord_id,
            display_value,
            username_value,
            guild,
        )
        # Keep `name` as the legacy variable referenced below — its
        # semantic is now "resolved display name", not "display_col
        # cell". Skip condition widens to honour the Name column too,
        # so a hand-typed row with only column B populated still rides
        # through.
        name = resolved_name
        if not (discord_id or display_value or username_value):
            continue

        # Parse the power cell only when the power data lives on the
        # Member Roster (same_power_tab). Cross-tab reads skip this
        # branch and get their power values overlaid after the loop
        # via `_lookup_power_in_index`. Blank → None (not zero).
        # Garbage → None plus a single log warning; we don't surface
        # every row as an error to leadership.
        power_val: Optional[int] = None
        if same_power_tab and power_col >= 0:
            raw_power = _cell(power_col)
            if raw_power:
                parsed = parse_power(raw_power)
                if parsed is None:
                    logger.warning(
                        "[STORM ROSTER] couldn't parse power %r for member %r (guild=%s event=%s)",
                        raw_power,
                        name or discord_id,
                        guild_id,
                        event_type,
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
                    "key": discord_id or name,
                    "name": name,
                    "discord_id": discord_id,
                    "power": power_val,
                    "not_on_discord": False,
                }
                continue
            if presence_cell == "no":
                key = discord_id or name
                members[key] = {
                    "key": key,
                    "name": name,
                    "discord_id": discord_id,
                    "power": power_val,
                    "not_on_discord": True,
                }
                continue
            # Blank / unknown value → fall through to legacy + inference.
        explicit_set = _cell(not_disc_col).lower() in truthy if has_not_col else False
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
            "key": key,
            "name": name,
            "discord_id": discord_id,
            "power": power_val,
            "not_on_discord": not_on_discord,
        }

    if stale_ids:
        preview = ", ".join(stale_ids[:5])
        extra = f" (+{len(stale_ids) - 5} more)" if len(stale_ids) > 5 else ""
        errors.append(
            f"stale Discord IDs on roster (member likely left the server): {preview}{extra}"
        )
        logger.warning(
            "[STORM ROSTER] stale roster Discord IDs for guild=%s event=%s: %s",
            guild_id,
            event_type,
            "; ".join(stale_ids),
        )

    # Cross-tab power overlay. When the alliance pointed storm at a
    # power tab other than the Member Roster, we deferred all power
    # parsing — every member.power is None at this point. Build the
    # ID + name indexes from the configured tab, then resolve each
    # member.
    if not same_power_tab and members:
        power_by_id, power_by_name, p_errors = _build_cross_tab_power_index(
            guild_id,
            configured_power_tab,
            power_col,
            cross_tab_match_col,
        )
        errors.extend(p_errors)
        matched_count = 0
        for m in members.values():
            resolved = _lookup_power_in_index(m, power_by_id, power_by_name)
            if resolved is not None:
                m["power"] = resolved
                matched_count += 1
        logger.info(
            "[STORM ROSTER] cross-tab power overlay: tab=%r matched=%d/%d "
            "(by_id=%d, by_name=%d) guild=%s event=%s",
            configured_power_tab,
            matched_count,
            len(members),
            len(power_by_id),
            len(power_by_name),
            guild_id,
            event_type,
        )

    # Last-updated overlay (#255). Stale-power DM nudge needs each
    # member's most-recent "Date Modified" / equivalent timestamp.
    # Source is configurable: empty `power_last_updated_tab` skips
    # the overlay entirely (alliances who haven't enabled the stale
    # check pay nothing). Empty match column falls back to the same
    # `cross_tab_match_col` resolved above for power. Members not
    # found in the index keep `last_updated: None`, which the
    # click-handler treats as "skip the stale check for this row."
    lu_tab = (structured.get("power_last_updated_tab") or "").strip()
    lu_col_letter = (structured.get("power_last_updated_column") or "").strip().upper()
    if lu_tab and len(lu_col_letter) == 1 and "A" <= lu_col_letter <= "Z" and members:
        lu_col = config.power_column_letter_to_index(lu_col_letter)
        lu_match_letter = (structured.get("power_last_updated_match_column") or "").strip().upper()
        if len(lu_match_letter) == 1 and "A" <= lu_match_letter <= "Z":
            lu_match_col = config.power_column_letter_to_index(lu_match_letter)
        else:
            # Empty match column falls back to whatever match column
            # the power source uses — that's the convention every
            # alliance already configured for power lookups.
            lu_match_col = cross_tab_match_col
        lu_by_id, lu_by_name, lu_errors = _build_last_updated_index(
            guild_id,
            lu_tab,
            lu_col,
            lu_match_col,
        )
        errors.extend(lu_errors)
        lu_matched = 0
        for m in members.values():
            ts = _lookup_last_updated_in_index(m, lu_by_id, lu_by_name)
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
        team: str,  # "A" / "B" / "" (CS uses faction)
        preset,  # storm_strategy.PresetBuffer
        members: dict[str, dict],
        per_member_rules: list,
        power_band_rules: list,
        *,
        event_date: Optional[str] = None,
        sub_mode: str = "pool",
    ):
        self.guild_id = guild_id
        self.user_id = user_id
        self.event_type = event_type
        self.team = team
        self.preset = preset
        self.members = members
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
        # True when the candidate pool was constrained by a saved
        # `storm_team_plans` row (#239). Auto-fill respects the plan's
        # primary/sub split instead of re-deriving by power.
        self.team_plan_applied: bool = False
        # #240 follow-up: latched flag set when an autosave write to
        # `storm_roster_drafts` raises. The next embed render surfaces
        # a clear warning so the officer knows their work isn't being
        # persisted (and can screenshot the embed as a manual backup).
        self.autosave_failed: bool = False

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
    members: dict[str, dict],
    subject: str,
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


AUTO_FILL_STRATEGIES = ("balanced", "priority_greedy")


def _place_starter_in_zone(
    session: RosterBuilderSession,
    starter_key: str,
    zone_name: str,
    phase: int,
    summary: dict,
) -> None:
    """Append a starter to a phase's zone and update the auto-fill
    bookkeeping (below-floor override flag, power-band counter,
    `auto_filled_by_power` count). Shared by every fill strategy so
    floor handling and the summary counts stay in sync (#226)."""
    session.assignments_for_phase(phase)[zone_name].append(starter_key)
    summary["auto_filled_by_power"] += 1
    preset_floor = session.floor_for_zone(zone_name)
    effective_floor = _effective_floor_for_zone(session, zone_name)
    member_power = session.members[starter_key].get("power")
    if member_power is None:
        session.below_floor_overrides_for_phase(phase).add(starter_key)
    elif member_power < effective_floor:
        session.below_floor_overrides_for_phase(phase).add(starter_key)
    elif effective_floor < preset_floor and member_power < preset_floor:
        summary["power_band_rules_applied"] += 1


def _fill_balanced(
    session: RosterBuilderSession,
    remaining: list[str],
    phase: int,
    zones_sorted: list,
    phase_assignments: dict,
    summary: dict,
) -> None:
    """Round-robin fill: pass over zones in priority order placing one
    starter per zone per pass, looping until every starter is placed
    or no zone has remaining capacity. Spreads power evenly across
    every zone the team uses this phase. 0-cap zones are skipped by
    the capacity guard."""
    while remaining:
        progress = False
        for z in zones_sorted:
            if not remaining:
                break
            if session.zone_member_count(z.zone) >= session.zone_capacity(z.zone):
                continue
            starter_key = remaining.pop(0)
            _place_starter_in_zone(session, starter_key, z.zone, phase, summary)
            progress = True
        if not progress:
            # Every zone is full this phase. Remaining starters stay
            # unassigned for this phase; the officer can place them
            # manually via the picker.
            break


def _zone_priority_value(session: RosterBuilderSession, z, phase: int) -> int:
    """The effective priority of a zone for a phase, matching the sort key
    used to order `zones_sorted`. priority=0 ("no priority set") sorts last
    via 9999. Phase-aware presets read per-phase priority; flat presets use
    the single `priority` field."""
    prio = z.priority_for_phase(phase) if session.is_phase_aware else z.priority
    return prio if prio > 0 else 9999


def _fill_priority_greedy(
    session: RosterBuilderSession,
    remaining: list[str],
    phase: int,
    zones_sorted: list,
    phase_assignments: dict,
    summary: dict,
) -> None:
    """Priority-greedy fill, balanced within each priority tier (#273).

    Walks zones in priority asc, but zones that share a priority form a
    group and are balanced by total squad power instead of being filled
    one-at-a-time. Within a group, each next-strongest starter goes to the
    group zone with the lowest running power total that still has capacity
    (longest-processing-time / greedy load balancing). Across groups the
    pool is still consumed strongest-first, so higher-priority zones get
    the strongest members overall — only the lopsided split between
    equal-priority zones (e.g. Oil Refinery I taking the top 5 and II the
    next 5) is fixed. 0-cap zones are skipped by the capacity guard.

    `remaining` is assumed power-desc (the caller sorts it); members with
    unknown power count as 0 for balancing purposes."""
    from itertools import groupby

    def _power(key: str) -> int:
        return session.members.get(key, {}).get("power") or 0

    # `zones_sorted` is already priority-asc, so consecutive equal-priority
    # zones are adjacent and groupby yields one group per tier in order.
    for _prio, grp in groupby(zones_sorted, key=lambda z: _zone_priority_value(session, z, phase)):
        group = list(grp)
        if not remaining:
            break
        # Seed each zone's running total from anything already placed
        # there (e.g. per-member pins landed before the fill).
        running = {z.zone: sum(_power(k) for k in phase_assignments.get(z.zone, [])) for z in group}
        while remaining:
            open_zones = [
                z
                for z in group
                if session.zone_member_count(z.zone) < session.zone_capacity(z.zone)
            ]
            if not open_zones:
                break
            # Lowest running power first; ties keep group (priority-sort)
            # order so the result is deterministic across re-runs.
            target = min(open_zones, key=lambda z: running[z.zone])
            starter_key = remaining.pop(0)
            _place_starter_in_zone(session, starter_key, target.zone, phase, summary)
            running[target.zone] += _power(starter_key)


def _auto_fill_session(
    session: RosterBuilderSession,
    *,
    strategy: str = "balanced",
    plan: dict | None = None,
) -> dict:
    """Auto-fill the roster from member rules and the LW 20-starters-plus-10-subs
    team rule (#219).

    Resets the current roster (assignments, subs, override flags, pairings)
    before filling so a re-click of the button is "redo from scratch"
    rather than "stack onto current state."

    Algorithm, in order:
      1. per_member zone rules. Pin members to their named zone if capacity,
         the member is in the signed-up pool, and the zone exists in the
         preset. Applied to Phase 1 only on phase-aware presets; the rule
         model does not yet carry a phase dimension. Pinned members always
         count as starters regardless of where they rank by power.
      2. Starter / sub split by squad power. Sort signed-up members with
         known power desc (tiebreak by stable member key). Pinned members
         from step 1 occupy starter seats first; the rest of the starter
         pool fills from the top of the power-desc list until
         `team_seats(event_type)` is reached. The next slice (subs_target
         members) becomes the sub pool. Members with no parseable power go
         to `gaps` and are not auto-placed.
      3. Per-phase zone fill (#226). Same starter pool across every
         phase the preset declares. The `strategy` parameter picks:
           "balanced" — round-robin (default; current behavior).
           "priority_greedy" — feed the strongest members to the
             highest-priority zones first, balancing power evenly
             between zones that share a priority (#273).
         Both strategies share `_place_starter_in_zone` so floor
         handling and summary bookkeeping stay aligned, and both
         skip 0-cap zones via the capacity guard.
      4. Paired-mode pairings. Each phase walks its primaries
         weakest-first (power asc) and picks the unpaired candidate
         whose power is closest to the primary's. Zone-floor
         eligibility stays a hard filter. Candidates come from sub_pool
         plus any starter that couldn't fit in a zone in step 3
         (small-alliance fallback).
      5. Spillover. Any power-known member that didn't land in a zone
         and didn't get paired in any phase ends up in `session.subs`.
         In pool mode that's where the sub roster lives. In paired
         mode it's overflow, typically empty in the 30-signup case.

    Returns the summary dict (also stored on `session.auto_fill_summary`).

    The fill is officer-correctable. Every assignment can be tweaked via
    the picker before Approve & Post.
    """
    if strategy not in AUTO_FILL_STRATEGIES:
        strategy = "balanced"
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
        "auto_filled_by_power": 0,
        # Decision #14 (#171): track each auto-pair explicitly so the
        # summary can list `Alice ↔ Bob, Carol ↔ Dan` instead of a
        # bare count. Officers edit auto-paired subs most often, so
        # visibility matters.
        "auto_paired_subs": [],  # list[str] each "PrimaryName ↔ SubName"
        "gaps": [],  # member names with no parseable power
        "conflicts": [],  # short strings: rule application failures
        # #219: how many starter seats went unfilled because too few
        # members signed up. 0 in the normal 30-signup case; positive
        # when the alliance is short.
        "starters_short": 0,
        # #238: subs that ended up in the available pool because their
        # power was below the floor for every remaining unpaired
        # primary's zone. Each entry is `{"name": ..., "power": int,
        # "min_floor": int}` so the embed can surface a clear reason
        # ("Couldn't pair Alice (60M) — power below 80M minimum for
        # any remaining open positions").
        "unpaired_subs_below_floor": [],
    }

    # Remember the officer's UI cursor; we mutate it while filling each
    # phase so capacity / member-count helpers resolve correctly, then
    # restore at the end.
    original_phase = session.selected_phase

    # ── 1. per_member zone rules ── (Phase 1 only on phase-aware)
    # Per Decision #7 (#173): if the rule's subject isn't in tonight's
    # roster the rule is a silent no-op. Nothing to apply, nothing to
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
            summary["conflicts"].append(f"per_member rule names unknown zone: {zone}")
            continue
        if session.zone_member_count(zone) >= session.zone_capacity(zone):
            summary["conflicts"].append(f"{zone} full when pinning {subject}")
            continue
        # Cross-phase duplicate check: pinned member can't already be
        # assigned in any phase or in the sub pool.
        if match_key in session.assigned_member_keys():
            summary["conflicts"].append(f"{subject} pinned to multiple zones")
            continue
        session.assignments_for_phase(1)[zone].append(match_key)
        summary["per_member_rules_applied"] += 1
        member = session.members.get(match_key)
        if member is not None and member.get("power") is None:
            session.below_floor_overrides_for_phase(1).add(match_key)

    # ── 2. Starter / sub split by squad power desc (#219) ──
    # Power-known members rank by power desc; ties break on member key
    # (deterministic, stable across re-runs of auto-fill on the same
    # signups). Power-unknown members flow to `gaps`. Pinned members
    # always occupy starter seats regardless of rank.
    #
    # Plan-aware branch (#239): when a saved team plan exists for this
    # event+team, the in-game commitment overrides the by-power split.
    # The plan's primaries become starters; the plan's subs become the
    # sub pool. Pinned members still occupy starter seats first; a
    # pin-vs-sub conflict surfaces in `summary["conflicts"]` and the
    # pin wins. Plan keys that aren't in `session.members` (e.g. the
    # member's vote changed to "cannot" after the plan was saved) are
    # also surfaced as conflicts.
    from storm import team_seats

    starters_target, subs_target = team_seats(session.event_type)

    pinned_keys: set[str] = set()
    for zone_members in session.assignments_for_phase(1).values():
        pinned_keys.update(zone_members)

    def _power_rank_key(key: str) -> tuple[int, str]:
        m = session.members[key]
        return (-(m.get("power") or 0), key)

    # Auto-load the saved plan if the caller didn't pass one. Tests
    # inject an explicit plan; production callers usually let the
    # session's (guild, event, team) coordinates drive the lookup.
    if plan is None and session.event_date and session.team:
        try:
            import config

            plan = config.get_storm_team_plan(
                session.guild_id,
                session.event_type,
                session.event_date,
                session.team,
            )
        except Exception:
            plan = None

    plan_applied = bool(plan and (plan.get("primaries") or plan.get("subs")))
    plan_sub_keys: set[str] = set()
    if plan_applied:
        member_keys = set(session.members.keys())
        plan_primary_keys = set(plan.get("primaries") or []) & member_keys
        plan_sub_keys = set(plan.get("subs") or []) & member_keys
        # Pinning beats sub marking — surface the conflict for the
        # officer but keep the per-member rule's intent.
        pinned_in_subs = pinned_keys & plan_sub_keys
        for k in sorted(pinned_in_subs):
            mname = session.members.get(k, {}).get("name", k)
            summary["conflicts"].append(
                f"{mname} is pinned by a per-member rule but the saved "
                f"team plan marks them as a sub — pin wins."
            )
        plan_sub_keys -= pinned_keys
        # Plan keys missing from the pool (vote changed to cannot,
        # member removed from roster between plan save and builder
        # open, etc.) — surface so the officer can re-open the plan
        # picker and clean up.
        all_plan_keys = set(plan.get("primaries") or []) | set(plan.get("subs") or [])
        missing_plan_keys = all_plan_keys - member_keys
        for k in sorted(missing_plan_keys):
            summary["conflicts"].append(
                f"plan key {k} missing from pool (vote changed or member dropped from roster)"
            )
        # Gaps still apply: any member with no parseable power that
        # isn't pinned. Plan-driven and signup-driven paths share the
        # gaps semantics.
        for key, m in session.members.items():
            if m.get("power") is None and key not in pinned_keys:
                summary["gaps"].append(m["name"])

        starters: list[str] = list(pinned_keys | plan_primary_keys)
        starters_set: set[str] = set(starters)
        summary["starters_short"] = max(0, starters_target - len(starters))
        sub_pool: list[str] = sorted(plan_sub_keys)
    else:
        power_known: list[str] = [
            k for k, m in session.members.items() if m.get("power") is not None
        ]
        power_known.sort(key=_power_rank_key)

        for key, m in session.members.items():
            if m.get("power") is None and key not in pinned_keys:
                summary["gaps"].append(m["name"])

        starters = list(pinned_keys)
        starters_set = set(starters)
        for key in power_known:
            if len(starters) >= starters_target:
                break
            if key in starters_set:
                continue
            starters.append(key)
            starters_set.add(key)

        summary["starters_short"] = max(0, starters_target - len(starters))

        sub_pool = []
        for key in power_known:
            if len(sub_pool) >= subs_target:
                break
            if key in starters_set:
                continue
            sub_pool.append(key)

    # ── 3. Per-phase fill via the selected strategy (#226) ──
    # Zones order by priority asc via `_zone_priority_value` (priority=0
    # sorts last; phase-aware presets read per-phase priority).
    for phase in session.iter_phases():
        session.selected_phase = phase
        phase_assignments = session.assignments_for_phase(phase)
        zones_sorted = sorted(
            session.preset.zones,
            key=lambda z: _zone_priority_value(session, z, phase),
        )

        # Members already placed in this phase (from per-member rules in
        # phase 1 only): they occupy a starter seat but were already put
        # in a zone, so the fill skips them.
        already_placed: set[str] = set()
        for zone_members in phase_assignments.values():
            already_placed.update(zone_members)

        remaining = [k for k in starters if k not in already_placed]
        # Re-sort by power desc so power-known starters are placed before
        # any pinned-with-unknown-power starters that landed in `starters`
        # via step 1. Stable on member key.
        remaining.sort(key=_power_rank_key)

        if strategy == "priority_greedy":
            _fill_priority_greedy(
                session,
                remaining,
                phase,
                zones_sorted,
                phase_assignments,
                summary,
            )
        else:
            _fill_balanced(
                session,
                remaining,
                phase,
                zones_sorted,
                phase_assignments,
                summary,
            )

    # ── 4. Sub pairings (paired mode only) ──
    # Pair candidates are sub_pool plus any starters that couldn't fit
    # in zones during step 3 (small-alliance fallback so the team's
    # weakest placed starter still gets a backup when fewer than 30 are
    # signed up). Per phase, walk primaries weakest-first (power asc,
    # unknown power last) and pick the closest-power sub that clears
    # the primary's zone floor. The same candidate identity can be
    # paired in multiple phases — the per-phase pairing dicts are
    # independent.
    placed_anywhere: set[str] = set()
    for phase in session.iter_phases():
        for zone_members in session.assignments_for_phase(phase).values():
            placed_anywhere.update(zone_members)
    unplaced_starters = [k for k in starters if k not in placed_anywhere]
    pairing_candidates = list(sub_pool) + unplaced_starters

    if session.is_paired:
        for phase in session.iter_phases():
            session.selected_phase = phase
            phase_assignments = session.assignments_for_phase(phase)
            phase_pairings = session.paired_subs_for_phase(phase)

            primaries_with_zone: list[tuple[str, str]] = []
            for zone_name, zmembers in phase_assignments.items():
                for primary_key in zmembers:
                    if primary_key in phase_pairings:
                        continue
                    primaries_with_zone.append((primary_key, zone_name))

            def _primary_rank_key(item: tuple[str, str]) -> tuple[int, str]:
                key, _ = item
                m = session.members.get(key, {})
                power = m.get("power")
                # Power-unknown primaries pair last (any eligible sub
                # is acceptable since there's no closest-power anchor).
                # 10**18 outranks any realistic squad power.
                return (power if power is not None else 10**18, key)

            primaries_with_zone.sort(key=_primary_rank_key)

            available_subs = list(pairing_candidates)
            for primary_key, primary_zone in primaries_with_zone:
                if not available_subs:
                    break
                primary_m = session.members.get(primary_key, {})
                primary_power = primary_m.get("power")
                effective_floor = _effective_floor_for_zone(session, primary_zone)
                eligible: list[str] = []
                for sub_key in available_subs:
                    if sub_key == primary_key:
                        continue
                    sub_power = session.members.get(sub_key, {}).get("power")
                    if sub_power is None:
                        continue
                    if sub_power >= effective_floor:
                        eligible.append(sub_key)
                if not eligible:
                    continue
                if primary_power is None:
                    # No anchor for closest-power. Use the strongest
                    # eligible sub so the unknown-power primary at least
                    # gets a backup.
                    eligible.sort(key=lambda sk: -(session.members[sk].get("power") or 0))
                    chosen_sub = eligible[0]
                else:

                    def _distance(sk: str) -> tuple[int, str]:
                        sp = session.members[sk].get("power") or 0
                        return (abs(sp - primary_power), sk)

                    eligible.sort(key=_distance)
                    chosen_sub = eligible[0]
                phase_pairings[primary_key] = chosen_sub
                available_subs.remove(chosen_sub)
                sub_m = session.members.get(chosen_sub, {})
                summary["auto_paired_subs"].append(
                    f"{primary_m.get('name', primary_key)} ↔ {sub_m.get('name', chosen_sub)}"
                )

        # ── 4b. Unpaired-sub reasons (#238) ──
        # After the pairing loop, identify subs whose power was below
        # the floor for every still-unpaired primary's zone. Those
        # subs end up in `session.subs` (the Available pool) with no
        # explanation pre-#238; populate `unpaired_subs_below_floor`
        # so the embed can surface "Couldn't pair Alice (60M) — power
        # below the 80M minimum for any remaining open positions."
        all_paired_sub_keys: set[str] = set()
        for ph in session.iter_phases():
            all_paired_sub_keys.update(session.paired_subs_for_phase(ph).values())
        unpaired_primary_floors: list[tuple[str, int]] = []
        for ph in session.iter_phases():
            ph_assigns = session.assignments_for_phase(ph)
            ph_pairings = session.paired_subs_for_phase(ph)
            for zone, primary_keys in ph_assigns.items():
                for pk in primary_keys:
                    if pk not in ph_pairings:
                        floor = _effective_floor_for_zone(session, zone)
                        if floor > 0:
                            unpaired_primary_floors.append((zone, floor))
        seen_unpaired: set[str] = set()
        for sub_key in pairing_candidates:
            if sub_key in all_paired_sub_keys:
                continue
            if sub_key in seen_unpaired:
                continue
            seen_unpaired.add(sub_key)
            sub_m = session.members.get(sub_key, {})
            sub_power = sub_m.get("power")
            if sub_power is None:
                continue  # Already in summary["gaps"].
            if not unpaired_primary_floors:
                continue  # No unpaired primaries — sub was just surplus.
            if all(sub_power < floor for _, floor in unpaired_primary_floors):
                summary["unpaired_subs_below_floor"].append(
                    {
                        "name": sub_m.get("name", sub_key),
                        "power": sub_power,
                        "min_floor": min(f for _, f in unpaired_primary_floors),
                    }
                )

    # ── 5. Spillover into session.subs ──
    # Plan-aware (#239): session.subs is exactly the plan's sub list
    # (intersected with the current pool). Non-plan members never spill
    # in — they aren't part of the in-game commitment, so dumping them
    # into the sub pool would contradict the officer's saved plan.
    # Legacy mode: everything power-known that didn't land in a zone or
    # a paired-sub seat ends up in the flat sub pool. In pool mode this
    # is the only surface for the 10 designated subs; in paired mode
    # it's overflow.
    assigned = session.assigned_member_keys()
    if plan_applied:
        session.subs = sorted(plan_sub_keys - assigned)
    else:
        for key, m in session.members.items():
            if key in assigned:
                continue
            if m.get("power") is None:
                # Already added to summary["gaps"] in step 2; skip so we
                # don't double-report.
                continue
            session.subs.append(key)

    session.selected_phase = original_phase
    session.auto_fill_summary = summary
    return summary


# ── Embed rendering ──────────────────────────────────────────────────────────


# Discord caps embed description at 4096 chars (the total embed cap
# is 6000). `_render_builder_embed` truncates with a notice when the
# composed description would exceed this — see #240 follow-up.
_MAX_EMBED_DESCRIPTION = 4096


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


def _format_zone_member_list(
    session: "RosterBuilderSession",
    member_keys: list[str],
    phase: int,
) -> str:
    """Render the comma-separated member list for one zone in one phase.

    Post-#222: just bare primary names. Pairings live in their own
    `### Auto-paired Subs:` section below the zone block, so inline
    ` + sub <name>` is gone; unpaired ⚠️ markers are gone too (the
    "Primaries without a designated Sub" message lists them).
    """
    names: list[str] = []
    for k in member_keys:
        m = session.members.get(k)
        names.append(m["name"] if m else f"<unknown:{k}>")
    return ", ".join(names) if names else "(empty)"


def _zone_minimum_suffix(session: RosterBuilderSession, zone_name: str) -> str:
    """Build a `_(minimum XM)_` suffix when the zone has a power-band
    floor > 0 (#238). Returns the empty string for zones without a
    rule so unrestricted zones stay visually clean."""
    from storm_strategy import format_power

    floor = _effective_floor_for_zone(session, zone_name)
    if floor and floor > 0:
        return f" _(minimum {format_power(floor)})_"
    return ""


def _render_zone_line(session: RosterBuilderSession, zone_name: str) -> str:
    """Render one zone's row in the builder embed.

    Flat presets: `[icon] Zone (n/cap): names`.

    Phase-aware presets (#172 / Rule L): zone-name header followed by
    one indented line per phase showing that phase's count, capacity,
    and member list.

    Post-#222: no status glyph (n/cap conveys state), no `←`
    selected-zone marker (the `Active zone:` line below shows it), no
    inline ` + sub <name>` or ⚠️ markers in the member list.

    #238: when a zone has a power-band floor, append
    ` _(minimum XM)_` to the zone name so officers can see the
    requirement next to the zone instead of having to cross-reference
    the Member Rules library.
    """
    z = session.preset.find_zone(zone_name)
    if z is None:
        return f"{zone_name} (?/?)"

    from storm_icons import zone_emoji_prefix

    icon = zone_emoji_prefix(zone_name)  # "" until #158 emojis upload.
    min_suffix = _zone_minimum_suffix(session, zone_name)

    if session.is_phase_aware:
        header = f"{icon}**{zone_name}**{min_suffix}"

        phases_seq = list(session.iter_phases())
        # Find first phase where this zone opens (cap > 0). Stages
        # BEFORE that are deliberately closed (DS / SF open at Stage
        # 2, VL at Stage 3) — hide them so officers don't see a noisy
        # `0/0` stub for stages the building isn't part of yet. Mirrors
        # the same skip-before-first-open rule the mail builder uses.
        first_open: int | None = None
        for p in phases_seq:
            if int(z.max_for_phase(p)) > 0:
                first_open = p
                break

        phase_lines: list[str] = []
        for p in phases_seq:
            if first_open is not None and p < first_open:
                continue
            members = session.assignments_for_phase(p).get(zone_name, [])
            cap = int(z.max_for_phase(p))
            count = len(members)
            names = _format_zone_member_list(session, members, phase=p)
            # Box-drawing prefix ("   └ ") visually nests the phase
            # row under the zone header without relying on Discord's
            # inconsistent leading-space rendering in embed bodies.
            phase_lines.append(f"   └ Stage {p}: {count}/{cap} · {names}")
        if not phase_lines:
            return header
        return "\n".join([header] + phase_lines)

    sel_count = session.zone_member_count(zone_name)
    sel_cap = int(z.max_players)
    member_keys = session.assignments_for_phase(session.selected_phase).get(zone_name, [])
    names_part = _format_zone_member_list(session, member_keys, phase=session.selected_phase)
    return f"{icon}**{zone_name}**{min_suffix} ({sel_count}/{sel_cap}): {names_part}"


def _render_builder_embed(session: RosterBuilderSession) -> discord.Embed:
    event_label = "Desert Storm" if session.event_type == "DS" else "Canyon Storm"
    title = f"🛡️ Roster Builder Template: {session.preset.name}"

    # Event + team line: `🗺️ Desert Storm: Team A` for DS, `🗺️ Canyon Storm:
    # <faction>` for CS with a faction, bare `🗺️ Canyon Storm` otherwise.
    if session.event_type == "DS":
        event_team_line = f"🗺️ {event_label}: Team {session.team}"
    elif session.preset.faction and session.preset.faction != "Either":
        event_team_line = f"🗺️ {event_label}: {session.preset.faction}"
    else:
        event_team_line = f"🗺️ {event_label}"

    lines: list[str] = []
    # #240 follow-up: when autosave to `storm_roster_drafts` fails,
    # surface a prominent warning at the top of the embed so the
    # officer doesn't unknowingly lose hours of work to a future View
    # timeout. Latched flag — clears on the next successful save.
    if getattr(session, "autosave_failed", False) and session.is_structured:
        lines.append(
            "⚠️ **Couldn't save your draft.** This build won't persist "
            "if the builder times out, you close it, or the bot "
            "restarts. Screenshot the embed below as a backup, or try "
            "any action (move a member, switch a stage) — the save "
            "will retry on every change."
        )
        lines.append("")
    lines.append(f"- {event_team_line}")
    if session.event_type == "DS":
        floor_label = "Min A" if session.team == "A" else "Min B"
        lines.append(f"- ⚖️ Enforcing {floor_label} for this team")
    # Phase-aware (#152): surface the active phase prominently so an
    # officer can see at a glance which phase the picker + assign
    # buttons will mutate.
    if session.is_phase_aware:
        lines.append(
            f"- 🔀 Editing Stage {session.selected_phase} _(use the Stage buttons below to switch)_"
        )
    lines.append("")

    if session.is_paired:
        lines.append("## 📋 Zones _(paired mode: each primary has a dedicated sub)_")
    else:
        lines.append("## 📋 Zones")
    for z in session.preset.zones:
        lines.append(_render_zone_line(session, z.zone))
    lines.append("")

    # ── Auto-paired Subs (#222) ──
    # Lifted out of the zone lines so pairings have a dedicated section.
    # Reads current state (not the auto-fill summary) so manual pairing
    # edits update the section live. Phase-aware: shows the selected
    # phase's pairings; officers switching phases see the swap.
    if session.is_paired:
        phase_pairings = session.paired_subs_for_phase(session.selected_phase)
        if phase_pairings:
            lines.append("### **Auto-paired Subs:**")
            for primary_key, sub_key in phase_pairings.items():
                primary_m = session.members.get(primary_key)
                sub_m = session.members.get(sub_key)
                primary_name = primary_m["name"] if primary_m else primary_key
                sub_name = sub_m["name"] if sub_m else sub_key
                lines.append(f"{primary_name} ↔ {sub_name}")
            lines.append("")

        unpaired = session.unpaired_primaries()
        if unpaired:
            unpaired_names = ", ".join(
                session.members[k]["name"] for k in unpaired if k in session.members
            )
            lines.append(f"Primaries without a designated Sub ({len(unpaired)}): {unpaired_names}.")
            lines.append(
                "Click 🔁 Pair subs to attach a sub to any of them. "
                "Subs may not cover every primary; that's expected."
            )
        # session.subs in paired mode = overflow only (members who
        # couldn't pair). Typically empty in the 30-signup case.
        sub_names = [session.members[k]["name"] for k in session.subs if k in session.members]
        if sub_names:
            lines.append(
                f"🪑 Available subs ({len(sub_names)}): "
                f"{', '.join(sub_names)}. Pair via 🔁 Pair subs "
                f"or leave as bench."
            )
    else:
        sub_names = [session.members[k]["name"] for k in session.subs if k in session.members]
        if sub_names:
            lines.append(f"🪑 Subs ({len(sub_names)}): {', '.join(sub_names)}")
        else:
            lines.append("🪑 Subs: _(none)_")
    lines.append("")

    # Phase-aware (Rule L / #172): per-phase counts in `Filled:`. Flat
    # presets keep the single total.
    if session.is_phase_aware:
        per_phase = []
        for p in session.iter_phases():
            assigned = sum(
                len(zone_members) for zone_members in session.assignments_for_phase(p).values()
            )
            cap = sum(int(z.max_for_phase(p)) for z in session.preset.zones)
            per_phase.append(f"S{p}: {assigned}/{cap}")
        lines.append(f"📊 Filled: {', '.join(per_phase)}")
    else:
        total_assigned = sum(
            len(zone_members) for zone_members in session.assignments_for_phase(1).values()
        )
        total_capacity = session.preset.total_capacity()
        lines.append(f"📊 Filled: {total_assigned} / {total_capacity}")

    selected = session.selected_zone
    if selected:
        from storm_icons import zone_emoji_prefix

        active_icon = zone_emoji_prefix(selected)
        preset_floor = session.floor_for_zone(selected)
        effective_floor = _effective_floor_for_zone(session, selected)
        from storm_strategy import format_power

        if effective_floor != preset_floor:
            # A power_band Member Rule lowered the effective minimum for
            # this zone. Surface both so leadership can tell at a glance
            # which rule is in play.
            lines.append(
                f"🎯 Active zone: {active_icon}{selected} · minimum "
                f"{format_power(effective_floor) if effective_floor else '(none)'} "
                f"_(preset minimum {format_power(preset_floor)} relaxed by power_band rule)_"
            )
        else:
            lines.append(
                f"🎯 Active zone: {active_icon}{selected} · minimum "
                f"{format_power(effective_floor) if effective_floor else '(none)'}"
            )
    has_unknown = any(m.get("power") is None for m in session.members.values())
    if has_unknown:
        lines.append(
            "_Members with no parseable power read as 'power unknown'. "
            "They can still be picked — you'll get a confirmation before "
            "the bot assigns them._"
        )

    if session.roster_errors:
        lines.append("")
        lines.append("⚠️ " + session.roster_errors[0])

    af = session.auto_fill_summary
    if af is not None:
        lines.append("")
        lines.append("## 🎯 Auto-fill summary")
        if af.get("starters_short", 0) > 0:
            # #219: surface short-signup counts up front so officers
            # see the gap before scanning the per-zone fill state.
            lines.append(
                f"- ⚠️ {af['starters_short']} of 20 starter seats unfilled (short on signups)."
            )
        lines.append(f"- Per-member rules applied: {af['per_member_rules_applied']}")
        lines.append(
            f"- Members slotted via a band-relaxed minimum: {af['power_band_rules_applied']}"
        )
        lines.append(f"- Auto-filled by power: {af['auto_filled_by_power']}")
        # Count only; the explicit `Primary ↔ Sub` list now lives in
        # the `### Auto-paired Subs:` section above, sourced from
        # current session state.
        paired = af.get("auto_paired_subs") or []
        lines.append(f"- Auto-paired subs: {len(paired)}")
        # #238: subs whose power was below the floor for every
        # remaining open primary zone get a dedicated warning line so
        # officers can see *why* they stayed in the Available pool.
        from storm_strategy import format_power as _fmt_pw

        for entry in af.get("unpaired_subs_below_floor") or []:
            lines.append(
                f"- ⚠️ Couldn't auto-pair **{entry['name']}** "
                f"({_fmt_pw(int(entry['power']))}). Their power "
                f"doesn't meet the minimum requirement "
                f"({_fmt_pw(int(entry['min_floor']))}) for any "
                f"remaining open positions."
            )
        # Decision #8 (#171): no truncation. Officers need every gap +
        # every conflict listed so they can make slotting decisions
        # manually. `(+N more)` hid exactly the entries they needed.
        if af["gaps"]:
            lines.append(
                f"- Gaps (power unknown, not slotted) ({len(af['gaps'])}): {', '.join(af['gaps'])}"
            )
        if af["conflicts"]:
            lines.append(f"- Conflicts ({len(af['conflicts'])}): {'; '.join(af['conflicts'])}")
        else:
            lines.append("- Conflicts: 0")
        not_on_discord_count = sum(1 for m in session.members.values() if m.get("not_on_discord"))
        lines.append(f"- Not on Discord: {not_on_discord_count}")

    # #8 (#240 follow-up): Discord caps embed description at 4096
    # chars. Roster errors + reconciliation banners + auto-fill
    # summary + zone lines can compound past that ceiling, and the
    # embed.set_message edit would fail (the builder would stop
    # updating). Defensively truncate the joined description so the
    # render always completes, and tell the officer what was clipped.
    description = "\n".join(lines)
    if len(description) > _MAX_EMBED_DESCRIPTION:
        # Leave room for a short truncation notice.
        budget = _MAX_EMBED_DESCRIPTION - 200
        description = (
            description[:budget].rsplit("\n", 1)[0]
            + "\n\n…\n⚠️ Builder details were too long to display in one "
            "embed — some lines were clipped. The underlying roster "
            "state is still intact; only the display was trimmed. "
            "Check your sheet's `Rosters` tab for the full record "
            "after Approve & Post."
        )

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.gold() if session.event_type == "DS" else discord.Color.orange(),
    )
    # #4 (#240 follow-up): subtle persistent reminder that the builder
    # auto-saves. Sits in the embed footer (small grey text below the
    # description) so officers don't worry their work is volatile.
    # Only shown for structured-mode builds — free-tier "manual apply"
    # has no draft to save.
    if session.is_structured:
        embed.set_footer(
            text=(
                "💾 Auto-saving as you go. Close anytime; resume from "
                "/desertstorm signups → ♻️ Resume Team X."
                if session.event_type == "DS"
                else "💾 Auto-saving as you go. Close anytime; resume from "
                "/canyonstorm signups → ♻️ Resume Team X."
            )
        )
    return embed


# ── Eligibility helpers ──────────────────────────────────────────────────────


def _effective_floor_for_zone(
    session: RosterBuilderSession,
    zone_name: str,
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
    session: RosterBuilderSession,
    zone_name: str,
) -> tuple[list[str], list[str]]:
    """Return (eligible_keys, below_floor_keys). Both lists exclude
    already-assigned members.

    `eligible` is members at or above the effective floor with a
    parseable power. `below_floor_keys` is everyone else (below the
    floor, or power unknown).

    The picker UI uses both lists: eligible members are shown plain,
    below-floor members are shown with a "below minimum" description.
    Picking a below-floor member surfaces an `_AssignConfirmView`
    confirm dialog — the bot doesn't gate the assign.

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
        # Bumped 900 → 3600 (15 min → 1 hour) after tester report
        # 2026-05-21: building a real roster — manual moves, auto-fill
        # iterations, pairing edits — easily took longer than 15 min and
        # the View timeout dropped the in-memory session, losing the
        # whole build. Proper fix is persistence (auto-save to SQLite);
        # 1 hour is the interim guardrail so officers have breathing
        # room while persistence ships.
        super().__init__(timeout=3600)
        self.session = session
        self.message: Optional[discord.Message] = None
        # `_user_action_since_open` (#240 follow-up): the initial
        # `_rebuild` during construction saves the freshly-built state
        # back to disk, which is wasted I/O AND creates a "draft" from
        # mere builder opens (officer opens, never edits, closes —
        # they get a Resume button next time labeled with a
        # 5-seconds-ago timestamp that doesn't represent real work).
        # The flag is False during __init__ so the initial `_rebuild`
        # skips the autosave; subsequent rebuilds (every one of which
        # flows through a user-clicked button) save normally.
        self._user_action_since_open: bool = False
        self._rebuild()

    def _rebuild(self) -> None:
        self.clear_items()
        s = self.session

        # Row layout adapts to phase-aware (#152) and structured-mode
        # post controls (#225). Discord caps a View at 5 ActionRows.
        #
        # Phase-aware flat layout — both modes:
        #   row 0: Stage nav buttons (phase-aware) OR zone select (flat)
        #
        # Structured-mode flat reshuffles so the "post tools" sit on a
        # dedicated row above the destructive Approve / Cancel actions
        # (per #225's drawn layout). Phase-aware structured can't add a
        # post_row (rows 0-4 are already used) so it keeps the
        # historical 4-row layout and the Approve button opens an
        # ephemeral picker for the with-image / text-only choice.
        if s.is_structured and not s.is_phase_aware:
            # Flat structured: 5 rows.
            zone_row = 0
            member_row = 1
            action_row = 2
            post_row = 3
            final_row = 4
        elif s.is_phase_aware:
            # Phase-aware (structured or free-tier): no post_row.
            zone_row = 1
            member_row = 2
            action_row = 3
            post_row = None
            final_row = 4
        else:
            # Flat free-tier: no post_row.
            zone_row = 0
            member_row = 1
            action_row = 2
            post_row = None
            final_row = 3

        # Row 0 — Phase navigation (phase-aware presets only). Walks
        # `iter_phases()` so 3-phase presets get a Phase 3 button.
        # Hardcoding `(1, 2)` left CS officers unable to edit Phase 3
        # manually even though auto-fill placed members there.
        if s.is_phase_aware:
            for phase in s.iter_phases():
                btn = discord.ui.Button(
                    label=f"Stage {phase}" + (" •" if phase == s.selected_phase else ""),
                    style=(
                        discord.ButtonStyle.primary
                        if phase == s.selected_phase
                        else discord.ButtonStyle.secondary
                    ),
                    row=0,
                )

                def _make_callback(p):
                    async def _on_phase(inter: discord.Interaction):
                        if not await self._guard_owner(inter):
                            return
                        s.selected_phase = p
                        await self._redraw(inter)

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
                min_values=1,
                max_values=1,
                options=zone_options,
                row=zone_row,
            )

            async def _on_zone(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                s.selected_zone = zone_select.values[0]
                s.show_below_floor = False
                await self._redraw(inter)

            zone_select.callback = _on_zone
            self.add_item(zone_select)

        # Row 1 — member picker. Below-floor members are ALWAYS in the
        # pool — leadership picks via confirmation, not via a hide/show
        # toggle. The bot stops being a gatekeeper.
        eligible, below = _eligible_member_keys_for_zone(s, s.selected_zone)
        pool = list(eligible) + list(below)
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
                options.append(
                    discord.SelectOption(
                        label=label,
                        value=value,
                        description=description,
                    )
                )
            placeholder = (
                f"Pick a member for {s.selected_zone or 'a zone'}…"
                if pool
                else "No members available for this zone."
            )
            # Surface overflow so the officer knows the dropdown is
            # truncated — Discord caps Select options at 25 and a
            # silent drop hides the rest of the eligible pool.
            overflow = len(pool) - len(options)
            if overflow > 0:
                placeholder = f"{placeholder} (+{overflow} more)"
            member_select = discord.ui.Select(
                placeholder=placeholder[:150],
                min_values=1,
                max_values=1,
                options=options,
                row=member_row,
            )

            async def _on_member(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                if not s.selected_zone:
                    await inter.response.send_message(
                        "⚠️ Pick a zone first.",
                        ephemeral=True,
                    )
                    return
                key = member_select.values[0]
                cap = s.zone_capacity(s.selected_zone)
                is_over_max = s.zone_member_count(s.selected_zone) >= cap
                is_below = key in below
                # Officers occasionally need to push a body over a
                # zone cap or assign someone below the power minimum.
                # Either condition surfaces an ephemeral confirm —
                # the bot is a tool, not a gatekeeper. Both conditions
                # together fold into a single confirm so leadership
                # doesn't see two sequential dialogs for the same
                # pick.
                if is_over_max or is_below:
                    member = s.members.get(key, {"name": key})
                    member_label = _format_member_label(member)
                    member_power = member.get("power")
                    floor_power = (
                        _effective_floor_for_zone(s, s.selected_zone) if is_below else None
                    )
                    prompt_lines = []
                    if is_over_max:
                        prompt_lines.append(
                            f"⚠️ **{s.selected_zone}** is already at the "
                            f"maximum of **{cap}** member(s)."
                        )
                    if is_below:
                        if member_power is None:
                            prompt_lines.append(
                                f"⚠️ **{member_label}** has no parseable "
                                f"power — they may not meet the minimum."
                            )
                        else:
                            from storm_strategy import format_power

                            floor_text = format_power(floor_power) if floor_power else "the minimum"
                            prompt_lines.append(
                                f"⚠️ **{member_label}**'s power is below "
                                f"**{s.selected_zone}**'s minimum "
                                f"({floor_text})."
                            )
                    prompt_lines.append(f"Do you want to assign **{member_label}** anyway?")
                    confirm = _AssignConfirmView(
                        parent_view=self,
                        member_key=key,
                        member_label=member_label,
                        zone=s.selected_zone,
                        phase=s.selected_phase,
                        over_max=is_over_max,
                        cap=cap,
                        below_floor=is_below,
                        member_power=member_power,
                        floor_power=floor_power,
                    )
                    await inter.response.send_message(
                        "\n".join(prompt_lines),
                        view=confirm,
                        ephemeral=True,
                    )
                    return
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
                await self._redraw(inter)

            member_select.callback = _on_member
            self.add_item(member_select)

        # Row 2 — action buttons. The 👁️ Show/Hide below-minimum
        # toggle was retired: below-floor members are always in the
        # picker, leadership picks via confirmation, not via a
        # hide/show toggle.

        # Edit-single-member affordance. Lists the zone's current
        # members + a destination select (other zones or 🗑️ Remove)
        # so officers can surgically move or remove one person without
        # nuking the whole zone. Disabled when the current zone is
        # empty — there's nothing to edit.
        zone_count = s.zone_member_count(s.selected_zone) if s.selected_zone else 0
        edit_btn = discord.ui.Button(
            label="✏️ Edit zone members",
            style=discord.ButtonStyle.secondary,
            row=action_row,
            disabled=(not s.selected_zone or zone_count == 0),
        )

        async def _edit_zone(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            if not s.selected_zone:
                await inter.response.send_message(
                    "⚠️ Pick a zone first.",
                    ephemeral=True,
                )
                return
            edit_view = _ZoneMemberEditView(
                parent_view=self,
                zone=s.selected_zone,
                phase=s.selected_phase,
            )
            await inter.response.send_message(
                f"✏️ Editing **{s.selected_zone}** — pick a member, then pick where to send them.",
                view=edit_view,
                ephemeral=True,
            )

        edit_btn.callback = _edit_zone
        self.add_item(edit_btn)

        unassign_btn = discord.ui.Button(
            label="🧹 Clear this zone",
            style=discord.ButtonStyle.secondary,
            row=action_row,
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
            await self._redraw(inter)

        unassign_btn.callback = _unassign
        self.add_item(unassign_btn)

        # 🪑 Manage subs (#274). Opens an ephemeral menu with both sub-pool
        # directions: bulk "Add all unassigned to Subs" (the old one-click
        # action) AND "Return a sub to available" so an officer can pull a
        # member back out of the sub pool and make them a starter — the
        # missing reverse that blocked starter/sub swaps. Combining them
        # under one button keeps the action_row within Discord's 5-button
        # cap (phase-aware structured paired mode is already full).
        manage_subs_btn = discord.ui.Button(
            label="🪑 Manage subs",
            style=discord.ButtonStyle.secondary,
            row=action_row,
        )

        async def _manage_subs(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            menu = _SubsManageView(parent_view=self)
            await inter.response.send_message(
                menu.render_content(),
                view=menu,
                ephemeral=True,
            )

        manage_subs_btn.callback = _manage_subs
        self.add_item(manage_subs_btn)

        # Pair subs button (paired mode only). Opens an ephemeral view
        # with a running pair list + Primary Select + Sub Select +
        # Assign + Done. Picking a pair writes immediately; the unpair
        # affordance is on the same view so officers can fix mistakes
        # without flipping screens. Replaces the per-primary auto-prompt
        # + separate Re-pair flow that #168 retired.
        if s.is_paired:
            pair_btn = discord.ui.Button(
                label="🔁 Pair subs",
                style=discord.ButtonStyle.secondary,
                row=action_row,
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
        # Auto-fill sits on post_row in flat-structured (#225) so the
        # destructive Approve / Cancel row stays clean; phase-aware
        # structured keeps it on action_row since post_row doesn't
        # exist there.
        auto_fill_row = post_row if (s.is_structured and post_row is not None) else action_row
        if s.is_structured:
            auto_fill_btn = discord.ui.Button(
                label="🎯 Auto-fill",
                style=discord.ButtonStyle.primary,
                row=auto_fill_row,
            )

            async def _run_auto_fill(
                inter: discord.Interaction,
                *,
                strategy: str = "balanced",
            ):
                """Actually execute the auto-fill + refresh. Called by
                the strategy picker after the officer chooses a
                strategy and (when relevant) accepts the destructive-
                rerun warning."""
                # Wrap the algorithm in explicit logging so when team-
                # test reports "auto-fill skipped after the first
                # power-unknown" we have a trail in Railway logs.
                logger.info(
                    "[STORM AUTO-FILL] start: guild=%s event=%s team=%s "
                    "preset=%s strategy=%s pool_size=%d (members=%s)",
                    s.guild_id,
                    s.event_type,
                    s.team,
                    s.preset.name,
                    strategy,
                    len(s.members),
                    [
                        {
                            "key": k,
                            "name": m.get("name"),
                            "discord_id": m.get("discord_id"),
                            "power": m.get("power"),
                            "not_on_discord": m.get("not_on_discord"),
                        }
                        for k, m in list(s.members.items())[:20]
                    ],
                )
                try:
                    summary = _auto_fill_session(s, strategy=strategy)
                except Exception as e:
                    logger.exception(
                        "[STORM AUTO-FILL] crashed for guild=%s event=%s: %s",
                        s.guild_id,
                        s.event_type,
                        e,
                    )
                    await inter.response.send_message(
                        f"⚠️ Auto-fill hit an unexpected error: `{type(e).__name__}: {str(e)[:120]}`. "
                        f"Please share this message with the bot maintainer; logs have details.",
                        ephemeral=True,
                    )
                    return
                logger.info(
                    "[STORM AUTO-FILL] done: guild=%s event=%s strategy=%s summary=%s "
                    "assignments=%s subs=%d paired=%d",
                    s.guild_id,
                    s.event_type,
                    strategy,
                    summary,
                    {z: names for z, names in s.assignments.items() if names},
                    len(s.subs),
                    len(s.paired_subs),
                )
                await self._redraw(inter)

            self._run_auto_fill = _run_auto_fill  # noqa — captured by the picker view

            async def _auto_fill(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                # #226: every Auto-fill click opens the strategy picker.
                # The picker carries (a) the two strategy buttons and
                # (b) the destructive-rerun warning copy when the
                # session already has assignments, so officers see one
                # ephemeral regardless of fresh vs rerun.
                picker_view = _AutoFillStrategyPickerView(parent_view=self)
                body = (
                    "## Auto-fill strategy\n"
                    "Pick how to distribute the 20 starters across "
                    "this team's zones:\n"
                    "\n"
                    "- ⚖️ **Balanced spread:** one starter per zone per "
                    "pass, power distributed across every zone.\n"
                    "- 💪 **Strength to priority:** send the strongest "
                    "members to the highest-priority zones first, keeping "
                    "power even between zones that share a priority."
                )
                if s.has_existing_assignments():
                    body = (
                        "⚠️ **Re-running auto-fill will reset every "
                        "assignment, sub pairing, and override on this "
                        "team.** Manual edits since the last auto-fill "
                        "will be lost.\n\n"
                    ) + body
                await inter.response.send_message(
                    body,
                    view=picker_view,
                    ephemeral=True,
                )
                try:
                    picker_view.message = await inter.original_response()
                except discord.HTTPException:
                    picker_view.message = None

            auto_fill_btn.callback = _auto_fill
            self.add_item(auto_fill_btn)

            # Approve & Post (#225). Flat-structured presents two
            # buttons on final_row so the officer can pick image vs
            # text-only in one click. Phase-aware structured can't fit
            # two buttons (rows 0-4 are full and the row-of-five limit
            # would force dropping another action), so it keeps one
            # Approve button that opens an ephemeral picker.
            if post_row is not None:
                approve_image_btn = discord.ui.Button(
                    label="🖼️ Approve & Post (with image)",
                    style=discord.ButtonStyle.success,
                    row=final_row,
                )

                async def _approve_with_image(inter: discord.Interaction):
                    if not await self._guard_owner(inter):
                        return
                    await _finalize_structured_roster(
                        inter,
                        self,
                        include_image=True,
                    )

                approve_image_btn.callback = _approve_with_image
                self.add_item(approve_image_btn)

                approve_text_btn = discord.ui.Button(
                    label="📄 Approve & Post (text only)",
                    style=discord.ButtonStyle.success,
                    row=final_row,
                )

                async def _approve_text_only(inter: discord.Interaction):
                    if not await self._guard_owner(inter):
                        return
                    await _finalize_structured_roster(
                        inter,
                        self,
                        include_image=False,
                    )

                approve_text_btn.callback = _approve_text_only
                self.add_item(approve_text_btn)
            else:
                approve_btn = discord.ui.Button(
                    label="✅ Approve & Post",
                    style=discord.ButtonStyle.success,
                    row=final_row,
                )

                async def _approve(inter: discord.Interaction):
                    if not await self._guard_owner(inter):
                        return
                    # Phase-aware: open the ephemeral picker.
                    picker = _ApprovePostPickerView(parent_view=self)
                    await inter.response.send_message(
                        "📬 **Approve & Post.** Pick how to post the roster:",
                        view=picker,
                        ephemeral=True,
                    )
                    try:
                        picker.message = await inter.original_response()
                    except discord.HTTPException:
                        picker.message = None

                approve_btn.callback = _approve
                self.add_item(approve_btn)

            preview_row = post_row if post_row is not None else final_row
            preview_btn = discord.ui.Button(
                label="📄 Preview mail",
                style=discord.ButtonStyle.secondary,
                row=preview_row,
            )

            async def _preview(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                await _send_mail_preview(inter, s)

            preview_btn.callback = _preview
            self.add_item(preview_btn)
        else:
            mail_btn = discord.ui.Button(
                label="📄 Generate mail",
                style=discord.ButtonStyle.primary,
                row=final_row,
            )

            async def _gen_mail(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                await _send_mail_preview(inter, s)

            mail_btn.callback = _gen_mail
            self.add_item(mail_btn)

            save_preset_btn = discord.ui.Button(
                label="💾 Save as preset",
                style=discord.ButtonStyle.success,
                row=final_row,
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
        # the button's clicked. In flat-structured mode the button sits
        # on post_row alongside Auto-fill / Preview mail (#225); other
        # modes keep it on final_row.
        render_label = (
            "🖼️ Generate DS assignments image"
            if s.event_type == "DS"
            else "🖼️ Generate CS assignments image"
        )
        render_row = post_row if post_row is not None else final_row
        render_btn = discord.ui.Button(
            label=render_label,
            style=discord.ButtonStyle.secondary,
            row=render_row,
        )

        async def _render(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            await _render_and_attach(inter, s)

        render_btn.callback = _render
        self.add_item(render_btn)

        # #240 follow-up: structured mode now persists the draft on
        # every action, so closing the builder doesn't lose work.
        # Button label switches from "❌ Cancel" (which implied
        # destruction) to "👋 Close (draft saved)" so officers know
        # they can come back via ♻️ Resume.
        cancel_label = "👋 Close (draft saved)" if s.is_structured else "✅ Done"
        done_btn = discord.ui.Button(
            label=cancel_label,
            style=discord.ButtonStyle.secondary if s.is_structured else discord.ButtonStyle.danger,
            row=final_row,
        )

        async def _done(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            for item in self.children:
                item.disabled = True
            if s.is_structured:
                hub_cmd = "/desertstorm signups" if s.event_type == "DS" else "/canyonstorm signups"
                close_msg = (
                    f"👋 Builder closed. **Your draft is saved** — "
                    f"come back via `{hub_cmd}` and click "
                    f"**♻️ Resume Team {s.team}** to pick up where "
                    f"you left off."
                )
            else:
                close_msg = "Roster builder closed."
            await inter.response.edit_message(
                content=close_msg,
                embed=_render_builder_embed(s),
                view=self,
            )
            self._release_session_lock()
            self.stop()

        done_btn.callback = _done
        self.add_item(done_btn)

        # Auto-save the draft (#240). `_rebuild` is the chokepoint
        # every state change flows through, so a write here covers
        # every move, auto-fill, pairing edit, phase switch, etc. The
        # save is best-effort — a SQLite hiccup logs but doesn't break
        # the UI.
        #
        # `_user_action_since_open` is False during the initial
        # __init__-driven rebuild, so the very first call skips the
        # save (the on-disk state already reflects this start point —
        # either freshly loaded from draft, or default-fresh). Every
        # subsequent rebuild is flowing through a user-clicked button
        # callback that flips the flag to True before calling
        # `_rebuild`, so real edits get saved.
        if self._user_action_since_open:
            _autosave_draft(self.session)

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.session.user_id:
            await inter.response.send_message(
                DENY_NOT_OWNER,
                ephemeral=True,
            )
            return False
        return True

    async def _redraw(self, inter: discord.Interaction) -> None:
        # `_redraw` is the chokepoint user-action button callbacks
        # call after mutating session state. Flip the user-action flag
        # so the autosave inside `_rebuild` fires (#240 follow-up:
        # skip the initial __init__-driven rebuild, save real edits).
        self._user_action_since_open = True
        self._rebuild()
        await inter.response.edit_message(
            embed=_render_builder_embed(self.session),
            view=self,
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
                s.guild_id,
                s.event_type,
                s.event_date or "",
                s.team or "",
            )
        except Exception as e:
            logger.warning(
                "[STORM BUILDER] release_storm_session failed for guild=%s event=%s/%s team=%s: %s",
                s.guild_id,
                s.event_type,
                s.event_date,
                s.team,
                e,
            )

    async def on_timeout(self) -> None:
        """Strip the view + release the session lock when the builder
        times out. Without this:
          - Buttons silently 404 with "Interaction failed" — same
            CLAUDE.md auto-post-view contract that other auto-posted
            views in this project respect.
          - The session lock would stick until process restart, which
            blocks legitimate re-opens for the same event indefinitely.

        Post-2026-05-21 tester report: also surface a clear "your
        builder timed out, your in-progress work was lost" message
        above the disabled buttons so officers don't blame the
        Interaction failed UX. The message points them at re-opening
        the builder. Persistence (auto-save to SQLite so re-opens
        recover state) is a follow-up issue.
        """
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(
                    content=(
                        "⏰ Roster builder timed out after 1 hour of "
                        "inactivity. In-progress assignments were lost; "
                        "re-open the builder to start over. Working on "
                        "a save-and-resume feature so this doesn't keep "
                        "happening."
                    ),
                    view=self,
                )
            except discord.HTTPException:
                pass
        self._release_session_lock()


class _ZoneMemberEditView(discord.ui.View):
    """Ephemeral picker for surgical edits to a single zone's roster
    (#251 tester ask). Replaces the "wipe entire zone + re-add"
    workflow with two specific actions:

      - 🗑️ Remove a single member from the zone.
      - ↔️ Move a single member to a different zone (same team /
        phase).

    Two selects + Apply / Cancel buttons. The destination select
    surfaces the destination's current count vs cap so officers see
    at a glance whether the move would push the destination over
    max — they make an informed choice without a separate confirm
    dialog (the Edit dialog itself IS the confirm surface).
    """

    REMOVE_VALUE = "__remove__"

    def __init__(
        self,
        *,
        parent_view: "RosterBuilderView",
        zone: str,
        phase: int,
    ):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.zone = zone
        self.phase = phase
        self.selected_member: Optional[str] = None
        self.selected_destination: Optional[str] = None
        self._build()

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.parent_view.session.user_id:
            await inter.response.send_message(
                DENY_NOT_OWNER,
                ephemeral=True,
            )
            return False
        return True

    def _build(self) -> None:
        self.clear_items()
        s = self.parent_view.session

        zone_members = list(s.assignments_for_phase(self.phase).get(self.zone, []))

        # Member select (row 0) — the zone's current members.
        if zone_members:
            member_options = []
            for k in zone_members[:25]:
                m = s.members.get(k, {"name": k})
                member_options.append(
                    discord.SelectOption(
                        label=_format_member_label(m)[:100],
                        value=k[:100],
                        default=(k == self.selected_member),
                    )
                )
            m_placeholder = (
                f"Picked: {self._picked_member_label()}"
                if self.selected_member
                else f"Pick a member from {self.zone}…"
            )
            m_select = discord.ui.Select(
                placeholder=m_placeholder[:150],
                options=member_options,
                min_values=1,
                max_values=1,
                row=0,
            )

            async def _on_member_pick(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                vals = inter.data.get("values") or []
                self.selected_member = vals[0] if vals else None
                # Picking a new member clears any prior destination
                # choice so officers don't accidentally re-apply a
                # stale destination.
                self.selected_destination = None
                self._build()
                try:
                    await inter.response.edit_message(view=self)
                except discord.HTTPException:
                    pass

            m_select.callback = _on_member_pick
            self.add_item(m_select)

        # Destination select (row 1) — only shown once a member is
        # picked. Includes "🗑️ Remove from zone" plus every other
        # zone on the team's preset with its current count/cap so
        # officers can spot over-cap destinations before clicking
        # Apply.
        if self.selected_member:
            dest_options = [
                discord.SelectOption(
                    label="🗑️ Remove (no destination zone)"[:100],
                    value=self.REMOVE_VALUE,
                    default=(self.selected_destination == self.REMOVE_VALUE),
                )
            ]
            for z in s.preset.zones:
                if z.zone == self.zone:
                    continue
                count = s.zone_member_count(z.zone)
                cap = s.zone_capacity(z.zone)
                cap_hint = f"({count}/{cap})"
                # Surface destination capacity so the officer sees
                # over-cap moves before clicking Apply.
                over_hint = " ⚠️ at cap" if count >= cap and cap > 0 else ""
                label = f"↔️ Move to {z.zone} {cap_hint}{over_hint}"
                dest_options.append(
                    discord.SelectOption(
                        label=label[:100],
                        value=z.zone[:100],
                        default=(self.selected_destination == z.zone),
                    )
                )
                if len(dest_options) >= 25:  # Discord cap
                    break

            d_placeholder = (
                f"Destination: {self.selected_destination}"
                if self.selected_destination
                else "Pick where to send them (or Remove)…"
            )
            d_select = discord.ui.Select(
                placeholder=d_placeholder[:150],
                options=dest_options,
                min_values=1,
                max_values=1,
                row=1,
            )

            async def _on_dest_pick(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                vals = inter.data.get("values") or []
                self.selected_destination = vals[0] if vals else None
                self._build()
                try:
                    await inter.response.edit_message(view=self)
                except discord.HTTPException:
                    pass

            d_select.callback = _on_dest_pick
            self.add_item(d_select)

        # Apply / Cancel (row 2).
        can_apply = self.selected_member is not None and self.selected_destination is not None
        apply_btn = discord.ui.Button(
            label="✅ Apply",
            style=discord.ButtonStyle.success,
            disabled=not can_apply,
            row=2,
        )
        apply_btn.callback = self._on_apply
        self.add_item(apply_btn)

        cancel_btn = discord.ui.Button(
            label="↩️ Cancel",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    def _picked_member_label(self) -> str:
        if not self.selected_member:
            return ""
        m = self.parent_view.session.members.get(
            self.selected_member,
            {"name": self.selected_member},
        )
        return _format_member_label(m)

    async def _on_apply(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        if self.is_finished():
            return
        s = self.parent_view.session
        member_key = self.selected_member or ""
        dest = self.selected_destination or ""
        if not member_key or not dest:
            return
        member = s.members.get(member_key, {"name": member_key})
        member_label = _format_member_label(member)

        # Remove from source zone first. The source slot list is the
        # canonical state; pruning stale overrides + pairings catches
        # the member's pair-sub mapping (if any) automatically.
        try:
            s.assignments_for_phase(self.phase)[self.zone].remove(member_key)
        except ValueError:
            pass  # already gone from source somehow — be defensive

        if dest == self.REMOVE_VALUE:
            ack = f"🗑️ Removed **{member_label}** from **{self.zone}**."
        else:
            s.assignments_for_phase(self.phase)[dest].append(member_key)
            ack = f"↔️ Moved **{member_label}** from **{self.zone}** to **{dest}**."

        s.prune_stale_overrides()
        s.prune_stale_pairings()
        s.auto_fill_summary = None
        self.stop()
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(content=ack, view=None)
        except discord.HTTPException:
            pass

        # Refresh the parent builder so the new state is visible.
        try:
            if self.parent_view.message is not None:
                self.parent_view._user_action_since_open = True
                self.parent_view._rebuild()
                await self.parent_view.message.edit(
                    embed=_render_builder_embed(s),
                    view=self.parent_view,
                )
        except discord.HTTPException:
            pass

    async def _on_cancel(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        if self.is_finished():
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(
                content="↩️ Edit cancelled. No changes made.",
                view=None,
            )
        except discord.HTTPException:
            pass


class _SubsManageView(discord.ui.View):
    """Ephemeral menu for the sub pool (#274 tester ask). Two actions in
    one place, opened by the main builder's 🪑 Manage subs button:

      - 🪑 Add all unassigned to Subs — bulk-sweep every member who isn't
        a primary and isn't already a sub into the sub pool (the old
        one-click behaviour, now living here).
      - ↩️ Return a sub to available — pull one member back OUT of the
        sub pool so they can be assigned as a starter again. Before this,
        moving to subs was a one-way trip: subs are hidden from the
        zone-assign picker (`assigned_member_keys_in_phase` unions
        `session.subs`), so an officer couldn't swap a starter and a sub.

    Returning a sub clears any pairing where they're the sub side, in
    every phase — `prune_stale_pairings` only drops pairings by the
    PRIMARY leaving a zone, so the sub-side cleanup is done explicitly.
    """

    def __init__(self, *, parent_view: "RosterBuilderView"):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.selected_sub: Optional[str] = None
        self._build()

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.parent_view.session.user_id:
            await inter.response.send_message(DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    def _unassigned_keys(self) -> list[str]:
        """Members eligible for the bulk add: in this team's pool, not a
        primary in any phase, not already a sub, and (paired mode) not
        already paired with a primary."""
        s = self.parent_view.session
        primaries: set[str] = set()
        for phase in s.iter_phases():
            for zone_members in s.assignments_for_phase(phase).values():
                primaries.update(zone_members)
        already_subs = set(s.subs)
        paired_keys = set(s.paired_subs.values()) if s.is_paired else set()
        return [
            key
            for key in s.members.keys()
            if key not in primaries and key not in already_subs and key not in paired_keys
        ]

    def render_content(self) -> str:
        s = self.parent_view.session
        sub_count = len(s.subs)
        unassigned = len(self._unassigned_keys())
        return (
            "🪑 **Manage subs**\n"
            f"Sub pool: **{sub_count}** | unassigned in pool: **{unassigned}**.\n\n"
            "- **Add all unassigned to Subs** moves everyone not already a "
            "primary or sub into the sub pool.\n"
            "- **Return a sub to available** pulls one member back out so "
            "you can make them a starter (then send the starter you're "
            "replacing to subs)."
        )

    def _build(self) -> None:
        self.clear_items()
        s = self.parent_view.session

        # Return-a-sub select (row 0) — only when there are subs.
        if s.subs:
            options = []
            for k in s.subs[:_MAX_DROPDOWN_OPTIONS]:
                m = s.members.get(k, {"name": k})
                options.append(
                    discord.SelectOption(
                        label=_format_member_label(m)[:100],
                        value=k[:100],
                        default=(k == self.selected_sub),
                    )
                )
            placeholder = (
                f"Picked: {self._picked_sub_label()}"
                if self.selected_sub
                else "Pick a sub to return to available…"
            )
            overflow = len(s.subs) - len(options)
            if overflow > 0:
                placeholder = f"{placeholder} (+{overflow} more)"
            sub_select = discord.ui.Select(
                placeholder=placeholder[:150],
                options=options,
                min_values=1,
                max_values=1,
                row=0,
            )

            async def _on_sub_pick(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                vals = inter.data.get("values") or []
                self.selected_sub = vals[0] if vals else None
                self._build()
                try:
                    await inter.response.edit_message(content=self.render_content(), view=self)
                except discord.HTTPException:
                    pass

            sub_select.callback = _on_sub_pick
            self.add_item(sub_select)

        # Action buttons (row 1).
        add_all_btn = discord.ui.Button(
            label="🪑 Add all unassigned to Subs",
            style=discord.ButtonStyle.secondary,
            row=1,
            disabled=not self._unassigned_keys(),
        )
        add_all_btn.callback = self._on_add_all
        self.add_item(add_all_btn)

        return_btn = discord.ui.Button(
            label="↩️ Return to available",
            style=discord.ButtonStyle.primary,
            row=1,
            disabled=self.selected_sub is None,
        )
        return_btn.callback = self._on_return_sub
        self.add_item(return_btn)

        done_btn = discord.ui.Button(
            label="✔ Done",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        done_btn.callback = self._on_done
        self.add_item(done_btn)

    def _picked_sub_label(self) -> str:
        if not self.selected_sub:
            return ""
        m = self.parent_view.session.members.get(
            self.selected_sub,
            {"name": self.selected_sub},
        )
        return _format_member_label(m)

    async def _refresh_parent(self) -> None:
        """Rebuild + re-render the main builder so pool/sub changes show."""
        s = self.parent_view.session
        if self.parent_view.message is not None:
            try:
                self.parent_view._user_action_since_open = True
                self.parent_view._rebuild()
                await self.parent_view.message.edit(
                    embed=_render_builder_embed(s),
                    view=self.parent_view,
                )
            except discord.HTTPException:
                pass

    async def _on_add_all(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        s = self.parent_view.session
        to_move = self._unassigned_keys()
        if not to_move:
            await inter.response.send_message(
                "⚠️ No unassigned members to move. Everyone in this team's "
                "pool is already assigned as a primary or sub.",
                ephemeral=True,
            )
            return
        s.subs.extend(to_move)
        s.prune_stale_overrides()
        s.prune_stale_pairings()
        s.auto_fill_summary = None
        self.selected_sub = None
        self._build()
        try:
            await inter.response.edit_message(content=self.render_content(), view=self)
        except discord.HTTPException:
            pass
        await self._refresh_parent()

    async def _on_return_sub(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        s = self.parent_view.session
        key = self.selected_sub
        if not key or key not in s.subs:
            self.selected_sub = None
            self._build()
            try:
                await inter.response.edit_message(content=self.render_content(), view=self)
            except discord.HTTPException:
                pass
            return
        s.subs.remove(key)
        # Clear any pairing where this member is the SUB side, every phase.
        for phase in s.iter_phases():
            pairings = s.paired_subs_for_phase(phase)
            for primary in [p for p, sub in pairings.items() if sub == key]:
                del pairings[primary]
        s.prune_stale_overrides()
        s.auto_fill_summary = None
        self.selected_sub = None
        self._build()
        try:
            await inter.response.edit_message(content=self.render_content(), view=self)
        except discord.HTTPException:
            pass
        await self._refresh_parent()

    async def _on_done(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(
                content="✔ Done managing subs.",
                view=None,
            )
        except discord.HTTPException:
            pass


class _AssignConfirmView(discord.ui.View):
    """Ephemeral yes/no confirm for assigning a member to a zone when
    one or both rule violations would otherwise block the assign:

      - `over_max`: the zone is already at its `max_players` cap
      - `below_floor`: the member's power is below the zone's
        effective minimum (or the member's power is unknown)

    Tester feedback: officers occasionally need to push a body into
    a zone that's already maxed out, or assign someone who comes
    short of the minimum. The bot shouldn't hard-block — let
    leadership knowingly override and capture the audit trail.

    Yes → assigns the member, marks the below-floor override flag
          when applicable (so audit / rendering still surfaces "this
          person was assigned despite being under the floor"),
          refreshes the parent view, dismisses the ephemeral.
    No  → just dismisses; parent state unchanged.

    Both `over_max` and `below_floor` can be true simultaneously —
    the dialog text covers both conditions in one ephemeral so
    leadership doesn't see two sequential confirms for the same
    pick.
    """

    def __init__(
        self,
        *,
        parent_view: "RosterBuilderView",
        member_key: str,
        member_label: str,
        zone: str,
        phase: int,
        over_max: bool,
        cap: int,
        below_floor: bool,
        member_power: int | None = None,
        floor_power: int | None = None,
    ):
        super().__init__(timeout=120)
        self.parent_view = parent_view
        self.member_key = member_key
        self.member_label = member_label
        self.zone = zone
        self.phase = phase
        self.over_max = over_max
        self.cap = cap
        self.below_floor = below_floor
        self.member_power = member_power
        self.floor_power = floor_power

        # Buttons built imperatively (not via @discord.ui.button) so the
        # callbacks remain regular methods callable from unit tests.
        yes_btn = discord.ui.Button(
            label="✅ Yes, assign anyway",
            style=discord.ButtonStyle.danger,
            row=0,
        )
        yes_btn.callback = self.yes
        self.add_item(yes_btn)

        no_btn = discord.ui.Button(
            label="↩️ No, cancel",
            style=discord.ButtonStyle.secondary,
            row=0,
        )
        no_btn.callback = self.no
        self.add_item(no_btn)

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.parent_view.session.user_id:
            await inter.response.send_message(
                DENY_NOT_OWNER,
                ephemeral=True,
            )
            return False
        return True

    async def yes(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        if self.is_finished():
            return
        self.stop()
        for item in self.children:
            item.disabled = True

        s = self.parent_view.session
        # Below-floor overrides still drive the audit trail and the
        # embed rendering (e.g. zone line marks who was assigned
        # under the floor). Over-max has no separate audit set in
        # v1 — the embed already shows zone counts as `(N/cap)`, so
        # a 5/4 reads as obviously over.
        if self.below_floor:
            s.below_floor_overrides_for_phase(self.phase).add(self.member_key)
        s.assignments_for_phase(self.phase)[self.zone].append(self.member_key)
        s.auto_fill_summary = None

        # Build the ack copy matching what was overridden.
        reasons = []
        if self.over_max:
            reasons.append(f"over the maximum of {self.cap}")
        if self.below_floor:
            reasons.append("below the minimum power")
        ack_reason = " and ".join(reasons) if reasons else "with overrides"

        try:
            await inter.response.edit_message(
                content=(
                    f"✅ Added **{self.member_label}** to **{self.zone}** "
                    f"({ack_reason}). Builder above is updated."
                ),
                view=None,
            )
        except discord.HTTPException:
            pass

        # Refresh the main builder via its captured message handle.
        try:
            if self.parent_view.message is not None:
                self.parent_view._user_action_since_open = True
                self.parent_view._rebuild()
                await self.parent_view.message.edit(
                    embed=_render_builder_embed(s),
                    view=self.parent_view,
                )
        except discord.HTTPException:
            pass

    async def no(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        if self.is_finished():
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(
                content=CANCEL_BACKPEDAL.format(
                    detail=f"**{self.member_label}** was not added to **{self.zone}**.",
                ),
                view=None,
            )
        except discord.HTTPException:
            pass


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


class _AutoFillStrategyPickerView(discord.ui.View):
    """Strategy picker for the Auto-fill button (#226).

    The Auto-fill button always opens this picker. Officers pick one
    of two strategies, each described in the picker's body copy:

      🎯 Balanced spread      — one starter per zone per pass.
      🔝 Strength to priority — fill top-priority zones first.

    A third button cancels without running. When the parent session
    already has assignments, the parent builder prepends a
    destructive-rerun warning to the body so officers see one
    ephemeral regardless of fresh vs rerun (the picker absorbs the
    role of the previous `_AutoFillConfirmView`).

    Each strategy button runs the parent's `_run_auto_fill` with the
    matching `strategy` kwarg and then refreshes the main builder
    view via its captured message handle.
    """

    def __init__(self, *, parent_view: "RosterBuilderView"):
        super().__init__(timeout=120)
        self.parent_view = parent_view
        self.message: Optional[discord.Message] = None

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.parent_view.session.user_id:
            await inter.response.send_message(
                DENY_NOT_OWNER,
                ephemeral=True,
            )
            return False
        return True

    async def _run_with_strategy(
        self,
        inter: discord.Interaction,
        strategy: str,
        label: str,
    ) -> None:
        if not await self._guard_owner(inter):
            return
        if self.is_finished():
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        s = self.parent_view.session
        try:
            summary = _auto_fill_session(s, strategy=strategy)
        except Exception as e:
            logger.exception(
                "[STORM AUTO-FILL] picker-path crashed for guild=%s event=%s: %s",
                s.guild_id,
                s.event_type,
                e,
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
            "[STORM AUTO-FILL] picker-path done: guild=%s event=%s strategy=%s summary=%s",
            s.guild_id,
            s.event_type,
            strategy,
            summary,
        )
        # Collapse the picker to a brief "done — look up" line and
        # remove the buttons so the officer's eye is pulled to the
        # main builder view (which we're about to refresh). Editing
        # with view=None is the safe ephemeral-dismissal path across
        # discord.py versions; full delete-on-ephemeral has
        # inconsistent semantics for component-attached messages.
        try:
            await inter.response.edit_message(
                content=(f"✅ {label} complete. Builder above is updated."),
                view=None,
            )
        except discord.HTTPException:
            pass
        # Refresh the main builder view via its captured message handle,
        # not through this interaction. The ephemeral picker and the
        # main builder live on separate messages. Flip the user-action
        # flag so the autosave fires (#240 follow-up).
        try:
            if self.parent_view.message is not None:
                self.parent_view._user_action_since_open = True
                self.parent_view._rebuild()
                await self.parent_view.message.edit(
                    embed=_render_builder_embed(s),
                    view=self.parent_view,
                )
        except discord.HTTPException:
            pass

    # Primary style on both strategy buttons. The new ⚖️ / 💪 glyphs
    # read fine against the blue background (the earlier 🎯 / 🔝
    # rendered washed-out at small sizes). All three buttons fit on a
    # single row.
    @discord.ui.button(label="⚖️ Balanced spread", style=discord.ButtonStyle.primary, row=0)
    async def balanced(self, inter: discord.Interaction, _btn: discord.ui.Button):
        await self._run_with_strategy(
            inter,
            "balanced",
            "⚖️ Balanced spread auto-fill",
        )

    @discord.ui.button(label="💪 Strength to priority", style=discord.ButtonStyle.primary, row=0)
    async def priority_greedy(self, inter: discord.Interaction, _btn: discord.ui.Button):
        await self._run_with_strategy(
            inter,
            "priority_greedy",
            "💪 Strength-to-priority auto-fill",
        )

    @discord.ui.button(label="↩️ Cancel Auto-fill", style=discord.ButtonStyle.secondary, row=0)
    async def cancel(self, inter: discord.Interaction, _btn: discord.ui.Button):
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


async def _drop_approve_picker(inter: discord.Interaction) -> None:
    """Drop the Approve & Post picker before the finalize flow takes
    over. The finalize step posts the mail to channel + sends its own
    ephemeral result ack, both of which are better as the most-recent
    visible messages than a disabled picker sitting above them. Defers
    the interaction first so subsequent followup.send calls inside the
    finalize step still work."""
    try:
        await inter.response.defer()
    except discord.HTTPException:
        pass
    try:
        await inter.delete_original_response()
    except discord.HTTPException:
        pass


class _ApprovePostPickerView(discord.ui.View):
    """Phase-aware-only fallback for the Approve & Post choice (#225).

    Flat-structured presets show two main-view buttons (Approve with
    image / Approve text only) so the officer picks in one click. Phase-
    aware structured can't fit two main-view buttons (Discord caps a
    View at 5 ActionRows and phase-aware already uses all 5), so the
    single Approve button opens this ephemeral picker instead.
    """

    def __init__(self, *, parent_view: "RosterBuilderView"):
        super().__init__(timeout=120)
        self.parent_view = parent_view
        self.message: Optional[discord.Message] = None

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.parent_view.session.user_id:
            await inter.response.send_message(
                DENY_NOT_OWNER,
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="🖼️ With image", style=discord.ButtonStyle.success)
    async def with_image(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard_owner(inter):
            return
        if self.is_finished():
            return
        self.stop()
        await _drop_approve_picker(inter)
        await _finalize_structured_roster(
            inter,
            self.parent_view,
            include_image=True,
        )

    @discord.ui.button(label="📄 Text only", style=discord.ButtonStyle.success)
    async def text_only(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard_owner(inter):
            return
        if self.is_finished():
            return
        self.stop()
        await _drop_approve_picker(inter)
        await _finalize_structured_roster(
            inter,
            self.parent_view,
            include_image=False,
        )

    @discord.ui.button(label="↩️ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, _btn: discord.ui.Button):
        if not await self._guard_owner(inter):
            return
        if self.is_finished():
            return
        self.stop()
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(
                content="↩️ Approve cancelled. Roster not posted.",
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
            content=view.render_content(),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()
    except discord.HTTPException as e:
        logger.warning(
            "[STORM BUILDER] pair-subs view failed to send (guild=%s): %s",
            s.guild_id,
            e,
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
                f"• **{p}** → **{s_name}**  _({zone})_" for p, s_name, zone in pair_rows
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
            unpaired_line = f"\n\n⚠️ **Unpaired primaries ({len(unpaired)}):** {unpaired_block}"
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
                options.append(
                    discord.SelectOption(
                        label=name[:100],
                        value=primary_key[:100],
                        description=zone[:100],
                        default=(primary_key == self.selected_primary),
                    )
                )
            primary_select = discord.ui.Select(
                placeholder="Pick an unpaired primary…",
                min_values=1,
                max_values=1,
                options=options,
                row=0,
            )

            async def _on_primary(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                self.selected_primary = primary_select.values[0]
                self._build_components()
                try:
                    await inter.response.edit_message(
                        content=self.render_content(),
                        view=self,
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
                options.append(
                    discord.SelectOption(
                        label=_format_member_label(m)[:100],
                        value=sub_key[:100],
                        default=(sub_key == self.selected_sub),
                    )
                )
            if options:
                sub_select = discord.ui.Select(
                    placeholder="Pick a sub…",
                    min_values=1,
                    max_values=1,
                    options=options,
                    row=1,
                )

                async def _on_sub(inter: discord.Interaction):
                    if not await self._guard_owner(inter):
                        return
                    self.selected_sub = sub_select.values[0]
                    self._build_components()
                    try:
                        await inter.response.edit_message(
                            content=self.render_content(),
                            view=self,
                        )
                    except discord.HTTPException:
                        pass

                sub_select.callback = _on_sub
                self.add_item(sub_select)

        assign_btn = discord.ui.Button(
            label="✅ Assign pair",
            style=discord.ButtonStyle.primary,
            row=2,
            disabled=not (self.selected_primary and self.selected_sub),
        )
        assign_btn.callback = self._on_assign
        self.add_item(assign_btn)

        unpair_btn = discord.ui.Button(
            label="🔄 Unpair…",
            style=discord.ButtonStyle.secondary,
            row=2,
            disabled=not bool(self._phase_pairings()),
        )
        unpair_btn.callback = self._enter_unpair_mode
        self.add_item(unpair_btn)

        done_btn = discord.ui.Button(
            label="✔ Done",
            style=discord.ButtonStyle.secondary,
            row=2,
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
                min_values=1,
                max_values=1,
                options=options,
                row=0,
            )

            async def _on_pick(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                self.selected_unpair_primary = unpair_select.values[0]
                self._build_components()
                try:
                    await inter.response.edit_message(
                        content=self.render_content(),
                        view=self,
                    )
                except discord.HTTPException:
                    pass

            unpair_select.callback = _on_pick
            self.add_item(unpair_select)

        confirm_btn = discord.ui.Button(
            label="🔄 Confirm unpair",
            style=discord.ButtonStyle.danger,
            row=1,
            disabled=not self.selected_unpair_primary,
        )
        confirm_btn.callback = self._on_confirm_unpair
        self.add_item(confirm_btn)

        back_btn = discord.ui.Button(
            label="↩️ Back",
            style=discord.ButtonStyle.secondary,
            row=1,
        )
        back_btn.callback = self._exit_unpair_mode
        self.add_item(back_btn)

    # ── Callbacks ────────────────────────────────────────────────────
    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.main_view.session.user_id:
            await inter.response.send_message(
                DENY_NOT_OWNER,
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
                content=self.render_content(),
                view=self,
            )
        except discord.HTTPException:
            pass
        # Re-render the main view so the new pairing is visible there too.
        try:
            if self.main_view.message:
                self.main_view._rebuild()
                await self.main_view.message.edit(
                    embed=_render_builder_embed(s),
                    view=self.main_view,
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
                content=self.render_content(),
                view=self,
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
                content=self.render_content(),
                view=self,
            )
        except discord.HTTPException:
            pass

    async def _on_confirm_unpair(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        if not self.selected_unpair_primary:
            await inter.response.send_message(
                "⚠️ Pick a pair to unpair.",
                ephemeral=True,
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
                content=self.render_content(),
                view=self,
            )
        except discord.HTTPException:
            pass
        try:
            if self.main_view.message:
                self.main_view._rebuild()
                await self.main_view.message.edit(
                    embed=_render_builder_embed(s),
                    view=self.main_view,
                )
        except discord.HTTPException:
            pass

    async def _on_done(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        self.stop()
        # Drop the picker on Done — the builder above already reflects
        # the final pairings (each Assign/Unpair edits it in-place), so
        # leaving a disabled picker below the builder just buries the
        # actionable view. Disable buttons first as a fallback in case
        # the delete fails (rate limit, expired token, etc.).
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.defer()
        except discord.HTTPException:
            pass
        if self.message is not None:
            try:
                await self.message.delete()
            except discord.HTTPException:
                # Fallback: at least disable the buttons so the picker
                # can't be re-clicked.
                try:
                    await self.message.edit(view=self)
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
            required=True,
            max_length=60,
        )
        self.add_item(self.preset_name)

    async def on_submit(self, inter: discord.Interaction):
        name = (self.preset_name.value or "").strip()
        if not name:
            await inter.response.send_message(
                "⚠️ Preset name is required.",
                ephemeral=True,
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
            new_zones.append(
                ss.ZoneRow(
                    zone=z.zone,
                    max_players=(flat_count if flat_count > 0 else int(z.max_players)),
                    max_phase1=_cap_for(1),
                    max_phase2=_cap_for(2),
                    max_phase3=_cap_for(3),
                    min_power_a=int(z.min_power_a or 0),
                    min_power_b=int(z.min_power_b or 0),
                    priority=int(z.priority or 0),
                    priority_phase1=int(z.priority_phase1 or 0),
                    priority_phase2=int(z.priority_phase2 or 0),
                    priority_phase3=int(z.priority_phase3 or 0),
                )
            )
        buf = ss.PresetBuffer(
            name=name,
            event_type=s.event_type,
            zones=new_zones,
            faction=s.preset.faction,
            phase_count=s.preset.phase_count,
        )
        ok = await asyncio.to_thread(
            ss.save_preset,
            s.guild_id,
            s.event_type,
            buf,
        )
        if ok:
            await inter.response.send_message(
                f"✅ Saved roster as preset **{name}**.",
                ephemeral=True,
            )
        else:
            await inter.response.send_message(
                "⚠️ Couldn't save preset. Check that your Sheet is configured "
                "and the bot has edit access.",
                ephemeral=True,
            )


# ── Mail generation ──────────────────────────────────────────────────────────


_MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024
# Discord caps regular message content (the `content` field, not
# attachments) at 2000 characters. Anything longer must be split or
# attached as a file.
_MAX_MESSAGE_CONTENT = 2000


async def _render_and_attach(
    inter: discord.Interaction,
    session: RosterBuilderSession,
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
            session.guild_id,
            e,
        )
        return

    try:
        import storm_renderer

        roster_data = storm_renderer.roster_from_session(session)
        png_bytes = await asyncio.to_thread(storm_renderer.render, roster_data)
    except RuntimeError as e:
        logger.warning(
            "[STORM RENDER] Pillow not available (guild=%s event=%s): %s",
            session.guild_id,
            session.event_type,
            e,
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
            session.guild_id,
            session.event_type,
            e,
        )
        await inter.followup.send(
            "⚠️ Couldn't render the roster image. See bot logs.",
            ephemeral=True,
        )
        return

    if len(png_bytes) > _MAX_ATTACHMENT_BYTES:
        logger.warning(
            "[STORM RENDER] PNG exceeded 25MB (size=%d guild=%s event=%s)",
            len(png_bytes),
            session.guild_id,
            session.event_type,
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
                getattr(inter.channel, "id", None),
                session.guild_id,
            )
            # Fall through to ephemeral-only delivery so the officer
            # still gets the image — they just don't get the public copy.
        except discord.HTTPException as e:
            logger.warning(
                "[STORM RENDER] failed to post public image (guild=%s): %s",
                session.guild_id,
                e,
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
        content=("🖼️ Roster image posted above. Pick an action below; only you'll see this prompt."),
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
        self,
        *,
        owner_id: int,
        png_bytes: bytes,
        filename: str,
        guild_id: int,
        event_type: str,
        event_date: str,
        team: str,
        public_channel_id: int,
        public_message_id: int,
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
        self,
        inter: discord.Interaction,
        _btn: discord.ui.Button,
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
                inter.user.id,
                self.guild_id,
                e,
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
        self,
        inter: discord.Interaction,
        _btn: discord.ui.Button,
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
                self.guild_id,
                self.event_type,
                self.event_date,
                self.team,
                self.public_channel_id,
                self.public_message_id,
                inter.user.id,
            )
        except Exception as e:
            logger.exception(
                "[STORM RENDER] save_roster_image_ref failed guild=%s: %s",
                self.guild_id,
                e,
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
        self,
        inter: discord.Interaction,
        _btn: discord.ui.Button,
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
        self,
        *,
        owner_id: int,
        png_bytes: bytes,
        filename: str,
        event_type: str,
        event_date: str,
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
            min_values=1,
            max_values=1,
        )

        async def _on_pick(picker_inter: discord.Interaction):
            if picker_inter.user.id != self.owner_id:
                await picker_inter.response.send_message(
                    "⛔ Not for you.",
                    ephemeral=True,
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
        self,
        *,
        channel_id: int,
        channel_mention: str,
        png_bytes: bytes,
        filename: str,
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
                self.channel_id,
                e,
            )
            await inter.response.send_message(
                f"⚠️ Discord refused the post: {e}.",
                ephemeral=True,
            )
            return
        await inter.response.send_message(
            f"📢 Posted to {self.channel_mention}.",
            ephemeral=True,
        )


def _mail_zone_and_sub_lists(
    session: RosterBuilderSession,
    phase: int = 1,
) -> tuple[dict[str, list[str]], list[str]]:
    """Return `(zones_for_mail, sub_names)` honoring the session's
    sub_mode.

    Pool mode: zones carry bare primary names; `sub_names` is the flat
    overflow pool from `session.subs`.

    Paired mode (#224): zones still carry bare primary names (the
    inline ` + sub <name>` is gone). `sub_names` carries the pairings
    formatted as `Primary ↔ Sub` lines first, followed by any
    overflow sub names. Matches the embed shape rolled out in #222 so
    the mail and the embed read the same way.

    `phase` (#152): 1 or 2 or 3. Selects which phase's assignments and
    paired-sub map to render. Flat presets ignore this and always
    return phase-1 data. The global sub pool is event-level (not
    per-phase) so the overflow names only attach to the phase-1
    return; later phases append only their own per-phase pairing
    list so subs aren't duplicated across blocks.
    """
    zones_for_mail: dict[str, list[str]] = {}
    is_paired = session.sub_mode == "paired"
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
            names.append(m["name"])
        if names:
            zones_for_mail[zone_name] = names

    # Paired-mode pairings render as `Primary ↔ Sub` lines. Each
    # phase carries its own pairing dict, so phase-aware mail emits
    # the right pairings per block. Overflow flat-list names append
    # after, but only on phase 1's return (sub pool is event-level).
    sub_names: list[str] = []
    paired_sub_keys: set[str] = set()
    if is_paired:
        # Collect every key that's been paired across ALL phases —
        # paired subs stay in `session.subs` too (the pairing layer
        # doesn't move them out), so we have to suppress them from
        # the overflow list below or they double-render as both a
        # `Primary ↔ Sub` line AND a bare name.
        if session.is_phase_aware:
            for p in session.iter_phases():
                paired_sub_keys.update(session.paired_subs_for_phase(p).values())
        else:
            paired_sub_keys.update(pairings.values())
        for primary_key, sub_key in pairings.items():
            primary_m = session.members.get(primary_key)
            sub_m = session.members.get(sub_key)
            if primary_m is None or sub_m is None:
                continue
            sub_names.append(f"{primary_m['name']} ↔ {sub_m['name']}")
    if phase == 1:
        for k in session.subs:
            if k in paired_sub_keys:
                continue
            m = session.members.get(k)
            if m is not None:
                sub_names.append(m["name"])
    return zones_for_mail, sub_names


def _build_mail_for_phase(
    session: RosterBuilderSession,
    phase: int,
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


def _build_zone_grouped_block(session: RosterBuilderSession) -> str:
    """Zones block grouped BY ZONE with each zone's content stacked
    inside. Matches the PNG render's organization so the mail and
    image read consistently. Handles BOTH flat and phase-aware
    presets (DS + CS):

      Flat preset (one stage):
          **Zone Name**:
              Alice
              Bob

      Phase-aware preset (multi-stage):
          **Zone Name**:
          Stage 1
              Alice
              Bob
          Stage 2
              (empty)
          Stage 3
              Carol

    Empty stages (cap > 0 in that stage but no assignees) render as
    `(empty)` so officers reading the mail can see deliberate gaps
    — key to strategies that intentionally leave a stage open.
    Closed stages (cap=0) are hidden EXCEPT when the entire zone is
    unused, in which case the zone is omitted from the mail entirely
    (otherwise every closed canonical preset zone would clutter the
    mail with "(empty)" everywhere).

    Replaces the prior per-phase template repetition that ballooned
    3-stage CS mails — the template's greeting + subs + time were
    rendered once PER PHASE, pushing the mail past Discord's
    2000-char limit on rosters that fit comfortably otherwise.
    """
    from storm_icons import zone_emoji_prefix

    is_phase_aware = session.is_phase_aware
    phases = list(session.iter_phases()) if is_phase_aware else [1]

    # Build a {zone: {phase: [members]}} pivot from the assignment
    # data, so each zone's stage stack renders in one zone-major
    # block matching the PNG render.
    by_zone: dict[str, dict[int, list[str]]] = {}
    for phase in phases:
        zones_for_phase, _subs = _mail_zone_and_sub_lists(
            session,
            phase=phase,
        )
        for zone, members in zones_for_phase.items():
            if not members:
                continue
            by_zone.setdefault(zone, {})[phase] = list(members)

    # Indent for assignee names under each stage header (or under
    # the zone header for flat presets). 4 spaces reads clearly in
    # monospaced Discord copies and survives the template's {zones}
    # substitution.
    indent = "    "

    blocks: list[str] = []
    rendered: set[str] = set()

    def _emit_zone(zone_label: str) -> None:
        """Build the per-zone block. Phase-aware presets emit
        Stage N sub-headers within the zone block; flat presets
        emit members directly under the zone."""
        phase_members = by_zone.get(zone_label, {})
        zone_has_members = bool(phase_members)

        def _cap_for(p: int) -> int:
            try:
                return int(session.zone_capacity(zone_label, phase=p))
            except Exception:
                return 0

        # Find the first phase where the zone opens (cap > 0). Stages
        # BEFORE that are deliberately closed (e.g., Defense Systems
        # opens at Stage 2; Virus Lab at Stage 3), so hide them
        # entirely. Stages from first-open onward render either
        # member names or `(empty)` so officers can see deliberate
        # gaps — matches the PNG image so the mail and image read
        # consistently.
        first_open: int | None = None
        for p in phases:
            if _cap_for(p) > 0:
                first_open = p
                break

        # Skip the zone entirely if every stage is closed and empty
        # (fully unused — listing it would just clutter the mail).
        if first_open is None and not zone_has_members:
            return

        inner_lines: list[str] = []
        for phase in phases:
            if first_open is not None and phase < first_open:
                # Building not open yet at this stage — hide.
                continue
            members = phase_members.get(phase, [])
            if is_phase_aware:
                inner_lines.append(f"Stage {phase}")
            if members:
                inner_lines.extend(f"{indent}{m}" for m in members)
            elif is_phase_aware:
                # Only phase-aware presets surface `(empty)`
                # markers — flat presets with no assignees just
                # render nothing (zone skipped via the first_open
                # check above when truly unused).
                inner_lines.append(f"{indent}(empty)")

        if not inner_lines:
            return
        zone_block = [
            f"{zone_emoji_prefix(zone_label)}**{zone_label}**:",
        ]
        zone_block.extend(inner_lines)
        blocks.append("\n".join(zone_block))

    # Walk the preset's own zone order — that's how the alliance
    # configured the roster, so the mail mirrors the strategy
    # layout rather than the canonical map order.
    for z in session.preset.zones:
        _emit_zone(z.zone)
        rendered.add(z.zone)

    # Non-canonical zones (legacy fixtures / test data) — append at
    # the tail so nothing silently disappears.
    for zone in by_zone.keys():
        if zone in rendered:
            continue
        _emit_zone(zone)

    return "\n\n".join(blocks)


# Back-compat alias for tests / callers that may reference the old
# phase-aware-only name. The implementation now handles flat too.
_build_combined_phase_zones_block = _build_zone_grouped_block


def _build_unified_mail(session: RosterBuilderSession) -> str:
    """Unified mail builder for the Approve & Post flow. Renders the
    alliance's template ONCE with `{zones}` substituted by the
    zone-grouped block (matches the PNG layout), `{subs}` by the
    deduped sub list, and `{time}` by the configured slot. Works
    for BOTH flat and phase-aware presets across DS and CS — the
    structure stays consistent across the four cases so officers
    see the same shape on every roster.
    """
    from config import (
        get_storm_template,
        format_storm_slot,
        get_storm_slot_for_key,
    )

    event_type = session.event_type
    template = ""
    if session.guild_id:
        template = get_storm_template(session.guild_id, event_type, None) or ""

    # Subs: walk every phase to collect paired-mode pairings plus the
    # event-level pool. Dedupe so a paired primary isn't double-listed.
    # `_mail_zone_and_sub_lists` returns:
    #   phase 1 → phase-1 pairings + global sub pool
    #   phase N (N>1) → only phase-N pairings
    sub_names_combined: list[str] = []
    seen_subs: set[str] = set()
    phases_for_subs = list(session.iter_phases()) if session.is_phase_aware else [1]
    for phase in phases_for_subs:
        _zones_p, sub_names_p = _mail_zone_and_sub_lists(
            session,
            phase=phase,
        )
        for s in sub_names_p:
            if s in seen_subs:
                continue
            sub_names_combined.append(s)
            seen_subs.add(s)
    subs_block = "\n".join(sub_names_combined) if sub_names_combined else "(none)"

    # Time string (matches the per-phase builder's `time_key="1"`).
    slot = get_storm_slot_for_key(event_type, "1") if session.guild_id else None
    if slot is not None:
        h, m = slot
        time_str = format_storm_slot(h, m, session.guild_id)
    else:
        time_str = "1"

    zones_block = _build_zone_grouped_block(session)

    if template:
        return template.format(
            alliance_name="Alliance",
            zones=zones_block,
            subs=subs_block,
            time=time_str,
        )

    # Fallback plain format (no alliance template configured).
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    return "\n".join(
        [
            f"**{label}**",
            "",
            "**Zone Assignments**",
            zones_block,
            "",
            "**Subs**",
            subs_block,
            "",
            f"**Time:** {time_str}",
        ]
    )


# Back-compat alias — older callers / tests reference this name.
_build_phase_aware_mail = _build_unified_mail


def _build_mail_body(session: RosterBuilderSession) -> str:
    """Top-level mail builder for the Approve & Post flow. Routes
    both flat and phase-aware presets through the same zone-grouped
    builder so the mail structure matches across DS + CS, flat +
    phase-aware. The PNG render organisation (zone-major, optional
    stage labels, indented assignees) is mirrored in the mail.

    The classic `/desertstorm draft` and `/canyonstorm draft` flows
    still use the original `storm.build_ds_mail` / `build_cs_mail`
    builders and are unaffected — this only changes the structured
    roster builder's mail.
    """
    return _build_unified_mail(session)


async def _send_mail_preview(
    inter: discord.Interaction,
    session: RosterBuilderSession,
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
    guild_id: int,
    event_type: str,
    event_date: str,
    team: str,
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


# ── Draft persistence (#240) ────────────────────────────────────────────────
#
# Snapshot the officer's intent (zone assignments, sub pool / pairings,
# below-floor overrides, preset choice, UI cursor) to SQLite on every
# state change so View timeouts AND Railway redeploys don't lose the
# build. Member identity comes from the current team plan / signups at
# load time and is NOT part of the saved JSON — drafts are reusable
# across event weeks. See #240's design comment for the full contract.

_DRAFT_FORMAT_VERSION = 1


def _serialize_session(session: RosterBuilderSession) -> str:
    """Serialize a `RosterBuilderSession` to a JSON string for the
    `storm_roster_drafts` store. Captures the officer's intent only;
    members, rules, and the preset object are re-resolved at load
    time so drafts stay valid across event weeks.

    `member_names_at_save` ships a tiny `{key: name}` lookup for the
    members referenced by saved assignments / pairings / subs /
    overrides — used by the reconciliation banner so dropped-member
    warnings show "Alice" instead of "1001" when the member isn't
    in the current pool to look up by key anymore (#240 follow-up).
    """
    import json

    payload = {
        "version": _DRAFT_FORMAT_VERSION,
        "selected_preset_name": session.preset.name if session.preset else "",
        "selected_phase": session.selected_phase,
        "selected_zone": session.selected_zone,
        "show_below_floor": session.show_below_floor,
        "subs": list(session.subs),
        "assignments_p1": {z: list(ks) for z, ks in session.assignments.items()},
        "assignments_p2": {z: list(ks) for z, ks in session.assignments_p2.items()},
        "assignments_p3": {z: list(ks) for z, ks in session.assignments_p3.items()},
        "paired_subs_p1": dict(session.paired_subs),
        "paired_subs_p2": dict(session.paired_subs_p2),
        "paired_subs_p3": dict(session.paired_subs_p3),
        "below_floor_overrides_p1": sorted(session.below_floor_overrides),
        "below_floor_overrides_p2": sorted(session.below_floor_overrides_p2),
        "below_floor_overrides_p3": sorted(session.below_floor_overrides_p3),
        "team_plan_applied": bool(session.team_plan_applied),
        "saved_for_event_date": session.event_date or "",
    }
    referenced_keys: set[str] = set()
    for zone_dict in (
        payload["assignments_p1"],
        payload["assignments_p2"],
        payload["assignments_p3"],
    ):
        for keys in zone_dict.values():
            referenced_keys.update(keys)
    for pair_dict in (
        payload["paired_subs_p1"],
        payload["paired_subs_p2"],
        payload["paired_subs_p3"],
    ):
        for primary, sub in pair_dict.items():
            referenced_keys.add(primary)
            referenced_keys.add(sub)
    referenced_keys.update(payload["subs"])
    referenced_keys.update(payload["below_floor_overrides_p1"])
    referenced_keys.update(payload["below_floor_overrides_p2"])
    referenced_keys.update(payload["below_floor_overrides_p3"])
    payload["member_names_at_save"] = {
        k: session.members.get(k, {}).get("name") or k for k in referenced_keys if k
    }
    return json.dumps(payload)


def _apply_saved_state(
    session: RosterBuilderSession,
    saved_payload: dict,
) -> dict:
    """Apply a deserialized draft payload to a freshly-built session,
    dropping any member keys that aren't in the current `session.members`
    (reconciled from this week's team plan / signups at load time).
    Saved entries for zones not in the current preset are silently
    dropped (preset choice may have changed).

    Returns a reconciliation report:
        {
            "dropped_members": list[str],   # member names dropped
            "kept_assignments": int,        # member-zone pairs kept
            "kept_pairings":   int,         # primary-sub pairs kept
            "stale_event_date": Optional[str],  # saved event_date when it differs
        }
    """
    member_keys = set(session.members.keys())
    dropped_keys: set[str] = set()

    def _filter_keys(keys: list[str]) -> list[str]:
        out: list[str] = []
        for k in keys:
            if k in member_keys:
                out.append(k)
            else:
                dropped_keys.add(k)
        return out

    def _apply_assignments(
        saved_by_zone: dict[str, list[str]],
        target: dict[str, list[str]],
    ) -> int:
        kept = 0
        for zone, saved_keys in (saved_by_zone or {}).items():
            if zone not in target:
                # Zone not in current preset — silently drop.
                continue
            filtered = _filter_keys(saved_keys)
            target[zone] = filtered
            kept += len(filtered)
        return kept

    def _apply_pairings(
        saved_pairs: dict[str, str],
        target: dict[str, str],
    ) -> int:
        kept = 0
        for primary, sub in (saved_pairs or {}).items():
            if primary in member_keys and sub in member_keys:
                target[primary] = sub
                kept += 1
            else:
                if primary not in member_keys:
                    dropped_keys.add(primary)
                if sub not in member_keys:
                    dropped_keys.add(sub)
        return kept

    # Per-phase apply
    kept_assignments = 0
    kept_assignments += _apply_assignments(
        saved_payload.get("assignments_p1", {}),
        session.assignments,
    )
    kept_assignments += _apply_assignments(
        saved_payload.get("assignments_p2", {}),
        session.assignments_p2,
    )
    kept_assignments += _apply_assignments(
        saved_payload.get("assignments_p3", {}),
        session.assignments_p3,
    )

    kept_pairings = 0
    kept_pairings += _apply_pairings(
        saved_payload.get("paired_subs_p1", {}),
        session.paired_subs,
    )
    kept_pairings += _apply_pairings(
        saved_payload.get("paired_subs_p2", {}),
        session.paired_subs_p2,
    )
    kept_pairings += _apply_pairings(
        saved_payload.get("paired_subs_p3", {}),
        session.paired_subs_p3,
    )

    # Flat sub pool (pool mode)
    session.subs = _filter_keys(list(saved_payload.get("subs", [])))

    # Below-floor overrides per phase (member-key sets)
    session.below_floor_overrides = set(
        _filter_keys(list(saved_payload.get("below_floor_overrides_p1", [])))
    )
    session.below_floor_overrides_p2 = set(
        _filter_keys(list(saved_payload.get("below_floor_overrides_p2", [])))
    )
    session.below_floor_overrides_p3 = set(
        _filter_keys(list(saved_payload.get("below_floor_overrides_p3", [])))
    )

    # UI cursor — restore the phase + zone the officer was on. Validate
    # against current preset; fall back to defaults if stale.
    sel_phase = int(saved_payload.get("selected_phase", 1) or 1)
    if sel_phase not in (1, 2, 3):
        sel_phase = 1
    session.selected_phase = sel_phase
    sel_zone = saved_payload.get("selected_zone", "") or ""
    current_zones = {z.zone for z in session.preset.zones}
    if sel_zone and sel_zone in current_zones:
        session.selected_zone = sel_zone
    session.show_below_floor = bool(saved_payload.get("show_below_floor", False))

    # Translate dropped keys to display names for the warning. Prefer
    # the names-at-save lookup the serializer shipped so officers see
    # "Alice" instead of the raw Discord ID. Falls back to current
    # `session.members` (when somehow a dropped key is still there)
    # and the raw key as a last resort (#240 follow-up).
    names_at_save = saved_payload.get("member_names_at_save") or {}
    dropped_names: list[str] = []
    for k in sorted(dropped_keys):
        name = names_at_save.get(k)
        if not name:
            current = session.members.get(k)
            if current:
                name = current.get("name")
        dropped_names.append(name or k)

    # Staleness: if the saved draft was last saved for a different
    # event_date, surface it so the officer reviews before posting.
    saved_event_date = saved_payload.get("saved_for_event_date", "") or ""
    stale_event_date: Optional[str] = None
    if saved_event_date and session.event_date and saved_event_date != session.event_date:
        stale_event_date = saved_event_date

    return {
        "dropped_members": dropped_names,
        "kept_assignments": kept_assignments,
        "kept_pairings": kept_pairings,
        "stale_event_date": stale_event_date,
    }


def _autosave_draft(session: RosterBuilderSession) -> None:
    """Best-effort serialize + write of the current session to
    `storm_roster_drafts`. Called from `RosterBuilderView._rebuild`
    after every state change. SQLite failures log + drop on the floor;
    the in-memory session stays authoritative."""
    if not session.is_structured:
        # Free-tier manual-apply mode — no draft persistence
        # (officer copies the mail themselves; nothing to resume).
        return
    if not session.event_date:
        # Defensive — structured mode always has event_date, but a
        # crafted fixture or hand-edited DB row could land here.
        return
    try:
        import config

        config.save_roster_draft(
            session.guild_id,
            session.event_type,
            session.team or "",
            session_json=_serialize_session(session),
            event_date=session.event_date,
        )
        # Latched flag — once an autosave fails, the warning stays
        # surfaced until the officer reopens the builder. A later
        # successful save clears the flag so officers know their
        # work is persisting again.
        session.autosave_failed = False
    except Exception as e:
        session.autosave_failed = True
        logger.warning(
            "[STORM DRAFT] autosave failed (guild=%s event=%s team=%s): %s",
            session.guild_id,
            session.event_type,
            session.team,
            e,
        )


def _team_plan_keys_or_signup_keys(
    guild_id: int,
    event_type: str,
    event_date: str,
    team: str,
) -> tuple[set[str], bool]:
    """Return the constrained candidate pool for the structured builder
    (#239).

    When a `storm_team_plans` row exists for this (guild, event, team),
    the pool is the plan's 30 members — the bot mirrors the in-game
    commitment instead of fighting it. Falls back to the vote-bucket
    filter for alliances that don't use the plan step, so today's
    behaviour is byte-identical when no plan is saved.

    Returns `(allowed_keys, plan_was_applied)`. The bool lets the
    caller surface a banner so officers know why the pool shrank.
    """
    import config

    plan = config.get_storm_team_plan(guild_id, event_type, event_date, team)
    if plan and (plan["primaries"] or plan["subs"]):
        return set(plan["primaries"]) | set(plan["subs"]), True
    return _signup_filter_keys(guild_id, event_type, event_date, team), False


def _other_team_claimed_keys(
    guild_id: int,
    event_type: str,
    team: str,
) -> set[str]:
    """Member keys already assigned on the OTHER team's saved draft (#275).

    A player can only be on one storm team at a time (hard game rule), but
    an "either time works" voter is eligible for both teams' pools. The
    builder auto-saves each team's in-progress roster to `storm_roster_drafts`
    on every edit, so when one team's pool is built we subtract everyone the
    other team has already placed — even before Approve & Post.

    "Claimed" = every member that appears as a zone primary, a flat sub, or a
    paired sub in any phase of the other team's draft. Returns an empty set
    when there's no other team's draft, the alliance isn't running both
    teams, or the draft can't be read/parsed (best-effort — never blocks the
    build). Single-team alliances (team == "") have no other team.
    """
    if team not in ("A", "B"):
        return set()
    other_team = "B" if team == "A" else "A"

    import config
    import json

    try:
        draft = config.get_roster_draft(guild_id, event_type, other_team)
    except Exception:
        return set()
    if not draft or not draft.get("session_json"):
        return set()
    try:
        payload = json.loads(draft["session_json"])
    except (ValueError, TypeError):
        return set()

    claimed: set[str] = set()
    for pkey in ("assignments_p1", "assignments_p2", "assignments_p3"):
        for keys in (payload.get(pkey) or {}).values():
            claimed.update(k for k in keys if k)
    for pkey in ("paired_subs_p1", "paired_subs_p2", "paired_subs_p3"):
        for primary, sub in (payload.get(pkey) or {}).items():
            if primary:
                claimed.add(primary)
            if sub:
                claimed.add(sub)
    claimed.update(k for k in (payload.get("subs") or []) if k)
    return claimed


# ── Long-mail picker (#237) ─────────────────────────────────────────────────
#
# When the rendered mail body exceeds Discord's 2000-char per-message
# ceiling, the officer picks how to handle it instead of the bot
# silently choosing (the pre-#237 behaviour from #234). Two options:
#
#   📨 Send as 2 posts   — splits at the next natural heading break so
#                          the second message always starts with a
#                          `**Heading**` line and sections stay
#                          together.
#   📎 Send as .txt      — full mail as a .txt file attachment
#                          alongside the image (the #234 fallback).
#
# Plus a Cancel button so the officer can back out and edit the
# roster before re-trying.

_HEADING_RE = re.compile(r"^(\*\*[^*\n]+\*\*)\s*$", re.MULTILINE)


def _split_mail_at_heading(
    mail: str,
    max_len: int = 2000,
) -> Optional[tuple[str, str]]:
    """Split a long mail body at a natural heading break (#237). The
    second message always starts with a `**Heading**` line so
    sections stay together for context.

    Returns `(part1, part2)` where each part fits within `max_len`.
    Picks the heading closest to the midpoint. Returns `None` when
    no heading split keeps both halves under the ceiling — caller
    falls back to the .txt attachment path.
    """
    candidates = [m.start() for m in _HEADING_RE.finditer(mail)]
    # A valid split position p means part_1 = mail[:p],
    # part_2 = mail[p:]. Both must fit, and p must be > 0 (otherwise
    # part_1 is empty).
    valid = [p for p in candidates if 0 < p and p <= max_len and (len(mail) - p) <= max_len]
    if not valid:
        return None
    mid = len(mail) // 2
    best = min(valid, key=lambda p: abs(p - mid))
    return mail[:best].rstrip(), mail[best:]


class _LongMailPickerView(discord.ui.View):
    """Ephemeral picker shown when the rendered mail exceeds Discord's
    2000-char message ceiling (#237). Three buttons: split / attach /
    cancel. Sets `self.choice` and stops the view; the caller awaits
    `view.wait()` then reads the choice."""

    def __init__(self, *, owner_id: int):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.choice: Optional[str] = None  # "split" | "txt" | "cancel"
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message(DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _pick(self, inter: discord.Interaction, choice: str) -> None:
        if self.is_finished():
            return
        self.choice = choice
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        self.stop()

    @discord.ui.button(label="📨 Send as 2 posts", style=discord.ButtonStyle.primary)
    async def split_btn(self, inter: discord.Interaction, _btn: discord.ui.Button):
        await self._pick(inter, "split")

    @discord.ui.button(label="📎 Send as .txt attachment", style=discord.ButtonStyle.primary)
    async def attach_btn(self, inter: discord.Interaction, _btn: discord.ui.Button):
        await self._pick(inter, "txt")

    @discord.ui.button(label="↩️ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(self, inter: discord.Interaction, _btn: discord.ui.Button):
        await self._pick(inter, "cancel")


# ── DM-the-roster (#226 follow-up) ─────────────────────────────────────────
#
# Officer-triggered, Premium-only DMs that go out AFTER Approve & Post.
# Each rostered member gets a personalised message listing exactly which
# zone(s) and stage(s) they're on. Sub pairings carry the primary's name
# so the sub knows who they're covering for; pool subs get a generic
# standby message. Members without a Discord ID or with closed DMs are
# surfaced in a single ephemeral so leadership knows who to chase up
# in-game / verbally.


def _collect_dm_assignments(
    session: "RosterBuilderSession",
) -> "list[tuple[str, list[dict]]]":
    """Walk the session and collect every member's roster role(s).

    Returns `[(member_key, [{"role": ..., "zone": ..., "phase": ...,
    "pair_with": ...}, ...]), ...]`. The returned list preserves the
    preset's zone order so a member with multiple assignments reads
    them in the same order the mail body does.

    `role` is one of:
      * `primary`    — assigned to that zone in that phase.
      * `paired_sub` — paired-mode sub covering a specific primary's
                       slot for that phase. `pair_with` is the
                       primary's display name.
      * `pool_sub`   — in `session.subs` and NOT paired anywhere.
    """
    by_member: dict[str, list[dict]] = {}
    phases = list(session.iter_phases()) if session.is_phase_aware else [1]
    zone_order = [z.zone for z in session.preset.zones]

    for phase in phases:
        assignments = session.assignments_for_phase(phase)
        pairings = session.paired_subs_for_phase(phase)
        for zone in zone_order:
            for key in assignments.get(zone, []):
                by_member.setdefault(key, []).append(
                    {
                        "role": "primary",
                        "zone": zone,
                        "phase": phase,
                        "pair_with": None,
                    }
                )
        # Paired subs — attribute the sub to the zone(s) their primary
        # is in for THIS phase, so the DM tells them where they'd be
        # filling in. A primary in two zones in the same phase
        # (unusual, but the picker doesn't block it) becomes two
        # paired_sub rows for the sub.
        for primary_key, sub_key in pairings.items():
            if not sub_key:
                continue
            primary_zones = [z for z in zone_order if primary_key in assignments.get(z, [])]
            primary_m = session.members.get(primary_key)
            primary_name = primary_m.get("name") if primary_m else primary_key
            for z in primary_zones:
                by_member.setdefault(sub_key, []).append(
                    {
                        "role": "paired_sub",
                        "zone": z,
                        "phase": phase,
                        "pair_with": primary_name,
                    }
                )

    # Pool subs — anyone in `session.subs` who isn't already covered
    # as a paired_sub somewhere (paired-mode sessions keep paired
    # keys in `session.subs` too; the mail dedup handles that, and
    # the DM dedup needs the same guard).
    for sub_key in session.subs:
        existing = by_member.get(sub_key, [])
        if any(a["role"] == "paired_sub" for a in existing):
            continue
        existing.append(
            {
                "role": "pool_sub",
                "zone": None,
                "phase": None,
                "pair_with": None,
            }
        )
        by_member[sub_key] = existing

    return list(by_member.items())


class _DmSafeDict(dict):
    """SafeDict for `_build_dm_body` template substitution. A typo
    placeholder in a saved alliance template (`{nme}` instead of
    `{name}`) renders literally instead of crashing the fan-out
    loop and leaving the rest of the roster un-DM'd. Matches the
    pattern used by every other configurable DM template in the
    bot (storm_log, train_cog, shiny_tasks)."""

    def __missing__(self, key):
        return "{" + key + "}"


def _safe_dm_format(template: str, **fields) -> str:
    """Apply `template.format_map(_DmSafeDict(...))`. Falls back to
    a substring replace on each known placeholder if the alliance's
    template carries an odd format spec (e.g. `{name:weird}`) — the
    DM still goes out, the typo just renders inside the body."""
    try:
        return template.format_map(_DmSafeDict(fields))
    except Exception:
        rendered = template
        for k, v in fields.items():
            rendered = rendered.replace("{" + k + "}", str(v))
        return rendered


def _format_starter_assignments(
    assignments: "list[dict]",
    *,
    is_phase_aware: bool,
) -> str:
    """Bullet list for Starter `{assignments}`. Each line is:
        `• Stage 1: Power Tower` (phase-aware)
        `• Power Tower`          (flat)
    Mixed-role recipients (rare: primary in one phase + standby pool
    membership) get an explicit standby footnote so they see both
    halves of their commitment.
    """
    lines: list[str] = []
    has_pool = any(a["role"] == "pool_sub" for a in assignments)
    for a in assignments:
        if a["role"] == "pool_sub":
            continue
        stage_prefix = f"Stage {a['phase']}: " if is_phase_aware else ""
        if a["role"] == "primary":
            lines.append(f"• {stage_prefix}{a['zone']}")
        else:  # paired_sub mixed into a starter DM (member plays as
            # a primary in one phase + as a sub in another)
            partner = a.get("pair_with") or "a primary"
            lines.append(f"• {stage_prefix}Sub for {partner}")
    if has_pool:
        lines.append(
            "• Plus standby pool — leadership may call you in if another primary can't make it."
        )
    return "\n".join(lines)


def _format_paired_sub_assignments(
    assignments: "list[dict]",
    *,
    is_phase_aware: bool,
) -> str:
    """Line list for Paired Sub `{assignments}`. Format mirrors the
    user's spec: `Sub for {Primary}` per pairing, with a stage prefix
    when the preset is phase-aware. A sub paired to the same primary
    across multiple stages collapses to one line so the DM doesn't
    repeat `Sub for Alice` three times."""
    by_partner: dict[str, list[int]] = {}
    for a in assignments:
        if a["role"] != "paired_sub":
            continue
        partner = a.get("pair_with") or "a primary"
        by_partner.setdefault(partner, []).append(int(a["phase"]))

    lines: list[str] = []
    for partner, phases in by_partner.items():
        if is_phase_aware and phases:
            phase_str = ", ".join(f"Stage {p}" for p in sorted(set(phases)))
            lines.append(f"{phase_str}: Sub for {partner}")
        else:
            lines.append(f"Sub for {partner}")
    return "\n".join(lines)


def _build_dm_body(
    session: "RosterBuilderSession",
    member: dict,
    assignments: "list[dict]",
    *,
    time_label: str,
    date_label: str,
) -> str:
    """Compose the personalised DM body for one rostered member.

    Resolves the alliance's saved template for the member's role
    (Starter / Paired Sub / Pool Sub) and substitutes
    `{name}`, `{event_label}`, `{team_blurb}`, `{date}`, `{time}`,
    `{assignments}` via `_safe_dm_format`. Empty saved template →
    fall back to the hardcoded default in defaults.py so a guild
    that skipped the wizard step still gets sensible copy.
    """
    import config
    from defaults import (
        DEFAULT_ROSTER_DM_STARTER,
        DEFAULT_ROSTER_DM_PAIRED_SUB,
        DEFAULT_ROSTER_DM_POOL_SUB,
    )

    label = "Desert Storm" if session.event_type == "DS" else "Canyon Storm"
    team_blurb = f" Team {session.team}" if session.team else ""
    name = (member.get("name") or "").strip() or "there"
    is_phase_aware = session.is_phase_aware

    # Role classification — the template selection mirrors what the
    # member's actual assignments look like:
    #   * any primary role  → Starter template (covers pure-primary
    #                          AND primary+sub mixed recipients;
    #                          paired_sub lines blend into the
    #                          Starter bullet list with the same
    #                          stage-prefix shape)
    #   * paired_sub only   → Paired Sub template
    #   * pool_sub only     → Pool Sub template
    has_primary = any(a["role"] == "primary" for a in assignments)
    only_pool = bool(assignments) and all(a["role"] == "pool_sub" for a in assignments)

    templates = (
        config.get_roster_dm_templates(
            session.guild_id,
            session.event_type,
        )
        if session.guild_id
        else {"starter": "", "paired_sub": "", "pool_sub": ""}
    )

    if only_pool:
        template = templates.get("pool_sub") or DEFAULT_ROSTER_DM_POOL_SUB
        assignments_block = ""
    elif has_primary:
        template = templates.get("starter") or DEFAULT_ROSTER_DM_STARTER
        assignments_block = _format_starter_assignments(
            assignments,
            is_phase_aware=is_phase_aware,
        )
    else:  # paired_sub-only
        template = templates.get("paired_sub") or DEFAULT_ROSTER_DM_PAIRED_SUB
        assignments_block = _format_paired_sub_assignments(
            assignments,
            is_phase_aware=is_phase_aware,
        )

    return _safe_dm_format(
        template,
        name=name,
        event_label=label,
        team_blurb=team_blurb,
        date=date_label,
        time=time_label,
        assignments=assignments_block,
    )


def _resolve_dm_time_label(session: "RosterBuilderSession") -> str:
    """Look up the team-specific time label (e.g. `4pm EDT (18:00
    server time)`) for the session's team. Falls back to a sensible
    placeholder when the alliance hasn't picked their team slot yet
    (would only happen if the wizard's Step 3 was skipped, which the
    save guard normally prevents)."""
    if not session.guild_id:
        return "(time not configured)"
    try:
        from config import get_storm_team_slot_labels

        a_label, b_label = get_storm_team_slot_labels(
            session.guild_id,
            session.event_type,
            session.event_date,
        )
    except Exception:
        return "(time not configured)"
    if session.team == "A":
        return a_label or "(time not configured)"
    if session.team == "B":
        return b_label or "(time not configured)"
    # CS / no-team — pick whichever side has a label.
    return a_label or b_label or "(time not configured)"


def _resolve_dm_date_label(session: "RosterBuilderSession") -> str:
    """Pretty-print the event date for the DM body. Empty string when
    the session isn't pinned to a date (free-tier template mode)."""
    if not session.event_date:
        return "the next event"
    try:
        from storm_date_helpers import format_event_date

        return format_event_date(session.event_date)
    except Exception:
        return session.event_date


async def _try_send_personal_dm(
    bot,
    discord_id: int,
    body: str,
) -> "tuple[bool, str]":
    """Send `body` to `discord_id` as a DM. Returns (success, reason).

    Reason is empty on success and a short human-readable string on
    failure, so the officer ephemeral can group recipients by why
    they didn't get the DM. The bot-side DM helper in dm.py swallows
    these distinctions; the roster-DM flow needs them surfaced.
    """
    try:
        user = await bot.fetch_user(int(discord_id))
    except discord.NotFound:
        return False, "Discord user not found (left server?)"
    except (discord.HTTPException, ValueError, TypeError) as e:
        logger.warning(
            "[STORM DM] fetch_user(%s) failed: %s",
            discord_id,
            e,
        )
        return False, "Discord lookup failed"
    try:
        await user.send(body)
        return True, ""
    except discord.Forbidden:
        return False, "DMs closed by member"
    except discord.HTTPException as e:
        logger.warning(
            "[STORM DM] send to %s failed: %s",
            discord_id,
            e,
        )
        return False, f"Discord rejected the send ({e.status})"


async def _dm_rostered_members(
    session: "RosterBuilderSession",
    bot,
) -> "tuple[int, list[tuple[str, str]]]":
    """Send a personalised DM to every primary + paired sub + pool sub
    on the approved roster.

    Returns `(sent_count, failures)`; failures is a list of
    `(display_name, reason)` tuples that the officer ephemeral renders
    so leadership knows who to chase up in-game.
    """
    time_label = _resolve_dm_time_label(session)
    date_label = _resolve_dm_date_label(session)

    by_member = _collect_dm_assignments(session)
    sent = 0
    failures: list[tuple[str, str]] = []

    for member_key, assignments in by_member:
        if not assignments:
            continue
        m = session.members.get(member_key)
        if m is None:
            failures.append((member_key, "member missing from roster"))
            continue
        display_name = (m.get("name") or "").strip() or member_key
        # `not_on_discord` is the explicit alliance / sync-time flag —
        # surface that as the reason rather than letting the empty-ID
        # branch below claim "no Discord ID" when the alliance already
        # marked them as not-on-Discord.
        if m.get("not_on_discord"):
            failures.append((display_name, "marked as not on Discord"))
            continue
        discord_id_raw = (m.get("discord_id") or "").strip()
        if not discord_id_raw or not discord_id_raw.isdigit():
            failures.append((display_name, "no Discord ID linked"))
            continue
        body = _build_dm_body(
            session,
            m,
            assignments,
            time_label=time_label,
            date_label=date_label,
        )
        ok, reason = await _try_send_personal_dm(
            bot,
            int(discord_id_raw),
            body,
        )
        if ok:
            sent += 1
        else:
            failures.append((display_name, reason))

    return sent, failures


class _DmRosteredMembersView(discord.ui.View):
    """Single-button view attached to the Approve & Post officer
    ephemeral. Click fires the DMs, disables the button, and replaces
    the message with the outcome summary so the officer can't double-
    send."""

    def __init__(self, session: "RosterBuilderSession", bot, *, owner_id: int):
        super().__init__(timeout=600)
        self.session = session
        self.bot = bot
        self.owner_id = owner_id

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    @discord.ui.button(
        label="📨 DM rostered members",
        style=discord.ButtonStyle.primary,
    )
    async def send_dms(
        self,
        interaction: discord.Interaction,
        _btn: discord.ui.Button,
    ):
        # Premium gate. Belt-and-suspenders — the view should only be
        # attached on Premium guilds, but double-check at click time
        # so a downgrade between attach and click doesn't fire DMs
        # the alliance is no longer entitled to.
        import premium

        if not await premium.is_premium(
            self.session.guild_id,
            bot=self.bot,
            interaction=interaction,
        ):
            await interaction.response.send_message(
                PREMIUM_LOCKED_INLINE.format(feature="DM-the-roster"),
                ephemeral=True,
            )
            return

        for item in self.children:
            item.disabled = True

        await interaction.response.defer(ephemeral=True, thinking=True)

        sent, failures = await _dm_rostered_members(
            self.session,
            self.bot,
        )

        # Render the outcome summary. Keep it under Discord's 2000-char
        # cap by truncating the failures list when a roster has more
        # than a handful of issues — the full list goes to the log.
        lines: list[str] = []
        if sent and not failures:
            lines.append(f"✅ DM'd {sent} member(s) on the approved roster.")
        elif sent:
            lines.append(f"✅ DM'd {sent} member(s) · ⚠️ {len(failures)} couldn't be reached:")
        elif failures:
            lines.append(f"⚠️ Couldn't DM any members ({len(failures)} failed):")
        else:
            lines.append("⚠️ No rostered members to DM — the approved roster is empty.")

        if failures:
            preview_limit = 20
            for name, reason in failures[:preview_limit]:
                lines.append(f"• **{name}** — {reason}")
            if len(failures) > preview_limit:
                lines.append(
                    f"_…and {len(failures) - preview_limit} more (see bot logs for the full list)._"
                )
                logger.info(
                    "[STORM DM] guild=%s event=%s full un-DM'd list: %s",
                    self.session.guild_id,
                    self.session.event_type,
                    failures,
                )

        body = "\n".join(lines)
        if len(body) > _MAX_MESSAGE_CONTENT:
            body = body[: _MAX_MESSAGE_CONTENT - 20] + "\n…(truncated)"
        try:
            await interaction.followup.send(body, ephemeral=True)
        except discord.HTTPException as e:
            logger.warning(
                "[STORM DM] outcome ephemeral failed (guild=%s event=%s): %s",
                self.session.guild_id,
                self.session.event_type,
                e,
            )

        # Edit the parent message to remove the now-clicked button so
        # the officer's confirmation history stays clean.
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass
        self.stop()


async def _finalize_structured_roster(
    interaction: discord.Interaction,
    view: RosterBuilderView,
    *,
    include_image: bool = False,
) -> None:
    """Approve & Post: posts the structured mail to the configured
    post channel and writes one row per slot to rosters_tab.

    `include_image=True` (#225) renders the roster as a PNG and
    attaches it to the same `channel.send` that carries the mail body,
    so the post lands as one message with both. Render failure (Pillow
    missing, encode error, >25 MB) falls back to text-only — the post
    still goes through, and the officer ephemeral confirmation tacks on
    a warning so the missing attachment isn't silent.
    """
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
                "[STORM STRUCTURED] guild.chunk() pre-pass failed for guild=%s: %s",
                s.guild_id,
                e,
            )
        fresh_members, _refresh_errors = await asyncio.to_thread(
            _read_roster_powers,
            s.guild_id,
            s.event_type,
            guild=interaction.guild,
        )
        for key, m in s.members.items():
            fresh = fresh_members.get(key)
            if fresh is not None:
                m["power"] = fresh.get("power")
    except Exception as e:
        logger.warning(
            "[STORM STRUCTURED] roster re-read for power snapshot failed (guild=%s event=%s): %s",
            s.guild_id,
            s.event_date,
            e,
        )

    # Build mail. `_build_mail_body` honors paired sub_mode (pairings
    # render as a `Primary ↔ Sub` list under the Subs section per
    # #224, matching the embed) and phase-aware presets (one block
    # per stage under `**Stage N**` headers).
    mail = _build_mail_body(s)

    cfg = config.get_storm_config(s.guild_id, s.event_type)
    post_channel_id = int(cfg.get("post_channel_id") or 0)
    post_channel = None
    if post_channel_id and interaction.guild:
        post_channel = interaction.guild.get_channel(post_channel_id)

    # Render the PNG up front (when requested) so we can attach it to
    # the channel.send call. Render failure falls back to text-only and
    # gets reported in the officer ephemeral so the missing attachment
    # isn't silent (#225). The renderer also populates
    # `roster_data.overflow` with members who couldn't fit the slot
    # grid (#228 follow-up); we stash the list for the second-ephemeral
    # warning posted after the channel.send.
    image_warning: Optional[str] = None
    image_file: Optional[discord.File] = None
    image_overflow: list = []
    if include_image and post_channel_id and post_channel is not None:
        try:
            import storm_renderer

            roster_data = storm_renderer.roster_from_session(s)
            png_bytes = await asyncio.to_thread(
                storm_renderer.render,
                roster_data,
            )
            image_overflow = list(roster_data.overflow or [])
        except RuntimeError as e:
            # Pillow missing — host doesn't have the dependency installed.
            image_warning = "Couldn't attach the image (host is missing Pillow). Posted text only."
            logger.warning(
                "[STORM STRUCTURED] image render skipped (Pillow missing) guild=%s event=%s: %s",
                s.guild_id,
                s.event_type,
                e,
            )
            png_bytes = None
        except Exception as e:
            image_warning = (
                f"Couldn't attach the image: `{type(e).__name__}: "
                f"{str(e)[:120]}`. Posted text only."
            )
            logger.exception(
                "[STORM STRUCTURED] image render failed guild=%s event=%s",
                s.guild_id,
                s.event_type,
            )
            png_bytes = None
        if png_bytes is not None:
            if len(png_bytes) > _MAX_ATTACHMENT_BYTES:
                image_warning = (
                    f"Rendered image too large to attach "
                    f"({len(png_bytes) // (1024 * 1024)} MB > 25 MB Discord "
                    f"limit). Posted text only."
                )
                logger.warning(
                    "[STORM STRUCTURED] image too large to attach (size=%d guild=%s event=%s)",
                    len(png_bytes),
                    s.guild_id,
                    s.event_type,
                )
            else:
                filename = (
                    f"{s.event_type.lower()}-roster"
                    + (f"-{s.event_date}" if s.event_date else "")
                    + (f"-team-{s.team}" if s.team else "")
                    + ".png"
                )
                image_file = discord.File(io.BytesIO(png_bytes), filename=filename)

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
    # #237: when the mail exceeds Discord's 2000-char ceiling and a
    # post channel is configured, ask the officer to pick the format
    # ("Send as 2 posts" or "Send as .txt attachment"). Pre-#237 the
    # bot silently attached the mail as .txt (#234). The picker is
    # only shown when both conditions hold; short mail and
    # no-channel branches skip it entirely.
    long_mail_choice: Optional[str] = None
    if len(mail) > _MAX_MESSAGE_CONTENT and post_channel is not None:
        picker = _LongMailPickerView(owner_id=interaction.user.id)
        try:
            picker.message = await interaction.followup.send(
                "📋 This message goes over the limit Discord allows for "
                "a single post. To be able to post this for you, we "
                "have two options:\n\n"
                "📨 **Send as 2 posts** splits at the next natural "
                "break so the second post starts with a section "
                "heading.\n\n"
                "📎 **Send as .txt attachment** posts the full mail as "
                "a file alongside the image. Copy the file's contents "
                "to send in-game.",
                view=picker,
                ephemeral=True,
            )
        except discord.HTTPException as e:
            logger.warning(
                "[STORM STRUCTURED] long-mail picker followup failed (guild=%s event=%s): %s",
                s.guild_id,
                s.event_type,
                e,
            )
            # Fall back to the #234 .txt behaviour if the picker can't
            # be sent — better than leaving the interaction stuck.
            long_mail_choice = "txt"
        if long_mail_choice is None:
            await picker.wait()
            long_mail_choice = picker.choice or "cancel"
        # Tear down the picker regardless of outcome — either the cancel
        # ack or the final post-result ack will be the most-recent
        # visible message instead of a disabled picker hanging above.
        if getattr(picker, "message", None) is not None:
            try:
                await picker.message.delete()
            except discord.HTTPException:
                pass
        if long_mail_choice == "cancel":
            # Officer cancelled — release the session lock so they can
            # reopen the builder, then exit without posting / writing
            # the rosters_tab.
            try:
                view._release_session_lock()
            except AttributeError:
                pass
            view.stop()
            try:
                await interaction.followup.send(
                    CANCEL_BACKPEDAL.format(
                        detail="Roster wasn't posted; you can keep editing the builder if you'd like.",
                    ),
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return

    if not post_channel_id:
        post_status = "no_channel"
    elif post_channel is None:
        post_status = "channel_gone"
    else:
        # Three post-send shapes depending on (mail length, officer's
        # long_mail_choice):
        #   - short mail               → one post, content=mail [+ image]
        #   - long mail + "split"      → two posts, split at heading;
        #                                image rides post 2 (the LAST
        #                                message). Tester report
        #                                2026-05-23: attaching the
        #                                image to post 1 was wrong —
        #                                the second message starts
        #                                with a heading that has no
        #                                visual link to the image
        #                                above it, and officers had
        #                                to scroll up to find the
        #                                roster image. Image on the
        #                                last post keeps the roster
        #                                visual adjacent to the last
        #                                visible heading.
        #   - long mail + "txt" / fallback → one post, mail as .txt
        #                                attachment [+ image]
        # Errors (HTTPException, perms, rate limit) on any of these
        # fall through to `post_status = "send_failed"` with the
        # exception message captured.
        post_status = "posted_ok"
        try:
            if long_mail_choice == "split" and len(mail) > _MAX_MESSAGE_CONTENT:
                parts = _split_mail_at_heading(mail)
                if parts is not None:
                    part1, part2 = parts
                    await post_channel.send(part1)
                    if image_file is not None:
                        await post_channel.send(part2, file=image_file)
                    else:
                        await post_channel.send(part2)
                else:
                    # No clean heading split — fall back to .txt path
                    # so the officer still gets the full mail.
                    long_mail_choice = "txt"

            if long_mail_choice != "split" or len(mail) <= _MAX_MESSAGE_CONTENT:
                files: list[discord.File] = []
                if len(mail) > _MAX_MESSAGE_CONTENT:
                    txt_name = (
                        f"{s.event_type.lower()}-roster"
                        + (f"-{s.event_date}" if s.event_date else "")
                        + (f"-team-{s.team}" if s.team else "")
                        + ".txt"
                    )
                    files.append(
                        discord.File(
                            io.BytesIO(mail.encode("utf-8")),
                            filename=txt_name,
                        )
                    )
                    content = (
                        f"📋 **{s.event_type} Roster** — full mail "
                        f"attached (longer than Discord's 2000-char "
                        f"message limit). Copy from the attachment to "
                        f"send in-game."
                    )
                else:
                    content = mail
                if image_file is not None:
                    files.append(image_file)
                # `file=` for one attachment, `files=` for two+, so the
                # single-image happy path preserves its kwarg shape +
                # existing tests.
                if len(files) == 1:
                    await post_channel.send(content, file=files[0])
                elif len(files) > 1:
                    await post_channel.send(content, files=files)
                else:
                    await post_channel.send(content)
            posted_to_mention = post_channel.mention
        except Exception as e:
            post_status = "send_failed"
            post_error = str(e)
            logger.warning(
                "[STORM STRUCTURED] failed to post mail to channel=%s guild=%s: %s",
                post_channel_id,
                s.guild_id,
                e,
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
        summary_lines = ["✅ Roster posted.", f"📬 Mail sent to {posted_to_mention}."]
    elif post_status == "no_channel":
        from setup_hub import STORM_SETUP_NAV

        setup_cmd = STORM_SETUP_NAV[s.event_type]
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
    if image_warning is not None:
        summary_lines.append("⚠️ " + image_warning)

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
    # Discord caps message content at 2000 chars; budget the preview to
    # what's left after the summary lines + code-fence wrappers fit.
    # Without this cap the recovery ephemeral itself blew the limit and
    # left the interaction stuck in "thinking…" (tester report
    # 2026-05-21).
    detail = "\n".join(summary_lines)
    if post_status != "posted_ok":
        # 8 chars for the ```\n…\n``` wrappers + 12 chars margin.
        fence_overhead = 20
        budget = _MAX_MESSAGE_CONTENT - len(detail) - fence_overhead
        if budget < 200:
            budget = 200  # always show at least a short snippet
        if len(mail) <= budget:
            preview = mail
        else:
            preview = mail[: budget - 20] + "\n…(truncated)"
        detail += f"\n\n```\n{preview}\n```"
    # Hard-cap defense: even with the budget above, truncate the final
    # string so a future bug in this builder can't re-introduce the
    # stuck-"thinking…" failure mode.
    if len(detail) > _MAX_MESSAGE_CONTENT:
        detail = detail[: _MAX_MESSAGE_CONTENT - 20] + "\n…(truncated)"
    try:
        await interaction.followup.send(detail, ephemeral=True)
    except discord.HTTPException as e:
        # Last-resort fallback: keep the interaction from staying stuck
        # in "thinking…" if the detail ephemeral still fails for any
        # reason (unexpected encoding issue, rate limit, etc.).
        logger.warning(
            "[STORM STRUCTURED] detail ephemeral failed (guild=%s event=%s, len=%d): %s",
            s.guild_id,
            s.event_type,
            len(detail),
            e,
        )
        try:
            await interaction.followup.send(
                "⚠️ Roster recorded but the confirmation message "
                "couldn't be sent. Check the configured post channel.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass  # nothing else to do; at least it's not stuck thinking

    # #226 follow-up — DM-the-roster: offer a one-click button to DM
    # every primary + paired sub + pool sub their personal assignment.
    # Only attached on a successful post AND on Premium guilds (the
    # bot fans out personalised messages, which is a Premium-tier
    # capability everywhere else in the codebase). Click-time gate
    # in `_DmRosteredMembersView.send_dms` re-checks Premium so a
    # downgrade between attach and click doesn't slip past.
    if post_status == "posted_ok":
        try:
            import premium

            is_premium = await premium.is_premium(
                s.guild_id,
                bot=interaction.client,
                interaction=interaction,
            )
        except Exception as e:
            logger.warning(
                "[STORM DM] premium check failed (guild=%s): %s",
                s.guild_id,
                e,
            )
            is_premium = False
        if is_premium:
            dm_view = _DmRosteredMembersView(
                s,
                interaction.client,
                owner_id=interaction.user.id,
            )
            dm_intro = (
                "📨 **DM rostered members?**\n"
                "Click below to DM each rostered member their "
                "personal assignment(s). Subs in paired mode get a "
                "note about which primary they're covering; the pool "
                "subs get a standby message.\n\n"
                "_Members without a linked Discord ID or with DMs "
                "closed get listed back here after — no DM goes out "
                "to them._"
            )
            try:
                await interaction.followup.send(
                    dm_intro,
                    view=dm_view,
                    ephemeral=True,
                )
            except discord.HTTPException as e:
                logger.warning(
                    "[STORM DM] DM-the-roster ephemeral failed (guild=%s event=%s): %s",
                    s.guild_id,
                    s.event_type,
                    e,
                )

    # #228 follow-up: if the rendered image couldn't fit every member
    # (slot grid capped at `max_rows` per zone), surface the names
    # that fell out as a SECOND ephemeral so the officer catches it.
    # The members are still in the rosters_tab and the mail body — the
    # warning only covers the image render. Officer-actionable hint:
    # shorter Discord display names take up less of the slot grid.
    if image_overflow and post_status == "posted_ok":
        # Group by (zone, stage) so the warning reads cleanly.
        from collections import OrderedDict

        grouped: "OrderedDict[tuple[str, int], list[str]]" = OrderedDict()
        for entry in image_overflow:
            key = (entry.canonical_zone, entry.phase)
            grouped.setdefault(key, []).append(entry.name)
        bullet_lines = []
        for (zone, phase), names in grouped.items():
            label = f"**{zone}**"
            if phase >= 1:
                label += f" Stage {phase}"
            bullet_lines.append(f"• {label}: {', '.join(names)}")
        warning = (
            f"⚠️ **{len(image_overflow)} member(s) didn't fit in the "
            f"posted image.** They're still in the mail body and the "
            f"rosters_tab — only the image render dropped them.\n\n"
            + "\n".join(bullet_lines)
            + "\n\nShorter Discord display names (≤ 20 chars) help — "
            "the image render truncates anything longer and a long "
            "name eats one slot in its zone's row grid."
        )
        try:
            await interaction.followup.send(warning, ephemeral=True)
        except discord.HTTPException as e:
            logger.warning(
                "[STORM STRUCTURED] overflow warning followup failed (guild=%s event=%s): %s",
                s.guild_id,
                s.event_type,
                e,
            )

    view._release_session_lock()
    view.stop()


_ROSTERS_HEADER = [
    "Event Date",
    "Team",
    "Stage",
    "Zone",
    "Member",
    "Role",
    "Power at Assignment",
    "Discord ID",
    "Override Below Minimum",
    "Paired With",
    "Posted At (UTC)",
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
            sh,
            tab,
            header_row=_ROSTERS_HEADER,
            rows=2000,
            cols=len(_ROSTERS_HEADER),
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
                for row in all_values[1:] if all_values else []:
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
                rows.append(
                    [
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
                    ]
                )
                if session.is_paired:
                    sub_key = phase_pairings.get(key)
                    if sub_key:
                        sub_m = session.members.get(sub_key)
                        if sub_m:
                            sub_power = sub_m.get("power")
                            sub_override = "yes" if sub_key in phase_overrides else ""
                            rows.append(
                                [
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
                                ]
                            )

    # Pool-mode subs (or paired-mode overflow) — written without a
    # paired-with reference. Subs are event-level (no phase scope) so
    # they don't repeat per phase — written once with the Phase cell
    # left blank to distinguish from primary/paired-sub rows.
    for key in session.subs:
        m = session.members.get(key)
        if not m:
            continue
        power = m.get("power")
        rows.append(
            [
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
            ]
        )

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
    resume_from_draft: bool = False,
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

    `resume_from_draft` (#240): when True, after the session is built
    fresh from the current team plan / signups, the saved draft for
    this team is loaded and applied. Saved zone assignments + sub
    pairings + overrides land on top of the current member pool. Set
    by the officer view's `♻️ Resume Team X` button.
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
            "⚠️ This command must be used inside a server.",
            ephemeral=True,
        )
        return

    is_structured = bool(event_date)
    if is_structured:
        ok, _structured = await ensure_premium_structured(
            interaction,
            event_type,
            feature_label="The structured roster builder",
        )
        if not ok:
            return

    preset = await asyncio.to_thread(
        ss.load_preset,
        interaction.guild_id,
        event_type,
        preset_name,
    )
    if preset is None:
        msg = f"⚠️ No preset named **{preset_name}**. Use the list command to see saved presets."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
        return

    if not preset.zones:
        msg = (
            f"⚠️ Preset **{preset_name}** has no zones yet. Edit it first "
            f"to add zones before applying."
        )
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
                f"Build roster for **Team A** or **Team B** with preset **{preset_name}**?",
                view=team_view,
                ephemeral=True,
            )
            await team_view.wait()
            if team_view.selected is None:
                await interaction.followup.send(
                    "⏰ Timed out. Run the apply command again.",
                    ephemeral=True,
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
            interaction.guild_id,
            e,
        )

    # Load powers + rules. Passes the live guild so the reader can
    # infer non-Discord status for rows with stale or blank Discord IDs
    # (#139) — explicit `not_on_discord` column still wins. gspread off
    # the event loop.
    members, roster_errors = await asyncio.to_thread(
        _read_roster_powers,
        interaction.guild_id,
        event_type,
        guild=interaction.guild,
    )

    # Structured-mode pool filter: keep only members who signed up
    # compatible with this team. When the officer saved a team plan
    # via 📋 Team A/B plan (#239), the pool tightens further to the
    # 30 members on that plan — bot mirrors the in-game commitment.
    # Unknown signups (not on roster) are surfaced as a soft warning
    # but don't gate the builder.
    plan_was_applied = False
    if is_structured:
        signup_keys, plan_was_applied = _team_plan_keys_or_signup_keys(
            interaction.guild_id,
            event_type,
            event_date,
            team,
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
        # Computed before cross-team exclusion so a member who's simply on
        # the other team isn't mislabelled as "couldn't be matched."
        matched_names_ci = {(m.get("name") or "").strip().lower() for m in members.values()}
        missing = {
            sk for sk in signup_keys if sk not in members and sk.lower() not in matched_names_ci
        }
        if missing:
            roster_errors.append(
                f"{len(missing)} signed-up member(s) couldn't be matched to a "
                f"roster row: {', '.join(sorted(missing))[:200]}"
            )

        # Cross-team exclusion (#275): a player can only be on one storm
        # team at a time. An "either time works" voter is eligible for
        # both pools, so drop anyone already assigned on the OTHER team's
        # auto-saved draft — even before Approve & Post. To move someone
        # A→B the officer pulls them off A first (autosave frees them).
        other_claimed = _other_team_claimed_keys(
            interaction.guild_id,
            event_type,
            team,
        )
        excluded_for_other_team: list[str] = []
        if other_claimed:
            excluded_for_other_team = [
                m.get("name") or k for k, m in members.items() if k in other_claimed
            ]
            members = {k: v for k, v in members.items() if k not in other_claimed}
            if excluded_for_other_team:
                other_team = "B" if team == "A" else "A"
                roster_errors.append(
                    f"{len(excluded_for_other_team)} member(s) already on Team "
                    f"{other_team}'s roster were hidden (a player can only be "
                    f"on one team): {', '.join(sorted(excluded_for_other_team))[:200]}"
                )

        if not members:
            from storm_date_helpers import format_event_date

            if excluded_for_other_team:
                # Pool emptied specifically because everyone who signed up
                # for this team is already on the other team's roster.
                other_team = "B" if team == "A" else "A"
                await interaction.followup.send(
                    f"⚠️ Everyone who signed up for team **{team or 'A'}** is "
                    f"already on Team {other_team}'s roster (a player can only "
                    f"be on one team). Move someone off Team {other_team} first, "
                    f"or wait for more sign-ups.",
                    ephemeral=True,
                )
                return
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
        if plan_was_applied:
            roster_errors.append(
                "Pool constrained to saved 30-member team plan (📋). "
                "Edit the plan via the officer view to change who's "
                "eligible."
            )

    rules = await asyncio.to_thread(
        smr.list_rules,
        interaction.guild_id,
        event_type,
    )
    per_member = [r for r in rules if r.rule_type == "per_member"]
    power_band = [r for r in rules if r.rule_type == "power_band"]

    # Structured mode: claim the per-(guild, event_type, event_date,
    # team) build slot so a second officer can't independently build
    # the same team for the same event in parallel.
    if is_structured:
        import config

        ok, holder = config.claim_storm_session(
            interaction.guild_id,
            event_type,
            event_date,
            team,
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
        interaction.guild_id,
        event_type,
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
    session.team_plan_applied = plan_was_applied
    # Seed errors from the roster read FIRST so _apply_rules_to_session
    # can append its own (e.g. unmatched per_member subjects) without
    # being clobbered.
    session.roster_errors = list(roster_errors)
    _apply_rules_to_session(session)

    # #240: resume from saved draft when requested. Loads the saved
    # session JSON, applies it on top of the freshly-built session,
    # drops any member keys that aren't in this week's pool, and
    # surfaces a reconciliation warning. If no draft exists (officer
    # clicked Resume but the row vanished between officer-view render
    # and click) silently fall through to a fresh build.
    if resume_from_draft and is_structured:
        try:
            saved = config.get_roster_draft(
                interaction.guild_id,
                event_type,
                team or "",
            )
        except Exception as e:
            saved = None
            logger.warning(
                "[STORM DRAFT] load failed (guild=%s event=%s team=%s): %s",
                interaction.guild_id,
                event_type,
                team,
                e,
            )
        if saved is not None:
            try:
                import json

                payload = json.loads(saved["session_json"])
            except (ValueError, KeyError) as e:
                payload = None
                logger.warning(
                    "[STORM DRAFT] payload parse failed (guild=%s event=%s team=%s): %s",
                    interaction.guild_id,
                    event_type,
                    team,
                    e,
                )
            if payload is not None:
                report = _apply_saved_state(session, payload)
                # Surface reconciliation results so the officer can
                # spot losses before posting. Banner sits at the top
                # of the `roster_errors` list (rendered first in the
                # embed).
                lines: list[str] = []
                if report["stale_event_date"]:
                    from storm_date_helpers import format_event_date

                    lines.append(
                        f"📅 Resumed a draft last saved for "
                        f"**{format_event_date(report['stale_event_date'])}**. "
                        f"Re-applied to this week's signups — review "
                        f"before posting."
                    )
                if report["dropped_members"]:
                    dropped = report["dropped_members"]
                    sample = ", ".join(dropped[:5])
                    more = f" (+{len(dropped) - 5} more)" if len(dropped) > 5 else ""
                    lines.append(
                        f"⚠️ {len(dropped)} saved member(s) aren't in "
                        f"this week's pool and were removed: "
                        f"{sample}{more}."
                    )
                if lines:
                    # Prepend so the resume banner is the FIRST error
                    # the officer reads in the embed.
                    session.roster_errors = lines + session.roster_errors

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
            interaction.guild_id,
            event_date,
            e,
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
                    DENY_NOT_OWNER,
                    ephemeral=True,
                )
                return
            self.selected = "A"
            for item in self.children:
                item.disabled = True
            await inter.response.edit_message(
                content="✅ Team A selected.",
                view=self,
            )
            self.stop()

        async def _pick_b(inter: discord.Interaction):
            if inter.user.id != self.owner_id:
                await inter.response.send_message(
                    DENY_NOT_OWNER,
                    ephemeral=True,
                )
                return
            self.selected = "B"
            for item in self.children:
                item.disabled = True
            await inter.response.edit_message(
                content="✅ Team B selected.",
                view=self,
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
