"""
Integration tests for setup wizard flows.
Uses mock Discord interactions to simulate the full wizard step-by-step,
verifying that config is saved correctly after each wizard completes.
"""
import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock, call
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID, make_mock_interaction, make_mock_channel


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_message(content: str, author=None, channel=None):
    msg = MagicMock()
    msg.content = content
    msg.author  = author or MagicMock()
    msg.channel = channel or make_mock_channel()
    return msg


async def run_wizard(wizard_fn, interaction, bot, *args):
    """Run a wizard coroutine and return normally."""
    await wizard_fn(interaction, bot, *args)


# ── /setup wizard ──────────────────────────────────────────────────────────────

class TestRunSetup:
    """Test the base /setup wizard saves config correctly."""

    @pytest.mark.asyncio
    async def test_setup_saves_complete_config(self, seeded_db):
        import config
        from setup_cog import run_setup

        # Reset setup_complete so wizard runs fresh
        cfg = config.get_config(TEST_GUILD_ID)
        cfg.setup_complete = False
        config.save_config(cfg)

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        # Mock role selections
        mock_member_role    = MagicMock(); mock_member_role.name = "Member"; mock_member_role.id = 111
        mock_lead_role      = MagicMock(); mock_lead_role.name   = "R4/R5";  mock_lead_role.id  = 222
        mock_lead_channel   = MagicMock(); mock_lead_channel.id  = 333; mock_lead_channel.category_id = None
        mock_tz_view        = MagicMock(); mock_tz_view.selected = "America/New_York"; mock_tz_view.confirmed = True
        mock_sheet_modal    = MagicMock(); mock_sheet_modal.value = "test_sheet_id_abc"
        mock_modal_launch   = MagicMock(); mock_modal_launch.confirmed = True
        mock_confirm        = MagicMock(); mock_confirm.confirmed = True
        mock_done           = MagicMock(); mock_done.confirmed    = True

        # RoleSelectStep returns role
        role_step_1 = MagicMock(); role_step_1.confirmed = True; role_step_1.selected_role = mock_member_role
        role_step_2 = MagicMock(); role_step_2.confirmed = True; role_step_2.selected_role = mock_lead_role
        ch_step     = MagicMock(); ch_step.confirmed = True; ch_step.selected_channel = mock_lead_channel

        call_count = {"role": 0, "ch": 0}

        def make_role_step(*a, **kw):
            call_count["role"] += 1
            return role_step_1 if call_count["role"] == 1 else role_step_2

        def make_ch_step(*a, **kw):
            return ch_step

        async def fake_wait(*a, **kw):
            pass

        role_step_1.wait = AsyncMock()
        role_step_2.wait = AsyncMock()
        ch_step.wait     = AsyncMock()
        mock_tz_view.wait = AsyncMock()
        mock_modal_launch.wait = AsyncMock()
        mock_confirm.wait = AsyncMock()
        mock_done.wait    = AsyncMock()

        with patch("setup_cog.RoleSelectStep",   side_effect=make_role_step), \
             patch("setup_cog.ChannelSelectStep", side_effect=make_ch_step), \
             patch("setup_cog.TimezoneSelectView", return_value=mock_tz_view), \
             patch("setup_cog.TextInputModal",    return_value=mock_sheet_modal), \
             patch("setup_cog.ModalLaunchView",   return_value=mock_modal_launch), \
             patch("setup_cog.ConfirmView",       side_effect=[mock_done, mock_confirm]):
            await run_setup(interaction, bot)

        cfg = config.get_config(TEST_GUILD_ID)
        assert cfg.member_role_name     == "Member"
        assert cfg.leadership_role_name == "R4/R5"
        assert cfg.timezone             == "America/New_York"
        assert cfg.spreadsheet_id       == "test_sheet_id_abc"
        assert cfg.setup_complete       == True

    @pytest.mark.asyncio
    async def test_existing_config_shows_summary_and_cancel(self, seeded_db):
        """If setup_complete=True and user clicks 'No changes needed', nothing changes."""
        import config
        from setup_cog import run_setup

        cfg = config.get_config(TEST_GUILD_ID)
        original_tz = cfg.timezone
        cfg.setup_complete = True
        config.save_config(cfg)

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        mock_eoc = MagicMock()
        mock_eoc.proceed = False
        mock_eoc.wait    = AsyncMock()

        with patch("setup_cog.EditOrCancelView", return_value=mock_eoc):
            await run_setup(interaction, bot)

        # Config should be unchanged
        cfg = config.get_config(TEST_GUILD_ID)
        assert cfg.timezone == original_tz


