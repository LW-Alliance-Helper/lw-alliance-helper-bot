"""
Characterization tests for the storm wizard's participation step
(`_run_storm_participation_step`), its preset picker
(`_run_participation_preset_picker_step`) and its question builder
(`_build_participation_question`), written against the functions in
`setup_cog` before their move to `storm_setup_participation.py` (#611
round 2) and kept unchanged after it.

Every wizard test so far patched the step away; this is its first direct
coverage. The scripted harness answers each view the step posts, in
order (`(attribute, value)`, a dict of attributes, `"timeout"` or
`"cancel"`); `ask_keep_or_change` is replaced by a recorder so the prompt,
default and current of every keep-or-change question are asserted too;
typed replies come from `bot.wait_for`.
"""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# setup_cog re-exports the wizard pieces by name at import time; import it
# before anything patches `wizard_steps.X` (see CLAUDE.md, test gotchas).
import setup_cog  # noqa: F401

from tests.conftest import TEST_GUILD_ID, make_mock_channel, make_mock_user
from messages import GENERIC_CMD_TIMEOUT

CMD = "setup → ⚔️ Desert Storm"
TIMEOUT = GENERIC_CMD_TIMEOUT.format(cmd=CMD)
USER_ID = 123456789


def _step():
    import setup_cog

    return setup_cog._run_storm_participation_step


def _picker():
    import setup_cog

    return setup_cog._run_participation_preset_picker_step


def _builder():
    import setup_cog

    return setup_cog._build_participation_question


class Script:
    """Answer each posted view in order; record everything sent."""

    def __init__(self, channel, cancel_event, answers):
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

    def labels(self, view_index):
        return [getattr(c, "label", None) for c in self.views[view_index].children]


class KeepRecorder:
    """Stands in for `ask_keep_or_change` in both its homes, answering from
    `values` in order and recording every prompt."""

    def __init__(self, values):
        self.values = list(values)
        self.calls = []

    def __enter__(self):
        rec = self

        # A plain async function: `patch` builds an AsyncMock for an async
        # target and only awaits a side effect that is itself a coroutine
        # function, which an instance with `async __call__` is not.
        async def fake(channel, prompt, **kw):
            rec.calls.append((prompt, kw))
            return rec.values.pop(0) if rec.values else kw.get("default")

        self._p = [
            patch("setup_cog.ask_keep_or_change", side_effect=fake),
            patch("wizard_steps.ask_keep_or_change", side_effect=fake),
        ]
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


def _bot(replies=()):
    """A bot whose `wait_for` hands back each reply in order; an exception
    instance in the list is raised instead."""
    bot = AsyncMock()
    queue = [r if isinstance(r, BaseException) else MagicMock(content=r) for r in replies]
    bot.wait_for = AsyncMock(side_effect=queue)
    return bot


async def _drive_step(
    answers, *, keep=(), replies=(), event_type="DS", is_premium=False, harness_bot=None
):
    channel = make_mock_channel()
    cancel_event = asyncio.Event()
    script = Script(channel, cancel_event, answers)
    bot = harness_bot or _bot(replies)
    with KeepRecorder(keep) as rec:
        result = await _step()(
            channel,
            bot,
            make_mock_user(USER_ID),
            cancel_event,
            guild_id=TEST_GUILD_ID,
            event_type=event_type,
            label="Desert Storm" if event_type == "DS" else "Canyon Storm",
            cmd_name=CMD,
            is_premium_flag=is_premium,
            current={},
        )
    return result, script, rec


async def _drive_picker(answers, *, is_premium=False, existing=(), cap="free"):
    channel = make_mock_channel()
    cancel_event = asyncio.Event()
    script = Script(channel, cancel_event, answers)
    result = await _picker()(
        channel,
        AsyncMock(),
        make_mock_user(USER_ID),
        cancel_event,
        cmd_name=CMD,
        is_premium_flag=is_premium,
        existing_questions=list(existing),
        cap=(None if is_premium else 3) if cap == "free" else cap,
    )
    return result, script


async def _drive_builder(
    answers, *, replies=(), is_premium=False, existing=None, all_questions=None
):
    channel = make_mock_channel()
    cancel_event = asyncio.Event()
    script = Script(channel, cancel_event, answers)
    result = await _builder()(
        channel,
        _bot(replies),
        make_mock_user(USER_ID),
        cancel_event,
        cmd_name=CMD,
        is_premium_flag=is_premium,
        existing=existing,
        all_questions=all_questions,
    )
    return result, script


