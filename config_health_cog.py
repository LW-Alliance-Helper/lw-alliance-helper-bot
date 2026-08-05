"""The clock behind :mod:`config_health`.

Recording a broken config is a synchronous DB write at the failure site
(``config_health.record``). This loop is the other half: every 15 minutes it
posts what each guild currently owes, batched into one digest.

Fifteen minutes is a deliberate middle. The failure sites are themselves on
loops measured in minutes to a day, so a shorter pass mostly re-scans an
unchanged table, and a longer one lets an alliance sit inside a broken
feature for most of an evening before hearing about it.

No outage catch-up adapter, unlike the member-facing post loops. Those
recover a *moment* that was missed while the bot was down. This recovers
nothing: the problem is durable state in ``guild_config_health``, so a pass
missed during an outage is fully covered by the next one. It stamps a
heartbeat anyway, matching ``transfer_poll``, since it is still a
clock-driven loop worth seeing in the loop-health view.
"""

from __future__ import annotations

import logging

from discord.ext import commands, tasks

import config
import config_health

logger = logging.getLogger(__name__)

PASS_INTERVAL_MINUTES = 15


class ConfigHealthCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.notify.start()

    def cog_unload(self):
        self.notify.cancel()

    @tasks.loop(minutes=PASS_INTERVAL_MINUTES)
    async def notify(self):
        try:
            posted = await config_health.run_notifier_pass(self.bot)
        except Exception as e:  # noqa: BLE001 - one bad guild must not kill the loop
            logger.warning("[CONFIG-HEALTH] notifier pass failed: %s", e)
            return
        if posted:
            logger.info("[CONFIG-HEALTH] posted config notices to %s guild(s)", posted)
        config.stamp_loop_heartbeat("config_health")

    @notify.before_loop
    async def _before(self):
        await self.bot.wait_until_ready()


async def setup(bot):
    await bot.add_cog(ConfigHealthCog(bot))
