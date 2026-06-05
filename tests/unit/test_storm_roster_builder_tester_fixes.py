"""
Regression tests for the storm roster-builder tester fixes
(#273 / #274 / #275). The fixes live in storm_roster_builder.py; these
lock in the new behaviour so a later refactor can't silently regress it.

  #273  Strength-to-priority auto-fill balances squad power within a tier
        of equal-priority zones instead of front-loading the first one.
  #274  A sub can be returned from the sub pool back to assignable
        (the reverse of "Add all unassigned to Subs").
  #275  One team's pool excludes members already placed on the other
        team's auto-saved draft, so an "either" voter isn't re-offered.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import storm_roster_builder as srb
import storm_strategy as ss

from tests.unit.test_storm_roster_builder import _make_session


def _members(powers):
    """Members dict keyed '1'..'N' from a list of powers (index 0 -> '1')."""
    return {
        str(i + 1): {
            "key": str(i + 1),
            "name": f"M{i + 1:02d}",
            "discord_id": str(i + 1),
            "power": p,
            "not_on_discord": False,
        }
        for i, p in enumerate(powers)
    }


class TestPriorityGreedyBalancesEqualPriorityTier:
    """#273: two zones sharing a priority should split the strongest
    members between them, not pile the top cohort onto the first zone."""

    def test_equal_priority_zones_are_power_balanced(self):
        # 6 members, strongest first: M01=600 ... M06=100.
        members = _members([600, 500, 400, 300, 200, 100])
        zones = [
            ss.ZoneRow(zone="Oil Refinery I", max_players=3, min_power_a=0, priority=1),
            ss.ZoneRow(zone="Oil Refinery II", max_players=3, min_power_a=0, priority=1),
        ]
        sess = _make_session(team="A", members=members, preset_zones=zones)
        srb._auto_fill_session(sess, strategy="priority_greedy")

        ref_i = sess.assignments["Oil Refinery I"]
        ref_ii = sess.assignments["Oil Refinery II"]

        # Both equal-priority zones fill to capacity (6 starters, 3 + 3).
        assert len(ref_i) == 3
        assert len(ref_ii) == 3
        # The bug: zone I would hoard the top three (M01, M02, M03). The
        # fix splits them, so the second-strongest lands on the OTHER zone.
        assert set(ref_i) != {"1", "2", "3"}
        assert "2" in ref_ii

        def zpow(keys):
            return sum(members[k]["power"] for k in keys)

        # Balanced: per-zone totals stay close. Old behaviour gave
        # 1500 vs 600 (diff 900); the fix keeps the gap small.
        assert abs(zpow(ref_i) - zpow(ref_ii)) <= 150

    def test_higher_priority_tier_still_gets_the_strongest(self):
        # Priority is still respected BETWEEN tiers: the lone top-priority
        # zone takes the strongest members before the lower tier fills.
        members = _members([600, 500, 400, 300, 200, 100])
        zones = [
            ss.ZoneRow(zone="Top", max_players=2, min_power_a=0, priority=1),
            ss.ZoneRow(zone="Low A", max_players=2, min_power_a=0, priority=2),
            ss.ZoneRow(zone="Low B", max_players=2, min_power_a=0, priority=2),
        ]
        sess = _make_session(team="A", members=members, preset_zones=zones)
        srb._auto_fill_session(sess, strategy="priority_greedy")
        # The two strongest (M01, M02) go to the top-priority zone.
        assert set(sess.assignments["Top"]) == {"1", "2"}


class TestOtherTeamClaimedKeys:
    """#275: members already on the OTHER team's auto-saved draft are
    excluded when building this team's pool."""

    @staticmethod
    def _draft(payload):
        return {
            "session_json": json.dumps(payload),
            "event_date": "2026-06-01",
            "updated_at": "2026-06-01T00:00:00",
        }

    def test_collects_assignments_subs_and_paired_subs(self):
        payload = {
            "assignments_p1": {"Zone A": ["alice", "bob"]},
            "assignments_p2": {"Zone B": ["carol"]},
            "assignments_p3": {},
            "paired_subs_p1": {"dave": "erin"},
            "paired_subs_p2": {},
            "paired_subs_p3": {},
            "subs": ["frank", ""],  # blank entries ignored
        }
        with patch("config.get_roster_draft", return_value=self._draft(payload)) as m:
            claimed = srb._other_team_claimed_keys(123, "DS", "B")
        # Building Team B reads Team A's draft.
        m.assert_called_once_with(123, "DS", "A")
        assert claimed == {"alice", "bob", "carol", "dave", "erin", "frank"}

    def test_team_a_reads_team_b_draft(self):
        payload = {"assignments_p1": {"Z": ["x"]}}
        with patch("config.get_roster_draft", return_value=self._draft(payload)) as m:
            claimed = srb._other_team_claimed_keys(123, "CS", "A")
        m.assert_called_once_with(123, "CS", "B")
        assert claimed == {"x"}

    def test_no_draft_returns_empty(self):
        with patch("config.get_roster_draft", return_value=None):
            assert srb._other_team_claimed_keys(123, "DS", "A") == set()

    def test_single_team_alliance_has_no_other_team(self):
        # team="" (single-team) -> no other team, no DB read at all.
        with patch("config.get_roster_draft") as m:
            assert srb._other_team_claimed_keys(123, "DS", "") == set()
        m.assert_not_called()

    def test_unreadable_draft_is_best_effort(self):
        # A DB hiccup must never block the build.
        with patch("config.get_roster_draft", side_effect=Exception("db down")):
            assert srb._other_team_claimed_keys(123, "DS", "A") == set()

    # ── #277: stale-draft event_date scoping ───────────────────────────────

    @staticmethod
    def _draft_for(payload, saved_for_event_date):
        p = dict(payload)
        p["saved_for_event_date"] = saved_for_event_date
        return {
            "session_json": json.dumps(p),
            "event_date": saved_for_event_date,
            "updated_at": f"{saved_for_event_date}T00:00:00",
        }

    def test_stale_other_team_draft_is_ignored(self):
        # Other team's draft was saved for a PRIOR event; building this
        # event's pool must not exclude anyone from it (#277).
        payload = {"assignments_p1": {"Z": ["alice", "bob"]}}
        draft = self._draft_for(payload, "2026-06-01")
        with patch("config.get_roster_draft", return_value=draft):
            claimed = srb._other_team_claimed_keys(123, "DS", "B", "2026-06-08")
        assert claimed == set()

    def test_matching_event_date_still_excludes(self):
        # Same event_date -> the #275 cross-team exclusion still applies.
        payload = {"assignments_p1": {"Z": ["alice", "bob"]}}
        draft = self._draft_for(payload, "2026-06-08")
        with patch("config.get_roster_draft", return_value=draft):
            claimed = srb._other_team_claimed_keys(123, "DS", "B", "2026-06-08")
        assert claimed == {"alice", "bob"}

    def test_missing_saved_date_falls_through_to_exclusion(self):
        # Older draft with no saved_for_event_date: can't prove staleness,
        # so keep the #275 behaviour rather than silently dropping it.
        payload = {"assignments_p1": {"Z": ["alice"]}}
        draft = self._draft(payload)  # no saved_for_event_date key
        with patch("config.get_roster_draft", return_value=draft):
            claimed = srb._other_team_claimed_keys(123, "DS", "B", "2026-06-08")
        assert claimed == {"alice"}

    def test_no_current_event_date_falls_through_to_exclusion(self):
        # event_date unknown -> can't compare, keep #275 behaviour.
        payload = {"assignments_p1": {"Z": ["alice"]}}
        draft = self._draft_for(payload, "2026-06-01")
        with patch("config.get_roster_draft", return_value=draft):
            claimed = srb._other_team_claimed_keys(123, "DS", "B", None)
        assert claimed == {"alice"}


