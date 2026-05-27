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
                 role_ids: list[int] | None = None,
                 name: str | None = None):
        self.id = member_id
        self.display_name = display_name
        # Underlying Discord username (the @ handle). Used by the
        # on-behalf collision-disambiguation path. Defaults to the
        # display_name lowercased + member_id suffix to keep test
        # fixtures terse — collision tests pass `name` explicitly.
        self.name = name if name is not None else f"{display_name.lower()}_{member_id}"
        self.bot = bot
        self.roles = [MagicMock(id=rid) for rid in (role_ids or [])]


class _FakeGuild:
    def __init__(self, guild_id: int, members: list[_FakeMember]):
        self.id = guild_id
        self.members = members

    def get_member(self, member_id: int):
        """Mirror discord.Guild.get_member — returns the member or None.
        Needed by storm_officer_view._read_roster_rows (#139) to infer
        non-Discord status for rows whose Discord ID isn't in the guild."""
        for m in self.members:
            if m.id == member_id:
                return m
        return None


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

    def test_stale_name_keyed_vote_attributes_to_discord_member(self, seeded_db):
        """Regression for the screenshot bug: an on-behalf vote stored
        with `target_member_id=<display_name>` (because the picker
        couldn't resolve to a Discord ID at write time) should still
        attribute to the live Discord member instead of leaking into
        the phantom-leftover loop with a `not_on_discord=True` marker
        AND a duplicate entry in "Not voted yet" for the same member."""
        import config
        config.record_storm_vote(
            TEST_GUILD_ID, "DS", "2026-05-18",
            voter_user_id=999, target_member_id="Kevin", vote="a",
            is_on_behalf=True,
        )
        # Live Discord member named "Kevin" — should attract the vote.
        guild = _FakeGuild(
            TEST_GUILD_ID,
            [_FakeMember(1234567890, "Kevin"),
             _FakeMember(2, "Other")],
        )
        buckets, _errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        # Kevin appears EXACTLY once, in the "Voted Team A" bucket,
        # keyed by his Discord ID (not the name string), and NOT
        # flagged not_on_discord.
        kevin_entries = [
            e for b in buckets.values() for e in b
            if e["label"] == "Kevin"
        ]
        assert len(kevin_entries) == 1, (
            f"Kevin should appear once total, got {len(kevin_entries)}: "
            f"{[(e['label'], e['target_id'], e['not_on_discord']) for e in kevin_entries]}"
        )
        kevin = kevin_entries[0]
        assert kevin["target_id"] == "1234567890"
        assert kevin["not_on_discord"] is False
        assert kevin["is_on_behalf"] is True
        # And he's in the "a" bucket, not "not_voted".
        assert kevin in buckets["a"]
        assert kevin not in buckets["not_voted"]


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
            ["1001",       "Alice", "Alice",        "yes"],
            ["1002",       "Bob",   "Bob",          ""],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            # Both have Discord IDs; bot must see Bob as a guild member
            # so he ISN'T mis-inferred as non-Discord by #139.
            bob_member = _FakeMember(1002, "Bob")
            guild = _FakeGuild(TEST_GUILD_ID, [bob_member])
            buckets, errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        # No stale-ID warning because Bob is in the guild.
        assert errs == []
        names = {e["label"] for e in buckets["not_voted"]}
        # Alice's explicit flag puts her in the not-voted bucket.
        assert "Alice" in names
        # Bob (Discord member, no flag, in guild) is also there because
        # _discord_member_pool enumerates him from guild.members.
        assert "Bob" in names

    def test_blank_discord_id_inferred_non_discord(self, seeded_db):
        """#139 — a row with blank Discord ID and no explicit flag is
        inferred as non-Discord and surfaces in Not Voted Yet."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            # No `not_on_discord` column at all — alliance hasn't added it.
            ["Discord ID", "Name",  "Display Name"],
            ["",           "Carol", "Carol"],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            guild = _FakeGuild(TEST_GUILD_ID, [])
            buckets, _errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        names = {e["label"] for e in buckets["not_voted"]}
        # Carol surfaces because her blank Discord ID is inferred as
        # non-Discord — alliance doesn't need to add the column for the
        # bot to do the right thing.
        assert "Carol" in names

    def test_presence_column_yes_overrides_inference(self, seeded_db):
        """The bot-maintained 'Is this user in Discord?' column wins
        over the ID-diff inference path — even if the live guild
        doesn't have a member with that ID, a Yes in the cell keeps
        the row classified as Discord-on."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name", "Display Name", "Is this user in Discord?"],
            ["12345",      "Alice", "Alice",        "Yes"],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            # No matching member in the guild — without the column,
            # the inference would flag Alice as non-Discord. The
            # presence column with "Yes" overrides that.
            guild = _FakeGuild(TEST_GUILD_ID, [])
            buckets, errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        # Alice was tagged Discord-on by the column — she gets
        # bucketed via guild membership, not roster non-Discord
        # fallback. The empty guild means she doesn't appear via the
        # Discord member pool, and her column-Yes row stays out of
        # the non-Discord enumeration too. So no stale-IDs warning.
        assert not any("stale Discord IDs" in e for e in errs)

    def test_presence_column_no_overrides_inference(self, seeded_db):
        """A No in the presence column flags non-Discord even when the
        Discord ID would otherwise resolve to a guild member."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name", "Display Name", "Is this user in Discord?"],
            ["100",        "Alice", "Alice",        "No"],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            # Alice IS in the guild — but the column override flags her
            # as non-Discord anyway (alliance signalled the alt-account
            # case).
            guild = _FakeGuild(TEST_GUILD_ID, [_FakeMember(100, "Alice")])
            buckets, _errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        # Alice's column-No flag lands her in not_voted as a non-Discord
        # entry (in addition to her live Discord-member entry).
        labels = {e["label"] for e in buckets["not_voted"]}
        assert "Alice" in labels

    def test_presence_column_blank_falls_back_to_legacy(self, seeded_db):
        """A blank cell in the new column falls through to the legacy
        not_on_discord path + inference."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name", "Display Name",
             "Is this user in Discord?", "not_on_discord"],
            ["",           "Carol", "Carol", "", "yes"],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            guild = _FakeGuild(TEST_GUILD_ID, [])
            buckets, _errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        # Blank cell → falls back to legacy not_on_discord = yes.
        labels = {e["label"] for e in buckets["not_voted"]}
        assert "Carol" in labels

    def test_bot_id_inferred_non_discord(self, seeded_db):
        """A roster row mapped to a bot ID (admin pasted the wrong ID)
        is treated as a stale match — bots aren't real alliance members."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name", "Display Name"],
            ["555",        "BotAccount", "BotAccount"],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            # The ID 555 maps to a bot in the guild.
            guild = _FakeGuild(
                TEST_GUILD_ID, [_FakeMember(555, "BotAccount", bot=True)],
            )
            buckets, errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        # BotAccount is in not_voted as a non-Discord entry — the
        # bot's "membership" in the guild doesn't make it count.
        not_voted_labels = {e["label"] for e in buckets["not_voted"]}
        assert "BotAccount" in not_voted_labels
        # Soft warning so the alliance can clean up the row.
        assert any("stale Discord IDs" in e for e in errs)

    def test_stale_discord_id_inferred_non_discord(self, seeded_db):
        """#139 — a row with a Discord ID that's not in the guild
        (member left) is inferred as non-Discord."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name", "Display Name"],
            ["9999",       "Ghost", "Ghost"],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            # Ghost (id=9999) isn't in the guild — she left.
            guild = _FakeGuild(TEST_GUILD_ID, [])
            buckets, errs = sov._build_bucket_map(guild, "DS", "2026-05-18")
        names = {e["label"] for e in buckets["not_voted"]}
        assert "Ghost" in names
        # Soft warning surfaces so leadership can clean up.
        assert any("stale Discord IDs" in e for e in errs)

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


