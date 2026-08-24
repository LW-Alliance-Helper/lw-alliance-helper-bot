"""Getting the simulation off the bot's back, and holding onto the answer.

Three things land together here and they fail in different ways, so they are
tested apart:

  * THE CACHE was first-in-first-out with room for 32 entries, and one grouping
    is 17. Two live groupings evicted in insertion order, which is reliably the
    entry about to be needed again.
  * THE SUBPROCESS is the actual fix. A run is pure Python and holds the GIL for
    a minute and a half, and every `to_thread` database read in every guild
    waits behind it. The tests that matter here are that a child gives the SAME
    answer, and that a machine which cannot give us a child still gives the
    member a number.
  * THE STORE has to be wrong in the right direction. Stale is showable, a
    different set of people is not, and a rename is neither.

WHAT WOULD HAVE FAILED BEFORE. The cache tests fail against the old FIFO dict
and the old cap. The rest cover a module that did not exist, so they pin
behaviour rather than catch a regression -- said plainly rather than implied,
because "every test must be confirmed to fail against the unfixed source" is a
rule on this feature and this file is a partial exception to it.
"""

from __future__ import annotations

import ast
import io
import json
from datetime import datetime, timedelta, timezone

import pytest

import champion_duel_db as db
import champion_duel_odds as odds
import champion_duel_store as store

WARZONES = [str(700 + i) for i in range(16)]


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    store.init_store()
    return db


@pytest.fixture(autouse=True)
def _clean_cache():
    odds._CACHE.clear()
    yield
    odds._CACHE.clear()


@pytest.fixture
def group(cd_db):
    """One semifinal group of eight, through the real writes and the real read.

    Built with `set_stage` and read back with `get_group_scouting` rather than
    assembled by hand. `get_group_members` carries a warning that the odds tests
    build their member dicts themselves and so never exercise its SELECT -- which
    is how `thp` and `troop_level` each went missing from it unnoticed. Anything
    here that depends on a column existing depends on it for real.
    """
    grouping = db.ensure_grouping(WARZONES, "2026-08-04")
    ids = []
    for i in range(8):
        row = db.upsert_registrant(
            name=f"P{i:02d}",
            server=WARZONES[i],
            alliance="OGV",
            thp=(480 - i * 9) * 1_000_000,
        )
        db.set_stage(row["id"], "semifinals", grp="A", grouping_id=grouping["id"])
        ids.append(row["id"])
    group = db.get_or_create_group(grouping["id"], "semifinals", "A")
    return {"grouping": grouping, "group_id": group["id"], "ids": ids}


def _members(group) -> list[dict]:
    return db.get_group_scouting(group["group_id"])


def _fake_odds(members: list[dict]) -> odds.GroupOdds:
    """An answer shaped like the engine's, without paying for one.

    `key` is the row position, which is what the engine keys on and what the
    store translates to a registrant id. Most of what is tested below is about
    the bookkeeping around a result rather than the result, so buying a real
    3-second simulation for each of them would be paying for nothing.
    """
    return odds.GroupOdds(
        rows=[
            odds.OddsRow(
                name=m.get("display_name") or "?",
                advance=0.9 - i * 0.1,
                win_group=0.5 - i * 0.05,
                points_mean=100 - i,
                points_sd=5,
                key=str(i),
            )
            for i, m in enumerate(members)
        ],
        trials=800,
        advance=2,
    )


# ── the in-memory cache ──────────────────────────────────────────────────────


def test_reading_an_entry_saves_it_from_the_next_eviction():
    """The FIFO bug, directly. It evicted the entry it was about to need."""
    for i in range(odds._CACHE_MAX):
        odds._cache_put(f"k{i}", i)

    odds._cache_get("k0")  # the oldest, now the most recently used
    odds._cache_put("new", -1)

    assert "k0" in odds._CACHE, "a re-read entry was evicted anyway -- this is still FIFO"
    assert "k1" not in odds._CACHE, "the least recently used entry should have gone"
    assert len(odds._CACHE) == odds._CACHE_MAX


def test_two_live_groupings_fit():
    """16 semifinal groups plus a bracket is 17, and there can be two events."""
    assert odds._CACHE_MAX >= 34, (
        f"_CACHE_MAX is {odds._CACHE_MAX}; two live groupings need 34 entries and "
        "anything less evicts a group that is still being read"
    )


# ── the subprocess ───────────────────────────────────────────────────────────


