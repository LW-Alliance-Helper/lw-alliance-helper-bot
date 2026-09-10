---
name: ast-grep-search
description: Search Python by AST shape instead of text — find every call of a given form, every function with a given signature, every swallowed exception, regardless of how the variables were named. Use when a grep would need too many spellings, when confirming a duplication cluster is really the same shape, or when a rename has to hit call sites without touching strings and comments. Also the tool for the blocking-I/O-in-async class that ruff.toml says needs a human grep.
---

# Structural search with ast-grep

Match Python by its syntax tree, not its characters. A pattern that names a
shape finds every instance of that shape whatever the identifiers were called.

Adapted from `ast-grep-search` in [laurigates/claude-plugins](https://github.com/laurigates/claude-plugins)
(MIT). Trimmed to Python, given this codebase's patterns, and corrected — the
upstream invocation does not run on this machine.

---

## Running it — use exactly this form

ast-grep is not installed. It runs through `npx`, but **the package name and the
binary name differ**, so the obvious command fails:

```bash
npx @ast-grep/cli -p '...'        # ✗ "npm error could not determine executable to run"
```

The working form names the package and the binary separately:

```bash
npx --yes --package @ast-grep/cli ast-grep -p '<pattern>' --lang py <paths>
```

Set it once per session and reuse it:

```bash
AG="npx --yes --package @ast-grep/cli ast-grep"
$AG -p 'discord.Embed($$$)' --lang py .
```

## Single-line patterns inline, multi-line patterns in a file

**This is the rule that matters.** A multi-line pattern passed inline to `-p`
gets mangled by the shell: ast-grep silently stops honouring `--lang`, falls
back to plain text matching, and returns hits from `README.md` and `CLAUDE.md`.
It exits 0 while doing so, so nothing tells you it went wrong.

- **One line** → inline with `-p`.
- **More than one line** → a YAML rule file, run with `scan -r`.

```bash
# multi-line: write the rule to a file first (in the session scratchpad, not C:/tmp)
cat > $SCRATCHPAD/ag-rule.yml <<'YAML'
id: except-pass
language: Python
severity: warning
message: except block that swallows the error
rule:
  pattern: |
    try:
        $$$BODY
    except $E:
        pass
YAML

$AG scan -r $SCRATCHPAD/ag-rule.yml <paths>
```

Rule files can express **context**, which a pattern cannot: `inside`,
`not`, `has`, `all`, `any`, with `stopBy` to bound the search. Reusable
pieces go under `utils:` and are referenced with `matches:`. **`utils:` must
be declared inside each rule document**; a top-level `utils:` block in a
multi-document file fails to parse with "missing field `language`". The
committed rule files under `scripts/quality/ast-grep/` are the worked
examples.

Two spellings of one call are two tree shapes. `expire_view_message($$$)`
does not match `wizard_registry.expire_view_message(...)`; when a helper can
be called bare or qualified, search both (`name($$$A)` and `$M.name($$$A)`).

`--inline-rules` exists but is fragile through this shell for the same reason.
Use the file.

## Pattern vocabulary

| Token | Matches |
|---|---|
| `$VAR` | One node, captured — reusable in a rewrite |
| `$$$ARGS` | Zero or more nodes (any argument list, any body) |
| `$_` | One node, not captured |
| `$A == $A` | The *same* node twice — finds `x == x`, not `x == y` |

Patterns match AST nodes, so they never fire inside strings, comments or URLs.
That is the whole reason to reach for this over grep on a rename.

A pattern must be a **complete syntactic unit**. `except $E: pass` on its own
fails with "Multiple AST nodes are detected" — Python needs the enclosing `try`,
which is why that particular check needs a rule file.

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
still needs a human grep." This is that grep, and it is committed:

```bash
PY=/c/Users/Kevin/Documents/GitHub/lw-alliance-helper/lw-alliance-helper-bot/.venv/Scripts/python.exe
$PY scripts/quality/blocking_io.py          # ~35 s repo-wide
```

It answers in two halves, because the question has two halves:

- **Direct:** a `conn.execute` or a gspread call whose nearest enclosing def
  is `async def` and which is not an argument of `asyncio.to_thread` or
  `run_in_executor`. That is `scripts/quality/ast-grep/blocking-in-async.yml`,
  a rule file with the context encoded. On the first run this turned 586
  bare-pattern hits into 13.
- **Indirect:** a coroutine calling one of the bot's *own* sync helpers
  (`config.get_config`, the `*_db` functions) without a hand-off. No fixed
  pattern can see this. The script generates the pattern list from the AST
  (`sync-db-fns.yml` lists every sync function that opens a connection or a
  spreadsheet, 280 on the first run) and searches for calls to any of them.
  That is the **generate-then-search two-step**, and it is the larger half:
  284 sites on the first run, plus one network call the 1.8.0 sweep missed.

Do not write "ast-grep finds the call, not the context". Rule files find the
context; that is the point of `inside` and `not`. What they cannot do is tell
a gspread worksheet from a dict or a set by method name, so `.update(` and
`.clear(` are not in the rule and need a filter step if you add them.

**Discord surface inventory** — useful before a UX or copy pass:

```bash
$AG -p 'await interaction.response.send_message($$$)' --lang py .
$AG -p 'discord.Embed($$$)' --lang py .          # 179 matches on origin/dev
$AG -p 'class $NAME(discord.ui.View): $$$' --lang py .
$AG -p '@discord.ui.button($$$)' --lang py .
$AG -p '@tasks.loop($$$)' --lang py .
```

**Swallowed exceptions** — the silent-failure class. Multi-line, so rule files:

```yaml
# bare-except.yml
id: bare-except
language: Python
severity: warning
message: bare except catches everything including KeyboardInterrupt
rule:
  pattern: |
    try:
        $$$BODY
    except:
        $$$HANDLER
```

The `except-pass` rule above already finds real hits in `scheduler.py`. Treat
them as questions, not defects — some are deliberate. `silent-failure-hunter`
from `pr-review-toolkit` is the agent that judges them.

**Confirming a duplication cluster** is the same shape rather than a token
coincidence — this is what `dry-consolidation` Step 2 calls:

```bash
$AG -p 'async def $NAME($$$ARGS) -> discord.Embed: $$$' --lang py .
```

## Output for scripting

```bash
$AG -p '<pattern>' --lang py --json=stream . | jq -r '.file' | sort -u   # file list
$AG -p '<pattern>' --lang py --json=stream . | wc -l                      # count
$AG -p '<pattern>' --lang py --debug-query .                              # why no match
```

A pattern returning zero matches is usually a malformed pattern, not a clean
codebase — and a *malformed* one can still exit 0. Check with `--debug-query`
before reporting an absence, and sanity-check that your paths and `--lang` were
actually honoured.

---

## Rewriting — read this before using `-U`

```bash
$AG -p 'old_helper($$$ARGS)' -r 'new_helper($$$ARGS)' --lang py .      # preview
$AG -p 'old_helper($$$ARGS)' -r 'new_helper($$$ARGS)' --lang py -i .   # one at a time
$AG -p 'old_helper($$$ARGS)' -r 'new_helper($$$ARGS)' --lang py -U .   # apply all
```

**`-U` rewrites every match across the tree with no review.** Before using it:

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
