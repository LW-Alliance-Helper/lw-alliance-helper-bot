# Storm Core & Signup — Code Quality Audit

Scope: the shared Storm Core + Signup surfaces used by both Desert Storm (DS)
and Canyon Storm (CS). Two other sections (storm-data, storm-strategy-render)
cover the rest of the storm feature area in parallel; findings here are
limited to the files below.

## 1. Files covered

| File | Lines |
|---|---|
| `storm.py` | 1599 |
| `storm_commands_root.py` | 84 |
| `storm_date_helpers.py` | 473 |
| `storm_event_hub.py` | 554 |
| `storm_icons.py` | 136 |
| `storm_permissions.py` | 117 |
| `storm_walkthrough.py` | 424 |
| `storm_signup_post.py` | 698 |
| `storm_signup_scheduler.py` | 226 |
| `storm_signup_view.py` | 1067 |
| **Total** | **5378** |

## 2. Summary

The newer files in this section (`storm_date_helpers.py`, `storm_permissions.py`,
`storm_event_hub.py`, `storm_signup_post.py`, `storm_signup_view.py`,
`storm_signup_scheduler.py`) are in good shape: consistent use of `logger`
over `print`, blocking gspread/Discord calls correctly offloaded with
`asyncio.to_thread` / `run_in_executor`, defer-before-I/O discipline on
interaction handlers, and unusually good docstrings that explain *why*,
including references to past bugs they fixed. `storm.py` is the outlier —
it predates the hub/permissions refactor, still uses `print()` instead of
`logging`, reimplements its own (buggy) permission check instead of routing
through `storm_permissions`, and carries near-total DS/CS logic duplication
that the rest of the section avoided by using a single parameterized
implementation. The scheduler has one correctness risk worth Kevin's
attention: it computes "now" fresh per-guild inside a shared per-minute
tick using exact-minute equality, which can silently skip a guild's
weekly auto-post if the tick runs long.

## 3. Findings

### Critical

