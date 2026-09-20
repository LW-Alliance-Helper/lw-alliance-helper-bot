"""Enter a past week's results one match at a time, from dropdowns.

The text box (`alliance_duel_entry.OtherResultsModal`) needs the tag typed
exactly and the line laid out just so. This screen removes both: pick an
alliance, pick its score from 0 to 13, pick the other alliance and its score,
add the match to the list, and save the whole week at once.

Discord gives each dropdown its own row, so four dropdowns plus one row of
buttons fills the message. Removing a match therefore swaps the screen into a
second state (one dropdown of the added matches) instead of adding a fifth
dropdown to the first.

Nothing reaches the sheet until Save. The list is held in memory, so it is lost
if the screen times out; the base view says how to get back.
"""

from __future__ import annotations

import discord

import alliance_duel as ad
import alliance_duel_entry as ad_entry
from wizard_registry import OwnedView

#: Multi-step work, per the DESIGN.md timeout tiers: a week is eight matches.
BUILDER_TIMEOUT = 900

VS_BUILDER_ADDED = "Added so far · {n} of {total} matches"
VS_BUILDER_EMPTY = "Nothing added yet."
VS_BUILDER_FIRST = "First alliance"
VS_BUILDER_SECOND = "Second alliance"
#: Before an alliance is picked, its score box cannot name it yet.
VS_BUILDER_FIRST_SCORE = "First alliance's score"
VS_BUILDER_SECOND_SCORE = "Second alliance's score"
VS_BUILDER_INCOMPLETE = "Pick both alliances and both scores first."
VS_BUILDER_REMOVE_PLACEHOLDER = "Pick a match to remove"

VS_BTN_BUILDER_ADD = "➕ Add to results list"
VS_BTN_BUILDER_SAVE = "✅ Save week"
VS_BTN_BUILDER_REMOVE = "🗑️ Remove match"
VS_BTN_BUILDER_REMOVE_SELECTED = "🗑️ Remove selected match"
VS_BTN_BUILDER_TEXT_BOX = "✏️ Use the text box instead"
#: The same words the predictions screen uses for the same step back.
VS_BTN_BUILDER_BACK = ad_entry.VS_BTN_CANCEL_PREDICTIONS


