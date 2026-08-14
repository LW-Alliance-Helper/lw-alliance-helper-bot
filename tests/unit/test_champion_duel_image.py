"""The prediction card.

A render is hard to assert on pixel by pixel and not worth it. What these
cover is the part that can be *wrong* rather than ugly: that the card shows the
line-up the prediction was actually computed from, that it never rounds a
probability up into a certainty, that a name in a non-Latin script or a missing
asset doesn't take the render down, and — since the card became a compositor
over a designed template — that the layout it is placed against still describes
the artwork it is placed on.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

import champion_duel_db as db
import champion_duel_image as img
import champion_duel_predict as cdp

ACTOR = {"discord_user_id": "111", "discord_name": "Kevin", "guild_id": "999"}


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return None


def _player(name, server, powers, source="observed", orders=()):
    db.import_registrants([{"name": name, "group": "M", "rank": 1, "server": server}])
    rid = db.resolve_registrant(name, server=server)["id"]
    for slot, (squad_type, power) in enumerate(
        zip(("Tank", "Missile", "Aircraft"), powers), start=1
    ):
        db.set_squad(rid, slot, squad_type=squad_type, power=power, actor=ACTOR, source=source)
    for order in orders:
        db.add_order(rid, list(order), actor=ACTOR)
    return db.get_player(name, server=server, include_scouting=True)


# ── The order shown is the order predicted ────────────────────────────────────


def test_lineup_shown_is_the_order_the_prediction_used(cd_db):
    """Deployment order decides which squad meets which, and the counter
    triangle means it can outweigh power. A card showing the natural slot order
    beside a probability computed from a different one invites the reader to
    work out why the number looks wrong and reach a conclusion that is also
    wrong."""
    player = _player(
        "Ravenshade",
        "738",
        (34_800_000, 31_500_000, 27_200_000),
        orders=[("Missile", "Tank", "Aircraft")],
    )
    side = cdp.build_side(player)
    lineup, from_sightings = side.likely_order()

    assert from_sightings is True
    assert [t for _, t in lineup] == ["Missile", "Tank", "Aircraft"]
    # The power travels with its type, not with the slot it used to sit in.
    assert lineup[0] == (31_500_000, "Missile")


def test_without_sightings_the_natural_order_is_shown(cd_db):
    player = _player("NightOwl", "738", (34_000_000, 30_000_000, 26_000_000))
    lineup, from_sightings = cdp.build_side(player).likely_order()
    assert from_sightings is False
    assert [t for _, t in lineup] == ["Tank", "Missile", "Aircraft"]


def test_a_tie_shows_the_most_recent_sighting(cd_db):
    """Seen once in each of two orders, the card shows the newer one.

    The question a prediction answers is which order they will have set when
    the two meet, so the later observation is the better evidence. Only the
    display collapses to one — the prediction still averages over both.
    """
    player = _player(
        "Ravenshade",
        "738",
        (34_000_000, 30_000_000, 26_000_000),
        orders=[("Tank", "Missile", "Aircraft"), ("Aircraft", "Tank", "Missile")],
    )
    side = cdp.build_side(player)
    lineup, _ = side.likely_order()
    assert [t for _, t in lineup] == ["Aircraft", "Tank", "Missile"]
    assert len(side.orders) == 2, "both still feed the prediction"


def test_the_most_seen_order_wins_not_the_most_recent(cd_db):
    """Repeats are the weight: five sightings in one order and one in another
    reads 5:1, and the card shows the five."""
    player = _player(
        "Ravenshade",
        "738",
        (34_000_000, 30_000_000, 26_000_000),
        orders=[
            ("Aircraft", "Tank", "Missile"),
            ("Aircraft", "Tank", "Missile"),
            ("Tank", "Missile", "Aircraft"),
        ],
    )
    lineup, _ = cdp.build_side(player).likely_order()
    assert [t for _, t in lineup] == ["Aircraft", "Tank", "Missile"]


# ── Not rounding certainty into existence ─────────────────────────────────────


@pytest.mark.parametrize(
    ("prob", "expected"),
    [
        (0.9999, ">99%"),
        (0.0001, "<1%"),
        (1.0, ">99%"),
        (0.0, "<1%"),
        (0.5, "50%"),
        (0.102, "10%"),
    ],
)
def test_extremes_never_render_as_a_certainty(prob, expected):
    """`f"{0.9999:.0%}"` is "100%", which claims the match cannot be lost. The
    engine is decisive enough that a lopsided pairing hits this routinely, and
    a card that said 100% before an upset is one nobody trusts afterwards."""
    assert img._pct(prob) == expected


# ── The render survives real-world input ──────────────────────────────────────


def _render(a, b, **kwargs):
    return img.render(cdp.predict(a, b), **kwargs)


def test_render_produces_a_webp_of_the_declared_size(cd_db):
    """WebP at the template's native size, not a downsampled PNG.

    The card is mostly a photographic background, so the format is what makes
    it small -- a fifth the bytes of the PNG, where downsampling would have
    saved a quarter and cost the resolution.
    """
    a = _player("Ravenshade", "738", (34_000_000, 30_000_000, 26_000_000))
    b = _player("NightOwl", "738", (33_000_000, 31_000_000, 25_000_000))
    card = _render(a, b, subtitle="Group M · Semifinal")
    image = Image.open(io.BytesIO(card))
    assert image.format == "WEBP"
    assert image.size == (img.W, img.H)
    # Discord's free-tier ceiling is 10 MB and this should not be near it.
    assert len(card) < 1_000_000, "the card got large enough to be worth re-checking"


def test_non_latin_and_overlong_names_render(cd_db):
    """Champion Duel names routinely carry non-Latin scripts, and a name is
    user-supplied text of arbitrary length. Neither may raise."""
    a = _player("MangowhiskY 망고", "1042", (28_000_000, 26_500_000, 24_100_000))
    b = _player("[VERYLONG]" + "A" * 50, "902", (27_800_000, 26_900_000, 23_500_000))
    assert _render(a, b)


def test_a_missing_logo_does_not_fail_the_render(cd_db, monkeypatch):
    """Branding is not worth losing a prediction over -- the storm renderer
    takes the same position on the same asset."""
    monkeypatch.setattr(img, "_LOGO_PATH", "/nonexistent/logo.png")
    a = _player("Ravenshade", "738", (34_000_000, 30_000_000, 26_000_000))
    b = _player("NightOwl", "738", (33_000_000, 31_000_000, 25_000_000))
    assert _render(a, b, subtitle="Group M · Semifinal")


def test_subtitle_is_optional(cd_db):
    """The bot doesn't know the schedule; a caller that does can pass it.

    There is no stage field to fall back on (#488), and an invented round is
    worse than an empty box, so the header simply goes without.
    """
    a = _player("Ravenshade", "738", (34_000_000, 30_000_000, 26_000_000))
    b = _player("NightOwl", "738", (33_000_000, 31_000_000, 25_000_000))
    assert _render(a, b)


# ── The layout still describes the artwork ────────────────────────────────────
#
# The card is a compositor now: every field is a box in the layout JSON, drawn
# over a template neither this module nor the tests can see into. What can go
# wrong is no longer "the drawing code is wrong" but "the coordinates and the
# picture have drifted apart" -- which is silent, and which is exactly what
# happens when a design revision lands as a file swap.


def test_the_template_matches_the_canvas_the_layout_declares():
    """A template of a different size would put every coordinate out."""
    template = Image.open(img._TEMPLATE_PATH)
    assert template.size == (img.W, img.H)


def _all_text_boxes():
    for side in (img.LAYOUT["left"], img.LAYOUT["right"]):
        for field in ("name", "win_probability", "to_win", "status"):
            yield field, side[field]
        for i, row in enumerate(side["squad_rows"], start=1):
            yield f"row{i}.icon", row["icon"]
            yield f"row{i}.text", row["text"]
    yield "event_title", img.LAYOUT["header"]["event_title"]
    yield "round_metadata", img.LAYOUT["header"]["round_metadata"]
    yield "confidence", img.LAYOUT["footer"]["confidence_summary"]


def test_no_field_is_drawn_over_the_vs_burst():
    """The spec reserves the centre for the VS artwork, and it is the one part
    of the template a caption cannot be moved off after the fact."""
    zone = img.LAYOUT["vs_exclusion_zone"]
    for name, box in _all_text_boxes():
        overlaps_x = box["x"] < zone["x"] + zone["w"] and zone["x"] < box["x"] + box["w"]
        overlaps_y = box["y"] < zone["y"] + zone["h"] and zone["y"] < box["y"] + box["h"]
        assert not (overlaps_x and overlaps_y), f"{name} overlaps the VS burst"


def test_every_field_is_on_the_canvas():
    for name, box in _all_text_boxes():
        assert 0 <= box["x"] and box["x"] + box["w"] <= img.W, f"{name} runs off the side"
        assert 0 <= box["y"] and box["y"] + box["h"] <= img.H, f"{name} runs off the top or bottom"


def test_the_odds_bar_is_drawn_and_splits_where_the_odds_split(cd_db):
    """The template ships an empty track; the fill is entirely the bot's.

    Sampling the rendered track is the only way to catch a bar that stopped
    being drawn, or one drawn at the wrong split -- both of which leave a card
    that still looks finished while misstating the prediction it illustrates.
    """
    track = img.LAYOUT["dynamic_progress_track"]
    mid_y = track["y"] + track["h"] // 2

    a = _player("Goliath", "101", (52_000_000, 49_000_000, 47_000_000))
    b = _player("Pebble", "101", (14_000_000, 12_000_000, 11_000_000))
    result = cdp.predict(a, b)
    card = Image.open(io.BytesIO(img.render(result))).convert("RGB")

    # Far left is A's colour and far right is B's, whichever way the odds fell.
    left = card.getpixel((track["x"] + track["radius"], mid_y))
    right = card.getpixel((track["x"] + track["w"] - track["radius"], mid_y))
    assert left[2] > left[0], "the left end of the bar is not blue"
    assert right[0] > right[2], "the right end of the bar is not red"

    # A near-certain A puts the divider hard right, but never off the end --
    # the track keeps its rounded cap rather than losing a corner.
    assert result.p_a > 0.99
    blue = [
        x
        for x in range(track["x"], track["x"] + track["w"])
        if card.getpixel((x, mid_y))[2] > card.getpixel((x, mid_y))[0]
    ]
    assert max(blue) < track["x"] + track["w"] - 1, "the fill ran to the very end of the track"
    assert max(blue) > track["x"] + track["w"] * 0.9, "a >99% prediction did not fill the bar"
