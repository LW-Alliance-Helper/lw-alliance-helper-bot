"""`/champion_duel` — the hub, and every flow its buttons drive.

Champion Duel is an off-season event alliances bet on through Predict & Win.
This is the surface a member reaches it from: ask for a matchup's odds, look a
registrant up, contribute a sighting, and — for the operator — browse, revert
and export the edit history.

The command name is `champion_duel`, never `duel`. Champion Duel, Warzone Duel
and Alliance VS Duel are three different events, and `/vs` already owns the
third — a bare `/duel` would be ambiguous the day the second one ships.

**Why a hub rather than subcommands.** A Discord command cannot be both a group
and a bare command, and the useful thing to type is `/champion_duel`: a member
who wants odds should not have to already know that the word after it is
`predict`. The admin tools that used to be `/champion_duel edits|revert|export`
are the same flows, now behind buttons.

**Admin buttons are hidden, not disabled**, which is the opposite of the
Premium rule in `notes/DESIGN.md`. Premium controls render disabled so the free
tier can see the shape of the paid product — that is a sales surface. This is
not: `CHAMPION_DUEL_ADMIN_IDS` is an operator env var, so the design's stated
exception ("hiding is reserved for surfaces behind a deploy flag") is exactly
what this is. Showing an alliance member a greyed-out "Revert an edit" would
advertise a surface no amount of paying gets them.

**Contributing is not gated, and the odds are.** Reversed 2026-08-17; the
reasoning is in `notes/DESIGN_champion_duel_premium.md`. Every other gated
feature produces value for the alliance that uses it, but Champion Duel
contributions produce value for everyone, so gating them means fewer
predictions for paying alliances too. Free alliances are the collection engine.
Every write is attributed and revertable, so the blast radius is bounded.

`🔮 Odds of advancing` is the one Premium control here, and it does follow the
Premium rule: disabled and 🔒 on the free tier, with the upsell on the embed.

`can_write` survives as a parameter and nothing sets it False. It is left
threaded rather than ripped out because its 🔒-and-disable rendering is the
shape any later gate reuses, and the odds gate proved that shape works. Read
the padlock branches as unreachable today, not as a live gate.
"""

from __future__ import annotations

import asyncio
import csv
import io
import os
from datetime import datetime, timezone

import discord

import champion_duel_db as db
import champion_duel_image
import champion_duel_intel as intel_lib
import champion_duel_odds as odds_lib
import champion_duel_predict as predict_lib
import champion_duel_wording as words
import premium
from api.champion_duel_auth import admin_ids
from messages import (
    CANCEL_BACKPEDAL,
    CANCEL_PLAIN,
    COMMUNITY_SERVER_NAME,
    COMMUNITY_SERVER_URL,
    DATE_PARSE_REJECT,
)

CHAMPION_DUEL_HUB_TITLE = "👑 Champion Duel"
CHAMPION_DUEL_HUB_CMD = "/champion_duel"

# Feature + action labels. Constants per the HUB_BTN_* convention: other
# modules name these buttons in prose, so a rename has to stay one line.
HUB_BTN_CHAMPION_DUEL = "👑 Champion Duel"
CD_BTN_PREDICT = "🆚 Predict a match"
CD_BTN_FIND = "🔍 Find a player"
CD_BTN_ADD = "➕ Add a player"
# The only squad entry screen. One open takes all three squads, their types
# and the purity answer, and every box is optional so a partial reading is one
# press rather than three.
#
# It replaced a second control, `✏️ Correct a squad`, which took one slot at a
# time. The two did the same job from the user's end and sat side by side under
# the same glyph, which `DESIGN.md` forbids across a choice set: two identical
# glyphs give the eye nothing to navigate by. Retired 2026-08-17 (Kevin).
#
# The one thing the retired control could express and a fixed permutation
# cannot is a lineup running two of the same type, which is about 4% of
# players. `_TYPE_ORDER_OTHER` covers them instead.
CD_BTN_SQUADS = "✏️ Record their squads"
CD_BTN_ORDER = "➕ Record a line-up"
CD_BTN_GUIDE = "📖 Where to find these numbers"
CD_BTN_EDITS = "📜 Recent edits"
CD_BTN_REVERT = "⏪ Revert an edit"
CD_BTN_EXPORT = "📤 Export edits"
CD_BTN_FILTER = "🔍 Filter these"
CD_BTN_SHARE = "📤 Share this prediction to current channel"
CD_BTN_SET_WARZONE = "⚙️ Set your warzone"
CD_BTN_CHANGE_WARZONE = "✏️ Change your warzone"
CD_BTN_ADD_GROUPING = "➕ Add your Participating Warzones"
CD_BTN_RETRY_GROUPING = "✏️ Edit and try again"
# 📥 is the catalog's "data coming into the bot", which is what a pasted group
# listing is. Not ➕: `CD_BTN_ADD` already carries that on this grid, and two of
# one glyph side by side give the eye nothing to navigate by.
# 🏅 is the game's own mark for a standing: its Ranking line carries a medal
# badge, so `DESIGN.md` rule 5 (borrow the game's iconography) has something to
# take here. It is also legible at button size, which 📇 was not -- Kevin could
# not identify that glyph at 200% zoom, and an icon nobody can read is doing
# none of the scanning work an emoji is on a label to do.
# 👥 was the obvious choice and is Member Sync's, which rule 3 puts out of reach.
CD_BTN_GROUP = "🏅 Your group"
# Deliberately not "prediction". The game runs its own prediction, and it is a
# betting market on individual matches (Kevin, 2026-08-16). This answers a
# question that one does not: whether you get out of your group.
#
# 🔮 for the thing being predicted (Kevin, 2026-08-16). 📊 was out as Growth
# Breakdown's, and 🎲 was the trap: the game's own prediction *is* a betting
# market, so a die would say we are that feature on the one surface where the
# distinction matters most. A crystal ball says forecast without saying wager.
# Not "register for a round". A player registered for this Champion Duel in
# the game weeks ago; what is missing is where the draw put them, which is a
# fact somebody read off a screen. A label claiming registration would describe
# an outcome the control does not have, which `UX.md` puts the wrong way round:
# the label describes the control, and this one sets a group.
#
# 🏅 rather than a new glyph. `DESIGN.md` has it as one player's standing in a
# round, and which group they are in is exactly that, so this and the group
# view share a mark because they share a meaning.
CD_BTN_PLACE = "🏅 Set their group"
CD_BTN_ODDS = "🔮 Odds of advancing"
# Named 2026-08-23, Kevin. The surface takes two players and both are now
# required, so it is a head-to-head in fact and the label says so. It stopped
# being "Counter a player" for the reason the placeholder was always at risk
# of: that phrasing made a family with `🔍 Find a player` and `➕ Add a player`,
# and the family was the problem rather than the point — three labels of the
# same shape, two of which sound like looking somebody up. "Head to head" names
# the one thing this control does that neither of the others can, which is put
# two named players against each other.
#
# Earlier candidates, kept because the reasoning outlived them: "What to field
# against them", "Plan against a player", "Read an opponent". All three describe
# advice given to one caller about one opponent, which is the shape the surface
# had when the second name was optional.
#
# 🎯 was the obvious glyph from the start and was ruled out only because
# three other senses held it — `events_hub`'s Pick a preset,
# `storm_roster_builder`'s Auto-fill and `transfer_setup`'s "Is one of specific
# values". All three cleared in the 2026-08-23 consolidation (#525): they took
# 📋, ✨ and 🔽. The glyph is unused anywhere else in the bot, it is legible at
# button size, and taking aim at one named opponent is what it means.
#
# The alternatives it beat, and why each was unavailable rather than merely
# worse: ⚔️ is Desert Storm's feature glyph (rules 3 and 4), 🔍 is Find and is
# the exact confusion this feature has to avoid, 🔮 is the odds, 📋 is
# transfer_setup's Decisions, 🗡️ collides with ⚔️ at button size, and ♟️ is
# unreadable there — which is what retired 📇. 🏹 carried the placeholder and
# is now free again.
CD_BTN_INTEL = "🎯 Head to head"
CD_BTN_RECORD = "📥 Record a group"
CD_BTN_SAVE_GROUP = "✅ Save group"
CD_BTN_LINE_NEW = "➕ Add as a new player"
CD_BTN_LINE_SKIP = "⏭️ Skip this line"
CD_BTN_LINE_BACK = "Back"
# 💬 borrowed from the website's own link to the same place, per `notes/DESIGN
# .md` emoji rule 5: somebody who has seen one should recognise the other.
CD_BTN_COMMUNITY = f"💬 {COMMUNITY_SERVER_NAME}"

# Confirm pairs go bare (`notes/DESIGN.md`, emoji rule 7): the two halves differ
# by answer, not by kind, so any glyph would be the same one twice.
CD_BTN_WARZONE_YES = "Yes, that's us"
CD_BTN_WARZONE_NO = "No, change it"
CD_BTN_CHANGE_YES = "Yes, change it"
CD_BTN_CANCEL = "Cancel"

# Discord's message limit is 2000 and an embed description is 4096. Keep the
# browse list well inside both, since the export exists for volume.
BROWSE_MAX = 20

# Servers named individually before the list stops being scannable. These are
# bare numbers now, so far more fit on a line than when each carried a count;
# the cap is here so a future stage with hundreds cannot turn the hub into a
# wall, not because sixteen is close to the limit.
_SERVERS_SHOWN = 30
#: Rows on the odds embed. A qualifier group is 100 players and an embed
#: description is 4,096 characters, so the list is cut and the remainder
#: counted. Eight is what advances from a qualifier group, so the cut sits
#: just past the line that decides the round.
_ODDS_SHOWN = 12

#: Troop levels the game has. Only 10 and 11 are measured; the rest carry the
#: same step down, which `champion_duel_engine.scoring.MEASURED_LEVELS` will
#: confirm. Levels only separate players in a mixed-level group: where everyone
#: is the same, ranking is unaffected.
MAX_TROOP_LEVEL = 11

# The six deployment orders. Every line-up observed to date runs exactly one
# Tank, one Missile and one Aircraft, so an order is a permutation of the three
# and the whole space fits in one select — which is the point of offering it
# that way. Three separate type pickers would let someone build "Tank, Tank,
# Missile", and the only thing left to do with that is reject it after the
# fact.
ORDERS = [
    ("Tank", "Missile", "Aircraft"),
    ("Tank", "Aircraft", "Missile"),
    ("Missile", "Tank", "Aircraft"),
    ("Missile", "Aircraft", "Tank"),
    ("Aircraft", "Tank", "Missile"),
    ("Aircraft", "Missile", "Tank"),
]

_DENY_NOT_OWNER = "⛔ Only the person who opened this hub can use these buttons."
_ENGINE_MISSING = (
    "⚠️ The Champion Duel engine isn't installed on this bot, so predictions and "
    "player look-ups are unavailable. If you're the bot operator, check that "
    "`CD_ENGINE_TOKEN` is set and the last deploy installed `champion-duel-engine`."
)


# ⚠️ COPY AWAITING SIGN-OFF (2026-08-23). "I", not "we": this is the bot
# unable to act on what it was given, not a statement about what the record
# holds. Names the field rather than the rule — "both names are required"
# describes the form, "add your own name" is the thing to go and do.
_INTEL_NEEDS_BOTH = (
    "⚠️ I need both players for a head to head. Open it again and fill in both names."
)


def _is_admin(user_id: int) -> bool:
    return str(user_id) in admin_ids()


def _btn_words(label: str) -> str:
    """A button's label without its leading icon, for naming it in prose.

    An emoji that reads fine on a button's grey surface does not always survive
    an embed's dark background: `➕` is U+2795 HEAVY PLUS SIGN, which Discord
    renders near-black and which therefore vanishes mid-sentence, leaving a gap
    where the icon should be. Prose names the button by its words and lets bold
    carry the emphasis.

    Derived from the constant rather than retyped, so the module's rule that a
    button rename stays one line still holds.
    """
    head, _, rest = label.partition(" ")
    return rest if rest and not head[:1].isascii() else label


def _server_sort(row: dict):
    """Numeric order, with anything unparseable last and alphabetical.

    A registrant's server is free text on the self-reported path, so this
    cannot assume digits: sorting has to place `abc` somewhere rather than
    raise on it in the middle of rendering the hub.
    """
    server = str(row.get("server") or "")
    return (0, int(server), "") if server.isdigit() else (1, 0, server)


def _actor(interaction: discord.Interaction) -> dict:
    """The actor dict every write is attributed to.

    `guild_id` rides along because it is the join key to Map Manager's
    `discord_guild_links` table — without it a ported edit could not be
    attributed to an alliance.
    """
    return {
        "discord_user_id": str(interaction.user.id),
        "discord_name": interaction.user.display_name,
        "guild_id": str(interaction.guild_id) if interaction.guild_id else None,
    }


def _parse_day(value: str, *, end_of_day: bool) -> str | None:
    """Accept YYYY-MM-DD and widen it to cover the whole day.

    Timestamps are stored as ISO-8601 UTC text and compared lexicographically,
    so an inclusive end needs the day's last instant rather than midnight —
    otherwise an export of `2026-08-12` to `2026-08-12` silently returns
    nothing, which reads as "no edits that day" instead of "you asked for a
    zero-width range".
    """
    try:
        day = datetime.strptime(value.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return None
    if end_of_day:
        day = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return day.isoformat()


def _describe(edit: dict) -> str:
    """One edit as a line. Shows the actor as a mention so an unfamiliar
    snowflake resolves to a person without a second lookup, and the server
    alongside the name because two servers can field the same name.

    The actor can be gone: a data removal scrubs `actor_discord_id` and leaves
    the edit. Formatted unconditionally, that printed a bare `<@>`, which is
    not a mention and reads as a rendering bug. Falls back to the same
    "(unknown)" this function already uses for a missing name."""
    who = f"<@{edit['actor_discord_id']}>" if edit.get("actor_discord_id") else "(unknown)"
    when = (edit.get("created_at") or "")[:16].replace("T", " ")
    what = edit.get("field") or edit.get("target")
    slot = f" slot {edit['slot']}" if edit.get("slot") else ""
    old, new = edit.get("old_value"), edit.get("new_value")
    change = f"{old or '(none)'} → {new or '(none)'}"
    tail = f"  ↩ revert of #{edit['revert_of']}" if edit.get("revert_of") else ""
    name = edit.get("display_name") or "(unknown)"
    server = f" (#{edit['server']})" if edit.get("server") else ""
    return f"`#{edit['id']}` **{name}**{server}{slot} {what}: {change} · {who} · {when}{tail}"


_POWER_SUFFIXES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}

# Below this, a suffix-less power was meant as millions. See `parse_power`.
_POWER_BARE_IS_MILLIONS = 1_000


def parse_power(text: str) -> float | None:
    """A squad power in whatever form the game showed it, or None.

    The game writes `84.6M`. A spreadsheet writes `84,600,000`. Both are the
    same number and neither is the user's mistake to fix — refusing one of them
    only moves arithmetic from the machine to the person reading a screen.

    Deliberately narrow about what it accepts: digits, one optional decimal
    point, separators, and a single k/m/b. Anything else returns None rather
    than a guess, because a squad power that is silently wrong by 1000x
    produces a confident prediction for a line-up nobody can field.
    """
    if text is None:
        return None
    cleaned = str(text).strip().lower().replace(",", "").replace(" ", "")
    if not cleaned:
        return None

    multiplier = 1
    if cleaned[-1] in _POWER_SUFFIXES:
        multiplier = _POWER_SUFFIXES[cleaned[-1]]
        cleaned = cleaned[:-1]

    try:
        value = float(cleaned)
    except ValueError:
        return None
    if value <= 0:
        return None

    # A bare number small enough to be a squad power only if it were millions
    # is one: the game prints `84.6M`, and someone copying that off a screen
    # drops the M more often than not. Taken literally it stored 81.9 and
    # rendered "82", which reads as the bot ignoring what they typed.
    #
    # The boundary is 1,000 rather than 1,000,000 so the guess only covers
    # what the game actually displays. Nothing between 1,000 and 1,000,000 is
    # a plausible squad power in either reading, so it is left alone rather
    # than multiplied into something absurd.
    if multiplier == 1 and value < _POWER_BARE_IS_MILLIONS:
        multiplier = _POWER_SUFFIXES["m"]

    return value * multiplier


def _label(player: dict) -> str:
    """A player as `Name (#738)`. The server is never dropped: identity is
    (name, server), and two servers fielding the same name is normal."""
    server = player.get("server")
    return f"{player.get('display_name')}" + (f" (#{server})" if server else "")


def _ambiguous_msg(exc: db.AmbiguousPlayer) -> str:
    """Ask which one rather than picking. Attaching a sighting to the wrong
    player is not recoverable, and the person typing is in a position to say."""
    servers = ", ".join(f"`{c['server'] or '?'}`" for c in exc.candidates)
    return (
        f"⚠️ **{exc.name}** is registered on more than one server ({servers}).\n"
        f"Run it again with the server number so the data lands on the right player."
    )


async def _suggestion_line(name: str, server: str | None) -> str:
    """ "Did you mean…", or nothing if we have no near match.

    Suggesting is not resolving. `normalize_name` refuses to fuzzy-match
    because guessing which of two similar names a sighting belongs to is
    unrecoverable — but the person typing can tell instantly, and "no registrant
    matches" tells them nothing about whether they mistyped or we are missing
    the player entirely.
    """
    candidates = await asyncio.to_thread(db.suggest_registrants, name, server)
    if not candidates:
        return ""
    named = ", ".join(
        f"**{c['display_name']}** on {c['server']}" if c["server"] else f"**{c['display_name']}**"
        for c in candidates
    )
    return f"\nDid you mean {named}?"


async def _resolve(name: str, server: str | None) -> dict | str:
    """One player with their scouting, or an error string ready to send."""
    if not db.NAMES_AVAILABLE:
        return _ENGINE_MISSING
    try:
        found = await asyncio.to_thread(db.get_player, name, server, True)
    except db.AmbiguousPlayer as exc:
        return _ambiguous_msg(exc)
    if found is None:
        return (
            f"⚠️ No registrant matches **{name}**"
            + (f" on server {server}" if server else "")
            + "."
            + await _suggestion_line(name, server)
        )
    return found


# ── Predict ───────────────────────────────────────────────────────────────────


def _bar(p: float, width: int = 20) -> str:
    """A probability as a filled bar. On a phone the bar is read before the
    number is, and it makes a near-coin-flip look like one."""
    filled = max(0, min(width, round(p * width)))
    return "█" * filled + "░" * (width - filled)


def _lineup(side: predict_lib.SideInput) -> str:
    """One side's line-up, in the order the prediction assumed.

    Not the natural slot order when the two differ: deployment order decides
    which squad meets which, and the counter triangle means it can outweigh
    power. Rendering one order beside a probability computed from another is
    how a reader talks themselves out of a correct prediction.
    """
    lineup, _from_sightings = side.likely_order()
    lines = [
        f"{i}. {squad_type} · {power:,.0f}" for i, (power, squad_type) in enumerate(lineup, start=1)
    ]
    return "\n".join(lines) + f"\n*{words.lineup_summary(side)}*"


