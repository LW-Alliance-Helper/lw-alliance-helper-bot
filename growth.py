"""
growth.py — Configurable per-guild growth snapshots.

Snapshots fire at 22:00 ET on the day picked in `the growth setup wizard`
(monthly day-of-month, or every-N-days from a fixed anchor). The
snapshot reads each member's metric values (one column per metric,
configured per guild) from the source tab and appends them as a new
period column to the growth tab.

The source tab, name column, data start row, and metric columns are
all per-guild config. Nothing in here is hardcoded to a particular
sheet layout — see `guild_growth_config` in `config.py`.
"""

import os
import json
from dataclasses import dataclass, field
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Snapshots fire at 22:00 ET (10pm) — matches bot.growth_task. Single source
# of truth so compute_next_snapshot stays in sync with the scheduler.
SNAPSHOT_FIRE_HOUR_ET = 22

# Anchor for interval-based schedules: every N days from this date. Matches
# the epoch baked into bot.growth_task.
INTERVAL_EPOCH = date(2026, 1, 1)


# ── Growth Breakdown (#34) ────────────────────────────────────────────────────
# Classifies each member's period-over-period change into one of five buckets,
# written alongside the raw snapshot data on a separate sheet tab. Forward-only:
# only new transitions get a breakdown row, history isn't backfilled.

# Canonical bucket keys, rendered top-down on the embed.
BUCKET_ORDER: list[str] = ["increased", "steady", "low", "none", "decline"]

# Default lower-bound thresholds (in %), one per bucket. Decline is anything
# below 0 and has no threshold of its own.
#   Increased ≥ 20%
#   Steady    10–20%
#   Low        5–10%
#   None       0–5%
#   Decline   <0%
DEFAULT_THRESHOLDS: dict[str, float] = {
    "none": 0.0,
    "low": 5.0,
    "steady": 10.0,
    "increased": 20.0,
}

DEFAULT_BUCKET_LABELS: dict[str, str] = {
    "increased": "Increased",
    "steady": "Steady",
    "low": "Low",
    # "No Change", not "None" — on the embed a bucket headed "None" reads as
    # "we have no data for these members" rather than "these members grew 0-5%",
    # which is the opposite of what it means (#417). The canonical key stays
    # `none`, so saved `breakdown_bucket_filter` values and any per-guild
    # `breakdown_labels` override are unaffected.
    "none": "No Change",
    "decline": "Decline",
}

# Buckets the breakdown lists by name when an alliance hasn't picked its own
# (#668). Every bucket but No Change: in a typical month that one holds most of
# the alliance and names the members who moved least, so it shows as a count.
DEFAULT_LISTED_BUCKETS: list[str] = [b for b in BUCKET_ORDER if b != "none"]

# Copy on the breakdown embed and its toggle. Constants because the on-demand
# screen, the auto-post and the tests all name them.
BREAKDOWN_TITLE = "📊 Growth Breakdown: {prev} to {curr}"
FIELD_UNCHANGED = "Same numbers as last snapshot"
UNCHANGED_LEAD = "**{n}** {members}, left out of the buckets below."
BREAKDOWN_EMPTY_METRIC = "*No members to show for this metric.*"
BTN_SHOW_ALL = "👀 Show all buckets"
BTN_SHOW_FILTER = "👀 Show your filter"
BTN_SHOW_DEFAULT = "👀 Hide No Change"
FOOTER_COUNTS_TOGGLE = "Buckets with only a count are hidden. Tap Show all buckets to list them."
FOOTER_COUNTS_SETTINGS = "Buckets with only a count are hidden by your Growth Breakdown settings."
FOOTER_FULL_LIST = "Every member is listed on the {tab} tab in your Sheet."


# Display labels this file used to write, keyed lowercase → canonical bucket.
# `read_latest_breakdown` folds these into its reverse map so cells written
# before a rename still classify. Never remove an entry: the sheet is the
# historical record and those cells are never rewritten.
_LEGACY_BUCKET_LABELS: dict[str, str] = {
    "none": "none",
}


def classify_bucket(prev: float, curr: float, thresholds: dict | None = None) -> str | None:
    """Classify a period-over-period change into a canonical bucket key.

    Returns one of ``BUCKET_ORDER`` (``increased`` / ``steady`` / ``low`` /
    ``none`` / ``decline``), or ``None`` when no meaningful percentage can
    be computed — that happens when ``prev <= 0`` (no recorded baseline,
    or the member was literally at zero last period). The sheet leaves
    both the % and Bucket cells blank in that case; users with no prior
    value start contributing to breakdowns once they have two snapshots.

    `thresholds` may override individual bucket lower bounds (e.g.
    ``{"increased": 30}``). Missing keys fall back to the default.
    """
    try:
        prev_f = float(prev)
        curr_f = float(curr)
    except (ValueError, TypeError):
        return None
    if prev_f <= 0:
        return None
    pct = ((curr_f - prev_f) / prev_f) * 100.0

    effective = dict(DEFAULT_THRESHOLDS)
    if thresholds:
        for k, v in thresholds.items():
            if k in effective:
                try:
                    effective[k] = float(v)
                except (ValueError, TypeError):
                    pass

    if pct < 0:
        return "decline"
    # Walk highest → lowest, return first bucket whose lower bound is met.
    for bucket in ("increased", "steady", "low", "none"):
        if pct >= effective[bucket]:
            return bucket
    return "none"  # defensive fallback when "none" override is > 0


def compute_pct_change(prev: float, curr: float) -> float | None:
    """Return ``(curr - prev) / prev * 100`` rounded to 2 decimals, or
    ``None`` when prev is non-positive (no meaningful percentage)."""
    try:
        prev_f = float(prev)
        curr_f = float(curr)
    except (ValueError, TypeError):
        return None
    if prev_f <= 0:
        return None
    return round(((curr_f - prev_f) / prev_f) * 100.0, 2)


def unchanged_members(pcts_by_member: dict[str, list[float | None]]) -> list[str]:
    """Members whose every metric is exactly where it was last snapshot.

    Real players don't hold every stat perfectly still for a month, so this
    almost always means their row on the source tab wasn't updated between
    snapshots. Counting them as No Change made a stale Sheet read as a
    stalled alliance (#668). A member missing a percentage for any metric
    (no baseline yet) isn't here: we can't say their numbers held.

    `pcts_by_member` is `{name: [pct per metric]}`, in the order members
    should be listed; percentages are the rounded ones the tab stores, so a
    change smaller than 0.005% on every metric also counts."""
    return [
        name
        for name, pcts in pcts_by_member.items()
        if pcts and all(p is not None and p == 0 for p in pcts)
    ]


def _parse_pct(cell) -> float | None:
    """A breakdown tab `%` cell ("12.50%", "-3.00%") as a number, or None."""
    s = str(cell).strip().rstrip("%").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _extract_period_labels(header_row: list[str], metric_labels: list[str]) -> list[str]:
    """The period labels `header_row` has a configured metric's
    ``{metric} ({period})`` column for, oldest first."""
    return growth_columns(header_row, metric_labels).periods


# ── Growth series read (for the Map Manager integration, #316) ───────────────
#
# `GET /sheet/growth` passes the alliance's *configured* metrics through to Map
# Manager, which renders them generically (no hardcoded metric). The Growth
# Tracking tab is a wide matrix: column A = member name, other columns are
# `{metric} ({period})` (period = the snapshot's "%b %Y" label). We sum each
# metric across members per period into alliance totals and shape it as
# `{ metrics: [...], snapshots: [{ date, members, values }] }`. See
# `docs/BOT_INTEGRATION_HANDOFF.md` in the Map Manager repo for the contract.


