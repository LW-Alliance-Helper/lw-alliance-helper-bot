"""
survey.py — Squad Powers Survey

A persistent button in the survey channel lets any alliance member submit
their squad powers. Clicking it opens a private thread, walks them through
the questions, then:
  - Updates their row in the Squad Powers sheet
  - Appends a timestamped row to the Survey History sheet
  - Archives the thread

`/survey` is a single hub command (embed + button grid, see
`survey_hub.py`). It replaced the `/survey overview | post | remind`
subcommand group: the list view, posting the button, reminders,
Add / Edit / Remove, and the translation helper are all buttons there now.
The `/setup` hub's 📋 Survey button opens the same surface.

Multi-survey support (Premium): a guild may have a "default" survey plus
any number of extras, each with its own questions, channel, intro message,
and reminder DM body. The persistent answer button is registered as a
DynamicItem so each extra survey gets its own button keyed by survey_id.
"""

import asyncio
import json
import os
import re
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from config import get_config
from messages import (
    HUB_TIMEOUT,
    NOT_SET_UP,
    TIME_PARSE_GIVE_UP,
    TIME_PARSE_RETRY,
)
from setup_hub import HUB_BTN_SURVEY
from survey_hub import SURVEY_HUB_BTN_POST, SURVEY_HUB_BTN_REMIND
import config_health
import wizard_registry

# #379: the channel the scheduled survey reminder posts to.
SURVEY_REMINDER_CHANNEL_SUBJECT = "survey.reminder_channel"

config_health.register(
    config_health.Subject(
        key=SURVEY_REMINDER_CHANNEL_SUBJECT,
        label="your survey reminder channel",
        fix_hub="/setup",
        fix_btn=HUB_BTN_SURVEY,
    )
)

# ── Config ─────────────────────────────────────────────────────────────────────

SURVEY_TIMEOUT = 600  # 10 minutes per step

# ── Magnitude-aware numeric parsing ───────────────────────────────────────────

# Suffixes a player might type instead of typing the full nine-digit number —
# `300m` / `300mil` / `1.2b` / `5k`. Case-insensitive at the call site.
_SUFFIX_MULTIPLIERS = {
    "k": 1_000,
    "m": 1_000_000,
    "mil": 1_000_000,
    "mill": 1_000_000,
    "million": 1_000_000,
    "b": 1_000_000_000,
    "bil": 1_000_000_000,
    "billion": 1_000_000_000,
}

# Field-magnitude → multiplier applied to bare numbers. `raw` (or any unknown
# value, including None) means no scaling.
_MAGNITUDE_MULTIPLIERS = {
    "K": 1_000,
    "M": 1_000_000,
    "B": 1_000_000_000,
}

# A bare value at or above this is treated as already-raw on a scaled field —
# the player typed the full in-game number (`304,743,912`), don't multiply it
# into nonsense (3.04e17). Picked at 1M because no real shorthand value lands
# that high — `300` shorthand max is 300M, but `300` < 1M as a bare number.
_RAW_HEURISTIC_THRESHOLD = 1_000_000

_NUMERIC_INPUT_RE = re.compile(
    r"(?P<num>-?\d+(?:\.\d+)?)(?P<unit>[a-zA-Z]+)?",
)


def _parse_magnitude_input(raw: str, magnitude: str | None = None) -> int | None:
    """Parse a numeric string with optional shorthand suffix into a stored integer.

    `magnitude` is one of `"K"` / `"M"` / `"B"` (or `"raw"` / `None` for no
    scaling). It tells the parser what bare numbers mean for this field —
    e.g. on a magnitude=`"M"` field, `"301"` is shorthand for 301,000,000.

    Tolerates the shapes players naturally type: bare integers and decimals
    (`301`, `43.27`), shorthand suffixes (`300m`, `300mil`, `1.2b`, `5k`),
    comma grouping (`304,743,912`), and surrounding whitespace.

    Heuristics:
      - A unit suffix on the input always overrides the field's magnitude.
      - A bare value ≥ 1,000,000 on a scaled field is treated as raw — the
        player typed the full in-game number, don't multiply it.

    Returns the stored integer, or None on parse failure (caller re-prompts).
    """
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace(" ", "")
    if not s:
        return None

    m = _NUMERIC_INPUT_RE.fullmatch(s)
    if not m:
        return None

    try:
        value = float(m.group("num"))
    except ValueError:
        return None

    unit = (m.group("unit") or "").lower()
    if unit:
        if unit not in _SUFFIX_MULTIPLIERS:
            return None
        return int(round(value * _SUFFIX_MULTIPLIERS[unit]))

    multiplier = _MAGNITUDE_MULTIPLIERS.get(magnitude or "", 1)
    if multiplier > 1 and abs(value) >= _RAW_HEURISTIC_THRESHOLD:
        return int(round(value))
    return int(round(value * multiplier))


def _fmt_response_value(value, qtype: str | None) -> str:
    """Comma-format numeric responses for the leadership notification embed.

    Numeric questions store full integers post-magnitude-scaling — `304743912`
    is much harder to skim than `304,743,912`. Non-numeric responses
    (dropdowns, text) are passed through unchanged."""
    if value == "" or value is None:
        return "—"
    s = str(value).strip()
    if not s:
        return "—"
    if qtype != "numeric":
        return s
    try:
        return f"{int(s):,}"
    except ValueError:
        try:
            return f"{float(s):,}"
        except ValueError:
            return s


# ── Sheets helpers ─────────────────────────────────────────────────────────────


def _get_spreadsheet(guild_id: int = None):
    from config import get_spreadsheet

    return get_spreadsheet(guild_id)


def survey_question_keys_and_labels(questions: list) -> tuple[list[str], list[str]]:
    """Split a question list into the keys answers are stored under and the
    column labels those answers are written beneath."""
    q_keys = [q.get("key", f"field_{i}") for i, q in enumerate(questions)]
    q_labels = [q.get("label", k) for k, q in zip(q_keys, questions)]
    return q_keys, q_labels


def survey_header_rows(questions: list) -> tuple[list[str], list[str]]:
    """Row 1 for a survey's two tabs: (current answers, submission history).

    One definition shared by the wizard, which seeds these the moment a
    survey is saved, and the two write paths, which still fall back to
    writing them if they find a blank tab.
    """
    _, q_labels = survey_question_keys_and_labels(questions)
    return (
        ["Username", "Discord ID"] + q_labels + ["Date Modified"],
        ["Timestamp", "Discord ID", "Username"] + q_labels,
    )


def seed_survey_headers(
    guild_id: int, *, tab_responses: str, tab_history: str, questions: list
) -> list[str]:
    """Label both of a survey's tabs as soon as the survey is saved.

    Without this the tabs sit blank until the first member submits, and
    an alliance opening their sheet in the meantime has two untitled
    tabs to guess at — easy to start typing roster data into by hand,
    under no headers, in the wrong columns.

    Only writes a tab whose row 1 is still empty, so re-running the
    wizard never relabels columns that existing rows were written
    under. Returns the tabs actually seeded, for the wizard to report.
    """
    from config import get_or_create_worksheet

    sh = _get_spreadsheet(guild_id)
    responses_header, history_header = survey_header_rows(questions)
    seeded: list[str] = []

    for tab_name, header, add_filter in (
        (tab_responses, responses_header, False),
        (tab_history, history_header, True),
    ):
        if not tab_name:
            continue
        ws = get_or_create_worksheet(sh, tab_name)
        if any(ws.row_values(1)):
            continue
        ws.update("A1", [header], value_input_option="USER_ENTERED")
        if add_filter:
            try:
                ws.set_basic_filter()
            except Exception:
                pass
        seeded.append(tab_name)

    return seeded