def test_a_child_gives_the_same_answer_as_this_thread(group):
    """The one thing that must not change when the work moves off-process."""
    members = _members(group)

    odds.USE_SUBPROCESS = False
    odds._CACHE.clear()
    on_thread = odds.group_advance_odds(members)

    odds.USE_SUBPROCESS = True
    odds._CACHE.clear()
    in_child = odds.group_advance_odds(members)

    assert [(r.name, r.advance, r.points_mean) for r in on_thread.rows] == [
        (r.name, r.advance, r.points_mean) for r in in_child.rows
    ]


def test_a_machine_that_cannot_give_us_a_child_still_answers(group, monkeypatch, capsys):
    """A pool that will not start degrades to slow, which is what it was before.

    The member gets their number. Everyone else in every guild pays for it, the
    way they did before this change -- that is a worse bot, not a broken one,
    and it is the right way to fail.
    """
    members = _members(group)

    def no_pool(*a, **k):
        raise OSError("no fork, no spawn, nothing")

    monkeypatch.setattr(odds, "ProcessPoolExecutor", no_pool)
    odds.USE_SUBPROCESS = True
    odds._CACHE.clear()

    result = odds.group_advance_odds(members)

    assert len(result.rows) == 8
    assert "running in-thread" in capsys.readouterr().out


class _FakeFuture:
    def __init__(self, exc):
        self._exc = exc

    def result(self):
        raise self._exc


class _FakePool:
    """Stands in for a real pool so the branch can be tested without a child.

    What is under test here is which failures fall back and which propagate, and
    a real `ProcessPoolExecutor` cannot be used to ask that question: it pickles
    the job, so a counter the test can read is exactly the thing it refuses to
    send.
    """

    raises = None

    def __init__(self, *a, **k):
        pass

    def submit(self, fn, *args):
        return _FakeFuture(type(self).raises)

    def shutdown(self, wait=True):
        pass


def test_the_expensive_thing_is_not_run_twice_to_reach_the_same_exception(monkeypatch):
    """An exception from the JOB is not a pool failure and must not fall back.

    The fallback exists for a machine that cannot host a child. A group the
    engine refuses would refuse identically on this thread, so retrying it here
    would buy a second full run of the most expensive thing the bot does in
    order to arrive at the same error.
    """
    calls = []

    def counted():
        calls.append(1)
        return "ran here"

    _FakePool.raises = odds.NotEnoughData("the group has 3 players")
    monkeypatch.setattr(odds, "ProcessPoolExecutor", _FakePool)
    monkeypatch.setattr(odds, "USE_SUBPROCESS", True)

    with pytest.raises(odds.NotEnoughData):
        odds._run_off_process(counted)

    assert calls == [], "the job ran again on this thread after raising in the child"


def test_a_child_that_dies_is_retried_here(monkeypatch, capsys):
    """A dead child IS a machine problem, and the member should still get a number."""
    from concurrent.futures.process import BrokenProcessPool

    calls = []

    def counted():
        calls.append(1)
        return "ran here"

    _FakePool.raises = BrokenProcessPool("child died")
    monkeypatch.setattr(odds, "ProcessPoolExecutor", _FakePool)
    monkeypatch.setattr(odds, "USE_SUBPROCESS", True)

    assert odds._run_off_process(counted) == "ran here"
    assert calls == [1]
    assert "running in-thread" in capsys.readouterr().out


def test_the_real_jobs_survive_the_pipe(group):
    """Both jobs and their arguments have to pickle, or the press fails outright.

    An unpicklable argument raises from `future.result()` looking exactly like an
    exception from the job, so it does NOT degrade to running in-thread -- it
    reaches the member. Nothing here can go through a pipe by accident: the specs
    are plain dicts of floats and strings, and this asserts they stay that way.
    """
    import pickle

    members = _members(group)
    specs, _display, missing = odds._specs(members)
    assert not missing

    for job, args in (
        (odds._simulate_group_job, ("semifinals", specs, 800, 42, True)),
        (odds._simulate_bracket_job, (specs, 200_000, 42, 250, True)),
    ):
        pickle.loads(pickle.dumps(job))
        pickle.loads(pickle.dumps(args))


def test_the_odds_module_never_reaches_the_database():
    """The child imports this module. It must not be able to open SQLite.

    One writer, in the parent, which is what keeps WAL's single-writer story
    intact. This is true today by construction -- the module imports stdlib and
    the engine and nothing else -- and this test is here to keep it true, because
    the natural place to reach for a stored answer is exactly this file.
    """
    tree = ast.parse(io.open(odds.__file__, encoding="utf-8").read())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for banned in ("champion_duel_db", "champion_duel_store", "sqlite3", "discord"):
        assert banned not in imported, (
            f"champion_duel_odds imports {banned}. The simulation runs in a spawned "
            "child that re-imports this module; it receives specs and returns "
            "numbers, and it must never be able to open the database."
        )


# ── the fingerprint ──────────────────────────────────────────────────────────


def test_the_same_group_read_twice_fingerprints_the_same(group):
    members = _members(group)
    assert store.fingerprint(members, stage="semifinals") == store.fingerprint(
        members, stage="semifinals"
    )


def test_a_reordered_read_is_not_a_different_group(group):
    """`get_group_scouting` sorts on rank, and recording a rank must not re-run.

    The specs are paired with their registrant ids and sorted by id before
    hashing precisely so this holds. Without it, every rank recorded during a
    round would invalidate a group whose inputs to the model never moved.
    """
    members = _members(group)
    first, _ = store.fingerprint(members, stage="semifinals")
    second, _ = store.fingerprint(list(reversed(members)), stage="semifinals")
    assert first == second


def test_a_new_engine_pins_every_stored_answer_stale(group, monkeypatch):
    """The one that will bite. A table survives the deploy that moves the pin."""
    members = _members(group)
    before, _ = store.fingerprint(members, stage="semifinals")

    monkeypatch.setattr(store, "_engine_version", lambda: "1.14.0")
    after, _ = store.fingerprint(members, stage="semifinals")

    assert before != after, (
        "the engine version is not in the key, so a deploy that moves the pin "
        "would serve last version's numbers under the new model"
    )


def test_turning_the_sampler_up_invalidates_a_stored_bracket(group, monkeypatch):
    """`BRACKET_TRIALS` and `MATRIX_TRIALS` are in neither in-memory key today."""
    members = _members(group)
    monkeypatch.setattr(odds, "BRACKET_TRIALS", 20_000)
    before, _ = store.fingerprint(members, stage="knockouts")
    monkeypatch.setattr(odds, "BRACKET_TRIALS", 200_000)
    after, _ = store.fingerprint(members, stage="knockouts")

    assert before != after


def test_a_changed_power_is_a_different_question(group):
    members = _members(group)
    before, _ = store.fingerprint(members, stage="semifinals")

    db.set_registrant_thp(group["ids"][0], 999_000_000)
    after, _ = store.fingerprint(_members(group), stage="semifinals")

    assert before != after


# ── what the store will and will not show ────────────────────────────────────


def test_a_stored_answer_reads_back_fresh(group):
    members = _members(group)
    store.store(group["group_id"], members, _fake_odds(members), stage="semifinals")

    held = store.lookup(group["group_id"], _members(group), stage="semifinals")

    assert held.state == "fresh"
    assert held.showable
    assert len(held.odds.rows) == 8


def test_nothing_stored_is_missing_rather_than_empty(group):
    held = store.lookup(group["group_id"], _members(group), stage="semifinals")
    assert held.state == "missing"
    assert held.odds is None


def test_the_same_people_with_new_data_read_stale_and_stay_showable(group):
    """Stale is about you and it is worth showing, with an "as of" line."""
    members = _members(group)
    store.store(group["group_id"], members, _fake_odds(members), stage="semifinals")

    db.set_registrant_thp(group["ids"][0], 999_000_000)
    held = store.lookup(group["group_id"], _members(group), stage="semifinals")

    assert held.state == "stale"
    assert held.showable, "a stale answer about the right group is showable with a timestamp"
    assert held.computed_at


def test_a_different_set_of_people_is_never_shown(group, cd_db):
    """Not a staler answer. In a group of eight, one swapped rival moves every row."""
    members = _members(group)
    store.store(group["group_id"], members, _fake_odds(members), stage="semifinals")

    newcomer = db.upsert_registrant(name="Late", server="799", alliance="OGV", thp=400_000_000)
    db.set_stage(newcomer["id"], "semifinals", grp="A", grouping_id=group["grouping"]["id"])

    held = store.lookup(group["group_id"], _members(group), stage="semifinals")

    assert held.state == "missing", (
        "a group with a different member set read as stale rather than missing; "
        "those numbers were computed against somebody else's field"
    )
    assert held.odds is None