class TestNonDiscordInferenceNonNumericId(TestNotOnDiscordEnumeration):
    """Audit minor for #139: non-numeric Discord-ID cells ("TBD",
    "abc") were silently kept as Discord-member rows. Spec says
    non-numeric → non-Discord."""

    def test_non_numeric_id_inferred_as_non_discord(self, seeded_db):
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name",   "Display Name"],
            ["TBD",        "Alice",  "Alice"],   # placeholder
            ["123abc",     "Bob",    "Bob"],     # malformed
            ["1001",       "Carol",  "Carol"],   # real ID
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            roster, _errors = sov._read_roster_rows(TEST_GUILD_ID)
        by_name = {r["name"]: r for r in roster}
        # Non-numeric placeholders inferred as non-Discord.
        assert by_name["Alice"]["not_on_discord"] is True
        assert by_name["Bob"]["not_on_discord"] is True
        # Real numeric ID NOT inferred (no guild handle, so the
        # stale-ID branch can't fire — that's a separate test).
        assert by_name["Carol"]["not_on_discord"] is False


class TestNameFallbackCascade:
    """#268 — when Display Name (C) is blank, the reader must cascade
    through Name (B) and the live Discord member before falling to the
    raw Discord ID. Pre-fix behaviour rendered the numeric ID (or the
    alliance's workaround text typed into the ID column) as the member's
    name in the poll and Team Plan."""

    def _fake_roster_ws(self, rows):
        ws = MagicMock()
        ws.get_all_values.return_value = rows
        return ws

    def test_display_blank_falls_back_to_name_column(self, seeded_db):
        """Hand-typed row: Name (B) populated, Display Name (C) blank.
        The resolved name should come from column B, not the empty C
        and not the discord_id fallback."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name",      "Display Name"],
            ["",           "JoeNoDisc", ""],  # alliance member, not on Discord
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            roster, _errs = sov._read_roster_rows(TEST_GUILD_ID)
        assert len(roster) == 1
        assert roster[0]["name"] == "JoeNoDisc"
        assert roster[0]["not_on_discord"] is True

    def test_workaround_id_text_does_not_render_as_name(self, seeded_db):
        """The user's reported workaround: typed "no-disc-1" into the
        ID column to force the row to appear. Pre-fix the poll rendered
        "no-disc-1" as the member's name. The cascade should pick up
        the actual name from column B instead."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name",  "Display Name"],
            ["no-disc-1",  "Alice", ""],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            roster, _errs = sov._read_roster_rows(TEST_GUILD_ID)
        assert len(roster) == 1
        # Name resolves to column B, not the non-numeric ID workaround.
        assert roster[0]["name"] == "Alice"
        # Non-numeric ID still infers non-Discord.
        assert roster[0]["not_on_discord"] is True

    def test_live_member_display_name_used_when_both_columns_blank(self, seeded_db):
        """Real numeric Discord ID + both name columns blank: cascade
        should look up the live Discord member's display_name. Covers
        the Team Plan case where Discord-on members rendered as raw
        18-digit IDs because Display Name was empty."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name", "Display Name"],
            ["100",        "",     ""],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            guild = _FakeGuild(TEST_GUILD_ID, [_FakeMember(100, "Alice")])
            roster, _errs = sov._read_roster_rows(TEST_GUILD_ID, guild=guild)
        assert len(roster) == 1
        assert roster[0]["name"] == "Alice"  # from live guild lookup
        assert roster[0]["not_on_discord"] is False

    def test_falls_back_to_discord_id_when_no_other_source(self, seeded_db):
        """All other sources blank and guild can't resolve — discord_id
        is the last-resort fallback so the row stays visible."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name", "Display Name"],
            ["100",        "",     ""],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            roster, _errs = sov._read_roster_rows(TEST_GUILD_ID)  # no guild
        assert len(roster) == 1
        assert roster[0]["name"] == "100"  # last-resort fallback

    def test_display_name_still_wins_when_populated(self, seeded_db):
        """Regression guard: when Display Name IS populated, it still
        wins over the Name column (preserving the alliance's chosen
        alias)."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name",       "Display Name"],
            ["100",        "alice_user", "AliceTheAlly"],  # Display wins
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            roster, _errs = sov._read_roster_rows(TEST_GUILD_ID)
        assert len(roster) == 1
        assert roster[0]["name"] == "AliceTheAlly"

    def test_only_name_column_populated_row_not_skipped(self, seeded_db):
        """Pre-fix row-skip condition was `if not (discord_id or name)`
        where `name` = display_col. A row with ONLY column B populated
        was silently dropped. The widened skip condition must keep it."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name",     "Display Name"],
            ["",           "HandTyped", ""],
        ]
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            roster, _errs = sov._read_roster_rows(TEST_GUILD_ID)
        assert len(roster) == 1
        assert roster[0]["name"] == "HandTyped"


