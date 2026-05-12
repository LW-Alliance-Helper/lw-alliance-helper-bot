"""
config.py — Per-guild configuration for LW Alliance Helper

All server-specific values live here. Guild configs are stored in a SQLite
database (guild_configs.db) so each server that installs the bot can have
its own settings.

For a new server, run /setup in Discord to configure the bot. Every guild
goes through the same flow — there is no special-cased seeding for any
particular alliance.
"""

import json
import os
import sqlite3
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional

from defaults import (
    DEFAULT_THEMES,
    DEFAULT_TONES,
    DEFAULT_PROMPT,
    DEFAULT_SURVEY_QUESTIONS,
    DEFAULT_SURVEY_INTRO,
    DEFAULT_DS_TEMPLATE,
    DEFAULT_CS_TEMPLATE,
)

DB_PATH       = os.getenv("CONFIG_DB_PATH",    "/app/data/guild_configs.db")


# ── GuildConfig dataclass ──────────────────────────────────────────────────────

@dataclass
class GuildConfig:
    guild_id:                 int
    leadership_channel_id:    int        = 0
    announcement_channel_id:  int        = 0
    member_role_id:           int        = 0
    member_role_name:         str        = "Member"
    leadership_role_id:       int        = 0
    leadership_role_name:     str        = "Leadership"
    survey_channel_id:        int        = 0
    survey_notify_channel_id: int        = 0
    ds_log_channel_id:        int        = 0
    cs_log_channel_id:        int        = 0
    event_draft_channel_id:   int        = 0
    event_announce_channel_id:int        = 0
    event_draft_time:         str        = "12:00"
    event_five_min_warning:   int        = 1
    spreadsheet_id:           str        = ""
    timezone:                 str        = "America/New_York"
    tab_train_schedule:       str        = "Train Schedule"
    tab_ds_assignments:       str        = "DS Assignments"
    tab_sitouts:              str        = "DS-CS Sit-outs"
    tab_survey_history:       str        = "Survey History"
    tab_member_default:       str        = "Season 5 - Off-Season"
    setup_complete:           bool       = False

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
    """Create the per-guild config tables if they don't exist and apply
    any pending schema migrations. No alliance is special-cased — every
    guild is created empty and populated through `/setup`.
    """
    # Ensure the data directory exists (Railway volume mount point)
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    with _get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_configs (
                guild_id                 INTEGER PRIMARY KEY,
                leadership_channel_id    INTEGER DEFAULT 0,
                announcement_channel_id  INTEGER DEFAULT 0,
                member_role_id           INTEGER DEFAULT 0,
                member_role_name         TEXT    DEFAULT 'Member',
                leadership_role_id       INTEGER DEFAULT 0,
                leadership_role_name     TEXT    DEFAULT 'Leadership',
                survey_channel_id        INTEGER DEFAULT 0,
                survey_notify_channel_id INTEGER DEFAULT 0,
                ds_log_channel_id          INTEGER DEFAULT 0,
                cs_log_channel_id          INTEGER DEFAULT 0,
                event_draft_channel_id     INTEGER DEFAULT 0,
                event_announce_channel_id  INTEGER DEFAULT 0,
                event_draft_time           TEXT    DEFAULT '12:00',
                event_five_min_warning     INTEGER DEFAULT 1,
                spreadsheet_id           TEXT    DEFAULT '',
                timezone                 TEXT    DEFAULT 'America/New_York',
                tab_train_schedule       TEXT    DEFAULT 'Train Schedule',
                tab_ds_assignments       TEXT    DEFAULT 'DS Assignments',
                tab_sitouts              TEXT    DEFAULT 'DS-CS Sit-outs',
                tab_survey_history       TEXT    DEFAULT 'Survey History',
                tab_member_default       TEXT    DEFAULT 'Season 5 - Off-Season',
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

        # guild_train_config — per-guild train schedule settings.
        # `dm_message` is the body of the Premium DM-to-assignee that fires
        # alongside the channel reminder; empty string means "use the
        # hardcoded default in train_cog.py". Supports `{name}` placeholder.
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
                reminder_time        TEXT    DEFAULT '22:00',
                dm_message           TEXT    DEFAULT ''
            )
        """)
        conn.commit()

        # guild_growth_config — per-guild growth tracking settings.
        # Breakdown columns (#34) classify members by % change between
        # snapshots. `breakdown_thresholds` / `breakdown_labels` are JSON
        # dicts (Premium override, empty `{}` = use hardcoded defaults).
        # `breakdown_post_channel_id` enables auto-post on snapshot (0 =
        # off; Premium-gated at call time). `breakdown_bucket_filter` is
        # a JSON list of bucket names included in the auto-post (empty `[]`
        # = no filter, post every bucket).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_growth_config (
                guild_id                   INTEGER PRIMARY KEY,
                enabled                    INTEGER DEFAULT 0,
                tab_source                 TEXT    DEFAULT '',
                name_col                   TEXT    DEFAULT 'A',
                metrics                    TEXT    DEFAULT '',
                tab_growth                 TEXT    DEFAULT 'Growth Tracking',
                snapshot_frequency         TEXT    DEFAULT 'monthly',
                snapshot_day               INTEGER DEFAULT 1,
                snapshot_interval          INTEGER DEFAULT 30,
                data_start_row             INTEGER DEFAULT 2,
                tab_breakdown              TEXT    DEFAULT 'Growth Breakdown',
                breakdown_thresholds       TEXT    DEFAULT '{}',
                breakdown_labels           TEXT    DEFAULT '{}',
                breakdown_post_channel_id  INTEGER DEFAULT 0,
                breakdown_bucket_filter    TEXT    DEFAULT '[]'
            )
        """)
        conn.commit()

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

        # guild_birthday_config — per-guild birthday settings.
        # `dm_message` is the body of the Premium birthday DM that fires
        # alongside the channel reminder; empty string means "use the
        # hardcoded default in train_cog.py". Supports `{name}` placeholder.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_birthday_config (
                guild_id                   INTEGER PRIMARY KEY,
                tab_name                   TEXT    DEFAULT 'Birthdays',
                name_col                   INTEGER DEFAULT 0,
                birthday_col               INTEGER DEFAULT 1,
                discord_id_col             INTEGER DEFAULT -1,
                data_start_row             INTEGER DEFAULT 2,
                enabled                    INTEGER DEFAULT 1,
                train_integration          INTEGER DEFAULT 0,
                flexible_placement         INTEGER DEFAULT 1,
                lookahead_days             INTEGER DEFAULT 14,
                reminders_enabled          INTEGER DEFAULT 0,
                reminder_channel_id        INTEGER DEFAULT 0,
                reminder_time              TEXT    DEFAULT '08:00',
                dm_message                 TEXT    DEFAULT '',
                last_train_population_date TEXT    DEFAULT ''
            )
        """)
        conn.commit()

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
        # `dm_reminder_message` is the body of the Premium DM that fires when
        # leadership runs `/desertstorm_remind` or `/canyonstorm_remind`;
        # empty string means "use the hardcoded default in storm_log.py".
        # Supports `{name}` placeholder.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_storm_config (
                guild_id             INTEGER NOT NULL,
                event_type           TEXT    NOT NULL,
                tab_name             TEXT    DEFAULT 'DS Assignments',
                mail_template        TEXT    DEFAULT '',
                templates_json       TEXT    DEFAULT '[]',
                default_template     TEXT    DEFAULT 'Default',
                timezone             TEXT    DEFAULT 'America/New_York',
                log_channel_id       INTEGER DEFAULT 0,
                post_channel_id      INTEGER DEFAULT 0,
                dm_reminder_message  TEXT    DEFAULT '',
                PRIMARY KEY (guild_id, event_type)
            )
        """)
        conn.commit()

        # premium_assignments — bot-side assignment layer for the User
        # Subscription SKU. Discord considers a subscriber's entitlement
        # valid in every guild they share with the bot; this table lets a
        # subscriber pin their one $4.99/mo to a single guild at a time
        # (MEE6 pattern). One row per subscriber: PRIMARY KEY on user_id
        # enforces one-assignment-per-user, UNIQUE on guild_id enforces
        # one-subscriber-per-guild. Rows persist across subscription
        # lapses so resubscribing auto-resumes Premium in the same guild.
        # See premium.py and issue #41 for the full model.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS premium_assignments (
                user_id     INTEGER PRIMARY KEY,
                guild_id    INTEGER NOT NULL UNIQUE,
                assigned_at TEXT    NOT NULL
            )
        """)

        # Operational metadata about which guilds the bot is installed in.
        # Lets support triage identify a guild from a logged `guild_id`
        # without a live `bot.get_guild` lookup. `installer_user_id` comes
        # from the audit log on join and is best-effort (the audit log only
        # retains 45 days); `owner_id` is the always-available fallback
        # contact path. Rows are deleted in `on_guild_remove` and on
        # `/admin_forget_guild` so kicked guilds aren't retained.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_install_metadata (
                guild_id          INTEGER PRIMARY KEY,
                guild_name        TEXT    NOT NULL DEFAULT '',
                owner_id          INTEGER NOT NULL DEFAULT 0,
                installer_user_id INTEGER,
                installed_at      TEXT    NOT NULL,
                last_seen_at      TEXT    NOT NULL
            )
        """)
        conn.commit()

        # guild_shiny_tasks_config — per-guild Daily Shiny Tasks settings.
        # Free for all tiers. `channel_id` is the destination, `post_time`
        # is HH:MM in the guild's `timezone` (mirrors birthday reminder
        # config), `server_min`/`server_max` define the alliance's
        # "transfer range" filter applied to `shiny_task_servers`, and
        # `message_template` is the customised announcement body (empty
        # string = use `DEFAULT_SHINY_TASKS_MESSAGE` from defaults.py).
        # `last_posted_date` is an ISO date string used by the scheduler
        # loop to prevent duplicate posts when Railway restarts across
        # the configured minute.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_shiny_tasks_config (
                guild_id          INTEGER PRIMARY KEY,
                enabled           INTEGER DEFAULT 0,
                channel_id        INTEGER DEFAULT 0,
                post_time         TEXT    DEFAULT '09:00',
                server_min        INTEGER DEFAULT 0,
                server_max        INTEGER DEFAULT 0,
                message_template  TEXT    DEFAULT '',
                last_posted_date  TEXT    DEFAULT ''
            )
        """)

        # shiny_task_servers — global table of every Last War server
        # known to cpt-hedge, refreshed weekly. The 3-day shiny-task
        # cycle is fully derivable from `creation_date` (no phase
        # column needed). `last_seen_at` is bumped on every refresh;
        # servers absent from the most recent fetch age out and are
        # filtered from queries (soft delete). See shiny_tasks.py and
        # docs/hedge_data_source.md.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shiny_task_servers (
                server_number INTEGER PRIMARY KEY,
                creation_date TEXT    NOT NULL,
                region        TEXT    DEFAULT '',
                last_seen_at  TEXT    NOT NULL
            )
        """)
        conn.commit()

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
            # ── Participation log (#20 rework) ───────────────────────────────────
            # Each (guild_id, event_type) row carries its own participation
            # config: enabled, the sheet tab to write rows to, the
            # JSON-encoded list of custom questions, and the roster source
            # for `roster_names`-typed questions (tab + name_col + optional
            # alias_col + start_row). The log-summary channel reuses the
            # existing `log_channel_id` column so we don't duplicate state.
            ("participation_enabled",         "INTEGER DEFAULT 0"),
            ("participation_tab_name",        "TEXT    DEFAULT ''"),
            ("participation_questions",       "TEXT    DEFAULT '[]'"),
            ("participation_roster_tab",      "TEXT    DEFAULT ''"),
            ("participation_roster_name_col", "INTEGER DEFAULT 0"),
            ("participation_roster_alias_col","INTEGER DEFAULT -1"),
            ("participation_roster_start_row","INTEGER DEFAULT 2"),
            # Premium /desertstorm_remind / /canyonstorm_remind DM body
            # (empty → hardcoded default in storm_log.py).
            ("dm_reminder_message",           "TEXT    DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE guild_storm_config ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to guild_storm_config")
            except Exception:
                pass

        # ── guild_extra_surveys migrations (per-survey reminder fields) ────────
        for col, definition in [
            ("reminder_message",      "TEXT DEFAULT ''"),
            ("reminder_enabled",      "INTEGER DEFAULT 0"),
            ("reminder_frequency",    "TEXT DEFAULT 'off'"),       # off | daily | weekly
            ("reminder_day_of_week",  "INTEGER DEFAULT 1"),        # 0=Mon..6=Sun (weekly only)
            ("reminder_time",         "TEXT DEFAULT '12:00'"),     # HH:MM 24h, in guild tz
            ("reminder_channel_id",   "INTEGER DEFAULT 0"),        # 0 = DM-via-roster (Premium)
            ("reminder_use_dm",       "INTEGER DEFAULT 0"),        # 1 = DM-via-roster (Premium)
            ("reminder_last_fired",   "TEXT DEFAULT ''"),          # ISO date of last fire
        ]:
            try:
                conn.execute(f"ALTER TABLE guild_extra_surveys ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to guild_extra_surveys")
            except Exception:
                pass

        # ── guild_survey_config migrations (default survey reminder fields) ────
        # Mirrors guild_extra_surveys so the default survey can have its own
        # scheduled reminder config too. Free guilds use channel posts;
        # Premium can opt into DM-via-roster.
        for col, definition in [
            ("reminder_message",      "TEXT DEFAULT ''"),
            ("reminder_enabled",      "INTEGER DEFAULT 0"),
            ("reminder_frequency",    "TEXT DEFAULT 'off'"),
            ("reminder_day_of_week",  "INTEGER DEFAULT 1"),
            ("reminder_time",         "TEXT DEFAULT '12:00'"),
            ("reminder_channel_id",   "INTEGER DEFAULT 0"),
            ("reminder_use_dm",       "INTEGER DEFAULT 0"),
            ("reminder_last_fired",   "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE guild_survey_config ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to guild_survey_config")
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
            # Premium DM-to-assignee body (empty → hardcoded default in train_cog.py)
            ("dm_message",          "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE guild_train_config ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to guild_train_config")
            except Exception:
                pass

        # ── guild_growth_config migrations (#34 Growth Breakdown) ──────────────
        for col, definition in [
            ("tab_breakdown",             "TEXT    DEFAULT 'Growth Breakdown'"),
            ("breakdown_thresholds",      "TEXT    DEFAULT '{}'"),
            ("breakdown_labels",          "TEXT    DEFAULT '{}'"),
            ("breakdown_post_channel_id", "INTEGER DEFAULT 0"),
            ("breakdown_bucket_filter",   "TEXT    DEFAULT '[]'"),
        ]:
            try:
                conn.execute(f"ALTER TABLE guild_growth_config ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to guild_growth_config")
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
            # Premium birthday DM body (empty → hardcoded default in train_cog.py)
            ("dm_message",          "TEXT DEFAULT ''"),
            # SQLite-backed dedup for the 22:00 ET train-population fire.
            # ISO date of the last successful auto-pop. Survives bot
            # restarts and discord.py reconnects, which the prior
            # in-memory `birthday_population_fired` set on the cog did
            # not (Railway redeploys at 22:00 were re-firing the
            # conflict alerts). See #89.
            ("last_train_population_date", "TEXT DEFAULT ''"),
        ]:
            try:
                conn.execute(f"ALTER TABLE guild_birthday_config ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to guild_birthday_config")
            except Exception:
                pass

        # ── guild_configs event/survey channel migrations ──────────────────────
        # Note: event_draft_channel_id, event_announce_channel_id,
        # event_draft_time, event_five_min_warning are also added by an
        # earlier silent-swallow block above; covered here so logging
        # picks up upgrades from the very oldest schema versions.
        # `leadership_role_id` (#95) lets the setup wizard's "Keep current"
        # button survive a role rename — old guilds only had the name.
        for col, definition in [
            ("survey_channel_id",         "INTEGER DEFAULT 0"),
            ("survey_notify_channel_id",  "INTEGER DEFAULT 0"),
            ("ds_log_channel_id",         "INTEGER DEFAULT 0"),
            ("cs_log_channel_id",         "INTEGER DEFAULT 0"),
            ("timezone",                  "TEXT DEFAULT 'America/New_York'"),
            ("leadership_role_id",        "INTEGER DEFAULT 0"),
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

        # ── 1.1.0: drop leadership_category_id ─────────────────────────────────
        # Channel/category gating was removed in favour of role-only gating.
        # The column is dead; drop it so GuildConfig(**dict(row)) doesn't
        # TypeError on rows that still carry it. SQLite 3.35+ (Railway prod
        # confirmed) supports DROP COLUMN.
        try:
            conn.execute("ALTER TABLE guild_configs DROP COLUMN leadership_category_id")
            conn.commit()
            print("[CONFIG] Dropped leadership_category_id from guild_configs")
        except Exception:
            pass  # Column already absent — expected on fresh databases

        # ── 1.1.3: drop storm time-slot columns entirely ───────────────────────
        # Storm time slots are game-defined constants (DS: 18:00 + 23:00,
        # CS: 12:00 + 23:00 server time, UTC-2, no DST). Local rendering is
        # computed at display time from the guild's `timezone`. Nothing
        # about the slot needs to be stored per-guild — drop the lot.
        for col in (
            "time_option_1_label",
            "time_option_1_local",
            "time_option_1_server",
            "time_option_2_label",
            "time_option_2_local",
            "time_option_2_server",
        ):
            try:
                conn.execute(f"ALTER TABLE guild_storm_config DROP COLUMN {col}")
                conn.commit()
                print(f"[CONFIG] Dropped {col} from guild_storm_config")
            except Exception:
                pass

        # ── 1.1.4: backfill numeric+magnitude on default LW survey keys ────────
        # The pre-rework hardcoded survey normalised shorthand like `301` to
        # 301,000,000 before writing to Sheets. The configurable-survey rework
        # dropped that, leaving stored values inconsistent with prior data and
        # hard for sheet-side sums to interpret. Numeric is now a free-tier
        # type and ships with a magnitude scaler. Backfill any saved guild
        # config whose questions still carry the original LW default keys so
        # leadership doesn't have to re-run /setup_survey by hand. Idempotent:
        # only upgrades type=text questions that haven't been migrated.
        _LW_DEFAULT_MAGNITUDES = {
            "squad1_power":  "M",
            "squad2_power":  "M",
            "squad3_power":  "M",
            "thp":           "M",
            "total_kills":   "M",
            "drone_level":   "raw",
            "gorilla_level": "raw",
        }
        for _table in ("guild_survey_config", "guild_extra_surveys"):
            try:
                _rows = conn.execute(
                    f"SELECT rowid, questions FROM {_table}"
                ).fetchall()
            except Exception:
                continue
            for _rowid, _raw in _rows:
                if not _raw:
                    continue
                try:
                    _qs = json.loads(_raw)
                except Exception:
                    continue
                if not isinstance(_qs, list):
                    continue
                _changed = False
                for _q in _qs:
                    if not isinstance(_q, dict):
                        continue
                    _mag = _LW_DEFAULT_MAGNITUDES.get(_q.get("key"))
                    if _mag is None:
                        continue
                    if _q.get("type") == "text" and "magnitude" not in _q:
                        _q["type"]      = "numeric"
                        _q["magnitude"] = _mag
                        _changed = True
                if _changed:
                    try:
                        conn.execute(
                            f"UPDATE {_table} SET questions = ? WHERE rowid = ?",
                            (json.dumps(_qs), _rowid),
                        )
                        conn.commit()
                        print(f"[CONFIG] Upgraded LW default questions to numeric+magnitude "
                              f"in {_table} rowid={_rowid}")
                    except Exception as _e:
                        print(f"[CONFIG] Could not write back {_table} rowid={_rowid}: {_e}")


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


def get_member_tab(guild_id: int) -> str:
    """Get the active member tab name for a guild."""
    cfg = get_config(guild_id)
    return cfg.tab_member_default if cfg else "Season 5 - Off-Season"


def get_spreadsheet_id(guild_id: int) -> str:
    """Get the Google Sheet ID for a guild from the config database."""
    cfg = get_config(guild_id)
    return cfg.spreadsheet_id if cfg and cfg.spreadsheet_id else ""


def get_spreadsheet(guild_id: int = None):
    """Return an authenticated gspread Spreadsheet for a guild.

    Reads creds from `GOOGLE_CREDENTIALS_JSON` (Railway-style env var) or
    falls back to the path in `GOOGLE_SERVICE_ACCOUNT_FILE`. Centralised
    so storm/storm_log/survey/growth all use one bootstrap.
    """
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

    gc       = gspread.authorize(creds)
    sheet_id = get_spreadsheet_id(guild_id)
    return gc.open_by_key(sheet_id)


def describe_sheet_error(e: Exception, *,
                         guild_id=None, tab: str = None) -> str:
    """Render a gspread exception as a one-line diagnostic for logging.

    Distinguishes worksheet-tab-missing from spreadsheet 404 / 403 / 429,
    so the log line answers 'what should I check?' without the original
    exception. Falls back to `type(e).__name__: e` for non-gspread errors.
    """
    import gspread

    parts = []
    if guild_id is not None:
        parts.append(f"guild={guild_id}")
    suffix = f" ({', '.join(parts)})" if parts else ""

    if isinstance(e, gspread.exceptions.WorksheetNotFound):
        # gspread sets str(e) = the missing tab name; prefer that since
        # it's authoritative, with the caller-supplied tab as a fallback
        # for cases where str(e) is empty.
        wanted = str(e) or tab or "?"
        return (
            f"worksheet tab '{wanted}' not found in spreadsheet — "
            f"check the tab name in /setup or rename the tab to match"
            f"{suffix}"
        )

    if isinstance(e, gspread.exceptions.SpreadsheetNotFound):
        return (
            f"spreadsheet not found — was it deleted, or is the ID wrong "
            f"in /setup?{suffix}"
        )

    if isinstance(e, gspread.exceptions.APIError):
        status = None
        reason = None
        resp = getattr(e, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)
            try:
                reason = resp.json().get("error", {}).get("message")
            except Exception:
                reason = None
        status = status or getattr(e, "code", None)
        if status == 404:
            return (
                f"spreadsheet 404 — deleted or inaccessible to the bot's "
                f"service account{suffix}"
            )
        if status == 403:
            return (
                f"spreadsheet 403 — share it with the bot's service account "
                f"as Editor{suffix}"
            )
        if status == 429:
            return f"sheets API rate-limited (429){suffix}"
        if status:
            msg = f": {reason}" if reason else ""
            return f"sheets API error HTTP {status}{msg}{suffix}"
        return f"sheets API error{suffix}: {e!r}"

    return f"{type(e).__name__}: {e}{suffix}"


def is_setup_complete(guild_id: int) -> bool:
    """Check if a guild has completed setup."""
    cfg = get_config(guild_id)
    return cfg.setup_complete if cfg else False


# ── Premium assignment helpers ─────────────────────────────────────────────────
#
# The bot's Premium SKU is User Subscription — Discord considers an entitlement
# valid in every guild the subscriber shares with the bot. The
# `premium_assignments` table pins each subscriber's one license to a single
# guild they choose. See issue #41 and `premium.is_premium` for how this layer
# is consulted on every premium check.

def get_premium_assignment_for_guild(guild_id: int) -> Optional[int]:
    """Return the user_id assigned to this guild, or None if no assignment."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT user_id FROM premium_assignments WHERE guild_id = ?", (guild_id,)
        ).fetchone()
        return row["user_id"] if row else None


def get_premium_assignment_for_user(user_id: int) -> Optional[int]:
    """Return the guild_id this user has assigned their license to, or None."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT guild_id FROM premium_assignments WHERE user_id = ?", (user_id,)
        ).fetchone()
        return row["guild_id"] if row else None


