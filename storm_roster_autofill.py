"""
Auto-fill for the structured roster builder: from the signed-up pool,
the member rules and the LW 20-starters-plus-10-subs team rule (#219),
build a complete draft roster the officer can then correct.

Split out of `storm_roster_builder.py` in the #589 step 11 refactor.
`storm_roster_builder` imports every public name here back under its old
underscore name, so `storm_roster_builder._auto_fill_session` and the
fill strategies keep resolving and every test patch keeps its target.
This module reaches back into `storm_roster_builder` through `_builder()`
at call time for the session's floor and rule-subject helpers, so
importing either module first works.

The algorithm, in order (each step is one function below):

  1. `_apply_zone_rules`: per_member zone rules pin members to their
     named zone (Phase 1 only on phase-aware presets; the rule model
     has no phase dimension). Pinned members are starters regardless
     of where they rank by power.
  2. `_split_starters_and_subs`: starters and the sub pool by squad
     power, or by the saved team plan when one exists (#239). Members
     with no parseable power go to `gaps` and are not auto-placed.
  3. `_fill_phases`: the same starter pool fills every phase the preset
     declares, by the chosen strategy: "balanced" round-robins across
     zones in priority order; "priority_greedy" feeds the strongest
     members to the highest-priority zones, balancing power between
     zones that share a priority (#273). Both share
     `place_starter_in_zone`, so floor handling and the summary
     bookkeeping stay aligned.
  4. `_pair_subs`: paired mode walks each phase's primaries weakest-
     first and gives each the unpaired candidate whose power is
     closest, zone floor permitting; `_unpaired_sub_reasons` then
     explains any candidate whose power was below every remaining
     floor (#238).
  5. `_spill_over`: whatever power-known member landed nowhere ends
     up in `session.subs`; in pool mode that is the sub roster, in
     paired mode the overflow.

Every assignment, pairing, override flag and summary entry is produced
exactly as before; `tests/unit/test_storm_roster_autofill.py` holds the
end state of ten scenarios against the function this replaced.
"""

from __future__ import annotations

from itertools import groupby


def _builder():
    """The roster builder module, resolved at call time (see the module docstring)."""
    import storm_roster_builder

    return storm_roster_builder


AUTO_FILL_STRATEGIES = ("balanced", "priority_greedy")


# ── Placing one starter ──────────────────────────────────────────────────────


def place_starter_in_zone(
    session,
    starter_key: str,
    zone_name: str,
    phase: int,
    summary: dict,
) -> None:
    """Append a starter to a phase's zone and update the auto-fill
    bookkeeping (below-floor override flag, power-band counter,
    `auto_filled_by_power` count). Shared by every fill strategy so
    floor handling and the summary counts stay in sync (#226)."""
    session.assignments_for_phase(phase)[zone_name].append(starter_key)
    summary["auto_filled_by_power"] += 1
    preset_floor = session.floor_for_zone(zone_name)
    effective_floor = _builder()._effective_floor_for_zone(session, zone_name)
    member_power = session.members[starter_key].get("power")
    if member_power is None:
        session.below_floor_overrides_for_phase(phase).add(starter_key)
    elif member_power < effective_floor:
        session.below_floor_overrides_for_phase(phase).add(starter_key)
    elif effective_floor < preset_floor and member_power < preset_floor:
        summary["power_band_rules_applied"] += 1


def zone_priority_value(session, z, phase: int) -> int:
    """The effective priority of a zone for a phase, matching the sort key
    used to order `zones_sorted`. priority=0 ("no priority set") sorts last
    via 9999. Phase-aware presets read per-phase priority; flat presets use
    the single `priority` field."""
    prio = z.priority_for_phase(phase) if session.is_phase_aware else z.priority
    return prio if prio > 0 else 9999


# ── The two fill strategies ──────────────────────────────────────────────────


