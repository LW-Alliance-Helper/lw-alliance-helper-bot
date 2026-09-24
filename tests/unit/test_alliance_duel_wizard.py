"""The VS setup wizard's tab step (#503) and its two new panel buttons (#651).

VS setup never asked which sheet tab to use before this: it hardcoded
`Alliance Duel (VS)`, created it silently, and a renamed or deleted tab was
quietly rebuilt blank with no notice. `TabNameView`/`TabNameModal` add the
same Keep current / Use default / Define my own choice every other wizard
offers through `setup_cog.ask_keep_or_change`, reimplemented natively here
because that helper is channel-based and this wizard is an ephemeral panel.

No existing test file covered this wizard at all before this one.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import alliance_duel_wizard as adw  # noqa: E402
from tests.conftest import TEST_GUILD_ID  # noqa: E402

OWNER_ID = 424242


def _buttons(view):
    return {c.label: c for c in view.children if getattr(c, "label", None)}


async def _click(item, inter=None):
    inter = inter or MagicMock()
    inter.response = AsyncMock()
    inter.followup = AsyncMock()
    await item.callback(inter)
    return inter


def _submit_tab_modal(modal: adw.TabNameModal, name: str):
    modal.name._value = name
    inter = MagicMock()
    inter.response = AsyncMock()
    inter.followup = AsyncMock()
    return modal.on_submit(inter), inter


@pytest.fixture()
def parent(seeded_db):
    return adw.VSSetupView(TEST_GUILD_ID, OWNER_ID)


# ── TabNameView shape ─────────────────────────────────────────────────────────


class TestTabNameViewShape:
    def test_unconfigured_guild_offers_only_two_buttons(self, parent):
        """A fresh guild's "current" tab already IS the default (seeded by
        `guild_vs_config`'s own default), so Keep current would be an exact
        duplicate of Use default and is dropped."""
        view = adw.TabNameView(parent, "own_alliance")
        labels = set(_buttons(view))
        assert "Keep current" not in labels
        assert f"Use default: {adw.DEFAULT_VS_TAB}" in labels
        assert "Define my own" in labels

    def test_a_custom_saved_tab_offers_all_three(self, seeded_db, parent):
        import config

        config.save_vs_config(TEST_GUILD_ID, tab_name="My Custom Tab")
        parent.cfg = config.get_vs_config(TEST_GUILD_ID)

        view = adw.TabNameView(parent, "own_alliance")
        buttons = _buttons(view)
        assert "Keep current: My Custom Tab" in buttons
        assert f"Use default: {adw.DEFAULT_VS_TAB}" in buttons
        assert "Define my own" in buttons


# ── Choosing a tab ────────────────────────────────────────────────────────────


class TestFinishFlow:
    @pytest.mark.asyncio
    async def test_use_default_saves_the_default_and_creates_it(self, parent):
        with patch.object(adw, "_find_or_create_tab", AsyncMock(return_value=False)):
            view = adw.TabNameView(parent, "own_alliance")
            inter = await _click(view.btn_default)

        import config

        cfg = config.get_vs_config(TEST_GUILD_ID)
        assert cfg["tab_name"] == adw.DEFAULT_VS_TAB
        assert cfg["tracking_mode"] == "own_alliance"
        assert cfg["enabled"] == 1
        body = inter.followup.send.call_args.kwargs["content"]
        assert f"Created **{adw.DEFAULT_VS_TAB}**" in body

    @pytest.mark.asyncio
    async def test_keep_current_saves_the_saved_name_not_the_default(self, seeded_db, parent):
        import config

        config.save_vs_config(TEST_GUILD_ID, tab_name="My Custom Tab")
        parent.cfg = config.get_vs_config(TEST_GUILD_ID)

        with patch.object(adw, "_find_or_create_tab", AsyncMock(return_value=True)):
            view = adw.TabNameView(parent, "full_bracket")
            await _click(view.btn_keep)

        assert config.get_vs_config(TEST_GUILD_ID)["tab_name"] == "My Custom Tab"

    @pytest.mark.asyncio
    async def test_define_my_own_opens_a_modal(self, parent):
        view = adw.TabNameView(parent, "own_alliance")
        inter = MagicMock()
        inter.response = AsyncMock()
        await view.btn_define.callback(inter)
        inter.response.send_modal.assert_awaited_once()
        modal = inter.response.send_modal.await_args.args[0]
        assert isinstance(modal, adw.TabNameModal)

    @pytest.mark.asyncio
    async def test_modal_saves_the_typed_name(self, parent):
        modal = adw.TabNameModal(parent, "own_alliance")
        with patch.object(adw, "_find_or_create_tab", AsyncMock(return_value=False)):
            coro, inter = _submit_tab_modal(modal, "Typed Tab Name")
            await coro

        import config

        assert config.get_vs_config(TEST_GUILD_ID)["tab_name"] == "Typed Tab Name"

    @pytest.mark.asyncio
    async def test_modal_rejects_a_blank_name(self, parent):
        modal = adw.TabNameModal(parent, "own_alliance")
        with patch.object(adw, "_find_or_create_tab", AsyncMock()) as mock_find:
            coro, inter = _submit_tab_modal(modal, "   ")
            await coro

        mock_find.assert_not_awaited()
        inter.response.send_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_found_vs_created_wording(self, parent):
        with patch.object(adw, "_find_or_create_tab", AsyncMock(return_value=True)):
            view = adw.TabNameView(parent, "own_alliance")
            inter = await _click(view.btn_default)
        body = inter.followup.send.call_args.kwargs["content"]
        assert f"Found **{adw.DEFAULT_VS_TAB}**" in body
        assert "Created" not in body

    @pytest.mark.asyncio
    async def test_unreachable_sheet_reports_and_stops(self, parent):
        with patch.object(adw, "_find_or_create_tab", AsyncMock(return_value=None)):
            view = adw.TabNameView(parent, "own_alliance")
            inter = await _click(view.btn_default)

        body = inter.followup.send.call_args.args[0]
        assert "could not reach your sheet" in body
        import config

        # Mode/tab are still saved -- only the sheet side failed.
        assert config.get_vs_config(TEST_GUILD_ID)["tab_name"] == adw.DEFAULT_VS_TAB


class TestTabChangeNotice:
    @pytest.mark.asyncio
    async def test_changing_the_tab_says_the_old_data_stays_put(self, seeded_db, parent):
        import config

        config.save_vs_config(TEST_GUILD_ID, tab_name="Old Tab")
        parent.cfg = config.get_vs_config(TEST_GUILD_ID)

        with patch.object(adw, "_find_or_create_tab", AsyncMock(return_value=False)):
            view = adw.TabNameView(parent, "own_alliance")
            inter = await _click(view.btn_default)

        body = inter.followup.send.call_args.kwargs["content"]
        assert "Old Tab" in body
        assert "doesn't move automatically" in body

    @pytest.mark.asyncio
    async def test_no_notice_when_the_tab_does_not_change(self, seeded_db, parent):
        import config

        config.save_vs_config(TEST_GUILD_ID, tab_name=adw.DEFAULT_VS_TAB)
        parent.cfg = config.get_vs_config(TEST_GUILD_ID)

        with patch.object(adw, "_find_or_create_tab", AsyncMock(return_value=True)):
            view = adw.TabNameView(parent, "own_alliance")
            inter = await _click(view.btn_default)

        body = inter.followup.send.call_args.kwargs["content"]
        assert "doesn't move automatically" not in body

    @pytest.mark.asyncio
    async def test_first_time_setup_has_no_stale_tab_to_warn_about(self, parent):
        """A brand-new guild's "previous" tab is the seeded default, which
        equals what it's about to become -- no prior data to warn about."""
        with patch.object(adw, "_find_or_create_tab", AsyncMock(return_value=False)):
            view = adw.TabNameView(parent, "own_alliance")
            inter = await _click(view.btn_default)

        body = inter.followup.send.call_args.kwargs["content"]
        assert "doesn't move automatically" not in body


