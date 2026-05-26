"""
messages.py — central registry of recurring user-facing copy (#267).

This module owns templates and constants for recurring user-facing
strings that previously lived as inline literals at 200+ callsites
across the bot. The audit + canonical-wording decisions live in
`notes/COPY_AUDIT_267.md` (gitignored); the resulting contract is
this file.

Imported by every module that previously inlined the literal. A
rename of any of the strings below is now a one-line change in this
file, instead of an 8-10-file sweep.

Companion to `setup_hub.py` (which owns hub button label constants
from #208) and `storm_event_hub.py` (which owns storm hub button
label constants). Those modules export the `HUB_BTN_*` constants
that many of the templates below take as `{wizard}` / `{hub_btn}`
parameters.

Style guide: see the "Success acknowledgements" section near the
bottom for the sentence-form rule and what's deliberately NOT
extracted.
"""
from __future__ import annotations


# ── Wizard scaffolding ───────────────────────────────────────────────────────
#
# Timeouts, cancellations, "not set up yet" gates, and feature-not-
# configured gates. Highest-frequency cluster in the audit (~150
# callsites).

# Setup wizard timed out, recovery is to re-enter the wizard via /setup.
# Callers pass HUB_BTN_* as {wizard}. Canonical wording = "to start again"
# (per D1; majority before, matches the Add-to-today's-draft fix wording).
WIZARD_TIMEOUT = "⏰ Timed out. Run `/setup` → {wizard} to start again."

# Non-setup wizard timed out; recovery is to re-run the slash command.
# Caller passes the command name (without leading slash) as {cmd}.
GENERIC_CMD_TIMEOUT = "⏰ Timed out. Run `/{cmd}` to start again."

# Hub command timed out mid-flow; recovery is to re-open the hub and
# click the same button. Caller passes the slash command (without
# leading slash) as {cmd} and a HUB_BTN_* constant as {hub_btn}.
HUB_TIMEOUT = "⏰ Timed out. Run `/{cmd}` and click **{hub_btn}** to start again."

# User cancelled a top-level command/wizard. Whole flow is dead, no
# parent state to preserve.
CANCEL_PLAIN = "❌ Cancelled."

# User backed out of a sub-step. Parent flow is intact; the generic
# "nothing happened, you can continue" reassurance.
CANCEL_BACKPEDAL_DEFAULT = "↩️ Cancelled. No changes made."

# User backed out of a sub-step with meaningful state context to
# preserve. Caller passes the contextual sentence as {detail} (e.g.
# "Your saved draft is still there." or "**Glacieradon** was not
# deleted."). Always end {detail} with a period.
CANCEL_BACKPEDAL = "↩️ Cancelled. {detail}"

# Bot's core setup hasn't been completed yet on this guild. Surfaces
# in every feature command that runs before /setup has been walked.
NOT_SET_UP = "⚙️ This bot hasn't been set up yet. Run `/setup` to get started."

# Same gate, but rendered inside a hub embed — the recovery is the
# button on the hub itself, not a slash command. Caller passes the
# hub's open-setup button label (typically setup_hub.HUB_BTN_SETUP_WIZARD)
# as {hub_btn}.
NOT_SET_UP_HUB = "⚙️ This server hasn't been set up yet. Click **{hub_btn}** above to start."

# A specific feature isn't configured yet on this guild. Used when
# the feature has its own setup wizard the user needs to walk first.
# Caller passes the feature display name as {feature} (e.g. "Member
# Roster Sync") and the relevant HUB_BTN_* as {wizard_btn}.
FEATURE_NOT_CONFIGURED = "⚙️ {feature} isn't configured yet. Run `/setup` → {wizard_btn} first."


# ── Premium / permissions ────────────────────────────────────────────────────
#
# Inline premium gates + view-ownership permission denials. Canonical
# upsell verb is "unlock it" per D6.

# Inline premium gate — feature is locked, here's how to upgrade.
# Caller passes the feature display name as {feature} (e.g. "Trends
# Viewer", "DM-the-roster"). Embed-style upsells (`premium_locked_embed`,
# `limit_reached_embed`) are separate; their bodies use the same
# canonical "unlock it" verb but render as full embeds in premium.py.
PREMIUM_LOCKED_INLINE = "🔒 The **{feature}** is a 💎 Premium feature. Run `/upgrade` to unlock it."

# Generic view-ownership deny. The audit collapsed 50+ near-identical
# "⛔ Only the {role} who {opened|ran|started} X can {action}" messages
# onto this single canonical (D8). The per-action verbs ("can edit",
# "can record", "can pick") were doing no real work — the user knows
# what they tried.
DENY_NOT_OWNER = "⛔ Only the user who opened this view can use it."

# Admin-or-role permission deny. The caller passes the leadership
# role display name as {role} and a short action verb phrase as
# {action} (e.g. "use the setup hub", "run `/setup`").
DENY_ADMIN_OR_ROLE = "⛔ You need server administrator permission or the **{role}** role to {action}."


# ── Leadership channel / configured-channel errors ───────────────────────────

