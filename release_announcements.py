"""Release-announcement embeds posted to each guild's leadership channel
on a major/minor version bump (#253).

Wired into `on_ready` after the `guild_install_metadata` upsert. For each
guild we compare the stored `last_seen_version` to the current
`__version__` by major.minor portion. If they differ AND the guild
hasn't opted out AND a `RELEASE_ANNOUNCEMENTS` entry exists for the
current version, we post the embed and update `last_seen_version`.

Patch releases (e.g. 1.4.1, 1.5.3) never trigger — they share the
major.minor of the previous release.

Content authoring: each major/minor release fills in the
`RELEASE_ANNOUNCEMENTS[version]` entry as part of release-PR prep,
alongside the CHANGELOG entry. The dict starts empty for 1.3.4 itself.
"""
from __future__ import annotations

from dataclasses import dataclass
import logging

import discord
import sentry_sdk

from config import get_config, set_last_seen_version, get_guild_install_metadata

log = logging.getLogger(__name__)


# Warm gold (#F1C40F). Workshopped with Kevin 2026-05-24 — celebratory
# without being shouty, pairs cleanly with 💎 on Premium bullets.
EMBED_COLOR = 0xF1C40F


@dataclass
class ReleaseAnnouncement:
    """The dict-entry shape for a single release's announcement copy."""
    description: str
    bullets: list[str]
    support_post_url: str
    github_release_url: str


# Keyed by version string ("1.4.0", "1.5.0", "2.0.0"). Entries are added
# during release-PR prep — see CLAUDE.md "Versioning is per-release" for
# the workflow. Missing entries are a noop (the boot-time check skips
# silently), so it's safe to ship a release without one if there's no
# user-visible content worth announcing.
RELEASE_ANNOUNCEMENTS: dict[str, ReleaseAnnouncement] = {
    "1.4.0": ReleaseAnnouncement(
        description=(
            "We're excited to share that 1.4.0 is here, our biggest release "
            "since launch. Here are some highlights of what your alliance "
            "can do starting now:"
        ),
        bullets=[
            "A roster builder for Desert Storm and Canyon Storm with auto-fill (💎 Premium)",
            "A visual roster you can share with your team (💎 Premium)",
            "DM each rostered member their personal assignment (💎 Premium)",
            "Participation logs where you decide what data to collect (💎 Premium)",
            "All your storm tools under `/desertstorm` and `/canyonstorm`",
            "All your bot settings under `/setup` with a button for each feature",
        ],
        # Links to the support server's #announcements channel rather than a
        # specific message, so the URL stays valid as new announcements get
        # posted there over time.
        support_post_url="https://discord.com/channels/1497432945827516639/1502745629217263746",
        github_release_url="https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/releases/tag/v1.4.0",
    ),
}


def _major_minor(version: str) -> str:
    """Return the `X.Y` portion of a `X.Y.Z` version string. Returns the
    input unchanged if it doesn't parse — we'd rather not crash on a
    malformed stored value than skip the safety check."""
    parts = version.strip().split(".")
    if len(parts) < 2:
        return version
    return f"{parts[0]}.{parts[1]}"


def should_announce(stored_version: str, current_version: str) -> bool:
    """True iff a major/minor change is detected between the two
    versions. Patch bumps and identical versions return False. Empty /
    missing `stored_version` is treated as "skip" — fresh installs are
    expected to have `last_seen_version` populated by
    `upsert_guild_install_metadata(current_version=...)` on first
    sighting, so an empty value here usually means a partial migration
    state we'd rather not spam on top of."""
    if not stored_version:
        return False
    return _major_minor(stored_version) != _major_minor(current_version)


