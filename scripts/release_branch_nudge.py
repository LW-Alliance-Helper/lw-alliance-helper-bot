#!/usr/bin/env python3
"""Nudge toward the release checklist when a release branch is created.

#628: cutting `release/1.9.0` this session ran through the parts of the
release process that are commands (version bump, CHANGELOG entry, PR,
CI green) and skipped the parts that are judgment calls (copy sign-off,
the Discord changelog post review, the announcement post decision, a
cross-repo dependency). CI (`release-changelog-check.yml`) now hard-gates
the machine-checkable half on the PR into main; this hook is the nudge
toward the judgment half, which only the `/release-check` skill can
actually assess. It fires at branch-creation time because that's the
only signal a hook has — nothing further down the release exists yet.

A `PostToolUse` hook on `Bash`, same shape as `check_changelog_slim.py`:
reads the tool-call payload from stdin, and when the command created a
`release/*` branch, exits 2 so the reminder reaches the model as
feedback (the git command already succeeded — there's nothing to
"fix", just something to do before the PR into main goes up).
"""

from __future__ import annotations

import json
import re
import sys

# `git checkout -b release/1.9.0`, `git switch -c release/1.9.0`,
# `git branch release/1.9.0`, each optionally preceded by `cd ... &&` and
# followed by a start-point (`origin/dev`, a SHA, ...).
#
# The boundary requirement (command start, or after `;`/`&&`/`|`) matters:
# without it, this matched inside its own test fixtures — a command whose
# text merely *contains* "git checkout -b release/1.9.0" (inside an echo'd
# JSON string, a grep, a comment) isn't a branch creation.
_CMD_BOUNDARY = r"(?:^|[;&|]+)\s*"
_RELEASE_BRANCH_CREATE = re.compile(
    _CMD_BOUNDARY + r"git\s+(?:checkout\s+-b|switch\s+-c|branch)\s+(release/\S+)",
    re.MULTILINE,
)


def _release_branch(command: str) -> str | None:
    match = _RELEASE_BRANCH_CREATE.search(command)
    return match.group(1) if match else None


def main() -> int:
    if sys.stdin.isatty():
        return 0

    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    command = (data.get("tool_input") or {}).get("command") or ""
    branch = _release_branch(command)
    if branch is None:
        return 0

    print(
        f"[RELEASE-CHECK] Created {branch}. Before opening the PR into main, "
        "run /release-check - it covers what CI can't: copy sign-off actually "
        "happened (not just pasted into chat), the Discord #changelog post "
        "bullets match what shipped, whether a release-unveiling announcement "
        "post makes sense for this release, and any cross-repo dependency "
        "(usually the website repo) is identified and resolved or explicitly "
        "deferred with Kevin's OK.",
        file=sys.stderr,
    )
    # Exit 2 is the Claude Code hook convention for "send stderr back to
    # the model as feedback" — the branch creation itself isn't wrong,
    # so there's nothing to block, only something to surface.
    return 2


if __name__ == "__main__":
    sys.exit(main())
