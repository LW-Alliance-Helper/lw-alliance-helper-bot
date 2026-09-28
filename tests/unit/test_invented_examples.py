"""Examples in shipped copy use invented values (#654).

Runs `scripts/quality/invented_examples.py`: a placeholder or worked example
with a real-looking warzone number or another alliance's tag, not drawn from
`examples.py`, fails CI instead of shipping.
"""

from __future__ import annotations

import importlib.util
import os

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts",
    "quality",
    "invented_examples.py",
)


@pytest.fixture(scope="module")
def check():
    spec = importlib.util.spec_from_file_location("invented_examples", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_no_shipped_example_borrows_a_real_value(check):
    findings = check.scan()
    assert not findings, "Use examples.py instead:\n" + "\n".join(findings)


@pytest.mark.parametrize(
    "texts, flagged",
    [
        (["Their server", "e.g. 1042"], True),
        (["The participating warzones", "#773, #800, ..."], True),
        (["Name, Warzone, Rank", "[OGV]Kestrel, 738, 1, 325.8M, 33,500,000\nWren, 744, 25"], True),
        (["One match per line", "e.g. OGV v QRS: OGV 7-6"], True),
        (["Their server", "e.g. 999"], False),
        (["Name, Warzone, Rank", "[OGV]Kestrel, 738, 1, 325.8M, 33,500,000\nWren, 999, 25"], False),
        (["Squad 1 power", "e.g. 325.8M, or 2026-08-04"], False),
        (["Drone level", "e.g. 150"], False),  # not about a warzone
        (["[tag] [warzone] [power] [gift level] [members]", "e.g.: Glo 999 26.8b 25 100"], False),
        (["The bracket", "[Nwx] 999 26.8b"], True),
    ],
)
def test_what_counts(check, texts, flagged):
    assert bool(check.problems_in(texts)) is flagged
