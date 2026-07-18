# Section Audit: Buddy System

## 1. Files covered

| File | Lines |
|---|---|
| `buddy.py` | 782 |
| `buddy_cog.py` | 34 |
| `buddy_hub.py` | 372 |
| `buddy_ui.py` | 877 |

## 2. Summary

This is a well-organized section that already follows the `_cog` / `_hub` / `_ui` / logic split the standards doc calls out as the target pattern, and it shows real engineering care around Discord's platform limits (25-option selects, 1024-char embed fields, 60-writes/min Sheets quota). The issue #341 manual-picker 25-cap bug appears genuinely fixed via a shared paginating `_PickerView`, applied consistently everywhere a picker is built. The main gaps are cross-module encapsulation leaks (private-helper reuse both within this module set and reaching into `storm_roster_builder`), an inconsistent logging story (`print` vs `logger`), one dead import, and a couple of long functions that are good decomposition candidates.

## 3. Findings

### Critical
None found.

### High

- **[Coupling] `buddy.py:642,678` reaches into another feature module's private helpers.** `read_power_for_members` and `read_reliability_for_members` both do `from storm_roster_builder import _build_cross_tab_power_index, _lookup_power_in_index` — importing underscore-prefixed (private-by-convention) functions from an unrelated feature (`storm_roster_builder`). This is exactly the cross-module coupling pattern the standards doc calls out: reaching into another module's internals instead of a shared public interface. If `storm_roster_builder`'s internals change shape, this breaks silently for Buddy System too, and nothing in `storm_roster_builder.py` signals that these "private" functions have an external consumer.
  - **Recommendation**: promote `_build_cross_tab_power_index` / `_lookup_power_in_index` to a small shared module (e.g. `power_lookup.py`) that both `storm_roster_builder.py` and `buddy.py` import publicly, or rename them without the leading underscore and document them as a supported cross-tab lookup API.

### Medium

- **[Logging] `buddy.py` uses bare `print()` for all error reporting (11 call sites: lines 334, 343, 372, 389, 419, 607, 663, 704, 730, 769, 781) while `buddy_hub.py` and `buddy_ui.py` both set up `logger = logging.getLogger(__name__)` and use it.** `print` output bypasses log levels, handlers, and any centralized log aggregation the bot has configured, and can't be filtered/silenced in production. This is an observability gap specific to the Sheet-I/O half of this module.
  - **Recommendation**: replace the `print(f"[BUDDY] ...")` calls in `buddy.py` with `logger.warning(...)` / `logger.error(...)` on a module-level `logger = logging.getLogger(__name__)`, matching `buddy_hub.py`/`buddy_ui.py`.

- **[Error visibility] `buddy.py:746-747` and `buddy.py:759-760` — bare `except Exception: pass` with no logging at all.** In `write_profession_cell`, both the header-write (`ws.update("A1", [header])`) and the append-Profession-header (`ws.update_cell(1, prof_idx + 1, ...)`) failure paths are fully silent — not even a print. Every other failure path in this file at least logs. If either of these silently fails, the function proceeds with a stale `header`/`prof_idx` and can misfile a member's profession into the wrong column with zero trace.
  - **Recommendation**: log these two `except` blocks the same way the rest of the file does (`print`/`logger` with the guild id and tab name), even though the function continues on a best-effort basis.

