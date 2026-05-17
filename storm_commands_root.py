"""
Root cog for the consolidated `/desertstorm` and `/canyonstorm` slash command
trees (#143).

Before this cog, storm functionality was spread across 15+ top-level slash
commands. Live-test feedback was emphatic ("way way too many commands") so
everything except the `/setup_desertstorm` / `/setup_canyonstorm` wizards
is nested under two parent groups here.

Each subcommand is a thin dispatcher into the relevant feature module:

  storm.py                 — overview, draft
  storm_log.py             — participation, log, remind
  storm_signup_post.py     — post_signup
  storm_officer_view.py    — signups
  storm_attendance.py      — attendance
  storm_strategy.py        — strategy ▶ create/edit/list/delete/apply/roster_history
  storm_member_rules.py    — member_rule ▶ set_power_band/set_member_team/set_member_zone/list

The feature modules expose `handle_*` coroutines (or build_*_group factory
functions for the strategy / member_rule subgroups) that take an explicit
`bot` arg. This lets the per-feature modules stay free of slash-command
machinery and keeps the root cog small and easy to audit.

Setup wizards (`/setup_desertstorm`, `/setup_canyonstorm`) intentionally
stay at the top level — matches the `/setup_<feature>` convention shared
by train, growth, birthdays, survey, etc.
"""

from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands


def _build_desertstorm_group(bot: commands.Bot) -> app_commands.Group:
    grp = app_commands.Group(
        name="desertstorm",
        description="Desert Storm commands",
        guild_only=True,
    )

    @grp.command(name="overview",
                 description="Show the configured Desert Storm setup and current rosters")
    async def overview(interaction: discord.Interaction):
        from storm import handle_storm_overview
        await handle_storm_overview(bot, interaction, "DS")

    @grp.command(name="draft",
                 description="Free text mail template. For team setup, use /desertstorm signups instead")
    async def draft(interaction: discord.Interaction):
        from storm import handle_storm_draft
        await handle_storm_draft(bot, interaction, "DS")

    @grp.command(name="remind",
                 description="💎 DM every roster member to participate in this week's Desert Storm")
    async def remind(interaction: discord.Interaction):
        from storm_log import handle_storm_remind
        await handle_storm_remind(bot, interaction, "DS")

    @grp.command(name="participation",
                 description="Log Desert Storm participation data")
    async def participation(interaction: discord.Interaction):
        from storm_log import handle_storm_participation
        await handle_storm_participation(bot, interaction, "DS")

    @grp.command(name="log",
                 description="View a Desert Storm log entry (defaults to today)")
    @app_commands.describe(date="Optional date, e.g. 'April 14' or '4/14' (defaults to today)")
    async def log(interaction: discord.Interaction, date: Optional[str] = None):
        from storm_log import handle_storm_log
        await handle_storm_log(bot, interaction, "DS", date)

    @grp.command(name="post_signup",
                 description="💎 Post a sign-up message for an upcoming Desert Storm event (Premium)")
    @app_commands.describe(
        event_date="Optional — defaults to the next configured event day. Accepts e.g. May 18, 5/18, 2026-05-18, Sunday.",
    )
    async def post_signup(interaction: discord.Interaction, event_date: Optional[str] = None):
        from storm_signup_post import handle_post_signup
        await handle_post_signup(bot, interaction, "DS", event_date)

    @grp.command(name="signups",
                 description="💎 View signups + Set up Team A/B rosters for a Desert Storm event (Premium)")
    @app_commands.describe(
        event_date="Optional — defaults to the next configured event day. Accepts e.g. May 18, 5/18, Sunday.",
    )
    async def signups(interaction: discord.Interaction, event_date: Optional[str] = None):
        from storm_officer_view import handle_storm_signups
        await handle_storm_signups(bot, interaction, "DS", event_date)

    @grp.command(name="attendance",
                 description="💎 Record who showed for an assigned Desert Storm event (Premium)")
    @app_commands.describe(
        event_date="Optional — defaults to the most recent posted event. Accepts e.g. May 18, 5/18, yesterday.",
    )
    async def attendance(interaction: discord.Interaction, event_date: Optional[str] = None):
        from storm_attendance import handle_storm_attendance
        await handle_storm_attendance(bot, interaction, "DS", event_date)

    # Nested subgroups: strategy + member_rule.
    from storm_strategy import build_ds_strategy_group
    from storm_member_rules import build_ds_member_rule_group
    grp.add_command(build_ds_strategy_group())
    grp.add_command(build_ds_member_rule_group())

    return grp