def test_a_rename_re_renders_and_does_not_re_run(group):
    """The model never sees a name, so a spelling fix must not cost a run.

    This is the one place the store deliberately behaves differently from the
    in-memory cache, which puts display names in its key. That is right for a
    dict that dies with the process and wrong for a table: paying 60 seconds of
    CPU to relabel a row is paying it for nothing.
    """
    members = _members(group)
    store.store(group["group_id"], members, _fake_odds(members), stage="semifinals")

    with db._get_conn() as conn:
        conn.execute(
            "UPDATE registrants SET display_name = ? WHERE id = ?",
            ("Renamed", group["ids"][0]),
        )

    held = store.lookup(group["group_id"], _members(group), stage="semifinals")

    assert held.state == "fresh", "a rename invalidated the answer; it cannot change a number"
    assert "Renamed" in [r.name for r in held.odds.rows], (
        "the stored answer re-rendered under the old name"
    )


def test_a_lookup_counts_as_a_view(group):
    """The sweeper works in last-viewed order, so the stamp cannot be optional."""
    members = _members(group)
    store.store(group["group_id"], members, _fake_odds(members), stage="semifinals")

    store.lookup(group["group_id"], _members(group), stage="semifinals")

    with db._get_conn() as conn:
        row = conn.execute(
            "SELECT last_viewed_at FROM odds_runs WHERE group_id = ?", (group["group_id"],)
        ).fetchone()
    assert row["last_viewed_at"]


def test_one_group_holds_one_row(group):
    """Rows are replaced in place. The volume never gives blocks back."""
    members = _members(group)
    for _ in range(4):
        store.store(group["group_id"], members, _fake_odds(members), stage="semifinals")

    with db._get_conn() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM odds_runs WHERE group_id = ?", (group["group_id"],)
        ).fetchone()["n"]
    assert n == 1


# ── the sweeper ──────────────────────────────────────────────────────────────


def _later(minutes=10):
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


def test_a_group_nobody_has_computed_is_due(group):
    due = store.due(now=_later())
    assert [c["group_id"] for c in due] == [group["group_id"]]


def test_a_group_still_being_edited_is_left_alone(group):
    """A member filling three squad boxes writes three times.

    Recomputing on the write turns one editing session into eight runs of the
    same group, so the quiet window is the debounce -- and it delays nobody,
    because the press path never waits on the sweeper.
    """
    assert store.due(now=datetime.now(timezone.utc)) == [], (
        "a group written seconds ago was picked up; the quiet window is not holding"
    )
    assert store.due(now=_later()), "and it must come back once the writing stops"


def test_a_computed_group_drops_out_of_the_queue(group):
    members = _members(group)
    store.store(group["group_id"], members, _fake_odds(members), stage="semifinals")
    assert store.due(now=_later()) == []


def test_changed_data_puts_it_back_in(group):
    members = _members(group)
    store.store(group["group_id"], members, _fake_odds(members), stage="semifinals")
    db.set_registrant_thp(group["ids"][0], 999_000_000)

    assert [c["group_id"] for c in store.due(now=_later())] == [group["group_id"]]


def test_the_sweeper_starts_with_what_somebody_actually_looked_at(cd_db):
    """Most groups in a tournament are never opened by anyone."""
    grouping = db.ensure_grouping(WARZONES, "2026-08-04")
    made = []
    for letter in ("A", "B", "C"):
        for i in range(8):
            row = db.upsert_registrant(
                name=f"{letter}{i}", server=WARZONES[i], alliance="OGV", thp=400_000_000
            )
            db.set_stage(row["id"], "semifinals", grp=letter, grouping_id=grouping["id"])
        made.append(db.get_or_create_group(grouping["id"], "semifinals", letter)["id"])

    # B was read this morning; C was read a week ago; A never.
    with db._get_conn() as conn:
        for gid, seen in (
            (made[1], "2026-08-24T09:00:00+00:00"),
            (made[2], "2026-08-17T09:00:00+00:00"),
        ):
            conn.execute(
                "INSERT INTO odds_runs (group_id, stage, fingerprint, member_ids, "
                "payload, computed_at, last_viewed_at) VALUES (?,?,?,?,?,?,?)",
                (gid, "semifinals", "old", json.dumps([]), None, db._now(), seen),
            )

    order = [c["group_id"] for c in store.due(now=_later())]

    assert order[0] == made[1], "the group somebody read this morning should go first"
    assert order.index(made[2]) < order.index(made[0]), "and a stale read beats no read at all"


