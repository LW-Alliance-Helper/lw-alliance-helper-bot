"""
Member Rules editor for Desert Storm and Canyon Storm (#127).

Two rule types complement the strategy preset library (#126):

  * power_band — "Members with power ≥ X (in the configured power column)
    are eligible for Zone Y." Primary rule type; surfaces by default in
    /ds_member_rule list.
  * per_member — Escape hatch for special cases. Three sub-types:
        team           e.g. "Alice always plays Team A"
        zone           e.g. "Charlie is always at Power Tower"
        special_role   e.g. "Bob is our Judicator candidate"

Sheet shape (`DS Member Rules` / `CS Member Rules`):
    Rule Type | Subject | Sub-Type | Value | Notes

Where:
  power_band rows:  Rule Type=power_band | Subject=<int power> | Sub-Type='' |
                    Value=<zone name>    | Notes=<free text>
  per_member rows:  Rule Type=per_member | Subject=<member name> |
                    Sub-Type=<team|zone|special_role> | Value=<…> | Notes=<…>

Stored Subject for power_band is the raw integer (e.g. "250000000") so
sorting works at the Sheet level. The slash command accepts shorthand
("250M") via the same parser as #126.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from storm_strategy import parse_power, format_power, canonical_zones_for

logger = logging.getLogger(__name__)


_HEADER = ["Rule Type", "Subject", "Sub-Type", "Value", "Notes"]

_RULE_TYPE_POWER_BAND = "power_band"
_RULE_TYPE_PER_MEMBER = "per_member"

_PER_MEMBER_SUB_TYPES = ("team", "zone", "special_role")
_SPECIAL_ROLES        = ("commander", "judicator")
_TEAMS                = ("A", "B")


# ── Sheet I/O ────────────────────────────────────────────────────────────────


def _rules_tab_name(guild_id: int, event_type: str) -> str:
    import config
    cfg = config.get_structured_storm_config(guild_id, event_type)
    return cfg.get("member_rules_tab") or config.default_structured_tab(
        event_type, "member_rules_tab"
    )


def _get_or_create_rules_worksheet(guild_id: int, event_type: str):
    """Returns the worksheet, creating it (with header) if missing.
    Returns None if no Sheet is configured."""
    import config
    sh = config.get_spreadsheet(guild_id)
    if sh is None:
        return None
    tab_name = _rules_tab_name(guild_id, event_type)
    if not tab_name:
        return None
    try:
        ws = sh.worksheet(tab_name)
    except Exception:
        ws = sh.add_worksheet(title=tab_name, rows=500, cols=max(8, len(_HEADER)))
        ws.append_row(_HEADER, value_input_option="RAW")
    return ws


class Rule:
    """One member rule row. Discriminated by `rule_type`."""
    __slots__ = ("rule_type", "subject", "sub_type", "value", "notes")

    def __init__(self, rule_type: str, subject: str, value: str,
                 sub_type: str = "", notes: str = ""):
        self.rule_type = rule_type
        self.subject   = subject
        self.sub_type  = sub_type or ""
        self.value     = value
        self.notes     = notes or ""

    def render_label(self) -> str:
        """Human-readable single-line summary for embed listings."""
        if self.rule_type == _RULE_TYPE_POWER_BAND:
            try:
                threshold = format_power(int(self.subject))
            except (TypeError, ValueError):
                threshold = self.subject
            return f"⚖️  ≥ {threshold} → eligible for **{self.value}**"
        # per_member
        if self.sub_type == "team":
            return f"👤  **{self.subject}** → plays **Team {self.value}**"
        if self.sub_type == "zone":
            return f"👤  **{self.subject}** → always at **{self.value}**"
        if self.sub_type == "special_role":
            return f"🎖️  **{self.subject}** → **{self.value.title()}** candidate"
        return f"👤  **{self.subject}** → {self.sub_type}={self.value}"


def list_rules(guild_id: int, event_type: str) -> list[Rule]:
    """Read every rule for this guild + event type. Order matches Sheet
    row order (top-down) so clear-by-index is stable across reads."""
    ws = _get_or_create_rules_worksheet(guild_id, event_type)
    if ws is None:
        return []
    try:
        records = ws.get_all_records()
    except Exception as e:
        logger.warning("[STORM RULES] list_rules failed for guild=%s event=%s: %s",
                       guild_id, event_type, e)
        return []
    rules: list[Rule] = []
    for r in records:
        rule_type = str(r.get("Rule Type", "")).strip().lower()
        if rule_type not in (_RULE_TYPE_POWER_BAND, _RULE_TYPE_PER_MEMBER):
            continue
        rules.append(Rule(
            rule_type=rule_type,
            subject=str(r.get("Subject", "")).strip(),
            sub_type=str(r.get("Sub-Type", "")).strip().lower(),
            value=str(r.get("Value", "")).strip(),
            notes=str(r.get("Notes", "")).strip(),
        ))
    return rules


def _rows_equivalent(rule: Rule, candidate: Rule) -> bool:
    """Two rules are duplicates if their (rule_type, subject, sub_type)
    match — value can change (clear before re-set if updating)."""
    if rule.rule_type != candidate.rule_type:
        return False
    if rule.rule_type == _RULE_TYPE_POWER_BAND:
        # power_band uniqueness keyed on (threshold, zone)
        return (rule.subject == candidate.subject
                and rule.value.lower() == candidate.value.lower())
    # per_member uniqueness keyed on (member, sub_type)
    return (rule.subject.lower() == candidate.subject.lower()
            and rule.sub_type == candidate.sub_type)


def save_rule(guild_id: int, event_type: str, rule: Rule) -> tuple[bool, str]:
    """Append a rule row. Returns (ok, message). Rejects duplicates per
    `_rows_equivalent`. Caller can `delete_rule_at` first to update."""
    existing = list_rules(guild_id, event_type)
    for r in existing:
        if _rows_equivalent(r, rule):
            return False, "A matching rule already exists. Clear it first to update."

    ws = _get_or_create_rules_worksheet(guild_id, event_type)
    if ws is None:
        return False, "Your Google Sheet isn't configured. Run setup first."
    try:
        ws.append_row(
            [rule.rule_type, rule.subject, rule.sub_type, rule.value, rule.notes],
            value_input_option="RAW",
        )
    except Exception as e:
        logger.warning("[STORM RULES] save_rule failed for guild=%s event=%s: %s",
                       guild_id, event_type, e)
        return False, "Couldn't write to the Sheet (see logs for details)."
    return True, "Rule saved."


def delete_rule_at(guild_id: int, event_type: str, index: int) -> bool:
    """Remove the rule at the given list_rules index (0-based). Returns
    True on success."""
    ws = _get_or_create_rules_worksheet(guild_id, event_type)
    if ws is None:
        return False
    rules = list_rules(guild_id, event_type)
    if index < 0 or index >= len(rules):
        return False

    try:
        all_values = ws.get_all_values()
    except Exception as e:
        logger.warning("[STORM RULES] delete read failed for guild=%s event=%s: %s",
                       guild_id, event_type, e)
        return False

    # all_values: header row + N data rows; data rows align 1:1 with `rules`.
    if len(all_values) < 2 + index:
        return False
    # Sheet row to drop = index 1 + index (because all_values[0] is header).
    target_row = 1 + index
    kept = [all_values[0]] + [r for i, r in enumerate(all_values[1:], start=1) if i != target_row]
    try:
        ws.clear()
        ws.update("A1", kept, value_input_option="RAW")
    except Exception as e:
        logger.warning("[STORM RULES] delete write failed for guild=%s event=%s: %s",
                       guild_id, event_type, e)
        return False
    return True


# ── Slash command group + UI ─────────────────────────────────────────────────


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


class _RulesListView(discord.ui.View):
    """List + clear buttons. Each button maps to one rule index. Discord
    limits Views to 25 components; we paginate at 20 rules per page (4
    rows of 5 clear buttons)."""

    def __init__(self, guild_id: int, user_id: int, event_type: str,
                 rules: list[Rule], page: int = 0, per_page: int = 20):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id  = user_id
        self.event_type = event_type
        self.rules    = rules
        self.page     = page
        self.per_page = per_page
        self._build_buttons()

    @property
    def total_pages(self) -> int:
        if not self.rules:
            return 1
        return (len(self.rules) + self.per_page - 1) // self.per_page

    def page_slice(self) -> list[tuple[int, Rule]]:
        start = self.page * self.per_page
        return list(enumerate(self.rules))[start:start + self.per_page]

    def render_embed(self) -> discord.Embed:
        label = "Desert Storm" if self.event_type == "DS" else "Canyon Storm"
        lines: list[str] = []
        if not self.rules:
            lines.append("*No member rules saved yet.*")
        else:
            for i, r in self.page_slice():
                lines.append(f"`{i + 1:>2}` · {r.render_label()}")
                if r.notes:
                    lines.append(f"     ↳ _{r.notes}_")
        embed = discord.Embed(
            title=f"📋 {label} — Member Rules",
            description="\n".join(lines) or "*empty*",
            color=discord.Color.blurple(),
        )
        if self.total_pages > 1:
            embed.set_footer(text=f"Page {self.page + 1}/{self.total_pages}")
        return embed

    def _build_buttons(self):
        self.clear_items()
        for i, _ in self.page_slice():
            btn = discord.ui.Button(
                label=f"🗑 Clear {i + 1}",
                style=discord.ButtonStyle.danger,
            )
            btn.callback = _make_clear_callback(self, i)
            self.add_item(btn)

        if self.total_pages > 1:
            prev_btn = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary,
                disabled=self.page == 0, row=4,
            )
            next_btn = discord.ui.Button(
                label="Next ▶", style=discord.ButtonStyle.secondary,
                disabled=self.page >= self.total_pages - 1, row=4,
            )

            async def _prev(inter: discord.Interaction):
                if inter.user.id != self.user_id:
                    await inter.response.send_message(
                        "⛔ Only the command owner can paginate.", ephemeral=True,
                    )
                    return
                self.page = max(0, self.page - 1)
                self._build_buttons()
                await inter.response.edit_message(embed=self.render_embed(), view=self)

            async def _next(inter: discord.Interaction):
                if inter.user.id != self.user_id:
                    await inter.response.send_message(
                        "⛔ Only the command owner can paginate.", ephemeral=True,
                    )
                    return
                self.page = min(self.total_pages - 1, self.page + 1)
                self._build_buttons()
                await inter.response.edit_message(embed=self.render_embed(), view=self)

            prev_btn.callback = _prev
            next_btn.callback = _next
            self.add_item(prev_btn)
            self.add_item(next_btn)


def _make_clear_callback(view: "_RulesListView", idx: int):
    """Build a click callback for one Clear-rule button. Pulled out as a
    function so each iteration of the for-loop captures `idx` by value
    rather than by reference."""
    async def _cb(inter: discord.Interaction):
        if inter.user.id != view.user_id:
            await inter.response.send_message(
                "⛔ Only the user who ran the command can clear rules from this list.",
                ephemeral=True,
            )
            return
        ok = delete_rule_at(view.guild_id, view.event_type, idx)
        if not ok:
            await inter.response.send_message(
                "⚠️ Couldn't remove that rule. Rerun the list command to refresh.",
                ephemeral=True,
            )
            return
        view.rules = list_rules(view.guild_id, view.event_type)
        if view.page >= view.total_pages:
            view.page = max(0, view.total_pages - 1)
        view._build_buttons()
        await inter.response.edit_message(embed=view.render_embed(), view=view)
    return _cb


# ── Cog ──────────────────────────────────────────────────────────────────────


class _MemberRuleGroup(app_commands.Group):
    """Shared shape for DS and CS member-rule slash command groups."""

    def __init__(self, *, name: str, description: str, event_type: str):
        super().__init__(name=name, description=description)
        self.event_type = event_type

    # ── set_power_band ────────────────────────────────────────────────
    async def _set_power_band(self, interaction: discord.Interaction,
                              threshold: str, zone: str, notes: str = ""):
        if not _user_can_run(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to manage member rules.",
                ephemeral=True,
            )
            return
        n = parse_power(threshold)
        if n is None or n <= 0:
            await interaction.response.send_message(
                f"⚠️ Couldn't parse `{threshold}` as a power value. "
                "Try formats like `250M`, `1.2B`, or `300,000,000`.",
                ephemeral=True,
            )
            return
        zone = (zone or "").strip()
        if not zone:
            await interaction.response.send_message(
                "⚠️ Provide a zone name (e.g. `Power Tower`).", ephemeral=True,
            )
            return
        canonical = {z.lower() for z in canonical_zones_for(self.event_type)}
        zone_warning = "" if zone.lower() in canonical else (
            f"\n⚠️ `{zone}` isn't in the canonical zone list — "
            "the rule was saved, but double-check the spelling."
        )
        ok, msg = save_rule(
            interaction.guild_id, self.event_type,
            Rule(rule_type=_RULE_TYPE_POWER_BAND,
                 subject=str(int(n)), value=zone, notes=notes),
        )
        if ok:
            await interaction.response.send_message(
                f"✅ Saved: ≥ {format_power(int(n))} → eligible for **{zone}**.{zone_warning}",
            )
        else:
            await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)

    # ── set_member_team (DS only — `team` doesn't exist for CS) ──────
    async def _set_member_team(self, interaction: discord.Interaction,
                               member: str, team: str, notes: str = ""):
        if not _user_can_run(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to manage member rules.",
                ephemeral=True,
            )
            return
        if self.event_type == "CS":
            await interaction.response.send_message(
                "⚠️ `team` rules only apply to Desert Storm. Use the zone or special_role "
                "commands for Canyon Storm.",
                ephemeral=True,
            )
            return
        team_clean = (team or "").strip().upper()
        if team_clean not in _TEAMS:
            await interaction.response.send_message(
                f"⚠️ Team must be `A` or `B`. Got `{team}`.", ephemeral=True,
            )
            return
        ok, msg = save_rule(
            interaction.guild_id, self.event_type,
            Rule(rule_type=_RULE_TYPE_PER_MEMBER,
                 subject=member.strip(), sub_type="team",
                 value=team_clean, notes=notes),
        )
        if ok:
            await interaction.response.send_message(
                f"✅ Saved: **{member.strip()}** → plays **Team {team_clean}**.",
            )
        else:
            await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)

    # ── set_member_zone ──────────────────────────────────────────────
    async def _set_member_zone(self, interaction: discord.Interaction,
                               member: str, zone: str, notes: str = ""):
        if not _user_can_run(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to manage member rules.",
                ephemeral=True,
            )
            return
        member_clean = (member or "").strip()
        zone_clean = (zone or "").strip()
        if not member_clean or not zone_clean:
            await interaction.response.send_message(
                "⚠️ Both `member` and `zone` are required.", ephemeral=True,
            )
            return
        canonical = {z.lower() for z in canonical_zones_for(self.event_type)}
        zone_warning = "" if zone_clean.lower() in canonical else (
            f"\n⚠️ `{zone_clean}` isn't in the canonical zone list — "
            "saved anyway; double-check the spelling."
        )
        ok, msg = save_rule(
            interaction.guild_id, self.event_type,
            Rule(rule_type=_RULE_TYPE_PER_MEMBER,
                 subject=member_clean, sub_type="zone",
                 value=zone_clean, notes=notes),
        )
        if ok:
            await interaction.response.send_message(
                f"✅ Saved: **{member_clean}** → always at **{zone_clean}**.{zone_warning}",
            )
        else:
            await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)

    # ── set_member_role ──────────────────────────────────────────────
    async def _set_member_role(self, interaction: discord.Interaction,
                               member: str, role: str, notes: str = ""):
        if not _user_can_run(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to manage member rules.",
                ephemeral=True,
            )
            return
        role_clean = (role or "").strip().lower()
        if role_clean not in _SPECIAL_ROLES:
            await interaction.response.send_message(
                f"⚠️ Role must be `commander` or `judicator`. Got `{role}`.",
                ephemeral=True,
            )
            return
        ok, msg = save_rule(
            interaction.guild_id, self.event_type,
            Rule(rule_type=_RULE_TYPE_PER_MEMBER,
                 subject=member.strip(), sub_type="special_role",
                 value=role_clean, notes=notes),
        )
        if ok:
            await interaction.response.send_message(
                f"✅ Saved: **{member.strip()}** → **{role_clean.title()}** candidate.",
            )
        else:
            await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)

    # ── list ─────────────────────────────────────────────────────────
    async def _list(self, interaction: discord.Interaction, member: str | None = None):
        if not _user_can_run(interaction):
            await interaction.response.send_message(
                "⛔ You need the leadership role (or admin) to view member rules.",
                ephemeral=True,
            )
            return
        rules = list_rules(interaction.guild_id, self.event_type)
        if member:
            mlow = member.strip().lower()
            rules = [r for r in rules
                     if r.rule_type == _RULE_TYPE_PER_MEMBER and r.subject.lower() == mlow]
        view = _RulesListView(interaction.guild_id, interaction.user.id,
                              self.event_type, rules)
        await interaction.response.send_message(embed=view.render_embed(), view=view)


def _build_ds_group() -> _MemberRuleGroup:
    grp = _MemberRuleGroup(
        name="ds_member_rule",
        description="Manage Desert Storm member rules (power bands + per-member)",
        event_type="DS",
    )

    @grp.command(name="set_power_band",
                 description="Add a power-band eligibility rule for a zone")
    @app_commands.describe(
        threshold="Minimum power (e.g. 250M, 1.2B, 300,000,000)",
        zone="Zone the band applies to (e.g. Power Tower)",
        notes="Optional free-text notes",
    )
    async def set_pb(interaction: discord.Interaction, threshold: str, zone: str, notes: str = ""):
        await grp._set_power_band(interaction, threshold, zone, notes)

    @grp.command(name="set_member_team",
                 description="Lock a specific member to Team A or B")
    @app_commands.describe(
        member="Roster member name (must match the Sheet)",
        team="Team A or Team B",
        notes="Optional free-text notes",
    )
    @app_commands.choices(team=[
        app_commands.Choice(name="Team A", value="A"),
        app_commands.Choice(name="Team B", value="B"),
    ])
    async def set_team(interaction: discord.Interaction, member: str,
                       team: app_commands.Choice[str], notes: str = ""):
        await grp._set_member_team(interaction, member, team.value, notes)

    @grp.command(name="set_member_zone",
                 description="Lock a specific member to a zone")
    @app_commands.describe(
        member="Roster member name",
        zone="Zone they always play",
        notes="Optional free-text notes",
    )
    async def set_zone(interaction: discord.Interaction, member: str,
                       zone: str, notes: str = ""):
        await grp._set_member_zone(interaction, member, zone, notes)

    @grp.command(name="set_member_role",
                 description="Tag a member as a Commander or Judicator candidate")
    @app_commands.describe(
        member="Roster member name",
        role="Commander or Judicator",
        notes="Optional free-text notes",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="Commander", value="commander"),
        app_commands.Choice(name="Judicator", value="judicator"),
    ])
    async def set_role(interaction: discord.Interaction, member: str,
                       role: app_commands.Choice[str], notes: str = ""):
        await grp._set_member_role(interaction, member, role.value, notes)

    @grp.command(name="list",
                 description="Show all saved DS member rules (with Clear buttons)")
    @app_commands.describe(member="Optional — filter to one member's rules")
    async def listing(interaction: discord.Interaction, member: str | None = None):
        await grp._list(interaction, member)

    return grp


def _build_cs_group() -> _MemberRuleGroup:
    grp = _MemberRuleGroup(
        name="cs_member_rule",
        description="Manage Canyon Storm member rules (power bands + per-member)",
        event_type="CS",
    )

    @grp.command(name="set_power_band",
                 description="Add a power-band eligibility rule for a zone")
    @app_commands.describe(
        threshold="Minimum power (e.g. 250M)",
        zone="Zone the band applies to",
        notes="Optional free-text notes",
    )
    async def set_pb(interaction: discord.Interaction, threshold: str, zone: str, notes: str = ""):
        await grp._set_power_band(interaction, threshold, zone, notes)

    @grp.command(name="set_member_zone",
                 description="Lock a specific member to a zone")
    @app_commands.describe(
        member="Roster member name",
        zone="Zone they always play",
        notes="Optional free-text notes",
    )
    async def set_zone(interaction: discord.Interaction, member: str,
                       zone: str, notes: str = ""):
        await grp._set_member_zone(interaction, member, zone, notes)

    @grp.command(name="set_member_role",
                 description="Tag a member as a Commander or Judicator candidate")
    @app_commands.describe(
        member="Roster member name",
        role="Commander or Judicator",
        notes="Optional free-text notes",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="Commander", value="commander"),
        app_commands.Choice(name="Judicator", value="judicator"),
    ])
    async def set_role(interaction: discord.Interaction, member: str,
                       role: app_commands.Choice[str], notes: str = ""):
        await grp._set_member_role(interaction, member, role.value, notes)

    @grp.command(name="list",
                 description="Show all saved CS member rules (with Clear buttons)")
    @app_commands.describe(member="Optional — filter to one member's rules")
    async def listing(interaction: discord.Interaction, member: str | None = None):
        await grp._list(interaction, member)

    return grp


class StormMemberRulesCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.ds_group = _build_ds_group()
        self.cs_group = _build_cs_group()
        bot.tree.add_command(self.ds_group)
        bot.tree.add_command(self.cs_group)

    async def cog_unload(self):
        try:
            self.bot.tree.remove_command(self.ds_group.name)
            self.bot.tree.remove_command(self.cs_group.name)
        except Exception:
            pass


async def setup(bot: commands.Bot):
    await bot.add_cog(StormMemberRulesCog(bot))
