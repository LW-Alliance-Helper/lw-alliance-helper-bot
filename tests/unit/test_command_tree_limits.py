"""Every slash command the bot registers fits Discord's length limits (#677).

Discord rejects a whole sync when one command in it breaks a limit, not just
that command. 1.9.2 shipped `/admin backfill_removed_guilds` with a
106-character description, and every start since failed the `/admin` guild
sync, so none of the owner tools could be updated and #649's backfill could
not be run. Nothing caught it before production because nothing checked.

This walks every command the bot would sync -- bot.py's global commands, the
guild-scoped `/admin` group, and every cog bot.py loads (read from bot.py
itself, so a new cog is covered the day it is added) -- and checks names,
descriptions and choice labels against the limits Discord enforces.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from pathlib import Path
from unittest.mock import patch

import discord
from discord import app_commands
from discord.ext import commands, tasks

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

# https://discord.com/developers/docs/interactions/application-commands
NAME_MAX = 32
DESCRIPTION_MAX = 100
CHOICE_NAME_MAX = 100


def _cog_extensions() -> list[str]:
    """Every extension bot.py loads, the flag-gated Map Manager cog included."""
    source = (ROOT / "bot.py").read_text(encoding="utf-8")
    return sorted(set(re.findall(r'load_extension\(\s*"([a-z_]+)"\s*\)', source)))


async def _all_top_level_commands() -> list:
    import bot as bot_module
    import bot_admin

    found = list(bot_module.bot.tree.get_commands())
    # Global when BOT_ADMIN_GUILD_IDS is unset, guild-scoped when it is set:
    # take the group itself so both cases are covered.
    found.append(bot_admin.admin_group)

    # Call each extension's own setup() against a throwaway bot instead of
    # `load_extension`, which would re-execute the module and swap it in
    # sys.modules under every other test's patches. The cogs start their
    # loops in __init__; nothing here should run them.
    scratch = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    with patch.object(tasks.Loop, "start", lambda self, *a, **k: None):
        for name in _cog_extensions():
            await importlib.import_module(name).setup(scratch)
    found.extend(scratch.tree.get_commands())

    unique = {}
    for cmd in found:
        unique[id(cmd)] = cmd
    return list(unique.values())


def _problems(cmd, path: str = "") -> list[str]:
    here = f"{path} {cmd.name}".strip()
    out = []
    if not 1 <= len(cmd.name) <= NAME_MAX:
        out.append(f"/{here}: name is {len(cmd.name)} characters (1-{NAME_MAX})")
    if isinstance(cmd, app_commands.ContextMenu):
        return out
    desc = cmd.description or ""
    if not 1 <= len(desc) <= DESCRIPTION_MAX:
        out.append(f"/{here}: description is {len(desc)} characters (1-{DESCRIPTION_MAX})")
    if isinstance(cmd, app_commands.Group):
        for sub in cmd.commands:
            out.extend(_problems(sub, here))
        return out
    for param in cmd.parameters:
        where = f"/{here} option {param.display_name!r}"
        if not 1 <= len(param.display_name) <= NAME_MAX:
            out.append(f"{where}: name is {len(param.display_name)} characters (1-{NAME_MAX})")
        pdesc = param.description or ""
        if not 1 <= len(pdesc) <= DESCRIPTION_MAX:
            out.append(f"{where}: description is {len(pdesc)} characters (1-{DESCRIPTION_MAX})")
        for choice in param.choices:
            if not 1 <= len(choice.name) <= CHOICE_NAME_MAX:
                out.append(
                    f"{where} choice {choice.name!r}: "
                    f"{len(choice.name)} characters (1-{CHOICE_NAME_MAX})"
                )
    return out


async def test_walk_reaches_the_known_commands():
    """Guard against the walk silently covering nothing: a cog, bot.py's own
    commands and the /admin subcommands all have to be in it."""
    names = {c.name for c in await _all_top_level_commands()}
    assert {"admin", "desertstorm", "canyonstorm", "setup", "growth"} <= names


async def test_every_command_fits_discords_limits():
    problems = []
    for cmd in await _all_top_level_commands():
        problems.extend(_problems(cmd))
    assert not problems, "Discord would reject the sync:\n" + "\n".join(problems)
