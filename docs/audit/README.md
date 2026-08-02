# Bot Codebase Audit — Summary & Index

Full sweep of the `lw-alliance-helper-bot` repo (65 source files, ~78,400 lines), section by section, against the baseline in [00-standards.md](00-standards.md). Audit only — no source files were changed. This doc ties the 15 section reports together for the joint review.

**Tracked in GitHub as of 2026-07-18**: umbrella issue [#361](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/361), all work items tagged [`Code-Audit-2026-07`](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues?q=label%3ACode-Audit-2026-07). Issue-to-finding mapping:

| Issue | Item |
|---|---|
| [#362](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/362) | Critical Tier 1 — train_cog.py per-minute blocking loop |
| [#363](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/363) | Critical Tier 1 — scheduler.py pending_warnings data loss |
| [#364](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/364) | Critical Tier 1 — outage_catchup.py wrong timezone |
| [#365](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/365) | Critical Tier 2 — storm_signup_scheduler.py tick-drift (needs design) |
| [#366](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/366) | Pattern A — blocking-I/O batch sweep + ruff ASYNC rules |
| [#367](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/367) | Pattern B — propagate storm.py/train-rotation fixes + guidance note |
| [#368](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/368) | Split setup_cog.py by wizard |
| [#369](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/369) | Split storm_roster_builder.py by 9 seams |
| [#370](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/370) | Split config.py (full split, last) |
| [#371](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/371) | Split storm_strategy.py data/UI |
| [#372](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/372) | Split bot.py along its seams |
| [#373](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/373) | Split train_rotation_ui.py by seam |
| [#374](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/374) | Dedupe transfer_setup.py picker views |
| [#375](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/375) | Dedupe storm_officer_view.py guard/pagination |
| [#376](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/376) | Low/medium cleanup pass (bucket) |

## Section reports

| # | Doc | Scope | Critical | High |
|---|---|---|---|---|
| 01 | [core-bot-entry](01-core-bot-entry.md) | bot.py, bot_state.py, api_server.py, api/routes/* | 1 | 2 |
| 02 | [config-core](02-config-core.md) | config.py, config_export.py, defaults.py | 1 | 3 |
| 03 | [setup-wizard](03-setup-wizard.md) | setup_cog.py, setup_hub.py, wizard_registry.py | 1 | 4 |
| 04 | [storm-core](04-storm-core.md) | storm.py, signup/date/permission/event-hub/walkthrough | 1 | 4 |
| 05 | [storm-data](05-storm-data.md) | attendance, history, log, member_rules, roster_builder, trends | 2 | 4 |
| 06 | [storm-strategy-render](06-storm-strategy-render.md) | officer_view, renderer, strategy | 0 | 2 |
| 07 | [train-system](07-train-system.md) | train.py + cog/hub/rotation/ui | 2 | 2 |
| 08 | [transfer-system](08-transfer-system.md) | transfer.py + cog/setup/sheets/hub | 0 | 2 |
| 09 | [buddy-system](09-buddy-system.md) | buddy.py + cog/hub/ui | 0 | 1 |
| 10 | [growth-member-stats](10-growth-member-stats.md) | growth.py, member_stats.py, member_roster.py | 0 | 3 |
| 11 | [survey-donate-events](11-survey-donate-events.md) | survey.py, donate.py, events_hub.py | 0 | 3 |
| 12 | [mapmanager-premium-messaging](12-mapmanager-premium-messaging.md) | mapmanager_*, premium.py, messages.py, help_content.py | 0 | 2 |
| 13 | [scheduling-ops](13-scheduling-ops.md) | scheduler.py, outage_catchup.py, release_announcements.py, shiny_tasks.py, support_join_watch.py, stats_publisher.py | 2 | 2 |
| 14 | [export-import-scripts](14-export-import-scripts.md) | export_import_cog.py, scripts/* | 0 | 3 |
| 15 | [tests-tooling](15-tests-tooling.md) | tests/, pytest.ini, ruff.toml, pre-commit, CI | — | 2 |

**10 Critical findings total, 0 premium-gating or auth bypasses found.** The good news up front: the two places most worth worrying about from a "did we leak money or data" standpoint — `premium.py`'s gating logic and the bot's HTTP API auth (`api/auth.py`) — both came back clean. Everything Critical here is reliability/correctness, not security.

## The one pattern that explains most of the Criticals

**Blocking (synchronous) I/O called directly inside `async def` code, unwrapped by `asyncio.to_thread`.** This single anti-pattern accounts for 7 of the 10 Criticals and several Highs, spread across sections that were audited independently by different agents with no cross-talk — meaning it's a systemic habit, not a one-off:

- `bot.py:1081` — growth "run now" button (its sibling button 20 lines away does this correctly)
- `storm_log.py:1457` — DS/CS vote prefill Sheets read
- `train_ui.py` (9 call sites) — every write path in the legacy blurb UI
- `train_cog.py` — the per-minute rotation reminder loop (worse: runs unattended)
- `config.py:4570` (`lookup_discord_id_for_name`) — called from `dm.py`'s async functions
- `setup_cog.py:9160-9161` — third of three sequential Sheets calls in a chain, first two correctly threaded
- `scripts/seed_demo.py` — called unthreaded from `bot.py`'s `on_ready`, would freeze the bot at boot if triggered

Each individually stalls the bot's single event loop — meaning every guild, not just the one that triggered it — for the duration of the network call. This is worth a dedicated pass rather than 7 separate fixes: grep the whole repo for direct `gspread`/sqlite calls inside `async def` bodies and wrap them consistently, ideally with a lint rule or code-review checklist item to stop new ones from creeping in (`ruff`'s `ASYNC` rule set catches some of this — currently not enabled, see tests-tooling below).

## The second pattern: fixes that didn't propagate to sibling code

Three separate agents independently found the same shape of bug — a real production issue got fixed in one place, but a structurally similar or newer code path never received the same fix:

1. **`storm.py`'s `_guard` reimplements permission logic instead of calling `storm_permissions.is_leader_or_admin`** — and drops the admin bypass in the process. `storm_permissions.py` exists specifically to be the canonical fix for this bug class, but `storm.py` predates it and was never migrated.
2. **Train rotation's weekly-draft/daily-confirm dedup uses in-memory-only sets** — the exact bug already fixed for birthday auto-population via a DB-backed `last_train_population_date` after a real incident (#89). Rotation (#55) shipped after that fix but never adopted the pattern, so a redeploy at the trigger minute can re-fire and silently overwrite a leader's manual weekly edits.
3. **`outage_catchup.py`'s `scan_shiny`/`scan_train_reminder` recompute "today" from guild-local time** instead of the Last War server clock (UTC-2) — reintroducing the exact bug fixed in the *live* loops (#330/#331 for shiny, #318 for train), which correctly call `config.server_date_for`. The recovery/catch-up path never got the fix that the normal path did.

This is worth calling out as a process note, not just a code note: when a scheduling/dedup/permission bug gets fixed, it's worth a quick grep for other places doing the same kind of thing before closing the issue.

## Third pattern: god files (flagged, not touched)

Ordered by size, all flagged as future-split candidates with documented seams — none of these need to be split immediately, but they're where a bug becomes hardest to review or a new feature becomes hardest to add cleanly:

| File | Lines | Split seams already identified |
|---|---|---|
| `setup_cog.py` | 11,166 | By wizard: Growth/Train/Buddy/Survey/Storm/Event/Birthday/Shiny — Storm alone is ~3,956 lines |
| `storm_roster_builder.py` | 7,226 | Data read / session state / auto-fill / embeds / views / mail gen / draft persistence / DM / finalize |
| `config.py` | 5,344 | Schema+migrations / dataclass model / Sheets I/O / time formatting / ~20 per-feature CRUD clusters / validation |
| `transfer_setup.py` | 3,442 | Mode picker / mapping / filter / intake sources / decisions / wizard driver / edit menu |
| `storm_officer_view.py` | 3,226 | (duplication-heavy rather than concern-mixing — see High findings) |
| `storm_strategy.py` | 2,629 | Data/I/O (176-1050) vs. 9 Modal/View UI classes (1116-2629) |
| `storm_renderer.py` | 2,341 | — |
| `bot.py` | 2,075 | Event handlers / 3 command groups / 5 background loops / ~820-line `/admin` toolkit — file's own comment banners already mark the seams |
| `train_rotation_ui.py` | 2,017 | Preset editor / weekly draft / daily confirm |
| `storm_log.py` | 2,012 | — |
| `survey.py` | 1,932 | UI + logic + Sheets I/O + scheduler tick in one file |

`config.py` is the highest-leverage one to eventually address carefully: it's imported by 52 of the ~65 non-test source files, so any split needs to preserve its public interface exactly.

## Fourth pattern: cross-module reach into "private" helpers

Repeated across sections: modules importing another module's underscore-prefixed (by-convention-private) functions instead of a public interface — `buddy_ui.py` → `buddy._norm`/`buddy._join_and`; `config_export.py` → `config._get_conn` (one site even has a self-flagged `noqa`); `member_stats.py` → `growth`'s private helpers; `storm_member_rules.py` → `storm_strategy._translate_legacy_cs_zone`; `survey.py`/`train_hub.py` → `setup_cog`'s private helpers; `buddy.py` → `storm_roster_builder`'s private power-index helpers. None of these are bugs today, but each is a hidden coupling that makes the "private" module harder to safely refactor later.

## Fifth: `print()` vs. the `logging` module

Inconsistent across the repo — `storm.py`, `storm_log.py`, `scheduler.py`, and all of `setup_cog.py` (zero logger usage in 11,166 lines) use bare `print()`, while `buddy_hub.py`, `outage_catchup.py`, `release_announcements.py`, and most of `transfer_*`/`train_rotation*` use `logger` correctly. `print()` output doesn't get the log level, timestamp, or Sentry routing the rest of the codebase relies on for debugging production issues.

## Tooling gaps (from the tests-tooling audit)

- Test coverage is better than file-count-alone suggested — only 3 of 65 modules have zero test references (`transfers_hub.py`, `stats_publisher.py`, `buddy_cog.py`).
- **No type checker configured anywhere** (no mypy/pyright) — relevant given how many findings above are "missing type hints on a cross-module function."
- **Ruff only runs in pre-commit, never in CI** — a direct push or a `--no-verify` commit bypasses lint entirely. `ruff.toml`'s rule set is also deliberately narrow (`E9`, `F` only) and misses `flake8-bugbear` rules that would catch some of the exact issues flagged above (mutable defaults, silent excepts).

## What's genuinely in good shape

Worth stating plainly since 10 sections came back with zero Criticals: `premium.py`'s gating is fail-closed everywhere and has no bypass; the bot's HTTP API auth (`api/auth.py`) is fail-closed and constant-time; `transfer_system`, `buddy_system`, `mapmanager_client.py`, `storm_date_helpers.py`, `storm_permissions.py`, and `donate.py`'s `_ConfirmActionView` were all called out by their respective auditors as models other modules should follow, not just "no findings." The `_cog`/`_hub`/`_ui`/logic split convention the repo already uses in several places (buddy, train, transfer) is a real, working pattern — the god-file list above is mostly the modules that predate or skipped that convention.

## Suggested order for the joint review

Given your framing (severity → category → recommended optimization → anything unusual), a reasonable path through this:

1. **The 10 Criticals** — all are blocking-I/O-on-event-loop or data-loss/timezone-regression bugs, all independently verified by different agents. These are the "should probably actually get fixed soon" list regardless of the broader cleanup timeline.
2. **The two cross-cutting patterns** (blocking I/O, unpropagated fixes) as process items — worth deciding if either merits a repo-wide sweep/lint rule rather than one-off fixes.
3. **God-file split candidates** — no urgency, but worth agreeing on which one (if any) to tackle first; `config.py` is highest-leverage but highest-risk given its 52 importers.
4. **Everything else** (naming, docstrings, minor duplication) — pick up opportunistically or ignore, per section doc.

## Review decisions (2026-07-17)

Triage only — no code changed yet. This is the agreed plan for follow-up sessions.

**Criticals — prioritized, not yet fixed:**
- Tier 1 (fix soonest): **#6** `train_cog.py` per-minute reminder loop (runs unattended, every minute, all guilds — highest real-world exposure of any item here), **#10** `scheduler.py` `pending_warnings` in-memory-only (silent data loss, zero trace), **#9** `outage_catchup.py` wrong timezone in recovery scans (degraded-mode correctness).
- Tier 2 (same blocking-I/O pattern, fix via the Pattern A sweep below): #1 (`bot.py`), #3 (`storm_log.py`), #4 (`storm_roster_writeback.py`), #5 (`train_ui.py` — confirmed still live, not dead code, so fix rather than delete), #7 (`config.py`/`dm.py`), #8 (`setup_cog.py`).
- Tier 2, needs a design decision first (not just a `to_thread` wrap): **#2** `storm_signup_scheduler.py` tick-drift.

**Pattern A (blocking I/O on the event loop) — approved: batch sweep + lint rule.** Plan: grep the whole repo once for direct `gspread`/sqlite calls inside `async def` bodies, fix everything found in one pass (this covers the Tier 2 blocking-I/O Criticals above plus any not yet individually flagged), then enable ruff's `ASYNC` rule set so new instances get caught automatically.

**Pattern B (fixes that don't propagate to sibling code) — approved: process change.** Add a note (CLAUDE.md or CONTRIBUTING) that when closing a bug tied to a specific code path (scheduling, permission checks, dedup logic), do a quick grep for structurally similar code elsewhere in the repo before closing the issue. Applies retroactively to: `storm.py`'s `_guard` (should call `storm_permissions.is_leader_or_admin`), train rotation's in-memory dedup (should adopt the DB-backed `last_train_population_date` pattern from the birthday fix), and `outage_catchup.py`'s timezone bug (should call `config.server_date_for` like the live loops do).

**God-file splits — decided 2026-07-17, second triage pass.** Kevin went through all 11 flagged files individually and approved action on most:

| File | Decision |
|---|---|
| `setup_cog.py` (11,166) | **Split by wizard now** — one file per wizard (setup_growth.py, setup_storm.py, ...), coordinated via the existing thin `setup_hub.py` dispatcher. |
| `storm_roster_builder.py` (7,226) | **Split by the 9 seams now** — data read / session state / auto-fill / embeds / views / mail gen / draft persistence / DM / finalize. |
| `config.py` (5,344, 52 importers) | **Full split now** — highest risk on the list given import blast radius; do this one carefully with strong before/after test coverage as a safety net. Split lines: schema/migrations, dataclass model, Sheets I/O, time formatting, per-feature CRUD clusters. |
| `transfer_setup.py` (3,442) | **No file split** — dedupe the 3 near-identical paged picker views (`_ColumnMapView`, `_AdaptiveColumnMapView`, `_SourceMapView`) into a shared base instead; this file's actual problem is duplication, not tangled concerns. |
| `storm_officer_view.py` (3,226) | **No file split** — dedupe `_guard_owner` (copy-pasted across 4 View classes + ~9 inline repeats) and the Prev/Next pagination wiring (copy-pasted across 3 picker views) into shared helpers/base class. |
| `storm_strategy.py` (2,629) | **Split data/UI now** — `storm_strategy.py` (data/I/O, lines 176-1050) / `storm_strategy_ui.py` (9 Modal/View classes, 1116-2629). Matches the repo's existing `_logic`/`_ui` convention. |
| `bot.py` (2,075) | **Split along its own comment-marked seams now** — pull the ~820-line `/admin` toolkit and the 5 background loops into their own modules; keep startup/`on_ready`/event registration in `bot.py` itself. |
| `train_rotation_ui.py` (2,017) | **Split by seam now** — preset editor / weekly draft / daily confirm. |
| `storm_renderer.py` (2,341) | **Deferred** — no split seam identified beyond size; the two long draw functions (`_draw_zone`/`_draw_subs_section`) are a smaller, lower-priority decomposition candidate for later. |
| `storm_log.py` (2,012) | **Deferred** — no concrete split/dedupe target identified; its Critical finding is already covered by the Pattern A sweep. |
| `survey.py` (1,932) | **Deferred** — no seam mapped yet; needs closer inspection before committing to a split shape. |

**Net effect: 7 of 11 god-files get real structural work this round** (setup_cog.py, storm_roster_builder.py, config.py, storm_strategy.py, bot.py, train_rotation_ui.py get split; transfer_setup.py and storm_officer_view.py get dedupe-not-split). This is a larger, higher-risk body of work than the original "defer everything" call — sequencing and regression-safety planning should happen before starting, especially for config.py given its 52 importers. See "Suggested execution order" below.

**Low/medium findings (naming, docstrings, print-vs-logging, minor duplication, private-helper coupling) — deferred to one dedicated low-priority cleanup pass later**, not opportunistic-only and not immediate.

**Type checker (mypy/pyright) — not adopted now.** Revisit later; discord.py's typing history and the volume of type-hint gaps found make this a bigger lift than seems worth it right now.

### Suggested execution order for the god-file work

Lowest-risk / most contained first, so early wins build confidence before the higher-blast-radius files:

1. **Dedupe-only work** (no import-path changes, no cross-file risk): `transfer_setup.py` picker views, `storm_officer_view.py` guard/pagination boilerplate. Good warm-up.
2. **Single-file, self-contained splits**: `train_rotation_ui.py`, `storm_strategy.py` (clean data/UI line already identified). Low importer counts, well-defined seams.
3. **`bot.py`** — comment-marked seams make this mechanical, but it's the startup path, so test a full bot boot after each extraction.
4. **`storm_roster_builder.py`** — 9 seams, larger surface area than #1-3, but confined to one feature domain (Storm roster building) so a bug stays contained to Storm testing.
5. **`setup_cog.py`** — biggest line count but the wizards are largely independent of each other, so splitting can go wizard-by-wizard with each one testable in isolation; treat as N small PRs rather than one big one, likely starting with the largest (Storm wizard, ~3,956 lines) or smallest, whichever Kevin prefers when this starts.
6. **`config.py`** — last, and its own dedicated session. 52 importers means every extraction needs a full-repo grep to confirm no import breaks, plus running the full test suite (not just the changed file's tests) after each step. This is the one where "verify end-to-end before moving on" matters most.

This is a plan, not a commitment to do it all in one sitting — pick it up whenever ready, in this order or another.
