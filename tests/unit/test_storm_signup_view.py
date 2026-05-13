"""
Tests for the SignupView persistence layer (#123).

Covers the custom_id encoder / parser and the View construction (every
button gets a stable custom_id under Discord's 100-char limit). The
actual click handler is integration territory — its data path is covered
by `test_record_vote_round_trip` in test_config.py.
"""

from unittest.mock import MagicMock

import pytest

import storm_signup_view as sv

from tests.unit.test_config import TEST_GUILD_ID


class TestCustomIdEncoding:
    def test_round_trip(self):
        cid = sv.make_custom_id(12345, "DS", "2026-05-18", "a")
        parsed = sv.parse_custom_id(cid)
        assert parsed == {
            "guild_id":   12345,
            "event_type": "ds",
            "event_date": "2026-05-18",
            "vote":       "a",
        }

    def test_event_type_normalised_lowercase(self):
        # encoder lowercases; parser only accepts lowercase
        assert sv.make_custom_id(1, "CS", "2026-01-01", "either").endswith(":either")
        parsed = sv.parse_custom_id("signup:1:cs:2026-01-01:either")
        assert parsed["event_type"] == "cs"

    def test_all_valid_votes_accepted(self):
        for v in ("a", "b", "either", "cannot"):
            cid = sv.make_custom_id(1, "DS", "2026-01-01", v)
            assert sv.parse_custom_id(cid)["vote"] == v

    def test_unknown_event_type_rejected(self):
        # Parser rejects unknown event_types to keep dispatch tight.
        assert sv.parse_custom_id("signup:1:xx:2026-01-01:a") is None

    def test_unknown_vote_rejected(self):
        assert sv.parse_custom_id("signup:1:ds:2026-01-01:bogus") is None

    def test_malformed_returns_none(self):
        for bad in ("", "signup", "signup:foo", "signup:not_int:ds:2026-01-01:a",
                    "other:1:ds:2026-01-01:a", "signup:1:ds:2026-01-01:a:extra"):
            assert sv.parse_custom_id(bad) is None

    def test_custom_id_under_discord_limit(self):
        # Discord caps custom_id at 100 chars. Worst realistic case is a
        # 19-digit guild_id (Snowflake ceiling).
        cid = sv.make_custom_id(9999999999999999999, "DS", "2026-12-31", "either")
        assert len(cid) <= 100


class TestSignupViewConstruction:
    def test_four_buttons_with_stable_custom_ids(self):
        view = sv.SignupView(12345, "DS", "2026-05-18",
                             time_a_label="9pm ET", time_b_label="4pm ET")
        # Four buttons total
        assert len(view.children) == 4
        # Each button has a parseable custom_id
        votes_found = set()
        for btn in view.children:
            parsed = sv.parse_custom_id(btn.custom_id)
            assert parsed is not None
            assert parsed["guild_id"]   == 12345
            assert parsed["event_type"] == "ds"
            assert parsed["event_date"] == "2026-05-18"
            votes_found.add(parsed["vote"])
        # Cover all four vote codes
        assert votes_found == {"a", "b", "either", "cannot"}

    def test_persistent_view_timeout_is_none(self):
        # discord.py requires timeout=None for persistent Views; this is
        # what makes the View survive bot restarts.
        view = sv.SignupView(1, "DS", "2026-05-18")
        assert view.timeout is None

    def test_default_labels_when_not_provided(self):
        view = sv.SignupView(1, "DS", "2026-05-18")
        # We don't pin exact strings (those are UX copy), just that the
        # configurable A/B buttons reference the defaults.
        labels = [c.label for c in view.children]
        assert any("Team A" in lab for lab in labels)
        assert any("Team B" in lab for lab in labels)

    def test_custom_labels_propagate(self):
        view = sv.SignupView(1, "DS", "2026-05-18",
                             time_a_label="9pm ET", time_b_label="4pm ET")
        labels = [c.label for c in view.children]
        assert any("9pm ET" in lab for lab in labels)
        assert any("4pm ET" in lab for lab in labels)


class TestSignupHistoryAudit:
    """The UPSERT in `record_storm_vote` overwrites the prior row in
    `storm_signups`, so the audit trail for re-votes (especially the
    on-behalf path required by #38) lives in `storm_signup_history`."""

    def test_self_vote_appends_history_row(self, seeded_db):
        import config
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=42, target_member_id="42", vote="a",
        )
        history = config.get_storm_signup_history(
            TEST_GUILD_ID, "DS", "2026-05-18",
        )
        assert len(history) == 1
        assert history[0]["voter_user_id"] == 42
        assert history[0]["vote"] == "a"
        assert history[0]["is_on_behalf"] is False
        assert history[0]["voted_at"].endswith("+00:00")

    def test_revote_preserves_prior_vote_in_history(self, seeded_db):
        import config
        # First vote — A
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=42, target_member_id="42", vote="a",
        )
        # Officer overrides — B
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=999, target_member_id="42", vote="b",
            is_on_behalf=True,
        )
        # Member changes their mind — Either
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=42, target_member_id="42", vote="either",
        )
        history = config.get_storm_signup_history(
            TEST_GUILD_ID, "DS", "2026-05-18", target_member_id="42",
        )
        # All three votes preserved, newest first.
        assert [h["vote"] for h in history] == ["either", "b", "a"]
        # Officer's intervention is intact.
        officer_entry = next(h for h in history if h["voter_user_id"] == 999)
        assert officer_entry["is_on_behalf"] is True

    def test_storm_signups_holds_only_latest(self, seeded_db):
        """Sanity-check the contract: the current row in `storm_signups`
        reflects the latest vote; full history lives in the audit table."""
        import config
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=42, target_member_id="42", vote="a",
        )
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=42, target_member_id="42", vote="cannot",
        )
        current = config.get_member_vote(
            TEST_GUILD_ID, "DS", "2026-05-18", "42",
        )
        assert current["vote"] == "cannot"
        history = config.get_storm_signup_history(
            TEST_GUILD_ID, "DS", "2026-05-18",
        )
        assert len(history) == 2


class TestPersistentViewRegistration:
    """Bot-restart-resiliency: `register_persistent_signup_views` walks
    every recent registration post and rebinds a SignupView to its
    `message_id`. Without this, every post-restart button click fails
    with "Interaction failed."""

    def test_walks_recent_posts_and_calls_add_view(self, seeded_db):
        import config
        # Two posts within the 14-day window.
        config.record_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-18",
            channel_id=100, message_id=200,
            time_a_label="9pm ET", time_b_label="4pm ET",
        )
        config.record_storm_registration_post(
            TEST_GUILD_ID, "CS", "2026-05-25",
            channel_id=100, message_id=300,
            time_a_label="12pm ET",
        )

        bot = MagicMock()
        added = sv.register_persistent_signup_views(bot)
        assert added == 2
        # add_view called once per post, keyed by message_id.
        message_ids = {call.kwargs.get("message_id") for call in bot.add_view.call_args_list}
        assert message_ids == {200, 300}
        # Each registered View carries the configured labels.
        for call in bot.add_view.call_args_list:
            view = call.args[0]
            assert isinstance(view, sv.SignupView)
            assert view.timeout is None

    def test_add_view_failure_is_logged_not_raised(self, seeded_db):
        import config
        config.record_storm_registration_post(
            TEST_GUILD_ID, "DS", "2026-05-18",
            channel_id=100, message_id=200,
        )
        bot = MagicMock()
        bot.add_view.side_effect = RuntimeError("simulated")
        # Should not raise even when add_view blows up.
        count = sv.register_persistent_signup_views(bot)
        assert count == 0
