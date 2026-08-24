"""The knockout field, end to end: load it, reach it, price what pressing it costs.

Written after an attempt to do this by hand got stuck three separate times, and
each place it stuck is a test here rather than a note somewhere:

  1. A correctly-formed knockout payload imported 32 registrants and created
     ZERO groups, so the field could not be loaded at all.
  2. With no knockout group, the group view fell back to the qualifiers and
     there was no round picker to escape with -- the picker only renders once
     two rounds already hold data.
  3. Only then is the odds button reachable, and pressing it is the most
     expensive thing the bot does.

WHAT IS ASSERTED, AND WHAT IS ONLY REPORTED. The cost of an odds run is not a
stable number: it is GIL contention, and measured on one machine it ranged from
1.3x on a single core to 32x on sixteen, because a compute thread on its own
core keeps winning the re-acquire race against a thread that has just woken.
Asserting a millisecond figure would be asserting the size of the CI runner.

So the invariant asserted here is the one that held at EVERY core count: the
event loop itself stays responsive, because the run is on `asyncio.to_thread`.
That is the property that breaks catastrophically if someone ever moves the
simulation back onto the loop, and it is worth a guard. The per-command cost is
printed instead, with the core count beside it so the number can be read.
"""

from __future__ import annotations

import asyncio
import os
import statistics
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import champion_duel_db as db

#: The knockout field is one field of 32 with a NULL label, not lettered groups.
FIELD_SIZE = 32

#: `push_to_bot.FALLBACK_RATIOS`, so a fixture player is shaped like a real
#: unscouted registrant rather than like something only a test would produce.
TYPES = ("Tank", "Missile", "Aircraft")
RATIOS = (0.338, 0.258, 0.238)

WARZONES = [str(700 + i) for i in range(16)]


def _registrant(i: int) -> dict:
    """One entrant, spread across the range the real event runs in."""
    thp = (480 - i * 7) * 1_000_000
    return {
        "name": f"P{i:02d}",
        "server": WARZONES[i % len(WARZONES)],
        "alliance": "OGV",
        "thp": thp,
        "rank": i + 1,
        # The knockouts have no group letter. This is the value the bot's own
        # data layer stores (`champion_duel_db.get_or_create_group`: "None for
        # knockouts, which are a single field of 32 rather than lettered
        # groups"), so it is the value a correct payload carries.
        "group": None,
    }


def _squads(name: str, thp: float) -> list[dict]:
    return [
        {
            "name": name,
            "slot": slot,
            "type": squad_type,
            "power": round(thp * ratio),
            "source": "estimated",
        }
        for slot, (squad_type, ratio) in enumerate(zip(TYPES, RATIOS), start=1)
    ]


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    """This feature's own database file, never `config.DB_PATH`."""
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return db


@pytest.fixture
def grouping(cd_db):
    return db.ensure_grouping(WARZONES, "2026-08-04")


@pytest.fixture
def imported(cd_db, grouping):
    """The 32 pushed through `import_registrants`, exactly as the admin command does.

    Deliberately NOT used by the navigation and cost tests. It is the subject of
    one test rather than the setup for all of them, because it is currently
    broken and a fixture that fails takes every test in the file with it.
    """
    rows = [_registrant(i) for i in range(FIELD_SIZE)]
    result = db.import_registrants(rows, stage="knockouts", grouping_id=grouping["id"])
    return {"grouping": grouping, "rows": rows, "result": result}


@pytest.fixture
def loaded(cd_db, grouping):
    """The 32 in the field, placed through the data layer's own call.

    `set_stage` is what `import_registrants` reaches for once it decides a row
    belongs to a round, and it handles a NULL knockout label correctly -- it is
    only the decision above it that is wrong. Placing directly here keeps every
    test below independent of that bug, so this file stays usable as a harness
    while the importer is still broken.
    """
    rows = [_registrant(i) for i in range(FIELD_SIZE)]
    db.import_registrants(rows, grouping_id=grouping["id"])  # people, no round
    for row in rows:
        found = db.find_registrants(row["name"], row["server"])
        assert found, f"{row['name']} did not import"
        db.set_stage(found[0]["id"], "knockouts", grp=None, grouping_id=grouping["id"])
    squads = []
    for row in rows:
        squads.extend(_squads(row["name"], row["thp"]))
    db.import_squads(squads, actor={"discord_user_id": "1", "discord_name": "test"})
    return {"grouping": grouping, "rows": rows}


def _as_send_group_view_would(grouping):
    """Resolve the round, label, group and members the way the surface does.

    Mirrors `champion_duel_hub.send_group_view` step for step rather than
    reaching for whichever call is convenient, because the steps are where the
    knockouts differ: `get_groups` filters `label IS NOT NULL`, so it returns
    NOTHING for a knockout field and the label falls through to None -- which
    is correct, and is also why the "Which group?" select does not render on a
    round that has only one field.
    """
    stages = db.recorded_stages(grouping["id"])
    running = db.current_stage(grouping["id"])
    stage = running if running in stages else stages[-1]
    groups = db.get_groups(stage, grouping["id"])
    label = str(groups[0]["group"]) if groups else None
    group = db.get_or_create_group(grouping["id"], stage, label)
    return stage, label, group, db.get_group_members(group["id"])


