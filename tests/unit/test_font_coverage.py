"""Every name the bot draws into an image renders, or fails visibly.

This is the test the font bug survived the absence of. `_font_for_text`
picked a font by the first non-Latin script range it recognised, the
range table only listed scripts somebody had already been bitten by,
and so every script nobody had hit yet silently claimed to be covered
by a 66 KB Latin subset. Nothing failed. The card just had boxes on it,
and you only find that by looking at a card with a Cyrillic name on it.

So: rasterise a corpus covering every script the player base writes in,
and assert no `.notdef` box comes out.

**The oracle is deliberately a second implementation.** It does not
import `storm_renderer._font_covers_char` — it rasterises a character,
rasterises a private-use codepoint no font assigns (a *different*
sentinel from the module's, on purpose), and compares the bitmaps. If
they match, the font drew its empty box. Sharing the module's coverage
check would let one bug hide itself in both places.

**The names are invented.** This repository is public and the roster is
real people. They are strings chosen to exercise glyph coverage, not
sentences — linguistic sense is not the property under test.

**What "the output" means here.** A rendered row is the concatenation
of exactly the glyph bitmaps checked below, drawn from exactly the font
`_font_for_text` returns. Asserting per glyph against the returned font
is asserting on the pixels, one step earlier and without a template
match over a 3800px canvas.
"""

import os

import pytest
from PIL import ImageFont

import storm_renderer as sr

# ── The oracle ───────────────────────────────────────────────────────

# Plane-15 private use, and NOT the codepoint `storm_renderer` probes
# with. Two independent sentinels agreeing is the point.
_ORACLE_SENTINEL = "\U000ffffd"


def _notdef(font) -> bytes:
    return bytes(font.getmask(_ORACLE_SENTINEL))


def _drawn_glyphs(font, text: str) -> tuple[list[str], list[str]]:
    """`(drawn, tofu)` — the characters of `text` this font renders as
    a real glyph, and the ones it renders as its empty box."""
    import unicodedata

    box = _notdef(font)
    drawn, tofu = [], []
    for ch in text:
        if unicodedata.category(ch) in ("Cf", "Cc"):
            continue  # no glyph in any font; nothing to draw or miss
        try:
            is_tofu = bytes(font.getmask(ch)) == box
        except Exception:  # noqa: BLE001 - unrenderable counts as tofu
            is_tofu = True
        (tofu if is_tofu else drawn).append(ch)
    return drawn, tofu


def _codepoints(chars) -> str:
    return ", ".join(f"U+{ord(c):04X}" for c in chars)


# ── The corpus ───────────────────────────────────────────────────────

# One invented name per script, ordered by the player population behind
# it (LWServers country counts). The three at the bottom are not scripts
# — they are how players style a plain Latin handle, and they were the
# single largest slice of the names no bundled font could draw.
CORPUS = {
    "Latin": "Ashvale",
    "Latin-1 accents": "Rooijmüller",
    "Latin Extended-A (Turkish)": "Yıldırgün Işık",
    "Latin Extended-A (Polish)": "Wąsławek Łódź",
    "Latin Extended-A (Czech / Romanian)": "Břeštăn Șerbu",
    "Cyrillic": "Волмирев",
    "Cyrillic (Ukrainian і)": "Трохіменко",
    "Cyrillic (Macedonian Ѕ)": "Ѕвонимир",
    "Greek": "Θρακοπούλης",
    "Vietnamese": "Nguyễn Đặng",
    "Arabic": "الرمالي",
    "Hebrew": "אבנרון",
    "Thai": "ธนวัฒน",
    "Devanagari": "अजयवर्धन",
    "Tamil": "அருள்மொழி",
    "Bengali": "অমিতরঞ্জ",
    "Georgian": "ვახტანგი",
    "Armenian": "Վարդանյան",
    "Khmer": "សុវណ្ណារី",
    "Lao": "ສີສະຫວາດ",
    "Myanmar": "ကျော်စွာ",
    "Sinhala": "නිමලසිරි",
    "Japanese": "山風たろう",
    "Korean": "한별진",
    "Chinese": "沈青岚",
    "Hangul compatibility jamo": "Ashvaleㅇ",
    "Small-caps styling": "ᴀsʜᴠᴀʟᴇ",
    "Modifier letters": "ᴬˢʰᵛᵃˡᵉ",
}

