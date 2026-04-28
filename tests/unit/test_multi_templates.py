"""
Unit tests for the multi-template features (Sprint C — Phase 2).

Covers:
  * Train templates: schema migration (legacy `prompt_template` lifts to a
    "Default" entry), name-based lookup, default template selection.
  * Storm templates: same shape, keyed by (guild, event_type, team).
  * Multi-survey: each survey is independent, persistent button identifies
    by survey_id.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID
from config import OGV_GUILD_ID


# ── Train templates ───────────────────────────────────────────────────────────

class TestTrainTemplates:

    def test_legacy_prompt_template_lifts_to_default_entry(self, seeded_db):
        """An older row with only the legacy `prompt_template` column populated
        should expose a single 'Default' template via the new API."""
        import config

        # Save in the legacy format (no templates kwarg → wraps as Default)
        config.save_train_config(
            TEST_GUILD_ID, "Train Schedule", ["A", "B"], ["x", "y"],
            "Hello {name}, write a {theme} blurb.",
            "Default (match the theme)",
        )

        cfg = config.get_train_config(TEST_GUILD_ID)
        templates = cfg["templates"]
        assert len(templates) == 1
        assert templates[0]["name"]     == "Default"
        assert "Hello {name}" in templates[0]["template"]
        assert cfg["default_template"]   == "Default"

    def test_save_and_load_multi_templates(self, seeded_db):
        import config
        templates = [
            {"name": "Default",  "template": "Default body for {name}"},
            {"name": "Birthday", "template": "Happy birthday {name}!"},
            {"name": "War",      "template": "Battle blurb for {name}"},
        ]
        config.save_train_config(
            TEST_GUILD_ID, "Train Schedule", ["a"], ["b"], "",
            "Default", templates=templates, default_template="Birthday",
        )
        cfg = config.get_train_config(TEST_GUILD_ID)

        assert len(cfg["templates"]) == 3
        names = [t["name"] for t in cfg["templates"]]
        assert names == ["Default", "Birthday", "War"]
        assert cfg["default_template"] == "Birthday"

    def test_get_prompt_template_named_lookup(self, seeded_db):
        import config
        from train import get_prompt_template

        config.save_train_config(
            TEST_GUILD_ID, "Train Schedule", ["a"], ["b"], "",
            "Default",
            templates=[
                {"name": "Default",  "template": "default body"},
                {"name": "Birthday", "template": "birthday body"},
            ],
            default_template="Default",
        )

        assert get_prompt_template(TEST_GUILD_ID)                            == "default body"
        assert get_prompt_template(TEST_GUILD_ID, template_name="Birthday")  == "birthday body"
        # Unknown name falls back to first template
        assert get_prompt_template(TEST_GUILD_ID, template_name="Nope")      == "default body"

    def test_get_train_template_names_returns_list(self, seeded_db):
        import config
        from train import get_train_template_names

        config.save_train_config(
            TEST_GUILD_ID, "Train Schedule", ["a"], ["b"], "",
            "Default",
            templates=[
                {"name": "Welcome",  "template": "x"},
                {"name": "Milestone","template": "y"},
            ],
            default_template="Welcome",
        )
        assert get_train_template_names(TEST_GUILD_ID) == ["Welcome", "Milestone"]

    def test_default_template_pinned_when_default_name_missing(self, seeded_db):
        """If `default_template` doesn't match any saved name, normalize to the first."""
        import config
        config.save_train_config(
            TEST_GUILD_ID, "Train Schedule", ["a"], ["b"], "",
            "Default",
            templates=[{"name": "Solo", "template": "z"}],
            default_template="Birthday",  # doesn't exist
        )
        cfg = config.get_train_config(TEST_GUILD_ID)
        assert cfg["default_template"] == "Solo"

    def test_build_chatgpt_prompt_uses_named_template(self, seeded_db):
        import config
        from train import build_chatgpt_prompt

        config.save_train_config(
            TEST_GUILD_ID, "Train Schedule", ["a"], ["b"], "",
            "Default",
            templates=[
                {"name": "Default",  "template": "Default for {name}: {theme}"},
                {"name": "Custom",   "template": "Custom blurb for {name} ({tone})"},
            ],
            default_template="Default",
        )

        out = build_chatgpt_prompt(
            "Alice", "Birthday", "Funny", "",
            guild_id=TEST_GUILD_ID, template_name="Custom",
        )
        assert "Custom blurb for Alice" in out
        assert "Funny" in out


