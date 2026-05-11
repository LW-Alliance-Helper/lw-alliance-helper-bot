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
    {"key": "squad1_power",  "label": "1st Squad Power",           "type": "numeric",  "options": [],                                                      "placeholder": "e.g. 43.27", "max_chars": 5, "magnitude": "M"},
    {"key": "squad1_type",   "label": "1st Squad Type",            "type": "dropdown", "options": ["Missile", "Air", "Tank"],                              "placeholder": "Select squad type...", "max_chars": 0},
    {"key": "squad2_power",  "label": "2nd Squad Power",           "type": "numeric",  "options": [],                                                      "placeholder": "e.g. 43.27", "max_chars": 5, "magnitude": "M"},
    {"key": "squad3_power",  "label": "3rd Squad Power",           "type": "numeric",  "options": [],                                                      "placeholder": "e.g. 43.27", "max_chars": 5, "magnitude": "M"},
    {"key": "drone_level",   "label": "Drone Level",               "type": "numeric",  "options": [],                                                      "placeholder": "e.g. 243",   "max_chars": 5, "magnitude": "raw"},
    {"key": "gorilla_level", "label": "Gorilla Level",             "type": "numeric",  "options": [],                                                      "placeholder": "e.g. 70",    "max_chars": 5, "magnitude": "raw"},
    {"key": "thp",           "label": "Total Hero Power (THP)",    "type": "numeric",  "options": [],                                                      "placeholder": "e.g. 301",   "max_chars": 5, "magnitude": "M"},
    {"key": "total_kills",   "label": "Total Kills",               "type": "numeric",  "options": [],                                                      "placeholder": "e.g. 55.40", "max_chars": 5, "magnitude": "M"},
    {"key": "profession",    "label": "Profession",                "type": "dropdown", "options": ["War Leader", "Engineer"],                              "placeholder": "Select profession...", "max_chars": 0},
    {"key": "banner",        "label": "Charge Banner",             "type": "dropdown", "options": ["Yes", "No"],                                           "placeholder": "Select...",  "max_chars": 0},
    {"key": "aid_removal",   "label": "Medical Aid / Ruin Removal","type": "dropdown", "options": ["Yes", "Only Medical Aid", "Only Ruin Removal", "No"], "placeholder": "Select...",  "max_chars": 0},
]

DEFAULT_SURVEY_INTRO = (
    "Please fill out this survey each week, if possible, to help keep track of "
    "squad powers, better balance Desert Storm teams, track alliance growth, "
    "and prepare for season events!"
)


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


# ── Shiny Tasks daily announcement (free tier) ──────────────────────────────
#
# `{servers}` renders as a comma-separated list with an "and" before the
# last entry (e.g. "681, 682, 689 and 706"). `{date}` is the calendar
# date the bot computed against, in the guild's timezone, formatted as
# e.g. "Monday, May 11". Unknown placeholders render literally via
# SafeDict so a typo doesn't crash the scheduler loop.

DEFAULT_SHINY_TASKS_MESSAGE = (
    "🌟 Daily shiny tasks are available on servers: {servers}."
)
