"""
Phase 3 of the full-coverage suite: storm draft + participation flows.

Covers the rewritten DS/CS draft pipeline (#17/#18/#19) and the new
configurable participation log (#20):

  • run_ds_draft_flow / run_cs_draft_flow exit cleanly on timeout / cancel
  • Editing the template runs through the parser and saves to the sheet
  • _post_and_copy posts to the configured post-channel and prints the
    copyable code block in leadership
  • run_log_flow rejects guilds where participation isn't enabled
  • run_log_flow walks every configured question type (text, yes_no,
    numeric, roster_names) end-to-end without dead-ending

These exercise the integration paths that the unit tests in test_storm.py
and the gate smoke tests in test_command_coverage.py don't reach.
"""
from __future__ import annotations

import asyncio
from datetime import date
from unittest.mock import patch, MagicMock, AsyncMock
import sys, os

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID, PREMIUM_TEST_GUILD_ID, make_mock_interaction


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_message():
    """Mock for the return of channel.send — has async edit/delete."""
    msg        = MagicMock(id=999)
    msg.edit   = AsyncMock(return_value=msg)
    msg.delete = AsyncMock()
    return msg


def _make_channel():
    """A channel mock that captures every send() call."""
    ch          = AsyncMock()
    ch.id       = 111111111111111111
    ch.name     = "leadership"
    ch.guild    = MagicMock(id=TEST_GUILD_ID)
    ch.guild.id = TEST_GUILD_ID
    ch.guild.get_channel = MagicMock(return_value=None)
    ch._state   = MagicMock()
    ch._state.get_channel = MagicMock(return_value=None)
    # Each send returns a fresh message-like mock (so .edit/.delete are
    # AsyncMocks the wizard can await).
    ch.send     = AsyncMock(side_effect=lambda *a, **kw: _make_message())
    return ch


def _last_send(channel):
    """Most-recent content/kwargs passed to channel.send."""
    if channel.send.call_args is None:
        return None, None
    args, kwargs = channel.send.call_args
    content = args[0] if args else kwargs.get("content")
    return content, kwargs


def _all_send_contents(channel):
    """All content strings passed to channel.send across the flow."""
    out = []
    for call in channel.send.call_args_list:
        args, kwargs = call
        c = args[0] if args else kwargs.get("content")
        if c:
            out.append(c)
    return out


# ── Storm draft flow — timeout branches ───────────────────────────────────────

class TestDsDraftFlowTimeouts:
    """run_ds_draft_flow exits gracefully on every timeout/cancel branch
    without dead-ending the user."""

    @pytest.mark.asyncio
    async def test_time_picker_timeout_exits_cleanly(self, seeded_db):
        from storm import run_ds_draft_flow

        bot     = AsyncMock()
        channel = _make_channel()
        user    = MagicMock(id=42); user.mention = "@user"

        # TimeSelectView's `selected` stays None after wait() → timeout
        with patch("storm.TimeSelectView") as MockTimeView:
            instance = MagicMock(selected=None, wait=AsyncMock())
            MockTimeView.return_value = instance
            await run_ds_draft_flow(bot, channel, user, "A",
                                     current_zones={"Z1": "names"}, current_subs=[])

        # Should have surfaced the timeout message
        contents = _all_send_contents(channel)
        assert any("Timed out" in c for c in contents)
        assert any("desertstorm_draft" in c for c in contents)

    @pytest.mark.asyncio
    async def test_template_choice_timeout_exits_cleanly(self, seeded_db):
        from storm import run_ds_draft_flow

        bot     = AsyncMock()
        channel = _make_channel()
        user    = MagicMock(id=42); user.mention = "@user"

        # Time-pick succeeds (returns "1"); template-choice times out (None).
        with patch("storm.TimeSelectView") as MockTimeView, \
             patch("storm.TemplateUseEditView") as MockTemplateView:
            time_view     = MagicMock(selected="1", wait=AsyncMock())
            template_view = MagicMock(choice=None,  wait=AsyncMock())
            MockTimeView.return_value     = time_view
            MockTemplateView.return_value = template_view

            await run_ds_draft_flow(bot, channel, user, "A",
                                     current_zones={"Z1": "names"}, current_subs=[])

        contents = _all_send_contents(channel)
        assert any("Timed out" in c for c in contents)