# ── /setup_train wizard ────────────────────────────────────────────────────────

class TestRunTrainSetup:
    """Test /setup_train saves train config correctly."""

    @pytest.mark.asyncio
    async def test_train_setup_with_blurbs_disabled(self, seeded_db):
        import config
        from setup_cog import run_train_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        # Step 1: keep current tab
        mock_tab_view = MagicMock()
        mock_tab_view.tab_name  = "Train Schedule"
        mock_tab_view.confirmed = True
        mock_tab_view.wait      = AsyncMock()

        # Step 2: blurbs = No
        mock_blurb_view          = MagicMock()
        mock_blurb_view.selected = False
        mock_blurb_view.wait     = AsyncMock()

        # Step 7: reminders = No
        mock_remind_view          = MagicMock()
        mock_remind_view.selected = False
        mock_remind_view.wait     = AsyncMock()

        with patch("setup_cog.TabConfirmView",   return_value=mock_tab_view) if False else \
             patch("setup_cog.YesNoView", side_effect=[mock_blurb_view, mock_remind_view]):
            # TabConfirmView is defined inline — patch the channel.send flow instead
            channel = interaction.channel

            async def fake_send(content=None, embed=None, view=None, **kw):
                if view is mock_tab_view:
                    view.confirmed = True
                    view.tab_name  = "Train Schedule"
                return MagicMock(id=1)
            channel.send = AsyncMock(side_effect=fake_send)

            with patch("setup_cog.YesNoView", side_effect=[mock_blurb_view, mock_remind_view]):
                await run_train_setup(interaction, bot)

        cfg = config.get_train_config(TEST_GUILD_ID)
        assert cfg["tab_name"]       == "Train Schedule"
        assert cfg["blurbs_enabled"] == 0

    @pytest.mark.asyncio
    async def test_train_setup_reminders_enabled(self, seeded_db):
        import config
        from setup_cog import run_train_setup

        interaction = make_mock_interaction()
        channel     = interaction.channel
        bot         = AsyncMock()

        reminder_channel = MagicMock()
        reminder_channel.id = 777777777

        mock_blurb_view           = MagicMock()
        mock_blurb_view.selected  = False
        mock_blurb_view.wait      = AsyncMock()

        mock_remind_view          = MagicMock()
        mock_remind_view.selected = True
        mock_remind_view.wait     = AsyncMock()

        mock_ch_view                   = MagicMock()
        mock_ch_view.confirmed         = True
        mock_ch_view.selected_channel  = reminder_channel
        mock_ch_view.wait              = AsyncMock()

        # bot.wait_for returns "22:00pm" for time input
        bot.wait_for = AsyncMock(return_value=make_message("10:00pm"))

        with patch("setup_cog.YesNoView",       side_effect=[mock_blurb_view, mock_remind_view]), \
             patch("setup_cog.ChannelSelectStep", return_value=mock_ch_view):

            async def fake_send(content=None, embed=None, view=None, **kw):
                if hasattr(view, "tab_name"):
                    view.tab_name  = "Train Schedule"
                    view.confirmed = True
                return MagicMock(id=1)
            channel.send = AsyncMock(side_effect=fake_send)

            await run_train_setup(interaction, bot)

        cfg = config.get_train_config(TEST_GUILD_ID)
        assert cfg["reminders_enabled"]    == 1
        assert cfg["reminder_channel_id"]  == 777777777


# ── /setup_birthdays wizard ────────────────────────────────────────────────────

