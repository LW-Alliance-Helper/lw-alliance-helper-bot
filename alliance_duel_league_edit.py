"""Edit league: the league's details, and adding or removing an alliance (#651).

The hub's **✏️ Edit league** opens a small screen rather than a form, because
it now holds three things a league gets corrected by: its details (season,
tier, group), adding or editing an alliance, and removing one. Kevin, 28 Sep:
this is where an officer goes to change what is in the league, so adding and
removing live here together, and "Add or edit alliance" left the hub for it.

**Removing is the one delete in the feature**, and it exists for one mistake:
an alliance typed wrong (`LlON` for `LION`) that the data check reports as a
seventeenth. Before this, the only way out was the Sheet. It is deliberately
narrow:

- **The current league only.** The same tag in an earlier league is most
  likely the real alliance, and the mistake being fixed happened here.
- **Never the officer's own alliance.** Removing it would take the rest of the
  feature with it; a wrong own tag is a setup fix.
- **The Sheet, plus this server's copies in the shared store**, never a row
  another server recorded (`alliance_duel_db.remove_alliance_from_league`).
- **Pairings are warned about twice**: named on the confirm step, while the
  officer can still back out, and offered back afterwards, as that week's
  results screen to pair them again or a button to leave them unpaired.
"""

from __future__ import annotations

import asyncio
import logging

import discord

import alliance_duel as ad
import alliance_duel_setup as ad_setup
import config
import config_health
import messages
from wizard_registry import OwnedView

logger = logging.getLogger(__name__)

# Signed off 28 Sep, on the second #651 page.
VS_BTN_EDIT_LEAGUE_MENU = "✏️ Edit league"
VS_EDIT_LEAGUE_PROMPT = "What do you want to change in **{league}**?"
VS_BTN_REMOVE_ALLIANCE = "🗑️ Remove an alliance"
VS_REMOVE_NONE = "There's no other alliance in **{league}** to remove."
VS_REMOVE_PICK_PROMPT = "Which alliance do you want to remove from **{league}**?"
VS_REMOVE_PICK_PLACEHOLDER = "Pick an alliance"
VS_REMOVE_OPT_RANK = "Rank {rank}"
VS_REMOVE_OPT_NO_RANK = "No rank recorded yet"
VS_REMOVE_CONFIRM_TITLE = "🗑️ Remove {tag} from {league}?"
VS_REMOVE_CONFIRM_BODY = (
    "This deletes **{tag}**'s entries for {weeks} of this League from your Sheet, along "
    "with the copies your server shared. It can't be undone."
)
VS_REMOVE_PAIRED_FIELD = "Still paired with {tag}"
VS_REMOVE_PAIRED_LINE = "**{other}** is recorded against **{tag}** in {weeks}."
VS_REMOVE_PAIRED_NOTE = (
    "They keep that pairing. After the removal you can pair them again or leave them unpaired."
)
VS_BTN_REMOVE_CONFIRM = "🗑️ Remove {tag}"
VS_BTN_REMOVE_CANCEL = "Cancel"
VS_REMOVE_CANCELED = "Nothing was removed."
VS_REMOVE_DONE = "✅ Removed **{tag}** from **{league}**."
VS_REMOVE_DONE_PAIRED_LINE = "**{other}** still has **{tag}** as their opponent in {weeks}."
VS_REMOVE_DONE_PAIRED_ASK = "Pair them again, or leave them unpaired."
VS_BTN_UNPAIR = "Leave them unpaired"
VS_UNPAIR_DONE = "✅ Left {who} unpaired."
VS_UNPAIR_WHO = "**{other}** ({weeks})"
VS_REMOVE_FAILED = "⚠️ I couldn't remove it from your tab: {reason}"
VS_UNPAIR_FAILED = "⚠️ I couldn't clear those pairings in your tab: {reason}"


async def _strip_buttons(message) -> None:
    """Take a view's buttons off its message once it has been acted on."""
    if message is None:
        return
    try:
        await message.edit(view=None)
    except discord.HTTPException:
        pass


def _timeout_hint() -> str:
    return messages.ROUTE_HINT.format(cmd="/vs", btn=VS_BTN_EDIT_LEAGUE_MENU)


def weeks_phrase(weeks) -> str:
    """`week 1`, `weeks 1 and 2`, `weeks 1, 2 and 3`."""
    weeks = sorted(set(weeks))
    if len(weeks) == 1:
        return f"week {weeks[0]}"
    head = ", ".join(str(w) for w in weeks[:-1])
    return f"weeks {head} and {weeks[-1]}"