# ── 1. the field can be loaded ───────────────────────────────────────────────


def test_a_knockout_payload_lands_as_one_field_of_32(imported, grouping):
    """The gap that blocked the round, and the reason this file exists.

    `import_registrants` places a row with `if stage and row.get("group")`
    (`champion_duel_db.py:2391`) -- a TRUTHINESS test. A knockout label is
    deliberately NULL, so every row of a correct knockout payload reads as "not
    in this round" and is skipped. The import reports its registrants and
    squads normally and silently creates no group, which is what makes it hard
    to spot: nothing fails, the field just is not there.

    The same guard is right for the rounds it was written for -- a semifinal
    payload carries all 1,600 registrants and only the 128 advancers have a
    group, so a blank there genuinely means "not in it". The knockouts are the
    one round whose real label is falsy.
    """
    assert "knockouts" in db.recorded_stages(grouping["id"]), (
        "the import created no knockout group at all. `import_registrants` "
        "places a row only `if stage and row.get('group')`, and a knockout "
        "label is the NULL that guard reads as 'not in this round'."
    )
    # Counted straight off the table rather than through `get_groups`, which
    # filters `label IS NOT NULL` and so can never return the knockout field --
    # and rather than through `get_or_create_group`, which would create the row
    # this test exists to prove was created.
    with db._get_conn() as conn:
        rows = conn.execute(
            "SELECT id, label FROM groups WHERE grouping_id = ? AND stage = 'knockouts'",
            (grouping["id"],),
        ).fetchall()
    assert len(rows) == 1, f"the knockouts are one field, got {len(rows)} rows"
    assert rows[0]["label"] is None, "the knockout field is unlettered"
    assert len(db.get_group_members(rows[0]["id"])) == FIELD_SIZE


def test_the_qualifiers_are_untouched_by_a_knockout_import(loaded, grouping):
    """Loading one round must never disturb another."""
    assert db.get_groups("qualifiers", grouping["id"]) == []


def test_a_whole_roster_relabelled_knockouts_is_refused(cd_db, grouping):
    """The other half of the same bug, and the more damaging half.

    `push_to_bot --stage knockouts` stamps the round onto the qualifier draw
    without changing it, so the payload arrives as 1,600 players carrying
    lettered qualifier groups. Every one of those labels is truthy, so a
    permissive fix would place all 1,600 into a field of 32 -- loading the
    round with the wrong people, which is worse than not loading it.

    Refused whole rather than partly applied: a half-filled knockout field
    would have to be found and unpicked by hand.
    """
    roster = [dict(_registrant(i), group=chr(ord("A") + i % 16)) for i in range(200)]
    with pytest.raises(ValueError, match="field of 32"):
        db.import_registrants(roster, stage="knockouts", grouping_id=grouping["id"])
    assert db.recorded_stages(grouping["id"]) == [], "a refused import must place nobody"


# ── 2. the field can be reached ──────────────────────────────────────────────


def test_the_round_the_view_lands_on_is_one_that_holds_data(loaded, grouping):
    """Why it kept showing Qualifiers.

    `send_group_view` picks `running if running in stages else stages[-1]`,
    where `stages` is only the rounds that hold groups. On 2026-08-04 + 19 days
    the calendar says semi-finals, semi-finals holds nothing, so it fell back
    to the furthest round that did. With the knockouts loaded it lands on them.
    """
    stages = db.recorded_stages(grouping["id"])
    assert stages == ["knockouts"], stages
    running = db.current_stage(grouping["id"])
    landed = running if running in stages else stages[-1]
    assert landed == "knockouts"


def test_a_single_round_renders_no_picker_to_escape_with(loaded, grouping):
    """The navigation trap, stated as a fact rather than a complaint.

    Each select renders only when it has something to choose between, so a
    grouping holding one round shows none -- and the round it holds is then the
    only round reachable. That is fine when it is the right round and a dead
    end when it is not, which is the shape of the problem a member hits.
    """
    import champion_duel_hub as hub

    stage, label, group, members = _as_send_group_view_would(grouping)
    view = hub._GroupView(
        user_id=1,
        groupings=[grouping],
        grouping=grouping,
        stages=db.recorded_stages(grouping["id"]),
        stage=stage,
        groups=db.get_groups(stage, grouping["id"]),
        label=label,
        members=members,
        can_odds=True,
    )
    selects = [i for i in view.children if i.__class__.__name__ == "Select"]
    assert selects == [], "one round, one grouping, one group: nothing to choose"
    odds = [i for i in view.children if getattr(i, "callback", None) == view._on_odds]
    assert len(odds) == 1, "the odds control must be reachable from the group view"
    assert not odds[0].disabled


# ── 3. what pressing it costs ────────────────────────────────────────────────


