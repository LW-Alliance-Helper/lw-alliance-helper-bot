"""
config_export.py — Serialize per-guild bot configuration to JSON for
migration to a new Discord server (or backup/restore in the same guild).

The export is intentionally narrow: it captures *what the alliance built*
(events, templates, surveys, settings) but never *data about members*
(roster contents, birthday list, growth snapshots, participation history),
since that data lives in the alliance's Google Sheet and travels with the
sheet, not with the bot.

JSON shape — see ``parse_and_validate`` for the authoritative layout. The
top-level ``categories`` array lists which sections appear in ``data``.
Each section renders either as a single object (one-row tables) or as a
list of objects (multi-row tables — events, DS/CS sub-rows, named
surveys). Per-section keys:

  * ``travels`` — alliance-defined values that move as-is.
  * ``remap_channels`` — Discord channel/thread IDs that need a new pick
    in the destination guild.
  * ``remap_roles`` — Discord role IDs that need a new pick.

Each remap entry carries a ``purpose`` string (e.g. "Leadership channel")
so the import wizard can tell the importer exactly what they're remapping
without surfacing raw column names.

This module deliberately has no Discord imports — channel/role display
name lookup is injected as a callable at export time, and the Discord
wizard UX lives in ``export_import_cog.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from setup_hub import (
    HUB_BTN_CS,
    HUB_BTN_DS,
    HUB_BTN_EVENTS,
    HUB_BTN_GROWTH,
    HUB_BTN_SHINY,
    HUB_BTN_TRAIN,
)


EXPORT_SCHEMA_VERSION = 1

# Categories the user can pick from when exporting. The display order here
# is also the order they're rendered in the multi-select.
CATEGORY_ORDER: list[str] = [
    "core",
    "events",
    "ds",
    "cs",
    "train",
    "birthday",
    "growth",
    "surveys",
    "shiny_tasks",
    "member_roster",
]

CATEGORY_LABELS: dict[str, str] = {
    "core": "⚙️ Core setup (roles, timezone, sheet ID)",
    "events": HUB_BTN_EVENTS,
    "ds": f"{HUB_BTN_DS} (templates, channels, participation)",
    "cs": f"{HUB_BTN_CS} (templates, channels, participation)",
    "train": f"{HUB_BTN_TRAIN} (templates, reminders)",
    "birthday": "🎂 Birthday tracking",
    "growth": f"{HUB_BTN_GROWTH} tracking (incl. breakdown)",
    "surveys": "📋 Surveys (default + Premium named)",
    "shiny_tasks": f"{HUB_BTN_SHINY} (daily announcement)",
    "member_roster": "💎 Member Roster sync (Premium)",
}


# ── Field labels: human-readable "purpose" strings for every remap field ────
# Single source of truth — collectors below reference these keys, and the
# import wizard groups by source ID and prints `purpose` to the user.

FIELD_PURPOSES: dict[str, str] = {
    # Core (guild_configs)
    "core.leadership_channel_id": "Leadership channel",
    "core.announcement_channel_id": "Alliance announcements channel",
    "core.survey_channel_id": "Default survey post channel",
    "core.survey_notify_channel_id": "Survey submission notifications channel",
    "core.ds_log_channel_id": "Desert Storm log channel",
    "core.cs_log_channel_id": "Canyon Storm log channel",
    "core.event_draft_channel_id": "Event editor channel",
    "core.event_announce_channel_id": "Event announcement channel",
    "core.member_role_id": "Member role",
    # Events (per-event)
    "events.draft_channel_id": "Event '{name}' draft channel",
    "events.announcement_channel_id": "Event '{name}' announcement channel",
    # Storm (per event_type DS / DS_A / DS_B / CS / CS_A / CS_B)
    "storm.log_channel_id": "{event_label} log channel",
    "storm.post_channel_id": "{event_label} mail post channel",
    # Train
    "train.reminder_channel_id": "Train reminder channel",
    # Birthday
    "birthday.reminder_channel_id": "Birthday reminder channel",
    # Growth
    "growth.breakdown_post_channel_id": "Growth Breakdown auto-post channel",
    # Shiny Tasks
    "shiny_tasks.channel_id": "Shiny Tasks announcement channel",
    # Surveys (default + extras)
    "surveys.survey_channel_id": "Survey '{name}' post channel",
    "surveys.notify_channel_id": "Survey '{name}' notifications channel",
    "surveys.reminder_channel_id": "Survey '{name}' reminder channel",
    # Member Roster
    "member_roster.role_filter_id": "Member roster filter role",
}


# Display labels for the storm sub-rows. The bot stores 6 logical rows
# per guild for storm: DS / DS_A / DS_B / CS / CS_A / CS_B. Each row has
# its own log and post channels, but the channels usually match across
# the three rows for a given event type. Showing "(Team A)" / "(Team B)"
# in the purpose label makes it clear which row is being remapped.
STORM_ROW_LABELS: dict[str, str] = {
    "DS": "Desert Storm (shared)",
    "DS_A": "Desert Storm Team A",
    "DS_B": "Desert Storm Team B",
    "CS": "Canyon Storm (shared)",
    "CS_A": "Canyon Storm Team A",
    "CS_B": "Canyon Storm Team B",
}


# ── Helpers ─────────────────────────────────────────────────────────────────


@dataclass
class _RemapField:
    """Internal representation of a single remap entry before serialization."""

    field: str  # canonical column / dotted-path field name
    purpose: str  # human-readable label
    source_id: int  # the ID in the source guild
    source_name: str  # display name at export time (best-effort)


def _channel_remap(
    field: str,
    purpose_key: str,
    value: int,
    channel_lookup: Callable[[int], str],
    purpose_format: dict | None = None,
) -> dict | None:
    """Build a remap dict for a channel field. Returns ``None`` when the
    field is unset (``0``) so the export doesn't carry empty remaps."""
    if not value:
        return None
    purpose = FIELD_PURPOSES[purpose_key]
    if purpose_format:
        purpose = purpose.format(**purpose_format)
    return {
        "field": field,
        "purpose": purpose,
        "source_id": int(value),
        "source_name": channel_lookup(int(value)) or "",
    }


def _role_remap(
    field: str,
    purpose_key: str,
    value: int,
    role_lookup: Callable[[int], str],
    purpose_format: dict | None = None,
) -> dict | None:
    """Role analogue of `_channel_remap`."""
    if not value:
        return None
    purpose = FIELD_PURPOSES[purpose_key]
    if purpose_format:
        purpose = purpose.format(**purpose_format)
    return {
        "field": field,
        "purpose": purpose,
        "source_id": int(value),
        "source_name": role_lookup(int(value)) or "",
    }


