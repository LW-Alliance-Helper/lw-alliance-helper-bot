"""
Tests for storm_walkthrough.py + the walkthrough_dismissals layer (#130).

Post-#190 the tour fires from the event hub (`/desertstorm`,
`/canyonstorm`) rather than the legacy `/<event> signups` officer
view. The 5-step content walks the weekly cycle, strategy presets +
member rules, free vs Premium gating, and a /help pointer.
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

        assert (
            config.is_walkthrough_dismissed(
                TEST_GUILD_ID,
                42,
                sw.STORM_HUB_TOUR_KEY,
            )
            is False
        )

    def test_dismiss_then_check(self, seeded_db):
        import config

        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_HUB_TOUR_KEY)
        assert (
            config.is_walkthrough_dismissed(
                TEST_GUILD_ID,
                42,
                sw.STORM_HUB_TOUR_KEY,
            )
            is True
        )

    def test_dismiss_idempotent(self, seeded_db):
        import config

        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_HUB_TOUR_KEY)
        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_HUB_TOUR_KEY)
        # Still just one record; still dismissed.
        assert (
            config.is_walkthrough_dismissed(
                TEST_GUILD_ID,
                42,
                sw.STORM_HUB_TOUR_KEY,
            )
            is True
        )

    def test_per_user_isolation(self, seeded_db):
        import config

        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_HUB_TOUR_KEY)
        # A different user in the same guild still gets offered the tour.
        assert (
            config.is_walkthrough_dismissed(
                TEST_GUILD_ID,
                99,
                sw.STORM_HUB_TOUR_KEY,
            )
            is False
        )

    def test_per_guild_isolation(self, seeded_db):
        import config

        config.dismiss_walkthrough(TEST_GUILD_ID, 42, sw.STORM_HUB_TOUR_KEY)
        # The same user in a different guild still gets offered the tour.
        assert (
            config.is_walkthrough_dismissed(
                TEST_GUILD_ID + 1,
                42,
                sw.STORM_HUB_TOUR_KEY,
            )
            is False
        )

    def test_hub_version_key_re_offers_after_v1_signups_dismissal(self, seeded_db):
        """The pre-#190 tour used `storm_signups_v1`; the hub tour
        uses `storm_hub_v1`. An officer who dismissed the old tour
        should still see the new hub-flow offer once."""
        import config

        config.dismiss_walkthrough(TEST_GUILD_ID, 42, "storm_signups_v1")
        assert (
            config.is_walkthrough_dismissed(
                TEST_GUILD_ID,
                42,
                sw.STORM_HUB_TOUR_KEY,
            )
            is False
        )


class TestTourContent:
    def test_tour_has_five_steps(self):
        # Hub tour is intentionally 5 steps: the hub itself, the weekly
        # cycle, strategy presets + member rules, free vs Premium, and
        # a wrap-up pointer at /help.
        assert len(sw._STORM_HUB_TOUR_STEPS) == 5

    def test_first_step_starts_with_step_label(self):
        # Each step in the tour body starts with a bolded "Step N / M"
        # so the user can tell where they are.
        assert sw._STORM_HUB_TOUR_STEPS[0].startswith("**Step 1 ")

    def test_steps_are_individually_short(self):
        # Discord ephemeral messages render best at a few sentences each.
        # 1200 chars accommodates the bullet-list style without forcing
        # a sub-step split.
        for step in sw._STORM_HUB_TOUR_STEPS:
            assert len(step) < 1200, f"Step too long ({len(step)} chars):\n{step}"

    def test_step_1_describes_hub_overview(self):
        step_1 = sw._STORM_HUB_TOUR_STEPS[0]
        # Step 1 introduces the embed body + button rows.
        assert "hub" in step_1.lower() or "home base" in step_1.lower()
        assert "buttons" in step_1.lower() or "button" in step_1.lower()

    def test_step_2_describes_weekly_cycle(self):
        step_2 = sw._STORM_HUB_TOUR_STEPS[1]
        # Step 2 walks the cycle: post poll -> view signups -> attendance.
        assert "Post sign-up poll" in step_2
        assert "set up teams" in step_2.lower()
        assert "attendance" in step_2.lower()

    def test_step_3_describes_presets_and_rules(self):
        step_3 = sw._STORM_HUB_TOUR_STEPS[2]
        assert "strategy presets" in step_3.lower()
        assert "member rules" in step_3.lower()

    def test_step_4_describes_premium_gating(self):
        step_4 = sw._STORM_HUB_TOUR_STEPS[3]
        assert "Premium" in step_4 or "premium" in step_4
        # Mentions specific Premium buttons and free-tier buttons so
        # officers see what falls on each side.
        assert "Post sign-up poll" in step_4
        assert "Manage strategy presets" in step_4 or "presets" in step_4.lower()

    def test_step_5_points_at_help(self):
        step_5 = sw._STORM_HUB_TOUR_STEPS[4]
        assert "/help" in step_5


class TestTourBuilderBranching:
    """Tour copy branches on event_type so DS officers see DS-flavoured
    copy + event day, and CS officers see CS."""

    def test_ds_event_label(self):
        ds_step1 = sw._build_storm_hub_tour_steps("DS")[0]
        assert "Desert Storm" in ds_step1
        assert "Canyon Storm" not in ds_step1

    def test_cs_event_label(self):
        cs_step1 = sw._build_storm_hub_tour_steps("CS")[0]
        assert "Canyon Storm" in cs_step1
        assert "Desert Storm" not in cs_step1

    def test_ds_event_day_is_friday(self):
        ds_step2 = sw._build_storm_hub_tour_steps("DS")[1]
        assert "Friday" in ds_step2

    def test_cs_event_day_is_thursday(self):
        cs_step2 = sw._build_storm_hub_tour_steps("CS")[1]
        assert "Thursday" in cs_step2

    def test_step_5_parent_command_matches_event_type(self):
        ds_step5 = sw._build_storm_hub_tour_steps("DS")[4]
        cs_step5 = sw._build_storm_hub_tour_steps("CS")[4]
        assert "/desertstorm" in ds_step5
        assert "/canyonstorm" in cs_step5

    def test_step_5_help_category_matches_event_type(self):
        """The /help dropdown has separate Desert Storm + Canyon Storm
        categories. A CS officer shouldn't be told to pick Desert Storm."""
        ds_step5 = sw._build_storm_hub_tour_steps("DS")[4]
        cs_step5 = sw._build_storm_hub_tour_steps("CS")[4]
        assert "Desert Storm" in ds_step5
        assert "Canyon Storm" not in ds_step5
        assert "Canyon Storm" in cs_step5
        assert "Desert Storm" not in cs_step5

    def test_offer_view_carries_event_type(self):
        view = sw._OfferView(
            guild_id=TEST_GUILD_ID,
            user_id=42,
            walkthrough_key=sw.STORM_HUB_TOUR_KEY,
            event_type="CS",
        )
        assert view.event_type == "CS"