DISABLED = {
    "enabled": 0,
    "tab_name": "",
    "questions": [],
    "roster_tab": "",
    "roster_name_col": 0,
    "roster_alias_col": -1,
    "roster_start_row": 2,
}

ENABLE_PROMPT = (
    "**Step 7 of 9: Participation Tracking**\n"
    "Do you want to track Desert Storm participation? Leadership clicks "
    "**📊 Fill out participation questions** on `/desertstorm` "
    "after each event to log who showed up, who sat out, etc.\n"
    "You'll define the questions yourself, so the tracker matches how "
    "your alliance runs the event."
)
ALIAS_PROMPT = (
    "**Step 7.4: Roster Source: Alias Column?**\n"
    "If you have other names or nicknames that you call your members in these "
    "mails, this helps resolve to their full name in your sheet automatically. "
    "Do you have an alias column?"
)


def _saved(**over):
    import config

    # The participation columns live on the storm row; give it one to update.
    config.save_storm_config(
        TEST_GUILD_ID,
        "DS",
        tab_name="DS Assignments",
        mail_template="t",
        timezone="America/New_York",
        log_channel_id=1,
        post_channel_id=2,
    )
    base = dict(
        enabled=1,
        tab_name="DS Log",
        questions=[{"key": "vote_count", "label": "Vote Count", "type": "numeric"}],
        roster_tab="Roster",
        roster_name_col=1,
        roster_alias_col=2,
        roster_start_row=3,
    )
    base.update(over)
    config.save_participation_config(TEST_GUILD_ID, "DS", **base)
    return base


# ── The step ─────────────────────────────────────────────────────────────────


