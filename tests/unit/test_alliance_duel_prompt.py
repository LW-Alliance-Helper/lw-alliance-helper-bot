"""Unit tests for the Alliance Duel (VS) daily score prompt (#405).

The prompt is the first VS surface that posts on its own, so most of what is
worth testing is not the copy but the conditions under which it fires:

- **Which day it asks about.** It asks for the day that has just finished, not
  the one running, and that has to hold whether the alliance picked 9am or
  11pm. The same expression produces the Tuesday-through-Sunday schedule, so a
  Monday test is a schedule test rather than a special case.
- **That it fires once.** Dedup is DB-backed on the server date, per the #89
  bug class: an in-memory set is wiped by every Railway redeploy.
- **That it stays quiet when there is nothing to ask.** No live week, no
  alliance identity, a score already typed into the sheet, or a channel the
  bot cannot post in are all silence, never a post.
- **That its buttons survive a restart**, which means holding no state beyond
  what a custom_id and one DB row can carry.
"""

import datetime as _dt
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import alliance_duel as ad
import alliance_duel_cog as ad_cog
import alliance_duel_hub as hub
import alliance_duel_setup as ad_setup
import alliance_duel_views as ad_views
import alliance_duel_wizard as ad_wizard
import config_health
import outage_catchup


GUILD_ID = 4242
CHANNEL_ID = 777
LEAGUE = ad.LeagueKey("S35", "Diamond", "12 - 2")
NEXT_LEAGUE = ad.LeagueKey("S36", "Diamond", "12 - 2")
# Anchored to the current duel week, not pinned: the code under test resolves
# the live week against the real clock, so an absolute Monday quietly stops
# being live — this one did, on Monday 2026-08-17. Server time rather than
# `date.today()`: the two disagree for a couple of hours around every UTC-2
# rollover, and on the Sunday/Monday one that disagreement is a whole week,
# because `week_monday` sends Sunday back rather than forward.
MONDAY = ad.week_monday(ad.server_today())  # week 1 of the league under test
OWN_TAG, OWN_WZ = "US", "1234"
OWN = ad.AllianceKey.of(OWN_TAG, OWN_WZ)
ET = ZoneInfo("America/New_York")


def _key(tag: str) -> ad.AllianceKey:
    return ad.AllianceKey.of(tag, OWN_WZ)


def _row(tag, week=1, seed=None, league=LEAGUE, week_date=MONDAY, **kw):
    return ad.AllianceWeek(
        league=league,
        week=week,
        alliance=_key(tag),
        seed=seed,
        week_date=week_date,
        tag_display=tag,
        **kw,
    )


def _rows(**own_fields):
    """Own alliance plus the opponent it is recorded as facing in week 1."""
    return [
        _row(OWN_TAG, seed=1, opponent=_key("A02"), **own_fields),
        _row("A02", seed=2, opponent=OWN),
    ]


def _vs_cfg(**over):
    base = {
        "guild_id": GUILD_ID,
        "enabled": 1,
        "tab_name": "Alliance Duel (VS)",
        "own_tag": OWN_TAG,
        "own_warzone": OWN_WZ,
        "tracking_mode": ad.MODE_FULL_BRACKET,
        "score_prompt_enabled": 1,
        "score_prompt_time": "09:00",
        "score_prompt_channel_id": CHANNEL_ID,
        "last_score_prompt_fired": "",
    }
    base.update(over)
    return base


def _state(rows=None, **cfg_over):
    return hub.HubState(GUILD_ID, _vs_cfg(**cfg_over), rows if rows is not None else _rows())


def _text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{f.name}\n{f.value}" for f in embed.fields]
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


@pytest.fixture(autouse=True)
def _no_config_health_db(monkeypatch):
    monkeypatch.setattr(config_health, "problems_for_subjects", lambda *a, **k: [])


# ── Which day the prompt asks about ───────────────────────────────────────────


def test_the_prompt_asks_about_the_day_that_just_ended_not_the_one_running():
    # 9am Tuesday local: server time has not rolled over, so Tuesday is still
    # running and the last finished day is Monday's Radar Training.
    day_date, day = ad.completed_duel_day(_dt.datetime(2026, 8, 11, 9, 0, tzinfo=ET))
    assert day == 1
    assert day_date == _dt.date(2026, 8, 10)


