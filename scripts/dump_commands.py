"""
dump_commands.py — Print the bot's registered global slash commands as JSON.

The output is the exact payload top.gg's "Import from Discord" modal accepts:
the same shape Discord uses for command registration. Pipe it to a file and
paste the file contents into top.gg's import dialog when commands change.

USAGE
-----

From the bot repo root with `DISCORD_TOKEN` in your local `.env` (or shell):

    python scripts/dump_commands.py > commands.json

Then paste the contents of `commands.json` into top.gg's modal and hit Import.
`commands.json` is gitignored / local-only — don't commit it (Discord is fine
with the payload being public, but no reason to clutter the repo).

This uses discord.py's HTTPClient directly so it does NOT connect to the
gateway. Safe to run while the production bot is live; no duplicate session.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

from discord.http import HTTPClient
from dotenv import load_dotenv


APP_ID = 1488378654709780510


async def main() -> int:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        print("DISCORD_TOKEN not set in env (.env or shell).", file=sys.stderr)
        return 1

    http = HTTPClient()
    try:
        await http.static_login(token)
        commands = await http.get_global_commands(APP_ID)
    finally:
        await http.close()

    json.dump(commands, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