class TestClaimWarning:
    @pytest.mark.asyncio
    async def test_a_tab_already_used_elsewhere_is_flagged(self, seeded_db, parent):
        import config

        with config._get_conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO guild_birthday_config (guild_id, tab_name) VALUES (?, ?)",
                (TEST_GUILD_ID, "Shared Tab"),
            )
            conn.commit()

        with (
            patch.object(adw, "_find_or_create_tab", AsyncMock(return_value=True)),
        ):
            modal = adw.TabNameModal(parent, "own_alliance")
            coro, inter = _submit_tab_modal(modal, "Shared Tab")
            await coro

        body = inter.followup.send.call_args.kwargs["content"]
        assert "Shared Tab" in body
        assert "Birthdays" in body

    @pytest.mark.asyncio
    async def test_an_unclaimed_tab_gets_no_warning(self, parent):
        with patch.object(adw, "_find_or_create_tab", AsyncMock(return_value=False)):
            view = adw.TabNameView(parent, "own_alliance")
            inter = await _click(view.btn_default)

        body = inter.followup.send.call_args.kwargs["content"]
        assert "also" not in body  # "is also {owner}" is the warning's own phrasing


# ── _find_or_create_tab ────────────────────────────────────────────────────────


class TestColumnGuideButton:
    @pytest.mark.asyncio
    async def test_shows_the_guide_for_the_saved_tracking_mode(self, seeded_db, parent):
        import config

        config.save_vs_config(TEST_GUILD_ID, tracking_mode="full_bracket")
        parent.cfg = config.get_vs_config(TEST_GUILD_ID)

        inter = MagicMock()
        inter.response = AsyncMock()
        await parent.btn_column_guide.callback(inter)

        inter.response.send_message.assert_awaited_once()
        assert inter.response.send_message.await_args.kwargs["ephemeral"] is True