class ResultsBuilderView(OwnedView):
    """The builder screen. Renders from its own state and never re-reads the sheet."""

    timeout_hint = "`/vs`"

    def __init__(self, state, week: int, owner_id: int):
        super().__init__(timeout=BUILDER_TIMEOUT)
        self.state = state
        self.week = week
        self.owner_id = owner_id
        self.message: discord.Message | None = None

        self.roster = sorted(
            {r.alliance for r in state.league_rows(week)},
            key=lambda alliance: state.display_name(alliance).casefold(),
        )
        self.total = len(self.roster) // 2
        #: (first, first score, second, second score), in the order entered.
        self.matches: list[tuple[ad.AllianceKey, int, ad.AllianceKey, int]] = []
        self._clear_picks()
        self.removing = False
        self.doomed: int | None = None
        self._build()

    # ── State ────────────────────────────────────────────────────────────────

    def _clear_picks(self) -> None:
        self.first: ad.AllianceKey | None = None
        self.first_score: int | None = None
        self.second: ad.AllianceKey | None = None
        self.second_score: int | None = None

    def _name(self, alliance: ad.AllianceKey) -> str:
        return self.state.display_name(alliance)

    def _free(self, exclude: ad.AllianceKey | None = None) -> list[ad.AllianceKey]:
        """Alliances with no match in the list yet, minus the other side's pick."""
        used = {alliance for match in self.matches for alliance in (match[0], match[2])}
        return [a for a in self.roster if a not in used and a != exclude]

    def match_line(self, match: tuple) -> str:
        """Plain on purpose: the numbers show who won, so nothing is highlighted."""
        first, x, second, y = match
        return f"{self._name(first)} {x} - {y} {self._name(second)}"

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=ad_entry.VS_RESULTS_MODAL_TITLE.format(week=self.week),
            color=discord.Color.blurple(),
        )
        lines = [self.match_line(m) for m in self.matches] or [VS_BUILDER_EMPTY]
        embed.add_field(
            name=VS_BUILDER_ADDED.format(n=len(self.matches), total=self.total),
            value="\n".join(lines),
            inline=False,
        )
        return embed

    # ── Controls ─────────────────────────────────────────────────────────────

    def _alliance_select(self, placeholder, chosen, other, attr, row) -> discord.ui.Select:
        options = [
            discord.SelectOption(
                label=self._name(a)[:100], value=str(self.roster.index(a)), default=a == chosen
            )
            for a in self._free(exclude=other)
        ]
        select = discord.ui.Select(
            placeholder=placeholder, options=options, min_values=1, max_values=1, row=row
        )
        select.callback = self._setter(attr, alliance=True)
        return select

    def _score_select(self, owner, chosen, placeholder, attr, row) -> discord.ui.Select:
        # Once the alliance is known, each score option carries its tag, so the
        # collapsed dropdown reads "ABC 9" and the number cannot pass for the
        # other side's.
        prefix = f"{self._name(owner)} " if owner is not None else ""
        options = [
            discord.SelectOption(label=f"{prefix}{n}", value=str(n), default=n == chosen)
            for n in range(ad.WEEK_POINTS_TOTAL + 1)
        ]
        if owner is not None:
            placeholder = ad_entry.VS_SCORE_LABEL.format(tag=self._name(owner))
        select = discord.ui.Select(
            placeholder=placeholder, options=options, min_values=1, max_values=1, row=row
        )
        select.callback = self._setter(attr, alliance=False)
        return select

    def _build(self) -> None:
        self.clear_items()
        if self.removing:
            self._build_remove()
            return

        can_add = len(self._free()) >= 2
        if can_add:
            self.add_item(
                self._alliance_select(VS_BUILDER_FIRST, self.first, self.second, "first", 0)
            )
            self.add_item(
                self._score_select(
                    self.first, self.first_score, VS_BUILDER_FIRST_SCORE, "first_score", 1
                )
            )
            self.add_item(
                self._alliance_select(VS_BUILDER_SECOND, self.second, self.first, "second", 2)
            )
            self.add_item(
                self._score_select(
                    self.second, self.second_score, VS_BUILDER_SECOND_SCORE, "second_score", 3
                )
            )

        self.add_button(
            VS_BTN_BUILDER_ADD, discord.ButtonStyle.primary, self._add, row=4, disabled=not can_add
        )
        self.add_button(
            VS_BTN_BUILDER_SAVE,
            discord.ButtonStyle.success,
            self._save,
            row=4,
            disabled=not self.matches,
        )
        self.add_button(
            VS_BTN_BUILDER_REMOVE,
            discord.ButtonStyle.secondary,
            self._start_remove,
            row=4,
            disabled=not self.matches,
        )
        self.add_button(
            VS_BTN_BUILDER_TEXT_BOX, discord.ButtonStyle.secondary, self._text_box, row=4
        )

    def _build_remove(self) -> None:
        options = [
            discord.SelectOption(
                label=self.match_line(match)[:100], value=str(i), default=i == self.doomed
            )
            for i, match in enumerate(self.matches)
        ]
        select = discord.ui.Select(
            placeholder=VS_BUILDER_REMOVE_PLACEHOLDER,
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )
        select.callback = self._pick_doomed
        self.add_item(select)
        self.add_button(
            VS_BTN_BUILDER_REMOVE_SELECTED,
            discord.ButtonStyle.secondary,
            self._remove_selected,
            row=1,
            disabled=self.doomed is None,
        )
        self.add_button(
            VS_BTN_BUILDER_BACK, discord.ButtonStyle.secondary, self._leave_remove, row=1
        )

    async def _redraw(self, interaction: discord.Interaction) -> None:
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    # ── Callbacks ────────────────────────────────────────────────────────────

    def _setter(self, attr: str, *, alliance: bool):
        async def _callback(interaction: discord.Interaction):
            raw = (interaction.data.get("values") or [None])[0]
            if raw is None:
                return
            setattr(self, attr, self.roster[int(raw)] if alliance else int(raw))
            await self._redraw(interaction)

        return _callback

    async def _add(self, interaction: discord.Interaction):
        picks = (self.first, self.first_score, self.second, self.second_score)
        if any(pick is None for pick in picks):
            await interaction.response.send_message(VS_BUILDER_INCOMPLETE, ephemeral=True)
            return
        if self.first_score + self.second_score != ad.WEEK_POINTS_TOTAL:
            await interaction.response.send_message(
                ad_entry.RESULTS_BAD_TOTAL.format(
                    label=f"{self._name(self.first)} v {self._name(self.second)}",
                    x=self.first_score,
                    y=self.second_score,
                    total=ad.WEEK_POINTS_TOTAL,
                ),
                ephemeral=True,
            )
            return
        self.matches.append(picks)
        self._clear_picks()
        await self._redraw(interaction)

    async def _start_remove(self, interaction: discord.Interaction):
        self.removing = True
        self.doomed = None
        await self._redraw(interaction)

    async def _pick_doomed(self, interaction: discord.Interaction):
        raw = (interaction.data.get("values") or [None])[0]
        if raw is None:
            return
        self.doomed = int(raw)
        await self._redraw(interaction)

    async def _remove_selected(self, interaction: discord.Interaction):
        if self.doomed is not None and self.doomed < len(self.matches):
            del self.matches[self.doomed]
        self.removing = False
        self.doomed = None
        await self._redraw(interaction)

    async def _leave_remove(self, interaction: discord.Interaction):
        self.removing = False
        self.doomed = None
        await self._redraw(interaction)

    async def _text_box(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            ad_entry.OtherResultsModal(self.state, self.week, backfill=True)
        )

    async def _save(self, interaction: discord.Interaction):
        # Defer before any sheet round-trip (CLAUDE.md 1.1.7 / #76).
        await interaction.response.defer()

        rows = []
        for first, x, second, y in self.matches:
            rows.extend(ad_entry.result_rows(self.state, self.week, first, x, second, y))
        said = ad_entry.results_saved_lines(self.state, rows)

        problem = await ad_entry.save_rows(self.state, rows, actor=interaction)
        if problem:
            await interaction.followup.send(f"⚠️ {problem}", ephemeral=True)
            return

        self.stop()
        await interaction.followup.send(
            ad_entry.RESULTS_SAVED.format(n=len(said), s="" if len(said) == 1 else "s")
            + " "
            + ", ".join(said),
            ephemeral=True,
        )
        # The saved list is the last thing that should be on screen, not a
        # builder with nothing left to build.
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass
