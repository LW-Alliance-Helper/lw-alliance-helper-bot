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
import functools
import io
import itertools
import os
from datetime import datetime, timedelta, timezone

import discord

import champion_duel_claim as claim_lib
import champion_duel_db as db
import champion_duel_image
import champion_duel_intel as intel_lib
import champion_duel_odds as odds_lib
import champion_duel_picks as picks_lib
import champion_duel_predict as predict_lib
import champion_duel_store as store_lib
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
#: Approved by Kevin, 2026-09-01: *"Predict a single match"*, then settled as
#: **Simulate a match** in the same conversation. The rename is not cosmetic.
#:
#: *"Predict"* named the mechanism and distinguished nothing: every row on a
#: picks card is a prediction and so is `🎯 Head to head`, so three controls on
#: this feature all promised one. **And it quietly overpromises.** The standing
#: finding here is that sorting by total hero power picks winners better than
#: the simulation does, 87.9% against 84.4%, and that the model earns its keep
#: on calibration rather than accuracy. *Simulate* says what the engine does
#: without claiming the answer is right.
#:
#: 🆚 is the game's own VS mark (`DESIGN.md` rule 5) and is unchanged.
CD_BTN_PREDICT = "🆚 Simulate a match"
#: The form that button opens, and it is the button's own words with the feature
#: named. Kevin, 2026-09-02: *"Fix the modal title then."* It said **"Predict a
#: Champion Duel match"** and had done since before the rename, so the door and
#: the form behind it gave two names for one thing -- which is the fault this
#: whole pass exists to clear.
#:
#: Hoisted to a constant rather than left as a class-level literal, because the
#: two now have to move together and a literal is what let them drift apart.
CD_SIMULATE_TITLE = "Simulate a Champion Duel match"
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
#: Approved by Kevin, 2026-09-05: *"Post this prediction to current channel."*
#:
#: **The verb changed, not the noun.** It said *Share* while its two siblings
#: said *Post*, and both of their comments claimed to follow this one **to the
#: word** -- which neither did until now. The noun stays because renaming the
#: button that produces this card to `Simulate a match` did not stop the output
#: being a prediction: *simulate* is what the engine does, *prediction* is what
#: comes out.
CD_BTN_SHARE = "📤 Post this prediction to current channel"

#: What a share button says when the bot cannot post in this channel. One
#: string rather than one per share button: it is the same refusal about the
#: same two permissions, and the picks card copied it word for word before this
#: was hoisted, which is how two wordings of one sentence start.
_SHARE_DENIED = (
    "⚠️ I can't post in this channel. I need **Send Messages** and "
    "**Attach Files** here. You can still save the image and post it yourself."
)
CD_BTN_SET_WARZONE = "⚙️ Set your warzone"
CD_BTN_CHANGE_WARZONE = "✏️ Change your warzone"
CD_BTN_ADD_GROUPING = "➕ Add your Participating Warzones"
CD_BTN_RETRY_GROUPING = "✏️ Edit and try again"

# Approved by Kevin, 2026-08-31, over two alternatives: *"Keep: Add a Champion
# Duel."*
#
# What it is for: the finished hub's own copy has said *"You can also record
# past Champion Duel results"* since 2026-08-15, and until now the only control
# beside it was `CD_BTN_ADD_GROUPING`, which is onboarding and refuses a
# Champion Duel your warzone is not in.
#
# **ONE CONTROL, NOT TWO, AND RULE 7 IS WHY.** The first shape of this was a
# second button beside `CD_BTN_ADD_GROUPING` reading "Record a Champion Duel you
# were sent". Both are the same act -- sixteen warzones and a date -- so both
# wanted the same glyph, and `notes/DESIGN.md` rule 7 forbids repeating one
# across a choice set. 📥 was already `CD_BTN_RECORD`'s and ➕ was already the
# other button's. **Two catalogued glyphs both colliding is the catalog saying
# these are one control**, so whose Champion Duel it is became a question inside
# the form rather than a choice between two doors.
#
# ➕ is the catalog's *create*, and this creates a Champion Duel record where
# `CD_BTN_RECORD` fills in a group inside one that exists.
CD_BTN_ADD_CD = "➕ Add a Champion Duel"
#: The modal titles, hoisted so the two forms cannot drift apart in wording
#: while claiming to be the same surface. `CD_ADD_GROUPING_TITLE` is the string
#: that shipped as the class-level `title=`, unchanged, and is what onboarding
#: still opens. `CD_ADD_SENT_TITLE` is `CD_BTN_ADD_CD` without its glyph, so it
#: carries that button's 2026-08-31 sign-off rather than needing its own: a
#: modal titled anything else would be a second name for one control.
CD_ADD_GROUPING_TITLE = "Add your Participating Warzones"
CD_ADD_SENT_TITLE = "Add a Champion Duel"
#: The two acknowledgements. Sentences, not labels (`UX.md`), and they differ
#: because the two acts differ: one tells us where your alliance is playing, the
#: other adds a Champion Duel to what we hold. `CD_ADDED_SENT` approved by
#: Kevin, 2026-08-31: *"Fine as it is."* `CD_ADDED_MINE` is the string that
#: already shipped, hoisted into a constant and unchanged.
#:
#: **Chosen by what happened, not by an answer the form asked for.** A draft put
#: a *whose Champion Duel is this?* select on the form; Kevin struck it the same
#: day: *"we should not care who all it is - for all we know it could be theirs
#: from a past Duel and we don't have a reason to need to know."* The only sense
#: in which one is *yours* is that the hub now opens on it, and the entry
#: already works that out to decide the pin.
#:
#: **So `CD_ADDED_SENT` now fires wider than it did when it was approved**: a
#: past Champion Duel of your own reaches it too, because that is also not the
#: one you are playing. It reads correctly there, which is what the widening
#: turned on.
CD_ADDED_MINE = "✅ Added your Participating Warzones, starting **{date}**."
CD_ADDED_SENT = "✅ Recorded a Champion Duel starting **{date}**."
#: Kevin's words, 2026-09-02. He gave the substance the day before -- *"I would
#: just say that their known warzone is not in the list but don't gate anything
#: on it"* -- and then the line itself.
#:
#: It rides under either acknowledgement. **It is not a warning and must not
#: read as one**: entering a Champion Duel your alliance is not in is the whole
#: point of the control, so the common reader of this line has done nothing
#: wrong. The one it is for is the other one, who meant to enter their own next
#: set and mistyped a digit -- and for them the only tell otherwise is that the
#: acknowledgement said "Recorded" where they expected "Added your".
#:
#: ℹ️ rather than ⚠️ for exactly that reason.
#: **His is shorter than the draft and drops the consequence clause.** That was
#: *"...so this is not the Champion Duel your alliance is in"*, which explains
#: something the reader can already see: they are looking at an acknowledgement
#: that says "Recorded a Champion Duel" rather than "Added your Participating
#: Warzones". "Heads up" carries the whole job the sentence had.
CD_NOT_YOUR_WARZONE = "ℹ️ Heads up, your warzone (**{warzone}**) is not in that list."
#: Approved by Kevin, 2026-08-31: *"Keep the two-sentence version."* The
#: finished state's own line, and the only string that differs between the
#: finished hub and the live one now that they are one surface. Both sentences are lifted from the approved `build_finished_embed`
#: copy of 2026-08-15, minus the paragraph break it needed as a standalone
#: description: it now sits where the calendar line sits, so it is two
#: sentences rather than two paragraphs. **The second sentence is the one that
#: has been advertising a control that did not exist**, and it stays because
#: `CD_BTN_ADD_CD` is now that control.
CD_FINISHED_LINE = (
    "The Champion Duel {whose} has finished. "
    "You can record past Champion Duel results to keep a historical record, "
    "and enter the next one here as soon as the draw is visible in game."
)
# 📥 is the catalog's "data coming into the bot", which is what a pasted group
# listing is. Not ➕: `CD_BTN_ADD` already carries that on this grid, and two of
# one glyph side by side give the eye nothing to navigate by.
# 🏅 is the game's own mark for a standing: its Ranking line carries a medal
# badge, so `DESIGN.md` rule 5 (borrow the game's iconography) has something to
# take here. It is also legible at button size, which 📇 was not -- Kevin could
# not identify that glyph at 200% zoom, and an icon nobody can read is doing
# none of the scanning work an emoji is on a label to do.
# 👥 was the obvious choice and is Member Sync's, which rule 3 puts out of reach.
#
# 🏟️ SETTLED BY KEVIN, 2026-09-01, and taken on its own merits rather than
# forced. This shared 🏅 with `CD_BTN_STANDING` and that was never a rule 7
# collision -- the two are never drawn together, because knowing who the reader
# is is exactly what swaps one for the other. What decided it is whose mark 🏅
# is: the game's Ranking line carries a medal badge, so it belongs to the
# surface about your rank. A stadium is the field of eight you are drawn
# against.
#
# 🥊 was offered and refused, for the third time: *"I have declined the boxing
# glove before, it looks like a red lightbulb to me."* Written down so it is
# not offered a fourth.
CD_BTN_GROUP = "🏟️ Your group"
# 🏅 is the catalog's *"one player's standing in a round: their rank, and the
# group it is a rank within"*, which is this surface word for word. **It is now
# the only claim on that mark**: `CD_BTN_GROUP` shared it until 2026-09-01 and
# took 🏟️, which leaves the game's own Ranking badge on the surface that is
# actually about a rank.
CD_BTN_STANDING = "🏅 Your standing"
# Approved by Kevin, 2026-08-24, as one of the four IA labels
# (`PLAN_champion_duel_ia.md`, *Settled names*). The words are not open.
#
# 🏰 SETTLED BY KEVIN, 2026-08-26, over 📇 and over retiring the label. It
# shipped on 📇 and flagged rather than substituting a mark silently, because
# the label was his: `CD_BTN_GROUP`'s comment records 📇 as **retired for being
# illegible at button size** -- *"Kevin could not identify that glyph at 200%
# zoom"* -- and `CD_BTN_INTEL`'s lists ♟️ as ruled out "which is what retired
# 📇". He settled the mark; the words were never in question, and the
# retirement note now stops contradicting a live label.
#
# 🏰 was uncatalogued and has its own `DESIGN.md` Action-catalog row as of the
# same day. `DESIGN.md` had no glyph for *the people on your side*, which is the
# gap that pushed the session to 📇 in the first place: 👥 is Member Sync's and
# out of reach by rule 3, ➕ and 🏅 are taken on this same grid, and 🗂️, 🤝
# and 🛡️ are spoken for elsewhere. 👪 was the other free one and is a
# household rather than a side.
CD_BTN_ALLIANCE = "🏰 Your alliance"
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
# The way back into the form after it refuses. Member voice, and deliberately
# so: it pairs with "Use what I entered" on the squads button, which `UX.md`
# names as one of the two things a voice sweep must never touch.
#
# ✏️ is the catalog's "edit, change" and the thing about to happen is an edit.
# ↩️ was considered and is wrong -- its catalogued sense is cancelling a step
# that has not happened, where this reopens one that has.
#
# ONE LABEL ON ALL THREE REFUSALS, and that is Kevin's call rather than an
# economy. A name missing, a name wrong and a name on two warzones are three
# states, but the button does the identical thing in each, and one label a
# member learns once beats three they read separately.
CD_BTN_INTEL_RETRY = "✏️ Edit what I entered"
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

#: Rows of a player listing on one page. Kevin's number, 2026-08-24, and it is
#: the rule for any long listing in this feature rather than for one surface.
#:
#: **Filtering is the fix and this is the fallback**, in that order, and the
#: order is the whole finding. The complaint about a 100-player qualifier group
#: was never that a hundred rows are too many to scroll: it was that all
#: hundred are strangers. Paging an unfiltered hundred hands the reader five
#: screens of strangers instead of one, so the filter comes first and this
#: catches whatever survives it.
GROUP_PAGE_SIZE = 20

#: Named alliances offered in the group filter, at most. A select holds 25
#: options and the unfiltered entry takes one of them.
#:
#: A hundred players drawn from sixteen warzones can easily carry more than
#: twenty-four alliances, so this cut is real rather than theoretical. Ordered
#: biggest first so what it drops is the one- and two-player tail, and the
#: unfiltered option says how many were dropped: a filter that silently omits
#: alliances reads as "your alliance is not in this group".
_ALLIANCES_SHOWN = 24

#: The unfiltered option's value. A sentinel rather than an empty string, which
#: Discord will not carry as a select value.
_FILTER_ALL = "__all__"

# Signed off by Kevin, 2026-08-24, each shown on the Discord surface it sits on
# rather than put to him as a list. The variants are spent; the reasoning is
# not, so it stays.
#
# `_STAGE_NOT_HELD` is the description under a round we hold nothing for. It
# has to say the record is empty without saying the round is, because the two
# were indistinguishable and that is the bug being fixed. "we", not "I": this
# is what the record holds (`notes/UX.md`). Taken as it shipped.
#
# `_FILTER_ALL_LABEL` is the way back to the unfiltered list. Bare, like every
# other option in its set: they differ by which alliance, which is a parameter
# rather than a kind (`notes/DESIGN.md` rule 7).
#
# It shipped as "Everyone". Kevin: *"if this is truly the Alliance filter, then
# it should say 'All Alliances' and not 'Everyone'."* Sentence case, so the
# second word is lowercase: an alliance is not a proper noun here
# (`notes/DESIGN.md`, *Labels*).
#
# ONE CONSEQUENCE, RECORDED RATHER THAN FIXED. Players we hold no `[TAG]` for
# are in this list and are not an alliance, so the label names the option after
# most of what it contains rather than all of it. That objection was put to him
# beside the wording and he chose the wording anyway. Do not re-open it, and do
# not "fix" it by dropping those players from the option: this is the way back
# to the whole list, and a whole list with people missing is the one thing it
# cannot be.
_STAGE_NOT_HELD = "Nothing recorded yet"
_FILTER_ALL_LABEL = "All alliances"

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

# Signed off by Kevin 2026-08-25, and he asked for it shorter than every
# variant that was offered: "This should say 'Updated 3 hours ago' or 'Update
# 18 Aug'. Keep it even more simple than what you're proposing here."
#
# THE ARGUMENT THIS DECISION OVERRULED IS KEPT, because it is the reason
# somebody will want to lengthen it again. The review reasoned that "Last
# updated ..." names a mechanism rather than saying what the reader needs to
# know. Kevin weighed that and chose plainness anyway. Do not reinstate the
# longer form.
#
# The caveat over a STALE stored answer, and the only state that carries one.
# A `fresh` answer is exactly what a run right now would produce, so it shows
# with no timestamp and nothing hedged; a `missing` one is never shown at all.
#
# IT DELIBERATELY DOES NOT SAY WHAT CHANGED, and that survives the shortening.
# `stale` is a fingerprint mismatch, and a squad recorded, a hero power
# corrected, a trial count raised and a new engine pin all reach it
# identically -- so "new data has arrived" would be false on half of them.
#
# AND IT PROMISES NOTHING. The obvious second half -- "so we are working out
# new ones" -- is false in two states this surface can reach, both checked in
# `champion_duel_store` rather than assumed:
#
#   * `GROUPINGS_SWEPT` is 2, so a group in any older Champion Duel is never
#     picked up. The round picker reaches those, and a deploy that moves the
#     engine pin marks every stored answer in them stale at once. There the
#     promise would never come true at all.
#   * While somebody is recording a group, each write resets the debounce
#     `due()` measures, so nothing is worked out until they stop.
#
# `{when}` is Discord's own relative stamp `<t:N:R>`, so each reader sees the
# age in their own terms. NOTE FOR ANYONE REVISITING THIS: Discord picks that
# wording, not us, so a week-old answer reads "Updated 7 days ago" rather than
# "Updated 18 Aug". Kevin was told that and took the relative form. Switching
# to a date past some age is a code change, not a copy one.
_ODDS_AS_OF = "Updated {when}."

#: What a group answer is and is not, under every surface that shows one.
#:
#: LIFTED OUT OF `build_odds_embed` RATHER THAN COPIED. `🏅 Your standing`
#: shows one row of the same answer and needs the same caveat, and a second
#: literal saying it in slightly different words is how the two drift -- which
#: is the drift `_ODDS_AS_OF` already caught once on this feature.
_ODDS_BASIS = (
    "Ranked on points across all 21 matches, not matches won. Squads "
    "we have not seen are sampled, so these carry that uncertainty."
)

#: What the odds table was computed over, and what its two columns mean.
#:
#: **Two constants rather than one sentence**, and the split is where the
#: fields are: a test can assert the trial count line without knowing how many
#: get out of a group, and the column line without knowing the trial count.
#: They were f-strings inside `build_odds_embed` until the 2026-08-26 sweep
#: moved *round* to *stage* and the test guarding them carried its own copy of
#: the words -- the `_ODDS_AS_OF` failure, one surface along.
_ODDS_OVER = "Over {trials:,} simulations of the stage."
_ODDS_COLUMNS = (
    "The first column gives the odds of finishing in the top **{advance}** and "
    "going through, the second the odds of winning the group outright."
)

#: The same, for the bracket, which is averaged over seedings rather than run
#: against the one anybody will get.
_BRACKET_BASIS = (
    "Seeding isn't set yet, so each of {trials:,} simulations runs a "
    "different bracket. Squads we haven't seen are sampled."
)


# Signed off by Kevin 2026-08-24. "I", not "we": this is the bot unable to act
# on what it was given, not a statement about what the record holds.
#
# IT USED TO CARRY A SECOND SENTENCE — "Open it again and fill in both names" —
# and losing it is the point rather than a trim. "Open it again" meant scrolling
# back up a busy channel to the hub message, which is the dead end this refusal
# now hands a button out of. The button carries the action; the sentence would
# be naming a worse route to the same place.
_INTEL_NEEDS_BOTH = "⚠️ I need both players for a head to head."


# ── `🏅 Your standing` ───────────────────────────────────────────
#
# SIGNED OFF by Kevin, 2026-08-25, over two rounds on a page rendering each
# string on the surface it sits on. Two verdict strings and the reward-band
# sentence were struck rather than reworded: see `_standing_worked_out`.
#
# Written to the standing rules: US English, sentence case, no em dashes, and
# the voice split where **"I" acts and "we" holds**. The claim flow next door
# is the one place that split does not reach -- a claim is a fact about the
# reader rather than about the record, so `champion_duel_claim` says "your" --
# and this block is on the other side of that line. It is describing what we
# hold, so it holds.
#
# EVERY STRING THAT NAMES THE CLAIM FLOW IMPORTS ITS CONSTANT instead of
# repeating the words. `champion_duel_claim` owns that copy, it is signed off,
# and a second copy here is how the two drift.

#: The landing when nobody has told us which player the reader is.
#:
#: **It does not say "claim".** Last War uses Claim for collecting a reward and
#: `UX.md` disambiguates inherited game vocabulary rather than borrowing it,
#: which is the same call `CLAIM_BTN` made.
#:
#: Two sentences: what is missing, then what fixing it buys. `UX.md` principle
#: 3 puts the exit on the message, and `_StandingClaimView` carries it.
_STANDING_UNCLAIMED = (
    "We do not know which of these players is you.\n\n"
    "Tell us once and this opens on your own stage every time."
)

#: The control that opens `champion_duel_claim.ClaimModal` from a surface with
#: no player on it.
#:
#: NOT `CLAIM_BTN`. That one is "This is my account" and it lives on a card
#: already showing one row, where "this" has something to point at. Here there
#: is no row on screen yet, so the same words would be pointing at nothing and
#: `UX.md`'s label rule (the label describes the control) would be broken by
#: reusing them. The glyph is shared because the meaning is: 🔗 is the
#: catalog's link between two things.
CD_BTN_WHO_AM_I = "🔗 Tell us which account is yours"

#: Kevin asked for the control on 2026-08-26 and suggested the words, and
#: settled this wording on 2026-08-30.
#:
#: **What it fixes.** `_ALLIANCE_NO_TAG` fires when we hold somebody's account
#: and no alliance tag, and the only route to fix that was `➕ Add a player`.
#: That control genuinely works -- the tag is a field on `_AddPlayerModal` and
#: `upsert_registrant` fills a blank one on the row already held rather than
#: duplicating it -- but Kevin: *"the label says you are adding a player when
#: you are filling in one field about yourself."* The label describes the
#: control (`UX.md`), and that one described somebody else's.
#:
#: **Member voice, deliberately.** "my", not "your", because it pairs with
#: `🔗 This is my account` on the claim it depends on, and `UX.md` names that
#: pair as one of the two things a voice sweep must not touch.
#:
#: ✏️ is the catalog's *edit, change*, and an edit is what this is. Not ➕,
#: which is Create and is the word that made the old route read wrong.
CD_BTN_EDIT_ME = "✏️ Edit my information"

#: Kevin settled this on 2026-08-30. Same terms as the button above it.
#:
#: **It takes the button's own noun**, which is the rule Kevin set on the
#: claiming acknowledgements: the modal a control opens says what the control
#: said. It is also `_STANDING_RECORDED` word for word -- the heading over
#: exactly these fields on `🏅 Your standing` -- so the surface that sends you
#: here and the screen you arrive on name the same thing.
#:
#: Near-collision, checked rather than assumed: `champion_duel_claim`'s modal
#: is titled **Your account**, settled 2026-08-25. That one asks *which*
#: account is yours; this one holds the facts about it. Same shape as the
#: buttons that open them, which are `🔗 This is my account` and this.
_EDIT_ME_TITLE = "Your information"

#: Kevin settled this on 2026-08-30. Same terms as the two above.
#:
#: **The add flow's own note is wrong here and `/code-review` found it.** It
#: says *"was already here. Opening them instead of adding a duplicate"*, which
#: fires on every edit -- the member's own row always matches -- and tells
#: somebody the write was declined when it landed.
_EDIT_ME_DONE = "✅ Updated **{player}**."

#: ✅ SIGNED OFF by Kevin on 2026-08-30. He took **variant A**, the one
#: shipped as the placeholder, off the four enumerated in #556's body.
#:
#: **None of the four say "someone else's account", deliberately.** The row
#: it collides with may be unclaimed, or may be this member's own duplicate,
#: so naming an owner would be a guess stated as a fact.
#:
#: **It replaces `_EDIT_ME_NEW`, which described a bug.** That string announced
#: the second account a rename used to create. `✏️ Edit my information` now
#: renames the row the member already owns, so the normal case is
#: `_EDIT_ME_DONE` and there is nothing to announce.
#:
#: **What is left is the one case a rename cannot be.** The name and warzone
#: submitted are already a *different* registrant -- a real record with its own
#: squads and history -- so there are two accounts and choosing between them is
#: the member's call, not ours. `upsert_registrant` writes nothing
#: (`db.RenameCollision`), and this says so rather than picking for them.
#:
#: **Refusing rather than moving their claim, which was the alternative.** A
#: silent move abandons whatever sits on the row they were on; this costs one
#: press and they can see what they are choosing.
#:
#: **It names its exit**, unlike the string it replaces: the card this rides on
#: carries the claim pair, so `🔗 This is my account` is on screen underneath.
_EDIT_ME_COLLISION = (
    "⚠️ **{other}** is already a different account we hold, with its own "
    "squads and history. Nothing was changed. If that account is really yours, "
    "claim it with **🔗 This is my account**."
)

#: Kevin settled this on 2026-08-30. The hub's second line, under the warzone counts.
#:
#: **It had to change and it is session 6 that broke it.** The line said
#: *"Predict a match, or look up a player to see their squads and power.
#: Missing someone? **Add a player**."* and named two controls that are no
#: longer on the root: predicting is absorbed by `🔮 Today's picks` and
#: adding a player happens at the miss. Prose naming a button that is not on
#: the surface is the dead end `UX.md` principle 3 exists to stop, so the
#: sentence now names the one control that is there and says where the other
#: one is reached from.
#:
#: Variants are on the copy page rather than here, which is the rule this
#: project has paid for twice (`notes/CHAMPION_DUEL_INDEX.md`).
_HUB_ROSTER_LINE = (
    "Look up a player to see their squads and power. "
    "Missing someone? **{find}**, and add them from there."
)

#: The round we hold nothing for. Not an error: a Champion Duel that has not
#: reached its semifinals has no group to stand in, and saying so plainly is
#: the honest state rather than an empty table.
_STANDING_NO_ROUND = "We have not recorded a stage for you yet. You can add it with **{record}**."

#: The free half's heading. Kevin's, 2026-08-25, over "What we recorded".
#:
#: **Both headings open on the reader rather than on us.** "What we recorded"
#: and "What we worked out" describe the bot's two activities, which is the
#: same failure the whole information architecture rethink started from --
#: a surface describing itself to somebody who came to it about themselves.
#: "Your" is the word that puts them back at the front of it.
_STANDING_RECORDED = "Your information"

#: The paid half's heading, and the other half of that pair.
#:
#: **It names the thing rather than the act.** `🔮 Odds of advancing` is
#: already the feature's word for this, and a reader who presses that button
#: elsewhere should land on a field carrying the same noun.
_STANDING_WORKED_OUT = "Your odds"

#: Nobody has read a number off a screen for them yet.
_STANDING_NOTHING_RECORDED = "Nothing recorded yet."

#: When the numbers above were read. `<t:N:R>` renders per viewer, which a UTC
#: stamp cannot, and this feature is read across sixteen warzones.
#:
#: **"Updated", not "Read".** Kevin, 2026-08-25: *"Saying Read implies you're
#: telling the user when they last read this page or info."* It also lands it
#: on the same word as `_ODDS_AS_OF`, which is deliberate rather than a
#: coincidence: one surface, one word for when a number was taken.
#:
#: **`-# ` is Discord's subtext**, and it is here because Kevin asked for this
#: line smaller than the figures above it -- it is provenance, not a reading.
#: It has to start a line, which is why `_standing_recorded` joins it on a
#: blank line rather than mid-paragraph. The two sites the bot already had
#: (`ChampionDuelShareView.share`, `survey.py`) are both message content;
#: this is the first inside an embed field.
_STANDING_READ_AT = "-# Updated {when}."

#: The stage has no model, which today is the qualifiers and only them.
#:
#: **It names the stage rather than saying "this stage".** A reader who has
#: just switched the picker to the qualifiers needs to know it is the stage and
#: not the bot, and `STAGES_WITH_A_MODEL` is a fact about stages.
#:
#: The `{round}` field keeps its name. The 2026-08-26 sweep moved the copy and
#: left every identifier alone, so it collides with nothing else in flight.
_STANDING_NO_MODEL = (
    "We do not model the **{round}**, so there is no projection for it. "
    "Your rank and kill score above are what the stage is scored on."
)

# THE VERDICT AND THE REWARD BAND ARE GONE, and this note is here so nobody
# rebuilds them. `_STANDING_IN_IT`, `_STANDING_LONG_SHOT` and `_STANDING_BAND`
# told the reader whether they were still in it and which reward band their
# projected finish landed in. Kevin struck all three on 2026-08-25:
#
#   *"People can see on their own about the wins getting rewards -- this goes
#   back to us telling someone about the game when they play and already know
#   this. It's more about seeing how I stack up against the competition in the
#   duel itself."*
#
# The numbers left standing -- getting through, winning the group, projected
# finish -- already say where the reader sits, and nothing replaces the
# sentences. The rule is wider than this surface: the bot does not narrate the
# game back at somebody playing it. `PROPOSAL_champion_duel_ia.md` carries the
# correction against the bullet these were built on.
#
# The band values themselves were the one thing about them anybody had
# verified, so they are recorded in `notes/DESIGN_champion_duel_api.md` rather
# than lost with `_RANKING_BANDS`.

#: The store holds nothing for this group, or holds an answer computed against
#: a different set of people. Both are `missing`, and neither is showable.
#:
#: **UX.md principle 2, never fail silently.** An earlier draft rendered no
#: paid field at all in this state, which on a paying alliance is the half they
#: are paying for quietly absent with nothing said.
#:
#: **IT PROMISES NOTHING.** The obvious second half, "so we are working it out",
#: is false in two states `champion_duel_store` documents: `GROUPINGS_SWEPT` is
#: 2, so a group in an older Champion Duel is never picked up at all, and while
#: somebody is recording a group every write resets the debounce `due()`
#: measures. `_ODDS_AS_OF` was written to the same rule for the same reason.
#:
#: **THE NAVIGATION CAME OUT**, 2026-08-25, and it was Kevin who spotted why:
#: it said *"Run `/champion_duel` -> Your group -> 🔮 Odds of advancing"*,
#: and `PLAN_champion_duel_ia.md` session 6 retires `🏅 Your group` and
#: moves `🔮 Odds of advancing` onto THIS surface. The line would send
#: the reader where they already are.
#:
#: **IT IS NOT A DEAD END ANY MORE**, and it was one for four days. The
#: reasoning for dropping the navigation -- the press is on this embed already
#: -- was true of the surface session 6 builds and not of the one that shipped
#: here, where `_StandingClaimView` carried one button and it was the claim.
#: Session 6 landed it: `🔮 Odds of advancing` is on this message, and pressing
#: it computes the projection this sentence says we do not have. Do not re-add
#: a route in words to a control that is on the same message.
_STANDING_NOT_WORKED_OUT = "We do not have a projection for your group yet."

#: The first of the two figures `🏅 Your standing` opens on.
#:
#: **Getting out of your group is getting to the next stage**, which is the
#: word the game uses for the qualifiers, the semi-finals and the knockouts.
#: It said *round* until 2026-08-26 and one test carried the words rather than
#: reading them, so the sweep made it a constant on the way past.
_STANDING_THROUGH = "Through to the next stage"

#: The upsell, on the embed rather than on the disabled button, which cannot
#: carry a reason. It names what the model adds over the free half sitting
#: directly above it, because a reader can already see their own rank.
#:
#: Kevin's, 2026-08-25, and much shorter than the three-sentence version it
#: replaces. **It says "Premium" rather than interpolating
#: `premium.PREMIUM_BRAND`**, which is the house form elsewhere
#: (`premium.py:610`, `:631`) and would render "💎 LW Alliance Helper
#: Premium" here. Shipped as the words he approved; flagged in the pull
#: request rather than decided here.
_STANDING_LOCKED = "Your odds, projected finish placement, and more included in Premium."

#: The reader's account is in a different Champion Duel from the one their
#: guild is in. See `read_standing` for why this is a note rather than a prompt.
#:
#: WARNING: THE COMMUNITY SERVER CANNOT REACH THIS STATE, and this comment said
#: it could until 2026-08-25. Kevin asked, and the source disagreed with it.
#:
#: The mechanism is `read_standing`'s `if mine or not grouping`: a guild with no
#: Champion Duel resolved takes that branch, sets `here = True`, and never
#: reaches the comparison at all. `_in_this_champion_duel` has a `not grouping`
#: guard that looks like the reason and is not -- it is unreachable, since its
#: one call site is inside that `if`'s `else`. Worth being exact about, because
#: a comment naming the wrong line is the thing this correction is fixing.
#:
#: The real trigger is a guild that DOES have a Champion Duel, where the claimed
#: account's warzone is not in it -- somebody who switched warzone and stayed in
#: their old alliance's Discord, which is the case this answer exists for.
#:
#: **IT DOES NOT SAY THEY SWITCHED WARZONE.** It states which Champion Duel the
#: standing above is about, and stops. The reader knows whether they moved;
#: guessing for them is the noisy proxy this whole surface avoids, and the
#: control that fixes it is on the message either way.
#:
#: **It names THIS SERVER'S warzone, not the player's.** Kevin's sentence, and
#: his framing: the reader knows their own number, and the thing they cannot
#: see is which event the Discord they are standing in belongs to.
_STANDING_ELSEWHERE = (
    "**{player}** is not in the same Champion Duel as this Discord server's warzone"
)

#: The warzone `_STANDING_ELSEWHERE` names, when we have it. Its own constant
#: rather than an optional field in the one above, because the number really is
#: optional and `"({warzone})".format(warzone=None)` prints "(None)" rather
#: than nothing. Dropped whole when the guild's warzone is unavailable.
_STANDING_ELSEWHERE_WARZONE = " ({warzone})"


# ── `🏰 Your alliance` copy ───────────────────────────────────────────────────
#
# **Signed off by Kevin, 2026-08-26**, off a page that put the strings to him
# one block per string, each rendered on the Discord surface it sits on. Three
# of the twenty-two are still marked below: his answers reach this file through
# `HANDOFF_apply_alliance_copy_and_stage.md`, whose lists name nineteen.
#
# `notes/UX.md` is binding on all of it: US English, **"I" acts and "we"
# holds**, `odds` rather than `chance`, and sentence case on anything that
# reads as a label.

