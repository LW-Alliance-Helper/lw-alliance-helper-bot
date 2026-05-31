#!/usr/bin/env python3
"""Block paragraph-length bullets from landing in CHANGELOG.md.

The slim-changelog rule (memory: `feedback_changelog_brevity.md`,
"REPEATED OFFENSE" — corrected 4+ times) keeps slipping despite the
memory note. This script is the mechanical backstop: a Claude Code
PostToolUse hook on `Write|Edit` runs it after any change to
`CHANGELOG.md`, and the model gets exit-code-2 feedback when any
top-level bullet exceeds the displayed-character limit.

Safe to invoke manually too:

    py scripts/check_changelog_slim.py                  # checks ./CHANGELOG.md
    py scripts/check_changelog_slim.py path/to/CHANGELOG.md

Counts displayed chars only — markdown link URLs strip to their visible
text (`[#34](https://...)` counts as `#34`, not the full URL).
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# 200 displayed chars is roughly the longest the slimmed 1.1.x entries
# run (a one-sentence "Numeric survey question type moved from Premium
# to Free; min/max bounds on numeric questions remain the Premium
# differentiator (#64)." is ~130 chars). Anything over 200 is paragraph
# territory and almost certainly contains rationale that belongs in the
# commit message or PR body, not the changelog.
LIMIT = 200

# `[visible](url)` → `visible`. Trailing-greedy because URLs never
# contain `)` and link texts shouldn't either.
_LINK = re.compile(r"\[([^]]+)\]\(([^)]+)\)")


def _baseline_lines(path: Path) -> set[str]:
    """Lines that already exist in `path` as of `git HEAD`.

    The hook should only flag bullets the user is *adding* in this
    edit — historical wordy entries pre-date the slim-rule enforcement
    and would otherwise drown out new violations. Returns an empty set
    when git isn't reachable or the file is new (every line is "new"
    by definition, which is also fine).
    """
    try:
        # `git show HEAD:<path>` only accepts paths relative to the repo
        # root. The hook passes absolute Windows paths which git rejects
        # with "exists on disk, but not in 'HEAD'", so resolve against
        # the repo toplevel before invoking show.
        toplevel = Path(
            subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                capture_output=True,
                check=True,
                text=True,
            ).stdout.strip()
        ).resolve()
        rel = path.resolve().relative_to(toplevel).as_posix()
        result = subprocess.run(
            ["git", "show", f"HEAD:{rel}"],
            capture_output=True,
            check=True,
        )
        return set(result.stdout.decode("utf-8", errors="replace").splitlines())
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return set()


def main(path: Path) -> int:
    if not path.exists():
        return 0

    baseline = _baseline_lines(path)
    violations: list[tuple[int, int, str]] = []

    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        # Top-level bullets only — the slim rule applies to changelog
        # entries, not to nested sub-bullets or prose.
        if not line.startswith("- "):
            continue
        # Skip lines that already existed in HEAD — historical
        # violations stay until they're individually slimmed in a
        # cleanup pass.
        if line in baseline:
            continue
        bullet = line[2:]
        visible = _LINK.sub(r"\1", bullet)
        if len(visible) > LIMIT:
            violations.append((line_no, len(visible), visible[:120]))

    if not violations:
        return 0

    print("[CHANGELOG-SLIM] Violations:", file=sys.stderr)
    print("", file=sys.stderr)
    for line_no, chars, preview in violations:
        print(f"  L{line_no}: {chars} chars (limit: {LIMIT})", file=sys.stderr)
        print(f"     {preview}...", file=sys.stderr)
        print("", file=sys.stderr)
    print(
        f"Found {len(violations)} verbose CHANGELOG bullet(s). "
        "Each entry must be ONE SENTENCE describing what changed.",
        file=sys.stderr,
    )
    print("", file=sys.stderr)
    print("  - Rationale (the WHY) goes in the commit message or PR description.", file=sys.stderr)
    print("  - Pattern to copy: 1.2.0 / 1.3.0 in CHANGELOG.md (post-slim).", file=sys.stderr)
    print(
        "  - Memory note: feedback_changelog_brevity.md (4+ corrections so far).", file=sys.stderr
    )

    # Exit code 2 is the Claude Code hook convention for "block + send
    # stderr back to the model as feedback".
    return 2


def _resolve_target() -> Path | None:
    """Pick the CHANGELOG path based on how this script was invoked.

    * Explicit CLI arg → use it (manual invocation, tests).
    * Stdin is a pipe (hook context) → parse the Claude Code tool
      payload JSON and pull out `tool_input.file_path`. Return None
      when the edit wasn't to `CHANGELOG.md` so the hook silently
      skips non-changelog edits.
    * Otherwise → default to `./CHANGELOG.md`.
    """
    if len(sys.argv) > 1:
        return Path(sys.argv[1])

    if not sys.stdin.isatty():
        import json

        try:
            data = json.load(sys.stdin)
        except (json.JSONDecodeError, ValueError):
            return None
        file_path = (data.get("tool_input") or {}).get("file_path") or (
            data.get("tool_response") or {}
        ).get("filePath")
        if not file_path or Path(file_path).name != "CHANGELOG.md":
            return None
        return Path(file_path)

    return Path("CHANGELOG.md")


if __name__ == "__main__":
    target = _resolve_target()
    if target is None:
        sys.exit(0)
    sys.exit(main(target))