# Scripts the three fonts committed to this repo cover on their own.
# Everything else needs `fonts-noto-core` in the deploy image, so on a
# checkout without it those corpus rows are best-effort rather than
# clean. Kept explicit: a name moving out of this set is a change to
# what the bot can draw with nothing installed.
BUNDLED_CLEAN = {
    "Latin",
    "Latin-1 accents",
    "Latin Extended-A (Turkish)",
    "Latin Extended-A (Polish)",
    "Latin Extended-A (Czech / Romanian)",
    "Arabic",
    "Japanese",
    "Korean",
    "Chinese",
    "Hangul compatibility jamo",
}

# CI installs the same package the deploy image does and sets this, so
# the corpus is asserted in full somewhere rather than only where a
# developer happens to have fonts. See `.github/workflows/test.yml`.
_EXPECT_SYSTEM_FONTS = os.environ.get("EXPECT_SYSTEM_FONTS") == "1"


@pytest.fixture
def bundled_only(monkeypatch):
    """Only the fonts committed to this repo resolve.

    Without this the same test says different things on a developer's
    Windows box and on a Linux runner that happens to carry DejaVu.
    """
    monkeypatch.setattr(sr, "_SYSTEM_FONT_DIRS", ())
    sr._font_file.cache_clear()
    yield
    sr._font_file.cache_clear()


# ── The deliverable ──────────────────────────────────────────────────


class TestNoTofuInTheOutput:
    """No name in the corpus renders as empty boxes."""

    @pytest.mark.parametrize("script", sorted(CORPUS))
    def test_the_chosen_font_draws_the_whole_name(self, script):
        """Where SOME font in the stack can draw the name, the router
        must return that font.

        This is the bug, stated as an assertion. Every failure the
        measurement found was a name a bundled font could draw perfectly
        well and the router never offered it to.
        """
        name = CORPUS[script]
        best = max(
            (len(_drawn_glyphs(f, name)[0]) for f in _every_available_font()),
            default=0,
        )
        chosen = sr._font_for_text(name, 24)
        drawn, tofu = _drawn_glyphs(chosen, name)
        assert len(drawn) >= best, (
            f"{script}: a font in the stack draws {best} characters of {name!r} "
            f"and the router picked one that draws {len(drawn)}. "
            f"Boxes at {_codepoints(tofu)}."
        )

    @pytest.mark.parametrize("script", sorted(BUNDLED_CLEAN))
    def test_bundled_fonts_alone_draw_these_cleanly(self, script, bundled_only):
        """With nothing installed — a stripped image, a developer's
        laptop — these scripts still come out clean, because the files
        that draw them are committed to the repo."""
        name = CORPUS[script]
        _drawn, tofu = _drawn_glyphs(sr._font_for_text(name, 24), name)
        assert not tofu, f"{script}: {name!r} has boxes at {_codepoints(tofu)}"

    @pytest.mark.skipif(
        not _EXPECT_SYSTEM_FONTS,
        reason="needs fonts-noto-core installed; CI sets EXPECT_SYSTEM_FONTS=1",
    )
    @pytest.mark.parametrize("script", sorted(CORPUS))
    def test_every_script_is_clean_on_the_deploy_image(self, script):
        """The whole corpus, with the packages the deploy image installs.

        Skipped on a machine without them, which is why the CI workflow
        installs them: the assertion has to run somewhere.
        """
        name = CORPUS[script]
        _drawn, tofu = _drawn_glyphs(sr._font_for_text(name, 24), name)
        assert not tofu, f"{script}: {name!r} has boxes at {_codepoints(tofu)}"

    @pytest.mark.skipif(
        not _EXPECT_SYSTEM_FONTS,
        reason="needs fonts-noto-core installed; CI sets EXPECT_SYSTEM_FONTS=1",
    )
    def test_the_deploy_image_is_missing_no_family(self):
        """Guards the CI step itself. If the package silently stops
        installing, this fails instead of the skips above quietly
        turning the corpus test off."""
        assert sr.log_font_coverage() == []


def _every_available_font():
    for family in sr._FONT_STACK:
        path = sr._family_font_file(family, bold=False)
        if path is not None:
            yield ImageFont.truetype(path, 24)


# ── Picking, not guessing ────────────────────────────────────────────


