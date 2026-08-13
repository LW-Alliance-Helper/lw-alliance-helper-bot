"""Unit tests for the Alliance Duel (VS) member day-theme reminder (#406).

The one free surface in a Premium feature, and the only member-facing one, so
what is worth testing is mostly about the boundaries that makes:

- **It never prints an award value.** The in-game board renders each player
  their own Tech-boosted figures, so no shared number is true for two members
  of the same alliance. Order survives; values do not. This is the rule most
  likely to be broken later by someone adding a helpful-looking number.
- **It does not need the tracker.** Requiring `enabled` would gate a free
  surface behind a Premium one.
- **It names today, not yesterday.** The opposite of the score prompt, which
  shares its loop, so the two are easy to mix up.
- **It never generalises one day's board to another.** Radar Tasks score on
  three days; the themes name a flavour, not an exclusive action set.
"""

import datetime as _dt
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import alliance_duel as ad
import alliance_duel_cog as ad_cog
import alliance_duel_setup as ad_setup
import alliance_duel_views as ad_views
import alliance_duel_wizard as ad_wizard
import outage_catchup


GUILD_ID = 4242
CHANNEL_ID = 555
ET = ZoneInfo("America/New_York")


def _vs_cfg(**over):
    base = {
        "guild_id": GUILD_ID,
        "enabled": 0,  # the reminder works without the tracker set up
        "day_theme_enabled": 1,
        "day_theme_time": "08:00",
        "day_theme_channel_id": CHANNEL_ID,
        "day_theme_note": "",
        "last_day_theme_fired": "",
        "score_prompt_enabled": 0,
        "score_prompt_time": "",
        "score_prompt_channel_id": 0,
        "last_score_prompt_fired": "",
    }
    base.update(over)
    return base


def _text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{f.name}\n{f.value}" for f in embed.fields]
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


def _guild():
    guild = MagicMock()
    guild.id = GUILD_ID
    return guild


class _Channel:
    def __init__(self):
        self.id = CHANNEL_ID
        self.name = "alliance-chat"
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        message = MagicMock()
        message.id = 4321
        return message


# ── What the post says ────────────────────────────────────────────────────────


def test_every_day_has_a_board_and_names_its_theme():
    for day, duel_day in ad.DUEL_DAY_BY_NUMBER.items():
        text = _text(ad_views.day_theme_embed(day))
        assert duel_day.theme in text
        assert ad.DAY_ACTIONS[day], f"day {day} has no actions"


def test_no_award_value_is_ever_printed():
    """The board shows each player their own Tech-boosted figures, so there is
    no shared number that is true for two members of the same alliance. This is
    the rule a later change is most likely to break by adding a helpful number.
    """
    import re

    for day in ad.DUEL_DAY_BY_NUMBER:
        text = _text(ad_views.day_theme_embed(day, note=""))
        # The day's league point value (1/2/2/2/2/4 of 13) is a real constant
        # and is allowed. Nothing else numeric belongs here.
        allowed = {str(ad.DUEL_DAY_BY_NUMBER[day].points), str(ad.WEEK_POINTS_TOTAL), str(day)}
        found = set(re.findall(r"\d[\d,\.]*", text))
        assert found <= allowed, f"day {day} prints {found - allowed}"


def test_the_league_point_value_is_shown_because_it_is_a_real_constant():
    text = _text(ad_views.day_theme_embed(6))
    assert "**4**" in text
    assert "13" in text


def test_the_actions_are_ordered_biggest_first():
    """Order is the one thing that survives the Tech multiplier, since every
    category caps at the same boost, so leading with the dominant action is
    safe where pricing it is not."""
    assert ad.DAY_ACTIONS[2][0] == "UR Trade Trucks"
    assert ad.DAY_ACTIONS[3][0].startswith("Drone Component Chests")
    assert ad.DAY_ACTIONS[6][0].startswith("killing rival alliance units")


def test_no_days_board_is_generalised_from_another():
    """Radar Tasks score on days 1, 3 and 5, and day 6 carries day 2's two
    big-ticket actions. The themes name a flavour, not an exclusive set."""
    radar_days = {d for d, actions in ad.DAY_ACTIONS.items() if "Radar Tasks" in actions}
    assert radar_days == {1, 3, 5}
    assert "UR Trade Trucks" in ad.DAY_ACTIONS[6]
    assert ad.DAY_ACTIONS[3] != ad.DAY_ACTIONS[6]


def test_enemy_buster_says_that_losing_units_scores_too():
    assert "lose" in _text(ad_views.day_theme_embed(6))
    assert "lose" not in _text(ad_views.day_theme_embed(3))


def test_diamond_purchases_are_named_once_rather_than_per_day():
    text = _text(ad_views.day_theme_embed(1))
    assert ad.SCORES_EVERY_DAY in text


def test_the_leadership_note_is_carried_verbatim():
    """Their words to their own members, in whatever language they write in."""
    note = "Attendez le signal avant de tout dépenser."
    assert note in _text(ad_views.day_theme_embed(2, note=note))


