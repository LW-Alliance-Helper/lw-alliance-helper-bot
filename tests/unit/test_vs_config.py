"""Unit tests for the `guild_vs_config` table (#399 / #448).

Covers the schema round-trip and the two things most likely to bite later:
`save_vs_config` being a *partial* update (so a multi-step wizard can't clobber
its own earlier answers), and the tracking mode surviving a round-trip intact,
since every bracket-dependent surface reads it to tell a deliberate choice from
missing data.
"""

import pytest

import config


@pytest.fixture()
def db(temp_db):
    """A fresh DB with the schema applied."""
    config.init_db()
    return temp_db


def test_unconfigured_guild_reads_all_off_defaults(db):
    cfg = config.get_vs_config(1234)
    assert cfg["enabled"] == 0
    assert cfg["tab_name"] == "Alliance Duel (VS)"
    assert cfg["own_tag"] == "" and cfg["own_warzone"] == ""
    assert cfg["score_prompt_enabled"] == 0
    assert cfg["day_theme_enabled"] == 0
    assert cfg["last_score_prompt_fired"] == ""


def test_fallback_dict_covers_every_writable_column(db):
    # The classic drift: a column added to CREATE TABLE but not to the
    # fallback, so an unconfigured guild KeyErrors where a configured one works.
    cfg = config.get_vs_config(1234)
    missing = [c for c in config._VS_CONFIG_COLUMNS if c not in cfg]
    assert missing == [], f"get_vs_config fallback is missing {missing}"


def test_save_then_read_round_trips(db):
    config.save_vs_config(
        1234,
        enabled=1,
        own_tag="ABC",
        own_warzone="1234",
        tracking_mode=config.VS_MODE_OWN_ALLIANCE,
        score_prompt_time="09:30",
        score_prompt_channel_id=999,
    )
    cfg = config.get_vs_config(1234)
    assert cfg["enabled"] == 1
    assert cfg["own_tag"] == "ABC"
    assert cfg["tracking_mode"] == config.VS_MODE_OWN_ALLIANCE
    assert cfg["score_prompt_time"] == "09:30"
    assert cfg["score_prompt_channel_id"] == 999


def test_saving_one_field_leaves_the_others_alone(db):
    """The reason this saver is partial rather than full-field.

    A wizard writes one answer per step. If saving the posting time reset the
    tracking mode to its default, the alliance would silently lose a choice
    they were explicitly asked to make.
    """
    config.save_vs_config(1234, tracking_mode=config.VS_MODE_OWN_ALLIANCE, own_tag="ABC")
    config.save_vs_config(1234, score_prompt_time="21:00")

    cfg = config.get_vs_config(1234)
    assert cfg["tracking_mode"] == config.VS_MODE_OWN_ALLIANCE
    assert cfg["own_tag"] == "ABC"
    assert cfg["score_prompt_time"] == "21:00"


def test_booleans_are_coerced_for_sqlite(db):
    config.save_vs_config(1234, enabled=True, day_theme_enabled=False)
    cfg = config.get_vs_config(1234)
    assert cfg["enabled"] == 1
    assert cfg["day_theme_enabled"] == 0


def test_an_unknown_tracking_mode_falls_back_to_full_bracket(db):
    config.save_vs_config(1234, tracking_mode="whatever")
    assert config.get_vs_config(1234)["tracking_mode"] == config.VS_MODE_FULL_BRACKET


def test_unknown_keys_are_ignored_rather_than_raising(db):
    # A retired column passed by an older caller shouldn't take a wizard down.
    assert config.save_vs_config(1234, enabled=1, retired_column="x") is True
    assert config.get_vs_config(1234)["enabled"] == 1


def test_save_with_nothing_writable_is_a_no_op(db):
    assert config.save_vs_config(1234, not_a_column=1) is False
    assert config.get_vs_config(1234)["enabled"] == 0


def test_enabled_guild_listing_drives_the_loops(db):
    config.save_vs_config(1, enabled=1)
    config.save_vs_config(2, enabled=0)
    config.save_vs_config(3, enabled=1)
    assert sorted(config.list_vs_enabled_guild_ids()) == [1, 3]


def test_clear_removes_the_row(db):
    config.save_vs_config(1234, enabled=1, own_tag="ABC")
    config.clear_vs_config(1234)
    cfg = config.get_vs_config(1234)
    assert cfg["enabled"] == 0
    assert cfg["own_tag"] == ""


def test_init_db_is_rerunnable(db):
    # Migrations live in try/except loops precisely so a re-run is a no-op.
    config.save_vs_config(1234, enabled=1, tracking_mode=config.VS_MODE_OWN_ALLIANCE)
    config.init_db()
    cfg = config.get_vs_config(1234)
    assert cfg["enabled"] == 1
    assert cfg["tracking_mode"] == config.VS_MODE_OWN_ALLIANCE


def test_config_mode_constants_match_the_feature_module(db):
    # config.py duplicates these so it stays importable without the feature
    # module; if they ever drift, the mode gate silently stops matching.
    import alliance_duel as ad

    assert config.VS_MODE_OWN_ALLIANCE == ad.MODE_OWN_ALLIANCE
    assert config.VS_MODE_FULL_BRACKET == ad.MODE_FULL_BRACKET
