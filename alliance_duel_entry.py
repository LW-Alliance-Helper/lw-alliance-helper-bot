"""Alliance Duel (VS) — the Discord-side entry paths (#404).

Everything here writes to the alliance's own tab. The sheet stays the primary
entry path and always wins: these surfaces exist because typing a day score on
a phone at reset time is easier than opening a spreadsheet, not because the
spreadsheet is second class.

Three rules hold across every write in this module.

**Defer before touching the sheet.** Every modal `on_submit` defers first. A
slow gspread call otherwise expires the 3-second interaction token and the
submit dies with `NotFound 10062`, which CLAUDE.md records as a bug this repo
has already shipped once (1.1.7, #76).

**Never clobber a human edit.** Writes go through `plan_upsert`, which locates
a row by its key and touches only the columns it actually has a value for. A
blank field in a modal means "leave whatever is there", never "set to empty".

**Patch the snapshot after writing.** The hub reads the sheet once per
invocation (#269), so a write has to be reflected in the loaded `HubState` too
or the next button click renders the value the user just replaced.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging

import discord

import alliance_duel as ad
import alliance_duel_setup as ad_setup
import config
import config_health
import messages
from wizard_registry import expire_view_message

logger = logging.getLogger(__name__)

#: Typing into a modal, per the DESIGN.md timeout tiers.
ENTRY_TIMEOUT = 300

#: Button labels on the entry surfaces, as constants so other modules' copy can
#: name them without retyping the words.
VS_BTN_LOG_SCORE = "✏️ Log today's score"
VS_BTN_ADD_ALLIANCE = "➕ Add or edit alliance"
VS_BTN_DETAILS = "✏️ Add name and notes"
#: ✏️ rather than 🔍: recording a read is an edit, and 🔍 names looking one up.
VS_BTN_KNOWN = "✏️ Set a Known read"
#: Bare, and both the same style. These differ only by which side they name,
#: which is a parameter choice rather than a difference in kind, so a glyph on
#: each would be the same glyph twice. Styling one `success` would also make
#: the bot look like it had a preferred answer, which it does not.
VS_BTN_PICK_WIN = "Pick us to win"
VS_BTN_PICK_LOSS = "Pick them to win"
VS_BTN_NEXT_WEEK = "➕ Start next week's rows"


# ── Writing ───────────────────────────────────────────────────────────────────


async def save_rows(state, rows: list[ad.AllianceWeek]) -> str:
    """Upsert `rows` into the guild's tab and patch the loaded snapshot.

    Returns an empty string on success, or a sentence naming what went wrong.
    Errors come back as text rather than raising because every caller is a
    modal submit that has already deferred, and the user needs a reply either
    way.
    """
    tab = state.cfg.get("tab_name") or "Alliance Duel (VS)"

    def _write():
        spreadsheet = config.get_spreadsheet(state.guild_id)
        worksheet = ad_setup.ensure_tab(spreadsheet, tab)
        plan = ad.plan_upsert(worksheet.get_all_values(), rows)
        ad.apply_upsert(worksheet, plan)
        return plan

    try:
        plan = await asyncio.to_thread(_write)
    except Exception as e:  # noqa: BLE001 - the alliance's sheet, their fix
        logger.warning("[VS] write failed for guild=%s: %s", state.guild_id, e)
        # Recorded, not just reported. A renamed tab breaks the write path and
        # the read path alike, and the officer who hits it here should still
        # see it on the hub tomorrow rather than having to remember. Never
        # Sentry-captured: it is the alliance's to fix.
        config_health.record_sheet_failure(state.guild_id, ad_setup.VS_SHEET_SUBJECT, e, tab=tab)
        return f"I couldn't write to your tab: {config.describe_sheet_error(e)}"

    if plan.unmapped_columns:
        missing = ", ".join(plan.unmapped_columns)
        return (
            f"I saved what I could, but your tab has no column called {missing}, "
            f"so those values were not written. Add the column back, or open "
            f"**{ad_setup.VS_SETUP_NAV}** for the column guide."
        )

    _patch_snapshot(state, rows)
    return ""


def _patch_snapshot(state, rows: list[ad.AllianceWeek]) -> None:
    """Fold written rows into the in-memory snapshot.

    The hub deliberately reads once per invocation, so without this the next
    view would render the value the user just replaced and read as a failed
    save.
    """
    by_key = {r.key: r for r in state.rows}
    for row in rows:
        existing = by_key.get(row.key)
        if existing is None:
            state.rows.append(row)
            continue
        for field, value in vars(row).items():
            if value not in (None, "", {}, 0) or field in ("day_scores", "day_outcomes"):
                setattr(existing, field, value or getattr(existing, field))
    state.profiles = ad.build_profiles(state.rows)


def _row_for_write(state, alliance: ad.AllianceKey, week: int) -> ad.AllianceWeek:
    """A row carrying this alliance's identity for `week`, existing or new."""
    existing = state.row_for(alliance, week)
    if existing is not None:
        return ad.AllianceWeek(
            league=existing.league,
            week=existing.week,
            alliance=existing.alliance,
            week_date=existing.week_date,
            ranking=existing.ranking,
            tag_display=existing.tag_display,
            warzone_display=existing.warzone_display,
        )
    return ad.AllianceWeek(
        league=state.league,
        week=week,
        alliance=alliance,
        week_date=state.live.week_date if state.live else None,
    )


# ── Which day is being logged ─────────────────────────────────────────────────


