"""
Characterization tests for the storm setup wizard (`setup_cog.run_storm_setup`,
Desert Storm and Canyon Storm), written against the function before its move
to `storm_setup.py` (#611 round 2) and kept unchanged after it.

The existing wizard tests answer every view with one flat override dict and
assert on the saved rows. These drive the wizard's own views one at a time,
in order, and hold three things: the prompts and their order, the button
labels an officer sees (fresh setup against re-entry), and the rows the
wizard writes. The participation step and the structured-flow step are
each the subject of their own characterization file and are stubbed here
the way the existing tests stub them.

The `Script` harness answers each view the wizard posts, in order. An
entry is `(attribute, value)` to set on the view before stopping it,
`"timeout"` to stop it with nothing set, or `"cancel"` to fire the
wizard's cancel event while the view is waiting. `ChannelSelectStep` is
patched to a confirmed stand-in, which still passes through `channel.send`
and so still consumes one entry.
"""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# setup_cog re-exports the wizard pieces by name at import time. Import it
# before any test patches `wizard_steps.X`, or the first import inside a
# patched block binds the mock into setup_cog for the rest of the run.
import setup_cog  # noqa: F401

from tests.conftest import TEST_GUILD_ID, make_mock_interaction
from tests.integration.test_setup_flows import patch_keep_or_change
from messages import GENERIC_CMD_TIMEOUT, PREV_CHANNEL_GONE

DS_CMD = "setup → ⚔️ Desert Storm"
CS_CMD = "setup → 🛡️ Canyon Storm"


def _wizard():
    """Resolve the function under test at call time so the same tests run
    before and after the move out of `setup_cog`."""
    import setup_cog

    return setup_cog.run_storm_setup


class Script:
    """Answer each posted view in order; record everything sent."""

    def __init__(self, channel, answers):
        self.answers = list(answers)
        self.sent = []
        self.embeds = []
        self.views = []
        self.cancel_event = None
        channel.send = AsyncMock(side_effect=self._send)

    async def _send(self, content=None, embed=None, view=None, **kw):
        self.sent.append(content)
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
            attr, value = answer
            setattr(view, attr, value)
        try:
            view.stop()
        except Exception:
            pass
        return MagicMock(id=1)

    @property
    def texts(self):
        return [s or "" for s in self.sent]

    def index_of(self, needle):
        for i, s in enumerate(self.texts):
            if needle in s:
                return i
        raise AssertionError(f"{needle!r} never sent; sent: {self.texts}")

    def assert_order(self, *needles):
        positions = [self.index_of(n) for n in needles]
        assert positions == sorted(positions), list(zip(needles, positions))

    def assert_never(self, needle):
        assert not any(needle in s for s in self.texts), self.texts

    def labels(self, view_index):
        return [c.label for c in self.views[view_index].children]

    def view_named(self, class_name):
        for v in self.views:
            if type(v).__name__ == class_name:
                return v
        raise AssertionError(
            f"no {class_name} posted; got {[type(v).__name__ for v in self.views]}"
        )

    @property
    def final_embed(self):
        embeds = [e for e in self.embeds if e is not None]
        assert embeds, "no embed sent"
        return embeds[-1]


def _channel_step(channel_id, *, stale=False, confirmed=True):
    return MagicMock(
        confirmed=confirmed,
        cancelled=False,
        selected_channel=MagicMock(id=channel_id),
        is_current_stale=stale,
        wait=AsyncMock(),
    )


PARTICIPATION_OFF = {
    "enabled": 0,
    "tab_name": "",
    "questions": [],
    "roster_tab": "",
    "roster_name_col": 0,
    "roster_alias_col": -1,
    "roster_start_row": 2,
}


def _structured(**over):
    base = {
        "structured_flow_enabled": False,
        "power_metric_column": "B",
        "power_metric_tab": "",
        "power_match_column": "",
        "sub_mode": "pool",
        "signup_channel_id": 0,
        "signup_schedule_cron": "",
        "signups_tab": "",
        "rosters_tab": "",
        "attendance_tab": "",
        "strategies_tab": "DS Strategies",
        "member_rules_tab": "DS Member Rules",
        "poll_day_of_week": -1,
        "signup_time": "",
        "power_refresh_dm_enabled": False,
        "roster_dm_starter_template": "",
        "roster_dm_paired_sub_template": "",
        "roster_dm_pool_sub_template": "",
    }
    base.update(over)
    return base


