"""
train_rotation_ui.py — Discord UI for Train Conductor Rotation (#55).

Three surfaces, plus shared embed builders and a runtime state loader:

- **TrainPresetEditorView** — a live, owner-locked editor for a schedule
  preset. Pick a day from a dropdown, set its rule with another dropdown,
  Save. (The issue mocked dropdowns *inside* a modal; Discord modals
  only hold text inputs, so the editor is a single live message instead.)
- **WeeklyDraftView** — the Sunday draft posted to leadership. A day picker
  plus Next / Assign / Skip / Regenerate buttons act on the chosen day. (The
  issue mocked per-day button rows; a 7-day × 3-button grid exceeds Discord's
  5-action-row cap, so it's a day-select + shared buttons.)
- **DailyConfirmView** — each drive day's confirmation. Confirm posts the
  conductor publicly (blurb + optional image URL — modals can't upload files).

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
    ):
        self.cfg = cfg
        self.roster = roster
        self.eligible_pool = eligible_pool
        self.role_pools = role_pools
        self.member_rules = member_rules
        self.history = history
        self.counted_reasons = counted_reasons


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


def load_rotation_state(bot, guild_id: int) -> RotationState:
    """Load config + roster + rules + history into a RotationState.

    Does the (blocking) Sheet reads; callers in async contexts should wrap this
    in `asyncio.to_thread`. Per-rule-type role pools are resolved against the
    roster by Discord ID so their names match Train History."""
    from config import get_train_config

    cfg = get_train_config(guild_id)
    roster = tr.load_roster_members(guild_id)
    eligible_pool = tr.roster_names(roster)

    guild = bot.get_guild(guild_id)
    role_pools: dict[str, list[str]] = {}
    # Explicit per-rule-type role assignments (#55). cfg["rule_type_roles"] is a
    # {rule_type: role_id} dict (parsed in config.get_train_config).
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
    history = tr.load_history(guild_id, cfg.get("history_tab") or "")
    counted = tr.parse_counted_reasons(cfg.get("counted_reasons"))

    return RotationState(
        cfg=cfg,
        roster=roster,
        eligible_pool=eligible_pool,
        role_pools=role_pools,
        member_rules=member_rules,
        history=history,
        counted_reasons=counted,
    )


def week_start_for(d: date) -> date:
    """The Monday on or before `d` (weekday() 0 = Monday)."""
    return d - timedelta(days=d.weekday())


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


def build_weekly_draft_embed(
    draft: list[tr.DraftDay], week_start: date, preset_name: str
) -> discord.Embed:
    week_end = week_start + timedelta(days=6)
    lines = []
    for dd in draft:
        d = date.fromisoformat(dd.date)
        rule_label = tr.RULE_LABELS.get(dd.rule_type, dd.rule_type)
        lines.append(
            f"{d:%a} {d:%b} {d.day:<2}  {_short(rule_label, 20):<20}  {_conductor_cell(dd)}"
        )
    body = "```\n" + "\n".join(lines) + "\n```"
    embed = discord.Embed(
        title=f"🚂 Train Schedule: Week of {week_start:%a %b} {week_start.day} to {week_end:%a %b} {week_end.day}",
        description=body,
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


# ══════════════════════════════════════════════════════════════════════════════
# Preset editor
# ══════════════════════════════════════════════════════════════════════════════


class TrainPresetEditorView(discord.ui.View):
    """Owner-locked live editor for one schedule preset. State held in
    `self.preset`; persisted to the Day Rules tab on Save."""

    def __init__(self, guild_id: int, user_id: int, preset: tr.SchedulePreset, day_rules_tab: str):
        super().__init__(timeout=EDITOR_TIMEOUT)
        self.guild_id = guild_id
        self.user_id = user_id
        self.preset = preset
        self.day_rules_tab = day_rules_tab
        self.dirty = False
        self.editing_day: int | None = None
        self.message: discord.Message | None = None
        self._rebuild()

    # ── component assembly ────────────────────────────────────────────────────

    def _rebuild(self):
        self.clear_items()

        day_select = discord.ui.Select(
            placeholder="📅 Pick a day to edit…",
            options=[
                discord.SelectOption(
                    label=tr.WEEKDAY_NAMES[wd],
                    value=str(wd),
                    description=tr.RULE_LABELS.get(self.preset.rule_for(wd).rule_type, "")[:100],
                    default=self.editing_day == wd,
                )
                for wd in range(7)
            ],
        )
        day_select.callback = self._on_day
        self.add_item(day_select)

        if self.editing_day is not None:
            cur = self.preset.rule_for(self.editing_day)

            rule_select = discord.ui.Select(
                placeholder="Rule type…",
                options=[
                    discord.SelectOption(
                        label=tr.RULE_LABELS[rt],
                        value=rt,
                        default=cur.rule_type == rt,
                    )
                    for rt in tr.DAY_RULE_TYPES
                ],
            )
            rule_select.callback = self._on_rule
            self.add_item(rule_select)

        # Action buttons. "Set specific member" only shows when the selected
        # day's rule is Specific member (the only rule that takes a pin).
        if (
            self.editing_day is not None
            and self.preset.rule_for(self.editing_day).rule_type == tr.RULE_SPECIFIC
        ):
            pin_btn = discord.ui.Button(
                label="✏️ Set specific member", style=discord.ButtonStyle.secondary
            )
            pin_btn.callback = self._on_set_pin
            self.add_item(pin_btn)

        save_btn = discord.ui.Button(
            label="💾 Save preset", style=discord.ButtonStyle.success, disabled=not self.dirty
        )
        save_btn.callback = self._on_save
        self.add_item(save_btn)

        abandon_btn = discord.ui.Button(label="🔙 Abandon", style=discord.ButtonStyle.danger)
        abandon_btn.callback = self._on_abandon
        self.add_item(abandon_btn)

    async def _guard(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction, *, content: str | None = None):
        self._rebuild()
        embed = build_preset_editor_embed(self.preset, dirty=self.dirty)
        try:
            if interaction.response.is_done():
                if self.message:
                    await self.message.edit(content=content, embed=embed, view=self)
            else:
                await interaction.response.edit_message(content=content, embed=embed, view=self)
        except discord.HTTPException:
            pass

    # ── callbacks ─────────────────────────────────────────────────────────────

    async def _on_day(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        self.editing_day = int(interaction.data["values"][0])
        await self._refresh(interaction)

    async def _on_rule(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        rt = interaction.data["values"][0]
        rule = self.preset.rule_for(self.editing_day)
        rule.rule_type = rt
        if rt != tr.RULE_SPECIFIC:
            rule.specific_member = ""
        self.preset.days[self.editing_day] = rule
        self.dirty = True
        await self._refresh(interaction)

    async def _on_set_pin(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        day_name = tr.WEEKDAY_NAMES[self.editing_day]
        cur = self.preset.rule_for(self.editing_day).specific_member

        async def _save_name(inter: discord.Interaction, name: str):
            rule = self.preset.rule_for(self.editing_day)
            rule.specific_member = name
            self.preset.days[self.editing_day] = rule
            self.dirty = True
            await self._refresh(
                inter,
                content=f"📌 **{name}** will drive the train every **{day_name}**.",
            )

        await interaction.response.send_modal(
            _MemberNameModal(f"Specific member for every {day_name}", _save_name, current=cur)
        )

    async def _on_save(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        await interaction.response.defer()
        ok = await asyncio.to_thread(tr.save_preset, self.guild_id, self.day_rules_tab, self.preset)
        if ok:
            self.dirty = False
            for item in self.children:
                item.disabled = True
            try:
                await interaction.followup.send(
                    f"✅ Saved preset **{self.preset.name}**.", ephemeral=False
                )
            except discord.HTTPException:
                pass
            if self.message:
                try:
                    await self.message.edit(
                        embed=build_preset_editor_embed(self.preset, dirty=False), view=self
                    )
                except discord.HTTPException:
                    pass
            self.stop()
        else:
            await interaction.followup.send(
                "⚠️ Couldn't save the preset. Check that your Google Sheet is configured "
                "and the bot has edit access.",
                ephemeral=True,
            )

    async def _on_abandon(self, interaction: discord.Interaction):
        if not await self._guard(interaction):
            return
        for item in self.children:
            item.disabled = True
        try:
            await interaction.response.edit_message(
                content="🔙 Abandoned. Changes were not saved.",
                embed=build_preset_editor_embed(self.preset, dirty=False),
                view=self,
            )
        except discord.HTTPException:
            pass
        self.stop()

    async def on_timeout(self):
        await wizard_registry.expire_view_message(
            self.message, command_hint="/train schedule_preset edit"
        )


async def open_preset_editor(
    interaction: discord.Interaction, preset: tr.SchedulePreset, day_rules_tab: str
):
    """Send a fresh editor as the interaction's initial response."""
    view = TrainPresetEditorView(interaction.guild_id, interaction.user.id, preset, day_rules_tab)
    embed = build_preset_editor_embed(preset, dirty=False)
    await interaction.response.send_message(embed=embed, view=view)
    try:
        view.message = await interaction.original_response()
    except discord.HTTPException:
        view.message = None
    return view