class TestTourStepProgression:
    """The tour is five steps; each Next click must advance the index.

    Before the earlier fix, the Next callback bumped a state-dict index
    but then re-entered a start function that built fresh state at 0,
    so the tour rendered "Step 1 / N" forever. This class drives the
    View callbacks directly and asserts the index advances.
    """

    @pytest.mark.asyncio
    async def test_next_button_advances_to_step_2(self):
        steps = ["one", "two", "three"]
        view = sw._TourStepView(steps=steps, index=0, owner_id=42, is_last=False)
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
        steps = sw._STORM_HUB_TOUR_STEPS
        observed: list[str] = []

        async def _record_followup_send(**kwargs):
            observed.append(kwargs["content"])
            return MagicMock()

        # Start with the first step's view.
        view = sw._TourStepView(
            steps=steps,
            index=0,
            owner_id=42,
            is_last=False,
        )
        for _ in range(len(steps) - 1):
            inter = _make_interaction(user_id=42, message=MagicMock(content="prior"))
            inter.followup.send = AsyncMock(side_effect=_record_followup_send)
            await view.children[0].callback(inter)
            # The Next callback called `_send_tour_step`, which created
            # the next view inside `inter.followup.send`. We can't grab
            # that view directly, so re-instantiate the equivalent view
            # for the next iteration.
            next_index = view._index + 1
            is_last = (next_index + 1) >= len(steps)
            view = sw._TourStepView(
                steps=steps,
                index=next_index,
                owner_id=42,
                is_last=is_last,
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
        inter = _make_interaction(user_id=42, message=MagicMock(content=None))
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
        inter = _make_interaction(
            user_id=99,  # not the owner
            message=MagicMock(content="one"),
        )
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
            guild_id=TEST_GUILD_ID,
            user_id=42,
            walkthrough_key=sw.STORM_HUB_TOUR_KEY,
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
