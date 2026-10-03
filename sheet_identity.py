"""Discord ID columns on the tabs that name members (#723).

A member who renames on Discord or in game is still the same person, but a
tab that only stores their name can't tell. Every tab the bot writes that
names a member therefore carries a Discord ID column, and every feature
finds a member's row the same way: by Discord ID first, then by name. This
module is that one way; no feature keeps its own copy.

- **Where the IDs come from.** The synced roster (`member_roster`), or the
  tab and columns the alliance named in `/setup` as where it keeps its
  members' Discord IDs. Read once per write. The name being written is current at that moment, so its
  roster ID is the right one to store beside it.
- **Finding the column.** The column carries a `sheet_tags` tag, so the bot
  finds it wherever an officer moves it and whatever they rename it to. A
  column headed "Discord ID" from before tags existed is adopted on the next
  write. Never by position.
- **Rows written before this** have a name and no ID. `fill_ids` gives them
  one as the bot rewrites a tab, `stamp_ids` as it reads one, both from the
  roster by name; from then on the ID is what matches.
- **IDs are text.** The column is formatted as plain text when the bot adds
  or adopts it, so Sheets never turns an 18-digit ID into a rounded number.
- **Shown or hidden** follows the alliance's choice in `/setup` (default
  hidden). A new column takes the setting when it is added; changing the
  setting applies it to every tagged ID column in the spreadsheet.
- **Bot tabs or every tab.** Bot-created tabs always get the column: the bot
  needs it. Whether tabs the alliance made (its birthday list, its storm
  roster) get one too is the alliance's choice, `Settings.alliance_tabs`.

Everything that touches the Sheet here is best-effort: a failed tag read
means "no column found yet", a failed format or hide logs and leaves the
column working, and a roster that can't be read means every lookup falls
back to the name, which is how every tab worked before this.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

import sheet_tags

ID_HEADER = "Discord ID"

# One tag for every member Discord ID column, whatever tab it is on. A tab has
# at most one, so the tab's role doesn't need to be in it.
TAG = {"t": "member", "k": "discord_id"}

# Growth's ID columns predate this module (#418) and carry their own tags.
# The show / hide setting covers them too; growth moves onto `TAG` when it
# moves onto this module.
_OTHER_ID_TAGS = {("growth", "id"), ("breakdown", "id")}

SCOPE_BOT_TABS = "bot"
SCOPE_ALL_TABS = "all"


# ── The alliance's choice ────────────────────────────────────────────────────


@dataclass(frozen=True)
class Settings:
    """The two `/setup` answers. The defaults are what an alliance that never
    answered gets: the bot's own tabs only, columns hidden."""

    alliance_tabs: bool = False
    shown: bool = False
    answered: bool = False


def settings_for(guild_id: int) -> Settings:
    """The guild's answers, or the defaults when it has none or the read fails."""
    try:
        import config

        cfg = config.get_config(guild_id)
    except Exception as e:
        print(f"[SHEET IDS] Could not read the ID column settings for guild {guild_id}: {e}")
        return Settings()
    scope = (getattr(cfg, "id_columns_scope", "") or "") if cfg else ""
    if not scope:
        return Settings()
    return Settings(
        alliance_tabs=scope == SCOPE_ALL_TABS,
        shown=bool(getattr(cfg, "id_columns_shown", 0)),
        answered=True,
    )


# ── Who is who ───────────────────────────────────────────────────────────────


def _norm(name: str) -> str:
    return (name or "").strip().lower()


@dataclass
class Roster:
    """The alliance roster, both ways: a name to its Discord ID, and an ID to
    the member's current name."""

    by_name: dict[str, str] = field(default_factory=dict)
    by_id: dict[str, str] = field(default_factory=dict)

    def id_for(self, name: str) -> str:
        """The roster ID for this name (either spelling), or ""."""
        return self.by_name.get(_norm(name), "")

    def name_for(self, identity: str) -> str:
        """The current roster name for this ID, or ""."""
        return self.by_id.get((identity or "").strip(), "")

    def current_name(self, name: str, identity: str) -> str:
        """The name to show and count a row under: the roster's current name
        when the row's ID is on the roster, else the name the row stores."""
        return self.name_for(identity) or name


