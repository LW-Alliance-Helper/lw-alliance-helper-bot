"""Transfer Management (#16) — pure-logic core.

A passive sheet-watching layer over an alliance's existing
transfer-tracking spreadsheet. This module holds the side-effect-free
core: column-map addressing, the AND-only filter DSL, change detection
(identity hashing + status snapshots), and in-game message template
rendering. It imports nothing from ``discord`` so it stays trivially
unit-testable.

The Discord-facing wiring (the ``/transfers`` hub + its Setup Transfers
wizard, the poll loop, notification embeds, the applicant viewer) lives
in ``transfers_hub.py`` / ``setup_cog.py`` / ``transfer_cog.py`` and
calls into here.

Design ground rules (see ``notes/DESIGN_transfer_management.md`` — read
the 2026-06-08 reconciliation addendum first):

- **The bot is a watcher, not a replacement.** It never writes tracking
  columns back to the alliance sheet. Change detection is therefore
  done from row content alone (Option A — content hashing), not from a
  bot-written marker column.
- **Only the Member name column is required.** Every other column is
  optional; behaviour scales to whatever the alliance maps. Columns are
  addressed by *header name* (resolved to a live index each poll, so an
  inserted/moved column doesn't break the mapping). A column map is a
  dict like::

      {"member": "Name", "power": "Total Power", "tier": "Tier",
       "want": "Want?", "confirmed": "Confirmed", "declined": "Declined",
       "notes": "Notes", "server": "Server",
       "extras": [{"label": "Bear vs Lion", "header": "BvL"}, ...]}

- **Degrade soft.** A filter that references a column which no longer
  resolves (or whose cell is blank/unparseable) errs toward *notifying*
  rather than silently dropping an applicant — for a recruiting tool,
  over-notifying beats missing someone.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import NamedTuple

from defaults import DEFAULT_TRANSFER_TEMPLATES

# Logical status columns whose value changes drive a status-change notice.
STATUS_KEYS = ("want", "confirmed", "declined")
# Top-level logical keys a column map may carry. ``server`` / ``alliance``
# feed the identity hash (not user-status); the rest are display/filter
# columns. Anything else a filter references is a free-form ``extras`` label.
TOP_LEVEL_KEYS = ("member", "power", "tier", *STATUS_KEYS, "notes", "server", "alliance")


# ── Column addressing (header-name → live index) ──────────────────────────────
#
# A column map stores *header names*, not letters:
#   {"member": "Name", "power": "Total Power", "confirmed": "Confirmed",
#    "server": "Server",
#    "extras": [{"label": "Bear vs Lion", "header": "BvL"}, ...]}
# On each poll we resolve those header names against the sheet's live header
# row into a ``{normalised-logical-key: 0-based-index}`` dict (``resolve_columns``)
# and every downstream lookup goes through that resolved dict. Resolving per
# poll is what makes the mapping survive an inserted/moved column — the index
# is recomputed from the header text, never cached as a brittle letter.


def col_letter_to_index(letter: str) -> int | None:
    """Spreadsheet column letter → 0-based index (``A`` → 0, ``Z`` → 25,
    ``AA`` → 26). Returns ``None`` for an empty / non-alphabetic value.

    Retained as the fallback in :func:`resolve_columns` (a configured value
    that matches no header but looks like a bare letter is treated as a
    literal column — a power-user escape hatch) and as a general utility.
    A local copy of the AA+-aware converter keeps this module Discord-free.
    """
    if not letter:
        return None
    letter = letter.strip().upper()
    if not letter or not letter.isalpha():
        return None
    idx = 0
    for ch in letter:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def col_index_to_letter(idx: int) -> str:
    """0-based index → spreadsheet column letter (0 → ``A``, 26 → ``AA``)."""
    if idx < 0:
        return ""
    out = ""
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(ord("A") + rem) + out
    return out


def _norm_header(value) -> str:
    """Normalise a header / key for matching: trim, collapse internal
    whitespace, casefold. So ``"Total  Power"`` and ``"total power"`` match."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def parse_column_map(raw) -> dict:
    """Decode a stored ``*_column_map_json`` value into a dict. Tolerates
    ``None`` / empty / malformed JSON by returning ``{}`` so a corrupt
    saved map degrades to "no addressable columns" rather than crashing the
    poll loop."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def resolve_columns(header_row: list, column_map: dict) -> dict:
    """Resolve a header-name column map against a sheet's live header row
    into ``{normalised-logical-key: 0-based-index}``.

    Top-level keys are stored under their own normalised name (``member``,
    ``power``, …); each extra is stored under its normalised *label* so a
    filter clause can reference it by the user-given label. A configured
    header that matches no column in ``header_row`` is dropped (degrade
    soft — a renamed/removed column simply disappears from the map); as a
    last resort a value that looks like a bare column letter is taken
    literally. Top-level keys win over a colliding extra label.
    """
    headers: dict = {}
    for i, h in enumerate(header_row):
        key = _norm_header(h)
        if key and key not in headers:
            headers[key] = i  # first occurrence wins on duplicate headers

    def _lookup(value):
        if value is None:
            return None
        norm = _norm_header(value)
        if not norm:
            return None
        if norm in headers:
            return headers[norm]
        return col_letter_to_index(str(value))  # literal-letter escape hatch

    resolved: dict = {}
    for key in TOP_LEVEL_KEYS:
        idx = _lookup(column_map.get(key))
        if idx is not None:
            resolved[key] = idx
    for extra in column_map.get("extras", []) or []:
        if not isinstance(extra, dict):
            continue
        label = _norm_header(extra.get("label", ""))
        if not label or label in resolved:
            continue
        # New maps store "header"; tolerate a legacy "letter" key too.
        idx = _lookup(extra.get("header", extra.get("letter")))
        if idx is not None:
            resolved[label] = idx
    return resolved


def cell_value(row: list, resolved: dict, key: str) -> str | None:
    """The trimmed string in ``row`` for logical column ``key`` (looked up in
    a :func:`resolve_columns` result), or ``None`` when the key isn't
    resolved or the row is too short to reach it. ``None`` is the
    "can't address this" signal that filter and snapshot logic soft-degrade
    on."""
    idx = resolved.get(_norm_header(key))
    if idx is None or idx < 0 or idx >= len(row):
        return None
    value = row[idx]
    return value.strip() if isinstance(value, str) else str(value).strip()


# Header-text synonyms used to auto-suggest a column map from a sheet's
# header row (longest/most-specific synonym wins a tie). Word-boundary
# matched, so "name" won't false-match inside "username" — only a real
# "...name..." word does. Status keys (want/confirmed/declined) only match a
# header that literally contains that word, so an intake/form sheet (which has
# no status columns) simply leaves them unmapped.
_HEADER_SYNONYMS = {
    "member": [
        "in game username",
        "in-game username",
        "in game name",
        "player name",
        "username",
        "ign",
        "player",
        "name",
    ],
    "power": ["total hero power", "hero power", "total power", "power"],
    "tier": ["anticipated seat color", "seat color", "tier", "rank", "seat"],
    "want": ["do we want them", "do we want", "want them", "want"],
    "confirmed": ["confirmed", "confirm", "approved", "accepted"],
    "declined": ["declined", "decline", "rejected", "denied"],
    "notes": ["notes", "comments", "note", "comment"],
    "server": ["current server", "home server", "server"],
    "alliance": ["current alliance", "alliance tag", "alliance"],
}


def _synonym_score(synonym: str, header_norm: str) -> int:
    """How well ``synonym`` matches a normalised header. Exact match wins big;
    a whole-word (phrase) hit scores by length so the most specific synonym
    wins; no match is 0. Word-boundary matching avoids substring false
    positives (``"name"`` inside ``"username"``)."""
    if synonym == header_norm:
        return 1000 + len(synonym)
    if re.search(rf"\b{re.escape(synonym)}\b", header_norm):
        return 10 + len(synonym)
    return 0


def suggest_column_map(header_row: list) -> dict:
    """Best-guess header-name column map from a sheet's header row, as
    ``{logical_key: header_text}``. Each logical key claims its best-matching
    *unused* header (priority follows ``TOP_LEVEL_KEYS``); a header is claimed
    by at most one key, so three power-ish columns don't all map to ``power``
    — the strongest match wins and the rest are left for the recruiter to add
    as extras. Keys with no decent match are omitted.

    This drives the wizard's "I read your sheet — does this mapping look
    right?" pre-fill; the recruiter confirms or overrides every field.
    """
    norm = [(i, _norm_header(h)) for i, h in enumerate(header_row)]
    suggestion: dict = {}
    used: set = set()
    for key in TOP_LEVEL_KEYS:
        synonyms = _HEADER_SYNONYMS.get(key, [])
        best = None  # (score, index)
        for i, header_norm in norm:
            if i in used or not header_norm:
                continue
            score = max((_synonym_score(s, header_norm) for s in synonyms), default=0)
            if score > 0 and (best is None or score > best[0]):
                best = (score, i)
        if best:
            suggestion[key] = header_row[best[1]]
            used.add(best[1])
    return suggestion


# ── Value coercion ───────────────────────────────────────────────────────────

_NUM_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
_TRUTHY = {"true", "yes", "y", "x", "1", "✓", "✔", "✅", "confirmed", "done"}
_FALSY = {"false", "no", "n", "0", "", "✗", "✘", "❌"}


# Leading numeric token: digits with comma/space thousands separators and an
# optional decimal, then an optional K/M/B suffix. Anchored at the start so we
# read the *number* out of a messy cell and ignore trailing prose.
_NUM_TOKEN = re.compile(r"\s*(\d[\d,\s]*(?:\.\d+)?)\s*([kmb])?", re.IGNORECASE)


def coerce_number(value) -> float | None:
    """Parse a numeric value out of the messy real-world formats a transfer
    sheet collects. Tolerates:

    - magnitude suffixes — ``"199M"`` → 199000000, ``"1.2b"`` → 1.2e9
    - thousands separators, comma *or* space — ``"304,743,912"`` and
      ``"125 971 854"`` both parse
    - trailing prose — ``"168,359,484 as of 5/5"`` → 168359484,
      ``"100M as of 5/5/26"`` → 1e8 (reads the leading number, drops the rest)

    Returns ``None`` when there's no leading number to read.

    Both sides of a numeric filter comparison go through this, so the
    recruiter can type the threshold in the same units the sheet displays
    (``100M``) and it lines up with a stored ``199M`` cell — and the filter
    works on a messy sheet *in place*, without the bot rewriting the data.

    Known limitation: a European decimal comma (``"174,5"`` meaning 174.5) is
    read as thousands (1745) — it's indistinguishable from a thousands group
    without locale knowledge, and the comma-as-thousands case dominates real
    sheets.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    m = _NUM_TOKEN.match(value.strip())
    if not m:
        return None
    digits = m.group(1).replace(",", "").replace(" ", "")
    if not digits or digits == ".":
        return None
    try:
        number = float(digits)
    except ValueError:
        return None
    suffix = m.group(2)
    if suffix:
        number *= _NUM_SUFFIX[suffix.lower()]
    return number