# ── The Edit league screen ────────────────────────────────────────────────────


class EditLeagueView(OwnedView):
    """Details, add, remove: the three ways a league gets corrected."""

    def __init__(self, state, owner_id: int):
        import alliance_duel_entry as ad_entry

        super().__init__(timeout=ad_entry.ENTRY_TIMEOUT, timeout_hint=_timeout_hint())
        self.state = state
        self.owner_id = owner_id
        self.message: discord.Message | None = None
        self.add_button(ad_entry.VS_BTN_EDIT_LEAGUE, discord.ButtonStyle.secondary, self._details)
        self.add_button(ad_entry.VS_BTN_ADD_ALLIANCE, discord.ButtonStyle.secondary, self._add)
        self.add_button(
            VS_BTN_REMOVE_ALLIANCE,
            discord.ButtonStyle.secondary,
            self._remove,
            disabled=not removable_alliances(state),
        )

    async def _details(self, interaction: discord.Interaction):
        import alliance_duel_entry as ad_entry

        await interaction.response.send_modal(ad_entry.EditLeagueModal(self.state))

    async def _add(self, interaction: discord.Interaction):
        import alliance_duel_entry as ad_entry

        await interaction.response.send_modal(
            ad_entry.AllianceModal(self.state, self.state.week or 1)
        )

    async def _remove(self, interaction: discord.Interaction):
        await open_remove_picker(interaction, self.state)


async def open_edit_league(interaction: discord.Interaction, state) -> None:
    view = EditLeagueView(state, interaction.user.id)
    await interaction.response.send_message(
        VS_EDIT_LEAGUE_PROMPT.format(league=state.league), view=view, ephemeral=True
    )
    view.message = await interaction.original_response()


# ── Picking who to remove ─────────────────────────────────────────────────────


def removable_alliances(state) -> list[tuple[ad.AllianceKey, int | None]]:
    """The current league's alliances, bar the officer's own, by rank.

    Ranked first in rank order, unranked after them by name: a typo'd tag is
    usually the unranked one, since a row written through Discord carries no
    rank, so it sits at the bottom where it is easy to find.
    """
    ranks: dict[ad.AllianceKey, int | None] = {}
    for row in state.league_rows():
        if row.alliance == state.own:
            continue
        if ranks.get(row.alliance) is None:
            ranks[row.alliance] = row.ranking
    return sorted(
        ranks.items(),
        key=lambda item: (
            item[1] is None,
            item[1] or 0,
            state.display_name(item[0]).casefold(),
        ),
    )


def pairings_with(state, alliance: ad.AllianceKey) -> dict[ad.AllianceKey, list[int]]:
    """Who, in the current league, is recorded against `alliance`, and when."""
    out: dict[ad.AllianceKey, list[int]] = {}
    for row in state.league_rows():
        if row.alliance != alliance and row.opponent == alliance:
            out.setdefault(row.alliance, []).append(row.week)
    return {k: sorted(v) for k, v in out.items()}


async def open_remove_picker(interaction: discord.Interaction, state) -> None:
    choices = removable_alliances(state)
    if not choices:
        await interaction.response.send_message(
            VS_REMOVE_NONE.format(league=state.league), ephemeral=True
        )
        return
    view = RemovePickerView(state, interaction.user.id, choices)
    await interaction.response.send_message(
        VS_REMOVE_PICK_PROMPT.format(league=state.league), view=view, ephemeral=True
    )
    view.message = await interaction.original_response()


class RemovePickerView(OwnedView):
    """A select of the league's alliances. Picking one only asks; the confirm
    step is where anything happens."""

    def __init__(self, state, owner_id: int, choices):
        import alliance_duel_entry as ad_entry

        super().__init__(timeout=ad_entry.ENTRY_TIMEOUT, timeout_hint=_timeout_hint())
        self.state = state
        self.owner_id = owner_id
        self.message: discord.Message | None = None
        # 25 is Discord's cap. A league is sixteen, seventeen with the typo.
        self._alliances = [alliance for alliance, _rank in choices][:25]
        select = discord.ui.Select(
            placeholder=VS_REMOVE_PICK_PLACEHOLDER,
            options=[
                discord.SelectOption(
                    label=state.display_name(alliance)[:100],
                    value=str(i),
                    description=(
                        VS_REMOVE_OPT_RANK.format(rank=rank)
                        if rank is not None
                        else VS_REMOVE_OPT_NO_RANK
                    ),
                )
                for i, (alliance, rank) in enumerate(choices[:25])
            ],
        )
        select.callback = self._picked
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction):
        alliance = self._alliances[int(self.children[0].values[0])]
        confirm = RemoveConfirmView(self.state, self.owner_id, alliance)
        await interaction.response.edit_message(
            content=None, embed=confirm_embed(self.state, alliance), view=confirm
        )
        confirm.message = await interaction.original_response()
        self.stop()


