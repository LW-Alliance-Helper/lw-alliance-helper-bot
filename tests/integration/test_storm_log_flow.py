"""
Characterization tests for the participation log walk
(`storm_log.run_log_flow`, moved to `storm_log_flow.run_log_flow` in the
#589 step 11 refactor).

Written against the pre-refactor function; they pass unchanged against the
refactored one. They drive the real views the walk posts, in order, and the
chat replies it waits for, and pin the saved row, the per-member rows, the
prompts, and every exit. `tests/integration/test_draft_flows.py` keeps the
gate tests (participation off, no questions) and the two original walks.

The `Script` harness answers each posted view in order. An entry is a dict
of attributes to set before stopping the view (`confirmed` is added unless
the dict says otherwise), `"timeout"` to stop it with nothing set, or
`"cancel"` to fire the officer's `/cancel` while the view is waiting.
`Replies` feeds `bot.wait_for("message")`: a string is a reply, `"cancel"`
fires `/cancel` while the flow waits, `"timeout"` raises the wait's
`TimeoutError`.
"""

import asyncio
from datetime import date
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID
from tests.integration.test_draft_flows import _make_channel, _make_message


def _flow():
    import storm_log

    return storm_log.run_log_flow


HINT = "`/desertstorm` → **📊 Fill out participation questions**"


class Script:
    def __init__(self, channel, answers, get_cancel):
        self.answers = list(answers)
        self.sent = []
        self.views = []
        self.messages = []
        self.get_cancel = get_cancel
        channel.send = AsyncMock(side_effect=self._send)

    async def _send(self, content=None, view=None, **kw):
        self.sent.append(content)
        msg = _make_message()
        self.messages.append(msg)
        if view is None:
            return msg
        self.views.append(view)
        if not self.answers:
            raise AssertionError(
                f"unexpected view #{len(self.views)} {type(view).__name__}: {content!r}"
            )
        answer = self.answers.pop(0)
        if answer == "cancel":
            self.get_cancel().set()
            return msg
        if answer != "timeout":
            attrs = dict(answer)
            attrs.setdefault("confirmed", True)
            for k, v in attrs.items():
                setattr(view, k, v)
        try:
            view.stop()
        except Exception:
            pass
        return msg

    @property
    def texts(self):
        return [s or "" for s in self.sent]

    def index_of(self, needle):
        for i, s in enumerate(self.texts):
            if needle in s:
                return i
        raise AssertionError(f"{needle!r} never sent; sent: {self.texts}")

    def has(self, needle):
        return any(needle in s for s in self.texts)


class Replies:
    def __init__(self, replies, get_cancel):
        self.replies = list(replies)
        self.get_cancel = get_cancel
        self.deleted = []

    async def __call__(self, event, *, check=None, timeout=None):
        assert event == "message"
        if not self.replies:
            raise AssertionError("bot.wait_for called with no reply queued")
        r = self.replies.pop(0)
        if r == "timeout":
            raise asyncio.TimeoutError
        if r == "cancel":
            self.get_cancel().set()
            await asyncio.sleep(1)  # the cancel race wins; this never returns
            raise AssertionError("cancel did not win the race")
        m = MagicMock(content=r)
        m.delete = AsyncMock(side_effect=lambda: self.deleted.append(r))
        return m


ROSTER = (["Alpha", "Bravo", "Charlie"], {"al": "Alpha"})


def _configure(questions, *, event_type="DS", log_channel_id=0):
    import config

    config.save_storm_config(
        TEST_GUILD_ID, event_type, "Tab", "Body", "America/New_York", log_channel_id
    )
    config.save_participation_config(
        TEST_GUILD_ID,
        event_type,
        enabled=1,
        tab_name="Participation Log",
        questions=questions,
        roster_tab="Roster",
        roster_name_col=0,
        roster_alias_col=-1,
        roster_start_row=2,
    )