def update_squad_powers(
    discord_id: str, username: str, data: dict, guild_id: int = None, survey: dict | None = None
):
    """
    Update or insert a member's row in the Squad Powers sheet.
    Columns are derived from the survey's question config. If `survey` is
    provided (multi-survey path), its questions/tab override the default.
    """
    from config import get_survey_config

    if survey is None:
        survey_cfg = get_survey_config(guild_id) if guild_id else {}
    else:
        survey_cfg = survey
    from config import get_or_create_worksheet

    questions = survey_cfg.get("questions") or []
    sh = _get_spreadsheet(guild_id)
    tab_name = survey_cfg.get("tab_squad_powers") or "Squad Powers"
    # Created rather than looked up: the wizard makes both tabs up front,
    # but a member submitting into a tab that was deleted or renamed since
    # would otherwise lose their answers to a WorksheetNotFound.
    ws = get_or_create_worksheet(sh, tab_name)
    rows = ws.get_all_values()

    _now = datetime.now(timezone.utc)
    now_str = f"{_now.month}/{_now.day}/{_now.year}"
    q_keys, _ = survey_question_keys_and_labels(questions)

    # The wizard seeds this when the survey is saved. Still handled here
    # for surveys configured before it did, and for a tab recreated after
    # someone deleted it.
    if not rows or not any(rows[0]):
        header, _unused = survey_header_rows(questions)
        ws.update("A1", [header], value_input_option="USER_ENTERED")
        rows = ws.get_all_values()

    new_row = [username, discord_id] + [data.get(k, "") for k in q_keys] + [now_str]

    for i, row in enumerate(rows):
        if len(row) >= 2 and row[1].strip() == discord_id:
            ws.update(f"A{i + 1}", [new_row], value_input_option="USER_ENTERED")
            print(f"[SURVEY] Updated Squad Powers row {i + 1} for {username}")
            return

    ws.append_row(new_row, value_input_option="USER_ENTERED")
    print(f"[SURVEY] Appended new Squad Powers row for {username}")


def append_survey_history(
    discord_id: str, username: str, data: dict, guild_id: int = None, survey: dict | None = None
):
    """Append a timestamped row to the Survey History sheet."""
    from config import get_config, get_survey_config, get_or_create_worksheet

    if survey is None:
        survey_cfg = get_survey_config(guild_id) if guild_id else {}
    else:
        survey_cfg = survey
    questions = survey_cfg.get("questions") or []
    cfg = get_config(guild_id)
    sh = _get_spreadsheet(guild_id)
    tab_name = survey_cfg.get("tab_history") or (
        cfg.tab_survey_history if cfg else "Survey History"
    )
    ws = get_or_create_worksheet(sh, tab_name)

    q_keys, _ = survey_question_keys_and_labels(questions)

    existing = ws.row_values(1)
    if not any(existing):
        _unused, header = survey_header_rows(questions)
        ws.update("A1", [header], value_input_option="USER_ENTERED")
        try:
            ws.set_basic_filter()
        except Exception:
            pass

    _now = datetime.now(timezone.utc)
    now_str = f"{_now.month}/{_now.day}/{_now.year} {_now:%H:%M} UTC"
    row = [now_str, discord_id, username] + [data.get(k, "") for k in q_keys]
    ws.append_row(row, value_input_option="USER_ENTERED")
    print(f"[SURVEY] Appended Survey History row for {username}")


# ── Dropdown views ─────────────────────────────────────────────────────────────


class DropdownView(discord.ui.View):
    """Generic single-select dropdown that persists the selected value after selection."""

    def __init__(self, placeholder: str, options: list, label: str = ""):
        super().__init__(timeout=SURVEY_TIMEOUT)
        self.selected = None
        self.confirmed = False
        self.label = label

        select = discord.ui.Select(
            placeholder=placeholder,
            options=[discord.SelectOption(label=o, value=o) for o in options],
            row=0,
        )

        async def _cb(interaction: discord.Interaction):
            self.selected = select.values[0]
            self.confirmed = True
            select.disabled = True
            content = f"**{self.label}** {self.selected}"
            await wizard_registry.safe_edit_response(interaction, content=content, view=self)
            self.stop()

        select.callback = _cb
        self.add_item(select)


# ── Survey flow ────────────────────────────────────────────────────────────────