class TestResolveMemberNameHelper:
    """Direct unit tests for the `_resolve_member_name` fallback
    cascade (#268). Shared between officer view + roster builder."""

    def test_prefers_display_value(self):
        guild = _FakeGuild(TEST_GUILD_ID, [_FakeMember(100, "Alice")])
        assert sov._resolve_member_name(
            "100", "DisplayAlias", "name_value", guild,
        ) == "DisplayAlias"

    def test_falls_back_to_name_value(self):
        guild = _FakeGuild(TEST_GUILD_ID, [_FakeMember(100, "Alice")])
        assert sov._resolve_member_name(
            "100", "", "name_value", guild,
        ) == "name_value"

    def test_falls_back_to_live_member_display_name(self):
        guild = _FakeGuild(TEST_GUILD_ID, [_FakeMember(100, "Alice")])
        assert sov._resolve_member_name(
            "100", "", "", guild,
        ) == "Alice"

    def test_bot_member_skipped_for_live_lookup(self):
        guild = _FakeGuild(TEST_GUILD_ID, [_FakeMember(100, "BotName", bot=True)])
        # Bot resolves the ID but the helper rejects bots — falls to ID.
        assert sov._resolve_member_name(
            "100", "", "", guild,
        ) == "100"

    def test_non_numeric_id_skips_live_lookup(self):
        guild = _FakeGuild(TEST_GUILD_ID, [_FakeMember(100, "Alice")])
        # ID isn't numeric so we don't even try get_member.
        assert sov._resolve_member_name(
            "no-disc-1", "", "", guild,
        ) == "no-disc-1"

    def test_no_guild_falls_to_discord_id(self):
        assert sov._resolve_member_name(
            "100", "", "", None,
        ) == "100"


class TestStaleIdLogDedup:
    """Audit Major for #139: the stale-ID warning re-logged on every
    Refresh button click and every on-behalf picker open. Module-level
    memo prevents the spam."""

    def _fake_roster_ws(self, rows):
        ws = MagicMock()
        ws.get_all_values.return_value = rows
        return ws

    def test_repeated_reads_log_warning_once(self, seeded_db, caplog):
        import config
        import logging
        config.save_member_roster_config(
            TEST_GUILD_ID + 7777, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name", "Display Name"],
            ["999111",     "Stale", "Stale"],
        ]
        # Clear the memo so this test isn't dependent on test order.
        sov._STALE_ID_LOG_MEMO.clear()
        # Guild whose get_member always returns None — every numeric
        # ID is "stale" from its perspective.
        guild = _FakeGuild(TEST_GUILD_ID + 7777, [])
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            with caplog.at_level(logging.WARNING, logger="storm_officer_view"):
                # First read — should log.
                sov._read_roster_rows(TEST_GUILD_ID + 7777, guild=guild)
                first = [r for r in caplog.records
                         if "stale roster Discord IDs" in r.getMessage()]
                # Second read with the same stale set — should NOT re-log.
                caplog.clear()
                sov._read_roster_rows(TEST_GUILD_ID + 7777, guild=guild)
                second = [r for r in caplog.records
                          if "stale roster Discord IDs" in r.getMessage()]
        assert len(first) == 1
        assert second == []

    def test_errors_list_still_returned_on_repeated_reads(self, seeded_db):
        """The soft error in `errors` is per-read context for the
        embed warning — that should fire every time. The DEDUP is only
        on the log, not the user-visible warning."""
        import config
        config.save_member_roster_config(
            TEST_GUILD_ID + 7778, enabled=1, tab_name="Members",
            discord_id_col=0, name_col=1, display_col=2,
        )
        rows = [
            ["Discord ID", "Name", "Display Name"],
            ["888777",     "Stale", "Stale"],
        ]
        sov._STALE_ID_LOG_MEMO.clear()
        guild = _FakeGuild(TEST_GUILD_ID + 7778, [])
        with patch(
            "config.get_member_roster_sheet",
            return_value=self._fake_roster_ws(rows),
        ):
            _, errs1 = sov._read_roster_rows(TEST_GUILD_ID + 7778, guild=guild)
            _, errs2 = sov._read_roster_rows(TEST_GUILD_ID + 7778, guild=guild)
        # Both reads return the soft error so the embed warning always
        # surfaces — only the log dedupes.
        assert any("stale Discord IDs" in e for e in errs1)
        assert any("stale Discord IDs" in e for e in errs2)


