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
    "none":       0.0,
    "low":        5.0,
    "steady":    10.0,
    "increased": 20.0,
}

DEFAULT_BUCKET_LABELS: dict[str, str] = {
    "increased": "Increased",
    "steady":    "Steady",
    "low":       "Low",
    "none":      "None",
    "decline":   "Decline",
}


def classify_bucket(prev: float, curr: float,
                    thresholds: dict | None = None) -> str | None:
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


def _extract_period_labels(header_row: list[str], metric_labels: list[str]) -> list[str]:
    """Return the unique period labels in `header_row` in order of first
    appearance, by stripping the ``{metric} ({period})`` suffix from each
    column header that matches a configured metric. Lets the snapshot
    code identify the previous period without parsing dates."""
    seen: list[str] = []
    for h in header_row:
        for m in metric_labels:
            prefix = f"{m} ("
            if h.startswith(prefix) and h.endswith(")"):
                period = h[len(prefix):-1]
                if period not in seen:
                    seen.append(period)
                break
    return seen


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
    freq  = gcfg.get("snapshot_frequency", "monthly")

    if freq == "monthly":
        # Stored value is always clamped to 1..28 by the wizard, so we can
        # rely on the date being valid in every month.
        day = max(1, min(28, int(gcfg.get("snapshot_day", 1))))
        candidate = today.replace(day=day)
        if candidate < today or (
            candidate == today and now.hour >= SNAPSHOT_FIRE_HOUR_ET
        ):
            year, month = today.year, today.month + 1
            if month > 12:
                year, month = year + 1, 1
            candidate = date(year, month, day)
        return datetime(
            candidate.year, candidate.month, candidate.day,
            SNAPSHOT_FIRE_HOUR_ET, 0, tzinfo=ET,
        )

    if freq == "interval":
        interval = max(1, int(gcfg.get("snapshot_interval", 30)))
        delta    = (today - INTERVAL_EPOCH).days
        remainder = delta % interval
        if remainder == 0 and now.hour < SNAPSHOT_FIRE_HOUR_ET:
            candidate = today
        else:
            candidate = today + timedelta(days=interval - remainder)
        return datetime(
            candidate.year, candidate.month, candidate.day,
            SNAPSHOT_FIRE_HOUR_ET, 0, tzinfo=ET,
        )

    return None




def _get_spreadsheet(guild_id: int = None):
    """Return an authenticated gspread Spreadsheet object."""
    from config import get_spreadsheet
    return get_spreadsheet(guild_id)


def _safe_float(val: str) -> float:
    """Parse a string to float, returning 0.0 if blank or invalid."""
    try:
        return float(str(val).strip()) if val and str(val).strip() else 0.0
    except ValueError:
        return 0.0


def load_member_data(guild_id: int = None) -> list[dict]:
    """
    Load member data from the configured source tab for growth tracking.
    Returns a list of { "name": str, "row_index": int, ...metric_key: value }
    using the column configuration from guild_growth_config.
    """
    from config import get_growth_config
    gcfg       = get_growth_config(guild_id)
    tab_source = gcfg.get("tab_source", "")
    name_col   = gcfg.get("name_col", "A")
    metrics    = gcfg.get("metrics", [])
    start_row  = gcfg.get("data_start_row", 2)

    if not tab_source or not metrics:
        print(f"[GROWTH] No source tab or metrics configured for guild {guild_id}")
        return []

    try:
        sh   = _get_spreadsheet(guild_id)
        ws   = sh.worksheet(tab_source)
        rows = ws.get_all_values()

        name_idx    = ord(name_col.upper()) - ord('A')
        metric_idxs = {m["label"]: ord(m["col"].upper()) - ord('A') for m in metrics}

        members = []
        for i, row in enumerate(rows[start_row - 1:], start=start_row):
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
                print(f"[GROWTH] Skipping guild {gid} — sheet tab not found. Configure via the growth setup wizard.")
            else:
                print(f"[GROWTH] Snapshot failed for guild {gid}: {e}")
                print(f"[GROWTH] Traceback:\n{traceback.format_exc()}")


