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
    # Opt-out toggle for the release-announcement embed posted to the
    # leadership channel when a new major/minor version deploys (#253).
    # Defaults to enabled so existing alliances see the next release;
    # surfaced as a toggle in the `/setup` re-entry hub.
    release_announcements_enabled: int   = 1

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
                setup_complete           INTEGER DEFAULT 0,
                release_announcements_enabled INTEGER DEFAULT 1
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
        # `dm_reminder_message` is the body of the Premium DM that fires
        # when leadership clicks `🔔 Send DM reminder to roster` on
        # `/desertstorm` or `/canyonstorm`; empty string means "use the
        # hardcoded default in storm_log.py".
        # Supports `{name}` placeholder.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_storm_config (
                guild_id                 INTEGER NOT NULL,
                event_type               TEXT    NOT NULL,
                tab_name                 TEXT    DEFAULT 'DS Assignments',
                mail_template            TEXT    DEFAULT '',
                templates_json           TEXT    DEFAULT '[]',
                default_template         TEXT    DEFAULT 'Default',
                timezone                 TEXT    DEFAULT 'America/New_York',
                log_channel_id           INTEGER DEFAULT 0,
                post_channel_id          INTEGER DEFAULT 0,
                dm_reminder_message      TEXT    DEFAULT '',
                -- Which teams the alliance runs for this event (#148 +
                -- Rule A / #166): 'both' | 'A' | 'B'. Applies identically
                -- to DS and CS — leadership decides whether to run one
                -- or two teams per event.
                teams                    TEXT    DEFAULT 'both',
                -- Structured storm flow (#38 + #54)
                structured_flow_enabled  INTEGER DEFAULT 0,
                power_metric_column      TEXT    DEFAULT 'B',
                -- Power Data Source flexibility: alliances maintain
                -- power data in different places — the bot's own
                -- Squad Power Survey writes to "Squad Powers", some
                -- alliances paste into the Member Roster, some keep
                -- a custom tab. `power_metric_tab` is the tab the
                -- structured roster builder reads at builder time
                -- (empty = Member Roster, preserving the pre-existing
                -- single-tab default). `power_match_column` is the
                -- column on that tab that identifies each row;
                -- the read path matches by Discord ID when the cell
                -- looks like one, otherwise by case-insensitive name.
                -- Empty `power_match_column` = use Member Roster's
                -- discord_id_col (existing behaviour).
                power_metric_tab         TEXT    DEFAULT '',
                power_match_column       TEXT    DEFAULT '',
                sub_mode                 TEXT    DEFAULT 'pool',
                signup_channel_id        INTEGER DEFAULT 0,
                signup_schedule_cron     TEXT    DEFAULT '',
                signups_tab              TEXT    DEFAULT '',
                rosters_tab              TEXT    DEFAULT '',
                attendance_tab           TEXT    DEFAULT '',
                strategies_tab           TEXT    DEFAULT '',
                member_rules_tab         TEXT    DEFAULT '',
                poll_day_of_week         INTEGER DEFAULT -1,
                signup_time              TEXT    DEFAULT '',
                power_refresh_dm_enabled INTEGER DEFAULT 0,
                -- Stale-power DM nudge (#255). Generalises #138's
                -- blank/unparseable nudge to also fire when the
                -- voter's power value is older than N days. The
                -- timestamp source is configurable (tab + column +
                -- match column) so it works with the bot's own
                -- Squad Power Survey "Date Modified" column, a
                -- manually-maintained column, or an export from a
                -- different bot. Empty tab OR `power_refresh_stale_days = 0`
                -- = stale check off (master toggle stays
                -- `power_refresh_dm_enabled`). Empty match column =
                -- reuse `power_match_column` (which itself falls
                -- back to Member Roster's discord_id_col when empty).
                power_last_updated_tab           TEXT    DEFAULT '',
                power_last_updated_column        TEXT    DEFAULT '',
                power_last_updated_match_column  TEXT    DEFAULT '',
                power_refresh_stale_days         INTEGER DEFAULT 0,
                -- Roster DM templates (#226 follow-up). Empty string =
                -- fall back to defaults.py's DEFAULT_ROSTER_DM_*
                -- constants. Three slots because each role (Starter,
                -- Paired Sub, Pool Sub) gets its own message shape.
                roster_dm_starter_template    TEXT DEFAULT '',
                roster_dm_paired_sub_template TEXT DEFAULT '',
                roster_dm_pool_sub_template   TEXT DEFAULT '',
                -- Per-team time-slot mapping (#251). 1 or 2, indexing into
                -- DS_SERVER_TIMES / CS_SERVER_TIMES. NULL until leadership
                -- picks the mapping in `/setup` → Desert Storm / Canyon
                -- Storm. The signup post creation flow blocks with a
                -- "pick team times first" message when a slot needed by
                -- the alliance's `teams` setting is still NULL. Both
                -- teams can share a slot.
                team_a_slot_index        INTEGER,
                team_b_slot_index        INTEGER,
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
        # `/admin forget_guild` so kicked guilds aren't retained.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS guild_install_metadata (
                guild_id          INTEGER PRIMARY KEY,
                guild_name        TEXT    NOT NULL DEFAULT '',
                owner_id          INTEGER NOT NULL DEFAULT 0,
                installer_user_id INTEGER,
                installed_at      TEXT    NOT NULL,
                last_seen_at      TEXT    NOT NULL,
                last_seen_version TEXT    NOT NULL DEFAULT ''
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

        # storm_signups — one row per member per event. Captures who voted
        # what, when, and (for on-behalf votes) which officer cast it.
        # `target_member_id` is a free-form string:
        #   * Discord self-vote: str(discord_user_id)
        #   * On-behalf for a non-Discord member: roster row identifier
        #     (canonical member name from the alliance roster Sheet)
        # `vote` is one of: a, b, either, cannot. Re-votes UPSERT this row
        # (the unique constraint on (guild_id, event_type, event_date,
        # target_member_id) enforces one row per member per event).
        # Each row also remembers `message_id` / `channel_id` so the
        # startup hook can re-register the SignupView and so officer
        # views can link back to the source post.
        # See #38 + #123 + #124.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS storm_signups (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id          INTEGER NOT NULL,
                event_type        TEXT    NOT NULL,
                event_date        TEXT    NOT NULL,
                target_member_id  TEXT    NOT NULL,
                voter_user_id     INTEGER NOT NULL,
                vote              TEXT    NOT NULL,
                is_on_behalf      INTEGER NOT NULL DEFAULT 0,
                channel_id        INTEGER NOT NULL DEFAULT 0,
                message_id        INTEGER NOT NULL DEFAULT 0,
                voted_at          TEXT    NOT NULL,
                UNIQUE (guild_id, event_type, event_date, target_member_id)
            )
        """)

        # storm_registration_posts — table-of-record for "this message
        # exists and is a sign-up post." Written by the scheduler in
        # #124 when it posts a fresh registration message; read by the
        # bot startup hook (#123) to re-register the SignupView so
        # buttons survive restarts. Also lets the scheduler enforce
        # idempotence (one post per guild per event_type per event_date)
        # so a Railway restart during the configured minute can't
        # double-post.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS storm_registration_posts (
                -- Surrogate PK (#265) so multiple posts can coexist for
                -- the same (guild, event_type, event_date). Leadership
                -- re-posts a fresh sign-up when the original gets lost
                -- in channel chatter; all rows feed into the same vote
                -- aggregate keyed on (guild, event_type, event_date),
                -- and persistent-View re-registration attaches to every
                -- live message_id on startup.
                id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id           INTEGER NOT NULL,
                event_type         TEXT    NOT NULL,
                event_date         TEXT    NOT NULL,
                channel_id         INTEGER NOT NULL,
                message_id         INTEGER NOT NULL,
                time_a_label       TEXT    DEFAULT '',
                time_b_label       TEXT    DEFAULT '',
                -- Per-event team→slot indices (#251). 1 or 2 indexing into
                -- DS_SERVER_TIMES / CS_SERVER_TIMES. Captured at post time
                -- from either the guild default (guild_storm_config.team_*_slot_index)
                -- or a one-week override the officer picked when posting.
                -- 0 = legacy / unknown.
                team_a_slot_index  INTEGER DEFAULT 0,
                team_b_slot_index  INTEGER DEFAULT 0,
                posted_at          TEXT    NOT NULL
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_storm_reg_posts_event
            ON storm_registration_posts (guild_id, event_type, event_date)
        """)

        # walkthrough_dismissals — per-officer-per-guild record of which
        # guided micro-tours an officer has already seen (or actively
        # declined). The bot offers the tour exactly once per
        # (guild_id, user_id, walkthrough_key); a single key bump
        # (e.g. `storm_signups_v1` → `storm_signups_v2`) re-offers the
        # tour after a major UI rewrite without losing per-officer
        # dismissal records.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS walkthrough_dismissals (
                guild_id         INTEGER NOT NULL,
                user_id          INTEGER NOT NULL,
                walkthrough_key  TEXT    NOT NULL,
                dismissed_at     TEXT    NOT NULL,
                PRIMARY KEY (guild_id, user_id, walkthrough_key)
            )
        """)

        # storm_signup_history — append-only audit log for storm sign-up
        # votes. `storm_signups` UPSERTs on (guild_id, event_type,
        # event_date, target_member_id) so the prior vote, the prior
        # voter (officer for on-behalf, self otherwise), and the prior
        # timestamp are overwritten. The audit-trail requirement in #38
        # ("on-behalf vote logs the casting officer's Discord ID") means
        # the bot must be able to reconstruct who recorded what and when
        # — keep every recorded vote here, not just the current one.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS storm_signup_history (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id          INTEGER NOT NULL,
                event_type        TEXT    NOT NULL,
                event_date        TEXT    NOT NULL,
                target_member_id  TEXT    NOT NULL,
                voter_user_id     INTEGER NOT NULL,
                vote              TEXT    NOT NULL,
                is_on_behalf      INTEGER NOT NULL DEFAULT 0,
                voted_at          TEXT    NOT NULL
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_storm_signup_history_event "
            "ON storm_signup_history (guild_id, event_type, event_date)"
        )

        # storm_power_refresh_dms_sent — one row per (guild, event_type,
        # event_date, voter) recording whether the power-refresh DM
        # nudge (#138) has been sent. Primary-key shape doubles as the
        # cooldown — duplicate INSERT OR IGNORE is a no-op so a re-vote
        # on the same event doesn't trigger a second nudge. Survives
        # bot restarts (in-memory would risk double-DM after a Railway
        # bounce).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS storm_power_refresh_dms_sent (
                guild_id       INTEGER NOT NULL,
                event_type     TEXT    NOT NULL,
                event_date     TEXT    NOT NULL,
                voter_user_id  INTEGER NOT NULL,
                sent_at        TEXT    NOT NULL,
                PRIMARY KEY (guild_id, event_type, event_date, voter_user_id)
            )
        """)

        # storm_session_state — per-(guild, event_type, event_date, team)
        # lock that the structured-flow roster builder takes when an
        # officer opens it. Prevents two officers from independently
        # building the same team for the same event and each posting
        # their own mail + writing their own rosters_tab rows. Released
        # on Approve, Cancel/Done, or builder timeout.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS storm_session_state (
                guild_id    INTEGER NOT NULL,
                event_type  TEXT    NOT NULL,
                event_date  TEXT    NOT NULL,
                team        TEXT    NOT NULL DEFAULT '',
                user_id     INTEGER NOT NULL,
                opened_at   TEXT    NOT NULL,
                PRIMARY KEY (guild_id, event_type, event_date, team)
            )
        """)
        conn.commit()

        # storm_team_plans — per-(guild, event_type, event_date, team) record
        # of the 30 players the officer committed to in-game for this event,
        # split into 20 primaries + 10 subs. Captured before the structured
        # roster builder opens; consumed by `open_roster_builder` to
        # constrain the candidate pool and seed the sub list so auto-fill
        # mirrors the in-game commitment instead of fighting it. See #239.
        #
        # Players are restricted to one team per event (matches the in-game
        # rule: once you submit a team, you can't move that player). The
        # extra UNIQUE INDEX enforces this across the (guild, event, team)
        # composite key — Team A and Team B cannot share a target_member_id
        # for the same event. CS uses the same A/B model as DS (#166).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS storm_team_plans (
                guild_id          INTEGER NOT NULL,
                event_type        TEXT    NOT NULL,
                event_date        TEXT    NOT NULL,
                team              TEXT    NOT NULL,
                target_member_id  TEXT    NOT NULL,
                role              TEXT    NOT NULL,
                saved_by_user_id  INTEGER NOT NULL,
                saved_at          TEXT    NOT NULL,
                PRIMARY KEY (guild_id, event_type, event_date, team, target_member_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_storm_team_plans_event "
            "ON storm_team_plans (guild_id, event_type, event_date, team)"
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_storm_team_plans_one_team_per_member "
            "ON storm_team_plans (guild_id, event_type, event_date, target_member_id)"
        )
        conn.commit()

        # storm_roster_drafts — per-(guild, event_type, team) snapshot of
        # the structured roster builder's in-progress state (#240).
        # Auto-saved on every state change; loaded when the officer
        # clicks `♻️ Resume Team X roster` on the OfficerView. Survives
        # View timeouts AND Railway redeploys so a builder session can
        # take longer than 1 hour without losing work.
        #
        # NOTE: `event_date` is stored for the load-time staleness
        # warning ("This draft was last saved for Sat May 18, signups
        # may have shifted") but is *not* part of the PRIMARY KEY. One
        # row per team — drafts are reusable across weeks. The team
        # plan (storm_team_plans) carries the per-event-date member
        # composition; the draft just carries zone assignments and sub
        # pairings on top, applied to whoever is in the current week's
        # plan / signups at load time.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS storm_roster_drafts (
                guild_id      INTEGER NOT NULL,
                event_type    TEXT    NOT NULL,
                team          TEXT    NOT NULL,
                session_json  TEXT    NOT NULL,
                event_date    TEXT    NOT NULL,
                updated_at    TEXT    NOT NULL,
                PRIMARY KEY (guild_id, event_type, team)
            )
        """)
        conn.commit()

        # storm_roster_images — pointer to a public roster-image message
        # in Discord, written by the `💾 Save to history` action on the
        # builder's render flow. The history browser surfaces this as
        # a `📷 View image` button on the matching event embed so a
        # roster image from week N is still retrievable in week N+8.
        # `team` differentiates DS Team A / Team B; CS uses empty string.
        # UPSERT on the composite key — re-saving overwrites the prior
        # pointer (officer rendered + saved twice). Image bytes are not
        # stored anywhere — Discord hosts the message, and we resolve
        # via channel.fetch_message at click time so a deleted message
        # is detected gracefully rather than served as a stale link.
        conn.execute("""
            CREATE TABLE IF NOT EXISTS storm_roster_images (
                guild_id          INTEGER NOT NULL,
                event_type        TEXT    NOT NULL,
                event_date        TEXT    NOT NULL,
                team              TEXT    NOT NULL DEFAULT '',
                channel_id        INTEGER NOT NULL,
                message_id        INTEGER NOT NULL,
                posted_by_user_id INTEGER NOT NULL,
                posted_at         TEXT    NOT NULL,
                PRIMARY KEY (guild_id, event_type, event_date, team)
            )
        """)
        conn.commit()

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
            # Premium DM-reminder body — sent when leadership clicks
            # `🔔 Send DM reminder to roster` on the storm hub
            # (empty → hardcoded default in storm_log.py).
            ("dm_reminder_message",           "TEXT    DEFAULT ''"),
            # Which DS teams the alliance runs (#148) — 'both' | 'A' | 'B'.
            # Defaults to 'both' so existing alliances see no behaviour change.
            # CS rows ignore this field.
            ("teams",                         "TEXT    DEFAULT 'both'"),
            # ── Structured storm flow (#38 + #54) ────────────────────────────────
            # Premium opt-in structured roster builder. `structured_flow_enabled`
            # gates the registration post, on-behalf voting, eligibility-gated
            # roster builder, and structured mail post. `power_metric_column`
            # is the column letter (A-Z) on the roster Sheet that stores power
            # — used at render time to look up the actual header (Rule C / #165).
            # `sub_mode` is `pool` (flat sub list) or `paired` (primary→sub
            # pairs). Tab names default to empty and are resolved to
            # event-type-aware defaults at read time.
            ("structured_flow_enabled", "INTEGER DEFAULT 0"),
            ("power_metric_column",     "TEXT    DEFAULT 'B'"),
            # Power Data Source flexibility — see CREATE TABLE comment.
            # Empty values preserve the pre-flexibility default of
            # reading power from the Member Roster tab keyed by its
            # discord_id_col, so existing alliances see zero behaviour
            # change until they reconfigure via the wizard.
            ("power_metric_tab",        "TEXT    DEFAULT ''"),
            ("power_match_column",      "TEXT    DEFAULT ''"),
            ("sub_mode",                "TEXT    DEFAULT 'pool'"),
            ("signup_channel_id",       "INTEGER DEFAULT 0"),
            ("signup_schedule_cron",    "TEXT    DEFAULT ''"),
            ("signups_tab",             "TEXT    DEFAULT ''"),
            ("rosters_tab",             "TEXT    DEFAULT ''"),
            ("attendance_tab",          "TEXT    DEFAULT ''"),
            ("strategies_tab",          "TEXT    DEFAULT ''"),
            ("member_rules_tab",        "TEXT    DEFAULT ''"),
            # Auto-scheduler (#131 + Rule H / #164). Event day is now
            # game-defined (DS = Friday, CS = Thursday), so we only
            # store the POLL day-of-week and the post time. -1 means
            # "manual posting only — use /<parent> post_signup".
            ("poll_day_of_week",        "INTEGER DEFAULT -1"),
            ("signup_time",             "TEXT    DEFAULT ''"),
            # Power-refresh DM nudge (#138). Premium-only — when on,
            # the SignupView click handler DMs the voter if their
            # power_column_name cell on the roster Sheet is missing
            # or unparseable. Once per (guild, event_type, event_date,
            # voter) — see storm_power_refresh_dms_sent below.
            ("power_refresh_dm_enabled", "INTEGER DEFAULT 0"),
            # Roster DM templates (#226 follow-up). One per role
            # (Starter / Paired Sub / Pool Sub) so each can carry
            # role-specific framing. Empty = fall back to
            # defaults.py's DEFAULT_ROSTER_DM_* constants at send time.
            ("roster_dm_starter_template",    "TEXT DEFAULT ''"),
            ("roster_dm_paired_sub_template", "TEXT DEFAULT ''"),
            ("roster_dm_pool_sub_template",   "TEXT DEFAULT ''"),
            # Per-team time-slot mapping (#251) — see CREATE TABLE comment.
            # No SQL default: NULL signals "leadership hasn't picked yet."
            ("team_a_slot_index",        "INTEGER"),
            ("team_b_slot_index",        "INTEGER"),
            # Stale-power DM nudge (#255) — see CREATE TABLE comment.
            ("power_last_updated_tab",          "TEXT    DEFAULT ''"),
            ("power_last_updated_column",       "TEXT    DEFAULT ''"),
            ("power_last_updated_match_column", "TEXT    DEFAULT ''"),
            ("power_refresh_stale_days",        "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE guild_storm_config ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to guild_storm_config")
            except Exception:
                pass

        # ── Drop judicator_role_id (Rule G / #167) ────────────────────────────
        # Faction-roles feature removed end-to-end. Drop the column.
        try:
            conn.execute("ALTER TABLE guild_storm_config DROP COLUMN judicator_role_id")
            conn.commit()
            print("[CONFIG] Dropped judicator_role_id from guild_storm_config")
        except Exception:
            pass

        # ── Power-column letter migration (Rule C / #165) ─────────────────────
        # Old shape stored `power_column_name TEXT DEFAULT ''` (a header
        # string like "1st Squad Power"). New shape stores the column
        # LETTER (A-Z) and resolves to the header row at render time.
        # Migration policy: every existing row falls back to the default
        # 'B' on the new column. Alliances using a non-B column re-run
        # setup to pick their letter — a one-time prompt that beats
        # making init_db() do per-guild gspread network calls at boot.
        try:
            conn.execute(
                "UPDATE guild_storm_config SET power_metric_column = 'B' "
                "WHERE power_metric_column = ''"
            )
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(
                "ALTER TABLE guild_storm_config DROP COLUMN power_column_name"
            )
            conn.commit()
            print("[CONFIG] Dropped power_column_name from guild_storm_config")
        except Exception:
            pass

        # ── Poll-day model migration (Rule H / #164) ──────────────────────────
        # Move alliances who configured event_day_of_week + signup_lead_days
        # under the old model to poll_day_of_week. Guard on the old columns
        # still existing (the DROP below runs in the same boot — re-runs
        # on already-migrated databases hit the except and skip silently).
        try:
            conn.execute(
                "UPDATE guild_storm_config "
                "SET poll_day_of_week = ((event_day_of_week - signup_lead_days) % 7 + 7) % 7 "
                "WHERE event_day_of_week >= 0 AND poll_day_of_week = -1"
            )
            conn.commit()
            print("[CONFIG] Migrated event_day_of_week + signup_lead_days "
                  "-> poll_day_of_week")
        except Exception:
            pass
        # Drop the retired columns in the same boot. SQLite 3.35+ supports
        # DROP COLUMN; Railway runs 3.40+. Try/except so re-runs (and
        # local sqlite < 3.35 if anyone forks the bot) don't crash.
        for col in ("event_day_of_week", "signup_lead_days"):
            try:
                conn.execute(f"ALTER TABLE guild_storm_config DROP COLUMN {col}")
                conn.commit()
                print(f"[CONFIG] Dropped {col} from guild_storm_config")
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

        # ── Per-event team→slot indices on storm_registration_posts (#251) ────
        # Captures the team-time mapping that was in effect when the
        # sign-up post went out, so attendance can reconstruct "what slot
        # was Team A on this event" without re-parsing the label.
        for col, definition in (
            ("team_a_slot_index", "INTEGER DEFAULT 0"),
            ("team_b_slot_index", "INTEGER DEFAULT 0"),
        ):
            try:
                conn.execute(f"ALTER TABLE storm_registration_posts ADD COLUMN {col} {definition}")
                conn.commit()
                print(f"[CONFIG] Added {col} to storm_registration_posts")
            except Exception:
                pass

        # ── storm_registration_posts → multi-post-per-event (#265) ────────────
        # Drop the composite PRIMARY KEY (guild_id, event_type, event_date)
        # and replace with a surrogate `id` so leadership can post a fresh
        # sign-up when the original gets lost in channel chatter. Detection:
        # the new schema has an `id` column. Old DBs don't. SQLite can't
        # ALTER an existing PRIMARY KEY, so the migration recreates the
        # table, copies rows over, then renames.
        try:
            cols = [r[1] for r in conn.execute(
                "PRAGMA table_info(storm_registration_posts)"
            ).fetchall()]
            if "id" not in cols:
                conn.execute("""
                    CREATE TABLE storm_registration_posts_v265 (
                        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id           INTEGER NOT NULL,
                        event_type         TEXT    NOT NULL,
                        event_date         TEXT    NOT NULL,
                        channel_id         INTEGER NOT NULL,
                        message_id         INTEGER NOT NULL,
                        time_a_label       TEXT    DEFAULT '',
                        time_b_label       TEXT    DEFAULT '',
                        team_a_slot_index  INTEGER DEFAULT 0,
                        team_b_slot_index  INTEGER DEFAULT 0,
                        posted_at          TEXT    NOT NULL
                    )
                """)
                conn.execute("""
                    INSERT INTO storm_registration_posts_v265
                    (guild_id, event_type, event_date, channel_id, message_id,
                     time_a_label, time_b_label, team_a_slot_index, team_b_slot_index, posted_at)
                    SELECT guild_id, event_type, event_date, channel_id, message_id,
                           time_a_label, time_b_label, team_a_slot_index, team_b_slot_index, posted_at
                    FROM storm_registration_posts
                """)
                conn.execute("DROP TABLE storm_registration_posts")
                conn.execute(
                    "ALTER TABLE storm_registration_posts_v265 "
                    "RENAME TO storm_registration_posts"
                )
                conn.execute("""
                    CREATE INDEX IF NOT EXISTS idx_storm_reg_posts_event
                    ON storm_registration_posts (guild_id, event_type, event_date)
                """)
                conn.commit()
                print("[CONFIG] Migrated storm_registration_posts to surrogate-id schema (#265)")
        except Exception as e:
            print(f"[CONFIG] storm_registration_posts #265 migration skipped: {e}")

        # ── 1.1.4: backfill numeric+magnitude on default LW survey keys ────────
        # The pre-rework hardcoded survey normalised shorthand like `301` to
        # 301,000,000 before writing to Sheets. The configurable-survey rework
        # dropped that, leaving stored values inconsistent with prior data and
        # hard for sheet-side sums to interpret. Numeric is now a free-tier
        # type and ships with a magnitude scaler. Backfill any saved guild
        # config whose questions still carry the original LW default keys so
        # leadership doesn't have to re-run the survey setup wizard by hand. Idempotent:
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

        # ── 1.3.4: release-announcement infra (#253) ───────────────────────────
        # Adds the opt-out toggle on guild_configs and the per-guild
        # `last_seen_version` so on_ready can detect a major/minor bump and
        # post a release-notification embed. DEFAULT '1.3.3' on the ALTER
        # is the migration backfill — existing rows pick up '1.3.3' so the
        # 1.3.4 deploy itself doesn't fire any announcements, then the
        # 1.4.0 deploy sees the major.minor change and triggers naturally.
        # New rows after this migration always get an explicit version via
        # `upsert_guild_install_metadata(current_version=...)`.
        try:
            conn.execute("ALTER TABLE guild_configs ADD COLUMN release_announcements_enabled INTEGER DEFAULT 1")
            conn.commit()
            print("[CONFIG] Added release_announcements_enabled to guild_configs")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE guild_install_metadata ADD COLUMN last_seen_version TEXT NOT NULL DEFAULT '1.3.3'")
            conn.commit()
            print("[CONFIG] Added last_seen_version to guild_install_metadata (existing rows backfilled to '1.3.3')")
        except Exception:
            pass


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


