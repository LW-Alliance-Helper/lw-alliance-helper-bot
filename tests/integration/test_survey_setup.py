"""
Characterization tests for the survey wizard (`setup_cog.run_survey_setup`)
and the extra-survey flows around it (`run_create_new_extra_survey`,
`run_pick_survey_to_edit`, `run_remove_extra_survey`), written against the
functions before their move out of `setup_cog` (#611 round 2) and kept
unchanged after it.

The three existing survey tests answer every view with one flat override
dict. These drive the views one at a time, in order, and hold the prompts,
the button and option labels, the saved rows and the confirmation embed.
`ask_keep_or_change`, `ChannelSelectStep` and `_ensure_survey_tab` are
patched in both of their homes so the same file holds before and after
the move.
"""

import asyncio
import contextlib
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# setup_cog re-exports the wizard pieces by name at import time; import it
# before anything patches `wizard_steps.X` (see CLAUDE.md, test gotchas).
import setup_cog  # noqa: F401

from tests.conftest import TEST_GUILD_ID, make_mock_interaction
from messages import WIZARD_TIMEOUT, CANCEL_PLAIN

TIMEOUT = WIZARD_TIMEOUT.format(wizard="📋 Survey")
ADD_TIMEOUT = WIZARD_TIMEOUT.format(wizard="➕ Add Survey")
USER_ID = 123456789
G = TEST_GUILD_ID

SQUAD_INTRO = (
    "Please fill out this survey each week, if possible, to help keep track of squad "
    "powers, better balance Desert Storm teams, track alliance growth, and prepare for "
    "season events!"
)


def _wizard():
    import setup_cog

    return setup_cog.run_survey_setup


def _both(name):
    """The two homes of a survey-family name: `setup_cog` before the move
    and the module after it. Patching a home that has no such attribute
    is skipped, so the same file runs on both sides."""
    targets = [f"setup_cog.{name}"]
    try:
        import survey_setup  # noqa: F401

        targets.append(f"survey_setup.{name}")
    except ImportError:
        pass
    return targets


def _patch_run(run):
    """`run_survey_setup` replaced in both of its homes."""
    stack = contextlib.ExitStack()
    for t in _both("run_survey_setup"):
        stack.enter_context(patch(t, run))
    return stack


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
        if type(view).__name__ == "SurveyConfiguredView":
            return MagicMock(id=1)  # posted with the summary, never waited on
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


def _channel_step(channel_id=600001, *, stale=False, confirmed=True):
    return MagicMock(
        confirmed=confirmed,
        cancelled=False,
        selected_channel=MagicMock(id=channel_id),
        is_current_stale=stale,
        wait=AsyncMock(),
    )


def _bot(replies=()):
    bot = AsyncMock()
    queue = [r if isinstance(r, BaseException) else MagicMock(content=r) for r in replies]
    bot.wait_for = AsyncMock(side_effect=queue)
    return bot


class _Env:
    """The patches every drive shares: the channel picker stand-ins (both
    homes), the sheet touches (tab creation and header seeding), premium
    and its limits, and a registry hook so a script can fire the wizard's
    cancel event."""

    def __init__(
        self,
        *,
        is_premium=False,
        channel_steps=None,
        script=None,
        question_cap=3,
        seeded=None,
    ):
        self.is_premium = is_premium
        self.steps = list(channel_steps or [_channel_step(600001), _channel_step(600002)])
        self.script = script
        self.question_cap = question_cap
        self.channel_calls = []
        self.ensured = []
        self.seed_calls = []
        self.seeded = seeded

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

        async def _limit(feature, *a, **kw):
            if self.is_premium:
                return None
            return self.question_cap if feature == "survey_questions" else None

        async def _ensure(channel, guild_id, tab):
            env.ensured.append(tab)

        def _seed(guild_id, **kw):
            env.seed_calls.append(kw)
            if env.seeded is None:
                return [kw["tab_responses"], kw["tab_history"]]
            if isinstance(env.seeded, BaseException):
                raise env.seeded
            return env.seeded

        self._p = [
            patch("setup_cog.ChannelSelectStep", side_effect=_pick),
            patch("wizard_steps.ChannelSelectStep", side_effect=_pick),
            patch("premium.is_premium", AsyncMock(return_value=self.is_premium)),
            patch("premium.get_limit", side_effect=_limit),
            patch("wizard_registry.register", side_effect=_register),
            patch("survey.seed_survey_headers", side_effect=_seed),
        ]
        self._p += [patch(t, side_effect=_ensure) for t in _both("_ensure_survey_tab")]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()
        return False


async def _drive(
    answers,
    *,
    keep=(),
    replies=(),
    is_premium=False,
    channel_steps=None,
    question_cap=3,
    seeded=None,
    target_survey_id=None,
    target_survey_name=None,
    template=None,
):
    interaction = make_mock_interaction()
    script = Script(interaction.channel, answers)
    with (
        _Env(
            is_premium=is_premium,
            channel_steps=channel_steps,
            script=script,
            question_cap=question_cap,
            seeded=seeded,
        ) as env,
        KeepRecorder(keep) as rec,
    ):
        await _wizard()(
            interaction,
            _bot(replies),
            target_survey_id=target_survey_id,
            target_survey_name=target_survey_name,
            template=template,
        )
    return script, rec, env


def _survey():
    import config

    return config.get_survey_config(G)


def _q(label, type_="text", options=(), placeholder="", **extra):
    key = label.lower().replace(" ", "_").replace("(", "").replace(")", "").replace("/", "_")
    return {
        "key": key,
        "label": label,
        "type": type_,
        "options": list(options),
        "placeholder": placeholder,
        "max_chars": 0,
        **extra,
    }


def _save_default(**over):
    import config

    kw = dict(
        tab_squad_powers="Squad Powers",
        tab_history="Survey History",
        questions=[_q("Time Zone"), _q("Role", "dropdown", ["Tank", "Air"])],
        intro_message="Take the survey!",
        template="squad_power",
    )
    kw.update(over)
    config.save_survey_config(G, **kw)
    config.update_config_field(G, "survey_channel_id", 300100)
    config.update_config_field(G, "survey_notify_channel_id", 300200)


# ── The default survey, fresh ────────────────────────────────────────────────


# A fresh guild's main survey is the squad-power template: the intro is
# offered (Keep current / Edit) and Step 6 offers the template's questions.
TEMPLATE_ASIS = [("intro_choice", "keep"), ("choice", "template_asis")]


