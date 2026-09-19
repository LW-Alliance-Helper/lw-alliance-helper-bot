"""`/champion_duel` — opens the Champion Duel hub.

One bare command rather than a group, because a Discord command cannot be both
and the useful thing to type is `/champion_duel`: a member who wants a match's
odds should not have to already know that the next word is `predict`. What used
to be `/champion_duel edits|revert|export` are the same flows, now behind
buttons on the hub.

The command name is `champion_duel`, never `duel`. Champion Duel, Warzone Duel
and Alliance VS Duel are three different events, and `/vs` already owns the
third — a bare `/duel` would be ambiguous the day the second one ships.

Everything the buttons drive lives in `champion_duel_hub.py`; this module is
just the slash-command registration, following `mapmanager_cog.py`.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

import champion_duel_hub


class ChampionDuelCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="champion_duel",
        description="Champion Duel odds, player scouting and match predictions",
    )
    @app_commands.guild_only()
    async def champion_duel(self, interaction: discord.Interaction):
        await champion_duel_hub.handle_champion_duel_hub(self.bot, interaction)


async def setup(bot: commands.Bot):
    await bot.add_cog(ChampionDuelCog(bot))