# ── Storm templates ───────────────────────────────────────────────────────────

class TestStormTemplates:

    def test_legacy_mail_template_lifts_to_default_entry(self, seeded_db):
        """A legacy single mail_template should expose as the 'Default' entry."""
        import config

        config.save_storm_config(
            TEST_GUILD_ID, "DS", "DS Assignments",
            "Old DS template body",
            "", "", "", "", "", "",
            "America/New_York", 0,
        )
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert len(cfg["templates"]) == 1
        assert cfg["templates"][0]["name"]     == "Default"
        assert cfg["templates"][0]["template"] == "Old DS template body"
        assert cfg["default_template"]          == "Default"

    def test_save_and_load_multi_storm_templates(self, seeded_db):
        import config
        templates = [
            {"name": "Default",     "template": "Default DS body"},
            {"name": "Alt-Format",  "template": "Alternative format body"},
        ]
        config.save_storm_config(
            TEST_GUILD_ID, "DS", "DS Assignments",
            "ignored fallback",
            "", "", "", "", "", "",
            "America/New_York", 0,
            templates=templates, default_template="Alt-Format",
        )
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert [t["name"] for t in cfg["templates"]] == ["Default", "Alt-Format"]
        assert cfg["default_template"] == "Alt-Format"

    def test_get_storm_template_named_lookup(self, seeded_db):
        import config
        from config import get_storm_template

        config.save_storm_config(
            TEST_GUILD_ID, "CS", "CS Assignments",
            "ignored",
            "", "", "", "", "", "",
            "America/New_York", 0,
            templates=[
                {"name": "Default",  "template": "default cs body"},
                {"name": "Holiday",  "template": "holiday cs body"},
            ],
            default_template="Default",
        )
        assert get_storm_template(TEST_GUILD_ID, "CS")                       == "default cs body"
        assert get_storm_template(TEST_GUILD_ID, "CS", "Holiday")             == "holiday cs body"
        assert get_storm_template(TEST_GUILD_ID, "CS", template_name="Nope")  == "default cs body"

    def test_template_names_helper(self, seeded_db):
        import config
        from config import get_storm_template_names

        config.save_storm_config(
            TEST_GUILD_ID, "DS", "DS", "ignored",
            "", "", "", "", "", "",
            "America/New_York", 0,
            templates=[
                {"name": "Plan A", "template": "x"},
                {"name": "Plan B", "template": "y"},
                {"name": "Plan C", "template": "z"},
            ],
            default_template="Plan A",
        )
        assert get_storm_template_names(TEST_GUILD_ID, "DS") == ["Plan A", "Plan B", "Plan C"]

    def test_ds_and_cs_are_independent(self, seeded_db):
        """Templates are scoped per (guild_id, event_type) — DS and CS don't leak."""
        import config

        config.save_storm_config(
            TEST_GUILD_ID, "DS", "DS Tab", "DS body only",
            "", "", "", "", "", "",
            "America/New_York", 0,
        )
        config.save_storm_config(
            TEST_GUILD_ID, "CS", "CS Tab", "CS body only",
            "", "", "", "", "", "",
            "America/New_York", 0,
        )

        ds = config.get_storm_config(TEST_GUILD_ID, "DS")
        cs = config.get_storm_config(TEST_GUILD_ID, "CS")
        assert ds["templates"][0]["template"] == "DS body only"
        assert cs["templates"][0]["template"] == "CS body only"
        assert ds["tab_name"] == "DS Tab"
        assert cs["tab_name"] == "CS Tab"


