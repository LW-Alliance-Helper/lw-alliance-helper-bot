"""
Storm Trends Viewer (#246) — Premium-only.

Reachable via the `🔍 View trends across events` button on the
`/desertstorm` and `/canyonstorm` event hubs. Officers pick a
roster-multi-select question (or the unified `showed_up` attendance
column from #245), an operator + threshold, a lookback window, and an
optional team filter. The bot reads the `<DS|CS> Member Log` tab
(#244) and surfaces every alliance member whose count of flagged
events matches the query.

Pure-Python query function (`query_member_log`) is unit-testable in
isolation; the UI layer wraps it with the query-builder embed and a
discord.ui.View. Quota: one Sheet read per query, no caching at v1
per the ticket.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import discord

from messages import DENY_NOT_OWNER, PREMIUM_LOCKED_INLINE


logger = logging.getLogger(__name__)


# ── Operator + threshold options ─────────────────────────────────────────────

_OPERATOR_LABELS: list[tuple[str, str]] = [
    (">=", "Greater than or equal to"),
    (">", "Greater than"),
    ("==", "Equal to"),
    ("<=", "Less than or equal to"),
    ("<", "Less than"),
]

_OPERATOR_FUNCS = {
    ">": lambda c, t: c > t,
    ">=": lambda c, t: c >= t,
    "<": lambda c, t: c < t,
    "<=": lambda c, t: c <= t,
    "==": lambda c, t: c == t,
}

# Preset numeric values. Officers asked for "fast iteration" in the
# spec — preset Selects beat free-text inputs since Discord requires
# numerics to come from a modal, which would interrupt the flow.
_THRESHOLD_PRESETS = [1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20]
_LOOKBACK_PRESETS = [2, 4, 6, 8, 10, 12, 16, 20]

_TEAM_FILTER_CYCLE = ["both", "A", "B"]
_TEAM_FILTER_LABEL = {
    "both": "👥 Teams: both",
    "A": "👥 Teams: A only",
    "B": "👥 Teams: B only",
}

# Defaults when the view first opens. Officer iterates from here.
_DEFAULT_OP = ">="
_DEFAULT_THRESHOLD = 3
_DEFAULT_LOOKBACK = 8
_DEFAULT_TEAM = "both"
_MAX_RESULT_ROWS = 25


# ── Pure query function ──────────────────────────────────────────────────────


def query_member_log(
    guild_id: int,
    event_type: str,
    *,
    question_key: str,
    operator_sym: str,
    threshold: int,
    lookback_events: int,
    team_filter: str = "both",
) -> dict:
    """Run the trends query against the Per-Member Log tab.

    Returns a dict shaped like:
      {
        "results": [{"member": str, "count": int, "last_flagged": str}, ...],
        "total_events_captured": int,
        "truncated": bool,
      }

    `results` is sorted by count desc, member asc. `last_flagged`
    is the most recent event date where the member's value was
    truthy in the lookback window. `total_events_captured` is the
    distinct event-date count the query saw (useful for "no data
    yet" messaging when 0). `truncated` is True when the matching
    set was capped at `_MAX_RESULT_ROWS`.
    """
    import storm_log

    distinct_dates, by_member = storm_log.read_member_log_window(
        guild_id,
        event_type,
        lookback_events,
        question_key,
    )
    counts = storm_log.count_member_flags_in_window(
        guild_id,
        event_type,
        lookback_events,
        question_key,
    )

    op = _OPERATOR_FUNCS.get(operator_sym)
    if op is None:
        return {"results": [], "total_events_captured": 0, "truncated": False}

    filtered: list[tuple[str, int]] = [(m, c) for m, c in counts.items() if op(c, threshold)]

    # Team filter: optional. Reads the most recent saved team plan for
    # this event_type and intersects members. When no plan exists,
    # the filter no-ops (every match shown).
    if team_filter in ("A", "B"):
        plan_members = _team_plan_member_set(
            guild_id,
            event_type,
            team_filter,
        )
        if plan_members is not None:
            filtered = [(m, c) for m, c in filtered if m in plan_members]

    truthy_set: set[str] = set()  # for value normalisation
    results: list[dict] = []
    for member, count in filtered:
        last_flagged = ""
        for d in distinct_dates:  # distinct_dates is newest-first
            v = (by_member.get(member, {}) or {}).get(d, "")
            normalized = str(v).strip().lower()
            if normalized and normalized not in ("no", "false", "0"):
                last_flagged = d
                break
        results.append(
            {
                "member": member,
                "count": count,
                "last_flagged": last_flagged,
            }
        )
    _ = truthy_set

    results.sort(key=lambda r: (-r["count"], r["member"]))
    truncated = len(results) > _MAX_RESULT_ROWS
    if truncated:
        results = results[:_MAX_RESULT_ROWS]
    return {
        "results": results,
        "total_events_captured": len(distinct_dates),
        "truncated": truncated,
    }


def _log_truthy(v) -> bool:
    """A Per-Member Log cell counts as a hit when non-empty and not an explicit
    no. Matches the normalisation in `query_member_log` / member_stats."""
    s = str(v).strip().lower()
    return bool(s) and s not in ("no", "false", "0")


def member_attendance_summary(guild_id: int, event_type: str, lookback_events: int = 12) -> dict:
    """Per-member storm attendance over the last ``lookback_events`` events, for
    the Map Manager Storms page (#316).

    Reads the ``<DS|CS> Member Log`` attendance column and returns
    ``{ event_type, lookback_events, total_events, members: [{ member, attended,
    tracked, attendance_pct, last_attended }] }``, sorted by attendance % then
    name. ``tracked`` is the member's own logged-event count (matching
    ``/member_stats``); ``total_events`` is the window's distinct event count.
    Degrades to an empty member list (never raises). Not premium-gated — the read
    is officer-facing behind the service key.
    """
    import storm_log

    try:
        dates, by_member = storm_log.read_member_log_window(
            guild_id, event_type, lookback_events, storm_log.ATTENDANCE_QUESTION_KEY
        )
    except Exception as e:
        logger.warning("[TRENDS] attendance summary read failed guild=%s: %s", guild_id, e)
        dates, by_member = [], {}

    members: list[dict] = []
    for name, rows in by_member.items():
        if not name or not name.strip():
            continue
        attended = sum(1 for v in rows.values() if _log_truthy(v))
        tracked = len(rows)
        last_attended = max((d for d, v in rows.items() if _log_truthy(v)), default="")
        members.append(
            {
                "member": name,
                "attended": attended,
                "tracked": tracked,
                "attendance_pct": round(attended / tracked * 100) if tracked else 0,
                "last_attended": last_attended or None,
            }
        )
    members.sort(key=lambda m: (-m["attendance_pct"], m["member"].lower()))
    return {
        "event_type": event_type.lower(),
        "lookback_events": lookback_events,
        "total_events": len(dates),
        "members": members,
    }


def _team_plan_member_set(
    guild_id: int,
    event_type: str,
    team: str,
) -> Optional[set[str]]:
    """Return the set of member names committed to the given team on
    the most recent saved team plan. None when no plan exists for the
    event_type (filter then no-ops in `query_member_log`)."""
    import config
    from storm_date_helpers import most_recent_event_date

    date_clean = most_recent_event_date(guild_id, event_type)
    if not date_clean:
        return None
    plan = config.get_storm_team_plan(guild_id, event_type, date_clean, team)
    if not plan:
        return None
    members = set(plan.get("primaries", [])) | set(plan.get("subs", []))
    return members or None


# ── Question discovery ──────────────────────────────────────────────────────


def list_trendable_questions(
    guild_id: int,
    event_type: str,
) -> list[dict]:
    """Return the question options the Trends Viewer can query.

    Each entry: `{key, label, source}`. Sources:
      - `attendance` — the `showed_up` column written by #245.
      - `participation` — roster_multi_select / derived_count
        questions from the participation config.
    """
    import storm_log
    from config import get_participation_config

    items: list[dict] = [
        {
            "key": storm_log.ATTENDANCE_QUESTION_KEY,
            "label": "Did this member show up? (attendance)",
            "source": "attendance",
        }
    ]

    pcfg = get_participation_config(guild_id, event_type)
    for q in pcfg.get("questions") or []:
        qtype = q.get("type", "")
        # Only roster multi-select is trendable. Derived count writes
        # the count itself (an integer) to the Sheet, not a yes/no
        # flag — querying "count of flagged events" on top of that is
        # confusing. Officers who want trend insight on a derived
        # count's underlying behaviour should query its source
        # question instead.
        if qtype != "roster_multi_select":
            continue
        key = q.get("key", "")
        label = q.get("label", key)
        if not key or key == storm_log.ATTENDANCE_QUESTION_KEY:
            # Skip duplicate showed_up (preset templates write it under
            # the same key — surface attendance entry once.)
            continue
        items.append({"key": key, "label": label, "source": "participation"})

    return items


# ── Result rendering ────────────────────────────────────────────────────────


def _operator_word(op: str) -> str:
    for sym, label in _OPERATOR_LABELS:
        if sym == op:
            return label.lower()
    return op


def render_results_text(
    *,
    event_type: str,
    question_label: str,
    operator_sym: str,
    threshold: int,
    lookback_events: int,
    team_filter: str,
    query_out: dict,
) -> str:
    """Plaintext block officers can copy into in-game mail. Avoids
    em-dashes per the Discord-post style memory."""
    label = "Desert Storm" if event_type == "DS" else "Canyon Storm"
    op_word = _operator_word(operator_sym)
    team_bit = ""
    if team_filter == "A":
        team_bit = " (Team A only)"
    elif team_filter == "B":
        team_bit = " (Team B only)"
    lines = [
        f"{label} trends, {question_label}",
        f"Members with {op_word} {threshold} flagged events "
        f"in the past {lookback_events} events{team_bit}:",
        "",
    ]
    results = query_out.get("results") or []
    if not results:
        lines.append("(no members found for this search)")
    else:
        for r in results:
            last = r.get("last_flagged") or "n/a"
            lines.append(f"  {r['member']}: {r['count']} (last: {last})")
        if query_out.get("truncated"):
            lines.append(
                f"  (+ more not shown, capped at {_MAX_RESULT_ROWS}, narrow your search to see all)"
            )
    return "\n".join(lines)


# ── View state ──────────────────────────────────────────────────────────────


class _TrendsState:
    """In-memory state for one officer's Trends session."""

    def __init__(
        self,
        *,
        guild_id: int,
        user_id: int,
        event_type: str,
        questions: list[dict],
    ):
        self.guild_id = guild_id
        self.user_id = user_id
        self.event_type = event_type
        self.questions = questions
        self.question_key = questions[0]["key"] if questions else ""
        self.operator = _DEFAULT_OP
        self.threshold = _DEFAULT_THRESHOLD
        self.lookback = _DEFAULT_LOOKBACK
        self.team_filter = _DEFAULT_TEAM
        # Result snapshot from the last `View trends` click. None until
        # the first run; refreshed every time.
        self.last_query: Optional[dict] = None

    def question_label(self) -> str:
        for q in self.questions:
            if q["key"] == self.question_key:
                return q["label"]
        return self.question_key


def _render_builder_embed(state: _TrendsState) -> discord.Embed:
    label = "Desert Storm" if state.event_type == "DS" else "Canyon Storm"
    embed = discord.Embed(
        title=f"🔍 {label} Trends Viewer",
        color=(discord.Color.gold() if state.event_type == "DS" else discord.Color.orange()),
    )
    if not state.questions:
        embed.description = (
            "_No trendable questions configured yet. The Trends Viewer "
            "needs at least one **Roster multi-select** question on "
            "the participation flow, or attendance records from the "
            f"**📋 Record attendance** button. Run `/setup → "
            f"{'⚔️ Desert Storm' if state.event_type == 'DS' else '🏜️ Canyon Storm'}` "
            "to add a question, or record attendance for a past event "
            "first._"
        )
        return embed

    op_word = _operator_word(state.operator)
    team_bit = ""
    if state.team_filter == "A":
        team_bit = " · Team A only"
    elif state.team_filter == "B":
        team_bit = " · Team B only"
    embed.description = (
        f"**Question:** {state.question_label()}\n"
        f"**Show members with:** {op_word} **{state.threshold}** "
        f"events flagged{team_bit}\n"
        f"**In the past:** {state.lookback} events captured\n"
        "\n_Pick the dropdowns to refine your search, then click "
        "**🔍 View trends**._"
    )
    return embed


def _render_results_embed(state: _TrendsState) -> discord.Embed:
    label = "Desert Storm" if state.event_type == "DS" else "Canyon Storm"
    embed = discord.Embed(
        title=f"🔍 {label} Trends",
        color=(discord.Color.gold() if state.event_type == "DS" else discord.Color.orange()),
    )
    q = state.last_query or {}
    results = q.get("results") or []
    total_events = q.get("total_events_captured", 0)
    op_word = _operator_word(state.operator)
    team_bit = ""
    if state.team_filter == "A":
        team_bit = " · Team A only"
    elif state.team_filter == "B":
        team_bit = " · Team B only"

    header_lines = [
        f"**{state.question_label()}** — {op_word} **{state.threshold}** "
        f"events flagged in past **{state.lookback}** events{team_bit}",
    ]
    if total_events == 0:
        header_lines.append(
            "_No events with data for this question yet. Record at "
            "least one event before searching trends._"
        )
        embed.description = "\n".join(header_lines)
        return embed
    if total_events < state.lookback:
        # Lookback exceeds the captured history — render what we have
        # and let the officer know. Trend results still come from the
        # full captured range; just framed as "we can't reach back
        # that far."
        header_lines.append(
            f"_Only {total_events} captured event(s) available. "
            f"We cannot show the full {state.lookback} you "
            f"requested._"
        )
    header_lines.append(f"\n**{len(results)} member(s) match.**")

    if not results:
        header_lines.append(
            "\n_No members found for this search. Try searching again "
            "with different options to widen the search._"
        )
        embed.description = "\n".join(header_lines)
        return embed

    # Plain text table — Discord renders monospace inside code blocks.
    table_lines = ["```", f"{'Member':<24} {'Count':>5}  {'Last flagged':<12}"]
    for r in results:
        member = r["member"][:23]
        count = r["count"]
        last = r.get("last_flagged") or "n/a"
        table_lines.append(f"{member:<24} {count:>5}  {last:<12}")
    table_lines.append("```")
    header_lines.append("\n".join(table_lines))

    if q.get("truncated"):
        header_lines.append(
            f"_+ more matches not shown, capped at {_MAX_RESULT_ROWS}. "
            "Narrow your search to see all._"
        )

    embed.description = "\n".join(header_lines)
    return embed


# ── View ────────────────────────────────────────────────────────────────────


class _TrendsView(discord.ui.View):
    """Query builder + results display. Owner-gated."""

    def __init__(self, state: _TrendsState):
        super().__init__(timeout=900)
        self.state = state
        self.message: Optional[discord.Message] = None
        self._build()

    def _build(self) -> None:
        self.clear_items()
        s = self.state
        if not s.questions:
            # Nothing to query — render the empty-state embed alone,
            # don't surface dead components.
            return

        # Row 0 — question Select. Cap at 25 options (Discord limit).
        q_opts = [
            discord.SelectOption(
                label=q["label"][:100],
                value=q["key"][:100],
                default=(q["key"] == s.question_key),
            )
            for q in s.questions[:25]
        ]
        q_select = discord.ui.Select(
            placeholder=f"Question: {s.question_label()[:90]}",
            options=q_opts,
            min_values=1,
            max_values=1,
            row=0,
        )
        q_select.callback = self._on_question
        self.add_item(q_select)

        # Row 1 — operator Select.
        op_opts = [
            discord.SelectOption(
                label=label,
                value=sym,
                default=(sym == s.operator),
            )
            for sym, label in _OPERATOR_LABELS
        ]
        op_select = discord.ui.Select(
            placeholder=f"Show members with: {_operator_word(s.operator)}",
            options=op_opts,
            min_values=1,
            max_values=1,
            row=1,
        )
        op_select.callback = self._on_operator
        self.add_item(op_select)

        # Row 2 — threshold Select.
        th_opts = [
            discord.SelectOption(
                label=str(n),
                value=str(n),
                default=(n == s.threshold),
            )
            for n in _THRESHOLD_PRESETS
        ]
        th_select = discord.ui.Select(
            placeholder=f"Threshold: {s.threshold} flagged events",
            options=th_opts,
            min_values=1,
            max_values=1,
            row=2,
        )
        th_select.callback = self._on_threshold
        self.add_item(th_select)

        # Row 3 — lookback Select.
        lb_opts = [
            discord.SelectOption(
                label=str(n),
                value=str(n),
                default=(n == s.lookback),
            )
            for n in _LOOKBACK_PRESETS
        ]
        lb_select = discord.ui.Select(
            placeholder=f"Lookback: past {s.lookback} captured events",
            options=lb_opts,
            min_values=1,
            max_values=1,
            row=3,
        )
        lb_select.callback = self._on_lookback
        self.add_item(lb_select)

        # Row 4 — team toggle + run + copy.
        team_btn = discord.ui.Button(
            label=_TEAM_FILTER_LABEL[s.team_filter],
            style=discord.ButtonStyle.secondary,
            row=4,
        )
        team_btn.callback = self._on_team_cycle
        self.add_item(team_btn)

        run_btn = discord.ui.Button(
            label="🔍 View trends",
            style=discord.ButtonStyle.primary,
            row=4,
        )
        run_btn.callback = self._on_run
        self.add_item(run_btn)

        copy_btn = discord.ui.Button(
            label="📋 Copy as text",
            style=discord.ButtonStyle.secondary,
            row=4,
            disabled=(s.last_query is None),
        )
        copy_btn.callback = self._on_copy
        self.add_item(copy_btn)

    # ── Owner guard ─────────────────────────────────────────────────────
    async def _guard(self, inter: discord.Interaction) -> bool:
        if inter.user.id != self.state.user_id:
            await inter.response.send_message(
                DENY_NOT_OWNER,
                ephemeral=True,
            )
            return False
        return True

    # ── Callbacks ───────────────────────────────────────────────────────
    async def _redraw(self, inter: discord.Interaction, *, results: bool = False):
        self._build()
        embed = _render_results_embed(self.state) if results else _render_builder_embed(self.state)
        await inter.response.edit_message(embed=embed, view=self)

    async def _on_question(self, inter: discord.Interaction):
        if not await self._guard(inter):
            return
        sel: discord.ui.Select = inter.data["values"]  # type: ignore
        self.state.question_key = sel[0] if sel else self.state.question_key
        self.state.last_query = None  # invalidate stale results
        await self._redraw(inter)

    async def _on_operator(self, inter: discord.Interaction):
        if not await self._guard(inter):
            return
        sel: list = inter.data.get("values") or []
        if sel:
            self.state.operator = sel[0]
        await self._redraw(inter)

    async def _on_threshold(self, inter: discord.Interaction):
        if not await self._guard(inter):
            return
        sel: list = inter.data.get("values") or []
        if sel:
            try:
                self.state.threshold = int(sel[0])
            except ValueError:
                pass
        await self._redraw(inter)

    async def _on_lookback(self, inter: discord.Interaction):
        if not await self._guard(inter):
            return
        sel: list = inter.data.get("values") or []
        if sel:
            try:
                self.state.lookback = int(sel[0])
            except ValueError:
                pass
        await self._redraw(inter)

    async def _on_team_cycle(self, inter: discord.Interaction):
        if not await self._guard(inter):
            return
        cur = self.state.team_filter
        try:
            idx = _TEAM_FILTER_CYCLE.index(cur)
        except ValueError:
            idx = 0
        self.state.team_filter = _TEAM_FILTER_CYCLE[(idx + 1) % len(_TEAM_FILTER_CYCLE)]
        await self._redraw(inter)

    async def _on_run(self, inter: discord.Interaction):
        if not await self._guard(inter):
            return
        # Defer so the gspread read doesn't blow the 3-second window.
        await inter.response.defer()
        try:
            out = await asyncio.to_thread(
                query_member_log,
                self.state.guild_id,
                self.state.event_type,
                question_key=self.state.question_key,
                operator_sym=self.state.operator,
                threshold=self.state.threshold,
                lookback_events=self.state.lookback,
                team_filter=self.state.team_filter,
            )
        except Exception as e:
            logger.warning(
                "[STORM TRENDS] query failed for guild=%s: %s",
                self.state.guild_id,
                e,
            )
            await inter.followup.send(
                f"⚠️ Couldn't read the Member Log: {e}\nTry again in a moment.",
                ephemeral=True,
            )
            return
        self.state.last_query = out
        self._build()
        await inter.edit_original_response(
            embed=_render_results_embed(self.state),
            view=self,
        )

    async def _on_copy(self, inter: discord.Interaction):
        if not await self._guard(inter):
            return
        text = render_results_text(
            event_type=self.state.event_type,
            question_label=self.state.question_label(),
            operator_sym=self.state.operator,
            threshold=self.state.threshold,
            lookback_events=self.state.lookback,
            team_filter=self.state.team_filter,
            query_out=self.state.last_query or {},
        )
        # Wrap in a code block so spacing survives Discord's renderer.
        wrapped = f"```\n{text[:1990]}\n```"
        await inter.response.send_message(wrapped, ephemeral=True)

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


# ── Entry point (hub button handler) ────────────────────────────────────────


async def handle_storm_trends(
    bot,
    interaction: discord.Interaction,
    event_type: str,
) -> None:
    """Wired from the `🔍 View trends across events` button on the
    `/desertstorm` and `/canyonstorm` event hubs (storm_event_hub.py).
    Premium-gated, leadership-only."""
    from storm_permissions import (
        is_leader_or_admin,
        deny_non_leader,
    )
    import premium

    if not is_leader_or_admin(interaction):
        await deny_non_leader(interaction)
        return

    if not interaction.guild_id:
        await interaction.response.send_message(
            "⚠️ This command must be used inside a server.",
            ephemeral=True,
        )
        return

    if not await premium.is_premium(
        interaction.guild_id,
        interaction=interaction,
        bot=bot,
    ):
        await interaction.response.send_message(
            PREMIUM_LOCKED_INLINE.format(feature="Trends Viewer"),
            ephemeral=True,
        )
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    questions = await asyncio.to_thread(
        list_trendable_questions,
        interaction.guild_id,
        event_type,
    )
    state = _TrendsState(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        event_type=event_type,
        questions=questions,
    )
    view = _TrendsView(state)
    msg = await interaction.followup.send(
        embed=_render_builder_embed(state),
        view=view,
        ephemeral=True,
    )
    view.message = msg
