"""The prediction cards.

A render is hard to assert on pixel by pixel and not worth it. What these
cover is the part that can be *wrong* rather than ugly: that the card shows the
line-up the prediction was actually computed from, that it never rounds a
probability up into a certainty, that a name in a non-Latin script or a missing
asset doesn't take the render down, and — since the card became a compositor
over a designed template — that the layout it is placed against still describes
the artwork it is placed on.

**Two cards.** The VS card is above; the day's picks card is in its own section
at the foot of this file. It has no artwork to drift from, so what is checked
instead is that its boxes do not drift into each other and that the strings it
draws are the ones it means to.
"""

from __future__ import annotations

import io
import itertools

import pytest
from PIL import Image, ImageChops, ImageDraw
from storm_renderer import _font_for_text

import champion_duel_db as db
import champion_duel_image as img
import champion_duel_picks as picks_lib
import champion_duel_predict as cdp
import champion_duel_wording as words

ACTOR = {"discord_user_id": "111", "discord_name": "Kevin", "guild_id": "999"}


@pytest.fixture
def cd_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", str(tmp_path / "champion_duel.sqlite3"))
    db.init_db()
    return None


def _player(name, server, powers, source="observed", orders=()):
    db.import_registrants(
        [{"name": name, "group": "M", "rank": 1, "server": server}], stage="qualifiers"
    )
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
    assert words.probability(prob) == expected


# ── Saying what the card is built on, in the reader's words ───────────────────


class _Side:
    def __init__(self, recorded, sightings):
        self.recorded_squads, self.sightings = recorded, sightings

    def likely_order(self):
        return [], self.sightings > 0


@pytest.mark.parametrize(
    ("sightings", "expected"),
    [
        (0, "Lineup not recorded — assuming strongest first"),
        (1, "Typical lineup in 1 observed battle"),
        (4, "Typical lineup in 4 observed battles"),
    ],
)
def test_the_status_line_says_where_the_lineup_came_from(sightings, expected):
    """Not "3/3 seen · their order in 1 sighting". Squads seen and sightings
    are how the data is stored, not a thing a player thinks about; what they
    want to know is whether this is what the opponent usually does."""
    assert words.lineup_summary(_Side(3, sightings)) == expected


def test_the_status_line_fits_without_shrinking_the_other_side():
    """Both status lines take one size, so an overlong string on one side sets
    the type size for both. Every variant has to fit at full size or a card
    with one unseen player quietly shrinks the line about the seen one."""
    draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    budget = min(img.LAYOUT[s]["status"]["w"] for s in ("left", "right")) - 16
    for sightings in (0, 1, 12):
        text = words.lineup_summary(_Side(3, sightings))
        width = draw.textlength(text, font=_font_for_text(text, 18))
        assert width <= budget, f"{text!r} is {width:.0f}px, over the {budget}px budget"


@pytest.mark.parametrize(
    ("a_recorded", "b_recorded", "expected"),
    [
        (3, 3, "both"),
        (0, 0, "neither"),
        (3, 0, "some"),
        (0, 3, "some"),
        (2, 1, "some"),
        (3, 2, "some"),
    ],
)
def test_the_footer_describes_what_this_card_actually_has(a_recorded, b_recorded, expected):
    """The confidence level is decided on both players' counts added together,
    so it cannot tell you whether one player is fully recorded and the other is
    guesswork or whether both are half known. The sentence beside it is chosen
    from the real per-side counts, so it can never claim more than the card has.
    """
    assert words.evidence(_Side(a_recorded, 0), _Side(b_recorded, 0)) == expected


