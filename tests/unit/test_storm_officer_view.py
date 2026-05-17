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

    def test_submit_disabled_until_both_selects_chosen(self, seeded_db):
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
        view.selected_member = "Alice"
        view.selected_vote = "a"
        interaction = self._fake_interaction(user_id=parent.owner_user_id)
        with patch(
            "config.record_storm_vote", return_value=True,
        ) as record:
            await view._on_submit(interaction)
        record.assert_called_once()
        kwargs = record.call_args.kwargs
        assert kwargs["target_member_id"] == "Alice"
        assert kwargs["vote"] == "a"
        assert kwargs["is_on_behalf"] is True
        parent.refresh_buckets.assert_awaited()


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
        # Command hint points at the parent-group `signups` subcommand —
        # event-type aware so DS officers see the DS path and vice versa.
        kwargs = ex.await_args.kwargs
        assert kwargs.get("command_hint") == "/desertstorm signups"
