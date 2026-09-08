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
| `ast-grep` | `npx @ast-grep/cli` — no install needed | n/a, node is present |
| `radon` | `.venv/Scripts/python.exe -m pip install radon` | **No** — ask before installing |
| `vulture` | `.venv/Scripts/python.exe -m pip install vulture` | **No** — ask before installing |

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
