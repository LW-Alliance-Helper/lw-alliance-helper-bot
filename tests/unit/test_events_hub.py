"""Smoke + shape tests for the /events hub (#249)."""

from datetime import date
from unittest.mock import MagicMock

import pytest

from events_hub import (
    AE_EVENT_PRESETS,
    ANCHOR_DATE_EXAMPLES,
    EVENTS_HUB_BTN_CREATE,
    EVENTS_HUB_BTN_DELETE,
    EVENTS_HUB_BTN_LOG,
    EVENTS_HUB_BTN_PAUSE,
    EVENTS_HUB_BTN_TODAY,
    EVENTS_HUB_BTN_UPCOMING,
    EVENTS_HUB_BTN_WARNING,
    EVENTS_HUB_TITLE,
    _EventsHubView,
    _preset_by_key,
    describe_event_schedule,
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
    assert EVENTS_HUB_TITLE == "📣 Event Announcements"
    assert EVENTS_HUB_BTN_TODAY == "📅 Today's events"
    assert EVENTS_HUB_BTN_UPCOMING == "🔜 Upcoming events"
    assert EVENTS_HUB_BTN_LOG == "📜 Event log"
    assert EVENTS_HUB_BTN_CREATE == "➕ Create an event"
    assert EVENTS_HUB_BTN_WARNING == "✏️ Edit 5-minute warning"
    assert EVENTS_HUB_BTN_PAUSE == "⏸️ Pause or resume"
    assert EVENTS_HUB_BTN_DELETE == "🗑️ Delete an event"


# ── Hub view smoke test ──────────────────────────────────────────────────────


def test_hub_view_has_seven_buttons_with_expected_labels():
    """The view should always render exactly the 7 hub buttons in the
    documented order. A failure here likely means a button got added,
    removed, or re-ordered without intent.

    Went from six to seven in #566: Edit 5-minute warning sits after Create,
    which shifted Pause and Delete one position right. That was the
    deliberate trade — see the layout docstring on `_EventsHubView`."""
    view = _EventsHubView(bot=MagicMock(), guild_id=1, owner_user_id=42)
    labels = [item.label for item in view.children]
    assert labels == [
        EVENTS_HUB_BTN_TODAY,
        EVENTS_HUB_BTN_UPCOMING,
        EVENTS_HUB_BTN_LOG,
        EVENTS_HUB_BTN_CREATE,
        EVENTS_HUB_BTN_WARNING,
        EVENTS_HUB_BTN_PAUSE,
        EVENTS_HUB_BTN_DELETE,
    ]


def test_hub_view_button_layout_two_rows():
    """Read-row (today/upcoming/log) sits on row 0; write-row
    (create/edit warning/pause/delete) sits on row 1. Layout decisions
    like this affect the visual hierarchy; pin it explicitly."""
    view = _EventsHubView(bot=MagicMock(), guild_id=1, owner_user_id=42)
    rows = {item.label: item.row for item in view.children}
    assert rows[EVENTS_HUB_BTN_TODAY] == 0
    assert rows[EVENTS_HUB_BTN_UPCOMING] == 0
    assert rows[EVENTS_HUB_BTN_LOG] == 0
    assert rows[EVENTS_HUB_BTN_CREATE] == 1
    assert rows[EVENTS_HUB_BTN_WARNING] == 1
    assert rows[EVENTS_HUB_BTN_PAUSE] == 1
    assert rows[EVENTS_HUB_BTN_DELETE] == 1


def test_pause_button_sits_between_create_and_delete():
    """Pause is the reversible middle ground; placing it next to the red
    Delete button is what makes it discoverable as the alternative.

    This is the constraint that decided where Edit 5-minute warning went:
    Delete stays last and Pause stays its neighbour, so the new button
    had to go earlier in the row rather than on the end."""
    view = _EventsHubView(bot=MagicMock(), guild_id=1, owner_user_id=42)
    labels = [item.label for item in view.children]
    assert (
        labels.index(EVENTS_HUB_BTN_CREATE)
        < labels.index(EVENTS_HUB_BTN_PAUSE)
        < labels.index(EVENTS_HUB_BTN_DELETE)
    )


# ── Schedule summary ─────────────────────────────────────────────────────────


class TestDescribeEventSchedule:
    """`describe_event_schedule` renders the hub embed's event lines AND
    the resume preview, so an officer deciding whether to re-anchor sees
    the same string the list will show afterwards."""

    TODAY = date(2026, 7, 30)

    def _repeating(self, **over):
        ev = {
            "schedule_type": "repeating",
            "anchor_date": "2026-07-30",
            "interval_days": 3,
        }
        ev.update(over)
        return ev

    def test_repeating_names_next_instance_and_interval(self):
        out = describe_event_schedule(self._repeating(), today=self.TODAY)
        assert "Next event instance" in out
        assert "every 3 days" in out

    def test_fires_today_reads_as_today(self):
        out = describe_event_schedule(self._repeating(), today=self.TODAY)
        assert "(today)" in out

    def test_tomorrow_reads_as_tomorrow(self):
        ev = self._repeating(anchor_date="2026-07-31")
        assert "(tomorrow)" in describe_event_schedule(ev, today=self.TODAY)

    def test_further_out_counts_days(self):
        ev = self._repeating(anchor_date="2026-08-01")
        assert "(in 2 days)" in describe_event_schedule(ev, today=self.TODAY)

    def test_stale_anchor_still_projects_forward(self):
        """A season-old anchor is exactly the paused case: the cycle math
        still lands on a future date, which is what the resume preview
        shows so the officer can judge whether it drifted."""
        ev = self._repeating(anchor_date="2026-01-05")
        out = describe_event_schedule(ev, today=self.TODAY)
        assert "Next event instance" in out

    def test_manual_event(self):
        ev = {"schedule_type": "manual", "anchor_date": "", "interval_days": 0}
        assert describe_event_schedule(ev, today=self.TODAY).startswith("Manual")

    def test_repeating_without_anchor_reads_as_manual(self):
        """No anchor means nothing to count from — the draft editor is the
        only way it can fire, same as a manual event."""
        ev = self._repeating(anchor_date="")
        assert describe_event_schedule(ev, today=self.TODAY).startswith("Manual")

    def test_zero_interval_is_not_computable(self):
        ev = self._repeating(interval_days=0)
        assert "not yet computable" in describe_event_schedule(ev, today=self.TODAY)

    def test_garbage_anchor_degrades_instead_of_raising(self):
        """The hub embed calls this for every event; one bad row must not
        blank the whole field."""
        ev = self._repeating(anchor_date="not-a-date")
        assert describe_event_schedule(ev, today=self.TODAY) == (
            "Schedule invalid (re-create the event)"
        )

    def test_defaults_to_real_today(self):
        assert describe_event_schedule(self._repeating())


def test_anchor_date_examples_cover_the_shapes_officers_type():
    """The prompt, the retry notice and the modal placeholder all quote
    this constant; it must keep advertising the numeric form that used to
    be rejected."""
    for shape in ("March 30", "7/30", "2026-07-30", "today"):
        assert shape in ANCHOR_DATE_EXAMPLES
