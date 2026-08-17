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


def test_the_column_guide_names_warzone_without_glossing_it():
    text = _all_text(ads.column_guide_embed())
    assert "Warzone" in text
    # Warzone is the game's own word, and UX.md exempts the game's vocabulary
    # from the define-it-or-drop-it rule. Glossing it spent three lines on the
    # one column nobody has to be taught.
    assert "you may call it the server" not in text


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


# ── /setup grid and /help wiring ──────────────────────────────────────────────


def test_the_vs_button_is_premium_gated_on_the_free_tier():
    import setup_hub

    free = setup_hub._SetupHubView(None, 1, 1, is_premium=False)
    # DESIGN.md: locked controls render disabled, not hidden, so the free tier
    # can see the shape of the paid product.
    assert free.btn_vs.disabled is True
    assert free.btn_vs.label.startswith("💎")
    # Disabled, but still rendered. Hiding is reserved for deploy-flagged
    # surfaces, which is a different thing.
    assert free.btn_vs in free.children


def test_the_vs_button_is_live_on_premium():
    import setup_hub

    paid = setup_hub._SetupHubView(None, 1, 1, is_premium=True)
    assert paid.btn_vs.disabled is False
    assert paid.btn_vs.label == setup_hub.HUB_BTN_VS


def test_the_vs_button_sits_in_the_combat_events_row():
    import setup_hub

    view = setup_hub._SetupHubView(None, 1, 1, is_premium=True)
    assert view.btn_vs.row == 2, "VS belongs with the other event features"
    per_row = {}
    for child in view.children:
        per_row[child.row] = per_row.get(child.row, 0) + 1
    # Five buttons per row is Discord's cap; 25 components total.
    assert all(count <= 5 for count in per_row.values())
    assert len(view.children) <= 25


def test_the_setup_grid_reports_vs_state():
    import setup_hub
    import inspect

    source = inspect.getsource(setup_hub)
    # Shown with the 💎 marker like the other Premium features, so a free-tier
    # reader sees it exists rather than wondering where it went.
    assert "_premium(vs_on)} Alliance Duel (VS)" in source


def test_help_has_a_vs_category_using_the_shared_label():
    import help_content
    from setup_hub import HUB_BTN_VS

    cat = help_content.HELP_CATEGORIES["alliance_duel"]
    assert cat["emoji"] == "🏆"
    assert "💎" in cat["label"]
    # The route in is the imported constant, not a retyped string.
    assert any(HUB_BTN_VS in cmd for cmd, _desc in cat["commands"])


def test_help_copy_avoids_the_reserved_word_server():
    import help_content

    cat = help_content.HELP_CATEGORIES["alliance_duel"]
    text = cat["description"] + " ".join(d for _c, d in cat["commands"])
    assert "guild" not in text.lower()
    assert "—" not in text


def test_the_wizard_imports_and_exposes_its_entry_point():
    import alliance_duel_wizard as w

    assert callable(w.run_vs_setup)
    # Wizard steps get the typing-and-thought timeout tier, not the 120s
    # confirm tier.
    assert w.STEP_TIMEOUT == 300


def test_timeout_hints_name_the_button_not_just_the_command():
    # UX.md principle 3: "/setup" alone is useless when it has fourteen
    # buttons. expire_view_message's own docstring shows the expected form.
    import inspect

    import alliance_duel_wizard as w

    source = inspect.getsource(w)
    assert 'command_hint="/setup"' not in source
    # Every view that can expire routes back the same way. Asserted as a rule
    # rather than a count, so adding a view doesn't fail this for the wrong
    # reason: what matters is that no view opts out.
    hints = source.count("command_hint=")
    assert hints >= 3, "expected a hint on every expiring view"
    assert source.count("command_hint=ads.VS_SETUP_NAV") == hints


def test_recovery_copy_says_start_again_once_the_flow_has_ended():
    # "try again" is for retyping one input with the flow still alive.
    # Both of these sit after the wizard has ended.
    import inspect

    import alliance_duel_wizard as w

    source = inspect.getsource(w)
    assert "to try again" not in source
    assert source.count("to start again") == 2