# ── Multi-survey ──────────────────────────────────────────────────────────────

class TestMultiSurvey:

    def test_default_survey_appears_in_list_surveys(self, seeded_db):
        import config
        config.save_survey_config(
            TEST_GUILD_ID, "Squad Powers", "Survey History",
            [{"key": "q1", "label": "Power", "type": "text", "options": [], "placeholder": "", "max_chars": 10}],
            "Welcome to the survey",
        )
        surveys = config.list_surveys(TEST_GUILD_ID)
        assert len(surveys) == 1
        assert surveys[0]["survey_id"]   == "default"
        assert surveys[0]["survey_name"] == "Default"
        assert len(surveys[0]["questions"]) == 1

    def test_save_and_list_extra_surveys(self, seeded_db):
        import config
        config.save_extra_survey(
            TEST_GUILD_ID, "season_5_recap",
            survey_name="Season 5 Recap",
            questions=[{"key": "fav", "label": "Favorite event"}],
            intro_message="Looking back at S5",
            survey_channel_id=1234,
            notify_channel_id=5678,
        )
        config.save_extra_survey(
            TEST_GUILD_ID, "monthly_check",
            survey_name="Monthly Check-in",
        )
        surveys = config.list_surveys(TEST_GUILD_ID)
        assert len(surveys) == 3   # default + 2 extras
        names = {s["survey_name"] for s in surveys}
        assert names == {"Default", "Monthly Check-in", "Season 5 Recap"}

    def test_get_survey_by_id(self, seeded_db):
        import config
        config.save_extra_survey(
            TEST_GUILD_ID, "alpha",
            survey_name="Alpha Survey",
            tab_squad_powers="Alpha Tab",
            survey_channel_id=999,
        )
        s = config.get_survey(TEST_GUILD_ID, "alpha")
        assert s is not None
        assert s["survey_name"]        == "Alpha Survey"
        assert s["tab_squad_powers"]   == "Alpha Tab"
        assert s["survey_channel_id"]  == 999

    def test_get_survey_default_always_returns(self, seeded_db):
        import config
        s = config.get_survey(TEST_GUILD_ID, "default")
        assert s is not None
        assert s["survey_id"]   == "default"

    def test_get_survey_unknown_id_returns_none(self, seeded_db):
        import config
        assert config.get_survey(TEST_GUILD_ID, "no-such-survey") is None

    def test_delete_extra_survey(self, seeded_db):
        import config
        config.save_extra_survey(TEST_GUILD_ID, "tmp", survey_name="Temporary")
        assert config.get_survey(TEST_GUILD_ID, "tmp") is not None
        assert config.delete_extra_survey(TEST_GUILD_ID, "tmp") is True
        assert config.get_survey(TEST_GUILD_ID, "tmp") is None

    def test_cannot_delete_default_survey(self, seeded_db):
        import config
        # Default survey is in guild_survey_config, not deletable via this helper.
        assert config.delete_extra_survey(TEST_GUILD_ID, "default") is False
        # Default still appears in the list
        surveys = config.list_surveys(TEST_GUILD_ID)
        assert any(s["survey_id"] == "default" for s in surveys)

    def test_extra_surveys_isolated_per_guild(self, seeded_db):
        import config
        config.save_extra_survey(TEST_GUILD_ID, "shared_id", survey_name="Guild A version")
        config.save_extra_survey(OGV_GUILD_ID,  "shared_id", survey_name="Guild B version")

        a = config.get_survey(TEST_GUILD_ID, "shared_id")
        b = config.get_survey(OGV_GUILD_ID,  "shared_id")
        assert a["survey_name"] == "Guild A version"
        assert b["survey_name"] == "Guild B version"
