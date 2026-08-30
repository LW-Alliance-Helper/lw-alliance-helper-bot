"""buddy_hub.py — the single `/buddy` hub for the Profession Buddy System (#289).

One command opens a hub that adapts to tier and role:

- **Everyone:** 🔍 Who's my buddy? · 📋 View buddy list
- **Leadership:** ✏️ Manage pairings · 🔄 Refresh from sheet · 📣 Post buddy
  list · ↩️ Undo last change · ⚙️ Open setup
- **Premium leadership:** ✨ Auto-assign · ♻️ Re-pair from scratch ·
  📣 Post self-service buttons · 💾 Save as preset

📂 Load preset and 🗑️ Delete preset are leadership but **not** Premium — saving
a lineup is the paid part; using or clearing one you already saved is not, so a
lapse never strands a preset the alliance made while paying.

Every action that writes captures the pair list first, so ↩️ Undo can put it
back. That snapshot lives on the view for the length of the sitting and is
never stored (#289 F-03). Every action that writes also reports what it
dropped and why, rather than the fixed "invalid pairs were cleared" line that
told an alliance nothing (#289 F-06).

Named presets (Stage 3) keep a lineup on the alliance's own sheet rather than
in our database — `buddy.save_preset` and friends, shaped after
`storm_strategy`'s zone presets. Loading one goes through the ordinary
validator, so a preset restores a lineup but can never bring back someone who
has left or contradict the profession survey.

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
                "leadership alerts, saving lineups as presets, and buddy DMs are part "
                "of Premium. Run `/upgrade`."
            ),
            inline=False,
        )
    embed.set_footer(text=f"Buddy hub · {BUDDY_HUB_CMD}")
    return embed


# Enough names to see who's going without pushing the confirmation past
# Discord's message limit; the count in the sentence above carries the rest.
_MAX_NAMED_DROPS = 15


def _name_list(names: list) -> str:
    """Bulleted preview of names, truncated with a remainder count."""
    shown = [f"• {n}" for n in names[:_MAX_NAMED_DROPS]]
    extra = len(names) - _MAX_NAMED_DROPS
    if extra > 0:
        shown.append(f"• …and {extra} more")
    return "\n".join(shown)


def _with_report(text: str, result) -> str:
    """Append the what-changed block when an action dropped any pairings.

    Every write goes through this, so "3 pairings cleared" always arrives with
    the three names and three reasons attached (#289 F-06)."""
    report = ui.describe_dropped(result)
    return f"{text}\n\n{report}" if report else text


class _ConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, on_confirm, confirm_label: str = "♻️ Yes, rebuild"):
        super().__init__(timeout=60)
        self.owner_id = owner_id
        self._on_confirm = on_confirm
        yes = discord.ui.Button(label=confirm_label, style=discord.ButtonStyle.danger)
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
        await inter.response.edit_message(content="Canceled. No pairings changed.", view=None)
        self.stop()


