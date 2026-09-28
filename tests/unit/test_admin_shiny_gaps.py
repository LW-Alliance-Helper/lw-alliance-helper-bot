"""`/admin shiny_gaps`: warzones missing from the Shiny Tasks table, by
warzone group (#653). The table is refreshed by hand; a group missing
warzones posts without them and nothing says so."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import bot  # noqa: E402,F401  (bot_admin binds to the bot at import)
import bot_admin  # noqa: E402


def test_runs_are_compressed():
    assert bot_admin._compress_runs([1, 2, 17, 90, 91, 92]) == "1–2, 17, 90–92"
    assert bot_admin._compress_runs([5]) == "5"
    assert bot_admin._compress_runs([]) == ""


def test_the_production_gap_is_reported_by_group():
    """The shape found while planning #604: 3 to 2285 stored, a few missing
    near the start, and 2286 to 2308 never imported. Warzones 1 and 2 are
    closed, so they don't count as missing."""
    present = set(range(3, 2286)) - {17, 90}
    report = bot_admin.shiny_gap_report(present)
    assert "`   1 – 164 ` 2 missing: 17, 90" in report
    assert "`2213 – 2308` 23 missing: 2286–2308" in report
    assert "` 677 – 804 `" not in report  # complete groups aren't listed
    assert len(report) < 1900


def test_a_complete_table_says_so():
    report = bot_admin.shiny_gap_report(set(range(3, 2309)))
    assert report == "✅ Every warzone from 1 to 2308 is in the Shiny Tasks table."