#: Nobody has told us which account the reader plays, so we cannot know whose
#: alliance to show.
#:
#: **Kevin cut it to two sentences on one line**, 2026-08-26, from a version
#: that ran what-is-missing and what-fixing-it-buys as separate paragraphs. It
#: still says both; it no longer needs the break to.
#:
#: **It does not reuse `_STANDING_UNCLAIMED`.** That one opens *"We do not know
#: which of these players is you"*, and "these players" is pointing at a roster
#: that is on screen there and is not here.
_ALLIANCE_UNCLAIMED = (
    "We do not have an account recorded for you. "
    "Tell us your account and we can find your alliance."
)

#: They hold a claim, on an account carrying no alliance tag.
#:
#: **The tag is a recorded field and a blank one is a gap in the record**, not a
#: statement that somebody is in no alliance -- `upsert_registrant` refuses to
#: let a blank overwrite an imported value for exactly that reason. So this
#: says what is missing, and the door under it is a control rather than a
#: sentence naming one, which is principle 3.
#:
#: **It used to end *"Add it with `➕ Add a player`, using the same name and
#: warzone"*, and Kevin cut that on 2026-08-26** -- the same day he asked for
#: `CD_BTN_EDIT_ME`. Sending somebody to *add a player* in order to fill in one
#: field about themselves is the thing that control exists to stop, so the
#: sentence naming it goes with it.
_ALLIANCE_NO_TAG = "We do not have an alliance recorded for **{player}**."

#: The tag is held and nobody carries it in this Champion Duel. Reachable when
#: a leader's own account is the only one we hold.
#:
#: **It does not say the alliance is not in the event.** We hold what people
#: entered, and one recorded account out of forty is the normal state of a
#: record nobody has filled in yet rather than a finding about the alliance.
#: **Kevin's wording says *recorded* outright** for that reason.
_ALLIANCE_NOBODY = (
    "We do not have anyone from **{alliance}** recorded in this Champion Duel yet.\n\n"
    "Anyone can add them one at a time with **{add}**, or paste a whole group "
    "in with **{record}**."
)

#: How much of this alliance we hold, said once at the top.
#:
#: **"Accounts", not "players" or "people".** A registrant is an account and
#: accounts change hands (`PROPOSAL_champion_duel_ia.md`, *What we hold is
#: accounts, not people*), and this is the one line on the surface that counts
#: rows rather than naming somebody -- the rows below all carry an in-game
#: name, which is what a leader recognises their team by.
_ALLIANCE_HELD = "{count} on file."

#: ✅ SIGNED OFF by Kevin on 2026-08-29 as **unchanged**, off
#: <https://claude.ai/code/artifact/5372637f-d147-4c58-ba1d-b4d4a51eaf3a>. It
#: had been on the page he answered on 2026-08-26 and he raised nothing against
#: it, but `HANDOFF_apply_alliance_copy_and_stage.md` named nineteen of the
#: twenty-two and this was one of the three it skipped -- so it was put in
#: front of him by name rather than counted as approved by silence.
#:
#: The field name over accounts we hold that are in no stage of this Champion
#: Duel.
#:
#: **Last, and named for the gap rather than for the people.** These are held
#: accounts nobody has placed, so the fact is about our record; a name like
#: "Not playing" would be a claim about the player that nothing supports.
_ALLIANCE_UNPLACED = "No stage recorded"

#: ✅ SIGNED OFF by Kevin on 2026-08-29, on the same page as the heading above
#: it. What the unplaced accounts need, said once under them rather than once
#: per row.
#:
#: **This is his own rewrite of the second sentence, not one of the options
#: offered.** *You can* is the opening he settled on the empty-round body on
#: 2026-08-24: it offers the reader something to do rather than telling them
#: what a button does. The trailing period is mine; he wrote none.
#:
#: `{record}` renders the button's own words rather than a name for it, which
#: is the rule set on the head-to-head modal -- the sentence and the control
#: under it cannot drift apart if the sentence reads the control.
#:
#: **The bold is mine, and the wording is untouched.** Kevin typed no markdown.
#: `notes/UX.md` is binding and says bold carries the noun the reader is looking
#: for, and every one of the eight siblings that names a control bolds it --
#: `_STANDING_NO_ROUND` is *"You can add it with **{record}**."*, which is the
#: sentence this one echoes. Unbolded, `_btn_words` strips the emoji too, so the
#: control's name would arrive as four ordinary words mid-sentence with nothing
#: marking it as the thing to press. Formatting rather than copy, recorded here
#: the same way the trailing period was.
_ALLIANCE_UNPLACED_BODY = (
    "We have these accounts on file but do not know their stage. "
    "You can add a stage by using **{record}**."
)

#: The Premium half, named for the thing rather than the act,
#: the same way `_STANDING_WORKED_OUT` is.
_ALLIANCE_LOCKED_FIELD = "Their odds"

#: The upsell, on the embed rather than on the disabled button, which cannot
#: carry a reason (`UX.md` principle 5).
#:
#: **It names what the model adds over the rows already above it**, which are
#: free and are most of the screen. A leader can already see who is where.
#:
#: **Kevin cut the "everything above is free" half** on 2026-08-26 and closed it
#: *"and more"* instead: the free rows are on the screen being read, so a line
#: spent saying they are free tells the reader what they can already see.
_ALLIANCE_LOCKED = "Premium sees their odds of getting through, of winning their group, and more."


# ── The personal reads ────────────────────────────────────────────────────────

#: The control that produces one read per player.
#:
#: 🎯 is `CD_BTN_INTEL`'s glyph and that is the point: this is that surface
#: applied to a whole team at once, so the mark that means "take aim at one
#: named opponent" is the one this should carry (`DESIGN.md` rule 5 -- somebody
#: who has seen one should recognise the other).
CD_BTN_READS = "🎯 Head to head for everyone"

#: Where a read is possible at all, said on the surface that offers it rather
#: than discovered by pressing.
#:
#: **The semi-finals and only the semi-finals**, and `db.ROUND_ROBIN_STAGES` is
#: what decides it rather than this sentence. A group of 8 meeting every other
#: once is the one round where the rest of the group IS somebody's opponent
#: list; the qualifiers are 100 players who do not all meet, and the knockouts
#: are a bracket whose pairings nothing in the schema holds.
#:
#: **It says "head to head", not "head to head reads."** Kevin, 2026-08-26.
#: The reader has just pressed `CD_BTN_READS`, which names the surface; the
#: word *reads* is ours for the output and made them learn a second noun for
#: something they had already found.
_READS_ROUND_ONLY = (
    "Head to head covers the **{round}**, where everyone in a group plays "
    "everyone else. We cannot say who meets who in the other stages."
)

#: The read's own opening line: which group it is about, and how many
#: meetings it covers.
#:
#: **IT DOES NOT SAY "REMAINING", AND THE MOCK DOES.** Kevin's page is headed
#: *"3 OPPONENTS REMAINING"*, and nothing the bot holds can say that: the only
#: record of a meeting having happened is `order_history.opponent`, which is a
#: sighting somebody chose to enter rather than a result, so an unrecorded
#: meeting and an unplayed one are the same row. What is true is that a
#: semi-final group of eight plays every one of the other seven, so the count
#: is the group less the player and the word "remaining" comes off.
_READS_TITLE = "🎯 {player}"
_READS_OPENER = "**{group}** · {count}."

#: Inline labels inside one opponent's block. **Kevin's own words, off the
#: mock he made** -- "Usually deploys", "Suggested answer" -- rather than
#: wording invented here.
#:
#: `_READ_ANSWER` is deliberately NOT `FIELD_YOURS`. That one says "Your
#: recommended line-up" and is right on `🎯 Head to head`, where the reader is
#: the player; here a leader is reading a page about somebody else and "your"
#: would name the wrong person.
#:
#: **The mock's third box has no constant here**, and that is deliberate.
#: "Moves squads around 50% of the time" is one of the two facts
#: `words.habit_line` already states, in copy Kevin settled on 2026-08-22 that
#: also carries the denominator -- how many meetings we could actually have
#: seen a change in. A second, shorter wording of the same measurement is how
#: the two drift, and the shorter one would be the one that overclaims.
_READ_DEPLOYS = "Usually deploys"
_READ_ANSWER = "Suggested answer"

#: The odds line at the head of one opponent's block.
#:
#: **"Odds", not "win chance."** The mock's own footer says *"estimated win
#: chance"*; `notes/UX.md` settles that word the other way, and the mock is a
#: picture rather than approved copy.
_READ_ODDS = "**{odds}** {player} wins"

#: Where there is no recommendation to price, the honest single figure does
#: not exist and the range is the answer.
#:
#: **`Envelope.mean` is never printed as the odds**, and that is the
#: correctness point rather than a style choice. `champion_duel_intel` states
#: it outright: weighting every configuration equally is the wrong prior, 63%
#: of real deployments are strongest-first, and quoting the mean as an estimate
#: "would be a worse claim than the one it criticises". The range is what one
#: number cannot carry, so the range is what goes here.
_READ_RANGE = "Runs from {floor} to {ceiling}, depending on what the two of them set."

#: This opponent cannot be read at all: a slot of theirs has no squad
#: recorded, so there is no line-up to put on the field.
#:
#: **The row stays on the page.** A leader handing this to a player needs to
#: see that one of their seven meetings is unanswerable, and which box fixes
#: it; a list that is quietly six long in one group and seven in another says
#: nothing at all.
_READ_NO_OPPONENT = (
    "We do not have a full line-up for them, so this one cannot be worked out. "
    "Slot(s) {slots} have no squad recorded. {path}"
)

#: What every figure on a read is, said once at the bottom.
#:
#: **"One match, not a meeting"** is the load-bearing half. Every probability
#: here is `best_of=1`, because a meeting is three matches with a redeploy
#: between them and pricing the advice at Bo3 would charge a decision to a
#: series the player gets to remake twice. It is also the figure the simulator
#: measured as actively wrong: `series_win_prob` amplifies a favourite by
#: 8.4pp against a real 0.4pp.
_READS_BASIS = (
    "Odds are for one match, not a whole meeting. Squad types we have not seen are inferred."
)

#: The player's own side is what is missing, which stops every one of their
#: meetings rather than one.
_READS_NEEDS_THEM = (
    "We do not have a full line-up for **{player}**, so none of their meetings "
    "can be worked out. Slot(s) {slots} have no squad recorded."
)

#: Nobody in the alliance is in a round these reads cover.
_READS_NOBODY = "We do not hold anyone from **{alliance}** in the **{round}** yet."

#: How many reads went out and, where it matters, who did not fit.
#:
#: **The cut is named rather than counted.** `CHAMPION_DUEL_INDEX.md`'s rule --
#: a filter that silently drops its tail reads as "your alliance is not in
#: this" -- applies to a bounded batch for the same reason, and a leader who
#: cannot see which of their people was left out cannot go and get them.
_READS_CUT = "The first {shown} by rank. Not included: {names}."

#: The control that hands the reads to the channel, and the line that rides
#: with them.
#:
#: Follows `CD_BTN_SHARE` to the word, because it is the same act on a
#: different payload: an ephemeral answer that the person who asked for it
#: chooses to make public. Private by default is `PROPOSAL_champion_duel_ia.md`
#: principle 5, and posting is the deliberate leadership half of it.
CD_BTN_SHARE_READS = "📤 Post these to current channel"

#: How many players one press reads for.
#:
#: **It bounds the cost, and that is what it is for.** The measured worst case
#: is about 450 ms of engine per player against a full semi-final group -- an
#: unscouted pair is a 1,296-cell grid at 57 ms -- so ten is about four and a
#: half seconds of Python holding the GIL of the process serving every guild.
#: That is a deliberate leadership press and it is two orders of magnitude
#: under the knockout bracket run this bot already does on one.
READS_PER_PRESS = 10

#: How many characters of embed one message may carry.
#:
#: **DISCORD'S RULE, AND IT IS NOT THE OBVIOUS ONE.** The ten-embeds-per-message
#: cap is the limit people know about; the one that actually binds here is that
#: the combined title, description, field names, field values and footer text
#: across *all* embeds on a message must not exceed 6,000 characters. A read is
#: one embed of roughly 2,500, so ten of them are four times over a limit that
#: is not about how many embeds there are.
#:
#: 5,500 rather than 6,000 leaves room for the message content riding with the
#: first batch. Anything that does not fit goes in another followup rather than
#: being dropped -- the reads are the deliverable, and a batching rule that
#: silently loses one would be the worst version of the cut this file already
#: refuses to make silently.
READS_CHAR_BUDGET = 5500

#: Discord's other cap on the same message, kept beside the one that binds so
#: neither is mistaken for the whole rule.
READS_EMBEDS_PER_MESSAGE = 10

# ── The day's picks ──────────────────────────────────────────────────────────
#
# THE DOOR. `db.set_slate` has had no caller outside its own tests since it
# shipped: the card renders, and no member could reach it. `_PicksView` below is
# the flow that fills one in, and its whole shape exists to satisfy one
# constraint -- **nothing here asks anybody to reproduce a name.**
#
# Both doors out of that were opened and closed on the same day (Kevin,
# 2026-08-27). Reading names off an image is out on cost and on evidence:
# *"there is never a consensus on how certain characters get read and
# displayed."* Typing is out because *"we can never guarantee that anyone in
# there would be able to be typed"* -- the field carries Korean and Tamil
# script, pipe padding and look-alike Cyrillic, and several names have no
# typeable substring on an English keyboard. Pasting into a modal was rejected
# as the primary path because it costs an app switch per name on a phone.
#
# So a meeting is chosen by tapping: **warzone, then Player 1, then Player 2.**
# The warzone number is under each portrait on the game's own Predict screen,
# which is the screen the maker is reading while they build the card.
#
# **Discord string selects have no type-ahead**, which settles the one open
# question the design left. Kevin called one *"the ideal UX"* and asked whether
# it exists. It does not: the only filtered pickers Discord offers are the
# auto-populated ones (user, role, channel), which pick Discord entities rather
# than arbitrary options, and slash-command autocomplete, which is typing by
# another name and is out for the reason above. The three selects were designed
# to work either way and this says which way it is.

#: The rounds a card can be built for. **Not the qualifiers**: the game runs no
#: prediction market on them, so the card never renders one (Kevin, 2026-08-27:
#: *"Qualifiers does not have prediction betting so there is no need to even
#: think about that."*).
#:
#: Derived from `db.STAGES` rather than typed out, so a fourth round the game
#: adds reaches this by being added there once.
PICK_STAGES = tuple(stage for stage in db.STAGES if stage != "qualifiers")

#: Options in one select. **Discord's ceiling, and deliberately not
#: `GROUP_PAGE_SIZE`.** That twenty is about how many rows a reader scans in an
#: embed before the list stops being a list; this is the hard limit Discord puts
#: on a component. Cutting a select to twenty would add a page for nothing.
_PICK_OPTIONS = 25

#: The three taps, in order, and the key each one's page is kept under. The
#: pager moves whichever of them the maker is currently working in.
_PICK_STEPS = ("warzone", "player", "opponent")

#: How many days forward the day picker offers, beyond today. A card is built
#: *"the day/evening/morning before"* (Kevin, 2026-08-27), so tomorrow has to be
#: one tap away, and the day after covers somebody working a night early.
_PICK_DAYS_AHEAD = 2

# ✅ SIGNED OFF by Kevin on 2026-08-29, off the picks sign-off page --
# <https://claude.ai/code/artifact/6cd70358-2103-4708-83f5-9684ddd4f098>.
# `_PICKS_NO_STAGE` and `_PICKS_TODAY` were the two he did not close that day.
# He closed both on 2026-08-29 off
# <https://claude.ai/code/artifact/5372637f-d147-4c58-ba1d-b4d4a51eaf3a>, and
# neither answer was a wording change: one deleted the string and the state
# behind it, the other moved the note he asked for onto the day picker.
#
# **THE GAME'S WORD IS `match`, AND IT IS NOW OURS.** Kevin, 29 Aug: *"Note
# that the game uses Matches to describe these"* and *"meeting = match"*. The
# schema still says `pick_meetings` and this module still has `_meeting_line`,
# because renaming a table is not a copy change -- but **nothing a member reads
# says "meeting" any more.**
#: ⚠️ The second sentence here was an INSTRUCTION, not copy, and it shipped.
#: Kevin's *"Note that the game uses Matches to describe these"* is the reason
#: for the meeting -> match sweep -- it is quoted in the comment directly above
#: for exactly that purpose -- and it was pasted into the string as well.
#: Removed 2026-08-30, on his say-so.
#:
#: Wrong three ways: nobody chose it and it was never on a sign-off page, the
#: sentence before it already says *matches* so it was circular, and it
#: narrates the game back at somebody playing it. Kevin, 2026-08-25: *"this
#: goes back to us telling someone about the game when they play and already
#: know this."*
_PICKS_INTRO = "To add matches to your picks, select a warzone and then the two players."
_PICKS_EMPTY = "No matches added yet"
_PICKS_NO_GROUPING = (
    "We do not know which Champion Duel your alliance is in yet. Set it with **{button}**."
)
# `_PICKS_NO_STAGE` was here and is GONE, along with the state it described.
# Kevin settled it on 2026-08-29 by refusing the gate rather than by changing
# the words: *"I think we could just have that as the default nothing here
# because maybe someone got a date wrong and then we're gating on that when we
# shouldn't."* -- *that* being `_PICKS_NO_FIELD` below, which is now the only
# empty state the flow has. `_pick_stage` carries the build.
#
# Deleted rather than kept for later. A string nothing draws is a string nobody
# can sign off, which is the rule that took `CARD_CONFIDENCE_HEADING` out of
# `champion_duel_picks`.
_PICKS_NO_FIELD = (
    "We hold no draw for the {stage} yet, so there is nobody to pick from. "
    "Record it with **{button}** and this card opens."
)
_PICKS_PICK_DAY = "Which day?"
_PICKS_PICK_CARD = "Which card?"
_PICKS_PICK_REMOVE = "Remove a match"
_PICKS_PICK_WARZONE = "Which warzone?"
_PICKS_PICK_P1 = "Player 1"
_PICKS_PICK_P2 = "Player 2"
_PICKS_PAGED = "{what}, page {page} of {pages}"
#: **No numeral, on either of them.** The card has carried none since session
#: A took them off (Kevin, 2026-08-27: *"get rid of the row #. Irrelevant"*),
#: and the text beside it dropped its own in session D -- so a number here
#: would be the last surface in this feature counting rows, and it would count
#: them in a third order: the bench lists meetings as they were entered, and
#: the card draws them strongest pick first. **A meeting is identified by its
#: place in the list, and the listing and the select below it are rendered from
#: the same list in the same order.** Found by `/code-review`.
_PICKS_MEETING = "**{a}** vs **{b}**"
_PICKS_MEETING_OPTION = "{a} vs {b}"
_PICKS_NOBODY_LEFT = (
    "Nobody is left in the {stage} to pick from. Everybody we hold has been knocked out."
)
_PICKS_NO_OPPONENTS = (
    "We hold nobody **{name}** can meet. Record the rest of their group with "
    "**{button}** and they show up here."
)
#: The half-made match, and the blank is doing the work the second select
#: cannot: it says an opponent is still owed rather than leaving the line
#: looking finished (Kevin, 29 Aug).
_PICKS_WORKING = "Entering match details: {a} vs {b}"
_PICKS_WORKING_HALF = "Entering match details: {a} vs _____"
#
# `_PICKS_DERIVED` was here and is gone with the behaviour it described.
# **Kevin took the preselect out on 29 Aug**: *"I do not know how this
# actually works out and would rather let the user choose, especially if we
# don't know the actual seed ranks it's safer that way."* Session C had the
# fold filling Player 2 in at the round of 32 -- seed i against seed 33 - i,
# measured 16 of 16 on one event -- and one event is not a rule.
#
# **The derivation still orders the list**, in `_opponents`: the partner is
# offered first out of 31 rather than chosen. That is what letting the maker
# choose looks like while still putting the likely answer where they will see
# it, and it makes no claim the surface has to stand behind.
_PICKS_ADDED = "✅ Added **{a}** vs **{b}**."
_PICKS_ROLLED = "✅ Added **{a}** vs **{b}**. Card {full} was full, so it went on card {n}."
_PICKS_REMOVED = "🗑️ Took **{a}** vs **{b}** off the card."
_PICKS_DELETED = "🗑️ Deleted the card for {day}."
# ⚠️ Kevin marked this UNCHANGED on 29 Aug, and it still says *meetings* --
# the one member-facing string that does, now that `match` is the word.
# **Not swept on his behalf**: he approved the sentence he was shown.
_PICKS_FULL = (
    "⚠️ All {cards} of this day's cards are full, at {picks} meetings each. "
    "Take one off before adding another."
)
_PICKS_SAME_PLAYER = "⚠️ A match needs two different players."
_PICKS_TAKEN = "Already on card {n}"
_PICKS_CARD_EMPTY = "Nothing yet"
_PICKS_CARD_COUNT = "{n} on this day"
# ✅ SIGNED OFF by Kevin on 2026-08-29, and the string itself never changed.
# Both halves of his answer landed somewhere else:
#
# Kevin, 29 Aug: *"We should add the day of the week here and we probably need
# to specify somewhere that this goes from the game's server time."*
#
# **The weekday is in `picks_lib.Slate.date_label`**, not here, because the
# card's own subject line reads off the same method -- so `{day}` arrives
# carrying it and the picker and the card cannot spell one day two ways.
#
# **The server clock became `_PICKS_DAY_CLOCK` below**, on this surface only.
# Kevin settled where on 29 Aug: *"I don't want it on the card, I think that
# just adds clutter and it's clear enough. This is more when someone is putting
# together the card and they select the day. We just need a note where they are
# inputting that info about this."*
_PICKS_TODAY = "{day} (today)"

# Kevin settled this on 2026-08-29, and the marker calling it a placeholder
# outlived that by a day and a merge. **A stale marker is worse than none** --
# it is what teaches the next reader that these can be skipped, which is how
# this one survived being merged.
#
# **The note is on the entry surface and nowhere else, and that split is his.**
# The person CHOOSING a day needs to know which clock decides it. The person
# reading the finished card does not, and the card travels to people who never
# opened the bot, so a line about server time there is clutter aimed at the
# wrong reader.
#
# It sits in the bench description, directly above the day select, because
# that select is the control it is about. It is not in the footer:
# `_PICKS_FOOTER_CAP` takes that slot on a full card, and a note that
# disappears once somebody has twenty matches is a note that vanishes exactly
# when the day has been decided.
_PICKS_DAY_CLOCK = "Days here follow the game's server clock, not your local time."
_PICKS_FOOTER_CAP = (
    "We can only add {n} matches per card. Adding more than {n} will generate another card."
)
_PICKS_NO_CARD = "There is nothing on this card yet, so there is nothing to draw."

# ✅ APPROVED, and it is the one label here that is not open. Kevin settled it
# 2026-08-24 as one of the four IA names (`PLAN_champion_duel_ia.md`, *Settled
# names*: *"They are the labels; do not re-open them."*), and
# `champion_duel_picks.PICKS_TITLE` is the same string approved the same day for
# the card's own title -- so the door and the thing behind it read as one name.
#
# 🔮 is the catalog's *forecast of something that has not happened*, which is
# what a picks card is. The other 🔮 in this file is `CD_BTN_ODDS`, which lives
# on the group view rather than this grid, so rule 7's
# never-repeat-a-glyph-across-a-choice-set holds.
CD_BTN_PICKS = "🔮 Today's picks"

# ⚠️ PLACEHOLDER LABELS from here, session E.
CD_BTN_PICKS_ADD = "Add a match"
CD_BTN_PICKS_SAVE = "Add match to card"
#: **Danger, and Kevin asked for it by name**: *"make this a Danger button
#: because it is a destructive action."* It clears the whole card, not one
#: match, and it is the only control in this flow that cannot be undone.
CD_BTN_PICKS_DELETE = "Remove all matches"
#: 🖼️ is the catalog's *image version of something that also has a
#: text form* (`notes/DESIGN.md`), which is this message exactly: the card is
#: drawn and every row of it is written out beside the drawing.
CD_BTN_PICKS_SHOW = "Create the card"
#: Follows `CD_BTN_SHARE` to the word, because it is the same act on a
#: different thing -- the same rule `CD_BTN_SHARE_READS` follows.
CD_BTN_PICKS_SHARE = "📤 Post this card to current channel"
# Bare, both of them: they are flow exits, which `notes/DESIGN.md` rule 7 names
# as established bare treatment.
CD_BTN_PICKS_RESTART = "Start over"
CD_BTN_PICKS_BACK = "Cancel and go back"


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
            await interaction.followup.send(_SHARE_DENIED, ephemeral=True)


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


class _PredictModal(discord.ui.Modal, title=CD_SIMULATE_TITLE):
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
#: The player card's list of where they have got to.
#:
#: A constant because the 2026-08-26 sweep moved it and five tests named the
#: old words, which is the `_ODDS_AS_OF` failure waiting to happen again.
FIELD_STAGES = "Stages"

#: The question every stage picker asks, on the record modal and on the group
#: view's own select.
#:
#: **The one picker question that is a constant, and only because it moved.**
#: Its siblings -- `Which group?`, `Which Champion Duel?`, `Which alliance?` --
#: are still literals at their call sites; the 2026-08-26 sweep changed this
#: one in two places and six tests, which is what the constant is for.
_PICK_STAGE = "Which stage?"


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


# Signed off by Kevin 2026-08-24. It was "What to field against a player",
# which described the one-sided surface. Shipped as the button's own words so
# pressing the button and reading the modal agree; the variants considered are
# in the PR. Copy is Kevin's.
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

    def __init__(
        self,
        *,
        opponent: str | None = None,
        opponent_server: str | None = None,
        you: str | None = None,
        your_server: str | None = None,
        origin: "_IntelRetryView | None" = None,
    ):
        """Optionally pre-filled, for the way back in after a refusal.

        Every argument is optional and the hub's own button passes none, so
        `_IntelModal()` keeps working unchanged.

        `origin` is the offer this was opened from, kept so that submitting
        can retire it. See `on_submit`.

        SAFE TO SET ON SELF, and this is the thing that gets "cleaned up" by
        someone who assumes a class attribute is shared: `Modal._init_children`
        deepcopies each declared item onto the instance, so a default set here
        cannot reach the next person who opens the modal. Verified against the
        installed library rather than taken on trust -- `discord/ui/modal.py`,
        `item = deepcopy(item)`. `_AddPlayerModal` in this file does the same
        for the same reason.
        """
        super().__init__()
        self.origin = origin
        for field, value in (
            (self.opponent, opponent),
            (self.opponent_server, opponent_server),
            (self.you, you),
            (self.your_server, your_server),
        ):
            if value:
                # Truncated off the field's own cap rather than a retyped
                # number, so widening a field cannot silently keep truncating
                # short. Discord rejects a default longer than `max_length`.
                field.default = value[: field.max_length]

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
            await self._refuse(interaction, _INTEL_NEEDS_BOTH)
            return

        them = await _resolve(self.opponent.value, self.opponent_server.value or None)
        if isinstance(them, str):
            await self._refuse(interaction, them)
            return

        you = await _resolve(self.you.value, self.your_server.value or None)
        if isinstance(you, str):
            await self._refuse(interaction, you)
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
        # Answered. The control that existed to fix the question is spent, and
        # left live it would invite the member to redo work that worked.
        await self._retire_origin()

    async def _retire_origin(self) -> None:
        """Grey out the offer this modal was opened from, if there was one.

        ONLY WHERE SOMETHING REPLACES IT -- a newer offer, or the answer the
        member was after. Two live offers is the failure this exists to stop:
        the older one hands back the older text, so a member who fixed one
        name, failed on the other, and then reached back for the wrong of two
        near-identical ephemeral messages would silently lose the correction
        they had already made.

        THE BUTTONLESS REFUSALS DELIBERATELY DO NOT CALL THIS. A missing
        engine, the paywall and an opponent with no squads on file all end in
        a message with nothing to press, and on those paths the offer above is
        not stale -- it holds the member's only remaining copy of what they
        typed. Greying it there would take their way back and give them
        nothing in exchange, which is the dead end the whole view exists to
        end rather than a tidier version of it.

        On submit rather than on press, either way. A button that spent itself
        the moment it was pressed would strand anyone who dismissed the modal
        without submitting, and that is one stray tap on a phone.
        """
        if self.origin is not None:
            await self.origin.retire()

    async def _refuse(self, interaction: discord.Interaction, message: str) -> None:
        """Say what is wrong, and hand back the form with what was typed in it.

        Without this the member's only route back is to scroll up a busy
        channel to the hub message they pressed the button on, and everything
        they typed is gone. On a misspelling the prefill is the whole point:
        the near-miss name is sitting in the box with `_suggestion_line`'s
        "Did you mean" beside it, one character from being right.

        A BUTTON RATHER THAN THE FORM ITSELF, and that is the only shape
        available rather than a preference. Discord will not accept a modal as
        the response to a modal submission; a component interaction can send
        one, so the reply has to carry a button and the button opens the form.

        `_ENGINE_MISSING` is excluded, because `_resolve` returns it when
        `db.NAMES_AVAILABLE` is false, and a retry button on it would be lying
        about what happens next -- nothing the member can type installs an
        engine. The two flags come from the same package and in practice fail
        together, but `on_submit`'s own engine check reads
        `intel_lib.ENGINE_AVAILABLE` while this path reads
        `db.NAMES_AVAILABLE`, so a partial install is a real way to arrive here
        with it.
        """
        if message == _ENGINE_MISSING:
            await interaction.followup.send(message, ephemeral=True)
            return
        view = _IntelRetryView(
            user_id=interaction.user.id,
            opponent=self.opponent.value,
            opponent_server=self.opponent_server.value,
            you=self.you.value,
            your_server=self.your_server.value,
        )
        # Superseded by the offer about to go out, which carries what was just
        # typed. Retired before rather than after, so there is never a moment
        # with two live buttons holding two different answers.
        await self._retire_origin()
        # The view holds its own message so it can retire the button -- on
        # timeout, and when a later submission supersedes this offer. Without
        # it `self.message` is None and the button stays live-looking on a
        # dead view.
        #
        # `wait=True` is explicit rather than load-bearing: `Webhook.send`
        # already forces it for an application webhook, which an interaction
        # followup is (`discord/webhook/async_.py`, `if application_webhook:
        # wait = True`). Stated because the neighbouring `_DisagreementView`
        # carries a comment claiming the flag is what makes the return value
        # arrive, and it is not.
        view.message = await interaction.followup.send(
            message, view=view, ephemeral=True, wait=True
        )


