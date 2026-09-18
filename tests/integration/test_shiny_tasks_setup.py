"""
Characterization tests for the Shiny Tasks wizard
(`setup_cog.run_shiny_tasks_setup`), written against the function before
its move out of `setup_cog` (#611 round 2) and kept unchanged after it.

The four existing shiny tests answer every view with one flat override
dict. These drive the views one at a time, in order, and hold the prompts,
the button labels, the saved row, the final-review embed and the
acknowledgement. `ask_keep_or_change`, `ChannelSelectStep` and
`ask_disable_with_clear` are patched in whichever homes hold them so the
same file holds before and after the move. The server-range step's
`ModalLaunchView` is real; a callable answer fills its modal before
confirming, the way a submitted modal would.
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
    PREV_CHANNEL_GONE,
    CANCEL_PLAIN,
)
from defaults import DEFAULT_SHINY_TASKS_MESSAGE

TIMEOUT = WIZARD_TIMEOUT.format(wizard="🌟 Shiny Tasks")
RECOVERY = "`/setup` → 🌟 Shiny Tasks"
RANGE_GIVE_UP = (
    "⚠️ Could not read those server numbers after a few tries. "
    "Run `/setup` → 🌟 Shiny Tasks to start over."
)
CONFIRM_CANCEL = f"{CANCEL_PLAIN} Run `/setup` → 🌟 Shiny Tasks to start again."
TZ_LABEL = "(UTC-5) Eastern (New York, Toronto, Miami)"
G = TEST_GUILD_ID


def _t(hhmm):
    """A time the way the wizard renders it (the zone abbreviation follows DST)."""
    from wizard_time import _format_time_with_tz

    return _format_time_with_tz(hhmm, "America/New_York")


def _wizard():
    import setup_cog

    return setup_cog.run_shiny_tasks_setup


def _both(name):
    targets = []
    for home in ("setup_cog", "wizard_steps", "shiny_tasks_setup"):
        try:
            module = importlib.import_module(home)
        except ImportError:
            continue
        if hasattr(module, name):
            targets.append(f"{home}.{name}")
    return targets


def _range(lo, hi):
    """Answer the server-range launcher the way a submitted modal would."""

    def _fill(view):
        view.modal.min_value = lo
        view.modal.max_value = hi
        view.confirmed = True

    return _fill


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
        if callable(answer):
            answer(view)
        elif answer != "timeout":
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


def _channel_step(channel_id=900001, *, stale=False, confirmed=True):
    return MagicMock(
        confirmed=confirmed,
        cancelled=False,
        selected_channel=MagicMock(id=channel_id),
        is_current_stale=stale,
        wait=AsyncMock(),
    )


class _Env:
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

    return config.get_shiny_tasks_config(G)


def _has():
    import config

    return config.has_shiny_tasks_config(G)


def _save(**over):
    import config

    kw = dict(
        enabled=1,
        channel_id=900100,
        post_time="09:00",
        server_min=677,
        server_max=804,
        message_template="",
    )
    kw.update(over)
    config.save_shiny_tasks_config(G, **kw)


MINIMAL = [("selected", True), _range("677", "804"), ("confirmed", True)]


# ── Fresh ────────────────────────────────────────────────────────────────────


class TestFresh:
    @pytest.mark.asyncio
    async def test_minimal_saves_and_acknowledges(self, seeded_db):
        s, rec, env = await _drive(MINIMAL)
        s.assert_order(
            "🌟 **Daily Shiny Tasks Setup**\n"
            "Each day, the bot can post the list of Last War servers where "
            "shiny tasks are available, filtered to the servers your alliance "
            "can reach.",
            "**Step 1 of 6 — Enable daily shiny tasks announcement?**",
            "**Step 2 of 6 — Announcement Channel**\n"
            "Pick the channel where the daily shiny tasks post should be posted.",
            "​",
            "**Step 3 of 6 — Server Range**\n"
            "Enter the lowest and highest server numbers your alliance can "
            "reach. Typically your transfer range.",
            "✅ Shiny-tasks announcement saved! The first post will fire at " + _t("09:00") + ".",
        )
        assert [type(v).__name__ for v in s.views] == [
            "YesNoView",
            "MagicMock",
            "ModalLaunchView",
            "ConfirmView",
        ]
        assert env.channel_calls[0]["suggested_name"] == "shiny-tasks"
        assert env.channel_calls[0]["include_threads"] is False
        assert env.channel_calls[0]["current_id"] == 0
        launcher = s.view_named("ModalLaunchView")
        assert [c.label for c in launcher.children] == ["✏️ Enter Server Numbers"]
        assert launcher.modal.title == "Server Range"
        assert [
            (c.label, c.placeholder, c.default, c.max_length) for c in launcher.modal.children
        ] == [
            ("Lowest reachable server number", "e.g. 677", "", 5),
            ("Highest reachable server number", "e.g. 804", "", 5),
        ]
        assert launcher.modal.value == "677 – 804"
        assert rec.prompt(0) == (
            f"**Step 4 of 6 — Post Time**\n"
            f"What time of day should the announcement post? "
            f"*(in your timezone: {TZ_LABEL})*\n"
            f"*(e.g. `9:00am`, `10:30am`, `9:00pm`)*"
        )
        assert rec.kw(0) == dict(
            default="9:00am",
            current="9:00am",
            modal_title="Post Time",
            modal_label="Time",
            timeout_cmd="setup_shiny_tasks",
            cancel_event=s.cancel_event,
        )
        assert rec.prompt(1) == (
            "**Step 5 of 6 — Announcement Message**\n"
            "Customize the announcement body, or use the default. "
            "Placeholders: `{servers}` and `{date}`."
        )
        assert rec.kw(1)["default"] == DEFAULT_SHINY_TASKS_MESSAGE and rec.kw(1)["current"] is None
        assert rec.kw(1)["modal_title"] == "Shiny Tasks Message"
        assert rec.kw(1)["modal_label"] == "Message body"
        embed = s.final_embed
        assert embed.title == "🌟 Shiny Tasks — Final Review"
        assert embed.description == "Confirm to save this configuration."
        assert [(f.name, f.value, f.inline) for f in embed.fields] == [
            ("Status", "✅ Enabled", True),
            ("Channel", "<#900001>", True),
            ("Server Range", "677 – 804", True),
            ("Post Time", _t("09:00"), True),
            ("Message", DEFAULT_SHINY_TASKS_MESSAGE, False),
        ]
        assert [c.label for c in s.view_named("ConfirmView").children] == [
            "✅ Confirm",
            "❌ Cancel",
        ]
        cfg = _cfg()
        assert cfg["enabled"] == 1 and cfg["channel_id"] == 900001
        assert cfg["server_min"] == 677 and cfg["server_max"] == 804
        assert cfg["post_time"] == "09:00" and cfg["message_template"] == ""

    @pytest.mark.asyncio
    async def test_custom_message_and_time(self, seeded_db):
        s, _, _ = await _drive(
            MINIMAL, keep=["9:30pm", "  Go go {servers} on {date}  "], is_premium=True
        )
        cfg = _cfg()
        assert (
            cfg["post_time"] == "21:30" and cfg["message_template"] == "Go go {servers} on {date}"
        )
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert fields["Message"] == "Go go {servers} on {date}" and fields["Post Time"] == _t(
            "21:30"
        )
        assert s.sent[-1].endswith(_t("21:30") + ".")

    @pytest.mark.asyncio
    async def test_the_default_message_with_whitespace_stores_blank(self, seeded_db):
        await _drive(MINIMAL, keep=["9:00am", "  " + DEFAULT_SHINY_TASKS_MESSAGE + "\n"])
        assert _cfg()["message_template"] == ""

    @pytest.mark.asyncio
    async def test_stale_channel_and_premium_threads(self, seeded_db):
        s, _, env = await _drive(
            MINIMAL, is_premium=True, channel_steps=[_channel_step(1, stale=True)]
        )
        s.assert_order("**Step 2 of 6", PREV_CHANNEL_GONE.format(channel_label="shiny tasks"), "​")
        assert env.channel_calls[0]["include_threads"] is True

    @pytest.mark.asyncio
    async def test_a_24h_time_is_kept(self, seeded_db):
        await _drive(MINIMAL, keep=["21:15"])
        assert _cfg()["post_time"] == "21:15"

    @pytest.mark.asyncio
    async def test_bad_time_is_retried_then_given_up(self, seeded_db):
        s, rec, _ = await _drive(MINIMAL[:2], keep=["noon-ish", "later", "whenever"])
        assert s.sent.count(TIME_PARSE_RETRY.format(raw="noon-ish")) == 1
        assert s.sent.count(TIME_PARSE_RETRY.format(raw="later")) == 1
        assert s.sent[-1] == TIME_PARSE_GIVE_UP.format(recovery=RECOVERY)
        assert len(rec.calls) == 3 and not _has()


class TestServerRange:
    @pytest.mark.asyncio
    async def test_unreadable_numbers_are_retried_with_the_typed_values(self, seeded_db):
        s, _, _ = await _drive(
            [("selected", True), _range("abc", "804"), _range("677", "804"), ("confirmed", True)]
        )
        assert s.sent[s.index_of("Could not read")] == (
            "⚠️ Could not read **`abc`** / **`804`** as whole "
            "numbers. Try something like `677` and `804`. Let's try once more."
        )
        first, second = [v for v in s.views if type(v).__name__ == "ModalLaunchView"]
        assert [c.default for c in second.modal.children] == ["abc", "804"]
        assert [c.label for c in second.children] == ["✏️ Enter Server Numbers"]
        assert _cfg()["server_min"] == 677

    @pytest.mark.asyncio
    async def test_a_backwards_range_is_retried(self, seeded_db):
        s, _, _ = await _drive(
            [
                ("selected", True),
                _range("900", "800"),
                _range("0", "5"),
                _range("1", "5"),
                ("confirmed", True),
            ]
        )
        assert s.sent[s.index_of("must be ≥ 1")] == (
            "⚠️ The lowest server (**`900`**) must be ≥ 1 and "
            "≤ the highest (**`800`**). Let's try once more."
        )
        assert "(**`0`**) must be ≥ 1" in s.sent[s.index_of("(**`0`**)")]
        assert _cfg()["server_min"] == 1 and _cfg()["server_max"] == 5

    @pytest.mark.asyncio
    async def test_three_bad_ranges_give_up(self, seeded_db):
        s, _, _ = await _drive(
            [("selected", True), _range("x", "y"), _range("x", "y"), _range("x", "y")]
        )
        assert s.sent[-1] == RANGE_GIVE_UP and not _has()

    @pytest.mark.asyncio
    async def test_a_saved_range_gets_keep_current(self, seeded_db):
        _save(server_min=677, server_max=804)
        s, _, _ = await _drive(
            [("proceed", True), ("selected", True), _range("677", "804"), ("confirmed", True)]
        )
        launcher = s.view_named("ModalLaunchView")
        # discord.py adds the decorated Enter button before the added Keep one.
        assert [c.label for c in launcher.children] == [
            "✏️ Enter Server Numbers",
            "Keep current: 677 – 804",
        ]
        assert [c.default for c in launcher.modal.children] == ["677", "804"]

    @pytest.mark.asyncio
    async def test_a_launcher_confirmed_with_nothing_typed_re_asks(self, seeded_db):
        s, _, _ = await _drive(
            [("selected", True), ("confirmed", True), _range("1", "2"), ("confirmed", True)]
        )
        assert "Could not read **``** / **``**" in s.sent[s.index_of("Could not read")]
        assert _cfg()["server_min"] == 1

    @pytest.mark.asyncio
    async def test_keep_current_fills_the_modal(self, seeded_db):
        _save(server_min=677, server_max=804)

        def _keep(view):
            view.children[0]  # the Keep current button is first
            view.modal.min_value, view.modal.max_value = "677", "804"
            view.confirmed = True

        await _drive([("proceed", True), ("selected", True), _keep, ("confirmed", True)])
        assert _cfg()["server_min"] == 677 and _cfg()["server_max"] == 804

    @pytest.mark.asyncio
    async def test_an_unusable_saved_range_gets_no_keep_button(self, seeded_db):
        _save(server_min=900, server_max=800)
        s, _, _ = await _drive(
            [("proceed", True), ("selected", True), _range("1", "2"), ("confirmed", True)]
        )
        launcher = s.view_named("ModalLaunchView")
        assert [c.label for c in launcher.children] == ["✏️ Enter Server Numbers"]
        assert [c.default for c in launcher.modal.children] == ["900", "800"]

    @pytest.mark.asyncio
    async def test_range_launcher_timeout_and_cancel(self, seeded_db):
        s, _, _ = await _drive([("selected", True), "timeout"])
        assert s.sent[-1] == TIMEOUT and not _has()
        s, _, _ = await _drive([("selected", True), "cancel"])
        s.assert_never(TIMEOUT)


# ── Disable ──────────────────────────────────────────────────────────────────


class TestDisable:
    @pytest.mark.asyncio
    async def test_first_run_disable(self, seeded_db):
        s, rec, env = await _drive([("selected", False)])
        assert (
            rec.calls == []
            and s.sent[-1] == "**Step 1 of 6 — Enable daily shiny tasks announcement?**"
        )
        call = env.disable_calls[0]
        assert call["feature_label"] == "Shiny tasks announcement"
        assert call["setup_command"] == "setup → 🌟 Shiny Tasks"
        assert call["had_prior_config"] is False and call["cancel_event"] is s.cancel_event
        cfg = _cfg()
        assert _has() and cfg["enabled"] == 0 and cfg["post_time"] == "09:00"
        call["clear_fn"]()
        assert not _has()

    @pytest.mark.asyncio
    async def test_disable_keeps_the_saved_settings(self, seeded_db):
        _save(message_template="Custom")
        s, _, env = await _drive([("proceed", True), ("selected", False)])
        assert env.disable_calls[0]["had_prior_config"] is True
        cfg = _cfg()
        assert cfg["enabled"] == 0 and cfg["channel_id"] == 900100
        assert cfg["server_min"] == 677 and cfg["message_template"] == "Custom"

    @pytest.mark.asyncio
    async def test_a_disabled_row_gets_no_summary_but_keeps_currents(self, seeded_db):
        _save(enabled=0, message_template="Custom", post_time="21:00")
        s, rec, env = await _drive(
            [("selected", True), _range("677", "804"), ("confirmed", True)],
            keep=["9:00pm", "Custom"],
        )
        embeds = [e for e in s.embeds if e is not None]
        assert [e.title for e in embeds] == ["🌟 Shiny Tasks — Final Review"]
        assert env.channel_calls[0]["current_id"] == 900100
        assert rec.kw(0)["current"] == "9:00pm" and rec.kw(1)["current"] == "Custom"
        labels = [c.label for c in s.view_named("ModalLaunchView").children]
        assert "Keep current: 677 – 804" in labels


# ── Re-entry ─────────────────────────────────────────────────────────────────


class TestReentry:
    @pytest.mark.asyncio
    async def test_summary_and_no_changes(self, seeded_db):
        _save(message_template="Custom", post_time="20:30")
        s, rec, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert embed.title == "🌟 Current Shiny Tasks Setup"
        assert embed.description == (
            "The daily shiny-tasks announcement is already configured. "
            "Would you like to edit these settings?"
        )
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Channel", "<#900100>"),
            ("Server Range", "677 – 804"),
            ("Post Time", _t("20:30")),
            ("Message", "Custom"),
        ]
        assert s.sent[-1] == "✅ No changes made. The daily announcement is still active."
        assert rec.calls == [] and _cfg()["enabled"] == 1

    @pytest.mark.asyncio
    async def test_summary_with_nothing_set(self, seeded_db):
        _save(channel_id=0, server_min=0, server_max=0, post_time="")
        s, _, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Channel", "*not set*"),
            ("Server Range", "? – ?"),
            ("Post Time", "*not set*"),
            ("Message", "Default"),
        ]

    @pytest.mark.asyncio
    async def test_summary_timeout_and_cancel_leave_the_row(self, seeded_db):
        _save()
        s, _, _ = await _drive(["timeout"])
        assert s.sent == [""]
        s, _, _ = await _drive(["cancel"])
        assert s.sent == [""] and _cfg()["enabled"] == 1


# ── Exits ────────────────────────────────────────────────────────────────────


class TestExits:
    @pytest.mark.asyncio
    async def test_enable_timeout_and_cancel(self, seeded_db):
        s, _, _ = await _drive(["timeout"])
        assert s.sent[-1] == TIMEOUT and not _has()
        s, _, _ = await _drive(["cancel"])
        s.assert_never(TIMEOUT)

    @pytest.mark.asyncio
    async def test_unconfirmed_channel_is_a_timeout(self, seeded_db):
        s, _, _ = await _drive([("selected", True)], channel_steps=[_channel_step(confirmed=False)])
        assert s.sent[-1] == TIMEOUT and not _has()

    @pytest.mark.asyncio
    async def test_abandoned_time_and_message_questions_end_silently(self, seeded_db):
        s, rec, _ = await _drive(MINIMAL[:2], keep=[None])
        s.assert_never(TIMEOUT)
        assert len(rec.calls) == 1 and not _has()
        s, rec, _ = await _drive(MINIMAL[:2], keep=["9:00am", None])
        assert len(rec.calls) == 2 and not _has()

    @pytest.mark.asyncio
    async def test_review_cancel_and_timeout_read_the_same(self, seeded_db):
        """Recorded as it stands: a timed-out review view posts the cancel
        line, since `not confirmed` covers None as well as False."""
        s, _, _ = await _drive(MINIMAL[:2] + [("confirmed", False)])
        assert s.sent[-1] == CONFIRM_CANCEL and not _has()
        s, _, _ = await _drive(MINIMAL[:2] + ["timeout"])
        assert s.sent[-1] == CONFIRM_CANCEL and not _has()
        s, _, _ = await _drive(MINIMAL[:2] + ["cancel"])
        s.assert_never(CONFIRM_CANCEL)
        assert not _has()
