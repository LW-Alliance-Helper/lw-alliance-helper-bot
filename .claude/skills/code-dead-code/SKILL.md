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

## Step 1 — Install the tool

`vulture` is not in the venv, and it is dev tooling — it must **not** go into
`requirements.txt`, which is the bot's runtime dependency list.

```bash
/c/Users/Kevin/Documents/GitHub/lw-alliance-helper/lw-alliance-helper-bot/.venv/Scripts/python.exe \
  -m pip install vulture
```

Ask before the first install. It changes the shared venv.

## Step 2 — Build the whitelist before the first run

Vulture takes a whitelist file of names to treat as used. Without one the first
run is thousands of lines of framework callbacks and the signal is gone.

Write `.vulture-whitelist.py` covering the three families above — decorated
callback names, the re-exported symbols, and anything reached only from
`messages.py` or config. Commit it; it is as much a project document as
`ruff.toml`, and the next session needs it too.

## Step 3 — Scan

```bash
VULTURE=/c/Users/Kevin/Documents/GitHub/lw-alliance-helper/lw-alliance-helper-bot/.venv/Scripts/python.exe

# High confidence only — start here
$VULTURE -m vulture . .vulture-whitelist.py \
  --min-confidence 90 \
  --exclude ".venv,tests,assets,scripts"

# Widen once the 90% band is clean and triaged
$VULTURE -m vulture . .vulture-whitelist.py --min-confidence 70 --exclude ".venv,tests,assets"
```

Start at 90. Below 60 the output is noise on a codebase this size.

Scope to a feature family when auditing something specific — that is also the
only run where the results are small enough to check one by one:

```bash
$VULTURE -m vulture champion_duel_*.py .vulture-whitelist.py --min-confidence 80
```

## Step 4 — Prove each suspect before proposing removal

For every hit, do all three. A name that survives all three is a candidate; a
name that fails any one of them goes in the whitelist instead.

1. **Grep the whole repo for the bare name**, including `tests/`, `scripts/`,
   `api/` and `.github/`. A function called only from a test is not dead — it is
   a tested helper whose caller you have not found yet.
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
$VULTURE -m pytest tests/unit/test_<module>.py -q
FORCE_PREMIUM=1 $VULTURE -m pytest tests/unit/test_<module>.py -q
```

## Related

- `code-complexity` — run first; it says which modules are worth auditing
- `dry-consolidation` — dead code often appears after an extraction, not before
- `bulk-sweep-classify` — if removal means a rename across many call sites
