---
name: ux-review
description: Check user-facing surfaces against UX.md and DESIGN.md. Run it before building a new surface to get the constraints that apply ("brief" mode, pass a surface name or issue), or after the work to audit the diff for terminology, copy, and interaction drift (default). Use whenever a change touches a slash command, hub, wizard, embed, button label, DM, scheduled post, or error message.
---

# UX review

Two modes over one knowledge base.

- **No argument** or a diff target: **review mode.** Audit what changed
  against the contracts.
- **A surface name, issue number, or description of upcoming work:**
  **brief mode.** Pull forward the rules that will apply, before any
  code is written.

Always load both `notes/UX.md` and `notes/DESIGN.md` first. They are
the contract; this skill is only the procedure for applying them.
`messages.py` is the third source: it holds the shared copy constants
and the per-string rationale for each one.

**Both contracts live in the private notes repo, not in this tree.**
`notes/` is an independent repo cloned into this one and is gitignored
here, so a checkout without it will not have them. If `notes/UX.md` and
`notes/DESIGN.md` are not present, **stop and say so.** Do not review
from memory, and do not reconstruct the rules by reading the codebase:
a review against guessed conventions is worse than no review, because
it produces confident findings with nothing behind them.

---

## Brief mode

Given a surface to be built, produce a short pre-flight. Do not write
code and do not design the feature. Answer only:

1. **Audience.** Which of the four in `notes/UX.md` is this for? If more than
   one, which surfaces belong to which? Installer, officer, member, and
   `/admin` have different rules and mixing them is the most common
   error.
2. **Surface type.** Which row of the `notes/DESIGN.md` surface table. State
   the ephemerality and lifetime that follow from it.
3. **Existing vocabulary.** Grep for the terms this feature will use.
   Report the canonical form from the glossary, the `HUB_BTN_*`
   constants to import, and the emoji already in use for these actions.
4. **Copy already available.** Which `messages.py` constants cover the
   timeouts, cancels, gates, and validation failures this flow will
   need. Flag anything the flow needs that has no constant yet.
5. **Principles in tension.** Which of the seven `notes/UX.md` principles
   this work could plausibly violate, and where the risk sits.
6. **Failure modes to design now.** What breaks when the channel is
   deleted, the sheet tab is renamed, the view times out, the bot
   restarts mid-flow, the alliance has 200 members, or the option list
   exceeds 25. Whether it needs a `config_health` subject and an
   `outage_catchup` adapter.

Keep it under a page. It is a checklist to build against, not a design
document.

---

## Review mode

### Scope

Default target is the working-tree diff against the branch base. If the
user names a PR, branch, or path, use that instead.

Filter to files that changed **user-facing surfaces**: slash commands,
hubs, wizards, views, embeds, button labels, DMs, scheduled posts,
error messages, `/help` content, and `messages.py` itself. Skip pure
data-layer, test, and tooling changes. If nothing in the diff is
user-facing, say so and stop.

### Checks

Run all of these. For each finding, quote the actual string or line.

**Terminology**

- Every term against the `notes/UX.md` glossary. `guild` in user copy,
  bare `DS`/`CS` where the full name fits, rank codes `R4`/`R5`,
  lowercase `premium`, "spreadsheet" for the alliance's Sheet, "sheet"
  where "tab" is meant.
- Feature names match the `HUB_BTN_*` constant exactly.
- Any button label or feature name referenced from another module is
  **imported**, not retyped. A hardcoded duplicate is a finding even
  when the words currently match.
- Internals leaking into copy: column names, table names, function
  names, exception text, `_id` suffixes.

**Naming**

Each of these is a defect observed in shipped software of this kind,
not a hypothesis. Full rules in `notes/UX.md` (Naming).

- **One name, one meaning.** For every term the diff introduces, grep
  the product for it. If it already means something else anywhere, that
  is a finding. Report both call sites.
- **The label describes the control**, not the outcome two steps later.
  A field label says what to put in the field.
- **No undefined jargon.** Any term that is not the game's own word and
  not defined on the surface using it is a finding. Test: would an
  officer who has never opened this surface know what it refers to?
