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
    "ds_log_channel_id":         1483977424231469229,
    "cs_log_channel_id":         1483977424231469229,
    "event_draft_channel_id":    1488693874938482799,
    "event_announce_channel_id": 1414725199257010336,
    "event_draft_time":          "12:00",
    "event_five_min_warning":    1,
    "spreadsheet_id":            "",   # populated from SPREADSHEET_ID env var on first run
    "timezone":                  "America/New_York",
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
    ds_log_channel_id:        int        = 0
    cs_log_channel_id:        int        = 0
    event_draft_channel_id:   int        = 0
    event_announce_channel_id:int        = 0
    event_draft_time:         str        = "12:00"
    event_five_min_warning:   int        = 1
    spreadsheet_id:           str        = ""
    timezone:                 str        = "America/New_York"
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
                ds_log_channel_id          INTEGER DEFAULT 0,
                cs_log_channel_id          INTEGER DEFAULT 0,
                event_draft_channel_id     INTEGER DEFAULT 0,
                event_announce_channel_id  INTEGER DEFAULT 0,
                event_draft_time           TEXT    DEFAULT '12:00',
                event_five_min_warning     INTEGER DEFAULT 1,
                spreadsheet_id           TEXT    DEFAULT '',
                timezone                 TEXT    DEFAULT 'America/New_York',
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

        # guild_train_config — per-guild train schedule settings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_train_config (
                guild_id             INTEGER PRIMARY KEY,
                tab_name             TEXT    DEFAULT 'Train Schedule',
                blurbs_enabled       INTEGER DEFAULT 1,
                themes               TEXT    DEFAULT '',
                tones                TEXT    DEFAULT '',
                prompt_template      TEXT    DEFAULT '',
                templates_json       TEXT    DEFAULT '[]',
                default_template     TEXT    DEFAULT 'Default',
                default_tone         TEXT    DEFAULT '',
                reminders_enabled    INTEGER DEFAULT 1,
                reminder_channel_id  INTEGER DEFAULT 0,
                reminder_time        TEXT    DEFAULT '22:00'
            )
        """)
        conn.commit()

        # Seed OGV train config
        _seed_ogv_train_config(conn)

        # guild_growth_config — per-guild growth tracking settings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_growth_config (
                guild_id             INTEGER PRIMARY KEY,
                enabled              INTEGER DEFAULT 0,
                tab_source           TEXT    DEFAULT '',
                name_col             TEXT    DEFAULT 'A',
                metrics              TEXT    DEFAULT '',
                tab_growth           TEXT    DEFAULT 'Growth Tracking',
                snapshot_frequency   TEXT    DEFAULT 'monthly',
                snapshot_day         INTEGER DEFAULT 1,
                snapshot_interval    INTEGER DEFAULT 30,
                data_start_row       INTEGER DEFAULT 2
            )
        """)
        conn.commit()
        _seed_ogv_growth_config(conn)

        # guild_survey_config — per-guild survey questions and sheet settings
        # The main table holds the "default" survey (one per guild). Premium
        # subscribers can add extra named surveys in `guild_extra_surveys`.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_survey_config (
                guild_id         INTEGER PRIMARY KEY,
                tab_squad_powers TEXT    DEFAULT 'Squad Powers',
                tab_history      TEXT    DEFAULT 'Survey History',
                questions        TEXT    DEFAULT '',
                intro_message    TEXT    DEFAULT ''
            )
        """)
        # guild_extra_surveys — additional named surveys (Premium feature)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_extra_surveys (
                guild_id              INTEGER NOT NULL,
                survey_id             TEXT    NOT NULL,
                survey_name           TEXT    NOT NULL,
                tab_squad_powers      TEXT    DEFAULT 'Squad Powers',
                tab_history           TEXT    DEFAULT 'Survey History',
                questions             TEXT    DEFAULT '',
                intro_message         TEXT    DEFAULT '',
                survey_channel_id     INTEGER DEFAULT 0,
                notify_channel_id     INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, survey_id)
            )
        """)
        conn.commit()
        _seed_ogv_survey_config(conn)

        # guild_birthday_config — per-guild birthday settings
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_birthday_config (
                guild_id             INTEGER PRIMARY KEY,
                tab_name             TEXT    DEFAULT 'Birthdays',
                name_col             INTEGER DEFAULT 0,
                birthday_col         INTEGER DEFAULT 1,
                discord_id_col       INTEGER DEFAULT -1,
                data_start_row       INTEGER DEFAULT 2,
                enabled              INTEGER DEFAULT 1,
                train_integration    INTEGER DEFAULT 0,
                flexible_placement   INTEGER DEFAULT 1,
                lookahead_days       INTEGER DEFAULT 14,
                reminders_enabled    INTEGER DEFAULT 0,
                reminder_channel_id  INTEGER DEFAULT 0,
                reminder_time        TEXT    DEFAULT '08:00'
            )
        """)
        conn.commit()
        _seed_ogv_birthday_config(conn)

        # guild_member_roster_config — per-guild member-roster sync settings.
        # Premium-only feature: writes a list of all members with the configured
        # member role to a dedicated sheet tab so other premium features
        # (birthday DMs, train DMs, auto-mention, etc.) can look up Discord IDs.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_member_roster_config (
                guild_id        INTEGER PRIMARY KEY,
                enabled         INTEGER DEFAULT 0,
                tab_name        TEXT    DEFAULT 'Member Roster',
                discord_id_col  INTEGER DEFAULT 0,
                name_col        INTEGER DEFAULT 1,
                display_col     INTEGER DEFAULT 2,
                joined_col      INTEGER DEFAULT 3,
                roles_col       INTEGER DEFAULT 4,
                role_filter_id  INTEGER DEFAULT 0,
                auto_sync       INTEGER DEFAULT 1,
                last_synced_at  TEXT    DEFAULT ''
            )
        """)
        conn.commit()

        # guild_storm_config — per-guild DS/CS mail templates and time options.
        # `templates_json` and `default_template` support multiple named
        # templates per (guild, event_type) for premium subscribers.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_storm_config (
                guild_id             INTEGER NOT NULL,
                event_type           TEXT    NOT NULL,
                tab_name             TEXT    DEFAULT 'DS Assignments',
                mail_template        TEXT    DEFAULT '',
                templates_json       TEXT    DEFAULT '[]',
                default_template     TEXT    DEFAULT 'Default',
                time_option_1_label  TEXT    DEFAULT '',
                time_option_1_local  TEXT    DEFAULT '',
                time_option_1_server TEXT    DEFAULT '',
                time_option_2_label  TEXT    DEFAULT '',
                time_option_2_local  TEXT    DEFAULT '',
                time_option_2_server TEXT    DEFAULT '',
                timezone             TEXT    DEFAULT 'America/New_York',
                log_channel_id       INTEGER DEFAULT 0,
                post_channel_id      INTEGER DEFAULT 0,
                PRIMARY KEY (guild_id, event_type)
            )
        """)
        conn.commit()
        _seed_ogv_storm_config(conn)

        # Add spreadsheet_id column if upgrading from an older schema that didn't have it
        try:
            conn.execute("ALTER TABLE guild_configs ADD COLUMN spreadsheet_id TEXT DEFAULT ''")
            conn.commit()
            print("[CONFIG] Added spreadsheet_id column to existing database")
        except Exception:
            pass

        try:
            conn.execute("ALTER TABLE guild_configs ADD COLUMN event_draft_channel_id INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE guild_configs ADD COLUMN event_announce_channel_id INTEGER DEFAULT 0")
            conn.execute("ALTER TABLE guild_configs ADD COLUMN event_draft_time TEXT DEFAULT '12:00'")
            conn.execute("ALTER TABLE guild_configs ADD COLUMN event_five_min_warning INTEGER DEFAULT 1")
            conn.commit()
        except Exception:
            pass

        try:
            conn.execute("ALTER TABLE guild_storm_config ADD COLUMN log_channel_id INTEGER DEFAULT 0")
            conn.commit()
            print("[CONFIG] Added log_channel_id column to guild_storm_config")
        except Exception:
            pass

        for col, definition in [
            ("templates_json",   "TEXT DEFAULT '[]'"),
            ("default_template", "TEXT DEFAULT 'Default'"),
            ("post_channel_id",  "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE guild_storm_config ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to guild_storm_config")
            except Exception:
                pass

        # ── guild_extra_surveys migrations (per-survey reminder fields) ────────
        for col, definition in [
            ("reminder_message", "TEXT DEFAULT ''"),
            ("reminder_enabled", "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE guild_extra_surveys ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to guild_extra_surveys")
            except Exception:
                pass

        # ── guild_train_config migrations ──────────────────────────────────────
        for col, definition in [
            ("blurbs_enabled",      "INTEGER DEFAULT 1"),
            ("reminders_enabled",   "INTEGER DEFAULT 1"),
            ("reminder_channel_id", "INTEGER DEFAULT 0"),
            ("reminder_time",       "TEXT DEFAULT '22:00'"),
            ("templates_json",      "TEXT DEFAULT '[]'"),
            ("default_template",    "TEXT DEFAULT 'Default'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE guild_train_config ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to guild_train_config")
            except Exception:
                pass

        # ── guild_birthday_config migrations ───────────────────────────────────
        for col, definition in [
            ("discord_id_col",      "INTEGER DEFAULT -1"),
            ("train_integration",   "INTEGER DEFAULT 0"),
            ("flexible_placement",  "INTEGER DEFAULT 1"),
            ("reminders_enabled",   "INTEGER DEFAULT 0"),
            ("reminder_channel_id", "INTEGER DEFAULT 0"),
            ("reminder_time",       "TEXT DEFAULT '08:00'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE guild_birthday_config ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to guild_birthday_config")
            except Exception:
                pass

        # ── guild_configs event/survey channel migrations ──────────────────────
        for col, definition in [
            ("event_draft_channel_id",    "INTEGER DEFAULT 0"),
            ("event_announce_channel_id", "INTEGER DEFAULT 0"),
            ("event_draft_time",          "TEXT DEFAULT '12:00'"),
            ("event_five_min_warning",    "INTEGER DEFAULT 1"),
            ("survey_channel_id",         "INTEGER DEFAULT 0"),
            ("survey_notify_channel_id",  "INTEGER DEFAULT 0"),
            ("ds_log_channel_id",         "INTEGER DEFAULT 0"),
            ("cs_log_channel_id",         "INTEGER DEFAULT 0"),
            ("timezone",                  "TEXT DEFAULT 'America/New_York'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE guild_configs ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to guild_configs")
            except Exception:
                pass

        try:
            conn.execute("ALTER TABLE guild_configs ADD COLUMN timezone TEXT DEFAULT 'America/New_York'")
            conn.commit()
            print("[CONFIG] Added timezone column to existing database")
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


OGV_DEFAULT_THEMES = [
    "Welcome to the Alliance",
    "Birthday",
    "Milestone",
    "War / Performance",
    "General Celebration",
    "Contest / Raffle",
    "Custom",
]

OGV_DEFAULT_TONES = [
    "Default (match the theme)",
    "More casual",
    "More intense",
    "Funny",
    "Serious",
    "Cinematic / Dramatic",
]

OGV_DEFAULT_PROMPT = (
    "You are writing a short motivational alliance announcement blurb for a mobile strategy game called Last War.\n"
    "Keep it under 3 sentences. It should feel energetic and personal.\n\n"
    "Member name: {name}\n"
    "Theme: {theme}\n"
    "Tone: {tone}\n"
    "Notes: {notes}\n\n"
    "Write the blurb now:"
)


OGV_DS_TEMPLATE = """\
🔥 **{alliance_name} — Desert Storm**
Stay coordinated and flexible — let's take this.