def set_premium_assignment(user_id: int, guild_id: int) -> Optional[int]:
    """Assign or move this user's license to `guild_id`.

    Returns the user_id of any prior assignment that was cleared from
    `guild_id` (a different subscriber being displaced should never happen
    because callers reject duplicates first, but this still surfaces the
    fact so the caller can invalidate caches correctly). The previous
    guild this user was assigned to (if any) is replaced atomically;
    callers should invalidate the premium cache for both the old and new
    guild_ids.
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        # Check if another user already holds this guild — unique on
        # guild_id would otherwise raise. Callers should have rejected
        # this case before calling, but defend against a race.
        prior_row = conn.execute(
            "SELECT user_id FROM premium_assignments WHERE guild_id = ? AND user_id != ?",
            (guild_id, user_id),
        ).fetchone()
        prior_user = prior_row["user_id"] if prior_row else None
        if prior_user is not None:
            conn.execute("DELETE FROM premium_assignments WHERE user_id = ?", (prior_user,))

        conn.execute(
            "INSERT INTO premium_assignments (user_id, guild_id, assigned_at) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET guild_id = excluded.guild_id, "
            "assigned_at = excluded.assigned_at",
            (user_id, guild_id, now),
        )
        conn.commit()
        return prior_user


def remove_premium_assignment(user_id: int) -> Optional[int]:
    """Remove this user's assignment. Returns the guild_id that was freed,
    or None if there was no assignment. Caller should invalidate the
    premium cache for the returned guild_id."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT guild_id FROM premium_assignments WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None
        guild_id = row["guild_id"]
        conn.execute("DELETE FROM premium_assignments WHERE user_id = ?", (user_id,))
        conn.commit()
        return guild_id


