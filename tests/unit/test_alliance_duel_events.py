"""Unit tests for the event-driven VS posts (#409).

These fire off a **write**, not a clock, and a write can happen twice: an
officer correcting a mistyped score re-saves the same day. So the properties
worth pinning are mostly about restraint.

- **Nothing posts twice**, and the dedup is durable rather than in-memory.
- **An announcement never breaks the save it followed.** The officer asked for
  a score to be recorded; a channel they deleted is not a reason to fail that.
- **Each of the three is its own opt-in**, so an alliance that wants the
  mid-week status and nothing else gets exactly that.
- **The reveal carries what it knows**, because naming an opponent without
  their record just sends someone to go and look, which is the work it was
  supposed to save.
"""

import datetime as _dt
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import alliance_duel as ad
import alliance_duel_events as events
import alliance_duel_hub as hub
import config_health


LEAGUE = ad.LeagueKey("S35", "Diamond", "12 - 2")
# Anchored to the current duel week, not pinned: the code under test resolves
# the live week against the real clock, so an absolute Monday quietly stops
# being live — this one did, on Monday 2026-08-17. Server time rather than
# `date.today()`: the two disagree for a couple of hours around every UTC-2
# rollover, and on the Sunday/Monday one that disagreement is a whole week,
# because `week_monday` sends Sunday back rather than forward.
MONDAY = ad.week_monday(ad.server_today())
OWN_TAG, OWN_WZ = "US", "1234"
OWN = ad.AllianceKey.of(OWN_TAG, OWN_WZ)
THEM = ad.AllianceKey.of("A02", OWN_WZ)


def _row(tag, week=1, **kw):
    return ad.AllianceWeek(
        league=LEAGUE,
        week=week,
        alliance=ad.AllianceKey.of(tag, OWN_WZ),
        week_date=MONDAY + _dt.timedelta(days=7 * (week - 1)),
        tag_display=tag,
        **kw,
    )


def _state(rows, **cfg_over):
    cfg = {
        "guild_id": 1,
        "enabled": 1,
        "tab_name": "Alliance Duel (VS)",
        "own_tag": OWN_TAG,
        "own_warzone": OWN_WZ,
        "tracking_mode": ad.MODE_FULL_BRACKET,
        "event_posts_channel_id": 4242,
        "clinch_status_enabled": 1,
        "opponent_reveal_enabled": 1,
        "season_recap_enabled": 1,
    }
    cfg.update(cfg_over)
    return hub.HubState(1, cfg, rows)


def _text(embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f"{f.name}\n{f.value}" for f in embed.fields]
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


class _Channel:
    def __init__(self):
        self.id = 4242
        self.name = "vs-leadership"
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return MagicMock()


@pytest.fixture(autouse=True)
def _no_config_health_db(monkeypatch):
    monkeypatch.setattr(config_health, "problems_for_subjects", lambda *a, **k: [])


@pytest.fixture
def posted(monkeypatch):
    """A fake channel plus a fake dedup store, so these run with no DB."""
    channel = _Channel()
    marked: set[tuple] = set()
    monkeypatch.setattr(
        config_health, "resolve_configured_channel", MagicMock(return_value=channel)
    )
    monkeypatch.setattr(
        "config.vs_event_already_posted",
        lambda gid, kind, key: (gid, kind, key) in marked,
    )
    monkeypatch.setattr(
        "config.mark_vs_event_posted", lambda gid, kind, key: marked.add((gid, kind, key))
    )
    channel.marked = marked
    return channel


# ── Live clinch status ────────────────────────────────────────────────────────


def test_the_clinch_post_says_what_would_settle_the_week():
    rows = [_row(OWN_TAG, opponent=THEM, day_outcomes={1: "W", 2: "W", 3: "W"}), _row("A02")]
    text = _text(events.clinch_embed(_state(rows), 1))
    assert "5-0" in text
    assert "clinches the week" in text


def test_a_clinched_week_says_so_plainly():
    outcomes = {1: "W", 2: "W", 3: "W", 4: "W"}
    rows = [_row(OWN_TAG, opponent=THEM, day_outcomes=outcomes), _row("A02")]
    text = _text(events.clinch_embed(_state(rows), 1))
    assert "is ours" in text


def test_a_lost_week_is_not_rendered_as_a_broken_thing():
    """Red means broken configuration in this product. A week going badly is
    still just the week's state."""
    import discord

    outcomes = {1: "L", 2: "L", 3: "L", 4: "L"}
    rows = [_row(OWN_TAG, opponent=THEM, day_outcomes=outcomes), _row("A02")]
    embed = events.clinch_embed(_state(rows), 1)
    assert embed.color != discord.Color.red()
    assert "has gone" in _text(embed)


def test_nothing_recorded_means_nothing_to_post():
    assert events.clinch_embed(_state([_row(OWN_TAG, opponent=THEM)]), 1) is None


async def test_the_clinch_post_fires_once_per_day(posted):
    rows = [_row(OWN_TAG, opponent=THEM, day_outcomes={1: "W"}), _row("A02")]
    state = _state(rows)
    assert await events.after_day_recorded(MagicMock(), state, 1, 1) is True
    assert await events.after_day_recorded(MagicMock(), state, 1, 1) is False
    assert len(posted.sent) == 1


