"""buddy_hub.py — the single `/buddy` hub for the Profession Buddy System (#289).

One command opens a hub that adapts to tier and role:

- **Everyone:** 🔍 Who's my buddy? · 📋 View buddy list
- **Leadership:** ✏️ Manage pairings · 📤 Post buddy list · ⚙️ Open setup
- **Premium leadership:** 🪄 Auto-assign · ♻️ Re-pair from scratch ·
  📌 Post self-service buttons

The member-facing lookup is free and works whether the caller is a War Leader
or an Engineer. Leadership actions are role-gated; the Premium actions gate via
``premium.feature_gate`` and fall back to an upgrade prompt.
"""

import asyncio
import logging
from typing import Optional

import discord

import buddy
import buddy_ui as ui

logger = logging.getLogger(__name__)

BUDDY_HUB_TITLE = "🤝 Profession Buddy System"
BUDDY_HUB_CMD = "/buddy"

_DENY_NOT_OWNER = "⛔ Only the person who opened this hub can use these buttons."
_DENY_NOT_LEADER = "⛔ That action is for leadership only."


def _build_hub_embed(guild_id: int, cfg: dict, *, is_premium: bool) -> discord.Embed:
    """Light, DB-only hub embed (no Sheet reads, so `/buddy` opens fast)."""
    embed = discord.Embed(title=BUDDY_HUB_TITLE, color=discord.Color.blurple())
    embed.description = (
        "Pair your War Leaders with Engineers so the daily buff Skill always "
        "has a home. Tap **Who's my buddy?** to see your match."
    )

    def _ch(cid):
        return f"<#{cid}>" if cid else "*not set*"

    doubling = "✅ on" if cfg.get("engineer_doubling") else "❌ off"
    scarcity = (
        "strongest first" if cfg.get("scarcity_priority") == "strongest_first" else "alphabetical"
    )
    posted = "✅ posted" if cfg.get("persistent_message_id") else "❌ not posted"
    lines = [
        f"**Buddy tab:** {cfg.get('buddy_tab') or 'Buddies'}",
        f"**Two Engineers per War Leader:** {doubling}",
        f"**When Engineers are scarce:** {scarcity}",
        f"**Leadership alerts:** {_ch(cfg.get('notify_channel_id'))}",
        f"**Self-service buttons:** {posted}",
    ]
    embed.add_field(name="Settings", value="\n".join(lines), inline=False)
    if not is_premium:
        embed.add_field(
            name="💎 Premium",
            value=(
                "Auto-assign, one-click profession swapping, auto re-pairing with "
                "leadership alerts, and buddy DMs are part of Premium. Run `/upgrade`."
            ),
            inline=False,
        )
    embed.set_footer(text=f"Buddy hub · {BUDDY_HUB_CMD}")
    return embed


