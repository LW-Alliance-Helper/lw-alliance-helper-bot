"""
donate.py — /donate, /upgrade, and the /premium command group.

Donation URLs are read from environment variables so they can be updated
without code changes. Any platform whose env var is unset is omitted from
the embed.

Premium licensing model (see `premium.py` and issue #41):
  Discord sells a User Subscription which is valid in every guild the
  subscriber shares with the bot. The bot enforces "one license = one
  guild at a time" via an assignment layer; the /premium group is the
  subscriber-facing surface for that layer.

Commands defined here:
  - /donate            — non-premium support links (Ko-fi, etc.)
  - /upgrade           — pitch + Discord premium-button + auto-assign
  - /premium overview  — subscription state; doubles as the upgrade
                         pitch for free-tier callers with no active sub
  - /premium assign    — pin the caller's license to the current guild
  - /premium unassign  — release the current assignment without canceling
"""

import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import premium


# Default Ko-fi link is set so the command works out-of-the-box for the
# current bot owner. Other platforms default to empty (omitted from embed).
DONATION_PLATFORMS = [
    {
        "env": "KOFI_URL",
        "name": "Ko-fi",
        "emoji": "☕",
        "default": "https://ko-fi.com/pinkcatboi",
    },
    {"env": "BUYMEACOFFEE_URL", "name": "Buy Me a Coffee", "emoji": "🥤", "default": ""},
    {"env": "GITHUB_SPONSORS_URL", "name": "GitHub Sponsors", "emoji": "💖", "default": ""},
    {"env": "PATREON_URL", "name": "Patreon", "emoji": "🎁", "default": ""},
    {"env": "PAYPAL_URL", "name": "PayPal", "emoji": "💵", "default": ""},
]


def _active_platforms() -> list[tuple[str, str, str]]:
    """Return [(name, emoji, url), ...] for platforms with a non-empty URL."""
    out = []
    for p in DONATION_PLATFORMS:
        url = os.getenv(p["env"], p["default"]).strip()
        if url:
            out.append((p["name"], p["emoji"], url))
    return out


# ── Helpers shared between commands ───────────────────────────────────────────


async def _resolve_guild_name(bot: commands.Bot, guild_id: int) -> str:
    """Best-effort human-readable name for a guild_id. Falls back to the
    bare id if the bot can't see the guild (subscriber may have left it,
    or the guild may have been removed)."""
    guild = bot.get_guild(guild_id)
    if guild is not None:
        return guild.name
    try:
        guild = await bot.fetch_guild(guild_id)
        return guild.name
    except Exception:
        return f"server #{guild_id}"


async def _resolve_user_label(bot: commands.Bot, user_id: int) -> str:
    """Best-effort `@username` for a user_id. Falls back to the bare id."""
    user = bot.get_user(user_id)
    if user is not None:
        return f"@{user.name}"
    try:
        user = await bot.fetch_user(user_id)
        return f"@{user.name}"
    except Exception:
        return f"user #{user_id}"


# ── Confirmation views ────────────────────────────────────────────────────────


class _ConfirmActionView(discord.ui.View):
    """Generic two-button confirm/cancel used by /premium assign (both fresh
    and switch flows) and /premium unassign. The confirm button label is
    configurable so each call site reads naturally."""

    def __init__(
        self,
        *,
        owner_id: int,
        confirm_label: str,
        confirm_style: discord.ButtonStyle = discord.ButtonStyle.primary,
    ):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.confirmed: Optional[bool] = None
        self.message: Optional[discord.Message] = None
        # Override the labels of the decorator-bound buttons so each call
        # site can phrase the action explicitly ("Pin to this server",
        # "Switch to this server", "Release pin").
        self.confirm.label = confirm_label
        self.confirm.style = confirm_style

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the user who ran the command can use these buttons.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        self.confirmed = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _btn: discord.ui.Button):
        self.confirmed = False
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()


# ── Shared pitch embed ────────────────────────────────────────────────────────


