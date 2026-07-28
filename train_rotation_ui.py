"""
train_rotation_ui.py — shared core for Train Conductor Rotation's Discord UI
(#55): RotationState + the runtime state/draft loaders, the embed builders,
and the picker components used by more than one surface (_RosterPickerView,
_MemberNameModal, AssignmentLogsView).

The three surfaces themselves live in sibling files, split out of this one
in #373 once it passed ~2000 lines (each still imports this module as `ui`
for the shared pieces above, so `patch.object(ui, ...)` test hooks keep
working regardless of which file defines a given symbol):

- **train_rotation_ui_presets.py** — TrainPresetEditorView, a live,
  owner-locked editor for a schedule preset. Pick a day from a dropdown, set
  its rule with another dropdown, Save. (The issue mocked dropdowns *inside*
  a modal; Discord modals only hold text inputs, so the editor is a single
  live message instead.)
- **train_rotation_ui_draft.py** — WeeklyDraftView, the Sunday draft posted
  to leadership. A day picker plus Next / Assign / Skip / Regenerate buttons
  act on the chosen day. (The issue mocked per-day button rows; a 7-day ×
  3-button grid exceeds Discord's 5-action-row cap, so it's a day-select +
  shared buttons.)
- **train_rotation_ui_confirm.py** — DailyConfirmView, each drive day's
  confirmation. Confirm posts the conductor publicly (blurb + optional
  image URL — modals can't upload files).

Kept separate from train_ui.py (the legacy blurb UI) to hold both files at a
manageable size, matching the repo's train.py / train_cog.py split.
"""

import asyncio
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import discord

import wizard_registry
import train_rotation as tr

DENY_NOT_LEADER = "⛔ You need the leadership role to use this."
DENY_NOT_OWNER = "⛔ Only the person who opened this editor can change it."
EDITOR_TIMEOUT = 900  # 15 min — Discord's component interaction-token ceiling


# ── Runtime state ─────────────────────────────────────────────────────────────


class RotationState:
    """Everything the selection algorithm needs for one guild, loaded together.

    Bundles the full roster pool, the per-rule-type role pools, member rules,
    history, and counted-reason set so the weekly-draft generator and the view
    re-roll callbacks share one consistent snapshot. `role_pools` maps a rule
    type (leadership / vs / contest / event) to the member names in its assigned
    role; leadership defaults to the alliance's main leadership role."""

    def __init__(
        self,
        *,
        cfg: dict,
        roster: list[dict],
        eligible_pool: list[str],
        role_pools: dict[str, list[str]],
        member_rules: list,
        history: list,
        counted_reasons: set,
        role_rules_enabled: bool = True,
    ):
        self.cfg = cfg
        self.roster = roster
        self.eligible_pool = eligible_pool
        self.role_pools = role_pools
        self.member_rules = member_rules
        self.history = history
        self.counted_reasons = counted_reasons
        # Role-scoped day rules are Premium (#337). When False, role pools are
        # empty and the draft/reroll fall back to the full roster.
        self.role_rules_enabled = role_rules_enabled


def _resolve_leadership_role(bot, guild_id: int, cfg: dict):
    """The guild's main leadership role — the default `leadership` day-rule pool
    when no explicit leadership-rule role is assigned in rule_type_roles."""
    guild = bot.get_guild(guild_id)
    if guild is None:
        return None
    from config import get_config

    gcfg = get_config(guild_id)
    if gcfg and gcfg.leadership_role_name:
        return discord.utils.get(guild.roles, name=gcfg.leadership_role_name)
    return None


def load_rotation_state(bot, guild_id: int, *, is_premium: bool = True) -> RotationState:
    """Load config + roster + rules + history into a RotationState.

    Does the (blocking) Sheet reads; callers in async contexts should wrap this
    in `asyncio.to_thread` and pass `is_premium` (resolved via
    `premium.is_premium` in the async context — this function is sync). Role
    pools are a Premium capability (#337): when `is_premium` is False they're
    left empty and the draft/reroll fall back to a full-roster auto pick.
    Per-rule-type role pools are resolved against the roster by Discord ID so
    their names match Train History."""
    from config import get_train_config

    cfg = get_train_config(guild_id)
    roster = tr.load_roster_members(guild_id)
    eligible_pool = tr.roster_names(roster)

    guild = bot.get_guild(guild_id)
    role_pools: dict[str, list[str]] = {}
    # Role-scoped day rules (leadership / vs / contest / event) are Premium-only
    # (#337). On free tier we skip building role pools entirely — select_conductor
    # falls back to the full roster for those days.
    if is_premium:
        # Explicit per-rule-type role assignments (#55). cfg["rule_type_roles"] is
        # a {rule_type: role_id} dict (parsed in config.get_train_config).
        rule_type_roles = cfg.get("rule_type_roles") or {}
        for rt, role_id in rule_type_roles.items():
            role = guild.get_role(int(role_id)) if (guild and role_id) else None
            if role:
                role_pools[rt] = tr.role_pool_from_roster(roster, {str(m.id) for m in role.members})

        # Leadership defaults to the alliance's main leadership role when no
        # explicit leadership-rule role was assigned.
        if tr.RULE_LEADERSHIP not in role_pools:
            lead_role = _resolve_leadership_role(bot, guild_id, cfg)
            if lead_role:
                role_pools[tr.RULE_LEADERSHIP] = tr.role_pool_from_roster(
                    roster, {str(m.id) for m in lead_role.members}
                )

    member_rules = tr.load_member_rules(guild_id, cfg.get("member_rules_tab") or "")
    # Canonicalize ID-tagged history rows to the roster's current names so a
    # renamed member keeps one record (name-only rows fall back as-is).
    history = tr.canonicalize_history(
        tr.load_history(guild_id, cfg.get("history_tab") or ""), roster
    )
    counted = tr.parse_counted_reasons(cfg.get("counted_reasons"))

    return RotationState(
        cfg=cfg,
        roster=roster,
        eligible_pool=eligible_pool,
        role_pools=role_pools,
        member_rules=member_rules,
        history=history,
        counted_reasons=counted,
        role_rules_enabled=is_premium,
    )


