---
name: ast-grep-search
description: Search Python by AST shape instead of text — find every call of a given form, every function with a given signature, every bare except, regardless of how the variables were named. Use when a grep would need too many spellings, when confirming a duplication cluster is really the same shape, or when a rename has to hit call sites without touching strings and comments. Also the tool for the blocking-I/O-in-async class that ruff.toml says needs a human grep.
---

# Structural search with ast-grep

Match Python by its syntax tree, not its characters. A pattern that names a
shape finds every instance of that shape whatever the identifiers were called.

Adapted from `ast-grep-search` in [laurigates/claude-plugins](https://github.com/laurigates/claude-plugins)
(MIT). Trimmed to Python and given this codebase's patterns.

---

## Running it

Not installed. Use the one-shot runner — node is present:

```bash
npx @ast-grep/cli -p '<pattern>' --lang py <path>
```

## Pattern vocabulary

| Token | Matches |
|---|---|
| `$VAR` | One node, captured — reusable in a rewrite |
| `$$$ARGS` | Zero or more nodes (any argument list, any body) |
| `$_` | One node, not captured |
| `$A == $A` | The *same* node twice — finds `x == x`, not `x == y` |

Because patterns match AST nodes, they never fire inside strings, comments or
URLs. That is the whole reason to reach for this over grep on a rename.

## When this beats Grep

Use ast-grep when the pattern needs "any arguments", "any name", or "any
expression" — a call shape, a function signature, a block form. Use Grep for a
literal string, a log message, or a filename. If you are about to write a regex
with three alternations to cover naming variations, that is the signal.

---

## Patterns that matter in this repo

**The blocking-I/O-in-async gap.** `ruff.toml` selects `ASYNC` but says plainly
that it only catches *stdlib* blocking calls — "ASYNC does NOT know about our
own blocking I/O (gspread, sqlite) since those aren't stdlib — that class of bug
still needs a human grep." This is that grep, done structurally:

```bash
# sqlite3 called without await inside the module — inspect each hit's enclosing def
npx @ast-grep/cli -p 'sqlite3.connect($$$)' --lang py .
npx @ast-grep/cli -p '$CONN.execute($$$)' --lang py .

# gspread / Sheets access
npx @ast-grep/cli -p '$SHEET.get_all_records($$$)' --lang py .
npx @ast-grep/cli -p '$SHEET.update($$$)' --lang py .
npx @ast-grep/cli -p '$CLIENT.open_by_key($$$)' --lang py .
```

Each hit still needs a human read of whether the enclosing function is `async
def` — ast-grep finds the call, not the context. But it finds *all* of them,
which a name-based grep does not.

**Discord surface inventory** — useful before a UX or copy pass:

```bash
npx @ast-grep/cli -p 'await interaction.response.send_message($$$)' --lang py .
npx @ast-grep/cli -p 'discord.Embed($$$)' --lang py .
npx @ast-grep/cli -p 'class $NAME(discord.ui.View): $$$' --lang py .
npx @ast-grep/cli -p '@discord.ui.button($$$)' --lang py .
npx @ast-grep/cli -p '@tasks.loop($$$)' --lang py .
```

**Error handling shapes** — the silent-failure class:

```bash
npx @ast-grep/cli -p 'try: $$$
except: $$$' --lang py .
npx @ast-grep/cli -p 'except $E: pass' --lang py .
npx @ast-grep/cli -p 'except Exception: $$$' --lang py .
```

**Confirming a duplication cluster** is the same shape rather than a token
coincidence — this is what `dry-consolidation` Step 2 calls:

```bash
npx @ast-grep/cli -p 'async def $NAME($$$ARGS) -> discord.Embed: $$$' --lang py .
```

## Output for scripting

```bash
# Just the file list
npx @ast-grep/cli -p '<pattern>' --lang py --json=stream . | jq -r '.file' | sort -u

# Count
npx @ast-grep/cli -p '<pattern>' --lang py --json=stream . | wc -l

# Debug a pattern that matches nothing
npx @ast-grep/cli -p '<pattern>' --lang py --debug-query .
```

A pattern returning zero matches is usually a malformed pattern, not a clean
codebase. Check it with `--debug-query` before reporting an absence.

---

## Rewriting — read this before using `-U`

```bash
# Preview (default: shows matches, changes nothing)
npx @ast-grep/cli -p 'old_helper($$$ARGS)' -r 'new_helper($$$ARGS)' --lang py .

# Interactive, one at a time
npx @ast-grep/cli -p 'old_helper($$$ARGS)' -r 'new_helper($$$ARGS)' --lang py -i .

# Apply everywhere — see the warning below
npx @ast-grep/cli -p 'old_helper($$$ARGS)' -r 'new_helper($$$ARGS)' --lang py -U .
```

**`-U` rewrites every match across the tree with no review.** Before using it
here:

- The working tree may hold **another session's uncommitted work** — parallel
  sessions share this checkout. Check `git status` first, and scope the path.
- Structural matching leaves the old form standing inside strings, comments,
  docstrings and error messages. Those are often user-facing, and copy changes
  need sign-off. A rewrite that "succeeded" can leave documentation asserting
  the old behaviour.
- For anything beyond a handful of call sites, run it through
  `bulk-sweep-classify` — classify every match first, then scope the transform.

Never point `-U` at the repo root.

## Related

- `bulk-sweep-classify` — the discipline for any rewrite touching many files
- `dry-consolidation` — Step 2 uses this skill to confirm clone shape
- `code-complexity` — pick a scope before searching the whole tree
