"""
storm_log.py — DS and CS event logging

Reached via the `📊 Fill out participation questions` button (log
write) and the `📜 View past participation logs` button (read) on
`/desertstorm` and `/canyonstorm` (hub-restructure #187).

Writes to the "DS-CS Sit-outs" tab in Google Sheets and posts a
summary to the dedicated log thread configured for this guild.

DS columns:
  Date | Event | Vote Count | RTF No Vote | Sitting Out | Prior Sit-Out No Request

CS columns:
  Date | Event | (blank) | (blank) | Sitting Out | Prior Sit-Out No Request
"""

import asyncio
import json
import os
from datetime import date

import discord
from messages import FEATURE_NOT_CONFIGURED, NOT_SET_UP
from setup_hub import HUB_BTN_MEMBERS, STORM_SETUP_NAV
from storm_event_hub import HUB_COMMAND, HUB_BTN_PARTICIPATION
from config import get_config
import wizard_registry

# ── Config ─────────────────────────────────────────────────────────────────────

WIZARD_TIMEOUT = 600  # 10 minutes

# Active log sessions — user_id → asyncio.Event (cancel signal)
# Checked by /cancel in train.py so one command covers everything
active_logs: dict = {}

# ── Sheets helpers ─────────────────────────────────────────────────────────────


def _get_spreadsheet(guild_id: int = None):
    from config import get_spreadsheet

    return get_spreadsheet(guild_id)


def _get_log_sheet(guild_id: int = None, event_type: str | None = None):
    """
    Resolve the worksheet that holds participation rows. Prefers the
    per-event-type `participation_tab_name` from the new config; falls
    back to the legacy `tab_sitouts` shared tab so any data written
    under the pre-rework schema keeps loading via /[event]_log.
    """
    from config import get_config, get_participation_config

    sh = _get_spreadsheet(guild_id)
    if event_type and guild_id:
        pcfg = get_participation_config(guild_id, event_type)
        tab = pcfg.get("tab_name") or ""
        if tab:
            try:
                import gspread

                return sh.worksheet(tab)
            except gspread.WorksheetNotFound:
                # Configured tab doesn't exist — legitimate fall-through
                # case (alliance hasn't created it yet, or renamed it
                # without updating config).
                pass
            except Exception as e:
                # API quota exhaustion, network failure, credential
                # expiry — *not* the same as a missing tab. Falling back
                # to the legacy tab silently could write data to the
                # wrong sheet. Log so the symptom is recoverable.
                print(
                    f"[STORM-LOG] Worksheet({tab!r}) lookup failed for "
                    f"guild {guild_id} ({event_type}): {e}"
                )
    cfg = get_config(guild_id)
    tab = cfg.tab_sitouts if cfg else "DS-CS Sit-outs"
    return sh.worksheet(tab)


# ── Name entry modal ──────────────────────────────────────────────────────────


class NameEntryModal(discord.ui.Modal):
    """
    Popup text box where the user types names comma-separated or one per line.
    Matches against the known roster by exact name or alias (col F in member tab).
    """

    def __init__(self, all_names: list, label: str, alias_map: dict = None):
        super().__init__(title=label[:45])
        self.all_names = all_names
        self.name_map = {n.lower(): n for n in all_names}  # lower → original
        self.alias_map = alias_map or {}  # alias.lower() → original name
        self.confirmed = False
        self.selected = []
        self.unrecognized = []

        self.text_input = discord.ui.TextInput(
            label="Names (comma-separated or one per line)",
            style=discord.TextStyle.paragraph,
            placeholder="e.g. Alice, Bob, Chris, or leave blank and submit for none",
            required=False,
            max_length=1000,
        )
        self.add_item(self.text_input)

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.text_input.value.strip()
        if not raw:
            self.selected = []
            self.unrecognized = []
            self.confirmed = True
            await interaction.response.defer()
            self.stop()
            return

        import re

        parts = [p.strip() for p in re.split(r"[,\n]+", raw) if p.strip()]

        recognized = []
        unrecognized = []
        for part in parts:
            lower = part.lower()
            if lower in self.name_map:
                # Exact case-insensitive match
                recognized.append(self.name_map[lower])
            elif lower in self.alias_map:
                # Alias match (e.g. "INSH4F" or "landers" → full decorated name)
                recognized.append(self.alias_map[lower])
            else:
                unrecognized.append(part)

        self.selected = recognized
        self.unrecognized = unrecognized
        self.confirmed = True
        await interaction.response.defer()
        self.stop()


class UnrecognizedView(discord.ui.View):
    """
    Shown when unrecognized names are submitted. Lets the user save as-is
    (visitor) or go back and re-enter.
    """

    def __init__(self, unrecognized: list):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.unrecognized = unrecognized
        self.save_as_is = False
        self.redo = False

    @discord.ui.button(label="Save as Visitor", style=discord.ButtonStyle.secondary, row=0)
    async def save(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.save_as_is = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()

    @discord.ui.button(label="Re-enter Names", style=discord.ButtonStyle.primary, row=0)
    async def redo_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.redo = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


class NameEntryView(discord.ui.View):
    """
    Shows an Enter Names button that opens a modal.
    If unrecognized names are submitted, asks the user to save as visitor or re-enter.
    Loops until the user is satisfied or skips.
    """

    def __init__(self, all_names: list, label: str, alias_map: dict = None):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.all_names = all_names
        self.label = label
        self.alias_map = alias_map or {}
        self.confirmed = False
        self.selected = []
        self.unrecognized = []

    @discord.ui.button(label="✏️ Enter Names", style=discord.ButtonStyle.primary, row=0)
    async def enter_names(self, interaction: discord.Interaction, button: discord.ui.Button):
        while True:
            modal = NameEntryModal(self.all_names, self.label, self.alias_map)
            await interaction.response.send_modal(modal)
            timed_out = await modal.wait()
            if timed_out or not modal.confirmed:
                return  # Let outer timeout handler deal with it

            recognized = modal.selected
            unrecognized = modal.unrecognized

            if not unrecognized:
                # All names recognized — done
                self.selected = recognized
                self.unrecognized = []
                self.confirmed = True
                for item in self.children:
                    item.disabled = True
                result = (
                    f"**Entered ({len(recognized)}):** {', '.join(recognized)}"
                    if recognized
                    else "*None entered.*"
                )
                try:
                    await interaction.message.edit(content=result, view=self)
                except discord.HTTPException:
                    pass
                self.stop()
                return

            # Some unrecognized — ask what to do
            unrecog_str = ", ".join(unrecognized)
            unrecog_view = UnrecognizedView(unrecognized)
            try:
                await interaction.message.edit(
                    content=(
                        f"⚠️ **Not recognized:** {unrecog_str}\n"
                        "These names aren't in the roster. Are they visitors or did you make a typo?"
                    ),
                    view=unrecog_view,
                )
            except discord.HTTPException:
                pass

            await unrecog_view.wait()

            if unrecog_view.save_as_is:
                # Save recognized + unrecognized (visitors)
                self.selected = recognized
                self.unrecognized = unrecognized
                self.confirmed = True
                for item in self.children:
                    item.disabled = True
                lines = []
                if recognized:
                    lines.append(f"**Entered ({len(recognized)}):** {', '.join(recognized)}")
                if unrecognized:
                    lines.append(f"**Visitors:** {unrecog_str}")
                result = "\n".join(lines) if lines else "*None entered.*"
                try:
                    await interaction.message.edit(content=result, view=self)
                except discord.HTTPException:
                    pass
                self.stop()
                return

            if unrecog_view.redo:
                # Restore the Enter Names button so they can try again
                try:
                    await interaction.message.edit(
                        content="*Re-enter names: press Enter Names again:*",
                        view=self,
                    )
                except discord.HTTPException:
                    pass
                # Loop back to modal
                # Need a new interaction — can't reuse the old one after message edit
                # So we just stop and let the button be clickable again
                return

    @discord.ui.button(label="Skip (none)", style=discord.ButtonStyle.secondary, row=0)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.selected = []
        self.unrecognized = []
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, content="*Skipped: none.*", view=self)
        self.stop()