🏆 **Zone Assignments**

{zones}

🔄 **Sub Pairs**
{subs}

⏳ **Timing**
{time}"""

OGV_CS_TEMPLATE = """\
⚡ **{alliance_name} — Canyon Storm**
Hit your zones fast and finish strong.

🏆 **Zone Assignments**

{zones}

🔄 **Subs**
{subs}

⏳ **Timing**
{time}"""

GENERIC_DS_TEMPLATE = """\
**{alliance_name} — Desert Storm**

**Zone Assignments**
{zones}

**Subs**
{subs}

**Time:** {time}"""

GENERIC_CS_TEMPLATE = """\
**{alliance_name} — Canyon Storm**

**Zone Assignments**
{zones}

**Subs**
{subs}

**Time:** {time}"""


def _seed_ogv_storm_config(conn):
    """Seed OGV's DS and CS storm config if not already present."""
    for event_type, template, t1_label, t1_local, t1_server, t2_label, t2_local, t2_server in [
        ("DS", OGV_DS_TEMPLATE, "4PM",  "4:00pm ET",  "18:00 Server Time", "9PM",  "9:00pm ET",  "01:00 Server Time"),
        ("CS", OGV_CS_TEMPLATE, "10AM", "10:00am ET", "12:00 Server Time", "9PM",  "9:00pm ET",  "23:00 Server Time"),
    ]:
        existing = conn.execute(
            "SELECT guild_id FROM guild_storm_config WHERE guild_id = ? AND event_type = ?",
            (OGV_GUILD_ID, event_type)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO guild_storm_config "
                "(guild_id, event_type, tab_name, mail_template, "
                "time_option_1_label, time_option_1_local, time_option_1_server, "
                "time_option_2_label, time_option_2_local, time_option_2_server, timezone) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (OGV_GUILD_ID, event_type, "DS Assignments", template,
                 t1_label, t1_local, t1_server,
                 t2_label, t2_local, t2_server, "America/New_York")
            )
    conn.commit()
    print("[CONFIG] Seeded OGV storm config")


