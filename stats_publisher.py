"""Publish the live alliance count to the public website.

The static GitHub Pages site at https://lw-alliance-helper.github.io
shows a "Powering N alliances" badge on the home page. The badge reads
from `assets/stats.json` in the website's repo. This module updates
that file via GitHub's Contents REST API so the bot doesn't need a git
clone on disk (Railway's filesystem is ephemeral and a clone wouldn't
survive redeploys).

How it works:
- `publish_alliance_count(count)` first GETs the current file. If it
  already shows the same count, no commit is made — keeps repo history
  clean.
- Otherwise it PUTs new content with a brief commit message.
- Both calls retry a 5xx before giving up. GitHub sheds load often
  enough that a one-shot publish was reporting its own retryable blips
  as errors (#382 / #383).
- All errors are caught and logged. A publish failure must NEVER take
  down the bot.

Env var:
    STATS_GITHUB_TOKEN — a fine-grained GitHub Personal Access Token
        scoped to *only* the lw-alliance-helper.github.io repo, with
        Contents: Read and Write permission. If unset, the publisher
        logs once and no-ops — useful for local dev.

The repo + path are constants below. They aren't expected to change;
if the website repo ever moves, update STATS_REPO.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
from datetime import datetime, timezone
from typing import Optional, Union

import aiohttp


# Hard-coded because these are stable for the life of the project.
# If the website repo or layout ever changes, edit them here.
STATS_REPO = "LW-Alliance-Helper/lw-alliance-helper.github.io"
STATS_PATH = "assets/stats.json"
STATS_BRANCH = "main"

# GitHub's recommended UA + accept headers for Contents API.
_GITHUB_API = "https://api.github.com"
_HEADERS_BASE = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "lw-alliance-helper-bot/1.0",
}

# The committer GitHub will attribute the commit to. Anything is fine
# here — just a label in the website repo's history.
_COMMITTER = {
    "name": "LW Alliance Helper Bot",
    "email": "bot@lw-alliance-helper.invalid",
}


# GitHub sheds load with a 503 often enough that a single-shot publish turned
# a passing blip into a Sentry alert and an auto-filed GitHub issue (#382 /
# #383). These statuses are retried instead; only an exhausted retry is worth
# telling anyone about.
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})

# Three attempts over ~7s. The publisher runs from a background task on a
# count change, so waiting is free, but it shares the bot's event loop and
# must not sit on it for minutes.
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (2.0, 5.0)


class _Unknown:
    """Sentinel for "the read failed", distinct from "the file isn't there"."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UNKNOWN"


UNKNOWN = _Unknown()


def _token() -> Optional[str]:
    return os.getenv("STATS_GITHUB_TOKEN")


def _auth_headers(token: str) -> dict:
    return {**_HEADERS_BASE, "Authorization": f"Bearer {token}"}


async def _sleep_before_retry(attempt: int) -> None:
    """Back off before attempt number ``attempt`` (1-based, so never called with 1)."""
    delay = _RETRY_BACKOFF_SECONDS[min(attempt - 2, len(_RETRY_BACKOFF_SECONDS) - 1)]
    await asyncio.sleep(delay)


def _capture_publish_failure(message: str) -> None:
    """Report a publish failure that outlived its retries. Never raises."""
    try:
        import sentry_sdk

        sentry_sdk.capture_message(message, level="error")
    except Exception:
        pass


async def _fetch_current(
    session: aiohttp.ClientSession, token: str
) -> tuple[Union[dict, _Unknown, None], Optional[str]]:
    """Return (parsed_json, sha) for the existing file.

    ``(None, None)`` means the file genuinely isn't there yet, so a PUT
    without a sha is the right next move. ``(UNKNOWN, None)`` means the read
    failed and we don't know what's on the branch; the caller has to skip the
    run, because GitHub rejects a sha-less PUT over an existing file with a
    422 and the publish would fail anyway. A malformed body is neither: the
    file is there and its sha is good, so the caller should overwrite it.
    """
    url = f"{_GITHUB_API}/repos/{STATS_REPO}/contents/{STATS_PATH}?ref={STATS_BRANCH}"
    payload = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if attempt > 1:
            await _sleep_before_retry(attempt)
        try:
            async with session.get(
                url, headers=_auth_headers(token), timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 404:
                    return (None, None)
                if resp.status in _TRANSIENT_STATUSES and attempt < _MAX_ATTEMPTS:
                    print(
                        f"[STATS] GET stats.json returned {resp.status}, "
                        f"retrying (attempt {attempt}/{_MAX_ATTEMPTS})."
                    )
                    continue
                if resp.status != 200:
                    body = await resp.text()
                    print(f"[STATS] Unexpected GET status {resp.status}: {body[:200]}")
                    return (UNKNOWN, None)
                payload = await resp.json()
                break
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < _MAX_ATTEMPTS:
                print(
                    f"[STATS] GET stats.json failed ({e}), "
                    f"retrying (attempt {attempt}/{_MAX_ATTEMPTS})."
                )
                continue
            print(f"[STATS] GET stats.json failed: {e}")
            return (UNKNOWN, None)

    if payload is None:
        return (UNKNOWN, None)

    sha = payload.get("sha")
    encoded = payload.get("content", "")
    try:
        raw = base64.b64decode(encoded).decode("utf-8")
        return (json.loads(raw), sha)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"[STATS] Could not decode existing stats.json: {e}")
        return (None, sha)


