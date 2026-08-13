"""`/champion_duel` — the admin surface over the Champion Duel edit history.

Deliberately in Discord rather than the web app. The predictor itself is a web
page, but "who changed what, and put it back" is an owner-only surface used
occasionally, and it is cheaper and safer here: the bot already has an admin
idiom, ephemeral replies, and an audience that is already in Discord.

The command name is `champion_duel`, never `duel`. Champion Duel, Warzone Duel
and Alliance VS Duel are three different events, and `/vs` already owns the
third — a bare `/duel` would be ambiguous the day the second one ships.

Access is `CHAMPION_DUEL_ADMIN_IDS` (comma-separated Discord user ids), the
same comma-separated-env-var idiom as `BOT_ADMIN_GUILD_IDS`. Unset means
nobody, so a misconfigured deploy closes the surface rather than opening it.

Browsing a long history in Discord is genuinely worse than a spreadsheet, so
`/champion_duel export` is a first-class command rather than an afterthought:
a date range in, a CSV attachment out.
"""

from __future__ import annotations

import asyncio
import csv
import io
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import champion_duel_db as db
from api.champion_duel_auth import admin_ids

# Discord's message limit is 2000; embeds are 4096 per description. Keep the
# browse list well inside both, since the export exists for volume.
BROWSE_MAX = 20


def _is_admin(user_id: int) -> bool:
    return str(user_id) in admin_ids()


def _parse_day(value: str, *, end_of_day: bool) -> str | None:
    """Accept YYYY-MM-DD and widen it to cover the whole day.

    Timestamps are stored as ISO-8601 UTC text and compared lexicographically,
    so an inclusive end needs the day's last instant rather than midnight —
    otherwise `export 2026-08-12 2026-08-12` silently returns nothing, which
    reads as "no edits that day" instead of "you asked for a zero-width range".
    """
    try:
        day = datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None
    if end_of_day:
        day = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day.isoformat()


def _describe(edit: dict) -> str:
    """One edit as a line. Shows the actor as a mention so an unfamiliar
    snowflake resolves to a person without a second lookup, and the server
    alongside the name because two servers can field the same name."""
    who = f"<@{edit['actor_discord_id']}>"
    when = (edit.get("created_at") or "")[:16].replace("T", " ")
    what = edit.get("field") or edit.get("target")
    slot = f" slot {edit['slot']}" if edit.get("slot") else ""
    old, new = edit.get("old_value"), edit.get("new_value")
    change = f"{old or '—'} → {new or '—'}"
    tail = f"  ↩ revert of #{edit['revert_of']}" if edit.get("revert_of") else ""
    name = edit.get("display_name") or "(unknown)"
    server = f" (#{edit['server']})" if edit.get("server") else ""
    return f"`#{edit['id']}` **{name}**{server}{slot} {what}: {change} · {who} · {when}{tail}"