# ── Async wrappers ────────────────────────────────────────────────────────────
# Role-scoped day rules are Premium (#337). The Sheet-reading builders below run
# off-thread (sync); these wrappers resolve Premium in the async context and
# thread it through, so role pools are built — and role days honored — only for
# Premium guilds. Free guilds get the full-roster fallback.


async def _resolve_premium(bot, guild_id: int) -> bool:
    import premium

    return await premium.is_premium(guild_id, bot=bot)


async def load_rotation_state_async(bot, guild_id: int) -> RotationState:
    """Resolve Premium, then load the rotation state off-thread."""
    is_premium = await _resolve_premium(bot, guild_id)
    return await asyncio.to_thread(load_rotation_state, bot, guild_id, is_premium=is_premium)


async def load_week_draft_async(bot, guild_id: int, week_start: date) -> list[tr.DraftDay]:
    """Resolve Premium, then load the week draft off-thread."""
    is_premium = await _resolve_premium(bot, guild_id)
    return await asyncio.to_thread(
        load_week_draft, bot, guild_id, week_start, is_premium=is_premium
    )


async def regenerate_week_async(bot, guild_id: int, week_start: date) -> list[tr.DraftDay]:
    """Resolve Premium, then regenerate + persist the week draft off-thread."""
    is_premium = await _resolve_premium(bot, guild_id)
    return await asyncio.to_thread(
        regenerate_week, bot, guild_id, week_start, is_premium=is_premium
    )


def week_start_for(d: date) -> date:
    """The Monday on or before `d` (weekday() 0 = Monday)."""
    return d - timedelta(days=d.weekday())


def default_draft_week(today: date, draft_day: int) -> date:
    """The week (Monday) the draft view should open to by default.

    The current week, except on the configured weekly draft day (Sunday by
    default), where it previews the **upcoming** week — matching the auto-posted
    draft. This is the fix for opening `/train` on a Sunday and getting the week
    that's ending instead of the one being planned (#304). Leadership can still
    step to any week with the view's ◀ / ▶ buttons."""
    cur = week_start_for(today)
    if today.weekday() == draft_day:
        return cur + timedelta(days=7)
    return cur


def _guild_today(bot, guild_id: int) -> date:
    """Today's date in the guild's configured timezone."""
    from config import get_config

    gcfg = get_config(guild_id)
    tz = ZoneInfo(gcfg.timezone if gcfg and gcfg.timezone else "America/New_York")
    return datetime.now(tz=tz).date()


# ── Shared helpers ────────────────────────────────────────────────────────────


def _is_leader(interaction: discord.Interaction) -> bool:
    from config import get_config

    cfg = get_config(interaction.guild_id)
    if not cfg:
        return False
    role_names = [r.name for r in getattr(interaction.user, "roles", [])]
    return cfg.leadership_role_name in role_names


def _short(name: str, width: int) -> str:
    name = name or ""
    return name if len(name) <= width else name[: width - 1] + "…"


MANUAL_LABEL = "✏️ Manual assignment"


def _conductor_cell(dd: tr.DraftDay) -> str:
    """The conductor column text for a draft day.

    A day with no conductor is one of two things:
      - a manual day (Manual / VS / Contest / Event with no role) where
        leadership assigns day-of and gets prompted, or
      - an auto/leadership day that couldn't resolve (empty roster/role) — the
        only case that shows the ⚠️ "requires selection" warning."""
    if dd.member:
        bday = " 🎂" if dd.reason == "birthday" else ""
        return f"{dd.member}{bday}"
    if dd.reason in tr.MANUAL_RULES:
        return MANUAL_LABEL
    return tr.NEEDS_PICKING_LABEL


