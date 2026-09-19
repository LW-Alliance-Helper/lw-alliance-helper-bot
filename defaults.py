"""Framework-level default values for new alliances.

Single source of truth for everything an alliance gets when they run
``/setup`` and don't pick custom values: train themes / tones / prompt
template, the default survey question set, the default survey intro,
and the neutral storm mail templates.

Lives here (rather than in ``config.py``) so it's easy to find and edit
when adjusting the bot's "out of the box" experience. Any new feature
that introduces shippable defaults should add them to this module too,
keeping the defaults list in one searchable place.

These values are intentionally **generic / Last War**-flavoured, not
alliance-flavoured. If a default reads as if it were written for a
specific alliance (uses "us" / "our", references a specific server
or season, etc.), that's a bug — please neutralise the wording.
"""

from __future__ import annotations


# ── Train blurb wizard ──────────────────────────────────────────────────────

DEFAULT_THEMES = [
    "Welcome to the Alliance",
    "Birthday",
    "Milestone",
    "War / Performance",
    "General Celebration",
    "Contest / Raffle",
    "Custom",
]

DEFAULT_TONES = [
    "Default (match the theme)",
    "More casual",
    "More intense",
    "Funny",
    "Serious",
    "Cinematic / Dramatic",
]

DEFAULT_PROMPT = (
    "You are writing a short motivational alliance announcement blurb for a mobile strategy game called Last War.\n"
    "Keep it under 3 sentences. It should feel energetic and personal.\n\n"
    "Member name: {name}\n"
    "Theme: {theme}\n"
    "Tone: {tone}\n"
    "Notes: {notes}\n\n"
    "Write the blurb now:"
)


# ── Squad-power survey ──────────────────────────────────────────────────────

DEFAULT_SURVEY_QUESTIONS = [
    {
        "key": "squad1_power",
        "label": "1st Squad Power",
        "type": "numeric",
        "options": [],
        "placeholder": "e.g. 43.27",
        "max_chars": 5,
        "magnitude": "M",
    },
    {
        "key": "squad1_type",
        "label": "1st Squad Type",
        "type": "dropdown",
        "options": ["Missile", "Air", "Tank"],
        "placeholder": "Select squad type...",
        "max_chars": 0,
    },
    {
        "key": "squad2_power",
        "label": "2nd Squad Power",
        "type": "numeric",
        "options": [],
        "placeholder": "e.g. 43.27",
        "max_chars": 5,
        "magnitude": "M",
    },
    {
        "key": "squad3_power",
        "label": "3rd Squad Power",
        "type": "numeric",
        "options": [],
        "placeholder": "e.g. 43.27",
        "max_chars": 5,
        "magnitude": "M",
    },
    {
        "key": "drone_level",
        "label": "Drone Level",
        "type": "numeric",
        "options": [],
        "placeholder": "e.g. 243",
        "max_chars": 5,
        "magnitude": "raw",
    },
    {
        "key": "gorilla_level",
        "label": "Gorilla Level",
        "type": "numeric",
        "options": [],
        "placeholder": "e.g. 70",
        "max_chars": 5,
        "magnitude": "raw",
    },
    {
        "key": "thp",
        "label": "Total Hero Power (THP)",
        "type": "numeric",
        "options": [],
        "placeholder": "e.g. 301",
        "max_chars": 5,
        "magnitude": "M",
    },
    {
        "key": "total_kills",
        "label": "Total Kills",
        "type": "numeric",
        "options": [],
        "placeholder": "e.g. 55.40",
        "max_chars": 5,
        "magnitude": "M",
    },
    {
        "key": "profession",
        "label": "Profession",
        "type": "dropdown",
        "options": ["War Leader", "Engineer"],
        "placeholder": "Select profession...",
        "max_chars": 0,
    },
    {
        "key": "banner",
        "label": "Charge Banner",
        "type": "dropdown",
        "options": ["Yes", "No"],
        "placeholder": "Select...",
        "max_chars": 0,
    },
    {
        "key": "aid_removal",
        "label": "Medical Aid / Ruin Removal",
        "type": "dropdown",
        "options": ["Yes", "Only Medical Aid", "Only Ruin Removal", "No"],
        "placeholder": "Select...",
        "max_chars": 0,
    },
]

DEFAULT_SURVEY_INTRO = (
    "Please fill out this survey each week, if possible, to help keep track of "
    "squad powers, better balance Desert Storm teams, track alliance growth, "
    "and prepare for season events!"
)


# ── Survey templates ────────────────────────────────────────────────────────
#
# A survey is a generic thing: a set of questions, a tab holding each
# member's latest answers, and a tab holding every submission. The
# squad-power question set above is one *template* for that, not the
# baseline every other survey deviates from — it's simply the set the
# module was first built around.
#
# Adding a template is data, not code: append an entry here and it shows
# up in the "Add Survey" picker. Each entry supplies the question set,
# the suggested tab names, a prefilled intro, and the wizard copy that
# should surround it, so a template can speak in its own language.
#
# Template dict fields:
#   key / name / emoji / description — picker UI.
#   suggested_survey_name — offered as the survey's display name.
#   tab_responses / tab_history — suggested tab names. `None` means
#       "derive them from the survey name" (what SCRATCH does).
#   questions / intro_message — what the wizard prefills.
#   *_step_label / *_step_prompt / intro_step_example — the per-step
#       wizard copy this template speaks in.

SURVEY_TEMPLATE_SCRATCH = "scratch"
SURVEY_TEMPLATE_SQUAD_POWER = "squad_power"

SURVEY_TEMPLATES = {
    SURVEY_TEMPLATE_SQUAD_POWER: {
        "key": SURVEY_TEMPLATE_SQUAD_POWER,
        "name": "Squad Power Survey",
        "emoji": "⚔️",
        "description": "Squad powers and types, drone and gorilla levels, profession, and more.",
        "suggested_survey_name": "Squad Power Survey",
        "tab_responses": "Squad Powers",
        "tab_history": "Survey History",
        "questions": DEFAULT_SURVEY_QUESTIONS,
        "intro_message": DEFAULT_SURVEY_INTRO,
        "responses_step_label": "Member Statistics Tab",
        "responses_step_prompt": (
            "Which tab should hold each member's current statistics? "
            "We update their row here every time they submit."
        ),
        "history_step_label": "Survey History Tab",
        "history_step_prompt": (
            "Which tab should hold the full history of every submission? "
            "We add a row here each time, so you keep a record over time."
        ),
        "intro_step_example": (
            "Please fill out this survey each week to help us track squad powers, "
            "balance our teams, and prepare for season events!"
        ),
    },
    SURVEY_TEMPLATE_SCRATCH: {
        "key": SURVEY_TEMPLATE_SCRATCH,
        "name": "Start from scratch",
        "emoji": "✏️",
        "description": "Build your own questions from nothing.",
        "suggested_survey_name": None,
        "tab_responses": None,
        "tab_history": None,
        "questions": [],
        "intro_message": "",
        "responses_step_label": "Current Answers Tab",
        "responses_step_prompt": (
            "Which tab should hold each member's current answers? "
            "We update their row here every time they submit."
        ),
        "history_step_label": "Answer History Tab",
        "history_step_prompt": (
            "Which tab should hold the full history of every submission? "
            "We add a row here each time, so you keep a record over time."
        ),
        "intro_step_example": (
            "Please take a moment to fill this out. It helps leadership keep "
            "our records current and plan around what everyone tells us."
        ),
    },
}

# Order the picker offers real templates in. `scratch` is deliberately
# absent — it's the other half of the fork, not a list entry.
SURVEY_TEMPLATE_PICKER_ORDER = [SURVEY_TEMPLATE_SQUAD_POWER]

# Google caps a tab name at 100 chars and rejects these characters
# outright. A survey named "Q3: Buff Agreement?" must not produce a tab
# name the Sheets API refuses to create.
_SHEET_TAB_FORBIDDEN = str.maketrans({c: " " for c in "[]*?/\\:"})
_SHEET_TAB_MAX = 100


def survey_template(key: str | None) -> dict:
    """Return a template dict by key, falling back to the scratch template.

    Guilds configured before templates existed carry `squad_power`
    (backfilled), and anything unrecognised degrades to scratch rather
    than raising mid-wizard.
    """
    return SURVEY_TEMPLATES.get(key or "", SURVEY_TEMPLATES[SURVEY_TEMPLATE_SCRATCH])


def sanitize_sheet_tab_name(name: str, *, fallback: str = "Survey") -> str:
    """Coerce arbitrary text into something Sheets will accept as a tab name."""
    cleaned = " ".join((name or "").translate(_SHEET_TAB_FORBIDDEN).split())
    cleaned = cleaned.strip("'")[:_SHEET_TAB_MAX].strip()
    return cleaned or fallback


def derive_survey_tab_names(survey_name: str) -> tuple[str, str]:
    """Suggest the two tab names for a survey with no template-supplied ones.

    Every survey needs its *own* pair of tabs — two surveys sharing a
    history tab append their answers positionally under the first
    survey's headers. Deriving both from the survey name means the
    suggested defaults are already unique, so leadership never has to
    reason about it.
    """
    base = sanitize_sheet_tab_name(survey_name)
    history_suffix = " History"
    history_base = base[: _SHEET_TAB_MAX - len(history_suffix)].strip()
    return base, f"{history_base}{history_suffix}"


