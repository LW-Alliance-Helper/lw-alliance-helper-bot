"""buddy_cog.py — registers the `/buddy` command for the Profession Buddy
System (#289).

`/buddy` is a single top-level hub command (it opens an embed + button grid via
buddy_hub.handle_buddy_hub), the same shape as `/train` and the storm hubs. No
background loop is needed: the buddy tab and the persistent self-service message
are edited on demand, not on a schedule. The persistent profession view is
re-registered on startup from bot.py via
buddy_ui.register_persistent_buddy_views.
"""

import discord
from discord import app_commands
from discord.ext import commands


class BuddyCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="buddy",
        description="Profession buddy lookup and management for this alliance",
    )
    @app_commands.guild_only()
    async def buddy(self, interaction: discord.Interaction):
        from buddy_hub import handle_buddy_hub

        await handle_buddy_hub(self.bot, interaction)


async def setup(bot):
    await bot.add_cog(BuddyCog(bot))
