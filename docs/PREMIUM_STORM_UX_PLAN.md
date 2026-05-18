# Premium Storm UX — Feedback Resolution Plan

Companion to `docs/PREMIUM_STORM_UX_WALKTHROUGH_edited.md`. Captures
every EDITED / NOTE the user left in sections 1-7, the answers to the
design questions that came out of catalog, and the cross-cutting rules
that need to ripple through sections 8-15 and into the live code.

## Branch reality

- **`dev` (local)**: holds the implemented Premium Storm structured-flow
  code (`storm_signup_*.py`, `storm_roster_builder.py`, `storm_strategy.py`,
  `storm_member_rules.py`, `storm_attendance.py`, `storm_history.py`,
  `storm_walkthrough.py`, plus `setup_cog._run_structured_flow_setup_step`
  and helpers) and the edited walkthrough doc
  (`docs/PREMIUM_STORM_UX_WALKTHROUGH_edited.md` — the user's annotated
  copy) alongside the original `PREMIUM_STORM_UX_WALKTHROUGH.md`.
- **`main`**: feature code not present — Premium Storm structured flow
  has not yet shipped to main.

So this is **real code-change work**, not spec drafting. The plan is:
1. Implement each cross-cutting rule as its own feature branch off
   `dev`, PR'd back to `dev` for staging (per CLAUDE.md major-feature
   routing — schema migrations + persistent Views + scheduler changes
   = dev-first).
2. When the staged set passes end-to-end on the dev Discord app, batch
   into a release branch and PR to `main`.
3. Reconcile the two walkthrough files at the end (either fold the
   `_edited` annotations back into the canonical `PREMIUM_STORM_UX_WALKTHROUGH.md`
   and delete the `_edited` copy, or keep both — your call).

---

## 0 — Design decisions locked in (your answers to my catalog questions)

Decisions 1–4 came out of the first catalog pass. Decisions 5–12 came
out of the section 8–15 review pass.

