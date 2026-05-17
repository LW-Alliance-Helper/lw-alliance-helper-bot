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


PRIVACY_URL = (
    "https://lw-alliance-helper.github.io/privacy.html#where-your-data-lives"
)

OVERVIEW_DESCRIPTION = (
    "Commands require the leadership role and the leadership channel. "
    "Run `/setup` first if you haven't configured the bot.\n"
    f"🗂️ Your alliance data lives in your own Google Sheet — see [Privacy]({PRIVACY_URL})."
)

ALWAYS_HANDY = (
    "`/setup` — Roles, leadership channel, timezone, Google Sheet\n"
    "`/view_configuration` — View all configured settings\n"
    "`/setup_reset` — Clear configuration and start over\n"
    "`/cancel` — Cancel any active wizard\n"
    "`/help` — Show this menu\n"
    "`/donate` — 💖 Tip-jar links\n"
    "`/upgrade` — 💎 Subscribe and pin Premium here\n"
    "`/premium_assign` — 💎 Move Premium to this server\n"
    "`/premium_status` — 💎 Subscription state and assigned server\n"
    "`/premium_unassign` — 💎 Release the pin (subscription stays)"
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
            "Drafts post to leadership for review, then to your public channel."
        ),
        "commands": [
            ("/setup_events",
             "Configure events, leadership and public channels, daily draft "
             "time, and the 5-min warning."),
            ("/events [date]",
             "Open the event editor for today or a chosen date. Edit, "
             "approve, and post."),
            ("/events_log",
             "Show approved event posts (free: 7 days / 💎 Premium: 30 days)."),
        ],
    },
    "train": {
        "emoji": "🚂",
        "label": "Train Schedule",
        "description": (
            "Track who's assigned the alliance train each day; optionally "
            "generate a personalised ChatGPT blurb prompt."
        ),
        "commands": [
            ("/setup_train",
             "Configure the train tab, blurb generation, and reminders."),
            ("/train",
             "View the schedule with Add / Update / Generate Prompt / Clear "
             "buttons."),
            ("/train_log [date]",
             "Recent prompt log entries (free: 7 days / 💎 Premium: 30 days)."),
            ("/train_addbirthdays",
             "Manually run the birthday → train auto-population now."),
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
            ("/setup_birthdays",
             "Configure birthday tracking, train integration, and "
             "announcement template."),
            ("/birthdays",
             "Show upcoming birthdays inside your lookahead window "
             "(default 14 days)."),
        ],
    },
    "desertstorm": {
        "emoji": "⚔️",
        "label": "Desert Storm",
        "description": (
            "Run weekly Desert Storm with mail drafts, strategy presets, "
            "structured sign-ups + roster builder (💎 Premium), and "
            "configurable participation tracking."
        ),
        "commands": [
            ("/setup_desertstorm",
             "Configure Team rosters, log channel, public post channel, "
             "mail template, and (💎 Premium) the structured-flow sign-up "
             "channel, schedule, and Sheet tabs."),
            ("/desertstorm overview",
             "Show current rosters and the active mail template."),
            ("/desertstorm draft",
             "**Free-tier text mail template only.** Step through team → "
             "time → template, preview, and post the mail blurb. **Does "
             "not** open the roster builder — for assigning members to "
             "zones and Approve & Post, use `/desertstorm signups` "
             "instead (💎 Premium)."),
            ("/desertstorm strategy <create | edit | list | delete | "
             "apply | roster_history>",
             "Manage strategy presets — saved zone layouts with optional "
             "per-zone power minimums. `apply` opens the roster builder "
             "against your full roster (free tier). `roster_history` "
             "browses past structured rosters with attendance overlay "
             "(💎 Premium)."),
            ("/desertstorm member_rule <set_power_band | set_member_team "
             "| set_member_zone | list>",
             "Manage member rules: power-band eligibility "
             "(`≥ 250M → Power Tower`) plus per-member overrides "
             "(`Alice always plays Team A`, `Bob always plays Power "
             "Tower`)."),
            ("/desertstorm post_signup [event_date]",
             "💎 Post a sign-up message in the configured channel; "
             "members click buttons to register Team A / Team B / Either "
             "/ Cannot."),
            ("/desertstorm signups [event_date]",
             "💎 **Main hub for structured-flow events.** Leadership view "
             "of who's signed up; record on-behalf votes for non-Discord "
             "roster members; click **Set up Team A** / **Set up Team B** "
             "to open the roster builder filtered to that team's signed-"
             "up members; Approve & Post posts the finished mail."),
            ("/desertstorm attendance [event_date]",
             "💎 Record who attended each assigned slot after the event; "
             "writes to the attendance Sheet tab."),
            ("/desertstorm participation",
             "Run this week's participation log using your configured "
             "questions."),
            ("/desertstorm log [date]",
             "View a saved log entry (free: 4 most recent / 💎 Premium: "
             "full history)."),
            ("/desertstorm remind",
             "💎 DM the roster to participate this week."),
        ],
    },
    "canyonstorm": {
        "emoji": "🏜️",
        "label": "Canyon Storm",
        "description": (
            "Same shape as Desert Storm — mail drafts, strategy presets, "
            "structured sign-ups + roster builder (💎 Premium), and "
            "configurable participation."
        ),
        "commands": [
            ("/setup_canyonstorm",
             "Configure Team rosters, log channel, public post channel, "
             "mail template, and the structured-flow sign-up channel, "
             "schedule, and Sheet tabs."),
            ("/canyonstorm overview",
             "Show current rosters and the active mail template."),
            ("/canyonstorm draft",
             "**Free-tier text mail template only.** Step through team → "
             "time → template, preview, and post the mail blurb. **Does "
             "not** open the roster builder — for assigning members to "
             "zones and Approve & Post, use `/canyonstorm signups` "
             "instead (💎 Premium)."),
            ("/canyonstorm strategy <create | edit | list | delete | "
             "apply | roster_history>",
             "Manage strategy presets — saved zone layouts with optional "
             "per-zone power minimums. `apply` opens the roster builder "
             "against your full roster (free tier). `roster_history` "
             "browses past structured rosters with attendance overlay "
             "(💎 Premium)."),
            ("/canyonstorm member_rule <set_power_band | set_member_team "
             "| set_member_zone | list>",
             "Manage member rules: power-band eligibility "
             "(`≥ 250M → Power Tower`) plus per-member overrides "
             "(`Alice always plays Team A`, `Charlie is always at "
             "Power Tower`). `set_member_team` only applies when CS is "
             "configured for both teams in setup."),
            ("/canyonstorm post_signup [event_date]",
             "💎 Post a sign-up message in the configured channel; "
             "members click buttons to register their availability."),
            ("/canyonstorm signups [event_date]",
             "💎 **Main hub for structured-flow events.** Leadership view "
             "of who's signed up; record on-behalf votes for non-Discord "
             "roster members; click **Set up Team A** / **Set up Team B** "
             "to open the roster builder; Approve & Post finalises the "
             "roster."),
            ("/canyonstorm attendance [event_date]",
             "💎 Record who attended each assigned slot after the event; "
             "writes to the attendance Sheet tab."),
            ("/canyonstorm participation",
             "Run this week's participation log using your configured "
             "questions."),
            ("/canyonstorm log [date]",
             "View a saved log entry (free: 4 most recent / 💎 Premium: "
             "full history)."),
            ("/canyonstorm remind",
             "💎 DM the roster to participate this week."),
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
            ("/setup_survey",
             "Configure questions, channels, sheet tabs, and the intro."),
            ("/survey",
             "View configured surveys. 💎 Premium gets Add / Edit / Remove "
             "for managing multiple."),
            ("/survey_post",
             "Post or repost the answer button."),
            ("/survey_remind",
             "Send now or schedule. Free: channel post. 💎 Premium: also DM "
             "via roster."),
        ],
    },
    "growth": {
        "emoji": "📈",
        "label": "Growth Tracking",
        "description": (
            "Periodic snapshots of member stats, written to your sheet. "
            "Each snapshot also classifies members into growth buckets — "
            "click **📊 See most recent Breakdown** on `/growth` to see who's climbing and "
            "who's stalled."
        ),
        "commands": [
            ("/setup_growth",
             "Configure source tab, metrics, and snapshot schedule."),
            ("/setup_growth_breakdown",
             "💎 Configure the breakdown auto-post, bucket thresholds, "
             "and bucket labels."),
            ("/growth",
             "Show status with options to snapshot, view the breakdown, "
             "or edit config."),
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
            ("/setup_shiny_tasks",
             "Configure the announcement channel, server range, post "
             "time, and message body."),
        ],
    },
    "data_portability": {
        "emoji": "📦",
        "label": "Data Portability",
        "description": (
            "Move your alliance's bot config to a new Discord server, or "
            "snapshot it as a backup you can restore later. Your alliance "
            "data lives in your Google Sheet either way — these commands "
            "carry the bot's wizard answers (templates, channels, schedules) "
            "alongside it."
        ),
        "commands": [
            ("/export_config",
             "DMs you a JSON file with the categories you select "
             "(core setup, events, DS, CS, train, birthday, growth, "
             "surveys, shiny tasks, member roster)."),
            ("/import_config <file>",
             "Apply a /export_config JSON to this server. The bot walks "
             "you through remapping each old channel and role to its new "
             "equivalent, then writes the imported config to your tables."),
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
            ("/setup_members",
             "Configure Member Roster Sync — writes Discord IDs to your "
             "sheet so other features find members by name."),
            ("/sync_members",
             "Manually re-sync the roster now."),
            ("Multiple named surveys",
             "Manage from `/survey` directly via Add / Edit / Remove."),
            ("DM-mode reminders",
             "`/survey_remind`, `/desertstorm remind`, `/canyonstorm remind` "
             "all gain DM-via-roster delivery; survey reminders can also "
             "schedule recurring DMs."),
            ("✨ More",
             "Personal birthday DMs, train-assignment DMs, auto-mention "
             "members in train reminders, threads as destinations, "
             "multi-template train and storm support, advanced question "
             "types (single-select, multi-select, date)."),
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
        title=f"🤖 Alliance Helper — Commands  ·  {badge}",
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
            text="Pick a category below — or run /upgrade to unlock Premium.",
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
    embed.set_footer(text="Pick another category from the dropdown — or 🏠 Overview to go back.")
    return embed


class HelpCategorySelect(discord.ui.Select):
    def __init__(self, is_premium: bool):
        self.is_premium = is_premium
        options = [
            discord.SelectOption(
                label="Overview", value="__overview", emoji="🏠",
                description="Back to the main /help screen",
            ),
        ]
        for cat_id, cat in HELP_CATEGORIES.items():
            options.append(discord.SelectOption(
                label=cat["label"], value=cat_id, emoji=cat["emoji"],
            ))
        super().__init__(
            placeholder="Choose a category…",
            options=options, min_values=1, max_values=1,
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

    def __init__(self, is_premium: bool, *,
                 origin: Optional[discord.Interaction] = None):
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
