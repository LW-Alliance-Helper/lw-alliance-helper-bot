"""alliance_duel_cog.py — registers the `/vs` command for the Alliance Duel
(VS) tracker (#402).

Thin registration only, the same shape as `buddy_cog.py`. `/vs` is a single
flat hub command opening an embed plus button grid via
`alliance_duel_hub.handle_vs_hub`, not an `app_commands.Group` with named
subcommands: the design settles the hub pattern for a feature this stateful,
matching `/buddy`, `/train`, `/events`, `/transfers` and `/map_manager`.

The clock-driven loop for the daily score prompt and the member day-theme
reminder lands here in #405 / #406, following the `check_rotation` precedent in
`train_cog.py`. Nothing scheduled runs yet.
"""

import discord
from discord import app_commands
from discord.ext import commands


class AllianceDuelCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="vs",
        description="Alliance Duel (VS): bracket, this week, and scouting for this alliance",
    )
    @app_commands.guild_only()
    async def vs(self, interaction: discord.Interaction):
        from alliance_duel_hub import handle_vs_hub

        await handle_vs_hub(self.bot, interaction)


async def setup(bot):
    await bot.add_cog(AllianceDuelCog(bot))
