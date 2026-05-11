# LW Alliance Helper — project context

Solo Discord bot for Last War alliance leadership. Premium via Discord
App Subscriptions ($4.99/mo). Railway-hosted, SQLite + gspread +
Google Sheets backend.

**This file carries context across chat sessions.** New chats in this
repo auto-load this; chats outside this repo don't see it. Companion
repo `../lw-alliance-helper.github.io` (the website) has its own
`CLAUDE.md`.

---

## Working agreement

- **Solo project**, but the bot repo uses a release-branch workflow:
  work is tracked as GitHub issues; a feature branch (descriptive
  kebab-case slug, may bundle multiple related issues) is PR'd into
  the active `release/X.Y.Z` with a merge commit; the release branch
  is eventually PR'd into `main`. Railway deploys from `main`, so
  merging to main *is* the release. Delete feature branches after
  merge to release; delete release branches after they merge to
  `main` — the GitHub Release tagged on the merge commit is the
  historical record. See `feedback_release_workflow_bot.md` in
  Memory for the full rule.
- **Dev branch for major-change staging.** A long-lived `dev` branch
  backs a separate Railway service + separate Discord app for real
  end-to-end testing of high-blast-radius features (schema
  migrations, persistent Views, scheduler/startup-hook changes,
  anything that touches money paths). Routing:
  - **Major features:** feature → PR to `dev` (merge commit) → test
    on the staging server → PR `dev` into `release/X.Y.Z` →
    `main`.
  - **Small / doc changes:** feature → `release/X.Y.Z` → `main`,
    same as before. They skip `dev`.
  - **Hotfixes:** still direct to `main` per the hotfix rule below.
  - **Keep `dev` in sync:** when `main` moves forward and `dev`
    is *not* ahead with feature work in progress, fast-forward `dev`
    to `main`. If `dev` has uncommitted-to-main feature work, leave
    it alone — it'll resync after that feature ships.
  - **Versioning still happens on the release branch.** Don't bump
    `__version__` or write CHANGELOG entries on `dev`.