class TestWholeStringPicking:
    """The routing half of the fix: one font, chosen because it covers
    the entire string rather than because a range table recognised the
    first non-Latin character in it."""

    def test_a_latin_name_stays_on_the_project_face(self, bundled_only):
        assert sr._family_for_text("Ashvale")[0] == "inter"
        assert sr._family_for_text("Member 5")[0] == "inter"
        assert sr._family_for_text("Rooijmüller")[0] == "inter"

    def test_inter_is_a_latin_subset_and_the_router_knows_it(self, bundled_only):
        """The docstring this replaces claimed Inter covered Cyrillic
        and Greek. The bundled file is 66 KB and has neither, which is
        why Russian names were boxes for a year."""
        inter = ImageFont.truetype(sr._INTER_REGULAR, 16)
        _drawn, tofu = _drawn_glyphs(inter, "Волмирев")
        assert tofu, "Inter has grown Cyrillic — re-measure the stack order"
        assert sr._family_for_text("Волмирев")[0] != "inter"

    def test_a_turkish_name_does_not_land_on_a_latin_subset(self, bundled_only):
        """`ı ğ ş` are ordinary letters to about 45,000 players and the
        bundled Inter has three characters of Latin Extended-A —
        `ı` among them, `ş` and `ğ` not."""
        inter = ImageFont.truetype(sr._INTER_REGULAR, 16)
        assert not _drawn_glyphs(inter, "şğ")[0], "Inter has grown Latin Extended-A"
        assert sr._family_for_text("Yıldırgün Işık")[0] != "inter"

    def test_the_broad_latin_face_outranks_the_arabic_one(self):
        """Order, not membership, is what this fix turns on. Noto Sans
        Arabic happens to carry all of Latin Extended-A, so with the
        stack in the wrong order a Turkish name renders correctly in a
        completely different typeface from the name beside it."""
        keys = [f.key for f in sr._FONT_STACK]
        assert keys.index("noto") < keys.index("arabic")
        assert keys[0] == "inter"

    def test_a_mixed_name_picks_the_font_covering_both_halves(self, bundled_only):
        assert sr._family_for_text("Ashvale 한별진")[0] == "cjk"
        assert sr._family_for_text("한별진 Ashvale")[0] == "cjk"

    def test_an_empty_name_asks_for_nothing(self):
        assert sr._family_for_text("") == (None, None)
        assert sr._font_for_text("", 16) is not None

    def test_the_contract_still_returns_one_font(self):
        """Per-character fallback was measured and rejected: ten more
        names out of 3,689, in exchange for changing every place a name
        is measured. `champion_duel_image.py` imports this function and
        must not need to know any of it."""
        font = sr._font_for_text("Ashvale 한별진 الرمالي", 24)
        assert hasattr(font, "getlength")
        assert font.size == 24


class TestBoldRequests:
    def test_the_project_face_has_a_bold_weight(self, bundled_only):
        regular = sr._font_for_text("Ashvale", 24, bold=False)
        bold = sr._font_for_text("Ashvale", 24, bold=True)
        assert os.path.basename(regular.path) == "Inter-Regular.ttf"
        assert os.path.basename(bold.path) == "Inter-Bold.ttf"

    def test_a_regular_only_family_answers_bold_with_regular(self, bundled_only):
        """The bundled CJK file ships Regular only — a Bold weight would
        double a 16 MB asset for a case the cards barely use. A bold
        request must degrade, not fall through to a font that cannot
        draw the name."""
        font = sr._font_for_text("한별진", 24, bold=True)
        assert os.path.basename(font.path) == "NotoSansCJKsc-Regular.otf"


# ── Best effort, never boxes ─────────────────────────────────────────