async def _drive(
    answers,
    *,
    event_type="DS",
    keep_values=("DS Assignments", ""),
    channel_steps=None,
    participation=None,
    structured=None,
    is_premium=False,
    reply_text=None,
):
    """Run the wizard once. Returns the script; the saved rows are read
    back from the seeded database by each test."""
    interaction = make_mock_interaction()
    bot = AsyncMock()
    if reply_text is not None:
        bot.wait_for = AsyncMock(return_value=MagicMock(content=reply_text))
    script = Script(interaction.channel, answers)
    steps = iter(channel_steps or [_channel_step(555001), _channel_step(555002)])

    async def _participation(*a, **kw):
        return participation if participation is not None else dict(PARTICIPATION_OFF)

    async def _structured_step(*a, **kw):
        return structured if structured is not None else _structured()

    import wizard_registry

    real_register = wizard_registry.register

    def _register(user_id):
        ev = real_register(user_id)
        script.cancel_event = ev
        return ev

    with (
        patch("wizard_steps.ChannelSelectStep", side_effect=lambda *a, **kw: next(steps)),
        patch("setup_cog._run_storm_participation_step", side_effect=_participation),
        patch("setup_cog._run_structured_flow_setup_step", side_effect=_structured_step),
        patch("premium.is_premium", AsyncMock(return_value=is_premium)),
        patch("wizard_registry.register", side_effect=_register),
        patch_keep_or_change(list(keep_values)),
    ):
        await _wizard()(interaction, bot, event_type)
    return script


def _save_existing(event_type="DS", *, template="template body", teams="both", **kw):
    import config

    config.save_storm_config(
        TEST_GUILD_ID,
        event_type,
        tab_name=f"{event_type} Assignments",
        mail_template=template,
        timezone="America/New_York",
        log_channel_id=555700,
        post_channel_id=555800,
        teams=teams,
        **kw,
    )
    config.update_config_field(
        TEST_GUILD_ID, "ds_log_channel_id" if event_type == "DS" else "cs_log_channel_id", 555700
    )


# ── Fresh setup ──────────────────────────────────────────────────────────────


