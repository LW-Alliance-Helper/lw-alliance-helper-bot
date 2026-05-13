"""
Unit tests for time parsing and Server Time conversion utilities.
Covers _parse_12h_time, server_time_to_local, format_storm_slot,
get_storm_slot_labels, get_storm_slot_for_key.
"""
import pytest
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


class TestParse12hTime:
    """Test _parse_12h_time from setup_cog."""

    def setup_method(self):
        from setup_cog import _parse_12h_time
        self.parse = _parse_12h_time

    def test_pm_time(self):
        assert self.parse("10:15pm") == "22:15"

    def test_am_time(self):
        assert self.parse("9:00am")  == "09:00"

    def test_noon(self):
        assert self.parse("12:00pm") == "12:00"

    def test_midnight(self):
        assert self.parse("12:00am") == "00:00"

    def test_no_minutes(self):
        assert self.parse("9pm")     == "21:00"

    def test_4pm(self):
        assert self.parse("4:00pm")  == "16:00"

    def test_9pm(self):
        assert self.parse("9:00pm")  == "21:00"

    def test_24h_passthrough(self):
        # _parse_12h_time only handles 12h format — 24h input returns None
        assert self.parse("22:15") is None

    def test_invalid_returns_empty(self):
        assert self.parse("not a time") is None
        assert self.parse("") is None

    def test_case_insensitive(self):
        assert self.parse("10:15PM") == "22:15"
        assert self.parse("9:00AM")  == "09:00"

    def test_with_space(self):
        assert self.parse("10:15 pm") == "22:15"


class TestFormat24hTo12h:
    """`_format_24h_to_12h` is the inverse of `_parse_12h_time` — used
    in setup wizards so 'Keep current' and 'Use default' buttons both
    show 12-hour times (the DB stores 24-hour). The reverse mismatch
    was reported in dev for /setup_shiny_tasks Step 4."""

    def setup_method(self):
        from setup_cog import _format_24h_to_12h
        self.fmt = _format_24h_to_12h

    def test_am_time(self):
        assert self.fmt("09:00") == "9:00am"

    def test_pm_time(self):
        assert self.fmt("22:15") == "10:15pm"

    def test_midnight_renders_as_12am(self):
        assert self.fmt("00:00") == "12:00am"

    def test_noon_renders_as_12pm(self):
        assert self.fmt("12:00") == "12:00pm"

    def test_round_trip_with_parse(self):
        from setup_cog import _parse_12h_time
        for raw in ("9:00am", "10:15pm", "12:00am", "12:00pm", "1:05am"):
            assert self.fmt(_parse_12h_time(raw)) == raw

    def test_empty_string_passthrough(self):
        """Wizard call-sites pass `current` straight in; an empty
        saved value must stay empty so `ask_keep_or_change` falls
        back to the 2-button 'Use default' layout."""
        assert self.fmt("") == ""

    def test_unparseable_passthrough(self):
        """Don't mangle garbage — let the caller surface it instead
        of swallowing the value silently."""
        assert self.fmt("garbage") == "garbage"


