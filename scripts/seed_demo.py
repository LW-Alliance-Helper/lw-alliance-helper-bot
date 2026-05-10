"""
seed_demo.py — Reset a demo guild to a clean fictional-alliance state.

Two ways to run:

1. **Deploy-time (recommended for the production bot's demo guild):** set
   `SEED_DEMO_ON_BOOT=1` plus the `SEED_DEMO_*` env vars in Railway and
   redeploy. `bot.py`'s `on_ready` calls `seed_demo_guild_from_env()`,
   which writes against the live mounted SQLite + gspread credentials.
   Use this whenever the demo guild has drifted from clean state because
   community members poked at it.

2. **CLI (local dev / one-off testing):** run from the bot repo root:

       python scripts/seed_demo.py \\
         --guild-id 1234567890 \\
         --sheet-id abc...XYZ \\
         --leadership-channel 1111 \\
         --leadership-role    2222 \\
         --member-role        3333 \\
         --post-channel       4444   # optional; falls back to leadership channel

What it does (idempotent — safe to fire repeatedly as a reset):
- Wipes user-added rows in `guild_events` and `guild_extra_surveys` for
  the demo guild (so re-seeds produce a true reset, not an accumulation)
- UPSERTs the canonical demo config across every per-guild config table
- Wipes and rewrites every demo Sheet tab (roster, birthdays, train
  schedule, survey history, etc.) with 20 fictional members and realistic
  but invented numbers

The demo data uses fantasy-style member names so screenshots from the
demo server can never be confused with a real alliance's data.

ENVIRONMENT
-----------

    CONFIG_DB_PATH              Path to the bot's SQLite. Defaults to the
                                production Railway volume path.
    GOOGLE_CREDENTIALS_JSON     OR
    GOOGLE_SERVICE_ACCOUNT_FILE Service account creds for sheet writes.

The demo Sheet must already be shared with the service account email
before the seed runs.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta

# Bot imports — assumes script is run from the repo root with PYTHONPATH=.
import config
from defaults import (
    DEFAULT_DS_TEMPLATE,
    DEFAULT_PROMPT,
    DEFAULT_SURVEY_INTRO,
    DEFAULT_SURVEY_QUESTIONS,
    DEFAULT_THEMES,
    DEFAULT_TONES,
)


# ── Demo dataset ──────────────────────────────────────────────────────────────
#
# Fictional members with gamer-handle names. Power numbers chosen to look
# realistic for a mid-game alliance (60-110M power range). Birthdays spread
# across the calendar with one within the next 14 days so the "upcoming
# birthdays" view always shows something.

DEMO_MEMBERS = [
    # name, 1st squad power, 2nd, 3rd, THP, total kills, birthday MM-DD
    ("ShadowHunter",   91_400_000, 78_200_000, 62_100_000, 231_700_000,  4_820_000, "01-14"),
    ("IronFist",       88_900_000, 75_600_000, 60_800_000, 225_300_000,  4_410_000, "02-08"),
    ("Valeria",        96_200_000, 81_700_000, 65_300_000, 243_200_000,  5_120_000, "03-22"),
    ("Krieger",        72_300_000, 64_100_000, 51_400_000, 187_800_000,  3_240_000, "04-03"),
    ("Phoenix99",     108_700_000, 92_400_000, 76_200_000, 277_300_000,  6_840_000, "05-17"),
    ("StormBreaker",   84_500_000, 71_900_000, 58_600_000, 215_000_000,  4_080_000, "06-29"),
    ("NightOwl",       67_800_000, 58_300_000, 47_200_000, 173_300_000,  2_910_000, "07-11"),
    ("Saber",          81_200_000, 69_800_000, 56_400_000, 207_400_000,  3_960_000, "08-26"),
    ("Echo",           74_600_000, 63_400_000, 51_900_000, 189_900_000,  3_310_000, "09-04"),
    ("Vortex",         99_300_000, 84_200_000, 68_100_000, 251_600_000,  5_470_000, "10-19"),
    ("Frostbite",      77_400_000, 65_700_000, 53_800_000, 196_900_000,  3_580_000, "11-02"),
    ("Maverick",       89_600_000, 76_300_000, 61_700_000, 227_600_000,  4_530_000, "12-15"),
    ("Cipher",         70_200_000, 60_100_000, 48_900_000, 179_200_000,  3_010_000, "01-28"),
    ("Hawthorne",      85_900_000, 72_800_000, 59_300_000, 218_000_000,  4_140_000, "03-09"),
    ("Tempest",        93_100_000, 79_600_000, 64_200_000, 236_900_000,  4_920_000, "04-21"),
    ("Onyx",           82_700_000, 70_400_000, 57_100_000, 210_200_000,  3_870_000, "05-30"),
    ("Ravenshield",    76_300_000, 64_800_000, 52_700_000, 193_800_000,  3_440_000, "07-08"),
    ("Sable",          88_200_000, 74_900_000, 60_500_000, 223_600_000,  4_290_000, "08-17"),
    ("Arclight",      102_400_000, 87_100_000, 70_900_000, 260_400_000,  5_780_000, "10-04"),
    ("Wraithborn",     69_700_000, 59_500_000, 48_300_000, 177_500_000,  2_960_000, "12-22"),
]

# Events — the actual LW recurring events leadership commonly schedules.
DEMO_EVENTS = [
    {
        "short_key":          "plague_marauder",
        "name":               "Plague Marauder (AE)",
        "default_time":       "21:00",
        "schedule_type":      "repeating",
        "anchor_date":        "",  # filled in at runtime
        "interval_days":      3,
        "announcement_blurb": "Plague Marauder (AE) at {time} ({server_time} Server Time). Make sure to have offline participation checked!",
        "active":             1,
    },
    {
        "short_key":          "zombie_siege",
        "name":               "Zombie Siege",
        "default_time":       "20:00",
        "schedule_type":      "repeating",
        "anchor_date":        "",
        "interval_days":      4,
        "announcement_blurb": "Zombie Siege at {time} ({server_time} Server Time). Capital city defense — bring your strongest squad!",
        "active":             1,
    },
    {
        "short_key":          "glacieradon",
        "name":               "Glacieradon",
        "default_time":       "22:00",
        "schedule_type":      "repeating",
        "anchor_date":        "",
        "interval_days":      7,
        "announcement_blurb": "Glacieradon at {time} ({server_time} Server Time). Get in voice 5 minutes before the start.",
        "active":             1,
    },
]


# ── Seeding functions ────────────────────────────────────────────────────────

def seed_core_config(args) -> None:
    """Seed guild_configs with a complete /setup row."""
    cfg = config.GuildConfig(
        guild_id                  = args.guild_id,
        leadership_channel_id     = args.leadership_channel,
        announcement_channel_id   = args.post_channel,
        member_role_id            = args.member_role,
        member_role_name          = args.member_role_name,
        leadership_role_name      = args.leadership_role_name,
        survey_channel_id         = args.post_channel,
        survey_notify_channel_id  = args.leadership_channel,
        ds_log_channel_id         = args.leadership_channel,
        cs_log_channel_id         = args.leadership_channel,
        event_draft_channel_id    = args.leadership_channel,
        event_announce_channel_id = args.post_channel,
        event_draft_time          = "12:00",
        event_five_min_warning    = 1,
        spreadsheet_id            = args.sheet_id,
        timezone                  = "America/New_York",
        tab_train_schedule        = "Train Schedule",
        tab_ds_assignments        = "DS Assignments",
        tab_sitouts               = "DS-CS Sit-outs",
        tab_survey_history        = "Survey History",
        tab_member_default        = "Squad Powers",
        setup_complete            = True,
    )
    config.save_config(cfg)
    print(f"  ✓ guild_configs row written (guild_id={args.guild_id})")


def seed_events(args) -> None:
    """Seed a few recurring event configs with anchor_date = today."""
    today_iso = date.today().isoformat()
    for ev in DEMO_EVENTS:
        event = dict(ev)
        event["anchor_date"]             = today_iso
        event["timezone"]                = "America/New_York"
        event["draft_channel_id"]        = args.leadership_channel
        event["announcement_channel_id"] = args.post_channel
        event["draft_time"]              = "12:00"
        event["five_min_warning"]        = 1
        config.save_guild_event(args.guild_id, event)
    print(f"  ✓ {len(DEMO_EVENTS)} events configured")


def seed_train(args) -> None:
    """Seed train config."""
    config.save_train_config(
        guild_id            = args.guild_id,
        tab_name            = "Train Schedule",
        themes              = list(DEFAULT_THEMES),
        tones               = list(DEFAULT_TONES),
        prompt_template     = DEFAULT_PROMPT,
        default_tone        = DEFAULT_TONES[0] if DEFAULT_TONES else "",
        blurbs_enabled      = 1,
        reminders_enabled   = 1,
        reminder_channel_id = args.post_channel,
        reminder_time       = "10:00",
    )
    print(f"  ✓ train config written")


def seed_birthdays(args) -> None:
    """Seed birthday config (sheet tab + columns).

    Column indices are 0-based to match the bot's parser (`row[name_col]`):
    A=0, B=1, C=2.
    """
    config.save_birthday_config(
        guild_id            = args.guild_id,
        tab_name            = "Birthdays",
        name_col            = 0,   # column A
        birthday_col        = 1,   # column B
        discord_id_col      = -1,
        data_start_row      = 2,
        enabled             = 1,
        train_integration   = 1,
        flexible_placement  = 1,
        lookahead_days      = 14,
        reminders_enabled   = 1,
        reminder_channel_id = args.post_channel,
        reminder_time       = "08:00",
    )
    print(f"  ✓ birthday config written")


def seed_storm(args) -> None:
    """Seed Desert Storm + Canyon Storm configs with the default mail template.

    Time slots aren't seeded — they're game-defined constants
    (DS_SERVER_TIMES / CS_SERVER_TIMES in config.py) rendered at display
    time from the guild's timezone.
    """
    for event_type in ("DS", "CS"):
        config.save_storm_config(
            guild_id           = args.guild_id,
            event_type         = event_type,
            tab_name           = f"{event_type} Assignments",
            mail_template      = DEFAULT_DS_TEMPLATE,
            timezone           = "America/New_York",
            log_channel_id     = args.leadership_channel,
            post_channel_id    = args.post_channel,
        )
    print(f"  ✓ DS + CS configs written")


def seed_survey(args) -> None:
    """Seed default survey config with the canonical question set."""
    config.save_survey_config(
        guild_id         = args.guild_id,
        tab_squad_powers = "Squad Powers",
        tab_history      = "Survey History",
        questions        = list(DEFAULT_SURVEY_QUESTIONS),
        intro_message    = DEFAULT_SURVEY_INTRO,
    )
    print(f"  ✓ survey config written")


def wipe_demo_extras(guild_id: int) -> None:
    """Delete user-added rows so the seed produces a true reset.

    The save_* helpers UPSERT the canonical config rows (one per guild), so
    those overwrite cleanly on re-seed. But two tables can grow with rows
    users add at runtime (extra events via /events, extra surveys via the
    Premium /survey flow). Without explicit deletes here, those rows
    accumulate across resets and the demo guild drifts further from the
    canonical state every time someone plays with it.
    """
    with config._get_conn() as conn:
        deleted_events = conn.execute(
            "DELETE FROM guild_events WHERE guild_id = ?", (guild_id,)
        ).rowcount
        deleted_surveys = conn.execute(
            "DELETE FROM guild_extra_surveys WHERE guild_id = ?", (guild_id,)
        ).rowcount
        conn.commit()
    print(f"  ✓ Cleared {deleted_events} extra events and {deleted_surveys} extra surveys")


def seed_growth(args) -> None:
    """Seed growth tracking with five demo metrics."""
    metrics = [
        {"label": "1st Squad Power", "col": "B"},
        {"label": "2nd Squad Power", "col": "C"},
        {"label": "3rd Squad Power", "col": "D"},
        {"label": "THP",             "col": "E"},
        {"label": "Total Kills",     "col": "F"},
    ]
    config.save_growth_config(
        guild_id           = args.guild_id,
        enabled            = 1,
        tab_source         = "Squad Powers",
        name_col           = "A",
        metrics            = metrics,
        tab_growth         = "Growth Tracking",
        snapshot_frequency = "monthly",
        snapshot_day       = 1,
        snapshot_interval  = 30,
        data_start_row     = 2,
    )
    print(f"  ✓ growth config written (5 metrics)")


# ── Sheet seeding (requires gspread creds) ───────────────────────────────────

def _ensure_tab(ss, name: str, rows: int = 100, cols: int = 12):
    """Return a worksheet with the given name, creating it if needed and
    clearing it if it already exists."""
    import gspread
    try:
        ws = ss.worksheet(name)
        ws.clear()
    except gspread.exceptions.WorksheetNotFound:
        ws = ss.add_worksheet(title=name, rows=rows, cols=cols)
    return ws


def _build_storm_assignment_rows(event_type: str,
                                 zones_a, subs_a,
                                 zones_b, subs_b) -> list:
    """Build the row list for a DS/CS Assignments tab in the format the
    bot's storm parser expects: section markers (DS_A_ZONES / DS_A_SUBS /
    DS_B_ZONES / DS_B_SUBS), zone rows (col A = zone name, col B = comma-
    separated members), sub-pair rows (col A = starter, col B = sub),
    blank separator rows between sections.
    """
    prefix = event_type.upper()  # "DS" or "CS"
    rows = []
    for team, zones, subs in [("A", zones_a, subs_a), ("B", zones_b, subs_b)]:
        rows.append([f"{prefix}_{team}_ZONES", ""])
        for zone, members in zones:
            rows.append([zone, ", ".join(members)])
        rows.append(["", ""])
        rows.append([f"{prefix}_{team}_SUBS", ""])
        for starter, sub in subs:
            rows.append([starter, sub])
        rows.append(["", ""])
    return rows


def seed_sheet(args) -> None:
    """Write the demo data tabs into the configured Google Sheet."""
    print("  Connecting to Sheet…")
    ss = config.get_spreadsheet(args.guild_id)

    # Squad Powers — the single source-of-truth roster tab
    ws = _ensure_tab(ss, "Squad Powers", rows=len(DEMO_MEMBERS) + 5, cols=8)
    rows = [["Name", "1st Squad Power", "2nd Squad Power",
             "3rd Squad Power", "THP", "Total Kills"]]
    rows.extend([m[0], m[1], m[2], m[3], m[4], m[5]] for m in DEMO_MEMBERS)
    ws.update(values=rows, range_name="A1")
    print(f"  ✓ Squad Powers tab — {len(DEMO_MEMBERS)} members")

    # Birthdays — name + M/D birthday in the slash format the bot's parser
    # accepts (`parse_birthday` regex matches `^(\d{1,2})/(\d{1,2})...`).
    # First member's birthday is always within the next 14 days so the
    # /birthdays preview always finds at least one upcoming.
    ws = _ensure_tab(ss, "Birthdays", rows=len(DEMO_MEMBERS) + 5, cols=3)
    today    = date.today()
    upcoming_dt = today + timedelta(days=5)
    upcoming    = f"{upcoming_dt.month}/{upcoming_dt.day}"
    rows = [["Name", "Birthday"]]
    for i, m in enumerate(DEMO_MEMBERS):
        if i == 0:
            bday = upcoming
        else:
            month, day = m[6].split("-")
            bday = f"{int(month)}/{int(day)}"
        rows.append([m[0], bday])
    ws.update(values=rows, range_name="A1")
    print(f"  ✓ Birthdays tab — {len(DEMO_MEMBERS)} members (next 14 days: {upcoming})")

    # Train Schedule — a week of past entries + this week. Three columns:
    # date, member name, blurb.
    ws = _ensure_tab(ss, "Train Schedule", rows=30, cols=4)
    rows = [["Date", "Member", "Blurb"]]
    for offset in range(-7, 7):
        d = today + timedelta(days=offset)
        member = DEMO_MEMBERS[(offset + 7) % len(DEMO_MEMBERS)][0]
        blurb = (f"{member} drives the train today — pile in!"
                 if offset >= 0 else
                 f"{member} drove the train.")
        rows.append([d.isoformat(), member, blurb])
    ws.update(values=rows, range_name="A1")
    print(f"  ✓ Train Schedule tab — 14 days (7 past, 7 upcoming)")

    # Survey History — last 6 weeks of submissions, half the roster each week.
    ws = _ensure_tab(ss, "Survey History", rows=200, cols=8)
    rows = [["Timestamp", "Name", "1st Squad Power", "2nd Squad Power",
             "3rd Squad Power", "THP", "Total Kills"]]
    for week in range(6, 0, -1):
        ts = (datetime.now() - timedelta(weeks=week)).strftime("%Y-%m-%d %H:%M")
        for m in DEMO_MEMBERS[: len(DEMO_MEMBERS) // 2 + week % 3]:
            # Tiny per-week growth so the numbers feel plausible
            decay = 0.985 ** week
            rows.append([
                ts, m[0],
                int(m[1] * decay), int(m[2] * decay), int(m[3] * decay),
                int(m[4] * decay), int(m[5] * decay),
            ])
    ws.update(values=rows, range_name="A1")
    print(f"  ✓ Survey History tab — 6 weeks of submissions")

    # Growth Tracking — three months of historical snapshots so screenshots
    # of /growth and the Sheet show real period-over-period change.
    growth_metrics = ["1st Squad Power", "2nd Squad Power",
                      "3rd Squad Power", "THP", "Total Kills"]
    growth_periods = []  # (month_label, decay_factor)
    today_dt = datetime.now()
    for offset in (2, 1, 0):
        month_dt = (today_dt.replace(day=15) - timedelta(days=offset * 30))
        growth_periods.append((month_dt.strftime("%b %Y"), 0.94 + 0.03 * (2 - offset)))
    ws = _ensure_tab(ss, "Growth Tracking",
                     rows=len(DEMO_MEMBERS) + 5,
                     cols=1 + len(growth_metrics) * len(growth_periods))
    header = ["Name"]
    for label, _ in growth_periods:
        header.extend(f"{m} ({label})" for m in growth_metrics)
    rows = [header]
    for m in DEMO_MEMBERS:
        row = [m[0]]  # name
        for _, decay in growth_periods:
            row.extend([
                int(m[1] * decay),  # 1st squad
                int(m[2] * decay),  # 2nd squad
                int(m[3] * decay),  # 3rd squad
                int(m[4] * decay),  # THP
                int(m[5] * decay),  # total kills
            ])
        rows.append(row)
    ws.update(values=rows, range_name="A1")
    print(f"  ✓ Growth Tracking tab — {len(growth_periods)} periods × {len(growth_metrics)} metrics")

    # DS Assignments — real game-defined zones, balanced 10-member teams
    # (snake-draft by 1st-squad power so neither team has a power advantage
    # in the demo screenshots). One zone per team is left empty to show
    # alliances don't always fill every slot.
    ds_zones_a = [
        ("Nuclear Silo",      ["Phoenix99"]),
        ("Oil Refinery I",    ["Valeria"]),
        ("Oil Refinery II",   ["Tempest"]),
        ("Science Hub",       ["IronFist"]),
        ("Info Center",       ["Sable"]),
        ("Field Hospital I",  ["Onyx"]),
        ("Field Hospital II", ["Saber"]),
        ("Field Hospital III",["Echo"]),
        ("Field Hospital IV", ["Krieger"]),
        ("Arsenal",           ["NightOwl"]),
        ("Mercenary Factory", []),  # not filled this week
    ]
    ds_subs_a = [
        ("Phoenix99",  "Onyx"),
        ("Valeria",    "Tempest"),
        ("IronFist",   "Saber"),
    ]
    ds_zones_b = [
        ("Nuclear Silo",      ["Arclight"]),
        ("Oil Refinery I",    ["Vortex"]),
        ("Oil Refinery II",   ["ShadowHunter"]),
        ("Science Hub",       ["Maverick"]),
        ("Info Center",       ["Hawthorne"]),
        ("Field Hospital I",  ["StormBreaker"]),
        ("Field Hospital II", ["Frostbite"]),
        ("Field Hospital III",["Ravenshield"]),
        ("Field Hospital IV", ["Cipher"]),
        ("Arsenal",           ["Wraithborn"]),
        ("Mercenary Factory", []),
    ]
    ds_subs_b = [
        ("Arclight",   "Hawthorne"),
        ("Vortex",     "Frostbite"),
        ("Maverick",   "Ravenshield"),
    ]
    ds_rows = _build_storm_assignment_rows("DS", ds_zones_a, ds_subs_a, ds_zones_b, ds_subs_b)
    ws = _ensure_tab(ss, "DS Assignments", rows=len(ds_rows) + 5, cols=2)
    ws.update(values=ds_rows, range_name="A1")
    print(f"  ✓ DS Assignments tab — Team A + Team B rosters populated")

    # CS Assignments — real game-defined zones with the same team split.
    # Two Sample Warehouses left empty per team since CS has 12 zones and
    # each team has 10 members.
    cs_zones_a = [
        ("Virus Lab",            ["Phoenix99"]),
        ("Power Tower",          ["Valeria"]),
        ("Data Center I",        ["Tempest"]),
        ("Data Center II",       ["IronFist"]),
        ("Defense System I",     ["Sable"]),
        ("Defense System II",    ["Onyx"]),
        ("Serum Factory I",      ["Saber"]),
        ("Serum Factory II",     ["Echo"]),
        ("Sample Warehouse I",   ["Krieger"]),
        ("Sample Warehouse II",  ["NightOwl"]),
        ("Sample Warehouse III", []),
        ("Sample Warehouse IV",  []),
    ]
    cs_subs_a = [
        ("Phoenix99",  "Onyx"),
        ("Valeria",    "Tempest"),
        ("IronFist",   "Echo"),
    ]
    cs_zones_b = [
        ("Virus Lab",            ["Arclight"]),
        ("Power Tower",          ["Vortex"]),
        ("Data Center I",        ["ShadowHunter"]),
        ("Data Center II",       ["Maverick"]),
        ("Defense System I",     ["Hawthorne"]),
        ("Defense System II",    ["StormBreaker"]),
        ("Serum Factory I",      ["Frostbite"]),
        ("Serum Factory II",     ["Ravenshield"]),
        ("Sample Warehouse I",   ["Cipher"]),
        ("Sample Warehouse II",  ["Wraithborn"]),
        ("Sample Warehouse III", []),
        ("Sample Warehouse IV",  []),
    ]
    cs_subs_b = [
        ("Arclight",   "Hawthorne"),
        ("Vortex",     "Frostbite"),
        ("Maverick",   "Ravenshield"),
    ]
    cs_rows = _build_storm_assignment_rows("CS", cs_zones_a, cs_subs_a, cs_zones_b, cs_subs_b)
    ws = _ensure_tab(ss, "CS Assignments", rows=len(cs_rows) + 5, cols=2)
    ws.update(values=cs_rows, range_name="A1")
    print(f"  ✓ CS Assignments tab — Team A + Team B rosters populated")


# ── Boot-time entry point (called from bot.py when SEED_DEMO_ON_BOOT=1) ─────

def seed_demo_guild_from_env() -> None:
    """Run the full demo seed using values pulled from env vars.

    Designed to be called from `bot.py` at startup when `SEED_DEMO_ON_BOOT=1`,
    so the seed runs against the production bot's mounted SQLite + gspread
    credentials without any extra Railway-volume gymnastics.

    Required env vars (all string-form integers except the role-name and
    sheet-id ones):
        SEED_DEMO_GUILD_ID
        SEED_DEMO_SHEET_ID
        SEED_DEMO_LEADERSHIP_CHANNEL
        SEED_DEMO_LEADERSHIP_ROLE
        SEED_DEMO_MEMBER_ROLE

    Optional env vars (with sensible defaults):
        SEED_DEMO_LEADERSHIP_ROLE_NAME   default "Leadership"
        SEED_DEMO_MEMBER_ROLE_NAME       default "Member"
        SEED_DEMO_POST_CHANNEL           default = leadership channel
        SEED_DEMO_SKIP_SHEET             "1" to skip the gspread half
    """
    import argparse, os

    required = [
        "SEED_DEMO_GUILD_ID",
        "SEED_DEMO_SHEET_ID",
        "SEED_DEMO_LEADERSHIP_CHANNEL",
        "SEED_DEMO_LEADERSHIP_ROLE",
        "SEED_DEMO_MEMBER_ROLE",
    ]
    missing = [k for k in required if not os.getenv(k)]
    if missing:
        print(f"[SEED] Missing required env vars: {', '.join(missing)}")
        print(f"[SEED] Aborting — set them in Railway and redeploy.")
        return

    leadership_channel = int(os.getenv("SEED_DEMO_LEADERSHIP_CHANNEL"))
    args = argparse.Namespace(
        guild_id              = int(os.getenv("SEED_DEMO_GUILD_ID")),
        sheet_id              = os.getenv("SEED_DEMO_SHEET_ID"),
        leadership_channel    = leadership_channel,
        leadership_role       = int(os.getenv("SEED_DEMO_LEADERSHIP_ROLE")),
        leadership_role_name  = os.getenv("SEED_DEMO_LEADERSHIP_ROLE_NAME", "Leadership"),
        member_role           = int(os.getenv("SEED_DEMO_MEMBER_ROLE")),
        member_role_name      = os.getenv("SEED_DEMO_MEMBER_ROLE_NAME", "Member"),
        post_channel          = int(os.getenv("SEED_DEMO_POST_CHANNEL", str(leadership_channel))),
        skip_sheet            = os.getenv("SEED_DEMO_SKIP_SHEET") == "1",
    )

    print(f"[SEED] Seeding demo data for guild {args.guild_id}…")
    print(f"[SEED]   DB: {config.DB_PATH}")
    print(f"[SEED]   Sheet: {args.sheet_id}")

    config.init_db()

    print(f"[SEED] Bot SQLite:")
    wipe_demo_extras(args.guild_id)
    seed_core_config(args)
    seed_events(args)
    seed_train(args)
    seed_birthdays(args)
    seed_storm(args)
    seed_survey(args)
    seed_growth(args)

    if args.skip_sheet:
        print(f"[SEED] Sheet writes skipped (SEED_DEMO_SKIP_SHEET=1).")
    else:
        print(f"[SEED] Google Sheet:")
        try:
            seed_sheet(args)
        except Exception as e:
            print(f"[SEED]   ✗ Sheet write failed: {type(e).__name__}: {e}")
            print(f"[SEED]   (DB seeding succeeded; re-run with proper creds or set")
            print(f"[SEED]    SEED_DEMO_SKIP_SHEET=1 and populate the Sheet manually.)")
            return

    print(f"[SEED] Done. Set SEED_DEMO_ON_BOOT=0 (or remove the env vars) for the next deploy.")


# ── CLI entry point ──────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(
        description="Populate a demo guild with fictional alliance data.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--guild-id",            type=int, required=True,
                   help="Discord guild ID of the demo server")
    p.add_argument("--sheet-id",            type=str, required=True,
                   help="Google Sheet ID for the demo alliance")
    p.add_argument("--leadership-channel",  type=int, required=True,
                   help="Channel ID for leadership-only commands and drafts")
    p.add_argument("--leadership-role",     type=int, required=True,
                   help="Role ID for the leadership role")
    p.add_argument("--leadership-role-name", type=str, default="Leadership",
                   help="Exact name of the leadership role (the bot gates feature commands by name match). Default: 'Leadership'")
    p.add_argument("--member-role",         type=int, required=True,
                   help="Role ID for the alliance-member role")
    p.add_argument("--member-role-name",    type=str, default="Member",
                   help="Exact name of the member role. Default: 'Member'")
    p.add_argument("--post-channel",        type=int, default=None,
                   help="Channel ID for public posts (events, surveys, storm). Defaults to leadership channel.")
    p.add_argument("--skip-sheet",          action="store_true",
                   help="Only seed the SQLite DB; skip Google Sheet writes")

    args = p.parse_args()
    if args.post_channel is None:
        args.post_channel = args.leadership_channel

    print(f"Seeding demo data for guild {args.guild_id}…")
    print(f"  DB: {config.DB_PATH}")
    print(f"  Sheet: {args.sheet_id}")
    print()

    config.init_db()

    print("Bot SQLite:")
    wipe_demo_extras(args.guild_id)
    seed_core_config(args)
    seed_events(args)
    seed_train(args)
    seed_birthdays(args)
    seed_storm(args)
    seed_survey(args)
    seed_growth(args)
    print()

    if args.skip_sheet:
        print("Sheet writes skipped (--skip-sheet).")
        print("Populate the Sheet manually if you want full screenshots.")
    else:
        print("Google Sheet:")
        try:
            seed_sheet(args)
        except Exception as e:
            print(f"  ✗ Sheet write failed: {type(e).__name__}: {e}")
            print(f"  (DB seeding succeeded — re-run with proper creds to fill the Sheet,")
            print(f"   or pass --skip-sheet and populate it manually.)")
            return 1

    print()
    print("Done. Next steps:")
    print(f"  1. In Discord, run /view_configuration in your demo server's leadership channel")
    print(f"     to confirm everything looks wired up.")
    print(f"  2. Take screenshots of /help, /train, /birthdays, /desertstorm_draft, etc.")
    print(f"  3. Re-run this script any time the demo data drifts (it's idempotent).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
