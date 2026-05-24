"""
member_roster.py — Member Roster Sync (Premium-only feature).

Iterates a guild's members (filtered by the configured member role if set)
and writes them to a designated sheet tab. Other premium features
(birthday DMs, train DMs, survey-reminder DMs, auto-mention) read this
sheet to look up Discord IDs by display name.

Sheet structure (column indices configurable per guild):
  Discord ID | Name | Display Name | Joined | Roles

Plus an auto-created `Is this user in Discord?` column the bot maintains
with Yes/No values per row + a Google Sheets data validation dropdown.
The storm readers prefer this column over the legacy `not_on_discord`
column when present — the alliance no longer has to flag non-Discord
members manually for the storm flow's officer-view bucketing to be
accurate.

Commands:
  /setup → 👥 Member Sync — admin wizard to configure tab/columns/role filter
  /members overview — roster source + sync state at a glance
  /members sync     — manually run a full sync now
"""

import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import premium
from config import (
    get_config, get_member_roster_config, save_member_roster_config,
    update_roster_last_synced, get_member_roster_sheet, get_spreadsheet,
)


logger = logging.getLogger(__name__)


# Header for the bot-maintained presence column. Storm readers also
# search this exact string — keep them in sync.
DISCORD_FLAG_COLUMN_HEADER = "Is this user in Discord?"


# ── Sync logic (pure-ish; takes a guild and config) ───────────────────────────

def _format_joined(member: discord.Member) -> str:
    if member.joined_at is None:
        return ""
    return member.joined_at.strftime("%Y-%m-%d")


def _format_roles(member: discord.Member) -> str:
    """Comma-separated list of role names, excluding @everyone."""
    names = [r.name for r in member.roles if r.name != "@everyone"]
    return ", ".join(sorted(names))


def _eligible(member: discord.Member, role_filter_id: int) -> bool:
    """True if the member should appear on the roster."""
    if member.bot:
        return False
    if role_filter_id == 0:
        return True
    return any(r.id == role_filter_id for r in member.roles)


def _bot_managed_cols(cfg: dict) -> dict[int, tuple[str, callable]]:
    """The column indices the bot owns and overwrites on every sync,
    keyed by index. Anything outside this set on the Sheet is alliance-
    owned data (custom power columns, the `not_on_discord` flag, etc.)
    and must be preserved across sync calls."""
    return {
        cfg["discord_id_col"]: ("Discord ID",   lambda m: str(m.id)),
        cfg["name_col"]:       ("Name",         lambda m: m.name),
        cfg["display_col"]:    ("Display Name", lambda m: m.display_name),
        cfg["joined_col"]:     ("Joined",       _format_joined),
        cfg["roles_col"]:      ("Roles",        lambda m: _format_roles(m)),
    }


# ── Layout detection (#226 follow-up) ──────────────────────────────────────
#
# Member Sync writes its 5 bot-managed columns + the "Is this user in
# Discord?" presence column to the configured roster tab. The original
# implementation hardcoded the bot columns to indices 0-4 (A-E). When
# an alliance had already organised their roster sheet — with power,
# alias, or notes columns living in A-E — the first sync would
# overwrite that alliance data with Discord username + display name.
#
# `detect_column_layout` reads the sheet's existing headers and tries
# to claim columns by matching header text. Anything that doesn't
# match (the common case for a brand-new sheet OR a sheet where the
# alliance organised columns differently) gets appended at the right
# edge, leaving every existing column untouched.

# Bot field → (normalised) header aliases. The Sheets writer uses
# the FIRST alias as the canonical label when appending a new column.
_FIELD_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "discord_id_col": ("discord id", "discordid", "id"),
    "name_col":       ("name", "username", "discord name"),
    "display_col":    ("display name", "displayname", "alias"),
    "joined_col":     ("joined", "join date", "joined at"),
    "roles_col":      ("roles", "role"),
}


def _normalise_header(value: str) -> str:
    """Strip the header down to a comparable key — lowercase, drop
    non-alphanumerics. `"Discord_ID"`, `"DiscordID"`, and `"discord id"`
    all collapse to `"discordid"`."""
    out = []
    for ch in (value or "").strip().lower():
        if ch.isalnum():
            out.append(ch)
    return "".join(out)


def detect_column_layout(headers: list[str]) -> dict:
    """Detect where each bot-managed field should land on the roster
    tab, given the existing header row.

    Returns:
        {
            "layout":          {field_name: column_index},
            "pending_appends": [field_name, ...]  # bot fields that
                                                  # didn't match an
                                                  # existing header
                                                  # and need a new
                                                  # column appended
                                                  # at the right edge.
        }

    Matching rules:
        * Each existing header is normalised (`_normalise_header`).
        * A bot field claims the first column whose normalised header
          matches one of the field's alias-set entries.
        * A column is claimed by AT MOST one field; ties resolve in
          `_FIELD_HEADER_ALIASES` declaration order.
        * Bot fields with no matched header land in `pending_appends`
          and get column indices at the right edge of the sheet, in
          declaration order.

    Pure function — no Sheets I/O, no config reads. Safe to unit
    test directly with a list of header strings.
    """
    normalised = [_normalise_header(h) for h in headers]
    width = len(headers)
    layout: dict[str, int] = {}
    claimed: set[int] = set()

    for field, aliases in _FIELD_HEADER_ALIASES.items():
        alias_keys = {_normalise_header(a) for a in aliases}
        for idx, key in enumerate(normalised):
            if idx in claimed:
                continue
            if not key:
                continue
            if key in alias_keys:
                layout[field] = idx
                claimed.add(idx)
                break

    pending: list[str] = []
    next_col = width
    for field in _FIELD_HEADER_ALIASES:
        if field in layout:
            continue
        layout[field] = next_col
        pending.append(field)
        next_col += 1

    return {"layout": layout, "pending_appends": pending}


