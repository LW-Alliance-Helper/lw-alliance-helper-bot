"""
Characterization tests for the birthday wizard (`setup_cog.run_birthday_setup`),
written against the function before its move out of `setup_cog` (#611
round 2) and kept unchanged after it.

The six existing birthday tests answer every view with one flat override
dict. These drive the views one at a time, in order, and hold the prompts,
the button labels, the saved row and the confirmation embed.
`ask_keep_or_change`, `ChannelSelectStep` and `ask_disable_with_clear` are
patched in both of their homes so the same file holds before and after
the move.
"""

import importlib
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# setup_cog re-exports the wizard pieces by name at import time; import it
# before anything patches `wizard_steps.X` (see CLAUDE.md, test gotchas).
import setup_cog  # noqa: F401

from tests.conftest import TEST_GUILD_ID, make_mock_interaction
from messages import (
    WIZARD_TIMEOUT,
    TIME_PARSE_RETRY,
    TIME_PARSE_GIVE_UP,
    INPUT_INVALID,
    PREV_CHANNEL_GONE,
    SETUP_POINTER_FOOTER,
)

TIMEOUT = WIZARD_TIMEOUT.format(wizard="🎂 Birthdays")
RECOVERY = "`/setup` → 🎂 Birthdays"
USER_ID = 123456789
G = TEST_GUILD_ID


def _t(hhmm):
    """A time the way the wizard renders it (the zone abbreviation follows DST)."""
    from wizard_time import _format_time_with_tz

    return _format_time_with_tz(hhmm, "America/New_York")


def _wizard():
    import setup_cog

    return setup_cog.run_birthday_setup


def _both(name):
    """The homes of a name that may have moved: whichever of `setup_cog`,
    `wizard_steps` and the birthday module currently holds it."""
    targets = []
    for home in ("setup_cog", "wizard_steps", "birthday_setup"):
        try:
            module = importlib.import_module(home)
        except ImportError:
            continue
        if hasattr(module, name):
            targets.append(f"{home}.{name}")
    return targets


class Script:
    def __init__(self, channel, answers, cancel_event=None):
        self.answers = list(answers)
        self.sent = []
        self.embeds = []
        self.views = []
        self.cancel_event = cancel_event
        channel.send = AsyncMock(side_effect=self._send)

    async def _send(self, content=None, embed=None, view=None, **kw):
        self.sent.append(content or "")
        self.embeds.append(embed)
        if view is None:
            return MagicMock(id=1)
        self.views.append(view)
        if isinstance(view, MagicMock):
            return MagicMock(id=1)  # a channel-step stand-in: answered already
        if not self.answers:
            raise AssertionError(
                f"unexpected view #{len(self.views)} {type(view).__name__}: {content!r}"
            )
        answer = self.answers.pop(0)
        if answer == "cancel":
            self.cancel_event.set()
            return MagicMock(id=1)
        if answer != "timeout":
            attrs = answer if isinstance(answer, dict) else dict([answer])
            for attr, value in attrs.items():
                setattr(view, attr, value)
        view.stop()
        return MagicMock(id=1)

    def index_of(self, needle):
        for i, s in enumerate(self.sent):
            if needle in s:
                return i
        raise AssertionError(f"{needle!r} never sent; sent: {self.sent}")

    def assert_order(self, *needles):
        positions = [self.index_of(n) for n in needles]
        assert positions == sorted(positions), list(zip(needles, positions))

    def assert_never(self, needle):
        assert not any(needle in s for s in self.sent), self.sent

    def view_named(self, name, nth=0):
        return [v for v in self.views if type(v).__name__ == name][nth]

    @property
    def final_embed(self):
        embeds = [e for e in self.embeds if e is not None]
        assert embeds, "no embed sent"
        return embeds[-1]


class KeepRecorder:
    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def __enter__(self):
        rec = self

        async def fake(channel, prompt, **kw):
            rec.calls.append((prompt, kw))
            return rec.values.pop(0) if rec.values else kw.get("default")

        self._p = [patch(t, side_effect=fake) for t in _both("ask_keep_or_change")]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()
        return False

    def prompt(self, i):
        return self.calls[i][0]

    def kw(self, i):
        return self.calls[i][1]