- **`storm_signup_scheduler.py:33-45, 64-85, 106-192` — per-guild "now" recomputed inside a shared 1-minute tick, exact-minute match can silently skip a guild's weekly post.**
  `_run_one_tick` iterates every scheduled guild row and, for each one, calls
  `_guild_today_and_now(tz_name)` which does `_dt.datetime.now(tz)` at that
  point in the loop — not once at tick start. `_should_fire_now` then requires
  an *exact* `now.hour == signup_time.hour and now.minute == signup_time.minute`
  match. If processing earlier guilds in the same tick (Discord `channel.send`,
  sheet reads, premium checks) pushes wall-clock time past the target minute
  before a later guild's row is reached, that guild's post is silently
  skipped for the tick — and since the schedule is "this weekday at this
  time," the next opportunity for that guild is not next tick, it's next
  week (the day-of-week won't match again for 7 days). There's no window
  tolerance and no catch-up for this specific case (the `#227` heartbeat
  catch-up scan covers full-tick/outage misses, not per-guild drift within a
  running tick).
  Recommendation: capture `now`/`today` once per tick (not per-row), and
  either (a) widen the match to a tolerance window (e.g. "signup_time <= now
  < signup_time + loop interval") with the existing `has_registration_post`
  idempotence guard doing the de-dup, or (b) track last-checked-minute state
  so a tick that starts late still catches a minute it hasn't fired for yet.
  Low risk today at small guild counts, but it's a latent bug that gets worse
  precisely as the bot scales — the failure mode is a silent no-post with
  only a missing log line, which is hard to notice.

### High

- **`storm.py:840-851` (`_guard`) reimplements permission checking outside `storm_permissions.is_leader_or_admin` and drops the admin bypass.**
  `storm_permissions.py`'s own docstring (`storm_permissions.py:16-22`)
  describes exactly this bug class: "the first round of structured-flow
  cogs each invented their own permission check... silently locking out
  non-admin officers." `storm.py`'s `_guard` does:
  ```python
  if cfg.leadership_role_name not in [r.name for r in interaction.user.roles]:
  ```
  with no `member.guild_permissions.administrator` bypass at all — a server
  admin without the configured leadership role is locked out of
  `handle_storm_draft`/`handle_storm_overview`. Today this is masked because
  the only live entry point, the hub's `📄 Generate mail` button
  (`storm_event_hub.py:416-419`), already gates on `is_leader_or_admin`
  before the hub even opens — but `_guard` is still exported/callable
  directly, is inconsistent with every other storm surface, and reintroduces
  the exact bug `storm_permissions.py` was built to eliminate in one place.
  `storm.py` never imports `storm_permissions` at all (confirmed via grep —
  zero references to `is_leader_or_admin`/`deny_non_leader` in the file).
  Recommendation: replace `_guard` with a call into
  `storm_permissions.is_leader_or_admin` / `deny_non_leader`, and drop the
  hand-rolled denial message.

- **`storm.py` uses `print()` for all error handling instead of `logging`.**
  Every other file in this section (`storm_signup_post.py`,
  `storm_signup_view.py`, `storm_signup_scheduler.py`, `storm_event_hub.py`,
  `storm_walkthrough.py`, `storm_icons.py`) uses `logger = logging.getLogger(__name__)`
  consistently. `storm.py` has no `logging` import and instead does
  `print(f"[STORM] Error loading Team {team} assignments: ...")` at
  `storm.py:201-204`, `260-263`, `1098-1104`, `1153-1159`, plus informational
  prints at `191`, `194`, `255`, `1093`, `1096`, `1152`. `print()` output
  doesn't carry a log level, doesn't route through whatever log
  aggregation/alerting the rest of the bot relies on, and is easy to miss in
  production. Recommendation: add `logger = logging.getLogger(__name__)` and
  swap the `print(...)` calls for `logger.warning(...)`/`logger.info(...)`
  (matching the `[STORM]` prefix convention already used elsewhere via
  `logger.warning("[STORM SIGNUP] ...")`).

- **`storm.py` — near-total DS/CS logic duplication instead of one parameterized implementation.**
  Every DS function has a hand-copied CS twin with the same shape and only
  the zone-structure/sheet-key details changed:
  `load_ds_assignments` (135-207) / `load_cs_assignments` (1060-1105),
  `save_ds_assignments` (209-263) / `save_cs_assignments` (1108-1159),
  `build_ds_template` (269-291) / `build_cs_template` (1202-1231),
  `parse_ds_template` (294-353) / `parse_cs_template` (1234-1301),
  `build_ds_mail` (359-462) / `build_cs_mail` (1307-1403),
  `run_ds_draft_flow` (671-834) / `run_cs_draft_flow` (1450-1599),
  `StormApprovalView` (564-607) / `CSApprovalView` (1409-1444, byte-for-byte
  identical except the hardcoded "Desert Storm"/"Canyon Storm" label and
  whether `subs` is threaded through separately).
  This is the same feature implemented twice; any bugfix or behavior change
  (e.g. the scheduler-related date fixes, the emoji-prefix feature, the
  legacy-subs-flattening logic) has to be manually ported to both halves,
  and it's easy to fix one side and forget the other (note `save_cs_assignments`
  and `save_ds_assignments` already independently reimplement the same
  "flatten legacy tuple sub" branch at `246-248` and `1131-1134`). This file
  is the clear outlier in the section — the sibling files
  (`storm_signup_post.py`, `storm_date_helpers.py`) all use a single
  `event_type`-parameterized implementation instead. Given the file is
  already at 1599 lines (well into "above ~2000 is a strong signal" territory
  for a second file), flagging the DS/CS duplication as the natural seam for
  a future split into a shared `_storm_mail_core.py` (or similar) driven by a
  small per-event-type structure table, rather than two parallel code paths.
  Not asking for the refactor here per the audit's scope — just naming the
  seam.

- **`storm_signup_view.py` is 1067 lines mixing persistent-View UI, vote-recording business logic, Sheet mirroring/pruning, and DM-nudge logic in one module.**
  The file covers: custom_id encoding/parsing (47-108), poll-embed rendering
  (125-183), the `SignupView` UI class (186-286), the vote click handler +
  its business rules (289-515), the power-refresh DM nudge business logic
  (518-740), the leadership "View sign-ups" click handler (743-852), raw
  Sheet mirror/prune I/O (855-1018), and startup view re-registration
  (1021-1067). Per standards §1 ("flag any file that mixes Discord UI,
  business logic, and data/sheet I/O in one place") and the repo's own
  `_logic`/`_cog`/`_ui` split convention (e.g. `buddy.py`/`buddy_cog.py`/
  `buddy_hub.py`/`buddy_ui.py`), this is a candidate for splitting into a
  UI module (`SignupView` + custom_id helpers) and a logic module (vote
  recording, DM nudge, Sheet mirror/prune). Flagging as a future seam, not
  asking for the split now.

### Medium

- **`storm.py:129-132, 135, 152, 209, 222, 359, 364, 1060, 1065, 1108, 1113, 1308` — `guild_id: int = None` pattern throughout instead of `Optional[int] = None`.**
  PEP 484: a parameter typed `int` with default `None` is not valid under
  strict type checking (`None` is not an `int`). This shape repeats across
  `_get_spreadsheet`, `load_ds_assignments`, `save_ds_assignments`,
  `build_ds_mail`, `load_cs_assignments`, `save_cs_assignments`,
  `build_cs_mail`. Every other file in the section correctly uses
  `Optional[...]` (e.g. `storm_signup_post.py`'s `Optional[str]`,
  `storm_date_helpers.py`'s `Optional[_dt.date]`). Recommendation:
  `from typing import Optional` and change these signatures to
  `guild_id: Optional[int] = None`.

- **`storm.py:926-1015` (`handle_storm_overview` / `_show_storm_overview`) — dead code, no callers.**
  Grepped the whole repo: `handle_storm_overview` has zero callers outside
  its own definition (the hub only wires `handle_storm_draft` at
  `storm_event_hub.py:416-419`; there is no "overview" button in the hub's
  button grid, and no other module imports it). The function still
  references the retired `/setup_*storm` slash-command naming in its output
  copy (`storm.py:940-943`: "the per-event `/setup_*` slash commands were
  retired... point officers at the hub"), which is itself evidence this path
  is stale rather than intentionally-kept. Recommendation: confirm with
  Kevin whether this is truly unreachable and, if so, remove it (it also
  drags `_guard`'s buggy permission check along as dead weight — see the
  High finding above).

- **`storm.py:1009-1010` and similar — raw exception text surfaced verbatim to Discord users.**
  `_show_storm_overview`'s except branch does
  `embed.add_field(name="Current Mail Template", value=f"⚠️ Could not load: {e}", ...)`
  — the raw `Exception` string (which could include sheet names, internal
  paths, or API error bodies) is sent straight into a Discord embed instead
  of a generic message + server-side log. Recommendation: log the exception
  with `logger.warning(...)` and show officers a generic
  "⚠️ Could not load the template — check bot logs" instead.

- **`storm_event_hub.py:99-102, 103-106, 140-145, 148-152` — silent broad `except Exception` in the embed builder, no logging.**
  `_build_event_hub_embed` swallows config/preset/date lookup failures with
  bare `except Exception: <fallback>` and no log line at all (contrast with
  `handle_event_hub`'s own premium-check except at `493-500`, which does log
  a warning). These are presentation-layer fallbacks so failing soft is the
  right behavior, but a silent swallow means a persistently-broken sheet
  lookup or config read would never surface anywhere. Recommendation: add a
  `logger.debug(...)` (not necessarily `warning`, to avoid noise) in each of
  the four except blocks so the failure is at least traceable.

- **Duplicated guild-local-time helper: `storm_signup_post.py:96-110` (`_today_in_guild_tz`) vs `storm_signup_scheduler.py:33-45` (`_guild_today_and_now`).**
  Both independently implement "resolve the guild's configured tz name,
  fall back to UTC on empty/invalid, return `datetime.now(tz)`." The
  scheduler's docstring even says "same convention as `_today_in_guild_tz`
  in storm_signup_post" — acknowledging the duplication rather than
  importing it. `storm_date_helpers.py` is supposed to be the single source
  of truth for date/time handling in this section per its own module
  docstring, but neither of these lives there. Recommendation: move one
  canonical `guild_now(guild_id_or_tz_name)` (returning both date and time,
  or just the tz-aware datetime and let callers `.date()`/`.time()`) into
  `storm_date_helpers.py` and have both call sites import it.

- **Long functions mixing validation/business-logic/formatting: `storm.py:671-834` (`run_ds_draft_flow`, ~164 lines), `storm.py:1450-1599` (`run_cs_draft_flow`, ~150 lines), `storm_signup_post.py:442-631` (`_run_post_signup_confirm_flow`, ~190 lines).**
  Each walks a multi-step wizard (validation → nested View class definitions
  → business logic → Discord message formatting) in one function body, well
  past the ~60-80 line guideline. `_run_post_signup_confirm_flow` in
  particular defines two nested view classes (`_ConfirmView`, `_SlotPick`)
  inline and does per-team recursive-shaped picking. Recommendation:
  extract the nested view classes to module level (easier to unit test in
  isolation) and split each flow into per-step helper functions (mirrors
  the "Step N of 4" comments that already mark the natural seams).

- **`storm_walkthrough.py` — timeout handling duplicated by hand instead of reusing the shared `wizard_registry.expire_view_message` helper.**
  `storm_event_hub.py:235-241` uses the canonical
  `wizard_registry.expire_view_message(self.message, command_hint=...)` on
  view timeout. `storm_walkthrough.py`'s `_OfferView.on_timeout` (270-281)
  and `_TourStepView.on_timeout` (413-424) instead hand-roll the same
  "disable every child, edit the message, swallow `HTTPException`" logic
  independently. Not wrong, just inconsistent with the canonical helper
  used one file away in the same feature. Recommendation: check whether
  `wizard_registry.expire_view_message` covers plain-content (non-embed)
  messages; if so, route both `on_timeout` methods through it.

### Low

- **`storm.py:27-28` — unused imports `json` and `os`.** Grepped the file;
  neither `json.` nor `os.` is referenced anywhere. Safe to remove.

- **`storm_walkthrough.py` — the denial string `"⛔ This walkthrough was offered to someone else."` is duplicated 4 times** (`_OfferView.accept` line 217-220, `_OfferView.decline` line 246-249, `_TourStepView._make_next`'s inner closure line 347-350, `_make_skip`'s inner closure line 367-370). Recommendation: hoist to a module-level constant.

- **Locally-scoped View classes defined inside handler functions** (`storm.py`'s `StormTemplatePickView` inside `_pick_storm_template` at 634-654; `storm_signup_post.py`'s `_ConfirmView` inside `_run_post_signup_confirm_flow` at 494-521 and `_SlotPick` inside the nested `_pick` closure at 556-593). Functionally fine (closures over local state are a legitimate discord.py pattern for capturing per-invocation defaults), but it makes these classes untestable in isolation and adds to the enclosing function's length — noted alongside the long-function finding above rather than as a separate ask.

- **`storm_permissions.py:56-66` (`deny_non_leader`) and hand-copied denial messages elsewhere aren't always reused.** `storm_signup_view.py:761-769` (`_handle_view_signups_click`) calls the canonical `is_leader_or_admin` but then writes its own inline denial text ("🔒 Leadership only. This shows the full sign-up breakdown.") instead of calling `deny_non_leader`. Intentional here since the copy is contextual ("this shows the full breakdown" vs. the generic denial), but worth a note in case Kevin wants one canonical denial string everywhere.

- **`storm_icons.py:80-88` (`refresh_zone_emoji_ids`) — non-atomic `dict.clear()` then repopulate.** Between `ZONE_EMOJI_IDS.clear()` and the loop finishing, any concurrent render call sees a temporarily empty dict and silently falls through to plain-text zone names (per `zone_emoji_prefix`'s designed no-op fallback). Impact is purely cosmetic (a zone name briefly renders without its emoji during a reconnect), and the function is docstring-explicit that this is the intended fail-soft behavior, so this is genuinely low severity — noting only because it's a module-level mutable dict mutated outside a lock per standards §4.

## 4. What's already optimal

- **`storm_date_helpers.py` is a strong single-source-of-truth module.** Every
  display formatter, the permissive officer-typed-date parser, the
  game-defined-weekday inference, and the tolerant `Last Updated` column
  parser (with its DMY/MDY column-level heuristic) live in one place with
  thorough docstrings explaining *why* each tolerance/fallback exists. Most
  of the section correctly imports from here rather than reimplementing
  (the guild-tz-now duplication noted above is the one gap).

- **`storm_permissions.py` is a clean, well-motivated canonical module.**
  Its docstring documents the exact class of bug it was built to prevent
  (non-admin officers locked out by ad hoc role checks; inconsistent
  premium gating), and `is_leader_or_admin` / `ensure_premium_structured`
  are consistently reused across `storm_event_hub.py`, `storm_signup_post.py`,
  and `storm_signup_view.py`. `storm.py` not routing through it (see High
  finding) is the exception, not the rule.

- **Blocking-call discipline is consistently good outside `storm.py`'s prints.**
  `storm_event_hub.py:506-511` explicitly wraps a gspread-touching embed
  builder in `asyncio.to_thread` with a comment citing the real production
  symptom it fixed (10-20s Railway boot-path stalls). `storm_signup_view.py`
  offloads the roster-power Sheet read (`582-587`), the power-column-header
  lookup (`650-654`), and the Sheet mirror/prune writes (`476-485`) the same
  way. `storm.py`'s DS/CS assignment loads and saves also correctly go
  through `run_in_executor` at every call site, even though the file's error
  handling is otherwise the weak point of the section.

- **`storm_signup_scheduler.py`'s idempotence and defense-in-depth design is solid** aside from the per-tick timing issue flagged above: `force=False` + `has_registration_post` prevents double-posting across ticks, the fire-time Premium re-check (`142-155`) explicitly guards against a guild that downgraded between setup and fire time (with a comment explaining exactly why the disk-persisted `structured_flow_enabled` flag alone isn't sufficient), and the whole tick is wrapped in `try/except Exception: logger.exception(...)` so one bad guild can't kill the loop task for everyone else.

- **`storm_signup_view.py`'s DM-nudge cooldown race handling is a good example of getting concurrency right.** The docstring at `525-538` explicitly documents an earlier ordering bug (SELECT → DM → INSERT allowing two concurrent clicks to both pass) and the fix (INSERT-first, `record_power_refresh_dm_sent` returns True only on a fresh insert), plus a compensating "back out the cooldown row" path (`727-740`) if the DM send itself fails with a transient HTTP error so a flake doesn't permanently silence the nudge.

## 5. Open questions for Kevin

- Is `storm.py`'s `handle_storm_overview` / `_show_storm_overview` genuinely
  dead (safe to delete), or is there a plan to re-wire it into the hub
  (e.g. as a future "Overview" button)? It's currently unreachable but
  still maintained-looking code with its own bug (the `_guard` permission
  gap).
- Is `storm.py`'s DS/CS duplication intentional near-term (e.g. because DS
  and CS zone structures are expected to diverge further and a shared
  abstraction would fight that), or is it debt from before the event-hub
  unification (#187) that's now worth collapsing given the rest of the
  section already parameterizes on `event_type`?
- For the scheduler's per-tick timing issue (Critical finding above): is
  there a rough ceiling on how many guilds are expected to have
  `structured_flow_enabled` + a configured schedule concurrently? That
  determines how urgent the fix is versus how long the exact-minute-match
  window has actually been safe in practice.
- Should `storm_signup_view.py`'s split (UI vs. vote/DM/Sheet logic) happen
  now while it's ~1067 lines, or wait until it crosses further into
  "definitely needs a split" territory? Flagged as a seam, not an urgent ask.
