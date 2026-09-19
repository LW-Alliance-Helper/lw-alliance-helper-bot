"""Guild-config side of a data-removal request (#517).

Most of what the bot holds about a person is storm sign-up data, which is why
this file is named for storm rather than for config. Before this, the only
user-keyed delete in `config.py` was `remove_premium_assignment`, and it exists
for subscription management -- everything else was guild-scoped or
event-scoped, so nothing could remove a person.

The split that decides each table: a record a person WROTE keeps its
contribution and loses its attribution, and a record ABOUT a person goes whole.
`storm_signups` carries both in one row, which is the case worth the most care.
Deleting an officer's on-behalf vote would silently withdraw somebody else's
sign-up, and that somebody else did not ask for anything.
"""

from __future__ import annotations

import json
import re

import pytest

import config

REQUESTER = 5150
BYSTANDER = 9001
GUILD = 424242
OTHER_GUILD = 434343
DATE = "2026-08-22"


def rows(table, where="", params=()):
    with config._get_conn() as conn:
        return [
            dict(r)
            for r in conn.execute(
                f"SELECT * FROM {table} {('WHERE ' + where) if where else ''}",  # noqa: S608
                params,
            ).fetchall()
        ]


# ── The spec matches the schema ───────────────────────────────────────────────
#
# The operating rule was compiled by reading the tree, and reading is not the
# same as checking. A wrong column name in a delete path is the worst possible
# place for one: SQLite would raise on a live removal, halfway through.

_IDENT = re.compile(r"\b([a-z_][a-z0-9_]*)\s*(?==)")


def columns_of(table):
    with config._get_conn() as conn:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_every_table_the_removal_names_exists(temp_db):
    with config._get_conn() as conn:
        live = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    named = {t for t, _ in config._REMOVAL_DELETES} | {t for t, _, _ in config._REMOVAL_SCRUBS}
    assert named <= live, f"removal names tables that do not exist: {sorted(named - live)}"


def test_every_column_the_removal_writes_or_reads_exists(temp_db):
    checked = 0
    for table, where in config._REMOVAL_DELETES:
        live = columns_of(table)
        for name in _IDENT.findall(where):
            assert name in live, f"{table}.{name} does not exist"
            checked += 1
    for table, sets, where in config._REMOVAL_SCRUBS:
        live = columns_of(table)
        for name in _IDENT.findall(sets) + _IDENT.findall(where):
            assert name in live, f"{table}.{name} does not exist"
            checked += 1
    assert checked >= 15, "the spec got smaller than the rule it implements"


# ── Records about a person go whole ───────────────────────────────────────────


def test_their_own_signup_is_deleted(temp_db):
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, str(REQUESTER), "a")

    result = config.purge_user_data(REQUESTER, apply=True)

    assert result["deleted"]["storm_signups"] == 1
    assert rows("storm_signups") == []


def test_the_history_row_behind_it_goes_too(temp_db):
    """`storm_signups` UPSERTs, so the history table is where prior votes live.
    A removal that left the audit trail would not have removed anything."""
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, str(REQUESTER), "a")
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, str(REQUESTER), "b")

    config.purge_user_data(REQUESTER, apply=True)

    assert rows("storm_signup_history") == []


def test_an_officers_vote_for_them_is_still_about_them(temp_db):
    """Cast by somebody else, but the row says what this member is doing."""
    config.record_storm_vote(
        GUILD, "DS", DATE, BYSTANDER, str(REQUESTER), "either", is_on_behalf=True
    )

    config.purge_user_data(REQUESTER, apply=True)

    assert rows("storm_signups") == []


def test_a_walkthrough_dismissal_goes(temp_db):
    config.dismiss_walkthrough(GUILD, REQUESTER, "storm_signups_v1")
    config.dismiss_walkthrough(GUILD, BYSTANDER, "storm_signups_v1")

    config.purge_user_data(REQUESTER, apply=True)

    remaining = rows("walkthrough_dismissals")
    assert [r["user_id"] for r in remaining] == [BYSTANDER]


def test_a_held_builder_lock_goes(temp_db):
    config.claim_storm_session(GUILD, "DS", DATE, "A", REQUESTER)

    config.purge_user_data(REQUESTER, apply=True)

    assert rows("storm_session_state") == []


