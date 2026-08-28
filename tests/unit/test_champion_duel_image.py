"""The prediction cards.

A render is hard to assert on pixel by pixel and not worth it. What these
cover is the part that can be *wrong* rather than ugly: that the card shows the
line-up the prediction was actually computed from, that it never rounds a
probability up into a certainty, that a name in a non-Latin script or a missing
asset doesn't take the render down, and — since the card became a compositor
over a designed template — that the layout it is placed against still describes
the artwork it is placed on.

**Two cards.** The VS card is above; the day's picks card is in its own section
at the foot of this file. That one is assembled from pieces rather than
composited over one template, so it gets the same "does the layout still
describe the artwork" check plus two the VS card does not need: that its boxes
do not drift into each other across *both* templates, and that the geometry
Kevin settled by hand — the reserved name box, the cap on the bar's outer
terminal, the balanced gap above and below the row stack — cannot be undone by
a later layout edit without a test saying so.
"""

from __future__ import annotations

import io
import os

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
# A different kind of card and so a different kind of test. The VS card is one
# fixed template; this one assembles bands onto two templates and its height is
# data, so what can go wrong is the geometry itself -- two boxes written into
# the same pixels, a column that runs off the card, a band whose artwork was
# re-exported at a different size -- and what the card SAYS. All of it is
# asserted directly.
#
# **Most of these build their slates from stand-ins rather than from the
# database**, and that is deliberate. A picks row needs four fields; going
# through the database instead would tie this file to a schema being rewritten
# in a neighbouring session, and would make a twenty-row card cost forty
# fabricated players. `_StubPick` and `_StubSlate` are the renderer's actual
# contract written out. One test at the foot of this section does go through
# the real `picks_lib`, because that seam is the thing the stand-ins cannot
# cover: it is what fails if the two ever disagree.


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


class _StubPick:
    def __init__(self, a_label, b_label, p_a):
        self.a_label = a_label
        self.b_label = b_label
        self.predicted = p_a is not None
        self.p_a = p_a
        self.p_b = None if p_a is None else 1.0 - p_a


class _StubSlate:
    def __init__(self, picks, subject="Semi-finals Predictions · Aug 18"):
        self.picks = picks
        self._subject = subject

    def subject(self):
        return self._subject


def _stub_slate(n, **kwargs):
    """`n` meetings, all predicted, at descending and distinct probabilities."""
    return _StubSlate(
        [_StubPick(f"Left{i}", f"Right{i}", 0.95 - 0.02 * i) for i in range(n)], **kwargs
    )


def _drawn(monkeypatch, slate):
    """Every string the card was asked to draw.

    Recording the call rather than reading pixels back is the only way to
    assert on twenty rows of copy, and it is the seam the render actually has:
    `_text` is where a string becomes ink. The PICK cap draws through
    `ImageDraw.text` instead, because it needs a per-zone halo, so the cap is
    checked in pixels below rather than here.
    """
    seen = []
    original = img._text

    def spy(draw, box, text, font, fill, **kwargs):
        seen.append(text)
        return original(draw, box, text, font, fill, **kwargs)

    monkeypatch.setattr(img, "_text", spy)
    img.render_slate(slate)
    return seen


def _cap_gold(card, box):
    """How much of `box` is the cap's gold, as a fraction of the pixels in it.

    The cap is the only strongly gold thing on a row -- the bars are blue and
    red and the starburst is magenta -- so this is what tells a capped bar from
    an uncapped one without reading the artwork. Gold is a *ratio* rather than
    a brightness: the red bar carries lit highlights that clear any threshold
    on the red channel alone, and the first version of this counted them.
    """
    hits = 0
    total = 0
    for y in range(box["y"] + 8, box["y"] + box["h"] - 8, 2):
        for x in range(box["x"] + 8, box["x"] + box["w"] - 8, 2):
            r, g, b = card.getpixel((x, y))
            total += 1
            if r > 140 and 0.5 * r < g < 0.85 * r and b < 0.45 * r:
                hits += 1
    return hits / max(total, 1)