# ── Storm mail templates (neutral baseline; alliances customise via /setup) ──

DEFAULT_DS_TEMPLATE = """\
**{alliance_name} — Desert Storm**

**Zone Assignments**
{zones}

**Subs**
{subs}

**Time:** {time}"""

DEFAULT_CS_TEMPLATE = """\
**{alliance_name} — Canyon Storm**

**Zone Assignments**
{zones}

**Subs**
{subs}

**Time:** {time}"""


# ── Storm roster DM templates (Premium — fired from Approve & Post) ────────
#
# Custom-able per (guild, event_type) via the storm setup wizard.
# Placeholders are SafeDict-substituted at send time — a typo in a
# saved template renders literally in the DM instead of crashing the
# fan-out loop and leaving the rest of the roster un-DM'd.
#
# Placeholders:
#   {name}        — member's display name (alias when configured, else
#                   the Discord display name)
#   {event_label} — "Desert Storm" or "Canyon Storm"
#   {team_blurb}  — " Team A" / " Team B" / "" (leading space included
#                   when present, so `{event_label}{team_blurb}` reads
#                   naturally for both team-bound DS and team-less CS)
#   {date}        — formatted event date (e.g. "Thursday, May 28, 2026")
#   {time}        — team time-slot label (e.g. "4pm EDT (18:00 server time)")
#   {assignments} — bullet / line list of zones+stages (Starter) or
#                   "Sub for X" lines (Paired Sub); empty for Pool Sub
#
# These templates intentionally use the alliance's voice ("we have
# you as a Starter", "our roster") — distinct from the mail templates
# which post to a public channel. The DMs are alliance-to-member
# nudges that read better in first-person plural; alliances can
# customise the wording via the setup wizard if they prefer otherwise.

DEFAULT_ROSTER_DM_STARTER = """\
👋 Hey {name},

We have you as a Starter for our {event_label}{team_blurb} roster for {date} at {time}.

Your assignments:
{assignments}

Please let us know if you aren't able to participate!"""

DEFAULT_ROSTER_DM_PAIRED_SUB = """\
👋 Hey {name},

We have you as a Sub for our {event_label}{team_blurb} roster for {date} at {time}.

Your assignment(s):
{assignments}

Please let us know if you aren't able to participate!"""

DEFAULT_ROSTER_DM_POOL_SUB = """\
👋 Hey {name},

We have you as a Sub for our {event_label}{team_blurb} roster for {date} at {time}.

Please let us know if you aren't able to participate!"""


# ── Profession Buddy System DM (#289, Premium) ──────────────────────────────
#
# Sent to a member when their profession buddy changes. `{name}` is the
# recipient, `{buddy}` the partner's name, and `{buddy_role}` the partner's
# profession (War Leader / Engineer). Unknown placeholders render literally
# via the SafeDict-backed renderer, so a typo can't crash the DM loop.

DEFAULT_BUDDY_DM = (
    "🤝 Hey {name}, your profession buddy is now **{buddy}** ({buddy_role}). "
    "Coordinate your daily Skill so it always gets used. Good luck out there!"
)


# ── Transfer Management in-game message templates (#16, Premium) ─────────────
#
# Three editable templates the recruiter renders on demand via
# `/transfer_template <kind> <candidate>` (and the buttons on the transfer
# status-change notification). The rendered text is meant to be copied into
# in-game chat, so it carries no Discord markup. Customised per guild via
# `/setup_transfers` Step 9; an empty saved value means "use the default
# here." Placeholders are SafeDict-substituted at render time
# (transfer.render_transfer_template) so a typo like `{nme}` renders
# literally instead of crashing.
#
# Available placeholders (any subset is fine — unmapped ones render blank):
#   {name}           — candidate's in-game name
#   {alliance_name}  — the alliance's name (the guild's configured name)
#   {tier}           — proposed tier, when a Tier column is mapped
#   {power}          — total power, when a Power column is mapped

DEFAULT_TRANSFER_APPLY = (
    "Hey {name}, we're recruiting for {alliance_name} and think you'd be a great fit. "
    "Want to hear what we're about?"
)

DEFAULT_TRANSFER_CONFIRM = (
    "Hi {name}, we'd love to have you on {alliance_name}. Can you confirm you're "
    "transferring so we can hold your spot?"
)

DEFAULT_TRANSFER_DECLINE = (
    "Hey {name}, thanks for considering {alliance_name}. We're going a different "
    "direction this season, but best of luck out there!"
)

