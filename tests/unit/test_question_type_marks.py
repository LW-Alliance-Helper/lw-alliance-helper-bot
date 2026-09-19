"""Tests for the question-type marks and the free-tier tier note.

Two things are guarded here.

**The marks themselves.** Both question-type pickers lead each option
with a type mark. The Premium types used to lead with 💎 instead, which
told a paying alliance about a tier it already held — and put four
identical glyphs in a row on the participation picker, which rule 7 in
`DESIGN.md` calls worse than bare. The Premium options now carry the
same kind of mark as the free ones (or none, where no mark survived the
catalog check), and 💎 is gone from both label sets.

**The tier note.** `SelectOption` has no `disabled` parameter, so the
free tier cannot be shown these options the way a locked button is
shown. `_locked_types_note` names them in the prompt instead. The short
names it is fed have to keep the marks the full labels carry, or the
note teaches a glyph the menu does not use.
"""

from __future__ import annotations

import setup_cog


# ── The marks ────────────────────────────────────────────────────────────────


def test_no_premium_diamond_in_participation_type_labels():
    """💎 never appears on an option only a Premium guild can see."""
    for key, label in setup_cog._PARTICIPATION_TYPE_LABELS.items():
        assert "💎" not in label, f"{key} still carries 💎: {label!r}"


def test_participation_premium_types_carry_their_marks():
    labels = setup_cog._PARTICIPATION_TYPE_LABELS
    assert labels["single_select"] == "🔽 Single-select dropdown"
    assert labels["date"] == "📅 Date (formatted entry)"
    assert labels["derived_count"].startswith("✨ Derived count")
    # Multi-select is deliberately bare: every candidate mark was taken
    # with another sense, and a wrong glyph is worse than none.
    assert labels["multi_select"] == "Multi-select dropdown"


def test_no_glyph_is_repeated_across_the_participation_choice_set():
    """Rule 7: never repeat one glyph across a choice set."""
    leading = [
        label.split(" ", 1)[0]
        for label in setup_cog._PARTICIPATION_TYPE_LABELS.values()
        if not label[0].isalpha()
    ]
    assert len(leading) == len(set(leading)), f"repeated mark in {leading}"


# ── The tier note ────────────────────────────────────────────────────────────


def test_participation_premium_short_names_keep_their_mark():
    """The note's short names carry the same mark as the full labels."""
    short = setup_cog._PARTICIPATION_PREMIUM_TYPE_SHORT
    labels = setup_cog._PARTICIPATION_TYPE_LABELS
    assert set(short) == set(setup_cog._PARTICIPATION_PREMIUM_TYPES)
    for key, name in short.items():
        full = labels[key]
        assert full.startswith(name), f"{key}: {name!r} is not the head of {full!r}"


def test_locked_types_note_names_every_type_once():
    note = setup_cog._locked_types_note("🔽 Single-select", "📅 Date")
    assert note.startswith("\n*🔒 ")
    assert note.endswith("*")
    assert "🔽 Single-select" in note
    assert "📅 Date" in note
    # 🔒 opens the prose; 💎 names the tier. Both, and once each.
    assert note.count("🔒") == 1
    assert note.count("💎") == 1


def test_locked_types_note_joins_more_than_two_with_commas():
    note = setup_cog._locked_types_note("A", "B", "C")
    assert "A, B and C" in note


def test_locked_types_note_reads_as_singular_for_one_type():
    note = setup_cog._locked_types_note("📅 Date")
    assert "One more answer type is 💎 Premium: 📅 Date." in note
    assert "unlock it." in note
    assert " and " not in note


def test_locked_types_note_rejects_an_empty_list():
    import pytest

    with pytest.raises(ValueError):
        setup_cog._locked_types_note()


# ── The Survey picker's Premium types ────────────────────────────────────────


def test_survey_premium_short_names_keep_their_mark():
    """Same drift guard as the participation picker."""
    for value, (label, short) in setup_cog._SURVEY_PREMIUM_TYPES.items():
        assert label.startswith(short), f"{value}: {short!r} is not the head of {label!r}"
        assert "💎" not in label


def test_survey_premium_types_are_the_two_gated_ones():
    assert set(setup_cog._SURVEY_PREMIUM_TYPES) == {"multi_select", "date"}