def test_an_empty_note_adds_no_empty_field():
    embed = ad_views.day_theme_embed(2, note="   ")
    assert not any(f.name == "From your leadership" for f in embed.fields)


def test_a_long_note_is_clamped_to_the_field_limit():
    embed = ad_views.day_theme_embed(2, note="x" * 2000)
    note_field = next(f for f in embed.fields if f.name == "From your leadership")
    assert len(note_field.value) <= 1024


def test_the_post_carries_no_em_dashes():
    for day in ad.DUEL_DAY_BY_NUMBER:
        assert "—" not in _text(ad_views.day_theme_embed(day))


# ── When it fires ─────────────────────────────────────────────────────────────


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    cog = ad_cog.AllianceDuelCog.__new__(ad_cog.AllianceDuelCog)
    cog.bot = bot
    return cog


async def test_it_names_today_where_the_score_prompt_names_yesterday():
    """Both surfaces share one loop and resolve different days on purpose: this
    one is a call to action for the day that is running."""
    cog = _make_cog()
    tuesday_8am = _dt.datetime(2026, 8, 11, 8, 0, tzinfo=ET)
    with (
        patch("alliance_duel_cog.post_day_theme", new=AsyncMock(return_value=True)) as posted,
        patch("config.save_vs_config", MagicMock()),
    ):
        await cog._maybe_post_day_theme(_guild(), _vs_cfg(), tuesday_8am)
    assert posted.await_args.args[3] == 2  # Tuesday is day 2, Base Expansion


async def test_sunday_posts_nothing():
    cog = _make_cog()
    with (
        patch("alliance_duel_cog.post_day_theme", new=AsyncMock()) as posted,
        patch("config.save_vs_config", MagicMock()),
    ):
        await cog._maybe_post_day_theme(
            _guild(), _vs_cfg(), _dt.datetime(2026, 8, 16, 8, 0, tzinfo=ET)
        )
    posted.assert_not_awaited()


async def test_monday_posts_because_radar_training_is_a_scoring_day():
    """The opposite of the score prompt, which is silent on Monday. Same loop,
    different question."""
    cog = _make_cog()
    with (
        patch("alliance_duel_cog.post_day_theme", new=AsyncMock(return_value=True)) as posted,
        patch("config.save_vs_config", MagicMock()),
    ):
        await cog._maybe_post_day_theme(
            _guild(), _vs_cfg(), _dt.datetime(2026, 8, 10, 8, 0, tzinfo=ET)
        )
    assert posted.await_args.args[3] == 1


async def test_it_fires_once_a_day():
    cog = _make_cog()
    with patch("alliance_duel_cog.post_day_theme", new=AsyncMock()) as posted:
        await cog._maybe_post_day_theme(
            _guild(),
            _vs_cfg(last_day_theme_fired="2026-08-11"),
            _dt.datetime(2026, 8, 11, 8, 0, tzinfo=ET),
        )
    posted.assert_not_awaited()


async def test_a_guild_that_did_not_opt_in_is_never_posted_to():
    cog = _make_cog()
    with patch("alliance_duel_cog.post_day_theme", new=AsyncMock()) as posted:
        await cog._maybe_post_day_theme(
            _guild(), _vs_cfg(day_theme_enabled=0), _dt.datetime(2026, 8, 11, 8, 0, tzinfo=ET)
        )
    posted.assert_not_awaited()


async def test_the_reminder_runs_without_the_tracker_set_up():
    """It reads nothing from the sheet, so requiring the Premium tracker to be
    configured would gate a free surface behind a paid one."""
    cog = _make_cog()
    cfg = MagicMock()
    cfg.setup_complete = True
    cfg.timezone = "America/New_York"
    cog.bot.guilds = [_guild()]
    with (
        patch("config.get_config", MagicMock(return_value=cfg)),
        patch("config.get_vs_config", MagicMock(return_value=_vs_cfg(enabled=0))),
        patch("config.stamp_loop_heartbeat", MagicMock()),
        patch.object(ad_cog.AllianceDuelCog, "_maybe_post_day_theme", new=AsyncMock()) as day_theme,
        patch.object(ad_cog.AllianceDuelCog, "_maybe_post_score_prompt", new=AsyncMock()) as prompt,
    ):
        await ad_cog.AllianceDuelCog.check_vs_posts(cog)
    day_theme.assert_awaited_once()
    prompt.assert_not_awaited()


async def test_no_premium_check_stands_between_a_guild_and_this_post():
    channel = _Channel()
    with (
        patch("config_health.resolve_configured_channel", MagicMock(return_value=channel)),
        patch("premium.feature_gate", new=AsyncMock(return_value=False)),
    ):
        posted = await ad_cog.post_day_theme(MagicMock(), _guild(), _vs_cfg(), 2)
    assert posted is True
    assert channel.sent


