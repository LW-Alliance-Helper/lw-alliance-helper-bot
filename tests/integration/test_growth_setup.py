"""
Characterization tests for the growth wizard (`setup_cog.run_growth_setup`),
written against the function before its move out of `setup_cog` (#611
round 2) and kept unchanged after it.

The five existing growth tests answer every view with one flat override
dict. These drive the views one at a time, in order, and hold the prompts,
the button labels, the metrics embed, the saved row and the confirmation
embed. `ask_keep_or_change` and `ask_disable_with_clear` are patched in
whichever homes hold them so the same file holds before and after the
move. The metric editor's launcher wraps a real modal; a callable answer
fills it the way a submitted one would.
"""

import importlib
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# setup_cog re-exports the wizard pieces by name at import time; import it
# before anything patches `wizard_steps.X` (see CLAUDE.md, test gotchas).
import setup_cog  # noqa: F401

from tests.conftest import TEST_GUILD_ID, make_mock_interaction
from messages import WIZARD_TIMEOUT, INPUT_INVALID, SETUP_POINTER_FOOTER, TIER_COMPARISON

TIMEOUT = WIZARD_TIMEOUT.format(wizard="📈 Growth")
RECOVERY = "`/setup` → 📈 Growth"
G = TEST_GUILD_ID
NEXT = datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)
NEXT_VALUE = (
    f"<t:{int(NEXT.timestamp())}:F> (<t:{int(NEXT.timestamp())}:R>)\n"
    "*Want to start tracking from today instead? "
    "Run `/growth overview` and click **📸 Run Snapshot Now**.*"
)
ONE_METRIC = [{"label": "1st Squad Power", "col": "E"}]


def _wizard():
    import setup_cog

    return setup_cog.run_growth_setup


def _both(name):
    targets = []
    for home in ("setup_cog", "wizard_steps", "growth_setup"):
        try:
            module = importlib.import_module(home)
        except ImportError:
            continue
        if hasattr(module, name):
            targets.append(f"{home}.{name}")
    return targets


def _edit(label, col):
    """Answer the metric editor's launcher the way a submitted modal would."""

    def _fill(view):
        view.modal.label_value = label
        view.modal.col_value = col
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
            return MagicMock(id=1)
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

    def views_named(self, name):
        return [v for v in self.views if type(v).__name__ == name]

    def view_named(self, name, nth=0):
        return self.views_named(name)[nth]

    def embeds_titled(self, title):
        return [e for e in self.embeds if e is not None and e.title == title]

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


class _Env:
    def __init__(self, *, is_premium=False, script=None, next_snapshot=NEXT):
        self.is_premium = is_premium
        self.script = script
        self.next_snapshot = next_snapshot
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

        async def _limit(feature, *a, **kw):
            if self.is_premium:
                return None
            return 5 if feature == "growth_metrics" else None

        async def _disable(channel, **kw):
            env.disable_calls.append(kw)

        self._p = [
            patch("premium.is_premium", AsyncMock(return_value=self.is_premium)),
            patch("premium.get_limit", side_effect=_limit),
            patch("wizard_registry.register", side_effect=_register),
            patch("growth.compute_next_snapshot", return_value=self.next_snapshot),
        ]
        self._p += [patch(t, side_effect=_disable) for t in _both("ask_disable_with_clear")]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()
        return False


async def _drive(answers, *, keep=(), is_premium=False, next_snapshot=NEXT):
    interaction = make_mock_interaction()
    script = Script(interaction.channel, answers)
    with (
        _Env(is_premium=is_premium, script=script, next_snapshot=next_snapshot) as env,
        KeepRecorder(keep) as rec,
    ):
        await _wizard()(interaction, AsyncMock())
    return script, rec, env


def _cfg():
    import config

    return config.get_growth_config(G)


def _has():
    import config

    return config.has_growth_config(G)


def _save(**over):
    import config

    kw = dict(
        enabled=1,
        tab_source="Squad Powers",
        name_col="A",
        metrics=list(ONE_METRIC),
        tab_growth="Growth Tracking",
        snapshot_frequency="monthly",
        snapshot_day=15,
        snapshot_interval=30,
        data_start_row=2,
    )
    kw.update(over)
    config.save_growth_config(G, **kw)