class _ConflictView(discord.ui.View):
    """Hands back the calls the pairing logic shouldn't be making on its own.

    Two situations leave two pairings that can't both stand: one Engineer
    listed with two War Leaders, and a War Leader holding two Engineers while
    doubling is off. Which one survived used to be decided alphabetically and
    reported as a fact (#289 F-04, F-05). The list still settles on a valid
    default so nothing is left broken, but the officer is offered the swap.

    Conflicts are recomputed after every resolution rather than worked through
    from a stale list, because settling one can change what the others are.
    """

    def __init__(self, hub, cfg: dict, conflicts: list):
        super().__init__(timeout=300)
        self.hub = hub
        self.cfg = cfg
        self.conflicts = conflicts
        n = len(conflicts)
        btn = discord.ui.Button(
            label=f"⚖️ Choose ({n})" if n > 1 else "⚖️ Choose",
            style=discord.ButtonStyle.primary,
        )
        btn.callback = self._open
        self.add_item(btn)

    async def interaction_check(self, inter):
        if inter.user.id != self.hub.owner_user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    def _label(self, d) -> str:
        if d.reason == buddy.DROP_ENGINEER_TAKEN:
            return f"{d.engineer}: {d.kept.war_leader} or {d.war_leader}"[:100]
        return f"{d.war_leader}: {d.kept.engineer} or {d.engineer}"[:100]

    async def _open(self, inter: discord.Interaction):
        if len(self.conflicts) == 1:
            await self._offer(inter, self.conflicts[0], deferred=False)
            return
        opts = [
            discord.SelectOption(label=self._label(d), value=str(i))
            for i, d in enumerate(self.conflicts)
        ]

        async def _pick(i: discord.Interaction, value: str):
            await self._offer(i, self.conflicts[int(value)], deferred=False)

        await inter.response.send_message(
            "Which one would you like to settle?",
            view=ui.PickerView(opts, inter.user.id, _pick, placeholder="Pick a pairing…"),
            ephemeral=True,
        )

    async def _offer(self, inter: discord.Interaction, conflict, *, deferred: bool):
        options = ui.conflict_options(conflict)
        if not options:
            await inter.response.send_message(
                "ℹ️ That one has already been settled.", ephemeral=True
            )
            return
        opts = [
            discord.SelectOption(label=label[:100], value=str(i))
            for i, (label, _pair) in enumerate(options)
        ]

        async def _pick(i: discord.Interaction, value: str):
            await i.response.defer(ephemeral=True, thinking=True)
            await self._apply(i, conflict, options[int(value)][1])

        await inter.response.send_message(
            ui.describe_conflict(conflict),
            view=ui.PickerView(opts, inter.user.id, _pick, placeholder="Pick one…"),
            ephemeral=True,
        )

    async def _apply(self, inter: discord.Interaction, conflict, chosen):
        tab = self.cfg.get("buddy_tab")
        pairs = await asyncio.to_thread(buddy.load_pairs, self.hub.guild_id, tab)
        self.hub.session.capture(pairs)
        pairs = ui.resolve_conflict(pairs, conflict, chosen)
        result = await asyncio.to_thread(ui.apply_pairs, self.hub.guild_id, self.cfg, pairs)
        await ui.refresh_persistent_message(self.hub.bot, self.hub.guild_id, self.cfg, result)
        embed = ui.build_buddy_list_embed(result, doubling=bool(self.cfg.get("engineer_doubling")))
        await inter.followup.send(
            content=_with_report(
                f"**{chosen.war_leader}** is now paired with **{chosen.engineer}**.", result
            ),
            embed=embed,
            ephemeral=True,
            **_conflict_kwargs(self.hub, self.cfg, result),
        )


def _conflict_kwargs(hub, cfg: dict, result) -> dict:
    """``{"view": ...}`` when an action left something for the officer to
    decide, or ``{}``. Built as kwargs because discord.py's ``send`` treats a
    ``None`` view as a real one and would raise on it."""
    conflicts = ui.conflicts_in(result)
    return {"view": _ConflictView(hub, cfg, conflicts)} if conflicts else {}


