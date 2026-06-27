"""`/map_manager` command — opens the Map Manager hub (6C, #316; hub redesign #338).

A single admin-only `/map_manager` command that opens the status hub
(`mapmanager_hub.handle_mapmanager_hub`), where Set up / Change / Unlink live as
buttons. The hub, its modals, and the MM link helpers live in `mapmanager_hub.py`;
this module is just the slash-command registration. See
`docs/BOT_INTEGRATION_HANDOFF.md` in the Map Manager repo for the contract.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import mapmanager_hub


class MapManagerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(
        name="map_manager",
        description="Connect this server's alliance to the Map Manager web app",
    )
    @app_commands.guild_only()
    async def map_manager(self, interaction: discord.Interaction):
        await mapmanager_hub.handle_mapmanager_hub(self.bot, interaction)


async def setup(bot):
    await bot.add_cog(MapManagerCog(bot))