# Saved metrics, Done, monthly.
MINIMAL = [("selected", True), ("choice", "done"), ("selected", "monthly")]


# ── Fresh ────────────────────────────────────────────────────────────────────


class TestFresh:
    @pytest.mark.asyncio
    async def test_minimal_over_a_disabled_row_saves_and_summarises(self, seeded_db):
        _save(enabled=0)
        s, rec, env = await _drive(MINIMAL)
        s.assert_order(
            "⚙️ **Growth Tracking Setup**\n"
            "Configure how the bot tracks your alliance's growth over time. "
            "Each month (or on your chosen schedule), the bot takes a snapshot of your members' stats "
            "and records them in your Google Sheet so you can track progress.",
            "**Step 1 of 7 — Enable growth tracking?**\n"
            "Should the bot automatically take snapshots of your members' stats on a schedule?",
            "**Step 7 of 7 — Snapshot Frequency**\nHow often should the bot take a snapshot?"
            "\n*🔒 Custom interval is a Premium feature.*",
        )
        assert [type(v).__name__ for v in s.views] == [
            "YesNoView",
            "MetricsActionView",
            "FrequencyView",
        ]
        assert rec.prompt(0) == (
            "**Step 2 of 7 — Source Tab**\n"
            "Which tab in your Google Sheet contains your member data?\n"
            "⚠️ *Make sure this tab exists in your sheet.*"
        )
        assert rec.kw(0) == dict(
            default="Squad Powers",
            current="Squad Powers",
            modal_title="Source Tab",
            modal_label="Tab name",
            timeout_cmd="setup_growth",
            cancel_event=s.cancel_event,
        )
        assert rec.prompt(1) == (
            "**Step 3 of 7 — Data Start Row**\n"
            "Which row does your member data start on? (Row 1 is usually the header)"
        )
        assert rec.kw(1)["default"] == "2" and rec.kw(1)["current"] == "2"
        assert rec.kw(1)["modal_title"] == "Data Start Row"
        assert rec.kw(1)["modal_label"] == "Row number"
        assert rec.prompt(2) == (
            "**Step 4 of 7 — Name Column**\nWhich column contains the member's name?"
        )
        assert rec.kw(2)["default"] == "A" and rec.kw(2)["current"] == "A"
        assert rec.kw(2)["modal_label"] == "Column letter"
        assert rec.prompt(3) == (
            "**Step 6 of 7 — Growth Tracking Tab**\n"
            "Which tab should snapshots be written to?\n"
            "⚠️ *If the tab doesn't exist, the bot will create it automatically.*"
        )
        assert (
            rec.kw(3)["default"] == "Growth Tracking" and rec.kw(3)["current"] == "Growth Tracking"
        )
        assert rec.prompt(4) == (
            "**Step 7a of 7 — Snapshot Day**\n"
            "Which day of the month should the snapshot run? (1–28)"
        )
        assert rec.kw(4)["default"] == "1" and rec.kw(4)["current"] == "15"
        assert rec.kw(4)["modal_title"] == "Snapshot Day"
        assert rec.kw(4)["modal_label"] == "Day of month (1–28)"

        metrics_embed = s.embeds_titled("📊 Step 5 of 7 — Metrics to Track")[0]
        assert metrics_embed.description == (
            "Define which columns the bot should snapshot each period. "
            "Add as many as you want — for example a `1st Squad Power` column, `THP`, `Total Kills`, etc."
        )
        assert [(f.name, f.value) for f in metrics_embed.fields] == [
            ("1st Squad Power", "Column E")
        ]
        assert metrics_embed.footer.text == TIER_COMPARISON.format(
            free_limit="1 of 5 metrics used", premium_limit="unlimited"
        )
        actions = s.view_named("MetricsActionView")
        assert [(c.label, c.disabled, c.row) for c in actions.children] == [
            ("➕ Add Metric", False, 0),
            ("✏️ Edit Metric", False, 0),
            ("🗑️ Delete Metric", False, 0),
            ("✅ Done", False, 1),
        ]
        freq = s.view_named("FrequencyView")
        assert [(c.label, c.disabled) for c in freq.children] == [
            ("📅 Monthly (1st of each month)", False),
            ("🔁 Custom interval (every X days) 💎", True),
        ]

        cfg = _cfg()
        assert cfg["enabled"] == 1 and cfg["tab_source"] == "Squad Powers"
        assert cfg["name_col"] == "A" and cfg["data_start_row"] == 2
        assert cfg["metrics"] == ONE_METRIC and cfg["tab_growth"] == "Growth Tracking"
        # The recorder answers the day question with its default ("1").
        assert cfg["snapshot_frequency"] == "monthly" and cfg["snapshot_day"] == 1
        assert cfg["snapshot_interval"] == 30
        embed = s.final_embed
        assert embed.title == "✅ Growth Tracking Configured"
        assert [(f.name, f.value, f.inline) for f in embed.fields] == [
            ("Source Tab", "Squad Powers", False),
            ("Name Column", "Column A", False),
            ("Data Start Row", "2", False),
            ("Growth Tab", "Growth Tracking", False),
            ("Snapshot Schedule", "Monthly on day 1", False),
            ("Next Snapshot", NEXT_VALUE, False),
            ("Metrics", "• **1st Squad Power** — column E", False),
        ]
        assert embed.footer.text == (
            SETUP_POINTER_FOOTER.format(wizard="📈 Growth")
            + " Use /growth overview to take a manual snapshot."
        )

    @pytest.mark.asyncio
    async def test_fresh_guild_has_no_metrics_and_cannot_finish(self, seeded_db):
        s, rec, _ = await _drive([("selected", True), ("choice", "done")])
        metrics_embed = s.embeds_titled("📊 Step 5 of 7 — Metrics to Track")[0]
        assert [(f.name, f.value) for f in metrics_embed.fields] == [
            ("No metrics yet", "Click **Add Metric** to begin.")
        ]
        actions = s.view_named("MetricsActionView")
        assert [(c.label, c.disabled) for c in actions.children] == [
            ("➕ Add Metric", False),
            ("✏️ Edit Metric", True),
            ("🗑️ Delete Metric", True),
            ("✅ Done", True),
        ]
        assert rec.kw(0)["current"] == "" and rec.kw(1)["current"] == "2"
        assert rec.kw(2)["current"] == "A"
        # Recorded as it stands: a Done that got through with no metrics
        # ends the wizard with the no-metrics line.
        assert s.sent[-1] == "⚠️ No metrics defined. Run `/setup` → 📈 Growth to try again."
        assert not _has()

    @pytest.mark.asyncio
    async def test_premium_has_no_cap_footer_and_custom_interval(self, seeded_db):
        _save(enabled=0)
        s, rec, _ = await _drive(
            [("selected", True), ("choice", "done"), ("selected", "interval")],
            keep=["Squad Powers", "3", "b", "Growth Tracking", " 14 "],
            is_premium=True,
        )
        metrics_embed = s.embeds_titled("📊 Step 5 of 7 — Metrics to Track")[0]
        assert metrics_embed.footer.text is None
        assert s.sent[s.index_of("Step 7 of 7")] == (
            "**Step 7 of 7 — Snapshot Frequency**\nHow often should the bot take a snapshot?"
        )
        assert not s.view_named("FrequencyView").children[1].disabled
        assert rec.prompt(4) == (
            "**Step 7a of 7 — Interval (days)**\nHow many days between each snapshot?"
        )
        assert rec.kw(4)["default"] == "30" and rec.kw(4)["current"] == "30"
        assert rec.kw(4)["modal_title"] == "Interval"
        assert rec.kw(4)["modal_label"] == "Days between snapshots"
        cfg = _cfg()
        assert cfg["name_col"] == "B" and cfg["data_start_row"] == 3
        assert cfg["snapshot_frequency"] == "interval" and cfg["snapshot_interval"] == 14
        assert cfg["snapshot_day"] == 1
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert (
            fields["Snapshot Schedule"] == "Every 14 days" and fields["Name Column"] == "Column B"
        )

    @pytest.mark.parametrize("raw, expected", [("0", 1), ("40", 28), ("abc", 1), ("7", 7)])
    @pytest.mark.asyncio
    async def test_snapshot_day_is_clamped_or_defaulted(self, seeded_db, raw, expected):
        _save(enabled=0)
        await _drive(MINIMAL, keep=["Squad Powers", "2", "A", "Growth Tracking", raw])
        assert _cfg()["snapshot_day"] == expected

    @pytest.mark.parametrize("raw, expected", [("0", 1), ("x", 30), ("90", 90)])
    @pytest.mark.asyncio
    async def test_interval_is_floored_or_defaulted(self, seeded_db, raw, expected):
        _save(enabled=0)
        await _drive(
            [("selected", True), ("choice", "done"), ("selected", "interval")],
            keep=["Squad Powers", "2", "A", "Growth Tracking", raw],
            is_premium=True,
        )
        assert _cfg()["snapshot_interval"] == expected

    @pytest.mark.asyncio
    async def test_next_snapshot_unknown(self, seeded_db):
        _save(enabled=0)
        s, _, _ = await _drive(MINIMAL, next_snapshot=None)
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert (
            fields["Next Snapshot"] == "*Could not compute — check `/growth overview` for status.*"
        )

    @pytest.mark.parametrize("bad", ["", "two", "2.5"])
    @pytest.mark.asyncio
    async def test_bad_start_row_ends_the_wizard(self, seeded_db, bad):
        s, rec, _ = await _drive([("selected", True)], keep=["Squad Powers", bad])
        assert s.sent[-1] == INPUT_INVALID.format(type="row number", example="2", recovery=RECOVERY)
        assert len(rec.calls) == 2 and not _has()

    @pytest.mark.parametrize("bad", ["", "AA", "1", "a1"])
    @pytest.mark.asyncio
    async def test_bad_name_column_ends_the_wizard(self, seeded_db, bad):
        s, rec, _ = await _drive([("selected", True)], keep=["Squad Powers", "2", bad])
        assert s.sent[-1] == INPUT_INVALID.format(
            type="single column letter", example="A", recovery=RECOVERY
        )
        assert len(rec.calls) == 3 and not _has()

    @pytest.mark.asyncio
    async def test_both_tabs_warn_when_another_feature_owns_them(self, seeded_db):
        _save(enabled=0)
        with patch(
            "config.tabs_in_use", return_value={"squad powers": "X", "growth tracking": "Y"}
        ) as tabs:
            s, _, _ = await _drive(MINIMAL)
        assert [c.kwargs["exclude_field"] for c in tabs.call_args_list] == [
            "tab_source",
            "tab_growth",
        ]
        assert s.sent[s.index_of("**Squad Powers** is also X")].startswith("⚠️ Heads up:")
        assert s.sent[s.index_of("**Growth Tracking** is also Y")].startswith("⚠️ Heads up:")


