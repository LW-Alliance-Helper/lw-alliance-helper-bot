"""
Clock and calendar text for the wizards: the 12-hour time parser and its
inverse, the time-with-timezone renderer, the month-day parser behind the
event wizard, and the sign-up schedule sub-flow (weekday + time with Keep
current) the storm structured-flow step asks through.

Moved out of `setup_cog.py` in round 2 of the skills walkthrough (#611)
after the shared wizard pieces (`wizard_steps.py`): the events hub, the
survey and train hubs and the storm companion all imported these from
`setup_cog`, which is the second-use rule's trigger. `setup_cog` imports
every name back, so `from setup_cog import X` still resolves; a new caller
imports from here. The names keep their leading underscore for now, so the
move is a move; renaming them is a sweep of its own.
"""

import discord

import wizard_registry
import wizard_steps
from messages import GENERIC_CMD_TIMEOUT
from storm_event_hub import HUB_BTN_POST_SIGNUP, HUB_COMMAND
from wizard_registry import wait_view_or_cancel


def _parse_12h_time(raw: str) -> str:
    """
    Parse a user-entered time like '10:15pm', '9am', '9:00 AM' into
    HH:MM 24h string for storage. Returns None if unparseable.
    """
    import re

    raw = raw.strip().lower().replace(" ", "")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(am|pm)$", raw)
    if not m:
        return None
    hour, minute, period = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if hour < 1 or hour > 12 or minute < 0 or minute > 59:
        return None
    if period == "am":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return f"{hour:02d}:{minute:02d}"


def _format_24h_to_12h(raw: str) -> str:
    """Inverse of `_parse_12h_time`: render a stored 'HH:MM' 24-hour
    value as e.g. '9:00am' for display in wizards. Pass-through on
    empty / unparseable input so callers can pipe it through unchanged
    when no saved value is present. Used wherever a setup step shows
    a saved time back to leadership and the default it could revert
    to is in 12-hour form — otherwise the 'Keep current' and 'Use
    default' buttons sit side-by-side in mismatched formats."""
    if not raw or ":" not in raw:
        return raw or ""
    try:
        h_str, m_str = raw.split(":", 1)
        hour, minute = int(h_str), int(m_str)
    except ValueError:
        return raw
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return raw
    period = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d}{period}"


def _format_time_with_tz(time_str: str, tz_name: str | None) -> str:
    """Render a stored 'HH:MM' 24-hour time as e.g. '8:00am EDT' using
    the guild's configured timezone. Used everywhere a wizard summary
    or `/setup` → 🗂️ View configuration shows a saved time back to leadership —
    bare '08:00' leaves them guessing which timezone the reminder
    fires in.

    The tz abbreviation comes from `dt.tzname()` anchored on today's
    date, so the suffix reflects DST for the current date ('EST' in
    winter vs. 'EDT' in summer).

    Falls back gracefully:
      * empty / `*not set*` / non-time strings → returned unchanged,
        so callers can pipe sentinels through without a separate guard;
      * unparseable HH:MM → returned unchanged;
      * unknown tz_name → bare 12-hour form without a tz suffix.
    """
    if not time_str or ":" not in str(time_str):
        return time_str or ""
    try:
        h_str, m_str = str(time_str).split(":", 1)
        hour, minute = int(h_str), int(m_str)
    except (ValueError, AttributeError):
        return time_str
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return time_str
    period = "am" if hour < 12 else "pm"
    hour12 = hour % 12 or 12
    base = f"{hour12}:{minute:02d}{period}"
    if not tz_name:
        return base
    try:
        from datetime import datetime
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
        today = datetime.now(tz=tz).date()
        dt = datetime(today.year, today.month, today.day, hour, minute, tzinfo=tz)
    except Exception:
        return base
    abbr = dt.tzname()
    return f"{base} {abbr}" if abbr else base