def _build_roster_rows(guild: discord.Guild, cfg: dict) -> list[list[str]]:
    """
    Build the rows that will be written to the sheet, including a header row.
    Column ordering matches cfg's *_col indices; gaps are filled with empty
    strings so each row is at least max(col_index)+1 cells wide.
    """
    role_filter = cfg.get("role_filter_id", 0)
    cols = _bot_managed_cols(cfg)
    width = max(cols.keys()) + 1

    header_row = [""] * width
    for idx, (label, _) in cols.items():
        header_row[idx] = label

    member_rows: list[list[str]] = []
    members = sorted(
        (m for m in guild.members if _eligible(m, role_filter)),
        key=lambda m: m.display_name.lower(),
    )
    for m in members:
        row = [""] * width
        for idx, (_, value_fn) in cols.items():
            row[idx] = value_fn(m)
        member_rows.append(row)

    return [header_row, *member_rows]


def _merge_with_existing(
    new_rows: list[list[str]], existing: list[list[str]], cfg: dict,
    guild: discord.Guild | None = None,
) -> tuple[list[list[str]], dict]:
    """Merge bot-managed columns from `new_rows` with alliance-owned
    columns preserved from `existing`.

    Returns `(merged_rows, name_match_report)`. The report is:

        {
            "matched_by_id":   [<discord_id>, ...],
            "matched_by_name": [<name>, ...],
            "ambiguous":       [<name>, ...],
            "no_match":        [<name>, ...],
        }

    Strategy:
      * The bot writes the columns named in `_bot_managed_cols`. Every
        OTHER column is alliance data (custom Power column, the
        `not_on_discord` flag, an annotation column, etc.) and is
        preserved as-is per Discord-ID match.
      * The header row keeps the bot's labels for managed columns and
        the alliance's labels for everything else.
      * Members who left the alliance — and whose data therefore drops
        out of `new_rows` — also lose their custom-column data. That's
        the correct behaviour: leaving the alliance means their row
        is gone.
      * Non-Discord alliance members (alliance plays the game but
        doesn't use Discord) live in hand-typed rows the bot would
        otherwise drop because they aren't in `new_rows`. Rows
        explicitly flagged via the bot-maintained "Is this user in
        Discord?" column (= "No") or the legacy `not_on_discord`
        column (truthy) are carried forward verbatim after the main
        merge so the storm officer view can still surface them in the
        "Not voted yet" bucket and accept on-behalf votes.
      * New members joining get blank cells in custom columns; the
        alliance can fill them in.

    Name-fallback row matching (#226 follow-up):
      * Existing rows keyed by Discord ID merge by ID (the original
        path). These count in `matched_by_id`.
      * Existing rows with a blank Discord ID try a name match: the
        row's `name_col` cell is looked up against a live-guild
        index built from member.name + member.display_name. A single
        unambiguous live match populates the Discord ID into the
        existing row before the merge, so the alliance's custom-
        column data threads through to the synced row. These count
        in `matched_by_name`. Multi-match (`ambiguous`) and no-match
        (`no_match`) rows stay as-is so the bot never silently writes
        the wrong member's Discord ID. The caller can surface the
        report in the setup-time preview.
      * `guild=None` skips the name-fallback entirely (preserves the
        pre-#226 behaviour for callers that don't have a guild handle).
    """
    report = {
        "matched_by_id":   [],
        "matched_by_name": [],
        "ambiguous":       [],
        "no_match":        [],
    }

    if not new_rows:
        return new_rows, report

    bot_cols = set(_bot_managed_cols(cfg).keys())
    id_col   = cfg["discord_id_col"]
    name_col = cfg["name_col"]
    display_col = cfg["display_col"]

    new_header = new_rows[0]
    existing_header = existing[0] if existing else []
    width = max(len(new_header), len(existing_header))

    # Header — bot owns its columns, alliance keeps theirs.
    header = list(new_header) + [""] * (width - len(new_header))
    for i in range(width):
        if i in bot_cols:
            continue
        if i < len(existing_header) and existing_header[i]:
            header[i] = existing_header[i]

    # Build a live-member name index for the name-fallback pass. Maps
    # lowercased Discord username + lowercased display_name → list of
    # member ids (single-element list = unambiguous, multi-element =
    # ambiguous and the bot declines to guess).
    name_to_ids: dict[str, list[int]] = {}
    if guild is not None:
        for m in getattr(guild, "members", []) or []:
            if getattr(m, "bot", False):
                continue
            for candidate in (getattr(m, "name", ""), getattr(m, "display_name", "")):
                key = (candidate or "").strip().lower()
                if not key:
                    continue
                bucket = name_to_ids.setdefault(key, [])
                if m.id not in bucket:
                    bucket.append(m.id)

    # Locate the non-Discord flag columns on the existing header so we
    # can both (a) skip name-fallback for rows the alliance explicitly
    # marked as non-Discord — a hand-typed name colliding with a real
    # Discord user must NOT silently bind that user's ID into the
    # alliance row — and (b) carry those rows forward after the merge
    # loop instead of dropping them. Both lookups are case-insensitive.
    def _find_existing_col(header_label: str) -> int:
        target = header_label.strip().lower()
        for idx, cell in enumerate(existing_header):
            if str(cell or "").strip().lower() == target:
                return idx
        return -1

    presence_idx = _find_existing_col(DISCORD_FLAG_COLUMN_HEADER)
    legacy_idx = _find_existing_col("not_on_discord")
    if legacy_idx < 0:
        legacy_idx = _find_existing_col("not on discord")
    _NON_DISCORD_TRUTHY = {"1", "true", "yes", "y", "x", "t"}

    def _row_is_non_discord(row: list[str]) -> bool:
        if presence_idx >= 0 and presence_idx < len(row):
            if (row[presence_idx] or "").strip().lower() == "no":
                return True
        if legacy_idx >= 0 and legacy_idx < len(row):
            if (row[legacy_idx] or "").strip().lower() in _NON_DISCORD_TRUTHY:
                return True
        return False

    # Walk existing rows: ID-match path first, then name-fallback for
    # rows with blank Discord ID. Populate the row's ID column when
    # a name-fallback match succeeds so the downstream ID-keyed merge
    # picks it up like any other matched row.
    existing_processed: list[list[str]] = []
    existing_by_id: dict[str, list[str]] = {}
    for raw_row in (existing[1:] if existing else []):
        row = list(raw_row)
        if _row_is_non_discord(row):
            # Explicit non-Discord flag — skip the ID-match and
            # name-fallback paths. Carried forward after the merge loop.
            continue
        did = (row[id_col] if id_col < len(row) else "").strip()
        if did:
            report["matched_by_id"].append(did)
            existing_by_id[did] = row
            existing_processed.append(row)
            continue
        # No Discord ID — try name fallback against the row's name
        # column first, then display-name column.
        name_cell = (
            row[name_col].strip().lower()
            if name_col < len(row) else ""
        )
        display_cell = (
            row[display_col].strip().lower()
            if display_col < len(row) else ""
        )
        candidate_name = name_cell or display_cell
        if not candidate_name:
            # Empty row with no name — nothing to match. Skip.
            existing_processed.append(row)
            continue
        matches = name_to_ids.get(candidate_name, [])
        # If the row had a name in `name_cell` AND that didn't match,
        # try `display_cell` too — alliances often store in-game names
        # in the display slot.
        if not matches and name_cell and display_cell and display_cell != name_cell:
            matches = name_to_ids.get(display_cell, [])
        if len(matches) == 1:
            new_id = str(matches[0])
            # Extend the row if it's too short to address id_col.
            while len(row) <= id_col:
                row.append("")
            row[id_col] = new_id
            existing_by_id[new_id] = row
            report["matched_by_name"].append(candidate_name)
        elif len(matches) > 1:
            report["ambiguous"].append(candidate_name)
        else:
            report["no_match"].append(candidate_name)
        existing_processed.append(row)

    merged_rows = [header]
    for new_row in new_rows[1:]:
        merged = list(new_row)
        if len(merged) < width:
            merged.extend([""] * (width - len(merged)))
        did = merged[id_col] if id_col < len(merged) else ""
        old = existing_by_id.get(did.strip())
        if old:
            for i in range(width):
                if i in bot_cols:
                    continue
                if i < len(old):
                    merged[i] = old[i]
        merged_rows.append(merged)

    # Carry forward explicitly-flagged non-Discord rows. The merge loop
    # above only iterates rows pulled from Discord, so without this pass
    # any hand-typed alliance member who doesn't use Discord disappears
    # on every sync — breaking the storm officer view's non-Discord
    # on-behalf voting path.
    for raw_row in (existing[1:] if existing else []):
        if not _row_is_non_discord(raw_row):
            continue
        preserved = list(raw_row)
        if len(preserved) < width:
            preserved.extend([""] * (width - len(preserved)))
        merged_rows.append(preserved)

    return merged_rows, report


def write_roster(guild: discord.Guild, cfg: dict) -> tuple[int, dict]:
    """
    Rebuild the configured tab with a fresh roster while preserving
    alliance-owned columns (custom Power column, `not_on_discord`,
    etc.) per Discord-ID match — with a name-fallback pass for
    existing rows that don't yet have a Discord ID populated.

    Returns `(member_count, name_match_report)`:
      * `member_count` — number of member rows written (excluding
        header), the same int the pre-#226 version returned.
      * `name_match_report` — `{"matched_by_id": [...],
        "matched_by_name": [...], "ambiguous": [...], "no_match":
        [...]}` from `_merge_with_existing`. Callers that don't care
        can ignore it; the setup-time preview surfaces it so the
        alliance sees how many rows were threaded through by name.

    The caller is responsible for ensuring the guild's member cache is
    populated (via `await guild.chunk()`) before invoking this function;
    otherwise `guild.members` may only contain a handful of members that
    Discord has surfaced via interactions, and the resulting roster will
    be incomplete. `_warn_if_cache_looks_thin` logs a warning when the
    Discord-side member_count is much larger than the cached size, which
    catches missing-intent and missing-chunk cases at runtime.
    """
    _warn_if_cache_looks_thin(guild)
    new_rows = _build_roster_rows(guild, cfg)
    ws = get_member_roster_sheet(guild.id, cfg["tab_name"])
    try:
        existing = ws.get_all_values()
    except Exception:
        existing = []
    merged, name_match_report = _merge_with_existing(
        new_rows, existing, cfg, guild=guild,
    )
    # Ensure the "Is this user in Discord?" column exists on the
    # sheet and is filled with bot-derived Yes/No values. Returns the
    # final column index so the data-validation request can target it.
    flag_col_idx = _ensure_discord_flag_column(merged, guild, cfg)
    ws.clear()
    if merged:
        ws.update("A1", merged, value_input_option="USER_ENTERED")
    # Apply the Yes/No dropdown data-validation rule on the new column.
    # Best-effort — a Sheets API failure here doesn't roll back the row
    # write (the column's values are correct either way).
    if flag_col_idx is not None and len(merged) > 1:
        try:
            _apply_discord_flag_validation(
                guild.id, ws, flag_col_idx, row_count=len(merged),
            )
        except Exception as e:
            logger.warning(
                "[ROSTER] data-validation rule write failed for guild=%s: %s",
                guild.id, e,
            )
    update_roster_last_synced(guild.id, datetime.now(timezone.utc).isoformat())
    return max(0, len(merged) - 1), name_match_report


