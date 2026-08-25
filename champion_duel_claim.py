"""Which Discord account plays which recorded account.

There is no "you" anywhere else in Champion Duel. Every surface resolves off
the guild's warzone and a group picker, and none of them knows which of the
hundred rows on screen belongs to the person reading: the odds handler says so
outright, offering *"everyone's chance of getting out of this group"*. This
module is the link that supplies the missing half, and every other information
architecture session depends on it.

**A registrant is an account, not a person.** Accounts change hands. People
move into a stronger one, buy one, or transfer warzone in the two-week window
after a season, and the in-game name travels with the account rather than with
whoever is playing it. So a claim is present tense, "I play this account right
now", and it moves when the person moves it.

Three things follow, and each of them is a thing this module deliberately does
NOT do:

- **Nothing is auto-linked and nothing is merged.** The account somebody left
  may now be somebody else's, and its recorded habits would be the previous
  player's. There is no transfer detection.
- **There is no history of past holders.** A released account is simply free.
- **Leaving a Discord server does not release a claim.** The bot is in many
  alliance servers plus the community one, so leaving one says nothing about
  whether somebody still plays.

**Claiming is trust-based**, exactly as Map Manager's own claiming is: nothing
verifies that the caller really is that player. In-game names are unique within
a warzone, so relying on people to claim only themselves is reasonable rather
than naive, and a second claim on a held account is refused and pointed at
support instead.

Nothing here is Premium. A claim is not a computation, it is a fact somebody
told us, and the free/paid line in this feature is *free is what we recorded,
paid is what we worked out*.

**Deliberately not confirmed.** `notes/UX.md` confirms only what is destructive
*and* irreversible. Claiming, moving a claim and releasing one are all undone
by pressing the other button, so each is a single press with an acknowledgement
that says what changed.

**One route in today, and half of this module is groundwork.** The plan puts
every surface change out of this session's scope, so the only production entry
point is `add_claim_button` on the player card: somebody who has just looked
themselves up is already staring at their own row. `ClaimModal` is the other
half of the flow, for a caller who has *not* found their row, and **nothing
constructs it yet** -- it is what the hub's landing calls once `🏅 Your
standing` exists. `db.get_claimed_registrant` and `db.claims_for` are the
read side of the same deal and have no callers either. All three are tested
and none is dead by accident; a session wiring the landing should reach for
them rather than write a second version.
"""

from __future__ import annotations

import asyncio

import discord

import champion_duel_db as db
from messages import COMMUNITY_SERVER_NAME, COMMUNITY_SERVER_URL

# ── Copy ──────────────────────────────────────────────────────────────────────
#
# Signed off by Kevin 2026-08-25, in full. The reasoning for each string is
# kept below because it outlives the choice; the variants weighed against each
# are spent, and the pull request body is where they stay.
#
# The rules they are written to: US English, sentence case, no em dashes, and
# the voice split where **"I" acts and "we" holds** -- the bot says "I" when it
# cannot act on what it was given, and "we" when the statement is about what
# the record contains.
#
# THE SIGN-OFF TOOK THE ACKNOWLEDGEMENTS OFF "WE" AND ONTO "YOUR". Every one
# of them used to be a sentence about the record ("we have you as **X**"); they
# now say whose the account is ("**X** is your account"). That is not the voice
# split breaking, it is the split not reaching this far: a claim is the one
# fact in this feature that is about the reader rather than about the data, and
# the second person says so in fewer words. `CLAIM_NEEDS_BOTH`, which really is
# the bot unable to act on what it was handed, still says "I".

#: The button on a player card that says this row is you.
#:
#: 🔗 is the catalog's *"a link between two things, and breaking one"*, which
#: is this control word for word: a claim links a Discord account to a recorded
#: one. The catalog entry also settles the pair below, since it specifies one
#: glyph with the label carrying the direction, the way 👀 does.
#:
#: **Not the word "claim".** Last War already uses Claim for collecting a
#: reward, and `notes/UX.md` says inherited game vocabulary gets disambiguated
#: rather than borrowed.
#:
#: **THE BUTTON AND THE ACKNOWLEDGEMENTS SHARE THEIR WORDS.** "This is my
#: account" here, "**X** is your account" in every reply below: pressing the
#: control and reading what comes back say the same thing, which is the rule
#: Kevin set on the head to head modal. A sweep that reworded one of them and
#: not the other would be breaking something deliberate.
CLAIM_BTN = "🔗 This is my account"