class _PresetNameModal(discord.ui.Modal, title="Save current pairings as a preset"):
    """Names the preset the current pairings get written to.

    An existing name is replaced rather than refused — updating a saved lineup
    is the common case, and the confirmation says which of the two happened."""

    def __init__(self, hub):
        super().__init__()
        self.hub = hub
        self.preset_name = discord.ui.TextInput(
            label="Preset name",
            placeholder="e.g. Season 4 Opener",
            required=True,
            max_length=buddy.MAX_PRESET_NAME,
        )
        self.add_item(self.preset_name)

    async def on_submit(self, interaction: discord.Interaction):
        await self.hub._commit_preset(interaction, (self.preset_name.value or "").strip())


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
        # This officer's sitting. Holds the single-step undo in memory and
        # nothing else; it dies with the view (#289 F-03).
        self.session = ui.BuddySession()
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
            self._add(
                "🔄 Refresh from sheet", discord.ButtonStyle.secondary, 1, self._refresh_sheet
            )
            self._add("📣 Post buddy list", discord.ButtonStyle.secondary, 1, self._post_list)
            self._add("↩️ Undo last change", discord.ButtonStyle.secondary, 1, self._undo)
            self._add("⚙️ Open setup", discord.ButtonStyle.secondary, 1, self._setup)
            self._add("✨ Auto-assign", discord.ButtonStyle.success, 2, self._auto_assign)
            self._add("♻️ Re-pair from scratch", discord.ButtonStyle.danger, 2, self._from_scratch)
            self._add(
                "📣 Post self-service buttons", discord.ButtonStyle.secondary, 2, self._post_buttons
            )
            self._add("💾 Save as preset", discord.ButtonStyle.secondary, 3, self._save_preset)
            self._add("📂 Load preset", discord.ButtonStyle.secondary, 3, self._load_preset)
            self._add("🗑️ Delete preset", discord.ButtonStyle.secondary, 3, self._delete_preset)

    # ── everyone ──────────────────────────────────────────────────────────────

    async def _whoami(self, inter: discord.Interaction):
        import config

        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = config.get_buddy_config(self.guild_id)
        result = await asyncio.to_thread(ui.compute_current, self.guild_id, cfg)
        await inter.followup.send(
            ui.describe_my_buddy(result, str(inter.user.id), inter.user.display_name),
            ephemeral=True,
        )

    async def _view_list(self, inter: discord.Interaction):
        import config

        await inter.response.defer(ephemeral=True, thinking=True)
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

        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = config.get_buddy_config(self.guild_id)
        result = await asyncio.to_thread(ui.compute_current, self.guild_id, cfg)
        embed = ui.build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
        view = ui.BuddyManageView(self.bot, self.guild_id, inter.user.id, session=self.session)
        view.message = await inter.followup.send(embed=embed, view=view, ephemeral=True)

    async def _refresh_sheet(self, inter: discord.Interaction):
        """Re-read the Google Sheet (buddy tab + Squad Powers), normalize it into
        the bot's layout, and update the list. For officers who edit the sheet by
        hand and want the bot to pick up their changes without auto-pairing."""
        if not self._leader_ok(inter):
            await inter.response.send_message(_DENY_NOT_LEADER, ephemeral=True)
            return
        import config

        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = config.get_buddy_config(self.guild_id)
        self.session.capture(await asyncio.to_thread(ui.snapshot_pairs, self.guild_id, cfg))
        # fill=False inside compute_current: keep exactly what's in the sheet
        # (drop only invalid pairs); leave gap-filling to the Auto-assign button.
        result = await asyncio.to_thread(ui.compute_current, self.guild_id, cfg)
        await asyncio.to_thread(ui.save_result, self.guild_id, cfg, result)
        await ui.refresh_persistent_message(self.bot, self.guild_id, cfg, result)
        embed = ui.build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
        await inter.followup.send(
            content=_with_report(
                "🔄 Your buddy list has been synced from your sheet. "
                "Use ✨ Auto-assign to fill any gaps.",
                result,
            ),
            embed=embed,
            ephemeral=True,
            **_conflict_kwargs(self, cfg, result),
        )

    async def _undo(self, inter: discord.Interaction):
        """Put the pair list back the way it was before this sitting's last write.

        One step, and only within this sitting — the snapshot lives on the view
        and is never stored (#289 F-03). Anything further back is what a saved
        preset is for."""
        if not self._leader_ok(inter):
            await inter.response.send_message(_DENY_NOT_LEADER, ephemeral=True)
            return
        import config

        await inter.response.defer(ephemeral=True, thinking=True)
        snapshot = self.session.take()
        if snapshot is None:
            await inter.followup.send(
                "ℹ️ Nothing to undo. You haven't changed anything since opening this hub.",
                ephemeral=True,
            )
            return
        cfg = config.get_buddy_config(self.guild_id)
        result = await asyncio.to_thread(ui.apply_pairs, self.guild_id, cfg, snapshot)
        await ui.refresh_persistent_message(self.bot, self.guild_id, cfg, result)
        embed = ui.build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
        await inter.followup.send(
            content=_with_report("↩️ Your last change has been undone.", result),
            embed=embed,
            ephemeral=True,
            **_conflict_kwargs(self, cfg, result),
        )

    async def _post_list(self, inter: discord.Interaction):
        if not self._leader_ok(inter):
            await inter.response.send_message(_DENY_NOT_LEADER, ephemeral=True)
            return
        import config

        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = config.get_buddy_config(self.guild_id)
        result = await asyncio.to_thread(ui.compute_current, self.guild_id, cfg)
        embed = ui.build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
        try:
            await inter.channel.send(embed=embed)
            await inter.followup.send("📣 Posted the buddy list here.", ephemeral=True)
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

        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = config.get_buddy_config(self.guild_id)
        self.session.capture(await asyncio.to_thread(ui.snapshot_pairs, self.guild_id, cfg))
        result = await asyncio.to_thread(
            ui.compute_autofill, self.guild_id, cfg, from_scratch=False
        )
        await asyncio.to_thread(ui.save_result, self.guild_id, cfg, result)
        await ui.refresh_persistent_message(self.bot, self.guild_id, cfg, result)
        embed = ui.build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
        rel_note = " Engineers ordered by reliability." if cfg.get("reliability_enabled") else ""
        roster_note = await asyncio.to_thread(ui.roster_warning, self.guild_id, cfg)
        await inter.followup.send(
            content=_with_report(
                f"✨ Buddies assigned (existing pairs kept).{rel_note}"
                + (f"\n\n{roster_note}" if roster_note else ""),
                result,
            ),
            embed=embed,
            ephemeral=True,
            **_conflict_kwargs(self, cfg, result),
        )

    async def _from_scratch(self, inter: discord.Interaction):
        if not await self._premium_guard(inter, "buddy_auto_assign"):
            return
        import config

        # Compute before confirming so the warning can name who leaves the list.
        # The rebuild reads the pool from Squad Powers alone, so anyone the
        # alliance has taken off it (or opted out) drops here rather than being
        # carried back in by their old Buddies-tab row (#427).
        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = config.get_buddy_config(self.guild_id)
        result, dropped = await asyncio.to_thread(ui.preview_scratch_rebuild, self.guild_id, cfg)

        async def _do(i: discord.Interaction):
            await i.response.defer(ephemeral=True, thinking=True)
            self.session.capture(await asyncio.to_thread(ui.snapshot_pairs, self.guild_id, cfg))
            await asyncio.to_thread(ui.save_result, self.guild_id, cfg, result)
            await ui.refresh_persistent_message(self.bot, self.guild_id, cfg, result)
            embed = ui.build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
            rel_note = (
                " Most reliable Engineers paired with your top War Leaders."
                if cfg.get("reliability_enabled")
                else ""
            )
            drop_note = f" {len(dropped)} removed from the list." if dropped else ""
            roster_note = await asyncio.to_thread(ui.roster_warning, self.guild_id, cfg)
            await i.followup.send(
                content=f"♻️ Rebuilt every pairing from scratch.{rel_note}{drop_note}"
                + (f"\n\n{roster_note}" if roster_note else ""),
                embed=embed,
                ephemeral=True,
                **_conflict_kwargs(self, cfg, result),
            )

        warning = (
            "⚠️ This ignores existing pairings and rebuilds the whole list. "
            "People may get a different buddy."
        )
        if dropped:
            prof_tab = cfg.get("profession_tab") or "Squad Powers"
            # With the roster filter on, "eligible" is two tabs, not one — don't
            # tell leadership to go check a profession that may be perfectly fine.
            if cfg.get("roster_filter_enabled"):
                because = (
                    f"The rebuild only keeps people who are on your member roster **and** have "
                    f"a profession on **{prof_tab}**"
                )
                where = "those two tabs"
            else:
                because = f"The rebuild reads who's eligible from **{prof_tab}**"
                where = "that tab"
            warning += (
                f"\n\n**{len(dropped)} will be removed from the list**. {because}:\n"
                f"{_name_list(dropped)}\n\n"
                f"If that's not what you expected, cancel and check {where} first."
            )
        await inter.followup.send(
            content=f"{warning}\n\nContinue?",
            view=_ConfirmView(inter.user.id, _do),
            ephemeral=True,
        )

    # ── presets (#289 Stage 3) ────────────────────────────────────────────────
    #
    # Saved lineups live on the alliance's own sheet, keyed by name, the same
    # way Storm strategy presets do. Loading one runs it through the ordinary
    # validator, so a preset can restore a lineup but never resurrect someone
    # who has left or contradict the profession survey.

    async def _save_preset(self, inter: discord.Interaction):
        # The one gated preset action. Loading and deleting stay free.
        if not await self._premium_guard(inter, "buddy_presets"):
            return
        await inter.response.send_modal(_PresetNameModal(self))

    async def _commit_preset(self, inter: discord.Interaction, name: str):
        """Modal callback: write the current pairings to the preset tab."""
        import config

        if not name:
            await inter.response.send_message(
                "⚠️ Give the preset a name, something like `Season 4 Opener`.", ephemeral=True
            )
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = config.get_buddy_config(self.guild_id)
        tab = cfg.get("preset_tab")
        existing = await asyncio.to_thread(buddy.list_presets, self.guild_id, tab)
        replacing = any(n.strip().lower() == name.lower() for n in existing)
        result = await asyncio.to_thread(ui.compute_current, self.guild_id, cfg)
        if not result.pairs:
            await inter.followup.send(
                "ℹ️ There are no pairings to save yet. Pair some people first, "
                "then save the lineup as a preset.",
                ephemeral=True,
            )
            return
        ok = await asyncio.to_thread(buddy.save_preset, self.guild_id, tab, name, result)
        if not ok:
            await inter.followup.send(
                f"⚠️ Couldn't write to the **{tab}** tab. Check the bot still has "
                "edit access to your sheet.",
                ephemeral=True,
            )
            return
        count = len(result.pairs)
        verb = "Updated" if replacing else "Saved"
        await inter.followup.send(
            f"💾 {verb} **{name}**, {count} pairing{'s' if count != 1 else ''}.",
            ephemeral=True,
        )

    async def _preset_picker(self, inter: discord.Interaction, prompt: str, on_pick):
        """Shared opener for Load and Delete: list the presets, or say there are
        none. Returns True when a picker was shown."""
        import config

        cfg = config.get_buddy_config(self.guild_id)
        names = await asyncio.to_thread(buddy.list_presets, self.guild_id, cfg.get("preset_tab"))
        if not names:
            await inter.followup.send(
                "ℹ️ No saved presets yet. Use 💾 **Save as preset** to keep the "
                "lineup you have now.",
                ephemeral=True,
            )
            return False
        opts = [discord.SelectOption(label=n[:100], value=n[:100]) for n in names]
        await inter.followup.send(
            prompt,
            view=ui.PickerView(opts, inter.user.id, on_pick, placeholder="Pick a preset…"),
            ephemeral=True,
        )
        return True

    async def _load_preset(self, inter: discord.Interaction):
        # Leadership-only, but deliberately *not* Premium: saving a lineup is
        # the paid part, using or clearing one you already saved is not. A
        # lapse must never strand a preset the alliance made while paying.
        if not self._leader_ok(inter):
            await inter.response.send_message(_DENY_NOT_LEADER, ephemeral=True)
            return
        import config

        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = config.get_buddy_config(self.guild_id)

        async def _pick(i: discord.Interaction, name: str):
            await i.response.defer(ephemeral=True, thinking=True)
            pairs = await asyncio.to_thread(
                buddy.load_preset, self.guild_id, cfg.get("preset_tab"), name
            )
            if not pairs:
                await i.followup.send(f"⚠️ **{name}** has no pairings saved in it.", ephemeral=True)
                return
            self.session.capture(await asyncio.to_thread(ui.snapshot_pairs, self.guild_id, cfg))
            result = await asyncio.to_thread(ui.apply_pairs, self.guild_id, cfg, pairs)
            await ui.refresh_persistent_message(self.bot, self.guild_id, cfg, result)
            embed = ui.build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
            kept = len(result.pairs)
            await i.followup.send(
                content=_with_report(
                    f"📂 Loaded **{name}**, {kept} pairing{'s' if kept != 1 else ''} restored.",
                    result,
                ),
                embed=embed,
                ephemeral=True,
                **_conflict_kwargs(self, cfg, result),
            )

        await self._preset_picker(inter, "Pick a preset to load:", _pick)

    async def _delete_preset(self, inter: discord.Interaction):
        # Leadership-only, but deliberately *not* Premium: saving a lineup is
        # the paid part, using or clearing one you already saved is not. A
        # lapse must never strand a preset the alliance made while paying.
        if not self._leader_ok(inter):
            await inter.response.send_message(_DENY_NOT_LEADER, ephemeral=True)
            return
        import config

        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = config.get_buddy_config(self.guild_id)

        async def _pick(i: discord.Interaction, name: str):
            async def _do(i2: discord.Interaction):
                await i2.response.defer(ephemeral=True, thinking=True)
                ok = await asyncio.to_thread(
                    buddy.delete_preset, self.guild_id, cfg.get("preset_tab"), name
                )
                await i2.followup.send(
                    f"🗑️ Deleted the preset **{name}**."
                    if ok
                    else f"⚠️ Couldn't find a preset named **{name}**.",
                    ephemeral=True,
                )

            await i.response.send_message(
                f"🗑️ Delete the preset **{name}**? Your current buddy list stays exactly "
                "as it is, this only removes the saved preset. This cannot be undone.",
                view=_ConfirmView(i.user.id, _do, confirm_label="🗑️ Yes, delete"),
                ephemeral=True,
            )

        await self._preset_picker(inter, "Pick a preset to delete:", _pick)

    async def _post_buttons(self, inter: discord.Interaction):
        if not await self._premium_guard(inter, "buddy_self_service"):
            return
        await inter.response.defer(ephemeral=True, thinking=True)
        msg = await ui.post_self_service_message(self.bot, inter.channel, self.guild_id)
        if msg is not None:
            await inter.followup.send(
                "📣 Posted the self-service profession message here. Members can set "
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