async def _probe(stop: asyncio.Event, loop_lag: list, thread_lag: list):
    """Two things a member's command depends on, sampled until told to stop.

    `loop_lag` is the event loop's own responsiveness -- pure Python on the loop
    thread. `thread_lag` is a `to_thread` round trip, which is what all 274 of
    the bot's database reads actually are, and the term that moves.
    """

    async def ticker():
        while not stop.is_set():
            t0 = time.perf_counter()
            await asyncio.sleep(0.01)
            loop_lag.append((time.perf_counter() - t0 - 0.01) * 1000)

    async def crosser():
        while not stop.is_set():
            t0 = time.perf_counter()
            await asyncio.to_thread(lambda: sum(range(50)))
            thread_lag.append((time.perf_counter() - t0) * 1000)
            await asyncio.sleep(0.05)

    await asyncio.gather(ticker(), crosser())


async def _measure(seconds: float, alongside=None) -> dict:
    stop = asyncio.Event()
    loop_lag, thread_lag = [], []
    probe = asyncio.create_task(_probe(stop, loop_lag, thread_lag))
    work = asyncio.create_task(alongside()) if alongside else None
    if work is not None:
        await work
    else:
        await asyncio.sleep(seconds)
    stop.set()
    await probe
    return {
        "loop_p50": statistics.median(loop_lag or [0]),
        "loop_p95": sorted(loop_lag)[int(0.95 * (len(loop_lag) - 1))] if loop_lag else 0,
        "thread_p50": statistics.median(thread_lag or [0]),
        "result": work.result() if work is not None else None,
    }


@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.skipif(
    not os.environ.get("ODDS_COST_TEST"),
    reason=(
        "opt-in: a cold 250-trial run. CI runs tests/integration twice per PR "
        "(once under FORCE_PREMIUM=1) and deselects only `sheets`, so leaving "
        "this on would buy two cold brackets a PR on a slower runner. Enable "
        "with ODDS_COST_TEST=1."
    ),
)
async def test_an_odds_run_keeps_the_event_loop_responsive(loaded, grouping, monkeypatch):
    """The expensive press, with the rest of the bot measured underneath it.

    ASSERTED: the event loop stays responsive. That held at every core count
    measured, because the run is on `asyncio.to_thread`, and it is what breaks
    if the simulation is ever moved back onto the loop.

    REPORTED, NOT ASSERTED: what a database read costs during the run. That is
    GIL contention and it scales with how many cores the host will let the
    compute thread sit on -- 1.3x on one core, 32x on sixteen. Asserting it
    would be asserting the runner's shape. Set ODDS_COST_MAX_RATIO to turn the
    report into a gate on a machine you control.
    """
    import champion_duel_hub as hub
    import champion_duel_odds as odds_lib

    if not odds_lib.KNOCKOUT_AVAILABLE:
        pytest.skip("the installed champion-duel-engine has no knockout model (1.12.0+)")

    stage, label, group, _members = _as_send_group_view_would(grouping)
    scouted = db.get_group_scouting(group["id"])
    assert len(scouted) == FIELD_SIZE, f"the press needs the whole field, have {len(scouted)}"
    # The press is a cold one. Warm is a dict hit and measures nothing.
    odds_lib._CACHE.clear()

    async def press():
        return await asyncio.to_thread(hub.build_odds_embed, scouted, stage, label, grouping)

    idle = await _measure(3.0)
    t0 = time.perf_counter()
    under = await _measure(0, alongside=press)
    run_seconds = time.perf_counter() - t0

    ratio = under["thread_p50"] / idle["thread_p50"] if idle["thread_p50"] else float("nan")
    print(
        f"\n  cores {os.cpu_count()}   matrix_trials {odds_lib.MATRIX_TRIALS}"
        f"   run {run_seconds:.1f}s"
        f"\n  event loop lag   idle {idle['loop_p50']:6.1f} ms -> {under['loop_p50']:7.1f} ms"
        f"  (p95 {under['loop_p95']:.1f})"
        f"\n  to_thread round  idle {idle['thread_p50']:6.1f} ms -> {under['thread_p50']:7.1f} ms"
        f"  ({ratio:.0f}x)  <-- every database read the bot does"
    )

    embed = under["result"]
    assert embed is not None
    body = (embed.description or "") + "".join(f.value or "" for f in embed.fields)
    assert body.strip(), "the press must come back with a table, not an empty embed"

    # The invariant, and the only thing here that is machine-independent.
    assert under["loop_p95"] < 500, (
        f"the event loop stalled for {under['loop_p95']:.0f} ms during an odds run. "
        "The simulation must stay on asyncio.to_thread -- on the loop it would "
        "stop the heartbeat for every guild, not just slow them down."
    )

    cap = os.environ.get("ODDS_COST_MAX_RATIO")
    if cap:
        assert ratio <= float(cap), (
            f"a database read cost {ratio:.0f}x its idle latency during the run "
            f"(cap {cap}). Moving the simulation to a subprocess removes this."
        )