class Run:
    """One walk. `result.row` is the saved event row (or None), `result.member`
    the per-member upsert call (or None)."""

    def __init__(
        self,
        *,
        views=(),
        replies=(),
        roster=ROSTER,
        recent=(),
        prefill=None,
        counts=None,
        append_error=None,
        member_error=None,
        get_channel=None,
    ):
        self.views, self.replies_in, self.roster = views, replies, roster
        self.recent, self.prefill, self.counts = list(recent), prefill or set(), counts or {}
        self.append_error, self.member_error = append_error, member_error
        self.get_channel = get_channel
        self.row = None
        self.member = None
        self.roster_loads = 0

    async def go(self, event_type="DS"):
        import storm_log

        channel = _make_channel()
        user = MagicMock(id=42)
        user.mention = "@officer"
        get_cancel = lambda: storm_log.active_logs[42]  # noqa: E731
        self.script = Script(channel, self.views, get_cancel)
        self.replies = Replies(self.replies_in, get_cancel)
        bot = AsyncMock()
        bot.wait_for = self.replies  # a plain coroutine function; ensure_future wraps it
        bot.get_channel = MagicMock(side_effect=self.get_channel or (lambda cid: None))

        def load_roster(guild_id, et):
            self.roster_loads += 1
            return self.roster

        def append(guild_id, et, log_date, answers):
            if self.append_error:
                raise self.append_error
            self.row = (et, log_date, dict(answers))

        def upsert(guild_id, et, log_date, per_member, keys):
            if self.member_error:
                raise self.member_error
            self.member = (log_date, {k: dict(v) for k, v in per_member.items()}, list(keys))

        with (
            patch("storm_log.load_roster_from_config", side_effect=load_roster),
            patch("storm_log._collect_recent_event_dates", return_value=self.recent),
            patch("storm_log._prefill_from_discord_poll", return_value=self.prefill),
            patch("storm_log.count_member_flags_in_window", return_value=self.counts),
            patch("storm_log.append_participation_row", side_effect=append),
            patch("storm_log.upsert_member_log_rows", side_effect=upsert),
        ):
            await _flow()(bot, channel, user, event_type)
        assert 42 not in storm_log.active_logs, "active_logs not cleaned up"
        assert self.script.answers == [], f"unanswered views: {self.script.answers}"
        assert self.replies.replies == [], f"unused replies: {self.replies.replies}"
        return self


PICK_TODAY = {"picked_date": date(2026, 9, 12)}
TEXT_Q = {"key": "outcome", "label": "Outcome", "type": "text"}


# ── Date step ────────────────────────────────────────────────────────────────


