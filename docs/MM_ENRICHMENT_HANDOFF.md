# Bot → Map Manager: new endpoints (#316)

Five bot endpoints landed on the integration branch. Two are the **OCR
write-backs** MM already has client methods for (now live, just verify); three
are **new enrichment reads** MM should add and wire into existing alliance pages.

All are guild-scoped, service-key (Bearer) authed, same base URL as the rest.
All reads degrade to an empty-but-valid shape (never 500) when the underlying
feature isn't configured, so a page can render its empty state.

**Joins are by name** (case-insensitive, display name preferred then username) —
the same key the growth + participation sheets use. The bot does the matching;
MM passes `discord_id` where it has one.

---

## A. OCR write-backs — now LIVE (were 501; verify your client)

Both return `{ "written": boolean, "rows": number }`. MM's `addRosterMembers` /
`upsertPower` already type the result as `{ written, rows }`, so no client change
is needed — just confirm against this behavior.

### `POST /api/guilds/{guildId}/sheet/roster` — member-add (§6.3)

```jsonc
{ "members": [ { "name": "Ada", "discord_id": null } ] }
```

- Merge-adds each name **not already on the roster** (by `discord_id` when
  present, else case-insensitive name).
- OCR'd names land as **non-Discord rows** (name in both Name and Display Name;
  the `Is this user in Discord?` column set to `No`). Identity stays
  Discord-sync-owned: if that person is actually in Discord, the next member sync
  reconciles the row and flips the flag.
- Existing rows and hand-typed non-Discord rows are **never touched**. Duplicate
  names within one request are collapsed.
- `rows` = number actually appended; `written` = `rows > 0`.
- No-ops (`{ "written": false, "rows": 0 }`) when the roster feature isn't enabled.

### `POST /api/guilds/{guildId}/sheet/power` — power upsert (§6.2)

```jsonc
{ "members": [
    { "name": "Ada", "discord_id": null,
      "values": { "Total Hero": 1234567, "1st Squad": 987654 } } ] }
```

- Upserts each sent metric into the **current month's** `{label} ({Mon YYYY})`
  column on the Growth Tracking tab (creates the column / member row if missing).
- Only labels matching the alliance's **configured growth metrics** are written;
  unknown labels are ignored. Metrics/columns MM didn't send are never touched.
- `rows` = members upserted; `written` = `rows > 0`. No-ops when growth isn't
  configured.
- **MM note:** the keys in `values` must be the alliance's configured metric
  labels — the same ones `/sheet/growth` and `/sheet/roster` return. Pull them
  from the growth metric list; don't hardcode. (Writes always target the current
  month, matching the scheduled snapshot's period column.)

---

## B. Enrichment reads — NEW (add client methods, wire into pages)

### `GET /api/guilds/{guildId}/growth/breakdown` → Growth page

Per-member growth classification for the latest period-over-period transition.

```jsonc
{ "has_data": true,
  "prev_period_label": "Apr 2026",
  "curr_period_label": "May 2026",
  "metric_labels": ["Total Hero", "1st Squad"],
  "summary": {
    "Total Hero": { "increased": ["Ada"], "steady": ["Cy"], "low": [], "none": [], "decline": ["Bo"] },
    "1st Squad":  { "increased": [], "steady": ["Ada"], "low": [], "none": ["Bo"], "decline": [] } } }
```

- `has_data: false` (other fields empty) before the alliance has two snapshots —
  render the empty state.
- Bucket keys are canonical (`increased` / `steady` / `low` / `none` /
  `decline`). A Premium alliance may relabel them in Discord; show your own
  labels keyed off these.
- Suggested UI: a bucket chip per member on the Growth page, or a
  "needs attention" group (`none` + `decline`).

### `GET /api/guilds/{guildId}/members/{discordId}/stats` → member detail

The consolidated profile behind the bot's `/member_stats` (leadership view — MM
is officer-facing). Sections are omitted when the member has no data for them.

```jsonc
{ "member": { "name": "Ada", "discord_id": "111", "joined_at": "2026-01-01", "is_manual": false },
  "power": { "Total Hero": 1234567, "1st Squad": 987654 },
  "storm": {
    "ds": {
      "signup":     { "available": 6, "total": 8, "pct": 75, "last_vote": "2026-05-15" },
      "attendance": { "attended": 5, "tracked": 7, "pct": 71, "last_attended": "2026-05-15" },
      "placement":  { "primary": 4, "sub": 1, "sat_out": 2, "last_sat_out": "2026-05-01" } },
    "cs": { "...": "same shape" } },
  "train":   { "conductor_count": 3, "last_drove": "2026-05-20",
               "reasons": [ { "reason": "Volunteered", "count": 2 } ] },
  "surveys": [ { "survey_name": "Squad Power", "last_response": "May 29, 2026" } ] }
```

- `404 { "error": "member_not_found" }` when the id is unknown to both the roster
  and the gateway; `400` on a non-numeric id.
- Empty sections: `power` is `{}`, `storm` is `{}`, `surveys` is `[]`, `train` is
  absent.
- This overlaps the existing `/members/{id}/history` (growth over time): use
  history for the chart, this for the at-a-glance profile.

### `GET /api/guilds/{guildId}/storm/trends?event_type=ds|cs&lookback=N` → Storms page

Per-member storm attendance over the last `N` events (default 12, clamped 1..50).
Useful now — it doesn't depend on match-outcome OCR.

```jsonc
{ "event_type": "ds", "lookback_events": 12, "total_events": 9,
  "members": [
    { "member": "Ada", "attended": 8, "tracked": 9, "attendance_pct": 89, "last_attended": "2026-05-15" } ] }
```

- Sorted by `attendance_pct` desc, then member name. `total_events` is the
  window's distinct event count; `tracked` is the member's own logged-event count
  (matches `/member_stats`).
- `400` on a bad/missing `event_type`.
- Suggested UI: an attendance-trend table on the Storms page, or a
  low-attendance highlight.

---

## What's intentionally NOT here

- **Storm results (opponent / win-loss / score)** stay MM-only (OCR), by design —
  the bot does not store match outcomes in the Sheet. `GET`/`POST
  /sheet/storm-history` remain 501.