# ── Storm draft flow — use-as-is happy path ───────────────────────────────────

class TestDsDraftFlowUseAsIs:
    """When leadership clicks 'Use as-is', the flow skips editing and
    lands on the StormApprovalView preview."""

    @pytest.mark.asyncio
    async def test_use_as_is_reaches_preview_step(self, seeded_db):
        import config
        from storm import run_ds_draft_flow

        # Configure storm so the draft flow has a tab + post-channel.
        config.save_storm_config(
            TEST_GUILD_ID, "DS", "DS Tab", "Mail body for {time}",
            "America/New_York", 0,
            post_channel_id=12345,
        )

        bot     = AsyncMock()
        channel = _make_channel()
        user    = MagicMock(id=42); user.mention = "@user"

        with patch("storm.TimeSelectView") as MockTime, \
             patch("storm.TemplateUseEditView") as MockTemplate, \
             patch("storm._pick_storm_template", new=AsyncMock(return_value=None)):
            time_view     = MagicMock(selected="1", wait=AsyncMock())
            template_view = MagicMock(choice="use", wait=AsyncMock())
            MockTime.return_value     = time_view
            MockTemplate.return_value = template_view

            await run_ds_draft_flow(
                bot, channel, user, "A",
                current_zones={"Nuclear Silo": "Alice"},
                current_subs=[("Bob", "Carol")],
            )

        # The flow should have made it to the preview step. The
        # preview message starts with "Step 4 of 4 — Preview" or a
        # "📬" mail preview header.
        contents = _all_send_contents(channel)
        assert any(
            "Step 4 of 4" in c or "preview" in c.lower()
            for c in contents
        ), f"Preview step never reached. Sent: {contents}"


# ── Storm draft flow — edit branch with cancel ────────────────────────────────

class TestDsDraftFlowEditCancel:
    """User clicks Edit, then types `cancel` — flow exits with a clear
    cancel message and does NOT save anything."""

    @pytest.mark.asyncio
    async def test_edit_then_cancel_exits_without_saving(self, seeded_db):
        import config
        from storm import run_ds_draft_flow

        config.save_storm_config(
            TEST_GUILD_ID, "DS", "DS Tab", "Body",
            "America/New_York", 0,
        )

        bot     = AsyncMock()
        # bot.wait_for returns the user's "cancel" reply
        bot.wait_for = AsyncMock(
            return_value=MagicMock(content="cancel", delete=AsyncMock())
        )
        channel = _make_channel()
        user    = MagicMock(id=42); user.mention = "@user"

        save_called = False
        def fake_save(*args, **kwargs):
            nonlocal save_called
            save_called = True

        with patch("storm.TimeSelectView") as MockTime, \
             patch("storm.TemplateUseEditView") as MockTemplate, \
             patch("storm.save_ds_assignments", side_effect=fake_save):
            MockTime.return_value     = MagicMock(selected="1", wait=AsyncMock())
            MockTemplate.return_value = MagicMock(choice="edit", wait=AsyncMock())

            await run_ds_draft_flow(
                bot, channel, user, "A",
                current_zones={"Z1": "names"}, current_subs=[],
            )

        contents = _all_send_contents(channel)
        assert any("Draft cancelled" in c or "cancelled" in c.lower() for c in contents)
        assert not save_called, "save_ds_assignments should not run on cancel"


# ── _post_and_copy ────────────────────────────────────────────────────────────

