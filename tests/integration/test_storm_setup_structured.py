"""
Characterization tests for the structured-flow block of the storm setup
wizard (`setup_cog._run_structured_flow_setup_step`, moved to
`storm_setup_structured.run_structured_flow_step` in the #589 step 11
refactor).

Every existing wizard test stubs this block out, so before the refactor
the block's own body had no coverage. These tests were written against
the pre-refactor function and pass unchanged against the refactored one:
they drive the real views the block posts, in order, and assert on the
returned config dict, the order of the prompts, and the button labels
officers see. Nothing here knows the block's internal shape beyond the
attribute each view answers through (the sub mode view's became
`outcome` when it joined the shared `_ChoiceView`); what it posts and
what it returns are the contract.

The `Script` harness answers each view the block posts, in order. An
entry is `(attribute, value)` to set on the view before stopping it,
`"timeout"` to stop it with nothing set, or `"cancel"` to fire the
wizard's cancel event while the view is waiting.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# setup_cog re-exports the wizard pieces by name at import time. Import it
# before any test patches `wizard_steps.X`, or the first import inside a
# patched block binds the mock into setup_cog for the rest of the run.
import setup_cog  # noqa: F401

from tests.conftest import TEST_GUILD_ID, make_mock_channel, make_mock_user
from tests.integration.test_setup_flows import patch_keep_or_change
from messages import GENERIC_CMD_TIMEOUT

CMD = "setup → ⚔️ Desert Storm"
TIMEOUT_MSG = GENERIC_CMD_TIMEOUT.format(cmd=CMD)


def _run_step():
    """Resolve the function under test at call time so the same tests run
    before and after the move out of `setup_cog`."""
    import setup_cog

    return setup_cog._run_structured_flow_setup_step


class Script:
    """Answer each posted view in order; record everything sent."""

    def __init__(self, channel, cancel_event, answers):
        self.answers = list(answers)
        self.sent = []
        self.views = []
        self.cancel_event = cancel_event
        channel.send = AsyncMock(side_effect=self._send)

    async def _send(self, content=None, embed=None, view=None, **kw):
        self.sent.append(content)
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

    def labels(self, view_index):
        return [c.label for c in self.views[view_index].children]


def _modal(**fields):
    """A stand-in for a submitted modal: `modal.<name>.value`."""
    return SimpleNamespace(**{k: SimpleNamespace(value=v) for k, v in fields.items()})


def _channel_step(channel_id=555, stale=False):
    return MagicMock(
        confirmed=True,
        cancelled=False,
        selected_channel=MagicMock(id=channel_id),
        is_current_stale=stale,
        wait=AsyncMock(),
    )


def _current(**overrides):
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
        "strategies_tab": "",
        "member_rules_tab": "",
        "poll_day_of_week": -1,
        "signup_time": "",
        "power_refresh_dm_enabled": False,
        "power_last_updated_tab": "",
        "power_last_updated_column": "",
        "power_last_updated_match_column": "",
        "power_refresh_stale_days": 0,
    }
    base.update(overrides)
    return base


async def _schedule(*a, **kw):
    return {"dow": 2, "time": "18:00"}


class _Harness:
    """Common patches: no saved storm config unless a test says so, Member
    Sync off, no presets or rules, the schedule sub-flow answered, and
    the channel picker confirmed."""

    def __init__(
        self,
        *,
        has_config=False,
        presets=None,
        rules=None,
        roster_cfg=None,
        dm_templates=None,
        channel_step=None,
        schedule=_schedule,
    ):
        self.schedule = schedule
        self.has_config = has_config
        self.presets = presets if presets is not None else []
        self.rules = rules if rules is not None else []
        self.roster_cfg = roster_cfg or {"enabled": False}
        self.dm_templates = dm_templates or {"starter": "", "paired_sub": "", "pool_sub": ""}
        self.channel_step = channel_step or _channel_step()

    def __enter__(self):
        self._patches = [
            patch("config.has_storm_config", return_value=self.has_config),
            patch("config.get_member_roster_config", return_value=self.roster_cfg),
            patch("config.get_roster_dm_templates", return_value=self.dm_templates),
            patch("storm_strategy.list_presets", return_value=self.presets),
            patch("storm_member_rules.list_rules", return_value=self.rules),
            patch("wizard_time._ask_signup_schedule", side_effect=self.schedule),
            patch("wizard_steps.ChannelSelectStep", return_value=self.channel_step),
        ]
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._patches):
            p.stop()
        return False


async def _drive(
    answers, *, is_premium=True, current=None, keep_values=None, harness=None, bot=None
):
    """Run the block once. Returns (result, script)."""
    channel = make_mock_channel()
    channel.guild = MagicMock(id=TEST_GUILD_ID)
    user = make_mock_user(123456789)
    cancel_event = asyncio.Event()
    bot = bot or AsyncMock()
    script = Script(channel, cancel_event, answers)
    current = current if current is not None else _current()
    harness = harness or _Harness()
    with harness, patch_keep_or_change(list(keep_values or [])):
        result = await _run_step()(
            channel,
            bot,
            user,
            cancel_event,
            guild_id=TEST_GUILD_ID,
            event_type="DS",
            label="Desert Storm",
            cmd_name=CMD,
            is_premium_flag=is_premium,
            current={},
            current_structured=current,
            interaction_guild=MagicMock(id=TEST_GUILD_ID),
        )
    return result, script


FULL_TABS = ["DS Signups", "DS Rosters", "DS Attendance", "DS Strategies", "DS Member Rules"]


# ── Gate ─────────────────────────────────────────────────────────────────────


class TestGate:
    @pytest.mark.asyncio
    async def test_free_tier_returns_current_values_and_asks_nothing(self, seeded_db):
        current = _current(strategies_tab="Kept", power_metric_column="D")
        result, script = await _drive([], is_premium=False, current=current)
        assert script.sent == []
        assert result["structured_flow_enabled"] is False
        assert result["strategies_tab"] == "Kept"
        assert result["power_metric_column"] == "D"
        # Fields the current dict didn't carry are seeded with their defaults.
        assert result["roster_dm_starter_template"] == ""
        assert result["power_refresh_stale_days"] == 0

    @pytest.mark.asyncio
    async def test_current_dict_is_not_mutated(self, seeded_db):
        current = _current()
        snapshot = dict(current)
        await _drive([], is_premium=False, current=current)
        assert current == snapshot

    @pytest.mark.asyncio
    async def test_premium_first_run_decline(self, seeded_db):
        current = _current(strategies_tab="Kept")
        result, script = await _drive([("selected", False)], current=current)
        assert result["structured_flow_enabled"] is False
        assert result["strategies_tab"] == "Kept"
        assert "Step 8 of 9" in script.texts[0]
        assert "💎 Premium" in script.texts[0]
        assert script.labels(0) == ["Yes", "No"]
        assert len(script.views) == 1

    @pytest.mark.asyncio
    async def test_premium_reentry_uses_keep_or_flip_gate(self, seeded_db):
        current = _current(structured_flow_enabled=True)
        harness = _Harness(has_config=True)
        result, script = await _drive([("value", False)], current=current, harness=harness)
        assert result["structured_flow_enabled"] is False
        assert script.labels(0) == ["Keep current: Yes", "↩️ Switch to: No"]

    @pytest.mark.asyncio
    async def test_cancel_is_silent(self, seeded_db):
        result, script = await _drive(["cancel"])
        assert result is None
        assert TIMEOUT_MSG not in script.texts

    @pytest.mark.asyncio
    async def test_timeout_posts_the_route_back(self, seeded_db):
        result, script = await _drive(["timeout"])
        assert result is None
        assert script.texts[-1] == TIMEOUT_MSG


# ── Full opt-in walk ─────────────────────────────────────────────────────────


HAPPY = [
    ("selected", True),  # opt in
    ("outcome", "default"),  # power data source
    ("outcome", "pool"),  # sub mode
    ("confirmed", True),  # sign-up channel (patched step)
    ("selected", False),  # power-refresh DM
    ("outcome", "default"),  # DM template: Starter
    ("outcome", "default"),  # DM template: Paired Sub
    ("outcome", "default"),  # DM template: Pool Sub
    ("choice", None),  # first-preset offer
    ("choice", None),  # first-rule offer
]


class TestFullWalk:
    @pytest.mark.asyncio
    async def test_first_run_defaults(self, seeded_db):
        result, script = await _drive(HAPPY, keep_values=FULL_TABS)
        assert result == {
            "structured_flow_enabled": True,
            "power_metric_column": "B",
            "power_metric_tab": "",
            "power_match_column": "",
            "sub_mode": "pool",
            "signup_channel_id": 555,
            "signup_schedule_cron": "",
            "signups_tab": "DS Signups",
            "rosters_tab": "DS Rosters",
            "attendance_tab": "DS Attendance",
            "strategies_tab": "DS Strategies",
            "member_rules_tab": "DS Member Rules",
            "poll_day_of_week": 2,
            "signup_time": "18:00",
            "power_refresh_dm_enabled": False,
            "power_last_updated_tab": "",
            "power_last_updated_column": "",
            "power_last_updated_match_column": "",
            "power_refresh_stale_days": 0,
            "roster_dm_starter_template": "",
            "roster_dm_paired_sub_template": "",
            "roster_dm_pool_sub_template": "",
        }
        script.assert_order(
            "Step 8 of 9",
            "**Power Data Source**",
            "**Sub Mode**",
            "Sign-Up Channel",
            "Power-Refresh DM",
            "Roster DM Templates",
            "Roster DM Template: Starter",
            "Roster DM Template: Paired Sub",
            "Roster DM Template: Pool Sub",
            "**Strategy Presets**",
            "first Desert Storm preset",
            "**Member Rules**",
            "first Desert Storm rule",
        )
        assert script.answers == []

    @pytest.mark.asyncio
    async def test_first_run_button_labels(self, seeded_db):
        # `sub_mode` is "" here: the config reader seeds "pool" even for a
        # guild that never ran the wizard, and any saved mode reads as
        # "Use Current". See TestSubMode for that path.
        _, script = await _drive(HAPPY, keep_values=FULL_TABS, current=_current(sub_mode=""))
        # Power source: first run drops Keep current and names the defaults.
        assert script.labels(1) == [
            "✅ Use defaults: Member Roster · B · matched by A",
            "✏️ Define my own",
        ]
        assert "Member Sync isn't enabled yet" in script.texts[script.index_of("Power Data Source")]
        # Sub mode: no saved value reads "Use Default".
        assert script.labels(2) == ["Use Default: Pool", "Paired: primary↔sub pairs"]
        # DM templates: first run drops Keep and promotes Use default.
        assert script.labels(5) == ["↩️ Use default template", "✏️ Edit template"]
        assert (
            "Would you like to use this default or write your own?"
            in script.texts[script.index_of("Roster DM Template: Starter")]
        )

    @pytest.mark.asyncio
    async def test_existing_presets_and_rules_skip_the_offers(self, seeded_db):
        answers = HAPPY[:-2]
        harness = _Harness(presets=[{"name": "Standard"}], rules=[{"id": 1}])
        result, script = await _drive(answers, keep_values=FULL_TABS, harness=harness)
        assert result is not None
        assert not any("preset now" in t for t in script.texts)
        assert not any("rule now" in t for t in script.texts)

    @pytest.mark.asyncio
    async def test_offer_lookup_failure_still_offers(self, seeded_db):
        harness = _Harness()
        with patch("storm_strategy.list_presets", side_effect=RuntimeError("sheet")):
            result, script = await _drive(HAPPY, keep_values=FULL_TABS, harness=harness)
        assert result is not None
        script.index_of("first Desert Storm preset")

    @pytest.mark.asyncio
    async def test_member_sync_drives_power_defaults(self, seeded_db):
        harness = _Harness(
            roster_cfg={"enabled": True, "tab_name": "Roster", "discord_id_col": 3},
        )
        answers = list(HAPPY)
        answers[1] = ("outcome", "default")
        result, script = await _drive(answers, keep_values=FULL_TABS, harness=harness)
        assert script.labels(1)[0] == "✅ Use defaults: Roster · B · matched by D"
        assert "Member Sync is enabled" in script.texts[script.index_of("Power Data Source")]
        assert result["power_metric_tab"] == ""
        assert result["power_match_column"] == ""

    @pytest.mark.asyncio
    async def test_stale_signup_channel_warns(self, seeded_db):
        harness = _Harness(channel_step=_channel_step(stale=True))
        result, script = await _drive(HAPPY, keep_values=FULL_TABS, harness=harness)
        assert result["signup_channel_id"] == 555
        script.index_of("sign-up channel no longer exists")

    @pytest.mark.asyncio
    async def test_schedule_cancel_propagates(self, seeded_db):
        async def cancelled(*a, **kw):
            return None

        result, script = await _drive(HAPPY[:4], harness=_Harness(schedule=cancelled))
        assert result is None

    @pytest.mark.asyncio
    async def test_tab_cancel_propagates(self, seeded_db):
        result, _ = await _drive(HAPPY[:4], keep_values=[None])
        assert result is None


# ── Power Data Source ────────────────────────────────────────────────────────


class TestPowerDataSource:
    @pytest.mark.asyncio
    async def test_edit_stores_values(self, seeded_db):
        answers = list(HAPPY)
        answers[1] = ("outcome", "edit")
        script_holder = {}

        # The picker reads its modal after the view stops, so plant it
        # through a wrapper answer.
        async def run():
            channel = make_mock_channel()
            channel.guild = MagicMock(id=TEST_GUILD_ID)
            cancel_event = asyncio.Event()
            script = Script(channel, cancel_event, answers)
            orig = script._send

            async def send(content=None, embed=None, view=None, **kw):
                if view is not None and len(script.views) == 1:
                    view.modal = _modal(tab_input="Squad Powers", col_input="c", match_input="a")
                return await orig(content=content, embed=embed, view=view, **kw)

            channel.send = AsyncMock(side_effect=send)
            script_holder["s"] = script
            with _Harness(), patch_keep_or_change(FULL_TABS):
                return await _run_step()(
                    channel,
                    AsyncMock(),
                    make_mock_user(1),
                    cancel_event,
                    guild_id=TEST_GUILD_ID,
                    event_type="DS",
                    label="Desert Storm",
                    cmd_name=CMD,
                    is_premium_flag=True,
                    current={},
                    current_structured=_current(),
                    interaction_guild=MagicMock(),
                )

        result = await run()
        assert result["power_metric_tab"] == "Squad Powers"
        assert result["power_metric_column"] == "C"
        assert result["power_match_column"] == "A"

    @pytest.mark.asyncio
    async def test_edit_with_member_roster_tab_and_bad_letters_falls_back(self, seeded_db):
        answers = list(HAPPY)
        answers[1] = ("outcome", "edit")
        result, _ = await _drive_with_modal(
            answers, 1, _modal(tab_input="Member Roster", col_input="12", match_input="zz")
        )
        assert result["power_metric_tab"] == ""
        assert result["power_metric_column"] == "B"
        assert result["power_match_column"] == ""

    @pytest.mark.asyncio
    async def test_keep_current_when_customised(self, seeded_db):
        current = _current(
            power_metric_tab="Powers", power_metric_column="C", power_match_column="A"
        )
        answers = list(HAPPY)
        answers[1] = ("outcome", "keep")
        result, script = await _drive(answers, current=current, keep_values=FULL_TABS)
        assert script.labels(1) == [
            "Keep current: Powers · C · matched by A",
            "✏️ Define my own",
        ]
        assert result["power_metric_tab"] == "Powers"
        assert result["power_metric_column"] == "C"
        assert result["power_match_column"] == "A"

    @pytest.mark.asyncio
    async def test_edit_dismissed_is_a_timeout(self, seeded_db):
        answers = list(HAPPY)
        answers[1] = "timeout"
        result, script = await _drive(answers, keep_values=FULL_TABS)
        assert result is None
        assert script.texts[-1] == TIMEOUT_MSG


async def _drive_with_modal(answers, view_index, modal, **kw):
    """Like `_drive`, but plants `modal` on the view at `view_index` before
    it is answered, for the picker-with-modal steps."""
    channel = make_mock_channel()
    channel.guild = MagicMock(id=TEST_GUILD_ID)
    cancel_event = asyncio.Event()
    script = Script(channel, cancel_event, answers)
    orig = script._send
    modals = modal if isinstance(modal, dict) else {view_index: modal}

    async def send(content=None, embed=None, view=None, **kw2):
        if view is not None and len(script.views) in modals:
            view.modal = modals[len(script.views)]
        return await orig(content=content, embed=embed, view=view, **kw2)

    channel.send = AsyncMock(side_effect=send)
    harness = kw.pop("harness", None) or _Harness()
    current = kw.pop("current", None) or _current()
    bot = kw.pop("bot", None) or AsyncMock()
    keep_values = kw.pop("keep_values", FULL_TABS)
    with harness, patch_keep_or_change(list(keep_values)):
        result = await _run_step()(
            channel,
            bot,
            make_mock_user(1),
            cancel_event,
            guild_id=TEST_GUILD_ID,
            event_type="DS",
            label="Desert Storm",
            cmd_name=CMD,
            is_premium_flag=True,
            current={},
            current_structured=current,
            interaction_guild=MagicMock(),
        )
    return result, script


# ── Sub mode ─────────────────────────────────────────────────────────────────


class TestSubMode:
    @pytest.mark.asyncio
    async def test_paired_saved_reads_use_current(self, seeded_db):
        current = _current(sub_mode="paired")
        answers = list(HAPPY)
        answers[2] = ("outcome", "paired")
        result, script = await _drive(answers, current=current, keep_values=FULL_TABS)
        assert script.labels(2) == ["Pool: flat sub list", "Use Current: Paired"]
        assert result["sub_mode"] == "paired"

    @pytest.mark.asyncio
    async def test_pool_saved_reads_use_current(self, seeded_db):
        current = _current(sub_mode="pool")
        _, script = await _drive(HAPPY, current=current, keep_values=FULL_TABS)
        assert script.labels(2)[0] == "Use Current: Pool"

    @pytest.mark.asyncio
    async def test_unknown_saved_mode_reads_default(self, seeded_db):
        current = _current(sub_mode="weird")
        _, script = await _drive(HAPPY, current=current, keep_values=FULL_TABS)
        assert script.labels(2)[0] == "Use Default: Pool"


# ── Power-refresh DM + stale power ───────────────────────────────────────────


STALE_ON = [
    ("selected", True),  # opt in
    ("outcome", "default"),  # power source
    ("outcome", "pool"),
    ("confirmed", True),
    ("selected", True),  # power-refresh DM on
    ("selected", True),  # stale DM on
]
TAIL = [
    ("outcome", "default"),
    ("outcome", "default"),
    ("outcome", "default"),
    ("choice", None),
    ("choice", None),
]


class TestStalePower:
    @pytest.mark.asyncio
    async def test_reentry_gate_for_power_refresh(self, seeded_db):
        current = _current(structured_flow_enabled=True, power_refresh_dm_enabled=True)
        harness = _Harness(has_config=True)
        answers = [("value", True)] + HAPPY[1:4] + [("value", False)] + HAPPY[5:]
        result, script = await _drive(
            answers, current=current, keep_values=FULL_TABS, harness=harness
        )
        assert result["power_refresh_dm_enabled"] is False
        i = script.index_of("Power-Refresh DM")
        assert "Currently **on**" in script.texts[i]
        assert script.labels(4) == ["Keep current: Yes", "↩️ Switch to: No"]

    @pytest.mark.asyncio
    async def test_stale_off_wipes_saved_source(self, seeded_db):
        current = _current(
            power_refresh_stale_days=9,
            power_last_updated_tab="X",
            power_last_updated_column="N",
            power_last_updated_match_column="A",
        )
        answers = STALE_ON[:5] + [("selected", False)] + TAIL
        result, _ = await _drive(answers, current=current, keep_values=FULL_TABS)
        assert result["power_refresh_dm_enabled"] is True
        assert result["power_refresh_stale_days"] == 0
        assert result["power_last_updated_tab"] == ""
        assert result["power_last_updated_column"] == ""
        assert result["power_last_updated_match_column"] == ""

    @pytest.mark.asyncio
    async def test_days_edit_retries_then_accepts(self, seeded_db):
        answers = (
            STALE_ON
            + [
                ("outcome", "edit"),  # days: "two weeks"
                ("outcome", "edit"),  # days: "0"
                ("outcome", "edit"),  # days: "10"
                ("outcome", "edit"),  # last-updated source
            ]
            + TAIL
        )
        modals = {
            6: _modal(days_input="two weeks"),
            7: _modal(days_input="0"),
            8: _modal(days_input="10"),
            9: _modal(tab_input="Powers", col_input="n", match_input=""),
        }
        result, script = await _drive_with_modal(answers, None, modals)
        assert result["power_refresh_stale_days"] == 10
        assert result["power_last_updated_tab"] == "Powers"
        assert result["power_last_updated_column"] == "N"
        assert result["power_last_updated_match_column"] == ""
        assert sum("Couldn't parse that" in t for t in script.texts) == 2
        # First-run days picker: no Keep, Set days names the default.
        assert script.labels(6) == ["✏️ Set days (default: 7)"]
        assert script.labels(9) == ["✏️ Set source: Member Roster"]

    @pytest.mark.asyncio
    async def test_days_three_failures_give_up(self, seeded_db):
        answers = STALE_ON + [("outcome", "edit")] * 3
        modals = {6: _modal(days_input="x"), 7: _modal(days_input="y"), 8: _modal(days_input="z")}
        result, script = await _drive_with_modal(answers, None, modals)
        assert result is None
        assert "after 3 tries" in script.texts[-1]

    @pytest.mark.asyncio
    async def test_days_keep_on_reentry(self, seeded_db):
        current = _current(
            structured_flow_enabled=True,
            power_refresh_dm_enabled=True,
            power_refresh_stale_days=5,
            power_last_updated_tab="Powers",
            power_last_updated_column="N",
        )
        harness = _Harness(has_config=True)
        answers = (
            [("value", True), ("outcome", "default"), ("outcome", "pool"), ("confirmed", True)]
            + [("value", True), ("value", True)]  # refresh gate, stale gate
            + [("outcome", "keep"), ("outcome", "keep")]  # days, source
            + TAIL
        )
        result, script = await _drive(
            answers, current=current, keep_values=FULL_TABS, harness=harness
        )
        assert result["power_refresh_stale_days"] == 5
        assert result["power_last_updated_tab"] == "Powers"
        assert script.labels(6) == ["Keep current: 5 days", "✏️ Set days"]
        assert script.labels(7) == ["Keep: Powers · N", "✏️ Define source"]
        assert "Currently **on** at **5** days" in script.texts[script.index_of("Stale-Power DM")]

    @pytest.mark.asyncio
    async def test_source_cleared_disables_stale(self, seeded_db):
        answers = STALE_ON + [("outcome", "edit"), ("outcome", "edit")] + TAIL
        modals = {
            6: _modal(days_input="7"),
            7: _modal(tab_input="", col_input="", match_input=""),
        }
        result, script = await _drive_with_modal(answers, None, modals)
        assert result["power_refresh_stale_days"] == 0
        assert result["power_last_updated_tab"] == ""
        script.index_of("Tab + column are both required")

    @pytest.mark.asyncio
    async def test_survey_shortcut_skips_the_source_picker(self, seeded_db):
        current = _current(power_metric_tab="Squad Powers")
        sheet = MagicMock()
        sheet.worksheet.return_value.row_values.return_value = ["Name", "Power", "Date Modified"]
        answers = [STALE_ON[0], ("outcome", "keep")] + STALE_ON[2:] + [("outcome", "edit")] + TAIL
        modals = {6: _modal(days_input="7")}
        with patch("config.get_spreadsheet", return_value=sheet):
            result, script = await _drive_with_modal(answers, None, modals, current=current)
        assert result["power_last_updated_tab"] == "Squad Powers"
        assert result["power_last_updated_column"] == "C"
        assert result["power_last_updated_match_column"] == ""
        assert result["power_refresh_stale_days"] == 7
        script.index_of("Auto-detected the survey's **Date Modified** column (`C`)")
        assert not any("Last-Updated Source" in t for t in script.texts)

    @pytest.mark.asyncio
    async def test_survey_header_without_date_column_falls_through(self, seeded_db):
        current = _current(power_metric_tab="Squad Powers")
        sheet = MagicMock()
        sheet.worksheet.return_value.row_values.return_value = ["Name", "Power"]
        answers = (
            [STALE_ON[0], ("outcome", "keep")]
            + STALE_ON[2:]
            + [("outcome", "edit"), ("outcome", "edit")]
            + TAIL
        )
        modals = {
            6: _modal(days_input="7"),
            7: _modal(tab_input="Squad Powers", col_input="D", match_input="A"),
        }
        with patch("config.get_spreadsheet", return_value=sheet):
            result, script = await _drive_with_modal(answers, None, modals, current=current)
        assert result["power_last_updated_column"] == "D"
        assert result["power_last_updated_match_column"] == "A"
        script.index_of("Last-Updated Source")

    @pytest.mark.asyncio
    async def test_survey_read_failure_falls_through(self, seeded_db):
        current = _current(power_metric_tab="Squad Powers")
        answers = (
            [STALE_ON[0], ("outcome", "keep")]
            + STALE_ON[2:]
            + [("outcome", "edit"), ("outcome", "edit")]
            + TAIL
        )
        modals = {
            6: _modal(days_input="7"),
            7: _modal(tab_input="Squad Powers", col_input="D", match_input=""),
        }
        with patch("config.get_spreadsheet", side_effect=RuntimeError("quota")):
            result, script = await _drive_with_modal(answers, None, modals, current=current)
        assert result["power_last_updated_column"] == "D"
        script.index_of("Last-Updated Source")


# ── Roster DM templates ──────────────────────────────────────────────────────


class TestRosterDmTemplates:
    @pytest.mark.asyncio
    async def test_keep_custom_and_revert(self, seeded_db):
        harness = _Harness(dm_templates={"starter": "Mine", "paired_sub": "Pair", "pool_sub": ""})
        answers = HAPPY[:5] + [("outcome", "keep"), ("outcome", "default"), ("outcome", "default")]
        answers += HAPPY[8:]
        result, script = await _drive(answers, keep_values=FULL_TABS, harness=harness)
        assert result["roster_dm_starter_template"] == "Mine"
        assert result["roster_dm_paired_sub_template"] == ""
        assert result["roster_dm_pool_sub_template"] == ""
        assert script.labels(5) == [
            "Keep current custom template",
            "↩️ Use default template",
            "✏️ Edit template",
        ]
        starter_prompt = script.texts[script.index_of("Roster DM Template: Starter")]
        assert "Here is your saved custom template:" in starter_prompt
        assert "keep your custom template, revert to the default, or edit it?" in starter_prompt

    @pytest.mark.asyncio
    async def test_edit_reads_the_next_chat_message(self, seeded_db):
        bot = AsyncMock()
        bot.wait_for = AsyncMock(return_value=MagicMock(content="  Hello {name}  "))
        answers = HAPPY[:5] + [("outcome", "edit")] + HAPPY[6:]
        result, script = await _drive(answers, keep_values=FULL_TABS, bot=bot)
        assert result["roster_dm_starter_template"] == "Hello {name}"
        script.index_of("Paste your custom Starter DM template")
        script.index_of("**Available placeholders:**")

    @pytest.mark.asyncio
    async def test_edit_blank_reply_falls_back_to_default(self, seeded_db):
        from defaults import DEFAULT_ROSTER_DM_STARTER

        bot = AsyncMock()
        bot.wait_for = AsyncMock(return_value=MagicMock(content="   "))
        answers = HAPPY[:5] + [("outcome", "edit")] + HAPPY[6:]
        result, _ = await _drive(answers, keep_values=FULL_TABS, bot=bot)
        assert result["roster_dm_starter_template"] == DEFAULT_ROSTER_DM_STARTER

    @pytest.mark.asyncio
    async def test_edit_timeout(self, seeded_db):
        bot = AsyncMock()
        bot.wait_for = AsyncMock(side_effect=asyncio.TimeoutError)
        answers = HAPPY[:5] + [("outcome", "edit")]
        result, script = await _drive(answers, keep_values=FULL_TABS, bot=bot)
        assert result is None
        assert script.texts[-1] == TIMEOUT_MSG

    @pytest.mark.asyncio
    async def test_choice_timeout(self, seeded_db):
        answers = HAPPY[:5] + ["timeout"]
        result, script = await _drive(answers, keep_values=FULL_TABS)
        assert result is None
        assert script.texts[-1] == TIMEOUT_MSG


# ── Preset library ───────────────────────────────────────────────────────────


class TestPresetLibrary:
    @pytest.mark.asyncio
    async def test_offer_cancel_returns_none(self, seeded_db):
        answers = HAPPY[:8] + ["cancel"]
        result, _ = await _drive(answers, keep_values=FULL_TABS)
        assert result is None

    @pytest.mark.asyncio
    async def test_offer_timeout_still_completes(self, seeded_db):
        answers = HAPPY[:8] + ["timeout", "timeout"]
        result, script = await _drive(answers, keep_values=FULL_TABS)
        assert result is not None
        assert TIMEOUT_MSG not in script.texts

    @pytest.mark.asyncio
    async def test_offers_point_at_the_hub(self, seeded_db):
        _, script = await _drive(HAPPY, keep_values=FULL_TABS)
        assert "`/desertstorm` → **" in script.texts[script.index_of("first Desert Storm preset")]
        assert "`/desertstorm` → **" in script.texts[script.index_of("first Desert Storm rule")]
        assert script.views[8].owner_id == 123456789