class TestOnBehalfVoteView:
    """#168 — the on-behalf flow is now an ephemeral view with Member +
    Vote selects, no free-text modal. `storm_signups.target_member_id`
    UPSERTs on a UNIQUE `(guild, event_type, event_date, target_member_id)`
    index; self-votes store `str(discord_user_id)` and on-behalf votes
    store the roster name. A purely numeric roster name would collide
    with a real Discord user's vote on the same event, so the new view
    filters those names out of the Member Select at construction time —
    the collision can't reach the schema because the officer can't pick
    a numeric-name option."""

    def _fake_parent_view(self):
        view = MagicMock()
        view.guild_id = TEST_GUILD_ID
        view.guild = _FakeGuild(TEST_GUILD_ID, [])
        view.event_type = "DS"
        view.event_date = "2026-05-18"
        view.owner_user_id = 1
        view.message = None
        view.bucket_filter = None
        view.buckets = {}
        # refresh_buckets is awaited after submit fires.
        view.refresh_buckets = AsyncMock()
        return view

    def _fake_interaction(self, user_id: int = 1):
        interaction = MagicMock()
        interaction.user.id = user_id
        interaction.response.send_message = AsyncMock()
        interaction.response.edit_message = AsyncMock()
        interaction.response.defer        = AsyncMock()
        interaction.followup.send         = AsyncMock()
        return interaction

    def test_numeric_names_filtered_from_member_select(self, seeded_db):
        """The schema-collision risk vanishes when the picker can't
        offer a numeric-name option. This is the structural replacement
        for the old `_OnBehalfModal` numeric-reject branch."""
        roster = [
            {"name": "Alice"},
            {"name": "1234"},           # numeric — must be filtered
            {"name": "Charlie #1234"},  # non-numeric — kept
        ]
        view = sov._OnBehalfVoteView(
            self._fake_parent_view(), roster, teams_setting="both",
        )
        names = [m["name"] for m in view.members]
        assert "Alice" in names
        assert "Charlie #1234" in names
        assert "1234" not in names

    def test_members_deduped_and_sorted(self, seeded_db):
        roster = [
            {"name": "Charlie"},
            {"name": "alice"},
            {"name": "Alice"},  # case-dupe — second one drops
            {"name": ""},        # blank — drops
        ]
        view = sov._OnBehalfVoteView(
            self._fake_parent_view(), roster, teams_setting="both",
        )
        names = [m["name"] for m in view.members]
        # Sorted case-insensitively; case-dupe collapsed to first seen.
        assert names == ["alice", "Charlie"]

    def test_vote_options_branch_on_teams_setting(self, seeded_db):
        roster = [{"name": "Alice"}]
        # Single-team alliance: only Team A + Cannot are valid options.
        view_a = sov._OnBehalfVoteView(
            self._fake_parent_view(), roster, teams_setting="A",
        )
        opts_a = sov._vote_select_options("DS", TEST_GUILD_ID, "A")
        values_a = [o.value for o in opts_a]
        assert "a" in values_a
        assert "b" not in values_a
        assert "either" not in values_a
        assert "cannot" in values_a
        # Both-teams alliance: all four options.
        opts_both = sov._vote_select_options("DS", TEST_GUILD_ID, "both")
        values_both = [o.value for o in opts_both]
        assert set(values_both) == {"a", "b", "either", "cannot"}
        # Touch the constructor to silence the unused-local lint.
        assert view_a.teams_setting == "A"

    def test_paging_kicks_in_above_25_members(self, seeded_db):
        roster = [{"name": f"Member{i:03d}"} for i in range(40)]
        view = sov._OnBehalfVoteView(
            self._fake_parent_view(), roster, teams_setting="both",
        )
        assert view.page_count == 2
        assert len(view._members_for_page()) == 25
        view.page = 1
        assert len(view._members_for_page()) == 15

    def test_collision_on_display_name_expands_to_disambiguated_picker_entries(
        self, seeded_db,
    ):
        """When two Discord members share the same server nickname AND
        the roster row doesn't carry an explicit discord_id, the picker
        must surface ALL matching Discord IDs — not silently overwrite
        one with the other. Officers see `Phoenix (@phoenix99)` vs
        `Phoenix (@phoenix01)` and pick the right user. The previous
        `dict[name] = id` lookup let last-write-win, so the officer
        could cast a vote for the wrong member with no signal."""
        parent = self._fake_parent_view()
        parent.guild = _FakeGuild(
            TEST_GUILD_ID,
            [
                _FakeMember(101, "Phoenix", name="phoenix99"),
                _FakeMember(102, "Phoenix", name="phoenix01"),
                _FakeMember(103, "SoloName", name="solo"),
            ],
        )
        roster = [
            {"name": "Phoenix", "discord_id": "", "not_on_discord": False},
            {"name": "SoloName", "discord_id": "", "not_on_discord": False},
        ]
        view = sov._OnBehalfVoteView(
            parent, roster, teams_setting="both",
        )
        names = [m["name"] for m in view.members]
        target_ids = [m["target_id"] for m in view.members]

        # SoloName has no collision -> renders once with the live ID.
        assert "SoloName" in names
        solo_idx = names.index("SoloName")
        assert target_ids[solo_idx] == "103"

        # Phoenix collision expands into two disambiguated entries. Each
        # maps to a distinct Discord ID so the officer's pick lands on
        # the right user.
        assert "Phoenix (@phoenix01)" in names
        assert "Phoenix (@phoenix99)" in names
        # The bare "Phoenix" label must NOT exist — that's the
        # last-write-wins entry the disambiguation is replacing.
        assert "Phoenix" not in names
        # IDs are distinct and cover both alts.
        disambiguated_ids = {
            target_ids[names.index("Phoenix (@phoenix01)")],
            target_ids[names.index("Phoenix (@phoenix99)")],
        }
        assert disambiguated_ids == {"101", "102"}

    def test_collision_does_not_expand_when_roster_pins_a_discord_id(
        self, seeded_db,
    ):
        """If the roster row already carries an explicit `discord_id`,
        the roster has chosen which user this row represents — no
        disambiguation needed even when Discord has multiple members
        with the same display_name."""
        parent = self._fake_parent_view()
        parent.guild = _FakeGuild(
            TEST_GUILD_ID,
            [
                _FakeMember(101, "Phoenix", name="phoenix99"),
                _FakeMember(102, "Phoenix", name="phoenix01"),
            ],
        )
        roster = [
            {"name": "Phoenix", "discord_id": "101", "not_on_discord": False},
        ]
        view = sov._OnBehalfVoteView(
            parent, roster, teams_setting="both",
        )
        names = [m["name"] for m in view.members]
        target_ids = [m["target_id"] for m in view.members]
        # Single entry, no `(@…)` suffix, ID matches the roster's pin.
        assert names == ["Phoenix"]
        assert target_ids == ["101"]

    def test_not_on_discord_roster_row_skips_collision_expansion(
        self, seeded_db,
    ):
        """A roster row flagged `not_on_discord` represents a real
        non-Discord member by name — no Discord-side expansion even if
        the name happens to match live Discord members."""
        parent = self._fake_parent_view()
        parent.guild = _FakeGuild(
            TEST_GUILD_ID,
            [
                _FakeMember(101, "Phoenix", name="phoenix99"),
                _FakeMember(102, "Phoenix", name="phoenix01"),
            ],
        )
        roster = [
            {"name": "Phoenix", "discord_id": "", "not_on_discord": True},
        ]
        view = sov._OnBehalfVoteView(
            parent, roster, teams_setting="both",
        )
        names = [m["name"] for m in view.members]
        target_ids = [m["target_id"] for m in view.members]
        # The vote is keyed by the raw name — non-Discord member.
        assert names == ["Phoenix"]
        assert target_ids == ["Phoenix"]

    def test_submit_disabled_until_member_and_vote_picked(self, seeded_db):
        view = sov._OnBehalfVoteView(
            self._fake_parent_view(), [{"name": "Alice"}], teams_setting="both",
        )
        submit_btns = [
            c for c in view.children
            if getattr(c, "label", "").startswith("✅ Submit")
        ]
        assert submit_btns and submit_btns[0].disabled is True

    async def test_submit_records_vote_and_refreshes_parent(self, seeded_db):
        parent = self._fake_parent_view()
        view = sov._OnBehalfVoteView(
            parent, [{"name": "Alice"}], teams_setting="both",
        )
        view.selected_members = ["Alice"]
        view.selected_vote = "a"
        interaction = self._fake_interaction(user_id=parent.owner_user_id)
        with patch(
            "config.record_storm_vote", return_value=True,
        ) as record:
            await view._on_submit(interaction)
        record.assert_called_once()
        kwargs = record.call_args.kwargs
        # Roster row without a Discord ID — target stays as the name.
        assert kwargs["target_member_id"] == "Alice"
        assert kwargs["vote"] == "a"
        assert kwargs["is_on_behalf"] is True
        parent.refresh_buckets.assert_awaited()

    async def test_submit_resolves_discord_member_to_discord_id(self, seeded_db):
        """Regression for the on-behalf "Submit appears to work but no
        change" bug. Pre-fix the picker stored the picked NAME as
        `target_member_id` regardless of whether the row was a Discord
        member or a non-Discord roster entry. `_build_bucket_map` keys
        Discord-member buckets by `str(member.id)` (matching SignupView's
        self-vote shape), so a name-keyed on-behalf vote landed in a
        separate phantom bucket and the original "Not voted yet" entry
        for the Discord member never moved.

        Fix: when the picked row carries a `discord_id` and isn't flagged
        `not_on_discord`, use the Discord ID as `target_member_id` so the
        bucket-builder rejoins it to the live member."""
        parent = self._fake_parent_view()
        roster = [
            {"name": "Kevin", "discord_id": "1501975127200501840", "not_on_discord": False},
        ]
        view = sov._OnBehalfVoteView(parent, roster, teams_setting="both")
        view.selected_members = ["Kevin"]
        view.selected_vote = "a"
        interaction = self._fake_interaction(user_id=parent.owner_user_id)
        with patch("config.record_storm_vote", return_value=True) as record:
            await view._on_submit(interaction)
        kwargs = record.call_args.kwargs
        assert kwargs["target_member_id"] == "1501975127200501840"

    async def test_submit_keeps_name_for_non_discord_roster_member(self, seeded_db):
        """Roster row flagged `not_on_discord=True` keeps the name as
        the target_member_id — those rows are bucket-builder-keyed by
        name in `_build_bucket_map`'s tier-2 loop."""
        parent = self._fake_parent_view()
        roster = [
            {"name": "Frank", "discord_id": "", "not_on_discord": True},
        ]
        view = sov._OnBehalfVoteView(parent, roster, teams_setting="both")
        view.selected_members = ["Frank"]
        view.selected_vote = "b"
        interaction = self._fake_interaction(user_id=parent.owner_user_id)
        with patch("config.record_storm_vote", return_value=True) as record:
            await view._on_submit(interaction)
        kwargs = record.call_args.kwargs
        assert kwargs["target_member_id"] == "Frank"