def _normalize_storm_templates(d: dict, event_type: str) -> dict:
    """
    Lift a guild's storm-template list into the canonical shape:
      d["templates"] = list[{"name", "template"}], with at least one entry.
      d["default_template"] names which entry is the default for drafting.

    Migration: when templates_json is empty/null but the legacy
    `mail_template` column has content, treat it as the "Default" entry.
    """
    import json
    raw = d.get("templates_json") or "[]"
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = []
    if not isinstance(parsed, list):
        parsed = []
    if not parsed:
        legacy = (d.get("mail_template") or "").strip()
        if legacy:
            parsed = [{"name": "Default", "template": legacy}]
    if not parsed:
        parsed = [{
            "name": "Default",
            "template": GENERIC_DS_TEMPLATE if event_type == "DS" else GENERIC_CS_TEMPLATE,
        }]
    d["templates"] = parsed
    known_names = {t.get("name") for t in parsed}
    if not d.get("default_template") or d["default_template"] not in known_names:
        d["default_template"] = parsed[0]["name"]
    d["mail_template"] = parsed[0].get("template", "")  # back-compat
    return d


def get_storm_config(guild_id: int, event_type: str) -> dict:
    """Return storm config for a guild and event type (DS or CS)."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_storm_config WHERE guild_id = ? AND event_type = ?",
            (guild_id, event_type)
        ).fetchone()
    if row:
        return _normalize_storm_templates(dict(row), event_type)
    fallback = {
        "guild_id":             guild_id,
        "event_type":           event_type,
        "tab_name":             "DS Assignments",
        "mail_template":        GENERIC_DS_TEMPLATE if event_type == "DS" else GENERIC_CS_TEMPLATE,
        "templates_json":       "",
        "default_template":     "Default",
        "time_option_1_label":  "",
        "time_option_1_local":  "",
        "time_option_1_server": "",
        "time_option_2_label":  "",
        "time_option_2_local":  "",
        "time_option_2_server": "",
        "timezone":             "America/New_York",
        "post_channel_id":      0,
    }
    return _normalize_storm_templates(fallback, event_type)


def save_storm_config(guild_id: int, event_type: str, tab_name: str,
                      mail_template: str,
                      t1_label: str, t1_local: str, t1_server: str,
                      t2_label: str, t2_local: str, t2_server: str,
                      timezone: str, log_channel_id: int = 0,
                      templates: list | None = None,
                      default_template: str = "Default",
                      post_channel_id: int = 0):
    """
    Insert or replace a guild's storm config.

    Backwards-compatible: callers may still pass `mail_template` as a single
    string. When `templates` is None, the string is wrapped as a single
    "Default" entry. Premium callers may pass a list of named templates.
    `post_channel_id` is the channel where /[event]_draft will post the
    final mail when leadership clicks "Post & Copy".
    """
    import json
    if templates is None:
        templates = [{"name": "Default", "template": mail_template or ""}]
    templates_json = json.dumps(templates)
    default_text = next(
        (t["template"] for t in templates if t.get("name") == default_template),
        templates[0]["template"] if templates else "",
    )
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_storm_config "
            "(guild_id, event_type, tab_name, mail_template, templates_json, default_template, "
            "time_option_1_label, time_option_1_local, time_option_1_server, "
            "time_option_2_label, time_option_2_local, time_option_2_server, "
            "timezone, log_channel_id, post_channel_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, event_type) DO UPDATE SET "
            "tab_name=excluded.tab_name, mail_template=excluded.mail_template, "
            "templates_json=excluded.templates_json, "
            "default_template=excluded.default_template, "
            "time_option_1_label=excluded.time_option_1_label, "
            "time_option_1_local=excluded.time_option_1_local, "
            "time_option_1_server=excluded.time_option_1_server, "
            "time_option_2_label=excluded.time_option_2_label, "
            "time_option_2_local=excluded.time_option_2_local, "
            "time_option_2_server=excluded.time_option_2_server, "
            "timezone=excluded.timezone, "
            "log_channel_id=excluded.log_channel_id, "
            "post_channel_id=excluded.post_channel_id",
            (guild_id, event_type, tab_name, default_text, templates_json, default_template,
             t1_label, t1_local, t1_server, t2_label, t2_local, t2_server,
             timezone, log_channel_id, post_channel_id)
        )
        conn.commit()


def get_storm_template(guild_id: int, event_type: str, template_name: str | None = None) -> str:
    """Return a named storm mail template's body. Falls back to default."""
    cfg = get_storm_config(guild_id, event_type)
    target = template_name or cfg.get("default_template") or "Default"
    for t in cfg.get("templates", []):
        if t.get("name") == target:
            return t.get("template", "")
    templates = cfg.get("templates") or []
    return templates[0]["template"] if templates else (cfg.get("mail_template") or "")