class TestFreshWalk:
    @pytest.mark.asyncio
    async def test_both_teams_one_shared_default_template(self, seeded_db):
        import config

        labels = config.get_storm_slot_labels("DS", TEST_GUILD_ID)
        s = await _drive(
            [
                ("selected", "both"),  # teams
                ("selected", 1),  # Team A slot
                ("selected", 2),  # Team B slot
                ("confirmed", True),  # log channel (stand-in)
                ("confirmed", True),  # post channel (stand-in)
                ("selected", "shared"),  # one template or two
                ("outcome", "default"),  # template choice
            ]
        )

        s.assert_order(
            "⚙️ **Desert Storm Setup**",
            "**Step 2 of 9: Which teams do you run for Desert Storm?**",
            "**Step 3 of 9: Team Time Slots**",
            "Which time slot does **Team A** run for Desert Storm?",
            "Which time slot does **Team B** run for Desert Storm?",
            "**Step 4 of 9: Storm Log Channel**",
            "**Step 5 of 9: Mail Post Channel**",
            "**Step 6 of 9: Mail Template**",
            "**Desert Storm Mail Template: Team A & B**",
        )
        # Fresh setup: no Keep current anywhere.
        assert s.labels(0) == ["Team A & Team B", "Team A only", "Team B only"]
        assert s.labels(1) == labels
        assert s.labels(2) == labels
        assert s.labels(5) == ["One template for both teams", "Separate templates per team"]
        assert s.labels(6) == ["↩️ Use default template", "✏️ Edit template"]
        s.assert_never("Current:")

        from defaults import DEFAULT_DS_TEMPLATE

        row = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert row["tab_name"] == "DS Assignments"
        assert row["teams"] == "both"
        assert row["post_channel_id"] == 555002
        assert row["team_a_slot_index"] == 1
        assert row["team_b_slot_index"] == 2
        assert row["dm_reminder_message"] == ""
        assert (
            config.get_storm_config(TEST_GUILD_ID, "DS_A")["mail_template"] == DEFAULT_DS_TEMPLATE
        )
        assert (
            config.get_storm_config(TEST_GUILD_ID, "DS_B")["mail_template"] == DEFAULT_DS_TEMPLATE
        )
        assert config.get_config(TEST_GUILD_ID).ds_log_channel_id == 555001

        embed = s.final_embed
        assert embed.title == "✅ Desert Storm Configured"
        fields = {f.name: f.value for f in embed.fields}
        assert fields["Teams"] == "A & B"
        assert fields["Team Times"] == f"A: {labels[0]} · B: {labels[1]}"
        assert fields["Log Channel"] == "<#555001>"
        assert fields["Post Channel"] == "<#555002>"
        assert fields["Participation Tracking"] == "❌ Disabled"
        assert fields["Structured Roster Flow"].startswith("❌ Disabled · Preset tabs:")
        assert "Template A Preview" in fields
        assert "Template B Preview" not in fields  # identical to A
        assert embed.footer.text == f"Run /{DS_CMD} again to update."
        s.assert_never("Want to post your first")

    @pytest.mark.asyncio
    async def test_separate_templates_one_edited_by_reply(self, seeded_db):
        import config
        from defaults import DEFAULT_DS_TEMPLATE

        s = await _drive(
            [
                ("selected", "both"),
                ("selected", 1),
                ("selected", 1),
                ("confirmed", True),
                ("confirmed", True),
                ("selected", "separate"),
                ("outcome", "edit"),  # Team A: paste
                ("outcome", "default"),  # Team B: default
            ],
            reply_text="  Custom A body {zones}  ",
        )
        s.assert_order(
            "**Desert Storm Mail Template: Team A**",
            "Paste your custom template for **Team A**",
            "**Desert Storm Mail Template: Team B**",
        )
        assert "**Available placeholders:**" in s.texts[s.index_of("Paste your custom template")]
        assert (
            config.get_storm_config(TEST_GUILD_ID, "DS_A")["mail_template"]
            == "Custom A body {zones}"
        )
        assert (
            config.get_storm_config(TEST_GUILD_ID, "DS_B")["mail_template"] == DEFAULT_DS_TEMPLATE
        )
        # The base row mirrors Team A.
        assert (
            config.get_storm_config(TEST_GUILD_ID, "DS")["mail_template"] == "Custom A body {zones}"
        )
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert "Template A Preview" in fields and "Template B Preview" in fields

    @pytest.mark.asyncio
    async def test_empty_edit_reply_falls_back_to_default(self, seeded_db):
        import config
        from defaults import DEFAULT_DS_TEMPLATE

        await _drive(
            [
                ("selected", "A"),
                ("selected", 2),
                ("confirmed", True),
                ("confirmed", True),
                ("outcome", "edit"),
            ],
            reply_text="   ",
        )
        assert (
            config.get_storm_config(TEST_GUILD_ID, "DS_A")["mail_template"] == DEFAULT_DS_TEMPLATE
        )

    @pytest.mark.asyncio
    async def test_canyon_storm_team_b_only(self, seeded_db):
        import config
        from defaults import DEFAULT_CS_TEMPLATE

        labels = config.get_storm_slot_labels("CS", TEST_GUILD_ID)
        s = await _drive(
            [
                ("selected", "B"),
                ("selected", 2),  # only Team B is asked
                ("confirmed", True),
                ("confirmed", True),
                ("outcome", "default"),
            ],
            event_type="CS",
            keep_values=("CS Assignments", ""),
        )
        s.assert_order(
            "⚙️ **Canyon Storm Setup**",
            "Which time slot does **Team B** run for Canyon Storm?",
            "**Step 6 of 9: Mail Template**",
            "**Canyon Storm Mail Template: Team B**",
        )
        s.assert_never("Which time slot does **Team A**")
        s.assert_never("Do you want one template that applies to both teams")
        assert "`/canyonstorm` → **📄 Generate mail**" in s.texts[s.index_of("Step 5 of 9")]

        # CS rows always persist teams="both" (the strategy editor ignores it).
        row = config.get_storm_config(TEST_GUILD_ID, "CS")
        assert row["teams"] == "both"
        assert row["team_a_slot_index"] is None
        assert row["team_b_slot_index"] == 2
        assert (
            config.get_storm_config(TEST_GUILD_ID, "CS_B")["mail_template"] == DEFAULT_CS_TEMPLATE
        )
        assert not config.has_storm_config(TEST_GUILD_ID, "CS_A")
        assert config.get_config(TEST_GUILD_ID).cs_log_channel_id == 555001
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert fields["Teams"] == "B only"
        assert fields["Team Times"] == f"B: {labels[1]}"

    @pytest.mark.asyncio
    async def test_custom_reminder_dm_is_stored_default_is_blank(self, seeded_db):
        import config

        await _drive(
            [
                ("selected", "A"),
                ("selected", 1),
                ("confirmed", True),
                ("confirmed", True),
                ("outcome", "default"),
            ],
            keep_values=("DS Assignments", "Bring snacks, {name}"),
        )
        assert (
            config.get_storm_config(TEST_GUILD_ID, "DS")["dm_reminder_message"]
            == "Bring snacks, {name}"
        )

    @pytest.mark.asyncio
    async def test_participation_and_structured_results_are_saved_and_summarised(self, seeded_db):
        import config

        participation = {
            "enabled": 1,
            "tab_name": "DS Participation Log",
            "questions": [{"key": "vote_count", "label": "Vote Count", "type": "numeric"}],
            "roster_tab": "Squad Powers",
            "roster_name_col": 0,
            "roster_alias_col": -1,
            "roster_start_row": 2,
        }
        structured = _structured(
            structured_flow_enabled=True,
            power_metric_tab="Member Roster",
            power_metric_column="D",
            power_match_column="A",
            signup_channel_id=777001,
            signups_tab="DS Signups",
            rosters_tab="DS Rosters",
            attendance_tab="DS Attendance",
            roster_dm_starter_template="You start in {zone}",
        )
        s = await _drive(
            [
                ("selected", "A"),
                ("selected", 1),
                ("confirmed", True),
                ("confirmed", True),
                ("outcome", "default"),
                ("choice", "later"),  # the first sign-up offer
            ],
            participation=participation,
            structured=structured,
        )
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert (
            fields["Participation Tracking"]
            == "✅ Enabled · 1 question(s) · Tab: `DS Participation Log`"
        )
        assert fields["Structured Roster Flow"] == (
            "✅ Enabled · Power source: `Member Roster` · column `D` · matched by `A` · "
            "Sub mode: `pool` · Sign-up channel: <#777001>"
        )
        offer = s.texts[s.index_of("Want to post your first Desert Storm sign-up now?")]
        assert "<#777001>" in offer and "`/desertstorm`" in offer
        assert s.labels(len(s.views) - 1)[0] == "📣 Post my first sign-up now"

        assert config.get_participation_config(TEST_GUILD_ID, "DS")["enabled"] is True
        sc = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert sc["structured_flow_enabled"]
        assert sc["signup_channel_id"] == 777001
        assert (
            config.get_roster_dm_templates(TEST_GUILD_ID, "DS")["starter"] == "You start in {zone}"
        )

    @pytest.mark.asyncio
    async def test_no_first_signup_offer_once_one_was_posted(self, seeded_db):
        import config

        with config._get_conn() as conn:
            conn.execute(
                "INSERT INTO storm_registration_posts (guild_id, event_type, event_date, channel_id, message_id, posted_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (TEST_GUILD_ID, "DS", "2026-09-20", 1, 1, "2026-09-16T00:00:00+00:00"),
            )
            conn.commit()
        s = await _drive(
            [
                ("selected", "A"),
                ("selected", 1),
                ("confirmed", True),
                ("confirmed", True),
                ("outcome", "default"),
            ],
            structured=_structured(structured_flow_enabled=True, signup_channel_id=777001),
        )
        s.assert_never("Want to post your first")

    @pytest.mark.asyncio
    async def test_premium_lets_the_channel_pickers_include_threads(self, seeded_db):
        calls = []
        steps = [_channel_step(1), _channel_step(2)]

        def _record(*a, **kw):
            calls.append(kw)
            return steps.pop(0)

        with patch("wizard_steps.ChannelSelectStep", side_effect=_record):
            interaction = make_mock_interaction()
            Script(
                interaction.channel,
                [
                    ("selected", "A"),
                    ("selected", 1),
                    ("confirmed", True),
                    ("confirmed", True),
                    ("outcome", "default"),
                ],
            )
            with (
                patch(
                    "setup_cog._run_storm_participation_step",
                    AsyncMock(return_value=dict(PARTICIPATION_OFF)),
                ),
                patch(
                    "setup_cog._run_structured_flow_setup_step",
                    AsyncMock(return_value=_structured()),
                ),
                patch("premium.is_premium", AsyncMock(return_value=True)),
                patch_keep_or_change(["DS Assignments", ""]),
            ):
                await _wizard()(interaction, AsyncMock(), "DS")
        assert [c["include_threads"] for c in calls] == [True, True]
        assert [c["suggested_name"] for c in calls] == ["storm-log", "desert-storm"]
        assert [c["current_id"] for c in calls] == [0, 0]


