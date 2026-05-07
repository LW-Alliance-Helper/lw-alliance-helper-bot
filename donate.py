"""
donate.py — /donate command for showing donation links.

Donation URLs are read from environment variables so they can be updated
without code changes. Any platform whose env var is unset is omitted from
the embed.
"""

import os

import discord
from discord import app_commands
from discord.ext import commands

import premium


# Default Ko-fi link is set so the command works out-of-the-box for the
# current bot owner. Other platforms default to empty (omitted from embed).
DONATION_PLATFORMS = [
    {
        "env":   "KOFI_URL",
        "name":  "Ko-fi",
        "emoji": "☕",
        "default": "https://ko-fi.com/pinkcatboi",
    },
    {"env": "BUYMEACOFFEE_URL",   "name": "Buy Me a Coffee", "emoji": "🥤", "default": ""},
    {"env": "GITHUB_SPONSORS_URL","name": "GitHub Sponsors", "emoji": "💖", "default": ""},
    {"env": "PATREON_URL",        "name": "Patreon",         "emoji": "🎁", "default": ""},
    {"env": "PAYPAL_URL",         "name": "PayPal",          "emoji": "💵", "default": ""},
]


def _active_platforms() -> list[tuple[str, str, str]]:
    """Return [(name, emoji, url), ...] for platforms with a non-empty URL."""
    out = []
    for p in DONATION_PLATFORMS:
        url = os.getenv(p["env"], p["default"]).strip()
        if url:
            out.append((p["name"], p["emoji"], url))
    return out


class DonateCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="donate",
        description="Support the bot's hosting costs and development",
    )
    async def donate(self, interaction: discord.Interaction):
        platforms = _active_platforms()

        embed = discord.Embed(
            title="💖 Support Alliance Helper",
            description=(
                "If this bot has been useful to your alliance and you'd like to "
                "help cover hosting costs or just show appreciation, any support "
                "is hugely appreciated. Thank you!"
            ),
            color=discord.Color.magenta(),
        )

        if platforms:
            lines = [f"{emoji} **[{name}]({url})**" for name, emoji, url in platforms]
            embed.add_field(name="Ways to Donate", value="\n".join(lines), inline=False)
        else:
            embed.add_field(
                name="Ways to Donate",
                value="*(No donation links configured yet.)*",
                inline=False,
            )

        embed.set_footer(text="100% optional — the bot is and will remain free to use at the base level.")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="upgrade",
        description="Unlock LW Alliance Helper Premium for this server",
    )
    async def upgrade(self, interaction: discord.Interaction):
        is_premium = await premium.is_premium(
            interaction.guild_id, interaction=interaction, bot=self.bot,
        )

        if is_premium:
            embed = discord.Embed(
                title="💎 Premium is active",
                description=(
                    "This server already has LW Alliance Helper Premium — you're set! "
                    "All premium features are unlocked. Thanks for supporting the bot."
                ),
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        embed = discord.Embed(
            title="💎 LW Alliance Helper Premium",
            description=(
                "Unlock the full power of Alliance Helper for your alliance.\n\n"
                "**What you get:**\n"
                "• 📣 Unlimited events (vs 5 free)\n"
                "• 🚂 Up to 10 saved train prompt templates (vs 1 free)\n"
                "• ⚔️ Up to 10 saved storm mail templates per team (vs 1 free)\n"
                "• 📋 Multiple surveys + extra question types (numeric, multi-select, date)\n"
                "• 📊 Custom snapshot intervals + unlimited tracked metrics\n"
                "• 🧵 Use threads as destinations for any channel-pickable feature\n"
                "• 👥 Member roster sync, birthday DMs, train DMs, survey reminders\n"
                "• 📅 30-day history windows on `/events_log` and `/train_log`\n"
                "• 📜 Unlimited storm-log lookback\n\n"
                "**$4.99/month**, billed by Discord. Cancel anytime.\n\n"
                "🗂️ Premium adds features, not lock-in — your alliance's data continues "
                "to live in your own Google Sheet."
            ),
            color=discord.Color.purple(),
        )

        view = premium.upgrade_view()
        if view is not None:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        else:
            embed.add_field(
                name="⚠️ Subscriptions not yet available",
                value=(
                    "Premium subscriptions aren't live yet. "
                    "Check back soon, or use `/donate` to support the bot in the meantime."
                ),
                inline=False,
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(DonateCog(bot))