def get_storm_template_names(guild_id: int, event_type: str) -> list[str]:
    """List of saved template names for a guild's storm config."""
    cfg = get_storm_config(guild_id, event_type)
    return [t.get("name", "") for t in (cfg.get("templates") or []) if t.get("name")]


# ── Storm event fixed Server Time constants ────────────────────────────────────
# These times are fixed by the game and never change.
# DS: 18:00 and 23:00 Server Time
# CS: 12:00 and 23:00 Server Time
# Server Time = UTC-2 (verified: 10pm ET summer = 22:00 EDT = 00:00 Server Time)

DS_SERVER_TIMES = [(18, 0), (23, 0)]
CS_SERVER_TIMES = [(12, 0), (23, 0)]

SERVER_TZ_OFFSET = -2  # Server Time is UTC-2


def server_time_to_local(hour: int, minute: int, guild_id: int) -> str:
    """
    Convert a Server Time (UTC-2) hour/minute to the guild's local timezone string.
    e.g. (18, 0) with ET timezone (summer) → "4:00pm EDT"
    """
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone as _tz, timedelta
    cfg    = get_config(guild_id)
    tz_str = cfg.timezone if cfg and cfg.timezone else "America/New_York"
    try:
        server_tz = _tz(timedelta(hours=SERVER_TZ_OFFSET))
        server_dt = datetime(2026, 6, 1, hour, minute, tzinfo=server_tz)  # use summer date
        local_dt  = server_dt.astimezone(ZoneInfo(tz_str))
        h12       = local_dt.hour % 12 or 12
        period    = "am" if local_dt.hour < 12 else "pm"
        tz_abbr   = local_dt.strftime("%Z")
        mins      = f":{local_dt.minute:02d}" if local_dt.minute != 0 else ""
        return f"{h12}{mins}{period} {tz_abbr}"
    except Exception:
        return f"{hour:02d}:{minute:02d} Server Time"


def get_storm_time_labels(event_type: str, guild_id: int) -> list:
    """
    Return list of (local_display, server_time_str) for DS or CS time buttons.
    e.g. [("4:00pm EDT", "18:00 Server Time"), ...]
    """
    times  = DS_SERVER_TIMES if event_type == "DS" else CS_SERVER_TIMES
    labels = []
    for h, m in times:
        local_str  = server_time_to_local(h, m, guild_id)
        server_str = f"{h:02d}:{m:02d} Server Time"
        labels.append((local_str, server_str))
    return labels