def fill_balanced(
    session,
    remaining: list[str],
    phase: int,
    zones_sorted: list,
    phase_assignments: dict,
    summary: dict,
) -> None:
    """Round-robin fill: pass over zones in priority order placing one
    starter per zone per pass, looping until every starter is placed
    or no zone has remaining capacity. Spreads power evenly across
    every zone the team uses this phase. 0-cap zones are skipped by
    the capacity guard."""
    while remaining:
        progress = False
        for z in zones_sorted:
            if not remaining:
                break
            if session.zone_member_count(z.zone) >= session.zone_capacity(z.zone):
                continue
            starter_key = remaining.pop(0)
            place_starter_in_zone(session, starter_key, z.zone, phase, summary)
            progress = True
        if not progress:
            # Every zone is full this phase. Remaining starters stay
            # unassigned for this phase; the officer can place them
            # manually via the picker.
            break


def fill_priority_greedy(
    session,
    remaining: list[str],
    phase: int,
    zones_sorted: list,
    phase_assignments: dict,
    summary: dict,
) -> None:
    """Priority-greedy fill, balanced within each priority tier (#273).

    Walks zones in priority asc, but zones that share a priority form a
    group and are balanced by total squad power instead of being filled
    one-at-a-time. Within a group, each next-strongest starter goes to the
    group zone with the lowest running power total that still has capacity
    (longest-processing-time / greedy load balancing). Across groups the
    pool is still consumed strongest-first, so higher-priority zones get
    the strongest members overall — only the lopsided split between
    equal-priority zones (e.g. Oil Refinery I taking the top 5 and II the
    next 5) is fixed. 0-cap zones are skipped by the capacity guard.

    `remaining` is assumed power-desc (the caller sorts it); members with
    unknown power count as 0 for balancing purposes."""

    def _power(key: str) -> int:
        return session.members.get(key, {}).get("power") or 0

    # `zones_sorted` is already priority-asc, so consecutive equal-priority
    # zones are adjacent and groupby yields one group per tier in order.
    for _prio, grp in groupby(zones_sorted, key=lambda z: zone_priority_value(session, z, phase)):
        group = list(grp)
        if not remaining:
            break
        # Seed each zone's running total from anything already placed
        # there (e.g. per-member pins landed before the fill).
        running = {z.zone: sum(_power(k) for k in phase_assignments.get(z.zone, [])) for z in group}
        while remaining:
            open_zones = [
                z
                for z in group
                if session.zone_member_count(z.zone) < session.zone_capacity(z.zone)
            ]
            if not open_zones:
                break
            # Lowest running power first; ties keep group (priority-sort)
            # order so the result is deterministic across re-runs.
            target = min(open_zones, key=lambda z: running[z.zone])
            starter_key = remaining.pop(0)
            place_starter_in_zone(session, starter_key, target.zone, phase, summary)
            running[target.zone] += _power(starter_key)


_STRATEGIES = {"balanced": fill_balanced, "priority_greedy": fill_priority_greedy}


# ── The steps ────────────────────────────────────────────────────────────────


def new_summary() -> dict:
    """The auto-fill summary the embed renders. Every key present from
    the start so the renderer never guards for a missing one."""
    return {
        "per_member_rules_applied": 0,
        "power_band_rules_applied": 0,
        "auto_filled_by_power": 0,
        # Decision #14 (#171): each auto-pair listed as `Primary ↔ Sub`
        # rather than a bare count; officers edit these most often.
        "auto_paired_subs": [],
        "gaps": [],  # member names with no parseable power
        "conflicts": [],  # short strings: rule application failures
        # #219: starter seats left unfilled because too few signed up.
        "starters_short": 0,
        # #238: subs left in the pool because their power was below the
        # floor for every remaining unpaired primary's zone; each is
        # `{"name", "power", "min_floor"}` so the embed can say why.
        "unpaired_subs_below_floor": [],
    }


def _reset(session) -> None:
    """Auto-fill is "redo from scratch": every phase's assignments,
    the sub pool, the pairings and the override flags all clear."""
    for phase in session.iter_phases():
        for zone in list(session.assignments_for_phase(phase).keys()):
            session.assignments_for_phase(phase)[zone] = []
    session.subs = []
    session.paired_subs.clear()
    session.paired_subs_p2.clear()
    session.paired_subs_p3.clear()
    session.below_floor_overrides.clear()
    session.below_floor_overrides_p2.clear()
    session.below_floor_overrides_p3.clear()


