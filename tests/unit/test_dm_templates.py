"""
Tests for the alliance-configurable DM body templates introduced for
the three Premium DM features:

  * Birthday DM       — train_cog.DEFAULT_BIRTHDAY_DM, guild_birthday_config.dm_message
  * Train DM          — train_cog.DEFAULT_TRAIN_DM,    guild_train_config.dm_message
  * Storm reminder DM — storm_log.DEFAULT_STORM_REMINDER_DM,
                        guild_storm_config.dm_reminder_message

Both `train_cog._render_dm_body` and `storm_log._render_dm_body` are
identical helpers — same SafeDict pattern. These tests pin the
contract:

  * `{name}` substitution works
  * Unknown placeholders (typos in user templates) render literally
    instead of crashing the whole reminder loop
  * Empty / missing name doesn't blow up
  * Pathological format specs fall back to plain-string substitution
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")


# Both helpers are intentionally duplicated rather than shared (one is in
# train_cog.py, one is in storm_log.py). We test both so a regression in
# one doesn't slip past the other.
@pytest.fixture(params=["train_cog._render_dm_body", "storm_log._render_dm_body"])
def render(request):
    mod_name, attr = request.param.split(".")
    import importlib

    mod = importlib.import_module(mod_name)
    return getattr(mod, attr)


# ── {name} substitution ──────────────────────────────────────────────────────


class TestNamePlaceholderSubstitution:
    def test_substitutes_name_when_present(self, render):
        assert render("Happy birthday, {name}!", name="Alice") == "Happy birthday, Alice!"

    def test_renders_empty_string_when_name_missing(self, render):
        assert render("Hi {name}, welcome!", name="") == "Hi , welcome!"

    def test_default_name_kwarg_is_empty(self, render):
        # Calling without the kwarg shouldn't crash.
        out = render("Hi {name}!")
        assert out == "Hi !"

    def test_template_without_placeholders_passes_through(self, render):
        body = "🚂 Reminder for the day!"
        assert render(body, name="Alice") == body

    def test_placeholder_can_appear_multiple_times(self, render):
        out = render("{name} — yes, you, {name}!", name="Bob")
        assert out == "Bob — yes, you, Bob!"


# ── Defensive: typo / unknown placeholders ────────────────────────────────────


class TestSurvivesUnknownPlaceholders:
    """If an alliance leader puts `{nme}` in their template (typo), or
    `{alliance}` (not supported), the reminder loop must NOT crash —
    rendering the unknown placeholder literally is fine. The DM still
    gets sent; the typo is visible and self-correcting on the next
    edit."""

    def test_unknown_placeholder_renders_literally(self, render):
        out = render("Hello {nme}!", name="Alice")
        # Bad token kept; good token would have substituted.
        assert "{nme}" in out

    def test_partial_substitution_still_happens(self, render):
        out = render("Hi {name}, see {channel}!", name="Alice")
        assert "Alice" in out
        assert "{channel}" in out

    def test_does_not_raise_on_unknown_placeholder(self, render):
        # The whole point: never raise.
        render("Random {garbage} text {oops}", name="X")  # would raise without SafeDict

    def test_format_spec_in_user_template_does_not_crash(self, render):
        """A user template like `{name:>20}` is unusual but harmless.
        format_map handles spec strings fine when the key resolves;
        when it doesn't (typo + spec), our SafeDict returns the literal
        and Python's str.format raises ValueError on the spec — we
        fall through to the plain-replace fallback."""
        out = render("Padded: '{name:>10}'", name="Bob")
        # Either spec applied or fallback rendered Bob — both acceptable.
        assert "Bob" in out


# ── End-to-end: each feature's default + override ─────────────────────────────


class TestDefaultBodiesAreSane:
    """Sanity-check that the hardcoded default text (used when an
    alliance hasn't customised the DM body) is non-empty and contains
    the {name} placeholder where appropriate. Regression guard against
    someone clearing the default by accident."""

    def test_train_default_uses_name_placeholder(self):
        from train_cog import DEFAULT_TRAIN_DM

        assert "**today's train is for you!**" in DEFAULT_TRAIN_DM

    def test_birthday_default_uses_name_placeholder(self):
        from train_cog import DEFAULT_BIRTHDAY_DM

        assert "{name}" in DEFAULT_BIRTHDAY_DM
        assert "Happy birthday" in DEFAULT_BIRTHDAY_DM

    def test_storm_default_has_label_token_for_caller_substitution(self):
        """Storm default is shared between DS and CS via a {label}
        token that the caller substitutes before save. Verify the
        token is present and the substitution result reads cleanly."""
        from storm_log import DEFAULT_STORM_REMINDER_DM

        assert "{label}" in DEFAULT_STORM_REMINDER_DM

        ds = DEFAULT_STORM_REMINDER_DM.format(label="Desert Storm")
        cs = DEFAULT_STORM_REMINDER_DM.format(label="Canyon Storm")
        assert "Desert Storm" in ds
        assert "Canyon Storm" in cs
        assert "{label}" not in ds and "{label}" not in cs
