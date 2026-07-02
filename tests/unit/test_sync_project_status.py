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