def _row_box(key, column_x, index):
    """One row box, moved onto the row and column it is drawn for."""
    return img._at(img.PICKS["row"][key], column_x, img._ROWS_TOP + index * img._GEOM["pitch"])


# ── Two templates, and which one a slate lands on ─────────────────────────────


@pytest.mark.parametrize(
    "rows,width,columns",
    [(1, 950, 1), (2, 950, 1), (10, 950, 1), (11, 1900, 2), (20, 1900, 2)],
)
def test_the_template_is_chosen_by_row_count(rows, width, columns):
    """Kevin, 27 Aug: *"having a different design for 1 column of 10 vs 2
    columns of 10 is better so we know which we need."* The switch is automatic
    rather than something the maker picks."""
    template = img.picks_template(rows)
    assert template["width"] == width
    assert len(template["columns"]) == columns
    assert img.picks_size(rows)[0] == width


def test_a_card_with_no_meetings_is_refused():
    """An empty card is not a smaller card. Nothing upstream should produce
    one, and drawing a header over a footer would hide that it did."""
    with pytest.raises(ValueError):
        img.render_slate(_StubSlate([]))


def test_the_twenty_first_meeting_is_refused_rather_than_dropped():
    """Overflow makes a second slate; it never drops a row. That is what
    retires `CAPTION_TRUNCATED` by construction rather than by rule, so the cap
    has to raise here rather than quietly render the first twenty."""
    assert img.picks_template(20)
    with pytest.raises(ValueError, match="second slate"):
        img.picks_template(21)
    with pytest.raises(ValueError):
        img.render_slate(_stub_slate(21))


@pytest.mark.parametrize("rows,split", [(11, [6, 5]), (12, [6, 6]), (19, [10, 9]), (20, [10, 10])])
def test_the_columns_balance_and_the_extra_row_goes_left(rows, split):
    """Kevin, correcting the wireframe: *"just do it as 6 and 5 and don't
    squish the 6, let there be an empty space under the 5."* Eleven rows is
    never ten and one, which would read as a mistake every time."""
    assert img._column_split(rows, img.picks_template(rows)) == split


def test_height_follows_the_tallest_column_not_the_row_count():
    """Two columns of ten is exactly as tall as one column of ten. A height
    that counted rows would make the wide card twice as tall as the artwork it
    is assembled from."""
    assert img.picks_height(20) == img.picks_height(10)
    assert img.picks_height(11) == img.picks_height(6)
    assert img.picks_height(10) - img.picks_height(1) == 9 * img._GEOM["pitch"]


def test_the_card_grows_by_exactly_one_pitch_per_row():
    """The one structural difference from the VS card: there is no fixed
    template, because two meetings and ten are the same card at two heights."""
    small = Image.open(io.BytesIO(img.render_slate(_stub_slate(2))))
    large = Image.open(io.BytesIO(img.render_slate(_stub_slate(7))))
    assert small.size == img.picks_size(2)
    assert large.size == img.picks_size(7)
    assert large.height - small.height == 5 * img._GEOM["pitch"]


def test_the_card_is_webp_like_the_other_one():
    assert Image.open(io.BytesIO(img.render_slate(_stub_slate(3)))).format == "WEBP"


# ── What the card says ────────────────────────────────────────────────────────


def test_the_card_carries_both_names_the_subject_and_the_footer(monkeypatch):
    slate = _StubSlate([_StubPick("Ravenshade", "NightOwl", 0.73)])
    drawn = _drawn(monkeypatch, slate)

    assert "Ravenshade" in drawn and "NightOwl" in drawn
    assert slate.subject() in drawn
    assert picks_lib.CARD_TITLE in drawn
    assert picks_lib.CARD_FOOTER in drawn


def test_a_row_carries_one_percentage_and_it_is_on_the_picked_side():
    """Question 2, closed: *"all that matters is who the pick is and who we
    predict to win."* One number, on the picked side, and the unpicked bar
    keeps its reserved space empty.

    The two probabilities are far apart on purpose. Giving both rows the same
    favourite margin makes the row order turn on whether `1 - 0.18` is exactly
    `0.82` in binary, which it is not.
    """
    slate = _StubSlate([_StubPick("Alpha", "Bravo", 0.91), _StubPick("Charlie", "Delta", 0.23)])
    card = Image.open(io.BytesIO(img.render_slate(slate))).convert("RGB")
    column = img.picks_template(2)["columns"][0]

    # Row 0 is picked on the left, row 1 on the right -- and each row's other
    # bar carries no cap at all rather than a second number.
    assert _cap_gold(card, _row_box("cap_a", column, 0)) > 0.10
    assert _cap_gold(card, _row_box("cap_b", column, 0)) < 0.02
    assert _cap_gold(card, _row_box("cap_b", column, 1)) > 0.10
    assert _cap_gold(card, _row_box("cap_a", column, 1)) < 0.02


def test_a_row_nobody_can_predict_keeps_both_names_and_claims_nothing(monkeypatch):
    """It is the most useful row on the card: two players nobody has scouted,
    named to the alliance about to read it. A dropped row hides the gap. What
    it must not do is carry a cap, because the cap *is* the claim."""
    slate = _StubSlate([_StubPick("Kestrel", "Basalt", None)])
    drawn = _drawn(monkeypatch, slate)
    assert "Kestrel" in drawn and "Basalt" in drawn
    assert not [text for text in drawn if text.endswith("%")]

    card = Image.open(io.BytesIO(img.render_slate(slate))).convert("RGB")
    column = img.picks_template(1)["columns"][0]
    assert _cap_gold(card, _row_box("cap_a", column, 0)) < 0.02
    assert _cap_gold(card, _row_box("cap_b", column, 0)) < 0.02


def test_the_rows_are_ordered_strongest_pick_first():
    """Kevin, 27 Aug: *"I think let's go with strongest pick first."* So the
    order on the card is the card's own rather than the order the maker entered
    the meetings in, and a row nobody can predict sorts to the end rather than
    interrupting the ladder."""
    picks = [
        _StubPick("Weak", "Weaker", 0.55),
        _StubPick("Absent", "Unknown", None),
        _StubPick("Strong", "Weakest", 0.97),
        _StubPick("Middle", "Other", 0.80),
    ]
    ordered = []
    original = img._picks_row
    img_render = img.render_slate

    def spy(canvas, draw, pick, origin, name_size):
        ordered.append(pick.a_label)
        return original(canvas, draw, pick, origin, name_size)

    img._picks_row = spy
    try:
        img_render(_StubSlate(picks))
    finally:
        img._picks_row = original
    assert ordered == ["Strong", "Middle", "Weak", "Absent"]


def test_the_card_never_rounds_a_probability_into_a_certainty(monkeypatch):
    """The same refusal the VS card makes, on a surface that makes it up to
    twenty times. The engine clears 0.999 on a large power edge, so this is the
    common case for a lopsided pairing rather than an edge case.

    The cap draws through `ImageDraw.text` rather than `_text`, so the spy goes
    on `_fit`, which every string on the cap is sized by.
    """
    seen = []
    original = img._fit
    monkeypatch.setattr(
        img,
        "_fit",
        lambda draw, text, *a, **k: (seen.append(text), original(draw, text, *a, **k))[1],
    )
    img.render_slate(_StubSlate([_StubPick("Goliath", "Pebble", 0.99994)]))
    assert ">99%" in seen
    assert "100%" not in seen


