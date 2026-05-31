"""Regression tests for `scripts/check_changelog_slim.py`.

Guards #250: the baseline lookup used `path.as_posix()` (absolute path)
against `git show HEAD:<path>`, which git rejects — collapsing the
baseline to an empty set and flagging every historical bullet as new.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import check_changelog_slim  # noqa: E402


def test_baseline_lookup_resolves_absolute_path():
    # Absolute path is what the Claude Code hook payload supplies.
    absolute = ROOT / "CHANGELOG.md"
    assert absolute.is_absolute()

    baseline = check_changelog_slim._baseline_lines(absolute)

    # CHANGELOG.md exists at HEAD with many historical bullets — a
    # populated baseline proves git show resolved the path correctly.
    assert baseline, "absolute path collapsed baseline to empty set (regression of #250)"
    assert any(line.startswith("- ") for line in baseline)


def test_unchanged_changelog_passes_main():
    # The exit-0 contract on an unmodified CHANGELOG: every bullet
    # already exists in HEAD, so the new-only filter drops them all.
    assert check_changelog_slim.main(ROOT / "CHANGELOG.md") == 0
