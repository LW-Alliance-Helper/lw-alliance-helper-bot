"""Regression tests for `scripts/release_branch_nudge.py` (#628)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import release_branch_nudge  # noqa: E402


def test_matches_checkout_b():
    assert release_branch_nudge._release_branch("git checkout -b release/1.9.0 origin/dev") == (
        "release/1.9.0"
    )


def test_matches_switch_c_with_cd_prefix():
    command = "cd /repo && git switch -c release/2.0.0 origin/dev"
    assert release_branch_nudge._release_branch(command) == "release/2.0.0"


def test_matches_plain_branch_create():
    assert release_branch_nudge._release_branch("git branch release/1.10.0") == "release/1.10.0"


def test_no_match_for_unrelated_git_command():
    assert release_branch_nudge._release_branch("git status") is None


def test_no_match_for_feature_branch():
    assert release_branch_nudge._release_branch("git checkout -b some-feature") is None


def test_no_match_when_pattern_only_appears_inside_quoted_text():
    # A command that merely *mentions* the pattern (an echo'd test
    # fixture, a grep, a comment) is not a branch creation — this is
    # the false positive the hook hit against its own test command
    # during development.
    command = (
        'echo \'{"tool_input":{"command":"git checkout -b release/1.9.0 origin/dev"}}\''
        " | py scripts/release_branch_nudge.py"
    )
    assert release_branch_nudge._release_branch(command) is None
