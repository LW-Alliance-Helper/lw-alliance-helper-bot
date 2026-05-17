"""
Root cog for the consolidated `/desertstorm` and `/canyonstorm` slash
commands (#143 then #187).

Each command is a single top-level slash that opens an event hub
embed plus button grid (`storm_event_hub.handle_event_hub`). The hub
buttons dispatch into the same feature modules the pre-#187 subcommands
used:

  storm.py                 ▶ draft (free-tier mail template)
  storm_log.py             ▶ participation, log, remind
  storm_signup_post.py     ▶ post_signup
  storm_officer_view.py    ▶ signups
  storm_attendance.py      ▶ attendance
  storm_history.py         ▶ past rosters
  storm_strategy.py        ▶ strategy preset list view
  storm_member_rules.py    ▶ member-rule list view

Pre-#187 each surface had its own slash subcommand. Live-test feedback
from the dev validation pass said the 11-subcommand-per-event-type
shape was confusing for first-time officers (e.g. `/desertstorm draft`
read like "draft a roster" but generated mail; the structured-flow
roster building lived under `/desertstorm signups`). The hub puts
every action behind a labeled button so the verb is visible on every
clickable element.

Setup wizards (`/setup_desertstorm`, `/setup_canyonstorm`) intentionally
stay at the top level — matches the `/setup_<feature>` convention shared
by train, growth, birthdays, survey, etc.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class StormCommandsRootCog(commands.Cog):
    """Registers `/desertstorm` and `/canyonstorm` as single top-level
    commands that open the event hub. No subcommands — every action
    reachable via hub buttons."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

        @app_commands.command(
            name="desertstorm",
            description="Open the Desert Storm hub for this alliance",
        )
        @app_commands.guild_only()
        async def desertstorm(interaction: discord.Interaction):
            from storm_event_hub import handle_event_hub
            await handle_event_hub(self.bot, interaction, "DS")

        @app_commands.command(
            name="canyonstorm",
            description="Open the Canyon Storm hub for this alliance",
        )
        @app_commands.guild_only()
        async def canyonstorm(interaction: discord.Interaction):
            from storm_event_hub import handle_event_hub
            await handle_event_hub(self.bot, interaction, "CS")

        self.desertstorm_cmd = desertstorm
        self.canyonstorm_cmd = canyonstorm
        bot.tree.add_command(self.desertstorm_cmd)
        bot.tree.add_command(self.canyonstorm_cmd)

    async def cog_unload(self):
        try:
            self.bot.tree.remove_command(self.desertstorm_cmd.name)
            self.bot.tree.remove_command(self.canyonstorm_cmd.name)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(StormCommandsRootCog(bot))
