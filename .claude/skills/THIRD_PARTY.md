# Third-party origins for vendored skills

Five skills in this directory are adapted from
[laurigates/claude-plugins](https://github.com/laurigates/claude-plugins),
`code-quality-plugin`, and are used under the MIT licence reproduced below.

| Skill here | Upstream skill | What changed |
|---|---|---|
| `dry-consolidation` | `code-quality-plugin/skills/dry-consolidation` | Rewritten for Python and this repo's flat module layout. JS/TS classification (components/hooks/types) replaced with real destinations. Guard rails added for `CLAUDE.md` § Patterns to reuse and the F401/F811 exemptions. **Upstream's "remove unused imports" step removed.** Verification commands replaced with this repo's pytest and pre-commit invocations. |
| `code-complexity` | `code-quality-plugin/skills/code-complexity` | Trimmed to the Python/`radon` path. Function-length thresholds raised from 25/50 to 40/80 because Discord handlers are legitimately long. Known baseline of the five largest modules added so runs don't re-report it. |
| `code-dead-code` | `code-quality-plugin/skills/code-dead-code` | Trimmed to the Python/`vulture` path. **Upstream's `--fix` auto-removal deliberately not carried over.** Three repo-specific false-positive families documented (framework dispatch, re-exports, the config late-binding hook), plus a mandatory whitelist step and a three-check proof requirement before anything is called dead. |
| `ast-grep-search` | `code-quality-plugin/skills/ast-grep-search` | Trimmed to Python; JS/Rust/Go examples dropped. discord.py patterns added. Targeted at the blocking-I/O-in-async gap that `ruff.toml` documents as needing a human grep. `-U` warnings added for the shared-checkout hazard. |
| `bulk-sweep-classify` | `code-quality-plugin/skills/bulk-sweep-classify` | Four categories and the verification trap kept close to upstream — they are method, not tooling. Cross-references rewritten. Repo hazards added: parallel sessions sharing the checkout, `notes/` being a separate gitignored repo, `messages.py` copy needing sign-off, and the changelog belonging to the release branch. |

Upstream's cross-references to skills we did not vendor (`/code:refactor`,
`/code:antipatterns`, `/lint:check`, `/configure:dead-code`,
`/test:run`, `tools-plugin:rg-code-search`,
`agent-patterns-plugin:parallel-agent-dispatch`) were removed rather than left
dangling, and repointed at the built-in `/code-review` and `/simplify` and at
each other.

Upstream's non-standard frontmatter keys (`args`, `argument-hint`, `model`,
`created`, `modified`, `reviewed`, `agent`, `context`, `user-invocable`) were
dropped in favour of this repo's house style: `name` and `description` only.

## Tooling these skills call

None of it is a runtime dependency. **Nothing here belongs in
`requirements.txt`** — Railway installs from that file.

| Tool | How it runs | Installed? |
|---|---|---|
| `jscpd` | `npx jscpd` — no install needed | n/a, node is present |
| `ast-grep` | `npx --yes --package @ast-grep/cli ast-grep` — no install needed | n/a, node is present |
| `radon` | `$PY -m radon` | **Yes** — 6.0.1, in the shared venv |
| `vulture` | `$PY -m vulture` | **Yes** — 2.16, in the shared venv |

`$PY` is the main checkout's interpreter by absolute path
(`.../lw-alliance-helper-bot/.venv/Scripts/python.exe`) — worktrees have none of
their own.

**The `ast-grep` invocation is not the obvious one.** `npx @ast-grep/cli` fails
with "could not determine executable to run", because the package and the binary
have different names. And a multi-line pattern passed inline to `-p` is silently
mangled by the shell — ast-grep stops honouring `--lang`, falls back to text
matching, returns hits from `README.md`, and still exits 0. Multi-line patterns
go in a YAML rule file run with `scan -r`. Both are documented in
`ast-grep-search`; both were found by smoke-testing, not by reading upstream.

---

## MIT License

Copyright (c) 2026 Lauri Gates

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
