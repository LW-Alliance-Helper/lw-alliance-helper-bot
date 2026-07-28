"""
bot_state.py — Single source of truth for cross-thread bot state.

The Railway Procfile runs ``python bot.py``, which loads ``bot.py``
into ``sys.modules`` as ``__main__``. Other modules that later do
``import bot`` get a *separate* copy of ``bot.py`` re-loaded as the
``bot`` module. Module-level globals set on the running ``__main__``
copy (the captured event loop, the bot instance once we attach it,
etc.) are NOT visible to code that imports ``bot``, because they
live in two different module objects.

This module dodges the trap by only ever being imported — never run
as a script. Both ``bot.py`` and downstream modules (``growth.py``
and anything else that needs to schedule onto the running event
loop from a background thread) import ``bot_state`` and end up
sharing the same module instance, with the same state visible to
everyone.

Set the values from ``bot.on_ready`` (after the bot has connected
and the event loop is running). Read them from background-thread
callers via ``getattr(bot_state, "event_loop", None)`` to stay
defensive against pre-ready callers.
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    import asyncio
    from collections.abc import Callable, Coroutine
    import discord
    from discord.ext import commands
    from zoneinfo import ZoneInfo


# Captured by `bot.on_ready` on first fire. Stays None until the bot
# is connected. Background callers should treat None as "not ready
# yet, skip and try next tick".
event_loop: "Optional[asyncio.AbstractEventLoop]" = None


# The running `commands.Bot` instance. Set in `bot.py` immediately
# after the bot is constructed so it's available even before
# `on_ready` fires (useful for callers that just need the bot
# reference without scheduling work onto its loop). Read this rather
# than `from bot import bot` to avoid the __main__-vs-`bot`
# double-load described above.
bot: "Optional[commands.Bot]" = None

# bot.py's timezone constant + support-join-watch helper (#372). Any
# module split out of bot.py that needs these -- bot_admin.py does --
# must read them from here, not `from bot import ET, _try_assign_verified`:
# that import crashed Railway (which runs `python bot.py` as __main__)
# with `ImportError: cannot import name '_ADMIN_GUILD_IDS' from
# 'bot_admin'`, because bot.py's own bottom-of-file `from bot_admin
# import _ADMIN_GUILD_IDS` triggered bot_admin's `from bot import ...`,
# which -- since `sys.modules` has no entry for "bot" when it's running
# as __main__ -- re-executed bot.py a second time under the name "bot",
# which hit its own `from bot_admin import _ADMIN_GUILD_IDS` line while
# the first bot_admin import was still mid-execution (paused before
# `_ADMIN_GUILD_IDS` gets defined), and failed. Set in `bot.py` right
# after each is defined.
ET: "Optional[ZoneInfo]" = None
try_assign_verified: "Optional[Callable[..., Coroutine]]" = None