class TestFreshDefault:
    @pytest.mark.asyncio
    async def test_template_as_is_saves_and_summarises(self, seeded_db):
        from defaults import DEFAULT_SURVEY_QUESTIONS
        from survey_hub import SURVEY_HUB_BTN_TRANSLATE, SURVEY_HUB_BTN_POST, SURVEY_HUB_BTN_EDIT

        s, rec, env = await _drive(TEMPLATE_ASIS)
        s.assert_order(
            "⚙️ **Survey Setup**\nConfigure the survey for your alliance.",
            "**Step 1 of 6 — Survey Channel**\n"
            "Select the channel where the survey button will be posted for members to access:",
            "**Step 2 of 6 — Survey Notification Channel**\n"
            "Select the channel where leadership will be notified when a member submits the survey:",
            "**Next: your two sheet tabs**\n"
            "This survey needs its own pair of tabs, separate from any other "
            "survey's: one holding each member's current answers, one holding "
            "every submission over time. **I'll create them for you** if they "
            "aren't in your sheet yet.",
            "**Step 5 of 6 — Survey Intro Message**\n"
            "Members see this introductory message before taking the survey.\n\n"
            f"**The template suggests:**\n>>> {SQUAD_INTRO}",
            "**Step 6 of 6 — Survey Questions**\n\n"
            "**Questions from the Squad Power Survey template:**\n"
            "1. **1st Squad Power** — numeric\n",
            "Added column headers to **Squad Powers** and **Survey History**.",
        )
        assert env.channel_calls[0]["suggested_name"] == "squad-survey"
        assert env.channel_calls[0]["include_threads"] is False
        assert env.channel_calls[0]["current_id"] == 333333333333333333
        assert env.channel_calls[1]["suggested_name"] == "survey-responses"
        assert env.channel_calls[1]["current_id"] == 444444444444444444
        assert rec.prompt(0) == (
            "**Step 3 of 6 — Member Statistics Tab**\n"
            "Which tab should hold each member's current statistics? "
            "We update their row here every time they submit."
        )
        assert rec.kw(0)["default"] == "Squad Powers" and rec.kw(0)["current"] == "Squad Powers"
        assert rec.kw(0)["modal_title"] == "Member Statistics Tab"
        assert rec.kw(0)["modal_label"] == "Tab name"
        assert rec.kw(0)["timeout_cmd"] == "setup_survey"
        assert rec.prompt(1) == (
            "**Step 4 of 6 — Survey History Tab**\n"
            "Which tab should hold the full history of every submission? "
            "We add a row here each time, so you keep a record over time."
        )
        assert rec.kw(1)["default"] == "Survey History" and rec.kw(1)["current"] == "Survey History"
        assert env.ensured == ["Squad Powers", "Survey History"]
        step6 = s.sent[s.index_of("Step 6 of 6")]
        assert "2. **1st Squad Type** — dropdown: Missile, Air, Tank\n" in step6
        assert step6.endswith("How would you like to set up this survey's questions?")
        assert "current questions" not in step6
        start = s.view_named("QuestionStartView")
        assert [c.label for c in start.children] == [
            "✅ Use these questions",
            "✏️ Edit these questions",
            "♻️ Start from scratch",
        ]
        intro = s.view_named("IntroChoiceView")
        assert [c.label for c in intro.children] == ["Keep current", "✏️ Edit"]

        cfg = _survey()
        assert cfg["tab_squad_powers"] == "Squad Powers" and cfg["tab_history"] == "Survey History"
        assert cfg["questions"] == DEFAULT_SURVEY_QUESTIONS
        assert cfg["intro_message"] == SQUAD_INTRO and cfg["template"] == "squad_power"
        import config

        g = config.get_config(G)
        assert g.survey_channel_id == 600001 and g.survey_notify_channel_id == 600002
        assert env.seed_calls == [
            dict(
                tab_responses="Squad Powers",
                tab_history="Survey History",
                questions=DEFAULT_SURVEY_QUESTIONS,
            )
        ]

        embed = s.final_embed
        assert embed.title == "✅ Survey Configured"
        assert [(f.name, f.value, f.inline) for f in embed.fields][:4] == [
            ("Survey Channel", "<#600001>", True),
            ("Notification Channel", "<#600002>", True),
            ("Member Statistics Tab", "Squad Powers", True),
            ("Survey History Tab", "Survey History", True),
        ]
        qf = embed.fields[4]
        assert qf.name == "Questions" and qf.inline is False
        assert qf.value.startswith(
            "• **1st Squad Power** — numeric\n• **1st Squad Type** — dropdown (Missile, Air, Tank)\n"
        )
        assert embed.footer.text == (
            "Run /setup → 📋 Survey again to update. Run /survey and click "
            f"{SURVEY_HUB_BTN_TRANSLATE} if your members don't all read the "
            "language you write your questions in."
        )
        confirm = s.views[-1]
        assert type(confirm).__name__ == "SurveyConfiguredView"
        assert [c.label for c in confirm.children] == [SURVEY_HUB_BTN_POST, SURVEY_HUB_BTN_EDIT]
        assert confirm.owner_id == USER_ID and confirm._survey_name == "Default"
        assert confirm.timeout == 900 and confirm.message is not None

    @pytest.mark.asyncio
    async def test_edited_intro_is_typed_as_a_reply(self, seeded_db):
        s, _, _ = await _drive(
            [("intro_choice", "edit"), ("choice", "template_asis")], replies=["  Hello all  "]
        )
        assert s.sent[s.index_of("Type the new intro")] == (
            "Type the new intro message below. Use the one above as a guide, or paste in your own."
        )
        assert _survey()["intro_message"] == "Hello all"

    @pytest.mark.asyncio
    async def test_stale_channels_are_named(self, seeded_db):
        from messages import PREV_CHANNEL_GONE

        s, _, _ = await _drive(
            TEMPLATE_ASIS,
            channel_steps=[_channel_step(1, stale=True), _channel_step(2, stale=True)],
        )
        s.assert_order(
            PREV_CHANNEL_GONE.format(channel_label="survey"),
            "**Step 1 of 6",
            PREV_CHANNEL_GONE.format(channel_label="notification"),
            "**Step 2 of 6",
        )

    @pytest.mark.asyncio
    async def test_premium_offers_threads(self, seeded_db):
        _, _, env = await _drive(TEMPLATE_ASIS, is_premium=True)
        assert env.channel_calls[0]["include_threads"] is True

    @pytest.mark.asyncio
    async def test_header_seed_failure_is_reported_and_the_survey_still_saved(self, seeded_db):
        s, _, _ = await _drive(TEMPLATE_ASIS, seeded=RuntimeError("403"))
        assert s.sent[s.index_of("couldn't add the column headers")] == (
            "⚠️ I couldn't add the column headers to your sheet just now. Your "
            "survey is saved, and I'll add them the first time a member submits."
        )
        s.assert_never("Added column headers")
        assert _survey()["tab_squad_powers"] == "Squad Powers"

    @pytest.mark.asyncio
    async def test_nothing_seeded_says_nothing(self, seeded_db):
        s, _, _ = await _drive(TEMPLATE_ASIS, seeded=[])
        s.assert_never("Added column headers")


# ── The tab steps ────────────────────────────────────────────────────────────


