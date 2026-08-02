# Section Audit: Tests & Tooling Config

Unlike the other section docs, this is a posture assessment of the test suite and CI/lint/type-check tooling as a whole, not a line-by-line code audit.

## 1. Files covered

**Config/tooling files:**

| File | Purpose |
|---|---|
| `pytest.ini` | pytest config (asyncio mode, markers, timeout, filterwarnings) |
| `ruff.toml` | lint + format config |
| `.pre-commit-config.yaml` | pre-commit hook set |
| `.codespellrc` | spell-check skip/ignore list |
| `.github/workflows/test.yml` | CI: unit + integration + sheets tests, deploy gate |
| `.github/workflows/release-on-main.yml` | release automation (no test/lint steps) |
| `.github/workflows/project-status-sync.yml` | project-board automation (not test/lint related) |

**Test suite:** 111 test files under `tests/` (`tests/unit/` 99, `tests/integration/` 9, `tests/sheets/` 3, plus `tests/conftest.py` and `tests/constants.py`), ~55,200 lines total. Full per-file line counts were gathered; the three largest are `tests/unit/test_storm_roster_builder.py` (7,422), `tests/unit/test_config.py` (2,259), and `tests/unit/test_storm_officer_view.py` (2,131).

**Source files cross-referenced:** all 65 top-level `*.py` files at the repo root (`tests/`, `.venv/`, `__pycache__/`, and other non-root paths excluded), ~75,986 lines total.

## 2. Summary

Test coverage by file-name/import cross-reference is strong: of 65 root source modules, only 3 have zero references anywhere in `tests/` (`buddy_cog.py`, `stats_publisher.py`, `transfers_hub.py`), and all three are thin. The suite itself is high quality where spot-checked — real registration/behavior assertions, properly scoped Discord/Sheets mocking, a well-reasoned single skip mechanism, no xfail anywhere. The bigger gaps are on the tooling side: ruff is configured deliberately narrow (bugs/dead-code only, no style/complexity/annotation rules), there is **no type-checking tool anywhere in the repo** (no mypy/pyright in `requirements.txt`, `.pre-commit-config.yaml`, or CI) despite the standards doc's emphasis on type hints, and **CI never runs ruff at all** — lint only happens pre-commit, so it's bypassable (`--no-verify`, a fresh clone without `pre-commit install`, or a direct push) with nothing in `test.yml` to catch it.

## 3. Findings

### Critical
None found.

### High

- **[Tooling] No static type-checking anywhere in the repo.** No `mypy`, `pyright`, or `pyre` in `requirements.txt`, `.pre-commit-config.yaml`, or any `.github/workflows/*.yml`. `ruff.toml`'s lint `select` is `["E9", "F"]` only — pyflakes/syntax-error rules — which does not do type inference or catch type mismatches the way `mypy`/`pyright` would. This is a real gap given the standards doc (section 3) explicitly calls for type-hint coverage on cross-module functions, and there's no automated backstop confirming those hints are even self-consistent once written.
  - **Recommendation**: add `mypy` (or `pyright`) as a pre-commit hook, even in a lenient/incremental mode (`--ignore-missing-imports`, per-module `disallow_untyped_defs` opt-in) rather than a strict whole-repo pass, so it can be adopted gradually without blocking every commit.

- **[CI gap] `ruff` (lint + format) never runs in CI — only in pre-commit, which is locally opt-in.** `.github/workflows/test.yml` only installs `pytest`/`pytest-asyncio`/`pytest-mock`/`pytest-timeout` and runs pytest; there is no `ruff check` / `ruff format --check` step in any workflow. `.pre-commit-config.yaml` (lines 1-2) notes hooks run "on STAGED files at commit time" and require `py -m pre_commit install` per clone — nothing enforces that installation happened, and a direct push or a `--no-verify` commit reaches `main` with zero lint gate ever having run.
  - **Recommendation**: add a `ruff check .` and `ruff format --check .` step to `test.yml` (fast, no extra infra needed) so lint is enforced regardless of whether a contributor has pre-commit installed locally.

### Medium