class TestCheckDataButton:
    @pytest.mark.asyncio
    async def test_an_unreachable_sheet_says_so(self, parent):
        inter = MagicMock()
        inter.response = AsyncMock()
        inter.followup = AsyncMock()
        with patch("alliance_duel_wizard.ads.load_rows", return_value=None):
            await parent.btn_check_data.callback(inter)

        body = inter.followup.send.call_args.args[0]
        assert "couldn't reach your sheet" in body

    @pytest.mark.asyncio
    async def test_a_clean_sheet_reports_nothing_to_fix(self, parent):
        inter = MagicMock()
        inter.response = AsyncMock()
        inter.followup = AsyncMock()
        with patch("alliance_duel_wizard.ads.load_rows", return_value=[]):
            await parent.btn_check_data.callback(inter)

        embed = inter.followup.send.call_args.kwargs["embed"]
        assert "looks right" in embed.title

    @pytest.mark.asyncio
    async def test_runs_the_real_validator_against_loaded_rows(self, seeded_db, parent):
        """Not a mock of `ad.validate` -- the point of this button is that
        it actually runs the same check `validation_report_embed` renders,
        so a real finding must actually surface."""
        import alliance_duel as ad
        import config

        config.save_vs_config(
            TEST_GUILD_ID, tracking_mode="full_bracket", own_tag="ABC", own_warzone="1234"
        )
        parent.cfg = config.get_vs_config(TEST_GUILD_ID)

        # A day-outcome row whose two week scores don't sum to 13 (rule 1).
        bad_row = MagicMock(spec=ad.AllianceWeek)
        with patch("alliance_duel_wizard.ads.load_rows", return_value=[bad_row]):
            with patch("alliance_duel_wizard.ad.validate") as mock_validate:
                mock_validate.return_value = [
                    ad.Finding(rule=1, severity=ad.SEVERITY_ERROR, message="test finding")
                ]
                inter = MagicMock()
                inter.response = AsyncMock()
                inter.followup = AsyncMock()
                await parent.btn_check_data.callback(inter)

        embed = inter.followup.send.call_args.kwargs["embed"]
        assert "test finding" in embed.description
        mock_validate.assert_called_once()
        _, kwargs = mock_validate.call_args
        assert kwargs["tracking_mode"] == "full_bracket"
        assert kwargs["own_alliance"] == ad.AllianceKey.of("ABC", "1234")


class TestFindOrCreateTab:
    @pytest.mark.asyncio
    async def test_an_existing_worksheet_reads_as_found(self):
        fake_sheet = MagicMock()
        fake_sheet.worksheet.return_value = MagicMock()  # no exception -- it's there
        with (
            patch("alliance_duel_wizard.config.get_spreadsheet", return_value=fake_sheet),
            patch("alliance_duel_wizard.ads.ensure_tab") as mock_ensure,
        ):
            found = await adw._find_or_create_tab(TEST_GUILD_ID, "Existing Tab")

        assert found is True
        mock_ensure.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_missing_worksheet_reads_as_created(self):
        fake_sheet = MagicMock()
        fake_sheet.worksheet.side_effect = Exception("not found")
        with (
            patch("alliance_duel_wizard.config.get_spreadsheet", return_value=fake_sheet),
            patch("alliance_duel_wizard.ads.ensure_tab") as mock_ensure,
        ):
            found = await adw._find_or_create_tab(TEST_GUILD_ID, "New Tab")

        assert found is False
        mock_ensure.assert_called_once()

    @pytest.mark.asyncio
    async def test_an_unreachable_sheet_returns_none(self):
        with patch(
            "alliance_duel_wizard.config.get_spreadsheet", side_effect=Exception("no access")
        ):
            found = await adw._find_or_create_tab(TEST_GUILD_ID, "Any Tab")

        assert found is None
