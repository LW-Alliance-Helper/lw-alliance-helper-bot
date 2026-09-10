---
name: code-complexity
description: Rank modules and functions by measured complexity to pick what to clean up next. Run this FIRST in any cleanup session — it turns "the codebase feels messy" into a ranked list of targets. Use when choosing a refactoring target, sizing a module before touching it, or checking whether a change made a file worse. Metrics come from a tool; never count branches or lines by hand.
---

# Complexity map

Measure, then rank. This is the map that decides where the other cleanup
skills get pointed.

Adapted from `code-complexity` in [laurigates/claude-plugins](https://github.com/laurigates/claude-plugins)
(MIT). Trimmed to the Python path and given this repo's thresholds and
known baseline.

---

## Offload the counting, always

Cyclomatic complexity, function length, nesting depth and parameter count are
**computed by a tool**. Never eyeball function boundaries or count branches by
reading. Hand-counting is irreproducible between runs, expensive in tokens, and
exactly the mechanical work a deterministic tool should do.

`radon` is the Python tool. **Installed** (6.0.1) in the shared venv. It is dev
tooling, so it must **not** go into `requirements.txt` — that file is the bot's
runtime dependency list and Railway installs from it.

Worktrees have no interpreter of their own; call the main checkout's by
absolute path:

```bash
PY=/c/Users/Kevin/Documents/GitHub/lw-alliance-helper/lw-alliance-helper-bot/.venv/Scripts/python.exe
$PY -m radon --version     # 6.0.1
```

If a future venv rebuild loses it: `$PY -m pip install radon`. Ask first — the
venv is shared with every other worktree and session.

---

## What is already known

Do not rediscover the five largest modules; the script prints them and
`wc -l *.py | sort -n | tail -6` does too. Hard-coded numbers in a skill file
rot (the table that used to sit here was three releases stale when it was
first checked), so this file carries commands, not figures.

The useful question is not *which files are big* but **which functions inside
them carry the complexity**, and whether a big file is one tangled thing or
forty tidy ones sharing a filename. That distinction decides whether a module
needs splitting or leaving alone.

**The maintainability index floors at 0.00 on anything over about 3,000
lines** (twelve files on 2026-09-09, not three), so it cannot rank the big
modules or show whether a cleanup helped. Use `radon cc` per function there;
`mi` still discriminates on mid-sized modules.

## Step 1 — Measure

The measurement is a script. Run it; do not re-derive it.

```bash
PY=/c/Users/Kevin/Documents/GitHub/lw-alliance-helper/lw-alliance-helper-bot/.venv/Scripts/python.exe
$PY scripts/quality/complexity_map.py                       # whole repo, ~25 s
$PY scripts/quality/complexity_map.py storm_                # one family, by filename prefix
$PY scripts/quality/complexity_map.py --since 2026-07-01 --json map.json
```

It runs radon for cyclomatic complexity, walks the AST for nesting depth,
parameter count and branch density (radon reports none of those), pulls
per-file churn from `git log`, and prints the report in Step 3's format with
a suggested order. `--json` writes the full measurement for a side-by-side
after a cleanup pass.

The raw radon commands, for a quick look at one file:

```bash
$PY -m radon cc <file>.py -s --min C
$PY -m radon mi <file>.py -s
```

## Step 2 — Rank against these thresholds

| Metric | Fine | Watch | Act |
|---|---|---|---|
| Cyclomatic complexity | 1–5 | 6–10 | 11+ |
| Function length (lines) | 1–40 | 41–80 | 81+ |
| Nesting depth | 1–3 | 4 | 5+ |
| Parameters | 1–4 | 5–6 | 7+ |
| Branches per 100 lines (functions of 100+ lines) | under 20 | | 20+ ("dense") |

**Churn is not a threshold but it is a column**, and on the first run it
changed the suggested order more than any complexity number did. Complexity
nobody touches is a lower risk than complexity that changes weekly. The script
prints commits-since per function's file; read the two together.

Function length is set higher than upstream's 25/50 on purpose. Discord command
handlers and wizard steps are legitimately long — they walk a user through a
sequence, and splitting one into six helpers called once each makes it harder to
read, not easier. **Length alone is not a finding here. Length plus branching
is.** A 200-line handler with a CC of 4 is fine; a 60-line one with a CC of 18
is the actual problem.

## Step 3 — Report a ranked target list

```
## Complexity map — <scope>

Functions measured: N     Average CC: X.X

### Act (CC 11+)
  module.py           function_name          CC 18   120 lines   depth 6
  ...

### Watch (CC 6-10, long)
  ...

### Files where complexity concentrates
  module.py     N functions over threshold, out of M

### Suggested order
1. <module>: <function> (CC N) — <why it is first: deepest, or most changed, or blocks other work>
```

End with a **suggested order**, not just a table, and give **one line of
reasoning per target**. The point of the map is picking the next target, and a
ranked list without a recommendation leaves that work undone; a recommendation
without a reason cannot be reordered. The script's order is a proposal that
weighs tangle first and churn second; say where you moved it and why. The
first run's own reordering: length alone did not make a wizard a target, depth
did, and the most-changed files went to `dry-consolidation` rather than to a
refactor.

## Step 4 — Hand off

Once the map exists, route each target:

- Tangled logic in one file → `/code-review high <path>`, then `/simplify`
- The same shape repeated across files → `dry-consolidation`
- Functions nothing calls → `code-dead-code`
- A rename or migration across many call sites → `bulk-sweep-classify`

## Re-running

Re-measure the same scope after a cleanup pass (`--json` before and after)
and put the two tables side by side. That is the only honest way to say
whether a session improved anything. "Feels cleaner" is not a result.

## Related

- `dry-consolidation` — for the duplication this map will surface
- `code-dead-code` — for the functions this map shows nothing reaching
- `/code-review` — for the correctness questions complexity hints at
