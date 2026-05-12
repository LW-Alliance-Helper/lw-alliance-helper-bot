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
    the wizard inspects after view.wait().

    Two cases:
      - Real `discord.ui.View` (inline classes like KeepOrChangeView,
        SkipTemplateView, ToneDefaultView): always apply every override —
        the attributes that exist on the instance came from __init__ defaults
        like `self.skipped = False` or `self.selected = None`, and the test
        wants to drive them.
      - MagicMock view: only apply override when the attribute is None or
        auto-generated. This way `MagicMock(selected=False)` keeps its
        explicit False even if `selected` is in `view_overrides`.

    Always calls view.stop() to unblock view.wait().
    """
    import discord
    overrides    = overrides or {}
    is_real_view = isinstance(view, discord.ui.View)
    for k, v in overrides.items():
        cur = getattr(view, k, None)
        if is_real_view or cur is None or isinstance(cur, MagicMock):
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

    @pytest.mark.asyncio
    async def test_existing_config_threads_current_id_through_pickers(self, seeded_db):
        """Re-running /setup on a configured guild must pass the saved
        role and channel ids through to the pickers so leadership sees
        Keep-current buttons. Regression guard for #95."""
        import config
        from setup_cog import run_setup

        cfg = config.get_config(TEST_GUILD_ID)
        cfg.setup_complete       = True
        cfg.member_role_id       = 4001
        cfg.member_role_name     = "Member"
        cfg.leadership_role_id   = 4002
        cfg.leadership_role_name = "R4/R5"
        cfg.leadership_channel_id = 4003
        config.save_config(cfg)

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        new_member  = MagicMock(name="member_role",  id=4001)
        new_member.name = "Member"
        new_lead    = MagicMock(name="lead_role",    id=4002)
        new_lead.name   = "R4/R5"
        new_channel = MagicMock(name="lead_channel", id=4003)
        new_channel.category_id = None

        role_step_1 = MagicMock(
            confirmed=True, selected_role=new_member,
            is_current_stale=False, cancelled=False,
            wait=AsyncMock(),
        )
        role_step_2 = MagicMock(
            confirmed=True, selected_role=new_lead,
            is_current_stale=False, cancelled=False,
            wait=AsyncMock(),
        )
        ch_step     = MagicMock(
            confirmed=True, selected_channel=new_channel,
            is_current_stale=False, cancelled=False,
            wait=AsyncMock(),
        )
        tz_view     = MagicMock(confirmed=True, selected="America/New_York", wait=AsyncMock())
        sheet_modal = MagicMock(value="sheet_xyz")
        modal_view  = MagicMock(confirmed=True, wait=AsyncMock())
        share_done  = MagicMock(confirmed=True, wait=AsyncMock())
        confirm     = MagicMock(confirmed=True, wait=AsyncMock())

        role_iter         = iter([role_step_1, role_step_2])
        role_call_kwargs  = []
        ch_call_kwargs    = []

        def _record_role(*a, **kw):
            role_call_kwargs.append(kw)
            return next(role_iter)

        def _record_ch(*a, **kw):
            ch_call_kwargs.append(kw)
            return ch_step

        with patch("setup_cog.RoleSelectStep",     side_effect=_record_role), \
             patch("setup_cog.ChannelSelectStep",  side_effect=_record_ch), \
             patch("setup_cog.TimezoneSelectView", return_value=tz_view), \
             patch("setup_cog.TextInputModal",     return_value=sheet_modal), \
             patch("setup_cog.ModalLaunchView",    return_value=modal_view), \
             patch("setup_cog.ConfirmView",        side_effect=[share_done, confirm]):
            # The pre-wizard summary's EditOrCancelView (inline inside
            # ask_proceed_with_existing_config) is the first view through
            # send; route it to proceed=True so we enter the steps.
            make_send_handler(
                interaction.channel,
                view_overrides={"proceed": True, "cancelled": False},
            )
            await run_setup(interaction, bot)

        # Both role picks received the saved ids/names.
        assert role_call_kwargs[0]["current_id"]   == 4001
        assert role_call_kwargs[0]["current_name"] == "Member"
        assert role_call_kwargs[1]["current_id"]   == 4002
        assert role_call_kwargs[1]["current_name"] == "R4/R5"
        # Channel pick received the saved id.
        assert ch_call_kwargs[0]["current_id"] == 4003


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

    @pytest.mark.asyncio
    async def test_existing_config_shows_summary_and_cancel(self, seeded_db):
        """Re-running /setup_train on a configured guild opens with a
        summary embed + Edit/No-changes; picking No keeps everything.
        Regression guard for #97."""
        import config
        from setup_cog import run_train_setup

        config.save_train_config(
            TEST_GUILD_ID,
            tab_name="My Tab",
            themes=["Heroic"],
            tones=["Serious"],
            prompt_template="prompt",
            default_tone="Serious",
            blurbs_enabled=1,
            reminders_enabled=1,
            reminder_channel_id=600600,
            reminder_time="22:00",
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        # Send-handler routes the inline EditOrCancelView -> proceed=False.
        # No other views should ever fire because the helper returns
        # early once proceed=False.
        make_send_handler(
            interaction.channel,
            view_overrides={"proceed": False, "cancelled": False},
        )
        await run_train_setup(interaction, bot)

        # Saved values unchanged.
        cfg = config.get_train_config(TEST_GUILD_ID)
        assert cfg["tab_name"]            == "My Tab"
        assert cfg["reminder_channel_id"] == 600600

    @pytest.mark.asyncio
    async def test_existing_config_threads_reminder_channel_current_id(self, seeded_db):
        """Re-running with saved config and walking through to Step 7a
        must pass the saved reminder_channel_id as current_id."""
        import config
        from setup_cog import run_train_setup

        config.save_train_config(
            TEST_GUILD_ID,
            tab_name="My Tab",
            themes=["Heroic"],
            tones=["Serious"],
            prompt_template="prompt",
            default_tone="Serious",
            blurbs_enabled=0,             # skip Steps 3-6
            reminders_enabled=1,
            reminder_channel_id=600700,
            reminder_time="22:00",
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        new_channel = MagicMock(id=600700)
        ch_view = MagicMock(
            confirmed=True, cancelled=False, is_current_stale=False,
            selected_channel=new_channel, wait=AsyncMock(),
        )

        # Wizard step views: blurbs=No, reminders=Yes.
        blurb_view  = MagicMock(selected=False, cancelled=False, wait=AsyncMock())
        remind_view = MagicMock(selected=True,  cancelled=False, wait=AsyncMock())

        ch_call_kwargs = []

        def _record_ch(*a, **kw):
            ch_call_kwargs.append(kw)
            return ch_view

        with patch("setup_cog.YesNoView",         side_effect=[blurb_view, remind_view]), \
             patch("setup_cog.ChannelSelectStep", side_effect=_record_ch), \
             patch_keep_or_change(["My Tab", "10:00pm", ""]):
            # `view_overrides` covers the summary EditOrCancelView ->
            # proceed=True so the wizard walks the steps.
            make_send_handler(
                interaction.channel,
                view_overrides={"proceed": True, "cancelled": False},
            )
            await run_train_setup(interaction, bot)

        # Single channel pick (Step 7a) — current_id should match the saved value.
        assert len(ch_call_kwargs) == 1
        assert ch_call_kwargs[0]["current_id"] == 600700


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

    @pytest.mark.asyncio
    async def test_existing_enabled_config_shows_summary_and_keeps_unchanged(self, seeded_db):
        """Re-running /setup_birthdays on an enabled-and-configured guild
        opens with a summary embed + Edit/No-changes; picking No keeps
        the saved config. Regression guard for #98."""
        import config
        from setup_cog import run_birthday_setup

        config.save_birthday_config(
            TEST_GUILD_ID,
            tab_name="Members",
            name_col=0, birthday_col=1, discord_id_col=-1, data_start_row=2,
            enabled=1,
            train_integration=0, flexible_placement=0, lookahead_days=14,
            reminders_enabled=1, reminder_channel_id=550550, reminder_time="08:00",
            dm_message="",
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        # Summary EditOrCancelView -> proceed=False. Nothing else should run.
        make_send_handler(
            interaction.channel,
            view_overrides={"proceed": False, "cancelled": False},
        )
        await run_birthday_setup(interaction, bot)

        cfg = config.get_birthday_config(TEST_GUILD_ID)
        assert cfg["enabled"]             == 1
        assert cfg["reminder_channel_id"] == 550550

    @pytest.mark.asyncio
    async def test_existing_config_threads_reminder_channel_current_id(self, seeded_db):
        """Re-running with saved config and walking to Step 8a must pass
        the saved reminder_channel_id as current_id."""
        import config
        from setup_cog import run_birthday_setup

        config.save_birthday_config(
            TEST_GUILD_ID,
            tab_name="Members",
            name_col=0, birthday_col=1, discord_id_col=-1, data_start_row=2,
            enabled=1,
            train_integration=0, flexible_placement=0, lookahead_days=14,
            reminders_enabled=1, reminder_channel_id=550600, reminder_time="08:00",
            dm_message="",
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        new_channel = MagicMock(id=550600)
        ch_view = MagicMock(
            confirmed=True, cancelled=False, is_current_stale=False,
            selected_channel=new_channel, wait=AsyncMock(),
        )

        # YesNoView order: enable=Yes, train_integration=No, reminders=Yes.
        yn_views = [
            MagicMock(selected=True,  cancelled=False, wait=AsyncMock()),
            MagicMock(selected=False, cancelled=False, wait=AsyncMock()),
            MagicMock(selected=True,  cancelled=False, wait=AsyncMock()),
        ]

        ch_call_kwargs = []

        def _record_ch(*a, **kw):
            ch_call_kwargs.append(kw)
            return ch_view

        with patch("setup_cog.YesNoView",         side_effect=yn_views), \
             patch("setup_cog.ChannelSelectStep", side_effect=_record_ch), \
             patch_keep_or_change(["Members", "A", "B", "8:00am", ""]):
            make_send_handler(
                interaction.channel,
                view_overrides={"proceed": True, "cancelled": False},
            )
            await run_birthday_setup(interaction, bot)

        assert len(ch_call_kwargs) == 1
        assert ch_call_kwargs[0]["current_id"] == 550600

    @pytest.mark.asyncio
    async def test_disable_with_prior_config_offers_clear_button(self, seeded_db):
        """When leadership picks No on Step 1 AND has prior config, the
        ask_disable_with_clear helper fires with had_prior_config=True
        (Clear button rendered). Captures the kwargs the wizard passes
        to verify the gating is right."""
        import config
        from setup_cog import run_birthday_setup

        # Pre-seed so has_birthday_config returns True.
        config.save_birthday_config(
            TEST_GUILD_ID,
            tab_name="Members",
            name_col=0, birthday_col=1, discord_id_col=-1, data_start_row=2,
            enabled=1,
            train_integration=0, flexible_placement=0, lookahead_days=14,
            reminders_enabled=0, reminder_channel_id=0, reminder_time="08:00",
            dm_message="",
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        # Step 1 -> No.
        enabled_no = MagicMock(selected=False, cancelled=False, wait=AsyncMock())
        captured   = {}

        async def fake_disable(channel, **kwargs):
            captured.update(kwargs)

        with patch("setup_cog.YesNoView",                return_value=enabled_no), \
             patch("setup_cog.ask_disable_with_clear",   side_effect=fake_disable):
            make_send_handler(
                interaction.channel,
                view_overrides={"proceed": True, "cancelled": False},
            )
            await run_birthday_setup(interaction, bot)

        assert captured["feature_label"]   == "Birthday tracking"
        assert captured["setup_command"]   == "setup_birthdays"
        assert captured["had_prior_config"] is True
        # clear_fn should wipe the DB row when invoked.
        captured["clear_fn"]()
        assert config.has_birthday_config(TEST_GUILD_ID) is False

    @pytest.mark.asyncio
    async def test_disable_first_run_skips_clear_button(self, seeded_db):
        """First-time disable (no saved row yet) should call
        ask_disable_with_clear with had_prior_config=False so the
        Clear button doesn't render."""
        import config
        from setup_cog import run_birthday_setup

        # Make sure no row exists.
        config.clear_birthday_config(TEST_GUILD_ID)

        interaction = make_mock_interaction()
        bot         = AsyncMock()
        enabled_no  = MagicMock(selected=False, cancelled=False, wait=AsyncMock())
        captured    = {}

        async def fake_disable(channel, **kwargs):
            captured.update(kwargs)

        with patch("setup_cog.YesNoView",              return_value=enabled_no), \
             patch("setup_cog.ask_disable_with_clear", side_effect=fake_disable):
            make_send_handler(interaction.channel)
            await run_birthday_setup(interaction, bot)

        assert captured["had_prior_config"] is False


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

    @pytest.mark.asyncio
    async def test_existing_config_shows_summary_and_keeps_unchanged(self, seeded_db):
        """Re-running /setup_survey on a configured guild opens with the
        summary embed; picking No-changes leaves state intact."""
        import config
        from setup_cog import run_survey_setup

        # Pre-seed channel ids on guild_configs + survey config row.
        config.update_config_field(TEST_GUILD_ID, "survey_channel_id",        300100)
        config.update_config_field(TEST_GUILD_ID, "survey_notify_channel_id", 300200)
        config.save_survey_config(
            TEST_GUILD_ID,
            tab_squad_powers="Squad Powers",
            tab_history="Survey History",
            questions=[{"key": "q1", "label": "Q1", "type": "text", "help_text": "", "options": []}],
            intro_message="Take the survey!",
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        make_send_handler(
            interaction.channel,
            view_overrides={"proceed": False, "cancelled": False},
        )
        await run_survey_setup(interaction, bot)

        cfg = config.get_config(TEST_GUILD_ID)
        assert cfg.survey_channel_id        == 300100
        assert cfg.survey_notify_channel_id == 300200

    @pytest.mark.asyncio
    async def test_existing_config_threads_both_channel_current_ids(self, seeded_db):
        """Walking past the summary and through Steps 1+2 must pass the
        saved survey_channel_id and survey_notify_channel_id as
        current_id to their respective ChannelSelectStep calls."""
        import config
        from setup_cog import run_survey_setup

        config.update_config_field(TEST_GUILD_ID, "survey_channel_id",        300300)
        config.update_config_field(TEST_GUILD_ID, "survey_notify_channel_id", 300400)
        config.save_survey_config(
            TEST_GUILD_ID,
            tab_squad_powers="Squad Powers",
            tab_history="Survey History",
            questions=[{"key": "q1", "label": "Q1", "type": "text", "help_text": "", "options": []}],
            intro_message="Take the survey!",
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        new_survey_ch = MagicMock(id=300300)
        new_notify_ch = MagicMock(id=300400)
        ch_views = [
            MagicMock(
                confirmed=True, cancelled=False, is_current_stale=False,
                selected_channel=new_survey_ch, wait=AsyncMock(),
            ),
            MagicMock(
                confirmed=True, cancelled=False, is_current_stale=False,
                selected_channel=new_notify_ch, wait=AsyncMock(),
            ),
        ]
        ch_iter = iter(ch_views)

        # IntroChoiceView -> Keep current; QuestionStartView -> default
        # (both use distinct attr names so the same view_overrides dict
        # is safe to broadcast).
        ch_call_kwargs = []

        def _record_ch(*a, **kw):
            ch_call_kwargs.append(kw)
            return next(ch_iter)

        with patch("setup_cog.ChannelSelectStep", side_effect=_record_ch), \
             patch_keep_or_change(["Squad Powers", "Survey History"]):
            make_send_handler(
                interaction.channel,
                view_overrides={
                    "proceed":      True,
                    "intro_choice": "keep",     # IntroChoiceView
                    "choice":       "default",  # QuestionStartView
                    "cancelled":    False,
                },
            )
            await run_survey_setup(interaction, bot)

        assert len(ch_call_kwargs) == 2
        assert ch_call_kwargs[0]["current_id"] == 300300
        assert ch_call_kwargs[1]["current_id"] == 300400


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

        async def _skip_participation(*args, **kwargs):
            return {"enabled": 0, "tab_name": "", "questions": [],
                    "roster_tab": "", "roster_name_col": 0,
                    "roster_alias_col": -1, "roster_start_row": 2}

        # TeamChoiceView (inline) → selected="A", TemplateChoiceView → use_default=True
        with patch("setup_cog.ChannelSelectStep", return_value=log_view), \
             patch("setup_cog._run_storm_participation_step", side_effect=_skip_participation), \
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

        async def _skip_participation(*args, **kwargs):
            return {"enabled": 0, "tab_name": "", "questions": [],
                    "roster_tab": "", "roster_name_col": 0,
                    "roster_alias_col": -1, "roster_start_row": 2}

        with patch("setup_cog.ChannelSelectStep", return_value=log_view), \
             patch("setup_cog._run_storm_participation_step", side_effect=_skip_participation), \
             patch_keep_or_change(["CS Assignments"]):
            make_send_handler(
                interaction.channel,
                view_overrides={"selected": "A", "use_default": True},
            )
            await run_storm_setup(interaction, bot, "CS")

        gcfg = config.get_config(TEST_GUILD_ID)
        assert gcfg.cs_log_channel_id == 666666

    @pytest.mark.asyncio
    async def test_existing_ds_config_shows_summary_and_keeps_unchanged(self, seeded_db):
        """Re-running /setup_desertstorm on a configured guild opens with
        the summary embed; No-changes keeps state intact. Regression
        guard for #103."""
        import config
        from setup_cog import run_storm_setup

        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Assignments",
            mail_template="template body",
            timezone="America/New_York",
            log_channel_id=555700,
            post_channel_id=555800,
        )
        config.update_config_field(TEST_GUILD_ID, "ds_log_channel_id", 555700)

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        make_send_handler(
            interaction.channel,
            view_overrides={"proceed": False, "cancelled": False},
        )
        await run_storm_setup(interaction, bot, "DS")

        # No changes were applied.
        cfg = config.get_storm_config(TEST_GUILD_ID, "DS")
        assert cfg["log_channel_id"]  == 555700
        assert cfg["post_channel_id"] == 555800

    @pytest.mark.asyncio
    async def test_existing_ds_config_threads_both_channel_current_ids(self, seeded_db):
        """Walking past the summary into Steps 3 + 4 must pass the saved
        log_channel_id and post_channel_id as current_id to their
        respective ChannelSelectStep calls."""
        import config
        from setup_cog import run_storm_setup

        config.save_storm_config(
            TEST_GUILD_ID, "DS",
            tab_name="DS Assignments",
            mail_template="template",
            timezone="America/New_York",
            log_channel_id=555900,
            post_channel_id=555950,
        )
        config.update_config_field(TEST_GUILD_ID, "ds_log_channel_id", 555900)

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        new_log_ch  = MagicMock(id=555900)
        new_post_ch = MagicMock(id=555950)
        ch_views = [
            MagicMock(
                confirmed=True, cancelled=False, is_current_stale=False,
                selected_channel=new_log_ch, wait=AsyncMock(),
            ),
            MagicMock(
                confirmed=True, cancelled=False, is_current_stale=False,
                selected_channel=new_post_ch, wait=AsyncMock(),
            ),
        ]
        ch_iter = iter(ch_views)
        ch_call_kwargs = []

        def _record_ch(*a, **kw):
            ch_call_kwargs.append(kw)
            return next(ch_iter)

        async def _skip_participation(*args, **kwargs):
            return {"enabled": 0, "tab_name": "", "questions": [],
                    "roster_tab": "", "roster_name_col": 0,
                    "roster_alias_col": -1, "roster_start_row": 2}

        with patch("setup_cog.ChannelSelectStep", side_effect=_record_ch), \
             patch("setup_cog._run_storm_participation_step", side_effect=_skip_participation), \
             patch_keep_or_change(["DS Assignments", ""]):
            make_send_handler(
                interaction.channel,
                view_overrides={
                    # Summary -> Edit; team -> A; template -> use default.
                    "proceed":     True,
                    "selected":    "A",
                    "use_default": True,
                    "cancelled":   False,
                },
            )
            await run_storm_setup(interaction, bot, "DS")

        assert len(ch_call_kwargs) == 2
        assert ch_call_kwargs[0]["current_id"] == 555900
        assert ch_call_kwargs[1]["current_id"] == 555950


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

    @pytest.mark.asyncio
    async def test_existing_enabled_config_shows_summary_and_keeps_unchanged(self, seeded_db):
        """Re-running /setup_growth on an enabled-and-configured guild
        opens with a summary embed + Edit/No-changes; picking No keeps
        the saved config. Regression guard for #99."""
        import config
        from setup_cog import run_growth_setup

        config.save_growth_config(
            TEST_GUILD_ID, enabled=1,
            tab_source="Squad Powers", name_col="A",
            metrics=[{"label": "1st Squad Power", "col": "E"}],
            tab_growth="Growth Tracking",
            snapshot_frequency="monthly", snapshot_day=15, snapshot_interval=30,
            data_start_row=2,
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        make_send_handler(
            interaction.channel,
            view_overrides={"proceed": False, "cancelled": False},
        )
        await run_growth_setup(interaction, bot)

        # Saved values unchanged.
        cfg = config.get_growth_config(TEST_GUILD_ID)
        assert cfg["enabled"]      == 1
        assert cfg["snapshot_day"] == 15

    @pytest.mark.asyncio
    async def test_disable_with_prior_config_offers_clear_button(self, seeded_db):
        """When leadership picks No on Step 1 AND has prior config, the
        ask_disable_with_clear helper fires with had_prior_config=True
        and a clear_fn that wipes the row when invoked."""
        import config
        from setup_cog import run_growth_setup

        config.save_growth_config(
            TEST_GUILD_ID, enabled=1,
            tab_source="Squad Powers", name_col="A",
            metrics=[{"label": "1st Squad Power", "col": "E"}],
            tab_growth="Growth Tracking",
            snapshot_frequency="monthly", snapshot_day=1, snapshot_interval=30,
            data_start_row=2,
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()
        enabled_no  = MagicMock(selected=False, cancelled=False, wait=AsyncMock())
        captured    = {}

        async def fake_disable(channel, **kwargs):
            captured.update(kwargs)

        with patch("setup_cog.YesNoView",              return_value=enabled_no), \
             patch("setup_cog.ask_disable_with_clear", side_effect=fake_disable):
            make_send_handler(
                interaction.channel,
                # Summary -> proceed=True so we hit Step 1.
                view_overrides={"proceed": True, "cancelled": False},
            )
            await run_growth_setup(interaction, bot)

        assert captured["feature_label"]   == "Growth tracking"
        assert captured["setup_command"]   == "setup_growth"
        assert captured["had_prior_config"] is True
        # clear_fn wipes the DB row when invoked.
        captured["clear_fn"]()
        assert config.has_growth_config(TEST_GUILD_ID) is False

    @pytest.mark.asyncio
    async def test_disable_first_run_skips_clear_button(self, seeded_db):
        """First-time disable (no saved row yet) -> had_prior_config=False."""
        import config
        from setup_cog import run_growth_setup

        config.clear_growth_config(TEST_GUILD_ID)

        interaction = make_mock_interaction()
        bot         = AsyncMock()
        enabled_no  = MagicMock(selected=False, cancelled=False, wait=AsyncMock())
        captured    = {}

        async def fake_disable(channel, **kwargs):
            captured.update(kwargs)

        with patch("setup_cog.YesNoView",              return_value=enabled_no), \
             patch("setup_cog.ask_disable_with_clear", side_effect=fake_disable):
            make_send_handler(interaction.channel)
            await run_growth_setup(interaction, bot)

        assert captured["had_prior_config"] is False


# ── /setup_shiny_tasks ────────────────────────────────────────────────────────

class TestRunShinyTasksSetup:
    """Test /setup_shiny_tasks re-entry: summary embed, current_id
    threading on the channel pick, and ask_disable_with_clear gating.
    Regression guard for #101 — the wizard that originally triggered
    the whole #80 effort."""

    @pytest.mark.asyncio
    async def test_existing_enabled_config_shows_summary_and_keeps_unchanged(self, seeded_db):
        """Re-running on an enabled-and-configured guild opens with the
        summary; picking No-changes keeps state intact."""
        import config
        from setup_cog import run_shiny_tasks_setup

        config.save_shiny_tasks_config(
            TEST_GUILD_ID,
            enabled=1,
            channel_id=900000,
            post_time="09:00",
            server_min=677, server_max=804,
            message_template="",
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        make_send_handler(
            interaction.channel,
            view_overrides={"proceed": False, "cancelled": False},
        )
        await run_shiny_tasks_setup(interaction, bot)

        cfg = config.get_shiny_tasks_config(TEST_GUILD_ID)
        assert cfg["enabled"]    == 1
        assert cfg["channel_id"] == 900000

    @pytest.mark.asyncio
    async def test_existing_config_threads_channel_current_id(self, seeded_db):
        """Re-running and walking past the summary into Step 2 must pass
        the saved channel_id as current_id."""
        import config
        from setup_cog import run_shiny_tasks_setup

        config.save_shiny_tasks_config(
            TEST_GUILD_ID,
            enabled=1,
            channel_id=900100,
            post_time="09:00",
            server_min=677, server_max=804,
            message_template="",
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        new_channel = MagicMock(id=900100)
        ch_view = MagicMock(
            confirmed=True, cancelled=False, is_current_stale=False,
            selected_channel=new_channel, wait=AsyncMock(),
        )
        # Step 1 -> Yes; final ConfirmView -> Yes.
        enable_yes = MagicMock(selected=True, cancelled=False, wait=AsyncMock())
        confirm    = MagicMock(confirmed=True, cancelled=False, wait=AsyncMock())
        # ModalLaunchView (Step 3 server-range) auto-confirmed; its modal
        # has min_value / max_value pre-filled.
        range_modal = MagicMock(min_value="677", max_value="804", value="677 – 804")
        range_launcher = MagicMock(
            confirmed=True, cancelled=False, wait=AsyncMock(),
            modal=range_modal,
            children=[MagicMock()],  # touched by `range_launcher.children[0].label = ...`
        )

        ch_call_kwargs = []
        def _record_ch(*a, **kw):
            ch_call_kwargs.append(kw)
            return ch_view

        with patch("setup_cog.ChannelSelectStep", side_effect=_record_ch), \
             patch("setup_cog.YesNoView",         return_value=enable_yes), \
             patch("setup_cog.ConfirmView",       return_value=confirm), \
             patch("setup_cog.ModalLaunchView",   return_value=range_launcher), \
             patch_keep_or_change(["9:00am", ""]):  # Step 4 time + Step 5 template
            make_send_handler(
                interaction.channel,
                view_overrides={"proceed": True, "cancelled": False},
            )
            await run_shiny_tasks_setup(interaction, bot)

        assert len(ch_call_kwargs) == 1
        assert ch_call_kwargs[0]["current_id"] == 900100

    @pytest.mark.asyncio
    async def test_disable_with_prior_config_offers_clear_button(self, seeded_db):
        """When leadership picks No on Step 1 AND has prior config, the
        ask_disable_with_clear helper fires with had_prior_config=True
        and clear_fn that wipes the row."""
        import config
        from setup_cog import run_shiny_tasks_setup

        config.save_shiny_tasks_config(
            TEST_GUILD_ID,
            enabled=1,
            channel_id=900200,
            post_time="09:00",
            server_min=1, server_max=1000,
            message_template="",
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()
        enabled_no  = MagicMock(selected=False, cancelled=False, wait=AsyncMock())
        captured    = {}

        async def fake_disable(channel, **kwargs):
            captured.update(kwargs)

        with patch("setup_cog.YesNoView",              return_value=enabled_no), \
             patch("setup_cog.ask_disable_with_clear", side_effect=fake_disable):
            make_send_handler(
                interaction.channel,
                view_overrides={"proceed": True, "cancelled": False},
            )
            await run_shiny_tasks_setup(interaction, bot)

        assert captured["feature_label"]    == "Shiny tasks announcement"
        assert captured["setup_command"]    == "setup_shiny_tasks"
        assert captured["had_prior_config"] is True
        captured["clear_fn"]()
        assert config.has_shiny_tasks_config(TEST_GUILD_ID) is False

    @pytest.mark.asyncio
    async def test_disable_first_run_skips_clear_button(self, seeded_db):
        """First-time disable (no saved row) -> had_prior_config=False."""
        import config
        from setup_cog import run_shiny_tasks_setup

        config.clear_shiny_tasks_config(TEST_GUILD_ID)

        interaction = make_mock_interaction()
        bot         = AsyncMock()
        enabled_no  = MagicMock(selected=False, cancelled=False, wait=AsyncMock())
        captured    = {}

        async def fake_disable(channel, **kwargs):
            captured.update(kwargs)

        with patch("setup_cog.YesNoView",              return_value=enabled_no), \
             patch("setup_cog.ask_disable_with_clear", side_effect=fake_disable):
            make_send_handler(interaction.channel)
            await run_shiny_tasks_setup(interaction, bot)

        assert captured["had_prior_config"] is False


# ── /setup_growth_breakdown ───────────────────────────────────────────────────

class TestRunGrowthBreakdownSetup:
    """Test /setup_growth_breakdown threads current_id through the
    auto-post channel pick and shows the summary embed on re-runs.
    Regression guard for #100."""

    @pytest.mark.asyncio
    async def test_existing_config_shows_summary_and_keeps_unchanged(self, seeded_db):
        """Re-running on a configured guild shows the summary embed;
        picking No keeps the saved config."""
        import config
        from setup_cog import run_growth_breakdown_setup

        # Prereq: growth must be enabled with metrics for the breakdown
        # wizard to proceed past its guard.
        config.save_growth_config(
            TEST_GUILD_ID, enabled=1,
            tab_source="Squad Powers", name_col="A",
            metrics=[{"label": "1st Squad Power", "col": "E"}],
            tab_growth="Growth Tracking",
            snapshot_frequency="monthly", snapshot_day=1, snapshot_interval=30,
            data_start_row=2,
        )
        config.save_growth_breakdown_config(
            TEST_GUILD_ID,
            tab_breakdown="My Breakdown",
            breakdown_thresholds={},
            breakdown_labels={},
            breakdown_post_channel_id=800800,
            breakdown_bucket_filter=[],
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        make_send_handler(
            interaction.channel,
            view_overrides={"proceed": False, "cancelled": False},
        )
        await run_growth_breakdown_setup(interaction, bot)

        cfg = config.get_growth_config(TEST_GUILD_ID)
        assert cfg["breakdown_post_channel_id"] == 800800
        assert cfg["tab_breakdown"]             == "My Breakdown"

    @pytest.mark.asyncio
    async def test_existing_config_threads_post_channel_current_id(self, seeded_db):
        """Walking through to Step 2 (Auto-Post = Yes) must pass the
        saved breakdown_post_channel_id as current_id to
        ChannelSelectStep."""
        import config
        from setup_cog import run_growth_breakdown_setup

        config.save_growth_config(
            TEST_GUILD_ID, enabled=1,
            tab_source="Squad Powers", name_col="A",
            metrics=[{"label": "1st Squad Power", "col": "E"}],
            tab_growth="Growth Tracking",
            snapshot_frequency="monthly", snapshot_day=1, snapshot_interval=30,
            data_start_row=2,
        )
        config.save_growth_breakdown_config(
            TEST_GUILD_ID,
            tab_breakdown="Growth Breakdown",
            breakdown_thresholds={},
            breakdown_labels={},
            breakdown_post_channel_id=800900,
            breakdown_bucket_filter=[],
        )

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        # ChannelSelectStep capture + auto-confirm.
        new_channel = MagicMock(id=800900)
        ch_view = MagicMock(
            confirmed=True, cancelled=False, is_current_stale=False,
            selected_channel=new_channel, wait=AsyncMock(),
        )
        ch_call_kwargs = []

        def _record_ch(*a, **kw):
            ch_call_kwargs.append(kw)
            return ch_view

        # AutoPost YesNoView -> Yes.
        autopost_yes = MagicMock(selected=True, cancelled=False, wait=AsyncMock())

        with patch("setup_cog.YesNoView",         return_value=autopost_yes), \
             patch("setup_cog.ChannelSelectStep", side_effect=_record_ch), \
             patch_keep_or_change(["Growth Breakdown"]):  # Step 1 tab name
            # Summary proceed=True; inline bucket-filter / thresholds /
            # labels views auto-resolve via send-handler overrides.
            make_send_handler(
                interaction.channel,
                view_overrides={
                    "proceed":  True,
                    "selected": [],          # BucketFilterView -> "use all"
                    "choice":   "defaults",  # ThresholdsChoiceView + LabelsChoiceView
                    "cancelled": False,
                },
            )
            await run_growth_breakdown_setup(interaction, bot)

        assert len(ch_call_kwargs) == 1
        assert ch_call_kwargs[0]["current_id"] == 800900

    @pytest.mark.asyncio
    async def test_first_run_skips_summary(self, seeded_db):
        """First-time setup (no breakdown fields saved) should NOT show
        the summary — has_growth_breakdown_config returns False, the
        guard short-circuits, and the wizard walks Step 1 directly."""
        import config
        from setup_cog import run_growth_breakdown_setup

        config.save_growth_config(
            TEST_GUILD_ID, enabled=1,
            tab_source="Squad Powers", name_col="A",
            metrics=[{"label": "1st Squad Power", "col": "E"}],
            tab_growth="Growth Tracking",
            snapshot_frequency="monthly", snapshot_day=1, snapshot_interval=30,
            data_start_row=2,
        )
        # No save_growth_breakdown_config — defaults all the way.

        interaction = make_mock_interaction()
        bot         = AsyncMock()
        # AutoPost -> No so we skip the channel/filter steps.
        autopost_no = MagicMock(selected=False, cancelled=False, wait=AsyncMock())

        with patch("setup_cog.YesNoView", return_value=autopost_no), \
             patch_keep_or_change(["Growth Breakdown"]):
            make_send_handler(
                interaction.channel,
                view_overrides={
                    # If summary HAD fired, proceed=False would short-
                    # circuit and we'd never reach the auto-post toggle.
                    # We assert it didn't fire by checking the breakdown
                    # config actually got saved.
                    "choice":   "defaults",
                    "cancelled": False,
                },
            )
            await run_growth_breakdown_setup(interaction, bot)

        # Save happened (Step 1 tab name + defaults), confirming the
        # wizard walked all the way through.
        cfg = config.get_growth_config(TEST_GUILD_ID)
        assert cfg["breakdown_post_channel_id"] == 0
        assert cfg["tab_breakdown"]             == "Growth Breakdown"


# ── /setup_events ─────────────────────────────────────────────────────────────

class TestRunEventSetup:
    """Test /setup_events threads the saved channel ids through to the
    Keep-current path on a re-run. Regression guard for #96."""

    @pytest.mark.asyncio
    async def test_existing_config_threads_current_id_through_channel_picks(self, seeded_db):
        """Re-running /setup_events on a pre-configured guild and
        picking 'Edit Event Settings' must construct each ChannelSelectStep
        with the saved channel id so leadership sees Keep-current
        buttons instead of blank dropdowns."""
        import config
        from setup_cog import run_event_setup

        # Pre-seed event config + one event so the EventActionView fires.
        config.update_config_field(TEST_GUILD_ID, "event_draft_channel_id",    700001)
        config.update_config_field(TEST_GUILD_ID, "event_announce_channel_id", 700002)
        config.update_config_field(TEST_GUILD_ID, "event_draft_time",          "12:00")
        config.update_config_field(TEST_GUILD_ID, "event_five_min_warning",    1)
        config.save_guild_event(TEST_GUILD_ID, {
            "short_key":               "ev_1",
            "name":                    "Test Event",
            "timezone":                "America/New_York",
            "default_time":            "21:00",
            "announcement_blurb":      "test",
            "schedule_type":           "manual",
            "anchor_date":             "",
            "interval_days":           0,
            "draft_channel_id":        700001,
            "announcement_channel_id": 700002,
            "draft_time":              "12:00",
            "five_min_warning":        1,
            "active":                  1,
        })

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        # Two channels picked through the wizard — same ids so the
        # "Keep current" path would naturally be taken in production.
        new_draft = MagicMock(name="draft");    new_draft.id = 700001
        new_ann   = MagicMock(name="announce"); new_ann.id   = 700002
        draft_view = MagicMock(
            confirmed=True, cancelled=False,
            is_current_stale=False,
            selected_channel=new_draft, wait=AsyncMock(),
        )
        ann_view = MagicMock(
            confirmed=True, cancelled=False,
            is_current_stale=False,
            selected_channel=new_ann, wait=AsyncMock(),
        )

        # YesNoView routes warn=True; ask_keep_or_change feeds the
        # draft time prompt back as the existing value. EventActionView
        # gets routed to "settings" via send-handler override.
        warn_view = MagicMock(selected=True, cancelled=False, wait=AsyncMock())

        ch_call_kwargs = []
        ch_iter = iter([draft_view, ann_view])

        def _record_ch(*a, **kw):
            ch_call_kwargs.append(kw)
            return next(ch_iter)

        with patch("setup_cog.ChannelSelectStep", side_effect=_record_ch), \
             patch("setup_cog.YesNoView",         return_value=warn_view), \
             patch_keep_or_change(["12:00"]):
            make_send_handler(
                interaction.channel,
                view_overrides={
                    # EventActionView routes to "settings" so the wizard
                    # walks the channel-pick steps. EventListView
                    # routes to "finish" so we exit cleanly without
                    # opening the event builder.
                    "choice": "settings",
                    "action": "finish",
                },
            )
            await run_event_setup(interaction, bot)

        # Both channel picks received the saved ids.
        assert len(ch_call_kwargs) == 2
        assert ch_call_kwargs[0]["current_id"] == 700001
        assert ch_call_kwargs[1]["current_id"] == 700002


# ── Premium tier caps (Phase 1) ───────────────────────────────────────────────

@pytest.mark.free_tier_only
class TestPremiumCaps:
    """Verify free-tier caps block adds at the limit and don't block premium."""

    @pytest.mark.asyncio
    async def test_events_cap_blocks_sixth_event_on_free(self, seeded_db):
        """Free-tier guild with 5 events sees limit-reached embed and no 6th gets added."""
        import config
        from setup_cog import run_event_setup

        # Pre-set the event-related guild config so the wizard takes the
        # "existing config" branch (which jumps straight to the events list
        # via EventActionView -> "add").
        config.update_config_field(TEST_GUILD_ID, "event_draft_channel_id",    900001)
        config.update_config_field(TEST_GUILD_ID, "event_announce_channel_id", 900002)
        config.update_config_field(TEST_GUILD_ID, "event_draft_time",          "12:00")
        config.update_config_field(TEST_GUILD_ID, "event_five_min_warning",    1)

        for i in range(5):
            config.save_guild_event(TEST_GUILD_ID, {
                "short_key":               f"event_{i}",
                "name":                    f"Event {i}",
                "timezone":                "America/New_York",
                "default_time":            "12:00",
                "announcement_blurb":      "test",
                "schedule_type":           "manual",
                "anchor_date":             "",
                "interval_days":           0,
                "draft_channel_id":        900001,
                "announcement_channel_id": 900002,
                "draft_time":              "12:00",
                "five_min_warning":        1,
                "active":                  1,
            })

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        list_actions = iter(["add", "finish"])
        sent_embeds  = []

        async def fake_send(content=None, embed=None, view=None, **kw):
            if embed is not None:
                sent_embeds.append(embed)
            if view is not None:
                # Route the existing-config EventActionView -> "add"
                if hasattr(view, "choice") and getattr(view, "choice", None) is None:
                    view.choice = "add"
                # Route the EventListView -> "add" then "finish"
                if hasattr(view, "action"):
                    try:
                        view.action = next(list_actions)
                    except StopIteration:
                        view.action = "finish"
                _stop_view(view)
            return MagicMock(id=1)

        interaction.channel.send = AsyncMock(side_effect=fake_send)

        await run_event_setup(interaction, bot)

        events = config.get_guild_events(TEST_GUILD_ID, active_only=False)
        assert len(events) == 5

        titles = [e.title for e in sent_embeds if e and e.title]
        assert any("Free tier limit" in t for t in titles), (
            f"Expected limit-reached embed; titles seen: {titles}"
        )

    @pytest.mark.asyncio
    async def test_events_unlimited_for_premium_guild(self, seeded_db, monkeypatch):
        """A premium-bypass guild has no event-count cap."""
        from tests.constants import PREMIUM_TEST_GUILD_ID
        import importlib
        monkeypatch.setenv("PREMIUM_BYPASS_GUILD_IDS", str(PREMIUM_TEST_GUILD_ID))
        import premium
        importlib.reload(premium)
        try:
            cap = await premium.get_limit("events", PREMIUM_TEST_GUILD_ID)
            assert cap is None
        finally:
            premium.clear_cache()

    @pytest.mark.asyncio
    async def test_themes_truncated_to_three_on_free(self, seeded_db):
        """Free user submitting 6 themes gets only the first 3 saved."""
        import config
        from setup_cog import run_train_setup

        interaction = make_mock_interaction()
        bot         = AsyncMock()

        yn_views = [
            MagicMock(selected=True,  wait=AsyncMock()),
            MagicMock(selected=False, wait=AsyncMock()),
        ]

        # Themes/tones now flow through ask_keep_or_change (Define my own
        # path returns the user's typed string). The first response feeds
        # the tab-name step; the next two feed themes and tones.
        with patch("setup_cog.YesNoView", side_effect=yn_views), \
             patch_keep_or_change([
                 "Train Schedule",
                 "T1, T2, T3, T4, T5, T6",
                 "tn1, tn2, tn3, tn4",
             ]):
            make_send_handler(
                interaction.channel,
                view_overrides={
                    "selected":      "tn1",
                    "skipped":       True,
                    # New template manager exits via Done button
                    "action":        "done",
                },
            )
            await run_train_setup(interaction, bot)

        cfg = config.get_train_config(TEST_GUILD_ID)
        assert cfg["themes"] == ["T1", "T2", "T3"], f"Got {cfg['themes']!r}"
        assert cfg["tones"]  == ["tn1", "tn2", "tn3"], f"Got {cfg['tones']!r}"

    @pytest.mark.asyncio
    async def test_growth_custom_interval_disabled_for_free(self, seeded_db):
        """Free user's FrequencyView has the Custom Interval button disabled."""
        import config
        from setup_cog import run_growth_setup

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

        captured_freq_view = {"view": None}

        async def fake_send(content=None, embed=None, view=None, **kw):
            if view is not None and hasattr(view, "monthly") and hasattr(view, "custom"):
                captured_freq_view["view"] = view
            if view is not None:
                _resolve_view(view, {
                    "selected": "monthly",
                    "choice":   "done",
                })
            return MagicMock(id=1)

        interaction.channel.send = AsyncMock(side_effect=fake_send)

        yn = MagicMock(selected=True, wait=AsyncMock())
        with patch("setup_cog.YesNoView", return_value=yn), \
             patch_keep_or_change(["Squad Powers", "2", "A", "Growth Tracking", "1"]):
            await run_growth_setup(interaction, bot)

        freq_view = captured_freq_view["view"]
        assert freq_view is not None, "FrequencyView was never sent"
        custom_button = next(
            (c for c in freq_view.children if "Custom interval" in (getattr(c, "label", "") or "")),
            None,
        )
        assert custom_button is not None, (
            f"Custom button not found in {[getattr(c, 'label', None) for c in freq_view.children]}"
        )
        assert custom_button.disabled is True
