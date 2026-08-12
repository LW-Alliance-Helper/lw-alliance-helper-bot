"""Alliance Duel (VS) guided setup and sheet rendering (#399).

Creates the `Alliance Duel (VS)` tab, walks leadership through the one
question that cannot be inferred, explains the columns with a worked example,
and renders the "Check my sheet" report.

Split from `alliance_duel.py` on the same seam as `transfer.py` /
`transfer_setup.py`: the pure data layer stays Discord-free and this module
owns everything that renders. The embed builders here are deliberately
**pure functions returning `discord.Embed`**, so the copy is unit-testable
without an interaction, a guild or a network call.

Conventions this follows, per `UX.md` and `DESIGN.md`:

- **Warzone, not "server".** `server` means the Discord server product-wide.
  Players say "server" colloquially, so the column guide says so once.
- Officer surfaces are ephemeral; `blurple()` unless reporting an outcome.
- Every dead end names the exact route back, button included.
- Findings are questions where the data might be right, statements where it
  cannot be.
"""

from __future__ import annotations

import logging

import discord

import alliance_duel as ad
import config_health
from setup_hub import HUB_BTN_VS

logger = logging.getLogger(__name__)


#: The alliance's VS tab, as a thing that can break. Registered at import
#: time, per the pattern in `train.py` and `transfer_cog.py`: a renamed tab, a
#: deleted spreadsheet or revoked access is the alliance's to fix, and the
#: normal state of a sheet they own, so it gets reported as a fixable
#: condition rather than failing silently or paging Sentry.
VS_SHEET_SUBJECT = "vs.sheet"

config_health.register(
    config_health.Subject(
        key=VS_SHEET_SUBJECT,
        label="your Alliance Duel (VS) tab",
        fix_hub="/setup",
        fix_btn=HUB_BTN_VS,
    )
)


#: Where to send someone whose VS setup needs revisiting. One constant so the
#: route stays identical across timeouts, gates and validation copy.
VS_SETUP_NAV = f"`/setup` → **{HUB_BTN_VS}**"

#: How many findings the report lists before it stops. A 64-row sheet with a
#: systematic mistake can produce hundreds, which would blow the 4096-char
#: description limit and, worse, bury the first one worth fixing. Chosen so
#: the embed stays readable on a phone.
MAX_FINDINGS_SHOWN = 15


# ── Tracking mode ─────────────────────────────────────────────────────────────

#: Asked at setup, never inferred. Skeleton generation is a *write*, so the
#: shape has to be known before there is any data to infer it from (#448).
TRACKING_MODE_QUESTION = (
    "Do you want to track just your own alliance week to week, or your entire League bracket?"
)

# Bare, no emoji. These two are alternatives to each other inside one
# question, not features or actions. Users navigate by icon, so two glyphs
# that mean the same kind of thing cost scan time and return nothing, and a
# repeated glyph is worse than none. Matches the export/import choice
# cluster (Keep current / Skip / Use exported). Feature and action buttons
# still take emoji; buttons answering one question do not.
MODE_BTN_OWN = "Just my alliance"
MODE_BTN_FULL = "My whole League bracket"