OGV_SURVEY_QUESTIONS = [
    {"key": "squad1_power",  "label": "1st Squad Power",           "type": "text",     "options": [],                                                      "placeholder": "e.g. 43.27", "max_chars": 5},
    {"key": "squad1_type",   "label": "1st Squad Type",            "type": "dropdown", "options": ["Missile", "Air", "Tank"],                              "placeholder": "Select squad type...", "max_chars": 0},
    {"key": "squad2_power",  "label": "2nd Squad Power",           "type": "text",     "options": [],                                                      "placeholder": "e.g. 43.27", "max_chars": 5},
    {"key": "squad3_power",  "label": "3rd Squad Power",           "type": "text",     "options": [],                                                      "placeholder": "e.g. 43.27", "max_chars": 5},
    {"key": "drone_level",   "label": "Drone Level",               "type": "text",     "options": [],                                                      "placeholder": "e.g. 243",   "max_chars": 5},
    {"key": "gorilla_level", "label": "Gorilla Level",             "type": "text",     "options": [],                                                      "placeholder": "e.g. 70",    "max_chars": 5},
    {"key": "thp",           "label": "Total Hero Power (THP)",    "type": "text",     "options": [],                                                      "placeholder": "e.g. 301",   "max_chars": 3},
    {"key": "total_kills",   "label": "Total Kills",               "type": "text",     "options": [],                                                      "placeholder": "e.g. 55.40", "max_chars": 5},
    {"key": "profession",    "label": "Profession",                "type": "dropdown", "options": ["War Leader", "Engineer"],                              "placeholder": "Select profession...", "max_chars": 0},
    {"key": "banner",        "label": "Charge Banner",             "type": "dropdown", "options": ["Yes", "No"],                                           "placeholder": "Select...",  "max_chars": 0},
    {"key": "aid_removal",   "label": "Medical Aid / Ruin Removal","type": "dropdown", "options": ["Yes", "Only Medical Aid", "Only Ruin Removal", "No"], "placeholder": "Select...",  "max_chars": 0},
]

OGV_SURVEY_INTRO = (
    "Please fill out this survey each week, if possible, to help us keep track of "
    "squad powers, better balance our Desert Storm teams, track alliance growth, "
    "and prepare for season events!"
)


def _seed_ogv_growth_config(conn):
    """Seed OGV's growth config if not already present."""
    import json
    existing = conn.execute(
        "SELECT guild_id FROM guild_growth_config WHERE guild_id = ?",
        (OGV_GUILD_ID,)
    ).fetchone()
    if not existing:
        ogv_metrics = [
            {"col": "E", "label": "1st Squad Power"},
            {"col": "G", "label": "2nd Squad Power"},
            {"col": "H", "label": "3rd Squad Power"},
        ]
        conn.execute(
            "INSERT INTO guild_growth_config "
            "(guild_id, enabled, tab_source, name_col, metrics, tab_growth, "
            "snapshot_frequency, snapshot_day, snapshot_interval, data_start_row) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (OGV_GUILD_ID, 1, "Squad Powers", "D", json.dumps(ogv_metrics),
             "Growth Tracking", "monthly", 1, 30, 10)
        )
        conn.commit()
        print("[CONFIG] Seeded OGV growth config")


def get_growth_config(guild_id: int) -> dict:
    """Return growth config for a guild."""
    import json
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_growth_config WHERE guild_id = ?",
            (guild_id,)
        ).fetchone()
    if row:
        d = dict(row)
        try:
            d["metrics"] = json.loads(d["metrics"]) if d["metrics"] else []
        except (json.JSONDecodeError, TypeError):
            d["metrics"] = []
        return d
    return {
        "guild_id":           guild_id,
        "enabled":            0,
        "tab_source":         "",
        "name_col":           "A",
        "metrics":            [],
        "tab_growth":         "Growth Tracking",
        "snapshot_frequency": "monthly",
        "snapshot_day":       1,
        "snapshot_interval":  30,
        "data_start_row":     2,
    }


def save_growth_config(guild_id: int, enabled: int, tab_source: str,
                       name_col: str, metrics: list, tab_growth: str,
                       snapshot_frequency: str, snapshot_day: int,
                       snapshot_interval: int, data_start_row: int):
    """Insert or replace a guild's growth config."""
    import json
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_growth_config "
            "(guild_id, enabled, tab_source, name_col, metrics, tab_growth, "
            "snapshot_frequency, snapshot_day, snapshot_interval, data_start_row) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "enabled=excluded.enabled, tab_source=excluded.tab_source, "
            "name_col=excluded.name_col, metrics=excluded.metrics, "
            "tab_growth=excluded.tab_growth, "
            "snapshot_frequency=excluded.snapshot_frequency, "
            "snapshot_day=excluded.snapshot_day, "
            "snapshot_interval=excluded.snapshot_interval, "
            "data_start_row=excluded.data_start_row",
            (guild_id, enabled, tab_source, name_col, json.dumps(metrics),
             tab_growth, snapshot_frequency, snapshot_day, snapshot_interval, data_start_row)
        )
        conn.commit()


def _seed_ogv_survey_config(conn):
    """Seed OGV's survey config if not already present."""
    import json
    existing = conn.execute(
        "SELECT guild_id FROM guild_survey_config WHERE guild_id = ?",
        (OGV_GUILD_ID,)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO guild_survey_config (guild_id, tab_squad_powers, tab_history, questions, intro_message) "
            "VALUES (?, ?, ?, ?, ?)",
            (OGV_GUILD_ID, "Squad Powers", "Survey History",
             json.dumps(OGV_SURVEY_QUESTIONS), OGV_SURVEY_INTRO)
        )
        conn.commit()
        print("[CONFIG] Seeded OGV survey config")


def get_survey_config(guild_id: int) -> dict:
    """Return survey config for a guild."""
    import json
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_survey_config WHERE guild_id = ?",
            (guild_id,)
        ).fetchone()
    if row:
        d = dict(row)
        try:
            d["questions"] = json.loads(d["questions"]) if d["questions"] else []
        except (json.JSONDecodeError, TypeError):
            d["questions"] = []
        return d
    return {
        "guild_id":        guild_id,
        "tab_squad_powers": "Squad Powers",
        "tab_history":     "Survey History",
        "questions":       [],
        "intro_message":   "",
    }