class TestFormatTimeWithTz:
    """`_format_time_with_tz` renders a stored 24h 'HH:MM' time with
    the guild's timezone abbreviation appended, e.g.
    '08:00' + 'America/New_York' → '8:00am EDT' (or EST in winter).
    Used everywhere a wizard summary or `/view_configuration` shows a
    saved reminder/draft/post time back to leadership — bare '08:00'
    leaves them guessing which timezone the reminder fires in."""

    def setup_method(self):
        from setup_cog import _format_time_with_tz
        self.fmt = _format_time_with_tz

    def test_et_time_carries_et_abbreviation(self):
        """ET tz always renders the abbreviation token (EDT or EST
        depending on DST for today's date)."""
        result = self.fmt("08:00", "America/New_York")
        assert result.startswith("8:00am ")
        # Either EDT (summer) or EST (winter) is acceptable.
        assert result.endswith("EDT") or result.endswith("EST")

    def test_pacific_time_carries_pt_abbreviation(self):
        result = self.fmt("14:30", "America/Los_Angeles")
        assert result.startswith("2:30pm ")
        assert result.endswith("PDT") or result.endswith("PST")

    def test_seoul_carries_kst(self):
        """Non-US tz works the same — pulled from dt.tzname()."""
        result = self.fmt("09:00", "Asia/Seoul")
        assert result == "9:00am KST"

    def test_london_carries_bst_or_gmt(self):
        result = self.fmt("12:00", "Europe/London")
        assert result.startswith("12:00pm ")
        assert result.endswith("BST") or result.endswith("GMT")

    def test_no_tz_falls_back_to_bare_12h(self):
        """Caller passing None tz still gets a 12h formatted time."""
        assert self.fmt("08:00", None) == "8:00am"

    def test_unknown_tz_falls_back_to_bare_12h(self):
        """Bad ZoneInfo lookup should never crash the embed render."""
        assert self.fmt("08:00", "Not/A/Real_Zone") == "8:00am"

    def test_empty_string_passthrough(self):
        """`*not set*` sentinels and empty strings pipe through
        unchanged so callers don't need a separate guard."""
        assert self.fmt("", "America/New_York") == ""

    def test_none_passthrough(self):
        assert self.fmt(None, "America/New_York") == ""

    def test_not_set_sentinel_passthrough(self):
        """Wizard surfaces show `*not set*` for unconfigured times —
        running it through the formatter must leave it alone."""
        assert self.fmt("*not set*", "America/New_York") == "*not set*"

    def test_unparseable_passthrough(self):
        """Don't mangle garbage — surface it so the bug is visible."""
        assert self.fmt("garbage", "America/New_York") == "garbage"

    def test_out_of_range_passthrough(self):
        assert self.fmt("25:99", "America/New_York") == "25:99"

    def test_midnight_renders_12am(self):
        result = self.fmt("00:00", "America/New_York")
        assert result.startswith("12:00am ")

    def test_noon_renders_12pm(self):
        result = self.fmt("12:00", "America/New_York")
        assert result.startswith("12:00pm ")


class TestServerTimeToLocal:
    """server_time_to_local converts a (hour, minute, guild_id) triple from
    Server Time (UTC-2) to the guild's local clock string. e.g. (18, 0)
    with ET timezone in summer → "4pm EDT"."""

    def test_ds_slot1_in_et(self, seeded_db):
        """18:00 server time → 4pm in ET (summer baseline)."""
        import config
        result = config.server_time_to_local(18, 0, TEST_GUILD_ID)
        assert "4" in result
        assert "pm" in result.lower()

    def test_ds_slot2_in_et(self, seeded_db):
        """23:00 server time → 9pm in ET (summer baseline)."""
        import config
        result = config.server_time_to_local(23, 0, TEST_GUILD_ID)
        assert "9" in result
        assert "pm" in result.lower()

    def test_cs_slot1_in_et(self, seeded_db):
        """12:00 server time → 10am in ET (summer baseline)."""
        import config
        result = config.server_time_to_local(12, 0, TEST_GUILD_ID)
        assert "10" in result
        assert "am" in result.lower()

    def test_includes_tz_abbreviation(self, seeded_db):
        import config
        result = config.server_time_to_local(18, 0, TEST_GUILD_ID)
        assert any(tz in result for tz in ("EDT", "EST", "ET"))

    def test_different_timezone_changes_output(self, seeded_db):
        import config
        cfg = config.get_config(TEST_GUILD_ID)
        et_result = config.server_time_to_local(18, 0, TEST_GUILD_ID)

        cfg.timezone = "Europe/London"
        config.save_config(cfg)
        try:
            london_result = config.server_time_to_local(18, 0, TEST_GUILD_ID)
            assert et_result != london_result
        finally:
            cfg.timezone = "America/New_York"
            config.save_config(cfg)

    def test_falls_back_when_guild_id_unknown(self, seeded_db):
        """Unknown guild_id (one with no row in guild_configs) → defaults to
        America/New_York instead of crashing."""
        import config
        result = config.server_time_to_local(18, 0, 99999999)
        assert any(c.isdigit() for c in result)