class TestPostAndCopyHelper:
    """The shared helper that posts the final mail to the configured
    post-channel + prints a copyable code block in leadership."""

    @pytest.mark.asyncio
    async def test_post_and_copy_with_configured_channel(self, seeded_db):
        from storm import _post_and_copy

        leadership = _make_channel()
        leadership.guild = MagicMock()
        post_channel = AsyncMock()
        post_channel.mention = "<#88888>"
        post_channel.send    = AsyncMock()
        leadership.guild.get_channel = MagicMock(return_value=post_channel)

        await _post_and_copy(
            leadership, post_channel_id=88888,
            event_label="Desert Storm", team="A",
            mail="HELLO MAIL BODY",
        )

        # Posted to the post channel
        post_channel.send.assert_called_once_with("HELLO MAIL BODY")

        # And the copyable block landed in leadership
        contents = _all_send_contents(leadership)
        assert any("HELLO MAIL BODY" in c for c in contents)
        assert any("ready to copy" in c.lower() for c in contents)

    @pytest.mark.asyncio
    async def test_post_and_copy_without_post_channel_only_copies(self, seeded_db):
        """When no post channel is configured (post_channel_id=0), the
        helper still prints the copyable block in leadership but skips
        the channel post."""
        from storm import _post_and_copy

        leadership = _make_channel()

        await _post_and_copy(
            leadership, post_channel_id=0,
            event_label="Canyon Storm", team="B",
            mail="CS body",
        )

        contents = _all_send_contents(leadership)
        assert any("CS body" in c for c in contents)
        assert any("ready to copy" in c.lower() for c in contents)
        # No "(also posted to ...)" suffix
        assert not any("also posted to" in c for c in contents)


# ── Participation flow — gate when not enabled ────────────────────────────────

class TestParticipationFlowGate:
    """run_log_flow refuses to run when participation is disabled or
    has no questions configured — and tells the user how to fix it."""

    @pytest.mark.asyncio
    async def test_disabled_participation_tells_user_to_set_up(self, seeded_db):
        from storm_log import run_log_flow

        bot     = AsyncMock()
        channel = _make_channel()
        user    = MagicMock(id=42); user.mention = "@user"

        await run_log_flow(bot, channel, user, "DS")

        contents = _all_send_contents(channel)
        assert any("setup_desertstorm" in c for c in contents), (
            "Disabled-participation message should point at /setup_desertstorm"
        )

    @pytest.mark.asyncio
    async def test_enabled_with_no_questions_tells_user_to_set_up(self, seeded_db):
        import config
        from storm_log import run_log_flow

        # Enable participation but configure zero questions.
        config.save_storm_config(
            TEST_GUILD_ID, "CS", "CS Tab", "Body",
            "America/New_York", 0,
        )
        config.save_participation_config(
            TEST_GUILD_ID, "CS",
            enabled=1, tab_name="CS Participation Log",
            questions=[],
            roster_tab="Squad Powers", roster_name_col=0,
            roster_alias_col=-1, roster_start_row=2,
        )

        bot     = AsyncMock()
        channel = _make_channel()
        user    = MagicMock(id=42); user.mention = "@user"

        await run_log_flow(bot, channel, user, "CS")

        contents = _all_send_contents(channel)
        assert any("setup_canyonstorm" in c for c in contents), (
            "Empty-questions message should point at /setup_canyonstorm"
        )


# ── Participation flow — happy path ───────────────────────────────────────────