| # | Decision |
|---|---|
| 1 | **Screen 7.14 = bulk move.** Button "🪑 Add all unassigned to Subs" moves every currently-unassigned member into the subs pool. Body copy gets rewritten to match. |
| 2 | **Screen 6.1 = ephemeral with two selects + Submit.** Officer picks Member (Select 1) and Vote (Select 2), then hits Submit. Roster > 25 names handled via paging. No longer a Discord modal. |
| 3 | **Screen 7.24 = single primary-picker + sub-picker pair.** One Select for primary, one for sub, an Assign button, repeat. Running pair list rendered above the selects. |
| 4 | **2.7/2.8 model = poll-day picker, no lead-days knob.** Wizard asks *"What day do you want to send the poll?"* DS event always Friday, CS event always Thursday. Picking the poll day implicitly sets the lead. Schema drops `event_day_of_week` and `signup_lead_days`; gains `poll_day_of_week`. |
| 5 | **Attendance UI = member-select + ✅/❌ buttons applied in-place.** No status-picker ephemeral. When a member is selected in the dropdown, the buttons read `[✅ Mark as attended]` / `[❌ Mark as did not attend]` and write directly to that member. When no member is selected, the buttons read `[✅ Mark all as attended]` / `[❌ Mark all as did not attend]` and bulk-apply to unrecorded slots. Drops the 🔄 Sub activated status entirely — UI is ✅/❌/— only. |
| 6 | **Drop "below minimum" override marker from UI rendering.** The internal `Override Below Floor` sheet column stays (the builder still records the override at assignment time), but the per-member `⚠️` glyph and the `_⚠️ Assigned below the zone floor at build time._` footnote disappear from attendance + history views. Officers don't need this surfaced post-event. |
| 7 | **Per-member rules silently no-op when subject isn't in this event's roster.** Auto-fill summary stops flagging "subject not on roster" as a conflict. A rule means "*if* this member is in tonight's event, do X" — they're not in tonight's event ⇒ nothing to apply ⇒ nothing to report. Applies to `per_member` zone/team rules. |
| 8 | **User-actionable summaries show every entry — no truncation.** Auto-fill gaps + conflicts lists drop the `(+N more)` truncation. Officers need the complete list to make decisions about who to slot manually. |
| 9 | **Destructive re-runs need a confirm step.** Clicking 🎯 Auto-fill on a session with manual edits opens a confirm ephemeral first (`This will clobber every manual edit you've made. Confirm?`). New sessions skip the prompt. |
| 10 | **Phase-aware UIs render per-zone-per-phase.** Each zone header gets its own line, then each phase indents below as its own line with the per-phase capacity + member list. `📊 Filled` line breaks out per-phase counts (e.g. `P1: 30/30, P2: 30/30, P3: 30/30`). Applies to auto-fill summary, preset editor, builder embed phase-aware variant, and history when rendering a phase-aware event. |
| 11 | **List commands always include inline actions.** `/desertstorm strategy list`, `/canyonstorm strategy list`, `/desertstorm member_rule list`, `/canyonstorm member_rule list`. Both the populated-list and empty states render summary + `[Create]` / `[Edit]` / `[Delete]` buttons. No dead-end summary that forces officers into a second slash command. |
| 12 | **Walkthrough tour fires for `/canyonstorm signups` too.** Today the tour offer only fires on `/desertstorm signups`. Add the same offer + tour to CS; tour copy branches on `teams` config when describing the team-setup step (Rule A). 14.9 (on-behalf vote step) gets rewritten to describe the new ephemeral view + selects (Decision #2). |
| 13 | **Preset editor: zones are game-defined — Add Zone modal goes away.** DS_ZONE_STRUCTURE and CS_ZONE_STRUCTURE are the canonical lists; alliances can't add their own. Screen 12.15 + the `[➕ Add zone]` button + `_AddZoneModal` class get deleted. |
| 14 | **Auto-pair summary lists who-with-whom.** When auto-fill auto-pairs subs, the summary block shows each pairing explicitly (`Alice ↔ Bob, Carol ↔ Dan, …`) instead of just a count. This is the highest-edit candidate from auto-fill, so visibility matters. |

---

## 1 — Cross-cutting rules (apply EVERYWHERE — sections 1-15)

Each rule below is the source of multiple individual feedback items.
Codifying them up front so the section-by-section sweep is mechanical.
File pointers reference the **dev branch** state.

### Rule A — Canyon Storm has Teams A & B, just like Desert Storm
- `teams` column already exists on `guild_storm_config`, but its
  inline comment lies: *"CS only ever runs one team so this defaults
  to 'both' and is unused on CS rows."* Remove that lie; `teams`
  applies to CS identically.
- Single-team gate (issue #148) treats CS as always single-team. That
  gate needs to flip: CS reads `teams` like DS does.
- **Files**: `config.py:288-289` (comment), `storm_signup_view.py`
  (the entire CS-single-team branch around line 105-245 — gating
  buttons, stale-button DS→CS migration text, `force_all_buttons`),
  `storm_officer_view.py:744` (single-team bucket-map),
  `storm_strategy.py:245, 744, 901, 1611` (single-team preset paths),
  `storm_history.py:377` (single-image button comment),
  `help_content.py:178` (CS-single-team claim), tests in
  `tests/unit/test_storm_signup_view.py:129-292`,
  `test_storm_strategy.py`, `test_storm_signup_post.py`,
  `test_storm_officer_view.py`.
- **Times stay differentiated**: DS slot pair and CS slot pair are
  game-defined constants — Rule A doesn't change the time math, only
  the team count.

### Rule B — "Floor" → "Minimum" in every user-facing string
- Buttons, embed labels, modal placeholders, ephemeral copy, error
  text, help-content categories. All "Floor", "floor", "below floor",
  "below-floor" become "Minimum" / "below minimum".
- Internal field names (`min_power_a`, `floor_*` variables in code)
  can stay — this is a **UI sweep**, not a refactor. But docstrings
  that surface as `/help` content or wizard prompts must change.
- **Files** (counts of "floor" matches on dev):
  - `storm_roster_builder.py` — 90 (biggest sweep; both UI strings
    and internal vars — separate user-facing from internal)
  - `storm_strategy.py` — 26 (preset editor labels)
  - `storm_attendance.py` — 6
  - `storm_history.py` — 4
  - `help_content.py` — 2
  - `setup_cog.py` — 1
  - `storm_member_rules.py` — 1
  - `storm_renderer.py` — 1
  - `storm_walkthrough.py` — 1
  - Tests: `test_storm_roster_builder.py` (60), `test_storm_history.py`
    (16), `test_storm_attendance.py` (10), `test_storm_strategy.py`
    (4). Update tests that assert UI-string contents; leave tests
    that assert internal field names alone.

### Rule C — Sheet columns are picked by letter, not by header text
- Today: `power_column_name TEXT DEFAULT ''` stores the literal header
  string ("1st Squad Power"); roster code does case-insensitive
  matches against the header row.
- Target: `power_metric_column TEXT DEFAULT 'B'` (column letter).
  At render time, fetch the header row, look up the letter's header,
  surface it as `Column B: 1st Squad Power`.
- Wizard 2.4 becomes a Keep/Default/Change view: `[Use Default: B]
  [Define your own]`. "Define your own" opens a short modal for a
  single column letter (A–Z) with validation.
- **Files**: `config.py` (schema column rename + migration —
  `power_column_name` → `power_metric_column`), `setup_cog.py`
  (`_ask_power_column` or equivalent wizard step), `storm_signup_view.py`
  (power-refresh DM read path), `storm_roster_builder.py` (roster
  power read path), tests in `test_config.py`, `test_storm_signup_view.py`,
  `test_storm_roster_builder.py`.
- **Migration**: copy old values → new column where old is non-empty
  and resolvable to a letter via the saved Sheets header lookup,
  fallback to default `'B'` otherwise. Then `ALTER TABLE … DROP
  COLUMN power_column_name`.

### Rule D — Tabs auto-create when missing
- Every tab-name prompt (Sign-Ups, Rosters, Attendance, Strategies,
  Member Rules) gets the same wording: *"The bot creates and
  maintains this tab if it doesn't exist."*
- Implementation: every code path that reads from these tabs must
  call `add_worksheet` if the tab is missing, then proceed. Today:
  inconsistent — some tabs auto-create on first write, some require
  a manual user step.
- **Files**: `config.py` (a single `get_or_create_tab(spreadsheet,
  tab_name, header_row=...)` helper next to `get_spreadsheet`), then
  callsites in `storm_signup_post.py`, `storm_roster_builder.py`,
  `storm_attendance.py`, `storm_strategy.py`, `storm_member_rules.py`.
- **Tests**: each tab's reader gets a test for the "tab missing"
  branch (currently most fail, since the spec wants them to
  auto-create instead).

### Rule E — Replace free-text with dropdowns for finite value sets
- Zones (DS/CS lock to canonical names per #35 — finite).
- Vote choices (Team A / Team B / Either / Cannot — finite).
- Member names from roster (finite, but may exceed Discord's 25 cap
  → paging).
- Consequence: every error path that handled "unparseable input",
  "non-canonical zone", "case mismatch", "member not found" becomes
  unreachable and gets removed.
- Discord modals can't host Select components → these conversions
  move the UX out of modals into view-based ephemerals.
- **Conversions**:
  - 2.14c InlinePowerBandModal → power-band view (Zone Select + Power
    TextInput hybrid — Power stays a TextInput since values are
    free-form magnitudes; build as a view that opens a tiny modal
    for the power number after the zone is picked, or a single
    view with a Select for zone + button-triggered modal for power).
  - 6.1 on-behalf vote modal → ephemeral view with Member Select +
    Vote Select + Submit + Cancel.
  - Member-rule modals (Section 13) wherever they capture a zone
    string today.
- **Files**: `storm_member_rules.py` (InlinePowerBandModal +
  `set_member_zone` modal), `storm_officer_view.py` (on-behalf vote
  modal), tests across the board.

### Rule F — Required means required (no "blank = skip" when user opted in)
- 2.9 sign-up time: if 2.7 set a poll day, the user said "yes, post
  automatically" → time can't be blank. Validate non-empty on submit.
- Today: `_ask_signup_schedule` accepts an empty `signup_time` as
  "manual posting only" (`setup_cog.py:5474+` docstring confirms).
  That whole branch goes away once 2.7 is poll-day-only and `dow >=
  0` is treated as opted-in.
- **Files**: `setup_cog._ask_signup_schedule`,
  `storm_signup_scheduler.py` (it currently treats `signup_time=''`
  as "do nothing this minute"; that becomes unreachable for guilds
  that completed setup).

### Rule G — Drop the Judicator role concept entirely
- Removes:
  - 2.12c wizard step.
  - 7.20 faction-roles offer at the end of Approve & Post.
  - Section 9.5-9.10 faction-roles cascade screens.
  - `set_member_role` subcommand from member-rule explainers and
    from `/desertstorm member_rule` + `/canyonstorm member_rule`.
- Schema: drop `judicator_role_id` from `guild_storm_config` via
  one-shot `DROP COLUMN`, drop from dataclass / `save_*` / `get_*`.
- **Files** (count of "judicator" matches on dev):
  - `setup_cog.py` — 22 (the `_ask_judicator_role` helper at 5755,
    its `_KeepOrChangeRoleGate` at 5695, and callsite at 6344;
    plus the explainer text)
  - `storm_roster_builder.py` — 30 (faction-roles offer view +
    apply logic at the end of Approve & Post)
  - `storm_member_rules.py` — 9 (the `set_member_role` subcommand
    + the per-member role rule type)
  - `help_content.py` — 3
  - `config.py` — 8 (schema column + getters/setters)
  - Tests: `test_storm_member_rules.py` (4 matches),
    `test_storm_roster_builder.py` (33). Many tests will need to
    delete entire test classes for the removed flow.

### Rule H — Event day is fixed; only ask poll day
- DS event = Friday. CS event = Thursday.
- **In-game roster lock** (newly confirmed by user, source of the
  poll-window bounds):
  - DS roster locks **Wednesday before server reset** (Thursday
    00:00 server time).
  - CS roster locks **Monday before server reset** (Tuesday 00:00
    server time).
- **Poll-day dropdown options are constrained per event type** so
  the poll posts after the prior week's event closes but before the
  in-game roster lock:
  - **DS poll-day options: Saturday, Sunday, Monday, Tuesday,
    Wednesday** (5 choices; Saturday is the day after Friday DS,
    Wednesday is the latest before Thursday-00:00 roster lock).
  - **CS poll-day options: Friday, Saturday, Sunday, Monday** (4
    choices; Friday is the day after Thursday CS, Monday is the
    latest before Tuesday-00:00 roster lock).
  - Event day itself (Friday for DS, Thursday for CS) is **not** an
    option, which neatly forbids same-day-poll-and-event.
- Wizard 2.7 asks *"What day do you want to send the poll?"* with a
  Select limited to the per-event-type options above.
- Schema: drop `event_day_of_week`, drop `signup_lead_days`, add
  `poll_day_of_week INTEGER DEFAULT -1` (–1 = manual-only, no
  auto-schedule).
- `next_event_date(poll_day, event_type)` rewrite: from today, find
  the next occurrence of the fixed event day-of-week (Fri/Thu).
  The poll's send-time on `poll_day_of_week` is whatever the user
  configured via 2.9. The roster-lock constraint is informational
  copy in the wizard, not a separate field.
- **Files**: `config.py` (schema + migration), `setup_cog.py`
  (`_ask_signup_schedule` rewrite — drop the dow Select's days, ask
  poll-day instead; drop the lead-days modal; keep the time modal
  but required per Rule F), `storm_date_helpers.py` (rewrite of
  `next_event_date`), `storm_signup_scheduler.py` (use new field),
  `storm_signup_post.py` (default-date inference), `storm_officer_view.py`
  (signups header date), `storm_strategy.py` (any preset-date
  inference), tests in `test_storm_date_helpers.py`,
  `test_storm_signup_scheduler.py`, `test_storm_signup_post.py`,
  `test_storm_officer_view.py`, `test_config.py`.

### Rule I — Slash-command references use the right event-type
- Every hardcoded `/desertstorm …` in CS-variant copy → `/canyonstorm …`.
- The walkthrough's edited screens consistently note this; verify
  every error-string callsite parameterises on `event_type`.
- **Files**: `help_content.py`, `storm_strategy.py`,
  `storm_member_rules.py`, `storm_walkthrough.py`, plus everywhere
  the screens render a "run /<cmd> to retry" hint.

### Rule J — Builder button labels stay identical across variants
- Flat (7.8), phase-aware (7.23), paired (7.24) all use the same
  button labels:
  - `👁️ Show members below minimum`
  - `↩️ Remove current zone assignees`
  - `🪑 Add all unassigned to Subs`
  - `🖼️ Generate DS/CS assignments image`
- Same row order, same emoji, same wording.
- **Files**: `storm_roster_builder.py` view classes (flat builder
  view + phase-aware view + paired-mode view).

### Rule K — Drop 🔄 Sub activated from attendance status (UI-only)
- Source: Decision #5. Attendance status surface is `✅` / `❌` / `—`
  only. The `sub_activated` value disappears from `_STATUS_LABELS`,
  the status picker (which itself gets removed per Decision #5), the
  footer counts, and the history rendering.
- **Schema**: the `sub_activated` value in the attendance Sheet column
  (if any prior alliance recorded it) becomes orphaned data — reader
  code should tolerate it (render as `—`) but writer code stops
  producing it. No DROP COLUMN needed; the column is text-cell.
- **Files**: `storm_attendance.py` (status enum, view, footer render,
  save path), `storm_history.py` (history footer + per-slot glyph
  resolution), tests in `test_storm_attendance.py` and
  `test_storm_history.py`.

### Rule L — Per-zone-per-phase rendering when `phase_count >= 2`
- Source: Decision #10. Phase-aware rendering breaks each zone into
  its own header, then one indented line per phase. Replaces the
  `(P1: 4/4, P2: 4/4, P3: 4/4)` inline format with:
  ```
  ✅ Power Tower:
      Phase 1: 4/4 — Alice, Bob, Carol, Dan
      Phase 2: 4/4 — Alice, Bob, Carol, Dan
      Phase 3: 4/4 — Alice, Bob, Carol, Dan
  ```
- `📊 Filled` line breaks out per-phase counts:
  `P1: 30/30, P2: 30/30, P3: 30/30`.
- **Surfaces**: auto-fill summary (`storm_roster_builder._render_builder_embed`),
  preset editor flat-vs-phase variants (`storm_strategy._render_editor_embed`),
  history phase-aware variant (new — `storm_history`).
- **Files**: `storm_roster_builder.py`, `storm_strategy.py`,
  `storm_history.py`, tests across each.

### Rule M — Inline actions in list views
- Source: Decision #11. Listing surfaces always render the summary
  *plus* actionable buttons. No dead-end summaries.
- `/desertstorm strategy list` + `/canyonstorm strategy list`: summary
  embed + `[➕ Create]` `[✏️ Edit]` `[🗑️ Delete]` row (Edit/Delete open
  a preset Select; Create opens the same modal as the create slash
  command).
- `/desertstorm member_rule list` + `/canyonstorm member_rule list`:
  current per-rule `[🗑 Clear N]` buttons stay; add `[➕ Add rule]`
  row that opens the same `InlinePowerBandModal` 2.14c will use, or
  redirects to the slash command for per-member rules. Empty state
  (Screen 13.27) also surfaces the `[➕ Add rule]` button.
- **Files**: `storm_strategy.py` (`_StrategyGroup._list` cog method
  + new `_StrategyListView`), `storm_member_rules.py` (list view + new
  add-rule button).

### Rule N — Walkthrough tour available for both DS and CS
- Source: Decision #12. The first-run tour offered today on
  `/desertstorm signups` also fires on `/canyonstorm signups`.
  Per-officer dismissal state keys on a walkthrough key (`storm_signups_v1`
  today); the CS tour shares the same key — dismissing on DS dismisses
  on CS for that officer, and vice versa.
- Tour-step copy branches on event type where relevant:
  - Step 5 (`14.10`) Set-up button copy: DS shows `🅰️ Set up Team A
    / 🅱️ Set up Team B`; CS shows the same when `teams=both`, single
    Set-up button when `teams=A` or `teams=B` only.
  - Step 4 (`14.9`) on-behalf vote: rewrite per Decision #2's new
    ephemeral view + Member/Vote selects + paging.
- **Files**: `storm_walkthrough.py` (`maybe_offer_storm_signups_tour`,
  `_TourStep` content per step), `storm_officer_view.py` (CS cog
  invokes the same `maybe_offer` helper).

---

## 2 — Section-by-section spec edits (sections 1-7)

Each entry below is **a change to land in BOTH the walkthrough doc
AND the code on dev**. The walkthrough edit is the easy part — the
code edit is most of the work.

### Section 1 — Auto sign-up post + voting + power-refresh DM

- **1.1 Variant C (CS)** (`storm_signup_view.py:105+`): Replace
  single-button CS with the same two-variant shape as DS — `teams=both`
  CS = 4 buttons (Team A, Team B, Either, Cannot) at CS time pair;
  `teams=A` and `teams=B` mirror DS Variant B with CS times. (Rule A)
- **1.3a stale-CS post** (`storm_signup_view.py:232+`): Rewrite to
  mirror 1.3b — "alliance currently configured as single-team" copy.
  Drop the "before CS switched to single-team format" framing
  entirely. (Rule A)
- **1.4 power-refresh DM** (`storm_signup_view.py`): Body wording is
  canonical per the EDITED doc. Confirm the `Your`/`My` stripping
  logic stays, plus update to read header via column letter (Rule C).

### Section 2 — Setup wizard (`setup_cog.py`)

- **2.1 CS re-entry summary**: Remove the *"no Teams field is
  included because CS is always single-team per faction"* comment +
  the suppressed-Teams-field code path; CS re-entry shows Teams
  whenever `teams != 'A' or 'B'`. (Rule A)
- **2.4 Power metric column**: Rewrite as Rule C — Keep/Use-default/
  Change view with letter modal. Rename schema column to
  `power_metric_column`.
- **2.5 Sub mode**: Already canonical in doc. Verify code at
  `_run_structured_flow_setup_step` matches.
- **2.6 Sign-up channel**: Already canonical in doc. Verify code.
- **2.7 Event day-of-week → Poll day-of-week**: Rewrite
  `_ask_signup_schedule`'s `_DowView` prompt to "What day do you want
  to send the poll?" Field becomes `poll_day_of_week`. (Rule H)
- **2.8 Lead days**: **DELETE** the entire screen + the lead-days
  modal step in `_ask_signup_schedule`. Drop `signup_lead_days`. (Rule H)
- **2.9 Sign-up time**: Drop "blank = manual" affordance. Modal is
  required when `poll_day_of_week >= 0`. Inline error on submit if
  empty. (Rule F)
- **2.10 / 2.11 / 2.12 tab prompts**: Update copy to "The bot creates
  and maintains this tab if it doesn't exist." + thread a shared
  `get_or_create_tab` helper through the readers. (Rule D)
- **2.12c Judicator role**: **DELETE** `_ask_judicator_role` and
  `_KeepOrChangeRoleGate`. Drop callsite at line 6344. Drop
  `judicator_role_id` from schema. (Rule G)
- **2.12d Power-refresh DM**: Reword embed to reference the
  column-letter-with-fetched-header pattern: *"Send a heads-up DM
  when a voter's **Column B: 1st Squad Power** on the roster Sheet
  is blank or unparseable."* (Rule C)
- **2.13a Strategy Presets explainer**: Canonical in doc.
- **2.13 Strategy Presets tab**: Rule D wording sweep.
- **2.14a Member Rules explainer Variant B (CS)**: Rewrite to mirror
  DS exactly — same bullets, same example structure, swap
  `desertstorm` → `canyonstorm`. **Add** `set_member_team` to the
  subcommand list (Rule A). **Drop** `set_member_role` from both
  variants (Rule G).
- **2.14 Member Rules tab**: Rule D wording sweep.
- **2.14c InlinePowerBandModal**: Convert Zone field to a Discord
  Select via Rule E pattern. Remove "non-canonical zone" and "empty
  zone" submit-error variants.
- **2.15 Inline post-first-signup offer**: Canonical in doc.

### Section 3 — `/members sync`

- **3.4 Sync running**: To customise the "is thinking…" indicator
  (per the EDITED copy "is working..."), send a placeholder ephemeral
  immediately after `defer()`. Discord's native defer-ack copy can't
  be re-worded.
- **3.6 Sync failure**: Canonical in doc.

### Section 4 — Manual post_signup

- No marked feedback. Knock-on:
  - Any copy that surfaces "auto-posts every N days" reframes to
    "auto-posts every <poll_day_of_week>".
  - CS variant must reflect Teams A & B options when `teams=both`.

### Section 5 — Officer view (`storm_officer_view.py`)

- **5.8 CS bucket-map**: Add two-set-up-button variant for `teams=both`
  CS — currently single-button assumes single-team. (Rule A)
- **5.15 Truncated buckets**: Canonical, but missing-word typo —
  *"use the filter dropdown see all votes"* should be *"use the
  filter dropdown to see all votes."* Fix.
- **5.17 Set-up Team A → preset picker**: CS variant points at
  `/canyonstorm strategy create` not `/desertstorm strategy create`
  in the empty-presets error. (Rule I)
- **5.21 First-run walkthrough offer**: Add third button (`[Ask me
  again next time]`). Fix typo `/deserstorm signups` →
  `/desertstorm signups`. New flag value distinct from `dismissed=True`.

### Section 6 — On-behalf vote (`storm_officer_view.py`)

- **6.1**: Rewrite per decision #2 — replace the modal with an
  ephemeral view:
  - `Member` Select sourced from roster (first 25 + paging if more).
  - `Vote` Select with options exactly matching sign-up buttons
    (Team A: <time>, Team B: <time>, Either time works, Cannot
    participate) using `storm_slot_labels`.
  - `[Submit]` and `[Cancel]` buttons.
  - On Submit, write the vote and ack with 6.6 success copy.
- **6.2, 6.3, 6.4**: **DELETE** all three (Rule E).
- **6.5 Vote-write failure**: Keep.
- **6.6 Success**: Canonical.
- **6.7 Case-normalisation success**: **DELETE** (Rule E).
- **6.8 Permissive fallback**: Rewrite — if roster read fails, show
  "Couldn't read the roster right now; try `/members sync` and try
  again" error ephemeral instead of opening the picker. No free-text
  fallback.

### Section 7 — Roster builder (`storm_roster_builder.py`)

- **7.2 Preset-not-found**: Add `[List saved strategies]` button.
  On click, ephemeral shows the same list as `/<parent> strategy list`.
- **7.3 Preset has no zones**: Add `[Edit <preset name>]` button.
  On click, opens the preset editor seeded with the named preset.
- **7.8 Builder embed**: Rule B sweep + confirm canonical button
  labels.
- **7.9 Builder embed (after assignments)**: Mirror 7.8. Rule B.
- **7.10 Member picker dropdown**: Rule B. Update placeholder
  "No eligible members — toggle below-floor override" to "…toggle
  below-minimum override".
- **7.12 Below-floor toggle**: Canonical.
- **7.13 Unassign-zone**: Canonical button label `↩️ Remove current
  zone assignees`.
- **7.14 Last-to-subs → Add all unassigned to Subs**: Rewrite the
  body description (decision #1). New copy: *"Moves every member
  on this team's available pool who isn't already assigned as a
  primary into the subs pool. Subs have no minimum power filter."*
  Implementation: source = the team's available-pool (officer-
  picked members); exclude = anyone with a non-empty current zone
  other than subs; write the remainder into the subs zone. Drop
  the per-zone last-added logic. (Per the user's clarification:
  subs have no minimum, so no power filtering applies here.)
- **7.17 Render image**: Canonical.
- **7.20 Faction-roles offer**: **DELETE** the entire screen + the
  `_FactionRolesView` class + the apply-roles cascade. (Rule G)
- **7.23 Phase-aware variant**: Rule J — replace short labels with
  canonical 7.8 labels. Rule B for "floor" stragglers.
- **7.24 Paired-mode variant**: Replace auto-prompt-after-each-primary
  with a single `[🔁 Pair subs]` button. Click opens ephemeral view
  with:
  - Running pair list at top (`Primary → Sub` rows, blank until paired).
  - `Primary` Select (currently-assigned primaries).
  - `Sub` Select (available unpaired subs).
  - `[Assign pair]` and `[Done]` buttons.
  - Each Assign pushes the pair into the list and clears both
    Selects.
  - Explicit copy: *"You have 10 subs and up to 20 primaries — not
    every primary will get a sub."* Drop "complete for every primary"
    success line.
- **7.25 Paired sub picker**: Folds into 7.24's ephemeral; remove
  the standalone auto-prompt trigger.
- **7.27 Roster-error warnings**: With `set_member_role` rules
  dropped (Rule G), audit unmatched-rule-name strings to remove role
  references.

---

## 3 — Sections 8-15 sweep (rule applications, line-by-line)

Line numbers reference the edited walkthrough on dev
(`docs/PREMIUM_STORM_UX_WALKTHROUGH_edited.md`). All findings here
need to flow into both the doc AND the code on dev.

### Section 8 — Auto-fill summary (~lines 4087-4429)
Rule B sweep + Decisions 7, 8, 9, 10, 14.
- **Rule B**: every "floor" / "band-relaxed floor" / "below floor"
  in summary copy → "minimum" / "below minimum". (L4104, L4117,
  L4225-4226, L4249-4257, L4287, L4322, L4361 + Screen 8.9 body).
  Power-band relaxation copy reword (per your 8.4 edit): drop
  "_(preset floor 300M relaxed by power_band rule)_"; add a
  free-standing line *"This zone has a power band rule of 300M that
  is currently being overridden by the local power minimum."*
- **Decision #7 (per-member rules silently no-op)**: drop the
  `per_member subject not on roster: <subject>` conflict shape from
  `_auto_fill_session` entirely. A rule whose subject isn't in
  tonight's roster ⇒ nothing to apply ⇒ nothing reported. Other
  conflict shapes (`unknown zone`, `full when pinning`, `pinned to
  multiple zones`) stay.
- **Decision #8 (show all)**: drop the `(+N more)` truncation on
  `Gaps (power unknown, not slotted)` and `Conflicts`. Officers
  need the full list to act on it. (L4163-4172 gaps shape, L4204-4210
  conflicts shape.)
- **Decision #14 (auto-pair listing)**: replace `• Auto-paired
  subs: **N**` with an explicit list — `• Auto-paired subs: Alice ↔
  Bob, Carol ↔ Dan, Erin ↔ Frank, …`. The count is preserved as a
  prefix or the line stays unbulleted-per-pair.
- **Decision #10 + Rule L (per-zone-per-phase)**: Screen 8.6's
  phase-aware auto-fill summary now lists each phase's members on
  its own line under the zone header. `📊 Filled` line breaks into
  per-phase counts (`P1: 30/30, P2: 30/30, P3: 30/30`).
- **Decision #9 (confirm destructive)**: Screen 8.8 — clicking 🎯
  Auto-fill while a session has manual edits opens a confirm
  ephemeral first ("This will reset every assignment, sub pairing,
  and override on this team. Confirm?"). New sessions skip the
  prompt.
- **Code**: `storm_roster_builder.py` — `_auto_fill_session`
  (drop the subject-not-on-roster conflict + drop truncation), the
  summary string renderer (auto-pair listing, per-phase break-out),
  the auto-fill button callback (confirm gate when
  `session.has_manual_edits`).

### Section 9 — Approve & Post + faction roles (~lines 4453-5138)
The big Rule G deletion + a Rule I sweep.
- **L4468-4471**: Faction-roles trigger condition references
  `judicator_role_id` — entire condition block goes. (Rule G)
- **L4801-5067, Screens 9.5–9.10 inclusive (~266 lines)**: DELETE.
  Includes:
  - 9.5 Faction-roles offer (CS-only, Rulebringers/Dawnbreakers
    buttons)
  - 9.6 Permission-preflight failures (role-position / Manage Roles)
  - 9.7 Apply-summary (per-member role apply results)
  - 9.8 Dawnbreakers acknowledgement
  - 9.9 Officer-only guard on the offer view
  - 9.10 Faction-roles offer timeout
  (Rule G)
- **L4545, 4569, 4582, 4595, 4607-4609, 4613-4616**: hardcoded
  `/setup → ⚔️ Desert Storm` in error copy — CS variants must say
  `/setup → 🏜️ Canyon Storm`. (Rule I)
- **L4560-4561**: "Run `/setup → ⚔️ Desert Storm`" needs event-type
  substitution. (Rule I)
- **L4490-4491, 4506-4507**: Public-ack button labels
  `[📄 Mail preview (disabled)]` and `[🖼️ Render PNG (disabled)]` —
  verify against canonical Rule J labels (especially `🖼️` → should
  read `🖼️ Generate <DS/CS> assignments image`).
- **Code**: `storm_roster_builder.py` — delete `_FactionRolesView`
  class, the post-approve cascade, and all permission/apply paths.

### Section 10 — Attendance (~lines 5141-5781)
Major UI refactor — Decisions 5, 6 + Rules A, B, C, D, I, K.
- **Decision #5 (UI pattern)**: replace today's flow (dropdown →
  click slot → status-picker ephemeral with 4 buttons → ack
  ephemeral) with: single dropdown + persistent `[✅ Attended]` /
  `[❌ Did Not Attend]` / `[💾 Save attendance]` row. When no
  member is selected, the buttons read `Mark all as attended /
  did not attend` (bulk over unrecorded slots). When a member is
  selected, they read `Mark as attended / did not attend` and
  write directly to that member.
- **Decision #5 (drop 🔄 + status picker)**: delete
  `_StatusPickerView` entirely. Screens 10.13, 10.14, 10.15, 10.16,
  10.17 are no longer reachable. Status picker timeout (10.17)
  goes. Bulk-mark button (10.18) becomes the no-selection state of
  the always-visible attendance buttons.
- **Rule K (drop 🔄 Sub activated)**: footer counts collapse from
  `✅ N · ❌ N · 🔄 N · — N` to `✅ N · ❌ N · — N`. The
  `sub_activated` value disappears from `_STATUS_LABELS`. Sheet
  reader tolerates orphaned `sub_activated` rows (renders as `—`).
- **Decision #6 (drop override marker)**: remove the per-slot ⚠️
  glyph and the trailing `_⚠️ Assigned below the zone floor at
  build time._` line. Internal `Override Below Floor` Sheet column
  stays (the builder still records it at assignment time).
- **Phase-aware variant (per your 10.8 NOTE)**: when the roster is
  phase-aware, the per-slot line shows the comma-separated list of
  zones across phases instead of one zone per row.
- **10.11 empty state**: hide the action buttons entirely — no
  selectable members ⇒ no actions to take.
- **Rule A (CS teams)**: Screen 10.9 needs a `teams=both` variant
  rendering `**Team A**` / `**Team B**` headers just like DS.
- **Rule I (CS slash refs)**: L5245-5252 (10.6),
  L5278-5280 / L5292-5293 (10.7a/b), Screen 10.4 — every
  CS-variant error pointing at `/desertstorm …` must say
  `/canyonstorm …`.
- **Rule B**: L5328 "below the zone floor at build time" → drop
  entirely per Decision #6.
- **Rule D**: attendance-tab-creation copy follows
  "creates and maintains".
- **Rule C**: any power-column read in attendance uses the
  letter-based field.
- **Code**: `storm_attendance.py` — full view rewrite (single
  member-select + persistent action buttons + bulk-vs-individual
  branching on selection state), delete `_StatusPickerView`, status
  enum trimmed, footer renderer simplified, override-marker
  rendering removed. Tests in `test_storm_attendance.py`
  rewritten extensively.

### Section 11 — Strategy commands (~lines 5785-6206)
Decision #11 (inline list actions, Rule M) + Rule I sweep.
- **Decision #11 / Rule M (inline actions)**: Screen 11.5 today
  posts the preset summary embed and stops. Rewrite to also post a
  view with `[➕ Create]` `[✏️ Edit]` `[🗑️ Delete]` buttons. Edit
  and Delete open a preset Select (single-pick) that hands off to
  the editor / delete-confirm flow. Create opens the same modal as
  `/desertstorm strategy create`. Empty state (11.5a) shows the
  same buttons with just Create enabled.
- **Rule I (full command names, not shorthands)**: L5843, L5850,
  L5876 reword `/ds strategy edit` and `/cs strategy edit` to
  `/desertstorm strategy edit` / `/canyonstorm strategy edit`.
- **Rule I (event-type wiring)**: L6128-6129 confirm
  `open_roster_builder(interaction, "DS", name)` vs
  `event_type="CS"` for the CS variant; L5851-5857 / L5880-5884
  confirm `seed_default_preset("Standard DS", "DS")` /
  `seed_default_preset("CS Standard", "CS")` are wired to their
  respective parents.
- **Code**: `storm_strategy.py` cog dispatch + new
  `_StrategyListView` class for the inline actions.

### Section 12 — Strategy preset editor (~lines 6210-7191)
Polish pass + Decisions 10, 13.
- **Decision #13 (zones are game-defined)**: DELETE the
  `[➕ Add zone]` button + `_AddZoneModal` class + Screen 12.15.
  Zones come exclusively from `DS_ZONE_STRUCTURE` /
  `CS_ZONE_STRUCTURE`. Officers configure max-players / minimum
  power / priority for the canonical zones only.
- **Decision #10 / Rule L (per-zone-per-phase rendering)**: Screen
  12.4 — phase-aware editor breaks each zone into its own line plus
  one indented line per phase, instead of the inline
  `(P1: 4, P2: 4, P3: 4)` shape. Screen 12.3 — flat CS editor's
  zone list is currently flooded (22 canonical zones). Surface
  phase-mode toggle prominently so officers don't try to scroll
  through every phase's zone at once.
- **Button labels (your 12.1 edits)**: `[✏️ Rename]` →
  `[✏️ Rename preset]`; `[🔙 Abandon]` →
  `[🔙 Abandon this preset]`. Footer reword: "*Unsaved changes —
  hit Save Preset to commit.*" → "*Unsaved changes — Save preset
  to save your changes.*"
- **Mode-toggle copy (your 12.5/12.6 edits)**:
  "Stored capacities + assignments are kept — flip back any time
  without data loss." →
  "Capacities + assignments are kept. Re-select **N-phase** mode
  to restore without data loss."
- **Apply-to-similar copy (your 12.14 edits)**:
  "Pick any to copy these same settings to, or skip." →
  "Would you like to apply the same settings to these as well?"
  `[ ▾ Choose siblings to apply to… ]` →
  `[ ▾ Select zones ]`.
- **Phase-aware 3-modal wizard (your 12.9 question)**: shipped as
  a 2-page collapse. Field-count math at the 5-component Discord
  cap: capacity + priority on page 1 would be 3 + 3 = 6 fields
  for 3-phase, so the originally-floated partition isn't viable.
  Capacity + minimums on page 1 is 3 + 2 = exactly 5 (worst case:
  DS both teams + 3 phases) and leaves priority on page 2 (up to
  3 fields). Implementation lands the new
  `_ZonePhaseCapacityAndFloorsModal` and removes the old floors
  modal; the priority modal is unchanged.
- **Phase-mode dropdown labels**: drop the redundant "Yes — ":
  `Flat (no phases)` / `2 Phases` / `3 Phases`.
- **Code**: `storm_strategy.py` — drop Add Zone class + button,
  re-render phase-aware editor per Rule L, button-label sweep,
  mode-toggle copy reword, phase-wizard collapsed to 2 pages.

### Section 13 — Member rules (~lines 7195-7983)
Biggest cluster of Rule A + Rule G + Rule E work, plus Rule M
inline-actions for the list view.
- **Rule A (CS has teams)**:
  - **L7202-7207** — "CS group exposes four (no `set_member_team` —
    Canyon Storm doesn't have A/B teams)" directly contradicts
    Rule A. CS supports `teams=both/A/B`; the CS member_rule group
    must include `set_member_team`. Update both the doc claim and
    the actual CS command tree.
  - **Screen 13.17** ("set_member_team on CS group is rejected"):
    guard is rewritten to gate on `cfg.teams` (rejected only when
    `teams != 'both' and teams != 'A' and teams != 'B'` — i.e. an
    untouched alliance), or drops entirely. The blanket "CS doesn't
    have teams" framing goes.
- **Rule G (drop Judicator)**:
  - **Screens 13.23, 13.24, 13.25** (`set_member_role` slash help
    + success + validation): DELETE all three. The whole
    subcommand goes.
  - **🎖️ list-rendering branch** (L7670, L7714-7715, L7722, L7750,
    L7807, L7936): strip every `🎖️ <name> → Judicator/Commander
    candidate` row from `list` output. The `special_role` rule
    type goes away.
  - **`MemberRoleRule` dataclass + storage**: delete from
    `storm_member_rules.py`. The Sheet column survives for legacy
    rows but the loader filters them out.
- **Rule E (dropdown for zones)**:
  - **Screen 13.3** ("`Power Towr` isn't in the canonical zone
    list … saved anyway") — unreachable with autocomplete. DELETE.
  - **Screen 13.5** (blank zone validation) — unreachable. DELETE.
  - **Screen 13.20** ("`Powr Tower` isn't in the canonical zone
    list … saved anyway") — unreachable. DELETE.
  - **Screen 13.21** (blank zone) — unreachable. DELETE.
  - Implementation: the `zone` slash-command parameter gets an
    autocomplete callback returning `DS_ZONE_STRUCTURE` or
    `CS_ZONE_STRUCTURE` per parent. Free-text input still
    technically possible (Discord autocomplete is suggestions, not
    enforcement) — keep a soft non-canonical warning as a fallback
    if a typo somehow lands, or treat the autocomplete as
    authoritative and reject non-canonical at submit.
- **Decision #11 / Rule M (inline actions)**:
  - **Screen 13.27** (empty rules list) + **Screen 13.28**
    (populated list) — add `[➕ Add rule]` row in addition to the
    existing `[🗑 Clear N]` buttons. Add-rule opens the same
    `InlinePowerBandModal` 2.14c will use for power-band rules,
    or surfaces a follow-up choice (power-band via modal vs. per-
    member via slash) for the per-member rule types.
- **Code**: `storm_member_rules.py` — delete `set_member_role`
  subcommand + `MemberRoleRule` dataclass + `🎖️` list-render
  branch + 4 unreachable validation screens; add `set_member_team`
  to the CS group's app-command tree; add zone-autocomplete
  callback; add the inline `[➕ Add rule]` button to the list
  view.

### Section 14 — Walkthrough tour (~lines 7986-8371)
Decision #12 (tour fires for CS) + Rule N + step content rewrites.
- **Decision #12 / Rule N (tour for CS)**: today
  `maybe_offer_storm_signups_tour` is called only from
  `/desertstorm signups`. Add the same call from
  `/canyonstorm signups`. Walkthrough key stays
  `storm_signups_v1` — per-officer dismissal applies across both
  variants. Step copy branches on the invoking event_type where
  relevant.
- **Screen 14.6 (Step 1 / 6)**: legend mentions `🅰️ Team A, 🅱️ Team
  B`. Confirm the tour function reads `cfg.teams` (and event_type)
  so a CS-with-teams=both alliance's officer sees the A/B legend,
  not a Roster legend. (Rule A)
- **Screen 14.9 (Step 4 / 6)**: copy describes the old on-behalf
  vote modal — *"type the member's roster name (it must match the
  Sheet exactly — typos are rejected), and pick A / B / Either /
  Cannot."* OUT OF DATE per Decision #2. Rewrite to describe the
  new ephemeral view: Member Select sourced from roster (first 25
  + paging beyond), Vote Select with Team-A/B/Either/Cannot
  options, Submit + Cancel. The "typos are rejected" framing goes.
- **Screen 14.10 (Step 5 / 6)**: *"click 🅰️ Set up Team A or 🅱️ Set
  up Team B (Desert Storm) — or 🏜️ Set up Roster (Canyon Storm;
  one roster per faction)."* CS portion must branch on `teams`:
  CS with `teams=both` shows A/B; CS with `teams=A` or `teams=B`
  shows the matching single button. (Rule A)
  Also: "eligibility floors enforced" → "eligibility minimums
  enforced" (Rule B).
- **Rule G**: Confirm no Judicator step exists in the tour. (Likely
  already absent.)
- **Code**: `storm_walkthrough.py` (tour-step content branching),
  `storm_officer_view.py` (CS cog invokes the same `maybe_offer`
  helper).

### Section 15 — History browser (~lines 8375-8867)
Rule A correction + Decision #6 (drop override marker) + Rule K
(drop 🔄) + open question on phase-aware history.
- **Rule A (Screen 15.18 CS parity)**: "Single-roster events (CS
  doesn't have A/B teams) render the field as 'Roster'" directly
  contradicts Rule A. CS with `teams=both` should render
  `── Team A ──` / `── Team B ──` (mirroring DS Screen 15.8). Add a
  `teams=both` CS variant; the existing single-`Roster` rendering
  becomes the `teams=A` / `teams=B` variant.
- **Decision #6 (drop override marker)**: Screen 15.8 L8534
  example `✅ Erin — 300M ⚠️ override` — drop the `⚠️ override`
  rendering. The entire `⚠️ override` flag description at L8559
  goes. (The internal `Override Below Floor` Sheet column survives;
  history just doesn't surface it.)
- **Rule K (drop 🔄 Sub activated)**: footer
  `Attendance: ✅ 4 · ❌ 1 · 🔄 1 (recorded 6 of 7 slots)` →
  `Attendance: ✅ 4 · ❌ 1 (recorded 5 of 7 slots)`. Same fix on
  Screens 15.13 and 15.18.
- **Phase-aware history rendering (open question)**: Section 15
  doesn't address phase-aware events at all. With Rule L
  (per-zone-per-phase rendering), history detail of a phase-aware
  event needs the same shape: each zone header, then per-phase
  member lines. Today's renderer collapses everything into a flat
  zone list. Worth a sub-task for the history phase-aware variant
  spec + implementation.
- **Rule J**: any builder/render buttons surfaced from history must
  match canonical labels.
- **Code**: `storm_history.py` — Rule A team grouping for CS,
  override-marker rendering removed, footer 🔄 removed, phase-aware
  variant added.

### Cross-section summary

| Rule | Count of locations / scope |
|---|---|
| Rule A (CS teams) | §13 L7202-7207 contradiction (add `set_member_team` to CS group); §14 Screens 14.6 + 14.10 tour copy branching on `cfg.teams`; §15 Screen 15.18 missing CS-both-teams variant; §10 attendance per-team rendering |
| Rule B (floor → minimum) | ~20 lines in §8; rendered in attendance L5328 (but L5328 drops entirely per Decision #6); §12-§15 mostly clean |
| Rule C (column letter) | §10 attendance power-column read |
| Rule D (tab auto-create) | §10 attendance-tab wording |
| Rule E (dropdowns) | §13 zone fields — autocomplete-or-Select on `set_member_zone` + `set_power_band` zone params; Screens 13.3, 13.5, 13.20, 13.21 DELETED as unreachable |
| Rule G (drop Judicator) | §9 Screens 9.5–9.10 (266 lines DELETED); §13 Screens 13.23–13.25 DELETED + 🎖️ rendering branch removed + `set_member_role` subcommand dropped + `MemberRoleRule` dataclass deleted |
| Rule I (CS slash refs) | §9 (5+ locations), §10 (3+ locations), §11 (3 locations including `/ds` shorthand fixes) |
| Rule J (button-label parity) | §9 public-ack buttons (`📄 Mail preview`, `🖼️ Render PNG`) need canonical labels; §15 history-browser action buttons |
| Rule K (drop 🔄 Sub activated) | §10 attendance status/footer/picker removal; §15 history footer counts |
| Rule L (per-zone-per-phase rendering) | §8 auto-fill summary line shape; §12 preset editor phase-aware variants (Screen 12.4); §15 history phase-aware variant (new) |
| Rule M (inline list actions) | §11 strategy list (Screen 11.5); §13 member_rule list (Screens 13.27 + 13.28) |
| Rule N (tour for both DS and CS) | §14 — invocation from `/canyonstorm signups` + tour copy branching on event_type + `cfg.teams` |
| Decision #2 (on-behalf vote view) | §14 Screen 14.9 outdated copy — rewrite |
| Decision #4 (poll-day model) | No §8-§15 surfaces reference `event_day_of_week` / `signup_lead_days` directly — Rule H is confined to §1-§2 |
| Decision #5 (attendance UI) | §10 entire attendance view rewrite (Screens 10.8–10.18) |
| Decision #6 (drop override marker) | §10 attendance per-slot + footnote removal; §15 Screen 15.8 + L8559 |
| Decision #7 (rules silently no-op) | §8 auto-fill summary conflict shapes |
| Decision #8 (show all entries) | §8 auto-fill summary truncation removal |
| Decision #9 (confirm destructive) | §8 Screen 8.8 — auto-fill re-run confirm |
| Decision #10 (per-zone-per-phase) | See Rule L |
| Decision #11 (inline list actions) | See Rule M |
| Decision #12 (CS tour) | See Rule N |
| Decision #13 (zones game-defined) | §12 Screen 12.15 + `[➕ Add zone]` button DELETED |
| Decision #14 (auto-pair listing) | §8 auto-pair summary line breaks pairs explicit |

---

## 4 — Schema migration plan (consolidated)

All schema changes implied by the rules / decisions above. These
ride a single release branch when implementation is batched. Apply
inside `config.init_db()` per the established migration block
pattern (try/except, log `[CONFIG] Added X to Y`).

### `guild_storm_config`

**Drop columns** via one-shot `ALTER TABLE … DROP COLUMN`:
- `event_day_of_week` (Rule H)
- `signup_lead_days` (Rule H)
- `judicator_role_id` (Rule G)
- `power_column_name` (Rule C — after migration to new column)

**Add columns** via idempotent `ALTER TABLE … ADD COLUMN`:
- `poll_day_of_week INTEGER DEFAULT -1` (Rule H; –1 = manual posting only)
- `power_metric_column TEXT DEFAULT 'B'` (Rule C — letter)

**Existing columns kept as-is**: `tab_name`, `mail_template`,
`templates_json`, `default_template`, `timezone`, `log_channel_id`,
`post_channel_id`, `dm_reminder_message`, `teams`,
`structured_flow_enabled`, `sub_mode`, `signup_channel_id`,
`signup_schedule_cron`, `signups_tab`, `rosters_tab`, `attendance_tab`,
`strategies_tab`, `member_rules_tab`, `signup_time`,
`power_refresh_dm_enabled`.

**Drop the `teams` inline-comment lie** at `config.py:288-289` — *"CS
only ever runs one team so this defaults to 'both' and is unused on
CS rows."* Replace with a comment that reflects CS-has-teams reality.

**Data migrations**:
- `power_column_name` → `power_metric_column`: read existing
  `power_column_name`. If empty, set new column to `'B'`. If non-empty,
  fetch the saved Sheet's header row, find the matching column letter,
  store it. Fallback to `'B'` on any failure. Log per-guild.
- `event_day_of_week` + `signup_lead_days` → `poll_day_of_week`:
  compute `poll_day_of_week = (event_day_of_week - signup_lead_days) %
  7` for guilds with both set. Guilds with `event_day_of_week == -1`
  (manual opt-out) → `poll_day_of_week = -1`.

After migration runs successfully across the dev Discord app's guilds,
the DROP COLUMNs go in. Per CLAUDE.md, prod SQLite supports `DROP
COLUMN` (3.35+ confirmed on Railway).

---

## 5 — Open items / verify before implementation

- **Modal → view rewrites (2.14c, 6.1, 7.24)** — ✅ RESOLVED.
  - 2.14c: View with Zone Select → "Set minimum power" button →
    modal with one Power TextInput.
  - 6.1: Ephemeral view with Member Select + Vote Select + Submit/
    Cancel; **Prev / Next paging** with `Page X / Y` indicator when
    roster > 25.
  - 7.24: Single Primary-picker + Sub-picker pair, running pair
    list with 🔄 unpair affordance, write-on-Assign, hides already-
    paired primaries and subs.
- **Same-day poll/event** — ✅ RESOLVED by Rule H's per-event-type
  poll-window: event days are excluded from the poll-day dropdown,
  so same-day is impossible by construction.
- **DS time slots on dev** — ✅ confirmed correct
  (`DS_SERVER_TIMES = [(18, 0), (23, 0)]` = 4pm EDT / 9pm EDT).
- **CS time slots on dev** — ✅ confirmed correct
  (`CS_SERVER_TIMES = [(12, 0), (23, 0)]` = 10am EDT / 9pm EDT).
- **Walkthrough doc time-label sweep** — Every example time label
  in `PREMIUM_STORM_UX_WALKTHROUGH_edited.md` needs auditing
  against the confirmed constants. Known mismatches:
  - DS Variant A: `Team A: 9pm ET (18:00 server time)` — wrong;
    18:00 server = 4pm EDT, not 9pm. Should be either
    `Team A: 4pm EDT (18:00 server time)` or
    `Team A: 9pm EDT (23:00 server time)` depending on which slot
    Team A maps to.
  - DS Variant A: `Team B: 4pm ET (13:00 server time)` — wrong;
    13:00 isn't a DS slot.
  - CS Variant C: `Team A: 4pm ET (13:00 server time)` — wrong;
    CS slots are 12:00 and 23:00 server (10am EDT / 9pm EDT).
- **7.14 bulk behaviour** — ✅ RESOLVED. Subs have no minimum
  filter. Behaviour: every member the officer picked onto this
  team's available pool who is **not** currently assigned as a
  primary on this team → assigned as a Sub. Implementation:
  iterate the team's available pool (the source of truth for who
  the officer has tagged for this team), exclude anyone whose
  current zone is non-empty + not the subs zone, write the rest
  into the subs zone.
- **`force_all_buttons` debug knob** — ✅ RESOLVED: **remove it**
  with Rule A's branch. The single-team gate it backstopped is
  going away.

### TBD — pending game-mechanics or external verification

- None at this time.

---

## 6 — Implementation rollout

Each cross-cutting rule = one feature branch off `dev`, PR'd back to
`dev` for end-to-end staging on the dev Discord app. After all rules
have shipped to `dev` and passed staging, batch into the next
`release/X.Y.Z` branch and PR to `main`.

**Suggested order** (low-blast-radius first; schema-touching branches
stage on dev mandatorily):

| # | Rule / Scope | Branch slug | Notes |
|---|---|---|---|
| 1 | I — Slash-ref correctness | `storm-cs-slash-refs` | Pure copy fixes, no schema. Easy warm-up. |
| 2 | B — Floor → Minimum sweep | `storm-floor-to-minimum` | Big copy sweep, no schema. Update tests that assert UI strings. |
| 3 | J — Builder button label parity | `storm-builder-button-parity` | Pure copy + view rewiring. No schema. |
| 4 | D — Tab auto-create | `storm-tab-autocreate` | New `get_or_create_tab` helper + callsite plumbing. No schema. |
| 5 | F — Required signup_time | `storm-signup-time-required` | Modal validation, no schema. Coupled to #6 — land first or together. |
| 6 | H — Poll-day model | `storm-poll-day-model` | Schema migration (drop event_day_of_week + signup_lead_days, add poll_day_of_week). Wizard + scheduler + signup_post + officer_view all touched. Dev-staging mandatory. |
| 7 | C — Power column by letter | `storm-power-column-letter` | Schema rename + data migration + wizard rewrite + reader updates. Dev-staging mandatory. |
| 8 | A — CS has teams | `storm-cs-teams` | Largest sweep — touches signup view, officer view, strategy, attendance, history, walkthrough, help, member_rule CS group, plus reverts most of #148's single-team gate. Dev-staging mandatory. |
| 9 | G — Drop Judicator | `storm-drop-judicator` | Schema drop + wizard removal + faction-roles view deletion + `set_member_role` subcommand removal + 🎖️ list-branch removal + lots of test deletions. Dev-staging mandatory. |
| 10 | E — Modal → view conversions | `storm-modal-to-view` | 2.14c + 6.1 + 7.24 rewrites + zone-autocomplete on member_rule. Persistent-View changes → dev-staging mandatory. |
| 11 | M — Inline list actions | `storm-list-inline-actions` | New `_StrategyListView` + `_MemberRuleListView` button rows. Couples loosely to #10 (modal → view). No schema. |
| 12 | N — Walkthrough tour for DS + CS | `storm-walkthrough-cs-tour` | Wire `/canyonstorm signups` to `maybe_offer_storm_signups_tour` + tour-step content branching on event_type + `cfg.teams` + 14.9 rewrite for ephemeral on-behalf view. No schema. |
| 13 | Decisions 1, 5, 6, 8, 9, 14 — Auto-fill summary + attendance UI bundle | `storm-attendance-and-auto-fill-ui` | Big UI refactor. Bundles (a) attendance view rewrite with member-select + ✅/❌ buttons + bulk-when-empty + drop 🔄 + drop override marker, (b) auto-fill summary changes — show all gaps/conflicts, list auto-paired subs, confirm before destructive re-run, drop subject-not-on-roster conflict shape. Dev-staging mandatory (persistent-View + bot-state changes). |
| 14 | L — Per-zone-per-phase rendering | `storm-phase-aware-rendering` | Rewrite phase-aware variant in builder embed (Screen 7.23), preset editor (Screen 12.4), auto-fill summary (Screen 8.6), and history detail (new variant). Test coverage for each surface. Couples with #13's auto-fill summary changes. |
| 15 | Decision #7 — Per-member rules silently no-op when subject absent | `storm-rule-scope-roster-only` | `_auto_fill_session` + `_apply_rules_to_session` audit; the `per_member subject not on roster` conflict shape goes away. Pure logic + tests. No schema. |
| 16 | Decisions 10, 13 — Preset editor polish | `storm-preset-editor-polish` | Drop `[➕ Add zone]` button + `_AddZoneModal` (Decision #13). Phase-mode dropdown label cleanup. Mode-toggle copy reword. Button-label sweep (Rename preset / Abandon this preset). 12.9 modal-collapse investigation (sub-task — collapse 3-page wizard to 2-page if field-count permits). No schema. |
| 17 | Storm zone emoji icons (#158) | `storm-zone-emoji-icons` | Application Emojis pipeline + zone-icon prefix helper + 5-file call-site sweep. Lands LAST per #158 dependency order. No schema. |

**Per-branch test coverage**: existing 851-test baseline does not
regress. Each branch deletes tests for removed flows and adds tests
for the new flow.

**Issue strategy**: open one GitHub issue per row in the table
above (Issues #1-#16 below + #158 already filed). Use the
**Premium-Storm-UX-Overhaul** label (new — create when first issue
opens) to group. Each issue lists its dev-branch files of interest
and the walkthrough-doc sections it satisfies.

**CHANGELOG strategy**: bundle into a single major release
(`1.4.0` or similar) per CLAUDE.md's "one CHANGELOG entry per
release" rule. The release-branch PR description doubles as the
CHANGELOG entry per the release-branch-PR-description rule.