class TestFormatStormSlot:
    """format_storm_slot composes "<local> (HH:MM server time)" — the
    canonical user-facing string used on every storm surface (TimeSelectView
    buttons, /view_configuration, storm overview embeds, mail templates)."""

    def test_includes_server_time_label_spelled_out(self, seeded_db):
        """Must NEVER abbreviate to 'ST' — confuses users about which zone."""
        import config
        result = config.format_storm_slot(18, 0, TEST_GUILD_ID)
        assert "server time" in result
        assert "ST" not in result.split()  # not as a separate token like "(18:00 ST)"

    def test_uses_lowercase_server_time(self, seeded_db):
        import config
        result = config.format_storm_slot(18, 0, TEST_GUILD_ID)
        # Lowercase "server time" — matches the agreed copy convention
        assert "server time" in result
        assert "Server Time" not in result

    def test_includes_server_hh_mm(self, seeded_db):
        import config
        result = config.format_storm_slot(18, 0, TEST_GUILD_ID)
        assert "18:00" in result

    def test_includes_local_clock(self, seeded_db):
        """Local part uses lowercase am/pm, e.g. '4pm EDT'."""
        import config
        result = config.format_storm_slot(18, 0, TEST_GUILD_ID)
        assert ("pm" in result.lower()) or ("am" in result.lower())


class TestGetStormSlotLabels:
    """get_storm_slot_labels returns the two slot labels in display order."""

    def test_ds_returns_two(self, seeded_db):
        import config
        labels = config.get_storm_slot_labels("DS", TEST_GUILD_ID)
        assert len(labels) == 2

    def test_cs_returns_two(self, seeded_db):
        import config
        labels = config.get_storm_slot_labels("CS", TEST_GUILD_ID)
        assert len(labels) == 2

    def test_ds_labels_carry_18_and_23(self, seeded_db):
        """DS hardcoded slots are 18:00 and 23:00 server time."""
        import config
        labels = config.get_storm_slot_labels("DS", TEST_GUILD_ID)
        joined = " | ".join(labels)
        assert "18:00 server time" in joined
        assert "23:00 server time" in joined

    def test_cs_labels_carry_12_and_23(self, seeded_db):
        """CS hardcoded slots are 12:00 and 23:00 server time."""
        import config
        labels = config.get_storm_slot_labels("CS", TEST_GUILD_ID)
        joined = " | ".join(labels)
        assert "12:00 server time" in joined
        assert "23:00 server time" in joined


class TestGetStormSlotForKey:
    """get_storm_slot_for_key resolves a TimeSelectView selection ('1'/'2')
    back into (hour, minute) so mail builders can render the same slot."""

    def test_ds_key_1_is_18_00(self):
        from config import get_storm_slot_for_key
        assert get_storm_slot_for_key("DS", "1") == (18, 0)

    def test_ds_key_2_is_23_00(self):
        from config import get_storm_slot_for_key
        assert get_storm_slot_for_key("DS", "2") == (23, 0)

    def test_cs_key_1_is_12_00(self):
        from config import get_storm_slot_for_key
        assert get_storm_slot_for_key("CS", "1") == (12, 0)

    def test_cs_key_2_is_23_00(self):
        from config import get_storm_slot_for_key
        assert get_storm_slot_for_key("CS", "2") == (23, 0)

    def test_unknown_key_returns_none(self):
        from config import get_storm_slot_for_key
        assert get_storm_slot_for_key("DS", "3") is None
        assert get_storm_slot_for_key("DS", "18:00 Server Time") is None