class TestDateStep:
    @pytest.mark.asyncio
    async def test_listed_date_is_the_log_date(self, seeded_db):
        _configure([TEXT_Q])
        r = await Run(views=[PICK_TODAY], replies=["Win"]).go()
        assert r.row == ("DS", date(2026, 9, 12), {"outcome": "Win"})
        assert r.script.texts[0].startswith("📋 **Desert Storm Log** started by @officer")
        assert "*2 step(s) total. Use `/cancel` at any time to stop.*" in r.script.texts[0]
        assert "**Step 1: Event date**" in r.script.texts[1]
        assert r.script.views[0].__class__.__name__ == "_LogDatePickerView"

    @pytest.mark.asyncio
    async def test_recent_dates_reach_the_picker(self, seeded_db):
        _configure([TEXT_Q])
        r = await Run(views=[PICK_TODAY], replies=["Win"], recent=["2026-09-05"]).go()
        import discord

        select = next(c for c in r.script.views[0].children if isinstance(c, discord.ui.Select))
        values = [o.value for o in select.options]
        assert "2026-09-05" in values
        assert (
            values[0] == "__today__" and values[1] == "__yesterday__" and values[-1] == "__manual__"
        )

    @pytest.mark.asyncio
    async def test_typed_today(self, seeded_db):
        _configure([TEXT_Q])
        r = await Run(views=[{"wants_manual": True}], replies=["today", "Win"]).go()
        # Pins the pre-refactor behaviour: the container's calendar day, not
        # the server day. The rule says never; the fix is its own commit.
        assert r.row[1] == date.today()  # noqa: DTZ011
        assert "Type the date (e.g. `April 14`, `4/14`) or type `today`:" in r.script.texts
        # The prompt and the reply are both deleted after a typed answer.
        assert r.replies.deleted == ["today", "Win"]

    @pytest.mark.asyncio
    async def test_typed_date_parses(self, seeded_db):
        _configure([TEXT_Q])
        r = await Run(views=[{"wants_manual": True}], replies=["4/14", "Win"]).go()
        assert r.row[1] == date(2026, 4, 14)

    @pytest.mark.asyncio
    async def test_typed_garbage_ends_the_log(self, seeded_db):
        _configure([TEXT_Q])
        r = await Run(views=[{"wants_manual": True}], replies=["garbage"]).go()
        assert r.row is None
        assert (
            r.script.texts[-1]
            == f"⚠️ Could not parse `garbage` as a date. Run {HINT} to start again."
        )

    @pytest.mark.asyncio
    async def test_picker_cancel_button(self, seeded_db):
        _configure([TEXT_Q])
        r = await Run(views=[{"cancelled": True}]).go()
        assert r.row is None
        assert r.script.texts[-1] == "❌ Log canceled."

    @pytest.mark.asyncio
    async def test_picker_timeout(self, seeded_db):
        _configure([TEXT_Q])
        r = await Run(views=["timeout"]).go()
        assert r.row is None
        assert r.script.texts[-1] == f"⏰ Timed out. Run {HINT} to start again."
        r.script.messages[1].delete.assert_awaited()

    @pytest.mark.asyncio
    async def test_slash_cancel_during_picker(self, seeded_db):
        _configure([TEXT_Q])
        r = await Run(views=["cancel"]).go()
        assert r.row is None
        assert r.script.texts[-1] == "❌ Log canceled."
        assert all(c.disabled for c in r.script.views[0].children)

    @pytest.mark.asyncio
    async def test_slash_cancel_during_typed_reply(self, seeded_db):
        _configure([TEXT_Q])
        r = await Run(views=[{"wants_manual": True}], replies=["cancel"]).go()
        assert r.row is None
        assert r.script.texts[-1] == "❌ Log canceled."
        r.script.messages[-2].delete.assert_awaited()

    @pytest.mark.asyncio
    async def test_typed_reply_timeout(self, seeded_db):
        _configure([TEXT_Q])
        r = await Run(views=[{"wants_manual": True}], replies=["timeout"]).go()
        assert r.row is None
        assert r.script.texts[-1] == f"⏰ Timed out. Run {HINT} to start again."


# ── Question types ───────────────────────────────────────────────────────────