def _run_growth_snapshot_inner(guild_id: int = None):
    from config import get_config, get_growth_config
    cfg   = get_config(guild_id)
    gcfg  = get_growth_config(guild_id)

    # Skip if growth tracking not enabled or configured
    if not gcfg.get("enabled"):
        return
    if not cfg or not cfg.spreadsheet_id:
        print(f"[GROWTH] Skipping guild {guild_id} — no sheet configured")
        return
    if not gcfg.get("tab_source") or not gcfg.get("tab_growth") or not gcfg.get("metrics"):
        print(f"[GROWTH] Skipping guild {guild_id} — growth tracking not fully configured. Run the growth setup wizard.")
        return

    import gspread
    now         = datetime.now(tz=ET)
    month_label = now.strftime("%b %Y")

    sh  = _get_spreadsheet(guild_id)
    tab_growth = gcfg["tab_growth"]
    try:
        ws = sh.worksheet(tab_growth)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_growth, rows=500, cols=50)
        print(f"[GROWTH] Created growth tracking tab '{tab_growth}' for guild {guild_id}")

    existing_headers = ws.row_values(1) if ws.row_count > 0 else []
    metric_labels    = [m["label"] for m in gcfg["metrics"]]

    # `period_already_exists` only short-circuits the *metric column* write
    # below — the breakdown writer at the bottom still fires either way so
    # leadership clicking "Run Snapshot Now" on a guild whose current
    # period was pre-populated (seeder, manual edit, prior in-period run)
    # gets the missing breakdown computed. (#85)
    period_cols = [h for h in existing_headers if h.endswith(f"({month_label})")]
    period_already_exists = len(period_cols) >= len(metric_labels)

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

    if period_already_exists:
        print(
            f"[GROWTH] Snapshot for {month_label} already exists "
            f"(guild {guild_id}) — checking breakdown only"
        )
    else:
        print(f"[GROWTH] Running snapshot for {month_label} (guild {guild_id})")

        # Add new metric columns for this period
        new_headers = [f"{label} ({month_label})" for label in metric_labels]
        for new_header in new_headers:
            if new_header not in header_row:
                header_row.append(new_header)

        # Write updated header
        ws.update("A1", [header_row], value_input_option="USER_ENTERED")

        # Build name → row index map
        name_to_row = {}
        for i, row in enumerate(all_values[1:], start=2):
            if row and row[0].strip():
                name_to_row[row[0].strip().lower()] = i

        # Write data rows
        updates = []
        new_member_rows = []
        for member in members:
            name     = member["name"]
            row_idx  = name_to_row.get(name.lower())

            if row_idx is None:
                # Reserve a row for this new member; the actual sheet append is
                # batched into one call after the loop so a roster of 60+ members
                # doesn't exhaust the 60/min Sheets write quota (#40).
                new_row = [name] + [""] * (len(header_row) - 1)
                row_idx = len(all_values) + 1
                new_member_rows.append(new_row)
                all_values.append(new_row)
                name_to_row[name.lower()] = row_idx
                print(f"[GROWTH] New member added: {name}")

            # Write each metric value into its column
            for label in metric_labels:
                col_name = f"{label} ({month_label})"
                if col_name in header_row:
                    col_idx   = header_row.index(col_name)
                    col_letter = chr(ord('A') + col_idx)
                    val        = member.get(label, "")
                    updates.append({
                        "range": f"{col_letter}{row_idx}",
                        "values": [[val]],
                    })

        if new_member_rows:
            ws.append_rows(new_member_rows, value_input_option="USER_ENTERED")

        if updates:
            ws.batch_update(updates, value_input_option="USER_ENTERED")

        print(f"[GROWTH] Snapshot complete for {month_label} — {len(members)} members (guild {guild_id})")

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
            sh, gcfg, members, metric_labels, all_values, header_row,
            curr_period_label=month_label, guild_id=guild_id,
        )
    except Exception as e:
        # Breakdown is a soft addition — never let it abort the snapshot
        # itself if something goes wrong.
        import traceback
        print(f"[GROWTH] Breakdown write failed for guild {guild_id}: {e}")
        print(f"[GROWTH] Breakdown traceback:\n{traceback.format_exc()}")


