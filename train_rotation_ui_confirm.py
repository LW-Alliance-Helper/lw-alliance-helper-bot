"""
train_rotation_ui_confirm.py — the daily-confirmation surface of Train
Conductor Rotation (#55), split out of train_rotation_ui.py (#373).

DailyConfirmView — each drive day's confirmation. Confirm posts the
conductor publicly (blurb + optional image URL — modals can't upload files).

Shared infrastructure (embeds, RotationState, the roster picker also used by
the weekly-draft surface, load helpers) stays in train_rotation_ui.py;
referenced here as `ui.X` (not a bound import) so the existing
`patch.object(ui, ...)` / `patch("train_rotation_ui.X", ...)` test hooks
keep working regardless of which file actually defines the symbol.
"""

import asyncio
from datetime import date, datetime
from zoneinfo import ZoneInfo

import discord

import wizard_registry
import train_rotation as tr
import train_rotation_ui as ui


class _ConfirmPostModal(discord.ui.Modal, title="Post Train Conductor"):
    """Captures an optional blurb + image URL, then posts the conductor publicly.

    Image is a URL (not an upload) because Discord modals can't carry file
    attachments — leadership pastes any image link, or leaves it blank."""

    def __init__(self, view: "DailyConfirmView"):
        super().__init__()
        self._view = view
        self.blurb = discord.ui.TextInput(
            label="Blurb (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
        )
        self.image_url = discord.ui.TextInput(
            label="Image URL (optional)",
            placeholder="https://…  (paste a link; uploads aren't possible here)",
            required=False,
            max_length=400,
        )
        self.add_item(self.blurb)
        self.add_item(self.image_url)

    async def on_submit(self, interaction: discord.Interaction):
        await self._view.do_confirm(
            interaction, blurb=self.blurb.value.strip(), image_url=self.image_url.value.strip()
        )