class TestStepFresh:
    @pytest.mark.asyncio
    async def test_no_returns_the_disabled_shape(self, seeded_db):
        result, s, rec = await _drive_step([("selected", False)])
        assert result == DISABLED
        assert s.sent == [ENABLE_PROMPT]
        assert type(s.views[0]).__name__ == "YesNoView"
        assert rec.calls == []

    @pytest.mark.asyncio
    async def test_yes_walks_the_roster_source_and_returns_the_enabled_shape(self, seeded_db):
        result, s, rec = await _drive_step(
            [
                ("selected", True),  # enable
                ("selected", False),  # alias column?
                ("action", "skip"),  # presets
                ("action", "done"),  # builder
            ],
            keep=["DS Participation Log", "Squad Powers", "A", "2"],
        )
        assert result == {
            "enabled": 1,
            "tab_name": "DS Participation Log",
            "questions": [],
            "roster_tab": "Squad Powers",
            "roster_name_col": 0,
            "roster_alias_col": -1,
            "roster_start_row": 2,
        }
        s.assert_order(
            ENABLE_PROMPT,
            ALIAS_PROMPT,
            "**Step 7.6: Use any preset questions?**",
            "**Step 7.7: Participation Questions**",
        )
        assert rec.prompt(0).startswith("**Step 7.1: Participation Sheet Tab**\n")
        assert "Which tab should the bot write Desert Storm participation rows to?" in rec.prompt(0)
        assert rec.kw(0)["default"] == "DS Participation Log" and rec.kw(0)["current"] == ""
        assert rec.kw(0)["modal_title"] == "Participation Tab"
        assert rec.prompt(1).startswith("**Step 7.2: Roster Source: Sheet Tab**\n")
        assert "`/setup` → 📋 Survey or `/setup` → 🎂 Birthdays" in rec.prompt(1)
        # The seeded guild has a survey config, so its squad-powers tab is suggested.
        assert rec.kw(1)["default"] == "Squad Powers" and rec.kw(1)["current"] == "Squad Powers"
        assert rec.prompt(2) == (
            "**Step 7.3: Roster Source: Name Column**\n"
            "Which column letter has the member name? (e.g. `A`, `B`, `E`)"
        )
        # A pristine row stores name column 0, which reads back as "A".
        assert rec.kw(2)["default"] == "A" and rec.kw(2)["current"] == "A"
        assert rec.prompt(3).startswith("**Step 7.5: Roster Source: First Data Row**\n")
        assert rec.kw(3)["default"] == "2" and rec.kw(3)["current"] == ""
        assert rec.kw(3)["modal_label"] == "Row number"

    @pytest.mark.asyncio
    async def test_canyon_storm_names_its_own_defaults(self, seeded_db):
        result, s, rec = await _drive_step(
            [("selected", True), ("selected", False), ("action", "skip"), ("action", "done")],
            event_type="CS",
        )
        assert "Do you want to track Canyon Storm participation?" in s.sent[0]
        assert "on `/canyonstorm` after each event" in s.sent[0]
        assert rec.kw(0)["default"] == "CS Participation Log"
        assert result["tab_name"] == "CS Participation Log"

    @pytest.mark.asyncio
    async def test_roster_tab_suggests_the_survey_tab_then_the_birthday_tab(self, seeded_db):
        import config

        with patch("config.get_survey_config", return_value={"tab_squad_powers": "Powers"}):
            _, _, rec = await _drive_step(
                [("selected", True), ("selected", False), ("action", "skip"), ("action", "done")]
            )
        assert rec.kw(1)["current"] == "Powers"
        with (
            patch("config.get_survey_config", return_value={}),
            patch("config.get_birthday_config", return_value={"tab_name": "Bdays"}),
        ):
            _, _, rec = await _drive_step(
                [("selected", True), ("selected", False), ("action", "skip"), ("action", "done")]
            )
        assert rec.kw(1)["current"] == "Bdays"
        assert config.get_participation_config(TEST_GUILD_ID, "DS")["roster_tab"] == ""

    @pytest.mark.asyncio
    async def test_alias_yes_asks_the_column_after_the_name_column(self, seeded_db):
        result, s, rec = await _drive_step(
            [("selected", True), ("selected", True), ("action", "skip"), ("action", "done")],
            keep=["DS Participation Log", "Squad Powers", "C", "D", "5"],
        )
        assert rec.prompt(3) == "**Alias Column**\nWhich column letter has the alias / nickname?"
        assert rec.kw(3)["default"] == "D"  # the column after C
        assert rec.kw(3)["modal_title"] == "Alias column"
        assert result["roster_name_col"] == 2
        assert result["roster_alias_col"] == 3
        assert result["roster_start_row"] == 5

    @pytest.mark.asyncio
    async def test_alias_default_follows_member_sync_display_column(self, seeded_db):
        with patch(
            "config.get_member_roster_config", return_value={"enabled": True, "display_col": 5}
        ):
            _, _, rec = await _drive_step(
                [("selected", True), ("selected", True), ("action", "skip"), ("action", "done")],
                keep=["DS Participation Log", "Squad Powers", "A", "F", "2"],
            )
        assert rec.kw(3)["default"] == "F"

    @pytest.mark.asyncio
    async def test_bad_name_column_ends_the_step(self, seeded_db):
        result, s, _ = await _drive_step([("selected", True)], keep=["DS Log", "Roster", "7"])
        assert result is None
        assert s.sent[-1] == f"⚠️ `7` isn't a valid column letter. Run `/{CMD}` to start again."

    @pytest.mark.asyncio
    async def test_bad_alias_column_ends_the_step(self, seeded_db):
        result, s, _ = await _drive_step(
            [("selected", True), ("selected", True)], keep=["DS Log", "Roster", "A", "??"]
        )
        assert result is None
        assert s.sent[-1] == f"⚠️ `??` isn't a valid column letter. Run `/{CMD}` to start again."

    @pytest.mark.asyncio
    async def test_bad_start_row_ends_the_step(self, seeded_db):
        result, s, _ = await _drive_step(
            [("selected", True), ("selected", False)], keep=["DS Log", "Roster", "A", "two"]
        )
        assert result is None
        assert s.sent[-1] == f"⚠️ `two` isn't a number. Run `/{CMD}` to start again."

    @pytest.mark.asyncio
    async def test_start_row_floors_at_one(self, seeded_db):
        result, _, _ = await _drive_step(
            [("selected", True), ("selected", False), ("action", "skip"), ("action", "done")],
            keep=["DS Log", "Roster", "A", "0"],
        )
        assert result["roster_start_row"] == 1


