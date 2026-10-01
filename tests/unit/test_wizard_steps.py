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


# ── Owned, expiring yes/no prompts (#679) ────────────────────────────────────

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

from messages import DENY_NOT_OWNER  # noqa: E402

HINT = "`/setup` → **🎂 Birthdays**"
OWNER = 111


def _yes_no():
    return wizard_steps.YesNoView(owner_id=OWNER, timeout_hint=HINT)


def _gate():
    return wizard_steps._KeepOrFlipYesNoGate(current_value=True, owner_id=OWNER, timeout_hint=HINT)


def _inter(user_id):
    inter = MagicMock()
    inter.user.id = user_id
    inter.response.send_message = AsyncMock()
    return inter


@pytest.mark.parametrize("make", [_yes_no, _gate], ids=["YesNoView", "_KeepOrFlipYesNoGate"])
async def test_only_the_officer_running_the_wizard_may_answer(make):
    view = make()
    stranger = _inter(222)
    assert await view.interaction_check(stranger) is False
    stranger.response.send_message.assert_awaited_once_with(DENY_NOT_OWNER, ephemeral=True)
    assert await view.interaction_check(_inter(OWNER)) is True


@pytest.mark.parametrize("make", [_yes_no, _gate], ids=["YesNoView", "_KeepOrFlipYesNoGate"])
async def test_a_timed_out_prompt_names_the_route_back(make):
    view = make()
    view.message = MagicMock()
    with patch("wizard_registry.expire_view_message", new=AsyncMock()) as expire:
        await view.on_timeout()
    expire.assert_awaited_once_with(view.message, command_hint=HINT)


def test_setup_route_is_the_route_hint_shape():
    assert wizard_steps.setup_route("🎂 Birthdays") == HINT


@pytest.mark.parametrize(
    "cmd_name, route",
    [
        ("setup → ⚔️ Desert Storm", "`/setup` → **⚔️ Desert Storm**"),
        ("setup → 🛡️ Canyon Storm", "`/setup` → **🛡️ Canyon Storm**"),
        ("desertstorm", "`/desertstorm`"),
    ],
)
def test_route_from_cmd_name(cmd_name, route):
    assert wizard_steps.route_from_cmd_name(cmd_name) == route


async def test_keep_or_change_times_out_in_the_wizards_own_words():
    channel = MagicMock()
    channel.send = AsyncMock()
    with patch("wizard_steps.wait_view_or_cancel", new=AsyncMock()) as wait:

        async def _timeout(view, _ev):
            view.cancelled = False

        wait.side_effect = _timeout
        got = await wizard_steps.ask_keep_or_change(
            channel,
            "Prompt",
            default="A",
            modal_title="T",
            modal_label="L",
            timeout_msg="⏰ Timed out. Run `/setup` → 🎂 Birthdays to start again.",
        )
    assert got is None
    channel.send.assert_awaited_with("⏰ Timed out. Run `/setup` → 🎂 Birthdays to start again.")