- **[Coupling] `buddy_ui.py` reaches into `buddy.py`'s private helpers 11 times**: `buddy._norm` (lines 135, 140, 145, 155, 158, 176, 182, 501) and `buddy._join_and` (lines 243, 431, 511). Both are underscore-prefixed in `buddy.py`, signaling "internal to this module," but `buddy_ui.py` depends on them directly as if they were public API. `buddy_hub.py`, by contrast, never reaches past `buddy_ui`'s public functions — it's the cleaner boundary of the two UI-adjacent files.
  - **Recommendation**: either drop the leading underscore on `_norm`/`_join_and` in `buddy.py` (they're clearly meant to be shared) and document them as public, or add thin public re-exports (`buddy.normalize_name`, `buddy.join_and`) that `buddy_ui.py` calls instead.

- **[Long function] `buddy.py:103-241` `assign_buddies` is ~140 lines** covering dedup, existing-pair validation, free-pool computation, base fill, and doubling fill in one body. It's well-commented with numbered "Step" markers that already mark natural seams, but per the standards' >60-80 line guidance it's a decomposition candidate.
  - **Recommendation**: split along the existing Step comments into private helpers (`_dedup_members`, `_preserve_existing_pairs`, `_fill_base_pairs`, `_fill_doubling`), keeping `assign_buddies` as the orchestrator. Not urgent — the step comments already make it readable — but worth doing before more priority modes are added.

- **[Long function] `buddy.py:515-590` `save_pairs` is ~76 lines** mixing War-Leader grouping, row construction, and the write call. Borderline against the 60-80 line guidance.
  - **Recommendation**: extract the grouping loop (lines 534-553) into a `_group_pairs_for_sheet(result)` helper returning `(wl_order, wl_info, wl_engs)`, leaving `save_pairs` to build rows and call `_rewrite`.

- **[Typing] Missing generic type parameters on cross-module public functions in `buddy.py`.** Signatures like `assign_buddies(members: list, existing_pairs: list, ...)`, `load_pairs(...) -> list`, `merge_members(primary: list, fallback: list) -> list`, `read_all_professions(...) -> list`, `read_members_from_buddy_tab(...) -> list` all type collection parameters/returns as bare `list` rather than `list[Member]` / `list[Pair]`. The file already imports `from __future__ import annotations` and uses precise generics elsewhere (e.g. `dict[str, Member]`), so this is inconsistent rather than a tooling limitation.
  - **Recommendation**: add `list[Member]` / `list[Pair]` generics to these cross-module signatures; they're the functions `buddy_ui.py` and `buddy_hub.py` actually call, so the hints have real IDE/type-checker value here.

### Low

- **[Dead code] `buddy_hub.py:22` — `import buddy` is unused.** The module only ever calls through `buddy_ui as ui` (e.g. `ui.compute_current`, `ui.save_result`); no `buddy.<name>` call exists anywhere in the file (verified by grep — the only match for `buddy.` is inside a user-facing string on line 310, "...different buddy. Continue?", not code).
  - **Recommendation**: remove the unused `import buddy` line.

- **[Magic number] `buddy.py:340` `_open_tab` hardcodes `rows=2000` as the initial Buddies-tab size.** At 9 columns/member-row this comfortably covers any realistic alliance roster, so this is low real-world risk, but it's an uncommented hardcoded limit in a module that just fixed one hardcoded-limit bug (#341). Worth a one-line comment so a future reader doesn't have to wonder if it's another silent cap.
  - **Recommendation**: add a short comment noting this is an initial-size hint (Sheets/gspread grows the tab on write, it's not a hard cap), or bump it if there's a known alliance size ceiling worth documenting.

- **[Duplication] `BuddyManageView._pair` (buddy_ui.py:751-810) and `._repair` (buddy_ui.py:812-870) share near-identical structure**: build a pairs/members picker, then a nested Engineer picker, then persist via `_save_pairs_list` and refresh. The `eng_opts` construction (`[discord.SelectOption(label=m.name[:100], value=_member_value(m)) for m in free_eng]`) is duplicated verbatim at lines 780 and 834.
  - **Recommendation**: extract a small `_engineer_picker_options(free_eng)` helper (and consider a shared `_two_step_picker(first_opts, ..., on_final)` helper) to collapse the duplicated wiring; not urgent since both call sites are easy to read in isolation.

- **[Docstrings] `save_result` (buddy_ui.py:120-127) has no docstring**, unlike its siblings `compute_current`/`compute_autofill`/`buddies_of` in the same file. It's a one-line pass-through, so low priority, but it is a cross-module function (`buddy_hub.py` calls `ui.save_result` in five places).
  - **Recommendation**: one-line docstring for consistency, e.g. "Persist `result` to the Buddies tab via `buddy.save_pairs`."

## 4. What's already optimal

- **Issue #341's 25-option cap appears genuinely fixed, not just patched around.** `_PickerView` (`buddy_ui.py:589-656`) is a single shared paginating select component (`PAGE_SIZE = 25`, matching Discord's real platform limit, with ◀/▶ buttons that resync `discord.ui.Select.options` per page) and every manual-pairing picker in `BuddyManageView` — Unpair (line 745), Pair's War-Leader and Engineer steps (lines 774, 806), and Re-pair's pairing and Engineer steps (lines 826, 866) — routes through it. There's no remaining code path in this module set that builds a raw `discord.ui.Select` with an unbounded option list. This correctly distinguishes "Discord's real 25-option limit" (handled via pagination) from "silently broken above 25" (the original bug).
- **Consistent async-safety discipline around Google Sheets I/O.** Every gspread-touching function in `buddy.py` is synchronous by design (per its own module docstring) and every call site in `buddy_hub.py`/`buddy_ui.py` wraps it in `asyncio.to_thread` — including nested cases like `BuddyManageView._save_pairs_list` (`buddy_ui.py:703-716`), which correctly `to_thread`s both the member load and the save. No blocking Sheets calls were found directly in an `async def` body.
- **Sheets quota awareness is deliberate and documented.** `_rewrite` (`buddy.py:360-373`) does exactly one `batch_clear` + one `update` per save, and the module docstring explicitly calls out staying under the 60-writes/min quota. `write_profession_cell` similarly does single-cell/single-row writes instead of full-tab rewrites, with a docstring explaining why ("anti-clobber alternative to survey.update_squad_powers").
- **The `_cog` / `_hub` / `_ui` / logic split is genuinely clean at the `buddy.py` boundary.** `buddy.py`'s own docstring states "No Discord imports live here," and that holds — it's pure pairing logic (`assign_buddies`, `compose_change_notification`) plus Sheet I/O, with zero `discord`/`import discord` references anywhere in the file (confirmed by inspection). `buddy_cog.py` is a minimal 34-line command registration with no business logic at all, the cleanest file in the set.
- **Proactive handling of a second Discord platform limit** (not just the 25-option one): `_FIELD_CHAR_CAP = 1000` (`buddy_ui.py:186`) deliberately leaves headroom under the real 1024-char embed field cap, and `build_buddy_list_embed` truncates by whole rows (not mid-string) so the two side-by-side War-Leader/Engineer columns stay aligned even when a roster is large enough to overflow a field.

## 5. Open questions

- `read_power_for_members` / `read_reliability_for_members` (`buddy.py:634-704`) both silently default to `0.0` on any read failure, which sinks the affected member to the bottom of their priority order rather than surfacing an error to leadership. Is silent degrade-to-zero the intended UX for a Sheets misconfiguration, or should a failed power/reliability read produce a leadership-visible warning (e.g. in the hub embed) so a broken column reference doesn't quietly skew auto-assignment? This is a product-behavior call, not a standards violation.
- `_open_tab`'s `rows=2000` and `_rewrite`'s `+ 5000` clear-buffer (`buddy.py:340, 367`) are both hardcoded numbers sized against "realistic alliance roster" assumptions. Is there a known largest-alliance figure worth codifying as a named constant (shared with `train_rotation`/`storm_roster_builder`, which the docstring says this mirrors), or are these fine as tab-specific tuning knobs?
- Should the `buddy._norm` / `buddy._join_and` cross-module reuse (Medium finding above) be resolved by promoting them to public API, or is there an appetite for a small shared `text_utils.py` used by `buddy.py`, `storm_roster_builder.py`, etc., given the docstring already says buddy.py "mirrors train_rotation" conventions? Worth deciding once, since the same underscore-reuse pattern likely recurs in other `_ui` files across the sweep.
