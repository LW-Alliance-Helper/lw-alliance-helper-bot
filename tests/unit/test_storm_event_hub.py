"""
Tests for storm_event_hub.py (#187 + #190 + #201 + the pre-dev-test
audit follow-up #209).

The hub is the front door for every storm flow after the slash-tree
restructure. Officers run `/desertstorm` or `/canyonstorm`, get an
embed showing the alliance's saved config, and click one of 11
buttons that dispatch into the existing storm modules.

This file covers the hub's local behaviour:
  * embed builder cold-path + populated-path
  * premium gating (button labels + disabled state) on both tiers
  * button dispatch (each callback fires the right downstream handler)
  * `interaction_check` owner-only gate
  * `_on_setup` wires the ⚙️ button straight into `_launch_storm_setup`
  * `handle_event_hub` leadership gate, ephemeral send, and the
    walkthrough-tour-offer pass-through

Downstream module behaviour is covered by each module's own tests; the
hub's only job is to dispatch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import storm_event_hub as seh

from tests.unit.test_config import TEST_GUILD_ID


# ── Factories ────────────────────────────────────────────────────────────────


def _make_guild(guild_id: int = TEST_GUILD_ID,
                name: str = "Test Alliance") -> MagicMock:
    g = MagicMock(spec=discord.Guild)
    g.id = guild_id
    g.name = name
    return g


def _make_interaction(
    *, user_id: int = 42,
    guild_id: int = TEST_GUILD_ID,
    in_guild: bool = True,
    response_done: bool = False,
) -> MagicMock:
    """Hub-sized stand-in for `discord.Interaction`. Covers the surface
    `handle_event_hub` and `_EventHubView` touch."""
    inter = MagicMock()
    inter.user = MagicMock()
    inter.user.id = user_id
    inter.guild_id = guild_id if in_guild else None
    inter.guild = _make_guild(guild_id) if in_guild else None
    inter.response = MagicMock()
    inter.response.send_message = AsyncMock()
    inter.response.is_done = MagicMock(return_value=response_done)
    inter.followup = MagicMock()
    inter.followup.send = AsyncMock(return_value=MagicMock())
    inter.original_response = AsyncMock(return_value=MagicMock())
    return inter


# ── Embed builder ────────────────────────────────────────────────────────────


class TestBuildEventHubEmbedColdPath:
    """When no storm config has been written, the embed renders sane
    fallbacks instead of crashing on `KeyError` / `TypeError`. Most
    first-time officers see this state."""

    def test_ds_cold_path_renders_without_crash(self, seeded_db):
        embed = seh._build_event_hub_embed(
            _make_guild(), "DS", is_premium=False,
        )
        assert "Desert Storm" in embed.title
        assert "Test Alliance" in embed.title
        # Gold for DS.
        assert embed.color == discord.Color.gold()

    def test_cs_cold_path_uses_orange(self, seeded_db):
        embed = seh._build_event_hub_embed(
            _make_guild(), "CS", is_premium=False,
        )
        assert "Canyon Storm" in embed.title
        assert embed.color == discord.Color.orange()

    def test_cold_path_signup_channel_says_not_configured(self, seeded_db):
        embed = seh._build_event_hub_embed(
            _make_guild(), "DS", is_premium=False,
        )
        assert "_not configured_" in embed.description

    def test_cold_path_structured_flow_says_not_enabled(self, seeded_db):
        embed = seh._build_event_hub_embed(
            _make_guild(), "DS", is_premium=False,
        )
        assert "Not enabled (free-tier flow only)" in embed.description

    def test_cold_path_preset_count_zero(self, seeded_db):
        embed = seh._build_event_hub_embed(
            _make_guild(), "DS", is_premium=False,
        )
        # `📋 **Presets saved:** 0` appears verbatim.
        assert "Presets saved:** 0" in embed.description

    def test_cold_path_default_teams_a_and_b(self, seeded_db):
        embed = seh._build_event_hub_embed(
            _make_guild(), "DS", is_premium=False,
        )
        assert "A & B" in embed.description

    def test_cold_path_falls_back_to_fixed_event_day(self, seeded_db):
        """When `next_event_date` can't resolve (no saved date and the
        helper raises), the description falls back to the
        game-defined event day."""
        embed = seh._build_event_hub_embed(
            _make_guild(), "DS", is_premium=False,
        )
        # DS is Friday game-side.
        assert "Friday" in embed.description

    def test_cold_path_cs_falls_back_to_thursday(self, seeded_db):
        embed = seh._build_event_hub_embed(
            _make_guild(), "CS", is_premium=False,
        )
        assert "Thursday" in embed.description

    def test_footer_points_back_at_hub_command(self, seeded_db):
        ds = seh._build_event_hub_embed(
            _make_guild(), "DS", is_premium=False,
        )
        cs = seh._build_event_hub_embed(
            _make_guild(), "CS", is_premium=False,
        )
        assert "/desertstorm" in (ds.footer.text or "")
        assert "/canyonstorm" in (cs.footer.text or "")


class TestBuildEventHubEmbedPremiumTierCopy:
    """The premium flag controls the trailing upsell line — present on
    free tier, suppressed on premium."""

    def test_free_tier_adds_upgrade_hint(self, seeded_db):
        embed = seh._build_event_hub_embed(
            _make_guild(), "DS", is_premium=False,
        )
        assert "/upgrade" in embed.description
        assert "Premium-only buttons below are disabled" in embed.description

    def test_premium_tier_omits_upgrade_hint(self, seeded_db):
        embed = seh._build_event_hub_embed(
            _make_guild(), "DS", is_premium=True,
        )
        assert "/upgrade" not in embed.description


class TestBuildEventHubEmbedPopulated:
    """Once the alliance has configured the storm surfaces, the embed
    surfaces that state instead of the cold-path placeholders."""

    def test_signup_channel_renders_as_mention_when_configured(
        self, seeded_db,
    ):
        with patch(
            "config.get_structured_storm_config",
            return_value={
                "signup_channel_id": 555000000000000001,
                "structured_flow_enabled": False,
            },
        ):
            embed = seh._build_event_hub_embed(
                _make_guild(), "DS", is_premium=True,
            )
        assert "<#555000000000000001>" in embed.description
        assert "_not configured_" not in embed.description

    def test_poll_auto_schedule_line_renders_when_set(self, seeded_db):
        with patch(
            "config.get_structured_storm_config",
            return_value={
                "signup_channel_id": 555000000000000001,
                "poll_day_of_week": 4,   # Friday
                "signup_time": "20:00",
                "structured_flow_enabled": False,
            },
        ):
            embed = seh._build_event_hub_embed(
                _make_guild(), "DS", is_premium=True,
            )
        # `auto-posted Friday at 20:00 server time` rides below the
        # channel mention.
        assert "auto-posted Friday at 20:00 server time" in embed.description

    def test_poll_auto_schedule_line_renders_for_monday(self, seeded_db):
        """Regression for the `or -1` truthiness trap: Monday is
        `poll_day_of_week == 0`, which is falsy. An earlier shape
        used `int(structured.get("poll_day_of_week", -1) or -1)`,
        collapsing Monday to -1 and silently dropping the schedule
        line for Monday-poll alliances. The fix uses an explicit
        `is None` check; this test pins it."""
        with patch(
            "config.get_structured_storm_config",
            return_value={
                "signup_channel_id": 555000000000000001,
                "poll_day_of_week": 0,   # Monday — falsy on purpose
                "signup_time": "12:00",
                "structured_flow_enabled": False,
            },
        ):
            embed = seh._build_event_hub_embed(
                _make_guild(), "DS", is_premium=True,
            )
        assert "auto-posted Monday at 12:00 server time" in embed.description

    def test_poll_auto_schedule_line_omitted_when_day_missing(self, seeded_db):
        """Counterpart to the Monday regression: a totally missing
        `poll_day_of_week` (no auto-schedule configured) must NOT
        render the schedule line, even though the fix removed `or -1`."""
        with patch(
            "config.get_structured_storm_config",
            return_value={
                "signup_channel_id": 555000000000000001,
                # poll_day_of_week omitted entirely.
                "signup_time": "20:00",
                "structured_flow_enabled": False,
            },
        ):
            embed = seh._build_event_hub_embed(
                _make_guild(), "DS", is_premium=True,
            )
        assert "auto-posted" not in embed.description

    def test_structured_flow_enabled_includes_power_column(self, seeded_db):
        with patch(
            "config.get_structured_storm_config",
            return_value={
                "structured_flow_enabled": True,
                "power_metric_column": "g",  # lower-case in DB
            },
        ):
            embed = seh._build_event_hub_embed(
                _make_guild(), "DS", is_premium=True,
            )
        # Power column normalised to upper-case display.
        assert "power column **G**" in embed.description

    def test_structured_flow_enabled_without_power_column_shows_enabled(
        self, seeded_db,
    ):
        with patch(
            "config.get_structured_storm_config",
            return_value={
                "structured_flow_enabled": True,
                "power_metric_column": "",
            },
        ):
            embed = seh._build_event_hub_embed(
                _make_guild(), "DS", is_premium=True,
            )
        assert "Enabled" in embed.description
        assert "power column" not in embed.description

    def test_teams_a_only_displays_team_a_only(self, seeded_db):
        with patch(
            "config.get_storm_config",
            return_value={"teams": "A"},
        ):
            embed = seh._build_event_hub_embed(
                _make_guild(), "DS", is_premium=True,
            )
        assert "Team A only" in embed.description

    def test_preset_count_reflects_saved_presets(self, seeded_db):
        with patch(
            "storm_strategy.list_presets",
            return_value=["Standard", "Backup", "Big Hits"],
        ):
            embed = seh._build_event_hub_embed(
                _make_guild(), "DS", is_premium=True,
            )
        assert "Presets saved:** 3" in embed.description


# ── View: premium gating ─────────────────────────────────────────────────────


_PREMIUM_GATED_LABELS = {
    seh.HUB_BTN_POST_SIGNUP,
    seh.HUB_BTN_VIEW_SIGNUPS,
    seh.HUB_BTN_ATTENDANCE,
    seh.HUB_BTN_REMIND,
    seh.HUB_BTN_TRENDS,
    seh.HUB_BTN_PAST_ROSTERS,
}

_FREE_TIER_LABELS = {
    seh.HUB_BTN_PARTICIPATION,
    seh.HUB_BTN_PRESETS,
    seh.HUB_BTN_RULES,
    seh.HUB_BTN_DRAFT,
    seh.HUB_BTN_LOGS,
    seh.HUB_BTN_SETUP,
}


def _make_view(
    *, event_type: str = "DS",
    is_premium: bool = True,
    owner_user_id: int = 42,
) -> seh._EventHubView:
    bot = MagicMock()
    return seh._EventHubView(
        bot=bot,
        guild_id=TEST_GUILD_ID,
        event_type=event_type,
        owner_user_id=owner_user_id,
        is_premium=is_premium,
    )


class TestEventHubViewPremiumGating:
    def test_premium_tier_renders_twelve_active_buttons(self):
        view = _make_view(is_premium=True)
        labels = {c.label for c in view.children}
        # Every active label appears, none of the locked variants.
        for label in _PREMIUM_GATED_LABELS | _FREE_TIER_LABELS:
            assert label in labels, f"Missing button: {label}"
        # No 💎-prefix locked labels.
        assert not any(c.label.startswith("💎 ") for c in view.children)
        assert all(c.disabled is False for c in view.children)

    @pytest.mark.free_tier_only
    def test_free_tier_premium_buttons_render_locked_and_disabled(self):
        view = _make_view(is_premium=False)
        for child in view.children:
            label = child.label
            unlocked_form = label.split(" ", 1)[-1] if label.startswith("💎 ") else label
            # Locked variants only apply to the premium-gated buttons.
            is_premium_label = any(
                unlocked_form in active for active in _PREMIUM_GATED_LABELS
            )
            if label.startswith("💎 "):
                assert child.disabled is True, (
                    f"Locked-label button should be disabled: {label}"
                )
                assert is_premium_label, (
                    f"Free-tier button rendered with locked-label prefix: {label}"
                )
            else:
                assert child.disabled is False, (
                    f"Free-tier button should be enabled: {label}"
                )

    @pytest.mark.free_tier_only
    def test_free_tier_locked_labels_strip_original_emoji(self):
        """The locked-label helper replaces the leading emoji with 💎,
        rather than concatenating both."""
        view = _make_view(is_premium=False)
        locked = [c.label for c in view.children if c.label.startswith("💎 ")]
        # Six premium-gated buttons in the hub (#246 added the Trends
        # Viewer to the existing five).
        assert len(locked) == 6
        # 📣/👁️/📋/🔔/🔍/📜 are the original emoji that should NOT
        # survive the locked rewrite (they're the leading character
        # on the premium-gated active labels).
        for label in locked:
            assert "📣" not in label  # Post sign-up
            assert "🔔" not in label  # DM reminder
            assert "🔍" not in label  # Trends viewer (#246)

    def test_button_layout_three_rows(self):
        view = _make_view(is_premium=True)
        rows = {c.row for c in view.children}
        assert rows == {0, 1, 2}
        # Row 0 has the four event-day actions.
        assert sum(1 for c in view.children if c.row == 0) == 4
        # Row 1 has the four comms + config buttons.
        assert sum(1 for c in view.children if c.row == 1) == 4
        # Row 2 has four reference + setup buttons (#246 added the
        # Trends Viewer alongside Logs / Past Rosters / Setup).
        assert sum(1 for c in view.children if c.row == 2) == 4

    def test_primary_action_buttons_use_coloured_styles(self):
        view = _make_view(is_premium=True)
        by_label = {c.label: c for c in view.children}
        assert by_label[seh.HUB_BTN_POST_SIGNUP].style == discord.ButtonStyle.primary
        assert by_label[seh.HUB_BTN_VIEW_SIGNUPS].style == discord.ButtonStyle.success
        # Everything else is secondary — no rainbow.
        for child in view.children:
            if child.label in (seh.HUB_BTN_POST_SIGNUP, seh.HUB_BTN_VIEW_SIGNUPS):
                continue
            assert child.style == discord.ButtonStyle.secondary, (
                f"{child.label} should be secondary, got {child.style}"
            )


# ── View: interaction_check (owner-only gate) ────────────────────────────────


class TestEventHubViewInteractionCheck:
    @pytest.mark.asyncio
    async def test_owner_can_click(self):
        view = _make_view(owner_user_id=42)
        inter = _make_interaction(user_id=42)
        assert await view.interaction_check(inter) is True
        inter.response.send_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_owner_rejected_with_ephemeral_message(self):
        view = _make_view(owner_user_id=42)
        inter = _make_interaction(user_id=99)
        assert await view.interaction_check(inter) is False
        inter.response.send_message.assert_awaited_once()
        sent = inter.response.send_message.await_args
        msg = sent.args[0] if sent.args else sent.kwargs.get("content", "")
        assert "Only the user who opened this view" in msg
        assert sent.kwargs.get("ephemeral") is True


# ── View: button dispatch ────────────────────────────────────────────────────


def _click(view: seh._EventHubView, label: str, inter: MagicMock):
    """Locate the button by label and invoke its callback."""
    for child in view.children:
        if child.label == label:
            return child.callback(inter)
    raise AssertionError(f"No button with label {label!r} on the view")


class TestEventHubViewDispatch:
    """Each button is a thin dispatcher into an existing module. The
    hub's responsibility is only to wire the click to the right handler
    with the right event_type — downstream behaviour is covered by each
    module's own test file."""

    @pytest.mark.asyncio
    async def test_post_signup_dispatches_to_storm_signup_post(self):
        view = _make_view(event_type="DS", is_premium=True)
        inter = _make_interaction()
        with patch(
            "storm_signup_post.handle_post_signup",
            new=AsyncMock(),
        ) as handler:
            await _click(view, seh.HUB_BTN_POST_SIGNUP, inter)
        handler.assert_awaited_once_with(view.bot, inter, "DS", None)

    @pytest.mark.asyncio
    async def test_view_signups_dispatches_to_storm_officer_view(self):
        view = _make_view(event_type="CS", is_premium=True)
        inter = _make_interaction()
        with patch(
            "storm_officer_view.handle_storm_signups",
            new=AsyncMock(),
        ) as handler:
            await _click(view, seh.HUB_BTN_VIEW_SIGNUPS, inter)
        handler.assert_awaited_once_with(view.bot, inter, "CS", None)

    @pytest.mark.asyncio
    async def test_attendance_dispatches_to_storm_attendance(self):
        view = _make_view(event_type="DS", is_premium=True)
        inter = _make_interaction()
        with patch(
            "storm_attendance.handle_storm_attendance",
            new=AsyncMock(),
        ) as handler:
            await _click(view, seh.HUB_BTN_ATTENDANCE, inter)
        handler.assert_awaited_once_with(view.bot, inter, "DS", None)

    @pytest.mark.asyncio
    async def test_participation_dispatches_to_storm_log(self):
        view = _make_view(event_type="DS", is_premium=True)
        inter = _make_interaction()
        with patch(
            "storm_log.handle_storm_participation",
            new=AsyncMock(),
        ) as handler:
            await _click(view, seh.HUB_BTN_PARTICIPATION, inter)
        handler.assert_awaited_once_with(view.bot, inter, "DS")

    @pytest.mark.asyncio
    async def test_remind_dispatches_to_storm_log(self):
        view = _make_view(event_type="CS", is_premium=True)
        inter = _make_interaction()
        with patch(
            "storm_log.handle_storm_remind",
            new=AsyncMock(),
        ) as handler:
            await _click(view, seh.HUB_BTN_REMIND, inter)
        handler.assert_awaited_once_with(view.bot, inter, "CS")

    @pytest.mark.asyncio
    async def test_manage_presets_dispatches_to_storm_strategy(self):
        view = _make_view(event_type="DS", is_premium=True)
        inter = _make_interaction()
        with patch(
            "storm_strategy.open_strategy_list",
            new=AsyncMock(),
        ) as handler:
            await _click(view, seh.HUB_BTN_PRESETS, inter)
        handler.assert_awaited_once_with(inter, "DS")

    @pytest.mark.asyncio
    async def test_manage_rules_dispatches_to_storm_member_rules(self):
        view = _make_view(event_type="CS", is_premium=True)
        inter = _make_interaction()
        with patch(
            "storm_member_rules.open_member_rule_list",
            new=AsyncMock(),
        ) as handler:
            await _click(view, seh.HUB_BTN_RULES, inter)
        handler.assert_awaited_once_with(inter, "CS", member_filter=None)

    @pytest.mark.asyncio
    async def test_draft_dispatches_to_storm(self):
        view = _make_view(event_type="DS", is_premium=True)
        inter = _make_interaction()
        with patch(
            "storm.handle_storm_draft",
            new=AsyncMock(),
        ) as handler:
            await _click(view, seh.HUB_BTN_DRAFT, inter)
        handler.assert_awaited_once_with(view.bot, inter, "DS")

    @pytest.mark.asyncio
    async def test_logs_dispatches_to_storm_log(self):
        view = _make_view(event_type="CS", is_premium=True)
        inter = _make_interaction()
        with patch(
            "storm_log.handle_storm_log",
            new=AsyncMock(),
        ) as handler:
            await _click(view, seh.HUB_BTN_LOGS, inter)
        handler.assert_awaited_once_with(view.bot, inter, "CS", None)

    @pytest.mark.asyncio
    async def test_past_rosters_dispatches_to_storm_history(self):
        view = _make_view(event_type="DS", is_premium=True)
        inter = _make_interaction()
        with patch(
            "storm_history.open_history",
            new=AsyncMock(),
        ) as handler:
            await _click(view, seh.HUB_BTN_PAST_ROSTERS, inter)
        handler.assert_awaited_once_with(inter, "DS", None)


