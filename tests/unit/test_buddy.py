"""Unit tests for the pure Profession Buddy System logic (#289).

Covers the stability-first pairing algorithm and the change-notification copy.
No Sheets / Discord — these are pure-function tests like test_train_rotation.py.
"""

import buddy
from buddy import Member, Pair, assign_buddies, compose_change_notification


# ── builders / helpers ────────────────────────────────────────────────────────


def W(name, did=None, power=0.0):
    return Member(name=name, discord_id=did or "", profession=buddy.WAR_LEADER, power=power)


def E(name, did=None, power=0.0):
    return Member(name=name, discord_id=did or "", profession=buddy.ENGINEER, power=power)


def _key(did, name):
    return (did or "").strip() or name.strip().lower()


def pset(result):
    """Pairs as an order-independent set of (wl_key, eng_key)."""
    return {
        (_key(p.wl_discord_id, p.war_leader), _key(p.eng_discord_id, p.engineer))
        for p in result.pairs
    }


def names(members):
    return sorted(m.name for m in members)


# ── stability ─────────────────────────────────────────────────────────────────


def test_deterministic_same_input_same_output():
    members = [W("Wanda", "1"), W("Walt", "2"), E("Eve", "3"), E("Ed", "4")]
    r1 = assign_buddies(members, [])
    r2 = assign_buddies(members, [])
    assert pset(r1) == pset(r2)
    assert r1 == r2


def test_feeding_result_back_is_zero_churn():
    members = [W("Wanda", "1"), W("Walt", "2"), E("Eve", "3"), E("Ed", "4")]
    r1 = assign_buddies(members, [])
    r2 = assign_buddies(members, r1.pairs)
    assert pset(r2) == pset(r1)
    assert not r2.unpaired_wl and not r2.unpaired_eng


def test_existing_pair_preserved_not_reshuffled():
    members = [W("Walt", "1"), W("Wanda", "2"), E("Ed", "3"), E("Eve", "4")]
    # Walt is already paired with Eve (out of alphabetical order on purpose).
    existing = [Pair("Walt", "1", "Eve", "4")]
    r = assign_buddies(members, existing)
    assert ("1", "4") in pset(r)  # Walt↔Eve survived
    assert ("2", "3") in pset(r)  # Wanda↔Ed filled from the free pool
    assert len(r.pairs) == 2


# ── uneven counts ─────────────────────────────────────────────────────────────


def test_wl_heavy_leaves_war_leaders_unpaired():
    members = [W("A", "1"), W("B", "2"), W("C", "3"), E("X", "4"), E("Y", "5")]
    r = assign_buddies(members, [], engineer_doubling=False)
    assert len(r.pairs) == 2
    assert len(r.unpaired_wl) == 1
    assert not r.unpaired_eng


def test_eng_heavy_leaves_engineers_unpaired_when_doubling_off():
    members = [W("A", "1"), W("B", "2"), E("X", "3"), E("Y", "4"), E("Z", "5")]
    r = assign_buddies(members, [], engineer_doubling=False)
    assert len(r.pairs) == 2
    assert not r.unpaired_wl
    assert len(r.unpaired_eng) == 1


# ── engineer doubling ─────────────────────────────────────────────────────────


def test_doubling_on_attaches_extra_engineer_to_a_war_leader():
    members = [W("A", "1"), W("B", "2"), E("X", "3"), E("Y", "4"), E("Z", "5")]
    r = assign_buddies(members, [], engineer_doubling=True)
    assert len(r.pairs) == 3
    assert not r.unpaired_eng
    # One War Leader now appears in two pairs (received two Engineers).
    wl_counts = {}
    for p in r.pairs:
        wl_counts[p.wl_discord_id] = wl_counts.get(p.wl_discord_id, 0) + 1
    assert sorted(wl_counts.values()) == [1, 2]


def test_doubling_never_doubles_war_leaders_in_wl_heavy_case():
    members = [W("A", "1"), W("B", "2"), W("C", "3"), E("X", "4"), E("Y", "5")]
    r = assign_buddies(members, [], engineer_doubling=True)
    # No spare Engineers to double — still 2 pairs, 1 unpaired WL.
    assert len(r.pairs) == 2
    assert len(r.unpaired_wl) == 1
    eng_counts = {}
    for p in r.pairs:
        eng_counts[p.eng_discord_id] = eng_counts.get(p.eng_discord_id, 0) + 1
    assert all(c == 1 for c in eng_counts.values())  # never 2 WLs to 1 Eng


# ── scarcity priority ─────────────────────────────────────────────────────────