def build_roster(rows: list[tuple[str, str, str]]) -> Roster:
    """A `Roster` from `(identity, name, display name)` rows.

    The display name is the current name when there is one, as the rest of
    the bot shows it. An identity on more than one row is no identity at all
    (an alliance whose ID column holds ranks, say), so it is left out rather
    than merging different members into one."""
    seen: dict[str, int] = {}
    for identity, _name, _display in rows:
        seen[identity] = seen.get(identity, 0) + 1
    roster = Roster()
    for identity, name, display in rows:
        if seen[identity] > 1:
            continue
        current = (display or name or "").strip()
        if current:
            roster.by_id.setdefault(identity, current)
        for spelling in (name, display):
            if _norm(spelling):
                roster.by_name.setdefault(_norm(spelling), identity)
    return roster


def load_roster(guild_id: int) -> Roster:
    """The guild's roster identities. Never raises; empty when unreadable.

    Two sources, the first that applies: a synced roster (Premium Member
    Sync), which gives every identity in its ID column, hand-kept keys for
    members not on Discord included; else the tab and columns the alliance
    named in `/setup` as where it keeps its members' Discord IDs. An
    alliance with neither has no IDs, and every match falls back to names."""
    try:
        import config
        import member_roster

        if config.get_member_roster_config(guild_id).get("enabled"):
            return build_roster(member_roster.roster_identity_rows(guild_id))
        source = id_source(guild_id)
        if source is None:
            return Roster()
        tab, id_col, name_col = source
        return build_roster(source_rows(_read_source(guild_id, tab), id_col, name_col))
    except Exception as e:
        print(f"[SHEET IDS] Could not load roster identities for guild {guild_id}: {e}")
        return Roster()


def id_source(guild_id: int) -> tuple[str, int, int] | None:
    """`(tab, ID column, name column)` the alliance keeps Discord IDs in, as
    answered in `/setup`, or None when they don't keep them."""
    import config

    cfg = config.get_config(guild_id)
    tab = (getattr(cfg, "id_source_tab", "") or "").strip() if cfg else ""
    if not tab:
        return None
    id_col = int(getattr(cfg, "id_source_id_col", -1))
    name_col = int(getattr(cfg, "id_source_name_col", -1))
    if id_col < 0 or name_col < 0 or id_col == name_col:
        return None
    return tab, id_col, name_col


_SNOWFLAKE = re.compile(r"^\d{17,20}$")


def source_rows(values: list[list[str]], id_col: int, name_col: int) -> list[tuple[str, str, str]]:
    """`(identity, name, "")` from the alliance's own Discord ID tab.

    Only a cell shaped like a Discord ID counts, so the header row, a blank
    or a note like "left" never becomes someone's identity."""
    out: list[tuple[str, str, str]] = []
    for row in values or []:
        identity, name = cell(row, id_col), cell(row, name_col)
        if name and _SNOWFLAKE.match(identity):
            out.append((identity, name, ""))
    return out


# The alliance's Discord ID tab changes when an officer adds a member, not
# from one write to the next, so a short cache keeps a burst of writes to
# one read. Opened, never created: a tab name with a typo reads as empty.
_SOURCE_TTL_S = 60.0
_source_cache: dict[tuple[int, str], tuple[float, list]] = {}


def _read_source(guild_id: int, tab: str) -> list[list[str]]:
    key = (guild_id, tab)
    hit = _source_cache.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < _SOURCE_TTL_S:
        return hit[1]
    import config

    sh = config.get_spreadsheet(guild_id)
    values = sh.worksheet(tab).get_all_values() if sh is not None else []
    _source_cache[key] = (now, values)
    return values