# ── Re-entry ─────────────────────────────────────────────────────────────────


class TestReentry:
    @pytest.mark.asyncio
    async def test_summary_then_no_changes_leaves_everything(self, seeded_db):
        import config

        _save_existing("DS", teams="A")
        config.save_storm_team_slots(
            TEST_GUILD_ID, "DS", team_a_slot_index=2, team_b_slot_index=None
        )
        labels = config.get_storm_slot_labels("DS", TEST_GUILD_ID)

        s = await _drive([("proceed", False)])

        embed = [e for e in s.embeds if e is not None][0]
        assert embed.title == "⚔️ Current Desert Storm Setup"
        fields = [(f.name, f.value) for f in embed.fields]
        assert fields[:4] == [
            ("Sheet Tab", "DS Assignments"),
            ("Teams", "A only"),
            ("Team Times", f"Team A: {labels[1]}"),
            ("Log Channel", "<#555700>"),
        ]
        assert ("Structured Roster Flow", "❌ Off (preset tabs available on free tier)") in fields
        assert ("Reminder DM", "Default") in fields
        s.assert_never("⚙️ **Desert Storm Setup**")
        assert config.get_storm_config(TEST_GUILD_ID, "DS")["post_channel_id"] == 555800

    @pytest.mark.asyncio
    async def test_keep_current_everywhere_preserves_a_custom_template(self, seeded_db):
        import config

        custom = "Apex DS — Team A\n\n{zones}"
        _save_existing("DS", template=custom, teams="A")
        config.save_storm_config(
            TEST_GUILD_ID,
            "DS_A",
            tab_name="DS Assignments",
            mail_template=custom,
            timezone="America/New_York",
            log_channel_id=555700,
            post_channel_id=555800,
            teams="A",
        )
        config.save_storm_team_slots(
            TEST_GUILD_ID, "DS", team_a_slot_index=2, team_b_slot_index=None
        )
        labels = config.get_storm_slot_labels("DS", TEST_GUILD_ID)

        s = await _drive(
            [
                ("proceed", True),
                ("selected", "A"),  # Keep current sets the saved value
                ("selected", 2),
                ("confirmed", True),
                ("confirmed", True),
                ("outcome", "keep"),
            ],
            channel_steps=[_channel_step(555700), _channel_step(555800)],
        )
        assert s.labels(1) == [
            "Keep current: Team A only",
            "Team A & Team B",
            "Team A only",
            "Team B only",
        ]
        assert "Current: **Team A only**" in s.texts[s.index_of("Step 2 of 9")]
        assert s.labels(2) == [f"Keep current: {labels[1]}", labels[0], labels[1]]
        assert s.labels(5) == [
            "Keep current custom template",
            "↩️ Use default template",
            "✏️ Edit template",
        ]
        template_prompt = s.texts[s.index_of("Desert Storm Mail Template: Team A")]
        assert "Here is your saved custom template:" in template_prompt
        assert "keep your custom template, revert to the default, or edit it?" in template_prompt
        assert config.get_storm_config(TEST_GUILD_ID, "DS_A")["mail_template"] == custom

    @pytest.mark.asyncio
    async def test_shared_template_reentry_offers_keep_current_shared(self, seeded_db):
        import config

        body = "shared body {zones}"
        _save_existing("DS", template=body)
        for side in ("DS_A", "DS_B"):
            config.save_storm_config(
                TEST_GUILD_ID,
                side,
                tab_name="DS Assignments",
                mail_template=body,
                timezone="America/New_York",
                log_channel_id=555700,
                post_channel_id=555800,
            )
        s = await _drive(
            [
                ("proceed", True),
                ("selected", "both"),
                ("selected", 1),
                ("selected", 1),
                ("confirmed", True),
                ("confirmed", True),
                ("selected", "shared"),
                ("outcome", "keep"),
            ]
        )
        shared = s.view_named("SharedTemplateView")
        assert [c.label for c in shared.children] == [
            "Keep current: One shared template",
            "One template for both teams",
            "Separate templates per team",
        ]
        assert "Current: **One shared template**." in s.texts[s.index_of("Step 6 of 9")]
        assert config.get_storm_config(TEST_GUILD_ID, "DS_A")["mail_template"] == body
        assert config.get_storm_config(TEST_GUILD_ID, "DS_B")["mail_template"] == body

    @pytest.mark.asyncio
    async def test_separate_templates_reentry_offers_keep_current_separate(self, seeded_db):
        import config

        _save_existing("DS", template="A body")
        config.save_storm_config(
            TEST_GUILD_ID,
            "DS_A",
            tab_name="DS Assignments",
            mail_template="A body",
            timezone="America/New_York",
            log_channel_id=555700,
            post_channel_id=555800,
        )
        config.save_storm_config(
            TEST_GUILD_ID,
            "DS_B",
            tab_name="DS Assignments",
            mail_template="B body",
            timezone="America/New_York",
            log_channel_id=555700,
            post_channel_id=555800,
        )
        s = await _drive(
            [
                ("proceed", True),
                ("selected", "both"),
                ("selected", 1),
                ("selected", 1),
                ("confirmed", True),
                ("confirmed", True),
                ("selected", "separate"),
                ("outcome", "keep"),
                ("outcome", "keep"),
            ]
        )
        shared = s.view_named("SharedTemplateView")
        assert shared.children[0].label == "Keep current: Separate templates"
        assert config.get_storm_config(TEST_GUILD_ID, "DS_A")["mail_template"] == "A body"
        assert config.get_storm_config(TEST_GUILD_ID, "DS_B")["mail_template"] == "B body"

    @pytest.mark.asyncio
    async def test_stale_saved_channel_is_called_out(self, seeded_db):
        _save_existing("DS", teams="A")
        s = await _drive(
            [
                ("proceed", True),
                ("selected", "A"),
                ("selected", 1),
                ("confirmed", True),
                ("confirmed", True),
                ("outcome", "default"),
            ],
            channel_steps=[_channel_step(1, stale=True), _channel_step(2, stale=True)],
        )
        s.assert_order(
            PREV_CHANNEL_GONE.format(channel_label="Desert Storm log"),
            "**Step 4 of 9: Storm Log Channel**",
            PREV_CHANNEL_GONE.format(channel_label="Desert Storm mail post"),
            "**Step 5 of 9: Mail Post Channel**",
        )


