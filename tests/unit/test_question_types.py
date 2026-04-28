"""
Unit tests for the new premium-only survey question types added in
Sprint C — Phase 5: numeric, multi_select, date.

The collection helpers live as inner functions inside `survey.run_survey`
so we test them by driving `run_survey` end-to-end with a single question
of each type. The default `_finalize_survey_thread` is patched out to
keep the test fast.
"""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID


def _make_message(content: str):
    msg = MagicMock()
    msg.content = content
    return msg


def _make_user(guild_id=TEST_GUILD_ID):
    user                   = MagicMock()
    user.id                = 999_111
    user.display_name      = "Tester"
    guild                  = MagicMock()
    guild.id               = guild_id
    user.guild             = guild
    return user


def _make_thread():
    t        = AsyncMock()
    t.send   = AsyncMock(return_value=MagicMock(id=1))
    return t


# ── Numeric ───────────────────────────────────────────────────────────────────

class TestNumeric:

    @pytest.mark.asyncio
    async def test_valid_integer_in_bounds(self, seeded_db):
        from survey import run_survey
        from config import save_survey_config

        save_survey_config(
            TEST_GUILD_ID, "Stats", "History",
            [{"key": "score", "label": "Score", "type": "numeric",
              "options": [], "placeholder": "", "max_chars": 0,
              "min": 0, "max": 100}],
            "Intro",
        )

        thread = _make_thread()
        user   = _make_user()
        bot    = AsyncMock()
        bot.get_channel = MagicMock(return_value=None)
        bot.wait_for    = AsyncMock(return_value=_make_message("42"))

        captured = {}
        def fake_update(did, name, data, guild_id=None, survey=None):
            captured.update(data)

        with patch("survey.update_squad_powers", side_effect=fake_update), \
             patch("survey.append_survey_history"), \
             patch("survey._finalize_survey_thread", new_callable=AsyncMock):
            await run_survey(bot, thread, user)

        assert captured.get("score") == "42"

    @pytest.mark.asyncio
    async def test_below_min_rejects_and_exits(self, seeded_db):
        from survey import run_survey
        from config import save_survey_config

        save_survey_config(
            TEST_GUILD_ID, "Stats", "History",
            [{"key": "score", "label": "Score", "type": "numeric",
              "options": [], "placeholder": "", "max_chars": 0, "min": 10}],
            "Intro",
        )
        thread = _make_thread()
        user   = _make_user()
        bot    = AsyncMock()
        bot.get_channel = MagicMock(return_value=None)
        bot.wait_for    = AsyncMock(return_value=_make_message("5"))

        with patch("survey.update_squad_powers") as up, \
             patch("survey.append_survey_history"), \
             patch("survey._finalize_survey_thread", new_callable=AsyncMock):
            await run_survey(bot, thread, user)
            up.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_number_rejects_and_exits(self, seeded_db):
        from survey import run_survey
        from config import save_survey_config

        save_survey_config(
            TEST_GUILD_ID, "Stats", "History",
            [{"key": "score", "label": "Score", "type": "numeric",
              "options": [], "placeholder": "", "max_chars": 0}],
            "Intro",
        )
        thread = _make_thread()
        user   = _make_user()
        bot    = AsyncMock()
        bot.get_channel = MagicMock(return_value=None)
        bot.wait_for    = AsyncMock(return_value=_make_message("forty-two"))

        with patch("survey.update_squad_powers") as up, \
             patch("survey.append_survey_history"), \
             patch("survey._finalize_survey_thread", new_callable=AsyncMock):
            await run_survey(bot, thread, user)
            up.assert_not_called()

    @pytest.mark.asyncio
    async def test_float_accepted(self, seeded_db):
        from survey import run_survey
        from config import save_survey_config

        save_survey_config(
            TEST_GUILD_ID, "Stats", "History",
            [{"key": "score", "label": "Score", "type": "numeric",
              "options": [], "placeholder": "", "max_chars": 0}],
            "Intro",
        )
        thread = _make_thread()
        user   = _make_user()
        bot    = AsyncMock()
        bot.get_channel = MagicMock(return_value=None)
        bot.wait_for    = AsyncMock(return_value=_make_message("43.27"))

        captured = {}
        with patch("survey.update_squad_powers",
                   side_effect=lambda *a, **kw: captured.update(a[2])), \
             patch("survey.append_survey_history"), \
             patch("survey._finalize_survey_thread", new_callable=AsyncMock):
            await run_survey(bot, thread, user)

        assert captured.get("score") == "43.27"