async def open_preset_editor_followup(
    interaction: discord.Interaction, preset: tr.SchedulePreset, day_rules_tab: str
):
    """Send a fresh editor as a followup (when the response was already used,
    e.g. from the setup wizard)."""
    view = TrainPresetEditorView(interaction.guild_id, interaction.user.id, preset, day_rules_tab)
    embed = build_preset_editor_embed(preset, dirty=False)
    view.message = await interaction.followup.send(embed=embed, view=view)
    return view


async def post_preset_editor(
    channel, guild_id: int, user_id: int, preset: tr.SchedulePreset, day_rules_tab: str
):
    """Post a fresh editor straight to a channel.

    Used at the end of the setup wizard, which runs long enough that the
    original interaction token has likely expired — so the editor is sent with
    `channel.send` rather than an interaction response/followup."""
    view = TrainPresetEditorView(guild_id, user_id, preset, day_rules_tab)
    embed = build_preset_editor_embed(preset, dirty=False)
    view.message = await channel.send(embed=embed, view=view)
    return view


# ══════════════════════════════════════════════════════════════════════════════
# Weekly draft
# ══════════════════════════════════════════════════════════════════════════════


class WeeklyDraftView(discord.ui.View):
    """Leadership-facing weekly draft. A day picker + shared action buttons edit
    the selected day; edits write straight to the Train History `scheduled`
    rows (the draft IS the schedule — no approve step)."""

    def __init__(
        self, bot, guild_id: int, draft: list[tr.DraftDay], week_start: date, preset_name: str
    ):
        super().__init__(timeout=EDITOR_TIMEOUT)
        self.bot = bot
        self.guild_id = guild_id
        self.draft = draft
        self.week_start = week_start
        self.preset_name = preset_name
        self.selected_iso: str | None = None
        self.message: discord.Message | None = None
        self._rebuild()

    def _by_iso(self, iso: str) -> tr.DraftDay:
        return next(d for d in self.draft if d.date == iso)

    def _rebuild(self):
        self.clear_items()
        day_select = discord.ui.Select(
            placeholder="📅 Pick a day to adjust…",
            options=[
                discord.SelectOption(
                    label=f"{date.fromisoformat(dd.date):%a %b} {date.fromisoformat(dd.date).day}",
                    value=dd.date,
                    description=_short(_conductor_cell(dd), 100),
                    default=self.selected_iso == dd.date,
                )
                for dd in self.draft
            ],
        )
        day_select.callback = self._on_day
        self.add_item(day_select)

        # Labels spell out exactly what happens to the picked day.
        for label, style, cb in [
            ("⏭️ Go to next person", discord.ButtonStyle.primary, self._on_next),
            ("✏️ Assign someone", discord.ButtonStyle.secondary, self._on_assign),
            ("✋ Set to manual", discord.ButtonStyle.secondary, self._on_set_manual),
            ("🔄 Re-draft the whole week", discord.ButtonStyle.danger, self._on_regen),
        ]:
            btn = discord.ui.Button(label=label, style=style)
            btn.callback = cb
            self.add_item(btn)

    async def _guard_day(self, interaction: discord.Interaction) -> tr.DraftDay | None:
        if not _is_leader(interaction):
            await interaction.response.send_message(DENY_NOT_LEADER, ephemeral=True)
            return None
        if not self.selected_iso:
            await interaction.response.send_message(
                "ℹ️ Pick a day from the dropdown first.", ephemeral=True
            )
            return None
        return self._by_iso(self.selected_iso)

    async def _refresh(self, interaction: discord.Interaction):
        self._rebuild()
        embed = build_weekly_draft_embed(self.draft, self.week_start, self.preset_name)
        try:
            if interaction.response.is_done():
                if self.message:
                    await self.message.edit(embed=embed, view=self)
            else:
                await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            pass

    def _persist_day(self, dd: tr.DraftDay):
        from config import get_train_config

        tab = get_train_config(self.guild_id).get("history_tab") or ""
        tr.set_day_status(
            self.guild_id,
            tab,
            dd.date,
            member=dd.member or "",
            reason=dd.reason,
            status=tr.STATUS_SCHEDULED,
            notes=dd.note,
        )

    async def _on_day(self, interaction: discord.Interaction):
        if not _is_leader(interaction):
            await interaction.response.send_message(DENY_NOT_LEADER, ephemeral=True)
            return
        self.selected_iso = interaction.data["values"][0]
        await self._refresh(interaction)

    async def _on_next(self, interaction: discord.Interaction):
        dd = await self._guard_day(interaction)
        if dd is None:
            return
        await interaction.response.defer()
        state = await asyncio.to_thread(load_rotation_state, self.bot, self.guild_id)
        other = {tr._norm(d.member) for d in self.draft if d.member and d.date != dd.date}
        member, reason, needs = tr.reroll_day(
            dd,
            eligible_pool=state.eligible_pool,
            role_pools=state.role_pools,
            member_rules=state.member_rules,
            history=state.history,
            counted_reasons=state.counted_reasons,
            other_scheduled=other,
            target_date=date.fromisoformat(dd.date),
        )
        dd.member, dd.reason, dd.needs_picking = member, reason, needs
        dd.note = "" if member else "needs picking"
        await asyncio.to_thread(self._persist_day, dd)
        await self._refresh(interaction)

    async def _on_assign(self, interaction: discord.Interaction):
        dd = await self._guard_day(interaction)
        if dd is None:
            return

        async def _assign_name(inter: discord.Interaction, typed: str):
            await inter.response.defer()
            state = await asyncio.to_thread(load_rotation_state, self.bot, self.guild_id)
            dd.member = _resolve_roster_name(state, typed)
            dd.reason = "manual"
            dd.needs_picking = False
            dd.note = ""
            await asyncio.to_thread(self._persist_day, dd)
            await self._refresh(inter)

        await interaction.response.send_modal(
            _MemberNameModal("Assign conductor", _assign_name, current=dd.member or "")
        )

    async def _on_set_manual(self, interaction: discord.Interaction):
        # Leave the day for leadership to assign on the day (they get prompted by
        # the daily confirmation). Shows "Manual assignment" in the draft.
        dd = await self._guard_day(interaction)
        if dd is None:
            return
        await interaction.response.defer()
        dd.member = None
        dd.reason = tr.RULE_MANUAL
        dd.needs_picking = True
        dd.note = ""
        await asyncio.to_thread(self._persist_day, dd)
        await self._refresh(interaction)

    async def _on_regen(self, interaction: discord.Interaction):
        # Re-drafting throws away every current pick (including ones set by
        # hand), so confirm first.
        if not _is_leader(interaction):
            await interaction.response.send_message(DENY_NOT_LEADER, ephemeral=True)
            return
        confirm = discord.ui.View(timeout=60)
        yes = discord.ui.Button(label="🔄 Yes, re-draft", style=discord.ButtonStyle.danger)
        no = discord.ui.Button(label="↩️ Keep current draft", style=discord.ButtonStyle.secondary)

        async def _do(ci: discord.Interaction):
            await ci.response.defer()
            self.draft = await asyncio.to_thread(
                regenerate_week, self.bot, self.guild_id, self.week_start
            )
            self._rebuild()
            if self.message:
                try:
                    await self.message.edit(
                        embed=build_weekly_draft_embed(
                            self.draft, self.week_start, self.preset_name
                        ),
                        view=self,
                    )
                except discord.HTTPException:
                    pass
            for c in confirm.children:
                c.disabled = True
            try:
                await ci.edit_original_response(
                    content="🔄 Re-drafted the week with fresh picks.", view=confirm
                )
            except discord.HTTPException:
                pass

        async def _cancel(ci: discord.Interaction):
            for c in confirm.children:
                c.disabled = True
            await ci.response.edit_message(content="↩️ Kept the current draft.", view=confirm)

        yes.callback = _do
        no.callback = _cancel
        confirm.add_item(yes)
        confirm.add_item(no)
        await interaction.response.send_message(
            "🔄 **Re-draft the whole week?** This replaces every conductor for this week with "
            "fresh fair rotation picks, including any you set by hand or marked as no-train.",
            view=confirm,
            ephemeral=True,
        )

    async def on_timeout(self):
        await wizard_registry.expire_view_message(self.message, command_hint="/train draft_week")


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