async def run_survey(bot, thread: discord.Thread, user: discord.Member, survey: dict | None = None):
    """
    Walk the user through all survey questions.

    `survey` is an optional pre-fetched survey dict (default or extra). When
    omitted, falls back to the guild's default survey config.
    """
    gid = user.guild.id if hasattr(user, "guild") and user.guild else None

    from config import get_survey_config

    if survey is None:
        survey_cfg = get_survey_config(gid) if gid else {}
    else:
        survey_cfg = survey
    questions = survey_cfg.get("questions") or []

    if not questions:
        await thread.send(
            "⚠️ No survey questions configured. Ask leadership to run `/setup → 📋 Survey`."
        )
        return

    def check(m):
        return m.author == user and m.channel == thread

    async def ask_number(prompt: str, max_chars: int = 10) -> str | None:
        """
        Text question with a length cap. On too-long input, re-prompts the
        same question (up to 5 attempts) so the user doesn't have to restart
        the whole survey for one slip — e.g. typing `153,725,881` instead
        of `154` for a THP-in-millions field.
        """
        attempts_left = 5
        first_pass = True
        while attempts_left > 0:
            if first_pass:
                await thread.send(prompt)
                first_pass = False
            try:
                reply = await bot.wait_for("message", check=check, timeout=SURVEY_TIMEOUT)
            except asyncio.TimeoutError:
                await thread.send(
                    "⏰ Survey timed out. You can start again by clicking the Answer button."
                )
                return None
            val = reply.content.strip()
            if len(val) > max_chars:
                attempts_left -= 1
                await thread.send(
                    f"⚠️ That entry is too long (max {max_chars} characters). "
                    f"Please re-enter your answer for this question."
                )
                continue
            return val

        await thread.send(
            "⚠️ Too many invalid attempts on this question. "
            "Cancelling the survey — click the Answer button to start over when you're ready."
        )
        return None

    async def ask_dropdown(
        prompt: str, options: list, placeholder: str, label: str = ""
    ) -> str | None:
        view = DropdownView(placeholder, options, label=label)
        await thread.send(prompt, view=view)
        await view.wait()
        if not view.confirmed:
            await thread.send(
                "⏰ Survey timed out. You can start again by clicking the Answer button."
            )
            return None
        return view.selected

    async def ask_numeric(
        prompt: str,
        min_val: float | None = None,
        max_val: float | None = None,
        magnitude: str | None = None,
        max_chars: int = 0,
    ) -> str | None:
        """
        Numeric input with optional magnitude scaling and min/max bounds.

        Magnitude (`K` / `M` / `B`) lets members type the natural shorthand
        (`301` for 301M THP, `43.27` for 43.27M squad power) — see
        `_parse_magnitude_input` for the full set of accepted shapes. Min/max
        bounds (Premium) are checked against the stored (post-scale) integer.

        On invalid input or out-of-bounds values the user is re-prompted
        for the same question (up to 5 attempts) instead of having the
        whole survey cancel out from under them.
        """
        full = prompt
        if min_val is not None or max_val is not None:
            bits = []
            if min_val is not None:
                bits.append(f"min: {min_val}")
            if max_val is not None:
                bits.append(f"max: {max_val}")
            full += f"\n*({', '.join(bits)})*"

        scaled = magnitude in _MAGNITUDE_MULTIPLIERS

        attempts_left = 5
        first_pass = True
        while attempts_left > 0:
            if first_pass:
                await thread.send(full)
                first_pass = False
            try:
                reply = await bot.wait_for("message", check=check, timeout=SURVEY_TIMEOUT)
            except asyncio.TimeoutError:
                await thread.send(
                    "⏰ Survey timed out. You can start again by clicking the Answer button."
                )
                return None
            raw = reply.content.strip()
            if max_chars and len(raw) > max_chars:
                attempts_left -= 1
                await thread.send(
                    f"⚠️ That entry is too long (max {max_chars} characters). "
                    f"Please re-enter your answer for this question."
                )
                continue
            if scaled:
                n = _parse_magnitude_input(raw, magnitude)
                if n is None:
                    attempts_left -= 1
                    await thread.send(
                        f"⚠️ `{raw}` isn't a number. Please re-enter your answer for this question."
                    )
                    continue
            else:
                try:
                    n = float(raw) if "." in raw else int(raw)
                except ValueError:
                    attempts_left -= 1
                    await thread.send(
                        f"⚠️ `{raw}` isn't a number. Please re-enter your answer for this question."
                    )
                    continue
            if min_val is not None and n < min_val:
                attempts_left -= 1
                await thread.send(
                    f"⚠️ Must be at least **{min_val}**. Please re-enter your answer for this question."
                )
                continue
            if max_val is not None and n > max_val:
                attempts_left -= 1
                await thread.send(
                    f"⚠️ Must be at most **{max_val}**. Please re-enter your answer for this question."
                )
                continue
            return str(n)

        await thread.send(
            "⚠️ Too many invalid attempts on this question. "
            "Cancelling the survey — click the Answer button to start over when you're ready."
        )
        return None

    async def ask_multi_select(
        prompt: str, options: list, placeholder: str, label: str = ""
    ) -> str | None:
        """Premium type: Discord multi-select (up to len(options) picks).
        Returns a comma-joined string."""
        if not options:
            await thread.send("⚠️ Question has no options configured. Please contact leadership.")
            return None

        view = discord.ui.View(timeout=SURVEY_TIMEOUT)
        result = {"values": None}

        select = discord.ui.Select(
            placeholder=placeholder or f"Select {label}…",
            min_values=1,
            max_values=min(len(options), 25),
            options=[discord.SelectOption(label=o, value=o) for o in options[:25]],
        )

        async def _cb(inter: discord.Interaction):
            result["values"] = list(select.values)
            select.disabled = True
            content = f"**{label}** {', '.join(result['values'])}"
            await wizard_registry.safe_edit_response(inter, content=content, view=view)
            view.stop()

        select.callback = _cb
        view.add_item(select)

        await thread.send(prompt, view=view)
        await view.wait()
        if result["values"] is None:
            await thread.send(
                "⏰ Survey timed out. You can start again by clicking the Answer button."
            )
            return None
        return ", ".join(result["values"])

    async def ask_date(prompt: str, date_format: str = "%m/%d/%Y") -> str | None:
        """
        Premium type: parse a date with strptime, return as ISO string.

        On a parse failure the user is re-prompted for the same question
        (up to 5 attempts) instead of having the whole survey cancel out.
        """
        from datetime import datetime as _dt

        full = prompt + f"\n*(format: `{date_format}`)*"

        attempts_left = 5
        first_pass = True
        while attempts_left > 0:
            if first_pass:
                await thread.send(full)
                first_pass = False
            try:
                reply = await bot.wait_for("message", check=check, timeout=SURVEY_TIMEOUT)
            except asyncio.TimeoutError:
                await thread.send(
                    "⏰ Survey timed out. You can start again by clicking the Answer button."
                )
                return None
            raw = reply.content.strip()
            try:
                d = _dt.strptime(raw, date_format).date()
            except ValueError:
                attempts_left -= 1
                await thread.send(
                    f"⚠️ `{raw}` doesn't match `{date_format}`. "
                    f"Please re-enter your answer for this question."
                )
                continue
            return d.isoformat()

        await thread.send(
            "⚠️ Too many invalid attempts on this question. "
            "Cancelling the survey — click the Answer button to start over when you're ready."
        )
        return None

    data = {}

    for i, q in enumerate(questions):
        key = q.get("key", f"field_{i}")
        label = q.get("label", f"Question {i + 1}")
        qtype = q.get("type", "text")
        options = q.get("options", [])
        placeholder = q.get("placeholder", "")
        max_chars = q.get("max_chars", 10) or 10

        if qtype == "text":
            hint = f"\n*{placeholder}*" if placeholder else ""
            if max_chars:
                hint += f"\n*Maximum characters: {max_chars}*"
            val = await ask_number(f"**{label}**{hint}", max_chars=max_chars)
        elif qtype == "dropdown":
            val = await ask_dropdown(
                f"**{label}**",
                options,
                placeholder or f"Select {label}...",
                label=f"{label}:",
            )
        elif qtype == "numeric":
            val = await ask_numeric(
                f"**{label}**" + (f"\n*{placeholder}*" if placeholder else ""),
                min_val=q.get("min"),
                max_val=q.get("max"),
                magnitude=q.get("magnitude"),
                max_chars=int(q.get("max_chars") or 0),
            )
        elif qtype == "multi_select":
            val = await ask_multi_select(
                f"**{label}**",
                options,
                placeholder or f"Select {label}...",
                label=label,
            )
        elif qtype == "date":
            val = await ask_date(
                f"**{label}**" + (f"\n*{placeholder}*" if placeholder else ""),
                date_format=q.get("date_format", "%m/%d/%Y"),
            )
        else:
            val = await ask_number(f"**{label}**", max_chars=max_chars)

        if val is None:
            return
        data[key] = val

    # ── Save to sheets ────────────────────────────────────────────────────────
    await thread.send("⏳ Saving your responses...")
    try:
        discord_id = str(user.id)
        username = user.display_name
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            update_squad_powers,
            discord_id,
            username,
            data,
            gid,
            survey_cfg,
        )
        await loop.run_in_executor(
            None,
            append_survey_history,
            discord_id,
            username,
            data,
            gid,
            survey_cfg,
        )
    except Exception as e:
        await thread.send(
            f"⚠️ There was an error saving your responses: {e}\nPlease let leadership know."
        )
        print(f"[SURVEY] Error saving for {user.display_name}: {e}")
        return

    # ── Notify leadership ─────────────────────────────────────────────────────
    try:
        from config import get_config as _sgc

        _scfg = _sgc(user.guild.id) if hasattr(user, "guild") else None
        # Extras may override the notify channel; fall back to guild-level.
        _notify_id = int(survey_cfg.get("notify_channel_id") or 0) or (
            _scfg.survey_notify_channel_id if _scfg else 0
        )
        notify_channel = bot.get_channel(_notify_id)
        if notify_channel:
            _now = datetime.now(timezone.utc)
            _hour12 = _now.hour % 12 or 12
            date_str = f"{_now:%B} {_now.day}, {_now.year} at {_hour12}:{_now:%M %p} UTC"
            embed = discord.Embed(
                title="📋 New Survey Response",
                color=discord.Color.blurple(),
            )
            embed.add_field(name="Member", value=user.mention, inline=True)
            embed.add_field(name="Submitted", value=date_str, inline=True)
            embed.add_field(name="\u200b", value="\u200b", inline=True)
            # Iterate the actual configured questions in order so guilds with
            # custom surveys see their own labels (not a hardcoded LW set).
            response_lines = []
            for q in questions:
                key = q.get("key", "")
                label = q.get("label", key) or key
                if not key:
                    continue
                value = _fmt_response_value(data.get(key, ""), q.get("type"))
                response_lines.append(f"**{label}:** {value}")

            embed.add_field(
                name="Responses",
                value="\n".join(response_lines)[:1024] if response_lines else "*(no responses)*",
                inline=False,
            )
            await notify_channel.send(embed=embed)
    except Exception as e:
        print(f"[SURVEY] Error sending leadership notification: {e}")

    await _finalize_survey_thread(thread)


class CloseThreadView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.closed = False

    @discord.ui.button(label="❌ Close Thread", style=discord.ButtonStyle.secondary)
    async def close_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.closed = True
        await interaction.response.defer()
        self.stop()

    async def on_timeout(self):
        self.closed = True
        self.stop()


async def _finalize_survey_thread(thread):
    """Send the success embed with a Close Thread button, then delete the thread."""
    embed = discord.Embed(
        title="✅ Survey Complete!",
        color=discord.Color.green(),
    )
    embed.add_field(
        name="Thank you!",
        value=(
            "Your response has been saved successfully! Thanks for keeping your stats up to date, "
            "it helps us to balance teams, track alliance growth, and prepare for season events."
        ),
        inline=False,
    )
    embed.set_footer(text="This thread will be deleted in 60 seconds or you can close it now.")

    close_view = CloseThreadView()
    await thread.send(embed=embed, view=close_view)

    await close_view.wait()
    await asyncio.sleep(2)
    try:
        await thread.delete()
    except discord.HTTPException as e:
        print(f"[SURVEY] Could not delete thread: {e}")


# ── Translation helper ─────────────────────────────────────────────────────────


async def add_translation_helper(thread: discord.Thread, guild, helper_id: int) -> bool:
    """
    Add the guild's configured translation-helper bot to a survey thread.

    Survey threads are private (`invitable=False`), so a third-party
    translate bot can't read the prompts, and therefore can't translate
    them, unless it's an explicit thread member. Alliances with
    non-English speakers point `survey_translate_bot_id` at their
    translate bot and it gets added alongside the member.

    Returns True when the helper was added. Every failure path returns
    False and logs: the survey itself must never be blocked by this, so a
    departed bot, a permissions gap on the parent channel, or a Discord
    hiccup degrades to an untranslated (but working) survey.
    """
    if not helper_id or guild is None:
        return False

    helper = guild.get_member(helper_id)
    if helper is None:
        print(
            f"[SURVEY] Translation helper {helper_id} is no longer in guild "
            f"{guild.id}, skipping. Pick a new one in /setup."
        )
        return False

    try:
        await thread.add_user(helper)
    except discord.Forbidden:
        print(
            f"[SURVEY] Could not add translation helper {helper} to thread "
            f"{thread.id} (guild {guild.id}): missing access. It likely can't "
            f"view the survey channel."
        )
        return False
    except discord.HTTPException as e:
        print(
            f"[SURVEY] Could not add translation helper {helper} to thread "
            f"{thread.id} (guild {guild.id}): {e}"
        )
        return False
    return True