def _parse_growth_cell(cell) -> float | None:
    """Parse a growth-tab cell to a number, or None when blank/unparseable.

    None (not 0.0) for blanks so a member with no value that period isn't
    counted as active. Strips thousands separators and tolerates the scientific
    notation Sheets can render for large numbers.
    """
    s = str(cell).strip().replace(",", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _as_number(value: float):
    """Collapse a float total to an int when it's whole (the common case for
    power / kills), else round to 2dp, so MM receives clean `number`s."""
    rounded = round(value, 2)
    return int(rounded) if rounded == int(rounded) else rounded


def _period_to_iso(period: str) -> str:
    """Convert a `%b %Y` period label (e.g. "Jun 2026") to an ISO date (first of
    that month). Falls back to the raw label if it doesn't parse."""
    try:
        return datetime.strptime(period, "%b %Y").date().isoformat()
    except ValueError:
        return period


def build_growth_series(
    metric_labels: list[str], rows: list[list[str]], cols: "GrowthColumns | None" = None
) -> dict:
    """Aggregate the Growth Tracking tab's wide matrix into MM's growth shape.

    `metric_labels` are the alliance's configured metrics in display order;
    `rows` is the tab's `get_all_values()` (header + one row per member) and
    `cols` where its columns are (#668), found from the header when omitted.
    Returns
    `{ "metrics": [...], "snapshots": [{ date, members, values }] }` with
    periods in chronological order and `values` summed across members.
    A metric absent from an early period (added later) is simply omitted from
    that period's `values`.
    """
    if not rows or not rows[0]:
        return {"metrics": metric_labels, "snapshots": []}

    cols = cols or growth_columns(rows[0], metric_labels)
    data_rows = rows[1:]

    snapshots = []
    for period in cols.periods:
        values: dict[str, float] = {}
        active: set[int] = set()
        for label in metric_labels:
            idx = cols.metrics.get((label, period))
            if idx is None:
                continue  # metric wasn't tracked this period
            total = 0.0
            for ri, row in enumerate(data_rows):
                if idx >= len(row):
                    continue
                parsed = _parse_growth_cell(row[idx])
                if parsed is None:
                    continue
                total += parsed
                active.add(ri)
            values[label] = _as_number(total)
        snapshots.append({"date": _period_to_iso(period), "members": len(active), "values": values})
    return {"metrics": metric_labels, "snapshots": snapshots}


def read_growth_series(guild_id: int) -> dict:
    """Read the guild's Growth Tracking tab and shape it for MM (#316).

    Returns `{ "metrics": [...], "snapshots": [...] }`. Degrades to empty
    snapshots (never raises) when growth isn't configured or the sheet can't be
    read, so MM renders its empty state rather than erroring. `metrics` is the
    configured label list even before the first snapshot, so MM can show the
    columns up front.
    """
    from config import get_growth_config

    gcfg = get_growth_config(guild_id)
    metric_labels = [m["label"] for m in (gcfg.get("metrics") or [])]
    tab_growth = gcfg.get("tab_growth")
    if not metric_labels or not tab_growth:
        return {"metrics": metric_labels, "snapshots": []}

    try:
        rows, cols = read_growth_tab(guild_id, metric_labels, tab_growth)
    except Exception as e:
        print(f"[GROWTH] Could not read growth tab for guild {guild_id}: {e}")
        return {"metrics": metric_labels, "snapshots": []}

    return build_growth_series(metric_labels, rows, cols)


def build_member_power_map(
    metric_labels: list[str], rows: list[list[str]], cols: "GrowthColumns | None" = None
) -> dict:
    """Per-member *current* value for each configured metric, from the latest
    snapshot column. Keyed by lowercased member name.

    This is the `power` map for `GET /sheet/roster`: the alliance's growth
    metrics double as their roster power columns (Total Hero / 1st Squad / Arena
    etc.). Returns `{ name_lower: { label: number } }`; members with no values
    are omitted.
    """
    if not rows or not rows[0] or not metric_labels:
        return {}
    cols = cols or growth_columns(rows[0], metric_labels)
    periods = cols.periods
    if not periods:
        return {}
    latest = periods[-1]
    latest_cols = {
        label: cols.metrics[(label, latest)]
        for label in metric_labels
        if (label, latest) in cols.metrics
    }
    out: dict[str, dict] = {}
    for row in rows[1:]:
        name = _cell(row, cols.name)
        if not name:
            continue
        # Emit in configured metric order (NOT sheet-header order): MM derives
        # the roster's power column order from object-key insertion order.
        values = {}
        for label in metric_labels:
            idx = latest_cols.get(label)
            if idx is not None and idx < len(row):
                parsed = _parse_growth_cell(row[idx])
                if parsed is not None:
                    values[label] = _as_number(parsed)
        if values:
            out[name.lower()] = values
    return out


def read_member_power_map(guild_id: int) -> dict:
    """Read the latest growth snapshot into a per-member power map for the
    roster API (see `build_member_power_map`). Degrades to `{}` (never raises)
    when growth isn't configured or the sheet can't be read."""
    from config import get_growth_config

    gcfg = get_growth_config(guild_id)
    metric_labels = [m["label"] for m in (gcfg.get("metrics") or [])]
    tab_growth = gcfg.get("tab_growth")
    if not metric_labels or not tab_growth:
        return {}
    try:
        rows, cols = read_growth_tab(guild_id, metric_labels, tab_growth)
    except Exception as e:
        print(f"[GROWTH] Could not read growth tab for power map, guild {guild_id}: {e}")
        return {}
    return build_member_power_map(metric_labels, rows, cols)


def build_member_history(
    metric_labels: list[str],
    rows: list[list[str]],
    name_keys: set[str],
    cols: "GrowthColumns | None" = None,
) -> dict:
    """One member's growth history for the roster/dashboard panels (#316).

    `name_keys` are lowercased candidate names (display name, username); the
    first row whose name matches wins. Returns
    `{ "metrics": { label: [{ "at": iso, "value": number }] } }` in chronological
    period order, omitting blank cells. Labels match the configured growth
    metrics (same as /sheet/growth + /sheet/roster).
    """
    if not rows or not rows[0] or not metric_labels or not name_keys:
        return {"metrics": {}}
    cols = cols or growth_columns(rows[0], metric_labels)
    periods = cols.periods
    member_row = next((r for r in rows[1:] if _cell(r, cols.name).lower() in name_keys), None)
    if member_row is None or not periods:
        return {"metrics": {label: [] for label in metric_labels}}

    metrics: dict[str, list] = {}
    for label in metric_labels:
        series = []
        for period in periods:
            idx = cols.metrics.get((label, period))
            if idx is None or idx >= len(member_row):
                continue
            val = _parse_growth_cell(member_row[idx])
            if val is None:
                continue
            series.append({"at": _period_to_iso(period), "value": _as_number(val)})
        metrics[label] = series
    return {"metrics": metrics}


def read_member_history(guild_id: int, name_keys: set[str]) -> dict:
    """Read the growth tab and extract one member's per-metric history (see
    `build_member_history`). Degrades to `{"metrics": {}}` (never raises)."""
    from config import get_growth_config

    gcfg = get_growth_config(guild_id)
    metric_labels = [m["label"] for m in (gcfg.get("metrics") or [])]
    tab_growth = gcfg.get("tab_growth")
    if not metric_labels or not tab_growth or not name_keys:
        return {"metrics": {}}
    try:
        rows, cols = read_growth_tab(guild_id, metric_labels, tab_growth)
    except Exception as e:
        print(f"[GROWTH] member history read failed (guild {guild_id}): {e}")
        return {"metrics": {}}
    return build_member_history(metric_labels, rows, name_keys, cols)


def upsert_member_power(
    guild_id: int, members: list[dict], *, period_label: str | None = None
) -> dict:
    """Upsert OCR'd power into the Growth Tracking tab (handoff §6.2).

    ``members`` is ``[{ "name": str, "discord_id": str | None, "values": { label:
    number } }]`` parsed by Map Manager from a power screenshot. For each member,
    only the metric labels MM sends that match the alliance's configured metrics
    are written, into the current period's ``{label} ({%b %Y})`` column — other
    columns (and metrics MM didn't send) are never touched. A member missing from
    the tab is appended. ``period_label`` overrides the target period (tests pass
    it explicitly); production uses the current month, matching the scheduled
    snapshot so an OCR reading and a snapshot share the period column.

    Returns ``{ "written": bool, "rows": int }`` (``rows`` = members upserted).
    Degrades to not-written (never raises) when growth isn't configured.
    """
    from config import get_growth_config

    if not members:
        return {"written": False, "rows": 0}
    gcfg = get_growth_config(guild_id)
    metric_labels = [m["label"] for m in (gcfg.get("metrics") or [])]
    tab_growth = gcfg.get("tab_growth")
    if not metric_labels or not tab_growth:
        return {"written": False, "rows": 0}

    period = period_label or datetime.now(tz=ET).strftime("%b %Y")

    import gspread
    import sheet_tags

    try:
        sh = _get_spreadsheet(guild_id)
        try:
            ws = sh.worksheet(tab_growth)
        except gspread.exceptions.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab_growth, rows=500, cols=50)
        all_values, tags = _read_tab(sh, ws)
    except Exception as e:
        print(f"[GROWTH] OCR power upsert read failed for guild {guild_id}: {e}")
        return {"written": False, "rows": 0}

    if not all_values or not all_values[0]:
        all_values = [["Name"]]
    header_row = all_values[0]
    cols = growth_columns(header_row, metric_labels, tags)

    # Which configured metrics did MM actually send (across all members)?
    sent_labels: list[str] = []
    for m in members:
        if not isinstance(m, dict):
            continue
        for label in m.get("values") or {}:
            if label in metric_labels and label not in sent_labels:
                sent_labels.append(label)
    if not sent_labels:
        return {"written": False, "rows": 0}

    # Ensure the current-period column exists for each sent metric.
    header_changed = False
    for label in sent_labels:
        if (label, period) not in cols.metrics:
            cols.add_metric(header_row, label, period)
            header_changed = True
    if cols.identity < 0:
        cols.add_identity(header_row)
        header_changed = True
    id_idx = cols.identity
    if header_changed:
        sheet_tags.ensure_columns(ws, len(header_row))
        ws.update("A1", [header_row], value_input_option="USER_ENTERED")
    sheet_tags.tag_columns(sh, ws, cols.to_tag)

    identities = load_identity_map(guild_id)
    id_to_row, name_to_row = build_row_maps(all_values[1:], id_idx, start=2, name_idx=cols.name)

    updates: list[dict] = []
    new_member_rows: list[list[str]] = []
    upserted = 0
    for m in members:
        if not isinstance(m, dict):
            continue
        name = str(m.get("name") or "").strip()
        values = m.get("values") or {}
        member_labels = [label for label in values if label in metric_labels]
        if not name or not member_labels:
            continue
        # MM sends the Discord ID alongside the OCR'd name (handoff §6.2), so
        # prefer it outright and only fall back to resolving the name against
        # the roster when the payload didn't carry one.
        identity = str(m.get("discord_id") or "").strip() or identities.get(name.lower(), "")
        row_idx = resolve_row(name, identity, id_to_row, name_to_row)
        if row_idx is None:
            # Reserve a row; the append is batched after the loop (#40 quota).
            new_row = [""] * len(header_row)
            new_row[cols.name] = name
            if identity:
                new_row[id_idx] = identity
            row_idx = len(all_values) + 1
            new_member_rows.append(new_row)
            all_values.append(new_row)
            name_to_row[name.lower()] = row_idx
            if identity:
                id_to_row[identity] = row_idx
        elif identity:
            existing = all_values[row_idx - 1] if row_idx - 1 < len(all_values) else []
            if _row_identity(existing, id_idx) != identity:
                updates.append({"range": f"{_col_letter(id_idx)}{row_idx}", "values": [[identity]]})
            stored_name = _cell(existing, cols.name)
            if stored_name and stored_name.lower() != name.lower():
                updates.append({"range": f"{_col_letter(cols.name)}{row_idx}", "values": [[name]]})
        for label in member_labels:
            col_idx = cols.metrics[(label, period)]
            updates.append(
                {"range": f"{_col_letter(col_idx)}{row_idx}", "values": [[values[label]]]}
            )
        upserted += 1

    if new_member_rows:
        ws.append_rows(new_member_rows, value_input_option="USER_ENTERED")
    if updates:
        ws.batch_update(updates, value_input_option="USER_ENTERED")
    # An OCR reading can be the first thing to create a period's column, so it
    # formats them the same way the scheduled snapshot does (#417).
    _apply_number_format(ws, [cols.metrics[(label, period)] for label in sent_labels], guild_id)
    return {"written": upserted > 0, "rows": upserted}