- **Backlog lives in [GitHub Project #2](https://github.com/orgs/LW-Alliance-Helper/projects/2).**
  Auto-add fires for both repos. Apply a label at issue-creation time
  (`bug` / `feature` / `documentation` / `hotfix`).
- **Project status updates automatically** via
  `.github/workflows/project-status-sync.yml`. An issue's Status field
  walks `Up Next → In progress → In review → Ready for Release →
  Shipped` based on where its linked PR lives (PR opened → In progress;
  push to `dev` → In review; push to `release/*` → Ready for Release;
  push to `main` → Shipped). Manual statuses still work for `Backlog`,
  `Up Next`, and `Canceled`. Driver: `closingIssuesReferences` on the
  PR — the issue must appear as `Closes #N` (or markdown-linked
  variant) in the PR body. Requires a `PROJECT_TOKEN` repo secret —
  fine-grained PAT with org-level `Projects: Read and Write` (the
  default `GITHUB_TOKEN` can't touch org Project v2). For one-off
  bootstraps, run `scripts/sync_project_status.py --issue N --status
  "..."` locally with `GH_TOKEN` exported.
- **Hotfix exception.** Direct-to-main is allowed for urgent one-line
  fixes, but only with explicit approval before each push. After a
  hotfix lands on main, fast-forward the active release branch to
  include it.
- **Versioning is per-release.** Branch name encodes the version
  (`release/1.0.16` → version `1.0.16`); one CHANGELOG entry per
  release covering all merged issues. Bump `bot.py.__version__` and
  write the CHANGELOG entry on the release branch right before
  opening the PR to main, not on individual feature branches. Sentry
  reads `__version__` for release tagging — keep it accurate.
- **Release-branch PR description is the slim CHANGELOG entry.** When
  opening `release/X.Y.Z` → `main`, paste the CHANGELOG section for
  that version into the PR body (plus a short "Closes #…" footer for
  every issue rolled in). Never leave the description blank — the
  release-on-main workflow uses the CHANGELOG section as the GitHub
  Release notes, and the PR body is what reviewers (and your future
  self when bisecting) see first.
- **Tests must pass before commit.** Pre-commit hook enforces this. If
  it fails: investigate the underlying issue, don't bypass with
  `--no-verify`.
- **Commit messages:** use HEREDOC, end with the
  `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`
  trailer.
- **Never amend** — always make a new commit, even after pre-commit
  hook failures.
- **Never `push --force` to main**, never `reset --hard`, never delete
  branches without confirming. Feature branches are deleted after
  merging into release; release branches are deleted after merging
  into main.
- **Companion repo `../lw-alliance-helper.github.io`** (the website)
  keeps the older direct-to-main rule — push commits straight to
  `main` there.
- **No time estimates, no S/M/L sizing.** Issues, proposals, and
  audit-style fix lists frame work by *what changes for users (or
  the bot's reliability)* and *why it matters*, not by hours or
  effort buckets. This is a side project; things take however long
  they take. Don't pad write-ups with "~2 hours", "small task",
  "large feature", phased rollouts, or stakeholder-style scoping.

---

## Repo layout

| File | Role | Size |
|---|---|---|
| `bot.py` | Entry point. Gateway intents (`members` is privileged), slash command tree. | ~790 LOC |
| `setup_cog.py` | Every `/setup_*` wizard. Largest file. | ~5000 LOC |
| `scheduler.py` | Background event scheduler — daily drafts, 5-min warnings, ApprovalView. | ~970 LOC |
| `train.py` / `train_cog.py` / `train_birthdays.py` / `train_ui.py` | Train schedule + birthday integration. Cog file separated from data layer for size. | ~1.8K total |
| `storm.py` / `storm_log.py` | Desert/Canyon Storm: drafts, participation, reminders. | ~2.5K total |
| `survey.py` | Squad-power surveys + scheduled reminders. | ~1.6K |
| `growth.py` | Growth-tracking snapshots. | ~300 |
| `member_roster.py` | Premium roster sync. **Requires `members` privileged intent.** | ~390 |
| `premium.py` | Central premium gating. Every premium check goes through here. | ~280 |
| `wizard_registry.py` | `wait_view_or_cancel` (cancel mid-wizard), `expire_view_message` (clean up timed-out auto-posts), `safe_edit_response` (survive interaction-token expiry). | ~200 |
| `defaults.py` | Hardcoded copy: themes/tones, default mail templates, default DM bodies. | ~100 |
| `help_content.py` | `/help` content + interactive `HelpView` dropdown. New categories = append a tuple to the right `HELP_CATEGORIES` entry. | ~270 |
| `dm.py` | DM helpers. | ~80 |
| `donate.py` | `/donate` and `/upgrade` commands. | ~135 |
| `config.py` | Schema, migrations, `get_*` / `save_*` helpers, gspread client. Also owns the `guild_install_metadata` table — operational record (guild name, owner, installer, install/last-seen timestamps) for support triage, refreshed on every `on_ready`. | ~1.5K |
| `stats_publisher.py` | Daily alliance-count publisher to website. | ~155 |

Tests: `tests/unit/` and `tests/integration/`. 610 collected, 18 skip
(intentional — `free_tier_only` markers under the `FORCE_PREMIUM=1` CI
lane).

---

## Patterns to reuse

These are deliberate and tested. Don't refactor away:

### Wizard "Use default vs Keep current vs Define my own"
- `setup_cog.ask_keep_or_change(default=, current=, ...)` — pass the
  hardcoded baseline as `default=` and the saved guild value as
  `current=`. Renders 2-button or 3-button view automatically.
- Don't pre-resolve to one value (the old pattern that mislabelled
  saved values as "Use default" — fixed in commit `2c577dd`).

### Cancellable view-based wizard steps
- `wizard_registry.wait_view_or_cancel(view, cancel_event)` for
  `view.wait()`. The `/cancel` command flips `cancel_event`. Without
  this helper, `/cancel` mid-wizard left views hanging until their own
  timeout fired and posted a misleading "⏰ Timed out" message.

### Auto-posted approval/review views must clean up on timeout
- Any background task that posts a `discord.ui.View` to a channel
  (daily event editor, the approval review that follows, the train
  reminder, etc.) must capture the sent message
  (`view.message = await ch.send(...)`) and override `on_timeout` to
  call `wizard_registry.expire_view_message(self.message,
  command_hint="/X")`.
- Without this, expired views render apparently-active buttons that
  fail with "Interaction failed" on click — there's no signal that
  the draft has gone stale. Canonical callsites:
  `scheduler.EventEditorView`, `scheduler.ApprovalView`,
  `train.ReminderView`.

### DM body templates (configurable per alliance)
- Schema column stores user template; empty string = "use hardcoded
  default".
- `_render_dm_body(template, name=...)` uses `SafeDict` so a typo
  placeholder like `{nme}` renders literally instead of crashing the
  reminder loop.
- Defaults live in `defaults.py` (or alongside the calling code as
  `DEFAULT_*` constants for storm).

### Schema migrations
- Add ALTER TABLE entries to the for-loop in `init_db()`. Each in
  try/except so re-runs don't crash. Log `[CONFIG] Added X to Y` on
  success.
- Update the corresponding `CREATE TABLE` for fresh DBs.
- Update `save_*_config` to write the new field.
- Update `get_*_config` fallback dict to include the new field.
- **Retiring a column?** Drop it from the dataclass, `CREATE TABLE`,
  *and* add a one-shot `ALTER TABLE … DROP COLUMN` to the migration
  block in the same release. Production SQLite supports
  `DROP COLUMN` (3.35+ confirmed on Railway). Don't leave
  retired-but-unmigrated columns around — `GuildConfig(**dict(row))`
  will TypeError if the row carries unknown columns. (Precedent:
  the 1.0.2/1.0.5/1.0.8 transition for `storm_log_thread_id` et al.
  added defensive filters that had to be removed once the DROP
  COLUMN ran.)

### Background `tasks.loop`
- Test by calling `task_name.coro(*args)` directly with patched
  dependencies. Don't try to start the loop in tests.
- See `bot.growth_task`, `train_cog.check_reminder`,
  `survey.check_scheduled_reminders` for canonical examples.

### Premium gating
- Every check via `await premium.is_premium(guild_id, ...)`.
- Never inline `if guild_id == X` bypasses.
- `PREMIUM_BYPASS_GUILD_IDS` env var = always-premium guild IDs (for
  owner's home alliance).
- `FORCE_PREMIUM=1` = every guild premium (local dev only).

### Inline imports
- `from config import X` *inside* a function is **deliberate** for
  late-binding under test patches. Don't refactor to module-level
  unless you also update every test that patches `config.X`.

---

## Test fixtures

- `seeded_db` (in `tests/conftest.py`) — temp SQLite with one
  fully-configured guild. Patches `config._get_conn` and
  `config.DB_PATH`.
- `temp_db` — temp SQLite, no seeded guild.
- `_isolate_premium_env` (where used) — pins
  `PREMIUM_BYPASS_GUILD_IDS` for tests. Reloads `premium` module to
  pick up the env change.
- `@pytest.mark.free_tier_only` — skipped under
  `FORCE_PREMIUM=1` CI lane (per `tests/conftest.py:34`).

### Common gotchas

- A function that newly calls `config.get_*` will need either
  `seeded_db` fixture or a `patch("config.get_*", ...)` mock — tests
  that don't set up a DB will hit
  `sqlite3.OperationalError: unable to open database file`.
  See `_bypass_guard` fixture in `tests/unit/test_storm_remind.py`
  for the right pattern.

---

## Recent shipped highlights

Versioned releases since 1.0.0 (the launch). See `CHANGELOG.md` for
the long form on each.

| Version | What |
|---|---|
| `1.1.7` | Hotfix: `/train` Add Entry and Update Entry modals now defer the interaction before their Google Sheets round-trip, so a slow gspread call no longer expires the 3-second initial-response token and crashes the submit with `NotFound 10062 Unknown interaction` ([#76](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/76)). Direct-to-main per the hotfix exception. |
| `1.1.6` | Operational record of bot installs ([#67](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/67)): new `guild_install_metadata` SQLite table captures guild name, owner ID, audit-log inviter, and install / last-seen timestamps per server, so logged `guild_id`s can be matched to an alliance for support. Owner-only `/admin_guild_info` and `/admin_forget_guild` slash commands scoped via the new `BOT_ADMIN_GUILD_IDS` env var, plus a `data_removal.yml` issue template and updated privacy/terms/README disclosures. |
| `1.1.5` | Numeric survey question type promoted from Premium to Free ([#64](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/64)) — min/max bounds remain the Premium differentiator. Numeric questions now require a magnitude (Exact / K / M / B), and `survey.ask_numeric` parses members' shorthand (`301` → 301M, `300m`, `1.2b`, `304,743,912`) into the stored full integer. Default LW survey questions ship as numeric with the right magnitude; a one-shot `init_db` backfill upgrades existing saved configs idempotently. Submission embed comma-formats numeric responses. |
| `1.1.4` | Hotfix: a single guild's `discord.Forbidden` on the configured birthday channel was aborting `train_cog.check_reminder`'s entire birthday loop for that minute, silently skipping every other guild. Per-guild `try/except` now isolates failures, and the channel-send path catches `Forbidden` specifically and logs `guild_id` + `channel_id` + channel name so leadership can be told which alliance has broken perms. Direct-to-main per the hotfix exception. |
| `1.1.3` | Storm time-slot rendering reworked ([#58](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/58)): DS and CS slots are game-defined constants (DS 18:00 + 23:00, CS 12:00 + 23:00 server time, UTC-2 / no DST), so `TimeSelectView` buttons now render `4pm EDT (18:00 server time)` style — local clock computed from the guild's `timezone` at click time, server-time portion always spelled out (no "ST" abbreviation). All six `time_option_*` columns dropped from `guild_storm_config` via `ALTER TABLE … DROP COLUMN`. `/growth` Edit Config button now opens the wizard inline instead of telling the user to run `/setup_growth` themselves ([#59](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/59)). Birthday parser accepts dash, dot, ISO 8601, abbreviated months, day-first (`7 Dec`, `7th December`), 2-digit years; bare numeric defaults to M/D unless first > 12; rejects impossible dates (`Feb 30`, `13/45`) instead of writing garbage ([#60](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/60)). |
| `1.1.2` | Hotfix: daily event announcements now print the local timezone alongside server time — `format_et` appends `dt.tzname()` so `{time}` renders as `5:00pm EDT` instead of bare `5:00pm`, leaving every existing custom blurb to surface the tz automatically. Add Event / Edit Time in the daily-draft editor used to call `make_et_datetime` which silently coerced every leadership-entered time to America/New_York; renamed to `make_event_datetime(tz=...)`, with Add Event looking up the per-event tz via `get_guild_event` and Edit Time preserving the existing `dt.tzinfo`. Direct-to-main per the hotfix exception. |
| `1.1.1` | Hotfix: `/help` rebuilt as a category-dropdown view (overview + `discord.ui.Select`) — the 1.1.0 data-ownership copy pushed the embed past Discord's 6000-char limit, causing `HTTPException 50035` on every invocation; new `help_content.py` module owns the content + view so future categories are an append, not a rewrite. Storm and train sheet-load logs now route through a new `config.describe_sheet_error` helper that distinguishes missing-tab from spreadsheet 404 / 403 / rate-limit, replacing opaque gspread reprs (e.g. `<Response [404]>`). Direct-to-main per the hotfix exception. |
| `1.1.0` | Premium per-user assignment layer ([#41](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/41)) — the SKU is now User Subscription, so the bot needs its own one-license-one-guild gate; new `/premium_assign` and `/premium_unassign` commands (with confirmation prompts) plus the `premium_assignments` SQLite table consulted on every premium check. Data-ownership story made explicit in README, welcome DM, `/help`, and `/upgrade` ([#39](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/39)). Setup wizard's "➕ Create a new channel" button no longer suppressed on Premium guilds ([#48](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/48)). Leadership commands no longer gated by channel category — role check is the security boundary, fixing `/cancel` mid-wizard and the empty-category edge case; `leadership_category_id` dropped via one-shot migration ([#49](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/49)). Working-agreement docs updated for the dev-branch staging workflow ([#36](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/36)) and the release-branch cleanup practice ([#46](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/46)). |
| `1.0.19` | Hotfix: growth snapshots called `ws.append_row` per new member inside the loop, so any first-ever snapshot of a populated roster (60+ members) blew the 60/min Sheets write quota and aborted with a 429 ([#40](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/40)). Collapsed into a single `ws.append_rows` after the loop. Direct-to-main per the hotfix exception. |
| `1.0.18` | Birthday → train auto-population now fires at 22:00 ET (10pm ET == 00:00 server time) instead of UTC midnight, and stops re-firing on every Railway redeploy ([#29](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/29)); plus a fleet-wide logging-gaps audit ([#31](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/31)) — DM-Forbidden now logs the (guild, user) pair, missing-channel scheduler/train/birthday fall-throughs log, `train.py` sheet I/O logs gain `guild_id`, `premium.is_premium` emits once-per-process warnings on missing SKU/bot, and several non-Discord exception paths now Sentry-capture instead of Railway-stdout-only. |
| `1.0.17` | Hotfix: `bot.entitlements()` was being called with the pre-2.4 `sku_ids=` kwarg instead of `skus=`, silently downgrading paying customers to free-tier in every background-task premium check ([#28](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/28)). Direct-to-main per the hotfix exception. |
| `1.0.16` | Docs-only release: slim CHANGELOG (746 → 159 lines), CLAUDE.md working-agreement rewrite for the new release-branch workflow, version-table sync to 1.0.15, and follow-up workflow corrections (merge commit, descriptive feature branches). Bumped `__version__` for accurate Sentry release tagging. |
| `1.0.15` | Sheet-CI rerun-filter fix — too-narrow `--only-rerun` filters were preventing legitimate quota-pressure retries on the live-Sheets job |
| `1.0.14` | Removed `docs/OGV_STRIP_INVENTORY.md` (resolved working doc; never linked) |
| `1.0.13` | README sync after post-1.0.11 audit (wizard step counts rewritten, customisable DM body row added, removed Canyon Storm fixed-time claim) |
| `1.0.12` | Fixed stale `__version__` constant (Sentry release tag was bucketing every error under `1.0.0`) and stale wizard step label `Step 6 of 7` → `of 8` |
| `1.0.11` | Doc sync that should have ridden with 1.0.7 (CLAUDE.md `wizard_registry` row + new auto-post-timeout pattern, CONTENT_AUDIT.md view-timeout rows) |
| `1.0.10` | Birthday → train auto-population: persistence bug fixed (in-place dict mutation defeated the change check) and gated to once-per-day instead of every-minute (was burning ~1440 sheet reads/day per guild) |
| `1.0.9` | Wizard views no longer hang on Discord interaction-token expiry — new `safe_edit_response` helper threaded through ~100 sites |
| `1.0.8` | Removed legacy-column shims (filter + scheduler patch + migration block) once production confirmed the 1.0.5 DROP COLUMN ran |
| `1.0.7` | Timed-out automated-post buttons now strip themselves and tell leadership how to re-open |
| `1.0.6` | (superseded by 1.0.8) Defensive scheduler filter for production DBs carrying retired columns — patched a misdiagnosed crash |
| `1.0.5` | Physically dropped 10 retired `guild_configs` columns via one-shot migration |
| `1.0.4` | Audit Round 4 — polish: dead local vars, narrow exceptions, sanitised storm defaults, dead `__init__` params, docstring refresh |
| `1.0.3` | Audit Round 3 — column-letter helpers consolidated, `EventEditorView` content rendering deduplicated, `_get_spreadsheet` extracted to `config.get_spreadsheet`, train themes/tones migrated to `ask_keep_or_change`, storm setup step counter `6 → 7` |
| `1.0.2` | Audit Round 2 — dropped 10 dead `guild_configs` schema columns + dataclass fields |
| `1.0.1` | Audit Round 1 — fixed `survey._run_schedule_wizard` broken import + dead `train_ui` line, deleted `sheets.py` and ~250 LOC of dead code (12 items) |
| `1.0.0` | Initial public release (2026-04-28) |

Test suite: **610 collected**, 18 skipped on the free-tier lane and
35 skipped under `FORCE_PREMIUM=1`. Total LOC: ~17K.

---

## Parked work (local-only docs)

These are untracked. Don't push them.

- **`AUDIT_2026-04-30.md`** — pre-launch code-quality audit, **fully
  shipped**. Rounds 1–4 landed as 1.0.1–1.0.4; the schema drops
  ride 1.0.5 + 1.0.8. Doc is kept as a record of how the audit was
  structured but should not generate new work.
- **`DESIGN_transfer_management.md`** — fully-iterated spec for a
  Premium transfer-tracking feature (sheet-watcher + filter wizard +
  in-game message templates). ~7 days of work. Post-launch v1.x.

When a chat session starts on `DESIGN_transfer_management.md`, that
doc is the ground truth.

---

## Strategic decisions (don't second-guess in passing)

These have been thought through. Reopening them needs a real reason:

- **Localisation:** English only at launch. Korean first when signal
  demands it (non-English alliances install but don't convert).
- **Pricing:** single Premium tier at $4.99/mo. Don't introduce a Pro
  tier without 6+ months of usage data. **Never** move existing
  Premium features to a higher tier — that's a takeaway and customers
  resent it.
- **Attribution footer:** post-first-customer, not pre-launch.
- **Self-host pivot:** kill criteria = user stops playing LW. Until
  then, hosted by user.
- **Game-agnostic abstraction:** keep architecture clean enough that
  a fork to another First Fun game (or similar mobile 4X) is plausible
  in 2–4 weeks if the opportunity arises. Don't preemptively
  abstract — fork when needed.

---

## Status snapshot

- 1.0.0 launched 2026-04-28. Currently on `1.1.7` — hotfix that
  defers the `/train` Add Entry / Update Entry modal interactions
  before their Google Sheets round-trip, so a slow gspread call no
  longer expires the 3-second response token and crashes the modal
  submit with `NotFound 10062 Unknown interaction`
  ([#76](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/76)).
  See `CHANGELOG.md` for per-version detail.
- 610 tests collected; 18 skipped on the free-tier lane.
- Pre-launch audit fully shipped (Rounds 1–4 → 1.0.1–1.0.4; schema
  drops → 1.0.5 + 1.0.8). No outstanding cleanup from that audit.
- Transfer management feature designed, not built (see
  `DESIGN_transfer_management.md` and issue
  [#16](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/16)).

For per-version detail, see `CHANGELOG.md`. New in-flight work goes
on a descriptive feature branch (which may bundle several related
issues) → PR into the active `release/X.Y.Z` branch with a merge
commit. The release branch is where the next release accumulates
before its own PR into `main`.