def test_a_late_evening_prompt_asks_about_the_day_that_ended_hours_before():
    # 11pm Tuesday local is already Wednesday on server time (UTC-2), so the
    # day that just closed is Tuesday's Base Expansion. A guild-local weekday
    # check would file this against Monday.
    day_date, day = ad.completed_duel_day(_dt.datetime(2026, 8, 11, 23, 0, tzinfo=ET))
    assert day == 2
    assert day_date == _dt.date(2026, 8, 11)


def test_monday_asks_nothing_because_sunday_is_the_rest_day():
    assert ad.completed_duel_day(_dt.datetime(2026, 8, 10, 9, 0, tzinfo=ET)) is None


def test_sundays_prompt_covers_saturdays_enemy_buster():
    day_date, day = ad.completed_duel_day(_dt.datetime(2026, 8, 16, 9, 0, tzinfo=ET))
    assert day == 6
    assert day_date == _dt.date(2026, 8, 15)


def test_the_schedule_is_tuesday_through_sunday_and_nothing_else():
    """The six posting days fall out of the day resolution, not a table."""
    asked = {}
    for offset in range(7):
        when = _dt.datetime(2026, 8, 10, 9, 0, tzinfo=ET) + _dt.timedelta(days=offset)
        target = ad.completed_duel_day(when)
        asked[when.strftime("%A")] = target[1] if target else None
    assert asked == {
        "Monday": None,
        "Tuesday": 1,
        "Wednesday": 2,
        "Thursday": 3,
        "Friday": 4,
        "Saturday": 5,
        "Sunday": 6,
    }


def test_sundays_prompt_still_resolves_to_the_week_that_is_ending():
    """Saturday's date belongs to the week that started six days earlier, so a
    Sunday prompt logs against that week rather than opening the next one."""
    _, day = ad.completed_duel_day(_dt.datetime(2026, 8, 16, 9, 0, tzinfo=ET))
    live = ad.resolve_live_week(_rows(), today=MONDAY + _dt.timedelta(days=5))
    assert (live.week, day) == (1, 6)


# ── The persistent view ───────────────────────────────────────────────────────


def test_custom_id_round_trips():
    parsed = ad_views.parse_custom_id(ad_views.make_custom_id(GUILD_ID, 3, 4))
    assert parsed == {"guild_id": GUILD_ID, "week": 3, "day": 4}


@pytest.mark.parametrize("bad", ["", "nonsense", "vsprompt:1:2", "vsprompt:a:b:c", "signup:1:2:3"])
def test_a_malformed_custom_id_is_a_no_op_not_a_crash(bad):
    assert ad_views.parse_custom_id(bad) is None


def test_the_view_is_persistent_and_holds_no_state_beyond_its_custom_id():
    view = ad_views.ScorePromptView(GUILD_ID, 1, 2)
    assert view.timeout is None
    assert all(item.custom_id for item in view.children)


def test_the_button_names_the_day_it_is_asking_about():
    """ "Log today's score" would be wrong here: by the time an officer reads
    the prompt, the day it asks about is yesterday."""
    view = ad_views.ScorePromptView(GUILD_ID, 1, 3)
    label = view.children[0].label
    assert "Age of Science" in label
    assert "today" not in label.lower()
    assert len(label) <= 80


def test_every_duel_days_button_label_fits_discords_cap():
    for day in ad.DUEL_DAY_BY_NUMBER:
        view = ad_views.ScorePromptView(GUILD_ID, 1, day)
        assert len(view.children[0].label) <= 80


# ── The posted embed ──────────────────────────────────────────────────────────


def test_the_prompt_names_the_opponent_and_what_the_day_was_worth():
    text = _text(ad_views.score_prompt_embed(_state(), 1, 2, _key("A02")))
    assert "A02" in text
    assert "**2** points" in text


def test_a_one_point_day_is_not_pluralised():
    text = _text(ad_views.score_prompt_embed(_state(), 1, 1, _key("A02")))
    assert "**1** point." in text


def test_an_unknown_opponent_still_asks_rather_than_failing():
    rows = [_row(OWN_TAG, seed=1)]
    text = _text(ad_views.score_prompt_embed(_state(rows), 1, 2, None))
    assert "your opponent" in text


def test_the_prompt_shows_where_the_week_stood_before_this_day():
    rows = _rows(day_outcomes={1: "W", 2: "W"})
    text = _text(ad_views.score_prompt_embed(_state(rows), 1, 3, _key("A02")))
    assert "3-0" in text


def test_the_prompt_carries_no_em_dashes():
    assert "—" not in _text(ad_views.score_prompt_embed(_state(), 1, 4, _key("A02")))


