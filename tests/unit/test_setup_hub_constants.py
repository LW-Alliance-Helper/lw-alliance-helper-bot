"""Regression guard for the hub-label constants in setup_hub (#208).

If a label gets renamed intentionally, update the literal here too. The
point is to catch *accidental* renames (typo, autoformatter doing
something cute, etc.) that would silently propagate everywhere these
constants are imported.
"""

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
    assert HUB_BTN_CS == "🏜️ Canyon Storm"
    assert HUB_BTN_SHINY == "🌟 Shiny Tasks"
    assert HUB_BTN_MEMBERS == "👥 Member Sync"
    assert HUB_BTN_SURVEY == "📋 Survey"
    assert HUB_BTN_BREAKDOWN == "📊 Growth Breakdown"


def test_storm_setup_nav_builds_correctly():
    assert STORM_SETUP_NAV["DS"] == "/setup → ⚔️ Desert Storm"
    assert STORM_SETUP_NAV["CS"] == "/setup → 🏜️ Canyon Storm"
    # Always exactly two keys — guards against accidentally adding a
    # third storm type without a deliberate refactor.
    assert set(STORM_SETUP_NAV) == {"DS", "CS"}
