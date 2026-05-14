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

logger = logging.getLogger(__name__)


def _strategy_helpers():
    """Late-bind storm_strategy helpers so storm_member_rules can load
    even if `storm_strategy` happens to be loaded later (or fails to
    load). The cost is a few function-call lookups per command; the
    benefit is no cog-import-order coupling between the two modules."""
    from storm_strategy import parse_power, format_power, canonical_zones_for
    return parse_power, format_power, canonical_zones_for


_HEADER = ["Rule Type", "Subject", "Sub-Type", "Value", "Notes"]

_RULE_TYPE_POWER_BAND = "power_band"
_RULE_TYPE_PER_MEMBER = "per_member"

_PER_MEMBER_SUB_TYPES = ("team", "zone", "special_role")
_SPECIAL_ROLES        = ("commander", "judicator")
_TEAMS                = ("A", "B")


# ── Subject resolution (#136) ────────────────────────────────────────────────
#
# Slash commands accept EITHER a `discord.Member` picker (the common
# case — alliance member who's on the server) OR a free-text
# `member_name` (the escape hatch — non-Discord member, or one the bot
# can't see). Storage convention:
#
#   * Discord-resolvable subject → str(discord_id)
#   * non-Discord subject        → name verbatim
#
# Loader (Rule.render_label + auto-fill resolution in storm_roster_builder)
# interprets the subject by looking at the string itself — numeric → Discord
# ID lookup against the live guild; otherwise → name match.
#
# Existing rules with name subjects keep working unchanged — render_label
# falls back to the raw subject when it isn't numeric, and the
# auto-fill / apply paths already accept both forms.

_SUBJECT_REQUIRED_MSG = (
    "⚠️ Provide a member. Pick from the typeahead (server member) OR "
    "type a roster name (non-Discord member) — exactly one, not both."
)


def _resolve_subject(
    member_user: discord.Member | None,
    member_name: str | None,
    *,
    guild: discord.Guild | None = None,
) -> tuple[str | None, str]:
    """Return `(subject_for_storage, display_name)` from the wizard's
    two-input shape.

    Returns `(None, "")` if neither / both were provided OR the picker
    selected a bot — caller should reject with `_SUBJECT_REQUIRED_MSG`.
    `member_user` is preferred when supplied; the free-text
    `member_name` is the escape hatch for non-Discord roster rows.

    Dedupe normalisation: if a free-text name matches a Discord member's
    display name in `guild` (case-insensitive, bots excluded), the
    subject is stored as that member's Discord ID instead of the typed
    name. Without this, an officer could create two rules for the same
    person — one via the picker (Discord ID) and one via the name —
    that would both fire at apply time and double-effect.

    The bot-reject branch was added by the audit pass — Discord's
    Member picker can include bot accounts, and a rule saved against a
    bot ID silently never resolves at apply time.
    """
    has_user = member_user is not None
    has_name = bool((member_name or "").strip())
    if has_user and has_name:
        return None, ""
    if has_user:
        if member_user.bot:
            return None, ""
        return str(member_user.id), member_user.display_name
    if has_name:
        cleaned = member_name.strip()
        if guild is not None:
            match = _match_member_by_display_name(guild, cleaned)
            if match is not None:
                return str(match.id), match.display_name
        return cleaned, cleaned
    return None, ""


def _match_member_by_display_name(
    guild: discord.Guild, name: str,
) -> discord.Member | None:
    """Case-insensitive single-match lookup against `guild.members`.
    Bots are excluded. Returns None when zero matches OR more than one
    match — ambiguity stays in the typed-name form so the officer can
    re-enter via the picker."""
    target = name.strip().lower()
    if not target:
        return None
    matches: list[discord.Member] = []
    for m in getattr(guild, "members", []) or []:
        if getattr(m, "bot", False):
            continue
        display = getattr(m, "display_name", "") or ""
        if display.strip().lower() == target:
            matches.append(m)
            if len(matches) > 1:
                return None  # ambiguous — don't normalize
    return matches[0] if matches else None


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

    def render_label(self, *, guild=None) -> str:
        """Human-readable single-line summary for embed listings.

        For per_member rules with a Discord-ID subject (#136), the
        guild is consulted to resolve the CURRENT display name. A
        rename between rule creation and rendering naturally surfaces
        the new name. Falls back to the raw subject when the guild is
        None or the member can't be resolved — the rule remains
        recognizable rather than crashing.
        """
        if self.rule_type == _RULE_TYPE_POWER_BAND:
            try:
                _parse_power, format_power, _zones = _strategy_helpers()
                threshold = format_power(int(self.subject))
            except (TypeError, ValueError):
                threshold = self.subject
            return f"⚖️  ≥ {threshold} → eligible for **{self.value}**"
        # per_member
        display = self._resolve_display_name(guild)
        if self.sub_type == "team":
            return f"👤  **{display}** → plays **Team {self.value}**"
        if self.sub_type == "zone":
            return f"👤  **{display}** → always at **{self.value}**"
        if self.sub_type == "special_role":
            return f"🎖️  **{display}** → **{self.value.title()}** candidate"
        return f"👤  **{display}** → {self.sub_type}={self.value}"

    def _resolve_display_name(self, guild) -> str:
        """Back-compat shim — delegates to the module-level helper so
        existing callers using `rule._resolve_display_name(guild)` keep
        working. New callers should prefer `resolve_subject_display`."""
        return resolve_subject_display(self.subject, guild)


