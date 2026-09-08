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

`radon` is the Python tool. It is **not** in the venv, and it is dev tooling, so
it must **not** go into `requirements.txt` — that file is the bot's runtime
dependency list and Railway installs from it.

```bash
/c/Users/Kevin/Documents/GitHub/lw-alliance-helper/lw-alliance-helper-bot/.venv/Scripts/python.exe \
  -m pip install radon
```

Ask before running that the first time. It changes the shared venv, which other
sessions and worktrees use.

---

## What is already known

You do not need a tool to discover these, and reporting them as findings wastes
the run. On `origin/dev`, five modules hold 35% of all source:

| Module | Lines |
|---|---|
| `setup_cog.py` | 12,015 |
| `champion_duel_hub.py` | 11,403 |
| `storm_roster_builder.py` | 7,227 |
| `config.py` | 6,948 |
| `champion_duel_db.py` | 5,865 |

The useful question is not *which files are big* but **which functions inside
them carry the complexity**, and whether a big file is one tangled thing or
forty tidy ones sharing a filename. That distinction decides whether a module
needs splitting or leaving alone.

## Step 1 — Measure

```bash
# Cyclomatic complexity, ranked, B and worse, excluding tests
radon cc . -s -a --min B -e "tests/*,.venv/*,assets/*,scripts/*"

# Maintainability index per file
radon mi . -s -e "tests/*,.venv/*,assets/*"

# Machine-readable, when you want to sort or diff it
radon cc . -j --min B -e "tests/*,.venv/*"
```

Scope to a feature family when chasing something specific:

```bash
radon cc champion_duel_*.py -s --min C
```

## Step 2 — Rank against these thresholds

| Metric | Fine | Watch | Act |
|---|---|---|---|
| Cyclomatic complexity | 1–5 | 6–10 | 11+ |
| Function length (lines) | 1–40 | 41–80 | 81+ |
| Nesting depth | 1–3 | 4 | 5+ |
| Parameters | 1–4 | 5–6 | 7+ |

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
1. <module> — <why it is first: highest CC, or most consumers, or blocks other work>
```

End with a **suggested order**, not just a table. The point of the map is
picking the next target, and a ranked list without a recommendation leaves that
work undone.

## Step 4 — Hand off

Once the map exists, route each target:

- Tangled logic in one file → `/code-review high <path>`, then `/simplify`
- The same shape repeated across files → `dry-consolidation`
- Functions nothing calls → `code-dead-code`
- A rename or migration across many call sites → `bulk-sweep-classify`

## Re-running

Re-measure the same scope after a cleanup pass and put the two tables side by
side. That is the only honest way to say whether a session improved anything.
"Feels cleaner" is not a result.

## Related

- `dry-consolidation` — for the duplication this map will surface
- `code-dead-code` — for the functions this map shows nothing reaching
- `/code-review` — for the correctness questions complexity hints at
