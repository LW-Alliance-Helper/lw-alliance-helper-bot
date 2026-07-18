# Section Audit: Growth & Member Stats

## 1. Files covered

- `growth.py` — 1294 lines
- `member_stats.py` — 1069 lines
- `member_roster.py` — 1550 lines

## 2. Summary

This section is in reasonably good shape on the axes that matter most for a Discord bot: async/blocking discipline is consistently correct (every Sheets/DB read that backs a command is offloaded via `asyncio.to_thread` / `run_in_executor`), Sheets writes are properly batched, and error handling almost never swallows silently. The main risks are structural rather than correctness bugs: all three files mix Discord UI, business logic, and Sheets I/O in one module (the repo's own `buddy.py`/`buddy_cog.py`/`buddy_ui.py` split isn't followed here), `member_roster.py`'s setup wizard is one ~460-line function with two UI classes defined inside it, and there's real duplication both within `growth.py` (the `{label} (period)` header-matching pattern is reimplemented three times) and across `member_stats.py` re-deriving roster/growth-column logic that `member_roster.py`/`growth.py` already expose.

## 3. Findings

### Critical

None found. No blocking-call-on-event-loop bugs, no data-loss risk, no secrets handling issues in these three files.

### High

- **[Duplication] Roster row/column resolution reimplemented 3x inside `member_stats.py`, instead of reusing `member_roster.py`.**
  `member_stats.py:56-131` (`_roster_rows`, `_cell`, `_resolve_self`, `_resolve_named`, `_roster_names`) each independently pull `discord_id_col`/`display_col`/`name_col`/`joined_col` out of `rcfg` and walk `values[1:]` by hand. `member_roster.py:198-231` (`_roster_cell`, `parse_roster_rows`) already does the "cell-safe read + column-index resolution" for the exact same sheet. `member_stats.py:66-67`'s `_cell()` is a byte-for-byte duplicate of `member_roster.py:198-199`'s `_roster_cell()` under a different name.
  Recommend: have `member_stats.py` resolve on top of `member_roster.parse_roster_rows()` / `read_roster_members()` (or extract a shared `roster_data.py`/`_roster_common.py` with the cell/column helpers) instead of re-deriving column indices four separate times across two files.

- **[Duplication] The `{label} ({period})` header-matching pattern is implemented independently 3 times inside `growth.py`, plus a 4th time in `member_stats.py`.**
  `growth.py:194-200` (`build_growth_series`), `growth.py:270-273` (`build_member_power_map`), and `growth.py:331-337` (`build_member_history`) each contain a near-identical nested loop: `for i, h in enumerate(header): for label in metric_labels: if h.startswith(f"{label} ("...`. `member_stats.py:266-288` (`_power_field`) reimplements the same "latest + previous period column" lookup a 4th time, by hand, instead of calling `growth.build_member_history()` (which already returns the full per-metric series in chronological order — the last two entries are exactly what `_power_field` wants).
  Recommend: factor the column-index-building loop into one shared `growth._period_column_index(header, metric_labels)` helper used by all three `growth.py` builders, and have `member_stats._power_field` build its lines from `growth.build_member_history()`'s output instead of re-scanning the header.

- **[Structure] `member_roster.py` mixes Discord UI (2 `discord.ui` classes defined *inside* a 460-line function), the setup wizard, sync/merge business logic, and Sheets I/O in a single 1550-line file, without the `_cog`/`_ui`/`_logic` split the repo already uses elsewhere (e.g. `buddy.py`/`buddy_cog.py`/`buddy_ui.py`).**
  `member_roster.py:1086-1547` (`run_member_roster_setup`) is one function containing the entire 3-step wizard, the layout-detection preview text, and two full UI classes defined inline (`_LayoutRemapModal` at `member_roster.py:1349-1369`, `_LayoutConfirmView` at `member_roster.py:1371-1442`). This makes the wizard impossible to unit-test independently of Discord objects and impossible to skim.
  Recommend: split into `member_roster.py` (pure sync/merge logic + Sheets I/O, already reasonably factored: `_build_roster_rows`, `_merge_with_existing`, `write_roster`, `detect_column_layout`), `member_roster_cog.py` (the `MemberRosterCog` command surface), and `member_roster_wizard.py` (the setup flow + its two UI classes hoisted to module level).

### Medium

- **[Function length] `run_member_roster_setup` is ~460 lines** (`member_roster.py:1086-1547`), roughly 6-7x the ~60-80 line guideline, mixing input gathering, layout-detection preview rendering, two UI class definitions, config save, and the initial sync + result embed all in one body. Flag as a decomposition candidate even independent of the structural split above — e.g. extract `_run_layout_detection_step(...)`, `_render_layout_preview(...)`, and `_run_initial_sync(...)` as named steps.

