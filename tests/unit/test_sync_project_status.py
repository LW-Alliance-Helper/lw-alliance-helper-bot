"""Tests for scripts/sync_project_status.py — specifically that an archived
project-board card is skipped rather than crashing the whole sync workflow.

Reopening an issue whose card was archived (which happens after it was closed)
makes the Status mutation fail with "The item is archived and cannot be
updated". That's a board-state quirk, not a workflow failure.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SCRIPT = os.path.join(_REPO_ROOT, "scripts", "sync_project_status.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("sync_project_status", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def mod():
    return _load_module()


def test_archived_item_returns_false(mod, monkeypatch):
    def fake_gql(query, variables=None):
        raise RuntimeError(
            "GraphQL errors: [{'type': 'VALIDATION', "
            "'message': 'The item is archived and cannot be updated'}]"
        )

    monkeypatch.setattr(mod, "gql", fake_gql)
    assert mod.set_status("item-1", "opt-1") is False


def test_successful_update_returns_true(mod, monkeypatch):
    monkeypatch.setattr(mod, "gql", lambda *a, **k: {"updateProjectV2ItemFieldValue": {}})
    assert mod.set_status("item-1", "opt-1") is True


def test_non_archived_error_still_raises(mod, monkeypatch):
    def fake_gql(query, variables=None):
        raise RuntimeError("GraphQL errors: [{'message': 'Something else broke'}]")

    monkeypatch.setattr(mod, "gql", fake_gql)
    with pytest.raises(RuntimeError):
        mod.set_status("item-1", "opt-1")


# ── Never backwards (#676) ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "current,target,expected",
    [
        ("Shipped", "In review", True),
        ("Ready for Release", "In review", True),
        ("In review", "In review", True),
        ("In review", "Ready for Release", False),
        ("In progress", "Shipped", False),
        ("Backlog", "In progress", False),
        ("Canceled", "In progress", False),
        (None, "In progress", False),
    ],
)
def test_is_backwards(mod, current, target, expected):
    assert mod.is_backwards(current, target) is expected


def _run(mod, monkeypatch, argv, current):
    calls = []
    monkeypatch.setattr(mod, "get_pr_for_commit", lambda sha: 673)
    monkeypatch.setattr(mod, "get_closing_issues", lambda pr: [{"id": "node-1", "number": 101}])
    monkeypatch.setattr(
        mod,
        "gql",
        lambda *a, **k: {"repository": {"issue": {"id": "node-1", "number": 101}}},
    )
    monkeypatch.setattr(mod, "get_project_item", lambda node: ("item-1", current))
    monkeypatch.setattr(mod, "set_status", lambda item, opt: calls.append((item, opt)) or True)
    monkeypatch.setattr("sys.argv", ["sync_project_status.py", *argv])
    mod.main()
    return calls


def test_dev_fast_forward_leaves_shipped_issues_alone(mod, monkeypatch, capsys):
    """The 1.9.2 case: the release merge commit lands on dev after main."""
    calls = _run(mod, monkeypatch, ["--commit", "abc123", "--status", "In review"], "Shipped")
    assert calls == []
    assert "already at Shipped" in capsys.readouterr().out


def test_forward_moves_still_happen(mod, monkeypatch):
    calls = _run(mod, monkeypatch, ["--commit", "abc123", "--status", "Shipped"], "In review")
    assert calls == [("item-1", mod.STATUS_OPTIONS["Shipped"])]


def test_manual_issue_correction_can_move_back(mod, monkeypatch):
    calls = _run(mod, monkeypatch, ["--issue", "101", "--status", "In review"], "Shipped")
    assert calls == [("item-1", mod.STATUS_OPTIONS["In review"])]