class TestStepReentry:
    @pytest.mark.asyncio
    async def test_saved_config_offers_keep_or_flip_gates_and_currents(self, seeded_db):
        _saved()
        result, s, rec = await _drive_step(
            [
                ("value", True),  # keep: enabled
                ("value", True),  # keep: alias yes
                ("action", "skip"),
                ("action", "done"),
            ],
            keep=["DS Log", "Roster", "B", "C", "3"],
        )
        assert type(s.views[0]).__name__ == "_KeepOrFlipYesNoGate"
        assert type(s.views[1]).__name__ == "_KeepOrFlipYesNoGate"
        assert rec.kw(0)["current"] == "DS Log"
        assert rec.kw(1)["current"] == "Roster"
        assert rec.kw(2)["current"] == "B"
        assert rec.kw(3)["current"] == "C"
        assert rec.kw(4)["current"] == "3"
        assert result["questions"] == [
            {"key": "vote_count", "label": "Vote Count", "type": "numeric"}
        ]
        assert result["roster_name_col"] == 1 and result["roster_alias_col"] == 2

    @pytest.mark.asyncio
    async def test_flipping_off_keeps_the_saved_values_around(self, seeded_db):
        saved = _saved()
        result, s, rec = await _drive_step([("value", False)])
        assert result == {
            "enabled": 0,
            "tab_name": "DS Log",
            "questions": saved["questions"],
            "roster_tab": "Roster",
            "roster_name_col": 1,
            "roster_alias_col": 2,
            "roster_start_row": 3,
        }
        assert rec.calls == []

    @pytest.mark.asyncio
    async def test_a_disabled_row_with_config_still_counts_as_reentry(self, seeded_db):
        _saved(enabled=0)
        _, s, _ = await _drive_step([("value", False)])
        assert type(s.views[0]).__name__ == "_KeepOrFlipYesNoGate"

    @pytest.mark.asyncio
    async def test_saved_alias_opt_out_still_gets_the_gate(self, seeded_db):
        _saved(roster_alias_col=-1)
        _, s, _ = await _drive_step(
            [("value", True), ("value", False), ("action", "skip"), ("action", "done")]
        )
        assert type(s.views[1]).__name__ == "_KeepOrFlipYesNoGate"


class TestStepBuilder:
    @pytest.mark.asyncio
    async def test_builder_prompt_free_tier_with_no_questions(self, seeded_db):
        _, s, _ = await _drive_step(
            [("selected", True), ("selected", False), ("action", "skip"), ("action", "done")]
        )
        prompt = s.sent[s.index_of("Step 7.7")]
        assert prompt == (
            "**Step 7.7: Participation Questions**\n"
            "Each question becomes a column on your sheet and a step in "
            "the **📊 Fill out participation questions** flow on "
            "`/desertstorm`.\n"
            "Examples: *Vote count*, *Sitting out*, *Did anyone show up late?*\n"
            "\n*Free tier limit: 3 questions.*\n\n"
            "*(no questions yet; every participation log will only ask for the date)*"
        )
        assert s.labels(3) == ["➕ Add question", "✅ Done"]

    @pytest.mark.asyncio
    async def test_builder_prompt_premium_lists_the_questions(self, seeded_db):
        _saved()
        _, s, _ = await _drive_step(
            [("value", True), ("value", True), ("action", "skip"), ("action", "done")],
            is_premium=True,
        )
        prompt = s.sent[s.index_of("Step 7.7")]
        assert "\n💎 *Premium: unlimited questions and three extra question types.*\n\n" in prompt
        assert prompt.endswith("**1. Vote Count**: _🔢 Numeric: number with optional min/max_")
        view = s.views[3]
        # discord.py adds the decorated buttons first, then the two selects.
        assert [c.placeholder for c in view.children[2:]] == [
            "✏️ Edit a question…",
            "🗑️ Remove a question…",
        ]
        assert [o.label for o in view.children[2].options] == ["Edit: Vote Count"]
        assert [o.label for o in view.children[3].options] == ["Remove: Vote Count"]

    @pytest.mark.asyncio
    async def test_delete_removes_and_reposts(self, seeded_db):
        _saved()
        result, s, _ = await _drive_step(
            [
                ("value", True),
                ("value", True),
                ("action", "skip"),
                {"action": "delete", "del_idx": 0},
                ("action", "done"),
            ]
        )
        assert result["questions"] == []
        assert sum("Step 7.7" in t for t in s.sent) == 2
        assert s.sent[s.index_of("Step 7.7") + 1] == "🗑️ Removed **Vote Count**."

    @pytest.mark.asyncio
    async def test_add_at_the_free_cap_shows_the_limit_embed(self, seeded_db):
        import discord

        _saved(questions=[{"key": f"q{i}", "label": f"Q{i}", "type": "text"} for i in range(3)])
        with patch("premium.limit_reached_embed", return_value=discord.Embed(title="cap")) as embed:
            result, s, _ = await _drive_step(
                [
                    ("value", True),
                    ("value", True),
                    ("action", "skip"),
                    ("action", "add"),
                    ("action", "done"),
                ]
            )
        embed.assert_called_once_with(
            feature_label="Participation Questions", current=3, cap=3, plural_unit="questions"
        )
        assert any(e is not None and e.title == "cap" for e in s.embeds)
        assert len(result["questions"]) == 3

    @pytest.mark.asyncio
    async def test_add_walks_the_question_builder_and_counts(self, seeded_db):
        result, s, _ = await _drive_step(
            [
                ("selected", True),
                ("selected", False),
                ("action", "skip"),
                ("action", "add"),
                ("selected", "numeric"),  # the type picker
                ("action", "done"),
            ],
            replies=["Vote Count", "none"],
        )
        assert result["questions"] == [
            {"key": "vote_count", "label": "Vote Count", "type": "numeric"}
        ]
        s.assert_order("Step 7.7", "**Question: Label**", "✅ Added **Vote Count** (1 so far).")
        assert sum("Step 7.7" in t for t in s.sent) == 2

    @pytest.mark.asyncio
    async def test_edit_replaces_in_place(self, seeded_db):
        _saved()
        result, s, _ = await _drive_step(
            [
                ("value", True),
                ("value", True),
                ("action", "skip"),
                {"action": "edit", "edit_idx": 0},
                ("selected", "text"),
                ("action", "done"),
            ],
            replies=["Votes"],
        )
        assert result["questions"] == [{"key": "votes", "label": "Votes", "type": "text"}]
        assert "✅ Updated **Votes**." in s.sent

    @pytest.mark.asyncio
    async def test_abandoned_question_ends_the_step(self, seeded_db):
        result, s, _ = await _drive_step(
            [("selected", True), ("selected", False), ("action", "skip"), ("action", "add")],
            replies=[asyncio.TimeoutError()],
        )
        assert result is None
        assert s.sent[-1] == TIMEOUT

    @pytest.mark.asyncio
    async def test_presets_land_in_the_list_before_the_builder(self, seeded_db):
        result, s, _ = await _drive_step(
            [
                ("selected", True),
                ("selected", False),
                {"action": "add", "selected": {"showed_up"}},
                ("action", "done"),
            ]
        )
        assert result["questions"] == [
            {"key": "showed_up", "label": "Did this member show up?", "type": "roster_multi_select"}
        ]
        assert "**1. Did this member show up?**" in s.sent[s.index_of("Step 7.7")]


