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

# Once-per-process flags so the silent-fallback warning doesn't spam every
# minute (premium check fires on most slash commands and inside background
# loops). Reset on process restart, which is the right cadence to detect a
# regression introduced by a deploy.
_warned_no_sku = False
_warned_no_bot = False
_warned_no_assignment_table = False

# Per-feature limits. None means unlimited.
LIMITS: dict[str, dict[str, Optional[int]]] = {
    "events":             {"free": 5,  "premium": None},
    "themes":             {"free": 3,  "premium": None},
    "tones":              {"free": 3,  "premium": None},
    "train_templates":    {"free": 1,  "premium": 10},
    "storm_templates":    {"free": 1,  "premium": 10},
    "storm_log_recent":   {"free": 4,  "premium": None},
    "survey_questions":   {"free": 5,  "premium": None},
    "growth_metrics":     {"free": 5,  "premium": None},
    "events_log_days":    {"free": 7,  "premium": 30},
    "train_log_days":     {"free": 7,  "premium": 30},
}

# Premium-only features (boolean gates, not counts).
PREMIUM_FEATURES: set[str] = {
    "member_sync",
    "birthday_dm",
    "train_dm",
    "survey_reminder_dm",
    "auto_mention",
    "storm_participation_dm",
    "growth_custom_interval",
    "survey_numeric",
    "survey_multi_select",
    "survey_date",
    "multiple_surveys",
    "thread_destinations",
}


# ── Entitlement cache ─────────────────────────────────────────────────────────

_CACHE_TTL_SECONDS = 300  # 5 minutes
_entitlement_cache: dict[int, tuple[bool, float]] = {}
# Per-user subscription cache. Discord's User Subscription SKU returns the
# same answer regardless of which guild we're querying for, so caching by
# user_id avoids re-fetching when the same subscriber is checked across
# multiple guilds (or when /premium_status and /premium_assign run in quick
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


def assign(user_id: int, guild_id: int) -> Optional[int]:
    """Pin this user's license to `guild_id`. Invalidates the premium cache
    for the old guild (if any) and the new guild. Returns the user_id of a
    prior subscriber whose claim on this guild was displaced, or None.
    """
    from config import (
        get_premium_assignment_for_user,
        set_premium_assignment,
    )
    prior_guild = get_premium_assignment_for_user(user_id)
    displaced_user = set_premium_assignment(user_id, guild_id)
    if prior_guild is not None:
        _cache_invalidate_guild(prior_guild)
    _cache_invalidate_guild(guild_id)
    _user_cache_invalidate(user_id)
    return displaced_user


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
        global _warned_no_sku
        if not _warned_no_sku:
            print("[PREMIUM] PREMIUM_SKU_ID env var is not set — every guild "
                  "will resolve to free tier. Subscriptions cannot be detected "
                  "until this is configured.")
            _warned_no_sku = True
        return False
    if bot is None:
        global _warned_no_bot
        if not _warned_no_bot:
            print(f"[PREMIUM] user_has_active_subscription called without a "
                  f"bot instance (user={user_id}); falling back to free "
                  f"tier. Callers in background loops must pass bot=...")
            _warned_no_bot = True
        return False

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

    `interaction` is accepted for backwards compatibility but no longer
    used as a cheap path: with a User Subscription SKU, the interaction
    user may not be the assigned subscriber (a different user could be
    keeping this guild premium).
    """
    if _force_premium_enabled():
        return True
    if guild_id in _bypass_guild_ids():
        return True

    cached = _cache_get(guild_id)
    if cached is not None:
        return cached

    from config import get_premium_assignment_for_guild
    try:
        assigned_user = get_premium_assignment_for_guild(guild_id)
    except Exception as exc:
        # Defensive: if init_db hasn't run yet (or the schema is older
        # than this code, or the file is locked), treat the lookup as
        # "no assignment" rather than crashing the caller. Logged once
        # per process via the warn flag below.
        global _warned_no_assignment_table
        if not _warned_no_assignment_table:
            print(f"[PREMIUM] Failed to read premium_assignments "
                  f"(guild={guild_id}): {exc}. Falling back to free tier "
                  f"for now; this should self-heal once the table exists.")
            _warned_no_assignment_table = True
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
    """True if the entitlement is for the configured premium SKU and active."""
    if PREMIUM_SKU_ID is None:
        return False
    sku_id = getattr(ent, "sku_id", None)
    if sku_id != PREMIUM_SKU_ID:
        return False
    # discord.py uses `deleted` and (since 2.4) `ends_at` to indicate state
    if getattr(ent, "deleted", False):
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
    """True if `name` is a fully-gated premium-only feature."""
    return name in PREMIUM_FEATURES


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
        title=f"📊 Free tier limit reached",
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
            "Run `/upgrade` to subscribe."
        ),
        inline=False,
    )
    return embed


def premium_locked_embed(*, feature_label: str, description: str = "") -> discord.Embed:
    """Embed shown when a free-tier user tries to use a premium-only feature."""
    embed = discord.Embed(
        title=f"🔒 {feature_label} is a Premium feature",
        description=(
            description
            or f"This feature is part of {PREMIUM_BRAND}. "
               "Run `/upgrade` to unlock it for your alliance."
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
    view.add_item(discord.ui.Button(
        style=discord.ButtonStyle.premium,
        sku_id=PREMIUM_SKU_ID,
    ))
    return view