# ── Guild install metadata helpers ─────────────────────────────────────────────
#
# Small operational record per guild used for support triage (matching a
# logged `guild_id` to an alliance name without a live Discord lookup).
# See `guild_install_metadata` schema in `init_db` for the column list.

def upsert_guild_install_metadata(
    guild_id: int,
    guild_name: str,
    owner_id: int,
    installer_user_id: Optional[int] = None,
) -> None:
    """Insert or update the metadata row for a guild.

    First sighting: writes all fields and stamps both `installed_at` and
    `last_seen_at` to now. Subsequent sightings: refreshes `guild_name`,
    `owner_id`, `last_seen_at`; preserves `installed_at` and only fills
    `installer_user_id` if it's still NULL (audit-log lookups can fail on
    later boots even when they succeeded once).
    """
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        existing = conn.execute(
            "SELECT installer_user_id FROM guild_install_metadata WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        if existing is None:
            conn.execute(
                """INSERT INTO guild_install_metadata
                   (guild_id, guild_name, owner_id, installer_user_id, installed_at, last_seen_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (guild_id, guild_name, owner_id, installer_user_id, now, now),
            )
        else:
            prev_installer = existing["installer_user_id"]
            new_installer = prev_installer if prev_installer is not None else installer_user_id
            conn.execute(
                """UPDATE guild_install_metadata
                   SET guild_name = ?, owner_id = ?, installer_user_id = ?, last_seen_at = ?
                   WHERE guild_id = ?""",
                (guild_name, owner_id, new_installer, now, guild_id),
            )
        conn.commit()


def get_guild_install_metadata(guild_id: int) -> Optional[dict]:
    """Return the metadata row as a dict, or None if absent."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_install_metadata WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def delete_guild_install_metadata(guild_id: int) -> bool:
    """Delete the metadata row. Returns True if a row was deleted, False
    if no row existed for that guild_id.
    """
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM guild_install_metadata WHERE guild_id = ?",
            (guild_id,),
        )
        conn.commit()
        return cur.rowcount > 0


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


# ── Storm event fixed Server Time constants ───────────────────────────────────
# Last War's Desert Storm and Canyon Storm both run at fixed server times
# defined by the game (UTC-2, no DST). They never change per-alliance, so
# we hardcode them here and compute the local rendering at display time
# from each guild's `timezone`.

DS_SERVER_TIMES = [(18, 0), (23, 0)]
CS_SERVER_TIMES = [(12, 0), (23, 0)]

SERVER_TZ_OFFSET = -2  # Server Time is UTC-2.


def server_time_to_local(hour: int, minute: int, guild_id: int) -> str:
    """Convert a Server Time (UTC-2) hour/minute to the guild's local clock.

    Returns a `4pm EDT` / `9:30am EDT` style string (lowercase am/pm to
    match the rest of the bot's user-facing copy). Falls back to a bare
    `HH:MM Server Time` if the timezone lookup fails for any reason.
    """
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone as _tz, timedelta
    cfg    = get_config(guild_id) if guild_id else None
    tz_str = cfg.timezone if cfg and cfg.timezone else "America/New_York"
    try:
        server_tz = _tz(timedelta(hours=SERVER_TZ_OFFSET))
        # Use a stable date so DST behaves consistently for the rendered string.
        server_dt = datetime(2026, 6, 1, hour, minute, tzinfo=server_tz)
        local_dt  = server_dt.astimezone(ZoneInfo(tz_str))
        h12       = local_dt.hour % 12 or 12
        period    = "am" if local_dt.hour < 12 else "pm"
        tz_abbr   = local_dt.strftime("%Z")
        mins      = f":{local_dt.minute:02d}" if local_dt.minute != 0 else ""
        return f"{h12}{mins}{period} {tz_abbr}"
    except Exception:
        return f"{hour:02d}:{minute:02d} server time"


def format_storm_slot(hour: int, minute: int, guild_id: int) -> str:
    """Compose the canonical storm-slot label.

    Returns `<local> (HH:MM server time)` — the format used on every
    user-facing surface (TimeSelectView buttons, /view_configuration,
    storm overview embeds, mail `{time}` placeholder).
    """
    return f"{server_time_to_local(hour, minute, guild_id)} ({hour:02d}:{minute:02d} server time)"


def get_storm_slot_labels(event_type: str, guild_id: int) -> list[str]:
    """Return the two slot labels for DS or CS in display order.

    Each label is `<local> (HH:MM server time)` for the corresponding
    game-defined slot. Used by TimeSelectView so button labels and the
    rendered mail's `{time}` placeholder both flow from one source.
    """
    times = DS_SERVER_TIMES if event_type == "DS" else CS_SERVER_TIMES
    return [format_storm_slot(h, m, guild_id) for h, m in times]


def get_storm_slot_for_key(event_type: str, time_key: str) -> tuple[int, int] | None:
    """Resolve a TimeSelectView selection (`"1"` / `"2"`) to (hour, minute).

    Returns None if the key isn't recognized — callers should fall back to
    the raw string in that case (test helpers pass arbitrary text here).
    """
    times = DS_SERVER_TIMES if event_type == "DS" else CS_SERVER_TIMES
    if time_key == "1":
        return times[0]
    if time_key == "2":
        return times[1]
    return None


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
            "template": DEFAULT_DS_TEMPLATE if event_type == "DS" else DEFAULT_CS_TEMPLATE,
        }]
    d["templates"] = parsed
    known_names = {t.get("name") for t in parsed}
    if not d.get("default_template") or d["default_template"] not in known_names:
        d["default_template"] = parsed[0]["name"]
    d["mail_template"] = parsed[0].get("template", "")  # back-compat
    return d


