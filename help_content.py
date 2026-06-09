"""Content + interactive view for /help.

`/help` opens an overview embed with a category dropdown. Picking a
category swaps in a per-category embed listing each command with a
short blurb. Centralising the content here keeps `bot.py` from growing
every time a feature is added — new commands just append a tuple to
the right category's `commands` list.
"""

from __future__ import annotations

from typing import Optional

import discord

from setup_hub import (
    HUB_BTN_BIRTHDAYS,
    HUB_BTN_BREAKDOWN,
    HUB_BTN_BUDDY,
    HUB_BTN_EVENTS,
    HUB_BTN_GROWTH,
    HUB_BTN_MEMBERS,
    HUB_BTN_SHINY,
    HUB_BTN_SURVEY,
    HUB_BTN_TRAIN,
    HUB_BTN_TRANSFERS,
    STORM_SETUP_NAV,
)


PRIVACY_URL = "https://lw-alliance-helper.github.io/privacy.html#where-your-data-lives"

OVERVIEW_DESCRIPTION = (
    "Commands require the leadership role and the leadership channel. "
    "Run `/setup` first if you haven't configured the bot.\n"
    f"🗂️ Your alliance data lives in your own Google Sheet. See [Privacy]({PRIVACY_URL})."
)

ALWAYS_HANDY = (
    "`/setup`: Setup hub. Foundations, every feature wizard, plus "
    "buttons to view full configuration or reset everything\n"
    "`/cancel`: Cancel any active wizard\n"
    "`/help`: Show this menu\n"
    "`/donate`: 💖 Tip-jar links\n"
    "`/upgrade`: 💎 Subscribe and pin Premium here\n"
    "`/premium overview`: 💎 Subscription state (doubles as upsell on free tier)\n"
    "`/premium assign`: 💎 Move Premium to this server\n"
    "`/premium unassign`: 💎 Release the pin (subscription stays)"
)