def compute_next_snapshot(gcfg: dict, now: datetime | None = None) -> datetime | None:
    """Compute the next scheduled snapshot datetime, in America/New_York.

    Returns None if growth tracking isn't enabled. Otherwise returns the
    next datetime at which `bot.growth_task` will actually fire — i.e.
    22:00 ET on:
      * monthly  → the next occurrence of day == snapshot_day (1–28)
      * interval → the next date where (date - INTERVAL_EPOCH).days is a
                   multiple of snapshot_interval

    `now` is injectable for tests; defaults to the real current time. If
    a naive datetime is passed it's interpreted as ET to keep the
    semantics consistent with the scheduler.
    """
    if not gcfg.get("enabled"):
        return None

    if now is None:
        now = datetime.now(tz=ET)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=ET)
    else:
        now = now.astimezone(ET)

    today = now.date()
    freq = gcfg.get("snapshot_frequency", "monthly")

    if freq == "monthly":
        # Stored value is always clamped to 1..28 by the wizard, so we can
        # rely on the date being valid in every month.
        day = max(1, min(28, int(gcfg.get("snapshot_day", 1))))
        candidate = today.replace(day=day)
        if candidate < today or (candidate == today and now.hour >= SNAPSHOT_FIRE_HOUR_ET):
            year, month = today.year, today.month + 1
            if month > 12:
                year, month = year + 1, 1
            candidate = date(year, month, day)
        return datetime(
            candidate.year,
            candidate.month,
            candidate.day,
            SNAPSHOT_FIRE_HOUR_ET,
            0,
            tzinfo=ET,
        )

    if freq == "interval":
        interval = max(1, int(gcfg.get("snapshot_interval", 30)))
        delta = (today - INTERVAL_EPOCH).days
        remainder = delta % interval
        if remainder == 0 and now.hour < SNAPSHOT_FIRE_HOUR_ET:
            candidate = today
        else:
            candidate = today + timedelta(days=interval - remainder)
        return datetime(
            candidate.year,
            candidate.month,
            candidate.day,
            SNAPSHOT_FIRE_HOUR_ET,
            0,
            tzinfo=ET,
        )

    return None


def _get_spreadsheet(guild_id: int = None):
    """Return an authenticated gspread Spreadsheet object."""
    from config import get_spreadsheet

    return get_spreadsheet(guild_id)


def _col_index(letter: str) -> int | None:
    """Spreadsheet column letter → 0-based index (``A`` → 0, ``AA`` → 26).
    Returns ``None`` for a blank / non-alphabetic value. Inverse of
    :func:`_col_letter`; bare ``ord()`` arithmetic raises TypeError past ``Z``."""
    if not letter:
        return None
    letter = str(letter).strip().upper()
    if not letter or not letter.isalpha():
        return None
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


# ── Member identity across snapshots (#418) ──────────────────────────────────
#
# Growth rows used to be keyed on name alone, so a member who changed their
# in-game name stopped matching their own history and got a second row: the old
# name holding the last period, the new one holding the current period with no
# baseline. The breakdown needs a previous value to compute a percentage, so
# *both* halves silently fell out of every bucket.
#
# Same fix Train Conductor Rotation got in #305/#306: stamp a stable identity
# on each row and match on that first, leaving the name as the fallback for
# rows written before this shipped and for alliances that keep no ID column.

ID_HEADER = "Discord ID"


def _row_identity(row: list[str], id_idx: int) -> str:
    return row[id_idx].strip() if 0 <= id_idx < len(row) else ""


def _set_cell(row: list[str], idx: int, value: str) -> None:
    """Write a cell into an in-memory row, padding short rows first.

    Rows read back from a tab that predates a column are shorter than it. The
    snapshot hands its `all_values` straight to the breakdown writer, so
    keeping the in-memory copy in step with the queued writes keeps both
    passes reading the same picture."""
    if idx < 0:
        return
    if len(row) <= idx:
        row.extend([""] * (idx + 1 - len(row)))
    row[idx] = value


def build_row_maps(
    rows: list[list[str]], id_idx: int, start: int, name_idx: int = 0
) -> tuple[dict[str, int], dict[str, int]]:
    """Index existing sheet rows by identity and by name.

    `rows` is the data region (header already stripped); `start` is the sheet
    row number of `rows[0]`. Later duplicates don't clobber earlier ones, so a
    tab that already grew a duplicate row from this bug keeps resolving to the
    first (older, baseline-carrying) one."""
    id_to_row: dict[str, int] = {}
    name_to_row: dict[str, int] = {}
    for offset, row in enumerate(rows):
        name = _cell(row, name_idx)
        if not name:
            continue
        i = start + offset
        name_to_row.setdefault(name.lower(), i)
        identity = _row_identity(row, id_idx)
        if identity:
            id_to_row.setdefault(identity, i)
    return id_to_row, name_to_row


def resolve_row(
    name: str,
    identity: str,
    id_to_row: dict[str, int],
    name_to_row: dict[str, int],
) -> int | None:
    """The sheet row for this member: identity first, name second.

    Identity wins outright — that's what makes a rename survivable. Falling
    through to the name covers rows written before identities were stamped and
    alliances whose roster carries no IDs at all."""
    if identity:
        row_idx = id_to_row.get(identity)
        if row_idx is not None:
            return row_idx
    return name_to_row.get(name.strip().lower())


def _cell(row: list[str], idx: int) -> str:
    """The stripped text of `row[idx]`, or "" when the row is too short."""
    return row[idx].strip() if row and 0 <= idx < len(row) else ""


# ── Where each column is (#668) ──────────────────────────────────────────────
#
# Both growth tabs used to be read by header text: `{metric} ({period})` on the
# Growth Tracking tab, `{prev} - {curr} {metric} %` / `Bucket` on the Growth
# Breakdown tab, with the member name always in column A. That tied every
# reader to the headers staying exactly as the bot wrote them, which is what
# kept the tabs unreadable (#668). Columns are now found by their
# `sheet_tags` tag first. Header text is the fallback for columns written
# before tags existed, and those are queued in `to_tag` so the next write
# labels them; a tagged column's header can then say anything.

GROWTH_TAG = "growth"
BREAKDOWN_TAG = "breakdown"


def _period_order(period: str, first_col: int) -> tuple:
    """Sort key putting snapshot periods in calendar order.

    Periods are the snapshot's `%b %Y` label, so they sort by date whatever
    order their columns sit in. A label that doesn't parse keeps its place by
    column, after the ones that do."""
    try:
        return (0, datetime.strptime(period, "%b %Y"), first_col)
    except ValueError:
        return (1, datetime.max, first_col)


@dataclass
class GrowthColumns:
    """Where the Growth Tracking tab's columns are, as 0-based indexes."""

    name: int = 0
    identity: int = -1
    # (metric label, period label) → column
    metrics: dict[tuple[str, str], int] = field(default_factory=dict)
    # Columns found by header text or just added, keyed by column: the tags
    # the next write attaches.
    to_tag: dict[int, dict] = field(default_factory=dict)

    @property
    def periods(self) -> list[str]:
        """Every period with at least one metric column, oldest first."""
        first: dict[str, int] = {}
        for (_, period), col in self.metrics.items():
            first[period] = min(col, first.get(period, col))
        return sorted(first, key=lambda p: _period_order(p, first[p]))

    def periods_for(self, label: str) -> list[str]:
        """The periods `label` has a column for, oldest first."""
        return [p for p in self.periods if (label, p) in self.metrics]

    def add_metric(self, header_row: list[str], label: str, period: str) -> int:
        """Append a `label`/`period` column to `header_row` and return it."""
        header_row.append(f"{label} ({period})")
        col = len(header_row) - 1
        self.metrics[(label, period)] = col
        self.to_tag[col] = {"t": GROWTH_TAG, "k": "metric", "m": label, "p": period}
        return col

    def add_identity(self, header_row: list[str]) -> int:
        """Append the identity column to `header_row` and return it."""
        header_row.append(ID_HEADER)
        self.identity = len(header_row) - 1
        self.to_tag[self.identity] = {"t": GROWTH_TAG, "k": "id"}
        return self.identity