# ── The metrics editor ───────────────────────────────────────────────────────


class TestMetrics:
    @pytest.mark.asyncio
    async def test_add_relists_without_a_metric_when_nothing_was_entered(self, seeded_db):
        _save(enabled=0)
        s, _, _ = await _drive(
            [("selected", True), ("choice", "loop"), ("choice", "done"), ("selected", "monthly")]
        )
        assert len(s.views_named("MetricsActionView")) == 2
        assert _cfg()["metrics"] == ONE_METRIC

    @pytest.mark.asyncio
    async def test_at_the_free_cap_add_is_disabled(self, seeded_db):
        five = [{"label": f"M{i}", "col": "ABCDE"[i]} for i in range(5)]
        _save(enabled=0, metrics=five)
        s, _, _ = await _drive(MINIMAL)
        actions = s.view_named("MetricsActionView")
        assert [(c.label, c.disabled) for c in actions.children][:1] == [("➕ Add Metric", True)]
        metrics_embed = s.embeds_titled("📊 Step 5 of 7 — Metrics to Track")[0]
        assert metrics_embed.footer.text == TIER_COMPARISON.format(
            free_limit="5 of 5 metrics used", premium_limit="unlimited"
        )
        assert len(metrics_embed.fields) == 5

    @pytest.mark.asyncio
    async def test_edit_a_metric(self, seeded_db):
        _save(enabled=0, metrics=[{"label": "THP", "col": "E"}, {"label": "Kills", "col": "F"}])
        s, _, _ = await _drive(
            [
                ("selected", True),
                ("choice", "edit"),
                ("index", 1),
                _edit("Total Kills", "g"),
                ("choice", "done"),
                ("selected", "monthly"),
            ]
        )
        assert s.sent[s.index_of("Which metric")] == "Which metric do you want to edit?"
        pick = s.view_named("PickMetricView")
        assert pick.select.placeholder == "Choose a metric..."
        assert [(o.label, o.value, o.description) for o in pick.select.options] == [
            ("THP", "0", "Column E"),
            ("Kills", "1", "Column F"),
        ]
        assert s.sent[s.index_of("Editing")] == (
            "Editing **Kills** (column F). Click below to update."
        )
        launch = s.view_named("EditLaunchView")
        assert [c.label for c in launch.children] == ["✏️ Edit values"]
        assert launch.modal.title == "Metric"
        assert [
            (c.label, c.default, c.placeholder, c.max_length) for c in launch.modal.children
        ] == [
            ("Label", "Kills", "e.g. 1st Squad Power, THP, Total Kills", 100),
            ("Column letter", "F", "e.g. E", 2),
        ]
        assert _cfg()["metrics"] == [
            {"label": "THP", "col": "E"},
            {"label": "Total Kills", "col": "g"},
        ]
        assert len(s.views_named("MetricsActionView")) == 2

    @pytest.mark.asyncio
    async def test_edit_with_a_bad_column_keeps_the_old_metric(self, seeded_db):
        _save(enabled=0)
        await _drive(
            [
                ("selected", True),
                ("choice", "edit"),
                ("index", 0),
                _edit("THP", "12"),
                ("choice", "done"),
                ("selected", "monthly"),
            ]
        )
        assert _cfg()["metrics"] == ONE_METRIC

    @pytest.mark.asyncio
    async def test_edit_launcher_left_alone_keeps_the_old_metric(self, seeded_db):
        _save(enabled=0)
        s, _, _ = await _drive(
            [
                ("selected", True),
                ("choice", "edit"),
                ("index", 0),
                "timeout",
                ("choice", "done"),
                ("selected", "monthly"),
            ]
        )
        s.assert_never(TIMEOUT)
        assert _cfg()["metrics"] == ONE_METRIC

    @pytest.mark.asyncio
    async def test_delete_a_metric(self, seeded_db):
        _save(enabled=0, metrics=[{"label": "THP", "col": "E"}, {"label": "Kills", "col": "F"}])
        s, _, _ = await _drive(
            [
                ("selected", True),
                ("choice", "delete"),
                ("index", 0),
                ("choice", "done"),
                ("selected", "monthly"),
            ]
        )
        assert s.sent[s.index_of("Which metric")] == "Which metric do you want to delete?"
        assert s.sent[s.index_of("Removed")] == "🗑️ Removed **THP** (column E)."
        assert _cfg()["metrics"] == [{"label": "Kills", "col": "F"}]

    @pytest.mark.asyncio
    async def test_edit_or_delete_with_no_metrics_relists(self, seeded_db):
        s, _, _ = await _drive(
            [("selected", True), ("choice", "edit"), ("choice", "delete"), "timeout"]
        )
        assert (
            len(s.views_named("MetricsActionView")) == 3 and s.views_named("PickMetricView") == []
        )
        assert s.sent[-1] == TIMEOUT

    @pytest.mark.asyncio
    async def test_metrics_timeouts_and_cancels(self, seeded_db):
        _save(enabled=0)
        s, _, _ = await _drive([("selected", True), "timeout"])
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive([("selected", True), "cancel"])
        s.assert_never(TIMEOUT)
        s, _, _ = await _drive([("selected", True), ("choice", "edit"), "timeout"])
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive([("selected", True), ("choice", "edit"), "cancel"])
        s.assert_never(TIMEOUT)
        s, _, _ = await _drive([("selected", True), ("choice", "edit"), ("index", 0), "cancel"])
        s.assert_never(TIMEOUT)
        assert _cfg()["enabled"] == 0