def get_or_create_worksheet(
    spreadsheet, tab_name: str, *,
    header_row=None, rows: int = 200, cols: int = 10,
):
    """Return the worksheet matching `tab_name`, creating it if absent.

    Used by the storm structured-flow surfaces (Sign-Ups, Rosters,
    Attendance, Strategies, Member Rules) so officers don't have to
    pre-create tabs as a manual step. `header_row` seeds row 1 on
    newly-created tabs; leaves existing tabs alone.

    Catches a broad `Exception` on the lookup so fake-worksheet
    test stubs (which raise plain `Exception("Worksheet X not found")`)
    work the same way as the real gspread `WorksheetNotFound`. The
    `add_worksheet` call is left to raise — if creation fails the
    caller surfaces the gspread error.
    """
    try:
        return spreadsheet.worksheet(tab_name)
    except Exception:
        ws = spreadsheet.add_worksheet(title=tab_name, rows=rows, cols=cols)
        if header_row:
            try:
                ws.update("A1", [list(header_row)])
            except TypeError:
                # Fake-worksheet stubs in tests use `append_row(header)`
                # instead of `update`; fall back so tests don't have to
                # learn the gspread API surface.
                ws.append_row(list(header_row), value_input_option="RAW")
        return ws


def power_column_letter_to_index(letter: str) -> int:
    """Convert a single column letter (A-Z) to a 0-indexed integer.

    `'A'` → 0, `'B'` → 1, …, `'Z'` → 25. Falls back to `1` (column B)
    for empty input, lowercase letters get normalised, anything else
    is invalid and also falls back to 1. Shared between the roster
    builder, signup-view power-refresh DM, and any future power-column
    reader (Rule C / #165).
    """
    cleaned = (letter or "").strip().upper()
    if len(cleaned) == 1 and "A" <= cleaned <= "Z":
        return ord(cleaned) - ord("A")
    return 1  # default B


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