def save_survey_config(guild_id: int, tab_squad_powers: str, tab_history: str,
                       questions: list, intro_message: str):
    """Insert or replace a guild's default survey config."""
    import json
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_survey_config (guild_id, tab_squad_powers, tab_history, questions, intro_message) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "tab_squad_powers=excluded.tab_squad_powers, tab_history=excluded.tab_history, "
            "questions=excluded.questions, intro_message=excluded.intro_message",
            (guild_id, tab_squad_powers, tab_history, json.dumps(questions), intro_message)
        )
        conn.commit()


# ── Multi-survey helpers (Premium) ─────────────────────────────────────────────
#
# The "default" survey lives in guild_survey_config (single row per guild).
# Additional named surveys live in guild_extra_surveys, keyed by survey_id.
# Premium subscribers may use up to LIMITS["surveys"] (TBD when wizard lands).

def list_surveys(guild_id: int) -> list[dict]:
    """
    Return all surveys configured for a guild as a list of dicts. The first
    entry is always the default survey from guild_survey_config (id="default",
    name="Default"); the rest come from guild_extra_surveys.
    """
    surveys: list[dict] = []
    default_cfg = get_survey_config(guild_id)
    surveys.append({
        "survey_id":         "default",
        "survey_name":       "Default",
        "tab_squad_powers":  default_cfg.get("tab_squad_powers", "Squad Powers"),
        "tab_history":       default_cfg.get("tab_history", "Survey History"),
        "questions":         default_cfg.get("questions", []),
        "intro_message":     default_cfg.get("intro_message", ""),
        "survey_channel_id": 0,   # default uses guild-level channel
        "notify_channel_id": 0,
    })
    import json
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM guild_extra_surveys WHERE guild_id = ? ORDER BY survey_name",
            (guild_id,),
        ).fetchall()
    for row in rows:
        d = dict(row)
        try:
            d["questions"] = json.loads(d["questions"]) if d["questions"] else []
        except (json.JSONDecodeError, TypeError):
            d["questions"] = []
        surveys.append(d)
    return surveys


def get_survey(guild_id: int, survey_id: str = "default") -> dict | None:
    """
    Fetch a specific survey by id. `survey_id="default"` returns the main
    survey from guild_survey_config (always present). Other ids look up
    guild_extra_surveys; returns None if not found.
    """
    if survey_id == "default":
        cfg = get_survey_config(guild_id)
        return {
            "survey_id":         "default",
            "survey_name":       "Default",
            "tab_squad_powers":  cfg.get("tab_squad_powers", "Squad Powers"),
            "tab_history":       cfg.get("tab_history", "Survey History"),
            "questions":         cfg.get("questions", []),
            "intro_message":     cfg.get("intro_message", ""),
            "survey_channel_id": 0,
            "notify_channel_id": 0,
        }
    import json
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_extra_surveys WHERE guild_id = ? AND survey_id = ?",
            (guild_id, survey_id),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["questions"] = json.loads(d["questions"]) if d["questions"] else []
    except (json.JSONDecodeError, TypeError):
        d["questions"] = []
    return d


def save_extra_survey(
    guild_id: int, survey_id: str, *,
    survey_name: str,
    tab_squad_powers: str = "Squad Powers",
    tab_history: str       = "Survey History",
    questions: list        = None,
    intro_message: str     = "",
    survey_channel_id: int = 0,
    notify_channel_id: int = 0,
    reminder_message: str  = "",
    reminder_enabled: int  = 0,
):
    """Insert or replace a non-default named survey for a guild."""
    import json
    if questions is None:
        questions = []
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_extra_surveys "
            "(guild_id, survey_id, survey_name, tab_squad_powers, tab_history, "
            " questions, intro_message, survey_channel_id, notify_channel_id, "
            " reminder_message, reminder_enabled) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, survey_id) DO UPDATE SET "
            " survey_name=excluded.survey_name, "
            " tab_squad_powers=excluded.tab_squad_powers, "
            " tab_history=excluded.tab_history, "
            " questions=excluded.questions, "
            " intro_message=excluded.intro_message, "
            " survey_channel_id=excluded.survey_channel_id, "
            " notify_channel_id=excluded.notify_channel_id, "
            " reminder_message=excluded.reminder_message, "
            " reminder_enabled=excluded.reminder_enabled",
            (guild_id, survey_id, survey_name, tab_squad_powers, tab_history,
             json.dumps(questions), intro_message, survey_channel_id, notify_channel_id,
             reminder_message, reminder_enabled),
        )
        conn.commit()


def delete_extra_survey(guild_id: int, survey_id: str) -> bool:
    """Remove a non-default named survey. Returns True if a row was removed."""
    if survey_id == "default":
        return False  # the default survey lives in guild_survey_config
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM guild_extra_surveys WHERE guild_id = ? AND survey_id = ?",
            (guild_id, survey_id),
        )
        conn.commit()
    return cur.rowcount > 0


def _seed_ogv_birthday_config(conn):
    """Seed OGV's birthday config if not already present."""
    existing = conn.execute(
        "SELECT guild_id FROM guild_birthday_config WHERE guild_id = ?",
        (OGV_GUILD_ID,)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO guild_birthday_config "
            "(guild_id, tab_name, name_col, birthday_col, discord_id_col, data_start_row, "
            "enabled, train_integration, flexible_placement, lookahead_days, "
            "reminders_enabled, reminder_channel_id, reminder_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (OGV_GUILD_ID, "Season 5 - Off-Season", 4, 8, -1, 10, 1, 1, 1, 14, 0, 0, "22:00")
        )
        conn.commit()
        print("[CONFIG] Seeded OGV birthday config")