# ── Disable ──────────────────────────────────────────────────────────────────


class TestDisable:
    @pytest.mark.asyncio
    async def test_first_run_disable(self, seeded_db):
        s, rec, env = await _drive([("selected", False)])
        assert rec.calls == []
        call = env.disable_calls[0]
        assert call["feature_label"] == "Growth tracking"
        assert call["setup_command"] == "setup → 📈 Growth"
        assert call["had_prior_config"] is False and call["cancel_event"] is s.cancel_event
        cfg = _cfg()
        assert _has() and cfg["enabled"] == 0 and cfg["tab_growth"] == "Growth Tracking"
        assert cfg["metrics"] == [] and cfg["name_col"] == "A"
        call["clear_fn"]()
        assert not _has()

    @pytest.mark.asyncio
    async def test_disable_keeps_the_saved_settings(self, seeded_db):
        _save(tab_source="Members", snapshot_frequency="interval", snapshot_interval=7)
        s, _, env = await _drive([("proceed", True), ("selected", False)])
        assert env.disable_calls[0]["had_prior_config"] is True
        cfg = _cfg()
        assert cfg["enabled"] == 0 and cfg["tab_source"] == "Members"
        assert cfg["metrics"] == ONE_METRIC and cfg["snapshot_interval"] == 7