def test_a_squad_someone_typed_in_counts_as_recorded(cd_db):
    """`edited` is the community's data-entry path, not an admin correction:
    everything entered through the hub lands with that source. Counting it as
    neither observed nor estimated made every squad a player contributed count
    against the confidence of the prediction it had just improved.
    """
    a = _player("Ravenshade", "738", (34_000_000, 30_000_000, 26_000_000), source="edited")
    b = _player("NightOwl", "738", (33_000_000, 31_000_000, 25_000_000), source="edited")
    side = cdp.build_side(a)

    assert side.recorded_squads == 3
    assert side.observed_squads == 0, "still not an observation; just a number we hold"
    assert words.evidence(cdp.build_side(a), cdp.build_side(b)) == "both"
    assert cdp.predict(a, b).confidence() == "medium", "no sightings yet, so not high"


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


def test_the_logo_lines_up_with_the_artwork_around_it():
    """The logo stands on the background with no frame to sit in, so what holds
    it in place is agreement with its neighbours: the header bar's height on
    one axis, the red card's right edge on the other. Both are in the artwork,
    so the check has to be too -- asserting the logo is centred in its own box
    would pass on any coordinates at all, which is how the previous version of
    this test missed a box 11px off its frame.
    """
    box = img.LAYOUT["header"]["logo_badge"]
    px = Image.open(img._TEMPLATE_PATH).convert("RGB").load()

    def lit_span(coords):
        lit = [i for i, x, y in coords if max(px[x, y]) > 110]
        assert lit, "found none of the artwork this is measured against"
        return lit[0], lit[-1]

    # The header bar, scanned in the gap between its two inner boxes so only
    # its own frame is in the way.
    bar_top, bar_bottom = lit_span([(y, 780, y) for y in range(0, 130)])
    assert abs(box["y"] - bar_top) <= 2, "logo does not start where the header bar does"
    assert abs((box["y"] + box["h"]) - bar_bottom) <= 2, "logo is not the header bar's height"

    # The red card's right border, below the header and clear of its corner.
    _, card_right = lit_span([(x, x, 400) for x in range(1500, img.W)])
    assert abs((box["x"] + box["w"]) - card_right) <= 2, "logo is not flush with the red card"

    assert box["w"] == box["h"], "the logo is square; a non-square box would letterbox it"


def test_the_templates_badge_frame_is_painted_out(cd_db):
    """The frame is 103x88 and the logo is square, so the logo replaces it
    rather than sitting in it. Anything left of the logo in that corner should
    be background -- a surviving stroke would read as a second, empty badge."""
    a = _player("Ravenshade", "738", (34_000_000, 30_000_000, 26_000_000))
    b = _player("NightOwl", "738", (33_000_000, 31_000_000, 25_000_000))
    card = Image.open(io.BytesIO(img.render(cdp.predict(a, b)))).convert("RGB")

    box = img.LAYOUT["header"]["logo_badge"]
    clear = box["clear"]
    # The strip between the header bar and the logo, where the old frame's left
    # side and both of its corners used to be.
    strip = card.crop((clear["x"] + clear["feather"], box["y"], box["x"], box["y"] + box["h"]))
    assert max(strip.getextrema()[0][1], strip.getextrema()[2][1]) < 40, (
        "the badge frame is still visible beside the logo"
    )


def test_the_logo_fills_its_badge_without_escaping_it(cd_db, monkeypatch):
    """Comparing a render against the same render with the logo suppressed
    isolates the pixels the logo drew, which is the only way to assert on
    placement without recognising the mark itself.

    The frame removal is switched off for this, so the difference is the logo
    alone rather than the logo plus the rectangle it was painted onto.
    """
    monkeypatch.setitem(img.LAYOUT["header"]["logo_badge"], "clear", None)
    a = _player("Ravenshade", "738", (34_000_000, 30_000_000, 26_000_000))
    b = _player("NightOwl", "738", (33_000_000, 31_000_000, 25_000_000))
    result = cdp.predict(a, b)

    with_logo = Image.open(io.BytesIO(img.render(result))).convert("RGB")
    monkeypatch.setattr(img, "_LOGO_PATH", "/nonexistent/logo.png")
    without = Image.open(io.BytesIO(img.render(result))).convert("RGB")

    # Thresholded, because the card is lossy WebP: changing one corner shifts
    # the encoding everywhere by a little, and only the logo shifts it by a lot.
    delta = ImageChops.difference(with_logo, without).convert("L")
    drawn = delta.point(lambda v: 255 if v > 40 else 0).getbbox()
    assert drawn, "the logo drew nothing at all"

    box = img.LAYOUT["header"]["logo_badge"]
    assert box["x"] <= drawn[0] and drawn[2] <= box["x"] + box["w"], "logo escapes its badge"
    assert box["y"] <= drawn[1] and drawn[3] <= box["y"] + box["h"], "logo escapes its badge"
    # Running the frame's full height rather than floating in the middle of it.
    assert drawn[3] - drawn[1] >= box["h"] - 2


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