def test_every_name_is_measured_and_drawn_in_a_font_that_has_its_script(monkeypatch):
    """Champion Duel names routinely carry Korean and Arabic, and
    `_font_for_text` picks the file by script.

    The first version of this card sized every name against one Latin font and
    then reused that font object for all of them, which drew a Hangul name as a
    row of empty boxes. Asserting that each label reaches `_font_for_text` is
    what catches that: under the defect the only string it ever saw was a
    placeholder.
    """
    names = ("그림자늑대", "Ravenshade", "منتقم", "NightOwl")
    asked = []
    original = img._font_for_text
    monkeypatch.setattr(
        img,
        "_font_for_text",
        lambda text, size, **kwargs: (asked.append(text), original(text, size, **kwargs))[1],
    )
    img.render_slate(
        _StubSlate([_StubPick(names[0], names[1], 0.6), _StubPick(names[2], names[3], 0.4)])
    )

    for name in names:
        assert name in asked, f"{name} was never sized in its own font"


# ── The layout describes the card ─────────────────────────────────────────────


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


@pytest.mark.parametrize("name", ("single", "wide"))
def test_nothing_in_a_header_is_drawn_over_anything_else(name):
    """The badge is square and sits where a centred title could reach, so
    those two are the pair that collides. On the old layout it did."""
    boxes = list(_rects(img.PICKS["templates"][name]["header"]))
    for i, (box_name, box) in enumerate(boxes):
        for other_name, other in boxes[i + 1 :]:
            assert not _overlap(box, other), f"{name}: {box_name} overlaps {other_name}"


def test_no_two_boxes_of_a_row_are_drawn_into_the_same_pixels():
    boxes = list(_rects(img.PICKS["row"]))
    for i, (name, box) in enumerate(boxes):
        for other_name, other in boxes[i + 1 :]:
            assert not _overlap(box, other), f"{name} overlaps {other_name}"


def test_the_two_name_boxes_are_the_same_size_and_symmetric():
    """Kevin, 28 Aug: *"no matter what we always have the same space for a
    name."* The reserved cap footprint is what makes that true, and a layout
    revision that widened one side would silently undo it -- the picked
    player's name would go back to being the first to ellipsize."""
    row, band = img.PICKS["row"], img._GEOM["row_band"]
    a, b = row["name_a"], row["name_b"]
    assert a["w"] == b["w"]
    assert a["x"] == band["w"] - (b["x"] + b["w"]), "the name boxes are not mirrored"
    assert a["x"] == row["cap_a"]["w"], "the name box does not start one cap width in"


def test_the_name_boxes_stop_clear_of_the_starburst():
    """The emblem is opaque artwork in the middle of the bar. A name box that
    reached it would put the end of a name under the VS."""
    row, emblem = img.PICKS["row"], img._GEOM["emblem"]
    a, b = row["name_a"], row["name_b"]
    assert a["x"] + a["w"] <= emblem["x"]
    assert b["x"] >= emblem["x"] + emblem["w"]


def test_the_caps_sit_on_the_bars_outer_terminals_and_match_its_height():
    """Kevin, 28 Aug: *"far left for blue, far right for red"*, and *"the cap
    is actually meant to be the same height as the bar."* Flush on it, not
    proud of it."""
    row, band, bar = img.PICKS["row"], img._GEOM["row_band"], img._GEOM["bar"]
    for key in ("cap_a", "cap_b"):
        assert row[key]["y"] == bar["y"]
        assert row[key]["h"] == bar["h"]
    assert row["cap_a"]["x"] == 0
    assert row["cap_b"]["x"] + row["cap_b"]["w"] == band["w"]


def test_the_cap_art_keeps_its_aspect_when_it_is_scaled_to_the_bar():
    """Its native 1672x941 is not on the 4x grid the rest of the set shares,
    so the size it is drawn at is derived rather than given. A wrong one
    stretches the plate, which is what the mockup does by hand."""
    cap = img.PICKS["cap"]
    native_w, native_h = cap["native"]
    draw_w, draw_h = cap["draw"]
    assert draw_h == img._GEOM["bar"]["h"] * img.PICKS["master_scale"]
    assert draw_w == pytest.approx(native_w * draw_h / native_h, abs=1)


