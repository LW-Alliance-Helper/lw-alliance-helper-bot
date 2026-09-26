"""
Characterization tests for the Growth Breakdown wizard
(`setup_cog.run_growth_breakdown_setup`), written against the function
before its move out of `setup_cog` (#611 round 2) and kept unchanged
after it.

The three existing breakdown tests answer every view with one flat
override dict. These drive the views one at a time, in order, and hold
the prompts, the button labels, the saved row and the confirmation embed.
`ask_keep_or_change` and `ChannelSelectStep` are patched in whichever
homes hold them so the same file holds before and after the move.
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
from messages import WIZARD_TIMEOUT, PREV_CHANNEL_GONE, SETUP_POINTER_FOOTER

TIMEOUT = WIZARD_TIMEOUT.format(wizard="📊 Growth Breakdown")
THRESHOLDS_TIMEOUT = (
    "⏰ Timed out or invalid thresholds. Run `/setup` → 📊 Growth Breakdown to start again."
)
G = TEST_GUILD_ID
GUARD = (
    "⚙️ Set up growth tracking first — run `/setup` → 📈 Growth and add at "
    "least one metric, then come back to `/setup` → 📊 Growth Breakdown to "
    "configure the breakdown layer."
)


def _wizard():
    import setup_cog

    return setup_cog.run_growth_breakdown_setup


def _both(name):
    targets = []
    for home in ("setup_cog", "wizard_steps", "growth_breakdown_setup"):
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


def _channel_step(channel_id=800001, *, stale=False, confirmed=True):
    return MagicMock(
        confirmed=confirmed,
        cancelled=False,
        selected_channel=MagicMock(id=channel_id),
        is_current_stale=stale,
        wait=AsyncMock(),
    )


class _Env:
    def __init__(self, *, channel_steps=None, script=None):
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

        self._p = [patch("wizard_registry.register", side_effect=_register)]
        self._p += [patch(t, side_effect=_pick) for t in _both("ChannelSelectStep")]
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()
        return False


async def _drive(answers, *, keep=(), channel_steps=None):
    interaction = make_mock_interaction()
    script = Script(interaction.channel, answers)
    with _Env(channel_steps=channel_steps, script=script) as env, KeepRecorder(keep) as rec:
        await _wizard()(interaction, AsyncMock())
    return script, rec, env


def _cfg():
    import config

    return config.get_growth_config(G)


def _save_growth(**over):
    import config

    kw = dict(
        enabled=1,
        tab_source="Squad Powers",
        name_col="A",
        metrics=[{"label": "1st Squad Power", "col": "E"}],
        tab_growth="Growth Tracking",
        snapshot_frequency="monthly",
        snapshot_day=1,
        snapshot_interval=30,
        data_start_row=2,
    )
    kw.update(over)
    config.save_growth_config(G, **kw)


def _save_breakdown(**over):
    import config

    kw = dict(
        tab_breakdown="Growth Breakdown",
        breakdown_thresholds={},
        breakdown_labels={},
        breakdown_post_channel_id=0,
        breakdown_bucket_filter=[],
    )
    kw.update(over)
    config.save_growth_breakdown_config(G, **kw)


CUSTOM_T = {"increased": 25.0, "steady": 12.5, "low": 3.0, "none": 0.0}
CUSTOM_L = {
    "increased": "Crushing It",
    "steady": "Fine",
    "low": "Meh",
    "none": "Stalled",
    "decline": "Going Backwards",
}

# Auto-post off, the default bucket filter, defaults for thresholds and labels.
MINIMAL = [
    ("selected", False),
    ("selected", []),
    ("choice", "defaults"),
    ("choice", "defaults"),
]
FILTER_PROMPT = (
    "**Step 3 of 5 — Bucket Filter**\n"
    "Which buckets should the breakdown list by name? The others show as a count. "
    "The default lists every bucket but **No Change**, which usually holds most "
    "of your alliance."
)


# ── Guard ────────────────────────────────────────────────────────────────────


class TestGuard:
    @pytest.mark.asyncio
    async def test_no_growth_config_stops_at_the_door(self, seeded_db):
        s, rec, _ = await _drive([])
        assert s.sent == [GUARD] and rec.calls == []

    @pytest.mark.asyncio
    async def test_growth_without_metrics_stops_too(self, seeded_db):
        _save_growth(metrics=[])
        s, _, _ = await _drive([])
        assert s.sent == [GUARD]

    @pytest.mark.asyncio
    async def test_growth_disabled_stops_too(self, seeded_db):
        _save_growth(enabled=0)
        s, _, _ = await _drive([])
        assert s.sent == [GUARD]


# ── Fresh ────────────────────────────────────────────────────────────────────


class TestFresh:
    @pytest.mark.asyncio
    async def test_minimal_saves_and_summarises(self, seeded_db):
        _save_growth()
        s, rec, env = await _drive(MINIMAL)
        s.assert_order(
            "📊 **Growth Breakdown Setup** (💎 Premium)\n"
            "Classifies each member's growth between snapshots into one of "
            "five buckets and (optionally) posts the summary to a channel "
            "after every snapshot.",
            "**Step 2 of 5 — Auto-Post After Snapshots?**\n"
            "Each time the bot finishes a snapshot, post the breakdown summary "
            "to a channel so leadership doesn't have to run `/growth breakdown` to see "
            "who's slowing down.",
            FILTER_PROMPT,
            "**Step 4 of 5 — Bucket Thresholds**\n"
            "Defaults: Increased ≥ 20%, Steady ≥ 10%, Low ≥ 5%, None ≥ 0%, Decline < 0%.\n"
            "Customize for stricter (or looser) growth standards — applies to "
            "every metric. Per-metric thresholds are tracked as a follow-up.",
            "**Step 5 of 5 — Bucket Labels**\n"
            "Defaults: Increased, Steady, Low, No Change, Decline.\n"
            "Rename buckets to match your alliance's voice (e.g. 'Crushing It', "
            "'Stalled', 'Going Backwards').",
        )
        assert [type(v).__name__ for v in s.views] == [
            "YesNoView",
            "BucketFilterView",
            "ThresholdsChoiceView",
            "LabelsChoiceView",
        ]
        assert rec.prompt(0) == (
            "**Step 1 of 5 — Breakdown Tab**\n"
            "Which tab in your Google Sheet should the breakdown data live in? "
            "The bot creates it automatically if it doesn't exist yet."
        )
        assert rec.kw(0) == dict(
            default="Growth Breakdown",
            current="Growth Breakdown",
            modal_title="Breakdown Tab",
            modal_label="Tab name",
            timeout_cmd="setup_growth_breakdown",
            cancel_event=s.cancel_event,
        )
        assert env.channel_calls == []
        t_view, l_view = s.view_named("ThresholdsChoiceView"), s.view_named("LabelsChoiceView")
        assert [c.label for c in t_view.children] == ["✅ Use defaults", "✏️ Customize"]
        assert [c.label for c in l_view.children] == ["✅ Use defaults", "✏️ Customize"]
        cfg = _cfg()
        assert cfg["tab_breakdown"] == "Growth Breakdown"
        assert cfg["breakdown_post_channel_id"] == 0 and cfg["breakdown_bucket_filter"] == []
        assert cfg["breakdown_thresholds"] == {} and cfg["breakdown_labels"] == {}
        embed = s.final_embed
        assert embed.title == "✅ Growth Breakdown Configured"
        assert [(f.name, f.value, f.inline) for f in embed.fields] == [
            ("Breakdown Tab", "Growth Breakdown", False),
            (
                "Auto-Post",
                "❌ Off — use `/growth breakdown` (or `/growth overview` → 📊 See most recent "
                "Breakdown) to view on demand.",
                False,
            ),
            ("Bucket Filter", "Default: All but No Change", False),
            (
                "Thresholds",
                "Defaults (Increased ≥ 20%, Steady ≥ 10%, Low ≥ 5%, None ≥ 0%, Decline < 0%)",
                False,
            ),
        ]
        assert embed.footer.text == SETUP_POINTER_FOOTER.format(wizard="📊 Growth Breakdown")

    @pytest.mark.asyncio
    async def test_another_feature_s_tab_only_warns(self, seeded_db):
        _save_growth()
        with patch("config.tabs_in_use", return_value={"squad powers": "your Growth tab"}) as tabs:
            s, _, _ = await _drive(MINIMAL, keep=["Squad Powers"])
        assert tabs.call_args.kwargs == dict(exclude_field="tab_breakdown", exclude_survey_id=None)
        assert s.sent[s.index_of("Heads up")].startswith(
            "⚠️ Heads up: **Squad Powers** is also your Growth tab."
        )
        assert _cfg()["tab_breakdown"] == "Squad Powers"

    @pytest.mark.asyncio
    async def test_custom_thresholds_and_labels(self, seeded_db):
        _save_growth()
        s, _, _ = await _drive(
            [
                ("selected", False),
                ("selected", []),
                {"choice": "customize", "_modal_values": CUSTOM_T},
                {"choice": "customize", "_modal_values": CUSTOM_L},
            ]
        )
        cfg = _cfg()
        assert cfg["breakdown_thresholds"] == CUSTOM_T and cfg["breakdown_labels"] == CUSTOM_L
        fields = {f.name: f.value for f in s.final_embed.fields}
        assert fields["Custom Thresholds"] == (
            "Increased ≥ 25%, Steady ≥ 12.5%, Low ≥ 3%, None ≥ 0%, Decline < 0%"
        )
        assert fields["Custom Labels"] == (
            "Increased→Crushing It, Steady→Fine, Low→Meh, No Change→Stalled, "
            "Decline→Going Backwards"
        )
        assert "Thresholds" not in fields

    @pytest.mark.asyncio
    async def test_custom_labels_with_blanks(self, seeded_db):
        _save_growth()
        s, _, _ = await _drive(
            [
                ("selected", False),
                ("selected", []),
                ("choice", "defaults"),
                {"choice": "customize", "_modal_values": {"increased": "Up", "decline": ""}},
            ]
        )
        assert {f.name: f.value for f in s.final_embed.fields}["Custom Labels"] == "Increased→Up"
        s, _, _ = await _drive(
            [
                ("proceed", True),
                ("selected", False),
                ("selected", []),
                ("choice", "defaults"),
                {"choice": "customize", "_modal_values": {"decline": ""}},
            ]
        )
        assert {f.name: f.value for f in s.final_embed.fields}["Custom Labels"] == "—"

    @pytest.mark.asyncio
    async def test_a_failed_save_says_so(self, seeded_db):
        _save_growth()
        with patch("config.save_growth_breakdown_config", return_value=False):
            s, _, _ = await _drive(MINIMAL)
        assert s.sent[-1] == (
            "⚠️ Couldn't save the breakdown config — make sure `/setup` → 📈 Growth "
            "has been run for this server first."
        )
        assert all(e is None for e in s.embeds)


class TestAutoPost:
    ON = [("selected", True), ("selected", []), ("choice", "defaults"), ("choice", "defaults")]

    @pytest.mark.asyncio
    async def test_on_asks_channel_and_filter(self, seeded_db):
        _save_growth()
        s, _, env = await _drive(self.ON, channel_steps=[_channel_step(800001, stale=True)])
        s.assert_order(
            "**Step 2 of 5",
            PREV_CHANNEL_GONE.format(channel_label="breakdown"),
            "**Auto-Post Channel**\nWhere should the breakdown summaries land?",
            FILTER_PROMPT,
            "**Step 4 of 5",
        )
        assert env.channel_calls[0]["suggested_name"] == "growth-breakdown"
        assert env.channel_calls[0]["include_threads"] is True
        assert env.channel_calls[0]["current_id"] == 0
        bf = s.view_named("BucketFilterView")
        sel = next(c for c in bf.children if hasattr(c, "options"))
        assert sel.placeholder == "Or pick the buckets to list by name"
        assert (sel.min_values, sel.max_values, sel.row) == (1, 5, 1)
        assert [(o.label, o.value, o.description) for o in sel.options] == [
            ("Increased", "increased", "Increased growth bucket"),
            ("Steady", "steady", "Steady growth bucket"),
            ("Low", "low", "Low growth bucket"),
            ("No Change", "none", "No Change growth bucket"),
            ("Decline", "decline", "Decline growth bucket"),
        ]
        buttons = [c for c in bf.children if getattr(c, "label", None)]
        assert [(b.label, b.row) for b in buttons] == [("✅ Use default: All but No Change", 0)]
        cfg = _cfg()
        assert cfg["breakdown_post_channel_id"] == 800001 and cfg["breakdown_bucket_filter"] == []
        fields = [(f.name, f.value) for f in s.final_embed.fields]
        assert fields[1:3] == [
            ("Auto-Post Channel", "<#800001>"),
            ("Bucket Filter", "Default: All but No Change"),
        ]

    @pytest.mark.asyncio
    async def test_a_filter_is_saved_and_named(self, seeded_db):
        _save_growth()
        s, _, _ = await _drive(
            [
                ("selected", True),
                ("selected", ["decline", "none"]),
                ("choice", "defaults"),
                ("choice", "defaults"),
            ]
        )
        assert _cfg()["breakdown_bucket_filter"] == ["decline", "none"]
        assert {f.name: f.value for f in s.final_embed.fields}[
            "Bucket Filter"
        ] == "Decline, No Change"

    @pytest.mark.asyncio
    async def test_a_saved_filter_gets_a_keep_button(self, seeded_db):
        _save_growth()
        _save_breakdown(breakdown_post_channel_id=5, breakdown_bucket_filter=["low", "decline"])
        s, _, env = await _drive([("proceed", True)] + self.ON)
        assert env.channel_calls[0]["current_id"] == 5
        assert s.sent[s.index_of("Step 3 of 5")] == (
            "**Step 3 of 5 — Bucket Filter**\n"
            "The breakdown lists **Low, Decline** by name, and shows the "
            "other buckets as a count. Keep that, go back to the default, or pick again."
        )
        bf = s.view_named("BucketFilterView")
        buttons = [c for c in bf.children if getattr(c, "label", None)]
        assert [(b.label, b.row) for b in buttons] == [
            ("Keep current: Low, Decline", 0),
            ("↩️ Use default: All but No Change", 0),
        ]
        assert next(c for c in bf.children if hasattr(c, "options")).row == 1

    @pytest.mark.asyncio
    async def test_unconfirmed_channel_is_a_timeout(self, seeded_db):
        _save_growth()
        s, _, _ = await _drive(self.ON, channel_steps=[_channel_step(confirmed=False)])
        assert s.sent[-1] == TIMEOUT and _cfg()["breakdown_post_channel_id"] == 0

    @pytest.mark.asyncio
    async def test_filter_timeout_and_cancel(self, seeded_db):
        _save_growth()
        s, _, _ = await _drive(self.ON[:1] + ["timeout"])
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive(self.ON[:1] + ["cancel"])
        s.assert_never(TIMEOUT)


# ── Re-entry ─────────────────────────────────────────────────────────────────


class TestReentry:
    @pytest.mark.asyncio
    async def test_summary_with_everything_and_no_changes(self, seeded_db):
        _save_growth()
        _save_breakdown(
            tab_breakdown="My Breakdown",
            breakdown_thresholds=CUSTOM_T,
            breakdown_labels={"increased": "Up"},
            breakdown_post_channel_id=7,
            breakdown_bucket_filter=["decline"],
        )
        s, rec, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert embed.title == "📊 Current Growth Breakdown Setup"
        assert embed.description == (
            "Growth breakdown is already configured. Would you like to edit these settings?"
        )
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Breakdown Tab", "My Breakdown"),
            ("Auto-Post Channel", "<#7>"),
            ("Bucket Filter", "Decline"),
            ("Custom Thresholds", "Increased ≥ 25%, Steady ≥ 12.5%, Low ≥ 3%, None ≥ 0%"),
            ("Custom Labels", "Increased→Up"),
        ]
        assert s.sent[-1] == "✅ No changes made. Your breakdown setup is still active."
        assert rec.calls == [] and _cfg()["tab_breakdown"] == "My Breakdown"

    @pytest.mark.asyncio
    async def test_summary_with_channel_and_no_filter(self, seeded_db):
        _save_growth()
        _save_breakdown(breakdown_post_channel_id=7)
        s, _, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Breakdown Tab", "Growth Breakdown"),
            ("Auto-Post Channel", "<#7>"),
            ("Bucket Filter", "Default: All but No Change"),
        ]

    @pytest.mark.asyncio
    async def test_summary_with_only_a_renamed_tab(self, seeded_db):
        _save_growth()
        _save_breakdown(tab_breakdown="Mine")
        s, _, _ = await _drive([("proceed", False)])
        embed = [e for e in s.embeds if e is not None][0]
        assert [(f.name, f.value) for f in embed.fields] == [
            ("Breakdown Tab", "Mine"),
            ("Auto-Post Channel", "❌ Off"),
            ("Bucket Filter", "Default: All but No Change"),
        ]

    @pytest.mark.asyncio
    async def test_keep_current_keeps_saved_thresholds_and_labels(self, seeded_db):
        _save_growth()
        _save_breakdown(breakdown_thresholds=CUSTOM_T, breakdown_labels=CUSTOM_L)
        s, rec, _ = await _drive(
            [
                ("proceed", True),
                ("selected", False),
                ("selected", []),
                ("choice", "keep"),
                ("choice", "keep"),
            ]
        )
        t_view, l_view = s.view_named("ThresholdsChoiceView"), s.view_named("LabelsChoiceView")
        assert [c.label for c in t_view.children] == [
            "Keep current values",
            "↩️ Use defaults",
            "✏️ Customize",
        ]
        assert [c.label for c in l_view.children] == [
            "Keep current labels",
            "↩️ Use defaults",
            "✏️ Customize",
        ]
        assert rec.kw(0)["current"] == "Growth Breakdown"
        cfg = _cfg()
        assert cfg["breakdown_thresholds"] == CUSTOM_T and cfg["breakdown_labels"] == CUSTOM_L

    @pytest.mark.asyncio
    async def test_use_defaults_clears_saved_thresholds_and_labels(self, seeded_db):
        _save_growth()
        _save_breakdown(breakdown_thresholds=CUSTOM_T, breakdown_labels=CUSTOM_L)
        await _drive([("proceed", True)] + MINIMAL)
        assert _cfg()["breakdown_thresholds"] == {} and _cfg()["breakdown_labels"] == {}

    @pytest.mark.asyncio
    async def test_summary_timeout_and_cancel_leave_the_row(self, seeded_db):
        _save_growth()
        _save_breakdown(tab_breakdown="Mine")
        s, _, _ = await _drive(["timeout"])
        assert s.sent == [""]
        s, _, _ = await _drive(["cancel"])
        assert s.sent == [""] and _cfg()["tab_breakdown"] == "Mine"


# ── Exits ────────────────────────────────────────────────────────────────────


class TestExits:
    @pytest.mark.asyncio
    async def test_timeouts(self, seeded_db):
        _save_growth()
        s, _, _ = await _drive(["timeout"])
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive(MINIMAL[:1] + ["timeout"])
        assert s.sent[-1] == TIMEOUT
        s, _, _ = await _drive(MINIMAL[:2] + ["timeout"])
        assert s.sent[-1] == THRESHOLDS_TIMEOUT
        s, _, _ = await _drive(MINIMAL[:3] + ["timeout"])
        assert s.sent[-1] == TIMEOUT
        assert _cfg()["breakdown_thresholds"] == {} and not __import__(
            "config"
        ).has_growth_breakdown_config(G)

    @pytest.mark.asyncio
    async def test_a_dismissed_thresholds_modal_reads_as_the_thresholds_timeout(self, seeded_db):
        _save_growth()
        s, _, _ = await _drive(MINIMAL[:2] + [{"choice": None, "_modal_values": None}])
        assert s.sent[-1] == THRESHOLDS_TIMEOUT

    @pytest.mark.parametrize("stop_at", [0, 1, 2, 3])
    @pytest.mark.asyncio
    async def test_cancel_is_silent(self, seeded_db, stop_at):
        _save_growth()
        s, _, _ = await _drive(MINIMAL[:stop_at] + ["cancel"])
        s.assert_never(TIMEOUT)
        s.assert_never(THRESHOLDS_TIMEOUT)
        assert not __import__("config").has_growth_breakdown_config(G)

    @pytest.mark.asyncio
    async def test_abandoned_tab_question_ends_silently(self, seeded_db):
        _save_growth()
        s, rec, _ = await _drive([], keep=[None])
        assert len(rec.calls) == 1 and s.views == []
        assert s.sent[-1].startswith("📊 **Growth Breakdown Setup**")