def test_the_record_of_a_dm_we_sent_them_goes(temp_db):
    config.record_power_refresh_dm_sent(GUILD, "DS", DATE, REQUESTER)

    config.purge_user_data(REQUESTER, apply=True)

    assert rows("storm_power_refresh_dms_sent") == []


def test_their_premium_assignment_goes(temp_db):
    """Which drops that guild's Premium. That is the honest consequence of a
    subscriber asking to be forgotten, not a bug to route around."""
    config.set_premium_assignment(REQUESTER, GUILD)

    result = config.purge_user_data(REQUESTER, apply=True)

    assert result["deleted"]["premium_assignments"] == 1
    assert config.get_premium_assignment_for_guild(GUILD) is None


def test_being_on_a_team_plan_is_a_record_about_them(temp_db):
    """Not in the enumerated rule -- `storm_team_plans` was named there only
    for `saved_by_user_id`. The row says "this member is committed to Team A",
    which is the same shape as a sign-up, so it takes the same answer."""
    ok, errors = config.save_storm_team_plan(
        GUILD, "DS", DATE, "A", [str(REQUESTER), str(BYSTANDER)], [], BYSTANDER
    )
    assert ok, errors

    config.purge_user_data(REQUESTER, apply=True)

    plan = config.get_storm_team_plan(GUILD, "DS", DATE, "A")
    assert plan["primaries"] == [str(BYSTANDER)]


# ── Records a person wrote keep the contribution ──────────────────────────────


def test_an_on_behalf_vote_keeps_the_vote_and_loses_the_officer(temp_db):
    """The case worth the most care. Deleting this row would withdraw a
    sign-up on behalf of somebody who did not ask for anything."""
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, "Wren", "a", is_on_behalf=True)

    result = config.purge_user_data(REQUESTER, apply=True)

    assert result["scrubbed"]["storm_signups"] == 1
    signup = rows("storm_signups")[0]
    assert signup["target_member_id"] == "Wren"
    assert signup["vote"] == "a"
    assert signup["voter_user_id"] == 0


def test_the_history_of_an_on_behalf_vote_is_scrubbed_not_deleted(temp_db):
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, "Wren", "a", is_on_behalf=True)

    config.purge_user_data(REQUESTER, apply=True)

    logged = rows("storm_signup_history")[0]
    assert logged["target_member_id"] == "Wren"
    assert logged["voter_user_id"] == 0


def test_a_team_plan_they_saved_stays_the_alliances(temp_db):
    """The officer's record of who the alliance committed in-game. Not theirs
    to take back on the way out."""
    ok, errors = config.save_storm_team_plan(
        GUILD, "DS", DATE, "A", ["Wren", "Kestrel"], [], REQUESTER
    )
    assert ok, errors

    result = config.purge_user_data(REQUESTER, apply=True)

    assert result["scrubbed"]["storm_team_plans"] == 2
    plan = config.get_storm_team_plan(GUILD, "DS", DATE, "A")
    assert sorted(plan["primaries"]) == ["Kestrel", "Wren"]
    assert {r["saved_by_user_id"] for r in rows("storm_team_plans")} == {0}


def test_a_roster_image_they_posted_stays_findable(temp_db):
    config.save_roster_image_ref(GUILD, "DS", DATE, "A", 111, 222, REQUESTER)

    config.purge_user_data(REQUESTER, apply=True)

    ref = rows("storm_roster_images")[0]
    assert (ref["channel_id"], ref["message_id"]) == (111, 222)
    assert ref["posted_by_user_id"] == 0


def test_a_plan_row_is_counted_once_whichever_column_names_them(temp_db):
    """They saved a plan that includes themselves. The row is about them, so it
    is deleted -- and the scrub predicate excludes it, so the two passes cannot
    both claim it."""
    ok, errors = config.save_storm_team_plan(
        GUILD, "DS", DATE, "A", [str(REQUESTER), "Wren"], [], REQUESTER
    )
    assert ok, errors

    result = config.purge_user_data(REQUESTER, apply=True)

    assert result["deleted"]["storm_team_plans"] == 1
    assert result["scrubbed"]["storm_team_plans"] == 1


# ── Install metadata ──────────────────────────────────────────────────────────