@pytest.mark.parametrize("name", ("single", "wide"))
def test_every_box_is_inside_the_band_it_belongs_to(name):
    template = img.PICKS["templates"][name]
    width = template["width"]
    for band_name, band, height in (
        ("header", template["header"], img._GEOM["header_h"]),
        ("footer", template["footer"], img._GEOM["footer_h"]),
    ):
        for box_name, box in _rects(band):
            assert 0 <= box["x"] and box["x"] + box["w"] <= width, (
                f"{name}.{band_name}.{box_name} runs off the card"
            )
            assert 0 <= box["y"] and box["y"] + box["h"] <= height, (
                f"{name}.{band_name}.{box_name} runs out of its band"
            )


@pytest.mark.parametrize("name", ("single", "wide"))
def test_the_columns_fit_the_card_with_a_margin_on_both_sides(name):
    template = img.PICKS["templates"][name]
    band_w = img._GEOM["row_band"]["w"]
    xs = template["columns"]
    assert xs[0] > 0, "the first column starts on the card's edge"
    assert xs[-1] + band_w < template["width"], "the last column runs off the card"
    assert xs[0] == template["width"] - (xs[-1] + band_w), "the side margins differ"
    for left, right in zip(xs, xs[1:]):
        assert left + band_w <= right, "two columns overlap"


def test_every_row_box_stays_inside_the_row_band():
    band = img._GEOM["row_band"]
    for name, box in _rects(img.PICKS["row"]):
        assert 0 <= box["x"] and box["x"] + box["w"] <= band["w"], f"row.{name} runs off the band"
        assert 0 <= box["y"] and box["y"] + box["h"] <= band["h"], f"row.{name} leaves the band"


def test_every_cap_box_stays_inside_the_cap_it_is_drawn_on():
    cap = img.PICKS["cap"]
    for name in ("label", "percentage"):
        box = cap[name]
        assert 0 <= box["x"] and box["x"] + box["w"] <= cap["draw"][0], f"cap.{name} runs off"
        assert 0 <= box["y"] and box["y"] + box["h"] <= cap["draw"][1], f"cap.{name} runs off"


def test_the_rows_overlap_their_own_bleed_but_never_their_bars():
    """The row asset is taller than the pitch on purpose -- each row sits about
    30% inside the one above and the starbursts come close without colliding.
    What must never overlap is the bars themselves."""
    band, bar, pitch = img._GEOM["row_band"], img._GEOM["bar"], img._GEOM["pitch"]
    assert pitch < band["h"], "the rows no longer overlap their bleed"
    assert pitch > bar["y"] + bar["h"], "two rows' bars would touch"


def test_the_gap_above_the_first_row_matches_the_gap_below_the_last():
    """Kevin, 28 Aug: *"The space above the first row is a lot and I debated
    reducing that. We may want to so it feels visually equal to the footer."*

    The two gaps are measured to the artwork's own ink rather than to its band
    edges, because both bands fade rather than ending: the header's ink stops
    33px above its bottom edge and the footer's starts 18px below its top. His
    mockup was 86 above and 64 below, the 1.35x he saw.
    """
    header_dead, footer_dead = 33, 18
    above = header_dead + (img._GEOM["bar"]["y"] - img._GEOM["header_overlap"])
    below = img._GEOM["trailing"] + footer_dead
    assert above == below, f"{above}px above the first bar against {below}px below the last"


# ── The layout describes the artwork ──────────────────────────────────────────
#
# The VS card's equivalent block, which this card could not have until the
# artwork existed. Every piece is committed at the size it is drawn at, so a
# re-export at a different size is a silent stretch rather than an error --
# `_art` resizes nothing.


