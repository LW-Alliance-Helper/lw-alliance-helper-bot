"""Transfer Management (#16) — pure-logic core.

A passive sheet-watching layer over an alliance's existing
transfer-tracking spreadsheet. This module holds the side-effect-free
core: header-name column addressing, the AND-only filter DSL, change
detection (identity hashing + status snapshots), and in-game message
template rendering. It imports nothing from ``discord`` so it stays
trivially unit-testable.

The Discord-facing wiring (the ``/transfers`` hub + its Setup Transfers
wizard, the poll loop, notification embeds, the applicant viewer) lives
in ``transfers_hub.py`` / ``setup_cog.py`` / ``transfer_cog.py`` and
calls into here.

Design ground rules (see ``notes/DESIGN_transfer_management.md`` — read
the 2026-06-08 reconciliation addendum first):

- **Watcher, not replacement.** Change detection reads row content
  alone (Option A — content hashing). The bot only writes to the
  alliance's *own* sheet, and only for the opt-in decision write-back /
  source-copy paths — never to a shared sheet.
- **Only the Name column is special-and-required.** There are no
  privileged "power"/"tier" slots. A column map names just what the bot
  must understand; everything else an alliance wants to see is a
  free-choice **display** column. Shape::

      {"name": "In Game Username",                  # required (identity + {name})
       "identity_extra": ["Current Server"],         # optional — also distinguishes people
       "status": ["Want?", "Confirmed", "Declined"], # optional set — watched + write-back
       "display": ["Total Hero Power", "Arena Total Hero Power", ...]}  # optional, ordered

  Columns are addressed by *header name*, resolved to a live index each
  poll (:func:`header_index` + :func:`cell_for`), so an inserted/moved
  column doesn't break the mapping. A filter clause can target *any*
  column by header, mapped or not.
- **Degrade soft.** A filter that references a column which no longer
  resolves (or whose cell is blank/unparseable) errs toward *notifying*
  rather than dropping an applicant — over-notifying beats missing
  someone.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from defaults import DEFAULT_TRANSFER_TEMPLATES


# ── Column addressing (header-name → live index) ──────────────────────────────


def col_letter_to_index(letter: str) -> int | None:
    """Spreadsheet column letter → 0-based index (``A`` → 0, ``Z`` → 25,
    ``AA`` → 26). Returns ``None`` for an empty / non-alphabetic value.

    Retained as the fallback in :func:`cell_for` (a configured value that
    matches no header but looks like a bare letter is treated as a literal
    column — a power-user escape hatch) and as a general utility."""
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


def extract_sheet_id(value) -> str:
    """Accept either a raw Google Sheet ID or a full sheet URL and return the
    bare ID. Recruiters routinely paste the whole
    ``https://docs.google.com/spreadsheets/d/<ID>/edit#gid=0`` link; pull the
    ID out of the ``/d/<ID>`` segment (it stops at the next ``/``, ``?`` or
    ``#``). A value that isn't a URL is returned trimmed as-is, so a bare ID
    passes through unchanged."""
    if not value:
        return ""
    s = str(value).strip()
    m = re.search(r"/spreadsheets/d/([A-Za-z0-9_-]+)", s) or re.search(r"/d/([A-Za-z0-9_-]+)", s)
    return m.group(1) if m else s


def parse_column_map(raw) -> dict:
    """Decode a stored ``*_column_map_json`` value into a dict. Tolerates
    ``None`` / empty / malformed JSON by returning ``{}`` so a corrupt saved
    map degrades to "nothing addressable" rather than crashing the poll
    loop."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def header_index(header_row: list) -> dict:
    """``{normalised header: 0-based index}`` for every column in a sheet's
    live header row (first occurrence wins on duplicate headers). Built once
    per poll; every column reference resolves through it, so the mapping is
    recomputed from header text each time and survives an inserted/moved
    column."""
    out: dict = {}
    for i, h in enumerate(header_row):
        key = _norm_header(h)
        if key and key not in out:
            out[key] = i
    return out


def cell_for(row: list, hidx: dict, header) -> str | None:
    """Trimmed cell value in ``row`` for a column named ``header`` (resolved
    via a :func:`header_index` map), or ``None`` when the header isn't in the
    sheet or the row is too short to reach it. Falls back to treating
    ``header`` as a literal column letter when it matches no header. ``None``
    is the "can't address this" signal filter/snapshot logic soft-degrade
    on."""
    if not header:
        return None
    idx = hidx.get(_norm_header(header))
    if idx is None:
        idx = col_letter_to_index(str(header))  # literal-letter escape hatch
    if idx is None or idx < 0 or idx >= len(row):
        return None
    value = row[idx]
    return value.strip() if isinstance(value, str) else str(value).strip()


# ── Auto-suggest a column map from a header row ──────────────────────────────
#
# Word-boundary synonym matching, so "name" won't false-match inside
# "username" — only a real "...name..." word does. The suggestion is a head
# start the wizard pre-fills; the recruiter overrides everything. Nothing is
# required except a Name column.

_NAME_SYNONYMS = [
    "in game username",
    "in-game username",
    "in game name",
    "player name",
    "username",
    "ign",
    "player",
    "name",
]
_IDENTITY_EXTRA_SYNONYMS = ["current server", "home server", "server"]
_STATUS_SYNONYMS = [
    "want",
    "do we want",
    "want them",
    "confirmed",
    "confirm",
    "declined",
    "decline",
    "approved",
    "accepted",
    "rejected",
    "denied",
]
# Stat-like columns seeded into the notification display set (the recruiter
# adds/removes freely). Deliberately broad: e.g. "power" claims all three of
# Total Hero / Arena / Main March power, so a 3-power-stat notice is the
# default rather than something to assemble by hand.
_DISPLAY_SYNONYMS = [
    "power",
    "tier",
    "seat color",
    "seat",
    "rank",
    "kills",
    "drone",
    "profession",
    "squad",
]


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


def decisions_for(column_map: dict) -> list:
    """The write-back **decisions** for a column map: a list of
    ``{"column", "kind", "options"}``. ``kind`` is ``"yesno"`` (a checkbox the
    bot ticks/unticks) or ``"pickone"`` (a dropdown the bot sets to one of
    ``options``).

    Prefers an explicit ``decisions`` list; falls back to treating each watched
    ``status`` column as a yes/no decision, so a config saved before the
    decision model still drives a sensible (checkbox) write-back. The watched-
    column list (``status``) stays the change-detection source of truth; this
    only adds *how to write* each one."""
    raw = column_map.get("decisions")
    out: list = []
    if isinstance(raw, list) and raw:
        for d in raw:
            if not isinstance(d, dict):
                continue
            col = d.get("column")
            if not col:
                continue
            kind = d.get("kind") if d.get("kind") in ("yesno", "pickone") else "yesno"
            options = [str(o) for o in (d.get("options") or [])]
            out.append({"column": col, "kind": kind, "options": options})
        return out
    return [
        {"column": col, "kind": "yesno", "options": []} for col in (column_map.get("status") or [])
    ]


def describe_decision(decision: dict) -> str:
    """One-line label for a decision: ``"Confirmed (Yes/No)"`` or
    ``"Status (Pending/Confirmed/Declined)"``."""
    col = decision.get("column", "?")
    if decision.get("kind") == "pickone" and decision.get("options"):
        return f"{col} ({'/'.join(decision['options'])})"
    return f"{col} (Yes/No)"


def summarize_column_map(column_map: dict) -> str:
    """Human-readable one-block summary of a column map for the wizard's
    review embed and the hub. Lists the Name column, any identity-fallback
    columns, the decisions the bot can make, and the columns shown in notices."""
    name = column_map.get("name") or "*not set*"
    identity = column_map.get("identity_extra") or []
    display = column_map.get("display") or []
    lines = [f"**Name:** {name}"]
    if identity:
        lines.append(f"**Identity Fallback:** {', '.join(identity)}")
    decisions = decisions_for(column_map)
    if decisions:
        lines.append(f"**Decisions:** {', '.join(describe_decision(d) for d in decisions)}")
    else:
        lines.append("**Decisions:** *none*")
    lines.append(f"**Shown in notices:** {', '.join(display) if display else '*none*'}")
    return "\n".join(lines)


def suggest_column_map(header_row: list) -> dict:
    """Best-guess column map from a sheet's header row, in the
    ``name`` / ``identity_extra`` / ``status`` / ``display`` shape. A head
    start for the wizard — the recruiter confirms/overrides all of it.

    - ``name``: the single best identity-column match.
    - ``identity_extra``: a server column when present (so same-name
      applicants from different servers stay distinct).
    - ``status``: *every* header that reads like a decision column
      (Want / Confirmed / Declined / Approved …); empty on an intake/form
      sheet that has none.
    - ``display``: the stat-like columns, in sheet order, as a starting set.

    Each header is claimed by at most one role (name > identity > status >
    display), so a column never double-books.
    """
    items = [(i, _norm_header(h)) for i, h in enumerate(header_row)]
    claimed: set = set()

    def _best(synonyms):
        best = None  # (score, index)
        for i, hn in items:
            if i in claimed or not hn:
                continue
            score = max((_synonym_score(s, hn) for s in synonyms), default=0)
            if score > 0 and (best is None or score > best[0]):
                best = (score, i)
        return best[1] if best else None

    def _all(synonyms):
        found = []
        for i, hn in items:
            if i in claimed or not hn:
                continue
            if any(_synonym_score(s, hn) > 0 for s in synonyms):
                found.append(i)
        return found

    suggestion: dict = {}

    ni = _best(_NAME_SYNONYMS)
    if ni is not None:
        suggestion["name"] = header_row[ni]
        claimed.add(ni)

    ei = _best(_IDENTITY_EXTRA_SYNONYMS)
    if ei is not None:
        suggestion["identity_extra"] = [header_row[ei]]
        claimed.add(ei)

    status_idx = _all(_STATUS_SYNONYMS)
    if status_idx:
        suggestion["status"] = [header_row[i] for i in status_idx]
        claimed.update(status_idx)

    display_idx = _all(_DISPLAY_SYNONYMS)
    if display_idx:
        suggestion["display"] = [header_row[i] for i in display_idx]

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
    """Parse a number out of the messy real-world formats a transfer sheet
    collects. Tolerates:

    - magnitude suffixes — ``"199M"`` → 199000000, ``"1.2b"`` → 1.2e9
    - thousands separators, comma *or* space — ``"304,743,912"`` and
      ``"125 971 854"`` both parse
    - trailing prose — ``"168,359,484 as of 5/5"`` → 168359484,
      ``"100M as of 5/5/26"`` → 1e8 (reads the leading number, drops the rest)

    Returns ``None`` when there's no leading number to read.

    Both sides of a numeric filter comparison go through this, so the
    recruiter types a threshold in the units the sheet shows (``100M``) and it
    lines up with a stored ``199M`` cell — the filter works on a messy sheet
    *in place*, without the bot rewriting the data.

    Known limitation: a European decimal comma (``"174,5"`` → 174.5) is read
    as thousands (1745); indistinguishable from a thousands group without
    locale knowledge, and comma-as-thousands dominates real sheets."""
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


def column_value_kind(values, *, max_choices: int = 20) -> tuple[str, list]:
    """Pick the filter control for a column from sampled cell values:

    - ``"numeric"`` if most non-empty values parse as numbers → the wizard
      offers a ``≥ / ≤ / =`` threshold (no value list).
    - ``"choice"`` if there's a small set of distinct non-empty values
      (``<= max_choices``) → a multi-select (``in`` operator). Returns the
      sorted distinct values.
    - ``"text"`` otherwise → a free-text ``contains`` match.

    Drives the setup filter builder so each column gets a sensible control
    without the recruiter choosing an operator type by hand."""
    non_empty = [str(v).strip() for v in values if str(v).strip()]
    if not non_empty:
        return "text", []
    numeric = sum(1 for v in non_empty if coerce_number(v) is not None)
    if numeric >= max(1, int(len(non_empty) * 0.6)):
        return "numeric", []
    distinct = sorted({v for v in non_empty}, key=str.lower)
    if len(distinct) <= max_choices:
        return "choice", distinct
    return "text", []


def coerce_bool(value) -> bool | None:
    """Interpret a status cell as a boolean. Returns ``None`` for values that
    aren't clearly true or false, so ``is_true`` / ``is_false`` can err toward
    notifying on ambiguity."""
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
#   {"and": [{"column": "<header name>", "op": "<op>", "value": ...}, ...]}
# The column reference is a header name (resolved via header_index), so a
# clause can target any column — mapped or not — and survives a column move.


def parse_filter(raw) -> dict | None:
    """Decode a stored ``*_filter_json`` value into a filter dict, or ``None``
    (meaning "no filter — notify on every row"). Empty string, ``None``, and
    malformed JSON all map to ``None``."""
    if isinstance(raw, dict):
        return raw or None
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if isinstance(parsed, dict) and (parsed.get("and") or parsed.get("clauses")):
        return parsed
    return None


def _filter_parts(filter_obj) -> tuple[list, list]:
    """Normalise either filter shape into ``(clauses, joins)`` where ``joins[i]``
    (``"and"`` / ``"or"``) connects ``clauses[i]`` to ``clauses[i + 1]``, so
    ``len(joins) == max(0, len(clauses) - 1)``.

    Two stored shapes are accepted: the original all-AND ``{"and": [...]}`` and
    the mixed ``{"clauses": [...], "joins": [...]}``. Left-to-right evaluation (no
    operator precedence) matches how a recruiter reads the filter they built."""
    if not isinstance(filter_obj, dict):
        return [], []
    if "clauses" in filter_obj:
        clauses = list(filter_obj.get("clauses") or [])
        joins = [str(j).lower() for j in (filter_obj.get("joins") or [])]
    else:
        clauses = list(filter_obj.get("and") or [])
        joins = ["and"] * max(0, len(clauses) - 1)
    need = max(0, len(clauses) - 1)
    joins = (joins + ["and"] * need)[:need]
    return clauses, joins


def _eval_clause(clause: dict, row: list, hidx: dict) -> bool:
    """Evaluate one filter clause. Unaddressable column or unparseable value →
    ``True`` (soft pass: don't drop the applicant)."""
    if not isinstance(clause, dict):
        return True
    col = clause.get("column", "")
    op = clause.get("op", "")
    target = clause.get("value")

    cell = cell_for(row, hidx, col)
    if cell is None:
        return True  # column no longer resolves / row too short — degrade soft

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
        return coerce_bool(cell) is not False  # True or ambiguous → pass
    if op == "is_false":
        return coerce_bool(cell) is not True

    return True  # unknown operator — don't silently drop the row


_OP_LABELS = {
    ">=": "≥",
    ">": ">",
    "<=": "≤",
    "<": "<",
    "==": "=",
    "!=": "≠",
    "contains": "contains",
    "equals": "=",
    "in": "in",
    "is_true": "is set",
    "is_false": "is not set",
}


def _describe_clause(clause: dict) -> str | None:
    """One clause as text (``"Total Power ≥ 250M"``), or ``None`` if malformed."""
    if not isinstance(clause, dict):
        return None
    col = clause.get("column", "")
    op = clause.get("op", "")
    val = clause.get("value")
    label = _OP_LABELS.get(op, op)
    if op == "in" and isinstance(val, (list, tuple)):
        return f"{col} in [{', '.join(str(v) for v in val)}]"
    if op in ("is_true", "is_false"):
        return f"{col} {label}"
    return f"{col} {label} {val}"


def describe_filter(filter_obj) -> str:
    """Human-readable one-line summary of a filter for setup/hub embeds, e.g.
    ``"Total Hero Power ≥ 250M AND Tier in [Pioneer, Elite]"`` or a mixed
    ``"Alliance contains OGV OR Alliance contains Open AND Power ≥ 70M"``. A
    falsy filter reads as "every new applicant"."""
    if isinstance(filter_obj, str):
        filter_obj = parse_filter(filter_obj)
    if not filter_obj:
        return "every new applicant (no filter)"
    clauses, joins = _filter_parts(filter_obj)
    parts = [p for c in clauses if (p := _describe_clause(c)) is not None]
    if not parts:
        return "every new applicant (no filter)"
    out = parts[0]
    for join, part in zip(joins, parts[1:]):
        out += f" {join.upper()} {part}"
    return out


def evaluate_filter(filter_obj, row: list, hidx: dict) -> bool:
    """``True`` if ``row`` passes the filter, evaluating clauses left-to-right
    with their AND/OR connectors (no operator precedence — matches how the
    recruiter built it). A falsy filter passes everything. ``filter_obj`` may be
    the parsed dict or the raw stored JSON string; ``hidx`` is a
    :func:`header_index` map."""
    if isinstance(filter_obj, str):
        filter_obj = parse_filter(filter_obj)
    if not filter_obj:
        return True
    clauses, joins = _filter_parts(filter_obj)
    if not clauses:
        return True
    result = _eval_clause(clauses[0], row, hidx)
    for join, clause in zip(joins, clauses[1:]):
        passed = _eval_clause(clause, row, hidx)
        result = (result or passed) if join == "or" else (result and passed)
    return result


# ── Change detection (Option A — row-content hashing) ────────────────────────


def normalize_identity(value) -> str:
    """Normalise an identity component for hashing: trim + casefold + collapse
    internal whitespace, so trivial spacing/case edits don't read as a
    brand-new applicant."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).strip()).casefold()


def identity_hash(*parts) -> str:
    """Stable per-applicant key: ``sha256`` over the identity components
    (name, then each configured ``identity_extra`` value). Only relatively
    immutable fields feed it — power/tier/status never do, since those
    legitimately change for the same applicant between polls."""
    joined = "\x1f".join(normalize_identity(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def row_identity(row: list, hidx: dict, column_map: dict) -> str | None:
    """Identity hash for a sheet row: the Name cell plus each configured
    ``identity_extra`` cell. ``None`` when the row has no name (a blank/spacer
    row to skip)."""
    name = cell_for(row, hidx, column_map.get("name"))
    if not name:
        return None
    parts = [name]
    for header in column_map.get("identity_extra", []) or []:
        parts.append(cell_for(row, hidx, header) or "")
    return identity_hash(*parts)


def find_row_index(rows: list, hidx: dict, column_map: dict, target_hash: str) -> int | None:
    """0-based index into ``rows`` of the row whose identity hash equals
    ``target_hash``, or ``None`` if it's no longer there. Used by decision
    write-back to locate the cell to update at click time (the sheet row is
    ``index + 2``, accounting for the header) — the row may have moved since
    the notice was posted, so it's re-found by identity, never by position."""
    for i, row in enumerate(rows):
        if not cell_for(row, hidx, column_map.get("name")):
            continue
        if row_identity(row, hidx, column_map) == target_hash:
            return i
    return None


def status_snapshot(row: list, hidx: dict, status_headers) -> dict:
    """Snapshot of a row's configured status columns, keyed by header. Stores
    the raw trimmed cell text (not a coerced bool) so any change — ``""`` →
    ``"Confirmed"``, ``false`` → ``true`` — is detected without depending on
    truthiness. Only columns that resolve on the sheet are included."""
    snap: dict = {}
    for header in status_headers or []:
        value = cell_for(row, hidx, header)
        if value is not None:
            snap[header] = value
    return snap


def display_fields(row: list, hidx: dict, display_headers) -> list[tuple[str, str]]:
    """``(header, value)`` pairs for the configured display columns, in the
    order the alliance chose. Columns that don't resolve are skipped. Drives
    the notification embed body and the templates' per-column placeholders."""
    out: list = []
    for header in display_headers or []:
        value = cell_for(row, hidx, header)
        if value is not None:
            out.append((header, value))
    return out


def status_was_set(snapshot: dict) -> bool:
    """``True`` if a status snapshot records a real decision — any field whose
    value is non-empty and not a falsy marker (``""`` / ``no`` / ``false`` /
    ``0`` …). Used to decide whether a *deleted* row is worth a removal notice:
    a pending applicant cleaned off the sheet shouldn't notify, one marked
    Confirmed / Declined should."""
    for value in (snapshot or {}).values():
        s = str(value).strip()
        if s and s.lower() not in _FALSY:
            return True
    return False


def diff_status(old: dict, new: dict) -> list[tuple[str, str, str]]:
    """Changed status fields between two snapshots, as ``(field, old, new)``
    tuples. Considers the union of keys, treating a missing side as ``""``;
    unchanged fields are omitted. Drives the "what changed" line on a
    status-change notification."""
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


def poll_is_due(last_polled_at: str, freq_minutes, now: datetime) -> bool:
    """Whether a guild is due for a poll. ``True`` if it has never polled, the
    stored timestamp is unparseable, or at least ``freq_minutes`` have elapsed
    since ``last_polled_at``. ``now`` (a timezone-aware UTC ``datetime``) is
    passed in so this stays pure and testable; a naive stored timestamp is
    treated as UTC."""
    if not last_polled_at:
        return True
    try:
        last = datetime.fromisoformat(last_polled_at)
    except (ValueError, TypeError):
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (now - last) >= timedelta(minutes=max(1, int(freq_minutes or 1)))


class NewApplicant(NamedTuple):
    """A row whose identity is unseen since the last poll and which passes the
    new-applicant filter."""

    hash: str
    row: list
    snapshot: dict


class StatusChange(NamedTuple):
    """A previously-seen row whose status columns changed. ``changes`` is the
    ``diff_status`` list of ``(field, old, new)`` tuples."""

    hash: str
    row: list
    changes: list
    snapshot: dict


class Deletion(NamedTuple):
    """A previously-seen applicant that's no longer on the sheet and *had a
    status set* (``status_was_set``). ``name`` is the last-seen applicant name
    (the row is gone, so it can only come from the stored state — which is why
    state carries the name); ``snapshot`` is the last-seen status. Surfaced
    only so the cog can post a removal notice when the alliance opted into
    ``notify_on_delete``; the entry drops from ``next_state`` regardless."""

    hash: str
    name: str
    snapshot: dict


class PollDiff(NamedTuple):
    new_applicants: list  # list[NewApplicant]
    status_changes: list  # list[StatusChange]
    deletions: list  # list[Deletion] — status-bearing rows that vanished
    next_state: dict  # {hash: {"name": str, "status": snapshot}} to persist


def _state_snapshot(entry) -> dict:
    """Status snapshot out of a stored state entry. Tolerates the current
    ``{"name", "status"}`` shape and a legacy bare-snapshot dict, so a state
    written by an older build still diffs cleanly."""
    if isinstance(entry, dict) and isinstance(entry.get("status"), dict):
        return entry["status"]
    return entry if isinstance(entry, dict) else {}


def _state_name(entry) -> str:
    """Applicant name out of a stored state entry (``""`` for legacy shape)."""
    return entry.get("name", "") if isinstance(entry, dict) else ""


def compute_poll_diff(
    rows: list,
    hidx: dict,
    column_map: dict,
    *,
    prior_state: dict,
    filter_obj=None,
    baseline: bool = False,
) -> PollDiff:
    """Diff one poll of a sheet against the last-seen state.

    ``rows`` are the data rows (header already stripped); ``hidx`` is the
    sheet's :func:`header_index`; ``column_map`` carries ``name`` /
    ``identity_extra`` / ``status``. Returns a :class:`PollDiff`.

    - **Identity** = Name + each ``identity_extra`` cell (e.g. server), so the
      same name on two servers stays distinct. Power/tier/status never feed
      it.
    - **New hash** → new applicant, announced only when it passes
      ``filter_obj``; either way it's bookmarked in ``next_state`` so a
      filtered-out row won't re-fire if the filter later loosens.
    - **Seen hash, changed status snapshot** → status change (not filtered —
      a low-power applicant marked Confirmed is exactly what to surface).
    - **Seen last time, absent now** → dropped from ``next_state``; returned
      in ``deletions`` only when a status had been set (the cog gates the
      actual notice on ``notify_on_delete``).
    - **baseline=True** → bookmark every current row and announce nothing
      (the silent first read at setup).

    Blank rows (no name) and duplicate identities collapse safely (last
    snapshot wins)."""
    prior_state = prior_state or {}
    status_headers = column_map.get("status", []) or []
    next_state: dict = {}
    new_applicants: list = []
    status_changes: list = []

    for row in rows:
        name = cell_for(row, hidx, column_map.get("name"))
        if not name:
            continue  # blank / spacer row
        h = row_identity(row, hidx, column_map)
        snap = status_snapshot(row, hidx, status_headers)
        seen_before = h in prior_state
        next_state[h] = {"name": name, "status": snap}

        if baseline:
            continue
        if not seen_before:
            if evaluate_filter(filter_obj, row, hidx):
                new_applicants.append(NewApplicant(h, row, snap))
        else:
            changes = diff_status(_state_snapshot(prior_state[h]), snap)
            if changes:
                status_changes.append(StatusChange(h, row, changes, snap))

    deletions: list = []
    if not baseline:
        for h, entry in prior_state.items():
            if h not in next_state:
                snap = _state_snapshot(entry)
                if status_was_set(snap):
                    deletions.append(Deletion(h, _state_name(entry), snap))

    return PollDiff(new_applicants, status_changes, deletions, next_state)


def align_row(
    source_header: list, source_row: list, target_header: list, copy_map: dict | None = None
) -> list:
    """Reorder a source row into the *target* sheet's column order. By default
    columns line up by header name (case-insensitive). ``copy_map`` overrides
    that for columns the two sheets name differently: it maps a *target* header
    to the *source* header that feeds it (``{target_header: source_header}``).
    A target column with neither an override nor a name match gets ``""`` — so a
    server-wide / form row copies into the alliance sheet's own layout cleanly
    even when the two sheets order or name their columns differently."""
    src_idx = header_index(source_header)
    cmap = copy_map or {}
    out = []
    for h in target_header:
        i = None
        mapped = cmap.get(h)
        if mapped:
            i = src_idx.get(_norm_header(mapped))
        if i is None:
            i = src_idx.get(_norm_header(h))
        if i is not None and i < len(source_row):
            cell = source_row[i]
            out.append(cell if isinstance(cell, str) else str(cell))
        else:
            out.append("")
    return out


def select_rows_to_copy(
    source_rows: list,
    source_hidx: dict,
    source_map: dict,
    *,
    already_copied: set,
    filter_obj=None,
) -> tuple[list, set]:
    """Pick rows from an optional source sheet (server-wide pool or intake
    form) to copy into the alliance's own sheet.

    ``source_hidx`` is the source sheet's :func:`header_index`; ``source_map``
    carries its ``name`` / ``identity_extra`` for dedup. Returns
    ``(rows_to_copy, updated_copied)``: rows that pass ``filter_obj`` and whose
    identity isn't already in ``already_copied``, plus the grown hash set. The
    set dedups across polls so a matching row is copied once even while it
    lingers in the source sheet. Rows are copied *whole* (every column) by the
    caller — the map here is only for dedup + filtering.

    The bot only ever copies into the alliance's *own* sheet — never one
    shared with other alliances (DESIGN §1)."""
    rows_to_copy: list = []
    updated = set(already_copied)
    for row in source_rows:
        h = row_identity(row, source_hidx, source_map)
        if h is None:
            continue
        if h in updated:
            continue
        if not evaluate_filter(filter_obj, row, source_hidx):
            continue
        rows_to_copy.append(row)
        updated.add(h)
    return rows_to_copy, updated


def classify_source_rows(source_rows, source_hidx, source_map, *, filter_obj, already_copied):
    """Same selection as :func:`select_rows_to_copy`, but also returns a count
    breakdown for the "Check now" / admin diagnostics. Behaviour is identical
    (filter + copied-state dedup) — this only adds visibility into *why* rows
    were or weren't picked. Returns ``(rows_to_copy, report)`` where ``report``
    is ``{read, matched, already_pulled, to_copy}``:

    - ``read``: rows with a resolvable name (blank-name spacer rows excluded)
    - ``matched``: of those, how many pass ``filter_obj``
    - ``already_pulled``: of the matched, how many are already in
      ``already_copied`` (the copied-state dedup set) and so are skipped
    - ``to_copy``: the remainder, the rows that would actually be appended"""
    read = matched = already_pulled = 0
    rows_to_copy: list = []
    seen = set(already_copied)
    for row in source_rows:
        h = row_identity(row, source_hidx, source_map)
        if h is None:
            continue
        read += 1
        if not evaluate_filter(filter_obj, row, source_hidx):
            continue
        matched += 1
        if h in seen:
            already_pulled += 1
            continue
        rows_to_copy.append(row)
        seen.add(h)
    return rows_to_copy, {
        "read": read,
        "matched": matched,
        "already_pulled": already_pulled,
        "to_copy": len(rows_to_copy),
    }


def plan_blank_fill(
    target_header: list,
    target_rows: list,
    target_map: dict,
    source_header: list,
    source_rows: list,
    source_map: dict,
    *,
    copy_map: dict | None = None,
) -> list:
    """Plan blank-cell enrichment of *existing* alliance rows from a source
    sheet (opt-in, #9). For each alliance row whose identity matches a source
    row, fill any alliance cell that is **blank** with the source's value —
    never overwriting a cell the recruiter already filled in. Identity is each
    sheet's own ``name`` (+ ``identity_extra``) via :func:`row_identity`, so a
    person already on the list gets enriched even if they were never copied.

    Returns a list of ``(row_number, col_index, value)``: ``row_number`` is the
    1-based sheet row (the header is row 1, so the first data row is 2),
    ``col_index`` is 0-based. ``copy_map`` (``{target_header: source_header}``)
    resolves columns the two sheets name differently; unmapped columns fall back
    to a same-name match, mirroring :func:`align_row`."""
    t_hidx = header_index(target_header)
    s_hidx = header_index(source_header)
    cmap = copy_map or {}

    # Source row per identity (first occurrence wins, matching select_rows_to_copy).
    src_by_id: dict = {}
    for srow in source_rows:
        sid = row_identity(srow, s_hidx, source_map)
        if sid is not None:
            src_by_id.setdefault(sid, srow)

    updates: list = []
    for r_i, trow in enumerate(target_rows):
        tid = row_identity(trow, t_hidx, target_map)
        if tid is None:
            continue
        srow = src_by_id.get(tid)
        if srow is None:
            continue
        for c_i, th in enumerate(target_header):
            current = trow[c_i] if c_i < len(trow) else ""
            if str(current).strip():
                continue  # never overwrite an existing value
            mapped = cmap.get(th)
            si = s_hidx.get(_norm_header(mapped)) if mapped else None
            if si is None:
                si = s_hidx.get(_norm_header(th))
            if si is None or si >= len(srow):
                continue
            val = srow[si]
            if not str(val).strip():
                continue  # source has nothing to add
            updates.append((r_i + 2, c_i, str(val)))
    return updates


# ── In-game message templates ────────────────────────────────────────────────


# Placeholders every template can rely on. Beyond these two, an alliance's
# chosen display columns are exposed as placeholders by their field token
# (see field_token), e.g. a "Total Hero Power" display column → {total_hero_power}.
TEMPLATE_PLACEHOLDERS = ("name", "alliance_name")


def field_token(header) -> str:
    """Template-placeholder token for a display column header:
    ``"Total Hero Power"`` → ``"total_hero_power"``. So a template can drop a
    chosen column's value in with ``{total_hero_power}``."""
    return re.sub(r"\s+", "_", _norm_header(header)).strip("_")


class _SafeDict(dict):
    """`str.format_map` backing dict that renders unknown placeholders
    literally (``{nme}`` stays ``{nme}``) instead of raising — same idiom as
    the storm / train / buddy DM renderers. A typo in a saved template can't
    crash the render path."""

    def __missing__(self, key):
        return "{" + key + "}"


def render_transfer_template(template: str, **context) -> str:
    """Substitute ``{name}`` / ``{alliance_name}`` plus any display-column
    tokens into a template body. A *known* placeholder not supplied (or
    ``None``) renders blank; an *unknown* placeholder (a typo) renders
    literally so the mistake shows."""
    safe = _SafeDict({k: "" for k in TEMPLATE_PLACEHOLDERS})
    safe.update({k: ("" if v is None else v) for k, v in context.items()})
    try:
        return str(template).format_map(safe)
    except (ValueError, IndexError):
        # A stray unescaped brace / bad format spec — return the raw template
        # rather than crashing the caller.
        return str(template)


def resolve_template(cfg: dict, kind: str) -> str:
    """The body to render for a template ``kind`` (``apply_invitation`` /
    ``confirm_request`` / ``decline``): the guild's saved override when
    non-empty, else the hardcoded default from ``defaults.py``."""
    if kind not in DEFAULT_TRANSFER_TEMPLATES:
        raise ValueError(f"unknown transfer template kind: {kind!r}")
    saved = (cfg.get(f"template_{kind}") or "").strip()
    return saved or DEFAULT_TRANSFER_TEMPLATES[kind]
