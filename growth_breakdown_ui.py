"""The on-demand Growth Breakdown screen (#668).

`/growth breakdown` and the **📊 See most recent Breakdown** button on
`/growth overview` both open it; they used to carry two copies of the same
read-and-render block. It shows the latest month with the alliance's bucket
filter (💎 Premium) or the default, every bucket but No Change listed by
name, and one 👀 toggle to list every bucket instead. The toggle only appears
when the filtered view actually shows a bucket as a count, so it can always
change something.

The Premium auto-post renders the same embed (`growth.format_breakdown_embed`)
without the toggle.
"""

from __future__ import annotations

import asyncio
import logging

import discord

import growth
import wizard_registry

logger = logging.getLogger(__name__)

#: How long the toggle stays live. The base view keeps an ephemeral message's
#: timeout inside its edit window, so the expired notice always lands.
BREAKDOWN_VIEW_TIMEOUT = 600

#: Where to go when the toggle has expired.
BREAKDOWN_ROUTE = "`/growth breakdown`"

LOAD_FAILED = (
    "⚠️ I couldn't read the breakdown from your Sheet just now. "
    "Try again in a minute. If it keeps failing, check that the bot still has access to your Sheet."
)


class BreakdownView(wizard_registry.OwnedView):
    """One toggle between the filtered view and every bucket listed."""

    timeout_hint = BREAKDOWN_ROUTE

    def __init__(self, owner_id: int, render, *, has_filter: bool):
        super().__init__(timeout=BREAKDOWN_VIEW_TIMEOUT)
        self.owner_id = owner_id
        self._render = render
        self._back_label = growth.BTN_SHOW_FILTER if has_filter else growth.BTN_SHOW_DEFAULT
        self.show_all = False
        self.toggle = self.add_button(
            growth.BTN_SHOW_ALL, discord.ButtonStyle.secondary, self._flip
        )

    async def _flip(self, interaction: discord.Interaction) -> None:
        self.show_all = not self.show_all
        self.toggle.label = self._back_label if self.show_all else growth.BTN_SHOW_ALL
        await wizard_registry.safe_edit_response(
            interaction, embed=self._render(self.show_all), view=self
        )


async def send_breakdown(
    interaction: discord.Interaction, guild_id: int, gcfg: dict, *, no_data: str
) -> None:
    """Send the breakdown as an ephemeral follow-up. The caller has deferred.

    `no_data` is the caller's own wording for a guild with no breakdown yet,
    since the overview button and the slash command point at different places
    to run a snapshot."""
    try:
        data = await asyncio.to_thread(growth.read_latest_breakdown, guild_id)
    except Exception:
        logger.exception("[GROWTH] breakdown read failed guild=%s", guild_id)
        await interaction.followup.send(LOAD_FAILED, ephemeral=True)
        return
    if not data.get("has_data"):
        await interaction.followup.send(no_data, ephemeral=True)
        return

    import premium

    # The filter is a 💎 Premium setting. A lapsed subscription falls back to
    # the default rather than keeping a view it no longer pays for, the same
    # way the auto-post stops.
    bucket_filter = []
    if await premium.is_premium(guild_id, interaction=interaction):
        bucket_filter = list(gcfg.get("breakdown_bucket_filter") or [])
    unchanged = data.get("unchanged") or []

    def render(show_all: bool) -> discord.Embed:
        return growth.format_breakdown_embed(
            metric_labels=data["metric_labels"],
            breakdown_summary=data["summary"],
            prev_period_label=data["prev_period_label"],
            curr_period_label=data["curr_period_label"],
            label_overrides=gcfg.get("breakdown_labels") or {},
            bucket_filter=bucket_filter,
            unchanged=unchanged,
            show_all=show_all,
            tab_name=gcfg.get("tab_breakdown") or "Growth Breakdown",
            with_toggle=True,
        )

    if not growth.breakdown_hides_buckets(
        data["summary"], data["metric_labels"], bucket_filter, unchanged
    ):
        await interaction.followup.send(embed=render(True), ephemeral=True)
        return

    view = BreakdownView(interaction.user.id, render, has_filter=bool(bucket_filter))
    view.message = await interaction.followup.send(
        embed=render(False), view=view, ephemeral=True, wait=True
    )
