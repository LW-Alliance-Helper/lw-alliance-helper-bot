"""
premium.py — Premium tier entitlement checks, feature limits, and UX helpers.

The bot has two tiers: free and premium. Premium is sold via a Discord App
**User Subscription** (SKU configured in the Discord Developer Portal). A
User Subscription is valid in every guild the subscriber shares with the
bot, which is not the licensing model — the intent is one $4.99/mo per
*guild*, with the subscriber able to move their license between guilds.

To enforce that, the bot maintains an assignment layer (`premium_assignments`
table; see `config.py`):
  - One subscriber → one assigned guild.
  - `is_premium(guild_id)` first looks up the assigned user, then verifies
    that user still has an active Discord subscription. The cached result
    (5-minute TTL, keyed by guild_id) absorbs the cost of the per-user
    `bot.entitlements()` lookup.

For development and bypass scenarios (e.g. the bot owner's home alliance),
two env-var overrides are available and short-circuit before the
assignment check:
  - `FORCE_PREMIUM=1`             — flags every guild as premium (dev nuke)
  - `PREMIUM_BYPASS_GUILD_IDS`    — comma-separated guild ids that always
                                    resolve to premium (no subscription
                                    needed). Use this for the owner's home
                                    server, internal test servers, or any
                                    guild that should permanently sit on
                                    the premium tier without paying.

Public API:
  - `is_premium(guild_id, interaction=None, bot=None)` → bool
  - `user_has_active_subscription(user_id, bot)`       → bool
  - `get_assigned_guild(user_id)`                      → int | None
  - `get_assigned_user(guild_id)`                      → int | None
  - `assign(user_id, guild_id)`                        → int | None  (prior user displaced)
  - `unassign(user_id)`                                → int | None  (guild freed)
  - `get_limit(feature, guild_id, ...)`                → int | None  (None = unlimited)
  - `is_premium_feature(name)`                         → bool        (declarative whitelist)
  - `feature_gate(name, guild_id, ...)`                → bool        (KeyError on unknown name)
  - `silent_fallback_counts()`                         → dict[str,int] (diagnostic)
  - `limit_reached_embed(...)`, `premium_locked_embed(...)`, `upgrade_view(...)`
"""

import os
import time
from typing import Optional

import discord


# ── Configuration ─────────────────────────────────────────────────────────────

# Discord SKU ID for the premium subscription (set in Discord Developer Portal).
# Until this is set, no real subscriptions can be detected — only the
# env-var bypass guilds and FORCE_PREMIUM are treated as premium.
PREMIUM_SKU_ID: Optional[int] = (
    int(os.environ["PREMIUM_SKU_ID"])
    if os.environ.get("PREMIUM_SKU_ID", "").strip().isdigit()
    else None
)

# Silent-fallback observability. Each fallback path (no SKU, no bot, no
# assignment table) logs once per process to keep stdout quiet, then
# increments a counter on every subsequent hit. Counters are
# process-local and reset on Railway restart, which is also when the
# once-per-process log re-emits — keeping the two on the same cadence.
# Read snapshots via `silent_fallback_counts()` for diagnostic queries,
# e.g. an admin command tailing how often these paths fire.
_silent_fallback_counts: dict[str, int] = {
    "no_sku": 0,
    "no_bot": 0,
    "no_assignment_table": 0,
}


def _track_silent_fallback(reason: str, detail: str) -> None:
    """Increment the counter for `reason`. Emit `detail` to stdout on
    the FIRST hit per process; later hits stay silent so background
    loops don't fill the log."""
    prev = _silent_fallback_counts.get(reason, 0)
    _silent_fallback_counts[reason] = prev + 1
    if prev == 0:
        print(f"[PREMIUM] silent fallback ({reason}): {detail}")


def silent_fallback_counts() -> dict[str, int]:
    """Snapshot of how many times each silent-fallback path fired this
    process. Useful for diagnostic / admin commands that need to spot
    a regression (e.g. a refactor that drops bot= on an is_premium
    call site) before a paying subscriber files a ticket."""
    return dict(_silent_fallback_counts)