def normalize_spreadsheet_id(value: str) -> str:
    """Pull a Google Sheets spreadsheet ID out of whatever the user pasted.

    Discord's `/setup` Step 5 prompts for the ID, but users routinely paste
    the full sheet URL (`https://docs.google.com/spreadsheets/d/{ID}/edit?...`)
    instead. The stored URL then gets fed to `gc.open_by_key(...)` which
    requests `/v4/spreadsheets/https://docs.google.com/...` and Google 404s
    on the resulting nonsense, with no signal back that the input was wrong.

    Strips whitespace, extracts the ID segment from a `/spreadsheets/d/{ID}`
    URL if one is present, and otherwise returns the value as-is.
    """
    import re
    cleaned = (value or "").strip()
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", cleaned)
    return m.group(1) if m else cleaned


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


def set_premium_assignment(user_id: int, guild_id: int) -> bool:
    """Assign or move this user's license to `guild_id`. Atomic.

    Returns True if the assignment now points (user_id → guild_id), or
    False if another subscriber already holds `guild_id` (race-protection:
    refuses to silently displace them). The race window matters only when
    two subscribers attempt to claim the same guild at nearly the same
    instant — slash-command surfaces pre-check via
    `get_premium_assignment_for_guild` before calling, and that check is
    still the primary defense. This atomic check is the safety net.

    The previous guild this user was assigned to (if any) is replaced
    in the same upsert; callers should invalidate the premium cache for
    both the old and new guild_ids on a True return.
    """
    from datetime import datetime, timezone
    import sqlite3
    now = datetime.now(timezone.utc).isoformat()
    try:
        with _get_conn() as conn:
            # ON CONFLICT(user_id) handles the same-user-moving-guilds case.
            # The UNIQUE(guild_id) constraint catches the race where another
            # subscriber claimed this guild between the caller's pre-check
            # and this insert — that path raises IntegrityError, the
            # transaction rolls back, both rows stay untouched.
            conn.execute(
                "INSERT INTO premium_assignments (user_id, guild_id, assigned_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET guild_id = excluded.guild_id, "
                "assigned_at = excluded.assigned_at",
                (user_id, guild_id, now),
            )
            conn.commit()
        return True
    except sqlite3.IntegrityError:
        # UNIQUE(guild_id) violation: another user holds the target guild.
        return False


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
    current_version: str = "",
) -> None:
    """Insert or update the metadata row for a guild.

    First sighting: writes all fields and stamps both `installed_at` and
    `last_seen_at` to now. Subsequent sightings: refreshes `guild_name`,
    `owner_id`, `last_seen_at`; preserves `installed_at` and only fills
    `installer_user_id` if it's still NULL (audit-log lookups can fail on
    later boots even when they succeeded once).

    `current_version` is the bot's `__version__` at the time of the
    sighting. On first sighting it's written to `last_seen_version` so
    fresh installs don't trigger a "Welcome to vX.Y.Z" announcement on
    their next deploy. On subsequent sightings `last_seen_version` is
    left alone — the release-announcement handler owns updates to that
    column.
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
                   (guild_id, guild_name, owner_id, installer_user_id,
                    installed_at, last_seen_at, last_seen_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, guild_name, owner_id, installer_user_id,
                 now, now, current_version),
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


def set_last_seen_version(guild_id: int, version: str) -> None:
    """Update only the `last_seen_version` column for a guild. Called by
    the release-announcement handler after a successful notification post
    so the next boot doesn't re-fire the announcement."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE guild_install_metadata SET last_seen_version = ? WHERE guild_id = ?",
            (version, guild_id),
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
    user-facing surface (TimeSelectView buttons, the /setup hub's 🗂️ View configuration button,
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


def get_storm_slot_label_by_index(
    event_type: str, slot_index: int | None, guild_id: int,
) -> str:
    """Render the label for a specific game slot (#251).

    `slot_index` is 1 or 2 (matching the `team_*_slot_index` columns);
    returns "" for None / out-of-range so callers can detect "unset"
    rather than fall back to a possibly-misleading default.
    """
    if slot_index not in (1, 2):
        return ""
    labels = get_storm_slot_labels(event_type, guild_id)
    if len(labels) < slot_index:
        return ""
    return labels[slot_index - 1]


def resolve_storm_team_slots(
    guild_id: int, event_type: str, event_date: str | None = None,
) -> tuple[int | None, int | None]:
    """Return (team_a_slot_index, team_b_slot_index) for an event (#251).

    Resolution order:
      1. If `event_date` is set and a `storm_registration_posts` row
         exists with non-zero indices, return those (the per-event
         mapping captured when the sign-up post went out — may differ
         from the current guild default if leadership picked an
         override for that week).
      2. Otherwise return the guild default from `guild_storm_config`.

    Returns `(None, None)` for either index that isn't configured.
    Callers gate signup-post creation on "both required indices set"
    per the alliance's `teams` config.
    """
    if event_date:
        post = get_storm_registration_post(guild_id, event_type, event_date)
        if post:
            a = post.get("team_a_slot_index") or 0
            b = post.get("team_b_slot_index") or 0
            if a or b:
                return (a if a in (1, 2) else None,
                        b if b in (1, 2) else None)

    cfg = get_storm_config(guild_id, event_type) or {}
    a = cfg.get("team_a_slot_index")
    b = cfg.get("team_b_slot_index")
    return (a if a in (1, 2) else None,
            b if b in (1, 2) else None)


def get_storm_team_slot_labels(
    guild_id: int, event_type: str, event_date: str | None = None,
) -> tuple[str, str]:
    """Return (team_a_label, team_b_label) in TEAM ORDER (#251).

    Driven by `resolve_storm_team_slots`. Empty strings for teams that
    haven't been assigned a slot yet — callers gate posting on whether
    the labels the alliance's `teams` config requires are non-empty.
    """
    a_idx, b_idx = resolve_storm_team_slots(guild_id, event_type, event_date)
    return (
        get_storm_slot_label_by_index(event_type, a_idx, guild_id),
        get_storm_slot_label_by_index(event_type, b_idx, guild_id),
    )


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
    event_type — i.e. they have run `the Desert Storm setup wizard` or
    `the Canyon Storm setup wizard` at least once. The fallback dict from
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
        "teams":                "both",
        # Structured storm flow (#38 + #54) — never-configured guilds get
        # all-off; tab fields resolve to event-type defaults via
        # default_structured_tab() / get_structured_storm_config().
        "structured_flow_enabled": 0,
        "power_metric_column":     "B",
        "power_metric_tab":        "",
        "power_match_column":      "",
        "sub_mode":                "pool",
        "signup_channel_id":       0,
        "signup_schedule_cron":    "",
        "signups_tab":             "",
        "rosters_tab":             "",
        "attendance_tab":          "",
        "strategies_tab":          "",
        "member_rules_tab":        "",
        "poll_day_of_week":        -1,
        "signup_time":             "",
        "power_refresh_dm_enabled": 0,
        # Stale-power DM nudge (#255) — never-configured guilds get
        # all-off, mirroring the rest of the structured-flow defaults.
        "power_last_updated_tab":           "",
        "power_last_updated_column":        "",
        "power_last_updated_match_column":  "",
        "power_refresh_stale_days":         0,
        # Per-team time-slot mapping (#251) — NULL until setup-touched.
        "team_a_slot_index":       None,
        "team_b_slot_index":       None,
    }
    return _normalize_storm_templates(fallback, event_type)