# ── View: setup button wiring (post-#209 hub-polish fix) ─────────────────────


class TestSetupButtonWiring:
    """The ⚙️ Open setup button used to render a pointer telling the
    officer to type `/setup` themselves. Post-#209 it dispatches into
    the `/setup` hub's storm-launcher helper directly. Verifies the
    fix landed and the event_type is forwarded correctly."""

    @pytest.mark.asyncio
    async def test_ds_setup_button_launches_ds_setup_wizard(self):
        view = _make_view(event_type="DS", is_premium=True)
        inter = _make_interaction()
        with patch(
            "setup_cog._launch_storm_setup", new=AsyncMock(),
        ) as launcher, patch(
            "wizard_registry.safe_edit_response", new=AsyncMock(),
        ):
            await _click(view, seh.HUB_BTN_SETUP, inter)
        launcher.assert_awaited_once_with(inter, view.bot, "DS")

    @pytest.mark.asyncio
    async def test_cs_setup_button_launches_cs_setup_wizard(self):
        view = _make_view(event_type="CS", is_premium=True)
        inter = _make_interaction()
        with patch(
            "setup_cog._launch_storm_setup", new=AsyncMock(),
        ) as launcher, patch(
            "wizard_registry.safe_edit_response", new=AsyncMock(),
        ):
            await _click(view, seh.HUB_BTN_SETUP, inter)
        launcher.assert_awaited_once_with(inter, view.bot, "CS")

    @pytest.mark.asyncio
    async def test_setup_button_disables_view_before_launch(self):
        """All buttons disable + view stops before `_launch_storm_setup`
        is awaited, so a fast second click can't double-fire the wizard.
        Same pattern `/growth`'s Edit Config button uses."""
        view = _make_view(event_type="DS", is_premium=True)
        inter = _make_interaction()
        launch = AsyncMock()
        with patch(
            "setup_cog._launch_storm_setup", new=launch,
        ), patch(
            "wizard_registry.safe_edit_response", new=AsyncMock(),
        ):
            await _click(view, seh.HUB_BTN_SETUP, inter)
        assert all(c.disabled for c in view.children)
        assert view.is_finished()

    @pytest.mark.asyncio
    async def test_setup_button_survives_safe_edit_response_failure(self):
        """If the view edit raises (interaction token expired, etc.),
        the launcher still fires — the user shouldn't get stuck without
        a wizard just because the cosmetic disable failed."""
        view = _make_view(event_type="DS", is_premium=True)
        inter = _make_interaction()
        launch = AsyncMock()
        with patch(
            "setup_cog._launch_storm_setup", new=launch,
        ), patch(
            "wizard_registry.safe_edit_response",
            new=AsyncMock(side_effect=RuntimeError("expired token")),
        ):
            await _click(view, seh.HUB_BTN_SETUP, inter)
        launch.assert_awaited_once_with(inter, view.bot, "DS")


