"""
Characterization tests for the Profession Buddy System wizard
(`setup_cog.run_buddy_setup`, #289), written against the function before its
move to `buddy_setup.py` (#611 round 2) and kept unchanged after it. Until
now the wizard had no direct tests.

The scripted harness answers each view the wizard posts, in order
(`(attribute, value)`, `"timeout"` or `"cancel"`); `ask_keep_or_change` is
replaced by a recorder in both of its homes so every keep-or-change prompt,
default and current is asserted too; the channel picker is a confirmed
stand-in, patched in both homes for the same reason.
"""

import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# setup_cog re-exports the wizard pieces by name at import time; import it
# before anything patches `wizard_steps.X` (see CLAUDE.md, test gotchas).
import setup_cog  # noqa: F401

from tests.conftest import TEST_GUILD_ID, make_mock_interaction

TIMEOUT = "⏰ Setup timed out. Run `/setup` → 🤝 Buddy System to start again."
USER_ID = 123456789


def _wizard():
    import setup_cog

    return setup_cog.run_buddy_setup


class Script:
    def __init__(self, channel, answers):
        self.answers = list(answers)
        self.sent = []
        self.embeds = []
        self.views = []
        self.cancel_event = None
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


def _channel_step(channel_id=777001, *, stale=False, confirmed=True):
    return MagicMock(
        confirmed=confirmed,
        cancelled=False,
        selected_channel=MagicMock(id=channel_id),
        is_current_stale=stale,
        wait=AsyncMock(),
    )


async def _drive(
    answers,
    *,
    keep=(),
    channel_step=None,
    is_premium=False,
    survey=None,
    roster=None,
    has_roster=None,
    storm=None,
):
    """Run the wizard once. Returns (script, recorder); saved state is read
    back from the seeded database by each test."""
    interaction = make_mock_interaction()
    script = Script(interaction.channel, answers)
    step = channel_step or _channel_step()

    import wizard_registry

    real_register = wizard_registry.register

    def _register(user_id):
        ev = real_register(user_id)
        script.cancel_event = ev
        return ev

    patches = [
        patch("setup_cog.ChannelSelectStep", return_value=step),
        patch("wizard_steps.ChannelSelectStep", return_value=step),
        patch("premium.is_premium", AsyncMock(return_value=is_premium)),
        patch("wizard_registry.register", side_effect=_register),
    ]
    if survey is not None:
        patches.append(patch("config.get_survey_config", return_value=survey))
    if roster is not None:
        patches.append(patch("config.get_member_roster_config", return_value=roster))
    if has_roster is not None:
        patches.append(patch("config.has_member_roster_config", return_value=has_roster))
    if storm is not None:
        patches.append(patch("config.get_storm_config", return_value=storm))
    with KeepRecorder(keep) as rec:
        for p in patches:
            p.start()
        try:
            await _wizard()(interaction, AsyncMock())
        finally:
            for p in reversed(patches):
                p.stop()
    return script, rec


def _saved(**fields):
    import config

    base = dict(
        enabled=1,
        buddy_tab="Pairs",
        preset_tab="Sets",
        include_col_header="",
        roster_filter_enabled=0,
        engineer_doubling=1,
        scarcity_priority="strongest_first",
        reliability_enabled=0,
        reliability_tab="",
        reliability_column="",
        notify_channel_id=0,
        dm_enabled=0,
        dm_template="",
    )
    base.update(fields)
    for k, v in base.items():
        config.update_buddy_config_field(TEST_GUILD_ID, k, v)
    return base


def _buddy():
    import config

    return config.get_buddy_config(TEST_GUILD_ID)


PROFESSION_SURVEY = {
    "tab_squad_powers": "Powers",
    "questions": [{"key": "profession", "label": "Your profession"}],
}
ROSTER_HAND = {"enabled": 0, "tab_name": "Members"}
ROSTER_SYNCED = {"enabled": 1, "tab_name": "Members"}
STORM_WITH_POWER = {"power_metric_tab": "Powers", "structured_flow_enabled": 0}

# The shortest walk that turns the system on: every optional question answered No.
MINIMAL = [
    ("selected", True),  # Step 1 enable
    ("selected", False),  # Step 4 opt-out column
    ("selected", False),  # Step 6 doubling
    ("selected", False),  # Step 8 reliability
    ("selected", False),  # Step 9 alerts
    ("selected", False),  # Step 10 DMs
]