def _channel_step(channel_id=600001, *, stale=False, confirmed=True):
    return MagicMock(
        confirmed=confirmed,
        cancelled=False,
        selected_channel=MagicMock(id=channel_id),
        is_current_stale=stale,
        wait=AsyncMock(),
    )


class _Env:
    """The patches every drive shares: the channel picker stand-ins, the
    disable helper (its kwargs recorded), premium, and a registry hook so a
    script can fire the wizard's cancel event."""

    def __init__(self, *, is_premium=False, channel_steps=None, script=None):
        self.is_premium = is_premium
        self.steps = list(channel_steps or [_channel_step()])
        self.script = script
        self.channel_calls = []
        self.disable_calls = []

    def __enter__(self):
        import wizard_registry

        real_register = wizard_registry.register
        script = self.script
        env = self

        def _register(user_id):
            ev = real_register(user_id)
            if script is not None:
                script.cancel_event = ev
            return ev

        def _pick(*a, **kw):
            self.channel_calls.append(kw)
            return self.steps.pop(0)

        async def _disable(channel, **kw):
            env.disable_calls.append(kw)

        self._p = [
            patch("premium.is_premium", AsyncMock(return_value=self.is_premium)),
            patch("wizard_registry.register", side_effect=_register),
        ]
        self._p += [patch(t, side_effect=_pick) for t in _both("ChannelSelectStep")]
        self._p += [patch(t, side_effect=_disable) for t in _both("ask_disable_with_clear")]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()
        return False


async def _drive(answers, *, keep=(), is_premium=False, channel_steps=None):
    interaction = make_mock_interaction()
    script = Script(interaction.channel, answers)
    with (
        _Env(is_premium=is_premium, channel_steps=channel_steps, script=script) as env,
        KeepRecorder(keep) as rec,
    ):
        await _wizard()(interaction, AsyncMock())
    return script, rec, env


def _cfg():
    import config

    return config.get_birthday_config(G)


def _has():
    import config

    return config.has_birthday_config(G)


def _save(**over):
    import config

    kw = dict(
        tab_name="Members",
        name_col=2,
        birthday_col=3,
        discord_id_col=-1,
        data_start_row=2,
        enabled=1,
        train_integration=0,
        flexible_placement=0,
        lookahead_days=14,
        reminders_enabled=0,
        reminder_channel_id=0,
        reminder_time="08:00",
        dm_message="",
    )
    kw.update(over)
    config.save_birthday_config(G, **kw)


# ── Fresh ────────────────────────────────────────────────────────────────────


MINIMAL = [("selected", True), ("selected", False), ("selected", False)]  # enable, train, remind