def has_storm_config(guild_id: int, event_type: str) -> bool:
    """True iff the guild has a row in `guild_storm_config` for this
    event_type — i.e. they have run `/setup_desertstorm` or
    `/setup_canyonstorm` at least once. The fallback dict from
    `get_storm_config` doesn't distinguish "saved with all defaults"
    from "never configured"; this helper exists for the setup-wizard
    summary embed gate (#103)."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM guild_storm_config "
            "WHERE guild_id = ? AND event_type = ?",
            (guild_id, event_type),
        ).fetchone()
    return row is not None


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
        "mail_template":        DEFAULT_DS_TEMPLATE if event_type == "DS" else DEFAULT_CS_TEMPLATE,
        "templates_json":       "",
        "default_template":     "Default",
        "timezone":             "America/New_York",
        "post_channel_id":      0,
        "dm_reminder_message":  "",
    }
    return _normalize_storm_templates(fallback, event_type)


def save_storm_config(guild_id: int, event_type: str, tab_name: str,
                      mail_template: str,
                      timezone: str, log_channel_id: int = 0,
                      templates: list | None = None,
                      default_template: str = "Default",
                      post_channel_id: int = 0,
                      dm_reminder_message: str = ""):
    """
    Insert or replace a guild's storm config.

    Backwards-compatible: callers may still pass `mail_template` as a single
    string. When `templates` is None, the string is wrapped as a single
    "Default" entry. Premium callers may pass a list of named templates.
    `post_channel_id` is the channel where /[event]_draft will post the
    final mail when leadership clicks "Post & Copy".

    Storm time slots are game-defined constants (see DS_SERVER_TIMES /
    CS_SERVER_TIMES below) and aren't stored per-guild — only the
    `timezone` is used at display time to render local clock equivalents.

    `dm_reminder_message` is the body of the Premium DM that fires when
    leadership runs `/desertstorm_remind` or `/canyonstorm_remind`. Empty
    string means "use the hardcoded default in storm_log.py". Supports the
    `{name}` placeholder.
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
            "timezone, log_channel_id, post_channel_id, dm_reminder_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, event_type) DO UPDATE SET "
            "tab_name=excluded.tab_name, mail_template=excluded.mail_template, "
            "templates_json=excluded.templates_json, "
            "default_template=excluded.default_template, "
            "timezone=excluded.timezone, "
            "log_channel_id=excluded.log_channel_id, "
            "post_channel_id=excluded.post_channel_id, "
            "dm_reminder_message=excluded.dm_reminder_message",
            (guild_id, event_type, tab_name, default_text, templates_json, default_template,
             timezone, log_channel_id, post_channel_id, dm_reminder_message)
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


