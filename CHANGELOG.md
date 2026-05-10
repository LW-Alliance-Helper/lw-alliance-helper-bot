# Changelog

All notable changes to **LW Alliance Helper** are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Each entry is a slim summary — heavier context (root cause, what we
tried, design rationale) lives in the corresponding commit message
and PR description.

## [1.1.3] — 2026-05-09

### Changed
- `/desertstorm_draft` and `/canyonstorm_draft` time-picker buttons now read `4pm EDT (18:00 server time)` style — local clock from the guild timezone, server-time portion always spelled out ([#58](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/58)).
- Birthday parser accepts dash, dot, ISO 8601, abbreviated months, day-first formats, 2-digit years, and whitespace around separators; bare numeric ambiguity defaults to M/D unless the first number is > 12 ([#60](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/60)).

### Fixed
- `/growth` Edit Config button now opens the `/setup_growth` wizard inline instead of telling the user to run the slash command themselves ([#59](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/59)).
- Birthday parser rejects impossible dates (`Feb 30`, `13/45`) instead of silently writing garbage to the train schedule ([#60](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/60)).

### Removed
- All six `time_option_*` columns dropped from `guild_storm_config` — slot times are game-defined and computed at display time from the guild's timezone ([#58](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/58)).

## [1.1.2] — 2026-05-08

### Fixed
- Daily event announcements now surface the local timezone alongside server time — `5:00pm EDT (19:00 Server Time)` instead of bare `5:00pm`, so members reading the post know which tz the leader meant.
- Add Event / Edit Time in the daily-draft editor preserve the alliance's per-event timezone instead of silently coercing every leadership-entered time into America/New_York.

Hotfix released direct to main per CLAUDE.md's hotfix exception.

## [1.1.1] — 2026-05-07

### Fixed
- `/help` rebuilt as a category-dropdown view so the embed no longer exceeds Discord's 6000-char limit (regression from 1.1.0's data-ownership copy).
- Storm and train sheet-load error logs name what actually failed — missing worksheet tab, spreadsheet 404, 403, or rate-limit — instead of opaque gspread reprs.

Hotfix released direct to main per CLAUDE.md's hotfix exception.

## [1.1.0] — 2026-05-07

### Added
- Premium per-user assignment layer with `/premium_assign` and `/premium_unassign` (both confirmation-gated) for the new User Subscription SKU ([#41](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/41)).

### Changed
- Data-ownership story made explicit in README, welcome DM, `/help`, and `/upgrade` ([#39](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/39)).
- CLAUDE.md documents the dev-branch staging workflow ([#36](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/36)) and the release-branch cleanup practice ([#46](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/46)).
- `/setup` step 3 prompt reworded to reflect the leadership channel's destination-only role.

### Fixed
- Setup wizard's "➕ Create a new channel" button no longer suppressed on Premium guilds ([#48](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/48)).
- Leadership commands no longer gated by channel category — role check is the security boundary, fixing `/cancel` mid-wizard and the empty-category edge case; `leadership_category_id` dropped via one-shot migration ([#49](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/49)).

## [1.0.19] — 2026-05-07

### Fixed
- Growth snapshots no longer blow the 60/min Sheets write quota — `_run_growth_snapshot_inner` was calling `ws.append_row` once per new member inside its loop, so any first-ever snapshot of a populated roster (60+ members) hit a 429 mid-write and aborted partway. Collapsed into a single `ws.append_rows` after the loop ([#40](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/40)).

Hotfix released direct to main per CLAUDE.md's hotfix exception.

## [1.0.18] — 2026-05-05

### Fixed
- Birthday → train auto-population now fires at 22:00 ET (= 00:00 server time) instead of UTC midnight, and no longer re-fires on every Railway redeploy ([#29](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/29)).

### Changed
- Fleet-wide logging-gaps audit across DM, scheduler, train, premium, member-roster, survey, storm-log, and stats-publisher paths — silent failures now log per-guild, and unexpected non-Discord exceptions Sentry-capture instead of Railway-stdout-only ([#31](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/31)).

## [1.0.17] — 2026-05-05

### Fixed
- Premium fallback path no longer crashes for guilds without an interaction context — `bot.entitlements()` was being called with the pre-2.4 `sku_ids=` kwarg instead of `skus=`, so every background-task premium check (scheduler loops, daily reminders, growth snapshots, roster sync) was silently downgrading paying customers to free-tier ([#28](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/28)). Test now binds the call's kwargs against the real `discord.Client.entitlements` signature so the next rename fails loudly.

Hotfix released direct to main per CLAUDE.md's hotfix exception.

## [1.0.16] — 2026-05-03

### Changed
- Slimmed CHANGELOG.md to one-line-per-change format ([#17](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/17)). 746 → 159 lines.
- Updated CLAUDE.md working agreement to reflect the release-branch workflow established 2026-05-03; gitignored `PLANNING.md` ([#19](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/19)).
- Synced CLAUDE.md "Recent shipped highlights" table to 1.0.15 and refreshed the status snapshot ([#18](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/18)).
- Corrected stale workflow references in CLAUDE.md after the workflow was refined: merge commit (not squash) and descriptive feature branches grouping multiple issues (not per-issue) ([#24](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/24)).

This release is documentation only — no bot code changed. `__version__` and `bot.py`'s Sentry release tag move to `1.0.16` to keep release tagging accurate.

## [1.0.15] — 2026-05-03

### Fixed
- Sheet-CI rerun filters dropped — `--only-rerun "Quota exceeded"` never matched real failures because `save_cs_assignments` swallows the 429 and the test fails with a plain `AssertionError`. Any sheet-test failure now retries twice with the existing 65s pause.

## [1.0.14] — 2026-05-02

### Removed
- `docs/OGV_STRIP_INVENTORY.md` — working doc for the 1.0.9 OGV strip; resolved three releases ago, never linked.

## [1.0.13] — 2026-05-02

### Changed
- README `/setup_train`, `/setup_birthdays`, `/setup_desertstorm` step lists rewritten to match the actual wizards (8, 9, and 7 steps respectively).
- README's outdated Canyon Storm fixed-time claim (12:00 / 23:00 Server Time) removed — that was an OGV assumption stripped in 1.0.9.
- Added Premium-feature-matrix row for "Customisable DM bodies" (1.0.9 feature).

## [1.0.12] — 2026-05-02

### Fixed
- `bot.py.__version__` was still `"1.0.0"` after eleven releases; Sentry was bucketing every error under that release. Bumped, will move per release going forward.
- `setup_cog.py:674` train-wizard prompt-templates manager rendered "Step 6 of 7" inside an "of 8" parent flow. Corrected.

## [1.0.11] — 2026-05-02

### Changed
- Doc sync that should have ridden with 1.0.7: `CLAUDE.md` `wizard_registry.py` row updated to list `expire_view_message` + `safe_edit_response`; new "Auto-posted views must clean up on timeout" pattern entry pointing at the three canonical callsites.
- `docs/CONTENT_AUDIT.md` got three new view-timeout copy rows + corrected blanket "no user-facing strings" claim about `wizard_registry`.

## [1.0.10] — 2026-05-02

### Fixed
- Birthday auto-population save was a no-op — `check_and_add_birthdays` mutated the dict in place so the scheduler's `if updated != current` guard always saw equal. Snapshot-and-compare fixed it.
- Birthday auto-population ran every minute (~1440 sheet reads/day per guild). Now gated to the first tick after daily reset.
- `/setup_birthdays` confirmation embed now tells users that auto-population runs once per day at server-time midnight.

## [1.0.9] — 2026-05-02

### Fixed
- Wizard views no longer hang on Discord interaction-token expiry. New `wizard_registry.safe_edit_response` falls back to `interaction.message.edit` on `NotFound`, swallows post-delete `HTTPException`. Threaded through ~100 callsites.
- `/cancel` now actually stops view-based wizard steps via `wizard_registry.wait_view_or_cancel` (~40 callsites in `setup_cog.py`).
- Survey too-long text input re-prompts the same question up to 5 times instead of cancelling the whole survey.
- CS draft renders full zone names from a single `CS_ZONE_STRUCTURE` source of truth (was rendering `Dc1` instead of "Data Center 1"; sub pairs were duplicated).
- `/sync_members` actually writes member counts — `intents.members = True` plus `_ensure_member_cache(guild)`. Side effect: `on_member_join` / `_remove` / `_update` now fire, so Member Roster auto-resync works.
- Custom event blurb shows up in `/events` announcements — `_resolve_event_info()` populates `name` + `blurb`; `build_announcement` re-resolves from `guild_events` if missing.
- Wizard "Use default" button no longer mislabels saved guild values; `ask_keep_or_change` split into separate `default=` and `current=` with a 3-button layout.

### Added
- Alliance-customisable DM bodies for birthday DMs, train DMs, and storm participation reminders. New Step 9 in `/setup_birthdays`, Step 8 in `/setup_train`, Step 7 in `/setup_desertstorm` and `/setup_canyonstorm`. Empty stored value = use bot's hardcoded default.
- Next-snapshot date displayed in `/setup_growth` and `/growth` (Discord-localized timestamp).
- 81 new tests across 7 background-task / view-callback paths that had shipped without dedicated coverage.

### Changed
- OGV strip — OGV (the bot owner's home alliance) no longer hardcoded as the canonical "test alliance". Goes through `/setup` like every other guild now. Premium gate moved from `ALWAYS_PREMIUM_GUILD_IDS` to a `PREMIUM_BYPASS_GUILD_IDS` env var. Framework defaults extracted from `config.py` into a new `defaults.py`. `/events` rewritten to use `guild_events` instead of OGV-fallback (was silently broken for non-OGV guilds). Test fixtures decoupled.
- Scheduler / publisher startup duplicate runs removed — each now runs exactly once on boot via the loop's immediate-fire.

### Documentation
- `docs/PREMIUM_SETUP.md` — added "Bot prerequisites" section covering the SERVER MEMBERS INTENT requirement.
- `docs/CONTENT_AUDIT.md` — `ask_keep_or_change` entry updated for the 3-button layout.

## [1.0.8] — 2026-05-02

### Removed
- Legacy-column shims (1.0.5 follow-up). Production confirmed `ALTER TABLE … DROP COLUMN` ran on Railway, so the defensive scaffolding is now dead weight: one-shot DROP COLUMN block, `get_config` row-dict filter, scheduler row-instantiation filter, and the `test_scheduler_row_instantiation_tolerates_legacy_columns` regression all removed.

## [1.0.7] — 2026-05-02

### Fixed
- Automated-post buttons (event editor, approval review, daily train reminder) no longer go silently dead on timeout. New `wizard_registry.expire_view_message` strips the buttons and appends *"⏰ The actions for this have timed out. Use `/X` to re-initiate."* Used by `EventEditorView`, `ApprovalView`, and `ReminderView`.

## [1.0.6] — 2026-05-02

### Fixed
- Scheduler crashed on production DBs carrying retired columns — Railway's SQLite < 3.35 silently no-op'd the 1.0.5 `DROP COLUMN`. Scheduler now applies `get_config`'s same `GuildConfig.__dataclass_fields__` filter before instantiation. (Superseded by 1.0.8 once the migration ran.)

## [1.0.5] — 2026-05-01

### Removed
- Physically dropped 10 retired `guild_configs` columns via one-shot `init_db()` migration: `storm_log_thread_id`, `tab_squad_powers`, `tab_growth_tracking`, `anchor_date`, `cycle_days`, `marauder_time_normal`, `siege_time_normal`, `marauder_time_saturday`, `siege_time_saturday`, `shield_warning_time`.

## [1.0.4] — 2026-05-01

### Changed — pre-launch polish (audit Round 4)
- `growth.py` worksheet-creation catch narrowed to `gspread.exceptions.WorksheetNotFound`; module docstring refreshed to per-guild flow.
- `storm.py` defaults stripped of OGV member names; `DEFAULT_A_ZONES` / `_B_ZONES` / `_A_SUBS` / `_B_SUBS` collapsed into one `DEFAULTS = {"A": ({}, []), "B": ({}, [])}`.
- Dead method/attribute pruning: `member_roster._format_roles(role_filter_id=)`, `TrainCog.reminder_sent_today`, `YesNoView` label kwargs, `BlurbChoiceView.has_existing`.
- Module-docstring refresh for `scheduler.py` and `train_birthdays.py`.

## [1.0.3] — 2026-05-01

### Changed — duplication + step-numbering cleanup (audit Round 3)
- One column-letter helper, not three. `setup_cog._col_letter_to_index` / `_col_index_to_letter` (AA+ aware) are now the single source of truth.
- `EventEditorView` content rendering deduplicated — five copies → one `_render_editor_content()` method.
- `_get_spreadsheet` is now one function. New `config.get_spreadsheet(guild_id)` helper; `growth.py` / `storm.py` / `storm_log.py` / `survey.py` delegate to it.
- Train themes/tones step migrated to `ask_keep_or_change` (was a hand-rolled `KeepOrChangeView` mislabelling saved values as "Defaults").
- `config.init_db` migration block deduplicated — four `event_*` ALTERs that ran twice now run once.
- `/setup_desertstorm` / `/setup_canyonstorm` step counter fixed from "Step X of 6" to "Step X of 7".

## [1.0.2] — 2026-05-01

### Removed — dead schema columns (audit Round 2)
- `storm_log_thread_id`, `tab_squad_powers`, `tab_growth_tracking`, `marauder_time_normal`, `siege_time_normal`, `marauder_time_saturday`, `siege_time_saturday`, `shield_warning_time`, `cycle_days`, `anchor_date` dropped from `GuildConfig` + CREATE TABLE. Existing physical columns left in production rows; new `config.get_config` filter swallows unknown row keys.
- `anchor_date_parsed()` helper.

## [1.0.1] — 2026-05-01

### Fixed
- `survey._run_schedule_wizard` broken import (`_parse_12h_time` lives in `setup_cog`, not `config`). The `# type: ignore` was masking a guaranteed `ImportError`.
- `train_ui.run_blurb_wizard_for_entry` write-only `existing = schedule.get(...)` line removed.

### Removed — dead code (audit Round 1)
- `sheets.py` (~85 LOC, OGV-era leftover; zero importers).
- `storm_log.py` legacy log path: `append_log_row`, `LOG_HEADERS`, `_ensure_headers`, `load_member_names`, `get_prior_sitouts` (~80 LOC).
- `scheduler.py` Friday shield workflow: `__noon_dt_for`, `post_shield_draft`, `SHIELD_REMINDER` (~30 LOC).
- `growth.py` OGV-era column-index constants.
- `survey.py` OGV-era constants: `SQUAD_TYPES`, `PROFESSIONS`, `BANNER_OPTIONS`, `AID_REMOVAL_OPTIONS`, `HISTORY_HEADERS`, `SURVEY_BUTTON_CUSTOM_ID_PREFIX` / `_RE`, `_to_millions`.
- `setup_cog.py` `ask_view` local helper inside `run_event_setup`.
- Various unused imports + re-exports (`storm.ZoneInfo`, `storm.ET`, `train.app_commands` / `tasks`, `survey.date_cls`, `bot.get_or_create_config`, `scheduler.init_db`, `config.set_member_tab`, `train_birthdays.get_birthday_lookahead`, `train_birthdays.DEFAULT_MEMBER_TAB`).
- Net: ~250 LOC of dead code.

## [1.0.0] — 2026-04-28

Initial public release.

### Added
- **Event announcements** — schedule recurring in-game events with leadership-approved drafts and 5-minute pre-event warnings (`/events`, `/events_log`).
- **Train schedule** — `/train` daily alliance-train assignment with inline edit; ChatGPT-prompt generation when configured.
- **Birthday tracking** — read birthdays from a configured Google Sheet; announce in Discord; auto-populate the train schedule on a member's birthday.
- **Desert Storm / Canyon Storm** — four-step mail flow with **Post & Copy** (`/desertstorm_draft`, `/canyonstorm_draft`); configurable participation logging (`/desertstorm_participation`, `/canyonstorm_participation`); past-log lookup (`/desertstorm_log`, `/canyonstorm_log`).
- **Squad-power survey** — `/survey_post` button → private thread → Sheets append; leadership summary in the notification channel.
- **Survey reminders** — `/survey_remind` for one-off or scheduled (channel post on free, DM on Premium).
- **Growth tracking** — `/growth` manual or scheduled snapshots (monthly / every-N-days).
- **Setup wizards** — `/setup` for core config; per-feature `/setup_*` for events / train / birthdays / storms / survey / growth / member roster.
- **`/view_configuration`** and **`/help`** management commands.

### Premium
LW Alliance Helper Premium ($4.99/mo via Discord App Subscriptions) unlocks:
- **Member Roster Sync** (`/sync_members`) — required for DM-based features.
- **Multi-survey** — `/survey` Add/Edit/Remove for multiple named surveys, each with its own questions / sheet tab / answer button.
- **DM-based reminders** for surveys and storms.
- **Scheduled survey reminders** delivered via DM to non-respondents.
- **Unlimited templates** — removes the free-tier mail/event template caps.

### Notes
- Earlier internal builds powered the OGV alliance during private testing.
- Bug reports / feature requests: <https://github.com/LW-Alliance-Helper/lw-alliance-helper.github.io/issues>.

[1.0.0]: https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/releases/tag/v1.0.0
