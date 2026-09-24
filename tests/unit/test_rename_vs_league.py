"""Unit tests for `config.rename_vs_league` (#634).

A VS league rename edits the sheet's Season/Tier/Group cells but, before
this, left `vs_score_prompt_posts` and `vs_event_posts` under the old
identity -- a prompt posted earlier in the week then read as belonging to
a stale league, and a clinch/recap could repost under the new one. This
helper carries both tables' rows forward in one guild-scoped write, and
refuses rather than pooling two leagues' records together when the new
identity already has rows of its own.
"""

import pytest

import config
from alliance_duel import LeagueKey

OLD = LeagueKey("Season 12", "Diamond", "3")
NEW = LeagueKey("Season 12", "Diamond", "4")
GUILD = 1001
OTHER_GUILD = 1002


@pytest.fixture()
def db(temp_db):
    config.init_db()
    return temp_db


def _prompt_post(guild_id: int, league: LeagueKey, *, message_id: int, week=1, day=1):
    config.record_vs_score_prompt_post(
        guild_id,
        channel_id=555,
        message_id=message_id,
        league=league,
        week=week,
        duel_day=day,
        server_date="2026-09-20",
    )


class TestScorePromptPosts:
    def test_carries_the_rows_forward(self, db):
        _prompt_post(GUILD, OLD, message_id=1)
        _prompt_post(GUILD, OLD, message_id=2)

        assert config.rename_vs_league(GUILD, OLD, NEW) is True

        assert config.get_vs_score_prompt_post(1)["league_season"] == NEW.season
        assert config.get_vs_score_prompt_post(1)["league_group"] == NEW.group
        assert config.get_vs_score_prompt_post(2)["league_group"] == NEW.group

    def test_other_guilds_untouched(self, db):
        _prompt_post(GUILD, OLD, message_id=1)
        _prompt_post(OTHER_GUILD, OLD, message_id=2)

        config.rename_vs_league(GUILD, OLD, NEW)

        assert config.get_vs_score_prompt_post(2)["league_group"] == OLD.group

    def test_other_leagues_for_this_guild_untouched(self, db):
        unrelated = LeagueKey("Season 12", "Diamond", "9")
        _prompt_post(GUILD, OLD, message_id=1)
        _prompt_post(GUILD, unrelated, message_id=2)

        config.rename_vs_league(GUILD, OLD, NEW)

        assert config.get_vs_score_prompt_post(2)["league_group"] == unrelated.group


class TestEventPosts:
    def test_clinch_key_prefix_rewritten_suffix_kept(self, db):
        old_key = f"{config.vs_league_key(OLD.season, OLD.tier, OLD.group)}|3|2"
        config.mark_vs_event_posted(GUILD, "clinch", old_key)

        config.rename_vs_league(GUILD, OLD, NEW)

        new_key = f"{config.vs_league_key(NEW.season, NEW.tier, NEW.group)}|3|2"
        assert config.vs_event_already_posted(GUILD, "clinch", new_key) is True
        assert config.vs_event_already_posted(GUILD, "clinch", old_key) is False

    def test_reveal_key_prefix_rewritten(self, db):
        old_key = f"{config.vs_league_key(OLD.season, OLD.tier, OLD.group)}|4"
        config.mark_vs_event_posted(GUILD, "reveal", old_key)

        config.rename_vs_league(GUILD, OLD, NEW)

        new_key = f"{config.vs_league_key(NEW.season, NEW.tier, NEW.group)}|4"
        assert config.vs_event_already_posted(GUILD, "reveal", new_key) is True

    def test_recap_key_exact_match_rewritten(self, db):
        """The recap key IS the league key, with no `|week` or `|day`
        suffix -- must not be skipped by a suffix-only rewrite."""
        old_key = config.vs_league_key(OLD.season, OLD.tier, OLD.group)
        config.mark_vs_event_posted(GUILD, "recap", old_key)

        config.rename_vs_league(GUILD, OLD, NEW)

        new_key = config.vs_league_key(NEW.season, NEW.tier, NEW.group)
        assert config.vs_event_already_posted(GUILD, "recap", new_key) is True
        assert config.vs_event_already_posted(GUILD, "recap", old_key) is False

    def test_other_guilds_and_leagues_untouched(self, db):
        old_key = f"{config.vs_league_key(OLD.season, OLD.tier, OLD.group)}|1|1"
        unrelated_key = "Season 12|Diamond|9|1|1"
        config.mark_vs_event_posted(GUILD, "clinch", old_key)
        config.mark_vs_event_posted(GUILD, "clinch", unrelated_key)
        config.mark_vs_event_posted(OTHER_GUILD, "clinch", old_key)

        config.rename_vs_league(GUILD, OLD, NEW)

        assert config.vs_event_already_posted(GUILD, "clinch", unrelated_key) is True
        assert config.vs_event_already_posted(OTHER_GUILD, "clinch", old_key) is True


class TestRefusesOnCollision:
    def test_refuses_when_new_identity_already_has_prompt_posts(self, db):
        _prompt_post(GUILD, OLD, message_id=1)
        _prompt_post(GUILD, NEW, message_id=2)

        assert config.rename_vs_league(GUILD, OLD, NEW) is False
        # Nothing moved.
        assert config.get_vs_score_prompt_post(1)["league_group"] == OLD.group

    def test_refuses_when_new_identity_already_has_event_posts(self, db):
        new_key = f"{config.vs_league_key(NEW.season, NEW.tier, NEW.group)}|1|1"
        old_key = f"{config.vs_league_key(OLD.season, OLD.tier, OLD.group)}|1|1"
        config.mark_vs_event_posted(GUILD, "clinch", old_key)
        config.mark_vs_event_posted(GUILD, "clinch", new_key)
        _prompt_post(GUILD, OLD, message_id=1)

        assert config.rename_vs_league(GUILD, OLD, NEW) is False
        assert config.get_vs_score_prompt_post(1)["league_group"] == OLD.group

    def test_no_matching_rows_is_a_safe_no_op(self, db):
        assert config.rename_vs_league(GUILD, OLD, NEW) is True