# ── Cancel and timeout at every view ─────────────────────────────────────────


TIMEOUT = GENERIC_CMD_TIMEOUT.format(cmd=DS_CMD)
WALK = [
    ("selected", "both"),
    ("selected", 1),
    ("selected", 2),
    ("confirmed", True),
    ("confirmed", True),
    ("selected", "shared"),
    ("outcome", "default"),
]


class TestExits:
    @pytest.mark.parametrize("stop_at", [0, 1, 2, 5, 6])
    @pytest.mark.asyncio
    async def test_timeout_posts_the_route_back_and_saves_nothing(self, seeded_db, stop_at):
        import config

        answers = WALK[:stop_at] + ["timeout"]
        s = await _drive(answers)
        assert s.texts[-1] == TIMEOUT
        assert not config.has_storm_config(TEST_GUILD_ID, "DS")

    @pytest.mark.parametrize("stop_at", [0, 1, 2, 5, 6])
    @pytest.mark.asyncio
    async def test_cancel_exits_quietly_and_saves_nothing(self, seeded_db, stop_at):
        import config

        answers = WALK[:stop_at] + ["cancel"]
        s = await _drive(answers)
        s.assert_never(TIMEOUT)
        assert not config.has_storm_config(TEST_GUILD_ID, "DS")

    @pytest.mark.asyncio
    async def test_unconfirmed_channel_picker_is_a_timeout(self, seeded_db):
        import config

        s = await _drive(
            WALK[:3] + [("confirmed", False)],
            channel_steps=[_channel_step(1, confirmed=False), _channel_step(2)],
        )
        assert s.texts[-1] == TIMEOUT
        assert not config.has_storm_config(TEST_GUILD_ID, "DS")

    @pytest.mark.asyncio
    async def test_edit_reply_timeout_is_a_timeout(self, seeded_db):
        import config

        interaction = make_mock_interaction()
        bot = AsyncMock()
        bot.wait_for = AsyncMock(side_effect=asyncio.TimeoutError)
        s = Script(
            interaction.channel,
            [
                ("selected", "A"),
                ("selected", 1),
                ("confirmed", True),
                ("confirmed", True),
                ("outcome", "edit"),
            ],
        )
        steps = iter([_channel_step(1), _channel_step(2)])
        with (
            patch("wizard_steps.ChannelSelectStep", side_effect=lambda *a, **kw: next(steps)),
            patch(
                "setup_cog._run_storm_participation_step",
                AsyncMock(return_value=dict(PARTICIPATION_OFF)),
            ),
            patch(
                "setup_cog._run_structured_flow_setup_step", AsyncMock(return_value=_structured())
            ),
            patch("premium.is_premium", AsyncMock(return_value=False)),
            patch_keep_or_change(["DS Assignments", ""]),
        ):
            await _wizard()(interaction, bot, "DS")
        assert s.texts[-1] == TIMEOUT
        assert not config.has_storm_config(TEST_GUILD_ID, "DS")

    @pytest.mark.parametrize("answer", ["timeout", "cancel"])
    @pytest.mark.asyncio
    async def test_an_exit_releases_the_wizard_registration(self, seeded_db, answer):
        """A cancelled or timed-out walk no longer leaves its cancel event
        registered against the officer."""
        import wizard_registry

        wizard_registry._active.pop(123456789, None)
        await _drive([answer])
        assert not wizard_registry._active.get(123456789)

    @pytest.mark.asyncio
    async def test_a_finished_walk_releases_the_wizard_registration(self, seeded_db):
        import wizard_registry

        wizard_registry._active.pop(123456789, None)
        await _drive(WALK)
        assert not wizard_registry._active.get(123456789)

    @pytest.mark.asyncio
    async def test_tab_step_abort_stops_before_any_prompt(self, seeded_db):
        import config

        s = await _drive([], keep_values=(None,))
        assert s.texts == ["⚙️ **Desert Storm Setup**"]
        assert not config.has_storm_config(TEST_GUILD_ID, "DS")

    @pytest.mark.asyncio
    async def test_participation_step_abort_stops_the_walk(self, seeded_db):
        import config

        interaction = make_mock_interaction()
        s = Script(interaction.channel, list(WALK))
        steps = iter([_channel_step(1), _channel_step(2)])
        with (
            patch("wizard_steps.ChannelSelectStep", side_effect=lambda *a, **kw: next(steps)),
            patch("setup_cog._run_storm_participation_step", AsyncMock(return_value=None)),
            patch("setup_cog._run_structured_flow_setup_step", AsyncMock()) as structured,
            patch("premium.is_premium", AsyncMock(return_value=False)),
            patch_keep_or_change(["DS Assignments", ""]),
        ):
            await _wizard()(interaction, AsyncMock(), "DS")
        structured.assert_not_awaited()
        assert not config.has_storm_config(TEST_GUILD_ID, "DS")