class TestTabs:
    @pytest.mark.asyncio
    async def test_a_tab_another_survey_owns_is_rejected_and_re_asked(self, seeded_db):
        import config

        config.save_extra_survey(
            G,
            "vp-buff",
            survey_name="VP Buff",
            tab_squad_powers="VP Buff",
            tab_history="VP Buff History",
            template="scratch",
        )
        s, rec, env = await _drive(
            TEMPLATE_ASIS, keep=["VP Buff", "Squad Powers", "Survey History"]
        )
        assert s.sent[s.index_of("already used by")] == (
            "⚠️ **VP Buff** is already used by **VP Buff**. Every survey needs its "
            "own two tabs, because sharing one would write both surveys' answers "
            "into the same columns. Please pick a different name."
        )
        assert len(rec.calls) == 3
        assert rec.kw(1)["current"] == "" and rec.kw(1)["default"] == "Squad Powers"
        assert env.ensured == ["Squad Powers", "Survey History"]

    @pytest.mark.asyncio
    async def test_a_survey_s_two_tabs_cannot_be_the_same(self, seeded_db):
        s, rec, _ = await _drive(TEMPLATE_ASIS, keep=["Mine", "mine", "Mine History"])
        assert "**mine** is already used by **this survey**" in s.sent[s.index_of("already used")]
        assert rec.kw(2)["current"] == ""
        assert _survey()["tab_history"] == "Mine History"

    @pytest.mark.asyncio
    async def test_template_tabs_claimed_elsewhere_fall_back_to_name_derived(self, seeded_db):
        import config

        config.save_extra_survey(
            G,
            "other",
            survey_name="Other",
            tab_squad_powers="Squad Powers",
            tab_history="Other History",
            template="squad_power",
        )
        _, rec, _ = await _drive(TEMPLATE_ASIS)
        assert rec.kw(0)["default"] == "Survey" and rec.kw(1)["default"] == "Survey History"

    @pytest.mark.asyncio
    async def test_another_feature_s_tab_only_warns(self, seeded_db):
        with patch("config.tabs_in_use", return_value={"squad powers": "your Growth tab"}):
            s, _, _ = await _drive(TEMPLATE_ASIS)
        assert s.sent[s.index_of("Heads up")].startswith(
            "⚠️ Heads up: **Squad Powers** is also your Growth tab."
        )
        assert _survey()["tab_squad_powers"] == "Squad Powers"

    @pytest.mark.asyncio
    async def test_renaming_the_stats_tab_follows_buddy(self, seeded_db):
        _save_default()
        with patch("config.follow_survey_tab_rename", return_value=True) as follow:
            s, _, _ = await _drive(
                [("proceed", True)] + TEMPLATE_ASIS, keep=["Stats", "Survey History"]
            )
        follow.assert_called_once_with(G, "Squad Powers", "Stats")
        assert s.sent[s.index_of("Buddy System")] == (
            "🔗 Your Buddy System read professions from **Squad Powers**, "
            "so I've pointed it at **Stats** too."
        )


# ── Step 6 choices and the builder ───────────────────────────────────────────


BUILDER_FINISH = [("action", "finish")]


class TestQuestionStart:
    @pytest.mark.asyncio
    async def test_template_edit_loads_the_template_into_the_builder(self, seeded_db):
        from defaults import DEFAULT_SURVEY_QUESTIONS

        s, _, _ = await _drive(
            [("intro_choice", "keep"), ("choice", "template_edit")] + BUILDER_FINISH
        )
        listing = s.sent[s.index_of("**Survey Questions:**")]
        assert listing.startswith(
            "**Survey Questions:**\n1. **1st Squad Power** — numeric *(help: e.g. 43.27)*\n"
        )
        assert "*(help:" in listing
        lv = s.view_named("QuestionListView")
        selects = [c for c in lv.children if hasattr(c, "options")]
        assert [c.placeholder for c in selects] == [
            "✏️ Edit a question...",
            "🗑️ Delete a question...",
        ]
        assert selects[0].options[0].label == "Edit: 1st Squad Power"
        assert selects[1].options[0].label == "Delete: 1st Squad Power"
        assert [c.label for c in lv.children if hasattr(c, "label")] == [
            "➕ Add Question",
            "✅ Finish Survey Setup",
        ]
        assert _survey()["questions"] == DEFAULT_SURVEY_QUESTIONS

    @pytest.mark.asyncio
    async def test_scratch_with_no_questions_refuses_to_save(self, seeded_db):
        s, _, _ = await _drive([("intro_choice", "keep"), ("choice", "scratch")] + BUILDER_FINISH)
        assert s.sent[s.index_of("(no questions added yet)")] == (
            "**Survey Questions:**\n*(no questions added yet)*"
        )
        lv = s.view_named("QuestionListView")
        assert not any(hasattr(c, "options") for c in lv.children)
        assert s.sent[-1] == "⚠️ No questions defined. Run `/setup` → 📋 Survey to try again."
        import config

        assert not config.has_survey_config(G)

    @pytest.mark.asyncio
    async def test_re_run_offers_the_current_questions_too(self, seeded_db):
        _save_default()
        s, _, _ = await _drive(
            [("proceed", True), ("intro_choice", "keep"), ("choice", "edit")] + BUILDER_FINISH
        )
        step6 = s.sent[s.index_of("Step 6 of 6")]
        assert (
            "**This survey's current questions:**\n1. **Time Zone** — text\n2. **Role** — dropdown: Tank, Air\n\n"
            in step6
        )
        assert [c.label for c in s.view_named("QuestionStartView").children] == [
            "✅ Use these questions",
            "✏️ Edit these questions",
            "✏️ Edit this survey's questions",
            "♻️ Start from scratch",
        ]
        assert [q["label"] for q in _survey()["questions"]] == ["Time Zone", "Role"]

    @pytest.mark.asyncio
    async def test_scratch_survey_first_run_goes_straight_to_the_builder(self, seeded_db):
        s, _, _ = await _drive(
            [("action", "add"), ("selected", "text")] + BUILDER_FINISH,
            replies=["Hi", "Favourite colour", "none"],
            target_survey_id="feedback",
            target_survey_name="Feedback",
            template="scratch",
        )
        s.assert_never("Step 6 of 6")
        s.assert_never("Step 5 of 6 — Survey Intro Message\nMembers see")
        assert s.sent[s.index_of("Step 5 of 6")] == (
            "**Step 5 of 6 — Survey Intro Message**\n"
            "When your survey is posted, what introductory message do you want your members to see "
            "before they take the survey?\n\n"
            "**Example:**\n"
            "*Please take a moment to fill this out. It helps leadership keep our records current "
            "and plan around what everyone tells us.*"
        )
        assert [type(v).__name__ for v in s.views if not isinstance(v, MagicMock)][0] == (
            "QuestionListView"
        )


