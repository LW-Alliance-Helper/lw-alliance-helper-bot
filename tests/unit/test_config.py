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