# Three distinct leadership-channel failure states. Surfaces in event
# log + scheduler paths when the configured channel is missing,
# inaccessible, or lacks read-history permission.
LEADERSHIP_NOT_CONFIGURED = "⚠️ Leadership channel isn't configured. Run `/setup` to configure it."
LEADERSHIP_INACCESSIBLE   = "⚠️ Could not access the leadership channel."
LEADERSHIP_NO_READ_PERM   = "⚠️ Bot does not have permission to read message history in the leadership channel."

# Setup-wizard channel-pick step found the previously-configured channel
# no longer exists (deleted, perms changed, etc.). The wizard offers
# a new pick immediately below. Caller passes a short label (e.g.
# "leadership", "draft", "announcement", "birthday") as {channel_label}.
PREV_CHANNEL_GONE = "⚠️ Your previously configured {channel_label} channel no longer exists. Pick a new one below."


# ── Validation retry messages ────────────────────────────────────────────────
#
# Single-input parse failures + give-up-after-N-tries variants. The
# wording distinction:
#   * "try again"   — single input wrong; user retypes
#   * "start again" — gave up after N tries; user re-enters the wizard

# User typed something that isn't a time. They get one more shot.
# Canonical example trio: 9:00am, 10:15pm, 22:00 (covers AM, PM, 24h).
# Caller passes the user's raw input as {raw}.
TIME_PARSE_RETRY = (
    "⚠️ Could not read **`{raw}`** as a time. "
    "Try `9:00am`, `10:15pm`, or `22:00`. Let's try once more."
)

# Same as above but after N failed tries — wizard bails out and tells
# the user how to re-enter. Caller passes the recovery hint (e.g.
# "`/setup` → 📋 Survey" or "`/survey remind`") as {recovery}.
TIME_PARSE_GIVE_UP = (
    "⚠️ Could not read that time after a few tries. "
    "Run {recovery} to start again."
)

# Generic validation failure with an example. Caller passes the input
# type (e.g. "row number", "single column letter", "number") as {type},
# the example value as {example}, and the recovery hint as {recovery}.
INPUT_INVALID = "⚠️ Please enter a {type} like `{example}`. Run {recovery} to try again."

# Same but for input types that don't need an example (e.g. "whole
# number"). Caller passes {type} and {recovery}.
INPUT_INVALID_NO_EXAMPLE = "⚠️ Please enter a {type}. Run {recovery} to try again."

# Date didn't parse. Caller passes the user's raw input as {raw}.
DATE_PARSE_REJECT = "⚠️ `{raw}` isn't a date I can parse. Try `May 18`, `5/18`, or `18-May`."


# ── Footers ──────────────────────────────────────────────────────────────────

# Free vs Premium comparison footer for tier-gated features. Caller
# passes the free-tier limit description (e.g. "7-day window", "5 of
# 10 metrics used") as {free_limit} and the premium description
# (e.g. "30 days", "unlimited") as {premium_limit}.
TIER_COMPARISON = "Free tier: {free_limit}. Upgrade to Premium for {premium_limit}."

# Pointer footer that tells leadership where to go to change a
# feature's settings. Caller passes a HUB_BTN_* constant as {wizard}.
SETUP_POINTER_FOOTER = "Run `/setup` → {wizard} to update settings."


# ── Success acknowledgements ─────────────────────────────────────────────────
#
# Success acks follow SENTENCE FORM, not "Verb: object" form. There are
# NO constants/templates for them in this module — every ack carries
# unique context (the date, the channel name, the count, the field
# they changed) that doesn't templatize usefully. The rule below is
# the contract callsites follow.
#
# Style:
#
#   ✅ Updated **{name}**.
#   ✅ Added **{name}** ({n} of {cap}).
#   ✅ Saved attendance for **{date}**: {details}
#   🗑️ Removed **{name}** from **{zone}**.
#   📬 Sent {n} reminder DMs to {label}.
#
# Rules:
#   1. Lead with the emoji + past-tense verb + bolded object.
#   2. Period-terminate the sentence.
#   3. Extra context is layered as natural English ("for **{date}**",
#      "to the **{tab}** tab", "in {channel}") — never as a bare colon
#      after the verb.
#   4. A colon AFTER a complete verb-object phrase is fine — it
#      introduces a list or detail block:
#         ✅ OK:  "✅ Saved attendance for **{date}**: {details}"
#         ✅ OK:  "✅ Added preset(s): {summary}"
#         ❌ BAD: "✅ Updated: **{name}**"
#         ❌ BAD: "🗑️ Removed: **{label}**"
#   5. Emoji catalog (pick the one that names the action):
#         ✅  default / save / add / update
#         🗑️  delete / remove
#         ↔️  move
#         💾  persist (especially when distinct from a plain ✅ save)
#         📬  send / post
#         ↩️  cancel a sub-step (not a success — see CANCEL_BACKPEDAL)
#
# Enforcement: `tests/unit/test_no_verb_colon_acks.py` greps the
# codebase for the `[emoji] Verb: **` antipattern and fails CI if
# any callsite regresses.
