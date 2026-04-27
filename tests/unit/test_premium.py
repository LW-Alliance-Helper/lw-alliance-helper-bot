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
from config import OGV_GUILD_ID


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def fresh_premium(monkeypatch):
    """
    Re-import the premium module per-test so module-level env reads
    (`PREMIUM_SKU_ID`) and the cache start clean.
    """
    # Clear env so module-level reads default to None
    for var in ("PREMIUM_SKU_ID", "FORCE_PREMIUM", "PREMIUM_TEST_GUILD_IDS"):
        monkeypatch.delenv(var, raising=False)
    import premium as _premium
    importlib.reload(_premium)
    _premium.clear_cache()
    yield _premium
    _premium.clear_cache()


def _make_entitlement(sku_id: int, deleted: bool = False):
    ent = MagicMock()
    ent.sku_id  = sku_id
    ent.deleted = deleted
    return ent


# ── Always-premium short-circuits ─────────────────────────────────────────────

class TestAlwaysPremiumShortCircuits:

    @pytest.mark.asyncio
    async def test_ogv_is_always_premium(self, fresh_premium):
        assert await fresh_premium.is_premium(OGV_GUILD_ID) is True

    @pytest.mark.asyncio
    async def test_random_guild_is_not_premium_by_default(self, fresh_premium):
        assert await fresh_premium.is_premium(TEST_GUILD_ID) is False

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
    async def test_test_guild_ids_env_var(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_TEST_GUILD_IDS", "111,222,333")
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
    async def test_test_guild_ids_ignores_blank_and_garbage(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_TEST_GUILD_IDS", "111, ,not-a-number,222")
        import premium as _premium
        importlib.reload(_premium)
        try:
            assert await _premium.is_premium(111) is True
            assert await _premium.is_premium(222) is True
        finally:
            _premium.clear_cache()


# ── Entitlement check via interaction ─────────────────────────────────────────

class TestInteractionEntitlements:

    @pytest.mark.asyncio
    async def test_no_sku_means_interaction_check_skipped(self, fresh_premium):
        """Without PREMIUM_SKU_ID, even a matching interaction entitlement is ignored."""
        interaction = MagicMock()
        interaction.entitlements = [_make_entitlement(sku_id=12345)]
        assert await fresh_premium.is_premium(TEST_GUILD_ID, interaction=interaction) is False

    @pytest.mark.asyncio
    async def test_matching_entitlement_grants_premium(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            interaction = MagicMock()
            interaction.entitlements = [_make_entitlement(sku_id=12345)]
            assert await _premium.is_premium(TEST_GUILD_ID, interaction=interaction) is True
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_deleted_entitlement_does_not_grant_premium(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            interaction = MagicMock()
            interaction.entitlements = [_make_entitlement(sku_id=12345, deleted=True)]
            assert await _premium.is_premium(TEST_GUILD_ID, interaction=interaction) is False
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_wrong_sku_does_not_grant_premium(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            interaction = MagicMock()
            interaction.entitlements = [_make_entitlement(sku_id=99999)]
            assert await _premium.is_premium(TEST_GUILD_ID, interaction=interaction) is False
        finally:
            _premium.clear_cache()


# ── Bot-API fallback + caching ────────────────────────────────────────────────

class TestBotEntitlementsFallback:

    @pytest.mark.asyncio
    async def test_bot_lookup_called_when_no_interaction(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            async def fake_iter(**kw):
                yield _make_entitlement(sku_id=12345)

            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            result = await _premium.is_premium(TEST_GUILD_ID, bot=bot)
            assert result is True
            assert bot.entitlements.called
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_bot_lookup_caches_result_and_skips_second_call(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            call_count = {"n": 0}

            async def fake_iter(**kw):
                call_count["n"] += 1
                yield _make_entitlement(sku_id=12345)

            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            await _premium.is_premium(TEST_GUILD_ID, bot=bot)
            await _premium.is_premium(TEST_GUILD_ID, bot=bot)
            await _premium.is_premium(TEST_GUILD_ID, bot=bot)

            # Only one fetch — subsequent calls served from cache.
            assert call_count["n"] == 1
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_bot_lookup_negative_result_also_cached(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            call_count = {"n": 0}

            async def fake_iter(**kw):
                call_count["n"] += 1
                # No matching entitlement — empty iterator.
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
    async def test_bot_lookup_swallows_api_errors(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            async def fake_iter(**kw):
                raise RuntimeError("Discord API down")
                yield  # unreachable

            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            # Should default to False, not raise.
            assert await _premium.is_premium(TEST_GUILD_ID, bot=bot) is False
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
    async def test_premium_returns_premium_cap(self, fresh_premium):
        # OGV is always premium → should hit the premium row.
        assert await fresh_premium.get_limit("events",            OGV_GUILD_ID) is None
        assert await fresh_premium.get_limit("train_templates",   OGV_GUILD_ID) == 10
        assert await fresh_premium.get_limit("storm_templates",   OGV_GUILD_ID) == 10
        assert await fresh_premium.get_limit("events_log_days",   OGV_GUILD_ID) == 30

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