class TestFresh:
    @pytest.mark.asyncio
    async def test_minimal_saves_and_summarises(self, seeded_db):
        s, rec, env = await _drive(MINIMAL)
        s.assert_order(
            "⚙️ **Birthday Tracking Setup**\nConfigure how the bot tracks member birthdays.",
            "**Step 1 of 9 — Enable birthday tracking?**\n"
            "Should the bot track member birthdays from your Google Sheet?",
            "**Step 5 of 9 — Train Schedule Integration**\n"
            "Should the bot automatically add members to the train schedule on their birthday?",
            "ℹ️ *Skipping Steps 6–7 (placement and lookahead) — train integration is off.*",
            "**Step 8 of 9 — Birthday Reminders**\n"
            "Should the bot post a message in Discord on a member's birthday?\n"
            '*(It will post: "🎂 Today is **[name]**\'s birthday!")*',
            "ℹ️ *Skipping Steps 8a–8b (reminder channel and time) — birthday reminders are off.*",
        )
        assert [type(v).__name__ for v in s.views] == ["YesNoView"] * 3
        assert rec.prompt(0) == (
            "**Step 2 of 9 — Sheet Tab**\n"
            "Which tab in your Google Sheet contains birthday data?\n"
            "⚠️ *Make sure this tab exists in your sheet before continuing.*"
        )
        assert rec.kw(0) == dict(
            default="Birthdays",
            current="Birthdays",
            modal_title="Sheet Tab Name",
            modal_label="Tab name",
            timeout_cmd="setup_birthdays",
            cancel_event=s.cancel_event,
        )
        assert rec.prompt(1) == (
            "**Step 3 of 9 — Name Column**\nWhich column contains the member's name?"
        )
        assert rec.kw(1)["default"] == "A" and rec.kw(1)["current"] == "A"
        assert rec.kw(1)["modal_title"] == "Name Column"
        assert rec.kw(1)["modal_label"] == "Column letter"
        assert rec.prompt(2) == (
            "**Step 4 of 9 — Birthday Column**\n"
            "Which column contains the member's birthday?\n"
            "ℹ️ *The bot accepts most date formats: `12/7`, `12-7`, `Dec 7`, "
            "`December 7`, `1990-12-07`, etc. Bare numeric dates like `7/12` are "
            "read as **M/D** (July 12) — use `Dec 7` if your alliance writes "
            "day-first.*"
        )
        assert rec.kw(2)["default"] == "B" and rec.kw(2)["current"] == "B"
        assert rec.kw(2)["modal_title"] == "Birthday Column"
        assert len(rec.calls) == 3 and env.channel_calls == [] and env.disable_calls == []
        cfg = _cfg()
        assert cfg["enabled"] == 1 and cfg["tab_name"] == "Birthdays"
        assert cfg["name_col"] == 0 and cfg["birthday_col"] == 1 and cfg["discord_id_col"] == -1
        assert cfg["data_start_row"] == 2 and cfg["train_integration"] == 0
        assert cfg["flexible_placement"] == 0 and cfg["lookahead_days"] == 14
        assert cfg["reminders_enabled"] == 0 and cfg["reminder_channel_id"] == 0
        assert cfg["reminder_time"] == "08:00" and cfg["dm_message"] == ""
        embed = s.final_embed
        assert embed.title == "✅ Birthday Tracking Configured"
        assert [(f.name, f.value, f.inline) for f in embed.fields] == [
            ("Sheet Tab", "Birthdays", True),
            ("Name Column", "Column A", True),
            ("Birthday Column", "Column B", True),
            ("Discord ID Column", "Not stored", True),
            ("Train Integration", "Disabled", True),
            ("Reminders", "Disabled", True),
        ]
        assert embed.footer.text == SETUP_POINTER_FOOTER.format(wizard="🎂 Birthdays")
        assert embed.footer.text == "Run `/setup` → 🎂 Birthdays to update settings."

    @pytest.mark.asyncio
    async def test_columns_are_letters_case_and_space_insensitive(self, seeded_db):
        s, _, _ = await _drive(MINIMAL, keep=["Sheet", " c ", "aa"])
        cfg = _cfg()
        assert cfg["tab_name"] == "Sheet" and cfg["name_col"] == 2 and cfg["birthday_col"] == 26
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert fields["Name Column"] == "Column C" and fields["Birthday Column"] == "Column AA"

    @pytest.mark.parametrize("bad", ["", "1", "A1", "a-b"])
    @pytest.mark.asyncio
    async def test_bad_name_column_ends_the_wizard(self, seeded_db, bad):
        s, rec, _ = await _drive([("selected", True)], keep=["Sheet", bad])
        assert s.sent[-1] == INPUT_INVALID.format(
            type="single column letter", example="A", recovery=RECOVERY
        )
        assert len(rec.calls) == 2 and not _has()

    @pytest.mark.asyncio
    async def test_bad_birthday_column_ends_the_wizard(self, seeded_db):
        s, rec, _ = await _drive([("selected", True)], keep=["Sheet", "A", "2"])
        assert s.sent[-1] == INPUT_INVALID.format(
            type="single column letter", example="B", recovery=RECOVERY
        )
        assert len(rec.calls) == 3 and not _has()

    @pytest.mark.asyncio
    async def test_another_feature_s_tab_only_warns(self, seeded_db):
        with patch("config.tabs_in_use", return_value={"members": "your Growth tab"}) as tabs:
            s, _, _ = await _drive(MINIMAL, keep=["Members"])
        assert tabs.call_args.kwargs == dict(
            exclude_field="birthday_tab_name", exclude_survey_id=None
        )
        assert s.sent[s.index_of("Heads up")].startswith(
            "⚠️ Heads up: **Members** is also your Growth tab."
        )
        assert _cfg()["tab_name"] == "Members"


