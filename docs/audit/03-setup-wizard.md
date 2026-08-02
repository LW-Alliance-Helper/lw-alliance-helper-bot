# Section 03 — Setup Wizard

## 1. Files covered

- `setup_cog.py` — 11,166 lines
- `setup_hub.py` — 528 lines
- `wizard_registry.py` — 205 lines

## 2. Summary

`wizard_registry.py` and `setup_hub.py` are in good shape: small, single-purpose, well-documented, and used consistently. `setup_cog.py` is the largest file in the repo by a wide margin and genuinely earns "god file" status — not primarily because it hosts many independent wizards (that part is defensible: Train/Growth/Buddy/Survey/Storm/Event/Birthday/Shiny Tasks are each their own feature domain and could each become their own module with minimal risk), but because the wizards don't share enough: the same view-close/disable-buttons boilerplate is hand-copied 50+ times instead of factored into a base class, one wizard step function (`_run_structured_flow_setup_step`) alone runs ~1,370 lines, and a cancel/resource-cleanup bug (unregister-on-every-return vs. unregister-once-at-the-end) is inconsistently applied across wizards. The macro-level design pattern the repo documents for wizards ("Keep current / Use default, cancellable, re-entry summary") is followed faithfully at the entry-point level across all ten wizards, which is the section's strongest asset.

## 3. Findings

### Critical

- **[Blocking I/O on event loop] `setup_cog.py:9160-9162`** — Inside the survey Date-Modified shortcut lookup, `get_spreadsheet(guild_id)` and `sh.worksheet(survey_tab)` are both called directly (synchronous gspread/Google API network calls) on the event loop; only the third, immediately-following call (`ws.row_values(1)`) is wrapped in `asyncio.to_thread`. The surrounding comment even says "Off the event loop in case the sheet is slow / rate-limited," but only 1 of the 3 blocking calls actually is. Compare with the correct pattern used elsewhere in the same file at `setup_cog.py:9648-9652` and `setup_cog.py:9713-9717`, where the whole gspread call is wrapped.
  **Recommendation**: wrap the whole three-call sequence (`get_spreadsheet` → `.worksheet()` → `.row_values()`) in a single `asyncio.to_thread(...)` call (or a small sync helper function passed to `to_thread`), matching the pattern already used two hundred lines later in the same function.

### High

- **[God file / natural split seams] `setup_cog.py` (11,166 lines)** — Well past the "strong signal to split" threshold (~2,000 lines) by more than 5x. Mapped seams, in file order:
  - `1-1800`: shared framework — generic view/modal classes (`CreateRoleModal`/`RoleSelectStep` 184-356, `CreateChannelModal`/`ChannelSelectStep` 358-750, `ConfirmView`/`TextInputModal`/`ModalLaunchView` 751-864), shared prompt helpers (`ask_keep_or_change` 865, `ask_proceed_with_existing_config` 990, `ask_disable_with_clear` 1060), `_manage_train_templates` 1149, permission gates (`_has_leadership_or_admin` 1393, `_check_wizard_can_run` 1453), `SetupCog` cog class 1493, all `_launch_*_setup` dispatchers 1532-1657, `_run_reset_flow` 1658, generic pickers (`TimezoneSelectView`, `ScheduleTypeView`, `YesNoView`) 1745-1856, `_send_view_configuration` 1857. → candidate `setup_shared.py`
  - `2076-2327` (~250 lines): `run_setup` (foundation wizard)
  - `2327-3428` (~1,100 lines): Growth — `run_growth_setup`, `run_growth_breakdown_setup`. → candidate `setup_growth.py`
  - `3428-4694` (~1,265 lines, plus `_manage_train_templates` at 1149): Train — `run_train_setup`, `_WeekdaySelectView`, `_RuleRoleAttachView`, `_run_train_rotation_step`. → candidate `setup_train.py`
  - `4694-5168` (~470 lines): Buddy — `run_buddy_setup`. → candidate `setup_buddy.py`
  - `5168-6174` (~1,000 lines): Survey — `run_create_new_extra_survey`, `run_remove_extra_survey`, `run_pick_survey_to_edit`, `run_survey_setup`. → candidate `setup_survey.py`
  - `6174-10130` (**~3,956 lines — over a third of the whole file**): Storm (DS/CS, shared) — `run_storm_setup`, `_run_participation_preset_picker_step`, `_run_storm_participation_step`, `_ask_signup_schedule`, `_normalise_hhmm`, `_KeepOrFlipYesNoGate`, `_InlineCreatePresetOffer`, `_InlineCreateMemberRuleOffer`, `_InlinePostFirstSignupOffer`, `_run_structured_flow_setup_step`, `_col_letter_to_index`/`_col_index_to_letter`, `_build_participation_question`, `wait_for_msg_simple`. → candidate `setup_storm.py`, the single highest-value split (see open question 1)
  - `10131-10293` (~160 lines): Event — `run_event_setup`
  - `10293-10794` (~500 lines): Birthday — `run_birthday_setup`
  - `10794-11165` (~370 lines): Shiny Tasks — `run_shiny_tasks_setup`

  **Recommendation**: do not attempt in this pass (per instructions), but this is a mechanical, low-risk extraction: every wizard is already dispatched through a thin `_launch_*_setup` function and `setup_hub.py` already imports each `run_*` function by name from `setup_cog`, so moving a wizard's code to its own module only requires updating the import line(s) in `setup_hub.py`/`setup_cog.py`, not touching call sites' logic.

