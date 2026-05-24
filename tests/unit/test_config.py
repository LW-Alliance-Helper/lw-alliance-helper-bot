"""
Unit tests for config.py — database schema, save/load round-trips,
per-guild config functions, migrations.
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


class TestGuildConfig:
    """Test GuildConfig save/load round-trips."""

    def test_get_or_create_returns_default(self, temp_db):
        import config
        cfg = config.get_or_create_config(TEST_GUILD_ID)
        assert cfg.guild_id == TEST_GUILD_ID
        assert cfg.setup_complete == False
        assert cfg.timezone == "America/New_York"

    def test_save_and_reload(self, temp_db):
        import config
        cfg = config.get_or_create_config(TEST_GUILD_ID)
        cfg.member_role_name      = "Alliance Member"
        cfg.leadership_role_name  = "R4/R5"
        cfg.timezone              = "America/Chicago"
        cfg.spreadsheet_id        = "abc123xyz"
        cfg.setup_complete        = True
        config.save_config(cfg)

        loaded = config.get_config(TEST_GUILD_ID)
        assert loaded.member_role_name     == "Alliance Member"
        assert loaded.leadership_role_name == "R4/R5"
        assert loaded.timezone             == "America/Chicago"
        assert loaded.spreadsheet_id       == "abc123xyz"
        assert loaded.setup_complete       == True

    def test_get_config_returns_none_for_unknown_guild(self, temp_db):
        import config
        result = config.get_config(9999999999999999)
        assert result is None

    def test_update_config_field(self, temp_db):
        import config
        config.get_or_create_config(TEST_GUILD_ID)
        config.update_config_field(TEST_GUILD_ID, "timezone", "Europe/London")
        cfg = config.get_config(TEST_GUILD_ID)
        assert cfg.timezone == "Europe/London"

    def test_is_setup_complete_false_by_default(self, temp_db):
        import config
        config.get_or_create_config(TEST_GUILD_ID)
        assert config.is_setup_complete(TEST_GUILD_ID) == False

    def test_is_setup_complete_true_after_save(self, temp_db):
        import config
        cfg = config.get_or_create_config(TEST_GUILD_ID)
        cfg.setup_complete = True
        config.save_config(cfg)
        assert config.is_setup_complete(TEST_GUILD_ID) == True

    def test_multiple_guilds_isolated(self, temp_db):
        import config
        guild_a = 1000000000000000001
        guild_b = 1000000000000000002
        cfg_a   = config.get_or_create_config(guild_a)
        cfg_b   = config.get_or_create_config(guild_b)
        cfg_a.timezone = "America/New_York"
        cfg_b.timezone = "Asia/Tokyo"
        config.save_config(cfg_a)
        config.save_config(cfg_b)

        assert config.get_config(guild_a).timezone == "America/New_York"
        assert config.get_config(guild_b).timezone == "Asia/Tokyo"


class TestTrainConfig:
    """Test guild_train_config save/load."""

    def test_default_train_config(self, temp_db):
        import config
        cfg = config.get_train_config(TEST_GUILD_ID)
        assert cfg["tab_name"] == "Train Schedule"
        assert isinstance(cfg["themes"], list)
        assert len(cfg["themes"]) > 0
        assert cfg["reminders_enabled"] in (0, 1)

    def test_save_and_reload_train_config(self, temp_db):
        import config
        themes = ["Welcome", "Birthday", "Custom"]
        tones  = ["Default", "Casual", "Intense"]
        config.save_train_config(
            TEST_GUILD_ID,
            tab_name="My Train Tab",
            themes=themes,
            tones=tones,
            prompt_template="Write a blurb for {name}.",
            default_tone="Default",
            blurbs_enabled=1,
            reminders_enabled=1,
            reminder_channel_id=123456,
            reminder_time="22:00",
        )
        cfg = config.get_train_config(TEST_GUILD_ID)
        assert cfg["tab_name"]            == "My Train Tab"
        assert cfg["themes"]              == themes
        assert cfg["tones"]               == tones
        assert cfg["prompt_template"]     == "Write a blurb for {name}."
        assert cfg["default_tone"]        == "Default"
        assert cfg["blurbs_enabled"]      == 1
        assert cfg["reminders_enabled"]   == 1
        assert cfg["reminder_channel_id"] == 123456
        assert cfg["reminder_time"]       == "22:00"


class TestBirthdayConfig:
    """Test guild_birthday_config save/load."""

    def test_default_birthday_config(self, temp_db):
        import config
        cfg = config.get_birthday_config(TEST_GUILD_ID)
        assert cfg["enabled"]      == 0
        assert cfg["tab_name"]     == "Birthdays"
        assert cfg["name_col"]     == 0
        assert cfg["birthday_col"] == 1

    def test_save_and_reload_birthday_config(self, temp_db):
        import config
        config.save_birthday_config(
            guild_id=TEST_GUILD_ID,
            tab_name="Member Roster",
            name_col=3,
            birthday_col=7,
            discord_id_col=2,
            data_start_row=2,
            enabled=1,
            train_integration=1,
            flexible_placement=1,
            lookahead_days=21,
            reminders_enabled=1,
            reminder_channel_id=777777,
            reminder_time="08:00",
        )
        cfg = config.get_birthday_config(TEST_GUILD_ID)
        assert cfg["tab_name"]           == "Member Roster"
        assert cfg["name_col"]           == 3
        assert cfg["birthday_col"]       == 7
        assert cfg["discord_id_col"]     == 2
        assert cfg["lookahead_days"]     == 21
        assert cfg["train_integration"]  == 1
        assert cfg["flexible_placement"] == 1
        assert cfg["reminders_enabled"]  == 1
        assert cfg["reminder_channel_id"]== 777777
        assert cfg["reminder_time"]      == "08:00"


class TestStormConfig:
    """Test guild_storm_config save/load."""

    def test_default_storm_config(self, temp_db):
        import config
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["event_type"]  == "DS"
        assert "mail_template" in cfg

    def test_save_and_reload_storm_config(self, temp_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Zones",
            mail_template="**{alliance_name}**\n{zones}\n{subs}\n{time}",
            timezone="America/New_York",
            log_channel_id=888888,
        )
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["tab_name"]        == "DS Zones"
        assert "{alliance_name}" in cfg["mail_template"]
        assert cfg["log_channel_id"]  == 888888

    def test_ds_and_cs_isolated(self, temp_db):
        import config
        config.save_storm_config(TEST_GUILD_ID, "DS", "DS Tab", "DS template",
                                 "America/New_York", 0)
        config.save_storm_config(TEST_GUILD_ID, "CS", "CS Tab", "CS template",
                                 "America/New_York", 0)
        ds = config.get_storm_config(TEST_GUILD_ID, "DS")
        cs = config.get_storm_config(TEST_GUILD_ID, "CS")
        assert ds["tab_name"]      == "DS Tab"
        assert cs["tab_name"]      == "CS Tab"
        assert ds["mail_template"] != cs["mail_template"]


class TestPowerDataSourceFields:
    """#226 follow-up — `power_metric_tab` + `power_match_column` are
    new fields on `guild_storm_config` that flex where storm reads
    power from. Empty values preserve the pre-flexibility default
    (read from Member Roster keyed by discord_id_col)."""

    def test_default_values_are_empty_strings(self, temp_db):
        import config
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["power_metric_tab"] == ""
        assert cfg["power_match_column"] == ""

    def test_round_trip_preserves_tab_and_match_column(self, temp_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            power_metric_column="B",
            power_metric_tab="Squad Powers",
            power_match_column="A",
        )
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["power_metric_tab"] == "Squad Powers"
        assert cfg["power_match_column"] == "A"

    def test_invalid_match_letter_normalised_to_empty(self, temp_db):
        """Bad input (`"AB"`, `"7"`, etc.) coerces to empty string so
        the read path falls back to the safe default instead of
        saving garbage."""
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            power_match_column="7",
        )
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["power_match_column"] == ""

    def test_existing_alliance_power_metric_column_preserved(self, temp_db):
        """Migration safety: a pre-flexibility alliance that saved
        `power_metric_column="F"` still reads "F" correctly after the
        new fields land."""
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            power_metric_column="F",
            # tab + match_column not passed → empty defaults
        )
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["power_metric_column"] == "F"
        assert cfg["power_metric_tab"] == ""
        assert cfg["power_match_column"] == ""


class TestRosterDmTemplates:
    """#226 follow-up — three per-(guild, event_type) DM templates for
    the Approve & Post DM-the-roster flow. Empty saved value falls
    back to the hardcoded default in defaults.py at send time."""

    def test_unset_returns_empty_strings(self, temp_db):
        import config
        # An unconfigured guild reads as all-empty so the DM composer
        # falls through to the hardcoded defaults.
        tpls = config.get_roster_dm_templates(TEST_GUILD_ID, "DS")
        assert tpls == {"starter": "", "paired_sub": "", "pool_sub": ""}

    def test_save_and_reload_roundtrip(self, temp_db):
        import config
        # Save_storm_config first so the row exists for the UPDATE.
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_roster_dm_templates(
            TEST_GUILD_ID, "DS",
            starter="Hi {name}, you're a Starter.",
            paired_sub="Hi {name}, you're a Sub for {assignments}.",
            pool_sub="Hi {name}, standby pool.",
        )
        tpls = config.get_roster_dm_templates(TEST_GUILD_ID, "DS")
        assert tpls["starter"]    == "Hi {name}, you're a Starter."
        assert tpls["paired_sub"] == "Hi {name}, you're a Sub for {assignments}."
        assert tpls["pool_sub"]   == "Hi {name}, standby pool."

    def test_ds_and_cs_isolated(self, temp_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS", mail_template="",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_storm_config(
            TEST_GUILD_ID, "CS",
            tab_name="CS", mail_template="",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_roster_dm_templates(
            TEST_GUILD_ID, "DS",
            starter="DS-only Starter",
            paired_sub="", pool_sub="",
        )
        ds = config.get_roster_dm_templates(TEST_GUILD_ID, "DS")
        cs = config.get_roster_dm_templates(TEST_GUILD_ID, "CS")
        assert ds["starter"] == "DS-only Starter"
        assert cs["starter"] == ""  # untouched CS row stays empty


class TestStructuredStormConfig:
    """Test the #38 + #54 structured storm flow config layer."""

    def test_default_structured_flow_off(self, temp_db):
        import config
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["structured_flow_enabled"] is False
        assert cfg["sub_mode"]                == "pool"
        assert cfg["power_metric_column"]     == "B"

    def test_tab_defaults_are_event_type_aware(self, temp_db):
        import config
        ds = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        cs = config.get_structured_storm_config(TEST_GUILD_ID, "CS")
        assert ds["signups_tab"]      == "DS Signups"
        assert ds["rosters_tab"]      == "DS Rosters"
        assert ds["strategies_tab"]   == "DS Strategies"
        assert ds["member_rules_tab"] == "DS Member Rules"
        assert ds["attendance_tab"]   == "DS Attendance"
        assert cs["signups_tab"]      == "CS Signups"
        assert cs["rosters_tab"]      == "CS Rosters"
        assert cs["strategies_tab"]   == "CS Strategies"
        assert cs["member_rules_tab"] == "CS Member Rules"
        assert cs["attendance_tab"]   == "CS Attendance"

    def test_save_requires_existing_row(self, temp_db):
        """save_structured_storm_config UPDATEs an existing row; returns
        False when there's no row to update."""
        import config
        updated = config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
        )
        assert updated is False

    def test_save_and_reload_round_trip(self, temp_db):
        import config
        # Create the row first via save_storm_config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Zones",
            mail_template="x",
            timezone="America/New_York",
            log_channel_id=0,
        )
        updated = config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            power_metric_column="F",
            sub_mode="paired",
            signup_channel_id=12345,
            signup_schedule_cron="0 14 * * 0",
            signups_tab="Sign Ups DS",
            rosters_tab="Rosters DS",
            attendance_tab="Attendance DS",
            strategies_tab="Strategies DS",
            member_rules_tab="Rules DS",
        )
        assert updated is True

        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["structured_flow_enabled"] is True
        assert cfg["power_metric_column"]     == "F"
        assert cfg["sub_mode"]                == "paired"
        assert cfg["signup_channel_id"]       == 12345
        assert cfg["signup_schedule_cron"]    == "0 14 * * 0"
        assert cfg["signups_tab"]             == "Sign Ups DS"
        assert cfg["rosters_tab"]             == "Rosters DS"
        assert cfg["attendance_tab"]          == "Attendance DS"
        assert cfg["strategies_tab"]          == "Strategies DS"
        assert cfg["member_rules_tab"]        == "Rules DS"

    def test_empty_tab_falls_back_to_default(self, temp_db):
        """Saving an empty tab name reads back as the event-type default."""
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "CS",
            tab_name="CS Zones",
            mail_template="x",
            timezone="America/New_York",
            log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "CS",
            structured_flow_enabled=True,
            signups_tab="",  # leave blank → default
        )
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "CS")
        assert cfg["signups_tab"] == "CS Signups"

    def test_invalid_sub_mode_normalised_to_pool(self, temp_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Zones", mail_template="x",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            sub_mode="garbage",
        )
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["sub_mode"] == "pool"

    def test_ds_and_cs_structured_isolated(self, temp_db):
        import config
        for event_type in ("DS", "CS"):
            config.save_storm_config(
                TEST_GUILD_ID, event_type,
                tab_name=f"{event_type} Tab", mail_template="x",
                timezone="America/New_York", log_channel_id=0,
            )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            power_metric_column="F",
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "CS",
            structured_flow_enabled=False,
            power_metric_column="G",
        )
        ds = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        cs = config.get_structured_storm_config(TEST_GUILD_ID, "CS")
        assert ds["structured_flow_enabled"] is True
        assert cs["structured_flow_enabled"] is False
        assert ds["power_metric_column"] == "F"
        assert cs["power_metric_column"] == "G"

    def test_default_structured_tab_helper(self):
        import config
        assert config.default_structured_tab("DS", "signups_tab") == "DS Signups"
        assert config.default_structured_tab("CS", "rosters_tab") == "CS Rosters"
        # Unknown event_type / field returns empty string (no crash).
        assert config.default_structured_tab("XX", "signups_tab") == ""
        assert config.default_structured_tab("DS", "unknown_field") == ""

    def test_schedule_fields_round_trip(self, temp_db):
        """Auto-scheduler fields (post-Rule H / #164): poll_day_of_week +
        signup_time. Event day is game-defined, no longer stored."""
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Zones", mail_template="x",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            poll_day_of_week=2, signup_time="14:00",
        )
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["poll_day_of_week"] == 2
        assert cfg["signup_time"]      == "14:00"

    def test_schedule_dow_out_of_range_normalises_to_negative_one(self, temp_db):
        """Save should reject DOW > 6 or < -1 by normalising to -1 — wizard
        validates first but defense in depth at the storage layer."""
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Zones", mail_template="x",
            timezone="America/New_York", log_channel_id=0,
        )
        config.save_structured_storm_config(
            TEST_GUILD_ID, "DS",
            structured_flow_enabled=True,
            poll_day_of_week=42, signup_time="14:00",
        )
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["poll_day_of_week"] == -1

    def test_schedule_default_unconfigured(self, temp_db):
        """Never-configured schedule reads as poll_dow=-1, time=''."""
        import config
        cfg = config.get_structured_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["poll_day_of_week"] == -1
        assert cfg["signup_time"]      == ""