def _apply_zone_rules(session, summary: dict) -> set[str]:
    """Step 1: pin per_member zone rules into Phase 1. A rule whose
    subject isn't in tonight's pool is a silent no-op (Decision #7,
    #173); an unknown zone, a full zone or a second pin for the same
    member surfaces in `conflicts`. Returns the pinned keys."""
    resolve = _builder()._resolve_per_member_subject
    session.selected_phase = 1
    for rule in session.per_member_rules:
        if rule.sub_type != "zone":
            continue
        subject = rule.subject.strip()
        zone = rule.value.strip()
        match_key = resolve(session.members, subject)
        if match_key is None:
            continue
        if not session.preset.find_zone(zone):
            summary["conflicts"].append(f"per_member rule names unknown zone: {zone}")
            continue
        if session.zone_member_count(zone) >= session.zone_capacity(zone):
            summary["conflicts"].append(f"{zone} full when pinning {subject}")
            continue
        # A pinned member can't already be assigned in any phase or in
        # the sub pool.
        if match_key in session.assigned_member_keys():
            summary["conflicts"].append(f"{subject} pinned to multiple zones")
            continue
        session.assignments_for_phase(1)[zone].append(match_key)
        summary["per_member_rules_applied"] += 1
        member = session.members.get(match_key)
        if member is not None and member.get("power") is None:
            session.below_floor_overrides_for_phase(1).add(match_key)

    pinned: set[str] = set()
    for zone_members in session.assignments_for_phase(1).values():
        pinned.update(zone_members)
    return pinned


def _power_rank(session):
    """Power desc, ties on member key: deterministic across re-runs of
    auto-fill on the same sign-ups."""

    def key(k: str) -> tuple[int, str]:
        m = session.members[k]
        return (-(m.get("power") or 0), k)

    return key


def _saved_plan(session) -> dict | None:
    """The saved team plan for this session's (guild, event, date, team),
    or None. Production callers let the session drive the lookup; tests
    inject an explicit plan instead."""
    if not (session.event_date and session.team):
        return None
    try:
        import config

        return config.get_storm_team_plan(
            session.guild_id, session.event_type, session.event_date, session.team
        )
    except Exception:
        return None


def _split_starters_and_subs(
    session, pinned: set[str], summary: dict, plan: dict | None
) -> tuple[list[str], list[str], bool, set[str]]:
    """Step 2. Returns `(starters, sub_pool, plan_applied, plan_sub_keys)`.

    With a saved plan (#239) the in-game commitment overrides the
    by-power split: the plan's primaries are the starters, its subs the
    pool. Pinned members still take starter seats first; a pin-vs-sub
    conflict surfaces and the pin wins; plan keys missing from the pool
    (vote changed to cannot, member dropped) surface too. Without one,
    power-known members rank by power desc and the top
    `team_seats` become starters, the next slice the sub pool."""
    from storm import team_seats

    starters_target, subs_target = team_seats(session.event_type)
    rank = _power_rank(session)

    plan_applied = bool(plan and (plan.get("primaries") or plan.get("subs")))
    plan_sub_keys: set[str] = set()
    if plan_applied:
        member_keys = set(session.members.keys())
        plan_primary_keys = set(plan.get("primaries") or []) & member_keys
        plan_sub_keys = set(plan.get("subs") or []) & member_keys
        for k in sorted(pinned & plan_sub_keys):
            mname = session.members.get(k, {}).get("name", k)
            summary["conflicts"].append(
                f"{mname} is pinned by a per-member rule but the saved "
                f"team plan marks them as a sub — pin wins."
            )
        plan_sub_keys -= pinned
        all_plan_keys = set(plan.get("primaries") or []) | set(plan.get("subs") or [])
        for k in sorted(all_plan_keys - member_keys):
            summary["conflicts"].append(
                f"plan key {k} missing from pool (vote changed or member dropped from roster)"
            )
        # Gaps apply either way: any member with no parseable power
        # that isn't pinned.
        for key, m in session.members.items():
            if m.get("power") is None and key not in pinned:
                summary["gaps"].append(m["name"])
        starters = list(pinned | plan_primary_keys)
        summary["starters_short"] = max(0, starters_target - len(starters))
        return starters, sorted(plan_sub_keys), True, plan_sub_keys

    power_known = [k for k, m in session.members.items() if m.get("power") is not None]
    power_known.sort(key=rank)
    for key, m in session.members.items():
        if m.get("power") is None and key not in pinned:
            summary["gaps"].append(m["name"])

    starters = list(pinned)
    starters_set = set(starters)
    for key in power_known:
        if len(starters) >= starters_target:
            break
        if key in starters_set:
            continue
        starters.append(key)
        starters_set.add(key)
    summary["starters_short"] = max(0, starters_target - len(starters))

    sub_pool: list[str] = []
    for key in power_known:
        if len(sub_pool) >= subs_target:
            break
        if key in starters_set:
            continue
        sub_pool.append(key)
    return starters, sub_pool, False, plan_sub_keys