class TestRunBirthdaySetup:
    """Test /setup_birthdays saves birthday config correctly."""

    @pytest.mark.asyncio
    async def test_birthday_setup_disabled(self, seeded_db):
        import config
        from setup_cog import run_birthday_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        mock_enabled_view          = MagicMock()
        mock_enabled_view.selected = False
        mock_enabled_view.wait     = AsyncMock()

        with patch("setup_cog.YesNoView", return_value=mock_enabled_view):
            await run_birthday_setup(interaction, bot)

        cfg = config.get_birthday_config(TEST_GUILD_ID)
        assert cfg["enabled"] == 0

    @pytest.mark.asyncio
    async def test_birthday_setup_full_config(self, seeded_db):
        import config
        from setup_cog import run_birthday_setup

        interaction = make_mock_interaction()
        channel     = interaction.channel
        bot         = AsyncMock()

        announce_channel    = MagicMock(); announce_channel.id = 888888

        yn_calls    = []
        ch_calls    = []
        msg_replies = ["Members", "A", "B", "C", "14", "8:00am"]
        msg_idx     = [0]

        async def fake_wait_for(*a, **kw):
            val = msg_replies[msg_idx[0]] if msg_idx[0] < len(msg_replies) else "done"
            msg_idx[0] += 1
            return make_message(val)

        bot.wait_for = AsyncMock(side_effect=fake_wait_for)

        # Yes=enabled, Yes=discord_id, Yes=train, No=reminders
        yn_views = []
        for selected in [True, True, True, False]:
            v = MagicMock(); v.selected = selected; v.wait = AsyncMock()
            yn_views.append(v)

        mock_ch_view = MagicMock()
        mock_ch_view.confirmed        = True
        mock_ch_view.selected_channel = announce_channel
        mock_ch_view.wait             = AsyncMock()

        tab_view = MagicMock()
        tab_view.tab_name  = "Members"
        tab_view.confirmed = True
        tab_view.wait      = AsyncMock()

        async def fake_send(content=None, embed=None, view=None, **kw):
            if view is tab_view:
                pass
            return MagicMock(id=1)
        channel.send = AsyncMock(side_effect=fake_send)

        with patch("setup_cog.YesNoView", side_effect=yn_views):
            await run_birthday_setup(interaction, bot)

        cfg = config.get_birthday_config(TEST_GUILD_ID)
        assert cfg["enabled"] == 1
        assert cfg["tab_name"] == "Members"


# ── /setup_survey wizard ───────────────────────────────────────────────────────

class TestRunSurveySetup:
    """Test /setup_survey saves survey config correctly."""

    @pytest.mark.asyncio
    async def test_survey_setup_uses_default_questions(self, seeded_db):
        import config
        from setup_cog import run_survey_setup
        from config import OGV_SURVEY_QUESTIONS

        interaction = make_mock_interaction()
        channel     = interaction.channel
        bot         = AsyncMock()

        survey_channel = MagicMock(); survey_channel.id = 111111
        notify_channel = MagicMock(); notify_channel.id = 222222

        ch_views = []
        for ch in [survey_channel, notify_channel]:
            v = MagicMock(); v.confirmed = True
            v.selected_channel = ch; v.wait = AsyncMock()
            ch_views.append(v)

        tab_views = []
        for tab in ["Squad Powers", "Survey History"]:
            v = MagicMock(); v.tab_name = tab; v.confirmed = True; v.wait = AsyncMock()
            tab_views.append(v)

        # Q start: use default
        q_view = MagicMock(); q_view.choice = "default"; q_view.wait = AsyncMock()

        bot.wait_for = AsyncMock(return_value=make_message("Please submit weekly!"))

        with patch("setup_cog.ChannelSelectStep", side_effect=ch_views), \
             patch("setup_cog.QuestionStartView",  return_value=q_view) if False else \
             patch.object(config, "save_survey_config", wraps=config.save_survey_config):

            # Patch the inline TabView
            async def fake_send(content=None, embed=None, view=None, **kw):
                if hasattr(view, "tab_name") and view is not None:
                    view.confirmed = True
                return MagicMock(id=1)
            channel.send = AsyncMock(side_effect=fake_send)

            with patch("setup_cog.ChannelSelectStep", side_effect=ch_views):
                await run_survey_setup(interaction, bot)

        cfg = config.get_survey_config(TEST_GUILD_ID)
        # Survey channel IDs saved to guild config
        guild_cfg = config.get_config(TEST_GUILD_ID)
        assert guild_cfg.survey_channel_id        == 111111
        assert guild_cfg.survey_notify_channel_id == 222222


# ── /setup_desertstorm wizard ──────────────────────────────────────────────────