def _ensure_discord_flag_column(
    merged: list[list[str]], guild: discord.Guild, cfg: dict,
) -> int | None:
    """Ensure the bot-maintained presence column exists in `merged` and
    fill every member row with "Yes" or "No" based on live guild
    membership.

    Mutates `merged` in place. Returns the column index (0-based) so
    the caller can target the data-validation request, or None when
    `merged` is empty.

    Header lookup is case-insensitive against
    `DISCORD_FLAG_COLUMN_HEADER`. If the column doesn't exist yet, a
    new column is appended at the right edge — every existing row is
    extended with a blank cell that the value fill then overwrites.
    """
    if not merged:
        return None

    header = merged[0]
    target = DISCORD_FLAG_COLUMN_HEADER.strip().lower()
    flag_idx = -1
    for i, cell in enumerate(header):
        if str(cell or "").strip().lower() == target:
            flag_idx = i
            break

    if flag_idx < 0:
        # Append a new column at the right edge for every row.
        flag_idx = len(header)
        header.append(DISCORD_FLAG_COLUMN_HEADER)
        for row in merged[1:]:
            row.append("")
    else:
        header[flag_idx] = DISCORD_FLAG_COLUMN_HEADER  # canonical casing
        for row in merged[1:]:
            while len(row) <= flag_idx:
                row.append("")

    # Build a (non-bot) member ID lookup once. `int()` conversion is
    # done in the same pass so the per-row check is a set membership.
    live_ids: set[int] = set()
    for m in getattr(guild, "members", []) or []:
        if getattr(m, "bot", False):
            continue
        try:
            live_ids.add(int(m.id))
        except (TypeError, ValueError):
            continue

    id_col = cfg["discord_id_col"]
    for row in merged[1:]:
        discord_id = (row[id_col] if id_col < len(row) else "").strip()
        is_on_discord = False
        if discord_id.isdigit():
            try:
                is_on_discord = int(discord_id) in live_ids
            except (TypeError, ValueError):
                is_on_discord = False
        row[flag_idx] = "Yes" if is_on_discord else "No"

    return flag_idx


def _apply_discord_flag_validation(
    guild_id: int, ws, flag_col_idx: int, *, row_count: int,
) -> None:
    """Write a Yes/No-dropdown data validation rule on the presence
    column for every member row. Spans rows 2..row_count (skipping
    header). gspread surfaces this via the spreadsheet-level
    `batch_update`."""
    spreadsheet = get_spreadsheet(guild_id)
    if spreadsheet is None:
        return  # no Sheet configured at all — nothing to validate
    sheet_id_raw = getattr(ws, "id", None)
    if sheet_id_raw is None:
        return
    try:
        sheet_id_int = int(sheet_id_raw)
    except (TypeError, ValueError):
        # Worksheet's `.id` isn't numeric (test fake, malformed mock).
        # Silent skip — the row values are still correct on the Sheet.
        return
    request = {
        "requests": [
            {
                "setDataValidation": {
                    "range": {
                        "sheetId":          sheet_id_int,
                        "startRowIndex":    1,            # skip header
                        "endRowIndex":      row_count,    # exclusive
                        "startColumnIndex": flag_col_idx,
                        "endColumnIndex":   flag_col_idx + 1,
                    },
                    "rule": {
                        "condition": {
                            "type":   "ONE_OF_LIST",
                            "values": [
                                {"userEnteredValue": "Yes"},
                                {"userEnteredValue": "No"},
                            ],
                        },
                        "showCustomUi": True,
                        "strict":       True,
                        "inputMessage": (
                            "Auto-filled by the LW Alliance Helper bot. "
                            "Override to Yes/No if needed."
                        ),
                    },
                },
            },
        ],
    }
    spreadsheet.batch_update(request)


