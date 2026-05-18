"""
Unit tests for the Premium assignment layer (issue #41).

The assignment layer pins each Premium subscriber's User Subscription to a
single guild at a time. These tests cover:

- The data-layer helpers (`config.set_premium_assignment`, etc.)
- The premium.py wrappers that invalidate caches on changes
- The four slash commands (`/upgrade`, `/premium_assign`, `/premium_status`,
  `/premium_unassign`) across their state-aware branches
- The `on_entitlement_create` / `on_entitlement_delete` listeners that
  auto-assign on first checkout and refresh caches on lapse
"""

import importlib
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from tests.conftest import TEST_GUILD_ID
from tests.constants import PREMIUM_TEST_GUILD_ID


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate_premium_env(monkeypatch, temp_db):
    """Same isolation pattern as test_premium.py — clear env, reload module,
    drop caches. `temp_db` is composed because the assignment layer hits
    SQLite on every is_premium() call."""
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


@pytest.fixture
def fresh_premium():
    import premium as _premium
    importlib.reload(_premium)
    _premium.clear_cache()
    yield _premium
    _premium.clear_cache()


def _make_entitlement(sku_id: int, user_id: int = 555000111,
                       deleted: bool = False, ends_at=None):
    ent = MagicMock()
    ent.sku_id  = sku_id
    ent.user_id = user_id
    ent.deleted = deleted
    # Set explicitly so _entitlement_matches' ends_at check sees None
    # rather than an auto-MagicMock attribute, which would fail the
    # `ends_at < datetime.now(...)` comparison with a TypeError.
    ent.ends_at = ends_at
    return ent


def _make_donate_cog():
    """Build a fresh DonateCog with a mock bot. Reloads `donate` so it sees
    the current `premium` module (which the autouse fixture reloads with a
    clean env per test)."""
    import donate
    importlib.reload(donate)
    from donate import DonateCog
    bot = MagicMock()
    bot.fetch_user  = AsyncMock(return_value=MagicMock(name="ZTestUser"))
    bot.fetch_guild = AsyncMock()
    bot.get_user    = MagicMock(return_value=None)
    bot.get_guild   = MagicMock(return_value=None)
    return DonateCog(bot), bot


def _patch_confirm_view(monkeypatch, confirmed: bool):
    """Replace donate._ConfirmActionView with a stub that immediately
    resolves to the given decision. Slash-command tests use this to
    exercise the post-confirmation branch without driving real button
    clicks."""
    import donate

    class _StubView:
        def __init__(self, *args, **kwargs):
            self.confirmed = confirmed
            self.message = None

        async def wait(self):
            return

    monkeypatch.setattr(donate, "_ConfirmActionView", _StubView)


# ── Data-layer helpers ────────────────────────────────────────────────────────

class TestAssignmentHelpers:

    def test_assign_creates_row(self, fresh_premium):
        from config import (
            get_premium_assignment_for_guild,
            get_premium_assignment_for_user,
        )
        fresh_premium.assign(user_id=111, guild_id=222)
        assert get_premium_assignment_for_guild(222) == 111
        assert get_premium_assignment_for_user(111) == 222

    def test_assign_moves_existing_user_to_new_guild(self, fresh_premium):
        from config import (
            get_premium_assignment_for_guild,
            get_premium_assignment_for_user,
        )
        fresh_premium.assign(user_id=111, guild_id=222)
        fresh_premium.assign(user_id=111, guild_id=333)
        # Old guild is freed, new guild is held.
        assert get_premium_assignment_for_guild(222) is None
        assert get_premium_assignment_for_guild(333) == 111
        assert get_premium_assignment_for_user(111) == 333

    def test_assign_displaces_prior_subscriber_on_same_guild(self, fresh_premium):
        """The data layer enforces UNIQUE(guild_id) by displacing the prior
        holder. The slash-command surface should reject this case before
        calling assign(), but the underlying primitive must not crash."""
        from config import (
            get_premium_assignment_for_guild,
            get_premium_assignment_for_user,
        )
        fresh_premium.assign(user_id=111, guild_id=222)
        displaced = fresh_premium.assign(user_id=999, guild_id=222)
        assert displaced == 111
        assert get_premium_assignment_for_guild(222) == 999
        assert get_premium_assignment_for_user(111) is None
        assert get_premium_assignment_for_user(999) == 222

    def test_unassign_removes_row_and_returns_freed_guild(self, fresh_premium):
        from config import get_premium_assignment_for_guild
        fresh_premium.assign(user_id=111, guild_id=222)
        freed = fresh_premium.unassign(111)
        assert freed == 222
        assert get_premium_assignment_for_guild(222) is None

    def test_unassign_returns_none_for_unknown_user(self, fresh_premium):
        assert fresh_premium.unassign(99999) is None