async def test_a_later_day_posts_again(posted):
    rows = [_row(OWN_TAG, opponent=THEM, day_outcomes={1: "W", 2: "W"}), _row("A02")]
    state = _state(rows)
    await events.after_day_recorded(MagicMock(), state, 1, 1)
    await events.after_day_recorded(MagicMock(), state, 1, 2)
    assert len(posted.sent) == 2


async def test_an_alliance_that_did_not_opt_in_gets_nothing(posted):
    rows = [_row(OWN_TAG, opponent=THEM, day_outcomes={1: "W"}), _row("A02")]
    state = _state(rows, clinch_status_enabled=0)
    assert await events.after_day_recorded(MagicMock(), state, 1, 1) is False
    assert posted.sent == []


async def test_each_post_is_its_own_switch(posted):
    """Wanting the mid-week status and not the recap is a normal thing to
    want."""
    rows = [_row(OWN_TAG, opponent=THEM, day_outcomes={1: "W"}), _row("A02")]
    state = _state(rows, opponent_reveal_enabled=0, season_recap_enabled=0)
    assert await events.after_day_recorded(MagicMock(), state, 1, 1) is True
    assert await events.after_pairing_known(MagicMock(), state, 1) is False


# ── Opponent reveal ───────────────────────────────────────────────────────────


def test_the_reveal_carries_the_head_to_head_rather_than_just_a_name():
    rows = [
        _row(OWN_TAG, week=1, opponent=THEM, week_outcome="W"),
        _row("A02", week=1, opponent=OWN, week_outcome="L"),
        _row(OWN_TAG, week=2, opponent=THEM),
        _row("A02", week=2, opponent=OWN),
    ]
    text = _text(events.reveal_embed(_state(rows), 2))
    assert "A02" in text
    assert "1-0" in text


def test_a_first_meeting_says_so_rather_than_rendering_an_empty_record():
    rows = [_row(OWN_TAG, opponent=THEM), _row("A02", opponent=OWN)]
    assert "not faced them before" in _text(events.reveal_embed(_state(rows), 1))


def test_no_opponent_recorded_means_no_reveal():
    assert events.reveal_embed(_state([_row(OWN_TAG)]), 1) is None


async def test_the_reveal_fires_once_per_week(posted):
    rows = [_row(OWN_TAG, opponent=THEM), _row("A02", opponent=OWN)]
    state = _state(rows)
    assert await events.after_pairing_known(MagicMock(), state, 1) is True
    assert await events.after_pairing_known(MagicMock(), state, 1) is False


# ── Season recap ──────────────────────────────────────────────────────────────


def _finished_league():
    rows = []
    for week in range(1, ad.LEAGUE_WEEKS + 1):
        rows.append(
            _row(
                OWN_TAG,
                week=week,
                opponent=THEM,
                week_outcome="W" if week < 4 else "L",
                day_outcomes={1: "W", 3: "L"},
            )
        )
        rows.append(_row("A02", week=week, opponent=OWN))
    return rows


def test_the_recap_reports_the_record_and_the_days():
    text = _text(events.recap_embed(_state(_finished_league())))
    assert "3-1" in text
    assert "Age of Science" in text


async def test_the_recap_waits_for_the_league_to_actually_finish(posted):
    rows = [_row(OWN_TAG, week=1, opponent=THEM, week_outcome="W"), _row("A02")]
    assert await events.after_league_complete(MagicMock(), _state(rows)) is False
    assert posted.sent == []


async def test_the_recap_fires_once_per_league(posted):
    state = _state(_finished_league())
    assert await events.after_league_complete(MagicMock(), state) is True
    assert await events.after_league_complete(MagicMock(), state) is False


# ── The shared entry point ────────────────────────────────────────────────────


async def test_an_announcement_failure_never_breaks_the_write(posted):
    """The officer asked for a score to be recorded, and it was. A channel they
    deleted is not a reason to fail that."""
    state = _state([_row(OWN_TAG, opponent=THEM, day_outcomes={1: "W"}), _row("A02")])
    with patch.object(events, "after_day_recorded", new=AsyncMock(side_effect=RuntimeError("x"))):
        await events.announce_after_write(MagicMock(), state, week=1, day=1)


async def test_a_broken_channel_is_a_skip_not_a_repost(posted, monkeypatch):
    """Nothing is marked as posted, so fixing the channel does not cost the
    alliance the announcement entirely."""
    monkeypatch.setattr(config_health, "resolve_configured_channel", MagicMock(return_value=None))
    state = _state([_row(OWN_TAG, opponent=THEM, day_outcomes={1: "W"}), _row("A02")])
    assert await events.after_day_recorded(MagicMock(), state, 1, 1) is False
    assert posted.marked == set()


async def test_no_bot_means_no_announcements_rather_than_a_crash():
    """The sheet-first paths can write without an interaction behind them."""
    state = _state([_row(OWN_TAG, opponent=THEM, day_outcomes={1: "W"})])
    await events.announce_after_write(None, state, week=1, day=1)


def test_the_event_posts_carry_no_em_dashes():
    rows = _finished_league()
    state = _state(rows)
    for embed in (
        events.clinch_embed(state, 1),
        events.reveal_embed(state, 1),
        events.recap_embed(state),
    ):
        assert embed is not None
        assert "—" not in _text(embed)
