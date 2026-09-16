"""
Characterization tests for the roster builder's auto-fill
(`storm_roster_builder._auto_fill_session`, moved to
`storm_roster_autofill.auto_fill_session` in the #589 step 11 refactor).

The existing auto-fill tests pin one behaviour each. These pin the whole
end state of ten scenarios at once: every phase's assignments, the sub
pool, the pairings, the below-floor overrides, the officer's phase
cursor and the full summary dict, exactly as the pre-refactor function
produced them. The scenarios live in `_autofill_scenarios.py`, which
also regenerates these literals; a diff here after a change is the
change, spelled out.
"""

import pytest

import storm_roster_builder as srb
from tests.unit._autofill_scenarios import scenarios, snapshot

EXPECTED = {}

# ── pool_balanced
EXPECTED["pool_balanced"] = {
    "assignments": {
        1: {
            "Power Tower": ["1011", "1001", "1005"],
            "Nuclear Silo": ["1002", "1006", "1009"],
            "Oil Refinery I": ["1003", "1007"],
            "Oil Refinery II": ["1004", "1008"],
        }
    },
    "subs": ["1010"],
    "paired": {1: {}},
    "overrides": {1: ["1005", "1009"]},
    "selected_phase": 1,
    "summary": {
        "per_member_rules_applied": 1,
        "power_band_rules_applied": 0,
        "auto_filled_by_power": 9,
        "auto_paired_subs": [],
        "gaps": ["Lee"],
        "conflicts": ["per_member rule names unknown zone: Moon Base"],
        "starters_short": 9,
        "unpaired_subs_below_floor": [],
    },
    "summary_is_stored": True,
}

# ── paired_balanced
EXPECTED["paired_balanced"] = {
    "assignments": {
        1: {
            "Power Tower": ["1001", "1005", "1009"],
            "Nuclear Silo": ["1002", "1006", "1010"],
            "Oil Refinery I": ["1003", "1007"],
            "Oil Refinery II": ["1004", "1008"],
        }
    },
    "subs": ["1011"],
    "paired": {1: {}},
    "overrides": {1: ["1005", "1009", "1010"]},
    "selected_phase": 1,
    "summary": {
        "per_member_rules_applied": 0,
        "power_band_rules_applied": 0,
        "auto_filled_by_power": 10,
        "auto_paired_subs": [],
        "gaps": ["Lee"],
        "conflicts": [],
        "starters_short": 9,
        "unpaired_subs_below_floor": [{"name": "Kim", "power": 160000000, "min_floor": 200000000}],
    },
    "summary_is_stored": True,
}

# ── paired_greedy
EXPECTED["paired_greedy"] = {
    "assignments": {
        1: {
            "Power Tower": ["1001", "1002", "1003"],
            "Nuclear Silo": ["1004", "1005", "1006"],
            "Oil Refinery I": ["1007", "1010"],
            "Oil Refinery II": ["1008", "1009"],
        }
    },
    "subs": ["1011"],
    "paired": {1: {}},
    "overrides": {1: ["1010"]},
    "selected_phase": 1,
    "summary": {
        "per_member_rules_applied": 0,
        "power_band_rules_applied": 0,
        "auto_filled_by_power": 10,
        "auto_paired_subs": [],
        "gaps": ["Lee"],
        "conflicts": [],
        "starters_short": 9,
        "unpaired_subs_below_floor": [{"name": "Kim", "power": 160000000, "min_floor": 200000000}],
    },
    "summary_is_stored": True,
}

# ── unknown_strategy_is_balanced
EXPECTED["unknown_strategy_is_balanced"] = {
    "assignments": {
        1: {
            "Power Tower": ["1001", "1005", "1009"],
            "Nuclear Silo": ["1002", "1006", "1010"],
            "Oil Refinery I": ["1003", "1007"],
            "Oil Refinery II": ["1004", "1008"],
        }
    },
    "subs": ["1011"],
    "paired": {1: {}},
    "overrides": {1: ["1005", "1009", "1010"]},
    "selected_phase": 1,
    "summary": {
        "per_member_rules_applied": 0,
        "power_band_rules_applied": 0,
        "auto_filled_by_power": 10,
        "auto_paired_subs": [],
        "gaps": ["Lee"],
        "conflicts": [],
        "starters_short": 9,
        "unpaired_subs_below_floor": [],
    },
    "summary_is_stored": True,
}

# ── phased_pool
EXPECTED["phased_pool"] = {
    "assignments": {
        1: {"Info Center": ["1001", "1003"], "Arsenal": [], "Depot": ["1002", "1004"]},
        2: {
            "Info Center": ["1001"],
            "Arsenal": ["1002", "1004", "1006"],
            "Depot": ["1003", "1005"],
        },
    },
    "subs": ["1007", "1008", "1009", "1010", "1011"],
    "paired": {1: {}, 2: {}},
    "overrides": {1: ["1004"], 2: ["1005"]},
    "selected_phase": 2,
    "summary": {
        "per_member_rules_applied": 0,
        "power_band_rules_applied": 0,
        "auto_filled_by_power": 10,
        "auto_paired_subs": [],
        "gaps": ["Lee"],
        "conflicts": [],
        "starters_short": 9,
        "unpaired_subs_below_floor": [],
    },
    "summary_is_stored": True,
}