class TestPowerRefreshDmCooldown:
    """#138 — `storm_power_refresh_dms_sent` is the persistent cooldown
    that gates the power-refresh DM nudge from firing twice for the
    same member per event."""

    def test_unsent_returns_false(self, temp_db):
        import config
        assert config.has_power_refresh_dm_been_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 12345,
        ) is False

    def test_record_then_check(self, temp_db):
        import config
        fresh = config.record_power_refresh_dm_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 12345,
        )
        assert fresh is True
        assert config.has_power_refresh_dm_been_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 12345,
        ) is True

    def test_re_record_idempotent(self, temp_db):
        import config
        first = config.record_power_refresh_dm_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 12345,
        )
        second = config.record_power_refresh_dm_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 12345,
        )
        # First insert returns True (new row); second returns False
        # (the cooldown blocked it). Both leave the table consistent.
        assert first is True
        assert second is False

    def test_per_member_isolation(self, temp_db):
        import config
        config.record_power_refresh_dm_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 12345,
        )
        # Different member same event → still firstable.
        assert config.has_power_refresh_dm_been_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 99999,
        ) is False

    def test_per_event_date_isolation(self, temp_db):
        import config
        config.record_power_refresh_dm_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 12345,
        )
        # Same member, different event_date → firstable.
        assert config.has_power_refresh_dm_been_sent(
            TEST_GUILD_ID, "DS", "2026-05-25", 12345,
        ) is False

    def test_per_event_type_isolation(self, temp_db):
        import config
        config.record_power_refresh_dm_sent(
            TEST_GUILD_ID, "DS", "2026-05-18", 12345,
        )
        # Same member, different event_type → firstable.
        assert config.has_power_refresh_dm_been_sent(
            TEST_GUILD_ID, "CS", "2026-05-18", 12345,
        ) is False