def test_the_guild_survives_its_owner_asking_to_be_forgotten(temp_db):
    """The row is about a guild; only the two IDs on it are about a person.
    Deleting it would take an alliance's support record because its owner
    asked, which is a different request -- `/admin forget_guild` is that one."""
    config.upsert_guild_install_metadata(
        guild_id=GUILD,
        guild_name="Wind Runners",
        owner_id=REQUESTER,
        installer_user_id=REQUESTER,
    )

    result = config.purge_user_data(REQUESTER, apply=True)

    assert result["scrubbed"]["guild_install_metadata"] == 1
    meta = config.get_guild_install_metadata(GUILD)
    assert meta["guild_name"] == "Wind Runners"
    assert meta["owner_id"] == 0
    assert meta["installer_user_id"] is None


def test_only_the_column_that_names_them_is_cleared(temp_db):
    config.upsert_guild_install_metadata(
        guild_id=GUILD,
        guild_name="Wind Runners",
        owner_id=BYSTANDER,
        installer_user_id=REQUESTER,
    )

    config.purge_user_data(REQUESTER, apply=True)

    meta = config.get_guild_install_metadata(GUILD)
    assert meta["owner_id"] == BYSTANDER
    assert meta["installer_user_id"] is None


# ── Roster drafts ─────────────────────────────────────────────────────────────
#
# The one place a member's Discord ID lives inside a blob rather than a column,
# and the one that outlives its event: the table keeps one row per team, reused
# across weeks, so a draft saved once can hold an ID indefinitely.


def draft_payload():
    """The shape `storm_roster_builder._serialize_session` writes."""
    return {
        "version": 1,
        "selected_preset_name": "Standard",
        "subs": [str(REQUESTER)],
        "assignments_p1": {"Zone 1": [str(REQUESTER), str(BYSTANDER)], "Zone 2": ["Kestrel"]},
        "assignments_p2": {},
        "assignments_p3": {},
        "paired_subs_p1": {str(BYSTANDER): str(REQUESTER)},
        "paired_subs_p2": {},
        "paired_subs_p3": {},
        "below_floor_overrides_p1": [str(REQUESTER)],
        "below_floor_overrides_p2": [],
        "below_floor_overrides_p3": [],
        "team_plan_applied": True,
        "saved_for_event_date": DATE,
        "member_names_at_save": {str(REQUESTER): "Kevin", str(BYSTANDER): "Wren"},
    }


def test_a_saved_draft_loses_every_trace_of_them(temp_db):
    config.save_roster_draft(
        GUILD, "DS", "A", session_json=json.dumps(draft_payload()), event_date=DATE
    )

    result = config.purge_user_data(REQUESTER, apply=True)

    assert result["scrubbed"]["storm_roster_drafts"] == 1
    saved = json.loads(config.get_roster_draft(GUILD, "DS", "A")["session_json"])
    assert str(REQUESTER) not in json.dumps(saved)
    assert saved["subs"] == []
    assert saved["assignments_p1"]["Zone 1"] == [str(BYSTANDER)]
    assert saved["below_floor_overrides_p1"] == []
    assert saved["member_names_at_save"] == {str(BYSTANDER): "Wren"}


def test_the_pairing_goes_whole_rather_than_half(temp_db):
    """Half a pairing is not a pairing, and the surviving half would name a
    primary whose sub had silently vanished."""
    config.save_roster_draft(
        GUILD, "DS", "A", session_json=json.dumps(draft_payload()), event_date=DATE
    )

    config.purge_user_data(REQUESTER, apply=True)

    saved = json.loads(config.get_roster_draft(GUILD, "DS", "A")["session_json"])
    assert saved["paired_subs_p1"] == {}


def test_the_rest_of_the_draft_is_left_alone(temp_db):
    config.save_roster_draft(
        GUILD, "DS", "A", session_json=json.dumps(draft_payload()), event_date=DATE
    )

    config.purge_user_data(REQUESTER, apply=True)

    saved = json.loads(config.get_roster_draft(GUILD, "DS", "A")["session_json"])
    assert saved["selected_preset_name"] == "Standard"
    assert saved["assignments_p1"]["Zone 2"] == ["Kestrel"]
    assert saved["saved_for_event_date"] == DATE


