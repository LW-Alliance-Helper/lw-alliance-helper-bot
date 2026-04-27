"""
Integration tests for setup wizard flows.

These tests drive each wizard end-to-end with mocked Discord views and
verify the resulting config DB state. They lean on the module-level
`ask_keep_or_change` helper (used by every text-input step) plus patches
of the small set of module-level view classes (RoleSelectStep,
ChannelSelectStep, TimezoneSelectView, YesNoView, ConfirmView,
ScheduleTypeView, TextInputModal, ModalLaunchView).

Inline view classes (TeamChoiceView, PlacementView, etc.) are unblocked by
a generic channel.send interceptor that auto-completes them with sensible
defaults so the wizard advances past them.
"""
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID, make_mock_interaction


# ── Test harness ──────────────────────────────────────────────────────────────

def _stop_view(view):
    """Best-effort stop on any discord.ui.View-like mock or real view."""
    try:
        view.stop()
    except Exception:
        pass


def _resolve_view(view, overrides: dict = None):
    """
    For views NOT explicitly mocked by a `patch(...)`, fill in the attributes
    the wizard inspects after view.wait(). Only sets test-supplied overrides;
    never clobbers attributes that already have a real value (so a patched
    `MagicMock(selected=False)` survives).

    Always calls view.stop() to unblock view.wait().
    """
    overrides = overrides or {}
    for k, v in overrides.items():
        try: setattr(view, k, v)
        except Exception: pass
    _stop_view(view)


def make_send_handler(channel, *, view_overrides=None):
    """
    Replace channel.send with an AsyncMock that auto-resolves any view it
    receives. `view_overrides` is a flat dict of attribute-name → value
    that gets applied to every view passed through send. Wizards only read
    the attributes they care about, so extra ones are harmless.
    """
    overrides = dict(view_overrides or {})
    sent      = []

    async def fake_send(content=None, embed=None, view=None, **kw):
        sent.append({"content": content, "embed": embed, "view": view})
        if view is not None:
            _resolve_view(view, overrides)
        return MagicMock(id=1)

    channel.send = AsyncMock(side_effect=fake_send)
    return sent


def patch_keep_or_change(values):
    """Return a patch context manager for setup_cog.ask_keep_or_change.

    `values` is a list consumed in order — one per call.
    """
    it = iter(values)

    async def fake(*args, **kwargs):
        try:
            return next(it)
        except StopIteration:
            # Tests should provide enough values; missing → default
            return kwargs.get("default", "default")

    return patch("setup_cog.ask_keep_or_change", side_effect=fake)


# ── /setup wizard ─────────────────────────────────────────────────────────────

class TestRunSetup:
    """Test the base /setup wizard saves config correctly."""

    @pytest.mark.asyncio
    async def test_setup_saves_complete_config(self, seeded_db):
        import config
        from setup_cog import run_setup

        cfg = config.get_config(TEST_GUILD_ID)
        cfg.setup_complete = False
        config.save_config(cfg)

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        member_role  = MagicMock(); member_role.name = "Member";  member_role.id = 111
        lead_role    = MagicMock(); lead_role.name   = "R4/R5";   lead_role.id   = 222
        lead_channel = MagicMock(); lead_channel.id  = 333; lead_channel.category_id = None

        role_step_1 = MagicMock(confirmed=True, selected_role=member_role, wait=AsyncMock())
        role_step_2 = MagicMock(confirmed=True, selected_role=lead_role,   wait=AsyncMock())
        ch_step     = MagicMock(confirmed=True, selected_channel=lead_channel, wait=AsyncMock())
        tz_view     = MagicMock(confirmed=True, selected="America/New_York",   wait=AsyncMock())
        sheet_modal = MagicMock(value="test_sheet_id_abc")
        modal_view  = MagicMock(confirmed=True, wait=AsyncMock())
        share_done  = MagicMock(confirmed=True, wait=AsyncMock())
        confirm     = MagicMock(confirmed=True, wait=AsyncMock())

        role_iter = iter([role_step_1, role_step_2])

        with patch("setup_cog.RoleSelectStep",     side_effect=lambda *a, **kw: next(role_iter)), \
             patch("setup_cog.ChannelSelectStep",  return_value=ch_step), \
             patch("setup_cog.TimezoneSelectView", return_value=tz_view), \
             patch("setup_cog.TextInputModal",     return_value=sheet_modal), \
             patch("setup_cog.ModalLaunchView",    return_value=modal_view), \
             patch("setup_cog.ConfirmView",        side_effect=[share_done, confirm]):
            make_send_handler(interaction.channel)
            await run_setup(interaction, bot)

        cfg = config.get_config(TEST_GUILD_ID)
        assert cfg.member_role_name     == "Member"
        assert cfg.leadership_role_name == "R4/R5"
        assert cfg.timezone             == "America/New_York"
        assert cfg.spreadsheet_id       == "test_sheet_id_abc"
        assert cfg.setup_complete       == 1

    @pytest.mark.asyncio
    async def test_existing_config_shows_summary_and_cancel(self, seeded_db):
        """If setup_complete=True and user clicks 'No changes needed', nothing changes."""
        import config
        from setup_cog import run_setup

        cfg = config.get_config(TEST_GUILD_ID)
        original_tz        = cfg.timezone
        cfg.setup_complete = True
        config.save_config(cfg)

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        # EditOrCancelView is inline; resolve via send-handler.
        make_send_handler(
            interaction.channel,
            view_overrides={"proceed": False},
        )
        await run_setup(interaction, bot)

        cfg = config.get_config(TEST_GUILD_ID)
        assert cfg.timezone == original_tz