def _warn_if_cache_looks_thin(guild: discord.Guild) -> None:
    """If the cached member list is wildly smaller than Discord's reported
    guild size, the Server Members Intent probably isn't enabled (or the
    caller forgot to chunk). Log loudly so the symptom — "/members sync
    wrote 0 rows" — has a breadcrumb pointing at the cause."""
    try:
        cached_count = len(guild.members)
        raw_total    = getattr(guild, "member_count", None)
        total_count  = int(raw_total) if isinstance(raw_total, int) else 0
    except (AttributeError, TypeError) as e:
        # Defensive: shouldn't happen with a real `discord.Guild`, but tests
        # and edge cases (None members list) shouldn't suppress real errors.
        print(f"[ROSTER] _warn_if_cache_looks_thin diagnostic failed for "
              f"guild {getattr(guild, 'id', '?')}: {e}")
        return
    if total_count > 1 and cached_count < max(2, total_count // 2):
        print(
            f"[ROSTER] Guild {guild.id}: only {cached_count}/{total_count} members "
            f"in cache. Enable the SERVER MEMBERS INTENT in the Discord Developer "
            f"Portal (Bot → Privileged Gateway Intents) — without it `guild.members` "
            f"can't see the full roster."
        )


async def _ensure_member_cache(guild: discord.Guild) -> None:
    """Force-load `guild.members` if it isn't already chunked.

    Safe to call on every sync — it short-circuits when the cache is
    already complete, and swallows the `ClientException` that fires when
    the members intent isn't actually granted (so the sync still attempts
    to run with whatever members are available, with the warning above
    explaining the partial result).
    """
    try:
        if not getattr(guild, "chunked", True):
            await guild.chunk()
    except discord.ClientException as e:
        # Raised when intents.members is False — i.e. the privileged intent
        # isn't enabled. Don't crash; let _warn_if_cache_looks_thin surface it.
        print(f"[ROSTER] guild.chunk() rejected for guild {guild.id}: {e}")
    except Exception as e:
        print(f"[ROSTER] guild.chunk() failed for guild {guild.id}: {e}")


# ── Cog ──────────────────────────────────────────────────────────────────────

class MemberRosterCog(commands.Cog):
    # /members is a top-level slash-command group containing overview /
    # sync. Leaves room for future roster-side leaves (audit, drift
    # detection, manual add/remove without re-running a full sync) to
    # land under the same namespace.
    members_group = app_commands.Group(
        name="members",
        description="💎 Member roster sync (Premium)",
    )

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        await self._auto_sync_if_enabled(member.guild)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self._auto_sync_if_enabled(member.guild)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Only resync if role membership changed (covers the role-filter case).
        if {r.id for r in before.roles} != {r.id for r in after.roles}:
            await self._auto_sync_if_enabled(after.guild)

    async def _auto_sync_if_enabled(self, guild: discord.Guild):
        if not await premium.feature_gate("member_sync", guild.id, bot=self.bot):
            return
        cfg = get_member_roster_config(guild.id)
        if not cfg.get("enabled") or not cfg.get("auto_sync"):
            return
        await _ensure_member_cache(guild)
        try:
            await asyncio.get_event_loop().run_in_executor(
                None, write_roster, guild, cfg,
            )
        except Exception as e:
            from config import describe_sheet_error
            print(
                f"[ROSTER] Auto-sync failed: "
                f"{describe_sheet_error(e, guild_id=guild.id, tab=cfg.get('tab_name'))}"
            )
            # Auto-sync runs on every member-join/leave/role-change, so a
            # transient error gets re-tried naturally. But unexpected bugs
            # (template typos, schema drift) should land in Sentry instead
            # of only stdout — the channel post path already logs, this
            # surfaces non-Discord-API failures for triage.
            try:
                import sentry_sdk
                sentry_sdk.capture_exception(e)
            except Exception:
                pass

    @members_group.command(
        name="overview",
        description="Roster source, sync state, and pointers into /members sync",
    )
    async def members_overview(self, interaction: discord.Interaction):
        from setup_cog import _has_leadership_or_admin
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to view the member roster.",
                ephemeral=True,
            )
            return

        is_premium = await premium.is_premium(
            interaction.guild_id, interaction=interaction, bot=self.bot,
        )
        cfg = get_member_roster_config(interaction.guild_id)

        embed = discord.Embed(
            title="👥 Member Roster Sync",
            description=(
                "Writes every Discord member to your alliance's Google Sheet "
                "so other Premium features (birthday DMs, train DMs, "
                "auto-mention, etc.) can find members by name. The sheet stays "
                "yours; this command only adds + updates rows."
            ),
            color=discord.Color.blurple() if is_premium else discord.Color.greyple(),
        )

        if not is_premium:
            embed.add_field(
                name="💎 Premium feature",
                value=(
                    "Member Roster Sync is part of Premium. "
                    "Run `/upgrade` to unlock it."
                ),
                inline=False,
            )
            await interaction.response.send_message(
                embed=embed, view=premium.upgrade_view(), ephemeral=True,
            )
            return

        enabled = bool(cfg.get("enabled"))
        tab_name = cfg.get("tab_name") or "Member Roster"
        auto_sync = bool(cfg.get("auto_sync"))
        last_synced = (cfg.get("last_synced_at") or "").strip()
        role_filter_id = int(cfg.get("role_filter_id") or 0)

        if enabled:
            lines = [
                f"**Sheet tab:** `{tab_name}`",
                f"**Auto-sync on join/leave/role-change:** "
                f"{'on' if auto_sync else 'off'}",
            ]
            if role_filter_id:
                role = interaction.guild.get_role(role_filter_id) if interaction.guild else None
                lines.append(
                    f"**Filtered to role:** "
                    f"{role.mention if role else f'`role #{role_filter_id}` (deleted?)'}"
                )
            else:
                lines.append("**Filtered to role:** *(all members)*")
            lines.append(
                f"**Last sync:** {last_synced or '*(never synced)*'}"
            )
            embed.add_field(
                name="Current state",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Not yet configured",
                value=(
                    "Run `/setup` → 👥 Member Sync to pick the destination tab and "
                    "(optionally) filter to a specific role."
                ),
                inline=False,
            )

        embed.add_field(
            name="Sub-commands",
            value=(
                "• `/members sync` — Rebuild the roster sheet now\n"
                "• `/setup` → 👥 Member Sync — Configure or change the roster destination"
            ),
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @members_group.command(
        name="sync",
        description="💎 Manually rebuild the member roster sheet now",
    )
    async def members_sync(self, interaction: discord.Interaction):
        from setup_cog import _has_leadership_or_admin
        if not _has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to sync the member roster.",
                ephemeral=True,
            )
            return

        if not await premium.feature_gate(
            "member_sync", interaction.guild_id,
            interaction=interaction, bot=self.bot,
        ):
            await interaction.response.send_message(
                embed=premium.premium_locked_embed(
                    feature_label="Member Roster Sync",
                    description=(
                        "Member Roster Sync writes every member's Discord ID to "
                        "your sheet so other Premium features (birthday DMs, "
                        "train DMs, auto-mention, etc.) can find them. "
                        "Run `/upgrade` to unlock it."
                    ),
                ),
                view=premium.upgrade_view(),
                ephemeral=True,
            )
            return

        cfg = get_member_roster_config(interaction.guild_id)
        if not cfg.get("enabled"):
            await interaction.response.send_message(
                "⚙️ Member Roster Sync isn't configured yet. Run `/setup` → 👥 Member Sync first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        await _ensure_member_cache(guild)
        try:
            count, _report = await asyncio.get_event_loop().run_in_executor(
                None, write_roster, guild, cfg,
            )
        except Exception as e:
            from config import describe_sheet_error
            diagnosis = describe_sheet_error(e, tab=cfg["tab_name"])
            print(
                f"[ROSTER] /members sync failed: "
                f"{describe_sheet_error(e, guild_id=interaction.guild_id, tab=cfg['tab_name'])}"
            )
            await interaction.followup.send(
                f"⚠️ Sync failed: {diagnosis}\nMake sure the bot has access to your sheet "
                f"and that the **{cfg['tab_name']}** tab can be written to.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Synced {count} members to the {cfg['tab_name']} tab.",
            ephemeral=True,
        )


# ── Setup-hub button launcher ────────────────────────────────────────────────
#
# `/setup` → 👥 Member Sync collapsed into the `/setup` hub in #201. The button
# `👥 Member Sync` on the hub dispatches into this helper, which preserves
# the leadership-or-admin + Premium + channel-perms gating that used to
# live in the slash command.

async def _launch_member_roster_setup(interaction: discord.Interaction, bot) -> None:
    from setup_cog import _has_leadership_or_admin, _check_wizard_can_run
    if not _has_leadership_or_admin(interaction):
        await interaction.response.send_message(
            "⛔ You need the leadership role (or admin) to configure the member roster.",
            ephemeral=True,
        )
        return

    # Premium gate before the channel-perms pre-check: a free user
    # trying this command should see the upsell, not a perms error.
    if not await premium.feature_gate(
        "member_sync", interaction.guild_id,
        interaction=interaction, bot=bot,
    ):
        await interaction.response.send_message(
            embed=premium.premium_locked_embed(
                feature_label="Member Roster Sync",
                description=(
                    "Member Roster Sync is part of LW Alliance Helper Premium. "
                    "Run `/upgrade` to unlock it."
                ),
            ),
            view=premium.upgrade_view(),
            ephemeral=True,
        )
        return

    if not await _check_wizard_can_run(interaction, "setup"):
        return

    await interaction.response.send_message(
        "⚙️ Starting Member Roster Sync setup — check the channel for prompts.",
        ephemeral=True,
    )
    await run_member_roster_setup(interaction, bot)


# ── Wizard ───────────────────────────────────────────────────────────────────

async def run_member_roster_setup(interaction: discord.Interaction, bot):
    """Walk an admin through configuring the member-roster sync tab."""
    import wizard_registry
    from wizard_registry import wait_view_or_cancel
    from setup_cog import (
        ask_keep_or_change, YesNoView, ask_proceed_with_existing_config,
    )
    from config import has_member_roster_config

    guild_id     = interaction.guild_id
    channel      = interaction.channel
    user         = interaction.user
    cancel_event = wizard_registry.register(user.id)

    current   = get_member_roster_config(guild_id)
    guild_cfg = get_config(guild_id)
    member_role_id = guild_cfg.member_role_id if guild_cfg else 0
    roster_already_configured = has_member_roster_config(guild_id)

    # ── If already configured, show summary and offer edit or cancel ─────────
    if roster_already_configured:
        rf_id = current.get("role_filter_id") or 0
        fields = [
            ("Roster Tab", current.get("tab_name") or "*not set*"),
            (
                "Role Filter",
                f"<@&{rf_id}>" if rf_id else "All non-bots",
            ),
            ("Auto-Sync", "✅ Enabled" if current.get("auto_sync") else "❌ Disabled"),
        ]
        proceed = await ask_proceed_with_existing_config(
            channel,
            title="💎 Current Member Roster Sync Setup",
            description="Member Roster Sync is already configured. Would you like to edit these settings?",
            fields=fields,
            cancel_event=cancel_event,
            no_changes_message="✅ No changes made. Member Roster Sync is still active.",
        )
        if proceed is not True:
            wizard_registry.unregister(user.id, cancel_event)
            return

    await channel.send(
        "💎 **Member Roster Sync Setup**\n"
        "Configure how the bot writes your roster (Discord IDs + names) to a "
        "sheet tab. Other premium features look this up to send DMs and tag members."
    )

    # ── Step 1: Tab name ──────────────────────────────────────────────────────
    # Pass `current=` separately from `default=` so the helper renders
    # "Keep current: X" instead of "Use default: X" when a guild-saved
    # value is present.
    tab_name = await ask_keep_or_change(
        channel,
        "**Step 1 of 3 — Roster Tab**\n"
        "Which tab should the roster be written to?\n"
        "⚠️ *If the tab doesn't exist, the bot will create it automatically.*\n"
        "⚠️ *The tab will be **completely overwritten** on each sync.*",
        default="Member Roster",
        current=current.get("tab_name", ""),
        modal_title="Roster Tab Name",
        modal_label="Tab name",
        timeout_cmd="setup_members",
        cancel_event=cancel_event,
    )
    if tab_name is None:
        return

    # ── Step 2: Filter to member role only? ───────────────────────────────────
    filter_view = YesNoView()
    role_label  = f"<@&{member_role_id}>" if member_role_id else "the configured member role"
    await channel.send(
        f"**Step 2 of 3 — Filter by Member Role?**\n"
        f"Should the roster only include members who have {role_label}?\n"
        f"Pick **No** to include every (non-bot) member of the server.",
        view=filter_view,
    )
    await wait_view_or_cancel(filter_view, cancel_event)
    if filter_view.cancelled:
        return
    if filter_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup` → 👥 Member Sync to start again.")
        return
    role_filter_id = member_role_id if filter_view.selected else 0

    # ── Step 3: Auto-sync on join/leave/role-change? ──────────────────────────
    auto_view = YesNoView()
    await channel.send(
        "**Step 3 of 3 — Auto-Sync?**\n"
        "Should the bot automatically re-sync when members join, leave, or "
        "change roles?\nPick **No** to only sync on `/members sync`.",
        view=auto_view,
    )
    await wait_view_or_cancel(auto_view, cancel_event)
    if auto_view.cancelled:
        return
    if auto_view.selected is None:
        await channel.send("⏰ Timed out. Run `/setup` → 👥 Member Sync to start again.")
        return
    auto_sync = 1 if auto_view.selected else 0

    # ── Step 4 (conditional): Layout detection + preview ─────────────────────
    # When the configured tab already has data, run the header-aware
    # detector against the existing layout. If any bot-managed column
    # would land on top of alliance data (because the alliance kept
    # custom values in A-E and the bot's default layout would overwrite
    # them), the alliance gets a confirm-or-remap preview before we
    # write. On a brand-new sheet (no headers), skip this step entirely
    # — there's nothing to collide with.
    detected_layout = None
    name_match_dry_run = None
    try:
        guild = interaction.guild
        await _ensure_member_cache(guild)
        from config import get_member_roster_sheet
        _preview_ws = await asyncio.get_event_loop().run_in_executor(
            None, get_member_roster_sheet, guild_id, tab_name,
        )
        _existing_rows = await asyncio.get_event_loop().run_in_executor(
            None, _preview_ws.get_all_values,
        )
    except Exception as e:
        # If we can't read the tab (perms, network, missing tab), fall
        # through to the legacy hardcoded-layout flow. The initial sync
        # below will surface the error with a clearer diagnosis.
        logger.warning(
            "[ROSTER SETUP] layout preview read failed for guild=%s: %s",
            guild_id, e,
        )
        _existing_rows = []

    # Layout detection is meaningful only when there's a header row.
    if _existing_rows and _existing_rows[0]:
        detected_layout = detect_column_layout(_existing_rows[0])

    if detected_layout is not None:
        # Dry-run name match to surface counts in the preview without
        # writing anything yet. Builds an effective cfg from the
        # proposed layout so `_merge_with_existing` has the right
        # column indices.
        _trial_cfg = {
            **detected_layout["layout"],
            "tab_name":       tab_name,
            "role_filter_id": role_filter_id,
            "auto_sync":      auto_sync,
        }
        # Skip the merge — we only want the report, not the merged
        # rows. _merge_with_existing also runs the name fallback.
        _new_rows = _build_roster_rows(guild, _trial_cfg)
        _merged, name_match_dry_run = _merge_with_existing(
            _new_rows, _existing_rows, _trial_cfg, guild=guild,
        )

        layout = detected_layout["layout"]
        pending = set(detected_layout["pending_appends"])

        def _col_letter(idx: int) -> str:
            letters = ""
            n = idx + 1
            while n > 0:
                n, rem = divmod(n - 1, 26)
                letters = chr(ord("A") + rem) + letters
            return letters

        field_label = {
            "discord_id_col": "Discord ID",
            "name_col":       "Name",
            "display_col":    "Display Name",
            "joined_col":     "Joined",
            "roles_col":      "Roles",
        }
        layout_lines = []
        for field, label_text in field_label.items():
            idx = layout[field]
            status = (
                "appending — no existing match"
                if field in pending else "matched existing header"
            )
            layout_lines.append(
                f"• **{label_text}** → column **{_col_letter(idx)}**  "
                f"_({status})_"
            )
        # Surface the bot-maintained presence column too so the
        # preview accounts for every column the bot will touch — a
        # tester saw column H in the "preserved" list and assumed it
        # was their data, when it was the bot's "Is this user in
        # Discord?" column from a prior sync.
        existing_header = _existing_rows[0]
        flag_header_lc = DISCORD_FLAG_COLUMN_HEADER.strip().lower()
        presence_idx: int | None = None
        for i, cell in enumerate(existing_header):
            if str(cell or "").strip().lower() == flag_header_lc:
                presence_idx = i
                break
        if presence_idx is not None:
            layout_lines.append(
                f"• **Is this user in Discord?** → column "
                f"**{_col_letter(presence_idx)}**  "
                f"_(matched existing header — bot refreshes Yes/No each sync)_"
            )
        else:
            # New presence column will append at the right edge AFTER
            # the 5 bot fields + alliance columns. Compute the
            # eventual landing index for the preview.
            future_presence_idx = max(
                list(layout.values()) + [len(existing_header) - 1],
            ) + 1
            layout_lines.append(
                f"• **Is this user in Discord?** → column "
                f"**{_col_letter(future_presence_idx)}**  "
                f"_(appending — bot-maintained Yes/No column)_"
            )

        match_lines = [
            f"• **{len(name_match_dry_run['matched_by_id'])}** rows "
            f"matched by Discord ID",
        ]
        if name_match_dry_run["matched_by_name"]:
            match_lines.append(
                f"• **{len(name_match_dry_run['matched_by_name'])}** rows "
                f"matched by name — Discord ID will be auto-populated"
            )
        if name_match_dry_run["ambiguous"]:
            match_lines.append(
                f"• **{len(name_match_dry_run['ambiguous'])}** rows "
                f"have ambiguous names (multiple live members match) "
                f"— left as-is"
            )
        if name_match_dry_run["no_match"]:
            match_lines.append(
                f"• **{len(name_match_dry_run['no_match'])}** rows "
                f"have no live Discord match — Discord ID left blank"
            )

        # `bot_claimed` covers the 5 fields in `_bot_managed_cols`
        # PLUS the presence column when it exists on the sheet — that
        # column is bot-maintained (refreshed by
        # `_ensure_discord_flag_column` after every merge), so it
        # mustn't appear in the "Custom data preserved" list. Tester
        # report: "the bot said custom data for F, G, H were
        # preserved but H was the presence column." `presence_idx`
        # was computed above when building the layout preview lines.
        bot_claimed = set(layout.values())
        if presence_idx is not None:
            bot_claimed.add(presence_idx)
        preserved_letters = [
            _col_letter(i)
            for i in range(len(existing_header))
            if i not in bot_claimed
        ]
        preserved_blurb = (
            f"Custom data in columns "
            f"{', '.join(preserved_letters)} is preserved."
            if preserved_letters else
            "No custom alliance columns detected — every column is "
            "claimed by the bot."
        )

        class _LayoutRemapModal(discord.ui.Modal):
            def __init__(self, current_layout: dict):
                super().__init__(title="Remap bot-managed columns")
                self.confirmed = False
                self.current_layout = current_layout
                self.inputs: dict[str, discord.ui.TextInput] = {}
                for field, label_text in field_label.items():
                    txt = discord.ui.TextInput(
                        label=f"{label_text} column letter",
                        placeholder="e.g. A",
                        default=_col_letter(current_layout[field]),
                        required=True,
                        max_length=2,
                    )
                    self.inputs[field] = txt
                    self.add_item(txt)

            async def on_submit(self, inter: discord.Interaction):
                self.confirmed = True
                await inter.response.defer()
                self.stop()

        class _LayoutConfirmView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)
                self.outcome: str | None = None
                self.modal: _LayoutRemapModal | None = None
                self.cancelled = False

            @discord.ui.button(
                label="✅ Looks good — sync now",
                style=discord.ButtonStyle.success,
            )
            async def confirm(
                self, inter: discord.Interaction, _btn: discord.ui.Button,
            ):
                self.outcome = "confirm"
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content="✅ Layout confirmed — syncing now.",
                    view=self,
                )
                self.stop()

            @discord.ui.button(
                label="🔧 Remap manually",
                style=discord.ButtonStyle.secondary,
            )
            async def remap(
                self, inter: discord.Interaction, _btn: discord.ui.Button,
            ):
                self.modal = _LayoutRemapModal(layout)
                await inter.response.send_modal(self.modal)
                await self.modal.wait()
                if self.modal.confirmed:
                    self.outcome = "remap"
                else:
                    # Modal cancelled — leave the picker active so the
                    # alliance can retry.
                    return
                for item in self.children:
                    item.disabled = True
                try:
                    if inter.message:
                        await inter.message.edit(view=self)
                except discord.HTTPException:
                    pass
                self.stop()

            @discord.ui.button(
                label="↩️ Cancel",
                style=discord.ButtonStyle.danger,
            )
            async def cancel(
                self, inter: discord.Interaction, _btn: discord.ui.Button,
            ):
                self.outcome = "cancel"
                self.cancelled = True
                for item in self.children:
                    item.disabled = True
                await wizard_registry.safe_edit_response(
                    inter,
                    content="↩️ Member Sync setup cancelled.",
                    view=self,
                )
                self.stop()

        preview = _LayoutConfirmView()
        await channel.send(
            f"🔍 **Existing data detected in tab `{tab_name}`.**\n\n"
            f"**Bot-managed columns will land at:**\n"
            + "\n".join(layout_lines) + "\n\n"
            f"**Row matching:**\n"
            + "\n".join(match_lines) + "\n\n"
            f"_{preserved_blurb}_",
            view=preview,
        )
        await wait_view_or_cancel(preview, cancel_event)
        if preview.cancelled or preview.outcome == "cancel":
            wizard_registry.unregister(user.id, cancel_event)
            return
        if preview.outcome is None:
            await channel.send(
                "⏰ Timed out. Run `/setup` → 👥 Member Sync to start again."
            )
            wizard_registry.unregister(user.id, cancel_event)
            return

        # If the officer remapped, parse the modal letters and overwrite
        # `layout` so the column indices saved below come from their
        # picks. Bad input falls back to the auto-detected layout for
        # that field — better than refusing to save.
        if preview.outcome == "remap" and preview.modal is not None:
            for field, txt in preview.modal.inputs.items():
                raw = (txt.value or "").strip().upper()
                if len(raw) == 1 and "A" <= raw <= "Z":
                    layout[field] = ord(raw) - ord("A")
                # else leave the auto-detected index in place.
    else:
        # No header row → blank sheet (or unreachable). Use the hardcoded
        # default layout.
        layout = {
            "discord_id_col": 0, "name_col": 1, "display_col": 2,
            "joined_col": 3, "roles_col": 4,
        }

    save_member_roster_config(
        guild_id,
        enabled=1, tab_name=tab_name,
        role_filter_id=role_filter_id, auto_sync=auto_sync,
        discord_id_col=layout["discord_id_col"],
        name_col=layout["name_col"],
        display_col=layout["display_col"],
        joined_col=layout["joined_col"],
        roles_col=layout["roles_col"],
    )

    # ── Initial sync ──────────────────────────────────────────────────────────
    cfg   = get_member_roster_config(guild_id)
    await _ensure_member_cache(guild)
    try:
        count, _report = await asyncio.get_event_loop().run_in_executor(
            None, write_roster, guild, cfg,
        )
    except Exception as e:
        from config import describe_sheet_error
        diagnosis = describe_sheet_error(e, tab=cfg["tab_name"])
        print(
            f"[ROSTER] /setup → 👥 Member Sync initial sync failed: "
            f"{describe_sheet_error(e, guild_id=guild_id, tab=cfg['tab_name'])}"
        )
        await channel.send(
            f"✅ Saved configuration but the initial sync failed: {diagnosis}\n"
            f"Try running `/members sync` once you've fixed the issue."
        )
        wizard_registry.unregister(user.id, cancel_event)
        return

    embed = discord.Embed(
        title="✅ Member Roster Sync Configured",
        color=discord.Color.gold(),
    )
    embed.add_field(name="Tab",          value=tab_name, inline=True)
    embed.add_field(
        name="Role Filter",
        value=f"<@&{role_filter_id}>" if role_filter_id else "All non-bots",
        inline=True,
    )
    embed.add_field(name="Auto-Sync",    value="Enabled" if auto_sync else "Disabled", inline=True)
    embed.add_field(name="Initial sync", value=f"**{count}** members written", inline=False)
    embed.add_field(
        name="👤 Alliance members not on Discord",
        value=(
            "Add them by typing a row directly into the sheet with the "
            "**Is this user in Discord?** column set to **No**. Sync will "
            "preserve those rows, and storm sign-up views will surface "
            "them under 'Not voted yet' so leadership can cast "
            "on-behalf votes for them."
        ),
        inline=False,
    )
    embed.set_footer(text="Run /members sync to re-sync manually any time.")
    await channel.send(embed=embed)
    wizard_registry.unregister(user.id, cancel_event)
    print(f"[ROSTER] Sync configured for guild {guild_id} ({count} members)")


async def setup(bot: commands.Bot):
    await bot.add_cog(MemberRosterCog(bot))
