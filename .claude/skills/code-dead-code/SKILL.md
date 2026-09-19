---
name: code-dead-code
description: Find code nothing reaches — orphaned functions, unreachable branches, modules left behind by a refactor. Use after a feature is removed or reworked, or when auditing what a module still needs. Output is a list of SUSPECTS, never a delete list — this codebase has three patterns that look dead to every tool and are not. Never auto-remove.
---

# Dead code

Find what nothing reaches, then prove it before removing anything.

Adapted from `code-dead-code` in [laurigates/claude-plugins](https://github.com/laurigates/claude-plugins)
(MIT). Upstream's `--fix` auto-removal is **deliberately not carried over** —
see below.

---

## Why this skill is mostly about false positives

A dead-code tool asks "does anything call this by name?" In a discord.py bot the
answer is usually no, because the framework dispatches by decorator, not by call
site. Three families here look dead to every tool and are load-bearing:

**1. Framework-dispatched callbacks.** Everything reached by decorator or by
convention rather than by a caller:

- `@bot.command`, `@app_commands.command`, `@commands.hybrid_command`
- `@bot.event` and any `async def on_*` listener
- `@discord.ui.button`, `@discord.ui.select`, `View` callback methods
- `@tasks.loop` bodies
- `setup()` / `async def setup(bot)` cog entry points

**2. Module-level re-exports.** `train.py` re-exports `parse_birthday` from
`train_birthdays` so `from train import parse_birthday` keeps working. Nothing
in `train.py` calls it. It is not dead.

**3. The config late-binding hook.** An inline `from config import X` inside a
function that also imports `X` at module level. It exists so tests can patch
`config.X`. Every tool calls it a redundant redefinition. `ruff.toml` disables
F811 for exactly this reason and explains why. Removing it does not fail a
test — it silently defeats the patching, and the tests keep passing while
covering nothing.

**This is why there is no `--fix` here.** Upstream has one. Auto-removal against
these three patterns is precisely the failure mode this whole cleanup effort
exists to stop: a change that looks clean, passes tests, and breaks in
production.

---

## Step 1 — The tool

`vulture` is **installed** (2.16) in the shared venv. It is dev tooling and must
**not** go into `requirements.txt`, which is the bot's runtime dependency list.

Worktrees have no interpreter of their own; call the main checkout's by
absolute path:

```bash
PY=/c/Users/Kevin/Documents/GitHub/lw-alliance-helper/lw-alliance-helper-bot/.venv/Scripts/python.exe
$PY -m vulture --version   # vulture 2.16
```

If a future venv rebuild loses it: `$PY -m pip install vulture`. Ask first —
the venv is shared.

**Vulture's confidence is fixed per category, not a noise curve.** Unused
imports are rated 90, unreachable code 100, and *every* unused function,
method, class, property and variable a flat **60**. So a run at 90 or 70 can
only ever find unused imports and unreachable code; it can never find a dead
function, and it will report a clean codebase that is not clean. **60 is the
working band**, and the whitelist is what makes 60 readable. (The earlier
advice here, "90 is signal, 60 is triage", was a misreading of the tool.)

## Step 2 — The whitelist is committed; grow it

`.vulture-whitelist.py` at the repo root holds every name a run has proved
live, in three sections: framework-dispatched callbacks, functions called from
outside the scanned family, and documented keeps. It is as much a project
document as `ruff.toml`. Every run adds what it proves; nothing is removed
from it without the three proofs below going the other way.

It cannot be written before the first run on a new scope (you do not know the
names yet), so on a new family: run, triage, then append the false positives
with `--make-whitelist` and a section comment saying why.

## Step 3 — Scan, with the reference check built in

```bash
PY=/c/Users/Kevin/Documents/GitHub/lw-alliance-helper/lw-alliance-helper-bot/.venv/Scripts/python.exe
$PY scripts/quality/dead_code.py champion_duel_     # one family, ~12 s
$PY scripts/quality/dead_code.py                    # whole repo
```

The script runs vulture at 60 with the whitelist applied, then does Step 4's
first proof for every hit and buckets it: no reference anywhere, tests only,
same file only, referenced elsewhere, framework. Scope to a feature family
when auditing something specific; the whole-repo run is large.

**A family scan cannot see callers in the rest of the bot.** Every database
function the admin toolkit, the API or startup calls looks dead to a scan of
`champion_duel_*.py` alone. The "referenced elsewhere" bucket is that class,
and it goes in the whitelist, not the suspect list. This is why the reference
check is mandatory, not one proof of three.

Raw vulture, for a quick look:

```bash
$PY -m vulture champion_duel_*.py .vulture-whitelist.py --min-confidence 60
```

## Step 4 — Prove each suspect before proposing removal

For every hit, do all three. A name that survives all three is a candidate; a
name that fails any one of them goes in the whitelist instead.

1. **Grep the whole repo for the bare name**, including `tests/`, `scripts/`,
   `api/` and `.github/`. The script does this; read its bucket, and read the
   actual lines for short names (`W`, `H`, `keep`, `due`), which match prose.
   A function called only from a test is not dead — it is a tested helper
   whose surface was never built, or whose caller you have not found yet.
   Either way it is a human decision, not a removal.
2. **Check for decorator dispatch.** Is it registered by a decorator, named in a
   `setup()`, or referenced as a string anywhere? Discord command names, view
   `custom_id`s and `tasks.loop` bodies are all reached without a call site.
3. **Check `git log` for the module.** Something added last week and not yet
   wired up is unfinished, not dead. Removing it deletes someone's in-progress
   work — and with parallel sessions on this repo, that work may be uncommitted
   in another worktree right now.

## Step 5 — Report suspects, not deletions

```
## Dead code suspects — <scope>

### Confirmed unreachable (all three checks passed)
  module.py:LINE  name        last touched <date>   <why it is safe>

### Whitelisted this run (looked dead, is not)
  module.py:LINE  name        framework callback | re-export | config hook

### Needs a human decision
  module.py:LINE  name        <what is ambiguous about it>
```

The middle section is not filler. It is the record that stops the next session
re-investigating the same forty names, and it should get longer over time as
the whitelist absorbs what it learns.

## Removing, when it comes to that

Removal is its own change on its own branch, reviewed as a diff — not a side
effect of an audit. Delete, then run the targeted tests for every module that
imported the removed name, in both lanes:

```bash
$PY -m pytest tests/unit/test_<module>.py -q
FORCE_PREMIUM=1 $PY -m pytest tests/unit/test_<module>.py -q
```

## Related

- `code-complexity` — run first; it says which modules are worth auditing
- `dry-consolidation` — dead code often appears after an extraction, not before
- `bulk-sweep-classify` — if removal means a rename across many call sites