# ── Re-entry ─────────────────────────────────────────────────────────────────


class TestReentry:
    @pytest.mark.asyncio
    async def test_summary_monthly_and_no_changes(self, seeded_db):
        _save(metrics=[{"label": "THP", "col": "E"}, {"label": "Kills", "col": "F"}])
        s, rec, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert embed.title == "📈 Current Growth Setup"
        assert embed.description == (
            "Growth tracking is already configured. Would you like to edit these settings?"
        )
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Source Tab", "Squad Powers"),
            ("Name Column", "Column A"),
            ("Data Start Row", "2"),
            ("Growth Tab", "Growth Tracking"),
            ("Snapshot Schedule", "Monthly on day 15"),
            ("Metrics (2)", "• THP — column E\n• Kills — column F"),
        ]
        assert s.sent[-1] == "✅ No changes made. Growth tracking is still active."
        assert rec.calls == [] and _cfg()["snapshot_day"] == 15

    @pytest.mark.asyncio
    async def test_summary_interval_with_nothing_set(self, seeded_db):
        _save(
            tab_source="",
            name_col="",
            data_start_row=0,
            tab_growth="",
            metrics=[],
            snapshot_frequency="interval",
            snapshot_interval=7,
        )
        s, _, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Source Tab", "*not set*"),
            ("Name Column", "Column *not set*"),
            ("Data Start Row", "*not set*"),
            ("Growth Tab", "*not set*"),
            ("Snapshot Schedule", "Every 7 days"),
            ("Metrics (0)", "*none*"),
        ]

    @pytest.mark.asyncio
    async def test_saved_values_are_the_currents(self, seeded_db):
        _save(
            tab_source="Members",
            name_col="C",
            data_start_row=3,
            tab_growth="Trend",
            snapshot_day=20,
        )
        s, rec, _ = await _drive(
            [("proceed", True)] + MINIMAL, keep=["Members", "3", "C", "Trend", "20"]
        )
        assert [rec.kw(i)["current"] for i in range(5)] == ["Members", "3", "C", "Trend", "20"]
        assert _cfg()["snapshot_day"] == 20 and _cfg()["name_col"] == "C"

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
    async def test_enable_and_frequency_timeouts(self, seeded_db):
        _save(enabled=0)
        s, _, _ = await _drive(["timeout"])
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive(MINIMAL[:2] + ["timeout"])
        assert s.sent[-1] == TIMEOUT
        assert _cfg()["enabled"] == 0

    @pytest.mark.parametrize("stop_at", [0, 1, 2])
    @pytest.mark.asyncio
    async def test_cancel_is_silent(self, seeded_db, stop_at):
        _save(enabled=0)
        s, _, _ = await _drive(MINIMAL[:stop_at] + ["cancel"])
        s.assert_never(TIMEOUT)
        assert _cfg()["enabled"] == 0

    @pytest.mark.parametrize("stop_at", [0, 1, 2, 3, 4])
    @pytest.mark.asyncio
    async def test_abandoned_keep_or_change_questions_end_silently(self, seeded_db, stop_at):
        _save(enabled=0)
        keep = ["Squad Powers", "2", "A", "Growth Tracking", "1"]
        keep[stop_at] = None
        s, rec, _ = await _drive(MINIMAL, keep=keep)
        s.assert_never(TIMEOUT)
        assert len(rec.calls) == stop_at + 1 and _cfg()["enabled"] == 0