class TestBuilder:
    ADD_TEXT = [("action", "add"), ("selected", "text")]

    @pytest.mark.asyncio
    async def test_add_a_text_question(self, seeded_db):
        s, _, _ = await _drive(
            [("intro_choice", "keep"), ("choice", "scratch")] + self.ADD_TEXT + BUILDER_FINISH,
            replies=["Time Zone (UTC)", "Use your in-game name"],
        )
        assert s.sent[s.index_of("Question 1 — Label")] == (
            "**Question 1 — Label**\n"
            "What is the label for this question? (e.g. `Time Zone`, `Preferred Role`)"
        )
        assert s.sent[s.index_of("Question 1 — Answer Type")] == (
            "**Question 1 — Answer Type**\nPick how members answer this question."
            "\n*🔒 More answer types are 💎 Premium: Multi-select and 📅 Date. "
            "Run `/upgrade` to unlock them.*"
        )
        tv = s.view_named("TypeView")
        sel = next(c for c in tv.children if hasattr(c, "options"))
        assert sel.placeholder == "Select answer type..."
        assert [(o.label, o.value) for o in sel.options] == [
            ("🔤 Text — member types their answer", "text"),
            ("🔽 Dropdown — member selects from a list", "dropdown"),
            ("🔢 Numeric — number, with shorthand support", "numeric"),
        ]
        assert s.sent[s.index_of("Question 1 — Help Text")] == (
            "**Question 1 — Help Text**\n"
            "Do you want to show help text for this question? "
            "This appears as a hint to help members answer correctly.\n"
            "*(e.g. `Pick the closest match` or `Use your in-game name`)*\n"
            "Type your help text, or type `none` to skip."
        )
        assert (
            s.sent[s.index_of("✅ Added")] == "✅ Added **Time Zone (UTC)** (1 question(s) so far)."
        )
        listing = s.sent[s.index_of("1. **Time Zone (UTC)**")]
        assert listing == (
            "**Survey Questions:**\n1. **Time Zone (UTC)** — text *(help: Use your in-game name)*"
        )
        assert _survey()["questions"] == [
            _q("Time Zone (UTC)", placeholder="Use your in-game name")
        ]
        assert _survey()["questions"][0]["key"] == "time_zone_utc"
        assert s.final_embed.fields[4].value == "• **Time Zone (UTC)** — text"

    @pytest.mark.asyncio
    async def test_premium_type_list_and_none_help(self, seeded_db):
        s, _, _ = await _drive(
            [("intro_choice", "keep"), ("choice", "scratch")] + self.ADD_TEXT + BUILDER_FINISH,
            replies=["Q", "NONE"],
            is_premium=True,
        )
        assert s.sent[s.index_of("Question 1 — Answer Type")] == (
            "**Question 1 — Answer Type**\nPick how members answer this question."
        )
        sel = next(c for c in s.view_named("TypeView").children if hasattr(c, "options"))
        assert [(o.label, o.value) for o in sel.options][3:] == [
            ("Multi-select — pick multiple options", "multi_select"),
            ("📅 Date — formatted date entry", "date"),
        ]
        assert _survey()["questions"][0]["placeholder"] == ""

    @pytest.mark.asyncio
    async def test_dropdown_asks_options_and_caps_at_25(self, seeded_db):
        opts = ", ".join(str(i) for i in range(30))
        s, _, _ = await _drive(
            [
                ("intro_choice", "keep"),
                ("choice", "scratch"),
                ("action", "add"),
                ("selected", "dropdown"),
            ]
            + BUILDER_FINISH,
            replies=["Role", "none", opts],
        )
        assert s.sent[s.index_of("Question 1 — Options")] == (
            "**Question 1 — Options**\n"
            "Enter the options as comma-separated values. Maximum of 25.\n"
            "*(e.g. `Yes, No, Not sure`)*"
        )
        q = _survey()["questions"][0]
        assert q["options"] == [str(i) for i in range(25)]
        assert (
            s.final_embed.fields[4].value
            == "• **Role** — dropdown (" + ", ".join(q["options"]) + ")"
        )

    @pytest.mark.asyncio
    async def test_numeric_free_asks_scale_and_teases_bounds(self, seeded_db):
        s, _, _ = await _drive(
            [
                ("intro_choice", "keep"),
                ("choice", "scratch"),
                ("action", "add"),
                ("selected", "numeric"),
                ("selected", "M"),
            ]
            + BUILDER_FINISH,
            replies=["Squad 1", "none"],
        )
        assert s.sent[s.index_of("Question 1 — Number Scale")] == (
            "**Question 1 — Number Scale**\n"
            "How big are these numbers typically? Picking a scale lets members type "
            "the natural shorthand (`301`) instead of the full value (`304,743,912`) — "
            "the bot accepts both either way."
        )
        sel = next(c for c in s.view_named("MagnitudeView").children if hasattr(c, "options"))
        assert sel.placeholder == "Select number scale..."
        assert [(o.label, o.value) for o in sel.options] == [
            ("Exact number — type what you mean (e.g. drone level 150 stays 150)", "raw"),
            ("Thousands (K) — 5 becomes 5,000", "K"),
            ("Millions (M) — 301 becomes 301,000,000", "M"),
            ("Billions (B) — 1.2 becomes 1,200,000,000", "B"),
        ]
        assert s.sent[s.index_of("Min/max bounds")] == (
            "💎 *Min/max bounds are a Premium feature — this question will accept any number.*"
        )
        q = _survey()["questions"][0]
        assert q["magnitude"] == "M" and "min" not in q and "max" not in q

    @pytest.mark.asyncio
    async def test_numeric_premium_bounds(self, seeded_db):
        answers = [
            ("intro_choice", "keep"),
            ("choice", "scratch"),
            ("action", "add"),
            ("selected", "numeric"),
            ("selected", "raw"),
        ] + BUILDER_FINISH
        s, _, _ = await _drive(answers, replies=["Drone", "none", "0,100"], is_premium=True)
        assert s.sent[s.index_of("Numeric Bounds")] == (
            "**Question 1 — Numeric Bounds** *(💎 Premium)*\n"
            "Reply with `min,max` (e.g. `0,100`), `min,` for only a minimum, "
            "`,max` for only a maximum, or `none` to skip both bounds.\n"
            "*Bounds are checked against the stored value after scaling.*"
        )
        q = _survey()["questions"][0]
        assert q["min"] == 0.0 and q["max"] == 100.0

        again = [("proceed", True)] + answers
        s, _, _ = await _drive(again, replies=["Drone", "none", ",50"], is_premium=True)
        q = _survey()["questions"][0]
        assert "min" not in q and q["max"] == 50.0

        s, _, _ = await _drive(again, replies=["Drone", "none", "none"], is_premium=True)
        q = _survey()["questions"][0]
        assert "min" not in q and "max" not in q

    @pytest.mark.asyncio
    async def test_unparseable_bounds_end_the_wizard(self, seeded_db):
        s, _, _ = await _drive(
            [
                ("intro_choice", "keep"),
                ("choice", "scratch"),
                ("action", "add"),
                ("selected", "numeric"),
                ("selected", "raw"),
            ],
            replies=["Drone", "none", "lots,few"],
            is_premium=True,
        )
        assert s.sent[-1] == "⚠️ Couldn't parse bounds. Run `/setup` → 📋 Survey to try again."
        import config

        assert not config.has_survey_config(G)

    @pytest.mark.asyncio
    async def test_date_format(self, seeded_db):
        answers = [
            ("intro_choice", "keep"),
            ("choice", "scratch"),
            ("action", "add"),
            ("selected", "date"),
        ] + BUILDER_FINISH
        s, _, _ = await _drive(answers, replies=["Joined", "none", "default"], is_premium=True)
        assert s.sent[s.index_of("Date Format")] == (
            "**Question 1 — Date Format** *(💎 Premium)*\n"
            "Reply with a strptime-style format (e.g. `%m/%d/%Y`, `%Y-%m-%d`), "
            "or reply `default` for `%m/%d/%Y`."
        )
        assert _survey()["questions"][0]["date_format"] == "%m/%d/%Y"
        await _drive(
            [("proceed", True)] + answers, replies=["Joined", "none", "%Y-%m-%d"], is_premium=True
        )
        assert _survey()["questions"][0]["date_format"] == "%Y-%m-%d"

    @pytest.mark.asyncio
    async def test_multi_select_asks_options(self, seeded_db):
        await _drive(
            [
                ("intro_choice", "keep"),
                ("choice", "scratch"),
                ("action", "add"),
                ("selected", "multi_select"),
            ]
            + BUILDER_FINISH,
            replies=["Days", "none", "Mon, Tue"],
            is_premium=True,
        )
        q = _survey()["questions"][0]
        assert q["type"] == "multi_select" and q["options"] == ["Mon", "Tue"]
        assert q["max_chars"] == 0

    @pytest.mark.asyncio
    async def test_edit_shows_existing_values_and_replaces_in_place(self, seeded_db):
        _save_default(
            questions=[
                _q("Squad", "numeric", magnitude="M", placeholder="big"),
                _q("Joined", "date", date_format="%d/%m"),
            ]
        )
        s, _, _ = await _drive(
            [
                ("proceed", True),
                ("intro_choice", "keep"),
                ("choice", "edit"),
                {"action": "edit", "edit_index": 0},
                ("selected", "numeric"),
                ("selected", "K"),
            ]
            + BUILDER_FINISH,
            replies=["", "", "1,"],
            is_premium=True,
        )
        assert s.sent[s.index_of("Question 1 — Label")].endswith("\n*Existing label:* `Squad`")
        assert s.sent[s.index_of("Question 1 — Answer Type")].endswith(
            "Pick how members answer this question.\n*Existing type:* `numeric`"
        )
        assert s.sent[s.index_of("Question 1 — Help Text")].endswith(
            "\n*Existing help text:* `big`"
        )
        assert s.sent[s.index_of("Question 1 — Number Scale")].endswith("\n*Existing scale:* `M`")
        assert s.sent[s.index_of("✅ Updated")] == "✅ Updated **Squad**."
        qs = _survey()["questions"]
        assert qs[0]["label"] == "Squad" and qs[0]["magnitude"] == "K" and qs[0]["min"] == 1.0
        assert "max" not in qs[0]
        # A blank help reply on edit stores blank, not the old value.
        assert qs[0]["placeholder"] == ""
        assert qs[1]["label"] == "Joined"

    @pytest.mark.asyncio
    async def test_edit_a_dropdown_and_a_date_show_their_extras(self, seeded_db):
        _save_default(questions=[_q("Role", "dropdown", ["A", "B"]), _q("Joined", "date")])
        s, _, _ = await _drive(
            [
                ("proceed", True),
                ("intro_choice", "keep"),
                ("choice", "edit"),
                {"action": "edit", "edit_index": 0},
                ("selected", "dropdown"),
                {"action": "edit", "edit_index": 1},
                ("selected", "date"),
            ]
            + BUILDER_FINISH,
            replies=["Role", "none", "C, D", "Joined", "none", ""],
            is_premium=True,
        )
        assert s.sent[s.index_of("Question 1 — Options")].endswith("\n*Existing options:* `A, B`")
        assert s.sent[s.index_of("Question 2 — Help Text")].endswith(
            "\n*Existing help text:* `none`"
        )
        assert s.sent[s.index_of("Question 2 — Date Format")].endswith(
            "\n*Existing format:* `%m/%d/%Y`"
        )
        assert s.sent[s.index_of("Question 2 — Answer Type")].endswith("\n*Existing type:* `date`")
        qs = _survey()["questions"]
        assert qs[0]["options"] == ["C", "D"] and qs[1]["date_format"] == "%m/%d/%Y"

    @pytest.mark.asyncio
    async def test_delete_removes_and_relists(self, seeded_db):
        _save_default()
        s, _, _ = await _drive(
            [
                ("proceed", True),
                ("intro_choice", "keep"),
                ("choice", "edit"),
                {"action": "delete", "del_index": 1},
            ]
            + BUILDER_FINISH,
        )
        assert s.sent[s.index_of("🗑️ Removed")] == "🗑️ Removed **Role**."
        assert [q["label"] for q in _survey()["questions"]] == ["Time Zone"]
        assert len([v for v in s.views if type(v).__name__ == "QuestionListView"]) == 2

    @pytest.mark.asyncio
    async def test_free_cap_blocks_add_and_relists(self, seeded_db):
        _save_default(questions=[_q("A"), _q("B"), _q("C")])
        s, _, _ = await _drive(
            [("proceed", True), ("intro_choice", "keep"), ("choice", "edit"), ("action", "add")]
            + BUILDER_FINISH,
        )
        cap = [e for e in s.embeds if e is not None][1]
        assert cap.title == "📊 Free tier limit reached"
        assert cap.description == (
            "You've used **3 of 3** questions on the free tier. Upgrade to 💎 LW Alliance "
            "Helper Premium to unlock more."
        )
        assert len([v for v in s.views if type(v).__name__ == "QuestionListView"]) == 2
        assert len(_survey()["questions"]) == 3

    @pytest.mark.asyncio
    async def test_label_help_and_options_timeouts(self, seeded_db):
        base = [("intro_choice", "keep"), ("choice", "scratch"), ("action", "add")]
        s, _, _ = await _drive(base, replies=[asyncio.TimeoutError()])
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive(base + [("selected", "text")], replies=["Q", asyncio.TimeoutError()])
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive(
            base + [("selected", "dropdown")], replies=["Q", "none", asyncio.TimeoutError()]
        )
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive(
            base + [("selected", "numeric"), ("selected", "raw")],
            replies=["Q", "none", asyncio.TimeoutError()],
            is_premium=True,
        )
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive(
            base + [("selected", "date")],
            replies=["Q", "none", asyncio.TimeoutError()],
            is_premium=True,
        )
        assert s.sent[-1] == TIMEOUT
        import config

        assert not config.has_survey_config(G)

    @pytest.mark.asyncio
    async def test_blank_label_on_add_stores_blank(self, seeded_db):
        """Recorded as it stands: a blank label reply on Add keeps going with
        an empty label and key (the edit path falls back to the existing one)."""
        await _drive(
            [
                ("intro_choice", "keep"),
                ("choice", "scratch"),
                ("action", "add"),
                ("selected", "text"),
            ]
            + BUILDER_FINISH,
            replies=["   ", "none"],
        )
        assert _survey()["questions"][0]["label"] == "" and _survey()["questions"][0]["key"] == ""


