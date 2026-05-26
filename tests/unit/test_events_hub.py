"""Smoke + shape tests for the /events hub (#249)."""
from unittest.mock import MagicMock

import pytest

from events_hub import (
    AE_EVENT_PRESETS,
    EVENTS_HUB_BTN_CREATE,
    EVENTS_HUB_BTN_DELETE,
    EVENTS_HUB_BTN_LOG,
    EVENTS_HUB_BTN_TODAY,
    EVENTS_HUB_BTN_UPCOMING,
    EVENTS_HUB_TITLE,
    _EventsHubView,
    _preset_by_key,
)


# ── Preset library shape ─────────────────────────────────────────────────────


def test_preset_library_has_expected_entries():
    """Six canonical presets: 3 AE variants + Zombie Siege + 2 boss
    events. If a preset is added or removed deliberately, update this
    test along with the library — it's a guard against accidental
    deletions, not a freeze."""
    assert len(AE_EVENT_PRESETS) == 6
    keys = {p["key"] for p in AE_EVENT_PRESETS}
    assert keys == {
        "ae_plague_marauder",
        "ae_marshalls_guard",
        "ae_sandworm",
        "zombie_siege",
        "glacieradon",
        "sky_predator",
    }


@pytest.mark.parametrize("preset", AE_EVENT_PRESETS)
def test_every_preset_has_required_fields(preset):
    """Every preset entry must carry the four fields the wizard reads:
    key, name, blurb, interval_days, and a stage_note for the picker
    dropdown description."""
    assert preset["key"]
    assert preset["name"]
    assert preset["stage_note"]
    assert preset["blurb"]
    assert isinstance(preset["interval_days"], int)
    assert preset["interval_days"] > 0


def test_preset_keys_are_unique():
    """Two presets sharing a key would silently collide when saved
    (short_key uniqueness in guild_events). Catch that here."""
    keys = [p["key"] for p in AE_EVENT_PRESETS]
    assert len(keys) == len(set(keys))


def test_preset_by_key_resolves_known_and_unknown():
    assert _preset_by_key("ae_plague_marauder") is not None
    assert _preset_by_key("ae_plague_marauder")["name"] == "Alliance Exercise: Plague Marauder"
    assert _preset_by_key("nonexistent_key") is None


# ── Hub button labels match constants ────────────────────────────────────────


def test_hub_button_labels_match_expected_text():
    """Regression guard for accidental rename. If a label is changed
    intentionally, update the literal here too — the point is to catch
    typos or autoformatter rewrites that would silently propagate to
    every importing module."""
    assert EVENTS_HUB_TITLE        == "📣 Event Announcements"
    assert EVENTS_HUB_BTN_TODAY    == "📅 Today's events"
    assert EVENTS_HUB_BTN_UPCOMING == "📆 Upcoming events"
    assert EVENTS_HUB_BTN_LOG      == "📜 Event log"
    assert EVENTS_HUB_BTN_CREATE   == "➕ Create an event"
    assert EVENTS_HUB_BTN_DELETE   == "🗑️ Delete an event"


# ── Hub view smoke test ──────────────────────────────────────────────────────


def test_hub_view_has_five_buttons_with_expected_labels():
    """The view should always render exactly the 5 hub buttons in the
    documented order. A failure here likely means a button got added,
    removed, or re-ordered without intent."""
    view = _EventsHubView(bot=MagicMock(), guild_id=1, owner_user_id=42)
    labels = [item.label for item in view.children]
    assert labels == [
        EVENTS_HUB_BTN_TODAY,
        EVENTS_HUB_BTN_UPCOMING,
        EVENTS_HUB_BTN_LOG,
        EVENTS_HUB_BTN_CREATE,
        EVENTS_HUB_BTN_DELETE,
    ]


def test_hub_view_button_layout_two_rows():
    """Read-row (today/upcoming/log) sits on row 0; write-row
    (create/delete) sits on row 1. Layout decisions like this affect
    the visual hierarchy; pin it explicitly."""
    view = _EventsHubView(bot=MagicMock(), guild_id=1, owner_user_id=42)
    rows = {item.label: item.row for item in view.children}
    assert rows[EVENTS_HUB_BTN_TODAY]    == 0
    assert rows[EVENTS_HUB_BTN_UPCOMING] == 0
    assert rows[EVENTS_HUB_BTN_LOG]      == 0
    assert rows[EVENTS_HUB_BTN_CREATE]   == 1
    assert rows[EVENTS_HUB_BTN_DELETE]   == 1