class TestStepExits:
    @pytest.mark.parametrize("stop_at", [0, 1, 2, 3])
    @pytest.mark.asyncio
    async def test_timeout_at_each_view(self, seeded_db, stop_at):
        walk = [("selected", True), ("selected", False), ("action", "skip"), ("action", "done")]
        result, s, _ = await _drive_step(walk[:stop_at] + ["timeout"])
        assert result is None
        assert s.sent[-1] == TIMEOUT

    @pytest.mark.parametrize("stop_at", [0, 1, 2, 3])
    @pytest.mark.asyncio
    async def test_cancel_at_each_view(self, seeded_db, stop_at):
        walk = [("selected", True), ("selected", False), ("action", "skip"), ("action", "done")]
        result, s, _ = await _drive_step(walk[:stop_at] + ["cancel"])
        assert result is None
        s.assert_never(TIMEOUT)

    @pytest.mark.parametrize("at", [0, 1, 2, 3])
    @pytest.mark.asyncio
    async def test_abandoning_a_keep_or_change_question(self, seeded_db, at):
        keep = ["DS Log", "Roster", "A", "2"]
        keep[at] = None
        result, _, _ = await _drive_step([("selected", True), ("selected", False)], keep=keep)
        assert result is None


# ── The preset picker ────────────────────────────────────────────────────────


FREE_LABELS = [
    "✅ Did this member show up?",
    "✏️ Who sat out this week?",
    "🗳️ Who didn't vote this week?",
]
PREMIUM_LABELS = [
    "📊 Sit-out count, past 4 events",
    "📊 Vote-miss count, past 8 events",
    "🗳️ Who didn't vote this week? (auto-prefill from poll)",
]


def _sel(view):
    return next(c for c in view.children if hasattr(c, "options"))


