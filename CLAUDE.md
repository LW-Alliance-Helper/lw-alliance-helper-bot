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
  every change → GitHub issue → feature branch (`issue-NN-slug`) → PR
  into the active `release/X.Y.Z` → merge commit → release branch
  eventually PR'd into `main`. Railway deploys from `main`, so merging
  to main *is* the release. Delete feature branches after merge to
  release; **keep** release branches as history. See
  `feedback_release_workflow_bot.md` in Memory for the full rule.
- **Backlog lives in [GitHub Project #2](https://github.com/orgs/LW-Alliance-Helper/projects/2).**
  Auto-add fires for both repos. Apply a label at issue-creation time
  (`bug` / `feature` / `documentation` / `hotfix`).
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
- **Tests must pass before commit.** Pre-commit hook enforces this. If
  it fails: investigate the underlying issue, don't bypass with
  `--no-verify`.
- **Commit messages:** use HEREDOC, end with the
  `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`
  trailer.
- **Never amend** — always make a new commit, even after pre-commit
  hook failures.
- **Never `push --force` to main**, never `reset --hard`, never delete
  branches without confirming. Release branches are preserved as
  history; feature branches are deleted only after their merge into
  release.
- **Companion repo `../lw-alliance-helper.github.io`** (the website)
  keeps the older direct-to-main rule — push commits straight to
  `main` there.

---

## Repo layout

| File | Role | Size |
|---|---|---|
| `bot.py` | Entry point. Gateway intents (`members` is privileged), slash command tree. | ~940 LOC |
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
| `dm.py` | DM helpers. | ~80 |
| `donate.py` | `/donate` and `/upgrade` commands. | ~135 |
| `config.py` | Schema, migrations, `get_*` / `save_*` helpers, gspread client. | ~1.5K |
| `stats_publisher.py` | Daily alliance-count publisher to website. | ~155 |

Tests: `tests/unit/` and `tests/integration/`. 538 collected, 18 skip
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

Test suite: **538 collected, 18 skipped** (matches CI lane). Total
LOC: ~17K.

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

- 1.0.0 launched 2026-04-28. Currently on `1.0.10` (see
  `CHANGELOG.md` for the per-version detail).
- 538 tests collected, 18 skipped under CI's `FORCE_PREMIUM=1` lane.
- Pre-launch audit fully shipped. No outstanding cleanup queued.
- Transfer management feature designed, not built (see
  `DESIGN_transfer_management.md`).

For per-version detail, see `CHANGELOG.md`. The `[Unreleased]` block
above the latest version is where new in-flight work goes before
cutting the next release.