class TestNothingCoversIt:
    """The last resort. The caption beside every card carries the name
    exactly — `prediction_caption` for the VS card, `CAPTION_ROW` for
    picks — so a name we cannot fully draw is unreadable in the PNG and
    perfectly readable in the message next to it. Drawing what we can
    beats a row of boxes."""

    def test_it_draws_the_half_it_can(self, bundled_only):
        # Latin plus a script nothing bundled covers.
        name = "Ashvale ธนวัฒน"
        drawn, tofu = _drawn_glyphs(sr._font_for_text(name, 24), name)
        assert "A" in drawn and "e" in drawn
        assert tofu, "test premise: nothing bundled draws Thai"

    def test_it_picks_the_font_that_draws_the_most(self, bundled_only):
        """Two scripts, no single font covering both — the measurement
        found real names like this, Arabic beside Hangul, both fully
        covered by files already in this repo and neither by one file.
        Whichever half is longer is the one that survives.

        Counted over the string, not over its distinct characters: six
        Hangul syllables that repeat still outweigh four Arabic letters
        that do not.
        """
        assert sr._family_for_text("한별진한별진한별진한 الرمالي")[0] == "cjk"
        assert sr._family_for_text("한별 الرمالي الرمالي")[0] == "arabic"

    def test_it_says_so_in_the_log(self, bundled_only, caplog):
        import logging

        sr._log_undrawable.cache_clear()
        with caplog.at_level(logging.INFO, logger="storm_renderer"):
            sr._font_for_text("ธนวัฒน", 24)
        assert "U+0E18" in caplog.text

    def test_the_log_carries_codepoints_not_the_name(self, bundled_only, caplog):
        """A player's name is theirs. The codepoint is the part that
        says which font is missing."""
        import logging

        sr._log_undrawable.cache_clear()
        with caplog.at_level(logging.INFO, logger="storm_renderer"):
            sr._font_for_text("ธนวัฒน", 24)
        assert "ธนวัฒน" not in caplog.text

    def test_a_missing_package_asks_for_a_package(self, bundled_only, caplog):
        """Nothing in the stack draws Thai here, so the line names the
        codepoint and asks for the family."""
        import logging

        sr._log_undrawable.cache_clear()
        with caplog.at_level(logging.INFO, logger="storm_renderer"):
            sr._font_for_text("ธนวัฒน", 24)
        assert "no installed font draws" in caplog.text
        assert "Add the family" in caplog.text

    def test_a_mixed_name_does_not_ask_for_a_package(self, bundled_only, caplog):
        """Arabic beside Hangul: both files are in this repo and no
        package fixes it, because no single font covers both. Telling
        an operator to install something would send them after a font
        that does not exist.
        """
        import logging

        sr._log_undrawable.cache_clear()
        with caplog.at_level(logging.INFO, logger="storm_renderer"):
            sr._font_for_text("한별진한별진한별진한 الرمالي", 24)
        assert "Add the family" not in caplog.text
        assert "no single font draws this whole name" in caplog.text
        assert "Every font it needs is installed" in caplog.text


# ── Finding the files ────────────────────────────────────────────────


class TestFontResolution:
    def test_a_bundled_file_wins_over_an_installed_one(self, tmp_path, monkeypatch):
        """`assets/fonts/NotoSansArabic-Regular.ttf` is committed and
        `fonts-noto-core` installs a file of the same name. Which one
        renders must not depend on the base image."""
        (tmp_path / "NotoSansArabic-Regular.ttf").write_bytes(b"not a font")
        monkeypatch.setattr(sr, "_SYSTEM_FONT_DIRS", (str(tmp_path),))
        sr._font_file.cache_clear()
        try:
            resolved = sr._font_file("NotoSansArabic-Regular.ttf")
            assert resolved == sr._NOTO_ARABIC_REGULAR
        finally:
            sr._font_file.cache_clear()

    def test_an_absent_family_is_skipped_not_fatal(self, bundled_only):
        assert sr._family_font_file(sr._family_of("thai"), bold=False) is None
        assert sr._font_for_text("Ashvale", 24) is not None

    def test_a_file_that_will_not_open_is_not_a_crash(self, tmp_path):
        bad = tmp_path / "broken.ttf"
        bad.write_bytes(b"\x00\x01truncated")
        assert sr._load_font(str(bad), 16) is None
        assert sr._notdef_bitmap(str(bad)) is None
        assert sr._font_covers_char(str(bad), "A") is False

    def test_fonts_are_cached_by_path_and_size(self):
        sr._load_font.cache_clear()
        a = sr._load_font(sr._INTER_REGULAR, 20)
        b = sr._load_font(sr._INTER_REGULAR, 20)
        c = sr._load_font(sr._INTER_REGULAR, 21)
        assert a is b
        assert a is not c

    def test_the_render_cache_stays_small(self):
        """A held FreeType face costs about 1.2 MB per SIZE for the
        bundled CJK file — the glyph cache is per size, not per file.
        Railway memory is roughly 83% of the hosting bill against 1% for
        CPU, and `champion_duel_image._fit` walks 28 sizes looking for
        one that fits. An unbounded cache here is a memory leak with a
        polite name.
        """
        assert sr._load_font.cache_info().maxsize <= 16

    def test_coverage_probing_reuses_one_font_per_file(self):
        a = sr._probe_font(sr._INTER_REGULAR)
        b = sr._probe_font(sr._INTER_REGULAR)
        assert a is b
        assert a.size == sr._FONT_PROBE_PX

    def test_a_name_nothing_covers_does_not_pin_the_whole_stack(self, bundled_only):
        """An emoji in a name — the everyday case of a character no
        family draws — makes the router walk all sixteen families
        looking for the one that draws the most of it. With an
        unbounded probe cache that walk would pin every face, the
        16 MB CJK file included, for the life of the process. Measured
        at about 5 MB, permanently, off one player's name.
        """
        sr._probe_font.cache_clear()
        sr._font_covers_char.cache_clear()
        sr._font_for_text("Ashvale\U0001f525", 24)
        held = sr._probe_font.cache_info().currsize
        assert held <= sr._PROBE_FONT_ENTRIES
        assert held < len(sr._FONT_STACK)

    def test_a_latin_name_never_opens_the_cjk_file(self, bundled_only, monkeypatch):
        """Lazily, and only what the name needs. Inter covers the
        common case, so the 16 MB file is never read."""
        from PIL import ImageFont

        sr._load_font.cache_clear()
        sr._probe_font.cache_clear()
        sr._notdef_bitmap.cache_clear()
        sr._font_covers_char.cache_clear()

        opened = []
        real = ImageFont.truetype

        def _record(path, *a, **kw):
            opened.append(str(path))
            return real(path, *a, **kw)

        monkeypatch.setattr(ImageFont, "truetype", _record)
        sr._font_for_text("Ashvale", 24)
        assert opened, "test premise: something was loaded"
        assert not any("CJK" in p for p in opened), opened