def confirm_embed(state, alliance: ad.AllianceKey) -> discord.Embed:
    tag = state.display_name(alliance)
    weeks = sorted({r.week for r in state.league_rows() if r.alliance == alliance})
    embed = discord.Embed(
        title=VS_REMOVE_CONFIRM_TITLE.format(tag=tag, league=state.league)[:256],
        description=VS_REMOVE_CONFIRM_BODY.format(tag=tag, weeks=weeks_phrase(weeks or [1])),
        color=discord.Color.red(),
    )
    paired = pairings_with(state, alliance)
    if paired:
        lines = [
            VS_REMOVE_PAIRED_LINE.format(
                other=state.display_name(other), tag=tag, weeks=weeks_phrase(ws)
            )
            for other, ws in paired.items()
        ]
        embed.add_field(
            name=VS_REMOVE_PAIRED_FIELD.format(tag=tag)[:256],
            value=("\n".join(lines) + "\n\n" + VS_REMOVE_PAIRED_NOTE)[:1024],
            inline=False,
        )
    return embed


class RemoveConfirmView(OwnedView):
    """The red button, and a way out."""

    def __init__(self, state, owner_id: int, alliance: ad.AllianceKey):
        import alliance_duel_entry as ad_entry

        super().__init__(timeout=ad_entry.ENTRY_TIMEOUT, timeout_hint=_timeout_hint())
        self.state = state
        self.owner_id = owner_id
        self.alliance = alliance
        self.message: discord.Message | None = None
        self.add_button(
            VS_BTN_REMOVE_CONFIRM.format(tag=state.display_name(alliance)),
            discord.ButtonStyle.danger,
            self._confirm,
        )
        self.add_button(VS_BTN_REMOVE_CANCEL, discord.ButtonStyle.secondary, self._cancel)

    async def _cancel(self, interaction: discord.Interaction):
        await interaction.response.edit_message(content=VS_REMOVE_CANCELED, embed=None, view=None)
        self.stop()

    async def _confirm(self, interaction: discord.Interaction):
        # Defer before the Sheet round-trip (CLAUDE.md 1.1.7 / #76), with the
        # thinking indicator, and take the buttons away so a second press
        # cannot run it twice.
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.stop()
        await _strip_buttons(self.message)

        state = self.state
        tag = state.display_name(self.alliance)
        league = state.league
        paired = pairings_with(state, self.alliance)

        problem = await remove_alliance(state, self.alliance)
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return

        text = VS_REMOVE_DONE.format(tag=tag, league=league)
        view = None
        if paired:
            lines = [
                VS_REMOVE_DONE_PAIRED_LINE.format(
                    other=state.display_name(other), tag=tag, weeks=weeks_phrase(ws)
                )
                for other, ws in paired.items()
            ]
            text += "\n\n" + "\n".join(lines) + "\n\n" + VS_REMOVE_DONE_PAIRED_ASK
            view = RepairView(state, self.owner_id, self.alliance, league, paired, tag)
        message = await interaction.followup.send(
            text[:2000], ephemeral=True, **({"view": view} if view else {})
        )
        if view is not None:
            view.message = message


# ── Doing it ──────────────────────────────────────────────────────────────────


def _tab(state) -> str:
    return state.cfg.get("tab_name") or "Alliance Duel (VS)"


