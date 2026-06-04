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
``train_rotation`` (``_open_tab`` / ``_cell`` / ``_col_letter`` / ``_rewrite``)
and the quota-safe one-``batch_clear``-plus-one-``update`` rewrite.

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


@dataclass
class Pair:
    war_leader: str
    wl_discord_id: str
    engineer: str
    eng_discord_id: str
    source: str = "auto"  # "auto" | "manual" — informational; every pair is sticky


@dataclass
class PairingResult:
    pairs: list = field(default_factory=list)
    unpaired_wl: list = field(default_factory=list)
    unpaired_eng: list = field(default_factory=list)


def _member_key(m: Member) -> str:
    """Identity key: Discord ID when present (robust to renames), else name."""
    did = (m.discord_id or "").strip()
    return did or _norm(m.name)


# ── Pairing algorithm ─────────────────────────────────────────────────────────


def assign_buddies(
    members: list,
    existing_pairs: list,
    *,
    engineer_doubling: bool = False,
    wl_priority: str = "name",
    fill: bool = True,
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

    ``fill=False`` validates and preserves ``existing_pairs`` and computes the
    free pools, but creates **no** new pairings — used by the manual editor so
    an "unpair" isn't instantly auto-refilled.

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
    eng_used: set[str] = set()
    wl_load: dict[str, int] = {}

    # Step 2 — validate & preserve existing pairs.
    for p in existing_pairs:
        wl = resolve(p.wl_discord_id, p.war_leader)
        eng = resolve(p.eng_discord_id, p.engineer)
        if not (wl and eng):
            continue
        if _classify(wl.profession) != "wl" or _classify(eng.profession) != "eng":
            continue
        wk, ek = _member_key(wl), _member_key(eng)
        if wk == ek or ek in eng_used or wl_load.get(wk, 0) >= cap:
            continue
        kept_pairs.append(Pair(wl.name, wl.discord_id, eng.name, eng.discord_id, source=p.source))
        eng_used.add(ek)
        wl_load[wk] = wl_load.get(wk, 0) + 1

    # Step 3 — free pools.
    all_wl = [m for m in deduped if _classify(m.profession) == "wl"]
    all_eng = [m for m in deduped if _classify(m.profession) == "eng"]

    def wl_sort_key(m: Member):
        if wl_priority == "power":
            return (-float(m.power or 0), _norm(m.name))
        return (_norm(m.name),)

    unpaired_wl_pool = sorted(
        [m for m in all_wl if wl_load.get(_member_key(m), 0) == 0], key=wl_sort_key
    )
    free_eng = sorted(
        [m for m in all_eng if _member_key(m) not in eng_used], key=lambda m: _norm(m.name)
    )

    new_pairs: list[Pair] = []

    if not fill:
        return PairingResult(
            pairs=list(kept_pairs),
            unpaired_wl=list(unpaired_wl_pool),
            unpaired_eng=list(free_eng),
        )

    # Step 4 — base 1:1 fill (give every unpaired WL an Engineer before doubling).
    while unpaired_wl_pool and free_eng:
        wl = unpaired_wl_pool.pop(0)
        eng = free_eng.pop(0)
        new_pairs.append(Pair(wl.name, wl.discord_id, eng.name, eng.discord_id, "auto"))
        wl_load[_member_key(wl)] = wl_load.get(_member_key(wl), 0) + 1
        eng_used.add(_member_key(eng))

    # Step 5 — Engineer doubling: leftover Engineers attach to the least-loaded
    # War Leader (cap 2). War Leaders are never doubled.
    if engineer_doubling and free_eng:
        while free_eng:
            candidates = [m for m in all_wl if wl_load.get(_member_key(m), 0) < 2]
            if not candidates:
                break
            candidates.sort(key=lambda m: (wl_load.get(_member_key(m), 0), _norm(m.name)))
            wl = candidates[0]
            eng = free_eng.pop(0)
            new_pairs.append(Pair(wl.name, wl.discord_id, eng.name, eng.discord_id, "auto"))
            wl_load[_member_key(wl)] += 1
            eng_used.add(_member_key(eng))

    return PairingResult(
        pairs=kept_pairs + new_pairs,
        unpaired_wl=list(unpaired_wl_pool),
        unpaired_eng=list(free_eng),
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


def _rewrite(
    ws, header: list[str], body_rows: list[list[str]], guild_id: int, tab_name: str
) -> bool:
    """Clear the tab below the header and write ``body_rows`` in one batch.
    One ``update`` after one ``batch_clear`` stays well under the Sheets
    60-writes/min quota. ``USER_ENTERED`` so Profession formulas evaluate."""
    try:
        ws.batch_clear([f"A2:{_col_letter(len(header))}{len(body_rows) + 5000}"])
        if body_rows:
            ws.update("A2", body_rows, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print(f"[BUDDY] rewrite of {tab_name!r} failed for guild {guild_id}: {e}")
        return False


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
    (or static values when the Squad Powers columns can't be resolved). One
    batched rewrite."""
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

    return _rewrite(ws, BUDDY_HEADER, body, guild_id, buddy_tab)


def read_all_professions(guild_id: int, profession_tab: str, profession_col_header: str) -> list:
    """Read the Squad Powers tab → list[Member] with true professions.

    Columns are located by header (case-insensitive) so a reordered survey
    still works. Returns [] when the tab is missing or unreadable."""
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

    out: list[Member] = []
    for row in values[1:]:
        did = _cell(row, id_idx) if id_idx >= 0 else ""
        nm = _cell(row, name_idx) if name_idx >= 0 else ""
        prof = _cell(row, prof_idx) if prof_idx >= 0 else ""
        if not (did or nm):
            continue
        out.append(Member(name=nm, discord_id=did, profession=prof))
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
