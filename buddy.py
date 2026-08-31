"""Profession Buddy System (#289) — pure pairing logic + Google Sheet I/O.

In Last War an **Engineer** can grant a once-per-24h buff Skill to exactly one
**War Leader**. Alliances pair each War Leader with an Engineer so the buff
always has a home. This module owns:

* the deterministic, stability-first pairing algorithm (`assign_buddies`),
* the leadership change-notification copy (`compose_change_notification`),
* and the I/O for the bot-owned, member-centric "Buddies" tab plus the
  single-cell profession write into the Squad Powers survey tab.

No Discord imports live here — the UI layer (`buddy_ui.py`) drives this module
off the event loop via ``asyncio.to_thread``. Sheet helpers mirror
``train_rotation`` (``_open_tab`` / ``_cell`` / ``_col_letter`` / ``_rewrite``).

Writes go through ``_write_body``, which diffs the rendered tab against what's
there and issues only the rows that moved — the same targeted-write pattern as
``alliance_duel.apply_upsert`` and ``storm_member_rules.delete_rule_at``.
Changing one pairing used to clear and rewrite every row (#289 F-02), which
re-sorted the tab under anything an alliance kept beside it. The full rewrite
is still there as the fallback for a tab the diff won't guess at.

Profession's single source of truth is the Squad Powers tab. The Buddies tab
never *stores* profession — its Profession cells are live-lookup formulas back
into Squad Powers, so a change there auto-reflects. The pairing logic always
reads true profession from Squad Powers via ``read_all_professions``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Canonical profession labels (the Squad Powers survey ships these).
WAR_LEADER = "War Leader"
ENGINEER = "Engineer"

# Member-centric layout of the bot-owned Buddies tab. Three repeated blocks:
# the War Leader (receiver), then up to two Engineer buddies (givers). Every
# person appears exactly once. Headers repeat per block so the sheet reads
# cleanly for leadership.
BUDDY_HEADER = [
    "Discord ID",
    "Name",
    "Profession",  # War Leader
    "Discord ID",
    "Name",
    "Profession",  # Engineer buddy 1
    "Discord ID",
    "Name",
    "Profession",  # Engineer buddy 2 (double pairing)
]


# ── Small helpers ─────────────────────────────────────────────────────────────


def _norm(name: str) -> str:
    """Case/whitespace-insensitive key for matching across surfaces (a synced
    display name vs a hand-typed Sheet cell)."""
    return (name or "").strip().lower()


def _classify(profession: str) -> str | None:
    """Map a profession cell to ``"wl"`` / ``"eng"`` / ``None`` (unknown)."""
    p = (profession or "").strip().lower()
    if p in ("war leader", "warleader", "wl"):
        return "wl"
    if p in ("engineer", "eng"):
        return "eng"
    return None


@dataclass
class Member:
    name: str
    discord_id: str = ""
    profession: str = ""
    power: float = 0.0
    # Engineer reliability (#303): higher = more reliable. Only read/used when
    # the alliance turns on reliability ranking; 0.0 = unranked (sorts last).
    reliability: float = 0.0
    # Opt-out column (#427). False only when the alliance configured an include
    # column and this member's cell reads no/false/0. Never trust this flag on a
    # Member that came off the Buddies tab — that reader can't see the column.
    # `eligible_members` is the only thing that should act on it.
    included: bool = True


@dataclass
class Pair:
    war_leader: str
    wl_discord_id: str
    engineer: str
    eng_discord_id: str
    source: str = "auto"  # "auto" | "manual" — informational; every pair is sticky


# Why a pair that was on the Buddies tab didn't survive validation (#289 F-06).
# Machine keys only — the sentences leadership reads are built in `buddy_ui`, so
# the wording lives in one place and this module stays Discord-free.
DROP_MISSING_WL = "missing_wl"
DROP_MISSING_ENG = "missing_eng"
DROP_PROFESSION_WL = "profession_wl"
DROP_PROFESSION_ENG = "profession_eng"
DROP_ENGINEER_TAKEN = "engineer_taken"
DROP_DOUBLING_OFF = "doubling_off"
DROP_WL_FULL = "wl_full"
DROP_SELF = "self"


# The two reasons an officer can overrule. Both mean "these two pairings can't
# both stand", and which one survives is currently decided alphabetically —
# which is not a decision the bot should be making silently on the alliance's
# behalf. `buddy_ui` offers the choice; everything else is simply reported.
RESOLVABLE_DROPS = (DROP_ENGINEER_TAKEN, DROP_DOUBLING_OFF)


@dataclass
class DroppedPair:
    """A pairing the tab carried that validation refused to keep.

    Names are whatever we could resolve — the tab's spelling when the member
    couldn't be found at all, the roster's when they could. Both are only ever
    rendered back to a human, never matched on.

    ``detail`` carries whatever the sentence needs beyond the two names — for a
    profession change, what they changed *to*.

    ``dropped`` and ``kept`` are the two ``Pair``s a resolvable conflict is
    choosing between: the one that lost and the one that won. Swapping them is
    the whole of resolving it, which is why both are carried rather than just
    the loser."""

    war_leader: str
    engineer: str
    reason: str
    detail: str = ""
    dropped: object = None
    kept: object = None

    @property
    def resolvable(self) -> bool:
        """True when an officer could sensibly pick the other pairing instead."""
        return (
            self.reason in RESOLVABLE_DROPS and self.dropped is not None and self.kept is not None
        )


@dataclass
class PairingResult:
    pairs: list = field(default_factory=list)
    unpaired_wl: list = field(default_factory=list)
    unpaired_eng: list = field(default_factory=list)
    # Pairs the tab carried that didn't survive (#289 F-06). Defaulted so every
    # existing caller and test that builds a PairingResult by hand still works.
    dropped: list = field(default_factory=list)


def _member_key(m: Member) -> str:
    """Identity key: Discord ID when present (robust to renames), else name."""
    did = (m.discord_id or "").strip()
    return did or _norm(m.name)


# Cell values that take a member out of the pool. Anything else (including a
# blank cell, and including a missing column) leaves them in — the opt-out has
# to be deliberate, never the result of a header the alliance hasn't filled in.
_EXCLUDE_VALUES = ("no", "n", "false", "0", "off", "left", "exclude")


def _is_excluded_value(raw: str) -> bool:
    return (raw or "").strip().lower() in _EXCLUDE_VALUES


@dataclass
class RosterIndex:
    """Who the alliance roster tab says is currently here (#428).

    Two sets rather than one key set because identity is tier-dependent: a
    synced (Premium) roster carries Discord IDs, a hand-maintained free one
    carries names only. A member counts as on the roster if *either* matches.

    Falsy when empty, which is load-bearing: an unreadable or misconfigured
    roster must never be treated as "nobody is in the alliance"."""

    ids: set = field(default_factory=set)
    names: set = field(default_factory=set)

    def __bool__(self) -> bool:
        return bool(self.ids or self.names)

    def has(self, m: Member) -> bool:
        did = (m.discord_id or "").strip()
        return (did and did in self.ids) or (_norm(m.name) in self.names)


def build_roster_index(rows: list) -> RosterIndex:
    """``[{"name":…, "discord_id":…}]`` (train_rotation.load_roster_members) →
    RosterIndex."""
    idx = RosterIndex()
    for r in rows or []:
        did = str(r.get("discord_id") or "").strip()
        nm = _norm(r.get("name") or "")
        if did:
            idx.ids.add(did)
        if nm:
            idx.names.add(nm)
    return idx


def eligible_members(
    primary: list[Member],
    fallback: list[Member],
    roster: RosterIndex | None = None,
) -> list[Member]:
    """The single place that decides who is in the buddy pool.

    ``primary`` is the Squad Powers read (authoritative, and the only source
    that can see the opt-out column); ``fallback`` is the buddy-tab read, which
    only ever supplies professions for members Squad Powers can't classify.

    Exclusions are collected from ``primary`` and applied to the *merged* list.
    Doing it here rather than inside either reader is what stops an opted-out
    member from being resurrected by their leftover Buddies-tab row: that row
    carries a position-implied profession, so ``merge_members`` would otherwise
    keep it whenever the Squad Powers row has no classifiable profession.

    ``roster`` (#428) additionally restricts the pool to people on the alliance
    roster tab, so a departure drops out with no sheet editing at all. **An
    empty or None roster skips the intersect entirely** rather than emptying
    the pool: ``load_roster_members`` returns [] on a renamed tab, revoked
    access or any read failure, and silently un-pairing an entire alliance over
    a transient Sheets error is far worse than briefly keeping a leaver.
    """
    excluded = {_member_key(m) for m in primary if not m.included}
    merged = merge_members(primary, fallback)
    if excluded:
        merged = [m for m in merged if _member_key(m) not in excluded]
    if not roster:
        return merged
    return [m for m in merged if roster.has(m)]


def members_missing_from_roster(primary: list[Member], roster: RosterIndex | None) -> list[str]:
    """Names on the profession tab, with a real profession, that the roster
    doesn't know about — sorted.

    These are the people the roster intersect silently removes, and on the free
    tier a typo in the roster tab is enough to cause it. Surfacing the count is
    what keeps a matching problem from looking like the bot lost members."""
    if not roster:
        return []
    missing = [
        m.name for m in primary if _classify(m.profession) and m.included and not roster.has(m)
    ]
    return sorted({n for n in missing if n}, key=_norm)


def read_roster_index(guild_id: int) -> RosterIndex:
    """Read the alliance roster tab → RosterIndex. Empty on any failure.

    Delegates to ``train_rotation.load_roster_members``, the same public reader
    Conductor Rotation uses, so both features agree on who is in the alliance
    and both get the free/Premium tier split (#337) for free: a synced roster
    yields Discord IDs, a hand-maintained one yields names only. Hand-typed
    non-Discord rows come along because that reader takes every row on the tab.
    """
    try:
        from train_rotation import load_roster_members

        return build_roster_index(load_roster_members(guild_id))
    except Exception as e:
        print(f"[BUDDY] read_roster_index failed for guild {guild_id}: {e}")
        return RosterIndex()


# ── Pairing algorithm ─────────────────────────────────────────────────────────


def assign_buddies(
    members: list,
    existing_pairs: list,
    *,
    engineer_doubling: bool = False,
    wl_priority: str = "name",
    eng_priority: str = "name",
    fill: bool = True,
    fill_only: set | None = None,
) -> PairingResult:
    """Stability-first 1:1 pairing of War Leaders and Engineers.

    Every *valid* existing pair is preserved (people keep their buddy); only
    currently-free members are placed. An Engineer is in at most one pair; a War
    Leader receives from two Engineers only when ``engineer_doubling`` is on
    (never two War Leaders to one Engineer).

    ``wl_priority``:
      * ``"name"`` (default) — free War Leaders take Engineers in name order.
      * ``"power"`` — strongest free War Leaders take scarce Engineers first
        (weaker fall to ``unpaired_wl``). Subordinate to stability — an
        established pair is never broken for a stronger newcomer.

    ``eng_priority``:
      * ``"name"`` (default) — free Engineers are offered in name order.
      * ``"reliability"`` — free Engineers are offered most-reliable first
        (ties broken by name). With ``wl_priority="power"`` this lands the most
        reliable Engineers on the strongest War Leaders (#303).

    ``fill=False`` validates and preserves ``existing_pairs`` and computes the
    free pools, but creates **no** new pairings — used by the manual editor so
    an "unpair" isn't instantly auto-refilled.

    ``fill_only`` narrows gap-filling to the given member keys (#289 F-08).
    Someone setting their own profession should come away with a buddy, not
    quietly pair up everyone else in the alliance; passing just their key makes
    the fill place them and leave every other gap alone.

    Pairings the tab carried that don't survive validation come back in
    ``result.dropped`` with a reason each, rather than vanishing (#289 F-06).

    Pure and deterministic: identical input → identical output; feeding the
    result's pairs back as ``existing_pairs`` produces zero churn.
    """
    cap = 2 if engineer_doubling else 1

    # Dedup members by identity key (first wins) and index for pair resolution.
    by_id: dict[str, Member] = {}
    by_name: dict[str, Member] = {}
    seen: set[str] = set()
    deduped: list[Member] = []
    for m in members:
        k = _member_key(m)
        if not k or k in seen:
            continue
        seen.add(k)
        deduped.append(m)
        did = (m.discord_id or "").strip()
        if did:
            by_id[did] = m
        nm = _norm(m.name)
        if nm:
            by_name.setdefault(nm, m)

    def resolve(discord_id: str, name: str) -> Member | None:
        did = (discord_id or "").strip()
        if did and did in by_id:
            return by_id[did]
        nm = _norm(name)
        if nm and nm in by_name:
            return by_name[nm]
        return None

    kept_pairs: list[Pair] = []
    dropped: list[DroppedPair] = []
    eng_used: set[str] = set()
    wl_load: dict[str, int] = {}

    # Step 2 — validate & preserve existing pairs. Every rejection is recorded
    # with its reason: silently dropping a pairing is what made this look like
    # the bot losing people's buddies (#289 F-05, F-06).
    # Which kept pairing currently holds each Engineer, and which kept pairings
    # each War Leader has. A conflict needs both sides to offer a choice
    # between them (#289 F-04, F-05).
    eng_owner: dict[str, Pair] = {}
    wl_holds: dict[str, list] = {}

    for p in existing_pairs:
        wl = resolve(p.wl_discord_id, p.war_leader)
        eng = resolve(p.eng_discord_id, p.engineer)
        if not (wl and eng):
            dropped.append(
                DroppedPair(
                    p.war_leader,
                    p.engineer,
                    DROP_MISSING_WL if not wl else DROP_MISSING_ENG,
                )
            )
            continue
        if _classify(wl.profession) != "wl":
            dropped.append(DroppedPair(wl.name, eng.name, DROP_PROFESSION_WL, detail=wl.profession))
            continue
        if _classify(eng.profession) != "eng":
            dropped.append(
                DroppedPair(wl.name, eng.name, DROP_PROFESSION_ENG, detail=eng.profession)
            )
            continue
        wk, ek = _member_key(wl), _member_key(eng)
        this = Pair(wl.name, wl.discord_id, eng.name, eng.discord_id, source=p.source)
        if wk == ek:
            dropped.append(DroppedPair(wl.name, eng.name, DROP_SELF, dropped=this))
            continue
        if ek in eng_used:
            # This Engineer already belongs to another War Leader. Which of the
            # two keeps them is the alliance's call, not ours.
            dropped.append(
                DroppedPair(
                    wl.name, eng.name, DROP_ENGINEER_TAKEN, dropped=this, kept=eng_owner.get(ek)
                )
            )
            continue
        if wl_load.get(wk, 0) >= cap:
            # cap == 1 means doubling is switched off, which is the actionable
            # case: the alliance can turn it on and keep this pairing, or pick
            # which of the two Engineers this War Leader keeps.
            held = wl_holds.get(wk) or []
            dropped.append(
                DroppedPair(
                    wl.name,
                    eng.name,
                    DROP_DOUBLING_OFF if cap == 1 else DROP_WL_FULL,
                    dropped=this,
                    kept=held[0] if (cap == 1 and held) else None,
                )
            )
            continue
        kept_pairs.append(this)
        eng_used.add(ek)
        eng_owner[ek] = this
        wl_holds.setdefault(wk, []).append(this)
        wl_load[wk] = wl_load.get(wk, 0) + 1

    # Step 3 — free pools.
    all_wl = [m for m in deduped if _classify(m.profession) == "wl"]
    all_eng = [m for m in deduped if _classify(m.profession) == "eng"]

    def wl_sort_key(m: Member):
        if wl_priority == "power":
            return (-float(m.power or 0), _norm(m.name))
        return (_norm(m.name),)

    def eng_sort_key(m: Member):
        if eng_priority == "reliability":
            # Most reliable first; alphabetical within a reliability tier.
            return (-float(m.reliability or 0), _norm(m.name))
        return (_norm(m.name),)

    unpaired_wl_pool = sorted(
        [m for m in all_wl if wl_load.get(_member_key(m), 0) == 0], key=wl_sort_key
    )
    free_eng = sorted([m for m in all_eng if _member_key(m) not in eng_used], key=eng_sort_key)

    new_pairs: list[Pair] = []

    if not fill:
        return PairingResult(
            pairs=list(kept_pairs),
            unpaired_wl=list(unpaired_wl_pool),
            unpaired_eng=list(free_eng),
            dropped=dropped,
        )

    def _wants(m: Member) -> bool:
        """Whether this member is allowed to pick up a new pairing."""
        return fill_only is None or _member_key(m) in fill_only

    def _bind(wl: Member, eng: Member) -> None:
        new_pairs.append(Pair(wl.name, wl.discord_id, eng.name, eng.discord_id, "auto"))
        wl_load[_member_key(wl)] = wl_load.get(_member_key(wl), 0) + 1
        eng_used.add(_member_key(eng))

    # Step 4 — base 1:1 fill (give every unpaired WL an Engineer before doubling).
    for wl in [m for m in unpaired_wl_pool if _wants(m)]:
        if not free_eng:
            break
        eng = free_eng.pop(0)
        unpaired_wl_pool.remove(wl)
        _bind(wl, eng)

    # A named Engineer with no free War Leader left still needs a home — attach
    # them to whoever has capacity. Only runs under `fill_only`; a general fill
    # reaches the same members through step 5.
    if fill_only is not None:
        for eng in [m for m in free_eng if _wants(m)]:
            candidates = [m for m in all_wl if wl_load.get(_member_key(m), 0) < cap]
            if not candidates:
                break
            candidates.sort(key=lambda m: (wl_load.get(_member_key(m), 0), _norm(m.name)))
            free_eng.remove(eng)
            _bind(candidates[0], eng)

    # Step 5 — Engineer doubling: leftover Engineers attach to the least-loaded
    # War Leader (cap 2). War Leaders are never doubled.
    if fill_only is None and engineer_doubling and free_eng:
        while free_eng:
            candidates = [m for m in all_wl if wl_load.get(_member_key(m), 0) < 2]
            if not candidates:
                break
            candidates.sort(key=lambda m: (wl_load.get(_member_key(m), 0), _norm(m.name)))
            _bind(candidates[0], free_eng.pop(0))

    return PairingResult(
        pairs=kept_pairs + new_pairs,
        unpaired_wl=list(unpaired_wl_pool),
        unpaired_eng=list(free_eng),
        dropped=dropped,
    )


# ── Change notification ───────────────────────────────────────────────────────


def _join_and(names: list[str]) -> str:
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) == 2:
        return f"{names[0]} and {names[1]}"
    return ", ".join(names[:-1]) + f", and {names[-1]}"


def _display_map(*results: PairingResult) -> dict[str, str]:
    m: dict[str, str] = {}
    for res in results:
        for p in res.pairs:
            m.setdefault(_norm(p.war_leader), p.war_leader)
            m.setdefault(_norm(p.engineer), p.engineer)
        for mem in list(res.unpaired_wl) + list(res.unpaired_eng):
            m.setdefault(_norm(mem.name), mem.name)
    return m


def _buddies_of(result: PairingResult, actor_norm: str) -> list[str]:
    out: list[str] = []
    for p in result.pairs:
        if _norm(p.war_leader) == actor_norm:
            out.append(p.engineer)
        elif _norm(p.engineer) == actor_norm:
            out.append(p.war_leader)
    return out


def compose_change_notification(
    actor: str, new_profession: str, before: PairingResult, after: PairingResult
) -> str:
    """Leadership note for a profession change, built from the before→after diff.

    Examples (the two cases that drove the design):
      "Alice changed profession to War Leader. Alice is now paired with Chloe.
       Bill currently has no assigned buddy."
      "Alice changed profession to Engineer. Alice and Bill currently have no
       assigned buddy."
    """
    actor_norm = _norm(actor)
    display = _display_map(after, before)
    display.setdefault(actor_norm, actor)

    before_paired: set[str] = set()
    for p in before.pairs:
        before_paired.add(_norm(p.war_leader))
        before_paired.add(_norm(p.engineer))
    after_unpaired = {_norm(m.name) for m in list(after.unpaired_wl) + list(after.unpaired_eng)}
    newly_unpaired = after_unpaired & before_paired

    clauses = [f"{actor} changed profession to {new_profession}."]
    actor_buddies = _buddies_of(after, actor_norm)
    others = sorted(n for n in newly_unpaired if n != actor_norm)
    others_display = [display.get(n, n) for n in others]

    if actor_buddies:
        clauses.append(f"{actor} is now paired with {_join_and(actor_buddies)}.")
        for od in others_display:
            clauses.append(f"{od} currently has no assigned buddy.")
    else:
        group = [actor] + others_display
        verb = "has" if len(group) == 1 else "have"
        clauses.append(f"{_join_and(group)} currently {verb} no assigned buddy.")

    return " ".join(clauses)


# ══════════════════════════════════════════════════════════════════════════════
# Google Sheet I/O
# ══════════════════════════════════════════════════════════════════════════════


def _open_tab(guild_id: int, tab_name: str, header: list[str]):
    """Return the worksheet for ``tab_name``, creating it with ``header`` if
    absent. Returns None when the guild has no Sheet configured or gspread
    errored (callers degrade gracefully)."""
    import config

    if not tab_name:
        return None
    try:
        sh = config.get_spreadsheet(guild_id)
    except Exception as e:
        print(f"[BUDDY] get_spreadsheet failed for guild {guild_id}: {e}")
        return None
    if sh is None:
        return None
    try:
        return config.get_or_create_worksheet(
            sh, tab_name, header_row=header, rows=2000, cols=max(9, len(header))
        )
    except Exception as e:
        print(f"[BUDDY] open/create tab {tab_name!r} failed for guild {guild_id}: {e}")
        return None


def _cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if 0 <= idx < len(row) else ""


def _col_letter(n: int) -> str:
    """1-based column index → spreadsheet letter (1→A, 26→Z, 27→AA)."""
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


# Rows of headroom the full rewrite clears past the longer of old/new body.
# Enough to catch a stray row someone left just under the list; nowhere near
# enough to reach content parked further down the tab.
_CLEAR_MARGIN = 20


def _rewrite(
    ws,
    header: list[str],
    body_rows: list[list[str]],
    guild_id: int,
    tab_name: str,
    current_rows: int = 0,
) -> bool:
    """Clear the tab below the header and write ``body_rows`` in one batch.
    One ``update`` after one ``batch_clear`` stays well under the Sheets
    60-writes/min quota. ``USER_ENTERED`` so Profession formulas evaluate.

    The clear reaches to whichever is longer — what's being written or what's
    already there — plus a small margin. It used to reach 5,000 rows past the
    data, which took anything an alliance had parked below the list with it.

    This is the fallback path. ``_write_body`` is what callers go through, and
    it only lands here when the tab can't be safely patched in place."""
    extent = max(len(body_rows), current_rows) + _CLEAR_MARGIN
    try:
        ws.batch_clear([f"A2:{_col_letter(len(header))}{extent + 1}"])
        if body_rows:
            ws.update("A2", body_rows, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print(f"[BUDDY] rewrite of {tab_name!r} failed for guild {guild_id}: {e}")
        return False


# Columns the diff compares. The Profession cells (C, F, I) are deliberately
# excluded: they hold live-lookup formulas, and `get_all_values` hands back
# what those evaluated to rather than the formula text, so comparing them would
# mark every row changed on every write. A row's formulas are rewritten
# whenever its ID/Name cells move, which is the only time they need to change.
_IDENTITY_COLS = (0, 1, 3, 4, 6, 7)

# Ceiling on row insert/delete operations before a full rewrite is cheaper.
# Each one is its own Sheets call; `alliance_duel.apply_upsert` carries the scar
# from a version that blew the 60/min write quota looping over single-row calls.
_MAX_ROW_OPS = 8


def _row_identity(row: list[str]) -> tuple:
    """The ID/Name cells of one row, for spotting which rows actually changed."""
    return tuple(_cell(row, i) for i in _IDENTITY_COLS)


def _row_key(row: list[str]) -> tuple:
    """Who a Buddies-tab row belongs to, for matching it across a rewrite.

    The War Leader in the left block owns the row; when that block is blank the
    row belongs to the unpaired Engineer in the middle block. Keyed on Discord
    ID when there is one so a rename doesn't read as a different row, else on
    the normalised name. A row carrying nobody returns ``()`` and never
    matches — blank padding must not pair up with real data."""
    did, name = _cell(row, 0), _cell(row, 1)
    if did or name:
        return ("wl", did or _norm(name))
    did, name = _cell(row, 3), _cell(row, 4)
    if did or name:
        return ("eng", did or _norm(name))
    return ()


def _diff_ops(current: list[list[str]], target: list[list[str]]):
    """Plan the smallest set of operations that turns ``current`` into ``target``.

    Returns ``(deletes, inserts, updates)`` in final 1-based sheet row numbers,
    or **None** when the tab isn't in a shape this can safely patch and the
    caller should fall back to a full rewrite.

    Both sides are keyed by `_row_key` and both are written in the same sorted
    order, so the plan is a straight three-way split rather than a general diff:
    keys only on the tab are deleted, keys only in the new body are inserted,
    and keys on both sides get their cells updated when they differ.

    Two things make it return None rather than guess:

    * a duplicate or empty key on either side — the key stops being an identity
      and rows could be matched to the wrong person;
    * shared keys sitting in a different relative order on the two sides, which
      means the tab has been reordered by hand and patching it in place would
      scatter rows rather than tidy them.
    """
    cur_keys = [_row_key(r) for r in current]
    tgt_keys = [_row_key(r) for r in target]
    if not all(cur_keys) or not all(tgt_keys):
        return None
    if len(set(cur_keys)) != len(cur_keys) or len(set(tgt_keys)) != len(tgt_keys):
        return None

    cur_set, tgt_set = set(cur_keys), set(tgt_keys)
    shared = cur_set & tgt_set
    if [k for k in cur_keys if k in shared] != [k for k in tgt_keys if k in shared]:
        return None

    # Sheet rows are 1-based and row 1 is the header, so body index i is row i+2.
    deletes = sorted(
        (i + 2 for i, k in enumerate(cur_keys) if k not in tgt_set),
        reverse=True,
    )
    cur_by_key = {k: current[i] for i, k in enumerate(cur_keys)}
    inserts: list[tuple[int, list[str]]] = []
    updates: list[tuple[int, list[str]]] = []
    for i, k in enumerate(tgt_keys):
        rownum = i + 2
        if k not in cur_set:
            inserts.append((rownum, target[i]))
        elif _row_identity(cur_by_key[k]) != _row_identity(target[i]):
            updates.append((rownum, target[i]))

    if len(deletes) + len(inserts) > _MAX_ROW_OPS:
        return None
    return deletes, inserts, updates


def _runs(rows: list[int]) -> list[tuple[int, int]]:
    """Collapse sorted-descending row numbers into ``(start, end)`` runs so a
    block of adjacent deletions costs one Sheets call instead of one each."""
    out: list[tuple[int, int]] = []
    for n in rows:
        if out and out[-1][0] == n + 1:
            out[-1] = (n, out[-1][1])
        else:
            out.append((n, n))
    return out


def _write_body(
    ws, header: list[str], body_rows: list[list[str]], guild_id: int, tab_name: str
) -> bool:
    """Bring the tab's body to ``body_rows``, touching as little as possible.

    Changing one pairing used to clear and rewrite every row, which re-sorted
    the tab under anything an alliance had written beside it and cost a full
    rewrite for a two-cell change (#289 F-02). This diffs first and issues only
    what actually moved, following the same targeted-write pattern as
    `alliance_duel.apply_upsert` and `storm_member_rules.delete_rule_at`.

    Real row inserts and deletes are used rather than a rewrite, so a row keeps
    the cells an alliance added to the right of the list, and the Profession
    formulas have their row references adjusted by Sheets rather than by us.

    Falls back to the full rewrite on an unreadable tab, a shape the diff won't
    guess at, or a change big enough that patching costs more calls than
    rewriting. That fallback is the old behaviour, so this is never worse than
    a rewrite — usually it's a single call, and when nothing moved, none.

    Buddies-tab only: `_row_key` reads the three-block layout to work out who
    owns a row. Other tabs (the preset tab) go straight to `_rewrite`."""
    try:
        current = [list(r) for r in ws.get_all_values()[1:]]
    except Exception as e:
        print(f"[BUDDY] diff read of {tab_name!r} failed for guild {guild_id}: {e}")
        return _rewrite(ws, header, body_rows, guild_id, tab_name)

    # Trailing blank rows are padding the sheet keeps, not rows we wrote.
    while current and not any((c or "").strip() for c in current[-1]):
        current.pop()

    # Nothing on the tab yet: one write beats one insert per row.
    if not current:
        return _rewrite(ws, header, body_rows, guild_id, tab_name)

    plan = _diff_ops(current, body_rows)
    if plan is None:
        return _rewrite(ws, header, body_rows, guild_id, tab_name, current_rows=len(current))
    deletes, inserts, updates = plan
    if not (deletes or inserts or updates):
        return True

    last_col = _col_letter(len(header))
    try:
        # Deletions run bottom-up so earlier row numbers stay valid; insertions
        # then run top-down, each landing on the row number it ends up at.
        for start, end in _runs(deletes):
            ws.delete_rows(start, end)
        for rownum, row in inserts:
            ws.insert_row(row, rownum, value_input_option="USER_ENTERED")
        if updates:
            ws.batch_update(
                [
                    {"range": f"A{rownum}:{last_col}{rownum}", "values": [row]}
                    for rownum, row in updates
                ],
                value_input_option="USER_ENTERED",
            )
        return True
    except Exception as e:
        # A partial write is worse than either outcome, so rebuild the tab
        # outright rather than leaving it half-patched.
        print(f"[BUDDY] targeted write of {tab_name!r} failed for guild {guild_id}: {e}")
        return _rewrite(ws, header, body_rows, guild_id, tab_name, current_rows=len(current))


def load_pairs(guild_id: int, buddy_tab: str) -> list:
    """Parse the member-centric Buddies tab back into Engineer→War-Leader links.

    Left block (A–C) is the War Leader; D–F and G–I are Engineer buddies. A row
    with a blank left block carries an unpaired Engineer (no link). Profession
    display cells are ignored — real profession comes from Squad Powers.
    Returns [] on any read failure."""
    ws = _open_tab(guild_id, buddy_tab, BUDDY_HEADER)
    if ws is None:
        return []
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[BUDDY] load_pairs read failed for guild {guild_id}: {e}")
        return []
    out: list[Pair] = []
    for row in values[1:]:
        wl_id, wl_name = _cell(row, 0), _cell(row, 1)
        if not (wl_id or wl_name):
            continue  # unpaired-Engineer row or blank line
        for e_id_idx, e_name_idx in ((3, 4), (6, 7)):
            e_id, e_name = _cell(row, e_id_idx), _cell(row, e_name_idx)
            if e_id or e_name:
                out.append(Pair(wl_name, wl_id, e_name, e_id, "auto"))
    return out


def read_members_from_buddy_tab(guild_id: int, buddy_tab: str) -> list:
    """Read the Buddies tab and return Members with a *position-implied*
    profession (left block → War Leader, middle/right → Engineer), or the
    block's Profession cell when it holds a real value.

    This lets an alliance that already maintains a buddy list bootstrap the
    feature with no survey data: their existing rows imply who's a War Leader
    and who's an Engineer. Squad Powers stays authoritative — these are only a
    fallback, merged under it by ``merge_members``. Returns [] on read failure.
    """
    ws = _open_tab(guild_id, buddy_tab, BUDDY_HEADER)
    if ws is None:
        return []
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[BUDDY] read_members_from_buddy_tab read failed for guild {guild_id}: {e}")
        return []
    out: list[Member] = []
    # (id_col, name_col, prof_col, implied_profession)
    blocks = ((0, 1, 2, WAR_LEADER), (3, 4, 5, ENGINEER), (6, 7, 8, ENGINEER))
    for row in values[1:]:
        for id_i, name_i, prof_i, implied in blocks:
            did, nm, prof = _cell(row, id_i), _cell(row, name_i), _cell(row, prof_i)
            if not (did or nm):
                continue
            profession = prof if _classify(prof) else implied
            out.append(Member(name=nm, discord_id=did, profession=profession))
    return out


def merge_members(primary: list, fallback: list) -> list:
    """Merge two member lists by identity key. ``primary`` (Squad Powers) wins
    whenever it carries a classifiable profession or the member is absent from
    ``fallback``; otherwise ``fallback`` (buddy-tab-implied) fills the gap.

    Keeps Squad Powers as the source of truth while letting an imported buddy
    list supply professions for members who haven't been surveyed yet."""
    by_key: dict[str, Member] = {}
    for m in fallback:
        k = _member_key(m)
        if k:
            by_key[k] = m
    for m in primary:
        k = _member_key(m)
        if not k:
            continue
        if _classify(m.profession) is not None or k not in by_key:
            by_key[k] = m
    return list(by_key.values())


def _result_names(result: PairingResult) -> list[str]:
    """Every member name a pairing result carries, paired or not."""
    names: list[str] = []
    for p in result.pairs:
        names.append(p.war_leader)
        names.append(p.engineer)
    for m in list(result.unpaired_wl) + list(result.unpaired_eng):
        names.append(m.name)
    return [n for n in names if n]


def names_dropped_by(result: PairingResult, current: list[Member]) -> list[str]:
    """Names on the Buddies tab that ``result`` no longer carries, sorted.

    Used to tell leadership who a from-scratch rebuild would remove before they
    confirm it (#427). Matched on normalised name rather than identity key
    because the point is to render names back to a human, and a departed member
    may well have no Discord ID on either surface."""
    kept = {_norm(n) for n in _result_names(result)}
    dropped: list[str] = []
    seen: set[str] = set()
    for m in current:
        key = _norm(m.name)
        if not key or key in kept or key in seen:
            continue
        seen.add(key)
        dropped.append(m.name)
    return sorted(dropped, key=_norm)


def _resolve_profession_columns(guild_id: int, profession_tab: str, profession_col_header: str):
    """Read the Squad Powers header and return ``(username_letter, id_letter,
    prof_letter)`` for building live-lookup formulas, or None when the header
    can't be resolved (caller falls back to static profession values)."""
    import config

    try:
        sh = config.get_spreadsheet(guild_id)
        ws = sh.worksheet(profession_tab)
        values = ws.get_all_values()
    except Exception:
        return None
    header = [h.strip().lower() for h in (values[0] if values else [])]
    if not header:
        return None

    def find(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return -1

    id_idx = find("discord id")
    user_idx = find("username", "name")
    prof_idx = find((profession_col_header or "profession").strip().lower())
    if prof_idx < 0 or id_idx < 0:
        return None
    if user_idx < 0:
        user_idx = 0
    return (_col_letter(user_idx + 1), _col_letter(id_idx + 1), _col_letter(prof_idx + 1))


def _prof_cell(id_col_letter, rownum, discord_id, name, cols, profession_tab, static_value):
    """A Profession cell value: a live-lookup formula against Squad Powers when
    the columns resolved, else the static position-implied profession."""
    if cols is None:
        return static_value
    username_letter, id_letter, prof_letter = cols
    tab = (profession_tab or "").replace("'", "''")
    if (discord_id or "").strip():
        ref = f"{id_col_letter}{rownum}"
        return (
            f"=IFERROR(INDEX('{tab}'!${prof_letter}:${prof_letter}, "
            f"MATCH({ref}, '{tab}'!${id_letter}:${id_letter}, 0)), \"\")"
        )
    # No Discord ID → match by name against the Username column.
    name_col_letter = _col_letter(_col_letter_to_index(id_col_letter) + 2)
    ref = f"{name_col_letter}{rownum}"
    return (
        f"=IFERROR(INDEX('{tab}'!${prof_letter}:${prof_letter}, "
        f"MATCH({ref}, '{tab}'!${username_letter}:${username_letter}, 0)), \"\")"
    )


def _col_letter_to_index(letter: str) -> int:
    """Single-letter column → 1-based index (A→1). Used to derive the Name cell
    (one column right of the ID cell) for the name-match formula fallback."""
    return ord(letter.strip().upper()[:1]) - ord("A") + 1 if letter else 1


def save_pairs(
    guild_id: int,
    buddy_tab: str,
    result: PairingResult,
    profession_tab: str,
    profession_col_header: str,
) -> bool:
    """Render a ``PairingResult`` to the member-centric Buddies tab.

    War-Leader rows first (sorted by name), each with their 0–2 Engineers, then
    unpaired-Engineer rows in the middle (D–F) block with a blank left block.
    ID + Name are written as values; Profession cells as live-lookup formulas
    (or static values when the Squad Powers columns can't be resolved).

    The alphabetical order is kept — it's what makes the tab readable — but
    getting there no longer means rewriting it. `_write_body` diffs this
    rendering against the tab and writes only the rows that moved."""
    ws = _open_tab(guild_id, buddy_tab, BUDDY_HEADER)
    if ws is None:
        return False
    cols = _resolve_profession_columns(guild_id, profession_tab, profession_col_header)

    # Group pairs by War Leader.
    wl_order: list[str] = []
    wl_info: dict[str, tuple[str, str]] = {}
    wl_engs: dict[str, list[tuple[str, str]]] = {}

    def _add_wl(name, did):
        k = (did or "").strip() or _norm(name)
        if k not in wl_engs:
            wl_order.append(k)
            wl_engs[k] = []
            wl_info[k] = (name, did)
        return k

    for p in result.pairs:
        k = _add_wl(p.war_leader, p.wl_discord_id)
        wl_engs[k].append((p.engineer, p.eng_discord_id))
    for m in result.unpaired_wl:
        _add_wl(m.name, m.discord_id)

    wl_order.sort(key=lambda k: _norm(wl_info[k][0]))

    body: list[list[str]] = []
    rownum = 2
    for k in wl_order:
        name, did = wl_info[k]
        engs = wl_engs[k][:2]
        row = [did, name, _prof_cell("A", rownum, did, name, cols, profession_tab, WAR_LEADER)]
        for slot, id_letter in ((0, "D"), (1, "G")):
            if slot < len(engs):
                e_name, e_id = engs[slot]
                row += [
                    e_id,
                    e_name,
                    _prof_cell(id_letter, rownum, e_id, e_name, cols, profession_tab, ENGINEER),
                ]
            else:
                row += ["", "", ""]
        body.append(row)
        rownum += 1

    for m in sorted(result.unpaired_eng, key=lambda x: _norm(x.name)):
        body.append(
            [
                "",
                "",
                "",
                m.discord_id,
                m.name,
                _prof_cell("D", rownum, m.discord_id, m.name, cols, profession_tab, ENGINEER),
                "",
                "",
                "",
            ]
        )
        rownum += 1

    return _write_body(ws, BUDDY_HEADER, body, guild_id, buddy_tab)


def read_all_professions(
    guild_id: int,
    profession_tab: str,
    profession_col_header: str,
    include_col_header: str = "",
) -> list:
    """Read the Squad Powers tab → list[Member] with true professions.

    Columns are located by header (case-insensitive) so a reordered survey
    still works. Returns [] when the tab is missing or unreadable.

    ``include_col_header`` (#427) optionally names an opt-out column. A member
    whose cell reads no/false/0 comes back with ``included=False``; everyone
    else, including every row when the header is blank or not found on the tab,
    comes back included. Acting on the flag is ``eligible_members``' job."""
    import config

    if not profession_tab:
        return []
    try:
        sh = config.get_spreadsheet(guild_id)
        ws = sh.worksheet(profession_tab)
        values = ws.get_all_values()
    except Exception as e:
        print(f"[BUDDY] read_all_professions failed for guild {guild_id}: {e}")
        return []
    if not values:
        return []
    header = [h.strip().lower() for h in values[0]]

    def find(*names):
        for n in names:
            if n in header:
                return header.index(n)
        return -1

    id_idx = find("discord id")
    name_idx = find("username", "name")
    prof_idx = find((profession_col_header or "profession").strip().lower())
    # -1 when the alliance hasn't configured an opt-out column, or configured
    # one whose header isn't on the tab. Both mean "no exclusions".
    inc_idx = find(include_col_header.strip().lower()) if (include_col_header or "").strip() else -1

    out: list[Member] = []
    for row in values[1:]:
        did = _cell(row, id_idx) if id_idx >= 0 else ""
        nm = _cell(row, name_idx) if name_idx >= 0 else ""
        prof = _cell(row, prof_idx) if prof_idx >= 0 else ""
        if not (did or nm):
            continue
        included = not _is_excluded_value(_cell(row, inc_idx)) if inc_idx >= 0 else True
        out.append(Member(name=nm, discord_id=did, profession=prof, included=included))
    return out


def read_power_for_members(guild_id: int, members: list) -> None:
    """In-place: set ``Member.power`` from the alliance's Power Data Source.

    Reuses the storm roster builder's cross-tab power index. Only needed for
    ``scarcity_priority == 'strongest_first'``. Any failure leaves power at 0.0
    (the member sinks to the bottom of the strongest-first order)."""
    try:
        import config
        from storm_roster_builder import _build_cross_tab_power_index, _lookup_power_in_index

        scfg = config.get_storm_config(guild_id, "DS")
        rcfg = config.get_member_roster_config(guild_id)
        tab = (scfg.get("power_metric_tab") or "").strip() or (
            rcfg.get("tab_name") or "Member Roster"
        )
        power_col = config.power_column_letter_to_index(scfg.get("power_metric_column") or "B")
        match_letter = (scfg.get("power_match_column") or "").strip()
        match_col = (
            config.power_column_letter_to_index(match_letter)
            if match_letter
            else int(rcfg.get("discord_id_col", 0))
        )
        by_id, by_name, _errs = _build_cross_tab_power_index(guild_id, tab, power_col, match_col)
        for m in members:
            val = _lookup_power_in_index(
                {"discord_id": m.discord_id, "name": m.name}, by_id, by_name
            )
            m.power = float(val or 0)
    except Exception as e:
        print(f"[BUDDY] power read failed for guild {guild_id}: {e}")


def read_reliability_for_members(guild_id: int, members: list) -> None:
    """In-place: set ``Member.reliability`` from the alliance's configured
    reliability column (#303).

    Mirrors ``read_power_for_members`` — reads the buddy-config reliability
    tab + column (a 1-5 number, higher = more reliable) and matches members with
    the same Power Data Source match column power reading uses. Only needed when
    ``reliability_enabled`` is on. Any failure (or a blank/non-numeric cell)
    leaves reliability at 0.0 — the engineer sinks to the bottom of their tier
    order."""
    try:
        import config
        from storm_roster_builder import _build_cross_tab_power_index, _lookup_power_in_index

        bcfg = config.get_buddy_config(guild_id)
        tab = (bcfg.get("reliability_tab") or "").strip()
        col_letter = (bcfg.get("reliability_column") or "").strip()
        if not tab or not col_letter:
            return
        rel_col = config.power_column_letter_to_index(col_letter)
        # Match members the same way power reading does — reuse the alliance's
        # Power Data Source match column, falling back to the roster's Discord ID
        # column. No separate per-buddy match setting (#303).
        scfg = config.get_storm_config(guild_id, "DS")
        rcfg = config.get_member_roster_config(guild_id)
        match_letter = (scfg.get("power_match_column") or "").strip()
        match_col = (
            config.power_column_letter_to_index(match_letter)
            if match_letter
            else int(rcfg.get("discord_id_col", 0))
        )
        by_id, by_name, _errs = _build_cross_tab_power_index(guild_id, tab, rel_col, match_col)
        for m in members:
            val = _lookup_power_in_index(
                {"discord_id": m.discord_id, "name": m.name}, by_id, by_name
            )
            m.reliability = float(val or 0)
    except Exception as e:
        print(f"[BUDDY] reliability read failed for guild {guild_id}: {e}")


def write_profession_cell(
    guild_id: int,
    profession_tab: str,
    profession_col_header: str,
    discord_id: str,
    username: str,
    profession: str,
) -> bool:
    """Single-cell write of a member's profession into the Squad Powers tab.

    Finds the member's row by Discord ID and updates exactly one cell (squad
    power numbers untouched); appends a bare row when the member has no row yet.
    This is the deliberate anti-clobber alternative to survey.update_squad_powers
    (which rewrites the whole row)."""
    import config

    try:
        sh = config.get_spreadsheet(guild_id)
        ws = config.get_or_create_worksheet(
            sh, profession_tab, header_row=["Username", "Discord ID", profession_col_header]
        )
        values = ws.get_all_values()
    except Exception as e:
        print(f"[BUDDY] profession write open failed for guild {guild_id}: {e}")
        return False

    header = values[0] if values else []
    lower = [h.strip().lower() for h in header]

    def find(*names):
        for n in names:
            if n in lower:
                return lower.index(n)
        return -1

    if not header:
        header = ["Username", "Discord ID", profession_col_header]
        try:
            ws.update("A1", [header])
        except Exception:
            pass
        values = [header]
        id_idx, prof_idx = 1, 2
    else:
        id_idx = find("discord id")
        prof_idx = find((profession_col_header or "profession").strip().lower())
        if id_idx < 0:
            id_idx = 1  # survey convention: Discord ID in column B
        if prof_idx < 0:
            prof_idx = len(header)
            try:
                ws.update_cell(1, prof_idx + 1, profession_col_header)
            except Exception:
                pass

    did = str(discord_id).strip()
    for i, row in enumerate(values[1:], start=2):
        if _cell(row, id_idx) == did:
            try:
                ws.update_cell(i, prof_idx + 1, profession)
                return True
            except Exception as e:
                print(f"[BUDDY] profession cell update failed for guild {guild_id}: {e}")
                return False

    # No existing row — append a sparse one (only Username + Discord ID + Profession).
    new_row = [""] * (prof_idx + 1)
    new_row[0] = username
    new_row[id_idx] = did
    new_row[prof_idx] = profession
    try:
        ws.append_row(new_row, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print(f"[BUDDY] profession row append failed for guild {guild_id}: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Named presets (#289 Stage 3)
#
# A saved lineup lives on the alliance's own sheet, one row per pairing, keyed
# by preset name — the same shape `storm_strategy` uses for its zone presets.
# That choice is deliberate and worth keeping: the rows are member names and
# Discord IDs, and putting them on the spreadsheet the alliance already owns
# means the bot stores none of it. Save replaces every row carrying that name;
# delete drops them. Both rewrite the tab, which is right here — a preset tab is
# small and changes rarely, unlike the buddy list itself.
# ══════════════════════════════════════════════════════════════════════════════

PRESET_HEADER = [
    "Preset Name",
    "War Leader ID",
    "War Leader",
    "Engineer ID",
    "Engineer",
]

# Long enough for a season or a rotation, short enough to sit in a Discord
# select option beside the pairing count.
MAX_PRESET_NAME = 60


def _preset_rows(guild_id: int, preset_tab: str):
    """``(worksheet, body_rows)`` for the preset tab, or ``(None, [])``."""
    ws = _open_tab(guild_id, preset_tab, PRESET_HEADER)
    if ws is None:
        return None, []
    try:
        values = ws.get_all_values()
    except Exception as e:
        print(f"[BUDDY] preset read failed for guild {guild_id}: {e}")
        return None, []
    return ws, [r for r in values[1:] if any((c or "").strip() for c in r)]


def list_presets(guild_id: int, preset_tab: str) -> list:
    """Preset names on the tab, in the order they first appear. [] on failure."""
    _ws, rows = _preset_rows(guild_id, preset_tab)
    seen: dict[str, None] = {}
    for row in rows:
        name = _cell(row, 0)
        if name and name not in seen:
            seen[name] = None
    return list(seen)


def load_preset(guild_id: int, preset_tab: str, name: str) -> list:
    """The pairs saved under ``name``, or [] when there's no such preset.

    Returned as plain ``Pair``s with no validation — the caller runs them
    through ``assign_buddies`` like any other pair list, so a preset can never
    resurrect someone who has left or contradict the profession survey."""
    _ws, rows = _preset_rows(guild_id, preset_tab)
    want = (name or "").strip().lower()
    out: list[Pair] = []
    for row in rows:
        if _cell(row, 0).lower() != want:
            continue
        wl_id, wl_name = _cell(row, 1), _cell(row, 2)
        eng_id, eng_name = _cell(row, 3), _cell(row, 4)
        if not (wl_id or wl_name) or not (eng_id or eng_name):
            continue
        out.append(Pair(wl_name, wl_id, eng_name, eng_id, source="preset"))
    return out


def save_preset(guild_id: int, preset_tab: str, name: str, result: PairingResult) -> bool:
    """Write ``result``'s pairings to the tab under ``name``, replacing any rows
    already carrying it. Returns False when the tab can't be opened or written.

    Only pairings are stored. Who is unpaired is a fact about the alliance right
    now, not about the lineup, so it's recomputed when the preset is loaded."""
    name = (name or "").strip()
    if not name:
        return False
    ws, rows = _preset_rows(guild_id, preset_tab)
    if ws is None:
        return False
    keep = [r for r in rows if _cell(r, 0).lower() != name.lower()]
    added = [
        [name, p.wl_discord_id, p.war_leader, p.eng_discord_id, p.engineer] for p in result.pairs
    ]
    return _rewrite(ws, PRESET_HEADER, keep + added, guild_id, preset_tab, current_rows=len(rows))


def delete_preset(guild_id: int, preset_tab: str, name: str) -> bool:
    """Remove every row for ``name``. False when there was nothing to remove."""
    ws, rows = _preset_rows(guild_id, preset_tab)
    if ws is None:
        return False
    want = (name or "").strip().lower()
    keep = [r for r in rows if _cell(r, 0).lower() != want]
    if len(keep) == len(rows):
        return False
    return _rewrite(ws, PRESET_HEADER, keep, guild_id, preset_tab, current_rows=len(rows))