def _parse_month_day(raw: str, *, today=None) -> str | None:
    """
    Parse an officer-typed date into YYYY-MM-DD using the most recent
    occurrence. Always looks backward — never assumes a far-future date.
    Examples (today = April 25 2026):
      'February 20' → 2026-02-20  (already passed this year)
      'December 3'  → 2025-12-03  (hasn't happened yet this year, so last year)
      'May 2'       → 2026-05-02  (upcoming this year, but within ~5 days so still this year)
    Rule: if the date this year is more than 31 days out, use last year.

    Format tolerance is delegated to `storm_date_helpers.parse_event_date`
    (the canonical permissive parser), so `7/30`, `2026-07-30`, `July 30th`,
    `30 Jul`, `today` and weekday names all parse — not just `Month Day`.
    Only the year-selection rule is ours: `parse_event_date` infers
    the *next* occurrence, which is wrong for an anchor date that leans
    backward, so we re-derive the year from the parsed month/day.

    `today` is injectable for tests; production callers omit it.
    """
    import re

    from storm_date_helpers import parse_event_date
    from time_helpers import server_today

    if not raw or not raw.strip():
        return None
    today = today or server_today()
    parsed = parse_event_date(raw, today=today)
    if parsed is None:
        return None

    # An explicit 4-digit year is the officer's word — take it as typed.
    if re.search(r"\d{4}", raw):
        return parsed.isoformat()

    try:
        this_year = parsed.replace(year=today.year)
    except ValueError:
        # Feb 29 typed in a non-leap current year — keep the parser's date.
        return parsed.isoformat()
    # Allow up to 31 days in the future (next upcoming event within a month).
    # Anything further out uses last year's date.
    if (this_year - today).days > 31:
        try:
            return this_year.replace(year=today.year - 1).isoformat()
        except ValueError:
            return this_year.isoformat()
    return this_year.isoformat()


def _normalise_hhmm(raw: str) -> str | None:
    """Thin wrapper around `config.parse_storm_signup_time` kept under
    the wizard's old name so existing tests / imports keep working.
    The canonical implementation lives in `config.py` so the scheduler
    and the wizard can't drift on parsing rules."""
    from config import parse_storm_signup_time

    return parse_storm_signup_time(raw)


# ── Auto-schedule sub-flow (#131) ────────────────────────────────────────────


_DOW_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