class TestPresetPicker:
    @pytest.mark.asyncio
    async def test_free_tier_shows_premium_presets_marked(self):
        result, s = await _drive_picker([("action", "skip")])
        assert result == []
        sel = _sel(s.views[0])
        assert [o.label for o in sel.options] == FREE_LABELS + [f"💎 {l}" for l in PREMIUM_LABELS]
        assert [o.description.startswith("💎 Premium · ") for o in sel.options] == [False] * 3 + [
            True
        ] * 3
        assert [o.default for o in sel.options] == [True, False, False, False, False, False]
        assert sel.placeholder == "Pick the preset questions you want…"
        assert sel.min_values == 0 and sel.max_values == 6
        assert [c.label for c in s.views[0].children[:2]] == [
            "✅ Add picked presets",
            "↩️ Skip presets",
        ]
        assert s.sent[0].endswith(
            "\n💎 *Presets marked with 💎 are Premium-only. Run "
            "`/upgrade` to unlock 3 additional preset(s).*"
        )
        assert s.sent[0].startswith("**Step 7.6: Use any preset questions?**\n")

    @pytest.mark.asyncio
    async def test_premium_shows_them_plain(self):
        _, s = await _drive_picker([("action", "skip")], is_premium=True)
        sel = _sel(s.views[0])
        assert [o.label for o in sel.options] == FREE_LABELS + PREMIUM_LABELS
        assert s.sent[0].endswith("\n💎 *Includes 3 Premium preset(s).*")

    @pytest.mark.asyncio
    async def test_already_configured_presets_are_left_out(self):
        _, s = await _drive_picker([("action", "skip")], existing=[{"key": "showed_up"}])
        assert [o.value for o in _sel(s.views[0]).options][0] == "sat_out"

    @pytest.mark.asyncio
    async def test_nothing_left_to_offer_returns_silently(self):
        keys = [
            "showed_up",
            "sat_out",
            "didnt_vote",
            "sit_out_count_4",
            "vote_miss_count_8",
            "didnt_vote_autoprefill",
        ]
        result, s = await _drive_picker([], existing=[{"key": k} for k in keys], is_premium=True)
        assert result == [] and s.sent == []

    @pytest.mark.asyncio
    async def test_add_converts_picks_in_catalogue_order(self):
        result, s = await _drive_picker(
            [{"action": "add", "selected": {"didnt_vote", "showed_up"}}], is_premium=True
        )
        assert [q["key"] for q in result] == ["showed_up", "didnt_vote"]
        assert result[0] == {
            "key": "showed_up",
            "label": "Did this member show up?",
            "type": "roster_multi_select",
        }
        assert (
            s.sent[-1]
            == "✅ Added preset(s): **Did this member show up?**, **Who didn't vote this week?**"
        )

    @pytest.mark.asyncio
    async def test_free_tier_drops_premium_picks_with_an_upsell(self):
        result, s = await _drive_picker(
            [{"action": "add", "selected": {"showed_up", "sit_out_count_4"}}]
        )
        assert [q["key"] for q in result] == ["showed_up"]
        assert s.sent[1] == (
            "💎 *Skipped Premium-only preset(s):* **Sit-out count, past 4 events**. Run "
            "`/upgrade` to unlock them, then re-run setup to add."
        )

    @pytest.mark.asyncio
    async def test_free_tier_only_premium_picks_returns_nothing(self):
        result, s = await _drive_picker([{"action": "add", "selected": {"sit_out_count_4"}}])
        assert result == []
        assert not any(t.startswith("✅ Added") for t in s.sent)

    @pytest.mark.asyncio
    async def test_free_cap_trims_to_the_room_left(self):
        result, s = await _drive_picker(
            [{"action": "add", "selected": {"showed_up", "sat_out"}}],
            existing=[{"key": "a"}, {"key": "b"}],
        )
        assert [q["key"] for q in result] == ["showed_up"]
        assert s.sent[1] == (
            "⚠️ Free tier caps participation questions at 3. "
            "You picked 2 preset(s), but only "
            "1 fit. Added the first 1; ignore or upgrade "
            "for the rest."
        )

    @pytest.mark.asyncio
    async def test_derived_count_without_its_source_is_warned(self):
        result, s = await _drive_picker(
            [{"action": "add", "selected": {"sit_out_count_4"}}], is_premium=True
        )
        assert [q["key"] for q in result] == ["sit_out_count_4"]
        assert s.sent[1] == (
            "⚠️ **Sit-out count, past 4 events** needs the **Who sat out this week?** question "
            "as its source. Pick that one too in the next step, or "
            "this column will stay empty until the source is added."
        )

    @pytest.mark.asyncio
    async def test_derived_count_with_its_source_picked_is_not_warned(self):
        _, s = await _drive_picker(
            [{"action": "add", "selected": {"sit_out_count_4", "sat_out"}}], is_premium=True
        )
        assert not any(t.startswith("⚠️") for t in s.sent)

    @pytest.mark.asyncio
    async def test_add_with_nothing_selected_is_a_skip(self):
        result, _ = await _drive_picker([{"action": "add", "selected": set()}])
        assert result == []

    @pytest.mark.asyncio
    async def test_timeout_and_cancel(self):
        result, s = await _drive_picker(["timeout"])
        assert result is None and s.sent[-1] == TIMEOUT
        result, s = await _drive_picker(["cancel"])
        assert result is None and len(s.sent) == 1