# ── Per-category collectors ─────────────────────────────────────────────────
# Each collector returns either a dict (one-row category) or a list of
# dicts (multi-row category), or `None` if the guild has nothing in that
# category. The cog filters out `None` so empty categories don't appear
# in the export multi-select.


def collect_core(
    guild_id: int, *, channel_lookup: Callable[[int], str], role_lookup: Callable[[int], str]
) -> dict | None:
    """Collect the core ``guild_configs`` row. Returns None if the guild
    hasn't completed setup yet (nothing to export)."""
    from config import get_config

    cfg = get_config(guild_id)
    if not cfg or not cfg.setup_complete:
        return None

    travels = {
        "leadership_role_name": cfg.leadership_role_name,
        "member_role_name": cfg.member_role_name,
        "timezone": cfg.timezone,
        "spreadsheet_id": cfg.spreadsheet_id,
        "event_draft_time": cfg.event_draft_time,
        "event_five_min_warning": int(cfg.event_five_min_warning),
        "tab_train_schedule": cfg.tab_train_schedule,
        "tab_ds_assignments": cfg.tab_ds_assignments,
        "tab_sitouts": cfg.tab_sitouts,
        "tab_survey_history": cfg.tab_survey_history,
        "tab_member_default": cfg.tab_member_default,
    }

    channel_fields = [
        ("leadership_channel_id", cfg.leadership_channel_id, "core.leadership_channel_id"),
        ("announcement_channel_id", cfg.announcement_channel_id, "core.announcement_channel_id"),
        ("survey_channel_id", cfg.survey_channel_id, "core.survey_channel_id"),
        ("survey_notify_channel_id", cfg.survey_notify_channel_id, "core.survey_notify_channel_id"),
        ("ds_log_channel_id", cfg.ds_log_channel_id, "core.ds_log_channel_id"),
        ("cs_log_channel_id", cfg.cs_log_channel_id, "core.cs_log_channel_id"),
        ("event_draft_channel_id", cfg.event_draft_channel_id, "core.event_draft_channel_id"),
        (
            "event_announce_channel_id",
            cfg.event_announce_channel_id,
            "core.event_announce_channel_id",
        ),
    ]
    remap_channels = [
        entry
        for entry in (
            _channel_remap(field, key, value, channel_lookup)
            for field, value, key in channel_fields
        )
        if entry is not None
    ]

    remap_roles: list[dict] = []
    role_entry = _role_remap(
        "member_role_id", "core.member_role_id", cfg.member_role_id, role_lookup
    )
    if role_entry:
        remap_roles.append(role_entry)

    return {
        "travels": travels,
        "remap_channels": remap_channels,
        "remap_roles": remap_roles,
    }


def collect_events(
    guild_id: int, *, channel_lookup: Callable[[int], str], role_lookup: Callable[[int], str]
) -> list[dict] | None:
    """Collect every row from ``guild_events``. Returns ``None`` when the
    guild has no events configured."""
    from config import get_guild_events

    rows = get_guild_events(guild_id, active_only=False)
    if not rows:
        return None

    out: list[dict] = []
    for ev in rows:
        travels = {
            "short_key": ev["short_key"],
            "name": ev["name"],
            "timezone": ev["timezone"],
            "default_time": ev["default_time"],
            "announcement_blurb": ev["announcement_blurb"],
            "schedule_type": ev["schedule_type"],
            "anchor_date": ev["anchor_date"] or "",
            "interval_days": int(ev["interval_days"] or 0),
            "draft_time": ev["draft_time"],
            "five_min_warning": int(ev["five_min_warning"] or 0),
            "active": int(ev["active"] or 0),
        }
        fmt = {"name": ev["name"]}
        remap_channels = [
            entry
            for entry in (
                _channel_remap(
                    "draft_channel_id",
                    "events.draft_channel_id",
                    int(ev["draft_channel_id"] or 0),
                    channel_lookup,
                    purpose_format=fmt,
                ),
                _channel_remap(
                    "announcement_channel_id",
                    "events.announcement_channel_id",
                    int(ev["announcement_channel_id"] or 0),
                    channel_lookup,
                    purpose_format=fmt,
                ),
            )
            if entry is not None
        ]
        out.append(
            {
                "travels": travels,
                "remap_channels": remap_channels,
                "remap_roles": [],
            }
        )

    return out


def _collect_storm(
    guild_id: int,
    event_kind: str,
    *,
    channel_lookup: Callable[[int], str],
    role_lookup: Callable[[int], str],
) -> list[dict] | None:
    """Shared logic for the DS/CS categories. `event_kind` is ``"DS"`` or
    ``"CS"`` — the function picks up the shared row plus the two team rows
    via direct SQL because `get_storm_config` only returns one row at a
    time and ad-hoc reads are clearer here than calling it 3 times."""
    from config import _get_conn  # noqa: PLC0415 — module-private helper

    wanted = [event_kind, f"{event_kind}_A", f"{event_kind}_B"]
    placeholders = ",".join("?" for _ in wanted)
    with _get_conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM guild_storm_config "
            f"WHERE guild_id = ? AND event_type IN ({placeholders})",
            (guild_id, *wanted),
        ).fetchall()
    rows_by_type = {r["event_type"]: r for r in rows}
    if not rows:
        return None

    out: list[dict] = []
    for event_type in wanted:
        row = rows_by_type.get(event_type)
        if row is None:
            continue
        d = dict(row)
        label = STORM_ROW_LABELS.get(event_type, event_type)
        travels = {
            "event_type": event_type,
            "tab_name": d.get("tab_name") or "",
            "mail_template": d.get("mail_template") or "",
            "templates_json": d.get("templates_json") or "[]",
            "default_template": d.get("default_template") or "Default",
            "timezone": d.get("timezone") or "America/New_York",
            "dm_reminder_message": d.get("dm_reminder_message") or "",
            "participation_enabled": int(d.get("participation_enabled") or 0),
            "participation_tab_name": d.get("participation_tab_name") or "",
            "participation_questions": d.get("participation_questions") or "[]",
            "participation_roster_tab": d.get("participation_roster_tab") or "",
            "participation_roster_name_col": int(d.get("participation_roster_name_col") or 0),
            "participation_roster_alias_col": int(d.get("participation_roster_alias_col") or -1),
            "participation_roster_start_row": int(d.get("participation_roster_start_row") or 2),
        }
        fmt = {"event_label": label}
        remap_channels = [
            entry
            for entry in (
                _channel_remap(
                    "log_channel_id",
                    "storm.log_channel_id",
                    int(d.get("log_channel_id") or 0),
                    channel_lookup,
                    purpose_format=fmt,
                ),
                _channel_remap(
                    "post_channel_id",
                    "storm.post_channel_id",
                    int(d.get("post_channel_id") or 0),
                    channel_lookup,
                    purpose_format=fmt,
                ),
            )
            if entry is not None
        ]
        out.append(
            {
                "travels": travels,
                "remap_channels": remap_channels,
                "remap_roles": [],
            }
        )

    return out or None