def regenerate_week(bot, guild_id: int, week_start: date) -> list[tr.DraftDay]:
    """Generate a fresh draft for the week and persist it as scheduled rows.
    Blocking — call via asyncio.to_thread."""
    from config import get_train_config

    state = load_rotation_state(bot, guild_id)
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
    )
    tr.write_draft_rows(guild_id, cfg.get("history_tab") or "", draft)
    return draft


def load_week_draft(bot, guild_id: int, week_start: date) -> list[tr.DraftDay]:
    """Reconstruct the current week's draft from the scheduled history rows, so
    `/train draft_week` can reopen an editable view without re-rolling. Falls
    back to generating a fresh draft when no scheduled rows exist for the week.
    Blocking — call via asyncio.to_thread."""
    from config import get_train_config

    cfg = get_train_config(guild_id)
    history = tr.load_history(guild_id, cfg.get("history_tab") or "")
    week_isos = {(week_start + timedelta(days=i)).isoformat() for i in range(7)}
    # Honour scheduled (still editable) + posted (already confirmed) rows so a
    # reopened draft reflects reality instead of reverting to needs-picking.
    relevant = (tr.STATUS_SCHEDULED, tr.STATUS_POSTED)
    rows = {h.date: h for h in history if h.date in week_isos and h.status in relevant}
    if not rows:
        return regenerate_week(bot, guild_id, week_start)
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
                    note="needs picking",
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
                    note=h.notes,
                )
            )
    return draft


