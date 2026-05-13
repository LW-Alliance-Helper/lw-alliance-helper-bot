"""
Tests for the SignupView persistence layer (#123).

Covers the custom_id encoder / parser and the View construction (every
button gets a stable custom_id under Discord's 100-char limit). The
actual click handler is integration territory — its data path is covered
by `test_record_vote_round_trip` in test_config.py.
"""

import pytest

import storm_signup_view as sv


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
