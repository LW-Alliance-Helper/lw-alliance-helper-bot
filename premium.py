"""
premium.py — Premium tier entitlement checks, feature limits, and UX helpers.

The bot has two tiers: free and premium. Premium is sold via Discord App
Subscriptions (SKU configured in the Discord Developer Portal). Entitlements
are read from `interaction.entitlements` when an interaction is available
(zero-cost) and from `bot.entitlements()` otherwise (cached for 5 minutes).

For development and the bot owner's home alliance, several short-circuits
are available:
  - `ALWAYS_PREMIUM_GUILD_IDS` (hardcoded — includes OGV)
  - `FORCE_PREMIUM=1`           (env var — flags every guild as premium)
  - `PREMIUM_TEST_GUILD_IDS`    (env var — comma-separated guild ids)

Public API:
  - `is_premium(guild_id, interaction=None, bot=None)` → bool
  - `get_limit(feature, guild_id, ...)`               → int | None  (None = unlimited)
  - `is_premium_feature(name)`                        → bool        (declarative whitelist)
  - `limit_reached_embed(...)`, `premium_locked_embed(...)`, `upgrade_view(...)`
"""

import os
import time
from typing import Optional

import discord

from config import OGV_GUILD_ID


# ── Configuration ─────────────────────────────────────────────────────────────

# Guilds that are always premium, regardless of subscription status.
# OGV is the bot owner's home alliance — they get full access.
ALWAYS_PREMIUM_GUILD_IDS: set[int] = {OGV_GUILD_ID}

# Discord SKU ID for the premium subscription (set in Discord Developer Portal).
# Until this is set, no real subscriptions can be detected — only the
# always-premium and dev-override guilds are treated as premium.
PREMIUM_SKU_ID: Optional[int] = (
    int(os.environ["PREMIUM_SKU_ID"])
    if os.environ.get("PREMIUM_SKU_ID", "").strip().isdigit()
    else None
)

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


def clear_cache() -> None:
    """Reset the entitlement cache. Useful in tests."""
    _entitlement_cache.clear()


# ── Env-driven dev overrides ──────────────────────────────────────────────────

def _force_premium_enabled() -> bool:
    return os.environ.get("FORCE_PREMIUM", "").strip().lower() in {"1", "true", "yes"}


def _test_guild_ids() -> set[int]:
    raw = os.environ.get("PREMIUM_TEST_GUILD_IDS", "").strip()
    if not raw:
        return set()
    out: set[int] = set()
    for piece in raw.split(","):
        piece = piece.strip()
        if piece.isdigit():
            out.add(int(piece))
    return out


# ── Public API ────────────────────────────────────────────────────────────────

async def is_premium(
    guild_id: int,
    interaction: Optional[discord.Interaction] = None,
    bot: Optional[discord.Client] = None,
) -> bool:
    """Return True if the given guild has an active premium entitlement.

    Resolution order (first hit wins):
      1. `FORCE_PREMIUM` env var → True for everyone.
      2. `ALWAYS_PREMIUM_GUILD_IDS` hardcoded list → True.
      3. `PREMIUM_TEST_GUILD_IDS` env var → True.
      4. `interaction.entitlements` if an interaction is supplied → True if a
         non-deleted entitlement matches PREMIUM_SKU_ID.
      5. Cached prior lookup (5-minute TTL).
      6. `bot.entitlements()` API call → True if a non-ended entitlement
         matches PREMIUM_SKU_ID. Result cached.
      7. Otherwise False.
    """
    if _force_premium_enabled():
        return True
    if guild_id in ALWAYS_PREMIUM_GUILD_IDS:
        return True
    if guild_id in _test_guild_ids():
        return True

    # Cheap path: the interaction already carries entitlements.
    if interaction is not None and PREMIUM_SKU_ID is not None:
        for ent in getattr(interaction, "entitlements", []) or []:
            if _entitlement_matches(ent):
                _cache_set(guild_id, True)
                return True

    # Cache hit — avoid an API call.
    cached = _cache_get(guild_id)
    if cached is not None:
        return cached

    # Cache miss — query Discord. Skipped silently if no SKU configured
    # or no bot instance available.
    if PREMIUM_SKU_ID is None or bot is None:
        _cache_set(guild_id, False)
        return False

    try:
        async for ent in bot.entitlements(
            guild=discord.Object(id=guild_id),
            sku_ids=[discord.Object(id=PREMIUM_SKU_ID)],
            exclude_ended=True,
        ):
            if _entitlement_matches(ent):
                _cache_set(guild_id, True)
                return True
    except Exception as exc:
        print(f"[PREMIUM] Failed to fetch entitlements for guild {guild_id}: {exc}")

    _cache_set(guild_id, False)
    return False


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

PREMIUM_BRAND = "💎 Alliance Helper Premium"


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
