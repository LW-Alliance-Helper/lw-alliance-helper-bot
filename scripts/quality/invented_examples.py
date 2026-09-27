"""User-visible examples that don't draw from `examples.py` (#654).

Placeholders and worked examples used to borrow other alliances' tags,
warzone numbers and player names from real leagues and groupings. The fix put
one invented set in `examples.py`; this finds the class again, so the next
surface can't quietly reach for a real value.

It reads every call in the bot's own modules (a `TextInput`, a `Label`, an
`app_commands.describe`, ...) and looks at the string literals among its
keyword arguments. It flags:

- **a warzone number** in the copy of a call that is about a warzone or a
  server: a standalone 101 to 2308 (not part of a power figure like 26.8b, a
  comma-grouped number like 33,500,000, or a date), other than the owner's
  own warzone and the invented ones;
- **an alliance tag** anywhere in that copy: a bracketed `[TAG]`, or either
  side of a `TAG v TAG` matchup, other than the owner's own and the invented
  ones.

Copy built from `examples` (an f-string reading its names) leaves no literal
to flag, which is the point. Player names can't be told from words by
pattern, so they stay a review item.

    python scripts/quality/invented_examples.py

Exits 1 when it finds anything. `tests/unit/test_invented_examples.py` runs
it in CI.
"""

from __future__ import annotations

import ast
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

import examples  # noqa: E402

# A standalone 3-4 digit number. Not part of a bigger number: no digit,
# comma, point, slash or hyphen straight before it, and no digit or letter
# straight after it, nor a comma, point, slash or hyphen that runs on into
# another digit. So 33,500,000, 26.8b, 500m, 8/4 and 2026-08-04 don't count,
# and a list like `#101, #102` still does.
_NUMBER = re.compile(r"(?<![\d,./\-])(\d{3,4})(?![\dA-Za-z]|[,./\-]\d)")
_BRACKET_TAG = re.compile(r"\[([A-Za-z0-9]{2,5})\]")
_MATCHUP = re.compile(r"\b([A-Za-z0-9]{2,5}) v ([A-Za-z0-9]{2,5})\b")
_ABOUT_WARZONES = re.compile(r"warzone|server", re.IGNORECASE)

_ALLOWED_WARZONES = {
    examples.OWN_WARZONE,
    examples.WARZONE_PLACEHOLDER,
    *examples.WARZONES,
    *examples.WARZONE_LIST,
}
_ALLOWED_TAGS = {examples.OWN_TAG, *examples.TAGS, "Tag", "TAG", "tag"}


def _bot_modules():
    for name in sorted(os.listdir(ROOT)):
        if name.endswith(".py") and name != "examples.py":
            yield os.path.join(ROOT, name)


def _call_copy(call: ast.Call) -> list[tuple[int, str]]:
    """(line, text) for every string literal among a call's keyword arguments."""
    out = []
    for kw in call.keywords:
        for node in ast.walk(kw.value):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                out.append((node.lineno, node.value))
    return out


def problems_in(texts: list[str]) -> list[str]:
    """What's wrong with one call's copy, taken together."""
    found = []
    about_warzones = any(_ABOUT_WARZONES.search(t) for t in texts)
    for text in texts:
        if about_warzones:
            for match in _NUMBER.finditer(text):
                n = int(match.group(1))
                if 101 <= n <= 2308 and n not in _ALLOWED_WARZONES:
                    found.append(f"warzone number {n} in {text[:70]!r}")
        tags = [m.group(1) for m in _BRACKET_TAG.finditer(text)]
        for m in _MATCHUP.finditer(text):
            tags += [m.group(1), m.group(2)]
        for tag in tags:
            if not tag.isdigit() and tag not in _ALLOWED_TAGS:
                found.append(f"alliance tag {tag} in {text[:70]!r}")
    return found


def scan() -> list[str]:
    findings = []
    for path in _bot_modules():
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
        rel = os.path.relpath(path, ROOT)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            copy = _call_copy(node)
            if not copy:
                continue
            for problem in problems_in([t for _, t in copy]):
                findings.append(f"{rel}:{node.lineno}: {problem}")
    return findings


def main() -> int:
    findings = scan()
    for f in findings:
        print(f)
    print(f"{len(findings)} example(s) not drawn from examples.py")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