def same_member(name_a: str, id_a: str, name_b: str, id_b: str) -> bool:
    """Discord ID first, then name: two IDs decide it outright, either way;
    a missing ID on either side falls back to the names."""
    id_a, id_b = (id_a or "").strip(), (id_b or "").strip()
    if id_a and id_b:
        return id_a == id_b
    return bool(_norm(name_a)) and _norm(name_a) == _norm(name_b)


def cell(row: list[str], idx: int) -> str:
    """The stripped text of `row[idx]`, or "" when the row is short or `idx` < 0."""
    return str(row[idx]).strip() if row and 0 <= idx < len(row) else ""


def set_cell(row: list[str], idx: int, value: str) -> None:
    """Write a cell into an in-memory row, padding a short row first."""
    if idx < 0:
        return
    if len(row) <= idx:
        row.extend([""] * (idx + 1 - len(row)))
    row[idx] = value


class RowIndex:
    """A tab's rows indexed by Discord ID and by name, for finding a member's
    row the shared way. Generalised from growth's `build_row_maps` /
    `resolve_row` (#418).

    `rows` is the data region (header stripped); `first_row` is the sheet row
    number of `rows[0]`. An earlier row wins over a later duplicate, so a tab
    that already holds two rows for someone keeps resolving to the first."""

    def __init__(self, rows: list[list[str]], *, name_col: int, id_col: int, first_row: int = 2):
        self.by_id: dict[str, int] = {}
        self.by_name: dict[str, int] = {}
        for offset, row in enumerate(rows):
            name = cell(row, name_col)
            identity = cell(row, id_col)
            if not name and not identity:
                continue
            at = first_row + offset
            if name:
                self.by_name.setdefault(_norm(name), at)
            if identity:
                self.by_id.setdefault(identity, at)

    def find(self, name: str, identity: str) -> int | None:
        """The member's sheet row: by ID first, then by name, or None."""
        identity = (identity or "").strip()
        if identity and identity in self.by_id:
            return self.by_id[identity]
        return self.by_name.get(_norm(name))


# ── Rows written before IDs ──────────────────────────────────────────────────


def fill_ids(rows: list[list[str]], *, name_col: int, id_col: int, roster: Roster) -> list[int]:
    """Give each row with a name and no ID its roster ID, in memory.

    Returns the offsets of the rows it changed. For a writer that rewrites
    the whole tab anyway: the IDs ride along with the write it was making."""
    changed: list[int] = []
    if id_col < 0 or not roster.by_name:
        return changed
    for offset, row in enumerate(rows):
        if cell(row, id_col):
            continue
        identity = roster.id_for(cell(row, name_col))
        if identity:
            set_cell(row, id_col, identity)
            changed.append(offset)
    return changed


def stamp_ids(
    ws, rows: list[list[str]], *, name_col: int, id_col: int, roster: Roster, first_row: int = 2
) -> int:
    """`fill_ids`, then write just the cells it filled, in one call.

    For a reader: the tab isn't being rewritten, but a row the roster can name
    should carry its ID before the member renames and the name stops matching.
    Returns how many rows were stamped; 0 when the write fails (logged), and
    the rows still carry the IDs in memory for this read."""
    changed = fill_ids(rows, name_col=name_col, id_col=id_col, roster=roster)
    if not changed:
        return 0
    letter = _col_letter(id_col + 1)
    try:
        ws.batch_update(
            [{"range": f"{letter}{first_row + i}", "values": [[rows[i][id_col]]]} for i in changed],
            value_input_option="RAW",
        )
    except Exception as e:
        print(f"[SHEET IDS] Could not stamp {len(changed)} ID(s) on '{_title(ws)}': {e}")
        return 0
    return len(changed)


