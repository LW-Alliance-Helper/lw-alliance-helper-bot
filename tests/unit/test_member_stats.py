"""Unit tests for /my_stats + /member_stats (#56): roster/Discord resolution,
the section fetchers (identity, the existing-Growth-tab power reader, storm
attendance, train, surveys), leadership picker + pagination, and embed
assembly. Storm sign-up counts and roster placement are a tracked follow-up."""

from unittest.mock import MagicMock, patch

import member_stats as ms

GUILD = 1

# A roster: header + two members. Cols: discord_id, name, display, joined, roles
ROSTER = [
    ["Discord ID", "Name", "Display Name", "Joined", "Roles"],
    ["111", "bob_acct", "Bob", "2025-08-12", "Member"],
    ["", "Charlie", "Charlie", "2025-09-01", "Member"],  # manual (no discord id)
]


def _roster_cfg():
    return {
        "tab_name": "Member Roster",
        "discord_id_col": 0,
        "name_col": 1,
        "display_col": 2,
        "joined_col": 3,
        "roles_col": 4,
    }


def _patch_roster():
    return (
        patch("config.get_member_roster_config", return_value=_roster_cfg()),
        patch("config.read_member_roster_values", return_value=ROSTER),
    )


# ── Resolution ───────────────────────────────────────────────────────────────


class TestResolution:
    def test_resolve_self_by_discord_id(self):
        with _patch_roster()[0], _patch_roster()[1]:
            t = ms._resolve_self(GUILD, 111)
        assert t is not None
        assert t.name == "Bob"
        assert t.discord_id == 111
        assert t.joined == "2025-08-12"
        assert t.is_manual is False

    def test_resolve_self_not_on_roster(self):
        with _patch_roster()[0], _patch_roster()[1]:
            assert ms._resolve_self(GUILD, 999) is None

    def test_resolve_named_by_display(self):
        with _patch_roster()[0], _patch_roster()[1]:
            t = ms._resolve_named(GUILD, "bob")
        assert t is not None and t.name == "Bob" and t.discord_id == 111

    def test_resolve_named_manual_member(self):
        with _patch_roster()[0], _patch_roster()[1]:
            t = ms._resolve_named(GUILD, "charlie")
        assert t is not None
        assert t.discord_id is None
        assert t.is_manual is True

    def test_resolve_named_not_found(self):
        with _patch_roster()[0], _patch_roster()[1]:
            assert ms._resolve_named(GUILD, "nobody") is None

    def test_roster_names_autocomplete_prefix(self):
        with _patch_roster()[0], _patch_roster()[1]:
            assert ms._roster_names(GUILD, prefix="bo") == ["Bob"]
            assert set(ms._roster_names(GUILD)) == {"Bob", "Charlie"}


# ── Identity ─────────────────────────────────────────────────────────────────


class TestIdentity:
    def test_identity_with_birthday(self):
        target = ms.Target(name="Bob", discord_id=111, joined="2025-08-12")
        with (
            patch("config.get_birthday_config", return_value={"enabled": 1, "tab_name": "B"}),
            patch("train.load_birthdays", return_value=[{"name": "Bob", "month": 3, "day": 15}]),
        ):
            val = ms._identity_field(GUILD, target)
        assert "<@111>" in val
        assert "Joined 2025-08-12" in val
        assert "🎂 March 15" in val

    def test_identity_manual_member_note(self):
        target = ms.Target(name="Charlie", discord_id=None, joined="2025-09-01")
        with patch("config.get_birthday_config", return_value={"enabled": 0}):
            val = ms._identity_field(GUILD, target)
        assert "**Charlie**" in val
        assert ms._MANUAL_MEMBER_NOTE in val


# ── Power (the existing Growth Tracking tab reader) ──────────────────────────