def get_birthday_config(guild_id: int) -> dict:
    """Return birthday config for a guild."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_birthday_config WHERE guild_id = ?",
            (guild_id,)
        ).fetchone()
    if row:
        return dict(row)
    return {
        "guild_id":            guild_id,
        "tab_name":            "Birthdays",
        "name_col":            0,
        "birthday_col":        1,
        "discord_id_col":      -1,
        "data_start_row":      2,
        "enabled":             0,
        "train_integration":   0,
        "flexible_placement":  1,
        "lookahead_days":      14,
        "reminders_enabled":   0,
        "reminder_channel_id": 0,
        "reminder_time":       "08:00",
    }


def save_birthday_config(guild_id: int, tab_name: str, name_col: int,
                         birthday_col: int, discord_id_col: int = -1,
                         data_start_row: int = 2, enabled: int = 1,
                         train_integration: int = 0, flexible_placement: int = 1,
                         lookahead_days: int = 14, reminders_enabled: int = 0,
                         reminder_channel_id: int = 0, reminder_time: str = "08:00"):
    """Insert or replace a guild's birthday config."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_birthday_config "
            "(guild_id, tab_name, name_col, birthday_col, discord_id_col, data_start_row, "
            "enabled, train_integration, flexible_placement, lookahead_days, "
            "reminders_enabled, reminder_channel_id, reminder_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "tab_name=excluded.tab_name, name_col=excluded.name_col, "
            "birthday_col=excluded.birthday_col, discord_id_col=excluded.discord_id_col, "
            "data_start_row=excluded.data_start_row, enabled=excluded.enabled, "
            "train_integration=excluded.train_integration, "
            "flexible_placement=excluded.flexible_placement, "
            "lookahead_days=excluded.lookahead_days, "
            "reminders_enabled=excluded.reminders_enabled, "
            "reminder_channel_id=excluded.reminder_channel_id, "
            "reminder_time=excluded.reminder_time",
            (guild_id, tab_name, name_col, birthday_col, discord_id_col, data_start_row,
             enabled, train_integration, flexible_placement, lookahead_days,
             reminders_enabled, reminder_channel_id, reminder_time)
        )
        conn.commit()


def get_member_roster_config(guild_id: int) -> dict:
    """Return member-roster config for a guild, with sensible defaults."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_member_roster_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    if row:
        return dict(row)
    return {
        "guild_id":       guild_id,
        "enabled":        0,
        "tab_name":       "Member Roster",
        "discord_id_col": 0,
        "name_col":       1,
        "display_col":    2,
        "joined_col":     3,
        "roles_col":      4,
        "role_filter_id": 0,
        "auto_sync":      1,
        "last_synced_at": "",
    }


def save_member_roster_config(
    guild_id: int,
    *,
    enabled:        int = 1,
    tab_name:       str = "Member Roster",
    discord_id_col: int = 0,
    name_col:       int = 1,
    display_col:    int = 2,
    joined_col:     int = 3,
    roles_col:      int = 4,
    role_filter_id: int = 0,
    auto_sync:      int = 1,
    last_synced_at: str = "",
):
    """Insert or replace a guild's member-roster config."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_member_roster_config "
            "(guild_id, enabled, tab_name, discord_id_col, name_col, display_col, "
            " joined_col, roles_col, role_filter_id, auto_sync, last_synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            " enabled=excluded.enabled, tab_name=excluded.tab_name, "
            " discord_id_col=excluded.discord_id_col, name_col=excluded.name_col, "
            " display_col=excluded.display_col, joined_col=excluded.joined_col, "
            " roles_col=excluded.roles_col, role_filter_id=excluded.role_filter_id, "
            " auto_sync=excluded.auto_sync, last_synced_at=excluded.last_synced_at",
            (guild_id, enabled, tab_name, discord_id_col, name_col, display_col,
             joined_col, roles_col, role_filter_id, auto_sync, last_synced_at),
        )
        conn.commit()


def update_roster_last_synced(guild_id: int, timestamp_iso: str):
    """Lightweight helper for the sync command to update only last_synced_at."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE guild_member_roster_config SET last_synced_at = ? WHERE guild_id = ?",
            (timestamp_iso, guild_id),
        )
        conn.commit()


def lookup_discord_id_for_name(guild_id: int, name: str) -> str | None:
    """
    Read the configured roster sheet and return the Discord ID for the given
    member name (case-insensitive match against display_col), or None.
    Used by DM-driven premium features.
    """
    cfg = get_member_roster_config(guild_id)
    if not cfg.get("enabled"):
        return None
    try:
        ws = get_member_roster_sheet(guild_id, cfg["tab_name"])
        rows = ws.get_all_values()
    except Exception as e:
        print(f"[ROSTER] Could not read roster sheet: {e}")
        return None

    target = name.strip().lower()
    did_col   = cfg["discord_id_col"]
    disp_col  = cfg["display_col"]
    name_col  = cfg["name_col"]
    for row in rows[1:]:  # skip header
        if not row:
            continue
        for col in (disp_col, name_col):
            if col < len(row) and row[col].strip().lower() == target:
                if did_col < len(row):
                    did = row[did_col].strip()
                    return did or None
                return None
    return None


def get_member_roster_sheet(guild_id: int, tab_name: str):
    """Open (or create) the roster tab in the guild's spreadsheet."""
    import os, json
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if credentials_json:
        info  = json.loads(credentials_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        key_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
        creds    = Credentials.from_service_account_file(key_file, scopes=scopes)

    gc = gspread.authorize(creds)
    sh = gc.open_by_key(get_spreadsheet_id(guild_id))
    try:
        return sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        return sh.add_worksheet(title=tab_name, rows=200, cols=10)


def _seed_ogv_train_config(conn):
    """Seed OGV's train config if not already present."""
    existing = conn.execute(
        "SELECT guild_id FROM guild_train_config WHERE guild_id = ?",
        (OGV_GUILD_ID,)
    ).fetchone()
    if not existing:
        import json
        conn.execute(
            "INSERT INTO guild_train_config "
            "(guild_id, tab_name, blurbs_enabled, themes, tones, prompt_template, default_tone, "
            "reminders_enabled, reminder_channel_id, reminder_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                OGV_GUILD_ID,
                "Train Schedule",
                1,
                json.dumps(OGV_DEFAULT_THEMES),
                json.dumps(OGV_DEFAULT_TONES),
                OGV_DEFAULT_PROMPT,
                "Default (match the theme)",
                1,
                1488693874938482799,  # OGV leadership channel
                "22:00",
            )
        )
        conn.commit()
        print(f"[CONFIG] Seeded OGV train config")