# ── The day's picks card ──────────────────────────────────────────────────────
#
# A different kind of card and so a different kind of test. The VS card is
# checked against artwork these tests cannot see into; this one draws its own
# bands, so what can go wrong is the geometry itself (two columns written into
# the same pixels) and what the card SAYS. Both are asserted directly: the
# layout by comparing its boxes against each other, and the copy by recording
# what `_text` was asked to draw.


def _slate_group(names, *, scouted=None, label="M", stage="semifinals"):
    """A group of invented players, most of them scouted."""
    scouted = len(names) if scouted is None else scouted
    db.import_registrants(
        [
            {"name": name, "group": label, "rank": i + 1, "server": "738", "thp": 90_000_000}
            for i, name in enumerate(names)
        ],
        stage=stage,
    )
    group = db.get_or_create_group(db.default_grouping_id(), stage, label)
    for i, name in enumerate(names):
        rid = db.resolve_registrant(name, server="738")["id"]
        db.set_placement(group["id"], rid, seed_rank=i + 1)
        if i < scouted:
            powers = (34_000_000 - i * 900_000, 31_000_000 - i * 700_000, 27_000_000 - i * 500_000)
            for slot, (squad_type, power) in enumerate(
                zip(("Tank", "Missile", "Aircraft"), powers), start=1
            ):
                db.set_squad(
                    rid, slot, squad_type=squad_type, power=power, actor=ACTOR, source="observed"
                )
    return group


def _slate(names, pairs, *, scouted=None, day="2026-08-25", **kwargs):
    group = _slate_group(names, scouted=scouted, **kwargs)
    ids = [db.resolve_registrant(n, server="738")["id"] for n in names]
    db.set_slate(ACTOR["guild_id"], day, [(ids[a], ids[b]) for a, b in pairs], actor=ACTOR)
    return picks_lib.build(ACTOR["guild_id"], day)


def _drawn(monkeypatch, slate):
    """Every string the card was asked to draw, and where.

    Recording the call rather than reading pixels back is the only way to
    assert on seventeen rows of copy, and it is the seam the render actually
    has: `_text` is where a string becomes ink.
    """
    seen = []
    original = img._text

    def spy(draw, box, text, font, fill, **kwargs):
        seen.append(text)
        return original(draw, box, text, font, fill, **kwargs)

    monkeypatch.setattr(img, "_text", spy)
    img.render_slate(slate)
    return seen


# ── The card grows with the slate ─────────────────────────────────────────────


def test_the_canvas_grows_with_the_row_count(cd_db):
    """The one structural difference from the VS card: there is no fixed
    template, because five meetings and seventeen are the same card at two
    heights."""
    names = ("Ravenshade", "NightOwl", "Ironclad", "Vesper", "Kestrel", "Basalt")
    five = _slate(names, [(0, 1), (2, 3), (4, 5), (0, 2), (1, 3)])
    fifteen = _slate(names, list(itertools.combinations(range(6), 2))[:15], day="2026-08-26")

    small = Image.open(io.BytesIO(img.render_slate(five)))
    large = Image.open(io.BytesIO(img.render_slate(fifteen)))

    assert small.size == (img.PICKS["canvas"]["width"], img.picks_height(5))
    assert large.size == (img.PICKS["canvas"]["width"], img.picks_height(15))
    assert large.height - small.height == 10 * img.PICKS["row"]["pitch"]


