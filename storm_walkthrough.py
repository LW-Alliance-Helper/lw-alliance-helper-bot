"""
Guided first-run walkthrough on storm entry points (#130).

Surfaces a `[👋 Walk me through this]` offer the first time an officer
hits `/storm_signups` in a guild. Clicking it runs a narrated micro-tour
that explains each component of the officer view; clicking dismiss
records the choice so the offer never re-appears for that officer.

The walkthrough key encodes a version (`storm_signups_v1`) so a major
UI rewrite can re-offer the tour without losing per-officer dismissal
records — just bump the key.
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)


# Tour content — each step is one short ephemeral message with [Next →]
# and [Skip the rest] buttons. Six steps tracks the design spec.
STORM_SIGNUPS_TOUR_KEY = "storm_signups_v1"

_STORM_SIGNUPS_TOUR_STEPS: list[str] = [
    "**Step 1 / 6 — The buckets**\n"
    "The embed groups everyone by their current vote: 🅰️ Team A, "
    "🅱️ Team B, 🔄 Either, ❌ Cannot, and ❓ Not voted yet. The "
    "counter in the title tells you the total members you're tracking.",

    "**Step 2 / 6 — Who's already assigned**\n"
    "Members already slotted into a roster for this event render with "
    "strikethrough. That way you can scan at a glance for who's left "
    "to place when you're building out a team.",

    "**Step 3 / 6 — Members who aren't on Discord**\n"
    "If your roster Sheet flags a row with `not_on_discord`, that "
    "member surfaces in the buckets just like everyone else (marked "
    "with ¹). They won't vote themselves — you cast their vote with "
    "**🙋 Record on-behalf vote**, and the bot logs that you recorded it.",

    "**Step 4 / 6 — Recording on-behalf votes**\n"
    "Open the modal, type the member's roster name (it must match the "
    "Sheet exactly — typos are rejected), and pick A / B / Either / "
    "Cannot. Each on-behalf vote captures your Discord ID for audit.",

    "**Step 5 / 6 — Setting up a team**\n"
    "When you're ready to build a roster, click **🅰️ Set up Team A** "
    "or **🅱️ Set up Team B** (Desert Storm) — or **🏜️ Set up Roster** "
    "(Canyon Storm; one roster per faction). The bot will ask which "
    "preset to use, then open the roster builder pre-filtered to "
    "members who signed up, with eligibility floors enforced.",

    "**Step 6 / 6 — That's the tour**\n"
    "You can run `/help storm` any time to revisit it (coming soon). "
    "Closing this message drops you back to the live officer view.",
]


async def maybe_offer_storm_signups_tour(
    interaction: discord.Interaction,
    *,
    walkthrough_key: str = STORM_SIGNUPS_TOUR_KEY,
) -> None:
    """If the officer hasn't seen the walkthrough yet, send an ephemeral
    offer message. Records the dismissal on either choice — accept (the
    tour fires) or decline (next time the officer runs the command, the
    offer doesn't reappear).

    No-op if the walkthrough was already dismissed. Safe to call once
    per `/storm_signups` invocation.
    """
    import config
    guild_id = interaction.guild_id
    user_id  = interaction.user.id
    if not guild_id:
        return
    if config.is_walkthrough_dismissed(guild_id, user_id, walkthrough_key):
        return

    view = _OfferView(guild_id=guild_id, user_id=user_id,
                      walkthrough_key=walkthrough_key)
    try:
        msg = await interaction.followup.send(
            "👋 First time using `/storm_signups`? Want a quick walkthrough "
            "of what each piece does?",
            view=view,
            ephemeral=True,
            wait=True,
        )
        # Capture the message so `on_timeout` can strip the view rather
        # than leaving stale buttons that 404 on click.
        view.message = msg
    except discord.HTTPException as e:
        logger.warning(
            "[STORM WALKTHROUGH] failed to send offer (guild=%s user=%s): %s",
            guild_id, user_id, e,
        )


class _OfferView(discord.ui.View):
    """First-run offer: [Walk me through this] / [No thanks]. Both
    record the dismissal — the bot only ever offers once."""

    def __init__(self, *, guild_id: int, user_id: int, walkthrough_key: str):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id  = user_id
        self.walkthrough_key = walkthrough_key
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
        await _send_tour_step(inter, _STORM_SIGNUPS_TOUR_STEPS, index=0)

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
                content="👍 Got it — won't ask again. Run `/help storm` any "
                        "time if you want a refresher.",
                view=self,
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self):
        # Timing out without clicking is treated as "deferred" — don't
        # record dismissal so the offer fires again next time. Strip the
        # view from the message so the buttons don't 404 on a stale click.
        for item in self.children:
            item.disabled = True
        if self.message is None:
            return
        try:
            await self.message.edit(view=self)
        except discord.HTTPException:
            pass


async def _start_tour(interaction: discord.Interaction, steps: list[str]) -> None:
    """Public entry — sends step 0. Kept for backwards compatibility
    with any external caller; the new internal path uses
    `_send_tour_step` directly."""
    await _send_tour_step(interaction, steps, index=0)


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
    correctly — the prior implementation re-entered `_start_tour` on
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
