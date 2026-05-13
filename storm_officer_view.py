"""
`/storm_signups` officer view (#125).

Leadership-only command that surfaces who's voted for an event,
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
on-behalf modal validates names against the roster Sheet so typos
don't create phantom signup rows that haunt every future officer view.
"""

from __future__ import annotations

import datetime as _dt
import logging
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)


# ── Bucket layout ────────────────────────────────────────────────────────────

# Per-process stale-roster-ID warning dedupe — keyed on
# `(guild_id, frozenset(stale_ids))`. The View's refresh button and
# the on-behalf modal both call `_read_roster_rows`; without dedup,
# every click re-logged the same stale-ID list. The set is bounded by
# the number of stale-ID combinations across reachable guilds.
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
    """Default event date when leadership doesn't pass one — next Sunday
    by convention. Alliances who run DS on a different day pass the
    event_date param explicitly."""
    today = today or _dt.date.today()
    days_ahead = (6 - today.weekday()) % 7  # 6 = Sunday in Python's weekday()
    if days_ahead == 0:
        days_ahead = 7  # "today" defaults to next week, not today
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
        # Dedup the log — refresh button + on-behalf modal re-call this
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

    buckets: dict[str, list[dict]] = {k: [] for k in _BUCKET_ORDER}

    seen_targets: set[str] = set()

    # Discord members → bucket from row or "not_voted".
    for m in _discord_member_pool(guild):
        target_id = str(m.id)
        seen_targets.add(target_id)
        row = by_target.get(target_id)
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
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    emoji = "🔥" if event_type == "DS" else "🏜️"
    try:
        d = _dt.date.fromisoformat(event_date)
        date_pretty = d.strftime("%A, %B %d, %Y")
    except ValueError:
        date_pretty = event_date

    total = sum(len(v) for v in buckets.values())
    title = f"{emoji} {label} Sign-Ups — {date_pretty}  ({total} members)"

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
        footnote = "\n¹ Not on Discord — cast their vote with **🙋 Record on-behalf vote**."
        if used + len(footnote) <= _DESCRIPTION_BUDGET:
            desc_lines.append(footnote)

    description = "\n".join(desc_lines) if desc_lines else "_No data yet._"
    if truncated_buckets:
        description += "\n\n_Some buckets clipped — use the filter dropdown to drill in._"

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


