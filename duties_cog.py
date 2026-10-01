"""Leadership Duties (#687): the `/duties` command, the reminder loop, and
the listener that opens a departed holder's positions.

The hub itself is `duties_hub`; the member-facing buttons and threads are
`duties_panel`; sending a reminder is `duties_reminders`.
"""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks

import duties_copy as c
import duties_health  # noqa: F401 - registers the config_health subjects
import duties_panel
import duties_posts
import duties_reminders

logger = logging.getLogger(__name__)


class DutiesCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self) -> None:
        # Survives a redeploy: the contact buttons carry their duty id in
        # their custom id, and the Close and My duties buttons are one static
        # id each.
        duties_panel.register_persistent_items(self.bot)
        duties_posts.register_persistent_items(self.bot)
        self.reminder_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()

    @app_commands.command(name=c.DUTIES_CMD.lstrip("/"), description=c.CMD_DESCRIPTION)
    @app_commands.guild_only()
    async def duties(self, interaction: discord.Interaction):
        from duties_hub import open_hub

        await open_hub(self.bot, interaction)

    @tasks.loop(minutes=1)
    async def reminder_loop(self):
        import config

        # An exception out of a `tasks.loop` body stops the loop until the
        # next restart. Each server's failures are already isolated inside
        # the pass; this catches the rest (a locked database on the read).
        try:
            await duties_reminders.run_reminder_tick(self.bot)
        except Exception as e:  # noqa: BLE001
            logger.exception("[DUTIES] reminder pass failed: %s", e)
            try:
                import sentry_sdk

                sentry_sdk.capture_exception(e)
            except Exception:  # noqa: BLE001
                pass
            return  # no heartbeat: this tick wasn't clean
        try:
            await asyncio.to_thread(config.stamp_loop_heartbeat, duties_reminders.HEARTBEAT)
        except Exception as e:  # noqa: BLE001
            logger.warning("[DUTIES] heartbeat stamp failed: %s", e)

    @reminder_loop.before_loop
    async def _before_reminder_loop(self):
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        try:
            await duties_reminders.handle_departure(self.bot, member)
        except Exception as e:  # noqa: BLE001 - a listener must not raise into discord.py
            logger.exception("[DUTIES] departure handling failed for %s: %s", member.id, e)


async def setup(bot):
    await bot.add_cog(DutiesCog(bot))