def growth_columns(
    header: list[str], metric_labels: list[str], tags: dict[int, dict] | None = None
) -> GrowthColumns:
    """Find the Growth Tracking tab's columns: by tag, then by header text.

    A tagged column is never re-read from its header, so a person can rename
    one freely. The name column is column A until something says otherwise,
    as it always was."""
    cols = GrowthColumns(name=-1)
    claimed: set[int] = set()
    for idx, tag in sorted((tags or {}).items()):
        if tag.get("t") != GROWTH_TAG:
            continue
        kind = tag.get("k")
        if kind == "name" and cols.name < 0:
            cols.name = idx
        elif kind == "id" and cols.identity < 0:
            cols.identity = idx
        elif kind == "metric" and tag.get("m") and tag.get("p"):
            cols.metrics.setdefault((tag["m"], tag["p"]), idx)
        else:
            continue
        claimed.add(idx)

    for idx, h in enumerate(header):
        if idx in claimed:
            continue
        if h == ID_HEADER and cols.identity < 0:
            cols.identity = idx
            cols.to_tag[idx] = {"t": GROWTH_TAG, "k": "id"}
            continue
        for label in metric_labels:
            prefix = f"{label} ("
            if h.startswith(prefix) and h.endswith(")"):
                period = h[len(prefix) : -1]
                if (label, period) not in cols.metrics:
                    cols.metrics[(label, period)] = idx
                    cols.to_tag[idx] = {"t": GROWTH_TAG, "k": "metric", "m": label, "p": period}
                break

    if cols.name < 0:
        cols.name = 0
        if 0 not in claimed and 0 not in cols.to_tag:
            cols.to_tag[0] = {"t": GROWTH_TAG, "k": "name"}
    return cols


def _breakdown_tag(kind: str, key: tuple[str, str, str]) -> dict:
    prev, curr, metric = key
    return {"t": BREAKDOWN_TAG, "k": kind, "m": metric, "from": prev, "to": curr}


@dataclass
class BreakdownColumns:
    """Where the Growth Breakdown tab's columns are, as 0-based indexes."""

    name: int = 0
    identity: int = -1
    # (prev period, curr period, metric label) → column
    pct: dict[tuple[str, str, str], int] = field(default_factory=dict)
    bucket: dict[tuple[str, str, str], int] = field(default_factory=dict)
    to_tag: dict[int, dict] = field(default_factory=dict)

    @property
    def transitions(self) -> list[tuple[str, str]]:
        """Every (prev, curr) pair with a bucket column, oldest first."""
        first: dict[tuple[str, str], int] = {}
        for (prev, curr, _), col in self.bucket.items():
            first[(prev, curr)] = min(col, first.get((prev, curr), col))
        return sorted(
            first,
            key=lambda t: (_period_order(t[1], first[t]), _period_order(t[0], first[t])),
        )

    def metrics_for(self, prev: str, curr: str) -> list[str]:
        """Metrics with both a % and a Bucket column for this transition."""
        return [m for (p, c, m) in self.bucket if (p, c) == (prev, curr) and (p, c, m) in self.pct]

    def add_transition_metric(self, header_row: list[str], prev: str, curr: str, metric: str):
        """Append this transition's % and Bucket columns for `metric`."""
        key = (prev, curr, metric)
        for kind, suffix, table in (("pct", "%", self.pct), ("bucket", "Bucket", self.bucket)):
            header_row.append(f"{prev} - {curr} {metric} {suffix}")
            col = len(header_row) - 1
            table[key] = col
            self.to_tag[col] = _breakdown_tag(kind, key)

    def add_identity(self, header_row: list[str]) -> int:
        header_row.append(ID_HEADER)
        self.identity = len(header_row) - 1
        self.to_tag[self.identity] = {"t": BREAKDOWN_TAG, "k": "id"}
        return self.identity


def breakdown_columns(
    header: list[str], metric_labels: list[str], tags: dict[int, dict] | None = None
) -> BreakdownColumns:
    """Find the Growth Breakdown tab's columns: by tag, then by header text."""
    cols = BreakdownColumns(name=-1)
    claimed: set[int] = set()
    for idx, tag in sorted((tags or {}).items()):
        if tag.get("t") != BREAKDOWN_TAG:
            continue
        kind = tag.get("k")
        key = (tag.get("from"), tag.get("to"), tag.get("m"))
        if kind == "name" and cols.name < 0:
            cols.name = idx
        elif kind == "id" and cols.identity < 0:
            cols.identity = idx
        elif kind in ("pct", "bucket") and all(key):
            (cols.pct if kind == "pct" else cols.bucket).setdefault(key, idx)
        else:
            continue
        claimed.add(idx)

    # Header fallback: `{prev} - {curr} {metric} Bucket` beside its `... %`.
    # The metric is matched longest-first against the configured labels so a
    # label that ends with another label splits the right way.
    by_length = sorted(metric_labels, key=len, reverse=True)
    for idx, h in enumerate(header):
        if idx in claimed:
            continue
        if h == ID_HEADER and cols.identity < 0:
            cols.identity = idx
            cols.to_tag[idx] = {"t": BREAKDOWN_TAG, "k": "id"}
            continue
        if not h.endswith(" Bucket"):
            continue
        base = h[: -len(" Bucket")]
        try:
            pct_idx = header.index(f"{base} %")
        except ValueError:
            continue
        if pct_idx in claimed or " - " not in base:
            continue
        prev, rest = base.split(" - ", 1)
        for label in by_length:
            if rest.endswith(f" {label}"):
                key = (prev, rest[: -len(label) - 1], label)
                if key not in cols.bucket:
                    cols.bucket[key] = idx
                    cols.to_tag[idx] = _breakdown_tag("bucket", key)
                if key not in cols.pct:
                    cols.pct[key] = pct_idx
                    cols.to_tag[pct_idx] = _breakdown_tag("pct", key)
                break

    if cols.name < 0:
        cols.name = 0
        if 0 not in claimed and 0 not in cols.to_tag:
            cols.to_tag[0] = {"t": BREAKDOWN_TAG, "k": "name"}
    return cols


def _read_tab(sh, ws, all_tags=None) -> tuple[list[list[str]], dict[int, dict]]:
    """A tab's values and its column tags. `all_tags` is a `read_column_tags`
    result to reuse when the caller already read the spreadsheet's tags."""
    import sheet_tags

    if all_tags is None:
        all_tags = sheet_tags.read_column_tags(sh)
    return ws.get_all_values(), sheet_tags.tags_for_sheet(all_tags, ws)


def read_growth_tab(guild_id: int, metric_labels: list[str], tab_growth: str):
    """The Growth Tracking tab's rows and where its columns are.

    Raises whatever the Sheets read raises; callers already wrap their reads
    and degrade in their own way."""
    sh = _get_spreadsheet(guild_id)
    rows, tags = _read_tab(sh, sh.worksheet(tab_growth))
    return rows, growth_columns(rows[0] if rows else [], metric_labels, tags)


def load_identity_map(guild_id: int) -> dict[str, str]:
    """Norm-name → stable identity for the alliance roster, or `{}`.

    `{}` (unconfigured roster, unreadable sheet, or an empty ID column) means
    every lookup misses and matching degrades to names, exactly as before."""
    try:
        from member_roster import roster_identity_map

        return roster_identity_map(guild_id)
    except Exception as e:
        print(f"[GROWTH] Could not load roster identities for guild {guild_id}: {e}")
        return {}


# gspread's `get_all_values()` returns the *formatted* display string, so a
# power cell with a thousands-separator number format comes back as
# "65,200,000" — which bare `float()` rejects. Every other numeric reader in
# the repo already normalises this (`_parse_growth_cell` below,
# `survey._parse_numeric`, `storm_strategy.parse_power`); this one didn't, so
# every metric big enough to carry a comma snapshotted as 0 while small ones
# (drone level) came through fine.
_MAGNITUDE = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


def _safe_float(val) -> float:
    """Parse a sheet cell to float, returning 0.0 if blank or invalid.

    Tolerates thousands separators ("65,200,000"), stray spaces/underscores,
    and the K/M/B shorthand alliances hand-type ("250M", "1.2b")."""
    if val is None:
        return 0.0
    s = str(val).strip().replace(",", "").replace("_", "").replace(" ", "").lower()
    if not s:
        return 0.0
    multiplier = 1
    if s[-1] in _MAGNITUDE:
        multiplier, s = _MAGNITUDE[s[-1]], s[:-1]
    try:
        return float(s) * multiplier
    except (ValueError, TypeError):
        return 0.0