def resolve_subject_display(subject: str, guild) -> str:
    """If `subject` is a numeric string (Discord ID), look up the
    current display name in the guild. Otherwise return the raw
    subject (non-Discord member name).

    Defensive — `guild=None`, non-digit subject, and missing-member
    all fall back to the raw subject so a renamed/left member's rule
    still renders. Three falsy paths, all explicit per the CLAUDE.md
    pattern.

    Module-level so `_list`'s member filter and any future caller can
    use it without reaching into a private `_resolve_display_name`
    method on a Rule instance.
    """
    s = (subject or "").strip()
    if not s or not s.isdigit():
        return s
    if guild is None:
        return s
    try:
        member = guild.get_member(int(s))
    except (TypeError, ValueError):
        return s
    if member is None:
        return s
    return member.display_name


def list_rules(guild_id: int, event_type: str) -> list[Rule]:
    """Read every rule for this guild + event type. Order matches Sheet
    row order (top-down) so clear-by-index is stable across reads.

    Uses `get_all_values` + manual header indexing rather than
    `get_all_records`. The latter raises on duplicate header values and
    squashes empty header cells, both of which can happen if an officer
    accidentally pastes a row into the header. With `get_all_values`,
    a header typo causes that one column to be skipped instead of
    silently emptying the rule list.
    """
    ws = _get_or_create_rules_worksheet(guild_id, event_type)
    if ws is None:
        return []
    try:
        values = ws.get_all_values()
    except Exception as e:
        logger.warning("[STORM RULES] list_rules failed for guild=%s event=%s: %s",
                       guild_id, event_type, e)
        return []
    if not values:
        return []

    header = [c.strip() for c in values[0]]

    def _col(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            return -1

    type_col    = _col("Rule Type")
    subject_col = _col("Subject")
    subtype_col = _col("Sub-Type")
    value_col   = _col("Value")
    notes_col   = _col("Notes")

    def _cell(row: list[str], idx: int) -> str:
        if idx < 0 or idx >= len(row):
            return ""
        return str(row[idx]).strip()

    rules: list[Rule] = []
    for row in values[1:]:
        if not row or not any(c.strip() for c in row):
            continue
        rule_type = _cell(row, type_col).lower()
        if rule_type not in (_RULE_TYPE_POWER_BAND, _RULE_TYPE_PER_MEMBER):
            continue
        rules.append(Rule(
            rule_type=rule_type,
            subject=_cell(row, subject_col),
            sub_type=_cell(row, subtype_col).lower(),
            value=_cell(row, value_col),
            notes=_cell(row, notes_col),
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
    True on success.

    Uses gspread's atomic `delete_rows` instead of clear-and-rewrite —
    the latter is non-atomic (read → clear → write) and two officers
    deleting different rules at the same time can clobber each other's
    work via the standard last-write-wins race.

    Re-checks the rule list and the Sheet row count immediately before
    issuing the delete so a stale view doesn't drop the wrong row.
    """
    ws = _get_or_create_rules_worksheet(guild_id, event_type)
    if ws is None:
        return False
    rules = list_rules(guild_id, event_type)
    if index < 0 or index >= len(rules):
        return False

    try:
        row_count = len(ws.get_all_values())
    except Exception as e:
        logger.warning("[STORM RULES] delete row-count read failed for guild=%s event=%s: %s",
                       guild_id, event_type, e)
        return False

    # Sheet is 1-indexed; row 1 is the header, row (2 + index) is the
    # target data row.
    target_row = 2 + index
    if target_row > row_count:
        return False

    try:
        ws.delete_rows(target_row)
    except Exception as e:
        logger.warning("[STORM RULES] delete write failed for guild=%s event=%s: %s",
                       guild_id, event_type, e)
        return False
    return True


# ── Slash command group + UI ─────────────────────────────────────────────────


async def _deny_if_not_leader(interaction: discord.Interaction) -> bool:
    """Return True iff the caller is admin/leadership. Sends the standard
    denial ephemeral on the False branch."""
    from storm_permissions import is_leader_or_admin, deny_non_leader
    if is_leader_or_admin(interaction):
        return True
    await deny_non_leader(interaction)
    return False


class _RulesListView(discord.ui.View):
    """List + clear buttons. Each button maps to one rule index. Discord
    limits Views to 25 components; we paginate at 20 rules per page (4
    rows of 5 clear buttons)."""

    def __init__(self, guild_id: int, user_id: int, event_type: str,
                 rules: list[Rule], page: int = 0, per_page: int = 20,
                 *, guild: discord.Guild | None = None):
        super().__init__(timeout=300)
        self.guild_id = guild_id
        self.user_id  = user_id
        self.event_type = event_type
        self.rules    = rules
        self.page     = page
        self.per_page = per_page
        # Live Guild handle so Rule.render_label can resolve Discord-ID
        # subjects to their current display name. Falls back gracefully
        # to the raw subject when None.
        self.guild    = guild
        self.message: discord.Message | None = None
        self._build_buttons()

    async def on_timeout(self) -> None:
        """Strip the buttons on timeout so officers know the list went
        stale (Clear and pagination would otherwise surface 'Interaction
        failed' silently)."""
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

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
                lines.append(f"`{i + 1:>2}` · {r.render_label(guild=self.guild)}")
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
    """Shared shape for DS and CS member-rule slash command groups.

    Guild-only: every subcommand reads `interaction.guild_id` / accepts
    a `discord.Member` picker, both of which require a guild context.
    Matches the rest of the storm command surface (signup_post,
    officer_view, strategy) which the prior audit pass added the
    decorator to.
    """

    def __init__(self, *, name: str, description: str, event_type: str):
        super().__init__(
            name=name, description=description,
            guild_only=True,
        )
        self.event_type = event_type

    # ── set_power_band ────────────────────────────────────────────────
    async def _set_power_band(self, interaction: discord.Interaction,
                              threshold: str, zone: str, notes: str = ""):
        if not await _deny_if_not_leader(interaction):
            return
        parse_power, format_power, canonical_zones_for = _strategy_helpers()
        n = parse_power(threshold)
        # Allow ≥ 0 — "≥ 0M → Power Tower" is a legitimate way to declare
        # "no floor for this zone." Only refuse unparseable or negative.
        if n is None or n < 0:
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
    async def _set_member_team(
        self, interaction: discord.Interaction,
        member_user: discord.Member | None,
        member_name: str | None,
        team: str, notes: str = "",
    ):
        if not await _deny_if_not_leader(interaction):
            return
        if self.event_type == "CS":
            await interaction.response.send_message(
                "⚠️ `team` rules only apply to Desert Storm. Use the zone or special_role "
                "commands for Canyon Storm.",
                ephemeral=True,
            )
            return
        subject, display = _resolve_subject(
            member_user, member_name, guild=interaction.guild,
        )
        if subject is None:
            await interaction.response.send_message(
                _SUBJECT_REQUIRED_MSG, ephemeral=True,
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
                 subject=subject, sub_type="team",
                 value=team_clean, notes=notes),
        )
        if ok:
            await interaction.response.send_message(
                f"✅ Saved: **{display}** → plays **Team {team_clean}**.",
            )
        else:
            await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)

    # ── set_member_zone ──────────────────────────────────────────────
    async def _set_member_zone(
        self, interaction: discord.Interaction,
        member_user: discord.Member | None,
        member_name: str | None,
        zone: str, notes: str = "",
    ):
        if not await _deny_if_not_leader(interaction):
            return
        subject, display = _resolve_subject(
            member_user, member_name, guild=interaction.guild,
        )
        if subject is None:
            await interaction.response.send_message(
                _SUBJECT_REQUIRED_MSG, ephemeral=True,
            )
            return
        zone_clean = (zone or "").strip()
        if not zone_clean:
            await interaction.response.send_message(
                "⚠️ `zone` is required.", ephemeral=True,
            )
            return
        _parse, _format, canonical_zones_for = _strategy_helpers()
        canonical = {z.lower() for z in canonical_zones_for(self.event_type)}
        zone_warning = "" if zone_clean.lower() in canonical else (
            f"\n⚠️ `{zone_clean}` isn't in the canonical zone list — "
            "saved anyway; double-check the spelling."
        )
        ok, msg = save_rule(
            interaction.guild_id, self.event_type,
            Rule(rule_type=_RULE_TYPE_PER_MEMBER,
                 subject=subject, sub_type="zone",
                 value=zone_clean, notes=notes),
        )
        if ok:
            await interaction.response.send_message(
                f"✅ Saved: **{display}** → always at **{zone_clean}**.{zone_warning}",
            )
        else:
            await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)

    # ── set_member_role ──────────────────────────────────────────────
    async def _set_member_role(
        self, interaction: discord.Interaction,
        member_user: discord.Member | None,
        member_name: str | None,
        role: str, notes: str = "",
    ):
        if not await _deny_if_not_leader(interaction):
            return
        subject, display = _resolve_subject(
            member_user, member_name, guild=interaction.guild,
        )
        if subject is None:
            await interaction.response.send_message(
                _SUBJECT_REQUIRED_MSG, ephemeral=True,
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
                 subject=subject, sub_type="special_role",
                 value=role_clean, notes=notes),
        )
        if ok:
            await interaction.response.send_message(
                f"✅ Saved: **{display}** → **{role_clean.title()}** candidate.",
            )
        else:
            await interaction.response.send_message(f"⚠️ {msg}", ephemeral=True)

    # ── list ─────────────────────────────────────────────────────────
    async def _list(self, interaction: discord.Interaction, member: str | None = None):
        if not await _deny_if_not_leader(interaction):
            return
        rules = list_rules(interaction.guild_id, self.event_type)
        if member:
            # Filter matches either the raw subject (works for non-Discord
            # name subjects) OR the resolved display name (works for
            # Discord-ID-keyed rules whose member has since been renamed).
            mlow = member.strip().lower()
            filtered = []
            for r in rules:
                if r.rule_type != _RULE_TYPE_PER_MEMBER:
                    continue
                if r.subject.strip().lower() == mlow:
                    filtered.append(r)
                    continue
                display = resolve_subject_display(r.subject, interaction.guild)
                if display.strip().lower() == mlow:
                    filtered.append(r)
            rules = filtered
        view = _RulesListView(
            interaction.guild_id, interaction.user.id,
            self.event_type, rules,
            guild=interaction.guild,
        )
        await interaction.response.send_message(embed=view.render_embed(), view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            view.message = None


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
        member_user="Pick from the server (preferred — keys by Discord ID, survives renames)",
        member_name="OR a roster name if the member isn't on Discord",
        team="Team A or Team B",
        notes="Optional free-text notes",
    )
    @app_commands.choices(team=[
        app_commands.Choice(name="Team A", value="A"),
        app_commands.Choice(name="Team B", value="B"),
    ])
    async def set_team(
        interaction: discord.Interaction,
        team: app_commands.Choice[str],
        member_user: discord.Member | None = None,
        member_name: str | None = None,
        notes: str = "",
    ):
        await grp._set_member_team(
            interaction, member_user, member_name, team.value, notes,
        )

    @grp.command(name="set_member_zone",
                 description="Lock a specific member to a zone")
    @app_commands.describe(
        member_user="Pick from the server (preferred)",
        member_name="OR a roster name if the member isn't on Discord",
        zone="Zone they always play",
        notes="Optional free-text notes",
    )
    async def set_zone(
        interaction: discord.Interaction,
        zone: str,
        member_user: discord.Member | None = None,
        member_name: str | None = None,
        notes: str = "",
    ):
        await grp._set_member_zone(
            interaction, member_user, member_name, zone, notes,
        )

    @grp.command(name="set_member_role",
                 description="Tag a member as a Commander or Judicator candidate")
    @app_commands.describe(
        member_user="Pick from the server (preferred)",
        member_name="OR a roster name if the member isn't on Discord",
        role="Commander or Judicator",
        notes="Optional free-text notes",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="Commander", value="commander"),
        app_commands.Choice(name="Judicator", value="judicator"),
    ])
    async def set_role(
        interaction: discord.Interaction,
        role: app_commands.Choice[str],
        member_user: discord.Member | None = None,
        member_name: str | None = None,
        notes: str = "",
    ):
        await grp._set_member_role(
            interaction, member_user, member_name, role.value, notes,
        )

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
        member_user="Pick from the server (preferred)",
        member_name="OR a roster name if the member isn't on Discord",
        zone="Zone they always play",
        notes="Optional free-text notes",
    )
    async def set_zone(
        interaction: discord.Interaction,
        zone: str,
        member_user: discord.Member | None = None,
        member_name: str | None = None,
        notes: str = "",
    ):
        await grp._set_member_zone(
            interaction, member_user, member_name, zone, notes,
        )

    @grp.command(name="set_member_role",
                 description="Tag a member as a Commander or Judicator candidate")
    @app_commands.describe(
        member_user="Pick from the server (preferred)",
        member_name="OR a roster name if the member isn't on Discord",
        role="Commander or Judicator",
        notes="Optional free-text notes",
    )
    @app_commands.choices(role=[
        app_commands.Choice(name="Commander", value="commander"),
        app_commands.Choice(name="Judicator", value="judicator"),
    ])
    async def set_role(
        interaction: discord.Interaction,
        role: app_commands.Choice[str],
        member_user: discord.Member | None = None,
        member_name: str | None = None,
        notes: str = "",
    ):
        await grp._set_member_role(
            interaction, member_user, member_name, role.value, notes,
        )

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
