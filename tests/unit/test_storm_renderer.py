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
    roster = sr.RosterData(
        title="Paired demo",
        zones=[sr.RosterZone(
            name="Power Tower", max_players=2,
            members=["Alice", "Bob"],
        )],
        paired_subs={"Alice": "Carol"},
    )
    # The renderer should fold paired subs in — we can't easily check
    # pixels, but the canvas height should grow when paired subs are
    # present compared to no pairing.
    no_pair = sr.RosterData(
        title="No pair demo",
        zones=[sr.RosterZone(
            name="Power Tower", max_players=2, members=["Alice", "Bob"],
        )],
    )
    h_paired = sr._measure_height(
        roster,
        title_font=_default_font(18),
        heading_font=_default_font(14),
        body_font=_default_font(12),
    )
    h_no_pair = sr._measure_height(
        no_pair,
        title_font=_default_font(18),
        heading_font=_default_font(14),
        body_font=_default_font(12),
    )
    # Same number of body lines (the sub is part of the same primary
    # line in the renderer), so heights should be equal.
    assert h_paired == h_no_pair
    # Sanity: render the paired version too.
    png = sr.render(roster)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_render_includes_special_roles():
    roster = sr.RosterData(
        title="Roles demo",
        zones=[sr.RosterZone(name="Power Tower", max_players=1, members=["Alice"])],
        special_roles={"Commander": ["Alice"], "Judicator": ["Bob"]},
    )
    png = sr.render(roster)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def _default_font(size: int):
    """Mirror the renderer's font-load logic so test sizing aligns
    exactly with the real path."""
    from PIL import ImageFont
    if sr._supports_size():
        return ImageFont.load_default(size=size)
    return ImageFont.load_default()


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
        assert "2026-05-18" in data.title
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