# ── The question builder ─────────────────────────────────────────────────────


class TestQuestionBuilder:
    @pytest.mark.asyncio
    async def test_label_then_type_free_tier(self):
        import setup_cog

        result, s = await _drive_builder([("selected", "text")], replies=["Sat Out (A/B)"])
        assert result == {"key": "sat_out_a_b", "label": "Sat Out (A/B)", "type": "text"}
        assert s.sent[0] == (
            "**Question: Label**\n"
            "What's the label for this question? (e.g. `Sitting Out`, `Vote Count`)"
        )
        sel = _sel(s.views[0])
        assert [o.value for o in sel.options] == [
            "text",
            "yes_no",
            "numeric",
            "roster_names",
            "roster_multi_select",
        ]
        assert sel.placeholder == "Pick the answer type…"
        note = setup_cog._locked_types_note(
            "🔽 Single-select", "Multi-select", "📅 Date", "✨ Derived count"
        )
        assert s.sent[1] == "**Question: Answer Type**" + note

    @pytest.mark.asyncio
    async def test_premium_offers_every_type_without_the_note(self):
        _, s = await _drive_builder([("selected", "yes_no")], replies=["Late?"], is_premium=True)
        assert len(_sel(s.views[0]).options) == 9
        assert s.sent[1] == "**Question: Answer Type**"

    @pytest.mark.asyncio
    async def test_editing_shows_the_existing_label_and_type_and_keeps_a_blank_label(self):
        existing = {"key": "vote_count", "label": "Vote Count", "type": "numeric"}
        result, s = await _drive_builder([("selected", "text")], replies=[""], existing=existing)
        assert s.sent[0].endswith("\n*Existing label:* `Vote Count`")
        assert s.sent[1].startswith("**Question: Answer Type**\n*Existing type:* `numeric`")
        assert result == {"key": "vote_count", "label": "Vote Count", "type": "text"}

    @pytest.mark.asyncio
    async def test_empty_label_skips(self):
        result, s = await _drive_builder([], replies=["   "])
        assert result is None and s.sent[-1] == "⚠️ Empty label. Skipping this question."

    @pytest.mark.asyncio
    async def test_label_timeout(self):
        result, s = await _drive_builder([], replies=[asyncio.TimeoutError()])
        assert result is None and s.sent[-1] == TIMEOUT

    @pytest.mark.asyncio
    async def test_type_timeout_and_cancel(self):
        result, s = await _drive_builder(["timeout"], replies=["X"])
        assert result is None and s.sent[-1] == TIMEOUT
        result, s = await _drive_builder(["cancel"], replies=["X"])
        assert result is None and TIMEOUT not in s.sent

    @pytest.mark.parametrize(
        "reply, extra",
        [
            ("0,500", {"min": 0, "max": 500}),
            ("0.5,2.5", {"min": 0.5, "max": 2.5}),
            (",10", {"max": 10}),
            ("none", {}),
            ("", {}),
        ],
    )
    @pytest.mark.asyncio
    async def test_numeric_bounds(self, reply, extra):
        result, s = await _drive_builder([("selected", "numeric")], replies=["Votes", reply])
        assert result == {"key": "votes", "label": "Votes", "type": "numeric", **extra}
        assert s.sent[2] == (
            "**Optional bounds**\nReply with `min,max` (e.g. `0,500`) or type `none` for no bounds."
        )

    @pytest.mark.asyncio
    async def test_unparseable_bounds_are_dropped_with_a_note(self):
        result, s = await _drive_builder([("selected", "numeric")], replies=["Votes", "lots"])
        assert result == {"key": "votes", "label": "Votes", "type": "numeric"}
        assert s.sent[-1] == "⚠️ Couldn't parse those bounds. Saving without min/max."

    @pytest.mark.asyncio
    async def test_select_options_are_split_and_capped(self):
        result, s = await _drive_builder(
            [("selected", "single_select")],
            replies=["Result", " Win, Loss ,, Draw "],
            is_premium=True,
        )
        assert result["options"] == ["Win", "Loss", "Draw"]
        assert s.sent[2] == (
            "**Options** *(💎 Premium)*\nList the choices separated by commas.\n"
            "Example: `Win, Loss, Draw`"
        )
        result, s = await _drive_builder(
            [("selected", "multi_select")], replies=["Result", ",,"], is_premium=True
        )
        assert result is None and s.sent[-1] == "⚠️ No options provided. Skipping this question."

    @pytest.mark.parametrize(
        "reply, fmt", [("default", "%m/%d/%Y"), ("", "%m/%d/%Y"), ("%d.%m.%Y", "%d.%m.%Y")]
    )
    @pytest.mark.asyncio
    async def test_date_format(self, reply, fmt):
        result, s = await _drive_builder(
            [("selected", "date")], replies=["When", reply], is_premium=True
        )
        assert result["date_format"] == fmt
        assert s.sent[2].startswith("**Date format** *(💎 Premium)*\n")

    @pytest.mark.asyncio
    async def test_roster_multi_select_prefill_is_premium_only(self):
        result, s = await _drive_builder([("selected", "roster_multi_select")], replies=["Who"])
        assert result == {"key": "who", "label": "Who", "type": "roster_multi_select"}
        assert len(s.views) == 1
        result, s = await _drive_builder(
            [("selected", "roster_multi_select"), ("selected", "discord_poll")],
            replies=["Who"],
            is_premium=True,
        )
        assert result["prefill_source"] == "discord_poll"
        assert s.sent[2].startswith("**Auto-prefill source (💎 Premium, optional)**\n")
        assert s.labels(1) == ["🗳️ Pre-fill from Discord poll signups", "✏️ Manual selection only"]
        result, _ = await _drive_builder(
            [("selected", "roster_multi_select"), ("selected", "")],
            replies=["Who"],
            is_premium=True,
        )
        assert "prefill_source" not in result

    @pytest.mark.asyncio
    async def test_derived_count_needs_a_source_question(self):
        result, s = await _drive_builder(
            [("selected", "derived_count")], replies=["Count"], is_premium=True, all_questions=[]
        )
        assert result is None
        assert s.sent[-1].startswith("⚠️ **Derived count needs a source question** of type ")

    @pytest.mark.asyncio
    async def test_derived_count_walks_source_lookback_and_display(self):
        sources = [{"key": "sat_out", "label": "Who sat out?", "type": "roster_multi_select"}]
        result, s = await _drive_builder(
            [("selected", "derived_count"), ("selected", "sat_out"), ("selected", True)],
            replies=["Sit-outs", "6"],
            is_premium=True,
            all_questions=sources,
        )
        assert result == {
            "key": "sit_outs",
            "label": "Sit-outs",
            "type": "derived_count",
            "source_question_key": "sat_out",
            "lookback_events": 6,
            "show_during_log": True,
        }
        assert [o.label for o in _sel(s.views[1]).options] == ["Who sat out?"]
        assert s.sent[3].startswith("**Lookback window** *(💎 Premium)*\n")
        assert s.labels(2) == [
            "📋 Show counts per member during the log",
            "🔍 Only show in the Trends Viewer",
        ]

    @pytest.mark.asyncio
    async def test_derived_count_lookback_defaults_and_reprompts(self):
        sources = [{"key": "sat_out", "label": "Who sat out?", "type": "roster_multi_select"}]
        result, s = await _drive_builder(
            [("selected", "derived_count"), ("selected", "sat_out"), ("selected", False)],
            replies=["Sit-outs", "x", "y", "z"],
            is_premium=True,
            all_questions=sources,
        )
        assert result["lookback_events"] == 4 and result["show_during_log"] is False
        assert s.sent.count("⚠️ `x` isn't a number. Please re-enter.") == 1
        assert sum(t.startswith("**Lookback window**") for t in s.sent) == 3
        result, _ = await _drive_builder(
            [("selected", "derived_count"), ("selected", "sat_out"), ("selected", True)],
            replies=["Sit-outs", ""],
            is_premium=True,
            all_questions=sources,
        )
        assert result["lookback_events"] == 4
