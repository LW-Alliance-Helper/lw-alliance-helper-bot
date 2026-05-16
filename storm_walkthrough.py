"""
Guided first-run walkthrough on storm entry points (#130).

Surfaces a `[👋 Walk me through this]` offer the first time an officer
opens the storm sign-ups view in a guild. Clicking it runs a narrated micro-tour
that explains each component of the officer view; clicking dismiss
records the choice so the offer never re-appears for that officer.

The walkthrough key encodes a version (`storm_signups_v1`) so a major
UI rewrite can re-offer the tour without losing per-officer dismissal
records — just bump the key.

Per Decision #12 / Rule N (#170), the tour fires for both
`/desertstorm signups` and `/canyonstorm signups`. Per-officer
dismissals share the same `storm_signups_v1` key — dismissing on DS
silences CS for that officer too. Step copy branches on `event_type`
+ the alliance's `teams` config so the on-behalf vote section
(Step 4) and Set-Up section (Step 5) describe the actual UI the
officer will see, not a DS-default that misleads CS officers.
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)


# Tour content — each step is one short ephemeral message with [Next →]
# and [Skip the rest] buttons. Six steps tracks the design spec.
STORM_SIGNUPS_TOUR_KEY = "storm_signups_v1"


def _build_storm_signups_tour_steps(
    event_type: str = "DS", teams: str = "both",
) -> list[str]:
    """Build the per-event-type, per-team-config tour copy.

    DS and CS share the bucket / strikethrough / not-on-Discord
    structure (steps 1-3) so those are identical. Steps 4-6 branch:
      * Step 4 wording references the post-#168 ephemeral view with
        Member + Vote selects (no more free-text modal).
      * Step 5 lists only the Set-Up button(s) the officer will
        actually see — both A + B when `teams=both`, just the single
        team's button when `teams=A` or `teams=B`. Event-type label
        + game-defined times naturally flow from the click target.
      * Step 6 points at the right /help category for the officer's
        event type (Desert Storm vs Canyon Storm) instead of a
        both-category mention.
    """
    teams_norm = (teams or "both").strip()
    if teams_norm not in ("both", "A", "B"):
        teams_norm = "both"

    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    help_category = label  # `/help` category names match this exactly.

    # Step 5 — branch on teams config so the copy matches the buttons.
    if teams_norm == "both":
        setup_phrase = (
            "click **🅰️ Set up Team A** or **🅱️ Set up Team B**"
        )
    elif teams_norm == "A":
        setup_phrase = "click **🅰️ Set up Team A**"
    else:
        setup_phrase = "click **🅱️ Set up Team B**"

    return [
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
        "Click **🙋 Record on-behalf vote** to open the picker. Pick the "
        "member from the dropdown (sourced from your roster Sheet — no "
        "free typing, so typos can't slip through), pick a vote (the "
        "options match the team buttons members see on the sign-up post), "
        "then hit **Submit**. Each on-behalf vote captures your Discord "
        "ID for audit.",

        "**Step 5 / 6 — Setting up a team**\n"
        f"When you're ready to build a {label} roster, {setup_phrase}. "
        "The bot will ask which preset to use, then open the roster builder "
        "pre-filtered to members who signed up, with eligibility minimums "
        "enforced.",

        "**Step 6 / 6 — That's the tour**\n"
        f"You can run `/help` any time and pick **{help_category}** "
        "from the dropdown to revisit the command list. Closing this "
        "message drops you back to the live officer view.",
    ]


# Backwards-compat alias: tests and any external callers that imported
# the constant continue to work against the DS+both default. New
# in-process callers go through `_build_storm_signups_tour_steps` via
# `maybe_offer_storm_signups_tour`.
_STORM_SIGNUPS_TOUR_STEPS: list[str] = _build_storm_signups_tour_steps("DS", "both")


async def maybe_offer_storm_signups_tour(
    interaction: discord.Interaction,
    *,
    walkthrough_key: str = STORM_SIGNUPS_TOUR_KEY,
    event_type: str = "DS",
    teams: str = "both",
) -> None:
    """If the officer hasn't seen the walkthrough yet, send an ephemeral
    offer message. Records the dismissal on either choice — accept (the
    tour fires) or decline (next time the officer runs the command, the
    offer doesn't reappear).

    No-op if the walkthrough was already dismissed. Safe to call once
    per storm sign-ups view invocation. `event_type` + `teams` shape
    the tour copy (Step 4 on-behalf flow, Step 5 Set-Up button list,
    Step 6 `/help` category pointer) so a CS officer sees CS-flavored
    steps and a single-team officer sees their single button.
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
        event_type=event_type, teams=teams,
    )
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    try:
        msg = await interaction.followup.send(
            f"👋 First time opening the {label} sign-ups view? Want a "
            "quick walkthrough of what each piece does?",
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

    def __init__(
        self, *, guild_id: int, user_id: int, walkthrough_key: str,
        event_type: str = "DS", teams: str = "both",
    ):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id  = user_id
        self.walkthrough_key = walkthrough_key
        self.event_type = event_type
        self.teams = teams
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
        steps = _build_storm_signups_tour_steps(self.event_type, self.teams)
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
                content="👍 Got it — won't ask again. Run `/help` any "
                        "time and pick Desert Storm or Canyon Storm "
                        "if you want a refresher.",
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
