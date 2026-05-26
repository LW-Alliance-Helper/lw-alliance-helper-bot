"""Regression guard for the success-ack sentence-form style rule (#267 D15).

Success acks follow the sentence-form rule documented at the bottom
of messages.py:

  ✅ ACK:   "✅ Updated **{name}**."
  ❌ AVOID: "✅ Updated: **{name}**"

This test greps the codebase for the verb-colon-bold antipattern and
fails CI if any callsite regresses. Colon usage after a complete verb-
object phrase (e.g. "✅ Added preset(s): {summary}") is fine — the
pattern only catches "Verb: **" where the colon separates verb from
object directly.

Companion to tests/unit/test_messages_constants.py and the style-rule
comment in messages.py.
"""
import pathlib
import re

# Match: emoji prefix + single past-tense verb + colon + space + bold-open.
# Examples this catches:
#   ✅ Updated: **{label}**
#   🗑️ Removed: **{name}**
#   ✅ Added: **X**
#
# Examples this does NOT catch (intentionally OK):
#   ✅ Added preset(s): {summary}       (verb+object, colon introduces list)
#   ✅ Saved attendance for ...: ...    (verb+object phrase, colon introduces details)
#   ✅ Created and selected new role: **X**  (multi-word verb+object)
#
# We restrict to single-word verbs in the antipattern set; multi-word
# verb+object phrases that legitimately use colon to introduce
# details are not caught.
_VERBS = "Saved|Added|Updated|Created|Removed|Deleted|Moved|Posted|Sent|Synced|Cleared|Recorded|Paired"
ANTIPATTERN = re.compile(
    r"[✅🗑️↔️💾📬]\s+(?:" + _VERBS + r"):\s+\*\*",
)

# Source files exempt from the check:
#   * messages.py — defines templates that may include the example
#     antipattern in docstrings/comments showing what NOT to do.
#   * tests/ — tests reference the antipattern in regression-guard
#     fixtures.
_EXEMPT_PATHS = {"messages.py"}


def _iter_source_files(root: pathlib.Path):
    """Yield every .py file under root that should be subject to the
    style rule. Skips tests/, .venv/, .git/, exempt paths."""
    for py in root.rglob("*.py"):
        if any(part in {".venv", ".git", "__pycache__", "tests"} for part in py.parts):
            continue
        if py.name in _EXEMPT_PATHS:
            continue
        yield py


def test_no_verb_colon_ack_form():
    """Catch '✅ Verb: **object**' regressions. Sentence form is the rule."""
    root = pathlib.Path(__file__).resolve().parents[2]
    offenders = []
    for py in _iter_source_files(root):
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if ANTIPATTERN.search(line):
                offenders.append(f"{py.name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Verb-colon ack form found. Use sentence form instead — see the "
        "style rule at the bottom of messages.py.\n\n" + "\n".join(offenders)
    )
