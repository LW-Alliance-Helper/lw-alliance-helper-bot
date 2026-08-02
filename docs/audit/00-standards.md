# Audit Baseline Standards

This is the checklist every section audit in `docs/audit/` is measured against. Sourced from PEP 8, PEP 257, PEP 484, standard refactoring/code-smell guidance, and discord.py's own cog conventions.

Sources:
- [discord.py Cogs docs](https://discordpy.readthedocs.io/en/stable/ext/commands/cogs.html)
- [Modular Discord Bots: A Guide to Using Cogs](https://medium.com/@ajiboyetolu1/modular-discord-bots-in-python-a-guide-to-using-cogs-d89da141c4b9)
- [God Object Anti-Pattern in Python](https://softwarepatternslexicon.com/patterns-python/11/2/4/)
- [Code Smells (refactoring.guru)](https://refactoring.guru/refactoring/smells)
- [Code Smells and Anti-Patterns (Codacy)](https://blog.codacy.com/code-smells-and-anti-patterns)

## 1. Structure & modularity

- **God file / god object**: a module or class that owns too many unrelated responsibilities. Flag any file that mixes Discord UI (views/modals/embeds), business logic, and data/sheet I/O in one place — these should generally be separable.
- **Single Responsibility**: each cog/module should cover one feature domain. A module that both defines commands and does deep data-transform work is a candidate for splitting into `_logic` / `_cog` / `_ui` (the repo already does this in places — e.g. `buddy.py` / `buddy_cog.py` / `buddy_hub.py` / `buddy_ui.py` — flag files that *don't* follow that split despite being large).
- **File size as a smell, not a rule**: files under ~800 lines are fine as-is. 800–2000 lines warrants a look at whether it's doing one job. Above ~2000 lines is a strong signal to identify natural seams (command groups, view classes, helper clusters) for a future split — flag but do not attempt the split yourself.
- **Cross-module coupling**: note where modules reach into each other's internals (importing private-looking helpers, duplicating another module's constants/logic instead of importing) versus going through a clear shared interface.

## 2. Functions & methods

- Long functions (roughly >60-80 lines) that mix input validation, business logic, and output/formatting in one body — flag as a decomposition candidate.
- Duplicated logic across functions/files (copy-pasted blocks with minor variable changes) — flag with all locations.
- Deep nesting (4+ levels of `if`/`for`) — flag as a guard-clause/early-return candidate.
- Functions with many positional parameters (5+) or "boolean flag soup" controlling behavior — flag.

## 3. Naming & typing

- PEP 8 naming: `snake_case` for functions/variables, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants. Flag inconsistencies.
- Type hints (PEP 484): flag public function signatures with no type hints, especially on cross-module functions (things other files import and call).
- Docstrings (PEP 257): flag missing docstrings on public/cross-module functions and classes — not on every private helper.

## 4. Python & async correctness (discord.py specific)

- **Blocking calls in async context**: synchronous I/O (`requests`, unbuffered file I/O, `time.sleep`, blocking gspread calls) inside `async def` command/listener bodies without `asyncio.to_thread`/executor offload — flag, this stalls the bot's event loop for everyone.
- **Bare `except:`** or overly broad `except Exception:` that swallows errors silently (no logging, no re-raise) — flag as an error-visibility gap.
- **Mutable default arguments** (`def f(x=[])`) — flag, classic Python bug source.
- **Global/module-level mutable state** used across cogs without a clear owner or lock — flag if it looks like a race-condition risk (bot commands run concurrently).
- **Secrets/config handling**: flag any hardcoded token/key/URL that should be an env var, and any secret logged or included in error messages sent to Discord.
- **Rate-limit / API-quota awareness**: flag Discord API or Google Sheets API calls in loops without batching/backoff, since both are rate-limited.

## 5. Duplication & dead code

- Flag copy-pasted helper functions that exist in multiple files instead of a shared module.
- Flag unused imports, unused functions/variables you can confirm have no callers (grep before flagging — don't guess).
- Flag commented-out code blocks left in place.

## 6. Severity levels to use in every section doc

- **Critical** — correctness/security bug, data-loss risk, or blocking-call-on-event-loop risk.
- **High** — real maintainability risk (god file, heavy duplication, no error visibility) likely to cause a future bug or slow down every change in that area.
- **Medium** — real but contained cleanup (long function, missing type hints on a cross-module function, naming inconsistency).
- **Low** — cosmetic/nice-to-have (docstring gaps, minor duplication, style inconsistency).
- **Positive** — call out what's already well-structured. Every section doc must include at least a short "what's already optimal" note — this is not just a fault-finding exercise.

## 7. Doc format for each section

Each `docs/audit/<section-slug>.md` must contain:
1. **Files covered** (with line counts)
2. **Summary** (2-4 sentences: overall health of this section)
3. **Findings**, grouped by severity, each with: category tag, file:line, description, concrete recommendation
4. **What's already optimal** (at least 2-3 real observations)
5. **Open questions** — anything ambiguous that needs Kevin's judgment call, not a clear-cut standards violation