def collect_ds(guild_id: int, **kw) -> list[dict] | None:
    return _collect_storm(guild_id, "DS", **kw)


def collect_cs(guild_id: int, **kw) -> list[dict] | None:
    return _collect_storm(guild_id, "CS", **kw)


def collect_train(
    guild_id: int, *, channel_lookup: Callable[[int], str], role_lookup: Callable[[int], str]
) -> dict | None:
    from config import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_train_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    travels = {
        "tab_name": d.get("tab_name") or "Train Schedule",
        "blurbs_enabled": int(d.get("blurbs_enabled") or 0),
        "themes": d.get("themes") or "",
        "tones": d.get("tones") or "",
        "prompt_template": d.get("prompt_template") or "",
        "templates_json": d.get("templates_json") or "[]",
        "default_template": d.get("default_template") or "Default",
        "default_tone": d.get("default_tone") or "",
        "reminders_enabled": int(d.get("reminders_enabled") or 0),
        "reminder_time": d.get("reminder_time") or "22:00",
        "dm_message": d.get("dm_message") or "",
    }
    remap_channels = [
        entry
        for entry in (
            _channel_remap(
                "reminder_channel_id",
                "train.reminder_channel_id",
                int(d.get("reminder_channel_id") or 0),
                channel_lookup,
            ),
        )
        if entry is not None
    ]
    return {
        "travels": travels,
        "remap_channels": remap_channels,
        "remap_roles": [],
    }


def collect_birthday(
    guild_id: int, *, channel_lookup: Callable[[int], str], role_lookup: Callable[[int], str]
) -> dict | None:
    from config import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_birthday_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    travels = {
        "tab_name": d.get("tab_name") or "Birthdays",
        "name_col": int(d.get("name_col") or 0),
        "birthday_col": int(d.get("birthday_col") or 1),
        "discord_id_col": int(d.get("discord_id_col") or -1),
        "data_start_row": int(d.get("data_start_row") or 2),
        "enabled": int(d.get("enabled") or 1),
        "train_integration": int(d.get("train_integration") or 0),
        "flexible_placement": int(d.get("flexible_placement") or 1),
        "lookahead_days": int(d.get("lookahead_days") or 14),
        "reminders_enabled": int(d.get("reminders_enabled") or 0),
        "reminder_time": d.get("reminder_time") or "08:00",
        "dm_message": d.get("dm_message") or "",
    }
    remap_channels = [
        entry
        for entry in (
            _channel_remap(
                "reminder_channel_id",
                "birthday.reminder_channel_id",
                int(d.get("reminder_channel_id") or 0),
                channel_lookup,
            ),
        )
        if entry is not None
    ]
    return {
        "travels": travels,
        "remap_channels": remap_channels,
        "remap_roles": [],
    }


def collect_growth(
    guild_id: int, *, channel_lookup: Callable[[int], str], role_lookup: Callable[[int], str]
) -> dict | None:
    from config import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_growth_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    travels = {
        "enabled": int(d.get("enabled") or 0),
        "tab_source": d.get("tab_source") or "",
        "name_col": d.get("name_col") or "A",
        "metrics": d.get("metrics") or "",
        "tab_growth": d.get("tab_growth") or "Growth Tracking",
        "snapshot_frequency": d.get("snapshot_frequency") or "monthly",
        "snapshot_day": int(d.get("snapshot_day") or 1),
        "snapshot_interval": int(d.get("snapshot_interval") or 30),
        "data_start_row": int(d.get("data_start_row") or 2),
        "tab_breakdown": d.get("tab_breakdown") or "Growth Breakdown",
        "breakdown_thresholds": d.get("breakdown_thresholds") or "{}",
        "breakdown_labels": d.get("breakdown_labels") or "{}",
        "breakdown_bucket_filter": d.get("breakdown_bucket_filter") or "[]",
    }
    remap_channels = [
        entry
        for entry in (
            _channel_remap(
                "breakdown_post_channel_id",
                "growth.breakdown_post_channel_id",
                int(d.get("breakdown_post_channel_id") or 0),
                channel_lookup,
            ),
        )
        if entry is not None
    ]
    return {
        "travels": travels,
        "remap_channels": remap_channels,
        "remap_roles": [],
    }