class TestFreshMinimal:
    @pytest.mark.asyncio
    async def test_prompts_in_order_with_every_optional_skipped(self, seeded_db):
        s, rec = await _drive(MINIMAL)
        s.assert_order(
            "🤝 **Profession Buddy System Setup**\nPair your War Leaders with Engineers so the daily buff Skill always has a home.",
            "**Step 1 of 10 — Turn on the Profession Buddy System?**",
            "⚠️ I couldn't find a **Profession** question in your Squad Power Survey.",
            "**Step 4 of 10 — Leave people out of the buddy list?**",
            "**Step 5 of 10 — Keep the list in step with your roster** *(skipped)*",
            "**Step 6 of 10 — Two Engineers per War Leader?**",
            "ℹ️ *Skipping the strongest-first option:",
            "**Step 8 of 10 — Rank Engineers by reliability?**",
            "**Step 9 of 10 — Leadership alerts**",
            "**Step 10 of 10 — Buddy DMs**",
        )
        assert (
            "via `/setup` → 📋 Survey. You can still finish this setup"
            in s.sent[s.index_of("couldn't find")]
        )
        assert (
            "Set one up via `/setup` → 👥 Member Sync (or 🚂 Train → Conductor Rotation)"
            in s.sent[s.index_of("(skipped)")]
        )
        assert all(type(v).__name__ == "YesNoView" for v in s.views)
        assert rec.prompt(0).startswith("**Step 2 of 10 — Buddy List Tab**\n")
        assert rec.kw(0)["default"] == "Buddy System" and rec.kw(0)["current"] == "Buddy System"
        assert rec.kw(0)["timeout_cmd"] == "setup_buddy"
        assert rec.prompt(1).startswith("**Step 3 of 10 — Buddy Presets Tab**\n")
        assert (
            rec.kw(1)["default"] == "Buddy Presets"
            and rec.kw(1)["modal_title"] == "Buddy Presets Tab Name"
        )
        assert len(rec.calls) == 2

    @pytest.mark.asyncio
    async def test_saved_fields_and_summary(self, seeded_db):
        s, _ = await _drive(MINIMAL, keep=["Pairs", "Sets"])
        cfg = _buddy()
        assert cfg["enabled"] == 1
        assert (cfg["buddy_tab"], cfg["preset_tab"]) == ("Pairs", "Sets")
        assert (cfg["profession_tab"], cfg["profession_col_header"]) == (
            "Squad Powers",
            "Profession",
        )
        assert cfg["include_col_header"] == "" and cfg["roster_filter_enabled"] == 0
        assert cfg["engineer_doubling"] == 0 and cfg["scarcity_priority"] == "alphabetical"
        assert cfg["reliability_enabled"] == 0 and cfg["notify_channel_id"] == 0
        assert cfg["dm_enabled"] == 0 and cfg["dm_template"] == ""
        embed = s.final_embed
        assert embed.title == "🤝 Buddy System configured"
        assert embed.description == (
            "**Buddy tab:** Pairs\n"
            "**Preset tab:** Sets\n"
            "**Opt-out column:** not set\n"
            "**Limited to your member roster:** ❌ No\n"
            "**Two Engineers per War Leader:** ❌ No\n"
            "**When Engineers are scarce:** alphabetical\n"
            "**Rank Engineers by reliability:** ❌ No\n"
            "**Leadership alerts:** off\n"
            "**Buddy DMs:** ❌ No"
        )
        assert embed.footer.text == "Open /buddy to view the list, pair members, or auto-assign."

    @pytest.mark.asyncio
    async def test_a_finished_walk_releases_the_registration(self, seeded_db):
        import wizard_registry

        wizard_registry._active.pop(USER_ID, None)
        await _drive(MINIMAL)
        assert not wizard_registry._active.get(USER_ID)


