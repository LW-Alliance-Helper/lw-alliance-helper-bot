# Changelog

All notable changes to **LW Alliance Helper** are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.0.13] — 2026-05-02

### Changed — README sync after post-1.0.11 audit

Documentation drift caught against the actual wizard step counts and
post-1.0.9 customisable DM body feature. Every change is in `README.md`;
no code changes.

- **`/setup_train` step list** rewritten as the actual 8 steps. Was
  collapsed to 3 numbered items; the rewrite calls out every step
  including Step 6 (prompt templates with the free-vs-premium cap)
  and Step 8 (Premium DM body).
- **`/setup_birthdays` step list** rewritten as the actual 9 steps.
  Added Step 1 (Enable birthday tracking?), Step 6 (placement) and
  Step 7 (lookahead) as their own items, and Step 9 (Premium DM body).
  Removed the outdated "Discord ID column" step — the wizard no
  longer asks for it (the column is preserved if previously set but
  collected via the birthday-sheet schema, not the wizard).
- **`/setup_desertstorm` step list** now explicitly says "7 steps"
  and includes Step 7 (Premium reminder DM body) as its own item.
- **Canyon Storm fixed-time claim removed.** README previously said
  *"Canyon Storm runs at 12:00 and 23:00 Server Time (displayed in
  your timezone)"* — that's an OGV-server assumption that the 1.0.9
  strip removed from the bot. Replaced with a note that CS times
  vary by server and come from the alliance's own calendar.
- **Premium-only feature matrix** now includes a "Customisable DM
  bodies" row covering the birthday / train / storm DM body
  overrides shipped in 1.0.9.

## [1.0.12] — 2026-05-02

### Fixed — stale version constant and stale wizard step label

Two latent string bugs caught during the post-1.0.11 documentation
audit:

- **`bot.py` `__version__` was still `"1.0.0"` after eleven releases.**
  Sentry reads this constant for its `release` tag, so every error
  shipped since 1.0.0 was bucketed under that release — making it
  impossible to tell which patch a crash came from. Now bumped to
  `"1.0.12"` and will move with each release going forward.
- **`setup_cog.py:674` rendered `"Step 6 of 7 — Prompt Templates"`
  inside the train wizard's prompt-templates manager.** The parent
  train wizard has been "of 8" since the Premium DM body step shipped
  in 1.0.9; users saw contradictory step counts mid-wizard. Corrected
  to `"Step 6 of 8"`.

No behavioural changes — both fixes are pure string corrections.

## [1.0.11] — 2026-05-02

### Changed — doc sync for 1.0.7 (CLAUDE.md, CONTENT_AUDIT.md)

Doc sync that should have ridden with 1.0.7 but was missed. The 1.0.9
and 1.0.10 doc syncs covered everything *except* the docs the 1.0.7
fix changed:

- **`CLAUDE.md` Repo layout** — `wizard_registry.py` row now lists
  `expire_view_message` (1.0.7) and `safe_edit_response` (1.0.9)
  alongside the original `wait_view_or_cancel`, and the LOC estimate
  is updated to reflect the file's growth.
- **`CLAUDE.md` Patterns to reuse** — added a new "Auto-posted
  approval/review views must clean up on timeout" entry pointing
  contributors at `expire_view_message` and the three canonical
  callsites (`scheduler.EventEditorView`, `scheduler.ApprovalView`,
  `train.ReminderView`). Without this entry, future View additions
  would silently re-introduce the dead-button bug.
- **`docs/CONTENT_AUDIT.md`** — three new view-timeout copy rows in
  §5.3 Event Editor, §5.8 Build Announcement → Approval (alongside
  the existing Edit & Send wait_for timeout), and §6.14 Daily train
  reminder loop. §14 Wizard infrastructure was corrected: the
  blanket "no user-facing strings" claim is no longer true now that
  `wizard_registry.expire_view_message` emits the timeout notice.

No code changes.

## [1.0.10] — 2026-05-02

### Fixed — birthday auto-population now persists, and only runs once a day

Two related bugs in `TrainCog.check_reminder` were quietly burning Google
Sheets API quota and spamming Railway logs every minute:

1. **Save was a no-op.** `check_and_add_birthdays` mutates the schedule
   dict in place and returns the same object, so the scheduler's
   `if updated_schedule != current_schedule` guard was comparing a dict
   to itself — always equal, so `save_schedule` never fired. The
   in-memory additions were thrown away on each tick. The manual
   `/train_addbirthdays` path was unaffected because it captures
   `len(current_schedule)` before the call. Fixed by snapshotting
   `before = dict(current_schedule)` and comparing against that.