# ── Re-entry ─────────────────────────────────────────────────────────────────


class TestReentry:
    @pytest.mark.asyncio
    async def test_summary_and_no_changes(self, seeded_db):
        _save_default()
        s, rec, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert embed.title == "📋 Current Survey Setup"
        assert embed.description == (
            "This survey is already configured. Would you like to edit these settings?"
        )
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Survey Channel", "<#300100>"),
            ("Notification Channel", "<#300200>"),
            ("Member Statistics Tab", "Squad Powers"),
            ("Survey History Tab", "Survey History"),
            ("Questions", "2 configured"),
        ]
        assert s.sent[-1] == "✅ No changes made. Your survey setup is still active."
        assert rec.calls == []

    @pytest.mark.asyncio
    async def test_summary_with_nothing_set(self, seeded_db):
        _save_default(tab_squad_powers="", tab_history="", questions=[])
        import config

        config.update_config_field(G, "survey_channel_id", 0)
        config.update_config_field(G, "survey_notify_channel_id", 0)
        s, _, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert [f.value for f in embed.fields] == ["*not set*"] * 4 + ["*none*"]

    @pytest.mark.asyncio
    async def test_saved_intro_is_offered_and_currents_thread_through(self, seeded_db):
        _save_default()
        s, rec, env = await _drive(
            [("proceed", True), ("intro_choice", "keep"), ("choice", "edit")] + BUILDER_FINISH
        )
        assert s.sent[s.index_of("Step 5 of 6")] == (
            "**Step 5 of 6 — Survey Intro Message**\n"
            "Members see this introductory message before taking the survey.\n\n"
            "**Currently saved:**\n>>> Take the survey!"
        )
        assert env.channel_calls[0]["current_id"] == 300100
        assert env.channel_calls[1]["current_id"] == 300200
        assert rec.kw(0)["current"] == "Squad Powers" and rec.kw(1)["current"] == "Survey History"
        assert _survey()["intro_message"] == "Take the survey!"

    @pytest.mark.asyncio
    async def test_long_saved_intro_is_previewed_truncated(self, seeded_db):
        _save_default(intro_message="x" * 600)
        s, _, _ = await _drive(
            [("proceed", True), ("intro_choice", "keep"), ("choice", "edit")] + BUILDER_FINISH
        )
        assert s.sent[s.index_of("Step 5 of 6")].endswith(">>> " + "x" * 500 + "…")
        assert _survey()["intro_message"] == "x" * 600

    @pytest.mark.asyncio
    async def test_a_scratch_survey_with_no_intro_asks_for_one_on_re_run(self, seeded_db):
        _save_default(intro_message="", template="scratch")
        s, _, _ = await _drive(
            [("proceed", True), ("choice", "edit")] + BUILDER_FINISH, replies=["New intro"]
        )
        assert "When your survey is posted" in s.sent[s.index_of("Step 5 of 6")]
        assert _survey()["intro_message"] == "New intro" and _survey()["template"] == "scratch"
        assert [c.label for c in s.view_named("QuestionStartView").children] == [
            "✏️ Edit this survey's questions",
            "♻️ Start from scratch",
        ]