def target_day(state) -> tuple[int, int] | None:
    """The (week, duel day) a score entered right now belongs to.

    Resolved on **server time**, never guild-local: a guild in UTC+10 sees
    Monday locally while it is still Sunday on the game server, and filing a
    day's scores under the wrong day is the #330 / #318 bug class.

    Sunday closes the week rather than opening the next one, so a Sunday entry
    is Saturday's Enemy Buster. Returns ``None`` when no recorded week covers
    today, which is the normal state between leagues.
    """
    if state.live is None:
        return None
    return state.live.week, state.live.day or 6


# ── Score entry ───────────────────────────────────────────────────────────────


class ScoreModal(discord.ui.Modal):
    """Two numbers. Week and duel day come from the date, not from the user.

    Day scores are read **literally**: a bare `500` is five hundred, and a big
    number needs a unit or its full digits. Power uses the opposite convention
    elsewhere, and the split is deliberate: an early-game alliance can honestly
    post `1000` on a day, so there is no floor below which a small number is
    safely assumed to be shorthand, and scaling one would silently multiply a
    real score by a million.
    """

    def __init__(self, state, week: int, day: int, opponent: ad.AllianceKey | None):
        theme = ad.DUEL_DAY_BY_NUMBER[day].theme
        super().__init__(title=f"Day {day}: {theme}"[:45], timeout=ENTRY_TIMEOUT)
        self.state = state
        self.week = week
        self.day = day
        self.opponent = opponent

        self.ours = discord.ui.TextInput(
            label="Your score",
            placeholder="1.2b, 500m, or the full digits",
            required=True,
            max_length=32,
        )
        self.theirs = discord.ui.TextInput(
            label="Their score",
            placeholder="Leave blank if you have not seen it",
            required=False,
            max_length=32,
        )
        self.add_item(self.ours)
        self.add_item(self.theirs)

    async def on_submit(self, interaction: discord.Interaction):
        # Defer before any sheet round-trip (CLAUDE.md 1.1.7 / #76).
        await interaction.response.defer(ephemeral=True, thinking=True)

        ours = ad.parse_score(self.ours.value)
        if ours is None:
            await interaction.followup.send(
                f"⚠️ I could not read **`{self.ours.value}`** as a score. "
                f"Try `1.2b`, `500m`, or the full digits. Run `/vs` and click "
                f"**{VS_BTN_LOG_SCORE}** to try again.",
                ephemeral=True,
            )
            return
        theirs = ad.parse_score(self.theirs.value) if self.theirs.value.strip() else None

        state = self.state
        rows = []
        mine = _row_for_write(state, state.own, self.week)
        mine.day_scores = {self.day: ours}
        rows.append(mine)

        # The higher day score takes the day. That is the game's rule, not an
        # inference, so it is safe to derive rather than ask for twice. It is
        # named in the acknowledgement so a mistyped score is obvious.
        outcome = None
        if theirs is not None:
            outcome = "W" if ours > theirs else ("L" if ours < theirs else None)
            if outcome:
                mine.day_outcomes = {self.day: outcome}
            if self.opponent is not None:
                other = _row_for_write(state, self.opponent, self.week)
                other.day_scores = {self.day: theirs}
                if outcome:
                    other.day_outcomes = {self.day: "L" if outcome == "W" else "W"}
                rows.append(other)

        problem = await save_rows(state, rows)
        if problem:
            await interaction.followup.send(f"⚠️ {problem}", ephemeral=True)
            return

        await interaction.followup.send(
            embed=_score_ack(state, self.week, self.day), ephemeral=True
        )

        # The officer already has their answer, so anything the alliance opted
        # into is announced afterwards and cannot delay or break the save.
        import alliance_duel_events as ad_events

        await ad_events.announce_after_write(
            interaction.client, state, week=self.week, day=self.day
        )


def _score_ack(state, week: int, day: int) -> discord.Embed:
    """Confirm the save and answer the question the score was entered to ask.

    Somebody logging Wednesday's score wants to know where the week stands, so
    the running split and the clinch line come back with the acknowledgement
    instead of costing another click.
    """
    theme = ad.DUEL_DAY_BY_NUMBER[day].theme
    row = state.row_for(state.own, week)
    embed = discord.Embed(
        title=f"✅ Saved day {day}",
        description=f"Recorded your **{theme}** score for week {week}.",
        color=discord.Color.green(),
    )
    if row is None:
        return embed

    scored = row.day_scores.get(day)
    if scored is not None:
        embed.description += f"\nYou scored **{scored:,}**."
    outcome = row.day_outcomes.get(day)
    if outcome:
        embed.description += (
            f" You **{'took' if outcome == 'W' else 'lost'}** the day "
            f"({ad.DUEL_DAY_BY_NUMBER[day].points} pts)."
        )

    clinch = ad.clinch_state(row.day_outcomes)
    if clinch.clinched:
        embed.add_field(
            name="Where the week stands",
            value=f"**{clinch.own_points}-{clinch.opponent_points}**. The week is yours.",
            inline=False,
        )
    elif clinch.lost:
        embed.add_field(
            name="Where the week stands",
            value=f"**{clinch.own_points}-{clinch.opponent_points}**. The week has gone.",
            inline=False,
        )
    elif clinch.own_points or clinch.opponent_points:
        detail = f"**{clinch.own_points}-{clinch.opponent_points}**, {clinch.points_needed} to go."
        if clinch.clinching_days:
            days = ", ".join(
                f"day {d} ({ad.DUEL_DAY_BY_NUMBER[d].points} pts)" for d in clinch.clinching_days
            )
            detail += f" Winning {days} clinches it."
        embed.add_field(name="Where the week stands", value=detail, inline=False)
    return embed


