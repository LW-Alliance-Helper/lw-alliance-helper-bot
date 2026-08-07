"""
Unit tests for survey templates and the one-pair-of-tabs-per-survey rule.

A survey is generic: a question set plus two sheet tabs. The squad-power
question set is one *template* for that, not the baseline. These tests
cover the template registry, the tab names a survey suggests, the
collision rule that keeps two surveys off each other's tabs, and the
auto-creation that means leadership never hand-makes a tab.
"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock
import sys, os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


class TestSurveyTemplateRegistry:
    def test_squad_power_template_carries_its_questions_and_tabs(self):
        from defaults import SURVEY_TEMPLATE_SQUAD_POWER, DEFAULT_SURVEY_QUESTIONS, survey_template

        tpl = survey_template(SURVEY_TEMPLATE_SQUAD_POWER)
        assert tpl["questions"] == DEFAULT_SURVEY_QUESTIONS
        assert tpl["tab_responses"] == "Squad Powers"
        assert tpl["tab_history"] == "Survey History"
        assert tpl["intro_message"]

    def test_scratch_template_prefills_nothing(self):
        from defaults import SURVEY_TEMPLATE_SCRATCH, survey_template

        tpl = survey_template(SURVEY_TEMPLATE_SCRATCH)
        assert tpl["questions"] == []
        assert tpl["intro_message"] == ""
        assert tpl["tab_responses"] is None
        assert tpl["suggested_survey_name"] is None

    @pytest.mark.parametrize("key", [None, "", "no-such-template"])
    def test_unknown_key_degrades_to_scratch(self, key):
        """A survey row carrying a template that no longer ships must not
        take the wizard down mid-run."""
        from defaults import SURVEY_TEMPLATE_SCRATCH, survey_template

        assert survey_template(key)["key"] == SURVEY_TEMPLATE_SCRATCH

    def test_picker_order_lists_only_real_templates(self):
        from defaults import (
            SURVEY_TEMPLATES,
            SURVEY_TEMPLATE_PICKER_ORDER,
            SURVEY_TEMPLATE_SCRATCH,
        )

        assert SURVEY_TEMPLATE_SCRATCH not in SURVEY_TEMPLATE_PICKER_ORDER
        for key in SURVEY_TEMPLATE_PICKER_ORDER:
            assert key in SURVEY_TEMPLATES

    def test_every_template_supplies_the_copy_the_wizard_reads(self):
        from defaults import SURVEY_TEMPLATES

        required = (
            "key",
            "name",
            "emoji",
            "description",
            "responses_step_label",
            "responses_step_prompt",
            "history_step_label",
            "history_step_prompt",
            "intro_step_example",
        )
        for key, tpl in SURVEY_TEMPLATES.items():
            for field in required:
                assert tpl.get(field), f"{key} is missing {field}"


class TestDeriveSurveyTabNames:
    def test_derives_a_distinct_pair_from_the_survey_name(self):
        from defaults import derive_survey_tab_names

        responses, history = derive_survey_tab_names("VP Buff Agreement")
        assert responses == "VP Buff Agreement"
        assert history == "VP Buff Agreement History"
        assert responses != history

    def test_strips_characters_sheets_rejects_in_a_tab_name(self):
        from defaults import derive_survey_tab_names

        responses, _ = derive_survey_tab_names("Q3: Buffs? [draft]/final")
        for bad in "[]*?/\\:":
            assert bad not in responses

    def test_both_names_stay_within_the_sheets_length_cap(self):
        from defaults import derive_survey_tab_names

        responses, history = derive_survey_tab_names("A" * 250)
        assert len(responses) <= 100
        assert len(history) <= 100
        assert history.endswith(" History")

    def test_falls_back_when_the_name_has_nothing_usable(self):
        from defaults import derive_survey_tab_names

        responses, history = derive_survey_tab_names("///")
        assert responses == "Survey"
        assert history == "Survey History"


class TestSurveyTabsInUse:
    def test_reports_the_survey_owning_each_claimed_tab(self, seeded_db):
        import config

        config.save_survey_config(
            TEST_GUILD_ID, "Squad Powers", "Survey History", [], "", "squad_power"
        )
        config.save_extra_survey(
            TEST_GUILD_ID,
            "vp-buff-agreement",
            survey_name="VP Buff Agreement",
            tab_squad_powers="VP Buff Agreement",
            tab_history="VP Buff Agreement History",
            template="scratch",
        )

        claimed = config.survey_tabs_in_use(TEST_GUILD_ID)
        assert claimed["squad powers"] == "Default"
        assert claimed["vp buff agreement history"] == "VP Buff Agreement"

    def test_excludes_the_survey_being_edited(self, seeded_db):
        """Re-running the wizard and keeping your own tab names is not a
        collision with yourself."""
        import config

        config.save_extra_survey(
            TEST_GUILD_ID,
            "vp-buff-agreement",
            survey_name="VP Buff Agreement",
            tab_squad_powers="VP Buff Agreement",
            tab_history="VP Buff Agreement History",
        )

        claimed = config.survey_tabs_in_use(TEST_GUILD_ID, exclude_survey_id="vp-buff-agreement")
        assert "vp buff agreement" not in claimed

    def test_matching_ignores_case(self, seeded_db):
        import config

        config.save_extra_survey(
            TEST_GUILD_ID,
            "intake",
            survey_name="Recruit Intake",
            tab_squad_powers="Recruit Intake",
            tab_history="Recruit Intake History",
        )

        claimed = config.survey_tabs_in_use(TEST_GUILD_ID)
        assert "RECRUIT intake".casefold() in claimed


class TestTemplatePersistence:
    def test_extra_survey_round_trips_its_template(self, seeded_db):
        import config

        config.save_extra_survey(
            TEST_GUILD_ID,
            "vp-buff-agreement",
            survey_name="VP Buff Agreement",
            tab_squad_powers="VP Buff Agreement",
            tab_history="VP Buff Agreement History",
            template="scratch",
        )
        assert config.get_survey(TEST_GUILD_ID, "vp-buff-agreement")["template"] == "scratch"

    def test_default_survey_round_trips_its_template(self, seeded_db):
        import config

        config.save_survey_config(TEST_GUILD_ID, "Stats", "History", [], "", "scratch")
        assert config.get_survey_config(TEST_GUILD_ID)["template"] == "scratch"

    def test_rows_predating_templates_backfill_to_squad_power(self, seeded_db):
        """Every survey configured before templates existed walked the
        squad-power wizard, so re-editing one must still read that way."""
        import config

        with config._get_conn() as conn:
            conn.execute(
                "INSERT INTO guild_extra_surveys (guild_id, survey_id, survey_name) "
                "VALUES (?, ?, ?)",
                (TEST_GUILD_ID, "legacy", "Legacy Survey"),
            )
            conn.execute(
                "UPDATE guild_extra_surveys SET template = NULL WHERE survey_id = 'legacy'"
            )
            conn.commit()

        from defaults import survey_template

        stored = config.get_survey(TEST_GUILD_ID, "legacy")["template"]
        assert survey_template(stored or "squad_power")["key"] == "squad_power"


class TestEnsureSurveyTab:
    """The wizard makes the tabs rather than telling leadership to."""

    @pytest.mark.asyncio
    async def test_creates_a_missing_tab_and_says_so(self):
        import setup_cog

        channel = MagicMock()
        channel.send = AsyncMock()

        sh = MagicMock()
        sh.worksheet = MagicMock(side_effect=Exception("Worksheet not found"))
        sh.add_worksheet = MagicMock(return_value=MagicMock())

        with patch("config.get_spreadsheet", return_value=sh):
            await setup_cog._ensure_survey_tab(channel, TEST_GUILD_ID, "VP Buff Agreement")

        sh.add_worksheet.assert_called_once()
        assert "Created" in channel.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_leaves_an_existing_tab_alone(self):
        import setup_cog

        channel = MagicMock()
        channel.send = AsyncMock()

        sh = MagicMock()
        sh.worksheet = MagicMock(return_value=MagicMock())
        sh.add_worksheet = MagicMock()

        with patch("config.get_spreadsheet", return_value=sh):
            await setup_cog._ensure_survey_tab(channel, TEST_GUILD_ID, "Squad Powers")

        sh.add_worksheet.assert_not_called()
        assert "Found" in channel.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_a_broken_sheet_warns_instead_of_ending_the_wizard(self):
        """The alliance owns their spreadsheet. It being unshared or gone
        is theirs to fix, and must not cost them the rest of setup."""
        import setup_cog

        channel = MagicMock()
        channel.send = AsyncMock()

        with patch("config.get_spreadsheet", side_effect=Exception("403 forbidden")):
            await setup_cog._ensure_survey_tab(channel, TEST_GUILD_ID, "VP Buff Agreement")

        msg = channel.send.call_args[0][0]
        assert "couldn't check your Google Sheet" in msg
        assert "VP Buff Agreement" in msg


class TestAskSurveyTab:
    @pytest.mark.asyncio
    async def test_rejects_a_tab_another_survey_owns_and_re_asks(self):
        import setup_cog

        channel = MagicMock()
        channel.send = AsyncMock()

        with (
            patch(
                "setup_cog.ask_keep_or_change",
                AsyncMock(side_effect=["Squad Powers", "VP Buff Agreement"]),
            ),
            patch("setup_cog._ensure_survey_tab", AsyncMock()) as ensure,
        ):
            chosen = await setup_cog._ask_survey_tab(
                channel,
                prompt="pick a tab",
                default="VP Buff Agreement",
                current="",
                modal_title="Tab",
                cancel_event=None,
                guild_id=TEST_GUILD_ID,
                claimed_tabs={"squad powers": "Default"},
                also_claimed={},
            )

        assert chosen == "VP Buff Agreement"
        warned = [c[0][0] for c in channel.send.call_args_list if c[0]]
        assert any("already used by **Default**" in m for m in warned)
        # Only the accepted name gets created.
        ensure.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rejects_pointing_both_of_one_survey_s_tabs_at_each_other(self):
        import setup_cog

        channel = MagicMock()
        channel.send = AsyncMock()

        with (
            patch(
                "setup_cog.ask_keep_or_change",
                AsyncMock(side_effect=["VP Buff Agreement", "VP Buff Agreement History"]),
            ),
            patch("setup_cog._ensure_survey_tab", AsyncMock()),
        ):
            chosen = await setup_cog._ask_survey_tab(
                channel,
                prompt="pick a history tab",
                default="VP Buff Agreement History",
                current="",
                modal_title="Tab",
                cancel_event=None,
                guild_id=TEST_GUILD_ID,
                claimed_tabs={},
                also_claimed={"vp buff agreement": "VP Buff Agreement"},
            )

        assert chosen == "VP Buff Agreement History"

    @pytest.mark.asyncio
    async def test_cancelling_the_step_returns_none(self):
        import setup_cog

        channel = MagicMock()
        channel.send = AsyncMock()

        with (
            patch("setup_cog.ask_keep_or_change", AsyncMock(return_value=None)),
            patch("setup_cog._ensure_survey_tab", AsyncMock()) as ensure,
        ):
            chosen = await setup_cog._ask_survey_tab(
                channel,
                prompt="pick a tab",
                default="X",
                current="",
                modal_title="Tab",
                cancel_event=None,
                guild_id=TEST_GUILD_ID,
                claimed_tabs={},
                also_claimed={},
            )

        assert chosen is None
        ensure.assert_not_awaited()


class TestSurveyConfiguredView:
    """The confirmation embed offers the two things leadership almost
    always wants next, instead of sending them back out to /survey."""

    def _view(self, bot=None, owner_id=7):
        import setup_cog

        return setup_cog.SurveyConfiguredView(
            bot or MagicMock(),
            guild_id=TEST_GUILD_ID,
            survey_id="vp-buff-agreement",
            survey_name="VP Buff Agreement",
            template_key="scratch",
            owner_id=owner_id,
        )

    def test_offers_post_and_edit(self):
        from survey_hub import SURVEY_HUB_BTN_EDIT, SURVEY_HUB_BTN_POST

        labels = [c.label for c in self._view().children]
        assert labels == [SURVEY_HUB_BTN_POST, SURVEY_HUB_BTN_EDIT]

    @pytest.mark.asyncio
    async def test_only_the_person_who_ran_setup_can_use_them(self):
        view = self._view(owner_id=7)
        interaction = MagicMock()
        interaction.user.id = 99
        interaction.response.send_message = AsyncMock()

        assert await view.interaction_check(interaction) is False
        assert (
            "belong to whoever ran the setup" in (interaction.response.send_message.call_args[0][0])
        )

    @pytest.mark.asyncio
    async def test_the_wizard_runner_passes_the_check(self):
        view = self._view(owner_id=7)
        interaction = MagicMock()
        interaction.user.id = 7

        assert await view.interaction_check(interaction) is True

    @pytest.mark.asyncio
    async def test_post_button_posts_that_survey_without_a_picker(self, seeded_db):
        import config, setup_cog

        config.save_extra_survey(
            TEST_GUILD_ID,
            "vp-buff-agreement",
            survey_name="VP Buff Agreement",
            tab_squad_powers="VP Buff Agreement",
            tab_history="VP Buff Agreement History",
            template="scratch",
        )
        view = self._view()
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        interaction.followup.send = AsyncMock()

        with patch(
            "survey.post_survey_to_its_channel", AsyncMock(return_value=(True, "✅ posted"))
        ) as post:
            await setup_cog.SurveyConfiguredView._on_post(view, interaction)

        assert post.await_args[0][2]["survey_id"] == "vp-buff-agreement"
        interaction.followup.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_post_button_reports_a_survey_deleted_since_setup(self, seeded_db):
        import setup_cog

        view = self._view()
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()
        interaction.followup.send = AsyncMock()

        await setup_cog.SurveyConfiguredView._on_post(view, interaction)

        assert "no longer configured" in interaction.followup.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_edit_button_reopens_the_wizard_on_the_same_survey(self):
        import setup_cog

        view = self._view()
        interaction = MagicMock()
        interaction.response.edit_message = AsyncMock()

        with patch("setup_cog.run_survey_setup", AsyncMock()) as run:
            await setup_cog.SurveyConfiguredView._on_edit(view, interaction)

        assert run.await_args.kwargs["target_survey_id"] == "vp-buff-agreement"
        assert run.await_args.kwargs["template"] == "scratch"

    @pytest.mark.asyncio
    async def test_timeout_strips_the_buttons(self):
        import setup_cog

        view = self._view()
        view.message = MagicMock()
        with patch("wizard_registry.expire_view_message", AsyncMock()) as expire:
            await setup_cog.SurveyConfiguredView.on_timeout(view)

        expire.assert_awaited_once()


class TestPostedIntroFallback:
    def test_squad_power_survey_keeps_its_headline(self):
        from survey import _default_posted_intro

        intro = _default_posted_intro({"template": "squad_power", "survey_name": "Default"})
        assert "Let us know your Squad Powers!" in intro

    def test_other_surveys_announce_themselves_by_name(self):
        from survey import _default_posted_intro

        intro = _default_posted_intro({"template": "scratch", "survey_name": "VP Buff Agreement"})
        assert "VP Buff Agreement" in intro
        assert "Squad Powers" not in intro


class TestSeedSurveyHeaders:
    """Tabs get labelled when the survey is saved, not when the first
    member submits, so an alliance opening their sheet in between doesn't
    find two blank tabs to guess at."""

    QUESTIONS = [
        {"key": "agree", "label": "Agree?", "type": "dropdown", "options": ["Yes", "No"]},
        {"key": "notes", "label": "Notes", "type": "text"},
    ]

    def _sheet(self, *, responses_row1=None, history_row1=None):
        responses = MagicMock()
        responses.row_values = MagicMock(return_value=responses_row1 or [])
        history = MagicMock()
        history.row_values = MagicMock(return_value=history_row1 or [])
        sh = MagicMock()
        sh.worksheet = MagicMock(
            side_effect=lambda name: responses if name == "VP Buff" else history
        )
        return sh, responses, history

    def test_writes_both_headers_on_blank_tabs(self, seeded_db):
        from survey import seed_survey_headers

        sh, responses, history = self._sheet()
        with patch("survey._get_spreadsheet", return_value=sh):
            seeded = seed_survey_headers(
                TEST_GUILD_ID,
                tab_responses="VP Buff",
                tab_history="VP Buff History",
                questions=self.QUESTIONS,
            )

        assert seeded == ["VP Buff", "VP Buff History"]
        assert responses.update.call_args[0][1] == [
            ["Username", "Discord ID", "Agree?", "Notes", "Date Modified"]
        ]
        assert history.update.call_args[0][1] == [
            ["Timestamp", "Discord ID", "Username", "Agree?", "Notes"]
        ]

    def test_leaves_a_tab_that_already_has_a_header_alone(self, seeded_db):
        """Re-running the wizard must not relabel columns that existing
        rows were written under."""
        from survey import seed_survey_headers

        sh, responses, history = self._sheet(responses_row1=["Username", "Discord ID", "Old"])
        with patch("survey._get_spreadsheet", return_value=sh):
            seeded = seed_survey_headers(
                TEST_GUILD_ID,
                tab_responses="VP Buff",
                tab_history="VP Buff History",
                questions=self.QUESTIONS,
            )

        assert seeded == ["VP Buff History"]
        responses.update.assert_not_called()

    def test_history_tab_gets_a_filter_row(self, seeded_db):
        from survey import seed_survey_headers

        sh, responses, history = self._sheet()
        with patch("survey._get_spreadsheet", return_value=sh):
            seed_survey_headers(
                TEST_GUILD_ID,
                tab_responses="VP Buff",
                tab_history="VP Buff History",
                questions=self.QUESTIONS,
            )

        history.set_basic_filter.assert_called_once()
        responses.set_basic_filter.assert_not_called()

    def test_header_definition_matches_what_the_write_paths_produce(self):
        """One definition, so a seeded header can't drift from the row
        that later lands under it."""
        from survey import survey_header_rows, survey_question_keys_and_labels

        responses_header, history_header = survey_header_rows(self.QUESTIONS)
        q_keys, q_labels = survey_question_keys_and_labels(self.QUESTIONS)

        assert responses_header == ["Username", "Discord ID"] + q_labels + ["Date Modified"]
        assert history_header == ["Timestamp", "Discord ID", "Username"] + q_labels
        assert q_keys == ["agree", "notes"]


class TestSurveyWritesCreateTheirTabs:
    """A tab deleted after setup must not cost a member their answers."""

    def test_update_creates_a_missing_responses_tab(self, seeded_db):
        from survey import update_squad_powers

        ws = MagicMock()
        ws.get_all_values = MagicMock(return_value=[])
        sh = MagicMock()
        sh.worksheet = MagicMock(side_effect=Exception("Worksheet not found"))
        sh.add_worksheet = MagicMock(return_value=ws)

        survey = {
            "tab_squad_powers": "VP Buff Agreement",
            "questions": [{"key": "agree", "label": "Agree?", "type": "dropdown"}],
        }
        with patch("survey._get_spreadsheet", return_value=sh):
            update_squad_powers(
                "1", "Alice", {"agree": "Yes"}, guild_id=TEST_GUILD_ID, survey=survey
            )

        sh.add_worksheet.assert_called_once()
        ws.append_row.assert_called_once()

    def test_append_creates_a_missing_history_tab(self, seeded_db):
        from survey import append_survey_history

        ws = MagicMock()
        ws.row_values = MagicMock(return_value=[])
        sh = MagicMock()
        sh.worksheet = MagicMock(side_effect=Exception("Worksheet not found"))
        sh.add_worksheet = MagicMock(return_value=ws)

        survey = {
            "tab_history": "VP Buff Agreement History",
            "questions": [{"key": "agree", "label": "Agree?", "type": "dropdown"}],
        }
        with patch("survey._get_spreadsheet", return_value=sh):
            append_survey_history(
                "1", "Alice", {"agree": "Yes"}, guild_id=TEST_GUILD_ID, survey=survey
            )

        sh.add_worksheet.assert_called_once()
        ws.append_row.assert_called()