def test_wizard_button_labels_fit_on_mobile():
    import alliance_duel_setup as a_setup
    import alliance_duel_wizard as w

    for label in (
        "🏆 Set up Alliance Duel (VS)",
        "✏️ Change alliance or mode",
        a_setup.MODE_BTN_OWN,
        a_setup.MODE_BTN_FULL,
    ):
        assert len(label) <= 35, f"{label!r} is {len(label)} chars"
    assert w.STEP_TIMEOUT == 300


def test_the_mode_choice_buttons_carry_no_emoji():
    """They are alternatives to each other inside one question, not features.

    Two glyphs meaning the same kind of thing cost scan time and return
    nothing, and a repeated glyph is worse than none. Matches the
    export/import choice cluster. Pinned because "add an emoji, everything
    else has one" is the obvious wrong fix.
    """
    import re

    emoji = re.compile("[\U0001f300-\U0001faff\U00002600-\U000027bf\U00002b00-\U00002bff]")
    assert not emoji.search(ads.MODE_BTN_OWN)
    assert not emoji.search(ads.MODE_BTN_FULL)
    assert ads.MODE_BTN_OWN == "Just my alliance"
    assert ads.MODE_BTN_FULL == "My whole League bracket"


def test_cancel_matches_every_other_cancel_in_the_bot():
    # Bare in bot_admin, buddy_hub, donate, export_import_cog and
    # mapmanager_hub. The catalog's ↩️ marks cancelled-a-sub-step in message
    # copy, not the button that does it.
    import inspect

    import alliance_duel_wizard as w

    assert 'label="Cancel"' in inspect.getsource(w)
    assert "↩️ Cancel" not in inspect.getsource(w)


# ── Mid-league switch to full bracket (#448) ─────────────────────────────────


def _missing():
    import datetime as _dt

    return {1: (14, _dt.date(2026, 8, 3)), 2: (14, None)}


def test_the_offer_is_honest_about_what_the_rows_contain():
    league = ad.LeagueKey("S35", "Diamond", "12 - 2")
    text = _all_text(ads.fill_bracket_embed(league, _missing()))
    # It must not read as though the bot will fill the bracket in for them.
    assert "blank rows" in text
    assert "tag, warzone and seed" in text
    # One screen, one name. The game titles it "Alliance Duel League", so
    # every surface says League screen.
    assert "in-game League screen" in text


def test_the_offer_counts_the_rows_and_names_the_weeks():
    league = ad.LeagueKey("S35", "Diamond", "12 - 2")
    text = _all_text(ads.fill_bracket_embed(league, _missing()))
    assert "28 rows" in text
    assert "week 1, week 2" in text
    assert "S35" in text


def test_the_offer_says_nothing_is_removed():
    league = ad.LeagueKey("S35", "Diamond", "12 - 2")
    text = _all_text(ads.fill_bracket_embed(league, _missing()))
    assert "Nothing is removed" in text
    assert "add them later" in text


def test_the_offer_has_no_em_dashes_and_no_internals():
    league = ad.LeagueKey("S35", "Diamond", "12 - 2")
    text = _all_text(ads.fill_bracket_embed(league, _missing()))
    assert "—" not in text
    assert "guild" not in text.lower()
    for leak in ("tracking_mode", "AllianceWeek", "COL_"):
        assert leak not in text


def test_the_offer_only_fires_when_widening_not_narrowing():
    # Narrowing needs no rows, and nothing is ever deleted.
    import inspect

    import alliance_duel_wizard as w

    source = inspect.getsource(w.VSSetupView.finish)
    assert "was == ad.MODE_OWN_ALLIANCE and tracking_mode == ad.MODE_FULL_BRACKET" in source


def test_declining_the_offer_is_a_real_answer():
    import inspect

    import alliance_duel_wizard as w

    source = inspect.getsource(w.FillBracketView)
    assert 'label="Not now"' in source
    # One primary per view: the affirmative. Declining is secondary, not danger.
    assert source.count("ButtonStyle.primary") == 1
    assert "ButtonStyle.danger" not in source