def load_member_data(guild_id: int = None) -> list[dict]:
    """
    Load member data from the configured source tab for growth tracking.
    Returns a list of { "name": str, "row_index": int, ...metric_key: value }
    using the column configuration from guild_growth_config.
    """
    from config import get_growth_config

    gcfg = get_growth_config(guild_id)
    tab_source = gcfg.get("tab_source", "")
    name_col = gcfg.get("name_col", "A")
    metrics = gcfg.get("metrics", [])
    start_row = gcfg.get("data_start_row", 2)

    if not tab_source or not metrics:
        print(f"[GROWTH] No source tab or metrics configured for guild {guild_id}")
        return []

    try:
        sh = _get_spreadsheet(guild_id)
        ws = sh.worksheet(tab_source)
        rows = ws.get_all_values()

        # Two-letter columns ("AA") are reachable on a survey tab with enough
        # questions; bare `ord()` raised TypeError there, which the except
        # below swallowed into an empty list and a silently skipped snapshot.
        name_idx = _col_index(name_col)
        if name_idx is None:
            print(f"[GROWTH] Invalid name column '{name_col}' for guild {guild_id}")
            return []
        metric_idxs = {}
        for m in metrics:
            idx = _col_index(m.get("col"))
            if idx is None:
                print(
                    f"[GROWTH] Skipping metric '{m.get('label')}' for guild {guild_id} "
                    f"— invalid column '{m.get('col')}'"
                )
                continue
            metric_idxs[m["label"]] = idx

        members = []
        for i, row in enumerate(rows[start_row - 1 :], start=start_row):
            if len(row) <= name_idx or not row[name_idx].strip():
                continue
            entry = {"name": row[name_idx].strip(), "row_index": i}
            for label, idx in metric_idxs.items():
                entry[label] = _safe_float(row[idx]) if len(row) > idx else 0.0
            members.append(entry)

        print(f"[GROWTH] Loaded {len(members)} members from '{tab_source}' for guild {guild_id}")
        return members
    except Exception as e:
        print(f"[GROWTH] Error loading member data for guild {guild_id}: {e}")
        return []


def run_growth_snapshot():
    """
    Take a snapshot for all configured guilds that have growth tracking enabled.
    """
    import traceback, sqlite3
    from config import DB_PATH

    try:
        with sqlite3.connect(DB_PATH) as conn:
            rows = conn.execute(
                "SELECT guild_id FROM guild_configs WHERE setup_complete = 1"
            ).fetchall()
        guild_ids = [row[0] for row in rows] or [None]
    except Exception as e:
        print(f"[GROWTH] Could not read guild list: {e}")
        guild_ids = [None]

    for gid in guild_ids:
        try:
            _run_growth_snapshot_inner(gid)
        except Exception as e:
            err_str = str(e)
            if "WorksheetNotFound" in type(e).__name__ or "WorksheetNotFound" in err_str:
                print(
                    f"[GROWTH] Skipping guild {gid} — sheet tab not found. Configure via the growth setup wizard."
                )
            else:
                print(f"[GROWTH] Snapshot failed for guild {gid}: {e}")
                print(f"[GROWTH] Traceback:\n{traceback.format_exc()}")


def _run_growth_snapshot_inner(guild_id: int = None):
    from config import get_config, get_growth_config

    cfg = get_config(guild_id)
    gcfg = get_growth_config(guild_id)

    # Skip if growth tracking not enabled or configured
    if not gcfg.get("enabled"):
        return
    if not cfg or not cfg.spreadsheet_id:
        print(f"[GROWTH] Skipping guild {guild_id} — no sheet configured")
        return
    if not gcfg.get("tab_source") or not gcfg.get("tab_growth") or not gcfg.get("metrics"):
        print(
            f"[GROWTH] Skipping guild {guild_id} — growth tracking not fully configured. Run the growth setup wizard."
        )
        return

    import gspread
    import sheet_tags

    now = datetime.now(tz=ET)
    month_label = now.strftime("%b %Y")

    sh = _get_spreadsheet(guild_id)
    tab_growth = gcfg["tab_growth"]
    try:
        ws = sh.worksheet(tab_growth)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_growth, rows=500, cols=50)
        print(f"[GROWTH] Created growth tracking tab '{tab_growth}' for guild {guild_id}")

    existing_headers = ws.row_values(1) if ws.row_count > 0 else []
    metric_labels = [m["label"] for m in gcfg["metrics"]]

    # One read covers both growth tabs' column tags (#668).
    all_tags = sheet_tags.read_column_tags(sh)
    growth_tags = sheet_tags.tags_for_sheet(all_tags, ws)

    # `period_already_exists` only short-circuits the *metric column* write
    # below — the breakdown writer at the bottom still fires either way so
    # leadership clicking "Run Snapshot Now" on a guild whose current
    # period was pre-populated (seeder, manual edit, prior in-period run)
    # gets the missing breakdown computed. (#85)
    existing_cols = growth_columns(existing_headers, metric_labels, growth_tags)
    period_already_exists = all(
        (label, month_label) in existing_cols.metrics for label in metric_labels
    )

    members = load_member_data(guild_id)
    if not members:
        print(f"[GROWTH] No member data found for guild {guild_id}")
        return

    all_values = ws.get_all_values()

    # Ensure header row has Name column
    if not all_values or not all_values[0]:
        ws.update("A1", [["Name"]], value_input_option="USER_ENTERED")
        all_values = [["Name"]]

    header_row = all_values[0] if all_values else []
    cols = growth_columns(header_row, metric_labels, growth_tags)

    if period_already_exists:
        print(
            f"[GROWTH] Snapshot for {month_label} already exists "
            f"(guild {guild_id}) — checking breakdown only"
        )
    else:
        print(f"[GROWTH] Running snapshot for {month_label} (guild {guild_id})")

        # Add new metric columns for this period
        for label in metric_labels:
            if (label, month_label) not in cols.metrics:
                cols.add_metric(header_row, label, month_label)

        # Identity column, so a later rename still finds this row (#418).
        if cols.identity < 0:
            cols.add_identity(header_row)
        id_idx = cols.identity

        # Write updated header
        sheet_tags.ensure_columns(ws, len(header_row))
        ws.update("A1", [header_row], value_input_option="USER_ENTERED")

        identities = load_identity_map(guild_id)
        id_to_row, name_to_row = build_row_maps(all_values[1:], id_idx, start=2, name_idx=cols.name)

        # Write data rows
        updates = []
        new_member_rows = []
        for member in members:
            name = member["name"]
            identity = identities.get(name.strip().lower(), "")
            row_idx = resolve_row(name, identity, id_to_row, name_to_row)

            if row_idx is None:
                # Reserve a row for this new member; the actual sheet append is
                # batched into one call after the loop so a roster of 60+ members
                # doesn't exhaust the 60/min Sheets write quota (#40).
                new_row = [""] * len(header_row)
                new_row[cols.name] = name
                if identity:
                    new_row[id_idx] = identity
                row_idx = len(all_values) + 1
                new_member_rows.append(new_row)
                all_values.append(new_row)
                name_to_row[name.lower()] = row_idx
                if identity:
                    id_to_row[identity] = row_idx
                print(f"[GROWTH] New member added: {name}")
            elif identity:
                existing = all_values[row_idx - 1] if row_idx - 1 < len(all_values) else []
                # Stamp the identity on rows that predate this column, and
                # re-point the name cell when the row was found by identity but
                # still carries the member's old name.
                if _row_identity(existing, id_idx) != identity:
                    updates.append(
                        {"range": f"{_col_letter(id_idx)}{row_idx}", "values": [[identity]]}
                    )
                    _set_cell(existing, id_idx, identity)
                stored_name = _cell(existing, cols.name)
                if stored_name and stored_name.lower() != name.strip().lower():
                    updates.append(
                        {"range": f"{_col_letter(cols.name)}{row_idx}", "values": [[name]]}
                    )
                    _set_cell(existing, cols.name, name)
                    print(f"[GROWTH] Member renamed: {stored_name} → {name}")

            # Write each metric value into its column
            for label in metric_labels:
                col_idx = cols.metrics.get((label, month_label))
                if col_idx is not None:
                    # `_col_letter`, not bare ord() arithmetic — a growth tab
                    # accumulating metric columns crosses Z within a few
                    # snapshots, and chr(ord("A") + 26) is "[", an invalid A1
                    # range that fails the whole batch write.
                    col_letter = _col_letter(col_idx)
                    val = member.get(label, "")
                    updates.append(
                        {
                            "range": f"{col_letter}{row_idx}",
                            "values": [[val]],
                        }
                    )

        if new_member_rows:
            ws.append_rows(new_member_rows, value_input_option="USER_ENTERED")

        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")

        # Give this period's columns the same thousands-separator format the
        # alliance already keeps on their source columns (#417).
        _apply_number_format(
            ws, [cols.metrics[(label, month_label)] for label in metric_labels], guild_id
        )

        print(
            f"[GROWTH] Snapshot complete for {month_label} — {len(members)} members (guild {guild_id})"
        )

    # Label the columns this run added, and any older ones it found by their
    # header text, so the next read finds them whatever their header says.
    sheet_tags.tag_columns(sh, ws, cols.to_tag)

    # ── Growth Breakdown: classify period-over-period change per member ──
    # Forward-only: skip when no previous period exists. Idempotent: skip
    # when a breakdown for this (prev, curr) transition has already been
    # written. Always runs after the snapshot block, including the
    # duplicate-period path, so a missing breakdown can still be filled in
    # without forcing leadership to hand-edit the sheet. (#85) Premium
    # auto-post fires after a successful write when
    # `breakdown_post_channel_id` is set.
    try:
        _write_breakdown_for_snapshot(
            sh,
            gcfg,
            members,
            metric_labels,
            all_values,
            header_row,
            curr_period_label=month_label,
            guild_id=guild_id,
            growth_cols=cols,
            all_tags=all_tags,
        )
    except Exception as e:
        # Breakdown is a soft addition — never let it abort the snapshot
        # itself if something goes wrong.
        import traceback

        print(f"[GROWTH] Breakdown write failed for guild {guild_id}: {e}")
        print(f"[GROWTH] Breakdown traceback:\n{traceback.format_exc()}")