# ── Alliance entry ────────────────────────────────────────────────────────────


class AllianceModal(discord.ui.Modal, title="Add or edit an alliance"):
    """The five values that decide whether a matchup can be projected at all.

    Name and notes are deliberately not here: five inputs is Discord's cap, and
    these five are the ones that unlock everything else. The optional pair get
    their own reopenable modal, following the `ModalLaunchView` pattern.

    Power takes the survey shorthand, where a bare `301` means 301 million.
    Day scores in `ScoreModal` do the opposite on purpose.
    """

    def __init__(self, state, week: int, existing: ad.AllianceWeek | None = None):
        super().__init__(timeout=ENTRY_TIMEOUT)
        self.state = state
        self.week = week

        profile = state.profiles.get(existing.alliance) if existing else None
        self.tag = discord.ui.TextInput(
            label="Alliance tag",
            placeholder="ABC",
            default=(existing.tag_display if existing else "") or None,
            required=True,
            max_length=12,
        )
        self.warzone = discord.ui.TextInput(
            label="Warzone",
            placeholder="1234",
            default=(existing.warzone_display if existing else "") or None,
            required=True,
            max_length=12,
        )
        self.power = discord.ui.TextInput(
            label="Total power",
            placeholder="301 means 301M. Also 1.2b, 304,743,912",
            default=str(profile.power) if profile and profile.power else None,
            required=False,
            max_length=24,
        )
        self.members = discord.ui.TextInput(
            label="Members", placeholder="95", required=False, max_length=6
        )
        self.gift = discord.ui.TextInput(
            label="Gift level", placeholder="18", required=False, max_length=4
        )
        for item in (self.tag, self.warzone, self.power, self.members, self.gift):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        alliance = ad.AllianceKey.of(self.tag.value, self.warzone.value)
        if alliance is None:
            await interaction.followup.send(
                "⚠️ An alliance needs both a tag and a warzone. Run `/vs` and click "
                f"**{VS_BTN_ADD_ALLIANCE}** to try again.",
                ephemeral=True,
            )
            return

        state = self.state
        row = _row_for_write(state, alliance, self.week)
        row.tag_display = self.tag.value.strip()
        row.warzone_display = self.warzone.value.strip()

        # A blank field means "leave it alone", never "clear it". `row_values`
        # omits None, so an untouched field is absent from the write entirely.
        if self.power.value.strip():
            row.power = ad.parse_power(self.power.value)
        if self.members.value.strip():
            row.members = ad.parse_int(self.members.value)
        if self.gift.value.strip():
            row.gift_level = ad.parse_int(self.gift.value)

        problem = await save_rows(state, [row])
        if problem:
            await interaction.followup.send(f"⚠️ {problem}", ephemeral=True)
            return

        profile = state.profiles.get(alliance)
        embed = discord.Embed(
            title="✅ Saved",
            description=f"Updated **{state.display_name(alliance)}** for week {self.week}.",
            color=discord.Color.green(),
        )
        if profile is not None and not profile.is_tier_1:
            embed.add_field(
                name="Still needed",
                value=(
                    "Power, members and gift level all have to be recorded before "
                    "this alliance's matchups can be projected."
                ),
                inline=False,
            )
        await interaction.followup.send(
            embed=embed, view=DetailsLaunchView(state, alliance, self.week), ephemeral=True
        )


class AllianceDetailsModal(discord.ui.Modal, title="Name and notes"):
    """The optional pair, reopened from the acknowledgement.

    Notes replace the structured player-power fields the original spec had.
    Free text is honest about what this is: whatever the officer wants to
    remember about the alliance, in whatever language they write in.
    """

    def __init__(self, state, alliance: ad.AllianceKey, week: int):
        super().__init__(timeout=ENTRY_TIMEOUT)
        self.state = state
        self.alliance = alliance
        self.week = week
        profile = state.profiles.get(alliance)

        self.name = discord.ui.TextInput(
            label="Alliance name",
            default=(profile.name if profile else "") or None,
            required=False,
            max_length=64,
        )
        self.notes = discord.ui.TextInput(
            label="Notes",
            style=discord.TextStyle.paragraph,
            default=(profile.notes if profile else "") or None,
            required=False,
            max_length=900,
        )
        self.add_item(self.name)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = _row_for_write(self.state, self.alliance, self.week)
        row.name = self.name.value.strip()
        row.notes = self.notes.value.strip()

        problem = await save_rows(self.state, [row])
        if problem:
            await interaction.followup.send(f"⚠️ {problem}", ephemeral=True)
            return
        await interaction.followup.send(
            f"✅ Updated **{self.state.display_name(self.alliance)}**.", ephemeral=True
        )


class DetailsLaunchView(discord.ui.View):
    """Offers the optional second modal. Discord cannot chain two modals from
    one submit, so the acknowledgement carries the button instead."""

    def __init__(self, state, alliance: ad.AllianceKey, week: int):
        super().__init__(timeout=ENTRY_TIMEOUT)
        self.state = state
        self.alliance = alliance
        self.week = week

        button = discord.ui.Button(label=VS_BTN_DETAILS, style=discord.ButtonStyle.secondary)
        button.callback = self._open
        self.add_item(button)

    async def _open(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            AllianceDetailsModal(self.state, self.alliance, self.week)
        )


# ── Known reads and Picked calls ──────────────────────────────────────────────