def test_a_draft_that_never_mentioned_them_is_not_rewritten(temp_db):
    payload = draft_payload()
    payload.pop("member_names_at_save")
    payload["subs"] = []
    payload["assignments_p1"] = {"Zone 1": [str(BYSTANDER)]}
    payload["paired_subs_p1"] = {}
    payload["below_floor_overrides_p1"] = []
    config.save_roster_draft(GUILD, "DS", "A", session_json=json.dumps(payload), event_date=DATE)

    result = config.purge_user_data(REQUESTER, apply=True)

    assert "storm_roster_drafts" not in result["scrubbed"]


def test_a_draft_that_will_not_parse_is_deleted_rather_than_left(temp_db):
    """Unreachable via the writer, which is `json.dumps`. A removal path is the
    wrong place to assume a row is well-formed."""
    config.save_roster_draft(
        GUILD, "DS", "A", session_json="{not json " + str(REQUESTER), event_date=DATE
    )

    result = config.purge_user_data(REQUESTER, apply=True)

    assert result["deleted"]["storm_roster_drafts"] == 1
    assert config.get_roster_draft(GUILD, "DS", "A") is None


# ── Scope ─────────────────────────────────────────────────────────────────────


def test_another_guilds_data_for_the_same_person_goes_too(temp_db):
    """A person, not a person-in-a-guild. Someone who asks to be forgotten is
    not asking to be forgotten by one alliance."""
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, str(REQUESTER), "a")
    config.record_storm_vote(OTHER_GUILD, "CS", DATE, REQUESTER, str(REQUESTER), "b")

    result = config.purge_user_data(REQUESTER, apply=True)

    assert result["deleted"]["storm_signups"] == 2


def test_a_member_named_by_roster_name_is_out_of_reach(temp_db):
    """`target_member_id` is a Discord ID for members on Discord and a roster
    name for those who are not. This route matches the first only, which is the
    scope of the issue: no Discord identity, no request through Discord."""
    config.record_storm_vote(GUILD, "DS", DATE, BYSTANDER, "Kestrel", "a", is_on_behalf=True)

    config.purge_user_data(REQUESTER, apply=True)

    assert len(rows("storm_signups")) == 1


# ── Preview ───────────────────────────────────────────────────────────────────


def test_a_preview_changes_nothing(temp_db):
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, str(REQUESTER), "a")
    config.dismiss_walkthrough(GUILD, REQUESTER, "storm_signups_v1")

    preview = config.purge_user_data(REQUESTER)

    assert preview["applied"] is False
    assert preview["deleted"]["storm_signups"] == 1
    assert preview["deleted"]["walkthrough_dismissals"] == 1
    assert len(rows("storm_signups")) == 1
    assert len(rows("walkthrough_dismissals")) == 1


def test_a_preview_counts_what_the_run_then_touches(temp_db):
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, str(REQUESTER), "a")
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, "Wren", "b", is_on_behalf=True)
    config.dismiss_walkthrough(GUILD, REQUESTER, "storm_signups_v1")
    config.save_roster_image_ref(GUILD, "DS", DATE, "A", 111, 222, REQUESTER)
    config.save_roster_draft(
        GUILD, "DS", "A", session_json=json.dumps(draft_payload()), event_date=DATE
    )
    config.upsert_guild_install_metadata(
        guild_id=GUILD, guild_name="Wind Runners", owner_id=REQUESTER
    )

    preview = config.purge_user_data(REQUESTER)
    applied = config.purge_user_data(REQUESTER, apply=True)

    assert preview["deleted"] == applied["deleted"]
    assert preview["scrubbed"] == applied["scrubbed"]


def test_an_unknown_id_reports_nothing_rather_than_failing(temp_db):
    config.record_storm_vote(GUILD, "DS", DATE, BYSTANDER, str(BYSTANDER), "a")

    result = config.purge_user_data(404404, apply=True)

    assert result == {"deleted": {}, "scrubbed": {}, "applied": True}


def test_running_it_twice_is_a_no_op_the_second_time(temp_db):
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, str(REQUESTER), "a")
    config.record_storm_vote(GUILD, "DS", DATE, REQUESTER, "Wren", "b", is_on_behalf=True)

    config.purge_user_data(REQUESTER, apply=True)
    again = config.purge_user_data(REQUESTER, apply=True)

    assert again["deleted"] == {}
    assert again["scrubbed"] == {}