- **[Tooling] Ruff's lint rule selection is deliberately narrow and may be worth revisiting for bug classes it currently misses.** `ruff.toml` lines 15-36: `select = ["E9", "F"]` with `F401`/`F811` explicitly disabled (documented, reasonable rationale for this codebase's re-export and late-binding-import patterns). This means ruff catches undefined names, unused locals outside tests, and syntax errors — but not `B` (flake8-bugbear: mutable-default-argument, except-pass, etc. — directly relevant to standards section 4), not `C90` (complexity/nesting), not `ANN` (missing type hints), not `SIM` (simplifiable code). Given the standards doc flags mutable defaults and bare/broad excepts as correctness issues, `B006`/`B008` (mutable defaults) and `B110`/`B112` (silent except) would be low-noise, high-signal additions.
  - **Recommendation**: consider opt-in of the `B` (bugbear) rule subset — at minimum `B006`, `B008`, `B110` — as a targeted addition rather than a full ruleset overhaul; the existing narrow-scope philosophy is reasonable and doesn't need to be abandoned wholesale.

- **[Coverage] Three root source modules have no test file and no reference anywhere under `tests/`.** See Coverage Gap Map below (`buddy_cog.py` 33 lines, `stats_publisher.py` 179 lines, `transfers_hub.py` 281 lines). All three are thin coordinator/publisher modules, so risk is lower than the file sizes below suggest, but `stats_publisher.py` and `transfers_hub.py` are large enough to warrant at least a smoke test.
  - **Recommendation**: add a minimal test file per module confirming the module imports cleanly and its top-level entry points don't throw on the happy path — mirrors the existing `test_mapmanager_cog.py` pattern (26 lines, delegates deep coverage elsewhere but still asserts the wiring).

- **[Tooling] `pytest.ini` globally ignores `DeprecationWarning` and `PytestUnraisableExceptionWarning` (lines 9-11).** Blanket-ignoring `DeprecationWarning` across the whole suite means an upstream library (discord.py, gspread, google-auth) deprecating something the bot depends on will not surface until it's a hard break. `PytestUnraisableExceptionWarning` suppression also hides exceptions raised in `__del__`/GC contexts that would otherwise be worth knowing about.
  - **Recommendation**: not urgent, but worth periodically running the suite once with these filters removed (e.g. a manual/scheduled CI job) to catch upcoming breakage before it's forced.

### Low

- **[Coverage] `setup_cog.py` (11,166 lines, the single largest source file in the repo) has no directly-named `test_setup_cog.py`.** It is well covered in practice — `tests/integration/test_setup_flows.py` (1,930 lines), `tests/integration/test_setup_branches.py` (491 lines), `tests/integration/test_command_coverage.py`, and roughly a dozen more unit files (`test_ask_*`, `test_channel_select_step.py`, `test_role_select_step.py`, `test_timezone_select_view.py`, `test_modal_launch_view.py`, `test_has_leadership_or_admin.py`, etc.) all import from it. This is a naming/discoverability note only, not a coverage gap — flagging so a future reviewer doesn't assume it's untested from the missing `test_setup_cog.py` filename alone.
  - **Recommendation**: none required; optionally note the coverage split in a comment at the top of `setup_cog.py` for future navigability, given its size.

- **[Tooling] No coverage-percentage tool/threshold anywhere (no `pytest-cov`, no `coverage.xml` gate in CI).** The repo clearly values behavioral test coverage (111 files, 55K lines of tests) but there's no quantified floor, so a coverage regression on a new file wouldn't be caught mechanically — only by a human noticing a missing test file, which is exactly what this audit had to do manually.
  - **Recommendation**: low priority given the manual cross-reference in this doc found only 3 real gaps out of 65 files; consider `pytest-cov` with a soft/informational threshold (not a hard gate) if the team wants this automated going forward.

## 4. Coverage Gap Map

Source modules with **zero** matching test file and **zero** reference (import or string match) anywhere under `tests/`, ordered by source file size (largest first). Cross-referenced against the full 65-file root source list; every other source file has either a directly-named test file or is imported by at least one test file with a different name (e.g. `setup_cog.py` is exercised by `test_setup_flows.py` et al.; `storm_log.py` by `test_storm_member_log.py`/`test_storm_attendance.py`/etc.; `train_cog.py` by `test_train_reminder_loop.py`/`test_birthday_conflict_view.py`/etc.).

| Source file | Lines | Notes |
|---|---|---|
| `transfers_hub.py` | 281 | Hub/UI coordinator for the Transfer feature (parallel to `buddy_hub.py`, `train_hub.py`, `mapmanager_hub.py` — all of which **do** have test files). No test file, no reference anywhere in `tests/`. |
| `stats_publisher.py` | 179 | Publishes stats (likely to a channel/webhook on a schedule) — no test file, no reference. Scheduled/background publishers are exactly the kind of code that fails silently in production without a test. |
| `buddy_cog.py` | 33 | Thin cog wrapper (mirrors the already-tested `mapmanager_cog.py` pattern, 33 lines) — no direct test, but low complexity/low risk. |