class TestStormSignups:
    """Test the #123 storm_signups + storm_registration_posts tables."""

    def test_record_vote_round_trip(self, temp_db):
        import config
        ok = config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=111, target_member_id="111", vote="a",
            channel_id=222, message_id=333,
        )
        assert ok is True
        row = config.get_member_vote(TEST_GUILD_ID, "DS", "2026-05-18", "111")
        assert row is not None
        assert row["vote"]            == "a"
        assert row["voter_user_id"]   == 111
        assert row["is_on_behalf"]    is False
        assert row["channel_id"]      == 222
        assert row["message_id"]      == 333

    def test_revote_replaces_prior(self, temp_db):
        import config
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=111, target_member_id="111", vote="a",
        )
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=111, target_member_id="111", vote="cannot",
        )
        rows = config.get_storm_signups(TEST_GUILD_ID, "DS", "2026-05-18")
        assert len(rows) == 1
        assert rows[0]["vote"] == "cannot"

    def test_on_behalf_flag_persists(self, temp_db):
        import config
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=999,  # officer ID
            target_member_id="Alice",  # roster row, not on Discord
            vote="b",
            is_on_behalf=True,
        )
        row = config.get_member_vote(TEST_GUILD_ID, "DS", "2026-05-18", "Alice")
        assert row["is_on_behalf"]    is True
        assert row["voter_user_id"]   == 999
        assert row["target_member_id"] == "Alice"

    def test_invalid_vote_rejected(self, temp_db):
        import config
        ok = config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=111, target_member_id="111", vote="bogus",
        )
        assert ok is False
        assert config.get_storm_signups(TEST_GUILD_ID, "DS", "2026-05-18") == []

    def test_get_signups_isolates_events(self, temp_db):
        import config
        # DS event
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=1, target_member_id="1", vote="a",
        )
        # CS event same date — should not bleed
        config.record_storm_vote(
            TEST_GUILD_ID, "CS", "2026-05-18",
            voter_user_id=1, target_member_id="1", vote="b",
        )
        ds = config.get_storm_signups(TEST_GUILD_ID, "DS", "2026-05-18")
        cs = config.get_storm_signups(TEST_GUILD_ID, "CS", "2026-05-18")
        assert len(ds) == 1 and ds[0]["vote"] == "a"
        assert len(cs) == 1 and cs[0]["vote"] == "b"

    def test_record_registration_post_allows_multiple_per_event(self, temp_db):
        """#265: multiple posts per event are supported so leadership can
        re-post when the original gets lost in channel chatter. Both
        inserts succeed; `has_registration_post` still reports True after
        either (used by the auto-scheduler's idempotency guard); the
        recent-posts list surfaces both rows so persistent-View
        re-registration can attach to every live message_id on startup."""
        import config
        first = config.record_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-18",
            channel_id=200, message_id=4001,
            time_a_label="9pm ET", time_b_label="4pm ET",
        )
        second = config.record_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-18",
            channel_id=200, message_id=9999,
        )
        assert first is True
        assert second is True
        assert config.has_registration_post(TEST_GUILD_ID, "DS", "2026-05-18") is True
        # Both message_ids land in the recent-posts list so persistent-
        # View re-registration attaches handlers to every live message.
        recents = config.get_recent_storm_registration_posts(within_days=365)
        msg_ids = {r["message_id"] for r in recents}
        assert msg_ids == {4001, 9999}

    def test_recent_posts_window(self, temp_db):
        import config
        import datetime as _dt
        today  = _dt.date.today().isoformat()
        recent = (_dt.date.today() - _dt.timedelta(days=5)).isoformat()
        old    = (_dt.date.today() - _dt.timedelta(days=30)).isoformat()
        config.record_storm_registration_post(TEST_GUILD_ID, "DS", today,  100, 1)
        config.record_storm_registration_post(TEST_GUILD_ID, "DS", recent, 100, 2)
        config.record_storm_registration_post(TEST_GUILD_ID, "DS", old,    100, 3)
        recents = config.get_recent_storm_registration_posts(within_days=14)
        dates = {p["event_date"] for p in recents}
        assert today  in dates
        assert recent in dates
        assert old    not in dates


