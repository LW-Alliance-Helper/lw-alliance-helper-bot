"""Unit tests for the VS setup surfaces (#399).

The embed builders are pure functions, so these assert on copy directly with
no interaction, guild or network. The checks that matter are the contract
ones: terminology, the choice-versus-missing-data distinction, and the
clamping that keeps a systematic sheet mistake from blowing Discord's limits.
"""

import discord
import pytest

import alliance_duel as ad
import alliance_duel_setup as ads


def _finding(rule=1, severity=ad.SEVERITY_ERROR, row=5, message="Something is off."):
    return ad.Finding(
        rule=rule, severity=severity, message=message, row_number=row, column=ad.COL_WEEK_SCORE
    )


# ── Terminology (UX.md glossary) ──────────────────────────────────────────────


def _all_text(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f.name or "" for f in embed.fields]
    parts += [f.value or "" for f in embed.fields]
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


@pytest.mark.parametrize(
    "embed",
    [
        ads.tracking_mode_embed(),
        ads.column_guide_embed(),
        ads.validation_report_embed([]),
        ads.validation_report_embed([_finding()]),
    ],
)
def test_no_em_dashes_in_user_facing_copy(embed):
    # UX.md Voice: if a user can see it, no em dashes.
    assert "—" not in _all_text(embed)


@pytest.mark.parametrize(
    "embed",
    [ads.tracking_mode_embed(), ads.column_guide_embed(), ads.validation_report_embed([])],
)
def test_copy_never_says_guild(embed):
    # "guild" is the discord.py term and never appears in user copy.
    assert "guild" not in _all_text(embed).lower()


def test_the_column_guide_says_warzone_and_explains_it_once():
    text = _all_text(ads.column_guide_embed())
    assert "Warzone" in text
    # Players say "server" colloquially, so it is acknowledged exactly once
    # rather than left for them to work out.
    assert "you may call it the server" in text


def test_no_internals_leak_into_copy():
    # No column names from the DB, table names, or _id suffixes.
    for embed in (ads.tracking_mode_embed(), ads.column_guide_embed()):
        text = _all_text(embed)
        for leak in ("guild_vs_config", "tracking_mode", "own_warzone", "_id", "AllianceWeek"):
            assert leak not in text


# ── The mode question and its upsell ──────────────────────────────────────────


def test_the_mode_question_is_asked_not_inferred():
    text = _all_text(ads.tracking_mode_embed())
    assert ads.TRACKING_MODE_QUESTION in text
    assert "change this at any time" in text


def test_own_alliance_is_presented_as_supported_not_lesser():
    # #448: it is a choice, and the copy must not frame it as incomplete.
    text = _all_text(ads.tracking_mode_embed()).lower()
    for word in ("incomplete", "limited", "missing", "only option", "downgrade"):
        assert word not in text


def test_the_upsell_names_what_the_bracket_actually_buys():
    text = _all_text(ads.tracking_mode_embed()).lower()
    assert "projected path" in text
    assert "scout" in text


# ── Choice versus missing data ────────────────────────────────────────────────


def test_a_tracking_mode_choice_gets_an_upsell_not_an_error():
    reason = ad.BracketIncomplete(reason="own_alliance_mode", detail="tracking own alliance")
    embed = ads.upsell_embed(reason)
    assert embed.color == discord.Color.blurple()
    text = _all_text(embed).lower()
    assert "error" not in text
    assert "switch any time" in text


def test_genuinely_missing_data_prompts_action():
    reason = ad.BracketIncomplete(reason="roster_size", detail="Only 4 alliances are recorded.")
    embed = ads.upsell_embed(reason)
    assert embed.color == discord.Color.orange()
    text = _all_text(embed)
    assert "Only 4 alliances are recorded." in text
    assert "Add the missing alliances" in text


def test_the_two_cases_do_not_share_wording():
    # They are different messages on purpose: only one should prompt action.
    choice = _all_text(ads.upsell_embed(ad.BracketIncomplete("own_alliance_mode", "x")))
    missing = _all_text(ads.upsell_embed(ad.BracketIncomplete("roster_size", "y")))
    assert choice != missing


# ── Validation report ─────────────────────────────────────────────────────────


def test_a_clean_sheet_says_the_check_actually_ran():
    embed = ads.validation_report_embed([], rows_checked=64)
    assert embed.color == discord.Color.green()
    assert "64 rows" in _all_text(embed)


def test_a_clean_own_alliance_sheet_says_which_checks_were_skipped():
    embed = ads.validation_report_embed([], tracking_mode=ad.MODE_OWN_ALLIANCE, rows_checked=4)
    text = _all_text(embed)
    assert "skipped" in text
    # It must read as a consequence of their choice, not as a gap.
    assert "Tracking just your alliance" in text


def test_findings_name_where_to_look():
    embed = ads.validation_report_embed([_finding(row=42)])
    assert f"row 42, column {ad.COL_WEEK_SCORE}" in _all_text(embed)


def test_errors_render_red_and_warnings_orange():
    err = ads.validation_report_embed([_finding(severity=ad.SEVERITY_ERROR)])
    warn = ads.validation_report_embed([_finding(severity=ad.SEVERITY_WARNING)])
    assert err.color == discord.Color.red()
    assert warn.color == discord.Color.orange()