# ── Extra (named) surveys ───────────────────────────────────────────────────


class TestExtraSurvey:
    @pytest.mark.asyncio
    async def test_create_from_scratch_saves_an_extra_row(self, seeded_db):
        import config

        s, rec, env = await _drive(
            [("action", "add"), ("selected", "text")] + BUILDER_FINISH,
            replies=["Welcome!", "Colour", "none"],
            target_survey_id="alliance-feedback",
            target_survey_name="Alliance Feedback",
            template="scratch",
        )
        assert (
            s.sent[0]
            == "⚙️ **Survey Setup — Alliance Feedback**\nConfigure the survey for your alliance."
        )
        assert env.channel_calls[0]["suggested_name"] == "alliance-feedback"
        assert env.channel_calls[1]["suggested_name"] == "alliance-feedback-responses"
        assert env.channel_calls[0]["current_id"] == 0
        assert rec.prompt(0) == (
            "**Step 3 of 6 — Current Answers Tab**\n"
            "Which tab should hold each member's current answers? "
            "We update their row here every time they submit."
        )
        assert rec.kw(0)["default"] == "Alliance Feedback" and rec.kw(0)["current"] == ""
        assert rec.prompt(1).startswith("**Step 4 of 6 — Answer History Tab**\n")
        assert rec.kw(1)["default"] == "Alliance Feedback History"
        row = config.get_survey(G, "alliance-feedback")
        assert row["survey_name"] == "Alliance Feedback" and row["template"] == "scratch"
        assert row["tab_squad_powers"] == "Alliance Feedback"
        assert row["tab_history"] == "Alliance Feedback History"
        assert row["survey_channel_id"] == 600001 and row["notify_channel_id"] == 600002
        assert row["intro_message"] == "Welcome!"
        assert [q["label"] for q in row["questions"]] == ["Colour"]
        assert row["reminder_enabled"] == 0 and row["reminder_message"] == ""
        embed = s.final_embed
        assert embed.title == "✅ Survey Configured — Alliance Feedback"
        assert embed.fields[2].name == "Current Answers Tab"
        assert embed.footer.text.startswith("Run /survey again to update.")
        confirm = s.views[-1]
        assert (
            confirm._survey_id == "alliance-feedback"
            and confirm._survey_name == "Alliance Feedback"
        )
        assert confirm._template_key == "scratch"

    @pytest.mark.asyncio
    async def test_re_edit_keeps_name_reminder_and_template(self, seeded_db):
        import config

        config.save_extra_survey(
            G,
            "vp",
            survey_name="VP Buff",
            tab_squad_powers="VP",
            tab_history="VP History",
            questions=[_q("Agree", "dropdown", ["Yes", "No"])],
            intro_message="Old intro",
            survey_channel_id=1,
            notify_channel_id=2,
            reminder_message="Remind!",
            reminder_enabled=1,
            template="scratch",
        )
        s, rec, env = await _drive(
            [("proceed", True), ("intro_choice", "keep"), ("choice", "edit")] + BUILDER_FINISH,
            target_survey_id="vp",
        )
        embed = [e for e in s.embeds if e is not None][0]
        assert embed.title == "📋 Current Survey Setup — VP Buff"
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Survey Channel", "<#1>"),
            ("Notification Channel", "<#2>"),
            ("Current Answers Tab", "VP"),
            ("Answer History Tab", "VP History"),
            ("Questions", "1 configured"),
        ]
        assert (
            s.sent[s.index_of("⚙️")]
            == "⚙️ **Survey Setup — VP Buff**\nConfigure the survey for your alliance."
        )
        assert env.channel_calls[0]["current_id"] == 1 and env.channel_calls[1]["current_id"] == 2
        assert env.channel_calls[0]["suggested_name"] == "vp-buff"
        assert rec.kw(0)["current"] == "VP" and rec.kw(0)["default"] == "VP Buff"
        row = config.get_survey(G, "vp")
        assert row["survey_name"] == "VP Buff" and row["reminder_message"] == "Remind!"
        assert row["reminder_enabled"] == 1 and row["template"] == "scratch"
        assert row["intro_message"] == "Old intro"

    @pytest.mark.asyncio
    async def test_a_missing_extra_id_is_a_fresh_named_survey(self, seeded_db):
        import config

        s, _, _ = await _drive(
            [("action", "add"), ("selected", "text")] + BUILDER_FINISH,
            replies=["Intro", "Q", "none"],
            target_survey_id="ghost",
            template="scratch",
        )
        assert s.sent[0] == "⚙️ **Survey Setup — ghost**\nConfigure the survey for your alliance."
        assert config.get_survey(G, "ghost")["survey_name"] == "ghost"

    @pytest.mark.asyncio
    async def test_squad_power_template_for_a_named_survey(self, seeded_db):
        """A second squad-power survey suggests name-derived tabs when the
        template's are taken by the default survey."""
        _save_default()
        _, rec, _ = await _drive(
            TEMPLATE_ASIS,
            target_survey_id="second",
            target_survey_name="Second Squad",
            template="squad_power",
        )
        assert rec.kw(0)["default"] == "Second Squad"
        assert rec.kw(1)["default"] == "Second Squad History"


# ── Exits ────────────────────────────────────────────────────────────────────


class TestExits:
    @pytest.mark.parametrize("stop_at", [0, 1])
    @pytest.mark.asyncio
    async def test_timeout_at_the_views(self, seeded_db, stop_at):
        s, _, _ = await _drive(TEMPLATE_ASIS[:stop_at] + ["timeout"])
        assert s.sent[-1] == TIMEOUT
        import config

        assert not config.has_survey_config(G)

    @pytest.mark.parametrize("stop_at", [0, 1])
    @pytest.mark.asyncio
    async def test_cancel_at_the_views(self, seeded_db, stop_at):
        s, _, _ = await _drive(TEMPLATE_ASIS[:stop_at] + ["cancel"])
        s.assert_never(TIMEOUT)

    @pytest.mark.asyncio
    async def test_builder_view_timeouts_and_cancels(self, seeded_db):
        base = [("intro_choice", "keep"), ("choice", "scratch")]
        s, _, _ = await _drive(base + ["timeout"])
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive(base + ["cancel"])
        s.assert_never(TIMEOUT)
        s, _, _ = await _drive(base + [("action", "add"), "timeout"], replies=["Q"])
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive(base + [("action", "add"), "cancel"], replies=["Q"])
        s.assert_never(TIMEOUT)
        s, _, _ = await _drive(
            base + [("action", "add"), ("selected", "numeric"), "timeout"], replies=["Q", "none"]
        )
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive(
            base + [("action", "add"), ("selected", "numeric"), "cancel"], replies=["Q", "none"]
        )
        s.assert_never(TIMEOUT)

    @pytest.mark.asyncio
    async def test_unconfirmed_channel_is_a_timeout(self, seeded_db):
        s, _, _ = await _drive([], channel_steps=[_channel_step(confirmed=False), _channel_step()])
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive([], channel_steps=[_channel_step(), _channel_step(confirmed=False)])
        assert s.sent[-1] == TIMEOUT

    @pytest.mark.asyncio
    async def test_abandoned_tab_question_ends_silently(self, seeded_db):
        s, rec, env = await _drive([], keep=[None])
        assert len(rec.calls) == 1 and env.ensured == []
        s.assert_never(TIMEOUT)
        s, rec, env = await _drive([], keep=["Squad Powers", None])
        assert len(rec.calls) == 2 and env.ensured == ["Squad Powers"]

    @pytest.mark.asyncio
    async def test_intro_reply_timeout_and_cancel(self, seeded_db):
        s, _, _ = await _drive([("intro_choice", "edit")], replies=[asyncio.TimeoutError()])
        assert s.sent[-1] == TIMEOUT
        # A /cancel while the free-text reply is pending posts the plain cancel line.
        interaction = make_mock_interaction()
        script = Script(interaction.channel, [("intro_choice", "edit")])
        bot = AsyncMock()

        async def _never(*a, **kw):
            script.cancel_event.set()
            await asyncio.sleep(10)

        bot.wait_for = _never
        with _Env(script=script), KeepRecorder([]):
            await _wizard()(interaction, bot)
        assert script.sent[-1] == CANCEL_PLAIN

    @pytest.mark.asyncio
    async def test_re_entry_summary_declined_leaves_the_row(self, seeded_db):
        _save_default()
        await _drive(["timeout"])
        assert _survey()["intro_message"] == "Take the survey!"


# ── run_create_new_extra_survey ──────────────────────────────────────────────


def _create():
    import setup_cog

    return setup_cog.run_create_new_extra_survey


async def _drive_create(answers, *, replies=(), run=None):
    interaction = make_mock_interaction()
    script = Script(interaction.channel, answers)
    run = run if run is not None else AsyncMock()
    with _Env(script=script), _patch_run(run):
        await _create()(interaction, _bot(replies))
    return script, run


class TestCreateNewExtraSurvey:
    @pytest.mark.asyncio
    async def test_template_path_uses_the_template_name(self, seeded_db):
        s, run = await _drive_create(
            [("start_choice", "template"), ("selected", "squad_power"), ("answered", True)]
        )
        assert s.sent[0] == (
            "💎 **Add a Survey**\n"
            "A survey is any set of questions you want to ask your members. Their "
            "latest answers go in one tab of your sheet, and every submission is "
            "kept in a second tab.\n\n"
            "Start from a ready-made template, or build your own questions from scratch?"
        )
        assert [c.label for c in s.view_named("StartChoiceView").children] == [
            "➕ Start from a template",
            "✏️ Start from scratch",
        ]
        assert s.sent[1] == (
            "**Pick a template**\n"
            "Each one comes with its questions already written. You can edit, "
            "add to, or remove any of them later in this wizard."
        )
        sel = next(c for c in s.view_named("TemplatePickView").children if hasattr(c, "options"))
        assert sel.placeholder == "Pick a template..."
        assert [(o.label, o.value, o.description) for o in sel.options] == [
            (
                "Squad Power Survey",
                "squad_power",
                "Squad powers and types, drone and gorilla levels, profession, and more.",
            )
        ]
        assert s.sent[2] == (
            "**Name your survey**\n"
            "This is what leadership and members will see when they pick or "
            "take this survey."
        )
        name_view = s.view_named("NameChoiceView")
        assert [c.label for c in name_view.children] == [
            "✅ Use this name: Squad Power Survey",
            "✏️ Name it myself",
        ]
        # The script marked the view answered without a name, which is what
        # a stopped-but-unanswered view looks like: nothing is created.
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_template_path_dispatches_with_slug_and_template(self, seeded_db):
        s, run = await _drive_create(
            [
                ("start_choice", "template"),
                ("selected", "squad_power"),
                {"answered": True, "survey_name": "Squad Power Survey"},
            ]
        )
        assert s.sent[-1] == (
            "✅ Creating new survey **Squad Power Survey** (id: `squad-power-survey`).\n"
            "Walking you through the same setup steps as `/setup` → 📋 Survey…"
        )
        assert run.await_args.kwargs == dict(
            target_survey_id="squad-power-survey",
            target_survey_name="Squad Power Survey",
            template="squad_power",
        )

    @pytest.mark.asyncio
    async def test_scratch_path_asks_for_a_typed_name_and_dedupes_the_slug(self, seeded_db):
        import config

        config.save_extra_survey(G, "feedback", survey_name="Feedback", template="scratch")
        config.save_extra_survey(G, "feedback-2", survey_name="Feedback", template="scratch")
        s, run = await _drive_create([("start_choice", "scratch")], replies=["  Feedback!  "])
        assert s.sent[1] == (
            "**Name your survey**\n"
            "Type a short display name for it (e.g. `Alliance Feedback` or "
            "`New Member Intake`). This is what leadership and members will see."
        )
        assert run.await_args.kwargs == dict(
            target_survey_id="feedback-3", target_survey_name="Feedback!", template="scratch"
        )

    @pytest.mark.asyncio
    async def test_scratch_name_is_capped_at_60(self, seeded_db):
        _, run = await _drive_create([("start_choice", "scratch")], replies=["x" * 70])
        assert run.await_args.kwargs["target_survey_name"] == "x" * 60

    @pytest.mark.asyncio
    async def test_empty_typed_name_creates_nothing(self, seeded_db):
        s, run = await _drive_create([("start_choice", "scratch")], replies=["   "])
        assert s.sent[-1] == (
            "⚠️ That name was empty, so nothing was created. "
            "Click **➕ Add Survey** on `/survey` to try again."
        )
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_slug_less_name_becomes_survey(self, seeded_db):
        _, run = await _drive_create([("start_choice", "scratch")], replies=["!!!"])
        assert run.await_args.kwargs["target_survey_id"] == "survey"

    @pytest.mark.asyncio
    async def test_timeouts_post_the_add_route(self, seeded_db):
        s, run = await _drive_create(["timeout"])
        assert s.sent[-1] == ADD_TIMEOUT
        s, run = await _drive_create([("start_choice", "template"), "timeout"])
        assert s.sent[-1] == ADD_TIMEOUT
        s, run = await _drive_create(
            [("start_choice", "template"), ("selected", "squad_power"), "timeout"]
        )
        assert s.sent[-1] == ADD_TIMEOUT
        s, run = await _drive_create(
            [("start_choice", "scratch")], replies=[asyncio.TimeoutError()]
        )
        assert s.sent[-1] == ADD_TIMEOUT
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancels_are_silent(self, seeded_db):
        for answers in (
            ["cancel"],
            [("start_choice", "template"), "cancel"],
            [("start_choice", "template"), ("selected", "squad_power"), "cancel"],
        ):
            s, run = await _drive_create(answers)
            s.assert_never(ADD_TIMEOUT)
            run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_cancel_during_the_typed_name_is_silent(self, seeded_db):
        interaction = make_mock_interaction()
        script = Script(interaction.channel, [("start_choice", "scratch")])
        bot = AsyncMock()

        async def _never(*a, **kw):
            script.cancel_event.set()
            await asyncio.sleep(10)

        bot.wait_for = _never
        run = AsyncMock()
        with _Env(script=script), _patch_run(run):
            await _create()(interaction, bot)
        script.assert_never(ADD_TIMEOUT)
        run.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_pre_wizard_registration_is_released_before_dispatch(self, seeded_db):
        import wizard_registry

        # Earlier tests in this process may have left registrations behind
        # (the old wizard returned without unregistering on most exits), so
        # count events rather than asking whether any is active.
        before = len(wizard_registry._active.get(USER_ID, []))
        seen = []

        async def _run(interaction, bot, **kw):
            seen.append(len(wizard_registry._active.get(interaction.user.id, [])))

        await _drive_create([("start_choice", "scratch")], replies=["Name"], run=_run)
        assert seen == [before]


# ── run_pick_survey_to_edit / run_remove_extra_survey ───────────────────────


async def _click_select(view, value):
    """Fire a module-built select's callback the way discord.py would."""
    sel = next(c for c in view.children if hasattr(c, "options"))
    sel._values = [value]
    inter = MagicMock()
    inter.response = AsyncMock()
    edited = {}

    async def _edit(inter, **kw):
        edited.update(kw)

    with patch("wizard_registry.safe_edit_response", side_effect=_edit):
        await sel.callback(inter)
    return edited


class TestPickSurveyToEdit:
    @pytest.mark.asyncio
    async def test_lists_default_first_and_dispatches(self, seeded_db):
        import config, setup_cog

        config.save_extra_survey(G, "vp", survey_name="VP Buff", template="scratch")
        interaction = make_mock_interaction()
        await setup_cog.run_pick_survey_to_edit(interaction, bot := AsyncMock())
        call = interaction.followup.send.await_args
        assert call.args[0] == "Which survey would you like to edit?"
        assert call.kwargs["ephemeral"] is True
        view = call.kwargs["view"]
        sel = next(c for c in view.children if hasattr(c, "options"))
        assert sel.placeholder == "Pick a survey to edit…"
        assert [(o.label, o.value) for o in sel.options] == [
            ("Default", "default"),
            ("VP Buff", "vp"),
        ]

        run = AsyncMock()
        with _patch_run(run):
            edited = await _click_select(view, "vp")
        assert edited["content"] == "✏️ Editing **VP Buff**…" and sel.disabled
        assert run.await_args.args == (interaction, bot)
        assert run.await_args.kwargs == dict(
            target_survey_id="vp", target_survey_name="VP Buff", template="scratch"
        )

    @pytest.mark.asyncio
    async def test_default_dispatches_with_no_target(self, seeded_db):
        import setup_cog

        interaction = make_mock_interaction()
        await setup_cog.run_pick_survey_to_edit(interaction, AsyncMock())
        view = interaction.followup.send.await_args.kwargs["view"]
        run = AsyncMock()
        with _patch_run(run):
            edited = await _click_select(view, "default")
        assert edited["content"] == "✏️ Editing **Default**…"
        assert run.await_args.kwargs["target_survey_id"] is None
        # The picker hands over the list's display name; the wizard ignores it
        # for the default survey (it only reads the name when an id is set).
        assert run.await_args.kwargs["target_survey_name"] == "Default"
        assert run.await_args.kwargs["template"] == "squad_power"


class TestRemoveExtraSurvey:
    @pytest.mark.asyncio
    async def test_nothing_to_remove(self, seeded_db):
        import setup_cog

        interaction = make_mock_interaction()
        await setup_cog.run_remove_extra_survey(interaction, AsyncMock())
        call = interaction.followup.send.await_args
        assert call.args[0] == (
            "*You have no extra surveys to remove.* "
            "Click **➕ Add Survey** on `/survey` to add one."
        )
        assert call.kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_pick_confirm_and_remove(self, seeded_db):
        import config, setup_cog

        config.save_extra_survey(G, "vp", survey_name="VP Buff", template="scratch")
        interaction = make_mock_interaction()
        await setup_cog.run_remove_extra_survey(interaction, AsyncMock())
        call = interaction.followup.send.await_args
        assert call.args[0] == "Pick which extra survey to remove:"
        view = call.kwargs["view"]
        sel = next(c for c in view.children if hasattr(c, "options"))
        assert sel.placeholder == "Pick a survey to remove…"
        assert [(o.label, o.value) for o in sel.options] == [("VP Buff", "vp")]
        edited = await _click_select(view, "vp")
        assert edited["content"] == "⚠️ Confirm: remove **VP Buff**?"
        confirm = edited["view"]
        assert type(confirm).__name__ == "_ConfirmRemoveView"
        assert [c.label for c in confirm.children] == ["🗑️ Remove", "❌ Cancel"]

        inter = MagicMock()
        inter.response = AsyncMock()
        edited = {}

        async def _edit(inter, **kw):
            edited.update(kw)

        with patch("wizard_registry.safe_edit_response", side_effect=_edit):
            await confirm.children[0].callback(inter)
        assert edited == {"content": "🗑️ Removed **VP Buff**.", "view": None}
        assert config.get_survey(G, "vp") is None

    @pytest.mark.asyncio
    async def test_cancel_leaves_it(self, seeded_db):
        import config, setup_cog

        config.save_extra_survey(G, "vp", survey_name="VP Buff", template="scratch")
        interaction = make_mock_interaction()
        await setup_cog.run_remove_extra_survey(interaction, AsyncMock())
        view = interaction.followup.send.await_args.kwargs["view"]
        confirm = (await _click_select(view, "vp"))["view"]
        inter = MagicMock()
        inter.response = AsyncMock()
        edited = {}

        async def _edit(inter, **kw):
            edited.update(kw)

        with patch("wizard_registry.safe_edit_response", side_effect=_edit):
            await confirm.children[1].callback(inter)
        assert edited == {"content": "❌ Canceled. No surveys removed.", "view": None}
        assert config.get_survey(G, "vp") is not None