class _ConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, on_confirm):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self._on_confirm = on_confirm
        yes = discord.ui.Button(label="♻️ Yes, rebuild", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        yes.callback = self._yes
        no.callback = self._no
        self.add_item(yes)
        self.add_item(no)

    async def interaction_check(self, inter):
        if inter.user.id != self.owner_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _yes(self, inter: discord.Interaction):
        for c in self.children:
            c.disabled = True
        await self._on_confirm(inter)
        self.stop()

    async def _no(self, inter: discord.Interaction):
        await inter.response.edit_message(content="Cancelled. No pairings changed.", view=None)
        self.stop()


class _BuddyHubView(discord.ui.View):
    def __init__(
        self, bot, guild_id: int, owner_user_id: int, *, is_leader: bool, is_premium: bool
    ):
        super().__init__(timeout=900)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_user_id = owner_user_id
        self.is_leader = is_leader
        self.is_premium = is_premium
        self.message: Optional[discord.Message] = None
        self._build()

    async def interaction_check(self, inter):
        if inter.user.id != self.owner_user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=BUDDY_HUB_CMD)

    def _add(self, label, style, row, cb):
        btn = discord.ui.Button(label=label[:80], style=style, row=row)
        btn.callback = cb
        self.add_item(btn)

    def _build(self):
        self._add("🔍 Who's my buddy?", discord.ButtonStyle.primary, 0, self._whoami)
        self._add("📋 View buddy list", discord.ButtonStyle.secondary, 0, self._view_list)
        if self.is_leader:
            self._add("✏️ Manage pairings", discord.ButtonStyle.success, 1, self._manage)
            self._add("📤 Post buddy list", discord.ButtonStyle.secondary, 1, self._post_list)
            self._add("⚙️ Open setup", discord.ButtonStyle.secondary, 1, self._setup)
            self._add("🪄 Auto-assign", discord.ButtonStyle.success, 2, self._auto_assign)
            self._add("♻️ Re-pair from scratch", discord.ButtonStyle.danger, 2, self._from_scratch)
            self._add(
                "📌 Post self-service buttons", discord.ButtonStyle.secondary, 2, self._post_buttons
            )

    # ── everyone ──────────────────────────────────────────────────────────────

    async def _whoami(self, inter: discord.Interaction):
        import config

        await inter.response.defer(ephemeral=True)
        cfg = config.get_buddy_config(self.guild_id)
        result = await asyncio.to_thread(ui.compute_current, self.guild_id, cfg)
        await inter.followup.send(
            ui.describe_my_buddy(result, str(inter.user.id), inter.user.display_name),
            ephemeral=True,
        )

    async def _view_list(self, inter: discord.Interaction):
        import config

        await inter.response.defer(ephemeral=True)
        cfg = config.get_buddy_config(self.guild_id)
        result = await asyncio.to_thread(ui.compute_current, self.guild_id, cfg)
        embed = ui.build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
        await inter.followup.send(embed=embed, ephemeral=True)

    # ── leadership ────────────────────────────────────────────────────────────

    def _leader_ok(self, inter) -> bool:
        from train import _is_leadership

        return _is_leadership(inter)

    async def _manage(self, inter: discord.Interaction):
        if not self._leader_ok(inter):
            await inter.response.send_message(_DENY_NOT_LEADER, ephemeral=True)
            return
        import config

        cfg = config.get_buddy_config(self.guild_id)
        result = await asyncio.to_thread(ui.compute_current, self.guild_id, cfg)
        embed = ui.build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
        view = ui.BuddyManageView(self.bot, self.guild_id, inter.user.id)
        await inter.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await inter.original_response()

    async def _post_list(self, inter: discord.Interaction):
        if not self._leader_ok(inter):
            await inter.response.send_message(_DENY_NOT_LEADER, ephemeral=True)
            return
        import config

        await inter.response.defer(ephemeral=True)
        cfg = config.get_buddy_config(self.guild_id)
        result = await asyncio.to_thread(ui.compute_current, self.guild_id, cfg)
        embed = ui.build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
        try:
            await inter.channel.send(embed=embed)
            await inter.followup.send("📤 Posted the buddy list here.", ephemeral=True)
        except discord.HTTPException:
            await inter.followup.send("⚠️ Couldn't post in this channel.", ephemeral=True)

    async def _setup(self, inter: discord.Interaction):
        if not self._leader_ok(inter):
            await inter.response.send_message(_DENY_NOT_LEADER, ephemeral=True)
            return
        from setup_cog import run_buddy_setup

        await inter.response.send_message("⚙️ Opening Buddy System setup below…", ephemeral=True)
        await run_buddy_setup(inter, self.bot)

    # ── premium leadership ────────────────────────────────────────────────────

    async def _premium_guard(self, inter, feature: str) -> bool:
        import premium

        if not self._leader_ok(inter):
            await inter.response.send_message(_DENY_NOT_LEADER, ephemeral=True)
            return False
        if not await premium.feature_gate(feature, self.guild_id, bot=self.bot):
            view = premium.upgrade_view()
            await inter.response.send_message(
                embed=premium.premium_locked_embed(
                    feature_label="This buddy action",
                    description=(
                        "Auto-assignment and self-service buttons are part of "
                        "💎 LW Alliance Helper Premium. Run `/upgrade` to unlock them."
                    ),
                ),
                view=view,
                ephemeral=True,
            )
            return False
        return True

    async def _auto_assign(self, inter: discord.Interaction):
        if not await self._premium_guard(inter, "buddy_auto_assign"):
            return
        import config

        await inter.response.defer(ephemeral=True)
        cfg = config.get_buddy_config(self.guild_id)
        result = await asyncio.to_thread(
            ui.compute_autofill, self.guild_id, cfg, from_scratch=False
        )
        await asyncio.to_thread(ui.save_result, self.guild_id, cfg, result)
        await ui.refresh_persistent_message(self.bot, self.guild_id, cfg, result)
        embed = ui.build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
        await inter.followup.send(
            content="🪄 Buddies assigned (existing pairs kept).", embed=embed, ephemeral=True
        )

    async def _from_scratch(self, inter: discord.Interaction):
        if not await self._premium_guard(inter, "buddy_auto_assign"):
            return

        async def _do(i: discord.Interaction):
            import config

            await i.response.defer(ephemeral=True)
            cfg = config.get_buddy_config(self.guild_id)
            result = await asyncio.to_thread(
                ui.compute_autofill, self.guild_id, cfg, from_scratch=True
            )
            await asyncio.to_thread(ui.save_result, self.guild_id, cfg, result)
            await ui.refresh_persistent_message(self.bot, self.guild_id, cfg, result)
            embed = ui.build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
            await i.followup.send(
                content="♻️ Rebuilt every pairing from scratch.", embed=embed, ephemeral=True
            )

        await inter.response.send_message(
            "⚠️ This ignores existing pairings and rebuilds the whole list. "
            "People may get a different buddy. Continue?",
            view=_ConfirmView(inter.user.id, _do),
            ephemeral=True,
        )

    async def _post_buttons(self, inter: discord.Interaction):
        if not await self._premium_guard(inter, "buddy_self_service"):
            return
        await inter.response.defer(ephemeral=True)
        msg = await ui.post_self_service_message(self.bot, inter.channel, self.guild_id)
        if msg is not None:
            await inter.followup.send(
                "📌 Posted the self-service profession message here. Members can set "
                "their profession and check their buddy from it.",
                ephemeral=True,
            )
        else:
            await inter.followup.send(
                "⚠️ Couldn't post the message in this channel.", ephemeral=True
            )