def _build_upgrade_pitch_embed() -> discord.Embed:
    """The Premium feature pitch + price + assignment explanation. Shared
    between /upgrade (when caller has no active sub) and /premium overview
    (when caller has no sub and is in a free-tier guild) so the copy stays
    in one place."""
    return discord.Embed(
        title="💎 LW Alliance Helper Premium",
        description=(
            "Unlock the full power of Alliance Helper for your alliance.\n\n"
            "**What you get:**\n"
            "• 📣 Unlimited events (vs 5 free)\n"
            "• 🚂 Up to 10 saved train prompt templates (vs 1 free)\n"
            "• 🚂 Role-scoped Conductor Rotation days (Leadership, VS, Contest, Event)\n"
            "• ⚔️ Up to 10 saved storm mail templates per team (vs 1 free)\n"
            "• 📋 Multiple surveys + extra question types (multi-select, date) plus min/max bounds on numeric\n"
            "• 📊 Custom snapshot intervals + unlimited tracked metrics\n"
            "• 🧵 Use threads as destinations for any channel-pickable feature\n"
            "• 👥 Member roster sync, birthday DMs, train DMs, survey reminders\n"
            "• 📅 30-day history windows on the `/events` event log and the `/train` prompt log\n"
            "• 📜 Unlimited storm-log lookback\n\n"
            "**$4.99/month**, billed by Discord. Cancel anytime.\n\n"
            "🪪 Your subscription unlocks Premium in **one server at a time**. "
            "After checkout the bot pins it to this server automatically; "
            "use `/premium assign` to move it later.\n\n"
            "🗂️ Premium adds features, your alliance data lives in your own Google Sheet. "
            "If you ever cancel, the Sheet you've been using is still yours, with all your data intact."
        ),
        color=discord.Color.purple(),
    )


# ── Cog ───────────────────────────────────────────────────────────────────────