class TestQuestionTypes:
    @pytest.mark.asyncio
    async def test_yes_no(self, seeded_db):
        _configure([{"key": "won", "label": "Won?", "type": "yes_no"}])
        r = await Run(views=[PICK_TODAY, {"value": False}]).go()
        assert r.row[2] == {"won": "No"}
        assert r.script.texts[2] == "**Step 2 of 2: Won?**\nPick one."

    @pytest.mark.asyncio
    async def test_numeric_bounds_and_retries(self, seeded_db):
        _configure([{"key": "kills", "label": "Kills", "type": "numeric", "min": 1, "max": 10}])
        r = await Run(views=[PICK_TODAY], replies=["x", "0", "11", "3.5"]).go()
        assert r.row[2] == {"kills": "3.5"}
        assert r.script.has("**Step 2 of 2: Kills** *(min `1`, max `10`)*\nType a number.")
        assert r.script.has("⚠️ `x` isn't a number. Please re-enter your answer.")
        assert r.script.has("⚠️ Must be at least **1**. Please re-enter.")
        assert r.script.has("⚠️ Must be at most **10**. Please re-enter.")

    @pytest.mark.asyncio
    async def test_numeric_integer_stays_integer(self, seeded_db):
        _configure([{"key": "kills", "label": "Kills", "type": "numeric"}])
        r = await Run(views=[PICK_TODAY], replies=["42"]).go()
        assert r.row[2] == {"kills": "42"}
        assert r.script.has("**Step 2 of 2: Kills**\nType a number.")

    @pytest.mark.asyncio
    async def test_numeric_five_failures_give_up(self, seeded_db):
        _configure([{"key": "kills", "label": "Kills", "type": "numeric"}])
        r = await Run(views=[PICK_TODAY], replies=["a", "b", "c", "d", "e"]).go()
        assert r.row is None
        assert r.script.texts[-1] == (
            f"⚠️ Too many invalid attempts. Canceling the log. run {HINT} when you're ready to try again."
        )

    @pytest.mark.asyncio
    async def test_roster_names(self, seeded_db):
        _configure(
            [
                {"key": "mvp", "label": "MVPs", "type": "roster_names"},
                {"key": "sat", "label": "Sat out", "type": "roster_names"},
            ]
        )
        r = await Run(
            views=[
                PICK_TODAY,
                {"selected": ["Charlie", "Alpha"], "unrecognized": ["Zulu"]},
                {"selected": [], "unrecognized": []},
            ]
        ).go()
        assert r.row[2] == {"mvp": "Alpha, Charlie, Zulu", "sat": ""}
        assert r.roster_loads == 1
        i = r.script.index_of("⏳ Loading roster from your configured tab…")
        r.script.messages[i].delete.assert_awaited()
        assert r.script.has(
            "**Step 2 of 3: MVPs**\nPress **Enter Names** to type who applies. "
            "Press **Skip** if none.\n*Roster: Alpha, Bravo, Charlie*"
        )

    @pytest.mark.asyncio
    async def test_roster_preview_collapses_past_25(self, seeded_db):
        _configure([{"key": "mvp", "label": "MVPs", "type": "roster_names"}])
        names = [f"M{i}" for i in range(30)]
        r = await Run(
            views=[PICK_TODAY, {"selected": [], "unrecognized": []}], roster=(names, {})
        ).go()
        assert r.script.has("*Roster: 30 members loaded*")

    @pytest.mark.asyncio
    async def test_empty_roster_ends_the_log(self, seeded_db):
        _configure([{"key": "mvp", "label": "MVPs", "type": "roster_names"}])
        r = await Run(views=[PICK_TODAY], roster=([], {})).go()
        assert r.row is None
        assert r.script.texts[-1] == (
            "⚠️ The configured roster tab is empty or unreachable. "
            "Run `/setup → ⚔️ Desert Storm` to update the roster source, then try again."
        )

    @pytest.mark.asyncio
    async def test_single_and_multi_select(self, seeded_db):
        _configure(
            [
                {
                    "key": "zone",
                    "label": "Zone",
                    "type": "single_select",
                    "options": ["North", "South"],
                },
                {
                    "key": "tags",
                    "label": "Tags",
                    "type": "multi_select",
                    "options": ["x", "y", "z"],
                },
                {"key": "none", "label": "No options", "type": "single_select"},
                {
                    "key": "none2",
                    "label": "No options either",
                    "type": "multi_select",
                    "options": [],
                },
            ]
        )
        r = await Run(views=[PICK_TODAY, {"selected": {"South"}}, {"selected": {"z", "x"}}]).go()
        assert r.row[2] == {"zone": "South", "tags": "x, z", "none": "", "none2": ""}
        assert r.script.has("**Step 2 of 5: Zone**\nPick one.")
        assert r.script.has("**Step 3 of 5: Tags**\nPick any that apply.")

    @pytest.mark.asyncio
    async def test_date_question(self, seeded_db):
        _configure([{"key": "when", "label": "When", "type": "date"}])
        r = await Run(views=[PICK_TODAY], replies=["nope", "04/14/2026"]).go()
        assert r.row[2] == {"when": "2026-04-14"}
        assert r.script.has("**Step 2 of 2: When** *(format `%m/%d/%Y`)*")
        assert r.script.has("⚠️ `nope` doesn't match `%m/%d/%Y`. Please re-enter.")

    @pytest.mark.asyncio
    async def test_date_question_custom_format_and_give_up(self, seeded_db):
        _configure([{"key": "when", "label": "When", "type": "date", "date_format": "%Y-%m-%d"}])
        r = await Run(views=[PICK_TODAY], replies=["a", "b", "c", "d", "e"]).go()
        assert r.row is None
        assert r.script.texts[-1] == "⚠️ Too many invalid attempts. Canceling the log."

    @pytest.mark.asyncio
    async def test_text_skip(self, seeded_db):
        _configure([TEXT_Q, {"key": "note", "label": "Note", "type": "text"}])
        r = await Run(views=[PICK_TODAY], replies=["skip", "Fine"]).go()
        assert r.row[2] == {"outcome": "", "note": "Fine"}
        assert r.script.has("**Step 2 of 3: Outcome**\nType your answer (or `skip` for none).")

    @pytest.mark.asyncio
    async def test_unknown_type_is_text(self, seeded_db):
        _configure([{"key": "q", "label": "Mystery", "type": "hologram"}])
        r = await Run(views=[PICK_TODAY], replies=["hi"]).go()
        assert r.row[2] == {"q": "hi"}

    @pytest.mark.asyncio
    async def test_missing_key_and_label_fall_back(self, seeded_db):
        _configure([{"type": "text"}])
        r = await Run(views=[PICK_TODAY], replies=["hi"]).go()
        assert r.row[2] == {"q2": "hi"}
        assert r.script.has("**Step 2 of 2: q2**")


