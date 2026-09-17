"""wizard_steps.py is the home of the shared wizard pieces (#611 step 1).

Two things hold the move together and both are asserted here:

* `setup_cog` re-exports every name, so `from setup_cog import X` and
  `patch("setup_cog.X")` keep resolving for the wizards still in that file.
* The module knows no feature: it imports nothing from `setup_cog` or any
  `*_setup` / `*_hub` module, so importing it first (as `transfer_setup` and
  `alliance_duel_wizard` now can) never pulls the 9,600-line wizard file in
  behind it.
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

import wizard_steps  # noqa: E402

SHARED = [
    "WIZARD_STEP_TIMEOUT",
    "CreateRoleModal",
    "RoleSelectStep",
    "CreateChannelModal",
    "ChannelSelectStep",
    "ConfirmView",
    "TextInputModal",
    "ModalLaunchView",
    "ask_keep_or_change",
    "TIMEZONE_OPTIONS",
    "TIMEZONE_LABELS",
    "TimezoneSelectView",
    "ScheduleTypeView",
    "YesNoView",
    "_KeepOrFlipYesNoGate",
]


@pytest.mark.parametrize("name", SHARED)
def test_setup_cog_re_exports_the_same_object(name):
    import setup_cog

    assert getattr(setup_cog, name) is getattr(wizard_steps, name)


def test_wizard_steps_imports_no_feature_module():
    src = pathlib.Path(wizard_steps.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    feature_like = {
        m
        for m in imported
        if m == "setup_cog" or m.endswith("_setup") or m.endswith("_hub") or m.endswith("_cog")
    }
    assert not feature_like, feature_like


def test_timezone_labels_cover_every_option():
    assert set(wizard_steps.TIMEZONE_LABELS) == {tz for tz, _ in wizard_steps.TIMEZONE_OPTIONS}
