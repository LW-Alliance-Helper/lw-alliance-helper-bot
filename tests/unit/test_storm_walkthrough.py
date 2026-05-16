"""
Tests for storm_walkthrough.py + the walkthrough_dismissals layer (#130).

Covers the dismissal table, the tour content shape, and the View-flow
state machine — the last of these is what the prior round was missing,
which let a "Next button re-renders step 1 forever" bug ship.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

import storm_walkthrough as sw


from tests.unit.test_config import TEST_GUILD_ID


def _make_interaction(user_id: int, message=None) -> MagicMock:
    """Minimal stand-in for `discord.Interaction` covering the surface
    the tour buttons actually touch."""
    inter = MagicMock()
    inter.user = MagicMock()
    inter.user.id = user_id
    inter.guild_id = TEST_GUILD_ID
    inter.message = message
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.edit_message = AsyncMock()
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock(return_value=MagicMock())
    return inter


class TestWalkthroughDismissals:
    def test_unseen_returns_false(self, seeded_db):
        import config
        assert config.is_walkthrough_dismissed(
            TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY,
        ) is False

    def test_dismiss_then_check(self, seeded_db):
        import config
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY)
        assert config.is_walkthrough_dismissed(
            TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY,
        ) is True

    def test_dismiss_idempotent(self, seeded_db):
        import config
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY)
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY)
        # Still just one record; still dismissed.
        assert config.is_walkthrough_dismissed(
            TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY,
        ) is True

    def test_per_user_isolation(self, seeded_db):
        import config
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY)
        # A different user in the same guild still gets offered the tour.
        assert config.is_walkthrough_dismissed(
            TEST_GUILD_ID, 99, sw.STORM_SIGNUPS_TOUR_KEY,
        ) is False

    def test_per_guild_isolation(self, seeded_db):
        import config
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_SIGNUPS_TOUR_KEY)
        # The same user in a different guild still gets offered the tour.
        assert config.is_walkthrough_dismissed(
            TEST_GUILD_ID + 1, 42, sw.STORM_SIGNUPS_TOUR_KEY,
        ) is False

    def test_versioned_key_isolates_dismissals(self, seeded_db):
        import config
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, "storm_signups_v0")
        # A v1 walkthrough should NOT be auto-dismissed just because v0 was.
        assert config.is_walkthrough_dismissed(
            TEST_GUILD_ID, 42, "storm_signups_v1",
        ) is False


class TestTourContent:
    def test_tour_has_at_least_three_steps(self):
        # The design spec calls for ~6 steps; lock in a floor so a
        # future content edit can't accidentally drop the tour to a
        # single message.
        assert len(sw._STORM_SIGNUPS_TOUR_STEPS) >= 3

    def test_first_step_starts_with_step_label(self):
        # Each step in the tour body starts with a bolded "Step N / M"
        # so the user can tell where they are.
        assert sw._STORM_SIGNUPS_TOUR_STEPS[0].startswith("**Step 1 ")

    def test_steps_are_individually_short(self):
        # Discord ephemeral messages render best at a few sentences each.
        # 800 chars is a soft cap that still lets us write paragraphs.
        for step in sw._STORM_SIGNUPS_TOUR_STEPS:
            assert len(step) < 800, f"Step too long ({len(step)} chars):\n{step}"

    def test_step_5_mentions_team_setup_buttons(self):
        # Post Rule A / #166: CS reads the same `teams=both/A/B` config
        # as DS does, so both event types render "🅰️ Set up Team A" /
        # "🅱️ Set up Team B" (or just the configured single team).
        # The pre-#166 CS-only "🏜️ Set up Roster" button is gone.
        step_5 = sw._STORM_SIGNUPS_TOUR_STEPS[4]
        assert "Team A" in step_5 and "Team B" in step_5


class TestTourStepProgression:
    """The tour is six steps; each Next click must advance the index.

    Before the fix, the Next callback bumped a state-dict index, then
    re-entered `_start_tour`, which built a fresh state at 0 — so the
    tour rendered "Step 1 / 6" forever. This class drives the View
    callbacks directly and asserts the index advances.
    """

    @pytest.mark.asyncio
    async def test_next_button_advances_to_step_2(self):
        steps = ["one", "two", "three"]
        view = sw._TourStepView(steps=steps, index=0,
                                owner_id=42, is_last=False)
        # The view exposes Next + Skip; the first child is Next.
        assert len(view.children) == 2
        inter = _make_interaction(user_id=42, message=MagicMock(content="one"))
        await view.children[0].callback(inter)

        # _send_tour_step is reached via followup.send; the new message
        # should contain step 2.
        inter.followup.send.assert_awaited_once()
        sent_kwargs = inter.followup.send.await_args.kwargs
        assert sent_kwargs["content"] == "two"
        assert sent_kwargs["ephemeral"] is True
        # The originating view has stopped + disabled its buttons.
        assert view.is_finished()
        assert all(c.disabled for c in view.children)

    @pytest.mark.asyncio
    async def test_full_walkthrough_reaches_final_step(self):
        """Chain Next → Next → … through every non-final step and
        verify the followup.send contents walk 0 → N-1 in order."""
        steps = sw._STORM_SIGNUPS_TOUR_STEPS
        observed: list[str] = []

        async def _record_followup_send(**kwargs):
            observed.append(kwargs["content"])
            return MagicMock()

        # Start with the first step's view.
        view = sw._TourStepView(
            steps=steps, index=0, owner_id=42, is_last=False,
        )
        for _ in range(len(steps) - 1):
            inter = _make_interaction(user_id=42,
                                      message=MagicMock(content="prior"))
            inter.followup.send = AsyncMock(side_effect=_record_followup_send)
            await view.children[0].callback(inter)
            # The Next callback called `_send_tour_step`, which created
            # the next view inside `inter.followup.send`. We can't grab
            # that view directly, so re-instantiate the equivalent view
            # for the next iteration.
            next_index = view._index + 1
            is_last = (next_index + 1) >= len(steps)
            view = sw._TourStepView(
                steps=steps, index=next_index, owner_id=42, is_last=is_last,
            )

        # The observed content list should be steps[1] … steps[N-1].
        assert observed == steps[1:]

    @pytest.mark.asyncio
    async def test_last_step_renders_close_button_only(self):
        steps = ["a", "b"]
        view = sw._TourStepView(steps=steps, index=1, owner_id=42, is_last=True)
        # Single child: the Close button.
        assert len(view.children) == 1
        assert view.children[0].label == "Close"

    @pytest.mark.asyncio
    async def test_skip_handles_missing_content_gracefully(self):
        """`inter.message.content` can be falsy; the Skip handler must
        not raise TypeError concatenating to None."""
        steps = ["one", "two"]
        view = sw._TourStepView(steps=steps, index=0, owner_id=42, is_last=False)
        # Children: [Next, Skip]
        skip_btn = view.children[1]
        inter = _make_interaction(user_id=42,
                                  message=MagicMock(content=None))
        # Must not raise.
        await skip_btn.callback(inter)
        # The view stopped + disabled.
        assert view.is_finished()
        inter.response.edit_message.assert_awaited_once()
        edited = inter.response.edit_message.await_args.kwargs
        assert "tour skipped" in edited["content"]

    @pytest.mark.asyncio
    async def test_non_owner_click_rejected(self):
        steps = ["one", "two"]
        view = sw._TourStepView(steps=steps, index=0, owner_id=42, is_last=False)
        next_btn = view.children[0]
        inter = _make_interaction(user_id=99,  # not the owner
                                  message=MagicMock(content="one"))
        await next_btn.callback(inter)
        # No followup sent — the view rejected the click.
        inter.followup.send.assert_not_called()
        inter.response.send_message.assert_awaited_once()
        # View not stopped.
        assert not view.is_finished()

    @pytest.mark.asyncio
    async def test_double_click_next_is_idempotent(self):
        """A fast second click on Next while the first is still
        in-flight must not spawn two step-2 messages."""
        steps = ["one", "two", "three"]
        view = sw._TourStepView(steps=steps, index=0, owner_id=42, is_last=False)
        next_btn = view.children[0]
        inter_a = _make_interaction(user_id=42, message=MagicMock(content="one"))
        await next_btn.callback(inter_a)
        inter_b = _make_interaction(user_id=42, message=MagicMock(content="one"))
        await next_btn.callback(inter_b)
        # Only the first click made it to followup.send.
        inter_a.followup.send.assert_awaited_once()
        inter_b.followup.send.assert_not_called()


class TestOfferViewDoubleClick:
    """The Accept button writes the dismissal + spawns the tour. A
    fast second click must not spawn two tours."""

    @pytest.mark.asyncio
    async def test_double_accept_only_starts_one_tour(self, seeded_db):
        view = sw._OfferView(
            guild_id=TEST_GUILD_ID, user_id=42,
            walkthrough_key=sw.STORM_SIGNUPS_TOUR_KEY,
        )
        # discord.ui.button decorates the method into a Button on the View;
        # `button.callback` is a discord.py `_ItemCallback` that takes just
        # the interaction (self + button are pre-bound).
        accept_cb = view.accept.callback
        inter_a = _make_interaction(user_id=42)
        await accept_cb(inter_a)
        inter_b = _make_interaction(user_id=42)
        await accept_cb(inter_b)
        # First click sent the tour-step-1 followup; second click bailed
        # before sending.
        inter_a.followup.send.assert_awaited_once()
        inter_b.followup.send.assert_not_called()