2. **Auto-population ran every minute.** The reminder loop ticks every
   minute (it has to, for the per-minute time-match check on train and
   birthday announcements) and was unconditionally running the birthday
   sheet fetch + auto-placement on every tick. Trains only fire once a
   day, so the auto-population only needs to run once a day. Now gated
   by a per-day `birthday_population_fired` set (mirroring the existing
   `reminders_fired` pattern) — fires on the first tick after the daily
   reset and is skipped for the rest of the day. Marked fired even on
   error so a transient sheets outage doesn't spam the same exception
   every minute until midnight.

For an alliance with birthdays enabled, this drops the auto-population
sheet reads from ~1440/day per guild to 1/day per guild. The manual
`/train_addbirthdays` command is still the escape hatch for "add this
birthday to the schedule right now."

`/setup_birthdays` now tells the user, when they enable train
integration, that auto-population runs once per day at server-time
midnight and points them to `/train_addbirthdays` for on-demand runs.

## [1.0.9] — 2026-05-02

### Fixed — wizard views no longer hang when the interaction token expires

Discord interaction tokens are valid for 3 seconds. When a wizard view's
button or select callback couldn't reach `interaction.response.edit_message`
in that window (network blip, idle wizard, busy event loop), Discord
rejected the response with `NotFound` (10062 "Unknown interaction").
discord.py's default `on_error` logged the traceback at error level
**and** the wizard hung on `view.wait()` until the view's own timeout
fired — which then posted a misleading "Timed out" message.

Sentry surfaced this for `ScheduleTypeView` in production (issue
7456234387, `[UZD] DARK SOULS` guild), but the same pattern existed at
~99 other `interaction.response.edit_message` sites across the
codebase — every wizard step view, dropdown callback, confirm/cancel
button, and persistent-button callback in the bot.

A new `wizard_registry.safe_edit_response(interaction, **kwargs)` helper
calls the normal interaction response, falls back to
`interaction.message.edit` on `NotFound`, and swallows any remaining
`HTTPException` (e.g. message deleted) — so the caller's `self.stop()`
runs unconditionally and the wizard advances. Every
`interaction.response.edit_message` callsite in `bot.py`, `scheduler.py`,
`setup_cog.py`, `storm.py`, `storm_log.py`, `survey.py`, `train.py`, and
`train_ui.py` was rewritten to use it (100 sites total). The two
inline-fallbacks `survey.py` already had were collapsed onto the same
helper. Helper covered by four new tests in
`tests/unit/test_wizard_registry.py`.

### Changed — OGV strip (internal refactor, no user-facing behavior change for OGV)