def _normalize_train_templates(d: dict) -> dict:
    """
    Lift a guild's train templates into the new shape:
      d["templates"] is a list[dict{name, template}], with at least one entry.
      d["default_template"] names which one is the default.

    Migration: when templates_json is empty/null but the legacy `prompt_template`
    column has content, treat the legacy template as the "Default" entry.
    """
    import json
    raw = d.get("templates_json") or "[]"
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = []
    if not isinstance(parsed, list):
        parsed = []
    # Legacy lift: if list is empty but legacy column has content, hoist it.
    if not parsed:
        legacy = (d.get("prompt_template") or "").strip()
        if legacy:
            parsed = [{"name": "Default", "template": legacy}]
    # Always guarantee at least one entry so downstream code can rely on it.
    if not parsed:
        parsed = [{"name": "Default", "template": OGV_DEFAULT_PROMPT}]
    d["templates"] = parsed
    # Pin default_template to a name that actually exists in the list.
    known_names = {t.get("name") for t in parsed}
    if not d.get("default_template") or d["default_template"] not in known_names:
        d["default_template"] = parsed[0]["name"]
    # Back-compat field for any caller still reading .prompt_template:
    d["prompt_template"] = parsed[0].get("template", "")
    return d


def get_train_config(guild_id: int) -> dict:
    """Return the train config for a guild, falling back to OGV defaults."""
    import json
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_train_config WHERE guild_id = ?",
            (guild_id,)
        ).fetchone()
    if row:
        d = dict(row)
        try:
            d["themes"] = json.loads(d["themes"]) if d["themes"] else OGV_DEFAULT_THEMES
            d["tones"]  = json.loads(d["tones"])  if d["tones"]  else OGV_DEFAULT_TONES
        except (json.JSONDecodeError, TypeError):
            d["themes"] = OGV_DEFAULT_THEMES
            d["tones"]  = OGV_DEFAULT_TONES
        return _normalize_train_templates(d)
    fallback = {
        "guild_id":            guild_id,
        "tab_name":            "Train Schedule",
        "blurbs_enabled":      1,
        "themes":              OGV_DEFAULT_THEMES,
        "tones":               OGV_DEFAULT_TONES,
        "prompt_template":     OGV_DEFAULT_PROMPT,
        "templates_json":      "",
        "default_template":    "Default",
        "default_tone":        "Default (match the theme)",
        "reminders_enabled":   1,
        "reminder_channel_id": 0,
        "reminder_time":       "22:00",
    }
    return _normalize_train_templates(fallback)


def save_train_config(guild_id: int, tab_name: str, themes: list,
                      tones: list, prompt_template: str, default_tone: str,
                      blurbs_enabled: int = 1, reminders_enabled: int = 1,
                      reminder_channel_id: int = 0, reminder_time: str = "22:00",
                      templates: list | None = None, default_template: str = "Default"):
    """
    Insert or replace a guild's train config.

    For backwards compatibility, callers may still pass `prompt_template` (a
    single string). When `templates` is None, that string is automatically
    wrapped as a single-entry list named "Default". Premium callers can pass
    `templates=[{name, template}, ...]` directly.
    """
    import json
    if templates is None:
        templates = [{"name": "Default", "template": prompt_template or ""}]
    templates_json = json.dumps(templates)
    # Keep prompt_template column populated with the default template's text
    # so older read paths still work during the migration window.
    default_text = next(
        (t["template"] for t in templates if t.get("name") == default_template),
        templates[0]["template"] if templates else "",
    )
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_train_config "
            "(guild_id, tab_name, blurbs_enabled, themes, tones, prompt_template, "
            " templates_json, default_template, default_tone, "
            " reminders_enabled, reminder_channel_id, reminder_time) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "tab_name=excluded.tab_name, blurbs_enabled=excluded.blurbs_enabled, "
            "themes=excluded.themes, tones=excluded.tones, "
            "prompt_template=excluded.prompt_template, "
            "templates_json=excluded.templates_json, "
            "default_template=excluded.default_template, "
            "default_tone=excluded.default_tone, "
            "reminders_enabled=excluded.reminders_enabled, "
            "reminder_channel_id=excluded.reminder_channel_id, "
            "reminder_time=excluded.reminder_time",
            (guild_id, tab_name, blurbs_enabled, json.dumps(themes), json.dumps(tones),
             default_text, templates_json, default_template, default_tone,
             reminders_enabled, reminder_channel_id, reminder_time)
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
