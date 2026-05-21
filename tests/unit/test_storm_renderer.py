"""
Tests for storm_renderer.py (#132 PNG part).

The renderer's job is "produce a PNG of any shape we hand it without
crashing." Pixel-perfect output is not the contract — that's a
visual review concern. These tests pin behaviour:
  * Output is a valid PNG byte-string (header check).
  * Empty / minimal rosters don't crash.
  * Paired subs surface in the layout.
  * The session → RosterData conversion is faithful.
"""

import io

import pytest


# Skip cleanly if Pillow isn't installed — the rest of the suite stays
# green even on environments that haven't pulled the new dep yet.
pytest.importorskip("PIL")

import storm_renderer as sr  # noqa: E402
import storm_strategy as ss  # noqa: E402


def test_render_returns_png_bytes():
    roster = sr.RosterData(
        title="Desert Storm — Standard — Team A — 2026-05-18",
        zones=[
            sr.RosterZone(name="Power Tower",  max_players=4,
                          members=["Alice", "Bob"]),
            sr.RosterZone(name="Nuclear Silo", max_players=4,
                          members=["Carol"]),
        ],
        subs=["Dan", "Erin"],
    )
    png = sr.render(roster)
    assert isinstance(png, bytes)
    # PNG magic header — 89 50 4E 47 0D 0A 1A 0A
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_empty_zones_does_not_crash():
    roster = sr.RosterData(
        title="Empty roster",
        zones=[sr.RosterZone(name="Power Tower", max_players=4, members=[])],
    )
    png = sr.render(roster)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_no_zones_no_subs_no_roles():
    roster = sr.RosterData(title="Wholly empty", zones=[])
    png = sr.render(roster)
    # Still produces a (minimal-height) image, not an exception.
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_includes_paired_subs():
    # Map-based renderer (#140): paired-mode rosters render the sub
    # inline with the primary inside each zone's member-list pill,
    # AND switch the Subs column to the two-column pairs table. We
    # can't easily assert against pixels, but the path must produce
    # a valid PNG without crashing.
    roster = sr.RosterData(
        title="Paired demo",
        event_type="DS",
        zones=[sr.RosterZone(
            name="Nuclear Silo", canonical_zone="Nuclear Silo",
            max_players=2, members=["Alice", "Bob"],
        )],
        paired_subs={"Alice": "Carol"},
    )
    png = sr.render(roster)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_closed_zero_cap_zone_survives():
    """#226: zones with `max_for_phase=0` still appear in `roster.zones`
    so the canonical layout slot draws as an empty pill. Officers use
    cap=0 to communicate 'do not assign here, we're letting points
    build,' so the visual handed to members needs every zone slot
    intact even when the zone is closed."""
    # Phase-aware preset where Field Hospital III is closed across
    # both stages (max_phase1 / max_phase2 default to 0).
    closed_zone = sr.RosterZone(
        name="Stage 1 — Field Hospital III",
        canonical_zone="Field Hospital III",
        max_players=0,
        members=[],
        phase=1,
    )
    closed_zone_p2 = sr.RosterZone(
        name="Stage 2 — Field Hospital III",
        canonical_zone="Field Hospital III",
        max_players=0,
        members=[],
        phase=2,
    )
    open_zone = sr.RosterZone(
        name="Stage 1 — Nuclear Silo",
        canonical_zone="Nuclear Silo",
        max_players=4,
        members=["Alice", "Bob"],
        phase=1,
    )
    roster = sr.RosterData(
        title="Closed zone demo",
        event_type="DS",
        zones=[closed_zone, closed_zone_p2, open_zone],
    )
    png = sr.render(roster)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


# ── roster_from_session conversion ───────────────────────────────────────────


class _FakeMember:
    def __init__(self, name: str, key: str, power: int | None = 0,
                 discord_id: str = "", not_on_discord: bool = False):
        self.name = name
        self.key = key
        self.power = power
        self.discord_id = discord_id
        self.not_on_discord = not_on_discord

    @property
    def as_dict(self):
        return {
            "key": self.key, "name": self.name, "discord_id": self.discord_id,
            "power": self.power, "not_on_discord": self.not_on_discord,
        }