class TestSubsManageReturnSub:
    """#274: returning a sub pulls them out of the sub pool (and clears
    any pairing where they're the sub side) so they reappear assignable."""

    def test_return_sub_removes_from_pool_and_clears_pairing(self):
        members = _members([600, 500, 400, 300, 200, 100])
        sess = _make_session(team="A", members=members, sub_mode="paired")
        sess.subs = ["5"]
        sess.paired_subs["1"] = "5"  # M1 (primary) paired with M5 (sub)

        parent = MagicMock()
        parent.session = sess
        view = srb._SubsManageView(parent_view=parent)
        view.selected_sub = "5"
        view._guard_owner = AsyncMock(return_value=True)
        view._refresh_parent = AsyncMock()

        inter = MagicMock()
        inter.response.edit_message = AsyncMock()

        asyncio.run(view._on_return_sub(inter))

        # M5 left the sub pool and the pairing it was the sub side of.
        assert "5" not in sess.subs
        assert "1" not in sess.paired_subs
        view._refresh_parent.assert_awaited_once()

    def test_return_sub_noop_when_not_in_pool(self):
        members = _members([600, 500])
        sess = _make_session(team="A", members=members)
        sess.subs = []

        parent = MagicMock()
        parent.session = sess
        view = srb._SubsManageView(parent_view=parent)
        view.selected_sub = "1"  # not actually a sub
        view._guard_owner = AsyncMock(return_value=True)
        view._refresh_parent = AsyncMock()

        inter = MagicMock()
        inter.response.edit_message = AsyncMock()

        asyncio.run(view._on_return_sub(inter))

        # Nothing to return -> pool untouched, no parent refresh.
        assert sess.subs == []
        view._refresh_parent.assert_not_called()