# Each category: emoji + label render in the dropdown and the embed
# title; description sits at the top of the category embed; commands
# is a list of (signature, blurb) tuples — one field per tuple.
HELP_CATEGORIES: dict[str, dict] = {
    "events": {
        "emoji": "📣",
        "label": "Event Announcements",
        "description": (
            "Schedule in-game events (Plague Marauder, Zombie Siege, etc). "
            "Drafts post to leadership for review, then to your public channel. "
            "Open `/events` to see every action in one place."
        ),
        "commands": [
            (
                f"/setup → {HUB_BTN_EVENTS}",
                "Configure the four shared event settings: leadership draft "
                "channel, public announcement channel, daily draft time, "
                "and 5-min warning toggle.",
            ),
            (
                "/events",
                "**Event hub.** Opens an embed showing the alliance's current "
                "event config plus a button grid.\n"
                "**Read row:** 📅 Today's events (open the draft editor), "
                "📆 Upcoming events (next firing dates), 📜 Event log "
                "(recent approvals — free: 7 days / 💎 Premium: 30 days).\n"
                "**Write row:** ➕ Create an event (pick a preset or define "
                "your own), 🗑️ Delete an event.",
            ),
        ],
    },
    "train": {
        "emoji": "🚂",
        "label": "Train Schedule",
        "description": (
            "Track who's assigned the alliance train each day; optionally "
            "generate a personalised ChatGPT blurb prompt, or let the bot "
            "pick fair conductors with Conductor Rotation."
        ),
        "commands": [
            (
                f"/setup → {HUB_BTN_TRAIN}",
                "Configure the train tab, blurb generation, reminders, and "
                "(optionally) turn on Conductor Rotation.",
            ),
            (
                "/train",
                "Open the train hub. With rotation on: this week's draft, schedule "
                "presets, member rules, assignment logs. With it off: the "
                "schedule overview, prompt log, and birthday check.",
            ),
            ("/birthdays", "Show upcoming birthdays from your member sheet."),
        ],
    },
    "birthdays": {
        "emoji": "🎂",
        "label": "Birthdays",
        "description": (
            "Track member birthdays from your sheet; optionally post "
            "announcements and auto-assign the train."
        ),
        "commands": [
            (
                f"/setup → {HUB_BTN_BIRTHDAYS}",
                "Configure birthday tracking, train integration, and announcement template.",
            ),
            (
                "/birthdays",
                "Show upcoming birthdays inside your lookahead window (default 14 days).",
            ),
        ],
    },
    "desertstorm": {
        "emoji": "⚔️",
        "label": "Desert Storm",
        "description": (
            "Run weekly Desert Storm with mail drafts, strategy presets, "
            "structured sign-ups + roster builder (💎 Premium), and "
            "configurable participation tracking. Open `/desertstorm` to "
            "see every action in one place."
        ),
        "commands": [
            (
                STORM_SETUP_NAV["DS"],
                "Configure Team rosters, log channel, public post channel, "
                "mail template, and (💎 Premium) the structured-flow sign-up "
                "channel, schedule, and Sheet tabs.",
            ),
            (
                "/desertstorm",
                "**Event hub.** Opens an embed showing the alliance's current "
                "config plus a button grid for every action.\n"
                "**Event-day row:** Post sign-up poll (💎), View sign-ups + "
                "set up teams (💎), Record attendance (💎), Fill out "
                "participation questions.\n"
                "**Comms + config row:** Send DM reminder to roster (💎), "
                "Manage strategy presets, Manage member rules, Generate mail.\n"
                "**Reference row:** View past participation logs, View past "
                "rosters (💎), Open setup.\n"
                "💎 buttons render disabled on the free tier so officers can "
                "see at a glance what `/upgrade` unlocks.",
            ),
        ],
    },
    "canyonstorm": {
        "emoji": "🏜️",
        "label": "Canyon Storm",
        "description": (
            "Same shape as Desert Storm: mail drafts, strategy presets, "
            "structured sign-ups + roster builder (💎 Premium), and "
            "configurable participation. Open `/canyonstorm` to see "
            "every action in one place."
        ),
        "commands": [
            (
                STORM_SETUP_NAV["CS"],
                "Configure Team rosters, log channel, public post channel, "
                "mail template, and (💎 Premium) the structured-flow sign-up "
                "channel, schedule, and Sheet tabs.",
            ),
            (
                "/canyonstorm",
                "**Event hub.** Opens an embed showing the alliance's current "
                "config plus a button grid for every action.\n"
                "**Event-day row:** Post sign-up poll (💎), View sign-ups + "
                "set up teams (💎), Record attendance (💎), Fill out "
                "participation questions.\n"
                "**Comms + config row:** Send DM reminder to roster (💎), "
                "Manage strategy presets, Manage member rules, Generate mail.\n"
                "**Reference row:** View past participation logs, View past "
                "rosters (💎), Open setup.\n"
                "💎 buttons render disabled on the free tier.",
            ),
        ],
    },
    "survey": {
        "emoji": "📋",
        "label": "Survey",
        "description": (
            "Collect member stats through a private Discord thread. Answers "
            "land in your sheet; leadership gets a notification per submission."
        ),
        "commands": [
            (
                f"/setup → {HUB_BTN_SURVEY}",
                "Configure questions, channels, sheet tabs, and the intro.",
            ),
            (
                "/survey overview",
                "View configured surveys. 💎 Premium gets Add / Edit / Remove "
                "for managing multiple.",
            ),
            ("/survey post", "Post or repost the answer button."),
            (
                "/survey remind",
                "Send now or schedule. Free: channel post. 💎 Premium: also DM via roster.",
            ),
        ],
    },
    "buddy": {
        "emoji": "🤝",
        "label": "Profession Buddy System",
        "description": (
            "Pair your War Leaders with Engineers so the Engineer's daily buff "
            "Skill always has a home. Professions come from your Squad Power "
            "Survey, and the buddy list lives in its own sheet tab."
        ),
        "commands": [
            (
                f"/setup → {HUB_BTN_BUDDY}",
                "Turn it on, pick the buddy tab, choose whether two Engineers "
                "can share a War Leader, set the scarcity priority, optionally "
                "rank Engineers by a reliability score you keep in your sheet, and "
                "pick the leadership-alert channel.",
            ),
            (
                "/buddy",
                "**Buddy hub.** Everyone can tap 🔍 Who's my buddy? or 📋 View "
                "buddy list. Leadership gets ✏️ Manage pairings (unpair / pair / "
                "re-pair) and 📤 Post buddy list.",
            ),
            (
                "💎 Auto-assign + self-service",
                "Premium adds 🪄 Auto-assign (keeps existing pairs), ♻️ Re-pair "
                "from scratch, 📌 one-click profession buttons members swap "
                "anytime, auto re-pairing with leadership alerts, and buddy DMs.",
            ),
        ],
    },
    "growth": {
        "emoji": "📈",
        "label": "Growth Tracking",
        "description": (
            "Periodic snapshots of member stats, written to your sheet. "
            "Each snapshot also classifies members into growth buckets. "
            "Click **📊 See most recent Breakdown** on `/growth overview` (or run `/growth breakdown` directly) to see who's climbing and "
            "who's stalled."
        ),
        "commands": [
            (f"/setup → {HUB_BTN_GROWTH}", "Configure source tab, metrics, and snapshot schedule."),
            (
                f"/setup → {HUB_BTN_BREAKDOWN}",
                "💎 Configure the breakdown auto-post, bucket thresholds, and bucket labels.",
            ),
            (
                "/growth overview",
                "Show status with options to snapshot, view the breakdown, or edit config.",
            ),
            (
                "/growth breakdown",
                "Jump straight to the most-recent bucket breakdown "
                "(Increased / Steady / Low / None / Decline).",
            ),
        ],
    },
    "shiny_tasks": {
        "emoji": "🌟",
        "label": "Shiny Tasks",
        "description": (
            "Daily auto-post of the Last War servers in your transfer range "
            "that have shiny tasks today."
        ),
        "commands": [
            (
                f"/setup → {HUB_BTN_SHINY}",
                "Configure the announcement channel, server range, post time, and message body.",
            ),
        ],
    },
    "transfers": {
        "emoji": "🔁",
        "label": "Transfer Management 💎",
        "description": (
            "💎 Premium. Watches your recruiting sheet and pings you when new "
            "applicants land or their status changes, drafts your in-game "
            "messages, and (optionally) pulls matching players from a "
            "server-wide sheet. Your sheet stays the source of truth. Open "
            "`/transfers` to see everything in one place."
        ),
        "commands": [
            (
                f"/setup → {HUB_BTN_TRANSFERS}",
                "💎 Run the setup wizard: point the bot at your transfer sheet, "
                "auto-map the columns, pick a notification channel + style, "
                "build a new-applicant filter, connect optional server-wide / "
                "intake-form sources, and turn on decision write-back.",
            ),
            (
                "/transfers",
                "💎 **Transfer hub.** Shows the watch status (sheet, channel, "
                "filter, sources) plus 📋 View applicants and ⚙️ Setup "
                "Transfers. New-applicant / status-change notices post to your "
                "chosen channel with 📄 Full details, draft-a-message buttons, "
                "and (if enabled) ✅ Set-status write-back.",
            ),
        ],
    },
    "data_portability": {
        "emoji": "📦",
        "label": "Data Portability",
        "description": (
            "Move your alliance's bot config to a new Discord server, or "
            "snapshot it as a backup you can restore later. Your alliance "
            "data lives in your Google Sheet either way; these commands "
            "carry the bot's wizard answers (templates, channels, schedules) "
            "alongside it."
        ),
        "commands": [
            (
                "/config overview",
                "What this guild has saved + pointers into /config export and /config import.",
            ),
            (
                "/config export",
                "DMs you a JSON file with the categories you select "
                "(core setup, events, DS, CS, train, birthday, growth, "
                "surveys, shiny tasks, member roster).",
            ),
            (
                "/config import <file>",
                "Apply a /config export JSON to this server. The bot walks "
                "you through remapping each old channel and role to its new "
                "equivalent, then writes the imported config to your tables.",
            ),
        ],
    },
    "premium": {
        "emoji": "💎",
        "label": "Premium Features",
        "description": (
            "Premium adds member-aware features that build on the free tier. "
            "Unlock with `/upgrade`."
        ),
        "commands": [
            (
                f"/setup → {HUB_BTN_MEMBERS}",
                "Configure Member Sync. Writes Discord IDs to your "
                "sheet so other features find members by name.",
            ),
            ("/members overview", "Roster source, last-sync time, and current state at a glance."),
            ("/members sync", "Manually re-sync the roster now."),
            (
                "Multiple named surveys",
                "Manage from `/survey overview` directly via Add / Edit / Remove.",
            ),
            (
                "DM-mode reminders",
                "`/survey remind` plus the **🔔 Send DM reminder to roster** "
                "button on `/desertstorm` and `/canyonstorm` all gain "
                "DM-via-roster delivery; survey reminders can also schedule "
                "recurring DMs.",
            ),
            (
                "✨ More",
                "Personal birthday DMs, train-assignment DMs, auto-mention "
                "members in train reminders, threads as destinations, "
                "multi-template train and storm support, advanced question "
                "types (single-select, multi-select, date).",
            ),
        ],
    },
}