# ── phased_paired
EXPECTED["phased_paired"] = {
    "assignments": {1: {"Info Center": [], "Arsenal": [], "Depot": []}},
    "subs": [
        "1001",
        "1002",
        "1003",
        "1004",
        "1005",
        "1006",
        "1007",
        "1008",
        "1009",
        "1010",
        "1011",
    ],
    "paired": {1: {}},
    "overrides": {1: []},
    "selected_phase": 1,
    "summary": {
        "per_member_rules_applied": 0,
        "power_band_rules_applied": 0,
        "auto_filled_by_power": 0,
        "auto_paired_subs": [],
        "gaps": ["Lee"],
        "conflicts": [],
        "starters_short": 9,
        "unpaired_subs_below_floor": [],
    },
    "summary_is_stored": True,
}

# ── plan_aware
EXPECTED["plan_aware"] = {
    "assignments": {
        1: {
            "Power Tower": ["1001"],
            "Nuclear Silo": ["1006", "1003"],
            "Oil Refinery I": ["1005"],
            "Oil Refinery II": ["1007"],
        }
    },
    "subs": ["1010"],
    "paired": {1: {"1007": "1002"}},
    "overrides": {1: []},
    "selected_phase": 1,
    "summary": {
        "per_member_rules_applied": 1,
        "power_band_rules_applied": 0,
        "auto_filled_by_power": 4,
        "auto_paired_subs": ["Gia ↔ Bravo"],
        "gaps": ["Lee"],
        "conflicts": [
            "Frank is pinned by a per-member rule but the saved team plan marks "
            "them as a sub — pin wins.",
            "plan key 9999 missing from pool (vote changed or member dropped from roster)",
        ],
        "starters_short": 15,
        "unpaired_subs_below_floor": [{"name": "Jon", "power": 190000000, "min_floor": 200000000}],
    },
    "summary_is_stored": True,
}

# ── team_b_floors
EXPECTED["team_b_floors"] = {
    "assignments": {
        1: {
            "Power Tower": ["1001", "1005", "1009"],
            "Nuclear Silo": ["1002", "1006", "1010"],
            "Oil Refinery I": ["1003", "1007"],
            "Oil Refinery II": ["1004", "1008"],
        }
    },
    "subs": ["1011"],
    "paired": {1: {}},
    "overrides": {1: []},
    "selected_phase": 1,
    "summary": {
        "per_member_rules_applied": 0,
        "power_band_rules_applied": 0,
        "auto_filled_by_power": 10,
        "auto_paired_subs": [],
        "gaps": ["Lee"],
        "conflicts": [],
        "starters_short": 9,
        "unpaired_subs_below_floor": [],
    },
    "summary_is_stored": True,
}

# ── empty_plan_is_ignored
EXPECTED["empty_plan_is_ignored"] = {
    "assignments": {
        1: {
            "Power Tower": ["1001", "1005", "1009"],
            "Nuclear Silo": ["1002", "1006", "1010"],
            "Oil Refinery I": ["1003", "1007"],
            "Oil Refinery II": ["1004", "1008"],
        }
    },
    "subs": ["1011"],
    "paired": {1: {}},
    "overrides": {1: ["1005", "1009", "1010"]},
    "selected_phase": 1,
    "summary": {
        "per_member_rules_applied": 0,
        "power_band_rules_applied": 0,
        "auto_filled_by_power": 10,
        "auto_paired_subs": [],
        "gaps": ["Lee"],
        "conflicts": [],
        "starters_short": 9,
        "unpaired_subs_below_floor": [],
    },
    "summary_is_stored": True,
}

# ── pin_conflicts
EXPECTED["pin_conflicts"] = {
    "assignments": {1: {"Tiny": ["1001"]}},
    "subs": ["1002", "1003", "1004", "1005", "1006", "1007", "1008", "1009", "1010", "1011"],
    "paired": {1: {}},
    "overrides": {1: []},
    "selected_phase": 1,
    "summary": {
        "per_member_rules_applied": 1,
        "power_band_rules_applied": 0,
        "auto_filled_by_power": 0,
        "auto_paired_subs": [],
        "gaps": ["Lee"],
        "conflicts": ["Tiny full when pinning Bravo", "Tiny full when pinning Alice"],
        "starters_short": 9,
        "unpaired_subs_below_floor": [],
    },
    "summary_is_stored": True,
}


@pytest.mark.parametrize("name", list(EXPECTED))
def test_golden_end_state(name):
    session, kwargs = next((s, k) for n, s, k in scenarios(srb) if n == name)
    # The officer's phase cursor is restored whatever it was.
    session.selected_phase = 2 if session.is_phase_aware else 1
    summary = srb._auto_fill_session(session, **kwargs)
    assert snapshot(session, summary) == EXPECTED[name]


def test_every_scenario_has_a_golden():
    assert [n for n, _, _ in scenarios(srb)] == list(EXPECTED)


def test_rerun_is_from_scratch():
    """A second click redoes the fill from nothing, not on top of the first."""
    session, kwargs = next((s, k) for n, s, k in scenarios(srb) if n == "paired_balanced")
    first = snapshot(session, srb._auto_fill_session(session, **kwargs))
    session.subs.append("junk")
    session.assignments_for_phase(1)["Power Tower"].append("junk2")
    second = snapshot(session, srb._auto_fill_session(session, **kwargs))
    assert second == first
