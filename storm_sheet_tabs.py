"""The storm Rosters and Signups tabs: headers, words and house style (#729).

Two writers fill a Rosters tab ("DS Rosters" / "CS Rosters"): the roster
builder's Approve & Post (`storm_roster_builder._write_rosters_tab`) and the
Map Manager write-back (`storm_roster_writeback`). They share this module's
header and cell helpers so both write the same thing. The Signups tab
("DS Signups" / "CS Signups") is the vote log `storm_signup_view` mirrors.

What an officer sees on both tabs:

- **Words, not codes.** Role is "Primary" / "Sub", the roster image's own
  column headers; Override Below Minimum is "Yes"; an unknown power is
  "Unknown"; On Behalf? is "Yes" / "No". Readers accept the old lowercase
  codes too, in any case, so rows written before this keep working. Both
  tabs are append-only, so old rows keep their codes until an officer
  changes them.
- **Real dates and numbers.** Rows are written `USER_ENTERED`, so Event Date
  is a real date, the posted / voted stamps are real date-times in server
  time (`time_helpers.server_stamp`, a game moment), and Power at Assignment
  is a number shown in full (`#,##0`).
- **IDs stay text.** The Discord ID columns are adopted by `sheet_identity`
  (tagged, plain text, shown or hidden per the alliance's answer) before the
  first `USER_ENTERED` write, which would otherwise round an 18-digit ID.
- **Readers address columns by header name**, so every read goes through
  `read_tab`, which finds the date columns by name and returns them as ISO
  text whatever the alliance's locale shows.
"""

from __future__ import annotations

import sheet_format
import sheet_identity
from sheet_words import Words, rename_headers

# ── Rosters ──────────────────────────────────────────────────────────────────

ROSTERS_HEADER = [
    "Event Date",
    "Team",
    "Stage",
    "Zone",
    "Member",
    "Role",
    "Power at Assignment",
    "Discord ID",
    "Override Below Minimum",
    "Paired With",
    "Posted At (server time)",
]

# Header cells renamed in place, only while they still hold the bot's old text.
ROSTERS_RENAMES = {"Posted At (UTC)": "Posted At (server time)"}

# Current header name → the name older tabs carry, for readers and the
# builder's header migration to fall back to. Covers both tabs.
LEGACY_ALIASES = {
    "Override Below Minimum": "Override Below Floor",
    "Posted At (server time)": "Posted At (UTC)",
    "Voted At (server time)": "Voted At (UTC)",
}

ROLE_PRIMARY = "primary"
ROLE_SUB = "sub"
ROLE_WORDS = Words({ROLE_PRIMARY: "Primary", ROLE_SUB: "Sub"})

OVERRIDE_YES = "Yes"
# Officers hand-edit the override column; any of these reads as flagged.
_OVERRIDE_TRUTHY = {"yes", "y", "1", "true", "t", "x"}

POWER_UNKNOWN = "unknown"
POWER_UNKNOWN_WORD = "Unknown"


def override_cell(flagged: bool) -> str:
    return OVERRIDE_YES if flagged else ""


def is_override(cell) -> bool:
    """True for a flagged Override Below Minimum cell, old or new, any case."""
    return str(cell or "").strip().lower() in _OVERRIDE_TRUTHY


def power_cell(power) -> str:
    """Power at Assignment as written: the full number's digits (Sheets
    stores them as a number under `USER_ENTERED`), or "Unknown"."""
    if power is None or power == "":
        return POWER_UNKNOWN_WORD
    try:
        return str(int(power))
    except (TypeError, ValueError):
        return str(power)


def power_code(cell) -> str:
    """A Power at Assignment cell as readers use it: digits, "unknown" for
    either spelling of unknown, or the cell as typed."""
    text = str(cell or "").strip()
    return POWER_UNKNOWN if text.lower() == POWER_UNKNOWN else text


def text_cell(value) -> str:
    """A name written `USER_ENTERED`, kept as the text it is.

    Sheets would read a member called `=Alpha` or `+1` as a formula and
    `007` as the number 7. A leading apostrophe makes Sheets store the cell
    as text; the apostrophe itself is not part of the value."""
    text = str(value or "")
    if text[:1] in ("=", "+", "-", "@") or _numeric(text):
        return "'" + text
    return text


def _numeric(text: str) -> bool:
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True


def reword_roster_row(row: list[str]) -> list[str]:
    """A row from `read_rosters`, laid out as `ROSTERS_HEADER`, ready to be
    written again `USER_ENTERED`: old codes become words and names stay text.
    Event Date and Posted At already read back as ISO text."""
    out = list(row) + [""] * (len(ROSTERS_HEADER) - len(row))

    def at(name: str) -> int:
        return ROSTERS_HEADER.index(name)

    for name in ("Member", "Paired With"):
        out[at(name)] = text_cell(out[at(name)])
    out[at("Role")] = ROLE_WORDS.reword(out[at("Role")])
    if power_code(out[at("Power at Assignment")]) == POWER_UNKNOWN:
        out[at("Power at Assignment")] = POWER_UNKNOWN_WORD
    if is_override(out[at("Override Below Minimum")]):
        out[at("Override Below Minimum")] = OVERRIDE_YES
    return out


def column(header: list[str], name: str) -> int:
    """The 0-based index of a Rosters / Signups column by header name,
    falling back to the name older tabs carry; -1 when absent."""
    clean = [str(h).strip() for h in header]
    for candidate in (name, LEGACY_ALIASES.get(name)):
        if candidate and candidate in clean:
            return clean.index(candidate)
    return -1