def save_storm_config(guild_id: int, event_type: str, tab_name: str,
                      mail_template: str,
                      timezone: str, log_channel_id: int = 0,
                      templates: list | None = None,
                      default_template: str = "Default",
                      post_channel_id: int = 0,
                      dm_reminder_message: str = "",
                      teams: str = "both"):
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

    `dm_reminder_message` is the body of the Premium DM that fires
    when leadership clicks `🔔 Send DM reminder to roster` on the
    `/desertstorm` or `/canyonstorm` event hub. Empty string means
    "use the hardcoded default in storm_log.py". Supports the
    `{name}` placeholder.

    `teams` is the wizard's "Which teams do you run?" answer for DS
    (`both` | `A` | `B`); CS ignores the field. Default `both` preserves
    pre-#148 behaviour for callers that don't pass it.
    """
    import json
    if templates is None:
        templates = [{"name": "Default", "template": mail_template or ""}]
    templates_json = json.dumps(templates)
    default_text = next(
        (t["template"] for t in templates if t.get("name") == default_template),
        templates[0]["template"] if templates else "",
    )
    teams_value = teams if teams in ("both", "A", "B") else "both"
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_storm_config "
            "(guild_id, event_type, tab_name, mail_template, templates_json, default_template, "
            "timezone, log_channel_id, post_channel_id, dm_reminder_message, teams) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, event_type) DO UPDATE SET "
            "tab_name=excluded.tab_name, mail_template=excluded.mail_template, "
            "templates_json=excluded.templates_json, "
            "default_template=excluded.default_template, "
            "timezone=excluded.timezone, "
            "log_channel_id=excluded.log_channel_id, "
            "post_channel_id=excluded.post_channel_id, "
            "dm_reminder_message=excluded.dm_reminder_message, "
            "teams=excluded.teams",
            (guild_id, event_type, tab_name, default_text, templates_json, default_template,
             timezone, log_channel_id, post_channel_id, dm_reminder_message, teams_value)
        )
        conn.commit()


def save_storm_team_slots(
    guild_id: int, event_type: str,
    team_a_slot_index: int | None,
    team_b_slot_index: int | None,
) -> None:
    """Persist the per-team time-slot mapping (#251) for a guild + event type.

    Each index is 1 or 2 (or None to reset). Stored on the existing
    `guild_storm_config` row (created with empty defaults if it doesn't
    exist yet). Kept separate from `save_storm_config` so the setup
    wizard's team-time step can be re-run without touching the rest of
    the storm config — same precedent as `save_structured_storm_config`.
    """
    def _norm(v):
        if v is None:
            return None
        try:
            i = int(v)
        except (TypeError, ValueError):
            return None
        return i if i in (1, 2) else None

    a = _norm(team_a_slot_index)
    b = _norm(team_b_slot_index)

    with _get_conn() as conn:
        # Ensure a row exists so the UPDATE has something to bite into.
        # Defaults from the CREATE TABLE definition apply on the INSERT
        # path; the UPDATE path only touches the two slot columns.
        conn.execute(
            "INSERT OR IGNORE INTO guild_storm_config (guild_id, event_type) "
            "VALUES (?, ?)",
            (int(guild_id), event_type),
        )
        conn.execute(
            "UPDATE guild_storm_config "
            "SET team_a_slot_index = ?, team_b_slot_index = ? "
            "WHERE guild_id = ? AND event_type = ?",
            (a, b, int(guild_id), event_type),
        )
        conn.commit()


# ── Structured storm flow config (#38 + #54) ─────────────────────────────────
#
# Shape of the structured flow config kept separate from the main storm config
# helpers so the existing `save_storm_config` signature doesn't balloon. This
# matches the `save_participation_config` pattern — UPDATE-only against an
# existing (guild_id, event_type) row.

# Defaults are resolved at read time (not at SQL-default time) because the
# default differs by event_type and the SQL layer can't see which row is DS
# vs CS.
_STRUCTURED_TAB_DEFAULTS = {
    "DS": {
        "signups_tab":      "DS Signups",
        "rosters_tab":      "DS Rosters",
        "attendance_tab":   "DS Attendance",
        "strategies_tab":   "DS Strategies",
        "member_rules_tab": "DS Member Rules",
    },
    "CS": {
        "signups_tab":      "CS Signups",
        "rosters_tab":      "CS Rosters",
        "attendance_tab":   "CS Attendance",
        "strategies_tab":   "CS Strategies",
        "member_rules_tab": "CS Member Rules",
    },
}


def default_structured_tab(event_type: str, field: str) -> str:
    """Resolve the event-type-aware default for a tab field. Returns '' if
    `event_type` isn't DS / CS or `field` isn't recognised — callers should
    treat that as "no default available."""
    return _STRUCTURED_TAB_DEFAULTS.get(event_type, {}).get(field, "")


def get_structured_storm_config(guild_id: int, event_type: str) -> dict:
    """Return the structured-flow config subset for a guild + event type.
    Unset tab fields fall back to event-type-aware defaults (e.g. DS row
    with empty `signups_tab` reads as 'DS Signups')."""
    cfg = get_storm_config(guild_id, event_type)
    def _tab(key: str) -> str:
        return cfg.get(key) or default_structured_tab(event_type, key)
    # poll_day_of_week stored as -1 (or None on a fallback dict) when
    # auto-scheduling hasn't been configured. Normalise to -1 so
    # callers can check `< 0` without worrying about Python's truthy-0
    # trap.
    raw_dow = cfg.get("poll_day_of_week")
    if raw_dow is None:
        raw_dow = -1
    return {
        "structured_flow_enabled": bool(cfg.get("structured_flow_enabled")),
        "power_metric_column":     (cfg.get("power_metric_column") or "B").upper(),
        # Power Data Source flexibility — empty values are the canonical
        # "use Member Roster + discord_id_col" signal; the read path in
        # storm_roster_builder._read_roster_powers interprets them.
        "power_metric_tab":        (cfg.get("power_metric_tab") or ""),
        "power_match_column":      (cfg.get("power_match_column") or "").upper(),
        "sub_mode":                cfg.get("sub_mode") or "pool",
        "signup_channel_id":       int(cfg.get("signup_channel_id") or 0),
        "signup_schedule_cron":    cfg.get("signup_schedule_cron") or "",
        "signups_tab":             _tab("signups_tab"),
        "rosters_tab":             _tab("rosters_tab"),
        "attendance_tab":          _tab("attendance_tab"),
        "strategies_tab":          _tab("strategies_tab"),
        "member_rules_tab":        _tab("member_rules_tab"),
        "poll_day_of_week":        int(raw_dow),
        "signup_time":             cfg.get("signup_time") or "",
        "power_refresh_dm_enabled": bool(cfg.get("power_refresh_dm_enabled")),
        # Stale-power DM nudge (#255). Empty tab / empty column /
        # 0 days each independently disable the stale branch (the
        # click-handler treats any of those as "skip the staleness
        # check"). Match column falls back to `power_match_column`
        # at read time when empty.
        "power_last_updated_tab":          (cfg.get("power_last_updated_tab") or ""),
        "power_last_updated_column":       (cfg.get("power_last_updated_column") or "").upper(),
        "power_last_updated_match_column": (cfg.get("power_last_updated_match_column") or "").upper(),
        "power_refresh_stale_days":        int(cfg.get("power_refresh_stale_days") or 0),
    }


def parse_storm_signup_time(value: str) -> Optional[str]:
    """Parse a user-entered signup time into canonical `HH:MM` (24-hour),
    or `None` if the input is unparseable.

    Permissive on input — accepts `"14:00"`, `"14"`, `"2pm"`, `"2:00pm"`,
    `"2:30 PM"`, etc. Returns `None` for empty / garbage so the wizard
    can distinguish "user gave us nothing → leave existing value alone"
    from "user gave us nonsense → re-prompt." Both wizards and the
    scheduler use this single helper so the two paths can't drift.
    """
    if value is None:
        return None
    raw = str(value).strip().lower()
    if not raw:
        return None
    # Strip am/pm suffix if present, remembering for hour fixup.
    is_pm = False
    is_am = False
    for suffix in ("am", "pm", "a.m.", "p.m."):
        if raw.endswith(suffix):
            is_pm = suffix.startswith("p")
            is_am = suffix.startswith("a")
            raw = raw[: -len(suffix)].strip()
            break
    if ":" in raw:
        h, _, m = raw.partition(":")
    else:
        h, m = raw, "0"
    try:
        hour = int(h)
        minute = int(m)
    except ValueError:
        return None
    if is_pm and hour < 12:
        hour += 12
    elif is_am and hour == 12:
        hour = 0
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def get_scheduled_storm_rows() -> list[dict]:
    """Return every (guild, event_type) row eligible for the auto-signup
    scheduler — structured flow on, poll-day set, and a non-empty
    signup_time. Public wrapper around the schema so callers (the
    minute-loop scheduler) don't reach into `_get_conn` directly."""
    rows = []
    with _get_conn() as conn:
        for row in conn.execute(
            "SELECT guild_id, event_type, poll_day_of_week, signup_time "
            "FROM guild_storm_config "
            "WHERE structured_flow_enabled = 1 "
            "  AND poll_day_of_week >= 0 "
            "  AND signup_time != ''",
        ).fetchall():
            rows.append(dict(row))
    return rows


def save_structured_storm_config(
    guild_id: int, event_type: str, *,
    structured_flow_enabled: bool = False,
    power_metric_column: str        = "B",
    power_metric_tab: str           = "",
    power_match_column: str         = "",
    sub_mode: str                   = "pool",
    signup_channel_id: int          = 0,
    signup_schedule_cron: str       = "",
    signups_tab: str                = "",
    rosters_tab: str                = "",
    attendance_tab: str             = "",
    strategies_tab: str             = "",
    member_rules_tab: str           = "",
    poll_day_of_week: int           = -1,
    signup_time: str                = "",
    power_refresh_dm_enabled: bool  = False,
    power_last_updated_tab: str           = "",
    power_last_updated_column: str        = "",
    power_last_updated_match_column: str  = "",
    power_refresh_stale_days: int         = 0,
) -> bool:
    """UPDATE the structured-flow fields on an existing (guild_id, event_type)
    row. The row must already exist (created by save_storm_config); this does
    not insert. Returns True if a row was updated. Tab name fields are stored
    verbatim — pass '' to fall back to the event-type-aware default at read.

    Auto-scheduler fields (Rule H / #164):
      * poll_day_of_week — 0=Monday..6=Sunday in the guild's tz. The
        day the poll-up post fires. Event day is game-defined (DS = Friday,
        CS = Thursday). Pass -1 (default) when auto-scheduling is
        intentionally off; the scheduler loop treats `< 0` as "skip
        this guild."
      * signup_time       — HH:MM in the guild's tz when to fire.
        Empty string disables auto-fire (manual post_signup under the
        parent group remains usable).
    """
    if sub_mode not in ("pool", "paired"):
        sub_mode = "pool"
    # Poll day-of-week is either 0-6 or "not configured" (-1). Reject
    # anything else rather than silently clipping — the wizard should
    # catch bad input first.
    try:
        dow = int(poll_day_of_week)
    except (TypeError, ValueError):
        dow = -1
    if not (-1 <= dow <= 6):
        dow = -1
    # Normalise the Power Data Source fields. Empty tab persists as
    # "use Member Roster"; empty match column persists as "use Member
    # Roster discord_id_col". Anything not A-Z gets coerced to empty
    # so a bad input falls back to the safe default instead of being
    # saved verbatim.
    pm_tab = (power_metric_tab or "").strip()
    pm_match = (power_match_column or "").strip().upper()
    if not (len(pm_match) == 1 and "A" <= pm_match <= "Z"):
        pm_match = ""
    # Stale-power DM nudge (#255). Same letter-validation as power.
    lu_tab = (power_last_updated_tab or "").strip()
    lu_col = (power_last_updated_column or "").strip().upper()
    if not (len(lu_col) == 1 and "A" <= lu_col <= "Z"):
        lu_col = ""
    lu_match = (power_last_updated_match_column or "").strip().upper()
    if not (len(lu_match) == 1 and "A" <= lu_match <= "Z"):
        lu_match = ""
    try:
        stale_days = int(power_refresh_stale_days)
    except (TypeError, ValueError):
        stale_days = 0
    if stale_days < 0:
        stale_days = 0
    if stale_days > 365:
        stale_days = 365
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE guild_storm_config SET "
            "  structured_flow_enabled = ?, "
            "  power_metric_column = ?, "
            "  power_metric_tab = ?, "
            "  power_match_column = ?, "
            "  sub_mode = ?, "
            "  signup_channel_id = ?, "
            "  signup_schedule_cron = ?, "
            "  signups_tab = ?, "
            "  rosters_tab = ?, "
            "  attendance_tab = ?, "
            "  strategies_tab = ?, "
            "  member_rules_tab = ?, "
            "  poll_day_of_week = ?, "
            "  signup_time = ?, "
            "  power_refresh_dm_enabled = ?, "
            "  power_last_updated_tab = ?, "
            "  power_last_updated_column = ?, "
            "  power_last_updated_match_column = ?, "
            "  power_refresh_stale_days = ? "
            "WHERE guild_id = ? AND event_type = ?",
            (
                1 if structured_flow_enabled else 0,
                (power_metric_column or "B").strip().upper()[:1] or "B",
                pm_tab,
                pm_match,
                sub_mode,
                int(signup_channel_id or 0), signup_schedule_cron,
                signups_tab, rosters_tab, attendance_tab,
                strategies_tab, member_rules_tab,
                dow, signup_time,
                1 if power_refresh_dm_enabled else 0,
                lu_tab, lu_col, lu_match, stale_days,
                guild_id, event_type,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


# ── Storm sign-up votes (#123) ───────────────────────────────────────────────
#
# Votes captured from the SignupView buttons (and from the `signups` officer
# view's on-behalf path) UPSERT into `storm_signups`. The View itself imports these
# helpers from the click handler; the officer view also reads from here.

_VALID_STORM_VOTES = {"a", "b", "either", "cannot"}


def _utcnow_iso() -> str:
    """Tz-aware UTC timestamp, ISO 8601 seconds precision, with `+00:00`
    suffix. Replaces the deprecated naive `datetime.utcnow()` so consumers
    can't accidentally interpret stored timestamps as local time."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def record_storm_vote(
    guild_id: int,
    event_type: str,
    event_date: str,
    voter_user_id: int,
    target_member_id: str,
    vote: str,
    *,
    is_on_behalf: bool = False,
    channel_id: int = 0,
    message_id: int = 0,
) -> bool:
    """UPSERT a vote into storm_signups and append the same vote to the
    append-only `storm_signup_history` table for audit. Re-votes on the
    same (guild_id, event_type, event_date, target_member_id) replace
    the prior row in `storm_signups` but preserve every prior vote in
    `storm_signup_history`. Returns True if recorded, False if the vote
    value is invalid."""
    if vote not in _VALID_STORM_VOTES:
        return False
    voted_at = _utcnow_iso()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO storm_signups "
            "(guild_id, event_type, event_date, target_member_id, "
            " voter_user_id, vote, is_on_behalf, channel_id, message_id, voted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, event_type, event_date, target_member_id) "
            "DO UPDATE SET "
            "  voter_user_id = excluded.voter_user_id, "
            "  vote          = excluded.vote, "
            "  is_on_behalf  = excluded.is_on_behalf, "
            "  channel_id    = excluded.channel_id, "
            "  message_id    = excluded.message_id, "
            "  voted_at      = excluded.voted_at",
            (
                int(guild_id), event_type, event_date, target_member_id,
                int(voter_user_id), vote, 1 if is_on_behalf else 0,
                int(channel_id or 0), int(message_id or 0), voted_at,
            ),
        )
        conn.execute(
            "INSERT INTO storm_signup_history "
            "(guild_id, event_type, event_date, target_member_id, "
            " voter_user_id, vote, is_on_behalf, voted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(guild_id), event_type, event_date, target_member_id,
                int(voter_user_id), vote, 1 if is_on_behalf else 0,
                voted_at,
            ),
        )
        conn.commit()
    return True


def get_storm_signup_history(
    guild_id: int, event_type: str, event_date: str,
    target_member_id: str | None = None,
) -> list[dict]:
    """Return every recorded vote for an event (or a single target),
    newest first. Lets the officer view surface "Alice voted A at T1,
    then officer X overrode to B at T2" so re-votes don't lose context."""
    with _get_conn() as conn:
        if target_member_id is None:
            rows = conn.execute(
                "SELECT id, guild_id, event_type, event_date, target_member_id, "
                "       voter_user_id, vote, is_on_behalf, voted_at "
                "FROM storm_signup_history "
                "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
                "ORDER BY id DESC",
                (int(guild_id), event_type, event_date),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, guild_id, event_type, event_date, target_member_id, "
                "       voter_user_id, vote, is_on_behalf, voted_at "
                "FROM storm_signup_history "
                "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
                "  AND target_member_id = ? "
                "ORDER BY id DESC",
                (int(guild_id), event_type, event_date, target_member_id),
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["is_on_behalf"] = bool(d.get("is_on_behalf"))
        out.append(d)
    return out


def get_storm_signups(guild_id: int, event_type: str, event_date: str) -> list[dict]:
    """Return all vote rows for a given event."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT guild_id, event_type, event_date, target_member_id, "
            "       voter_user_id, vote, is_on_behalf, channel_id, message_id, voted_at "
            "FROM storm_signups "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ?",
            (int(guild_id), event_type, event_date),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["is_on_behalf"] = bool(d.get("is_on_behalf"))
        out.append(d)
    return out


def get_member_vote(
    guild_id: int, event_type: str, event_date: str, target_member_id: str,
) -> dict | None:
    """Return a single member's vote row, or None if they haven't voted."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT guild_id, event_type, event_date, target_member_id, "
            "       voter_user_id, vote, is_on_behalf, channel_id, message_id, voted_at "
            "FROM storm_signups "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
            "  AND target_member_id = ?",
            (int(guild_id), event_type, event_date, target_member_id),
        ).fetchone()
    if not row:
        return None
    d = dict(row)
    d["is_on_behalf"] = bool(d.get("is_on_behalf"))
    return d


# ── Storm team plans (#239) ──────────────────────────────────────────────────
#
# Per-team, per-event record of the 30 players the officer committed to in
# Last War (20 primaries + 10 subs). Read by the roster builder at open and
# auto-fill time so the bot mirrors the in-game submission instead of
# re-deriving a starter/sub split that may contradict it.
#
# Validation lives in `save_storm_team_plan` (returns `(ok, errors)`) so the
# UI can surface conflicts inline. The `UNIQUE INDEX (guild, event_type,
# event_date, target_member_id)` defined in `init_db` is the belt-and-braces
# backstop for the one-team-per-member rule if the validator is bypassed.

STORM_PLAN_MAX_PRIMARIES = 20
STORM_PLAN_MAX_SUBS = 10
STORM_PLAN_MAX_TOTAL = STORM_PLAN_MAX_PRIMARIES + STORM_PLAN_MAX_SUBS
_VALID_STORM_PLAN_ROLES = {"primary", "sub"}


def save_storm_team_plan(
    guild_id: int,
    event_type: str,
    event_date: str,
    team: str,
    primaries: list[str],
    subs: list[str],
    saved_by_user_id: int,
) -> tuple[bool, list[str]]:
    """Atomic replace of the team plan for one (guild, event, team).

    DELETE-then-bulk-INSERT inside a single transaction. Validates:
    * No member appears in both `primaries` and `subs`.
    * `primaries` ≤ 20, `subs` ≤ 10, total ≤ 30.
    * No member listed is already on the *other* team's plan for the
      same event (one-team-per-member, matching the in-game rule that
      a submitted team can't be moved).
    * `role` values are restricted to `{"primary", "sub"}` by construction.

    Returns `(True, [])` on success, `(False, errors)` otherwise. Errors
    are short human-readable strings the UI can surface verbatim.
    """
    errors: list[str] = []
    primaries = [str(m) for m in primaries]
    subs = [str(m) for m in subs]

    primary_set = set(primaries)
    sub_set = set(subs)
    overlap = primary_set & sub_set
    if overlap:
        errors.append(
            "Members listed as both primary and sub: " + ", ".join(sorted(overlap))
        )
    if len(primaries) > STORM_PLAN_MAX_PRIMARIES:
        errors.append(
            f"Too many primaries ({len(primaries)}); max is "
            f"{STORM_PLAN_MAX_PRIMARIES}."
        )
    if len(subs) > STORM_PLAN_MAX_SUBS:
        errors.append(
            f"Too many subs ({len(subs)}); max is {STORM_PLAN_MAX_SUBS}."
        )
    total = len(primary_set | sub_set)
    if total > STORM_PLAN_MAX_TOTAL:
        errors.append(
            f"Total ({total}) exceeds {STORM_PLAN_MAX_TOTAL} per team."
        )
    if errors:
        return False, errors

    incoming = primary_set | sub_set
    with _get_conn() as conn:
        # Cross-team conflict check: anyone on the OTHER team's plan for
        # this same event blocks the save.
        if incoming:
            placeholders = ",".join("?" for _ in incoming)
            rows = conn.execute(
                f"SELECT target_member_id FROM storm_team_plans "
                f"WHERE guild_id = ? AND event_type = ? AND event_date = ? "
                f"  AND team != ? "
                f"  AND target_member_id IN ({placeholders})",
                (
                    int(guild_id), event_type, event_date, team,
                    *sorted(incoming),
                ),
            ).fetchall()
            conflicts = sorted({r["target_member_id"] for r in rows})
            if conflicts:
                errors.append(
                    "Already on the other team for this event: "
                    + ", ".join(conflicts)
                )
                return False, errors

        saved_at = _utcnow_iso()
        try:
            conn.execute(
                "DELETE FROM storm_team_plans "
                "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
                "  AND team = ?",
                (int(guild_id), event_type, event_date, team),
            )
            rows_to_insert = [
                (int(guild_id), event_type, event_date, team, m,
                 "primary", int(saved_by_user_id), saved_at)
                for m in primaries
            ] + [
                (int(guild_id), event_type, event_date, team, m,
                 "sub", int(saved_by_user_id), saved_at)
                for m in subs
            ]
            if rows_to_insert:
                conn.executemany(
                    "INSERT INTO storm_team_plans "
                    "(guild_id, event_type, event_date, team, target_member_id, "
                    " role, saved_by_user_id, saved_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows_to_insert,
                )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            return False, [f"Database rejected the plan: {exc}"]
    return True, []


def get_storm_team_plan(
    guild_id: int, event_type: str, event_date: str, team: str,
) -> dict | None:
    """Return the saved team plan or None if no rows exist for this
    (guild, event_type, event_date, team). Shape:
        {"primaries": list[str], "subs": list[str],
         "saved_by_user_id": int, "saved_at": str}
    """
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT target_member_id, role, saved_by_user_id, saved_at "
            "FROM storm_team_plans "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
            "  AND team = ?",
            (int(guild_id), event_type, event_date, team),
        ).fetchall()
    if not rows:
        return None
    primaries: list[str] = []
    subs: list[str] = []
    saved_by_user_id = 0
    saved_at = ""
    for r in rows:
        if r["role"] == "primary":
            primaries.append(r["target_member_id"])
        elif r["role"] == "sub":
            subs.append(r["target_member_id"])
        saved_by_user_id = int(r["saved_by_user_id"])
        if r["saved_at"] > saved_at:
            saved_at = r["saved_at"]
    return {
        "primaries": sorted(primaries),
        "subs": sorted(subs),
        "saved_by_user_id": saved_by_user_id,
        "saved_at": saved_at,
    }


def get_storm_team_plans_for_event(
    guild_id: int, event_type: str, event_date: str,
) -> dict[str, dict]:
    """Return a `{team: plan_dict}` map covering every team that has a
    plan for this event. Cheaper than two `get_storm_team_plan` calls
    when the picker needs to know what the other team has claimed."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT team, target_member_id, role, saved_by_user_id, saved_at "
            "FROM storm_team_plans "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ?",
            (int(guild_id), event_type, event_date),
        ).fetchall()
    plans: dict[str, dict] = {}
    for r in rows:
        team = r["team"]
        p = plans.setdefault(
            team,
            {"primaries": [], "subs": [],
             "saved_by_user_id": 0, "saved_at": ""},
        )
        if r["role"] == "primary":
            p["primaries"].append(r["target_member_id"])
        elif r["role"] == "sub":
            p["subs"].append(r["target_member_id"])
        p["saved_by_user_id"] = int(r["saved_by_user_id"])
        if r["saved_at"] > p["saved_at"]:
            p["saved_at"] = r["saved_at"]
    for p in plans.values():
        p["primaries"] = sorted(p["primaries"])
        p["subs"] = sorted(p["subs"])
    return plans


def clear_storm_team_plan(
    guild_id: int, event_type: str, event_date: str, team: str,
) -> int:
    """Delete the saved plan for one team. Returns the number of rows
    removed (0 if nothing was saved)."""
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM storm_team_plans "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
            "  AND team = ?",
            (int(guild_id), event_type, event_date, team),
        )
        conn.commit()
        return cur.rowcount


# ── Storm roster drafts (#240) ────────────────────────────────────────────────
#
# Per-(guild, event_type, team) snapshot of the structured roster
# builder's in-progress state. One row per team; reusable across
# event weeks. Saves the officer's intent (zone assignments, sub
# pairings, overrides, selected preset) — NOT the member dict
# (re-read from team plan / signups at load time). See #240's
# design comment for the full contract.


def save_roster_draft(
    guild_id: int,
    event_type: str,
    team: str,
    *,
    session_json: str,
    event_date: str,
) -> None:
    """Persist (or overwrite) the roster draft for one team. Called
    by the builder's auto-save hook on every state change. Idempotent
    upsert keyed by (guild_id, event_type, team) so one team only
    ever has one draft row."""
    from datetime import datetime, timezone as _tz
    updated_at = datetime.now(_tz.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO storm_roster_drafts "
            "(guild_id, event_type, team, session_json, event_date, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(guild_id, event_type, team) DO UPDATE SET "
            "  session_json = excluded.session_json, "
            "  event_date   = excluded.event_date, "
            "  updated_at   = excluded.updated_at",
            (int(guild_id), event_type, team, session_json,
             event_date, updated_at),
        )
        conn.commit()


def get_roster_draft(
    guild_id: int, event_type: str, team: str,
) -> Optional[dict]:
    """Return the saved roster draft for one team, or None if no row
    exists. Caller deserializes `session_json` and reconciles against
    the current team plan / signups at builder open."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT session_json, event_date, updated_at "
            "FROM storm_roster_drafts "
            "WHERE guild_id = ? AND event_type = ? AND team = ?",
            (int(guild_id), event_type, team),
        ).fetchone()
    if row is None:
        return None
    return {
        "session_json": row["session_json"],
        "event_date":   row["event_date"],
        "updated_at":   row["updated_at"],
    }