class TestOnBehalfMultiSelect:
    """#218 — multi-select + "select all not-voted" shortcut + show/hide
    already-voted toggle. Tester feedback: manually entering 30 members
    is tedious; a 100-member alliance would be unworkable. The picker
    now accepts multi-pick per page (up to Discord's 25-per-Select cap),
    persists picks across pagination, and exposes two shortcuts: stage
    every not-yet-voted member with one click, and toggle whether the
    voted bucket appears in the picker."""

    def _fake_parent_view(self):
        view = MagicMock()
        view.guild_id = TEST_GUILD_ID
        view.guild = _FakeGuild(TEST_GUILD_ID, [])
        view.event_type = "DS"
        view.event_date = "2026-05-18"
        view.owner_user_id = 1
        view.message = None
        view.bucket_filter = None
        view.buckets = {}
        view.refresh_buckets = AsyncMock()
        return view

    def _fake_interaction(self, user_id: int = 1):
        interaction = MagicMock()
        interaction.user.id = user_id
        interaction.response.send_message = AsyncMock()
        interaction.response.edit_message = AsyncMock()
        interaction.response.defer        = AsyncMock()
        interaction.followup.send         = AsyncMock()
        return interaction

    def test_member_select_max_values_matches_page_size(self, seeded_db):
        """Multi-pick: max_values=len(options) so officers can tick up
        to 25 names per page in one Select interaction."""
        roster = [{"name": f"Member{i:03d}"} for i in range(10)]
        view = sov._OnBehalfVoteView(
            self._fake_parent_view(), roster, teams_setting="both",
        )
        member_sels = [
            c for c in view.children
            if isinstance(c, __import__("discord").ui.Select) and c.row == 0
        ]
        assert member_sels
        assert member_sels[0].max_values == 10
        assert member_sels[0].min_values == 0

    def test_voted_members_hidden_by_default(self, seeded_db):
        """`voted_target_ids` set → Member Select shows only not-voted
        members. Officers can't accidentally clobber an existing vote."""
        roster = [
            {"name": "Alice", "discord_id": "10", "not_on_discord": False},
            {"name": "Bob",   "discord_id": "20", "not_on_discord": False},
            {"name": "Carol", "discord_id": "30", "not_on_discord": False},
        ]
        view = sov._OnBehalfVoteView(
            self._fake_parent_view(), roster, teams_setting="both",
            voted_target_ids={"10", "20"},
        )
        visible = [m["name"] for m in view.members]
        assert visible == ["Carol"]

    def test_show_voted_toggle_surfaces_voted_members(self, seeded_db):
        """Flipping show_voted=True restores the full roster so the
        officer can correct a prior vote."""
        roster = [
            {"name": "Alice", "discord_id": "10", "not_on_discord": False},
            {"name": "Bob",   "discord_id": "20", "not_on_discord": False},
        ]
        view = sov._OnBehalfVoteView(
            self._fake_parent_view(), roster, teams_setting="both",
            voted_target_ids={"10"},
        )
        assert [m["name"] for m in view.members] == ["Bob"]
        view.show_voted = True
        view._build_components()
        assert sorted(m["name"] for m in view.members) == ["Alice", "Bob"]

    def test_show_voted_button_only_rendered_when_voted_set_nonempty(self, seeded_db):
        """No prior votes → the 👁️ toggle button doesn't render; the
        affordance would have nothing to toggle."""
        roster = [{"name": "Alice"}]
        view_no_votes = sov._OnBehalfVoteView(
            self._fake_parent_view(), roster, teams_setting="both",
        )
        labels = [getattr(c, "label", "") for c in view_no_votes.children]
        assert not any("Show already-voted" in lab for lab in labels)
        assert not any("Hide already-voted" in lab for lab in labels)

        view_with_votes = sov._OnBehalfVoteView(
            self._fake_parent_view(), roster, teams_setting="both",
            voted_target_ids={"99"},
        )
        labels = [getattr(c, "label", "") for c in view_with_votes.children]
        assert any("Show already-voted" in lab for lab in labels)

    async def test_select_all_not_voted_stages_picks(self, seeded_db):
        """📥 button drops every not-yet-voted member into
        selected_members without auto-submitting. Submit gate stays in
        place so a misclick doesn't cast 100 votes."""
        roster = [
            {"name": "Alice", "discord_id": "10", "not_on_discord": False},
            {"name": "Bob",   "discord_id": "20", "not_on_discord": False},
            {"name": "Carol", "discord_id": "30", "not_on_discord": False},
        ]
        view = sov._OnBehalfVoteView(
            self._fake_parent_view(), roster, teams_setting="both",
            voted_target_ids={"10"},
        )
        select_all_btns = [
            c for c in view.children
            if getattr(c, "label", "").startswith("📥 Select all not-voted")
        ]
        assert select_all_btns
        interaction = self._fake_interaction(user_id=view.parent_view.owner_user_id)
        with patch("config.record_storm_vote") as record:
            await select_all_btns[0].callback(interaction)
        # The button only stages — no write happened.
        record.assert_not_called()
        assert sorted(view.selected_members) == ["Bob", "Carol"]

    def test_select_all_button_disabled_when_no_not_voted_left(self, seeded_db):
        roster = [
            {"name": "Alice", "discord_id": "10", "not_on_discord": False},
        ]
        view = sov._OnBehalfVoteView(
            self._fake_parent_view(), roster, teams_setting="both",
            voted_target_ids={"10"},
        )
        select_all_btns = [
            c for c in view.children
            if getattr(c, "label", "").startswith("📥 Select all not-voted")
        ]
        assert select_all_btns and select_all_btns[0].disabled is True

    async def test_submit_records_every_picked_member(self, seeded_db):
        """N picks → N record_storm_vote calls + one parent refresh."""
        parent = self._fake_parent_view()
        roster = [
            {"name": "Alice", "discord_id": "10", "not_on_discord": False},
            {"name": "Bob",   "discord_id": "20", "not_on_discord": False},
            {"name": "Carol", "discord_id": "30", "not_on_discord": False},
        ]
        view = sov._OnBehalfVoteView(parent, roster, teams_setting="both")
        view.selected_members = ["Alice", "Bob", "Carol"]
        view.selected_vote = "either"
        interaction = self._fake_interaction(user_id=parent.owner_user_id)
        with patch("config.record_storm_vote", return_value=True) as record:
            await view._on_submit(interaction)
        assert record.call_count == 3
        target_ids = [c.kwargs["target_member_id"] for c in record.call_args_list]
        assert sorted(target_ids) == ["10", "20", "30"]
        # One refresh, not three — flicker / sheet-read amplification
        # would be the bug here.
        parent.refresh_buckets.assert_awaited_once()

    async def test_submit_partial_failure_does_not_abort_remaining(self, seeded_db):
        """If one record_storm_vote returns False, the rest still write
        and the ack tells the officer how many fell out."""
        parent = self._fake_parent_view()
        roster = [
            {"name": "Alice", "discord_id": "10", "not_on_discord": False},
            {"name": "Bob",   "discord_id": "20", "not_on_discord": False},
            {"name": "Carol", "discord_id": "30", "not_on_discord": False},
        ]
        view = sov._OnBehalfVoteView(parent, roster, teams_setting="both")
        view.selected_members = ["Alice", "Bob", "Carol"]
        view.selected_vote = "a"
        interaction = self._fake_interaction(user_id=parent.owner_user_id)
        # Bob's write fails; Alice + Carol succeed.
        with patch(
            "config.record_storm_vote",
            side_effect=[True, False, True],
        ) as record:
            await view._on_submit(interaction)
        assert record.call_count == 3
        # The followup ack carries the partial-failure copy.
        ack_call = interaction.followup.send.call_args
        ack_text = ack_call.args[0] if ack_call.args else ack_call.kwargs.get("content", "")
        assert "2 on-behalf vote" in ack_text
        assert "1 failed" in ack_text

    def test_page_picks_survive_pagination(self, seeded_db):
        """Picks made on page 1 persist when the officer paginates to
        page 2 and ticks more names. Replacement is page-scoped — only
        unticking on the current page removes a pick."""
        roster = [{"name": f"Member{i:03d}"} for i in range(40)]
        view = sov._OnBehalfVoteView(
            self._fake_parent_view(), roster, teams_setting="both",
        )
        # Manually seed page-1 picks (simulating an earlier Select
        # interaction).
        view.selected_members = ["Member000", "Member001"]
        view.page = 1
        view._build_components()
        # Add a page-2 pick via direct list mutation (the Select callback
        # would do the same kept+new merge).
        view.selected_members = ["Member000", "Member001", "Member025"]
        view.page = 0
        view._build_components()
        # All three survive across the page flip.
        assert sorted(view.selected_members) == [
            "Member000", "Member001", "Member025",
        ]


