"""
config.py — Per-guild configuration for LW Alliance Helper

All server-specific values live here. Guild configs are stored in a SQLite
database (guild_configs.db) so each server that installs the bot can have
its own settings.

For a new server, run /setup in Discord to configure the bot.
The OGV guild config is seeded automatically as the default.
"""

import json
import os
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional

DB_PATH       = os.getenv("CONFIG_DB_PATH",    "/app/data/guild_configs.db")

# ── OGV default values (seeded on first run) ───────────────────────────────────

OGV_GUILD_ID = 1266229297723605052

OGV_DEFAULTS = {
    "guild_id":                  1266229297723605052,
    "leadership_channel_id":     1488693874938482799,
    "announcement_channel_id":   1414725199257010336,
    "leadership_category_id":    1266243885743603783,
    "member_role_id":            1266235041600503880,
    "member_role_name":          "OGV",
    "leadership_role_name":      "OGV Leadership",
    "survey_channel_id":         1399401720026759198,
    "survey_notify_channel_id":  1405930574253920408,
    "storm_log_thread_id":       1483977424231469229,
    "spreadsheet_id":            "",   # populated from SPREADSHEET_ID env var on first run
    # Sheet tab names
    "tab_squad_powers":          "Squad Powers",
    "tab_growth_tracking":       "Growth Tracking",
    "tab_train_schedule":        "Train Schedule",
    "tab_ds_assignments":        "DS Assignments",
    "tab_sitouts":               "DS-CS Sit-outs",
    "tab_survey_history":        "Survey History",
    "tab_member_default":        "Season 5 - Off-Season",
    # Event timing
    "anchor_date":               "2026-03-30",
    "cycle_days":                3,
    "marauder_time_normal":      "22:15",
    "siege_time_normal":         "22:45",
    "marauder_time_saturday":    "17:00",
    "siege_time_saturday":       "17:30",
    "shield_warning_time":       "21:55",
}


# ── GuildConfig dataclass ──────────────────────────────────────────────────────

@dataclass
class GuildConfig:
    guild_id:                 int
    leadership_channel_id:    int        = 0
    announcement_channel_id:  int        = 0
    leadership_category_id:   int        = 0
    member_role_id:           int        = 0
    member_role_name:         str        = "OGV"
    leadership_role_name:     str        = "OGV Leadership"
    survey_channel_id:        int        = 0
    survey_notify_channel_id: int        = 0
    storm_log_thread_id:      int        = 0
    spreadsheet_id:           str        = ""
    tab_squad_powers:         str        = "Squad Powers"
    tab_growth_tracking:      str        = "Growth Tracking"
    tab_train_schedule:       str        = "Train Schedule"
    tab_ds_assignments:       str        = "DS Assignments"
    tab_sitouts:              str        = "DS-CS Sit-outs"
    tab_survey_history:       str        = "Survey History"
    tab_member_default:       str        = "Season 5 - Off-Season"
    anchor_date:              str        = "2026-03-30"
    cycle_days:               int        = 3
    marauder_time_normal:     str        = "22:15"
    siege_time_normal:        str        = "22:45"
    marauder_time_saturday:   str        = "17:00"
    siege_time_saturday:      str        = "17:30"
    shield_warning_time:      str        = "21:55"
    setup_complete:           bool       = False

    def anchor_date_parsed(self) -> date:
        return date.fromisoformat(self.anchor_date)

    def parse_time(self, time_str: str) -> tuple[int, int]:
        """Parse 'HH:MM' into (hour, minute)."""
        h, m = time_str.split(":")
        return int(h), int(m)

    @property
    def role_mention(self) -> str:
        return f"<@&{self.member_role_id}>"