# ── The loop ──────────────────────────────────────────────────────────────────


def _make_cog():
    bot = MagicMock()
    bot.guilds = []
    cog = ad_cog.AllianceDuelCog.__new__(ad_cog.AllianceDuelCog)
    cog.bot = bot
    return cog


def _guild():
    guild = MagicMock()
    guild.id = GUILD_ID
    return guild


async def test_the_loop_posts_at_the_configured_minute():
    cog = _make_cog()
    with (
        patch("alliance_duel_cog.post_score_prompt", new=AsyncMock(return_value=True)) as posted,
        patch("config.save_vs_config", MagicMock()),
    ):
        await cog._maybe_post_score_prompt(
            _guild(), _vs_cfg(), _dt.datetime(2026, 8, 11, 9, 0, tzinfo=ET)
        )
    assert posted.await_count == 1
    assert posted.await_args.args[3:] == (_dt.date(2026, 8, 10), 1)


async def test_the_loop_stays_quiet_at_every_other_minute():
    cog = _make_cog()
    with patch("alliance_duel_cog.post_score_prompt", new=AsyncMock()) as posted:
        await cog._maybe_post_score_prompt(
            _guild(), _vs_cfg(), _dt.datetime(2026, 8, 11, 9, 1, tzinfo=ET)
        )
    posted.assert_not_awaited()


async def test_an_unset_time_never_posts_at_some_hour_nobody_chose():
    """Unlike the train reminder there is no fallback hour: the bot does not
    pick a posting time on the alliance's behalf."""
    cog = _make_cog()
    with patch("alliance_duel_cog.post_score_prompt", new=AsyncMock()) as posted:
        for hour in range(24):
            await cog._maybe_post_score_prompt(
                _guild(),
                _vs_cfg(score_prompt_time=""),
                _dt.datetime(2026, 8, 11, hour, 0, tzinfo=ET),
            )
    posted.assert_not_awaited()


async def test_a_prompt_that_already_fired_today_does_not_fire_again():
    """DB-backed dedup, so a redeploy at the trigger minute cannot double-post
    the way an in-memory set did in #89."""
    cog = _make_cog()
    cfg = _vs_cfg(last_score_prompt_fired="2026-08-10")
    with patch("alliance_duel_cog.post_score_prompt", new=AsyncMock()) as posted:
        await cog._maybe_post_score_prompt(
            _guild(), cfg, _dt.datetime(2026, 8, 11, 9, 0, tzinfo=ET)
        )
    posted.assert_not_awaited()


async def test_the_day_is_marked_fired_before_the_post_is_attempted():
    """One attempt per duel day whatever happens. A sheet that is failing must
    not put the loop into a per-minute retry for the rest of the hour."""
    cog = _make_cog()
    with (
        patch("alliance_duel_cog.post_score_prompt", new=AsyncMock(return_value=False)),
        patch("config.save_vs_config", MagicMock()) as saved,
    ):
        await cog._maybe_post_score_prompt(
            _guild(), _vs_cfg(), _dt.datetime(2026, 8, 11, 9, 0, tzinfo=ET)
        )
    assert saved.call_args.kwargs == {"last_score_prompt_fired": "2026-08-10"}


async def test_monday_is_not_marked_fired_and_asks_nothing():
    cog = _make_cog()
    with (
        patch("alliance_duel_cog.post_score_prompt", new=AsyncMock()) as posted,
        patch("config.save_vs_config", MagicMock()) as saved,
    ):
        await cog._maybe_post_score_prompt(
            _guild(), _vs_cfg(), _dt.datetime(2026, 8, 10, 9, 0, tzinfo=ET)
        )
    posted.assert_not_awaited()
    saved.assert_not_called()


async def test_a_guild_that_did_not_opt_in_is_never_posted_to():
    cog = _make_cog()
    with patch("alliance_duel_cog.post_score_prompt", new=AsyncMock()) as posted:
        await cog._maybe_post_score_prompt(
            _guild(),
            _vs_cfg(score_prompt_enabled=0),
            _dt.datetime(2026, 8, 11, 9, 0, tzinfo=ET),
        )
    posted.assert_not_awaited()


# ── The post itself ───────────────────────────────────────────────────────────


class _Channel:
    def __init__(self, channel_id=CHANNEL_ID):
        self.id = channel_id
        self.name = "vs-tracking"
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        message = MagicMock()
        message.id = 90210
        return message


