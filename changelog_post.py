"""Post each release's changelog entry to the support server's #changelog.

The bot does this itself on boot rather than a GitHub Action firing a
webhook (#92), for three reasons:

* **It's true when it posts.** The release workflow runs on merge to
  main, before Railway has finished deploying. A webhook there announces
  a release that might still fail to deploy. The bot can only say "1.8.5
  is out" while actually running 1.8.5.
* **State is durable.** Appending to a running message needs the message
  id to survive between releases. In CI that meant the Actions cache,
  which is evictable; here it's SQLite, same as everything else.
* **No shared secret.** No webhook URL to store in GitHub, leak, or
  rotate, and posts come from the bot rather than a second identity.

The post itself is authored by hand in `docs/DISCORD_CHANGELOG.md` on the
release branch — see that file's preamble. `release-changelog-check.yml`
fails the release PR when a version has no block, which is what makes
"every release posts" true rather than aspirational.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import discord
import sentry_sdk

log = logging.getLogger(__name__)

CHANGELOG_PATH = Path(__file__).with_name("docs") / "DISCORD_CHANGELOG.md"

# Discord hard-caps message content at 2000 characters.
DISCORD_CONTENT_LIMIT = 2000

# Opts a release out of posting. Every release posts unless its block
# says this, so a missing block means "forgotten" and gets caught by the
# release-PR check rather than discovered as a quiet channel.
NO_POST_MARKER = "NO POST"

# How long after a post a following release still joins its message.
# Over half this project's releases land within 24h of the previous one
# and the closest pair was 13 minutes apart, so a burst of hotfixes would
# otherwise be a burst of notifications.
BURST_WINDOW_SECONDS = 12 * 3600

# app_settings keys. Bot-global, not per guild — there is one support
# server and one changelog channel.
CHANNEL_SETTING = "changelog_channel_id"
LAST_VERSION_SETTING = "changelog_last_version"
LAST_MESSAGE_SETTING = "changelog_last_message_id"
LAST_POSTED_AT_SETTING = "changelog_last_posted_at"


# ── Pure helpers (shared with scripts/discord_changelog.py) ──────────────


def find_block(content: str, version: str) -> str | None:
    """Return the post whose header names `version`, or None.

    Blocks are separated by a bare `---`. The file's preamble is the first
    block and doesn't start with `**`, so it's skipped along with anything
    else that isn't a version header.

    Matching is literal, so a legacy range header (`**1.7.1 to 1.7.4**`)
    resolves for the two versions it names and not the ones between them.
    """
    if not version:
        return None
    for block in re.split(r"^---\s*$", content, flags=re.M):
        block = block.strip()
        if not block or not block.startswith("**"):
            continue
        header = block.splitlines()[0]
        # Guard against 1.5.1 matching inside 1.5.10.
        if re.search(rf"(?<![\d.]){re.escape(version)}(?![\d.])", header):
            return block
    return None


def is_no_post(block: str) -> bool:
    """True when a block exists purely to record "this one doesn't post"."""
    return any(
        line.strip().upper().startswith(NO_POST_MARKER) for line in (block or "").splitlines()[1:]
    )


def plan_post(
    block: str | None,
    state: dict | None,
    *,
    now: float,
    window_seconds: float = BURST_WINDOW_SECONDS,
    limit: int = DISCORD_CONTENT_LIMIT,
) -> dict:
    """Decide whether this release starts a new message or joins the last.

    Returns `{"action": "none"|"post"|"edit", "content": str,
    "message_id": str}`. Anything making an append unsafe — no stored
    message, window elapsed, combined text over the limit — falls back to
    a new message. The fallback is always to say something twice, never
    to say nothing.
    """
    if not block or is_no_post(block):
        return {"action": "none", "content": "", "message_id": ""}

    message_id = (state or {}).get("message_id") or ""
    posted_at = (state or {}).get("posted_at") or 0
    previous = (state or {}).get("content") or ""

    if message_id and previous and 0 <= (now - posted_at) <= window_seconds:
        combined = f"{previous}\n\n{block}"
        if len(combined) <= limit:
            return {"action": "edit", "content": combined, "message_id": message_id}

    return {"action": "post", "content": block, "message_id": ""}


def load_block(version: str, path: Path | None = None) -> str | None:
    """Read the version's block off disk. None when the file or block is
    missing, or the block is too long for one Discord message."""
    try:
        content = (path or CHANGELOG_PATH).read_text(encoding="utf-8")
    except OSError as e:
        log.warning("[CHANGELOG] Could not read %s: %s", path or CHANGELOG_PATH, e)
        return None

    block = find_block(content, version)
    if block is not None and not is_no_post(block) and len(block) > DISCORD_CONTENT_LIMIT:
        log.warning(
            "[CHANGELOG] Block for %s is %d chars, over Discord's %d limit — not posting",
            version,
            len(block),
            DISCORD_CONTENT_LIMIT,
        )
        return None
    return block


# ── Bot-side posting ────────────────────────────────────────────────────


def _state() -> dict:
    from config import get_app_setting

    try:
        posted_at = float(get_app_setting(LAST_POSTED_AT_SETTING) or 0)
    except (TypeError, ValueError):
        posted_at = 0.0
    return {
        "message_id": get_app_setting(LAST_MESSAGE_SETTING) or "",
        "posted_at": posted_at,
        "content": get_app_setting("changelog_last_content") or "",
    }


def _remember(version: str, message_id: str, content: str) -> None:
    from config import set_app_setting

    set_app_setting(LAST_VERSION_SETTING, version)
    if message_id:
        set_app_setting(LAST_MESSAGE_SETTING, message_id)
        set_app_setting(LAST_POSTED_AT_SETTING, str(time.time()))
        set_app_setting("changelog_last_content", content)


async def maybe_post_changelog(bot, version: str, *, force: bool = False) -> str:
    """Post `version`'s changelog entry if it hasn't been posted already.

    Called from `on_ready`, which fires again on every gateway reconnect
    and every Railway redeploy. `changelog_last_version` is what stops
    that becoming a repost each time — the same restart-safety trap that
    made birthday auto-population re-fire on every deploy (#29).

    Returns a short status string for logging and the `/admin` surface.
    """
    from config import get_app_setting, set_app_setting

    raw_channel = get_app_setting(CHANNEL_SETTING)
    if not raw_channel:
        return "no changelog channel configured"

    if not force and (get_app_setting(LAST_VERSION_SETTING) or "") == version:
        return f"{version} already posted"

    block = load_block(version)
    if block is None:
        # Record it anyway. Retrying every restart would just log the same
        # complaint forever, and the release-PR check is what's meant to
        # catch a missing block.
        set_app_setting(LAST_VERSION_SETTING, version)
        return f"no usable block for {version}"

    if is_no_post(block):
        set_app_setting(LAST_VERSION_SETTING, version)
        return f"{version} is marked {NO_POST_MARKER}"

    channel = bot.get_channel(int(raw_channel))
    if channel is None:
        log.warning("[CHANGELOG] Channel %s not found or not visible", raw_channel)
        return "changelog channel not found"

    # Passed explicitly rather than leaning on plan_post's default, which
    # binds at import and so can't be adjusted at runtime or in a test.
    plan = plan_post(block, _state(), now=time.time(), window_seconds=BURST_WINDOW_SECONDS)

    if plan["action"] == "edit":
        try:
            message = await channel.fetch_message(int(plan["message_id"]))
            await message.edit(content=plan["content"])
            _remember(version, plan["message_id"], plan["content"])
            return f"appended {version} to message {plan['message_id']}"
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            # Deleted, or we lost permission to edit it. Starting a new
            # message is the right fallback: a duplicate beats silence.
            log.info("[CHANGELOG] Could not edit %s (%s) — posting fresh", plan["message_id"], e)
            plan = {"action": "post", "content": block, "message_id": ""}

    try:
        message = await channel.send(plan["content"])
    except discord.Forbidden:
        log.warning("[CHANGELOG] Missing permission to post in %s", raw_channel)
        return "cannot post in the changelog channel"
    except discord.HTTPException as e:
        log.warning("[CHANGELOG] Post failed: %s", e)
        sentry_sdk.capture_exception(e)
        return f"post failed: {e}"

    _remember(version, str(message.id), plan["content"])
    return f"posted {version}"