# A reader stamps a tab at most this often. A row whose name isn't on the
# roster (a member who left) stays unstamped for good, and without this every
# read of the tab would spend a roster read finding that out again.
STAMP_INTERVAL_S = 30 * 60
_last_stamped: dict[tuple, float] = {}


def maybe_stamp(
    ws,
    rows: list[list[str]],
    *,
    guild_id: int,
    name_col: int,
    id_col: int,
    first_row: int = 2,
    roster: Roster | None = None,
) -> int:
    """`stamp_ids` for a reader: only when a row needs an ID, and at most once
    per tab every `STAMP_INTERVAL_S`. Loads the roster unless given one."""
    if id_col < 0 or not any(cell(r, name_col) and not cell(r, id_col) for r in rows):
        return 0
    key = _sheet_key(ws)
    now = time.monotonic()
    if key is not None and now - _last_stamped.get(key, -STAMP_INTERVAL_S) < STAMP_INTERVAL_S:
        return 0
    if key is not None:
        _last_stamped[key] = now
    if roster is None:
        roster = load_roster(guild_id)
    return stamp_ids(ws, rows, name_col=name_col, id_col=id_col, roster=roster, first_row=first_row)


# ── The column itself ────────────────────────────────────────────────────────

# Where each tab's ID column was last found, so a read doesn't spend a tag
# search every time. An entry holds the column's header text too, and is only
# trusted while the header at that index still says the same: a column an
# officer moved shows up as a different header there, and the tags are read
# again.
_COLUMN_TTL_S = 10 * 60
_found: dict[tuple, tuple[float, int, str]] = {}


def _sheet_key(ws) -> tuple | None:
    sheet_id = getattr(ws, "id", None)
    spreadsheet = getattr(ws, "spreadsheet_id", None)
    if not isinstance(sheet_id, int) or not isinstance(spreadsheet, str):
        return None
    return (spreadsheet, sheet_id)


def _cached(ws, header: list[str]) -> int:
    key = _sheet_key(ws)
    hit = _found.get(key) if key is not None else None
    if hit is None:
        return -1
    at, idx, text = hit
    if time.monotonic() - at > _COLUMN_TTL_S or cell(header, idx) != text:
        return -1
    return idx


def _remember(ws, header: list[str], idx: int) -> None:
    key = _sheet_key(ws)
    if key is not None and idx >= 0:
        _found[key] = (time.monotonic(), idx, cell(header, idx))


def locate_column(ws, header: list[str], *, sh=None) -> int:
    """The tab's Discord ID column (0-based), or -1. For a reader: never adds
    or tags a column, so reading a tab never changes it."""
    idx = _cached(ws, header)
    if idx >= 0:
        return idx
    idx, untagged = find_column(header, read_tags(ws, sh))
    if not untagged:
        _remember(ws, header, idx)
    return idx


def is_id_tag(tag: dict) -> bool:
    """True for the shared ID tag."""
    return isinstance(tag, dict) and tag.get("t") == TAG["t"] and tag.get("k") == TAG["k"]


def find_column(header: list[str], tags: dict[int, dict] | None) -> tuple[int, bool]:
    """Where the tab's Discord ID column is: `(index, untagged)`.

    By tag first. Failing that, a column headed "Discord ID" from before tags
    existed, returned with `untagged` True so the writer tags it. `(-1, False)`
    when there is neither."""
    for idx, tag in sorted((tags or {}).items()):
        if is_id_tag(tag):
            return idx, False
    for idx, h in enumerate(header or []):
        if (h or "").strip().lower() == ID_HEADER.lower():
            return idx, True
    return -1, False


def read_tags(ws, sh=None) -> dict[int, dict]:
    """The column tags on `ws`, or {} when they can't be read."""
    sh = sh or getattr(ws, "spreadsheet", None)
    if sh is None:
        return {}
    return sheet_tags.tags_for_sheet(sheet_tags.read_column_tags(sh), ws)


