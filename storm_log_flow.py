"""
The participation log walk for Desert Storm and Canyon Storm: the officer
answers the date and then one question per entry in the alliance's
participation config, the event row is appended to the participation tab,
per-member answers go to the Per-Member Log tab (#244), and a summary is
posted and mirrored to the configured log channel.

Split out of `storm_log.py` in the #589 step 11 refactor, following the
`storm_setup_structured.py` shape: the data layer, the views and the
`/desertstorm` / `/canyonstorm` handlers stay in `storm_log`, which imports
`run_log_flow` from here. This module reaches back into `storm_log` through
`_log()` at call time for the views, the Sheet helpers and `active_logs`,
so importing either module first works and every `patch("storm_log.X")` in
the tests keeps its target.

Shape:

* `_Walk` carries the handles every step shares (bot, channel, officer,
  cancel event, labels), the lazily loaded roster, and the answers being
  built up.
* `_ask_date` and one `_q_*` function per question type, dispatched by
  `_HANDLERS`; an unknown type is free text, as before.
* A cancel or timeout raises `_Abort` after posting its notice;
  `run_log_flow` catches it. That replaces the twelve-deep nesting of
  per-branch `if raw is None: ... return` exits inside the question loop.
* `_ask_until` is the one retry loop behind the numeric and date questions.

Every prompt and notice is unchanged from the function this replaced;
`tests/integration/test_storm_log_flow.py` was written against that
function and holds this one to it.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable

import discord

from setup_hub import STORM_SETUP_NAV
from storm_event_hub import HUB_COMMAND, HUB_BTN_PARTICIPATION
from time_helpers import server_today


def _log():
    """The `storm_log` module, resolved at call time (see the module docstring)."""
    import storm_log

    return storm_log


class _Abort(Exception):
    """The walk ended early: cancelled, timed out, gave up, or failed to
    save. Whatever the officer needed to read has already been posted."""


# ── Pure helpers ─────────────────────────────────────────────────────────────


def bound_hint(lo, hi) -> str:
    """The ` *(min `1`, max `10`)*` suffix on a numeric prompt, or ''."""
    if lo is None and hi is None:
        return ""
    bits = []
    if lo is not None:
        bits.append(f"min `{lo}`")
    if hi is not None:
        bits.append(f"max `{hi}`")
    return f" *({', '.join(bits)})*"


def parse_numeric(raw: str, lo, hi) -> tuple[str | None, str | None]:
    """`(value, None)` for a number inside the bounds, else `(None, why)`.
    An integer stays an integer; a decimal point makes it a float."""
    try:
        n = float(raw) if "." in raw else int(raw)
    except ValueError:
        return None, f"⚠️ `{raw}` isn't a number. Please re-enter your answer."
    if lo is not None and n < lo:
        return None, f"⚠️ Must be at least **{lo}**. Please re-enter."
    if hi is not None and n > hi:
        return None, f"⚠️ Must be at most **{hi}**. Please re-enter."
    return str(n), None


def parse_answer_date(raw: str, fmt: str) -> tuple[str | None, str | None]:
    """`(iso_date, None)` when `raw` matches `fmt`, else `(None, why)`."""
    try:
        return datetime.strptime(raw, fmt).date().isoformat(), None
    except ValueError:
        return None, f"⚠️ `{raw}` doesn't match `{fmt}`. Please re-enter."


def roster_preview(names: list[str]) -> str:
    return ", ".join(names) if len(names) <= 25 else f"{len(names)} members loaded"


def prefill_preview(preselected: set[str]) -> str:
    """Up to five pre-checked names, then `(+N more)`; '' when none."""
    if not preselected:
        return ""
    names = sorted(preselected)
    more = f" (+{len(names) - 5} more)" if len(names) > 5 else ""
    return ", ".join(names[:5]) + more


def top_counts(counts: dict[str, int], n: int = 5) -> list[str]:
    """`Name (count)` for the top `n` non-zero counts, highest first."""
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [f"{name} ({c})" for name, c in ordered[:n] if c > 0]


def summary_text(label: str, log_date: date, questions: list[dict], answers: dict) -> str:
    date_str = f"{log_date:%A, %B} {log_date.day}, {log_date.year}"
    lines = [f"📋 **{label} Log: {date_str}**"]
    for q in questions:
        qkey = q.get("key", "")
        qlabel = q.get("label", qkey)
        v = answers.get(qkey, "")
        lines.append(f"**{qlabel}:** {v if v not in ('', None) else 'None'}")
    return "\n".join(lines)


# ── The walk's shared state ──────────────────────────────────────────────────


@dataclass
class _Walk:
    bot: discord.Client
    channel: discord.abc.Messageable
    user: discord.abc.User
    event_type: str
    guild_id: int | None
    cancel_event: asyncio.Event
    label: str
    log_hint: str
    setup_cmd: str
    log_date: date | None = None
    names: list[str] = field(default_factory=list)
    alias_map: dict[str, str] = field(default_factory=dict)
    roster_loaded: bool = False
    # Event-level answers go to the participation tab; per-(member,
    # question) values from `roster_multi_select` and `derived_count`
    # go to the Per-Member Log tab (#244). Different tabs at save time.
    answers: dict[str, str] = field(default_factory=dict)
    per_member: dict[str, dict[str, str]] = field(default_factory=dict)
    per_member_keys: list[str] = field(default_factory=list)

    def _is_reply(self, m) -> bool:
        return m.author == self.user and m.channel == self.channel

    async def canceled(self):
        await self.channel.send("❌ Log canceled.")
        raise _Abort

    async def timed_out(self):
        await self.channel.send(f"⏰ Timed out. Run {self.log_hint} to start again.")
        raise _Abort

    async def give_up(self, text: str):
        await self.channel.send(text)
        raise _Abort

    async def ask_text(self, prompt: str) -> str:
        """Post `prompt`, wait for the officer's next message, and return it
        trimmed. The prompt and the reply are deleted afterwards so the
        channel does not fill with the walk. Raises `_Abort` on `/cancel`
        (with the cancel notice) or timeout (with the route back)."""
        prompt_msg = await self.channel.send(prompt)
        try:
            reply_task = asyncio.ensure_future(
                self.bot.wait_for("message", check=self._is_reply, timeout=_log().WIZARD_TIMEOUT)
            )
            cancel_task = asyncio.ensure_future(self.cancel_event.wait())
            done, pending = await asyncio.wait(
                [reply_task, cancel_task], return_when=asyncio.FIRST_COMPLETED
            )
            for t in pending:
                t.cancel()
            if self.cancel_event.is_set():
                try:
                    await prompt_msg.delete()
                except discord.HTTPException:
                    pass
                await self.canceled()
            reply = done.pop().result()
            try:
                await prompt_msg.delete()
                await reply.delete()
            except discord.HTTPException:
                pass
            return reply.content.strip()
        except asyncio.TimeoutError:
            await self.timed_out()

    async def ask_view(self, prompt: str, view):
        """Post `prompt` with `view` and wait for it. Raises `_Abort` on
        `/cancel` (the buttons are disabled in place, then the cancel
        notice) or timeout (`confirmed` still False: the prompt is deleted,
        then the route back). Returns the view for its answer."""
        prompt_msg = await self.channel.send(prompt, view=view)
        view_task = asyncio.ensure_future(view.wait())
        cancel_task = asyncio.ensure_future(self.cancel_event.wait())
        done, pending = await asyncio.wait(
            [view_task, cancel_task], return_when=asyncio.FIRST_COMPLETED
        )
        for t in pending:
            t.cancel()
        if self.cancel_event.is_set():
            for item in view.children:
                item.disabled = True
            try:
                await prompt_msg.edit(view=view)
            except discord.HTTPException:
                pass
            await self.canceled()
        if not getattr(view, "confirmed", True):
            try:
                await prompt_msg.delete()
            except discord.HTTPException:
                pass
            await self.timed_out()
        return view

    async def ask_until(self, prompt: str, parse: Callable, *, give_up: str, attempts: int = 5):
        """Ask `prompt` until `parse(raw)` returns `(value, None)`, posting
        the `why` from `(None, why)` each time. After `attempts` failures,
        posts `give_up` and raises `_Abort`."""
        while attempts > 0:
            raw = await self.ask_text(prompt)
            value, why = parse(raw)
            if why is None:
                return value
            attempts -= 1
            await self.channel.send(why)
        await self.give_up(give_up)

    async def ensure_roster(self):
        """Load the roster once, on the first question that needs it."""
        if self.roster_loaded:
            return
        loading_msg = await self.channel.send("⏳ Loading roster from your configured tab…")
        self.names, self.alias_map = await asyncio.get_event_loop().run_in_executor(
            None, _log().load_roster_from_config, self.guild_id, self.event_type
        )
        try:
            await loading_msg.delete()
        except discord.HTTPException:
            pass
        self.roster_loaded = True

    def record_per_member(self, key: str, value_for: Callable[[str], str]):
        """One value per roster member under `key`, for the Per-Member Log."""
        for member_name in self.names:
            self.per_member.setdefault(member_name, {})[key] = value_for(member_name)
        self.per_member_keys.append(key)


@dataclass(frozen=True)
class _Question:
    raw: dict
    key: str
    label: str
    header: str


# ── Step 1: the date ─────────────────────────────────────────────────────────


async def _ask_date(w: _Walk) -> date:
    """Always asked, never configurable. Recent saved event dates (storm
    sign-ups + structured rosters) come as a dropdown, since typing a date
    from scratch was a tester pain point; free text stays for backfilling
    events that pre-date the saved data."""
    sl = _log()
    recent_dates = await asyncio.get_event_loop().run_in_executor(
        None, sl._collect_recent_event_dates, w.guild_id, w.event_type
    )
    picker = await w.ask_view(
        "**Step 1: Event date**\nPick the date this log is for:",
        sl._LogDatePickerView(recent_dates),
    )
    if picker.cancelled:
        await w.canceled()
    if picker.picked_date is not None:
        return picker.picked_date

    raw = await w.ask_text("Type the date (e.g. `April 14`, `4/14`) or type `today`:")
    if raw.lower() == "today":
        # A storm is a game event, so "today" is the in-game (server) day.
        return server_today()
    from train import parse_date_and_name

    parsed, _, _ = parse_date_and_name(f"{raw} - placeholder")
    if not parsed:
        await w.give_up(f"⚠️ Could not parse `{raw}` as a date. Run {w.log_hint} to start again.")
    return parsed


# ── One handler per question type ────────────────────────────────────────────
#
# Each returns the event-level answer to record under the question's key,
# or `None` to record nothing for it.


async def _q_yes_no(w: _Walk, q: _Question) -> str:
    view = await w.ask_view(f"{q.header}\nPick one.", _log()._YesNoLogView())
    return "Yes" if view.value else "No"


async def _q_numeric(w: _Walk, q: _Question) -> str:
    lo, hi = q.raw.get("min"), q.raw.get("max")
    return await w.ask_until(
        f"{q.header}{bound_hint(lo, hi)}\nType a number.",
        lambda raw: parse_numeric(raw, lo, hi),
        give_up=(
            "⚠️ Too many invalid attempts. Canceling the log. "
            f"run {w.log_hint} when you're ready to try again."
        ),
    )


async def _q_roster_names(w: _Walk, q: _Question) -> str:
    await w.ensure_roster()
    if not w.names:
        await w.give_up(
            "⚠️ The configured roster tab is empty or unreachable. "
            f"Run `{w.setup_cmd}` "
            f"to update the roster source, then try again."
        )
    view = await w.ask_view(
        f"{q.header}\nPress **Enter Names** to type who applies. "
        f"Press **Skip** if none.\n*Roster: {roster_preview(w.names)}*",
        _log().NameEntryView(w.names, q.label, w.alias_map),
    )
    picked = sorted(view.selected)
    if view.unrecognized:
        picked += sorted(view.unrecognized)
    return ", ".join(picked)


async def _q_single_select(w: _Walk, q: _Question) -> str:
    opts = q.raw.get("options") or []
    if not opts:
        return ""
    view = await w.ask_view(f"{q.header}\nPick one.", _log().ShortSelectView(opts, q.label))
    return next(iter(view.selected), "")


async def _q_multi_select(w: _Walk, q: _Question) -> str:
    opts = q.raw.get("options") or []
    if not opts:
        return ""
    view = await w.ask_view(
        f"{q.header}\nPick any that apply.", _log().ShortSelectView(opts, q.label)
    )
    return ", ".join(sorted(view.selected))


async def _q_date(w: _Walk, q: _Question) -> str:
    fmt = q.raw.get("date_format") or "%m/%d/%Y"
    return await w.ask_until(
        f"{q.header} *(format `{fmt}`)*",
        lambda raw: parse_answer_date(raw, fmt),
        give_up="⚠️ Too many invalid attempts. Canceling the log.",
    )


async def _q_roster_multi_select(w: _Walk, q: _Question) -> str:
    """#244: a paginated multi-select over the roster. Every roster member
    gets a yes / no under this key in the Per-Member Log; the officer's
    omission is a meaningful "no", not missing data."""
    await w.ensure_roster()
    if not w.names:
        await w.give_up(
            "⚠️ The configured roster tab is empty or unreachable. "
            f"Run `{w.setup_cmd}` to update the roster source."
        )
    # Pre-fill (Premium only): members who voted to attend in the poll.
    preselected: set[str] = set()
    prefill_source = q.raw.get("prefill_source") or ""
    if prefill_source == "discord_poll":
        preselected = await asyncio.to_thread(
            _log()._prefill_from_discord_poll,
            w.guild_id,
            w.event_type,
            w.log_date.isoformat(),
            w.names,
            w.alias_map,
        )
    prompt_lines = [q.header]
    if prefill_source == "discord_poll":
        prompt_lines.append(
            "🗳️ Pre-checked members are those who voted to "
            "attend in the Discord signup poll. The legend "
            "`✏️` marks any member you toggle manually."
        )
        preview = prefill_preview(preselected)
        if preview:
            prompt_lines.append(f"*Pre-checked:* {preview}")
    prompt_lines.append(
        "Use the dropdown(s) to pick the members who match. Click ✅ Save when done."
    )
    view = await w.ask_view(
        "\n".join(prompt_lines),
        _log()._PaginatedRosterMultiSelectView(
            w.names, q.label, preselected=preselected, prefill_used=bool(prefill_source)
        ),
    )
    picked = view.selected_set
    w.record_per_member(q.key, lambda name: "yes" if name in picked else "no")
    return f"{len(picked)} member(s)"


async def _q_derived_count(w: _Walk, q: _Question) -> str | None:
    """#244, Premium: count each member's flags under the source question
    across the last N events and write the counts to the Per-Member Log.
    Override UI deferred to v2."""
    source_key = q.raw.get("source_question_key", "")
    lookback = int(q.raw.get("lookback_events", 4))
    if not source_key:
        await w.channel.send(
            f"⚠️ Derived count `{q.label}` has no source question configured. Skipping."
        )
        return None
    await w.ensure_roster()
    counts = await asyncio.get_event_loop().run_in_executor(
        None, _log().count_member_flags_in_window, w.guild_id, w.event_type, lookback, source_key
    )
    # Every roster member gets a row, 0 for those never seen in the source.
    w.record_per_member(q.key, lambda name: str(counts.get(name, 0)))
    if q.raw.get("show_during_log"):
        top = top_counts(counts)
        if top:
            await w.channel.send(
                f"{q.header}\n📊 Top by count in past {lookback} events: {', '.join(top)}"
            )
    return f"max {max(counts.values()) if counts else 0} (past {lookback} events)"


async def _q_text(w: _Walk, q: _Question) -> str:
    raw = await w.ask_text(f"{q.header}\nType your answer (or `skip` for none).")
    return "" if raw.lower() == "skip" else raw


_HANDLERS = {
    "yes_no": _q_yes_no,
    "numeric": _q_numeric,
    "roster_names": _q_roster_names,
    "single_select": _q_single_select,
    "multi_select": _q_multi_select,
    "date": _q_date,
    "roster_multi_select": _q_roster_multi_select,
    "derived_count": _q_derived_count,
    "text": _q_text,
}


# ── Save, summarise, mirror ──────────────────────────────────────────────────


async def _save(w: _Walk):
    sl = _log()
    await w.channel.send("💾 Saving log…")
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, sl.append_participation_row, w.guild_id, w.event_type, w.log_date, w.answers
        )
    except Exception as e:
        await w.give_up(f"⚠️ Error saving to sheet: {e}")

    # The event-level tab keeps the summary value; the Member Log tab keeps
    # the per-member detail the Trends Viewer (#246) and derived counts read.
    if w.per_member_keys and w.per_member:
        try:
            await asyncio.get_event_loop().run_in_executor(
                None,
                sl.upsert_member_log_rows,
                w.guild_id,
                w.event_type,
                w.log_date,
                w.per_member,
                w.per_member_keys,
            )
        except Exception as e:
            await w.channel.send(f"⚠️ Saved event row, but per-member log failed: {e}")


async def _summarise(w: _Walk, questions: list[dict], log_channel_id: int):
    summary = summary_text(w.label, w.log_date, questions, w.answers)
    await w.channel.send(f"✅ **Log saved!**\n\n{summary}")
    # Mirror into the configured log channel when it is a different one.
    try:
        if log_channel_id and w.channel.id != log_channel_id:
            target = w.bot.get_channel(log_channel_id)
            if target:
                await target.send(summary)
    except Exception as e:
        print(f"[LOG] Error mirroring summary to log channel: {e}")


# ── Entry point ──────────────────────────────────────────────────────────────


async def run_log_flow(bot, channel, user, event_type):
    """
    Walk leadership through the participation log flow. The questions
    asked are read from the per-guild participation config saved by
    the storm setup wizard (`/setup → ⚔️ Desert Storm` or `/setup → 🛡️ Canyon Storm`). The date is always asked
    first (mandatory, never configurable).
    """
    sl = _log()
    is_ds = event_type.upper() == "DS"
    hub_cmd = HUB_COMMAND["DS"] if is_ds else HUB_COMMAND["CS"]
    w = _Walk(
        bot=bot,
        channel=channel,
        user=user,
        event_type=event_type,
        guild_id=channel.guild.id if hasattr(channel, "guild") and channel.guild else None,
        cancel_event=asyncio.Event(),
        label="Desert Storm" if is_ds else "Canyon Storm",
        log_hint=f"`{hub_cmd}` → **{HUB_BTN_PARTICIPATION}**",
        # Post-#201: storm setup wizards live behind /setup hub buttons.
        setup_cmd=STORM_SETUP_NAV["DS" if is_ds else "CS"],
    )
    sl.active_logs[user.id] = w.cancel_event
    try:
        from config import get_participation_config

        pcfg = get_participation_config(w.guild_id, event_type) if w.guild_id else {}
        if not pcfg.get("enabled"):
            await channel.send(
                f"⚙️ Participation tracking isn't enabled for {w.label} yet. "
                f"Run `{w.setup_cmd}` and walk through Step 6 to define what you want to track."
            )
            return
        questions = pcfg.get("questions") or []
        if not questions:
            await channel.send(
                f"⚙️ Participation tracking is enabled but no questions are configured. "
                f"Run `{w.setup_cmd}` to add questions."
            )
            return
        try:
            await _walk(w, questions, int(pcfg.get("log_channel_id") or 0))
        except _Abort:
            pass
    finally:
        sl.active_logs.pop(user.id, None)


async def _walk(w: _Walk, questions: list[dict], log_channel_id: int):
    total_steps = len(questions) + 1  # +1 for the always-required date
    await w.channel.send(
        f"📋 **{w.label} Log** started by {w.user.mention}\n"
        f"*{total_steps} step(s) total. Use `/cancel` at any time to stop.*"
    )
    w.log_date = await _ask_date(w)
    for idx, raw in enumerate(questions, start=2):
        key = raw.get("key", f"q{idx}")
        label = raw.get("label", key)
        q = _Question(
            raw=raw, key=key, label=label, header=f"**Step {idx} of {total_steps}: {label}**"
        )
        handler = _HANDLERS.get(raw.get("type", "text"), _q_text)
        value = await handler(w, q)
        if value is not None:
            w.answers[key] = value
    await _save(w)
    await _summarise(w, questions, log_channel_id)