def test_the_card_is_webp_like_the_other_one(cd_db):
    slate = _slate(("Ravenshade", "NightOwl"), [(0, 1)])
    assert Image.open(io.BytesIO(img.render_slate(slate))).format == "WEBP"


def test_a_card_with_no_meetings_is_refused(cd_db):
    """An empty card is not a smaller card. Nothing upstream should produce
    one, and drawing a header over a footer would hide that it did."""
    _slate_group(("Ravenshade", "NightOwl"))
    empty = picks_lib.assemble(ACTOR["guild_id"], "2026-08-25", [])
    with pytest.raises(ValueError):
        img.render_slate(empty)


# ── What the card says ────────────────────────────────────────────────────────


def test_the_card_carries_both_names_the_odds_and_how_much_they_are_worth(cd_db, monkeypatch):
    slate = _slate(("Ravenshade", "NightOwl"), [(0, 1)])
    drawn = _drawn(monkeypatch, slate)

    assert "Ravenshade" in drawn and "NightOwl" in drawn
    assert words.probability(slate.picks[0].p_a) in drawn
    assert words.probability(slate.picks[0].p_b) in drawn
    assert slate.picks[0].confidence().capitalize() in drawn
    assert slate.subject() in drawn
    assert picks_lib.CARD_FOOTER in drawn
    assert list(picks_lib.CARD_CONFIDENCE_HEADING) == [
        line for line in drawn if line in picks_lib.CARD_CONFIDENCE_HEADING
    ]


def test_a_row_nobody_can_predict_keeps_both_names(cd_db, monkeypatch):
    """It is the most useful row on the card: two players nobody has scouted,
    named to the alliance about to read it. A dropped row hides the gap and a
    row without names cannot be acted on."""
    slate = _slate(("Ravenshade", "NightOwl", "Kestrel", "Basalt"), [(0, 1), (2, 3)], scouted=2)
    drawn = _drawn(monkeypatch, slate)

    assert "Kestrel" in drawn and "Basalt" in drawn
    assert picks_lib.CARD_NO_PREDICTION in drawn
    # And no probability is invented for it: only the one predicted row
    # contributes percentages.
    assert len([text for text in drawn if text.endswith("%")]) == 2


def test_the_card_never_rounds_a_probability_into_a_certainty(cd_db, monkeypatch):
    """The same refusal the VS card makes, on a surface that makes it up to
    seventeen times. The engine clears 0.999 on a 35% power edge, so this is
    the common case for a lopsided pairing rather than an edge case."""
    slate = _slate(("Goliath", "Pebble"), [(0, 1)], scouted=2)
    rid = db.resolve_registrant("Pebble", server="738")["id"]
    with db._get_conn() as conn:
        conn.execute("UPDATE squads SET power = power / 4 WHERE registrant_id = ?", (rid,))
    slate = picks_lib.build(slate.guild_id, "2026-08-25")

    drawn = _drawn(monkeypatch, slate)
    assert ">99%" in drawn
    assert "100%" not in drawn


def test_every_name_is_measured_and_drawn_in_a_font_that_has_its_script(cd_db, monkeypatch):
    """Champion Duel names routinely carry Korean and Arabic, and
    `_font_for_text` picks the file by script.

    The first version of this card sized every name against one Latin font and
    then reused that font object for all of them, which drew a Hangul name as a
    row of empty boxes. Asserting that each label reaches `_font_for_text` is
    what catches that: under the defect the only string it ever saw was a
    placeholder.
    """
    names = ("그림자늑대", "Ravenshade", "منتقم", "NightOwl")
    slate = _slate(names, [(0, 1), (2, 3)])

    asked = []
    original = img._font_for_text
    monkeypatch.setattr(
        img,
        "_font_for_text",
        lambda text, size, **kwargs: (asked.append(text), original(text, size, **kwargs))[1],
    )
    img.render_slate(slate)

    for name in names:
        assert name in asked, f"{name} was never sized in its own font"


