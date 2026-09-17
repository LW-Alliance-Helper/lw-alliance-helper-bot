"""The sign-up schedule sub-flow (`_ask_signup_schedule`), driven directly.

Written against the function in `setup_cog` before its move to
`wizard_time.py` (#611 round 2) and kept unchanged after it. Until now the
only test that touched it patched it away. These drive the real day picker
and hold the prompt, the day options per event type, the Keep current
label, the skip path, the time step's parsing and re-prompt, and the
cancel and timeout exits.
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# setup_cog re-exports the wizard pieces by name at import time; import it
# before anything patches `wizard_steps.X` (see CLAUDE.md, test gotchas).
import setup_cog  # noqa: F401

from tests.integration.test_setup_flows import patch_keep_or_change  # noqa: E402
from messages import GENERIC_CMD_TIMEOUT  # noqa: E402

CMD = "setup → ⚔️ Desert Storm"
TIMEOUT = GENERIC_CMD_TIMEOUT.format(cmd=CMD)
DOW = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _flow():
    """Resolve the function at call time so the same tests run before and
    after the move out of `setup_cog`."""
    import setup_cog

    return setup_cog._ask_signup_schedule


class Script:
    """Answer each posted view in order; record everything sent."""

    def __init__(self, channel, cancel_event, answers):
        self.answers = list(answers)
        self.sent = []
        self.views = []
        self.cancel_event = cancel_event
        channel.send = AsyncMock(side_effect=self._send)

    async def _send(self, content=None, embed=None, view=None, **kw):
        self.sent.append(content or "")
        if view is None:
            return MagicMock(id=1)
        self.views.append(view)
        answer = self.answers.pop(0)
        if answer == "cancel":
            self.cancel_event.set()
            return MagicMock(id=1)
        if answer != "timeout":
            attr, value = answer
            setattr(view, attr, value)
        view.stop()
        return MagicMock(id=1)


async def _drive(
    answers, *, times=(), event_type="DS", current_dow=-1, current_time="", tz_label=""
):
    channel = MagicMock()
    cancel_event = asyncio.Event()
    script = Script(channel, cancel_event, answers)
    with patch_keep_or_change(list(times)):
        result = await _flow()(
            channel,
            AsyncMock(),
            MagicMock(),
            cancel_event,
            label="Desert Storm" if event_type == "DS" else "Canyon Storm",
            cmd_name=CMD,
            current_dow=current_dow,
            current_time=current_time,
            tz_label=tz_label,
            event_type=event_type,
        )
    return result, script


def _select(view):
    return next(c for c in view.children if hasattr(c, "options"))


def _keep(view):
    return view.children[0]


class TestDayPicker:
    @pytest.mark.asyncio
    async def test_desert_storm_offers_saturday_through_wednesday(self):
        result, s = await _drive([("selected", 2)], times=["2:00pm"])
        prompt = s.sent[0]
        assert prompt.startswith("**Auto-Schedule: Poll Day (💎 Premium)**\n")
        assert "**Desert Storm** runs every **Friday** in-game." in prompt
        sel = _select(s.views[0])
        assert [o.label for o in sel.options] == [
            "Saturday",
            "Sunday",
            "Monday",
            "Tuesday",
            "Wednesday",
            "Skip auto-scheduling (post manually from the hub)",
        ]
        assert [o.value for o in sel.options] == ["5", "6", "0", "1", "2", "-1"]
        assert sel.placeholder == "When should the bot post the sign-up poll?"
        assert [o.default for o in sel.options] == [False] * 5 + [True]
        assert result == {"dow": 2, "time": "14:00"}

    @pytest.mark.asyncio
    async def test_canyon_storm_offers_friday_through_monday(self):
        _, s = await _drive([("selected", 0)], times=["9:00am"], event_type="CS")
        assert "**Canyon Storm** runs every **Thursday** in-game." in s.sent[0]
        assert [o.value for o in _select(s.views[0]).options] == ["4", "5", "6", "0", "-1"]

    @pytest.mark.asyncio
    async def test_no_saved_day_keeps_skip(self):
        _, s = await _drive([("selected", -1)])
        assert _keep(s.views[0]).label == "Keep current: Skip auto-scheduling"

    @pytest.mark.asyncio
    async def test_saved_day_is_named_on_keep_current_and_marked_default(self):
        _, s = await _drive([("selected", 5)], times=[""], current_dow=5, current_time="18:00")
        assert _keep(s.views[0]).label == "Keep current: Saturday"
        sel = _select(s.views[0])
        assert [o.default for o in sel.options] == [True, False, False, False, False, False]

    @pytest.mark.asyncio
    async def test_skip_returns_a_cleared_schedule_and_asks_no_time(self):
        result, s = await _drive([("selected", -1)])
        assert result == {"dow": -1, "time": ""}
        assert len(s.sent) == 1

    @pytest.mark.asyncio
    async def test_timeout_posts_the_route_back(self):
        result, s = await _drive(["timeout"])
        assert result is None
        assert s.sent[-1] == TIMEOUT

    @pytest.mark.asyncio
    async def test_cancel_is_silent(self):
        result, s = await _drive(["cancel"])
        assert result is None
        assert s.sent == [s.sent[0]]


class TestDayPickerClicks:
    async def _view(self, current=-1, event_type="DS"):
        """Post the picker and grab it without answering, via a timeout."""
        _, s = await _drive(["timeout"], current_dow=current, event_type=event_type)
        return s.views[0]

    @pytest.mark.asyncio
    async def test_keep_current_with_a_saved_day(self):
        view = await self._view(current=6)
        edited = {}

        async def _edit(inter, **kw):
            edited.update(kw)

        with patch("wizard_registry.safe_edit_response", side_effect=_edit):
            await _keep(view).callback(MagicMock())
        assert view.selected == 6
        assert edited["content"] == "✅ Keeping poll day: **Sunday**."
        assert all(c.disabled for c in view.children)

    @pytest.mark.asyncio
    async def test_keep_current_without_a_saved_day_stays_skipped(self):
        view = await self._view(current=-1)
        edited = {}

        async def _edit(inter, **kw):
            edited.update(kw)

        with patch("wizard_registry.safe_edit_response", side_effect=_edit):
            await _keep(view).callback(MagicMock())
        assert view.selected == -1
        assert edited["content"] == (
            "✅ Auto-scheduling stays skipped. Post manually via `/desertstorm` → "
            "**📣 Post sign-up poll** when you're ready."
        )

    @pytest.mark.asyncio
    async def test_picking_a_day_from_the_dropdown(self):
        view = await self._view()
        sel = _select(view)
        sel._values = ["1"]
        edited = {}

        async def _edit(inter, **kw):
            edited.update(kw)

        with patch("wizard_registry.safe_edit_response", side_effect=_edit):
            await sel.callback(MagicMock())
        assert view.selected == 1
        assert edited["content"] == "✅ Poll day: **Tuesday**."

    @pytest.mark.asyncio
    async def test_picking_skip_from_the_dropdown(self):
        view = await self._view(event_type="CS")
        sel = _select(view)
        sel._values = ["-1"]
        edited = {}

        async def _edit(inter, **kw):
            edited.update(kw)

        with patch("wizard_registry.safe_edit_response", side_effect=_edit):
            await sel.callback(MagicMock())
        assert view.selected == -1
        assert edited["content"] == (
            "✅ Auto-scheduling skipped. Post manually via `/canyonstorm` → "
            "**📣 Post sign-up poll** when you're ready."
        )


class TestTimeStep:
    @pytest.mark.asyncio
    async def test_prompt_names_the_timezone_and_the_saved_time(self):
        calls = []

        async def _record(*a, **kw):
            calls.append((a, kw))
            return "6:00pm"

        channel = MagicMock()
        channel.send = AsyncMock(
            side_effect=Script(channel, asyncio.Event(), [("selected", 1)])._send
        )
        with (
            patch("setup_cog.ask_keep_or_change", side_effect=_record),
            patch("wizard_steps.ask_keep_or_change", side_effect=_record),
        ):
            result = await _flow()(
                channel,
                AsyncMock(),
                MagicMock(),
                asyncio.Event(),
                label="Desert Storm",
                cmd_name=CMD,
                current_dow=1,
                current_time="18:30",
                tz_label="(UTC-5) Eastern (New York)",
                event_type="DS",
            )
        assert result == {"dow": 1, "time": "18:00"}
        ((a, kw),) = calls
        assert a[1] == (
            "**Auto-Schedule: Sign-Up Post Time**\n"
            "What time should the bot fire the sign-up post? *(in your timezone: (UTC-5) Eastern (New York))*\n"
            "*(e.g. `2:00pm`, `9:00am`, or 24-hour `14:00`)*"
        )
        assert kw["default"] == "12:00pm"
        assert kw["current"] == "6:30pm"
        assert kw["modal_title"] == "Sign-Up Time" and kw["modal_label"] == "e.g. 2:00pm"

    @pytest.mark.asyncio
    async def test_twenty_four_hour_input_is_kept(self):
        result, _ = await _drive([("selected", 5)], times=["14:00"])
        assert result == {"dow": 5, "time": "14:00"}

    @pytest.mark.asyncio
    async def test_a_blank_time_is_nudged_then_accepted(self):
        result, s = await _drive([("selected", 5)], times=["", "9:00am"])
        assert result == {"dow": 5, "time": "09:00"}
        assert s.sent[-1] == (
            "⚠️ A sign-up time is required when auto-scheduling is on. "
            "Pick a time (e.g. `12:00pm`) or use the default."
        )

    @pytest.mark.asyncio
    async def test_three_blank_times_fall_back_to_noon(self):
        result, s = await _drive([("selected", 5)], times=["", "", ""])
        assert result == {"dow": 5, "time": "12:00"}
        assert sum("A sign-up time is required" in t for t in s.sent) == 3

    @pytest.mark.asyncio
    async def test_abandoning_the_time_step_returns_none(self):
        result, _ = await _drive([("selected", 5)], times=[None])
        assert result is None