def build_prediction_embed(result: predict_lib.Prediction) -> discord.Embed:
    """The prediction as an embed.

    Blurple, not green-for-the-winner: the bot does not know which of the two
    the reader is rooting for, and colouring by "who is ahead" would encode a
    judgement it has no basis for. Same rule that keeps it from grading members.
    """
    a, b = result.a, result.b
    a_label = _label({"display_name": a.name, "server": a.server})
    b_label = _label({"display_name": b.name, "server": b.server})
    # Clamped as one string, not per half: player names are user-supplied and
    # two long ones together are what pushes a title past Discord's 256.
    embed = discord.Embed(
        title=f"🆚 {a_label} vs {b_label}"[:256],
        description=(
            f"**{a.name}** {words.probability(result.p_a)}\n`{_bar(result.p_a)}`\n"
            f"**{b.name}** {words.probability(result.p_b)}\n`{_bar(result.p_b)}`"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name=a.name[:256], value=_lineup(a)[:1024], inline=True)
    embed.add_field(name=b.name[:256], value=_lineup(b)[:1024], inline=True)
    embed.add_field(
        name=f"{words.CONFIDENCE_LABEL}: {result.confidence().capitalize()}",
        value=words.EVIDENCE_COPY[words.evidence(a, b)],
        inline=False,
    )
    embed.set_footer(
        text=(
            "Exact odds over both players' recorded orders, no sampling. "
            "Record a sighting to sharpen it."
        )
    )
    return embed


def prediction_caption(result: predict_lib.Prediction) -> str:
    """The prediction in one line of text.

    The card carries it visually, but the line is what survives a screen
    reader, a failed image load, and Discord's own search — none of which can
    read a PNG.
    """
    a, b = result.a, result.b
    return (
        f"🆚 **{a.name}** {words.probability(result.p_a)} · "
        f"**{b.name}** {words.probability(result.p_b)} "
        f"({words.CONFIDENCE_LABEL}: {result.confidence().capitalize()})"
    )


class SharePredictionView(discord.ui.View):
    """Lets the person who asked repost the card visibly to this channel.

    Follows `member_stats.SharePowerView`: the same 📤, the same "to this
    channel" phrasing, the same disable-after-use. Posting is opt-in and
    user-initiated rather than the bot deciding a prediction is public —
    the ephemeral default holds until someone chooses otherwise.

    No `interaction_check`: the message this hangs off is ephemeral, so the
    only person who can press it is already the only person who can see it.

    The rendered bytes are held rather than re-rendered. A second render could
    disagree with the first if a sighting landed in between, and a card that
    changes between being read and being shared is worse than the memory.
    """

    def __init__(self, *, png: bytes, caption: str, user_id: int):
        super().__init__(timeout=600)
        self.png = png
        self.caption = caption
        self.user_id = user_id
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    @discord.ui.button(label=CD_BTN_SHARE, style=discord.ButtonStyle.secondary)
    async def share(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        button.disabled = True
        await interaction.edit_original_response(view=self)
        try:
            # Posted to the channel directly: a followup to an ephemeral
            # interaction would itself be ephemeral, which is the one thing
            # this button exists to avoid.
            await interaction.channel.send(
                f"{self.caption}\n-# Shared by <@{self.user_id}>",
                file=discord.File(io.BytesIO(self.png), filename="champion_duel_prediction.webp"),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "⚠️ I can't post in this channel. I need **Send Messages** and "
                "**Attach Files** here. You can still save the image and post it yourself.",
                ephemeral=True,
            )


CARD_DEFAULT_SUBTITLE = "Matchup prediction"


def card_subtitle(a: dict | None, b: dict | None) -> str:
    """What the card calls this fixture.

    Names the round only when the card really is about that round: both players
    in it, and in the same group within it. Anything else is a matchup someone
    asked about rather than a fixture that exists, and says so.

    The two ways it falls back are worth stating, because both look like they
    ought to work:

    - **Different rounds.** One player still in, one knocked out. Captioning
      that with the live round would say they are both still in it.
    - **Same round, different groups.** They will never actually meet, so a
      "Group M" caption over two people who are not both in group M is wrong
      about the one thing the caption asserts.

    A round with no draw loaded, or a player we hold no round data for, is the
    same fallback.
    """
    stages = [db.stage_for_display(p["id"]) if p and p.get("id") else None for p in (a, b)]
    if not all(stages):
        return CARD_DEFAULT_SUBTITLE
    groups = {s["grp"] for s in stages}
    if len(groups) != 1 or None in groups:
        return CARD_DEFAULT_SUBTITLE
    label = db.STAGE_LABELS.get(stages[0]["stage"], stages[0]["stage"].title())
    return f"Group {stages[0]['grp']} · {label}"


async def _send_prediction(
    interaction: discord.Interaction,
    result: predict_lib.Prediction,
    *,
    subtitle: str | None = None,
):
    """The card, falling back to the embed if rendering fails.

    A render is more moving parts than an embed -- fonts, a logo asset, Pillow
    -- and none of them are worth losing a correct prediction over. The
    fallback is silent to the user because the numbers are identical either
    way; the exception still reaches Sentry.
    """
    try:
        png = await asyncio.to_thread(champion_duel_image.render, result, subtitle=subtitle)
    except Exception as exc:  # noqa: BLE001 - a failed render must not eat the answer
        try:
            import sentry_sdk

            sentry_sdk.capture_exception(exc)
        except ImportError:  # pragma: no cover - sentry optional in some envs
            pass
        await interaction.followup.send(embed=build_prediction_embed(result), ephemeral=True)
        return

    caption = prediction_caption(result)
    view = SharePredictionView(png=png, caption=caption, user_id=interaction.user.id)
    await interaction.followup.send(
        caption,
        file=discord.File(io.BytesIO(png), filename="champion_duel_prediction.webp"),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


class _PredictModal(discord.ui.Modal, title="Predict a Champion Duel match"):
    """Two players in, one probability out.

    Server is its own optional field rather than something parsed out of the
    name, because a name is free text a player chose and may itself contain
    digits, brackets or a hash.
    """

    player_a = discord.ui.TextInput(label="First player", max_length=64)
    server_a = discord.ui.TextInput(
        label="First player's server", required=False, max_length=10, placeholder="e.g. 738"
    )
    player_b = discord.ui.TextInput(label="Second player", max_length=64)
    server_b = discord.ui.TextInput(
        label="Second player's server", required=False, max_length=10, placeholder="e.g. 1042"
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not predict_lib.ENGINE_AVAILABLE:
            await interaction.followup.send(_ENGINE_MISSING, ephemeral=True)
            return

        sides = []
        for name, server in (
            (self.player_a.value, self.server_a.value),
            (self.player_b.value, self.server_b.value),
        ):
            found = await _resolve(name, server or None)
            if isinstance(found, str):
                await interaction.followup.send(found, ephemeral=True)
                return
            sides.append(found)

        try:
            result = await asyncio.to_thread(predict_lib.predict, sides[0], sides[1])
        except predict_lib.NotEnoughData as exc:
            slots = ", ".join(str(s) for s in exc.missing)
            await interaction.followup.send(
                f"⚠️ We don't have a full line-up for **{exc.name}**. Slot(s) {slots} "
                f"have no squad recorded, so there's nothing to predict with.\n"
                f"Run `{CHAMPION_DUEL_HUB_CMD}` → **{CD_BTN_FIND}** → "
                f"**{CD_BTN_SQUADS}** to fill them in.",
                ephemeral=True,
            )
            return

        subtitle = await asyncio.to_thread(card_subtitle, sides[0], sides[1])
        await _send_prediction(interaction, result, subtitle=subtitle)


# ── Intel and recommendations ─────────────────────────────────────────────────


def _order_text(order) -> str:
    """A deployment as the reader sets it: first squad first, arrows between.

    Types spelled out rather than initialled. T/M/A saves two lines and costs
    the reader a key to learn, and this surface already asks them to hold a
    grade and a range in their head.
    """
    return " → ".join(order)


def _intel_title(result) -> str:
    return f"{CD_BTN_INTEL.split(' ', 1)[0]} {result.them.name}"


#: The three field names, paired so the eye sorts them by the possessive.
#: Kevin's naming, 2026-08-20. THEY DO NOT CHANGE BY STATE: a heading that
#: appears and disappears costs more cohesion than it buys precision, so
#: "Your recommended line-up" stays put and the body says there is no
#: recommendation when there is not. Constants because two of them are
#: referenced from the tests and one is referenced twice here.
#:
#: `FIELD_THEIRS` IS ALSO THE PLAYER CARD'S FIELD NAME. It used to say "Most
#: common order" there and this on the intel surface, which was two names for
#: one fact. Kevin's call, 2026-08-22: same wording in both places. Shared
#: through the constant rather than typed twice, so they cannot drift again.
FIELD_THEIRS = "Their typical line-up"
FIELD_YOURS = "Your recommended line-up"
FIELD_OTHERS = "Other line-ups & winning odds"
FIELD_FIX = "What would fix this"
FIELD_ANYWAY = "Worth recording anyway"
FIELD_WORTH = "Best and worst case"


def _card_path(button: str) -> str:
    """Where a control lives, as the reader would get to it.

    Every dead end carries its exit (`UX.md`), and this surface has three of
    them now. Button labels come through `_btn_words`: `CD_BTN_ORDER` leads
    with U+2795 HEAVY PLUS SIGN, which Discord renders near-black on an embed
    and which therefore vanishes mid-sentence.
    """
    return (
        f"Run `{CHAMPION_DUEL_HUB_CMD}` → **{_btn_words(CD_BTN_FIND)}** → **{_btn_words(button)}**."
    )


def build_intel_embed(result) -> discord.Embed:
    """What they field, what to set, and how much the choice is worth.

    ORDERED BY WHAT DECIDES THE MATCH, not by what we know most about. The
    power gap leads, because it is the one thing on the surface a reader will
    get backwards: the intuition is that more scouting means a better read, and
    what actually decides whether a read is worth anything is how far apart the
    two players are. Under a 5% gap the deployment is very nearly the whole
    match; past 10% a counter has never overturned it in 39 recorded attempts.

    Then what they do, then what to set, then what they can do about it. The
    last section is the one with no equivalent anywhere else in the product and
    it is deliberately last, because it is the widest claim and it reads as
    hedging if it comes before the advice it qualifies.
    """
    embed = discord.Embed(title=_intel_title(result), color=discord.Color.blurple())
    # Decided once. Three sections below turn on it, and the whole point of the
    # grade is that a surface answering "the line-up does not decide this one"
    # should then not spend four fields ranking line-ups.
    worth_little = result.worth == intel_lib.WORTH_SETTLED

    # `worth` is always a grade and every grade has a sentence, so the lead is
    # never empty. The gap in front of it is the part that can be absent: THP is
    # a recorded column and either player can be missing it.
    lead = words.worth_line(result.worth)
    if result.gap is not None:
        lead = f"Total Hero Power gap **{result.gap:.1%}**. {lead}"
    embed.description = lead

    # ── what they do ─────────────────────────────────────────────────────────
    if result.habit:
        # `grade_read` returns `none` for two different reasons and the copy
        # only speaks to one of them: they genuinely move around, or nobody has
        # watched them enough to tell. Under `LEAN_SEEN` it is the second, and
        # "they change it often" is then a claim about the player that the
        # record does not support — printed, in the thinnest case, directly
        # under "the only line-up on record for this player".
        #
        # Kevin, 2026-08-23: print nothing. Not a softer verdict, because a
        # hedged verdict is still read as a verdict. The field shows the
        # line-up and what the record holds, and stops.
        told = words.habit_line(result.habit)
        if result.habit.total >= intel_lib.LEAN_SEEN:
            told = f"{told} {words.read_line(result.read)}"
        embed.add_field(
            name=FIELD_THEIRS,
            # The line-up on its own line, unbolded, against the bolded
            # recommendation below it: their observed thing is plain and the
            # reader's action is emphasised. Then one paragraph of what the
            # record says and what it is worth. Kevin's layout, 2026-08-20.
            value=(_order_text(result.habit.top) + "\n" + told)[:1024],
            inline=False,
        )
    else:
        embed.add_field(
            name=FIELD_THEIRS,
            value=words.NOTHING_SEEN.format(button=_btn_words(CD_BTN_ORDER))[:1024],
            inline=False,
        )

    # ── what to set ──────────────────────────────────────────────────────────
    if worth_little:
        # One sentence and stop. Ranking six line-ups that are all the same
        # number to the nearest point makes the reader work to arrive at what
        # the sentence already told them, and six rows of "<1%" reads as a
        # broken surface rather than as a finding. This is also the one case
        # where the answer IS a recommendation, so it needs no refusal: set
        # whatever you normally would is advice a member can act on.
        embed.add_field(
            name=FIELD_YOURS,
            value=words.order_barely_matters(result.envelope.spread)[:1024],
            inline=False,
        )
    elif result.needs_your_squads:
        # ⚠️ OPEN QUESTION FOR KEVIN — the counter order has no home in this
        # state, and this is where it used to have one.
        #
        # `counter_types` is computed here and rendered nowhere. It needs
        # nothing about you — the triangle does not care what you field — so it
        # survived on the one-name path exactly where a recommendation could
        # not, and the one-name path is what this change removed. Past this
        # branch your own types are known and the recommendation IS the counter
        # wherever the two agree, so this is the only state left with something
        # unsaid.
        #
        # NOT RESTORED HERE, and the reason is copy rather than plumbing.
        # Printing the counter above `NEEDS_YOUR_SQUADS` puts "**Tank →
        # Aircraft → Missile**" directly over "every line-up you could set
        # looks the same from here", under a heading that says "Your
        # recommended line-up". The two are compatible in fact and contradict
        # each other on screen, and the one-name sentence that reconciled them
        # ("Add your own name to this to see what it is worth against your
        # squads") is exactly the sentence the required field made nonsense of.
        # Reconciling them needs a new sentence, and copy is Kevin's.
        embed.add_field(
            name=FIELD_YOURS,
            value=words.NEEDS_YOUR_SQUADS.format(path=_card_path(CD_BTN_SQUADS))[:1024],
            inline=False,
        )
    elif result.recommended is not None and not result.choice_matters:
        # Kevin, on review: rather than give a false recommendation, be honest
        # about what we can give them and carry the control that fixes it.
        #
        # Same shape as the `worth_little` branch above and a different finding.
        # There the line-up does not decide the match. Here it decides it
        # completely and we cannot say which way, because every arrangement they
        # could field was averaged and your six came out level. The two must not
        # be confused: one says the choice does not matter, the other says the
        # choice matters and we cannot call it.
        refusal = [
            words.CANNOT_RECOMMEND_FLAT.format(measured=words.points(result.choice_spread)),
        ]
        # Only their squad types are named here. The other thing that could be
        # missing is a line-up, and the field above has already said so and
        # already named the press: `NOTHING_SEEN` ends with "Anyone who has
        # faced them can add one with **Record a line-up**", the button named
        # through `_btn_words`. Saying it twice in one embed reads as a surface
        # that is not listening to itself.
        if not result.their_types_known:
            refusal.append(words.CANNOT_RECOMMEND_WHY)
        embed.add_field(name=FIELD_YOURS, value="\n".join(refusal)[:1024], inline=False)

        if not result.their_types_known:
            embed.add_field(
                name=FIELD_FIX,
                value=words.WHAT_WOULD_HELP.format(path=_card_path(CD_BTN_SQUADS))[:1024],
                inline=False,
            )
    elif result.recommended is not None:
        lines = [f"**{_order_text(result.recommended.order)}**"]
        if result.counter_types and result.recommended.order == result.counter_types:
            lines.append(
                f"That counters the line-up they show most often, slot for slot. "
                f"If they hold it your odds of winning are "
                f"**{words.probability(result.recommended.mean)}**."
            )
        else:
            lines.append(
                f"Best across everything they could field: your odds of winning are "
                f"**{words.probability(result.recommended.mean)}** on average, "
                f"between {words.probability(result.recommended.worst)} and "
                f"{words.probability(result.recommended.best)} depending on what they set."
            )
        if result.their_best_reply is not None and result.p_if_they_switch is not None:
            lines.append(
                f"Their best answer to it is {_order_text(result.their_best_reply)}, "
                f"which would drop your odds of winning to "
                f"{words.probability(result.p_if_they_switch)}."
            )
        embed.add_field(name=FIELD_YOURS, value="\n".join(lines)[:1024], inline=False)

    # ── the other five ───────────────────────────────────────────────────────
    if len(result.options) > 1 and not worth_little and result.choice_matters:
        embed.add_field(
            name=FIELD_OTHERS,
            value="\n".join(
                f"{_order_text(option.order)}: {words.probability(option.mean)}"
                for option in result.options[1:]
            )[:1024],
            inline=False,
        )

    # ── worth recording anyway ─────────────────────────────────────────
    # Kevin, on review: recording squads is worth nothing in the matchup you are
    # doing now, and it is still data worth collecting for other rounds and for
    # the next Champion Duel. The old surface suppressed the ask here entirely,
    # which optimised for the answer on screen and threw the contribution away.
    #
    # The suppression it replaces was right about one thing and that is kept:
    # `NEEDS_YOUR_SQUADS` promises this becomes a recommendation, and at a 45%
    # gap that is false. So this is a different sentence, not the same one
    # un-suppressed.
    if worth_little and (result.needs_your_squads or not result.their_types_known):
        embed.add_field(
            name=FIELD_ANYWAY,
            value=words.SQUADS_WORTH_RECORDING_ANYWAY.format(path=_card_path(CD_BTN_SQUADS))[:1024],
            inline=False,
        )

    # ── best and worst case ──────────────────────────────────────────────────
    # Suppressed where it is worth nothing: the range is then "<1% to <1%",
    # which is true, useless, and reads as a bug. The description already
    # carried that finding as a sentence.
    #
    # No note under it any more. The label used to read "What the choice is
    # worth", which valued the range for the reader, and `ENVELOPE_NOTE` then
    # spent two sentences defending the figure against a misreading. A label
    # that just names the two numbers leaves the judgement where it belongs and
    # gives the note nothing left to do. Kevin's call, 2026-08-22.
    if not worth_little:
        envelope = result.envelope
        embed.add_field(
            name=FIELD_WORTH,
            value=(
                f"Across every line-up the two of you could set, this match runs "
                f"from {words.probability(envelope.floor)} to "
                f"{words.probability(envelope.ceiling)}."
            )[:1024],
            inline=False,
        )

    embed.set_footer(text=words.intel_basis(result)[:2048])
    return embed


# ⚠️ TITLE AWAITING SIGN-OFF (2026-08-23). It was "What to field against a
# player", which described the one-sided surface. Shipped as the button's own
# words so pressing the button and reading the modal agree; the variants
# considered are in the PR. Copy is Kevin's.
class _IntelModal(discord.ui.Modal, title="Head to head"):
    """Two named players, both required.

    What comes back is one matchup: their observed habit, the counter to it,
    what your squads make of theirs, what they can do about that, and the range
    the match runs over.

    BOTH NAMES ARE REQUIRED AND THAT IS A REVERSAL, not an oversight corrected.
    Your side was optional, and the argument for it was good enough to survive
    two reviews: a member has to know their own registrant name to fill it in,
    the Discord-user-to-registrant link that would spare them is post-MVP
    (#488), and the one-name answer was a real answer because the counter
    triangle does not care what you field.

    Kevin overruled it on 2026-08-22, and the reason is what the control is for
    rather than what it can do: with the second name optional this was a lookup
    that sometimes did more, and the bot already has a lookup in
    `🔍 Find a player`. Two required names make it the one surface that puts a
    member against a named opponent.

    The cost is real and it is carried here rather than argued away. A member
    who does not know how their name is spelled in the roster cannot reach this
    at all. What that buys is that mistyping it is recoverable: both sides go
    through `_resolve`, so a near miss comes back as "Did you mean" rather than
    as a dead end, and a name on two servers is asked about rather than guessed.
    """

    opponent = discord.ui.TextInput(label="Which player?", max_length=64)
    opponent_server = discord.ui.TextInput(
        label="Their server", required=False, max_length=10, placeholder="e.g. 738"
    )
    you = discord.ui.TextInput(
        label="Your name",
        max_length=64,
        # The placeholder does the work the "(optional)" used to: the one way
        # this field fails is a member who does not know their roster spelling,
        # so it says which spelling is wanted rather than why to fill it in.
        placeholder="As it's spelled in the roster",
    )
    your_server = discord.ui.TextInput(
        label="Your server", required=False, max_length=10, placeholder="e.g. 1042"
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not intel_lib.ENGINE_AVAILABLE:
            await interaction.followup.send(_ENGINE_MISSING, ephemeral=True)
            return
        # Re-checked here rather than trusted off the button, exactly as the
        # odds do: this view outlives the five-minute entitlement cache, so a
        # subscription that lapsed while the hub was on screen would otherwise
        # come through on a button that was live when it was drawn.
        if not await premium.feature_gate(
            "champion_duel_intel", interaction.guild_id, interaction=interaction
        ):
            await _send_intel_upsell(interaction)
            return

        # Discord enforces `required=True` on its side, so neither name can
        # arrive blank from the client. Checked anyway: a modal submission is
        # an HTTP payload and the only thing standing between this handler and
        # a hand-rolled one is Discord's own validation. Without the check the
        # blank falls through to `_resolve`, which asks the roster for "" and
        # answers "No registrant matches ****" — a true sentence about a
        # question nobody asked.
        #
        # BOTH SIDES, not just the one that changed. `opponent` carries the same
        # `required=True` and reaches the same roster query, and a guard that
        # covered only the new field would be defending against the payload
        # threat on one half of a two-half form.
        if not self.opponent.value.strip() or not self.you.value.strip():
            await interaction.followup.send(_INTEL_NEEDS_BOTH, ephemeral=True)
            return

        them = await _resolve(self.opponent.value, self.opponent_server.value or None)
        if isinstance(them, str):
            await interaction.followup.send(them, ephemeral=True)
            return

        you = await _resolve(self.you.value, self.your_server.value or None)
        if isinstance(you, str):
            await interaction.followup.send(you, ephemeral=True)
            return

        try:
            result = await asyncio.to_thread(intel_lib.intel, them, you)
        except predict_lib.NotEnoughData as exc:
            slots = ", ".join(str(s) for s in exc.missing)
            await interaction.followup.send(
                f"⚠️ We don't have a full line-up for **{exc.name}**. Slot(s) {slots} "
                f"have no squad recorded, so there's nothing to work out.\n"
                f"Run `{CHAMPION_DUEL_HUB_CMD}` → **{CD_BTN_FIND}** → "
                f"**{CD_BTN_SQUADS}** to fill them in.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(embed=build_intel_embed(result), ephemeral=True)


async def _send_intel_upsell(interaction: discord.Interaction) -> None:
    """Refuse the recommendation and offer the upgrade.

    Same fallback as the odds: `upgrade_view` returns None with no SKU
    configured and discord.py raises on `view=None`, so the embed's own
    "Run `/upgrade`" line carries it in that case.
    """
    view = premium.upgrade_view()
    embed = premium.premium_locked_embed(feature_label=_btn_words(CD_BTN_INTEL))
    kwargs = {"view": view} if view is not None else {}
    await interaction.followup.send(embed=embed, ephemeral=True, **kwargs)


# ── Look up ───────────────────────────────────────────────────────────────────


def _order_share(seen: int, total: int) -> str:
    """The order on screen as a share of what we hold for this player.

    "Seen 1 of 1 sightings" was ungrammatical and circular: it answered a
    question nobody asked with two counts that were the same number. The reader
    wants one thing, which is whether this is what the player always does or
    just the most common of several, so the three cases are phrased rather than
    computed from a template.

    Says "recorded orders" rather than "sightings" to match the rest of the
    surface. One thing, one name.
    """
    if total <= 1:
        return "Their only recorded order"
    if seen == total:
        return f"All {total} of their recorded orders"
    return f"{seen} of their {total} recorded orders"


def _squad_basis(squads: list[dict]) -> str:
    """Where these numbers came from, as a sentence.

    Replaces the `👁 ≈ ✏️` legend. Per-value glyphs made the reader learn a key
    and apply it three times to answer one question ("can I trust this?"), and
    `DESIGN.md` retired 👁️ in 2026-08-10 for reading clinical. This follows the
    prediction card's footer instead (`champion_duel_image._footer`), which
    states the basis for the whole card in the reader's own words.

    Estimated is called out ahead of observed when both are present: the
    weakest input is what qualifies the card, exactly as `medium` confidence
    does on the prediction.
    """
    sources = {s.get("source") for s in squads}
    corrected = " Corrected values came from a member." if "edited" in sources else ""
    if "estimated" in sources:
        if sources & {"observed", "edited"}:
            return "Some squad powers are estimated from total hero power." + corrected
        return "Squad powers are estimated from total hero power, not seen in game."
    return "Squad powers are what someone saw in game." + corrected


def build_player_embed(
    player: dict, top_order: dict | None, *, grouping: dict | None = None
) -> discord.Embed:
    """One registrant: who they are, what they field, and what they've been
    seen doing.

    Ordered by what a member came for. The squads and the order are the answer;
    the group and rank are qualifier history, which is context rather than the
    point, so they sit below rather than in the lead.

    `grouping` is the *caller's*, and it is only used to decide whether a group
    letter needs qualifying. A letter is meaningful inside a grouping and
    nowhere else, so "Group M" on a player from another draw reads as a claim
    the reader will act on and it is not one.
    """
    alliance = f"[{player['alliance']}] " if player.get("alliance") else ""
    embed = discord.Embed(
        title=f"{alliance}{_label(player)}"[:256],
        color=discord.Color.blurple(),
    )
    embed.description = (
        f"THP: {player['thp']:,.0f}" if player.get("thp") else "No total hero power recorded."
    )

    squads = sorted(player.get("squads") or [], key=lambda s: s["slot"])
    if squads:
        embed.add_field(
            name="Squads",
            value="\n".join(
                f"{s['slot']}. {s.get('squad_type') or '(none)'} · {(s.get('power') or 0):,.0f}"
                for s in squads
            )[:1024],
            inline=False,
        )
    else:
        embed.add_field(name="Squads", value="Nothing recorded yet.", inline=False)

    if top_order:
        order = " → ".join(top_order["order"])
        embed.add_field(
            name=FIELD_THEIRS,
            value=f"**{order}**\n{_order_share(top_order['seen'], top_order['total'])}",
            inline=False,
        )
    else:
        embed.add_field(
            name=FIELD_THEIRS,
            value="No deploy orders recorded. A prediction will assume strongest first.",
            inline=False,
        )

    # Every round they are in, oldest first, which is how far they have got.
    # This used to be one field hardcoded to "Qualifiers", which stopped being
    # true the day a semifinal draw landed.
    #
    # A round with no rank shows the group alone: a draw is not a result, and
    # nobody has a position in a round until they play it.
    #
    # The knockouts carry no group letter, and their placement says more than a
    # bare number does: a 32-bracket is rigid, so the position is how far the
    # player got and that is the thing worth reading.
    def _group_bit(row: dict) -> str | None:
        """The group letter, qualified when it belongs to a different draw.

        "Group D" is only exact inside one grouping. On a player from another
        one it reads as a claim the reader will act on, so it says plainly that
        this is not the reader's.

        It deliberately does NOT name the other one by its start date. Every
        draw in a season starts on the same day, so the date would print the
        reader's own Champion Duel's name while asserting it is a different
        one, which is worse than saying nothing. Naming it would need the thing
        that actually separates the two, which is the other set of
        Participating Warzones, and that is a list rather than a label.
        """
        if not row.get("grp"):
            return None
        if grouping and row.get("grouping_id") != grouping.get("id"):
            return f"Group {row['grp']} (not your Champion Duel)"
        return f"Group {row['grp']}"

    def _rank_bit(stage: str, row: dict) -> str | None:
        """How they finished, in the terms that round is read in.

        A knockout placement replaces the bare rank rather than sitting beside
        it: "Rank 1 · 1st" says one thing twice, and for the other 29 the
        position among the eliminated is not the part worth reading. The
        number is still stored; this is what the card says about it.
        """
        if stage == "knockouts":
            return db.knockout_result(row.get("rank"))
        return f"Rank {row['rank']}" if row.get("rank") else None

    rounds = "\n".join(
        f"**{db.STAGE_LABELS.get(stage, stage.title())}** · "
        + " · ".join(bit for bit in (_group_bit(row), _rank_bit(stage, row)) if bit).rstrip(" ·")
        for stage, row in (player.get("stages") or {}).items()
    )
    if rounds:
        embed.add_field(name="Rounds", value=rounds[:1024], inline=False)

    if squads:
        embed.set_footer(text=_squad_basis(squads))
    return embed


class _PlaceInGroupModal(discord.ui.Modal, title="Which group are they in?"):
    """Put a player we already have into a round's group.

    Two dropdowns and nothing typed, because both answers come from a fixed
    set: there are three rounds and sixteen letters, and free text here only
    creates ways to be wrong. It replaces the group box that used to sit on the
    add-a-player screen, which had to guess a round and could not offer the
    letters.

    The knockouts are absent from the round list on purpose. They are one field
    of 32 with no letter at all, so there would be nothing to pick.
    """

    def __init__(self, *, player: dict, grouping: dict):
        super().__init__()
        self.player = player
        self.grouping = grouping

    stage = discord.ui.Label(
        text="Which round?",
        component=discord.ui.Select(
            options=[
                discord.SelectOption(label=db.STAGE_LABELS[key], value=key)
                for key in ("qualifiers", "semifinals")
            ],
        ),
    )
    group = discord.ui.Label(
        text="Which group?",
        component=discord.ui.Select(
            options=[
                discord.SelectOption(label=f"Group {letter}", value=letter)
                for letter in db.GROUP_LABELS
            ],
        ),
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        stage = self.stage.component.values[0]
        letter = self.group.component.values[0]
        server = self.player.get("server")

        # The guard that used to live on the add-a-player screen, kept because
        # it is the one that matters: a letter belongs to one Champion Duel, and
        # writing one for a player whose warzone is in a different draw is what
        # put an officer in warzone 1500's opponent into the imported grouping's
        # Group D. Refused out loud rather than dropped.
        if server and server not in self.grouping["warzones"]:
            await interaction.followup.send(
                f"⚠️ Warzone **{server}** is not in {_grouping_name(self.grouping)}, "
                f"so **Group {letter}** there is a different group from yours. "
                f"Nothing was saved.",
                ephemeral=True,
            )
            return

        await asyncio.to_thread(
            db.set_stage,
            self.player["id"],
            stage,
            grp=letter,
            grouping_id=self.grouping["id"],
        )
        await interaction.followup.send(
            f"✅ Put **{_label(self.player)}** in **Group {letter}** for the "
            f"**{db.STAGE_LABELS[stage]}**.",
            ephemeral=True,
        )


class PlayerActionsView(discord.ui.View):
    """The write actions, attached to a player already on screen.

    Each flow used to open with "who?" — so contributing three squad values
    and an order meant typing one name four times, and four chances to get an
    ambiguous match or a typo. Finding the player once and acting on them is
    the same work with the identity question asked once.

    Locked controls render disabled rather than vanishing, per the Premium rule
    in `notes/DESIGN.md`: someone on the free tier should see what contributing
    would look like.
    """

    def __init__(
        self, *, player: dict, user_id: int, can_write: bool, grouping: dict | None = None
    ):
        super().__init__(timeout=600)
        self.player = player
        self.user_id = user_id
        self.grouping = grouping
        self.message: discord.Message | None = None

        actions = [
            (CD_BTN_SQUADS, self._on_squads),
            (CD_BTN_ORDER, self._on_order),
        ]
        # Absent rather than disabled without a grouping: a group letter is
        # meaningless outside one, so there is nothing this could set.
        if grouping:
            actions.append((CD_BTN_PLACE, self._on_place))

        for label, callback in actions:
            button = discord.ui.Button(
                label=(label if can_write else f"🔒 {label}")[:80],
                style=discord.ButtonStyle.secondary,
                disabled=not can_write,
            )
            button.callback = callback
            self.add_item(button)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    async def _on_squads(self, inter: discord.Interaction):
        await inter.response.send_modal(_SquadDetailModal(player=self.player))

    async def _on_place(self, inter: discord.Interaction):
        await inter.response.send_modal(
            _PlaceInGroupModal(player=self.player, grouping=self.grouping)
        )

    async def _on_order(self, inter: discord.Interaction):
        # Straight to the picker. The modal that used to sit in front of this
        # asked only who the player faced, and that is not an input to
        # anything: a prediction samples the order, not the opponent. Asking
        # for it made the flow look like it wanted a battle report.
        await inter.response.defer(ephemeral=True, thinking=True)
        view = _OrderSelectView(player=self.player, opponent=None, user_id=inter.user.id)
        await inter.followup.send(
            f"Which order did **{_label(self.player)}** deploy in?\n"
            f"Deployment order decides which squad meets which, so a recorded "
            f"order is what sharpens every prediction for them.",
            view=view,
            ephemeral=True,
        )
        view.message = await inter.original_response()


async def send_player_card(
    interaction: discord.Interaction,
    player: dict,
    *,
    can_write: bool,
    note: str | None = None,
    grouping: dict | None = None,
):
    """One player, with what can be done to them underneath.

    Shared by finding a player and adding one, so a player you just created
    lands you in the same place as one that was already there — the next thing
    you want is to record what you saw, either way.

    `grouping` is the caller's, and only decides whether a group letter on this
    card needs qualifying. Find stays global on purpose: prediction is useful
    against players on other warzones before any draw, and scoping the look-up
    would take that away.
    """
    top = await asyncio.to_thread(db.most_common_order, player["id"])
    view = PlayerActionsView(
        player=player,
        user_id=interaction.user.id,
        can_write=can_write,
        grouping=grouping,
    )
    await interaction.followup.send(
        content=note,
        embed=build_player_embed(player, top, grouping=grouping),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


class _MissView(discord.ui.View):
    """The exit from a name we do not have, on the message that reported it.

    The name and server they just typed are carried into the modal as
    defaults, so someone who spelled it right and simply met a player we have
    never imported does not type it a second time.
    """

    def __init__(
        self,
        *,
        can_write: bool,
        user_id: int,
        name: str,
        server: str | None,
        grouping: dict | None = None,
    ):
        super().__init__(timeout=600)
        self.can_write = can_write
        self.user_id = user_id
        self.name = name
        self.server = server
        self.grouping = grouping
        self.message: discord.Message | None = None

        button = discord.ui.Button(
            label=(CD_BTN_ADD if can_write else f"🔒 {CD_BTN_ADD}")[:80],
            style=discord.ButtonStyle.primary if can_write else discord.ButtonStyle.secondary,
            disabled=not can_write,
        )
        button.callback = self._on_add
        self.add_item(button)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    async def _on_add(self, inter: discord.Interaction):
        await inter.response.send_modal(
            _AddPlayerModal(
                self.can_write, name=self.name, server=self.server, grouping=self.grouping
            )
        )


class _FindPlayerModal(discord.ui.Modal, title="Find a Champion Duel player"):
    def __init__(self, can_write: bool, *, grouping: dict | None = None):
        super().__init__()
        self.can_write = can_write
        self.grouping = grouping

    name = discord.ui.TextInput(label="Player name", max_length=64)
    server = discord.ui.TextInput(
        label="Server", required=False, max_length=10, placeholder="e.g. 738"
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        found = await _resolve(self.name.value, self.server.value or None)
        if isinstance(found, str):
            # A miss is not a dead end any more: the name they typed is very
            # likely a real player we simply have not met. The exit is a button
            # on this message rather than a route back to the hub, because the
            # user is already mid-task and naming a button they have to go find
            # is only half of "every dead end carries its exit".
            view = _MissView(
                can_write=self.can_write,
                user_id=interaction.user.id,
                name=self.name.value,
                server=self.server.value or None,
                grouping=self.grouping,
            )
            await interaction.followup.send(
                f"{found}\n\nIf we don't have them listed, add them below.",
                view=view,
                ephemeral=True,
            )
            view.message = await interaction.original_response()
            return
        await send_player_card(interaction, found, can_write=self.can_write, grouping=self.grouping)


# ── Collecting a player, in the shape the model reads ─────────────────────────
#
# Two screens, because Discord allows five components a modal and the fields
# split cleanly at five. Kevin's structure, 2026-08-16.
#
# The gorilla is deliberately absent. It sits on the biggest squad 93% of the
# time and inflates whichever squad carries it by about a tenth, so the biggest
# reading IS the carrier in almost every real lineup and the engine works that
# out from the powers. Asking would spend a component on a question we can
# answer better ourselves.

#: The six type orders, which is every arrangement of one of each. Measured on
#: 50 real lineups: nobody fields two of a type, so this is the whole space and
#: one select covers what would otherwise be three.
#:
#: Ordered by BOX, not by power. The member is reading their lineup screen left
#: to right and typing the powers in that order, so the types have to line up
#: with the boxes beside them. The engine sorts and carries the types along.
_TYPE_ORDERS = [
    ("Tank", "Missile", "Aircraft"),
    ("Tank", "Aircraft", "Missile"),
    ("Missile", "Tank", "Aircraft"),
    ("Missile", "Aircraft", "Tank"),
    ("Aircraft", "Tank", "Missile"),
    ("Aircraft", "Missile", "Tank"),
]

#: The seventh option, for a lineup the six permutations cannot describe.
#:
#: 96% of players run one of each type, so six options cover almost everyone
#: and a dropdown beats typing for them. The rest run two of something, and
#: enumerating those would take the list from six to twenty-seven to catch one
#: player in twenty-five. So the list stays short and the exception says so.
_TYPE_ORDER_OTHER = "other"

#: How long to wait for somebody to type their squad types in the channel.
#: The same 120s the setup wizards give a free-text step, and for the same
#: reason: a member may be reading it off a game screen on the same phone.
_TYPE_ORDER_TIMEOUT = 120


def _parse_type_order(text: str) -> tuple | None:
    """Three squad types in box order, from something a person typed.

    Forgiving on separators and on how much of each word they wrote, because
    this is the path for somebody who has already been told the dropdown does
    not fit them and is now typing what they can see. `T/M/A` and
    `tank, tank, air` both work.

    Returns None when it cannot be read, which the caller turns into a retry
    rather than a guess: a wrong type is a wrong counter matchup on every
    prediction that player appears in.
    """
    cleaned = (text or "").strip().lower()
    if not cleaned:
        return None
    for separator in (",", "/", "-", ">"):
        cleaned = cleaned.replace(separator, " ")
    words = cleaned.split()
    if len(words) != 3:
        return None
    out = []
    for word in words:
        # Prefix match, so "air", "aircraft" and "a" all land. The three names
        # share no first letter, which is what makes a single character enough.
        matched = [t for t in db.VALID_TYPES if t.lower().startswith(word)]
        if len(matched) != 1:
            return None
        out.append(matched[0])
    return tuple(out)


def _squad_offer(powers, order, mixed) -> dict:
    """One squad submission as `{slot: {field: value}}`, aligned box by box.

    Every box carries a purity answer, because on the screen this comes from
    the member had the lineup in front of them: saying nothing about a squad is
    saying it is pure. That is the one place the NULL-versus-0 distinction on
    `squads.mixed` is deliberately spent.
    """
    return {
        slot: {
            "squad_type": squad_type,
            "power": power,
            "mixed": None if mixed is None else int(slot in mixed),
        }
        for slot, (power, squad_type) in enumerate(zip(powers, order), start=1)
    }


def _parse_mixed(text: str) -> set[int] | None:
    """Which boxes are mixed type, from something like `1,3`.

    **Blank means none, not "not asked".** Kevin's decision, 2026-08-17: the
    box is optional, and leaving it empty says the same thing as typing
    "none". Somebody filling this screen in has the lineup in front of them,
    so silence about a mixed squad is an answer.

    That deliberately spends the NULL-versus-0 distinction the `squads.mixed`
    column keeps, and it only spends it HERE, at the one surface where a person
    was looking at the lineup when they said nothing. Everywhere else -- an
    import, a player nobody has opened -- absence still means nobody has looked,
    which is what stops the model treating an unscouted player as measured.

    Returns a set of 1-based box numbers, an empty set for an explicit "none",
    or None when it cannot be read.

    Free text rather than a dropdown, because the model needs to know WHICH
    squads are mixed and not how many. It used to take a count and apply the
    penalty to the bottom two, and the corpus says that is usually wrong:
    across the players whose three squads have all been seen, the bottom pair
    was the mixed one once against five for the top pair. Purity is not where a
    player is weakest, it is where their best heroes are spread.
    """
    cleaned = (text or "").strip().lower()
    if not cleaned or cleaned in ("none", "no", "n", "-", "0"):
        return set()
    out = set()
    for piece in cleaned.replace(" ", "").split(","):
        if piece not in ("1", "2", "3"):
            return None
        out.add(int(piece))
    return out


class _SquadDetailModal(discord.ui.Modal, title="Squad powers and types"):
    """The second screen: what the lineup screen shows, box by box.

    Every box is optional. A player is placed by any single squad power or by
    their Total Hero Power, so somebody who reads one number off and closes the
    app has still helped. Given powers are used exactly and the rest are filled
    from the shape fit, which is the whole reason partial entry is worth taking.
    """

    def __init__(self, *, player: dict):
        super().__init__()
        self.player = player

    squad1 = discord.ui.TextInput(
        label="Squad 1 power", required=False, max_length=16, placeholder="e.g. 94.2M"
    )
    squad2 = discord.ui.TextInput(
        label="Squad 2 power", required=False, max_length=16, placeholder="Leave blank if unknown"
    )
    squad3 = discord.ui.TextInput(
        label="Squad 3 power", required=False, max_length=16, placeholder="Leave blank if unknown"
    )
    types = discord.ui.Label(
        text="Squad types, in the same order as the boxes above",
        component=discord.ui.Select(
            required=False,
            options=[
                discord.SelectOption(label=" / ".join(order), value=str(i))
                for i, order in enumerate(_TYPE_ORDERS)
            ]
            + [
                discord.SelectOption(
                    label="Other",
                    value=_TYPE_ORDER_OTHER,
                    description="If they run two of the same type",
                )
            ],
        ),
    )
    # A Label rather than a bare TextInput so the question and the instruction
    # can be separate lines. Discord caps a field label at 45 characters and
    # both together run past it, and the question is the half that has to
    # survive: somebody who reads only the bold line still knows what is being
    # asked.
    mixed = discord.ui.Label(
        text="Are any of these squads mixed type?",
        description="List which squads if so.",
        component=discord.ui.TextInput(
            required=False,
            max_length=8,
            placeholder="e.g. 1,3",
        ),
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        powers = [parse_power(box.value) for box in (self.squad1, self.squad2, self.squad3)]
        typed = [box.value for box in (self.squad1, self.squad2, self.squad3)]
        unreadable = [
            i + 1
            for i, (raw, val) in enumerate(zip(typed, powers))
            if (raw or "").strip() and val is None
        ]
        if unreadable:
            await interaction.followup.send(
                f"⚠️ Squad {_plural(len(unreadable), 'power')} "
                f"{', '.join(str(i) for i in unreadable)} could not be read. "
                f"The game writes **94.2M** and a spreadsheet writes "
                f"**94,200,000**; both work. Nothing was saved.",
                ephemeral=True,
            )
            return

        raw_mixed = (self.mixed.component.value or "").strip()
        mixed = _parse_mixed(raw_mixed)
        if mixed is None:
            await interaction.followup.send(
                "⚠️ Say which squads are mixed type as box numbers, like **1,3**. "
                "Leave it blank if none of them are. Nothing was saved.",
                ephemeral=True,
            )
            return

        chosen = self.types.component.values

        # A blank purity box means "none are mixed", but only once the member
        # has told us something. Submitting the whole screen empty is not a
        # measurement that every squad is pure -- nobody looked at anything --
        # and without this check it would write one for all three boxes.
        if not (any(p is not None for p in powers) or chosen or raw_mixed):
            await interaction.followup.send(
                f"↩️ Nothing to record for **{_label(self.player)}**. No changes made.",
                ephemeral=True,
            )
            return

        # "Other" is a lineup the six permutations cannot describe, so the
        # order has to be typed. Everything they already filled in is saved
        # first, so the follow-up costs them only the types.
        if chosen and chosen[0] == _TYPE_ORDER_OTHER:
            await _write_squad_powers(interaction, self.player, powers, mixed, source="observed")
            await _ask_for_type_order(interaction, self.player)
            return

        order = _TYPE_ORDERS[int(chosen[0])] if chosen else (None, None, None)
        offered = _squad_offer(powers, order, mixed)
        await _ask_or_write(interaction, self.player, offered, source="observed")


class _AddPlayerModal(discord.ui.Modal, title="Add a player we don't have"):
    """Create a registrant from a sighting.

    The roster is an official import of who signed up. It is not everyone
    anyone will ever face — names change, and an opponent can be outside
    whatever we last imported. Without this, meeting someone we don't have is
    a dead end, and the argument for opening writes to Premium alliances
    (more contributors, better data) only holds for players we already knew.

    Rows created here carry `origin='self_reported'` and say so wherever they
    are shown. That flag is what keeps a community guess from ever reading as
    an official record, and an import later upgrades the row rather than
    duplicating it.
    """

    def __init__(
        self,
        can_write: bool,
        *,
        name: str | None = None,
        server: str | None = None,
        grouping: dict | None = None,
    ):
        super().__init__()
        self.can_write = can_write
        self.grouping = grouping
        # Safe to set on self: `Modal._init_children` deepcopies each declared
        # item onto the instance, so a default here cannot leak to the next
        # person who opens this modal.
        if name:
            self.name.default = name[:64]
        if server:
            self.server.default = server[:10]

    # Five components is the cap and all five earn their place. `group` moved
    # off this screen when Total Hero Power and troop level arrived: a letter
    # is round data that the record and reconcile flows already collect
    # properly, where these two are facts about the player that nothing else
    # asks for and the model cannot run without one of them.
    name = discord.ui.TextInput(label="Player name", max_length=64)
    server = discord.ui.TextInput(label="Warzone", max_length=10, placeholder="e.g. 738")
    # The tag, not the name. `registrants.alliance` has always held three or
    # four characters because that is what the game shows beside a player, and
    # nothing said so, so people typed the whole alliance name into it.
    alliance = discord.ui.TextInput(
        label="Alliance tag",
        required=False,
        max_length=8,
        placeholder="The 3 or 4 characters in brackets, e.g. OGV",
    )
    thp = discord.ui.TextInput(
        label="Total Hero Power",
        required=False,
        max_length=16,
        placeholder="e.g. 325.8M",
    )
    troop_level = discord.ui.Label(
        text="Troop level",
        component=discord.ui.Select(
            required=False,
            options=[
                discord.SelectOption(label=f"Lv.{n}", value=str(n))
                for n in range(MAX_TROOP_LEVEL, 0, -1)
            ],
        ),
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not db.NAMES_AVAILABLE:
            await interaction.followup.send(_ENGINE_MISSING, ephemeral=True)
            return

        name = (self.name.value or "").strip()
        server = (self.server.value or "").strip()
        if not name or not server:
            await interaction.followup.send(
                "⚠️ A player needs both a name and a server. Identity here is the "
                "two together, because two servers can field the same name.",
                ephemeral=True,
            )
            return

        thp = parse_power(self.thp.value)
        if (self.thp.value or "").strip() and thp is None:
            await interaction.followup.send(
                "⚠️ That Total Hero Power could not be read. The game writes "
                "**325.8M** and a spreadsheet writes **325,800,000**; both work. "
                "Nothing was saved.",
                ephemeral=True,
            )
            return

        chosen = self.troop_level.component.values
        level = int(chosen[0]) if chosen else None

        existing = await asyncio.to_thread(db.find_registrants, name, server)
        try:
            player = await asyncio.to_thread(
                db.upsert_registrant,
                name,
                server=server,
                alliance=(self.alliance.value or "").strip() or None,
                thp=thp,
                troop_level=level,
                origin="self_reported",
                actor=_actor(interaction),
            )
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return

        # The second screen cannot be opened from here: a modal has to be the
        # first response to an interaction and this one has already answered.
        # So the squads are offered as a button on the result instead, which
        # also lets somebody who only knows a name stop after one screen.
        aside = ""

        note = (
            f"ℹ️ **{_label(player)}** was already here. Opening them instead of adding a duplicate."
            if existing
            else f"✅ Added **{_label(player)}**."
        )
        await send_player_card(
            interaction,
            player,
            can_write=self.can_write,
            note=note + aside,
            grouping=self.grouping,
        )


# ── When we already hold a different answer ───────────────────────────────────
#
# Kevin's design: if someone is entering data we already have, surface what we
# have, show them the two pieces, and ask which is correct.
#
# One question per submission, never one per field. A member filling in three
# boxes answered one thing; asking three times turns a correction into an
# interrogation, and `compare_squad` already narrows this to real
# contradictions -- an estimate giving way to a reading, somebody correcting
# their own entry, and two people agreeing all pass without a word.

#: How each disputed field is named to a member. `mixed` is stored as a flag
#: and read off the game's lineup screen as a squad of four types rather than
#: five, so it is named the way the screen shows it, not the way we store it.
_FIELD_LABELS = {
    "squad_type": "Squad type",
    "power": "Power",
    "mixed": "4-of-a-type",
}


def _value_text(field: str, value) -> str:
    """One held or offered value, in the units the member reads it in."""
    if value is None:
        return "nothing"
    if field == "power":
        return f"{float(value):,.0f}"
    if field == "mixed":
        return "Yes" if value else "No"
    return str(value)


def build_disagreement_embed(player: dict, pending: list[dict]) -> discord.Embed:
    """The two pieces, side by side, for every field that contradicts.

    ❓ rather than ⚠️, per the row-state catalog: nothing is wrong here, there
    is simply more than one right answer, and the two must not read the same.
    """
    disputed = [entry for entry in pending if entry["disputed"]]
    count = sum(len(entry["disputed"]) for entry in disputed)
    embed = discord.Embed(
        title="❓ We already have a different answer",
        description=(
            f"**{_label(player)}** already has "
            f"{'a value' if count == 1 else 'values'} recorded that "
            f"{'does' if count == 1 else 'do'} not match what you entered. "
            f"Pick whichever is right."
        ),
        color=discord.Color.blurple(),
    )
    for entry in disputed:
        for row in entry["disputed"]:
            embed.add_field(
                name=f"Slot {entry['slot']}: {_FIELD_LABELS[row['field']]}",
                value=(
                    f"What we have: **{_value_text(row['field'], row['held'])}**\n"
                    f"What you entered: **{_value_text(row['field'], row['offered'])}**"
                ),
                inline=False,
            )
    return embed


async def _write_squad_fields(interaction, player, slot, values, *, source: str) -> dict:
    """One `set_squad`, or nothing when there is nothing to say.

    `source` travels from the surface that collected it and is not defaulted
    here: `edited` outranks every later import and `observed` does not, so
    guessing it would either bury a correction or protect a sighting that was
    never meant to be permanent.
    """
    if not any(value is not None for value in values.values()):
        return {}
    return await asyncio.to_thread(
        db.set_squad,
        player["id"],
        slot,
        values.get("squad_type"),
        values.get("power"),
        mixed=values.get("mixed"),
        source=source,
        actor=_actor(interaction),
    )


async def _write_undisputed(interaction, player, pending, *, source: str) -> int:
    """Save everything this submission says that nothing contradicts.

    **Written before the question is asked, not after it is answered.** The
    question is only about the fields that contradict; holding the rest hostage
    to it means a member who reads three powers, is asked about one, and gets
    interrupted loses all three. There is nothing to arbitrate about a value
    nobody has offered a different one for, so there is no reason to wait.
    """
    written = 0
    for entry in pending:
        disputed = {row["field"] for row in entry["disputed"]}
        values = {
            field: value for field, value in entry["offered"].items() if field not in disputed
        }
        if await _write_squad_fields(interaction, player, entry["slot"], values, source=source):
            written += 1
    return written


class _DisagreementView(discord.ui.View):
    """Two pieces, two buttons.

    It settles ONLY the contradicted fields. Everything else in the submission
    was written before this view went up, so a member who never answers loses
    nothing they told us that nobody disputes.

    Bare labels. The alternatives differ by which value is right, which is a
    parameter rather than a kind, and `DESIGN.md` sends parameter sets out
    without glyphs rather than repeating one across the pair.

    Neither button is `primary`. The bot has no view on which of two people
    read the screen correctly, and styling one as recommended would be exactly
    the opinion `UX.md` says it does not have.
    """

    def __init__(self, *, player: dict, pending: list[dict], user_id: int, source: str):
        super().__init__(timeout=120)
        self.player = player
        self.pending = [entry for entry in pending if entry["disputed"]]
        self.user_id = user_id
        self.source = source
        #: Set by `_ask_which` so the view can retire its own message.
        self.message = None

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _settle(self, inter: discord.Interaction, *, use_offered: bool):
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(view=self)
        actor = _actor(inter)
        for entry in self.pending:
            edits = {}
            if use_offered:
                disputed = {row["field"] for row in entry["disputed"]}
                edits = (
                    await _write_squad_fields(
                        inter,
                        self.player,
                        entry["slot"],
                        {
                            field: value
                            for field, value in entry["offered"].items()
                            if field in disputed
                        },
                        # `edited`, whatever the surface that collected it
                        # says. A value that overrides one a person already
                        # recorded is a correction by definition, and
                        # `_import_would_downgrade` protects `edited` from
                        # every later import where `observed` only outranks an
                        # estimate. Losing that was the real cost of retiring
                        # the one-slot modal, which wrote `edited` outright;
                        # this puts it back on the only entries that need it.
                        source="edited",
                    )
                ).get("edits", {})
            await asyncio.to_thread(
                db.record_disagreement,
                self.player["id"],
                target="squad",
                slot=entry["slot"],
                rows=entry["disputed"],
                chose="offered" if use_offered else "held",
                actor=actor,
                edits=edits,
            )
        settled = "Saved what you entered" if use_offered else "Kept what we had"
        await inter.followup.send(
            f"✅ {settled} for **{_label(self.player)}**. Either way, your answer is on record.",
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="Keep what we have", style=discord.ButtonStyle.secondary)
    async def keep(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._settle(inter, use_offered=False)

    @discord.ui.button(label="Use what I entered", style=discord.ButtonStyle.secondary)
    async def use_mine(self, inter: discord.Interaction, button: discord.ui.Button):
        await self._settle(inter, use_offered=True)

    async def on_timeout(self) -> None:
        # A live-looking button on a dead view is a bug, not cosmetics: the
        # member presses it, gets "Interaction failed", and never learns the
        # question went unanswered.
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)


async def _pending_squad_entries(interaction, player: dict, offered_by_slot: dict) -> list[dict]:
    """What this submission would write, and where it contradicts what we hold.

    `offered_by_slot` is `{slot: {field: value}}`, carrying only the fields the
    member filled in. Omitting a field is not an assertion about it.
    """
    actor = _actor(interaction)
    pending = []
    for slot, offered in sorted(offered_by_slot.items()):
        disputed = await asyncio.to_thread(
            db.compare_squad, player["id"], slot, actor=actor, **offered
        )
        pending.append({"slot": slot, "offered": offered, "disputed": disputed})
    return pending


async def _ask_which(interaction, player: dict, pending: list[dict], *, source: str) -> None:
    """Put the two pieces up with two buttons. The caller has deferred.

    Everything nobody disputes is saved first, so the question is only ever
    about the fields that contradict and an unanswered one costs only those.
    """
    await _write_undisputed(interaction, player, pending, source=source)
    view = _DisagreementView(
        player=player, pending=pending, user_id=interaction.user.id, source=source
    )
    # `wait=True` so the view holds its own message and can retire it on
    # timeout. Without it `self.message` is None and the buttons stay live
    # looking on a dead view.
    view.message = await interaction.followup.send(
        embed=build_disagreement_embed(player, pending),
        view=view,
        ephemeral=True,
        wait=True,
    )


async def _ask_or_write(
    interaction, player: dict, offered_by_slot: dict, *, source: str, quiet: bool = False
) -> None:
    """Save a squad submission, asking first only where it contradicts.

    `quiet` suppresses the acknowledgement, for a caller that is only halfway
    through and will say something itself. It never suppresses the
    disagreement prompt: that is a question, not an acknowledgement, and
    swallowing it would drop the answer on the floor.
    """
    pending = await _pending_squad_entries(interaction, player, offered_by_slot)
    if any(entry["disputed"] for entry in pending):
        await _ask_which(interaction, player, pending, source=source)
        return

    written = await _write_undisputed(interaction, player, pending, source=source)
    if quiet:
        return
    if not written:
        await interaction.followup.send(
            f"↩️ Nothing to record for **{_label(player)}**. No changes made.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"✅ Recorded {_plural(written, 'squad')} for **{_label(player)}**.",
        ephemeral=True,
    )


# ── "Other": a lineup the six permutations cannot describe ────────────────────
#
# 96% of players run one of each type. The other 4% run two of something, and
# enumerating those would take the dropdown from six options to twenty-seven to
# catch one player in twenty-five. So the list stays short and the exception is
# asked for afterwards.
#
# It cannot be a sixth field on the squad modal: five components is Discord's
# cap and that screen is at it. And it cannot be a second modal, because
# Discord will not accept a modal as the response to a modal submission. So it
# is asked the way every free-text step in the setup wizards is asked -- an
# ephemeral prompt, then `wait_for` on the member's next message.


async def _write_squad_powers(interaction, player, powers, mixed, *, source: str) -> None:
    """Save the half of an "Other" submission that needs no types.

    Written before the question rather than after the answer, so a member who
    reads three powers, picks Other and then gets pulled away keeps the powers.
    The types are the only thing the follow-up adds.
    """
    offered = _squad_offer(powers, (None, None, None), mixed)
    await _ask_or_write(interaction, player, offered, source=source, quiet=True)


async def _ask_for_type_order(interaction, player: dict) -> None:
    """Ask for the order in the channel, and wait for them to type it.

    A modal cannot answer a modal, and the alternative -- a button that opens a
    second modal -- makes somebody press twice to answer one question. So the
    question is asked the way the setup wizards ask theirs: `wait_for` on their
    next message in the channel.

    The prompt is ephemeral, but their reply cannot be: nobody can send an
    ephemeral message. So the reply is deleted once it has been read, which
    leaves the channel as it was and keeps a line that means nothing without
    the prompt above it from sitting there.

    One retry before giving up, per `UX.md`: a validation failure costs one
    step, not the whole flow. Their squad powers are already saved either way,
    so the worst outcome is the types missing.
    """
    await interaction.followup.send(
        "**What are their three squad types?**\n"
        "Type them here in the same order as the power boxes, like "
        "`Tank, Tank, Aircraft`.",
        ephemeral=True,
    )

    def _mine(message):
        return (
            message.author.id == interaction.user.id
            and message.channel.id == interaction.channel_id
        )

    for attempt in range(2):
        try:
            reply = await interaction.client.wait_for(
                "message", check=_mine, timeout=_TYPE_ORDER_TIMEOUT
            )
        except asyncio.TimeoutError:
            await interaction.followup.send(
                f"⏰ No squad types recorded for **{_label(player)}**. Their squad "
                f"powers are saved. Run `{CHAMPION_DUEL_HUB_CMD}` → "
                f"**{CD_BTN_FIND}** → **{CD_BTN_SQUADS}** when you have them.",
                ephemeral=True,
            )
            return

        raw = (reply.content or "").strip()
        # Best effort. Deleting needs Manage Messages, and not having it is not
        # a reason to fail a save that has already happened.
        try:
            await reply.delete()
        except discord.HTTPException:
            pass

        parsed = _parse_type_order(raw)
        if parsed is not None:
            # `mixed` is None, not a set: purity was answered on the previous
            # screen and written there. Sending a fresh answer nobody gave
            # would be a measurement we invented.
            offered = _squad_offer((None, None, None), parsed, None)
            await _ask_or_write(interaction, player, offered, source="observed")
            return

        if attempt == 0:
            await interaction.followup.send(
                f"⚠️ I couldn't read **{discord.utils.escape_markdown(raw)[:60]}** as "
                f"three squad types. Name all three in box order, like "
                f"**Tank, Tank, Aircraft**. Try again.",
                ephemeral=True,
            )

    await interaction.followup.send(
        f"⚠️ Still couldn't read that as three squad types, so none were saved "
        f"for **{_label(player)}**. Their squad powers are saved. Run "
        f"`{CHAMPION_DUEL_HUB_CMD}` → **{CD_BTN_FIND}** → **{CD_BTN_SQUADS}** "
        f"to try again.",
        ephemeral=True,
    )


# ── Record a line-up (Premium) ────────────────────────────────────────────────


class _OrderSelectView(discord.ui.View):
    """The six permutations in one select, plus a confirm.

    Select-then-confirm rather than acting on change, because a mis-tap on a
    phone would otherwise file a sighting nobody can see to correct.
    """

    def __init__(self, *, player: dict, opponent: str | None, user_id: int):
        super().__init__(timeout=300)
        self.player = player
        self.opponent = opponent
        self.user_id = user_id
        self.choice: tuple[str, str, str] | None = None
        self.message: discord.Message | None = None

        self.select = discord.ui.Select(
            placeholder="Which order did they deploy in?",
            options=[
                discord.SelectOption(label=" → ".join(order), value=str(i))
                for i, order in enumerate(ORDERS)
            ],
            row=0,
        )
        self.select.callback = self._on_select
        self.add_item(self.select)

        self.confirm = discord.ui.Button(
            label="Record this order", style=discord.ButtonStyle.success, disabled=True, row=1
        )
        self.confirm.callback = self._on_confirm
        self.add_item(self.confirm)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    async def _on_select(self, inter: discord.Interaction):
        self.choice = ORDERS[int(self.select.values[0])]
        self.confirm.disabled = False
        # Keep the pick visible after the menu closes: on mobile the select
        # collapses back to its placeholder, and an unlabelled confirm button
        # is then asking the user to remember what they tapped.
        self.select.placeholder = " → ".join(self.choice)
        await inter.response.edit_message(view=self)

    async def _on_confirm(self, inter: discord.Interaction):
        if self.choice is None:  # pragma: no cover - the button is disabled until then
            await inter.response.send_message("⚠️ Pick an order first.", ephemeral=True)
            return
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(view=self)
        try:
            await asyncio.to_thread(
                db.add_order,
                self.player["id"],
                list(self.choice),
                actor=_actor(inter),
                opponent=self.opponent,
            )
        except (ValueError, LookupError) as exc:
            await inter.followup.send(f"⚠️ Couldn't record that: {exc}", ephemeral=True)
            self.stop()
            return

        top = await asyncio.to_thread(db.most_common_order, self.player["id"])
        tail = ""
        if top:
            tail = (
                f"\nMost recorded for them: **{' → '.join(top['order'])}**, "
                f"{_order_share(top['seen'], top['total']).lower()}."
            )
        await inter.followup.send(
            f"✅ Recorded **{' → '.join(self.choice)}** for **{_label(self.player)}**.{tail}",
            ephemeral=True,
        )
        self.stop()


# ── The capture guide ─────────────────────────────────────────────────────────

_GUIDE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "champion_duel")

# Alt text rides on the attachment (WCAG 2.2 AA 1.1.1). These images are
# entirely instructional — the whole content is text and arrows over a
# screenshot — so "annotated screenshot" would convey nothing. Each description
# states what the markers point at and what the numbers are, well enough to
# follow without seeing them. Kept beside the filenames so adding an image
# without a description is visibly incomplete rather than quietly inaccessible.
GUIDE_IMAGES = {
    "guide_order.png": (
        "Battle report screenshot, scrolled to Round 1. Three panels are "
        "outlined and numbered 1, 2 and 3 down the screen; each shows one squad "
        "of five heroes facing the opponent's."
    ),
    "guide_squad.png": (
        "Battle report screenshot for a single squad, with three outlined "
        "areas numbered down the screen. 1 is the header carrying both player "
        "names. 2 is the row beside the word Overview showing each side's "
        "power, 84.6M and 81.3M. 3 is a row of five vehicle icons in the "
        "Lineup section."
    ),
}

# The instructions are Discord text, not pixels. Text is selectable,
# translatable, resizes with the reader's settings and is read aloud natively;
# words burned into a screenshot are none of those things, and an image full of
# annotations reads like a developer marking up a ticket rather than a guide.
# Each image only has to say *where*, and its numbers key it to these lines.
GUIDE_SECTIONS = (
    {
        "image": "guide_order.png",
        "title": "Deployment Order",
        "body": ("1. The squad in Slot 1.\n2. The squad in Slot 2.\n3. The squad in Slot 3."),
    },
    {
        "image": "guide_squad.png",
        "title": "Recording Player Squad Information",
        "body": (
            "Enter this information for all 3 squads in the line-up.\n\n"
            "1. This shows who is on each side of the battle. Enter their names "
            "(best to copy from in-game).\n"
            "2. Enter the Power listed for each squad.\n"
            "3. Remember the troop type for each squad. If mixed, log as the "
            "type that has the most heroes present."
        ),
    },
)

GUIDE_FOOTER = "Screens shown with permission from the players in them."


def guide_files() -> list[discord.File]:
    """The annotated screenshots, or an empty list if they aren't deployed.

    Missing assets degrade to the words alone rather than failing the button —
    the text carries the answer and the pictures make it fast, which is the
    right way round for something that must not break.
    """
    files = []
    for name, description in GUIDE_IMAGES.items():
        path = os.path.join(_GUIDE_DIR, name)
        if os.path.isfile(path):
            files.append(discord.File(path, filename=name, description=description))
    return files


def build_guide() -> tuple[list[discord.Embed], list[discord.File]]:
    """One embed per step, each with its own image directly beneath its words.

    Two embeds rather than one message with both pictures at the bottom: a
    numbered list is useless if the thing it numbers is two screens away, and
    Discord stacks attachments after all the text.

    An embed whose image is missing still renders its instructions, so a
    partial deployment loses the picture and keeps the guide.
    """
    files = guide_files()
    present = {file.filename for file in files}

    embeds = []
    for section in GUIDE_SECTIONS:
        embed = discord.Embed(
            title=section["title"],
            description=section["body"],
            colour=discord.Colour.blurple(),
        )
        if section["image"] in present:
            embed.set_image(url=f"attachment://{section['image']}")
        embeds.append(embed)
    embeds[-1].set_footer(text=GUIDE_FOOTER)
    return embeds, files


# ── Admin: browse, revert, export ─────────────────────────────────────────────


def build_edits_embed(result: dict, shown: int) -> discord.Embed:
    embed = discord.Embed(
        title="📜 Champion Duel: recent edits",
        description="\n".join(_describe(e) for e in result["edits"])[:4096],
        color=discord.Color.blurple(),
    )
    embed.set_footer(
        text=(
            f"Showing {shown} of {result['total']}. "
            f"Use {CD_BTN_EXPORT} for a spreadsheet, or {CD_BTN_REVERT} to put one back."
        )
    )
    return embed


class _EditsFilterModal(discord.ui.Modal, title="Filter Champion Duel edits"):
    player = discord.ui.TextInput(label="Player name", required=False, max_length=64)
    actor = discord.ui.TextInput(
        label="Actor's Discord ID", required=False, max_length=32, placeholder="e.g. 461845428…"
    )
    limit = discord.ui.TextInput(
        label=f"How many (max {BROWSE_MAX})", required=False, max_length=3, placeholder="10"
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            limit = int((self.limit.value or "").strip() or 10)
        except ValueError:
            limit = 10
        await _send_edits(
            interaction,
            player=(self.player.value or "").strip() or None,
            actor=(self.actor.value or "").strip() or None,
            limit=limit,
        )


class _EditsView(discord.ui.View):
    """The listing's own filter control. The common case — "what happened
    lately" — stays one click; narrowing costs a second one."""

    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.message: discord.Message | None = None
        button = discord.ui.Button(label=CD_BTN_FILTER, style=discord.ButtonStyle.secondary)
        button.callback = self._on_filter
        self.add_item(button)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    async def _on_filter(self, inter: discord.Interaction):
        await inter.response.send_modal(_EditsFilterModal())


async def _send_edits(interaction, *, player=None, actor=None, limit=10):
    """Shared by the button and its filter modal — both have already deferred."""
    result = await asyncio.to_thread(
        db.list_edits,
        player=player,
        actor=actor,
        limit=max(1, min(limit, BROWSE_MAX)),
    )
    if not result["edits"]:
        await interaction.followup.send("No edits match that.", ephemeral=True)
        return
    view = _EditsView(interaction.user.id)
    await interaction.followup.send(
        embed=build_edits_embed(result, len(result["edits"])), view=view, ephemeral=True
    )
    view.message = await interaction.original_response()


class _RevertAnyway(discord.ui.View):
    """The `force` flag, as a button on the conflict that provoked it.

    Better than the old `force: True` parameter: nobody can set it before
    seeing what they would be overwriting, which is the only moment the
    decision can be made well.
    """

    def __init__(self, *, edit_id: int, user_id: int, current: str):
        super().__init__(timeout=120)
        self.edit_id = edit_id
        self.user_id = user_id
        self.current = current

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    @discord.ui.button(label="⏪ Revert anyway", style=discord.ButtonStyle.danger)
    async def force(self, inter: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(view=self)
        await _do_revert(inter, self.edit_id, force=True)
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(
            content=f"↩️ Left it as **{self.current}**.", view=self, embed=None
        )
        self.stop()


async def _do_revert(interaction: discord.Interaction, edit_id: int, *, force: bool):
    """Apply a revert and report it. The caller has already responded."""
    try:
        result = await asyncio.to_thread(
            db.revert_edit, edit_id, actor=_actor(interaction), force=force
        )
    except db.RevertConflict as exc:
        # Refusing is the point: two scouts entering sightings for one player is
        # normal, and the later entry is usually the better information. Show
        # what's there now and let the admin decide.
        await interaction.followup.send(
            f"⚠️ Edit `#{edit_id}` wasn't reverted. That value has changed since.\n"
            f"It's now **{exc.current}**, but the edit expected **{exc.expected}**.\n"
            f"Someone may have corrected it more recently.",
            view=_RevertAnyway(
                edit_id=edit_id, user_id=interaction.user.id, current=str(exc.current)
            ),
            ephemeral=True,
        )
        return
    except LookupError:
        await interaction.followup.send(f"⚠️ No edit `#{edit_id}`.", ephemeral=True)
        return
    except ValueError as exc:
        await interaction.followup.send(f"⚠️ Can't revert that: {exc}", ephemeral=True)
        return

    await interaction.followup.send(
        f"✅ Reverted `#{edit_id}`. Restored to **{result['restored_to'] or '(none)'}**.\n"
        f"Logged as edit `#{result['edit_id']}`; nothing was deleted.",
        ephemeral=True,
    )


class _RevertModal(discord.ui.Modal, title="Revert a Champion Duel edit"):
    edit_id = discord.ui.TextInput(
        label="Edit ID", max_length=12, placeholder="The #id from Recent edits"
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            edit_id = int(self.edit_id.value.strip().lstrip("#"))
        except (ValueError, AttributeError):
            await interaction.followup.send(
                f"⚠️ That isn't an edit ID. It's the number after `#` in {CD_BTN_EDITS}.",
                ephemeral=True,
            )
            return
        await _do_revert(interaction, edit_id, force=False)


EXPORT_COLUMNS = [
    "id",
    "created_at",
    "target",
    "registrant_id",
    "display_name",
    # Server and group ride along so a spreadsheet can tell two players with the
    # same name on different servers apart -- the whole reason identity is
    # (name, server) rather than the name alone.
    "server",
    "grp",
    "slot",
    "field",
    "old_value",
    "new_value",
    "actor_discord_id",
    "actor_name",
    "actor_guild_id",
    "revert_of",
]


def build_export_csv(rows: list[dict]) -> io.BytesIO:
    """utf-8-sig so Excel opens non-ASCII player names correctly instead of
    rendering mojibake — these names routinely carry non-Latin scripts."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return io.BytesIO(buf.getvalue().encode("utf-8-sig"))


class _ExportModal(discord.ui.Modal, title="Export Champion Duel edits"):
    start = discord.ui.TextInput(label="Start date", placeholder="YYYY-MM-DD", max_length=10)
    end = discord.ui.TextInput(
        label="End date (inclusive)", placeholder="YYYY-MM-DD", max_length=10
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        start_iso = _parse_day(self.start.value, end_of_day=False)
        end_iso = _parse_day(self.end.value, end_of_day=True)
        if not start_iso or not end_iso:
            await interaction.followup.send(
                "⚠️ Dates need to be `YYYY-MM-DD`, for example `2026-08-12`.", ephemeral=True
            )
            return
        if start_iso > end_iso:
            await interaction.followup.send(
                "⚠️ The start date is after the end date.", ephemeral=True
            )
            return

        rows = await asyncio.to_thread(db.export_edits, start_iso, end_iso)
        if not rows:
            await interaction.followup.send(
                f"No edits between {self.start.value} and {self.end.value}.", ephemeral=True
            )
            return
        await interaction.followup.send(
            f"{len(rows)} edit(s) between {self.start.value} and {self.end.value}.",
            file=discord.File(
                build_export_csv(rows),
                filename=f"champion_duel_edits_{self.start.value}_to_{self.end.value}.csv",
            ),
            ephemeral=True,
        )


# ── Warzone and grouping onboarding ───────────────────────────────────────────
#
# Champion Duel structure is per grouping: the 16 warzones drawn together, shown
# in game as one line at the bottom of the Match Overview box. Everything this
# hub says about rounds, groups and dates belongs to one of them, so the hub has
# to know which before it can say any of it.
#
# One number gets there. A warzone is in at most one grouping per Champion Duel,
# so the ask is a warzone rather than sixteen -- and a warzone is durable where a
# grouping is not, which is why the answer keeps working next season.


def _mm_warzone(guild_id) -> str | None:
    """The warzone from this alliance's Map Manager link, if it has one.

    Read here rather than in `champion_duel_db`, which is deliberately
    independent of `config`: the link lives in `guild_configs.db`, and reaching
    across from the tournament database would tie global tournament data to
    per-guild config in exactly the way keeping them in separate files avoids.

    The column is INTEGER there and TEXT here, which is the kind of boundary
    that silently matches nothing; `parse_warzones` reconciles it.
    """
    import config

    mapping = config.get_guild_alliance_mapping(int(guild_id)) or {}
    zones = db.parse_warzones(str(mapping.get("server") or ""))
    return zones[0] if zones else None


async def _grouping_state(interaction: discord.Interaction) -> tuple[dict | None, str | None]:
    """(the caller's grouping, the warzone it resolved from), either may be None.

    Both, because the two unresolved states are different surfaces. An alliance
    that has told us nothing has to be asked. One whose warzone is in no grouping
    we hold has already answered, and needs somebody to enter that grouping
    instead -- asking them again for a number they already gave would be the
    surface failing to say what is actually missing.
    """
    guild_id = interaction.guild_id
    if not guild_id:
        return (None, None)
    pinned = await asyncio.to_thread(db.get_guild_warzone, str(guild_id))
    warzone = (pinned or {}).get("warzone") or await asyncio.to_thread(_mm_warzone, guild_id)
    if not warzone:
        return (None, None)
    grouping = await asyncio.to_thread(
        db.resolve_grouping_for_guild, str(guild_id), fallback_warzone=warzone
    )
    return (grouping, str(warzone))


# Past-leaning, per `messages.DATE_PARSE_REJECT`'s note that the example list is
# the caller's to tailor: the Sign-up stage has already run by the time its date
# can be read off the Match Overview box. `today` and `yesterday` parse but are
# left out of the hint for the same reason -- the date wanted here is up to a
# whole event ago.
_START_DATE_EXAMPLES = "`Aug 4`, `8/4`, or `2026-08-04`"


def parse_start_date(text, *, today=None) -> str | None:
    """The Sign-up stage's start date as an ISO string, or None if unreadable.

    Runs the same permissive parser every other date surface in the bot uses, so
    `8/4`, `Aug 4`, `4 August`, `2026-08-04` and `2026.08.04` all land here the
    way they land in a storm date. Nobody should have to learn a second date
    format for one modal.

    One correction on top of it. `parse_event_date` infers a **forward** year for
    a date typed without one, which is right for a storm being scheduled and
    wrong here: a Champion Duel's Sign-up stage has already run by the time the
    Match Overview box can be read, so `8/4` typed on 8/15 would otherwise become
    next August. A year-less date takes the nearest occurrence instead. A year
    the user actually typed is never second-guessed.
    """
    import re

    from storm_date_helpers import parse_event_date

    raw = str(text or "").strip()
    today = today or _server_today()
    parsed = parse_event_date(raw, today=today)
    if parsed is None:
        return None
    if not re.search(r"\d{4}", raw):
        try:
            earlier = parsed.replace(year=parsed.year - 1)
        except ValueError:  # 29 February, and the year before is not a leap year
            earlier = None
        if earlier and abs((earlier - today).days) < abs((parsed - today).days):
            parsed = earlier
    return parsed.isoformat()


def _server_today():
    """Today's in-game date. Every date on these surfaces is a game date, and
    `UX.md` is explicit that game time is not local time."""
    from config import server_date_for

    return server_date_for(datetime.now(timezone.utc))


def _plural(count: int, singular: str, plural: str | None = None) -> str:
    """`1 warzone`, `16 warzones`. The count and its noun, agreeing.

    Worth a helper rather than an f-string each time: "across 1 warzones" is
    the kind of thing that reads as machine output and turns up in three
    surfaces at once because each was written separately.
    """
    return f"{count} {singular if count == 1 else (plural or singular + 's')}"


def _grouping_option_label(grouping: dict) -> str:
    """One grouping in a picker: the date it started, which is its only handle.

    Nothing gives a Champion Duel a name, so the start date is what a member
    recognises theirs by -- it is the one on their own Match Overview box. The
    id would be exact and mean nothing to anybody.
    """
    started = _short_date(grouping.get("started_on"))
    return f"Started {started}" if started else f"Champion Duel {grouping['id']} (no date recorded)"


def _grouping_name(grouping: dict | None, *, whose: str = "your") -> str:
    """Ours named so a member can tell which one is meant.

    **Never says "grouping".** The game uses that word for the group of 8 a
    player is drawn into ("Semi-final Grouping: Group H") and calls the 16
    warzones Participating Warzones, so the one meaning a member has already
    learned for it is the one we do not mean. `UX.md`'s term table asserted the
    opposite until 2026-08-16; the correction is under Settled there.

    That leaves the start date as the whole name, which it already was: nothing
    in the game gives a Champion Duel a title, and the date is the one handle a
    member can check against their own Match Overview box.

    Falls back to the bare phrase when no date is stored. An import can
    establish one before anyone has read its dates, and a name with a blank
    where the date goes is worse than no date at all.
    """
    started = _short_date((grouping or {}).get("started_on"))
    if not started:
        return f"{whose} Champion Duel"
    return f"{whose} Champion Duel that started {started}"


def _warzone_list(zones) -> str:
    """A grouping's warzones as one line, in the numeric order they are stored.

    Sixteen bare numbers fit on a phone line and the reader is scanning for
    their own, which is the same reason the hub lists servers bare.
    """
    return ", ".join(str(z) for z in zones)


def _short_date(value) -> str:
    """A date the way the game prints it: `8/4`, no leading zeros and no year.

    The year is dropped because every date on these surfaces is inside one
    27-day event, and the number a member is comparing against is the one on
    the Match Overview box, which has no year on it either.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value[:10]).date()
        except ValueError:  # pragma: no cover - a hand-edited row
            return value
    return f"{value.month}/{value.day}"


def _typed(value: str, limit: int = 32) -> str:
    """A user's own input, echoed back into an error, clamped.

    Errors name what was typed so the user can see which of the two fields was
    the wrong one, and a paste of sixteen warzones is well past what an embed
    should repeat back.
    """
    text = " ".join(str(value or "").split())
    return text[:limit] + ("…" if len(text) > limit else "")


def build_onboarding_embed(*, servers: list[dict], warzone: str | None) -> discord.Embed:
    """The third hub state: we do not know which grouping this alliance is in.

    The global "what we hold" line stays, because before we know who they are it
    is the only honest thing to say -- and it is the thing that shows the ask is
    worth answering rather than a form for its own sake.
    """
    embed = discord.Embed(title=CHAMPION_DUEL_HUB_TITLE, color=discord.Color.blurple())
    total = sum(s["registrants"] for s in servers)
    held = (
        f"We currently have **{total}** players across **{_plural(len(servers), 'warzone')}**.\n\n"
        if total
        else ""
    )
    if warzone:
        embed.description = (
            f"{held}"
            f"Your alliance is on warzone **{warzone}**. We do not currently know what "
            f"warzones you are matched with for this Champion Duel. Please add your "
            f"**Participating Warzones**. The game lists them at the bottom of the "
            f"Match Overview box."
        )[:4096]
    else:
        embed.description = (
            f"{held}"
            f"Which warzone is your alliance on? Champion Duel matches "
            f"{db.GROUPING_SIZE} warzones together, and all of the data will be unique "
            f"to yours. Add your warzone and we will either match you to a Champion "
            f"Duel we already hold or ask you for the other participating warzones."
        )[:4096]
    return embed


class ChampionDuelOnboardingView(discord.ui.View):
    """Set a warzone, or enter the grouping it belongs to.

    **Add a grouping renders disabled until the warzone is known**, rather than
    absent. It is the second half of one job and the embed says what unlocks it,
    so hiding it would leave the surface looking like a dead end with one button
    on it. Live and failing validation would be worse: `notes/DESIGN.md` says a
    control that cannot change anything under current conditions is disabled with
    the reason, not left inert.
    """

    def __init__(self, *, user_id: int, can_write: bool, warzone: str | None):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.can_write = can_write
        self.warzone = warzone
        self.message: discord.Message | None = None

        known = bool(warzone)
        self._add(
            CD_BTN_CHANGE_WARZONE if known else CD_BTN_SET_WARZONE,
            discord.ButtonStyle.secondary if known else discord.ButtonStyle.primary,
            self._on_warzone,
        )
        self._add(
            CD_BTN_ADD_GROUPING,
            discord.ButtonStyle.primary if known else discord.ButtonStyle.secondary,
            self._on_add_grouping,
            disabled=not known,
        )

    def _add(self, label, style, cb, *, disabled=False):
        button = discord.ui.Button(label=label[:80], style=style, row=0, disabled=disabled)
        button.callback = cb
        self.add_item(button)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    async def _on_warzone(self, inter: discord.Interaction):
        await inter.response.send_modal(
            _WarzoneModal(can_write=self.can_write, current=self.warzone)
        )

    async def _on_add_grouping(self, inter: discord.Interaction):
        await inter.response.send_modal(
            _AddGroupingModal(can_write=self.can_write, warzone=self.warzone)
        )


class _WarzoneModal(discord.ui.Modal, title="Your alliance's warzone"):
    """One number, which is all it takes to find the grouping.

    A warzone rather than a grouping, because a warzone is durable and a
    grouping is not: the sixteen change every Champion Duel and the number does
    not, so this answer keeps resolving next season with nobody re-pinning
    anything.
    """

    def __init__(self, *, can_write: bool, current: str | None = None):
        super().__init__()
        self.can_write = can_write
        self.current = current
        # Safe to set on self: `Modal._init_children` deepcopies each declared
        # item onto the instance, so a default cannot leak to the next opener.
        if current:
            self.warzone.default = current[:10]

    warzone = discord.ui.TextInput(label="Warzone number", max_length=10, placeholder="e.g. 738")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.guild_id:
            await interaction.followup.send(_WARZONE_NEEDS_A_SERVER, ephemeral=True)
            return

        zones = db.parse_warzones(self.warzone.value)
        if len(zones) != 1:
            await interaction.followup.send(
                f"⚠️ **{_typed(self.warzone.value, 16)}** is not a warzone number. A "
                f"warzone is the number your alliance plays on, like 738. Try again.",
                ephemeral=True,
            )
            return

        zone = zones[0]
        # Changing an existing answer repoints every member of this server at a
        # different grouping, so it is confirmed and the confirmation names both
        # numbers. Setting one for the first time changes nothing that was there.
        if self.current and zone != self.current:
            view = _ChangeWarzoneView(
                user_id=interaction.user.id,
                can_write=self.can_write,
                current=self.current,
                proposed=zone,
            )
            await interaction.followup.send(
                f"⚠️ Your alliance is set to warzone **{self.current}**. Changing it to "
                f"**{zone}** points everyone on this server at a different Champion Duel.",
                view=view,
                ephemeral=True,
            )
            view.message = await interaction.original_response()
            return

        await _pin_warzone(interaction, zone, can_write=self.can_write)


_WARZONE_NEEDS_A_SERVER = (
    "⚠️ A warzone is remembered for a whole Discord server, so this only works "
    "inside one. Run `/champion_duel` in your alliance's server."
)


class _ChangeWarzoneView(discord.ui.View):
    """The confirm half of changing a warzone that was already answered."""

    def __init__(self, *, user_id: int, can_write: bool, current: str, proposed: str):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.can_write = can_write
        self.current = current
        self.proposed = proposed
        self.message: discord.Message | None = None

        for label, style, cb in (
            (CD_BTN_CHANGE_YES, discord.ButtonStyle.success, self._on_yes),
            (CD_BTN_CANCEL, discord.ButtonStyle.secondary, self._on_no),
        ):
            button = discord.ui.Button(label=label[:80], style=style, row=0)
            button.callback = cb
            self.add_item(button)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    async def _on_yes(self, inter: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(view=self)
        await _pin_warzone(inter, self.proposed, can_write=self.can_write)
        self.stop()

    async def _on_no(self, inter: discord.Interaction):
        # A backpedal, not a cancelled flow: the warzone they had is untouched,
        # and the detail sentence is what says so (`messages.CANCEL_BACKPEDAL`).
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(
            content=CANCEL_BACKPEDAL.format(
                detail=f"Your alliance is still on warzone **{self.current}**."
            ),
            embed=None,
            view=self,
        )
        self.stop()


class _ConfirmWarzoneView(discord.ui.View):
    """Once per Champion Duel, check the warzone we resolved from is still right.

    An alliance that moves warzone still resolves, silently and to the wrong
    grouping: the old number keeps existing and keeps getting drawn into
    somebody's draw. Nothing in the data can tell the two apart, so the answer is
    re-confirmed when the grouping changes rather than trusted forever.
    """

    def __init__(self, *, user_id: int, can_write: bool, warzone: str, grouping: dict):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.can_write = can_write
        self.warzone = warzone
        self.grouping = grouping
        self.message: discord.Message | None = None

        for label, style, cb in (
            (CD_BTN_WARZONE_YES, discord.ButtonStyle.success, self._on_yes),
            (CD_BTN_WARZONE_NO, discord.ButtonStyle.secondary, self._on_no),
        ):
            button = discord.ui.Button(label=label[:80], style=style, row=0)
            button.callback = cb
            self.add_item(button)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    async def _on_yes(self, inter: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(view=self)
        await asyncio.to_thread(
            db.set_guild_warzone,
            str(inter.guild_id),
            self.warzone,
            discord_id=str(inter.user.id),
            confirmed_grouping_id=self.grouping["id"],
        )
        await _open_hub(inter, can_write=self.can_write)
        self.stop()

    async def _on_no(self, inter: discord.Interaction):
        # Straight into the modal, which cannot follow an edit_message on the
        # same interaction. The stale buttons behind it are answered by whatever
        # the modal produces, and a cancelled modal leaves the question standing,
        # which is the honest state: it has not been answered yet.
        await inter.response.send_modal(
            _WarzoneModal(can_write=self.can_write, current=self.warzone)
        )


def build_confirm_warzone_embed(*, warzone: str, grouping: dict) -> discord.Embed:
    started = _short_date(grouping.get("started_on"))
    # A grouping can exist before anyone has read its dates, so the second
    # sentence has a version that claims nothing about when it began.
    began = (
        f"Your Champion Duel is already set and began on **{started}**."
        if started
        else "Your Champion Duel is already set."
    )
    return discord.Embed(
        title=CHAMPION_DUEL_HUB_TITLE,
        description=(
            f"A new Champion Duel has begun. We currently have your alliance set as "
            f"warzone **{warzone}**. {began}\n\n"
            f"Are you still in warzone **{warzone}**?"
        )[:4096],
        color=discord.Color.blurple(),
    )


async def _pin_warzone(interaction: discord.Interaction, zone: str, *, can_write: bool) -> None:
    """Store the guild's warzone, then show whichever state it resolved to.

    Pinned before resolving succeeds, not after. An alliance whose grouping
    nobody has entered has still given us a true answer, and losing it would
    mean asking again on the way to the surface that fixes it.
    """
    grouping = await asyncio.to_thread(db.find_grouping_by_warzone, zone)
    await asyncio.to_thread(
        db.set_guild_warzone,
        str(interaction.guild_id),
        zone,
        discord_id=str(interaction.user.id),
        confirmed_grouping_id=grouping["id"] if grouping else None,
    )
    await _open_hub(
        interaction,
        can_write=can_write,
        note=f"✅ Set your alliance to warzone **{zone}**.",
    )


class _AddGroupingModal(discord.ui.Modal, title="Add your Participating Warzones"):
    """The 16 warzones and the day it started, which is the whole grouping.

    Two fields because that is everything the game shows: the Participating
    Warzone line and the Sign-up stage's start date. From those two the hub
    derives every round, every window and every date it will ever state, so
    nobody has to come back and tell it the event moved on.

    Both fields take defaults so a refusal can hand back what was typed. Sixteen
    numbers copied off a phone screen is not something anyone should retype
    because one of them was a digit out.
    """

    def __init__(
        self,
        *,
        can_write: bool,
        warzone: str | None,
        warzones_default: str | None = None,
        started_default: str | None = None,
    ):
        super().__init__()
        self.can_write = can_write
        self.warzone = warzone
        # Safe to set on self: `Modal._init_children` deepcopies each declared
        # item onto the instance, so a default cannot leak to the next opener.
        if warzones_default:
            self.warzones.default = warzones_default[:200]
        if started_default:
            self.started_on.default = started_default[:20]

    warzones = discord.ui.TextInput(
        label="The participating warzones, all 16",
        style=discord.TextStyle.paragraph,
        max_length=200,
        placeholder="#773, #800, #744, ...",
    )
    started_on = discord.ui.TextInput(
        label="Sign-up stage start date",
        max_length=20,
        placeholder="e.g. 8/4, Aug 4, or 2026-08-04",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if not interaction.guild_id:
            await interaction.followup.send(_WARZONE_NEEDS_A_SERVER, ephemeral=True)
            return

        started = parse_start_date(self.started_on.value)
        if started is None:
            # The shared rejection, plus the one sentence that is this feature's
            # rather than every date surface's. `events_hub` appends its route
            # back the same way.
            await self._refuse(
                interaction,
                DATE_PARSE_REJECT.format(
                    # The field's own cap, so a date is never echoed back
                    # truncated. Clamped at all only because nothing guarantees
                    # Discord enforced `max_length` before this ran.
                    raw=_typed(self.started_on.value, 20),
                    examples=_START_DATE_EXAMPLES,
                )
                + " The Sign-up stage's start date is at the top of the Match Overview"
                " box in game.",
            )
            return

        typed = db.parse_warzones(self.warzones.value, unique=False)
        zones = sorted(set(typed), key=int)
        repeated = next((z for z in zones if typed.count(z) > 1), None)
        if repeated is not None:
            await self._refuse(
                interaction,
                f"⚠️ Warzone **{repeated}** is in that list twice. Your Participating Warzones are "
                f"{db.GROUPING_SIZE} different warzones. Try again.",
            )
            return
        if len(zones) != db.GROUPING_SIZE:
            await self._refuse(
                interaction,
                f"⚠️ That is **{_plural(len(zones), 'warzone')}**. Participating Warzones are "
                f"exactly **{db.GROUPING_SIZE}**, listed together at the bottom of the "
                f"Match Overview box in game. Try again.",
            )
            return

        # The caller's own warzone has to be in the set. If it is not, one of the
        # two answers is off and there is no way to tell which from here -- and
        # pinning a guild to a grouping it is not in is the exact silent failure
        # the grouping separation exists to stop.
        if self.warzone and self.warzone not in zones:
            await self._refuse(
                interaction,
                f"⚠️ Your alliance's warzone, **{self.warzone}**, is not in that list. "
                f"Either a warzone is missing from it or the warzone we have for your "
                f"alliance is incorrect. Check both, then try again.",
            )
            return

        overlaps = await asyncio.to_thread(db.overlapping_groupings, zones, started)
        exact = next((g for g, _ in overlaps if set(g["warzones"]) == set(zones)), None)
        if exact is None and overlaps:
            await self._report_conflict(interaction, overlaps[0], zones, started)
            return

        if exact is not None:
            grouping = exact
            note = (
                f"ℹ️ Those Participating Warzones have already been entered.\n"
                f"The {db.GROUPING_SIZE} warzones: {_warzone_list(exact['warzones'])}."
            )
        else:
            grouping = await asyncio.to_thread(
                db.create_grouping,
                zones,
                started,
                origin="member",
                guild_id=str(interaction.guild_id),
                discord_id=str(interaction.user.id),
            )
            note = (
                f"✅ Added your Participating Warzones, starting **{_short_date(started)}**.\n"
                f"The {db.GROUPING_SIZE} warzones: {_warzone_list(zones)}."
            )

        # Creating pins the guild as a side effect. They just told us their
        # sixteen; asking for the one they play on again would be asking for
        # something we already have.
        pin = self.warzone if self.warzone in zones else None
        if pin:
            await asyncio.to_thread(
                db.set_guild_warzone,
                str(interaction.guild_id),
                pin,
                discord_id=str(interaction.user.id),
                confirmed_grouping_id=grouping["id"],
            )
        await _open_hub(interaction, can_write=self.can_write, note=note)

    async def _refuse(self, interaction: discord.Interaction, message: str) -> None:
        """Say what is wrong, and hand back what was typed.

        A validation failure costs one step, not the whole flow (`UX.md`), and
        without the retry button "try again" means retyping sixteen numbers off a
        phone screen to fix one of them.
        """
        view = _RetryGroupingView(
            user_id=interaction.user.id,
            can_write=self.can_write,
            warzone=self.warzone,
            warzones_default=self.warzones.value,
            started_default=self.started_on.value,
        )
        await interaction.followup.send(message, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def _report_conflict(
        self,
        interaction: discord.Interaction,
        overlap: tuple[dict, str],
        zones: list[str],
        started: str,
    ) -> None:
        """Both lists, side by side, and the two ways out.

        Naming the shared warzone is not enough on its own: it says one of the
        two lists has a mistake in it without showing the other one, so the
        reader has no way to work out which. Printing both is what makes the
        answer visible, and it is usually obvious at a glance.

        The exit depends on which list is wrong, and only the reader can tell.
        Theirs is one button away. The other belongs to somebody else, and
        overwriting another alliance's grouping on one person's say-so is an
        opinion the bot does not have (`UX.md` principle 6), so that half is a
        route to the operator rather than a control.
        """
        other, shared = overlap
        embed = discord.Embed(
            title=f"⚠️ Warzone {shared} is in two different lists",
            description=(
                f"A warzone is only ever drawn into one set of Participating Warzones, "
                f"so one of these two lists has a mistake in it. Nothing was saved.\n\n"
                f"**You entered**, starting {_short_date(started)}:\n"
                f"{_warzone_list(zones)}\n\n"
                f"**Already here**, starting {_short_date(other.get('started_on')) or 'an unknown date'}:\n"
                f"{_warzone_list(other['warzones'])}"
            )[:4096],
            color=discord.Color.orange(),
        )
        embed.add_field(
            name="If your list is the one to fix",
            value=f"Press **{_btn_words(CD_BTN_RETRY_GROUPING)}**. What you typed is kept.",
            inline=False,
        )
        embed.add_field(
            name="If the list already here is wrong",
            value=(
                f"Another alliance entered it, so it is not yours to change. Tell us on "
                f"the {COMMUNITY_SERVER_NAME} and we will correct it."
            ),
            inline=False,
        )
        view = _RetryGroupingView(
            user_id=interaction.user.id,
            can_write=self.can_write,
            warzone=self.warzone,
            warzones_default=self.warzones.value,
            started_default=self.started_on.value,
            offer_community=True,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()


class _RetryGroupingView(discord.ui.View):
    """Reopen the grouping modal with what was typed still in it.

    `offer_community` adds the second exit, and only the conflict has one: a
    miscounted list is entirely the caller's to fix, and a control that leads
    somewhere with nothing to do there is the same waste as one that cannot
    change anything.
    """

    def __init__(
        self,
        *,
        user_id: int,
        can_write: bool,
        warzone: str | None,
        warzones_default: str | None,
        started_default: str | None,
        offer_community: bool = False,
    ):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.can_write = can_write
        self.warzone = warzone
        self.warzones_default = warzones_default
        self.started_default = started_default
        self.message: discord.Message | None = None

        button = discord.ui.Button(
            label=CD_BTN_RETRY_GROUPING[:80], style=discord.ButtonStyle.primary
        )
        button.callback = self._on_retry
        self.add_item(button)
        if offer_community:
            # A link button rather than the URL in the field text: an invite is
            # one tap here and a thing to read and copy there, and this is a
            # phone surface.
            self.add_item(discord.ui.Button(label=CD_BTN_COMMUNITY[:80], url=COMMUNITY_SERVER_URL))

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    async def _on_retry(self, inter: discord.Interaction):
        await inter.response.send_modal(
            _AddGroupingModal(
                can_write=self.can_write,
                warzone=self.warzone,
                warzones_default=self.warzones_default,
                started_default=self.started_default,
            )
        )


# ── Recording a group ─────────────────────────────────────────────────────────
#
# One surface covers the qualifier standings and the semifinal group, because
# they are the same work: pick a round and a group, paste players, reconcile.
#
# It is not "enter eight". Rank is typed rather than derived from order, so an
# alliance can record just its own members' placements -- ranks 22, 25, 51, 87 --
# which is the question "which of my alliance's players placed where".
#
# A group is recorded twice over its life: once at the draw into `seed_rank`,
# once at the standings into `rank`. Which of the two an entry writes is
# explicit on the surface, the same argument that made the round explicit.
# Inferring it from "is the score zero" would silently misfile a draw entered
# late.


# What the two entries are called wherever a user sees them: the picker, the
# reconcile footer, and the acknowledgement. One table so the ack can echo the
# choice in the words it was offered in rather than paraphrasing it.
_RECORDING_LABELS = {"draw": "Initial Seed", "final": "Final Standings"}

# What a line resolved to. `problem` is a parse failure, `skipped` is the user
# deciding this one is not worth chasing; both are excluded from the write and
# neither blocks it.
_UNRESOLVED = ("ambiguous", "problem")

_LINE_PROBLEMS = {
    "no_name": "no name on this line",
    "bad_server": "the warzone slot is not a number",
    "bad_rank": "the rank is not a number",
    "bad_thp": "the total hero power is not a number",
    "bad_score": "the score is not a number",
    # Every number on the line is readable and there is still more than one way
    # to read them, which happens when nothing structural breaks the tie. The
    # parser does not guess at these; it says so and they come here.
    "bad_numbers": "I can't tell which number is which",
}


def _resolve_line(row: dict) -> dict:
    """Attach a registrant to one parsed line, or say why it could not be.

    Never matches silently across warzones. Identity is name plus warzone, so a
    line naming a warzone we have no such player on is a new player rather than
    the same name somewhere else -- that is two people, and merging them is
    unrecoverable.
    """
    if row.get("problem"):
        row["state"] = "problem"
        return row
    matches = db.find_registrants(row["name"], row.get("server"))
    if len(matches) == 1:
        row["state"], row["registrant_id"] = "matched", matches[0]["id"]
    elif matches:
        row["state"], row["candidates"] = "ambiguous", matches
    else:
        row["state"] = "new"
    return row


def _line_summary(rows: list[dict]) -> str:
    counts = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    parts = []
    for state, word in (
        ("matched", "matched"),
        ("ambiguous", "needs a decision"),
        ("problem", "can't be read"),
        ("new", "new"),
        ("skipped", "skipped"),
    ):
        if counts.get(state):
            parts.append(f"{counts[state]} {word}")
    # No "N still have no hero power" count here, and it was written and taken
    # back out. `group_advance_odds` refuses a player with neither a power NOR
    # any squad power, and `find_registrants` returns registrant columns only --
    # so the count could see the power and not the squads, and would have named
    # players the odds are perfectly happy with. Answering it properly needs a
    # squad lookup for the whole paste; the per-line power in `_line_row` is
    # what a human checks in the meantime.
    return " · ".join(parts)


def _line_row(row: dict, *, stage: str | None = None, recording: str | None = None) -> str:
    """One line of the reconcile list, as it will be saved."""
    rank = str(row["rank"]) if row.get("rank") is not None else "–"
    name = row.get("name") or row.get("raw") or ""
    warzone = f"  #{row['server']}" if row.get("server") else ""
    # Rendered the way the game writes it and the way the Add a player
    # placeholder asks for it, rather than as nine digits: this row is read on a
    # phone next to a warzone and a score, and it is here to be checked at a
    # glance rather than audited.
    thp = f"  ·  {row['thp'] / 1_000_000:,.1f}M" if row.get("thp") else ""
    score = f"  ·  {row['score']:,}" if row.get("score") is not None else ""
    # A knockout placement is the match they went out in, and that is what a
    # reader can actually check against what they watched. The seed order is
    # just a position, so the draw gets no such gloss.
    if stage == "knockouts" and recording == "final":
        exit_round = db.knockout_result(row.get("rank"))
        score = f"  ·  {exit_round}" if exit_round else score
    if row["state"] == "matched":
        return f"`{rank:>3}` ✅ **{name}**{warzone}{thp}{score}"
    if row["state"] == "ambiguous":
        return f"`{rank:>3}` ❓ **{name}**: on {len(row['candidates'])} warzones, pick one"
    if row["state"] == "new":
        return f"`{rank:>3}` ➕ **{name}**{warzone}{thp}: new, will be added"
    if row["state"] == "skipped":
        return f"`{rank:>3}` ⏭️ ~~{name}~~ (skipped)"
    why = _LINE_PROBLEMS.get(row.get("problem"), "can't be read")
    return f"`  ?` ⚠️ `{_typed(row.get('raw'), 40)}`: {why}"


def build_reconcile_embed(*, rows: list[dict], stage: str, label, recording: str):
    """Every line and what it will do, before anything is written.

    Never a silent match. `AmbiguousPlayer` already carries its candidates so a
    caller can ask which rather than picking one, and this is that precedent
    applied to a paste rather than a new mechanism.
    """
    where = f"Group {label}" if label else db.STAGE_LABELS.get(stage, stage)
    lines = "\n".join(_line_row(row, stage=stage, recording=recording) for row in rows)
    embed = discord.Embed(
        # A noun phrase, per `notes/DESIGN.md`. The instruction is the first
        # line of the description, which is where a sentence belongs.
        title=f"👑 {where}",
        description=f"Check this before saving.\n\n{lines}"[:4096],
        color=discord.Color.blurple(),
    )
    embed.add_field(name="", value=_line_summary(rows), inline=False)
    # Eight names against a hundred-player qualifier group is deliberately
    # partial, so the count must not read as though something went missing.
    expected = db.GROUP_SIZE.get(stage)
    keeping = [r for r in rows if r["state"] not in _UNRESOLVED and r["state"] != "skipped"]
    if expected and len(keeping) < expected:
        embed.set_footer(
            text=(
                f"Recording {_plural(len(keeping), 'player')} for "
                f"{_RECORDING_LABELS[recording]}. If you want to add more, you can "
                f"at any time."
            )
        )
    return embed


class _RecordGroupModal(discord.ui.Modal, title="Record a group"):
    """Round, which entry this is, the group, and the players, in one surface.

    Three selects and a paragraph. This is the first modal in the tree to hold a
    select (`notes/DESIGN.md`, Selects inside modals), which is what collapses
    what would otherwise be a picker view in front of a typing modal.
    """

    def __init__(
        self,
        *,
        can_write: bool,
        grouping: dict,
        stage: str | None = None,
        groupings: list[dict] | None = None,
        warzone: str | None = None,
    ):
        super().__init__()
        self.can_write = can_write
        self.grouping = grouping
        self.groupings = groupings or [grouping]
        # Goes to the parser as a prior on which number is the warzone. Most
        # lines an alliance pastes are its own, and between this and the
        # grouping's sixteen a warzone typed as `2,308` stops being ambiguous.
        # Neither is a filter: a line naming a warzone we have never seen still
        # parses, it just stops being what settles an otherwise tied reading.
        self.warzone = warzone

        # Which Champion Duel this is for. A warzone is drawn into a new
        # grouping every season, so "the one running now" is only the right
        # answer while there is one -- and the finished hub invites people to
        # record past results, which is exactly when it is not.
        #
        # Removed rather than hidden when there is only one, so the common case
        # is not asked a question with a single answer. Declared first and
        # dropped in place, which keeps it above Round when it is there;
        # `add_item` would append it after the paragraph.
        if len(self.groupings) > 1:
            self.champion_duel.component.options = [
                discord.SelectOption(
                    label=_grouping_option_label(g),
                    value=str(g["id"]),
                    default=(g["id"] == grouping["id"]),
                )
                for g in self.groupings[:25]
            ]
        else:
            self.remove_item(self.champion_duel)

        # `stage` is passed in rather than read here: a modal constructor cannot
        # be async, and every DB call from a handler goes through
        # `asyncio.to_thread`. The caller already has the grouping in hand.
        #
        # Defaulted to the running round, which is what somebody recording
        # during the event almost always wants -- but still explicit, so a
        # backfill during the semifinals files against the qualifiers correctly.
        self.round_.component.options = [
            discord.SelectOption(label=db.STAGE_LABELS[key], value=key, default=(key == stage))
            for key in db.STAGES
        ]

    champion_duel = discord.ui.Label(
        text="Which Champion Duel?",
        component=discord.ui.Select(options=[discord.SelectOption(label="_", value="_")]),
    )
    round_ = discord.ui.Label(
        text="Round",
        component=discord.ui.Select(
            options=[discord.SelectOption(label=db.STAGE_LABELS[k], value=k) for k in db.STAGES]
        ),
    )
    # No help text on these two. The question is the label and the options are
    # the answer, so a description line would only restate what the picker
    # already shows. Options come from `_RECORDING_LABELS` so the picker, the
    # reconcile footer and the save acknowledgement all say the same words.
    recording = discord.ui.Label(
        text="What are you recording?",
        component=discord.ui.Select(
            options=[
                discord.SelectOption(label=_RECORDING_LABELS["final"], value="final", default=True),
                discord.SelectOption(label=_RECORDING_LABELS["draw"], value="draw"),
            ]
        ),
    )
    group = discord.ui.Label(
        text="What group is this for? (Leave blank for Knockout)",
        component=discord.ui.Select(
            min_values=0,
            max_values=1,
            options=[
                discord.SelectOption(label=letter, value=letter) for letter in db.GROUP_LABELS
            ],
        ),
    )
    # Total Hero Power is fourth, before the score, which is what lets the
    # score keep the tail of the line. The placeholder writes it the way the
    # game does, but it is an example and not a specification: `325,800,000`
    # and `325800000` read the same, and a line that stops early is fine.
    players = discord.ui.Label(
        text="Add one player per line",
        description="Name, Warzone, Rank, Total Hero Power, Score. Only the name is required.",
        component=discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=4000,
            placeholder="[OGV]Kestrel, 738, 1, 325.8M, 33,500,000\nWren, 744, 25",
        ),
    )

    @staticmethod
    def _picked(label, default=None):
        values = label.component.values
        return values[0] if values else default

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        stage = self._picked(self.round_, "qualifiers")
        recording = self._picked(self.recording, "final")
        label = self._picked(self.group)

        # Whichever Champion Duel they named, or the only one there was. The
        # picker is absent in the single-grouping case, so `_picked` returns
        # the default and this resolves to what the hub already had.
        chosen = self._picked(self.champion_duel)
        grouping = next((g for g in self.groupings if str(g["id"]) == chosen), self.grouping)

        if stage == "knockouts":
            # One field of 32 rather than lettered groups, so a letter here
            # would be a claim about a structure the round does not have.
            label = None
        elif not label:
            await interaction.followup.send(
                f"⚠️ **{db.STAGE_LABELS[stage]}** are played in lettered groups, so this "
                f"needs a group. Pick one and submit again.",
                ephemeral=True,
            )
            return

        rows = db.parse_placement_lines(
            self.players.component.value,
            warzone=self.warzone,
            known_warzones=grouping.get("warzones") or (),
        )
        if not rows:
            await interaction.followup.send(
                "⚠️ No players were entered. Paste them one per line, as "
                "`name, warzone, rank, total hero power, score`.",
                ephemeral=True,
            )
            return

        rows = [await asyncio.to_thread(_resolve_line, row) for row in rows]
        view = _ReconcileView(
            user_id=interaction.user.id,
            can_write=self.can_write,
            grouping=grouping,
            stage=stage,
            label=label,
            recording=recording,
            rows=rows,
        )
        await interaction.followup.send(
            embed=build_reconcile_embed(rows=rows, stage=stage, label=label, recording=recording),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()


class _ReconcileView(discord.ui.View):
    """The paste, line by line, with Save held back until nothing is unresolved.

    A select carries **only the unresolved lines**. One select per line would
    blow the five-row budget at six players, and the resolved ones need no
    control: they are already right.
    """

    def __init__(self, *, user_id, can_write, grouping, stage, label, recording, rows, index=None):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.can_write = can_write
        self.grouping = grouping
        self.stage = stage
        self.label = label
        self.recording = recording
        self.rows = rows
        self.index = index
        self.message: discord.Message | None = None
        self._build()

    # ── rendering ─────────────────────────────────────────────────────────────

    def _unresolved(self) -> list[int]:
        return [i for i, row in enumerate(self.rows) if row["state"] in _UNRESOLVED]

    def _build(self):
        self.clear_items()
        if self.index is not None:
            self._build_one_line()
            return

        pending = self._unresolved()
        if pending:
            select = discord.ui.Select(
                placeholder=f"Fix a name ({len(pending)})",
                options=[
                    discord.SelectOption(
                        label=(self.rows[i].get("name") or self.rows[i]["raw"])[:100],
                        value=str(i),
                        description=_LINE_PROBLEMS.get(self.rows[i].get("problem"))
                        or "on more than one warzone",
                    )
                    for i in pending[:25]
                ],
                row=0,
            )
            select.callback = self._on_pick_line
            self.add_item(select)

        # Disabled rather than absent while anything is unresolved: a control
        # that would half-write a group should not look live (`notes/DESIGN.md`).
        save = discord.ui.Button(
            label=CD_BTN_SAVE_GROUP[:80],
            style=discord.ButtonStyle.success,
            row=1,
            disabled=bool(pending),
        )
        save.callback = self._on_save
        self.add_item(save)
        cancel = discord.ui.Button(label=CD_BTN_CANCEL, style=discord.ButtonStyle.secondary, row=1)
        cancel.callback = self._on_cancel
        self.add_item(cancel)

    def _build_one_line(self):
        """The candidates as a select, not a button each.

        A button per candidate ate four of the five rows and still capped at
        20. One select is one row and holds 25, and its description line
        carries the warzone -- which is the only thing telling two identical
        names apart, so it is the part that has to be readable.

        Past 25 the exit already exists: `notes/DESIGN.md` wants paging or a
        filter decided before the wall is hit, and here the filter is the
        warzone modal behind "Add as a new player". Reaching it needs one name
        registered on 26 warzones out of the grouping's 16, so the cap is
        recorded rather than engineered around.
        """
        row = self.rows[self.index]
        candidates = (row.get("candidates") or [])[:25]
        if candidates:
            picker = discord.ui.Select(
                placeholder="Which one is this?",
                options=[
                    discord.SelectOption(
                        label=candidate["display_name"][:100],
                        value=str(candidate["id"]),
                        description=f"Warzone {candidate['server']}"
                        + (f" · [{candidate['alliance']}]" if candidate.get("alliance") else ""),
                    )
                    for candidate in candidates
                ],
                row=0,
            )
            picker.callback = self._on_pick_candidate
            self.add_item(picker)

        add = discord.ui.Button(
            label=CD_BTN_LINE_NEW[:80], style=discord.ButtonStyle.primary, row=1
        )
        add.callback = self._on_add_new
        self.add_item(add)
        skip = discord.ui.Button(
            label=CD_BTN_LINE_SKIP[:80], style=discord.ButtonStyle.secondary, row=1
        )
        skip.callback = self._on_skip
        self.add_item(skip)
        back = discord.ui.Button(label=CD_BTN_LINE_BACK, style=discord.ButtonStyle.secondary, row=1)
        back.callback = self._on_back
        self.add_item(back)

    def _embed(self):
        if self.index is None:
            return build_reconcile_embed(
                rows=self.rows, stage=self.stage, label=self.label, recording=self.recording
            )
        row = self.rows[self.index]
        why = _LINE_PROBLEMS.get(row.get("problem"))
        if why:
            detail = f"That line reads `{_typed(row.get('raw'), 60)}`, and {why}."
        else:
            # Name the warzones rather than the count. "On more than one
            # warzone" is a description of our problem; the two numbers are
            # what the reader recognises one of.
            zones = [str(c["server"]) for c in (row.get("candidates") or []) if c.get("server")]
            listed = (
                f"warzones {', '.join(zones[:-1])} and {zones[-1]}"
                if len(zones) > 1
                else f"warzone {zones[0]}"
                if zones
                else "more than one warzone"
            )
            detail = f"Our records show **{row.get('name')}** on {listed}. Which is correct?"
        return discord.Embed(
            title="👑 One line to settle",
            description=(
                f"{detail}\n\nIf you don't know, you can skip this and all others "
                f"entered will be saved."
            ),
            color=discord.Color.orange(),
        )

    async def _rerender(self, inter: discord.Interaction):
        self._build()
        await inter.response.edit_message(embed=self._embed(), view=self)

    # ── plumbing ──────────────────────────────────────────────────────────────

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    # ── callbacks ─────────────────────────────────────────────────────────────

    async def _on_pick_line(self, inter: discord.Interaction):
        self.index = int(inter.data["values"][0])
        await self._rerender(inter)

    async def _on_pick_candidate(self, inter: discord.Interaction):
        self.rows[self.index]["state"] = "matched"
        self.rows[self.index]["registrant_id"] = int(inter.data["values"][0])
        self.index = None
        await self._rerender(inter)

    async def _on_add_new(self, inter: discord.Interaction):
        row = self.rows[self.index]
        if not row.get("server"):
            # Identity is name plus warzone, so this is the one case that has to
            # ask for something the paste did not carry. Putting warzone in the
            # line format is what keeps it rare.
            await inter.response.send_modal(_NewPlayerWarzoneModal(view=self, index=self.index))
            return
        row["state"] = "new"
        self.index = None
        await self._rerender(inter)

    async def _on_skip(self, inter: discord.Interaction):
        self.rows[self.index]["state"] = "skipped"
        self.index = None
        await self._rerender(inter)

    async def _on_back(self, inter: discord.Interaction):
        self.index = None
        await self._rerender(inter)

    async def _on_cancel(self, inter: discord.Interaction):
        # `CANCEL_PLAIN`, not a backpedal: recording a group is a whole flow and
        # cancelling loses the paste. There is no parent step still holding it,
        # and saying "no changes made" would imply otherwise.
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(content=CANCEL_PLAIN, embed=None, view=self)
        self.stop()

    async def _on_save(self, inter: discord.Interaction):
        for item in self.children:
            item.disabled = True
        await inter.response.edit_message(view=self)
        written = await asyncio.to_thread(self._write, _actor(inter))
        self.stop()
        # Echoes the picker's own words rather than lowercasing them into
        # prose: the user chose "Final Standings" and that is what they should
        # see back, so the ack and the control cannot drift apart.
        await inter.followup.send(
            f"✅ Saved **{written}** {'player' if written == 1 else 'players'} to "
            f"{f'Group {self.label}' if self.label else db.STAGE_LABELS[self.stage]} "
            f"as **{_RECORDING_LABELS[self.recording]}**.",
            ephemeral=True,
        )

    def _write(self, actor: dict) -> int:
        """Create the group if new, then place everyone who resolved.

        Runs in a thread: every DB call from a handler does. Skipped and
        unreadable lines are left out rather than half-written.
        """
        group = db.get_or_create_group(
            self.grouping["id"], self.stage, self.label, guild_id=actor.get("guild_id")
        )
        written = 0
        for row in self.rows:
            if row["state"] not in ("matched", "new"):
                continue
            registrant_id = row.get("registrant_id")
            if registrant_id is None:
                player = db.upsert_registrant(
                    row["name"],
                    server=row["server"],
                    alliance=row.get("alliance"),
                    thp=row.get("thp"),
                    origin="self_reported",
                    actor=actor,
                )
                registrant_id = player["id"]
            elif row.get("thp") is not None:
                # The whole reason the paste now asks for a power. Without one
                # on every member of a group `group_advance_odds` refuses the
                # group, and this is the only bulk path that can supply eight.
                db.set_registrant_thp(registrant_id, row["thp"])
            db.set_placement(
                group["id"],
                registrant_id,
                rank=row.get("rank"),
                score=row.get("score"),
                recording=self.recording,
            )
            written += 1
        return written


class _NewPlayerWarzoneModal(discord.ui.Modal, title="Which warzone is this player on?"):
    """The one thing a paste can leave out that we cannot do without.

    Identity is name plus warzone. Adding a player without one makes a row
    nobody can match against later, which is the same refusal `_AddPlayerModal`
    already makes.
    """

    warzone = discord.ui.TextInput(label="Warzone number", max_length=10, placeholder="e.g. 738")

    def __init__(self, *, view: "_ReconcileView", index: int):
        super().__init__()
        self.parent = view
        self.index = index

    async def on_submit(self, interaction: discord.Interaction) -> None:
        zones = db.parse_warzones(self.warzone.value)
        row = self.parent.rows[self.index]
        if len(zones) != 1:
            await interaction.response.send_message(
                f"⚠️ **{_typed(self.warzone.value, 16)}** is not a warzone number. "
                f"**{row.get('name')}** was left unresolved, so nothing is lost.",
                ephemeral=True,
            )
            return
        row["server"], row["state"], row["registrant_id"] = zones[0], "new", None
        self.parent.index = None
        self.parent._build()
        await interaction.response.edit_message(embed=self.parent._embed(), view=self.parent)


# ── Hub ───────────────────────────────────────────────────────────────────────


def _phase_window_text(grouping_id, phase: str) -> str:
    """One phase's dates as a range: `8/10-8/14`.

    The game prints a tilde (`8/10~8/14`), which is a CJK-origin convention its
    UI carries throughout. We take the *layout* from it -- name, then range, so
    each half is one row of the Match Overview box -- and not the punctuation. A
    tilde is not how a range is written in the English copy around it, and
    `DESIGN.md`'s borrow-from-the-game rule is about icons and structure rather
    than typography. A hyphen reads correctly and still matches at a glance.
    """
    starts, ends = db.phase_window(grouping_id, phase)
    return f"{_short_date(starts)}-{_short_date(ends)}"


def phase_line(grouping: dict | None) -> str:
    """Where this grouping is on the calendar, and what comes next.

    Derived from the start date on every read, so it cannot go stale and nobody
    has to remember to advance it when the event moves on. That was already the
    argument for deriving the round; what changed is that the calendar can
    answer for a grouping with no draw loaded, which is every grouping but one.

    Laid out as the game lays it out -- name then date range -- so each half is
    one row of the Match Overview box. That also settles a grammar problem an
    earlier draft had: the phases mix plural ("Qualifiers", "Semi-finals") with
    singular ("Qualifier Detail", "Knockout Stage"), so any sentence with a verb
    in it reads as "Qualifier Detail start 8/17" for half the event.
    """
    if not grouping or not grouping.get("started_on"):
        return ""
    phase = db.current_phase(grouping["id"])
    if phase is None:
        return ""
    keys = [key for key, _, _ in db.PHASES]
    line = f"**{db.PHASE_LABELS[phase]}** {_phase_window_text(grouping['id'], phase)}"
    following = keys.index(phase) + 1
    if following < len(keys):
        nxt = keys[following]
        line += f", then **{db.PHASE_LABELS[nxt]}** {_phase_window_text(grouping['id'], nxt)}"
    return line + "."


def _group_title(stage: str, label: str | None) -> str:
    """What the game calls this group, in the game's own words.

    The game writes `Semi-final Grouping: Group H` on the screen a member reads
    between the qualifiers and the semi-finals, so "Group H" is the phrase they
    arrive already holding. The knockouts have no letter at all: 32 players, one
    field, and `db.get_groups` drops them for exactly that reason.
    """
    round_name = db.STAGE_LABELS.get(stage, "This round")
    if not label:
        return round_name
    return f"{round_name} - Group {label}"


def _rank_basis(members: list[dict]) -> str:
    """Whether these numbers are seed positions, results, or a mix of both.

    `seed_rank` and `rank` are different facts and the surface has to say which
    it is showing. A group recorded at the draw has seed positions and no
    results; the same group recorded again at the standings has both. A column
    of numbers that silently switches meaning between those two moments is the
    failure the two columns exist to prevent.
    """
    if not members:
        return "empty"
    ranked = sum(1 for m in members if m.get("rank") is not None)
    if ranked == len(members):
        return "results"
    if ranked == 0:
        return "seeds"
    return "mixed"


def _member_line(member: dict, basis: str, stage: str) -> str:
    """One player: where they are, who they are, and where they are from.

    The number is whichever we hold, and in a mixed group it is marked per row
    rather than in the header, because there the header cannot be true for
    everybody at once.
    """
    rank = member.get("rank")
    seed = member.get("seed_rank")
    shown = rank if rank is not None else seed
    position = f"`{shown}`" if shown is not None else "`-`"
    if basis == "mixed":
        position += " *(seed)*" if rank is None and seed is not None else ""

    name = discord.utils.escape_markdown(member.get("display_name") or "?")
    bits = [f"{position} **{name}**"]
    where = " · ".join(str(x) for x in (member.get("server"), member.get("alliance")) if x)
    if where:
        bits.append(where)

    # A knockout placement is an exit round, said forwards. Thirty of the 32 go
    # out somewhere and naming each exit is a scoreboard nobody asked us to
    # keep, so `knockout_result` gives "Made it to Top 16" rather than the match
    # they lost (Kevin, 2026-08-15).
    if stage == "knockouts" and rank is not None:
        result = db.knockout_result(rank)
        if result:
            bits.append(result)
    return " · ".join(bits)


def build_group_embed(
    *,
    members: list[dict],
    stage: str,
    label: str | None,
    grouping: dict | None,
    # Defaulted, unlike `_GroupView.can_odds`, which is required. This one only
    # decides whether an upsell renders: omitting it shows no upsell, where
    # omitting the view's would hand out the odds. The failure modes are not
    # the same size and the constructors do not need the same rule.
    can_odds: bool = True,
) -> discord.Embed:
    """One group, with whatever standing we hold for it.

    Deliberately renders at any size. An incomplete group still answers the
    question a member actually came with, which is who am I facing, and saying
    so is better than withholding seven names until somebody supplies the
    eighth.
    """
    embed = discord.Embed(
        title=f"{_group_title(stage, label)}",
        color=discord.Color.blurple(),
    )
    basis = _rank_basis(members)
    expected = db.GROUP_SIZE.get(stage)

    # The round and the group letter are the title now, so the description no
    # longer repeats them and opens on the one fact the title cannot carry:
    # which Champion Duel this is. Undated ones say nothing rather than leaving
    # a sentence with a blank in it.
    started = _short_date((grouping or {}).get("started_on"))
    opener = f"This Champion Duel started {started}. " if started else ""

    if not members:
        embed.description = (
            f"{opener}We do not have anyone recorded for this group.\n\n"
            f"Anyone can paste the standings in with "
            f"**{_btn_words(CD_BTN_RECORD)}**."
        )[:4096]
        return embed

    header = (
        opener
        + {
            "results": "These are the final standings that we have recorded.",
            "seeds": "These are seed positions. No results are recorded yet.",
            "mixed": "Rows marked *(seed)* are seed positions, not results.",
        }[basis]
    )

    lines = [_member_line(m, basis, stage) for m in members]
    embed.description = f"{header}\n\n" + "\n".join(lines)[: 4096 - len(header) - 2]

    # Completeness is stated, never inferred away. Eight names against a
    # 100-player qualifier group is the normal case rather than a truncation,
    # so this says what we hold against what the round holds and leaves the
    # reader to judge it.
    if expected and len(members) != expected:
        embed.add_field(
            name="Not the whole group",
            value=(
                f"We have **{_plural(len(members), 'player')}** of the "
                f"**{expected}** in this round. Anyone can add the rest with "
                f"**{_btn_words(CD_BTN_RECORD)}**."
            ),
            inline=False,
        )
    # The upsell rides on the embed rather than on the disabled button, which
    # cannot carry a reason. It names what the odds add over what this surface
    # already gives away for nothing, because a member looking at their eight
    # opponents can see most of the answer already.
    if not can_odds and stage in odds_lib.STAGES_WITH_A_MODEL:
        embed.add_field(
            name=f"🔒 {_btn_words(CD_BTN_ODDS)}",
            value=(
                f"Everything above is free, and so is recording it. What "
                f"{premium.PREMIUM_BRAND} adds here is the model: how often "
                f"each of these players gets through, across thousands of "
                f"simulated rounds. Run `/upgrade` to unlock it."
            ),
            inline=False,
        )
    return embed


def build_hub_embed(
    *,
    servers: list[dict],
    can_write: bool,
    grouping: dict | None = None,
    warzone: str | None = None,
) -> discord.Embed:
    """The hub's own state: what data is loaded, and what this caller can do.

    Every count is scoped to the caller's grouping when we know it. A figure
    spanning every grouping describes several tournaments at once and belongs to
    none of them, and to the alliance reading it, it is mostly somebody else's.

    Takes no `is_admin`: the admin row is hidden rather than announced, so the
    embed has nothing to say that differs for an operator.
    """
    embed = discord.Embed(title=CHAMPION_DUEL_HUB_TITLE, color=discord.Color.blurple())
    # Counted from warzones rather than groups. `get_groups` drops anyone whose
    # `grp` is empty, and a self-reported player's group is optional -- so a
    # group-based total silently omits exactly the players this hub invites
    # people to add. A warzone is required by both write paths, so it counts
    # everyone.
    total = sum(s["registrants"] for s in servers)
    mine = f" on warzone **{warzone}**" if warzone else ""
    calendar = phase_line(grouping)
    opener = f"{calendar}\n\n" if calendar else ""

    if grouping and not total:
        # Scoped, and holding nothing. Worth saying plainly rather than falling
        # through to the global "no roster loaded": their grouping is known, the
        # calendar still works, and the gap is exactly what a contribution fills.
        embed.description = (
            f"{opener}"
            f"We do not have any players for your Champion Duel yet.\n\n"
            f"Predictions and look-ups need players. Anyone{mine} can add the ones "
            f"they meet, and every one entered sharpens the next prediction."
        )[:4096]
    elif total:
        # Numeric order, no per-warzone counts. Counts answered a question
        # nobody asked here and made the line something to decode rather than
        # scan; a member is looking for their own number in it.
        #
        # Sorted defensively: a warzone is free text on a self-reported player,
        # so a non-numeric one has to sort somewhere rather than raise.
        listed = ", ".join(s["server"] for s in sorted(servers, key=_server_sort)[:_SERVERS_SHOWN])
        more = len(servers) - _SERVERS_SHOWN
        if more > 0:
            listed += f", and {more} more"
        scope = "in your Champion Duel" if grouping else "loaded"
        embed.description = (
            f"{opener}"
            f"**{total}** players {scope} across **{_plural(len(servers), 'warzone')}**: "
            f"{listed}.\n\n"
            f"Predict a match, or look up a player to see their squads and power. "
            f"Missing someone? **{_btn_words(CD_BTN_ADD)}**."
        )[:4096]
    else:
        embed.description = (
            "No roster is loaded yet.\n\n"
            "Predictions and look-ups need players. An admin imports them "
            "through the Champion Duel API."
        )

    if not predict_lib.ENGINE_AVAILABLE or not db.NAMES_AVAILABLE:
        embed.add_field(
            name="⚠️ Engine not installed",
            value=(
                "Predictions and look-ups are unavailable on this deploy. If you're "
                "the operator, check `CD_ENGINE_TOKEN` and the last build's install step."
            ),
            inline=False,
        )
    # No upsell for contributing, because contributing is not gated. The field
    # that stood here sold Premium on "correcting squads and recording
    # sightings", which is the one thing in this feature that must never be
    # gated: free alliances are the collection engine, and every sighting they
    # enter sharpens the predictions paying alliances get.
    #
    # No source legend here. 👁/≈/✏️ mark individual squad powers, which only
    # appear on a player's card -- `build_player_embed` carries the legend, next
    # to the marks it explains. On the hub it was a key to a map nobody was
    # holding.
    return embed


def build_finished_embed(*, grouping: dict, servers: list[dict], warzone: str | None):
    """Past the last day. What they hold stays readable, and the next one is
    offered.

    The offer has to survive the gap. A Champion Duel ends before the next
    draw is visible in game, so for some days after this appears there is
    nothing anyone could type into it. Copy that says "add the next one" and
    means "if you can" is a control that cannot be used, so this states the
    condition rather than the instruction.
    """
    whose = f"**{warzone}** is participating in" if warzone else "your alliance is in"
    return discord.Embed(
        title=CHAMPION_DUEL_HUB_TITLE,
        description=(
            f"The Champion Duel {whose} has finished.\n\n"
            f"When the next Champion Duel happens, you can enter your own warzone and "
            f"other Participating Warzones to start a new one. You can also "
            f"record past Champion Duel results if you want to keep a historical "
            f"record and help better improve future predictions."
        )[:4096],
        color=discord.Color.blurple(),
    )


class ChampionDuelFinishedView(discord.ui.View):
    """The finished hub: what they still hold, plus the way into the next one.

    Predict and Find stay live. They are global and useful between events, and
    the plan is explicit that scoping them would take something away.
    """

    def __init__(
        self,
        *,
        user_id: int,
        can_write: bool,
        engine_ok: bool,
        warzone: str | None,
        grouping: dict | None = None,
    ):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.can_write = can_write
        self.engine_ok = engine_ok
        self.warzone = warzone
        self.grouping = grouping
        self.message: discord.Message | None = None

        for label, style, row, cb, off in (
            (CD_BTN_ADD_GROUPING, discord.ButtonStyle.primary, 0, self._on_add_grouping, False),
            (CD_BTN_PREDICT, discord.ButtonStyle.secondary, 0, self._on_predict, not engine_ok),
            (CD_BTN_FIND, discord.ButtonStyle.secondary, 0, self._on_find, not engine_ok),
            (CD_BTN_CHANGE_WARZONE, discord.ButtonStyle.secondary, 1, self._on_warzone, False),
        ):
            button = discord.ui.Button(label=label[:80], style=style, row=row, disabled=off)
            button.callback = cb
            self.add_item(button)

        # Recording stays live after the event. Filling a group in from
        # screenshots once the Duel is over is the normal way this data arrives,
        # and the round is chosen explicitly, so a late entry still files right.
        if grouping:
            record = discord.ui.Button(
                label=(CD_BTN_RECORD if can_write else f"🔒 {CD_BTN_RECORD}")[:80],
                style=discord.ButtonStyle.secondary,
                row=1,
                disabled=not can_write,
            )
            record.callback = self._on_record
            self.add_item(record)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    async def _on_add_grouping(self, inter: discord.Interaction):
        await inter.response.send_modal(
            _AddGroupingModal(can_write=self.can_write, warzone=self.warzone)
        )

    async def _on_predict(self, inter: discord.Interaction):
        await inter.response.send_modal(_PredictModal())

    async def _on_find(self, inter: discord.Interaction):
        await inter.response.send_modal(_FindPlayerModal(self.can_write))

    async def _on_warzone(self, inter: discord.Interaction):
        await inter.response.send_modal(
            _WarzoneModal(can_write=self.can_write, current=self.warzone)
        )

    async def _on_record(self, inter: discord.Interaction):
        stage, groupings = await asyncio.gather(
            asyncio.to_thread(db.current_stage, self.grouping["id"]),
            asyncio.to_thread(db.groupings_for_warzone, self.warzone),
        )
        await inter.response.send_modal(
            _RecordGroupModal(
                can_write=self.can_write,
                grouping=self.grouping,
                stage=stage,
                groupings=groupings,
                warzone=self.warzone,
            )
        )


class _GroupView(discord.ui.View):
    """One group, plus every way of getting to a different one.

    Three selects rather than a sequence of steps. A member who has been
    knocked out, or whose Champion Duel has finished, is looking backwards
    rather than forwards, and making them re-enter the flow to change one axis
    is the wrong shape for that. All three are on screen at once and any of
    them re-reads the group.

    Each select is present only when it has something to choose between, so
    the common live case -- one Champion Duel, the round that is running, one
    group recorded -- renders as the odds button alone.
    """

    def __init__(
        self,
        *,
        user_id: int,
        groupings: list[dict],
        grouping: dict,
        stages: list[str],
        stage: str,
        groups: list[dict],
        label: str | None,
        members: list[dict],
        can_odds: bool,
    ):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.can_odds = can_odds
        self.groupings = groupings
        self.grouping = grouping
        self.stages = stages
        self.stage = stage
        self.groups = groups
        self.label = label
        self.members = members
        self.message: discord.Message | None = None
        self._build()

    # ── shape ────────────────────────────────────────────────────────────────

    def _build(self):
        self.clear_items()
        row = 0
        if len(self.groupings) > 1:
            self.add_item(
                self._select(
                    "Which Champion Duel?",
                    [
                        discord.SelectOption(
                            label=_grouping_option_label(g),
                            value=str(g["id"]),
                            default=g["id"] == self.grouping["id"],
                        )
                        for g in self.groupings[:25]
                    ],
                    row,
                    self._on_grouping,
                )
            )
            row += 1
        if len(self.stages) > 1:
            self.add_item(
                self._select(
                    "Which round?",
                    [
                        discord.SelectOption(
                            label=db.STAGE_LABELS.get(s, s),
                            value=s,
                            default=s == self.stage,
                        )
                        for s in self.stages
                    ],
                    row,
                    self._on_stage,
                )
            )
            row += 1
        if len(self.groups) > 1:
            self.add_item(
                self._select(
                    "Which group?",
                    [
                        discord.SelectOption(
                            label=f"Group {g['group']}",
                            value=str(g["group"]),
                            description=f"{_plural(g['registrants'], 'player')} recorded",
                            default=str(g["group"]) == str(self.label),
                        )
                        for g in self.groups[:25]
                    ],
                    row,
                    self._on_group,
                )
            )
            row += 1

        # Wherever there is a model. The qualifiers and the semi-finals are
        # separate models with separate constants and the engine is explicit
        # that they must not be mixed, so `odds_lib` dispatches rather than
        # this deciding. The knockouts have no model at all -- a
        # single-elimination field of 32 is a different question again -- so
        # the button is absent there rather than present and refusing.
        #
        # Disabled with a padlock on the free tier rather than hidden, which is
        # `DESIGN.md`'s Premium rule: a locked control lets the free tier see
        # the shape of the paid product. It reads well here because everything
        # around it is free. An alliance sees their eight opponents, sees the
        # button, and knows exactly what it would tell them. The upsell rides
        # on the embed, the same split `PlayerActionsView` used to use.
        if self.members and self.stage in odds_lib.STAGES_WITH_A_MODEL:
            odds = discord.ui.Button(
                label=(CD_BTN_ODDS if self.can_odds else f"🔒 {CD_BTN_ODDS}")[:80],
                style=discord.ButtonStyle.primary
                if self.can_odds
                else discord.ButtonStyle.secondary,
                disabled=not self.can_odds,
                row=row,
            )
            odds.callback = self._on_odds
            self.add_item(odds)

    def _select(self, placeholder, options, row, callback):
        select = discord.ui.Select(placeholder=placeholder, options=options, row=row)
        select.callback = callback
        return select

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    # ── moving between groups ────────────────────────────────────────────────

    async def _reload(self, inter: discord.Interaction):
        """Re-read whichever group the three selects now point at.

        Every axis is re-resolved rather than patched, because changing one
        invalidates the ones below it: a different Champion Duel has its own
        rounds, and a different round has its own letters. Carrying the old
        letter across would show a group from the wrong round or none at all.
        """
        self.stages = await asyncio.to_thread(db.recorded_stages, self.grouping["id"])
        if self.stage not in self.stages:
            self.stage = self.stages[-1] if self.stages else self.stage
        self.groups = await asyncio.to_thread(db.get_groups, self.stage, self.grouping["id"])
        labels = [str(g["group"]) for g in self.groups]
        if str(self.label) not in labels:
            self.label = labels[0] if labels else None

        group = await asyncio.to_thread(
            db.get_or_create_group, self.grouping["id"], self.stage, self.label
        )
        self.members = await asyncio.to_thread(db.get_group_members, group["id"])
        self._build()
        await inter.edit_original_response(
            embed=build_group_embed(
                members=self.members,
                stage=self.stage,
                label=self.label,
                grouping=self.grouping,
                can_odds=self.can_odds,
            ),
            view=self,
        )

    async def _on_grouping(self, inter: discord.Interaction):
        await inter.response.defer()
        chosen = inter.data["values"][0]
        self.grouping = next((g for g in self.groupings if str(g["id"]) == chosen), self.grouping)
        await self._reload(inter)

    async def _on_stage(self, inter: discord.Interaction):
        await inter.response.defer()
        self.stage = inter.data["values"][0]
        await self._reload(inter)

    async def _on_group(self, inter: discord.Interaction):
        await inter.response.defer()
        self.label = inter.data["values"][0]
        await self._reload(inter)

    # ── odds ─────────────────────────────────────────────────────────────────

    async def _on_odds(self, inter: discord.Interaction):
        """Everyone's chance of getting out of this group.

        The gate is that every player has SOMETHING to place them by, which
        is a Total Hero Power or any single squad power. Neither is
        individually required. The engine fills what is missing from the shape
        fit and samples what nobody has measured.
        """
        await inter.response.defer(ephemeral=True, thinking=True)
        # Re-resolved, not read off `self`. The flag was captured when the view
        # was built, and this view lives 15 minutes against a 5 minute
        # entitlement cache -- so the stale case that matters is a subscription
        # that lapsed while the group was on screen, where the button is still
        # live because it was enabled at build time. Reading `self.can_odds`
        # would let that through; checking here catches it. One cached lookup
        # in front of a simulation that costs seconds is not a price worth
        # optimising.
        if not await premium.feature_gate("champion_duel_odds", inter.guild_id, interaction=inter):
            await _send_odds_upsell(inter)
            return
        group = await asyncio.to_thread(
            db.get_or_create_group, self.grouping["id"], self.stage, self.label
        )
        scouted = await asyncio.to_thread(db.get_group_scouting, group["id"])
        await inter.followup.send(
            embed=await asyncio.to_thread(
                build_odds_embed, scouted, self.stage, self.label, self.grouping
            ),
            ephemeral=True,
        )


async def _send_odds_upsell(interaction: discord.Interaction) -> None:
    """Refuse the odds and offer the upgrade.

    `upgrade_view` returns None when no SKU is configured, and discord.py
    raises `TypeError` on a `view=None`. So the button is offered when there is
    one and the embed's own "Run `/upgrade`" line carries it when there is not,
    which is the same fallback `donate.py` uses.
    """
    view = premium.upgrade_view()
    embed = premium.premium_locked_embed(feature_label=_btn_words(CD_BTN_ODDS))
    kwargs = {"view": view} if view is not None else {}
    await interaction.followup.send(embed=embed, ephemeral=True, **kwargs)


#: Which rungs of the bracket the table shows, in order, and what each is
#: called on screen. Kevin's four, 2026-08-23.
#:
#: `bracket_odds` computes every round, deliberately, because "what does
#: advancing mean in a bracket" was an open product question. It is answered
#: now, and this constant is where the answer lives: display only, no engine
#: change. The two-rung version it replaces was a placeholder picking two of
#: seven to be the exact analogue of the group table beside it.
#:
#: THREE COMPUTED ROUNDS ARE DELIBERATELY NOT HERE, and the third of them was
#: cut on the rendered table rather than in the abstract.
#:
#:   `last32` is the field itself. Reaching it is true of every row, so it
#:   would print 100% thirty-two times.
#:
#:   `final` loses to `podium` on the same width: losing a final still takes
#:   second, and the top three is what the game rewards.
#:
#:   `podium` then lost to nothing at all. It shipped in the five-rung version
#:   and came out on 2026-08-23 once there was a real field to read: over a
#:   spread thirty-two it sits within 2 to 6 points of `last4` at the top of
#:   the table, and from the thirteenth row down it is `<1%` beside a
#:   `champion` that is already `<1%`. A rung that tracks its neighbour where
#:   the numbers are large and duplicates the next one where they are small is
#:   width spent on nothing, and the line is long enough to wrap without it.
#:
#: SAID AS REACHING A RUNG, NEVER AS GOING OUT IN ONE. Thirty of the thirty-two
#: are eliminated somewhere, and a surface naming each exit is a scoreboard
#: nobody asked for (`notes/UX.md`). `champion` is the one rung that is not a
#: reach, which is why the explainer names it apart from the rest.
BRACKET_RUNGS = {
    "last16": "Top 16",
    "last8": "Top 8",
    "last4": "Top 4",
    "champion": "Champion",
}

#: Most significant rung first, for the sort. Every rung on screen is in the
#: key, so nothing orders this table that the reader cannot see.
_BRACKET_SORT = tuple(BRACKET_RUNGS)[::-1]


def _printed_rank(prob: float) -> float:
    """Where a figure sits in the printed order, read off the printed figure.

    Ordering the bracket on the raw floats orders it on differences the
    surface does not show: `probability()` rounds to the nearest percent and
    floors a long tail into `<1%`, so a thousandth of a point can put one
    player above another and the reader then sees a lower rung climb underneath
    rungs that are visibly equal. That is the sorting bug the re-sort exists to
    prevent, and with two of the four rungs small, it is the common case
    rather than an edge.

    Derived from `probability()` rather than from a second copy of its
    thresholds, so the ordering cannot drift away from the rendering.
    """
    text = words.probability(prob)
    if text == "<1%":
        return 0.0
    if text == ">99%":
        return 100.0
    return float(text.rstrip("%"))


def build_bracket_embed(result, grouping) -> discord.Embed:
    """How far each of the 32 gets, one ladder per player.

    Kept apart from `build_odds_embed` rather than branched inside it, because
    almost none of that function survives the change of round: there is no
    group letter, no points to rank on, no "top N and through", and the footer
    it sets would be actively wrong here. What the two share is the refusals,
    and those are `NotEnoughData` either way.

    STACKED RATHER THAN ALIGNED, AND THAT IS FORCED. An embed holds roughly 40
    monospace columns. Five figures at four characters, plus their separators,
    plus a name that can carry an alliance tag, comes to about 43 -- so the
    aligned five-column table does not fit, rather than fitting badly. A fenced
    block does not rescue it either: Discord code blocks wrap and do not
    scroll (the scrollable ones existed and were removed), so the row would
    come back with its alignment destroyed, which is worse than never having
    aligned it.

    Kevin's pick, 2026-08-23. Every label travels with its own number, so a
    wrap costs nothing, and the name stays bold on its own line because what
    this surface is read for is finding yourself in a field of thirty-two.
    """
    embed = discord.Embed(
        title=f"🔮 {db.STAGE_LABELS['knockouts']}",
        color=discord.Color.blurple(),
    )
    # Re-sorted on what this table actually shows, rather than taken in the
    # join's order. `bracket_odds` ranks on the title and then cascades out
    # through every round, which is the right canonical order for a caller
    # that wants all of them -- but it cascades through rounds this table does
    # not print, and the visible result is a figure that climbs as the eye
    # goes down, which reads as a sorting bug.
    #
    # On the PRINTED figures, through `_printed_rank`, and that part is not
    # cosmetic: two thirds of a thirty-two field share a title chance under
    # half a percent, so ordering on the floats would order most of this list
    # by a difference nothing on screen shows. Where every printed figure ties,
    # `sorted` is stable and the join's own ranking decides it, invisibly.
    shown = sorted(
        result.rows,
        key=lambda row: tuple(_printed_rank(row.reach.get(rung, 0.0)) for rung in _BRACKET_SORT),
        reverse=True,
    )
    # Through `probability()`, not `:.0%`. In a group of eight a bottom row
    # rounds to 0% occasionally; in a field of thirty-two most of the ladder
    # does, and a `0%` tells a player a rung is arithmetically out of reach
    # when what it means is "under half a percent". That is the exact claim
    # `probability()` exists to refuse, and this is the surface where the
    # refusal earns its keep -- four rungs deep, it is most of what is printed.
    blocks = [
        f"**{discord.utils.escape_markdown(row.name)}**\n"
        + " · ".join(
            f"{label} {words.probability(row.reach.get(rung, 0))}"
            for rung, label in BRACKET_RUNGS.items()
        )
        for row in shown
    ]
    lead = (
        f"The knockout bracket: {_plural(len(result.rows), 'player')}, single "
        f"elimination. Each figure gives the odds of reaching that far, and "
        f"**{BRACKET_RUNGS['champion']}** the odds of winning it."
    )
    # The whole field rather than `_ODDS_SHOWN`: the bracket IS the thirty-two,
    # and a member looking for their own name is the reason this is read at
    # all. Two lines each measures about 2,700 characters against the
    # 4,096-character cap, so it fits -- but a field of long names would not,
    # and Discord truncates an over-long description mid-figure. So rows come
    # off the bottom until it fits and the count goes in the tail, which is
    # what the group table already does with a hundred-player qualifier group.
    kept = list(blocks)
    while True:
        more = len(result.rows) - len(kept)
        tail = f"\n\nand **{_plural(more, 'player')}** below them." if more > 0 else ""
        description = lead + ("\n\n" + "\n".join(kept) if kept else "") + tail
        if len(description) <= 4096 or not kept:
            break
        kept.pop()
    embed.description = description[:4096]

    # The one thing this surface has to say that the group one does not. A
    # bracket answer depends on who a player meets, and nobody knows that yet,
    # so the seeding is redrawn every trial and these are averages over the
    # brackets that could happen rather than the one anybody will get. A reader
    # who takes them for the second thing will be badly wrong about one
    # specific player, which is exactly the failure a footer can prevent and a
    # table cannot.
    #
    # "SEEDING", NEVER "THE DRAW". `_RECORDING_LABELS` already calls it
    # **Initial Seed** on the recording surface, and two words for one thing is
    # how a member ends up thinking they are two things.
    embed.set_footer(
        text=(
            f"Seeding isn't set yet, so each of {result.trials:,} simulations "
            f"runs a different bracket. Squads we haven't seen are sampled."
        )
    )
    return embed


def build_odds_embed(scouted, stage, label, grouping) -> discord.Embed:
    """The odds, or the reason there are none.

    The model refuses a group that is not exactly eight, and refuses a player
    it has nothing to place by. Both are hard stops rather than degraded answers,
    and the copy has to say which one it hit: "add the missing players" and
    "record one squad for these two" are different jobs, and pointing at the wrong
    one is a dead end.

    Everything past THP is optional. The engine samples squads it has not been
    given, so a group nobody has scouted still gets odds, just wider ones.
    """
    embed = discord.Embed(
        title=f"🔮 {_group_title(stage, label)}",
        color=discord.Color.blurple(),
    )
    if not odds_lib.ENGINE_AVAILABLE:
        embed.description = _ENGINE_MISSING
        return embed

    try:
        # The knockouts are a bracket rather than a group, so they take the
        # other join. Dispatched here rather than inside `group_advance_odds`
        # because the two return different shapes -- a bracket has no points
        # and no "top N", so there is no row type both could fill without one
        # of them inventing a column.
        if stage == "knockouts":
            return build_bracket_embed(odds_lib.bracket_odds(scouted), grouping)
        result = odds_lib.group_advance_odds(scouted, stage=stage)
    except odds_lib.NotEnoughData as exc:
        if exc.missing_thp:
            named = ", ".join(
                f"**{discord.utils.escape_markdown(n)}**" for n in exc.missing_thp[:8]
            )
            # Deliberately does not name a button. It used to be that
            # `Correct a squad` rendered locked on the free tier, so pointing
            # at it sent a member through two surfaces to find a padlock. That
            # is no longer true -- contributing is free since 2026-08-17 -- but
            # the control still lives on a player's own card, reached by
            # searching each of these names one at a time, so naming it here
            # would still be a signpost rather than an exit. Worth revisiting
            # if the card ever becomes reachable from this surface.
            embed.description = (
                f"Odds need something to place each player by, and for {named} we "
                f"have neither a Total Hero Power nor a single squad power.\n\n"
                f"Either arrives with the roster, or from anyone who records a "
                f"squad for them. One squad is enough."
            )[:4096]
        else:
            expected = db.GROUP_SIZE.get(stage)
            embed.description = (
                f"Odds need the whole group. We have "
                f"**{_plural(len(scouted), 'player')}** of the **{expected}**.\n\n"
                f"Anyone can add the rest with **{_btn_words(CD_BTN_RECORD)}**."
            )[:4096]
        return embed

    # A hundred rows will not fit an embed and nobody reads past the first
    # screen, so a big group is cut to the players actually in contention.
    # The remainder is counted rather than dropped silently.
    shown = result.rows[:_ODDS_SHOWN]
    # Through `probability()`, not `:.0%`. A weak player in a strong group
    # rounds to `0%` in both columns, which reads as "you are arithmetically
    # eliminated" when what it means is "under half a percent" -- and it is the
    # same overclaim, in the same direction, that the prediction card refuses
    # at the other end of the scale. Same strings, same formatter, one fewer
    # false claim.
    lines = [
        f"`{words.probability(row.advance):>4}` `{words.probability(row.win_group):>4}`  "
        f"**{discord.utils.escape_markdown(row.name)}**"
        for row in shown
    ]
    more = len(result.rows) - len(shown)
    tail = f"\n\nand **{_plural(more, 'player')}** below them." if more > 0 else ""
    embed.description = (
        f"Over {result.trials:,} simulations of the round. The first column "
        f"gives the odds of finishing in the top **{result.advance}** and going "
        f"through, the second the odds of winning the group outright."
        + "\n\n"
        + "\n".join(lines)
        + tail
    )[:4096]

    # The round ranks on points rather than on matches or meetings won, and
    # saying so stops the first column reading as "win 4 of 7". This used to be
    # keyed off a per-round phrase; the qualifiers were the other key and their
    # odds came out on 2026-08-21, so the count is stated rather than looked up.
    # The knockouts never reached here: they return above, through
    # `build_bracket_embed`.
    embed.set_footer(
        text=(
            "Ranked on points across all 21 matches, not matches won. Squads "
            "we have not seen are sampled, so these carry that uncertainty."
        )
    )
    return embed


async def send_group_view(
    interaction: discord.Interaction, *, grouping: dict | None, warzone: str | None, user_id: int
) -> None:
    """Open the caller's group, with the history reachable from it.

    Starts on the Champion Duel the hub resolved and the round currently
    running, which is what somebody asking during an event means. Everything
    else is one select away, because a member who is out, or whose Champion
    Duel has finished, is looking backwards and there is no live round to show
    them.
    """
    if not grouping:
        await interaction.followup.send(
            "We do not know which Champion Duel your alliance is in yet. "
            f"Set it with **{_btn_words(CD_BTN_ADD_GROUPING)}**.",
            ephemeral=True,
        )
        return

    groupings = await asyncio.to_thread(db.groupings_for_warzone, warzone) if warzone else []
    if not any(g["id"] == grouping["id"] for g in groupings):
        groupings = [grouping] + list(groupings)

    stages = await asyncio.to_thread(db.recorded_stages, grouping["id"])
    if not stages:
        await interaction.followup.send(
            f"No rounds are recorded for {_grouping_name(grouping)} yet.",
            ephemeral=True,
        )
        return

    running = await asyncio.to_thread(db.current_stage, grouping["id"])
    stage = running if running in stages else stages[-1]
    groups = await asyncio.to_thread(db.get_groups, stage, grouping["id"])
    label = str(groups[0]["group"]) if groups else None

    group = await asyncio.to_thread(db.get_or_create_group, grouping["id"], stage, label)
    members = await asyncio.to_thread(db.get_group_members, group["id"])

    # The only gated thing in Champion Duel. Everything else on this surface,
    # and every way of contributing to it, is free.
    can_odds = bool(
        interaction.guild_id
        and await premium.feature_gate(
            "champion_duel_odds", interaction.guild_id, interaction=interaction
        )
    )

    view = _GroupView(
        user_id=user_id,
        groupings=groupings,
        grouping=grouping,
        stages=stages,
        stage=stage,
        groups=groups,
        label=label,
        members=members,
        can_odds=can_odds,
    )
    await interaction.followup.send(
        embed=build_group_embed(
            members=members, stage=stage, label=label, grouping=grouping, can_odds=can_odds
        ),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


class ChampionDuelHubView(discord.ui.View):
    """The button grid. Rows group by kind: everyone, contributors, operator."""

    def __init__(
        self,
        *,
        user_id: int,
        is_admin: bool,
        can_write: bool,
        engine_ok: bool,
        warzone: str | None = None,
        grouping: dict | None = None,
        can_intel: bool = False,
    ):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.is_admin = is_admin
        self.can_write = can_write
        self.engine_ok = engine_ok
        # Defaults False so a caller that forgets it renders the padlock rather
        # than handing out the paid surface. The gate is re-checked inside the
        # modal anyway; this only decides how the button is drawn.
        self.can_intel = can_intel
        self.warzone = warzone
        self.grouping = grouping
        self.message: discord.Message | None = None
        self._build_buttons()

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    def _add(self, label, style, row, cb, *, disabled=False):
        button = discord.ui.Button(label=label[:80], style=style, row=row, disabled=disabled)
        button.callback = cb
        self.add_item(button)

    def _build_buttons(self):
        # Row 0 — the three ways in. Two of them lead to a player, and the
        # write actions live on that player's card rather than here: asking
        # "who?" once beats asking it again in every flow.
        self._add(
            CD_BTN_PREDICT,
            discord.ButtonStyle.primary,
            0,
            self._on_predict,
            disabled=not self.engine_ok,
        )
        # Beside Predict rather than beside Find, because the pairing that
        # matters to the eye is the two controls that take two names. It renders
        # locked rather than hidden on the free tier, which is the Premium rule
        # in `DESIGN.md`: an alliance should see the shape of what they would be
        # buying, and this one is hard to describe and easy to show.
        self._add(
            CD_BTN_INTEL if self.can_intel else f"🔒 {CD_BTN_INTEL}",
            discord.ButtonStyle.secondary,
            0,
            self._on_intel,
            disabled=not self.can_intel or not self.engine_ok,
        )
        self._add(
            CD_BTN_FIND,
            discord.ButtonStyle.secondary,
            0,
            self._on_find,
            disabled=not self.engine_ok,
        )
        # Deliberately on the front row: meeting someone we do not have is the
        # most common way a contributor is currently turned away.
        #
        # NOT Premium, despite the padlock branch below. This comment used to
        # say "Adding is Premium because it is a write", which was true until
        # contributing came off the gate on 2026-08-17 and has described a gate
        # that does not exist since -- nothing sets `can_write` False, so no
        # padlock renders here. The branch survives because its 🔒-and-disable
        # rendering is the shape any later gate reuses, which the odds and the
        # intel surface both went on to use. Read it as unreachable, not live.
        self._add(
            f"🔒 {CD_BTN_ADD}" if not self.can_write else CD_BTN_ADD,
            discord.ButtonStyle.secondary,
            0,
            self._on_add,
            disabled=not self.can_write or not self.engine_ok,
        )

        # Row 1 — never locked. Someone deciding whether the feature is worth
        # paying for should be able to see what contributing involves, and it
        # is documentation: withholding it protects nothing.
        self._add(CD_BTN_GUIDE, discord.ButtonStyle.secondary, 1, self._on_guide)
        # Never locked, and absent rather than disabled without a grouping, for
        # the same reason as recording: with none resolved there is no group to
        # show and the caller is being asked for their warzone instead. Reading
        # who you are facing is not a contribution, so it is not Premium.
        if self.grouping:
            self._add(CD_BTN_GROUP, discord.ButtonStyle.secondary, 1, self._on_group)
        # Recording needs a grouping to file the group against, so it is absent
        # rather than disabled when there is none: on that surface the caller is
        # being asked for their warzone and has nothing to record yet.
        if self.grouping:
            self._add(
                f"🔒 {CD_BTN_RECORD}" if not self.can_write else CD_BTN_RECORD,
                discord.ButtonStyle.secondary,
                1,
                self._on_record,
                disabled=not self.can_write,
            )
        # A wrong warzone points the whole server at somebody else's tournament,
        # and nothing else on this hub can fix it. Present whenever we resolved
        # from one, which is the only time there is something to change.
        if self.warzone:
            self._add(CD_BTN_CHANGE_WARZONE, discord.ButtonStyle.secondary, 1, self._on_warzone)

        # Row 2 — operator only, and absent entirely for everyone else.
        if self.is_admin:
            self._add(CD_BTN_EDITS, discord.ButtonStyle.secondary, 2, self._on_edits)
            self._add(CD_BTN_REVERT, discord.ButtonStyle.secondary, 2, self._on_revert)
            self._add(CD_BTN_EXPORT, discord.ButtonStyle.secondary, 2, self._on_export)

    # ── callbacks ─────────────────────────────────────────────────────────────

    async def _on_predict(self, inter: discord.Interaction):
        await inter.response.send_modal(_PredictModal())

    async def _on_intel(self, inter: discord.Interaction):
        await inter.response.send_modal(_IntelModal())

    async def _on_find(self, inter: discord.Interaction):
        await inter.response.send_modal(_FindPlayerModal(self.can_write, grouping=self.grouping))

    async def _on_add(self, inter: discord.Interaction):
        await inter.response.send_modal(_AddPlayerModal(self.can_write, grouping=self.grouping))

    async def _on_warzone(self, inter: discord.Interaction):
        await inter.response.send_modal(
            _WarzoneModal(can_write=self.can_write, current=self.warzone)
        )

    async def _on_record(self, inter: discord.Interaction):
        # Read before responding, not after: a modal has to be the first
        # response to an interaction, so this cannot defer first. One indexed
        # SQLite read is well inside the three seconds.
        stage, groupings = await asyncio.gather(
            asyncio.to_thread(db.current_stage, self.grouping["id"]),
            asyncio.to_thread(db.groupings_for_warzone, self.warzone),
        )
        await inter.response.send_modal(
            _RecordGroupModal(
                can_write=self.can_write,
                grouping=self.grouping,
                stage=stage,
                groupings=groupings,
                warzone=self.warzone,
            )
        )

    async def _on_group(self, inter: discord.Interaction):
        """Who this caller is facing.

        The odds of advancing belong on the surface this opens, because odds
        need a group and this is where a group exists. They are wired and
        gated: `CD_BTN_ODDS` renders there disabled with a padlock on the free
        tier, which is the Premium rule, and `champion_duel_odds` is the one
        entry in `PREMIUM_FEATURES` this feature has.

        This docstring described them as unwired until 2026-08-20. That was
        true when written -- the model was being rebuilt in
        `champion-duel-simulator` as of 2026-08-16 -- and stopped being true
        when #506 merged on the 19th.
        """
        await inter.response.defer(ephemeral=True, thinking=True)
        await send_group_view(
            inter, grouping=self.grouping, warzone=self.warzone, user_id=self.user_id
        )

    async def _on_guide(self, inter: discord.Interaction):
        """Its own button rather than part of the write flows.

        A modal cannot carry an image, and putting the guide in front of the
        modal would charge everyone who already knows an extra click on every
        entry. Beside the two buttons it explains is where someone looks when
        the question occurs to them.
        """
        embeds, files = build_guide()
        await inter.response.send_message(embeds=embeds, files=files, ephemeral=True)

    async def _on_edits(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        await _send_edits(inter)

    async def _on_revert(self, inter: discord.Interaction):
        await inter.response.send_modal(_RevertModal())

    async def _on_export(self, inter: discord.Interaction):
        await inter.response.send_modal(_ExportModal())


async def _open_hub(
    interaction: discord.Interaction, *, can_write: bool, note: str | None = None
) -> None:
    """Whichever of the hub's states this alliance is in.

    One entry point for all of them, so every flow that answers the grouping
    question lands back on the surface its answer unlocked rather than on an
    acknowledgement the user then has to leave. The caller has already responded
    or deferred.

    A caller with no server (a DM) skips straight to the global hub. There is
    nowhere to remember a warzone for them and nothing to scope, so asking would
    be a question with no use for the answer.
    """
    grouping, warzone = await _grouping_state(interaction)
    # Scoped the moment we know who is asking. Global is what the hub can
    # honestly say to an alliance it cannot place, and nothing more.
    servers = await asyncio.to_thread(db.get_servers, grouping["id"] if grouping else None)

    if interaction.guild_id and grouping is None:
        view = ChampionDuelOnboardingView(
            user_id=interaction.user.id, can_write=can_write, warzone=warzone
        )
        await interaction.followup.send(
            content=note,
            embed=build_onboarding_embed(servers=servers, warzone=warzone),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()
        return

    if (
        grouping
        and warzone
        and await asyncio.to_thread(
            db.needs_warzone_confirmation, str(interaction.guild_id), grouping["id"]
        )
    ):
        view = _ConfirmWarzoneView(
            user_id=interaction.user.id,
            can_write=can_write,
            warzone=warzone,
            grouping=grouping,
        )
        await interaction.followup.send(
            content=note,
            embed=build_confirm_warzone_embed(warzone=warzone, grouping=grouping),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()
        return

    engine_ok = predict_lib.ENGINE_AVAILABLE and db.NAMES_AVAILABLE
    # Asked once here rather than inside `_build_buttons`, which is not async.
    # One cached entitlement lookup on the way into the hub, and the modal
    # re-checks it before doing any work -- so this decides the padlock and
    # nothing else.
    can_intel = engine_ok and await premium.feature_gate(
        "champion_duel_intel", interaction.guild_id, interaction=interaction
    )

    if grouping and await asyncio.to_thread(db.is_finished, grouping["id"]):
        view = ChampionDuelFinishedView(
            user_id=interaction.user.id,
            can_write=can_write,
            engine_ok=engine_ok,
            warzone=warzone,
            grouping=grouping,
        )
        await interaction.followup.send(
            content=note,
            embed=build_finished_embed(grouping=grouping, servers=servers, warzone=warzone),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()
        return

    view = ChampionDuelHubView(
        user_id=interaction.user.id,
        is_admin=_is_admin(interaction.user.id),
        can_write=can_write,
        engine_ok=engine_ok,
        warzone=warzone,
        grouping=grouping,
        can_intel=can_intel,
    )
    await interaction.followup.send(
        content=note,
        embed=build_hub_embed(
            servers=servers, can_write=can_write, grouping=grouping, warzone=warzone
        ),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


async def handle_champion_duel_hub(bot, interaction: discord.Interaction) -> None:
    """Top-level handler for `/champion_duel`. Opens the hub.

    **Contributing is not gated.** `can_write` used to be a Premium check here.
    Kevin's decision, 2026-08-17: every other gated feature produces value for
    the alliance that uses it, but Champion Duel contributions produce value
    for everyone, so gating them means fewer predictions for paying alliances
    too. Free alliances are the collection engine.

    The flag stays threaded through the hub rather than being deleted, because
    the surfaces it renders (`🔒` and the disabled state) are what the odds
    gate will need when it is built. Nothing sets it False today, so no padlock
    renders.
    """
    await interaction.response.defer(ephemeral=True, thinking=True)
    await _open_hub(interaction, can_write=True)