#: The other half of the pair, shown in its place once the account is already
#: theirs. Decided with `CLAIM_BTN` rather than after it (`notes/DESIGN.md`
#: emoji rule 6), which is why they share a glyph, and worded with it: this is
#: the claim button negated, not a second sentence about the same idea.
CLAIM_RELEASE_BTN = "🔗 This is no longer my account"

#: The modal that asks somebody who they are, for the paths that reach claiming
#: without a player already on screen.
#:
#: IT USED TO BE A QUESTION, "Which player are you?", to match the family
#: already on this feature: "Which group are they in?", "Which warzone is this
#: player on?". The sign-off went the other way. A modal title is the heading
#: over its fields, both of these fields already open with "Your", and the
#: question was asking what the two of them then ask again.
CLAIM_MODAL_TITLE = "Your account"
CLAIM_FIELD_NAME = "Your in-game name"
CLAIM_FIELD_SERVER = "Your warzone"

#: Both halves are required. "I", because this is the bot unable to act on what
#: it was handed, not a statement about the record.
#:
#: One sentence, deliberately. `_INTEL_NEEDS_BOTH` carried a second sentence
#: and Kevin cut it when he signed that string off, and his standing note on
#: the stale-odds line was *"keep it even more simple than what you're
#: proposing here"*. The longer form explained that identity is the two
#: together because two warzones can field one name, and it lost for the same
#: reason: somebody who left a box empty does not need the theory.
CLAIM_NEEDS_BOTH = "⚠️ I need your in-game name and your warzone."

#: The acknowledgement, in the words of the button that produced it.
#:
#: IT USED TO SAY "We have you as **{player}**", which matched the phrasing the
#: bot already uses for a fact about somebody ("we have you as a Starter"). The
#: sign-off turned it around. The subject of this sentence is the account, and
#: what the reader is checking is whose it is.
CLAIM_DONE = "✅ **{player}** is your account."

#: A claim moving off one account and onto another, which is the single
#: mechanism behind all three kinds of account change: a warzone transfer, an
#: alliance move, and buying a stronger account.
#:
#: IT USED TO CARRY A SECOND SENTENCE, "**{previous}** is free for whoever
#: plays it", and the argument for it was that the account they left going free
#: is a consequence they would otherwise have to guess at. Kevin cut it, and
#: "now" carries what is left: one word is the whole difference from
#: `CLAIM_DONE`, and it says a claim moved without spending a sentence on the
#: account it moved off. Releasing the old one is still what the code does; it
#: is only the telling that went.
CLAIM_MOVED = "✅ **{player}** is your account now."

#: Pressed twice. Says what is already true rather than inventing a change, and
#: the release button on the same message is the exit. "already" is the whole
#: difference from `CLAIM_DONE`: three outcomes, one sentence.
CLAIM_ALREADY_YOURS = "ℹ️ **{player}** is already your account."

#: The refusal, and Kevin's call that it points at support in the community
#: server. Condition first, then the fix (`notes/UX.md`).
#:
#: **It never names the holder.** Who claimed an account is for support to
#: look up, and printing it would hand out an identity to anybody willing to
#: guess a name.
CLAIM_TAKEN = (
    "⚠️ Someone else already has **{player}** as their account. If that account "
    "is yours, reach out in the {community} and we will look into it."
)

#: Releasing. Mirrors `CLAIM_DONE` on purpose, so the pair reads as one setting
#: going on and off rather than as two features, and it mirrors
#: `CLAIM_RELEASE_BTN` exactly the way the claim half does.
CLAIM_RELEASED = "✅ **{player}** is no longer your account."

#: The release pressed when it cannot release anything: either they hold no
#: claim at all, which a stale message can still ask for, or they hold a
#: different account than the card they pressed it on.
#:
#: THEY USED TO BE TWO STRINGS, `CLAIM_NOTHING_TO_RELEASE` and
#: `CLAIM_NOT_YOURS`, the second naming the account on the card. `/code-review`
#: found that second state and it is real. Kevin chose one sentence for both,
#: because from where the reader sits they are the same news: the button they
#: pressed does not apply to them, and nothing they can see has changed.
#:
#: **THE TWO BRANCHES STAY IN THE CODE.** They are different states and a
#: session that needs to tell them apart must still be able to. Only the
#: surface is the same. See `release` and `add_claim_button`.
#:
#: What that costs, recorded rather than argued: in the second state the
#: sentence is not literally true of them. They are linked, to an account this
#: card is not showing, and it takes another look at their own row to see it.
#: The state is rare, nothing changes underneath them either way, and Kevin
#: chose one sentence across both knowing the second one exists.
CLAIM_NOT_LINKED = "ℹ️ You're not linked to an account right now."