OGV (the bot owner's home alliance) was originally hardcoded into the bot
as the canonical "test alliance" — its config was auto-seeded into the
SQLite database on every startup, its templates and survey questions
shipped as the default values for every new guild, and it was permanently
flagged as Premium via a hardcoded `{OGV_GUILD_ID}` set in `premium.py`.
Pre-launch this was fine; post-launch it meant every alliance that
installed the bot got OGV-flavoured defaults, and one alliance had a
subtly different code path from everyone else.

This release removes all of that. OGV now goes through the same `/setup`
flow as every other alliance and is treated identically by the bot. The
migration was executed in seven steps:

1. **`defaults.py`** — extracted framework-level default values
   (themes, tones, prompt template, survey question set, neutral storm
   templates) out of `config.py` into a dedicated module. The default
   survey intro was rewritten to drop "us" / "our" so it reads
   neutrally for any alliance.
2. **Seeding code stripped** — deleted the six `_seed_ogv_*` functions,
   `seed_ogv_events()`, the OGV `INSERT` block in `init_db()`, and the
   `SPREADSHEET_ID` env-var-into-OGV-row logic. Schema defaults for the
   role-name columns went from `'OGV'` / `'OGV Leadership'` to generic
   `'Member'` / `'Leadership'` placeholders. OGV's existing rows in the
   production DB were preserved intact.
3. **Premium gate moved to env var** — replaced `ALWAYS_PREMIUM_GUILD_IDS
   = {OGV_GUILD_ID}` with a `PREMIUM_BYPASS_GUILD_IDS` env-var-driven
   set. OGV's permanent Premium status now comes from the env var on
   the host (Railway), not from hardcoded code.
4. **`/events` rewritten to use per-guild events** — fixed a long-
   standing bug where `/events` silently fell back to OGV's hardcoded
   event times for any non-OGV guild because the legacy
   `next_event_dates()` / `get_event_datetimes()` wrappers used
   `get_config(None)` which always returned None. The command now reads
   from `guild_events` and supports guilds with multiple cycle types.
5. **Test fixtures decoupled** — replaced every `OGV_GUILD_ID` reference
   in the test suite with a synthetic `PREMIUM_TEST_GUILD_ID` constant
   in `tests/constants.py`. The `OGV_GUILD_ID` constant in `config.py`
   was deleted entirely.
6. **Comment + docs cleanup** — generalised ~10 stale narrative
   references in code comments and docstrings; updated
   `docs/PREMIUM_SETUP.md` to document the new env var.
7. **Final verification** — full test suite (318 tests) passes;
   production smoke test confirmed clean startup, no `[CONFIG] Seeded
   OGV …` log lines, and OGV-specific commands continue to work
   identically through the env-var-driven Premium grant.

The one-shot migration scripts (`scripts/verify_ogv_data.py`,
`scripts/fix_ogv_data.py`) used during the strip have also been removed
from the repo — git history preserves them as a template if a similar
migration is ever needed.

After this work, OGV's real guild ID appears only in:
- The `PREMIUM_BYPASS_GUILD_IDS` env var on the host (production config)
- `docs/OGV_STRIP_INVENTORY.md` (historical record of the migration)

### Fixed

- **`/canyonstorm` rendering for OGV** — the CS mail template stored in
  OGV's row used the old `{subs_list}` placeholder name. The codebase
  had migrated to `{subs}` long ago, so OGV's CS mails were rendering
  with literal `{subs_list}` text. One-shot data fix replaced the
  placeholder.
- **Train reminders for OGV** — `reminder_channel_id` was 0, so the
  daily train reminder was effectively going nowhere. Set to OGV's
  leadership channel.
- **Event channel defaults for OGV** — `guild_configs.event_draft_channel_id`
  and `event_announce_channel_id` were 0 because the columns were added
  via `ALTER TABLE` after OGV's row was inserted. Set to match the
  per-event values in `guild_events`.
- **`/events` for non-OGV guilds** — see step 4 above. Previously
  silently broken for any alliance that wasn't OGV; now works
  correctly across the board.
- **Duplicate startup logs** — both the growth snapshot task and the
  stats publisher had redundant startup-time invocations alongside their
  scheduled `tasks.loop`. Removed the duplicates; each now runs exactly
  once on boot via the loop's `.start()` immediate-fire behavior.

### Fixed — post-strip launch readiness

- **Survey too-long input no longer cancels the whole survey.** A
  member typing `153,725,881` for a THP-in-millions field (max 3
  characters) used to see "Please try the survey again" and have to
  click the Answer button to restart from question 1. The text
  question handler (`ask_number`) now re-prompts the same question up
  to 5 times — matching the existing retry behaviour of the Premium
  `numeric` and `date` question types — so one slip costs one re-entry,
  not the entire survey. Reported by an OGV member who hit it on the
  THP question.
- **`/cancel` actually stops view-based wizard steps.** Bare
  `view.wait()` doesn't know about the cancel registry, so when a
  user ran `/cancel` mid-wizard the active view sat there until its
  own 2-minute timeout fired and the wizard then posted a misleading
  "⏰ Timed out" message. New `wizard_registry.wait_view_or_cancel()`
  races the view's `wait()` against the cancel event; cancel now
  returns silently. Threaded through ~40 callsites in `setup_cog.py`.
- **CS draft renders full zone names.** `build_cs_mail` was deriving
  zone labels from dict keys via `key.replace('s1 ', '').title()`,
  which collapsed `s1_dc1` → `Dc1` instead of "Data Center 1". The
  same loop also rendered `s3_pop_pair1` as a `Pop Pair1` zone *and*
  as the `{subs}` block, duplicating sub pairs. Introduced a single
  `CS_ZONE_STRUCTURE` source of truth shared by the template
  builder, parser, and mail builder. Output now groups zones under
  `**Stage 1/2/3**` headers with full labels. DS was unaffected
  (DS uses user-entered zone names directly as keys).
- **`/sync_members` writes the actual member count.** The bot was
  constructed with `Intents.default()` plus `message_content`, but
  `default()` deliberately omits the privileged `members` intent. So
  even with the SERVER MEMBERS INTENT toggled on in the Developer
  Portal, the gateway connection wasn't requesting member data —
  leaving `guild.members` populated only with users Discord surfaces
  via interactions, and `/sync_members` writing 0 rows. Added
  `intents.members = True` plus `_ensure_member_cache(guild)` (calls
  `guild.chunk()` before each sync) and a `[ROSTER]` warning when
  the cache is dramatically smaller than `guild.member_count`. Side
  benefit: `on_member_join` / `_remove` / `_update` now fire, so
  Member Roster auto-resync actually works (was silently dead).
- **Custom event blurb shows up in `/events` announcements.** The
  EventEditorView's "Add Event" handler appended `{key, dt}` to
  `event_list` without `name` or `blurb`, so `build_announcement`
  saw an empty blurb, fell through `EVENT_LIBRARY` (no entry for
  guild-defined keys like `glacieradon`), and hit the f-string
  fallback `f"{key} at {time}..."` — rendering the lowercase
  short_key. Now populates `name` + `blurb` from
  `_resolve_event_info()`. Added defense-in-depth: `build_announcement`
  gained an optional `guild_id` kwarg that re-resolves the blurb
  from `guild_events` when the dict is missing it.
- **Wizard "Use default" label no longer mislabels saved guild
  values.** `ask_keep_or_change` took a single `default=` parameter
  that callers pre-resolved as `current.get("X") or "fallback"`, and
  the button hardcoded `✅ Use default: {default}`. Result: an OGV
  admin running `/setup_birthdays` saw their saved
  `Season 5 - Off-Season` tab labelled as "Use default" — confusing.
  Split into separate `default=` (hardcoded baseline) and
  `current=` (saved value). Now renders 3 buttons when they differ:
  `✅ Keep current: <saved>` / `↩️ Use default: <hardcoded>` /
  `✏️ Define my own`. Two-button layout retained when they match.

### Added — alliance-customisable DM bodies

Three Premium DM features (birthday DMs, train DMs, storm participation
reminders) had hardcoded English bodies that no alliance could change.
Every other text the bot sends — survey questions, event blurbs, storm
mail templates, announcement templates — is alliance-configurable.
This was a gap, and a particularly painful one for non-English alliances:
member-facing DMs go to private channels where translation bots in the
guild can't help. Closing the gap unlocks the highest-value localization
win without committing to full bot i18n.

- **`guild_birthday_config.dm_message`** — body of the per-member
  birthday DM. Configured via a new Step 9 in `/setup_birthdays`
  (Premium-labelled but visible on free tier so alliances can pre-
  configure before upgrading). Supports `{name}` placeholder.
- **`guild_train_config.dm_message`** — body of the train DM that fires
  alongside the channel reminder. New Step 8 in `/setup_train`. Same
  `{name}` placeholder.
- **`guild_storm_config.dm_reminder_message`** — body of the DM that
  fires on `/desertstorm_remind` / `/canyonstorm_remind`. New Step 7
  in `/setup_desertstorm` / `/setup_canyonstorm`. Per-event-type, so
  DS and CS can have different copy. Supports `{name}` placeholder.

Empty stored value = "use the bot's hardcoded default", which means
future tweaks to the default text reach existing alliances automatically
unless they've explicitly customised. Typo-tolerant: an unknown
placeholder like `{nme}` renders literally instead of crashing the
reminder loop. Three new wizard step prompts ask "Use default" or
"Keep current" via the existing `ask_keep_or_change` helper.

25 new tests in `tests/unit/test_dm_templates.py` (the shared
`_render_dm_body` helper — `{name}` substitution, unknown-placeholder
tolerance, format-spec edge cases) plus added paths in
`test_storm_remind.py` and `test_train_reminder_loop.py` covering
the configured-template flow end-to-end. 501 passed (was 476).

### Added — post-strip launch readiness

- **Next-snapshot date in `/setup_growth` and `/growth`.** After
  picking a custom interval, users had no idea when the first
  snapshot would actually fire. New `growth.compute_next_snapshot()`
  helper mirrors `bot.growth_task`'s scheduling rules (22:00 ET on
  monthly day or every-N-days-from-2026-01-01) and surfaces the
  result as a Discord-localized timestamp (`<t:N:F> (<t:N:R>)` —
  full date plus relative). Both the wizard confirmation embed and
  `/growth` status now display it.
