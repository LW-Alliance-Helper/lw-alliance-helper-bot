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

The write buttons *do* follow the Premium rule: disabled and 🔒 on the free
tier. Writes are Premium and deliberately community-gathering — if an alliance
pays, it decides who on its team it trusts to enter data, and the dataset is
only worth anything if more people contribute. Every write is attributed and
revertable, so the blast radius is bounded.
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
CD_BTN_SQUAD = "✏️ Correct a squad"
CD_BTN_ORDER = "➕ Record an order"
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
CD_BTN_ODDS = "🔮 Odds of advancing"
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
    alongside the name because two servers can field the same name."""
    who = f"<@{edit['actor_discord_id']}>"
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
        f"— {words.CONFIDENCE_LABEL}: {result.confidence().capitalize()}"
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
                f"⚠️ I don't have a full line-up for **{exc.name}**. Slot(s) {slots} "
                f"have no squad recorded, so there's nothing to predict with.\n"
                f"Run `{CHAMPION_DUEL_HUB_CMD}` → **{CD_BTN_FIND}** → "
                f"**{CD_BTN_SQUAD}** to fill them in.",
                ephemeral=True,
            )
            return

        subtitle = await asyncio.to_thread(card_subtitle, sides[0], sides[1])
        await _send_prediction(interaction, result, subtitle=subtitle)


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
            name="Most common order",
            value=f"**{order}**\n{_order_share(top_order['seen'], top_order['total'])}",
            inline=False,
        )
    else:
        embed.add_field(
            name="Most common order",
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

    def __init__(self, *, player: dict, user_id: int, can_write: bool):
        super().__init__(timeout=600)
        self.player = player
        self.user_id = user_id
        self.message: discord.Message | None = None

        for label, callback in (
            (CD_BTN_SQUAD, self._on_squad),
            (CD_BTN_ORDER, self._on_order),
        ):
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

    async def _on_squad(self, inter: discord.Interaction):
        await inter.response.send_modal(_SquadModal(self.player))

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
    view = PlayerActionsView(player=player, user_id=interaction.user.id, can_write=can_write)
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

    name = discord.ui.TextInput(label="Player name", max_length=64)
    server = discord.ui.TextInput(label="Server", max_length=10, placeholder="e.g. 738")
    group = discord.ui.TextInput(
        label="Group", required=False, max_length=2, placeholder="A single letter, if you know it"
    )
    alliance = discord.ui.TextInput(
        label="Alliance tag", required=False, max_length=16, placeholder="Optional"
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

        existing = await asyncio.to_thread(db.find_registrants, name, server)
        try:
            player = await asyncio.to_thread(
                db.upsert_registrant,
                name,
                server=server,
                grp=(self.group.value or "").strip() or None,
                alliance=(self.alliance.value or "").strip() or None,
                origin="self_reported",
                actor=_actor(interaction),
            )
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return

        # A group is round data, so it lands on the round being played rather
        # than on the player. Only written when they actually gave one: a blank
        # stage row would claim this player is in the round when all we know is
        # that somebody met them.
        #
        # And only when the letter can be placed. A group letter is meaningless
        # outside a grouping, so writing one against the globally-running round
        # is what put an officer in warzone 1500's opponent into the imported
        # grouping's Group D. Refusing to record it is the honest outcome, and
        # it is said out loud rather than dropped.
        group = (self.group.value or "").strip()
        aside = ""
        if group:
            if not self.grouping:
                aside = (
                    "\nℹ️ The group letter was not recorded: we do not know which "
                    "Champion Duel your alliance is in yet."
                )
            elif server not in self.grouping["warzones"]:
                # Names the grouping by its start date. A member may be in a
                # different one every season, and "your grouping" leaves them
                # nothing to check this against.
                aside = (
                    f"\nℹ️ The group letter was not recorded. Warzone **{server}** is not "
                    f"in {_grouping_name(self.grouping)}, so **Group {group}** there is a "
                    f"different group from yours."
                )
            else:
                stage = await asyncio.to_thread(db.current_stage, self.grouping["id"])
                if stage:
                    await asyncio.to_thread(
                        db.set_stage,
                        player["id"],
                        stage,
                        grp=group,
                        grouping_id=self.grouping["id"],
                    )
                    player = await asyncio.to_thread(db.get_player, name, server)

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


# ── Correct a squad (Premium) ─────────────────────────────────────────────────


class _SquadModal(discord.ui.Modal, title="Correct a squad"):
    """One slot per submission, for a player already on screen.

    Three fields rather than five: the name and server came from the card this
    opened from, so there is no second chance to mistype them and no ambiguous
    match to resolve mid-flow.

    Still one slot at a time. A five-field modal that half-fills is how a typo
    in slot 3 silently overwrites a good slot 1.
    """

    def __init__(self, player: dict):
        super().__init__()
        self.player = player
        self.title = f"Correct a squad: {player['display_name']}"[:45]

    slot = discord.ui.TextInput(label="Slot (1, 2 or 3)", max_length=1)
    squad_type = discord.ui.TextInput(
        label="Squad type", required=False, max_length=16, placeholder="Tank, Missile or Aircraft"
    )
    # Takes the number in whatever form the game showed it. Demanding a
    # normalised figure pushed a conversion onto the person reading "84.6M" off
    # a screen, to produce a value the bot then renders back as "84.6M" -- work
    # invented at the point of entry and undone at the point of display.
    power = discord.ui.TextInput(
        label="Power",
        required=False,
        max_length=24,
        placeholder="84.6M, 84,600,000 or 84600000",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        squad_type = (self.squad_type.value or "").strip().title() or None
        raw_power = (self.power.value or "").strip()
        if not squad_type and not raw_power:
            await interaction.followup.send(
                "⚠️ Nothing to change. Fill in a squad type, a power, or both.", ephemeral=True
            )
            return
        if squad_type and squad_type not in db.VALID_TYPES:
            await interaction.followup.send(
                f"⚠️ Squad type has to be one of {', '.join(db.VALID_TYPES)}.", ephemeral=True
            )
            return
        power = parse_power(raw_power) if raw_power else None
        if raw_power and power is None:
            await interaction.followup.send(
                f"⚠️ I couldn't read **{raw_power}** as a power. `84.6M`, "
                f"`84,600,000` and `84600000` all work.",
                ephemeral=True,
            )
            return
        try:
            slot = int(self.slot.value.strip())
        except (ValueError, AttributeError):
            slot = 0
        if slot not in (1, 2, 3):
            await interaction.followup.send("⚠️ Slot has to be 1, 2 or 3.", ephemeral=True)
            return

        found = self.player
        result = await asyncio.to_thread(
            db.set_squad,
            found["id"],
            slot,
            squad_type,
            power,
            actor=_actor(interaction),
            source="edited",
        )
        if not result["edit_ids"]:
            await interaction.followup.send(
                f"ℹ️ Slot {slot} for **{_label(found)}** already said that. Nothing changed.",
                ephemeral=True,
            )
            return
        changed = ", ".join(
            bit for bit in (squad_type, f"{power:,.0f}" if power is not None else None) if bit
        )
        await interaction.followup.send(
            f"✅ Slot {slot} for **{_label(found)}** is now **{changed}**.",
            ephemeral=True,
        )


# ── Record an order (Premium) ─────────────────────────────────────────────────


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
            "Enter this information for all 3 squads in the lineup.\n\n"
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
    "bad_score": "the score is not a number",
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
    return " · ".join(parts)


def _line_row(row: dict, *, stage: str | None = None, recording: str | None = None) -> str:
    """One line of the reconcile list, as it will be saved."""
    rank = str(row["rank"]) if row.get("rank") is not None else "–"
    name = row.get("name") or row.get("raw") or ""
    warzone = f"  #{row['server']}" if row.get("server") else ""
    score = f"  ·  {row['score']:,}" if row.get("score") is not None else ""
    # A knockout placement is the match they went out in, and that is what a
    # reader can actually check against what they watched. The seed order is
    # just a position, so the draw gets no such gloss.
    if stage == "knockouts" and recording == "final":
        exit_round = db.knockout_result(row.get("rank"))
        score = f"  ·  {exit_round}" if exit_round else score
    if row["state"] == "matched":
        return f"`{rank:>3}` ✅ **{name}**{warzone}{score}"
    if row["state"] == "ambiguous":
        return f"`{rank:>3}` ❓ **{name}**: on {len(row['candidates'])} warzones, pick one"
    if row["state"] == "new":
        return f"`{rank:>3}` ➕ **{name}**{warzone}: new, will be added"
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
    ):
        super().__init__()
        self.can_write = can_write
        self.grouping = grouping
        self.groupings = groupings or [grouping]

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
    players = discord.ui.Label(
        text="Add one player per line",
        description="Format: Name, Warzone, Rank, Score. Name is required.",
        component=discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            max_length=4000,
            placeholder="[OGV]Kestrel, 738, 1, 33,500,000\nWren, 744, 25",
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

        rows = db.parse_placement_lines(self.players.component.value)
        if not rows:
            await interaction.followup.send(
                "⚠️ No players were entered. Paste them one per line, as "
                "`name, warzone, rank, score`.",
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
                    origin="self_reported",
                    actor=actor,
                )
                registrant_id = player["id"]
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
            "mixed": "Rows marked *(seed)* are draw positions, not results.",
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
            f"You can predict a match or look up a player's information to see their "
            f"squads and power (if we have it). If we don't have data from your "
            f"warzone, or you can't find the player you're looking for, "
            f"**{_btn_words(CD_BTN_ADD)}**."
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
    if not can_write:
        embed.add_field(
            name="🔒 Contributing is Premium",
            value=(
                f"Correcting squads and recording sightings are part of "
                f"{premium.PREMIUM_BRAND}. Run `/upgrade` to unlock it. The more "
                "people entering sightings, the sharper every prediction gets."
            ),
            inline=False,
        )
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
    ):
        super().__init__(timeout=900)
        self.user_id = user_id
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

        # The odds need a group to be about, so they are absent on a round that
        # has none rather than present and refusing.
        if self.members:
            odds = discord.ui.Button(label=CD_BTN_ODDS, style=discord.ButtonStyle.primary, row=row)
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

        Wired to the engine path, which needs squads for all eight. The model
        behind this is being rebuilt in `champion-duel-simulator` and these
        numbers are expected to move when it lands (Kevin, 2026-08-16, having
        accepted that while nothing is merged nobody can see it).
        """
        await inter.response.defer(ephemeral=True, thinking=True)
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


