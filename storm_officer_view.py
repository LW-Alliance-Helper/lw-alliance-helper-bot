"""
`/storm_signups` officer view (#125).

Leadership-only command that surfaces who's voted for an event,
grouped by vote bucket, with a path to cast on-behalf votes for
members who don't use Discord.

v1 enumeration:
  * Discord members come from `guild.members` filtered by the
    member_roster `role_filter_id` (so the same role gate that drives
    the alliance roster sync drives the officer view).
  * Non-Discord members appear in the appropriate bucket once an
    officer casts an on-behalf vote for them via the modal — they
    self-populate into the view from the bot's `storm_signups` table.

Buckets:
  🅰 Voted Team A    — vote=a
  🅱 Voted Team B    — vote=b
  🔄 Voted Either    — vote=either
  ❌ Voted Cannot    — vote=cannot
  ❓ Not voted yet   — Discord member with no row in storm_signups

The "Vote on behalf" button captures the casting officer's Discord
ID alongside the vote, so audit history shows who recorded what.
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

_BUCKET_ORDER = ("a", "b", "either", "cannot", "not_voted")
_BUCKET_LABELS = {
    "a":         "🅰️ Voted Team A",
    "b":         "🅱️ Voted Team B",
    "either":    "🔄 Voted Either",
    "cannot":    "❌ Voted Cannot",
    "not_voted": "❓ Not voted yet",
}


def _user_can_run(interaction: discord.Interaction) -> bool:
    from config import get_config
    member = interaction.user
    if isinstance(member, discord.Member) and member.guild_permissions.administrator:
        return True
    cfg = get_config(interaction.guild_id) if interaction.guild_id else None
    leader_role_id = getattr(cfg, "leader_role_id", 0) if cfg else 0
    if leader_role_id and isinstance(member, discord.Member):
        return any(r.id == leader_role_id for r in member.roles)
    return False


def _next_event_date(today: _dt.date | None = None) -> str:
    """Default event date when leadership doesn't pass one — next Sunday
    by convention. Alliances who run DS on a different day pass the
    event_date param explicitly."""
    today = today or _dt.date.today()
    days_ahead = (6 - today.weekday()) % 7  # 6 = Sunday in Python's weekday()
    if days_ahead == 0:
        days_ahead = 7  # "today" defaults to next week, not today
    return (today + _dt.timedelta(days=days_ahead)).isoformat()


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
) -> dict[str, list[dict]]:
    """Group every relevant member into a vote bucket.

    Returns: {bucket_key: [ {label, target_id, is_on_behalf} ... ]}
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
            "label":        m.display_name,
            "target_id":    target_id,
            "is_on_behalf": bool(row["is_on_behalf"]) if row else False,
        })

    # Non-Discord targets (only appear via on-behalf votes) — they have
    # a row in storm_signups but no entry in `_discord_member_pool`.
    for target_id, row in by_target.items():
        if target_id in seen_targets:
            continue
        bucket = row["vote"] if row["vote"] in buckets else "cannot"
        buckets[bucket].append({
            "label":        target_id,  # roster member name stored verbatim
            "target_id":    target_id,
            "is_on_behalf": bool(row["is_on_behalf"]),
        })

    # Sort each bucket alphabetically.
    for k in buckets:
        buckets[k].sort(key=lambda e: e["label"].lower())
    return buckets


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

    desc_lines: list[str] = []
    for bucket_key in _BUCKET_ORDER:
        if bucket_filter and bucket_filter != bucket_key:
            continue
        entries = buckets[bucket_key]
        if not entries and bucket_filter is None:
            # Always show empty buckets when not filtered, so leadership
            # can see "0 voted Cannot" at a glance.
            desc_lines.append(f"\n**{_BUCKET_LABELS[bucket_key]}** (0)\n_(none)_")
            continue
        if not entries:
            continue
        line_label = f"**{_BUCKET_LABELS[bucket_key]}** ({len(entries)})"
        names = []
        for e in entries:
            name = e["label"]
            if e["is_on_behalf"]:
                name = f"{name} _(on behalf)_"
            names.append(name)
        # Discord embed description has a 4096-char limit. Truncate long
        # buckets with an overflow hint.
        joined = ", ".join(names)
        if len(joined) > 900:
            joined = joined[:900].rsplit(",", 1)[0] + f", … (+{len(names) - joined.count(',') - 1} more)"
        desc_lines.append(f"\n{line_label}\n{joined}")

    embed = discord.Embed(
        title=title,
        description="\n".join(desc_lines) if desc_lines else "_No data yet._",
        color=discord.Color.gold() if event_type == "DS" else discord.Color.orange(),
    )
    counts_line = " · ".join(
        f"{_BUCKET_LABELS[k].split(' ')[0]} {len(buckets[k])}" for k in _BUCKET_ORDER
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
    """Modal for casting a vote for a non-Discord member."""

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

        import config
        ok = config.record_storm_vote(
            self._view.guild_id, self._view.event_type, self._view.event_date,
            voter_user_id=interaction.user.id,
            target_member_id=raw_member,
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
        self.refresh_buckets()
        self._build_components()

    def refresh_buckets(self):
        self.buckets = _build_bucket_map(self.guild, self.event_type, self.event_date)

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
    async def storm_signups(
        self,
        interaction: discord.Interaction,
        event_type: app_commands.Choice[str],
        event_date: Optional[str] = None,
    ):
        if not _user_can_run(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to view storm sign-ups.",
                ephemeral=True,
            )
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

        view = OfficerView(interaction.guild, interaction.user.id, et, date_clean)
        await interaction.response.send_message(
            embed=_render_embed(interaction.guild, et, date_clean, view.buckets),
            view=view,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(StormSignupsViewCog(bot))