class TestTrainIntegration:
    ON = [("selected", True), ("selected", True), ("selected", 1), ("selected", False)]

    @pytest.mark.asyncio
    async def test_on_asks_placement_and_lookahead(self, seeded_db):
        s, rec, _ = await _drive(self.ON, keep=["Birthdays", "A", "B", "21"])
        s.assert_order(
            "**Step 5 of 9",
            "ℹ️ Heads up: birthdays auto-populate the train schedule **once per day** "
            "(on the bot's first tick after server-time midnight). If you need a "
            "birthday reflected on the schedule sooner, open `/train` and click "
            "**🎂 Run birthday check** to trigger the check on demand.",
            "**Step 6 of 9 — Birthday Placement**\n"
            "If the member's birthday is already taken on the train schedule, what should the bot do?",
            "**Step 8 of 9",
        )
        s.assert_never("Skipping Steps 6–7")
        placement = s.view_named("PlacementView")
        assert [c.label for c in placement.children] == [
            "🎂 Birthday only",
            "↔️ Assign nearby if taken",
        ]
        assert rec.prompt(3) == (
            "**Step 7 of 9 — Train Schedule Lookahead**\n"
            "Since you enabled train integration, how many days ahead of a "
            "member's birthday should the bot pre-populate them on the train "
            "schedule? This only applies to train-integration auto-placement; "
            "the birthday announcement itself always fires on the day.\n"
            "*(we recommend 14)*"
        )
        assert rec.kw(3)["default"] == "14" and rec.kw(3)["current"] == "14"
        assert rec.kw(3)["modal_title"] == "Lookahead Days"
        assert rec.kw(3)["modal_label"] == "Number of days"
        cfg = _cfg()
        assert cfg["train_integration"] == 1 and cfg["flexible_placement"] == 1
        assert cfg["lookahead_days"] == 21
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert fields["Train Integration"] == "Enabled"
        assert fields["Placement"] == "Flexible (±1 day)" and fields["Lookahead"] == "21 days"
        assert [f.name for f in s.final_embed.fields] == [
            "Sheet Tab",
            "Name Column",
            "Birthday Column",
            "Discord ID Column",
            "Train Integration",
            "Placement",
            "Lookahead",
            "Reminders",
        ]

    @pytest.mark.asyncio
    async def test_birthday_only_placement(self, seeded_db):
        s, _, _ = await _drive(
            [("selected", True), ("selected", True), ("selected", 0), ("selected", False)],
            keep=["Birthdays", "A", "B", " 7 "],
        )
        assert _cfg()["flexible_placement"] == 0 and _cfg()["lookahead_days"] == 7
        assert {f.name: f.value for f in s.final_embed.fields}["Placement"] == "Birthday only"

    @pytest.mark.parametrize("bad", ["0", "-3", "two", "1.5", ""])
    @pytest.mark.asyncio
    async def test_bad_lookahead_ends_the_wizard(self, seeded_db, bad):
        s, _, _ = await _drive(self.ON[:3], keep=["Birthdays", "A", "B", bad])
        assert s.sent[-1] == INPUT_INVALID.format(type="number", example="14", recovery=RECOVERY)
        assert not _has()

    @pytest.mark.asyncio
    async def test_placement_timeout_and_cancel(self, seeded_db):
        s, _, _ = await _drive(self.ON[:2] + ["timeout"])
        assert s.sent[-1] == TIMEOUT and not _has()
        s, _, _ = await _drive(self.ON[:2] + ["cancel"])
        s.assert_never(TIMEOUT)
        assert not _has()