# ── Per-member question types (#244) ─────────────────────────────────────────


class TestPerMember:
    @pytest.mark.asyncio
    async def test_roster_multi_select_writes_flags(self, seeded_db):
        _configure([{"key": "attended", "label": "Attended", "type": "roster_multi_select"}])
        r = await Run(views=[PICK_TODAY, {"selected_set": {"Bravo"}}]).go()
        assert r.row[2] == {"attended": "1 member(s)"}
        assert r.member == (
            date(2026, 9, 12),
            {
                "Alpha": {"attended": "no"},
                "Bravo": {"attended": "yes"},
                "Charlie": {"attended": "no"},
            },
            ["attended"],
        )
        assert r.script.has(
            "**Step 2 of 2: Attended**\nUse the dropdown(s) to pick the members who match. Click ✅ Save when done."
        )
        assert not r.script.has("Pre-checked")

    @pytest.mark.asyncio
    async def test_roster_multi_select_prefill(self, seeded_db):
        _configure(
            [
                {
                    "key": "attended",
                    "label": "Attended",
                    "type": "roster_multi_select",
                    "prefill_source": "discord_poll",
                }
            ]
        )
        r = await Run(
            views=[PICK_TODAY, {"selected_set": {"Alpha", "Bravo"}}], prefill={"Alpha", "Bravo"}
        ).go()
        assert r.script.has(
            "🗳️ Pre-checked members are those who voted to attend in the Discord signup poll."
        )
        assert r.script.has("*Pre-checked:* Alpha, Bravo")
        assert r.script.views[1].selected_set == {"Alpha", "Bravo"}

    @pytest.mark.asyncio
    async def test_prefill_preview_caps_at_five(self, seeded_db):
        _configure(
            [
                {
                    "key": "attended",
                    "label": "Attended",
                    "type": "roster_multi_select",
                    "prefill_source": "discord_poll",
                }
            ]
        )
        names = [f"M{i:02d}" for i in range(8)]
        r = await Run(
            views=[PICK_TODAY, {"selected_set": set(names)}], roster=(names, {}), prefill=set(names)
        ).go()
        assert r.script.has("*Pre-checked:* M00, M01, M02, M03, M04 (+3 more)")

    @pytest.mark.asyncio
    async def test_roster_multi_select_empty_roster(self, seeded_db):
        _configure([{"key": "attended", "label": "Attended", "type": "roster_multi_select"}])
        r = await Run(views=[PICK_TODAY], roster=([], {})).go()
        assert r.row is None
        assert r.script.texts[-1] == (
            "⚠️ The configured roster tab is empty or unreachable. "
            "Run `/setup → ⚔️ Desert Storm` to update the roster source."
        )

    @pytest.mark.asyncio
    async def test_derived_count(self, seeded_db):
        _configure(
            [
                {
                    "key": "streak",
                    "label": "Streak",
                    "type": "derived_count",
                    "source_question_key": "attended",
                    "lookback_events": 3,
                    "show_during_log": True,
                }
            ]
        )
        r = await Run(views=[PICK_TODAY], counts={"Bravo": 2, "Alpha": 3, "Zulu": 0}).go()
        assert r.row[2] == {"streak": "max 3 (past 3 events)"}
        assert r.member[1] == {
            "Alpha": {"streak": "3"},
            "Bravo": {"streak": "2"},
            "Charlie": {"streak": "0"},
        }
        assert r.member[2] == ["streak"]
        assert r.script.has(
            "**Step 2 of 2: Streak**\n📊 Top by count in past 3 events: Alpha (3), Bravo (2)"
        )

    @pytest.mark.asyncio
    async def test_derived_count_quiet_when_not_shown(self, seeded_db):
        _configure(
            [
                {
                    "key": "streak",
                    "label": "Streak",
                    "type": "derived_count",
                    "source_question_key": "attended",
                }
            ]
        )
        r = await Run(views=[PICK_TODAY], counts={}).go()
        assert r.row[2] == {"streak": "max 0 (past 4 events)"}
        assert not r.script.has("📊 Top by count")

    @pytest.mark.asyncio
    async def test_derived_count_without_source_is_skipped(self, seeded_db):
        _configure(
            [
                {"key": "streak", "label": "Streak", "type": "derived_count"},
                TEXT_Q,
            ]
        )
        r = await Run(views=[PICK_TODAY], replies=["Win"]).go()
        assert r.row[2] == {"outcome": "Win"}
        assert r.member is None
        assert r.script.has("⚠️ Derived count `Streak` has no source question configured. Skipping.")