class TestOnBehalfAckFormatting:
    """#218 — `_format_on_behalf_ack` covers single, multi, partial-fail
    and all-fail paths. Single-pick keeps the original bold-name copy
    so existing tester muscle memory still matches."""

    def test_single_recorded_keeps_original_phrasing(self):
        assert sov._format_on_behalf_ack(["Alice"], [], "a") == (
            "✅ Recorded on-behalf vote for **Alice**."
        )

    def test_multi_recorded_uses_count_and_preview(self):
        ack = sov._format_on_behalf_ack(
            ["Alice", "Bob", "Carol"], [], "either",
        )
        assert "3 on-behalf vote" in ack
        assert "Either" in ack
        assert "Alice" in ack and "Bob" in ack and "Carol" in ack

    def test_long_preview_caps_at_preview_count_with_overflow(self):
        names = [f"M{i}" for i in range(10)]
        ack = sov._format_on_behalf_ack(names, [], "a")
        assert "+5 more" in ack
        # First five names show, rest don't.
        for name in names[:sov._ACK_NAME_PREVIEW]:
            assert name in ack
        for name in names[sov._ACK_NAME_PREVIEW:]:
            assert name not in ack

    def test_all_failed_returns_warning(self):
        ack = sov._format_on_behalf_ack([], ["Alice", "Bob"], "a")
        assert ack.startswith("⚠️")
        assert "any of the 2" in ack

    def test_partial_failure_appends_failed_count(self):
        ack = sov._format_on_behalf_ack(
            ["Alice", "Bob"], ["Carol"], "a",
        )
        assert "2 on-behalf vote" in ack
        assert "1 failed" in ack


