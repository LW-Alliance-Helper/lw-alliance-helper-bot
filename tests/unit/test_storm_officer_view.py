"""
Tests for storm_officer_view.py (#125).

Pure-function helpers (bucket map, embed rendering, next-event-date
fallback) are tested here. The slash command + modal are integration
territory.
"""

import datetime as _dt
from unittest.mock import MagicMock

import storm_officer_view as sov

from tests.unit.test_config import TEST_GUILD_ID


class _FakeMember:
    def __init__(self, member_id: int, display_name: str, bot: bool = False,
                 role_ids: list[int] | None = None):
        self.id = member_id
        self.display_name = display_name
        self.bot = bot
        self.roles = [MagicMock(id=rid) for rid in (role_ids or [])]


class _FakeGuild:
    def __init__(self, guild_id: int, members: list[_FakeMember]):
        self.id = guild_id
        self.members = members


class TestNextEventDate:
    def test_returns_next_sunday(self):
        # Wednesday — next Sunday is 4 days out.
        wed = _dt.date(2026, 5, 13)
        next_date = sov._next_event_date(wed)
        assert next_date == "2026-05-17"

    def test_when_today_is_sunday_returns_next_sunday(self):
        # The fallback skips "today" and goes to next week's Sunday.
        sun = _dt.date(2026, 5, 17)
        assert sov._next_event_date(sun) == "2026-05-24"


class TestDiscordMemberPool:
    def test_filters_out_bots(self, seeded_db):
        guild = _FakeGuild(
            TEST_GUILD_ID,
            [
                _FakeMember(1, "Alice"),
                _FakeMember(2, "Bot", bot=True),
                _FakeMember(3, "Charlie"),
            ],
        )
        pool = sov._discord_member_pool(guild)
        names = [m.display_name for m in pool]
        assert names == ["Alice", "Charlie"]

    def test_none_guild_returns_empty(self):
        assert sov._discord_member_pool(None) == []


class TestBucketMap:
    def test_unvoted_members_land_in_not_voted(self, seeded_db):
        guild = _FakeGuild(
            TEST_GUILD_ID,
            [_FakeMember(1, "Alice"), _FakeMember(2, "Bob")],
        )
        buckets, _errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        assert {e["label"] for e in buckets["not_voted"]} == {"Alice", "Bob"}
        for k in ("a", "b", "either", "cannot"):
            assert buckets[k] == []

    def test_discord_member_with_vote_lands_in_bucket(self, seeded_db):
        import config
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=1, target_member_id="1", vote="a",
        )
        guild = _FakeGuild(
            TEST_GUILD_ID,
            [_FakeMember(1, "Alice"), _FakeMember(2, "Bob")],
        )
        buckets, _errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        assert {e["label"] for e in buckets["a"]}        == {"Alice"}
        assert {e["label"] for e in buckets["not_voted"]} == {"Bob"}

    def test_on_behalf_vote_for_non_discord_member_appears(self, seeded_db):
        import config
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=999, target_member_id="Charlie", vote="b",
            is_on_behalf=True,
        )
        guild = _FakeGuild(
            TEST_GUILD_ID,
            [_FakeMember(1, "Alice")],
        )
        buckets, _errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        # Charlie shows up in B even though she's not in guild.members.
        names = {e["label"] for e in buckets["b"]}
        assert names == {"Charlie"}
        # is_on_behalf is preserved.
        charlie = next(e for e in buckets["b"] if e["label"] == "Charlie")
        assert charlie["is_on_behalf"] is True


class TestEmbedRendering:
    def test_renders_total_count_in_title(self, seeded_db):
        guild = _FakeGuild(
            TEST_GUILD_ID,
            [_FakeMember(1, "Alice"), _FakeMember(2, "Bob")],
        )
        buckets, _errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        embed = sov._render_embed(guild, "DS", "2026-05-18", buckets)
        assert "Desert Storm" in embed.title
        assert "(2 members)" in embed.title

    def test_unknown_date_falls_back_safely(self, seeded_db):
        guild = _FakeGuild(TEST_GUILD_ID, [_FakeMember(1, "Alice")])
        buckets, _errs = sov._build_bucket_map(guild, "DS", "garbage-date")
        embed = sov._render_embed(guild, "DS", "garbage-date", buckets)
        # Doesn't crash; renders the raw string in the title.
        assert "garbage-date" in embed.title

    def test_filter_shows_only_one_bucket(self, seeded_db):
        import config
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=1, target_member_id="1", vote="a",
        )
        guild = _FakeGuild(
            TEST_GUILD_ID,
            [_FakeMember(1, "Alice"), _FakeMember(2, "Bob")],
        )
        buckets, _errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        # With filter='a' only that bucket's contents render.
        embed = sov._render_embed(guild, "DS", "2026-05-18", buckets, bucket_filter="a")
        assert "Alice" in embed.description
        assert "Bob"   not in embed.description

    def test_on_behalf_marker_appears_in_description(self, seeded_db):
        import config
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=999, target_member_id="Charlie", vote="cannot",
            is_on_behalf=True,
        )
        guild = _FakeGuild(TEST_GUILD_ID, [])
        buckets, _errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        embed = sov._render_embed(guild, "DS", "2026-05-18", buckets)
        assert "on behalf" in embed.description.lower()