def coerce_bool(value) -> bool | None:
    """Interpret a status cell as a boolean. Returns ``None`` for values
    that aren't clearly true or false, so ``is_true`` / ``is_false`` can
    err toward notifying on ambiguity."""
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    s = str(value).strip().lower()
    if s in _TRUTHY:
        return True
    if s in _FALSY:
        return False
    return None


# ── Filter DSL (AND-only) ────────────────────────────────────────────────────
#
# Stored as JSON. Grammar:
#   {"and": [{"column": "<logical key>", "op": "<op>", "value": ...}, ...]}
# The column reference is the bot's logical name (a key in the column map),
# never a raw sheet letter — so a column that shifts position still resolves.


def parse_filter(raw) -> dict | None:
    """Decode a stored ``*_filter_json`` value into a filter dict, or
    ``None`` (meaning "no filter — notify on every row"). Empty string,
    ``None``, and malformed JSON all map to ``None``."""
    if isinstance(raw, dict):
        return raw or None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict) and parsed.get("and"):
        return parsed
    return None


def _eval_clause(clause: dict, row: list, resolved: dict) -> bool:
    """Evaluate one filter clause. Unaddressable column or unparseable
    value → ``True`` (soft pass: don't drop the applicant)."""
    if not isinstance(clause, dict):
        return True
    col = clause.get("column", "")
    op = clause.get("op", "")
    target = clause.get("value")

    cell = cell_value(row, resolved, col)
    if cell is None:
        # Column no longer resolves / row too short — degrade soft.
        return True

    if op in (">=", ">", "<=", "<", "==", "!="):
        left = coerce_number(cell)
        right = coerce_number(target)
        if left is None or right is None:
            return True
        if op == ">=":
            return left >= right
        if op == ">":
            return left > right
        if op == "<=":
            return left <= right
        if op == "<":
            return left < right
        if op == "==":
            return left == right
        return left != right

    if op == "contains":
        return str(target).strip().lower() in cell.lower()
    if op == "equals":
        return cell.lower() == str(target).strip().lower()
    if op == "in":
        if not isinstance(target, (list, tuple)):
            return True
        wanted = {str(v).strip().lower() for v in target}
        return cell.lower() in wanted
    if op == "is_true":
        result = coerce_bool(cell)
        return result is not False  # True or ambiguous → pass
    if op == "is_false":
        result = coerce_bool(cell)
        return result is not True

    # Unknown operator — don't silently drop the row.
    return True