def build_embed(version: str, bot_user: discord.User) -> discord.Embed | None:
    """Render the release-announcement embed for `version`. Returns None
    if the version has no `RELEASE_ANNOUNCEMENTS` entry."""
    entry = RELEASE_ANNOUNCEMENTS.get(version)
    if entry is None:
        return None

    embed = discord.Embed(
        title=f"🎉 Introducing version {version} of LW Alliance Helper",
        url=entry.support_post_url or None,
        description=entry.description,
        color=EMBED_COLOR,
    )
    if bot_user is not None:
        avatar = getattr(bot_user, "display_avatar", None)
        if avatar is not None and avatar.url:
            embed.set_thumbnail(url=avatar.url)
    embed.add_field(
        name="⚔️ What's new",
        value="\n".join(f"• {b}" for b in entry.bullets),
        inline=False,
    )
    embed.add_field(
        name="📖 Read more",
        value=(
            f"You can read the [full announcement]({entry.support_post_url}) "
            f"or [release notes]({entry.github_release_url}) to see all the "
            f"details."
        ),
        inline=False,
    )
    # Footer doesn't render markdown — no backticks around /setup.
    embed.set_footer(
        text=(
            "If you don't want to see release announcements here, you can "
            "turn them off by going to /setup → 📢 Release announcements."
        )
    )
    return embed


async def maybe_post_release_announcement(
    guild: discord.Guild,
    bot: discord.Client,
    current_version: str,
) -> None:
    """Per-guild entry point called from `on_ready`. Compares stored
    version to current, posts the embed to the leadership channel if a
    major/minor change is detected and the guild hasn't opted out, then
    bumps `last_seen_version`.

    Swallows all per-guild failures — a missing channel, a perms error,
    or a stray exception in one guild must not abort the on_ready loop
    for the remaining guilds. Forbidden gets a non-error breadcrumb to
    Sentry (perms drift is the alliance's responsibility, not a bug);
    other exceptions are captured as exceptions.
    """
    try:
        meta = get_guild_install_metadata(guild.id)
        if meta is None:
            return  # No metadata row yet — upsert hasn't run for this guild.
        stored = meta.get("last_seen_version") or ""
        if not should_announce(stored, current_version):
            return

        if RELEASE_ANNOUNCEMENTS.get(current_version) is None:
            # No content authored for this release. Still record that the
            # guild has seen this version so we don't re-evaluate every
            # boot — patch releases past this won't trigger anyway, but
            # the next minor that *does* author content will.
            set_last_seen_version(guild.id, current_version)
            return

        cfg = get_config(guild.id)
        if cfg is None or not cfg.release_announcements_enabled:
            # Opted out — bump the stored version so we don't reconsider
            # this guild every boot until the next major/minor.
            set_last_seen_version(guild.id, current_version)
            return
        if not cfg.leadership_channel_id:
            # Setup never completed (no leadership channel chosen). Don't
            # update last_seen_version — once they finish setup we want
            # the next boot to deliver the announcement they missed.
            return

        channel = guild.get_channel(cfg.leadership_channel_id)
        if channel is None:
            # Channel deleted or bot can't see it. Same logic as missing
            # leadership_channel_id — don't bump, let the next boot retry
            # once they fix the channel.
            return

        embed = build_embed(current_version, bot.user)
        if embed is None:
            set_last_seen_version(guild.id, current_version)
            return

        await channel.send(embed=embed)
        set_last_seen_version(guild.id, current_version)
        log.info(
            "[RELEASE-ANNOUNCE] Posted %s announcement to guild %s (%s)",
            current_version, guild.id, guild.name,
        )

    except discord.Forbidden:
        # Bot lost permission to post in the leadership channel. Not a
        # bug, not worth crashing. Breadcrumb to Sentry so we can spot
        # if it's widespread. Don't bump last_seen_version — once perms
        # come back, the next boot delivers the missed announcement.
        sentry_sdk.add_breadcrumb(
            category="release_announce",
            message=f"Forbidden posting to leadership channel for guild {guild.id}",
            level="info",
        )
    except Exception as e:
        log.warning(
            "[RELEASE-ANNOUNCE] Failed for guild %s: %s",
            guild.id, e,
        )
        sentry_sdk.capture_exception(e)