# ── The layout describes the card ─────────────────────────────────────────────
#
# The equivalent of the VS card's "does the layout still describe the artwork"
# block. There is no artwork to drift from here, so what is checked instead is
# that the boxes do not drift into each other -- which is exactly how the
# column heading first came to be drawn underneath the badge.


def _rects(band: dict, skip=()):
    for name, box in band.items():
        if name in skip or not isinstance(box, dict) or "x" not in box:
            continue
        yield name, box


def _overlap(one: dict, two: dict) -> bool:
    return (
        one["x"] < two["x"] + two["w"]
        and two["x"] < one["x"] + one["w"]
        and one["y"] < two["y"] + two["h"]
        and two["y"] < one["y"] + one["h"]
    )


def test_nothing_in_the_header_is_drawn_over_anything_else():
    """The badge is square and as wide as the confidence column, so those two
    are the pair that collides. It did."""
    header = dict(img.PICKS["header"])
    heading = header.pop("confidence_heading")
    boxes = list(_rects(header))
    # The heading is two stacked lines, so it is checked as the pair it draws.
    for i, line in enumerate(picks_lib.CARD_CONFIDENCE_HEADING):
        boxes.append((f"confidence_heading[{i}]", img._at(heading, i * heading["line_height"])))

    for i, (name, box) in enumerate(boxes):
        for other_name, other in boxes[i + 1 :]:
            assert not _overlap(box, other), f"{name} overlaps {other_name}"


def test_no_two_columns_of_a_row_are_drawn_into_the_same_pixels():
    """`no_prediction` and `plate` are left out: one replaces the middle three
    columns and the other sits behind all of them, so both overlap on purpose.
    """
    boxes = list(_rects(img.PICKS["row"], skip=("no_prediction", "plate")))
    for i, (name, box) in enumerate(boxes):
        for other_name, other in boxes[i + 1 :]:
            assert not _overlap(box, other), f"{name} overlaps {other_name}"


def test_the_refusal_covers_the_columns_it_replaces_and_no_others():
    """It stands in for both probabilities and the track. Reaching a name
    column would put it over a name that is still being drawn."""
    row = img.PICKS["row"]
    absent = row["no_prediction"]
    for name in ("probability_a", "track", "probability_b"):
        assert _overlap(absent, row[name]), f"the refusal does not cover {name}"
    for name in ("index", "name_a", "name_b", "confidence"):
        assert not _overlap(absent, row[name]), f"the refusal runs into {name}"


def test_every_box_is_inside_the_band_it_belongs_to():
    width = img.PICKS["canvas"]["width"]
    bands = (
        ("header", img.PICKS["header"], img.PICKS["header"]["h"]),
        ("row", img.PICKS["row"], img.PICKS["row"]["pitch"]),
        ("footer", img.PICKS["footer"], img.PICKS["footer"]["h"]),
    )
    for band_name, band, height in bands:
        for name, box in _rects(band):
            assert 0 <= box["x"] and box["x"] + box["w"] <= width, f"{band_name}.{name} runs off"
            assert 0 <= box["y"] and box["y"] + box["h"] <= height, (
                f"{band_name}.{name} runs out of its band"
            )


def test_the_rows_leave_a_gap_between_their_plates():
    """A pitch equal to the plate height would butt every row against the next
    and the card would read as one block rather than as a list."""
    row = img.PICKS["row"]
    assert row["pitch"] > row["plate"]["y"] + row["plate"]["h"]


# ── The bar ───────────────────────────────────────────────────────────────────