async def _ask_signup_schedule(
    channel,
    bot,
    user,
    cancel_event,
    *,
    label: str,
    cmd_name: str,
    current_dow: int,
    current_time: str,
    tz_label: str = "",
    event_type: str = "DS",
) -> dict | None:
    """Two-step Premium sub-flow for the auto-scheduler config:
        * Poll day-of-week (dropdown; or "Skip auto-scheduling").
          Event day is game-defined (DS = Friday, CS = Thursday); the
          dropdown only shows poll days that sit between the previous
          event and the in-game roster lock.
        * Sign-up post time (HH:MM in guild timezone; modal). Required
          when a day is picked — Rule F / #163.

    Returns `{"dow": int, "time": str}`. `dow = -1` indicates the
    alliance explicitly opted out of auto-scheduling (manual
    `/<parent> post_signup` remains usable). Returns None on cancel
    or timeout — callers should propagate the None.
    """
    import wizard_registry

    # Per Rule H, valid poll days per event type sit between the day
    # AFTER the previous event and the day BEFORE the in-game roster
    # lock. Event days themselves are excluded (game-defined: DS=Fri,
    # CS=Thu) so same-day poll/event is impossible by construction.
    # 0=Monday..6=Sunday.
    if event_type == "DS":
        # DS event = Friday; roster locks Wednesday before reset.
        # Valid poll days: Sat, Sun, Mon, Tue, Wed.
        poll_options = [5, 6, 0, 1, 2]
        event_label = "Friday"
    else:
        # CS event = Thursday; roster locks Monday before reset.
        # Valid poll days: Fri, Sat, Sun, Mon.
        poll_options = [4, 5, 6, 0]
        event_label = "Thursday"

    # ── Step 1: poll day-of-week ──
    class _DowView(discord.ui.View):
        def __init__(self, current: int):
            super().__init__(timeout=300)
            self.selected: int | None = None
            self.cancelled = False

            # Keep-current button (#80 pattern). Re-selecting an already
            # default-marked dropdown option doesn't read as "save" to
            # leadership, so the picker also exposes an explicit
            # Keep-current affordance like every other /setup_* re-entry
            # surface. Label reflects whatever the saved value is: the
            # day name when a poll day was configured, "Skip
            # auto-scheduling" when the alliance opted out previously.
            if 0 <= current <= 6 and current in poll_options:
                keep_label = f"Keep current: {_DOW_NAMES[current]}"
            else:
                keep_label = "Keep current: Skip auto-scheduling"
            keep_btn = discord.ui.Button(
                label=keep_label[:80],
                style=discord.ButtonStyle.success,
                row=0,
            )

            async def _on_keep(inter: discord.Interaction):
                self.selected = current if (0 <= current <= 6 and current in poll_options) else -1
                for item in self.children:
                    item.disabled = True
                if self.selected < 0:
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=(
                            f"✅ Auto-scheduling stays skipped. Post "
                            f"manually via `{HUB_COMMAND[event_type]}` → "
                            f"**{HUB_BTN_POST_SIGNUP}** when you're ready."
                        ),
                        view=self,
                    )
                else:
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=f"✅ Keeping poll day: **{_DOW_NAMES[self.selected]}**.",
                        view=self,
                    )
                self.stop()

            keep_btn.callback = _on_keep
            self.add_item(keep_btn)

            options = [
                discord.SelectOption(
                    label=_DOW_NAMES[i],
                    value=str(i),
                    default=(i == current),
                )
                for i in poll_options
            ]
            options.append(
                discord.SelectOption(
                    label="Skip auto-scheduling (post manually from the hub)",
                    value="-1",
                    default=(current < 0),
                )
            )
            sel = discord.ui.Select(
                placeholder="When should the bot post the sign-up poll?",
                min_values=1,
                max_values=1,
                options=options,
                row=1,
            )

            async def _on_pick(inter: discord.Interaction):
                try:
                    self.selected = int(sel.values[0])
                except ValueError:
                    self.selected = -1
                for item in self.children:
                    item.disabled = True
                if self.selected < 0:
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=(
                            f"✅ Auto-scheduling skipped. Post manually "
                            f"via `{HUB_COMMAND[event_type]}` → "
                            f"**{HUB_BTN_POST_SIGNUP}** when you're ready."
                        ),
                        view=self,
                    )
                else:
                    await wizard_registry.safe_edit_response(
                        inter,
                        content=f"✅ Poll day: **{_DOW_NAMES[self.selected]}**.",
                        view=self,
                    )
                self.stop()

            sel.callback = _on_pick
            self.add_item(sel)

    dow_view = _DowView(int(current_dow if current_dow is not None else -1))
    await channel.send(
        f"**Auto-Schedule: Poll Day (💎 Premium)**\n"
        f"**{label}** runs every **{event_label}** in-game. Which day "
        f"do you want the bot to post the sign-up poll? (The dropdown "
        f"shows only days that sit between the previous event and the "
        f"in-game roster lock.)",
        view=dow_view,
    )
    await wait_view_or_cancel(dow_view, cancel_event)
    if dow_view.cancelled:
        return None
    if dow_view.selected is None:
        await channel.send(GENERIC_CMD_TIMEOUT.format(cmd=cmd_name))
        return None
    if dow_view.selected < 0:
        # Skipped — return cleared schedule.
        return {"dow": -1, "time": ""}

    # ── Step 3: sign-up time ──
    # The alliance opted into auto-scheduling at Step 1 (`dow >= 0`),
    # so the time field is required (#163 / Rule F). Empty submissions
    # surface a one-line re-prompt and the modal re-opens.
    #
    # Time copy follows the existing convention used by train / birthday
    # / shiny setup: 12-hour clock for display + parsing, with the
    # guild's local timezone surfaced inline. Storage stays 24-hour HH:MM
    # so the scheduler doesn't have to disambiguate at fire time.
    saved_12h = _format_24h_to_12h(current_time) if current_time else ""
    tz_hint = f" *(in your timezone: {tz_label})*" if tz_label else ""
    time_clean: str | None = None
    for attempt in range(3):
        time_picked = await wizard_steps.ask_keep_or_change(
            channel,
            f"**Auto-Schedule: Sign-Up Post Time**\n"
            f"What time should the bot fire the sign-up post?{tz_hint}\n"
            f"*(e.g. `2:00pm`, `9:00am`, or 24-hour `14:00`)*",
            default="12:00pm",
            current=saved_12h,
            modal_title="Sign-Up Time",
            modal_label="e.g. 2:00pm",
            timeout_cmd=cmd_name,
            cancel_event=cancel_event,
        )
        if time_picked is None:
            return None
        raw = str(time_picked).strip()
        if raw:
            time_clean = _parse_12h_time(raw) or _normalise_hhmm(raw) or "12:00"
            break
        # Empty submission — surface a friendly nudge and re-prompt.
        await channel.send(
            "⚠️ A sign-up time is required when auto-scheduling is on. "
            "Pick a time (e.g. `12:00pm`) or use the default."
        )
    if time_clean is None:
        # Three blank attempts in a row — fall back to the default so
        # the wizard doesn't loop forever.
        time_clean = _parse_12h_time("12:00pm") or "12:00"

    return {
        "dow": dow_view.selected,
        "time": time_clean,
    }