def _write_breakdown_for_snapshot(sh, gcfg: dict, members: list, metric_labels: list[str],
                                  all_values: list, header_row: list[str],
                                  curr_period_label: str, guild_id: int | None) -> None:
    """Compute the period-over-period breakdown for the snapshot that just
    landed and append it to the configured breakdown tab.

    `all_values` is the pre-snapshot view of the growth tab (used for
    prev-period values); `members` carries the current-period values
    (just read from the source tab). Idempotency is enforced by checking
    whether the (prev → curr) transition columns already exist on the
    breakdown tab.
    """
    import gspread

    periods = _extract_period_labels(header_row, metric_labels)
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

    bd_existing = ws_bd.get_all_values()
    if not bd_existing or not bd_existing[0]:
        bd_header = ["Name"]
        bd_existing = [bd_header]
    else:
        bd_header = list(bd_existing[0])

    transition_prefix = f"{prev_period_label} - {curr_period_label}"
    # Idempotency: if the % column for the first metric of this transition
    # is already present, the breakdown has already been computed and
    # written. Don't duplicate.
    first_metric_pct_col = f"{transition_prefix} {metric_labels[0]} %"
    if first_metric_pct_col in bd_header:
        print(
            f"[GROWTH] Breakdown for {transition_prefix} already exists — skipping "
            f"(guild {guild_id})"
        )
        return

    # Reserve new columns at the right edge: two per metric (% + Bucket).
    new_cols: list[str] = []
    for m in metric_labels:
        new_cols.append(f"{transition_prefix} {m} %")
        new_cols.append(f"{transition_prefix} {m} Bucket")
    for col in new_cols:
        if col not in bd_header:
            bd_header.append(col)

    # Read the pre-snapshot growth values for the previous period.
    growth_header = header_row
    prev_idxs = {}
    for m in metric_labels:
        col_name = f"{m} ({prev_period_label})"
        if col_name in growth_header:
            prev_idxs[m] = growth_header.index(col_name)

    # Build name → prev-row index map on the growth tab pre-snapshot view.
    growth_name_to_row = {}
    for i, row in enumerate(all_values[1:], start=1):
        if row and row[0].strip():
            growth_name_to_row[row[0].strip().lower()] = row

    # Render labels (Premium override → fallback to defaults).
    label_overrides = gcfg.get("breakdown_labels") or {}
    thresholds      = gcfg.get("breakdown_thresholds") or {}

    def _label_for(bucket: str) -> str:
        return str(label_overrides.get(bucket) or DEFAULT_BUCKET_LABELS[bucket])

    # Build name → row index on breakdown tab; new members append.
    bd_name_to_row = {}
    for i, row in enumerate(bd_existing[1:], start=2):
        if row and row[0].strip():
            bd_name_to_row[row[0].strip().lower()] = i

    appended_rows: list[list[str]] = []
    updates: list[dict] = []
    # Use the new bd_header for column indexing.
    col_index = {h: i for i, h in enumerate(bd_header)}

    # Sorted member walk so the auto-post embed (which reuses this loop's
    # output via `breakdown_summary`) has stable ordering for tests.
    breakdown_summary: dict[str, dict[str, list[str]]] = {
        m: {b: [] for b in BUCKET_ORDER} for m in metric_labels
    }

    for member in members:
        name = member["name"]
        prev_row = growth_name_to_row.get(name.lower())
        bd_row_idx = bd_name_to_row.get(name.lower())
        if bd_row_idx is None:
            new_row = [name] + [""] * (len(bd_header) - 1)
            bd_row_idx = len(bd_existing) + len(appended_rows) + 1
            appended_rows.append(new_row)
            bd_name_to_row[name.lower()] = bd_row_idx

        for m in metric_labels:
            pct_col_name    = f"{transition_prefix} {m} %"
            bucket_col_name = f"{transition_prefix} {m} Bucket"

            curr_val = member.get(m, 0.0)
            prev_val = 0.0
            if prev_row is not None and m in prev_idxs:
                idx = prev_idxs[m]
                if idx < len(prev_row):
                    try:
                        prev_val = float(prev_row[idx]) if prev_row[idx] else 0.0
                    except (ValueError, TypeError):
                        prev_val = 0.0

            pct_val = compute_pct_change(prev_val, curr_val)
            bucket  = classify_bucket(prev_val, curr_val, thresholds=thresholds)

            pct_cell    = "" if pct_val is None else f"{pct_val:.2f}%"
            bucket_cell = "" if bucket is None else _label_for(bucket)

            pct_col_idx    = col_index[pct_col_name]
            bucket_col_idx = col_index[bucket_col_name]
            pct_col_letter    = _col_letter(pct_col_idx)
            bucket_col_letter = _col_letter(bucket_col_idx)

            updates.append({"range": f"{pct_col_letter}{bd_row_idx}",
                            "values": [[pct_cell]]})
            updates.append({"range": f"{bucket_col_letter}{bd_row_idx}",
                            "values": [[bucket_cell]]})

            if bucket is not None:
                breakdown_summary[m][bucket].append(name)

    # Write the (possibly expanded) header row first so col letters resolve.
    ws_bd.update("A1", [bd_header], value_input_option="USER_ENTERED")
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
                guild_id, post_channel_id, prev_period_label, curr_period_label,
                metric_labels, breakdown_summary, gcfg,
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
        letters = chr(ord('A') + (n % 26)) + letters
        n = n // 26 - 1
        if n < 0:
            break
    return letters