# ── Date ──────────────────────────────────────────────────────────────────────

class TestDate:

    @pytest.mark.asyncio
    async def test_valid_date_returns_iso(self, seeded_db):
        from survey import run_survey
        from config import save_survey_config

        save_survey_config(
            TEST_GUILD_ID, "Stats", "History",
            [{"key": "joined", "label": "Date joined", "type": "date",
              "options": [], "placeholder": "", "max_chars": 0,
              "date_format": "%m/%d/%Y"}],
            "Intro",
        )
        thread = _make_thread()
        user   = _make_user()
        bot    = AsyncMock()
        bot.get_channel = MagicMock(return_value=None)
        bot.wait_for    = AsyncMock(return_value=_make_message("03/15/2026"))

        captured = {}
        with patch("survey.update_squad_powers",
                   side_effect=lambda *a, **kw: captured.update(a[2])), \
             patch("survey.append_survey_history"), \
             patch("survey._finalize_survey_thread", new_callable=AsyncMock):
            await run_survey(bot, thread, user)

        assert captured.get("joined") == "2026-03-15"

    @pytest.mark.asyncio
    async def test_unparseable_date_rejects_and_exits(self, seeded_db):
        from survey import run_survey
        from config import save_survey_config

        save_survey_config(
            TEST_GUILD_ID, "Stats", "History",
            [{"key": "joined", "label": "Date joined", "type": "date",
              "options": [], "placeholder": "", "max_chars": 0,
              "date_format": "%m/%d/%Y"}],
            "Intro",
        )
        thread = _make_thread()
        user   = _make_user()
        bot    = AsyncMock()
        bot.get_channel = MagicMock(return_value=None)
        bot.wait_for    = AsyncMock(return_value=_make_message("not a date"))

        with patch("survey.update_squad_powers") as up, \
             patch("survey.append_survey_history"), \
             patch("survey._finalize_survey_thread", new_callable=AsyncMock):
            await run_survey(bot, thread, user)
            up.assert_not_called()


# ── Multi-select ──────────────────────────────────────────────────────────────

class TestMultiSelect:

    @pytest.mark.asyncio
    async def test_multi_select_returns_comma_joined(self, seeded_db):
        """The view's callback packs selected values into a comma-joined string."""
        from survey import run_survey
        from config import save_survey_config

        save_survey_config(
            TEST_GUILD_ID, "Stats", "History",
            [{"key": "roles", "label": "Pick all that apply", "type": "multi_select",
              "options": ["Tank", "Air", "Missile"],
              "placeholder": "Pick…", "max_chars": 0}],
            "Intro",
        )
        thread = _make_thread()
        user   = _make_user()
        bot    = AsyncMock()
        bot.get_channel = MagicMock(return_value=None)

        # The multi-select view is built inside survey.run_survey. Drive it by
        # patching thread.send to set the values + stop the view inline.
        async def fake_send(content=None, view=None, **kw):
            if view is not None and len(view.children) and isinstance(
                view.children[0], __import__("discord").ui.Select,
            ):
                # Simulate Discord setting the select's values, then call the
                # callback by hand to populate the result-dict closure.
                sel = view.children[0]
                sel._values = ["Tank", "Air"]   # internal state for .values
                # The actual callback closure lives in run_survey; since we
                # can't reach that closure directly, we set the view.stop()
                # path via the public attributes to make ask_multi_select see
                # values. We mimic by triggering the callback if attached.
                if sel.callback is not None:
                    inter = AsyncMock()
                    await sel.callback(inter)
            return MagicMock(id=1)

        thread.send = AsyncMock(side_effect=fake_send)

        captured = {}

        def grab(did, name, data, guild_id=None, survey=None):
            captured.update(data)

        with patch("survey.update_squad_powers", side_effect=grab), \
             patch("survey.append_survey_history"), \
             patch("survey._finalize_survey_thread", new_callable=AsyncMock):
            # The select.values property reads from the underlying Discord
            # state; mock it via the descriptor.
            with patch("discord.ui.Select.values",
                       new_callable=lambda: property(lambda self: ["Tank", "Air"])):
                await run_survey(bot, thread, user)

        assert captured.get("roles") == "Tank, Air"