def _hub():
    """`champion_duel_hub`, imported at call time.

    The hub imports this module to put the button on a player card, so a
    module-level import back would be a cycle. Deferring it is the pattern the
    hub already uses for `wizard_registry`.
    """
    import champion_duel_hub

    return champion_duel_hub


def _community_link_view() -> discord.ui.View:
    """The refusal's exit: a way to reach a human.

    `notes/UX.md` principle 3 -- every dead end carries its exit -- and this is
    a dead end by design, because the member cannot resolve it themselves.
    """
    hub = _hub()
    view = discord.ui.View(timeout=None)
    view.add_item(discord.ui.Button(label=hub.CD_BTN_COMMUNITY[:80], url=COMMUNITY_SERVER_URL))
    return view


class ClaimResultView(discord.ui.View):
    """The message a claim lands on, carrying the way back out of it.

    A claim with no visible release is a one-way door, and accounts change
    hands often enough that the person will want it. So the acknowledgement
    itself holds the control that undoes it, which is also what makes the
    confirm step unnecessary.

    It does not hold the player: releasing is keyed on the caller, who holds at
    most one claim, so a stale copy of this message cannot be aimed at an
    account they have since moved off.
    """

    def __init__(self, *, user_id: int):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.message: discord.Message | None = None

        button = discord.ui.Button(
            label=CLAIM_RELEASE_BTN[:80], style=discord.ButtonStyle.secondary
        )
        button.callback = self._on_release
        self.add_item(button)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        hub = _hub()
        if inter.user.id != self.user_id:
            await inter.response.send_message(hub._DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        hub = _hub()
        await expire_view_message(self.message, command_hint=hub.CHAMPION_DUEL_HUB_CMD)

    async def _on_release(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        await release(inter)


async def release(interaction: discord.Interaction) -> None:
    """Give up whatever this caller currently claims. The caller has deferred.

    Keyed on the caller rather than on the account on screen: a person holds at
    most one claim, and releasing "mine" cannot be aimed at somebody else's by
    a stale message.
    """
    hub = _hub()
    released = await asyncio.to_thread(db.release_claim, str(interaction.user.id))
    if released is None:
        # They hold nothing. The other half of `CLAIM_NOT_LINKED` is in
        # `add_claim_button`, where they hold a different account than the one
        # on the card; the two states read the same and stay apart in code.
        await interaction.followup.send(CLAIM_NOT_LINKED, ephemeral=True)
        return
    player = await asyncio.to_thread(db.get_registrant, released["registrant_id"])
    name = hub._label(player) if player else "that account"
    await interaction.followup.send(CLAIM_RELEASED.format(player=name), ephemeral=True)


async def claim(interaction: discord.Interaction, player: dict) -> None:
    """Record that this caller plays this account. The caller has deferred.

    Three outcomes and three different messages, because they are three
    different states rather than one with a shade: taken by somebody else,
    already theirs, and a claim that moved or was made.
    """
    hub = _hub()
    label = hub._label(player)
    try:
        result = await asyncio.to_thread(
            db.claim_registrant,
            player["id"],
            str(interaction.user.id),
            discord_name=getattr(interaction.user, "display_name", None),
            guild_id=str(interaction.guild_id) if interaction.guild_id else None,
        )
    except db.ClaimRefused:
        await interaction.followup.send(
            CLAIM_TAKEN.format(player=label, community=COMMUNITY_SERVER_NAME),
            view=_community_link_view(),
            ephemeral=True,
        )
        return
    except db.NoSuchRegistrant:
        # The row went away between the card being drawn and the button being
        # pressed. Rare, and the honest thing to say is that we no longer have
        # them, which is the same miss `🔍 Find a player` reports.
        #
        # Caught by its own class rather than by `LookupError`: `KeyError` and
        # `IndexError` are `LookupError` too, and a bug anywhere under this
        # call would otherwise tell a member we lost a player who is still
        # there and send them off to re-add somebody we already hold.
        await interaction.followup.send(
            f"⚠️ We no longer have **{label}**.",
            ephemeral=True,
        )
        return

    if not result["changed"]:
        message = CLAIM_ALREADY_YOURS.format(player=label)
    elif result["moved_from"]:
        # The account they left is free either way. `CLAIM_MOVED` stopped
        # naming it when Kevin cut its second sentence, which is why nothing
        # here reads `moved_from` beyond knowing that there was one.
        message = CLAIM_MOVED.format(player=label)
    else:
        message = CLAIM_DONE.format(player=label)

    view = ClaimResultView(user_id=interaction.user.id)
    await interaction.followup.send(message, view=view, ephemeral=True)
    view.message = await interaction.original_response()


class ClaimModal(discord.ui.Modal, title=CLAIM_MODAL_TITLE):
    """Picking yourself out by name and warzone.

    The path for somebody who has not already found their own row. Identity is
    (name, warzone) here as everywhere else in this feature, so both are asked
    for and neither is guessed.

    **Neither Total Hero Power nor an alliance tag is asked for.** They are
    what a claimed account needs to be *useful*, which is a different gate:
    requiring them to claim would turn "which of these is you" into a data
    entry form, and somebody who cannot answer would be stuck outside their own
    standing.
    """

    name = discord.ui.TextInput(label=CLAIM_FIELD_NAME, max_length=64)
    server = discord.ui.TextInput(label=CLAIM_FIELD_SERVER, max_length=10, placeholder="e.g. 738")

    def __init__(self, *, can_write: bool = True, grouping: dict | None = None):
        super().__init__()
        self.can_write = can_write
        self.grouping = grouping

    async def on_submit(self, interaction: discord.Interaction) -> None:
        hub = _hub()
        await interaction.response.defer(ephemeral=True, thinking=True)

        if not db.NAMES_AVAILABLE:
            await interaction.followup.send(hub._ENGINE_MISSING, ephemeral=True)
            return

        name = (self.name.value or "").strip()
        server = (self.server.value or "").strip()
        if not name or not server:
            await interaction.followup.send(CLAIM_NEEDS_BOTH, ephemeral=True)
            return

        found = await hub._resolve(name, server)
        if isinstance(found, str):
            # A miss here is the same miss `🔍 Find a player` reports, and it
            # takes the same exit rather than a second wording of it: somebody
            # who is genuinely not in our data adds themselves and lands on
            # their own card, where this button is waiting.
            view = hub._MissView(
                can_write=self.can_write,
                user_id=interaction.user.id,
                name=name,
                server=server,
                grouping=self.grouping,
            )
            await interaction.followup.send(
                f"{found}\n\nIf we don't have you listed, add yourself below.",
                view=view,
                ephemeral=True,
            )
            view.message = await interaction.original_response()
            return

        await claim(interaction, found)


def add_claim_button(view: discord.ui.View, *, player: dict, claim_row: dict | None, user_id: int):
    """Put the claim control on a view that is already showing one player.

    Which half of the pair renders is decided by who holds the account, not by
    what the press turns out to do. `notes/DESIGN.md` requires the label to say
    what the control does, and "This is my account" on a row that is already
    yours would be describing a press that cannot happen.

    An account held by **somebody else** still offers `CLAIM_BTN`. The refusal
    is the point: hiding it would leave a person who really did take over that
    account with nothing to press and nothing to read, where the refusal names
    the route to support.

    **A press never does something its own label did not describe.** This card
    lives ten minutes and a claim can move inside that window, in another
    message, so the label can be stale by the time it is pressed. Releasing is
    keyed on the caller rather than on what is on screen, so a stale release
    button acting on the caller's *current* claim would give up an account this
    card never mentioned. Re-claiming this one instead is no better: the button
    says "this is no longer my account" and would be making a claim.

    So the two halves are handled differently:

    - **Drawn as the claim**, `claim` covers every state the press can land in,
      including the one where they claimed this account somewhere else in the
      meantime, and it says which.
    - **Drawn as the release**, the claim is re-read and the press releases
      only if it still points here. If it has moved, `CLAIM_NOT_LINKED` says
      so and nothing changes, which is the only outcome the label supports.
    """
    mine = bool(claim_row) and claim_row["discord_user_id"] == str(user_id)
    label = CLAIM_RELEASE_BTN if mine else CLAIM_BTN
    button = discord.ui.Button(label=label[:80], style=discord.ButtonStyle.secondary)

    async def _pressed(inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        if not mine:
            await claim(inter, player)
            return
        current = await asyncio.to_thread(db.get_claim, player["id"])
        if current and current["discord_user_id"] == str(inter.user.id):
            await release(inter)
            return
        await inter.followup.send(CLAIM_NOT_LINKED, ephemeral=True)

    button.callback = _pressed
    view.add_item(button)
    return button
