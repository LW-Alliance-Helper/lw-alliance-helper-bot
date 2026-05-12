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
    import discord
    from discord.ext import commands


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