- **`docs/USER_TESTING.md` refresh.** Added regression hooks for
  every fix above so testers know what specific behaviour to verify
  and report. New section covers `/cancel`, the `/setup_*` family,
  the Reference IDs that now appear in error messages, and the
  Premium channel/thread destination chooser.

### Tests — coverage audit

- **+81 tests across 7 high-impact background-task / view-callback
  paths** that had shipped without dedicated coverage. New files:
  `test_events_command.py` (9 — `/events` end-to-end),
  `test_scheduler_helpers.py::TestParseTimeStr` (+22 — 12h/24h/12am/
  12pm boundaries), `test_approval_view.py` (7 — Send-As-Is +
  Edit-And-Send + timeout/missing-channel), `test_growth_task.py`
  (11 — fire-decision loop + multi-guild crash isolation),
  `test_fire_warning.py` (8 — 5-min path + `pending_warnings`
  lifecycle), `test_storm_remind.py` (9 — Premium-gate + roster
  iteration + DM tally), `test_train_reminder_loop.py` (9 —
  time-match + idempotency + Premium DM-to-assignee),
  `test_survey_scheduled_dm.py` (6 — DM branch + Premium-lapse +
  weekly + multi-guild isolation). The `run_scheduler` main loop
  is the only audit gap deferred (Large effort, low ROI per minute
  given its individual components are now individually covered).
  Total: 476 passing, 18 skipped (`free_tier_only` markers under
  the `FORCE_PREMIUM=1` CI lane).