def _make_session(team="A", *, members=None, paired_subs=None, sub_mode="pool"):
    import storm_roster_builder as srb
    preset_zones = [
        ss.ZoneRow(zone="Power Tower",  max_players=2, min_power_a=300_000_000, min_power_b=180_000_000),
        ss.ZoneRow(zone="Nuclear Silo", max_players=2, min_power_a=250_000_000, min_power_b=150_000_000),
    ]
    preset = ss.PresetBuffer(name="Standard", event_type="DS", zones=preset_zones)
    sess = srb.RosterBuilderSession(
        guild_id=1, user_id=42, event_type="DS", team=team,
        preset=preset, members=members or {}, per_member_rules=[],
        power_band_rules=[], event_date="2026-05-18", sub_mode=sub_mode,
    )
    if paired_subs:
        sess.paired_subs = paired_subs
    return sess


class TestRosterFromSession:
    def test_title_includes_team_and_date(self):
        sess = _make_session(team="A")
        data = sr.roster_from_session(sess)
        assert "Team A" in data.title
        # Date is now rendered as the human form (#145) — Monday May 18 2026.
        assert "May" in data.title
        assert "18" in data.title
        assert "2026" in data.title
        assert "Standard" in data.title

    def test_includes_zones_and_capacities(self):
        sess = _make_session(team="A")
        data = sr.roster_from_session(sess)
        names = [z.name for z in data.zones]
        assert "Power Tower" in names
        assert "Nuclear Silo" in names
        # Capacities preserved from the preset.
        pt = next(z for z in data.zones if z.name == "Power Tower")
        assert pt.max_players == 2

    def test_paired_subs_carried_through(self):
        members = {
            "1001": _FakeMember("Alice", "1001").as_dict,
            "1002": _FakeMember("Bob",   "1002").as_dict,
        }
        sess = _make_session(
            team="A", members=members,
            paired_subs={"1001": "1002"}, sub_mode="paired",
        )
        sess.assignments["Power Tower"].append("1001")
        data = sr.roster_from_session(sess)
        # Paired map keyed by display name (renderer-friendly), value
        # is the partner's display name.
        assert data.paired_subs.get("Alice") == "Bob"

    def test_closed_phase_blocks_still_appear_in_zones(self):
        """#226: a zone with `max_for_phase = 0` for every phase still
        appears in `roster.zones` as a `RosterZone` with empty members.
        Without this, the canonical PNG layout slot for the closed
        zone goes unused and the visual handed to members has an
        empty hole where the zone should be."""
        import storm_strategy as ss
        import storm_roster_builder as srb
        members = {
            "1001": _FakeMember("Alice", "1001").as_dict,
        }
        # Phase-aware preset where Field Hospital III is closed for
        # every phase (max_phase1 + max_phase2 both default to 0).
        zones = [
            ss.ZoneRow(zone="Info Center", max_players=0,
                       max_phase1=2, max_phase2=1,
                       min_power_a=200_000_000, min_power_b=100_000_000),
            ss.ZoneRow(zone="Field Hospital III", max_players=0,
                       max_phase1=0, max_phase2=0,
                       min_power_a=0, min_power_b=0),
        ]
        preset = ss.PresetBuffer(name="Closed", event_type="DS",
                                 zones=zones, phase_count=2)
        sess = srb.RosterBuilderSession(
            guild_id=1, user_id=42, event_type="DS",
            team="A", preset=preset, members=members,
            per_member_rules=[], power_band_rules=[], sub_mode="pool",
        )
        sess.event_date = "2026-05-18"
        data = sr.roster_from_session(sess)
        # Field Hospital III appears as a `canonical_zone` key in the
        # roster.zones list, with cap=0 and no members, for every
        # phase the preset declares.
        fh3_blocks = [
            z for z in data.zones if z.canonical_zone == "Field Hospital III"
        ]
        assert len(fh3_blocks) == 2
        for b in fh3_blocks:
            assert b.max_players == 0
            assert b.members == []

    def test_pool_subs_carried_through(self):
        members = {
            "1003": _FakeMember("Carol", "1003").as_dict,
            "1004": _FakeMember("Dan",   "1004").as_dict,
        }
        sess = _make_session(team="A", members=members, sub_mode="pool")
        sess.subs = ["1003", "1004"]
        data = sr.roster_from_session(sess)
        assert "Carol" in data.subs
        assert "Dan"   in data.subs

    def test_cs_faction_in_title(self):
        import storm_roster_builder as srb
        preset = ss.PresetBuffer(
            name="Rulebringers Plan", event_type="CS", faction="Rulebringers",
            zones=[ss.ZoneRow(zone="Z", max_players=4)],
        )
        sess = srb.RosterBuilderSession(
            guild_id=1, user_id=42, event_type="CS", team="",
            preset=preset, members={}, per_member_rules=[],
            power_band_rules=[], event_date="2026-05-18",
        )
        data = sr.roster_from_session(sess)
        assert "Rulebringers" in data.title


