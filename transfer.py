"""Transfer Management (#16) — pure-logic core.

A passive sheet-watching layer over an alliance's existing
transfer-tracking spreadsheet. This module holds the side-effect-free
core: column-map addressing, the AND-only filter DSL, change detection
(identity hashing + status snapshots), and in-game message template
rendering. It imports nothing from ``discord`` so it stays trivially
unit-testable.

The Discord-facing wiring (the ``/setup_transfers`` wizard, the poll
loop, notification embeds, the ``/transfers`` viewer) lives in
``setup_cog.py`` / ``transfer_cog.py`` and calls into here.

Design ground rules (see ``notes/DESIGN_transfer_management.md``):

- **The bot is a watcher, not a replacement.** It never writes tracking
  columns back to the alliance sheet. Change detection is therefore
  done from row content alone (Option A — content hashing), not from a
  bot-written marker column.
- **Only the Member name column is required.** Every other column is
  optional; behaviour scales to whatever the alliance maps. A column map
  is a dict like::

      {"member": "A", "power": "G", "tier": "B", "want": "F",
       "confirmed": "I", "declined": "K", "notes": "Z",
       "extras": [{"label": "Bear vs Lion", "letter": "Y"}, ...]}

- **Degrade soft.** A filter that references a column which is no longer
  mapped (or whose cell is blank/unparseable) errs toward *notifying*
  rather than silently dropping an applicant — for a recruiting tool,
  over-notifying beats missing someone.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import NamedTuple

from defaults import DEFAULT_TRANSFER_TEMPLATES

# Logical column keys that live at the top level of a column map. Anything
# else a filter references is looked up among the free-form ``extras``.
STATUS_KEYS = ("want", "confirmed", "declined")
KNOWN_KEYS = ("member", "power", "tier", *STATUS_KEYS, "notes")


# ── Column addressing ────────────────────────────────────────────────────────


def col_letter_to_index(letter: str) -> int | None:
    """Spreadsheet column letter → 0-based index (``A`` → 0, ``Z`` → 25,
    ``AA`` → 26). Returns ``None`` for an empty / non-alphabetic value so
    callers can treat "no column" distinctly from "column A".

    A local copy of the AA+-aware converter (rather than importing
    ``setup_cog``) keeps this module free of the Discord dependency.
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