class ShortSelectView(discord.ui.View):
    """Simple single-page select for short lists (e.g. prior sit-outs, always < 25)."""

    def __init__(self, names: list, label: str):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.confirmed = False
        self.selected = set()

        select = discord.ui.Select(
            placeholder=label,
            options=[discord.SelectOption(label=n, value=n) for n in names],
            min_values=0,
            max_values=len(names),
            row=0,
        )

        async def _cb(interaction: discord.Interaction):
            self.selected = set(select.values)
            await interaction.response.defer()

        select.callback = _cb
        self.add_item(select)

    @discord.ui.button(label="✅ Done", style=discord.ButtonStyle.success, row=1)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()

    @discord.ui.button(label="Skip (none)", style=discord.ButtonStyle.secondary, row=1)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        self.selected = set()
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


def _collect_recent_event_dates(
    guild_id: int,
    event_type: str,
    *,
    limit: int = 6,
) -> list[str]:
    """Return up to `limit` recent event dates (ISO `YYYY-MM-DD`,
    newest first) the officer is likely logging participation for.

    Pulls from two sources so the dropdown stays useful across both
    free-tier and Premium alliances:
      - `storm_signups` (every alliance posting a signup poll
        accumulates dates here)
      - `storm_history.list_event_dates` (structured-flow rosters)

    De-duplicated; malformed dates filtered; returns empty list when
    no historical data exists yet (caller falls back to today / type-
    your-own affordances).
    """
    import datetime as _dt

    candidates: set[str] = set()

    # storm_signups via SQLite. Cheap query (events posted with polls).
    try:
        import config

        with config._get_conn() as conn:
            rows = conn.execute(
                "SELECT DISTINCT event_date FROM storm_signups "
                "WHERE guild_id = ? AND event_type = ? "
                "ORDER BY event_date DESC LIMIT ?",
                (int(guild_id), event_type, limit * 2),
            ).fetchall()
        for r in rows:
            d = (r["event_date"] or "").strip()
            if not d:
                continue
            try:
                _dt.date.fromisoformat(d)
            except ValueError:
                continue
            candidates.add(d)
    except Exception as e:
        print(f"[LOG] _collect_recent_event_dates signups read failed (guild {guild_id}): {e}")

    # Structured-flow rosters (Premium).
    try:
        from storm_history import list_event_dates

        rdates, _errs = list_event_dates(
            guild_id,
            event_type,
            limit=limit * 2,
        )
        candidates.update(d for d in rdates if d)
    except Exception as e:
        print(f"[LOG] _collect_recent_event_dates rosters read failed (guild {guild_id}): {e}")

    return sorted(candidates, reverse=True)[:limit]