class TestRendererPillowMissing:
    """Audit asked for explicit coverage of the `RuntimeError` path.
    Pillow is installed in CI so the test mocks the import failure."""

    def test_render_raises_runtime_error_when_pillow_missing(self):
        import sys
        from unittest.mock import patch
        # Map PIL + every submodule we import inside render to None.
        # Pillow's submodules are imported lazily in `render()`; patching
        # `PIL` alone is insufficient because `from PIL import X` resolves
        # via `sys.modules["PIL"].X`. Patch the parent + the leaves.
        broken = {
            "PIL": None,
            "PIL.Image": None,
            "PIL.ImageDraw": None,
            "PIL.ImageFont": None,
        }
        with patch.dict(sys.modules, broken):
            roster = sr.RosterData(title="t", zones=[])
            with pytest.raises(RuntimeError, match="Pillow isn't installed"):
                sr.render(roster)


class TestMapBasedRender:
    """#140 — map-based renderer dispatching on event_type. Pin the
    new public-contract behaviour (DS vs CS dispatch, phase-aware
    grouping, missing-zone resilience) so a future tweak can't
    silently regress what the alliance lead's SVG mock specified."""

    def test_ds_render_produces_png(self):
        roster = sr.RosterData(
            title="DS demo", event_type="DS",
            preset_name="Standard", team_label="Team A",
            event_date_label="May 18 2026",
            zones=[
                sr.RosterZone(name="Nuclear Silo",
                              canonical_zone="Nuclear Silo",
                              max_players=4, members=["Alice", "Bob"]),
                sr.RosterZone(name="Info Center",
                              canonical_zone="Info Center",
                              max_players=4, members=["Carol"]),
            ],
            subs=["Dan"],
        )
        png = sr.render(roster)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        # Sanity: the DS layout canvas is wider than tall after
        # SCALE-up — distinguishable from a CS render.
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        assert img.size[0] > img.size[1]

    def test_cs_render_produces_png_with_taller_canvas(self):
        roster = sr.RosterData(
            title="CS demo", event_type="CS",
            preset_name="Rulebringers Plan", team_label="Rulebringers",
            event_date_label="May 18 2026",
            zones=[
                sr.RosterZone(name="Power Tower",
                              canonical_zone="Power Tower",
                              max_players=4, members=["Alice"]),
                sr.RosterZone(name="Virus Lab",
                              canonical_zone="Virus Lab",
                              max_players=4, members=["Bob"]),
            ],
        )
        png = sr.render(roster)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        # CS canvas is taller-relative to DS because the 3-stage
        # layout needs the extra vertical room.
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        # CS svg post-#227 is 1235.67 wide x 938.44 tall — aspect
        # ratio ≈ 1.32. DS is 1107.6 x 764.3 — aspect ≈ 1.45. CS
        # render must still be the taller-aspect one.
        assert img.size[1] / img.size[0] > 0.7

    def test_render_missing_zone_doesnt_crash(self):
        # Unknown / typo canonical zones get skipped silently — render
        # produces a PNG with everything ELSE intact. Without this
        # the old text-canvas would print whatever string came in; the
        # map renderer drops zones it can't place.
        roster = sr.RosterData(
            title="Typo zone", event_type="DS",
            zones=[
                sr.RosterZone(name="Misspelled Zone",
                              canonical_zone="Misspelled Zone",
                              max_players=4, members=["Alice"]),
                sr.RosterZone(name="Nuclear Silo",
                              canonical_zone="Nuclear Silo",
                              max_players=4, members=["Bob"]),
            ],
        )
        png = sr.render(roster)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_phase_aware_groups_by_canonical_zone(self):
        # A 2-phase preset sends one RosterZone per (zone, phase); the
        # renderer groups them so the map slot for Info Center renders
        # once with both phases' members stacked inside the pill.
        roster = sr.RosterData(
            title="Phased demo", event_type="DS",
            phase_count=2,
            zones=[
                sr.RosterZone(name="Stage 1 — Info Center",
                              canonical_zone="Info Center",
                              max_players=4, members=["Alice", "Bob"],
                              phase=1),
                sr.RosterZone(name="Stage 2 — Info Center",
                              canonical_zone="Info Center",
                              max_players=2, members=["Carol"],
                              phase=2),
            ],
        )
        png = sr.render(roster)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_three_phase_cs_render(self):
        roster = sr.RosterData(
            title="3-phase CS", event_type="CS",
            phase_count=3, team_label="Rulebringers",
            zones=[
                sr.RosterZone(name="Stage 1 — Power Tower",
                              canonical_zone="Power Tower",
                              max_players=4, members=["A", "B"],
                              phase=1),
                sr.RosterZone(name="Stage 2 — Power Tower",
                              canonical_zone="Power Tower",
                              max_players=2, members=["C"],
                              phase=2),
                sr.RosterZone(name="Stage 3 — Power Tower",
                              canonical_zone="Power Tower",
                              max_players=2, members=["D"],
                              phase=3),
            ],
        )
        png = sr.render(roster)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_missing_icon_falls_back_to_placeholder(self):
        # The renderer must draw a grey placeholder circle (not crash)
        # for any zone whose icon entry is `None` or whose file is
        # absent on disk. Patch the dict to simulate a zone that
        # shipped before its art did. (All DS slots have art today —
        # this is a defensive guard for the next zone the game adds.)
        from unittest.mock import patch
        patched = dict(sr._DS_ICON_FILES)
        patched["Arsenal"] = None
        patched["Mercenary Factory"] = None
        with patch.object(sr, "_DS_ICON_FILES", patched):
            roster = sr.RosterData(
                title="Missing icon", event_type="DS",
                zones=[
                    sr.RosterZone(name="Arsenal", canonical_zone="Arsenal",
                                  max_players=4, members=["Alice"]),
                    sr.RosterZone(name="Mercenary Factory",
                                  canonical_zone="Mercenary Factory",
                                  max_players=4, members=["Bob"]),
                ],
            )
            png = sr.render(roster)
            assert png[:8] == b"\x89PNG\r\n\x1a\n"

    def test_event_type_defaults_to_ds_layout(self):
        # Unknown / empty event_type falls back to DS — protects
        # against a stale RosterData that didn't get the new field
        # plumbed through.
        roster = sr.RosterData(title="Default", zones=[])
        png = sr.render(roster)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