def _tier_meta(is_premium: bool) -> tuple[str, discord.Color]:
    if is_premium:
        return "💎 Premium", discord.Color.gold()
    return "Free tier", discord.Color.blurple()


def build_overview_embed(is_premium: bool) -> discord.Embed:
    badge, color = _tier_meta(is_premium)
    embed = discord.Embed(
        title=f"🤖 Alliance Helper Commands  ·  {badge}",
        color=color,
        description=OVERVIEW_DESCRIPTION,
    )
    embed.add_field(name="🧰 Always handy", value=ALWAYS_HANDY, inline=False)
    if is_premium:
        embed.set_footer(
            text="💎 Premium is active. Pick a category below for details.",
        )
    else:
        embed.set_footer(
            text="Pick a category below, or run /upgrade to unlock Premium.",
        )
    return embed


def build_category_embed(category_id: str, is_premium: bool) -> discord.Embed:
    cat = HELP_CATEGORIES[category_id]
    badge, color = _tier_meta(is_premium)
    embed = discord.Embed(
        title=f"{cat['emoji']} {cat['label']}  ·  {badge}",
        color=color,
        description=cat["description"],
    )
    for name, value in cat["commands"]:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text="Pick another category from the dropdown, or 🏠 Overview to go back.")
    return embed


class HelpCategorySelect(discord.ui.Select):
    def __init__(self, is_premium: bool):
        self.is_premium = is_premium
        options = [
            discord.SelectOption(
                label="Overview",
                value="__overview",
                emoji="🏠",
                description="Back to the main /help screen",
            ),
        ]
        for cat_id, cat in HELP_CATEGORIES.items():
            options.append(
                discord.SelectOption(
                    label=cat["label"],
                    value=cat_id,
                    emoji=cat["emoji"],
                )
            )
        super().__init__(
            placeholder="Choose a category…",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        choice = self.values[0]
        if choice == "__overview":
            embed = build_overview_embed(self.is_premium)
        else:
            embed = build_category_embed(choice, self.is_premium)
        await interaction.response.edit_message(embed=embed, view=self.view)


class HelpView(discord.ui.View):
    """Dropdown-driven /help. Stores the originating interaction so the
    select can be disabled in place when the 3-min view timeout fires
    (matches the auto-post-timeout cleanup pattern used elsewhere).
    """

    def __init__(self, is_premium: bool, *, origin: Optional[discord.Interaction] = None):
        super().__init__(timeout=180)
        self.origin = origin
        self.add_item(HelpCategorySelect(is_premium))

    async def on_timeout(self):
        for item in self.children:
            if hasattr(item, "disabled"):
                item.disabled = True
        if self.origin is not None:
            try:
                await self.origin.edit_original_response(view=self)
            except discord.HTTPException:
                pass
