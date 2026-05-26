"""
Member Rules editor for Desert Storm and Canyon Storm (#127).

Reached via the `👤 Manage member rules` button on `/desertstorm` and
`/canyonstorm` (hub-restructure #187; legacy `/desertstorm member_rule list`
subcommand pre-#127).

Two rule types complement the strategy preset library (#126):

  * power_band — "Members with power ≥ X (in the configured power column)
    are eligible for Zone Y." Primary rule type; surfaces by default on
    the list view.
  * per_member — Escape hatch for special cases. Two sub-types:
        team           e.g. "Alice always plays Team A"
        zone           e.g. "Charlie is always at Power Tower"

Sheet shape (`DS Member Rules` / `CS Member Rules`):
    Rule Type | Subject | Sub-Type | Value | Notes

Where:
  power_band rows:  Rule Type=power_band | Subject=<int power> | Sub-Type='' |
                    Value=<zone name>    | Notes=<free text>
  per_member rows:  Rule Type=per_member | Subject=<member name> |
                    Sub-Type=<team|zone> | Value=<…> | Notes=<…>

Stored Subject for power_band is the raw integer (e.g. "80000000") so
sorting works at the Sheet level. The slash command accepts shorthand
("80M") via the same parser as #126.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from messages import CANCEL_BACKPEDAL_DEFAULT, NOT_SET_UP
from storm_event_hub import HUB_COMMAND, HUB_BTN_RULES

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

_PER_MEMBER_SUB_TYPES = ("team", "zone")
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
    "type a roster name (non-Discord member): exactly one, not both."
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
    return config.get_or_create_worksheet(
        sh, tab_name, header_row=_HEADER, rows=500,
        cols=max(8, len(_HEADER)),
    )


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

    # #178 CS-only: translate legacy internal-key zone values
    # (`s1_power_tower` → `Power Tower`) at read time so existing dev/
    # staging rules continue to match against the post-#178 display-
    # name preset zones. Imported lazily to avoid the cog-import-order
    # coupling the rest of this module is careful about.
    if event_type == "CS":
        from storm_strategy import _translate_legacy_cs_zone
    else:
        _translate_legacy_cs_zone = None

    rules: list[Rule] = []
    for row in values[1:]:
        if not row or not any(c.strip() for c in row):
            continue
        rule_type = _cell(row, type_col).lower()
        if rule_type not in (_RULE_TYPE_POWER_BAND, _RULE_TYPE_PER_MEMBER):
            continue
        sub_type = _cell(row, subtype_col).lower()
        value = _cell(row, value_col)
        # Per-member zone rules + power-band rules both carry a zone
        # name in the Value column. Translate legacy CS keys for
        # either rule type — the auto-fill apply path does case-
        # insensitive equality against the preset's ZoneRow.zone (now
        # display names post-#178), so a stale `s1_power_tower` value
        # would silently no-op without this translation.
        if _translate_legacy_cs_zone is not None and value and (
            rule_type == _RULE_TYPE_POWER_BAND
            or (rule_type == _RULE_TYPE_PER_MEMBER and sub_type == "zone")
        ):
            value = _translate_legacy_cs_zone(value)
        rules.append(Rule(
            rule_type=rule_type,
            subject=_cell(row, subject_col),
            sub_type=sub_type,
            value=value,
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
        return False, NOT_SET_UP
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
            title=f"📋 {label}: Member Rules",
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

        # ➕ Add rule (#169 — Rule M). Always present, even on empty
        # state — the list view is the canonical place officers reach
        # for to manage rules, so the add affordance lives here too.
        add_btn = discord.ui.Button(
            label="➕ Add rule", style=discord.ButtonStyle.primary, row=4,
        )

        async def _on_add(inter: discord.Interaction):
            if inter.user.id != self.user_id:
                await inter.response.send_message(
                    "⛔ Only the command owner can add rules from this list.",
                    ephemeral=True,
                )
                return
            picker = _AddRuleTypePickerView(
                event_type=self.event_type, owner_id=self.user_id,
            )
            await inter.response.send_message(
                "➕ Pick the rule type to add.", view=picker, ephemeral=True,
            )
            try:
                picker.message = await inter.original_response()
            except discord.HTTPException:
                pass

        add_btn.callback = _on_add
        self.add_item(add_btn)

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


class _AddRuleTypePickerView(discord.ui.View):
    """Choice view opened by `_RulesListView`'s [➕ Add rule] button.

    Power-band rules go through the same `InlinePowerBandView` the setup
    wizard uses (zone Select → power-modal). Per-member rules need a
    `discord.Member` picker that Discord modals can't host, so this view
    points the officer at the slash commands instead.
    """

    def __init__(self, *, event_type: str, owner_id: int):
        super().__init__(timeout=120)
        self.event_type = event_type
        self.owner_id = owner_id
        self.message: discord.Message | None = None
        parent = "desertstorm" if event_type == "DS" else "canyonstorm"
        self.parent = parent

        pb_btn = discord.ui.Button(
            label="⚡ Add a power-band rule", style=discord.ButtonStyle.primary,
        )
        pb_btn.callback = self._on_power_band
        self.add_item(pb_btn)

        pm_btn = discord.ui.Button(
            label="👤 Add a per-member rule",
            style=discord.ButtonStyle.secondary,
        )
        pm_btn.callback = self._on_per_member
        self.add_item(pm_btn)

        cancel_btn = discord.ui.Button(
            label="↩️ Cancel", style=discord.ButtonStyle.secondary,
        )
        cancel_btn.callback = self._on_cancel
        self.add_item(cancel_btn)

    async def _guard_owner(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.owner_id:
            await inter.response.send_message(
                "⛔ Only the officer who opened the list can add rules.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_power_band(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        for item in self.children:
            item.disabled = True
        self.stop()
        picker = InlinePowerBandView(self.event_type, owner_id=inter.user.id)
        try:
            await inter.response.edit_message(
                content=(
                    "Pick the zone the rule applies to, then click "
                    "**Set minimum power** to enter the threshold."
                ),
                view=picker,
            )
            picker.message = await inter.original_response()
        except discord.HTTPException:
            pass

    async def _on_per_member(self, inter: discord.Interaction):
        if not await self._guard_owner(inter):
            return
        for item in self.children:
            item.disabled = True
        self.stop()
        body = (
            "👤 Per-member rules need a server-member picker, which Discord "
            "doesn't expose inside a modal. Close this view and re-open "
            f"the rules surface via `{HUB_COMMAND[self.event_type]}` → "
            f"**{HUB_BTN_RULES}**: the per-member options (pin to a "
            "specific zone, or pin to Team A / Team B) live there "
            "alongside the member picker."
        )
        try:
            await inter.response.edit_message(content=body, view=self)
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
                content=CANCEL_BACKPEDAL_DEFAULT, view=self,
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
        # gspread off the event loop — delete + reload both block on
        # a network round-trip.
        await inter.response.defer()
        ok = await asyncio.to_thread(
            delete_rule_at, view.guild_id, view.event_type, idx,
        )
        if not ok:
            await inter.followup.send(
                "⚠️ Couldn't remove that rule. Rerun the list command to refresh.",
                ephemeral=True,
            )
            return
        view.rules = await asyncio.to_thread(
            list_rules, view.guild_id, view.event_type,
        )
        if view.page >= view.total_pages:
            view.page = max(0, view.total_pages - 1)
        view._build_buttons()
        # We deferred above, so edit the deferred response in place.
        try:
            await inter.edit_original_response(
                embed=view.render_embed(), view=view,
            )
        except discord.HTTPException:
            pass
    return _cb


async def open_member_rule_list(
    interaction: discord.Interaction,
    event_type: str,
    *,
    member_filter: str | None = None,
) -> None:
    """Public entry point for the member-rule list view (#187 hub
    + the legacy `/<event> member_rule list` subcommand both call this).
    Posts the inline-action list view with [➕ Add rule] + [🗑 Clear N]
    buttons per Rule M / #169.
    """
    if not await _deny_if_not_leader(interaction):
        return
    # gspread off the event loop.
    rules = await asyncio.to_thread(
        list_rules, interaction.guild_id, event_type,
    )
    if member_filter:
        # Filter matches either the raw subject (works for non-Discord
        # name subjects) OR the resolved display name (works for
        # Discord-ID-keyed rules whose member has since been renamed).
        mlow = member_filter.strip().lower()
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
        event_type, rules,
        guild=interaction.guild,
    )
    if interaction.response.is_done():
        sent = await interaction.followup.send(embed=view.render_embed(), view=view)
        view.message = sent
    else:
        await interaction.response.send_message(embed=view.render_embed(), view=view)
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            view.message = None


# Slash-command surface used to live here as a `_MemberRuleGroup` plus
# `build_ds_member_rule_group` / `build_cs_member_rule_group` factories
# (registered as subgroups under `/desertstorm` and `/canyonstorm`)
# alongside a `_make_zone_autocomplete` factory feeding the `zone`
# autocomplete on those subcommands. Removed in the #187 follow-up —
# the hub buttons (`storm_event_hub`) call `open_member_rule_list`
# directly, and the inline rule-creation flow below is reached from
# `/setup`, so the slash-subgroup layer is no longer reached.


# ── Inline power-band rule view (#144 / #168 — setup wizard inline create) ──
#
# Streamlined `set_power_band` flow for the storm setup wizard's
# 'add your first rule now?' branch (reached via /setup → ⚔️ Desert Storm
# or /setup → 🏜️ Canyon Storm). The full
# slash command (`/<parent> member_rule set_power_band`) takes threshold +
# zone + optional notes; this inline flow omits notes for brevity —
# alliances can edit later via the slash command if they want to add notes.
#
# Shape (#168, Rule E): zone is a Select sourced from
# DS_ZONE_STRUCTURE / CS_ZONE_STRUCTURE so a typo can't slip through; the
# power threshold stays a free-text TextInput (values are open-ended
# magnitudes like `80M` / `80,000,000`). The view captures the
# picked zone, then a [Set minimum power] button opens a single-field
# modal for the power value. Submit on the modal writes the rule.


class _InlinePowerBandPowerModal(discord.ui.Modal):
    """Single-field modal that captures the power threshold for the picked
    zone and writes the rule. Opened from `InlinePowerBandView` after the
    officer has chosen a zone via the Select."""

    def __init__(self, event_type: str, zone: str):
        label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
        super().__init__(title=f"{label} Power-Band Rule: {zone}"[:45])
        self.event_type = event_type
        self.zone = zone
        self.threshold = discord.ui.TextInput(
            label=f"Minimum power for {zone}"[:45],
            placeholder="e.g. 80M or 80,000,000",
            required=True,
            max_length=20,
        )
        self.add_item(self.threshold)

    async def on_submit(self, interaction: discord.Interaction):
        parse_power, format_power, _ = _strategy_helpers()
        n = parse_power(self.threshold.value)
        hub_cmd = HUB_COMMAND[self.event_type]
        if n is None or n < 0:
            await interaction.response.send_message(
                f"⚠️ Couldn't parse `{self.threshold.value}` as a power "
                f"value. Try `80M` or `80,000,000` next time via "
                f"`{hub_cmd}` → **{HUB_BTN_RULES}**.",
                ephemeral=True,
            )
            return
        ok, msg = await asyncio.to_thread(save_rule,
            interaction.guild_id, self.event_type,
            Rule(rule_type=_RULE_TYPE_POWER_BAND,
                 subject=str(int(n)), value=self.zone),
        )
        if ok:
            await interaction.response.send_message(
                f"✅ Saved: ≥ {format_power(int(n))} → eligible for "
                f"**{self.zone}**.\n"
                f"Add more rules later via `{hub_cmd}` → **{HUB_BTN_RULES}**.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"⚠️ {msg}", ephemeral=True,
            )


class InlinePowerBandView(discord.ui.View):
    """Zone-picker view that gates the power-threshold modal.

    Sent ephemerally from the setup wizard's "Add a rule now?" offer. The
    officer picks a canonical zone from the Select, which enables the
    [Set minimum power] button; clicking it opens a one-field modal for
    the threshold. The whole flow stays ephemeral.
    """

    def __init__(self, event_type: str, owner_id: int):
        super().__init__(timeout=300)
        self.event_type = event_type
        self.owner_id = owner_id
        self.selected_zone: str | None = None
        self.message: discord.Message | None = None
        self._build_components()

    def _build_components(self):
        self.clear_items()
        _, _, canonical_zones_for = _strategy_helpers()
        zones = canonical_zones_for(self.event_type)
        options = [
            discord.SelectOption(
                label=z[:100], value=z[:100],
                default=(z == self.selected_zone),
            )
            for z in zones[:25]
        ]
        zone_select = discord.ui.Select(
            placeholder=(
                f"Pick a zone…  (current: {self.selected_zone})"
                if self.selected_zone else "Pick a zone…"
            ),
            min_values=1, max_values=1, options=options,
        )

        async def _on_zone(inter: discord.Interaction):
            if inter.user.id != self.owner_id:
                await inter.response.send_message(
                    "⛔ Only the user running setup can pick.",
                    ephemeral=True,
                )
                return
            self.selected_zone = zone_select.values[0]
            self._build_components()
            try:
                await inter.response.edit_message(view=self)
            except discord.HTTPException:
                pass

        zone_select.callback = _on_zone
        self.add_item(zone_select)

        set_btn = discord.ui.Button(
            label="⚙️ Set minimum power",
            style=discord.ButtonStyle.primary,
            disabled=self.selected_zone is None,
        )

        async def _on_set(inter: discord.Interaction):
            if inter.user.id != self.owner_id:
                await inter.response.send_message(
                    "⛔ Only the user running setup can pick.",
                    ephemeral=True,
                )
                return
            if not self.selected_zone:
                await inter.response.send_message(
                    "⚠️ Pick a zone first.", ephemeral=True,
                )
                return
            await inter.response.send_modal(
                _InlinePowerBandPowerModal(self.event_type, self.selected_zone)
            )
            # Disable the view so the officer can't fire the modal twice.
            for child in self.children:
                child.disabled = True
            if self.message is not None:
                try:
                    await self.message.edit(view=self)
                except discord.HTTPException:
                    pass
            self.stop()

        set_btn.callback = _on_set
        self.add_item(set_btn)

        cancel_btn = discord.ui.Button(
            label="↩️ Cancel", style=discord.ButtonStyle.secondary,
        )

        async def _on_cancel(inter: discord.Interaction):
            if inter.user.id != self.owner_id:
                await inter.response.send_message(
                    "⛔ Only the user running setup can pick.",
                    ephemeral=True,
                )
                return
            for child in self.children:
                child.disabled = True
            try:
                await inter.response.edit_message(
                    content=CANCEL_BACKPEDAL_DEFAULT, view=self,
                )
            except discord.HTTPException:
                pass
            self.stop()

        cancel_btn.callback = _on_cancel
        self.add_item(cancel_btn)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass
