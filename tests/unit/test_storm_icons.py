"""Tests for storm_icons (#158 + #177).

Pure-logic coverage of `zone_emoji_prefix` + its stem-stripping
helper, plus the bot-startup `refresh_zone_emoji_ids` populator.
The render path returns plain `<:name:id>` markup (Discord renders
it client-side); the refresh path mocks the bot's
`fetch_application_emojis` so no live Discord call is made."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import storm_icons as si


class _FakeEmoji:
    """Minimal stand-in for `discord.Emoji` — only `name` + `id`
    attributes are touched by `refresh_zone_emoji_ids`. Using a
    plain class instead of `MagicMock` because `MagicMock(name=...)`
    binds the mock's own repr-name rather than a fake `.name`
    attribute."""

    def __init__(self, name: str, id: int):
        self.name = name
        self.id = id


class TestZoneStem:
    def test_strips_trailing_roman_numeral(self):
        assert si._zone_stem("Field Hospital I") == "field_hospital"
        assert si._zone_stem("Field Hospital IV") == "field_hospital"

    def test_strips_trailing_arabic_numeral(self):
        assert si._zone_stem("Data Center 1") == "data_center"
        assert si._zone_stem("Sample Warehouse 4") == "sample_warehouse"

    def test_unnumbered_name_passes_through(self):
        assert si._zone_stem("Nuclear Silo") == "nuclear_silo"
        assert si._zone_stem("Power Tower") == "power_tower"

    def test_does_not_strip_mid_word_digits(self):
        # Sanity: "Data Center" shouldn't lose anything (no trailing
        # numeral). The Center→2-style match only fires on whitespace+
        # numeral at the END.
        assert si._zone_stem("Data Center") == "data_center"

    def test_empty_returns_empty(self):
        assert si._zone_stem("") == ""


class TestZoneEmojiPrefixEmptyDict:
    """When `ZONE_EMOJI_IDS` is empty (the shipped default before the
    upload script runs), every call returns "" so renderers fall
    through to plain text. Ship-safe for every storm surface."""

    def test_empty_dict_returns_blank(self):
        with patch.object(si, "ZONE_EMOJI_IDS", {}):
            assert si.zone_emoji_prefix("Power Tower") == ""
            assert si.zone_emoji_prefix("Field Hospital III") == ""


class TestZoneEmojiPrefixWithMapping:
    """When the emoji dict carries an entry, the prefix renders as
    `<:stem:id> ` — trailing space included so callers can concatenate
    `prefix + zone_name` and get correct spacing whether the icon is
    present or absent."""

    def _patched_dict(self):
        return {
            "nuclear_silo":   123,
            "field_hospital": 456,
            "data_center":    789,
        }

    def test_known_stem_returns_emoji_markup(self):
        with patch.object(si, "ZONE_EMOJI_IDS", self._patched_dict()):
            assert si.zone_emoji_prefix("Nuclear Silo") == "<:nuclear_silo:123> "

    def test_numbered_variants_share_one_emoji(self):
        with patch.object(si, "ZONE_EMOJI_IDS", self._patched_dict()):
            # All four Field Hospitals route to the same icon.
            assert si.zone_emoji_prefix("Field Hospital I") == "<:field_hospital:456> "
            assert si.zone_emoji_prefix("Field Hospital II") == "<:field_hospital:456> "
            assert si.zone_emoji_prefix("Field Hospital III") == "<:field_hospital:456> "
            assert si.zone_emoji_prefix("Field Hospital IV") == "<:field_hospital:456> "

    def test_arabic_numbered_cs_zones_share_one_emoji(self):
        with patch.object(si, "ZONE_EMOJI_IDS", self._patched_dict()):
            assert si.zone_emoji_prefix("Data Center 1") == "<:data_center:789> "
            assert si.zone_emoji_prefix("Data Center 2") == "<:data_center:789> "

    def test_unmapped_zone_returns_blank(self):
        """A zone whose stem isn't in the dict (e.g. its emoji hasn't
        been uploaded yet, or a custom zone an alliance added that the
        bot doesn't ship art for) falls through to plain text."""
        with patch.object(si, "ZONE_EMOJI_IDS", self._patched_dict()):
            # `mercenary_factory` isn't in `_patched_dict`.
            assert si.zone_emoji_prefix("Mercenary Factory") == ""
            assert si.zone_emoji_prefix("Custom Bunker") == ""

    def test_typo_zone_returns_blank(self):
        with patch.object(si, "ZONE_EMOJI_IDS", self._patched_dict()):
            assert si.zone_emoji_prefix("Powr Tower") == ""

    def test_empty_input_returns_blank(self):
        with patch.object(si, "ZONE_EMOJI_IDS", self._patched_dict()):
            assert si.zone_emoji_prefix("") == ""
            assert si.zone_emoji_prefix(None) == ""  # type: ignore[arg-type]