# ── Top-level handler ────────────────────────────────────────────────────────


class TestHandleEventHubGates:
    @pytest.mark.asyncio
    async def test_non_leadership_caller_rejected(self, seeded_db):
        bot = MagicMock()
        inter = _make_interaction()
        with patch(
            "storm_permissions.is_leader_or_admin", return_value=False,
        ), patch(
            "storm_permissions.deny_non_leader", new=AsyncMock(),
        ) as denier:
            await seh.handle_event_hub(bot, inter, "DS")
        denier.assert_awaited_once_with(inter)
        # No hub embed sent.
        inter.response.send_message.assert_not_called()
        inter.followup.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_dm_invocation_rejected(self, seeded_db):
        """Hub is guild-only — running it in a DM (or any non-guild
        interaction) is rejected with the standard server-only copy."""
        bot = MagicMock()
        inter = _make_interaction(in_guild=False)
        with patch(
            "storm_permissions.is_leader_or_admin", return_value=True,
        ), patch(
            "storm_permissions.deny_non_leader", new=AsyncMock(),
        ):
            await seh.handle_event_hub(bot, inter, "DS")
        inter.response.send_message.assert_awaited_once()
        sent = inter.response.send_message.await_args
        msg = sent.args[0] if sent.args else sent.kwargs.get("content", "")
        assert "server" in msg.lower()