- **[Function length / nesting] `_merge_with_existing` is ~200 lines with 4+ levels of nested `if`/`for`** (`member_roster.py:366-569`, worst nesting at `member_roster.py:500-539`: `for raw_row → if _row_is_non_discord → if did / else → if not candidate_name → if len(matches)==1/elif/else`). The docstring is excellent and the logic is intentional, but the row-matching branch (ID match / name-fallback / ambiguous / no-match) reads as a good candidate for extracting a `_match_existing_row(row, ...) -> MatchResult` helper with early returns, called from a flatter loop.

- **[Function length] `_write_breakdown_for_snapshot` is ~160 lines** (`growth.py:781-941`), combining idempotency-checking, header-column reservation, per-member percent/bucket computation, the Sheets write, and triggering the Premium auto-post. Consider splitting the "compute breakdown rows" part from the "write to sheet" and "maybe auto-post" parts.

- **[Function length] `_run_growth_snapshot_inner` is ~145 lines** (`growth.py:632-778`) mixing tab-existence checks, header-column creation, per-member row writes, and triggering `_write_breakdown_for_snapshot`. Same shape as the finding above — a reasonable split point is "ensure header/rows" vs "write" vs "post-snapshot breakdown trigger."

- **[Function length / fragile parsing] `read_latest_breakdown` is ~125 lines** (`growth.py:1046-1172`) and reverse-engineers which columns belong to which transition by string-splitting header text (`" - "` split at `growth.py:1119`, suffix-matching against metric labels at `growth.py:1122-1129`). This works today because metric labels are matched longest-first, but it's an implicit protocol (column-name grammar) with no schema/version marker — a metric label containing `" - "` or being a suffix of another configured label in an unexpected way would silently misparse. Not a bug today; flagged as something worth a second pair of eyes given it drives what leadership sees in `/growth breakdown`.

- **[Coupling] `member_stats.py` reaches into `growth.py`'s underscore-prefixed "private" helpers instead of its public API.**
  `member_stats.py:155`, `:251`, `:767` call `growth._get_spreadsheet(...)` directly, and `member_stats.py:276`, `:281` call `growth._safe_float(...)` directly. Both are named with a leading underscore in `growth.py`, signaling "internal." Recommend either renaming them to a public, documented cross-module surface (`growth.get_spreadsheet` / `growth.safe_float`) if they're meant to be shared, or wrapping the growth-tab read behind one of `growth.py`'s existing public `read_*` functions so `member_stats.py` never needs the spreadsheet handle at all.

