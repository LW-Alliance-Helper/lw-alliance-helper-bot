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
import champion_duel_predict as predict_lib
import premium
from api.champion_duel_auth import admin_ids

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

# Discord's message limit is 2000 and an embed description is 4096. Keep the
# browse list well inside both, since the export exists for volume.
BROWSE_MAX = 20

# Servers named individually before the list stops being scannable. Sixteen
# fits today; a future stage with more should not turn the hub into a wall.
_SERVERS_SHOWN = 12

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
_SOURCE_MARK = {"observed": "👁", "estimated": "≈", "edited": "✏️"}


def _is_admin(user_id: int) -> bool:
    return str(user_id) in admin_ids()


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
    change = f"{old or '—'} → {new or '—'}"
    tail = f"  ↩ revert of #{edit['revert_of']}" if edit.get("revert_of") else ""
    name = edit.get("display_name") or "(unknown)"
    server = f" (#{edit['server']})" if edit.get("server") else ""
    return f"`#{edit['id']}` **{name}**{server}{slot} {what}: {change} · {who} · {when}{tail}"


_POWER_SUFFIXES = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}


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
    lineup, from_sightings = side.likely_order()
    lines = [
        f"{i}. {squad_type} · {power:,.0f}" for i, (power, squad_type) in enumerate(lineup, start=1)
    ]
    tail = f"{side.observed_squads}/3 observed · "
    tail += (
        f"their order in {side.sightings} sighting{'s' if side.sightings != 1 else ''}"
        if from_sightings
        else "never seen deploying, assuming strongest first"
    )
    return "\n".join(lines) + f"\n*{tail}*"


_CONFIDENCE_COPY = {
    "high": "Built on observed squads and recorded sightings.",
    "medium": "Part of this is estimated from total hero power.",
    "low": "Both line-ups are estimates and neither player has been seen deploying.",
}


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
            f"**{a.name}** {result.p_a:.0%}\n`{_bar(result.p_a)}`\n"
            f"**{b.name}** {result.p_b:.0%}\n`{_bar(result.p_b)}`"
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(name=a.name[:256], value=_lineup(a)[:1024], inline=True)
    embed.add_field(name=b.name[:256], value=_lineup(b)[:1024], inline=True)
    confidence = result.confidence()
    embed.add_field(
        name=f"Confidence: {confidence}",
        value=_CONFIDENCE_COPY[confidence],
        inline=False,
    )
    embed.set_footer(
        text=(
            "Exact odds over both players' recorded orders — no sampling. "
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
        f"🆚 **{a.name}** {result.p_a:.0%} · **{b.name}** {result.p_b:.0%} "
        f"— confidence: {result.confidence()}"
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

    @discord.ui.button(
        label="📤 Share this prediction to this channel", style=discord.ButtonStyle.secondary
    )
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
                file=discord.File(io.BytesIO(self.png), filename="champion_duel_prediction.png"),
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "⚠️ I can't post in this channel — I need **Send Messages** and "
                "**Attach Files** here. You can still save the image and post it yourself.",
                ephemeral=True,
            )


async def _send_prediction(interaction: discord.Interaction, result: predict_lib.Prediction):
    """The card, falling back to the embed if rendering fails.

    A render is more moving parts than an embed -- fonts, a logo asset, Pillow
    -- and none of them are worth losing a correct prediction over. The
    fallback is silent to the user because the numbers are identical either
    way; the exception still reaches Sentry.
    """
    try:
        png = await asyncio.to_thread(champion_duel_image.render, result)
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
        file=discord.File(io.BytesIO(png), filename="champion_duel_prediction.png"),
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
                f"⚠️ I don't have a full line-up for **{exc.name}** — slot(s) {slots} "
                f"have no squad recorded, so there's nothing to predict with.\n"
                f"Use **{CD_BTN_FIND}** on `{CHAMPION_DUEL_HUB_CMD}` to open them — "
                f"**{CD_BTN_SQUAD}** is on their card.",
                ephemeral=True,
            )
            return

        await _send_prediction(interaction, result)


