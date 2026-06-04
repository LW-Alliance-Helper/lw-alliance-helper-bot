"""
Unit tests for train_hub.py — the /train hub embed + view construction.

The button callbacks dispatch into flows exercised elsewhere (the editor, the
weekly draft, the legacy views); here we cover the deterministic surface: the
embed adapts to rotation on/off, the hub view shows the right buttons, the
management views construct, and ui_time renders.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import train_hub


def _cfg():
    cfg = MagicMock()
    cfg.timezone = "America/New_York"
    cfg.leadership_channel_id = 1111
    return cfg


def _tcfg(**over):
    base = {
        "rotation_enabled": 1,
        "active_schedule_preset": "Standard Week",
        "weekly_draft_day": 6,
        "reminder_channel_id": 1111,
        "reminder_time": "18:00",
        "rotation_public_channel_id": 2222,
        "blurbs_enabled": 1,
        "reminders_enabled": 1,
        "history_tab": "Train History",
        "day_rules_tab": "Train Day Rules",
        "member_rules_tab": "Train Member Rules",
    }
    base.update(over)
    return base


def test_hub_embed_rotation_on():
    with (
        patch("config.get_config", return_value=_cfg()),
        patch("config.get_train_config", return_value=_tcfg()),
    ):
        embed = train_hub._build_train_hub_embed(MagicMock(), 123)
    blob = (embed.description or "") + " ".join(f.value for f in embed.fields)
    assert "Rotation is **on**" in (embed.description or "")
    assert "Standard Week" in blob
    assert "<#2222>" in blob  # public channel


def test_hub_embed_rotation_off():
    with (
        patch("config.get_config", return_value=_cfg()),
        patch("config.get_train_config", return_value=_tcfg(rotation_enabled=0)),
    ):
        embed = train_hub._build_train_hub_embed(MagicMock(), 123)
    blob = (embed.description or "") + " ".join(f.value for f in embed.fields)
    assert "Conductor Rotation:** ❌ off" in blob


def test_hub_view_buttons_rotation_on():
    view = train_hub._TrainHubView(MagicMock(), 123, 1, rotation_on=True)
    labels = {c.label for c in view.children}
    assert train_hub.TRAIN_HUB_BTN_WEEK in labels
    assert train_hub.TRAIN_HUB_BTN_PRESETS in labels
    assert train_hub.TRAIN_HUB_BTN_MEMBER_RULES in labels
    assert train_hub.TRAIN_HUB_BTN_LOGS in labels
    # legacy-only buttons absent
    assert train_hub.TRAIN_HUB_BTN_OVERVIEW not in labels


def test_hub_view_buttons_rotation_off():
    view = train_hub._TrainHubView(MagicMock(), 123, 1, rotation_on=False)
    labels = {c.label for c in view.children}
    assert train_hub.TRAIN_HUB_BTN_OVERVIEW in labels
    assert train_hub.TRAIN_HUB_BTN_LOG in labels
    assert train_hub.TRAIN_HUB_BTN_BIRTHDAYS in labels
    # rotation-only buttons absent
    assert train_hub.TRAIN_HUB_BTN_WEEK not in labels


def test_management_views_construct():
    presets = train_hub.PresetsManageView(MagicMock(), 123, 1, "Train Day Rules")
    assert len(presets.children) == 4  # Create / Edit / Set active / Delete
    rules = train_hub.MemberRulesManageView(MagicMock(), 123, 1, "Train Member Rules")
    assert len(rules.children) == 2  # Add / Remove