class DonateCog(commands.Cog):
    # /premium is a top-level slash-command group containing overview /
    # assign / unassign. Defined as a class attribute so discord.py picks
    # it up when the cog is added and registers it on the bot tree.
    premium_group = app_commands.Group(
        name="premium",
        description="Manage your LW Alliance Helper Premium subscription",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # In-memory map user_id → guild_id for auto-assign on first
        # checkout. Set by /upgrade or /premium overview when the Discord
        # premium button is offered, consumed by on_entitlement_create.
        # In-memory is fine — a Railway restart that straddles a checkout
        # just means the user has to run /premium assign manually after
        # subscribing, which the post-subscribe DM tells them to do anyway.
        self._pending_upgrade_guilds: dict[int, int] = {}

    # ── /donate ───────────────────────────────────────────────────────────────

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

        embed.set_footer(
            text="100% optional — the bot is and will remain free to use at the base level."
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /upgrade ──────────────────────────────────────────────────────────────

    @app_commands.command(
        name="upgrade",
        description="Unlock LW Alliance Helper Premium for this server",
    )
    async def upgrade(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        # If this guild is already premium via FORCE_PREMIUM/bypass or an
        # already-assigned subscription, short-circuit with the existing
        # "Premium is active" copy.
        is_premium_here = await premium.is_premium(guild_id, interaction=interaction, bot=self.bot)
        if is_premium_here:
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

        # Free guild. Two cases for the caller:
        #   A. They already have an active subscription (assigned elsewhere or
        #      not assigned at all) — handle assignment directly.
        #   B. They don't — show the upgrade pitch + Discord's premium button.
        caller_has_sub = await premium.user_has_active_subscription(user_id, bot=self.bot)
        if caller_has_sub:
            assigned_guild = premium.get_assigned_guild(user_id)
            if assigned_guild is None:
                # First-checkout auto-assign. Reject if another subscriber
                # already holds this guild.
                holder = premium.get_assigned_user(guild_id)
                if holder is not None and holder != user_id:
                    holder_label = await _resolve_user_label(self.bot, holder)
                    embed = discord.Embed(
                        title="⚠️ This server already has Premium from another subscriber",
                        description=(
                            f"{holder_label}'s subscription is currently assigned here, "
                            "so two subscriptions can't both apply. Options:\n"
                            "• Apply your subscription to a different server "
                            "(run `/premium assign` there)\n"
                            "• Run `/premium overview` to see your options\n"
                        ),
                        color=discord.Color.orange(),
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return

                if not premium.assign(user_id, guild_id):
                    # Race: another subscriber claimed this guild between
                    # the holder pre-check above and the assign call.
                    embed = discord.Embed(
                        title="⚠️ Couldn't claim this server",
                        description=(
                            "Another subscriber's claim on this server "
                            "landed first. Run `/premium assign` from a "
                            "different server, or `/premium overview` to "
                            "see your options."
                        ),
                        color=discord.Color.orange(),
                    )
                    await interaction.response.send_message(embed=embed, ephemeral=True)
                    return
                embed = discord.Embed(
                    title="💎 Premium is active in this server!",
                    description=(
                        f"Your subscription has been automatically assigned to "
                        f"**{interaction.guild.name if interaction.guild else 'this server'}**. "
                        "A subscription can only be active in one server at a time — "
                        "if you want to apply it to a different alliance later, run "
                        "`/premium assign` from that server, or `/premium overview` "
                        "to manage it."
                    ),
                    color=discord.Color.gold(),
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            if assigned_guild == guild_id:
                # Edge case: assigned here, but is_premium returned False
                # (e.g. cache hadn't picked up a recent re-subscribe). Surface
                # that the assignment is intact rather than push them through
                # the upgrade flow again.
                embed = discord.Embed(
                    title="💎 Premium is assigned to this server",
                    description=(
                        "Your subscription is already pinned to this server. "
                        "If features still appear locked, give it a minute for "
                        "the cache to refresh, or run `/premium overview` to see "
                        "the current state."
                    ),
                    color=discord.Color.gold(),
                )
                await interaction.response.send_message(embed=embed, ephemeral=True)
                return

            # Subscription assigned to a different guild — prompt to switch.
            other_name = await _resolve_guild_name(self.bot, assigned_guild)
            embed = discord.Embed(
                title="💎 Switch your Premium to this server?",
                description=(
                    f"Your subscription is currently active in **{other_name}**. "
                    "A subscription can only be active in one server at a time. "
                    "Run `/premium assign` here to switch, or `/premium overview` "
                    "to manage it."
                ),
                color=discord.Color.purple(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # No active subscription for this user — pitch + Discord premium button.
        await self._send_pitch_with_subscribe_button(interaction)

    async def _send_pitch_with_subscribe_button(
        self,
        interaction: discord.Interaction,
    ) -> None:
        """Render the upgrade pitch with Discord's subscribe button.

        Shared between /upgrade (no-sub branch) and /premium overview
        (free-tier caller, no sub) so the copy + button + pending-guild
        bookkeeping stay in one place.
        """
        embed = _build_upgrade_pitch_embed()
        view = premium.upgrade_view()
        if view is not None:
            # Remember which guild the user was in when they were offered
            # the premium button, so on_entitlement_create can auto-assign
            # to it after checkout completes.
            if interaction.guild_id is not None:
                self._pending_upgrade_guilds[interaction.user.id] = interaction.guild_id
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

    # ── /premium overview ─────────────────────────────────────────────────────

    @premium_group.command(
        name="overview",
        description="Your subscription state and where it's assigned",
    )
    async def premium_overview(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        has_sub = await premium.user_has_active_subscription(user_id, bot=self.bot)
        assigned_guild = premium.get_assigned_guild(user_id)

        # No subscription, no preserved assignment → this leaf doubles as
        # the upgrade pitch so /premium acts as a free-tier upsell surface.
        if not has_sub and assigned_guild is None:
            await self._send_pitch_with_subscribe_button(interaction)
            return

        if not has_sub and assigned_guild is not None:
            # Subscription lapsed but the assignment row was kept so a
            # resubscribe will auto-resume in the same guild.
            assigned_name = await _resolve_guild_name(self.bot, assigned_guild)
            embed = discord.Embed(
                title="💎 Subscription not active",
                description=(
                    f"Your subscription is currently inactive, but your previous "
                    f"assignment to **{assigned_name}** is preserved. If you "
                    "resubscribe via `/upgrade`, Premium will resume there "
                    "automatically. To clear the assignment, run `/premium unassign`."
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # has_sub == True
        if assigned_guild is None:
            embed = discord.Embed(
                title="💎 Subscription active — not assigned to any server",
                description=(
                    "Your subscription is active but not pinned to any server yet. "
                    "Run `/premium assign` from the server you want Premium in."
                ),
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        assigned_name = await _resolve_guild_name(self.bot, assigned_guild)
        embed = discord.Embed(
            title="💎 Premium is active",
            description=(
                f"Your subscription is currently pinned to **{assigned_name}**. "
                "To move it, run `/premium assign` from a different server. "
                "To release the pin without canceling, run `/premium unassign`."
            ),
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /premium assign ───────────────────────────────────────────────────────

    @premium_group.command(
        name="assign",
        description="Pin your Premium subscription to this server",
    )
    async def premium_assign(self, interaction: discord.Interaction):
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        if guild_id is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.",
                ephemeral=True,
            )
            return

        has_sub = await premium.user_has_active_subscription(user_id, bot=self.bot)
        if not has_sub:
            embed = discord.Embed(
                title="💎 You don't have an active Premium subscription",
                description=(
                    "Run `/upgrade` to unlock it, then `/premium assign` to pin "
                    "your subscription to a specific server."
                ),
                color=discord.Color.purple(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        current = premium.get_assigned_guild(user_id)
        if current == guild_id:
            embed = discord.Embed(
                title="💎 Already assigned here",
                description=(
                    "Your subscription is already pinned to this server. "
                    "Run `/premium overview` to see details or `/premium unassign` "
                    "to release it."
                ),
                color=discord.Color.gold(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        # Reject if another subscriber already holds this guild.
        holder = premium.get_assigned_user(guild_id)
        if holder is not None and holder != user_id:
            holder_label = await _resolve_user_label(self.bot, holder)
            embed = discord.Embed(
                title="⚠️ This server already has Premium",
                description=(
                    f"{holder_label}'s subscription is currently assigned here. "
                    "Two subscriptions can't both apply to the same server. Options:\n"
                    "• Apply your subscription to a different server "
                    "(run `/premium assign` there)\n"
                    f"• Coordinate with {holder_label} if one of you should unsubscribe\n"
                    "• Run `/premium overview` to see where your subscription can apply"
                ),
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        this_name = interaction.guild.name if interaction.guild else "this server"

        if current is None:
            # Fresh assignment — confirm to make the destination guild
            # explicit (the user shouldn't have to guess what server
            # /premium assign just bound to).
            embed = discord.Embed(
                title="💎 Pin Premium to this server?",
                description=(
                    f"Your subscription will be pinned to **{this_name}**. "
                    "Premium activates here immediately, and you can move it "
                    "later with `/premium assign` from a different server."
                ),
                color=discord.Color.purple(),
            )

            view = _ConfirmActionView(
                owner_id=user_id,
                confirm_label=f"Yes, pin to {this_name}"[:80],
            )
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
            view.message = await interaction.original_response()
            await view.wait()

            if not view.confirmed:
                await interaction.followup.send(
                    "Cancelled — your subscription is unchanged.",
                    ephemeral=True,
                )
                return

            if not premium.assign(user_id, guild_id):
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="⚠️ Couldn't claim this server",
                        description=(
                            "Another subscriber claimed this server between "
                            "the time you confirmed and now. Run "
                            "`/premium assign` from a different server, or "
                            "`/premium overview` to manage your subscription."
                        ),
                        color=discord.Color.orange(),
                    ),
                    ephemeral=True,
                )
                return
            confirm_embed = discord.Embed(
                title="💎 Premium is now active in this server",
                description=(
                    f"**{this_name}** is now Premium. Run `/premium overview` to "
                    "manage it later, or `/premium unassign` to release it."
                ),
                color=discord.Color.gold(),
            )
            await interaction.followup.send(embed=confirm_embed, ephemeral=True)
            return

        # Reassignment — confirm with the prior guild named so the move
        # is unambiguous.
        prior_name = await _resolve_guild_name(self.bot, current)
        embed = discord.Embed(
            title="💎 Switch Premium to this server?",
            description=(
                f"Currently active in: **{prior_name}**\n"
                f"After switch: **{this_name}** will be Premium, "
                f"**{prior_name}** will revert to Free."
            ),
            color=discord.Color.purple(),
        )

        view = _ConfirmActionView(
            owner_id=user_id,
            confirm_label=f"Yes, switch to {this_name}"[:80],
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()
        await view.wait()

        if not view.confirmed:
            await interaction.followup.send(
                "Cancelled — your subscription is unchanged.",
                ephemeral=True,
            )
            return

        if not premium.assign(user_id, guild_id):
            await interaction.followup.send(
                embed=discord.Embed(
                    title="⚠️ Couldn't switch to this server",
                    description=(
                        "Another subscriber claimed this server between "
                        "the time you confirmed and now. Run "
                        f"`/premium overview` to confirm **{prior_name}** is "
                        "still your active server, or try `/premium assign` "
                        "from a different one."
                    ),
                    color=discord.Color.orange(),
                ),
                ephemeral=True,
            )
            return
        confirm_embed = discord.Embed(
            title="💎 Premium switched to this server",
            description=(
                f"**{this_name}** is now Premium. **{prior_name}** has reverted "
                "to Free. You can switch again any time with `/premium assign`."
            ),
            color=discord.Color.gold(),
        )
        await interaction.followup.send(embed=confirm_embed, ephemeral=True)

    # ── /premium unassign ─────────────────────────────────────────────────────

    @premium_group.command(
        name="unassign",
        description="Release your Premium pin without canceling the subscription",
    )
    async def premium_unassign(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        # Look up the current assignment before mutating, so the
        # confirmation can name the guild that's about to revert.
        current = premium.get_assigned_guild(user_id)
        if current is None:
            embed = discord.Embed(
                title="💎 Nothing to release",
                description=(
                    "You don't have a Premium assignment to release. Run "
                    "`/premium overview` to see your subscription state."
                ),
                color=discord.Color.purple(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        current_name = await _resolve_guild_name(self.bot, current)
        embed = discord.Embed(
            title="💎 Release Premium pin?",
            description=(
                f"**{current_name}** is currently pinned to your subscription. "
                f"Releasing will revert **{current_name}** to Free; your "
                "subscription stays active and can be reassigned to any "
                "server with `/premium assign`."
            ),
            color=discord.Color.purple(),
        )

        view = _ConfirmActionView(
            owner_id=user_id,
            confirm_label=f"Yes, release {current_name}"[:80],
            confirm_style=discord.ButtonStyle.danger,
        )
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()
        await view.wait()

        if not view.confirmed:
            await interaction.followup.send(
                "Cancelled — your assignment is unchanged.",
                ephemeral=True,
            )
            return

        # Re-check the assignment in case it changed between prompt and
        # confirm (e.g. /premium unassign run twice in parallel windows).
        # The unassign helper's idempotent: returns None if already gone.
        freed = premium.unassign(user_id)
        if freed is None:
            await interaction.followup.send(
                "Nothing to release — your assignment was already cleared before you confirmed.",
                ephemeral=True,
            )
            return

        freed_name = await _resolve_guild_name(self.bot, freed)
        confirm_embed = discord.Embed(
            title="💎 Premium pin released",
            description=(
                f"**{freed_name}** has reverted to Free. Your subscription is "
                "still active — run `/premium assign` from any server you want "
                "to use it in next."
            ),
            color=discord.Color.orange(),
        )
        await interaction.followup.send(embed=confirm_embed, ephemeral=True)

    # ── Entitlement listeners ─────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_entitlement_create(self, entitlement: discord.Entitlement):
        """Auto-assign on first checkout. If the new subscriber ran /upgrade
        in a guild, pin their license there; otherwise DM them prompting a
        manual /premium assign."""
        if premium.PREMIUM_SKU_ID is None:
            return
        if getattr(entitlement, "sku_id", None) != premium.PREMIUM_SKU_ID:
            return
        user_id = getattr(entitlement, "user_id", None)
        if user_id is None:
            return  # User Subscription should always have user_id; defensive

        # Refresh caches first — we may now resolve premium=True for the
        # assigned guild on the next is_premium call.
        premium.invalidate_for_user(user_id)

        existing_assignment = premium.get_assigned_guild(user_id)
        if existing_assignment is not None:
            # Resubscribe path. Assignment was preserved through the lapse;
            # cache invalidation above is enough — premium auto-resumes.
            return

        guild_id = self._pending_upgrade_guilds.pop(user_id, None)
        if guild_id is None:
            # Subscribed without going through /upgrade (e.g. via Discord's
            # app store directly). Tell them how to assign.
            await self._dm_assignment_prompt(user_id)
            return

        holder = premium.get_assigned_user(guild_id)
        if holder is not None and holder != user_id:
            # Race: another subscriber claimed this guild after /upgrade was
            # opened but before checkout completed. Tell the new subscriber
            # to pick a different guild.
            await self._dm_blocked_assignment(user_id, guild_id, holder)
            return

        if not premium.assign(user_id, guild_id):
            # Race between the pre-check above and this assign call.
            # Re-resolve the new holder so the user gets a helpful DM.
            new_holder = premium.get_assigned_user(guild_id)
            if new_holder is not None:
                await self._dm_blocked_assignment(user_id, guild_id, new_holder)
            else:
                await self._dm_assignment_prompt(user_id)
            return
        await self._dm_auto_assigned(user_id, guild_id)

    @commands.Cog.listener()
    async def on_entitlement_delete(self, entitlement: discord.Entitlement):
        """When a subscription ends, drop cached state so the assigned guild
        flips to Free on the next is_premium check. The assignment row is
        preserved so a future resubscribe auto-resumes in the same guild."""
        if premium.PREMIUM_SKU_ID is None:
            return
        if getattr(entitlement, "sku_id", None) != premium.PREMIUM_SKU_ID:
            return
        user_id = getattr(entitlement, "user_id", None)
        if user_id is None:
            return
        premium.invalidate_for_user(user_id)

    # ── Post-subscribe DMs ────────────────────────────────────────────────────

    async def _dm_user(self, user_id: int, embed: discord.Embed) -> None:
        try:
            user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
            await user.send(embed=embed)
        except discord.Forbidden:
            print(
                f"[PREMIUM] Cannot DM user {user_id} (DMs closed); "
                f"they will need to run /premium overview manually"
            )
        except Exception as exc:
            print(f"[PREMIUM] Failed to DM user {user_id} after entitlement event: {exc}")

    async def _dm_auto_assigned(self, user_id: int, guild_id: int) -> None:
        guild_name = await _resolve_guild_name(self.bot, guild_id)
        embed = discord.Embed(
            title="💎 Premium is active!",
            description=(
                f"Thanks for subscribing to LW Alliance Helper Premium!\n\n"
                f"Your subscription has been automatically assigned to "
                f"**{guild_name}**. A subscription can only be active in "
                f"one server at a time — if you want to apply it to a "
                f"different alliance later, run `/premium assign` from that "
                f"server, or `/premium overview` to manage it."
            ),
            color=discord.Color.gold(),
        )
        await self._dm_user(user_id, embed)

    async def _dm_assignment_prompt(self, user_id: int) -> None:
        embed = discord.Embed(
            title="💎 Premium subscription confirmed",
            description=(
                "Thanks for subscribing to LW Alliance Helper Premium!\n\n"
                "Your subscription isn't pinned to a server yet. Run "
                "`/premium assign` from the alliance Discord you'd like "
                "Premium to apply to. A subscription can only be active in "
                "one server at a time — you can move it any time with "
                "`/premium assign`, or check the status with `/premium overview`."
            ),
            color=discord.Color.purple(),
        )
        await self._dm_user(user_id, embed)

    async def _dm_blocked_assignment(self, user_id: int, guild_id: int, holder_id: int) -> None:
        guild_name = await _resolve_guild_name(self.bot, guild_id)
        holder_label = await _resolve_user_label(self.bot, holder_id)
        embed = discord.Embed(
            title="💎 Premium subscription confirmed",
            description=(
                f"Thanks for subscribing!\n\n"
                f"⚠️ **{guild_name}** already has Premium from "
                f"{holder_label}'s subscription, so we couldn't auto-assign "
                f"yours there. Run `/premium assign` from a different "
                f"alliance Discord to pin your subscription, or "
                f"`/premium overview` to manage it later."
            ),
            color=discord.Color.orange(),
        )
        await self._dm_user(user_id, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(DonateCog(bot))
