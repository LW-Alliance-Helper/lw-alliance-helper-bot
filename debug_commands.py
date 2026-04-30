"""
debug_commands.py — TEMPORARY diagnostic commands.

Loaded as a cog only when DEBUG_COMMANDS=1 is set in the environment,
so production deploys don't accidentally expose these to users. Each
command is admin-only.

These exist to investigate specific production issues. Remove the file
and the load entry in bot.py once the issue is diagnosed.

Currently investigating: threads not appearing in ChannelSelect dropdowns
even when `include_threads=True` is passed (premium guilds should see
threads as picker options but the dropdown shows only text channels).
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class DebugCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="admin_debug_channels",
        description="(admin only) Dump channel + thread visibility info for this guild",
    )
    async def admin_debug_channels(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message(
                "⛔ Admin only.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("⚠️ No guild context.", ephemeral=True)
            return

        # Build the diagnostic output. Aim to stay under Discord's 2000-char
        # message limit; truncate if needed.
        sections: list[str] = []

        # ── Section 1: environment ────────────────────────────────────
        sections.append("=== ENV ===")
        sections.append(f"discord.py: {discord.__version__}")
        sections.append(f"guild: {guild.name} (id={guild.id})")
        sections.append(f"bot member: {guild.me is not None}")
        if guild.me:
            sections.append(f"bot top role: {guild.me.top_role.name}")

        # ── Section 2: ChannelSelect channel_types we're sending ──────
        sections.append("")
        sections.append("=== ChannelSelectStep(include_threads=True) channel_types ===")
        try:
            from setup_cog import ChannelSelectStep
            view = ChannelSelectStep("test", include_threads=True)
            select = view.children[0]
            for t in select.channel_types:
                sections.append(f"  - {t.name} (api value={t.value})")
        except Exception as e:
            sections.append(f"  ERROR building ChannelSelectStep: {e!r}")

        # ── Section 3: cached guild.threads ───────────────────────────
        sections.append("")
        threads = guild.threads
        sections.append(f"=== guild.threads (cached, n={len(threads)}) ===")
        for t in threads[:30]:
            parent = t.parent.name if t.parent else "?"
            sections.append(
                f"  - {t.name} | type={t.type.name} | "
                f"parent=#{parent} | archived={t.archived} | "
                f"locked={t.locked}"
            )
        if len(threads) > 30:
            sections.append(f"  ... ({len(threads) - 30} more)")
        if not threads:
            sections.append("  (none)")

        # ── Section 4: active threads via API call ────────────────────
        sections.append("")
        sections.append("=== guild.active_threads() (API) ===")
        try:
            active = await guild.active_threads()
            sections.append(f"count={len(active)}")
            for t in active[:30]:
                parent = t.parent.name if t.parent else "?"
                bot_in_thread = False
                try:
                    # is_member can hit the API per thread; cap how many we check.
                    bot_in_thread = guild.me in t.members if t.members else False
                except Exception:
                    bot_in_thread = "?"
                sections.append(
                    f"  - {t.name} | type={t.type.name} | "
                    f"parent=#{parent} | archived={t.archived} | "
                    f"bot_in_thread={bot_in_thread}"
                )
            if len(active) > 30:
                sections.append(f"  ... ({len(active) - 30} more)")
        except Exception as e:
            sections.append(f"FAILED: {e!r}")

        # ── Section 5: bot's permissions on the calling channel ──────
        sections.append("")
        sections.append("=== bot perms in this channel ===")
        if interaction.channel and guild.me:
            try:
                perms = interaction.channel.permissions_for(guild.me)
                relevant = (
                    "view_channel", "send_messages", "embed_links",
                    "read_message_history", "manage_threads",
                    "send_messages_in_threads", "create_public_threads",
                    "create_private_threads",
                )
                for name in relevant:
                    sections.append(f"  {name}: {getattr(perms, name, '?')}")
            except Exception as e:
                sections.append(f"  ERROR: {e!r}")

        output = "\n".join(sections)
        # Discord message limit is 2000 chars; leave headroom for the
        # ```...``` fence.
        if len(output) > 1900:
            output = output[:1900] + "\n... (truncated)"

        await interaction.followup.send(
            f"```\n{output}\n```", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(DebugCog(bot))