### Documentation

- **`docs/PREMIUM_SETUP.md`** — added a "Bot prerequisites" section
  documenting that **SERVER MEMBERS INTENT** must be toggled on in
  the Discord Developer Portal. Without it the bot won't start
  (gateway refuses the connection) and `/sync_members` would write
  0 rows even with a paid SKU.
- **`docs/CONTENT_AUDIT.md`** — updated the
  `ask_keep_or_change` wizard step entry to reflect the new
  current-vs-default 3-button layout.

## [1.0.8] — 2026-05-02

### Removed — legacy-column shims (1.0.5 follow-up)

Production startup logs from the 1.0.5 deploy confirmed all 10
retired `guild_configs` columns physically dropped on Railway —
SQLite is recent enough for `ALTER TABLE … DROP COLUMN`. The
defensive scaffolding around them is now dead weight:

- One-shot DROP COLUMN migration block in `init_db()` removed
  (its job is done; re-running it would just emit no-op log lines).
- `get_config` row-dict filter against `GuildConfig.__dataclass_fields__`
  reverted to a plain `GuildConfig(**dict(row))`.
- `scheduler.run_scheduler` row-instantiation filter (added in 1.0.6
  for the same defensive reason) reverted in lockstep.
- `test_scheduler_row_instantiation_tolerates_legacy_columns`
  regression test deleted — the scenario it guarded against can't
  happen anymore.

If a future schema retirement is mishandled (column dropped from
the dataclass without a matching `ALTER TABLE … DROP COLUMN`),
the symptom will surface as a `TypeError` rather than being
silently swallowed by a defensive filter — which is a feature,
not a regression.

## [1.0.7] — 2026-05-02

### Fixed — automated-post buttons no longer go silently dead on timeout

The daily event-editor draft, the review/approval draft that follows,
and the daily train reminder are all posted by background tasks with
1-hour view timeouts. Before this release, when those views expired:

- The Discord-side message kept rendering active-looking buttons.
- A leadership click would fail with the unhelpful "Interaction failed"
  toast, with no indication the draft had timed out.