def _reset_silent_fallback_counts() -> None:
    """Test-only: reset the counters between cases. Production code
    has no reason to call this — counters live for the bot's process
    lifetime."""
    for k in _silent_fallback_counts:
        _silent_fallback_counts[k] = 0


# Per-feature limits. None means unlimited.
LIMITS: dict[str, dict[str, Optional[int]]] = {
    "events": {"free": 5, "premium": None},
    "themes": {"free": 3, "premium": None},
    "tones": {"free": 3, "premium": None},
    "train_templates": {"free": 1, "premium": 10},
    "storm_templates": {"free": 1, "premium": 10},
    "storm_log_recent": {"free": 4, "premium": None},
    "survey_questions": {"free": 5, "premium": None},
    "growth_metrics": {"free": 5, "premium": None},
    "events_log_days": {"free": 7, "premium": 30},
    "train_log_days": {"free": 7, "premium": 30},
}

# Premium-only features (boolean gates, not counts).
#
# Source of truth for "this named feature requires an active premium
# subscription." Every gate that's named should run through
# `feature_gate(name, guild_id, ...)`, which validates the name against
# this set and raises KeyError on unknown names. That forces new premium
# features to register here up front instead of silently shipping ungated.
#
# `survey_numeric` was moved off this list when 1.1.5 promoted numeric
# survey questions to the free tier (min/max bounds on numeric questions
# remain the differentiator and are gated separately).
PREMIUM_FEATURES: set[str] = {
    "member_sync",
    "birthday_dm",
    "train_dm",
    "survey_reminder_dm",
    "auto_mention",
    "storm_participation_dm",
    "growth_custom_interval",
    "survey_multi_select",
    "survey_date",
    "multiple_surveys",
    "thread_destinations",
    # Profession Buddy System (#289). Free tier keeps the shared list, the
    # /buddy lookup, and manual pairing; these four gate the Premium layer.
    "buddy_auto_assign",
    "buddy_self_service",
    "buddy_auto_repair",
    "buddy_dm",
}


# ── Entitlement cache ─────────────────────────────────────────────────────────

_CACHE_TTL_SECONDS = 300  # 5 minutes
_entitlement_cache: dict[int, tuple[bool, float]] = {}
# Per-user subscription cache. Discord's User Subscription SKU returns the
# same answer regardless of which guild we're querying for, so caching by
# user_id avoids re-fetching when the same subscriber is checked across
# multiple guilds (or when /premium overview and /premium assign run in quick
# succession).
_user_subscription_cache: dict[int, tuple[bool, float]] = {}


def _cache_get(guild_id: int) -> Optional[bool]:
    entry = _entitlement_cache.get(guild_id)
    if entry is None:
        return None
    value, ts = entry
    if time.time() - ts >= _CACHE_TTL_SECONDS:
        return None
    return value


def _cache_set(guild_id: int, value: bool) -> None:
    _entitlement_cache[guild_id] = (value, time.time())


def _cache_invalidate_guild(guild_id: int) -> None:
    _entitlement_cache.pop(guild_id, None)


def _user_cache_get(user_id: int) -> Optional[bool]:
    entry = _user_subscription_cache.get(user_id)
    if entry is None:
        return None
    value, ts = entry
    if time.time() - ts >= _CACHE_TTL_SECONDS:
        return None
    return value


def _user_cache_set(user_id: int, value: bool) -> None:
    _user_subscription_cache[user_id] = (value, time.time())


def _user_cache_invalidate(user_id: int) -> None:
    _user_subscription_cache.pop(user_id, None)


def clear_cache() -> None:
    """Reset the entitlement caches. Useful in tests."""
    _entitlement_cache.clear()
    _user_subscription_cache.clear()


# ── Env-driven dev overrides ──────────────────────────────────────────────────


def _force_premium_enabled() -> bool:
    return os.environ.get("FORCE_PREMIUM", "").strip().lower() in {"1", "true", "yes"}