def _write_breakdown_for_snapshot(
    sh,
    gcfg: dict,
    members: list,
    metric_labels: list[str],
    all_values: list,
    header_row: list[str],
    curr_period_label: str,
    guild_id: int | None,
    growth_cols: GrowthColumns | None = None,
    all_tags: dict | None = None,
) -> None:
    """Compute the period-over-period breakdown for the snapshot that just
    landed and append it to the configured breakdown tab.

    `all_values` is the pre-snapshot view of the growth tab (used for
    prev-period values) and `growth_cols` where its columns are; `members`
    carries the current-period values (just read from the source tab).
    `all_tags` is the spreadsheet's column tags when the caller already read
    them. Idempotency is enforced by checking whether the (prev → curr)
    transition columns already exist on the breakdown tab.
    """
    import gspread
    import sheet_tags

    growth_cols = growth_cols or growth_columns(header_row, metric_labels)
    periods = growth_cols.periods
    if len(periods) < 2:
        # First snapshot — nothing to compare against. Per the spec
        # (forward-only), do not backfill historical transitions.
        print(f"[GROWTH] Skipping breakdown for guild {guild_id} — first snapshot")
        return
    prev_period_label = periods[-2]
    if periods[-1] != curr_period_label:
        # Defensive: the snapshot we just wrote should be the last period.
        # If it isn't (unusual ordering), fall back to comparing the last
        # two periods we found in headers.
        curr_period_label = periods[-1]
        prev_period_label = periods[-2]

    tab_breakdown = gcfg.get("tab_breakdown") or "Growth Breakdown"
    try:
        ws_bd = sh.worksheet(tab_breakdown)
    except gspread.exceptions.WorksheetNotFound:
        ws_bd = sh.add_worksheet(title=tab_breakdown, rows=500, cols=50)
        ws_bd.update("A1", [["Name"]], value_input_option="USER_ENTERED")
        print(f"[GROWTH] Created breakdown tab '{tab_breakdown}' for guild {guild_id}")

    bd_existing, bd_tags = _read_tab(sh, ws_bd, all_tags)
    if not bd_existing or not bd_existing[0]:
        bd_header = ["Name"]
        bd_existing = [bd_header]
    else:
        bd_header = list(bd_existing[0])
    bd = breakdown_columns(bd_header, metric_labels, bd_tags)

    transition_prefix = f"{prev_period_label} - {curr_period_label}"
    # Idempotency: if the % column for the first metric of this transition
    # is already present, the breakdown has already been computed and
    # written. Don't duplicate.
    if (prev_period_label, curr_period_label, metric_labels[0]) in bd.pct:
        print(
            f"[GROWTH] Breakdown for {transition_prefix} already exists — skipping "
            f"(guild {guild_id})"
        )
        sheet_tags.tag_columns(sh, ws_bd, bd.to_tag)
        return

    # Reserve new columns at the right edge: two per metric (% + Bucket).
    for m in metric_labels:
        key = (prev_period_label, curr_period_label, m)
        if key not in bd.pct and key not in bd.bucket:
            bd.add_transition_metric(bd_header, *key)

    # Read the pre-snapshot growth values for the previous period.
    prev_idxs = {
        m: growth_cols.metrics[(m, prev_period_label)]
        for m in metric_labels
        if (m, prev_period_label) in growth_cols.metrics
    }

    # Baseline lookup on the growth tab's pre-snapshot view. Identity-first
    # (#418) — a member who renamed this period carries their history on a row
    # still filed under the old name, and that row holds the only baseline
    # there is. Missing it is what silently dropped renamed members out of
    # every bucket.
    identities = load_identity_map(guild_id)
    growth_id_idx = growth_cols.identity
    growth_rows_by_id: dict[str, list[str]] = {}
    growth_rows_by_name: dict[str, list[str]] = {}
    for row in all_values[1:]:
        row_name = _cell(row, growth_cols.name)
        if not row_name:
            continue
        growth_rows_by_name.setdefault(row_name.lower(), row)
        identity = _row_identity(row, growth_id_idx)
        if identity:
            growth_rows_by_id.setdefault(identity, row)

    # Render labels (Premium override → fallback to defaults).
    label_overrides = gcfg.get("breakdown_labels") or {}
    thresholds = gcfg.get("breakdown_thresholds") or {}

    def _label_for(bucket: str) -> str:
        return str(label_overrides.get(bucket) or DEFAULT_BUCKET_LABELS[bucket])

    # Build identity/name → row index on breakdown tab; new members append.
    if bd.identity < 0:
        bd.add_identity(bd_header)
    bd_id_idx = bd.identity
    bd_id_to_row, bd_name_to_row = build_row_maps(
        bd_existing[1:], bd_id_idx, start=2, name_idx=bd.name
    )

    appended_rows: list[list[str]] = []
    updates: list[dict] = []

    # Sorted member walk so the auto-post embed (which reuses this loop's
    # output via `breakdown_summary`) has stable ordering for tests.
    breakdown_summary: dict[str, dict[str, list[str]]] = {
        m: {b: [] for b in BUCKET_ORDER} for m in metric_labels
    }
    pcts_by_member: dict[str, list[float | None]] = {}

    for member in members:
        name = member["name"]
        identity = identities.get(name.strip().lower(), "")
        prev_row = None
        if identity:
            prev_row = growth_rows_by_id.get(identity)
        if prev_row is None:
            prev_row = growth_rows_by_name.get(name.lower())
        bd_row_idx = resolve_row(name, identity, bd_id_to_row, bd_name_to_row)
        if bd_row_idx is None:
            new_row = [""] * len(bd_header)
            new_row[bd.name] = name
            if identity:
                new_row[bd_id_idx] = identity
            bd_row_idx = len(bd_existing) + len(appended_rows) + 1
            appended_rows.append(new_row)
            bd_name_to_row[name.lower()] = bd_row_idx
            if identity:
                bd_id_to_row[identity] = bd_row_idx
        elif identity:
            existing = bd_existing[bd_row_idx - 1] if bd_row_idx - 1 < len(bd_existing) else []
            if _row_identity(existing, bd_id_idx) != identity:
                updates.append(
                    {"range": f"{_col_letter(bd_id_idx)}{bd_row_idx}", "values": [[identity]]}
                )
            stored_name = _cell(existing, bd.name)
            if stored_name and stored_name.lower() != name.strip().lower():
                updates.append({"range": f"{_col_letter(bd.name)}{bd_row_idx}", "values": [[name]]})

        for m in metric_labels:
            key = (prev_period_label, curr_period_label, m)

            curr_val = member.get(m, 0.0)
            prev_val = 0.0
            if prev_row is not None and m in prev_idxs:
                idx = prev_idxs[m]
                if idx < len(prev_row):
                    # `_safe_float`, not bare float() (#417). The growth tab
                    # carries a thousands-separator number format, so gspread's
                    # FORMATTED_VALUE read hands back "57,150,000"; float()
                    # raised, the except zeroed the baseline, and prev <= 0
                    # reads as "no baseline" in classify_bucket — so every
                    # member fell out of every bucket and the embed rendered
                    # "No members in the included buckets" for each metric big
                    # enough to carry a comma. Drone level (3 digits) was the
                    # only survivor. Same normalisation the sibling readers use.
                    prev_val = _safe_float(prev_row[idx])

            pct_val = compute_pct_change(prev_val, curr_val)
            bucket = classify_bucket(prev_val, curr_val, thresholds=thresholds)
            pcts_by_member.setdefault(name, []).append(pct_val)

            pct_cell = "" if pct_val is None else f"{pct_val:.2f}%"
            bucket_cell = "" if bucket is None else _label_for(bucket)

            pct_col_idx = bd.pct[key]
            bucket_col_idx = bd.bucket[key]
            pct_col_letter = _col_letter(pct_col_idx)
            bucket_col_letter = _col_letter(bucket_col_idx)

            updates.append({"range": f"{pct_col_letter}{bd_row_idx}", "values": [[pct_cell]]})
            updates.append({"range": f"{bucket_col_letter}{bd_row_idx}", "values": [[bucket_cell]]})

            if bucket is not None:
                breakdown_summary[m][bucket].append(name)

    # Write the (possibly expanded) header row first so col letters resolve.
    sheet_tags.ensure_columns(ws_bd, len(bd_header))
    ws_bd.update("A1", [bd_header], value_input_option="USER_ENTERED")
    sheet_tags.tag_columns(sh, ws_bd, bd.to_tag)
    if appended_rows:
        ws_bd.append_rows(appended_rows, value_input_option="USER_ENTERED")
    if updates:
        ws_bd.batch_update(updates, value_input_option="USER_ENTERED")

    print(
        f"[GROWTH] Breakdown written for {transition_prefix} "
        f"({len(metric_labels)} metric(s), {len(members)} member(s), guild {guild_id})"
    )

    # Fire premium auto-post if configured. Gated by `is_premium` so a
    # subscription lapse stops the auto-post without needing config changes.
    post_channel_id = int(gcfg.get("breakdown_post_channel_id") or 0)
    if post_channel_id:
        try:
            _maybe_post_breakdown(
                guild_id,
                post_channel_id,
                prev_period_label,
                curr_period_label,
                metric_labels,
                breakdown_summary,
                gcfg,
                unchanged=unchanged_members(pcts_by_member),
            )
        except Exception as e:
            import traceback

            print(f"[GROWTH] Breakdown auto-post failed for guild {guild_id}: {e}")
            print(f"[GROWTH] Auto-post traceback:\n{traceback.format_exc()}")