class _LogDatePickerView(discord.ui.View):
    """Date picker for the participation log flow (#251).

    Replaces the text-typed date entry with a dropdown of recent
    saved event dates (storm signups + structured rosters), plus
    Today / Yesterday quick picks and a `Type a different date`
    fallback that hands back to the existing `wait_for_msg` path.

    Officer selection lands in:
      - `picked_date`: a `datetime.date` when a listed date was picked
      - `wants_manual`: True when the officer chose to type a date
      - `cancelled`: True on explicit cancel / timeout
    """

    def __init__(self, recent_dates: list[str]):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.picked_date: "date | None" = None
        self.wants_manual: bool = False
        self.cancelled: bool = False
        # `confirmed` participates in the shared `wait_for_view` helper
        # (storm_log's run_log_flow contract): True only after the
        # officer commits to a choice (pick / cancel). A bare timeout
        # leaves it False so the caller can treat the view as expired.
        self.confirmed: bool = False
        self._build(recent_dates)

    def _build(self, recent_dates: list[str]):
        import datetime as _dt

        today = _dt.date.today()
        yesterday = today - _dt.timedelta(days=1)

        options: list[discord.SelectOption] = []
        # Always-present quick picks. Use `today` / `yesterday` as the
        # value so the callback can resolve to a date without re-parsing.
        options.append(
            discord.SelectOption(
                label=f"Today ({today.strftime('%a %b %d')})",
                value="__today__",
                description=today.isoformat(),
            )
        )
        options.append(
            discord.SelectOption(
                label=f"Yesterday ({yesterday.strftime('%a %b %d')})",
                value="__yesterday__",
                description=yesterday.isoformat(),
            )
        )

        # Recent saved event dates. Skip today/yesterday if they're
        # already in the saved list (avoid duplicates).
        skip = {today.isoformat(), yesterday.isoformat()}
        for d in recent_dates:
            if d in skip:
                continue
            try:
                dt = _dt.date.fromisoformat(d)
            except ValueError:
                continue
            label = dt.strftime("%a %b %d, %Y")
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=d,
                    description=d,
                )
            )
            if len(options) >= 24:  # 24 dates + 1 "type my own" = 25 cap
                break

        # Type-your-own fallback.
        options.append(
            discord.SelectOption(
                label="✏️ Type a different date…",
                value="__manual__",
                description="Free-form date entry",
            )
        )

        select = discord.ui.Select(
            placeholder="Pick the event date…",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

        async def _on_pick(inter: discord.Interaction):
            values = inter.data.get("values") or []
            value = values[0] if values else ""
            if value == "__manual__":
                self.wants_manual = True
            elif value == "__today__":
                self.picked_date = today
            elif value == "__yesterday__":
                self.picked_date = yesterday
            else:
                try:
                    self.picked_date = _dt.date.fromisoformat(value)
                except ValueError:
                    self.wants_manual = True
            self.confirmed = True
            for item in self.children:
                item.disabled = True
            try:
                await inter.response.edit_message(view=self)
            except discord.HTTPException:
                pass
            self.stop()

        select.callback = _on_pick
        self.add_item(select)

    @discord.ui.button(
        label="↩️ Cancel",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def cancel(self, inter: discord.Interaction, _btn):
        self.cancelled = True
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(view=self)
        except discord.HTTPException:
            pass
        self.stop()


class _PaginatedRosterMultiSelectView(discord.ui.View):
    """Roster-wide multi-select with Discord-friendly pagination (#244).

    Discord caps a `Select` at 25 options, so a 60+ member alliance can't
    fit in one component. This view shows one page of up to 25 at a
    time, with Prev / Page X of Y / Next controls. Each page's
    selections are merged into a single `selected_set` that survives
    page flips.

    `preselected` is the initial check state (used by the Premium
    Discord-poll prefill path). Names appearing there are marked
    selected on first render so the officer only needs to toggle
    corrections.
    """

    PAGE_SIZE = 25

    def __init__(
        self,
        names: list[str],
        label: str,
        *,
        preselected: set[str] | None = None,
        prefill_used: bool = False,
    ):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.confirmed = False
        self.names_sorted: list[str] = sorted(names)
        self.label = label
        self.prefill_used = prefill_used
        self.selected_set: set[str] = set(preselected or [])
        self.page = 0
        self.page_count = max(
            1,
            (len(self.names_sorted) + self.PAGE_SIZE - 1) // self.PAGE_SIZE,
        )
        self._build_components()

    # ── Page helpers ────────────────────────────────────────────────────
    def _page_slice(self) -> list[str]:
        start = self.page * self.PAGE_SIZE
        return self.names_sorted[start : start + self.PAGE_SIZE]

    def _build_components(self) -> None:
        """Rebuild the view from scratch — needed when the user flips
        pages, because Discord's Select options can't be reassigned in
        place without losing selection state on the wire."""
        self.clear_items()
        page_names = self._page_slice()
        if not page_names:
            return
        select = discord.ui.Select(
            placeholder=(
                f"{self.label} — page {self.page + 1}/{self.page_count} ({len(page_names)} names)"
            ),
            options=[
                discord.SelectOption(
                    label=n[:100],
                    value=n,
                    default=(n in self.selected_set),
                )
                for n in page_names
            ],
            min_values=0,
            max_values=len(page_names),
            row=0,
        )
        select.callback = self._on_select
        self.add_item(select)
        # Pagination row only when there's more than one page.
        if self.page_count > 1:
            prev_btn = discord.ui.Button(
                label="◀ Prev",
                style=discord.ButtonStyle.secondary,
                disabled=(self.page == 0),
                row=1,
            )
            prev_btn.callback = self._on_prev
            self.add_item(prev_btn)

            page_btn = discord.ui.Button(
                label=f"Page {self.page + 1} / {self.page_count}",
                style=discord.ButtonStyle.secondary,
                disabled=True,
                row=1,
            )
            self.add_item(page_btn)

            next_btn = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                disabled=(self.page >= self.page_count - 1),
                row=1,
            )
            next_btn.callback = self._on_next
            self.add_item(next_btn)

        save_btn = discord.ui.Button(
            label="✅ Save",
            style=discord.ButtonStyle.success,
            row=2,
        )
        save_btn.callback = self._on_save
        self.add_item(save_btn)

        clear_btn = discord.ui.Button(
            label="Clear all",
            style=discord.ButtonStyle.secondary,
            row=2,
        )
        clear_btn.callback = self._on_clear
        self.add_item(clear_btn)

    # ── Callbacks ───────────────────────────────────────────────────────
    async def _on_select(self, interaction: discord.Interaction):
        # Merge: every name on this page is either picked or not.
        page_names = set(self._page_slice())
        picked = set(interaction.data.get("values") or [])
        self.selected_set = (self.selected_set - page_names) | picked
        await interaction.response.defer()

    async def _on_prev(self, interaction: discord.Interaction):
        if self.page > 0:
            self.page -= 1
            self._build_components()
        await wizard_registry.safe_edit_response(interaction, view=self)

    async def _on_next(self, interaction: discord.Interaction):
        if self.page < self.page_count - 1:
            self.page += 1
            self._build_components()
        await wizard_registry.safe_edit_response(interaction, view=self)

    async def _on_clear(self, interaction: discord.Interaction):
        self.selected_set.clear()
        self._build_components()
        await wizard_registry.safe_edit_response(interaction, view=self)

    async def _on_save(self, interaction: discord.Interaction):
        self.confirmed = True
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(interaction, view=self)
        self.stop()


# ── Discord-poll prefill helper (#244, Premium) ──────────────────────────────


def _prefill_from_discord_poll(
    guild_id: int,
    event_type: str,
    event_date: str,
    roster_names: list[str],
    alias_map: dict[str, str],
) -> set[str]:
    """Resolve `storm_signups` votes for this event to roster names.

    Used by `roster_multi_select` questions configured with
    `prefill_source = "discord_poll"` so the officer doesn't re-pick
    every voter by hand. Counts every "a", "b", or "either" vote as
    "attending"; "cannot" and absent votes are excluded.

    Resolution order for each vote's `target_member_id`:
      1. Direct match against roster `names` (on-behalf path stores
         the canonical name here).
      2. Lowercase alias match via `alias_map` (alliance aliases).
      3. Member Roster sheet lookup by Discord ID (self-vote path —
         only resolvable when `/setup → 👥 Member Sync` is configured).

    Unresolvable signups are silently dropped — the prefill is a
    convenience, not the source of truth. The officer can toggle any
    missed member by hand in the multi-select view.
    """
    import config

    rows = config.get_storm_signups(guild_id, event_type, event_date)
    if not rows:
        return set()
    attending_ids = [
        r["target_member_id"] for r in rows if (r.get("vote") or "").lower() in ("a", "b", "either")
    ]
    if not attending_ids:
        return set()

    name_set = set(roster_names)
    resolved: set[str] = set()
    unresolved: list[str] = []
    for tid in attending_ids:
        if not tid:
            continue
        if tid in name_set:
            resolved.add(tid)
            continue
        aliased = alias_map.get(tid.lower())
        if aliased and aliased in name_set:
            resolved.add(aliased)
            continue
        unresolved.append(tid)

    if not unresolved:
        return resolved

    # Self-vote IDs are numeric Discord IDs. Resolve them via the
    # Member Roster sheet (Premium feature). When unconfigured, drop
    # the unresolved IDs — prefill is best-effort.
    try:
        from config import get_member_roster_config, get_member_roster_sheet

        rcfg = get_member_roster_config(guild_id)
        if not rcfg or not rcfg.get("enabled"):
            return resolved
        ws = get_member_roster_sheet(guild_id, rcfg.get("tab_name") or "")
        rows_mr = ws.get_all_values()
    except Exception as e:
        print(f"[LOG] Discord-poll prefill: member roster lookup failed (guild {guild_id}): {e}")
        return resolved

    did_col = int(rcfg.get("discord_id_col", 0))
    name_col = int(rcfg.get("name_col", 1))
    id_to_name: dict[str, str] = {}
    for row in rows_mr[1:]:  # skip header
        if did_col >= len(row) or name_col >= len(row):
            continue
        did = (row[did_col] or "").strip()
        nm = (row[name_col] or "").strip()
        if did and nm:
            id_to_name[did] = nm

    for tid in unresolved:
        nm = id_to_name.get(tid)
        if nm and nm in name_set:
            resolved.add(nm)
    return resolved


# ── Roster + sheet helpers (new configurable participation flow) ──────────────


def load_roster_from_config(guild_id: int, event_type: str) -> tuple[list[str], dict[str, str]]:
    """
    Read the configured roster source for the given (guild, event_type) and
    return (names, alias_map). Every alliance configures their own roster
    tab + name column + optional alias column via the storm setup wizard.
    """
    from config import get_participation_config

    pcfg = get_participation_config(guild_id, event_type)
    tab = pcfg.get("roster_tab") or ""
    name_col = int(pcfg.get("roster_name_col") or 0)
    alias_col = int(
        pcfg.get("roster_alias_col") if pcfg.get("roster_alias_col") is not None else -1
    )
    start_row = int(pcfg.get("roster_start_row") or 2)

    names: list[str] = []
    alias_map: dict[str, str] = {}
    if not tab:
        return names, alias_map

    try:
        ws = _get_spreadsheet(guild_id).worksheet(tab)
        rows = ws.get_all_values()
    except Exception as e:
        print(f"[LOG] Could not read roster tab `{tab}` for guild {guild_id}: {e}")
        return names, alias_map

    for row in rows[start_row - 1 :]:  # start_row is 1-indexed
        if name_col >= len(row):
            continue
        name = row[name_col].strip()
        if not name:
            continue
        names.append(name)
        if alias_col >= 0 and alias_col < len(row):
            alias = row[alias_col].strip()
            if alias:
                alias_map[alias.lower()] = name

    print(
        f"[LOG] Loaded {len(names)} roster names ({len(alias_map)} aliases) "
        f"from `{tab}` (guild {guild_id}, {event_type})"
    )
    return names, alias_map


# ── Per-Member Log (#244) ────────────────────────────────────────────────────
#
# New Sheet tab keyed by `(event_date, member)` for per-member capture
# (roster multi-select + derived count question types). One row per
# member per event. Wide format — each per-member question gets its
# own column. New questions add columns; removed questions leave the
# historical column intact but new rows don't fill it. The Trends
# Viewer (#246) aggregates this tab across past events.


def _member_log_tab_name(event_type: str) -> str:
    return f"{event_type.upper()} Member Log"


def _format_member_log_date(log_date) -> str:
    """ISO-format the date (`YYYY-MM-DD`) so lookback-window filters in
    the Trends Viewer can do straight string comparison instead of
    re-parsing every row."""
    return log_date.isoformat() if hasattr(log_date, "isoformat") else str(log_date)


def upsert_member_log_rows(
    guild_id: int,
    event_type: str,
    log_date,
    per_member_data: dict[str, dict[str, str]],
    question_keys: list[str],
) -> None:
    """Upsert one row per (event_date, member) into the Per-Member
    Log tab (#244). Re-running the log (or attendance recording)
    for the same event date REPLACES the prior rows for the members
    in `per_member_data` rather than appending duplicates — without
    this, the Trends Viewer (#246) would double-count flagged events
    on every officer re-run.

    `per_member_data` maps `{member_name: {question_key: value}}`. A
    member entry with no flags can still be written (every alliance
    member shows up so the Trends Viewer can distinguish "didn't sit
    out" from "wasn't asked"); empty member-data dicts produce empty-
    column rows. `question_keys` is the ordered list of per-member
    question keys configured on the alliance — drives the header +
    column layout.

    Write strategy mirrors `storm_attendance.save_attendance`: read
    existing rows → filter out (event_date, member) pairs in the write
    batch → append fresh rows → `ws.update("A1", ...)` → blank
    trailing rows if the result is shorter. Atomic-ish — prior history
    survives a failed write.
    """
    if not per_member_data:
        # Nothing to write (no per-member questions captured this
        # event). Skip the gspread round-trip entirely.
        return

    sh = _get_spreadsheet(guild_id)
    tab = _member_log_tab_name(event_type)
    try:
        ws = sh.worksheet(tab)
    except Exception:
        ws = sh.add_worksheet(
            title=tab,
            rows=200,
            cols=max(8, len(question_keys) + 4),
        )

    # Header reconciliation: existing tabs may have more columns than
    # this event needs (a previous question that's since been
    # removed), or fewer (a new question added since the last write).
    # We MERGE — keep historical columns intact, append new ones at
    # the right edge. Existing data in dropped-question columns
    # stays for officer reference.
    try:
        all_values = ws.get_all_values()
    except Exception as e:
        print(f"[MEMBER LOG] read-before-write failed for guild={guild_id}: {e}")
        return
    existing_header = all_values[0] if all_values else []
    base_cols = ["Event Date", "Member"]
    if not any(existing_header):
        header = base_cols + list(question_keys)
    else:
        header = list(existing_header)
        # Ensure the base cols are in the right place; some legacy
        # tabs may have started with a different shape. Defensive:
        # if the first two cols don't match, rebuild the header.
        if header[:2] != base_cols:
            header = base_cols + ([h for h in header[2:]] if len(header) >= 2 else [])
        for qk in question_keys:
            if qk not in header:
                header.append(qk)

    date_str = _format_member_log_date(log_date)
    write_members = set(per_member_data.keys())

    # kept = header + every existing data row EXCEPT the ones we're
    # about to replace (same date + a member in the write batch).
    # Re-runs of the same event for the same member overwrite cleanly;
    # other dates and other members are preserved verbatim. Rows for
    # this date for members NOT in the write batch are also preserved
    # (e.g. a participation re-log for sit-outs shouldn't drop
    # attendance rows captured earlier).
    kept: list[list[str]] = [header]
    for row in all_values[1:] if all_values else []:
        if len(row) < 2:
            continue
        row_date = row[0]
        row_member = row[1]
        if row_date == date_str and row_member in write_members:
            continue  # Will be replaced by the new row below.
        kept.append(row)

    # Append fresh rows for this batch. Columns follow the merged
    # header so each value lands in the right cell.
    for member_name in sorted(per_member_data.keys()):
        member_flags = per_member_data[member_name] or {}
        row = [date_str, member_name]
        for col_name in header[2:]:
            row.append(str(member_flags.get(col_name, "")))
        kept.append(row)

    old_row_count = len(all_values)
    new_row_count = len(kept)
    try:
        ws.update("A1", kept, value_input_option="USER_ENTERED")
    except Exception as e:
        print(f"[MEMBER LOG] write failed for guild={guild_id}: {e}")
        return

    # Blank trailing rows when the result shrank (a duplicate set was
    # collapsed). Soft error — the live data is correct.
    if new_row_count < old_row_count:
        try:
            blanks = [[""] * len(header) for _ in range(old_row_count - new_row_count)]
            ws.update(
                f"A{new_row_count + 1}",
                blanks,
                value_input_option="USER_ENTERED",
            )
        except Exception as e:
            print(f"[MEMBER LOG] trailing-blank failed: {e}")
    print(
        f"[MEMBER LOG] {len(per_member_data)} row(s) upserted for "
        f"guild={guild_id} event={event_type} date={date_str}"
    )


# #245 — Canonical question key for the attendance-unified Member Log
# write. `/desertstorm attendance` and `/canyonstorm attendance` both
# write per-member values under this column so the Trends Viewer
# (#246) and the "Did this member show up?" preset (#247) can all
# reference the same data.
ATTENDANCE_QUESTION_KEY = "showed_up"


def read_member_log_window(
    guild_id: int,
    event_type: str,
    lookback_events: int,
    question_key: str | None = None,
) -> tuple[list[str], dict[str, dict[str, str]]]:
    """Read the past `lookback_events` distinct event dates from the
    Per-Member Log tab. Returns `(event_dates_desc, rows_by_member)`
    where:

    - `event_dates_desc` is the list of dates included, newest first.
    - `rows_by_member` maps `{member_name: {event_date: value}}` for
      the configured `question_key`. If `question_key` is None,
      returns the full row dict per `{member_name: {event_date:
      {col: value}}}` (used by the Trends Viewer when surfacing
      multiple columns).

    Empty list / dict when the tab doesn't exist or has no data.
    Used by `derived_count` questions during the participation log
    (#244) and by the Trends Viewer (#246).
    """
    sh = _get_spreadsheet(guild_id)
    tab = _member_log_tab_name(event_type)
    try:
        ws = sh.worksheet(tab)
    except Exception:
        return [], {}
    all_values = ws.get_all_values()
    if len(all_values) < 2:
        return [], {}
    header = all_values[0]
    if len(header) < 2 or header[:2] != ["Event Date", "Member"]:
        return [], {}
    # Find the column index for the question_key (if specified) so we
    # can pluck just that value per row.
    col_idx: int | None = None
    if question_key is not None:
        try:
            col_idx = header.index(question_key)
        except ValueError:
            return [], {}

    # Collect distinct event dates, ordered newest first (string sort
    # works because we ISO-format dates as YYYY-MM-DD).
    distinct_dates: list[str] = []
    seen: set[str] = set()
    # Walk rows in reverse (last appended = most recent under a
    # typical Sheet usage) and accumulate dates until we hit the
    # lookback cap. Within each date the rows arrive together.
    for r in reversed(all_values[1:]):
        if len(r) < 2:
            continue
        d = r[0]
        if not d or d in seen:
            continue
        seen.add(d)
        distinct_dates.append(d)
        if len(distinct_dates) >= max(1, lookback_events):
            break
    if not distinct_dates:
        return [], {}
    date_set = set(distinct_dates)

    rows_by_member: dict[str, dict] = {}
    for r in all_values[1:]:
        if len(r) < 2:
            continue
        d = r[0]
        if d not in date_set:
            continue
        member = r[1]
        if not member:
            continue
        if col_idx is not None:
            val = r[col_idx] if col_idx < len(r) else ""
            rows_by_member.setdefault(member, {})[d] = val
        else:
            # Full-row mode — return dict of column -> value per date.
            cells = {
                h: (r[i] if i < len(r) else "")
                for i, h in enumerate(header)
                if i >= 2  # skip event-date and member cols
            }
            rows_by_member.setdefault(member, {})[d] = cells
    return distinct_dates, rows_by_member


def count_member_flags_in_window(
    guild_id: int,
    event_type: str,
    lookback_events: int,
    question_key: str,
) -> dict[str, int]:
    """For each member, count how many of the last `lookback_events`
    captured events have a truthy value in the `question_key` column.
    Returns `{member_name: count}`. Members with zero hits are
    included (count=0) so the caller can render them as "0 sit-outs"
    instead of "missing."

    Truthy = value is non-empty and not "no" / "false" / "0". Used by
    `derived_count` questions during the participation flow and by
    the Trends Viewer (#246).
    """
    _dates, rows_by_member = read_member_log_window(
        guild_id,
        event_type,
        lookback_events,
        question_key,
    )
    counts: dict[str, int] = {}
    for member, by_date in rows_by_member.items():
        c = 0
        for v in by_date.values():
            normalized = str(v).strip().lower()
            if normalized and normalized not in ("no", "false", "0", ""):
                c += 1
        counts[member] = c
    return counts


def append_participation_row(
    guild_id: int, event_type: str, log_date, answers: dict[str, str]
) -> None:
    """
    Append a row to the configured participation tab. Header columns are:
    Date | Event | <one column per configured question, in order>.
    """
    from config import get_participation_config

    pcfg = get_participation_config(guild_id, event_type)
    tab = pcfg.get("tab_name") or (
        "DS Participation Log" if event_type.upper() == "DS" else "CS Participation Log"
    )
    questions = pcfg.get("questions") or []

    sh = _get_spreadsheet(guild_id)
    try:
        ws = sh.worksheet(tab)
    except Exception:
        # Create the tab if it doesn't exist
        ws = sh.add_worksheet(title=tab, rows=200, cols=max(8, len(questions) + 4))

    headers = ["Date", "Event"] + [q.get("label", q.get("key", "?")) for q in questions]
    existing_header = ws.row_values(1)
    if not any(existing_header):
        ws.update("A1", [headers], value_input_option="USER_ENTERED")

    row = [
        f"{log_date.month}/{log_date.day}/{log_date.year}",
        event_type.upper(),
    ]
    for q in questions:
        val = answers.get(q.get("key", ""), "")
        if isinstance(val, list):
            val = ", ".join(str(v) for v in val)
        row.append(str(val) if val is not None else "")
    ws.append_row(row, value_input_option="USER_ENTERED")
    print(
        f"[LOG] Participation row appended for guild={guild_id} "
        f"event={event_type} date={log_date.isoformat()}"
    )


# ── Shared log flow (new configurable version) ───────────────────────────────


async def run_log_flow(bot, channel, user, event_type):
    """
    Walk leadership through the participation log flow. The questions
    asked are read from the per-guild participation config saved by
    the storm setup wizard (`/setup → ⚔️ Desert Storm` or `/setup → 🏜️ Canyon Storm`). The date is always asked
    first (mandatory, never configurable).
    """
    is_ds = event_type.upper() == "DS"
    event_label = "Desert Storm" if is_ds else "Canyon Storm"
    hub_cmd = HUB_COMMAND["DS"] if is_ds else HUB_COMMAND["CS"]
    log_hint = f"`{hub_cmd}` → **{HUB_BTN_PARTICIPATION}**"
    # Post-#201: storm setup wizards live behind /setup hub buttons.
    setup_cmd = STORM_SETUP_NAV["DS" if is_ds else "CS"]
    guild_id = channel.guild.id if hasattr(channel, "guild") and channel.guild else None
    cancel_event = asyncio.Event()
    active_logs[user.id] = cancel_event

    from config import get_participation_config

    pcfg = get_participation_config(guild_id, event_type) if guild_id else {}

    if not pcfg.get("enabled"):
        await channel.send(
            f"⚙️ Participation tracking isn't enabled for {event_label} yet. "
            f"Run `{setup_cmd}` and walk through Step 6 to define what you want to track."
        )
        active_logs.pop(user.id, None)
        return

    questions = pcfg.get("questions") or []
    if not questions:
        await channel.send(
            f"⚙️ Participation tracking is enabled but no questions are configured. "
            f"Run `{setup_cmd}` to add questions."
        )
        active_logs.pop(user.id, None)
        return

    def check(m):
        return m.author == user and m.channel == channel

    async def wait_for_msg(prompt_text):
        prompt_msg = await channel.send(prompt_text)
        try:
            reply_task = asyncio.ensure_future(
                bot.wait_for("message", check=check, timeout=WIZARD_TIMEOUT)
            )
            cancel_task = asyncio.ensure_future(cancel_event.wait())
            done, pending = await asyncio.wait(
                [reply_task, cancel_task], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            if cancel_event.is_set():
                try:
                    await prompt_msg.delete()
                except discord.HTTPException:
                    pass
                return None
            reply = done.pop().result()
            try:
                await prompt_msg.delete()
                await reply.delete()
            except discord.HTTPException:
                pass
            return reply.content.strip()
        except asyncio.TimeoutError:
            await channel.send(f"⏰ Timed out. Run {log_hint} to start again.")
            return None

    async def wait_for_view(view, prompt_msg):
        """Wait for any view (NameEntryView, YesNoLogView, etc). Returns False if cancelled/timed out."""
        view_task = asyncio.ensure_future(view.wait())
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        done, pending = await asyncio.wait(
            [view_task, cancel_task], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        if cancel_event.is_set():
            for item in view.children:
                item.disabled = True
            try:
                await prompt_msg.edit(view=view)
            except discord.HTTPException:
                pass
            return False
        if not getattr(view, "confirmed", True):
            try:
                await prompt_msg.delete()
            except discord.HTTPException:
                pass
            await channel.send(f"⏰ Timed out. Run {log_hint} to start again.")
            return False
        return True

    try:
        total_steps = len(questions) + 1  # +1 for the always-required date
        await channel.send(
            f"📋 **{event_label} Log** started by {user.mention}\n"
            f"*{total_steps} step(s) total. Use `/cancel` at any time to stop.*"
        )

        # ── Step 1: Date (always asked, never configurable) ──────────────────
        # Officers can pick from recent saved event dates (storm
        # signups + structured rosters) — typing a date from scratch
        # was a tester pain point. Free-text remains an option for
        # backfilling old events that pre-date the saved data.
        recent_dates = await asyncio.get_event_loop().run_in_executor(
            None,
            _collect_recent_event_dates,
            guild_id,
            event_type,
        )
        picker = _LogDatePickerView(recent_dates)
        picker_msg = await channel.send(
            "**Step 1: Event date**\nPick the date this log is for:",
            view=picker,
        )
        if not await wait_for_view(picker, picker_msg):
            if cancel_event.is_set():
                await channel.send("❌ Log cancelled.")
            return
        if picker.cancelled:
            await channel.send("❌ Log cancelled.")
            return
        if picker.picked_date is not None:
            log_date = picker.picked_date
        else:
            # Free-text fallback — officer chose "Type a different date".
            raw_date = await wait_for_msg(
                "Type the date (e.g. `April 14`, `4/14`) or type `today`:"
            )
            if raw_date is None:
                if cancel_event.is_set():
                    await channel.send("❌ Log cancelled.")
                return
            if raw_date.lower() == "today":
                log_date = date.today()
            else:
                from train import parse_date_and_name

                parsed_d, _, _ = parse_date_and_name(
                    f"{raw_date} - placeholder",
                )
                if not parsed_d:
                    await channel.send(
                        f"⚠️ Could not parse `{raw_date}` as a date. Run {log_hint} to start again."
                    )
                    return
                log_date = parsed_d

        # ── Roster (lazy — only loaded if any question needs it) ─────────────
        roster_loaded = False
        names: list[str] = []
        alias_map: dict[str, str] = {}

        async def _ensure_roster():
            nonlocal roster_loaded, names, alias_map
            if roster_loaded:
                return
            loading_msg = await channel.send("⏳ Loading roster from your configured tab…")
            names, alias_map = await asyncio.get_event_loop().run_in_executor(
                None,
                load_roster_from_config,
                guild_id,
                event_type,
            )
            try:
                await loading_msg.delete()
            except discord.HTTPException:
                pass
            roster_loaded = True

        # ── Walk through configured questions ────────────────────────────────
        # `answers` holds event-level data (existing behaviour); the new
        # `per_member_data` holds per-(member, question) flags / counts
        # produced by `roster_multi_select` and `derived_count` (#244).
        # The two stores write to different Sheet tabs at save time.
        answers: dict[str, str] = {}
        per_member_data: dict[str, dict[str, str]] = {}
        per_member_question_keys: list[str] = []
        for idx, q in enumerate(questions, start=2):
            qkey = q.get("key", f"q{idx}")
            qlabel = q.get("label", qkey)
            qtype = q.get("type", "text")

            header = f"**Step {idx} of {total_steps}: {qlabel}**"

            if qtype == "yes_no":
                yn = _YesNoLogView()
                msg = await channel.send(f"{header}\nPick one.", view=yn)
                if not await wait_for_view(yn, msg):
                    if cancel_event.is_set():
                        await channel.send("❌ Log cancelled.")
                    return
                answers[qkey] = "Yes" if yn.value else "No"

            elif qtype == "numeric":
                lo = q.get("min")
                hi = q.get("max")
                bound_hint = ""
                if lo is not None or hi is not None:
                    bits = []
                    if lo is not None:
                        bits.append(f"min `{lo}`")
                    if hi is not None:
                        bits.append(f"max `{hi}`")
                    bound_hint = f" *({', '.join(bits)})*"
                attempts = 5
                value: str | None = None
                while attempts > 0:
                    raw = await wait_for_msg(f"{header}{bound_hint}\nType a number.")
                    if raw is None:
                        if cancel_event.is_set():
                            await channel.send("❌ Log cancelled.")
                        return
                    try:
                        n = float(raw) if "." in raw else int(raw)
                    except ValueError:
                        attempts -= 1
                        await channel.send(
                            f"⚠️ `{raw}` isn't a number. Please re-enter your answer."
                        )
                        continue
                    if lo is not None and n < lo:
                        attempts -= 1
                        await channel.send(f"⚠️ Must be at least **{lo}**. Please re-enter.")
                        continue
                    if hi is not None and n > hi:
                        attempts -= 1
                        await channel.send(f"⚠️ Must be at most **{hi}**. Please re-enter.")
                        continue
                    value = str(n)
                    break
                if value is None:
                    await channel.send(
                        "⚠️ Too many invalid attempts. Cancelling the log. "
                        f"run {log_hint} when you're ready to try again."
                    )
                    return
                answers[qkey] = value

            elif qtype == "roster_names":
                await _ensure_roster()
                if not names:
                    await channel.send(
                        "⚠️ The configured roster tab is empty or unreachable. "
                        f"Run `{setup_cmd}` "
                        f"to update the roster source, then try again."
                    )
                    return
                preview = ", ".join(names) if len(names) <= 25 else f"{len(names)} members loaded"
                view = NameEntryView(names, qlabel, alias_map)
                prompt = await channel.send(
                    f"{header}\nPress **Enter Names** to type who applies. "
                    f"Press **Skip** if none.\n*Roster: {preview}*",
                    view=view,
                )
                if not await wait_for_view(view, prompt):
                    if cancel_event.is_set():
                        await channel.send("❌ Log cancelled.")
                    return
                picked = sorted(view.selected)
                if view.unrecognized:
                    picked += sorted(view.unrecognized)
                answers[qkey] = ", ".join(picked)

            elif qtype == "single_select":
                opts = q.get("options") or []
                if not opts:
                    answers[qkey] = ""
                    continue
                view = ShortSelectView(opts, qlabel)
                prompt = await channel.send(f"{header}\nPick one.", view=view)
                if not await wait_for_view(view, prompt):
                    if cancel_event.is_set():
                        await channel.send("❌ Log cancelled.")
                    return
                answers[qkey] = next(iter(view.selected), "")

            elif qtype == "multi_select":
                opts = q.get("options") or []
                if not opts:
                    answers[qkey] = ""
                    continue
                view = ShortSelectView(opts, qlabel)
                prompt = await channel.send(f"{header}\nPick any that apply.", view=view)
                if not await wait_for_view(view, prompt):
                    if cancel_event.is_set():
                        await channel.send("❌ Log cancelled.")
                    return
                answers[qkey] = ", ".join(sorted(view.selected))

            elif qtype == "date":
                fmt = q.get("date_format") or "%m/%d/%Y"
                attempts = 5
                value = None
                while attempts > 0:
                    raw = await wait_for_msg(f"{header} *(format `{fmt}`)*")
                    if raw is None:
                        if cancel_event.is_set():
                            await channel.send("❌ Log cancelled.")
                        return
                    try:
                        from datetime import datetime as _dt

                        d = _dt.strptime(raw, fmt).date()
                        value = d.isoformat()
                        break
                    except ValueError:
                        attempts -= 1
                        await channel.send(f"⚠️ `{raw}` doesn't match `{fmt}`. Please re-enter.")
                if value is None:
                    await channel.send("⚠️ Too many invalid attempts. Cancelling the log.")
                    return
                answers[qkey] = value

            elif qtype == "roster_multi_select":
                # #244 — paginated multi-select against the alliance
                # roster. Officer picks members who match; per-member
                # flags (yes/no) get written to the Per-Member Log
                # tab so the Trends Viewer can aggregate.
                await _ensure_roster()
                if not names:
                    await channel.send(
                        "⚠️ The configured roster tab is empty or unreachable. "
                        f"Run `{setup_cmd}` to update the roster source."
                    )
                    return
                # Pre-fill (Premium only). Compute the matching member
                # names from signup data when configured.
                preselected: set[str] = set()
                prefill_source = q.get("prefill_source") or ""
                if prefill_source == "discord_poll":
                    preselected = await asyncio.to_thread(
                        _prefill_from_discord_poll,
                        guild_id,
                        event_type,
                        log_date.isoformat(),
                        names,
                        alias_map,
                    )
                view = _PaginatedRosterMultiSelectView(
                    names,
                    qlabel,
                    preselected=preselected,
                    prefill_used=bool(prefill_source),
                )
                preview = (
                    (
                        ", ".join(sorted(preselected)[:5])
                        + (f" (+{len(preselected) - 5} more)" if len(preselected) > 5 else "")
                    )
                    if preselected
                    else ""
                )
                prompt_lines = [header]
                if prefill_source == "discord_poll":
                    prompt_lines.append(
                        "🗳️ Pre-checked members are those who voted to "
                        "attend in the Discord signup poll. The legend "
                        "`✏️` marks any member you toggle manually."
                    )
                    if preview:
                        prompt_lines.append(f"*Pre-checked:* {preview}")
                prompt_lines.append(
                    "Use the dropdown(s) to pick the members who match. Click ✅ Save when done."
                )
                prompt = await channel.send(
                    "\n".join(prompt_lines),
                    view=view,
                )
                if not await wait_for_view(view, prompt):
                    if cancel_event.is_set():
                        await channel.send("❌ Log cancelled.")
                    return
                picked = view.selected_set
                # Build per-member flags for every roster member: "yes"
                # for picked, "no" otherwise. Officer's omission is a
                # meaningful "no" — not "missing data."
                for member_name in names:
                    per_member_data.setdefault(member_name, {})[qkey] = (
                        "yes" if member_name in picked else "no"
                    )
                per_member_question_keys.append(qkey)
                # Surface a short event-level summary too (for the
                # post-log embed): count of picked members.
                answers[qkey] = f"{len(picked)} member(s)"

            elif qtype == "derived_count":
                # #244 — Premium derived count. Read past Per-Member
                # Log rows for the configured source question, count
                # per member, write to the Per-Member Log under this
                # question's key. Override UI deferred to v2.
                source_key = q.get("source_question_key", "")
                lookback = int(q.get("lookback_events", 4))
                if not source_key:
                    await channel.send(
                        f"⚠️ Derived count `{qlabel}` has no source question configured. Skipping."
                    )
                    continue
                await _ensure_roster()
                counts = await asyncio.get_event_loop().run_in_executor(
                    None,
                    count_member_flags_in_window,
                    guild_id,
                    event_type,
                    lookback,
                    source_key,
                )
                # Make sure every roster member has a row (count = 0
                # for those who never appeared in the source data).
                for member_name in names:
                    per_member_data.setdefault(member_name, {})[qkey] = str(
                        counts.get(member_name, 0)
                    )
                per_member_question_keys.append(qkey)
                if q.get("show_during_log"):
                    # Surface the top-5 by count so officers see at a
                    # glance who's flagged most often.
                    ordered = sorted(
                        counts.items(),
                        key=lambda kv: (-kv[1], kv[0]),
                    )
                    top = [f"{n} ({c})" for n, c in ordered[:5] if c > 0]
                    if top:
                        await channel.send(
                            f"{header}\n📊 Top by count in past {lookback} events: {', '.join(top)}"
                        )
                answers[qkey] = (
                    f"max {max(counts.values()) if counts else 0} (past {lookback} events)"
                )

            else:  # "text" or unknown — fall back to free text
                raw = await wait_for_msg(f"{header}\nType your answer (or `skip` for none).")
                if raw is None:
                    if cancel_event.is_set():
                        await channel.send("❌ Log cancelled.")
                    return
                answers[qkey] = "" if raw.lower() == "skip" else raw

        # ── Save row ─────────────────────────────────────────────────────────
        await channel.send("💾 Saving log…")
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                append_participation_row,
                guild_id,
                event_type,
                log_date,
                answers,
            )
        except Exception as e:
            await channel.send(f"⚠️ Error saving to sheet: {e}")
            return

        # Per-Member Log tab: append wide-format rows for question types
        # that produce one value per alliance member (roster_multi_select,
        # derived_count). The event-level tab keeps the summary value; the
        # Member Log tab keeps the per-member detail so Trends Viewer (#246)
        # and derived_count lookbacks can read history.
        if per_member_question_keys and per_member_data:
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None,
                    upsert_member_log_rows,
                    guild_id,
                    event_type,
                    log_date,
                    per_member_data,
                    per_member_question_keys,
                )
            except Exception as e:
                await channel.send(f"⚠️ Saved event row, but per-member log failed: {e}")

        # ── Summary ──────────────────────────────────────────────────────────
        date_str = f"{log_date:%A, %B} {log_date.day}, {log_date.year}"
        lines = [f"📋 **{event_label} Log: {date_str}**"]
        for q in questions:
            qkey = q.get("key", "")
            qlabel = q.get("label", qkey)
            v = answers.get(qkey, "")
            lines.append(f"**{qlabel}:** {v if v not in ('', None) else 'None'}")
        summary = "\n".join(lines)

        await channel.send(f"✅ **Log saved!**\n\n{summary}")

        # Mirror the summary into the configured log channel (if different).
        try:
            log_channel_id = int(pcfg.get("log_channel_id") or 0)
            if log_channel_id and channel.id != log_channel_id:
                target = bot.get_channel(log_channel_id)
                if target:
                    await target.send(summary)
        except Exception as e:
            print(f"[LOG] Error mirroring summary to log channel: {e}")

    finally:
        active_logs.pop(user.id, None)


class _YesNoLogView(discord.ui.View):
    """Simple Yes/No picker for participation `yes_no` questions."""

    def __init__(self):
        super().__init__(timeout=WIZARD_TIMEOUT)
        self.confirmed = False
        self.value: bool | None = None

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(self, inter: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self.confirmed = True
        for c in self.children:
            c.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no(self, inter: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self.confirmed = True
        for c in self.children:
            c.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()


# ── Log lookup ─────────────────────────────────────────────────────────────────


def list_recent_log_dates(event_type: str, n: int, guild_id=None) -> list[date]:
    """
    Return up to `n` most-recent log dates for `event_type`, sorted newest-first.
    Used to gate free-tier access (only the N most recent entries are visible).
    """
    out: list[date] = []
    try:
        ws = _get_log_sheet(guild_id, event_type=event_type)
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return out
        from datetime import datetime

        seen: set[date] = set()
        parsed: list[date] = []
        for row in rows[1:]:
            if len(row) < 2:
                continue
            if row[1].strip().upper() != event_type.upper():
                continue
            row_date = row[0].strip()
            for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                try:
                    d = datetime.strptime(row_date, fmt).date()
                    if d not in seen:
                        seen.add(d)
                        parsed.append(d)
                    break
                except ValueError:
                    continue
        parsed.sort(reverse=True)
        return parsed[:n]
    except Exception as e:
        print(f"[STORM_LOG] Could not list recent dates: {e}")
        return out


def lookup_log_entry(event_type: str, log_date: date, guild_id=None):
    """
    Find the most recent log row matching event_type and log_date.
    Returns a dict shaped like `{"date": ..., "event": ..., "fields":
    [(label, value), ...]}` so /[event]_log can format it generically
    against whatever questions the alliance configured. Falls back to
    the legacy column shape (Vote Count / RTF / Sitting Out / Prior)
    when a guild is still on the old "DS-CS Sit-outs" tab.
    """
    try:
        ws = _get_log_sheet(guild_id, event_type=event_type)
        rows = ws.get_all_values()
        if len(rows) <= 1:
            return None
        header_row = rows[0]
        # Resolve column labels: skip Date and Event, use the remaining
        # header cells as the field labels. Empty header cells fall back
        # to a generic name so we don't crash on malformed sheets.
        field_labels = [(h.strip() or f"Column {i + 1}") for i, h in enumerate(header_row[2:])]

        from datetime import datetime

        for row in reversed(rows[1:]):
            if len(row) < 2:
                continue
            if row[1].strip().upper() != event_type.upper():
                continue
            row_date = row[0].strip()
            for fmt in ("%-m/%-d/%Y", "%m/%d/%Y", "%Y-%m-%d"):
                try:
                    parsed = datetime.strptime(row_date, fmt).date()
                except ValueError:
                    continue
                if parsed != log_date:
                    break
                fields: list[tuple[str, str]] = []
                for i, label in enumerate(field_labels):
                    fields.append((label, row[i + 2] if len(row) > i + 2 else ""))
                return {
                    "date": row[0] if len(row) > 0 else "",
                    "event": row[1] if len(row) > 1 else "",
                    "fields": fields,
                    # Legacy aliases for callers that haven't migrated yet:
                    "vote_count": row[2] if len(row) > 2 else "",
                    "rtf_no_vote": row[3] if len(row) > 3 else "",
                    "sitting_out": row[4] if len(row) > 4 else "",
                    "prior_no_request": row[5] if len(row) > 5 else "",
                }
        return None
    except Exception as e:
        print(f"[LOG] Error looking up log entry: {e}")
        return None


# ── Guard ──────────────────────────────────────────────────────────────────────


async def _guard(interaction: discord.Interaction) -> bool:
    cfg = get_config(interaction.guild_id)
    if not cfg or not cfg.setup_complete:
        await interaction.response.send_message(NOT_SET_UP, ephemeral=True)
        return False
    if cfg.leadership_role_name not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message(
            f"⛔ You need the **{cfg.leadership_role_name}** role to use this command.",
            ephemeral=True,
        )
        return False
    return True


# ── Slash command handlers ────────────────────────────────────────────────────
#
# Registered by `storm_commands_root` under the `/desertstorm` and
# `/canyonstorm` parent groups. The bodies stay here so the root cog
# is a thin dispatcher.


async def handle_storm_participation(
    bot, interaction: discord.Interaction, event_type: str
) -> None:
    if not await _guard(interaction):
        return
    if interaction.user.id in active_logs:
        await interaction.response.send_message(
            "⚠️ You already have an active log session. Use `/cancel` to stop it first.",
            ephemeral=True,
        )
        return
    label = "DS" if event_type == "DS" else "CS"
    await interaction.response.send_message(f"📋 Starting {label} log...", ephemeral=True)
    await run_log_flow(bot, interaction.channel, interaction.user, event_type)


async def handle_storm_log(
    bot, interaction: discord.Interaction, event_type: str, date: str | None = None
) -> None:
    await _show_storm_log(interaction, event_type, date)


async def handle_storm_remind(bot, interaction: discord.Interaction, event_type: str) -> None:
    await _send_storm_reminder(bot, interaction, event_type)


async def _send_storm_reminder(bot, interaction: discord.Interaction, event_type: str):
    """DM every roster member with a participation reminder for the given storm."""
    if not await _guard(interaction):
        return

    import premium
    import dm
    from config import (
        get_member_roster_config,
        get_member_roster_sheet,
        get_storm_config,
    )

    if not await premium.feature_gate(
        "storm_participation_dm",
        interaction.guild_id,
        interaction=interaction,
        bot=bot,
    ):
        await interaction.response.send_message(
            embed=premium.premium_locked_embed(
                feature_label="Storm participation DMs",
                description=(
                    "Storm participation reminders are part of Alliance Helper "
                    "Premium and require Member Roster Sync (`/setup` → 👥 Member Sync). "
                    "Run `/upgrade` to unlock."
                ),
            ),
            view=premium.upgrade_view(),
            ephemeral=True,
        )
        return

    roster_cfg = get_member_roster_config(interaction.guild_id)
    if not roster_cfg.get("enabled"):
        await interaction.response.send_message(
            FEATURE_NOT_CONFIGURED.format(feature="Member Roster Sync", wizard_btn=HUB_BTN_MEMBERS),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True)

    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    try:
        ws = get_member_roster_sheet(interaction.guild_id, roster_cfg["tab_name"])
        rows = await asyncio.get_event_loop().run_in_executor(
            None,
            ws.get_all_values,
        )
    except Exception as e:
        await interaction.followup.send(
            f"⚠️ Could not read the roster sheet: {e}",
            ephemeral=True,
        )
        return

    # Resolve the DM body — prefer the alliance's configured template,
    # fall back to the bot's default. `{name}` is the only supported
    # placeholder; the per-event-type config separates DS from CS so we
    # don't need a {label} placeholder.
    storm_cfg = get_storm_config(interaction.guild_id, event_type) or {}
    dm_body_tmpl = (
        storm_cfg.get("dm_reminder_message") or ""
    ).strip() or DEFAULT_STORM_REMINDER_DM.format(label=label)

    name_col = roster_cfg.get("name_col", 1)
    did_col = roster_cfg["discord_id_col"]
    sent = 0
    skipped = 0
    for row in rows[1:]:
        if did_col >= len(row):
            continue
        did = row[did_col].strip()
        if not did:
            skipped += 1
            continue
        member_name = row[name_col].strip() if name_col < len(row) else ""
        ok = await dm.send_dm_to_id(
            bot,
            interaction.guild_id,
            did,
            content=_render_dm_body(dm_body_tmpl, name=member_name),
        )
        if ok:
            sent += 1
        else:
            skipped += 1

    await interaction.followup.send(
        f"✅ Sent {sent} **{label}** reminder DM{'s' if sent != 1 else ''}. {skipped} skipped.",
        ephemeral=True,
    )


# ── Default DM body + safe template rendering ────────────────────────────────

# Hardcoded fallback when a guild hasn't configured its own DM body via
# the storm setup wizard (`/setup → ⚔️ Desert Storm` or `/setup → 🏜️ Canyon Storm`). `{label}` is substituted at
# call time from the event_type so DS and CS share one default. The only
# user-supplied placeholder is `{name}` (member's roster name).
DEFAULT_STORM_REMINDER_DM = (
    "⚔️ **{label} reminder**: your alliance is preparing for this week's "
    "{label}. Please confirm your participation in Discord and check the "
    "team channel for your zone assignment. Good luck out there!"
)


def _render_dm_body(template: str, *, name: str = "") -> str:
    """Substitute `{name}` into a user-configured DM body. Tolerates
    missing or unknown placeholders so a typo in the configured template
    doesn't crash the entire reminder loop — the typo just renders as
    literal text in the DM."""

    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    try:
        return template.format_map(_SafeDict(name=name or ""))
    except Exception:
        # Catches odd format spec issues (`{name:weird-spec}`); fall back
        # to substring replacement so the alliance still sees something.
        return template.replace("{name}", name or "")


async def _show_storm_log(interaction: discord.Interaction, event: str, date: str | None):
    """Shared handler for the `log` subcommand under both storm parents."""
    if not await _guard(interaction):
        return

    await interaction.response.defer()

    if date:
        from train import parse_date_and_name

        parsed_d, _, _ = parse_date_and_name(f"{date} - placeholder")
        if not parsed_d:
            await interaction.followup.send(
                f"⚠️ Could not parse date **{date}**. Try a format like `April 14` or `4/14`.",
                ephemeral=True,
            )
            return
    else:
        from datetime import date as date_cls

        parsed_d = date_cls.today()

    event_label = "Desert Storm" if event == "DS" else "Canyon Storm"

    # Free tier sees only the most recent N storm participation log entries.
    import premium

    recent_cap = await premium.get_limit(
        "storm_log_recent",
        interaction.guild_id,
        interaction=interaction,
        bot=interaction.client,
    )
    if recent_cap is not None:
        recent_dates = await asyncio.get_event_loop().run_in_executor(
            None,
            list_recent_log_dates,
            event,
            recent_cap,
            interaction.guild_id,
        )
        if recent_dates and parsed_d not in recent_dates:
            embed = discord.Embed(
                title=f"📊 {event_label} log lookback: Free tier limit",
                description=(
                    f"You can only see the **{recent_cap} most recent** log "
                    f"entries with the free tier. Upgrade to "
                    f"{premium.PREMIUM_BRAND} to unlock unlimited lookback."
                ),
                color=discord.Color.orange(),
            )
            await interaction.followup.send(
                embed=embed,
                view=premium.upgrade_view(),
                ephemeral=True,
            )
            return

    entry = await asyncio.get_event_loop().run_in_executor(
        None,
        lookup_log_entry,
        event,
        parsed_d,
        interaction.guild_id,
    )

    if entry is None:
        await interaction.followup.send(
            f"❌ No **{event_label}** log found for **{parsed_d:%B} {parsed_d.day}, {parsed_d.year}**.",
            ephemeral=True,
        )
        return

    date_str = f"{parsed_d:%A, %B} {parsed_d.day}, {parsed_d.year}"
    lines = [f"📋 **{event_label} Log: {date_str}**"]
    # Prefer the generic `fields` list (set by the new participation flow);
    # fall back to the legacy DS/CS column shape so pre-rework data still
    # renders nicely.
    fields = entry.get("fields") or []
    if fields:
        for label_, value in fields:
            lines.append(f"**{label_}:** {value or 'None'}")
    else:
        action_label = "Vote" if event == "DS" else "Request"
        if event == "DS":
            lines.append(f"**Votes:** {entry.get('vote_count') or 'Not recorded'}")
            lines.append(f"**RTF No Vote:** {entry.get('rtf_no_vote') or 'None'}")
        lines.append(f"**Sitting Out:** {entry.get('sitting_out') or 'None'}")
        lines.append(
            f"**Prior Sit-Out No {action_label}:** {entry.get('prior_no_request') or 'None'}"
        )

    await interaction.followup.send("\n".join(lines))