def test_scarcity_strongest_first_pairs_strongest_war_leaders():
    members = [
        W("Weak", "1", power=100),
        W("Mid", "2", power=500),
        W("Strong", "3", power=900),
        E("X", "4"),
        E("Y", "5"),
    ]
    r = assign_buddies(members, [], wl_priority="power")
    paired_wl = {p.wl_discord_id for p in r.pairs}
    assert paired_wl == {"2", "3"}  # Mid + Strong
    assert [m.name for m in r.unpaired_wl] == ["Weak"]


def test_scarcity_alphabetical_default_ignores_power():
    members = [
        W("Charlie", "1", power=900),
        W("Alice", "2", power=100),
        W("Bob", "3", power=500),
        E("X", "4"),
        E("Y", "5"),
    ]
    r = assign_buddies(members, [], wl_priority="name")
    paired_wl = {p.wl_discord_id for p in r.pairs}
    assert paired_wl == {"2", "3"}  # Alice + Bob (alphabetical), not by power


def test_scarcity_stability_dominates_power():
    # Weak WL already paired; a stronger WL is free but only that one pair's
    # Engineer exists. Stability wins — the weak WL keeps its buddy.
    members = [W("Weak", "1", power=100), W("Strong", "2", power=900), E("X", "3")]
    existing = [Pair("Weak", "1", "X", "3")]
    r = assign_buddies(members, existing, wl_priority="power")
    assert ("1", "3") in pset(r)
    assert [m.name for m in r.unpaired_wl] == ["Strong"]


# ── profession change → dissolve + re-pair, and the notification copy ──────────


def test_profession_change_to_war_leader_repairs_and_notifies():
    # Before: Alice is an Engineer paired with Bill (War Leader); Chloe is a
    # free Engineer.
    before_members = [E("Alice", "1"), W("Bill", "2"), E("Chloe", "3")]
    before = assign_buddies(before_members, [Pair("Bill", "2", "Alice", "1")])
    assert ("2", "1") in pset(before)

    # Alice switches to War Leader. Her pair with Bill dissolves; she takes the
    # free Engineer Chloe; Bill is left without a buddy.
    after_members = [W("Alice", "1"), W("Bill", "2"), E("Chloe", "3")]
    after = assign_buddies(after_members, before.pairs)
    assert ("1", "3") in pset(after)  # Alice↔Chloe
    assert [m.name for m in after.unpaired_wl] == ["Bill"]

    msg = compose_change_notification("Alice", "War Leader", before, after)
    assert msg == (
        "Alice changed profession to War Leader. "
        "Alice is now paired with Chloe. "
        "Bill currently has no assigned buddy."
    )


def test_profession_change_to_engineer_leaves_two_unpaired():
    # Before: Alice (War Leader) paired with Bill (Engineer).
    before_members = [W("Alice", "1"), E("Bill", "2")]
    before = assign_buddies(before_members, [])
    assert ("1", "2") in pset(before)

    # Alice switches to Engineer — now two Engineers, no War Leader.
    after_members = [E("Alice", "1"), E("Bill", "2")]
    after = assign_buddies(after_members, before.pairs)
    assert not after.pairs
    assert names(after.unpaired_eng) == ["Alice", "Bill"]

    msg = compose_change_notification("Alice", "Engineer", before, after)
    assert msg == (
        "Alice changed profession to Engineer. Alice and Bill currently have no assigned buddy."
    )


# ── manual pair stickiness ────────────────────────────────────────────────────


def test_merge_members_squad_wins_buddy_fills():
    squad = [Member("Walt", "1", buddy.WAR_LEADER)]
    fallback = [Member("Walt", "1", buddy.ENGINEER), Member("Eve", "3", buddy.ENGINEER)]
    merged = {m.discord_id: m for m in buddy.merge_members(squad, fallback)}
    assert merged["1"].profession == buddy.WAR_LEADER  # Squad Powers wins
    assert merged["3"].profession == buddy.ENGINEER  # buddy-tab fills the gap


def test_merge_members_buddy_fills_when_squad_profession_blank():
    squad = [Member("Walt", "1", "")]  # surveyed but no profession set
    merged = buddy.merge_members(squad, [Member("Walt", "1", buddy.WAR_LEADER)])
    assert merged[0].profession == buddy.WAR_LEADER


def test_manual_pair_survives_auto_run_and_keeps_source():
    members = [W("A", "1"), W("B", "2"), E("X", "3"), E("Y", "4")]
    manual = Pair("A", "1", "Y", "4", source="manual")
    r = assign_buddies(members, [manual])
    assert ("1", "4") in pset(r)
    kept = next(p for p in r.pairs if p.wl_discord_id == "1" and p.eng_discord_id == "4")
    assert kept.source == "manual"
    # The other two still get paired from the free pool.
    assert ("2", "3") in pset(r)