class TestReminders:
    ON = [("selected", True), ("selected", False), ("selected", True)]

    @pytest.mark.asyncio
    async def test_on_asks_channel_time_and_dm(self, seeded_db):
        from train_cog import DEFAULT_BIRTHDAY_DM

        s, rec, env = await _drive(
            self.ON,
            keep=["Birthdays", "A", "B", "9:30am", "Happy day {name}"],
            channel_steps=[_channel_step(600001, stale=True)],
        )
        s.assert_order(
            "**Step 8 of 9",
            PREV_CHANNEL_GONE.format(channel_label="birthday"),
            "**Step 8a of 9 — Birthday Announcement Channel**\n"
            "Which channel should birthday announcements be posted in?",
        )
        s.assert_never("Skipping Steps 8a–8b")
        assert len(env.channel_calls) == 1
        assert env.channel_calls[0]["suggested_name"] == "birthdays"
        assert env.channel_calls[0]["include_threads"] is False
        assert env.channel_calls[0]["current_id"] == 0
        assert rec.prompt(3) == (
            "**Step 8b of 9 — Reminder Time**\n"
            "What time should birthday announcements be posted? "
            "*(in (UTC-5) Eastern (New York, Toronto, Miami))*\n"
            "*(e.g. `8:00am`, `12:00pm`)*"
        )
        assert rec.kw(3)["default"] == "8:00am" and rec.kw(3)["current"] == "8:00am"
        assert rec.kw(3)["modal_title"] == "Reminder Time" and rec.kw(3)["modal_label"] == "Time"
        assert rec.prompt(4) == (
            "**Step 9 of 9 — Birthday DM Body (💎 Premium)**\n"
            "When a birthday fires, the bot also DMs the member directly with a personal "
            "note. Free guilds can configure this now — it just won't fire until you have "
            "Premium + Member Sync + a Discord ID column in your birthday sheet.\n\n"
            "Use `{name}` as a placeholder for the member's name."
        )
        assert rec.kw(4)["default"] == DEFAULT_BIRTHDAY_DM and rec.kw(4)["current"] == ""
        assert rec.kw(4)["modal_title"] == "Birthday DM Body"
        assert rec.kw(4)["modal_label"] == "DM body (max 1000 chars)"
        cfg = _cfg()
        assert cfg["reminders_enabled"] == 1 and cfg["reminder_channel_id"] == 600001
        assert cfg["reminder_time"] == "09:30" and cfg["dm_message"] == "Happy day {name}"
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert fields["Reminders"] == "Enabled"
        assert fields["Reminder Channel"] == "<#600001>"
        assert fields["Reminder Time"] == _t("09:30")

    @pytest.mark.asyncio
    async def test_default_dm_stores_blank_and_24h_time_is_kept(self, seeded_db):
        from train_cog import DEFAULT_BIRTHDAY_DM

        _, _, env = await _drive(
            self.ON, keep=["Birthdays", "A", "B", "21:15", DEFAULT_BIRTHDAY_DM], is_premium=True
        )
        assert _cfg()["reminder_time"] == "21:15" and _cfg()["dm_message"] == ""
        assert env.channel_calls[0]["include_threads"] is True

    @pytest.mark.asyncio
    async def test_bad_time_is_retried_then_given_up(self, seeded_db):
        s, rec, _ = await _drive(
            self.ON, keep=["Birthdays", "A", "B", "noon-ish", "later", "whenever"]
        )
        assert s.sent.count(TIME_PARSE_RETRY.format(raw="noon-ish")) == 1
        assert s.sent.count(TIME_PARSE_RETRY.format(raw="later")) == 1
        assert s.sent[-1] == TIME_PARSE_GIVE_UP.format(recovery=RECOVERY)
        assert len(rec.calls) == 6 and not _has()

    @pytest.mark.asyncio
    async def test_a_bad_time_then_a_good_one(self, seeded_db):
        s, rec, _ = await _drive(self.ON, keep=["Birthdays", "A", "B", "eightish", "8:00pm", ""])
        assert s.sent.count(TIME_PARSE_RETRY.format(raw="eightish")) == 1
        assert _cfg()["reminder_time"] == "20:00" and len(rec.calls) == 6

    @pytest.mark.asyncio
    async def test_unconfirmed_channel_is_a_timeout(self, seeded_db):
        s, _, _ = await _drive(self.ON, channel_steps=[_channel_step(confirmed=False)])
        assert s.sent[-1] == TIMEOUT and not _has()

    @pytest.mark.asyncio
    async def test_abandoned_time_and_dm_questions_end_silently(self, seeded_db):
        s, rec, _ = await _drive(self.ON, keep=["Birthdays", "A", "B", None])
        s.assert_never(TIMEOUT)
        assert len(rec.calls) == 4 and not _has()
        s, rec, _ = await _drive(self.ON, keep=["Birthdays", "A", "B", "8:00am", None])
        assert len(rec.calls) == 5 and not _has()