def _fill_phases(session, starters: list[str], strategy: str, summary: dict) -> None:
    """Step 3: the same starter pool fills every phase (#226), zones in
    priority order. Members a per-member rule already placed in Phase 1
    keep their seat; the rest are placed power-desc so power-known
    starters land before pinned-with-unknown-power ones."""
    fill = _STRATEGIES[strategy]
    rank = _power_rank(session)
    for phase in session.iter_phases():
        session.selected_phase = phase
        phase_assignments = session.assignments_for_phase(phase)
        zones_sorted = sorted(
            session.preset.zones, key=lambda z: zone_priority_value(session, z, phase)
        )
        already_placed: set[str] = set()
        for zone_members in phase_assignments.values():
            already_placed.update(zone_members)
        remaining = [k for k in starters if k not in already_placed]
        remaining.sort(key=rank)
        fill(session, remaining, phase, zones_sorted, phase_assignments, summary)


def _placed_anywhere(session) -> set[str]:
    placed: set[str] = set()
    for phase in session.iter_phases():
        for zone_members in session.assignments_for_phase(phase).values():
            placed.update(zone_members)
    return placed


def _pair_subs(session, candidates: list[str], summary: dict) -> None:
    """Step 4, paired mode: per phase, walk the primaries weakest-first
    (unknown power last) and give each the unpaired candidate whose
    power is closest, among those clearing the primary's zone floor. A
    power-unknown primary takes the strongest eligible candidate. The
    per-phase pairing dicts are independent, so one candidate can back
    a primary in every phase."""
    effective_floor = _builder()._effective_floor_for_zone
    for phase in session.iter_phases():
        session.selected_phase = phase
        phase_assignments = session.assignments_for_phase(phase)
        phase_pairings = session.paired_subs_for_phase(phase)

        primaries_with_zone: list[tuple[str, str]] = []
        for zone_name, zmembers in phase_assignments.items():
            for primary_key in zmembers:
                if primary_key in phase_pairings:
                    continue
                primaries_with_zone.append((primary_key, zone_name))

        def _primary_rank_key(item: tuple[str, str]) -> tuple[int, str]:
            key, _ = item
            power = session.members.get(key, {}).get("power")
            # 10**18 outranks any realistic squad power, so power-unknown
            # primaries pair last.
            return (power if power is not None else 10**18, key)

        primaries_with_zone.sort(key=_primary_rank_key)

        available = list(candidates)
        for primary_key, primary_zone in primaries_with_zone:
            if not available:
                break
            primary_m = session.members.get(primary_key, {})
            primary_power = primary_m.get("power")
            floor = effective_floor(session, primary_zone)
            eligible = [
                sub_key
                for sub_key in available
                if sub_key != primary_key
                and session.members.get(sub_key, {}).get("power") is not None
                and session.members.get(sub_key, {}).get("power") >= floor
            ]
            if not eligible:
                continue
            if primary_power is None:
                eligible.sort(key=lambda sk: -(session.members[sk].get("power") or 0))
            else:
                eligible.sort(
                    key=lambda sk: (
                        abs((session.members[sk].get("power") or 0) - primary_power),
                        sk,
                    )
                )
            chosen = eligible[0]
            phase_pairings[primary_key] = chosen
            available.remove(chosen)
            sub_m = session.members.get(chosen, {})
            summary["auto_paired_subs"].append(
                f"{primary_m.get('name', primary_key)} ↔ {sub_m.get('name', chosen)}"
            )