def test_each_row_gets_its_own_bar_split_where_that_meeting_splits(cd_db):
    """The same routine the VS card uses, over a track the layout names. It is
    worth sampling here too: a card of seventeen bars all drawn at one split
    would still look finished."""
    slate = _slate(("Goliath", "Pebble", "Ravenshade", "NightOwl"), [(0, 1), (2, 3)], scouted=4)
    ids = {
        n: db.resolve_registrant(n, server="738")["id"]
        for n in ("Pebble", "Ravenshade", "NightOwl")
    }
    with db._get_conn() as conn:
        # One meeting as lopsided as the engine gets, one as level as it gets,
        # so a single bar drawn twice cannot pass both checks below.
        conn.execute(
            "UPDATE squads SET power = power / 4 WHERE registrant_id = ?", (ids["Pebble"],)
        )
        conn.execute(
            "UPDATE squads SET power = (SELECT power FROM squads o WHERE o.registrant_id = ? "
            "AND o.slot = squads.slot) WHERE registrant_id = ?",
            (ids["Ravenshade"], ids["NightOwl"]),
        )
    slate = picks_lib.build(slate.guild_id, "2026-08-25")
    card = Image.open(io.BytesIO(img.render_slate(slate))).convert("RGB")

    row = img.PICKS["row"]
    track = row["track"]
    for i, pick in enumerate(slate.picks):
        top = img.PICKS["header"]["h"] + i * row["pitch"] + track["y"]
        mid = top + track["h"] // 2
        left = card.getpixel((track["x"] + track["radius"], mid))
        right = card.getpixel((track["x"] + track["w"] - track["radius"], mid))
        assert left[2] > left[0], f"row {i + 1} does not start blue"
        assert right[0] > right[2], f"row {i + 1} does not end red"

        blue = [
            x
            for x in range(track["x"], track["x"] + track["w"])
            if card.getpixel((x, mid))[2] > card.getpixel((x, mid))[0]
        ]
        share = (max(blue) - track["x"]) / track["w"]
        assert share == pytest.approx(pick.p_a, abs=0.12), f"row {i + 1} splits at the wrong place"

    # The two rows are genuinely different meetings, and by more than the
    # tolerance above, so a bar drawn once and pasted everywhere fails the
    # check rather than passing it twice.
    assert abs(slate.picks[0].p_a - slate.picks[1].p_a) > 0.4


# ── The artwork seam ──────────────────────────────────────────────────────────


def test_finished_artwork_is_composited_the_moment_the_files_exist(cd_db, monkeypatch, tmp_path):
    """The card draws its own bands only because there is no artwork yet. When
    it arrives it arrives as three bands, and this is the seam it lands on --
    built and covered now, so it is a file drop later rather than a rewrite.
    """
    monkeypatch.setattr(img, "_ASSETS", str(tmp_path))
    width = img.PICKS["canvas"]["width"]
    Image.new("RGBA", (width, img.PICKS["row"]["pitch"]), (0, 200, 0, 255)).save(
        tmp_path / "row.png"
    )
    monkeypatch.setitem(img.PICKS["bands"], "row", "row.png")

    slate = _slate(("Ravenshade", "NightOwl"), [(0, 1)])
    card = Image.open(io.BytesIO(img.render_slate(slate))).convert("RGB")

    # A pixel inside the row band and clear of every column's text.
    y = img.PICKS["header"]["h"] + 4
    assert card.getpixel((img.PICKS["row"]["plate"]["x"] + 4, y))[1] > 150


def test_a_named_band_that_is_not_on_disk_is_drawn_instead_of_raised_on(cd_db, monkeypatch):
    """The opposite of the VS card's rule about its template, and for a
    reason: that card cannot exist without its background, where this one is
    drawn either way and a missing band only costs it some polish.
    """
    monkeypatch.setitem(img.PICKS["bands"], "header", "not_delivered_yet.webp")
    slate = _slate(("Ravenshade", "NightOwl"), [(0, 1)])
    assert img.render_slate(slate)