def _post_patches(rows, channel, *, premium_ok=True):
    """Everything `post_score_prompt` reaches outside its own module."""
    return (
        patch("premium.feature_gate", new=AsyncMock(return_value=premium_ok)),
        patch("alliance_duel_hub.read_tab_once", new=AsyncMock(return_value=rows)),
        patch("config_health.resolve_configured_channel", MagicMock(return_value=channel)),
        patch("config.record_vs_score_prompt_post", MagicMock()),
    )


async def _post(rows, channel, day=1, day_date=MONDAY, **cfg_over):
    patches = _post_patches(rows, channel, premium_ok=cfg_over.pop("premium_ok", True))
    with patches[0], patches[1], patches[2], patches[3] as recorded:
        posted = await ad_cog.post_score_prompt(
            MagicMock(), _guild(), _vs_cfg(**cfg_over), day_date, day
        )
    return posted, recorded


async def test_a_normal_day_posts_the_prompt_with_its_view():
    channel = _Channel()
    posted, recorded = await _post(_rows(), channel)
    assert posted is True
    assert isinstance(channel.sent[0]["view"], ad_views.ScorePromptView)
    assert recorded.call_count == 1


async def test_a_day_already_recorded_is_not_asked_for_again():
    """Someone who typed the score into the spreadsheet at breakfast should not
    be asked for it an hour later."""
    channel = _Channel()
    posted, _ = await _post(_rows(day_scores={1: 500_000}), channel)
    assert posted is False
    assert channel.sent == []


async def test_no_live_week_means_no_post():
    """Between leagues there is nothing to ask about, so the prompt does not
    invent a week."""
    channel = _Channel()
    stale = [_row(OWN_TAG, seed=1, week_date=_dt.date(2026, 3, 2))]
    posted, _ = await _post(stale, channel)
    assert posted is False


async def test_an_unidentified_alliance_means_no_post():
    channel = _Channel()
    posted, _ = await _post(_rows(), channel, own_tag="", own_warzone="")
    assert posted is False


async def test_a_lapsed_premium_guild_is_not_posted_to():
    channel = _Channel()
    posted, _ = await _post(_rows(), channel, premium_ok=False)
    assert posted is False
    assert channel.sent == []


async def test_a_broken_channel_is_a_silent_skip_here_and_a_notice_elsewhere():
    """`resolve_configured_channel` records the problem through config_health,
    so the prompt does not need to say anything at post time."""
    posted, _ = await _post(_rows(), None)
    assert posted is False


async def test_the_post_is_recorded_so_its_buttons_survive_a_restart():
    channel = _Channel()
    day_2 = MONDAY + _dt.timedelta(days=1)
    _, recorded = await _post(_rows(), channel, day=2, day_date=day_2)
    args = recorded.call_args.args
    assert args[0] == GUILD_ID
    assert args[2] == 90210  # message id
    assert args[3] == LEAGUE  # the league, so a later click cannot cross into another
    assert args[4:] == (1, 2, day_2.isoformat())


# ── Stale prompts ─────────────────────────────────────────────────────────────


def _stale_check(post_row, state):
    view = ad_views.ScorePromptView(GUILD_ID, 1, 2)
    interaction = MagicMock()
    interaction.message.id = 90210
    with patch("config.get_vs_score_prompt_post", MagicMock(return_value=post_row)):
        return view._stale_league(interaction, state)


def _post_row(league):
    return {
        "league_season": league.season,
        "league_tier": league.tier,
        "league_group": league.group,
        "week": 1,
        "duel_day": 2,
    }


def test_a_prompt_from_a_finished_league_refuses_rather_than_writing():
    rows = [_row(OWN_TAG, seed=1, league=NEXT_LEAGUE, week_date=MONDAY)]
    refusal = _stale_check(_post_row(LEAGUE), _state(rows))
    assert refusal is not None
    assert "S35" in refusal
    assert "/vs" in refusal


def test_a_prompt_from_the_current_league_is_allowed_through():
    assert _stale_check(_post_row(LEAGUE), _state()) is None


def test_a_prompt_the_bot_has_no_record_of_is_allowed_through():
    """A missing row means the table aged out or was lost, which is far likelier
    than a league turning over inside the fortnight it keeps."""
    assert _stale_check(None, _state()) is None


# ── Outage catch-up ───────────────────────────────────────────────────────────


def _window(start, end):
    return outage_catchup.OutageWindow(start=start, end=end)


def _cfg_obj():
    cfg = MagicMock()
    cfg.timezone = "America/New_York"
    return cfg


