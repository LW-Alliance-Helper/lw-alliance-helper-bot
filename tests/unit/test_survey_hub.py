"""
Unit tests for survey_hub.py — the `/survey` hub that replaced the
`/survey overview | post | remind` subcommand group.

The gating rules are the point of this file. Extra surveys are Premium, so
Add and Remove gate; configuring the one default survey, posting it,
reminders, and the translation helper are free. That per-button split is
what makes the free tier able to configure a survey at all — the whole
surface used to sit behind a Premium-disabled `/setup` button.
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def _view(**kwargs):
    from survey_hub import _SurveyHubView

    defaults = dict(is_premium=True, has_extras=True, has_default=True)
    defaults.update(kwargs)
    return _SurveyHubView(MagicMock(), 1234, 99, **defaults)


def _labelled(view):
    return {c.label: c for c in view.children}


def _find(view, needle):
    return next(c for lbl, c in _labelled(view).items() if needle in lbl)


class TestButtonGating:
    def test_premium_with_extras_enables_everything(self):
        view = _view()
        assert all(not c.disabled for c in view.children)
        assert not any(c.label.startswith("💎") for c in view.children)

    def test_free_tier_gates_add_and_remove_only(self):
        view = _view(is_premium=False, has_extras=False)

        add = _find(view, "Add Survey")
        remove = _find(view, "Remove Survey")
        assert add.disabled and add.label.startswith("💎")
        assert remove.disabled and remove.label.startswith("💎")

        # The free-tier lockout fix: everything else stays usable.
        for needle in ("Edit Survey", "Post Survey", "Reminders", "Translation"):
            assert not _find(view, needle).disabled, f"{needle} should be free"

    def test_remove_is_off_on_premium_with_no_extras(self):
        """Remove only ever targets extras, so there's nothing to remove."""
        view = _view(has_extras=False)
        assert _find(view, "Remove Survey").disabled
        assert not _find(view, "Add Survey").disabled

    def test_fresh_install_offers_setup_and_hides_operations(self):
        """With no survey configured, Post and Reminders have no target."""
        view = _view(is_premium=False, has_extras=False, has_default=False)

        assert _find(view, "Set Up Survey")
        assert not _find(view, "Set Up Survey").disabled
        assert _find(view, "Post Survey").disabled
        assert _find(view, "Reminders").disabled
        # Translation can be picked before the survey exists.
        assert not _find(view, "Translation").disabled

    def test_buttons_fit_discord_row_limits(self):
        from collections import Counter

        rows = Counter(c.row for c in _view().children)
        assert all(n <= 5 for n in rows.values()), rows


class TestEditDispatch:
    """Edit skips the picker when there's only one survey to pick."""

    async def test_single_survey_goes_straight_to_the_wizard(self):
        view = _view(has_extras=False)
        inter = AsyncMock()

        with (
            patch("setup_cog.run_survey_setup", new=AsyncMock()) as direct,
            patch("setup_cog.run_pick_survey_to_edit", new=AsyncMock()) as picker,
            patch("setup_cog._check_wizard_can_run", new=AsyncMock(return_value=True)),
            patch("wizard_registry.safe_edit_response", new=AsyncMock()),
        ):
            await _find(view, "Edit Survey").callback(inter)

        direct.assert_awaited_once()
        picker.assert_not_awaited()

    async def test_extras_present_shows_the_picker(self):
        view = _view(has_extras=True)
        inter = AsyncMock()

        with (
            patch("setup_cog.run_survey_setup", new=AsyncMock()) as direct,
            patch("setup_cog.run_pick_survey_to_edit", new=AsyncMock()) as picker,
            patch("setup_cog._check_wizard_can_run", new=AsyncMock(return_value=True)),
            patch("wizard_registry.safe_edit_response", new=AsyncMock()),
        ):
            await _find(view, "Edit Survey").callback(inter)

        picker.assert_awaited_once()
        direct.assert_not_awaited()

    async def test_missing_channel_perms_blocks_the_wizard(self):
        """The wizard talks via channel.send, so a hub click in a channel the
        bot can't post in must explain rather than hang (the guard the old
        /setup door applied)."""
        view = _view(has_extras=False)
        inter = AsyncMock()

        with (
            patch("setup_cog.run_survey_setup", new=AsyncMock()) as direct,
            patch("setup_cog._check_wizard_can_run", new=AsyncMock(return_value=False)),
            patch("wizard_registry.safe_edit_response", new=AsyncMock()),
        ):
            await _find(view, "Edit Survey").callback(inter)

        direct.assert_not_awaited()

    async def test_add_survey_also_checks_channel_perms(self):
        view = _view()
        inter = AsyncMock()

        with (
            patch("setup_cog.run_create_new_extra_survey", new=AsyncMock()) as add,
            patch("setup_cog._check_wizard_can_run", new=AsyncMock(return_value=False)),
            patch("wizard_registry.safe_edit_response", new=AsyncMock()),
        ):
            await _find(view, "Add Survey").callback(inter)

        add.assert_not_awaited()