- **[Duplication] `_col_letter` (0-indexed column → `A`/`B`/…/`AA` conversion) is implemented twice with different code**: `growth.py:964-974` (module-level, reused by 3+ callers in that file) and a second, differently-written version nested inside `run_member_roster_setup` (`member_roster.py:1253-1259`). `gspread` also ships `gspread.utils.rowcol_to_a1` which could replace both. Recommend consolidating on one implementation (or the library's) in a shared sheets-utility module.

- **[Many parameters] Two growth.py functions take 7-8 positional parameters**: `_maybe_post_breakdown` (`growth.py:977-985`, 7 params) and `_write_breakdown_for_snapshot` (`growth.py:781-790`, 8 params). Both are private/internal so the risk is low, but a future caller passing args in the wrong order would fail silently (e.g. swapping `prev_period_label`/`curr_period_label`, both plain `str`). Recommend keyword-only (`*`) the way `format_breakdown_embed` (`growth.py:1249-1257`) already does.

- **[Performance / consistency] Single-member growth-tab reads are never cached, unlike roster reads.** `member_stats._power_field` (`member_stats.py:248-256`) and `member_stats._power_values` (`member_stats.py:766-767`) each call `growth._get_spreadsheet(guild_id).worksheet(...).get_all_values()` fresh on every call. `member_roster`'s equivalent read (`config.read_member_roster_values`) is wrapped in a short-TTL cache. Today this is bounded (one member per `/my_stats` or `/member_stats` pick at a time) so it's not urgent, but if `GET /profile` (Map Manager, routed through `member_stats.build_member_profile`) is ever called in a loop across a whole alliance roster, this becomes an N-full-sheet-read problem. Worth deciding now whether growth-tab reads should go through the same TTL-cache pattern as roster reads, before an MM feature starts calling it in bulk.

### Low

- **[Dead code] Unused imports in `growth.py`**: `import os` (`growth.py:15`) and `import json` (`growth.py:16`) — grepped, neither `os.` nor `json.` is referenced anywhere in the file. Safe to remove.

- **[Error visibility] Silent `except Exception: pass` around Sentry capture** (`member_roster.py:864-866`, inside `_auto_sync_if_enabled`'s error path): if `sentry_sdk.capture_exception(e)` itself raises (e.g. Sentry not configured / import error already handled above, but a capture-time failure isn't), it's swallowed with no log line at all. Low risk since it's already inside an error-handling path, but worth at least a one-line `logger.debug` so a broken Sentry integration doesn't fail invisibly forever.

- **[Naming] Inconsistent embed-building reuse for "power" data.** `member_stats._power_values` (`member_stats.py:755-771`) correctly reuses `growth.build_member_power_map`, while `member_stats._power_field` (`member_stats.py:235-291`) — covering the same data for the same member — reimplements the column-scanning instead of calling `growth.build_member_history`. Same file, same underlying data, two different reuse postures; worth aligning once the High-severity duplication finding above is addressed.

## 4. What's already optimal

- **Async/blocking discipline is consistently correct across all three files.** Every command handler, button callback, and event listener that needs to read/write Sheets or hit the DB does so via `asyncio.to_thread` or `run_in_executor` — e.g. `member_stats.py:1032-1039` (`my_stats`), `member_stats.py:1003-1006` (`MemberPickerView._on_pick`), `member_roster.py:842-848` (`_auto_sync_if_enabled`), `member_roster.py:1006-1012`/`1204-1213`/`1499-1504` (the setup wizard's sheet reads and initial sync). No event-loop-stalling blocking calls were found in this section — this is not the norm across every part of a codebase this size and is worth calling out.

- **Sheets writes are properly batched, with the quota constraint explicitly called out in comments.** `growth.py:472-474` (`upsert_member_power`), `growth.py:743-747` (`_run_growth_snapshot_inner`), and `growth.py:933-936` (`_write_breakdown_for_snapshot`) all collect per-cell updates into a list and fire one `ws.batch_update(...)` / `ws.append_rows(...)` at the end of the loop, with an explicit `(#40 quota)` comment explaining why. `member_roster.write_roster` (`member_roster.py:613-615`) rewrites the whole tab in one `ws.clear()` + one `ws.update("A1", merged, ...)` rather than per-row. This is exactly the batching discipline the standards doc calls out as a common perf pitfall, and this section gets it right everywhere.

- **Error handling almost never swallows silently.** The overwhelming majority of `except Exception as e:` blocks across all three files log with `logger.warning(...)` or `print(...)` including guild ID and enough context to debug from logs alone (e.g. `member_stats.py:74-76`, `:159-160`, `:168-169`, `:221-223`; `growth.py:245-247`, `:306-308`, `:367-369`; `member_roster.py:849-855`, `:1217-1223`). Combined with the "degrade to empty, never raise" pattern used throughout the `read_*` functions (documented explicitly in their docstrings, e.g. `growth.py:224-232`, `member_roster.py:234-239`), a Sheets outage degrades gracefully instead of crashing commands or the API routes that consume these functions.

- **Pure functions are cleanly separated from I/O, and it shows in test coverage.** `growth.py`'s `classify_bucket`, `compute_pct_change`, `build_growth_series`, `build_member_power_map`, and `build_member_history` are all pure (no Sheets/Discord calls), taking already-fetched rows in and returning plain dicts — each has a thin `read_*` wrapper that does the I/O and hands off to the pure function. Same pattern in `member_roster.py` with `detect_column_layout` and `parse_roster_rows`. This is exactly why `tests/unit/test_growth_series.py` and `tests/unit/test_growth.py` can test the aggregation logic directly without mocking gspread.

## 5. Open questions

- **Is `GET /profile` (backed by `member_stats.build_member_profile`) ever called in a loop across a whole roster from Map Manager, or always one member at a time?** This determines whether the uncached growth-tab read flagged above (Medium, Performance/consistency) is a real N+1 risk worth fixing now or a non-issue. Needs Kevin's/MM-side knowledge of the calling pattern, not visible from this repo alone.

- **Is the `read_latest_breakdown` column-name parsing (`growth.py:1096-1149`) considered a stable-enough "protocol" to leave as string-matching, or does it warrant a real schema (e.g. a hidden metadata row/column) now that it's read by both the bot's own `/growth breakdown` command and MM's `GET /growth/breakdown` route?** Flagged as fragile above, but whether it's worth the migration cost is a product call, not a clear standards violation.

- **Should `member_stats.py` and `member_roster.py` be split along the `_cog`/`_ui`/`_logic` convention now, or is this section considered stable enough (Premium roster sync, `/member_stats`) that a structural refactor risks regressions for limited benefit?** Both files work correctly today; the split is a maintainability investment, not a bug fix, and Kevin may reasonably decide to defer it.

- **The `growth._get_spreadsheet` / `growth._safe_float` underscore-privates are used cross-module by `member_stats.py` today — are they meant to be part of `growth.py`'s public surface (worth renaming without the leading underscore) or should `member_stats.py` stop reaching in?** Both are reasonable answers; flagged as a coupling smell above but the "right" fix depends on whether `growth.py` intends to expose them.