def _build_canyonstorm_group(bot: commands.Bot) -> app_commands.Group:
    grp = app_commands.Group(
        name="canyonstorm",
        description="Canyon Storm commands",
        guild_only=True,
    )

    @grp.command(name="overview",
                 description="Show the configured Canyon Storm setup and current rosters")
    async def overview(interaction: discord.Interaction):
        from storm import handle_storm_overview
        await handle_storm_overview(bot, interaction, "CS")

    @grp.command(name="draft",
                 description="Free text mail template. For team setup, use /canyonstorm signups instead")
    async def draft(interaction: discord.Interaction):
        from storm import handle_storm_draft
        await handle_storm_draft(bot, interaction, "CS")

    @grp.command(name="remind",
                 description="💎 DM every roster member to participate in this week's Canyon Storm")
    async def remind(interaction: discord.Interaction):
        from storm_log import handle_storm_remind
        await handle_storm_remind(bot, interaction, "CS")

    @grp.command(name="participation",
                 description="Log Canyon Storm participation data")
    async def participation(interaction: discord.Interaction):
        from storm_log import handle_storm_participation
        await handle_storm_participation(bot, interaction, "CS")

    @grp.command(name="log",
                 description="View a Canyon Storm log entry (defaults to today)")
    @app_commands.describe(date="Optional date, e.g. 'April 14' or '4/14' (defaults to today)")
    async def log(interaction: discord.Interaction, date: Optional[str] = None):
        from storm_log import handle_storm_log
        await handle_storm_log(bot, interaction, "CS", date)

    @grp.command(name="post_signup",
                 description="💎 Post a sign-up message for an upcoming Canyon Storm event (Premium)")
    @app_commands.describe(
        event_date="Optional — defaults to the next configured event day. Accepts e.g. May 18, 5/18, 2026-05-18, Sunday.",
    )
    async def post_signup(interaction: discord.Interaction, event_date: Optional[str] = None):
        from storm_signup_post import handle_post_signup
        await handle_post_signup(bot, interaction, "CS", event_date)

    @grp.command(name="signups",
                 description="💎 View signups + Set up Team A/B rosters for a Canyon Storm event (Premium)")
    @app_commands.describe(
        event_date="Optional — defaults to the next configured event day. Accepts e.g. May 18, 5/18, Sunday.",
    )
    async def signups(interaction: discord.Interaction, event_date: Optional[str] = None):
        from storm_officer_view import handle_storm_signups
        await handle_storm_signups(bot, interaction, "CS", event_date)

    @grp.command(name="attendance",
                 description="💎 Record who showed for an assigned Canyon Storm event (Premium)")
    @app_commands.describe(
        event_date="Optional — defaults to the most recent posted event. Accepts e.g. May 18, 5/18, yesterday.",
    )
    async def attendance(interaction: discord.Interaction, event_date: Optional[str] = None):
        from storm_attendance import handle_storm_attendance
        await handle_storm_attendance(bot, interaction, "CS", event_date)

    from storm_strategy import build_cs_strategy_group
    from storm_member_rules import build_cs_member_rule_group
    grp.add_command(build_cs_strategy_group())
    grp.add_command(build_cs_member_rule_group())

    return grp


class StormCommandsRootCog(commands.Cog):
    """Hosts the `/desertstorm` and `/canyonstorm` parent groups."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.desertstorm_group = _build_desertstorm_group(bot)
        self.canyonstorm_group = _build_canyonstorm_group(bot)
        bot.tree.add_command(self.desertstorm_group)
        bot.tree.add_command(self.canyonstorm_group)

    async def cog_unload(self):
        try:
            self.bot.tree.remove_command(self.desertstorm_group.name)
            self.bot.tree.remove_command(self.canyonstorm_group.name)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(StormCommandsRootCog(bot))