class TestRefreshZoneEmojiIds:
    """#177 / Option C: the bot reads its own Application Emojis at
    `on_ready` instead of carrying hardcoded IDs in source. Each
    environment (dev, prod) ships with its own Application + emoji
    set; the bot resolves the right one from the token it boots
    with. Source carries zero IDs and zero env-switch logic."""

    @pytest.mark.asyncio
    async def test_populates_dict_from_bot_application_emojis(self):
        bot = MagicMock()
        bot.fetch_application_emojis = AsyncMock(return_value=[
            _FakeEmoji("nuclear_silo", 111),
            _FakeEmoji("power_tower", 222),
            _FakeEmoji("field_hospital", 333),
        ])
        with patch.object(si, "ZONE_EMOJI_IDS", {}):
            count = await si.refresh_zone_emoji_ids(bot)
            assert count == 3
            assert si.ZONE_EMOJI_IDS == {
                "nuclear_silo": 111,
                "power_tower": 222,
                "field_hospital": 333,
            }

    @pytest.mark.asyncio
    async def test_idempotent_clears_and_rebuilds(self):
        """Re-running picks up the current state, dropping stale
        entries that no longer exist on Discord. Officers can re-
        upload after deleting an old icon and the next reconnect
        cleans up automatically."""
        bot = MagicMock()
        with patch.object(si, "ZONE_EMOJI_IDS", {}):
            bot.fetch_application_emojis = AsyncMock(return_value=[
                _FakeEmoji("old_icon", 999),
            ])
            await si.refresh_zone_emoji_ids(bot)
            assert "old_icon" in si.ZONE_EMOJI_IDS

            bot.fetch_application_emojis = AsyncMock(return_value=[
                _FakeEmoji("new_icon", 1),
            ])
            await si.refresh_zone_emoji_ids(bot)
            assert "old_icon" not in si.ZONE_EMOJI_IDS
            assert si.ZONE_EMOJI_IDS == {"new_icon": 1}

    @pytest.mark.asyncio
    async def test_fetch_failure_preserves_prior_dict(self):
        """Transient network blip or Discord 5xx must not clobber the
        last-known-good IDs. The render path keeps working off whatever
        was loaded the previous call."""
        bot = MagicMock()
        bot.fetch_application_emojis = AsyncMock(
            side_effect=RuntimeError("Discord 503"),
        )
        with patch.object(si, "ZONE_EMOJI_IDS", {"prior": 1}):
            count = await si.refresh_zone_emoji_ids(bot)
            assert count == 0
            # Prior entry preserved — .clear() didn't run.
            assert si.ZONE_EMOJI_IDS == {"prior": 1}

    @pytest.mark.asyncio
    async def test_no_emojis_logs_warning(self, caplog):
        """A bot whose Application has zero emojis (e.g. fresh env
        before the upload script runs) populates an empty dict, which
        is the same as the shipped default."""
        import logging
        bot = MagicMock()
        bot.fetch_application_emojis = AsyncMock(return_value=[])
        with patch.object(si, "ZONE_EMOJI_IDS", {}):
            with caplog.at_level(logging.WARNING, logger="storm_icons"):
                count = await si.refresh_zone_emoji_ids(bot)
            assert count == 0
            assert si.ZONE_EMOJI_IDS == {}
            assert any(
                "no application emojis registered" in rec.message
                for rec in caplog.records
            )

    @pytest.mark.asyncio
    async def test_zone_emoji_prefix_uses_runtime_populated_dict(self):
        """End-to-end: refresh from a fake bot, then exercise the
        render helper against the populated dict. Confirms the upload
        → refresh → render chain hands off correctly."""
        bot = MagicMock()
        bot.fetch_application_emojis = AsyncMock(return_value=[
            _FakeEmoji("nuclear_silo", 555),
        ])
        with patch.object(si, "ZONE_EMOJI_IDS", {}):
            await si.refresh_zone_emoji_ids(bot)
            assert si.zone_emoji_prefix("Nuclear Silo") == "<:nuclear_silo:555> "