def _describe_helper_gaps(helper: discord.Member, survey_channel: discord.abc.GuildChannel) -> list:
    """
    Return the permissions the helper bot is missing on the survey channel.

    A thread member inherits its permissions from the parent channel, so a
    translate bot that can't view the survey channel can't be added at all,
    and one that can't post in threads can be added but never answers.
    Both are silent failures at survey time, so they're surfaced at
    pick time instead.
    """
    perms = survey_channel.permissions_for(helper)
    gaps = []
    if not perms.view_channel:
        gaps.append("**View Channel**")
    if not perms.send_messages_in_threads:
        gaps.append("**Send Messages in Threads**")
    return gaps


class _TranslationHelperView(discord.ui.View):
    """Pick (or clear) the translate bot added to every survey thread."""

    def __init__(self, current_id: int):
        super().__init__(timeout=180)
        self.current_id = current_id

        select = discord.ui.UserSelect(
            placeholder="Pick your translation bot…",
            min_values=1,
            max_values=1,
        )
        select.callback = self._on_select
        self._select = select
        self.add_item(select)

        self.btn_clear.disabled = not current_id

    async def _on_select(self, inter: discord.Interaction):
        from config import get_config, get_or_create_config, update_config_field

        # A guild UserSelect resolves to a Member, but fall back to the raw
        # value so an uncached pick can't crash the callback.
        picked = self._select.values[0]
        helper = inter.guild.get_member(picked.id) or picked

        # Survey threads are private to one member. A human added to every
        # thread would silently read every member's answers, so restrict
        # this to bots. That privacy boundary is the whole point of the
        # setting.
        if not getattr(helper, "bot", False):
            await inter.response.send_message(
                f"⚠️ **{helper.display_name}** isn't a bot. Survey threads are private "
                "between the member and me, so only a translate **bot** can be added "
                "here. Adding a person would let them read every member's answers.",
                ephemeral=True,
            )
            return

        cfg = get_config(inter.guild_id) or get_or_create_config(inter.guild_id)
        update_config_field(inter.guild_id, "survey_translate_bot_id", helper.id)

        lines = [
            f"🌐 **{helper.mention} will be added to every new survey thread.**",
            "",
            "Members can now use its translate commands or reactions on the survey "
            "prompts, right inside their own thread.",
        ]

        survey_channel = inter.guild.get_channel(int(cfg.survey_channel_id or 0))
        if survey_channel is None:
            lines += [
                "",
                "⚠️ No survey channel is configured yet, so I can't check whether this "
                f"bot has access. Set one up in `/setup` → {HUB_BTN_SURVEY}.",
            ]
        elif isinstance(helper, discord.Member):
            gaps = _describe_helper_gaps(helper, survey_channel)
            if gaps:
                lines += [
                    "",
                    f"⚠️ It's missing {' and '.join(gaps)} on {survey_channel.mention}. "
                    "Survey threads inherit that channel's permissions, so fix this or "
                    "the bot won't be able to help in the thread.",
                ]

        lines += [
            "",
            "-# Heads up: this bot will be able to read everything in members' survey "
            "threads while they're open. Threads are deleted once the survey is submitted.",
        ]

        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(
            inter, content="\n".join(lines), embed=None, view=self
        )
        self.stop()

    @discord.ui.button(label="🚫 Remove helper", style=discord.ButtonStyle.danger)
    async def btn_clear(self, inter: discord.Interaction, _b: discord.ui.Button):
        from config import update_config_field

        update_config_field(inter.guild_id, "survey_translate_bot_id", 0)
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(
            inter,
            content=(
                "🚫 **Translation helper removed.** New survey threads will only "
                "contain the member and me."
            ),
            embed=None,
            view=self,
        )
        self.stop()


async def run_translation_helper_setup(interaction: discord.Interaction):
    """
    `/setup` → Survey translation. Picks the third-party translate bot that
    gets added to each private survey thread.

    Survey threads are created `invitable=False` with only the member and
    this bot as members, which is what keeps one member's answers private
    from the rest of the alliance. It also means a server-wide translate
    bot can't see the prompts. Alliances with non-English speakers name
    their translate bot here and it's added at thread creation.
    """
    from config import get_config

    cfg = get_config(interaction.guild_id)
    current_id = int((cfg.survey_translate_bot_id if cfg else 0) or 0)
    current = interaction.guild.get_member(current_id) if current_id else None

    if current_id and current is None:
        current_state = (
            f"⚠️ Previously set to a bot that's no longer in this server (`{current_id}`)."
        )
    elif current:
        current_state = f"Currently: {current.mention}"
    else:
        current_state = "Currently: *no helper. Survey prompts are English only.*"

    embed = discord.Embed(
        title="🌐 Survey Translation Helper",
        color=discord.Color.blurple(),
        description=(
            "Survey threads are private between the member and me, so a translate "
            "bot in your server can't see the prompts. That's why members can't "
            "translate their survey.\n\n"
            "Pick your translate bot below and I'll add it to every new survey "
            "thread, so members can translate the questions in place.\n\n"
            f"{current_state}"
        ),
    )
    embed.set_footer(text="Only bots can be picked. Free for every alliance.")

    await interaction.response.send_message(
        embed=embed,
        view=_TranslationHelperView(current_id),
        ephemeral=True,
    )


# ── Persistent survey button ───────────────────────────────────────────────────


async def _start_survey_answer_flow(interaction: discord.Interaction, survey_id: str = "default"):
    """Shared handler for both legacy and dynamic survey-answer buttons."""
    cfg = get_config(interaction.guild_id)
    if not cfg or not cfg.setup_complete:
        await interaction.response.send_message(NOT_SET_UP, ephemeral=True)
        return
    if cfg.member_role_name not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message(
            f"⛔ You need the **{cfg.member_role_name}** role to fill out this survey.",
            ephemeral=True,
        )
        return

    from config import get_survey

    survey_cfg = get_survey(interaction.guild_id, survey_id)
    if survey_cfg is None:
        await interaction.response.send_message(
            "⚠️ This survey is no longer configured. Ask leadership to repost it.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        "🚀 Let's get started! Your private thread is being created...",
        ephemeral=True,
    )

    # Create a private thread named after the chosen survey (slugified).
    title_source = survey_cfg.get("survey_name") or survey_cfg.get("tab_squad_powers") or "survey"
    slug = re.sub(r"[^a-z0-9]+", "-", title_source.lower()).strip("-") or "survey"
    channel = interaction.channel
    thread_name = f"survey-{slug}-{interaction.user.name}"[:100]
    try:
        thread = await channel.create_thread(
            name=thread_name,
            type=discord.ChannelType.private_thread,
            invitable=False,
        )
        await thread.add_user(interaction.user)
    except discord.HTTPException as e:
        await interaction.followup.send(
            f"⚠️ Could not create your survey thread: {e}",
            ephemeral=True,
        )
        return

    await add_translation_helper(thread, interaction.guild, cfg.survey_translate_bot_id)

    await interaction.followup.send(
        f"🚀 Your thread is ready — head over here to get started: {thread.mention}",
        ephemeral=True,
    )

    # A survey runs for as long as the member takes to answer it, and the
    # thread can be deleted out from under it at any point — by an officer
    # tidying up, by Discord's own thread cleanup, or by the member leaving.
    # Every prompt, retry and timeout message in run_survey is a thread.send,
    # so once the thread is gone the flow cannot do anything except stop
    # (#432, reported as NotFound 10003 from the timeout branch of
    # ask_numeric). Nothing has been written to the sheet at that point: the
    # save happens only after the last answer.
    #
    # Caught at this boundary rather than at 20-odd send sites, because a
    # vanished thread invalidates the whole run rather than any single
    # message, and there is nobody left to tell either way.
    try:
        await run_survey(interaction.client, thread, interaction.user, survey=survey_cfg)
    except discord.NotFound:
        print(
            f"[SURVEY] Thread {thread.id} disappeared mid-survey "
            f"(guild={interaction.guild_id}, user={interaction.user.id}) — abandoning the run"
        )
    except discord.Forbidden:
        print(
            f"[SURVEY] Lost access to thread {thread.id} mid-survey "
            f"(guild={interaction.guild_id}, user={interaction.user.id}) — abandoning the run"
        )