class _OnBehalfModal(discord.ui.Modal, title="Record vote on behalf"):
    """Modal for casting a vote for a non-Discord member.

    Validates the member name against the roster Sheet so a typo doesn't
    create a phantom signup row that haunts every future officer view.
    """

    def __init__(self, view: "OfficerView"):
        super().__init__()
        self._view = view
        self.member_name = discord.ui.TextInput(
            label="Member name (must match your roster Sheet)",
            placeholder="e.g. Alice",
            required=True, max_length=80,
        )
        self.vote_label = discord.ui.TextInput(
            label="Vote: A / B / Either / Cannot",
            placeholder="A",
            required=True, max_length=10,
        )
        self.add_item(self.member_name)
        self.add_item(self.vote_label)

    async def on_submit(self, interaction: discord.Interaction):
        raw_member = (self.member_name.value or "").strip()
        raw_vote   = (self.vote_label.value  or "").strip().lower()
        vote_map = {
            "a": "a", "team a": "a",
            "b": "b", "team b": "b",
            "either": "either", "either time": "either",
            "cannot": "cannot", "cannot participate": "cannot", "no": "cannot",
        }
        vote = vote_map.get(raw_vote)
        if not raw_member or not vote:
            await interaction.response.send_message(
                "⚠️ I couldn't read that. Member name and one of `A`, `B`, "
                "`Either`, or `Cannot`. Try again.",
                ephemeral=True,
            )
            return

        # Resolve the member name against the roster Sheet (case-insensitive)
        # so typos don't create phantom signup rows. If the roster doesn't
        # have a `not_on_discord` column yet (or the Sheet read failed), we
        # fall back to permissive behaviour to keep the command useful.
        roster_rows, _errors = _read_roster_rows(
            self._view.guild_id, guild=self._view.guild,
        )
        canonical_name = raw_member
        if roster_rows:
            match = next(
                (r for r in roster_rows
                 if r["name"].strip().lower() == raw_member.lower()),
                None,
            )
            if match is None:
                await interaction.response.send_message(
                    f"⚠️ I don't see **{raw_member}** in your roster Sheet. "
                    f"Check the spelling (it must match the name column on "
                    f"the roster tab) and try again.",
                    ephemeral=True,
                )
                return
            canonical_name = match["name"]

        import config
        ok = config.record_storm_vote(
            self._view.guild_id, self._view.event_type, self._view.event_date,
            voter_user_id=interaction.user.id,
            target_member_id=canonical_name,
            vote=vote,
            is_on_behalf=True,
        )
        if not ok:
            await interaction.response.send_message(
                "⚠️ Couldn't record that vote. Check the bot logs.",
                ephemeral=True,
            )
            return

        # Refresh the view in place.
        self._view.refresh_buckets()
        await interaction.response.edit_message(
            embed=_render_embed(
                self._view.guild, self._view.event_type, self._view.event_date,
                self._view.buckets, self._view.bucket_filter,
            ),
            view=self._view,
        )


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
        self.refresh_buckets()
        self._build_components()

    def refresh_buckets(self):
        self.buckets, self.roster_errors = _build_bucket_map(
            self.guild, self.event_type, self.event_date,
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
            await inter.response.send_modal(_OnBehalfModal(self))
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
            self.refresh_buckets()
            await inter.response.edit_message(
                embed=_render_embed(self.guild, self.event_type, self.event_date,
                                    self.buckets, self.bucket_filter),
                view=self,
            )
        refresh_btn.callback = _refresh
        self.add_item(refresh_btn)

        # Team setup buttons (#129) — opens the structured roster builder
        # filtered to signed-up members for this team. DS gets two buttons;
        # CS has a single "Set up Roster" since the faction is implicit
        # in the preset.
        if self.event_type == "DS":
            a_btn = discord.ui.Button(
                label="🅰️ Set up Team A", style=discord.ButtonStyle.success, row=2,
            )
            b_btn = discord.ui.Button(
                label="🅱️ Set up Team B", style=discord.ButtonStyle.success, row=2,
            )

            async def _setup_a(inter: discord.Interaction):
                await _open_team_setup(inter, self, team="A")

            async def _setup_b(inter: discord.Interaction):
                await _open_team_setup(inter, self, team="B")

            a_btn.callback = _setup_a
            b_btn.callback = _setup_b
            self.add_item(a_btn)
            self.add_item(b_btn)
        else:
            cs_btn = discord.ui.Button(
                label="🏜️ Set up Roster", style=discord.ButtonStyle.success, row=2,
            )

            async def _setup_cs(inter: discord.Interaction):
                await _open_team_setup(inter, self, team="")

            cs_btn.callback = _setup_cs
            self.add_item(cs_btn)


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
        await inter.response.send_message(
            f"⚠️ No strategy presets defined yet for "
            f"{'Desert Storm' if officer_view.event_type == 'DS' else 'Canyon Storm'}. "
            f"Run `/ds_strategy create` (or `/cs_strategy create`) first.",
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
                content=f"✅ Preset **{self.selected_preset}** selected — "
                        f"opening the roster builder…",
                view=self,
            )
            self.stop()

        select.callback = _on_pick
        self.add_item(select)


# ── Cog ──────────────────────────────────────────────────────────────────────


class StormSignupsViewCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="storm_signups",
        description="Leadership view of who's signed up for an upcoming storm event",
    )
    @app_commands.describe(
        event_type="Which event's sign-ups to view",
        event_date="Optional — defaults to the next upcoming event date",
    )
    @app_commands.choices(event_type=[
        app_commands.Choice(name="Desert Storm", value="DS"),
        app_commands.Choice(name="Canyon Storm", value="CS"),
    ])
    @app_commands.guild_only()
    async def storm_signups(
        self,
        interaction: discord.Interaction,
        event_type: app_commands.Choice[str],
        event_date: Optional[str] = None,
    ):
        from storm_permissions import (
            is_leader_or_admin,
            deny_non_leader,
            ensure_premium_structured,
        )

        if not is_leader_or_admin(interaction):
            await deny_non_leader(interaction)
            return

        et = event_type.value
        date_clean = (event_date or "").strip()
        if not date_clean:
            date_clean = _next_event_date()
        try:
            _dt.date.fromisoformat(date_clean)
        except ValueError:
            await interaction.response.send_message(
                f"⚠️ `{date_clean}` isn't a valid date. Use the format `YYYY-MM-DD`.",
                ephemeral=True,
            )
            return

        ok, _structured = await ensure_premium_structured(
            interaction, et,
            bot=self.bot,
            feature_label="`/storm_signups`",
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
                "⚠️ Roster Sheet read had issues — non-Discord member "
                f"enumeration may be incomplete: {preview}"
            )
            logger.warning(
                "[STORM OFFICER VIEW] roster errors for guild=%s: %s",
                interaction.guild_id, "; ".join(view.roster_errors),
            )
        await interaction.followup.send(**followup_args)

        # First-run walkthrough offer (#130). Fires after the main view
        # lands so the officer sees the actual command output even if
        # they decline the tour. No-op if already dismissed. Failures
        # here must not crash the main flow — the officer view is the
        # critical path; the tour is a nice-to-have.
        try:
            from storm_walkthrough import maybe_offer_storm_signups_tour
            await maybe_offer_storm_signups_tour(interaction)
        except Exception as e:
            logger.warning(
                "[STORM OFFICER VIEW] walkthrough offer failed for guild=%s: %s",
                interaction.guild_id, e,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(StormSignupsViewCog(bot))