- **[Duplication] View/modal boilerplate hand-copied instead of shared.** 52 function-local `discord.ui.View`/`Modal` class definitions exist in `setup_cog.py` (`grep -n "^\s\+class " setup_cog.py` → 52 matches, e.g. lines 903, 1017, 1104, 1199, 1273, 1669, 2519, 2579, 2636, 2682, 2741, 3053, 3151, 3199, 3279, 3300, 3665, 4214, 4240, 4486, 4509, 4929, 4951, 5248, 5271, 5317, 5548, 5630, 5708, 5887, 5990, 6388, 6490, 6637, 6747, 7263, 7704, 7892, 8545, 8579, …). The "disable every button then stop the view" idiom (`for x in self.children: x.disabled = True`) appears 108 times; `.disabled = True` appears 126 times. A minority of call sites correctly reuse the existing `wizard_registry.safe_edit_response` helper (e.g. `setup_cog.py:280`, `307`, `5733`, `5756`), but several others hand-duplicate the same 4-line try/except-around-`edit_original_response` pattern instead, e.g. `setup_cog.py:3239-3244`, `3340-3345`, `4258-4263`, `4555-4560`, `5006-5011`.
  **Recommendation**: extract a small mixin/base (e.g. `_WizardStepView(discord.ui.View)` with a `disable_and_stop(self, interaction)` method that calls `wizard_registry.safe_edit_response`) and have the ad-hoc nested views inherit/call it instead of re-writing the same lines.

- **[Long function] `_run_structured_flow_setup_step` — `setup_cog.py:8373-9742` (~1,370 lines)** — the single largest function in the file. One function handles: premium opt-in gate, power-metric source/column config, sub mode, signup channel, all 5 sheet-tab names, stale-power DM nudge config (4 sub-fields), and roster DM template capture, all sequentially in one body.
  **Recommendation**: split into named per-sub-step private helpers (`_ask_power_metric_source`, `_ask_sub_mode`, `_ask_stale_power_dm`, `_ask_roster_dm_templates`, …) called in sequence from a slim orchestrator, mirroring the pattern the file already uses for the train wizard's step 9 (`_run_train_rotation_step`, `setup_cog.py:4126`, which itself documents "owns … every rotation setting as its own lettered, gated sub-step (9a..9h) so it never floods the user").

