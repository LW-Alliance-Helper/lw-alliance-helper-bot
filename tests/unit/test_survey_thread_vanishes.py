"""A survey thread deleted mid-run (#432).

Sentry: `NotFound: 404 Unknown Channel` out of `ask_numeric`'s timeout branch.
The sequence is a survey that sat unanswered until SURVEY_TIMEOUT, then tried
to post "survey timed out" into a thread that no longer existed.

A survey runs for as long as the member takes, so the thread can go at any
point — an officer tidying up, Discord's own thread cleanup, the member
leaving. Once it's gone every remaining prompt, retry and timeout message is
undeliverable, so the run has nothing left to do but stop quietly.

Deliberately not filtered out in sentry_filter: `NotFound` is real signal
elsewhere (a configured channel that vanished is #379's business), so this is
fixed at the source instead.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ.setdefault("DISCORD_TOKEN", "fake-test-token")

import survey  # noqa: E402


def _interaction():
    inter = MagicMock()
    inter.guild_id = 4242
    inter.user = MagicMock()
    inter.user.id = 77
    inter.user.name = "member"
    inter.user.roles = [MagicMock(name="Member")]
    inter.user.roles[0].name = "Member"
    inter.response = AsyncMock()
    inter.followup = AsyncMock()
    inter.client = MagicMock()
    thread = AsyncMock()
    thread.id = 999
    thread.mention = "<#999>"
    inter.channel = AsyncMock()
    inter.channel.create_thread = AsyncMock(return_value=thread)
    return inter, thread


def _cfg():
    cfg = MagicMock()
    cfg.setup_complete = True
    cfg.member_role_name = "Member"
    cfg.survey_translate_bot_id = 0
    return cfg


def _response(status: int):
    resp = MagicMock()
    resp.status = status
    resp.reason = "err"
    return resp


class TestThreadVanishesMidSurvey:
    async def _run(self, side_effect):
        inter, thread = _interaction()
        with (
            patch("survey.get_config", MagicMock(return_value=_cfg())),
            patch("config.get_survey", MagicMock(return_value={"questions": [{"q": "x"}]})),
            patch("survey.add_translation_helper", AsyncMock()),
            patch("survey.run_survey", AsyncMock(side_effect=side_effect)) as run,
        ):
            await survey._start_survey_answer_flow(inter, survey_id="default")
        return run

    @pytest.mark.asyncio
    async def test_deleted_thread_does_not_raise(self):
        """The exact #432 shape. Before this it escaped into the View callback,
        where discord.py logs it and Sentry files an issue for something no
        code change could have prevented."""
        run = await self._run(discord.NotFound(_response(404), "Unknown Channel"))
        run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lost_access_does_not_raise(self):
        run = await self._run(discord.Forbidden(_response(403), "Missing Access"))
        run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_real_bug_still_propagates(self):
        """Only the thread-is-gone shapes are swallowed. A genuine error in the
        survey flow must still reach Sentry."""
        with pytest.raises(RuntimeError):
            await self._run(RuntimeError("schema drift"))

    @pytest.mark.asyncio
    async def test_a_normal_run_is_untouched(self):
        run = await self._run(None)
        run.assert_awaited_once()


def test_not_found_is_still_reported_generally():
    """Guard against someone "fixing" this by filtering NotFound globally.
    A vanished *configured* channel is #379's signal and must keep paging."""
    import sentry_filter

    assert sentry_filter.drop_reason(discord.NotFound(_response(404), "Unknown Channel")) is None


if __name__ == "__main__":
    pytest.main([__file__])