# ── Participation log config (#20) ────────────────────────────────────────────

def get_participation_config(guild_id: int, event_type: str) -> dict:
    """
    Return the participation-log config for a guild + event type. The
    log-summary channel is read from the existing `log_channel_id`
    column on guild_storm_config (shared with the legacy participation
    flow); the `participation_*` columns hold the new-flow-specific
    bits — questions, sheet tab, roster source.
    """
    import json
    cfg = get_storm_config(guild_id, event_type)
    raw_qs = cfg.get("participation_questions") or "[]"
    try:
        questions = json.loads(raw_qs) if raw_qs else []
    except (json.JSONDecodeError, TypeError):
        questions = []
    return {
        "enabled":          bool(cfg.get("participation_enabled")),
        "log_channel_id":   int(cfg.get("log_channel_id") or 0),
        "tab_name":         cfg.get("participation_tab_name") or "",
        "questions":        questions,
        "roster_tab":       cfg.get("participation_roster_tab") or "",
        "roster_name_col":  int(cfg.get("participation_roster_name_col") or 0),
        "roster_alias_col": int(cfg.get("participation_roster_alias_col")
                                if cfg.get("participation_roster_alias_col") is not None else -1),
        "roster_start_row": int(cfg.get("participation_roster_start_row") or 2),
    }


