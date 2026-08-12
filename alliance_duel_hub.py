"""Alliance Duel (VS) — the `/vs` hub (front door) (#402).

One flat command opening a stateful hub: a status embed plus a button grid,
matching `/buddy`, `/train`, `/events`, `/transfers` and `/map_manager`. Not an
`app_commands.Group` with named subcommands. This repo's convention for a
feature this stateful is the hub pattern, and the design doc settles it.

This module owns the hub embed, the button grid, and the two whole-bracket
reads (Bracket and This week). The per-alliance scout profile lives in
`alliance_duel_ui.py`, which is where its read buttons and note modal land in
#404.

**The sheet is read exactly once per `/vs` invocation.** 1.5.1 had to fix storm
screens blowing the Sheets read limit on quick click-through (#269), and this
hub has more buttons than those did. `handle_vs_hub` loads the tab, wraps it in
a `HubState`, and every view in the chain renders from that snapshot. No button
callback reads the sheet. Re-running `/vs` is the refresh, which is also the
only thing that re-reads.
"""

from __future__ import annotations

import asyncio
import logging

import discord

import alliance_duel as ad
import alliance_duel_setup as ad_setup
import config
import config_health
import premium
import wizard_registry

logger = logging.getLogger(__name__)

VS_HUB_CMD = "/vs"

#: Button labels. Named UI surfaces referenced from other modules' copy (the
#: sheet-problem notice and the /help category both point at them), so they
#: live as constants rather than duplicated literals.
VS_BTN_BRACKET = "📊 Bracket"
VS_BTN_WEEK = "📅 This week"
VS_BTN_SCOUT = "🔍 Scout"
VS_BTN_SETUP = "⚙️ Sheet setup and check"

_DENY_NOT_OWNER = "⛔ Only the person who opened this hub can use these buttons."

#: The not-entered glyph, shared with every other VS surface.
NOT_ENTERED = ad_setup.NOT_ENTERED


# ── Loaded state ──────────────────────────────────────────────────────────────


class HubState:
    """One `/vs` invocation's snapshot of the sheet, passed down the chain.

    Everything derived is computed once here rather than per button click:
    profiles, the live week, and the current league. A view that needs
    something not on here should take it as an argument, not go back to the
    sheet for it.
    """

    def __init__(self, guild_id: int, cfg: dict, rows: list[ad.AllianceWeek]):
        self.guild_id = guild_id
        self.cfg = cfg
        self.rows = rows
        self.tracking_mode = cfg.get("tracking_mode") or ad.MODE_FULL_BRACKET
        self.own = ad.AllianceKey.of(cfg.get("own_tag"), cfg.get("own_warzone"))
        self.profiles = ad.build_profiles(rows)
        self.live = ad.resolve_live_week(rows)
        self.league = self.live.league if self.live else ad.latest_league(rows)

    @property
    def full_bracket(self) -> bool:
        return self.tracking_mode == ad.MODE_FULL_BRACKET

    @property
    def week(self) -> int | None:
        return self.live.week if self.live else None

    def league_rows(self, week: int | None = None) -> list[ad.AllianceWeek]:
        """Rows for the current league, optionally narrowed to one week."""
        if self.league is None:
            return []
        return [
            r for r in self.rows if r.league == self.league and (week is None or r.week == week)
        ]

    def row_for(self, alliance: ad.AllianceKey, week: int | None) -> ad.AllianceWeek | None:
        for row in self.league_rows(week):
            if row.alliance == alliance:
                return row
        return None

    def display_name(self, alliance: ad.AllianceKey) -> str:
        """How an alliance reads on screen: its tag, plus its name when one was
        typed. Falls back to the normalised key so a row is never nameless."""
        profile = self.profiles.get(alliance)
        for row in self.rows:
            if row.alliance == alliance and row.tag_display:
                tag = f"[{row.tag_display}]"
                break
        else:
            tag = f"[{alliance.tag.upper()}]"
        name = (profile.name if profile else "") or ""
        return f"{tag} {name}".strip() if name else tag

    def own_match(self, week: int | None) -> ad.AllianceKey | None:
        """Who the guild faces in `week`, from the recorded Opponent column."""
        if self.own is None:
            return None
        row = self.row_for(self.own, week)
        return row.opponent if row else None


# ── Embeds ────────────────────────────────────────────────────────────────────


