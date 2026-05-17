"""Tests for storm_icons (#158).

Pure-logic coverage of `zone_emoji_prefix` and its stem-stripping
helper. No Discord API calls — the function returns plain `<:name:id>`
markup, which Discord renders client-side."""

from unittest.mock import patch

import storm_icons as si


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
        """Floaters / Arsenal / Mercenary Factory ship without icons —
        their stems are intentionally absent until the art is drawn.
        Renderers fall through to plain text."""
        with patch.object(si, "ZONE_EMOJI_IDS", self._patched_dict()):
            assert si.zone_emoji_prefix("Floaters") == ""
            assert si.zone_emoji_prefix("Arsenal") == ""

    def test_typo_zone_returns_blank(self):
        with patch.object(si, "ZONE_EMOJI_IDS", self._patched_dict()):
            assert si.zone_emoji_prefix("Powr Tower") == ""

    def test_empty_input_returns_blank(self):
        with patch.object(si, "ZONE_EMOJI_IDS", self._patched_dict()):
            assert si.zone_emoji_prefix("") == ""
            assert si.zone_emoji_prefix(None) == ""  # type: ignore[arg-type]