class TestOwnerScoping:
    async def test_another_officer_cannot_drive_your_hub(self):
        view = _view()
        inter = AsyncMock()
        inter.user.id = 12345  # not the owner (99)

        assert await view.interaction_check(inter) is False
        inter.response.send_message.assert_awaited_once()

    async def test_owner_passes(self):
        view = _view()
        inter = AsyncMock()
        inter.user.id = 99

        assert await view.interaction_check(inter) is True


class TestHubEmbed:
    def _guild(self, member=None):
        guild = MagicMock()
        guild.get_member = MagicMock(return_value=member)
        return guild

    def test_empty_state_when_nothing_configured(self):
        from survey_hub import _build_survey_hub_embed, SURVEY_HUB_BTN_SETUP

        embed = _build_survey_hub_embed(self._guild(), [], is_premium=True, translate_bot_id=0)
        assert "No survey configured" in (embed.description or "")
        assert SURVEY_HUB_BTN_SETUP in (embed.description or "")

    def test_a_survey_with_no_questions_still_reads_as_unconfigured(self):
        """A row can exist with zero questions; that's not a usable survey."""
        from survey_hub import _build_survey_hub_embed

        embed = _build_survey_hub_embed(
            self._guild(),
            [{"survey_id": "default", "survey_name": "Squad Powers", "questions": []}],
            is_premium=True,
            translate_bot_id=0,
        )
        assert "No survey configured" in (embed.description or "")

    def test_lists_every_survey_with_its_question_count(self):
        from survey_hub import _build_survey_hub_embed

        embed = _build_survey_hub_embed(
            self._guild(),
            [
                {
                    "survey_id": "default",
                    "survey_name": "Squad Powers",
                    "questions": [{"label": "a"}, {"label": "b"}],
                    "tab_squad_powers": "Squad Powers",
                    "survey_channel_id": 555,
                },
                {
                    "survey_id": "recruits",
                    "survey_name": "Recruit Intake",
                    "questions": [{"label": "c"}],
                    "tab_squad_powers": "Recruits",
                },
            ],
            is_premium=True,
            translate_bot_id=0,
        )
        names = [f.name for f in embed.fields]
        assert any("Squad Powers" in n and "default" in n for n in names)
        assert any("Recruit Intake" in n for n in names)
        body = " ".join(f.value for f in embed.fields)
        assert "**2** question(s)" in body and "**1** question(s)" in body
        assert "<#555>" in body

    def test_free_tier_sees_the_premium_note(self):
        from survey_hub import _build_survey_hub_embed

        embed = _build_survey_hub_embed(self._guild(), [], is_premium=False, translate_bot_id=0)
        assert any("Premium" in f.name for f in embed.fields)

    def test_configured_translation_helper_is_surfaced(self):
        from survey_hub import _build_survey_hub_embed

        helper = MagicMock()
        helper.mention = "<@777>"
        embed = _build_survey_hub_embed(
            self._guild(member=helper), [], is_premium=True, translate_bot_id=777
        )
        translation = next(f for f in embed.fields if "Translation" in f.name)
        assert "<@777>" in translation.value

    def test_departed_translation_helper_is_flagged(self):
        from survey_hub import _build_survey_hub_embed

        embed = _build_survey_hub_embed(
            self._guild(member=None), [], is_premium=True, translate_bot_id=777
        )
        translation = next(f for f in embed.fields if "Translation" in f.name)
        assert "no longer in this server" in translation.value


class TestSetupHubDoor:
    """`/setup` → 📋 Survey must not be Premium-gated any more: it's the only
    route a free alliance has to configure its one (free) survey."""

    def test_survey_button_is_not_premium_gated(self):
        import setup_hub

        with (
            patch("config.get_config", return_value=None),
            patch("api_server.map_manager_commands_enabled", return_value=True),
        ):
            view = setup_hub._SetupHubView(
                bot=MagicMock(), guild_id=1, owner_user_id=2, is_premium=False
            )

        survey_btn = next(
            c for c in view.children if getattr(c, "label", "") == setup_hub.HUB_BTN_SURVEY
        )
        assert not survey_btn.disabled
        assert not survey_btn.label.startswith("💎")

    def test_other_premium_buttons_still_gate(self):
        """Guard against the un-gating being over-applied."""
        import setup_hub

        with (
            patch("config.get_config", return_value=None),
            patch("api_server.map_manager_commands_enabled", return_value=True),
        ):
            view = setup_hub._SetupHubView(
                bot=MagicMock(), guild_id=1, owner_user_id=2, is_premium=False
            )

        for label in (setup_hub.HUB_BTN_MEMBERS, setup_hub.HUB_BTN_TRANSFERS):
            btn = next(c for c in view.children if label in getattr(c, "label", ""))
            assert btn.disabled, f"{label} should still be Premium-gated"