def hub_embed(state: HubState) -> discord.Embed:
    """The front page: which league and week is live, and the guild's matchup.

    Leads with the matchup because that is what someone opening `/vs` mid-week
    is actually there for. League identity sits underneath as context.
    """
    embed = discord.Embed(title="🏆 Alliance Duel (VS)", color=discord.Color.blurple())

    if state.league is None:
        embed.description = (
            "Your tab is set up but has no league in it yet.\n\n"
            f"Fill in the bracket off the in-game League screen, or open "
            f"**{VS_BTN_SETUP}** for the column guide."
        )
        return embed

    league = state.league
    header = f"**{league.season} · {league.tier} {league.group}**".replace(" ·  ", " · ")
    if state.live:
        day = state.live.day
        when = f"Week {state.live.week} of {ad.LEAGUE_WEEKS}, " + (
            f"day {day} · {state.live.theme} ({ad.DUEL_DAY_BY_NUMBER[day].points} pts)"
            if day
            else "Sunday, no scoring today"
        )
    else:
        when = "No week running right now."
    embed.description = f"{header}\n{when}"

    embed.add_field(name="This week", value=_own_matchup_line(state), inline=False)

    if not state.full_bracket:
        embed.add_field(
            name="Tracking",
            value=(
                f"You are tracking {ad_setup.mode_label(state.tracking_mode)}. "
                f"The bracket views need all 16 alliances; you can widen at any "
                f"time from {ad_setup.VS_SETUP_NAV}."
            ),
            inline=False,
        )

    _add_sheet_problem_field(embed, state.guild_id)
    embed.set_footer(text="Your sheet is the source. Anything you type there wins.")
    return embed


def _own_matchup_line(state: HubState) -> str:
    """The guild's own matchup for the live week, with its running split."""
    if state.own is None:
        return f"I don't know which alliance is yours yet. Set it in {ad_setup.VS_SETUP_NAV}."
    row = state.row_for(state.own, state.week)
    if row is None:
        return f"No row for {state.display_name(state.own)} in this week yet."

    opponent = row.opponent
    if opponent is None:
        return f"{state.display_name(state.own)}, with no opponent recorded for this week yet."

    clinch = ad.clinch_state(row.day_outcomes)
    line = f"{state.display_name(state.own)} vs {state.display_name(opponent)}"
    if clinch.own_points or clinch.opponent_points:
        line += f"\n**{clinch.own_points}-{clinch.opponent_points}** on league points"
        if clinch.clinched:
            line += ", already clinched."
        elif clinch.lost:
            line += ", already lost."
        elif clinch.clinching_days:
            days = ", ".join(
                f"day {d} ({ad.DUEL_DAY_BY_NUMBER[d].points} pts)" for d in clinch.clinching_days
            )
            line += f". Winning {days} clinches it."
        else:
            line += f". {clinch.points_needed} more to take the week."
    return line


def _add_sheet_problem_field(embed: discord.Embed, guild_id: int) -> None:
    """Surface a broken tab on the hub itself.

    The leadership-channel notice can be scrolled past, and the hub is where
    someone goes to ask "is this working?", so a recorded problem has to be
    visible right here (the #413 reasoning, generalised in #414/#379).
    """
    problems = config_health.problems_for_subjects(guild_id, [ad_setup.VS_SHEET_SUBJECT])
    if not problems:
        return
    embed.color = discord.Color.red()
    for problem in problems:
        embed.add_field(
            name=f"⚠️ I can't read {problem.label}"[:256],
            value=(
                f"{config_health.describe(problem)}\n\nEverything below is from the "
                f"last good read, so it may be out of date."
            ).strip()[:1024],
            inline=False,
        )


def bracket_embed(state: HubState, week: int | None = None) -> discord.Embed:
    """All sixteen alliances for the current league, with their raw data.

    Raw, not derived: this is the "what have we actually typed in" view, and a
    blank cell renders as unknown rather than as a zero or an estimate. Sorted
    by seed, because that is the order the in-game bracket screen shows and the
    order someone copying off it will be reading in.
    """
    league = state.league
    embed = discord.Embed(
        title="📊 Bracket",
        color=discord.Color.blurple(),
        description=(
            f"**{league.season} · {league.tier} {league.group}**"
            + (f" · week {week}" if week else "")
        ),
    )

    rows = sorted(
        state.league_rows(week),
        key=lambda r: (r.seed if r.seed is not None else ad.BRACKET_SIZE + 1, r.alliance),
    )
    if not rows:
        embed.description += "\n\n*No rows recorded for this week yet.*"
        return embed

    lines = []
    for row in rows:
        seed = f"`{row.seed:>2}`" if row.seed is not None else "` ?`"
        power = f"{row.power / 1_000_000:,.0f}M" if row.power else NOT_ENTERED
        members = str(row.members) if row.members is not None else NOT_ENTERED
        gift = str(row.gift_level) if row.gift_level is not None else NOT_ENTERED
        mine = " ⬅️" if row.alliance == state.own else ""
        lines.append(
            f"{seed} **{state.display_name(row.alliance)}** · {power} · "
            f"{members} members · gift {gift}{mine}"
        )
    embed.description += "\n\n" + "\n".join(lines)[:3800]
    embed.set_footer(
        text=f"{len(rows)} of {ad.BRACKET_SIZE} alliances · {NOT_ENTERED} means not entered"
    )
    return embed


