"""
shiny_tasks.py — Daily Shiny Tasks announcement support

Last War "shiny tasks" (bonus plunderable tasks) light up on a subset of
servers each day, on a 3-day cycle anchored by each server's launch date.
This module owns:

  * fetching the canonical server list from cpt-hedge.com (the same
    source alliance leadership reads manually today)
  * the 3-day cycle date math
  * rendering today's announcement copy

The per-guild scheduler loop in `bot.py` (`shiny_tasks_post_task`) drives
the daily post; the weekly refresh loop (`shiny_tasks_refresh_task`)
keeps `shiny_task_servers` current with new servers and soft-deletes
servers that have disappeared from Hedge's table.

Data source: see `docs/hedge_data_source.md` for the chunk-URL +
regex-parse strategy. Hedge's server table is client-rendered from data
embedded in a Next.js page chunk, not a JSON API.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import aiohttp


# Last War's in-game day rolls over at 00:00 server time (UTC-2, no DST), so the
# launch *date* that anchors the 3-day shiny cycle is the server-time date of
# the creation timestamp — not the UTC date. A server launched at, say, 00:30
# UTC is already on the previous server-time day; using the UTC date would put
# it one day late in the cycle (#331 — same off-by-one class as #318/#330).
_SERVER_TZ = timezone(timedelta(hours=-2))


def _creation_date_from_ms(ts_ms: int) -> date:
    """Server-time (UTC-2) calendar date of a unix-millisecond launch timestamp."""
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=_SERVER_TZ).date()


HEDGE_BASE_URL = "https://cpt-hedge.com"
HEDGE_SERVERS_PATH = "/servers"
# Browser User-Agent is mandatory — cpt-hedge sits behind Cloudflare
# which 403s the default aiohttp UA. Pinning a real browser string
# (rather than something that screams "bot") keeps us in the polite-
# scraper lane until Hedge ever publishes a real API.
HEDGE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
HEDGE_FETCH_TIMEOUT_S = 30

# Regex to pull `/_next/static/chunks/app/servers/page-<hash>.js` out
# of the HTML. The hash rotates on every Hedge deploy so it must be
# extracted at fetch time, not hardcoded.
_PAGE_CHUNK_RE = re.compile(r"/_next/static/chunks/app/servers/page-[0-9a-f]+\.js")

# Each server record looks like:
#   {"id":"10","server":"State#10","timestamp":"1694157329000",
#    "seasonStartTimestamps":{...},"currentSeason":6,
#    "isPostSeason":false,"currentWeek":5,
#    "updatedAt":1776087064698,"region":["global"]}
# `timestamp` is a quoted string of unix-ms; `region` is a list of one
# string. We grab the three fields we need with a single regex per
# record to stay robust against ordering changes in unrelated fields.
_SERVER_RECORD_RE = re.compile(
    r'"id":"(?P<id>\d+)"'
    r',"server":"State#\d+"'
    r',"timestamp":"(?P<ts>\d+)"'
    r".*?"
    r'"region":\[(?P<region>[^\]]*)\]',
    re.DOTALL,
)


def is_shiny_today(creation_date: date, today: date) -> bool:
    """Return True if `today` is a shiny-task day for a server created
    on `creation_date`.

    The cycle is: a server's first shiny day is creation + 3 days, then
    every third day forever. Equivalent to:

        delta = (today - creation_date).days
        return delta >= 3 and delta % 3 == 0

    Verified against cpt-hedge.com for #2263–#2270 on 2026-05-11; see
    docs/hedge_data_source.md for the full reference table.
    """
    delta = (today - creation_date).days
    return delta >= 3 and delta % 3 == 0


def servers_shiny_today(
    server_rows: list[dict],
    today: date,
) -> list[int]:
    """Return server numbers that are shiny today, sorted ascending.

    Pure function over the list of `shiny_task_servers` rows returned
    from `config.get_shiny_task_servers_in_range`. `creation_date` may
    be an ISO date string or a `date` object — both are accepted so the
    helper can be exercised against fixtures without round-tripping
    through SQLite.
    """
    out: list[int] = []
    for row in server_rows:
        cd = row.get("creation_date")
        if isinstance(cd, str):
            try:
                cd = date.fromisoformat(cd[:10])
            except ValueError:
                continue
        if not isinstance(cd, date):
            continue
        if is_shiny_today(cd, today):
            out.append(int(row["server_number"]))
    out.sort()
    return out


def format_server_list(servers: list[int]) -> str:
    """Render a server-number list as friendly English.

    []                → ""
    [681]             → "681"
    [681, 682]        → "681 and 682"
    [681, 682, 689]   → "681, 682 and 689"
    """
    nums = [str(n) for n in servers]
    if not nums:
        return ""
    if len(nums) == 1:
        return nums[0]
    return ", ".join(nums[:-1]) + " and " + nums[-1]


class _SafeDict(dict):
    """Format-map that renders missing keys as literal `{key}` text.

    Same pattern as `train_cog._SafeDict` — a typo in the configured
    announcement template (e.g. `{servrs}`) must render literally
    instead of crashing the daily scheduler loop.
    """

    def __missing__(self, key):
        return "{" + key + "}"


def _format_date_for_template(d: date) -> str:
    """Cross-platform "Weekday, Month D" — manual day-of-month rather
    than `%-d`/`%#d`, which are POSIX-only and Windows-only respectively.
    """
    return d.strftime("%A, %B ") + str(d.day)


def render_announcement(
    template: str,
    *,
    servers: list[int],
    today: date,
) -> str:
    """Substitute `{servers}` and `{date}` into the configured template.

    `template` is the guild's saved `message_template` or, if empty,
    `DEFAULT_SHINY_TASKS_MESSAGE` from defaults.py — resolve the
    fallback via `resolve_announcement_template` before calling.
    """
    return template.format_map(
        _SafeDict(
            servers=format_server_list(servers),
            date=_format_date_for_template(today),
        )
    )


# ── Fetch + parse cpt-hedge bundle ─────────────────────────────────────────


class HedgeFetchError(RuntimeError):
    """Raised when fetching or parsing cpt-hedge returns a result we
    can't act on (HTTP failure, missing chunk URL, zero records parsed).
    The refresh task catches this and logs; it doesn't propagate."""