class TestStormRosterImages:
    """Pointer to a saved roster-image message — written by the
    `💾 Save to history` action, read by the history browser."""

    def test_save_and_list_single_image(self, temp_db):
        import config
        config.save_roster_image_ref(
            TEST_GUILD_ID, "DS", "2026-05-18", "A",
            channel_id=900, message_id=12345, user_id=1,
        )
        refs = config.list_roster_image_refs(TEST_GUILD_ID, "DS", "2026-05-18")
        assert len(refs) == 1
        assert refs[0]["team"] == "A"
        assert refs[0]["channel_id"] == 900
        assert refs[0]["message_id"] == 12345
        assert refs[0]["posted_by_user_id"] == 1

    def test_save_two_teams_one_event(self, temp_db):
        """DS rosters can have one image per team (A + B). Both
        survive in the same query, ordered by team for stable
        history rendering."""
        import config
        config.save_roster_image_ref(
            TEST_GUILD_ID, "DS", "2026-05-18", "B",
            channel_id=900, message_id=2002, user_id=1,
        )
        config.save_roster_image_ref(
            TEST_GUILD_ID, "DS", "2026-05-18", "A",
            channel_id=900, message_id=1001, user_id=1,
        )
        refs = config.list_roster_image_refs(TEST_GUILD_ID, "DS", "2026-05-18")
        assert [r["team"] for r in refs] == ["A", "B"]
        assert refs[0]["message_id"] == 1001
        assert refs[1]["message_id"] == 2002

    def test_cs_uses_empty_team(self, temp_db):
        """CS has one roster per event — saved with empty team."""
        import config
        config.save_roster_image_ref(
            TEST_GUILD_ID, "CS", "2026-05-18", "",
            channel_id=900, message_id=5555, user_id=1,
        )
        refs = config.list_roster_image_refs(TEST_GUILD_ID, "CS", "2026-05-18")
        assert len(refs) == 1
        assert refs[0]["team"] == ""

    def test_resave_upserts(self, temp_db):
        """Officer renders + saves twice — second save overwrites the
        first pointer (the public message has a new ID)."""
        import config
        config.save_roster_image_ref(
            TEST_GUILD_ID, "DS", "2026-05-18", "A",
            channel_id=900, message_id=1001, user_id=1,
        )
        config.save_roster_image_ref(
            TEST_GUILD_ID, "DS", "2026-05-18", "A",
            channel_id=900, message_id=2002, user_id=2,
        )
        refs = config.list_roster_image_refs(TEST_GUILD_ID, "DS", "2026-05-18")
        assert len(refs) == 1
        assert refs[0]["message_id"] == 2002
        assert refs[0]["posted_by_user_id"] == 2

    def test_delete_clears_stale_pointer(self, temp_db):
        """When the history browser detects a deleted message at
        click time, the stale pointer gets pruned so it stops
        showing up on future opens."""
        import config
        config.save_roster_image_ref(
            TEST_GUILD_ID, "DS", "2026-05-18", "A",
            channel_id=900, message_id=1001, user_id=1,
        )
        deleted = config.delete_roster_image_ref(
            TEST_GUILD_ID, "DS", "2026-05-18", "A",
        )
        assert deleted is True
        assert config.list_roster_image_refs(TEST_GUILD_ID, "DS", "2026-05-18") == []
        # Second delete is a no-op (idempotent), not an error.
        again = config.delete_roster_image_ref(
            TEST_GUILD_ID, "DS", "2026-05-18", "A",
        )
        assert again is False

    def test_event_isolation(self, temp_db):
        """Two events on different dates don't bleed into each other."""
        import config
        config.save_roster_image_ref(
            TEST_GUILD_ID, "DS", "2026-05-18", "A",
            channel_id=900, message_id=1001, user_id=1,
        )
        config.save_roster_image_ref(
            TEST_GUILD_ID, "DS", "2026-05-25", "A",
            channel_id=900, message_id=2002, user_id=1,
        )
        may18 = config.list_roster_image_refs(TEST_GUILD_ID, "DS", "2026-05-18")
        may25 = config.list_roster_image_refs(TEST_GUILD_ID, "DS", "2026-05-25")
        assert len(may18) == 1 and may18[0]["message_id"] == 1001
        assert len(may25) == 1 and may25[0]["message_id"] == 2002


