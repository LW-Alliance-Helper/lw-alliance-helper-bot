"""
Guided first-run walkthrough on the storm event hub (#130, rewritten
for #187 + #190).

Surfaces a `[👋 Walk me through this]` offer the first time an officer
opens `/desertstorm` or `/canyonstorm`. Clicking it runs a narrated
5-step tour that walks through the hub layout, the weekly event cycle,
the strategy presets + member rules surfaces, free vs Premium gating,
and a wrap-up pointer at `/help`. Clicking dismiss records the choice
so the offer doesn't re-appear for that officer.

The walkthrough key encodes a version (`storm_hub_v1`). The earlier
`storm_signups_v1` tour was tied to the pre-#187 `/<event> signups`
officer view; bumping the key to `storm_hub_v1` re-offers the tour
to officers who already dismissed v1 so they see the new hub-flow
explanation once.

Per-officer dismissals share the key across DS + CS: dismissing on
one event type silences the other for that officer.
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)


# Tour content version. Bump when the hub's UX changes enough that
# existing officers should see the updated tour. The old
# `storm_signups_v1` key (officer-view tour) is dead post-#187, but
# leaving it in `config.is_walkthrough_dismissed` won't hurt; new
# dismissals key against this new value.
STORM_HUB_TOUR_KEY = "storm_hub_v1"


def _build_storm_hub_tour_steps(
    event_type: str = "DS",
) -> list[str]:
    """Build the 5-step hub-centric tour copy. Branches on event_type
    so DS officers see DS-flavoured copy (and CS officers see CS).

    The five steps walk the weekly cycle:
        1. The hub itself (config summary + button grid).
        2. The weekly event cycle (post poll, view sign-ups, set up
           teams, record attendance).
        3. Strategy presets + member rules (the storage surfaces that
           feed the roster builder).
        4. Free vs Premium gating (what each `💎` button unlocks).
        5. Wrap-up (`/<event>` re-opens the hub, `/help` has the
           command list).
    """
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    event_day = "Friday" if event_type == "DS" else "Thursday"
    parent = "desertstorm" if event_type == "DS" else "canyonstorm"

    return [
        # ── Step 1 / 5: The hub ──────────────────────────────────────────
        f"**Step 1 / 5 — The {label} hub**\n"
        f"This embed is your home base for {label}. The top of the embed "
        f"shows your alliance's current setup: the next event date, the "
        f"sign-up post channel (and auto-schedule if you set one up), "
        f"team configuration (A & B / single team), how many strategy "
        f"presets you've saved, and whether the structured roster flow "
        f"is enabled.\n"
        f"\n"
        f"The buttons below cover every action in three groups: "
        f"event-day actions (top row), communications + configuration "
        f"(middle row), and reference + setup (bottom row).",

        # ── Step 2 / 5: The weekly cycle ─────────────────────────────────
        f"**Step 2 / 5 — The weekly cycle**\n"
        f"{label} runs every **{event_day}** in-game. Here's the flow:\n"
        f"\n"
        f"1. **📣 Post sign-up poll** drops a vote message in your sign-up "
        f"channel. Members click a button to register their availability.\n"
        f"2. After votes come in, click **👁️ View sign-ups + set up "
        f"teams**. You'll see who voted in each bucket and can click "
        f"🅰️ Set up Team A or 🅱️ Set up Team B to open the roster "
        f"builder, pre-filtered to members who signed up for that team.\n"
        f"3. After the event, **📋 Record attendance** lets you mark who "
        f"actually showed at each assigned slot.\n"
        f"\n"
        f"Auto-scheduling can fire step 1 for you on a configured day; "
        f"otherwise the button works on-demand.",

        # ── Step 3 / 5: Strategy presets + member rules ──────────────────
        f"**Step 3 / 5 — Strategy presets + member rules**\n"
        f"Two storage surfaces feed the roster builder:\n"
        f"\n"
        f"**🧮 Manage strategy presets** opens your saved zone layouts. "
        f"A preset lists which zones the team uses, max players per "
        f"zone, optional minimum power per zone, and priority. When you "
        f"set up a team, you pick which preset to apply.\n"
        f"\n"
        f"**👤 Manage member rules** opens the eligibility rule list. "
        f"Two types: power-band rules ('members ≥ 80M are eligible for "
        f"Power Tower') and per-member overrides ('Alice always plays "
        f"Team A'). Both feed into the auto-fill when you build a "
        f"roster.",

        # ── Step 4 / 5: Free vs Premium ──────────────────────────────────
        f"**Step 4 / 5 — Free vs Premium**\n"
        f"The hub buttons with `💎` render disabled on the free tier. "
        f"Here's what unlocks with `/upgrade`:\n"
        f"\n"
        f"**Premium:** Post sign-up poll, View sign-ups + set up teams, "
        f"Record attendance, Send DM reminder to roster, View past "
        f"rosters. These power the structured roster flow.\n"
        f"\n"
        f"**Free tier:** Manage strategy presets, Manage member rules, "
        f"Generate mail (the legacy text template), Fill out "
        f"participation questions, View past participation logs. "
        f"Strategy presets + member rules still let free-tier alliances "
        f"use the roster builder against their full roster.",

        # ── Step 5 / 5: Wrap-up ──────────────────────────────────────────
        f"**Step 5 / 5 — That's the tour**\n"
        f"Run `/{parent}` any time to come back to this hub. The hub "
        f"re-reads your config on every open, so any setup changes "
        f"show up the next time you open it.\n"
        f"\n"
        f"For the full command list, run `/help` and pick **{label}** "
        f"from the category dropdown. Closing this message drops you "
        f"back to the live hub.",
    ]


# Backwards-compat alias kept for any external/test callers that
# imported the constant before the hub rewrite.
_STORM_HUB_TOUR_STEPS: list[str] = _build_storm_hub_tour_steps("DS")


async def maybe_offer_storm_hub_tour(
    interaction: discord.Interaction,
    *,
    walkthrough_key: str = STORM_HUB_TOUR_KEY,
    event_type: str = "DS",
) -> None:
    """Fire the first-run hub tour offer if the officer hasn't already
    dismissed it. Called from `storm_event_hub.handle_event_hub` after
    the hub embed lands so the offer arrives as a followup ephemeral
    immediately below the hub.

    No-op if the walkthrough was already dismissed (either via
    `[Walk me through this]` or `[No thanks]`). Safe to call once per
    hub invocation; defends against double-click races inside the
    `_OfferView` itself.

    `event_type` shapes the tour copy (event-type label in Step 1,
    game-defined event day in Step 2, `/<parent>` reference in Step 5).
    """
    import config
    guild_id = interaction.guild_id
    user_id  = interaction.user.id
    if not guild_id:
        return
    if config.is_walkthrough_dismissed(guild_id, user_id, walkthrough_key):
        return

    view = _OfferView(
        guild_id=guild_id, user_id=user_id,
        walkthrough_key=walkthrough_key,
        event_type=event_type,
    )
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    try:
        msg = await interaction.followup.send(
            f"👋 First time opening {label}? Want a quick walkthrough "
            f"of how this hub works?",
            view=view,
            ephemeral=True,
            wait=True,
        )
        # Capture the message so `on_timeout` can strip the view rather
        # than leaving stale buttons that 404 on click.
        view.message = msg
    except discord.HTTPException as e:
        logger.warning(
            "[STORM WALKTHROUGH] failed to send hub-tour offer "
            "(guild=%s user=%s): %s",
            guild_id, user_id, e,
        )


class _OfferView(discord.ui.View):
    """First-run offer: [Walk me through this] / [No thanks]. Both
    record the dismissal. The bot only ever offers once per officer +
    walkthrough_key tuple."""

    def __init__(
        self, *, guild_id: int, user_id: int, walkthrough_key: str,
        event_type: str = "DS",
    ):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id  = user_id
        self.walkthrough_key = walkthrough_key
        self.event_type = event_type
        self.message: discord.Message | discord.WebhookMessage | None = None

    @discord.ui.button(label="👋 Walk me through this", style=discord.ButtonStyle.success)
    async def accept(self, inter: discord.Interaction, btn: discord.ui.Button):
        if inter.user.id != self.user_id:
            await inter.response.send_message(
                "⛔ This walkthrough was offered to someone else.",
                ephemeral=True,
            )
            return
        # Stop the view BEFORE any awaits so a fast second click can't
        # race through and spawn a duplicate tour message.
        if self.is_finished():
            return
        self.stop()

        import config
        config.dismiss_walkthrough(self.guild_id, self.user_id, self.walkthrough_key)
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(
                content="✅ Starting the tour…", view=self,
            )
        except discord.HTTPException:
            pass
        steps = _build_storm_hub_tour_steps(self.event_type)
        await _send_tour_step(inter, steps, index=0)

    @discord.ui.button(label="No thanks", style=discord.ButtonStyle.secondary)
    async def decline(self, inter: discord.Interaction, btn: discord.ui.Button):
        if inter.user.id != self.user_id:
            await inter.response.send_message(
                "⛔ This walkthrough was offered to someone else.",
                ephemeral=True,
            )
            return
        if self.is_finished():
            return
        self.stop()

        import config
        config.dismiss_walkthrough(self.guild_id, self.user_id, self.walkthrough_key)
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(
                content="👍 Got it, won't ask again. Run `/help` any "
                        "time and pick Desert Storm or Canyon Storm "
                        "for a refresher.",
                view=self,
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        # Timing out without clicking is treated as "deferred" so the
        # offer fires again next time. Strip the view from the message
        # so the buttons don't 404 on a stale click.
        for item in self.children:
            item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


async def _send_tour_step(
    interaction: discord.Interaction,
    steps: list[str],
    *,
    index: int,
) -> None:
    """Send one tour step as an ephemeral followup. The view itself
    carries the index for the next step, so progression doesn't have
    to thread state through a shared dict."""
    if not steps or index >= len(steps):
        return
    is_last = (index + 1) >= len(steps)
    view = _TourStepView(
        steps=steps,
        index=index,
        owner_id=interaction.user.id,
        is_last=is_last,
    )
    try:
        msg = await interaction.followup.send(
            content=steps[index], view=view, ephemeral=True, wait=True,
        )
        view.message = msg
    except discord.HTTPException as e:
        logger.warning(
            "[STORM WALKTHROUGH] failed to send tour step %s: %s", index, e,
        )


class _TourStepView(discord.ui.View):
    """One step of the tour. Owns the index so Next/Skip can advance
    correctly. Earlier implementation re-entered a start function on
    every Next click, which threw away the increment and looped on
    step 1 forever."""

    def __init__(self, *, steps: list[str], index: int, owner_id: int, is_last: bool):
        super().__init__(timeout=600)
        self._steps = steps
        self._index = index
        self._owner_id = owner_id
        self.message: discord.Message | discord.WebhookMessage | None = None

        if not is_last:
            next_btn = discord.ui.Button(label="Next →", style=discord.ButtonStyle.primary)
            next_btn.callback = self._make_next()
            self.add_item(next_btn)

            skip_btn = discord.ui.Button(label="Skip the rest", style=discord.ButtonStyle.secondary)
            skip_btn.callback = self._make_skip()
            self.add_item(skip_btn)
        else:
            close_btn = discord.ui.Button(label="Close", style=discord.ButtonStyle.success)
            close_btn.callback = self._make_close()
            self.add_item(close_btn)

    def _make_next(self):
        async def _next(inter: discord.Interaction):
            if inter.user.id != self._owner_id:
                await inter.response.send_message(
                    "⛔ This walkthrough was offered to someone else.",
                    ephemeral=True,
                )
                return
            if self.is_finished():
                return
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                await inter.response.edit_message(view=self)
            except discord.HTTPException:
                pass
            await _send_tour_step(inter, self._steps, index=self._index + 1)
        return _next

    def _make_skip(self):
        async def _skip(inter: discord.Interaction):
            if inter.user.id != self._owner_id:
                await inter.response.send_message(
                    "⛔ This walkthrough was offered to someone else.",
                    ephemeral=True,
                )
                return
            if self.is_finished():
                return
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                # `inter.message.content` is normally a string, but defend
                # against the rare None case (e.g. ephemerals where content
                # got dropped) so the concat doesn't raise TypeError that
                # escapes the surrounding `try/except discord.HTTPException`.
                prior = inter.message.content if inter.message else ""
                await inter.response.edit_message(
                    content=(prior or "") + "\n\n_(tour skipped)_",
                    view=self,
                )
            except discord.HTTPException:
                pass
        return _skip

    def _make_close(self):
        async def _close(inter: discord.Interaction):
            if inter.user.id != self._owner_id:
                await inter.response.send_message(
                    "⛔ This walkthrough was offered to someone else.",
                    ephemeral=True,
                )
                return
            if self.is_finished():
                return
            self.stop()
            for item in self.children:
                item.disabled = True
            try:
                await inter.response.edit_message(view=self)
            except discord.HTTPException:
                pass
        return _close

    async def on_timeout(self):
        """Strip the view so stale buttons don't 404 with "Interaction
        failed" after the 10-minute timeout. Per CLAUDE.md's
        auto-post-view contract."""
        for item in self.children:
            item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass
