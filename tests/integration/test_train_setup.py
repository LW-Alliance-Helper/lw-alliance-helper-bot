"""
Characterization tests for the train wizard (`setup_cog.run_train_setup`),
its prompt-template manager (`_manage_train_templates`) and its Conductor
Rotation step (`_run_train_rotation_step`, #55 / #302), written against the
functions before their move out of `setup_cog` (#611 round 2) and kept
unchanged after it.

The four existing train tests answer every view with one flat override
dict. These drive the views one at a time, in order, and hold the prompts,
the button and option labels, the saved rows and the rotation step's
result dict. `ask_keep_or_change` and `ChannelSelectStep` are patched in
both of their homes so the same file holds before and after the move.
"""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# setup_cog re-exports the wizard pieces by name at import time; import it
# before anything patches `wizard_steps.X` (see CLAUDE.md, test gotchas).
import setup_cog  # noqa: F401

from tests.conftest import TEST_GUILD_ID, make_mock_interaction, make_mock_channel, make_mock_user
from messages import WIZARD_TIMEOUT, TIME_PARSE_RETRY, TIME_PARSE_GIVE_UP, CANCEL_PLAIN

TIMEOUT = WIZARD_TIMEOUT.format(wizard="🚂 Train")
USER_ID = 123456789
G = TEST_GUILD_ID


def _t(hhmm):
    """A time the way the wizard renders it (the zone abbreviation follows DST)."""
    from wizard_time import _format_time_with_tz

    return _format_time_with_tz(hhmm, "America/New_York")


def _wizard():
    import setup_cog

    return setup_cog.run_train_setup


def _templates():
    import setup_cog

    return setup_cog._manage_train_templates


def _rotation():
    import setup_cog

    return setup_cog._run_train_rotation_step


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

    def view_named(self, name):
        return next(v for v in self.views if type(v).__name__ == name)

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
    homes), premium and its limits, and a registry hook so a script can
    fire the wizard's cancel event."""

    def __init__(self, *, is_premium=False, channel_steps=None, script=None):
        self.is_premium = is_premium
        self.steps = list(channel_steps or [_channel_step()])
        self.script = script
        self.channel_calls = []

    def __enter__(self):
        import wizard_registry

        real_register = wizard_registry.register
        script = self.script

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
                return 10 if feature == "train_templates" else None
            return 1 if feature == "train_templates" else 3

        self._p = [
            patch("setup_cog.ChannelSelectStep", side_effect=_pick),
            patch("wizard_steps.ChannelSelectStep", side_effect=_pick),
            patch("premium.is_premium", AsyncMock(return_value=self.is_premium)),
            patch("premium.get_limit", side_effect=_limit),
            patch("wizard_registry.register", side_effect=_register),
        ]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()
        return False


async def _drive(answers, *, keep=(), replies=(), is_premium=False, channel_steps=None):
    interaction = make_mock_interaction()
    script = Script(interaction.channel, answers)
    with (
        _Env(is_premium=is_premium, channel_steps=channel_steps, script=script) as env,
        KeepRecorder(keep) as rec,
    ):
        await _wizard()(interaction, _bot(replies))
    return script, rec, env


def _train():
    import config

    return config.get_train_config(G)


def _save_train(**over):
    import config

    kw = dict(
        tab_name="My Tab",
        themes=["Heroic"],
        tones=["Serious"],
        prompt_template="prompt",
        default_tone="Serious",
        blurbs_enabled=1,
        reminders_enabled=1,
        reminder_channel_id=600600,
        reminder_time="22:00",
    )
    kw.update(over)
    config.save_train_config(G, **kw)


# ── The wizard ───────────────────────────────────────────────────────────────


MINIMAL = [
    ("selected", False),
    ("selected", False),
    ("selected", False),
]  # blurbs, reminders, rotation


class TestWizardFresh:
    @pytest.mark.asyncio
    async def test_everything_off_saves_and_summarises(self, seeded_db):
        s, rec, _ = await _drive(MINIMAL, keep=["Trains"])
        s.assert_order(
            "⚙️ **Train Schedule Setup**\n*Configure how the train schedule works for your alliance.*",
            "**Step 2 of 9 — ChatGPT Blurb Generation**",
            "ℹ️ *Skipping Steps 3–6 (themes, tones, default tone, prompt template) — blurb generation is off.*",
            "**Step 7 of 9 — Train Reminders**",
            "ℹ️ *Skipping the reminder channel and time — train reminders are off.*",
            "**Step 9 of 9 — Conductor Rotation** *(optional)*",
        )
        assert (
            "*(You can always set this up later by running `/setup` → 🚂 Train again)*"
            in s.sent[s.index_of("Step 2 of 9")]
        )
        assert rec.prompt(0) == (
            "**Step 1 of 9 — Schedule Sheet Tab**\n"
            "Which tab in your Google Sheet stores the train schedule?\n"
            "⚠️ *Make sure this tab exists in your sheet before continuing.*"
        )
        assert rec.kw(0)["default"] == "Train Schedule" and rec.kw(0)["current"] == "Train Schedule"
        assert rec.kw(0)["timeout_cmd"] == "setup → 🚂 Train"
        cfg = _train()
        assert cfg["tab_name"] == "Trains" and cfg["blurbs_enabled"] == 0
        assert cfg["reminders_enabled"] == 0 and cfg["rotation_enabled"] == 0
        assert cfg["reminder_time"] == "22:00" and cfg["dm_message"] == ""
        embed = s.final_embed
        assert embed.title == "✅ Train Schedule Configured"
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Sheet Tab", "Trains"),
            ("Blurb Generation", "Disabled"),
            ("Reminders", "Disabled"),
            ("Conductor Rotation", "Disabled"),
        ]
        assert embed.footer.text == "Run `/setup` → 🚂 Train to update settings."

    @pytest.mark.asyncio
    async def test_reminders_on_asks_channel_time_and_dm(self, seeded_db):
        from train_cog import DEFAULT_TRAIN_DM
        from messages import PREV_CHANNEL_GONE

        s, rec, env = await _drive(
            [("selected", False), ("selected", True), ("confirmed", True), ("selected", False)],
            keep=["Trains", "9:30am", "Choo choo {name}"],
            channel_steps=[_channel_step(600001, stale=True)],
        )
        s.assert_order(
            PREV_CHANNEL_GONE.format(channel_label="reminder"),
            "**Step 7a of 9 — Reminder Channel**\nWhich channel should the train reminder be posted to?",
        )
        assert (
            env.channel_calls[0]["suggested_name"] == "leadership"
            and env.channel_calls[0]["current_id"] == 0
        )
        assert rec.prompt(1) == (
            "**Step 7b of 9 — Reminder Time**\n"
            "What time should the reminder fire? *(in your timezone: (UTC-5) Eastern (New York, Toronto, Miami))*\n"
            "*(e.g. `10:00pm`, `9:00am`)*"
        )
        assert rec.kw(1)["default"] == "10:00pm" and rec.kw(1)["current"] == "10:00pm"
        assert rec.prompt(2).startswith("**Step 8 of 9 — Train DM Body (💎 Premium)**\n")
        assert (
            rec.kw(2)["default"] == DEFAULT_TRAIN_DM
            and rec.kw(2)["modal_label"] == "DM body (max 1000 chars)"
        )
        cfg = _train()
        assert cfg["reminders_enabled"] == 1 and cfg["reminder_channel_id"] == 600001
        assert cfg["reminder_time"] == "09:30" and cfg["dm_message"] == "Choo choo {name}"
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert fields["Reminder Channel"] == "<#600001>" and fields["Reminder Time"] == _t("09:30")

    @pytest.mark.asyncio
    async def test_default_dm_stores_blank_and_24h_time_is_kept(self, seeded_db):
        from train_cog import DEFAULT_TRAIN_DM

        await _drive(
            [("selected", False), ("selected", True), ("confirmed", True), ("selected", False)],
            keep=["Trains", "21:15", DEFAULT_TRAIN_DM],
        )
        assert _train()["reminder_time"] == "21:15" and _train()["dm_message"] == ""

    @pytest.mark.asyncio
    async def test_bad_time_is_retried_then_given_up(self, seeded_db):
        s, rec, _ = await _drive(
            [("selected", False), ("selected", True), ("confirmed", True)],
            keep=["Trains", "noon-ish", "later", "whenever"],
        )
        assert s.sent.count(TIME_PARSE_RETRY.format(raw="noon-ish")) == 1
        assert s.sent.count(TIME_PARSE_RETRY.format(raw="later")) == 1
        assert s.sent[-1] == TIME_PARSE_GIVE_UP.format(recovery="`/setup` → 🚂 Train")
        assert len(rec.calls) == 4
        assert not __import__("config").has_train_config(G)

    @pytest.mark.asyncio
    async def test_blurbs_on_free_tier_caps_themes_and_tones(self, seeded_db):
        from defaults import DEFAULT_THEMES, DEFAULT_TONES

        s, rec, _ = await _drive(
            [
                ("selected", True),  # blurbs
                ("selected", "Funny"),  # default tone
                ("action", "done"),  # templates
                ("selected", False),  # reminders
                ("selected", False),  # rotation
            ],
            keep=["Trains", "Epic, Chill, Loud, Extra", "Funny, Serious"],
        )
        assert rec.prompt(1) == (
            "**Step 3 of 9 — Themes**\n"
            "These appear as options when selecting a theme for the train."
            "\n*Free tier: up to 3 themes. Upgrade for unlimited.*"
        )
        assert rec.kw(1)["default"] == ", ".join(DEFAULT_THEMES[:3])
        assert rec.kw(1)["current"] == ", ".join(DEFAULT_THEMES[:3])
        assert (
            rec.kw(1)["modal_title"] == "Themes"
            and rec.kw(1)["modal_label"] == "Themes (comma-separated)"
        )
        assert rec.kw(2)["default"] == ", ".join(DEFAULT_TONES[:3])
        assert s.sent[s.index_of("ℹ️ Free tier: only the first 3 themes")] == (
            "ℹ️ Free tier: only the first 3 themes were saved (`Epic, Chill, Loud`). "
            "Upgrade to Premium to save more."
        )
        tone_view = s.view_named("ToneDefaultView")
        sel = next(c for c in tone_view.children if hasattr(c, "options"))
        assert [o.label for o in sel.options] == ["Funny", "Serious"]
        assert sel.placeholder == "Select default tone..."
        assert (
            s.sent[s.index_of("Step 5 of 9")]
            == "**Step 5 of 9 — Default Tone**\nWhich tone should be pre-selected by default?"
        )
        cfg = _train()
        assert cfg["themes"] == ["Epic", "Chill", "Loud"] and cfg["tones"] == ["Funny", "Serious"]
        assert cfg["default_tone"] == "Funny" and cfg["blurbs_enabled"] == 1
        assert [t["name"] for t in cfg["templates"]] == ["Default"] and cfg[
            "default_template"
        ] == "Default"
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert fields["Default Tone"] == "Funny" and fields["Themes"] == "Epic, Chill, Loud"
        assert fields["Templates (1)"] == "`Default` — default: **Default**"
        assert "Default Template Preview" in fields

    @pytest.mark.asyncio
    async def test_premium_has_no_caps_and_a_saved_default_tone_offers_keep(self, seeded_db):
        _save_train(tones=["Serious", "Funny"], default_tone="Funny")
        s, rec, _ = await _drive(
            [
                ("proceed", True),
                ("selected", True),
                ("selected", "Funny"),
                ("action", "done"),
                ("selected", False),
                ("selected", False),
            ],
            keep=["My Tab", "", ""],
            is_premium=True,
        )
        assert rec.prompt(1).endswith(
            "These appear as options when selecting a theme for the train."
        )
        tone_view = s.view_named("ToneDefaultView")
        assert tone_view.children[0].label == "Keep current: Funny"
        assert _train()["themes"] == ["Heroic"]  # blank reply keeps the current list

    @pytest.mark.asyncio
    async def test_turning_blurbs_and_reminders_off_keeps_saved_values(self, seeded_db):
        _save_train()
        s, _, _ = await _drive([("proceed", True)] + MINIMAL, keep=["My Tab"])
        s.assert_order(
            "ℹ️ Blurb generation disabled. Your themes, tones, and templates remain saved — re-enable later to restore them.",
            "ℹ️ Train reminders disabled. Your saved reminder channel and time remain saved — re-enable later to restore them.",
        )
        cfg = _train()
        assert cfg["themes"] == ["Heroic"] and cfg["reminder_channel_id"] == 600600


class TestWizardReentry:
    @pytest.mark.asyncio
    async def test_summary_lists_every_setting(self, seeded_db):
        import config

        _save_train()
        # Draft day 0 is Monday; it used to render as Sunday because the reader
        # treated a falsy 0 as unset.
        config.save_train_rotation_config(
            G,
            rotation_enabled=1,
            weekly_draft_day=0,
            rotation_public_channel_id=7,
            active_schedule_preset="Season 6",
        )
        s, rec, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert embed.title == "🚂 Current Train Setup"
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Schedule Tab", "My Tab"),
            ("Blurbs", "✅ Enabled"),
            ("Themes", "Heroic"),
            ("Tones", "Serious"),
            ("Default Tone", "Serious"),
            ("Reminders", "✅ Enabled"),
            ("Reminder Channel", "<#600600>"),
            ("Reminder Time", _t("22:00")),
            (
                "Conductor Rotation",
                f"✅ Enabled\nWeekly draft: Monday at {_t('22:00')}\nPublic posts: <#7>\nActive preset: Season 6",
            ),
        ]
        assert rec.calls == []

    @pytest.mark.asyncio
    async def test_summary_with_everything_off(self, seeded_db):
        _save_train(blurbs_enabled=0, reminders_enabled=0)
        s, _, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Schedule Tab", "My Tab"),
            ("Blurbs", "❌ Disabled"),
            ("Reminders", "❌ Disabled"),
            ("Conductor Rotation", "❌ Disabled"),
        ]


class TestWizardExits:
    @pytest.mark.parametrize("stop_at", [0, 1, 2])
    @pytest.mark.asyncio
    async def test_timeout_posts_the_route_back(self, seeded_db, stop_at):
        s, _, _ = await _drive(MINIMAL[:stop_at] + ["timeout"])
        assert s.sent[-1] == TIMEOUT

    @pytest.mark.parametrize("stop_at", [0, 1, 2])
    @pytest.mark.asyncio
    async def test_cancel_is_silent(self, seeded_db, stop_at):
        s, _, _ = await _drive(MINIMAL[:stop_at] + ["cancel"])
        s.assert_never(TIMEOUT)
        assert not __import__("config").has_train_config(G) or stop_at == 2

    @pytest.mark.asyncio
    async def test_unconfirmed_reminder_channel_is_a_timeout(self, seeded_db):
        s, _, _ = await _drive(
            [("selected", False), ("selected", True), ("confirmed", False)],
            channel_steps=[_channel_step(confirmed=False)],
        )
        assert s.sent[-1] == TIMEOUT

    @pytest.mark.asyncio
    async def test_rotation_abort_after_the_save_still_ends_the_wizard(self, seeded_db):
        """The train config is saved before Step 9; a timeout inside Step 9
        ends the wizard after the save and before the summary."""
        s, _, _ = await _drive(
            [("selected", False), ("selected", False), "timeout"], keep=["Trains"]
        )
        assert _train()["tab_name"] == "Trains"
        assert all(e is None for e in s.embeds)


# ── The template manager ─────────────────────────────────────────────────────


async def _drive_templates(answers, *, replies=(), existing=None, default_name="Default", cap=1):
    channel = make_mock_channel()
    cancel_event = asyncio.Event()
    script = Script(channel, answers, cancel_event)
    user = make_mock_user(USER_ID)
    result = await _templates()(
        bot=_bot(replies),
        channel=channel,
        check=lambda m: m.author == user and m.channel == channel,
        existing=existing
        if existing is not None
        else [{"name": "Default", "template": "body one"}],
        default_name=default_name,
        cap=cap,
        cancel_event=cancel_event,
    )
    return result, script


def _buttons(view):
    return {c.label: c for c in view.children if hasattr(c, "label")}


class TestTemplateManager:
    @pytest.mark.asyncio
    async def test_listing_and_free_cap_disable_add(self):
        (templates, default), s = await _drive_templates([("action", "done")])
        assert templates == [{"name": "Default", "template": "body one"}] and default == "Default"
        embed = s.embeds[0]
        assert embed.title == "**Step 6 of 9 — Prompt Templates**"
        assert embed.description == (
            "Saved ChatGPT prompt templates. The default ⭐ is the one used "
            "by the blurb wizard unless a member's day overrides it.\n\n"
            "`1.` **Default** ⭐ — *body one*"
            "\n\n*Slot usage: **1 of 1**.*"
        )
        b = _buttons(s.views[0])
        assert list(b) == ["➕ Add", "✏️ Edit", "⭐ Set Default", "🗑️ Delete", "✅ Done"]
        assert b["➕ Add"].disabled and b["🗑️ Delete"].disabled
        assert not b["✏️ Edit"].disabled and not b["✅ Done"].disabled

    @pytest.mark.asyncio
    async def test_empty_list_gets_an_empty_default(self):
        (templates, default), s = await _drive_templates(
            [("action", "done")], existing=[], default_name="x"
        )
        assert templates == [{"name": "Default", "template": ""}] and default == "Default"
        assert "`1.` **Default** ⭐ — *(empty)*" in s.embeds[0].description

    @pytest.mark.asyncio
    async def test_add_then_set_default_then_done_premium(self):
        (templates, default), s = await _drive_templates(
            [("action", "add"), {"action": "default"}, ("idx", 1), ("action", "done")],
            replies=["Birthday", "Happy {name}!"],
            cap=10,
        )
        assert templates == [
            {"name": "Default", "template": "body one"},
            {"name": "Birthday", "template": "Happy {name}!"},
        ]
        assert default == "Birthday"
        s.assert_order(
            "**Template name** *(short label)*\nReply with a name (e.g. `Birthday`, `Welcome`, `Default`). Reply `cancel` to abort.",
            "**Template body**\nPaste the full ChatGPT prompt.",
            "✅ Added **Birthday** (2 of 10).",
            "Which template?",
            "⭐ Default set to **Birthday**.",
        )
        assert (
            "*Slot usage: **2 of 10**.*" in [e.description for e in s.embeds if e is not None][-1]
        )
        pick = s.view_named("PickView")
        assert [
            o.label for o in next(c for c in pick.children if hasattr(c, "options")).options
        ] == ["Default", "Birthday"]

    @pytest.mark.asyncio
    async def test_duplicate_name_is_rejected(self):
        (templates, _), s = await _drive_templates(
            [("action", "add"), ("action", "done")], replies=["default"], cap=10
        )
        assert (
            s.sent[s.index_of("already exists")]
            == "⚠️ A template named **default** already exists. Try a different name."
        )
        assert len(templates) == 1

    @pytest.mark.asyncio
    async def test_edit_keeps_the_body_and_renames_the_default(self):
        (templates, default), s = await _drive_templates(
            [("action", "edit"), ("idx", 0), ("action", "done")],
            replies=["Standard", "keep"],
            cap=10,
        )
        assert templates == [{"name": "Standard", "template": "body one"}] and default == "Standard"
        assert s.sent[s.index_of("Template name")].startswith(
            "**Template name** *(short label)* — *editing* `Default`"
        )
        assert "✅ Updated **Standard**." in s.sent

    @pytest.mark.asyncio
    async def test_delete_moves_the_default_and_restores_an_empty_one(self):
        existing = [{"name": "A", "template": "a"}, {"name": "B", "template": "b"}]
        (templates, default), s = await _drive_templates(
            [
                ("action", "delete"),
                ("idx", 0),
                ("action", "delete"),
                ("idx", 0),
                ("action", "done"),
            ],
            existing=existing,
            default_name="A",
            cap=10,
        )
        assert "🗑️ Removed **A**." in s.sent
        assert (
            "🗑️ Removed **B**. (Restored an empty Default — you need at least one template.)"
            in s.sent
        )
        assert templates == [{"name": "Default", "template": ""}] and default == "Default"

    @pytest.mark.asyncio
    async def test_cancel_words_return_to_the_list(self):
        (templates, _), s = await _drive_templates(
            [("action", "add"), ("action", "add"), ("action", "done")],
            replies=["cancel", "New", "cancel"],
            cap=10,
        )
        assert len(templates) == 1 and sum(e is not None for e in s.embeds) == 3

    @pytest.mark.asyncio
    async def test_timeouts(self):
        result, s = await _drive_templates(["timeout"])
        assert result == (None, None) and s.sent[-1] == TIMEOUT
        result, s = await _drive_templates([("action", "edit"), "timeout"], cap=10)
        assert result == (None, None) and s.sent[-1] == TIMEOUT
        result, s = await _drive_templates(
            [("action", "add")], replies=[asyncio.TimeoutError()], cap=10
        )
        assert result == (None, None) and s.sent[-1] == TIMEOUT


# ── The Conductor Rotation step ──────────────────────────────────────────────


async def _drive_rotation(
    answers,
    *,
    keep=(),
    replies=(),
    is_premium=False,
    channel_steps=None,
    guild_tz="America/New_York",
):
    interaction = make_mock_interaction()
    channel = interaction.channel
    cancel_event = asyncio.Event()
    script = Script(channel, answers, cancel_event)
    with _Env(is_premium=is_premium, channel_steps=channel_steps) as env, KeepRecorder(keep) as rec:
        result = await _rotation()(
            bot=_bot(replies),
            interaction=interaction,
            channel=channel,
            user=make_mock_user(USER_ID),
            guild_id=G,
            cancel_event=cancel_event,
            guild_tz=guild_tz,
        )
    return result, script, rec, env


ROTATION_ON_FREE = [
    ("selected", True),  # toggle
    ("choice", "keep"),  # roster source
    ("selected", 6),  # weekly draft day
    ("selected", False),  # public posts
    ("choice", "default"),  # sheet tabs
    ("selected", False),  # weekly pattern
]


class TestRotationStep:
    @pytest.mark.asyncio
    async def test_off_stays_off_quietly(self, seeded_db):
        result, s, _, _ = await _drive_rotation([("selected", False)])
        assert result == {"enabled": False}
        assert s.sent[0].startswith("**Step 9 of 9 — Conductor Rotation** *(optional)*\n")
        assert "Rotation is currently on" not in s.sent[0] and len(s.sent) == 1

    @pytest.mark.asyncio
    async def test_turning_a_running_rotation_off(self, seeded_db):
        import config

        _save_train()
        config.save_train_rotation_config(G, rotation_enabled=1)
        result, s, _, _ = await _drive_rotation([("selected", False)])
        assert result == {"enabled": False}
        assert s.sent[0].endswith(
            "\n\n*Rotation is currently on. Choose Yes to review or adjust it.*"
        )
        assert s.sent[-1] == (
            "🚂 Conductor Rotation turned off. Your presets and member rules stay saved, "
            "so you can re-enable any time from `/setup` → 🚂 Train."
        )
        assert _train()["rotation_enabled"] == 0

    @pytest.mark.asyncio
    async def test_free_walk_reusing_the_reminder_channel(self, seeded_db):
        import train_rotation as tr

        _save_train()
        result, s, rec, _ = await _drive_rotation(ROTATION_ON_FREE, keep=["vs, contest, bogus"])
        s.assert_order(
            "**Step 9a of 9 — Roster Source**\nWho can be a conductor?",
            f"📅 Rotation will post the weekly draft and daily confirmation in <#600600> at **{_t('22:00')}** (reusing your reminder channel and time).",
            "**Step 9d of 9 — Weekly Draft Day**",
            "**Step 9e of 9 — Public Posts**",
            "**Step 9f of 9 — Role-Scoped Days** 💎\nLeadership / VS / Contest / Event days that rotate only members in a specific Discord role are a Premium feature.",
            "**Step 9h of 9 — Sheet Tabs**",
            "**Step 9i of 9 — Weekly Pattern** *(recommended)*",
            "ℹ️ No weekly pattern set yet, so the bot uses a default until you build one from `/train` → 📅 Schedule presets (you can set member rules there too).",
        )
        assert s.sent[s.index_of("Step 9a")].endswith(
            "Current: tab **Member Roster**, name column **B**."
        )
        assert [c.label for c in s.view_named("_RosterView").children] == [
            "⚙️ Set roster tab + column",
            "Keep current",
        ]
        day = s.view_named("_WeekdaySelectView")
        assert day.children[0].label == "Keep current: Sunday"
        assert [o.label for o in day.children[1].options] == tr.WEEKDAY_NAMES
        tabs = s.sent[s.index_of("Step 9h")]
        assert tabs.endswith(
            "History tab: **Train History** (default)\n"
            "Member Rules: **Train Member Rules** (default)\n"
            "Day Rules: **Train Day Rules** (default)\n\n"
            "Keep these, or define your own?"
        )
        assert [c.label for c in s.view_named("_TabsChoiceView").children] == [
            "✅ Use default",
            "✏️ Define my own",
        ]
        assert rec.prompt(0).startswith(
            "**Step 9g of 9 — Counted Reasons** *(most alliances keep the default)*\n"
        )
        assert rec.prompt(0).endswith(f"Comma-separated; valid: `{', '.join(tr.REASONS)}`.")
        assert (
            rec.kw(0)["default"] == ", ".join(tr.DEFAULT_COUNTED_REASONS)
            and rec.kw(0)["current"] == ""
        )

        cfg = _train()
        assert cfg["rotation_enabled"] == 1 and cfg["weekly_draft_day"] == 6
        assert cfg["counted_reasons"] == "contest,vs" and cfg["rotation_public_channel_id"] == 0
        assert cfg["active_schedule_preset"] == "Standard Week"
        assert result == {
            "enabled": True,
            "summary": (
                "Roster: tab Member Roster, name column B\n"
                f"Draft + confirm: <#600600> at {_t('22:00')}\n"
                "Weekly draft day: Sunday\n"
                "Public posts: off (record only)\n"
                "Active preset: Standard Week"
            ),
            "open_editor": False,
            "preset": None,
            "day_rules_tab": "Train Day Rules",
        }

    @pytest.mark.asyncio
    async def test_free_roster_source_set_through_the_modal(self, seeded_db):
        import config

        _save_train()
        result, s, _, _ = await _drive_rotation(
            [("selected", True), {"choice": "set", "modal_out": ("Members", 3)}]
            + ROTATION_ON_FREE[2:]
        )
        assert "✅ Roster source set: tab **Members**, name column **D**." in s.sent
        assert config.get_member_roster_config(G)["tab_name"] == "Members"
        assert config.get_member_roster_config(G)["enabled"] == 0
        assert result["summary"].startswith("Roster: tab Members, name column D\n")

    @pytest.mark.asyncio
    async def test_without_reminders_it_asks_its_own_channel_and_time(self, seeded_db):
        _save_train(reminders_enabled=0, reminder_channel_id=0)
        result, s, rec, env = await _drive_rotation(
            [("selected", True), ("choice", "keep"), ("confirmed", True)] + ROTATION_ON_FREE[2:],
            keep=["8:15pm", ""],
        )
        assert s.sent[s.index_of("Step 9b")].startswith("**Step 9b of 9 — Rotation Channel**\n")
        assert env.channel_calls[0]["suggested_name"] == "leadership"
        assert rec.prompt(0) == (
            "**Step 9c of 9 — Rotation Time**\n"
            "What time should the draft and daily confirmation fire? "
            "*(in your timezone: (UTC-5) Eastern (New York, Toronto, Miami))*\n*(e.g. `10:00pm`, `9:00am`)*"
        )
        assert rec.kw(0)["default"] == "10:00pm" and rec.kw(0)["current"] == ""
        cfg = _train()
        assert cfg["reminder_channel_id"] == 600001 and cfg["reminder_time"] == "20:15"
        assert f"Draft + confirm: <#600001> at {_t('20:15')}" in result["summary"]

    @pytest.mark.asyncio
    async def test_an_unreadable_rotation_time_falls_back_to_ten(self, seeded_db):
        _save_train(reminders_enabled=0, reminder_channel_id=0)
        _, s, _, _ = await _drive_rotation(
            [("selected", True), ("choice", "keep"), ("confirmed", True)] + ROTATION_ON_FREE[2:],
            keep=["dusk", ""],
        )
        assert (
            "⚠️ Couldn't read that time, so I'll use 10:00pm. You can change it later from `/setup` → 🚂 Train."
            in s.sent
        )
        assert _train()["reminder_time"] == "22:00"

    @pytest.mark.asyncio
    async def test_public_posts_pick_a_channel(self, seeded_db):
        from messages import PREV_CHANNEL_GONE

        _save_train()
        __import__("config").save_train_rotation_config(
            G, rotation_enabled=0, rotation_public_channel_id=5
        )
        result, s, _, env = await _drive_rotation(
            [
                ("selected", True),
                ("choice", "keep"),
                ("selected", 0),
                ("selected", True),
                ("confirmed", True),
                ("choice", "default"),
                ("selected", False),
            ],
            channel_steps=[_channel_step(9, stale=True)],
        )
        s.assert_order(
            PREV_CHANNEL_GONE.format(channel_label="public post"),
            "Which channel should confirmed conductors be announced in?",
        )
        assert (
            env.channel_calls[0]["suggested_name"] == "general"
            and env.channel_calls[0]["current_id"] == 5
        )
        assert _train()["rotation_public_channel_id"] == 9
        assert (
            "Public posts: <#9>" in result["summary"]
            and "Weekly draft day: Monday" in result["summary"]
        )

    @pytest.mark.asyncio
    async def test_premium_synced_roster_and_role_scoped_days(self, seeded_db):
        import config

        _save_train()
        config.save_member_roster_config(G, enabled=1, tab_name="Roster")
        result, s, _, _ = await _drive_rotation(
            [
                ("selected", True),
                ("selected", 6),
                ("selected", False),
                ("selected", True),  # role-scoped days offer
                {"rule_type_roles": {"vs": 4242}},  # attach view
                ("choice", "default"),
                ("selected", False),
            ],
            is_premium=True,
        )
        assert s.sent[s.index_of("Step 9a")] == (
            "**Step 9a of 9 — Roster Source** 💎\n"
            "Using your synced **Member Roster** (tab **Roster**) "
            "as the conductor pool. Members stay current automatically, and role-scoped "
            "day rules are unlocked for you in a later step."
        )
        assert s.sent[s.index_of("Step 9f")].startswith(
            "**Step 9f of 9 — Role-Scoped Days** *(optional)* 💎\n"
        )
        attach_prompt = s.sent[s.index_of("Pick a rule type and a role")]
        assert attach_prompt.endswith("**Attached so far:**\n_No roles attached yet._")
        assert _train()["rule_type_roles"] == {"vs": 4242}
        assert result["summary"].startswith("Roster: synced Member Roster (tab Roster) 💎\n")
        assert result["summary"].endswith("Rule-type roles: VS (you assign) → <@&4242>")

    @pytest.mark.asyncio
    async def test_premium_without_sync_explains_where_to_set_it_up(self, seeded_db):
        _save_train()
        _, s, _, _ = await _drive_rotation(
            [
                ("selected", True),
                ("selected", 6),
                ("selected", False),
                ("selected", False),
                ("choice", "default"),
                ("selected", False),
            ],
            is_premium=True,
        )
        assert s.sent[s.index_of("Step 9a")].startswith(
            "**Step 9a of 9 — Roster Source** 💎\nYour **Member Roster** sync isn't set up yet."
        )

    @pytest.mark.asyncio
    async def test_custom_tabs_and_a_named_pattern_open_the_editor(self, seeded_db):
        import train_rotation as tr

        _save_train()
        fake_preset = MagicMock(name="preset")
        with patch("train_rotation.load_preset", return_value=fake_preset) as load:
            result, s, _, _ = await _drive_rotation(
                [
                    ("selected", True),
                    ("choice", "keep"),
                    ("selected", 6),
                    ("selected", False),
                    {"choice": "custom", "modal_out": ("H", "M", "D")},
                    ("selected", True),
                ],
                replies=["Season 6"],
            )
        assert s.sent[s.index_of("What should we name this pattern?")] == (
            "What should we name this pattern? *(e.g. `Standard Week`, `Season 6`. Type a "
            "name, or `skip` to use the default.)*"
        )
        cfg = _train()
        assert (cfg["history_tab"], cfg["member_rules_tab"], cfg["day_rules_tab"]) == (
            "H",
            "M",
            "D",
        )
        assert cfg["active_schedule_preset"] == "Season 6"
        load.assert_called_once_with(G, "D", "Season 6")
        assert (
            result["open_editor"] is True
            and result["preset"] is fake_preset
            and result["day_rules_tab"] == "D"
        )
        assert "Active preset: Season 6" in result["summary"]
        assert tr.DEFAULT_PRESET_NAME == "Standard Week"

    @pytest.mark.asyncio
    async def test_skip_keeps_the_default_name_and_a_failed_load_gets_a_default_preset(
        self, seeded_db
    ):
        import train_rotation as tr

        _save_train()
        with patch("train_rotation.load_preset", side_effect=RuntimeError("no sheet")):
            result, s, _, _ = await _drive_rotation(
                ROTATION_ON_FREE[:5] + [("selected", True)], replies=["skip"]
            )
        assert s.sent[-1].startswith("⚠️ Couldn't load your saved pattern to open the editor: ")
        assert s.sent[-1].endswith(
            "Rotation is still saved and on — fix your Sheet and reopen the editor any time from `/train` → 📅 Schedule presets."
        )
        assert (
            isinstance(result["preset"], tr.SchedulePreset)
            and result["preset"].name == "Standard Week"
        )

    @pytest.mark.asyncio
    async def test_saved_custom_tabs_offer_keep_current(self, seeded_db):
        _save_train()
        __import__("config").save_train_rotation_config(G, rotation_enabled=0, history_tab="Hist")
        _, s, _, _ = await _drive_rotation(
            [
                ("selected", True),
                ("choice", "keep"),
                ("selected", 6),
                ("selected", False),
                ("choice", "keep"),
                ("selected", False),
            ]
        )
        assert [c.label for c in s.view_named("_TabsChoiceView").children] == [
            "Keep current",
            "↩️ Use default",
            "✏️ Define my own",
        ]
        assert "History tab: **Hist**\n" in s.sent[s.index_of("Step 9h")]
        assert _train()["history_tab"] == "Hist"

    @pytest.mark.parametrize("stop_at", [0, 1, 2, 3, 4, 5])
    @pytest.mark.asyncio
    async def test_timeout_at_each_view(self, seeded_db, stop_at):
        _save_train()
        result, s, _, _ = await _drive_rotation(ROTATION_ON_FREE[:stop_at] + ["timeout"])
        assert result is None and s.sent[-1] == TIMEOUT

    @pytest.mark.parametrize("stop_at", [0, 1, 2, 3, 4, 5])
    @pytest.mark.asyncio
    async def test_cancel_at_each_view(self, seeded_db, stop_at):
        _save_train()
        result, s, _, _ = await _drive_rotation(ROTATION_ON_FREE[:stop_at] + ["cancel"])
        assert result is None and TIMEOUT not in s.sent

    @pytest.mark.asyncio
    async def test_pattern_name_timeout_and_cancel(self, seeded_db):
        _save_train()
        result, s, _, _ = await _drive_rotation(
            ROTATION_ON_FREE[:5] + [("selected", True)], replies=[asyncio.TimeoutError()]
        )
        assert result is None and s.sent[-1] == TIMEOUT
        interaction = make_mock_interaction()
        cancel_event = asyncio.Event()
        script = Script(
            interaction.channel, ROTATION_ON_FREE[:5] + [("selected", True)], cancel_event
        )
        bot = AsyncMock()

        async def _wait_for(*a, **kw):
            cancel_event.set()
            await asyncio.sleep(5)

        bot.wait_for = _wait_for
        with _Env(), KeepRecorder([]):
            result = await _rotation()(
                bot=bot,
                interaction=interaction,
                channel=interaction.channel,
                user=make_mock_user(USER_ID),
                guild_id=G,
                cancel_event=cancel_event,
                guild_tz="America/New_York",
            )
        assert result is None and script.sent[-1] == CANCEL_PLAIN