# ── Database layer ─────────────────────────────────────────────────────────────

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create the guild_configs table if it doesn't exist and seed OGV defaults."""
    # Ensure the data directory exists (Railway volume mount point)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_configs (
                guild_id                 INTEGER PRIMARY KEY,
                leadership_channel_id    INTEGER DEFAULT 0,
                announcement_channel_id  INTEGER DEFAULT 0,
                leadership_category_id   INTEGER DEFAULT 0,
                member_role_id           INTEGER DEFAULT 0,
                member_role_name         TEXT    DEFAULT 'OGV',
                leadership_role_name     TEXT    DEFAULT 'OGV Leadership',
                survey_channel_id        INTEGER DEFAULT 0,
                survey_notify_channel_id INTEGER DEFAULT 0,
                storm_log_thread_id      INTEGER DEFAULT 0,
                spreadsheet_id           TEXT    DEFAULT '',
                tab_squad_powers         TEXT    DEFAULT 'Squad Powers',
                tab_growth_tracking      TEXT    DEFAULT 'Growth Tracking',
                tab_train_schedule       TEXT    DEFAULT 'Train Schedule',
                tab_ds_assignments       TEXT    DEFAULT 'DS Assignments',
                tab_sitouts              TEXT    DEFAULT 'DS-CS Sit-outs',
                tab_survey_history       TEXT    DEFAULT 'Survey History',
                tab_member_default       TEXT    DEFAULT 'Season 5 - Off-Season',
                anchor_date              TEXT    DEFAULT '2026-03-30',
                cycle_days               INTEGER DEFAULT 3,
                marauder_time_normal     TEXT    DEFAULT '22:15',
                siege_time_normal        TEXT    DEFAULT '22:45',
                marauder_time_saturday   TEXT    DEFAULT '17:00',
                siege_time_saturday      TEXT    DEFAULT '17:30',
                shield_warning_time      TEXT    DEFAULT '21:55',
                setup_complete           INTEGER DEFAULT 0
            )
        """)
        conn.commit()

        # guild_events — one row per event type per guild
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_events (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id                INTEGER NOT NULL,
                short_key               TEXT    NOT NULL,
                name                    TEXT    NOT NULL,
                timezone                TEXT    NOT NULL DEFAULT 'America/New_York',
                default_time            TEXT    NOT NULL DEFAULT '22:00',
                announcement_blurb      TEXT    NOT NULL DEFAULT '',
                schedule_type           TEXT    NOT NULL DEFAULT 'repeating',
                anchor_date             TEXT    DEFAULT '',
                interval_days           INTEGER DEFAULT 3,
                draft_channel_id        INTEGER DEFAULT 0,
                announcement_channel_id INTEGER DEFAULT 0,
                draft_time              TEXT    DEFAULT '12:00',
                five_min_warning        INTEGER DEFAULT 1,
                active                  INTEGER DEFAULT 1,
                UNIQUE(guild_id, short_key)
            )
        """)

        # Add spreadsheet_id column if upgrading from an older schema that didn't have it
        try:
            conn.execute("ALTER TABLE guild_configs ADD COLUMN spreadsheet_id TEXT DEFAULT ''")
            conn.commit()
            print("[CONFIG] Added spreadsheet_id column to existing database")
        except Exception:
            pass  # Column already exists — expected on fresh or already-upgraded installs

        # Seed OGV defaults if not already present
        existing = conn.execute(
            "SELECT guild_id FROM guild_configs WHERE guild_id = ?",
            (OGV_GUILD_ID,)
        ).fetchone()
        if not existing:
            cols         = ", ".join(OGV_DEFAULTS.keys())
            placeholders = ", ".join(["?"] * len(OGV_DEFAULTS))
            values       = list(OGV_DEFAULTS.values())
            conn.execute(
                f"INSERT INTO guild_configs ({cols}, setup_complete) VALUES ({placeholders}, 1)",
                values,
            )
            conn.commit()
            print(f"[CONFIG] Seeded OGV default config for guild {OGV_GUILD_ID}")

        # Ensure OGV's spreadsheet_id is populated from env var if not already stored.
        # This treats OGV the same as any other server — everything lives in the database.
        row = conn.execute(
            "SELECT spreadsheet_id FROM guild_configs WHERE guild_id = ?",
            (OGV_GUILD_ID,)
        ).fetchone()
        if row and not row[0]:
            env_sheet_id = os.getenv("SPREADSHEET_ID", "")
            if env_sheet_id:
                conn.execute(
                    "UPDATE guild_configs SET spreadsheet_id = ? WHERE guild_id = ?",
                    (env_sheet_id, OGV_GUILD_ID),
                )
                conn.commit()
                print(f"[CONFIG] Persisted SPREADSHEET_ID env var to database for OGV")

    # Seed OGV events after table is ready
    seed_ogv_events()


def get_config(guild_id: int) -> Optional[GuildConfig]:
    """Retrieve config for a guild. Returns None if not found."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_configs WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        if row is None:
            return None
        return GuildConfig(**dict(row))


def get_or_create_config(guild_id: int) -> GuildConfig:
    """Get config for a guild, creating an empty one if it doesn't exist."""
    cfg = get_config(guild_id)
    if cfg is not None:
        return cfg
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO guild_configs (guild_id) VALUES (?)", (guild_id,)
        )
        conn.commit()
    return get_config(guild_id)