# ── Save and summary ─────────────────────────────────────────────────────────


class TestSaveAndSummary:
    @pytest.mark.asyncio
    async def test_summary_and_mirror(self, seeded_db):
        _configure([TEXT_Q, {"key": "note", "label": "Note", "type": "text"}], log_channel_id=777)
        target = AsyncMock()
        r = await Run(
            views=[PICK_TODAY],
            replies=["Win", "skip"],
            get_channel=lambda cid: target if cid == 777 else None,
        ).go()
        summary = "📋 **Desert Storm Log: Saturday, September 12, 2026**\n**Outcome:** Win\n**Note:** None"
        assert r.script.has("💾 Saving log…")
        assert r.script.texts[-1] == f"✅ **Log saved!**\n\n{summary}"
        target.send.assert_awaited_once_with(summary)

    @pytest.mark.asyncio
    async def test_no_mirror_to_the_same_channel(self, seeded_db):
        _configure([TEXT_Q], log_channel_id=111111111111111111)
        calls = []
        r = await Run(
            views=[PICK_TODAY], replies=["Win"], get_channel=lambda cid: calls.append(cid)
        ).go()
        assert r.row is not None
        assert calls == []

    @pytest.mark.asyncio
    async def test_mirror_failure_is_swallowed(self, seeded_db):
        _configure([TEXT_Q], log_channel_id=777)
        target = AsyncMock()
        target.send = AsyncMock(side_effect=RuntimeError("boom"))
        r = await Run(views=[PICK_TODAY], replies=["Win"], get_channel=lambda cid: target).go()
        assert r.row is not None
        assert r.script.texts[-1].startswith("✅ **Log saved!**")

    @pytest.mark.asyncio
    async def test_append_failure(self, seeded_db):
        _configure([TEXT_Q])
        r = await Run(views=[PICK_TODAY], replies=["Win"], append_error=RuntimeError("quota")).go()
        assert r.script.texts[-1] == "⚠️ Error saving to sheet: quota"
        assert r.member is None

    @pytest.mark.asyncio
    async def test_member_log_failure_still_summarises(self, seeded_db):
        _configure([{"key": "attended", "label": "Attended", "type": "roster_multi_select"}])
        r = await Run(
            views=[PICK_TODAY, {"selected_set": set()}], member_error=RuntimeError("tab")
        ).go()
        assert r.row is not None
        assert r.script.has("⚠️ Saved event row, but per-member log failed: tab")
        assert r.script.texts[-1].startswith("✅ **Log saved!**")

    @pytest.mark.asyncio
    async def test_canyon_storm_labels(self, seeded_db):
        _configure([TEXT_Q], event_type="CS")
        r = await Run(views=[PICK_TODAY], replies=["Win"]).go("CS")
        assert r.row[0] == "CS"
        assert r.script.texts[0].startswith("📋 **Canyon Storm Log** started by")
        assert (
            r.script.texts[-1].startswith("📋 **Canyon Storm Log: ")
            or "Canyon Storm Log:" in r.script.texts[-1]
        )