def _maybe_post_breakdown(guild_id, post_channel_id, prev_period_label,
                          curr_period_label, metric_labels, breakdown_summary,
                          gcfg) -> None:
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
    bot  = getattr(bot_state, "bot", None)
    loop = getattr(bot_state, "event_loop", None)
    if bot is None or loop is None or not loop.is_running():
        print(f"[GROWTH] Bot loop not ready — skipping auto-post "
              f"(guild {guild_id})")
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
        )
        try:
            await channel.send(embed=embed)
            print(f"[GROWTH] Auto-posted breakdown to channel {post_channel_id} "
                  f"(guild {guild_id})")
        except Exception as e:
            print(f"[GROWTH] Failed to send breakdown to channel "
                  f"{post_channel_id}: {e}")

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

    The dict's other fields are present-but-empty when ``has_data`` is
    ``False`` so callers can branch cleanly.
    """
    from config import get_growth_config

    empty = {
        "has_data":          False,
        "prev_period_label": "",
        "curr_period_label": "",
        "metric_labels":     [],
        "summary":           {},
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

    try:
        sh = _get_spreadsheet(guild_id)
        ws = sh.worksheet(tab_breakdown)
    except Exception as e:
        print(f"[GROWTH] Could not open breakdown tab for guild {guild_id}: {e}")
        return empty

    values = ws.get_all_values()
    if not values or len(values) < 1 or not values[0]:
        return empty
    header = values[0]

    # Parse transition columns: each looks like
    # `{prev_label} - {curr_label} {metric} Bucket` or `... %`. The pair
    # appears together; we key by (prev, curr) and walk metrics in order.
    # Use the rightmost transition (the most recent snapshot).
    transitions: list[tuple[str, str]] = []
    seen_keys: set[tuple[str, str]] = set()
    transition_cols: dict[tuple[str, str], list[tuple[str, int, int]]] = {}
    # ↑ {(prev, curr) → [(metric, pct_col_idx, bucket_col_idx)]}

    for i, h in enumerate(header):
        if h.endswith(" Bucket"):
            # Find a matching `% column to its left.
            base = h[: -len(" Bucket")]
            pct_name = f"{base} %"
            try:
                pct_idx = header.index(pct_name)
            except ValueError:
                continue
            # `base` is `{prev_label} - {curr_label} {metric}` — split on
            # the first `' - '` for the prev/curr split, then strip the
            # metric off the curr side.
            if " - " not in base:
                continue
            prev_label, rest = base.split(" - ", 1)
            # `rest` is `{curr_label} {metric}`. Match against configured
            # metric labels (longest-first) so the split is unambiguous.
            metric_match = None
            for m in sorted((gcfg.get("metrics") or []), key=lambda m: -len(m["label"])):
                m_label = m["label"]
                suffix = f" {m_label}"
                if rest.endswith(suffix):
                    metric_match = m_label
                    curr_label = rest[: -len(suffix)]
                    break
            if metric_match is None:
                continue
            key = (prev_label, curr_label)
            if key not in seen_keys:
                seen_keys.add(key)
                transitions.append(key)
            transition_cols.setdefault(key, []).append((metric_match, pct_idx, i))

    if not transitions:
        return empty
    prev_period_label, curr_period_label = transitions[-1]
    metric_entries = transition_cols[(prev_period_label, curr_period_label)]
    # Preserve the configured metric order rather than column-discovery order.
    configured_order = [m["label"] for m in (gcfg.get("metrics") or [])]
    metric_entries.sort(
        key=lambda t: (configured_order.index(t[0]) if t[0] in configured_order else len(configured_order))
    )
    metric_labels = [t[0] for t in metric_entries]

    summary: dict = {m: {b: [] for b in BUCKET_ORDER} for m in metric_labels}
    for row in values[1:]:
        if not row or not row[0].strip():
            continue
        name = row[0].strip()
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
        "has_data":          True,
        "prev_period_label": prev_period_label,
        "curr_period_label": curr_period_label,
        "metric_labels":     metric_labels,
        "summary":           summary,
    }


def format_breakdown_embed(*, metric_labels: list[str],
                           breakdown_summary: dict,
                           prev_period_label: str,
                           curr_period_label: str,
                           label_overrides: dict | None = None,
                           bucket_filter: list[str] | None = None):
    """Render the breakdown summary as a Discord embed. Shared by the
    Premium auto-post, the `/growth overview` "📊 See most recent Breakdown"
    button, and the standalone `/growth breakdown` leaf so all three views
    read the same. `bucket_filter` is a list of canonical bucket keys to
    include; empty list = include every bucket (the typical case).
    """
    import discord
    label_overrides = label_overrides or {}
    bucket_filter   = bucket_filter or []

    def _label(bucket: str) -> str:
        return str(label_overrides.get(bucket) or DEFAULT_BUCKET_LABELS[bucket])

    embed = discord.Embed(
        title=f"📊 Growth Breakdown — {prev_period_label} → {curr_period_label}",
        color=discord.Color.blue(),
    )

    for metric in metric_labels:
        per_bucket = breakdown_summary.get(metric, {})
        sections: list[str] = []
        for bucket in BUCKET_ORDER:
            if bucket_filter and bucket not in bucket_filter:
                continue
            names = per_bucket.get(bucket, [])
            if not names:
                continue
            sections.append(f"**{_label(bucket)}** ({len(names)})\n" + ", ".join(names))
        value = "\n\n".join(sections) if sections else "*No members in the included buckets.*"
        # Embed field value cap is 1024 chars; truncate with an ellipsis if
        # a metric has too many members to fit.
        if len(value) > 1020:
            value = value[:1017] + "…"
        embed.add_field(name=metric, value=value, inline=False)

    return embed