async def test_a_broken_channel_is_a_skip_rather_than_a_crash():
    with patch("config_health.resolve_configured_channel", MagicMock(return_value=None)):
        posted = await ad_cog.post_day_theme(MagicMock(), _guild(), _vs_cfg(), 2)
    assert posted is False


# ── Outage catch-up ───────────────────────────────────────────────────────────


def _cfg_obj():
    cfg = MagicMock()
    cfg.timezone = "America/New_York"
    return cfg


def _window(start, end):
    return outage_catchup.OutageWindow(start=start, end=end)


async def test_a_reminder_missed_while_the_day_is_still_running_is_recovered():
    # Down 7:30am to 10am on Tuesday, over an 8am reminder.
    window = _window(
        _dt.datetime(2026, 8, 11, 11, 30, tzinfo=_dt.timezone.utc),
        _dt.datetime(2026, 8, 11, 14, 0, tzinfo=_dt.timezone.utc),
    )
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=_Channel())
    with patch("config.get_vs_config", MagicMock(return_value=_vs_cfg())):
        items = await outage_catchup.scan_vs_day_theme(bot, _guild(), _cfg_obj(), window)
    assert len(items) == 1
    assert "Base Expansion" in items[0].title


async def test_a_multi_day_outage_recovers_todays_reminder_not_a_stale_one():
    """Down from Tuesday to Thursday, the reminder worth posting is Thursday's.
    Posting "today is Base Expansion" on Thursday would be a wrong reminder
    rather than a late one, and the catch-up framework's own helpers already
    make that impossible: the slot is resolved against the day the bot came
    back."""
    window = _window(
        _dt.datetime(2026, 8, 11, 11, 30, tzinfo=_dt.timezone.utc),
        _dt.datetime(2026, 8, 13, 14, 0, tzinfo=_dt.timezone.utc),
    )
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=_Channel())
    with patch("config.get_vs_config", MagicMock(return_value=_vs_cfg())):
        items = await outage_catchup.scan_vs_day_theme(bot, _guild(), _cfg_obj(), window)
    assert len(items) == 1
    assert "Train Heroes" in items[0].title  # Thursday, the day it came back
    assert "Base Expansion" not in items[0].title


async def test_a_reminder_still_pending_today_is_not_recovered_early():
    """Back online at 6am with an 8am reminder still to come: the live loop
    will post it, so catch-up must not."""
    window = _window(
        _dt.datetime(2026, 8, 11, 5, 0, tzinfo=_dt.timezone.utc),
        _dt.datetime(2026, 8, 11, 10, 0, tzinfo=_dt.timezone.utc),  # 6am ET
    )
    with patch("config.get_vs_config", MagicMock(return_value=_vs_cfg())):
        items = await outage_catchup.scan_vs_day_theme(MagicMock(), _guild(), _cfg_obj(), window)
    assert items == []


# ── Settings ──────────────────────────────────────────────────────────────────


def test_the_panel_explains_the_monday_to_saturday_schedule():
    text = _text(ad_setup.scheduled_post_embed(_vs_cfg(), "day_theme"))
    assert "Monday through Saturday" in text
    assert "free" in text


def test_the_members_panel_does_not_borrow_the_notification_bell():
    """DESIGN.md: an auto-post to the whole alliance is a post, not a
    notification, so 🔕 would be the wrong glyph for switching it off."""
    text = _text(ad_setup.scheduled_post_embed(_vs_cfg(), "day_theme"))
    assert "🔕" not in text
    assert "🔔" not in text
    assert ad_wizard.DAY_THEME_SURFACE.off_label == "Turn it off"


def test_the_score_prompt_panel_keeps_the_bell():
    text = _text(ad_setup.scheduled_post_embed(_vs_cfg(score_prompt_enabled=1), "score_prompt"))
    assert "🔔" in text


def test_the_note_button_appears_only_on_the_surface_that_has_one(monkeypatch):
    monkeypatch.setattr("config.get_vs_config", lambda _gid: _vs_cfg())
    with_note = ad_wizard.ScheduledPostSettingsView(GUILD_ID, 1, ad_wizard.DAY_THEME_SURFACE)
    without = ad_wizard.ScheduledPostSettingsView(GUILD_ID, 1, ad_wizard.SCORE_PROMPT_SURFACE)
    labels = {c.label for c in with_note.children}
    assert ad_wizard.VS_BTN_PROMPT_NOTE in labels
    assert ad_wizard.VS_BTN_PROMPT_NOTE not in {c.label for c in without.children}


def test_a_saved_note_is_shown_back_on_the_panel(monkeypatch):
    text = _text(
        ad_setup.scheduled_post_embed(_vs_cfg(day_theme_note="Hold until 8pm"), "day_theme")
    )
    assert "Hold until 8pm" in text


@pytest.mark.parametrize("surface_key", ["score_prompt", "day_theme"])
def test_both_panels_carry_no_em_dashes(surface_key):
    assert "—" not in _text(ad_setup.scheduled_post_embed(_vs_cfg(), surface_key))