# ── Disable ──────────────────────────────────────────────────────────────────


class TestDisable:
    @pytest.mark.asyncio
    async def test_first_run_disable_saves_a_disabled_row(self, seeded_db):
        s, rec, env = await _drive([("selected", False)])
        assert rec.calls == []
        assert s.sent[-1] == "**Step 1 of 9 — Enable birthday tracking?**\n" + (
            "Should the bot track member birthdays from your Google Sheet?"
        )
        assert len(env.disable_calls) == 1
        call = env.disable_calls[0]
        assert call["feature_label"] == "Birthday tracking"
        assert call["setup_command"] == "setup → 🎂 Birthdays"
        assert call["had_prior_config"] is False and call["cancel_event"] is s.cancel_event
        cfg = _cfg()
        assert _has() and cfg["enabled"] == 0
        # The default dict's values were written as the row.
        assert cfg["tab_name"] == "Birthdays" and cfg["flexible_placement"] == 1
        call["clear_fn"]()
        assert not _has()

    @pytest.mark.asyncio
    async def test_disable_keeps_the_saved_settings(self, seeded_db):
        _save(train_integration=1, lookahead_days=21, reminders_enabled=1, reminder_channel_id=5)
        s, _, env = await _drive([("proceed", True), ("selected", False)])
        assert env.disable_calls[0]["had_prior_config"] is True
        cfg = _cfg()
        assert cfg["enabled"] == 0 and cfg["tab_name"] == "Members" and cfg["name_col"] == 2
        assert cfg["lookahead_days"] == 21 and cfg["reminder_channel_id"] == 5

    @pytest.mark.asyncio
    async def test_a_disabled_row_gets_no_summary(self, seeded_db):
        _save(enabled=0)
        s, rec, _ = await _drive(MINIMAL, keep=["Members", "C", "D"])
        assert all(e is None for e in s.embeds[:-1])
        assert s.sent[0].startswith("⚙️ **Birthday Tracking Setup**")
        assert rec.kw(0)["current"] == "Members"
        assert rec.kw(1)["current"] == "C" and rec.kw(2)["current"] == "D"
        assert _cfg()["enabled"] == 1 and _cfg()["name_col"] == 2


# ── Re-entry ─────────────────────────────────────────────────────────────────


