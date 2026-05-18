"""
Unit tests for premium.py — entitlement resolution, limits, caching, and
user-facing messaging.
"""

import importlib
import os
from unittest.mock import AsyncMock, MagicMock

import pytest

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID
from tests.constants import PREMIUM_TEST_GUILD_ID


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_premium(monkeypatch, temp_db):
    """
    Re-import the premium module per-test so module-level env reads
    (`PREMIUM_SKU_ID`) and the cache start clean. Composes `temp_db`
    so `is_premium`'s assignment lookup hits a real (empty) table.
    """
    # Clear env so module-level reads default to None
    for var in ("PREMIUM_SKU_ID", "FORCE_PREMIUM", "PREMIUM_BYPASS_GUILD_IDS"):
        monkeypatch.delenv(var, raising=False)
    import premium as _premium
    importlib.reload(_premium)
    _premium.clear_cache()
    yield _premium
    _premium.clear_cache()
    # Reload again with clean env so subsequent tests don't see stale module
    # state if the test set env vars and reloaded mid-test.
    importlib.reload(_premium)


@pytest.fixture
def fresh_premium_with_bypass(monkeypatch, temp_db):
    """
    Like `fresh_premium`, but with `PREMIUM_TEST_GUILD_ID` pre-loaded
    into the `PREMIUM_BYPASS_GUILD_IDS` env var so it resolves as
    premium without going through the entitlement / SKU pathway.
    Mirrors how the bot owner's home alliance stays premium in
    production (Railway sets the same env var with that guild's id).
    """
    for var in ("PREMIUM_SKU_ID", "FORCE_PREMIUM"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PREMIUM_BYPASS_GUILD_IDS", str(PREMIUM_TEST_GUILD_ID))
    import premium as _premium
    importlib.reload(_premium)
    _premium.clear_cache()
    yield _premium
    _premium.clear_cache()
    monkeypatch.delenv("PREMIUM_BYPASS_GUILD_IDS", raising=False)
    importlib.reload(_premium)


@pytest.fixture(autouse=True)
def _isolate_premium_env(monkeypatch, temp_db):
    """
    Autouse isolation: clear premium env vars at both SETUP and teardown so
    these tests are unaffected by the outer-process FORCE_PREMIUM=1 lane (CI
    runs the suite twice, once with FORCE_PREMIUM unset and once with it
    set). Each test in this file must drive premium state explicitly.

    Composes `temp_db` because `is_premium`'s assignment-layer lookup hits
    the `premium_assignments` table — without it, every cache-miss would
    raise a "no such table" OperationalError.
    """
    for var in ("PREMIUM_SKU_ID", "FORCE_PREMIUM", "PREMIUM_BYPASS_GUILD_IDS"):
        monkeypatch.delenv(var, raising=False)
    import premium as _premium
    importlib.reload(_premium)
    _premium.clear_cache()
    yield
    for var in ("PREMIUM_SKU_ID", "FORCE_PREMIUM", "PREMIUM_BYPASS_GUILD_IDS"):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(_premium)
    _premium.clear_cache()


def _make_entitlement(sku_id: int, deleted: bool = False):
    ent = MagicMock()
    ent.sku_id  = sku_id
    ent.deleted = deleted
    return ent


# ── Always-premium short-circuits ─────────────────────────────────────────────

class TestAlwaysPremiumShortCircuits:

    @pytest.mark.asyncio
    async def test_no_env_var_no_premium(self, fresh_premium):
        """With no env vars set, no guild is premium by default.
        Premium status comes from the PREMIUM_BYPASS_GUILD_IDS env
        var (which Railway sets in production for the bot owner's
        home alliance)."""
        assert await fresh_premium.is_premium(PREMIUM_TEST_GUILD_ID) is False
        assert await fresh_premium.is_premium(TEST_GUILD_ID) is False

    @pytest.mark.asyncio
    async def test_premium_via_bypass_env_var(self, monkeypatch):
        """Setting a guild's id in PREMIUM_BYPASS_GUILD_IDS flips it
        to premium — this is how the bot owner's home alliance stays
        premium in production without a Discord subscription."""
        monkeypatch.setenv("PREMIUM_BYPASS_GUILD_IDS", str(PREMIUM_TEST_GUILD_ID))
        import premium as _premium
        importlib.reload(_premium)
        try:
            assert await _premium.is_premium(PREMIUM_TEST_GUILD_ID) is True
            assert await _premium.is_premium(TEST_GUILD_ID) is False
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_force_premium_env_var(self, monkeypatch):
        monkeypatch.setenv("FORCE_PREMIUM", "1")
        import premium as _premium
        importlib.reload(_premium)
        try:
            assert await _premium.is_premium(TEST_GUILD_ID) is True
            assert await _premium.is_premium(999999) is True
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_bypass_guild_ids_env_var(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_BYPASS_GUILD_IDS", "111,222,333")
        import premium as _premium
        importlib.reload(_premium)
        try:
            assert await _premium.is_premium(111) is True
            assert await _premium.is_premium(222) is True
            assert await _premium.is_premium(333) is True
            assert await _premium.is_premium(444) is False
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_bypass_guild_ids_ignores_blank_and_garbage(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_BYPASS_GUILD_IDS", "111, ,not-a-number,222")
        import premium as _premium
        importlib.reload(_premium)
        try:
            assert await _premium.is_premium(111) is True
            assert await _premium.is_premium(222) is True
        finally:
            _premium.clear_cache()


# ── Assignment-layered is_premium flow ────────────────────────────────────────
#
# After issue #41 (User Subscription + assignment layer), `is_premium`
# resolution is:
#   FORCE_PREMIUM / bypass guilds → True
#   else → look up assigned user → user_has_active_subscription(...)
# Interaction-level entitlements no longer participate as a cheap path.

class TestAssignmentLayeredIsPremium:

    @pytest.mark.asyncio
    async def test_no_assignment_means_not_premium(self, fresh_premium):
        """Empty assignment table → no guild is premium even with SKU set."""
        assert await fresh_premium.is_premium(TEST_GUILD_ID) is False

    @pytest.mark.asyncio
    async def test_assigned_user_with_active_sub_grants_premium(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            user_id = 555000111
            _premium.assign(user_id, TEST_GUILD_ID)

            async def fake_iter(**kw):
                yield _make_entitlement(sku_id=12345)

            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            assert await _premium.is_premium(TEST_GUILD_ID, bot=bot) is True

            # Verify the bot.entitlements call used the assigned user, not
            # the guild — the per-user lookup is the new contract.
            _, call_kwargs = bot.entitlements.call_args
            assert "user" in call_kwargs

            # discord.py signature guard — bind kwargs against real signature.
            import inspect
            import discord
            sig = inspect.signature(discord.Client.entitlements)
            sig.bind_partial(bot, **call_kwargs)
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_assigned_user_without_active_sub_means_not_premium(self, monkeypatch):
        """Assignment row exists but Discord says no entitlement (lapsed
        sub) → guild reverts to free."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            _premium.assign(555000111, TEST_GUILD_ID)

            async def fake_iter(**kw):
                if False:
                    yield  # empty — sub lapsed
                return

            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            assert await _premium.is_premium(TEST_GUILD_ID, bot=bot) is False
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_interaction_entitlements_no_longer_grant_premium(self, monkeypatch):
        """User Subscription SKU: an interaction's entitlements reflect the
        interaction user, not necessarily the guild's assigned subscriber.
        The interaction path is no longer a cheap-positive — only the
        assignment layer + bot-side lookup count."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            interaction = MagicMock()
            interaction.entitlements = [_make_entitlement(sku_id=12345)]
            # No assignment for this guild → not premium, even with a
            # matching entitlement on the interaction.
            assert await _premium.is_premium(TEST_GUILD_ID, interaction=interaction) is False
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_no_sku_means_not_premium_even_with_assignment(self, fresh_premium):
        """Without PREMIUM_SKU_ID, user_has_active_subscription returns
        None (transient), so is_premium returns False without caching.
        The next call with a configured environment can still resolve True."""
        fresh_premium.assign(555000111, TEST_GUILD_ID)
        bot = MagicMock()
        assert await fresh_premium.is_premium(TEST_GUILD_ID, bot=bot) is False
        # Cache must NOT have been poisoned by the SKU-missing path.
        assert fresh_premium._cache_get(TEST_GUILD_ID) is None


# ── Caching of is_premium / user-subscription ────────────────────────────────

class TestPremiumCaching:

    @pytest.mark.asyncio
    async def test_repeated_is_premium_only_calls_discord_once(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            _premium.assign(555000111, TEST_GUILD_ID)
            call_count = {"n": 0}

            async def fake_iter(**kw):
                call_count["n"] += 1
                yield _make_entitlement(sku_id=12345)

            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            await _premium.is_premium(TEST_GUILD_ID, bot=bot)
            await _premium.is_premium(TEST_GUILD_ID, bot=bot)
            await _premium.is_premium(TEST_GUILD_ID, bot=bot)

            assert call_count["n"] == 1
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_negative_result_also_cached(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            _premium.assign(555000111, TEST_GUILD_ID)
            call_count = {"n": 0}

            async def fake_iter(**kw):
                call_count["n"] += 1
                if False:
                    yield
                return

            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            assert await _premium.is_premium(TEST_GUILD_ID, bot=bot) is False
            assert await _premium.is_premium(TEST_GUILD_ID, bot=bot) is False
            assert call_count["n"] == 1
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_api_errors_are_swallowed_and_not_cached(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            _premium.assign(555000111, TEST_GUILD_ID)
            call_count = {"n": 0}

            async def fake_iter(**kw):
                call_count["n"] += 1
                raise RuntimeError("Discord API down")
                yield  # unreachable

            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            assert await _premium.is_premium(TEST_GUILD_ID, bot=bot) is False
            # Second call should retry, not be cache-locked at False.
            assert await _premium.is_premium(TEST_GUILD_ID, bot=bot) is False
            assert call_count["n"] == 2
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_bot_none_does_not_poison_per_guild_cache(self, monkeypatch):
        """Regression: a caller that doesn't pass `bot=` must not cache
        False at the per-guild level. If it did, every subsequent caller
        (including the one that DOES pass bot correctly) would hit that
        False for the full 5-minute TTL and lock the subscriber out.
        """
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            _premium.assign(555000111, TEST_GUILD_ID)

            # First call: no bot, simulating a setup_cog or background loop.
            # Returns False (we can't verify the subscription) but must
            # NOT cache that False.
            assert await _premium.is_premium(TEST_GUILD_ID) is False

            # Second call: with bot, simulating /sync_members. The cache
            # must have been left alone so this call hits the real lookup
            # and returns True.
            async def fake_iter(**kw):
                yield _make_entitlement(sku_id=12345)

            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            assert await _premium.is_premium(TEST_GUILD_ID, bot=bot) is True
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_interaction_client_supplies_bot_when_kwarg_omitted(self, monkeypatch):
        """is_premium pulls bot off interaction.client when bot= isn't
        passed, so the 11 setup_cog call sites that pass only interaction=
        still get a real entitlement check.
        """
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            _premium.assign(555000111, TEST_GUILD_ID)

            async def fake_iter(**kw):
                yield _make_entitlement(sku_id=12345)

            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            interaction = MagicMock()
            interaction.client = bot

            # Caller passes only interaction, not bot. Should still resolve
            # to True because is_premium falls back to interaction.client.
            assert await _premium.is_premium(TEST_GUILD_ID, interaction=interaction) is True
        finally:
            _premium.clear_cache()


# ── get_limit ─────────────────────────────────────────────────────────────────

class TestGetLimit:

    @pytest.mark.asyncio
    async def test_free_returns_free_cap(self, fresh_premium):
        assert await fresh_premium.get_limit("events",            TEST_GUILD_ID) == 5
        assert await fresh_premium.get_limit("themes",            TEST_GUILD_ID) == 3
        assert await fresh_premium.get_limit("survey_questions",  TEST_GUILD_ID) == 5
        assert await fresh_premium.get_limit("growth_metrics",    TEST_GUILD_ID) == 5
        assert await fresh_premium.get_limit("train_templates",   TEST_GUILD_ID) == 1

    @pytest.mark.asyncio
    async def test_premium_returns_premium_cap(self, fresh_premium_with_bypass):
        # PREMIUM_TEST_GUILD_ID in the bypass list → resolves premium → premium-row limits.
        fresh_premium = fresh_premium_with_bypass
        assert await fresh_premium.get_limit("events",            PREMIUM_TEST_GUILD_ID) is None
        assert await fresh_premium.get_limit("train_templates",   PREMIUM_TEST_GUILD_ID) == 10
        assert await fresh_premium.get_limit("storm_templates",   PREMIUM_TEST_GUILD_ID) == 10
        assert await fresh_premium.get_limit("events_log_days",   PREMIUM_TEST_GUILD_ID) == 30

    @pytest.mark.asyncio
    async def test_unknown_feature_raises(self, fresh_premium):
        with pytest.raises(KeyError):
            await fresh_premium.get_limit("nonexistent_feature", TEST_GUILD_ID)


# ── is_premium_feature ────────────────────────────────────────────────────────

class TestIsPremiumFeature:

    def test_known_premium_features(self, fresh_premium):
        assert fresh_premium.is_premium_feature("member_sync")             is True
        assert fresh_premium.is_premium_feature("birthday_dm")             is True
        assert fresh_premium.is_premium_feature("thread_destinations")     is True
        assert fresh_premium.is_premium_feature("growth_custom_interval")  is True

    def test_unknown_feature_is_false(self, fresh_premium):
        assert fresh_premium.is_premium_feature("not_a_feature") is False
        assert fresh_premium.is_premium_feature("")              is False

    def test_survey_numeric_is_not_premium_anymore(self, fresh_premium):
        """1.1.5 promoted numeric survey questions to free tier. The
        PREMIUM_FEATURES set must reflect that — leaving the name in
        the set would be misleading documentation and would cause
        feature_gate('survey_numeric', ...) to actually gate it.
        """
        assert fresh_premium.is_premium_feature("survey_numeric") is False
        assert "survey_numeric" not in fresh_premium.PREMIUM_FEATURES


# ── feature_gate ──────────────────────────────────────────────────────────────

class TestFeatureGate:
    """feature_gate is the canonical premium check for named features.
    It validates the name against PREMIUM_FEATURES (KeyError on unknown)
    and delegates to is_premium for the entitlement lookup.
    """

    @pytest.mark.asyncio
    async def test_known_feature_returns_true_for_premium_guild(
        self, fresh_premium_with_bypass,
    ):
        # Bypass guild → is_premium True → feature_gate True.
        assert await fresh_premium_with_bypass.feature_gate(
            "member_sync", PREMIUM_TEST_GUILD_ID,
        ) is True

    @pytest.mark.asyncio
    async def test_known_feature_returns_false_for_free_guild(
        self, fresh_premium_with_bypass,
    ):
        # Non-bypass guild with no assignment → is_premium False.
        assert await fresh_premium_with_bypass.feature_gate(
            "storm_participation_dm", TEST_GUILD_ID,
        ) is False

    @pytest.mark.asyncio
    async def test_unknown_feature_raises_keyerror(self, fresh_premium):
        with pytest.raises(KeyError) as excinfo:
            await fresh_premium.feature_gate("not_a_feature", TEST_GUILD_ID)
        # Error should hint at the fix.
        assert "PREMIUM_FEATURES" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_survey_numeric_no_longer_gates(self, fresh_premium):
        """Regression: survey_numeric was removed from PREMIUM_FEATURES
        when 1.1.5 promoted numeric questions to free. Calling
        feature_gate('survey_numeric', ...) must now raise so any
        leftover gate call site fails loudly instead of silently
        locking free-tier users out of a free feature.
        """
        with pytest.raises(KeyError):
            await fresh_premium.feature_gate("survey_numeric", TEST_GUILD_ID)

    @pytest.mark.asyncio
    async def test_delegates_to_is_premium_with_bot(
        self, fresh_premium, monkeypatch,
    ):
        """feature_gate must thread bot= through so the per-guild
        entitlement check sees the real subscription state — same
        contract as is_premium itself."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            _premium.assign(555000111, TEST_GUILD_ID)

            async def fake_iter(**kw):
                yield _make_entitlement(sku_id=12345)

            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            assert await _premium.feature_gate(
                "member_sync", TEST_GUILD_ID, bot=bot,
            ) is True
        finally:
            _premium.clear_cache()


# ── Messaging helpers ─────────────────────────────────────────────────────────

class TestMessagingHelpers:

    def test_limit_reached_embed_includes_counts(self, fresh_premium):
        e = fresh_premium.limit_reached_embed(
            feature_label="events", current=5, cap=5, plural_unit="events",
        )
        assert "5 of 5" in e.description
        assert "events" in e.fields[0].name

    def test_premium_locked_embed_has_default_description(self, fresh_premium):
        e = fresh_premium.premium_locked_embed(feature_label="Member Roster Sync")
        assert "Member Roster Sync" in e.title
        assert "Premium" in e.description

    def test_premium_locked_embed_uses_custom_description_when_given(self, fresh_premium):
        e = fresh_premium.premium_locked_embed(
            feature_label="X",
            description="Custom message here.",
        )
        assert e.description == "Custom message here."

    def test_upgrade_view_returns_none_without_sku(self, fresh_premium):
        assert fresh_premium.upgrade_view() is None

    def test_upgrade_view_returns_view_with_sku(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            view = _premium.upgrade_view()
            assert view is not None
            assert len(view.children) == 1
            assert view.children[0].sku_id == 12345
        finally:
            _premium.clear_cache()


# ── ChannelSelectStep thread-destination gating (Phase 1.5) ───────────────────

class TestChannelSelectStepThreadGating:
    """Verify the include_threads kwarg controls which channel types appear."""

    def test_default_text_only(self, fresh_premium):
        import discord
        from setup_cog import ChannelSelectStep

        view  = ChannelSelectStep("Pick a channel...")
        # The first child is the ChannelSelect with channel_types attribute.
        select = view.children[0]
        assert discord.ChannelType.text in select.channel_types
        assert discord.ChannelType.public_thread  not in select.channel_types
        assert discord.ChannelType.private_thread not in select.channel_types

    def test_include_threads_adds_three_thread_types(self, fresh_premium):
        import discord
        from setup_cog import ChannelSelectStep

        view   = ChannelSelectStep("Pick a channel...", include_threads=True)
        select = view.children[0]
        assert discord.ChannelType.text            in select.channel_types
        assert discord.ChannelType.public_thread   in select.channel_types
        assert discord.ChannelType.private_thread  in select.channel_types
        assert discord.ChannelType.news_thread     in select.channel_types

    def test_create_button_visible_when_threads_included(self, fresh_premium):
        """The create button should appear in every channel-select state —
        creating only ever produces a text channel, which is fine even
        when the picker also offers thread types. (#48)"""
        from setup_cog import ChannelSelectStep

        view = ChannelSelectStep(
            "Pick a channel...",
            include_threads=True,
            allow_create=True,
        )
        labels = [getattr(c, "label", None) for c in view.children]
        assert "➕ Create a new channel" in labels

    def test_create_button_visible_when_text_only(self, fresh_premium):
        from setup_cog import ChannelSelectStep

        view = ChannelSelectStep(
            "Pick a channel...",
            include_threads=False,
            allow_create=True,
        )
        labels = [getattr(c, "label", None) for c in view.children]
        assert "➕ Create a new channel" in labels


# ── Log day-window resolution (Phase 6) ───────────────────────────────────────

class TestLogWindowLimits:
    """Verify get_limit returns the correct day window per feature/tier."""

    @pytest.mark.asyncio
    async def test_events_log_days_free_is_seven(self, fresh_premium):
        assert await fresh_premium.get_limit("events_log_days", TEST_GUILD_ID) == 7

    @pytest.mark.asyncio
    async def test_events_log_days_premium_is_thirty(self, fresh_premium_with_bypass):
        assert await fresh_premium_with_bypass.get_limit("events_log_days", PREMIUM_TEST_GUILD_ID) == 30

    @pytest.mark.asyncio
    async def test_train_log_days_free_is_seven(self, fresh_premium):
        assert await fresh_premium.get_limit("train_log_days", TEST_GUILD_ID) == 7

    @pytest.mark.asyncio
    async def test_train_log_days_premium_is_thirty(self, fresh_premium_with_bypass):
        assert await fresh_premium_with_bypass.get_limit("train_log_days", PREMIUM_TEST_GUILD_ID) == 30

    @pytest.mark.asyncio
    async def test_storm_log_recent_free_is_four(self, fresh_premium):
        assert await fresh_premium.get_limit("storm_log_recent", TEST_GUILD_ID) == 4

    @pytest.mark.asyncio
    async def test_storm_log_recent_premium_is_unlimited(self, fresh_premium_with_bypass):
        assert await fresh_premium_with_bypass.get_limit("storm_log_recent", PREMIUM_TEST_GUILD_ID) is None


# ── Storm log recent-date helper (Phase 6) ────────────────────────────────────

class TestStormLogRecentDates:
    """Verify list_recent_log_dates parses, dedupes, sorts, and trims."""

    def test_returns_n_most_recent_dates_sorted_desc(self, fresh_premium, monkeypatch):
        from datetime import date
        import storm_log

        rows = [
            ["Date", "Event", "VoteCount", "RTFNoVote", "SittingOut", "Prior"],
            ["1/5/2026",  "DS", "10", "", "", ""],
            ["1/12/2026", "DS", "11", "", "", ""],
            ["1/19/2026", "DS", "12", "", "", ""],
            ["1/26/2026", "DS", "13", "", "", ""],
            ["2/2/2026",  "DS", "14", "", "", ""],   # 5th
            ["1/12/2026", "CS", "20", "", "", ""],   # CS — should be filtered out
        ]

        class FakeWS:
            def get_all_values(self):
                return rows

        monkeypatch.setattr(storm_log, "_get_log_sheet", lambda guild_id, event_type=None: FakeWS())

        result = storm_log.list_recent_log_dates("DS", n=4, guild_id=999)
        assert result == [
            date(2026, 2, 2),
            date(2026, 1, 26),
            date(2026, 1, 19),
            date(2026, 1, 12),
        ]

    def test_returns_empty_list_when_sheet_unreadable(self, fresh_premium, monkeypatch):
        import storm_log

        def boom(guild_id, event_type=None):
            raise RuntimeError("Sheet unavailable")
        monkeypatch.setattr(storm_log, "_get_log_sheet", boom)

        result = storm_log.list_recent_log_dates("DS", n=4, guild_id=999)
        assert result == []

    def test_deduplicates_same_date_for_same_event(self, fresh_premium, monkeypatch):
        from datetime import date
        import storm_log

        rows = [
            ["Date", "Event", "VoteCount", "", "", ""],
            ["1/5/2026", "DS", "10", "", "", ""],
            ["1/5/2026", "DS", "10", "", "", ""],   # duplicate
            ["1/12/2026", "DS", "11", "", "", ""],
        ]

        class FakeWS:
            def get_all_values(self):
                return rows

        monkeypatch.setattr(storm_log, "_get_log_sheet", lambda guild_id, event_type=None: FakeWS())

        result = storm_log.list_recent_log_dates("DS", n=10, guild_id=999)
        assert result == [date(2026, 1, 12), date(2026, 1, 5)]


# ── /upgrade command behavior (Phase 7) ───────────────────────────────────────

class TestUpgradeCommand:
    """Verify /upgrade renders correctly for free vs premium and with/without SKU."""

    @pytest.mark.asyncio
    async def test_premium_user_sees_already_active_message(self, fresh_premium_with_bypass):
        from donate import DonateCog

        bot = MagicMock()
        cog = DonateCog(bot)

        interaction = AsyncMock()
        interaction.guild_id = PREMIUM_TEST_GUILD_ID  # premium via PREMIUM_BYPASS_GUILD_IDS
        interaction.entitlements = []
        interaction.response.send_message = AsyncMock()

        await cog.upgrade.callback(cog, interaction)

        call = interaction.response.send_message.call_args
        embed = call.kwargs.get("embed") or (call.args[0] if call.args else None)
        assert embed is not None
        assert "Premium is active" in embed.title

    @pytest.mark.asyncio
    async def test_free_user_with_no_sku_sees_unavailable_notice(self, fresh_premium):
        from donate import DonateCog

        bot = MagicMock()
        cog = DonateCog(bot)

        interaction = AsyncMock()
        interaction.guild_id = TEST_GUILD_ID  # free
        interaction.entitlements = []
        interaction.response.send_message = AsyncMock()

        await cog.upgrade.callback(cog, interaction)

        call  = interaction.response.send_message.call_args
        embed = call.kwargs.get("embed") or (call.args[0] if call.args else None)
        view  = call.kwargs.get("view")
        assert embed is not None
        assert "Premium" in embed.title
        # No SKU configured → no upgrade view, but a notice field is added.
        assert view is None
        notices = [f.name for f in embed.fields]
        assert any("not yet available" in n for n in notices)

    @pytest.mark.asyncio
    async def test_free_user_with_sku_gets_upgrade_view(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            # donate.py imports premium at module load — reload it so the upgrade
            # view function references the freshly-reloaded premium module.
            import donate
            importlib.reload(donate)
            from donate import DonateCog

            bot = MagicMock()
            cog = DonateCog(bot)

            interaction = AsyncMock()
            interaction.guild_id     = TEST_GUILD_ID
            interaction.entitlements = []
            interaction.response.send_message = AsyncMock()

            await cog.upgrade.callback(cog, interaction)

            call  = interaction.response.send_message.call_args
            view  = call.kwargs.get("view")
            assert view is not None
            assert len(view.children) == 1
            assert view.children[0].sku_id == 12345
        finally:
            _premium.clear_cache()