def collect_surveys(
    guild_id: int, *, channel_lookup: Callable[[int], str], role_lookup: Callable[[int], str]
) -> list[dict] | None:
    """The "surveys" category covers BOTH the default survey
    (``guild_survey_config``) and any Premium named surveys
    (``guild_extra_surveys``). They share roughly the same shape; the
    default survey gets ``survey_id = "default"`` for round-trip
    identification on import."""
    from config import _get_conn

    out: list[dict] = []
    with _get_conn() as conn:
        default_row = conn.execute(
            "SELECT * FROM guild_survey_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
        extra_rows = conn.execute(
            "SELECT * FROM guild_extra_surveys WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()

    if default_row is not None:
        d = dict(default_row)
        fmt = {"name": "Default"}
        travels = {
            "survey_id": "default",
            "survey_name": "Default",
            "tab_squad_powers": d.get("tab_squad_powers") or "Squad Powers",
            "tab_history": d.get("tab_history") or "Survey History",
            "questions": d.get("questions") or "",
            "intro_message": d.get("intro_message") or "",
            "reminder_message": d.get("reminder_message") or "",
            "reminder_enabled": int(d.get("reminder_enabled") or 0),
            "reminder_frequency": d.get("reminder_frequency") or "off",
            "reminder_day_of_week": int(d.get("reminder_day_of_week") or 1),
            "reminder_time": d.get("reminder_time") or "12:00",
            "reminder_use_dm": int(d.get("reminder_use_dm") or 0),
        }
        remap_channels = [
            entry
            for entry in (
                _channel_remap(
                    "reminder_channel_id",
                    "surveys.reminder_channel_id",
                    int(d.get("reminder_channel_id") or 0),
                    channel_lookup,
                    purpose_format=fmt,
                ),
            )
            if entry is not None
        ]
        out.append(
            {
                "travels": travels,
                "remap_channels": remap_channels,
                "remap_roles": [],
            }
        )

    for row in extra_rows:
        d = dict(row)
        name = d.get("survey_name") or d.get("survey_id") or "Extra"
        fmt = {"name": name}
        travels = {
            "survey_id": d.get("survey_id") or "",
            "survey_name": d.get("survey_name") or "",
            "tab_squad_powers": d.get("tab_squad_powers") or "Squad Powers",
            "tab_history": d.get("tab_history") or "Survey History",
            "questions": d.get("questions") or "",
            "intro_message": d.get("intro_message") or "",
            "reminder_message": d.get("reminder_message") or "",
            "reminder_enabled": int(d.get("reminder_enabled") or 0),
            "reminder_frequency": d.get("reminder_frequency") or "off",
            "reminder_day_of_week": int(d.get("reminder_day_of_week") or 1),
            "reminder_time": d.get("reminder_time") or "12:00",
            "reminder_use_dm": int(d.get("reminder_use_dm") or 0),
        }
        remap_channels = [
            entry
            for entry in (
                _channel_remap(
                    "survey_channel_id",
                    "surveys.survey_channel_id",
                    int(d.get("survey_channel_id") or 0),
                    channel_lookup,
                    purpose_format=fmt,
                ),
                _channel_remap(
                    "notify_channel_id",
                    "surveys.notify_channel_id",
                    int(d.get("notify_channel_id") or 0),
                    channel_lookup,
                    purpose_format=fmt,
                ),
                _channel_remap(
                    "reminder_channel_id",
                    "surveys.reminder_channel_id",
                    int(d.get("reminder_channel_id") or 0),
                    channel_lookup,
                    purpose_format=fmt,
                ),
            )
            if entry is not None
        ]
        out.append(
            {
                "travels": travels,
                "remap_channels": remap_channels,
                "remap_roles": [],
            }
        )

    return out or None


def collect_shiny_tasks(
    guild_id: int, *, channel_lookup: Callable[[int], str], role_lookup: Callable[[int], str]
) -> dict | None:
    """Collect the per-guild Daily Shiny Tasks config row, if any. The
    operational `last_posted_date` column is not exported — it's
    duplicate-suppression state for the scheduler loop, not config."""
    from config import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_shiny_tasks_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    travels = {
        "enabled": int(d.get("enabled") or 0),
        "post_time": d.get("post_time") or "09:00",
        "server_min": int(d.get("server_min") or 0),
        "server_max": int(d.get("server_max") or 0),
        "message_template": d.get("message_template") or "",
    }
    remap_channels = [
        entry
        for entry in (
            _channel_remap(
                "channel_id",
                "shiny_tasks.channel_id",
                int(d.get("channel_id") or 0),
                channel_lookup,
            ),
        )
        if entry is not None
    ]
    return {
        "travels": travels,
        "remap_channels": remap_channels,
        "remap_roles": [],
    }


def collect_member_roster(
    guild_id: int, *, channel_lookup: Callable[[int], str], role_lookup: Callable[[int], str]
) -> dict | None:
    from config import _get_conn

    with _get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM guild_member_roster_config WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    travels = {
        "enabled": int(d.get("enabled") or 0),
        "tab_name": d.get("tab_name") or "Member Roster",
        "discord_id_col": int(d.get("discord_id_col") or 0),
        "name_col": int(d.get("name_col") or 1),
        "display_col": int(d.get("display_col") or 2),
        "joined_col": int(d.get("joined_col") or 3),
        "roles_col": int(d.get("roles_col") or 4),
        "auto_sync": int(d.get("auto_sync") or 1),
    }
    remap_channels: list[dict] = []
    remap_roles: list[dict] = []
    role_entry = _role_remap(
        "role_filter_id",
        "member_roster.role_filter_id",
        int(d.get("role_filter_id") or 0),
        role_lookup,
    )
    if role_entry:
        remap_roles.append(role_entry)
    return {
        "travels": travels,
        "remap_channels": remap_channels,
        "remap_roles": remap_roles,
    }


COLLECTORS: dict[str, Callable] = {
    "core": collect_core,
    "events": collect_events,
    "ds": collect_ds,
    "cs": collect_cs,
    "train": collect_train,
    "birthday": collect_birthday,
    "growth": collect_growth,
    "surveys": collect_surveys,
    "shiny_tasks": collect_shiny_tasks,
    "member_roster": collect_member_roster,
}


# ── Orchestration: build_export, serialize, parse, validate ─────────────────


def collect_available_categories(
    guild_id: int,
    *,
    channel_lookup: Callable[[int], str],
    role_lookup: Callable[[int], str],
) -> list[str]:
    """Return the list of category keys that have data for this guild —
    used by the export command to render the multi-select."""
    return [
        key
        for key in CATEGORY_ORDER
        if COLLECTORS[key](guild_id, channel_lookup=channel_lookup, role_lookup=role_lookup)
        is not None
    ]


def build_export(
    guild_id: int,
    *,
    categories: list[str],
    source_guild_name: str,
    exporter_user_id: int,
    channel_lookup: Callable[[int], str],
    role_lookup: Callable[[int], str],
) -> dict:
    """Build the full export dict. `categories` is the user's selection
    from the multi-select; unknown keys are silently skipped (defensive,
    but shouldn't happen in practice since the multi-select is bounded)."""
    data: dict[str, Any] = {}
    applied_categories: list[str] = []
    for key in categories:
        if key not in COLLECTORS:
            continue
        collected = COLLECTORS[key](
            guild_id, channel_lookup=channel_lookup, role_lookup=role_lookup
        )
        if collected is None:
            continue
        data[key] = collected
        applied_categories.append(key)

    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "exported_at": datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z"),
        "exported_by_user_id": int(exporter_user_id),
        "source_guild": {
            "id": int(guild_id),
            "name": source_guild_name,
        },
        "categories": applied_categories,
        "data": data,
    }


def serialize_to_json_bytes(export_dict: dict) -> bytes:
    """JSON-encode an export dict to bytes (UTF-8, indented). The output
    is small enough to comfortably fit in a Discord attachment for any
    realistic alliance config."""
    return json.dumps(export_dict, indent=2, ensure_ascii=False, sort_keys=False).encode("utf-8")


class ImportValidationError(Exception):
    """Raised when an import file fails structural validation. The
    message is user-facing — show it directly in the import flow."""


def parse_and_validate(file_bytes: bytes) -> dict:
    """Parse JSON bytes into a validated export dict.

    Lenient: unknown top-level keys, unknown category keys, and unknown
    fields inside a known category are all silently ignored (with a hint
    surfaced in `unknown_keys` for the import wizard to show as a
    warning). Strict on the shape we *do* recognize — a wrong type on a
    recognized field raises `ImportValidationError` with a precise path.

    Returns the validated dict with two extra keys for the cog to use:
      * ``unknown_keys`` — list of dotted paths the parser didn't recognize
      * ``categories_present`` — list of known category keys (intersection
        of the file's `categories` list and `COLLECTORS`)
    """
    try:
        text = file_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ImportValidationError(
            f"File isn't valid UTF-8 (offset {e.start}). "
            f"Make sure you're attaching the JSON file from /config export "
            f"without converting it through any other tool."
        ) from e
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ImportValidationError(
            f"File isn't valid JSON: {e.msg} at line {e.lineno}, "
            f"column {e.colno}. If you edited the file by hand, "
            f"double-check the syntax (commas, brackets, quotes)."
        ) from e

    if not isinstance(parsed, dict):
        raise ImportValidationError(
            "Top-level value isn't an object — the file looks corrupted or "
            "was hand-edited incorrectly."
        )

    schema_version = parsed.get("schema_version")
    if not isinstance(schema_version, int):
        raise ImportValidationError(
            "Missing or invalid `schema_version`. This file may not be a "
            "/config export output — re-export from the source server."
        )
    if schema_version > EXPORT_SCHEMA_VERSION:
        raise ImportValidationError(
            f"This export was made by a newer bot (schema v{schema_version}) "
            f"than the one running here (v{EXPORT_SCHEMA_VERSION}). "
            f"Update the bot in this server and try again."
        )

    source_guild = parsed.get("source_guild") or {}
    if not isinstance(source_guild, dict):
        raise ImportValidationError("`source_guild` isn't an object. File is corrupted.")
    if not isinstance(source_guild.get("id"), int):
        raise ImportValidationError(
            "`source_guild.id` is missing or not an integer. File is corrupted."
        )

    categories_raw = parsed.get("categories") or []
    if not isinstance(categories_raw, list) or not all(isinstance(c, str) for c in categories_raw):
        raise ImportValidationError("`categories` must be a list of strings.")

    data = parsed.get("data") or {}
    if not isinstance(data, dict):
        raise ImportValidationError("`data` must be an object keyed by category name.")

    unknown_keys: list[str] = []
    for cat in categories_raw:
        if cat not in COLLECTORS:
            unknown_keys.append(f"categories[{cat}]")

    categories_present = [c for c in categories_raw if c in COLLECTORS and c in data]

    # Sanity-check the shape of each known category. Lenient: don't reject
    # unknown fields inside `travels` (forward-compat — newer exports may
    # carry fields this bot doesn't know about yet; apply_import simply
    # won't write them).
    for cat in categories_present:
        section = data[cat]
        if cat in ("core", "train", "birthday", "growth", "shiny_tasks", "member_roster"):
            if not isinstance(section, dict):
                raise ImportValidationError(f"`data.{cat}` must be an object.")
            for key in ("travels", "remap_channels", "remap_roles"):
                if key in section and not isinstance(
                    section[key],
                    (dict if key == "travels" else list),
                ):
                    raise ImportValidationError(f"`data.{cat}.{key}` has the wrong type.")
        else:
            # Multi-row categories
            if not isinstance(section, list):
                raise ImportValidationError(
                    f"`data.{cat}` must be a list of objects (one per row)."
                )
            for i, row in enumerate(section):
                if not isinstance(row, dict):
                    raise ImportValidationError(f"`data.{cat}[{i}]` must be an object.")

    parsed["unknown_keys"] = unknown_keys
    parsed["categories_present"] = categories_present
    return parsed


# ── Apply: write the imported config into SQLite ────────────────────────────


@dataclass
class RemapDecisions:
    """The import wizard collects user decisions for each unique source
    ID before applying. Each decision is one of:

      * ``("set", new_id)``   — write the new ID into every field that
                                referenced this source ID
      * ``("keep_current",)`` — leave each affected field's current value
                                untouched
      * ``("skip",)``         — set each affected field to 0 (not configured)
    """

    channel_decisions: dict[int, tuple]  # source_channel_id → decision tuple
    role_decisions: dict[int, tuple]  # source_role_id    → decision tuple
    spreadsheet_id: str | None  # None = keep current, "" = clear, else use
    same_guild: bool


def _resolve_channel(source_id: int, decisions: dict[int, tuple], current_value: int) -> int:
    """Resolve a single source channel ID to its final value per the
    user's decision. Defaults to current_value when no decision was
    recorded (defensive)."""
    decision = decisions.get(source_id)
    if decision is None:
        return current_value
    action = decision[0]
    if action == "set":
        return int(decision[1])
    if action == "keep_current":
        return current_value
    if action == "skip":
        return 0
    return current_value


def _resolve_role(source_id: int, decisions: dict[int, tuple], current_value: int) -> int:
    return _resolve_channel(source_id, decisions, current_value)


def apply_import(guild_id: int, parsed: dict, remap: RemapDecisions) -> dict:
    """Apply a parsed + validated export to a guild. Pre-flight validates
    every required remap is decided; if anything is missing, the function
    raises `ImportValidationError` BEFORE any writes happen. Returns a
    summary dict with `applied`, `skipped`, and `warnings` keys.

    Apply is best-effort per category: each category's writes are wrapped
    in their own try/except so a failure on one (e.g. an unexpected
    schema drift) doesn't poison the others. The summary lists exactly
    which categories applied and which failed with their reason.
    """
    from config import (
        get_config,
        save_config,
        save_growth_config,
        save_growth_breakdown_config,
        save_train_config,
        save_birthday_config,
        save_storm_config,
        save_member_roster_config,
        save_guild_event,
        save_survey_config,
        save_extra_survey,
        save_survey_reminder,
        save_participation_config,
        save_shiny_tasks_config,
        _get_conn,
    )

    summary = {"applied": [], "skipped": [], "warnings": []}
    data = parsed.get("data") or {}

    if parsed.get("unknown_keys"):
        for k in parsed["unknown_keys"]:
            summary["warnings"].append(
                f"Ignored unknown key `{k}` (newer-than-this-bot data, or hand-edited file)."
            )

    def _apply(category: str, body: Callable[[], None]) -> None:
        try:
            body()
            summary["applied"].append(category)
        except Exception as e:
            summary["skipped"].append({"category": category, "reason": str(e)})

    cats = parsed.get("categories_present") or []

    # ── core ──────────────────────────────────────────────────────────────
    if "core" in cats:

        def _do_core():
            section = data["core"]
            travels = section.get("travels") or {}
            cfg = get_config(guild_id) or None
            if cfg is None:
                # Brand-new guild — create a fresh config first via
                # get_or_create_config so the row exists for save_config.
                from config import get_or_create_config

                cfg = get_or_create_config(guild_id)
            # Apply alliance-defined values.
            for field in (
                "leadership_role_name",
                "member_role_name",
                "timezone",
                "event_draft_time",
                "tab_train_schedule",
                "tab_ds_assignments",
                "tab_sitouts",
                "tab_survey_history",
                "tab_member_default",
            ):
                if field in travels:
                    setattr(cfg, field, travels[field])
            if "event_five_min_warning" in travels:
                cfg.event_five_min_warning = int(travels["event_five_min_warning"])
            if remap.spreadsheet_id is not None:
                cfg.spreadsheet_id = remap.spreadsheet_id

            # Apply remapped channel/role IDs.
            for entry in section.get("remap_channels") or []:
                final = _resolve_channel(
                    int(entry["source_id"]),
                    remap.channel_decisions,
                    getattr(cfg, entry["field"], 0),
                )
                setattr(cfg, entry["field"], final)
            for entry in section.get("remap_roles") or []:
                final = _resolve_role(
                    int(entry["source_id"]), remap.role_decisions, getattr(cfg, entry["field"], 0)
                )
                setattr(cfg, entry["field"], final)

            cfg.setup_complete = True
            save_config(cfg)

        _apply("core", _do_core)

    # ── events ────────────────────────────────────────────────────────────
    if "events" in cats:

        def _do_events():
            from config import (
                get_guild_events,
                delete_guild_event,
                save_guild_event,
            )

            existing_by_key = {
                ev["short_key"]: ev for ev in get_guild_events(guild_id, active_only=False)
            }
            for row in data["events"]:
                t = row.get("travels") or {}
                channel_remap = {entry["field"]: entry for entry in row.get("remap_channels") or []}

                draft_entry = channel_remap.get("draft_channel_id")
                announce_entry = channel_remap.get("announcement_channel_id")
                current_draft = (
                    int(existing_by_key.get(t.get("short_key"), {}).get("draft_channel_id") or 0)
                    if t.get("short_key") in existing_by_key
                    else 0
                )
                current_announce = (
                    int(
                        existing_by_key.get(t.get("short_key"), {}).get("announcement_channel_id")
                        or 0
                    )
                    if t.get("short_key") in existing_by_key
                    else 0
                )
                draft_id = (
                    _resolve_channel(
                        int(draft_entry["source_id"]), remap.channel_decisions, current_draft
                    )
                    if draft_entry
                    else current_draft
                )
                announce_id = (
                    _resolve_channel(
                        int(announce_entry["source_id"]), remap.channel_decisions, current_announce
                    )
                    if announce_entry
                    else current_announce
                )

                event_dict = {
                    "short_key": t.get("short_key", ""),
                    "name": t.get("name", ""),
                    "timezone": t.get("timezone", "America/New_York"),
                    "default_time": t.get("default_time", "22:00"),
                    "announcement_blurb": t.get("announcement_blurb", ""),
                    "schedule_type": t.get("schedule_type", "repeating"),
                    "anchor_date": t.get("anchor_date", ""),
                    "interval_days": int(t.get("interval_days") or 0),
                    "draft_channel_id": draft_id,
                    "announcement_channel_id": announce_id,
                    "draft_time": t.get("draft_time", "12:00"),
                    "five_min_warning": int(t.get("five_min_warning") or 0),
                    "active": int(t.get("active") or 1),
                }
                save_guild_event(guild_id, event_dict)

        _apply("events", _do_events)

    # ── storm: ds & cs ────────────────────────────────────────────────────
    for storm_key, event_kind in (("ds", "DS"), ("cs", "CS")):
        if storm_key not in cats:
            continue

        def _do_storm(k=storm_key, kind=event_kind):
            import json as _json
            from config import _get_conn

            for row in data[k]:
                t = row.get("travels") or {}
                event_type = t.get("event_type", kind)
                channel_remap = {entry["field"]: entry for entry in row.get("remap_channels") or []}

                # Look up the current value for keep-current resolution.
                with _get_conn() as conn:
                    cur_row = conn.execute(
                        "SELECT log_channel_id, post_channel_id "
                        "FROM guild_storm_config WHERE guild_id = ? AND event_type = ?",
                        (guild_id, event_type),
                    ).fetchone()
                cur_dict = dict(cur_row) if cur_row else {}
                current_log = int(cur_dict.get("log_channel_id") or 0)
                current_post = int(cur_dict.get("post_channel_id") or 0)
                log_entry = channel_remap.get("log_channel_id")
                post_entry = channel_remap.get("post_channel_id")
                log_id = (
                    _resolve_channel(
                        int(log_entry["source_id"]), remap.channel_decisions, current_log
                    )
                    if log_entry
                    else current_log
                )
                post_id = (
                    _resolve_channel(
                        int(post_entry["source_id"]), remap.channel_decisions, current_post
                    )
                    if post_entry
                    else current_post
                )

                # Decode templates_json (kept as JSON-encoded text in the
                # export to preserve the on-disk shape verbatim).
                templates: list = []
                try:
                    templates = _json.loads(t.get("templates_json") or "[]")
                except (json.JSONDecodeError, TypeError):
                    templates = []

                save_storm_config(
                    guild_id,
                    event_type,
                    tab_name=t.get("tab_name", ""),
                    mail_template=t.get("mail_template", ""),
                    timezone=t.get("timezone", "America/New_York"),
                    log_channel_id=log_id,
                    templates=templates if templates else None,
                    default_template=t.get("default_template", "Default"),
                    post_channel_id=post_id,
                    dm_reminder_message=t.get("dm_reminder_message", ""),
                )

                # Apply participation fields (separate save call).
                try:
                    p_questions = _json.loads(t.get("participation_questions") or "[]")
                except (json.JSONDecodeError, TypeError):
                    p_questions = []
                save_participation_config(
                    guild_id,
                    event_type,
                    enabled=int(t.get("participation_enabled") or 0),
                    tab_name=t.get("participation_tab_name") or "",
                    questions=p_questions,
                    roster_tab=t.get("participation_roster_tab") or "",
                    roster_name_col=int(t.get("participation_roster_name_col") or 0),
                    roster_alias_col=int(t.get("participation_roster_alias_col") or -1),
                    roster_start_row=int(t.get("participation_roster_start_row") or 2),
                )

        _apply(storm_key, _do_storm)

    # ── train ─────────────────────────────────────────────────────────────
    if "train" in cats:

        def _do_train():
            import json as _json
            from config import _get_conn

            t = data["train"].get("travels") or {}
            channel_remap = {
                entry["field"]: entry for entry in data["train"].get("remap_channels") or []
            }
            with _get_conn() as conn:
                cur_row = conn.execute(
                    "SELECT reminder_channel_id FROM guild_train_config WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
            cur_dict = dict(cur_row) if cur_row else {}
            current_reminder = int(cur_dict.get("reminder_channel_id") or 0)
            reminder_entry = channel_remap.get("reminder_channel_id")
            reminder_id = (
                _resolve_channel(
                    int(reminder_entry["source_id"]), remap.channel_decisions, current_reminder
                )
                if reminder_entry
                else current_reminder
            )
            try:
                templates = _json.loads(t.get("templates_json") or "[]")
            except (json.JSONDecodeError, TypeError):
                templates = []
            try:
                themes = _json.loads(t.get("themes") or "[]")
            except (json.JSONDecodeError, TypeError):
                themes = []
            try:
                tones = _json.loads(t.get("tones") or "[]")
            except (json.JSONDecodeError, TypeError):
                tones = []
            save_train_config(
                guild_id,
                tab_name=t.get("tab_name", "Train Schedule"),
                themes=themes,
                tones=tones,
                prompt_template=t.get("prompt_template", ""),
                default_tone=t.get("default_tone", ""),
                blurbs_enabled=int(t.get("blurbs_enabled") or 0),
                reminders_enabled=int(t.get("reminders_enabled") or 0),
                reminder_channel_id=reminder_id,
                reminder_time=t.get("reminder_time", "22:00"),
                templates=templates if templates else None,
                default_template=t.get("default_template", "Default"),
                dm_message=t.get("dm_message", ""),
            )

        _apply("train", _do_train)

    # ── birthday ──────────────────────────────────────────────────────────
    if "birthday" in cats:

        def _do_birthday():
            from config import _get_conn

            t = data["birthday"].get("travels") or {}
            channel_remap = {
                entry["field"]: entry for entry in data["birthday"].get("remap_channels") or []
            }
            with _get_conn() as conn:
                cur_row = conn.execute(
                    "SELECT reminder_channel_id FROM guild_birthday_config WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
            cur_dict = dict(cur_row) if cur_row else {}
            current_reminder = int(cur_dict.get("reminder_channel_id") or 0)
            reminder_entry = channel_remap.get("reminder_channel_id")
            reminder_id = (
                _resolve_channel(
                    int(reminder_entry["source_id"]), remap.channel_decisions, current_reminder
                )
                if reminder_entry
                else current_reminder
            )
            save_birthday_config(
                guild_id,
                tab_name=t.get("tab_name", "Birthdays"),
                name_col=int(t.get("name_col") or 0),
                birthday_col=int(t.get("birthday_col") or 1),
                discord_id_col=int(t.get("discord_id_col") or -1),
                data_start_row=int(t.get("data_start_row") or 2),
                enabled=int(t.get("enabled") or 1),
                train_integration=int(t.get("train_integration") or 0),
                flexible_placement=int(t.get("flexible_placement") or 1),
                lookahead_days=int(t.get("lookahead_days") or 14),
                reminders_enabled=int(t.get("reminders_enabled") or 0),
                reminder_channel_id=reminder_id,
                reminder_time=t.get("reminder_time", "08:00"),
                dm_message=t.get("dm_message", ""),
            )

        _apply("birthday", _do_birthday)

    # ── growth ────────────────────────────────────────────────────────────
    if "growth" in cats:

        def _do_growth():
            import json as _json
            from config import _get_conn

            t = data["growth"].get("travels") or {}
            channel_remap = {
                entry["field"]: entry for entry in data["growth"].get("remap_channels") or []
            }
            with _get_conn() as conn:
                cur_row = conn.execute(
                    "SELECT breakdown_post_channel_id FROM guild_growth_config WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
            cur_dict = dict(cur_row) if cur_row else {}
            current_post = int(cur_dict.get("breakdown_post_channel_id") or 0)
            post_entry = channel_remap.get("breakdown_post_channel_id")
            post_id = (
                _resolve_channel(
                    int(post_entry["source_id"]), remap.channel_decisions, current_post
                )
                if post_entry
                else current_post
            )
            try:
                metrics = _json.loads(t.get("metrics") or "[]")
            except (json.JSONDecodeError, TypeError):
                metrics = []
            save_growth_config(
                guild_id,
                enabled=int(t.get("enabled") or 0),
                tab_source=t.get("tab_source", ""),
                name_col=t.get("name_col", "A"),
                metrics=metrics,
                tab_growth=t.get("tab_growth", "Growth Tracking"),
                snapshot_frequency=t.get("snapshot_frequency", "monthly"),
                snapshot_day=int(t.get("snapshot_day") or 1),
                snapshot_interval=int(t.get("snapshot_interval") or 30),
                data_start_row=int(t.get("data_start_row") or 2),
            )
            # Breakdown layer (#34) is a separate setter — only applies
            # if the core growth row exists, which it now does.
            try:
                thresholds = _json.loads(t.get("breakdown_thresholds") or "{}")
            except (json.JSONDecodeError, TypeError):
                thresholds = {}
            try:
                labels = _json.loads(t.get("breakdown_labels") or "{}")
            except (json.JSONDecodeError, TypeError):
                labels = {}
            try:
                bucket_filter = _json.loads(t.get("breakdown_bucket_filter") or "[]")
            except (json.JSONDecodeError, TypeError):
                bucket_filter = []
            save_growth_breakdown_config(
                guild_id,
                tab_breakdown=t.get("tab_breakdown") or "Growth Breakdown",
                breakdown_thresholds=thresholds,
                breakdown_labels=labels,
                breakdown_post_channel_id=post_id,
                breakdown_bucket_filter=bucket_filter,
            )

        _apply("growth", _do_growth)

    # ── surveys (default + extras) ────────────────────────────────────────
    if "surveys" in cats:

        def _do_surveys():
            import json as _json
            from config import _get_conn

            for row in data["surveys"]:
                t = row.get("travels") or {}
                survey_id = t.get("survey_id") or "default"
                channel_remap = {entry["field"]: entry for entry in row.get("remap_channels") or []}
                try:
                    questions = _json.loads(t.get("questions") or "[]")
                except (json.JSONDecodeError, TypeError):
                    questions = []

                if survey_id == "default":
                    save_survey_config(
                        guild_id,
                        tab_squad_powers=t.get("tab_squad_powers", "Squad Powers"),
                        tab_history=t.get("tab_history", "Survey History"),
                        questions=questions,
                        intro_message=t.get("intro_message", ""),
                    )
                else:
                    save_extra_survey(
                        guild_id,
                        survey_id=survey_id,
                        survey_name=t.get("survey_name", survey_id),
                        tab_squad_powers=t.get("tab_squad_powers", "Squad Powers"),
                        tab_history=t.get("tab_history", "Survey History"),
                        questions=questions,
                        intro_message=t.get("intro_message", ""),
                        survey_channel_id=0,
                        notify_channel_id=0,
                    )

                # Resolve remap channel IDs against the current state.
                with _get_conn() as conn:
                    if survey_id == "default":
                        cur_row = conn.execute(
                            "SELECT reminder_channel_id FROM guild_survey_config "
                            "WHERE guild_id = ?",
                            (guild_id,),
                        ).fetchone()
                    else:
                        cur_row = conn.execute(
                            "SELECT survey_channel_id, notify_channel_id, "
                            "       reminder_channel_id FROM guild_extra_surveys "
                            "WHERE guild_id = ? AND survey_id = ?",
                            (guild_id, survey_id),
                        ).fetchone()
                cur = dict(cur_row) if cur_row else {}

                # Reminder fields are saved via save_survey_reminder regardless
                # of default vs. extra.
                rem_entry = channel_remap.get("reminder_channel_id")
                rem_id = (
                    _resolve_channel(
                        int(rem_entry["source_id"]),
                        remap.channel_decisions,
                        int(cur.get("reminder_channel_id") or 0),
                    )
                    if rem_entry
                    else int(cur.get("reminder_channel_id") or 0)
                )
                save_survey_reminder(
                    guild_id,
                    survey_id,
                    enabled=int(t.get("reminder_enabled") or 0),
                    frequency=t.get("reminder_frequency", "off"),
                    day_of_week=int(t.get("reminder_day_of_week") or 1),
                    time_str=t.get("reminder_time", "12:00"),
                    channel_id=rem_id,
                    use_dm=int(t.get("reminder_use_dm") or 0),
                    message=t.get("reminder_message", ""),
                )

                # Survey/notify channels apply only to extras.
                if survey_id != "default":
                    s_entry = channel_remap.get("survey_channel_id")
                    s_id = (
                        _resolve_channel(
                            int(s_entry["source_id"]),
                            remap.channel_decisions,
                            int(cur.get("survey_channel_id") or 0),
                        )
                        if s_entry
                        else int(cur.get("survey_channel_id") or 0)
                    )
                    n_entry = channel_remap.get("notify_channel_id")
                    n_id = (
                        _resolve_channel(
                            int(n_entry["source_id"]),
                            remap.channel_decisions,
                            int(cur.get("notify_channel_id") or 0),
                        )
                        if n_entry
                        else int(cur.get("notify_channel_id") or 0)
                    )
                    with _get_conn() as conn:
                        conn.execute(
                            "UPDATE guild_extra_surveys SET "
                            "  survey_channel_id = ?, notify_channel_id = ? "
                            "WHERE guild_id = ? AND survey_id = ?",
                            (s_id, n_id, guild_id, survey_id),
                        )
                        conn.commit()

        _apply("surveys", _do_surveys)

    # ── shiny_tasks ───────────────────────────────────────────────────────
    if "shiny_tasks" in cats:

        def _do_shiny_tasks():
            from config import _get_conn

            t = data["shiny_tasks"].get("travels") or {}
            channel_remap = {
                entry["field"]: entry for entry in data["shiny_tasks"].get("remap_channels") or []
            }
            with _get_conn() as conn:
                cur_row = conn.execute(
                    "SELECT channel_id FROM guild_shiny_tasks_config WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
            cur_dict = dict(cur_row) if cur_row else {}
            current_channel = int(cur_dict.get("channel_id") or 0)
            channel_entry = channel_remap.get("channel_id")
            channel_id = (
                _resolve_channel(
                    int(channel_entry["source_id"]), remap.channel_decisions, current_channel
                )
                if channel_entry
                else current_channel
            )
            save_shiny_tasks_config(
                guild_id,
                enabled=int(t.get("enabled") or 0),
                channel_id=channel_id,
                post_time=t.get("post_time", "09:00"),
                server_min=int(t.get("server_min") or 0),
                server_max=int(t.get("server_max") or 0),
                message_template=t.get("message_template", ""),
            )

        _apply("shiny_tasks", _do_shiny_tasks)

    # ── member_roster ─────────────────────────────────────────────────────
    if "member_roster" in cats:

        def _do_roster():
            from config import _get_conn

            t = data["member_roster"].get("travels") or {}
            role_remap_entries = data["member_roster"].get("remap_roles") or []
            with _get_conn() as conn:
                cur_row = conn.execute(
                    "SELECT role_filter_id FROM guild_member_roster_config WHERE guild_id = ?",
                    (guild_id,),
                ).fetchone()
            cur_dict = dict(cur_row) if cur_row else {}
            current_role = int(cur_dict.get("role_filter_id") or 0)
            role_entry = next(
                (e for e in role_remap_entries if e["field"] == "role_filter_id"), None
            )
            role_filter_id = (
                _resolve_role(int(role_entry["source_id"]), remap.role_decisions, current_role)
                if role_entry
                else current_role
            )
            save_member_roster_config(
                guild_id,
                enabled=int(t.get("enabled") or 0),
                tab_name=t.get("tab_name", "Member Roster"),
                discord_id_col=int(t.get("discord_id_col") or 0),
                name_col=int(t.get("name_col") or 1),
                display_col=int(t.get("display_col") or 2),
                joined_col=int(t.get("joined_col") or 3),
                roles_col=int(t.get("roles_col") or 4),
                role_filter_id=role_filter_id,
                auto_sync=int(t.get("auto_sync") or 1),
            )

        _apply("member_roster", _do_roster)

    return summary


# ── Remap discovery helper for the wizard ───────────────────────────────────


@dataclass
class RemapGroup:
    """A unique (source_id, kind) bundle with the human-readable purposes
    each occurrence carries. The wizard renders one prompt per group."""

    kind: str  # "channel" or "role"
    source_id: int
    source_name: str
    purposes: list[str]  # one per occurrence, in collection order


def discover_remap_groups(parsed: dict) -> tuple[list[RemapGroup], list[RemapGroup]]:
    """Walk every remap entry across every present category and group by
    (kind, source_id). Returns ``(channel_groups, role_groups)`` for the
    wizard to render in order."""
    by_channel: dict[int, RemapGroup] = {}
    by_role: dict[int, RemapGroup] = {}
    data = parsed.get("data") or {}

    def _consume(entries: list, target: dict, kind: str) -> None:
        for entry in entries or []:
            src_id = int(entry.get("source_id") or 0)
            if not src_id:
                continue
            grp = target.get(src_id)
            if grp is None:
                grp = RemapGroup(
                    kind=kind,
                    source_id=src_id,
                    source_name=str(entry.get("source_name") or ""),
                    purposes=[],
                )
                target[src_id] = grp
            grp.purposes.append(str(entry.get("purpose") or "?"))

    for cat in parsed.get("categories_present") or []:
        section = data.get(cat)
        if section is None:
            continue
        if isinstance(section, dict):
            _consume(section.get("remap_channels") or [], by_channel, "channel")
            _consume(section.get("remap_roles") or [], by_role, "role")
        elif isinstance(section, list):
            for row in section:
                if not isinstance(row, dict):
                    continue
                _consume(row.get("remap_channels") or [], by_channel, "channel")
                _consume(row.get("remap_roles") or [], by_role, "role")

    # Sort by first-occurrence order so the wizard walks the same path
    # for the same export each time (helps with reviewing decisions).
    channels_sorted = sorted(by_channel.values(), key=lambda g: g.source_id)
    roles_sorted = sorted(by_role.values(), key=lambda g: g.source_id)
    return channels_sorted, roles_sorted