class TestSurveyConfig:
    """Test guild_survey_config save/load."""

    def test_default_survey_config(self, temp_db):
        import config
        cfg = config.get_survey_config(TEST_GUILD_ID)
        assert cfg["tab_squad_powers"] == "Squad Powers"
        assert cfg["tab_history"]      == "Survey History"
        assert isinstance(cfg["questions"], list)

    def test_save_and_reload_survey_config(self, temp_db):
        import config
        questions = [
            {"key": "squad1", "label": "1st Squad Power", "type": "text",
             "options": [], "placeholder": "e.g. 43.27", "max_chars": 5},
            {"key": "profession", "label": "Profession", "type": "dropdown",
             "options": ["War Leader", "Engineer"], "placeholder": "", "max_chars": 0},
        ]
        config.save_survey_config(
            TEST_GUILD_ID, "Squad Powers", "Survey History",
            questions, "Please fill out the survey each week!"
        )
        cfg = config.get_survey_config(TEST_GUILD_ID)
        assert len(cfg["questions"])        == 2
        assert cfg["questions"][0]["key"]   == "squad1"
        assert cfg["questions"][1]["type"]  == "dropdown"
        assert cfg["intro_message"]         == "Please fill out the survey each week!"


class TestNumericMagnitudeBackfill:
    """init_db upgrades guild survey configs that still carry the original
    LW default keys with `type: text` to `type: numeric` + the right
    magnitude. Idempotent: re-running init_db on already-migrated data
    leaves it alone, and custom (non-default-key) text questions are never
    touched."""

    def test_backfills_default_lw_keys(self, temp_db):
        """A pre-rework guild config with `thp` / `squad*_power` as text-type
        questions gets upgraded to numeric+magnitude on init_db."""
        import config, json
        legacy = [
            {"key": "thp",          "label": "THP",          "type": "text",
             "options": [], "placeholder": "e.g. 301", "max_chars": 3},
            {"key": "squad1_power", "label": "1st Squad",    "type": "text",
             "options": [], "placeholder": "e.g. 43.27", "max_chars": 5},
            {"key": "drone_level",  "label": "Drone Level",  "type": "text",
             "options": [], "placeholder": "e.g. 243", "max_chars": 5},
        ]
        config.save_survey_config(
            TEST_GUILD_ID, "Squad Powers", "Survey History", legacy, ""
        )
        # Re-running init_db triggers the backfill block.
        config.init_db()
        upgraded = config.get_survey_config(TEST_GUILD_ID)["questions"]
        by_key = {q["key"]: q for q in upgraded}
        assert by_key["thp"]["type"]               == "numeric"
        assert by_key["thp"]["magnitude"]          == "M"
        assert by_key["squad1_power"]["type"]      == "numeric"
        assert by_key["squad1_power"]["magnitude"] == "M"
        assert by_key["drone_level"]["type"]       == "numeric"
        assert by_key["drone_level"]["magnitude"]  == "raw"

    def test_backfill_idempotent(self, temp_db):
        """Running init_db twice on already-migrated data must not corrupt
        anything — no double-applied magnitudes, no type churn."""
        import config
        legacy = [
            {"key": "thp", "label": "THP", "type": "text",
             "options": [], "placeholder": "", "max_chars": 3},
        ]
        config.save_survey_config(
            TEST_GUILD_ID, "Squad Powers", "Survey History", legacy, ""
        )
        config.init_db()
        config.init_db()  # second run should be a no-op
        upgraded = config.get_survey_config(TEST_GUILD_ID)["questions"]
        assert upgraded[0]["type"]      == "numeric"
        assert upgraded[0]["magnitude"] == "M"

    def test_backfill_skips_custom_keys(self, temp_db):
        """A guild that defined their own `power` or `level` text-type questions
        keeps them as text — only the documented LW default keys are touched."""
        import config
        custom = [
            {"key": "my_custom_power", "label": "Power", "type": "text",
             "options": [], "placeholder": "", "max_chars": 5},
        ]
        config.save_survey_config(
            TEST_GUILD_ID, "Squad Powers", "Survey History", custom, ""
        )
        config.init_db()
        kept = config.get_survey_config(TEST_GUILD_ID)["questions"]
        assert kept[0]["type"] == "text"
        assert "magnitude" not in kept[0]

    def test_backfill_does_not_overwrite_explicit_magnitude(self, temp_db):
        """If a question already declares a magnitude (someone re-ran the
        wizard, or an earlier migration touched it), don't second-guess it."""
        import config
        already = [
            {"key": "thp", "label": "THP", "type": "numeric",
             "options": [], "placeholder": "", "max_chars": 3,
             "magnitude": "K"},  # weird choice, but it's theirs
        ]
        config.save_survey_config(
            TEST_GUILD_ID, "Squad Powers", "Survey History", already, ""
        )
        config.init_db()
        kept = config.get_survey_config(TEST_GUILD_ID)["questions"]
        assert kept[0]["magnitude"] == "K"


class TestGrowthConfig:
    """Test guild_growth_config save/load."""

    def test_default_growth_config(self, temp_db):
        import config
        cfg = config.get_growth_config(TEST_GUILD_ID)
        assert cfg["enabled"]            == 0
        assert cfg["tab_growth"]         == "Growth Tracking"
        assert cfg["snapshot_frequency"] == "monthly"

    def test_save_and_reload_growth_config(self, temp_db):
        import config
        metrics = [
            {"col": "E", "label": "1st Squad Power"},
            {"col": "I", "label": "THP"},
        ]
        config.save_growth_config(
            TEST_GUILD_ID, enabled=1,
            tab_source="Squad Powers", name_col="D",
            metrics=metrics, tab_growth="Growth Tracking",
            snapshot_frequency="monthly", snapshot_day=1,
            snapshot_interval=30, data_start_row=2,
        )
        cfg = config.get_growth_config(TEST_GUILD_ID)
        assert cfg["enabled"]     == 1
        assert cfg["tab_source"]  == "Squad Powers"
        assert cfg["name_col"]    == "D"
        assert len(cfg["metrics"])== 2
        assert cfg["metrics"][0]["label"] == "1st Squad Power"
        assert cfg["metrics"][1]["col"]   == "I"


