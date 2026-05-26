"""
Integration tests for the /events hub command (post-#249).

The pre-hub /events show [date] handler was a single slash with an
optional date arg. The hub's `📅 Today's events` button dispatches
into `events_hub._open_today_editor`, which lifted the same
event-grouping + cycle-math logic. These tests exercise that lifted
logic against a seeded SQLite DB; only `scheduler.post_editor` is
mocked so we don't try to send Discord messages.

The two date-arg-parsing tests from the pre-hub suite (named-date
parsing, numeric-date parsing, unparseable date) were removed
because the hub button always uses today's date — no date arg.
"""

from __future__ import annotations

import os
import sys
from datetime import date as date_cls
from unittest.mock import patch, AsyncMock, MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID, make_mock_interaction


# ── Helpers ───────────────────────────────────────────────────────────────────

def _seed_leadership_setup(guild_id: int = TEST_GUILD_ID) -> "config.GuildConfig":
    import config
    cfg = config.get_or_create_config(guild_id)
    cfg.leadership_role_name  = "Leadership"
    cfg.leadership_channel_id = 111111111111111111
    cfg.timezone              = "America/New_York"
    cfg.setup_complete        = 1
    config.save_config(cfg)
    return cfg


def _seed_repeating_event(
    guild_id: int,
    short_key: str,
    *,
    name: str | None = None,
    anchor: str = "2026-04-01",
    interval: int = 3,
    default_time: str = "22:00",
    draft_channel_id: int = 333333333333333333,
    announcement_channel_id: int = 444444444444444444,
    timezone: str = "America/New_York",
    five_min_warning: int = 1,
    blurb: str = "{name} at {time} ({server_time}).",
) -> None:
    import config
    config.save_guild_event(guild_id, {
        "short_key":               short_key,
        "name":                    name or short_key.title(),
        "timezone":                timezone,
        "default_time":            default_time,
        "announcement_blurb":      blurb,
        "schedule_type":           "repeating",
        "anchor_date":             anchor,
        "interval_days":           interval,
        "draft_channel_id":        draft_channel_id,
        "announcement_channel_id": announcement_channel_id,
        "draft_time":              "12:00",
        "five_min_warning":        five_min_warning,
        "active":                  1,
    })


def _make_events_interaction(guild_id: int = TEST_GUILD_ID):
    interaction = make_mock_interaction(guild_id=guild_id)
    role = MagicMock()
    role.name = "Leadership"
    interaction.user.roles = [role]
    interaction.response.defer        = AsyncMock()
    interaction.response.send_message = AsyncMock()
    interaction.followup.send         = AsyncMock()
    return interaction


def _captured_followups(interaction):
    out = []
    for call in interaction.followup.send.call_args_list:
        args, kwargs = call
        c = args[0] if args else kwargs.get("content")
        out.append((c, kwargs))
    return out