def _col_letter(idx0: int) -> str:
    """Convert 0-indexed column index to spreadsheet letter (A, B, ..., Z,
    AA, AB, ...). Matches the convention used elsewhere in growth.py."""
    letters = ""
    n = idx0
    while True:
        letters = chr(ord("A") + (n % 26)) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return letters


# Thousands-separator pattern for snapshot columns. Growth metrics are whole
# numbers (power, kills, drone level), so `#,##0` matches what alliances apply
# to their own source columns by hand.
_NUMBER_FORMAT = {"numberFormat": {"type": "NUMBER", "pattern": "#,##0"}}


def _apply_number_format(ws, columns: list[int], guild_id=None) -> None:
    """Format each of `columns` (0-based, row 2 down) with `_NUMBER_FORMAT`.

    A fresh period otherwise lands as a bare ``65190000`` beside the previous
    period's ``57,150,000``, which is unreadable across a wide growth tab
    (#417). Formatting the column rather than writing pre-formatted strings
    keeps the cells genuine numbers, so every reader that parses them back
    keeps working.

    Best-effort: the snapshot values are already committed by the time this
    runs, so a Sheets failure logs and returns instead of failing the write.
    """
    ranges = []
    for idx in columns:
        letter = _col_letter(idx)
        ranges.append(f"{letter}2:{letter}")
    if not ranges:
        return
    try:
        ws.format(ranges, _NUMBER_FORMAT)
    except Exception as e:
        print(f"[GROWTH] Could not apply number format for guild {guild_id}: {e}")