async def remove_alliance(state, alliance: ad.AllianceKey) -> str:
    """Take `alliance` out of the current league. Empty string on success.

    The Sheet goes first and decides, as in `save_rows`: a failure there is the
    alliance's to fix and is reported, never Sentry-captured. The shared store
    follows, and its failure costs a stale scouting row rather than the officer
    being told the removal failed after their Sheet already took it.
    """
    league = state.league
    tab = _tab(state)
    # Before the snapshot drops them: the store keys rows by week date (#658).
    dates = {r.week_date for r in state.league_rows() if r.alliance == alliance and r.week_date}

    def _delete():
        spreadsheet = config.get_spreadsheet(state.guild_id)
        worksheet = ad_setup.ensure_tab(spreadsheet, tab)
        numbers = ad.plan_remove_alliance(worksheet.get_all_values(), league, alliance)
        ad.apply_row_deletes(worksheet, numbers)

    try:
        await asyncio.to_thread(_delete)
    except Exception as e:  # noqa: BLE001 - the alliance's sheet, their fix
        logger.warning("[VS] remove failed for guild=%s: %s", state.guild_id, e)
        config_health.record_sheet_failure(state.guild_id, ad_setup.VS_SHEET_SUBJECT, e, tab=tab)
        return VS_REMOVE_FAILED.format(reason=config.describe_sheet_error(e))

    try:
        import alliance_duel_db as vsdb

        # SQLite blocks, and this runs on the gateway thread (#366).
        await asyncio.to_thread(
            vsdb.remove_alliance_from_league,
            alliance,
            league,
            guild_id=state.guild_id,
            week_dates=sorted(dates),
        )
    except Exception as e:  # noqa: BLE001 - the Sheet has it; this is the copy
        logger.warning("[VS] central remove failed for guild=%s: %s", state.guild_id, e)

    state.rows[:] = [r for r in state.rows if not (r.league == league and r.alliance == alliance)]
    state.profiles = ad.build_profiles(state.rows)
    return ""


async def unpair(state, league, opponent: ad.AllianceKey, paired) -> str:
    """Blank `opponent` from the rows that still name it. Empty on success."""
    keys = [ad.RowKey(league, week, other) for other, weeks in paired.items() for week in weeks]
    tab = _tab(state)

    def _clear():
        spreadsheet = config.get_spreadsheet(state.guild_id)
        worksheet = ad_setup.ensure_tab(spreadsheet, tab)
        plan = ad.plan_clear_opponent(worksheet.get_all_values(), keys, opponent)
        ad.apply_upsert(worksheet, plan)

    try:
        await asyncio.to_thread(_clear)
    except Exception as e:  # noqa: BLE001 - the alliance's sheet, their fix
        logger.warning("[VS] unpair failed for guild=%s: %s", state.guild_id, e)
        config_health.record_sheet_failure(state.guild_id, ad_setup.VS_SHEET_SUBJECT, e, tab=tab)
        return VS_UNPAIR_FAILED.format(reason=config.describe_sheet_error(e))

    try:
        import alliance_duel_db as vsdb

        pairs = []
        for key in keys:
            row = state.row_for(key.alliance, key.week)
            pairs.append((key.alliance, key.week, row.week_date if row else None))
        await asyncio.to_thread(
            vsdb.clear_opponent, pairs, league, opponent, guild_id=state.guild_id
        )
    except Exception as e:  # noqa: BLE001 - the Sheet has it; this is the copy
        logger.warning("[VS] central unpair failed for guild=%s: %s", state.guild_id, e)

    wanted = set(keys)
    for row in state.rows:
        if row.key in wanted and row.opponent == opponent:
            row.opponent = None
    return ""


class RepairView(OwnedView):
    """After a removal: pair each affected week again, or leave them unpaired."""

    def __init__(self, state, owner_id: int, removed, league, paired, tag: str):
        import alliance_duel_entry as ad_entry
        import alliance_duel_fixes as ad_fixes

        super().__init__(timeout=ad_entry.ENTRY_TIMEOUT, timeout_hint=_timeout_hint())
        self.state = state
        self.owner_id = owner_id
        self.removed = removed
        self.league = league
        self.paired = paired
        self.tag = tag
        self.message: discord.Message | None = None
        for week in sorted({w for ws in paired.values() for w in ws}):
            target = ad_fixes.FixTarget(
                "results", ad_fixes.VS_FIX_RESULTS.format(week=week), week=week
            )
            self.add_button(target.label, discord.ButtonStyle.secondary, self._results(target))
        self.add_button(VS_BTN_UNPAIR, discord.ButtonStyle.secondary, self._unpair)

    def _results(self, target):
        async def _open(interaction: discord.Interaction):
            import alliance_duel_fixes as ad_fixes

            await ad_fixes.open_fix(interaction, self.state, target)

        return _open

    async def _unpair(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.stop()
        await _strip_buttons(self.message)
        problem = await unpair(self.state, self.league, self.removed, self.paired)
        if problem:
            await interaction.followup.send(problem, ephemeral=True)
            return
        who = [
            VS_UNPAIR_WHO.format(other=self.state.display_name(other), weeks=weeks_phrase(ws))
            for other, ws in self.paired.items()
        ]
        joined = who[0] if len(who) == 1 else ", ".join(who[:-1]) + " and " + who[-1]
        await interaction.followup.send(VS_UNPAIR_DONE.format(who=joined)[:2000], ephemeral=True)
