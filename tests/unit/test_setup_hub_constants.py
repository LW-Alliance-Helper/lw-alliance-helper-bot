"""Regression guard for the hub-label constants in setup_hub (#208).

If a label gets renamed intentionally, update the literal here too. The
point is to catch *accidental* renames (typo, autoformatter doing
something cute, etc.) that would silently propagate everywhere these
constants are imported.
"""

import sqlite3

from setup_hub import (
    HUB_BTN_BIRTHDAYS,
    HUB_BTN_BREAKDOWN,
    HUB_BTN_CS,
    HUB_BTN_DS,
    HUB_BTN_EVENTS,
    HUB_BTN_GROWTH,
    HUB_BTN_MEMBERS,
    HUB_BTN_RELEASE_ANN,
    HUB_BTN_RESET,
    HUB_BTN_SETUP_WIZARD,
    HUB_BTN_SHINY,
    HUB_BTN_SURVEY,
    HUB_BTN_TRAIN,
    HUB_BTN_VIEW_CONFIG,
    STORM_SETUP_NAV,
)
from survey_hub import (
    SURVEY_HUB_BTN_ADD,
    SURVEY_HUB_BTN_EDIT,
    SURVEY_HUB_BTN_POST,
    SURVEY_HUB_BTN_REMIND,
    SURVEY_HUB_BTN_REMOVE,
    SURVEY_HUB_BTN_SETUP,
    SURVEY_HUB_BTN_TRANSLATE,
)


def test_hub_button_labels_match_expected_text():
    assert HUB_BTN_SETUP_WIZARD == "⚙️ Open setup wizard"
    assert HUB_BTN_VIEW_CONFIG == "🗂️ View configuration"
    assert HUB_BTN_RESET == "🗑️ Reset configuration"
    assert HUB_BTN_RELEASE_ANN == "📢 Release announcements"
    assert HUB_BTN_TRAIN == "🚂 Train"
    assert HUB_BTN_GROWTH == "📈 Growth"
    assert HUB_BTN_BIRTHDAYS == "🎂 Birthdays"
    assert HUB_BTN_EVENTS == "📣 Events"
    assert HUB_BTN_DS == "⚔️ Desert Storm"
    assert HUB_BTN_CS == "🛡️ Canyon Storm"
    assert HUB_BTN_SHINY == "🌟 Shiny Tasks"
    assert HUB_BTN_MEMBERS == "👥 Member Sync"
    assert HUB_BTN_SURVEY == "📋 Survey"
    assert HUB_BTN_BREAKDOWN == "📊 Growth Breakdown"


def test_survey_hub_button_labels_match_expected_text():
    assert SURVEY_HUB_BTN_ADD == "➕ Add Survey"
    assert SURVEY_HUB_BTN_EDIT == "✏️ Edit Survey"
    assert SURVEY_HUB_BTN_SETUP == "⚙️ Set Up Survey"
    assert SURVEY_HUB_BTN_REMOVE == "🗑️ Remove Survey"
    assert SURVEY_HUB_BTN_POST == "📮 Post Survey"
    assert SURVEY_HUB_BTN_REMIND == "🔔 Reminders"
    assert SURVEY_HUB_BTN_TRANSLATE == "🌐 Survey Translation"


def test_storm_setup_nav_builds_correctly():
    assert STORM_SETUP_NAV["DS"] == "/setup → ⚔️ Desert Storm"
    assert STORM_SETUP_NAV["CS"] == "/setup → 🛡️ Canyon Storm"
    # Always exactly two keys — guards against accidentally adding a
    # third storm type without a deliberate refactor.
    assert set(STORM_SETUP_NAV) == {"DS", "CS"}


# ── the hub survives an unreadable database ───────────────────────────────────


def test_the_hub_still_builds_when_the_config_database_cannot_be_read():
    """`_SetupHubView.__init__` reads `guild_configs` for one button label.
    Letting that failure escape takes the whole of `/setup` down over a
    transient database problem, which is a bad trade for a label.

    It also made three VS tests environment-dependent: they constructed the
    hub directly and only passed where a real database file happened to
    exist, so they went red the moment CI started running on `dev`.
    """
    from unittest.mock import patch

    import setup_hub

    boom = sqlite3.OperationalError("unable to open database file")
    with patch("config.get_config", side_effect=boom):
        view = setup_hub._SetupHubView(None, 1, 1, is_premium=True)

    # Falls through to the same default a guild with no row yet gets.
    assert view.btn_release_announcements.label.endswith("ON")