def evaluate_filter(filter_obj, row: list, resolved: dict) -> bool:
    """``True`` if ``row`` passes the (AND-of-clauses) filter. A falsy
    filter passes everything. ``filter_obj`` may be the parsed dict or the
    raw stored JSON string; ``resolved`` is a :func:`resolve_columns` map."""
    if isinstance(filter_obj, str):
        filter_obj = parse_filter(filter_obj)
    if not filter_obj:
        return True
    clauses = filter_obj.get("and") or []
    return all(_eval_clause(c, row, resolved) for c in clauses)


# ── Change detection (Option A — row-content hashing) ────────────────────────


def normalize_identity(value) -> str:
    """Normalise an identity component for hashing: trim + casefold +
    collapse internal whitespace, so trivial spacing/case edits in the
    sheet don't read as a brand-new applicant."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def identity_hash(member: str, alliance: str = "", server: str = "") -> str:
    """Stable per-applicant key: ``sha256`` over the relatively-immutable
    identity fields (member name + source alliance + server). Power and
    tier are deliberately excluded — those legitimately change for the
    same applicant between polls."""
    parts = "\x1f".join(normalize_identity(p) for p in (member, alliance, server))
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def row_identity(row: list, resolved: dict, *, alliance: str = "", server: str = "") -> str | None:
    """Identity hash for a sheet row, or ``None`` when the row has no
    member name (a blank/spacer row we should skip). ``alliance`` and
    ``server`` are supplied by the caller — the alliance is constant for an
    alliance's own sheet, and a server value comes from a mapped column
    when present."""
    member = cell_value(row, resolved, "member")
    if not member:
        return None
    return identity_hash(member, alliance, server)


def status_snapshot(row: list, resolved: dict) -> dict:
    """Snapshot of a row's mapped status columns, keyed by logical name.
    Stores the raw trimmed cell text (not a coerced bool) so any change —
    ``""`` → ``"Confirmed"``, ``false`` → ``true`` — is detected without
    depending on truthiness interpretation. Only keys actually resolved on
    the sheet are included."""
    snap = {}
    for key in STATUS_KEYS:
        value = cell_value(row, resolved, key)
        if value is not None:
            snap[key] = value
    return snap


def status_was_set(snapshot: dict) -> bool:
    """``True`` if a status snapshot records a real decision — any status
    field whose value is non-empty and not a falsy marker (``""`` / ``no`` /
    ``false`` / ``0`` …). Used to decide whether a *deleted* row is worth a
    removal notice: a pending applicant cleaned off the sheet shouldn't
    notify, but one that had been marked Confirmed / Declined should."""
    for value in (snapshot or {}).values():
        s = str(value).strip()
        if s and s.lower() not in _FALSY:
            return True
    return False


def diff_status(old: dict, new: dict) -> list[tuple[str, str, str]]:
    """Changed status fields between two snapshots, as
    ``(field, old_value, new_value)`` tuples. Considers the union of keys,
    treating a missing side as ``""``; unchanged fields are omitted. Drives
    the "what changed" line on a status-change notification."""
    old = old or {}
    new = new or {}
    changes = []
    for key in sorted(set(old) | set(new)):
        before = str(old.get(key, "") or "")
        after = str(new.get(key, "") or "")
        if before != after:
            changes.append((key, before, after))
    return changes


# ── Poll orchestration ───────────────────────────────────────────────────────


class NewApplicant(NamedTuple):
    """A row whose identity is unseen since the last poll and which passes
    the new-applicant filter."""

    hash: str
    row: list
    snapshot: dict


class StatusChange(NamedTuple):
    """A previously-seen row whose mapped status columns changed. ``changes``
    is the ``diff_status`` list of ``(field, old, new)`` tuples."""

    hash: str
    row: list
    changes: list
    snapshot: dict


class Deletion(NamedTuple):
    """A previously-seen applicant that's no longer on the sheet and *had a
    status set* (``status_was_set``). ``snapshot`` is the last-seen status.
    Surfaced only so the cog can post a removal notice when the alliance
    opted into ``notify_on_delete``; the row is dropped from ``next_state``
    either way."""

    hash: str
    snapshot: dict


class PollDiff(NamedTuple):
    new_applicants: list  # list[NewApplicant]
    status_changes: list  # list[StatusChange]
    deletions: list  # list[Deletion] — status-bearing rows that vanished
    next_state: dict  # {hash: status_snapshot} to persist


def compute_poll_diff(
    rows: list,
    resolved: dict,
    *,
    prior_state: dict,
    filter_obj=None,
    alliance: str = "",
    baseline: bool = False,
) -> PollDiff:
    """Diff one poll of an alliance sheet against the last-seen state.

    ``rows`` are the data rows (header already stripped); ``resolved`` is a
    :func:`resolve_columns` map for that sheet's live header row. Returns a
    :class:`PollDiff` of the new applicants and status changes to announce,
    the status-bearing deletions to optionally announce, and the
    ``next_state`` to persist.

    Behaviour (see ``DESIGN_transfer_management.md`` §7, §11):

    - **Identity** is ``member + alliance + server``. ``alliance`` defaults
      to the guild's alliance (a column labelled "Alliance" overrides it
      per-row, for server-wide sources); ``server`` comes from a column
      labelled "Server" when mapped, else blank. Power/tier never feed the
      hash — they legitimately change between polls.
    - **New hash** → new applicant, but only announced when it passes
      ``filter_obj``. Either way the row is recorded in ``next_state``, so a
      filtered-out applicant is bookmarked and won't re-fire if the filter
      later loosens.
    - **Seen hash, changed status snapshot** → status change. Status changes
      are *not* filtered — a low-power applicant being marked Confirmed is
      exactly the event leadership wants to see.
    - **Seen last time, absent now** → row deleted; dropped from
      ``next_state``. Returned in ``deletions`` only when a status had been
      set (so the opt-in removal notice never fires for cleaned-up pending
      rows). The cog decides whether to actually post it (``notify_on_delete``).
    - **baseline=True** → bookmark every current row into ``next_state`` and
      announce nothing (no new/changed/deleted). Used for the silent first
      read at setup so existing rows don't flood the channel.

    Blank rows (no member name) and exact duplicate identities collapse
    safely (last snapshot wins).
    """
    prior_state = prior_state or {}
    next_state: dict = {}
    new_applicants: list = []
    status_changes: list = []

    for row in rows:
        member = cell_value(row, resolved, "member")
        if not member:
            continue  # blank / spacer row
        server = cell_value(row, resolved, "server") or ""
        row_alliance = cell_value(row, resolved, "alliance") or alliance
        h = identity_hash(member, row_alliance, server)
        snap = status_snapshot(row, resolved)
        seen_before = h in prior_state
        next_state[h] = snap

        if baseline:
            continue
        if not seen_before:
            if evaluate_filter(filter_obj, row, resolved):
                new_applicants.append(NewApplicant(h, row, snap))
        else:
            changes = diff_status(prior_state[h], snap)
            if changes:
                status_changes.append(StatusChange(h, row, changes, snap))

    deletions: list = []
    if not baseline:
        for h, snap in prior_state.items():
            if h not in next_state and status_was_set(snap):
                deletions.append(Deletion(h, snap))

    return PollDiff(new_applicants, status_changes, deletions, next_state)


def select_rows_to_copy(
    source_rows: list,
    source_resolved: dict,
    *,
    already_copied: set,
    filter_obj=None,
    alliance: str = "",
) -> tuple[list, set]:
    """Pick rows from an optional source sheet (server-wide pool or intake
    form) that should be copied into the alliance's own sheet.

    ``source_resolved`` is a :func:`resolve_columns` map for the source
    sheet. Returns ``(rows_to_copy, updated_copied)`` where ``rows_to_copy``
    are the source rows that pass ``filter_obj`` and whose identity hash
    isn't in ``already_copied``, and ``updated_copied`` is ``already_copied``
    plus the hashes of the rows being copied. The hash set dedups across
    polls so a matching row is copied once even though it lingers in the
    source sheet.

    The bot only ever copies *into the alliance's own* sheet — never into a
    sheet shared with other alliances (DESIGN §1).
    """
    rows_to_copy: list = []
    updated = set(already_copied)
    for row in source_rows:
        member = cell_value(row, source_resolved, "member")
        if not member:
            continue
        server = cell_value(row, source_resolved, "server") or ""
        row_alliance = cell_value(row, source_resolved, "alliance") or alliance
        h = identity_hash(member, row_alliance, server)
        if h in updated:
            continue
        if not evaluate_filter(filter_obj, row, source_resolved):
            continue
        rows_to_copy.append(row)
        updated.add(h)
    return rows_to_copy, updated


# ── In-game message templates ────────────────────────────────────────────────


# The placeholders a transfer template may reference. A *known* placeholder
# left unfilled renders blank (the alliance didn't map that column); a typo'd
# / unknown placeholder renders literally so a mistake is visible instead of
# silently swallowed.
TEMPLATE_PLACEHOLDERS = ("name", "alliance_name", "tier", "power")


class _SafeDict(dict):
    """`str.format_map` backing dict that renders unknown placeholders
    literally (``{nme}`` stays ``{nme}``) instead of raising — same idiom
    as the storm / train / buddy DM renderers. A typo in a saved template
    can't crash the render path."""

    def __missing__(self, key):
        return "{" + key + "}"


def render_transfer_template(template: str, **context) -> str:
    """Substitute ``{name}`` / ``{alliance_name}`` / ``{tier}`` /
    ``{power}`` (any subset) into a template body. A *known* placeholder
    not supplied (or supplied as ``None``) renders as an empty string; an
    *unknown* placeholder (a typo) renders literally so the mistake shows."""
    safe = _SafeDict({k: "" for k in TEMPLATE_PLACEHOLDERS})
    safe.update({k: ("" if v is None else v) for k, v in context.items()})
    try:
        return str(template).format_map(safe)
    except (ValueError, IndexError):
        # A stray unescaped brace / bad format spec — return the raw
        # template rather than crashing the caller.
        return str(template)


def resolve_template(cfg: dict, kind: str) -> str:
    """The body to render for a template ``kind`` (``apply_invitation`` /
    ``confirm_request`` / ``decline``): the guild's saved override when
    non-empty, else the hardcoded default from ``defaults.py``."""
    if kind not in DEFAULT_TRANSFER_TEMPLATES:
        raise ValueError(f"unknown transfer template kind: {kind!r}")
    saved = (cfg.get(f"template_{kind}") or "").strip()
    return saved or DEFAULT_TRANSFER_TEMPLATES[kind]