class SurveyButtonView(discord.ui.View):
    """
    Persistent view for the **default** survey button. Re-registered every
    on_ready via `bot.add_view(SurveyButtonView())`. Extra surveys use the
    `DynamicSurveyButton` below so each one keeps its own custom_id.
    """

    def __init__(self):
        super().__init__(timeout=None)  # persistent

    @discord.ui.button(
        label="📋 Answer",
        style=discord.ButtonStyle.success,
        custom_id="survey_answer_button",
    )
    async def answer(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _start_survey_answer_flow(interaction, survey_id="default")


class DynamicSurveyButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"survey_answer_button:(?P<survey_id>[A-Za-z0-9_\-]{1,64})",
):
    """
    Persistent button for an extra (non-default) survey. Each extra survey
    posts its own button whose custom_id encodes the `survey_id`. Discord
    re-creates these via `from_custom_id` after a bot restart.
    """

    def __init__(self, survey_id: str):
        super().__init__(
            discord.ui.Button(
                label="📋 Answer",
                style=discord.ButtonStyle.success,
                custom_id=f"survey_answer_button:{survey_id}",
            )
        )
        self.survey_id = survey_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["survey_id"])

    async def callback(self, interaction: discord.Interaction):
        await _start_survey_answer_flow(interaction, survey_id=self.survey_id)


def build_survey_button_view(survey_id: str = "default") -> discord.ui.View:
    """Return the right persistent view for a given survey id."""
    if survey_id == "default":
        return SurveyButtonView()
    view = discord.ui.View(timeout=None)
    view.add_item(DynamicSurveyButton(survey_id))
    return view


# ── Guard (leadership only) ────────────────────────────────────────────────────


async def _guard(interaction: discord.Interaction) -> bool:
    cfg = get_config(interaction.guild_id)
    if not cfg or not cfg.setup_complete:
        await interaction.response.send_message(NOT_SET_UP, ephemeral=True)
        return False
    if cfg.leadership_role_name not in [r.name for r in interaction.user.roles]:
        await interaction.response.send_message(
            f"⛔ You need the **{cfg.leadership_role_name}** role to use this command.",
            ephemeral=True,
        )
        return False
    return True


# ── Survey selector helper (Premium multi-survey) ─────────────────────────────


class _SurveyPickView(discord.ui.View):
    """Internal: dropdown for picking which survey to act on."""

    def __init__(self, surveys: list[dict]):
        super().__init__(timeout=120)
        self.selected_id: str | None = None

        options = []
        for s in surveys[:25]:
            label = s.get("survey_name") or s.get("survey_id") or "?"
            sid = s.get("survey_id") or "default"
            desc = ", ".join(q.get("label", "") for q in (s.get("questions") or [])[:3])
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=sid[:100],
                    description=(desc[:100] if desc else None),
                )
            )

        sel = discord.ui.Select(placeholder="Pick a survey…", options=options)

        async def _cb(inter: discord.Interaction):
            self.selected_id = sel.values[0]
            sel.disabled = True
            picked = next(
                (s for s in surveys if (s.get("survey_id") or "default") == self.selected_id), None
            )
            label = picked.get("survey_name", self.selected_id) if picked else self.selected_id
            await wizard_registry.safe_edit_response(
                inter, content=f"✅ Survey: **{label}**", view=self
            )
            self.stop()

        sel.callback = _cb
        self.add_item(sel)


async def _pick_survey(interaction: discord.Interaction, *, prompt: str) -> dict | None:
    """
    For premium guilds with more than one configured survey, prompt the
    caller to pick one. Returns the chosen survey dict (always at least the
    default). Returns `None` only if the picker timed out.
    """
    from config import list_surveys
    import premium as _prem

    surveys = list_surveys(interaction.guild_id)

    if (
        not await _prem.is_premium(interaction.guild_id, bot=interaction.client)
        or len(surveys) <= 1
    ):
        return surveys[0]  # default-only path

    view = _SurveyPickView(surveys)
    await interaction.followup.send(prompt, view=view, ephemeral=True)
    await view.wait()
    if view.selected_id is None:
        return None
    return next(
        (s for s in surveys if (s.get("survey_id") or "default") == view.selected_id),
        surveys[0],
    )


# ── Multi-survey manage view (Premium /survey UX) ─────────────────────────────


# ── Cog ────────────────────────────────────────────────────────────────────────