- `ApprovalView.on_timeout` set `disabled = True` on its in-memory
  buttons but never edited the message, so the change never reached
  Discord. `EventEditorView.on_timeout` and `ReminderView` (which
  didn't define one at all) had the same problem.

Now, when any of those automated approval/review posts time out, the
buttons are physically removed and a notice is appended to the
original message: *"⏰ The actions for this have timed out. Use
`/events` (or `/train`) to re-initiate."* Leadership sees immediately
that the draft is stale and knows the exact command to re-open it.

The expire-and-notify pattern lives in
`wizard_registry.expire_view_message` — a small helper any future
View can call from its own `on_timeout` to get the same behaviour.

## [1.0.6] — 2026-05-02

### Fixed — scheduler crash on production DBs carrying retired columns

`scheduler.run_scheduler` reads `guild_configs` rows directly via raw
SQL and builds `GuildConfig(**dict(row))` for each one. The 1.0.5
"physically drop legacy columns" migration runs an `ALTER TABLE …
DROP COLUMN` that silently no-ops on SQLite < 3.35, so the Railway
production DB still carried `storm_log_thread_id` — and every
scheduler tick crashed with:

```
TypeError: GuildConfig.__init__() got an unexpected keyword argument
'storm_log_thread_id'
```

`get_config` already filters unknown columns against
`GuildConfig.__dataclass_fields__` for exactly this reason; the
scheduler now applies the same filter before instantiation. Added
`test_scheduler_row_instantiation_tolerates_legacy_columns` as a
regression guard.

## [1.0.5] — 2026-05-01

### Removed — physically drop dead `guild_configs` columns

The 10 columns retired in 1.0.2 (dropped from the dataclass + CREATE
TABLE but left in production rows) are now physically dropped via a
new idempotent migration block in `init_db()`. Runs once on the next
bot startup; subsequent runs are no-ops because the `ALTER TABLE …
DROP COLUMN` fails harmlessly when the column is already gone.

Columns dropped: `storm_log_thread_id`, `tab_squad_powers`,
`tab_growth_tracking`, `anchor_date`, `cycle_days`,
`marauder_time_normal`, `siege_time_normal`,
`marauder_time_saturday`, `siege_time_saturday`,
`shield_warning_time`.

The defensive `get_config` filter that ignored unknown columns stays
in for now — once production confirms the DROP COLUMN ran, both the
filter and this migration block can be deleted in a follow-up.

Verified by feeding `init_db()` a synthetic DB seeded with all 10
legacy columns; post-migration `PRAGMA table_info(guild_configs)`
returned only the live schema fields.

## [1.0.4] — 2026-05-01

### Changed — pre-launch polish (audit Round 4)

Smaller cleanups grouped together. None change behaviour beyond fixing
small bugs the broader code already handled defensively.

- **`growth.py` snapshot loop** now narrows the worksheet-creation
  catch from `except Exception` to `except gspread.exceptions.WorksheetNotFound`,
  drops a write-only `col_header` local, and rewrites the module
  docstring to describe the per-guild `guild_growth_config` flow
  instead of the OGV-era hardcoded Squad Powers shape.
- **`storm.py` defaults** strip the OGV member names that shipped as
  `DEFAULT_CS_A` / `DEFAULT_CS_B` values (the keys are still required
  by `test_default_cs_assignments_only_use_canonical_keys`, so the
  dicts now hold empty strings instead). The 35 lines of `"Member
  Name"` placeholders for `DEFAULT_A_ZONES` / `DEFAULT_B_ZONES` /
  `DEFAULT_A_SUBS` / `DEFAULT_B_SUBS` collapse to a single
  `DEFAULTS = {"A": ({}, []), "B": ({}, [])}`. The "e.g. Jon, Lionel,
  Ice" Modal placeholder in `storm_log.py` is now "Alice, Bob, Chris".
- **Dead method/attribute pruning.**
  - `member_roster._format_roles` no longer accepts a `role_filter_id`
    argument it never read.
  - `train_cog.TrainCog` drops the `reminder_sent_today` attribute
    (and the matching defensive `hasattr(self, "reminders_fired")`
    branch that `__init__` always satisfied). Test fixture updated
    in lockstep.
  - `setup_cog.YesNoView.__init__` no longer accepts `yes_label` /
    `no_label` (buttons were always hardcoded "Yes" / "No").
  - `setup_cog.BlurbChoiceView.__init__` no longer accepts a
    `has_existing` flag it never stored.
- **Module-docstring refresh** for `scheduler.py` (drops the
  Marauder/Siege/Friday-shield narrative — events are now per-guild
  and Friday shields were retired in Round 1) and `train_birthdays.py`
  (drops the "exposes the lookahead window" line — that helper was
  removed in Round 1).

## [1.0.3] — 2026-05-01

### Changed — duplication + step-numbering cleanup (audit Round 3)

- **One column-letter helper, not three.** `_col_letter_to_index` and
  `_col_index_to_letter` (the AA+ aware module-level pair in
  `setup_cog.py`) are now the single source of truth. The local
  `_col_letter` inside `_send_view_configuration` becomes a thin
  display wrapper, and the local `col_letter_to_index` /
  `index_to_letter` inside `run_birthday_setup` are deleted (their
  callers now use the module-level pair directly).
- **`EventEditorView` content rendering deduplicated.** The five
  copies of the editor content string are replaced with calls to a
  new `_render_editor_content()` method. `post_editor` (which built
  the initial editor message via a parallel local loop using
  `EVENT_LIBRARY` directly) now also calls the same method, so
  custom-event names render consistently with `_resolve_event_info`.
- **`_get_spreadsheet` is now one function.** `growth.py`,
  `storm.py`, `storm_log.py`, and `survey.py` all delegate to a new
  `config.get_spreadsheet(guild_id)` helper. The four ~15-line
  gspread-bootstrap copies become 2-line wrappers.
- **Train themes/tones step migrated to `ask_keep_or_change`.** The
  hand-rolled `KeepOrChangeView` in `run_train_setup` mislabelled
  the saved guild value as "**Defaults:**" — same regression
  `ask_keep_or_change` was fixed for elsewhere. The themes/tones
  prompts now go through that helper (capped values passed in for
  both the hardcoded baseline and the saved value), so users see
  the proper 3-button **Keep current / Use default / Define my
  own** layout. The integration test for free-tier theme truncation
  was updated in lockstep.
- **`config.init_db` migration block deduplicated.** The four
  `event_*` ALTERs that appeared in two places now only run once,
  with a comment explaining why the silent-swallow earlier block
  is kept (handles upgrades from the very oldest schema versions).
- **`/setup_desertstorm` and `/setup_canyonstorm` step counter
  fixed** from "Step X of 6" to "Step X of 7". The participation
  step (#6) and reminder-DM step (#7) had been added without
  updating the earlier prompts, so wizard progress looked
  truncated to the user.

## [1.0.2] — 2026-05-01

### Removed — dead schema columns (audit Round 2)

These columns lived on the `guild_configs` dataclass + CREATE TABLE
but had zero readers anywhere in the repo. Per the audit's
recommended migration policy: drop them from schema/dataclass and
leave any existing physical columns in production DBs untouched.
A new filter in `config.get_config` (`{k: v for k, v in dict(row)
if k in GuildConfig.__dataclass_fields__}`) keeps `GuildConfig(**...)`
from blowing up when a legacy column is still in a row.

- **`storm_log_thread_id`** — replaced by event-typed log channels.
- **`tab_squad_powers`, `tab_growth_tracking`** — survey/growth use
  their own per-feature config tables. The one stale fallback
  (`survey.update_squad_powers` referencing `cfg.tab_squad_powers`)
  is replaced with the literal `"Squad Powers"` default.
- **Pre-`/events`-rewrite game-time fields**: `marauder_time_normal`,
  `siege_time_normal`, `marauder_time_saturday`,
  `siege_time_saturday`, `shield_warning_time`, `cycle_days`,
  `anchor_date`, plus the `anchor_date_parsed()` helper. All
  superseded by `guild_events` rows.

`tests/conftest.py` no longer assigns the dropped
`storm_log_thread_id` field.

## [1.0.1] — 2026-05-01

### Fixed

- **`/survey_remind` "Manage scheduled reminders" path no longer
  fails on entry.** `survey._run_schedule_wizard` opened with
  `from config import save_survey_reminder, _parse_12h_time as _parse_time_helper`,
  but `_parse_12h_time` lives in `setup_cog`, not `config`. A
  `# type: ignore` masked the guaranteed `ImportError`. The aliased
  name was never used inside the function — the actual time parser
  is correctly re-imported from `setup_cog` further down. Dropped
  the broken alias.
- **`train_ui.run_blurb_wizard_for_entry` no longer fetches a value
  it doesn't read.** The wizard collects all five schedule-entry
  fields (`name`, `theme`, `tone`, `notes`, `prompt_retrieved`)
  fresh on each run, so the unused `existing = schedule.get(...)`
  line was dead. Removed.

### Removed — dead code (zero callers, verified across the repo)

- `sheets.py` (~85 LOC, OGV-era leftover; nothing imports it).
- `storm_log.py` legacy log path: `append_log_row`, `LOG_HEADERS`,
  `_ensure_headers`, `load_member_names`, `get_prior_sitouts`
  (~80 LOC). Replaced long ago by the configurable participation
  flow that reads through `get_participation_config`.
- `scheduler.py` Friday shield workflow: `__noon_dt_for`,
  `post_shield_draft`, `SHIELD_REMINDER` (~30 LOC). Pre-`/events`-rewrite
  feature, no longer wired into the scheduler loop.
- `growth.py` OGV-era column-index constants
  (`SP_USERNAME_COL` and four siblings).
- `survey.py` OGV-era constants: `SQUAD_TYPES`, `PROFESSIONS`,
  `BANNER_OPTIONS`, `AID_REMOVAL_OPTIONS`, `HISTORY_HEADERS`,
  `SURVEY_BUTTON_CUSTOM_ID_PREFIX`, `SURVEY_BUTTON_CUSTOM_ID_RE`,
  `_to_millions`. The current dynamic-survey code derives all of
  these from per-guild config.
- `setup_cog.py` `ask_view` local helper inside `run_event_setup`
  (no callers; the wizard uses `wait_view_or_cancel` directly).
- `storm.py` unused `ZoneInfo` import + `ET` constant.
- `train.py` unused `app_commands` and `tasks` imports (slash
  commands moved to `train_cog.py`).
- `survey.py` unused `date as date_cls` import.
- `bot.py` unused `get_or_create_config` import.
- `scheduler.py` unused `init_db` import.
- `config.set_member_tab`, `train_birthdays.get_birthday_lookahead`,
  `train_birthdays.DEFAULT_MEMBER_TAB` and the matching
  re-exports in `train.py`.

Net: ~250 LOC of dead code removed. 501 tests still pass.

## [1.0.0] — 2026-04-28

Initial public release.

### Added

- **Event announcements** — schedule recurring in-game events (Plague Marauder,
  Zombie Siege, custom events). Drafts are posted to leadership for approval at
  a configurable lead time before each event, and the final announcement is
  posted automatically once approved. A 5-minute pre-event warning fires
  automatically. Managed via `/events` and `/events_log`.
- **Train schedule** — `/train` to view and edit the daily alliance train
  assignment, with inline buttons for Add, Update, Generate Prompt, and Clear.
  When a blurb template and ChatGPT integration are configured, the bot
  generates a personalised prompt for the day's train recipient.
- **Birthday tracking** — read birthday data from the configured Google Sheet,
  optionally announce birthdays in Discord, and (with `/train_addbirthdays`)
  pre-populate the train schedule on a member's birthday.
- **Desert Storm / Canyon Storm**
  - `/desertstorm_draft` and `/canyonstorm_draft` — four-step mail drafting
    flow (Pick Team → Pick Time → Use template / Edit → Preview), ending
    with a **Post & Copy** action that publishes the announcement and
    saves the latest text as the next-time template.
  - `/desertstorm_participation` and `/canyonstorm_participation` —
    configurable participation logging. Each alliance defines up to 3 custom
    questions (free text, numeric, multi-select roster pick, etc.) and the
    log is written to the configured Google Sheet tab.
  - `/desertstorm_log` and `/canyonstorm_log` — look up a past log by date.
  - `/desertstorm` and `/canyonstorm` — overview embeds showing the current
    configuration.
- **Squad-power survey** — `/survey_post` posts a button members can click to
  open a private thread and submit their stats. Responses are appended to the
  configured Google Sheet, and leadership receives a summary in the
  notification channel for each submission.
- **Survey reminders** — `/survey_remind` to send a one-off reminder
  immediately or schedule a recurring reminder (frequency, day-of-week, time).
  Free tier delivers as a channel post; Premium can deliver via DM to roster
  members who haven't yet submitted.
- **Growth tracking** — `/growth` to take a manual snapshot or schedule
  recurring snapshots (monthly or every-N-days). Captures the metrics defined
  in the alliance's growth config (squad powers, THP, kills, etc.) into a
  history sheet for trend analysis.
- **Setup wizard** — `/setup` walks new servers through core configuration
  (member role, leadership role, leadership channel, timezone, Google Sheet
  share). Per-feature `/setup_*` commands cover events, train, birthdays,
  storms, survey, growth, and member roster sync.
- **Configuration view** — `/view_configuration` displays everything the bot
  has been configured with so leadership can audit current settings.
- **Help command** — `/help` lists every available command, grouped by
  feature, with one-line descriptions.

### Premium

The following features require an active **LW Alliance Helper Premium**
subscription via Discord App Subscriptions ($4.99 USD / month — Discord does
not currently support annual billing):

- **Member Roster Sync** — `/sync_members` keeps a roster of in-game member
  names mapped to Discord users. Required for DM-based features.
- **Multi-survey** — define and manage multiple named surveys via the
  `/survey` manage view (Add / Edit / Remove). Each survey has its own
  question set, sheet tab, and answer button.
- **DM-based reminders** — `/survey_remind`, `/desertstorm_remind`, and
  `/canyonstorm_remind` can DM individual roster members instead of posting a
  channel reminder.
- **Scheduled survey reminders** — recurring reminders fire automatically on
  the configured frequency (weekly / fortnightly / monthly), day, and time,
  delivered via DM to members who haven't yet responded.
- **Unlimited templates** — the free tier caps mail templates and event
  templates at sensible defaults; Premium removes those caps.

### Notes

- This is the first publicly released version. Earlier internal builds powered
  the **OGV** alliance during private testing. The version constant is
  `__version__ = "1.0.0"` in `bot.py`.
- Bug reports and feature requests can be filed at
  <https://github.com/LW-Alliance-Helper/lw-alliance-helper.github.io/issues>.

[Unreleased]: https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/releases/tag/v1.0.0
