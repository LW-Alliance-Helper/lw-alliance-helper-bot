# cpt-hedge.com data source — Shiny Tasks

Reference for `shiny_tasks.fetch_server_table` and the weekly refresh
loop. Documents *how* we get the server list and *what* it looks like.

> **Status (2026-06, #293): refresh DISABLED.** The site moved its server
> data behind an API key (the `/servers` page no longer inlines records in
> the page chunk, and `/api/servers` now returns `401 "Invalid or missing
> API key"`). An access request to the maintainer went unanswered, and the
> site itself notes that newer servers' day values are crowd-corrected
> estimates. So `shiny_tasks.SERVER_REFRESH_ENABLED` is `False`: the weekly
> refresh and startup seed are no-ops, and the feature serves the frozen
> `shiny_task_servers` snapshot already in the DB. The endpoint-discovery
> and parsing notes below describe the **old** working scrape, kept for
> reference. To re-enable, repoint `fetch_server_table` at an authenticated
> endpoint and flip the flag.

## Endpoint discovery

`https://cpt-hedge.com/servers` is a Next.js app. The server table is
client-rendered from data **embedded in the page JS chunk**, not from a
separate JSON API. There is no `/api/...` route and no
`/_next/data/.../servers.json` payload — the React bundle ships the full
list inline.

Fetch flow (all `aiohttp`, browser User-Agent required — Cloudflare 403s
without one):

1. `GET https://cpt-hedge.com/servers` → HTML
2. Extract the chunk path from the HTML:
   `/_next/static/chunks/app/servers/page-<hash>.js`
   (`<hash>` changes on every Hedge deploy — never hardcode)
3. `GET https://cpt-hedge.com<chunk_path>` → ~600KB JS
4. Regex-extract records matching:

```
{"id":"N","server":"State#N","timestamp":"<ms>",
 "seasonStartTimestamps":{...},"currentSeason":I,
 "isPostSeason":B,"currentWeek":I,
 "updatedAt":<ms>,"region":["<region>"]}
```

The fields we keep: `id` → `server_number`, `timestamp` → `creation_date`
(`datetime.fromtimestamp(int(ms)/1000, tz=UTC).date()`), `region[0]` →
`region`.

As of 2026-05-11 the bundle contains 2266 servers (highest #2270, with
gaps).

## Why not just scrape the HTML table?

The rendered HTML table is virtual-scrolled — `<td style="height:
162936px">` spacer guards mean only a window of rows is in the DOM at
any moment. Even with a full page render the response body only carries
a few rows. The chunk JS payload is the canonical source.

## 3-day shiny cycle

The cycle is fully derivable from `creation_date`. No need to track a
phase column or anchor date:

```python
def is_shiny_today(creation_date: date, today: date) -> bool:
    delta = (today - creation_date).days
    return delta >= 3 and delta % 3 == 0
```

Verified against Hedge's "Shiny Tasks" column for all 8 servers in the
#2263–#2270 range, today = 2026-05-11:

| Server | Created | Days since | Hedge says | Computed     |
|--------|---------|------------|------------|--------------|
| #2263  | Apr 27  | 14         | Tomorrow   | 15 % 3 == 0  |
| #2264  | Apr 29  | 12         | Today      | 12 % 3 == 0  |
| #2265  | May 1   | 10         | In 2 days  | 12 % 3 == 0  |
| #2266  | May 2   |  9         | Today      |  9 % 3 == 0  |
| #2267  | May 4   |  7         | In 2 days  |  9 % 3 == 0  |
| #2268  | May 6   |  5         | Tomorrow   |  6 % 3 == 0  |
| #2269  | May 8   |  3         | Today      |  3 % 3 == 0  |
| #2270  | May 10  |  1         | In 2 days  |  3 % 3 == 0  |

Same `creation_date % 3` ⇒ same phase, so the population splits cleanly
into 3 cohorts that light up on alternating days.

## Refresh cadence + closed-server handling

One fetch path, one write strategy: `INSERT OR REPLACE` of the whole
returned set, run on first startup (table empty) and weekly thereafter.
Every row gets `last_seen_at = NOW()` on every refresh.

Soft-delete: when a server stops appearing in Hedge's table (closed,
merged, removed for whatever reason), its `last_seen_at` ages out and
becomes a marker that we can filter on. We treat "missing from the last
30 days of refreshes" as functionally deleted.

## Credits

Hedge's footer notes: "Server region data is curated by the people from
the Coordinates List Discord Server." The Coordinates List Sheet is
maintained by Princess Wolfy (server 723-g4w).