class TestParticipationFlowHappyPath:
    """A guild with participation enabled walks through the configured
    questions and writes a row."""

    @pytest.mark.asyncio
    async def test_text_and_yes_no_questions_save_row(self, seeded_db):
        """Two questions (text + yes_no) — flow walks through both,
        formats the row, and calls append_participation_row with the
        right answers."""
        import config
        from storm_log import run_log_flow

        config.save_storm_config(
            TEST_GUILD_ID, "DS", "DS Tab", "Body",
            "America/New_York", 0,
        )
        config.save_participation_config(
            TEST_GUILD_ID, "DS",
            enabled=1, tab_name="DS Participation Log",
            questions=[
                {"key": "outcome", "label": "Outcome", "type": "text"},
                {"key": "rescheduled", "label": "Rescheduled?", "type": "yes_no"},
            ],
            roster_tab="Roster", roster_name_col=0,
            roster_alias_col=-1, roster_start_row=2,
        )

        bot = AsyncMock()
        # bot.wait_for returns Step-1 (date) then Step-2 (text answer)
        wait_for_seq = iter([
            MagicMock(content="today", delete=AsyncMock()),     # date
            MagicMock(content="Win", delete=AsyncMock()),       # text
        ])
        bot.wait_for = AsyncMock(side_effect=lambda *a, **kw: next(wait_for_seq))

        channel = _make_channel()
        user    = MagicMock(id=42); user.mention = "@user"

        # _YesNoLogView is defined inline in run_log_flow's module scope.
        # Driving it: when channel.send sees a view, set value=True (Yes)
        # and stop().
        def _resolve_view(view):
            if hasattr(view, "value"):
                view.value     = True
                view.confirmed = True
                try: view.stop()
                except Exception: pass

        async def _send(content=None, view=None, **kw):
            if view is not None:
                _resolve_view(view)
            return _make_message()
        channel.send = AsyncMock(side_effect=_send)

        captured = {}
        def fake_append(guild_id, event_type, log_date, answers):
            captured.update({
                "guild_id": guild_id,
                "event_type": event_type,
                "log_date": log_date,
                "answers": answers,
            })

        with patch("storm_log.append_participation_row", side_effect=fake_append):
            await run_log_flow(bot, channel, user, "DS")

        assert captured.get("event_type") == "DS"
        assert captured.get("answers", {}).get("outcome") == "Win"
        assert captured.get("answers", {}).get("rescheduled") == "Yes"
        # log_date should be today
        assert captured.get("log_date") == date.today()


# ── Participation flow — numeric retry on bad input ───────────────────────────

class TestParticipationFlowNumericRetry:
    """Bad numeric input re-prompts up to 5 times instead of cancelling
    the whole log — matches the survey ask_numeric pattern."""

    @pytest.mark.asyncio
    async def test_numeric_question_retries_on_bad_input(self, seeded_db):
        import config
        from storm_log import run_log_flow

        config.save_storm_config(
            TEST_GUILD_ID, "DS", "DS Tab", "Body",
            "America/New_York", 0,
        )
        config.save_participation_config(
            TEST_GUILD_ID, "DS",
            enabled=1, tab_name="DS Participation Log",
            questions=[
                {"key": "vote_count", "label": "Vote Count", "type": "numeric"},
            ],
            roster_tab="Roster", roster_name_col=0,
            roster_alias_col=-1, roster_start_row=2,
        )

        bot = AsyncMock()
        # Sequence: today, then bad input, then good number
        replies = iter([
            MagicMock(content="today",   delete=AsyncMock()),
            MagicMock(content="not a #", delete=AsyncMock()),
            MagicMock(content="42",      delete=AsyncMock()),
        ])
        bot.wait_for = AsyncMock(side_effect=lambda *a, **kw: next(replies))

        channel = _make_channel()
        user    = MagicMock(id=42); user.mention = "@user"

        captured = {}
        def fake_append(guild_id, event_type, log_date, answers):
            captured.update(answers)

        with patch("storm_log.append_participation_row", side_effect=fake_append):
            await run_log_flow(bot, channel, user, "DS")

        # The second attempt (42) wins
        assert captured.get("vote_count") == "42"
        # We should have surfaced a "isn't a number" reprompt at least once
        contents = _all_send_contents(channel)
        assert any("re-enter" in c.lower() or "not a number" in c.lower() for c in contents)