class TestOfficerViewTeamsGate:
    """#148 — OfficerView's "Set up Team A/B" buttons honor the
    alliance's `teams` setting. Single-team alliances see only their
    team's button."""

    def test_teams_both_shows_both_buttons(self, seeded_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
            teams="both",
        )
        guild = _FakeGuild(TEST_GUILD_ID, [])
        view = sov.OfficerView(guild, owner_user_id=1, event_type="DS",
                               event_date="2026-05-18")
        labels = [getattr(c, "label", "") for c in view.children if hasattr(c, "label")]
        assert any("Set up Team A" in lab for lab in labels)
        assert any("Set up Team B" in lab for lab in labels)

    def test_teams_a_shows_only_a_button(self, seeded_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
            teams="A",
        )
        guild = _FakeGuild(TEST_GUILD_ID, [])
        view = sov.OfficerView(guild, owner_user_id=1, event_type="DS",
                               event_date="2026-05-18")
        labels = [getattr(c, "label", "") for c in view.children if hasattr(c, "label")]
        assert any("Set up Team A" in lab for lab in labels)
        assert not any("Set up Team B" in lab for lab in labels)

    def test_teams_b_shows_only_b_button(self, seeded_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
            teams="B",
        )
        guild = _FakeGuild(TEST_GUILD_ID, [])
        view = sov.OfficerView(guild, owner_user_id=1, event_type="DS",
                               event_date="2026-05-18")
        labels = [getattr(c, "label", "") for c in view.children if hasattr(c, "label")]
        assert any("Set up Team B" in lab for lab in labels)
        assert not any("Set up Team A" in lab for lab in labels)

    def test_cs_respects_teams_setting(self, seeded_db):
        """Rule A / #166: CS supports teams=both/A/B just like DS.
        Single-team CS shows just that team's button."""
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "CS",
            tab_name="CS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
            teams="A",
        )
        guild = _FakeGuild(TEST_GUILD_ID, [])
        view = sov.OfficerView(guild, owner_user_id=1, event_type="CS",
                               event_date="2026-05-18")
        labels = [getattr(c, "label", "") for c in view.children if hasattr(c, "label")]
        assert any("Set up Team A" in lab for lab in labels)
        assert not any("Set up Team B" in lab for lab in labels)


class TestTeamPlanButtons:
    """#239 — the 📋 Team A/B plan buttons sit alongside the existing
    🅰️/🅱️ Set up Team buttons, gated by the same `teams` setting.
    Label flips to '✅' when a plan is saved so officers can see at a
    glance whether the in-game commitment is captured."""

    def test_teams_both_shows_both_plan_buttons(self, seeded_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
            teams="both",
        )
        guild = _FakeGuild(TEST_GUILD_ID, [])
        view = sov.OfficerView(guild, owner_user_id=1, event_type="DS",
                               event_date="2026-05-21")
        labels = [getattr(c, "label", "") for c in view.children if hasattr(c, "label")]
        assert any("Team A plan" in lab for lab in labels)
        assert any("Team B plan" in lab for lab in labels)

    def test_teams_a_hides_team_b_plan_button(self, seeded_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
            teams="A",
        )
        guild = _FakeGuild(TEST_GUILD_ID, [])
        view = sov.OfficerView(guild, owner_user_id=1, event_type="DS",
                               event_date="2026-05-21")
        labels = [getattr(c, "label", "") for c in view.children if hasattr(c, "label")]
        assert any("Team A plan" in lab for lab in labels)
        assert not any("Team B plan" in lab for lab in labels)

    def test_saved_plan_flips_label_to_checkmark(self, seeded_db):
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
            teams="both",
        )
        config.save_storm_team_plan(
            TEST_GUILD_ID, "DS", "2026-05-21", "A",
            primaries=["11"], subs=[], saved_by_user_id=999,
        )
        guild = _FakeGuild(TEST_GUILD_ID, [])
        view = sov.OfficerView(guild, owner_user_id=1, event_type="DS",
                               event_date="2026-05-21")
        labels = [getattr(c, "label", "") for c in view.children if hasattr(c, "label")]
        # Team A has a saved plan → ✅ suffix.
        assert any(lab == "📋 Team A plan ✅" for lab in labels), labels
        # Team B has no plan → plain label.
        assert any(lab == "📋 Team B plan" for lab in labels), labels

    def test_cs_uses_same_ab_model_for_plan_buttons(self, seeded_db):
        """Regression guard against the 'CS is single-team' misread
        (#166): the team plan buttons branch on `teams` exactly like
        DS does, so a teams=both CS alliance gets both plan buttons."""
        import config
        config.save_storm_config(
            TEST_GUILD_ID, "CS",
            tab_name="CS Tab", mail_template="",
            timezone="America/New_York", log_channel_id=0,
            teams="both",
        )
        guild = _FakeGuild(TEST_GUILD_ID, [])
        view = sov.OfficerView(guild, owner_user_id=1, event_type="CS",
                               event_date="2026-05-21")
        labels = [getattr(c, "label", "") for c in view.children if hasattr(c, "label")]
        assert any("Team A plan" in lab for lab in labels)
        assert any("Team B plan" in lab for lab in labels)