class TestFreshEverythingOn:
    ALL_ON = [
        ("selected", True),  # enable
        ("selected", True),  # opt-out column
        ("selected", True),  # roster filter
        ("selected", True),  # doubling
        ("selected", True),  # strongest first
        ("selected", True),  # reliability
        ("choice", "default"),  # reliability source
        ("selected", True),  # alerts
        ("confirmed", True),  # channel picker stand-in
        ("selected", True),  # DMs
    ]

    @pytest.mark.asyncio
    async def test_every_branch_with_a_profession_question_a_roster_and_a_power_source(
        self, seeded_db
    ):
        s, rec = await _drive(
            self.ALL_ON,
            keep=["Pairs", "Sets", "Active?", "Hi {name}, meet {buddy} ({buddy_role})"],
            survey=PROFESSION_SURVEY,
            roster=ROSTER_HAND,
            has_roster=True,
            storm=STORM_WITH_POWER,
            is_premium=True,
        )
        assert s.sent[s.index_of("✅ Found your")] == (
            "✅ Found your **Your profession** survey question writing to the "
            "**Powers** tab. The Buddy System will read professions from there."
        )
        opt_out = s.sent[s.index_of("Step 4 of 10")]
        assert "keep members on the **Powers** tab after they leave" in opt_out
        assert rec.prompt(2).startswith("**Step 4a of 10 — Which column?**\n")
        assert "row 1 of **Powers**" in rec.prompt(2)
        assert (
            rec.kw(2)["default"] == "In Buddy System"
            and rec.kw(2)["modal_title"] == "Opt-out Column Header"
        )
        roster_prompt = s.sent[s.index_of("Step 5 of 10")]
        assert roster_prompt.startswith(
            "**Step 5 of 10 — Keep the list in step with your roster?**\n"
        )
        assert "Your **Members** tab is one you maintain by hand." in roster_prompt
        assert "💎 *With Member Sync, that tab keeps itself current" in roster_prompt
        assert roster_prompt.endswith("Only people on that tab would be eligible for a buddy.")
        assert s.sent[s.index_of("Step 7 of 10")].startswith(
            "**Step 7 of 10 — When Engineers are scarce**\n"
        )
        rel = s.sent[s.index_of("Step 8a of 10")]
        assert rel == (
            "**Step 8a of 10 — Where are your reliability scores?**\n"
            "The bot reads each Engineer's 1-5 score from here (it never writes to it) "
            "and matches members the same way it reads power:\n"
            "Tab name: **Powers** (default)\n"
            "Column: **D** (default)\n\n"
            "Keep these, or define your own?"
        )
        assert [c.label for c in s.view_named("_RelChoiceView").children] == [
            "✅ Use default",
            "✏️ Define my own",
        ]
        assert s.sent[s.index_of("Step 9 of 10")].endswith(
            "💎 Premium: these posts send only while Premium is active."
        )
        assert rec.prompt(3).startswith("**Buddy DM message**\n")
        assert rec.kw(3)["modal_label"] == "DM body"

        cfg = _buddy()
        assert (cfg["profession_tab"], cfg["profession_col_header"]) == (
            "Powers",
            "Your profession",
        )
        assert cfg["include_col_header"] == "Active?"
        assert cfg["roster_filter_enabled"] == 1 and cfg["engineer_doubling"] == 1
        assert cfg["scarcity_priority"] == "strongest_first"
        assert (cfg["reliability_enabled"], cfg["reliability_tab"], cfg["reliability_column"]) == (
            1,
            "Powers",
            "D",
        )
        assert cfg["notify_channel_id"] == 777001
        assert (cfg["dm_enabled"], cfg["dm_template"]) == (
            1,
            "Hi {name}, meet {buddy} ({buddy_role})",
        )
        assert s.final_embed.description == (
            "**Buddy tab:** Pairs\n"
            "**Preset tab:** Sets\n"
            "**Opt-out column:** `Active?` on Powers\n"
            "**Limited to your member roster:** ✅ Yes (Members)\n"
            "**Two Engineers per War Leader:** ✅ Yes\n"
            "**When Engineers are scarce:** strongest first\n"
            "**Rank Engineers by reliability:** ✅ Yes (column D on Powers)\n"
            "**Leadership alerts:** <#777001>\n"
            "**Buddy DMs:** ✅ Yes (custom message)"
        )

    @pytest.mark.asyncio
    async def test_synced_roster_gets_the_premium_blurb_and_the_default_dm_stores_blank(
        self, seeded_db
    ):
        from defaults import DEFAULT_BUDDY_DM

        s, rec = await _drive(
            self.ALL_ON,
            keep=["Pairs", "Sets", "Active?", DEFAULT_BUDDY_DM],
            roster=ROSTER_SYNCED,
            has_roster=True,
            storm=STORM_WITH_POWER,
        )
        assert (
            "Your **Members** tab is synced from Discord 💎" in s.sent[s.index_of("Step 5 of 10")]
        )
        assert rec.kw(3)["default"] == DEFAULT_BUDDY_DM
        assert _buddy()["dm_template"] == ""
        assert s.final_embed.description.endswith("**Buddy DMs:** ✅ Yes")

    @pytest.mark.asyncio
    async def test_power_source_from_a_synced_roster_alone(self, seeded_db):
        s, _ = await _drive(
            [
                ("selected", True),
                ("selected", False),
                ("selected", False),
                ("selected", False),
                ("selected", False),
                ("selected", False),
                ("selected", False),
                ("selected", False),
            ],
            roster=ROSTER_SYNCED,
            has_roster=True,
        )
        assert "Step 7 of 10" in "".join(s.sent)
        s.assert_never("Skipping the strongest-first option")

    @pytest.mark.asyncio
    async def test_stale_alerts_channel_is_called_out(self, seeded_db):
        from messages import PREV_CHANNEL_GONE

        s, _ = await _drive(
            [
                ("selected", True),
                ("selected", False),
                ("selected", False),
                ("selected", False),
                ("selected", True),
                ("confirmed", True),
                ("selected", False),
            ],
            channel_step=_channel_step(5, stale=True),
        )
        s.assert_order(PREV_CHANNEL_GONE.format(channel_label="leadership alerts"), "​")
        assert _buddy()["notify_channel_id"] == 5


