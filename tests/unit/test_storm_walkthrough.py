"""
Tests for storm_walkthrough.py + the walkthrough_dismissals layer (#130).

The persistent-View / modal flow is integration territory; these tests
cover the pure-data layer (dismissal table) and the tour content shape.
"""

import pytest

import storm_walkthrough as sw


from tests.unit.test_config import TEST_GUILD_ID


class TestWalkthroughDismissals:
    def test_unseen_returns_false(self, seeded_db):
        import config
        assert config.is_walkthrough_dismissed(
            TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY,
        ) is False

    def test_dismiss_then_check(self, seeded_db):
        import config
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY)
        assert config.is_walkthrough_dismissed(
            TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY,
        ) is True

    def test_dismiss_idempotent(self, seeded_db):
        import config
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY)
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY)
        # Still just one record; still dismissed.
        assert config.is_walkthrough_dismissed(
            TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY,
        ) is True

    def test_per_user_isolation(self, seeded_db):
        import config
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY)
        # A different user in the same guild still gets offered the tour.
        assert config.is_walkthrough_dismissed(
            TEST_GUILD_ID, 99, sw.STORM_SIGNUPS_TOUR_KEY,
        ) is False

    def test_per_guild_isolation(self, seeded_db):
        import config
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY)
        # The same user in a different guild still gets offered the tour.
        assert config.is_walkthrough_dismissed(
            TEST_GUILD_ID + 1, 42, sw.STORM_SIGNUPS_TOUR_KEY,
        ) is False

    def test_versioned_key_isolates_dismissals(self, seeded_db):
        import config
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, "storm_signups_v0")
        # A v1 walkthrough should NOT be auto-dismissed just because v0 was.
        assert config.is_walkthrough_dismissed(
            TEST_GUILD_ID, 42, "storm_signups_v1",
        ) is False


class TestTourContent:
    def test_tour_has_at_least_three_steps(self):
        # The design spec calls for ~6 steps; lock in a floor so a
        # future content edit can't accidentally drop the tour to a
        # single message.
        assert len(sw._STORM_SIGNUPS_TOUR_STEPS) >= 3

    def test_first_step_starts_with_step_label(self):
        # Each step in the tour body starts with a bolded "Step N / M"
        # so the user can tell where they are.
        assert sw._STORM_SIGNUPS_TOUR_STEPS[0].startswith("**Step 1 ")

    def test_steps_are_individually_short(self):
        # Discord ephemeral messages render best at a few sentences each.
        # 800 chars is a soft cap that still lets us write paragraphs.
        for step in sw._STORM_SIGNUPS_TOUR_STEPS:
            assert len(step) < 800, f"Step too long ({len(step)} chars):\n{step}"