class TestFontFallbackForNonLatinNames:
    """#236: Inter covers Latin / Cyrillic / Greek; CJK + Arabic
    player names need bundled Noto fallback fonts. The script-detection
    helper picks the right family per string."""

    def test_latin_name_keeps_inter(self):
        assert sr._script_family_for_text("Alice") == "inter"
        assert sr._script_family_for_text("Member 5") == "inter"
        assert sr._script_family_for_text("José") == "inter"  # Spanish
        assert sr._script_family_for_text("João") == "inter"  # Portuguese
        assert sr._script_family_for_text("Müller") == "inter"  # German
        assert sr._script_family_for_text("") == "inter"  # empty edge

    def test_cyrillic_name_keeps_inter(self):
        # Inter covers Cyrillic — Russian player names render on Inter
        # directly, no fallback needed.
        assert sr._script_family_for_text("Алексей") == "inter"

    def test_korean_name_picks_cjk(self):
        # Hangul Syllables block.
        assert sr._script_family_for_text("김민준") == "cjk"
        assert sr._script_family_for_text("이서연") == "cjk"

    def test_japanese_name_picks_cjk(self):
        # Hiragana + Katakana + Kanji.
        assert sr._script_family_for_text("たなか") == "cjk"
        assert sr._script_family_for_text("タナカ") == "cjk"
        assert sr._script_family_for_text("田中") == "cjk"

    def test_chinese_name_picks_cjk(self):
        # CJK Unified Ideographs.
        assert sr._script_family_for_text("王伟") == "cjk"
        assert sr._script_family_for_text("张三") == "cjk"

    def test_arabic_name_picks_arabic(self):
        assert sr._script_family_for_text("محمد") == "arabic"
        assert sr._script_family_for_text("علي") == "arabic"

    def test_mixed_latin_and_cjk_picks_cjk(self):
        # Mixed strings (e.g. "한국 Member" or "Member 김") need the CJK
        # font for the whole string so the non-Latin characters don't
        # show as .notdef boxes. CJK fonts also render Latin (just with
        # a slightly different aesthetic).
        assert sr._script_family_for_text("한국 Member") == "cjk"
        assert sr._script_family_for_text("Member 김") == "cjk"

    def test_font_loader_returns_a_font(self):
        # Smoke test the actual loader so a font-file-missing scenario
        # would surface here rather than crashing render().
        from PIL.ImageFont import FreeTypeFont
        f = sr._font_for_text("김민준", 16)
        # Either the Noto fallback loaded, or it gracefully fell back
        # to Inter (the catch path inside `_font_for_text`).
        assert f is not None
        # The Inter latin font also returns; both should be FreeTypeFont
        # or load_default (PIL's fallback for missing files).
        f2 = sr._font_for_text("Alice", 16)
        assert f2 is not None

    def test_render_with_mixed_script_names_doesnt_crash(self):
        """Smoke: a roster with Korean, Japanese, Chinese, Arabic, and
        Latin names renders without raising. The exact pixels aren't
        tested (visual review concern), but PIL must accept every
        glyph via the right font family."""
        roster = sr.RosterData(
            title="Desert Storm — Mixed",
            zones=[
                sr.RosterZone(name="Power Tower", max_players=4, members=[
                    "Alice", "김민준", "田中", "محمد",
                ]),
                sr.RosterZone(name="Nuclear Silo", max_players=4, members=[
                    "José", "王伟", "타나카", "علي",
                ]),
            ],
            subs=["Алексей", "한국 Member"],
            event_type="DS",
        )
        png = sr.render(roster)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"


