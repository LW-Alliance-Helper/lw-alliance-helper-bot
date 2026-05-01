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

- **Solo project.** Push directly to `main`. Always fast-forward merge.
- **No PRs unless explicitly asked.** Default workflow is branch → fix
  → tests pass → fast-forward to main → push.
- **Tests must pass before commit.** Pre-commit hook enforces this. If
  it fails: investigate the underlying issue, don't bypass with
  `--no-verify`.
- **Commit messages:** use HEREDOC, end with the
  `Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>`
  trailer.
- **Never amend** — always make a new commit, even after pre-commit
  hook failures.
- **Never push --force to main**, never `reset --hard`, never delete
  branches without confirming.

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
| `wizard_registry.py` | `wait_view_or_cancel` for cancellable wizards. | ~145 |
| `defaults.py` | Hardcoded copy: themes/tones, default mail templates, default DM bodies. | ~100 |
| `dm.py` | DM helpers. | ~80 |
| `donate.py` | `/donate` and `/upgrade` commands. | ~135 |
| `config.py` | Schema, migrations, `get_*` / `save_*` helpers, gspread client. | ~1.5K |
| `stats_publisher.py` | Daily alliance-count publisher to website. | ~155 |

Tests: `tests/unit/` and `tests/integration/`. 501 passing, 18 skipped
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

| Commit | What |
|---|---|
| `5c541ea` | Alliance-customisable DM bodies (birthday, train, storm) |
| `2c577dd` | Wizard "Keep current" vs "Use default" labelling |
| `4ef9481` | Custom event blurb propagates from `/setup_events` to `/events` announcements |
| `aff6c20` | Growth: surface next snapshot date in wizard + `/growth` |
| `72d61a5` | `/sync_members` actually requests the privileged `members` intent |
| `f832f09` | CS draft renders full zone names ("Data Center 1") not abbreviations |
| `555e1db` | `/cancel` actually stops view-based wizard steps |
| `52cca61` | Test audit — 81 tests across 7 high-impact gaps |

Test suite: **501 passing, 18 skipped** (matches CI lane). Total LOC:
~17K.

---

## Parked work (local-only docs)

These are untracked. Don't push them; keep them as future-pickup specs.

- **`AUDIT_2026-04-30.md`** — pre-launch code-quality audit. Four
  rounds of cleanup queued:
  - Round 1 (A + B): 2 real bugs + 12 safe deletes
  - Round 2 (C): dead schema columns
  - Round 3 (D + F): bloat / duplication / storm step renumber
  - Round 4 (E + G): polish + larger refactors (post-launch)
- **`DESIGN_transfer_management.md`** — fully-iterated spec for a
  Premium transfer-tracking feature (sheet-watcher + filter wizard +
  in-game message templates). ~7 days of work. Post-launch v1.x.

When a chat session starts on either of these, that doc is the
ground truth.

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

- Pre-launch, ready to ship 1.0.
- 501 tests passing.
- Latest commit: `5c541ea` (alliance-customisable DM bodies).
- Audit Round 1 cleanup pending (low risk, high confidence — see
  `AUDIT_2026-04-30.md`).
- Transfer management feature designed, not built (see
  `DESIGN_transfer_management.md`).

For week-by-week shipped work, see `CHANGELOG.md`'s `[Unreleased]`
section.