def week_embed(state: HubState, week: int) -> discord.Embed:
    """This week's matchups, own first, each labelled with its status.

    Status is the design's user-facing ladder: a confirmed result, then Known,
    then Picked, then Estimated, then Unassessed. Unassessed renders plainly
    rather than being folded into something that sounds like a call.
    """
    embed = discord.Embed(
        title=f"📅 Week {week}",
        color=discord.Color.blurple(),
        description=f"**{state.league.season} · {state.league.tier} {state.league.group}**",
    )

    rows = state.league_rows(week)
    pairing = ad.compute_week_pairing(rows, week)
    if isinstance(pairing, ad.BracketIncomplete):
        matches = _matches_from_recorded_opponents(rows)
        if not matches:
            embed.description += f"\n\n*{pairing.detail}*"
            return embed
    else:
        matches = list(pairing.matches)

    matches.sort(key=lambda m: 0 if state.own in (m.a, m.b) else 1)
    estimate = ad.make_estimator(state.profiles)

    lines = []
    for match in matches:
        own_side = state.own in (match.a, match.b)
        left, right = (match.a, match.b)
        if own_side and right == state.own:
            left, right = right, left
        marker = "**" if own_side else ""
        lines.append(
            f"{marker}{state.display_name(left)} vs {state.display_name(right)}{marker}\n"
            f"  {_match_status(state, left, right, week, estimate)}"
        )
    embed.description += "\n\n" + "\n".join(lines)[:3800]
    return embed


def _matches_from_recorded_opponents(rows) -> list[ad.Match]:
    """Fall back to the Opponent column when the bracket can't be computed.

    In own-alliance tracking mode there is no bracket to pair, but the guild
    still recorded who they faced, and refusing to show that would be the
    tracker arguing with a deliberate choice (#448).
    """
    seen: set = set()
    matches = []
    for row in rows:
        if row.opponent is None:
            continue
        key = tuple(sorted((row.alliance, row.opponent)))
        if key in seen:
            continue
        seen.add(key)
        matches.append(ad.Match(row.week, row.alliance, row.opponent))
    return matches


def _match_status(state: HubState, left, right, week: int, estimate) -> str:
    """One line saying who is favoured and on what evidence."""
    left_row = state.row_for(left, week)
    if left_row is not None and left_row.won is not None:
        winner = left if left_row.won else right
        mine = left_row.week_score
        # The two halves of a week always total 13, so the opponent's half is
        # arithmetic rather than a second lookup.
        score = f" ({mine}-{ad.WEEK_POINTS_TOTAL - mine})" if mine is not None else ""
        return f"✅ {state.display_name(winner)} took it{score}"

    left_profile = state.profiles.get(left)
    right_profile = state.profiles.get(right)
    if left_profile is None or right_profile is None:
        return "Not assessed."

    picked = left_row.picked if left_row is not None else None
    projection = ad.project_week(left_profile, right_profile, picked=picked)
    if projection.status == ad.SOURCE_UNASSESSED:
        return "Not assessed. Add power, members and gift level for both."

    favoured = left if projection.outlook in (ad.OUTLOOK_EASY, ad.OUTLOOK_LIKELY) else right
    label = {
        ad.SOURCE_PICKED: "Picked",
        ad.SOURCE_KNOWN: "Known read",
        ad.SOURCE_ESTIMATED: "Estimated",
    }.get(projection.status, "Estimated")
    if projection.outlook == ad.OUTLOOK_TOSSUP:
        return f"{label}: too close to call."
    return f"{label}: {state.display_name(favoured)} favoured ({projection.outlook})."


# ── Hub view ──────────────────────────────────────────────────────────────────