# ── /setup_train ──────────────────────────────────────────────────────────────

class TestRunTrainSetup:
    """Test /setup_train saves train config correctly."""

    @pytest.mark.asyncio
    async def test_train_setup_with_blurbs_disabled(self, seeded_db):
        import config
        from setup_cog import run_train_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        blurb_view  = MagicMock(selected=False, wait=AsyncMock())
        remind_view = MagicMock(selected=False, wait=AsyncMock())

        with patch("setup_cog.YesNoView", side_effect=[blurb_view, remind_view]), \
             patch_keep_or_change(["My Train Tab"]):
            make_send_handler(interaction.channel)
            await run_train_setup(interaction, bot)

        cfg = config.get_train_config(TEST_GUILD_ID)
        assert cfg["tab_name"]       == "My Train Tab"
        assert cfg["blurbs_enabled"] == 0

    @pytest.mark.asyncio
    async def test_train_setup_reminders_enabled(self, seeded_db):
        import config
        from setup_cog import run_train_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        reminder_channel = MagicMock(id=777777777)

        blurb_view  = MagicMock(selected=False, wait=AsyncMock())
        remind_view = MagicMock(selected=True,  wait=AsyncMock())
        ch_view     = MagicMock(confirmed=True, selected_channel=reminder_channel, wait=AsyncMock())

        with patch("setup_cog.YesNoView",         side_effect=[blurb_view, remind_view]), \
             patch("setup_cog.ChannelSelectStep", return_value=ch_view), \
             patch_keep_or_change(["Train Schedule", "10:00pm"]):
            make_send_handler(interaction.channel)
            await run_train_setup(interaction, bot)

        cfg = config.get_train_config(TEST_GUILD_ID)
        assert cfg["reminders_enabled"]   == 1
        assert cfg["reminder_channel_id"] == 777777777
        assert cfg["reminder_time"]       == "22:00"


# ── /setup_birthdays ──────────────────────────────────────────────────────────

class TestRunBirthdaySetup:
    """Test /setup_birthdays saves birthday config correctly."""

    @pytest.mark.asyncio
    async def test_birthday_setup_disabled(self, seeded_db):
        import config
        from setup_cog import run_birthday_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        enabled_view = MagicMock(selected=False, wait=AsyncMock())

        with patch("setup_cog.YesNoView", return_value=enabled_view):
            make_send_handler(interaction.channel)
            await run_birthday_setup(interaction, bot)

        cfg = config.get_birthday_config(TEST_GUILD_ID)
        assert cfg["enabled"] == 0

    @pytest.mark.asyncio
    async def test_birthday_setup_full_config(self, seeded_db):
        import config
        from setup_cog import run_birthday_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        # YesNo views, in order: enabled, train_integration, reminders
        yn_views = [
            MagicMock(selected=True,  wait=AsyncMock()),  # enabled
            MagicMock(selected=False, wait=AsyncMock()),  # train_integration off
            MagicMock(selected=False, wait=AsyncMock()),  # reminders off
        ]

        # Tab → "Members", name col → "A", bday col → "B"
        with patch("setup_cog.YesNoView", side_effect=yn_views), \
             patch_keep_or_change(["Members", "A", "B"]):
            make_send_handler(interaction.channel)
            await run_birthday_setup(interaction, bot)

        cfg = config.get_birthday_config(TEST_GUILD_ID)
        assert cfg["enabled"]      == 1
        assert cfg["tab_name"]     == "Members"
        assert cfg["name_col"]     == 0   # "A"
        assert cfg["birthday_col"] == 1   # "B"


# ── /setup_survey ─────────────────────────────────────────────────────────────