async def _put_new(
    session: aiohttp.ClientSession,
    token: str,
    content: dict,
    sha: Optional[str],
    message: str,
) -> bool:
    """PUT the new content. Returns True on success, False otherwise."""
    url = f"{_GITHUB_API}/repos/{STATS_REPO}/contents/{STATS_PATH}"
    payload = {
        "message": message,
        "content": base64.b64encode(json.dumps(content, indent=2).encode("utf-8") + b"\n").decode(
            "ascii"
        ),
        "branch": STATS_BRANCH,
        "committer": _COMMITTER,
    }
    if sha:
        payload["sha"] = sha

    for attempt in range(1, _MAX_ATTEMPTS + 1):
        if attempt > 1:
            await _sleep_before_retry(attempt)
        try:
            async with session.put(
                url,
                headers=_auth_headers(token),
                json=payload,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status in (200, 201):
                    return True
                if resp.status in _TRANSIENT_STATUSES and attempt < _MAX_ATTEMPTS:
                    print(
                        f"[STATS] PUT stats.json returned {resp.status}, "
                        f"retrying (attempt {attempt}/{_MAX_ATTEMPTS})."
                    )
                    continue
                body = await resp.text()
                print(f"[STATS] PUT failed ({resp.status}): {body[:300]}")
                # 401/403/404 mean the PAT is expired, missing scope, or the
                # repo moved — every daily run will keep failing silently
                # until someone tails Railway logs at the right moment.
                # Sentry-capture so it surfaces in email instead.
                #
                # A 5xx only gets here once the retries above are spent, which
                # means GitHub has been down across the whole backoff rather
                # than shedding one request (#382 / #383).
                if resp.status in (401, 403, 404) or resp.status >= 500:
                    _capture_publish_failure(
                        f"[STATS] PUT stats.json returned {resp.status} after "
                        f"{attempt} attempt(s): {body[:300]}"
                    )
                return False
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            if attempt < _MAX_ATTEMPTS:
                print(
                    f"[STATS] PUT stats.json failed ({e}), "
                    f"retrying (attempt {attempt}/{_MAX_ATTEMPTS})."
                )
                continue
            print(f"[STATS] PUT stats.json failed: {e}")
            return False
    return False


async def publish_alliance_count(count: int) -> None:
    """Push `{"alliances": count, "updated_utc": ...}` to the website
    repo's stats.json — but only if the count has actually changed.

    Errors are swallowed and logged. This must never raise into the
    bot's event loop.
    """
    token = _token()
    if not token:
        print("[STATS] STATS_GITHUB_TOKEN not set — skipping alliance-count publish.")
        return

    new_payload = {
        "alliances": int(count),
        "updated_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds"),
    }

    async with aiohttp.ClientSession() as session:
        existing, sha = await _fetch_current(session, token)
        if existing is UNKNOWN:
            # Without the sha, a PUT over the existing file is a 422. Skip and
            # let the next count change try again rather than burning a write.
            print("[STATS] Could not read the current stats.json — skipping this publish.")
            return
        if existing is not None and existing.get("alliances") == new_payload["alliances"]:
            print(f"[STATS] Alliance count unchanged ({count}) — no commit.")
            return

        message = f"Update alliance count to {count}"
        ok = await _put_new(session, token, new_payload, sha, message)
        if ok:
            print(f"[STATS] Published alliance count = {count} to {STATS_REPO}/{STATS_PATH}")
        else:
            print("[STATS] Failed to publish alliance count.")