class TestReentry:
    @pytest.mark.asyncio
    async def test_summary_with_everything_on_and_no_changes(self, seeded_db):
        _save(
            train_integration=1,
            flexible_placement=1,
            lookahead_days=21,
            reminders_enabled=1,
            reminder_channel_id=7,
            reminder_time="20:30",
        )
        s, rec, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert embed.title == "🎂 Current Birthday Setup"
        assert embed.description == (
            "Birthday tracking is already configured. Would you like to edit these settings?"
        )
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Sheet Tab", "Members"),
            ("Name Column", "C"),
            ("Birthday Column", "D"),
            ("Train Integration", "✅ Enabled"),
            ("Placement", "Flexible (±1 day)"),
            ("Lookahead", "21 days"),
            ("Reminders", "✅ Enabled"),
            ("Reminder Channel", "<#7>"),
            ("Reminder Time", _t("20:30")),
        ]
        assert s.sent[-1] == "✅ No changes made. Birthday tracking is still active."
        assert rec.calls == [] and _cfg()["enabled"] == 1

    @pytest.mark.asyncio
    async def test_summary_with_everything_off(self, seeded_db):
        _save(tab_name="")
        s, _, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Sheet Tab", "*not set*"),
            ("Name Column", "C"),
            ("Birthday Column", "D"),
            ("Train Integration", "❌ Disabled"),
            ("Reminders", "❌ Disabled"),
        ]

    @pytest.mark.asyncio
    async def test_summary_reminder_fields_unset(self, seeded_db):
        _save(reminders_enabled=1, reminder_channel_id=0, reminder_time="")
        s, _, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert [(f.name, f.value) for f in embed.fields][-2:] == [
            ("Reminder Channel", "*not set*"),
            ("Reminder Time", "*not set*"),
        ]

    @pytest.mark.asyncio
    async def test_saved_values_are_the_currents(self, seeded_db):
        _save(
            train_integration=1,
            lookahead_days=21,
            reminders_enabled=1,
            reminder_channel_id=550600,
            reminder_time="20:30",
            dm_message="Yo {name}",
            discord_id_col=4,
        )
        s, rec, env = await _drive(
            [
                ("proceed", True),
                ("selected", True),
                ("selected", True),
                ("selected", 0),
                ("selected", True),
            ],
            keep=["Members", "C", "D", "21", "8:30pm", "Yo {name}"],
        )
        assert rec.kw(0)["current"] == "Members"
        assert rec.kw(1)["current"] == "C" and rec.kw(2)["current"] == "D"
        assert rec.kw(3)["current"] == "21"
        assert rec.kw(4)["current"] == "8:30pm"
        assert rec.kw(5)["current"] == "Yo {name}"
        assert env.channel_calls[0]["current_id"] == 550600
        cfg = _cfg()
        assert cfg["reminder_time"] == "20:30" and cfg["dm_message"] == "Yo {name}"
        assert cfg["lookahead_days"] == 21 and cfg["discord_id_col"] == 4
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert fields["Discord ID Column"] == "Column E"

    @pytest.mark.asyncio
    async def test_summary_timeout_and_cancel_leave_the_row(self, seeded_db):
        _save()
        s, _, _ = await _drive(["timeout"])
        s.assert_never(TIMEOUT)
        assert s.sent == [""]  # the summary embed, nothing after it
        s, _, _ = await _drive(["cancel"])
        assert s.sent == [""] and _cfg()["enabled"] == 1


# ── Exits ────────────────────────────────────────────────────────────────────


class TestExits:
    @pytest.mark.parametrize("stop_at", [0, 1, 2])
    @pytest.mark.asyncio
    async def test_timeout_posts_the_route_back(self, seeded_db, stop_at):
        s, _, _ = await _drive(MINIMAL[:stop_at] + ["timeout"])
        assert s.sent[-1] == TIMEOUT and not _has()

    @pytest.mark.parametrize("stop_at", [0, 1, 2])
    @pytest.mark.asyncio
    async def test_cancel_is_silent(self, seeded_db, stop_at):
        s, _, _ = await _drive(MINIMAL[:stop_at] + ["cancel"])
        s.assert_never(TIMEOUT)
        assert not _has()

    @pytest.mark.parametrize("stop_at", [0, 1, 2])
    @pytest.mark.asyncio
    async def test_abandoned_tab_and_column_questions_end_silently(self, seeded_db, stop_at):
        keep = ["Birthdays", "A", "B"]
        keep[stop_at] = None
        s, rec, _ = await _drive([("selected", True)], keep=keep)
        s.assert_never(TIMEOUT)
        assert len(rec.calls) == stop_at + 1 and not _has()