def parse_column_map(raw) -> dict:
    """Decode a stored ``*_column_map_json`` value into a dict. Tolerates
    ``None`` / empty / malformed JSON by returning ``{}`` so a corrupt
    saved map degrades to "only the rows, no addressable columns" rather
    than crashing the poll loop."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def column_index(column_map: dict, key: str) -> int | None:
    """0-based sheet index for a logical column ``key`` in a column map,
    or ``None`` if the key isn't mapped. Checks the top-level keys first
    (``member``/``power``/``tier``/status/``notes``), then the free-form
    ``extras`` list by case-insensitive label match."""
    if not key:
        return None
    letter = column_map.get(key)
    if isinstance(letter, str) and letter.strip():
        return col_letter_to_index(letter)
    target = key.strip().lower()
    for extra in column_map.get("extras", []) or []:
        if not isinstance(extra, dict):
            continue
        if str(extra.get("label", "")).strip().lower() == target:
            return col_letter_to_index(str(extra.get("letter", "")))
    return None


def cell_value(row: list, column_map: dict, key: str) -> str | None:
    """The trimmed string in ``row`` for logical column ``key``, or
    ``None`` when the key isn't mapped or the row is too short to reach it.
    ``None`` is the "can't address this" signal that filter and snapshot
    logic treat as soft-degrade."""
    idx = column_index(column_map, key)
    if idx is None or idx < 0 or idx >= len(row):
        return None
    value = row[idx]
    return value.strip() if isinstance(value, str) else str(value).strip()


# ── Value coercion ───────────────────────────────────────────────────────────

_NUM_SUFFIX = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}
_TRUTHY = {"true", "yes", "y", "x", "1", "✓", "✔", "✅", "confirmed", "done"}
_FALSY = {"false", "no", "n", "0", "", "✗", "✘", "❌"}


def coerce_number(value) -> float | None:
    """Parse a numeric value tolerant of recruiter shorthand: thousands
    commas and ``K`` / ``M`` / ``B`` magnitude suffixes (``"199M"`` →
    199000000, ``"1.2b"`` → 1200000000, ``"304,743,912"`` → that integer).
    Returns ``None`` when the value can't be read as a number.

    Both sides of a numeric filter comparison go through this, so the
    recruiter can type the threshold in the same units the sheet displays
    (``100M``) and it lines up with a stored ``199M`` cell.
    """
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        return None
    s = value.strip().lower().replace(",", "")
    if not s:
        return None
    mult = 1
    if s[-1] in _NUM_SUFFIX:
        mult = _NUM_SUFFIX[s[-1]]
        s = s[:-1].strip()
    try:
        return float(s) * mult
    except ValueError:
        return None


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


def _eval_clause(clause: dict, row: list, column_map: dict) -> bool:
    """Evaluate one filter clause. Unaddressable column or unparseable
    value → ``True`` (soft pass: don't drop the applicant)."""
    if not isinstance(clause, dict):
        return True
    col = clause.get("column", "")
    op = clause.get("op", "")
    target = clause.get("value")

    cell = cell_value(row, column_map, col)
    if cell is None:
        # Column no longer mapped / row too short — degrade soft.
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


def evaluate_filter(filter_obj, row: list, column_map: dict) -> bool:
    """``True`` if ``row`` passes the (AND-of-clauses) filter. A falsy
    filter passes everything. ``filter_obj`` may be the parsed dict or the
    raw stored JSON string."""
    if isinstance(filter_obj, str):
        filter_obj = parse_filter(filter_obj)
    if not filter_obj:
        return True
    clauses = filter_obj.get("and") or []
    return all(_eval_clause(c, row, column_map) for c in clauses)


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


def row_identity(
    row: list, column_map: dict, *, alliance: str = "", server: str = ""
) -> str | None:
    """Identity hash for a sheet row, or ``None`` when the row has no
    member name (a blank/spacer row we should skip). ``alliance`` and
    ``server`` are supplied by the caller — the alliance is constant for an
    alliance's own sheet, and a server value comes from a mapped column
    when present."""
    member = cell_value(row, column_map, "member")
    if not member:
        return None
    return identity_hash(member, alliance, server)


def status_snapshot(row: list, column_map: dict) -> dict:
    """Snapshot of a row's mapped status columns, keyed by logical name.
    Stores the raw trimmed cell text (not a coerced bool) so any change —
    ``""`` → ``"Confirmed"``, ``false`` → ``true`` — is detected without
    depending on truthiness interpretation. Only keys actually mapped on
    the sheet are included."""
    snap = {}
    for key in STATUS_KEYS:
        value = cell_value(row, column_map, key)
        if value is not None:
            snap[key] = value
    return snap


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


class PollDiff(NamedTuple):
    new_applicants: list  # list[NewApplicant]
    status_changes: list  # list[StatusChange]
    next_state: dict  # {hash: status_snapshot} to persist


def compute_poll_diff(
    rows: list,
    column_map: dict,
    *,
    prior_state: dict,
    filter_obj=None,
    alliance: str = "",
    baseline: bool = False,
) -> PollDiff:
    """Diff one poll of an alliance sheet against the last-seen state.

    ``rows`` are the data rows (header already stripped). Returns a
    :class:`PollDiff` of the new applicants to announce, the status changes
    to announce, and the ``next_state`` to persist.

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
    - **Seen last time, absent now** → row deleted; quietly forgotten (left
      out of ``next_state``, no notification).
    - **baseline=True** → bookmark every current row into ``next_state`` and
      announce nothing. Used for the silent first read at setup so existing
      rows don't flood the channel.

    Blank rows (no member name) and exact duplicate identities collapse
    safely (last snapshot wins).
    """
    prior_state = prior_state or {}
    next_state: dict = {}
    new_applicants: list = []
    status_changes: list = []

    for row in rows:
        member = cell_value(row, column_map, "member")
        if not member:
            continue  # blank / spacer row
        server = cell_value(row, column_map, "server") or ""
        row_alliance = cell_value(row, column_map, "alliance") or alliance
        h = identity_hash(member, row_alliance, server)
        snap = status_snapshot(row, column_map)
        seen_before = h in prior_state
        next_state[h] = snap

        if baseline:
            continue
        if not seen_before:
            if evaluate_filter(filter_obj, row, column_map):
                new_applicants.append(NewApplicant(h, row, snap))
        else:
            changes = diff_status(prior_state[h], snap)
            if changes:
                status_changes.append(StatusChange(h, row, changes, snap))

    return PollDiff(new_applicants, status_changes, next_state)


def select_rows_to_copy(
    source_rows: list,
    source_map: dict,
    *,
    already_copied: set,
    filter_obj=None,
    alliance: str = "",
) -> tuple[list, set]:
    """Pick rows from an optional source sheet (server-wide pool or intake
    form) that should be copied into the alliance's own sheet.

    Returns ``(rows_to_copy, updated_copied)`` where ``rows_to_copy`` are the
    source rows that pass ``filter_obj`` and whose identity hash isn't in
    ``already_copied``, and ``updated_copied`` is ``already_copied`` plus the
    hashes of the rows being copied. The hash set dedups across polls so a
    matching row is copied once even though it lingers in the source sheet.

    The bot only ever copies *into the alliance's own* sheet — never into a
    sheet shared with other alliances (DESIGN §1).
    """
    rows_to_copy: list = []
    updated = set(already_copied)
    for row in source_rows:
        member = cell_value(row, source_map, "member")
        if not member:
            continue
        server = cell_value(row, source_map, "server") or ""
        row_alliance = cell_value(row, source_map, "alliance") or alliance
        h = identity_hash(member, row_alliance, server)
        if h in updated:
            continue
        if not evaluate_filter(filter_obj, row, source_map):
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
