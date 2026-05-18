"""
Officer view for storm sign-ups — reached via the
`👁️ View sign-ups + set up teams` button on `/desertstorm` and
`/canyonstorm` (hub-restructure #187; legacy `/desertstorm signups`
subcommand pre-#125).

Leadership-only surface that displays who's voted for an event,
grouped by vote bucket, with a path to cast on-behalf votes for
members who don't use Discord.

Enumeration:
  * Discord members come from `guild.members` filtered by the
    member_roster `role_filter_id` (so the same role gate that drives
    the alliance roster sync drives the officer view).
  * Non-Discord members are read from the alliance's roster Sheet via
    a `not_on_discord` column (any truthy value flags the row). They
    appear in the "Not voted yet" bucket pre-emptively so leadership
    can see who still needs an on-behalf vote — without this, an
    officer would only see non-Discord members AFTER casting a vote
    for them, which is the wrong direction.

Buckets:
  🅰 Voted Team A    — vote=a
  🅱 Voted Team B    — vote=b
  🔄 Voted Either    — vote=either
  ❌ Voted Cannot    — vote=cannot
  ❓ Not voted yet   — Discord member or roster row with no signup row

The "Vote on behalf" button captures the casting officer's Discord
ID alongside the vote, so audit history shows who recorded what. The
on-behalf picker view (#168) sources its Member Select from the roster
Sheet so typos can't create phantom signup rows — the officer can only
pick names that already exist on the roster.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import logging
from typing import Optional

import discord

from storm_event_hub import HUB_COMMAND, HUB_BTN_VIEW_SIGNUPS, HUB_BTN_PRESETS

logger = logging.getLogger(__name__)


# ── Bucket layout ────────────────────────────────────────────────────────────

# Per-process stale-roster-ID warning dedupe — keyed on
# `(guild_id, frozenset(stale_ids))`. The View's refresh button and
# the on-behalf picker view both call `_read_roster_rows`; without
# dedup, every click re-logged the same stale-ID list. The set is
# bounded by the number of stale-ID combinations across reachable
# guilds.
_STALE_ID_LOG_MEMO: set[tuple[int, frozenset]] = set()


_BUCKET_ORDER = ("a", "b", "either", "cannot", "not_voted")
_BUCKET_LABELS = {
    "a":         "🅰️ Voted Team A",
    "b":         "🅱️ Voted Team B",
    "either":    "🔄 Voted Either",
    "cannot":    "❌ Voted Cannot",
    "not_voted": "❓ Not voted yet",
}


def _next_event_date(today: _dt.date | None = None) -> str:
    """Back-compat shim — delegates to `storm_date_helpers.next_event_date`
    without the guild/event-type lookup. Kept on the module surface
    because at least one stale test patches `_next_event_date` directly.
    New callers should reach for the helper module.
    """
    today = today or _dt.date.today()
    days_ahead = (6 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    return (today + _dt.timedelta(days=days_ahead)).isoformat()


def _read_roster_rows(
    guild_id: int, *, guild: discord.Guild | None = None,
) -> tuple[list[dict], list[str]]:
    """Read the alliance's member-roster Sheet and return:
      (rows, errors)
    where each row is `{"discord_id": str, "name": str, "not_on_discord": bool}`.

    Returns ([], []) on any failure — the officer view degrades gracefully
    to "Discord members only" rather than blocking on a missing Sheet or
    a roster column that hasn't been added yet. The errors list is for
    callers that want to surface a soft warning.

    Non-Discord detection (#139) is tiered:
      1. If the row has a `not_on_discord` column with a truthy value,
         the alliance has explicitly flagged the row (current behaviour).
      2. Otherwise, infer:
         - Blank `discord_id` cell → non-Discord (member never had one).
         - Non-blank `discord_id` but `guild.get_member(int(id))` is None
           → non-Discord (member's left the server but the alliance
           still tracks them on the roster).
      3. The explicit column wins when present — alliance override is
         load-bearing.

    The `guild` kwarg is optional; when None, only tier 1 + the
    blank-discord-id half of tier 2 fire. Officer view always passes
    the live guild; the auto-fill / roster-builder path delegates here
    too for consistency.
    """
    import config
    errors: list[str] = []
    stale_ids: list[str] = []
    try:
        cfg = config.get_member_roster_config(guild_id)
    except Exception as e:
        return [], [f"roster-config read failed: {e}"]
    if not cfg.get("enabled"):
        return [], []

    try:
        ws = config.get_member_roster_sheet(guild_id, cfg.get("tab_name") or "Member Roster")
    except Exception as e:
        errors.append(f"roster-sheet open failed: {e}")
        return [], errors

    try:
        values = ws.get_all_values()
    except Exception as e:
        errors.append(f"roster-sheet read failed: {e}")
        return [], errors

    if not values:
        return [], []

    header = values[0]

    def _find_col(name: str) -> int:
        target = name.strip().lower()
        for idx, cell in enumerate(header):
            if cell.strip().lower() == target:
                return idx
        return -1

    id_col   = int(cfg.get("discord_id_col", 0))
    name_col = int(cfg.get("display_col", cfg.get("name_col", 1)))
    # Prefer the bot-maintained presence column when present. Falls
    # back to the legacy `not_on_discord` column for back-compat.
    presence_col = _find_col("is this user in discord?")
    not_col  = _find_col("not_on_discord")
    if not_col < 0:
        not_col = _find_col("not on discord")

    rows: list[dict] = []
    truthy = {"1", "true", "yes", "y", "x", "t"}
    # Used for the "explicit column wins" tier-1 / tier-2 logic.
    has_not_col = not_col >= 0
    has_presence_col = presence_col >= 0

    for row in values[1:]:
        discord_id = row[id_col].strip() if id_col < len(row) else ""
        name       = row[name_col].strip() if name_col < len(row) else ""
        if not (discord_id or name):
            continue

        # New presence column wins — bot writes this on every sync.
        if has_presence_col:
            presence_cell = (
                row[presence_col].strip().lower()
                if presence_col < len(row) else ""
            )
            if presence_cell == "yes":
                rows.append({
                    "discord_id":     discord_id,
                    "name":           name or discord_id,
                    "not_on_discord": False,
                })
                continue
            if presence_cell == "no":
                rows.append({
                    "discord_id":     discord_id,
                    "name":           name or discord_id,
                    "not_on_discord": True,
                })
                continue
            # Blank → fall through to legacy + inference.

        explicit_flag = ""
        if has_not_col and not_col < len(row):
            explicit_flag = row[not_col].strip().lower()
        explicit_set = explicit_flag in truthy

        # Tier 2 inference — only fires when no explicit flag is set.
        # (An empty cell in a present column means "no explicit flag";
        # we still infer in that case so alliances who add the column
        # later don't have to backfill every row to get the inference.)
        inferred = False
        if not explicit_set:
            if not discord_id:
                inferred = True
            elif not discord_id.isdigit():
                # Non-numeric ID ("TBD", "abc", "n/a") — alliance has
                # written a placeholder rather than a real Discord ID.
                # Treat as non-Discord per the #139 spec: "non-numeric →
                # non-Discord". The audit found this path silently
                # escaped before, keeping such rows mis-classified as
                # Discord members.
                inferred = True
            elif guild is not None:
                try:
                    member = guild.get_member(int(discord_id))
                except (TypeError, ValueError):
                    member = None
                # Bots aren't real alliance members. A roster row that
                # resolves to a bot (admin pasted the wrong ID) gets
                # the same treatment as a stale ID — flag it for
                # cleanup rather than counting the bot as Discord-on.
                if member is None or member.bot:
                    inferred = True
                    stale_ids.append(f"{name or '?'} (id {discord_id})")

        rows.append({
            "discord_id":     discord_id,
            "name":           name or discord_id,
            "not_on_discord": explicit_set or inferred,
        })

    if stale_ids:
        # Soft warning so leadership can clean up the roster Sheet.
        # The first 5 are surfaced via the embed warning surface; the
        # full list goes to logs.
        preview = ", ".join(stale_ids[:5])
        extra = f" (+{len(stale_ids) - 5} more)" if len(stale_ids) > 5 else ""
        errors.append(
            "stale Discord IDs on roster (member likely left the server): "
            f"{preview}{extra}"
        )
        # Dedup the log — refresh button + on-behalf picker re-call this
        # function on every click. Without the memo, a 5-stale-ID
        # roster would log 5 entries × every click. Memo key includes
        # the stale-ID set so a roster cleanup naturally clears it
        # (next read has fewer entries, fresh key).
        memo_key = (int(guild_id), frozenset(stale_ids))
        if memo_key not in _STALE_ID_LOG_MEMO:
            _STALE_ID_LOG_MEMO.add(memo_key)
            logger.warning(
                "[STORM OFFICER VIEW] stale roster Discord IDs for guild=%s: %s",
                guild_id, "; ".join(stale_ids),
            )

    return rows, errors


def _discord_member_pool(guild: discord.Guild) -> list[discord.Member]:
    """Members eligible for storm sign-up enumeration. Filters by the
    member_roster role gate if configured; otherwise returns all
    cacheable, non-bot members."""
    if guild is None:
        return []
    role_filter_id = 0
    try:
        from config import get_member_roster_config
        roster_cfg = get_member_roster_config(guild.id)
        role_filter_id = int(roster_cfg.get("role_filter_id", 0) or 0)
    except Exception:
        role_filter_id = 0

    members = []
    for m in guild.members:
        if m.bot:
            continue
        if role_filter_id and not any(r.id == role_filter_id for r in m.roles):
            continue
        members.append(m)
    return sorted(members, key=lambda m: m.display_name.lower())


def _build_bucket_map(
    guild: discord.Guild,
    event_type: str,
    event_date: str,
) -> tuple[dict[str, list[dict]], list[str]]:
    """Group every relevant member into a vote bucket.

    Returns: ({bucket_key: [ {label, target_id, is_on_behalf, not_on_discord} ... ]},
              roster_errors)
    """
    import config
    rows = config.get_storm_signups(guild.id, event_type, event_date) if guild else []
    by_target: dict[str, dict] = {r["target_member_id"]: r for r in rows}
    # Lenient lookup: on-behalf votes stored before the picker's
    # Discord-ID resolution landed (or for guilds where the picker
    # fell back to name because of a roster mis-classification) are
    # keyed by display name. Build a case-insensitive name index so
    # the Discord-members loop below can re-attribute those rows to
    # their live member rather than leaking them into the
    # "phantom non-Discord" leftover bucket.
    by_target_name_ci: dict[str, dict] = {
        r["target_member_id"].lower(): r
        for r in rows
        if r["target_member_id"] and not r["target_member_id"].isdigit()
    }

    buckets: dict[str, list[dict]] = {k: [] for k in _BUCKET_ORDER}

    seen_targets: set[str] = set()

    # Discord members → bucket from row or "not_voted".
    for m in _discord_member_pool(guild):
        target_id = str(m.id)
        seen_targets.add(target_id)
        row = by_target.get(target_id)
        # Stale-vote fallback: if no row keyed by Discord ID, try the
        # case-insensitive display-name index. Catches on-behalf votes
        # for this Discord member stored when the picker couldn't
        # resolve to an ID. Mark the matched row as "consumed" so the
        # leftover-rows loop below doesn't re-add it as a phantom.
        if row is None:
            matched_by_name = by_target_name_ci.get(m.display_name.lower())
            if matched_by_name is not None:
                row = matched_by_name
                seen_targets.add(matched_by_name["target_member_id"])
        bucket = row["vote"] if row else "not_voted"
        if bucket not in buckets:
            bucket = "not_voted"
        buckets[bucket].append({
            "label":          m.display_name,
            "target_id":      target_id,
            "is_on_behalf":   bool(row["is_on_behalf"]) if row else False,
            "not_on_discord": False,
        })

    # Non-Discord roster rows — read the alliance's roster Sheet and
    # surface every row flagged `not_on_discord` so leadership can see
    # who still needs an on-behalf vote BEFORE casting it.
    roster_rows, roster_errors = (
        _read_roster_rows(guild.id, guild=guild) if guild else ([], [])
    )
    for r in roster_rows:
        if not r.get("not_on_discord"):
            continue
        # Roster-by-name key matches the on-behalf target_member_id, which
        # is stored as the roster name verbatim. (Roster Discord IDs for
        # non-Discord members are usually empty.)
        target_id = r["name"]
        if target_id in seen_targets:
            continue
        seen_targets.add(target_id)
        row = by_target.get(target_id)
        bucket = row["vote"] if row else "not_voted"
        if bucket not in buckets:
            bucket = "not_voted"
        buckets[bucket].append({
            "label":          r["name"],
            "target_id":      target_id,
            "is_on_behalf":   bool(row["is_on_behalf"]) if row else False,
            "not_on_discord": True,
        })

    # On-behalf votes whose target wasn't matched above — phantom rows
    # from a pre-fix typo or a member removed from the roster. Surface
    # them under their current vote so officers can clean them up.
    for target_id, row in by_target.items():
        if target_id in seen_targets:
            continue
        bucket = row["vote"] if row["vote"] in buckets else "cannot"
        buckets[bucket].append({
            "label":          target_id,
            "target_id":      target_id,
            "is_on_behalf":   bool(row["is_on_behalf"]),
            "not_on_discord": True,
        })

    # Sort each bucket alphabetically.
    for k in buckets:
        buckets[k].sort(key=lambda e: e["label"].lower())
    return buckets, roster_errors


_BUCKET_EMOJIS = {
    "a":         "🅰️",
    "b":         "🅱️",
    "either":    "🔄",
    "cannot":    "❌",
    "not_voted": "❓",
}

# Discord embed description hard cap. We stop at a safe margin so the
# header/title and section dividers can't push us over.
_DESCRIPTION_BUDGET = 3800
# Per-bucket soft cap so a single bucket doesn't gobble the whole budget.
_BUCKET_BUDGET = 900


def _format_bucket_names(entries: list[dict]) -> str:
    """Comma-separated member labels, truncated at `_BUCKET_BUDGET` chars
    with an accurate `+N more` overflow hint."""
    if not entries:
        return ""
    formatted: list[str] = []
    for e in entries:
        name = e["label"]
        if e.get("not_on_discord"):
            name = f"{name} ¹"
        if e.get("is_on_behalf"):
            name = f"{name} _(on behalf)_"
        formatted.append(name)

    joined = ", ".join(formatted)
    if len(joined) <= _BUCKET_BUDGET:
        return joined

    # Find the longest prefix that fits, breaking at a comma so we don't
    # mid-truncate a name. Then count exactly how many entries fit.
    truncated = joined[:_BUCKET_BUDGET]
    last_comma = truncated.rfind(",")
    if last_comma > 0:
        truncated = truncated[:last_comma]
    shown_count = truncated.count(", ") + 1
    remaining = len(entries) - shown_count
    if remaining <= 0:
        return joined
    return f"{truncated}, … (+{remaining} more)"


def _render_embed(
    guild: discord.Guild,
    event_type: str,
    event_date: str,
    buckets: dict[str, list[dict]],
    bucket_filter: str | None = None,
) -> discord.Embed:
    from storm_date_helpers import format_event_date

    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    emoji = "🔥" if event_type == "DS" else "🏜️"
    date_pretty = format_event_date(event_date)

    total = sum(len(v) for v in buckets.values())
    title = f"{emoji} {label} Sign-Ups: {date_pretty}  ({total} members)"

    # If any bucket holds a not-on-Discord entry, render a footnote so the
    # ¹ marker we add to each such entry isn't unexplained.
    has_off_discord = any(
        e.get("not_on_discord") for entries in buckets.values() for e in entries
    )

    desc_lines: list[str] = []
    truncated_buckets = False
    used = 0
    for bucket_key in _BUCKET_ORDER:
        if bucket_filter and bucket_filter != bucket_key:
            continue
        entries = buckets[bucket_key]

        # Add an off-Discord summary to the "not_voted" header.
        bucket_title = _BUCKET_LABELS[bucket_key]
        if bucket_key == "not_voted":
            off = sum(1 for e in entries if e.get("not_on_discord"))
            if off:
                bucket_title = f"{bucket_title} [{off} not on Discord]"

        if not entries and bucket_filter is None:
            block = f"\n**{bucket_title}** (0)\n_(none)_"
            if used + len(block) > _DESCRIPTION_BUDGET:
                truncated_buckets = True
                break
            desc_lines.append(block)
            used += len(block)
            continue
        if not entries:
            continue

        names_blob = _format_bucket_names(entries)
        block = f"\n**{bucket_title}** ({len(entries)})\n{names_blob}"
        if used + len(block) > _DESCRIPTION_BUDGET:
            truncated_buckets = True
            break
        desc_lines.append(block)
        used += len(block)

    if has_off_discord:
        footnote = "\n¹ Not on Discord. Cast their vote with **🙋 Record on-behalf vote**."
        if used + len(footnote) <= _DESCRIPTION_BUDGET:
            desc_lines.append(footnote)

    description = "\n".join(desc_lines) if desc_lines else "_No data yet._"
    if truncated_buckets:
        description += "\n\n_Some buckets clipped. Use the filter dropdown to drill in._"

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.gold() if event_type == "DS" else discord.Color.orange(),
    )
    counts_line = " · ".join(
        f"{_BUCKET_EMOJIS[k]} {len(buckets[k])}" for k in _BUCKET_ORDER
    )
    embed.set_footer(text=counts_line)
    return embed


# ── View + on-behalf flow ────────────────────────────────────────────────────


_VOTE_CHOICES = [
    ("Team A",        "a"),
    ("Team B",        "b"),
    ("Either time",   "either"),
    ("Cannot participate", "cannot"),
]


_ON_BEHALF_PAGE_SIZE = 25


def _vote_select_options(
    event_type: str, guild_id: int, teams_setting: str,
) -> list[discord.SelectOption]:
    """Build the on-behalf Vote-select options to match the sign-up buttons.

    Reads the event's slot labels via `get_storm_slot_labels` so the wording
    surfaces *the same* `<local> (HH:MM server time)` strings members see on
    the sign-up post. The set of options branches on `teams_setting` to match
    the sign-up Variants A/B/C — single-team alliances only see their team
    + Cannot; both-teams alliances see all four choices.
    """
    from config import get_storm_slot_labels
    try:
        slot_labels = get_storm_slot_labels(event_type, guild_id)
    except Exception:
        slot_labels = ["", ""]
    slot_a = slot_labels[0] if len(slot_labels) > 0 else ""
    slot_b = slot_labels[1] if len(slot_labels) > 1 else ""

    label_a = f"Team A: {slot_a}" if slot_a else "Team A"
    label_b = f"Team B: {slot_b}" if slot_b else "Team B"

    teams = (teams_setting or "both").strip()
    if teams not in ("both", "A", "B"):
        teams = "both"

    options: list[discord.SelectOption] = []
    if teams in ("both", "A"):
        options.append(discord.SelectOption(label=label_a[:100], value="a"))
    if teams in ("both", "B"):
        options.append(discord.SelectOption(label=label_b[:100], value="b"))
    if teams == "both":
        options.append(discord.SelectOption(label="Either time works", value="either"))
    options.append(discord.SelectOption(label="Cannot participate", value="cannot"))
    return options


class _OnBehalfVoteView(discord.ui.View):
    """Ephemeral on-behalf vote picker (#168).

    Replaces the old `_OnBehalfModal` free-text flow with structured
    selects: Member Select sourced from the roster Sheet + Vote Select
    that mirrors the sign-up post's button labels. Both selects must be
    populated before Submit fires — there's no free-text path, so typo
    members and unparseable votes are unreachable.

    Roster lists longer than 25 paginate via Prev/Next buttons + a
    `Page X / Y` label-only indicator. The Member Select's options are
    rebuilt on every page change while the picked-vote value sticks.
    """

    def __init__(
        self,
        parent_view: "OfficerView",
        members: list[dict],
        teams_setting: str,
    ):
        super().__init__(timeout=300)
        self.parent_view = parent_view
        self.teams_setting = teams_setting
        # Sort + de-dupe roster (case-insensitive on name). Each entry
        # carries both the display `name` and the row's `discord_id`
        # (when present) so submit can choose the right
        # `target_member_id`: Discord ID for actual Discord members
        # (matches the self-vote key shape from SignupView), or the
        # name verbatim for non-Discord roster rows.
        #
        # We ALSO cross-reference each picked name against the live
        # guild membership. The roster Sheet's `not_on_discord`
        # inference can mis-flag a Discord member on a cold cache or
        # when the presence column header drifts; without this fallback
        # an on-behalf vote for a real Discord member would end up
        # keyed by name and never join back to the bucket-builder's
        # Discord-ID-keyed lookup. The Discord membership is the most
        # authoritative signal: if the picked name matches a live
        # member's display name, use their Discord ID regardless of
        # what the roster row says.
        #
        # Display-name collisions: alliances sometimes have two Discord
        # members with the same server nickname (e.g. two "Phoenix"
        # alts). A single-key lookup would let one ID silently overwrite
        # the other and the officer would cast a vote for the wrong
        # member with no signal. Multi-value lookup so colliding names
        # surface ALL matching Discord IDs; the picker then disambiguates
        # by appending `(@username)` for each colliding entry.
        guild = parent_view.guild if parent_view is not None else None
        discord_members_by_name: dict[str, list[tuple[str, str]]] = {}
        if guild is not None:
            for gm in _discord_member_pool(guild):
                key = gm.display_name.lower()
                discord_members_by_name.setdefault(key, []).append(
                    (str(gm.id), gm.name)
                )
        seen: set[str] = set()
        cleaned: list[dict] = []
        for m in members:
            name = (m.get("name") or "").strip()
            if not name:
                continue
            # Discord stores numeric-name on-behalf targets in the same
            # column as Discord user IDs; surface that here so the user
            # never picks a name that would collide.
            if name.isdigit():
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            discord_id = (m.get("discord_id") or "").strip()
            not_on_discord = bool(m.get("not_on_discord"))
            live_matches = discord_members_by_name.get(key, [])
            # Resolution priority for `target_member_id`:
            #   1. Roster row's discord_id when filled in and not flagged
            #      not_on_discord. The roster is explicit about which
            #      user this row represents, so no Discord-side
            #      disambiguation is needed even if multiple Discord
            #      members share the display name.
            #   2. Single live Discord member with matching display_name
            #      — use their ID directly.
            #   3. Multiple live Discord members share the display name
            #      AND the roster row didn't pre-commit to an ID — emit
            #      ONE picker entry per colliding member, suffixed
            #      `(@username)`, so the officer can tell them apart.
            #   4. The picked name verbatim (genuine non-Discord roster
            #      member, or no live match at all).
            if discord_id and not not_on_discord:
                cleaned.append({"name": name, "target_id": discord_id})
                continue
            if not_on_discord or not live_matches:
                cleaned.append({"name": name, "target_id": name})
                continue
            if len(live_matches) == 1:
                cleaned.append({"name": name, "target_id": live_matches[0][0]})
                continue
            # Collision: expand into one disambiguated entry per
            # Discord match. Sort by username for a stable order across
            # picker rebuilds.
            for member_id, username in sorted(live_matches, key=lambda p: p[1].lower()):
                cleaned.append({
                    "name": f"{name} (@{username})",
                    "target_id": member_id,
                })
        cleaned.sort(key=lambda r: r["name"].lower())
        self.members = cleaned
        # Cache a name→target_id map so the submit handler can resolve
        # without re-walking the list. Keys are lowercased to match the
        # case-insensitive picker dedup.
        self._target_by_name = {
            r["name"].lower(): r["target_id"] for r in cleaned
        }
        self.page = 0
        self.selected_member: str | None = None
        self.selected_vote: str | None = None
        self.message: discord.Message | None = None
        self._build_components()

    @property
    def page_count(self) -> int:
        if not self.members:
            return 1
        return (len(self.members) + _ON_BEHALF_PAGE_SIZE - 1) // _ON_BEHALF_PAGE_SIZE

    def _members_for_page(self) -> list[dict]:
        start = self.page * _ON_BEHALF_PAGE_SIZE
        return self.members[start:start + _ON_BEHALF_PAGE_SIZE]

    def _build_components(self):
        self.clear_items()

        page_members = self._members_for_page()
        if page_members:
            member_options = [
                discord.SelectOption(
                    label=m["name"][:100],
                    value=m["name"][:100],
                    default=(m["name"] == self.selected_member),
                )
                for m in page_members
            ]
            member_select = discord.ui.Select(
                placeholder=(
                    f"Picked: {self.selected_member}"
                    if self.selected_member else "Pick a member…"
                ),
                min_values=1, max_values=1,
                options=member_options,
                row=0,
            )

            async def _on_member(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                self.selected_member = member_select.values[0]
                self._build_components()
                try:
                    await inter.response.edit_message(view=self)
                except discord.HTTPException:
                    pass

            member_select.callback = _on_member
            self.add_item(member_select)

        vote_options = _vote_select_options(
            self.parent_view.event_type,
            self.parent_view.guild_id,
            self.teams_setting,
        )
        for opt in vote_options:
            opt.default = (opt.value == self.selected_vote)
        vote_select = discord.ui.Select(
            placeholder=(
                f"Picked: {dict((o.value, o.label) for o in vote_options).get(self.selected_vote, '')}"
                if self.selected_vote else "Pick a vote…"
            ),
            min_values=1, max_values=1,
            options=vote_options,
            row=1,
        )

        async def _on_vote(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            self.selected_vote = vote_select.values[0]
            self._build_components()
            try:
                await inter.response.edit_message(view=self)
            except discord.HTTPException:
                pass

        vote_select.callback = _on_vote
        self.add_item(vote_select)

        # Paging row — only rendered when the roster is bigger than the
        # 25-option Select cap.
        if self.page_count > 1:
            prev_btn = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary,
                disabled=(self.page == 0), row=2,
            )

            async def _on_prev(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                if self.page > 0:
                    self.page -= 1
                    self._build_components()
                    try:
                        await inter.response.edit_message(view=self)
                    except discord.HTTPException:
                        pass
                else:
                    await inter.response.defer()

            prev_btn.callback = _on_prev
            self.add_item(prev_btn)

            page_label = discord.ui.Button(
                label=f"Page {self.page + 1} / {self.page_count}",
                style=discord.ButtonStyle.secondary,
                disabled=True, row=2,
            )
            self.add_item(page_label)

            next_btn = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.secondary,
                disabled=(self.page >= self.page_count - 1), row=2,
            )

            async def _on_next(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                if self.page < self.page_count - 1:
                    self.page += 1
                    self._build_components()
                    try:
                        await inter.response.edit_message(view=self)
                    except discord.HTTPException:
                        pass
                else:
                    await inter.response.defer()

            next_btn.callback = _on_next
            self.add_item(next_btn)

        submit_btn = discord.ui.Button(
            label="✅ Submit", style=discord.ButtonStyle.primary,
            disabled=not (self.selected_member and self.selected_vote),
            row=3,
        )
        submit_btn.callback = self._on_submit
        self.add_item(submit_btn)

        cancel_btn = discord.ui.Button(
            label="↩️ Cancel", style=discord.ButtonStyle.secondary, row=3,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.parent_view.owner_user_id:
            await inter.response.send_message(
                "⛔ Only the officer who opened this view can record on-behalf votes here.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_submit(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        if not (self.selected_member and self.selected_vote):
            await inter.response.send_message(
                "⚠️ Pick a member and a vote before submitting.",
                ephemeral=True,
            )
            return
        try:
            await inter.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        # Resolve picked display name → bucket-builder target_id. For
        # Discord-member targets this is the Discord ID (matches the
        # self-vote shape from SignupView); for non-Discord roster
        # rows this is the name verbatim. Without this resolution,
        # on-behalf votes for Discord members landed in a phantom
        # name-keyed bucket and the original Discord-ID-keyed
        # "Not voted yet" entry never moved.
        target_member_id = (
            self._target_by_name.get(self.selected_member.lower())
            or self.selected_member
        )

        import config
        ok = config.record_storm_vote(
            self.parent_view.guild_id,
            self.parent_view.event_type,
            self.parent_view.event_date,
            voter_user_id=inter.user.id,
            target_member_id=target_member_id,
            vote=self.selected_vote,
            is_on_behalf=True,
        )
        if not ok:
            await inter.followup.send(
                "⚠️ Couldn't record that vote. Check the bot logs.",
                ephemeral=True,
            )
            return

        # Refresh the parent view in place so the new vote shows up.
        await self.parent_view.refresh_buckets()
        try:
            if self.parent_view.message is not None:
                await self.parent_view.message.edit(
                    embed=_render_embed(
                        self.parent_view.guild, self.parent_view.event_type,
                        self.parent_view.event_date,
                        self.parent_view.buckets, self.parent_view.bucket_filter,
                    ),
                    view=self.parent_view,
                )
        except discord.HTTPException:
            pass

        for item in self.children:
            item.disabled = True
        self.stop()
        try:
            if self.message is not None:
                await self.message.edit(
                    content=f"✅ Recorded on-behalf vote for **{self.selected_member}**.",
                    view=self,
                )
        except discord.HTTPException:
            pass
        try:
            await inter.followup.send(
                f"✅ Recorded on-behalf vote for **{self.selected_member}**.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass

    async def _on_cancel(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        for item in self.children:
            item.disabled = True
        self.stop()
        try:
            await inter.response.edit_message(
                content="↩️ Cancelled. No vote recorded.", view=self,
            )
        except discord.HTTPException:
            pass

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class OfficerView(discord.ui.View):
    """Officer view for one event. Owns the bucket map + filter state."""

    def __init__(self, guild: discord.Guild, owner_user_id: int,
                 event_type: str, event_date: str):
        super().__init__(timeout=900)
        self.guild = guild
        self.guild_id = guild.id
        self.owner_user_id = owner_user_id
        self.event_type = event_type
        self.event_date = event_date
        self.bucket_filter: str | None = None
        self.buckets: dict[str, list[dict]] = {}
        self.roster_errors: list[str] = []
        # `message` is captured at send-time so `on_timeout` can edit
        # the rendered post. The view is intentionally public (the
        # bucket map serves as a leadership audit trail across multiple
        # officers); without an on_timeout, the buttons silently 404
        # after 15 minutes with "Interaction failed" and no signal.
        self.message: Optional[discord.Message] = None
        # NOTE: buckets are populated by the caller via
        # `await view.refresh_buckets()` AFTER construction. Earlier
        # code called `_build_bucket_map` synchronously inside __init__,
        # which read the alliance roster Sheet on the event loop —
        # under Sheets rate-limit pressure that stalled every other
        # guild's button clicks + scheduler ticks. The slash command
        # and the refresh-button callback are async, so threading the
        # sheet read out is cheap; the buckets attribute starts empty
        # and only the embed-render path reads it (post-refresh).
        self._build_components()

    async def on_timeout(self) -> None:
        """Strip the view + append the canonical timeout notice so
        officers know the buttons are dead. Matches the auto-post-view
        cleanup contract in CLAUDE.md."""
        from wizard_registry import expire_view_message
        hint = f"{HUB_COMMAND[self.event_type]} → **{HUB_BTN_VIEW_SIGNUPS}**"
        await expire_view_message(self.message, command_hint=hint)

    async def refresh_buckets(self) -> None:
        """Re-read the alliance roster Sheet + storm_signups SQLite
        and rebuild the bucket map. Off the event loop so a slow
        gspread call doesn't stall the bot. Callers MUST await."""
        self.buckets, self.roster_errors = await asyncio.to_thread(
            _build_bucket_map, self.guild, self.event_type, self.event_date,
        )

    def _build_components(self):
        self.clear_items()

        # Filter dropdown
        filter_select = discord.ui.Select(
            placeholder=("Filter bucket — currently: "
                         f"{self.bucket_filter or 'All'}"),
            min_values=1, max_values=1,
            options=[
                discord.SelectOption(label="All buckets",  value="_all"),
                discord.SelectOption(label=_BUCKET_LABELS["a"],         value="a"),
                discord.SelectOption(label=_BUCKET_LABELS["b"],         value="b"),
                discord.SelectOption(label=_BUCKET_LABELS["either"],    value="either"),
                discord.SelectOption(label=_BUCKET_LABELS["cannot"],    value="cannot"),
                discord.SelectOption(label=_BUCKET_LABELS["not_voted"], value="not_voted"),
            ],
        )

        async def _on_filter(inter: discord.Interaction):
            if inter.user.id != self.owner_user_id:
                await inter.response.send_message(
                    "⛔ Only the officer who opened this view can change the filter.",
                    ephemeral=True,
                )
                return
            choice = filter_select.values[0]
            self.bucket_filter = None if choice == "_all" else choice
            self._build_components()
            await inter.response.edit_message(
                embed=_render_embed(self.guild, self.event_type, self.event_date,
                                    self.buckets, self.bucket_filter),
                view=self,
            )

        filter_select.callback = _on_filter
        self.add_item(filter_select)

        # Vote on behalf
        on_behalf_btn = discord.ui.Button(
            label="🙋 Record on-behalf vote",
            style=discord.ButtonStyle.primary,
        )

        async def _on_behalf(inter: discord.Interaction):
            if inter.user.id != self.owner_user_id:
                await inter.response.send_message(
                    "⛔ Only the officer who opened this view can record on-behalf votes here.",
                    ephemeral=True,
                )
                return
            # Defer first — `_read_roster_rows` is a gspread round-trip
            # that can take seconds under rate-limit pressure, and the
            # 3-second initial-response token would otherwise expire.
            try:
                await inter.response.defer(ephemeral=True)
            except discord.HTTPException:
                pass
            roster_rows, _errs = await asyncio.to_thread(
                _read_roster_rows, self.guild_id, guild=self.guild,
            )
            if not roster_rows:
                # Permissive fallback path. Without a roster read, we can't
                # populate the Member Select — surface the same actionable
                # error so the officer knows to retry after /sync_members.
                try:
                    await inter.followup.send(
                        "⚠️ Couldn't read the roster right now. Try "
                        "`/sync_members` and reopen this view to retry.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass
                return
            import config
            cfg = config.get_storm_config(self.guild_id, self.event_type) or {}
            teams_setting = (cfg.get("teams") or "both").strip()
            picker = _OnBehalfVoteView(self, roster_rows, teams_setting)
            try:
                msg = await inter.followup.send(
                    content=(
                        "🙋 Pick a member and a vote, then **Submit**. "
                        "Only roster members are listed. `/sync_members` "
                        "refreshes the list."
                    ),
                    view=picker, ephemeral=True,
                )
                picker.message = msg
            except discord.HTTPException:
                pass
        on_behalf_btn.callback = _on_behalf
        self.add_item(on_behalf_btn)

        # Refresh
        refresh_btn = discord.ui.Button(label="🔄 Refresh", style=discord.ButtonStyle.secondary)

        async def _refresh(inter: discord.Interaction):
            if inter.user.id != self.owner_user_id:
                await inter.response.send_message(
                    "⛔ Only the officer who opened this view can refresh.",
                    ephemeral=True,
                )
                return
            # Defer first — `refresh_buckets` does a gspread read off
            # the event loop, which can exceed Discord's 3-second
            # initial-response window under Sheets rate-limit pressure.
            try:
                await inter.response.defer()
            except discord.HTTPException:
                pass
            await self.refresh_buckets()
            try:
                await inter.edit_original_response(
                    embed=_render_embed(self.guild, self.event_type, self.event_date,
                                        self.buckets, self.bucket_filter),
                    view=self,
                )
            except discord.HTTPException:
                pass
        refresh_btn.callback = _refresh
        self.add_item(refresh_btn)

        # Team setup buttons (#129 + Rule A / #166) — opens the
        # structured roster builder filtered to signed-up members for
        # this team. Gated by `teams` config — applies identically to
        # DS and CS. teams=both shows A+B; teams=A or teams=B shows
        # just that team's button.
        from config import get_storm_config
        cfg = get_storm_config(self.guild_id, self.event_type) or {}
        teams_setting = (cfg.get("teams") or "both").strip()
        if teams_setting not in ("both", "A", "B"):
            teams_setting = "both"

        show_a = teams_setting in ("both", "A")
        show_b = teams_setting in ("both", "B")

        if show_a:
            a_btn = discord.ui.Button(
                label="🅰️ Set up Team A", style=discord.ButtonStyle.success, row=2,
            )

            async def _setup_a(inter: discord.Interaction):
                await _open_team_setup(inter, self, team="A")

            a_btn.callback = _setup_a
            self.add_item(a_btn)

        if show_b:
            b_btn = discord.ui.Button(
                label="🅱️ Set up Team B", style=discord.ButtonStyle.success, row=2,
            )

            async def _setup_b(inter: discord.Interaction):
                await _open_team_setup(inter, self, team="B")

            b_btn.callback = _setup_b
            self.add_item(b_btn)


async def _open_team_setup(
    inter: discord.Interaction, officer_view: "OfficerView", *, team: str,
) -> None:
    """Pick a preset, then hand off to the structured roster builder.
    Called from the Set-up-Team buttons on the officer view."""
    if inter.user.id != officer_view.owner_user_id:
        await inter.response.send_message(
            "⛔ Only the officer who opened this view can start team setup.",
            ephemeral=True,
        )
        return

    import storm_strategy as ss
    preset_names = ss.list_presets(officer_view.guild_id, officer_view.event_type)
    if not preset_names:
        hub_cmd = HUB_COMMAND[officer_view.event_type]
        await inter.response.send_message(
            f"⚠️ No strategy presets defined yet for "
            f"{'Desert Storm' if officer_view.event_type == 'DS' else 'Canyon Storm'}. "
            f"Run `{hub_cmd}` and click **{HUB_BTN_PRESETS}** first.",
            ephemeral=True,
        )
        return

    picker = _PresetPickerView(
        owner_id=inter.user.id, preset_names=preset_names,
    )
    team_label = (
        "Team A" if team == "A" else "Team B" if team == "B" else "this roster"
    )
    await inter.response.send_message(
        f"Pick a strategy preset to apply for **{team_label}**:",
        view=picker, ephemeral=True,
    )
    try:
        picker.message = await inter.original_response()
    except discord.HTTPException:
        picker.message = None
    await picker.wait()
    if not picker.selected_preset:
        return  # user dismissed or timed out

    # Hand off to the roster builder. It manages its own defer/followup
    # cycle; we're already responded to so the next message comes via
    # interaction.followup. (The roster builder calls
    # `interaction.followup.send` on a deferred interaction by default.)
    from storm_roster_builder import open_roster_builder
    await open_roster_builder(
        inter, officer_view.event_type, picker.selected_preset,
        event_date=officer_view.event_date,
        team_override=team or None,
    )


class _PresetPickerView(discord.ui.View):
    """Single-select dropdown for picking a saved preset."""

    def __init__(self, *, owner_id: int, preset_names: list[str]):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self.selected_preset: Optional[str] = None
        self.message: Optional[discord.Message] = None
        options = [
            discord.SelectOption(label=n[:100], value=n[:100])
            for n in preset_names[:25]
        ]
        select = discord.ui.Select(
            placeholder="Pick a preset…",
            min_values=1, max_values=1,
            options=options,
        )

        async def _on_pick(inter: discord.Interaction):
            if inter.user.id != self.owner_id:
                await inter.response.send_message(
                    "⛔ Only the user who started team setup can pick.",
                    ephemeral=True,
                )
                return
            self.selected_preset = select.values[0]
            for item in self.children: item.disabled = True
            await inter.response.edit_message(
                content=f"✅ Preset **{self.selected_preset}** selected. "
                        f"opening the roster builder…",
                view=self,
            )
            self.stop()

        select.callback = _on_pick
        self.add_item(select)

    async def on_timeout(self) -> None:
        """Strip the picker on timeout so a click on a stale option
        doesn't surface 'Interaction failed'."""
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ── Slash command handler ────────────────────────────────────────────────────
#
# Wired from the `👁️ View sign-ups + set up teams` button on the
# `/desertstorm` and `/canyonstorm` event hubs (storm_event_hub.py).
# This module exposes the handler body so the hub stays a thin
# dispatcher.


async def handle_storm_signups(
    bot,
    interaction: discord.Interaction,
    event_type: str,
    event_date: Optional[str] = None,
) -> None:
    from storm_permissions import (
        is_leader_or_admin,
        deny_non_leader,
        ensure_premium_structured,
    )
    from storm_date_helpers import parse_event_date, next_event_date

    if not is_leader_or_admin(interaction):
        await deny_non_leader(interaction)
        return

    et = event_type
    raw_input = (event_date or "").strip()
    if not raw_input:
        date_clean = next_event_date(interaction.guild_id, et)
    else:
        parsed = parse_event_date(raw_input)
        if parsed is None:
            await interaction.response.send_message(
                f"⚠️ `{event_date}` isn't a date I can parse. Try `May 18`, "
                f"`5/18`, `2026-05-18`, `Sunday`, or `tomorrow`.",
                ephemeral=True,
            )
            return
        date_clean = parsed.isoformat()

    feature_label = (
        f"`/{'desertstorm' if et == 'DS' else 'canyonstorm'} signups`"
    )
    ok, _structured = await ensure_premium_structured(
        interaction, et,
        bot=bot,
        feature_label=feature_label,
    )
    if not ok:
        return

    # Defer before building buckets — roster Sheet read + member-cache
    # scan can blow past the 3-second initial-response token on a cold
    # cache or rate-limited Sheets API.
    await interaction.response.defer(thinking=True)

    # Ensure the guild member cache is populated so `guild.get_member`
    # in `_read_roster_rows` doesn't false-positive-infer members as
    # not-on-Discord during a cold cache (cold restart, this guild
    # not yet touched by an interaction, etc.). `_ensure_member_cache`
    # is a no-op when the cache is already chunked and silently
    # tolerates the SERVER MEMBERS INTENT being off (the warning
    # path in `_read_roster_rows` still surfaces).
    try:
        import member_roster
        await member_roster._ensure_member_cache(interaction.guild)
    except Exception as e:
        logger.warning(
            "[STORM OFFICER VIEW] guild.chunk() pre-pass failed for "
            "guild=%s: %s",
            interaction.guild_id, e,
        )

    view = OfficerView(interaction.guild, interaction.user.id, et, date_clean)
    # Populate buckets via `asyncio.to_thread` so the gspread read
    # doesn't block the event loop (the read used to fire inside
    # `__init__`, stalling every other guild's click handlers while
    # this guild's Sheet was being fetched).
    await view.refresh_buckets()
    followup_args = dict(
        embed=_render_embed(interaction.guild, et, date_clean, view.buckets),
        view=view,
    )
    if view.roster_errors:
        # Surface the actual error contents — alliances need to see
        # WHICH IDs are stale (or which read failed) so they can fix
        # the roster Sheet. The prior generic "See bot logs" message
        # hid the detail the audit explicitly asked to expose.
        preview = " · ".join(view.roster_errors[:2])
        followup_args["content"] = (
            "⚠️ Roster Sheet read had issues. Non-Discord member "
            f"enumeration may be incomplete: {preview}"
        )
        logger.warning(
            "[STORM OFFICER VIEW] roster errors for guild=%s: %s",
            interaction.guild_id, "; ".join(view.roster_errors),
        )
    view.message = await interaction.followup.send(**followup_args)

    # First-run tour offer moved to `storm_event_hub.handle_event_hub`
    # post-#190 (the hub is the front door for the storm flow now).
    # The officer view is reached via the hub's "View sign-ups + set
    # up teams" button; the tour fires upstream at hub entry instead.