# ── Look up ───────────────────────────────────────────────────────────────────


def build_player_embed(player: dict, top_order: dict | None) -> discord.Embed:
    """One registrant: who they are, what they field, and what they've been
    seen doing. Every squad value carries its source, so an estimate cannot
    quietly harden into a fact."""
    embed = discord.Embed(
        title=f"🔍 {_label(player)}"[:256],
        color=discord.Color.blurple(),
    )
    bits = []
    if player.get("grp"):
        bits.append(f"Group **{player['grp']}**")
    if player.get("rank"):
        bits.append(f"rank {player['rank']}")
    if player.get("alliance"):
        bits.append(f"[{player['alliance']}]")
    if player.get("thp"):
        bits.append(f"THP {player['thp']:,.0f}")
    if player.get("origin") == "self_reported":
        # Never let a community-entered opponent read like an official import.
        bits.append("*added from a sighting, not the official roster*")
    embed.description = " · ".join(bits) or "No roster details recorded."

    squads = sorted(player.get("squads") or [], key=lambda s: s["slot"])
    if squads:
        embed.add_field(
            name="Squads",
            value="\n".join(
                f"{s['slot']}. {s.get('squad_type') or '—'} · "
                f"{(s.get('power') or 0):,.0f} {_SOURCE_MARK.get(s.get('source'), '')}"
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
            value=f"**{order}**\nSeen {top_order['seen']} of {top_order['total']} sightings",
            inline=False,
        )
    else:
        embed.add_field(
            name="Most common order",
            value="Never seen deploying. A prediction will assume strongest first.",
            inline=False,
        )
    embed.set_footer(text="👁 observed · ≈ estimated from total hero power · ✏️ corrected")
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
        await inter.response.send_modal(_OrderModal(self.player))


async def send_player_card(
    interaction: discord.Interaction, player: dict, *, can_write: bool, note: str | None = None
):
    """One player, with what can be done to them underneath.

    Shared by finding a player and adding one, so a player you just created
    lands you in the same place as one that was already there — the next thing
    you want is to record what you saw, either way.
    """
    top = await asyncio.to_thread(db.most_common_order, player["id"])
    view = PlayerActionsView(player=player, user_id=interaction.user.id, can_write=can_write)
    await interaction.followup.send(
        content=note,
        embed=build_player_embed(player, top),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()


class _FindPlayerModal(discord.ui.Modal, title="Find a Champion Duel player"):
    def __init__(self, can_write: bool):
        super().__init__()
        self.can_write = can_write

    name = discord.ui.TextInput(label="Player name", max_length=64)
    server = discord.ui.TextInput(
        label="Server", required=False, max_length=10, placeholder="e.g. 738"
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        found = await _resolve(self.name.value, self.server.value or None)
        if isinstance(found, str):
            # A miss is not a dead end any more: the name they typed is very
            # likely a real player we simply have not met.
            await interaction.followup.send(
                f"{found}\n\nIf they're real and we're missing them, "
                f"**{CD_BTN_ADD}** on `{CHAMPION_DUEL_HUB_CMD}`.",
                ephemeral=True,
            )
            return
        await send_player_card(interaction, found, can_write=self.can_write)


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

    def __init__(self, can_write: bool):
        super().__init__()
        self.can_write = can_write

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
                "⚠️ A player needs both a name and a server — identity here is the "
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

        note = (
            f"ℹ️ **{_label(player)}** was already here — opening them instead of adding a duplicate."
            if existing
            else f"✅ Added **{_label(player)}**. Record what you saw below."
        )
        await send_player_card(interaction, player, can_write=self.can_write, note=note)


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
        self.title = f"Correct a squad — {player['display_name']}"[:45]

    slot = discord.ui.TextInput(label="Slot (1, 2 or 3)", max_length=1)
    squad_type = discord.ui.TextInput(
        label="Squad type", required=False, max_length=16, placeholder="Tank, Missile or Aircraft"
    )
    # Takes the number in whatever form the game showed it. Demanding a
    # normalised figure pushed a conversion onto the person reading "84.6M" off
    # a screen, to produce a value the bot then renders back as "84.6M" — work
    # invented at the point of entry and undone at the point of display.
    power = discord.ui.TextInput(
        label="Power",
        required=False,
        max_length=24,
        placeholder="84.6M, 84,600,000 or 84600000 — all fine",
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)

        squad_type = (self.squad_type.value or "").strip().title() or None
        raw_power = (self.power.value or "").strip()
        if not squad_type and not raw_power:
            await interaction.followup.send(
                "⚠️ Nothing to change — fill in a squad type, a power, or both.", ephemeral=True
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
                f"ℹ️ Slot {slot} for **{_label(found)}** already said that — nothing changed.",
                ephemeral=True,
            )
            return
        changed = ", ".join(
            bit for bit in (squad_type, f"{power:,.0f}" if power is not None else None) if bit
        )
        await interaction.followup.send(
            f"✅ Slot {slot} for **{_label(found)}** is now **{changed}**.\n"
            f"Logged as edit `#{result['edit_ids'][0]}` and attributed to you.",
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
                f"\n**{_label(self.player)}** is now {top['seen']} of {top['total']} sightings in "
                f"**{' → '.join(top['order'])}**."
            )
        await inter.followup.send(
            f"✅ Recorded **{' → '.join(self.choice)}** for **{_label(self.player)}**.{tail}",
            ephemeral=True,
        )
        self.stop()


class _OrderModal(discord.ui.Modal, title="Record a deployment order"):
    """Only who they faced — the player came from the card this opened from.

    The order itself is picked from a select afterwards. A modal takes text
    inputs only, and free-typing a permutation is how "Tank, Tank, Missile"
    gets entered.
    """

    def __init__(self, player: dict):
        super().__init__()
        self.player = player
        self.title = f"Record an order — {player['display_name']}"[:45]

    opponent = discord.ui.TextInput(
        label="Who they faced", required=False, max_length=64, placeholder="Optional"
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        found = self.player
        view = _OrderSelectView(
            player=found,
            opponent=(self.opponent.value or "").strip() or None,
            user_id=interaction.user.id,
        )
        await interaction.followup.send(
            f"Which order did **{_label(found)}** deploy in?", view=view, ephemeral=True
        )
        view.message = await interaction.original_response()


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
        title="📜 Champion Duel — recent edits",
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
            f"⚠️ Edit `#{edit_id}` wasn't reverted — that value has changed since.\n"
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
        f"✅ Reverted `#{edit_id}` — restored to **{result['restored_to'] or '—'}**.\n"
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
                "⚠️ Dates need to be `YYYY-MM-DD` — for example `2026-08-12`.", ephemeral=True
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


# ── Hub ───────────────────────────────────────────────────────────────────────


def build_hub_embed(
    *, groups: list[dict], servers: list[dict], is_admin: bool, can_write: bool
) -> discord.Embed:
    """The hub's own state: what data is loaded, and what this caller can do."""
    embed = discord.Embed(title=CHAMPION_DUEL_HUB_TITLE, color=discord.Color.blurple())
    total = sum(g["registrants"] for g in groups)
    if total:
        letters = ", ".join(g["group"] for g in groups if g.get("group"))
        embed.description = (
            f"**{total}** registrants loaded across {len(groups)} group(s): {letters}.\n\n"
            "Ask for a matchup's odds, or look up what a player fields and how "
            "they've been seen deploying."
        )
        if servers:
            # Groups say which bracket; servers say whether this is about anyone
            # you know. Scouted counts sit beside registrant counts because they
            # answer different questions -- a server can be fully rostered and
            # still have nobody we have watched deploy.
            #
            # The list is the servers we happen to hold, not the servers we
            # accept. Naming sixteen of them reads as a closed set, so the ask
            # says otherwise outright: `upsert_registrant` takes any server
            # string, and an opponent from outside the import is exactly the
            # case add-player exists for.
            listed = ", ".join(
                f"**{s['server']}** ({s['registrants']})" for s in servers[:_SERVERS_SHOWN]
            )
            more = len(servers) - _SERVERS_SHOWN
            if more > 0:
                listed += f", and {more} more"
            scouted = sum(1 for s in servers if s["scouted"])
            embed.add_field(
                name=f"Servers ({len(servers)})",
                value=(
                    f"{listed}\n\n"
                    f"We've seen a real line-up on **{scouted}** of them. If we don't "
                    f"have data from your server, or you can't find the player you're "
                    f"looking for, **{CD_BTN_ADD}** — any server, whether it's listed "
                    f"here or not."
                )[:1024],
                inline=False,
            )
    else:
        embed.description = (
            "No roster is loaded for this stage yet.\n\n"
            "Predictions and look-ups need registrants — an admin imports them "
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
                f"{premium.PREMIUM_BRAND}. Run `/upgrade` to unlock it — the more "
                "people entering sightings, the sharper every prediction gets."
            ),
            inline=False,
        )
    if is_admin:
        embed.set_footer(text="👁 observed · ≈ estimated · ✏️ corrected · you have admin tools")
    else:
        embed.set_footer(text="👁 observed · ≈ estimated from total hero power · ✏️ corrected")
    return embed


class ChampionDuelHubView(discord.ui.View):
    """The button grid. Rows group by kind: everyone, contributors, operator."""

    def __init__(self, *, user_id: int, is_admin: bool, can_write: bool, engine_ok: bool):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.is_admin = is_admin
        self.can_write = can_write
        self.engine_ok = engine_ok
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

        # Row 2 — operator only, and absent entirely for everyone else.
        if self.is_admin:
            self._add(CD_BTN_EDITS, discord.ButtonStyle.secondary, 2, self._on_edits)
            self._add(CD_BTN_REVERT, discord.ButtonStyle.secondary, 2, self._on_revert)
            self._add(CD_BTN_EXPORT, discord.ButtonStyle.secondary, 2, self._on_export)

    # ── callbacks ─────────────────────────────────────────────────────────────

    async def _on_predict(self, inter: discord.Interaction):
        await inter.response.send_modal(_PredictModal())

    async def _on_find(self, inter: discord.Interaction):
        await inter.response.send_modal(_FindPlayerModal(self.can_write))

    async def _on_add(self, inter: discord.Interaction):
        await inter.response.send_modal(_AddPlayerModal(self.can_write))

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


async def handle_champion_duel_hub(bot, interaction: discord.Interaction) -> None:
    """Top-level handler for `/champion_duel`. Opens the hub."""
    await interaction.response.defer(ephemeral=True, thinking=True)

    can_write = bool(
        interaction.guild_id
        and await premium.feature_gate(
            "champion_duel_write", interaction.guild_id, interaction=interaction, bot=bot
        )
    )
    groups = await asyncio.to_thread(db.get_groups)
    servers = await asyncio.to_thread(db.get_servers)
    is_admin = _is_admin(interaction.user.id)
    engine_ok = predict_lib.ENGINE_AVAILABLE and db.NAMES_AVAILABLE

    view = ChampionDuelHubView(
        user_id=interaction.user.id,
        is_admin=is_admin,
        can_write=can_write,
        engine_ok=engine_ok,
    )
    await interaction.followup.send(
        embed=build_hub_embed(
            groups=groups, servers=servers, is_admin=is_admin, can_write=can_write
        ),
        view=view,
        ephemeral=True,
    )
    view.message = await interaction.original_response()