class TestGuildEvents:
    """Test guild_events save/load/delete."""

    def test_save_and_load_event(self, temp_db):
        import config
        event = {
            "short_key": "marauder",
            "name": "Plague Marauder (AE)",
            "timezone": "America/New_York",
            "default_time": "22:15",
            "announcement_blurb": "Marauder at {time}!",
            "schedule_type": "repeating",
            "anchor_date": "2026-03-30",
            "interval_days": 3,
            "draft_channel_id": 111,
            "announcement_channel_id": 222,
            "draft_time": "12:00",
            "five_min_warning": 1,
            "active": 1,
        }
        config.save_guild_event(TEST_GUILD_ID, event)
        loaded = config.get_guild_event(TEST_GUILD_ID, "marauder")
        assert loaded["name"]           == "Plague Marauder (AE)"
        assert loaded["interval_days"]  == 3
        assert loaded["schedule_type"]  == "repeating"

    def test_get_guild_events_returns_list(self, temp_db):
        import config
        for key, name in [("siege", "Zombie Siege"), ("marauder", "Plague Marauder")]:
            config.save_guild_event(TEST_GUILD_ID, {
                "short_key": key, "name": name, "timezone": "America/New_York",
                "default_time": "22:00", "announcement_blurb": "Event!",
                "schedule_type": "manual", "anchor_date": "", "interval_days": 0,
                "draft_channel_id": 0, "announcement_channel_id": 0,
                "draft_time": "12:00", "five_min_warning": 0, "active": 1,
            })
        events = config.get_guild_events(TEST_GUILD_ID)
        assert len(events) == 2

    def test_delete_event(self, temp_db):
        import config
        config.save_guild_event(TEST_GUILD_ID, {
            "short_key": "siege", "name": "Zombie Siege", "timezone": "America/New_York",
            "default_time": "22:00", "announcement_blurb": "!", "schedule_type": "manual",
            "anchor_date": "", "interval_days": 0, "draft_channel_id": 0,
            "announcement_channel_id": 0, "draft_time": "12:00",
            "five_min_warning": 0, "active": 1,
        })
        config.delete_guild_event(TEST_GUILD_ID, "siege")
        # Events are soft-deleted (active=0), not removed
        # get_guild_events with active_only=True should exclude it
        active_events = config.get_guild_events(TEST_GUILD_ID, active_only=True)
        assert not any(e["short_key"] == "siege" for e in active_events)

    def test_events_isolated_per_guild(self, temp_db):
        import config
        event = {
            "short_key": "marauder", "name": "Marauder", "timezone": "America/New_York",
            "default_time": "22:00", "announcement_blurb": "!", "schedule_type": "manual",
            "anchor_date": "", "interval_days": 0, "draft_channel_id": 0,
            "announcement_channel_id": 0, "draft_time": "12:00",
            "five_min_warning": 0, "active": 1,
        }
        guild_a = 1000000000000000001
        config.save_guild_event(guild_a, event)
        assert config.get_guild_events(TEST_GUILD_ID) == []
        assert len(config.get_guild_events(guild_a))  == 1


class TestDescribeSheetError:
    """describe_sheet_error should turn opaque gspread exceptions into
    one-line diagnostics that name the actual failure."""

    def test_worksheet_not_found_includes_tab_name(self):
        import gspread
        import config
        # gspread sets str(e) = the missing tab name
        e = gspread.exceptions.WorksheetNotFound("Train Schedule")
        msg = config.describe_sheet_error(e, guild_id=12345, tab="fallback")
        assert "Train Schedule" in msg
        assert "guild=12345" in msg
        assert "tab" in msg.lower()

    def test_worksheet_not_found_falls_back_to_caller_tab(self):
        import gspread
        import config
        # Empty exception message — fall back to caller-supplied tab
        e = gspread.exceptions.WorksheetNotFound("")
        msg = config.describe_sheet_error(e, guild_id=99, tab="DS Assignments")
        assert "DS Assignments" in msg

    def test_api_error_404_says_deleted(self):
        import gspread
        import config
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 404
        resp.json.return_value = {}
        e = gspread.exceptions.APIError(resp)
        msg = config.describe_sheet_error(e, guild_id=42)
        assert "404" in msg
        assert "deleted" in msg.lower() or "inaccessible" in msg.lower()
        assert "guild=42" in msg

    def test_api_error_403_says_permission(self):
        import gspread
        import config
        from unittest.mock import MagicMock
        resp = MagicMock()
        resp.status_code = 403
        resp.json.return_value = {}
        e = gspread.exceptions.APIError(resp)
        msg = config.describe_sheet_error(e, guild_id=7)
        assert "403" in msg
        assert "permission" in msg.lower() or "share" in msg.lower()

    def test_non_gspread_error_falls_through(self):
        import config
        msg = config.describe_sheet_error(ValueError("bad input"), guild_id=1)
        assert "ValueError" in msg
        assert "bad input" in msg
        assert "guild=1" in msg


class TestNormalizeSpreadsheetId:
    """normalize_spreadsheet_id should extract the ID when the user pastes a
    full sheet URL into the /setup Step 5 prompt. Saving the raw URL leads to
    a 404 dead-end at gc.open_by_key time with no usable diagnostic."""

    def test_extracts_id_from_full_url(self):
        import config
        url = "https://docs.google.com/spreadsheets/d/1yQ6tgzrj4c23xK7X5l1GyWM9uMpgCgCC3iz8ZvyMg18/edit?gid=2117513184"
        assert config.normalize_spreadsheet_id(url) == "1yQ6tgzrj4c23xK7X5l1GyWM9uMpgCgCC3iz8ZvyMg18"

    def test_extracts_id_from_url_without_query_string(self):
        import config
        url = "https://docs.google.com/spreadsheets/d/1yQ6tgzrj4c23xK7X5l1GyWM9uMpgCgCC3iz8ZvyMg18/edit"
        assert config.normalize_spreadsheet_id(url) == "1yQ6tgzrj4c23xK7X5l1GyWM9uMpgCgCC3iz8ZvyMg18"

    def test_extracts_id_from_url_without_scheme(self):
        import config
        url = "docs.google.com/spreadsheets/d/1yQ6tgzrj4c23xK7X5l1GyWM9uMpgCgCC3iz8ZvyMg18/edit"
        assert config.normalize_spreadsheet_id(url) == "1yQ6tgzrj4c23xK7X5l1GyWM9uMpgCgCC3iz8ZvyMg18"

    def test_bare_id_passes_through_unchanged(self):
        import config
        sid = "1yQ6tgzrj4c23xK7X5l1GyWM9uMpgCgCC3iz8ZvyMg18"
        assert config.normalize_spreadsheet_id(sid) == sid

    def test_strips_surrounding_whitespace(self):
        import config
        assert config.normalize_spreadsheet_id("  abc123  ") == "abc123"

    def test_empty_string_returns_empty(self):
        import config
        assert config.normalize_spreadsheet_id("") == ""

    def test_none_returns_empty(self):
        import config
        assert config.normalize_spreadsheet_id(None) == ""


