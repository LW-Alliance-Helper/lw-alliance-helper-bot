---
name: dry-consolidation
description: Find code duplicated across modules and extract it into one shared helper. Use when the same logic appears in two or more files, when a feature was built by copying a sibling feature, or when a review turns up "this is the third place that does this". Runs a real clone detector first — never eyeball files for duplication. Not for tidying a single file (use /simplify) and not for finding bugs (use /code-review).
---

# DRY consolidation

Find duplicated code with a detector, confirm it structurally, then extract it
into one helper that everything calls.

Adapted from `dry-consolidation` in [laurigates/claude-plugins](https://github.com/laurigates/claude-plugins)
(MIT). The 7-step method is upstream's. The classification table, the
destinations, the guard rails and the verification commands are this repo's.

---

## Read this before proposing a single extraction

**1. `CLAUDE.md` § "Patterns to reuse" is off limits.** It opens with *"These
are deliberate and tested. Don't refactor away."* Everything listed there —
the `ask_keep_or_change` wizard pattern, `wait_view_or_cancel`, the DM body
templates, the premium gating shape, the inline-import hook — will look like
duplication to a clone detector, because it is repetition on purpose. Load that
section first and treat every pattern in it as **preserved, not a candidate**.

**2. Never remove an unused import as part of this work.** `ruff.toml` disables
F401 and F811 deliberately, and says why. Two patterns depend on it:

- Module-level re-exports (`train.py` re-exports `parse_birthday` from
  `train_birthdays` so `from train import parse_birthday` keeps working).
- Inline `from config import X` *inside a function* that also imports `X` at
  module level — a late-binding hook so tests can patch `config.X`.

An import cleanup deletes both and the tests still pass, because what breaks is
the *patching*, silently. Step 6 upstream says "remove unused imports". Here it
does not. If an import genuinely became dead, say so in the summary and leave
it for a human.

**3. `tests/` is out of scope by default.** It is 93,165 lines — 43% of the
Python — and consolidating a test helper can thin coverage without a single
test turning red. Pass an explicit `tests/` path if you mean it.

**4. User-facing strings go through sign-off.** If an extraction touches copy
that a person reads, the string moves to `messages.py` and the change needs
approval — load `copy-signoff`. Do not reword while extracting.

---

## Step 1 — Discover clone clusters with a detector

Enumerate duplicate ranges mechanically, then **read only the reported line
ranges**. Do not read whole candidate files, and do not form an impression of
duplication by browsing. At 124,000 lines an impression is not evidence.

`jscpd` is token-based, handles Python despite the name, and needs no install:

```bash
npx jscpd --reporters json --min-tokens 60 --output /tmp/jscpd-out --silent \
  --format python --ignore "tests/**,.venv/**,assets/**" .
```

Read `/tmp/jscpd-out/jscpd-report.json` and parse `duplicates[]`. Each entry
gives both file paths, exact line ranges, and the clone's size in tokens and
lines. Read those ranges with `Read`'s `offset`/`limit`, nothing more.

`--min-tokens 60` is tuned for this codebase. Lower it to 40 when hunting small
repeated helpers; raise it to 100 when the report is too noisy to triage.

**Scope it.** A whole-repo run produces more clusters than one session can act
on. Prefer one feature family at a time — `champion_duel_*`, `storm_*`,
`train_*`, `alliance_duel_*` — which is also how the duplication actually
arose, since features here were built by copying their siblings.

## Step 2 — Confirm the shape structurally

A token match can be coincidence. Confirm the cluster is the same *shape* —
same call form, same block modulo renamed variables — with ast-grep
metavariables, where `$VAR` matches any identifier and `$$$ARGS` any argument
list:

```bash
npx @ast-grep/cli -p 'async def $NAME($$$ARGS) -> discord.Embed: $$$' --lang py .
npx @ast-grep/cli -p 'await interaction.response.send_message($$$ARGS)' --lang py .
```

See `ast-grep-search` for the pattern vocabulary. Use it to separate a genuine
extractable duplicate from two blocks that happen to tokenize alike.

**Fallback.** If `npx` is unavailable, fall back to Grep over the feature
prefixes plus Glob over the sibling naming convention (`*_hub.py`, `*_db.py`,
`*_ui.py`, `*_cog.py`). Recall is meaningfully lower for renamed variables —
say so in the report rather than presenting a grep sweep as a clone scan.

## Step 3 — Classify each cluster

| What it is | Where it goes | Notes |
|---|---|---|
| Pure helper used by 2+ feature families | New `<topic>_helpers.py` at root | Follows the `storm_date_helpers.py` precedent |
| Helper used inside one feature family | That family's existing module | e.g. `champion_duel_store.py`, `storm_permissions.py` |
| A user-facing string | `messages.py` | Needs `copy-signoff` approval — see guard rail 4 |
| A default or tunable constant | `defaults.py` or `config.py` | Check `config.py` first; it is already 6,948 lines |
| Discord view / embed furniture | `wizard_registry.py` or the family's `*_ui.py` | Check `CLAUDE.md` § Patterns to reuse first |
| A DB access shape | The family's `*_db.py` | Never introduce a second DB layer |

The repo is a **flat root layout** with feature-prefixed modules. Do not
introduce `src/`, `lib/`, `utils/` or a package directory — that is a
restructure, not a consolidation, and it is not this skill's call to make.

## Step 4 — Plan, and show the plan before editing

```
## Extraction plan

### 1. <helper name> → <target module>
- Replaces: N blocks across M files
- Consumers: <every file that will change>
- Parameters: <the variations being parameterised>
- Duplicated: N tokens / N lines            (from jscpd; blank if grep fallback)
- Confirmed: ast-grep same-shape | token-only
- Preserved nearby: <anything in CLAUDE.md § Patterns to reuse this sits next to>
- Lines saved: ~N
```

Present it and stop. Extraction without a reviewed plan is how a "cleanup"
becomes a rewrite. If more than about six clusters survive triage, hand the
list to `bulk-sweep-classify` rather than working through it ad hoc.

## Step 5 — Extract

Order: pure helpers first (no dependencies), then family-scoped helpers, then
anything touching Discord views.

Parameterise the differences between instances rather than creating two
helpers. If a "difference" turns out to be a behaviour change nobody
intended, that is a bug — report it, do not silently normalise it.

Add the import at the consumer. Do not touch any other import in the file.

## Step 6 — Test the new helper

Every extracted helper gets unit tests in `tests/unit/`, matching the
convention of the module it came from. Cover each variation the parameters
were introduced to absorb — that is the whole risk surface of an extraction.

Do not delete or merge the consumers' existing tests. They now cover the
call sites, which is still worth something.

## Step 7 — Verify

Worktrees have no `champion_duel_engine`, so tests run against the main
checkout's interpreter by absolute path:

```bash
/c/Users/Kevin/Documents/GitHub/lw-alliance-helper/lw-alliance-helper-bot/.venv/Scripts/python.exe \
  -m pytest tests/unit/test_<module>.py -q
```

Run **targeted selectors for what changed**, not the full suite — the full run
belongs to CI and to Release. Then run the premium lane too, because CI runs
both and a local pass is only the free half:

```bash
FORCE_PREMIUM=1 <same python> -m pytest tests/unit/test_<module>.py -q
```

Lint via pre-commit, which owns ruff's environment:

```bash
pre-commit run --files <changed files>
```

A red `-k champion` on a clean diff is usually the calendar, not your change —
several tests are date-dependent. Confirm against `origin/dev` before chasing it.

## Reporting

```
## DRY consolidation — <scope>

### Extracted
- <helper> (<destination>) — replaced N blocks in M files

### Preserved on purpose
- <cluster> — CLAUDE.md § Patterns to reuse
- <cluster> — re-export / config late-binding hook

### Not acted on
- <cluster> — <why: too risky, needs a decision, spans premium gating>

### Verified
- pytest <selectors>: N passed (free + FORCE_PREMIUM)
- pre-commit: clean
```

Report what you preserved and why, not only what you changed. A consolidation
pass that lists only its edits is unreviewable.

## Related

- `ast-grep-search` — the structural pattern vocabulary Step 2 uses
- `bulk-sweep-classify` — when the cluster list is long enough to need triage
- `code-complexity` — ranks modules, so run it first to pick a scope
- `/simplify` — single-file tidying; this skill is cross-file only
- `/code-review` — correctness; this skill assumes the code is already correct