class VSHubView(discord.ui.View):
    """The button grid. Renders from `state` and never re-reads the sheet."""

    def __init__(self, bot, state: HubState, owner_id: int):
        super().__init__(timeout=900)
        self.bot = bot
        self.state = state
        self.owner_id = owner_id
        self.message: discord.Message | None = None

        has_league = state.league is not None
        bracket = discord.ui.Button(
            label=VS_BTN_BRACKET,
            style=discord.ButtonStyle.primary,
            disabled=not has_league,
            row=0,
        )
        bracket.callback = self._bracket
        self.add_item(bracket)

        week = discord.ui.Button(
            label=VS_BTN_WEEK,
            style=discord.ButtonStyle.primary,
            disabled=not (has_league and state.week),
            row=0,
        )
        week.callback = self._week
        self.add_item(week)

        scout = discord.ui.Button(
            label=VS_BTN_SCOUT,
            style=discord.ButtonStyle.primary,
            disabled=not has_league,
            row=0,
        )
        scout.callback = self._scout
        self.add_item(scout)

        setup = discord.ui.Button(label=VS_BTN_SETUP, style=discord.ButtonStyle.secondary, row=1)
        setup.callback = self._setup
        self.add_item(setup)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        await wizard_registry.expire_view_message(self.message, command_hint=f"`{VS_HUB_CMD}`")

    async def _bracket(self, interaction: discord.Interaction):
        if not self.state.full_bracket:
            await interaction.response.send_message(
                embed=ad_setup.upsell_embed(
                    ad.BracketIncomplete(
                        reason="own_alliance_mode",
                        detail=(
                            "The bracket view shows all 16 alliances, and you are "
                            "tracking just your own."
                        ),
                    )
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=bracket_embed(self.state, self.state.week), ephemeral=True
        )

    async def _week(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            embed=week_embed(self.state, self.state.week), ephemeral=True
        )

    async def _scout(self, interaction: discord.Interaction):
        from alliance_duel_ui import open_scout_picker

        await open_scout_picker(interaction, self.state)

    async def _setup(self, interaction: discord.Interaction):
        from alliance_duel_wizard import run_vs_setup

        await run_vs_setup(interaction, self.bot)


# ── Entry point ───────────────────────────────────────────────────────────────


async def handle_vs_hub(bot, interaction: discord.Interaction) -> None:
    """Top-level handler for `/vs`. Leadership plus Premium gated.

    The whole tracker is Premium, so free tier gets the upsell rather than a
    half-open hub. Everything derived from the sheet is what the alliance is
    paying for; they typed the raw values themselves.
    """
    from setup_cog import _has_leadership_or_admin

    if not _has_leadership_or_admin(interaction):
        cfg = config.get_config(interaction.guild_id)
        role = (cfg.leadership_role_name if cfg else None) or "Leadership"
        await interaction.response.send_message(
            f"⛔ You need the **{role}** role (or admin) to use the Alliance Duel tracker.",
            ephemeral=True,
        )
        return

    if not await premium.feature_gate(
        "alliance_duel_vs", interaction.guild_id, interaction=interaction, bot=bot
    ):
        await interaction.response.send_message(
            embed=premium.premium_locked_embed(
                feature_label="Alliance Duel (VS) tracker",
                description=(
                    "The VS tracker turns the league data you type into your sheet into a "
                    "readable bracket, a per-week projection, your path through the bracket, "
                    "and your record against every alliance you have faced. It's part of "
                    "LW Alliance Helper Premium. Run `/upgrade` to unlock it."
                ),
            ),
            view=premium.upgrade_view(),
            ephemeral=True,
        )
        return

    vs_cfg = config.get_vs_config(interaction.guild_id)
    if not vs_cfg.get("enabled"):
        await interaction.response.send_message(
            embed=discord.Embed(
                title="🏆 Alliance Duel (VS)",
                description=(
                    "Not set up yet. The tracker reads a tab in your own sheet, so it needs "
                    "one sitting with the in-game League screen open.\n\n"
                    f"Start at {ad_setup.VS_SETUP_NAV}."
                ),
                color=discord.Color.blurple(),
            ),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)

    # The one sheet read. Everything below renders from this snapshot (#269).
    try:
        rows = await asyncio.to_thread(
            ad_setup.load_rows, interaction.guild_id, vs_cfg.get("tab_name") or "Alliance Duel (VS)"
        )
    except Exception as e:  # noqa: BLE001 - a bot bug, not the alliance's to fix
        logger.exception("[VS] hub load failed for guild %s", interaction.guild_id)
        await interaction.followup.send(
            f"⚠️ Couldn't read your sheet: {config.describe_sheet_error(e)}", ephemeral=True
        )
        return

    if rows is None:
        problems = config_health.problems_for_subjects(
            interaction.guild_id, [ad_setup.VS_SHEET_SUBJECT]
        )
        detail = config_health.describe(problems[0]) if problems else "I couldn't read the tab."
        await interaction.followup.send(
            embed=discord.Embed(
                title="🏆 Alliance Duel (VS)",
                description=f"⚠️ {detail}\n\nFix that and run `{VS_HUB_CMD}` again.",
                color=discord.Color.red(),
            ),
            ephemeral=True,
        )
        return

    state = HubState(interaction.guild_id, vs_cfg, rows)
    view = VSHubView(bot, state, interaction.user.id)
    await interaction.followup.send(embed=hub_embed(state), view=view, ephemeral=True)
    view.message = await interaction.original_response()
