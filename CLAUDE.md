# LW Alliance Helper — project context

Solo Discord bot for Last War alliance leadership. Premium via Discord
App Subscriptions ($4.99/mo). Railway-hosted, SQLite + gspread +
Google Sheets backend.

**This file carries context across chat sessions.** New chats in this
repo auto-load this; chats outside this repo don't see it. Companion
repo `../lw-alliance-helper.github.io` (the website) has its own
`CLAUDE.md`.

**Changing anything a user sees? Read `notes/UX.md` and
`notes/DESIGN.md` first.** They're the contract for every user-facing
surface, and they live in the **private** notes repo rather than here.
This file owns engineering patterns, workflow, and release process;
those two own the product's users, language, and visual conventions,
and `messages.py` owns the shared copy constants themselves.

| File | Owns | Read it when |
|---|---|---|
| `notes/UX.md` | Audiences, operating constraints, principles, interaction standards, naming, glossary, voice | Any slash command, hub, wizard, embed, button label, DM, scheduled post, or error message changes |
| `notes/DESIGN.md` | Surface types, color semantics, emoji catalog, button styles + grid, embed anatomy, Discord limits, ephemerality, view timeouts | Same trigger, plus anything that renders |

`/ux-review` runs both as a checklist: with no argument it audits the
diff; given a surface name or issue it produces a pre-flight brief
before the code is written. Without the notes repo cloned it has no
contract to check against and will say so instead of guessing.

