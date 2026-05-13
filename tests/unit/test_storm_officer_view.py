"""
Tests for storm_officer_view.py (#125).

Pure-function helpers (bucket map, embed rendering, next-event-date
fallback) are tested here. The slash command + modal are integration
territory.
"""

import datetime as _dt
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestBucketTruncationOverflow:
    """The original "+N more" hint did `len(names) - joined.count(',') - 1`
    which is algebraically always 0. Verify the rewrite reports an
    accurate remainder when a bucket overflows the per-bucket budget."""

    def test_long_bucket_reports_accurate_remaining_count(self):
        entries = [{"label": f"Member_{i:03d}", "target_id": str(i),
                    "is_on_behalf": False, "not_on_discord": False}
                   for i in range(100)]
        rendered = sov._format_bucket_names(entries)
        assert "more)" in rendered
        import re
        m = re.search(r"\(\+(\d+) more\)", rendered)
        assert m is not None
        remaining = int(m.group(1))
        assert remaining > 0
        # The remaining count plus the names actually rendered should
        # equal the total input count.
        names_shown = rendered.split(", … (+")[0].count(", ") + 1
        assert names_shown + remaining == 100

    def test_short_bucket_renders_no_overflow_hint(self):
        entries = [{"label": "Alice", "target_id": "1",
                    "is_on_behalf": False, "not_on_discord": False},
                   {"label": "Bob",   "target_id": "2",
                    "is_on_behalf": False, "not_on_discord": False}]
        rendered = sov._format_bucket_names(entries)
        assert "more)" not in rendered
        assert rendered == "Alice, Bob"

    def test_description_caps_at_safe_budget(self):
        # Five oversized buckets ≈ 4500 chars unguarded. The render must
        # stay under Discord's 4096-char description limit.
        entries = [{"label": f"M{i:04d}", "target_id": str(i),
                    "is_on_behalf": False, "not_on_discord": False}
                   for i in range(200)]
        buckets = {k: list(entries) for k in sov._BUCKET_ORDER}
        embed = sov._render_embed(None, "DS", "2026-05-18", buckets)
        assert len(embed.description) <= 4096


class TestNotOnDiscordEnumeration:
    """Roster Sheet rows flagged `not_on_discord` should surface in the
    officer view BEFORE any on-behalf vote is cast, so leadership can
    see who still needs an on-behalf vote."""

    def _fake_roster_ws(self, header_and_rows):
        ws = MagicMock()
        ws.get_all_values.return_value = header_and_rows
        return ws

    def test_off_discord_member_appears_in_not_voted_bucket(self, seeded_db):
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name", "Display Name", "not_on_discord"],
            ["",           "Alice", "Alice",        "yes"],
            ["",           "Bob",   "Bob",          ""],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            guild = _FakeGuild(TEST_GUILD_ID, [])
            buckets, errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        assert errs == []
        names = {e["label"] for e in buckets["not_voted"]}
        assert "Alice" in names
        # Bob has not_on_discord=blank → not enumerated as a phantom.
        assert "Bob" not in names

    def test_off_discord_flag_renders_footnote_marker(self, seeded_db):
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name", "Display Name", "not_on_discord"],
            ["",           "Alice", "Alice",        "yes"],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            guild = _FakeGuild(TEST_GUILD_ID, [])
            buckets, _ = sov._build_bucket_map(guild, "DS", "2026-05-18")
            embed = sov._render_embed(guild, "DS", "2026-05-18", buckets)
        assert "¹" in embed.description
        assert "Not on Discord" in embed.description

    def test_not_voted_header_includes_off_discord_count(self, seeded_db):
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name", "Display Name", "not_on_discord"],
            ["",           "Alice", "Alice",        "yes"],
            ["",           "Carol", "Carol",        "true"],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            guild = _FakeGuild(TEST_GUILD_ID, [])
            buckets, _ = sov._build_bucket_map(guild, "DS", "2026-05-18")
            embed = sov._render_embed(guild, "DS", "2026-05-18", buckets)
        assert "[2 not on Discord]" in embed.description

    def test_roster_disabled_yields_empty_extras(self, seeded_db):
        # When roster sync isn't enabled, the helper returns ([], [])
        # and the officer view degrades to Discord-only enumeration.
        guild = _FakeGuild(TEST_GUILD_ID, [])
        buckets, errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        assert errs == []
        for entries in buckets.values():
            assert all(not e.get("not_on_discord") for e in entries)

    def test_sheet_read_failure_surfaces_in_errors(self, seeded_db):
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        with patch(
            "config.get_member_roster_sheet",
            side_effect=RuntimeError("simulated 403"),
        ):
            guild = _FakeGuild(TEST_GUILD_ID, [])
            buckets, errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        assert any("simulated 403" in e for e in errs)
        assert "not_voted" in buckets
