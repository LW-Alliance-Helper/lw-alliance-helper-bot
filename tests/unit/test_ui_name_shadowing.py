"""
Guard against our `discord.ui` subclasses shadowing the library's own
attributes (#460).

`discord.ui.View` has a private, *synchronous* `_refresh(components)` that
discord.py calls itself whenever a tracked message is edited. Five of our
views had defined `async def _refresh(self, interaction)` on top of it, so
the library built a coroutine it never awaited and logged a RuntimeWarning
on essentially every button click:

    discord/ui/view.py:1118: RuntimeWarning: coroutine
    'WeeklyDraftView._refresh' was never awaited

Nothing user-facing broke, which is exactly why it went unnoticed across
five classes and several releases. A name that reads as obviously ours
(`_refresh`, `_rebuild`, `_sync`) is not necessarily free, and there is no
warning at definition time — the collision only shows up as odd runtime
behaviour in a library internal.

So rather than re-fixing this per class, this walks every `discord.ui`
subclass in the repo and fails on any method that collides with something
the base class already defines, except the handful we are *meant* to
override.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import discord

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

REPO_ROOT = Path(__file__).resolve().parents[2]

# The discord.ui bases we subclass, mapped to everything they already define.
_BASES = {
    "View": set(dir(discord.ui.View)),
    "Modal": set(dir(discord.ui.Modal)),
    "Select": set(dir(discord.ui.Select)),
    "Button": set(dir(discord.ui.Button)),
}

# Names discord.py documents as override points. Overriding these is the
# whole mechanism, not a collision.
_INTENDED_OVERRIDES = {
    "__init__",
    "__init_subclass__",
    "callback",
    "interaction_check",
    "on_error",
    "on_submit",
    "on_timeout",
}


def _ui_base(node: ast.ClassDef) -> str | None:
    """Which discord.ui base this class derives from, if any. Matches on the
    written name, so it sees `discord.ui.View` and a bare `View` alike but
    not our own intermediate classes — those inherit any collision from the
    base they were declared against, which is where it gets reported."""
    for base in node.bases:
        rendered = ast.unparse(base)
        for name in _BASES:
            if rendered == name or rendered.endswith(f"ui.{name}"):
                return name
    return None


def _collisions() -> list[str]:
    found: list[str] = []
    for path in sorted(REPO_ROOT.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            base = _ui_base(node)
            if base is None:
                continue
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if item.name in _INTENDED_OVERRIDES:
                    continue
                if item.name in _BASES[base]:
                    found.append(
                        f"{path.name}:{item.lineno} {node.name}({base}).{item.name} "
                        f"shadows discord.ui.{base}.{item.name}"
                    )
    return found


def test_no_ui_subclass_shadows_a_discord_attribute():
    found = _collisions()
    assert not found, (
        "These methods shadow an attribute discord.py already defines on the "
        "base class, which silently replaces library behaviour (#460). Rename "
        "them to something the library doesn't own:\n  " + "\n  ".join(found)
    )


def test_the_scan_would_actually_catch_the_bug_it_guards():
    """A clean scan is only reassuring if the scan works, and this one is a
    pile of AST matching that could quietly stop matching anything."""
    module = ast.parse(
        "import discord\n"
        "class Doomed(discord.ui.View):\n"
        "    async def _refresh(self, interaction):\n"
        "        pass\n"
        "    async def on_timeout(self):\n"
        "        pass\n"
    )
    klass = next(n for n in ast.walk(module) if isinstance(n, ast.ClassDef))
    assert _ui_base(klass) == "View"

    shadowing = [
        item.name
        for item in klass.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        and item.name not in _INTENDED_OVERRIDES
        and item.name in _BASES["View"]
    ]
    assert shadowing == ["_refresh"]