async def handle_buddy_hub(bot, interaction: discord.Interaction) -> None:
    """Top-level handler for `/buddy`. Setup-complete gate only — the buddy
    lookup is available to every member, not just leadership."""
    import config
    from messages import NOT_SET_UP
    from train import _is_leadership

    cfg_guild = config.get_config(interaction.guild_id)
    if not cfg_guild or not cfg_guild.setup_complete:
        await interaction.response.send_message(NOT_SET_UP, ephemeral=True)
        return

    bcfg = config.get_buddy_config(interaction.guild_id)
    is_leader = _is_leadership(interaction)

    if not bcfg.get("enabled"):
        if is_leader:
            from setup_cog import run_buddy_setup

            await interaction.response.send_message(
                "The Profession Buddy System isn't turned on yet. Opening setup below…",
                ephemeral=True,
            )
            await run_buddy_setup(interaction, bot)
        else:
            await interaction.response.send_message(
                "The Profession Buddy System isn't set up for this alliance yet. "
                "Ask your leadership to enable it.",
                ephemeral=True,
            )
        return

    import premium

    is_premium = await premium.is_premium(interaction.guild_id, interaction=interaction, bot=bot)
    embed = _build_hub_embed(interaction.guild_id, bcfg, is_premium=is_premium)
    view = _BuddyHubView(
        bot, interaction.guild_id, interaction.user.id, is_leader=is_leader, is_premium=is_premium
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    view.message = await interaction.original_response()