def test_the_knockout_field_is_visible_to_the_sweeper(cd_db):
    """`db.get_groups` filters `label IS NOT NULL`, and the bracket has no label.

    It is one unlettered field of 32, the most expensive thing in the tournament
    and the single row this sweeper most needs to see -- which is why the store
    queries `groups` itself rather than reaching for that helper.
    """
    grouping = db.ensure_grouping(WARZONES, "2026-08-04")
    for i in range(32):
        row = db.upsert_registrant(
            name=f"K{i:02d}", server=WARZONES[i % 16], alliance="OGV", thp=400_000_000
        )
        db.set_stage(row["id"], "knockouts", grp=None, grouping_id=grouping["id"])

    assert db.get_groups("knockouts", grouping["id"]) == [], (
        "if this helper started returning the knockout field, the store can use it"
    )
    assert [c["stage"] for c in store.due(now=_later())] == ["knockouts"]


def test_a_group_that_cannot_be_modelled_stops_being_picked_up(cd_db):
    """A refusal is cheap to produce and not free to produce every minute.

    The plan said a refusal "never needs storing". It costs nothing to REACH --
    both odds functions refuse before simulating anything -- but not storing it
    means the sweeper picks the same unmodellable group every tick forever and
    never reaches the group behind it.
    """
    grouping = db.ensure_grouping(WARZONES, "2026-08-04")
    for i in range(3):  # three players, and the semifinal model wants eight
        row = db.upsert_registrant(
            name=f"S{i}", server=WARZONES[i], alliance="OGV", thp=400_000_000
        )
        db.set_stage(row["id"], "semifinals", grp="A", grouping_id=grouping["id"])

    due = store.due(now=_later())
    assert len(due) == 1
    assert store.run_one(due[0]) == "refused"

    assert store.due(now=_later()) == [], (
        "the refused group is still in the queue and will be refused again every minute"
    )


def test_the_refusal_is_reconsidered_once_the_data_changes(cd_db):
    grouping = db.ensure_grouping(WARZONES, "2026-08-04")
    ids = []
    for i in range(3):
        row = db.upsert_registrant(
            name=f"S{i}", server=WARZONES[i], alliance="OGV", thp=400_000_000
        )
        db.set_stage(row["id"], "semifinals", grp="A", grouping_id=grouping["id"])
        ids.append(row["id"])
    store.run_one(store.due(now=_later())[0])

    for i in range(3, 8):  # the group fills up
        row = db.upsert_registrant(
            name=f"S{i}", server=WARZONES[i], alliance="OGV", thp=400_000_000
        )
        db.set_stage(row["id"], "semifinals", grp="A", grouping_id=grouping["id"])

    assert store.due(now=_later()), "a refusal outlived the data it was about"


def test_a_swept_group_is_served_from_the_store_afterwards(group, monkeypatch):
    """End to end, through the real engine: sweep it, then read it back.

    The one test here that pays for a simulation, and it is worth it -- it is
    the only one that proves the thing the whole session exists to do.
    """
    monkeypatch.setattr(odds, "USE_SUBPROCESS", False)  # a child buys nothing in a test

    due = store.due(now=_later())
    assert len(due) == 1
    assert store.run_one(due[0]) == "stored"

    held = store.lookup(group["group_id"], _members(group), stage="semifinals")
    assert held.state == "fresh"
    assert len(held.odds.rows) == 8
    assert sum(r.advance for r in held.odds.rows) == pytest.approx(2.0, abs=0.05), (
        "two of eight advance, so the column sums to two"
    )


def test_the_sweeper_stands_aside_for_somebody_who_is_actually_asking(group):
    """A press already running must not wait ninety seconds behind a precompute.

    Advisory rather than airtight -- `compute` takes that same lock a moment
    later and it is not reentrant, so this is a check and not an acquisition.
    What it removes is the common case: a member pressing while the once-a-minute
    sweeper happens to be mid-bracket.
    """
    odds._RUN_LOCK.acquire()
    try:
        due = store.due(now=_later())
        assert len(due) == 1
        assert store.run_one(due[0]) == "deferred"
    finally:
        odds._RUN_LOCK.release()

    assert store.lookup(group["group_id"], _members(group), stage="semifinals").state == "missing"
    assert store.due(now=_later()), "a deferred group must stay due"
