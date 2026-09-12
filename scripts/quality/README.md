# Quality checks

The measurements behind the cleanup skills in `.claude/skills/`. Each skill
used to describe its check in prose and every session re-derived it; these
are the checks, committed, so a run starts where the last one ended.

All of it is dev tooling. **Nothing here goes in `requirements.txt`**, which is
the bot's runtime dependency list. `radon` and `vulture` live in the shared
venv; `ast-grep` and `jscpd` run through `npx` and need node.

Run from the repo root with the main checkout's interpreter (worktrees have
none of their own):

```sh
PY=/c/Users/Kevin/Documents/GitHub/lw-alliance-helper/lw-alliance-helper-bot/.venv/Scripts/python.exe
```

| Script | Skill | What it prints | Time |
|---|---|---|---|
| `complexity_map.py [prefix] [--since DATE] [--json PATH]` | `code-complexity` | Functions ranked by cyclomatic complexity, with nesting depth, parameter count, branch density and per-file churn, then a suggested order with a reason per target | ~25 s whole repo, ~3 s per family |
| `blocking_io.py [--json PATH]` | `ast-grep-search` | Blocking gspread and sqlite calls inside coroutines: direct calls, and coroutines calling the bot's own sync helpers without a thread hand-off, split by network versus local | ~35 s |
| `dead_code.py [prefix] [--json PATH]` | `code-dead-code` | Vulture at the band that finds functions, after `.vulture-whitelist.py`, with every hit bucketed by where the repo references it | ~12 s per family |

The `ast-grep/` folder holds the rule files the scripts and the skills use:

| Rule file | Finds |
|---|---|
| `blocking-in-async.yml` | Direct sqlite and gspread calls whose nearest enclosing def is `async def` and which are not handed to a thread |
| `sync-db-fns.yml` | Every sync function that opens a connection or a spreadsheet; `blocking_io.py` turns the list into the indirect rule |
| `view-handlers.yml` | Every owner guard, timeout handler, storm-style guard and inline per-callback owner check. The shared base views landed 2026-09-11 (#589); the rule is now the regression check, and its preserved set (the two non-owner `interaction_check` methods, the 23 silent timeouts, the pagination row's `_guard_owner`, the walkthrough's five inline checks) is recorded on the issue |

Run a rule file directly with:

```sh
npx --yes --package @ast-grep/cli ast-grep scan -r scripts/quality/ast-grep/<file>.yml --json=compact .
```

Two things every rule file here relies on, both found by running them rather
than by reading the docs: `utils:` must be declared inside each rule document,
not at the top of the file; and a bare call pattern such as
`expire_view_message($$$)` does not match the qualified spelling
`wizard_registry.expire_view_message(...)`, so both have to be searched.

## Re-running after a cleanup

Re-measure the same scope and put the two outputs side by side. That is the
only honest way to say whether a session improved anything. "Feels cleaner" is
not a result.

## Adding a check

When an audit finding names a *class* of problem rather than a list of sites,
it ships the rule that finds the class, here. The 1.8.0 blocking-I/O sweep
closed on its named sites and left 284 in the class it had called "worth a
repo-wide look"; `blocking_io.py` is what that look should have been.