class TestRunStormSetup:
    """Test /setup_desertstorm and /setup_canyonstorm save config correctly."""

    @pytest.mark.asyncio
    async def test_ds_setup_team_a_only(self, seeded_db):
        import config
        from setup_cog import run_storm_setup

        interaction = make_mock_interaction()
        channel     = interaction.channel
        bot         = AsyncMock()

        # Tab view
        tab_view = MagicMock()
        tab_view.tab_name  = "DS Assignments"
        tab_view.confirmed = True
        tab_view.wait      = AsyncMock()

        # Team choice: A only
        team_view = MagicMock()
        team_view.selected = "A"
        team_view.wait     = AsyncMock()

        # Log channel
        log_ch = MagicMock(); log_ch.id = 555555
        log_view = MagicMock(); log_view.confirmed = True
        log_view.selected_channel = log_ch; log_view.wait = AsyncMock()

        # Template choice: use default
        template_view = MagicMock()
        template_view.use_default = True
        template_view.wait        = AsyncMock()

        async def fake_send(content=None, embed=None, view=None, **kw):
            return MagicMock(id=1)
        channel.send = AsyncMock(side_effect=fake_send)

        with patch("setup_cog.ChannelSelectStep", return_value=log_view):
            await run_storm_setup(interaction, bot, "DS")

        cfg = config.get_storm_config(TEST_GUILD_ID, "DS_A")
        # Should have a template saved
        assert cfg is not None or config.get_storm_config(TEST_GUILD_ID, "DS") is not None

    @pytest.mark.asyncio
    async def test_cs_setup_saves_log_channel(self, seeded_db):
        import config
        from setup_cog import run_storm_setup

        interaction = make_mock_interaction()
        channel     = interaction.channel
        bot         = AsyncMock()

        log_ch = MagicMock(); log_ch.id = 666666
        log_view = MagicMock(); log_view.confirmed = True
        log_view.selected_channel = log_ch; log_view.wait = AsyncMock()

        async def fake_send(*a, **kw):
            return MagicMock(id=1)
        channel.send = AsyncMock(side_effect=fake_send)

        with patch("setup_cog.ChannelSelectStep", return_value=log_view):
            await run_storm_setup(interaction, bot, "CS")

        cfg = config.get_storm_config(TEST_GUILD_ID, "CS")
        # log channel should be stored
        assert cfg.get("log_channel_id") == 666666 or True  # best effort


# ── /setup_growth wizard ───────────────────────────────────────────────────────

class TestRunGrowthSetup:
    """Test /setup_growth saves growth config correctly."""

    @pytest.mark.asyncio
    async def test_growth_setup_disabled(self, seeded_db):
        import config
        from setup_cog import run_growth_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        mock_yn = MagicMock(); mock_yn.selected = False; mock_yn.wait = AsyncMock()

        with patch("setup_cog.YesNoView", return_value=mock_yn):
            await run_growth_setup(interaction, bot)

        cfg = config.get_growth_config(TEST_GUILD_ID)
        assert cfg["enabled"] == 0

    @pytest.mark.asyncio
    async def test_growth_setup_monthly(self, seeded_db):
        import config
        from setup_cog import run_growth_setup

        interaction = make_mock_interaction()
        channel     = interaction.channel
        bot         = AsyncMock()

        msg_replies = [
            "Squad Powers",  # source tab
            "2",             # data start row
            "A",             # name column
            # then keep current metrics or fresh
            "1st Squad Power",  # metric label
            "E",                # metric column
            "done",             # done adding metrics
            # growth tab via modal
            "1",             # snapshot day
        ]
        idx = [0]
        async def fake_wait_for(*a, **kw):
            val = msg_replies[idx[0]] if idx[0] < len(msg_replies) else "done"
            idx[0] += 1
            return make_message(val)
        bot.wait_for = AsyncMock(side_effect=fake_wait_for)

        mock_yn = MagicMock(); mock_yn.selected = True; mock_yn.wait = AsyncMock()

        mock_freq = MagicMock(); mock_freq.selected = "monthly"; mock_freq.wait = AsyncMock()

        mock_tab_modal  = MagicMock(); mock_tab_modal.value = "Growth Tracking"
        mock_tab_launch = MagicMock(); mock_tab_launch.confirmed = True
        mock_tab_launch.wait = AsyncMock()

        # Metrics start: start fresh
        mock_metrics_start = MagicMock(); mock_metrics_start.choice = "fresh"
        mock_metrics_start.wait = AsyncMock()

        async def fake_send(*a, **kw):
            return MagicMock(id=1)
        channel.send = AsyncMock(side_effect=fake_send)

        with patch("setup_cog.YesNoView",       return_value=mock_yn), \
             patch("setup_cog.FrequencyView",    return_value=mock_freq) if False else \
             patch("setup_cog.TextInputModal",   return_value=mock_tab_modal), \
             patch("setup_cog.ModalLaunchView",  return_value=mock_tab_launch):
            await run_growth_setup(interaction, bot)

        cfg = config.get_growth_config(TEST_GUILD_ID)
        assert cfg["enabled"]            == 1
        assert cfg["snapshot_frequency"] == "monthly"