# ── Cache invalidation on assignment changes ──────────────────────────────────

class TestAssignmentCacheInvalidation:

    @pytest.mark.asyncio
    async def test_assign_invalidates_new_guild_cache(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            # Prime the cache with a False result for guild 222 (no assignment).
            assert await _premium.is_premium(222) is False

            # Now create the assignment + simulate active subscription.
            async def fake_iter(**kw):
                yield _make_entitlement(sku_id=12345)
            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            _premium.assign(user_id=111, guild_id=222)
            # The cache for guild 222 should have been invalidated by assign().
            assert await _premium.is_premium(222, bot=bot) is True
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_assign_invalidates_old_guild_cache_on_move(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            _premium.assign(user_id=111, guild_id=222)

            async def fake_iter(**kw):
                yield _make_entitlement(sku_id=12345)
            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            # Prime: guild 222 cached as premium.
            assert await _premium.is_premium(222, bot=bot) is True

            # Move the assignment — guild 222 should flip back to free, no
            # stale True read from a 5-minute-old cache entry.
            _premium.assign(user_id=111, guild_id=333)
            assert await _premium.is_premium(222, bot=bot) is False
            assert await _premium.is_premium(333, bot=bot) is True
        finally:
            _premium.clear_cache()

    @pytest.mark.asyncio
    async def test_unassign_invalidates_freed_guild_cache(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            _premium.assign(user_id=111, guild_id=222)
            async def fake_iter(**kw):
                yield _make_entitlement(sku_id=12345)
            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: fake_iter(**kw))

            assert await _premium.is_premium(222, bot=bot) is True
            _premium.unassign(111)
            assert await _premium.is_premium(222, bot=bot) is False
        finally:
            _premium.clear_cache()


# ── Lapse-and-resume ──────────────────────────────────────────────────────────

class TestLapseAndResume:

    @pytest.mark.asyncio
    async def test_assignment_persists_through_subscription_lapse(self, monkeypatch):
        """When a subscription ends, the assignment row stays in place so
        that resubscribing auto-resumes Premium in the same guild."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        try:
            from config import get_premium_assignment_for_guild
            _premium.assign(user_id=111, guild_id=222)

            # Simulate an entitlement-delete event: caches refresh, but the
            # assignment row stays.
            _premium.invalidate_for_user(111)
            assert get_premium_assignment_for_guild(222) == 111

            # Subscription absent → not premium.
            async def empty_iter(**kw):
                if False:
                    yield
                return
            bot = MagicMock()
            bot.entitlements = MagicMock(side_effect=lambda **kw: empty_iter(**kw))
            assert await _premium.is_premium(222, bot=bot) is False

            # User resubscribes — fire entitlement_create cache refresh.
            _premium.invalidate_for_user(111)

            async def matching_iter(**kw):
                yield _make_entitlement(sku_id=12345)
            bot.entitlements = MagicMock(side_effect=lambda **kw: matching_iter(**kw))

            # Same assignment → premium auto-resumes in the same guild.
            assert await _premium.is_premium(222, bot=bot) is True
        finally:
            _premium.clear_cache()


# ── /premium_assign ───────────────────────────────────────────────────────────

class TestPremiumAssignCommand:

    @pytest.mark.asyncio
    async def test_no_subscription_prompts_to_upgrade(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        cog, _ = _make_donate_cog()
        async def empty_iter(**kw):
            if False:
                yield
            return
        cog.bot.entitlements = MagicMock(side_effect=lambda **kw: empty_iter(**kw))

        interaction = AsyncMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.user.id  = 111
        interaction.guild    = MagicMock(name="Test", id=TEST_GUILD_ID)
        interaction.response.send_message = AsyncMock()

        await cog.premium_assign.callback(cog, interaction)

        call = interaction.response.send_message.call_args
        embed = call.kwargs.get("embed") or call.args[0]
        assert "don't have an active Premium subscription" in embed.title.lower() or \
               "no active" in embed.title.lower() or \
               "subscribe" in embed.description.lower()

    @pytest.mark.asyncio
    async def test_already_assigned_here_short_circuits(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        cog, _ = _make_donate_cog()

        async def matching_iter(**kw):
            yield _make_entitlement(sku_id=12345, user_id=111)
        cog.bot.entitlements = MagicMock(side_effect=lambda **kw: matching_iter(**kw))
        _premium.assign(user_id=111, guild_id=TEST_GUILD_ID)

        interaction = AsyncMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.user.id  = 111
        interaction.guild    = MagicMock(name="Test", id=TEST_GUILD_ID)
        interaction.response.send_message = AsyncMock()

        await cog.premium_assign.callback(cog, interaction)
        call = interaction.response.send_message.call_args
        embed = call.kwargs.get("embed") or call.args[0]
        assert "already" in embed.title.lower() or \
               "already" in embed.description.lower()

    @pytest.mark.asyncio
    async def test_fresh_assignment_confirms_then_assigns(self, monkeypatch):
        """Fresh /premium_assign now prompts for confirmation naming the
        target guild — only writes the assignment after the user confirms."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        cog, _ = _make_donate_cog()

        async def matching_iter(**kw):
            yield _make_entitlement(sku_id=12345, user_id=111)
        cog.bot.entitlements = MagicMock(side_effect=lambda **kw: matching_iter(**kw))

        _patch_confirm_view(monkeypatch, confirmed=True)

        interaction = AsyncMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.user.id  = 111
        interaction.guild    = MagicMock()
        interaction.guild.name = "Test Alliance"
        interaction.response.send_message = AsyncMock()
        interaction.followup.send         = AsyncMock()
        interaction.original_response     = AsyncMock(return_value=MagicMock())

        await cog.premium_assign.callback(cog, interaction)

        # Confirmation embed shown first; assignment written after confirm.
        prompt_call = interaction.response.send_message.call_args
        prompt_embed = prompt_call.kwargs.get("embed") or prompt_call.args[0]
        assert "pin premium" in prompt_embed.title.lower()
        assert "Test Alliance" in prompt_embed.description

        from config import get_premium_assignment_for_guild
        assert get_premium_assignment_for_guild(TEST_GUILD_ID) == 111

    @pytest.mark.asyncio
    async def test_fresh_assignment_cancelled_does_not_write(self, monkeypatch):
        """If the user cancels the confirmation, no assignment is written."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        cog, _ = _make_donate_cog()

        async def matching_iter(**kw):
            yield _make_entitlement(sku_id=12345, user_id=111)
        cog.bot.entitlements = MagicMock(side_effect=lambda **kw: matching_iter(**kw))

        _patch_confirm_view(monkeypatch, confirmed=False)

        interaction = AsyncMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.user.id  = 111
        interaction.guild    = MagicMock()
        interaction.guild.name = "Test Alliance"
        interaction.response.send_message = AsyncMock()
        interaction.followup.send         = AsyncMock()
        interaction.original_response     = AsyncMock(return_value=MagicMock())

        await cog.premium_assign.callback(cog, interaction)

        from config import get_premium_assignment_for_user
        assert get_premium_assignment_for_user(111) is None

    @pytest.mark.asyncio
    async def test_blocked_by_other_subscriber(self, monkeypatch):
        """If a different subscriber has already pinned this guild, reject
        and surface the blocker's username."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        _premium.assign(user_id=999, guild_id=TEST_GUILD_ID)  # other subscriber holds it

        cog, bot = _make_donate_cog()
        # Caller (111) has an active subscription.
        async def matching_iter(**kw):
            yield _make_entitlement(sku_id=12345, user_id=111)
        bot.entitlements = MagicMock(side_effect=lambda **kw: matching_iter(**kw))

        # Resolve the holder's name so the embed can mention them.
        holder_user = MagicMock()
        holder_user.name = "OtherSubscriber"
        bot.fetch_user = AsyncMock(return_value=holder_user)
        bot.get_user   = MagicMock(return_value=None)

        interaction = AsyncMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.user.id  = 111
        interaction.guild    = MagicMock()
        interaction.guild.name = "Test"
        interaction.response.send_message = AsyncMock()

        await cog.premium_assign.callback(cog, interaction)

        call = interaction.response.send_message.call_args
        embed = call.kwargs.get("embed") or call.args[0]
        # Caller should NOT have ended up with the assignment.
        from config import get_premium_assignment_for_user
        assert get_premium_assignment_for_user(111) is None
        # And the holder's username must be in the message for coordination.
        assert "OtherSubscriber" in embed.description


# ── /premium_status ───────────────────────────────────────────────────────────

class TestPremiumStatusCommand:

    @pytest.mark.asyncio
    async def test_no_subscription_no_assignment(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        cog, _ = _make_donate_cog()
        async def empty_iter(**kw):
            if False:
                yield
            return
        cog.bot.entitlements = MagicMock(side_effect=lambda **kw: empty_iter(**kw))

        interaction = AsyncMock()
        interaction.user.id  = 111
        interaction.response.send_message = AsyncMock()

        await cog.premium_status.callback(cog, interaction)

        call = interaction.response.send_message.call_args
        embed = call.kwargs.get("embed") or call.args[0]
        assert "no active subscription" in embed.title.lower()

    @pytest.mark.asyncio
    async def test_subscription_active_assigned_here(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        _premium.assign(user_id=111, guild_id=TEST_GUILD_ID)

        cog, bot = _make_donate_cog()
        async def matching_iter(**kw):
            yield _make_entitlement(sku_id=12345, user_id=111)
        bot.entitlements = MagicMock(side_effect=lambda **kw: matching_iter(**kw))

        guild_obj = MagicMock()
        guild_obj.name = "AssignedGuild"
        bot.get_guild = MagicMock(return_value=guild_obj)

        interaction = AsyncMock()
        interaction.user.id = 111
        interaction.response.send_message = AsyncMock()

        await cog.premium_status.callback(cog, interaction)
        call = interaction.response.send_message.call_args
        embed = call.kwargs.get("embed") or call.args[0]
        assert "AssignedGuild" in embed.description

    @pytest.mark.asyncio
    async def test_subscription_lapsed_with_preserved_assignment(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        _premium.assign(user_id=111, guild_id=TEST_GUILD_ID)

        cog, bot = _make_donate_cog()
        async def empty_iter(**kw):
            if False:
                yield
            return
        bot.entitlements = MagicMock(side_effect=lambda **kw: empty_iter(**kw))

        guild_obj = MagicMock()
        guild_obj.name = "PreservedGuild"
        bot.get_guild = MagicMock(return_value=guild_obj)

        interaction = AsyncMock()
        interaction.user.id = 111
        interaction.response.send_message = AsyncMock()

        await cog.premium_status.callback(cog, interaction)
        call = interaction.response.send_message.call_args
        embed = call.kwargs.get("embed") or call.args[0]
        # Preserved-assignment message should mention re-subscription resumes
        # there, and name the preserved guild.
        assert "PreservedGuild" in embed.description
        assert "resubscribe" in embed.description.lower() or \
               "resume" in embed.description.lower()


# ── /premium_unassign ─────────────────────────────────────────────────────────

class TestPremiumUnassignCommand:

    @pytest.mark.asyncio
    async def test_no_assignment_says_nothing_to_release(self):
        cog, _ = _make_donate_cog()
        interaction = AsyncMock()
        interaction.user.id = 111
        interaction.response.send_message = AsyncMock()

        await cog.premium_unassign.callback(cog, interaction)
        call = interaction.response.send_message.call_args
        embed = call.kwargs.get("embed") or call.args[0]
        assert "nothing to release" in embed.title.lower()

    @pytest.mark.asyncio
    async def test_with_assignment_confirms_then_releases(self, monkeypatch):
        """/premium_unassign now prompts for confirmation naming the
        guild that's about to revert — only releases after the user confirms."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        _premium.assign(user_id=111, guild_id=TEST_GUILD_ID)

        cog, bot = _make_donate_cog()
        guild_obj = MagicMock()
        guild_obj.name = "ReleasedGuild"
        bot.get_guild = MagicMock(return_value=guild_obj)

        _patch_confirm_view(monkeypatch, confirmed=True)

        interaction = AsyncMock()
        interaction.user.id = 111
        interaction.response.send_message = AsyncMock()
        interaction.followup.send         = AsyncMock()
        interaction.original_response     = AsyncMock(return_value=MagicMock())

        await cog.premium_unassign.callback(cog, interaction)

        # Confirmation embed names the guild being released.
        prompt_call = interaction.response.send_message.call_args
        prompt_embed = prompt_call.kwargs.get("embed") or prompt_call.args[0]
        assert "release" in prompt_embed.title.lower()
        assert "ReleasedGuild" in prompt_embed.description

        from config import get_premium_assignment_for_user
        assert get_premium_assignment_for_user(111) is None

    @pytest.mark.asyncio
    async def test_unassign_cancelled_does_not_release(self, monkeypatch):
        """If the user cancels the confirmation, the assignment stays."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        _premium.assign(user_id=111, guild_id=TEST_GUILD_ID)

        cog, bot = _make_donate_cog()
        guild_obj = MagicMock()
        guild_obj.name = "ReleasedGuild"
        bot.get_guild = MagicMock(return_value=guild_obj)

        _patch_confirm_view(monkeypatch, confirmed=False)

        interaction = AsyncMock()
        interaction.user.id = 111
        interaction.response.send_message = AsyncMock()
        interaction.followup.send         = AsyncMock()
        interaction.original_response     = AsyncMock(return_value=MagicMock())

        await cog.premium_unassign.callback(cog, interaction)

        from config import get_premium_assignment_for_user
        assert get_premium_assignment_for_user(111) == TEST_GUILD_ID  # unchanged


# ── /upgrade auto-assign branch ───────────────────────────────────────────────

class TestUpgradeAutoAssign:

    @pytest.mark.asyncio
    async def test_subscriber_in_unassigned_guild_auto_assigns(self, monkeypatch):
        """A subscriber who's already paying but has never run /premium_assign:
        running /upgrade in any guild auto-assigns it there with clear
        messaging."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)

        cog, bot = _make_donate_cog()
        async def matching_iter(**kw):
            yield _make_entitlement(sku_id=12345, user_id=111)
        bot.entitlements = MagicMock(side_effect=lambda **kw: matching_iter(**kw))

        interaction = AsyncMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.user.id  = 111
        interaction.guild    = MagicMock()
        interaction.guild.name = "FreshGuild"
        interaction.response.send_message = AsyncMock()
        interaction.entitlements = []

        await cog.upgrade.callback(cog, interaction)

        from config import get_premium_assignment_for_guild
        assert get_premium_assignment_for_guild(TEST_GUILD_ID) == 111

    @pytest.mark.asyncio
    async def test_subscriber_in_other_assigned_guild_prompts_switch(self, monkeypatch):
        """If the caller's subscription is pinned to a *different* guild,
        /upgrade explains they need to use /premium_assign here to switch."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        _premium.assign(user_id=111, guild_id=999_888_777)  # elsewhere

        cog, bot = _make_donate_cog()
        async def matching_iter(**kw):
            yield _make_entitlement(sku_id=12345, user_id=111)
        bot.entitlements = MagicMock(side_effect=lambda **kw: matching_iter(**kw))

        other_guild = MagicMock()
        other_guild.name = "OtherAlliance"
        bot.get_guild = MagicMock(return_value=other_guild)

        interaction = AsyncMock()
        interaction.guild_id = TEST_GUILD_ID
        interaction.user.id  = 111
        interaction.guild    = MagicMock()
        interaction.guild.name = "ThisGuild"
        interaction.response.send_message = AsyncMock()
        interaction.entitlements = []

        await cog.upgrade.callback(cog, interaction)

        # Should NOT have moved the assignment automatically.
        from config import get_premium_assignment_for_user
        assert get_premium_assignment_for_user(111) == 999_888_777

        # Embed should mention the OtherAlliance to make the switch obvious.
        call = interaction.response.send_message.call_args
        embed = call.kwargs.get("embed") or call.args[0]
        assert "OtherAlliance" in embed.description


# ── on_entitlement_create / on_entitlement_delete ─────────────────────────────

class TestEntitlementListeners:

    @pytest.mark.asyncio
    async def test_create_with_pending_guild_auto_assigns(self, monkeypatch):
        """User runs /upgrade in guild X (which records pending), then
        completes checkout — on_entitlement_create assigns to X."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        cog, bot = _make_donate_cog()

        cog._pending_upgrade_guilds[111] = TEST_GUILD_ID

        ent = _make_entitlement(sku_id=12345, user_id=111)
        await cog.on_entitlement_create(ent)

        from config import get_premium_assignment_for_guild
        assert get_premium_assignment_for_guild(TEST_GUILD_ID) == 111
        # Pending entry consumed.
        assert 111 not in cog._pending_upgrade_guilds

    @pytest.mark.asyncio
    async def test_create_for_wrong_sku_is_ignored(self, monkeypatch):
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        cog, _ = _make_donate_cog()
        cog._pending_upgrade_guilds[111] = TEST_GUILD_ID

        ent = _make_entitlement(sku_id=99999, user_id=111)  # different SKU
        await cog.on_entitlement_create(ent)

        from config import get_premium_assignment_for_guild
        assert get_premium_assignment_for_guild(TEST_GUILD_ID) is None

    @pytest.mark.asyncio
    async def test_create_with_no_pending_dms_user(self, monkeypatch):
        """Subscribed via Discord app store (no /upgrade context) — bot
        DMs them prompting a manual /premium_assign rather than guessing."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        cog, bot = _make_donate_cog()

        # Mock fetch_user to return a user with a sendable DM.
        user_mock = AsyncMock()
        user_mock.send = AsyncMock()
        bot.fetch_user = AsyncMock(return_value=user_mock)
        bot.get_user   = MagicMock(return_value=None)

        ent = _make_entitlement(sku_id=12345, user_id=111)
        await cog.on_entitlement_create(ent)

        # No assignment should have been written.
        from config import get_premium_assignment_for_user
        assert get_premium_assignment_for_user(111) is None
        # DM should have been attempted.
        user_mock.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_for_resubscriber_keeps_existing_assignment(self, monkeypatch):
        """Subscription lapsed → user resubscribes. Existing assignment row
        preserved; on_entitlement_create just refreshes caches and exits."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        _premium.assign(user_id=111, guild_id=TEST_GUILD_ID)
        cog, bot = _make_donate_cog()

        ent = _make_entitlement(sku_id=12345, user_id=111)
        await cog.on_entitlement_create(ent)

        from config import get_premium_assignment_for_guild
        assert get_premium_assignment_for_guild(TEST_GUILD_ID) == 111  # unchanged

    @pytest.mark.asyncio
    async def test_create_blocked_by_other_subscriber_dms_user(self, monkeypatch):
        """Race: /upgrade was opened in guild X, but another subscriber
        claimed X before checkout completed. Bot DMs the new subscriber to
        pick a different guild instead of clobbering."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        _premium.assign(user_id=999, guild_id=TEST_GUILD_ID)  # holder

        cog, bot = _make_donate_cog()
        cog._pending_upgrade_guilds[111] = TEST_GUILD_ID

        user_mock = AsyncMock()
        user_mock.send = AsyncMock()
        bot.fetch_user = AsyncMock(return_value=user_mock)
        bot.get_user   = MagicMock(return_value=None)

        ent = _make_entitlement(sku_id=12345, user_id=111)
        await cog.on_entitlement_create(ent)

        from config import get_premium_assignment_for_user
        assert get_premium_assignment_for_user(111) is None  # not assigned
        user_mock.send.assert_called_once()  # DM sent

    @pytest.mark.asyncio
    async def test_delete_invalidates_caches(self, monkeypatch):
        """When an entitlement is deleted, the assigned guild's cache
        flips to free on the next is_premium read instead of returning a
        stale True for the rest of the TTL."""
        monkeypatch.setenv("PREMIUM_SKU_ID", "12345")
        import premium as _premium
        importlib.reload(_premium)
        _premium.assign(user_id=111, guild_id=TEST_GUILD_ID)
        cog, bot = _make_donate_cog()

        # Prime the cache as premium.
        async def matching_iter(**kw):
            yield _make_entitlement(sku_id=12345, user_id=111)
        bot.entitlements = MagicMock(side_effect=lambda **kw: matching_iter(**kw))
        assert await _premium.is_premium(TEST_GUILD_ID, bot=bot) is True

        # Now simulate the entitlement-delete event.
        ent = _make_entitlement(sku_id=12345, user_id=111)
        await cog.on_entitlement_delete(ent)

        # Discord now returns nothing.
        async def empty_iter(**kw):
            if False:
                yield
            return
        bot.entitlements = MagicMock(side_effect=lambda **kw: empty_iter(**kw))

        # Next is_premium should NOT return the stale True.
        assert await _premium.is_premium(TEST_GUILD_ID, bot=bot) is False