async def _fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(
        url,
        headers={"User-Agent": HEDGE_USER_AGENT},
        timeout=aiohttp.ClientTimeout(total=HEDGE_FETCH_TIMEOUT_S),
    ) as resp:
        if resp.status != 200:
            raise HedgeFetchError(f"GET {url} → HTTP {resp.status}")
        return await resp.text()


def _parse_records(bundle_text: str) -> list[tuple[int, str, str]]:
    """Extract (server_number, creation_date_iso, region) from the JS
    chunk. Returns rows in stable iteration order (no sort).

    `creation_date_iso` is `YYYY-MM-DD` in UTC. `region` is the first
    string in the embedded `region` list, or empty if the list is empty
    / quoting is unparseable. Records the regex doesn't match are
    silently skipped — Hedge can ship harmless additional `currentX`
    fields without breaking us as long as the `id` / `timestamp` /
    `region` triple is intact.
    """
    rows: list[tuple[int, str, str]] = []
    for m in _SERVER_RECORD_RE.finditer(bundle_text):
        try:
            sid = int(m.group("id"))
            ts_ms = int(m.group("ts"))
        except (TypeError, ValueError):
            continue
        # region capture is the inside of the brackets: e.g.
        #   "global"           → global
        #   "europe","na"      → europe (we keep only the first; Hedge
        #                                ships single-element lists in
        #                                practice)
        #   (empty)            → ""
        region_raw = m.group("region") or ""
        region_match = re.search(r'"([^"]*)"', region_raw)
        region = region_match.group(1) if region_match else ""
        cd = _creation_date_from_ms(ts_ms)
        rows.append((sid, cd.isoformat(), region))
    return rows