class SurveyCog(commands.Cog):
    # `/survey` is a single top-level hub command (embed + button grid via
    # survey_hub.handle_survey_hub) — the same shape as `/train`, `/events`,
    # and the storm hubs. It replaced the `/survey overview | post | remind`
    # subcommand group (#426). The scheduled-reminder loop lives here too.

    def __init__(self, bot):
        self.bot = bot
        # Re-register the persistent view (default survey button) and the
        # dynamic-item handler (extra survey buttons) so both keep working
        # across bot restarts.
        self.bot.add_view(SurveyButtonView())
        try:
            self.bot.add_dynamic_items(DynamicSurveyButton)
        except AttributeError:
            # discord.py older than 2.4 — dynamic items unsupported. The
            # default survey button will still work; extras will not survive
            # restarts on this version. Surfacing this in logs lets us notice
            # when a deploy needs an upgrade.
            print(
                "[SURVEY] discord.py too old for DynamicItem — extra-survey "
                "buttons will not be persistent on this version."
            )
        # Start the per-minute scheduler tick that fires scheduled reminders
        # (#27). Stamps `reminder_last_fired` on each survey row so we don't
        # double-fire on a restart in the same minute.
        self.check_scheduled_reminders.start()

    def cog_unload(self):
        try:
            self.check_scheduled_reminders.cancel()
        except Exception:
            pass

    @tasks.loop(minutes=1)
    async def check_scheduled_reminders(self):
        """
        Walk every guild's scheduled survey reminders. Fire the ones whose
        frequency, day-of-week, and time match `now` in the guild's timezone.
        DM-via-roster reminders silently no-op for non-Premium guilds.
        """
        from zoneinfo import ZoneInfo
        from config import (
            list_scheduled_survey_reminders,
            update_survey_reminder_last_fired,
            get_config as _get_config,
            stamp_loop_heartbeat,
        )
        import premium as _prem

        try:
            scheduled = list_scheduled_survey_reminders()
        except Exception as e:
            print(f"[SURVEY] Error listing scheduled reminders: {e}")
            return

        for entry in scheduled:
            try:
                guild_id = int(entry["guild_id"])
                survey_id = entry.get("survey_id") or "default"
                frequency = (entry.get("reminder_frequency") or "off").lower()
                if frequency == "off":
                    continue

                cfg = _get_config(guild_id)
                if not cfg or not cfg.setup_complete:
                    continue

                tz_str = cfg.timezone or "America/New_York"
                guild_tz = ZoneInfo(tz_str)
                guild_now = datetime.now(tz=guild_tz)

                # Time-of-day match (HH:MM, minute granularity)
                time_str = entry.get("reminder_time") or "12:00"
                try:
                    r_h, r_m = int(time_str.split(":")[0]), int(time_str.split(":")[1])
                except Exception:
                    continue
                if guild_now.hour != r_h or guild_now.minute != r_m:
                    continue

                # Day-of-week match for weekly schedules. Python: Monday=0
                if frequency == "weekly":
                    target_day = int(entry.get("reminder_day_of_week") or 1)
                    if guild_now.weekday() != target_day:
                        continue

                # Idempotency — don't fire twice for the same date
                today_iso = guild_now.date().isoformat()
                last_fired = entry.get("reminder_last_fired") or ""
                if last_fired == today_iso:
                    continue

                # Resolve the survey config (so we can format the reminder body)
                from config import get_survey

                survey = get_survey(guild_id, survey_id) or {
                    "survey_name": entry.get("survey_name") or "Default",
                    "reminder_message": entry.get("reminder_message") or "",
                }
                # Refresh body with the latest custom message + sensible default
                body = (
                    survey.get("reminder_message")
                    or entry.get("reminder_message")
                    or _default_reminder_body(survey)
                )

                use_dm = bool(entry.get("reminder_use_dm"))
                channel_id = int(entry.get("reminder_channel_id") or 0)

                if use_dm:
                    # DM path is Premium-only because it depends on Member
                    # Roster Sync. Silently skip when the guild lapses.
                    if not await _prem.is_premium(guild_id, bot=self.bot):
                        print(
                            f"[SURVEY] Skipping DM reminder for guild {guild_id}: Premium lapsed."
                        )
                        continue
                    sent, skipped = await _send_reminder_via_dm(self.bot, guild_id, body)
                    print(
                        f"[SURVEY] Scheduled DM reminder fired for guild={guild_id} "
                        f"survey={survey_id} sent={sent} skipped={skipped}"
                    )
                elif channel_id:
                    ok = await _send_reminder_to_channel(
                        self.bot,
                        guild_id,
                        channel_id,
                        body,
                        health_subject=SURVEY_REMINDER_CHANNEL_SUBJECT,
                    )
                    if not ok:
                        print(
                            f"[SURVEY] Channel reminder failed for guild={guild_id} "
                            f"survey={survey_id} channel={channel_id}"
                        )
                        continue
                    print(
                        f"[SURVEY] Scheduled channel reminder fired for guild={guild_id} "
                        f"survey={survey_id} channel={channel_id}"
                    )
                else:
                    # No destination configured — schedule is incomplete; skip.
                    continue

                update_survey_reminder_last_fired(guild_id, survey_id, today_iso)

            except Exception as e:
                # `guild_id` may not have been bound yet if `int(entry["guild_id"])`
                # itself raised — read straight from `entry` so the log is always
                # attributed to the offending row.
                gid = entry.get("guild_id", "?")
                print(f"[SURVEY] Error firing scheduled reminder for guild {gid}: {e}")

        # Clean tick — stamp liveness for the outage catch-up scan (#227).
        stamp_loop_heartbeat("survey_reminder")

    @check_scheduled_reminders.before_loop
    async def _before_check_scheduled(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="survey",
        description="Open the survey hub for this alliance",
    )
    @app_commands.guild_only()
    async def survey(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        from survey_hub import handle_survey_hub

        await handle_survey_hub(self.bot, interaction)


# ── Post the survey button ────────────────────────────────────────────────────


async def run_post_survey(interaction: discord.Interaction, bot):
    """
    Post (or repost) a survey's Answer button in its configured channel.

    Was `/survey post` before the hub consolidation; now the
    📮 Post Survey button. Reposting is safe and supported: votes and
    responses key off the member, not the message, and the button's
    `custom_id` is stable, so old posts keep working too.

    Precondition: the caller has already responded to `interaction` (the
    hub disables its grid via `safe_edit_response` before dispatching), so
    everything here goes out through `followup`.
    """
    from config import get_config

    cfg = get_config(interaction.guild_id)
    if not cfg:
        await interaction.followup.send("⚙️ Bot not configured. Run `/setup` first.", ephemeral=True)
        return

    # Premium guilds with multiple surveys pick which one to post.
    survey = await _pick_survey(
        interaction,
        prompt="📋 You have multiple surveys configured. Which one do you want to post?",
    )
    if survey is None:
        await interaction.followup.send(
            HUB_TIMEOUT.format(cmd="survey", hub_btn=SURVEY_HUB_BTN_POST), ephemeral=True
        )
        return

    survey_id = survey.get("survey_id") or "default"
    channel_id = int(survey.get("survey_channel_id") or 0) or cfg.survey_channel_id
    channel = bot.get_channel(channel_id)
    if channel is None:
        await interaction.followup.send(
            f"⚠️ Could not find the survey channel for **{survey.get('survey_name', 'this survey')}**.",
            ephemeral=True,
        )
        return

    intro = survey.get("intro_message") or _default_posted_intro(survey)

    view = build_survey_button_view(survey_id)
    try:
        await channel.send(intro, view=view)
    except discord.Forbidden:
        await interaction.followup.send(
            f"⚠️ I can't post in {channel.mention}. Check my permissions there and try again.",
            ephemeral=True,
        )
        return
    await interaction.followup.send(
        f"✅ Survey button posted for **{survey.get('survey_name', 'Default')}** in {channel.mention}.",
        ephemeral=True,
    )


def _default_posted_intro(survey: dict) -> str:
    """Headline + body posted above the survey button when none is saved.

    Falls back to the survey's own template so a non-squad-power survey
    never announces itself as a squad-power one. A survey built from
    scratch speaks in its own name.
    """
    from defaults import SURVEY_TEMPLATE_SQUAD_POWER, survey_template

    tpl = survey_template(survey.get("template"))
    if tpl["key"] == SURVEY_TEMPLATE_SQUAD_POWER:
        return f"**Let us know your Squad Powers!**\n\n{tpl['intro_message']}"

    name = survey.get("survey_name") or "our latest survey"
    return (
        f"**{name}**\n\n"
        "Please take a moment to fill this out. Click the button below to get started."
    )


# ── Reminder helpers ──────────────────────────────────────────────────────────


def _default_reminder_body(survey: dict) -> str:
    """Fallback reminder message when the survey doesn't have one saved."""
    name = survey.get("survey_name") or "the survey"
    return (
        f"📋 **Friendly reminder** — your alliance is asking you to fill out "
        f"**{name}** this week. Open the survey channel in Discord and click "
        f"the **📋 Answer** button to get started. Thanks!"
    )


async def _send_reminder_to_channel(
    bot, guild_id: int, channel_id: int, body: str, *, health_subject: str = ""
) -> bool:
    """Post a reminder body to a guild channel. Returns True on success.

    ``health_subject`` opts this call into config-health recording (#379).
    Passed by the scheduled loop, which posts with nobody watching, and
    deliberately not by "remind now", where the user gets an inline error and
    a duplicate leadership notice would just be noise.
    """
    if health_subject:
        channel = config_health.resolve_configured_channel(
            bot, guild_id, health_subject, channel_id
        )
    else:
        channel = bot.get_channel(channel_id)
    if channel is None:
        print(
            f"[REMINDER] Channel {channel_id} not usable (guild={guild_id}) "
            f"— scheduled reminder skipped"
        )
        return False
    try:
        await channel.send(body)
        return True
    except discord.HTTPException as e:
        # Forbidden / NotFound / bot-was-kicked are recoverable user-state
        # errors; print so leadership can see, but don't escalate to Sentry.
        print(f"[REMINDER] Channel post failed (guild={guild_id}, channel={channel_id}): {e}")
        return False
    except Exception as e:
        # Unexpected non-Discord errors (template bugs, schema drift) should
        # surface in Sentry — Railway-only logs are easy to miss.
        print(f"[REMINDER] Channel post failed (guild={guild_id}, channel={channel_id}): {e}")
        try:
            import sentry_sdk

            sentry_sdk.capture_exception(e)
        except Exception:
            pass
        return False


async def _send_reminder_via_dm(bot, guild_id: int, body: str) -> tuple[int, int]:
    """
    DM every member listed in the guild's Member Roster sheet. Returns
    (sent, skipped). Premium-gating happens at the call site — this helper
    just does the work.
    """
    import dm
    from config import get_member_roster_config, get_member_roster_sheet

    roster_cfg = get_member_roster_config(guild_id)
    if not roster_cfg.get("enabled"):
        return (0, 0)

    try:
        ws = get_member_roster_sheet(guild_id, roster_cfg["tab_name"])
        rows = await asyncio.get_event_loop().run_in_executor(None, ws.get_all_values)
    except Exception as e:
        print(f"[REMINDER] Could not read roster for guild {guild_id}: {e}")
        return (0, 0)

    did_col = roster_cfg["discord_id_col"]
    sent = 0
    skipped = 0
    for row in rows[1:]:  # skip header
        if did_col >= len(row):
            continue
        did = row[did_col].strip()
        if not did:
            skipped += 1
            continue
        ok = await dm.send_dm_to_id(bot, guild_id, did, content=body)
        if ok:
            sent += 1
        else:
            skipped += 1
    return (sent, skipped)


# ── Wizard hub ────────────────────────────────────────────────────────────────


class _ReminderHubView(discord.ui.View):
    """Top-level picker shown by the hub's Reminders button."""

    def __init__(self):
        super().__init__(timeout=120)
        self.choice: str | None = None  # "send" | "schedule" | None

    @discord.ui.button(label="📤 Send reminder now", style=discord.ButtonStyle.success)
    async def send_now(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = "send"
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()

    @discord.ui.button(label="⚙️ Manage scheduled reminders", style=discord.ButtonStyle.primary)
    async def manage(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = "schedule"
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = None
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(inter, content="Cancelled.", view=self)
        self.stop()


async def _run_remind_hub(interaction: discord.Interaction, bot):
    import premium as _prem

    is_premium_flag = await _prem.is_premium(
        interaction.guild_id,
        interaction=interaction,
        bot=bot,
    )

    view = _ReminderHubView()
    await interaction.response.send_message(
        "📋 **Survey Reminders**\n"
        "What would you like to do?\n"
        f"*Tier: {'💎 Premium' if is_premium_flag else 'Free'}*",
        view=view,
        ephemeral=True,
    )
    await view.wait()
    if view.choice == "send":
        await _run_send_now(interaction, bot, is_premium_flag)
    elif view.choice == "schedule":
        await _run_schedule_wizard(interaction, bot, is_premium_flag)


# ── Send-now path ─────────────────────────────────────────────────────────────


class _DestinationPickView(discord.ui.View):
    """Channel vs DM picker. DM option only enabled for Premium guilds."""

    def __init__(self, allow_dm: bool):
        super().__init__(timeout=120)
        self.choice: str | None = None  # "channel" | "dm" | None

        ch_btn = discord.ui.Button(
            label="📢 Post to a channel",
            style=discord.ButtonStyle.primary,
        )

        async def _ch(inter: discord.Interaction):
            self.choice = "channel"
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        ch_btn.callback = _ch
        self.add_item(ch_btn)

        dm_btn = discord.ui.Button(
            label="📨 DM via Member Roster" + ("" if allow_dm else " (💎 Premium)"),
            style=discord.ButtonStyle.secondary,
            disabled=not allow_dm,
        )

        async def _dm(inter: discord.Interaction):
            self.choice = "dm"
            for item in self.children:
                item.disabled = True
            await wizard_registry.safe_edit_response(inter, view=self)
            self.stop()

        dm_btn.callback = _dm
        self.add_item(dm_btn)


class _ChannelPickView(discord.ui.View):
    """Single-channel picker for the send-now flow."""

    def __init__(self):
        super().__init__(timeout=120)
        self.channel: discord.abc.GuildChannel | None = None

        sel = discord.ui.ChannelSelect(
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            placeholder="Pick a channel…",
        )

        async def _cb(inter: discord.Interaction):
            self.channel = sel.values[0].resolve() or sel.values[0]
            sel.disabled = True
            await wizard_registry.safe_edit_response(
                inter,
                content=f"✅ Channel: {self.channel.mention if hasattr(self.channel, 'mention') else self.channel}",
                view=self,
            )
            self.stop()

        sel.callback = _cb
        self.add_item(sel)


async def _run_send_now(interaction: discord.Interaction, bot, is_premium_flag: bool):
    # Premium with multiple surveys gets a survey selector; otherwise it's
    # the only configured survey.
    survey = await _pick_survey(
        interaction,
        prompt="📋 You have multiple surveys — which one are you reminding members about?",
    )
    if survey is None:
        await interaction.followup.send(
            HUB_TIMEOUT.format(cmd="survey", hub_btn=SURVEY_HUB_BTN_REMIND),
            ephemeral=True,
        )
        return

    body = survey.get("reminder_message") or _default_reminder_body(survey)

    dest_view = _DestinationPickView(allow_dm=is_premium_flag)
    await interaction.followup.send(
        f"📋 Reminder for **{survey.get('survey_name', 'Default')}** — where should it go?\n"
        f"{'' if is_premium_flag else 'ℹ️ *DM-via-roster is Premium-only — `/upgrade` to unlock.*'}",
        view=dest_view,
        ephemeral=True,
    )
    await dest_view.wait()
    if dest_view.choice is None:
        await interaction.followup.send(
            HUB_TIMEOUT.format(cmd="survey", hub_btn=SURVEY_HUB_BTN_REMIND), ephemeral=True
        )
        return

    if dest_view.choice == "channel":
        ch_view = _ChannelPickView()
        await interaction.followup.send(
            "📢 Pick the channel to post to:", view=ch_view, ephemeral=True
        )
        await ch_view.wait()
        if ch_view.channel is None:
            await interaction.followup.send(
                HUB_TIMEOUT.format(cmd="survey", hub_btn=SURVEY_HUB_BTN_REMIND), ephemeral=True
            )
            return
        ok = await _send_reminder_to_channel(bot, interaction.guild_id, ch_view.channel.id, body)
        if ok:
            await interaction.followup.send(
                f"✅ Posted reminder for **{survey.get('survey_name', 'Default')}** in "
                f"{ch_view.channel.mention if hasattr(ch_view.channel, 'mention') else '#?'}.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                "⚠️ Could not post to that channel — make sure the bot has permission.",
                ephemeral=True,
            )
        return

    # dest_view.choice == "dm" (Premium only)
    from config import get_member_roster_config

    roster_cfg = get_member_roster_config(interaction.guild_id)
    if not roster_cfg.get("enabled"):
        await interaction.followup.send(
            "⚙️ DM reminders need Member Sync. Run `/setup` → 👥 Member Sync first.",
            ephemeral=True,
        )
        return
    sent, skipped = await _send_reminder_via_dm(bot, interaction.guild_id, body)
    await interaction.followup.send(
        f"✅ Sent {sent} reminder DM{'s' if sent != 1 else ''} for "
        f"**{survey.get('survey_name', 'Default')}**. "
        f"{skipped} skipped (DMs closed, missing ID, or other failures).",
        ephemeral=True,
    )


# ── Schedule-management path ──────────────────────────────────────────────────

DAYS_OF_WEEK = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


class _FrequencyPickView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.choice: str | None = None

    @discord.ui.button(label="Off (disable)", style=discord.ButtonStyle.danger)
    async def off(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = "off"
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()

    @discord.ui.button(label="Daily", style=discord.ButtonStyle.primary)
    async def daily(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = "daily"
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()

    @discord.ui.button(label="Weekly", style=discord.ButtonStyle.success)
    async def weekly(self, inter: discord.Interaction, button: discord.ui.Button):
        self.choice = "weekly"
        for item in self.children:
            item.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()


class _DayPickView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.day: int | None = None
        sel = discord.ui.Select(
            placeholder="Day of the week…",
            options=[
                discord.SelectOption(label=name, value=str(i))
                for i, name in enumerate(DAYS_OF_WEEK)
            ],
        )

        async def _cb(inter: discord.Interaction):
            self.day = int(sel.values[0])
            sel.disabled = True
            await wizard_registry.safe_edit_response(
                inter, content=f"✅ Day: **{DAYS_OF_WEEK[self.day]}**", view=self
            )
            self.stop()

        sel.callback = _cb
        self.add_item(sel)


async def _run_schedule_wizard(interaction: discord.Interaction, bot, is_premium_flag: bool):
    """Walk leadership through configuring a survey's scheduled reminder."""
    from config import save_survey_reminder, get_config
    from setup_cog import _format_time_with_tz

    # Pick which survey
    survey = await _pick_survey(
        interaction,
        prompt="⚙️ Which survey are you scheduling reminders for?",
    )
    if survey is None:
        await interaction.followup.send(
            HUB_TIMEOUT.format(cmd="survey", hub_btn=SURVEY_HUB_BTN_REMIND),
            ephemeral=True,
        )
        return

    survey_id = survey.get("survey_id") or "default"
    survey_name = survey.get("survey_name") or "Default"

    guild_cfg = get_config(interaction.guild_id)
    guild_tz = guild_cfg.timezone if guild_cfg else "America/New_York"

    # Show current settings as context
    cur_freq = survey.get("reminder_frequency") or "off"
    cur_day = int(survey.get("reminder_day_of_week") or 1)
    cur_time = survey.get("reminder_time") or "12:00"
    cur_ch = int(survey.get("reminder_channel_id") or 0)
    cur_use_dm = bool(survey.get("reminder_use_dm"))
    cur_msg = survey.get("reminder_message") or ""

    cur_dest = (
        "DM via Member Roster" if cur_use_dm else (f"<#{cur_ch}>" if cur_ch else "*(not set)*")
    )
    cur_time_label = _format_time_with_tz(cur_time, guild_tz) or cur_time
    cur_when = (
        "Off"
        if cur_freq == "off"
        else f"Daily at {cur_time_label}"
        if cur_freq == "daily"
        else f"Weekly on {DAYS_OF_WEEK[cur_day]} at {cur_time_label}"
    )

    await interaction.followup.send(
        f"⚙️ **Scheduling reminders for `{survey_name}`**\n"
        f"**Current schedule:** {cur_when}\n"
        f"**Current destination:** {cur_dest}\n"
        f"**Current message:** {('*set*' if cur_msg else '*default*')}",
        ephemeral=True,
    )

    # ── Step 1: Frequency ─────────────────────────────────────────────────────
    freq_view = _FrequencyPickView()
    await interaction.followup.send(
        "**Step 1 — Frequency**\nHow often should this reminder fire?",
        view=freq_view,
        ephemeral=True,
    )
    await freq_view.wait()
    if freq_view.choice is None:
        await interaction.followup.send(
            HUB_TIMEOUT.format(cmd="survey", hub_btn=SURVEY_HUB_BTN_REMIND), ephemeral=True
        )
        return

    new_freq = freq_view.choice
    if new_freq == "off":
        save_survey_reminder(
            interaction.guild_id,
            survey_id,
            enabled=0,
            frequency="off",
            day_of_week=cur_day,
            time_str=cur_time,
            channel_id=cur_ch,
            use_dm=int(cur_use_dm),
            message=cur_msg,
        )
        await interaction.followup.send(
            f"✅ Scheduled reminders disabled for **{survey_name}**. "
            f"Run `/survey` and click **{SURVEY_HUB_BTN_REMIND}** to re-enable.",
            ephemeral=True,
        )
        return

    # ── Step 2: Day-of-week (weekly only) ─────────────────────────────────────
    new_day = cur_day
    if new_freq == "weekly":
        day_view = _DayPickView()
        await interaction.followup.send(
            "**Step 2 — Day of the week**\nWhich day should the reminder fire each week?",
            view=day_view,
            ephemeral=True,
        )
        await day_view.wait()
        if day_view.day is None:
            await interaction.followup.send(
                HUB_TIMEOUT.format(cmd="survey", hub_btn=SURVEY_HUB_BTN_REMIND), ephemeral=True
            )
            return
        new_day = day_view.day

    # ── Step 3: Time of day ───────────────────────────────────────────────────
    new_time, ok = await _ask_time(
        interaction,
        default=cur_time,
        step_label="Step 3 — Time of day",
        tz_name=guild_tz,
    )
    if not ok:
        return

    # ── Step 4: Destination ───────────────────────────────────────────────────
    dest_view = _DestinationPickView(allow_dm=is_premium_flag)
    await interaction.followup.send(
        f"**Step 4 — Where to send the reminder**\n"
        f"{'' if is_premium_flag else 'ℹ️ *DM-via-roster is Premium-only.*'}",
        view=dest_view,
        ephemeral=True,
    )
    await dest_view.wait()
    if dest_view.choice is None:
        await interaction.followup.send(
            HUB_TIMEOUT.format(cmd="survey", hub_btn=SURVEY_HUB_BTN_REMIND), ephemeral=True
        )
        return

    new_use_dm = 0
    new_channel = 0
    if dest_view.choice == "dm":
        new_use_dm = 1
    else:
        ch_view = _ChannelPickView()
        await interaction.followup.send(
            "📢 Pick the channel to post the reminder to:", view=ch_view, ephemeral=True
        )
        await ch_view.wait()
        if ch_view.channel is None:
            await interaction.followup.send(
                HUB_TIMEOUT.format(cmd="survey", hub_btn=SURVEY_HUB_BTN_REMIND), ephemeral=True
            )
            return
        new_channel = ch_view.channel.id

    # ── Step 5: Message body ──────────────────────────────────────────────────
    new_msg, ok = await _ask_reminder_message(interaction, bot, default=cur_msg)
    if not ok:
        return

    # ── Save ──────────────────────────────────────────────────────────────────
    save_survey_reminder(
        interaction.guild_id,
        survey_id,
        enabled=1,
        frequency=new_freq,
        day_of_week=new_day,
        time_str=new_time,
        channel_id=new_channel,
        use_dm=new_use_dm,
        message=new_msg,
    )

    new_time_label = _format_time_with_tz(new_time, guild_tz) or new_time
    when = (
        f"Daily at {new_time_label}"
        if new_freq == "daily"
        else f"Weekly on {DAYS_OF_WEEK[new_day]} at {new_time_label}"
    )
    where = "DMs to every roster member" if new_use_dm else f"<#{new_channel}>"
    await interaction.followup.send(
        f"✅ **{survey_name} reminders scheduled.**\n"
        f"**When:** {when}\n"
        f"**Where:** {where}\n"
        f"**Message:** {('*custom*' if new_msg else '*default*')}\n\n"
        f"Run `/survey` and click **{SURVEY_HUB_BTN_REMIND}** any time to update or disable.",
        ephemeral=True,
    )


async def _ask_time(
    interaction: discord.Interaction, *, default: str, step_label: str, tz_name: str | None = None
) -> tuple[str, bool]:
    """
    Ask leadership for a HH:MM time via a one-field modal. Re-prompts up to
    3 times on unparseable input. Returns (time_str_24h, ok). `tz_name`
    is used only to render the "current:" hint in the button label as
    e.g. `8:00am EDT` — saved values are still HH:MM 24h.
    """
    from setup_cog import _parse_12h_time, _format_time_with_tz

    current_label = _format_time_with_tz(default, tz_name) or default

    class _TimeModal(discord.ui.Modal, title="Reminder time"):
        time_in = discord.ui.TextInput(
            label="Time (e.g. 9:00am, 22:30, 12:00pm)",
            default=default,
            max_length=8,
            required=True,
        )

        def __init__(self):
            super().__init__()
            self.value: str | None = None

        async def on_submit(self, inter: discord.Interaction):
            self.value = str(self.time_in.value).strip()
            await inter.response.defer(ephemeral=True)
            self.stop()

    class _TimeView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=180)
            self.modal: _TimeModal | None = None

        @discord.ui.button(
            label=f"⏰ Set time (current: {current_label})", style=discord.ButtonStyle.primary
        )
        async def open_modal(self, inter: discord.Interaction, button: discord.ui.Button):
            self.modal = _TimeModal()
            await inter.response.send_modal(self.modal)
            await self.modal.wait()
            self.stop()

    attempts_left = 3
    while True:
        view = _TimeView()
        await interaction.followup.send(
            f"**{step_label}**\nWhat time should the reminder fire? *(your guild's timezone)*",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.modal is None or view.modal.value is None:
            await interaction.followup.send(
                HUB_TIMEOUT.format(cmd="survey", hub_btn=SURVEY_HUB_BTN_REMIND), ephemeral=True
            )
            return ("", False)
        raw = view.modal.value
        parsed = _parse_12h_time(raw)
        if parsed:
            return (parsed, True)
        if len(raw) == 5 and raw[2] == ":" and raw.replace(":", "").isdigit():
            return (raw, True)
        attempts_left -= 1
        if attempts_left <= 0:
            await interaction.followup.send(
                TIME_PARSE_GIVE_UP.format(recovery=f"`/survey` → **{SURVEY_HUB_BTN_REMIND}**"),
                ephemeral=True,
            )
            return ("", False)
        await interaction.followup.send(
            TIME_PARSE_RETRY.format(raw=raw),
            ephemeral=True,
        )


async def _ask_reminder_message(
    interaction: discord.Interaction, bot, *, default: str
) -> tuple[str, bool]:
    """
    Prompt for the reminder message body. Empty input keeps the existing
    custom message, or falls back to the generic default at fire time.
    Returns (body, ok).
    """

    class _MsgModal(discord.ui.Modal, title="Reminder message"):
        body_in = discord.ui.TextInput(
            label="Reminder message body",
            style=discord.TextStyle.paragraph,
            default=default[:4000] if default else "",
            placeholder=(
                "📋 Reminder — please fill out the survey this week!\n"
                "(Leave blank to use the bot's default message.)"
            ),
            required=False,
            max_length=2000,
        )

        def __init__(self):
            super().__init__()
            self.value: str | None = None
            self.confirmed = False

        async def on_submit(self, inter: discord.Interaction):
            self.value = str(self.body_in.value)
            self.confirmed = True
            await inter.response.defer(ephemeral=True)
            self.stop()

    class _MsgView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=300)
            self.modal: _MsgModal | None = None

        @discord.ui.button(label="✏️ Edit message", style=discord.ButtonStyle.primary)
        async def open_modal(self, inter: discord.Interaction, button: discord.ui.Button):
            self.modal = _MsgModal()
            await inter.response.send_modal(self.modal)
            await self.modal.wait()
            self.stop()

        @discord.ui.button(label="Use default", style=discord.ButtonStyle.secondary)
        async def use_default(self, inter: discord.Interaction, button: discord.ui.Button):
            self.modal = _MsgModal()
            self.modal.value = ""
            self.modal.confirmed = True
            await wizard_registry.safe_edit_response(
                inter, content="✅ Will use the default reminder message.", view=self
            )
            self.stop()

    view = _MsgView()
    await interaction.followup.send(
        "**Step 5 — Reminder message**\n"
        "What should the reminder say? Leave blank to use the bot's default.",
        view=view,
        ephemeral=True,
    )
    await view.wait()
    if view.modal is None or not view.modal.confirmed:
        await interaction.followup.send(
            HUB_TIMEOUT.format(cmd="survey", hub_btn=SURVEY_HUB_BTN_REMIND), ephemeral=True
        )
        return ("", False)
    return ((view.modal.value or "").strip(), True)


async def setup(bot: commands.Bot):
    await bot.add_cog(SurveyCog(bot))