def _resolve_roster_name(state: RotationState, typed: str) -> str:
    """Resolve a hand-typed name to the roster's canonical spelling.

    Exact (case-insensitive) match wins; else a unique substring match; else
    the typed string is used as-is so leadership can still assign someone not
    on the roster."""
    t = (typed or "").strip()
    if not t:
        return t
    tl = t.lower()
    for m in state.roster:
        if (m.get("name") or "").strip().lower() == tl:
            return m["name"]
    hits = [m["name"] for m in state.roster if tl in (m.get("name") or "").lower()]
    return hits[0] if len(hits) == 1 else t


# ══════════════════════════════════════════════════════════════════════════════
# Embeds
# ══════════════════════════════════════════════════════════════════════════════


def build_preset_editor_embed(preset: tr.SchedulePreset, *, dirty: bool) -> discord.Embed:
    lines = [f"{'Day':<10} {'Rule':<22} Specific member", "─" * 50]
    for wd in range(7):
        r = preset.rule_for(wd)
        rule_label = tr.RULE_LABELS.get(r.rule_type, r.rule_type)
        pinned = r.specific_member if r.rule_type == tr.RULE_SPECIFIC and r.specific_member else "-"
        lines.append(
            f"{tr.WEEKDAY_NAMES[wd]:<10} {_short(rule_label, 22):<22} {_short(pinned, 16)}"
        )
    body = "```\n" + "\n".join(lines) + "\n```"
    if dirty:
        body += "\n⚠️ **Unsaved changes.** Hit 💾 Save preset to commit."
    embed = discord.Embed(
        title=f"🚂 Editing Schedule Preset: {preset.name}",
        description=body,
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Pick a day to set its rule and (for Specific member) the pinned member.")
    return embed


def _conductor_mention(dd: tr.DraftDay) -> str:
    """The conductor as a Discord @mention when we know their ID, else their
    name (off-roster / hand-typed), else the manual / needs-picking marker.

    Mentions render as the person's real Discord account so leadership recognizes
    them; inside an embed they don't ping. Requires no code block — that's the
    whole point of the markdown draft list."""
    if dd.member:
        bday = " 🎂" if dd.reason == "birthday" else ""
        did = (dd.discord_id or "").strip()
        return (f"<@{did}>" if did else dd.member) + bday
    if dd.reason in tr.MANUAL_RULES:
        return "✏️ Manual"  # compact marker for the one-line draft list
    return tr.NEEDS_PICKING_LABEL


def _reason_subline(dd: tr.DraftDay) -> str:
    """A small indented line under the conductor showing why they're on this day
    (a leadership-typed reason, or a birthday offset). Empty when there's nothing
    useful to add — including a plain on-the-day birthday, whose 🎂 already shows
    on the conductor line."""
    note = (dd.note or "").strip()
    if not note or note == "needs picking":
        return ""
    if dd.reason == "birthday" and "(" not in note:
        return ""  # redundant with the 🎂 already on the conductor line
    return f"\n↳ *{note}*"


def build_weekly_draft_embed(
    draft: list[tr.DraftDay], week_start: date, preset_name: str
) -> discord.Embed:
    week_end = week_start + timedelta(days=6)
    # One markdown line per day (no code block) so conductors render as real
    # @mentions and nothing wraps mid-cell. `·` separates day · rule · conductor;
    # a typed reason (if any) follows on a small indented sub-line.
    lines = [
        f"**{date.fromisoformat(dd.date):%a %b} {date.fromisoformat(dd.date).day}** · "
        f"{tr.RULE_LABELS_SHORT.get(dd.rule_type, tr.RULE_LABELS.get(dd.rule_type, dd.rule_type))} · "
        f"{_conductor_mention(dd)}{_reason_subline(dd)}"
        for dd in draft
    ]
    embed = discord.Embed(
        title=f"🚂 Train Schedule: Week of {week_start:%a %b} {week_start.day} to {week_end:%a %b} {week_end.day}",
        description="\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.add_field(name="Preset", value=preset_name, inline=True)
    embed.set_footer(
        text="This draft is the schedule, so edit any day below. "
        "Each day's conductor is confirmed and posted on the day."
    )
    return embed


def build_daily_confirm_embed(dd: tr.DraftDay) -> discord.Embed:
    d = date.fromisoformat(dd.date)
    embed = discord.Embed(
        title=f"🚂 Today's Train: {d:%A, %B} {d.day}",
        color=discord.Color.gold(),
    )
    if dd.member:
        reason_label = tr.RULE_LABELS.get(dd.reason, dd.reason)
        bday = " 🎂" if dd.reason == "birthday" else ""
        embed.description = f"**Conductor:** {dd.member}{bday}\n*Reason: {reason_label}*"
        note = _reason_subline(dd)
        if note:
            embed.description += note
    elif dd.reason in tr.MANUAL_RULES:
        embed.description = f"{MANUAL_LABEL}. Pick today's conductor below."
    else:
        embed.description = tr.NEEDS_PICKING_LABEL
    embed.set_footer(text="Confirm today's conductor, or adjust it first.")
    return embed


def build_public_post_embed(
    dd: tr.DraftDay, *, blurb: str = "", image_url: str = ""
) -> discord.Embed:
    d = date.fromisoformat(dd.date)
    bday = " 🎂" if dd.reason == "birthday" else ""
    embed = discord.Embed(
        title="🚂 Today's Train Conductor",
        description=f"**{dd.member}**{bday}\n{d:%A, %B} {d.day}",
        color=discord.Color.gold(),
    )
    if blurb:
        embed.add_field(name="​", value=blurb[:1024], inline=False)
    if image_url:
        embed.set_image(url=image_url)
    return embed


LOGS_FOOTER = "Train counts exclude birthday / welcome / event trains by default."
PAGE_SIZE = 15  # rows per page in the View-all pager (keeps each field under 1024)


def _train_word(count: int) -> str:
    return "train" if count == 1 else "trains"


def _tally_line(name: str, count: int, last: str, *, rank: int | None = None) -> str:
    """One by-member row: name, train count, last-driven date (or 'never')."""
    when = f"last {last}" if last else "never"
    prefix = f"`{rank:>2}.` " if rank is not None else ""
    return f"{prefix}**{name}**: {count} {_train_word(count)} · {when}"


def _log_line(h) -> str:
    """One chronological log row: date, conductor, reason label."""
    reason = tr.RULE_LABELS.get(h.reason, h.reason)
    return f"✅ **{h.date}** · {h.member or '(none)'} · {reason}"


def build_assignment_logs_embed(
    tally: list, posted: list, *, top_n: int = 10, recent_n: int = 6
) -> discord.Embed:
    """Summary view of the assignment record: the most-assigned members (spot
    anyone getting too many trains), the fewest-assigned (verify nobody's being
    skipped, including roster members who've driven zero times), and the recent
    chronological log. `tally` comes from `tr.member_tally`; `posted` is the
    posted-status history rows. Merges the old History + Rotation-balance views.

    The full, paged, sortable record lives behind the View-all button
    (`AssignmentLogsView`); this is the at-a-glance top."""
    embed = discord.Embed(title="🚂 Train Assignment Logs", color=discord.Color.gold())

    if not tally and not posted:
        embed.description = "*No assignments logged yet. Confirmed conductors appear here.*"
        return embed

    most = tr.sort_tally(tally, tr.TALLY_SORT_MOST)[:top_n]
    fewest = tr.sort_tally(tally, tr.TALLY_SORT_FEWEST)[:top_n]
    embed.add_field(
        name="🔝 Most trains",
        value="\n".join(_tally_line(n, c, l, rank=i + 1) for i, (n, c, l) in enumerate(most))[:1024]
        or "*none yet*",
        inline=False,
    )
    embed.add_field(
        name="🔻 Fewest trains",
        value="\n".join(_tally_line(n, c, l, rank=i + 1) for i, (n, c, l) in enumerate(fewest))[
            :1024
        ]
        or "*none yet*",
        inline=False,
    )

    recent = tr.sort_posted(posted)[:recent_n]
    embed.add_field(
        name="🕒 Most recent",
        value="\n".join(_log_line(h) for h in recent)[:1024] or "*No trains posted yet.*",
        inline=False,
    )
    embed.set_footer(text=f"{LOGS_FOOTER} {len(tally)} conductor(s) tracked.")
    return embed


def build_history_page_embed(
    tally: list, posted: list, *, mode: str, sort_key: str, page: int
) -> discord.Embed:
    """One page of the full, sortable record. `mode` is 'member' (the by-member
    tally) or 'date' (the chronological log); `sort_key` selects the ordering;
    `page` is 0-based. Used by AssignmentLogsView's pager."""
    if mode == "date":
        rows = tr.sort_posted(posted, newest_first=(sort_key != "oldest"))
        sort_label = "Oldest first" if sort_key == "oldest" else "Newest first"
        mode_label = "By date"
    else:
        rows = tr.sort_tally(tally, sort_key)
        sort_label = _SORT_LABELS.get(sort_key, "Most trains")
        mode_label = "By member"

    total_pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    window = rows[start : start + PAGE_SIZE]

    embed = discord.Embed(title="🚂 Train History", color=discord.Color.gold())
    header = f"**{mode_label}** · Sorted by {sort_label} · Page {page + 1} of {total_pages}"
    if not rows:
        embed.description = f"{header}\n\n*Nothing logged yet.*"
        return embed

    if mode == "date":
        body = "\n".join(_log_line(h) for h in window)
    else:
        body = "\n".join(
            _tally_line(n, c, l, rank=start + i + 1) for i, (n, c, l) in enumerate(window)
        )
    embed.description = f"{header}\n\n{body}"[:4000]
    embed.set_footer(text=LOGS_FOOTER)
    return embed


# Sort-dropdown option labels, keyed by sort key.
_SORT_LABELS = {
    tr.TALLY_SORT_MOST: "Most trains",
    tr.TALLY_SORT_FEWEST: "Fewest trains",
    tr.TALLY_SORT_LONGEST_SINCE: "Longest since a train",
    tr.TALLY_SORT_NAME: "Name A-Z",
}
_MEMBER_SORTS = [
    tr.TALLY_SORT_MOST,
    tr.TALLY_SORT_FEWEST,
    tr.TALLY_SORT_LONGEST_SINCE,
    tr.TALLY_SORT_NAME,
]
_DATE_SORTS = [("newest", "Newest first"), ("oldest", "Oldest first")]


class AssignmentLogsView(discord.ui.View):
    """Owner-locked, ephemeral Assignment Logs surface. Opens on the summary
    (most / fewest / recent); the View-all button swaps the same message into a
    paged, sortable history that toggles between a by-member tally and the
    chronological log. All paging/sorting is in-memory over the data captured at
    open time, so no Sheet re-reads on a click.

    Modes: 'summary' | 'member' | 'date'."""

    BTN_VIEW_ALL = "📜 View all history"
    BTN_BY_MEMBER = "👥 By member"
    BTN_BY_DATE = "🗓️ By date"
    BTN_PREV = "◀️ Prev"
    BTN_NEXT = "▶️ Next"
    BTN_BACK = "🔙 Back"

    def __init__(self, owner_id: int, tally: list, posted: list):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.tally = tally
        self.posted = posted
        self.message = None
        self.mode = "summary"
        self.page = 0
        self.sort_member = tr.TALLY_SORT_MOST
        self.sort_date = "newest"
        self._sync()

    async def interaction_check(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message(DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    # ── rendering ──────────────────────────────────────────────────────────────

    def render_embed(self) -> discord.Embed:
        if self.mode == "summary":
            return build_assignment_logs_embed(self.tally, self.posted)
        sort_key = self.sort_date if self.mode == "date" else self.sort_member
        return build_history_page_embed(
            self.tally, self.posted, mode=self.mode, sort_key=sort_key, page=self.page
        )

    def _total_pages(self) -> int:
        n = len(self.posted) if self.mode == "date" else len(self.tally)
        return max(1, (n + PAGE_SIZE - 1) // PAGE_SIZE)

    def _sync(self):
        """Rebuild the component set for the current mode."""
        self.clear_items()
        if self.mode == "summary":
            if self.tally or self.posted:
                self._button(self.BTN_VIEW_ALL, discord.ButtonStyle.primary, 0, self._on_view_all)
            return

        # Pager modes: mode toggle, sort select, prev/next/back.
        self._button(
            self.BTN_BY_MEMBER,
            discord.ButtonStyle.primary if self.mode == "member" else discord.ButtonStyle.secondary,
            0,
            self._on_by_member,
            disabled=(self.mode == "member"),
        )
        self._button(
            self.BTN_BY_DATE,
            discord.ButtonStyle.primary if self.mode == "date" else discord.ButtonStyle.secondary,
            0,
            self._on_by_date,
            disabled=(self.mode == "date"),
        )
        self._add_sort_select()
        total = self._total_pages()
        self._button(
            self.BTN_PREV,
            discord.ButtonStyle.secondary,
            2,
            self._on_prev,
            disabled=(self.page <= 0),
        )
        self._button(
            self.BTN_NEXT,
            discord.ButtonStyle.secondary,
            2,
            self._on_next,
            disabled=(self.page >= total - 1),
        )
        self._button(self.BTN_BACK, discord.ButtonStyle.secondary, 2, self._on_back)

    def _button(self, label, style, row, cb, *, disabled=False):
        btn = discord.ui.Button(label=label, style=style, row=row, disabled=disabled)
        btn.callback = cb
        self.add_item(btn)

    def _add_sort_select(self):
        if self.mode == "date":
            opts = [
                discord.SelectOption(label=lbl, value=val, default=(val == self.sort_date))
                for val, lbl in _DATE_SORTS
            ]
        else:
            opts = [
                discord.SelectOption(
                    label=_SORT_LABELS[k], value=k, default=(k == self.sort_member)
                )
                for k in _MEMBER_SORTS
            ]
        sel = discord.ui.Select(placeholder="Sort…", options=opts, row=1)
        sel.callback = self._on_sort
        self.add_item(sel)

    async def _refresh(self, inter: discord.Interaction):
        self._sync()
        await inter.response.edit_message(embed=self.render_embed(), view=self)

    # ── callbacks ──────────────────────────────────────────────────────────────

    async def _on_view_all(self, inter):
        self.mode = "member"
        self.page = 0
        await self._refresh(inter)

    async def _on_by_member(self, inter):
        self.mode = "member"
        self.page = 0
        await self._refresh(inter)

    async def _on_by_date(self, inter):
        self.mode = "date"
        self.page = 0
        await self._refresh(inter)

    async def _on_sort(self, inter):
        value = inter.data["values"][0]
        if self.mode == "date":
            self.sort_date = value
        else:
            self.sort_member = value
        self.page = 0
        await self._refresh(inter)

    async def _on_prev(self, inter):
        self.page = max(0, self.page - 1)
        await self._refresh(inter)

    async def _on_next(self, inter):
        self.page = min(self._total_pages() - 1, self.page + 1)
        await self._refresh(inter)

    async def _on_back(self, inter):
        self.mode = "summary"
        self.page = 0
        await self._refresh(inter)


# ══════════════════════════════════════════════════════════════════════════════
# Member-name modal (assign / pin) — text input resolves against the roster, so
# it works for any alliance size (a 25-option select can't hold a real roster).
# ══════════════════════════════════════════════════════════════════════════════


class _MemberNameModal(discord.ui.Modal):
    def __init__(self, title: str, on_name, *, current: str = ""):
        super().__init__(title=title[:45])
        self._on_name = on_name
        self.name_input = discord.ui.TextInput(
            label="Member name",
            placeholder="Type the conductor's name",
            default=current or "",
            required=True,
            max_length=80,
        )
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        await self._on_name(interaction, self.name_input.value.strip())


def _assign_pool_for_day(state: "RotationState", rule_type: str) -> tuple[list[str], str]:
    """Names to offer when assigning a conductor by hand, plus a scope note.

    A role-scoped day (Leadership / VS / Contest / Event with an assigned role)
    offers just that role's members — what leadership expects when they pick a
    day tied to a role, rather than the whole roster. Every other day offers the
    full roster. Either way the picker's **Type a name instead** still covers
    anyone off the list."""
    pool = state.role_pools.get(rule_type)
    if pool:
        names = list(dict.fromkeys(pool))  # dedupe, preserve order
        return names, f"\nShowing the **{tr.RULE_LABELS.get(rule_type, rule_type)}** pool."
    return tr.roster_names(state.roster), ""


def _id_for_name(state: "RotationState", name: str) -> str:
    """The roster's Discord ID for a conductor name (for @mention rendering in
    the draft embed), or "" when off-roster / unknown."""
    key = tr._norm(name or "")
    if not key:
        return ""
    for m in state.roster:
        if tr._norm(m.get("name") or "") == key:
            return str(m.get("discord_id") or "")
    return ""


def _resolve_name_from_list(names: list[str], typed: str) -> str:
    """Resolve a hand-typed name against a known name list — exact match wins,
    then a unique substring match, else the typed string as-is (so leadership can
    still assign someone off-roster). The list-based sibling of
    `_resolve_roster_name`, for callers that already hold the roster names."""
    t = (typed or "").strip()
    if not t:
        return t
    tl = t.lower()
    for n in names:
        if n.lower() == tl:
            return n
    hits = [n for n in names if tl in n.lower()]
    return hits[0] if len(hits) == 1 else t


class _RosterPickerView(discord.ui.View):
    """Reusable roster-backed conductor picker (dropdown + Save / Cancel / Type a
    name instead), shown ephemerally wherever leadership assigns someone by hand.

    The dropdown only stages a *pending* choice (Discord won't re-fire the change
    event when you re-pick the already-selected member), so **💾 Save** commits.
    On commit it calls `on_commit(name)` — which owns updating the parent view's
    message — and never touches the parent interaction, so the parent refresh and
    this picker's own ack stay independent. An empty roster collapses to just
    Cancel + Type a name instead, preserving the off-roster path."""

    PAGE = 25

    def __init__(
        self,
        names: list[str],
        *,
        current: str,
        prompt: str,
        modal_title: str,
        on_commit,
        full_names: list[str] | None = None,
        scope: str = "",
        full_scope: str = "",
    ):
        super().__init__(timeout=180)
        self._names = names
        # A real toggle target only when the full roster is a different (larger)
        # set than the filtered list — otherwise there's nothing to switch to.
        self._full_names = full_names if (full_names and full_names != names) else None
        self.scope = scope  # note shown for the filtered list (e.g. the role pool)
        self.full_scope = full_scope  # note shown once switched to the full roster
        self.prompt = prompt
        self.modal_title = modal_title
        self.on_commit = on_commit  # async (name: str) -> None
        self.page = 0
        self.showing_full = False
        self.pending = current or ""
        self._build()

    @property
    def names(self) -> list[str]:
        """The active list — the full roster once toggled, else the filtered one."""
        return self._full_names if (self.showing_full and self._full_names) else self._names

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.names) + self.PAGE - 1) // self.PAGE)

    def content(self) -> str:
        base = f"{self.prompt}{self.full_scope if self.showing_full else self.scope}"
        if self.pending:
            return f"{base}\nSelected: **{self.pending}** — hit **💾 Save** to confirm."
        if self.names:
            return f"{base}\nPick a member, then **💾 Save**."
        return f"{base}\nNo roster is set up — use **✏️ Type a name instead**."

    def _build(self):
        self.clear_items()
        if self.names:
            start = self.page * self.PAGE
            page_names = self.names[start : start + self.PAGE]
            sel = discord.ui.Select(
                placeholder="Pick the member…",
                options=[
                    discord.SelectOption(label=n[:100], value=n[:100], default=(n == self.pending))
                    for n in page_names
                ],
                row=0,
            )
            sel.callback = self._on_select
            self.add_item(sel)
            if self.total_pages > 1:
                prev = discord.ui.Button(
                    label="◀ Prev",
                    style=discord.ButtonStyle.secondary,
                    disabled=self.page == 0,
                    row=1,
                )
                prev.callback = self._prev
                self.add_item(prev)
                nxt = discord.ui.Button(
                    label="Next ▶",
                    style=discord.ButtonStyle.secondary,
                    disabled=self.page >= self.total_pages - 1,
                    row=1,
                )
                nxt.callback = self._next
                self.add_item(nxt)
            save = discord.ui.Button(label="💾 Save", style=discord.ButtonStyle.success, row=2)
            save.callback = self._on_save
            self.add_item(save)
        cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary, row=2)
        cancel.callback = self._on_cancel
        self.add_item(cancel)
        # Pop out to the full roster (and back) when a role filter is in effect.
        if self._full_names:
            tog = discord.ui.Button(
                label="🔁 Show role only" if self.showing_full else "🔁 Show full roster",
                style=discord.ButtonStyle.secondary,
                row=2,
            )
            tog.callback = self._on_toggle
            self.add_item(tog)
        typ = discord.ui.Button(
            label="✏️ Type a name instead", style=discord.ButtonStyle.secondary, row=2
        )
        typ.callback = self._on_type
        self.add_item(typ)

    async def _on_toggle(self, interaction: discord.Interaction):
        self.showing_full = not self.showing_full
        self.page = 0
        self._build()
        await interaction.response.edit_message(content=self.content(), view=self)

    async def _on_select(self, interaction: discord.Interaction):
        self.pending = interaction.data["values"][0]
        self._build()
        await interaction.response.edit_message(content=self.content(), view=self)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._build()
        await interaction.response.edit_message(content=self.content(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.total_pages - 1, self.page + 1)
        self._build()
        await interaction.response.edit_message(content=self.content(), view=self)

    async def _on_save(self, interaction: discord.Interaction):
        if not self.pending:
            await interaction.response.send_message(
                "Pick a member first, or use **✏️ Type a name instead**.", ephemeral=True
            )
            return
        name = self.pending
        self.stop()
        try:
            await interaction.response.edit_message(content=f"✅ Assigned **{name}**.", view=None)
        except discord.HTTPException:
            pass
        await self.on_commit(name)

    async def _on_cancel(self, interaction: discord.Interaction):
        self.stop()
        try:
            await interaction.response.edit_message(
                content="Cancelled — nothing changed.", view=None
            )
        except discord.HTTPException:
            pass

    async def _on_type(self, interaction: discord.Interaction):
        self.stop()

        async def _typed(inter: discord.Interaction, typed: str):
            await inter.response.defer()  # ack the modal; on_commit owns the refresh
            # Resolve against the widest pool so a typed name matches even when the
            # role filter is showing.
            await self.on_commit(_resolve_name_from_list(self._full_names or self._names, typed))

        await interaction.response.send_modal(
            _MemberNameModal(self.modal_title, _typed, current=self.pending or "")
        )


def resolve_birthday_mode(guild_id: int) -> str:
    """Derive the rotation birthday mode from the Birthday setup (#55, Kevin):
    `override` when birthdays are enabled AND wired to trains
    (`train_integration`), otherwise `disabled`. There is no separate
    train-rotation birthday toggle — it follows the Birthday config."""
    from config import get_birthday_config

    bcfg = get_birthday_config(guild_id)
    if bcfg.get("enabled") and bcfg.get("train_integration"):
        return tr.BIRTHDAY_OVERRIDE
    return tr.BIRTHDAY_DISABLED


def regenerate_week(
    bot, guild_id: int, week_start: date, is_premium: bool = True
) -> list[tr.DraftDay]:
    """Generate a fresh draft for the week and persist it as scheduled rows.
    Blocking — call via asyncio.to_thread (or `regenerate_week_async`, which
    resolves `is_premium` for you). `is_premium` gates role-scoped day rules
    (#337)."""
    from config import get_train_config

    state = load_rotation_state(bot, guild_id, is_premium=is_premium)
    cfg = get_train_config(guild_id)
    preset = tr.load_preset(
        guild_id,
        cfg.get("day_rules_tab") or "",
        cfg.get("active_schedule_preset") or tr.DEFAULT_PRESET_NAME,
    ) or tr.SchedulePreset.default(cfg.get("active_schedule_preset") or tr.DEFAULT_PRESET_NAME)

    birthday_mode = resolve_birthday_mode(guild_id)
    birthdays = {}
    if birthday_mode == tr.BIRTHDAY_OVERRIDE:
        from train_birthdays import birthday_lookup_for_dates

        week_dates = [week_start + timedelta(days=i) for i in range(7)]
        birthdays = birthday_lookup_for_dates(week_dates, guild_id)

    draft = tr.generate_week_draft(
        preset,
        week_start,
        eligible_pool=state.eligible_pool,
        role_pools=state.role_pools,
        member_rules=state.member_rules,
        history=state.history,
        counted_reasons=state.counted_reasons,
        birthday_mode=birthday_mode,
        birthdays_on_date=birthdays,
        role_rules_enabled=state.role_rules_enabled,
    )
    # Stamp each conductor's Discord ID from the roster so the draft embed can
    # render them as @mentions.
    id_by_name = {
        tr._norm(m["name"]): str(m.get("discord_id") or "") for m in state.roster if m.get("name")
    }
    for dd in draft:
        if dd.member:
            dd.discord_id = id_by_name.get(tr._norm(dd.member), "")
    tr.write_draft_rows(guild_id, cfg.get("history_tab") or "", draft)
    return draft


def load_week_draft(
    bot, guild_id: int, week_start: date, is_premium: bool = True
) -> list[tr.DraftDay]:
    """Reconstruct the current week's draft from the scheduled history rows, so
    `/train draft_week` can reopen an editable view without re-rolling. Falls
    back to generating a fresh draft when no scheduled rows exist for the week.
    Blocking — call via asyncio.to_thread (or `load_week_draft_async`, which
    resolves `is_premium` for you). `is_premium` only matters on the
    regenerate fallback, where it gates role-scoped day rules (#337)."""
    from config import get_train_config

    cfg = get_train_config(guild_id)
    history = tr.load_history(guild_id, cfg.get("history_tab") or "")
    week_isos = {(week_start + timedelta(days=i)).isoformat() for i in range(7)}
    # Honour scheduled (still editable) + posted (already confirmed) rows so a
    # reopened draft reflects reality instead of reverting to needs-picking.
    relevant = (tr.STATUS_SCHEDULED, tr.STATUS_POSTED)
    rows = {h.date: h for h in history if h.date in week_isos and h.status in relevant}
    if not rows:
        return regenerate_week(bot, guild_id, week_start, is_premium=is_premium)
    draft = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        iso = d.isoformat()
        h = rows.get(iso)
        if h is None:
            draft.append(
                tr.DraftDay(
                    date=iso,
                    weekday=d.weekday(),
                    rule_type=tr.RULE_AUTO,
                    member=None,
                    reason="auto",
                    needs_picking=True,
                    note="",
                )
            )
        else:
            draft.append(
                tr.DraftDay(
                    date=iso,
                    weekday=d.weekday(),
                    rule_type=h.reason if h.reason in tr.RULE_LABELS else tr.RULE_AUTO,
                    member=h.member or None,
                    reason=h.reason,
                    needs_picking=not bool(h.member),
                    # Scrub the legacy "needs picking" sentinel older sheets may
                    # still carry in the Notes column so it never renders as a reason.
                    note="" if h.notes == "needs picking" else h.notes,
                    discord_id=h.discord_id,  # carried for the @mention render
                )
            )
    return draft


# ══════════════════════════════════════════════════════════════════════════════
# Re-exports (#373 split) — TrainPresetEditorView/open_preset_editor* live in
# train_rotation_ui_presets.py; WeeklyDraftView in train_rotation_ui_draft.py;
# DailyConfirmView in train_rotation_ui_confirm.py. Re-exported here so every
# existing `import train_rotation_ui as ui` call site (train_cog.py,
# train_hub.py, setup_cog.py) keeps working unchanged. Matches this repo's
# existing re-export convention (see train.py re-exporting from
# train_birthdays.py) — ruff's F401 is deliberately off repo-wide for exactly
# this pattern.
# ══════════════════════════════════════════════════════════════════════════════

from train_rotation_ui_presets import (
    TrainPresetEditorView,
    open_preset_editor,
    open_preset_editor_followup,
    post_preset_editor,
)
from train_rotation_ui_draft import WeeklyDraftView
from train_rotation_ui_confirm import DailyConfirmView