def delete_roster_draft(
    guild_id: int, event_type: str, team: str,
) -> int:
    """Delete the saved draft for one team. Returns the rowcount (0
    if nothing was saved). Called when the officer confirms
    🆕 Set up new — the draft is cleared and a fresh builder opens."""
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM storm_roster_drafts "
            "WHERE guild_id = ? AND event_type = ? AND team = ?",
            (int(guild_id), event_type, team),
        )
        conn.commit()
        return cur.rowcount


# ── Storm registration posts (#123, written by #124) ─────────────────────────


def record_storm_registration_post(
    guild_id: int, event_type: str, event_date: str,
    channel_id: int, message_id: int,
    *,
    time_a_label: str = "",
    time_b_label: str = "",
    team_a_slot_index: int = 0,
    team_b_slot_index: int = 0,
) -> bool:
    """Record a freshly-posted sign-up message. Always inserts a new row
    (#265) — multiple posts per event are supported so leadership can
    re-post a fresh sign-up when the original gets lost in channel
    chatter. Votes still aggregate to the same event via the
    (guild_id, event_type, event_date) key on storm_signups.

    Returns True on successful insert. (Errors raise; the bool return is
    preserved for callers that previously branched on the idempotent
    no-op.)

    `team_a_slot_index` / `team_b_slot_index` capture the team→slot
    mapping (#251) in effect when this specific post went out — either
    the guild default or a one-week override the officer picked. 0
    means "legacy / unknown" (rows written before #251).
    """
    posted_at = _utcnow_iso()
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO storm_registration_posts "
            "(guild_id, event_type, event_date, channel_id, message_id, "
            " time_a_label, time_b_label, team_a_slot_index, team_b_slot_index, posted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                int(guild_id), event_type, event_date,
                int(channel_id), int(message_id),
                time_a_label, time_b_label,
                int(team_a_slot_index or 0), int(team_b_slot_index or 0),
                posted_at,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def has_registration_post(guild_id: int, event_type: str, event_date: str) -> bool:
    """Whether a registration message has already been posted for this event."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM storm_registration_posts "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ?",
            (int(guild_id), event_type, event_date),
        ).fetchone()
    return row is not None


def get_recent_storm_registration_posts(within_days: int = 14) -> list[dict]:
    """Return all registration posts whose event_date is within the last
    `within_days` days. Used by the bot startup hook to re-register the
    SignupView for messages that are still "live".

    Bounded against UTC today so the Railway host's local clock can't
    drift the cutoff out from under non-UTC alliances.
    """
    import datetime as _dt
    today_utc = _dt.datetime.now(_dt.timezone.utc).date()
    cutoff = (today_utc - _dt.timedelta(days=within_days)).isoformat()
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT guild_id, event_type, event_date, channel_id, message_id, "
            "       time_a_label, time_b_label, team_a_slot_index, team_b_slot_index, "
            "       posted_at "
            "FROM storm_registration_posts "
            "WHERE event_date >= ?",
            (cutoff,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_storm_registration_post(
    guild_id: int, event_type: str, event_date: str,
) -> dict | None:
    """Return the LATEST storm_registration_posts row for (guild,
    event_type, event_date), or None if no post has been recorded.

    Post-#265 multiple rows can exist for the same event when leadership
    re-posts. The latest by `posted_at` wins because slot mapping should
    be identical across reposts of the same event (resolved from the
    same guild config / override), so any row's mapping is correct —
    picking the newest is the safest default for any future caller that
    might also want the freshest channel_id / message_id.

    Used by attendance / mail-rendering paths that need to know the
    team→slot mapping (#251) that was actually in effect when the
    sign-up post went out, not the current guild default. Returns the
    full row as a dict including `time_a_label`, `time_b_label`,
    `team_a_slot_index`, `team_b_slot_index`.
    """
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM storm_registration_posts "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
            "ORDER BY posted_at DESC LIMIT 1",
            (int(guild_id), event_type, event_date),
        ).fetchone()
    return dict(row) if row else None


# ── Saved roster-image pointers (#140 follow-up) ────────────────────────────


def save_roster_image_ref(
    guild_id: int, event_type: str, event_date: str, team: str,
    channel_id: int, message_id: int, user_id: int,
) -> None:
    """UPSERT the (channel_id, message_id) of a freshly-posted roster
    image. Composite key (guild, event_type, event_date, team) — DS has
    one image per team (A / B), CS uses empty team string for its
    single-roster shape. Officer re-renders and re-saves overwrite
    the prior pointer."""
    posted_at = _utcnow_iso()
    with _get_conn() as conn:
        conn.execute(
            "INSERT INTO storm_roster_images "
            "(guild_id, event_type, event_date, team, channel_id, "
            " message_id, posted_by_user_id, posted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (guild_id, event_type, event_date, team) DO UPDATE "
            "SET channel_id = excluded.channel_id, "
            "    message_id = excluded.message_id, "
            "    posted_by_user_id = excluded.posted_by_user_id, "
            "    posted_at = excluded.posted_at",
            (
                int(guild_id), event_type, event_date, team or "",
                int(channel_id), int(message_id), int(user_id), posted_at,
            ),
        )
        conn.commit()


def list_roster_image_refs(
    guild_id: int, event_type: str, event_date: str,
) -> list[dict]:
    """All saved roster-image pointers for a (guild, event) — usually
    one for CS, up to two (Team A + Team B) for DS. Empty list if no
    `💾 Save to history` clicks have been recorded for this event.
    Ordered so DS Team A renders before Team B in the history view."""
    with _get_conn() as conn:
        rows = conn.execute(
            "SELECT guild_id, event_type, event_date, team, channel_id, "
            "       message_id, posted_by_user_id, posted_at "
            "FROM storm_roster_images "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
            "ORDER BY team",
            (int(guild_id), event_type, event_date),
        ).fetchall()
    return [dict(r) for r in rows]


def delete_roster_image_ref(
    guild_id: int, event_type: str, event_date: str, team: str,
) -> bool:
    """Remove a saved pointer (e.g. when the click-time fetch finds the
    message was deleted and the officer asks to unlink). Returns True
    if a row was deleted."""
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM storm_roster_images "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
            "  AND team = ?",
            (int(guild_id), event_type, event_date, team or ""),
        )
        conn.commit()
        return cur.rowcount > 0


# ── Power-refresh DM cooldown (#138) ────────────────────────────────────────


def has_power_refresh_dm_been_sent(
    guild_id: int, event_type: str, event_date: str, voter_user_id: int,
) -> bool:
    """True if the bot has already sent the power-refresh nudge to this
    voter for this event. Used by the SignupView click handler to
    cap at one nudge per (member, event_date) regardless of re-votes
    or bot restarts."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM storm_power_refresh_dms_sent "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
            "  AND voter_user_id = ?",
            (int(guild_id), event_type, event_date, int(voter_user_id)),
        ).fetchone()
    return row is not None


