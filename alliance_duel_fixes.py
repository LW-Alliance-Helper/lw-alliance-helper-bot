"""Fix a VS data problem from the screen that caused it (#651).

Every one of the nine checks in `alliance_duel.validate` has a Discord screen
that writes the value it complains about, because Discord entry and Sheet entry
are the same write path. So a finding never sends an officer to their Sheet: it
carries a button that opens that screen, already on the week, day or alliance
concerned.

Two ways in, one set of buttons:

- **After a save.** `save_rows` checks the snapshot before and after it patches
  it, and keeps only what the save created (`alliance_duel.new_findings`). The
  screen that saved calls :func:`send_new_findings` once its own confirmation is
  out, so the problem lands directly under "✅ Saved" rather than above it.
- **Check my data for errors**, the full sweep on the setup panel, which lists
  everything and attaches the same :class:`FindingsFixView`.

**Current league only.** Every entry screen writes into `state.league`, and the
button labels name a week but not a league, so a finding in an earlier league
is listed without a button rather than offered a screen that would write into
the wrong one.

The entry modules are imported inside the callbacks: `alliance_duel_entry`
imports this module at load, so a top-level import back would be a cycle.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord

import alliance_duel as ad
import alliance_duel_setup as ad_setup
from wizard_registry import OwnedView

logger = logging.getLogger(__name__)

# Signed off 28 Sep (#651).
VS_CHECK_AFTER_SAVE_TITLE = "⚠️ Something in that save doesn't add up"
VS_CHECK_AFTER_SAVE_LINE = (
    "Your save went through. If this is a mistake, the button below opens the screen that fixes it."
)
VS_FIX_RESULTS = "Enter week {week} results"
VS_FIX_SCORE = "✏️ Enter day {day} score (week {week})"
VS_FIX_RANK = "🔢 Set {tag}'s rank (week {week})"
VS_FIX_PREDICT = "Enter predictions for week {week}"

#: Rules whose value is written by the week's results screen: the two scores,
#: the days against the score, the outcome, and who played whom.
_RESULTS_RULES = frozenset({1, 2, 3, 4})


def state_findings(state) -> list[ad.Finding]:
    """Everything wrong with the loaded snapshot, in the state's own mode.

    The effective mode, not the configured one: a lapsed guild reads as
    own-alliance (#667), and the bracket rules would otherwise report its
    whole-bracket rows as mistakes it cannot currently see or fix.
    """
    return ad.validate(
        state.rows,
        tracking_mode=getattr(state, "tracking_mode", ad.MODE_FULL_BRACKET),
        own_alliance=getattr(state, "own", None),
    )


# ── Which screen fixes what ───────────────────────────────────────────────────


@dataclass(frozen=True)
class FixTarget:
    """One button: the screen it opens, and what it opens that screen on."""

    kind: str
    label: str
    week: int | None = None
    day: int | None = None
    alliance: ad.AllianceKey | None = None


def fix_targets(state, findings) -> list[FixTarget]:
    """The buttons for these findings, one per screen, in finding order.

    Several findings often share a screen (a week's scores and its pairings are
    both fixed in that week's results), so a button is keyed on what it opens
    rather than on the finding. A finding with no screen that can reach it gets
    no button: an earlier league's, or a day score for a match the officer's
    daily score screen does not cover.
    """
    import alliance_duel_entry as ad_entry
    import alliance_duel_league_edit as ad_edit

    out: dict[tuple, FixTarget] = {}
    for f in findings:
        if f.league is None or f.league != state.league:
            continue
        target = None
        if f.rule in _RESULTS_RULES and f.week:
            target = FixTarget("results", VS_FIX_RESULTS.format(week=f.week), week=f.week)
        elif f.rule == 8 and f.week and f.day:
            # The daily score screen writes our row and our opponent's, so it
            # can only reach a day recorded on one of those two.
            if f.alliance is not None and f.alliance in (state.own, state.own_match(f.week)):
                target = FixTarget(
                    "score", VS_FIX_SCORE.format(day=f.day, week=f.week), week=f.week, day=f.day
                )
        elif f.rule == 5 and f.week and f.alliance is not None:
            target = FixTarget(
                "rank",
                VS_FIX_RANK.format(tag=state.display_name(f.alliance), week=f.week),
                week=f.week,
                alliance=f.alliance,
            )
        elif f.rule == 7 and f.week:
            target = FixTarget("predictions", VS_FIX_PREDICT.format(week=f.week), week=f.week)
        elif f.rule == 6:
            target = FixTarget("add", ad_entry.VS_BTN_ADD_ALLIANCE)
        elif f.rule == 9:
            target = FixTarget("remove", ad_edit.VS_BTN_REMOVE_ALLIANCE)
        if target is not None:
            out.setdefault((target.kind, target.week, target.day, target.alliance), target)
    return list(out.values())


class FindingsFixView(OwnedView):
    """One button per screen that fixes something listed above it."""

    timeout_hint = "`/vs`"

    def __init__(self, state, findings, owner_id: int):
        import alliance_duel_entry as ad_entry

        super().__init__(timeout=ad_entry.ENTRY_TIMEOUT)
        self.state = state
        self.owner_id = owner_id
        self.message: discord.Message | None = None
        # 25 is Discord's cap; the report shows at most MAX_FINDINGS_SHOWN
        # findings, so the cap is never what limits this in practice.
        self.targets = fix_targets(state, findings)[:25]
        for target in self.targets:
            self.add_button(target.label, discord.ButtonStyle.secondary, self._opener(target))

    def _opener(self, target: FixTarget):
        async def _open(interaction: discord.Interaction):
            await open_fix(interaction, self.state, target)

        return _open


async def open_fix(interaction: discord.Interaction, state, target: FixTarget) -> None:
    """Open `target`'s screen the way the hub's own button would."""
    import alliance_duel_entry as ad_entry
    import alliance_duel_league_edit as ad_edit
    import alliance_duel_results_builder as builder

    if target.kind == "results":
        view = builder.ResultsBuilderView(state, target.week, interaction.user.id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)
        view.message = await interaction.original_response()
    elif target.kind == "score":
        await interaction.response.send_modal(
            ad_entry.ScoreModal(
                state, target.week, target.day, ad_entry.own_opponent(state, target.week)
            )
        )
    elif target.kind == "rank":
        await interaction.response.send_modal(
            ad_entry.AllianceRankModal(state, target.week, target.alliance)
        )
    elif target.kind == "predictions":
        view = ad_entry.PredictionsView(state, target.week, interaction.user.id, interaction.guild)
        await interaction.response.send_message(
            embed=ad_entry.predictions_embed(
                state, target.week, {}, interaction.guild, interaction.user.id
            ),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()
    elif target.kind == "add":
        await interaction.response.send_modal(ad_entry.AllianceModal(state, state.week or 1))
    elif target.kind == "remove":
        await ad_edit.open_remove_picker(interaction, state)


# ── After a save ──────────────────────────────────────────────────────────────


def after_save_embed(findings) -> discord.Embed:
    """What a save turned up, under its own confirmation."""
    shown = list(findings)[: ad_setup.MAX_FINDINGS_SHOWN]
    lines = [ad_setup.finding_line(f) for f in shown]
    hidden = len(findings) - len(shown)
    if hidden:
        lines.append(ad_setup.more_findings_line(hidden))
    errors = any(f.severity == ad.SEVERITY_ERROR for f in findings)
    embed = discord.Embed(
        title=VS_CHECK_AFTER_SAVE_TITLE,
        description=VS_CHECK_AFTER_SAVE_LINE + "\n\n" + "\n".join(lines),
        color=discord.Color.red() if errors else discord.Color.orange(),
    )
    # Alliance-supplied tags ride in these lines; clamp under the 4096 cap.
    if len(embed.description) > 4000:
        embed.description = embed.description[:3990] + "\n…"
    return embed


async def send_new_findings(interaction: discord.Interaction, state) -> None:
    """Post what the last save created, with a way to fix each, if anything.

    Called by a screen **after** its own confirmation, so the problem sits
    directly under the "✅ Saved" it qualifies. Never raises: the save has
    already been confirmed, and a failure here must not turn that into an
    "interaction failed".
    """
    found = list(getattr(state, "new_findings", None) or [])
    if not found:
        return
    # Consumed, so a second screen reusing this state does not repeat it.
    state.new_findings = []
    try:
        view = FindingsFixView(state, found[: ad_setup.MAX_FINDINGS_SHOWN], interaction.user.id)
        kwargs = {"embed": after_save_embed(found), "ephemeral": True}
        if view.targets:
            kwargs["view"] = view
        message = await interaction.followup.send(**kwargs)
        if view.targets:
            view.message = message
    except Exception:  # noqa: BLE001 - the save stands; this is the follow-up
        logger.exception("[VS] after-save check failed for guild=%s", state.guild_id)