def test_every_named_piece_of_artwork_is_on_disk_at_the_size_the_layout_states():
    expected = [
        (img._GEOM["row_band"]["art"], (img._GEOM["row_band"]["w"], img._GEOM["row_band"]["h"])),
        (img.PICKS["cap"]["art"], tuple(img.PICKS["cap"]["draw"])),
    ]
    for name in ("single", "wide"):
        template = img.PICKS["templates"][name]
        expected.append((template["header"]["art"], (template["width"], img._GEOM["header_h"])))
        expected.append((template["footer"]["art"], (template["width"], img._GEOM["footer_h"])))

    for filename, size in expected:
        path = os.path.join(img._ASSETS, filename)
        assert os.path.isfile(path), f"{filename} is named by the layout but not on disk"
        with Image.open(path) as art:
            assert art.size == size, f"{filename} is {art.size}, layout says {size}"


def test_the_cap_is_mirrored_for_the_red_side_but_its_type_is_not():
    """The plate's chamfer points outwards on both sides, so the artwork is
    flipped for red -- and flipping it without flipping the text boxes would
    put the type on the chamfer, where there is no flat face to sit on."""
    blue = img._pick_cap("74%", mirror=False)
    red = img._pick_cap("74%", mirror=True)
    assert blue is not None and red is not None
    # The art really is mirrored...
    assert ImageChops.difference(blue, red).getbbox() is not None
    # ...and the type is not: a mirrored render would match the flip exactly.
    assert ImageChops.difference(blue.transpose(Image.FLIP_LEFT_RIGHT), red).getbbox() is not None


@pytest.mark.parametrize("piece", ("header", "footer"))
def test_a_decorative_piece_that_will_not_load_is_drawn_without_rather_than_raised_on(
    monkeypatch, piece
):
    """The opposite of the VS card's rule about its template, and for a
    reason: that card cannot exist without its background, where this one is
    drawn either way and a missing header or footer only costs it some polish.
    """
    monkeypatch.setattr(img, "_art_cache", {})
    monkeypatch.setitem(img.PICKS["templates"]["single"][piece], "art", "not_delivered.webp")
    assert img.render_slate(_stub_slate(2))


def test_a_row_band_that_will_not_decode_is_survived_too(monkeypatch, tmp_path):
    """`os.path.isfile` says a path exists, not that Pillow can read it. A
    truncated asset is the same failure as a missing one and must not be a
    different outcome."""
    (tmp_path / "truncated.webp").write_bytes(b"RIFF not really a webp")
    monkeypatch.setattr(img, "_art_cache", {})
    monkeypatch.setattr(img, "_ASSETS", str(tmp_path))
    monkeypatch.setitem(img._GEOM["row_band"], "art", "truncated.webp")
    # ...but only for the decorative pieces. The cap is loaded from the same
    # directory, so an unpredicted row is what proves the band alone survived.
    assert img.render_slate(_StubSlate([_StubPick("Kestrel", "Basalt", None)]))


def test_a_missing_pick_cap_fails_the_render_rather_than_dropping_every_pick(monkeypatch):
    """The one piece that is NOT optional. Without it a row loses the only
    thing that says who to back, which makes it indistinguishable from a
    meeting nobody could call -- a card that states something false rather than
    one that looks plain. Failing hands the surface back to the embed, which
    carries the same numbers.
    """
    monkeypatch.setattr(img, "_art_cache", {})
    monkeypatch.setitem(img.PICKS["cap"], "art", "not_delivered.webp")
    with pytest.raises(FileNotFoundError):
        img.render_slate(_stub_slate(2))


# ── The seam with the slate the surface actually passes ───────────────────────


def test_a_slate_built_by_picks_lib_renders(cd_db):
    """The stand-ins above are the renderer's contract as this file
    understands it. This is the one test that checks the understanding is
    right, and it is the one that fails if `Slate` or `Pick` change shape.
    """
    names = ("Ravenshade", "NightOwl", "Ironclad", "Vesper")
    slate = _slate(names, [(0, 1), (2, 3)])
    card = Image.open(io.BytesIO(img.render_slate(slate)))
    assert card.size == img.picks_size(len(slate.picks))