**They're private deliberately** (settled 2026-08-08). They're our own
design and UX reasoning, which is worth more to a competitor than to
any user, and the closest competitor's known weakness is precisely a
lack of this kind of context. Don't copy them into this tree, don't
quote them into this file, and see `notes/README.md` for the full
reasoning. Verifying their contents is
[#451](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/451).

---

## Working agreement

- **Bot work starts in this folder.** Skills, this file and the project
  memory load from the folder a session *starts* in, and from nowhere
  else: registering this folder as an additional directory from a session
  started elsewhere does not load them (verified 2026-09-09, when the
  Skill tool answered "unknown skill" with the folder registered and every
  skill on disk). A session working on the bot from another repo's folder
  is working without this contract, and that is what "sloppy" sessions
  turned out to be. Open the bot folder; then start.
- **Kevin works in UIs, not the terminal. Design for that, always.**
  A terminal step is the path of least resistance for an agent and the
  path of most resistance for him — if there is any other way to do a
  thing, build that way instead. Prefer, in order: a Discord command or
  hub button, a web UI, a file he can drag into Discord, a dashboard
  toggle. A CLI script is a last resort, and when one is genuinely
  unavoidable it needs a one-line invocation, no environment plumbing,
  and no expectation that he keeps a shell open.
  This is not a preference to accommodate where convenient — it decides
  designs. `/admin champion_duel_import` taking a JSON attachment exists
  because the first version needed a public host, a service key, three
  environment variables and a shell with a second repo checked out. Same
  data, same gate, same data-layer functions; the delivery mechanism was
  the entire difference between usable and not. Ask "what does he
  actually have to *do*" before calling something finished.
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
  - **Hotfixes:** straight at `main`, but via a PR, per the hotfix
    rule below. They skip `dev` and the release branch, not review.
  - **Keep `dev` in sync:** when `main` moves forward and `dev`
    is *not* ahead with feature work in progress, fast-forward `dev`
    to `main`. **When `dev` is ahead and `main` takes a hotfix, merge
    `main` into `dev` by pull request the same day**, before the next
    feature merges into `dev`, while the conflict is one commit wide.
    The old rule had only two cases, fast-forward or leave alone, and
    "leave alone" for a month produced an eleven-file conflict
    ([#588](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/pull/588):
    three releases and a CI fix, resolved by hand). A hotfix PR into
    `main` is not finished until its sync PR into `dev` is open.
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
  - `hotfix` — urgent fix PR'd straight into `main` per the hotfix
    exception below.
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
- **Hotfix exception.** Urgent fixes skip the release branch and go
  straight at `main` — but through a **PR**, never a direct push. Taking
  the hotfix path needs explicit approval; once it has that, the session
  carries it to completion without checking back again:
  1. Branch, fix, open the PR into `main`.
  2. **Watch the checks through to the end.** `Sheet Integration Tests`
     only runs on PRs into `main`, and only starts once the unit lane
     has passed, so it appears late — a PR that looks green a minute
     after opening usually has not run it yet. Opening the PR is not
     the end of the task.
  3. All checks green clears the merge. Merge it; no second approval.

  A red check is never a merge, and step 3's clearance does not apply to
  one. Re-run it if it looks transient, and if it fails again, bring it
  back rather than merging past it.

  Direct-to-main is what 1.8.10 did, and it is why this rule changed: a
  direct push gets no sheet coverage and nothing gates the merge, so a
  CI failure that withheld the Railway deploy went unnoticed until the
  bug it was fixing fired again in production that evening
  ([#569](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/569)).

  After the hotfix lands on main, fast-forward the active release branch
  to include it.
- **Versioning is per-release.** Branch name encodes the version
  (`release/1.0.16` → version `1.0.16`); one CHANGELOG entry per
  release covering all merged issues. Bump `bot.py.__version__` and
  write the CHANGELOG entry on the release branch right before
  opening the PR to main, not on individual feature branches. Sentry
  reads `__version__` for release tagging — keep it accurate.
- **Release-branch PR is titled `Release X.Y.Z`.** Plain, no
  conventional-commit prefix — a `chore(release):` title makes the one
  PR that ships to production read like a dependency bump in the PR
  list. An optional short suffix is fine on a headline release
  (`Release 1.6.0: Transfer Management`, `Release 1.5.0 — Train
  Conductor Rotation + Profession Buddy System`); recent patch releases
  are plain. Note the *commit* on the release branch is the opposite:
  it does use `chore(release): X.Y.Z - <summary>`.
- **Release branches also write the Discord changelog block.** Add the
  release's post to `docs/DISCORD_CHANGELOG.md` next to the CHANGELOG
  entry. The **bot** posts it to the support server's `#changelog` from
  `on_ready` (`changelog_post.py`), not CI — the release workflow runs
  before Railway finishes deploying, so a post from there could announce
  a release that then fails to deploy ([#92](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/92)).
  The destination is the `CHANGELOG_CHANNEL_ID` env var (deploy config,
  so it survives a volume reset and the staging service's is visibly
  separate); `/admin changelog` shows status, previews a version, or
  re-posts. Consecutive releases inside 12 hours append to the running
  message instead of each pinging.
  The post is *not* derived from CHANGELOG.md — it merges related
  bullets, drops anything an alliance can't act on, and caps at five
  lines. Rules and a worked example are in that file's preamble.
  **Every release posts.** `release-changelog-check.yml` fails the
  release PR when `__version__` moves without a matching block, so a
  release can't ship without one; to skip one deliberately, write its
  block with a `NO POST: <reason>` line rather than leaving a hole. The
  announcements channel stays fully manual.
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
- **Worktrees live under the session scratchpad, and only there.**
  Parallel sessions share this checkout, so branch work happens in
  `git worktree add <scratchpad>/wt-<slug> -b <branch> origin/dev`, and
  the worktree is removed (`git worktree remove`) the moment its branch
  merges. Never a sibling folder beside the repo, never `C:/tmp`: on
  2026-09-10 the cleanup found 36 stale worktrees, two sibling folders and
  a stale copy of this file in `C:/tmp` that every worktree under it had
  been reading. A worktree has no `notes/`, no `.venv` and no
  `champion_duel_engine`; run tests with the main checkout's interpreter
  by absolute path.
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
| `bot.py` | Entry point. Gateway intents (`members` is privileged), slash command tree, `on_ready`, and the four `@tasks.loop` background loops (`growth_task`, `stats_publish_task`, `shiny_tasks_refresh_task`, `shiny_tasks_post_task`). The loops and the ~241-line `on_ready` are the unfinished half of [#372](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/372) — don't assume the split is done. | ~1.3K |
| `bot_admin.py` / `bot_state.py` | Owner-only `/admin` toolkit extracted from `bot.py` ([#372](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/372), partial), plus the shared bot-state accessor both sides import to avoid a circular import. | ~1K total |
| `api_server.py` | aiohttp server backing the Map Manager integration. Gated by `MAPMANAGER_API_KEY`; the Procfile runs the bot as a `web` service for it. | ~175 |
| `setup_cog.py` | The `/setup` hub launcher (`setup_hub`) + every feature wizard (foundations, birthdays, growth, storm, members, shiny tasks, etc.), reachable as hub buttons. Largest file in the repo. | ~11.2K |
| `scheduler.py` | Background event scheduler — daily drafts, 5-min warnings, ApprovalView. `iter_guild_event_drafts` (the per-guild draft computation) is extracted so the live loop and the #227 catch-up scan share one code path. | ~970 LOC |
| `outage_catchup.py` | Outage catch-up digest (#227). Detects downtime from the per-minute loop heartbeats, scans every clock-driven surface (event draft, shiny, survey, birthday, train, storm sign-up) for posts missed during the window that are still in their catch-up window, and posts one leadership-channel digest with a multi-select + Send/Dismiss view for one-click recovery. Per-surface adapters; Premium re-checked at fire time for the paid paths. | ~840 LOC |
| `train.py` / `train_cog.py` / `train_birthdays.py` / `train_ui.py` | Train schedule + birthday integration. Cog file separated from data layer for size. | ~1.8K total |
| `train_rotation.py` / `train_rotation_ui*.py` / `train_hub.py` | Train Conductor Rotation (#55, free, opt-in): fairness selection (fewest drives → oldest last-driven → **stable random** tie-break seeded by the day, replacing the old alphabetical fallback) + `Train History`/`Member Rules`/`Day Rules` Sheet I/O. Fairness counts the **whole** history sheet as fact — any membered row counts (no posted/reason needed, blank reason counts), only the drafted week + future excluded via the `before` boundary; identity is **Discord-ID-first, name-fallback** (`canonicalize_history` + the appended `Discord ID` history column, stamped on write via `roster_id_map`). UI = buffered preset editor, weekly draft view (with ◀/▶ week nav), daily confirmation view. `train_hub.py` is the single `/train` hub (embed + button grid, Events-hub pattern) that fronts both rotation and the legacy blurb surface. The `check_rotation` loop (weekly draft + daily confirm) lives in `train_cog.py`; rotation gates on the `rotation_enabled` train-config flag. No strategy axis — auto/manual is derived from rule type + role; per-rule-type roles scope candidate pools; birthday mode is derived from the Birthday setup. `train_rotation_ui.py` was split by surface in 1.8.0 ([#373](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/373)) into `_draft`, `_confirm` and `_presets` companions. | ~3.1K total |
| `storm.py` / `storm_log.py` | Desert/Canyon Storm: drafts, participation, reminders. | ~2.5K total |
| `storm_strategy.py` / `storm_strategy_ui.py` | Storm strategy data layer and its Discord UI, split in 1.8.0 ([#371](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/371)). Keep new work on the matching side of the seam. | ~2.7K total |
| `transfer.py` / `transfer_cog.py` / `transfer_setup.py` / `transfer_sheets.py` / `transfers_hub.py` | Transfer Management ([#16](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/16), Premium, 1.6.0): passive sheet-watcher over an alliance's recruiting sheet. `transfer.py` is the Discord-free core (header-name column addressing, AND-filter DSL, `compute_poll_diff` against `last_seen_state_json`, template render). `transfer_cog.py` is the per-minute poll loop posting new-applicant / status-change / removal notices (each or digest) with message drafts, full-record view, and opt-in decision write-back to the alliance's **own** sheet. `transfer_setup.py` is the wizard (largest piece); `transfers_hub.py` the `/transfers` front door. Optional server-wide / intake-form source pulls auto-copy filter-matching rows. Only Name is privileged; everything else is free-choice display/filter. State-diff poll self-heals after outages → no `outage_catchup` adapter (by design). A sheet problem the alliance owns (renamed/deleted tab, deleted sheet, revoked access) posts one leadership-channel notice plus a `/transfers` hub warning instead of failing silently — deduplicated by `transfer.sheet_error_signature` via the `sheet_error_*` columns with a 24h re-nudge, cleared with a recovery line on the first clean read (#413). 429s and bot bugs deliberately don't alert (`sheet_problem_kind` vs `config.is_user_config_sheet_error` answer two different questions). | ~4.8K total |
| `alliance_duel.py` | Alliance Duel (VS) tracker ([#398](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/398), Premium except the member day-theme reminder) — Discord-free core for the `/vs` hub. Fixed game constants (day themes, the 1/2/2/2/2/4 **league** point values, tier ordering), the one-row-per-alliance-per-league-week dataclasses, header-name sheet I/O, server-time league/week/day resolution, and both pairing functions. `compute_week_pairing` re-ranks on the weighted `[8,4,2,1]` score; `project_own_path` walks the bracket lineage instead, and a randomized unit test asserts the two agree — they're deliberately independent derivations. Sheet writes split into a pure `plan_upsert` (unit-testable never-clobber guarantee) and a thin `apply_upsert`. Bracket-dependent calls return `BracketIncomplete` carrying `is_choice`, which separates an own-alliance tracking-mode decision ([#448](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/448)) from genuinely missing data. **Per-action award points are not constants** — Tech research raises them per player, so no surface may print them as fact. Design: `notes/DESIGN_alliance_duel_vs.md`. | ~1K |
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

Tests: `tests/unit/` and `tests/integration/`. ~5,600 collected, 31 skip
(intentional — `free_tier_only` markers under the `FORCE_PREMIUM=1` CI
lane).

---

## Patterns to reuse

These are deliberate and tested. Don't refactor away:

**A helper used by a second feature moves to a shared module the day it is
used the second time, and gets a line here.** The Champion Duel feature
copied its siblings instead of reusing them because the helpers it needed
were private to whichever module wrote them first, and nothing here named
them. Four such helpers are queued for promotion on
[#589](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/589)
(the owner-only `interaction_check`, the `on_timeout` cleanup, the
Prev / Page / Next row, the add-a-button helper). Until that lands, the
canonical forms are: the guard compares `inter.user.id` to the owner and
answers with `messages.DENY_NOT_OWNER`; the timeout calls
`wizard_registry.expire_view_message`; pagination is
`storm_officer_view._add_pagination_row`. Do not write a fifth copy of any
of them.

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

### Telling an alliance their config broke
- A guild points the bot at things it owns (a Google Sheet, a Discord
  channel) and those rot: a tab gets renamed, a spreadsheet gets
  unshared, a channel gets deleted or the bot's role loses View
  Channel. The feature then stops silently. `config_health.py` is the
  one mechanism for saying so (#414/#379, generalized out of #413).
- **Recording is a sync DB write at the failure site.**
  `config_health.record(guild_id, subject, kind, detail)` on failure,
  `config_health.clear(guild_id, subject)` on a clean read. No Discord
  I/O, no async, safe to call every tick — `record` holds the quiet
  window open for a repeat of the same problem. Never post from the
  failure site.
- **`config_health_cog` posts**, every 15 min, batching everything a
  guild owes into one digest. That batching is the point: one channel
  reorg can break several subjects, and six red posts read as six
  emergencies.
- **Register the subject at import time** in the module that owns it:
  `config_health.register(Subject(key="feature.thing", label="…",
  fix_hub=…, fix_btn=…))`. `label` is what the *alliance* calls it, and
  the fix must name the surface that actually fixes that subject —
  pointing a permissions failure at the setup wizard sends leadership
  down a path that can't work.
- **Never Sentry-capture config rot.** It's the alliance's to fix, and
  capturing buries real bugs (the `config.is_user_config_sheet_error`
  reasoning, #285/#286).
- Expensive `detail` (a network round-trip to list a spreadsheet's real
  tab names) goes behind `config_health.is_new_problem(...)` so it's
  paid once per problem, not once per tick.
- Pull surfaces read `config_health.problems(guild_id)` /
  `problems_for_subjects(...)`. Detection is reactive, so a hub or
  `/setup` screen that wants live truth about a *channel* should check
  it on render rather than trusting stored rows alone.
- **Channels (#379):** a clock-driven post loop replaces its
  `channel = bot.get_channel(id); if channel is None: continue` with
  `config_health.resolve_configured_channel(bot, guild_id, subject, id)`,
  which records/clears as a side effect and returns `None` unless the
  bot can actually *post* there. `check_channel` is the cache-only
  predicate behind it — free, no REST call, safe per-minute. It cannot
  tell a deleted channel from one the bot lost View Channel on (the
  gateway omits invisible channels from cache), so it reports
  `CHANNEL_GONE` for both and the copy covers both causes;
  `check_channel_precise` spends one REST call to separate them where a
  human is waiting. Loops that already classify their own failure (the
  storm sign-up scheduler's status dict) map straight to a kind instead.
- Several loops sharing one configured channel share one subject — the
  three train loops all post to `reminder_channel_id`, and three notices
  for one broken channel would be three notices for one fix.

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
- **Both SQLite files run `journal_mode=WAL`, and it is load-bearing
  for storage rather than a concurrency tweak.** Don't remove it, and
  set it on any new database file. The Railway volume is a
  thin-provisioned ZFS zvol: it allocates blocks on write and does not
  return them when a file is deleted. The default rollback journal
  creates and deletes a `-journal` file on *every* write transaction,
  and the background loops write continuously — so the volume's
  reported usage climbed ~55 MB/day while the filesystem itself held
  under 2 MB, reaching 3.7 GB of 5 GB before anyone looked. `fstrim`
  can't recover it (the container is refused the FITRIM ioctl); only
  wiping the volume can. WAL keeps one `-wal` file that is reused in
  place, so the allocation stays flat. Note the failure mode: `df`
  inside the container and Railway's volume graph disagree by three
  orders of magnitude, and the graph is the one that hits the ceiling.

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

### Fixing a scheduling / dedup / permission bug? Grep for siblings before closing it
- The 2026-07-17 audit (#361/#367) found the same bug fixed once but
  never propagated to a structurally similar or newer code path,
  three separate times: `storm.py`'s `_guard` reimplemented the
  leadership check instead of calling `storm_permissions
  .is_leader_or_admin` (and silently dropped the admin bypass);
  Train Conductor Rotation's draft/confirm dedup used an in-memory
  set instead of the DB-backed pattern already fixed for birthday
  auto-population after a real production incident (#89); and
  `outage_catchup.py`'s recovery scans recomputed "today" from
  guild-local time instead of `time_helpers.server_date_for`,
  reintroducing a bug already fixed in the live loops (#330/#318).
  A fourth turned up
  in #413: the transfer poll loop still Sentry-captured every sheet
  read failure, though `config.is_user_config_sheet_error` had been
  added in 1.6.7 (#285/#286) precisely so alliance-owned Sheet problems
  log-and-skip instead of paging. One alliance's renamed tab produced
  445 events in 9 days. #414 tracks the same shape in `train.py`,
  `member_roster.py`, and `storm.py`, and #379 the channel equivalent;
  both now build on `config_health.py` rather than reimplementing
  #413's notice a second and third time.
- Before closing a bug tied to one of these code-path shapes —
  scheduling/dedup ("did this already fire today"), permission
  checks, or server-vs-guild-local date resolution — grep the repo
  for other places doing the same kind of thing and check whether
  they need the same fix. A canonical helper existing (`storm_permissions
  .is_leader_or_admin`, `time_helpers.server_today`, the DB-backed
  `last_*_fired` column pattern) doesn't mean every call site uses it.
- **An audit finding that names a *class* of problem ships the check that
  finds the class**, under `scripts/quality/`, in the same PR. A finding
  that only lists sites closes on the sites: the 1.8.0 blocking-I/O sweep
  ([#366](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/366))
  called the config-helper class "worth a repo-wide look" and closed with
  284 of them untouched, because nothing could find them again.
  `scripts/quality/blocking_io.py` is what that look should have been.
- **Dates: never call `date.today()`.** It answers without being asked
  which calendar, and hands back the container's UTC day — nobody's.
  `time_helpers` is the single home: `server_today()` is the default
  (anything the game drives), `local_today(tz)` is the documented
  exception for human-calendar data like birthdays, and if you already
  hold a correctly-zoned datetime just call `.date()` on it. 1.8.11
  came out of `date.today()` being used in four places that each
  needed a different one of those answers.

---

## Cleanup skills

Five skills in `.claude/skills/` automate the "grep for siblings before
closing it" discipline above. Run them in this order; each narrows what
the next has to look at.

- **`code-complexity`** — ranks functions by measured complexity. Run it
  first in any cleanup session: it turns "this feels messy" into a target
  list. `radon` is in the shared venv.
- **`dry-consolidation`** — finds code duplicated across modules with a
  real clone detector, then extracts it into one helper. Reads § Patterns
  to reuse above and treats everything there as preserved.
- **`code-dead-code`** — finds what nothing reaches. Outputs suspects,
  never a delete list: framework-dispatched callbacks, module re-exports
  and the inline-config hook all look dead and are not. Needs `vulture`.
- **`ast-grep-search`** — searches by AST shape rather than text. This is
  the "human grep" `ruff.toml` says the ASYNC rule still needs for our own
  non-stdlib blocking I/O (gspread, sqlite).
- **`bulk-sweep-classify`** — classify every match before any repo-wide
  find-replace. The success test is never "the grep returns zero".

**The measurements are committed under `scripts/quality/`** (the
complexity map, the blocking-I/O check, the dead-code runner, and the
ast-grep rule files they use). Each skill calls its script rather than
describing it, so a run starts where the last one ended and a re-run after
a cleanup can be put beside the run before it. See `scripts/quality/README.md`.
The 2026-09-09 walkthrough that produced them, one skill per turn with its
findings, is [#589](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/589).

Adapted from a third-party MIT plugin; provenance and what changed is in
`.claude/skills/THIRD_PARTY.md`. Their tooling is dev-only and must never
reach `requirements.txt`.

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

The full per-release table, from 1.0.0, is `docs/RELEASE_HISTORY.md`;
`CHANGELOG.md` is the authoritative long form. The three most recent
entries the table carried when it moved (2026-09-10) are kept here so a
session has the shape of a recent release in front of it. Production is
at 1.8.11; the 1.8.1 to 1.8.11 entries live in `CHANGELOG.md`.

| Version | What |
|---|---|
| `1.8.0` | Survey threads can carry a named translate bot so non-English members can read their prompts (private threads hide server-wide translate bots) ([#422](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/422)). `/events` gains **⏸️ Pause or resume** — stop an event for a season and turn it back on with every setting intact, re-anchoring repeating events on the way back; **🗑️ Delete** becomes permanent and says so ([#421](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/421)). Event anchor dates accept `7/30` / ISO / `today` / weekday names and retry instead of ending the wizard ([#420](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/420)). Transfer watcher reports a renamed/deleted/inaccessible sheet tab instead of failing silently ([#413](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/413)). First batch of the 2026-07-17 audit lands: blocking-I/O sweep + ruff ASYNC ([#366](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/366)), train reminder loop off the event loop ([#362](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/362)), `pending_warnings` restart-safe ([#363](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/363)), outage-catchup server-time dates ([#364](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/364)), storm sign-up tick drift ([#365](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/365)), unpropagated-fix sweep ([#367](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/367)), plus structural splits of `storm_strategy.py` ([#371](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/371)) and `train_rotation_ui.py` ([#373](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/373)) and dedupes in `transfer_setup.py` ([#374](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/374)) / `storm_officer_view.py` ([#375](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/375)). |
| `1.7.6` | Growth Breakdown bucketed nothing for comma-formatted metrics; snapshot columns now written with thousands separators and the 0-5% bucket relabelled "No Change" ([#417](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/417)). |
| `1.7.5` | Growth snapshots recorded `0` for every comma-formatted metric (squad power, total kills) and broke past column Z; both fixed, and `/my_stats` / `/member_stats` show real numbers again ([#415](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/415)). |

Test suite: **~5,600 tests** (5,601 passed, 31 skipped on the free lane,
2026-09-09). Total LOC: ~80K application, ~57K tests.

---

## Where things get pushed (three repos, read before you commit)

**This repo is PUBLIC.** So is the website. Assume anything you commit
here is world-readable the moment it's pushed.

| Repo | Visibility | Lives at | Push rule |
|---|---|---|---|
| `lw-alliance-helper-bot` | **Public** | this directory | Release-branch workflow (see Working agreement) |
| `lw-alliance-helper.github.io` | **Public** | `../lw-alliance-helper.github.io` | Straight to `main` |
| `lw-alliance-helper-notes` | **Private** | `notes/`, nested inside this repo | Straight to `main`, from inside `notes/` |

`notes/` is its own independent git repo cloned into this one, and is
listed in this repo's `.gitignore`, so the outer repo never descends
into it. From inside `notes/`, git commands act on the private repo;
from here, they act on the bot. **Two repos, two commit habits** — work
that touches both needs a commit in each.

It is deliberately **not a submodule**: a submodule writes
`.gitmodules` into this public repo, which would publish the private
repo's URL and its existence.

### What goes where

- **`notes/` (private)** holds anything whose reasoning is worth more
  to a competitor than to a user: the `UX.md` / `DESIGN.md` contracts,
  `STRATEGY.md`, design parks, competitive research, planning, audit
  notes, ad-hoc test plans.
- **`docs/` (public)** is tracked reference that ships with the repo.
  **Not a place for captured data.** Third-party datasets, scraped
  snapshots, and anything pulled from behind someone else's auth wall
  do not belong in a public repo even transiently.
- **This file (public)** may *name* a file in `notes/`, the way the
  list below does, but **must not quote its content**. A rule or
  decision that lives in `notes/` gets referenced here, not restated.

That boundary was set on 2026-08-08, after competitor-teardown examples
were inlined into the design docs and had to be stripped back out
before the branch was pushed, and again after the design docs
themselves were found to belong on the private side.

### Parked work

Current contents, verified 2026-08-08 (worth being aware of when
picking up new work):

- **`notes/README.md`** — what the private repo is, how it's wired into
  this one, and why the design docs live there.
- **`notes/UX.md`** / **`notes/DESIGN.md`** — the user-facing contract.
  See the pointer at the top of this file.
- **`notes/STRATEGY.md`** — commercial positions (pricing, localisation,
  self-host, fork). Moved out of this file on 2026-08-08 because this
  file is public.
- **`notes/DESIGN_transfer_management.md`** — spec + build log for the
  Premium transfer-tracking feature ([#16](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/16)),
  now **built and shipping in 1.6.0**. Kept for the reconciliation
  decisions (header-name addressing, only-Name-is-special, the
  intentional no-outage-adapter call).
- **`notes/DESIGN_alliance_duel_vs.md`** — Alliance Duel (VS) design,
  ground truth for [#398](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/398).
- **`notes/DESIGN_config_health.md`** — shared config-health notices,
  ground truth for the #414 / #379 work.
- **`notes/COMPETITOR_*.md`** — competitive research, each with its own
  intake protocol for filing a new dump. **Don't name the subjects in
  this file** — see the boundary rule above.
- **`notes/PLANNING.md`** — cross-session work tracker.

The pre-launch `AUDIT_2026-04-30.md` and the per-batch
`DEV_TEST_PLAN_*.md` files listed here previously are gone; both had
shipped. Don't go looking for them.

When a chat session starts on a `notes/DESIGN_*.md`, that doc is the
ground truth for its feature.

### Capture artifacts

`docs/server_json.json` (untracked, ~1MB) is a Shiny Tasks server
snapshot captured for `/admin shiny_import`, per the manual maintenance
process in `docs/hedge_data_source.md`. It is third-party data from
behind an auth wall, which is why it's gitignored rather than
committed.

**Captures are disposable.** The source rotates its chunk URL every
deploy, so the process says to capture fresh each time. A stale
snapshot on disk is a hazard (someone imports it and reintroduces the
drifted dates from
[#330](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/330) /
[#331](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/331)),
not an asset. Delete after importing.

Note the gitignore entry for it currently exists only on the
`alliance-duel-vs-core` branch, not on `main`.

---

## Strategic decisions

Moved to `notes/STRATEGY.md` on 2026-08-08. Pricing, localisation,
self-host, and fork positions are commercial decisions with their
reasoning attached, which is worth real money to a competitor and
nothing to a user, and this file is public.

They still bind: **don't second-guess them in passing.** Read them
there before proposing anything that touches pricing, tiers, language
support, or hosting.

---

## Status snapshot

- 1.0.0 launched 2026-04-28. **Production is `1.8.11`** (shipped
  2026-09-09), `dev` carries `1.9.0`. No release branch is currently
  open. See `CHANGELOG.md` and `docs/RELEASE_HISTORY.md` for per-release
  detail.
- **Map Manager integration is live in code but invisible.** The
  authenticated bot-side HTTP API (`api_server.py`) + the
  `/map_manager` hub shipped in 1.7.0
  ([#316](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/316),
  [#338](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/338))
  with the **user-facing surfaces hidden behind
  `MAP_MANAGER_COMMANDS_ENABLED` (default off)** — the `/map_manager`
  cog isn't loaded, and the `/setup` button + `/help` category are
  dropped — while the HTTP endpoints stay gated by
  `MAPMANAGER_API_KEY`. The surfaces stay invisible until the flag is
  flipped. **The Procfile flipped `worker` → `web` in 1.7.0**, so
  Railway runs the bot as a web service (health check tolerant of the
  gateway-login window — see
  `docs/MAPMANAGER_INTEGRATION_DEPLOY.md`).
- **2026-07-17 audit
  ([#361](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/361))
  is partly shipped.** 1.8.0 landed the Critical fixes
  ([#362](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/362)-[#367](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/367))
  and four structure/cleanup items
  ([#371](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/371),
  [#373](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/373),
  [#374](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/374),
  [#375](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/375)).
  **[#372](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/372)
  (split `bot.py`) is deliberately still open** — the `/admin` toolkit
  moved to `bot_admin.py` + `bot_state.py` (2,075 → 1,304 lines), but
  the four `@tasks.loop` background loops (`growth_task`,
  `stats_publish_task`, `shiny_tasks_refresh_task`,
  `shiny_tasks_post_task`) are still in `bot.py`, and `on_ready` is
  still ~241 lines. Remaining audit items are tracked on the board.
- ~5,600 tests pass on the default (non-sheets) lane (31 skipped).
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
- Transfer Management ([#16](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/16))
  shipped in 1.6.0 and refined through 1.6.4; sheet-error notices
  followed in 1.8.0
  ([#413](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/413)).
  The state-diff poll self-heals after outages, so it has
  **no `outage_catchup` adapter by design** (see the design doc's
  reconciliation note). See `transfer*.py` and
  `notes/DESIGN_transfer_management.md`.
- **Not yet merged:** `growth-418-identity-matching` carries
  [#418](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/418)
  (match growth members by Discord ID so a rename keeps their history).
  It's pushed to its own remote branch, unmerged to `dev` or `main`,
  and still needs its own trip through the workflow.

For per-version detail, see `CHANGELOG.md`. New in-flight work goes
on a descriptive feature branch (which may bundle several related
issues) → PR into the active `release/X.Y.Z` branch with a merge
commit. The release branch is where the next release accumulates
before its own PR into `main`.