class TestRosterFromSessionStructuredFields:
    """#140 plumbs new structured fields (event_type, preset_name,
    team_label, event_date_label, phase_count) so the map renderer
    doesn't have to parse the legacy `title` string."""

    def test_ds_session_populates_team_label(self):
        sess = _make_session(team="A")
        data = sr.roster_from_session(sess)
        assert data.event_type == "DS"
        assert data.team_label == "Team A"
        assert data.preset_name == "Standard"

    def test_cs_session_populates_faction_label(self):
        import storm_roster_builder as srb
        preset = ss.PresetBuffer(
            name="Plan", event_type="CS", faction="Rulebringers",
            zones=[ss.ZoneRow(zone="Virus Lab", max_players=4)],
        )
        sess = srb.RosterBuilderSession(
            guild_id=1, user_id=42, event_type="CS", team="",
            preset=preset, members={}, per_member_rules=[],
            power_band_rules=[], event_date="2026-05-18",
        )
        data = sr.roster_from_session(sess)
        assert data.event_type == "CS"
        assert data.team_label == "Rulebringers"

    def test_phase_count_carried_through(self):
        import storm_roster_builder as srb
        preset = ss.PresetBuffer(
            name="Phased", event_type="DS", phase_count=2,
            zones=[ss.ZoneRow(zone="Info Center", max_phase1=4, max_phase2=2)],
        )
        sess = srb.RosterBuilderSession(
            guild_id=1, user_id=42, event_type="DS", team="A",
            preset=preset, members={}, per_member_rules=[],
            power_band_rules=[],
        )
        data = sr.roster_from_session(sess)
        assert data.phase_count == 2
        # Each phase block carries phase + canonical_zone for the
        # renderer's grouping pass.
        ic_blocks = [z for z in data.zones if z.canonical_zone == "Info Center"]
        assert len(ic_blocks) == 2
        assert {b.phase for b in ic_blocks} == {1, 2}
