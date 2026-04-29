#!/usr/bin/env python3
"""Pre-flight check before stripping OGV-specific seeding code.

Reads (does NOT modify) every guild_* table looking for OGV's rows.
Prints what's there per table; calls out anything that's missing or
looks empty so we can decide whether to manually INSERT/UPDATE before
deleting the seed code.

Two ways to run this:

1. **Embedded in bot startup** (production / Railway): set the env
   var ``VERIFY_OGV_DATA=1`` and redeploy. ``bot.py`` imports
   :func:`run_verification` and calls it once on startup, dumping the
   audit to stdout (which Railway captures in its log stream). The
   bot then continues its normal startup. To stop running on every
   restart, just unset the env var.

2. **Standalone** (local dev with a local SQLite copy): run as a
   regular script. Will look at ``CONFIG_DB_PATH`` or the default
   ``/app/data/guild_configs.db``::

       python scripts/verify_ogv_data.py

   This is mostly useless on a fresh dev machine since you won't
   have the production DB locally — keep it for cases where you've
   already snapshotted the file.

ZERO writes. Pure SELECT. Safe to run any time.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path

# Hardcoded on purpose so this script is self-contained and survives
# the upcoming refactor that will rename / move OGV constants.
OGV_GUILD_ID = 1266229297723605052

DB_PATH = os.getenv("CONFIG_DB_PATH", "/app/data/guild_configs.db")

# Tables we expect OGV to have data in, with a one-line description and
# the number of rows we'd expect under the current seed model. If the
# actual row count differs from `expected_rows`, the script flags it.
TABLES = [
    ("guild_configs",                "Core config — roles, channels, sheet ID, timezone",         1),
    ("guild_events",                 "Event library — Plague Marauder, Zombie Siege, etc.",       2),
    ("guild_train_config",           "Train schedule — themes, tones, prompt template",           1),
    ("guild_growth_config",          "Growth tracking — source tab, metrics, snapshot schedule",  1),
    ("guild_survey_config",          "Default survey — channels, tabs, intro, questions",         1),
    ("guild_extra_surveys",          "Premium named surveys (in addition to the default)",       "any"),
    ("guild_birthday_config",        "Birthday tab + columns + train integration",                1),
    ("guild_member_roster_config",   "Member Roster Sync (Premium) — configured?",               "any"),
    ("guild_storm_config",           "Desert Storm + Canyon Storm — 2 rows (one per event)",      2),
]


def fetch_rows(conn: sqlite3.Connection, table: str) -> list[dict]:
    """Return all rows for OGV's guild_id in `table` as a list of dicts.
    Returns [] if the table doesn't exist or has no rows for OGV.
    """
    try:
        cur = conn.execute(
            f"SELECT * FROM {table} WHERE guild_id = ?",
            (OGV_GUILD_ID,),
        )
    except sqlite3.OperationalError as e:
        print(f"  ⚠️  Table missing or query failed: {e}")
        return []

    cols = [c[0] for c in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def render_value(v) -> str:
    """Compact renderer — keeps NULLs visible, truncates long blobs."""
    if v is None:
        return "<NULL>"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    s = str(v)
    if len(s) > 200:
        return s[:200] + f"… <truncated, {len(s)} chars total>"
    return s


def print_row(row: dict, indent: int = 4) -> None:
    pad = " " * indent
    width = max((len(k) for k in row.keys()), default=0)
    for k, v in row.items():
        print(f"{pad}{k.ljust(width)}  {render_value(v)}")


def run_verification(db_path: str | None = None) -> int:
    """Run the OGV data verification, printing the full audit to stdout.

    Args:
        db_path: Override the DB location. Defaults to the module-level
            ``DB_PATH`` constant which itself defaults to
            ``$CONFIG_DB_PATH`` or ``/app/data/guild_configs.db``.

    Returns:
        ``0`` if every required table has its expected row count,
        ``1`` if the DB file is missing,
        ``2`` if any required table is missing rows for OGV.

    Importable from ``bot.py`` so the same logic runs whether you
    invoke the script directly or trigger it on bot startup via the
    ``VERIFY_OGV_DATA`` env var.
    """
    path = db_path or DB_PATH

    print("=" * 72)
    print("OGV DATA VERIFICATION")
    print("=" * 72)
    print(f"DB path     : {path}")
    print(f"OGV guild   : {OGV_GUILD_ID}")
    print()

    if not Path(path).exists():
        print(f"❌ DB file not found at {path}")
        print("   If running on Railway, the path env var may be different.")
        print("   If running locally, this is expected — embed in bot startup instead.")
        return 1

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row

    summary: list[tuple[str, int, str]] = []  # (table, row_count, status)

    for table, description, expected in TABLES:
        print("-" * 72)
        print(f"TABLE: {table}")
        print(f"  Purpose: {description}")
        rows = fetch_rows(conn, table)
        print(f"  Rows for OGV: {len(rows)}  (expected: {expected})")

        if not rows:
            if expected == "any":
                status = "⚪ optional — no row, fine"
            else:
                status = f"❌ MISSING — expected {expected} row(s)"
            print(f"  Status: {status}")
            summary.append((table, 0, status))
            continue

        if isinstance(expected, int) and len(rows) != expected:
            status = f"⚠️  count mismatch — expected {expected}, got {len(rows)}"
        else:
            status = "✅ present"
        print(f"  Status: {status}")
        summary.append((table, len(rows), status))

        for i, row in enumerate(rows, 1):
            print(f"\n  Row {i}:")
            print_row(row)
        print()

    conn.close()

    print("=" * 72)
    print("SUMMARY")
    print("=" * 72)
    width = max(len(t) for t, _, _ in summary)
    for table, count, status in summary:
        print(f"  {table.ljust(width)}  rows={count}  {status}")

    # Exit non-zero if any non-optional table is missing — handy for
    # scripting later, but the human reading the output is the main
    # consumer.
    missing = [t for t, c, s in summary if s.startswith("❌")]
    if missing:
        print()
        print(f"⚠️  {len(missing)} table(s) need attention before stripping seed code:")
        for t in missing:
            print(f"     - {t}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(run_verification())
