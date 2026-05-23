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

from config import (
    STORM_PLAN_MAX_PRIMARIES,
    STORM_PLAN_MAX_SUBS,
    STORM_PLAN_MAX_TOTAL,
)
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
    team_plans: dict[str, dict] | None = None,
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

    # 📋 Team plan summary (#239) — surface the saved in-game commitment
    # so officers can see at a glance whether the plan is current and
    # whether the auto-fill will be constrained. Renders one inline
    # field per saved team; absent teams aren't displayed (no plan = no
    # row). Callers can pass an already-fetched `team_plans` dict to
    # skip the SQL round-trip; otherwise we fetch on demand so legacy
    # callsites get the summary for free.
    if team_plans is None:
        try:
            from config import get_storm_team_plans_for_event
            team_plans = get_storm_team_plans_for_event(
                guild.id, event_type, event_date,
            )
        except Exception:
            team_plans = {}
    if team_plans:
        for plan_team in ("A", "B"):
            plan = team_plans.get(plan_team)
            if not plan:
                continue
            n_primary = len(plan.get("primaries") or [])
            n_sub = len(plan.get("subs") or [])
            embed.add_field(
                name=f"📋 Team {plan_team} plan",
                value=f"{n_primary} primary / {n_sub} sub",
                inline=True,
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


_ACK_NAME_PREVIEW = 5


def _vote_ack_label(vote: str) -> str:
    """Friendly label for an ack line. Doesn't need slot strings — the
    parent view embed already shows the slot timing, and the ack is
    explicitly an officer-side confirmation."""
    return {
        "a":      "Team A",
        "b":      "Team B",
        "either": "Either",
        "cannot": "Cannot",
    }.get(vote, vote)


def _format_on_behalf_ack(
    recorded: list[str], failed: list[str], vote: str,
) -> str:
    """Build the on-behalf submit ack. Single-pick keeps the original
    phrasing; multi-pick gets a count + first-N-names preview + overflow
    hint. Partial-failure path tells the officer how many fell out."""
    label = _vote_ack_label(vote)
    if not recorded and failed:
        return (
            f"⚠️ Couldn't record any of the {len(failed)} on-behalf votes. "
            "Check the bot logs."
        )
    if len(recorded) == 1 and not failed:
        return f"✅ Recorded on-behalf vote for **{recorded[0]}**."

    preview = ", ".join(recorded[:_ACK_NAME_PREVIEW])
    overflow = ""
    if len(recorded) > _ACK_NAME_PREVIEW:
        overflow = f", … +{len(recorded) - _ACK_NAME_PREVIEW} more"
    msg = (
        f"✅ Recorded **{len(recorded)} on-behalf vote(s)** ({label}): "
        f"{preview}{overflow}."
    )
    if failed:
        msg += (
            f"\n⚠️ {len(failed)} failed to record — check the bot logs."
        )
    return msg


class _OnBehalfVoteView(discord.ui.View):
    """Ephemeral on-behalf vote picker (#168, multi-select in #218).

    Replaces the old `_OnBehalfModal` free-text flow with structured
    selects: Member Select sourced from the roster Sheet + Vote Select
    that mirrors the sign-up post's button labels. Both selects must be
    populated before Submit fires — there's no free-text path, so typo
    members and unparseable votes are unreachable.

    Multi-select (#218): the Member Select accepts up to 25 picks per
    page; Submit casts the chosen vote against every picked member.
    Picks are persisted across pages so officers can paginate, tick more
    names, and submit once. By default the picker hides members who
    already have a vote on this event so officers can't accidentally
    clobber sign-up-post votes; the 👁️ button toggles them back in for
    correction flows. The 📥 button stages every not-yet-voted member
    in one click — Submit still required.

    Roster lists longer than 25 paginate via Prev/Next buttons + a
    `Page X / Y` label-only indicator. The Member Select's options are
    rebuilt on every page change while the picked-vote value sticks.
    """

    def __init__(
        self,
        parent_view: "OfficerView",
        members: list[dict],
        teams_setting: str,
        voted_target_ids: set[str] | None = None,
    ):
        # Bumped 300 → 600 because multi-select flows take longer than
        # the original single-pick flow (paginate, tick, paginate, tick).
        super().__init__(timeout=600)
        self.parent_view = parent_view
        self.teams_setting = teams_setting
        # Set of target_ids whose vote is already recorded on this event.
        # Drives the "hide already-voted" default + the 📥 shortcut's
        # not-voted scope. Empty set = "no prior votes" → toggle/shortcut
        # are still rendered but no-op.
        self.voted_target_ids: set[str] = set(voted_target_ids or ())
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
        # `_all_members` is the full roster post-dedup-sort. `members`
        # is a property that returns the filter-applied subset (hides
        # already-voted by default; full list when `show_voted` is True).
        # Page math reads `members`, so toggling rebuilds page_count.
        self._all_members: list[dict] = cleaned
        # Cache a name→target_id map so the submit handler can resolve
        # without re-walking the list. Keys are lowercased to match the
        # case-insensitive picker dedup.
        self._target_by_name = {
            r["name"].lower(): r["target_id"] for r in cleaned
        }
        self.page = 0
        # Multi-select state: picks accumulate across pages until Submit
        # fires. Insertion order is preserved for stable ack rendering.
        self.selected_members: list[str] = []
        self.selected_vote: str | None = None
        # Toggle: when False (default), the Member Select hides any
        # roster row whose target_id is in `voted_target_ids` so an
        # officer can't accidentally overwrite a vote already cast via
        # the sign-up post. Flip True via the 👁️ button when the
        # officer's intentionally correcting a prior vote.
        self.show_voted: bool = False
        self.message: discord.Message | None = None
        self._build_components()

    @property
    def members(self) -> list[dict]:
        """Filter-applied member list. `show_voted=False` (default) hides
        members already in a vote bucket; `True` returns the full roster.
        Read by `_members_for_page` and `page_count`."""
        if self.show_voted or not self.voted_target_ids:
            return self._all_members
        return [
            m for m in self._all_members
            if m["target_id"] not in self.voted_target_ids
        ]

    @property
    def not_voted_count(self) -> int:
        """Members in `_all_members` whose target_id isn't already voted.
        Drives the 📥 shortcut button label + disable state. Independent
        of `show_voted` — the shortcut always means "not-voted only"."""
        if not self.voted_target_ids:
            return len(self._all_members)
        return sum(
            1 for m in self._all_members
            if m["target_id"] not in self.voted_target_ids
        )

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

        # Clamp the page index after a filter toggle / shortcut shrinks
        # the list — otherwise paging beyond the new last page produces
        # an empty Select that breaks the disabled-Submit invariant.
        page_count = self.page_count
        if self.page >= page_count:
            self.page = max(0, page_count - 1)

        page_members = self._members_for_page()
        if page_members:
            # Picks for THIS page are the intersection of currently-shown
            # names with `selected_members`. The Select's interaction
            # only carries the new values for the visible page, so the
            # callback must remember which page-1 picks survived a page-2
            # update vs. which were unticked on this page.
            selected_set = set(self.selected_members)
            page_names = {m["name"] for m in page_members}
            picks_on_this_page = [
                m["name"] for m in page_members if m["name"] in selected_set
            ]
            member_options = [
                discord.SelectOption(
                    label=m["name"][:100],
                    value=m["name"][:100],
                    default=(m["name"] in selected_set),
                )
                for m in page_members
            ]
            placeholder = "Pick one or more members…"
            if self.selected_members:
                placeholder = (
                    f"Picked: {len(self.selected_members)} "
                    f"(this page: {len(picks_on_this_page)})"
                )
            # `max_values` is capped at the page size — Discord rejects
            # `max_values > len(options)`. The 25-cap is enforced by
            # `_ON_BEHALF_PAGE_SIZE` so we never exceed Discord's limit.
            member_select = discord.ui.Select(
                placeholder=placeholder,
                min_values=0,
                max_values=len(member_options),
                options=member_options,
                row=0,
            )

            async def _on_member(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                # Replace the picks for the current page only. Anything
                # picked on other pages survives. Without this, paging
                # to page 2 + ticking new names would silently erase
                # everything chosen on page 1.
                kept = [
                    name for name in self.selected_members
                    if name not in page_names
                ]
                self.selected_members = kept + list(member_select.values)
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

        submit_label = "✅ Submit"
        if self.selected_members and self.selected_vote:
            submit_label = f"✅ Submit ({len(self.selected_members)})"
        submit_btn = discord.ui.Button(
            label=submit_label, style=discord.ButtonStyle.primary,
            disabled=not (self.selected_members and self.selected_vote),
            row=3,
        )
        submit_btn.callback = self._on_submit
        self.add_item(submit_btn)

        cancel_btn = discord.ui.Button(
            label="↩️ Cancel", style=discord.ButtonStyle.secondary, row=3,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

        # 📥 Stage every not-yet-voted member in one click. Officer still
        # has to hit Submit — no auto-submit, because a misclick at 100
        # members is brutal. Disabled when nothing to stage.
        not_voted_n = self.not_voted_count
        select_all_btn = discord.ui.Button(
            label=f"📥 Select all not-voted ({not_voted_n})",
            style=discord.ButtonStyle.secondary,
            disabled=(not_voted_n == 0),
            row=4,
        )

        async def _on_select_all(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            not_voted_names = [
                m["name"] for m in self._all_members
                if m["target_id"] not in self.voted_target_ids
            ]
            # Replace, don't append — repeat clicks should stay
            # idempotent rather than dupe the list.
            self.selected_members = not_voted_names
            self._build_components()
            try:
                await inter.response.edit_message(view=self)
            except discord.HTTPException:
                pass

        select_all_btn.callback = _on_select_all
        self.add_item(select_all_btn)

        # 👁️ Toggle whether already-voted members appear in the picker.
        # Hidden by default to keep officers out of accidental-overwrite
        # range; flip on for correction flows.
        if self.voted_target_ids:
            toggle_label = (
                "🙈 Hide already-voted"
                if self.show_voted
                else f"👁️ Show already-voted ({len(self.voted_target_ids)})"
            )
            toggle_btn = discord.ui.Button(
                label=toggle_label,
                style=discord.ButtonStyle.secondary,
                row=4,
            )

            async def _on_toggle(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                self.show_voted = not self.show_voted
                # Reset to first page so a toggle never strands the
                # officer on a now-empty page.
                self.page = 0
                self._build_components()
                try:
                    await inter.response.edit_message(view=self)
                except discord.HTTPException:
                    pass

            toggle_btn.callback = _on_toggle
            self.add_item(toggle_btn)

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
        if not (self.selected_members and self.selected_vote):
            await inter.response.send_message(
                "⚠️ Pick at least one member and a vote before submitting.",
                ephemeral=True,
            )
            return
        try:
            await inter.response.defer(ephemeral=True)
        except discord.HTTPException:
            pass

        # Resolve picked display names → bucket-builder target_ids in one
        # pass before writing, so a single resolution miss doesn't strand
        # half the batch. For Discord-member targets the resolved id is
        # the Discord ID (matches the self-vote shape from SignupView);
        # for non-Discord roster rows it's the name verbatim. Without
        # this resolution, on-behalf votes for Discord members landed in
        # a phantom name-keyed bucket and the original Discord-ID-keyed
        # "Not voted yet" entry never moved.
        import config
        recorded: list[str] = []
        failed: list[str] = []
        for name in self.selected_members:
            target_member_id = (
                self._target_by_name.get(name.lower()) or name
            )
            ok = config.record_storm_vote(
                self.parent_view.guild_id,
                self.parent_view.event_type,
                self.parent_view.event_date,
                voter_user_id=inter.user.id,
                target_member_id=target_member_id,
                vote=self.selected_vote,
                is_on_behalf=True,
            )
            if ok:
                recorded.append(name)
            else:
                failed.append(name)

        # Refresh the parent view in place ONCE after the batch — a
        # per-member refresh would do N sheet reads on a 100-member
        # apply-all submit and the embed would flicker through every
        # intermediate state.
        if recorded:
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

        ack = _format_on_behalf_ack(recorded, failed, self.selected_vote)

        for item in self.children:
            item.disabled = True
        self.stop()
        try:
            if self.message is not None:
                await self.message.edit(content=ack, view=self)
        except discord.HTTPException:
            pass
        try:
            await inter.followup.send(ack, ephemeral=True)
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


class _TeamPlanRosterPickerView(discord.ui.View):
    """Step 1 of the team-plan picker (#239) — pick up to 30 players
    for one team for one event.

    Candidate pool = members who voted Team A (or Either) for Team A's
    plan; Team B (or Either) for Team B's plan. Members already on the
    OTHER team's saved plan for the same event are filtered out and
    surfaced as a one-line "N hidden — already on Team X" note, so the
    one-team-per-member rule (matching the in-game "can't move once
    submitted" constraint) is enforced at pick time rather than only at
    save time.

    Re-entry pre-seeds picks from the saved plan (primaries ∪ subs) so
    the officer is editing the existing 30, not starting over.
    """

    def __init__(
        self,
        parent_view: "OfficerView",
        team: str,
        candidates: list[dict],
        other_team_claimed: list[str],
        prior_picks: list[str],
        prior_subs: list[str],
        prior_saved_at: str,
    ):
        super().__init__(timeout=600)
        self.parent_view = parent_view
        self.team = team
        self.candidates = candidates  # [{"name": str, "target_id": str}]
        self.other_team_claimed = list(other_team_claimed)
        self.selected_target_ids: list[str] = list(prior_picks)
        self.prior_subs: list[str] = list(prior_subs)
        self.has_prior_plan: bool = bool(prior_picks or prior_subs)
        self.prior_saved_at = prior_saved_at
        self.advance_to_step2: bool = False
        self.cleared: bool = False
        self.page = 0
        self.message: discord.Message | None = None
        # Build a quick name lookup so step 2 can render display names
        # for the picked target_ids without re-querying the bucket map.
        self.name_by_target_id: dict[str, str] = {
            c["target_id"]: c["name"] for c in candidates
        }
        self._build_components()

    @property
    def page_count(self) -> int:
        if not self.candidates:
            return 1
        return (
            (len(self.candidates) + _ON_BEHALF_PAGE_SIZE - 1)
            // _ON_BEHALF_PAGE_SIZE
        )

    def _candidates_for_page(self) -> list[dict]:
        start = self.page * _ON_BEHALF_PAGE_SIZE
        return self.candidates[start:start + _ON_BEHALF_PAGE_SIZE]

    def _build_components(self):
        self.clear_items()

        page_count = self.page_count
        if self.page >= page_count:
            self.page = max(0, page_count - 1)

        page_candidates = self._candidates_for_page()
        if page_candidates:
            selected_set = set(self.selected_target_ids)
            page_ids = {c["target_id"] for c in page_candidates}
            picks_on_this_page = [
                c for c in page_candidates if c["target_id"] in selected_set
            ]
            # Defensive: fall back to the target_id when name is
            # missing or empty. Discord rejects SelectOption with an
            # empty label, and bucket entries sometimes carry an
            # empty name field (the bucket builder couldn't resolve a
            # display name — usually a member who left Discord since
            # the on-behalf vote landed). The id is a better degraded
            # label than crashing the whole picker.
            options = [
                discord.SelectOption(
                    label=(c["name"] or c["target_id"] or "(unknown)")[:100],
                    value=c["target_id"][:100],
                    default=(c["target_id"] in selected_set),
                )
                for c in page_candidates
            ]
            placeholder = "Pick up to 30 players for this event…"
            if self.selected_target_ids:
                placeholder = (
                    f"Picked: {len(self.selected_target_ids)} of 30 "
                    f"(this page: {len(picks_on_this_page)})"
                )
            select = discord.ui.Select(
                placeholder=placeholder,
                min_values=0,
                max_values=len(options),
                options=options,
                row=0,
            )

            async def _on_pick(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                kept = [
                    tid for tid in self.selected_target_ids
                    if tid not in page_ids
                ]
                self.selected_target_ids = kept + list(select.values)
                self._build_components()
                try:
                    await inter.response.edit_message(view=self)
                except discord.HTTPException:
                    pass

            select.callback = _on_pick
            self.add_item(select)

        if self.page_count > 1:
            prev_btn = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary,
                disabled=(self.page == 0), row=1,
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
                disabled=True, row=1,
            )
            self.add_item(page_label)

            next_pg_btn = discord.ui.Button(
                label="Page ▶", style=discord.ButtonStyle.secondary,
                disabled=(self.page >= self.page_count - 1), row=1,
            )

            async def _on_next_page(inter: discord.Interaction):
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

            next_pg_btn.callback = _on_next_page
            self.add_item(next_pg_btn)

        # Advance to step 2 — enabled only when 1..30 picks are made.
        pick_count = len(self.selected_target_ids)
        next_btn = discord.ui.Button(
            label=f"Next ▶ Mark subs ({pick_count}/30)",
            style=discord.ButtonStyle.primary,
            disabled=not (1 <= pick_count <= STORM_PLAN_MAX_TOTAL),
            row=2,
        )

        async def _on_next(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            self.advance_to_step2 = True
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                await inter.response.edit_message(
                    content="↪️ Pick the subs (up to 10)…", view=self,
                )
            except discord.HTTPException:
                pass

        next_btn.callback = _on_next
        self.add_item(next_btn)

        cancel_btn = discord.ui.Button(
            label="↩️ Cancel", style=discord.ButtonStyle.secondary, row=2,
        )

        async def _on_cancel(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                await inter.response.edit_message(
                    content="↩️ Cancelled. Plan unchanged.", view=self,
                )
            except discord.HTTPException:
                pass

        cancel_btn.callback = _on_cancel
        self.add_item(cancel_btn)

        if self.has_prior_plan:
            clear_btn = discord.ui.Button(
                label="🗑️ Clear plan", style=discord.ButtonStyle.danger,
                row=2,
            )

            async def _on_clear(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                import config
                config.clear_storm_team_plan(
                    self.parent_view.guild_id,
                    self.parent_view.event_type,
                    self.parent_view.event_date,
                    self.team,
                )
                self.cleared = True
                for item in self.children:
                    item.disabled = True
                self.stop()
                try:
                    await inter.response.edit_message(
                        content="🗑️ Plan cleared.", view=self,
                    )
                except discord.HTTPException:
                    pass

            clear_btn.callback = _on_clear
            self.add_item(clear_btn)

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.parent_view.owner_user_id:
            await inter.response.send_message(
                "⛔ Only the officer who opened this view can edit the team plan.",
                ephemeral=True,
            )
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class _TeamPlanSubPickerView(discord.ui.View):
    """Step 2 of the team-plan picker (#239) — of the 30 picked in
    step 1, mark up to 10 as subs. The remaining are primaries.

    Save → atomic replace of the saved plan via
    `config.save_storm_team_plan`. Back → returns to step 1 with
    state preserved so the officer can swap picks before saving.
    """

    def __init__(
        self,
        parent_view: "OfficerView",
        team: str,
        chosen: list[dict],
        prior_subs: list[str],
    ):
        super().__init__(timeout=600)
        self.parent_view = parent_view
        self.team = team
        self.chosen = chosen  # [{"name": str, "target_id": str}]
        # Drop any prior-sub IDs that aren't in the step-1 picks anymore
        # (officer might have deselected them).
        chosen_ids = {c["target_id"] for c in chosen}
        self.selected_sub_ids: list[str] = [
            tid for tid in prior_subs if tid in chosen_ids
        ]
        self.saved: bool = False
        self.go_back: bool = False
        self.page = 0
        self.save_errors: list[str] = []
        self.message: discord.Message | None = None
        self._build_components()

    @property
    def page_count(self) -> int:
        if not self.chosen:
            return 1
        return (
            (len(self.chosen) + _ON_BEHALF_PAGE_SIZE - 1)
            // _ON_BEHALF_PAGE_SIZE
        )

    def _chosen_for_page(self) -> list[dict]:
        start = self.page * _ON_BEHALF_PAGE_SIZE
        return self.chosen[start:start + _ON_BEHALF_PAGE_SIZE]

    def _build_components(self):
        self.clear_items()

        page_count = self.page_count
        if self.page >= page_count:
            self.page = max(0, page_count - 1)

        page_chosen = self._chosen_for_page()
        if page_chosen:
            selected_set = set(self.selected_sub_ids)
            page_ids = {c["target_id"] for c in page_chosen}
            picks_on_this_page = [
                c for c in page_chosen if c["target_id"] in selected_set
            ]
            # Same defensive fallback as the step-1 picker — an empty
            # name would 400 the whole message and silently break the
            # Mark-subs step.
            options = [
                discord.SelectOption(
                    label=(c["name"] or c["target_id"] or "(unknown)")[:100],
                    value=c["target_id"][:100],
                    default=(c["target_id"] in selected_set),
                )
                for c in page_chosen
            ]
            placeholder = "Pick up to 10 subs (the rest are primaries)…"
            if self.selected_sub_ids:
                placeholder = (
                    f"Subs picked: {len(self.selected_sub_ids)} of 10 "
                    f"(this page: {len(picks_on_this_page)})"
                )
            select = discord.ui.Select(
                placeholder=placeholder,
                min_values=0,
                max_values=len(options),
                options=options,
                row=0,
            )

            async def _on_pick(inter: discord.Interaction):
                if not await self._guard_owner(inter):
                    return
                kept = [
                    tid for tid in self.selected_sub_ids
                    if tid not in page_ids
                ]
                self.selected_sub_ids = kept + list(select.values)
                self._build_components()
                try:
                    await inter.response.edit_message(view=self)
                except discord.HTTPException:
                    pass

            select.callback = _on_pick
            self.add_item(select)

        if self.page_count > 1:
            prev_btn = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary,
                disabled=(self.page == 0), row=1,
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
                disabled=True, row=1,
            )
            self.add_item(page_label)

            next_pg_btn = discord.ui.Button(
                label="Page ▶", style=discord.ButtonStyle.secondary,
                disabled=(self.page >= self.page_count - 1), row=1,
            )

            async def _on_next_page(inter: discord.Interaction):
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

            next_pg_btn.callback = _on_next_page
            self.add_item(next_pg_btn)

        sub_count = len(self.selected_sub_ids)
        primary_count = len(self.chosen) - sub_count
        save_disabled = (
            sub_count > STORM_PLAN_MAX_SUBS
            or primary_count > STORM_PLAN_MAX_PRIMARIES
        )
        save_btn = discord.ui.Button(
            label=f"💾 Save plan ({primary_count} primary / {sub_count} sub)",
            style=discord.ButtonStyle.success,
            disabled=save_disabled,
            row=2,
        )
        save_btn.callback = self._on_save
        self.add_item(save_btn)

        back_btn = discord.ui.Button(
            label="◀ Back", style=discord.ButtonStyle.secondary, row=2,
        )

        async def _on_back(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            self.go_back = True
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                await inter.response.edit_message(
                    content="◀ Back to player picker…", view=self,
                )
            except discord.HTTPException:
                pass

        back_btn.callback = _on_back
        self.add_item(back_btn)

        cancel_btn = discord.ui.Button(
            label="↩️ Cancel", style=discord.ButtonStyle.secondary, row=2,
        )

        async def _on_cancel(inter: discord.Interaction):
            if not await self._guard_owner(inter):
                return
            for item in self.children:
                item.disabled = True
            self.stop()
            try:
                await inter.response.edit_message(
                    content="↩️ Cancelled. Plan unchanged.", view=self,
                )
            except discord.HTTPException:
                pass

        cancel_btn.callback = _on_cancel
        self.add_item(cancel_btn)

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.parent_view.owner_user_id:
            await inter.response.send_message(
                "⛔ Only the officer who opened this view can edit the team plan.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_save(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        sub_set = set(self.selected_sub_ids)
        primaries = [
            c["target_id"] for c in self.chosen
            if c["target_id"] not in sub_set
        ]
        subs = [
            c["target_id"] for c in self.chosen
            if c["target_id"] in sub_set
        ]
        import config
        ok, errors = config.save_storm_team_plan(
            self.parent_view.guild_id,
            self.parent_view.event_type,
            self.parent_view.event_date,
            self.team,
            primaries=primaries,
            subs=subs,
            saved_by_user_id=inter.user.id,
        )
        if not ok:
            self.save_errors = errors
            try:
                await inter.response.send_message(
                    "⚠️ Couldn't save plan:\n• " + "\n• ".join(errors),
                    ephemeral=True,
                )
            except discord.HTTPException:
                pass
            return
        self.saved = True
        for item in self.children:
            item.disabled = True
        self.stop()
        try:
            await inter.response.edit_message(
                content=(
                    f"✅ Plan saved for Team {self.team}: "
                    f"{len(primaries)} primary, {len(subs)} sub. "
                    "Open **Set up Team " + self.team + "** to apply it."
                ),
                view=self,
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


async def _open_team_plan(
    inter: discord.Interaction, officer_view: "OfficerView", *, team: str,
) -> None:
    """Drive the two-step team-plan picker for one team (#239). Called
    from the 📋 Team A/B plan buttons on the officer view.

    Step 1 picks up to 30 players from the team's yes-pool (filtered
    against the other team's saved plan). Step 2 marks up to 10 of
    those as subs. Save persists via `config.save_storm_team_plan`;
    cancel or timeout leaves any prior plan untouched.
    """
    if inter.user.id != officer_view.owner_user_id:
        await inter.response.send_message(
            "⛔ Only the officer who opened this view can edit the team plan.",
            ephemeral=True,
        )
        return

    # Log entry so the click is at least visible in Railway logs even
    # if downstream silently fails. logger.info routes to stdout.
    logger.info(
        "[STORM TEAM PLAN] open click for guild=%s event=%s/%s team=%s "
        "by user=%s",
        officer_view.guild_id, officer_view.event_type,
        officer_view.event_date, team, inter.user.id,
    )

    # Defer immediately so any downstream blip (SQLite contention, slow
    # bucket walk, picker-view construction) never blows the 3-second
    # initial-response token. A tester reported `Interaction Failed` on
    # 📋 Team B plan with nothing in the logs — that's the textbook
    # signature of a timed-out interaction token. The actual error (if
    # any) surfaces through the followup-error path below.
    try:
        await inter.response.defer(ephemeral=True)
    except discord.HTTPException as e:
        # Re-raise so the outer `_plan_a` / `_plan_b` wrapper's
        # logger.exception captures the full traceback. The wrapper
        # will also attempt a followup.send (which will likely also
        # fail if the interaction is dead) — at minimum the bot logs
        # have a record.
        logger.exception(
            "[STORM TEAM PLAN] defer failed for guild=%s event=%s team=%s: %s",
            officer_view.guild_id, officer_view.event_type, team, e,
        )
        raise

    import config

    # Candidate pool: voted-yes for this team. "either" voters appear
    # in BOTH teams' pools, but the other-team filter below makes sure
    # we don't surface anyone the OTHER team's saved plan has already
    # claimed.
    if team == "A":
        eligible_buckets = ("a", "either")
    elif team == "B":
        eligible_buckets = ("b", "either")
    else:
        await inter.followup.send(
            f"⚠️ Unknown team `{team}`.", ephemeral=True,
        )
        return

    raw_pool: list[dict] = []
    seen_target_ids: set[str] = set()
    for k in eligible_buckets:
        for e in officer_view.buckets.get(k, []):
            tid = e.get("target_id")
            if not tid or tid in seen_target_ids:
                continue
            seen_target_ids.add(tid)
            raw_pool.append({"name": e.get("name", ""), "target_id": tid})

    # Cross-team filter: anyone already on the OTHER team's plan for
    # this event is hidden from this picker. The save-time validator
    # is the backstop if two officers race; this is the friendly path.
    other_team = "B" if team == "A" else "A"
    other_plan = config.get_storm_team_plan(
        officer_view.guild_id, officer_view.event_type,
        officer_view.event_date, other_team,
    ) or {"primaries": [], "subs": []}
    other_claimed = set(other_plan["primaries"]) | set(other_plan["subs"])
    if other_claimed:
        hidden_names = sorted({
            c["name"] for c in raw_pool if c["target_id"] in other_claimed
        })
        candidates = [
            c for c in raw_pool if c["target_id"] not in other_claimed
        ]
    else:
        hidden_names = []
        candidates = list(raw_pool)
    candidates.sort(key=lambda c: c["name"].lower())

    prior_plan = config.get_storm_team_plan(
        officer_view.guild_id, officer_view.event_type,
        officer_view.event_date, team,
    )
    prior_picks: list[str] = []
    prior_subs: list[str] = []
    prior_saved_at = ""
    if prior_plan:
        prior_picks = list(prior_plan["primaries"]) + list(prior_plan["subs"])
        prior_subs = list(prior_plan["subs"])
        prior_saved_at = prior_plan.get("saved_at", "")
        # If the saved plan references members no longer in the pool
        # (vote changed to "cannot", roster row removed, etc.), keep
        # them in the picker so the officer can deselect them — adding
        # phantom rows preserves the "edit, don't restart" experience.
        existing_ids = {c["target_id"] for c in candidates}
        for tid in prior_picks:
            if tid in existing_ids:
                continue
            # Best-effort name resolution from any bucket — fall back
            # to the id itself if we can't find a friendlier label.
            name = tid
            for k in ("a", "b", "either", "cannot"):
                for e in officer_view.buckets.get(k, []):
                    if e.get("target_id") == tid:
                        name = e.get("name") or name
                        break
            candidates.append({"name": f"{name} (vote changed)", "target_id": tid})
        candidates.sort(key=lambda c: c["name"].lower())

    if not candidates:
        await inter.followup.send(
            f"⚠️ No eligible players for Team {team} yet. Members need to "
            f"vote {'A' if team == 'A' else 'B'} or Either before they "
            f"can appear in the picker.",
            ephemeral=True,
        )
        return

    intro_lines = [
        f"📋 **Team {team} plan** — "
        f"pick up to **30** players for this event, then mark up to "
        f"**10** as subs.",
        f"_Candidate pool: {len(candidates)} member(s) who voted "
        f"{'A' if team == 'A' else 'B'} or Either._",
    ]
    if hidden_names:
        n = len(hidden_names)
        sample = ", ".join(hidden_names[:3])
        more = f" +{n - 3} more" if n > 3 else ""
        intro_lines.append(
            f"_{n} member(s) hidden — already on Team {other_team}: "
            f"{sample}{more}._"
        )

    first_response = True
    while True:
        step1 = _TeamPlanRosterPickerView(
            officer_view, team, candidates, sorted(other_claimed),
            prior_picks, prior_subs, prior_saved_at,
        )
        if first_response:
            # Always use followup.send — we deferred up front so the
            # initial response slot is already consumed. Followup
            # messages return a Message object directly, no separate
            # `original_response()` fetch needed.
            try:
                step1.message = await inter.followup.send(
                    "\n".join(intro_lines), view=step1, ephemeral=True,
                )
            except discord.HTTPException as e:
                # Surface the failure — the prior swallow meant the
                # picker silently hung on `await step1.wait()` because
                # no message ever appeared. Re-raise so the outer
                # `_plan_a` / `_plan_b` wrapper logs + acks.
                logger.exception(
                    "[STORM TEAM PLAN] followup.send failed for guild=%s "
                    "event=%s team=%s: %s",
                    officer_view.guild_id, officer_view.event_type, team, e,
                )
                raise
            first_response = False
        else:
            # Re-entry after step 2 "back" — same followup path.
            try:
                step1.message = await inter.followup.send(
                    "\n".join(intro_lines), view=step1, ephemeral=True,
                )
            except discord.HTTPException as e:
                logger.exception(
                    "[STORM TEAM PLAN] re-entry followup.send failed for "
                    "guild=%s event=%s team=%s: %s",
                    officer_view.guild_id, officer_view.event_type, team, e,
                )
                raise

        await step1.wait()
        if step1.cleared:
            await _refresh_officer_view_message(officer_view)
            return
        if not step1.advance_to_step2:
            return  # cancelled / timed out

        chosen_ids = list(step1.selected_target_ids)
        # Preserve display name from step 1's candidate list so step 2
        # shows the same names; sort case-insensitively for stability.
        name_lookup = {c["target_id"]: c["name"] for c in candidates}
        chosen = [
            {"name": name_lookup.get(tid, tid), "target_id": tid}
            for tid in chosen_ids
        ]
        chosen.sort(key=lambda c: c["name"].lower())

        step2 = _TeamPlanSubPickerView(
            officer_view, team, chosen, prior_subs=prior_subs,
        )
        try:
            step2.message = await inter.followup.send(
                f"📋 **Team {team} plan** — Step 2 of 2. Mark up to "
                f"**10** subs from the {len(chosen)} picked. The "
                f"remaining will be primaries.",
                view=step2, ephemeral=True,
            )
        except discord.HTTPException:
            step2.message = None

        await step2.wait()
        if step2.saved:
            await _refresh_officer_view_message(officer_view)
            return
        if step2.go_back:
            # Update prior_picks/prior_subs so re-entry shows the
            # tweaked state, then loop back to step 1.
            prior_picks = chosen_ids
            prior_subs = list(step2.selected_sub_ids)
            continue
        # Cancel / timeout from step 2 — nothing to do.
        return


async def _refresh_officer_view_message(officer_view: "OfficerView") -> None:
    """Re-render the officer view's public embed + rebuild its component
    row so the 📋 Team plan button label can flip between "📋 Team A plan"
    and "📋 Team A plan ✅", and the team-plan summary lines pick up the
    fresh `saved_at` timestamp. Best-effort — if the message was already
    deleted or the bot lost perms, the followup ack from the picker is
    the officer's confirmation."""
    if officer_view.message is None:
        return
    officer_view._build_components()  # rebuild button labels
    try:
        import config
        team_plans = config.get_storm_team_plans_for_event(
            officer_view.guild_id,
            officer_view.event_type,
            officer_view.event_date,
        )
        await officer_view.message.edit(
            embed=_render_embed(
                officer_view.guild,
                officer_view.event_type,
                officer_view.event_date,
                officer_view.buckets,
                officer_view.bucket_filter,
                team_plans=team_plans,
            ),
            view=officer_view,
        )
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
                # error so the officer knows to retry after /members sync.
                try:
                    await inter.followup.send(
                        "⚠️ Couldn't read the roster right now. Try "
                        "`/members sync` and reopen this view to retry.",
                        ephemeral=True,
                    )
                except discord.HTTPException:
                    pass
                return
            import config
            cfg = config.get_storm_config(self.guild_id, self.event_type) or {}
            teams_setting = (cfg.get("teams") or "both").strip()
            # Collect target_ids already in a vote bucket so the picker
            # can hide them by default — officers can flip them back in
            # via 👁️ when intentionally correcting a prior vote.
            voted_target_ids: set[str] = set()
            for k in ("a", "b", "either", "cannot"):
                for e in self.buckets.get(k, []):
                    voted_target_ids.add(e["target_id"])
            picker = _OnBehalfVoteView(
                self, roster_rows, teams_setting,
                voted_target_ids=voted_target_ids,
            )
            try:
                msg = await inter.followup.send(
                    content=(
                        "🙋 Pick one or more members and a vote, then "
                        "**Submit**. Already-voted members are hidden — "
                        "use **👁️ Show already-voted** to correct a "
                        "prior vote. **📥 Select all not-voted** stages "
                        "the remaining roster in one click. `/members "
                        "sync` refreshes the list."
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
        #
        # #240: when a saved draft exists for a team, that team's row
        # becomes `[♻️ Resume <ts>]  [🆕 Set up new]`. Resume is success/
        # green (the most likely intended path), Set up new is
        # secondary. When no draft exists, the row shows a single
        # `[🅰️ Set up Team A]` success button (pre-#240 behaviour).
        from config import get_storm_config, get_roster_draft
        cfg = get_storm_config(self.guild_id, self.event_type) or {}
        teams_setting = (cfg.get("teams") or "both").strip()
        if teams_setting not in ("both", "A", "B"):
            teams_setting = "both"

        show_a = teams_setting in ("both", "A")
        show_b = teams_setting in ("both", "B")

        def _format_draft_timestamp(updated_at_iso: str) -> str:
            """Render the saved-at timestamp for a Resume button label.
            Discord button labels don't render `<t:...>` format codes,
            so the timestamp is static absolute text computed at view
            render time. Format mirrors what the design source used:
            `May 21 8:42pm`."""
            from datetime import datetime, timezone
            try:
                dt = datetime.fromisoformat(updated_at_iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                # Localize to the guild's timezone for human-readable
                # labels (officers think in their alliance's local
                # time, not UTC).
                tz_name = (cfg.get("timezone") or "America/New_York")
                try:
                    from zoneinfo import ZoneInfo
                    dt = dt.astimezone(ZoneInfo(tz_name))
                except Exception:
                    pass
                hour = dt.hour % 12 or 12
                ampm = "am" if dt.hour < 12 else "pm"
                month_label = dt.strftime("%b")  # Jan / Feb / Mar / ...
                return (
                    f"{month_label} {dt.day} {hour}:{dt.minute:02d}{ampm}"
                )
            except (TypeError, ValueError):
                return "earlier"

        def _add_team_row(team_letter: str, row: int) -> None:
            """Render one team's row: paired `[Resume] [Set up new]`
            when a draft exists, single `[Set up Team X]` otherwise.
            Row index passed in so caller controls layout placement."""
            draft = get_roster_draft(
                self.guild_id, self.event_type, team_letter,
            )
            has_draft = draft is not None
            if has_draft:
                ts_label = _format_draft_timestamp(draft["updated_at"])
                resume_btn = discord.ui.Button(
                    label=f"♻️ Resume Team {team_letter} ({ts_label})",
                    style=discord.ButtonStyle.success, row=row,
                )

                async def _resume(inter: discord.Interaction):
                    await _open_team_setup(
                        inter, self, team=team_letter, resume=True,
                    )

                resume_btn.callback = _resume
                self.add_item(resume_btn)

                fresh_btn = discord.ui.Button(
                    label=f"🆕 Set up new Team {team_letter} roster",
                    style=discord.ButtonStyle.secondary, row=row,
                )

                async def _fresh(inter: discord.Interaction):
                    await _confirm_discard_and_setup(
                        inter, self, team=team_letter,
                    )

                fresh_btn.callback = _fresh
                self.add_item(fresh_btn)
            else:
                solo_btn = discord.ui.Button(
                    label=f"🅰️ Set up Team {team_letter}"
                    if team_letter == "A"
                    else f"🅱️ Set up Team {team_letter}",
                    style=discord.ButtonStyle.success, row=row,
                )

                async def _setup(inter: discord.Interaction):
                    await _open_team_setup(inter, self, team=team_letter)

                solo_btn.callback = _setup
                self.add_item(solo_btn)

        # Layout: Team A on row 2, Team B on row 4. Plan buttons sit
        # between them on row 3 so each team's actions cluster
        # vertically (Set up / Plan together for Team A, then Set up /
        # Plan together for Team B — easier to scan than mixing
        # teams across rows).
        if show_a:
            _add_team_row("A", row=2)
        if show_b:
            _add_team_row("B", row=4)

        # 📋 Team plan buttons (#239) — capture the 20+10 split the
        # officer committed to in-game so the roster builder's auto-fill
        # mirrors the in-game submission instead of re-deriving its own
        # split. Saved label flips to ✅ so officers can see at a glance
        # whether the plan is current. Same `teams` gate as the setup
        # buttons above so single-team alliances don't see the other
        # team's button.
        from config import get_storm_team_plan as _get_team_plan
        if show_a:
            plan_a_saved = _get_team_plan(
                self.guild_id, self.event_type, self.event_date, "A",
            ) is not None
            a_plan_btn = discord.ui.Button(
                label="📋 Team A plan" + (" ✅" if plan_a_saved else ""),
                style=discord.ButtonStyle.secondary, row=3,
            )

            async def _plan_a(inter: discord.Interaction):
                try:
                    await _open_team_plan(inter, self, team="A")
                except Exception as e:
                    logger.exception(
                        "[STORM TEAM PLAN] open failed for guild=%s "
                        "event=%s team=A: %s",
                        self.guild_id, self.event_type, e,
                    )
                    try:
                        await inter.followup.send(
                            "⚠️ Couldn't open the Team A plan picker. "
                            "Bot logs have details. Try clicking 🔄 "
                            "Refresh and try again.",
                            ephemeral=True,
                        )
                    except discord.HTTPException:
                        pass

            a_plan_btn.callback = _plan_a
            self.add_item(a_plan_btn)

        if show_b:
            plan_b_saved = _get_team_plan(
                self.guild_id, self.event_type, self.event_date, "B",
            ) is not None
            b_plan_btn = discord.ui.Button(
                label="📋 Team B plan" + (" ✅" if plan_b_saved else ""),
                style=discord.ButtonStyle.secondary, row=3,
            )

            async def _plan_b(inter: discord.Interaction):
                try:
                    await _open_team_plan(inter, self, team="B")
                except Exception as e:
                    logger.exception(
                        "[STORM TEAM PLAN] open failed for guild=%s "
                        "event=%s team=B: %s",
                        self.guild_id, self.event_type, e,
                    )
                    try:
                        await inter.followup.send(
                            "⚠️ Couldn't open the Team B plan picker. "
                            "Bot logs have details. Try clicking 🔄 "
                            "Refresh and try again.",
                            ephemeral=True,
                        )
                    except discord.HTTPException:
                        pass

            b_plan_btn.callback = _plan_b
            self.add_item(b_plan_btn)


async def _open_team_setup(
    inter: discord.Interaction, officer_view: "OfficerView", *, team: str,
    resume: bool = False,
) -> None:
    """Pick a preset, then hand off to the structured roster builder.
    Called from the Set-up-Team buttons on the officer view.

    `resume=True` (#240) skips the preset picker when a saved draft
    exists for this team (the draft already carries the preset name),
    and threads `resume_from_draft=True` into `open_roster_builder` so
    the saved zone assignments + pairings load on top of the freshly-
    built session.
    """
    if inter.user.id != officer_view.owner_user_id:
        await inter.response.send_message(
            "⛔ Only the officer who opened this view can start team setup.",
            ephemeral=True,
        )
        return

    # #240 Resume path: the saved draft already names the preset. Skip
    # the preset picker and go straight to the builder. If the draft
    # vanished between officer-view render and click (extreme race),
    # fall through to the fresh-setup picker.
    if resume:
        import config
        draft = config.get_roster_draft(
            officer_view.guild_id, officer_view.event_type, team,
        )
        if draft is not None:
            try:
                import json
                payload = json.loads(draft["session_json"])
                preset_name = payload.get("selected_preset_name", "")
            except (ValueError, KeyError):
                preset_name = ""
            if preset_name:
                # #240 follow-up: graceful handling when the saved
                # preset was renamed or deleted. Without this check
                # `open_roster_builder` bails on `load_preset` →
                # None with a generic "no preset named X" error and
                # the orphan draft sits there forever. Surface a
                # clearer error + a discard button so the officer
                # can clean up the stale row.
                import storm_strategy as ss
                preset = await asyncio.to_thread(
                    ss.load_preset,
                    officer_view.guild_id, officer_view.event_type,
                    preset_name,
                )
                if preset is None:
                    orphan_view = _OrphanDraftDiscardView(
                        owner_id=inter.user.id,
                        officer_view=officer_view, team=team,
                    )
                    await inter.response.send_message(
                        f"📋 The saved draft for **Team {team}** "
                        f"references a strategy preset named "
                        f"**{preset_name}** which no longer exists "
                        f"(it may have been renamed or deleted). "
                        f"Discard the orphan draft and start fresh "
                        f"with a current preset?",
                        view=orphan_view, ephemeral=True,
                    )
                    try:
                        orphan_view.message = await inter.original_response()
                    except discord.HTTPException:
                        orphan_view.message = None
                    return
                await inter.response.defer(ephemeral=False, thinking=True)
                from storm_roster_builder import open_roster_builder
                await open_roster_builder(
                    inter, officer_view.event_type, preset_name,
                    event_date=officer_view.event_date,
                    team_override=team or None,
                    resume_from_draft=True,
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
    # #240 chain-from-confirm path: the discard-confirm view already
    # responded to `inter` via `edit_message`, so a follow-up has to
    # ride `interaction.followup.send`. Fresh path: `response` is
    # unused, send via `response.send_message`.
    prompt = f"Pick a strategy preset to apply for **{team_label}**:"
    if inter.response.is_done():
        picker.message = await inter.followup.send(
            prompt, view=picker, ephemeral=True,
        )
    else:
        await inter.response.send_message(
            prompt, view=picker, ephemeral=True,
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


async def _confirm_discard_and_setup(
    inter: discord.Interaction, officer_view: "OfficerView", *, team: str,
) -> None:
    """#240: when the officer clicks `🆕 Set up new Team X` and a saved
    draft exists, confirm before discarding the draft. Yes → delete
    the draft + open the preset picker; Cancel → back to officer
    view, draft untouched."""
    if inter.user.id != officer_view.owner_user_id:
        await inter.response.send_message(
            "⛔ Only the officer who opened this view can start team setup.",
            ephemeral=True,
        )
        return

    import config
    draft = config.get_roster_draft(
        officer_view.guild_id, officer_view.event_type, team,
    )
    if draft is None:
        # Edge case — draft vanished between render and click. Go
        # straight to the fresh setup path without a redundant confirm.
        await _open_team_setup(inter, officer_view, team=team)
        return

    confirm = _DiscardDraftConfirmView(
        owner_id=inter.user.id, officer_view=officer_view, team=team,
        draft_event_date=draft.get("event_date", ""),
    )
    await inter.response.send_message(
        f"⚠️ You have a saved roster draft for **Team {team}** from "
        f"**{draft.get('event_date', 'an earlier event')}**. Starting "
        f"fresh will discard it.",
        view=confirm, ephemeral=True,
    )
    try:
        confirm.message = await inter.original_response()
    except discord.HTTPException:
        confirm.message = None


class _DiscardDraftConfirmView(discord.ui.View):
    """Two-button confirm shown before `🆕 Set up new` overwrites a
    saved draft (#240). Yes → delete draft + open preset picker;
    Cancel → close ephemeral, draft untouched."""

    def __init__(
        self, *, owner_id: int, officer_view: "OfficerView", team: str,
        draft_event_date: str,
    ):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.officer_view = officer_view
        self.team = team
        self.draft_event_date = draft_event_date
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message(
                "⛔ Only the officer who opened this can confirm.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Yes, start over",
                       style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction,
                      _btn: discord.ui.Button):
        if self.is_finished():
            return
        import config
        try:
            config.delete_roster_draft(
                self.officer_view.guild_id,
                self.officer_view.event_type,
                self.team,
            )
        except Exception:
            pass
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(
                content=(
                    f"🆕 Starting fresh for **Team {self.team}**. Pick a "
                    f"strategy preset to apply..."
                ),
                view=self,
            )
        except discord.HTTPException:
            pass
        self.stop()
        # Open the preset picker via the existing fresh-setup path.
        # We've already responded to `inter`, so `_open_team_setup`
        # will fall through to its `send_message` call which Discord
        # rejects on a responded interaction — call via followup
        # instead by passing a fresh-state path.
        await _open_team_setup(inter, self.officer_view, team=self.team)

    @discord.ui.button(label="↩️ Cancel",
                       style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction,
                     _btn: discord.ui.Button):
        if self.is_finished():
            return
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(
                content="↩️ Cancelled. Your saved draft is still there.",
                view=self,
            )
        except discord.HTTPException:
            pass
        self.stop()


class _OrphanDraftDiscardView(discord.ui.View):
    """Shown when a Resume click hits a draft whose `selected_preset_name`
    no longer exists (preset was renamed or deleted between save and
    resume). #240 follow-up — without this the officer just sees a
    generic "no preset named X" error and the orphan row sits there
    forever, blocking future Resume clicks too."""

    def __init__(
        self, *, owner_id: int, officer_view: "OfficerView", team: str,
    ):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.officer_view = officer_view
        self.team = team
        self.message: Optional[discord.Message] = None

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message(
                "⛔ Only the officer who opened this can confirm.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="🗑️ Discard orphan draft",
                       style=discord.ButtonStyle.danger)
    async def discard(self, inter: discord.Interaction,
                      _btn: discord.ui.Button):
        if self.is_finished():
            return
        import config
        try:
            config.delete_roster_draft(
                self.officer_view.guild_id,
                self.officer_view.event_type,
                self.team,
            )
        except Exception:
            pass
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(
                content=(
                    f"🗑️ Orphan draft cleared. Click **Set up Team "
                    f"{self.team}** on the signups view to start fresh."
                ),
                view=self,
            )
        except discord.HTTPException:
            pass
        self.stop()

    @discord.ui.button(label="↩️ Keep draft (do nothing)",
                       style=discord.ButtonStyle.secondary)
    async def keep(self, inter: discord.Interaction,
                   _btn: discord.ui.Button):
        if self.is_finished():
            return
        for item in self.children:
            item.disabled = True
        try:
            await inter.response.edit_message(
                content=(
                    "↩️ Draft kept. Resume won't work until the missing "
                    "preset is restored or you discard the draft."
                ),
                view=self,
            )
        except discord.HTTPException:
            pass
        self.stop()


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