# ══════════════════════════════════════════════════════════════════════════════
# Daily confirmation
# ══════════════════════════════════════════════════════════════════════════════


class _ConfirmPostModal(discord.ui.Modal, title="Post Train Conductor"):
    """Captures an optional blurb + image URL, then posts the conductor publicly.

    Image is a URL (not an upload) because Discord modals can't carry file
    attachments — leadership pastes any image link, or leaves it blank."""

    def __init__(self, view: "DailyConfirmView"):
        super().__init__()
        self._view = view
        self.blurb = discord.ui.TextInput(
            label="Blurb (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=1000,
        )
        self.image_url = discord.ui.TextInput(
            label="Image URL (optional)",
            placeholder="https://…  (paste a link; uploads aren't possible here)",
            required=False,
            max_length=400,
        )
        self.add_item(self.blurb)
        self.add_item(self.image_url)

    async def on_submit(self, interaction: discord.Interaction):
        await self._view.do_confirm(
            interaction, blurb=self.blurb.value.strip(), image_url=self.image_url.value.strip()
        )


class DailyConfirmView(discord.ui.View):
    """Each drive day's confirmation. Confirm writes a `posted` history row and —
    when a public channel is configured — announces the conductor there (with an
    optional blurb + image). With no public channel it just records the
    conductor. The other buttons adjust the conductor first.

    `public_channel_id` of 0 means the alliance opted out of public posts."""

    def __init__(self, bot, guild_id: int, draft_day: tr.DraftDay, public_channel_id: int):
        super().__init__(timeout=EDITOR_TIMEOUT)
        self.bot = bot
        self.guild_id = guild_id
        self.dd = draft_day
        self.public_channel_id = public_channel_id
        self.message: discord.Message | None = None
        # Label the confirm button by whether it posts publicly.
        self.confirm.label = (
            "✅ Confirm + post publicly" if public_channel_id else "✅ Confirm conductor"
        )

    @discord.ui.button(label="✅ Confirm conductor", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_leader(interaction):
            await interaction.response.send_message(DENY_NOT_LEADER, ephemeral=True)
            return
        if not self.dd.member:
            await interaction.response.send_message(
                "⚠️ No conductor set. Use **✏️ Manually assign** or **⏭️ Select next person** first.",
                ephemeral=True,
            )
            return
        if self.public_channel_id:
            # Public post configured → collect an optional blurb + image first.
            await interaction.response.send_modal(_ConfirmPostModal(self))
        else:
            # No public channel → just record the conductor as posted.
            await self.do_confirm(interaction, blurb="", image_url="")

    @discord.ui.button(label="⏭️ Go to next person", style=discord.ButtonStyle.primary)
    async def next_person(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_leader(interaction):
            await interaction.response.send_message(DENY_NOT_LEADER, ephemeral=True)
            return
        await interaction.response.defer()
        state = await asyncio.to_thread(load_rotation_state, self.bot, self.guild_id)
        member, reason, needs = tr.reroll_day(
            self.dd,
            eligible_pool=state.eligible_pool,
            role_pools=state.role_pools,
            member_rules=state.member_rules,
            history=state.history,
            counted_reasons=state.counted_reasons,
            other_scheduled=set(),
            target_date=date.fromisoformat(self.dd.date),
        )
        self.dd.member, self.dd.reason, self.dd.needs_picking = member, reason, needs
        await asyncio.to_thread(self._persist_scheduled)
        await self._refresh(interaction)

    @discord.ui.button(label="✏️ Assign someone", style=discord.ButtonStyle.secondary)
    async def assign(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not _is_leader(interaction):
            await interaction.response.send_message(DENY_NOT_LEADER, ephemeral=True)
            return

        async def _assign_name(inter: discord.Interaction, typed: str):
            await inter.response.defer()
            state = await asyncio.to_thread(load_rotation_state, self.bot, self.guild_id)
            self.dd.member = _resolve_roster_name(state, typed)
            self.dd.reason = "manual"
            self.dd.needs_picking = False
            await asyncio.to_thread(self._persist_scheduled)
            await self._refresh(inter)

        await interaction.response.send_modal(
            _MemberNameModal("Assign today's conductor", _assign_name, current=self.dd.member or "")
        )

    def _persist_scheduled(self):
        from config import get_train_config

        tab = get_train_config(self.guild_id).get("history_tab") or ""
        tr.set_day_status(
            self.guild_id,
            tab,
            self.dd.date,
            member=self.dd.member or "",
            reason=self.dd.reason,
            status=tr.STATUS_SCHEDULED,
        )

    async def _refresh(self, interaction: discord.Interaction):
        embed = build_daily_confirm_embed(self.dd)
        try:
            if self.message:
                await self.message.edit(embed=embed, view=self)
            elif not interaction.response.is_done():
                await interaction.response.edit_message(embed=embed, view=self)
        except discord.HTTPException:
            pass

    async def do_confirm(self, interaction: discord.Interaction, *, blurb: str, image_url: str):
        """Write the posted row and, when a public channel is configured,
        announce the conductor there. With no public channel it just records."""
        from config import get_train_config

        if not interaction.response.is_done():
            await interaction.response.defer()

        # Public announcement (only when a channel was configured).
        if self.public_channel_id:
            channel = self.bot.get_channel(self.public_channel_id)
            if channel is None:
                await interaction.followup.send(
                    "⚠️ The public post channel isn't reachable. Re-check it in "
                    "`/setup` → 🚂 Train.",
                    ephemeral=True,
                )
                return
            embed = build_public_post_embed(self.dd, blurb=blurb, image_url=image_url)
            try:
                await channel.send(embed=embed)
            except discord.Forbidden:
                await interaction.followup.send(
                    f"⚠️ I can't post in <#{self.public_channel_id}>. Grant me View Channel and "
                    "Send Messages there.",
                    ephemeral=True,
                )
                return

        now_iso = datetime.now(tz=ZoneInfo("UTC")).isoformat(timespec="minutes")
        tab = get_train_config(self.guild_id).get("history_tab") or ""
        await asyncio.to_thread(
            tr.set_day_status,
            self.guild_id,
            tab,
            self.dd.date,
            member=self.dd.member or "",
            reason=self.dd.reason,
            status=tr.STATUS_POSTED,
            posted_at=now_iso,
        )
        for item in self.children:
            item.disabled = True
        if self.message:
            done = (
                f"✅ Posted **{self.dd.member}** to <#{self.public_channel_id}>."
                if self.public_channel_id
                else f"✅ Recorded **{self.dd.member}** as today's conductor."
            )
            try:
                await self.message.edit(content=done, view=self)
            except discord.HTTPException:
                pass
        self.stop()

    async def on_timeout(self):
        await wizard_registry.expire_view_message(self.message, command_hint="/train draft_week")
