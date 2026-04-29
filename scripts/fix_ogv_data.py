#!/usr/bin/env python3
"""One-shot data fixes for OGV's row before the seed code is stripped.

Run ONCE on Railway by setting ``FIX_OGV_DATA=1`` in the bot's
environment, redeploying, watching the logs, and unsetting the env
var afterwards.

Each fix is **idempotent** — re-running it has no effect once the
data is already in the desired shape, so it's safe if the env var
gets left on for an extra restart by accident.

Three fixes:

1. ``guild_storm_config`` (CS row): the mail template was seeded with
   the old ``{subs_list}`` placeholder name. The codebase migrated to
   ``{subs}`` long ago, so OGV's CS mails were rendering with literal
   ``{subs_list}`` text. We replace any remaining ``{subs_list}`` with
   ``{subs}`` for OGV's row only.

2. ``guild_train_config``: OGV's row was created at some point with
   ``reminder_channel_id = 0`` (probably from the old default before
   the seed function picked up the leadership-channel ID). We set it
   to OGV's leadership channel so the daily train reminder actually
   posts somewhere.

3. ``guild_configs``: ``event_draft_channel_id`` and
   ``event_announce_channel_id`` are 0 for OGV because the columns
   were added via ``ALTER TABLE`` AFTER OGV's row was inserted, and
   the ``INSERT IF NOT EXISTS`` seed never backfilled them. They're
   actively read by ``/setup_events`` as the leadership-wide draft
   and announcement defaults. Set them to match the values already
   stored in OGV's ``guild_events`` rows.

Discoverable via ``railway logs`` after the redeploy fires. The script
prints exactly what it changed (or "no change needed") for each fix.

ZERO new INSERTs. Pure UPDATEs scoped to OGV's guild_id. Safe to run
any time the env var is set.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


# Hardcoded so the script is self-contained and survives the upcoming
# refactor that will rename / move OGV constants.
OGV_GUILD_ID = 1266229297723605052
OGV_LEADERSHIP_CHANNEL_ID = 1488693874938482799
OGV_ANNOUNCEMENTS_CHANNEL_ID = 1414725199257010336

DB_PATH = os.getenv("CONFIG_DB_PATH", "/app/data/guild_configs.db")


def fix_cs_subs_list_placeholder(conn: sqlite3.Connection) -> None:
    """Replace `{subs_list}` with `{subs}` in OGV's CS storm template."""
    row = conn.execute(
        "SELECT mail_template FROM guild_storm_config WHERE guild_id = ? AND event_type = 'CS'",
        (OGV_GUILD_ID,),
    ).fetchone()

    if row is None:
        print("  ⚪ No CS row for OGV — nothing to fix")
        return

    current = row[0] or ""
    if "{subs_list}" not in current:
        print("  ⚪ CS template already uses `{subs}` — no change needed")
        return

    fixed = current.replace("{subs_list}", "{subs}")
    conn.execute(
        "UPDATE guild_storm_config SET mail_template = ? WHERE guild_id = ? AND event_type = 'CS'",
        (fixed, OGV_GUILD_ID),
    )
    conn.commit()
    print(f"  ✅ Replaced {{subs_list}} → {{subs}} in CS template ({len(current)} → {len(fixed)} chars)")


def fix_train_reminder_channel(conn: sqlite3.Connection) -> None:
    """Set OGV's train reminder_channel_id to the leadership channel
    if it's currently 0 (unset).
    """
    row = conn.execute(
        "SELECT reminder_channel_id FROM guild_train_config WHERE guild_id = ?",
        (OGV_GUILD_ID,),
    ).fetchone()

    if row is None:
        print("  ⚪ No train config row for OGV — nothing to fix")
        return

    current = row[0]
    if current == OGV_LEADERSHIP_CHANNEL_ID:
        print(f"  ⚪ Train reminder_channel_id already set to {current} — no change needed")
        return

    if current != 0:
        # Don't stomp a non-zero value — only fix the broken 0 case.
        print(f"  ⚪ Train reminder_channel_id = {current} (not 0) — leaving alone")
        return

    conn.execute(
        "UPDATE guild_train_config SET reminder_channel_id = ? WHERE guild_id = ?",
        (OGV_LEADERSHIP_CHANNEL_ID, OGV_GUILD_ID),
    )
    conn.commit()
    print(f"  ✅ Train reminder_channel_id: 0 → {OGV_LEADERSHIP_CHANNEL_ID}")


def fix_event_channel_defaults(conn: sqlite3.Connection) -> None:
    """Set OGV's `event_draft_channel_id` and `event_announce_channel_id`
    in `guild_configs` to the values already in their `guild_events`
    rows. These columns were added via ALTER TABLE after OGV's row
    was inserted, so the seed never populated them.
    """
    row = conn.execute(
        "SELECT event_draft_channel_id, event_announce_channel_id "
        "FROM guild_configs WHERE guild_id = ?",
        (OGV_GUILD_ID,),
    ).fetchone()

    if row is None:
        print("  ⚪ No guild_configs row for OGV — nothing to fix")
        return

    current_draft, current_announce = row
    actions: list[str] = []

    if current_draft != OGV_LEADERSHIP_CHANNEL_ID:
        if current_draft == 0:
            conn.execute(
                "UPDATE guild_configs SET event_draft_channel_id = ? WHERE guild_id = ?",
                (OGV_LEADERSHIP_CHANNEL_ID, OGV_GUILD_ID),
            )
            actions.append(f"event_draft_channel_id: 0 → {OGV_LEADERSHIP_CHANNEL_ID}")
        else:
            actions.append(f"event_draft_channel_id = {current_draft} (not 0) — leaving alone")

    if current_announce != OGV_ANNOUNCEMENTS_CHANNEL_ID:
        if current_announce == 0:
            conn.execute(
                "UPDATE guild_configs SET event_announce_channel_id = ? WHERE guild_id = ?",
                (OGV_ANNOUNCEMENTS_CHANNEL_ID, OGV_GUILD_ID),
            )
            actions.append(f"event_announce_channel_id: 0 → {OGV_ANNOUNCEMENTS_CHANNEL_ID}")
        else:
            actions.append(f"event_announce_channel_id = {current_announce} (not 0) — leaving alone")

    if not actions:
        print("  ⚪ Both event channel defaults already set — no change needed")
        return

    conn.commit()
    for action in actions:
        if "→" in action:
            print(f"  ✅ {action}")
        else:
            print(f"  ⚪ {action}")


def run_fixes(db_path: str | None = None) -> int:
    """Run every OGV data fix. Returns 0 on success, 1 if the DB is missing.

    Importable from ``bot.py``; called once on startup when the
    ``FIX_OGV_DATA`` env var is set to ``1``.
    """
    path = db_path or DB_PATH

    print("=" * 72)
    print("OGV DATA FIXES")
    print("=" * 72)
    print(f"DB path     : {path}")
    print(f"OGV guild   : {OGV_GUILD_ID}")
    print()

    if not Path(path).exists():
        print(f"❌ DB file not found at {path}")
        return 1

    conn = sqlite3.connect(path)

    print("Fix 1: CS storm template — `{subs_list}` → `{subs}`")
    fix_cs_subs_list_placeholder(conn)

    print()
    print("Fix 2: Train reminder_channel_id (set to leadership if 0)")
    fix_train_reminder_channel(conn)

    print()
    print("Fix 3: guild_configs event channel defaults (set if 0)")
    fix_event_channel_defaults(conn)

    conn.close()

    print()
    print("=" * 72)
    print("DONE — unset FIX_OGV_DATA on Railway to stop running on every restart.")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(run_fixes())