def parse_server_records_json(text: str) -> list[tuple[int, str, str]]:
    """Parse a JSON array of server records into rows ready for
    `config.upsert_shiny_task_servers`.

    This is the shape the source's servers page loads (and the format the
    `/admin shiny_import` command ingests): a list of objects each carrying an
    `id`, a `timestamp` of unix-milliseconds, and a `region` list. Returns
    `(server_number, creation_date_iso, region)` tuples with creation dates in
    server time (UTC-2) so they match the source's displayed dates and the
    3-day cycle lines up. Records missing a usable id/timestamp are skipped
    rather than aborting the whole import.
    """
    records = json.loads(text)
    rows: list[tuple[int, str, str]] = []
    for r in records:
        try:
            sid = int(r["id"])
            ts_ms = int(r["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        region_list = r.get("region") or []
        region = region_list[0] if isinstance(region_list, list) and region_list else ""
        rows.append((sid, _creation_date_from_ms(ts_ms).isoformat(), region))
    return rows


async def fetch_server_table() -> list[tuple[int, str, str]]:
    """Fetch + parse the cpt-hedge server table.

    Returns a list of `(server_number, creation_date_iso, region)`
    tuples ready to feed into `config.upsert_shiny_task_servers`.
    Raises `HedgeFetchError` on any unrecoverable failure.
    """
    async with aiohttp.ClientSession() as session:
        # Step 1: HTML page → find chunk URL
        html = await _fetch_text(session, HEDGE_BASE_URL + HEDGE_SERVERS_PATH)
        m = _PAGE_CHUNK_RE.search(html)
        if not m:
            raise HedgeFetchError(
                "could not locate /_next/static/chunks/app/servers/page-*.js "
                "in cpt-hedge HTML — page structure may have changed"
            )
        chunk_path = m.group(0)

        # Step 2: chunk bundle → regex-parse records
        bundle = await _fetch_text(session, HEDGE_BASE_URL + chunk_path)

    rows = _parse_records(bundle)
    if not rows:
        raise HedgeFetchError(
            f"parsed 0 server records from {chunk_path} — "
            f"record shape may have changed (bundle size={len(bundle)} chars)"
        )
    return rows


# Whether the server refresh actually fetches the upstream source.
#
# The community site that fed `shiny_task_servers` moved its server data
# behind an API key we don't have — the maintainer hasn't responded to an
# access request, and the site itself notes that newer servers' day values
# are crowd-corrected estimates, so freezing the data loses little (#293).
# The feature now serves the snapshot already in the DB; the daily post
# loop reads from `shiny_task_servers` and is unaffected. New servers are
# added manually. With this False the weekly refresh and the startup seed
# are clean no-ops instead of raising `HedgeFetchError` into Sentry every
# run. To re-enable: repoint `fetch_server_table` at an authenticated
# endpoint and set this True.
SERVER_REFRESH_ENABLED = False


async def refresh_servers() -> int:
    """Fetch the latest server list and upsert every row.

    Returns the number of rows upserted on success. Called from the
    weekly background loop, on first startup when the table is empty,
    and (manually) via the `/admin` group if a fast resync is ever needed.

    No-op returning 0 while `SERVER_REFRESH_ENABLED` is False (#293): the
    upstream source is unavailable, so the existing `shiny_task_servers`
    rows are left untouched rather than the scrape raising on every run.

    When enabled, errors are propagated to the caller — the scheduler loop
    catches them and routes to Sentry / logs. Don't swallow here; a silent
    no-op refresh is harder to diagnose than a logged failure.
    """
    if not SERVER_REFRESH_ENABLED:
        print(
            "[SHINY] Server refresh disabled — upstream source requires "
            "authenticated access we don't have (#293). Serving the frozen "
            "shiny_task_servers snapshot; add new servers manually."
        )
        return 0

    from config import upsert_shiny_task_servers

    rows = await fetch_server_table()
    seen_at = datetime.now(tz=timezone.utc).isoformat()
    return upsert_shiny_task_servers(rows, seen_at=seen_at)


def resolve_announcement_template(saved_template: str) -> str:
    """Return the template to use for rendering: the guild's saved
    value, or the hardcoded default if empty / whitespace-only.

    Centralised so the scheduler loop, the setup hub's 🗂️ View configuration embed,
    and any future preview command all resolve the same way.
    """
    from defaults import DEFAULT_SHINY_TASKS_MESSAGE

    if saved_template and saved_template.strip():
        return saved_template
    return DEFAULT_SHINY_TASKS_MESSAGE


def build_announcement_for_guild(
    *,
    server_rows: list[dict],
    server_min: int,
    server_max: int,
    today: date,
    template: str,
) -> Optional[str]:
    """End-to-end helper: filter the rows to the configured range,
    compute today's shinies, and render the configured template.

    Returns `None` if no servers in the alliance's range are shiny
    today — callers should treat that as "skip posting" rather than
    posting an empty message. The range filter belongs at the DB-query
    level (`get_shiny_task_servers_in_range`) for the production path;
    this helper additionally re-applies it so tests can pass an
    unfiltered roster fixture without a SQLite round-trip.
    """
    in_range = [r for r in server_rows if server_min <= int(r["server_number"]) <= server_max]
    todays = servers_shiny_today(in_range, today)
    if not todays:
        return None
    return render_announcement(
        resolve_announcement_template(template),
        servers=todays,
        today=today,
    )