def ensure_column(
    ws,
    header: list[str],
    *,
    guild_id: int,
    sh=None,
    tags: dict[int, dict] | None = None,
    header_row: int = 1,
) -> int:
    """The tab's Discord ID column (0-based), adding one if it has none.

    `header` is the tab's header row as read; a new column goes at its right
    edge (so past any column an officer added), and `header` is extended in
    place so a caller that rewrites its header keeps the new one. The bot
    writes the header cell, tags the column, formats it as text and hides it
    unless the alliance chose to show ID columns. A column found by its
    header is tagged and formatted; its visibility follows the alliance's
    answer once they have given one, and is left alone until then. `tags` is
    a `read_tags` result to reuse."""
    if tags is None:
        idx = _cached(ws, header)
        if idx >= 0:
            return idx
        tags = read_tags(ws, sh)
    idx, untagged = find_column(header, tags)
    if idx >= 0 and not untagged:
        _remember(ws, header, idx)
        return idx
    sheet_id = getattr(ws, "id", None)
    settings = settings_for(guild_id)
    if idx >= 0:
        # An ID column from before tags: tagged and made text. Its visibility
        # changes only once the alliance has said how they want it.
        hide = (not settings.shown) if settings.answered else None
        _apply(ws, sh, _column_requests(sheet_id, idx, header_row, hide=hide), "adopt")
        _remember(ws, header, idx)
        return idx

    idx = len(header)
    header.append(ID_HEADER)
    try:
        sheet_tags.ensure_columns(ws, idx + 1)
        ws.update_cell(header_row, idx + 1, ID_HEADER)
    except Exception as e:
        print(f"[SHEET IDS] Could not add the ID column header on '{_title(ws)}': {e}")
    _apply(ws, sh, _column_requests(sheet_id, idx, header_row, hide=not settings.shown), "add")
    _remember(ws, header, idx)
    return idx


# Tabs whose own ID columns have been adopted in this process.
_adopted: set[tuple] = set()


def adopt_columns(ws, columns: list[int], *, guild_id: int, sh=None, header_row: int = 1) -> None:
    """Adopt Discord ID columns a tab already has at fixed places (#723/#729).

    For a tab whose layout puts its IDs where the bot always writes them
    (Buddy's three, a Rosters or Signups tab's voter column) rather than in
    one column `ensure_column` adds. Each column is tagged, so the show / hide
    setting reaches it, and formatted as text, so an ID is never rounded. Its
    visibility follows the alliance's answer once they have given one. Runs
    once per tab per process; columns already tagged are left alone."""
    key = _sheet_key(ws)
    if key is not None and key in _adopted:
        return
    sheet_id = getattr(ws, "id", None)
    if sheet_id is None:
        return
    tags = read_tags(ws, sh)
    settings = settings_for(guild_id)
    hide = (not settings.shown) if settings.answered else None
    requests: list[dict] = []
    for idx in columns:
        if idx < 0 or is_id_tag(tags.get(idx) or {}):
            continue
        requests += _column_requests(sheet_id, idx, header_row, hide=hide)
    if requests:
        _apply(ws, sh, requests, "adopt")
    if key is not None:
        _adopted.add(key)


def _column_requests(sheet_id, idx: int, header_row: int, *, hide: bool | None) -> list[dict]:
    """Tag, text format and (optionally) visibility for one ID column."""
    if sheet_id is None:
        return []
    requests = sheet_tags.create_requests(sheet_id, {idx: dict(TAG)})
    requests.append(
        {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": header_row,
                    "startColumnIndex": idx,
                    "endColumnIndex": idx + 1,
                },
                "cell": {"userEnteredFormat": {"numberFormat": {"type": "TEXT"}}},
                "fields": "userEnteredFormat.numberFormat",
            }
        }
    )
    if hide is not None:
        requests.append(_visibility_request(sheet_id, idx, shown=not hide))
    return requests