class TestPowerField:
    def _growth_sheet(self):
        header = [
            "Name",
            "1st Squad Power (May 2026)",
            "THP (May 2026)",
            "1st Squad Power (Jun 2026)",
            "THP (Jun 2026)",
        ]
        rows = [header, ["Bob", "84100000", "400000000", "87300000", "412000000"]]
        ws = MagicMock()
        ws.get_all_values.return_value = rows
        sh = MagicMock()
        sh.worksheet.return_value = ws
        return sh

    def test_reads_latest_and_delta_from_existing_tab(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        gcfg = {
            "enabled": 1,
            "tab_growth": "Growth Tracking",
            "metrics": [{"label": "1st Squad Power"}, {"label": "THP"}],
        }
        with (
            patch("config.get_growth_config", return_value=gcfg),
            patch("growth._get_spreadsheet", return_value=self._growth_sheet()),
        ):
            val = ms._power_field(GUILD, target)
        assert val is not None
        # latest Jun value + change since prev (May)
        assert "**1st Squad Power:** 87.3M (+3.2M since May 2026)" in val
        assert "**THP:** 412.0M (+12.0M since May 2026)" in val

    def test_disabled_growth_hides_section(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        with patch("config.get_growth_config", return_value={"enabled": 0}):
            assert ms._power_field(GUILD, target) is None

    def test_member_not_in_growth_tab(self):
        target = ms.Target(name="Nobody", discord_id=1, joined="")
        gcfg = {"enabled": 1, "tab_growth": "G", "metrics": [{"label": "THP"}]}
        with (
            patch("config.get_growth_config", return_value=gcfg),
            patch("growth._get_spreadsheet", return_value=self._growth_sheet()),
        ):
            assert ms._power_field(GUILD, target) is None


# ── Train ────────────────────────────────────────────────────────────────────


class TestTrainField:
    def _history(self):
        import train_rotation as tr

        return [
            tr.HistoryRow(
                date="2026-04-15",
                member="Bob",
                reason="auto",
                status=tr.STATUS_POSTED,
                posted_at="2026-04-15",
            ),
            tr.HistoryRow(
                date="2026-05-01",
                member="Bob",
                reason="vs",
                status=tr.STATUS_POSTED,
                posted_at="2026-05-01",
            ),
            tr.HistoryRow(
                date="2026-03-01",
                member="Bob",
                reason="birthday",
                status=tr.STATUS_POSTED,
                posted_at="2026-03-01",
            ),
        ]

    def test_member_view_count_and_last_drove(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        with (
            patch(
                "config.get_train_config",
                return_value={"history_tab": "Train History", "counted_reasons": ""},
            ),
            patch("train_rotation.load_history", return_value=self._history()),
        ):
            val = ms._train_field(GUILD, target, leadership_view=False)
        assert "Conductor: **2** times" in val  # auto + vs counted, birthday excluded
        assert "last drove 2026-05-01" in val
        assert "Reason breakdown" not in val  # leadership-only

    def test_leadership_view_adds_reason_breakdown(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        with (
            patch(
                "config.get_train_config",
                return_value={"history_tab": "Train History", "counted_reasons": ""},
            ),
            patch("train_rotation.load_history", return_value=self._history()),
        ):
            val = ms._train_field(GUILD, target, leadership_view=True)
        assert "Reason breakdown" in val
        assert "auto 1" in val and "vs 1" in val and "birthday 1" in val

    def test_no_history_hides_section(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        with (
            patch(
                "config.get_train_config", return_value={"history_tab": "T", "counted_reasons": ""}
            ),
            patch("train_rotation.load_history", return_value=[]),
        ):
            assert ms._train_field(GUILD, target, leadership_view=False) is None


# ── Embed assembly ───────────────────────────────────────────────────────────


class TestBuildEmbed:
    def test_identity_always_present(self):
        target = ms.Target(name="Bob", discord_id=111, joined="2025-08-12")
        with (
            patch("config.get_birthday_config", return_value={"enabled": 0}),
            patch("config.get_growth_config", return_value={"enabled": 0}),
            patch(
                "config.get_train_config", return_value={"history_tab": "T", "counted_reasons": ""}
            ),
            patch("train_rotation.load_history", return_value=[]),
        ):
            embed = ms.build_embed(GUILD, target, leadership_view=False)
        assert embed.title == "👤 Bob's Member Stats"
        names = [f.name for f in embed.fields]
        assert "Identity" in names
        # No tracked activity -> empty-state pointer field
        assert any("Nothing tracked here yet" in (f.value or "") for f in embed.fields)


# ── Surveys ──────────────────────────────────────────────────────────────────


class TestSurveyField:
    def _history_sheet(self, rows):
        ws = MagicMock()
        ws.get_all_values.return_value = rows
        sh = MagicMock()
        sh.worksheet.return_value = ws
        return sh

    def test_last_response_by_discord_id(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        rows = [
            ["Timestamp", "Discord ID", "Username", "Q1"],
            ["3/15/2026 10:00 UTC", "111", "Bob", "x"],
            ["5/1/2026 09:30 UTC", "111", "Bob", "y"],  # latest
            ["4/1/2026 09:30 UTC", "222", "Other", "z"],
        ]
        with (
            patch(
                "config.list_surveys",
                return_value=[
                    {
                        "survey_id": "default",
                        "survey_name": "Squad Power",
                        "tab_history": "Survey History",
                    }
                ],
            ),
            patch("config.get_spreadsheet", return_value=self._history_sheet(rows)),
        ):
            val = ms._survey_field(GUILD, target)
        assert val == "**Squad Power:** last response May 1, 2026"

    def test_manual_member_matched_by_username(self):
        target = ms.Target(name="Charlie", discord_id=None, joined="")
        rows = [
            ["Timestamp", "Discord ID", "Username", "Q1"],
            ["2/2/2026 08:00 UTC", "", "Charlie", "a"],
        ]
        with (
            patch(
                "config.list_surveys",
                return_value=[{"survey_name": "Default", "tab_history": "Survey History"}],
            ),
            patch("config.get_spreadsheet", return_value=self._history_sheet(rows)),
        ):
            val = ms._survey_field(GUILD, target)
        assert "last response Feb 2, 2026" in val

    def test_no_responses_hides_section(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        rows = [["Timestamp", "Discord ID", "Username"], ["5/1/2026 09:30 UTC", "999", "Nope"]]
        with (
            patch(
                "config.list_surveys",
                return_value=[{"survey_name": "Default", "tab_history": "Survey History"}],
            ),
            patch("config.get_spreadsheet", return_value=self._history_sheet(rows)),
        ):
            assert ms._survey_field(GUILD, target) is None

    def test_parse_ts_variants(self):
        assert ms._parse_survey_ts("5/1/2026 09:30 UTC") is not None
        assert ms._parse_survey_ts("5/1/2026") is not None
        assert ms._parse_survey_ts("garbage") is None


# ── Storm sign-ups + attendance ──────────────────────────────────────────────


class TestStormField:
    def test_attendance_only(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        ds = (
            ["2026-06-05", "2026-05-29", "2026-05-22"],
            {"Bob": {"2026-06-05": "yes", "2026-05-29": "no", "2026-05-22": "yes"}},
        )
        cs = ([], {})
        with (
            patch("config.get_recent_storm_registration_posts", return_value=[]),
            patch("storm_log.read_member_log_window", side_effect=[ds, cs]),
        ):
            val = ms._storm_field(GUILD, target, leadership_view=False)
        assert "**Desert Storm:** attended 2 of 3 (67%)" in val
        assert "Canyon Storm" not in val

    def test_signups_count_available_votes(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        posts = [
            {"guild_id": GUILD, "event_type": "DS", "event_date": "2026-06-05"},
            {"guild_id": GUILD, "event_type": "DS", "event_date": "2026-05-29"},
            {"guild_id": GUILD, "event_type": "DS", "event_date": "2026-05-22"},
        ]
        votes = {"2026-06-05": "a", "2026-05-29": "cannot", "2026-05-22": "either"}

        def _vote(g, et, d, mid):
            return {"vote": votes[d]}

        with (
            patch("config.get_recent_storm_registration_posts", return_value=posts),
            patch("config.get_member_vote", side_effect=_vote),
            patch("storm_log.read_member_log_window", return_value=([], {})),
        ):
            val = ms._storm_field(GUILD, target, leadership_view=False)
        # "a" + "either" count as available, "cannot" does not -> 2 of 3
        assert "**Desert Storm:** signed up 2 of 3 (67%)" in val

    def test_signups_and_attendance_combined(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        posts = [{"guild_id": GUILD, "event_type": "DS", "event_date": "2026-06-05"}]
        ds_att = (["2026-06-05"], {"Bob": {"2026-06-05": "yes"}})
        with (
            patch("config.get_recent_storm_registration_posts", return_value=posts),
            patch("config.get_member_vote", return_value={"vote": "a"}),
            patch("storm_log.read_member_log_window", side_effect=[ds_att, ([], {})]),
        ):
            val = ms._storm_field(GUILD, target, leadership_view=False)
        assert "signed up 1 of 1 (100%)" in val and "attended 1 of 1 (100%)" in val

    def test_manual_member_skips_signups(self):
        target = ms.Target(name="Charlie", discord_id=None, joined="")
        assert ms._storm_signups_for_member(GUILD, "DS", None) is None

    def test_leadership_placement_counts(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        posts = [
            {"guild_id": GUILD, "event_type": "DS", "event_date": d}
            for d in ("d1", "d2", "d3", "d4")
        ]
        plans = {
            "d1": {"A": {"primaries": ["111"], "subs": []}},  # primary
            "d2": {"A": {"primaries": [], "subs": ["111"]}},  # sub
            "d3": {"A": {"primaries": ["999"], "subs": []}},  # not placed
            "d4": {},  # no plan for this event -> skipped
        }

        def _plans(g, et, d):
            return plans.get(d, {})

        def _vote(g, et, d, mid):
            return {"vote": "a"} if d == "d3" else None  # available but unplaced on d3

        with (
            patch("config.get_recent_storm_registration_posts", return_value=posts),
            patch("config.get_storm_team_plans_for_event", side_effect=_plans),
            patch("config.get_member_vote", side_effect=_vote),
            patch("storm_log.read_member_log_window", return_value=([], {})),
        ):
            val = ms._storm_field(GUILD, target, leadership_view=True)
        assert "placed: 1 primary, 1 sub, 1 sat out" in val

    def test_placement_skipped_for_manual_member(self):
        assert ms._storm_placement_for_member(GUILD, "DS", None) is None

    def test_nothing_tracked_hides(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        with (
            patch("config.get_recent_storm_registration_posts", return_value=[]),
            patch("storm_log.read_member_log_window", return_value=([], {})),
        ):
            assert ms._storm_field(GUILD, target, leadership_view=False) is None

    def test_truthy_helper(self):
        assert ms._storm_truthy("yes") is True
        assert ms._storm_truthy("no") is False
        assert ms._storm_truthy("") is False
        assert ms._storm_truthy("0") is False


# ── Train is leadership-only (#56 sensitivity) ───────────────────────────────


class TestTrainGating:
    def _history(self):
        import train_rotation as tr

        return [
            tr.HistoryRow(
                date="2026-05-01",
                member="Bob",
                reason="vs",
                status=tr.STATUS_POSTED,
                posted_at="2026-05-01",
            )
        ]

    def _patches(self):
        return (
            patch("config.get_birthday_config", return_value={"enabled": 0}),
            patch("config.get_growth_config", return_value={"enabled": 0}),
            patch(
                "config.get_train_config", return_value={"history_tab": "T", "counted_reasons": ""}
            ),
            patch("train_rotation.load_history", return_value=self._history()),
        )

    def test_member_view_hides_train_entirely(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        ps = self._patches()
        with ps[0], ps[1], ps[2], ps[3]:
            embed = ms.build_embed(GUILD, target, leadership_view=False)
        assert not any(f.name == "🚂 Train" for f in embed.fields)

    def test_leadership_view_shows_train(self):
        target = ms.Target(name="Bob", discord_id=111, joined="")
        ps = self._patches()
        with ps[0], ps[1], ps[2], ps[3]:
            embed = ms.build_embed(GUILD, target, leadership_view=True)
        assert any(f.name == "🚂 Train" for f in embed.fields)


# ── Self resolution from Discord identity (free-tier, no roster) ──────────────


class TestDiscordIdentity:
    def test_target_from_member(self):
        import datetime

        member = MagicMock()
        member.id = 777
        member.display_name = "Dana"
        member.joined_at = datetime.datetime(2025, 7, 1)
        t = ms._target_from_discord(member)
        assert t.name == "Dana"
        assert t.discord_id == 777
        assert t.joined == "2025-07-01"


# ── Leadership picker source (roster vs tracked-data fallback) ────────────────


class TestLeadershipMemberList:
    def test_uses_roster_when_enabled(self):
        rcfg = dict(_roster_cfg(), enabled=1)
        with (
            patch("config.get_member_roster_config", return_value=rcfg),
            patch("config.read_member_roster_values", return_value=ROSTER),
        ):
            names, has_roster = ms._leadership_member_list(GUILD)
        assert has_roster is True
        assert set(names) == {"Bob", "Charlie"}

    def test_falls_back_to_tracked_data(self):
        rcfg = dict(_roster_cfg(), enabled=0)
        with (
            patch("config.get_member_roster_config", return_value=rcfg),
            patch.object(ms, "_tracked_data_names", return_value=["Eve", "Frank"]),
        ):
            names, has_roster = ms._leadership_member_list(GUILD)
        assert has_roster is False
        assert names == ["Eve", "Frank"]


# ── Picker view: pagination + no-roster notice ───────────────────────────────


class TestMemberPickerView:
    def test_single_page_no_nav(self):
        view = ms.MemberPickerView(GUILD, ["A", "B", "C"], no_roster=False)
        assert view.total_pages == 1
        selects = [c for c in view.children if isinstance(c, discord_ui_select())]
        assert len(selects) == 1 and len(selects[0].options) == 3
        # no Prev/Next on a single page
        assert not [c for c in view.children if isinstance(c, discord_ui_button())]

    def test_paginates_beyond_25(self):
        names = [f"M{i}" for i in range(30)]
        view = ms.MemberPickerView(GUILD, names, no_roster=False)
        assert view.total_pages == 2
        selects = [c for c in view.children if isinstance(c, discord_ui_select())]
        assert len(selects[0].options) == 25  # first page full
        # Prev/Next present
        assert len([c for c in view.children if isinstance(c, discord_ui_button())]) == 2

    def test_no_roster_notice_points_to_member_sync(self):
        view = ms.MemberPickerView(GUILD, ["A"], no_roster=True)
        note = view.notice()
        assert "No member roster is set up" in note
        assert "Member Sync" in note and "/upgrade" in note

    def test_roster_present_no_warning(self):
        view = ms.MemberPickerView(GUILD, ["A"], no_roster=False)
        assert "No member roster" not in view.notice()


def discord_ui_select():
    import discord

    return discord.ui.Select


def discord_ui_button():
    import discord

    return discord.ui.Button


# ── Number formatting ────────────────────────────────────────────────────────


class TestFmtNum:
    def test_formats(self):
        assert ms._fmt_num(87_300_000) == "87.3M"
        assert ms._fmt_num(1_200_000_000) == "1.2B"
        assert ms._fmt_num(304_743) == "304,743"
        assert ms._fmt_num(950) == "950"