def rosters_format(header: list[str]) -> sheet_format.TabSpec:
    """The house style for a Rosters tab with this header.

    Built from the header rather than fixed positions because a tab created
    by the Map Manager write-back before #729 has a shorter header. The
    frozen columns stop at the header row: Member is the fifth column, and
    freezing five columns would fill a phone screen. Role and Override get a
    dropdown of their words because officers correct them by hand."""

    def at(*names: str) -> tuple[int, ...]:
        return tuple(i for i in (column(header, n) for n in names) if i >= 0)

    role, override = column(header, "Role"), column(header, "Override Below Minimum")
    dropdowns = []
    if role >= 0:
        dropdowns.append((role, ROLE_WORDS.options))
    if override >= 0:
        dropdowns.append((override, (OVERRIDE_YES,)))
    return sheet_format.TabSpec(
        date=at("Event Date"),
        quantity=at("Power at Assignment"),
        text=at("Discord ID"),
        datetime=at("Posted At (server time)"),
        dropdowns=tuple(dropdowns),
    )


def prepare_rosters(ws, header: list[str], *, guild_id: int, id_header=None) -> None:
    """Before a Rosters write: rename the old Posted At header and adopt the
    Discord ID column, so the `USER_ENTERED` write that follows keeps IDs as
    text. `header` is the tab's header as read, updated in place;
    `id_header` is the header the write will leave, when it rewrites the
    header (the builder's migration), so the column adopted is the one the
    IDs land in."""
    if header:
        rename_headers(ws, header, ROSTERS_RENAMES)
    id_col = column(id_header or header, "Discord ID")
    if id_col < 0:
        id_col = ROSTERS_HEADER.index("Discord ID")
    sheet_identity.adopt_columns(ws, [id_col], guild_id=guild_id)


def read_rosters(ws) -> list[list[str]]:
    """A Rosters tab's rows with Event Date as `YYYY-MM-DD` and Posted At as
    `YYYY-MM-DD HH:MM:SS`, wherever those columns sit."""
    return read_tab(ws, dates=("Event Date",), stamps=("Posted At (server time)",))


# ── Signups ──────────────────────────────────────────────────────────────────

SIGNUPS_HEADER = [
    "Event Date",
    "Member",
    "Vote",
    "Voter Discord ID",
    "On Behalf?",
    "Voted At (server time)",
]
SIGNUPS_RENAMES = {"Voted At (UTC)": "Voted At (server time)"}
BEHALF_WORDS = Words({"yes": "Yes", "no": "No"})
# Event Date and Member frozen. No dropdowns: the tab is a log of votes the
# bot keeps in its own database, so nothing an officer types here is read
# back except by the clear-votes prune.
SIGNUPS_FORMAT = sheet_format.TabSpec(date=(0,), text=(3,), datetime=(5,), frozen_columns=2)

# Signups tabs whose header and ID column this process has already seen to.
_signups_prepared: set[tuple] = set()


def prepare_signups(ws, *, guild_id: int) -> None:
    """Before a vote is appended: rename the old Voted At header and adopt the
    Voter Discord ID column. A vote is a single row, so this runs once per
    tab per process rather than spending a header read on every vote."""
    key = (getattr(ws, "spreadsheet_id", None), getattr(ws, "id", None))
    if key in _signups_prepared:
        return
    try:
        header = [str(c) for c in ws.row_values(1)]
    except Exception as e:
        print(f"[STORM SIGNUPS] Could not read the header of '{getattr(ws, 'title', '?')}': {e}")
        header = []
    if header:
        rename_headers(ws, header, SIGNUPS_RENAMES)
    sheet_identity.adopt_columns(ws, [SIGNUPS_HEADER.index("Voter Discord ID")], guild_id=guild_id)
    if isinstance(key[1], int):
        _signups_prepared.add(key)


def read_signups(ws) -> list[list[str]]:
    """A Signups tab's rows with Event Date as `YYYY-MM-DD` and Voted At as
    `YYYY-MM-DD HH:MM:SS`."""
    return read_tab(ws, dates=("Event Date",), stamps=("Voted At (server time)",))


# ── Reading by header name ───────────────────────────────────────────────────


class _Rows:
    """Rows already read, in the shape `sheet_format.read_values` reads."""

    def __init__(self, rows):
        self._rows = rows

    def get_all_values(self, **_kwargs):
        return self._rows


def read_tab(ws, *, dates: tuple[str, ...], stamps: tuple[str, ...]) -> list[list[str]]:
    """`sheet_format.read_values` for a tab whose columns are found by header
    name: one unformatted read, then the named date and date-time columns
    turned back into ISO text, every other cell as its plain text."""
    try:
        raw = ws.get_all_values(
            value_render_option="UNFORMATTED_VALUE",
            date_time_render_option="SERIAL_NUMBER",
        )
    except TypeError:
        raw = ws.get_all_values()  # a test double
    if not raw:
        return []
    header = [str(c) for c in raw[0]]

    def at(names: tuple[str, ...]) -> tuple[int, ...]:
        return tuple(i for i in (column(header, n) for n in names) if i >= 0)

    spec = sheet_format.TabSpec(date=at(dates), datetime=at(stamps))
    return sheet_format.read_values(_Rows(raw), spec)