async def _run_today_editor(interaction, today_value: date_cls):
    """Run events_hub._open_today_editor with today's date pinned to
    `today_value`. Returns the post_editor mock so callers can assert
    on its call args."""
    from events_hub import _open_today_editor
    bot = AsyncMock()
    with patch("scheduler.post_editor", new_callable=AsyncMock) as mock_post, \
         patch("events_hub.date_cls") as mock_date:
        mock_date.today.return_value = today_value
        mock_date.fromisoformat.side_effect = date_cls.fromisoformat
        await _open_today_editor(bot, interaction)
    return mock_post


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestEventsHubTodayEditor:
    """The 📅 Today's events button finds the next event date and opens
    the editor — same shape as the pre-hub /events show callback,
    lifted verbatim into events_hub._open_today_editor."""

    @pytest.mark.asyncio
    async def test_single_repeating_event_opens_editor(self, seeded_db):
        _seed_leadership_setup()
        # Anchor 2026-04-01 + cycle 3 → fires 4-1, 4-4, 4-7, 4-10, ...
        _seed_repeating_event(TEST_GUILD_ID, "marauder",
                              name="Plague Marauder", anchor="2026-04-01",
                              interval=3, default_time="22:15")

        interaction = _make_events_interaction()
        mock_post = await _run_today_editor(interaction, date_cls(2026, 4, 5))

        followups = [c for c, _ in _captured_followups(interaction)]
        assert any(c and "April 5" in c and "April 7" in c for c in followups), (
            f"Expected 'next event date' hint mentioning Apr 5 + Apr 7; got {followups}"
        )

        mock_post.assert_called_once()
        call_args, call_kwargs = mock_post.call_args
        event_list = call_args[1]
        event_key  = call_args[2]
        run_date   = call_args[3]
        assert run_date == date_cls(2026, 4, 7)
        assert len(event_list) == 1
        assert event_list[0]["key"]   == "marauder"
        assert event_list[0]["name"]  == "Plague Marauder"
        assert event_list[0]["dt"].hour == 22
        assert event_list[0]["dt"].minute == 15
        assert event_key == f"event-{TEST_GUILD_ID}-2026-04-07-hub"
        assert call_kwargs["draft_channel_id"]        == 333333333333333333
        assert call_kwargs["announcement_channel_id"] == 444444444444444444
        assert call_kwargs["five_min_warning"]        is True

    @pytest.mark.asyncio
    async def test_multiple_cycle_types_picks_soonest(self, seeded_db):
        _seed_leadership_setup()
        # Group A: every-3-day events anchored 2026-04-01 → 4-1, 4-4, 4-7, ...
        _seed_repeating_event(TEST_GUILD_ID, "marauder",
                              name="Plague Marauder", anchor="2026-04-01",
                              interval=3, default_time="22:15")
        _seed_repeating_event(TEST_GUILD_ID, "siege",
                              name="Zombie Siege", anchor="2026-04-01",
                              interval=3, default_time="22:45")
        # Group B: weekly event anchored 2026-04-06 → 4-6, 4-13, ...
        _seed_repeating_event(TEST_GUILD_ID, "weekly_thing",
                              name="Weekly Thing", anchor="2026-04-06",
                              interval=7, default_time="20:00")

        interaction = _make_events_interaction()
        mock_post = await _run_today_editor(interaction, date_cls(2026, 4, 5))

        # From 4-5: Group A next is 4-7, Group B next is 4-6. Soonest is 4-6.
        mock_post.assert_called_once()
        event_list = mock_post.call_args[0][1]
        run_date   = mock_post.call_args[0][3]
        assert run_date == date_cls(2026, 4, 6)
        assert len(event_list) == 1
        assert event_list[0]["key"] == "weekly_thing"

    @pytest.mark.asyncio
    async def test_target_date_is_event_day_no_hint(self, seeded_db):
        """When today IS an event day, no 'not an event day' hint shows."""
        _seed_leadership_setup()
        _seed_repeating_event(TEST_GUILD_ID, "marauder",
                              name="Plague Marauder", anchor="2026-04-01",
                              interval=3, default_time="22:15")

        interaction = _make_events_interaction()
        mock_post = await _run_today_editor(interaction, date_cls(2026, 4, 4))

        followups = [c for c, _ in _captured_followups(interaction)]
        assert not any(c and "is not an event day" in c for c in followups), (
            f"Should not show 'not an event day' hint when today IS event day; got {followups}"
        )
        mock_post.assert_called_once()
        assert mock_post.call_args[0][3] == date_cls(2026, 4, 4)


class TestEventsHubErrorBranches:
    """Each of the info-message error paths."""

    @pytest.mark.asyncio
    async def test_no_events_configured(self, seeded_db):
        _seed_leadership_setup()
        interaction = _make_events_interaction()
        mock_post = await _run_today_editor(interaction, date_cls(2026, 4, 5))

        followups = [c for c, _ in _captured_followups(interaction)]
        assert any(c and "No events configured" in c for c in followups), (
            f"Expected 'No events configured' message; got {followups}"
        )
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_only_manual_events(self, seeded_db):
        _seed_leadership_setup()
        import config
        config.save_guild_event(TEST_GUILD_ID, {
            "short_key":               "one_off",
            "name":                    "One-Off Event",
            "timezone":                "America/New_York",
            "default_time":            "22:00",
            "announcement_blurb":      "X",
            "schedule_type":           "manual",
            "anchor_date":             "",
            "interval_days":           0,
            "draft_channel_id":        0,
            "announcement_channel_id": 0,
            "draft_time":              "12:00",
            "five_min_warning":        0,
            "active":                  1,
        })

        interaction = _make_events_interaction()
        mock_post = await _run_today_editor(interaction, date_cls(2026, 4, 5))

        followups = [c for c, _ in _captured_followups(interaction)]
        assert any(c and "No repeating events" in c for c in followups), (
            f"Expected 'No repeating events' message; got {followups}"
        )
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_anchor_date(self, seeded_db):
        _seed_leadership_setup()
        import config
        config.save_guild_event(TEST_GUILD_ID, {
            "short_key":               "broken",
            "name":                    "Broken Event",
            "timezone":                "America/New_York",
            "default_time":            "22:00",
            "announcement_blurb":      "X",
            "schedule_type":           "repeating",
            "anchor_date":             "not-a-date",
            "interval_days":           3,
            "draft_channel_id":        0,
            "announcement_channel_id": 0,
            "draft_time":              "12:00",
            "five_min_warning":        0,
            "active":                  1,
        })

        interaction = _make_events_interaction()
        mock_post = await _run_today_editor(interaction, date_cls(2026, 4, 5))

        followups = [c for c, _ in _captured_followups(interaction)]
        assert any(c and "invalid anchor dates" in c for c in followups), (
            f"Expected 'invalid anchor dates' message; got {followups}"
        )
        mock_post.assert_not_called()