class KnownModal(discord.ui.Modal, title="What do you know about them?"):
    """The human read, which outranks the computed estimate on disagreement.

    Two fields because day 6 is 4 of 13 points and formula-proof, so it has to
    be callable on its own without forcing a full re-read of an alliance
    somebody has only fought once.
    """

    def __init__(self, state, alliance: ad.AllianceKey, week: int):
        super().__init__(timeout=ENTRY_TIMEOUT)
        self.state = state
        self.alliance = alliance
        self.week = week
        profile = state.profiles.get(alliance)

        self.days_1_5 = discord.ui.TextInput(
            label="Days 1 to 5",
            placeholder=" / ".join(ad.KNOWN_SCALE),
            default=(profile.known_1_5 if profile else "") or None,
            required=False,
            max_length=32,
        )
        self.day_6 = discord.ui.TextInput(
            label="Enemy Buster (day 6)",
            placeholder="Only if someone has actually fought them",
            default=(profile.known_6 if profile else "") or None,
            required=False,
            max_length=120,
        )
        self.add_item(self.days_1_5)
        self.add_item(self.day_6)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = _row_for_write(self.state, self.alliance, self.week)
        row.known_1_5 = self.days_1_5.value.strip()
        row.known_6 = self.day_6.value.strip()

        problem = await save_rows(self.state, [row])
        if problem:
            await interaction.followup.send(f"⚠️ {problem}", ephemeral=True)
            return

        note = ""
        if row.known_1_5 and ad.known_rank(row.known_1_5) is None:
            # Kept verbatim rather than coerced. An unranked read still reads
            # fine to a human; it just cannot be compared, so say so once.
            note = (
                f"\nI kept **{row.known_1_5}** as written. To have it weigh against "
                f"the numbers, use one of: {', '.join(ad.KNOWN_SCALE)}."
            )
        await interaction.followup.send(
            f"✅ Saved your read on **{self.state.display_name(self.alliance)}**.{note}",
            ephemeral=True,
        )


