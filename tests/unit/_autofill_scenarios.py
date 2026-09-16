"""Golden scenarios for the auto-fill characterization tests.

`snapshot(session, summary)` flattens everything auto-fill writes; the
scenarios build sessions on the same factory the rest of the roster
builder tests use. `python tests/unit/_autofill_scenarios.py` prints the
snapshots as Python literals, which is how the expected values in
`test_storm_roster_autofill.py` were produced against the pre-refactor
function.
"""

import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import storm_strategy as ss
import storm_member_rules as smr


def member(key, name, power, *, discord_id=None):
    return {
        "key": key,
        "name": name,
        "discord_id": key if discord_id is None else discord_id,
        "power": power,
        "not_on_discord": False,
    }


def twelve():
    powers = [412, 380, 355, 320, 298, 270, 244, 219, 205, 190, 160, None]
    names = [
        "Alice",
        "Bravo",
        "Carol",
        "Dan",
        "Erin",
        "Frank",
        "Gia",
        "Hal",
        "Ivy",
        "Jon",
        "Kim",
        "Lee",
    ]
    return {
        str(1001 + i): member(str(1001 + i), names[i], None if p is None else p * 1_000_000)
        for i, p in enumerate(powers)
    }


def flat_zones():
    return [
        ss.ZoneRow(
            zone="Power Tower",
            max_players=3,
            min_power_a=300_000_000,
            min_power_b=180_000_000,
            priority=1,
        ),
        ss.ZoneRow(
            zone="Nuclear Silo",
            max_players=3,
            min_power_a=250_000_000,
            min_power_b=150_000_000,
            priority=2,
        ),
        ss.ZoneRow(
            zone="Oil Refinery I",
            max_players=2,
            min_power_a=200_000_000,
            min_power_b=100_000_000,
            priority=3,
        ),
        ss.ZoneRow(
            zone="Oil Refinery II",
            max_players=2,
            min_power_a=200_000_000,
            min_power_b=100_000_000,
            priority=3,
        ),
    ]


def phased_zones():
    return [
        ss.ZoneRow(
            zone="Info Center",
            max_players=0,
            max_phase1=2,
            max_phase2=1,
            min_power_a=100_000_000,
            min_power_b=50_000_000,
        ),
        ss.ZoneRow(
            zone="Arsenal", max_players=0, max_phase1=0, max_phase2=3, min_power_a=0, min_power_b=0
        ),
        ss.ZoneRow(
            zone="Depot",
            max_players=0,
            max_phase1=2,
            max_phase2=2,
            min_power_a=350_000_000,
            min_power_b=0,
        ),
    ]


def make_session(
    srb,
    *,
    zones,
    members,
    sub_mode="pool",
    per_member_rules=(),
    power_band_rules=(),
    uses_phases=False,
    event_date=None,
    team="A",
):
    preset = ss.PresetBuffer(name="Golden", event_type="DS", zones=zones, uses_phases=uses_phases)
    s = srb.RosterBuilderSession(
        guild_id=1,
        user_id=42,
        event_type="DS",
        team=team,
        preset=preset,
        members=members,
        per_member_rules=list(per_member_rules),
        power_band_rules=list(power_band_rules),
        sub_mode=sub_mode,
    )
    if event_date is not None:
        s.event_date = event_date
    return s


def snapshot(session, summary):
    out = {
        "assignments": {
            p: {z: list(m) for z, m in session.assignments_for_phase(p).items()}
            for p in session.iter_phases()
        },
        "subs": list(session.subs),
        "paired": {p: dict(session.paired_subs_for_phase(p)) for p in session.iter_phases()},
        "overrides": {
            p: sorted(session.below_floor_overrides_for_phase(p)) for p in session.iter_phases()
        },
        "selected_phase": session.selected_phase,
        "summary": summary,
        "summary_is_stored": session.auto_fill_summary is summary,
    }
    return out


def rule(subject, zone):
    return smr.Rule(rule_type="per_member", subject=subject, sub_type="zone", value=zone)


def scenarios(srb):
    """name -> (session, kwargs for auto-fill)."""
    yield (
        "pool_balanced",
        make_session(
            srb,
            zones=flat_zones(),
            members=twelve(),
            per_member_rules=[
                rule("Kim", "Power Tower"),
                rule("Nobody", "Power Tower"),
                rule("Alice", "Moon Base"),
            ],
        ),
        {},
    )
    yield (
        "paired_balanced",
        make_session(srb, zones=flat_zones(), members=twelve(), sub_mode="paired"),
        {},
    )
    yield (
        "paired_greedy",
        make_session(srb, zones=flat_zones(), members=twelve(), sub_mode="paired"),
        {"strategy": "priority_greedy"},
    )
    yield (
        "unknown_strategy_is_balanced",
        make_session(srb, zones=flat_zones(), members=twelve()),
        {"strategy": "chaos"},
    )
    yield (
        "phased_pool",
        make_session(srb, zones=phased_zones(), members=twelve(), uses_phases=True),
        {},
    )
    yield (
        "phased_paired",
        make_session(srb, zones=phased_zones(), members=twelve(), sub_mode="paired"),
        {},
    )
    yield (
        "plan_aware",
        make_session(
            srb,
            zones=flat_zones(),
            members=twelve(),
            sub_mode="paired",
            per_member_rules=[rule("Frank", "Nuclear Silo")],
        ),
        {
            "plan": {
                "primaries": ["1001", "1003", "1005", "1007", "9999"],
                "subs": ["1002", "1006", "1010"],
            }
        },
    )
    yield "team_b_floors", make_session(srb, zones=flat_zones(), members=twelve(), team="B"), {}
    yield (
        "empty_plan_is_ignored",
        make_session(srb, zones=flat_zones(), members=twelve()),
        {"plan": {"primaries": [], "subs": []}},
    )
    yield (
        "pin_conflicts",
        make_session(
            srb,
            zones=[ss.ZoneRow(zone="Tiny", max_players=1, min_power_a=0, min_power_b=0)],
            members=twelve(),
            per_member_rules=[rule("Alice", "Tiny"), rule("Bravo", "Tiny"), rule("Alice", "Tiny")],
        ),
        {},
    )


if __name__ == "__main__":
    import pprint
    import storm_roster_builder as srb

    for name, session, kwargs in scenarios(srb):
        session.selected_phase = 2 if session.is_phase_aware else 1
        summary = srb._auto_fill_session(session, **kwargs)
        print(f"# ── {name}")
        print(f"EXPECTED[{name!r}] = ", end="")
        pprint.pprint(snapshot(session, summary), width=100, sort_dicts=False)
        print()
