"""Smoke tests for the Buddy System Discord surfaces (#289).

Custom-id round-trips, embed shaping, the persistent view's buttons, the
tier/role-aware hub button grid, and the persistent click handler's defer +
premium gate. Discord objects are mocked (conftest helpers); Sheet I/O is
patched so nothing touches gspread.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import buddy
import buddy_ui
import buddy_hub
from buddy_ui import BuddyProfessionView, make_buddy_custom_id, parse_buddy_custom_id
from tests.conftest import make_mock_interaction


def _result():
    return buddy.PairingResult(
        pairs=[buddy.Pair("Walt", "1", "Eve", "3")],
        unpaired_wl=[buddy.Member("Wanda", "2", buddy.WAR_LEADER)],
        unpaired_eng=[buddy.Member("Zed", "5", buddy.ENGINEER)],
    )


# ── custom_id ─────────────────────────────────────────────────────────────────


def test_custom_id_roundtrip_and_malformed():
    cid = make_buddy_custom_id(42, "wl")
    assert parse_buddy_custom_id(cid) == {"guild_id": 42, "code": "wl"}
    assert parse_buddy_custom_id("buddy:42:eng")["code"] == "eng"
    assert parse_buddy_custom_id("buddy:42:whoami")["code"] == "whoami"
    # malformed
    assert parse_buddy_custom_id("buddy:abc:wl") is None
    assert parse_buddy_custom_id("nope:42:wl") is None
    assert parse_buddy_custom_id("buddy:42:bogus") is None
    assert parse_buddy_custom_id("") is None


# ── persistent view ───────────────────────────────────────────────────────────


def test_persistent_view_has_three_stable_buttons():
    view = BuddyProfessionView(123)
    cids = [c.custom_id for c in view.children]
    assert make_buddy_custom_id(123, "wl") in cids
    assert make_buddy_custom_id(123, "eng") in cids
    assert make_buddy_custom_id(123, "whoami") in cids
    assert view.timeout is None  # persistent


# ── embed ─────────────────────────────────────────────────────────────────────


def test_list_embed_shows_pairs_and_unpaired_sections():
    embed = buddy_ui.build_buddy_list_embed(_result(), doubling=False)
    desc = embed.description
    # Single markdown block: header + pairs + unpaired label lines (no fields).
    assert desc.startswith("## ")
    assert "Walt" in desc and "Eve" in desc
    assert "**Walt**" not in desc  # names are not bolded
    assert "War Leaders without a buddy: Wanda" in desc
    assert "Engineers without a buddy: Zed" in desc
    assert not embed.fields


def test_render_buddy_dm_substitutes_and_tolerates_typos():
    from defaults import DEFAULT_BUDDY_DM

    out = buddy_ui._render_buddy_dm(
        DEFAULT_BUDDY_DM, name="Walt", buddy="Eve", buddy_role="Engineer"
    )
    assert "Walt" in out and "Eve" in out and "Engineer" in out
    # Unknown placeholder renders literally rather than crashing.
    out2 = buddy_ui._render_buddy_dm("Hi {name}, ping {oops}", name="Walt", buddy="", buddy_role="")
    assert out2 == "Hi Walt, ping {oops}"


def test_describe_my_buddy_variants():
    r = _result()
    assert "Eve" in buddy_ui.describe_my_buddy(r, "1", "Walt")  # WL → engineer
    assert "Walt" in buddy_ui.describe_my_buddy(r, "3", "Eve")  # eng → WL
    assert "without a buddy" in buddy_ui.describe_my_buddy(r, "2", "Wanda")  # unpaired WL
    assert "couldn't find you" in buddy_ui.describe_my_buddy(r, "99", "Ghost")  # unknown


# ── hub button grid (tier/role-aware) ─────────────────────────────────────────


def _hub(is_leader, is_premium):
    bot = AsyncMock()
    return buddy_hub._BuddyHubView(bot, 1, 1, is_leader=is_leader, is_premium=is_premium)


def test_hub_member_only_sees_lookup_buttons():
    view = _hub(is_leader=False, is_premium=False)
    labels = [c.label for c in view.children]
    assert any("Who's my buddy" in l for l in labels)
    assert any("View buddy list" in l for l in labels)
    assert not any("Manage pairings" in l for l in labels)
    assert not any("Auto-assign" in l for l in labels)


def test_hub_leadership_sees_management_and_premium_buttons():
    view = _hub(is_leader=True, is_premium=True)
    labels = [c.label for c in view.children]
    for needle in (
        "Manage pairings",
        "Post buddy list",
        "Open setup",
        "Auto-assign",
        "self-service",
    ):
        assert any(needle in l for l in labels), needle


# ── persistent click handler ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whoami_click_works_without_premium():
    inter = make_mock_interaction(guild_id=1)
    inter.data = {"custom_id": make_buddy_custom_id(1, "whoami")}
    inter.client = AsyncMock()
    with (
        patch("config.get_buddy_config", return_value={"engineer_doubling": 0}),
        patch("buddy_ui.compute_current", return_value=_result()),
    ):
        await buddy_ui._handle_profession_click(inter, "whoami")
    inter.response.defer.assert_awaited()
    inter.followup.send.assert_awaited()


@pytest.mark.asyncio
@pytest.mark.free_tier_only
async def test_profession_click_blocked_without_premium():
    inter = make_mock_interaction(guild_id=1)
    inter.data = {"custom_id": make_buddy_custom_id(1, "wl")}
    inter.client = AsyncMock()
    with (
        patch("config.get_buddy_config", return_value={"engineer_doubling": 0}),
        patch("premium.is_premium", new=AsyncMock(return_value=False)),
    ):
        await buddy_ui._handle_profession_click(inter, "wl")
    # Acked with a premium-required message; no sheet write attempted.
    inter.followup.send.assert_awaited()
    sent = inter.followup.send.await_args.args[0]
    assert "Premium" in sent


@pytest.mark.asyncio
async def test_profession_click_writes_and_acks_when_premium():
    inter = make_mock_interaction(guild_id=1)
    inter.data = {"custom_id": make_buddy_custom_id(1, "wl")}
    inter.client = AsyncMock()
    inter.client.get_channel = MagicMock(return_value=None)
    after = _result()
    data = {
        "ok": True,
        "before": _result(),
        "after": after,
        "notification": "note",
        "role": "wl",
        "buddies": ["Eve"],
    }
    with (
        patch(
            "config.get_buddy_config",
            return_value={"engineer_doubling": 0, "notify_channel_id": 0, "dm_enabled": 0},
        ),
        patch("premium.is_premium", new=AsyncMock(return_value=True)),
        patch("buddy_ui._apply_profession_change", return_value=data),
        patch("buddy_ui.refresh_persistent_message", new=AsyncMock()),
    ):
        await buddy_ui._handle_profession_click(inter, "wl")
    inter.followup.send.assert_awaited()
    sent = inter.followup.send.await_args.args[0]
    assert "War Leader" in sent and "Eve" in sent
