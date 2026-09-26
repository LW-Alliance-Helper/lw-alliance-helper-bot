"""
The Growth Breakdown's three fixes (#668 step 2), and the screen that
carries them.

1. The bucket filter applies to every view, not just the auto-post, and a
   bucket it leaves out shows as a count instead of vanishing. With no
   filter, every bucket but No Change is listed by name.
2. A bucket too long for Discord ends "and N more" and points at the tab,
   instead of stopping mid-name.
3. Members whose every number matches last snapshot are pulled out of the
   buckets into their own list, so a Sheet nobody updated stops reading as
   an alliance that stopped growing.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.constants import TEST_GUILD_ID

import growth
from growth import BUCKET_ORDER, format_breakdown_embed


def _summary(**buckets):
    """One metric, "Power", with the given buckets filled."""
    return {"Power": {b: list(buckets.get(b, [])) for b in BUCKET_ORDER}}


def _embed(summary, metrics=("Power",), **kw):
    return format_breakdown_embed(
        metric_labels=list(metrics),
        breakdown_summary=summary,
        prev_period_label="Aug 2026",
        curr_period_label="Sep 2026",
        **kw,
    )


# ── Fix 3: the same-numbers rule ─────────────────────────────────────────────


class TestUnchangedMembers:
    def test_every_metric_at_zero_is_unchanged(self):
        pcts = {
            "Alpha Tester": [0.0, 0.0, 0.0],
            "Bravo Tester": [0.0, 1.5, 0.0],
            "Charlie Tester": [0.0, None, 0.0],
            "Delta Tester": [],
        }
        assert growth.unchanged_members(pcts) == ["Alpha Tester"]

    def test_parse_pct_reads_the_tab_s_cells(self):
        assert growth._parse_pct("12.50%") == 12.5
        assert growth._parse_pct("-3.00%") == -3.0
        assert growth._parse_pct("0.00%") == 0.0
        assert growth._parse_pct("") is None
        assert growth._parse_pct("n/a") is None


class TestReadLatestBreakdownUnchanged:
    def test_members_with_every_percentage_at_zero_come_back_as_unchanged(self):
        rows = [
            [
                "Name",
                "Aug 2026 - Sep 2026 Power %",
                "Aug 2026 - Sep 2026 Power Bucket",
                "Aug 2026 - Sep 2026 Kills %",
                "Aug 2026 - Sep 2026 Kills Bucket",
            ],
            ["Alpha Tester", "0.00%", "No Change", "0.00%", "No Change"],
            ["Bravo Tester", "0.00%", "No Change", "4.00%", "No Change"],
            ["Charlie Tester", "12.00%", "Steady", "25.00%", "Increased"],
        ]
        ws = MagicMock()
        ws.get_all_values.return_value = rows
        sh = MagicMock()
        sh.worksheet.return_value = ws
        cfg = {"metrics": [{"label": "Power"}, {"label": "Kills"}], "tab_breakdown": "B"}
        with (
            patch("config.get_growth_config", return_value=cfg),
            patch("growth._get_spreadsheet", return_value=sh),
            patch("sheet_tags.read_column_tags", return_value={}),
        ):
            out = growth.read_latest_breakdown(TEST_GUILD_ID)

        assert out["unchanged"] == ["Alpha Tester"]
        # The summary is untouched, so the Map Manager API reads what it did.
        assert out["summary"]["Power"]["none"] == ["Alpha Tester", "Bravo Tester"]


class TestAutoPostGetsUnchanged:
    def test_the_snapshot_hands_the_auto_post_its_unchanged_members(self):
        header = ["Name", "Power (Aug 2026)", "Power (Sep 2026)"]
        growth_rows = [header, ["Alpha Tester", "100", ""], ["Bravo Tester", "100", ""]]
        members = [
            {"name": "Alpha Tester", "Power": 100.0},
            {"name": "Bravo Tester", "Power": 150.0},
        ]
        ws_bd = MagicMock()
        ws_bd.get_all_values.return_value = [["Name"]]
        ws_bd.col_count = 50
        sh = MagicMock()
        sh.worksheet.return_value = ws_bd
        gcfg = {"breakdown_post_channel_id": 99, "breakdown_labels": {}}

        with (
            patch("growth.load_identity_map", return_value={}),
            patch("growth._maybe_post_breakdown") as post,
        ):
            growth._write_breakdown_for_snapshot(
                sh,
                gcfg,
                members,
                ["Power"],
                growth_rows,
                header,
                curr_period_label="Sep 2026",
                guild_id=TEST_GUILD_ID,
                all_tags={},
            )

        assert post.call_args.kwargs["unchanged"] == ["Alpha Tester"]


# ── The embed ────────────────────────────────────────────────────────────────


class TestBreakdownEmbed:
    def test_title_color_and_footer(self):
        embed = _embed(_summary(increased=["Alpha Tester"]))
        assert embed.title == "📊 Growth Breakdown: Aug 2026 to Sep 2026"
        assert "—" not in embed.title and "→" not in embed.title
        assert embed.color.value == 0x5865F2  # blurple
        assert embed.footer.text.endswith("Run `/setup` → 📊 Growth Breakdown to update settings.")

    def test_default_lists_every_bucket_but_no_change(self):
        embed = _embed(
            _summary(low=["Alpha Tester"], none=["Bravo Tester", "Charlie Tester"]),
            tab_name="My Breakdown",
        )
        value = embed.fields[0].value
        assert "**Low** (1)\nAlpha Tester" in value
        assert "**No Change** (2)" in value
        assert "Bravo Tester" not in value
        assert growth.FOOTER_COUNTS_SETTINGS in embed.footer.text
        assert "Every member is listed on the My Breakdown tab in your Sheet." in embed.footer.text

    def test_a_filter_picks_the_listed_buckets(self):
        embed = _embed(
            _summary(increased=["Alpha Tester"], none=["Bravo Tester"]),
            bucket_filter=["none"],
        )
        value = embed.fields[0].value
        assert "**No Change** (1)\nBravo Tester" in value
        assert "**Increased** (1)" in value and "Alpha Tester" not in value

    def test_show_all_lists_everything_and_needs_no_count_note(self):
        embed = _embed(_summary(none=["Bravo Tester"]), show_all=True)
        assert "**No Change** (1)\nBravo Tester" in embed.fields[0].value
        assert growth.FOOTER_COUNTS_SETTINGS not in embed.footer.text
        assert growth.FOOTER_COUNTS_TOGGLE not in embed.footer.text

    def test_the_toggle_footer_is_only_for_the_on_demand_screen(self):
        embed = _embed(_summary(none=["Bravo Tester"]), with_toggle=True)
        assert growth.FOOTER_COUNTS_TOGGLE in embed.footer.text

    def test_unchanged_members_leave_the_buckets_for_their_own_field(self):
        embed = _embed(
            _summary(none=["Alpha Tester", "Bravo Tester"], low=["Charlie Tester"]),
            unchanged=["Alpha Tester"],
        )
        first, power = embed.fields
        assert first.name == growth.FIELD_UNCHANGED
        assert first.value == "**1** member, left out of the buckets below.\nAlpha Tester"
        assert "**No Change** (1)" in power.value

    def test_a_metric_with_nobody_left_says_so(self):
        embed = _embed(_summary(none=["Alpha Tester"]), unchanged=["Alpha Tester"])
        assert embed.fields[1].value == growth.BREAKDOWN_EMPTY_METRIC

    def test_member_names_are_escaped(self):
        embed = _embed(_summary(low=["Under_Score", "Star*Name"]))
        assert "Under\\_Score" in embed.fields[0].value
        assert "Star\\*Name" in embed.fields[0].value


class TestBreakdownFitsDiscordLimits:
    """Fix 2. A full alliance of long names, all in one bucket, on five
    metrics, plus a long same-numbers list."""

    NAMES = [f"Long Member Name Tester {i:03d}" for i in range(100)]

    def test_a_long_bucket_ends_with_a_count_not_a_cut_name(self):
        embed = _embed(_summary(low=self.NAMES))
        value = embed.fields[0].value
        assert len(value) <= 1024
        assert value.endswith(" more")
        listed = value.split("\n", 1)[1].rsplit(", and ", 1)[0].split(", ")
        assert all(name in self.NAMES for name in listed)
        assert "Every member is listed on the Growth Breakdown tab" in embed.footer.text

    def test_the_whole_embed_stays_under_six_thousand(self):
        metrics = ["1st Squad Power", "2nd Squad Power", "3rd Squad Power", "Drone", "Kills"]
        summary = {m: {b: [] for b in BUCKET_ORDER} for m in metrics}
        for m in metrics:
            summary[m]["low"] = list(self.NAMES)
        embed = _embed(summary, metrics=metrics, unchanged=self.NAMES[:50], show_all=True)
        assert len(embed) <= 6000
        assert all(len(f.value) <= 1024 for f in embed.fields)

    def test_later_buckets_keep_their_headers_when_an_earlier_one_is_long(self):
        embed = _embed(_summary(increased=self.NAMES, decline=["Last Tester"]))
        value = embed.fields[0].value
        assert "**Decline** (1)" in value
        assert len(value) <= 1024


class TestHidesBuckets:
    def test_only_when_a_bucket_with_members_shows_as_a_count(self):
        hides = growth.breakdown_hides_buckets
        assert hides(_summary(none=["A"]), ["Power"], [])
        assert not hides(_summary(low=["A"]), ["Power"], [])
        assert not hides(_summary(none=["A"]), ["Power"], [], unchanged=["A"])
        assert hides(_summary(low=["A"]), ["Power"], ["none"])


# ── The on-demand screen ─────────────────────────────────────────────────────


def _data(**buckets):
    return {
        "has_data": True,
        "prev_period_label": "Aug 2026",
        "curr_period_label": "Sep 2026",
        "metric_labels": ["Power"],
        "summary": _summary(**buckets),
        "unchanged": [],
    }


def _interaction():
    inter = MagicMock()
    inter.user.id = 42
    inter.followup.send = AsyncMock(return_value=MagicMock())
    return inter


async def _send(data, gcfg=None, *, premium=True, read_error=None):
    import growth_breakdown_ui

    inter = _interaction()
    read = patch(
        "growth.read_latest_breakdown",
        side_effect=read_error,
        return_value=data,
    )
    with read, patch("premium.is_premium", AsyncMock(return_value=premium)):
        await growth_breakdown_ui.send_breakdown(
            inter, TEST_GUILD_ID, gcfg or {}, no_data="NO DATA"
        )
    return inter


class TestSendBreakdown:
    @pytest.mark.asyncio
    async def test_no_data_uses_the_caller_s_wording(self):
        inter = await _send({"has_data": False})
        inter.followup.send.assert_awaited_once_with("NO DATA", ephemeral=True)

    @pytest.mark.asyncio
    async def test_a_read_failure_never_shows_the_exception(self):
        import growth_breakdown_ui

        inter = await _send(None, read_error=RuntimeError("secret internals"))
        sent = inter.followup.send.call_args.args[0]
        assert sent == growth_breakdown_ui.LOAD_FAILED
        assert "secret" not in sent

    @pytest.mark.asyncio
    async def test_a_hidden_bucket_brings_the_toggle(self):
        import growth_breakdown_ui

        inter = await _send(_data(none=["Bravo Tester"]))
        kw = inter.followup.send.call_args.kwargs
        view = kw["view"]
        assert isinstance(view, growth_breakdown_ui.BreakdownView)
        assert view.owner_id == 42
        assert view.timeout_hint == "`/growth breakdown`"
        assert view.toggle.label == growth.BTN_SHOW_ALL
        assert view.message is inter.followup.send.return_value
        assert "Bravo Tester" not in kw["embed"].fields[0].value

    @pytest.mark.asyncio
    async def test_nothing_hidden_means_no_toggle(self):
        inter = await _send(_data(low=["Alpha Tester"]))
        assert "view" not in inter.followup.send.call_args.kwargs

    @pytest.mark.asyncio
    async def test_the_filter_applies_only_while_premium(self):
        gcfg = {"breakdown_bucket_filter": ["none"]}
        inter = await _send(_data(none=["Bravo Tester"]), gcfg, premium=True)
        assert "Bravo Tester" in inter.followup.send.call_args.kwargs["embed"].fields[0].value
        inter = await _send(_data(none=["Bravo Tester"]), gcfg, premium=False)
        assert "Bravo Tester" not in inter.followup.send.call_args.kwargs["embed"].fields[0].value

    @pytest.mark.asyncio
    async def test_the_toggle_flips_the_list_and_its_label(self):
        inter = await _send(_data(none=["Bravo Tester"]))
        view = inter.followup.send.call_args.kwargs["view"]
        edits = []

        async def _edit(interaction, **kw):
            edits.append(kw)

        with patch("wizard_registry.safe_edit_response", side_effect=_edit):
            await view.toggle.callback(MagicMock())
            assert "Bravo Tester" in edits[-1]["embed"].fields[0].value
            assert view.toggle.label == growth.BTN_SHOW_DEFAULT
            await view.toggle.callback(MagicMock())
            assert "Bravo Tester" not in edits[-1]["embed"].fields[0].value
            assert view.toggle.label == growth.BTN_SHOW_ALL

    @pytest.mark.asyncio
    async def test_with_a_filter_the_way_back_names_the_filter(self):
        inter = await _send(_data(low=["Alpha Tester"]), {"breakdown_bucket_filter": ["none"]})
        view = inter.followup.send.call_args.kwargs["view"]
        with patch("wizard_registry.safe_edit_response", AsyncMock()):
            await view.toggle.callback(MagicMock())
        assert view.toggle.label == growth.BTN_SHOW_FILTER