async def test_the_loop_stamps_a_heartbeat_outage_catchup_watches():
    assert ad_cog.VS_POSTS_HEARTBEAT in outage_catchup.HEARTBEAT_LOOPS


async def test_a_prompt_missed_during_an_outage_is_offered_for_catch_up():
    # Down from 8:30am to 10am Tuesday, over a 9am prompt.
    window = _window(
        _dt.datetime(2026, 8, 11, 12, 30, tzinfo=_dt.timezone.utc),
        _dt.datetime(2026, 8, 11, 14, 0, tzinfo=_dt.timezone.utc),
    )
    bot = MagicMock()
    bot.get_channel = MagicMock(return_value=_Channel())
    with patch("config.get_vs_config", MagicMock(return_value=_vs_cfg())):
        items = await outage_catchup.scan_vs_score_prompt(bot, _guild(), _cfg_obj(), window)
    assert len(items) == 1
    assert "day 1" in items[0].title
    assert items[0].surface == "vs_score_prompt"


async def test_a_prompt_that_went_out_before_the_outage_is_not_re_offered():
    window = _window(
        _dt.datetime(2026, 8, 11, 12, 30, tzinfo=_dt.timezone.utc),
        _dt.datetime(2026, 8, 11, 14, 0, tzinfo=_dt.timezone.utc),
    )
    bot = MagicMock()
    with patch(
        "config.get_vs_config",
        MagicMock(return_value=_vs_cfg(last_score_prompt_fired="2026-08-10")),
    ):
        items = await outage_catchup.scan_vs_score_prompt(bot, _guild(), _cfg_obj(), window)
    assert items == []


async def test_catch_up_offers_nothing_to_a_guild_that_opted_out():
    window = _window(
        _dt.datetime(2026, 8, 11, 12, 30, tzinfo=_dt.timezone.utc),
        _dt.datetime(2026, 8, 11, 14, 0, tzinfo=_dt.timezone.utc),
    )
    with patch("config.get_vs_config", MagicMock(return_value=_vs_cfg(score_prompt_enabled=0))):
        items = await outage_catchup.scan_vs_score_prompt(MagicMock(), _guild(), _cfg_obj(), window)
    assert items == []


# ── Settings panel ────────────────────────────────────────────────────────────


def _panel_text(**cfg_over):
    return _text(ad_setup.scheduled_post_embed(_vs_cfg(**cfg_over), "score_prompt"))


def test_the_settings_panel_says_what_is_saved_before_what_it_does():
    text = _panel_text()
    assert "9:00am" in text
    assert f"<#{CHANNEL_ID}>" in text


def test_turning_it_off_says_the_channel_and_time_are_kept():
    assert "still saved" in _panel_text(score_prompt_enabled=0)


def test_an_unconfigured_panel_names_what_is_missing():
    text = _panel_text(score_prompt_enabled=0, score_prompt_time="", score_prompt_channel_id=0)
    assert "a time and a channel" in text


def test_the_panel_explains_the_schedule_and_whose_clock_is_whose():
    text = _panel_text()
    assert "Tuesday through Sunday" in text
    assert "server time" in text


def test_the_panel_carries_no_em_dashes():
    assert "\u2014" not in _panel_text()


def _panel(monkeypatch, **cfg_over):
    monkeypatch.setattr("config.get_vs_config", lambda _gid: _vs_cfg(**cfg_over))
    return ad_wizard.ScheduledPostSettingsView(GUILD_ID, 1, ad_wizard.SCORE_PROMPT_SURFACE)


def test_the_toggle_cannot_switch_on_a_post_that_could_never_fire(monkeypatch):
    view = _panel(
        monkeypatch,
        score_prompt_enabled=0,
        score_prompt_time="",
        score_prompt_channel_id=0,
    )
    toggle = next(c for c in view.children if c.label == ad_wizard.VS_BTN_PROMPT_ON)
    assert toggle.disabled is True


def test_the_toggle_is_live_once_both_halves_are_set(monkeypatch):
    view = _panel(monkeypatch, score_prompt_enabled=0)
    toggle = next(c for c in view.children if c.label == ad_wizard.VS_BTN_PROMPT_ON)
    assert toggle.disabled is False


def test_the_panel_has_at_most_one_primary_button(monkeypatch):
    view = _panel(monkeypatch)
    primaries = [c for c in view.children if c.style is discord.ButtonStyle.primary]
    assert len(primaries) <= 1