class _IntelRetryView(discord.ui.View):
    """Reopen the head-to-head form with what was typed still in it.

    The message this rides on is ephemeral and so already private to the one
    member, which makes `interaction_check` unreachable rather than wrong. It
    is here because every other view in this file carries it -- `_MissView`,
    `_DisagreementView`, `_RetryGroupingView` -- and a single view that quietly
    opts out reads as a considered exemption to whoever finds it next.
    """

    def __init__(
        self,
        *,
        user_id: int,
        opponent: str | None,
        opponent_server: str | None,
        you: str | None,
        your_server: str | None,
    ):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.opponent = opponent
        self.opponent_server = opponent_server
        self.you = you
        self.your_server = your_server
        self.message: discord.Message | None = None

        button = discord.ui.Button(label=CD_BTN_INTEL_RETRY[:80], style=discord.ButtonStyle.primary)
        button.callback = self._on_retry
        self.add_item(button)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    async def retire(self) -> None:
        """Grey the button out. A later submission has superseded this offer.

        Disabled rather than removed: two near-identical ephemeral messages sit
        one above the other by this point, and a greyed control says which of
        them is the stale one where a vanished control would just leave the
        member looking for it.
        """
        if self.is_finished():
            return
        for item in self.children:
            item.disabled = True
        self.stop()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except Exception:
                # Deleted, expired, or the connection went while we asked.
                # Deliberately everything, not just `HTTPException`: this is a
                # cosmetic edit standing between the member and their answer,
                # and a dropped connection here would otherwise raise straight
                # out of `on_submit` after the defer and cost them the whole
                # submission over a greyed button. The view is stopped either
                # way, so the button is already dead wherever it still draws.
                # `wizard_registry.expire_view_message` swallows the same for
                # the same reason.
                pass

    async def _on_retry(self, inter: discord.Interaction):
        # Deliberately does NOT retire this view. Dismissing a modal without
        # submitting is one stray tap on a phone, and a button that spent
        # itself on that tap would strand the member in the exact dead end
        # this view exists to end. `_IntelModal.on_submit` retires it instead,
        # once the member has actually submitted something.
        await inter.response.send_modal(
            _IntelModal(
                opponent=self.opponent,
                opponent_server=self.opponent_server,
                you=self.you,
                your_server=self.your_server,
                origin=self,
            )
        )


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
        embed.add_field(name=FIELD_STAGES, value=rounds[:1024], inline=False)

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
        text=_PICK_STAGE,
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
        self,
        *,
        player: dict,
        user_id: int,
        can_write: bool,
        grouping: dict | None = None,
        claim: dict | None = None,
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

        # WHERE THE GUIDE LIVES NOW. It was a hub button until session 6, and
        # `PLAN_champion_duel_ia.md` attaches it to the entry flows it
        # explains: its two screens are the deployment order and one squad's
        # power and type, which is exactly what the two controls beside it ask
        # for. Beside them is where somebody looks when the question occurs to
        # them, and on the hub it was a shelf between them and the answer.
        #
        # NEVER LOCKED, and that is unchanged. Documentation is not a paid
        # surface: someone deciding whether the feature is worth paying for
        # should be able to see what contributing involves, and withholding a
        # picture of a game screen protects nothing.
        guide = discord.ui.Button(label=CD_BTN_GUIDE[:80], style=discord.ButtonStyle.secondary)
        guide.callback = self._on_guide
        self.add_item(guide)

        # The one control here that is not a contribution, so it is never
        # locked: a claim says who the reader is, not what they saw.
        #
        # This is the point of need for it. Somebody who has just looked
        # themselves up is already staring at their own row, which is the whole
        # of "pick yourself out and we remember" -- and the claim is what gives
        # every other Champion Duel surface a "you" to open on.
        claim_lib.add_claim_button(self, player=player, claim_row=claim, user_id=user_id)

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    async def _on_guide(self, inter: discord.Interaction):
        """The annotated screens, beside the controls that ask for them.

        A modal cannot carry an image and putting the guide in front of one
        would charge everybody who already knows an extra press on every entry,
        so it stays its own control rather than a step.
        """
        embeds, files = build_guide()
        await inter.response.send_message(embeds=embeds, files=files, ephemeral=True)

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
    # Both reads at once. The claim decides which half of the claim pair the
    # card renders, and a label that says what the control does has to know
    # before the view is built rather than when it is pressed.
    top, claim = await asyncio.gather(
        asyncio.to_thread(db.most_common_order, player["id"]),
        asyncio.to_thread(db.get_claim, player["id"]),
    )
    view = PlayerActionsView(
        player=player,
        user_id=interaction.user.id,
        can_write=can_write,
        grouping=grouping,
        claim=claim,
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

        # Beside the control it explains, which is where session 6 put it:
        # `_AddPlayerModal` asks for three squad powers and their types, and
        # the guide's second screen is where to read them off. Never locked.
        guide = discord.ui.Button(label=CD_BTN_GUIDE[:80], style=discord.ButtonStyle.secondary)
        guide.callback = self._on_guide
        self.add_item(guide)

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

    async def _on_guide(self, inter: discord.Interaction):
        embeds, files = build_guide()
        await inter.response.send_message(embeds=embeds, files=files, ephemeral=True)


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


def _edit_me_modal(player: dict, *, can_write: bool, grouping: dict | None):
    """`✏️ Edit my information`: the add modal, opened on the reader's own row.

    NOT A SECOND MODAL, and that is the point rather than a saving. The fields
    are the same five and a member correcting their own entry is the same write
    as somebody entering it, so a parallel modal would be two screens that have
    to be kept saying the same thing.

    **The one difference is which row the write lands on**, and it is a single
    argument rather than a second screen: this passes `rename_id`, so a name or
    warzone the member changes is written onto the row they already own instead
    of creating a second account.

    **Only from a claim.** Without one there is no "my", which is why every
    call site here sits behind a state that already resolved a claimed player.
    """
    return _AddPlayerModal(
        can_write,
        name=player.get("display_name"),
        server=player.get("server"),
        alliance=player.get("alliance"),
        thp=player.get("thp"),
        troop_level=player.get("troop_level"),
        grouping=grouping,
        editing=player,
    )


async def _open_edit_me(inter: discord.Interaction, *, can_write: bool, grouping: dict | None):
    """`✏️ Edit my information`, opened on the claim as it stands right now.

    READ BEFORE RESPONDING, not after. A modal has to be the first response to
    an interaction so this cannot defer first, and one indexed SQLite read is
    well inside the three seconds -- `_on_record` takes the same route for the
    same reason.

    **Fresh rather than off the view.** Both views that carry this button live
    ten and fifteen minutes, and a claim can move from another message inside
    that window -- `ClaimResultView` has a release button that does exactly
    that. A snapshot taken when the message was sent would prefill an account
    the reader gave up, and then write to it. Found by `/code-review`.
    """
    player = await asyncio.to_thread(db.get_claimed_registrant, inter.user.id)
    if player is None:
        await inter.response.send_message(claim_lib.CLAIM_NOT_LINKED, ephemeral=True)
        return
    await inter.response.send_modal(_edit_me_modal(player, can_write=can_write, grouping=grouping))


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
        alliance: str | None = None,
        thp=None,
        troop_level=None,
        editing: dict | None = None,
    ):
        # `editing` is the registrant this was opened on, and it carries the
        # whole difference between the two flows: the title, the
        # acknowledgement, whether a blank box clears, and -- through
        # `rename_id` -- whether a changed name renames that row or creates a
        # new one. None is the add flow, which is the default.
        super().__init__(**({"title": _EDIT_ME_TITLE[:45]} if editing else {}))
        self.can_write = can_write
        self.grouping = grouping
        self.editing = editing
        # Safe to set on self: `Modal._init_children` deepcopies each declared
        # item onto the instance, so a default here cannot leak to the next
        # person who opens this modal.
        if name:
            self.name.default = name[:64]
        if server:
            self.server.default = server[:10]
        # ALL FIVE OR NONE, for `CD_BTN_EDIT_ME`. A member opening their own
        # record to change one field must see the other four as we hold them:
        # a blank box beside a filled one reads as "we have nothing", which is
        # the surface lying about its own record.
        #
        # **This is also what makes clearing safe to offer.** A box on the edit
        # flow shows what we hold, so emptying one is a member disagreeing with
        # something they can see rather than a guess about a field they were
        # never shown. See `_blank_means`.
        if alliance:
            self.alliance.default = str(alliance)[:8]
        if thp:
            # The separator form rather than `325.8M`. `parse_power` reads both
            # and this one round-trips exactly, where the short form re-enters
            # as a number rounded to one decimal place -- a member who changed
            # nothing would have their Total Hero Power moved by pressing save.
            self.thp.default = f"{float(thp):,.0f}"[:16]
        if troop_level:
            for option in self.troop_level.component.options:
                option.default = option.value == str(troop_level)

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

    def _blank_means(self):
        """What an empty box on THIS submission means: nothing, or `db.CLEAR`.

        **The add flow is unchanged and stays unchanged.** Somebody adding a
        player they just met has no idea what we already hold, so a box they
        left alone is an omission and `upsert_registrant` writes nothing for
        it. That rule protects imported values well outside this control and is
        not what Kevin re-opened.

        **The edit flow is the one place a blank box is a statement.** It opens
        on the member's own row with all five fields filled in as we hold them
        -- that is the ALL FIVE OR NONE rule above -- so somebody looking at
        their alliance tag and deleting it has said something, and until now the
        save silently declined to hear it. Kevin, 2026-08-29, on whether *Edit*
        may empty a box: yes.

        **It used to need the submitted identity checked back against the row
        this opened on, and no longer does.** Name and warzone are both
        editable here, and changing either used to land the write on a
        DIFFERENT registrant -- where the boxes were filled from somebody
        else's row, so a cleared one said nothing about theirs. `rename_id`
        removes that case at the root: the write lands on the row this modal
        opened on or it does not happen at all (`db.RenameCollision`, which
        writes nothing). So there is no longer a submission where the boxes
        came from one row and the clear would reach another.
        """
        return db.CLEAR if self.editing else None

    def _note(self, player: dict, *, existing: bool) -> str:
        """What just happened, in the terms of the flow that opened this.

        The add flow's two notes are about a player the caller may not have
        held; the edit flow's are about the caller's own row, where "already
        here" is the normal case rather than the interesting one.
        """
        if self.editing is None:
            return (
                f"ℹ️ **{_label(player)}** was already here. "
                "Opening them instead of adding a duplicate."
                if existing
                else f"✅ Added **{_label(player)}**."
            )
        # No second branch any more. A rename lands on the row this opened on
        # or raises, so by the time there is a `player` to acknowledge it is
        # always theirs -- `on_submit` answers the collision and returns before
        # reaching here.
        return _EDIT_ME_DONE.format(player=discord.utils.escape_markdown(_label(player)))

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

        if self.editing is not None:
            # **RE-READ THE CLAIM, do not trust the snapshot this opened on.**
            # `_open_edit_me` reads it fresh, and then this modal sits open for
            # as long as the member leaves it open -- during which
            # `ClaimResultView` can release it or move it from another message.
            # Without this, `rename_id` would rename, and CLEAR fields on, an
            # account they no longer hold, using boxes filled from it.
            #
            # `champion_duel_claim._pressed` settles the identical window the
            # identical way: re-read, and if it has moved say `CLAIM_NOT_LINKED`
            # and change nothing. Found by `/code-review`.
            claim_now = await asyncio.to_thread(db.get_claimed_registrant, interaction.user.id)
            if claim_now is None or claim_now["id"] != self.editing.get("id"):
                await interaction.followup.send(claim_lib.CLAIM_NOT_LINKED, ephemeral=True)
                return

        existing = await asyncio.to_thread(db.find_registrants, name, server)
        # See `_blank_means`. `None` on the add flow, which writes nothing for
        # an empty box; `db.CLEAR` on the edit flow, which empties the column.
        blank = self._blank_means()
        try:
            player = await asyncio.to_thread(
                db.upsert_registrant,
                name,
                server=server,
                # **The whole of the rename, and it is opt-in on purpose.**
                # `➕ Add a player` passes nothing here and still creates,
                # which is what it is for -- somebody entering an opponent
                # must get a new row, and renaming there would overwrite a
                # different player. `✏️ Edit my information` names the row
                # the member already owns, so a new name and warzone are
                # written onto it rather than INSERTing a second account.
                rename_id=(self.editing or {}).get("id"),
                alliance=(self.alliance.value or "").strip() or blank,
                thp=thp if thp is not None else blank,
                # **NOT `blank`, and that is deliberate.** The other two are
                # text boxes, which always submit their contents, so an empty
                # one is unambiguously an empty one. This is a select, and an
                # empty `values` means either "deselected" or "Discord did not
                # echo the default back" -- and those are the same payload. A
                # wrong guess here silently wipes the troop level of every
                # member who opens this screen and changes something else, so
                # the level keeps today's behaviour and cannot be cleared from
                # here. Raised by `/code-review` as the one thing it could not
                # settle, and it is right that it cannot be settled from the
                # payload.
                troop_level=level,
                origin="self_reported",
                actor=_actor(interaction),
            )
        except db.RenameCollision as exc:
            # Two real records, so this is a merge question rather than a
            # rename and it is not ours to decide. Nothing was written.
            await interaction.followup.send(
                _EDIT_ME_COLLISION.format(
                    other=discord.utils.escape_markdown(_label(exc.existing))
                ),
                ephemeral=True,
            )
            return
        except db.NoSuchRegistrant:
            # The row this opened on is gone -- merged away or removed while
            # the modal sat open. Nothing was written, and the claim copy is
            # the one that already describes holding no account.
            await interaction.followup.send(claim_lib.CLAIM_NOT_LINKED, ephemeral=True)
            return
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return

        # The second screen cannot be opened from here: a modal has to be the
        # first response to an interaction and this one has already answered.
        # So the squads are offered as a button on the result instead, which
        # also lets somebody who only knows a name stop after one screen.
        aside = ""

        note = self._note(player, existing=bool(existing))
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

    **ONE FORM, ONE BEHAVIOUR.** This carried two modes for a day and has
    neither now, and the two decisions that collapsed it are worth keeping:

    - *Whose Champion Duel is this?* was a select on the form. Kevin struck it,
      2026-08-31: *"we should not care who all it is - for all we know it could
      be theirs from a past Duel and we don't have a reason to need to know."*
      Nothing needed the answer -- **the pin derives itself**, firing only where
      `resolve_grouping_for_guild` would hand this grouping back, which is the
      only sense in which one is *yours*, and the acknowledgement reads off
      that.
    - *Your warzone has to be in the sixteen* was a refusal, kept afterwards on
      the onboarding path alone. Kevin struck that too, 2026-09-01: *"I would
      just say that their known warzone is not in the list but don't gate
      anything on it."* It is `CD_NOT_YOUR_WARZONE` now, an aside under the
      acknowledgement.

    So every entry takes the same path: the date parser, the count, the
    repeated-warzone check, the overlap conflict, joining an identical set
    somebody else already entered, and a pin that decides itself.

    **`onboarding` picks the title and nothing else.** It is named for the door
    rather than for a behaviour because it no longer has one: a modal has to
    carry the words of the button that opened it, and the two buttons differ
    (`CD_BTN_ADD_GROUPING` on the onboarding view, `CD_BTN_ADD_CD` on the hub).
    It is threaded through the retry view for that reason alone.
    """

    def __init__(
        self,
        *,
        can_write: bool,
        warzone: str | None,
        onboarding: bool = True,
        warzones_default: str | None = None,
        started_default: str | None = None,
    ):
        super().__init__(title=CD_ADD_GROUPING_TITLE if onboarding else CD_ADD_SENT_TITLE)
        self.can_write = can_write
        self.warzone = warzone
        # Carried so a refusal reopens the form the caller was actually in.
        self.onboarding = onboarding
        # The field labels are shared and stay shared: "The participating
        # warzones, all 16" describes the input whichever form is open.
        #
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
        #
        # **Only when this is the caller's own Champion Duel.** A grouping they
        # were sent has no reason to contain their warzone, and this guard is
        # what made the finished hub's offer to "record past Champion Duel
        # results" impossible to act on: the copy advertised contributing and
        # the control beside it was onboarding.
        overlaps = await asyncio.to_thread(db.overlapping_groupings, zones, started)
        exact = next((g for g, _ in overlaps if set(g["warzones"]) == set(zones)), None)
        if exact is None and overlaps:
            await self._report_conflict(interaction, overlaps[0], zones, started)
            return

        joined = exact is not None
        if joined:
            grouping = exact
        else:
            grouping = await asyncio.to_thread(
                db.create_grouping,
                zones,
                started,
                origin="member",
                guild_id=str(interaction.guild_id),
                discord_id=str(interaction.user.id),
            )

        # **On both branches, and the join is the one that matters.** A Champion
        # Duel somebody was sent has usually already been entered by the
        # alliance that plays in it, so `exact` is the common path here -- and
        # on it nothing else records that this server can now read it. Written
        # on the onboarding path too, where it is a no-op the warzone already answers:
        # one rule is cheaper to hold than a condition nobody can check.
        await asyncio.to_thread(db.note_grouping_reader, grouping["id"], str(interaction.guild_id))

        # Creating pins the guild as a side effect. They just told us their
        # sixteen; asking for the one they play on again would be asking for
        # something we already have.
        #
        # **ONLY WHERE THE HUB WILL ACTUALLY RESOLVE TO IT**, and it used to
        # pin whatever was just entered. `resolve_grouping_for_guild` answers
        # off the warzone and takes the NEWEST grouping holding it, so entering
        # an older Champion Duel of your own wrote a `confirmed_grouping_id`
        # the resolver would never return -- and `needs_warzone_confirmation`
        # compares exactly those two. The server was then thrown onto "is
        # warzone 738 yours?", a question it had already answered, every time
        # somebody recorded a past event.
        #
        # Asking the resolver rather than reproducing its rule here is what
        # stops the two drifting: whatever it would hand back is by definition
        # the grouping the confirmation is about.
        opens_on_it = False
        if self.warzone and self.warzone in zones:
            guild = str(interaction.guild_id)
            resolved, pinned = await asyncio.gather(
                asyncio.to_thread(
                    db.resolve_grouping_for_guild, guild, fallback_warzone=self.warzone
                ),
                asyncio.to_thread(db.get_guild_warzone, guild),
            )
            opens_on_it = bool(resolved and resolved["id"] == grouping["id"])
            # Still written where the server had no warzone row at all, which is
            # the Map Manager case: the warzone was inferred rather than stored,
            # and this is the moment it becomes the server's own answer.
            if opens_on_it or not pinned:
                await asyncio.to_thread(
                    db.set_guild_warzone,
                    guild,
                    self.warzone,
                    discord_id=str(interaction.user.id),
                    # Carried forward rather than cleared when this is not the
                    # grouping being opened. `set_guild_warzone` overwrites the
                    # column with whatever it is handed, so passing None here
                    # would drop a confirmation the server has already given
                    # and ask for it again on the next visit.
                    confirmed_grouping_id=(
                        grouping["id"]
                        if opens_on_it
                        else (pinned or {}).get("confirmed_grouping_id")
                    ),
                )

        # **The acknowledgement reports what happened rather than echoing what
        # was declared**, which is what let the form stop asking whose Champion
        # Duel this is. Kevin, 2026-08-31: *"we should not care who all it is -
        # for all we know it could be theirs from a past Duel and we don't have
        # a reason to need to know."*
        #
        # He is right, and the derivation is exact: `opens_on_it` is true only
        # where this is the Champion Duel the hub will now open on, which is
        # the only sense in which one is *yours*. A past event of your own and a
        # set somebody sent you are both false, correctly -- neither is the one
        # you are playing.
        # **Said, never gated.** Kevin, 2026-09-01: *"I would just say that their
        # known warzone is not in the list but don't gate anything on it."*
        #
        # This was a refusal until then, and it was right for the one flow it
        # guarded and wrong everywhere else: what it stopped was a server being
        # pinned to a Champion Duel it is not in, and the pin now works that out
        # for itself. What was left is a typo catch, and a typo catch that
        # refuses a legitimate entry costs more than it saves -- being sent a
        # Champion Duel you are not in is the thing this control is *for*.
        #
        # It fires on both branches, because joining a set somebody else entered
        # says nothing about whether your own warzone is in it.
        aside = (
            CD_NOT_YOUR_WARZONE.format(warzone=self.warzone)
            if self.warzone and self.warzone not in zones
            else ""
        )
        if joined:
            note = (
                f"ℹ️ Those Participating Warzones have already been entered.\n"
                f"The {db.GROUPING_SIZE} warzones: {_warzone_list(grouping['warzones'])}."
            )
        else:
            note = (CD_ADDED_MINE if opens_on_it else CD_ADDED_SENT).format(
                date=_short_date(started)
            ) + f"\nThe {db.GROUPING_SIZE} warzones: {_warzone_list(zones)}."
        if aside:
            note = f"{note}\n{aside}"
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
            onboarding=self.onboarding,
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
            onboarding=self.onboarding,
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
        onboarding: bool = True,
        offer_community: bool = False,
    ):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.can_write = can_write
        self.warzone = warzone
        # Carried so the retry reopens the modal the caller was actually in.
        # Without it a refusal on a Champion Duel somebody was sent hands back
        # the onboarding form, which would then refuse the same entry for not
        # containing their warzone -- a retry button that cannot succeed.
        self.onboarding = onboarding
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
                onboarding=self.onboarding,
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

#: The two shapes of nothing on the group view, and they are not the same
#: nothing. `_GROUP_NO_STAGE` is a stage the picker offers that nobody has ever
#: recorded; `_GROUP_NO_MEMBERS` is a group inside a stage we do hold, which
#: the reader picked a letter to reach.
#:
#: **Both end on the same door, and the door is a live button on the view as
#: well as a name in the sentence.** Constants since 2026-08-26: the first said
#: *round*, two tests named the phrase, and splitting a pair that is decided
#: together is what `DESIGN.md` rule 6 exists to stop.
_GROUP_NO_STAGE = (
    "We do not have anything recorded for this stage yet.\n\nYou can add it with **{record}**."
)
_GROUP_NO_MEMBERS = (
    "We do not have anyone recorded for this group.\n\n"
    "Anyone can paste the standings in with **{record}**."
)

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
        text="Stage",
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


def _alliance_counts(members: list[dict]) -> list[tuple[str, int]]:
    """Which alliances are in this group, biggest first.

    Alliance rather than warzone, which is the standing rule for every summary
    in this feature: a warzone carries several alliances and a reader belongs
    to one of them, so warzone 738 answers a question nobody in it asked.

    Players we hold no alliance tag for are counted by nobody and are reachable
    only through the unfiltered list. That is the honest treatment: a blank tag
    is a gap in the record, not a group somebody is in.
    """
    counts: dict[str, int] = {}
    for member in members:
        alliance = (member.get("alliance") or "").strip()
        if alliance:
            counts[alliance] = counts.get(alliance, 0) + 1
    return sorted(counts.items(), key=lambda pair: (-pair[1], pair[0].lower()))


def _by_alliance(members: list[dict], alliance: str | None) -> list[dict]:
    """The rows a filter leaves. No filter leaves all of them.

    Compared through `db.alliance_tag`, which is the same comparison
    `db.get_alliance_members` reads a whole Champion Duel with. Behaviour is
    unchanged from #536 -- that helper is trim-and-keep-case, which is what
    this did inline -- and routing both through it is what stops "who is in my
    alliance" meaning two things on two surfaces of one feature.
    """
    tag = db.alliance_tag(alliance)
    if not tag:
        return list(members)
    return [m for m in members if db.alliance_tag(m.get("alliance")) == tag]


def _read_group(grouping_id, stage: str, label, recorded: list[str]) -> list[dict]:
    """Everyone in one group, and nobody at all for a round we do not hold.

    **Reading must not write.** `get_or_create_group` inserts, and the round
    picker now reaches rounds nobody has recorded, so calling it on one would
    create the `groups` row that makes `recorded_stages` report that round as
    held. The round would then stop offering the contribution door it exists to
    offer, having been closed by somebody looking at it. `recorded` is what the
    caller already read, so this costs no extra query.
    """
    if stage not in recorded:
        return []
    group = db.get_or_create_group(grouping_id, stage, label)
    return db.get_group_members(group["id"])


def _opening_stage(recorded: list[str], running: str | None) -> str:
    """Which round a surface opens on.

    The round being played where we hold it, then the last round we hold, then
    the first round of all. **Never None**: the picker offers every round the
    game plays now, so there is always a round to be looking at, including for
    a Champion Duel nobody has recorded anything for.
    """
    if running in recorded:
        return running
    if recorded:
        return recorded[-1]
    return running or db.STAGES[0]


def _group_title(stage: str, label: str | None) -> str:
    """What the game calls this group, in the game's own words.

    The game writes `Semi-final Grouping: Group H` on the screen a member reads
    between the qualifiers and the semi-finals, so "Group H" is the phrase they
    arrive already holding. The knockouts have no letter at all: 32 players, one
    field, and `db.get_groups` drops them for exactly that reason.
    """
    round_name = db.STAGE_LABELS.get(stage, "This stage")
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


def _listing_footer(*, first: int, last: int, shown: int, held: int, filtered: bool):
    """Which slice of the list is on screen, and nothing else.

    Both forms signed off by Kevin, 2026-08-24, unchanged.

    Silent on the common case: a group that fits on one page with no filter on
    it has a listing that is self-evidently whole, and saying so is a line the
    reader has to read to learn nothing.

    Says nothing about which alliance either. The select above carries that,
    with the chosen option showing as its own default, and repeating it here
    would be the surface saying one thing twice while the completeness field
    says a third thing about the same list.
    """
    if not filtered and shown <= GROUP_PAGE_SIZE:
        return None
    if last - first + 1 == shown:
        text = f"Showing {_plural(shown, 'player')}."
    else:
        text = f"Showing {first} to {last} of {_plural(shown, 'player')}."
    return f"{text} Filtered from {held}." if filtered else text


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
    # The listing controls, both presentation. `members` is always the whole
    # group: completeness is measured against what the round holds, and a
    # filtered count measured against it would report a gap the alliance filter
    # invented. So the filter is applied here rather than by the caller.
    alliance: str | None = None,
    page: int = 0,
) -> discord.Embed:
    """One group, with whatever standing we hold for it.

    Deliberately renders at any size. An incomplete group still answers the
    question a member actually came with, which is who am I facing, and saying
    so is better than withholding seven names until somebody supplies the
    eighth.

    **A qualifier group is a hundred players and the fix for that is the
    filter, not the page.** All hundred are in the reader's round and almost
    none of them are anybody they know, so `alliance` narrows the list to
    people they have a reason to read and `page` catches whatever is still
    long. Three separate facts about the same list, each stated exactly once:
    the select says which alliance, the footer says which slice, and the
    completeness field says what the round holds that we do not.
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
        # The second branch was signed off by Kevin, 2026-08-24, with one word
        # moved: "Anyone can add it" became "You can add it". The invitation is
        # to the person reading it rather than to a room, and the button under
        # it is live for them.
        #
        # Two shapes of nothing, and they are not the same gap. A lettered
        # group we hold nobody for is one group inside a round we do hold, and
        # the reader got there by picking that letter. No letter and nobody is
        # the whole round missing, which is the state the picker now makes
        # reachable: it offers every round the game plays, so a member can open
        # one nobody has ever recorded and has to be told that is what they are
        # looking at rather than left to read it as a broken screen.
        #
        # Both end on the same door, and the door is a live button on the view
        # as well as a name in the sentence.
        if label is None:
            embed.description = (opener + _GROUP_NO_STAGE.format(record=_btn_words(CD_BTN_RECORD)))[
                :4096
            ]
        else:
            embed.description = (
                opener + _GROUP_NO_MEMBERS.format(record=_btn_words(CD_BTN_RECORD))
            )[:4096]
        return embed

    # Off the whole group rather than the page, so this sentence stays true
    # while the reader moves through the list. A header that rewords between
    # page two and page three is the same failure `UX.md` names for a field name
    # that moves when the data thins.
    header = (
        opener
        + {
            "results": "These are the final standings that we have recorded.",
            "seeds": "These are seed positions. No results are recorded yet.",
            "mixed": "Rows marked *(seed)* are seed positions, not results.",
        }[basis]
    )

    shown = _by_alliance(members, alliance)
    if not shown:
        # Kevin settled this on 2026-08-30. Unreachable through the view, which builds its
        # options out of the alliances actually present, and written anyway:
        # the parameter is public and a caller that passes a name nobody in the
        # group carries gets an answer rather than a blank list under a header
        # promising standings.
        embed.description = (
            f"{opener}We do not have anyone from "
            f"**{discord.utils.escape_markdown(alliance or '')}** in this group."
        )[:4096]
        return embed

    pages = max(1, -(-len(shown) // GROUP_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start = page * GROUP_PAGE_SIZE
    rows = shown[start : start + GROUP_PAGE_SIZE]

    lines = [_member_line(m, basis, stage) for m in rows]
    embed.description = f"{header}\n\n" + "\n".join(lines)[: 4096 - len(header) - 2]

    listing = _listing_footer(
        first=start + 1,
        last=start + len(rows),
        shown=len(shown),
        held=len(members),
        filtered=bool(alliance),
    )
    if listing:
        embed.set_footer(text=listing[:2048])

    # Completeness is stated, never inferred away. Eight names against a
    # 100-player qualifier group is the normal case rather than a truncation,
    # so this says what we hold against what the round holds and leaves the
    # reader to judge it.
    if expected and len(members) != expected:
        embed.add_field(
            name="Not the whole group",
            value=(
                f"We have **{_plural(len(members), 'player')}** of the "
                f"**{expected}** in this stage. Anyone can add the rest with "
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
                f"simulated stages. Run `/upgrade` to unlock it."
            ),
            inline=False,
        )
    return embed


# ── `🏅 Your standing` ────────────────────────────────────────
#
# The surface that puts a "you" in this feature. Everything else here resolves
# off the guild's warzone and a group picker and knows nothing about the
# caller; this reads their claim and opens on them.
#
# NOTHING NEW IS COMPUTED HERE, and that is a hard rule rather than a
# preference. A knockout bracket is sixty to ninety seconds of Python holding
# the GIL of the process serving every guild, and this surface is the hub's
# LANDING -- so a compute-on-open path would make that the bot's default state
# in every guild that runs `/champion_duel`. The odds come out of
# `champion_duel_store` or they do not come at all, which is the one place this
# differs from `🔮 Odds of advancing`: that surface is a press, and a press
# may pay.


# `_RANKING_BANDS`, `_band_for` and `_STANDING_IN_IT_AT` were deleted on
# 2026-08-25 with the two sentences that were their only readers. The reward
# bands themselves ARE verified -- Kevin checked them against the game before
# they went -- so they live in `notes/DESIGN_champion_duel_api.md` now, which is
# the one place the knowledge survives the code. Do not re-derive them here.


def _same_warzone(a, b) -> bool:
    """Two warzone numbers, compared the way the game means them.

    `db.parse_warzones` canonicalizes through `str(int(...))`, so a grouping
    holds `738`; `db._server` only strips whitespace and a leading `#`, so a
    registrant added from a modal can hold `0738`. Comparing the two as strings
    puts that player permanently outside their own Champion Duel.

    **The normalisation itself moved to `db.warzone_key`** and is not repeated
    here. `get_alliance_members` scopes a whole alliance by the same rule, and
    two copies of it is how one surface starts disagreeing with another about
    who is in this Champion Duel -- which is a disagreement that never
    announces itself, because the row is simply absent.
    """
    key_a, key_b = db.warzone_key(a), db.warzone_key(b)
    return bool(key_a) and key_a == key_b


def _in_this_champion_duel(player: dict, grouping: dict | None) -> bool:
    """Whether this account is in the Champion Duel the reader's guild is in."""
    if not grouping:
        return True
    return any(_same_warzone(player.get("server"), w) for w in (grouping.get("warzones") or []))


def _my_odds_row(result, members, registrant_id):
    """One player's row out of a whole group's answer, or None.

    **Matched on the row POSITION, never on the display name.** `OddsRow.key`
    is the index into `members` that produced the answer, which is what lets
    two players sharing a name stay two players -- the property `_specs` keys
    its specs by position to preserve, and the reason `key` exists at all.

    Returns None rather than raising on anything that does not line up. This is
    an accelerator on a landing surface, so a keyless row costs the personal
    line and must not cost the hub.
    """
    for row in getattr(result, "rows", None) or []:
        key = getattr(row, "key", None)
        if key is None:
            continue
        try:
            member = members[int(key)]
        except (TypeError, ValueError, IndexError):
            continue
        if member.get("id") == registrant_id:
            return row
    return None


def _read_at_line(when) -> str | None:
    """`_STANDING_READ_AT` over a stored timestamp, in the reader's own terms.

    The same `<t:N:R>` treatment `_as_of_line` gives the odds, and for the same
    reason: this feature is read across sixteen warzones in as many time zones,
    and a UTC string is a puzzle in fifteen of them.
    """
    if not when:
        return None
    try:
        stamp = datetime.fromisoformat(str(when))
    except (TypeError, ValueError):  # pragma: no cover - a hand-edited row
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return _STANDING_READ_AT.format(when=f"<t:{int(stamp.timestamp())}:R>")


def read_standing(
    user_id: int, grouping: dict | None, *, warzone=None, with_odds: bool = True
) -> dict:
    """Everything `🏅 Your standing` renders, in one blocking read.

    Returns a dict whose `state` is one of:

      unclaimed  -- nobody has told us which player this is.
      elsewhere  -- they hold a claim, on an account whose warzone is not in
                    this guild's Champion Duel.
      held       -- theirs, and in this Champion Duel.

    **`elsewhere` IS THE WARZONE-SWITCH ANSWER**, and it is worth recording why
    it is this rather than the guild-change detector the plan floated.

    A guild change was always a proxy: people join the community server, or a
    second alliance's, without moving warzone, so a detector keyed on it fires
    often and mostly wrongly -- and `registrant_claims.guild_id` is overwritten
    on every claim, so the comparison would have to happen inside the write
    path to see anything at all. This asks the question that proxy was standing
    in for, directly, off two reads that were already happening: is the account
    you claimed in the Champion Duel you are looking at? A transfer moves
    warzone and alliance together, so somebody who switched sees this the first
    time they open the hub, and somebody who merely joined another Discord
    server never does.

    It is also the quieter of the two options the plan set out, because it is
    not a detector at all. The update is permanently reachable from the surface
    the reader already opens, and this state is that route being pointed out at
    the one moment it is the answer.

    **Nothing here computes odds.** `store_lib.lookup` is a SELECT, and a
    `missing` answer stays missing.

    `warzone` is the guild's own number, carried through rather than looked up
    so `_STANDING_ELSEWHERE` can name which Champion Duel the reader is
    standing in. `_grouping_state` has already resolved it by the time either
    caller gets here, and it is the number the grouping was resolved FROM, so
    re-reading it would be a second answer to a settled question. Optional: a
    caller without one gets the sentence without its parenthetical.

    `with_odds` is False for the landing, which renders a name, a round and a
    rank and uses none of the rest. Reading it anyway cost every
    `/champion_duel` in every guild a `get_group_scouting` -- four queries over
    up to thirty-two registrants -- plus a store SELECT, a JSON parse and a
    full `fingerprint()` rebuild with the SHA-256 under it, for an answer that
    was then dropped on the floor. The press re-reads from scratch regardless.
    """
    # UNSCOPED, DELIBERATELY. Passing the guild's grouping here scopes
    # `attach_stages` to it, so a reader whose account is in a different
    # Champion Duel gets an empty `stages` and is told we have no round for
    # them -- when we have their whole round and it is simply somewhere else. A
    # claim points at one account and an account plays one grouping, so the
    # unscoped read is the account's own rounds and there is nothing to
    # disambiguate.
    claimed = db.get_claimed_registrant(user_id)
    if claimed is None:
        return {"state": "unclaimed", "player": None}

    # `elsewhere` IS NOT A PROMPT, and an earlier version of this made it one:
    # it returned early and invited the reader to move their claim. That fires
    # in the community server, and in any second alliance's server, because a
    # claim is one per Discord account and every guild resolves its own
    # Champion Duel -- which is exactly the noisy-proxy failure the guild-change
    # detector was rejected for, rebuilt one level up.
    #
    # So the standing renders either way and this only decides whether a note
    # rides along saying which Champion Duel it is about. The update is on the
    # message regardless (`_StandingClaimView`), which is the "permanently
    # reachable" half of the answer and does not depend on guessing.
    # WHICH ROUND, and the unscoped read above is exactly why this has to be
    # decided rather than taken. `attach_stages` reports the furthest round in
    # STAGES order across every grouping the account appears in, and a warzone
    # is drawn into a new grouping every event -- so a player who reached the
    # semifinals last time and is in a qualifier group now would have last
    # event's group, rank, kill score and stored odds rendered as their current
    # standing. `find_grouping_by_warzone` picking the newest is what makes the
    # older one reachable at all, and `_finished_line` invites people to
    # start the next one.
    #
    # So the caller's own Champion Duel wins when the account is in it, and the
    # account's own furthest round is the fallback for when it is not.
    stages = claimed.get("stages") or {}
    mine = [st for st, r in stages.items() if grouping and r.get("grouping_id") == grouping["id"]]
    if mine or not grouping:
        stage = mine[-1] if mine else claimed.get("stage")
        here = True
    else:
        # No round of theirs in this Champion Duel. Either they are in it and
        # nobody has recorded a round yet, which is a held standing with
        # nothing in it, or their account is somewhere else entirely.
        stage = claimed.get("stage")
        here = _in_this_champion_duel(claimed, grouping)
    row = stages.get(stage) if stage else None

    out = {
        "state": "held" if here else "elsewhere",
        "player": claimed,
        "stage": stage,
        "row": row,
        "warzone": str(warzone).strip() if warzone else None,
    }
    if (
        not with_odds
        or not row
        or not row.get("group_id")
        or stage not in odds_lib.STAGES_WITH_A_MODEL
    ):
        return out

    members = db.get_group_scouting(row["group_id"])
    out["members"] = members
    try:
        # STAMPED, because everything that reaches here is a press. `due()`
        # orders the sweeper most-recently-viewed first, and a member opening
        # their own standing is the same signal about the same group that
        # pressing `🔮 Odds of advancing` is. An earlier version pinned this
        # False to keep the LANDING from stamping, which it did -- and also
        # left a member whose group has no stored answer able to press this
        # every day and never once join the queue. The landing does not read
        # odds at all now (`with_odds`), so the flag has nothing left to
        # protect.
        out["stored"] = store_lib.lookup(row["group_id"], members, stage=stage)
    except Exception as exc:  # noqa: BLE001 - a bad store must not break the hub
        # The same degradation `_stored_odds` takes, for the same reason, and
        # it matters more here: this is the landing, so a store fault would be
        # every guild's `/champion_duel` rather than one press.
        print(f"[CHAMPION_DUEL] standing odds lookup failed for group {row['group_id']}: {exc}")
    return out


def _projected_place(result, row) -> int:
    """Where a player is projected to finish, as a position in their group.

    ON POINTS, WHICH IS WHAT THE ROUND IS RANKED ON. An earlier version counted
    off the printed advance probability and it was wrong twice over:
    `words.probability` floors a long tail into `<1%` and caps the top into
    `>99%`, so in a lopsided group of eight three players share a rung and all
    three are told they finish 1st; and even unsaturated it rounds to a whole
    percent, which is a coarser sort than the `points_mean` printed in the very
    next clause of the same sentence.

    `_printed_rank` exists for the opposite job -- ordering a table BY the
    figures it displays, so a reader cannot see two equal numbers in the wrong
    order. Nothing here displays `advance` as the basis of the finish, so there
    is no such contradiction to avoid.
    """
    rows = getattr(result, "rows", None) or []
    return sum(1 for other in rows if other.points_mean > row.points_mean) + 1


def _standing_recorded(player: dict, stage: str | None, row: dict | None) -> str:
    """The free half: what somebody read off a screen, and when.

    Rank AND kill score, because the round is scored on the score and ranked on
    it in turn. A rank with no score behind it is the conclusion without the
    number it was drawn from, and the score is the thing a player can still
    move.
    """
    bits = []
    if player.get("thp"):
        bits.append(f"Total Hero Power **{player['thp']:,.0f}**")
    if player.get("alliance"):
        bits.append(f"Alliance **{discord.utils.escape_markdown(player['alliance'])}**")
    if stage and row:
        where = f"**{db.STAGE_LABELS.get(stage, stage.title())}**"
        if row.get("grp"):
            where += f", Group **{row['grp']}**"
        bits.append(where)
        if row.get("rank"):
            bits.append(f"Rank **{row['rank']:,}**")
        if row.get("score") is not None:
            bits.append(f"Kill score **{row['score']:,}**")
    if not bits:
        return _STANDING_NOTHING_RECORDED
    # ONLY OVER A ROW THAT HOLDS A READING. `group_members.updated_at` is
    # stamped by `set_placement` on a bare membership write and on a draw, so
    # it moves when somebody records who is in a group and nothing about how
    # they are doing. "Read 3 minutes ago" over a blank rank is the surface
    # claiming a measurement nobody took.
    measured = bool(row and (row.get("rank") or row.get("score") is not None))
    read_at = _read_at_line(row.get("updated_at")) if measured else None
    return "\n".join(bits) + (f"\n\n{read_at}" if read_at else "")


def _standing_worked_out(state: dict) -> str | None:
    """The paid half, or None when there is nothing stored to say it with.

    Returning None is a real outcome rather than a failure, and the caller
    renders nothing instead of a caveat: a `missing` answer means the store
    holds nothing for this group OR holds one computed against a different set
    of people, and in a group of eight one swapped rival moves every row. An
    answer about somebody else's group is wrong rather than old.
    """
    stage, stored = state.get("stage"), state.get("stored")
    members, player = state.get("members") or [], state["player"]
    if stored is None or not stored.showable:
        return None
    result = stored.odds
    # The two rounds store different shapes and only one of them is a group.
    # Checked rather than assumed for the reason `build_odds_embed` checks it:
    # this is reachable from a public surface, and being wrong is an
    # `AttributeError` behind a deferred interaction.
    expected = odds_lib.BracketOdds if stage == "knockouts" else odds_lib.GroupOdds
    if not isinstance(result, expected):
        return None
    row = _my_odds_row(result, members, player["id"])
    if row is None:
        return None

    as_of = _as_of_line(stored.computed_at) if stored.state == "stale" else None
    if as_of is None and stored.state == "stale":
        # The caveat is the CONDITION on showing a stale answer rather than a
        # decoration over it, which is the rule `build_odds_embed` set. A
        # timestamp we cannot read costs the answer, not the line.
        return None

    if stage == "knockouts":
        ladder = "\n".join(
            f"`{words.probability(row.reach.get(rung, 0.0)):>4}`  {label}"
            for rung, label in BRACKET_RUNGS.items()
        )
        lines = [ladder]
    else:
        # THE NUMBERS AND NOTHING ELSE. A verdict sentence and a reward-band
        # sentence used to close this block; Kevin struck both on 2026-08-25
        # because they narrate the game back at somebody already playing it.
        # The three figures are the whole answer to "how do I compare to the
        # field", which is the question this surface exists for.
        place = _projected_place(result, row)
        lines = [
            f"`{words.probability(row.advance):>4}`  {_STANDING_THROUGH}",
            f"`{words.probability(row.win_group):>4}`  Winning the group outright",
            "",
            f"Projected finish **{place}** of **{len(result.rows)}**, "
            f"on **{row.points_mean:,.0f}** points.",
        ]

    return ((as_of + "\n\n") if as_of else "") + "\n".join(lines)


def build_standing_embed(state: dict, *, can_odds: bool) -> discord.Embed:
    """`🏅 Your standing`: where the reader stands, and how far they get.

    Free is what we recorded, paid is what we worked out, and the two are
    separate fields rather than one blended block so the line between them is
    visible rather than asserted.

    **The paid half renders locked and disabled rather than hidden**
    (`UX.md` principle 5). A free alliance should be able to see the shape of
    what it would be buying, and this is the one surface in Champion Duel where
    that shape is about them.

    IT TAKES NO `grouping`. It carried one briefly and never read it, which
    reads as intent that was not finished: the obvious use would be for
    `_STANDING_ELSEWHERE` to name the Champion Duel this server is in, and
    `build_player_embed` already declines to do that for a reason that holds
    here too. Every draw in a season starts on the same day, so a start date
    would print the reader's own event's name while asserting it is a different
    one, and the thing that actually separates two groupings is their list of
    Participating Warzones, which is a list rather than a label.
    """
    player = state["player"]
    alliance = f"[{player['alliance']}] " if player.get("alliance") else ""
    embed = discord.Embed(
        title=f"{CD_BTN_STANDING}"[:256],
        description=f"**{discord.utils.escape_markdown(f'{alliance}{_label(player)}')}**",
        color=discord.Color.blurple(),
    )
    if state.get("state") == "elsewhere":
        embed.description += f"\n{_elsewhere_note(player, state.get('warzone'))}"
    embed.add_field(
        name=_STANDING_RECORDED,
        value=_standing_recorded(player, state.get("stage"), state.get("row"))[:1024],
        inline=False,
    )

    if not state.get("row"):
        embed.add_field(
            name=_STANDING_WORKED_OUT,
            value=_STANDING_NO_ROUND.format(record=_btn_words(CD_BTN_RECORD))[:1024],
            inline=False,
        )
        return embed

    stage = state.get("stage")
    # THE QUALIFIERS, NAMED RATHER THAN INFERRED. This used to read
    # `stage not in STAGES_WITH_A_MODEL`, which is a different question: that
    # tuple drops `knockouts` when `KNOCKOUT_AVAILABLE` is False, so a deploy
    # whose engine pin lags told a player still in the bracket that we do not
    # model the Knockout Stage -- permanent-sounding, wrong, and followed by a
    # sentence about kill score that a 32-bracket is not ranked on.
    #
    # The qualifiers are the real case and they are a decision rather than a
    # gap: their odds came out of the bot on 2026-08-21 while recording a
    # qualifier group deliberately stayed.
    if stage == "qualifiers":
        embed.add_field(
            name=_STANDING_WORKED_OUT,
            value=_STANDING_NO_MODEL.format(round=db.STAGE_LABELS.get(stage, str(stage).title()))[
                :1024
            ],
            inline=False,
        )
        return embed

    if stage not in odds_lib.STAGES_WITH_A_MODEL:
        # A round that HAS a model, on a deploy that cannot run it. An operator
        # problem, said in the operator's words, which is what `_ENGINE_MISSING`
        # is for -- and never dressed up as a property of the round.
        embed.add_field(name=_STANDING_WORKED_OUT, value=_ENGINE_MISSING[:1024], inline=False)
        return embed

    if not can_odds:
        embed.add_field(
            name=f"🔒 {_STANDING_WORKED_OUT}",
            value=_STANDING_LOCKED[:1024],
            inline=False,
        )
        return embed

    worked_out = _standing_worked_out(state)
    if not worked_out:
        # Nothing stored, or something stored against a different set of
        # people. Said out loud rather than left as an absent field.
        embed.add_field(
            name=_STANDING_WORKED_OUT,
            value=_STANDING_NOT_WORKED_OUT[:1024],
            inline=False,
        )
    else:
        embed.add_field(name=_STANDING_WORKED_OUT, value=worked_out[:1024], inline=False)
        # The same caveat the surface these numbers came from carries, off the
        # same constant. One row of an answer is still that answer.
        embed.set_footer(
            text=(
                _BRACKET_BASIS.format(trials=state["stored"].odds.trials)
                if stage == "knockouts"
                else _ODDS_BASIS
            )
        )
    return embed


class _StandingClaimView(discord.ui.View):
    """The exit from a landing that does not know who is reading it.

    `UX.md` principle 3 -- every dead end carries its exit -- and this is the
    dead end the whole information architecture rethink started from: Kevin
    opened the hub, found eight buttons and no content, and had nowhere to go.

    It opens `champion_duel_claim.ClaimModal`, which is the half of the claim
    flow built for a caller with no row on screen. A miss inside that modal
    lands on `_MissView` and its `➕ Add a player`, which is the third of
    the plan's three states -- not in our data at all, add yourself right here.

    **IT IS ALSO WHERE THE GROUP AND THE ODDS NOW LIVE.**
    `PLAN_champion_duel_ia.md` session 6 retires `🏅 Your group` from the
    root and moves `🔮 Odds of advancing` onto this surface, and both land
    here rather than back on the hub because this is the surface the reader
    reached through themselves: the group is theirs, and the odds are about the
    group they are standing in. `_STANDING_NOT_WORKED_OUT` was a dead end until
    this -- it says we hold no projection, and the press that makes one is now
    on the same message.
    """

    def __init__(
        self,
        *,
        user_id: int,
        can_write: bool,
        grouping: dict | None = None,
        player: dict | None = None,
        standing: dict | None = None,
        can_odds: bool = False,
        warzone: str | None = None,
    ):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.can_write = can_write
        self.grouping = grouping
        # WHETHER THE BUTTON IS THERE, not what it opens on. `_open_edit_me`
        # re-reads the claim when it is pressed, so this is only the question
        # of whether there was a "my" when the message was built.
        self.player = player
        # WHICH group and WHICH round the two new presses are about. They
        # re-read everything they render; this decides only which group they
        # are about, and that cannot change without the claim moving, which
        # redraws this message.
        self.standing = standing
        # Defaults False so a caller that forgets it renders the padlock rather
        # than handing out the paid surface. `send_group_odds` re-checks the
        # entitlement on the press anyway, so this only decides how it is drawn.
        self.can_odds = can_odds
        # The guild's own number. Only a parsing prior for the record modal on
        # the group listing, never a filter.
        self.warzone = warzone
        self.message: discord.Message | None = None

        row = (standing or {}).get("row") or {}
        stage = (standing or {}).get("stage")
        # ONLY WHERE WE KNOW THE READER. This view is both the unclaimed
        # landing and the footer of a standing that has one, and on the landing
        # "your group" and "your odds" would both be promises to somebody we
        # cannot pick out of a hundred rows. The landing stays exactly what it
        # was: one button, and it is the claim.
        if player:
            # Wherever there is a model and a group to run it over. The
            # qualifiers came out of the bot on 2026-08-21 and the knockouts
            # are a bracket rather than a group, so `odds_lib` decides which
            # rounds have one -- the same dispatch the group listing defers to.
            #
            # Disabled with a padlock on the free tier rather than hidden,
            # which is `DESIGN.md`'s Premium rule and what the embed above it
            # already does with its own `Your odds` field.
            if row.get("group_id") and stage in odds_lib.STAGES_WITH_A_MODEL:
                odds = discord.ui.Button(
                    label=(CD_BTN_ODDS if can_odds else f"🔒 {CD_BTN_ODDS}")[:80],
                    style=discord.ButtonStyle.primary
                    if can_odds
                    else discord.ButtonStyle.secondary,
                    disabled=not can_odds,
                )
                odds.callback = self._on_odds
                self.add_item(odds)
            # The roster, reached through the reader rather than picked out of
            # a list. Never locked: who you are facing is a read, and every way
            # of contributing to it is free.
            group = discord.ui.Button(label=CD_BTN_GROUP[:80], style=discord.ButtonStyle.secondary)
            group.callback = self._on_group
            self.add_item(group)

        # PRIMARY ONLY ON THE LANDING. There it is the only thing to press and
        # the whole point of the message. On a standing it is the identity
        # footer under two controls about the round the reader is in, and
        # `DESIGN.md` allows one primary per view -- which the odds take, being
        # what somebody who opened their own standing came for.
        button = discord.ui.Button(
            label=CD_BTN_WHO_AM_I[:80],
            style=discord.ButtonStyle.secondary if player else discord.ButtonStyle.primary,
        )
        button.callback = self._on_press
        self.add_item(button)

        # ONLY WHERE A CLAIM IS HELD, for the same reason: on the landing there
        # is no "my" to edit.
        if player:
            edit = discord.ui.Button(
                label=(CD_BTN_EDIT_ME if can_write else f"🔒 {CD_BTN_EDIT_ME}")[:80],
                style=discord.ButtonStyle.secondary,
                disabled=not can_write,
            )
            edit.callback = self._on_edit_me
            self.add_item(edit)

    async def _their_grouping(self) -> dict | None:
        """The Champion Duel the reader's own round is in, not the guild's.

        The two are the same whenever the claimed account is in this server's
        Champion Duel, which is the ordinary case. They are not when it is
        somewhere else (`read_standing`'s `elsewhere`), and not when the only
        round we hold for the account is an older event's -- and opening the
        guild's tournament in either case would show somebody a group they are
        not in, under a heading about their own standing.
        """
        gid = ((self.standing or {}).get("row") or {}).get("grouping_id")
        if gid is None or (self.grouping and gid == self.grouping["id"]):
            return self.grouping
        return await asyncio.to_thread(db.get_grouping, gid)

    async def _on_group(self, inter: discord.Interaction):
        """Everyone in the round the reader is standing in.

        Opened on their own group rather than on a letter picked off a list,
        which is the case `PLAN_champion_duel_ia.md` says stops being
        group-first the moment you reach it through yourself. A reader we hold
        no round for gets exactly what the retired hub button gave them: the
        guild's Champion Duel on the round it is playing, with the picker and
        the door to recording one.
        """
        await inter.response.defer(ephemeral=True, thinking=True)
        row = (self.standing or {}).get("row") or {}
        grouping = await self._their_grouping()
        # THE READER'S OWN WARZONE WHERE WE FOLLOWED THEIR CLAIM OUT OF THIS
        # SERVER'S TOURNAMENT. It is only a parsing prior for the record modal
        # on that surface -- which number on a pasted line is the warzone -- and
        # this guild's number is the wrong prior for a paste out of a Champion
        # Duel this guild is not in.
        here = bool(self.grouping and grouping and grouping["id"] == self.grouping["id"])
        warzone = self.warzone if here else ((self.player or {}).get("server") or self.warzone)
        await send_group_view(
            inter,
            grouping=grouping,
            warzone=warzone,
            user_id=self.user_id,
            can_write=self.can_write,
            stage=(self.standing or {}).get("stage"),
            label=row.get("grp"),
        )

    async def _on_odds(self, inter: discord.Interaction):
        """How the reader compares to the field, which is what this screen is for.

        The embed above carries their own two figures wherever the store
        already holds an answer. This is the whole table, and where nothing is
        stored the press computes it, which is what closes the dead end
        `_STANDING_NOT_WORKED_OUT` was.
        """
        await inter.response.defer(ephemeral=True, thinking=True)
        grouping = await self._their_grouping()
        if grouping is None:
            # The grouping the row pointed at is gone. Nothing to run a model
            # over, said in the words the field above already uses.
            await inter.followup.send(_STANDING_NOT_WORKED_OUT, ephemeral=True)
            return
        row = (self.standing or {}).get("row") or {}
        await send_group_odds(
            inter,
            grouping=grouping,
            stage=(self.standing or {}).get("stage"),
            label=row.get("grp"),
        )

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.user_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    async def _on_press(self, inter: discord.Interaction):
        await inter.response.send_modal(
            claim_lib.ClaimModal(can_write=self.can_write, grouping=self.grouping)
        )

    async def _on_edit_me(self, inter: discord.Interaction):
        await _open_edit_me(inter, can_write=self.can_write, grouping=self.grouping)


def standing_opener(standing: dict | None) -> str:
    """The hub's first line, about the reader rather than about the roster.

    This is the whole point of the information architecture rethink. The hub
    opened on eight buttons and a player count, which is the feature describing
    itself; it now opens on the person, and the counts move below them.

    Returns "" for a caller that did not read a standing -- a DM, or a guild
    with no Champion Duel resolved -- so the hub falls back to exactly what it
    said before rather than to a gap where a name should be.
    """
    if not standing:
        return ""
    if standing.get("state") == "unclaimed":
        return _STANDING_UNCLAIMED

    player = standing.get("player") or {}
    alliance = f"[{player['alliance']}] " if player.get("alliance") else ""
    line = f"**{discord.utils.escape_markdown(f'{alliance}{_label(player)}')}**"
    row, stage = standing.get("row"), standing.get("stage")
    if stage and row:
        bits = [db.STAGE_LABELS.get(stage, stage.title())]
        if row.get("grp"):
            bits.append(f"Group {row['grp']}")
        if row.get("rank"):
            bits.append(f"Rank {row['rank']:,}")
        line += " \u00b7 " + " \u00b7 ".join(bits)
    # The standing renders either way and the note says which Champion Duel it
    # is about. It is not a prompt; see `read_standing`.
    return line + (
        "\n" + _elsewhere_note(player, standing.get("warzone"))
        if standing.get("state") == "elsewhere"
        else ""
    )


def _elsewhere_note(player: dict, warzone=None) -> str:
    """`_STANDING_ELSEWHERE`, filled in with THIS SERVER'S warzone.

    `display_name`, not `_label`: the label already carries a warzone as
    `(#1500)`, and that one is the player's -- printing it beside the guild's
    is two numbers in one sentence meaning opposite things.

    The warzone is passed in rather than read. `read_standing` takes the one
    the hub already resolved its grouping from, so this costs no query, and
    the superseded route is `db.get_guild_warzone`, which resolves the guild
    rather than the Champion Duel and would be a second answer to a question
    already answered upstream.

    Falls back to the sentence alone when there is no warzone to name, which
    an empty `({warzone})` would otherwise render as a hole in the copy.
    """
    line = _STANDING_ELSEWHERE.format(
        player=discord.utils.escape_markdown(str(player.get("display_name") or "?")),
    )
    number = str(warzone or "").strip()
    if number:
        line += _STANDING_ELSEWHERE_WARZONE.format(warzone=discord.utils.escape_markdown(number))
    return line + "."


# ── `🏰 Your alliance` ────────────────────────────────────────────────────────
#
# Leadership's view of their own people, and the surface that answers the one
# question the information architecture rethink found nobody had asked for.
# Kevin: *"I like to see where all of my team is and how far they could
# potentially go... it's always fun to see the potential for who will get into
# semifinals and then if anyone has a shot at going further."*
#
# **IT IS PARTLY FOR THE PLEASURE OF IT AND IT IS NOT A TABLE.** The shape is
# the rounds, furthest first, so the top of the screen is whoever got deepest
# -- which for an alliance that rarely gets more than one player through is a
# section with one name in it, and that name is the point. Flattening it into
# one sorted grid would bury that under the qualifiers.
#
# **IT READS ACROSS GROUPS AND NEVER FROM ONE.** The alliance filter that
# shipped in #536 sits inside a single group and answers "who from my alliance
# is in this group"; that is a different question and it has not answered this
# one (`PLAN_champion_duel_ia.md`, *Standing instructions*).
#
# **WHOSE ALLIANCE IT IS COMES FROM THE CLAIM.** `guild_alliance_mappings`
# carries an `alliance_name`, but only for guilds linked to Map Manager, so it
# cannot be relied on. The claiming leader's own recorded account carries the
# tag, and that is a fact somebody read off a screen.
#
# **NOTHING NEW IS COMPUTED HERE**, exactly as on `🏅 Your standing` and for a
# harder reason: an alliance spans several groups, so a compute-on-press path
# would be several seventy-second runs behind one button. The odds come out of
# `champion_duel_store` or they do not come at all.


def _current_row(player: dict) -> dict | None:
    """The round row `attach_stages` pointed this player at, or None.

    `stages` carries every round they are in and `stage` says which one the
    read is about, so this is that pair read back rather than a second decision
    about which round somebody is in.
    """
    stage = player.get("stage")
    return (player.get("stages") or {}).get(stage) if stage else None


def _alliance_odds(players: list[dict]) -> dict[int, dict]:
    """The stored answers this listing can actually show, keyed by group id.

    ONE LOOKUP PER GROUP, NOT PER PLAYER. An alliance with four players in one
    semi-final group is one `get_group_scouting` and one store SELECT, and the
    four rows are read out of the same answer -- which is also the only thing
    that guarantees the four agree with each other.

    **Rounds with no model are skipped entirely**, and that is a cost decision
    as much as a correctness one: a qualifier group is 100 players, and
    `get_group_scouting` over one is four queries and a profile read for an
    answer that round would never have. `STAGES_WITH_A_MODEL` is read rather
    than a list kept in step with it by hand.

    **WHAT COMES BACK IS SHOWABLE, and an absent key is the whole answer for
    everything else.** A `missing` row means the store holds nothing for that
    group or holds an answer computed against a different set of people, and
    the second is wrong rather than old; a `stale` one is showable only under
    its own timestamp. Deciding both here rather than in the renderer is what
    stops a row and its caveat disagreeing about which groups are showable --
    which is how a stale figure once reached the screen with nothing dating it.
    """
    wanted: dict[int, str] = {}
    for player in players:
        row, stage = _current_row(player), player.get("stage")
        if row and row.get("group_id") and stage in odds_lib.STAGES_WITH_A_MODEL:
            wanted[row["group_id"]] = stage

    out: dict[int, dict] = {}
    for group_id, stage in wanted.items():
        members = db.get_group_scouting(group_id)
        if not members:
            continue
        try:
            stored = store_lib.lookup(group_id, members, stage=stage)
        except Exception as exc:  # noqa: BLE001 - a bad store must not break the hub
            # The same degradation `read_standing` takes. A leader losing the
            # paid half of one group is a worse surface; a leader losing the
            # whole listing is a broken one.
            print(f"[CHAMPION_DUEL] alliance odds lookup failed for group {group_id}: {exc}")
            continue
        if not stored.showable:
            continue
        # A TIMESTAMP WE CANNOT READ COSTS THE ANSWER, NOT THE LINE. The caveat
        # is the CONDITION on showing a stale figure rather than a decoration
        # over it -- `build_odds_embed` set that rule and `_standing_worked_out`
        # follows it. Found by `/code-review`.
        if stored.state == "stale" and _as_of_line(stored.computed_at) is None:
            continue
        out[group_id] = {"stored": stored, "members": members, "stage": stage}
    return out


def read_alliance(
    user_id: int, grouping: dict | None, *, warzone=None, with_odds: bool = True
) -> dict:
    """Everything `🏰 Your alliance` renders, in one blocking read.

    Returns a dict whose `state` is one of:

      unclaimed  -- nobody has told us which account this is, so there is no
                    alliance to resolve.
      no_tag     -- they hold a claim, on an account carrying no alliance tag.
      elsewhere  -- their own account's warzone is not in this guild's Champion
                    Duel, so this listing is not about the event they are in.
      held       -- the tag, and every account carrying it in this Champion
                    Duel.

    `warzone` is the guild's own number, carried through rather than looked up
    so the `elsewhere` note can name which Champion Duel the reader is standing
    in. Exactly what `read_standing` takes, for exactly that reason.

    **`with_odds=False` SKIPS THE STORE ENTIRELY**, and two callers want it.
    The reads path picks players out of the roster and re-reads each group's
    scouting for itself. And a guild without the odds entitlement renders none
    of them, so reading them would be a `get_group_scouting` per group for an
    answer the embed throws away -- and worse than waste, because
    `store_lib.lookup` stamps `last_viewed_at`, which is what orders the
    sweeper. A free guild paging this listing would push its own groups to the
    front of a queue whose output it cannot be shown.
    """
    claimed = db.get_claimed_registrant(user_id)
    if claimed is None:
        return {"state": "unclaimed", "player": None, "alliance": None, "players": []}

    tag = db.alliance_tag(claimed.get("alliance"))
    if not tag:
        return {"state": "no_tag", "player": claimed, "alliance": None, "players": []}

    players = db.get_alliance_members(tag, (grouping or {}).get("id"))
    # `elsewhere` IS THE STATE AN EMPTY LISTING WOULD OTHERWISE LIE ABOUT, and
    # `/code-review` is what found it. This read is scoped by the grouping's
    # own warzones, so the reader's account is in its own result whenever it is
    # in this Champion Duel -- which makes an empty list *only* reachable when
    # the reader themselves is somewhere else. Without this branch the surface
    # answered that with "we do not hold anyone from OGV yet" over a door
    # marked `📥 Record a group`: a claim about their alliance drawn from a
    # fact about them, and the wrong thing to press either way.
    #
    # Same answer `read_standing` gives, in the same words, and it is not a
    # prompt for the same reason: the listing still renders, and the note only
    # says which Champion Duel it is about.
    out = {
        "state": "held" if _in_this_champion_duel(claimed, grouping) else "elsewhere",
        "player": claimed,
        "alliance": tag,
        "players": players,
        "grouping": grouping,
        "warzone": str(warzone).strip() if warzone else None,
        "odds": {},
    }
    if with_odds and players:
        out["odds"] = _alliance_odds(players)
    return out


def _alliance_row(player: dict, odds: dict, *, can_odds: bool) -> str:
    """One player: where they are, and how far they get from there.

    The rank in a code span and the rest as prose, which is `_member_line`'s
    shape rather than a second one -- a leader who has read a group listing on
    this feature has already learned how to scan this.

    **The paid half is the tail of the line and nothing else moves.** Where
    there are no odds to add the row is the free half and is complete on its
    own, so a leader can see who is where without paying for anything.

    **THE NUMBER IS WHICHEVER WE HOLD, AND A SEED SAYS SO.** `seed_rank` and
    `rank` are different facts about the same player, and between the draw and
    the standings every group has the first and none of the second -- which is
    the window this surface is most interesting in. Reading only `rank` printed
    `-` for a whole alliance there. Marked per row rather than in a header,
    which is `_member_line`'s rule for a mixed group and is the general case
    here: this listing spans groups and rounds, so no header could be true for
    everybody at once. Found by `/code-review`.
    """
    row = _current_row(player) or {}
    rank, seed = row.get("rank"), row.get("seed_rank")
    shown = rank if rank is not None else seed
    position = f"`{shown}`" if shown is not None else "`-`"
    name = discord.utils.escape_markdown(player.get("display_name") or "?")
    bits = [f"{position} **{name}**"]
    if rank is None and seed is not None:
        bits[0] += " *(seed)*"
    if row.get("grp"):
        bits.append(f"Group {row['grp']}")

    if not can_odds or not row or not row.get("group_id"):
        return " · ".join(bits)
    held = odds.get(row["group_id"])
    if not held or not held["stored"].showable:
        return " · ".join(bits)

    result = held["stored"].odds
    # The two rounds store different shapes and only one of them is a group.
    # Checked rather than assumed for the reason `build_odds_embed` checks it:
    # this is reachable from a public surface, and being wrong is an
    # `AttributeError` behind a deferred interaction.
    expected = odds_lib.BracketOdds if held["stage"] == "knockouts" else odds_lib.GroupOdds
    if not isinstance(result, expected):
        return " · ".join(bits)
    mine = _my_odds_row(result, held["members"], player["id"])
    if mine is None:
        return " · ".join(bits)

    if held["stage"] == "knockouts":
        # The whole ladder on one line. A bracket is where "how far could they
        # go" is a range rather than one number, and an alliance has few
        # players in a field of 32 -- so the width is affordable exactly where
        # it is worth having.
        bits.append(
            " · ".join(
                f"{label} {words.probability(mine.reach.get(rung, 0.0))}"
                for rung, label in BRACKET_RUNGS.items()
            )
        )
    else:
        bits.append(f"{words.probability(mine.advance)} through")
        bits.append(f"{words.probability(mine.win_group)} win the group")
    return " · ".join(bits)


def _alliance_as_of(odds: dict) -> str | None:
    """The oldest stale stamp among the groups on screen, or None.

    ONE LINE FOR THE WHOLE LISTING, and it is the oldest rather than the
    newest: the caveat has to be true of every figure under it, and a stamp
    taken off the freshest group would understate the age of the others.

    A `fresh` answer carries no caveat and a `missing` one is never shown, so
    only `stale` reaches this -- the rule `build_odds_embed` set and
    `_standing_worked_out` follows.
    """
    stamps = [
        held["stored"].computed_at
        for held in odds.values()
        if held["stored"].showable
        and held["stored"].state == "stale"
        and held["stored"].computed_at
    ]
    return _as_of_line(min(stamps)) if stamps else None


def _alliance_showable(odds: dict, *, can_odds: bool) -> bool:
    """Whether any figure on this listing came out of the store.

    Decides the basis footer, so the surface cannot print the basis of an
    answer it is not showing.
    """
    return bool(can_odds) and any(held["stored"].showable for held in (odds or {}).values())


#: One embed field's value, which is Discord's cap rather than a chosen one.
FIELD_LIMIT = 1024


def _add_listing(embed: discord.Embed, name: str, lines: list[str]) -> None:
    """Rows into as many fields as they need, and never into a clamp.

    **A PAGE OF TWENTY DOES NOT FIT ONE FIELD.** A field value stops at 1,024
    characters and a row carrying a rank, a name, a group and two probabilities
    runs to about ninety, so twenty of them is roughly 1,800 -- and the clamp
    every other call site in this file uses would have dropped the tail of the
    list while the footer went on counting them. That is the silent cut this
    feature refuses to make anywhere else. Found by `/code-review`.

    Continuations carry a zero-width space for a name, which is Discord's own
    way of running a field on with no second heading, and the same thing the
    stale caveat below uses.
    """
    chunk: list[str] = []
    used = 0
    for line in lines:
        if chunk and used + len(line) + 1 > FIELD_LIMIT:
            embed.add_field(name=(name or "​")[:256], value="\n".join(chunk), inline=False)
            name, chunk, used = "​", [], 0
        chunk.append(line)
        used += len(line) + 1
    if chunk:
        embed.add_field(name=(name or "​")[:256], value="\n".join(chunk)[:FIELD_LIMIT], inline=False)


def build_alliance_embed(state: dict, *, can_odds: bool, page: int = 0) -> discord.Embed:
    """`🏰 Your alliance`: where all of my people are, and how far they get.

    **One field per round, furthest round first.** The rounds are the shape of
    the answer: a leader wants to know who got deepest before they want a
    sorted list, and naming the sections off `db.STAGE_LABELS` borrows the
    game's own words rather than inventing a ladder of our own.

    NO VERDICT SENTENCE ANYWHERE, and that is a rule rather than an omission.
    Kevin struck exactly that from `🏅 Your standing` on 2026-08-25 -- *"It's
    more about seeing how I stack up against the competition in the duel
    itself"* -- and `PLAN_champion_duel_ia.md` says this session inherits it.
    The figures say how far somebody gets; a sentence about whether they are
    still in it would be narrating the game back at people playing it.

    **Paged at twenty**, the feature's fallback for any long listing, and
    applied to the flattened order rather than per round so a page is always
    twenty players rather than twenty per section.
    """
    player = state.get("player") or {}
    if state.get("state") == "unclaimed":
        return discord.Embed(
            title=CD_BTN_ALLIANCE[:256],
            description=_ALLIANCE_UNCLAIMED[:4096],
            color=discord.Color.blurple(),
        )
    if state.get("state") == "no_tag":
        return discord.Embed(
            title=CD_BTN_ALLIANCE[:256],
            description=_ALLIANCE_NO_TAG.format(
                player=discord.utils.escape_markdown(_label(player)),
            )[:4096],
            color=discord.Color.blurple(),
        )

    tag = discord.utils.escape_markdown(str(state.get("alliance") or "?"))
    glyph = CD_BTN_ALLIANCE.split(" ", 1)[0]
    embed = discord.Embed(title=f"{glyph} {tag}"[:256], color=discord.Color.blurple())

    # Which Champion Duel this listing is about, where that is not the one the
    # reader's own account is in. Not a prompt and not a guess about why: the
    # same note `🏅 Your standing` carries, off the same constant.
    elsewhere = (
        _elsewhere_note(player, state.get("warzone")) if state.get("state") == "elsewhere" else None
    )

    players = state.get("players") or []
    if not players:
        # Reachable only from `elsewhere`, since this read is scoped by the
        # grouping's own warzones and so contains the reader whenever they are
        # in it. The note leads, because "we hold nobody from OGV" on its own
        # would be a claim about their alliance drawn from a fact about them.
        body = _ALLIANCE_NOBODY.format(
            alliance=tag,
            add=_btn_words(CD_BTN_ADD),
            record=_btn_words(CD_BTN_RECORD),
        )
        embed.description = (f"{elsewhere}\n\n{body}" if elsewhere else body)[:4096]
        return embed

    started = _short_date((state.get("grouping") or {}).get("started_on"))
    opener = f"This Champion Duel started {started}. " if started else ""
    held = opener + _ALLIANCE_HELD.format(count=_plural(len(players), "account"))
    embed.description = (f"{held}\n{elsewhere}" if elsewhere else held)[:4096]

    pages = max(1, -(-len(players) // GROUP_PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    start = page * GROUP_PAGE_SIZE
    rows = players[start : start + GROUP_PAGE_SIZE]

    odds = state.get("odds") or {}
    # Grouped in the order they already arrive in. `get_alliance_members` sorts
    # furthest round first, so walking the page and cutting where the round
    # changes cannot disagree with that sort -- which a second grouping pass
    # keyed on the round could.
    for stage, group in itertools.groupby(rows, key=lambda p: p.get("stage")):
        listed = list(group)
        if stage is None:
            _add_listing(
                embed,
                _ALLIANCE_UNPLACED,
                [_alliance_row(p, odds, can_odds=False) for p in listed],
            )
            # ITS OWN FIELD, NOT A TAIL ON THE ROWS. Appended to the listing it
            # was the first thing a 1,024-character clamp cut, and it is the
            # only exit this state has. Found by `/code-review`.
            embed.add_field(
                name="​",
                value=_ALLIANCE_UNPLACED_BODY.format(record=_btn_words(CD_BTN_RECORD))[:1024],
                inline=False,
            )
            continue
        _add_listing(
            embed,
            db.STAGE_LABELS.get(stage, str(stage).title()),
            [_alliance_row(p, odds, can_odds=can_odds) for p in listed],
        )

    as_of = _alliance_as_of(odds) if can_odds else None
    if as_of:
        # In a field rather than the footer, because Discord does not format
        # `<t:N:R>` in footer text -- the same reason `_as_of_line` is used this
        # way everywhere else on this feature. The blank name is Discord's own
        # way of running a value with no heading over it.
        embed.add_field(name="​", value=as_of[:1024], inline=False)

    if not can_odds:
        embed.add_field(
            name=f"🔒 {_ALLIANCE_LOCKED_FIELD}", value=_ALLIANCE_LOCKED[:1024], inline=False
        )

    listing = _listing_footer(
        first=start + 1,
        last=start + len(rows),
        shown=len(players),
        held=len(players),
        filtered=False,
    )
    basis = _ODDS_BASIS if _alliance_showable(odds, can_odds=can_odds) else None
    footer = " ".join(text for text in (listing, basis) if text)
    if footer:
        embed.set_footer(text=footer[:2048])
    return embed


# ── The personal reads ────────────────────────────────────────────────────────
#
# **This is the half of `🏰 Your alliance` that reaches somebody.** Kevin
# produced these by hand and tagged players one at a time; doing it for the
# whole team in one action is the mechanism that turns a computed answer into
# one that arrived. The content is his -- he mocked a page showing one player,
# their opponents, and per opponent the line-up that opponent usually deploys,
# a suggested answer, the odds, and a plain-language read of how far the
# pattern can be trusted.
#
# **IT IS AN EMBED, NOT A CARD, AND THAT IS THE ACCESSIBLE SHAPE RATHER THAN A
# CHEAPER ONE.** Kevin, 2026-08-24: *"we cannot have things just on an image
# that are not also in text ... people who are visually impaired have the exact
# same experience as those who are not."* So the text version has to exist
# whatever else does. Artwork arrives as a finished asset with a layout JSON,
# the way the VS card did, and there is no such asset for this page -- and the
# picks card is the standing lesson about a design nobody has sat down over.
# When one arrives it composites what is already here.
#
# **THE MOCK'S GORILLA ROW IS NOT BUILT, AND IT IS NOT AN OVERSIGHT.** It reads
# "COMMON GORILLA PLACEMENT · Slot 2 · 2 of 3 matches", and the bot holds
# nothing that can say that. `registrant_profiles.gorilla` is a single power
# rank written only by the profile import -- not a slot in a deployed order,
# and with no denominator, so there is no "2 of 3" to print. Nothing observes
# it per match, and the collection modal leaves it out on purpose
# (`_AddPlayerModal`: it sits on the biggest squad 93% of the time and the
# engine works it out from the powers). Rendering the profile rank under the
# mock's heading would be a different fact wearing its label.


def _read_block(read, player_name: str) -> str:
    """One opponent, as the mock lays them out: odds, theirs, yours, the trust.

    The same decision tree `build_intel_embed` walks, compressed to lines
    rather than fields, and reusing its sentences rather than shortened
    rewrites of them -- those are Kevin's words and several of them exist
    precisely to stop a shorter version overclaiming.
    """
    if read.intel is None:
        slots = ", ".join(str(s) for s in read.missing)
        return _READ_NO_OPPONENT.format(slots=slots, path=_card_path(CD_BTN_SQUADS))

    result = read.intel
    worth_little = result.worth == intel_lib.WORTH_SETTLED
    lines: list[str] = []

    # THE HEADLINE FIGURE, AND WHEN THERE IS HONESTLY ONE.
    #
    # A single number is quotable in two states and only two. Either there is a
    # recommendation to price it against, or the range has collapsed to a point
    # -- which is what `worth_little` means: the power gap decides the match and
    # every line-up either of them could set gives the same answer.
    #
    # `build_intel_embed` SUPPRESSES ITS RANGE IN THAT SECOND STATE and an
    # earlier draft of this printed it, which `/code-review` caught: at a large
    # gap the envelope is "<1% to <1%", which is true, useless, and reads as a
    # broken surface. So `worth_little` takes the figure, and the range is left
    # for the state it was written for -- the choice matters and we cannot call
    # it, where floor and ceiling are genuinely far apart.
    #
    # `Envelope.mean` is never any of these. Weighting every configuration
    # equally is the wrong prior, and `champion_duel_intel` says quoting it as
    # an estimate "would be a worse claim than the one it criticises".
    floor = words.probability(result.envelope.floor)
    ceiling = words.probability(result.envelope.ceiling)
    if result.recommended is not None and (worth_little or result.choice_matters):
        lines.append(
            _READ_ODDS.format(
                odds=words.probability(result.recommended.mean),
                player=discord.utils.escape_markdown(player_name),
            )
        )
    elif floor == ceiling:
        # No recommendation, and every configuration lands on one figure
        # anyway. The range IS the number, so printing it twice with "from"
        # and "to" around it would be the same defect wearing the other shape.
        lines.append(
            _READ_ODDS.format(odds=floor, player=discord.utils.escape_markdown(player_name))
        )
    else:
        lines.append(_READ_RANGE.format(floor=floor, ceiling=ceiling))

    # What they do. Observed, never modelled.
    if result.habit:
        lines.append(f"{_READ_DEPLOYS}: {_order_text(result.habit.top)}")
    else:
        lines.append(words.NOTHING_SEEN.format(button=_btn_words(CD_BTN_ORDER)))

    # What to set against it. Every branch here is `build_intel_embed`'s, in
    # its order, so the two surfaces cannot answer the same matchup differently.
    if worth_little:
        lines.append(words.order_barely_matters(result.envelope.spread))
    elif result.needs_your_squads:
        lines.append(words.NEEDS_YOUR_SQUADS.format(path=_card_path(CD_BTN_SQUADS)))
    elif result.recommended is not None and not result.choice_matters:
        refusal = words.CANNOT_RECOMMEND_FLAT.format(measured=words.points(result.choice_spread))
        if not result.their_types_known:
            refusal = f"{refusal} {words.CANNOT_RECOMMEND_WHY}"
        lines.append(refusal)
    elif result.recommended is not None:
        lines.append(f"{_READ_ANSWER}: **{_order_text(result.recommended.order)}**")

    # How far the pattern can be trusted. `habit_line` carries both measured
    # figures -- the share and the change rate -- and `read_line` grades them,
    # under the same threshold `build_intel_embed` uses: below `LEAN_SEEN` the
    # grade would be a claim about the player that the record cannot support.
    if result.habit:
        told = words.habit_line(result.habit)
        if result.habit.total >= intel_lib.LEAN_SEEN:
            told = f"{told} {words.read_line(result.read)}"
        lines.append(told)
    return "\n".join(lines)


def build_read_embed(player: dict, reads: list, *, stage: str, label=None) -> discord.Embed:
    """One player's read against every opponent in their group.

    One field per opponent, in the order the group listing shows them, so a
    leader reading this beside the group sees the same people in the same
    order.

    **Named for the player it is about**, because the whole point of this
    surface is that it gets handed to them: a page headed with the round would
    be a page about the tournament.
    """
    name = str(player.get("display_name") or "?")
    embed = discord.Embed(
        title=_READS_TITLE.format(player=name)[:256],
        description=_READS_OPENER.format(
            group=_group_title(stage, label), count=_plural(len(reads), "opponent")
        )[:4096],
        color=discord.Color.blurple(),
    )
    for read in reads:
        embed.add_field(
            name=discord.utils.escape_markdown(str(read.them.get("display_name") or "?"))[:256],
            value=_read_block(read, name)[:1024],
            inline=False,
        )
    embed.set_footer(text=_READS_BASIS[:2048])
    return embed


def _embed_chars(embed: discord.Embed) -> int:
    """The characters Discord counts against its 6,000-per-message budget.

    Title, description, every field name and value, and the footer. Author and
    provider text count too and this feature sets neither, so they are left out
    rather than read off an object that will always answer None.
    """
    total = len(embed.title or "") + len(embed.description or "")
    total += sum(len(f.name or "") + len(f.value or "") for f in embed.fields)
    return total + len((embed.footer.text if embed.footer else "") or "")


def read_batches(embeds: list[discord.Embed]) -> list[list[discord.Embed]]:
    """The reads split into messages Discord will actually accept.

    **Two caps, and the one people know about is not the one that binds.** Ten
    embeds a message is the famous limit; the 6,000 combined characters across
    every embed on the message is the one a page of seven opponents runs into,
    at roughly 2,500 characters each.

    **NOTHING IS DROPPED.** An embed that would not fit starts the next message
    instead, and one that exceeds the whole budget on its own is sent alone --
    the reads are the deliverable, and a batching rule that quietly lost one
    would be exactly the silent cut this file refuses to make anywhere else.
    A single read cannot realistically reach 6,000 on its own: seven opponents
    at the longest block any branch produces is well under half of it, and
    `test_a_full_group_of_reads_fits_a_discord_message` is what keeps that true.
    """
    batches: list[list[discord.Embed]] = []
    current: list[discord.Embed] = []
    used = 0
    for embed in embeds:
        size = _embed_chars(embed)
        if current and (
            used + size > READS_CHAR_BUDGET or len(current) >= READS_EMBEDS_PER_MESSAGE
        ):
            batches.append(current)
            current, used = [], 0
        current.append(embed)
        used += size
    if current:
        batches.append(current)
    return batches


def team_reads(state: dict, *, limit: int = READS_PER_PRESS) -> dict:
    """A read for each of the alliance's players in a round-robin round.

    Returns `{"stage", "embeds", "cut", "shown"}`. `cut` names the players who
    did not fit, and it is names rather than a count for the reason
    `_alliance_options` states the same way: a leader who cannot see which of
    their people was left out cannot go and get them.

    **`db.ROUND_ROBIN_STAGES` decides which round this covers**, and there is
    only one. The rest of a semi-final group of eight IS somebody's opponent
    list; the qualifiers are a hundred players who do not all meet, and a
    knockout player meets exactly one person at a time, so "everyone else in the
    round" is not an opponent list there however well we know the bracket.

    **That last clause used to read "a bracket whose pairings nothing in the
    schema holds", and it is corrected rather than deleted.** The round of 32
    pairing IS derivable from `seed_rank` -- it is a fold, seed *i* against seed
    33 - i, and `_fold_partner` uses it. The conclusion is unchanged: one
    opponent is not seven, so this surface still covers the semi-finals only.

    ONE `get_group_scouting` PER GROUP, and both the player and their opponents
    come out of it. Reading a player separately would give them a squad set
    read at a different instant from the people they are being priced against.

    BLOCKING AND MEASURED. The worst case is about 450 ms of engine per player
    against a full group of seven -- unscouted pairs are a 1,296-cell grid --
    so `limit` is what stops one press from becoming several seconds of Python
    holding the GIL of the process serving every guild. Call it in a thread.
    """
    players = [
        p
        for p in (state.get("players") or [])
        if p.get("stage") in db.ROUND_ROBIN_STAGES and (_current_row(p) or {}).get("group_id")
    ]
    stage = db.ROUND_ROBIN_STAGES[0]
    if not players:
        return {"stage": stage, "embeds": [], "cut": [], "shown": 0}

    # Already sorted furthest round first and then by rank, so the cut takes
    # the best-placed rather than whoever the database happened to return.
    taken, cut = players[:limit], players[limit:]

    scouting: dict[int, list[dict]] = {}
    embeds: list[discord.Embed] = []
    for player in taken:
        row = _current_row(player) or {}
        group_id = row["group_id"]
        if group_id not in scouting:
            scouting[group_id] = db.get_group_scouting(group_id)
        members = scouting[group_id]
        # Matched on the registrant id. `get_group_scouting` sets `id` to it
        # for exactly this, and matching on a display name would put two
        # players sharing one into the same read.
        mine = next((m for m in members if m.get("id") == player["id"]), None)
        if mine is None:  # pragma: no cover - the group is where the row came from
            continue
        opponents = [m for m in members if m.get("id") != player["id"]]
        try:
            reads = intel_lib.reads_for(mine, opponents)
        except predict_lib.NotEnoughData as exc:
            embed = discord.Embed(
                title=_READS_TITLE.format(player=str(player.get("display_name") or "?"))[:256],
                description=_READS_NEEDS_THEM.format(
                    player=discord.utils.escape_markdown(str(player.get("display_name") or "?")),
                    slots=", ".join(str(s) for s in exc.missing),
                )[:4096],
                color=discord.Color.blurple(),
            )
            embeds.append(embed)
            continue
        embeds.append(build_read_embed(mine, reads, stage=stage, label=row.get("grp")))

    return {
        "stage": stage,
        "embeds": embeds,
        "cut": [str(p.get("display_name") or "?") for p in cut],
        "shown": len(embeds),
    }


class _ReadsShareView(discord.ui.View):
    """Hands the reads to the channel, which is the deliberate half.

    Private by default (`PROPOSAL_champion_duel_ia.md` principle 5): the
    leader pulls them as an ephemeral and chooses to post them. Follows
    `SharePredictionView` -- the same 📤, the same "to current channel"
    phrasing, the same disable-after-use, and the same held payload rather than
    a second render, so what gets posted is what was read.

    No `interaction_check`: the message this hangs off is ephemeral, so the
    only person who can press it is the only person who can see it.
    """

    def __init__(self, *, embeds: list[discord.Embed], user_id: int):
        super().__init__(timeout=600)
        self.embeds = embeds
        self.user_id = user_id
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    @discord.ui.button(label=CD_BTN_SHARE_READS, style=discord.ButtonStyle.secondary)
    async def share(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        button.disabled = True
        await interaction.edit_original_response(view=self)
        # Batched the same way the ephemeral was, so what lands in the channel
        # is what the leader read. The attribution rides on the first message
        # only: repeating it under every batch would say one thing four times.
        batches = read_batches(self.embeds)
        try:
            # Posted to the channel directly: a followup to an ephemeral
            # interaction would itself be ephemeral, which is the one thing
            # this button exists to avoid.
            for index, batch in enumerate(batches):
                await interaction.channel.send(
                    f"-# Shared by <@{self.user_id}>" if index == 0 else None, embeds=batch
                )
        except discord.Forbidden:
            await interaction.followup.send(
                "⚠️ I can't post in this channel. I need **Send Messages** here. "
                "You can still read these above and post them yourself.",
                ephemeral=True,
            )


class _AllianceView(discord.ui.View):
    """`🏰 Your alliance`, with the page control and the way to hand reads out.

    Re-reads on every press rather than paging a captured list. This view lives
    fifteen minutes, and a claim can move or a group can be recorded inside
    that window -- and unlike the group listing, the thing being paged here is
    resolved from the reader rather than passed in.
    """

    def __init__(
        self,
        *,
        user_id: int,
        grouping: dict | None,
        state: dict,
        can_odds: bool,
        can_intel: bool,
        can_write: bool,
        # The guild's own number, carried rather than taken off the grouping's
        # warzone list. It is only a parsing prior for the record modal -- which
        # number on a pasted line is the warzone -- and a grouping holds sixteen
        # of them, so picking one would be a guess wearing an answer's clothes.
        warzone: str | None = None,
        page: int = 0,
    ):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.grouping = grouping
        self.state = state
        self.can_odds = can_odds
        self.can_intel = can_intel
        self.can_write = can_write
        self.warzone = warzone
        self.page = page
        self.message: discord.Message | None = None
        self._build()

    def _build(self):
        """The exit that fits the state, and every state has one.

        `UX.md` principle 3. The dead ends here are different gaps and take
        different doors: nobody claimed is the claim flow, a claimed account
        with no tag is `✏️ Edit my information` opened on their own row, an
        account in a different Champion Duel is the claim again, and a tag we
        hold nobody for is `📥 Record a group`.
        """
        self.clear_items()
        state = self.state.get("state")
        if state == "unclaimed":
            # The claim flow, and it is `_StandingClaimView`'s button rather
            # than a second one: one label for one act, wherever it is offered.
            button = discord.ui.Button(
                label=CD_BTN_WHO_AM_I[:80], style=discord.ButtonStyle.primary
            )
            button.callback = self._on_who_am_i
            self.add_item(button)
            return
        if state == "no_tag":
            # `CD_BTN_EDIT_ME` RATHER THAN `➕ Add a player`, and this state is
            # the one Kevin was looking at when he asked for it: we hold their
            # account and no alliance tag, and the old exit told them to add a
            # player. Same modal, same write, opened on their own row.
            #
            # It is the exit for this state as well as the fix for the label,
            # so it is not optional here: `_ALLIANCE_NO_TAG` lost the sentence
            # that named the old control on the same day, and `UX.md` principle
            # 3 does not let this state have no door.
            edit = discord.ui.Button(
                label=(CD_BTN_EDIT_ME if self.can_write else f"🔒 {CD_BTN_EDIT_ME}")[:80],
                style=discord.ButtonStyle.primary
                if self.can_write
                else discord.ButtonStyle.secondary,
                disabled=not self.can_write,
            )
            edit.callback = self._on_edit_me
            self.add_item(edit)
            return

        players = self.state.get("players") or []
        pages = max(1, -(-len(players) // GROUP_PAGE_SIZE))
        self.page = max(0, min(self.page, pages - 1))
        row = 0
        if pages > 1:
            # Bare, and the labels are `storm_log.py`'s to the character. This
            # is the bot's pagination and a second wording of it would be a
            # second thing to learn (`notes/DESIGN.md`, emoji rule 7).
            self._pager("◀ Prev", row, self._on_prev, self.page == 0)
            self._pager(f"Page {self.page + 1} / {pages}", row, None, True)
            self._pager("Next ▶", row, self._on_next, self.page >= pages - 1)
            row += 1

        # Present only where there is somebody to read for. The round-robin
        # rule is a fact about the format rather than about entitlement, so a
        # free alliance in the semi-finals sees the padlock and a free alliance
        # in the qualifiers sees nothing -- which is the honest pair: one is
        # locked and the other does not exist yet.
        if any(p.get("stage") in db.ROUND_ROBIN_STAGES for p in players):
            reads = discord.ui.Button(
                label=(CD_BTN_READS if self.can_intel else f"🔒 {CD_BTN_READS}")[:80],
                style=discord.ButtonStyle.primary
                if self.can_intel
                else discord.ButtonStyle.secondary,
                disabled=not self.can_intel,
                row=row,
            )
            reads.callback = self._on_reads
            self.add_item(reads)

        # THE DOOR AT THE GAP, AND WHICH GAP IT IS DEPENDS ON THE READER. An
        # empty listing is only reachable when the reader's own account is in a
        # different Champion Duel, so what fixes it is moving their claim and
        # not recording somebody else's group -- and `/code-review` found the
        # surface offering the second. Recording still helps a leader who is
        # here and holds nobody, so it rides along rather than being replaced.
        if not players:
            if state == "elsewhere":
                claim = discord.ui.Button(
                    label=CD_BTN_WHO_AM_I[:80], style=discord.ButtonStyle.primary, row=row
                )
                claim.callback = self._on_who_am_i
                self.add_item(claim)
            # BOTH DOORS THE TEXT NAMES. `_ALLIANCE_NOBODY` offers adding
            # people one at a time or pasting a whole group, and only the
            # second was a control here: the first was prose pointing at a hub
            # button. Session 6 takes `➕ Add a player` off the root, so the
            # sentence would have named a control that is nowhere -- and
            # `UX.md` principle 3 wants the exit on the message rather than a
            # description of one. Secondary, because `DESIGN.md` allows one
            # primary and recording a whole group is the recommended way to
            # fill an empty listing.
            add = discord.ui.Button(
                label=(CD_BTN_ADD if self.can_write else f"🔒 {CD_BTN_ADD}")[:80],
                style=discord.ButtonStyle.secondary,
                disabled=not self.can_write,
                row=row,
            )
            add.callback = self._on_add
            self.add_item(add)
            record = discord.ui.Button(
                label=(CD_BTN_RECORD if self.can_write else f"🔒 {CD_BTN_RECORD}")[:80],
                style=discord.ButtonStyle.primary
                if self.can_write and state != "elsewhere"
                else discord.ButtonStyle.secondary,
                disabled=not self.can_write,
                row=row,
            )
            record.callback = self._on_record
            self.add_item(record)

    def _pager(self, label, row, cb, disabled):
        button = discord.ui.Button(
            label=label[:80], style=discord.ButtonStyle.secondary, row=row, disabled=disabled
        )
        if cb:
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

    async def _turn(self, inter: discord.Interaction, page: int):
        await inter.response.defer()
        self.state = await asyncio.to_thread(
            read_alliance,
            inter.user.id,
            self.grouping,
            warzone=self.warzone,
            with_odds=self.can_odds,
        )
        self.page = page
        self._build()
        await inter.edit_original_response(
            embed=build_alliance_embed(self.state, can_odds=self.can_odds, page=self.page),
            view=self,
        )

    async def _on_prev(self, inter: discord.Interaction):
        await self._turn(inter, self.page - 1)

    async def _on_next(self, inter: discord.Interaction):
        await self._turn(inter, self.page + 1)

    async def _on_who_am_i(self, inter: discord.Interaction):
        await inter.response.send_modal(
            claim_lib.ClaimModal(can_write=self.can_write, grouping=self.grouping)
        )

    async def _on_edit_me(self, inter: discord.Interaction):
        await _open_edit_me(inter, can_write=self.can_write, grouping=self.grouping)

    async def _on_add(self, inter: discord.Interaction):
        await inter.response.send_modal(_AddPlayerModal(self.can_write, grouping=self.grouping))

    async def _on_record(self, inter: discord.Interaction):
        # Read before responding, not after: a modal has to be the first
        # response to an interaction, so this cannot defer first.
        stage, groupings = await asyncio.gather(
            asyncio.to_thread(db.current_stage, (self.grouping or {}).get("id")),
            # The same list the hub root's control offers. Two record controls
            # whose Champion Duel pickers disagree is one surface contradicting
            # another, and this one is reached from further in.
            asyncio.to_thread(
                db.groupings_readable_by,
                self.warzone,
                str(inter.guild_id) if inter.guild_id else None,
            ),
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

    async def _on_reads(self, inter: discord.Interaction):
        """Every player's read, in one press, private until they post it."""
        await inter.response.defer(ephemeral=True, thinking=True)
        if not intel_lib.ENGINE_AVAILABLE:
            await inter.followup.send(_ENGINE_MISSING, ephemeral=True)
            return
        # Re-checked here rather than trusted off the button, exactly as the
        # odds and `_IntelModal` do: this view outlives the five-minute
        # entitlement cache, so a subscription that lapsed while it sat on
        # screen would otherwise come through on a button that was live when it
        # was drawn.
        if not await premium.feature_gate("champion_duel_intel", inter.guild_id, interaction=inter):
            await _send_intel_upsell(inter)
            return

        state = await asyncio.to_thread(
            read_alliance, inter.user.id, self.grouping, with_odds=False
        )
        result = await asyncio.to_thread(team_reads, state)
        if not result["embeds"]:
            await inter.followup.send(
                _READS_NOBODY.format(
                    alliance=discord.utils.escape_markdown(str(state.get("alliance") or "?")),
                    round=db.STAGE_LABELS.get(result["stage"], result["stage"]),
                ),
                ephemeral=True,
            )
            return

        # The cut is named on the message rather than left to be noticed.
        note = _READS_ROUND_ONLY.format(round=db.STAGE_LABELS.get(result["stage"], result["stage"]))
        if result["cut"]:
            note += "\n" + _READS_CUT.format(
                shown=result["shown"],
                names=", ".join(discord.utils.escape_markdown(n) for n in result["cut"]),
            )
        # SEVERAL MESSAGES, BECAUSE ONE WILL NOT HOLD THEM. Discord counts 6,000
        # characters across every embed on a message and a read is about 2,500,
        # so a team of five is three messages. The button sits on the first with
        # the note, which is where the reader starts, and it posts every batch.
        batches = read_batches(result["embeds"])
        view = _ReadsShareView(embeds=result["embeds"], user_id=inter.user.id)
        await inter.followup.send(note[:2000], embeds=batches[0], view=view, ephemeral=True)
        view.message = await inter.original_response()
        for batch in batches[1:]:
            await inter.followup.send(embeds=batch, ephemeral=True)


def build_hub_embed(
    *,
    servers: list[dict],
    can_write: bool,
    grouping: dict | None = None,
    warzone: str | None = None,
    standing: dict | None = None,
    finished: bool = False,
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
    # The calendar, or the sentence that replaces it once there is no calendar
    # left to state. `phase_line` returns "" past the last day, so a finished
    # Champion Duel would otherwise say nothing at all about being finished --
    # which is the one fact a reader between events most needs.
    calendar = _finished_line(warzone) if finished else phase_line(grouping)
    # The person, then the calendar, then the roster. `standing_opener` returns
    # "" for a caller with no standing read, which is what keeps the DM and
    # no-grouping paths on exactly the text they had before.
    who = standing_opener(standing)
    opener = "\n\n".join(bit for bit in (who, calendar) if bit)
    opener = f"{opener}\n\n" if opener else ""

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
            f"{_HUB_ROSTER_LINE.format(find=_btn_words(CD_BTN_FIND))}"
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


def _finished_line(warzone: str | None) -> str:
    """Past the last day, said where the calendar line would have been.

    **This is not a separate hub any more, and that is the point.** It used to
    be `build_finished_embed`, paired with a `ChampionDuelFinishedView` that
    carried five controls. Both were written 2026-08-15 and neither was touched
    again, so every surface built after that date -- the claim, `Your standing`,
    `Your alliance`, `Today's picks`, `Head to head`, the hub root itself --
    went into `ChampionDuelHubView` and never into its twin. A member opening
    the hub between events got the 15 August hub, and nothing decided that.

    So the finished state renders through the same embed and the same button
    grid as every other state, and this is the one line that differs. A second
    view cannot silently miss a control that a single one does not have.

    **The gap is the resting state, not a tail.** `db.is_finished` has no upper
    bound and nothing advances a server off it: it ends when somebody enters
    the next sixteen, and that draw is not visible in game for days after this
    appears. So the copy states the condition rather than an instruction nobody
    can act on yet.
    """
    whose = f"**{warzone}** is participating in" if warzone else "your alliance is in"
    return CD_FINISHED_LINE.format(whose=whose)


class _GroupView(discord.ui.View):
    """One group, plus every way of getting to a different one.

    Selects rather than a sequence of steps. A member who has been knocked out,
    or whose Champion Duel has finished, is looking backwards rather than
    forwards, and making them re-enter the flow to change one axis is the wrong
    shape for that. They are all on screen at once and any of them re-reads the
    group.

    **The round picker is the exception, and it is always here.** The others
    are present only when they have something to choose between, which is right
    for them: a Champion Duel nobody else shares and a round with one group
    recorded are both facts about the reader's own tournament. A round is not.
    The game plays three of them, so hiding the picker when we hold one made
    "no other round exists" and "the picker is missing" the same screen, which
    is what made this surface unreadable rather than merely long. It now offers
    all three, marks the ones we hold nothing for, and lets a member open one:
    what they find there is the door to recording it.

    `stages` stays what it always was, **the rounds we hold**, and drives the
    marks rather than the options.

    Two more axes, both presentation and neither touching the database:
    `alliance` narrows a hundred strangers to the people a reader knows, and
    `page` catches whatever is still long at twenty.
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
        # Defaulted where `can_odds` is required, and for the same reason that
        # one is not: forgetting this renders a padlock on a contribution door,
        # which is a worse surface but not a giveaway. Nothing sets it False
        # today -- read the padlock branch as the shape a later gate reuses,
        # exactly as the module docstring says.
        can_write: bool = True,
        # Only a parsing prior for the record modal, which uses it to decide
        # which number on a pasted line is the warzone. Not a filter.
        warzone: str | None = None,
        alliance: str | None = None,
        page: int = 0,
    ):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.can_odds = can_odds
        self.can_write = can_write
        self.groupings = groupings
        self.grouping = grouping
        self.stages = stages
        self.stage = stage
        self.groups = groups
        self.label = label
        self.members = members
        self.warzone = warzone
        self.alliance = alliance
        self.page = page
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
        # Always, and always all three. `db.STAGES` rather than `self.stages`:
        # the rounds the game plays are a fact about the game, and the rounds we
        # hold are a fact about our record. Driving the picker off the second
        # made the two indistinguishable.
        self.add_item(self._select(_PICK_STAGE, self._stage_options(), row, self._on_stage))
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

        # The filter comes before the page, and it only appears where the list
        # is long enough to need one. A semifinal group is eight players from
        # eight alliances and a filter over it costs a row to save nobody a
        # scroll; a qualifier group is a hundred and the filter is the surface.
        #
        # **A filter with no control to undo it is a trap**, and this is the one
        # place that can see both halves. Filter a hundred-player qualifier
        # group to one alliance, then move to the semi-finals: the new list is
        # eight, so the select does not render, and the filter would still be
        # narrowing it with nothing on screen to say so or turn it off. Keeping
        # the rule here rather than in `_reload` is what stops the two copies
        # disagreeing, which is exactly how that state was reachable.
        alliances = _alliance_counts(self.members)
        offer_filter = len(self.members) > GROUP_PAGE_SIZE and len(alliances) > 1
        if not offer_filter or self.alliance not in {name for name, _ in alliances}:
            self.alliance = None
        if offer_filter:
            self.add_item(
                self._select(
                    "Which alliance?", self._alliance_options(alliances), row, self._on_alliance
                )
            )
            row += 1

        shown = _by_alliance(self.members, self.alliance)
        pages = max(1, -(-len(shown) // GROUP_PAGE_SIZE))
        # Clamped here rather than by the callbacks, because the page can also
        # fall off the end when the filter changes under it.
        self.page = max(0, min(self.page, pages - 1))
        if pages > 1:
            # Bare, and the labels are `storm_log.py`'s to the character. This
            # is the bot's pagination and a second wording of it would be a
            # second thing to learn (`notes/DESIGN.md`, emoji rule 7).
            self._pager("◀ Prev", row, self._on_prev, self.page == 0)
            self._pager(f"Page {self.page + 1} / {pages}", row, None, True)
            self._pager("Next ▶", row, self._on_next, self.page >= pages - 1)

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

        # The door at the gap, and the third place this feature puts one
        # (`notes/PROPOSAL_champion_duel_ia.md`, principle 3). Naming the button
        # in the embed's prose was the whole offer until now, and prose naming a
        # control two surfaces away is a worse dead end than none: the button it
        # names is on the hub message the reader scrolled past.
        #
        # **Offered wherever the embed names it**, which is both gaps rather
        # than one: a round we hold nothing for, and a group short of what the
        # round holds. `build_group_embed` decides the second from the same
        # comparison, so the sentence and the button cannot disagree about
        # whether there is anything to add.
        #
        # Primary only on the empty round, where recording is the only thing
        # left to do. Beside a group with players in it the odds are the
        # recommended action and `notes/DESIGN.md` allows one primary per view.
        expected = db.GROUP_SIZE.get(self.stage)
        if not self.members or (expected and len(self.members) != expected):
            record = discord.ui.Button(
                label=(CD_BTN_RECORD if self.can_write else f"🔒 {CD_BTN_RECORD}")[:80],
                style=discord.ButtonStyle.primary
                if self.can_write and not self.members
                else discord.ButtonStyle.secondary,
                disabled=not self.can_write,
                row=row,
            )
            record.callback = self._on_record
            self.add_item(record)

    def _stage_options(self) -> list[discord.SelectOption]:
        """Every round the game plays, with the ones we hold nothing for marked.

        Bare labels. The three differ by which round, which is a parameter
        rather than a kind, and `notes/DESIGN.md` rule 7 sends a set like that
        bare rather than giving it three glyphs the eye cannot sort. The mark
        goes on the description line, which is text and not colour, so it
        survives rule 9 and a screen reader.
        """
        return [
            discord.SelectOption(
                label=db.STAGE_LABELS.get(stage, stage),
                value=stage,
                description=None if stage in self.stages else _STAGE_NOT_HELD,
                default=stage == self.stage,
            )
            for stage in db.STAGES
        ]

    def _alliance_options(self, alliances: list[tuple[str, int]]) -> list[discord.SelectOption]:
        """The alliances in this group, plus the way back to all of them.

        The cut line under the unfiltered option was signed off by Kevin,
        2026-08-24, unchanged: it is a count rather than a voice decision.

        The cut is stated on the unfiltered option rather than in the embed,
        which is where somebody who cannot find their own alliance is looking.
        A filter that silently drops the tail reads as "your alliance is not in
        this group", which for a two-player alliance is exactly wrong.
        """
        named = [name for name, _ in alliances[:_ALLIANCES_SHOWN]]
        # A filter set from an earlier read can name an alliance the cut has
        # since dropped. Carrying it keeps the select showing what the list is
        # actually filtered to, instead of an unfiltered-looking placeholder
        # over twelve rows of one alliance.
        if self.alliance and self.alliance not in named:
            named = [self.alliance] + named[: _ALLIANCES_SHOWN - 1]
        dropped = len({name for name, _ in alliances} - set(named))

        counts = dict(alliances)
        everyone = _plural(len(self.members), "player")
        options = [
            discord.SelectOption(
                label=_FILTER_ALL_LABEL,
                value=_FILTER_ALL,
                description=(
                    f"{everyone}. {_plural(dropped, 'smaller alliance')} not listed."
                    if dropped
                    else everyone
                )[:100],
                default=self.alliance is None,
            )
        ]
        options += [
            discord.SelectOption(
                label=name[:100],
                value=name[:100],
                description=_plural(counts.get(name, 0), "player"),
                default=name == self.alliance,
            )
            for name in named
        ]
        return options

    def _select(self, placeholder, options, row, callback):
        select = discord.ui.Select(placeholder=placeholder, options=options, row=row)
        select.callback = callback
        return select

    def _pager(self, label, row, callback, disabled):
        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            row=row,
            disabled=disabled,
        )
        if callback is not None:
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

    # ── moving between groups ────────────────────────────────────────────────

    def _embed(self) -> discord.Embed:
        """One place the embed is built, so the two ways in cannot drift."""
        return build_group_embed(
            members=self.members,
            stage=self.stage,
            label=self.label,
            grouping=self.grouping,
            can_odds=self.can_odds,
            alliance=self.alliance,
            page=self.page,
        )

    async def _reload(self, inter: discord.Interaction, *, resolve_stage: bool = False):
        """Re-read whichever group the selects now point at.

        Every axis below the one that moved is re-resolved rather than patched,
        because changing one invalidates the ones under it: a different Champion
        Duel has its own rounds, and a different round has its own letters.
        Carrying the old letter across would show a group from the wrong round
        or none at all.

        **The round is re-resolved only when the Champion Duel changed.** It
        used to be re-resolved on every reload, which was right while the picker
        only offered rounds we hold: an unheld round could not be picked, so
        snapping off one was housekeeping. It can be picked now, and that is the
        point of the change, so snapping off it would put the reader back where
        they started and read as a broken control.

        The alliance filter survives whatever `_build` will still offer a
        control for, which is decided there rather than here. The page does not
        survive anything: a slice of a list is meaningless once the list changes
        underneath it.
        """
        self.stages = await asyncio.to_thread(db.recorded_stages, self.grouping["id"])
        if resolve_stage:
            running = await asyncio.to_thread(db.current_stage, self.grouping["id"])
            self.stage = _opening_stage(self.stages, running)
        self.groups = await asyncio.to_thread(db.get_groups, self.stage, self.grouping["id"])
        labels = [str(g["group"]) for g in self.groups]
        if str(self.label) not in labels:
            self.label = labels[0] if labels else None

        self.members = await asyncio.to_thread(
            _read_group, self.grouping["id"], self.stage, self.label, self.stages
        )
        # The filter is not cleared here. `_build` drops it whenever it would
        # not also render the control that undoes it, which is a rule only that
        # method can apply, and a second copy of it here is how a filter came to
        # outlive its own select.
        self.page = 0
        await self._rerender(inter)

    async def _rerender(self, inter: discord.Interaction):
        """Redraw from what is already in hand. No query, and no round trip to
        the database for a control that only decides which rows are on screen."""
        self._build()
        await inter.edit_original_response(embed=self._embed(), view=self)

    async def _on_grouping(self, inter: discord.Interaction):
        await inter.response.defer()
        chosen = inter.data["values"][0]
        self.grouping = next((g for g in self.groupings if str(g["id"]) == chosen), self.grouping)
        await self._reload(inter, resolve_stage=True)

    async def _on_stage(self, inter: discord.Interaction):
        await inter.response.defer()
        self.stage = inter.data["values"][0]
        await self._reload(inter)

    async def _on_group(self, inter: discord.Interaction):
        await inter.response.defer()
        self.label = inter.data["values"][0]
        await self._reload(inter)

    async def _on_alliance(self, inter: discord.Interaction):
        await inter.response.defer()
        chosen = inter.data["values"][0]
        self.alliance = None if chosen == _FILTER_ALL else chosen
        self.page = 0
        await self._rerender(inter)

    async def _on_prev(self, inter: discord.Interaction):
        await inter.response.defer()
        self.page -= 1
        await self._rerender(inter)

    async def _on_next(self, inter: discord.Interaction):
        await inter.response.defer()
        self.page += 1
        await self._rerender(inter)

    async def _on_record(self, inter: discord.Interaction):
        """The empty round's way out, opened on the round they are looking at.

        Everything the modal needs is already on this view, so this is the one
        button here that reaches the database not at all. It must also stay the
        first response to its own interaction: Discord will not open a modal
        after a defer.
        """
        await inter.response.send_modal(
            _RecordGroupModal(
                can_write=self.can_write,
                grouping=self.grouping,
                stage=self.stage,
                groupings=self.groupings,
                warzone=self.warzone,
            )
        )

    # ── odds ─────────────────────────────────────────────────────────────────

    async def _on_odds(self, inter: discord.Interaction):
        """Everyone's chance of getting out of the group on screen.

        The gate is that every player has SOMETHING to place them by, which
        is a Total Hero Power or any single squad power. Neither is
        individually required. The engine fills what is missing from the shape
        fit and samples what nobody has measured.

        The entitlement, the store and the stamp are `send_group_odds`, which
        `🏅 Your standing` presses too.
        """
        await inter.response.defer(ephemeral=True, thinking=True)
        await send_group_odds(inter, grouping=self.grouping, stage=self.stage, label=self.label)


async def send_group_odds(interaction, *, grouping: dict, stage: str, label) -> None:
    """Everyone's odds for one group, behind the one gate this feature has.

    Shared by the group listing and by `🏅 Your standing`, which session 6
    moves this press onto (`PLAN_champion_duel_ia.md`). One copy, because the
    two things that are easy to get wrong here are the gate and the stamp, and
    a second implementation of either is a second chance to lose one.

    THE GATE IS RE-RESOLVED HERE, not read off whatever drew the button. Both
    views live fifteen minutes against a five minute entitlement cache, so the
    stale case that matters is a subscription that lapsed while the surface sat
    on screen, where the button is still live because it was enabled at build
    time. One cached lookup in front of a simulation that costs seconds is not
    a price worth optimising -- and it runs before `get_or_create_group`, so a
    refused press writes nothing.

    A PRESS IS A READ WHEREVER IT CAN BE. The sweeper works these out in the
    background a group at a time, so the common case is that the answer is
    already sitting in the store and this costs a SELECT. Where it is not, the
    press pays for it exactly as it always did.
    """
    if not await premium.feature_gate(
        "champion_duel_odds", interaction.guild_id, interaction=interaction
    ):
        await _send_odds_upsell(interaction)
        return
    group = await asyncio.to_thread(db.get_or_create_group, grouping["id"], stage, label)
    scouted = await asyncio.to_thread(db.get_group_scouting, group["id"])
    # The lookup is also the stamp. `last_viewed_at` is what `due()` orders
    # on, most recent first, so a press that has to fall through and
    # compute puts this group at the head of the sweeper's queue -- and the
    # next reader gets it off the table. A press is the strongest signal
    # this feature has about which of a tournament's seventeen groups
    # anybody actually cares about, and it costs one row to record.
    stored = await asyncio.to_thread(_stored_odds, group["id"], scouted, stage)
    await interaction.followup.send(
        embed=await asyncio.to_thread(
            build_odds_embed, scouted, stage, label, grouping, stored=stored
        ),
        ephemeral=True,
    )


def _stored_odds(group_id: int, members: list[dict], stage: str):
    """What the store already holds for this group, or nothing at all.

    NEVER RAISES, and that is the whole reason it exists. This surface answered
    presses for weeks before there was a store, and it still can: everything in
    here is an accelerator in front of a working path, so a table that is
    locked, half-migrated or holding a row nobody can parse must cost a member
    sixty seconds rather than the answer.

    Printed rather than reported. `champion_duel_store` degrades the same way
    on an unreadable row, and a store that is broken is broken on every press
    in every guild -- which is a thousand Sentry issues a day for one fault.
    """
    try:
        return store_lib.lookup(group_id, members, stage=stage)
    except Exception as exc:  # noqa: BLE001 - a bad store must not break a press
        print(f"[CHAMPION_DUEL] stored odds lookup failed for group {group_id}: {exc}")
        return None


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


def _as_of_line(computed_at) -> str | None:
    """`_ODDS_AS_OF`, timed the way each reader's own client will read it.

    `<t:N:R>` renders per viewer -- "3 hours ago" -- which a UTC stamp out of
    the store cannot, and this surface is read across sixteen warzones in as
    many time zones.

    THAT IS ALSO WHY THE LINE IS NOT IN THE FOOTER, where the rest of this
    surface's caveats live. Discord formats timestamp markup in a description
    and a field value and NOT in footer text, so the footer would print the
    markup at the reader.

    Returns None on anything unparseable, and the caller treats that as a
    reason to compute rather than as a caveat it can go without. A row
    hand-edited on the volume is the case that reaches this.
    """
    if not computed_at:
        return None
    try:
        when = datetime.fromisoformat(str(computed_at))
    except (TypeError, ValueError):  # pragma: no cover - a hand-edited row
        return None
    # `db._now()` writes an aware UTC string. A naive one can only come from a
    # hand edit, and reading it as UTC is the assumption that matches the
    # column rather than the machine the bot happens to be running on.
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return _ODDS_AS_OF.format(when=f"<t:{int(when.timestamp())}:R>")


def build_bracket_embed(result, grouping, *, as_of: str | None = None) -> discord.Embed:
    """How far each of the 32 gets, one ladder per player.

    `as_of` is the stale caveat when `result` came out of the store rather than
    off the engine. `build_odds_embed` decides that and hands the line down;
    nothing here works out whether an answer is old.

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
    # The stale caveat goes INSIDE the lead rather than being prepended to the
    # finished description, so the fitting loop below counts it. Prepended
    # afterwards it would push a full field back over 4,096 and Discord would
    # cut the last row mid-figure -- which is the exact failure that loop
    # exists to prevent.
    lead = (f"{as_of}\n\n" if as_of else "") + (
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
    embed.set_footer(text=_BRACKET_BASIS.format(trials=result.trials))
    return embed


def build_odds_embed(scouted, stage, label, grouping, *, stored=None) -> discord.Embed:
    """The odds, or the reason there are none.

    The model refuses a group that is not exactly eight, and refuses a player
    it has nothing to place by. Both are hard stops rather than degraded answers,
    and the copy has to say which one it hit: "add the missing players" and
    "record one squad for these two" are different jobs, and pointing at the wrong
    one is a dead end.

    Everything past THP is optional. The engine samples squads it has not been
    given, so a group nobody has scouted still gets odds, just wider ones.

    `stored` is a `champion_duel_store.Stored`, and the three states it carries
    are three different surfaces:

      fresh   -- served, with no timestamp and nothing hedged. It is bit for
                 bit what a run right now would produce.
      stale   -- served, and only ever with `_ODDS_AS_OF` over it.
      missing -- computed. NOT a weaker stale: it also means a DIFFERENT SET OF
                 PEOPLE, and in a group of eight one swapped rival moves every
                 row, so that answer is wrong rather than old.

    Omitting it computes, which is what every caller did before the store
    existed and is what the tests that predate it still do.

    A STORED REFUSAL IS DELIBERATELY NOT READ. `lookup` can hand back the
    reason a group could not be modelled, and re-deriving it costs nothing:
    both odds functions refuse before they simulate anything. Taking the stored
    string would trade the branch-specific copy below -- which names either the
    missing players or the missing powers -- for one sentence that cannot tell
    the reader which job they have.
    """
    embed = discord.Embed(
        title=f"🔮 {_group_title(stage, label)}",
        color=discord.Color.blurple(),
    )
    # Before the store is consulted, and that is the order on purpose. A bot
    # with no engine says so rather than quietly serving whatever the table
    # still holds from the last deploy that had one.
    if not odds_lib.ENGINE_AVAILABLE:
        embed.description = _ENGINE_MISSING
        return embed

    held = stored.odds if stored is not None and stored.showable else None
    # The two rounds hold different shapes, and only one of them has a `reach`
    # on every row. Nothing reachable stores the wrong one -- a group row's
    # stage does not change under it, and a field of 32 and a group of 8 are a
    # different member set anyway -- but this is a public entry point, and
    # being wrong here is an `AttributeError` behind an interaction that has
    # already been deferred. Checked rather than assumed, and it costs a fall
    # back to computing.
    if held is not None and not isinstance(
        held, odds_lib.BracketOdds if stage == "knockouts" else odds_lib.GroupOdds
    ):
        held = None
    # Tied to `held` rather than to the state alone, so the caveat can only
    # ever travel with the answer it is about. A freshly computed answer must
    # never carry one.
    as_of = None
    if held is not None and stored.state == "stale":
        as_of = _as_of_line(stored.computed_at)
        if as_of is None:
            # THE CAVEAT IS THE CONDITION ON SHOWING A STALE ANSWER, not a
            # decoration over it, so a `computed_at` we cannot read costs the
            # stored answer rather than the line. Without this the one state
            # both docstrings say must never happen -- old numbers rendered
            # exactly like current ones -- is what a bad timestamp produces.
            held = None

    try:
        # The knockouts are a bracket rather than a group, so they take the
        # other join. Dispatched here rather than inside `group_advance_odds`
        # because the two return different shapes -- a bracket has no points
        # and no "top N", so there is no row type both could fill without one
        # of them inventing a column.
        if stage == "knockouts":
            return build_bracket_embed(
                held if held is not None else odds_lib.bracket_odds(scouted),
                grouping,
                as_of=as_of,
            )
        result = held if held is not None else odds_lib.group_advance_odds(scouted, stage=stage)
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
        (f"{as_of}\n\n" if as_of else "")
        + _ODDS_OVER.format(trials=result.trials)
        + " "
        + _ODDS_COLUMNS.format(advance=result.advance)
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
    embed.set_footer(text=_ODDS_BASIS)
    return embed


async def send_group_view(
    interaction: discord.Interaction,
    *,
    grouping: dict | None,
    warzone: str | None,
    user_id: int,
    can_write: bool = True,
    stage: str | None = None,
    label=None,
) -> None:
    """Open the caller's group, with the history reachable from it.

    Starts on the Champion Duel the hub resolved and the round currently
    running, which is what somebody asking during an event means. Everything
    else is one select away, because a member who is out, or whose Champion
    Duel has finished, is looking backwards and there is no live round to show
    them.

    **`stage` and `label` open it on one group that is already known**, which
    is how `🏅 Your standing` reaches it now that session 6 has retired the hub
    button. That is the difference between this and the old front door and it
    is the whole point of the retirement: the reader arrives at their own group
    through themselves rather than picking a letter out of a list they have no
    way to place themselves in (`PLAN_champion_duel_ia.md`, *nobody goes
    group-first*). Both are hints rather than instructions -- a round or a
    letter we no longer hold falls back to what the surface would have opened
    on anyway, because a stale pointer must not strand a live view.

    **It opens on a Champion Duel we hold nothing for as well.** It used to
    refuse one, which put the flattest dead end in the feature exactly where the
    contribution was most wanted: an alliance that has just set its Participating
    Warzones holds nothing by definition, and being told so with no way to fix
    it is the state this whole surface is being rebuilt out of.
    """
    if not grouping:
        await interaction.followup.send(
            "We do not know which Champion Duel your alliance is in yet. "
            f"Set it with **{_btn_words(CD_BTN_ADD_GROUPING)}**.",
            ephemeral=True,
        )
        return

    # Every Champion Duel this server can read, which is more than the ones its
    # warzone was drawn into: a grouping somebody was sent and entered here
    # contains none of their warzones, so `groupings_for_warzone` alone would
    # leave it recorded and unreachable. See `db.groupings_readable_by`.
    groupings = await asyncio.to_thread(
        db.groupings_readable_by,
        warzone,
        str(interaction.guild_id) if interaction.guild_id else None,
    )
    if not any(g["id"] == grouping["id"] for g in groupings):
        groupings = [grouping] + list(groupings)

    stages = await asyncio.to_thread(db.recorded_stages, grouping["id"])
    running = await asyncio.to_thread(db.current_stage, grouping["id"])
    stage = stage if stage in db.STAGES else _opening_stage(stages, running)
    groups = await asyncio.to_thread(db.get_groups, stage, grouping["id"])
    labels = [str(g["group"]) for g in groups]
    # A letter we hold for this round, or the first one we do. `get_groups`
    # drops the knockouts entirely -- one field of 32 with no letter -- so both
    # sides of this land on None there, which is what the view expects.
    wanted = str(label) if label is not None else None
    label = wanted if wanted in labels else (labels[0] if labels else None)
    members = await asyncio.to_thread(_read_group, grouping["id"], stage, label, stages)

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
        can_write=can_write,
        warzone=warzone,
    )
    await interaction.followup.send(embed=view._embed(), view=view, ephemeral=True)
    view.message = await interaction.original_response()


def _pick_day(value) -> str:
    """A day to build a card for, as an ISO date. Today for anything else.

    **The surface's guard, not the storage rule.** `champion_duel_db._play_on`
    is what decides whether a day is writable and it still refuses at the door;
    this only decides what to render. The one producer of the value is the day
    select below, whose options are built from `db.server_today` and
    `db.slate_days` and are therefore already dates, so anything that does not
    parse here is a forged interaction payload rather than a state a member can
    reach. Today is the honest thing to show for one, and raising inside a
    callback would show them "Interaction failed" instead.
    """
    try:
        return datetime.fromisoformat(str(value)[:10]).date().isoformat()
    except (TypeError, ValueError):
        return db.server_today().isoformat()


def _pick_field(grouping_id, stage: str, recorded: list[str]) -> list[dict]:
    """Everyone drawn into one round of one Champion Duel, group letter and all.

    The three selects all read off this one list, and it is read once when the
    flow opens rather than per tap: choosing a warzone or a player narrows a
    list already in hand.

    **Group by group rather than `get_roster`.** `get_roster` reads every
    registrant in the database and then costs an `attach_stages` query a row;
    this costs two reads a group, which is sixteen groups at the semi-finals and
    one at the knockouts. It also hands back `seed_rank` and `rank` directly,
    which is what the knockout pairing below is derived from.

    **The letter is put on the row here.** `get_group_members` selects from
    `group_members`, which does not carry one -- the letter is a property of the
    group -- and the semi-final filter needs it per player.

    Reading must not write, so this goes through `_read_group` rather than
    calling `get_or_create_group` itself: the knockouts are the one round whose
    label is NULL, and creating that row for a round nobody has recorded would
    make `recorded_stages` report it as held.
    """
    if stage not in recorded:
        return []
    # `get_groups` drops NULL labels, which is every knockout row -- 32 players,
    # one field, no letter. So an empty answer for a round we do hold means the
    # unlettered field rather than an empty round.
    labels = [g["group"] for g in db.get_groups(stage, grouping_id)] or [None]
    field: list[dict] = []
    for label in labels:
        for member in _read_group(grouping_id, stage, label, recorded):
            member["grp"] = label
            field.append(member)
    return field


def _pick_stage(stage: str | None, recorded: list[str]) -> str:
    """Which round a picks card is built from. **The calendar does not decide.**

    Kevin, 2026-08-29, on the message this replaces: *"I think we could just
    have that as the default nothing here because maybe someone got a date
    wrong and then we're gating on that when we shouldn't."*

    **The round used to be able to shut the flow.** It is derived from the
    grouping's start date, so a start date typed a week out reported the
    qualifiers while the semi-finals were being played -- and the flow answered
    with a paragraph about the game running no prediction market on the
    qualifiers, which is true and was not the reader's problem. One mistyped
    field, and nobody in that alliance could build a card.

    **So the record decides instead of the calendar.** A round we can card is
    taken as it stands; anything else -- the qualifiers, a window before any
    round starts, a grouping with no calendar at all -- falls back to the
    furthest round we actually hold a draw for. That is the same rule
    `db.furthest_stage_held` gives `current_stage`, off `recorded`, which this
    flow has already read.

    **It cannot lock anybody out, because the miss is not a refusal.** With no
    draw for any round we card, this answers with the first of them and the
    flow lands on `no_field` -- which names the round, carries
    `CD_BTN_RECORD` as its way out, and is the same dead end any unrecorded
    round produces. That is the *default nothing* Kevin asked for.
    """
    if stage in PICK_STAGES:
        return stage
    for candidate in reversed(PICK_STAGES):
        if candidate in recorded:
            return candidate
    return PICK_STAGES[0]


def _still_in(field: list[dict], stage: str) -> list[dict]:
    """The players a meeting can still be built from.

    Only the knockouts drop anybody. A knockout `rank` is a final placement, and
    in a rigid 32-bracket that placement is the exit round, so a player carrying
    one has been knocked out and cannot be in tomorrow's meetings. Everyone in a
    semi-final group meets every other one of them over the round, so a
    semi-final `rank` says where they finished and never that they are gone.
    """
    if stage != "knockouts":
        return list(field)
    return [m for m in field if m.get("rank") is None]


def _warzone_counts(field: list[dict]) -> list[tuple[str, int]]:
    """Which warzones this round's field is drawn from, and how many from each.

    **Driven off the field, never off sixteen.** A grouping is sixteen warzones
    today and Kevin named the growth himself (2026-08-27: *"Nothing says that
    can't increase in the future"*), so this offers seventeen the day the game
    plays seventeen, with no code change.

    **And it does not narrow the way the design assumed.** Groups mix warzones:
    joining the recorded semi-final field against the LWS warzone paste gives
    per-warzone counts of 32, 9, 8, 7 and 4, so one warzone alone can overflow
    Discord's 25-option cap. Player 1 pages for that reason.

    Numeric order through `_server_sort`, which is the ordering the hub already
    lists warzones in: the reader arrives holding a number they read off the
    Predict screen and is looking it up rather than browsing.

    Normalised through `db.warzone_key`, which is the one comparison this
    feature makes for "same warzone". A registrant added through a modal can
    hold `0738` where the grouping holds `738`, and two spellings of one warzone
    would split its players across two options that each look incomplete.
    """
    counts: dict[str, int] = {}
    for member in field:
        zone = db.warzone_key(member.get("server"))
        if zone:
            counts[zone] = counts.get(zone, 0) + 1
    return sorted(counts.items(), key=lambda pair: _server_sort({"server": pair[0]}))


def _in_warzone(field: list[dict], warzone) -> list[dict]:
    """One warzone's players out of the field, by name."""
    key = db.warzone_key(warzone)
    if not key:
        return []
    rows = [m for m in field if db.warzone_key(m.get("server")) == key]
    return sorted(rows, key=lambda m: (m.get("display_name") or "").lower())


def _fold_partner(field: list[dict], player: dict, stage: str) -> dict | None:
    """Who the bracket says Player 1 meets, or None where nothing says.

    **The round of 32 is a fold. The round of 16 onwards is not.** Measured
    against `champion-duel-simulator`, `knockout_data/knockout_field.csv` and
    `knockout_reconstruction.csv` -- the round 3 capture -- and re-derived rather
    than read off that file's own header: every one of the sixteen first-round
    meetings is seed *i* against seed **33 - i**, 16 of 16, no violations and
    every name resolved. The eight second-round meetings give six distinct seed
    sums, so from there the seeds say nothing. The bracket *tree order* is drawn
    rather than seeded; only the first round's pairing follows from the
    position, which is the half `champion_duel_db.py`'s schema comment used to
    deny.

    **Derived and preselected, never enforced.** This is one event. The standing
    rule on this project is not to move on a single observation, and a hard
    validation refusing a pair outside the fold would block legitimate entry the
    day the game changes the rule. The caller offers this as the default and
    lets it be overridden, so the flow is right either way.

    **Only while the whole field is unplayed.** Once any knockout result is
    recorded the field is past its first round, the fold no longer describes it,
    and there is nothing to derive. The 32 comes from `db.GROUP_SIZE` rather
    than a literal, and so does the 33 -- the fold is *size + 1* -- so a bracket
    of a different size needs nothing changed here.
    """
    if stage != "knockouts":
        return None
    size = db.GROUP_SIZE.get("knockouts")
    alive = _still_in(field, stage)
    if not size or len(field) != size or len(alive) != size:
        return None
    seed = player.get("seed_rank")
    if seed is None:
        return None
    partner = size + 1 - seed
    return next((m for m in alive if m.get("seed_rank") == partner), None)


def _pick_opponents(field: list[dict], player: dict, stage: str) -> list[dict]:
    """Who Player 1 can meet, which is what validates the meeting as it is made.

    `set_slate`'s only membership rule is that both players exist, and its
    docstring says why: *"What actually stops an impossible pair is the entry
    flow filtering Player 2 to who Player 1 can meet."* This is that filter, and
    the rule differs by round because the format does.

    - **Semi-finals**: the seven other members of Player 1's own group. Eight
      players meet each other once over the round, so the rest of the group is
      exactly the opponent list, which is a fact about the format rather than
      about our record (`db.ROUND_ROBIN_STAGES`).
    - **Knockouts**: everyone still in. A bracket pairs by position rather than
      by group, and from the round of 16 there is nothing to derive, so the list
      is the remaining field -- 16, then 8, then 4, all inside the cap.

    Ordered with the derived partner first where there is one, then by name. At
    the round of 32 that puts the likely answer at the top of a list of 31.
    **Ordering only.** It used to be set as Player 2 as well, and Kevin took that
    out on 29 Aug -- the ordering survives because it makes no claim, and being
    wrong about it costs a scroll rather than a match nobody chose.
    """
    others = [m for m in _still_in(field, stage) if m["registrant_id"] != player["registrant_id"]]
    # A player we hold no letter for cannot be narrowed, and narrowing to the
    # other letterless rows would be worse than not narrowing: it would offer a
    # list nothing says they can meet. Fall through to the round's whole field
    # and let the maker choose, which is what every other unknown here does.
    if stage in db.ROUND_ROBIN_STAGES and player.get("grp"):
        others = [m for m in others if m.get("grp") == player.get("grp")]
    others.sort(key=lambda m: (m.get("display_name") or "").lower())
    partner = _fold_partner(field, player, stage)
    if partner is not None:
        others = [partner] + [m for m in others if m["registrant_id"] != partner["registrant_id"]]
    return others


def _pick_name(member: dict) -> str:
    """A player as one select option's label.

    Never escaped and never decorated: a select label is drawn as plain text, so
    the pipe padding and combining marks real names carry come through as they
    are. The 100 is Discord's, and a name long enough to reach it is why the
    warzone goes on the description line rather than the label.
    """
    return ((member or {}).get("display_name") or "?")[:100]


def _pick_where(member: dict) -> str:
    """Where a player is from, for the line under their name in a select.

    The warzone leads because it is what the Predict screen prints under each
    portrait, and because it is the only thing separating two players who share
    a name: names are unique per warzone and not across them.
    """
    return " · ".join(str(x) for x in (member.get("server"), member.get("alliance")) if x)[:100]


def read_picks(
    guild_id,
    grouping: dict | None,
    *,
    play_on=None,
    card_no: int = 1,
    field=None,
    field_stage: str | None = None,
) -> dict:
    """Everything the picks builder renders, in one blocking read.

    Returns a dict whose `state` is one of:

      no_grouping -- the guild's warzone is in no Champion Duel we hold, so
                     there is no field to pick from and no round to stamp.
      no_field    -- we hold no draw for the round, so there is nobody to pick
                     from. The door out is recording the group, and the surface
                     says so.
      ready       -- there is a field.

    **There is no `no_stage` any more, and that is Kevin's call on 2026-08-29.**
    The calendar could shut this flow, and a start date typed wrong shut it for
    an alliance that had done nothing wrong. `_pick_stage` carries the whole
    reasoning; the short version is that the round we hold a draw for wins over
    the round the dates say, and the miss is `no_field` rather than a refusal.

    **The round is the one the card is stamped with, and only then the one
    running.** `set_slate` stamps `_stage_for_guild` at creation and keeps it on
    every rebuild, so a card built at the semi-finals and edited during the
    knockouts is still a semi-final card. Reading the knockout field for it
    would offer players its own rows cannot contain.

    **Every one of the day's cards is read, not just this one.** `set_slate`
    refuses the same two players twice across a day in either order, so the
    select has to be able to mark a pair that is already carded elsewhere. A
    refusal that only arrives after all three taps is the thing this prevents.

    **`field` is handed back in rather than re-read where the round has not
    moved.** Reading it costs two queries a group, which is a dozen a card at
    the semi-finals, and a twenty-meeting card is twenty of those reads for a
    list that cannot have changed between two taps. `field_stage` is what the
    caller last read it for: a different round is a different field and re-reads.
    """
    if not grouping:
        return {
            "state": "no_grouping",
            "guild_id": guild_id,
            "grouping": None,
            "stage": None,
            "field": [],
            "names": {},
        }

    day = _pick_day(play_on)
    # Clamped rather than refused, and the bound is read off `db` so the two
    # cannot drift. The only producer of this is the card select, whose options
    # this module builds inside the same bound.
    card_no = max(1, min(int(card_no), db.MAX_CARDS_PER_DAY))
    cards = {n: db.get_slate(guild_id, day, card_no=n) for n in range(1, db.MAX_CARDS_PER_DAY + 1)}
    slate = cards.get(card_no)
    recorded = db.recorded_stages(grouping["id"])
    # Resolved once, and every surface downstream reads this rather than the
    # calendar: the bench's subject, the field, `_still_in`, and the card the
    # share path assembles. See `_pick_stage`.
    stage = _pick_stage((slate or {}).get("stage") or db.current_stage(grouping["id"]), recorded)

    out = {
        "state": "ready",
        "guild_id": guild_id,
        "grouping": grouping,
        "stage": stage,
        "play_on": day,
        "card_no": card_no,
        "cards": cards,
        "slate": slate,
        "days": db.slate_days(guild_id),
        "recorded": recorded,
        "field": [],
        "names": {},
    }
    out["field"] = (
        list(field)
        if field is not None and field_stage == stage
        else _pick_field(grouping["id"], stage, recorded)
    )
    names = {m["registrant_id"]: m for m in out["field"]}
    # A carded player the field does not hold: somebody moved out of the group
    # after the meeting was made. The row stays on the card either way, and one
    # read fills its name in rather than letting the surface print a blank
    # beside a meeting somebody chose.
    carded = {
        rid
        for card in cards.values()
        if card
        for meeting in card["meetings"]
        for rid in (meeting["a_id"], meeting["b_id"])
    }
    for row in db.get_scouting(sorted(carded - set(names))):
        names[row["registrant_id"]] = row
    out["names"] = names
    if not out["field"]:
        out["state"] = "no_field"
    return out


def _cards_on_day(state: dict) -> int:
    """How many cards this day actually has, off the read the flow already did.

    `read_picks` reads every one of the day's card slots so the selects can mark
    a pair that is carded elsewhere, so the total is already in hand and costs
    nothing to count. **The bench and the card both head themselves off this**,
    which is what stops one of them saying `Card 1 of 2` while the other says
    `Card 1 of 3`.

    **The highest card number, not how many there are**, and the difference is
    a day with a gap in it: emptying card 2 while 1 and 3 exist deletes it, and
    a COUNT would then head card 3 `Card 3 of 2`. That is the impossible marker
    `Slate.subject` clamps against, arriving from the other side. The highest
    number is the one that can never contradict the number beside it.

    At least 1, and at least the card being looked at. A card being built has
    not been written yet on the first meeting somebody adds, so the day can
    honestly hold zero rows while the person is plainly looking at card 1.
    """
    cards = state.get("cards") or {}
    return max(1, int(state.get("card_no") or 1), *(n for n, card in cards.items() if card))


def _uncard_a_meeting(guild_id, play_on: str, card_no: int, pair, *, actor=None) -> bool:
    """Take one meeting off a card. False if it was not on it.

    **Still a read and then a rewrite on two connections, and that window is
    open.** Its twin closed by moving into `db.add_to_slate`; the same is not
    true here, because a removal that loses a concurrent addition loses a
    meeting somebody added seconds ago, where the addition it replaced was
    losing a whole card. Worth doing and not done here, so it is written down
    rather than implied.

    The pair identifies the row rather than its position: positions shift the
    moment anybody else edits the card, so a removal keyed on one can take off
    a meeting nobody asked about.

    **An emptied card is deleted rather than written**, because `set_slate`
    refuses an empty list by design. "Nobody has built tomorrow's card yet" and
    "the card is empty" are different things to say to a reader, and only one of
    them is ever true.
    """
    stored = db.get_slate(guild_id, play_on, card_no=card_no)
    pairs = [(m["a_id"], m["b_id"]) for m in (stored or {}).get("meetings") or []]
    wanted = frozenset((int(pair[0]), int(pair[1])))
    kept = [p for p in pairs if frozenset(p) != wanted]
    if len(kept) == len(pairs):
        return False
    if kept:
        db.set_slate(guild_id, play_on, kept, card_no=card_no, actor=actor)
    else:
        db.delete_slate(guild_id, play_on, card_no=card_no)
    return True


def _meeting_line(meeting: dict, names: dict) -> str:
    """One row of the card being built: the two names, and nothing else.

    Names are escaped because they are bolded here. A player called `Rav**en`
    would otherwise appear under a different name from the one the card draws,
    which is the reason `champion_duel_picks.text_rows` escapes them too.

    **No percentage, deliberately.** This is the bench somebody assembles a card
    on, and the prediction is what the card itself says. Printing it here as
    well would make two places to read one number off, and only one of them is
    the thing that gets shared.
    """
    both = [
        discord.utils.escape_markdown(
            (names.get(meeting[side]) or {}).get("display_name") or picks_lib.CARD_UNKNOWN
        )
        for side in ("a_id", "b_id")
    ]
    return _PICKS_MEETING.format(a=both[0], b=both[1])


def build_picks_embed(state: dict, *, working=None, notice=None) -> discord.Embed:
    """The card as it stands, and what is still needed to add to it.

    **Deliberately not the card.** The image, the embed beside it and the share
    path are one surface built together; this is the bench. It says what is on
    the card rather than what the card will look like, and the two must not
    become two answers to the same question.

    `working` is the half-made match, which exists only while the three
    selects are on screen, and `notice` is the exit for a select that has
    nothing to offer -- **every dead end carries its exit** (`notes/UX.md`,
    principle 3), and a screen with a control missing and no sentence saying why
    is the flattest one this feature can produce.
    """
    if state["state"] == "no_grouping":
        return discord.Embed(
            title=picks_lib.PICKS_TITLE,
            description=_PICKS_NO_GROUPING.format(button=_btn_words(CD_BTN_ADD_GROUPING)),
            color=discord.Color.blurple(),
        )

    # Built through `Slate` rather than formatted here, so the subject on the
    # bench and the subject on the card are the same string by construction.
    slate = picks_lib.Slate(
        guild_id=str(state.get("guild_id") or ""),
        play_on=state["play_on"],
        stage=state.get("stage") or "",
        card_no=state["card_no"],
        card_total=_cards_on_day(state),
    )
    embed = discord.Embed(
        title=f"{picks_lib.PICKS_TITLE}: {slate.subject()}",
        color=discord.Color.blurple(),
    )
    if state.get("grouping"):
        embed.set_author(name=_grouping_name(state["grouping"], whose="Your"))
    if state["state"] == "no_field":
        # The clock note rides along, because the day select is drawn on this
        # state too -- `_build_card` keeps it so a reader can get back to a day
        # that does have a card. A control on screen without the sentence about
        # it is the gap this state used to have. Found by `/code-review`.
        embed.description = "\n\n".join(
            (
                _PICKS_NO_FIELD.format(
                    stage=db.STAGE_LABELS.get(state["stage"], state["stage"]),
                    button=_btn_words(CD_BTN_RECORD),
                ),
                _PICKS_DAY_CLOCK,
            )
        )
        return embed

    lines = [_PICKS_INTRO, "", _PICKS_DAY_CLOCK]
    if working:
        lines += ["", working]
    if notice:
        lines += ["", notice]
    embed.description = "\n".join(lines)

    # Through `_add_listing` rather than into one field, for the reason its own
    # docstring gives: a field value stops at 1,024 characters and a card can
    # carry twenty rows of two names each, so a clamp here would drop the tail
    # of the card while the heading went on counting them. That is the silent
    # cut this feature refuses to make anywhere else.
    # ⚠️ `_plural(n, "meeting")` here and in `build_slate_embed` is the field
    # heading a member reads -- `2 meetings` over the list. Kevin's `match` is
    # not applied to it yet: it was not a block on the sign-off page, so he was
    # never shown it. **Raise it rather than sweeping it.**
    meetings = (state.get("slate") or {}).get("meetings") or []
    if meetings:
        _add_listing(
            embed,
            _plural(len(meetings), "meeting"),
            [_meeting_line(m, state["names"]) for m in meetings],
        )
    else:
        embed.add_field(name=_plural(0, "meeting"), value=_PICKS_EMPTY, inline=False)
    if len(meetings) >= db.MAX_PICKS:
        embed.set_footer(text=_PICKS_FOOTER_CAP.format(n=db.MAX_PICKS))
    return embed


def build_slate_embed(slate) -> discord.Embed:
    """The card as text, for beside the image. Every row, and never fewer.

    **This is the half that makes the image safe to send.** Kevin, 2026-08-28:
    *"we cannot have things just on an image that are not also in text."* So
    everything the card draws is here -- its subject in the title, its rows in
    the fields, its footer in the footer -- and the one thing the card cannot
    draw, the coin-flip caveat, is here too.

    **It cannot overflow.** A card carries at most `db.MAX_PICKS` meetings, and
    the limit that binds a whole embed is Discord's 6,000 characters across
    everything on the message. Twenty rows of sixty-character names measure
    about 3,100 and twenty of a hundred characters about 4,800, so the worst
    case a roster can produce is comfortably inside it -- and the game caps a
    name at twenty. The rows go through `_add_listing` because an embed FIELD
    stops at 1,024 even though the message as a whole does not, and a clamp
    there would drop the tail of the card while the heading went on counting
    it. **Not the description's 4,096**, which is what an earlier draft of this
    docstring named: the rows are fields, so that number never applied. Found
    by `/code-review`.

    **In the card's order, by construction.** `picks_lib.assemble` returns the
    slate strongest pick first and `render_slate` re-sorts by the same key,
    which is a no-op on an already-sorted list -- so row three here is row
    three on the image without either half being told about the other. Neither
    is numbered: session A took the numerals off the card, and a numeral here
    would be counting rows that carry none.
    """
    embed = discord.Embed(
        title=f"{picks_lib.PICKS_TITLE}: {slate.subject()}",
        color=discord.Color.blurple(),
    )
    # Only where there is one. A caveat about 50% rows on a card that has none
    # would be explaining something the reader cannot see.
    if picks_lib.has_coin_flip(slate):
        embed.description = picks_lib.TEXT_COIN_FLIP
    _add_listing(
        embed,
        _plural(len(slate.picks), "meeting"),
        picks_lib.text_rows(slate),
    )
    embed.set_footer(text=picks_lib.CARD_FOOTER)
    return embed


def _slate_file(png: bytes | None, alt: str) -> dict:
    """The attachment kwargs for a card, or none where the render failed.

    A dict rather than a `File | None`, because `discord.File` cannot be sent
    twice: the object is consumed on send, so the ephemeral and the shared copy
    each need their own built from the same bytes.

    **The description is the alt text**, which is how a screen reader names the
    attachment. It points at the rows rather than repeating them -- Discord
    caps a description at 1,024 and twenty decorated names run past that --
    and the rows are in the embed on the same message either way.
    """
    if png is None:
        return {}
    return {
        "file": discord.File(
            io.BytesIO(png),
            filename="champion_duel_picks.webp",
            description=alt,
        )
    }


class _SlateShareView(discord.ui.View):
    """Hands the card to the channel, which is the deliberate half.

    Private by default (`PROPOSAL_champion_duel_ia.md` principle 5): the maker
    pulls the card as an ephemeral and chooses to post it. Follows
    `SharePredictionView` -- the same 📤, the same "to current channel"
    phrasing, the same disable-after-use, and the same held payload rather than
    a second render, so what lands in the channel is what was read. A second
    render could disagree with the first if squads were recorded in between,
    and a card that changes between being read and being shared is worse than
    the memory.

    **The image and the text go together or not at all.** They are one message
    here for the same reason they are one message on the ephemeral: an image
    posted without its rows is the thing this surface exists to refuse.

    `png` is None where the render failed. The rows are the substance and the
    embed carries all of them, so the card still posts -- as text, silently,
    because the numbers are identical either way.

    No `interaction_check`: the message this hangs off is ephemeral, so the
    only person who can press it is the only person who can see it.
    """

    def __init__(self, *, png: bytes | None, embed: discord.Embed, alt: str, user_id: int):
        super().__init__(timeout=600)
        self.png = png
        self.embed = embed
        self.alt = alt
        self.user_id = user_id
        self.message: discord.Message | None = None

    async def on_timeout(self) -> None:
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=CHAMPION_DUEL_HUB_CMD)

    @discord.ui.button(label=CD_BTN_PICKS_SHARE, style=discord.ButtonStyle.secondary)
    async def share(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        button.disabled = True
        await interaction.edit_original_response(view=self)
        try:
            # Posted to the channel directly: a followup to an ephemeral
            # interaction would itself be ephemeral, which is the one thing
            # this button exists to avoid.
            await interaction.channel.send(
                f"-# Shared by <@{self.user_id}>",
                embed=self.embed,
                **_slate_file(self.png, self.alt),
            )
        except discord.Forbidden:
            await interaction.followup.send(_SHARE_DENIED, ephemeral=True)


async def _draw_slate(slate) -> bytes | None:
    """The card as WebP, or None where it would not draw.

    A render is more moving parts than an embed -- fonts, four pieces of
    artwork, Pillow -- and none of them are worth losing a card over. **The
    fallback is silent to the reader** because the rows are identical either
    way and the embed carries every one of them; the exception still reaches
    Sentry. That is the same deal `_send_prediction` makes, and it is only
    honest here because the text half is guaranteed complete.
    """
    try:
        return await asyncio.to_thread(champion_duel_image.render_slate, slate)
    except Exception as exc:  # noqa: BLE001 - a failed render must not eat the card
        try:
            import sentry_sdk

            sentry_sdk.capture_exception(exc)
        except ImportError:  # pragma: no cover - sentry optional in some envs
            pass
        return None


class _PicksView(discord.ui.View):
    """The card, and the three selects that put a meeting on it.

    **Two modes on one message rather than two surfaces.** The card is what the
    maker is working on and the three selects are one meeting's worth of work
    inside it, so `Back` returns to the thing they were building rather than to
    a menu. Discord allows five action rows and a select takes a whole one, so
    the modes also exist because warzone, Player 1, Player 2, a pager and the
    buttons already fill the five.

    **Every add and every removal is written immediately.** A twenty-meeting
    card is sixty taps and this view times out in fifteen minutes; holding the
    work in memory until a Save button would lose a card somebody spent the
    evening on. `set_slate` rewrites the whole card in one transaction, so
    writing per meeting is not a half-written card either -- it is a shorter
    card that is true, which is what the reader would see anyway.

    **The stage is not a control.** `set_slate` stamps the round the guild's
    grouping is playing and keeps it on every rebuild, so a picker offering a
    different one would build a card out of one round's field and stamp it with
    another's. `read_picks` resolves it once, off the stored card first.
    """

    def __init__(
        self,
        *,
        user_id: int,
        guild_id,
        state: dict,
        can_write: bool = True,
    ):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.guild_id = guild_id
        self.can_write = can_write
        self.state = state
        self.message: discord.Message | None = None

        # The half-made meeting. All three are cleared together whenever the
        # axis above them moves, the same rule `_GroupView._reload` applies to
        # its own selects: a Player 1 chosen in one warzone means nothing once
        # the warzone changes, and a Player 2 means nothing without a Player 1.
        self.warzone: str | None = None
        self.p1: int | None = None
        self.p2: int | None = None
        # A page each, rather than one shared. One pager row is all the screen
        # has, so it moves whichever list the maker is working in -- but a
        # Player 1 picked off page 2 has to still be sitting in a select showing
        # page 2 afterwards. A single counter reset that select to page 1 under
        # them, which put the other people they were choosing between out of
        # reach with nothing on screen to say so.
        self.pages = dict.fromkeys(_PICK_STEPS, 0)
        self.adding = False
        self._build()

    # ── shape ────────────────────────────────────────────────────────────────

    @property
    def meetings(self) -> list[dict]:
        return ((self.state.get("slate") or {}).get("meetings")) or []

    @property
    def field(self) -> list[dict]:
        return self.state.get("field") or []

    def _member(self, registrant_id) -> dict | None:
        return (self.state.get("names") or {}).get(registrant_id)

    def _taken(self) -> dict[frozenset, int]:
        """Every pair already on one of the day's cards, and which card it is on.

        `set_slate` refuses the same two players twice across a day, in either
        order. Marking them here is what stops that refusal arriving only after
        somebody has made all three taps.
        """
        out: dict[frozenset, int] = {}
        for number, card in (self.state.get("cards") or {}).items():
            for meeting in (card or {}).get("meetings") or []:
                out[frozenset((meeting["a_id"], meeting["b_id"]))] = number
        return out

    def _build(self):
        self.clear_items()
        # Nothing to render for a guild in no Champion Duel: there is no day to
        # pick, no field to pick from, and the embed carries the control that
        # fixes it.
        if self.state["state"] == "no_grouping":
            return
        # A round this card does not cover, and a round we hold no draw for,
        # both still get the day picker. A reader can have a card for another
        # day, and this is the only way back to it -- without it, moving the day
        # onto an unrecorded round strands a live view with no controls at all.
        # Found by `/code-review`.
        if self.state["state"] != "ready":
            self.adding = False
        if self.adding:
            self._build_adding()
        else:
            self._build_card()

    def _build_card(self):
        row = 0
        self.add_item(self._select(_PICKS_PICK_DAY, self._day_options(), row, self._on_day))
        row += 1
        if self.state["state"] != "ready":
            # **THE ONE-OFF IS GONE FROM HERE**, and it was on both of this
            # view's rows until 2026-09-01. Session 6 had taken it off the hub
            # root on the reasoning that the day's card *absorbs* it, and put
            # it here so that retiring a door did not take a surface away.
            #
            # Kevin overturned that: *"Predict a match doesn't even belong
            # here. It's a single match prediction, not anything to do with
            # today's picks."* He is right. The card answers *who should I pick
            # today* out of this stage's field; the one-off answers *what
            # happens if these two meet* for any two players we hold. Same two
            # inputs, different questions, and only one of them is about today.
            #
            # It is on the hub root unconditionally now, so nothing is lost by
            # taking it off a bench it had nothing to do with.
            return
        # Only where there is something to choose between. One card is the
        # normal day and a picker over it would be a control whose every option
        # is where the reader already is.
        if self._cards_in_play() > 1:
            self.add_item(self._select(_PICKS_PICK_CARD, self._card_options(), row, self._on_card))
            row += 1
        # A write, so it renders with the write gate rather than beside it. A
        # select cannot be drawn disabled the way a button can, so the only
        # honest treatment of a reader who may not write is not to offer it.
        if self.meetings and self.can_write:
            self.add_item(
                self._select(_PICKS_PICK_REMOVE, self._remove_options(), row, self._on_remove)
            )
            row += 1
        self._add(
            CD_BTN_PICKS_ADD,
            discord.ButtonStyle.primary if self.can_write else discord.ButtonStyle.secondary,
            row,
            self._on_add,
            disabled=not self.can_write,
        )
        # A read, so no write gate on it: anybody who can open the bench can
        # look at the card. Only where there is a card -- `render_slate`
        # refuses an empty one, and a button that always fails is worse than a
        # button that is not there.
        if self.meetings:
            self._add(CD_BTN_PICKS_SHOW, discord.ButtonStyle.secondary, row, self._on_show)
        if self.meetings:
            self._add(
                CD_BTN_PICKS_DELETE,
                discord.ButtonStyle.danger,
                row,
                self._on_delete,
                disabled=not self.can_write,
            )

    def _step(self) -> str:
        """Which of the three taps is being made, which is what the pager moves.

        One pager row is all the screen has once three selects and the buttons
        are on it, so it points at the list the maker is working in, and the page
        it shows is named in that select's own placeholder. Only one select ever
        shows a page at a time, so the two arrows are never ambiguous about what
        they move.
        """
        if not self.warzone:
            return "warzone"
        return "player" if self.p1 is None else "opponent"

    def _build_adding(self):
        # Everyone still in, and the warzone counts taken off that rather than
        # off the whole field. A warzone whose players have all been knocked out
        # is not one to offer, and a count including them would promise players
        # the next select cannot show.
        alive = _still_in(self.field, self.state["stage"])
        zones = _warzone_counts(alive)
        players = _in_warzone(alive, self.warzone) if self.warzone else []
        opponents = self._opponents()

        step = self._step()
        pages = max(
            1,
            -(
                -len({"warzone": zones, "player": players, "opponent": opponents}[step])
                // _PICK_OPTIONS
            ),
        )
        self.pages[step] = max(0, min(self.pages[step], pages - 1))

        row = 0
        # No warzones is a round with nobody left in it, which the knockouts
        # reach once the last meeting has been played. There is nothing to pick,
        # the embed says so, and the only control is the way back.
        if zones:
            self.add_item(
                self._select(
                    self._placeholder("warzone", _PICKS_PICK_WARZONE, step, pages),
                    self._warzone_options(zones),
                    row,
                    self._on_warzone,
                )
            )
            row += 1
        if self.warzone and players:
            self.add_item(
                self._select(
                    self._placeholder("player", _PICKS_PICK_P1, step, pages),
                    self._player_options(players),
                    row,
                    self._on_p1,
                )
            )
            row += 1
        # An empty opponent list is a group we hold one player of. The embed
        # carries the door out rather than this offering an empty control.
        if self.p1 is not None and opponents:
            self.add_item(
                self._select(
                    self._placeholder("opponent", _PICKS_PICK_P2, step, pages),
                    self._opponent_options(opponents),
                    row,
                    self._on_p2,
                )
            )
            row += 1
        if pages > 1:
            # `storm_log.py`'s labels to the character. This is the bot's
            # pagination and a second wording of it would be a second thing to
            # learn (`notes/DESIGN.md`, emoji rule 7).
            self._pager("◀ Prev", row, self._on_prev, self.pages[step] == 0)
            self._pager(f"Page {self.pages[step] + 1} / {pages}", row, None, True)
            self._pager("Next ▶", row, self._on_next, self.pages[step] >= pages - 1)
            row += 1
        self._add(
            CD_BTN_PICKS_SAVE,
            discord.ButtonStyle.success,
            row,
            self._on_save,
            disabled=not (self.can_write and self.p1 is not None and self.p2 is not None),
        )
        # The way back out of a half-made meeting, and it is a button rather
        # than re-picking the warzone already selected: a client with nothing
        # new to send sends nothing at all, so a select somebody re-taps the
        # same value on may never reach us.
        self._add(CD_BTN_PICKS_RESTART, discord.ButtonStyle.secondary, row, self._on_restart)
        self._add(CD_BTN_PICKS_BACK, discord.ButtonStyle.secondary, row, self._on_back)

    def _placeholder(self, mine: str, label: str, step: str, pages: int) -> str:
        """A select's placeholder, saying which page it shows while it pages."""
        if pages > 1 and mine == step:
            return _PICKS_PAGED.format(what=label, page=self.pages[mine] + 1, pages=pages)[:150]
        return label[:150]

    def _slice(self, rows: list, step: str) -> list:
        first = self.pages[step] * _PICK_OPTIONS
        return rows[first : first + _PICK_OPTIONS]

    # ── options ──────────────────────────────────────────────────────────────

    def _days(self) -> list[str]:
        """The days offered, in the order somebody building a card wants them.

        Today first, then the days a card can be built for ahead of it, then
        whatever days already carry cards. A slate is prepared *"the
        day/evening/morning before"* (Kevin, 2026-08-27), so tomorrow has to be
        one tap away, and a day already carrying a card has to be reachable to
        be corrected.
        """
        today = db.server_today()
        days = [(today + timedelta(days=n)).isoformat() for n in range(_PICK_DAYS_AHEAD + 1)]
        for row in self.state.get("days") or []:
            if row["play_on"] not in days:
                days.append(row["play_on"])
        if self.state["play_on"] not in days:
            days.append(self.state["play_on"])
        return days[:_PICK_OPTIONS]

    def _day_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.state.get("days") or []:
            counts[row["play_on"]] = counts.get(row["play_on"], 0) + (row["meetings"] or 0)
        return counts

    def _day_options(self) -> list[discord.SelectOption]:
        counts = self._day_counts()
        today = db.server_today().isoformat()
        options = []
        for day in self._days():
            # Built through `Slate.date_label` rather than formatted here, so
            # the day on the picker and the day on the card head are the same
            # string by construction.
            label = picks_lib.Slate(guild_id="", play_on=day).date_label()
            if day == today:
                label = _PICKS_TODAY.format(day=label)
            held = counts.get(day, 0)
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=day,
                    description=(
                        _PICKS_CARD_COUNT.format(n=_plural(held, "meeting"))
                        if held
                        else _PICKS_CARD_EMPTY
                    )[:100],
                    default=day == self.state["play_on"],
                )
            )
        return options

    def _cards_in_play(self) -> int:
        """How many of the day's cards exist, counting the one being looked at.

        The picker appears at two, which is a day that overflowed twenty
        meetings. Below that there is nothing to move between.
        """
        held = {n for n, card in (self.state.get("cards") or {}).items() if card}
        held.add(self.state["card_no"])
        return len(held)

    def _card_options(self) -> list[discord.SelectOption]:
        cards = self.state.get("cards") or {}
        # The same total the card heads itself with, and read the same way, so
        # an option in this picker and the card it opens are named the same
        # thing. **Not `_cards_in_play`**, which counts cards rather than
        # numbering them: that decides whether this select is drawn at all,
        # where one card is nothing to move between, and a count read as a
        # total labels card 3 of a gapped day `Card 3 of 2`. Found by
        # `/code-review`.
        total = _cards_on_day(self.state)
        options = []
        for number in range(1, db.MAX_CARDS_PER_DAY + 1):
            held = len((cards.get(number) or {}).get("meetings") or [])
            if not held and number != self.state["card_no"]:
                continue
            options.append(
                discord.SelectOption(
                    label=picks_lib.CARD_NUMBER.format(n=number, total=total)[:100],
                    value=str(number),
                    description=(_plural(held, "meeting") if held else _PICKS_CARD_EMPTY)[:100],
                    default=number == self.state["card_no"],
                )
            )
        return options

    def _remove_options(self) -> list[discord.SelectOption]:
        options = []
        for meeting in self.meetings[:_PICK_OPTIONS]:
            a = (self._member(meeting["a_id"]) or {}).get("display_name") or picks_lib.CARD_UNKNOWN
            b = (self._member(meeting["b_id"]) or {}).get("display_name") or picks_lib.CARD_UNKNOWN
            options.append(
                discord.SelectOption(
                    label=_PICKS_MEETING_OPTION.format(a=a, b=b)[:100],
                    # The two players rather than the row's place on the card.
                    # Positions shift the moment anybody else edits it, and a
                    # removal keyed on one takes off whatever moved into that
                    # slot instead.
                    value=f"{meeting['a_id']}:{meeting['b_id']}",
                )
            )
        return options

    def _warzone_options(self, zones) -> list[discord.SelectOption]:
        shown = self._slice(zones, "warzone")
        # The chosen warzone is carried onto whatever window is showing, so the
        # select never reads as unset while a warzone is filtering the two lists
        # under it. Same reason `_alliance_options` carries its own selection.
        chosen = db.warzone_key(self.warzone)
        if chosen and chosen not in {zone for zone, _ in shown}:
            shown = [(chosen, 0)] + list(shown)[: _PICK_OPTIONS - 1]
        return [
            discord.SelectOption(
                label=str(zone)[:100],
                value=str(zone),
                description=_plural(count, "player")[:100] if count else None,
                default=zone == db.warzone_key(self.warzone),
            )
            for zone, count in shown
        ]

    def _player_options(self, players) -> list[discord.SelectOption]:
        chosen = self.p1
        shown = self._slice(players, "player")
        if chosen is not None and chosen not in {m["registrant_id"] for m in shown}:
            picked = self._member(chosen)
            if picked:
                shown = [picked] + list(shown)[: _PICK_OPTIONS - 1]
        return [
            discord.SelectOption(
                label=_pick_name(member),
                value=str(member["registrant_id"]),
                description=_pick_where(member) or None,
                default=member["registrant_id"] == chosen,
            )
            for member in shown
        ]

    def _opponents(self) -> list[dict]:
        player = self._member(self.p1) if self.p1 is not None else None
        if player is None:
            return []
        return _pick_opponents(self.field, player, self.state["stage"])

    def _opponent_options(self, opponents) -> list[discord.SelectOption]:
        taken = self._taken()
        shown = self._slice(opponents, "opponent")
        if self.p2 is not None and self.p2 not in {m["registrant_id"] for m in shown}:
            picked = self._member(self.p2)
            if picked:
                shown = [picked] + list(shown)[: _PICK_OPTIONS - 1]
        options = []
        for member in shown:
            # A pair already on one of the day's cards is marked rather than
            # dropped. Dropping it would read as "these two cannot meet", which
            # is the one thing this select is otherwise saying.
            on_card = taken.get(frozenset((self.p1, member["registrant_id"])))
            options.append(
                discord.SelectOption(
                    label=_pick_name(member),
                    value=str(member["registrant_id"]),
                    description=(
                        _PICKS_TAKEN.format(n=on_card) if on_card else _pick_where(member) or None
                    ),
                    default=member["registrant_id"] == self.p2,
                )
            )
        return options

    # ── controls ─────────────────────────────────────────────────────────────

    def _add(self, label, style, row, cb, *, disabled=False):
        button = discord.ui.Button(label=label[:80], style=style, row=row, disabled=disabled)
        button.callback = cb
        self.add_item(button)

    def _select(self, placeholder, options, row, callback):
        select = discord.ui.Select(placeholder=placeholder[:150], options=options, row=row)
        select.callback = callback
        return select

    def _pager(self, label, row, callback, disabled):
        button = discord.ui.Button(
            label=label,
            style=discord.ButtonStyle.secondary,
            row=row,
            disabled=disabled,
        )
        if callback is not None:
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

    # ── rendering ────────────────────────────────────────────────────────────

    def _embed(self) -> discord.Embed:
        """One place the embed is built, so the two ways in cannot drift.

        It carries the half-made meeting because the three selects cannot: a
        Player 1 the pager has moved off shows nothing in its own select, and a
        chosen player who is invisible on the surface that chose them is the
        state this line exists to make impossible.
        """
        working = notice = None
        if not self.adding or self.state["state"] != "ready":
            return build_picks_embed(self.state)

        if not _warzone_counts(_still_in(self.field, self.state["stage"])):
            notice = _PICKS_NOBODY_LEFT.format(
                stage=db.STAGE_LABELS.get(self.state["stage"], self.state["stage"])
            )
        if self.p1 is not None:
            player = self._member(self.p1) or {}
            a = discord.utils.escape_markdown(_pick_name(player))
            if self.p2 is not None:
                working = _PICKS_WORKING.format(
                    a=a, b=discord.utils.escape_markdown(_pick_name(self._member(self.p2) or {}))
                )
            else:
                working = _PICKS_WORKING_HALF.format(a=a)
            if not self._opponents():
                notice = _PICKS_NO_OPPONENTS.format(name=a, button=_btn_words(CD_BTN_RECORD))
        return build_picks_embed(self.state, working=working, notice=notice)

    async def _rerender(self, inter: discord.Interaction, *, notice: str | None = None):
        """Redraw from what is already in hand, and say what just happened.

        The acknowledgement is a separate ephemeral rather than a line on the
        embed: the embed is the card as it now stands, and a "removed X" line
        living on it would still be there two taps later describing something
        that is no longer the last thing to happen.
        """
        self._build()
        await inter.edit_original_response(embed=self._embed(), view=self)
        if notice:
            await inter.followup.send(notice, ephemeral=True)

    async def _reload_state(self) -> None:
        """Re-read the day's cards into `self.state`, without redrawing.

        Split out of `_reload` for `_on_show`, which needs what is on the card
        now but must not repaint the bench underneath a card it is about to
        send. Everything else about the two is the same read.
        """
        self.state = await asyncio.to_thread(
            functools.partial(
                read_picks,
                self.guild_id,
                self.state["grouping"],
                play_on=self.state["play_on"],
                card_no=self.state["card_no"],
                field=self.state.get("field"),
                field_stage=self.state.get("stage"),
            )
        )

    async def _reload(self, inter: discord.Interaction, *, notice: str | None = None):
        """Re-read the day's cards, then redraw.

        Everything the three selects need is already in hand -- the field is
        read once when the flow opens and a warzone or a player only narrows
        it -- so this is for the half that a write moves. The field goes back in
        rather than being read again, and `read_picks` drops it the moment the
        round it was read for stops being the round this card is for.
        """
        await self._reload_state()
        await self._rerender(inter, notice=notice)

    # ── the card ─────────────────────────────────────────────────────────────

    async def _on_day(self, inter: discord.Interaction):
        await inter.response.defer()
        self.state["play_on"] = inter.data["values"][0]
        # Back to card 1 with the day, because card 3 of one day says nothing
        # about another: a day that never overflowed has no card 3 at all, and
        # carrying the number across would open an empty card nobody made.
        self.state["card_no"] = 1
        self._clear_working()
        await self._reload(inter)

    async def _on_card(self, inter: discord.Interaction):
        await inter.response.defer()
        self.state["card_no"] = int(inter.data["values"][0])
        self._clear_working()
        await self._reload(inter)

    async def _on_remove(self, inter: discord.Interaction):
        await inter.response.defer()
        pair = tuple(int(x) for x in inter.data["values"][0].split(":"))
        a, b = (
            discord.utils.escape_markdown(
                (self._member(side) or {}).get("display_name") or picks_lib.CARD_UNKNOWN
            )
            for side in pair
        )
        gone = await asyncio.to_thread(
            functools.partial(
                _uncard_a_meeting,
                self.guild_id,
                self.state["play_on"],
                self.state["card_no"],
                pair,
                actor=_actor(inter),
            )
        )
        await self._reload(inter, notice=_PICKS_REMOVED.format(a=a, b=b) if gone else None)

    async def _on_delete(self, inter: discord.Interaction):
        await inter.response.defer()
        await asyncio.to_thread(
            db.delete_slate, self.guild_id, self.state["play_on"], card_no=self.state["card_no"]
        )
        day = picks_lib.Slate(guild_id="", play_on=self.state["play_on"]).date_label()
        await self._reload(inter, notice=_PICKS_DELETED.format(day=day))

    async def _on_show(self, inter: discord.Interaction):
        """Draw the card, and send it with every row written out beside it.

        **One message, and that is the requirement rather than the layout.**
        Kevin, 2026-08-28: *"we cannot have things just on an image that are
        not also in text."* The image and the embed are sent together, so there
        is no state in which the drawing has arrived and the rows have not.

        **A card that will not draw still goes out.** The rows are the
        substance and the embed carries all of them, so a failed render costs
        the picture and nothing else -- the same silent fallback
        `_send_prediction` makes, for the same reason: the numbers are
        identical either way and the exception still reaches Sentry.

        **Scored from what is on the card right now, not from what the bench
        was holding.** This view lives fifteen minutes and two officers can
        build one evening's card, so the meetings are re-read before they are
        drawn. Scoring is engine work and rendering is Pillow, so both go off
        the event loop. The bench itself is not repainted underneath the card
        -- nothing reads `self.state` without rebuilding first, and a message
        that jumped while a card was being sent would read as the card having
        changed it.
        """
        await inter.response.defer()
        await self._reload_state()
        pairs = [(m["a_id"], m["b_id"]) for m in self.meetings]
        if not pairs:
            await self._rerender(inter, notice=_PICKS_NO_CARD)
            return
        try:
            slate = await asyncio.to_thread(
                functools.partial(
                    picks_lib.assemble,
                    self.guild_id,
                    self.state["play_on"],
                    pairs,
                    # `state["stage"]` rather than the stored card's own,
                    # which `read_picks` has already resolved: a card whose
                    # stage was stamped NULL -- a guild whose grouping came
                    # off the Map Manager warzone fallback -- falls back to
                    # the round the grouping is playing. Reading the stored
                    # value here would head the shared card with a bare date
                    # while the bench above it said `Semi-finals`. Found by
                    # `/code-review`.
                    stage=self.state.get("stage") or "",
                    card_no=self.state["card_no"],
                    # Off the same read the bench headed itself with, so the
                    # card somebody shares says what the screen they built it
                    # on said.
                    card_total=_cards_on_day(self.state),
                )
            )
        except RuntimeError:
            # `picks_lib.assemble` raises this and only this when the engine is
            # not installed, which is an operator problem said in the
            # operator's words rather than a card that quietly never appears.
            await inter.followup.send(_ENGINE_MISSING, ephemeral=True)
            return

        embed = build_slate_embed(slate)
        alt = picks_lib.alt_text(slate)
        png = await _draw_slate(slate)
        view = _SlateShareView(png=png, embed=embed, alt=alt, user_id=self.user_id)
        view.message = await inter.followup.send(
            embed=embed,
            view=view,
            ephemeral=True,
            wait=True,
            **_slate_file(png, alt),
        )

    async def _on_add(self, inter: discord.Interaction):
        await inter.response.defer()
        self.adding = True
        self._clear_working()
        await self._rerender(inter)

    # ── one meeting ──────────────────────────────────────────────────────────

    def _clear_working(self):
        self.warzone = self.p1 = self.p2 = None
        self.pages = dict.fromkeys(_PICK_STEPS, 0)

    async def _on_warzone(self, inter: discord.Interaction):
        """A different warzone, and both lists under it start again.

        Every axis below the one that moved is re-resolved rather than patched,
        which is the rule `_GroupView._reload` applies to its own selects: a
        Player 1 chosen in one warzone means nothing in another, and a page into
        that warzone's players means less than nothing.
        """
        await inter.response.defer()
        self.warzone = inter.data["values"][0]
        self.p1 = self.p2 = None
        self.pages["player"] = self.pages["opponent"] = 0
        await self._rerender(inter)

    async def _on_p1(self, inter: discord.Interaction):
        """Player 1. Player 2 stays empty, including where the bracket has an answer.

        **Offered, never chosen.** Session C set Player 2 from the fold at the
        round of 32, and Kevin took that out on 29 Aug: *"I do not know how this
        actually works out and would rather let the user choose, especially if
        we don't know the actual seed ranks it's safer that way."* The bracket
        rule is measured on one event, `seed_rank` is given rather than derived,
        and a value nobody picked would have written a match on the strength of
        both. **`_opponents` still sorts the partner to the top**, so the likely
        answer is the first thing in the list and the tap is still one tap.
        """
        await inter.response.defer()
        self.p1 = int(inter.data["values"][0])
        self.p2 = None
        self.pages["opponent"] = 0
        await self._rerender(inter)

    async def _on_p2(self, inter: discord.Interaction):
        await inter.response.defer()
        self.p2 = int(inter.data["values"][0])
        await self._rerender(inter)

    async def _on_prev(self, inter: discord.Interaction):
        await inter.response.defer()
        self.pages[self._step()] -= 1
        await self._rerender(inter)

    async def _on_next(self, inter: discord.Interaction):
        await inter.response.defer()
        self.pages[self._step()] += 1
        await self._rerender(inter)

    async def _on_restart(self, inter: discord.Interaction):
        await inter.response.defer()
        self._clear_working()
        await self._rerender(inter)

    async def _on_back(self, inter: discord.Interaction):
        await inter.response.defer()
        self.adding = False
        self._clear_working()
        await self._rerender(inter)

    async def _on_save(self, inter: discord.Interaction):
        """Put the meeting on the card, rolling onto the next card when this one
        is full.

        **Overflow opens a card, it never drops a row.** Twenty is what stays
        legible on the image, not what the data can hold, so the twenty-first
        meeting is card 2 rather than a refusal. `db.MAX_CARDS_PER_DAY` is a
        runaway guard rather than rationing -- four cards of twenty hold the
        whole day's 64 meetings with room over -- so reaching it is the one
        state that does refuse.
        """
        await inter.response.defer()
        if self.p1 is None or self.p2 is None:
            await self._rerender(inter)
            return
        if self.p1 == self.p2:
            await self._rerender(inter, notice=_PICKS_SAME_PLAYER)
            return

        a = discord.utils.escape_markdown(_pick_name(self._member(self.p1) or {}))
        b = discord.utils.escape_markdown(_pick_name(self._member(self.p2) or {}))
        opened = self.state["card_no"]
        try:
            # ONE TRANSACTION, in the database. This was a read of the card
            # followed by a full rewrite of it on a second connection, so a
            # meeting somebody else added in between was dropped silently.
            # `db.add_to_slate` appends under `BEGIN IMMEDIATE`, which is what
            # actually closes that rather than narrowing it.
            target = await asyncio.to_thread(
                functools.partial(
                    db.add_to_slate,
                    self.guild_id,
                    self.state["play_on"],
                    (self.p1, self.p2),
                    card_no=opened,
                    # The round `read_picks` resolved, not the one the calendar
                    # would stamp. The two can differ now that `_pick_stage`
                    # refuses to let a mistyped date decide, and the resolved
                    # one is the round this card was built from -- stamping the
                    # other would head the card for a round its own rows cannot
                    # belong to, and would let it re-label itself as the draw
                    # moved on. Found by `/code-review`.
                    stage=self.state.get("stage") or None,
                    actor=_actor(inter),
                )
            )
        except ValueError as exc:
            # `set_slate` is the authority on a pair already carded, across
            # every one of the day's cards. The selects mark what a read
            # already knew about; this catches the pair somebody else carded
            # while this view was on screen.
            await self._rerender(inter, notice=f"⚠️ {exc}")
            return
        if target is None:
            await self._rerender(
                inter,
                notice=_PICKS_FULL.format(cards=db.MAX_CARDS_PER_DAY, picks=db.MAX_PICKS),
            )
            return

        self.state["card_no"] = target
        self._clear_working()
        notice = (
            _PICKS_ADDED.format(a=a, b=b)
            if target == opened
            else _PICKS_ROLLED.format(a=a, b=b, full=opened, n=target)
        )
        await self._reload(inter, notice=notice)


async def send_picks_view(
    interaction: discord.Interaction,
    *,
    grouping: dict | None,
    user_id: int,
    can_write: bool = True,
) -> None:
    """Open the picks card for today, on the round the guild is playing.

    **It opens on every state rather than refusing three of them.** A guild with
    no Champion Duel resolved, a round the card does not cover, and a round we
    hold no draw for are all real and all different, and each one arrives with
    the control that fixes it named on the surface. Refusing would put the
    flattest dead end in the feature exactly where the contribution is wanted,
    which is the state `send_group_view` was rebuilt out of.
    """
    state = await asyncio.to_thread(read_picks, interaction.guild_id, grouping)
    view = _PicksView(
        user_id=user_id,
        guild_id=interaction.guild_id,
        state=state,
        can_write=can_write,
    )
    await interaction.followup.send(embed=view._embed(), view=view, ephemeral=True)
    view.message = await interaction.original_response()


class ChampionDuelHubView(discord.ui.View):
    """The button grid. Rows group by kind: everyone, contributors, operator.

    **Every state of the hub is this view.** There used to be a second one for
    a finished Champion Duel, and it is why Kevin opened the hub on 31 August
    and could reach none of the work: it was written on 15 August and never
    updated, so `Your standing`, `Your alliance`, `Today's picks` and
    `Head to head` were all added here and never there. `finished` is a state
    this grid is in, not a grid of its own, so a control added below cannot go
    missing from it again.
    """

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
        standing: dict | None = None,
        finished: bool = False,
    ):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.is_admin = is_admin
        self.can_write = can_write
        self.engine_ok = engine_ok
        self.finished = finished
        # Read once on the way in, so `_build_buttons` -- which is not async --
        # can decide which half of the identity pair to draw. The standing
        # surface re-reads before it renders, so this decides the label and
        # nothing else, which is the same deal `can_intel` has.
        self.standing = standing
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
        """Five rows, each one a kind of thing rather than a rank of importance.

        **Kevin's layout, 2026-09-01, and the rows are the reasoning:**

        - **0 — yours.** The personalised surfaces, and the only row that is
          dynamic: what it draws depends on whether we can pick the reader out
          of the roster.
        - **1 — what you open every day** during an event. Not *the Premium
          row*, which was the first reading and is wrong: only `Head to head`
          is gated at the door, and `Today's picks` is free. Premium in this
          feature is a *field* inside several surfaces, not a tier of buttons.
        - **2 — global.** Two names in, an answer out, no Champion Duel needed.
        - **3 — adding and editing** what we hold.
        - **4 — the operator**, and least important by far.

        **THE VISIBILITY RULES DO NOT MOVE.** Kevin, 2026-09-01: *"You
        shouldn't change the logic for when something displays. If your group
        goes under your standings when we do know, then your group disappears
        at that point."* This is a re-lay of the same controls under the same
        conditions, and the one exception is named where it happens.
        """
        # ── Row 0: yours ─────────────────────────────────────────────────────
        #
        # FIRST, AND FIRST ON PURPOSE. `PROPOSAL_champion_duel_ia.md` principle
        # 1 is identity first: the hub opens on the person, and the control
        # that answers "where do I stand" is what the reader's eye should land
        # on before anything else.
        #
        # Which half of the identity pair is drawn is decided by whether we
        # know the reader, exactly as `champion_duel_claim.add_claim_button`
        # decides its pair -- a label that says "your standing" cannot be shown
        # to somebody we cannot pick out of a hundred rows.
        #
        # Absent entirely without a grouping: with no Champion Duel resolved
        # there is no round to stand in, and the caller is being asked for
        # their warzone instead.
        known = (self.standing or {}).get("state") in ("held", "elsewhere")
        if self.grouping:
            if known:
                self._add(CD_BTN_STANDING, discord.ButtonStyle.primary, 0, self._on_standing)
            else:
                self._add(CD_BTN_WHO_AM_I, discord.ButtonStyle.primary, 0, self._on_who_am_i)
        # Drawn whether or not we know the reader, unlike the pair above. That
        # pair swaps because a button reading "your standing" would be a promise
        # to somebody we cannot place; this one lands on a surface that says
        # which of the three things is missing and carries the door for each.
        # Hiding it would make "leadership has no view of their own people" and
        # "you have not claimed yet" the same screen.
        if self.grouping:
            self._add(CD_BTN_ALLIANCE, discord.ButtonStyle.secondary, 0, self._on_alliance)
        # ONLY WHERE THE READER CANNOT REACH IT THROUGH THEMSELVES, which is the
        # rule it already had and keeps. You get to your own group by getting to
        # yourself first, and `🏅 Your standing` carries it opened on your own
        # letter. That is not true before then: an unclaimed reader has no
        # standing to reach it from, and the group listing is a free read
        # carrying the round picker, the alliance filter and the door to
        # recording a round we hold nothing for.
        #
        # **This is also why 🏟️ and 🏅 are not a rule 7 collision.** The two are
        # never drawn together -- knowing who the reader is is exactly what
        # swaps one for the other -- so they could have shared a glyph as they
        # did before. 🏟️ is Kevin's call on 2026-09-01, taken on its own merits
        # rather than forced: the stadium is the field of eight you are drawn
        # against, where 🏅 is the game's own Ranking badge and belongs to the
        # surface about your rank.
        if self.grouping and not known:
            self._add(CD_BTN_GROUP, discord.ButtonStyle.secondary, 0, self._on_group)

        # ── Row 1: what you open every day ───────────────────────────────────
        #
        # Renders locked rather than hidden on the free tier, which is the
        # Premium rule in `DESIGN.md`: an alliance should see the shape of what
        # they would be buying, and this one is hard to describe and easy to
        # show. **The only control in this feature gated at the door.**
        self._add(
            CD_BTN_INTEL if self.can_intel else f"🔒 {CD_BTN_INTEL}",
            discord.ButtonStyle.secondary,
            1,
            self._on_intel,
            disabled=not self.can_intel or not self.engine_ok,
        )
        # NOT GATED ON `can_write`, and it used to be. The card is a read for
        # everybody who is not building one, `_PicksView` draws its own write
        # controls locked, and gating the door would deny the read to keep back
        # the write. Absent without a grouping: with no Champion Duel resolved
        # there is no field to pick two players out of.
        if self.grouping:
            self._add(CD_BTN_PICKS, discord.ButtonStyle.secondary, 1, self._on_picks)

        # ── Row 2: global, and needing no Champion Duel ──────────────────────
        #
        # Finding a player is how somebody reaches an opponent, and it is the
        # gap-fill door as well: a miss lands on `_MissView` and its
        # `➕ Add a player`, which is where adding one now lives.
        self._add(
            CD_BTN_FIND,
            discord.ButtonStyle.secondary,
            2,
            self._on_find,
            disabled=not self.engine_ok,
        )
        # **ALWAYS, AND THAT IS THE ONE VISIBILITY CHANGE HERE.** Kevin,
        # 2026-09-01: *"I think that it should always be at that root level."*
        #
        # It used to be drawn only where `🔮 Today's picks` was not, on the
        # reasoning that predicting one match is *"absorbed by the day's card"*
        # and would otherwise be a second front door to something that already
        # has one. **That reasoning was wrong, and Kevin found it:** the card
        # answers *who should I pick today* out of this stage's field, and this
        # answers *what happens if these two meet* for any two players we hold.
        # Same inputs, different questions, and only one of them is about today.
        #
        # It also means the one-off stops living two clicks deep on a bench it
        # has nothing to do with, which is where it was reachable from when a
        # Champion Duel was resolved.
        self._add(
            CD_BTN_PREDICT,
            discord.ButtonStyle.secondary,
            2,
            self._on_predict,
            disabled=not self.engine_ok,
        )

        # ── Row 3: adding and editing what we hold ───────────────────────────
        #
        # Recording needs a grouping to file the group against, so it is absent
        # rather than disabled when there is none: on that surface the caller is
        # being asked for their warzone and has nothing to record yet.
        #
        # OPEN TO EVERYONE, STILL. `PROPOSAL_champion_duel_ia.md` principle 4
        # puts batch entry behind a role the alliance configures; that role map
        # does not exist and gating this today would take recording away from
        # members who have it.
        if self.grouping:
            self._add(
                f"🔒 {CD_BTN_RECORD}" if not self.can_write else CD_BTN_RECORD,
                discord.ButtonStyle.secondary,
                3,
                self._on_record,
                disabled=not self.can_write,
            )
        # A wrong warzone points the whole server at somebody else's tournament,
        # and nothing else on this hub can fix it. Present whenever we resolved
        # from one, which is the only time there is something to change.
        if self.warzone:
            self._add(CD_BTN_CHANGE_WARZONE, discord.ButtonStyle.secondary, 3, self._on_warzone)
        # One control, and the form asks nothing about whose Champion Duel it
        # is -- see `_AddGroupingModal`. It needs a Champion Duel resolved for
        # the same reason `Record a group` does: without one the caller is being
        # asked for their warzone instead, and `ChampionDuelOnboardingView`
        # carries `CD_BTN_ADD_GROUPING` for exactly that.
        if self.grouping:
            self._add(CD_BTN_ADD_CD, discord.ButtonStyle.secondary, 3, self._on_add_cd)

        # ── Row 4: the operator, least important by far ──────────────────────
        #
        # Absent entirely for everyone else, so for every other reader this is
        # a four-row grid and Discord collapses the gap.
        if self.is_admin:
            self._add(CD_BTN_EDITS, discord.ButtonStyle.secondary, 4, self._on_edits)
            self._add(CD_BTN_REVERT, discord.ButtonStyle.secondary, 4, self._on_revert)
            self._add(CD_BTN_EXPORT, discord.ButtonStyle.secondary, 4, self._on_export)

    # ── callbacks ─────────────────────────────────────────────────────────────

    async def _on_standing(self, inter: discord.Interaction):
        """Where the reader stands, and how far they get.

        RE-READ RATHER THAN RENDERED OFF `self.standing`. This view lives
        fifteen minutes and a claim can move inside that window from another
        message -- `ClaimResultView` carries a release button that does exactly
        that. Rendering the captured copy would show somebody the standing of
        an account they gave up while it was on screen.
        """
        await inter.response.defer(ephemeral=True, thinking=True)
        standing = await asyncio.to_thread(
            read_standing, inter.user.id, self.grouping, warzone=self.warzone
        )
        if standing["state"] == "unclaimed":
            # They released the claim while this hub was open. The invite is
            # the honest surface, and it is the same one the landing would have
            # drawn had it been built a moment later.
            view = _StandingClaimView(
                user_id=inter.user.id, can_write=self.can_write, grouping=self.grouping
            )
            await inter.followup.send(standing_opener(standing), view=view, ephemeral=True)
            view.message = await inter.original_response()
            return
        # Re-resolved rather than read off `self`, the same way the odds press
        # does it: this view outlives the five minute entitlement cache, so a
        # subscription that lapsed while the hub sat on screen would otherwise
        # be served the paid half by a button that was enabled at build time.
        can_odds = bool(
            inter.guild_id
            and await premium.feature_gate("champion_duel_odds", inter.guild_id, interaction=inter)
        )
        # THE UPDATE IS ON EVERY STANDING, not only on the one that looks like
        # it needs it. This is the half of the warzone-switch answer that does
        # not depend on noticing anything: whoever opens their own standing can
        # point it at a different account from right here, and claiming a new
        # one moves the claim (`CLAIM_MOVED`). Nothing has to be detected.
        #
        # The player rides along so `CD_BTN_EDIT_ME` is offered at all; the
        # modal it opens re-reads the claim when it is pressed.
        view = _StandingClaimView(
            user_id=inter.user.id,
            can_write=self.can_write,
            grouping=self.grouping,
            player=standing.get("player"),
            standing=standing,
            can_odds=can_odds,
            warzone=self.warzone,
        )
        await inter.followup.send(
            embed=build_standing_embed(standing, can_odds=can_odds),
            view=view,
            ephemeral=True,
        )
        view.message = await inter.original_response()

    async def _on_who_am_i(self, inter: discord.Interaction):
        await inter.response.send_modal(
            claim_lib.ClaimModal(can_write=self.can_write, grouping=self.grouping)
        )

    async def _on_alliance(self, inter: discord.Interaction):
        """Where all of this leader's people are, across every group.

        READ FRESH, like the standing press and for the same reason: this view
        lives fifteen minutes and a claim can move inside that window from
        another message, so a captured copy could show somebody the alliance of
        an account they gave up while it was on screen.
        """
        await inter.response.defer(ephemeral=True, thinking=True)
        # Re-resolved rather than read off `self`, the same way the odds press
        # does it: this view outlives the five minute entitlement cache, so a
        # subscription that lapsed while the hub sat on screen would otherwise
        # be served the paid half by a button that was enabled at build time.
        can_odds = bool(
            inter.guild_id
            and await premium.feature_gate("champion_duel_odds", inter.guild_id, interaction=inter)
        )
        # `with_odds` follows the entitlement. A free guild renders none of
        # them, so reading them would be a `get_group_scouting` per group for
        # an answer the embed drops -- and `store_lib.lookup` stamps
        # `last_viewed_at`, so it would also push groups whose answer nobody
        # here can be shown to the front of the sweeper's queue.
        state = await asyncio.to_thread(
            read_alliance,
            inter.user.id,
            self.grouping,
            warzone=self.warzone,
            with_odds=can_odds,
        )
        view = _AllianceView(
            user_id=inter.user.id,
            grouping=self.grouping,
            state=state,
            can_odds=can_odds,
            can_intel=self.can_intel,
            can_write=self.can_write,
            warzone=self.warzone,
        )
        await inter.followup.send(
            embed=build_alliance_embed(state, can_odds=can_odds), view=view, ephemeral=True
        )
        view.message = await inter.original_response()

    async def _on_predict(self, inter: discord.Interaction):
        await inter.response.send_modal(_PredictModal())

    async def _on_intel(self, inter: discord.Interaction):
        await inter.response.send_modal(_IntelModal())

    async def _on_find(self, inter: discord.Interaction):
        await inter.response.send_modal(_FindPlayerModal(self.can_write, grouping=self.grouping))

    async def _on_warzone(self, inter: discord.Interaction):
        await inter.response.send_modal(
            _WarzoneModal(can_write=self.can_write, current=self.warzone)
        )

    async def _on_add_cd(self, inter: discord.Interaction):
        """Sixteen warzones and a date, for a Champion Duel of either kind.

        **It asks nothing about whose it is.** Kevin struck that question on
        2026-08-31: *"we should not care who all it is - for all we know it
        could be theirs from a past Duel and we don't have a reason to need to
        know."* Nothing needed the answer -- the pin derives itself from
        whether the hub will open on the result, and the acknowledgement reads
        off that.

        So this passes `onboarding=False`, which drops one thing only: the
        guard that the sixteen contain your own warzone. Everything else is
        shared -- the count, the duplicate check and the overlap conflict are
        what stop a mistyped list becoming a grouping nobody can untangle, and
        none of them depend on whose Champion Duel it is.

        **⚠️ That guard is now unreachable for a returning server**, which is
        a known gap rather than an oversight: `ChampionDuelOnboardingView` is
        the only caller that passes `onboarding=True`, and it draws only where
        no Champion Duel is resolved. A one-digit typo in your own next set
        therefore creates a grouping you are not in, reports success, and
        leaves the hub where it was. Raised with Kevin 2026-09-01: it is a
        typo catch rather than an identity question, so it may want to come
        back as a note on the acknowledgement rather than as a refusal.
        """
        await inter.response.send_modal(
            _AddGroupingModal(
                can_write=self.can_write,
                warzone=self.warzone,
                onboarding=False,
            )
        )

    async def _on_record(self, inter: discord.Interaction):
        # Read before responding, not after: a modal has to be the first
        # response to an interaction, so this cannot defer first. One indexed
        # SQLite read is well inside the three seconds.
        stage, groupings = await asyncio.gather(
            asyncio.to_thread(db.current_stage, self.grouping["id"]),
            asyncio.to_thread(
                db.groupings_readable_by,
                self.warzone,
                str(inter.guild_id) if inter.guild_id else None,
            ),
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
        """Who is in this Champion Duel, for a reader we cannot place in it.

        No stage and no letter: we do not know which group is theirs, so this
        opens exactly what the retired root control opened -- the round the
        guild is playing, with the picker and the alliance filter on it. A
        reader we do know reaches the same surface from `🏅 Your standing`,
        where it can be opened on their own group instead.
        """
        await inter.response.defer(ephemeral=True, thinking=True)
        await send_group_view(
            inter,
            grouping=self.grouping,
            warzone=self.warzone,
            user_id=self.user_id,
            can_write=self.can_write,
        )

    async def _on_picks(self, inter: discord.Interaction):
        """The day's card, and the flow that fills one in.

        Opens on today for the round the guild's Champion Duel is playing. It
        opens on the states with nothing in them too, each carrying the control
        that fixes it, which is the same deal `send_group_view` takes: an alliance
        that has just set its Participating Warzones holds nothing by
        definition, and being told so with no way to fix it is the dead end
        this whole surface is being rebuilt out of.
        """
        await inter.response.defer(ephemeral=True, thinking=True)
        await send_picks_view(
            inter,
            grouping=self.grouping,
            user_id=self.user_id,
            can_write=self.can_write,
        )

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

    # Past the last day. **Not a branch to a different surface any more.** It
    # was one until 2026-08-31, and the fork was where everything built after
    # 15 August went missing: the second view was never updated, so between
    # events the hub silently reverted to a fortnight-old shape. It is now one
    # flag on the surface everybody else gets, which is a state the hub is in
    # rather than a hub of its own.
    finished = bool(grouping and await asyncio.to_thread(db.is_finished, grouping["id"]))

    # The person, read once and handed to both halves of the surface. Only
    # inside a guild with a Champion Duel resolved: without a grouping there is
    # no round to stand in, and in a DM there is nothing to scope it to, so
    # both fall through to exactly the hub they had before.
    #
    # ONE READ, NOT TWO. The embed and the button grid disagree if they ask
    # separately, and the disagreement they can reach is the one that matters:
    # a landing that says we do not know who you are, over a button that says
    # `Your standing`.
    standing = (
        await asyncio.to_thread(
            read_standing, interaction.user.id, grouping, warzone=warzone, with_odds=False
        )
        if grouping
        else None
    )

    view = ChampionDuelHubView(
        user_id=interaction.user.id,
        is_admin=_is_admin(interaction.user.id),
        can_write=can_write,
        engine_ok=engine_ok,
        warzone=warzone,
        grouping=grouping,
        can_intel=can_intel,
        standing=standing,
        finished=finished,
    )
    await interaction.followup.send(
        content=note,
        embed=build_hub_embed(
            servers=servers,
            can_write=can_write,
            grouping=grouping,
            warzone=warzone,
            standing=standing,
            finished=finished,
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