- **[Resource leak / inconsistent cleanup] Wizard cancel-event never unregistered on early return in several wizards.** Every wizard entry point calls `wizard_registry.register(user.id)` once at the top and is expected to call `wizard_registry.unregister(user.id, cancel_event)` when it's done (per `wizard_registry.py`'s own module docstring, which shows this exact `try/finally` usage as canonical). In practice:
  - `run_setup` (`setup_cog.py:2076-2327`) calls `unregister` exactly once, at line 2323 (the success path). The function has 15 `return` statements before that (cancel/timeout early-exits at lines 2104, 2131, 2134, 2156, 2159, 2185, 2188, 2201, 2204, 2231, 2234, 2270, 2273, 2298, 2301) — every one of them skips `unregister`.
  - The same single-unregister-at-the-bottom pattern recurs in `run_growth_setup` (2327-2890, unregister only at 2886), `run_train_setup` (3428-3989, unregister only at 3965), and `run_storm_setup` (6174-7206, unregister only at 7158) — all of which also have multiple `view.cancelled`/timeout early returns upstream of that single unregister call.
  - By contrast, `run_buddy_setup`, `run_survey_setup`, `run_birthday_setup`, and `run_shiny_tasks_setup` call `unregister` at *every* return point (verbose, but correct).

  Net effect: every time a leadership member cancels or lets one of the four affected wizards time out mid-flow, an `asyncio.Event` is left permanently in `wizard_registry._active[user_id]`. Nothing currently reads `wizard_registry.is_active()` (confirmed via repo-wide grep), so this is not user-visible today, but it's an unbounded per-process memory leak and a latent bug if `is_active()` is ever used to gate "you already have a wizard open."
  **Recommendation**: wrap each wizard body in `try: ... finally: wizard_registry.unregister(user.id, cancel_event)` (as the module's own docstring already recommends) instead of a single call at the bottom — this both closes the leak and removes the need for per-branch unregister calls in the wizards that currently do it verbosely.

### Medium

- **[Missing type hints, cross-module functions] All 13 `run_*_setup` wizard entry points are missing a return type and leave `bot` untyped**, e.g. `setup_cog.py:2076` — `async def run_setup(interaction: discord.Interaction, bot):`. Same shape at lines 2327, 2890, 3428, 4694, 5168, 5227, 5307, 5362, 6174, 10131, 10293, 10794. These are exactly the functions imported and called from other modules (`setup_hub.py`, `member_roster.py`, `transfer_setup.py`), i.e. the file's primary cross-module surface per the standards doc's emphasis.
  **Recommendation**: annotate `bot: commands.Bot` and `-> None` (all of them are call-and-forget coroutines).

- **[Observability] `print()` used instead of `logging` throughout `setup_cog.py`.** Zero uses of the `logging` module in the file; at least 11 `print(f"[SETUP] ...")` calls used as operational logging (e.g. `setup_cog.py:10290`, `10790`, `11162`), while the sibling `setup_hub.py:22,29` properly sets up `logger = logging.getLogger(__name__)` and uses it. This means setup-wizard completions/failures aren't filterable or routable the way the rest of the hub layer's logs are.
  **Recommendation**: switch to `logger.info(...)`. Note `member_roster.py` has the same mix (10 prints, 2 logger calls) — see open question 2 on scope.

- **[Duplication] Redundant local `import wizard_registry` statements.** `wizard_registry` is already imported at module level (`setup_cog.py:27`) and `wait_view_or_cancel` is imported by name (`setup_cog.py:54`), yet 15 functions re-import the module locally anyway: lines 1169, 2077, 2329, 2908, 3430, 4701, 5377, 6176, 7229, 7441, 7873, 8406, 10135, 10295, 10798. Unlike other local imports in this file (which plausibly dodge circular imports with `config.py`/`premium.py`), there's no such justification here — the top-level import already works.
  **Recommendation**: drop the 15 redundant local imports.

- **[Duplication] Identical text duplicated across a DS/CS branch.** `setup_cog.py:6247-6263` — the `if event_type == "DS": ... else: ...` block sets `placeholder_info` to the exact same 4-line string in both arms; only `default_template` actually differs between DS and CS.
  **Recommendation**: hoist the shared `placeholder_info` string out of the branch.

- **[Inconsistency] `run_event_setup` skips the "already configured, edit or cancel?" gate every other wizard has.** `setup_cog.py:10131-10289` never calls `ask_proceed_with_existing_config`, unlike all nine sibling wizards (`run_setup:2089`, `run_growth_setup:2396`, `run_growth_breakdown_setup:2972`, `run_train_setup:3506`, `run_buddy_setup:4743`, `run_survey_setup:5439`, `run_storm_setup:6323`, `run_birthday_setup:10365`, `run_shiny_tasks_setup:10837`). It's plausible this is intentional (each of its 4 steps already offers its own keep-current button, so the summary gate may be redundant) — flagged as open question 3 rather than a hard violation.

- **[Cross-module coupling] Other cogs import setup_cog's underscore-prefixed "private" helpers directly.** `survey.py:1607,1792` and `train_hub.py:69` do `from setup_cog import _format_time_with_tz` / `_parse_12h_time` rather than these living in a neutral shared module. This makes `setup_cog.py` a load-bearing dependency for cogs whose feature domain has nothing to do with setup, and works against ever splitting the file up (see High finding above) since those two functions would need to stay behind or be re-exported.
  **Recommendation**: extract `_parse_12h_time`, `_format_24h_to_12h`, `_format_time_with_tz`, `_parse_month_day` (`setup_cog.py:63-183`, no dependency on anything else in the file) into a small `time_utils.py`. `tests/unit/test_time_utils.py` already exists and currently tests these via `setup_cog` imports, suggesting the extraction was half-anticipated.

- **[Parameter count] `_run_structured_flow_setup_step` (`setup_cog.py:8373-8387`) takes 10 keyword-only parameters.** Keyword-only args avoid the "boolean flag soup" call-site risk the standards doc warns about, so this is a mild flag rather than a hard violation — it will likely resolve on its own once the High-severity decomposition of that function happens.

### Low

- **[Docstrings] `run_setup` (`setup_cog.py:2076`) is the only one of the 13 wizard entry points with no docstring.** Every sibling (`run_growth_setup`, `run_growth_breakdown_setup`, `run_train_setup`, `run_buddy_setup`, `run_survey_setup`, `run_storm_setup`, `run_event_setup`, `run_birthday_setup`, `run_shiny_tasks_setup`) has at least a one-line docstring.

- **[Nesting] Deep nesting in `run_survey_setup`'s inline question-editor sub-flow.** `setup_cog.py:5708-5760` — a locally-defined `QuestionListView` class (itself nested inside a `while True:` loop, inside a closure, inside `run_survey_setup`) reaches 10 indentation levels (40-space indents at lines 5721, 5744). Working correctly, but hard to read/step through.
  **Recommendation**: candidate for extracting to a module-level view class that takes `questions` as a constructor arg and exposes a `refresh(questions)` method instead of being rebuilt from scratch every loop iteration.

- **[Silent exception swallowing without logging]** Several `try: await inter.edit_original_response(...) except Exception: pass` sites (`setup_cog.py:3242-3245`, `3343-3345`, `4261-4264`, `4558-4561`, `5009-5012`) swallow the exception with no log line. Low severity because they're immediately followed by `self.stop()` on a view that's ending anyway, but a `logger.debug` would help diagnose "the wizard silently didn't update" reports.

## 4. What's already optimal

- **`wizard_registry.py`** is a small (205 lines), well-documented, single-purpose shared utility. Every public function (`register`, `unregister`, `cancel_user`, `wait_or_cancel`, `wait_view_or_cancel`, `safe_edit_response`, `expire_view_message`) has a docstring that names the exact Discord-specific failure mode it exists to guard against (interaction-token expiry inside `safe_edit_response`, the /cancel-vs-view-timeout race inside `wait_view_or_cancel`). This is the strongest file of the three reviewed.
- **`setup_hub.py`** is an appropriately-sized (528 lines), genuinely thin dispatcher exactly as its module docstring promises: it owns only the hub embed builder and the button grid, and every one of its 15 button callbacks is a 2-3 line stub that imports and calls into the real wizard body elsewhere. No business logic or Discord-API-heavy work lives here.
- **The "Keep current / Use default, cancellable, re-entry summary" pattern is applied consistently at the entry-point level.** All 10 top-level wizard functions register a `wizard_registry` cancel event at the very start; `ask_keep_or_change` (38 call sites across every feature wizard) and `ask_proceed_with_existing_config` (9 of 10 entry points) are genuinely shared, well-documented helpers rather than being reimplemented per wizard — the file follows its own documented convention here even where it fails to share other boilerplate (see High findings).
- **`RoleSelectStep`/`ChannelSelectStep`** (`setup_cog.py:202-750`) are well-designed reusable components: clear docstrings, a documented `is_current_stale` property, and a deliberate, explained fallback path for pre-migration guilds that stored roles by name only (`setup_cog.py:237-250`). These are reused by essentially every wizard in the file rather than being redefined per-wizard — the opposite pattern from the ad-hoc nested views flagged above.
- No bare `except:` clauses, no mutable default arguments, and no hardcoded secrets/tokens found anywhere across all three files.

## 5. Open questions

1. Is the Storm section (DS/CS, ~3,956 lines — over a third of `setup_cog.py`) meant to become its own module (`setup_storm.py`, mirroring the existing standalone-cog pattern in `transfer_setup.py`), or is keeping it alongside the other wizards intentional? This is the single highest-value split candidate identified above.
2. Should the `print()` → `logging` cleanup in `setup_cog.py` (Medium finding) be scoped to this file, or ride along with a bot-wide sweep? `member_roster.py` has the same print/logger mix, suggesting it isn't unique to setup.
3. Is `run_event_setup`'s missing `ask_proceed_with_existing_config` gate (Medium finding) intentional given its short 4-step flow, or an oversight left over from the #249 event-hub split that should be backfilled for consistency with the other 9 wizards?
4. The wizard cancel-event leak (High finding) is currently harmless because nothing calls `wizard_registry.is_active()` anywhere in the repo today. Worth confirming there's no near-term plan to use `is_active()` as a "you already have a wizard open" guard before deciding how urgently to fix it.