# ── Storm roster drafts (#240) ────────────────────────────────────────────────


class TestRosterDraftCrud:
    """#240: persist the structured roster builder's in-progress state
    so View timeouts and Railway redeploys don't lose work. One row per
    (guild, event_type, team); reusable across event weeks."""

    def test_get_returns_none_when_no_row(self, seeded_db):
        import config
        assert config.get_roster_draft(TEST_GUILD_ID, "DS", "A") is None

    def test_save_then_get_round_trips(self, seeded_db):
        import config
        config.save_roster_draft(
            TEST_GUILD_ID, "DS", "A",
            session_json='{"version": 1, "test": "payload"}',
            event_date="2026-05-22",
        )
        loaded = config.get_roster_draft(TEST_GUILD_ID, "DS", "A")
        assert loaded is not None
        assert loaded["session_json"] == '{"version": 1, "test": "payload"}'
        assert loaded["event_date"] == "2026-05-22"
        assert loaded["updated_at"]  # ISO timestamp string, non-empty

    def test_save_upserts_one_row_per_team(self, seeded_db):
        """Saving for the same (guild, event_type, team) overwrites the
        previous row — never appends. One draft per team."""
        import config
        config.save_roster_draft(
            TEST_GUILD_ID, "DS", "A",
            session_json='{"v": 1}', event_date="2026-05-22",
        )
        config.save_roster_draft(
            TEST_GUILD_ID, "DS", "A",
            session_json='{"v": 2}', event_date="2026-05-29",
        )
        loaded = config.get_roster_draft(TEST_GUILD_ID, "DS", "A")
        # Second save wins; event_date advanced too.
        assert loaded["session_json"] == '{"v": 2}'
        assert loaded["event_date"] == "2026-05-29"

    def test_drafts_isolated_per_team(self, seeded_db):
        import config
        config.save_roster_draft(
            TEST_GUILD_ID, "DS", "A",
            session_json='{"team": "A"}', event_date="2026-05-22",
        )
        config.save_roster_draft(
            TEST_GUILD_ID, "DS", "B",
            session_json='{"team": "B"}', event_date="2026-05-22",
        )
        loaded_a = config.get_roster_draft(TEST_GUILD_ID, "DS", "A")
        loaded_b = config.get_roster_draft(TEST_GUILD_ID, "DS", "B")
        assert loaded_a["session_json"] == '{"team": "A"}'
        assert loaded_b["session_json"] == '{"team": "B"}'

    def test_drafts_isolated_per_event_type(self, seeded_db):
        import config
        config.save_roster_draft(
            TEST_GUILD_ID, "DS", "A",
            session_json='{"ev": "DS"}', event_date="2026-05-22",
        )
        config.save_roster_draft(
            TEST_GUILD_ID, "CS", "A",
            session_json='{"ev": "CS"}', event_date="2026-05-22",
        )
        loaded_ds = config.get_roster_draft(TEST_GUILD_ID, "DS", "A")
        loaded_cs = config.get_roster_draft(TEST_GUILD_ID, "CS", "A")
        assert loaded_ds["session_json"] == '{"ev": "DS"}'
        assert loaded_cs["session_json"] == '{"ev": "CS"}'

    def test_delete_removes_row(self, seeded_db):
        import config
        config.save_roster_draft(
            TEST_GUILD_ID, "DS", "A",
            session_json='{"x": 1}', event_date="2026-05-22",
        )
        assert config.delete_roster_draft(TEST_GUILD_ID, "DS", "A") == 1
        assert config.get_roster_draft(TEST_GUILD_ID, "DS", "A") is None

    def test_delete_returns_zero_when_no_row(self, seeded_db):
        import config
        assert config.delete_roster_draft(TEST_GUILD_ID, "DS", "A") == 0


class TestStormTeamSlotMapping:
    """Per-team time-slot mapping (#251). Each event type (DS / CS)
    stores Team A's slot index and Team B's slot index independently;
    both teams can share a slot. Slots are 1 or 2 (game-defined times
    in DS_SERVER_TIMES / CS_SERVER_TIMES). NULL = officer hasn't picked
    yet — sign-up posts gate on this."""

    def test_unset_guild_returns_none_indices(self, seeded_db):
        import config
        a, b = config.resolve_storm_team_slots(TEST_GUILD_ID, "DS")
        assert a is None
        assert b is None

    def test_save_round_trip_persists_indices(self, seeded_db):
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, 2)
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["team_a_slot_index"] == 1
        assert cfg["team_b_slot_index"] == 2

    def test_ds_and_cs_are_independent(self, seeded_db):
        """Saving DS doesn't affect CS — each event type has its own
        (guild_id, event_type) row."""
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, 2)
        config.save_storm_team_slots(TEST_GUILD_ID, "CS", 2, 1)
        ds = config.get_storm_config(TEST_GUILD_ID, "DS")
        cs = config.get_storm_config(TEST_GUILD_ID, "CS")
        assert (ds["team_a_slot_index"], ds["team_b_slot_index"]) == (1, 2)
        assert (cs["team_a_slot_index"], cs["team_b_slot_index"]) == (2, 1)

    def test_both_teams_can_share_a_slot(self, seeded_db):
        """If an alliance runs both teams at the same time, both
        indices can be the same slot — that's a legal saved state."""
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, 1)
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["team_a_slot_index"] == 1
        assert cfg["team_b_slot_index"] == 1

    def test_invalid_index_normalises_to_none(self, seeded_db):
        """Non-1/2 inputs (typos, out-of-range, strings) clamp to None
        rather than corrupting the row."""
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 0, 99)
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["team_a_slot_index"] is None
        assert cfg["team_b_slot_index"] is None

    def test_partial_team_save_supports_single_team_alliances(self, seeded_db):
        """A teams=A alliance only needs Team A's slot; passing None
        for Team B's index leaves that column NULL."""
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, None)
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["team_a_slot_index"] == 1
        assert cfg["team_b_slot_index"] is None

    def test_save_does_not_clobber_other_storm_config_fields(self, seeded_db):
        """save_storm_team_slots is UPDATE-only against the two slot
        columns — re-running it must not blank tab_name, templates,
        or any other field on the storm config row."""
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS", tab_name="DS Custom Tab",
            mail_template="Custom mail body",
            timezone="America/New_York", log_channel_id=999,
            post_channel_id=777,
            dm_reminder_message="custom dm",
            teams="A",
        )
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 2, 1)
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["tab_name"] == "DS Custom Tab"
        assert cfg["mail_template"] == "Custom mail body"
        assert cfg["log_channel_id"] == 999
        assert cfg["post_channel_id"] == 777
        assert cfg["dm_reminder_message"] == "custom dm"
        assert cfg["teams"] == "A"
        assert cfg["team_a_slot_index"] == 2
        assert cfg["team_b_slot_index"] == 1