class TestReliabilitySource:
    WALK_TO_REL = [("selected", True), ("selected", False), ("selected", False), ("selected", True)]

    @pytest.mark.asyncio
    async def test_saved_custom_offers_keep_and_names_the_defaults(self, seeded_db):
        _saved(reliability_enabled=1, reliability_tab="Scores", reliability_column="E", enabled=0)
        s, _ = await _drive(
            self.WALK_TO_REL + [("choice", "keep"), ("selected", False), ("selected", False)],
            roster=ROSTER_HAND,
        )
        view = s.view_named("_RelChoiceView")
        assert [c.label for c in view.children] == [
            "Keep current",
            "↩️ Use default (Members; D)",
            "✏️ Define my own",
        ]
        rel = s.sent[s.index_of("Step 8a of 10")]
        assert "Tab name: **Scores**\nColumn: **E**\n" in rel
        cfg = _buddy()
        assert (cfg["reliability_enabled"], cfg["reliability_tab"], cfg["reliability_column"]) == (
            1,
            "Scores",
            "E",
        )

    @pytest.mark.asyncio
    async def test_define_my_own_takes_the_modal_values(self, seeded_db):
        s, _ = await _drive(
            self.WALK_TO_REL
            + [
                {"choice": "custom", "modal_out": ("Sheet2", "G")},
                ("selected", False),
                ("selected", False),
            ]
        )
        cfg = _buddy()
        assert (cfg["reliability_tab"], cfg["reliability_column"]) == ("Sheet2", "G")
        assert (
            "**Rank Engineers by reliability:** ✅ Yes (column G on Sheet2)"
            in s.final_embed.description
        )

    @pytest.mark.asyncio
    async def test_default_falls_back_to_member_roster_when_nothing_is_configured(self, seeded_db):
        s, _ = await _drive(
            self.WALK_TO_REL + [("choice", "default"), ("selected", False), ("selected", False)]
        )
        assert _buddy()["reliability_tab"] == "Member Roster"

    @pytest.mark.asyncio
    async def test_keep_with_nothing_saved_turns_reliability_back_off(self, seeded_db):
        s, _ = await _drive(
            self.WALK_TO_REL + [("choice", "keep"), ("selected", False), ("selected", False)]
        )
        assert s.sent[s.index_of("Step 8a of 10") + 1] == (
            "⚠️ I need both a tab and a column letter to read reliability. Leaving it "
            "off for now — re-run setup to set it. Using alphabetical Engineer order."
        )
        assert _buddy()["reliability_enabled"] == 0


class TestDisable:
    @pytest.mark.asyncio
    async def test_first_time_no_is_a_bare_confirmation(self, seeded_db):
        s, _ = await _drive([("selected", False)])
        assert s.sent[-1] == "✅ Profession Buddy System disabled."
        assert len(s.views) == 1
        assert _buddy()["enabled"] == 0

    @pytest.mark.asyncio
    async def test_no_with_prior_config_offers_to_clear_it(self, seeded_db):
        import config

        _saved(enabled=0)
        s, _ = await _drive([("selected", False), ("cleared", False)])
        assert s.sent[-1] == (
            "✅ Profession Buddy System disabled. Your previous configuration is saved. "
            "Re-run `/setup → 🤝 Buddy System` and pick Yes to restore it instantly."
        )
        assert type(s.views[1]).__name__ == "ClearConfigView"
        assert config.has_buddy_config(TEST_GUILD_ID)
        assert _buddy()["buddy_tab"] == "Pairs"