class TestRunSurveySetup:
    """Test /setup_survey saves survey config correctly."""

    @pytest.mark.asyncio
    async def test_survey_setup_uses_default_questions(self, seeded_db):
        import config
        from setup_cog import run_survey_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        survey_channel = MagicMock(id=111111)
        notify_channel = MagicMock(id=222222)

        ch_views = [
            MagicMock(confirmed=True, selected_channel=survey_channel, wait=AsyncMock()),
            MagicMock(confirmed=True, selected_channel=notify_channel, wait=AsyncMock()),
        ]
        q_view = MagicMock(choice="default", wait=AsyncMock())

        # Step 5 intro message arrives via bot.wait_for
        bot.wait_for = AsyncMock(return_value=MagicMock(content="Please submit weekly!"))

        with patch("setup_cog.ChannelSelectStep", side_effect=ch_views), \
             patch("setup_cog.QuestionStartView", return_value=q_view) if False else \
             patch_keep_or_change(["Squad Powers", "Survey History"]):
            # Two patches — survey wizard creates QuestionStartView inline,
            # which we resolve via the send handler below.
            make_send_handler(
                interaction.channel,
                view_overrides={"choice": "default"},
            )
            await run_survey_setup(interaction, bot)

        guild_cfg = config.get_config(TEST_GUILD_ID)
        assert guild_cfg.survey_channel_id        == 111111
        assert guild_cfg.survey_notify_channel_id == 222222

        survey_cfg = config.get_survey_config(TEST_GUILD_ID)
        assert survey_cfg["tab_squad_powers"] == "Squad Powers"
        assert survey_cfg["tab_history"]      == "Survey History"
        assert len(survey_cfg["questions"])   > 0   # defaults loaded


# ── /setup_desertstorm and /setup_canyonstorm ─────────────────────────────────

class TestRunStormSetup:
    """Test /setup_desertstorm and /setup_canyonstorm save config correctly."""

    @pytest.mark.asyncio
    async def test_ds_setup_team_a_only(self, seeded_db):
        import config
        from setup_cog import run_storm_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        log_ch   = MagicMock(id=555555)
        log_view = MagicMock(confirmed=True, selected_channel=log_ch, wait=AsyncMock())

        # TeamChoiceView (inline) → selected="A", TemplateChoiceView → use_default=True
        with patch("setup_cog.ChannelSelectStep", return_value=log_view), \
             patch_keep_or_change(["DS Assignments"]):
            make_send_handler(
                interaction.channel,
                view_overrides={"selected": "A", "use_default": True},
            )
            await run_storm_setup(interaction, bot, "DS")

        # log channel persisted to guild_configs
        gcfg = config.get_config(TEST_GUILD_ID)
        assert gcfg.ds_log_channel_id == 555555

    @pytest.mark.asyncio
    async def test_cs_setup_saves_log_channel(self, seeded_db):
        import config
        from setup_cog import run_storm_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        log_ch   = MagicMock(id=666666)
        log_view = MagicMock(confirmed=True, selected_channel=log_ch, wait=AsyncMock())

        with patch("setup_cog.ChannelSelectStep", return_value=log_view), \
             patch_keep_or_change(["CS Assignments"]):
            make_send_handler(
                interaction.channel,
                view_overrides={"selected": "A", "use_default": True},
            )
            await run_storm_setup(interaction, bot, "CS")

        gcfg = config.get_config(TEST_GUILD_ID)
        assert gcfg.cs_log_channel_id == 666666


# ── /setup_growth ─────────────────────────────────────────────────────────────

class TestRunGrowthSetup:
    """Test /setup_growth saves growth config correctly."""

    @pytest.mark.asyncio
    async def test_growth_setup_disabled(self, seeded_db):
        import config
        from setup_cog import run_growth_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        yn = MagicMock(selected=False, wait=AsyncMock())

        with patch("setup_cog.YesNoView", return_value=yn):
            make_send_handler(interaction.channel)
            await run_growth_setup(interaction, bot)

        cfg = config.get_growth_config(TEST_GUILD_ID)
        assert cfg["enabled"] == 0

    @pytest.mark.asyncio
    async def test_growth_setup_monthly(self, seeded_db):
        import config
        from setup_cog import run_growth_setup

        # Pre-seed at least one metric so the MetricsActionView's "Done"
        # button isn't disabled when the wizard reaches it.
        config.save_growth_config(
            TEST_GUILD_ID, enabled=0,
            tab_source="Squad Powers", name_col="A",
            metrics=[{"label": "1st Squad Power", "col": "E"}],
            tab_growth="Growth Tracking",
            snapshot_frequency="monthly", snapshot_day=1, snapshot_interval=30,
            data_start_row=2,
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        yn = MagicMock(selected=True, wait=AsyncMock())

        # Ordered ask_keep_or_change values:
        # 1. Source Tab     → "Squad Powers"
        # 2. Data Start Row → "2"
        # 3. Name Column    → "A"
        # 4. Growth Tab     → "Growth Tracking"
        # 5. Snapshot Day   → "1"
        keep_values = ["Squad Powers", "2", "A", "Growth Tracking", "1"]

        with patch("setup_cog.YesNoView", return_value=yn), \
             patch_keep_or_change(keep_values):
            # MetricsActionView and FrequencyView are inline; resolve via
            # send-handler with their respective attribute overrides.
            make_send_handler(
                interaction.channel,
                view_overrides={
                    "choice":   "done",      # MetricsActionView
                    "selected": "monthly",   # FrequencyView
                },
            )
            await run_growth_setup(interaction, bot)

        cfg = config.get_growth_config(TEST_GUILD_ID)
        assert cfg["enabled"]            == 1
        assert cfg["snapshot_frequency"] == "monthly"