class ScoutActionsView(discord.ui.View):
    """The write actions that hang off a scout profile."""

    def __init__(self, state, alliance: ad.AllianceKey, owner_id: int):
        super().__init__(timeout=ENTRY_TIMEOUT)
        self.state = state
        self.alliance = alliance
        self.owner_id = owner_id
        self.week = state.week

        known = discord.ui.Button(label=VS_BTN_KNOWN, style=discord.ButtonStyle.secondary)
        known.callback = self._known
        self.add_item(known)

        # Their day pattern (#408), offered only once something has actually
        # been logged against them. Their day outcomes exist only for weeks you
        # played them, so on a never-met alliance this button would open an
        # embed that says nothing.
        import alliance_duel_analytics as an

        if an.day_profile(state.rows, alliance).weeks_recorded:
            trends = discord.ui.Button(label=_trends_label(), style=discord.ButtonStyle.secondary)
            trends.callback = self._trends
            self.add_item(trends)

        # Only offered on the alliance you are actually playing this week: a
        # Picked call is a call on one specific match, not a standing opinion.
        if self.week and state.own_match(self.week) == alliance:
            for label, outcome in ((VS_BTN_PICK_WIN, "W"), (VS_BTN_PICK_LOSS, "L")):
                button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
                button.callback = self._picker(outcome)
                self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(messages.DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _known(self, interaction: discord.Interaction):
        await interaction.response.send_modal(KnownModal(self.state, self.alliance, self.week or 1))

    async def _trends(self, interaction: discord.Interaction):
        import alliance_duel_ui as ad_ui

        await interaction.response.send_message(
            embed=ad_ui.opponent_trends_embed(self.state, self.alliance), ephemeral=True
        )

    def _picker(self, outcome: str):
        async def _pick(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            row = _row_for_write(self.state, self.state.own, self.week)
            row.picked = outcome
            # Calls made through Discord carry their author so pick accuracy
            # can be ranked later. Calls typed into the sheet have no Discord
            # identity and simply do not feed that.
            row.picked_by = str(interaction.user.id)
            row.opponent = self.alliance

            problem = await save_rows(self.state, [row])
            if problem:
                await interaction.followup.send(f"⚠️ {problem}", ephemeral=True)
                return
            side = "you" if outcome == "W" else self.state.display_name(self.alliance)
            await interaction.followup.send(
                f"✅ Picked **{side}** for week {self.week}.", ephemeral=True
            )

        return _pick


# ── Next week's rows ──────────────────────────────────────────────────────────


def pending_next_week(state) -> int | None:
    """The week to advance *from*, when next week's rows can be written.

    ``None`` whenever the control would be a no-op, which is what lets the hub
    show the button only when pressing it would do something. A button that
    looks actionable and is not costs the user a click and some trust.

    Two conditions: the week just played is fully decided, and the week after
    it has not been written yet.
    """
    if state.league is None or not state.full_bracket:
        return None
    weeks = sorted({r.week for r in state.league_rows()})
    for week in reversed(weeks):
        if week + 1 in weeks:
            continue
        if ad.next_week_rows(state.league_rows(), week):
            return week
    return None


async def generate_next_week(state, week: int, bot=None) -> tuple[bool, str]:
    """Write next week's rows, carried forward with predicted opponents.

    Season, tier, group, ranking, tag and warzone come forward, so the only thing
    left to type is what actually happened. The **predicted** opponent is
    written rather than left blank: if the game paired differently and the
    officer corrects it, that correction is itself the signal that the pairing
    algorithm needs a look, and a blank column would never produce it.
    """
    rows = ad.next_week_rows(state.league_rows(), week)
    if not rows:
        return False, (
            f"Week {week} needs every outcome recorded before I can work out "
            f"week {week + 1}'s pairings."
        )
    problem = await save_rows(state, rows)
    if problem:
        return False, problem

    # Next week's opponent is now known, which is the event the reveal post
    # (#409) exists for. Announced from here rather than from the button so the
    # sheet-first path (an officer typing the rows in themselves) is the only
    # way it can be missed.
    import alliance_duel_events as ad_events

    await ad_events.announce_after_write(bot, state, week=week + 1)

    return True, (
        f"Added {len(rows)} rows for week {week + 1}, with the pairings I expect. "
        f"Correct any the game paired differently."
    )


# ── A new league ──────────────────────────────────────────────────────────────

VS_BTN_NEW_LEAGUE = "➕ Start a new league"


def pending_new_league(state) -> bool:
    """Whether pressing the button would actually start something.

    Two moments, and only two: nothing recorded yet, or the league on the sheet
    has all four weeks decided. In between, the rows already exist and
    :data:`VS_BTN_NEXT_WEEK` is the control that moves them on, so this one
    stays off the hub rather than sitting there inviting a second bracket into
    a league still being played.

    Deliberately exclusive with :func:`pending_next_week`, which declines at
    week 4 because there is no week 5. The two never contend for the same slot
    on the button row.
    """
    if state.league is None:
        return True
    return ad.is_league_complete(state.league_rows(), state.league)


class NewLeagueModal(discord.ui.Modal, title="Start a new league"):
    """League identity off the start screen, and the bracket in ranking order.

    The fields are built in ``__init__`` rather than declared on the class
    because the last one changes shape with the tracking mode: a full bracket
    wants all sixteen alliances, and an own-alliance sheet wants a ranking and
    nothing else. One modal, because a two-step wizard for what is one screen
    in game would be the bot making this longer than it is.
    """

    def __init__(self, state, *, defaults: dict | None = None):
        super().__init__(timeout=ENTRY_TIMEOUT)
        self.state = state
        d = defaults or {}

        self.season = discord.ui.TextInput(
            label="Season",
            placeholder="S36",
            max_length=12,
            required=True,
            default=d.get("season"),
        )
        self.tier = discord.ui.TextInput(
            label="Tier",
            placeholder="Diamond",
            max_length=24,
            required=False,
            default=d.get("tier"),
        )
        self.group = discord.ui.TextInput(
            label="Group",
            placeholder="12 - 1",
            max_length=24,
            required=False,
            default=d.get("group"),
        )
        # A week number rather than a date. The League screen shows a countdown
        # and a Week 1-4 column header, so which week it is on is something the
        # officer can read; the Monday that week 1 began is something they would
        # have to work out. The bot counts back instead.
        self.week_now = discord.ui.TextInput(
            label="Which week is the League on now?",
            placeholder="1, 2, 3 or 4. Blank means week 1.",
            max_length=2,
            required=False,
            default=d.get("week_now"),
        )
        if state.full_bracket:
            self.bracket = discord.ui.TextInput(
                label="The bracket, in League order",
                style=discord.TextStyle.paragraph,
                # Discord caps a placeholder at 100 characters, so the shape is
                # shown rather than described: the labelled example line says
                # the order, and the tail says what is optional.
                placeholder=(
                    "kTZ 714 26.8b 25 100  (tag warzone power gift members)\n"
                    "IMI 685\nAll 16, one per line."
                ),
                max_length=1800,
                required=True,
                default=d.get("bracket"),
            )
        else:
            self.bracket = discord.ui.TextInput(
                label="Your ranking",
                placeholder="9",
                max_length=4,
                required=True,
                default=d.get("bracket"),
            )
        for item in (self.season, self.tier, self.group, self.week_now, self.bracket):
            self.add_item(item)

    def _typed(self) -> dict:
        """What was entered, so a refusal can hand it straight back."""
        return {
            "season": self.season.value,
            "tier": self.tier.value,
            "group": self.group.value,
            "week_now": self.week_now.value,
            "bracket": self.bracket.value,
        }

    async def _refuse(self, interaction: discord.Interaction, message: str) -> None:
        """Say what is wrong and hand back what was typed.

        A validation failure costs one step, not the whole flow (`UX.md`).
        Without the retry button, "try again" would mean retyping sixteen lines
        off a phone screen to fix one of them.
        """
        view = _RetryNewLeagueView(self.state, interaction.user.id, self._typed())
        await interaction.followup.send(message, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)

        league = ad.LeagueKey.of(self.season.value, self.tier.value, self.group.value)
        if league is None:
            await self._refuse(
                interaction,
                "⚠️ A league needs a season, the one on the League screen.",
            )
            return

        raw_week = (self.week_now.value or "").strip()
        week_now = ad.parse_int(raw_week) if raw_week else 1
        if week_now is None or not 1 <= week_now <= ad.LEAGUE_WEEKS:
            await self._refuse(
                interaction,
                f"⚠️ A League runs {ad.LEAGUE_WEEKS} weeks, so that one is a number from 1 "
                f"to {ad.LEAGUE_WEEKS}. Leave it blank if the League has just opened.",
            )
            return

        # Count back from this week to the Monday week 1 began on. Server time,
        # never guild-local: a guild in UTC+10 sees Monday locally while it is
        # still Sunday on the game server, and `week_monday` sends Sunday back.
        week_date = ad.week_monday(ad.server_today()) - _dt.timedelta(weeks=week_now - 1)

        state = self.state
        parse = _parse_new_league_bracket(state, self.bracket.value)
        if not parse.ok:
            await self._refuse(
                interaction,
                "⚠️ I did not write anything. Fix these and try again:\n"
                + "\n".join(f"• {p}" for p in parse.problems[:8]),
            )
            return

        ok, message = await start_new_league(
            state, league, week_date, parse.entries, upto_week=week_now
        )
        if not ok:
            await self._refuse(interaction, f"⚠️ {message}")
            return
        await interaction.followup.send(f"✅ {message}", ephemeral=True)


VS_BTN_RETRY_NEW_LEAGUE = "✏️ Edit and try again"


class _RetryNewLeagueView(discord.ui.View):
    """Reopen the new-league modal with what was typed still in it."""

    def __init__(self, state, user_id: int, defaults: dict):
        super().__init__(timeout=600)
        self.state = state
        self.user_id = user_id
        self.defaults = defaults
        self.message: discord.Message | None = None

        button = discord.ui.Button(label=VS_BTN_RETRY_NEW_LEAGUE, style=discord.ButtonStyle.primary)
        button.callback = self._retry
        self.add_item(button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(messages.DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        await expire_view_message(self.message, command_hint="`/vs`")

    async def _retry(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NewLeagueModal(self.state, defaults=self.defaults))


def _parse_new_league_bracket(state, text) -> ad.BracketParse:
    """Read the modal's last field, whichever shape the tracking mode gave it.

    Own-alliance mode types a ranking rather than a roster, so the alliance comes
    from the configured identity and the field supplies only where it sits in
    the bracket. The result is the same shape either way, which is what keeps
    :func:`start_new_league` free of the mode question.
    """
    if state.full_bracket:
        return ad.parse_bracket(text)

    if state.own is None:
        return ad.BracketParse(
            problems=(
                f"I do not know which alliance is yours yet. Set it in {ad_setup.VS_SETUP_NAV}.",
            )
        )
    ranking = ad.parse_int(text)
    if ranking is None or not 1 <= ranking <= ad.BRACKET_SIZE:
        return ad.BracketParse(problems=(f"A ranking is a number from 1 to {ad.BRACKET_SIZE}.",))

    row = next((r for r in state.rows if r.alliance == state.own and r.tag_display), None)
    return ad.BracketParse(
        entries=(
            ad.BracketEntry(
                alliance=state.own,
                ranking=ranking,
                tag_display=(row.tag_display if row else state.own.tag.upper()),
                warzone_display=(row.warzone_display if row else state.own.warzone),
            ),
        )
    )


async def start_new_league(
    state, league, week_date, entries, *, upto_week: int = 1
) -> tuple[bool, str]:
    """Write rows for `league` from week 1 up to `upto_week`, ready to fill.

    The League screen shows all sixteen alliances and their rankings the moment a
    league opens, so this is one sitting: the bot writes the rows and the
    officer fills power, members and gift level straight off that same screen.

    **Every week up to the current one, not just week 1.** An alliance that
    finds this feature in week 3 would otherwise get a set of rows dated a
    fortnight ago, no row covering today, and a hub that reports itself as
    between leagues, which is the exact opposite of what they just asked for.
    The intervening weeks come out blank and ranked, which is also what makes
    backfilling the results off the League screen a matter of typing outcomes.

    Pairings are **not** written for any of them. Week 1's follow from the
    rankings, and later weeks' cannot be known until the preceding week is
    recorded, so a written guess there would be a confident lie rather than a
    prediction worth correcting. `next_week_rows` still writes one for week
    N+1 once week N is decided, which is when it means something.
    """
    if state.own is not None and state.full_bracket:
        if not any(e.alliance == state.own for e in entries):
            return False, (
                f"**{state.display_name(state.own)}** is not in that bracket. Your own "
                "alliance has to be one of the sixteen."
            )

    existing = {(r.league, r.week, r.alliance) for r in state.rows}
    rows = []
    for week in range(1, max(1, min(upto_week, ad.LEAGUE_WEEKS)) + 1):
        rows += ad.skeleton_rows(
            league,
            week,
            week_date + _dt.timedelta(weeks=week - 1),
            [(e.alliance, e.ranking) for e in entries],
            tracking_mode=state.tracking_mode,
            own_alliance=state.own,
        )
    if not rows:
        return False, (
            "I had nothing to write. I need to know which alliance is yours before I "
            f"can track only that one: set it in {ad_setup.VS_SETUP_NAV}."
        )

    # Whatever came in on the bracket lines rides onto the rows here. A field
    # left off a line stays None, which `row_values` omits from the write, so
    # the skeleton keeps its "leave whatever is there" behaviour.
    typed = {e.alliance: e for e in entries}
    for row in rows:
        entry = typed.get(row.alliance)
        if entry is not None:
            row.tag_display = entry.tag_display
            row.warzone_display = entry.warzone_display
            row.power = entry.power
            row.gift_level = entry.gift_level
            row.members = entry.members

    problem = await save_rows(state, rows)
    if problem:
        return False, problem

    # The league the rest of the hub reads comes from the loaded snapshot, and
    # the rows just written are the only ones in it. Without this the officer
    # would save a bracket and land back on a hub that still says there is no
    # league, which reads as a failed write.
    state.league = league

    added = len([r for r in rows if (r.league, r.week, r.alliance) not in existing])
    noun = "row" if added == 1 else "rows"
    weeks = sorted({r.week for r in rows})
    span = f"week {weeks[0]}" if len(weeks) == 1 else f"weeks {weeks[0]} to {weeks[-1]}"

    # Counted per alliance, not per row: the same alliance appears once a week,
    # and "48 of them still need power" would be three times the truth.
    short = [e for e in entries if not (e.power and e.members and e.gift_level)]
    if not short:
        nudge = "Record each day as it lands."
    elif len(short) == len(entries):
        nudge = (
            "Add power, gift level and members from the League screen so matchups "
            "can be projected, then record each day as it lands."
        )
    else:
        nudge = (
            f"{len(short)} of them still need power, gift level or members before their "
            f"matchups can be projected. Record each day as it lands."
        )
    return True, f"Started **{league}** with {added} {noun} for {span}. {nudge}"


__all__ = [
    "AllianceModal",
    "KnownModal",
    "NewLeagueModal",
    "ScoreModal",
    "ScoutActionsView",
    "generate_next_week",
    "pending_new_league",
    "pending_next_week",
    "save_rows",
    "start_new_league",
    "target_day",
]


# ── Push / Save declaration (#407) ────────────────────────────────────────────

#: Bare, and no `primary` among them. These are alternatives inside one
#: question, which the DESIGN.md rule says go without glyphs, and the bot has
#: no opinion about whether an alliance should spend or bank its resources.
VS_BTN_DECLARE = "✏️ Declare this week"
VS_BTN_PUSH = "Push to win"
VS_BTN_SAVE = "Save for a later week"
VS_BTN_CLEAR_INTENT = "Clear the declaration"
VS_BTN_ANNOUNCE = "📣 Tell members"

#: How a recorded intent reads back. The sheet stores the code; nothing else
#: should spell these out, so a rename stays here.
INTENT_WORDS = {
    ad.INTENT_PUSH: "pushing to win",
    ad.INTENT_SAVE: "saving for a later week",
    ad.INTENT_NONE: "undeclared",
}


def declaration_embed(state, week: int) -> discord.Embed:
    """What is declared for `week`, and what saving it would actually cost.

    The cost is the point of the surface. Week 1 carries more weight than every
    later week combined (8 against 4+2+1), so its winners and losers separate
    into two cohorts that never re-merge: a save in week 1 is not a neutral
    resource decision, it fixes which half of the league you spend the season
    in. The lineage walk already computes that, so this answers "who do we end
    up facing?" outright rather than leaving leadership to infer it from a
    warning.
    """
    embed = discord.Embed(
        title=f"Week {week}: push or save?",
        color=discord.Color.blurple(),
    )

    row = state.row_for(state.own, week) if state.own else None
    declared = row.intent if row else None
    if declared and declared != ad.INTENT_NONE:
        embed.description = f"You have this week recorded as **{INTENT_WORDS[declared]}**."
    else:
        embed.description = (
            "Nothing recorded for this week yet. Declaring it puts the call on "
            "your sheet, so a week that was lost on purpose still reads that "
            "way months later."
        )

    consequence = _save_consequence(state, week)
    if consequence:
        embed.add_field(name="If you save this week", value=consequence[:1024], inline=False)

    # The announce button hangs off the members' channel, which belongs to the
    # day theme reminder. With none saved the button is absent, so the footer
    # has to say why rather than leaving a control that silently never appears.
    if state.cfg.get("day_theme_channel_id"):
        embed.set_footer(text="Recorded on your own rows. Nothing is announced unless you ask.")
    else:
        embed.set_footer(
            text=(
                "Recorded on your own rows. To be able to tell members, set a channel "
                "under Day theme reminder."
            )
        )
    return embed


def _save_consequence(state, week: int) -> str:
    """The path that follows from losing `week` on purpose, in plain words.

    Returns an empty string when the bracket cannot be walked, which is the
    normal state in own-alliance tracking mode: there is no cohort to project
    without the other fifteen rows, and saying so here would repeat an upsell
    the mode question already made.
    """
    if state.own is None or state.league is None:
        return ""

    projection = ad.project_own_path(
        state.own,
        state.league_rows(),
        estimate=ad.make_estimator(state.profiles),
        assume={week: (state.own, "L")},
    )
    if isinstance(projection, ad.BracketIncomplete):
        return ""

    later = [s for s in projection.steps if s.week > week and s.opponent is not None]
    lines = []
    if week == 1:
        lines.append(
            "Week 1 decides which half of the league you spend the season in. "
            "Its winners and losers never meet again, so this one does not "
            "come back."
        )
    if later:
        route = ", ".join(f"week {s.week} {state.display_name(s.opponent)}" for s in later[:3])
        lines.append(f"You would then face {route}.")
    else:
        lines.append(
            "Who you would face after that is not worked out yet. "
            f"Open **{ad_hub_btn_path()}** to see what is blocking it."
        )
    return "\n".join(lines)


def _trends_label() -> str:
    """The Trends button's label, imported rather than retyped so a rename
    stays one line. Lazy for the same reason `ad_hub_btn_path` is: the UI
    module imports this one."""
    import alliance_duel_ui

    return alliance_duel_ui.VS_BTN_TRENDS


def ad_wizard_btn_day_theme() -> str:
    """The day theme settings button, imported lazily for the same one-way
    dependency reason as `ad_hub_btn_path`. The members' channel lives there,
    so copy about it has to name that button rather than the panel generally."""
    import alliance_duel_wizard

    return alliance_duel_wizard.VS_BTN_DAY_THEME


def ad_hub_btn_path() -> str:
    """Imported lazily so the entry module keeps its one-way dependency on the
    hub: the hub imports this module, not the other way round."""
    import alliance_duel_hub

    return alliance_duel_hub.VS_BTN_PATH


class DeclarationView(discord.ui.View):
    """Push, save, or clear, plus an optional announcement to members.

    Neither call is styled as the recommended one. Whether to spend or bank a
    week is a strategy decision belonging entirely to the alliance, and a
    `primary` button on either would read as the bot having a view about it.
    """

    def __init__(self, state, week: int, owner_id: int):
        super().__init__(timeout=ENTRY_TIMEOUT)
        self.state = state
        self.week = week
        self.owner_id = owner_id
        self.message: discord.Message | None = None
        self._render()

    def _render(self) -> None:
        self.clear_items()
        row = self.state.row_for(self.state.own, self.week) if self.state.own else None
        declared = (row.intent if row else None) or ad.INTENT_NONE

        for label, intent in (
            (VS_BTN_PUSH, ad.INTENT_PUSH),
            (VS_BTN_SAVE, ad.INTENT_SAVE),
        ):
            button = discord.ui.Button(
                label=label,
                style=discord.ButtonStyle.secondary,
                disabled=declared == intent,
                row=0,
            )
            button.callback = self._make_declare(intent)
            self.add_item(button)

        clear = discord.ui.Button(
            label=VS_BTN_CLEAR_INTENT,
            style=discord.ButtonStyle.secondary,
            disabled=declared == ad.INTENT_NONE,
            row=1,
        )
        clear.callback = self._make_declare(ad.INTENT_NONE)
        self.add_item(clear)

        # Only offered once there is something to announce and somewhere to
        # announce it. A live button that could not post anywhere would be a
        # control that cannot change anything.
        channel_id = self.state.cfg.get("day_theme_channel_id") or 0
        if declared != ad.INTENT_NONE and channel_id:
            announce = discord.ui.Button(
                label=VS_BTN_ANNOUNCE, style=discord.ButtonStyle.secondary, row=1
            )
            announce.callback = self._announce
            self.add_item(announce)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(messages.DENY_NOT_OWNER, ephemeral=True)
        return False

    async def on_timeout(self) -> None:
        import wizard_registry

        await wizard_registry.expire_view_message(self.message, command_hint="/vs")

    def _make_declare(self, intent: str):
        async def _callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)
            row = _row_for_write(self.state, self.state.own, self.week)
            row.intent = intent
            problem = await save_rows(self.state, [row])
            if problem:
                await interaction.followup.send(f"⚠️ {problem}", ephemeral=True)
                return

            self._render()
            if self.message is not None:
                try:
                    await self.message.edit(
                        embed=declaration_embed(self.state, self.week), view=self
                    )
                except discord.HTTPException:
                    pass

            if intent == ad.INTENT_NONE:
                said = f"✅ Cleared the declaration for week {self.week}."
            else:
                said = (
                    f"✅ Recorded week {self.week} as **{INTENT_WORDS[intent]}**."
                    " Members are not told unless you ask."
                )
            await interaction.followup.send(said, ephemeral=True)

        return _callback

    async def _announce(self, interaction: discord.Interaction):
        """Post the call where members will see it. Never automatic: a save is
        exactly the kind of decision leadership may want to make quietly."""
        await interaction.response.defer(ephemeral=True, thinking=True)
        row = self.state.row_for(self.state.own, self.week)
        intent = (row.intent if row else None) or ad.INTENT_NONE
        channel_id = self.state.cfg.get("day_theme_channel_id") or 0

        channel = config_health.resolve_configured_channel(
            interaction.client, self.state.guild_id, ad_setup.VS_POST_CHANNEL_SUBJECT, channel_id
        )
        if channel is None:
            await interaction.followup.send(
                "⚠️ I could not post to your members' channel. Set it again under "
                f"**{ad_wizard_btn_day_theme()}** in {ad_setup.VS_SETUP_NAV}, then open "
                f"`/vs` and click **{VS_BTN_DECLARE}** to try again.",
                ephemeral=True,
            )
            return

        try:
            await channel.send(embed=announcement_embed(self.week, intent))
        except discord.Forbidden:
            await interaction.followup.send(
                f"⚠️ I am not allowed to post in <#{channel.id}>. Give me permission "
                f"there, or pick another channel under **{ad_wizard_btn_day_theme()}** "
                f"in {ad_setup.VS_SETUP_NAV}.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(f"📣 Told members in <#{channel.id}>.", ephemeral=True)


def announcement_embed(week: int, intent: str) -> discord.Embed:
    """The member-facing half of a declaration.

    Written for someone who has no idea what a cohort is and does not need to:
    it says what to do with their own resources this week, which is the only
    part that affects them.
    """
    if intent == ad.INTENT_PUSH:
        embed = discord.Embed(
            title=f"🏆 We are going for week {week}",
            description=(
                "Spend what you have been saving. Every day this week is worth "
                "taking, so use your speedups and shards as the day themes come up."
            ),
            color=discord.Color.green(),
        )
    else:
        embed = discord.Embed(
            title=f"🏆 We are saving week {week}",
            description=(
                f"Week {week} is a deliberate hold. Bank your speedups and shards "
                "rather than spending them, and keep them for the week we go for."
            ),
            color=discord.Color.blurple(),
        )
    embed.set_footer(text="A call from your leadership.")
    return embed