- **Inherited game vocabulary is pinned down, not passed through.**
  This one needs judgment and does not reduce to a grep, so treat it as
  a question to answer rather than a pattern to match: *is this term
  ambiguous in Last War or in our own product, and if so does this
  usage make clear which sense is meant?* The known overloads are
  `draft`, `roster`, and `transfer`, each tabled in `notes/UX.md` with its
  live senses. A new surface using any of them bare, outside a context
  that has already established the sense, is a finding. A term not on
  that list may still be overloaded; check before assuming it is not,
  and add it to the table if it is.

**Copy**

- Success acks are sentence-form, not `Verb: object`. The repo already
  enforces this in `tests/unit/test_no_verb_colon_acks.py`; a finding
  here means the test needs its pattern widened.
- Cancel copy uses the right one of `CANCEL_PLAIN` versus
  `CANCEL_BACKPEDAL`: whole flow dead versus parent intact.
- "try again" for a single retry, "start again" for a flow that ended.
- New inline strings that duplicate an existing `messages.py` constant.
- Second person, present tense, plain English. No effort or time
  language. No exclamation marks outside genuine celebration.
- **No em dashes in anything a user can see.** Bot copy, `CHANGELOG.md`,
  `docs/DISCORD_CHANGELOG.md`, `README.md`, release and announcement
  posts. Not comments, docstrings, `print()` / log lines, or internal
  docs, and not the changelog version header separator. Full rule and
  carve-out in `notes/UX.md` (Voice).
- Copy that assumes the alliance writes in English.

**Recovery**

- Every timeout, validation failure, permission denial, and
  not-configured gate names the exact route back: the command *and*
  the button, not just `/setup`.
- Every view posted by a background task overrides `on_timeout` to
  call `wizard_registry.expire_view_message`.
- Anything that can fail because the alliance changed a channel or a
  sheet records through `config_health` rather than failing silently or
  Sentry-capturing.

**Design**

- Color matches semantics: green success, red broken, orange partial,
  blurple neutral, greyple disabled. Identity colors only for
  paired-event features.
- **Encoding agrees with meaning.** For anything rendering a
  comparison, delta, or ranking: does green mean good *here*? A bigger
  number is not automatically better (enemy power, missed events, days
  since last drive). Check emphasis too: if the row's most important
  field is small grey text and a repeated constant carries the visual
  weight, the hierarchy is inverted.
- **Every control can change something.** A button or select that is a
  no-op under current conditions is a finding. It should be removed or
  rendered disabled with the reason, never left live and inert.
- Emoji come from the `notes/DESIGN.md` catalog and mean what they mean
  elsewhere. A new emoji is a finding unless the catalog is updated in
  the same change.
- At most one `primary` button per view. `danger` only for
  irreversible loss.
- Labels are `{emoji} {Sentence case}`, under ~35 characters.
- Row grouping and button order preserved. A button inserted mid-row
  in an existing grid is a finding.
- Responses ephemeral unless there is a written reason.
- Anything rendering alliance-supplied text is clamped to the relevant
  Discord limit.
- Selects that can exceed 25 options have paging or filtering.

**Consistency with siblings**

The audit lesson in `CLAUDE.md`: a pattern fixed once often never
propagates. If the change establishes or corrects a UX pattern, grep
for structurally similar surfaces and report which ones now diverge.
This is the highest-value check in the skill and the one most often
skipped.

### Reporting

Rank most severe first. For each:

- File and line, as `path.py:123`.
- The string or construct, quoted.
- What contract it breaks, naming the file and section
  (`notes/UX.md` glossary, `notes/DESIGN.md` buttons).
- The concrete fix.

Then a short list of anything the diff does that `notes/UX.md` or `notes/DESIGN.md`
does not cover. That list is the point of the exercise: it is how the
contracts get better. Do not invent a rule to close a gap; report the
gap and let it be decided.

Report only what you verified by reading the code. No effort sizing, no
severity theater, no em dashes in the write-up.

If nothing is wrong, say so plainly and still report the uncovered-gap
list.

### Validating the checks themselves

If the naming or encoding checks above are ever changed, validate them
against a corpus with known answers before trusting them on our own
diff. Reviewing our own work is the worst possible test set: the checks
were written by the same hand that wrote the surfaces. Local research
notes carry a suitable corpus and how to use it.

### Applying fixes

Only when asked. Terminology, copy, and constant-extraction fixes can
be applied directly. Anything that changes a button grid, a color
scheme, or a flow's shape is a decision, not a fix: propose it and
stop.
