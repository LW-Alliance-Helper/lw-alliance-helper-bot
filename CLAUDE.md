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
  - **`dev` carries the next patch `__version__` over `main`** (e.g.
    `main` at `1.4.5` → `dev` at `1.4.6`) so the staging Railway
    service's Sentry release tag is distinct from production's and
    staging errors don't get bucketed under the shipped version. Bump
    it whenever `main` moves forward. The CHANGELOG entry and the
    final release version are still settled on the release branch —
    don't write CHANGELOG entries on `dev`.
- **Backlog lives in [GitHub Project #2](https://github.com/orgs/LW-Alliance-Helper/projects/2).**
  Auto-add fires for both repos. Apply a label at issue-creation time:
  - `feature` — large work warranting a minor/major version bump. Multiple
    sub-tickets, days of design discussion, real user testing. Examples:
    [#16](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/16),
    [#55](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/55),
    [#56](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/56).
  - `enhancement` — smaller-than-feature improvements that land in a patch
    bump. Single-PR scope, mirrors existing functionality, or polish of a
    shipped surface. Examples:
    [#249](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/249),
    [#258](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/258).
  - `bug` — broken behavior or UX-clarity fixes (e.g. a confusing DM, a
    silent failure).
  - `documentation` — README / CLAUDE.md / docs/ / website copy changes.
  - `hotfix` — urgent direct-to-main fix per the hotfix exception below.
- **Project status updates automatically** via
  `.github/workflows/project-status-sync.yml`. An issue's Status field
  walks `Up Next → In progress → In review → Ready for Release →
  Shipped` based on where its linked PR lives (PR opened → In progress;
  push to `dev` → In review; push to `release/*` → Ready for Release;
  push to `main` → Shipped). Manual statuses still work for `Backlog`,
  `Up Next`, and `Canceled`. Driver: the PR body — the script merges
  GitHub's `closingIssuesReferences` (which only auto-populates for
  PRs into `main`) with a direct regex scan for `Closes / Fixes /
  Resolves #N` and markdown-linked variants. The body has to contain
  one of those keywords against each issue you want walked.
  Requires a `PROJECT_TOKEN` repo secret —
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
- **Pre-commit hooks run on staged files** (`pre-commit` framework, config
  in `.pre-commit-config.yaml`): stock `pre-commit-hooks` file checks
  (check-merge-conflict, check-yaml, check-toml, check-added-large-files),
  ruff lint + ruff format (line-length 100), codespell, a gitleaks
  staged-secret scan, and `actionlint` on `.github/workflows/*.yml`. Install
  once per clone with `py -m pre_commit install`. If a hook fails:
  investigate and fix, don't bypass with `--no-verify`. ruff config is in
  `ruff.toml`, codespell's ignore list in `.codespellrc`. actionlint runs via
  the `actionlint-py` pip wrapper (Go isn't installed, so the upstream
  go-language hook can't build — same reason gitleaks runs as a system
  binary).
  - **ruff lint scope is bugs + dead code only** (`E9` + pyflakes `F`).
    **F401 (unused import) and F811 (redefinition) are deliberately OFF** —
    they break this repo's module re-exports (e.g. `train.py`) and inline
    late-binding `from config import X` imports, and their autofix silently
    deleted both during the initial sweep. Don't re-enable them without
    `# noqa`-ing every re-export and inline-import site first.
  - **ruff format reflows to its own style**; the whole tree was formatted
    once at line-length 100. It does NOT split `if x: return` (that's a lint
    rule we don't enable).
- **Tests are NOT in the commit hook.** They run in CI (`test.yml`) and
  targeted-per-issue locally; the full suite (~5 min) runs at the end of a
  batch. The full suite is the real safety net for sweeps — it's what caught
  the ruff-autofix import regressions. Don't wire it into pre-commit.
- **Commit messages:** Conventional Commits style (`type(scope): summary` —
  `feat` / `fix` / `docs` / `refactor` / `test` / `chore` / `build`),
  written via HEREDOC. Not enforced by a hook. **No `Co-Authored-By` /
  attribution trailer** — the user opted out of it.
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
| `setup_cog.py` | The `/setup` hub launcher (`setup_hub`) + every feature wizard (foundations, birthdays, growth, storm, members, shiny tasks, etc.), reachable as hub buttons. Largest file. | ~5000 LOC |
| `scheduler.py` | Background event scheduler — daily drafts, 5-min warnings, ApprovalView. `iter_guild_event_drafts` (the per-guild draft computation) is extracted so the live loop and the #227 catch-up scan share one code path. | ~970 LOC |
| `outage_catchup.py` | Outage catch-up digest (#227). Detects downtime from the per-minute loop heartbeats, scans every clock-driven surface (event draft, shiny, survey, birthday, train, storm sign-up) for posts missed during the window that are still in their catch-up window, and posts one leadership-channel digest with a multi-select + Send/Dismiss view for one-click recovery. Per-surface adapters; Premium re-checked at fire time for the paid paths. | ~840 LOC |
| `train.py` / `train_cog.py` / `train_birthdays.py` / `train_ui.py` | Train schedule + birthday integration. Cog file separated from data layer for size. | ~1.8K total |
| `train_rotation.py` / `train_rotation_ui.py` / `train_hub.py` | Train Conductor Rotation (#55, free, opt-in): deterministic selection algorithm + `Train History`/`Member Rules`/`Day Rules` Sheet I/O; UI = buffered preset editor, weekly draft view, daily confirmation view. `train_hub.py` is the single `/train` hub (embed + button grid, Events-hub pattern) that fronts both rotation and the legacy blurb surface. The `check_rotation` loop (weekly draft + daily confirm) lives in `train_cog.py`; rotation gates on the `rotation_enabled` train-config flag. No strategy axis — auto/manual is derived from rule type + role; per-rule-type roles scope candidate pools; birthday mode is derived from the Birthday setup. | ~2.8K total |
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
| `config.py` | Schema, migrations, `get_*` / `save_*` helpers, gspread client. Also owns the `guild_install_metadata` table — operational record (guild name, owner, installer, install/last-seen timestamps) for support triage, refreshed on every `on_ready` — and the `loop_heartbeat` table (one row per background loop, stamped at each clean tick; powers the #227 outage catch-up). | ~1.5K |
| `stats_publisher.py` | Daily alliance-count publisher to website. | ~155 |
| `shiny_tasks.py` | Daily Shiny Tasks announcement (3-day cycle math + render). Per-minute post loop and weekly refresh loop live in `bot.py`. Free for all tiers. **Refresh is disabled (`SERVER_REFRESH_ENABLED=False`, #293)** — the upstream source gated its data behind an API key, so the feature serves the frozen `shiny_task_servers` snapshot and new servers are added manually. See `docs/hedge_data_source.md`. | ~250 |

Tests: `tests/unit/` and `tests/integration/`. 2334 collected, 18 skip
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
- **Clock-driven loops stamp a heartbeat** at the end of each clean tick
  via `config.stamp_loop_heartbeat("<name>")` so the #227 outage catch-up
  can detect downtime. The four per-minute loops (`shiny_post`,
  `survey_reminder`, `train_reminder`, `storm_signup`) are the reliable
  outage signal; `scheduler` stamps too but is excluded from window
  detection (variable sleep). Adding a new clock-driven member-facing
  post? Stamp a heartbeat **and** add a per-surface adapter to
  `outage_catchup.SURFACE_ADAPTERS` so an outage doesn't silently eat it.
  Tests that exercise a loop without a real DB must patch
  `config.stamp_loop_heartbeat` to a no-op.

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
| `1.5.2` | Outage catch-up ([#227](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/227)): on return from downtime the bot posts one leadership-channel digest of every clock-driven post it missed (event draft, Shiny, survey, birthday, train, storm sign-up) with a multi-select Send/Dismiss view — `outage_catchup.py` + per-loop `loop_heartbeat` stamps. Member stats ([#56](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/56), [#299](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/299)): `/my_stats` (member-safe self view) + `/member_stats` (leadership picker) consolidate identity/power/storm/train/survey into one embed; storm section adds sign-up counts, primary/sub/sit-out placement, and leadership-only recency dates. Buddy engineer reliability ranking ([#303](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/303)): optional 1-5 score (Step 5a, train-rotation-style Keep/Default/Custom; matches members like power reading) orders engineers so the most reliable pair with the strongest War Leaders; Re-pair from scratch applies it. Train Conductor Rotation setup reworked into its own gated Step 9 with lettered sub-steps, condensed sheet-tabs, reworked preset editor, roster-based conductor picker ([#302](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/302)). Stale slash/button refs swept after the train/events/storm hub consolidations ([#298](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/298)). |
| `1.5.1` | Bug-fix batch: buddy self-profession-change sends one DM listing all your buddies; `/setup` survives a DM context ([#271](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/271)); storm sign-up/roster screens stop hitting the Sheets read limit on quick click-through ([#269](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/269)); storm roster builder ignores a leftover prior-event draft ([#277](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/277)); Shiny Tasks keeps posting from the saved server list with the upstream refresh disabled ([#293](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/293)). |
| `1.5.0` | Train Conductor Rotation ([#55](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/55), free/opt-in): deterministic daily conductor rotation with presets, per-member/per-day rules, weekly draft + daily confirmation in the `/train` hub. Profession Buddy System ([#289](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/289)): pair War Leaders with Engineers; free buddy lookup, Premium auto-assign / re-pair / DMs. Storm sign-up officer buttons to clear all or on-behalf votes ([#287](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/287)). Setup step-timeout crash fixed ([#290](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/290)). |
| `1.4.7` | Hotfix: **Today's events** opens the editor even when every event is Manual, so you can add a one-off to today's draft ([#291](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/291)). Direct-to-main per the hotfix exception. |
| `1.4.6` | `/events` becomes a hub command with a preset library matching `/desertstorm` / `/canyonstorm` ([#249](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/249)); consistent wording across wizards/errors/timeouts ([#267](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/267), [#208](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/208)); storm fixes — Strength-to-priority balances power across shared-priority buildings ([#273](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/273)), return a sub to the pool ([#274](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/274)), no double-pool players once placed ([#275](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/275)). |
| `1.4.5` | Hotfix: choosing **Edit** to paste a custom roster DM template during Premium storm setup no longer crashes the wizard — the structured-flow Edit branch called `bot.wait_for(check=check)` without defining `check`. Surfaced by the ruff `F821` lint sweep landing on `dev`. Direct-to-main per the hotfix exception. |
| `1.4.4` | Hotfix: Team A / Team B plan picker lists candidate members by name instead of their raw Discord ID ([#270](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/270)). Direct-to-main per the hotfix exception. |
| `1.4.3` | Hotfix: storm roster readers fall back to the Name column (then the live Discord member) when Display Name is blank, so the sign-up poll and Team Plan render names instead of IDs ([#268](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/268)). Direct-to-main per the hotfix exception. |
| `1.4.2` | Sign-up vote click shows a poll-style ephemeral with per-option totals and a ✓ on your vote, plus a leadership 👁️ View sign-ups breakdown ([#258](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/258)); Premium stale-power DM nudges members whose roster power hasn't refreshed in N days ([#255](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/255)); sign-up messages can be re-posted with votes aggregating across every post ([#265](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/265)). Name-match column renamed Member-match for clarity ([#260](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/260)); member sync preserves hand-typed non-Discord roster rows ([#262](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/262)); power-refresh DM leads with the ✅ vote-recorded confirmation so it isn't mistaken for a failure ([#259](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/259)). |
| `1.4.1` | Hotfix: power-refresh DM names the column on the configured Power Data Source tab instead of always reading the Member Roster ([#256](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/256)). Direct-to-main per the hotfix exception. |
| `1.4.0` | Premium Storm Overhaul ([#233](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/233)): structured sign-up → roster builder → PNG mail flow with auto-fill, per-event team plan picker, per-team time-slot mapping with weekly override, per-member assignment DMs with role-keyed templates, unified DS + CS mail body, and `/desertstorm` / `/canyonstorm` event hubs that consolidate every storm action under one command per event type. Participation Tracking 2.0 (Premium, [#243](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/243)): per-member question types written to a Per-Member Log tab, parameterized Trends Viewer for cross-event queries, and preset question templates during setup. Member Sync renamed with Power Data Source flexibility, collision protection, and a presence column surfaced in the sync preview; storm + participation now share one Alias Column instead of duplicating it. 📢 Release announcements toggle lands on the `/setup` hub — the first leadership-channel embed posts to every alliance as part of this release ([#253](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/253) infra shipped in 1.3.4). Setup wizard re-entry covers mail template choices ([#231](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/231)) and the shared/separate picker without clobbering saved bodies; officers with the Leadership role can run `/setup` ([#229](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/229)) without server-admin permission. Stale post-consolidation slash refs in Steps 5 and 9 of the storm wizard fixed ([#242](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/242)). |
| `1.3.4` | Release-announcement infrastructure ([#253](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/253)): `last_seen_version` column on `guild_install_metadata`, `release_announcements_enabled` on `guild_configs`, and an `on_ready` handler that posts a short embed to each alliance's leadership channel when the running version's major.minor changes. The `RELEASE_ANNOUNCEMENTS` dict is empty in 1.3.4 itself so the deploy is silent; existing rows backfill to `'1.3.3'` so 1.4.0 fires the first real announcement. Opt-out toggle ships with 1.4.0's `/setup` hub. Changelog-slim hook resolves absolute hook paths to repo-relative so historical bullets stop flagging as new violations ([#250](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/250)). |
| `1.3.0` | Setup wizard re-entry UX overhaul ([#80](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/80)): every `/setup_*` command (plus `/setup_members`) opens with a saved-config summary on re-entry; Keep current buttons across every channel, role, timezone, sheet ID, time, default tone, intro message, and `ask_keep_or_change` step; enable-toggle wizards (`/setup_birthdays`, `/setup_growth`, `/setup_shiny_tasks`) preserve config on disable with an optional 🗑️ Clear my saved configuration button. Shiny-tasks weekly refresh no longer thrashes cpt-hedge on every Railway redeploy — gated on the last-seen timestamp in `shiny_task_servers` ([#109](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/109)). |
| `1.2.0` | Growth Breakdown classifies snapshot deltas into Increased / Steady / Low / None / Decline buckets, with optional Premium auto-post + bucket filter + custom thresholds/labels ([#34](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/34)). Daily Shiny Tasks free-tier announcement posts every LW server in the alliance's transfer range that has shiny tasks today, refreshed weekly from cpt-hedge ([#72](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/72)). `/export_config` + `/import_config` move config across guilds via JSON with a channel/role remap wizard ([#42](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/42)). DS/CS zones lock to canonical game-defined names ([#35](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/35)); DS/CS subs flatten to plain name lists ([#37](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/37)). Multiple breakdown auto-post fixes ([#84](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/84), [#85](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/85), [#87](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/87)) and birthday→train conflict spam consolidated with restart-survival via persisted dedup ([#89](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/89)). |
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

Test suite: **2334 collected**, 18 skipped on the free-tier lane and
35 skipped under `FORCE_PREMIUM=1`. Total LOC: ~50K.

---

## Parked work (local-only docs)

Working docs that don't belong in the public tree live under `notes/`
(gitignored). They're cross-session scratch space for planning, design
parks, audit notes, recruiter prep, and ad-hoc test plans. Anything
that should ship as a tracked reference belongs in `docs/` instead.

Current contents (worth being aware of when picking up new work):

- **`notes/AUDIT_2026-04-30.md`** — pre-launch code-quality audit,
  **fully shipped**. Rounds 1–4 landed as 1.0.1–1.0.4; the schema
  drops ride 1.0.5 + 1.0.8. Doc is kept as a record of how the audit
  was structured but should not generate new work.
- **`notes/DESIGN_transfer_management.md`** — fully-iterated spec for
  a Premium transfer-tracking feature (sheet-watcher + filter wizard +
  in-game message templates). Post-launch v1.x.
- **`notes/PLANNING.md`** — cross-session work tracker.
- **`notes/DEV_TEST_PLAN_*.md`** — ad-hoc test plans for a specific
  release-batch dev validation session. Delete after the batch ships
  unless something in there warrants tracking as an issue.

When a chat session starts on `notes/DESIGN_transfer_management.md`,
that doc is the ground truth.

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

- 1.0.0 launched 2026-04-28. `release/1.5.2` is cut from `dev` and
  PR'd into `main` (production currently `1.5.1`; merging the PR ships
  `1.5.2`). It rolls up the outage catch-up digest ([#227](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/227)),
  `/my_stats` + `/member_stats` member lookup ([#56](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/56),
  [#299](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/299)),
  buddy engineer reliability ranking ([#303](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/303)),
  the Train Conductor Rotation setup rework ([#302](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/302)),
  and a stale-ref sweep ([#298](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/298)).
  The two big preceding releases: 1.5.0 (Train Conductor Rotation
  [#55](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/55)
  + Profession Buddy System [#289](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/289))
  and 1.4.0 (Premium Storm Overhaul [#233](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/233)
  + Participation Tracking 2.0 [#243](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/243)).
  See `CHANGELOG.md` and the Recent shipped highlights table for
  per-version detail.
- ~2085 tests pass on the default (non-sheets) lane.
- Repo tooling (shipping with 1.4.6): pre-commit runs stock
  `pre-commit-hooks` file checks (merge-conflict / yaml / toml /
  large-files), ruff lint + format (line-length 100), codespell, a
  gitleaks staged-secret scan, and `actionlint` on the workflow files;
  the whole tree was formatted once at line-length 100. See the Working
  agreement for the deliberate F401/F811 caveat. A `.github/dependabot.yml`
  (weekly pip + github-actions update PRs) also rides this release —
  Dependabot reads its config from the default branch, so it activates
  once 1.4.6 lands on main.
- Pre-launch audit fully shipped (Rounds 1–4 → 1.0.1–1.0.4; schema
  drops → 1.0.5 + 1.0.8). No outstanding cleanup from that audit.
- Transfer management feature designed, not built (see
  `notes/DESIGN_transfer_management.md` and issue
  [#16](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/16)).

For per-version detail, see `CHANGELOG.md`. New in-flight work goes
on a descriptive feature branch (which may bundle several related
issues) → PR into the active `release/X.Y.Z` branch with a merge
commit. The release branch is where the next release accumulates
before its own PR into `main`.