def build_odds_embed(scouted, stage, label, grouping) -> discord.Embed:
    """The odds, or the reason there are none.

    Two different gates and they need different copy. A group can be short of
    members, which is about what has been recorded; or short of players we can
    predict, which is about how much scouting exists for the ones we hold. A
    member hits the second far more often, and telling them to record more
    names when the names are already there is a dead end pointing the wrong way.
    """
    embed = discord.Embed(
        title=f"🔮 {_group_title(stage, label)}",
        color=discord.Color.blurple(),
    )
    if not predict_lib.ENGINE_AVAILABLE:
        embed.description = _ENGINE_MISSING
        return embed

    best_of = db.MEETING_LENGTH.get(stage, 1)
    try:
        result = odds_lib.group_advance_odds(scouted, best_of=best_of)
    except odds_lib.NotEnoughPlayers:
        embed.description = (
            f"We cannot work these out yet. Odds need squads for the players in "
            f"the group, and we hold them for fewer than two of these.\n\n"
            f"Anyone can fill those in with **{_btn_words(CD_BTN_SQUAD)}** on a "
            f"player's card."
        )
        return embed

    lines = []
    for row in result.rows:
        name = discord.utils.escape_markdown(row.name)
        if row.is_range:
            low, high = sorted((row.p_advance, row.p_advance_coinflip))
            lines.append(f"`{low:>4.0%} to {high:>3.0%}` **{name}**")
        else:
            lines.append(f"`{row.p_advance:>11.0%}` **{name}**")

    embed.description = (
        f"Chance of finishing in the top **{result.advance}** and going through, "
        f"over {result.trials:,} simulations of the round. Each meeting is a "
        f"best-of-{result.best_of}.\n\n" + "\n".join(lines)
    )[:4096]

    if result.skipped:
        embed.add_field(
            name="Not included",
            value=(
                "We have no squads for "
                + ", ".join(f"**{discord.utils.escape_markdown(n)}**" for n in result.skipped)
                + ", so they are left out rather than guessed at. The odds above "
                "are for the rest of the group only."
            )[:1024],
            inline=False,
        )
    embed.set_footer(text="A range means the two tie-break rules disagree.")
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

    view = _GroupView(
        user_id=user_id,
        groupings=groupings,
        grouping=grouping,
        stages=stages,
        stage=stage,
        groups=groups,
        label=label,
        members=members,
    )
    await interaction.followup.send(
        embed=build_group_embed(members=members, stage=stage, label=label, grouping=grouping),
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
    ):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.is_admin = is_admin
        self.can_write = can_write
        self.engine_ok = engine_ok
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
        self._add(
            CD_BTN_FIND,
            discord.ButtonStyle.secondary,
            0,
            self._on_find,
            disabled=not self.engine_ok,
        )
        # Adding is Premium because it is a write, but it is deliberately on
        # the front row: meeting someone we do not have is the most common way
        # a contributor is currently turned away.
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
            )
        )

    async def _on_group(self, inter: discord.Interaction):
        """Who this caller is facing.

        The odds of advancing belong on the surface this opens, because odds
        need a group and this is where a group exists. They are not wired yet:
        the model behind them is being rebuilt in `champion-duel-simulator` as
        of 2026-08-16, and `CD_BTN_ODDS` is the label waiting for it. No
        disabled placeholder in the meantime -- `UX.md` principle 7 keeps phase
        language out of anything a user reads, and a greyed button promising a
        future feature is exactly that.
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
    """Top-level handler for `/champion_duel`. Opens the hub."""
    await interaction.response.defer(ephemeral=True, thinking=True)

    can_write = bool(
        interaction.guild_id
        and await premium.feature_gate(
            "champion_duel_write", interaction.guild_id, interaction=interaction, bot=bot
        )
    )
    await _open_hub(interaction, can_write=can_write)