def save_participation_config(
    guild_id: int, event_type: str, *,
    enabled: int           = 0,
    tab_name: str          = "",
    questions: list        = None,
    roster_tab: str        = "",
    roster_name_col: int   = 0,
    roster_alias_col: int  = -1,
    roster_start_row: int  = 2,
):
    """
    Persist the participation-log config to the (guild_id, event_type) row.
    The row must already exist (created by /setup_desertstorm or
    /setup_canyonstorm via save_storm_config); this UPDATE does not insert.
    The log-summary channel lives on `log_channel_id` and is set by the
    main storm-setup save call, so it isn't a parameter here.
    """
    import json
    if questions is None:
        questions = []
    questions_json = json.dumps(questions)
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE guild_storm_config SET "
            "  participation_enabled = ?, "
            "  participation_tab_name = ?, "
            "  participation_questions = ?, "
            "  participation_roster_tab = ?, "
            "  participation_roster_name_col = ?, "
            "  participation_roster_alias_col = ?, "
            "  participation_roster_start_row = ? "
            "WHERE guild_id = ? AND event_type = ?",
            (
                1 if enabled else 0, tab_name,
                questions_json, roster_tab, roster_name_col,
                roster_alias_col, roster_start_row,
                guild_id, event_type,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


# Survey defaults moved to defaults.py — DEFAULT_SURVEY_QUESTIONS and
# DEFAULT_SURVEY_INTRO are imported at the top of this module.


def has_growth_config(guild_id: int) -> bool:
    """True iff the guild has a row in `guild_growth_config` — i.e. they
    have run `/setup_growth` at least once. `get_growth_config` returns
    a fallback dict on miss, so it can't distinguish "saved with all
    defaults" from "never configured"; this helper exists for the
    setup-wizard summary embed and the disable-with-clear gate (#99)."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM guild_growth_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return row is not None


def clear_growth_config(guild_id: int) -> None:
    """Delete the guild's growth config row entirely. Called by the
    `ask_disable_with_clear` Clear button after the user disables growth
    tracking. Wipes both the snapshot config and the breakdown config
    (which lives on the same row) — breakdown isn't functional without
    growth metrics anyway, and `/setup_growth_breakdown` blocks when
    growth is disabled or has no metrics."""
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM guild_growth_config WHERE guild_id = ?",
            (guild_id,),
        )
        conn.commit()


def get_growth_config(guild_id: int) -> dict:
    """Return growth config for a guild. Breakdown JSON fields
    (`breakdown_thresholds`, `breakdown_labels`, `breakdown_bucket_filter`)
    are parsed back into dicts/lists; empty / malformed JSON falls back to
    empty defaults so callers don't need to handle parse errors."""
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
        for json_field, fallback in (
            ("breakdown_thresholds", {}),
            ("breakdown_labels",     {}),
            ("breakdown_bucket_filter", []),
        ):
            raw = d.get(json_field)
            try:
                d[json_field] = json.loads(raw) if raw else fallback
            except (json.JSONDecodeError, TypeError):
                d[json_field] = fallback
        return d
    return {
        "guild_id":                  guild_id,
        "enabled":                   0,
        "tab_source":                "",
        "name_col":                  "A",
        "metrics":                   [],
        "tab_growth":                "Growth Tracking",
        "snapshot_frequency":        "monthly",
        "snapshot_day":              1,
        "snapshot_interval":         30,
        "data_start_row":            2,
        "tab_breakdown":             "Growth Breakdown",
        "breakdown_thresholds":      {},
        "breakdown_labels":          {},
        "breakdown_post_channel_id": 0,
        "breakdown_bucket_filter":   [],
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


def has_growth_breakdown_config(guild_id: int) -> bool:
    """True iff the guild has saved any non-default breakdown field —
    i.e. they have walked `/setup_growth_breakdown` at least once and
    changed something. Breakdown shares a row with growth, so the row
    existing isn't a useful signal on its own; instead this checks the
    breakdown-specific fields (post channel, thresholds, labels, bucket
    filter, custom tab name) for any non-default value. Used by the
    setup-wizard summary embed gate (#100)."""
    cfg = get_growth_config(guild_id)
    if (cfg.get("breakdown_post_channel_id") or 0) != 0:
        return True
    if cfg.get("breakdown_thresholds"):
        return True
    if cfg.get("breakdown_labels"):
        return True
    if cfg.get("breakdown_bucket_filter"):
        return True
    if cfg.get("tab_breakdown") not in ("", "Growth Breakdown"):
        return True
    return False


def save_growth_breakdown_config(
    guild_id: int, *,
    tab_breakdown: str             = "Growth Breakdown",
    breakdown_thresholds: dict     = None,
    breakdown_labels: dict         = None,
    breakdown_post_channel_id: int = 0,
    breakdown_bucket_filter: list  = None,
) -> bool:
    """Update the breakdown-specific fields on guild_growth_config without
    touching the core growth-snapshot fields. Returns True if a row was
    updated; False if the guild has no growth config yet (caller should
    run `/setup_growth` first).

    `breakdown_thresholds` is a dict like ``{"increased": 20, "steady": 10,
    "low": 5, "none": 0}`` (Decline is implicit at < 0%). Empty dict means
    "use hardcoded defaults". `breakdown_labels` is a dict keyed by the
    canonical bucket names (``increased / steady / low / none / decline``).
    `breakdown_bucket_filter` is a list of canonical bucket names that
    fire the auto-post (empty list = post every bucket).
    """
    import json
    if breakdown_thresholds is None:
        breakdown_thresholds = {}
    if breakdown_labels is None:
        breakdown_labels = {}
    if breakdown_bucket_filter is None:
        breakdown_bucket_filter = []
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE guild_growth_config SET "
            "  tab_breakdown = ?, "
            "  breakdown_thresholds = ?, "
            "  breakdown_labels = ?, "
            "  breakdown_post_channel_id = ?, "
            "  breakdown_bucket_filter = ? "
            "WHERE guild_id = ?",
            (
                tab_breakdown,
                json.dumps(breakdown_thresholds),
                json.dumps(breakdown_labels),
                int(breakdown_post_channel_id or 0),
                json.dumps(breakdown_bucket_filter),
                guild_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def has_survey_config(guild_id: int) -> bool:
    """True iff the guild has a row in `guild_survey_config` — i.e. they
    have run `/setup_survey` for the main survey at least once. The
    fallback dict from `get_survey_config` doesn't distinguish that
    case from "never configured"; this helper exists for the
    setup-wizard summary embed gate (#102). Extra named surveys live
    in a different table and aren't covered here — callers pass an
    `or {}` over `get_survey()` and check the result directly."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM guild_survey_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return row is not None


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
            "survey_id":            "default",
            "survey_name":          "Default",
            "tab_squad_powers":     cfg.get("tab_squad_powers", "Squad Powers"),
            "tab_history":          cfg.get("tab_history", "Survey History"),
            "questions":            cfg.get("questions", []),
            "intro_message":        cfg.get("intro_message", ""),
            "survey_channel_id":    0,
            "notify_channel_id":    0,
            "reminder_message":     cfg.get("reminder_message", ""),
            "reminder_enabled":     cfg.get("reminder_enabled", 0),
            "reminder_frequency":   cfg.get("reminder_frequency", "off"),
            "reminder_day_of_week": cfg.get("reminder_day_of_week", 1),
            "reminder_time":        cfg.get("reminder_time", "12:00"),
            "reminder_channel_id":  cfg.get("reminder_channel_id", 0),
            "reminder_use_dm":      cfg.get("reminder_use_dm", 0),
            "reminder_last_fired":  cfg.get("reminder_last_fired", ""),
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


def save_survey_reminder(
    guild_id: int, survey_id: str, *,
    enabled: int            = 0,
    frequency: str          = "off",
    day_of_week: int        = 1,
    time_str: str           = "12:00",
    channel_id: int         = 0,
    use_dm: int             = 0,
    message: str            = "",
):
    """
    Store the scheduled-reminder config for one survey. `survey_id="default"`
    targets `guild_survey_config`; any other id targets `guild_extra_surveys`.
    Both tables share the same reminder column names so the SQL can be
    parametrised.
    """
    table = "guild_survey_config" if survey_id == "default" else "guild_extra_surveys"
    where = "guild_id = ?" if survey_id == "default" else "guild_id = ? AND survey_id = ?"
    params: tuple = (
        enabled, frequency, day_of_week, time_str,
        channel_id, use_dm, message,
        guild_id,
    )
    if survey_id != "default":
        params = params + (survey_id,)
    with _get_conn() as conn:
        cur = conn.execute(
            f"UPDATE {table} SET "
            f"  reminder_enabled = ?, "
            f"  reminder_frequency = ?, "
            f"  reminder_day_of_week = ?, "
            f"  reminder_time = ?, "
            f"  reminder_channel_id = ?, "
            f"  reminder_use_dm = ?, "
            f"  reminder_message = ? "
            f"WHERE {where}",
            params,
        )
        conn.commit()
        return cur.rowcount > 0


def update_survey_reminder_last_fired(guild_id: int, survey_id: str, when_iso: str):
    """Stamp the last-fired timestamp so the scheduler doesn't double-fire."""
    table = "guild_survey_config" if survey_id == "default" else "guild_extra_surveys"
    where = "guild_id = ?" if survey_id == "default" else "guild_id = ? AND survey_id = ?"
    params = (when_iso, guild_id) + (() if survey_id == "default" else (survey_id,))
    with _get_conn() as conn:
        conn.execute(
            f"UPDATE {table} SET reminder_last_fired = ? WHERE {where}",
            params,
        )
        conn.commit()


def list_scheduled_survey_reminders() -> list[dict]:
    """
    Return every survey across all guilds that has scheduled reminders
    enabled. Used by the scheduler tick to know what to fire.
    """
    out: list[dict] = []
    with _get_conn() as conn:
        # Default surveys: one row per guild.
        for r in conn.execute(
            "SELECT g.guild_id, "
            "       s.reminder_enabled, s.reminder_frequency, s.reminder_day_of_week, "
            "       s.reminder_time, s.reminder_channel_id, s.reminder_use_dm, "
            "       s.reminder_message, s.reminder_last_fired "
            "FROM guild_survey_config s "
            "JOIN guild_configs g ON g.guild_id = s.guild_id "
            "WHERE s.reminder_enabled = 1 AND s.reminder_frequency != 'off'"
        ).fetchall():
            d = dict(r)
            d["survey_id"]   = "default"
            d["survey_name"] = "Default"
            out.append(d)
        # Extra surveys: many per guild.
        for r in conn.execute(
            "SELECT * FROM guild_extra_surveys "
            "WHERE reminder_enabled = 1 AND reminder_frequency != 'off'"
        ).fetchall():
            out.append(dict(r))
    return out


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


def has_birthday_config(guild_id: int) -> bool:
    """True iff the guild has a row in `guild_birthday_config` — i.e. they
    have run `/setup_birthdays` at least once. `get_birthday_config`
    returns a fallback dict on miss, so it can't distinguish "saved
    with all defaults" from "never configured"; this helper exists for
    the setup-wizard summary embed and the disable-with-clear gate
    (#98)."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM guild_birthday_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return row is not None


def clear_birthday_config(guild_id: int) -> None:
    """Delete the guild's birthday config row entirely. Called by the
    `ask_disable_with_clear` Clear button when leadership picks "🗑️
    Clear my saved configuration" after disabling birthday tracking.
    `get_birthday_config` already returns a default dict when the row
    is absent, so deletion is the cleanest reset."""
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM guild_birthday_config WHERE guild_id = ?",
            (guild_id,),
        )
        conn.commit()


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
        "guild_id":                   guild_id,
        "tab_name":                   "Birthdays",
        "name_col":                   0,
        "birthday_col":               1,
        "discord_id_col":             -1,
        "data_start_row":             2,
        "enabled":                    0,
        "train_integration":          0,
        "flexible_placement":         1,
        "lookahead_days":             14,
        "reminders_enabled":          0,
        "reminder_channel_id":        0,
        "reminder_time":              "08:00",
        "dm_message":                 "",
        "last_train_population_date": "",
    }


def save_birthday_config(guild_id: int, tab_name: str, name_col: int,
                         birthday_col: int, discord_id_col: int = -1,
                         data_start_row: int = 2, enabled: int = 1,
                         train_integration: int = 0, flexible_placement: int = 1,
                         lookahead_days: int = 14, reminders_enabled: int = 0,
                         reminder_channel_id: int = 0, reminder_time: str = "08:00",
                         dm_message: str = ""):
    """Insert or replace a guild's birthday config."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_birthday_config "
            "(guild_id, tab_name, name_col, birthday_col, discord_id_col, data_start_row, "
            "enabled, train_integration, flexible_placement, lookahead_days, "
            "reminders_enabled, reminder_channel_id, reminder_time, dm_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "tab_name=excluded.tab_name, name_col=excluded.name_col, "
            "birthday_col=excluded.birthday_col, discord_id_col=excluded.discord_id_col, "
            "data_start_row=excluded.data_start_row, enabled=excluded.enabled, "
            "train_integration=excluded.train_integration, "
            "flexible_placement=excluded.flexible_placement, "
            "lookahead_days=excluded.lookahead_days, "
            "reminders_enabled=excluded.reminders_enabled, "
            "reminder_channel_id=excluded.reminder_channel_id, "
            "reminder_time=excluded.reminder_time, "
            "dm_message=excluded.dm_message",
            (guild_id, tab_name, name_col, birthday_col, discord_id_col, data_start_row,
             enabled, train_integration, flexible_placement, lookahead_days,
             reminders_enabled, reminder_channel_id, reminder_time, dm_message)
        )
        conn.commit()


def get_birthday_population_last_fired(guild_id: int) -> str:
    """Return the ISO date the birthday auto-population last fired for
    this guild, or `""` when it hasn't fired yet (or the guild has no
    birthday config row). Used by the train cog's 22:00 ET scheduler to
    dedup across bot restarts — the previous in-memory set on the cog
    instance got wiped on every Railway redeploy. See #89."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT last_train_population_date FROM guild_birthday_config "
            "WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    if row is None:
        return ""
    return (dict(row).get("last_train_population_date") or "")


def mark_birthday_population_fired(guild_id: int, date_iso: str) -> None:
    """Stamp `last_train_population_date` so subsequent ticks (including
    fresh-process ticks after a Railway redeploy) skip the auto-pop for
    the rest of the day. UPDATE-only — the guild has to have run
    `/setup_birthdays` first for the row to exist, in which case the
    caller's `bcfg.get("train_integration")` check has already passed
    so we know the row is present."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE guild_birthday_config SET last_train_population_date = ? "
            "WHERE guild_id = ?",
            (date_iso, guild_id),
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
        parsed = [{"name": "Default", "template": DEFAULT_PROMPT}]
    d["templates"] = parsed
    # Pin default_template to a name that actually exists in the list.
    known_names = {t.get("name") for t in parsed}
    if not d.get("default_template") or d["default_template"] not in known_names:
        d["default_template"] = parsed[0]["name"]
    # Back-compat field for any caller still reading .prompt_template:
    d["prompt_template"] = parsed[0].get("template", "")
    return d


def has_train_config(guild_id: int) -> bool:
    """True iff the guild has a row in `guild_train_config` — i.e. they
    have run `/setup_train` at least once. `get_train_config` returns
    a fallback dict on miss, so it can't distinguish "saved with all
    defaults" from "never configured"; this helper exists for the
    setup-wizard summary embed (#97) which only renders when there is
    real saved config to show."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM guild_train_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return row is not None


def get_train_config(guild_id: int) -> dict:
    """Return the train config for a guild, falling back to framework defaults."""
    import json
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_train_config WHERE guild_id = ?",
            (guild_id,)
        ).fetchone()
    if row:
        d = dict(row)
        try:
            d["themes"] = json.loads(d["themes"]) if d["themes"] else DEFAULT_THEMES
            d["tones"]  = json.loads(d["tones"])  if d["tones"]  else DEFAULT_TONES
        except (json.JSONDecodeError, TypeError):
            d["themes"] = DEFAULT_THEMES
            d["tones"]  = DEFAULT_TONES
        return _normalize_train_templates(d)
    fallback = {
        "guild_id":            guild_id,
        "tab_name":            "Train Schedule",
        "blurbs_enabled":      1,
        "themes":              DEFAULT_THEMES,
        "tones":               DEFAULT_TONES,
        "prompt_template":     DEFAULT_PROMPT,
        "templates_json":      "",
        "default_template":    "Default",
        "default_tone":        "Default (match the theme)",
        "reminders_enabled":   1,
        "reminder_channel_id": 0,
        "reminder_time":       "22:00",
        "dm_message":          "",
    }
    return _normalize_train_templates(fallback)


def save_train_config(guild_id: int, tab_name: str, themes: list,
                      tones: list, prompt_template: str, default_tone: str,
                      blurbs_enabled: int = 1, reminders_enabled: int = 1,
                      reminder_channel_id: int = 0, reminder_time: str = "22:00",
                      templates: list | None = None, default_template: str = "Default",
                      dm_message: str = ""):
    """
    Insert or replace a guild's train config.

    For backwards compatibility, callers may still pass `prompt_template` (a
    single string). When `templates` is None, that string is automatically
    wrapped as a single-entry list named "Default". Premium callers can pass
    `templates=[{name, template}, ...]` directly.

    `dm_message` is the body of the Premium DM-to-assignee that fires
    alongside the channel reminder. Empty string means "use the hardcoded
    default". Supports the `{name}` placeholder.
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
            " reminders_enabled, reminder_channel_id, reminder_time, dm_message) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "tab_name=excluded.tab_name, blurbs_enabled=excluded.blurbs_enabled, "
            "themes=excluded.themes, tones=excluded.tones, "
            "prompt_template=excluded.prompt_template, "
            "templates_json=excluded.templates_json, "
            "default_template=excluded.default_template, "
            "default_tone=excluded.default_tone, "
            "reminders_enabled=excluded.reminders_enabled, "
            "reminder_channel_id=excluded.reminder_channel_id, "
            "reminder_time=excluded.reminder_time, "
            "dm_message=excluded.dm_message",
            (guild_id, tab_name, blurbs_enabled, json.dumps(themes), json.dumps(tones),
             default_text, templates_json, default_template, default_tone,
             reminders_enabled, reminder_channel_id, reminder_time, dm_message)
        )
        conn.commit()


# ── Shiny Tasks (free-tier daily announcement of shiny servers) ───────────────

def get_shiny_tasks_config(guild_id: int) -> dict:
    """Return shiny-tasks config for a guild, or a default dict if absent."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_shiny_tasks_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    if row:
        return dict(row)
    return {
        "guild_id":         guild_id,
        "enabled":          0,
        "channel_id":       0,
        "post_time":        "09:00",
        "server_min":       0,
        "server_max":       0,
        "message_template": "",
        "last_posted_date": "",
    }


def save_shiny_tasks_config(
    guild_id: int, *,
    enabled: int,
    channel_id: int,
    post_time: str,
    server_min: int,
    server_max: int,
    message_template: str,
):
    """Insert or replace a guild's shiny-tasks config.

    `last_posted_date` is managed by the scheduler loop (see
    `mark_shiny_tasks_posted`), not the wizard, so it isn't a parameter
    here — leaving the column as-is on update preserves the loop's
    duplicate-suppression state across re-runs of the setup wizard.
    """
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_shiny_tasks_config "
            "(guild_id, enabled, channel_id, post_time, server_min, server_max, "
            " message_template, last_posted_date) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE("
            "  (SELECT last_posted_date FROM guild_shiny_tasks_config WHERE guild_id = ?),"
            "  ''"
            ")) "
            "ON CONFLICT(guild_id) DO UPDATE SET "
            "enabled=excluded.enabled, channel_id=excluded.channel_id, "
            "post_time=excluded.post_time, server_min=excluded.server_min, "
            "server_max=excluded.server_max, "
            "message_template=excluded.message_template",
            (guild_id, enabled, channel_id, post_time, server_min, server_max,
             message_template, guild_id),
        )
        conn.commit()


def mark_shiny_tasks_posted(guild_id: int, posted_date: str) -> None:
    """Record that today's announcement has fired for this guild.

    Called by the scheduler loop right after a successful channel.send so
    a Railway restart across the configured minute can't double-post.
    """
    with _get_conn() as conn:
        conn.execute(
            "UPDATE guild_shiny_tasks_config SET last_posted_date = ? "
            "WHERE guild_id = ?",
            (posted_date, guild_id),
        )
        conn.commit()


def list_shiny_enabled_guild_ids() -> list[int]:
    """Return guild_ids with `enabled=1` shiny-tasks config.

    Used by the per-minute scheduler loop to walk only the guilds that
    actually opted in, instead of every configured guild.
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT guild_id FROM guild_shiny_tasks_config WHERE enabled = 1"
        ).fetchall()
    return [r["guild_id"] for r in rows]


