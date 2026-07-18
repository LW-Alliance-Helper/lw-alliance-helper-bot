# Section Audit: Storm Strategy, Officer View & Rendering

## 1. Files covered

| File | Lines |
|---|---|
| `storm_officer_view.py` | 3226 |
| `storm_renderer.py` | 2341 |
| `storm_strategy.py` | 2629 |

## 2. Summary

All three files are large (well past the standards doc's ~2000-line "identify seams for a future split" threshold) but the async/blocking-call discipline is genuinely solid: every gspread round-trip is offloaded via `asyncio.to_thread`, and the one synchronous PIL render entry point (`storm_renderer.render`) is only ever invoked through `asyncio.to_thread` at its call sites in `storm_roster_builder.py` — no event-loop-blocking calls were found in either file. The real cost of the size shows up as duplication: `storm_officer_view.py` re-implements the same owner-guard and pagination (Prev/Next button) pattern verbatim across at least four `discord.ui.View` classes, and `storm_strategy.py` mixes Sheets I/O, preset business logic, and five Modal/View classes in one module (a `_data` / `_ui` split candidate). Error handling is mostly good (logged failures with guild/event context) but a handful of `except Exception: pass` blocks swallow failures with zero trace.

## 3. Findings

### Critical
None found. No blocking Sheets/PIL calls were found running directly on the event loop.

### High

- **[God file / duplication] `storm_officer_view.py` re-implements the same `_guard_owner` interaction-owner check in four separate classes.** Identical 8-line bodies at `storm_officer_view.py:1111-1118` (`_OnBehalfVoteView`), `:1472-1479` (`_TeamPlanRosterPickerView`), and `:1697-1704` (`_TeamPlanSubPickerView`), plus the same `if inter.user.id != officer_view.owner_user_id: await inter.response.send_message(DENY_NOT_OWNER, ephemeral=True)` inline pattern repeated standalone at `:1805`, `:2163`, `:2189`, `:2261`, `:2542`, `:2687`, `:2821` (12 occurrences total). This is session-ownership gating (correctly distinct from the leader/admin permission check in `storm_permissions.py` — see Positive notes below), but it's copy-pasted rather than shared.
  - **Recommendation**: factor a single `async def guard_owner(inter, owner_user_id) -> bool` module-level helper (or a small `_OwnerGuardedView` mixin with an `owner_user_id` attribute) and have all four View classes and the standalone functions call through it.

- **[God file] `storm_strategy.py` mixes three responsibilities in one 2629-line module**: Sheets I/O / data model (`load_preset`, `save_preset`, `delete_preset`, `zone_rules_for`, `ZoneRow`, `PresetBuffer` — lines 176-1050), and five separate `discord.ui.Modal`/`View` classes (`_ZoneEditModal`, `_ZonePhaseCapacityAndFloorsModal`, `_ZonePhasePriorityModal`, `_ZoneWizardNextView`, `_ApplyToSimilarView`, `_RenameModal`, `_PresetEditorView`, `_StrategyListView`, `_PresetPickerView` — lines 1116-2629). This is exactly the pattern the standards doc calls out (§1, citing the repo's own `buddy.py`/`buddy_ui.py` split as the target). `storm_officer_view.py` and `storm_renderer.py` are cleanly single-purpose (Discord UI, and pure rendering, respectively) by comparison.
  - **Recommendation**: split into `storm_strategy_data.py` (the gspread/parsing/model layer — everything above line ~1050) and `storm_strategy_ui.py` (the Modal/View classes), matching the `buddy.py`/`buddy_ui.py` precedent. Flagging only, per the standards doc — not attempting the split here.

- **[Duplication] Prev/Next pagination logic is copy-pasted across three picker views in `storm_officer_view.py`.** `_OnBehalfVoteView._build_components` (`:976-1023`), `_TeamPlanRosterPickerView._build_components` (`:1334-1381` approx.), and a third picker later in the file all build an identical `◀ Prev` / `Page N / M` (disabled) / `Next ▶` button triple with matching `_on_prev`/`_on_next` closures (`self.page -= 1` / `+= 1`, guarded by `_guard_owner`, `_build_components()` + `edit_message` re-render, `discord.HTTPException` swallow). Only the `row=` value and the "Next"/"Page ▶" label text differ between copies.
  - **Recommendation**: extract a `_add_pagination_row(view, *, row: int) -> None` helper (or a small `PaginatedView` base mixin exposing `page`/`page_count`/`_build_components`) that all three views call, to keep future pagination fixes (e.g. Discord API changes) a one-place edit instead of three.

### Medium

- **[Error visibility] Several `except Exception:` blocks in `storm_officer_view.py` swallow failures with no logging at all**, falling back to a default value silently: `:335` (`role_filter_id = 0`), `:587` (`team_plans = {}`), `:645` (`team_a_label, team_b_label = "", ""`), `:2767` and `:3038` (bare `pass` inside on-timeout cleanup). Every other except-block in the same file logs via `logger.warning`/`logger.exception` with guild/event context (e.g. `:169`, `:178`, `:2933`), so this is an inconsistency rather than a systemic gap. If, e.g., `role_filter_id` parsing starts failing for a misconfigured guild, the officer view silently shows an unfiltered member list with no trace in the logs.
  - **Recommendation**: add a `logger.debug`/`logger.warning` call to these five except blocks, matching the logging style already used elsewhere in the same file.

- **[Long function] `storm_officer_view.py:1791-2060` `_open_team_plan` is ~270 lines** covering permission check, defer, candidate-pool building, cross-team filtering, and picker-view construction/wiring in one async function body. It's well-commented with a clear docstring and inline rationale (defer-first pattern, cross-team filter), which keeps it readable, but per the >60-80 line guidance it's a decomposition candidate.
  - **Recommendation**: extract the candidate-pool + cross-team-filter block (roughly `:1846-1885`) into a `_build_team_plan_candidates(officer_view, team)` helper, leaving `_open_team_plan` as the orchestrator that defers, builds candidates, and opens the picker view.

- **[Long function] `storm_officer_view.py:133-322` `_read_roster_rows` (190 lines) and `:348-469` `_build_bucket_map` (122 lines)** both merge multiple data sources (Discord member pool, roster Sheet rows, on-behalf vote rows) with several sequential loops in one function body. Both are heavily commented and the logic is genuinely order-dependent (each loop consumes `seen_targets` state from the one before), so a naive split risks losing that invariant — flagging as a decomposition candidate for a careful pass, not a quick win.
  - **Recommendation**: if split, keep the `seen_targets` threading explicit (e.g. pass it as a parameter/return value between the three merge steps) rather than relying on shared closure state, to preserve the current correctness.

- **[Long function] `storm_renderer.py:1571-1825` `_draw_zone` (255 lines) and `:1826-2103` `_draw_subs_section` (278 lines)** are the two largest drawing functions, each combining layout math, text flow, and PIL draw calls in one body. Rendering code is somewhat inherently procedural/sequential, but both functions are past the point where sub-steps (e.g. "compute flow lines" vs. "draw computed lines") could be pulled into named helpers — a few already exist nearby (`_attempt_flow_at`, `_build_flow_lines`, `_draw_flow_lines`) that these two functions call, so the seam pattern is already established in the file.
  - **Recommendation**: apply the same extract-a-named-helper pattern already used for the zone-drawing flow (`_attempt_flow_at`/`_build_flow_lines`/`_draw_flow_lines`) to `_draw_subs_section`, which currently inlines its flat/pairs-variant branching rather than delegating to helpers.

- **[Coupling] `storm_strategy.py:474` `_translate_legacy_cs_zone` (leading-underscore, module-private by convention) is imported directly by `storm_member_rules.py:308`.** This reaches into `storm_strategy`'s internals from an unrelated file instead of going through a public interface — the same cross-module coupling pattern flagged in the `buddy_system` audit for this codebase.
  - **Recommendation**: either drop the leading underscore and document `_translate_legacy_cs_zone` (and its `_legacy_cs_zone_translation()` cache) as a small supported "legacy CS zone name" API, or add a public wrapper in `storm_strategy.py` for `storm_member_rules.py` to call.

- **[Naming] `Optional[X]` vs. `X | None` used inconsistently, including within the same file.** `storm_officer_view.py:82` uses `_dt.date | None`, but `:1951`, `:2112`, `:2744`, `:2893`, `:3015`, `:3079-3080`, `:3133` use `Optional[X]` (imported from `typing` at `:41`) for the same purpose. `storm_strategy.py:2508` uses the newer `discord.Message | None` style instead. Both files declare `from __future__ import annotations`, so the modern `X | None` syntax is available everywhere — this is a style inconsistency, not a functional issue.
  - **Recommendation**: standardize on `X | None` (already the majority style and the modern PEP 604 form) and drop the `typing.Optional` import from `storm_officer_view.py` once its 7 call sites are converted.

### Low

- **[Dead code] `storm_strategy.py:584` and `:605` both do `import config` inside the same function (`_get_or_create_strategies_worksheet`).** The second import at `:605` is redundant — `config` is already bound from the import three lines into the function body.
  - **Recommendation**: delete the duplicate `import config` at `:605`.

- **[Naming collision] Two unrelated classes are both named `_PresetPickerView`**: `storm_officer_view.py:3073` (a simple single-select preset dropdown, 56 lines) and `storm_strategy.py:2490` (an Edit/Delete action picker with overflow-notice handling, 140 lines). Both are module-private by the leading underscore, so there's no import collision, but the shared name makes tracebacks/logs and cross-file search ambiguous about which file's class raised.
  - **Recommendation**: rename one (e.g. `storm_officer_view.py`'s to `_SimplePresetPickerView`) to disambiguate, next time either file is touched.

- **[Local-import sprawl] `storm_officer_view.py` does a function-local `import config` at 10 separate call sites** (`:163, 358, 1142, 1712, 1846, 2072, 2554, 2694, 2759, 2825, 3030`) rather than a single top-level import. No circular-import evidence was found (`config.py` doesn't import any `storm_*` module), so this looks like habit/copy-paste rather than a necessity.
  - **Recommendation**: move `import config` to the top-level import block unless a specific circular-import reason exists; if there is one, a one-line comment would save the next reader from re-deriving it.

## 4. What's already optimal

- **Consistent `asyncio.to_thread` discipline around every gspread call in both `storm_officer_view.py` and `storm_strategy.py`.** Every Sheets round-trip (`_read_roster_rows`, `refresh_buckets`, preset save/load/delete/list, uniqueness checks) is offloaded, each with a comment explaining *why* (e.g. `storm_officer_view.py:2136-2137`, "gspread call doesn't stall the bot. Callers MUST await."). No blocking Sheets I/O was found running directly on the event loop.
- **`storm_renderer.render()` is a clean, side-effect-scoped pure function** (`storm_renderer.py:601-702`) — it takes a `RosterData` and returns `bytes`, with no Discord or gspread dependency inside it at all. Its only callers (`storm_roster_builder.py:4340, 6128-6130`) correctly wrap it in `asyncio.to_thread`, and `render()`'s own docstring documents the Pillow-missing fallback contract. Neither `storm_officer_view.py` nor `storm_strategy.py` calls into the renderer directly, keeping the render/UI/strategy boundary clean.
- **`storm_strategy.save_preset` (`:848-981`) batches its Sheet write into one `clear()` + one `update()` call** instead of per-row writes, with an explicit comment about why (avoiding per-zone API calls against the Sheets write quota) — good rate-limit awareness per the standards' §4 guidance.
- **Officer/leader permission checks correctly defer to `storm_permissions.is_leader_or_admin`** (`storm_officer_view.py:3135-3142`, `storm_strategy.py:2140-2145`) rather than reimplementing role/permission logic locally. The locally-defined owner-guards (flagged above as duplicated) are a *different*, legitimate check — "is this the person who opened this ephemeral session" — and are correctly kept separate from the leader/admin gate.
- **Legacy-data migration paths are unusually well-documented in `storm_strategy.py`.** `save_preset`'s `_translate` closure (`:882-918`) and the `_legacy_cs_zone_translation`/`_translate_legacy_cs_zone` pair (`:460-491`) both carry detailed comments explaining the exact historical header-shape bug they guard against (pre-#152 6-column DS header, pre-#178 internal zone keys) — this is the kind of tribal knowledge that's easy to lose and it's captured well here.

## 5. Open questions

- Is the `storm_officer_view.py`/`storm_strategy.py` owner-guard duplication (High finding above) worth unifying now, or is it low-risk enough (each copy is simple and self-contained) to leave until one of these files is touched for an unrelated change? A shared helper reduces future-fix surface but isn't fixing a live bug today.
- `storm_strategy.py`'s data/UI split (High finding) is a larger refactor than the pagination/owner-guard dedup — worth scoping as its own follow-up task (mirroring `buddy.py` → `buddy_data.py`/`buddy_ui.py` if that split exists) rather than folding into a general cleanup pass?
- The five silent `except Exception: pass`/bare-fallback blocks in `storm_officer_view.py` (Medium finding) all currently degrade gracefully (empty dict, default label, etc.) rather than crashing — is silent degrade-with-no-log the intended UX here, or should any of these surface a leadership-visible warning the way `_read_roster_rows`'s failures already do? This is a product-behavior call, not a clear-cut standards violation.