def record_power_refresh_dm_sent(
    guild_id: int, event_type: str, event_date: str, voter_user_id: int,
) -> bool:
    """Idempotent record of "this voter got a power-refresh DM for
    this event." Returns True on a fresh insert, False if already
    recorded — caller can use either return as the cooldown gate.

    `INSERT OR IGNORE` + the `rowcount > 0` return is what makes this
    a race-tight cooldown: callers should insert FIRST, then send
    the DM only on a True return. Two simultaneous click handlers
    each call this; only the first sees True, the second sees False
    and bails — so the DM fires exactly once.
    """
    with _get_conn() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO storm_power_refresh_dms_sent "
            "(guild_id, event_type, event_date, voter_user_id, sent_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                int(guild_id), event_type, event_date,
                int(voter_user_id), _utcnow_iso(),
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def clear_power_refresh_dm_sent(
    guild_id: int, event_type: str, event_date: str, voter_user_id: int,
) -> bool:
    """Back out a `record_power_refresh_dm_sent` row. Used by the
    click handler when a transient `discord.HTTPException` blew up
    the DM send AFTER the cooldown was claimed via INSERT-first —
    without backing it out, the member would never get a retry for
    this event because the cooldown row would persist.

    Returns True if a row was removed; False if nothing matched.
    """
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM storm_power_refresh_dms_sent "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
            "  AND voter_user_id = ?",
            (int(guild_id), event_type, event_date, int(voter_user_id)),
        )
        conn.commit()
        return cur.rowcount > 0