def _visibility_request(sheet_id, idx: int, *, shown: bool) -> dict:
    return {
        "updateDimensionProperties": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "COLUMNS",
                "startIndex": idx,
                "endIndex": idx + 1,
            },
            "properties": {"hiddenByUser": not shown},
            "fields": "hiddenByUser",
        }
    }


def _apply(ws, sh, requests: list[dict], what: str) -> None:
    sh = sh or getattr(ws, "spreadsheet", None)
    if not requests or sh is None:
        return
    try:
        sh.batch_update({"requests": requests})
    except Exception as e:
        print(f"[SHEET IDS] Could not {what} the ID column on '{_title(ws)}': {e}")


def apply_visibility(guild_id: int, *, shown: bool) -> int:
    """Show or hide every Discord ID column the bot has tagged in the guild's
    spreadsheet, in one call. Run when the alliance changes the setting.

    Returns how many columns it set; 0 when there is no sheet or a call fails
    (logged): the setting still applies to every column added from now on."""
    try:
        import config

        sh = config.get_spreadsheet(guild_id)
    except Exception as e:
        print(f"[SHEET IDS] Could not open the sheet for guild {guild_id}: {e}")
        return 0
    if sh is None:
        return 0
    requests = []
    for sheet_id, columns in sheet_tags.read_column_tags(sh).items():
        for idx, tag in columns.items():
            if is_id_tag(tag) or (tag.get("t"), tag.get("k")) in _OTHER_ID_TAGS:
                requests.append(_visibility_request(sheet_id, idx, shown=shown))
    if not requests:
        return 0
    try:
        sh.batch_update({"requests": requests})
    except Exception as e:
        print(f"[SHEET IDS] Could not set ID column visibility for guild {guild_id}: {e}")
        return 0
    return len(requests)


def _title(ws) -> str:
    return getattr(ws, "title", "?")


def _col_letter(n: int) -> str:
    """1-based column index → letter (1→A, 27→AA)."""
    out = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


# ── Tabs the alliance made ───────────────────────────────────────────────────


def alliance_tab_column(
    ws, rows: list[list[str]], *, guild_id: int, name_col: int, first_row: int
) -> int:
    """The Discord ID column on a tab the alliance made (its birthday list, its
    storm roster), adding and filling one if the alliance chose that in
    `/setup`. Returns the column, or -1 when there is none.

    `rows` is the whole tab as read and `first_row` the sheet row its data
    starts on; the row above it is the header the new column is named in. A
    tab whose data starts on row 1 has no header row and gets no column. Nor
    does any tab while the roster has no IDs to give, so an alliance without
    Member Sync never finds an empty column in its own tab. The tab the IDs
    come from is never given a second column."""
    header_row = first_row - 1
    header = list(rows[header_row - 1]) if 0 < header_row <= len(rows) else []
    if header_row < 1:
        return -1
    if not settings_for(guild_id).alliance_tabs or _is_roster_tab(guild_id, ws):
        return locate_column(ws, header)
    roster = load_roster(guild_id)
    if not roster.by_name:
        return locate_column(ws, header)
    id_col = ensure_column(ws, header, guild_id=guild_id, header_row=header_row)
    maybe_stamp(
        ws,
        rows[first_row - 1 :],
        guild_id=guild_id,
        name_col=name_col,
        id_col=id_col,
        first_row=first_row,
        roster=roster,
    )
    return id_col


def _is_roster_tab(guild_id: int, ws) -> bool:
    """True for a tab the IDs come from: the roster, or the alliance's own
    Discord ID tab. Neither gets a second ID column."""
    try:
        import config

        sources = [config.get_member_roster_config(guild_id).get("tab_name") or "Member Roster"]
        source = id_source(guild_id)
        if source is not None:
            sources.append(source[0])
    except Exception:
        return True  # can't tell, so leave the tab alone
    return _title(ws).strip().lower() in {t.strip().lower() for t in sources}