def _bypass_guild_ids() -> set[int]:
    """Guild IDs that should always be treated as premium, regardless of
    Discord subscription state. Read from the `PREMIUM_BYPASS_GUILD_IDS`
    env var (comma-separated). Returns an empty set if unset.
    """
    raw = os.environ.get("PREMIUM_BYPASS_GUILD_IDS", "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if piece.isdigit():
            out.add(int(piece))
    return out


# ── Assignment layer (thin wrappers around config helpers) ────────────────────
#
# These are the single entry points premium-aware code should use. Cache
# invalidation lives here so callers don't have to remember which guilds
# need clearing on each operation.


def get_assigned_guild(user_id: int) -> Optional[int]:
    """Return the guild_id this user has pinned their license to, or None."""
    from config import get_premium_assignment_for_user

    return get_premium_assignment_for_user(user_id)


def get_assigned_user(guild_id: int) -> Optional[int]:
    """Return the user_id assigned to this guild, or None."""
    from config import get_premium_assignment_for_guild

    return get_premium_assignment_for_guild(guild_id)


def assign(user_id: int, guild_id: int) -> bool:
    """Pin this user's license to `guild_id`.

    Returns True if the assignment was applied, or False if another
    subscriber already holds `guild_id` (race-safe: refuses to silently
    displace). On True, invalidates the premium cache for both the
    user's old guild (if any) and the new guild. On False, no state
    changes — caches untouched.

    Callers in `donate.py` pre-check the guild's holder before calling
    and surface a "race occurred" message to the user on False.
    """
    from config import (
        get_premium_assignment_for_user,
        set_premium_assignment,
    )

    prior_guild = get_premium_assignment_for_user(user_id)
    assigned = set_premium_assignment(user_id, guild_id)
    if not assigned:
        return False
    if prior_guild is not None:
        _cache_invalidate_guild(prior_guild)
    _cache_invalidate_guild(guild_id)
    _user_cache_invalidate(user_id)
    return True


def unassign(user_id: int) -> Optional[int]:
    """Remove this user's assignment. Invalidates the premium cache for
    the freed guild. Returns the guild_id that was freed, or None.
    """
    from config import remove_premium_assignment

    freed_guild = remove_premium_assignment(user_id)
    if freed_guild is not None:
        _cache_invalidate_guild(freed_guild)
    _user_cache_invalidate(user_id)
    return freed_guild


def invalidate_for_user(user_id: int) -> None:
    """Drop cached state for a user (per-user subscription cache + the
    per-guild premium cache for the guild they're assigned to, if any).
    Call this from `on_entitlement_create` / `on_entitlement_delete`
    listeners so the next `is_premium` read picks up the fresh state.
    """
    from config import get_premium_assignment_for_user

    _user_cache_invalidate(user_id)
    assigned = get_premium_assignment_for_user(user_id)
    if assigned is not None:
        _cache_invalidate_guild(assigned)


# ── Public API ────────────────────────────────────────────────────────────────


async def user_has_active_subscription(
    user_id: int,
    bot: Optional[discord.Client] = None,
) -> bool:
    """Return True if this Discord user has an active Premium entitlement.

    Cached per-user with the same TTL as the per-guild cache. Returns
    False if `PREMIUM_SKU_ID` is unset, the bot instance is missing, or
    the API lookup raises — in the last case the failure is intentionally
    not cached so the next call retries (see `_lookup_user_subscription`).
    """
    result = await _lookup_user_subscription(user_id, bot=bot)
    return bool(result)


async def _lookup_user_subscription(
    user_id: int,
    bot: Optional[discord.Client] = None,
) -> Optional[bool]:
    """Internal: return True/False on a definitive answer, None on a
    transient lookup failure. `is_premium` uses None to skip writing the
    guild-level cache so a one-off Discord API error doesn't lock a
    paying customer out of premium for the full 5-minute TTL.
    """
    cached = _user_cache_get(user_id)
    if cached is not None:
        return cached

    if PREMIUM_SKU_ID is None:
        _track_silent_fallback(
            "no_sku",
            "PREMIUM_SKU_ID env var is not set — every guild will resolve to "
            "free tier. Subscriptions cannot be detected until this is "
            "configured.",
        )
        # Return None (transient), not False, matching the bot=None branch
        # below. PREMIUM_SKU_ID is module-level so in practice it won't
        # change at runtime, but treating "we can't check" as transient
        # avoids the cache-poisoning class of bug if config reload or
        # hot-reload ever flips that assumption.
        return None
    if bot is None:
        _track_silent_fallback(
            "no_bot",
            f"user_has_active_subscription called without a bot instance "
            f"(user={user_id}); falling back to free tier. Callers in "
            f"background loops must pass bot=...",
        )
        # Return None (transient), not False. is_premium must NOT cache
        # this answer: a per-guild False cached here would lock the
        # subscriber out of every premium check for the 5-minute TTL.
        return None

    try:
        async for ent in bot.entitlements(
            user=discord.Object(id=user_id),
            skus=[discord.Object(id=PREMIUM_SKU_ID)],
            exclude_ended=True,
        ):
            if _entitlement_matches(ent):
                _user_cache_set(user_id, True)
                return True
    except Exception as exc:
        print(f"[PREMIUM] Failed to fetch entitlements for user {user_id}: {exc}")
        return None  # transient — let the next call retry

    _user_cache_set(user_id, False)
    return False


async def is_premium(
    guild_id: int,
    interaction: Optional[discord.Interaction] = None,
    bot: Optional[discord.Client] = None,
) -> bool:
    """Return True if the given guild has an active premium entitlement.

    Resolution order (first hit wins):
      1. `FORCE_PREMIUM` env var → True for everyone.
      2. `PREMIUM_BYPASS_GUILD_IDS` env var → True if guild_id is in the set.
      3. Cached prior lookup (5-minute TTL).
      4. Look up the assigned user for this guild. No assignment → False.
      5. Verify the assigned user's Discord subscription is still active.
         Result cached.

    `interaction` is no longer consulted for entitlements (under a User
    Subscription SKU, the interaction user may not be the assigned
    subscriber) but its `.client` is used as a fallback when `bot=` is
    omitted, so call sites that only have an interaction still get a
    valid entitlement check instead of degrading to free tier.
    """
    if _force_premium_enabled():
        return True
    if guild_id in _bypass_guild_ids():
        return True

    # Many slash-command call sites pass interaction= but not bot= (the
    # bot kwarg used to be optional and is only consulted now). Without
    # a bot instance the entitlement lookup can't run; pull bot off the
    # interaction so those call sites resolve correctly instead of
    # silently degrading to free tier.
    if bot is None and interaction is not None:
        bot = interaction.client

    cached = _cache_get(guild_id)
    if cached is not None:
        return cached

    from config import get_premium_assignment_for_guild

    try:
        assigned_user = get_premium_assignment_for_guild(guild_id)
    except Exception as exc:
        # Defensive: if init_db hasn't run yet (or the schema is older
        # than this code, or the file is locked), treat the lookup as
        # "no assignment" rather than crashing the caller.
        _track_silent_fallback(
            "no_assignment_table",
            f"Failed to read premium_assignments (guild={guild_id}): {exc}. "
            f"Falling back to free tier; this should self-heal once the "
            f"table exists.",
        )
        return False
    if assigned_user is None:
        _cache_set(guild_id, False)
        return False

    result = await _lookup_user_subscription(assigned_user, bot=bot)
    if result is None:
        # Transient API error — don't cache False or we'd lock the
        # subscriber out for the full 5-minute TTL.
        return False
    _cache_set(guild_id, result)
    return result


def _entitlement_matches(ent) -> bool:
    """True if the entitlement is for the configured premium SKU and active.

    `bot.entitlements(exclude_ended=True)` is supposed to filter ended
    entitlements server-side, but check both `deleted` and `ends_at`
    locally as defense-in-depth — a discord.py change or a Discord API
    quirk shouldn't be the only thing standing between "subscription
    ended" and "guild keeps Premium."
    """
    if PREMIUM_SKU_ID is None:
        return False
    sku_id = getattr(ent, "sku_id", None)
    if sku_id != PREMIUM_SKU_ID:
        return False
    if getattr(ent, "deleted", False):
        return False
    # discord.py 2.4+ exposes ends_at; if set and in the past, the
    # entitlement has ended even if `deleted` is False.
    ends_at = getattr(ent, "ends_at", None)
    if ends_at is not None:
        from datetime import datetime, timezone

        if ends_at < datetime.now(timezone.utc):
            return False
    return True


async def get_limit(
    feature: str,
    guild_id: int,
    interaction: Optional[discord.Interaction] = None,
    bot: Optional[discord.Client] = None,
) -> Optional[int]:
    """Return the per-feature limit for this guild.

    Returns `None` if the feature is unlimited at the resolved tier.
    Raises `KeyError` for unknown features (caller bug).
    """
    if feature not in LIMITS:
        raise KeyError(f"Unknown premium-limit feature: {feature!r}")
    tier = "premium" if await is_premium(guild_id, interaction, bot) else "free"
    return LIMITS[feature][tier]


def is_premium_feature(name: str) -> bool:
    """True if `name` is a fully-gated premium-only feature.

    Cheap predicate that doesn't touch the entitlement cache or the
    assignment table — use `feature_gate(...)` when you actually need
    to know whether a specific guild has access.
    """
    return name in PREMIUM_FEATURES


async def feature_gate(
    feature_name: str,
    guild_id: int,
    *,
    interaction: Optional[discord.Interaction] = None,
    bot: Optional[discord.Client] = None,
) -> bool:
    """Canonical gate for a named premium feature.

    Returns True iff this guild has access to `feature_name`. The name
    must be a member of `PREMIUM_FEATURES`; an unknown name raises
    KeyError. That contract forces newly added premium features to
    register themselves in the set up front, so a missed gate becomes a
    test failure or a crash instead of a silently-free feature.

    Resolution delegates to `is_premium` (same cache, same assignment
    lookup, same env-var short-circuits), so the gate is consistent with
    every other premium check in the codebase.
    """
    if feature_name not in PREMIUM_FEATURES:
        raise KeyError(
            f"Unknown premium feature: {feature_name!r}. "
            f"Add it to PREMIUM_FEATURES in premium.py before gating on it."
        )
    return await is_premium(guild_id, interaction=interaction, bot=bot)


# ── User-facing messaging ─────────────────────────────────────────────────────

PREMIUM_BRAND = "💎 LW Alliance Helper Premium"


def limit_reached_embed(
    *,
    feature_label: str,
    current: int,
    cap: int,
    plural_unit: str = "items",
) -> discord.Embed:
    """Embed shown when a free-tier user hits a count cap.

    `feature_label` is human-readable (e.g. "events"). `plural_unit` is the
    countable noun for the limit message (e.g. "events", "metrics", "themes").
    """
    embed = discord.Embed(
        title="📊 Free tier limit reached",
        description=(
            f"You've used **{current} of {cap}** {plural_unit} on the free tier. "
            f"Upgrade to {PREMIUM_BRAND} to unlock more."
        ),
        color=discord.Color.orange(),
    )
    embed.add_field(
        name=f"This limit applies to: {feature_label}",
        value=(
            "Premium subscribers get expanded limits, plus features like "
            "member roster sync, birthday DMs, and thread destinations. "
            "Run `/upgrade` to unlock it."
        ),
        inline=False,
    )
    return embed


def premium_locked_embed(*, feature_label: str, description: str = "") -> discord.Embed:
    """Embed shown when a free-tier user tries to use a premium-only feature."""
    embed = discord.Embed(
        title=f"🔒 {feature_label} is a Premium feature",
        description=(
            description or f"This feature is part of {PREMIUM_BRAND}. Run `/upgrade` to unlock it."
        ),
        color=discord.Color.purple(),
    )
    return embed


def upgrade_view() -> Optional[discord.ui.View]:
    """A View containing Discord's native premium-upgrade button.

    Returns None if no SKU is configured, so callers can fall back to a
    text-only message ("Run `/upgrade`...") gracefully.
    """
    if PREMIUM_SKU_ID is None:
        return None
    view = discord.ui.View(timeout=None)
    view.add_item(
        discord.ui.Button(
            style=discord.ButtonStyle.premium,
            sku_id=PREMIUM_SKU_ID,
        )
    )
    return view
