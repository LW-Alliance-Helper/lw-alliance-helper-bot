"""The "Start a new league" guide and the paste box's game formats (#655).

An officer starting a league needs every alliance's tag and warzone, and the
game only shows a warzone three taps from the League screen. Two things make
that bearable, and both are pinned here:

- the paste box reads a line exactly as the game prints it, so nothing has to
  be retyped into the bot's own shape, and the ambiguity a comma and a slash
  bring (they are separators too) is settled by two narrow rules;
- a 📖 guide with the annotated screens sits beside ➕ Start a new league and
  on the message that refuses a paste.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import alliance_duel as ad
import alliance_duel_guide as guide
import alliance_duel_hub as hub
import config_health
import game_guide

# ── The paste box reads the game as printed ───────────────────────────────────


def _one(line):
    parse = ad.parse_bracket(line, expect=1)
    assert parse.ok, parse.problems
    return parse.entries[0]


def test_a_line_copied_off_the_alliance_page_is_one_alliance():
    entry = _one("<ToWR> #723 26,677,744,044 Lv.30 95/100")
    assert entry.alliance == ad.AllianceKey.of("ToWR", "723")
    assert (entry.power, entry.gift_level, entry.members) == (26_677_744_044, 30, 95)
    assert entry.warzone_display == "723"


def test_the_league_screens_square_brackets_still_read():
    assert _one("[Glo] 999").alliance == ad.AllianceKey.of("Glo", "999")


@pytest.mark.parametrize(
    "line, power",
    [
        ("Glo 999 361,867,727", 361_867_727),
        ("Glo 999 26,800,000,000", 26_800_000_000),
    ],
)
def test_power_with_thousands_separators_is_one_number(line, power):
    assert _one(line).power == power


def test_a_single_comma_group_is_still_a_separator():
    """`Glo,999,100` has always meant warzone 999 and power 100(M). Only a
    number of a million or more reads as grouped digits."""
    entry = _one("Glo,999,100")
    assert entry.alliance == ad.AllianceKey.of("Glo", "999")
    assert entry.power == ad.parse_power("100")


def test_members_as_the_game_prints_them():
    assert _one("Glo 999 26.8b 25 99 / 100").members == 99


def test_a_slash_pair_before_the_end_is_not_read_as_members():
    # Only the last field on the line is the member count; anything else keeps
    # the slash as a separator, as before.
    parse = ad.parse_bracket("Glo/999 26.8b", expect=1)
    assert parse.ok and parse.entries[0].alliance == ad.AllianceKey.of("Glo", "999")


@pytest.mark.parametrize("gift", ["Lv.30", "Lv 30", "lv30", "30"])
def test_gift_level_with_or_without_the_games_prefix(gift):
    assert _one(f"Glo 999 26.8b {gift} 95").gift_level == 30


def test_a_numbered_line_still_checks_its_number():
    parse = ad.parse_bracket("2 <Glo> #999", expect=1)
    assert not parse.ok and "numbered 2" in parse.problems[0]


# ── The guide ─────────────────────────────────────────────────────────────────


def test_every_screen_is_deployed_and_described():
    embeds, files = guide.build_guide()
    images = [s.image for s in guide.GUIDE_STEPS if s.image]
    assert [f.filename for f in files] == images
    # Alt text rides on every image (WCAG 2.2 AA 1.1.1).
    assert all(f.description for f in files)
    assert len(embeds) == len(guide.GUIDE_STEPS)


def test_the_guide_fits_in_one_discord_message():
    embeds, files = guide.build_guide()
    assert len(embeds) <= 10 and len(files) <= 10
    total = sum(len(e.title or "") + len(e.description or "") for e in embeds)
    assert total <= 6000


def test_the_example_is_invented_and_reads_back():
    """The line the guide tells people to type must be one the box accepts."""
    import examples

    entry = _one(guide._EXAMPLE_LINE)
    assert entry.alliance == ad.AllianceKey.of(examples.TAG, examples.WARZONE)


def test_a_missing_screen_keeps_the_words(monkeypatch):
    monkeypatch.setattr(guide, "_GUIDE_DIR", "/nonexistent/assets")
    embeds, files = guide.build_guide()
    assert files == []
    assert all(e.image.url is None for e in embeds)
    assert len(embeds) == len(guide.GUIDE_STEPS)


async def test_the_guide_is_sent_privately():
    inter = MagicMock()
    inter.response.send_message = AsyncMock()
    await guide.send_guide(inter)
    kwargs = inter.response.send_message.call_args.kwargs
    assert kwargs["ephemeral"] is True and len(kwargs["embeds"]) == len(guide.GUIDE_STEPS)


# ── Where it opens ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_recorded_sheet_problems(monkeypatch):
    monkeypatch.setattr(config_health, "problems_for_subjects", lambda *a, **k: [])


def _cfg():
    return {
        "guild_id": 1,
        "enabled": 1,
        "tab_name": "Alliance Duel (VS)",
        "own_tag": "Fre3",
        "own_warzone": "1234",
        "tracking_mode": ad.MODE_FULL_BRACKET,
    }


def _labels(state):
    return [c.label for c in hub.VSHubView(None, state, owner_id=7).children]


def test_the_guide_sits_beside_start_a_new_league():
    labels = _labels(hub.HubState(1, _cfg(), []))
    assert guide.VS_BTN_LEAGUE_GUIDE in labels
    at = labels.index(guide.VS_BTN_LEAGUE_GUIDE)
    assert labels[at - 1] == hub.ad_entry.VS_BTN_NEW_LEAGUE


def test_the_guide_is_gone_while_a_league_is_being_played():
    monday = ad.week_monday(ad.server_today())
    rows = [
        ad.AllianceWeek(
            league=ad.LeagueKey("S37", "Diamond", "12 - 4"),
            week=1,
            alliance=ad.AllianceKey.of("Fre3", "1234"),
            week_date=monday,
            ranking=1,
        )
    ]
    assert guide.VS_BTN_LEAGUE_GUIDE not in _labels(hub.HubState(1, _cfg(), rows))


# ── The shared builder ────────────────────────────────────────────────────────


def test_a_step_without_a_screen_is_words_only(tmp_path):
    steps = [game_guide.GuideStep(title="Then", body="Paste it.")]
    embeds, files = game_guide.build_guide(str(tmp_path), steps, footer="fine print")
    assert files == [] and embeds[0].description == "Paste it."
    assert embeds[0].footer.text == "fine print"