class TestTeamPlanRosterPickerView:
    """Step 1 of the team-plan picker — pick up to 30 from the yes-pool."""

    def _make_parent(self):
        parent = MagicMock()
        parent.owner_user_id = 999
        parent.guild_id = TEST_GUILD_ID
        parent.event_type = "DS"
        parent.event_date = "2026-05-21"
        return parent

    def _make_candidates(self, n: int) -> list[dict]:
        return [
            {"name": f"M{i:02d}", "target_id": str(i)}
            for i in range(1, n + 1)
        ]

    def test_next_disabled_with_zero_picks(self):
        parent = self._make_parent()
        view = sov._TeamPlanRosterPickerView(
            parent, "A", self._make_candidates(5),
            other_team_claimed=[], prior_picks=[], prior_subs=[],
            prior_saved_at="",
        )
        next_btn = next(
            c for c in view.children
            if getattr(c, "label", "").startswith("Next ▶")
        )
        assert next_btn.disabled is True

    def test_next_enabled_with_one_pick(self):
        parent = self._make_parent()
        view = sov._TeamPlanRosterPickerView(
            parent, "A", self._make_candidates(5),
            other_team_claimed=[], prior_picks=["1"], prior_subs=[],
            prior_saved_at="",
        )
        next_btn = next(
            c for c in view.children
            if getattr(c, "label", "").startswith("Next ▶")
        )
        assert next_btn.disabled is False

    def test_next_disabled_when_over_thirty(self):
        parent = self._make_parent()
        # Pre-seed 31 prior picks — over the 30 cap.
        view = sov._TeamPlanRosterPickerView(
            parent, "A", self._make_candidates(40),
            other_team_claimed=[],
            prior_picks=[str(i) for i in range(1, 32)],
            prior_subs=[], prior_saved_at="",
        )
        next_btn = next(
            c for c in view.children
            if getattr(c, "label", "").startswith("Next ▶")
        )
        assert next_btn.disabled is True

    def test_pagination_appears_when_over_25(self):
        parent = self._make_parent()
        view = sov._TeamPlanRosterPickerView(
            parent, "A", self._make_candidates(30),
            other_team_claimed=[], prior_picks=[], prior_subs=[],
            prior_saved_at="",
        )
        labels = [getattr(c, "label", "") for c in view.children]
        assert any(lab and lab.startswith("Page ") for lab in labels)

    def test_clear_button_only_when_prior_plan(self):
        parent = self._make_parent()
        view_with = sov._TeamPlanRosterPickerView(
            parent, "A", self._make_candidates(5),
            other_team_claimed=[], prior_picks=["1"],
            prior_subs=[], prior_saved_at="2026-05-21T10:00:00+00:00",
        )
        view_without = sov._TeamPlanRosterPickerView(
            parent, "A", self._make_candidates(5),
            other_team_claimed=[], prior_picks=[],
            prior_subs=[], prior_saved_at="",
        )
        labels_with = [getattr(c, "label", "") for c in view_with.children]
        labels_without = [getattr(c, "label", "") for c in view_without.children]
        assert any("Clear plan" in lab for lab in labels_with)
        assert not any("Clear plan" in lab for lab in labels_without)

    async def test_owner_guard_blocks_non_owner(self):
        parent = self._make_parent()
        view = sov._TeamPlanRosterPickerView(
            parent, "A", self._make_candidates(5),
            other_team_claimed=[], prior_picks=[], prior_subs=[],
            prior_saved_at="",
        )
        inter = MagicMock()
        inter.user.id = 12345  # not the owner (999)
        inter.response.send_message = AsyncMock()
        ok = await view._guard_owner(inter)
        assert ok is False
        inter.response.send_message.assert_awaited_once()
        msg = inter.response.send_message.await_args.args[0]
        assert "Only the officer" in msg


class TestTeamPlanSubPickerView:
    """Step 2 of the team-plan picker — mark up to 10 subs from the 30."""

    def _make_parent(self):
        parent = MagicMock()
        parent.owner_user_id = 999
        parent.guild_id = TEST_GUILD_ID
        parent.event_type = "DS"
        parent.event_date = "2026-05-21"
        return parent

    def _make_chosen(self, n: int) -> list[dict]:
        return [
            {"name": f"M{i:02d}", "target_id": str(i)}
            for i in range(1, n + 1)
        ]

    def test_save_button_disabled_when_too_many_subs(self):
        parent = self._make_parent()
        # 15 chosen, 11 marked as sub (over the 10 cap).
        view = sov._TeamPlanSubPickerView(
            parent, "A", self._make_chosen(15),
            prior_subs=[str(i) for i in range(1, 12)],
        )
        save_btn = next(
            c for c in view.children
            if getattr(c, "label", "").startswith("💾 Save plan")
        )
        assert save_btn.disabled is True

    def test_save_button_enabled_with_ten_subs(self):
        parent = self._make_parent()
        view = sov._TeamPlanSubPickerView(
            parent, "A", self._make_chosen(30),
            prior_subs=[str(i) for i in range(21, 31)],  # exactly 10
        )
        save_btn = next(
            c for c in view.children
            if getattr(c, "label", "").startswith("💾 Save plan")
        )
        assert save_btn.disabled is False

    def test_label_reports_primary_sub_split(self):
        parent = self._make_parent()
        view = sov._TeamPlanSubPickerView(
            parent, "A", self._make_chosen(28),
            prior_subs=[str(i) for i in range(19, 29)],  # 10 subs
        )
        save_btn = next(
            c for c in view.children
            if getattr(c, "label", "").startswith("💾 Save plan")
        )
        # 28 chosen, 10 marked sub → 18 primary.
        assert "18 primary" in save_btn.label
        assert "10 sub" in save_btn.label

    def test_drops_prior_subs_not_in_chosen(self):
        """If the officer deselects a member in step 1 who was a prior
        sub, that member shouldn't carry over to step 2."""
        parent = self._make_parent()
        chosen = self._make_chosen(5)  # M01..M05 only
        view = sov._TeamPlanSubPickerView(
            parent, "A", chosen,
            prior_subs=["1", "2", "99"],  # 99 not in chosen
        )
        assert "99" not in view.selected_sub_ids
        assert set(view.selected_sub_ids) == {"1", "2"}

    async def test_owner_guard_blocks_non_owner(self):
        parent = self._make_parent()
        view = sov._TeamPlanSubPickerView(
            parent, "A", self._make_chosen(5), prior_subs=[],
        )
        inter = MagicMock()
        inter.user.id = 12345
        inter.response.send_message = AsyncMock()
        ok = await view._guard_owner(inter)
        assert ok is False
        inter.response.send_message.assert_awaited_once()


class TestOfficerViewTimeout:
    """The OfficerView is posted publicly so multiple leadership members
    can use it as an audit trail. Without `on_timeout`, the buttons
    silently 404 after 15 minutes with 'Interaction failed' and no
    signal. `on_timeout` strips the buttons + appends the canonical
    timeout notice via `wizard_registry.expire_view_message`."""

    async def test_on_timeout_calls_expire_view_message(self, seeded_db):
        guild = _FakeGuild(TEST_GUILD_ID, [])
        view = sov.OfficerView(guild, owner_user_id=1, event_type="DS",
                               event_date="2026-05-18")
        view.message = MagicMock()
        with patch("wizard_registry.expire_view_message", new=AsyncMock()) as ex:
            await view.on_timeout()
        ex.assert_awaited_once()
        # Command hint points at the hub button — event-type aware so DS
        # officers see the DS hub and vice versa.
        kwargs = ex.await_args.kwargs
        hint = kwargs.get("command_hint", "")
        assert "/desertstorm" in hint
        assert "View sign-ups" in hint