# ── Walkthrough dismissals (#130) ────────────────────────────────────────────


def is_walkthrough_dismissed(
    guild_id: int, user_id: int, walkthrough_key: str,
) -> bool:
    """True if this officer has already seen (or declined) the named
    walkthrough in this guild. Walkthrough keys carry a version suffix
    (e.g. `storm_signups_v1`) so a major UI rewrite can re-offer the
    tour without losing per-officer dismissal records."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM walkthrough_dismissals "
            "WHERE guild_id = ? AND user_id = ? AND walkthrough_key = ?",
            (int(guild_id), int(user_id), walkthrough_key),
        ).fetchone()
    return row is not None


def dismiss_walkthrough(
    guild_id: int, user_id: int, walkthrough_key: str,
) -> None:
    """Record that an officer has seen or declined a walkthrough.
    Idempotent — re-recording is a no-op."""
    with _get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO walkthrough_dismissals "
            "(guild_id, user_id, walkthrough_key, dismissed_at) "
            "VALUES (?, ?, ?, ?)",
            (int(guild_id), int(user_id), walkthrough_key, _utcnow_iso()),
        )
        conn.commit()


# ── Structured roster-build session lock (#129 follow-up) ────────────────────
#
# Used by `storm_roster_builder` to make sure two officers can't
# independently build the same team for the same event and both Approve
# (which would post two mails + write two sets of rosters_tab rows).
# The session row is keyed on (guild_id, event_type, event_date, team).
# `team` is `"A"` / `"B"` when the alliance configured `teams=both` (DS
# or CS), and the configured single team (`"A"` or `"B"`) when
# `teams=A` / `teams=B`. Legacy CS rosters from before Rule A / #166
# may carry `""` (single-roster era).


def claim_storm_session(
    guild_id: int, event_type: str, event_date: str, team: str,
    user_id: int,
) -> tuple[bool, Optional[int]]:
    """Try to claim the build slot for this (guild, event_type, event_date,
    team). Returns `(True, None)` on a fresh claim, `(False, owner_user_id)`
    if another officer already holds it.

    Re-claiming as the same `user_id` succeeds (returns `(True, None)`) so
    an officer who opens the builder twice in a row from a stale view
    doesn't get locked out by their own prior claim.
    """
    with _get_conn() as conn:
        # Check existing holder first.
        row = conn.execute(
            "SELECT user_id FROM storm_session_state "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
            "  AND team = ?",
            (int(guild_id), event_type, event_date, team),
        ).fetchone()
        if row is not None:
            existing = int(row["user_id"])
            if existing == int(user_id):
                # Reclaim by the same officer — refresh opened_at and ok.
                conn.execute(
                    "UPDATE storm_session_state SET opened_at = ? "
                    "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
                    "  AND team = ?",
                    (_utcnow_iso(), int(guild_id), event_type, event_date, team),
                )
                conn.commit()
                return True, None
            return False, existing
        conn.execute(
            "INSERT INTO storm_session_state "
            "(guild_id, event_type, event_date, team, user_id, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (int(guild_id), event_type, event_date, team, int(user_id), _utcnow_iso()),
        )
        conn.commit()
    return True, None


def release_storm_session(
    guild_id: int, event_type: str, event_date: str, team: str,
) -> bool:
    """Drop the session lock. Safe to call on Done / Cancel / Approve /
    timeout — re-releasing is a no-op."""
    with _get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM storm_session_state "
            "WHERE guild_id = ? AND event_type = ? AND event_date = ? "
            "  AND team = ?",
            (int(guild_id), event_type, event_date, team),
        )
        conn.commit()
    return cur.rowcount > 0


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


def get_roster_dm_templates(guild_id: int, event_type: str) -> dict:
    """Return the per-role DM templates for the Approve & Post
    DM-the-roster flow (#226 follow-up).

    Returns a `{"starter": str, "paired_sub": str, "pool_sub": str}`
    dict; each value is the alliance's saved template body, or an
    empty string when the slot has never been customised. The DM
    composer falls back to `defaults.DEFAULT_ROSTER_DM_*` on empty
    so a guild that hasn't run the wizard's DM template step still
    gets sensible copy.
    """
    cfg = get_storm_config(guild_id, event_type)
    return {
        "starter":    (cfg.get("roster_dm_starter_template")    or ""),
        "paired_sub": (cfg.get("roster_dm_paired_sub_template") or ""),
        "pool_sub":   (cfg.get("roster_dm_pool_sub_template")   or ""),
    }


def save_roster_dm_templates(
    guild_id: int, event_type: str, *,
    starter: str = "",
    paired_sub: str = "",
    pool_sub: str = "",
) -> bool:
    """UPDATE the three roster DM templates on an existing
    (guild_id, event_type) row. Empty strings persist as "fall back
    to the hardcoded default" — the wizard's Use-default branch
    passes empty deliberately so the alliance can revert to a
    bot-updated default later without re-pasting.
    """
    with _get_conn() as conn:
        cur = conn.execute(
            "UPDATE guild_storm_config SET "
            "  roster_dm_starter_template    = ?, "
            "  roster_dm_paired_sub_template = ?, "
            "  roster_dm_pool_sub_template   = ?  "
            "WHERE guild_id = ? AND event_type = ?",
            (starter or "", paired_sub or "", pool_sub or "",
             guild_id, event_type),
        )
        conn.commit()
        return cur.rowcount > 0


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
    The row must already exist (created by the Desert Storm setup wizard or
    the Canyon Storm setup wizard via save_storm_config); this UPDATE does not insert.
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
    have run `the growth setup wizard` at least once. `get_growth_config` returns
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
    growth metrics anyway, and `the growth-breakdown setup wizard` blocks when
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
    i.e. they have walked `the growth-breakdown setup wizard` at least once and
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
    run `the growth setup wizard` first).

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
    have run `the survey setup wizard` for the main survey at least once. The
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
    have run `the birthday setup wizard` at least once. `get_birthday_config`
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
    `the birthday setup wizard` first for the row to exist, in which case the
    caller's `bcfg.get("train_integration")` check has already passed
    so we know the row is present."""
    with _get_conn() as conn:
        conn.execute(
            "UPDATE guild_birthday_config SET last_train_population_date = ? "
            "WHERE guild_id = ?",
            (date_iso, guild_id),
        )
        conn.commit()


def has_member_roster_config(guild_id: int) -> bool:
    """True iff the guild has a row in `guild_member_roster_config` —
    i.e. they have run `the Member Roster setup wizard` at least once.
    `get_member_roster_config` returns a fallback dict on miss, so it
    can't distinguish "saved with all defaults" from "never
    configured"; this helper exists for the setup-wizard summary embed
    gate."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM guild_member_roster_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return row is not None


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
    have run `the train setup wizard` at least once. `get_train_config` returns
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

def has_shiny_tasks_config(guild_id: int) -> bool:
    """True iff the guild has a row in `guild_shiny_tasks_config` — i.e.
    they have run `the Shiny Tasks setup wizard` at least once.
    `get_shiny_tasks_config` returns a fallback dict on miss, so it
    can't distinguish "saved with all defaults" from "never configured";
    this helper exists for the setup-wizard summary embed and the
    disable-with-clear gate (#101)."""
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM guild_shiny_tasks_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    return row is not None


def clear_shiny_tasks_config(guild_id: int) -> None:
    """Delete the guild's shiny-tasks config row entirely. Called by the
    `ask_disable_with_clear` Clear button after the user disables the
    daily announcement. `get_shiny_tasks_config` already returns a
    default dict when the row is absent, so deletion is the cleanest
    reset."""
    with _get_conn() as conn:
        conn.execute(
            "DELETE FROM guild_shiny_tasks_config WHERE guild_id = ?",
            (guild_id,),
        )
        conn.commit()


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