class TestResolveStormTeamSlots:
    """resolve_storm_team_slots (#251) prefers a per-event override
    (captured on storm_registration_posts when the sign-up post went
    out) and falls back to the guild default. Lets attendance + the
    SignupView vote-ack render the times that were actually in effect
    on that specific event, even if leadership later changed the saved
    default."""

    def test_falls_back_to_guild_default_when_no_post(self, seeded_db):
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, 2)
        a, b = config.resolve_storm_team_slots(
            TEST_GUILD_ID, "DS", "2026-05-29",
        )
        assert (a, b) == (1, 2)

    def test_per_event_override_wins_over_guild_default(self, seeded_db):
        """When the officer picked an override for a single week, the
        storm_registration_posts row stores those indices and they
        win over the saved guild default for that event_date."""
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, 2)
        config.record_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-29",
            channel_id=111, message_id=222,
            time_a_label="A label", time_b_label="B label",
            team_a_slot_index=2, team_b_slot_index=2,
        )
        a, b = config.resolve_storm_team_slots(
            TEST_GUILD_ID, "DS", "2026-05-29",
        )
        assert (a, b) == (2, 2)

    def test_other_event_date_keeps_guild_default(self, seeded_db):
        """The per-event override only applies to its own event_date —
        a different event picks up the saved default."""
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, 2)
        config.record_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-29",
            channel_id=111, message_id=222,
            time_a_label="A", time_b_label="B",
            team_a_slot_index=2, team_b_slot_index=2,
        )
        a, b = config.resolve_storm_team_slots(
            TEST_GUILD_ID, "DS", "2026-06-05",
        )
        assert (a, b) == (1, 2)

    def test_no_event_date_returns_guild_default(self, seeded_db):
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "CS", 2, 1)
        a, b = config.resolve_storm_team_slots(TEST_GUILD_ID, "CS")
        assert (a, b) == (2, 1)


class TestGetStormTeamSlotLabels:
    """get_storm_team_slot_labels (#251) returns labels in TEAM order
    — used by the SignupView vote-ack, the on-behalf select, the
    storm overview embed, and the /setup hub config view to keep
    every surface aligned with the team→slot mapping members vote on."""

    def test_returns_team_ordered_labels(self, seeded_db):
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 1, 2)
        slot_labels = config.get_storm_slot_labels("DS", TEST_GUILD_ID)
        a, b = config.get_storm_team_slot_labels(TEST_GUILD_ID, "DS")
        assert a == slot_labels[0]
        assert b == slot_labels[1]

    def test_returns_empty_strings_when_unset(self, seeded_db):
        """No mapping saved → empty strings, which is what the sign-up
        post creation flow tests for to gate posting."""
        import config
        a, b = config.get_storm_team_slot_labels(TEST_GUILD_ID, "DS")
        assert (a, b) == ("", "")

    def test_partial_mapping_only_returns_set_team_label(self, seeded_db):
        """teams=A alliance with only Team A's slot set — Team B's
        label is empty, but Team A's renders correctly."""
        import config
        config.save_storm_team_slots(TEST_GUILD_ID, "DS", 2, None)
        slot_labels = config.get_storm_slot_labels("DS", TEST_GUILD_ID)
        a, b = config.get_storm_team_slot_labels(TEST_GUILD_ID, "DS")
        assert a == slot_labels[1]
        assert b == ""


class TestStormRegistrationPostIndices:
    """Per-event team→slot indices on storm_registration_posts (#251)
    capture the mapping in effect when each individual sign-up post
    went out. Lets attendance reconstruct the times that were live on
    a specific event without re-deriving them from a possibly-changed
    guild default."""

    def test_record_persists_indices(self, seeded_db):
        import config
        config.record_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-29",
            channel_id=111, message_id=222,
            time_a_label="A label", time_b_label="B label",
            team_a_slot_index=1, team_b_slot_index=2,
        )
        post = config.get_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-29",
        )
        assert post is not None
        assert post["team_a_slot_index"] == 1
        assert post["team_b_slot_index"] == 2
        assert post["time_a_label"] == "A label"
        assert post["time_b_label"] == "B label"

    def test_indices_default_to_zero_on_legacy_callsites(self, seeded_db):
        """Existing callers that don't pass indices keep working — the
        kwargs default to 0 (the schema's "legacy / unknown" sentinel)."""
        import config
        config.record_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-29",
            channel_id=111, message_id=222,
            time_a_label="A", time_b_label="B",
        )
        post = config.get_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-29",
        )
        assert post["team_a_slot_index"] == 0
        assert post["team_b_slot_index"] == 0

    def test_get_returns_latest_when_multiple_posts(self, seeded_db):
        """#265: with multiple posts per event, `get_storm_registration_post`
        returns the LATEST by posted_at. Slot mapping is expected to be
        identical across reposts (same guild config / override), but
        the freshest channel_id / message_id is the safest pick for any
        future caller."""
        import config
        import time
        config.record_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-29",
            channel_id=111, message_id=4001,
            team_a_slot_index=1, team_b_slot_index=2,
        )
        # Tiny sleep so the ISO-second-precision `posted_at` differs.
        time.sleep(1.1)
        config.record_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-29",
            channel_id=222, message_id=9999,
            team_a_slot_index=1, team_b_slot_index=2,
        )
        post = config.get_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-29",
        )
        assert post is not None
        assert post["message_id"] == 9999
        assert post["channel_id"] == 222