def _maybe_post_breakdown(
    guild_id,
    post_channel_id,
    prev_period_label,
    curr_period_label,
    metric_labels,
    breakdown_summary,
    gcfg,
    unchanged: list[str] | None = None,
) -> None:
    """Fire the Premium breakdown auto-post. No-op when the guild isn't
    premium at the moment of posting. The bot / channel resolution and
    the actual send happen on the bot's event loop, scheduled via
    `asyncio.run_coroutine_threadsafe`. The loop reference + bot
    instance come from `bot_state` rather than `from bot import bot`
    because Railway runs `python bot.py`, which means `bot.py` lives
    in `sys.modules` as `__main__`; a downstream `import bot` would
    return a *separate* (idle) copy whose `event_loop` is never set.
    `bot_state` is only ever imported, so it has exactly one copy and
    everyone sees the same state. See #87.
    """
    if not guild_id:
        return
    import asyncio

    try:
        import bot_state
    except Exception as e:
        print(f"[GROWTH] Cannot resolve bot_state for auto-post: {e}")
        return
    bot = getattr(bot_state, "bot", None)
    loop = getattr(bot_state, "event_loop", None)
    if bot is None or loop is None or not loop.is_running():
        print(f"[GROWTH] Bot loop not ready — skipping auto-post (guild {guild_id})")
        return

    async def _post():
        import premium

        if not await premium.is_premium(guild_id, bot=bot):
            print(f"[GROWTH] Guild {guild_id} not premium — skipping auto-post")
            return
        channel = bot.get_channel(post_channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(post_channel_id)
            except Exception as e:
                print(f"[GROWTH] Auto-post channel {post_channel_id} unreachable: {e}")
                return
        embed = format_breakdown_embed(
            metric_labels=metric_labels,
            breakdown_summary=breakdown_summary,
            prev_period_label=prev_period_label,
            curr_period_label=curr_period_label,
            label_overrides=gcfg.get("breakdown_labels") or {},
            bucket_filter=gcfg.get("breakdown_bucket_filter") or [],
            unchanged=unchanged or [],
            tab_name=gcfg.get("tab_breakdown") or "Growth Breakdown",
        )
        try:
            await channel.send(embed=embed)
            print(f"[GROWTH] Auto-posted breakdown to channel {post_channel_id} (guild {guild_id})")
        except Exception as e:
            print(f"[GROWTH] Failed to send breakdown to channel {post_channel_id}: {e}")

    # `run_coroutine_threadsafe` is safe to call from the loop's own
    # thread (manual /growth button path) and from a worker thread
    # (scheduled run_in_executor path) — both end up enqueuing the
    # coroutine for the event loop to run.
    asyncio.run_coroutine_threadsafe(_post(), loop)


def read_latest_breakdown(guild_id: int) -> dict:
    """Read the breakdown tab and reconstruct the most-recent transition's
    summary for rendering. Returns a dict with keys:

      * ``has_data``: ``True`` only when at least one transition's columns
        exist on the tab (no transitions yet → first-snapshot state).
      * ``prev_period_label`` / ``curr_period_label``: the most-recent
        transition's labels.
      * ``metric_labels``: list of metric names in transition-column order.
      * ``summary``: ``{metric → {bucket_key → [member names]}}``.
      * ``unchanged``: members with the same numbers as last snapshot on
        every metric (see `unchanged_members`). They stay in ``summary``
        too, so the Map Manager API reads exactly what it always did; the
        embed is what sets them apart.

    The dict's other fields are present-but-empty when ``has_data`` is
    ``False`` so callers can branch cleanly.
    """
    from config import get_growth_config

    empty = {
        "has_data": False,
        "prev_period_label": "",
        "curr_period_label": "",
        "metric_labels": [],
        "summary": {},
        "unchanged": [],
    }

    gcfg = get_growth_config(guild_id)
    tab_breakdown = gcfg.get("tab_breakdown") or "Growth Breakdown"
    label_overrides = gcfg.get("breakdown_labels") or {}

    # Invert the labels map so the saved cell text round-trips back to the
    # canonical bucket key (the sheet stores the display label, not the
    # key, so a premium guild with `{"increased": "Crushing It"}` will
    # have cells with `Crushing It` that we need to map back).
    label_to_key = {}
    for bucket_key, default_label in DEFAULT_BUCKET_LABELS.items():
        display = label_overrides.get(bucket_key) or default_label
        label_to_key[str(display).strip().lower()] = bucket_key
        label_to_key[default_label.strip().lower()] = bucket_key  # always accept canonical too
    # Retired default labels, so rows written before a rename still round-trip.
    # `none` was displayed as "None" until #417; every breakdown tab in the
    # fleet has historical cells carrying it.
    for legacy_label, bucket_key in _LEGACY_BUCKET_LABELS.items():
        label_to_key.setdefault(legacy_label, bucket_key)

    configured_order = [m["label"] for m in (gcfg.get("metrics") or [])]
    try:
        sh = _get_spreadsheet(guild_id)
        values, tags = _read_tab(sh, sh.worksheet(tab_breakdown))
    except Exception as e:
        print(f"[GROWTH] Could not open breakdown tab for guild {guild_id}: {e}")
        return empty

    if not values or len(values) < 1 or not values[0]:
        return empty
    cols = breakdown_columns(values[0], configured_order, tags)

    # The most recent transition, with its metrics in configured order rather
    # than column order.
    transitions = cols.transitions
    if not transitions:
        return empty
    prev_period_label, curr_period_label = transitions[-1]
    found = cols.metrics_for(prev_period_label, curr_period_label)
    metric_labels = sorted(
        found,
        key=lambda m: configured_order.index(m) if m in configured_order else len(configured_order),
    )
    metric_entries = [
        (
            m,
            cols.pct[(prev_period_label, curr_period_label, m)],
            cols.bucket[(prev_period_label, curr_period_label, m)],
        )
        for m in metric_labels
    ]

    summary: dict = {m: {b: [] for b in BUCKET_ORDER} for m in metric_labels}
    pcts_by_member: dict[str, list[float | None]] = {}
    for row in values[1:]:
        name = _cell(row, cols.name)
        if not name:
            continue
        pcts_by_member[name] = [_parse_pct(_cell(row, pct_idx)) for _, pct_idx, _ in metric_entries]
        for metric, _, bucket_idx in metric_entries:
            if bucket_idx >= len(row):
                continue
            cell = row[bucket_idx].strip()
            if not cell:
                continue
            bucket_key = label_to_key.get(cell.lower())
            if bucket_key:
                summary[metric][bucket_key].append(name)

    return {
        "has_data": True,
        "prev_period_label": prev_period_label,
        "curr_period_label": curr_period_label,
        "metric_labels": metric_labels,
        "summary": summary,
        "unchanged": unchanged_members(pcts_by_member),
    }


def breakdown_for_range(guild_id: int, from_period: str, to_period: str) -> dict:
    """Per-member growth buckets between two arbitrary snapshot periods (#316).

    Same shape as `read_latest_breakdown`, but classifies `{from_period}` ->
    `{to_period}` on the fly from the Growth Tracking tab so MM's Compare picker
    can pick any two snapshot months (not just the latest consecutive pair).
    `from_period` / `to_period` are `{%b %Y}` labels exactly as the bot emits
    them, echoed back in `prev_period_label` / `curr_period_label`. Returns the
    empty (`has_data: False`) dict when either period is absent from the tab, so
    the caller can fall back to the latest transition. Never raises.
    """
    from config import get_growth_config

    empty = {
        "has_data": False,
        "prev_period_label": "",
        "curr_period_label": "",
        "metric_labels": [],
        "summary": {},
        "unchanged": [],
    }

    gcfg = get_growth_config(guild_id)
    metric_labels = [m["label"] for m in (gcfg.get("metrics") or [])]
    tab_growth = gcfg.get("tab_growth")
    thresholds = gcfg.get("breakdown_thresholds") or {}
    if not metric_labels or not tab_growth or not from_period or not to_period:
        return empty
    try:
        rows, cols = read_growth_tab(guild_id, metric_labels, tab_growth)
    except Exception as e:
        print(f"[GROWTH] breakdown range read failed (guild {guild_id}): {e}")
        return empty
    if not rows or not rows[0]:
        return empty

    from_cols = {
        label: cols.metrics[(label, from_period)]
        for label in metric_labels
        if (label, from_period) in cols.metrics
    }
    to_cols = {
        label: cols.metrics[(label, to_period)]
        for label in metric_labels
        if (label, to_period) in cols.metrics
    }
    # Only metrics present in BOTH periods can be compared.
    metrics_present = [label for label in metric_labels if label in from_cols and label in to_cols]
    if not metrics_present:
        return empty

    summary: dict = {m: {b: [] for b in BUCKET_ORDER} for m in metrics_present}
    pcts_by_member: dict[str, list[float | None]] = {}
    for row in rows[1:]:
        name = _cell(row, cols.name)
        if not name:
            continue
        pcts = pcts_by_member.setdefault(name, [])
        for label in metrics_present:
            fi, ti = from_cols[label], to_cols[label]
            prev = _parse_growth_cell(row[fi]) if fi < len(row) else None
            curr = _parse_growth_cell(row[ti]) if ti < len(row) else None
            if prev is None or curr is None:
                pcts.append(None)
                continue
            pcts.append(compute_pct_change(prev, curr))
            bucket = classify_bucket(prev, curr, thresholds=thresholds)
            if bucket:
                summary[label][bucket].append(name)

    return {
        "has_data": True,
        "prev_period_label": from_period,
        "curr_period_label": to_period,
        "metric_labels": metrics_present,
        "summary": summary,
        "unchanged": unchanged_members(pcts_by_member),
    }


_FIELD_LIMIT = 1024
_EMBED_LIMIT = 6000


def listed_buckets(bucket_filter: list[str] | None, show_all: bool = False) -> list[str]:
    """The buckets the breakdown lists by name; every other bucket is a count.

    `bucket_filter` is the alliance's saved choice (💎 Premium; pass `[]` for
    a guild that isn't Premium right now). Empty means the default."""
    if show_all:
        return list(BUCKET_ORDER)
    if bucket_filter:
        return [b for b in BUCKET_ORDER if b in bucket_filter]
    return list(DEFAULT_LISTED_BUCKETS)


def breakdown_hides_buckets(
    breakdown_summary: dict,
    metric_labels: list[str],
    bucket_filter: list[str] | None,
    unchanged: list[str] | None = None,
) -> bool:
    """Whether the filtered view shows any bucket as a count only, which is
    when a Show all toggle has something to change."""
    listed = set(listed_buckets(bucket_filter))
    gone = set(unchanged or [])
    for metric in metric_labels:
        for bucket, names in (breakdown_summary.get(metric) or {}).items():
            if bucket not in listed and any(n not in gone for n in names):
                return True
    return False


def _names_within(names: list[str], budget: int) -> tuple[str, bool]:
    """As many of `names` as fit in `budget` characters, ending "and N more"
    when some are cut. Returns the text and whether anything was cut."""
    full = ", ".join(names)
    if len(full) <= budget:
        return full, False
    for keep in range(len(names) - 1, 0, -1):
        text = ", ".join(names[:keep]) + f", and {len(names) - keep} more"
        if len(text) <= budget:
            return text, True
    return "", True


def _sections_value(sections: list[tuple[str, list[str] | None]], limit: int) -> tuple[str, bool]:
    """Join `(header, names)` sections into one field value under `limit`.

    A section with `names=None` is a count only. Listed sections share what
    the headers leave, in order, so a long bucket gives way to the ones after
    it instead of the whole field being cut mid-name."""
    parts: list[str] = []
    cut = False
    for i, (header, names) in enumerate(sections):
        if names is None:
            parts.append(header)
            continue
        later = sum(len(h) + 2 for h, _ in sections[i + 1 :])
        used = len("\n\n".join(parts + [header])) + 1
        text, was_cut = _names_within(names, limit - used - later)
        cut = cut or was_cut
        parts.append(header + ("\n" + text if text else ""))
    return "\n\n".join(parts)[:limit], cut


def format_breakdown_embed(
    *,
    metric_labels: list[str],
    breakdown_summary: dict,
    prev_period_label: str,
    curr_period_label: str,
    label_overrides: dict | None = None,
    bucket_filter: list[str] | None = None,
    unchanged: list[str] | None = None,
    show_all: bool = False,
    tab_name: str = "Growth Breakdown",
    with_toggle: bool = False,
):
    """Render the breakdown as a Discord embed. Shared by the Premium
    auto-post and the on-demand screen behind `/growth breakdown` and the
    `/growth overview` button, so every view reads the same.

    Buckets in `listed_buckets(bucket_filter, show_all)` list their members
    by name; the rest show a count (#668). Members in `unchanged` are pulled
    out of every bucket into one field of their own. Anything left out or cut
    for length is on the breakdown tab, and the footer says so.
    `with_toggle` is for the on-demand screen, whose footer can point at its
    Show all button.
    """
    import discord
    from messages import SETUP_POINTER_FOOTER
    from setup_hub import HUB_BTN_BREAKDOWN

    label_overrides = label_overrides or {}
    listed = listed_buckets(bucket_filter, show_all)
    gone = list(unchanged or [])
    gone_set = set(gone)
    esc = discord.utils.escape_markdown

    def _label(bucket: str) -> str:
        return str(label_overrides.get(bucket) or DEFAULT_BUCKET_LABELS[bucket])

    counts_only = False
    per_metric: list[tuple[str, list[tuple[str, list[str] | None]]]] = []
    for metric in metric_labels:
        per_bucket = breakdown_summary.get(metric, {})
        sections: list[tuple[str, list[str] | None]] = []
        for bucket in BUCKET_ORDER:
            names = [esc(n) for n in per_bucket.get(bucket, []) if n not in gone_set]
            if not names:
                continue
            header = f"**{_label(bucket)}** ({len(names)})"
            if bucket in listed:
                sections.append((header, names))
            else:
                sections.append((header, None))
                counts_only = True
        per_metric.append((metric, sections))

    title = BREAKDOWN_TITLE.format(prev=prev_period_label, curr=curr_period_label)
    embed = discord.Embed(title=title[:256], color=discord.Color.blurple())

    # Leave room for the footer and titles, then share the rest out so five
    # metrics and the unchanged list together stay under Discord's total.
    field_count = len(per_metric) + (1 if gone else 0)
    reserved = len(title) + 400 + sum(len(m) for m in metric_labels) + len(FIELD_UNCHANGED)
    limit = min(_FIELD_LIMIT, (_EMBED_LIMIT - reserved) // max(1, field_count))

    cut = False
    if gone:
        lead = UNCHANGED_LEAD.format(n=len(gone), members="member" if len(gone) == 1 else "members")
        value, was_cut = _sections_value([(lead, [esc(n) for n in gone])], limit)
        cut = cut or was_cut
        embed.add_field(name=FIELD_UNCHANGED, value=value, inline=False)
    for metric, sections in per_metric:
        if sections:
            value, was_cut = _sections_value(sections, limit)
            cut = cut or was_cut
        else:
            value = BREAKDOWN_EMPTY_METRIC
        embed.add_field(name=metric[:256], value=value, inline=False)

    footer: list[str] = []
    if counts_only:
        footer.append(FOOTER_COUNTS_TOGGLE if with_toggle else FOOTER_COUNTS_SETTINGS)
    if counts_only or cut:
        footer.append(FOOTER_FULL_LIST.format(tab=tab_name))
    footer.append(SETUP_POINTER_FOOTER.format(wizard=HUB_BTN_BREAKDOWN))
    embed.set_footer(text="\n".join(footer))
    return embed
