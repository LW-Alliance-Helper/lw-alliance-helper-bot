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
from typing import Optional

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
    "or **🅱️ Set up Team B**. The bot will ask which preset to use, "
    "then open the roster builder — pre-filtered to members who "
    "signed up for that team, with eligibility floors enforced.",

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
        await interaction.followup.send(
            "👋 First time using `/storm_signups`? Want a quick walkthrough "
            "of what each piece does?",
            view=view,
            ephemeral=True,
        )
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

    @discord.ui.button(label="👋 Walk me through this", style=discord.ButtonStyle.success)
    async def accept(self, inter: discord.Interaction, btn: discord.ui.Button):
        if inter.user.id != self.user_id:
            await inter.response.send_message(
                "⛔ This walkthrough was offered to someone else.",
                ephemeral=True,
            )
            return
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
        await _start_tour(inter, _STORM_SIGNUPS_TOUR_STEPS)
        self.stop()

    @discord.ui.button(label="No thanks", style=discord.ButtonStyle.secondary)
    async def decline(self, inter: discord.Interaction, btn: discord.ui.Button):
        if inter.user.id != self.user_id:
            await inter.response.send_message(
                "⛔ This walkthrough was offered to someone else.",
                ephemeral=True,
            )
            return
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
        self.stop()

    async def on_timeout(self):
        # Timing out without clicking is treated as "deferred" — don't
        # record dismissal so the offer fires again next time. The view
        # itself just disables.
        for item in self.children:
            item.disabled = True


async def _start_tour(interaction: discord.Interaction, steps: list[str]) -> None:
    """Run the multi-step tour as a chain of ephemeral messages with
    Next / Skip buttons. Each step's view replaces the prior."""
    if not steps:
        return
    state = {"index": 0}

    async def _send_step(inter: discord.Interaction) -> None:
        idx = state["index"]
        content = steps[idx]
        if idx + 1 < len(steps):
            view = _TourStepView(
                state=state, steps=steps, owner_id=interaction.user.id,
                is_last=False,
            )
            await inter.followup.send(content=content, view=view, ephemeral=True)
        else:
            # Final step — no "Next", just a "Close" button.
            view = _TourStepView(
                state=state, steps=steps, owner_id=interaction.user.id,
                is_last=True,
            )
            await inter.followup.send(content=content, view=view, ephemeral=True)

    await _send_step(interaction)


class _TourStepView(discord.ui.View):
    def __init__(self, *, state: dict, steps: list[str], owner_id: int, is_last: bool):
        super().__init__(timeout=600)
        self._state = state
        self._steps = steps
        self._owner_id = owner_id

        if not is_last:
            next_btn = discord.ui.Button(label="Next →", style=discord.ButtonStyle.primary)

            async def _next(inter: discord.Interaction):
                if inter.user.id != self._owner_id:
                    await inter.response.send_message(
                        "⛔ This walkthrough was offered to someone else.",
                        ephemeral=True,
                    )
                    return
                self._state["index"] += 1
                for item in self.children:
                    item.disabled = True
                try:
                    await inter.response.edit_message(view=self)
                except discord.HTTPException:
                    pass
                await _start_tour(inter, self._steps)
                self.stop()

            next_btn.callback = _next
            self.add_item(next_btn)

            skip_btn = discord.ui.Button(label="Skip the rest", style=discord.ButtonStyle.secondary)

            async def _skip(inter: discord.Interaction):
                if inter.user.id != self._owner_id:
                    await inter.response.send_message(
                        "⛔ This walkthrough was offered to someone else.",
                        ephemeral=True,
                    )
                    return
                for item in self.children:
                    item.disabled = True
                try:
                    await inter.response.edit_message(
                        content=inter.message.content + "\n\n_(tour skipped)_",
                        view=self,
                    )
                except discord.HTTPException:
                    pass
                self.stop()

            skip_btn.callback = _skip
            self.add_item(skip_btn)
        else:
            close_btn = discord.ui.Button(label="Close", style=discord.ButtonStyle.success)

            async def _close(inter: discord.Interaction):
                if inter.user.id != self._owner_id:
                    await inter.response.send_message(
                        "⛔ This walkthrough was offered to someone else.",
                        ephemeral=True,
                    )
                    return
                for item in self.children:
                    item.disabled = True
                try:
                    await inter.response.edit_message(view=self)
                except discord.HTTPException:
                    pass
                self.stop()

            close_btn.callback = _close
            self.add_item(close_btn)