class TestHandleEventHubRender:
    @pytest.mark.asyncio
    async def test_render_sends_ephemeral_embed_with_view(self, seeded_db):
        bot = MagicMock()
        inter = _make_interaction()
        with patch(
            "storm_permissions.is_leader_or_admin", return_value=True,
        ), patch(
            "premium.is_premium", new=AsyncMock(return_value=True),
        ), patch(
            "storm_walkthrough.maybe_offer_storm_hub_tour", new=AsyncMock(),
        ):
            await seh.handle_event_hub(bot, inter, "DS")
        inter.response.send_message.assert_awaited_once()
        kwargs = inter.response.send_message.await_args.kwargs
        assert kwargs.get("ephemeral") is True
        assert isinstance(kwargs.get("embed"), discord.Embed)
        view = kwargs.get("view")
        assert isinstance(view, seh._EventHubView)
        assert view.owner_user_id == inter.user.id
        assert view.is_premium is True
        # The hub captures the sent message for `on_timeout` cleanup.
        assert view.message is inter.original_response.return_value

    @pytest.mark.asyncio
    async def test_render_uses_followup_when_response_already_done(
        self, seeded_db,
    ):
        bot = MagicMock()
        inter = _make_interaction(response_done=True)
        with patch(
            "storm_permissions.is_leader_or_admin", return_value=True,
        ), patch(
            "premium.is_premium", new=AsyncMock(return_value=True),
        ), patch(
            "storm_walkthrough.maybe_offer_storm_hub_tour", new=AsyncMock(),
        ):
            await seh.handle_event_hub(bot, inter, "DS")
        inter.response.send_message.assert_not_called()
        inter.followup.send.assert_awaited_once()
        followup_kwargs = inter.followup.send.await_args.kwargs
        assert followup_kwargs.get("ephemeral") is True
        # The hub still wires the view's message to the followup return.
        view = followup_kwargs.get("view")
        assert view.message is inter.followup.send.return_value

    @pytest.mark.asyncio
    async def test_premium_check_failure_falls_through_to_free_tier(
        self, seeded_db,
    ):
        """A flaky premium check shouldn't 500 the hub; it falls
        through to `is_premium=False` so the user still sees the
        embed (with the locked buttons + upgrade hint)."""
        bot = MagicMock()
        inter = _make_interaction()
        with patch(
            "storm_permissions.is_leader_or_admin", return_value=True,
        ), patch(
            "premium.is_premium",
            new=AsyncMock(side_effect=RuntimeError("entitlement boom")),
        ), patch(
            "storm_walkthrough.maybe_offer_storm_hub_tour", new=AsyncMock(),
        ):
            await seh.handle_event_hub(bot, inter, "DS")
        view = inter.response.send_message.await_args.kwargs["view"]
        assert view.is_premium is False

    @pytest.mark.asyncio
    async def test_tour_offer_fires_after_hub_lands(self, seeded_db):
        bot = MagicMock()
        inter = _make_interaction()
        with patch(
            "storm_permissions.is_leader_or_admin", return_value=True,
        ), patch(
            "premium.is_premium", new=AsyncMock(return_value=True),
        ), patch(
            "storm_walkthrough.maybe_offer_storm_hub_tour", new=AsyncMock(),
        ) as offer:
            await seh.handle_event_hub(bot, inter, "DS")
        offer.assert_awaited_once()
        kwargs = offer.await_args.kwargs
        assert kwargs.get("event_type") == "DS"

    @pytest.mark.asyncio
    async def test_tour_offer_failure_does_not_break_hub(self, seeded_db):
        """Tour offer is non-essential; a failure inside the walkthrough
        layer (DB error, ephemeral send fail) must not raise out of
        `handle_event_hub`."""
        bot = MagicMock()
        inter = _make_interaction()
        with patch(
            "storm_permissions.is_leader_or_admin", return_value=True,
        ), patch(
            "premium.is_premium", new=AsyncMock(return_value=True),
        ), patch(
            "storm_walkthrough.maybe_offer_storm_hub_tour",
            new=AsyncMock(side_effect=RuntimeError("walkthrough boom")),
        ):
            # Must not raise.
            await seh.handle_event_hub(bot, inter, "DS")
        # Hub still rendered.
        inter.response.send_message.assert_awaited_once()


class TestTourOfferSuppression:
    """The walkthrough offer no-ops once the officer has dismissed it,
    which is what makes the tour 'fire once per officer' rather than
    every time they open the hub. Drives the real `maybe_offer_…`
    function against the seeded DB so the dismissal layer is also
    exercised."""

    @pytest.mark.asyncio
    async def test_offer_fires_when_not_dismissed(self, seeded_db):
        import storm_walkthrough as sw
        inter = _make_interaction()
        await sw.maybe_offer_storm_hub_tour(inter, event_type="DS")
        inter.followup.send.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_offer_suppressed_after_dismissal(self, seeded_db):
        import config
        import storm_walkthrough as sw
        config.dismiss_walkthrough(
            TEST_GUILD_ID, 42, sw.STORM_HUB_TOUR_KEY,
        )
        inter = _make_interaction(user_id=42)
        await sw.maybe_offer_storm_hub_tour(inter, event_type="DS")
        # No followup — the offer bailed before sending.
        inter.followup.send.assert_not_called()