def save_config(cfg: GuildConfig):
    """Insert or replace a guild's config."""
    d = asdict(cfg)
    cols         = ", ".join(d.keys())
    placeholders = ", ".join(["?"] * len(d))
    updates      = ", ".join(f"{k} = excluded.{k}" for k in d if k != "guild_id")
    with _get_conn() as conn:
        conn.execute(
            f"INSERT INTO guild_configs ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {updates}",
            list(d.values()),
        )
        conn.commit()


def update_config_field(guild_id: int, field: str, value) -> bool:
    """Update a single field for a guild's config."""
    with _get_conn() as conn:
        conn.execute(
            f"UPDATE guild_configs SET {field} = ? WHERE guild_id = ?",
            (value, guild_id),
        )
        conn.commit()
        return conn.execute(
            "SELECT changes()"
        ).fetchone()[0] > 0


def set_member_tab(guild_id: int, tab_name: str):
    """Update the active member tab name for a guild."""
    update_config_field(guild_id, "tab_member_default", tab_name)


def get_member_tab(guild_id: int) -> str:
    """Get the active member tab name for a guild."""
    cfg = get_config(guild_id)
    return cfg.tab_member_default if cfg else "Season 5 - Off-Season"


def get_spreadsheet_id(guild_id: int) -> str:
    """Get the Google Sheet ID for a guild from the config database."""
    cfg = get_config(guild_id)
    return cfg.spreadsheet_id if cfg and cfg.spreadsheet_id else ""


def is_setup_complete(guild_id: int) -> bool:
    """Check if a guild has completed setup."""
    cfg = get_config(guild_id)
    return cfg.setup_complete if cfg else False


# ── Guild event helpers ────────────────────────────────────────────────────────

def get_guild_events(guild_id: int, active_only: bool = True) -> list:
    """Return all event configs for a guild as dicts."""
    with _get_conn() as conn:
        if active_only:
            rows = conn.execute(
                "SELECT * FROM guild_events WHERE guild_id = ? AND active = 1 ORDER BY id",
                (guild_id,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM guild_events WHERE guild_id = ? ORDER BY id",
                (guild_id,)
            ).fetchall()
        return [dict(r) for r in rows]


def get_guild_event(guild_id: int, short_key: str) -> dict | None:
    """Return a single event config by short_key."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_events WHERE guild_id = ? AND short_key = ?",
            (guild_id, short_key)
        ).fetchone()
        return dict(row) if row else None


def save_guild_event(guild_id: int, event: dict):
    """Insert or replace an event config for a guild."""
    event["guild_id"] = guild_id
    cols         = ", ".join(event.keys())
    placeholders = ", ".join(["?"] * len(event))
    updates      = ", ".join(f"{k} = excluded.{k}" for k in event if k not in ("id", "guild_id", "short_key"))
    with _get_conn() as conn:
        conn.execute(
            f"INSERT INTO guild_events ({cols}) VALUES ({placeholders}) "
            f"ON CONFLICT(guild_id, short_key) DO UPDATE SET {updates}",
            list(event.values()),
        )
        conn.commit()


def delete_guild_event(guild_id: int, short_key: str):
    """Soft-delete an event by marking it inactive."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE guild_events SET active = 0 WHERE guild_id = ? AND short_key = ?",
            (guild_id, short_key)
        )
        conn.commit()


def seed_ogv_events():
    """Seed OGV's two default events if they don't already exist."""
    ogv_events = [
        {
            "short_key":               "marauder",
            "name":                    "Plague Marauder (AE)",
            "timezone":                "America/New_York",
            "default_time":            "22:15",
            "announcement_blurb":      "Plague Marauder (AE) at {time} ({server_time} Server Time). Make sure to have offline participation checked!",
            "schedule_type":           "repeating",
            "anchor_date":             "2026-03-30",
            "interval_days":           3,
            "draft_channel_id":        1488693874938482799,
            "announcement_channel_id": 1414725199257010336,
            "draft_time":              "12:00",
            "five_min_warning":        1,
            "active":                  1,
        },
        {
            "short_key":               "siege",
            "name":                    "Zombie Siege",
            "timezone":                "America/New_York",
            "default_time":            "22:45",
            "announcement_blurb":      "Zombie Siege at {time} ({server_time} Server Time).",
            "schedule_type":           "repeating",
            "anchor_date":             "2026-03-30",
            "interval_days":           3,
            "draft_channel_id":        1488693874938482799,
            "announcement_channel_id": 1414725199257010336,
            "draft_time":              "12:00",
            "five_min_warning":        1,
            "active":                  1,
        },
    ]
    for event in ogv_events:
        existing = get_guild_event(OGV_GUILD_ID, event["short_key"])
        if not existing:
            save_guild_event(OGV_GUILD_ID, event)
    print("[CONFIG] OGV events seeded")