# ── The install has to be visible ────────────────────────────────────


class TestStartupCheck:
    """An environment change fails silently. A build that drops
    `fonts-noto-core` brings the boxes straight back with no error
    anywhere, and the last time that class of thing happened it was
    found from a screenshot."""

    def test_it_names_the_families_that_are_missing(self, bundled_only, capsys):
        missing = sr.log_font_coverage()
        assert "thai" in missing
        assert "noto" in missing
        assert "inter" not in missing
        assert "fonts-noto-core" in capsys.readouterr().out

    def test_it_shouts_when_a_bundled_font_is_gone(self, tmp_path, monkeypatch, capsys):
        """A committed font missing is a broken deploy, not a missing
        package, and reads differently in the log."""
        monkeypatch.setattr(sr, "_BUNDLED_FONT_DIR", str(tmp_path))
        monkeypatch.setattr(sr, "_SYSTEM_FONT_DIRS", ())
        sr._font_file.cache_clear()
        try:
            missing = sr.log_font_coverage()
            out = capsys.readouterr().out
            assert "inter" in missing
            assert "MISSING bundled font Inter-Regular.ttf" in out
        finally:
            sr._font_file.cache_clear()

    def test_it_does_not_load_a_single_font(self, monkeypatch):
        """Resolves paths only. Loading the family at import is what
        would put 16 MB of CJK in every container that never draws a
        CJK name."""
        from PIL import ImageFont

        def _explode(*_a, **_kw):
            raise AssertionError("log_font_coverage loaded a font file")

        monkeypatch.setattr(ImageFont, "truetype", _explode)
        sr.log_font_coverage()

    def test_the_bot_calls_it_at_boot(self):
        import re

        source = open("bot.py", encoding="utf-8").read()
        assert re.search(r"log_font_coverage\(\)", source), (
            "on_ready must call storm_renderer.log_font_coverage() — "
            "otherwise a deploy that loses the fonts says nothing"
        )


# ── The deploy config is part of the fix ─────────────────────────────


class TestDeployConfig:
    """The coverage half of this fix is an installed package, so the
    package name is load-bearing code. Both builder configs are shipped
    because this repo has no `railway.json` pinning which builder runs:
    Nixpacks reads one, Railpack the other, and each ignores the other's.
    """

    def test_nixpacks_installs_the_font_package(self):
        toml = open("nixpacks.toml", encoding="utf-8").read()
        assert "fonts-noto-core" in toml
        assert "'...'" in toml or '"..."' in toml, (
            "without the spread the provider's own apt packages are replaced"
        )

    def test_railpack_installs_the_font_package(self):
        import json

        config = json.load(open("railpack.json", encoding="utf-8"))
        packages = config["deploy"]["aptPackages"]
        assert "fonts-noto-core" in packages
        assert "..." in packages

    def test_ci_installs_what_the_deploy_image_installs(self):
        """The corpus assertion above only runs where the fonts are.
        If CI and production drift apart on the package name, the test
        that proves the fix silently stops covering it."""
        workflow = open(".github/workflows/test.yml", encoding="utf-8").read()
        assert "fonts-noto-core" in workflow
        assert "EXPECT_SYSTEM_FONTS" in workflow

    def test_every_corpus_script_has_a_family_in_the_stack(self):
        """A script in the corpus with no family behind it can never
        come out clean, however the deploy is configured."""
        keys = {f.key for f in sr._FONT_STACK}
        for expected in (
            "noto",
            "cjk",
            "arabic",
            "thai",
            "devanagari",
            "tamil",
            "hebrew",
            "georgian",
            "bengali",
            "khmer",
            "myanmar",
            "lao",
            "armenian",
            "sinhala",
        ):
            assert expected in keys