def tracking_mode_embed() -> discord.Embed:
    """The mode question, with the upsell sitting on it.

    The bracket is exactly what buys My Path and the scouting priority list,
    so this is the honest place to say what the fuller option unlocks. It is
    said **once**, here, at the moment the choice is actually being made.
    Bracket-dependent views later show what they need rather than nagging.

    Own-alliance is presented as a supported shape, not a lesser one, because
    it is: it still records every score and outcome, still builds head to head
    history, and still produces the strongest and weakest days read.
    """
    embed = discord.Embed(
        title="🏆 Alliance Duel (VS): what do you want to track?",
        description=(
            f"{TRACKING_MODE_QUESTION}\n\n"
            "You can change this at any time, and switching to the full "
            "bracket later offers to fill in the rows you skipped."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name=f"{MODE_BTN_OWN}",
        value=(
            "Your rows only. Records every day score, week score and outcome, "
            "your head to head history against anyone you actually face, and "
            "your strongest and weakest days."
        ),
        inline=False,
    )
    embed.add_field(
        name=f"{MODE_BTN_FULL}",
        value=(
            "All 16 alliances. Everything above, plus your projected path "
            "through the bracket, who you are likely to face next, and which "
            "alliances to scout first."
        ),
        inline=False,
    )
    embed.set_footer(text="Setup takes one sitting off the in-game bracket screen.")
    return embed


def mode_label(tracking_mode: str) -> str:
    """How the alliance's current mode reads back to them."""
    return (
        "just your alliance"
        if tracking_mode == ad.MODE_OWN_ALLIANCE
        else "your whole League bracket"
    )


# ── The sheet tab ─────────────────────────────────────────────────────────────


def ensure_tab(spreadsheet, tab_name: str = "Alliance Duel (VS)"):
    """Return the VS worksheet, creating it with headers if it is absent.

    Header seeding goes through `config.get_or_create_worksheet` so the tab
    matches every other bot-created tab. Columns resolve by name afterwards,
    so a user who reorders or inserts columns keeps working.
    """
    import config

    return config.get_or_create_worksheet(
        spreadsheet,
        tab_name,
        header_row=list(ad.SHEET_COLUMNS),
        rows=200,
        cols=len(ad.SHEET_COLUMNS) + 4,
    )


def load_rows(guild_id: int, tab_name: str = "Alliance Duel (VS)"):
    """Read and parse the guild's VS tab, or ``None`` if it can't be reached.

    Every VS read goes through here so the sheet-health reporting happens in
    exactly one place. On a clean read the subject clears with a recovery
    line; on a failure the alliance is told what broke and which surface fixes
    it, and nothing is Sentry-captured when the cause is theirs to fix
    (`config.is_user_config_sheet_error`, #285/#286).

    Returns ``None`` rather than raising or returning ``[]``, because "we
    couldn't read your sheet" and "your sheet is empty" are different states
    and a caller that conflates them would render an empty bracket as fact.
    """
    import config

    try:
        spreadsheet = config.get_spreadsheet(guild_id)
        worksheet = ensure_tab(spreadsheet, tab_name)
        values = worksheet.get_all_values()
    except Exception as e:  # noqa: BLE001 - classified by config_health
        recorded = config_health.record_sheet_failure(guild_id, VS_SHEET_SUBJECT, e, tab=tab_name)
        logger.warning(
            "[VS] sheet read failed for guild=%s tab=%s: %s",
            guild_id,
            tab_name,
            config.describe_sheet_error(e, guild_id=guild_id, tab=tab_name),
        )
        if not recorded:
            raise
        return None

    config_health.clear(guild_id, VS_SHEET_SUBJECT)
    return ad.parse_rows(values)


def column_guide_embed(tracking_mode: str = ad.MODE_FULL_BRACKET) -> discord.Embed:
    """Explain the tab with a worked example.

    Grouped by who fills each column and when, rather than by sheet order,
    because "what do I type and when" is the question being asked. Sheet order
    is visible in the sheet.
    """
    embed = discord.Embed(
        title="🏆 Alliance Duel (VS): filling in your tab",
        description=(
            "One row per alliance per league week. You type the values; the "
            "bot reads them back as your bracket, projections and history.\n\n"
            "Columns are found by **name**, so you can reorder them or add "
            "your own."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="Once per league, off the bracket screen",
        value=(
            f"**{ad.COL_SEASON}** `S35` · **{ad.COL_TIER}** `Diamond` · "
            f"**{ad.COL_GROUP}** `12 - 2`\n"
            f"**{ad.COL_SEED}** `1` to `16`, fixed for the whole league.\n"
            f"**{ad.COL_TAG}** and **{ad.COL_WARZONE}** identify an alliance. "
            "Warzone is the game's word for the world an alliance plays in "
            "(you may call it the server)."
        ),
        inline=False,
    )
    embed.add_field(
        name="Whenever you scout",
        value=(
            f"**{ad.COL_POWER}**, **{ad.COL_MEMBERS}**, **{ad.COL_GIFT_LEVEL}**. "
            "The latest value you fill in wins, so you only re-enter these "
            "when they change. Power takes shorthand: `301` means 301M."
        ),
        inline=False,
    )
    embed.add_field(
        name="As the week runs",
        value=(
            f"**{ad.day_score_col(1)}** to **{ad.day_score_col(6)}** are the raw "
            "in-game points, for your own matchups. Type the full number or "
            "use a unit (`500m`, `1.2b`); a bare `500` means five hundred.\n"
            f"**{ad.day_outcome_col(1)}** to **{ad.day_outcome_col(6)}** are "
            "`W` or `L`. "
            f"**{ad.COL_WEEK_SCORE}** is your league points out of 13."
        ),
        inline=False,
    )
    embed.add_field(
        name="Your reads, whenever you have one",
        value=(
            f"**{ad.COL_KNOWN_1_5}** and **{ad.COL_KNOWN_6}** hold your own "
            "judgement of an alliance: "
            f"`{'` / `'.join(ad.KNOWN_SCALE)}`.\n"
            f"**{ad.COL_PICKED}** is your call on one week's match. "
            f"**{ad.COL_NOTES}** is free text."
        ),
        inline=False,
    )
    if tracking_mode == ad.MODE_OWN_ALLIANCE:
        embed.add_field(
            name="📌 You're tracking just your alliance",
            value=(
                "So you only need your own rows and the opponent you face. "
                "Everything above still applies to them."
            ),
            inline=False,
        )
    embed.set_footer(text=f"Run /setup → {HUB_BTN_VS} to change any of this.")
    return embed


# ── Validation report ─────────────────────────────────────────────────────────


def _finding_line(finding: ad.Finding) -> str:
    """One finding as a bullet. Location first, because the reader is about to
    go and look at it."""
    icon = "⚠️" if finding.severity == ad.SEVERITY_WARNING else "❌"
    where = finding.where or "your sheet"
    return f"{icon} **{where}**: {finding.message}"


def validation_report_embed(
    findings: list[ad.Finding],
    *,
    tracking_mode: str = ad.MODE_FULL_BRACKET,
    rows_checked: int = 0,
) -> discord.Embed:
    """Render "Check my sheet".

    Clamped to :data:`MAX_FINDINGS_SHOWN`. One systematic mistake across a
    64-row league produces hundreds of findings, which would exceed the
    4096-character description limit and bury the first one worth fixing.
    The count is always honest about how many were left out.

    A clean sheet is reported in green and says what was checked, so the
    officer knows the check ran rather than wondering if it did.
    """
    if not findings:
        embed = discord.Embed(
            title="✅ Your sheet looks right",
            description=(
                f"Checked {rows_checked} row{'s' if rows_checked != 1 else ''} "
                "and found nothing to fix."
            ),
            color=discord.Color.green(),
        )
        if tracking_mode == ad.MODE_OWN_ALLIANCE:
            embed.set_footer(
                text=(
                    "Tracking just your alliance, so bracket checks "
                    "(reciprocal opponents, seeds) were skipped."
                )
            )
        return embed

    errors = [f for f in findings if f.severity == ad.SEVERITY_ERROR]
    warnings = [f for f in findings if f.severity == ad.SEVERITY_WARNING]

    shown = findings[:MAX_FINDINGS_SHOWN]
    lines = [_finding_line(f) for f in shown]
    hidden = len(findings) - len(shown)
    if hidden:
        lines.append(
            f"\n…and {hidden} more. Fixing the ones above often clears several "
            "at once, so check again afterwards."
        )

    summary = []
    if errors:
        summary.append(f"{len(errors)} thing{'s' if len(errors) != 1 else ''} to fix")
    if warnings:
        summary.append(f"{len(warnings)} worth a look")

    embed = discord.Embed(
        title="⚠️ Your sheet needs a look",
        description=f"{', '.join(summary)}.\n\n" + "\n".join(lines),
        color=discord.Color.orange() if not errors else discord.Color.red(),
    )
    # Description has a hard 4096 cap and alliance-supplied tags ride in these
    # messages, so clamp rather than trust the finding count alone.
    if len(embed.description) > 4000:
        embed.description = embed.description[:3990] + "\n…"
    embed.set_footer(text="Fix these in your sheet, then run the check again.")
    return embed


def fill_bracket_embed(league: ad.LeagueKey, missing: dict) -> discord.Embed:
    """Offer the blank rows after a switch to full-bracket tracking (#448).

    States plainly what the rows will and will not contain. They carry the
    league identity and week, and nothing else: the bot has no way to know who
    the other alliances are, so tag, warzone and seed still come off the
    in-game bracket screen. Saying so here stops the offer reading as though
    the bot is about to fill the bracket in for them.
    """
    total = sum(count for count, _stamp in missing.values())
    weeks = ", ".join(f"week {w}" for w in sorted(missing))
    embed = discord.Embed(
        title="Add the rest of your bracket?",
        description=(
            f"You're now tracking your whole League bracket, but **{league}** has "
            f"{total} row{'s' if total != 1 else ''} missing across {weeks}.\n\n"
            "I can add them as blank rows, already stamped with the season, tier, "
            "group, week and date. You fill in the tag, warzone and seed for each "
            "from the in-game bracket screen."
        ),
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="Nothing is removed either way, and you can add them later.")
    return embed


def upsell_embed(reason: ad.BracketIncomplete) -> discord.Embed:
    """What a bracket-dependent view shows when the bracket isn't there.

    Two genuinely different messages, decided by `reason.is_choice`:

    - **You chose not to track this.** Say what the fuller option would give
      and how to switch. No warning colour, no error framing, and it does not
      repeat itself across views.
    - **The data is missing.** That one should prompt action, so it says what
      is absent and where to put it.
    """
    if reason.is_choice:
        return discord.Embed(
            title="🏆 This view needs the full bracket",
            description=(
                "You're tracking just your alliance, so there's no bracket to "
                "project through. Tracking all 16 alliances adds your "
                "projected path, who you're likely to face next, and which "
                "alliances to scout first.\n\n"
                f"Switch any time from {VS_SETUP_NAV}. Doing it mid league "
                "offers to fill in the rows you skipped."
            ),
            color=discord.Color.blurple(),
        )
    return discord.Embed(
        title="⚠️ Not enough of the bracket recorded yet",
        description=(
            f"{reason.detail}\n\n"
            "Add the missing alliances to your tab and this view fills in. "
            f"The column guide is in {VS_SETUP_NAV}."
        ),
        color=discord.Color.orange(),
    )