def _unpaired_sub_reasons(session, candidates: list[str], summary: dict) -> None:
    """Step 4b (#238): after pairing, name the candidates whose power
    was below the floor for every still-unpaired primary's zone, so the
    embed can say "Couldn't pair Alice (60M): power below the 80M
    minimum for any remaining open positions" instead of leaving them
    in the pool unexplained."""
    effective_floor = _builder()._effective_floor_for_zone
    all_paired: set[str] = set()
    for ph in session.iter_phases():
        all_paired.update(session.paired_subs_for_phase(ph).values())
    unpaired_floors: list[tuple[str, int]] = []
    for ph in session.iter_phases():
        ph_pairings = session.paired_subs_for_phase(ph)
        for zone, primary_keys in session.assignments_for_phase(ph).items():
            for pk in primary_keys:
                if pk not in ph_pairings:
                    floor = effective_floor(session, zone)
                    if floor > 0:
                        unpaired_floors.append((zone, floor))
    seen: set[str] = set()
    for sub_key in candidates:
        if sub_key in all_paired or sub_key in seen:
            continue
        seen.add(sub_key)
        sub_m = session.members.get(sub_key, {})
        sub_power = sub_m.get("power")
        if sub_power is None:
            continue  # already in summary["gaps"]
        if not unpaired_floors:
            continue  # no unpaired primaries: the sub was surplus
        if all(sub_power < floor for _, floor in unpaired_floors):
            summary["unpaired_subs_below_floor"].append(
                {
                    "name": sub_m.get("name", sub_key),
                    "power": sub_power,
                    "min_floor": min(f for _, f in unpaired_floors),
                }
            )


def _spill_over(session, plan_applied: bool, plan_sub_keys: set[str]) -> None:
    """Step 5. With a plan (#239) `session.subs` is exactly the plan's
    subs still in the pool; non-plan members never spill in, since they
    aren't part of the in-game commitment. Otherwise every power-known
    member that landed in no zone and no paired seat goes to the flat
    sub pool (the 10 designated subs in pool mode, overflow in paired)."""
    assigned = session.assigned_member_keys()
    if plan_applied:
        session.subs = sorted(plan_sub_keys - assigned)
        return
    for key, m in session.members.items():
        if key in assigned:
            continue
        if m.get("power") is None:
            continue  # already in summary["gaps"]
        session.subs.append(key)


# ── Entry point ──────────────────────────────────────────────────────────────


def auto_fill_session(
    session,
    *,
    strategy: str = "balanced",
    plan: dict | None = None,
) -> dict:
    """Auto-fill the roster from member rules and the LW 20-starters-
    plus-10-subs team rule (#219). See the module docstring for the
    steps. Resets the current roster first, so a re-click is "redo
    from scratch". Returns the summary dict, also stored on
    `session.auto_fill_summary`. Every assignment stays officer-
    correctable in the picker before Approve & Post."""
    if strategy not in AUTO_FILL_STRATEGIES:
        strategy = "balanced"
    _reset(session)
    summary = new_summary()
    # The officer's UI cursor moves while each phase fills so the
    # capacity and member-count helpers resolve; restored at the end.
    original_phase = session.selected_phase

    pinned = _apply_zone_rules(session, summary)
    if plan is None:
        plan = _saved_plan(session)
    starters, sub_pool, plan_applied, plan_sub_keys = _split_starters_and_subs(
        session, pinned, summary, plan
    )
    _fill_phases(session, starters, strategy, summary)

    # Pair candidates are the sub pool plus any starter that couldn't
    # fit in a zone (small-alliance fallback, so the weakest placed
    # starter still gets a backup with fewer than 30 signed up).
    placed = _placed_anywhere(session)
    candidates = list(sub_pool) + [k for k in starters if k not in placed]
    if session.is_paired:
        _pair_subs(session, candidates, summary)
        _unpaired_sub_reasons(session, candidates, summary)

    _spill_over(session, plan_applied, plan_sub_keys)

    session.selected_phase = original_phase
    session.auto_fill_summary = summary
    return summary