# Maps the template "kind" (used in config field names, the wizard, and the
# `/transfer_template` command) to its hardcoded default body. The config
# column for each kind is `template_<kind>` on guild_transfer_config.
DEFAULT_TRANSFER_TEMPLATES = {
    "apply_invitation": DEFAULT_TRANSFER_APPLY,
    "confirm_request": DEFAULT_TRANSFER_CONFIRM,
    "decline": DEFAULT_TRANSFER_DECLINE,
}


# ── Shiny Tasks daily announcement (free tier) ──────────────────────────────
#
# `{servers}` renders as a comma-separated list with an "and" before the
# last entry (e.g. "681, 682, 689 and 706"). `{date}` is the calendar
# date the bot computed against, in the guild's timezone, formatted as
# e.g. "Monday, May 11". Unknown placeholders render literally via
# SafeDict so a typo doesn't crash the scheduler loop.

DEFAULT_SHINY_TASKS_MESSAGE = "🌟 Daily shiny tasks are available on servers: {servers}."


# ── Storm participation question presets (#247) ─────────────────────────────
#
# Templates surfaced in the storm setup wizard's Step 6.6 picker.
# Officers pick zero or more; each selection adds a pre-configured
# question to the participation question list. Officer can keep
# customising (re-label, re-key, edit type-specific fields) via the
# regular question builder afterward.
#
# Field contract (matches the participation question dict shape used
# by `get_participation_config` and the run_log_flow walker):
#   key            — stable identifier; doubles as the Sheet column.
#                    `showed_up` is the canonical attendance column
#                    (see #245 — `storm_log.ATTENDANCE_QUESTION_KEY`).
#   label          — officer-facing question label.
#   type           — one of the question-type ids (#244).
#   description    — one-line "what this does" for the picker UI.
#   emoji          — picker-UI glyph (matches the ticket spec).
#   default_checked — checked by default in the picker.
#   source_question_key — derived_count only: the question to count.
#   lookback_events — derived_count only: how many past events to scan.
#   prefill_source  — roster_multi_select only: `"discord_poll"` for
#                    the Premium auto-prefill variant.

STORM_PARTICIPATION_PRESETS_FREE = [
    {
        "key": "showed_up",
        "label": "Did this member show up?",
        "type": "roster_multi_select",
        "description": "Roster multi-select. Tracks attendance per event.",
        "emoji": "✅",
        "default_checked": True,
    },
    {
        "key": "sat_out",
        "label": "Who sat out this week?",
        "type": "roster_multi_select",
        "description": "Roster multi-select against your full roster.",
        "emoji": "✏️",
    },
    {
        "key": "didnt_vote",
        "label": "Who didn't vote this week?",
        "type": "roster_multi_select",
        "description": "Roster multi-select. Manual selection only on free tier.",
        "emoji": "🗳️",
    },
]

STORM_PARTICIPATION_PRESETS_PREMIUM = [
    {
        "key": "sit_out_count_4",
        "label": "Sit-out count, past 4 events",
        "type": "derived_count",
        "description": 'Derived count from the "Who sat out?" question.',
        "emoji": "📊",
        "source_question_key": "sat_out",
        "lookback_events": 4,
    },
    {
        "key": "vote_miss_count_8",
        "label": "Vote-miss count, past 8 events",
        "type": "derived_count",
        "description": 'Derived count from the "Who didn\'t vote?" question.',
        "emoji": "📊",
        "source_question_key": "didnt_vote",
        "lookback_events": 8,
    },
    {
        "key": "didnt_vote_autoprefill",
        "label": "Who didn't vote this week? (auto-prefill from poll)",
        "type": "roster_multi_select",
        "description": "Variant of the above that pre-checks members from the Discord signup poll.",
        "emoji": "🗳️",
        "prefill_source": "discord_poll",
    },
]


def storm_participation_presets(is_premium: bool) -> list[dict]:
    """Return the preset list visible to a guild at its current tier."""
    if is_premium:
        return STORM_PARTICIPATION_PRESETS_FREE + STORM_PARTICIPATION_PRESETS_PREMIUM
    return STORM_PARTICIPATION_PRESETS_FREE


def preset_to_question(preset: dict) -> dict:
    """Convert a preset entry into the question dict shape the
    participation flow expects. Strips picker-UI fields (description,
    emoji, default_checked) and keeps only the run-time question
    fields."""
    runtime_keys = {
        "key",
        "label",
        "type",
        "source_question_key",
        "lookback_events",
        "prefill_source",
    }
    return {k: v for k, v in preset.items() if k in runtime_keys}