def upsert_shiny_task_servers(
    rows: list[tuple[int, str, str]], seen_at: str,
) -> int:
    """Upsert a batch of (server_number, creation_date, region) rows.

    Every row gets `last_seen_at = seen_at`. Returns the number of rows
    upserted. Called by `shiny_tasks.refresh_servers` after fetching the
    cpt-hedge bundle.
    """
    if not rows:
        return 0
    payload = [(n, cd, region, seen_at) for (n, cd, region) in rows]
    with _get_conn() as conn:
        conn.executemany(
            "INSERT INTO shiny_task_servers "
            "(server_number, creation_date, region, last_seen_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(server_number) DO UPDATE SET "
            "creation_date=excluded.creation_date, "
            "region=excluded.region, "
            "last_seen_at=excluded.last_seen_at",
            payload,
        )
        conn.commit()
    return len(payload)


def get_shiny_task_servers_in_range(
    server_min: int, server_max: int, *, max_age_days: int = 30,
) -> list[dict]:
    """Return rows in [server_min, server_max] seen within `max_age_days`.

    The `max_age_days` filter is the soft-delete mechanism: any server
    missing from the last N refreshes is excluded automatically. Result
    is sorted by server_number for deterministic announcement copy.
    """
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    cutoff = (_dt.now(tz=_tz.utc) - _td(days=max_age_days)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT server_number, creation_date, region, last_seen_at "
            "FROM shiny_task_servers "
            "WHERE server_number BETWEEN ? AND ? "
            "  AND last_seen_at >= ? "
            "ORDER BY server_number",
            (server_min, server_max, cutoff),
        ).fetchall()
    return [dict(r) for r in rows]


def count_shiny_task_servers() -> int:
    """Return the total row count in `shiny_task_servers` (used to
    decide whether the initial seed has run)."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM shiny_task_servers"
        ).fetchone()
    return row["n"] if row else 0


def get_last_shiny_refresh_at():
    """Return the most recent `last_seen_at` from `shiny_task_servers` as
    a tz-aware UTC `datetime`, or `None` if the table is empty.

    Used by the weekly refresh loop to gate cpt-hedge re-fetches across
    process restarts: `tasks.loop` fires its body immediately on
    `.start()`, so Railway redeploys would otherwise hammer Hedge once
    per deploy. The 7-day interval lives in process memory only —
    `MAX(last_seen_at)` is the persistent equivalent.
    """
    from datetime import datetime as _dt
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(last_seen_at) AS last FROM shiny_task_servers"
        ).fetchone()
    if not row or not row["last"]:
        return None
    return _dt.fromisoformat(row["last"])