class DailyConfirmView(discord.ui.View):
    """Each drive day's confirmation. Confirm writes a `posted` history row and —
    when a public channel is configured — announces the conductor there (with an
    optional blurb + image). With no public channel it just records the
    conductor. The other buttons adjust the conductor first.

    `public_channel_id` of 0 means the alliance opted out of public posts."""

    def __init__(self, bot, guild_id: int, draft_day: tr.DraftDay, public_channel_id: int):
        super().__init__(timeout=ui.EDITOR_TIMEOUT)
        self.bot = bot
        self.guild_id = guild_id
        self.dd = draft_day
        self.public_channel_id = public_channel_id
        self.message: discord.Message | None = None
        # Label the confirm button by whether it posts publicly.
        self.confirm.label = (
            "✅ Confirm + post publicly" if public_channel_id else "✅ Confirm conductor"
        )

    @discord.ui.button(label="✅ Confirm conductor", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not ui._is_leader(interaction):
            await interaction.response.send_message(ui.DENY_NOT_LEADER, ephemeral=True)
            return
        if not self.dd.member:
            await interaction.response.send_message(
                "⚠️ No conductor set. Use **✏️ Manually assign** or **⏭️ Select next person** first.",
                ephemeral=True,
            )
            return
        if self.public_channel_id:
            # Public post configured → collect an optional blurb + image first.
            await interaction.response.send_modal(_ConfirmPostModal(self))
        else:
            # No public channel → just record the conductor as posted.
            await self.do_confirm(interaction, blurb="", image_url="")

    @discord.ui.button(label="⏭️ Go to next person", style=discord.ButtonStyle.primary)
    async def next_person(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not ui._is_leader(interaction):
            await interaction.response.send_message(ui.DENY_NOT_LEADER, ephemeral=True)
            return
        await interaction.response.defer()
        state = await ui.load_rotation_state_async(self.bot, self.guild_id)
        member, reason, needs = tr.reroll_day(
            self.dd,
            eligible_pool=state.eligible_pool,
            role_pools=state.role_pools,
            member_rules=state.member_rules,
            history=state.history,
            counted_reasons=state.counted_reasons,
            other_scheduled=set(),
            target_date=date.fromisoformat(self.dd.date),
            role_rules_enabled=state.role_rules_enabled,
        )
        self.dd.member, self.dd.reason, self.dd.needs_picking = member, reason, needs
        await asyncio.to_thread(self._persist_scheduled)
        await self._refresh(interaction)

    @discord.ui.button(label="✏️ Assign someone", style=discord.ButtonStyle.secondary)
    async def assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not ui._is_leader(interaction):
            await interaction.response.send_message(ui.DENY_NOT_LEADER, ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        state = await ui.load_rotation_state_async(self.bot, self.guild_id)
        names, scope = ui._assign_pool_for_day(state, self.dd.rule_type)
        full_names = tr.roster_names(state.roster)

        async def _commit(name: str):
            self.dd.member = name
            self.dd.reason = "manual"
            self.dd.needs_picking = False
            await asyncio.to_thread(self._persist_scheduled)
            if self.message:
                try:
                    await self.message.edit(embed=ui.build_daily_confirm_embed(self.dd), view=self)
                except discord.HTTPException:
                    pass

        picker = ui._RosterPickerView(
            names,
            current=self.dd.member or "",
            prompt="Who drives today's train?",
            modal_title="Assign today's conductor",
            on_commit=_commit,
            full_names=full_names,
            scope=scope,
            full_scope="\nShowing the **full roster**.",
        )
        await interaction.followup.send(content=picker.content(), view=picker, ephemeral=True)

    def _persist_scheduled(self):
        from config import get_train_config

        tab = get_train_config(self.guild_id).get("history_tab") or ""
        tr.set_day_status(
            self.guild_id,
            tab,
            self.dd.date,
            member=self.dd.member or "",
            reason=self.dd.reason,
            status=tr.STATUS_SCHEDULED,
            notes=self.dd.note,
        )

    async def _refresh(self, interaction: discord.Interaction):
        embed = ui.build_daily_confirm_embed(self.dd)
        try:
            if self.message:
                await self.message.edit(embed=embed, view=self)
            elif not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            pass

    async def do_confirm(self, interaction: discord.Interaction, *, blurb: str, image_url: str):
        """Write the posted row and, when a public channel is configured,
        announce the conductor there. With no public channel it just records."""
        from config import get_train_config

        if not interaction.response.is_done():
            await interaction.response.defer()

        # Public announcement (only when a channel was configured).
        if self.public_channel_id:
            channel = self.bot.get_channel(self.public_channel_id)
            if channel is None:
                await interaction.followup.send(
                    "⚠️ The public post channel isn't reachable. Re-check it in "
                    "`/setup` → 🚂 Train.",
                    ephemeral=True,
                )
                return
            embed = ui.build_public_post_embed(self.dd, blurb=blurb, image_url=image_url)
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                await interaction.followup.send(
                    f"⚠️ I can't post in <#{self.public_channel_id}>. Grant me View Channel and "
                    "Send Messages there.",
                    ephemeral=True,
                )
                return

        now_iso = datetime.now(tz=ZoneInfo("UTC")).isoformat(timespec="minutes")
        tab = get_train_config(self.guild_id).get("history_tab") or ""
        await asyncio.to_thread(
            tr.set_day_status,
            self.guild_id,
            tab,
            self.dd.date,
            member=self.dd.member or "",
            reason=self.dd.reason,
            status=tr.STATUS_POSTED,
            posted_at=now_iso,
            notes=self.dd.note,
        )
        for item in self.children:
            item.disabled = True
        if self.message:
            done = (
                f"✅ Posted **{self.dd.member}** to <#{self.public_channel_id}>."
                if self.public_channel_id
                else f"✅ Recorded **{self.dd.member}** as today's conductor."
            )
            try:
                await self.message.edit(content=done, view=self)
            except discord.HTTPException:
                pass
        self.stop()

    async def on_timeout(self):
        await wizard_registry.expire_view_message(self.message, command_hint="/train draft_week")