def test_the_report_is_clamped_and_honest_about_it():
    # A systematic mistake across a league produces hundreds. The embed must
    # survive it, and must not pretend it showed everything.
    findings = [_finding(row=i) for i in range(200)]
    embed = ads.validation_report_embed(findings)
    text = _all_text(embed)
    assert f"and {200 - ads.MAX_FINDINGS_SHOWN} more" in text
    assert len(embed.description) <= 4096


def test_the_report_survives_a_pathological_finding_count():
    # Belt and braces on the hard 4096 cap, with long messages too.
    findings = [_finding(row=i, message="x" * 300) for i in range(200)]
    embed = ads.validation_report_embed(findings)
    assert len(embed.description) <= 4096


def test_the_summary_counts_errors_and_warnings_separately():
    findings = [
        _finding(severity=ad.SEVERITY_ERROR),
        _finding(severity=ad.SEVERITY_ERROR),
        _finding(severity=ad.SEVERITY_WARNING),
    ]
    text = _all_text(ads.validation_report_embed(findings))
    assert "2 things to fix" in text
    assert "1 worth a look" in text


def test_embeds_stay_inside_discords_total_character_budget():
    for embed in (
        ads.tracking_mode_embed(),
        ads.column_guide_embed(),
        ads.column_guide_embed(tracking_mode=ad.MODE_OWN_ALLIANCE),
        ads.validation_report_embed([_finding(row=i) for i in range(200)]),
    ):
        assert len(_all_text(embed)) <= 6000
        assert len(embed.title) <= 256
        for field in embed.fields:
            assert len(field.name) <= 256
            assert len(field.value) <= 1024


# ── Route back ────────────────────────────────────────────────────────────────


def test_the_route_back_names_the_button_not_just_the_command():
    # UX.md principle 3: "/setup" alone is useless when it has fourteen buttons.
    from setup_hub import HUB_BTN_VS

    assert HUB_BTN_VS in ads.VS_SETUP_NAV
    assert "/setup" in ads.VS_SETUP_NAV


def test_the_vs_button_label_is_imported_not_retyped():
    # DESIGN.md hard rule: a label referenced from another module is a
    # constant, so a rename stays one line.
    import setup_hub

    assert ads.HUB_BTN_VS is setup_hub.HUB_BTN_VS
    assert setup_hub.HUB_BTN_VS.startswith("🏆 ")


# ── Sheet health (#413 / #414 pattern) ────────────────────────────────────────


def test_the_vs_tab_is_registered_as_a_fixable_subject():
    import config_health

    subject = config_health.get_subject(ads.VS_SHEET_SUBJECT)
    # The label is what the alliance calls it, not what the code calls it.
    assert subject.label == "your Alliance Duel (VS) tab"
    assert "vs_config" not in subject.label
    # The fix has to name the surface that actually fixes this subject.
    assert subject.fix_btn == ads.HUB_BTN_VS
    assert subject.fix_hub == "/setup"


def test_a_clean_read_clears_the_subject(monkeypatch):
    import config_health

    calls = {"cleared": 0}
    monkeypatch.setattr(ads, "ensure_tab", lambda *a, **k: _FakeWorksheet())
    monkeypatch.setattr(config_health, "clear", lambda *a, **k: calls.__setitem__("cleared", 1))
    import config

    monkeypatch.setattr(config, "get_spreadsheet", lambda gid: object())

    rows = ads.load_rows(1234)
    assert rows == []
    assert calls["cleared"] == 1


def test_an_alliance_owned_failure_is_reported_not_raised(monkeypatch):
    import config_health

    recorded = {}

    def _boom(*a, **k):
        raise RuntimeError("tab gone")

    monkeypatch.setattr(ads, "ensure_tab", _boom)
    monkeypatch.setattr(
        config_health,
        "record_sheet_failure",
        lambda gid, subj, e, **k: recorded.setdefault("subject", subj) or True,
    )
    import config

    monkeypatch.setattr(config, "get_spreadsheet", lambda gid: object())

    # None, not [], because "couldn't read" and "empty sheet" are different
    # states and rendering an empty bracket as fact would be worse.
    assert ads.load_rows(1234) is None
    assert recorded["subject"] == ads.VS_SHEET_SUBJECT


def test_a_bot_bug_still_raises(monkeypatch):
    import config_health

    def _boom(*a, **k):
        raise RuntimeError("not a sheet problem")

    monkeypatch.setattr(ads, "ensure_tab", _boom)
    # record_sheet_failure returning False means "not the alliance's to fix",
    # so the caller keeps its existing Sentry behaviour rather than swallowing.
    monkeypatch.setattr(config_health, "record_sheet_failure", lambda *a, **k: False)
    import config

    monkeypatch.setattr(config, "get_spreadsheet", lambda gid: object())

    with pytest.raises(RuntimeError):
        ads.load_rows(1234)


class _FakeWorksheet:
    def get_all_values(self):
        return [list(ad.SHEET_COLUMNS)]