class TestReentry:
    @pytest.mark.asyncio
    async def test_summary_then_no_changes(self, seeded_db):
        _saved(notify_channel_id=42, include_col_header="Active?", dm_enabled=1)
        s, rec = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert embed.title == "🤝 Current Buddy System Setup"
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Buddy Tab", "Pairs"),
            ("Preset Tab", "Sets"),
            ("Opt-out column", "`Active?`"),
            ("Limited to your member roster", "❌ No"),
            ("Two Engineers per War Leader", "✅ Yes"),
            ("When Engineers are scarce", "Strongest War Leaders first"),
            ("Rank Engineers by reliability", "❌ No"),
            ("Leadership alerts", "<#42>"),
            ("Buddy DMs", "✅ Yes"),
        ]
        assert rec.calls == [] and _buddy()["buddy_tab"] == "Pairs"
        s.assert_never("🤝 **Profession Buddy System Setup**")

    @pytest.mark.asyncio
    async def test_unset_opt_out_column_reads_as_everyone_eligible(self, seeded_db):
        _saved()
        s, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert embed.fields[2].value == "*not set — everyone on the profession tab is eligible*"

    @pytest.mark.asyncio
    async def test_edit_walks_on_with_the_saved_values_as_currents(self, seeded_db):
        _saved()
        s, rec = await _drive([("proceed", True)] + MINIMAL)
        assert rec.kw(0)["current"] == "Pairs" and rec.kw(1)["current"] == "Sets"
        assert _buddy()["engineer_doubling"] == 0  # re-answered No

    @pytest.mark.asyncio
    async def test_a_disabled_saved_row_skips_the_summary(self, seeded_db):
        _saved(enabled=0)
        s, _ = await _drive(MINIMAL)
        assert s.sent[0].startswith("🤝 **Profession Buddy System Setup**")


class TestExits:
    @pytest.mark.parametrize("stop_at", [0, 2, 3, 4, 5])
    @pytest.mark.asyncio
    async def test_timeout_at_a_required_question_posts_the_route_back(self, seeded_db, stop_at):
        s, _ = await _drive(MINIMAL[:stop_at] + ["timeout"])
        assert s.sent[-1] == TIMEOUT
        assert _buddy()["enabled"] == 0

    @pytest.mark.asyncio
    async def test_timeout_at_the_opt_out_question_is_taken_as_no(self, seeded_db):
        """The opt-out and roster questions never had a timeout branch: an
        unanswered view falls through as No and the walk carries on."""
        s, _ = await _drive([("selected", True), "timeout"] + MINIMAL[2:])
        s.assert_never(TIMEOUT)
        assert _buddy()["enabled"] == 1 and _buddy()["include_col_header"] == ""

    @pytest.mark.parametrize("stop_at", [0, 1, 2, 3, 4, 5])
    @pytest.mark.asyncio
    async def test_cancel_is_silent_and_saves_nothing(self, seeded_db, stop_at):
        import wizard_registry

        wizard_registry._active.pop(USER_ID, None)
        s, _ = await _drive(MINIMAL[:stop_at] + ["cancel"])
        s.assert_never(TIMEOUT)
        assert _buddy()["enabled"] == 0
        assert not wizard_registry._active.get(USER_ID)

    @pytest.mark.asyncio
    async def test_reliability_source_timeout_and_unconfirmed_channel(self, seeded_db):
        s, _ = await _drive(TestReliabilitySource.WALK_TO_REL + ["timeout"])
        assert s.sent[-1] == TIMEOUT
        s, _ = await _drive(
            [
                ("selected", True),
                ("selected", False),
                ("selected", False),
                ("selected", False),
                ("selected", True),
                ("confirmed", False),
            ],
            channel_step=_channel_step(confirmed=False),
        )
        assert s.sent[-1] == TIMEOUT

    @pytest.mark.parametrize("at", [0, 1])
    @pytest.mark.asyncio
    async def test_abandoning_a_tab_question_saves_nothing(self, seeded_db, at):
        keep = ["Pairs", "Sets"]
        keep[at] = None
        await _drive([("selected", True)], keep=keep)
        assert _buddy()["enabled"] == 0