**Note on methodology**: this table reflects files with *no* reference at all. Several other files initially looked untested by filename alone (`bot.py`, `bot_state.py`, `defaults.py`, `donate.py`, `export_import_cog.py`, `help_content.py`, `setup_cog.py`, `storm_commands_root.py`, `storm_log.py`, `train_birthdays.py`, `train_cog.py`) but are all imported/exercised by at least one differently-named test file (mostly integration tests and the larger storm/train/setup unit suites) — confirmed via `grep -rl` across `tests/` before being excluded from this table, per the standards doc's "grep before flagging" rule.

## 5. What's already optimal

- **Skip/xfail hygiene is excellent.** Only one skip mechanism exists in the entire suite (`tests/conftest.py:36-48`, the `free_tier_only` marker that auto-skips free-tier-behavior tests during the CI `FORCE_PREMIUM=1` pass), and it's well-documented with a clear rationale. Zero `xfail` markers anywhere — no tests are being carried in a permanently-broken state.
- **CI runs the suite twice under different tenancy conditions** (`test.yml` lines 38-54): once as free-tier, once with `FORCE_PREMIUM=1`. The workflow comment explicitly calls out that this catches premium-only code paths (multi-template manager, `FORCE_PREMIUM` short-circuit, mention-or-name parsing, new survey question types) the default run wouldn't exercise — a deliberate design decision to widen behavioral coverage without writing a second copy of every test.
- **Live-Sheets tests are isolated, quota-aware, and documented.** `tests/sheets/` is gated behind a `sheets` pytest marker, only runs in CI on `main` (not every PR), and `test.yml` lines 82-96 document *why* `pytest-rerunfailures` with a 65-second delay is used (Google Sheets' 60-writes/minute-per-user quota) rather than just being copy-pasted boilerplate. `tests/conftest.py`'s `sheets_client`/`test_spreadsheet` fixtures degrade gracefully (`pytest.skip`) when credentials aren't available, so the file doesn't hard-fail for a contributor without sheets access.
- **Mocking discipline is consistent and realistic.** `tests/conftest.py`'s `make_mock_interaction`/`make_mock_channel`/`make_mock_bot` helpers correctly distinguish `AsyncMock` (for `channel.send`, `interaction.response.send_message`, awaited Discord API surfaces) from `MagicMock` (for synchronous attributes like `user.guild_permissions`), and are reused as the shared fixture surface across the whole suite rather than each test file rolling its own mock shape.
- **`test_command_coverage.py` is a genuine regression net, not a smoke test in name only.** It asserts the *exact* expected set of registered slash commands per cog (so a renamed/removed/un-decorated command fails immediately) and separately drives every command's rejection path (permission-denied, not-set-up, premium-required) to catch import-time errors and silent dead-end branches — this is exactly the kind of breadth-over-depth test that pays for itself on a codebase this large.
- **Tooling config is self-documenting.** `ruff.toml` and `.pre-commit-config.yaml` both carry inline comments explaining *why* a rule is off or a hook is scoped the way it is (e.g. the F401/F811 rationale tied to specific bugs it caused during the initial sweep) — this is unusually good practice and made this audit's job easier; most repos leave config files bare.

## 6. Open questions

- Is the absence of `mypy`/`pyright` a deliberate choice (e.g. discord.py's typing story historically being incomplete/painful) or simply never prioritized? Worth confirming with Kevin before recommending adoption, since a strict pass on a 76K-line untyped codebase would be a large first-time lift.
- Should `ruff check`/`ruff format --check` be added to CI as a blocking gate, or would Kevin prefer to keep lint enforcement purely at the pre-commit layer (trusting contributor discipline) to avoid slowing down CI or creating a new failure mode for drive-by pushes?
- `buddy_cog.py`, `stats_publisher.py`, and `transfers_hub.py` having zero test references — is this intentional (thin/low-risk enough not to warrant a test) or an oversight? `stats_publisher.py` in particular (179 lines, scheduled/background-sounding name) seems worth a second look given the standards doc's emphasis on rate-limit/background-task correctness.
- Is there an appetite for a coverage-percentage tool (`pytest-cov`) even as an informational/non-blocking signal, given the manual file-by-file cross-reference this audit had to do to find the 3 real gaps? A repeatable, automated version of that check would make future audits cheaper.