class ChampionDuelAdmin(commands.Cog):
    """Owner-facing tools over the Champion Duel scouting data."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    group = app_commands.Group(
        name="champion_duel",
        description="Champion Duel scouting data admin",
    )

    async def _guard(self, interaction: discord.Interaction) -> bool:
        """Refuse non-admins, and say so ephemerally.

        Not silent: an admin whose id was never added to the env var would
        otherwise see the command do nothing and assume it was broken.
        """
        if _is_admin(interaction.user.id):
            return True
        await interaction.response.send_message(
            "That's limited to Champion Duel admins. If that should be you, "
            "your Discord ID needs adding to `CHAMPION_DUEL_ADMIN_IDS`.",
            ephemeral=True,
        )
        return False

    @group.command(name="edits", description="Browse recent Champion Duel data edits")
    @app_commands.describe(
        player="Only edits to this player",
        actor="Only edits by this person",
        limit=f"How many to show (max {BROWSE_MAX})",
    )
    async def edits(
        self,
        interaction: discord.Interaction,
        player: str | None = None,
        actor: discord.User | None = None,
        limit: int = 10,
    ):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        result = await asyncio.to_thread(
            db.list_edits,
            player=player,
            actor=str(actor.id) if actor else None,
            limit=max(1, min(limit, BROWSE_MAX)),
        )
        rows = result["edits"]
        if not rows:
            await interaction.followup.send("No edits match that.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Champion Duel — recent edits",
            description="\n".join(_describe(e) for e in rows),
            colour=discord.Colour.blurple(),
        )
        shown = len(rows)
        embed.set_footer(
            text=(
                f"Showing {shown} of {result['total']}. "
                f"Use /champion_duel export for a spreadsheet, "
                f"or /champion_duel revert to put one back."
            )
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @group.command(name="revert", description="Put a Champion Duel edit back to its prior value")
    @app_commands.describe(
        edit_id="The #id from /champion_duel edits",
        force="Revert even if the value changed again since",
    )
    async def revert(self, interaction: discord.Interaction, edit_id: int, force: bool = False):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        actor = {
            "discord_user_id": str(interaction.user.id),
            "discord_name": interaction.user.display_name,
            "guild_id": str(interaction.guild_id) if interaction.guild_id else None,
        }
        try:
            result = await asyncio.to_thread(db.revert_edit, edit_id, actor=actor, force=force)
        except db.RevertConflict as exc:
            # Refusing is the point: two scouts entering sightings for one
            # player is normal, and the later entry is usually the better
            # information. Show what's there now and let the admin decide.
            await interaction.followup.send(
                f"⚠️ Edit `#{edit_id}` wasn't reverted — that value has changed since.\n"
                f"It's now **{exc.current}**, but the edit expected **{exc.expected}**.\n"
                f"Someone may have corrected it more recently. Re-run with `force: True` "
                f"to overwrite it anyway.",
                ephemeral=True,
            )
            return
        except LookupError:
            await interaction.followup.send(f"No edit `#{edit_id}`.", ephemeral=True)
            return
        except ValueError as exc:
            await interaction.followup.send(f"Can't revert that: {exc}", ephemeral=True)
            return

        await interaction.followup.send(
            f"✅ Reverted `#{edit_id}` — restored to **{result['restored_to'] or '—'}**.\n"
            f"Logged as edit `#{result['edit_id']}`; nothing was deleted.",
            ephemeral=True,
        )

    @group.command(name="export", description="Export Champion Duel edits between two dates as CSV")
    @app_commands.describe(start="Start date, YYYY-MM-DD", end="End date, YYYY-MM-DD (inclusive)")
    async def export(self, interaction: discord.Interaction, start: str, end: str):
        if not await self._guard(interaction):
            return
        await interaction.response.defer(ephemeral=True)

        start_iso = _parse_day(start, end_of_day=False)
        end_iso = _parse_day(end, end_of_day=True)
        if not start_iso or not end_iso:
            await interaction.followup.send(
                "Dates need to be `YYYY-MM-DD` — for example `2026-08-12`.", ephemeral=True
            )
            return
        if start_iso > end_iso:
            await interaction.followup.send("The start date is after the end date.", ephemeral=True)
            return

        rows = await asyncio.to_thread(db.export_edits, start_iso, end_iso)
        if not rows:
            await interaction.followup.send(f"No edits between {start} and {end}.", ephemeral=True)
            return

        columns = [
            "id",
            "created_at",
            "target",
            "registrant_id",
            "display_name",
            # Server and group ride along so a spreadsheet can tell two players
            # with the same name on different servers apart -- the whole reason
            # identity is (name, server) rather than the name alone.
            "server",
            "grp",
            "slot",
            "field",
            "old_value",
            "new_value",
            "actor_discord_id",
            "actor_name",
            "actor_guild_id",
            "revert_of",
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)

        # utf-8-sig so Excel opens non-ASCII player names correctly instead of
        # rendering mojibake -- these names routinely carry non-Latin scripts.
        data = io.BytesIO(buf.getvalue().encode("utf-8-sig"))
        filename = f"champion_duel_edits_{start}_to_{end}.csv"
        await interaction.followup.send(
            f"{len(rows)} edit(s) between {start} and {end}.",
            file=discord.File(data, filename=filename),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(ChampionDuelAdmin(bot))
