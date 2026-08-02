# Audit: Core Bot Entry & HTTP API

## 1. Files covered

| File | Lines | Role |
|---|---|---|
| `bot.py` | 2075 | Main entry point: client construction, event handlers, background `tasks.loop`s, `/growth`, `/events`, `/help`, `/admin` command groups, global slash-command error handler |
| `bot_state.py` | 48 | Cross-module singleton holding the running event loop + bot instance, dodging the `__main__`-vs-`bot` dual-import trap |
| `api_server.py` | 176 | aiohttp app assembly + lifecycle for the Map Manager (MM) ↔ bot HTTP integration |
| `api/routes/guilds.py` | 177 | Credentialed guild-link / member-tier / config / storm-vote reads |
| `api/routes/sheets.py` | 444 | Credentialed Sheet-backed reads + OCR write-backs MM calls |
| `api/auth.py` (read for context, not a primary target) | 63 | `requires_api_key` Bearer-token gate shared by every route above |
| `api/__init__.py` (read for context) | 27 | `BOT_KEY` app-key plumbing |
| `Procfile` | 1 | `web: python bot.py` — single Railway web process, sane |

## 2. Summary

The HTTP API surface (`api_server.py`, `api/routes/guilds.py`, `api/routes/sheets.py`, `api/auth.py`) is in excellent shape: auth is fail-closed and constant-time, every blocking gspread/sqlite read is correctly offloaded with `asyncio.to_thread`/`asyncio.gather`, and the module docstrings double as an accurate spec of what's implemented vs. stubbed. `bot.py` is functionally solid and its error-visibility discipline (log + Sentry on nearly every `except`) is consistently good, but at 2075 lines it has crossed from "big" into "god file" territory — it mixes gateway event handlers, three separate slash-command groups, five background scheduler loops, and an owner-only admin toolkit in one module with no internal seams. There is one real event-loop-blocking bug (a direct gspread call in a button handler that has a `to_thread` sibling two lines away), plus a handful of smaller consistency and duplication items.

## 3. Findings by severity

### Critical

- **[async-blocking] `bot.py:1081`** — `run_now` button callback in `growth_slash`'s `GrowthActionView` calls `_run_growth_snapshot_inner(guild_id)` directly (a blocking gspread read+write), stalling the bot's single event loop for every guild/user until the Sheet round-trip completes. The sibling `breakdown` button in the **same class**, 20 lines later, correctly wraps the equivalent read in `asyncio.to_thread` (`bot.py:1102`). This is a live regression risk: whichever alliance clicks "Run Snapshot Now" freezes command handling for every other guild on the bot for the duration of the Sheets API call.
  - **Recommendation**: change line 1081 to `await asyncio.to_thread(_run_growth_snapshot_inner, guild_id)`, matching the `breakdown` button and the `/growth breakdown` slash command (`bot.py:1171`), which already do this correctly.

### High

- **[god-file] `bot.py` (2075 lines, whole file)** — Single module owns: bot construction + intents (1-101), welcome DM template + presence updater (104-155), `on_ready` (186-424, itself ~240 lines — see below), guild join/remove events (427-491), support-server join-watch listener + helpers (494-597), the global slash-command error formatter (600-703), five `tasks.loop` background jobs (706-978), `on_message`/`on_command_error` (980-998), the `/growth` command group (1001-1197), the `/events` command (1209-1217), `/help` (1223-1242), and the entire owner-only `/admin` group (1245-2068, ~820 lines — overview, guild_info, forget_guild + confirm view, shiny_servers, shiny_import, shiny_set, transfer_dump, verify + `_run_verify_scan`). This crosses the standards' "≥2000 lines → identify natural seams" threshold. Natural seams are already visible in the section-comment banners the file itself uses (`# ── ... ──`), which is good evidence a split is mechanical, not a redesign:
  - `admin_group` and its ~10 subcommands/helpers → `admin_cog.py` (mirrors the existing `train.py`/`survey.py`/etc. cog pattern the repo already uses elsewhere)
  - `growth_group` (`growth_slash`, `growth_breakdown_slash`, `GrowthActionView`) → could move into `growth.py` or a new `growth_cog.py`, consistent with the repo's `_logic`/`_cog` split convention called out in the standards doc
  - the five `tasks.loop` jobs (`growth_task`, `stats_publish_task`, `shiny_tasks_refresh_task`, `shiny_tasks_post_task`) → a `scheduler_tasks.py`, leaving `bot.py` to just `.start()` them
  - `on_ready`'s cog-loading block, command-sync block, and background-task-startup block are each independently extractable functions
  - **Recommendation**: flagging only — do not split without Kevin's go-ahead, per the task brief. When a split happens, do it incrementally (admin group first — it's the largest, most self-contained chunk and has zero cross-references from the rest of `bot.py`).

- **[long-function] `bot.py:186-424`, `on_ready` (~240 lines)** — Mixes DB init, demo-guild reseeding, 11 sequential cog-load calls, global + per-guild command sync, presence update, install-metadata backfill for every guild, release-announcement checks for every guild, persistent-view re-registration (x2), emoji refresh, and (guarded by `_tasks_started`) starting 4 background loops + the HTTP API server + a delayed outage-catchup task. Each concern is individually well-commented, but the function as a whole does at least 8 unrelated things in one body.
  - **Recommendation**: extract into named steps, e.g. `_load_cogs(bot)`, `_sync_commands(bot)`, `_backfill_guild_metadata(bot)`, `_start_background_tasks(bot)`, called in sequence from a much shorter `on_ready`. Pure decomposition — no behavior change.

- **[coupling] `bot.py:1332`, `bot.py:1582`** — `admin_overview_slash` and `admin_shiny_servers_slash` both do `from config import _get_conn` (a leading-underscore, private-by-convention symbol) and hand-write raw SQL against it, rather than going through a public `config` accessor the way every other guild/data lookup in the file does (`get_config`, `get_guild_install_metadata`, `get_growth_config`, etc.). This is exactly the "reaching into another module's internals instead of a clear shared interface" smell the standards doc calls out. Both sites even have a `# noqa: PLC0415` / inline comment acknowledging the private import.
  - **Recommendation**: add small public functions to `config.py` (e.g. `get_admin_overview_counts()`, `get_shiny_servers_in_range(min, max)`) that own the SQL, and have `bot.py` call those instead of importing `_get_conn` directly. Keeps the "private = internal" convention meaningful and gives the queries a home that's testable independent of Discord.

### Medium

- **[rate-limit] `bot.py:1999-2017`, `_run_verify_scan`** — Iterates every non-bot member of a guild (`for m in sorted(members, ...)`) and, when a Verified role is configured, calls `await _try_assign_verified(m, role)` — a `member.add_roles()` Discord API call — once per member with no batching, delay, or 429/backoff handling. On a large guild (hundreds of members) this is a tight loop of individual write calls to a rate-limited API. Sequential `await`s provide some natural pacing, but there's no explicit handling if Discord starts returning 429s mid-scan (an unhandled `discord.HTTPException` here would abort the whole scan without a partial-progress report — though it's owner-only wrapped in a `try/except` further up the call chain via `on_app_command_error`, so it fails loud, not silently).
  - **Recommendation**: either bound this to guilds below some member-count threshold with a warning, or add a small `asyncio.sleep`/batch pattern à la the standards' "rate-limit / API-quota awareness" guidance. Low urgency since it's an owner-only manual tool, not a per-user command.

- **[async-blocking-consistency] `bot.py:715` (`growth_task`), `bot.py:1334-1354` (`admin_overview_slash`), `bot.py:1585-1591` (`admin_shiny_servers_slash`)** — These do direct synchronous `sqlite3.connect(...)`/`_get_conn()` + `.execute()`/`.fetchall()` calls inside `async def` bodies without `asyncio.to_thread`, unlike the gspread reads elsewhere in the same file (e.g. `bot.py:1102`, `bot.py:1171`, `bot.py:1817` all correctly use `to_thread`). Local SQLite reads are fast enough that this is unlikely to cause a visible stall in practice, but it's an inconsistent standard within the same file — the gspread-vs-sqlite distinction isn't documented anywhere as an intentional exception.
  - **Recommendation**: either standardize on `to_thread` everywhere for any blocking I/O (simplest, most defensible rule) or add a one-line comment at the top of `bot.py` (or in `config.py`) stating "SQLite reads are fast/local and don't need `to_thread`; gspread reads always do" so the inconsistency reads as a deliberate policy rather than an oversight.

- **[duplication] `bot.py:1305` (`_parse_guild_id`), `api/routes/sheets.py:49` (`_parse_guild_id`), `api/routes/guilds.py:32` (`_parse_int`)** — Three near-identical "parse an int, return None on failure" helpers with two different names for the same concept, one of them (`_parse_guild_id`) reused verbatim as a name across two different modules with different signatures (`bot.py`'s takes a raw string; `sheets.py`'s reads `request.match_info`). Not a bug, but a `grep`-for-`_parse_guild_id` will surface two unrelated functions, and a future contributor could easily assume they're the same.
  - **Recommendation**: rename one, or (better) extract a single `parse_int(raw: str) -> int | None` into a small shared module (e.g. `api/utils.py`) and have `guilds.py`/`sheets.py`'s request-parsing wrappers call it with `request.match_info[key]`.

- **[duplication] `api/routes/guilds.py:93`, `:163`, `api/routes/sheets.py:126`, `:249`, `:407`** — The pattern `bot = request.app[BOT_KEY]; guild = bot.get_guild(guild_id) if bot is not None else None` (sometimes followed by `guild.get_member(...)`) is repeated near-verbatim 5 times across the two route files.
  - **Recommendation**: extract a `_resolve_guild(request) -> discord.Guild | None` (and optionally `_resolve_member(request, guild_id, user_id)`) helper into a shared `api/routes/_common.py` or `api/utils.py`, imported by both route modules.

- **[long-function] `bot.py:1962-2061`, `_run_verify_scan` (~100 lines)** and **`bot.py:1751-1874`, `admin_transfer_dump_slash` (~120 lines, including a nested `_probe` closure)** — Both mix validation, data gathering, formatting, and Discord I/O in one body. Not urgent (owner-only diagnostic tools, low change frequency) but they're the two longest single functions in the file after `on_ready`.
  - **Recommendation**: low priority; only worth splitting if these get touched again for feature work.

### Low

- **[dead-code / unused-imports] `bot.py:6`, `:13-15`** — `import re` (line 6) is never referenced anywhere in the file (confirmed via search — zero `re.` usages). `post_editor`, `next_event_dates`, and `is_friday` are imported from `scheduler` (lines 13-15) alongside `run_scheduler`, but only `run_scheduler` is ever called (`bot.py:370`); the other three have no references in `bot.py`.
  - **Recommendation**: drop the unused `re` import and the three unused `scheduler` imports (verify they're not needed for a re-export contract elsewhere first — a quick grep for `bot.post_editor` etc. across the repo would confirm before deleting).

- **[docstrings] `bot.py`** — Most `/admin` and `/growth` subcommand handlers (e.g. `growth_slash`, `growth_breakdown_slash`, `events_slash`, `help_slash`) have no docstring, relying entirely on the `description=` kwarg passed to the decorator. That kwarg is user-facing (shown in Discord's slash-command picker) and reads fine for that purpose, but it's not a substitute for a docstring aimed at the next developer reading the source (what does the function return, side effects, etc.). Low priority since nothing outside `bot.py` imports these handlers (confirmed — no other module does `from bot import ...`).
  - **Recommendation**: optional; only worth adding where the decorator description doesn't already say enough (e.g. `_run_verify_scan`, `_try_assign_verified`, `_verification_line` already have good docstrings and are the right model to follow).

- **[state-shape] `bot.py:360` (`bot._tasks_started`), `bot.py:402` (`bot._api_runner`)** — Dynamically attached attributes on the `commands.Bot` instance rather than a declared/typed home (e.g. a small dataclass or `bot_state.py`, which already exists for exactly this kind of cross-cutting state). Not a race-condition risk in practice — `on_ready` handlers run sequentially on the single event loop, so `hasattr(bot, "_tasks_started")` can't be checked concurrently — but it's an implicit, untyped contract that only this one function knows about.
  - **Recommendation**: optional; could move both into `bot_state.py` alongside `event_loop`/`bot` for discoverability, but current usage is safe as-is.

## 4. What's already optimal

- **`api/auth.py`** is a model of "fail closed, not open": `requires_api_key` returns 503 (`service_unconfigured`) when `MAPMANAGER_API_KEY` is unset rather than treating an absent key as "auth disabled," and the token comparison uses `hmac.compare_digest` specifically to avoid timing side-channels. The module docstring states the threat model explicitly ("no key configured" must never collapse into "any key works"). This is exactly right for a shared-secret service boundary.
- **Blocking-I/O discipline in `api/routes/sheets.py`** is consistently correct: every gspread call is wrapped in `asyncio.to_thread`, and where multiple independent reads are needed (`sheet_roster`, `bot.py:118-122`... rather `sheets.py:118-122`) they're run concurrently via `asyncio.gather` instead of serially. The module docstring even explains *why* (`"gspread reads are blocking and the API server shares the gateway's event loop"`), so the pattern reads as a documented policy, not an accident.
- **`bot_state.py`** solves a genuinely subtle Python problem (the `__main__`-vs-`bot` dual-module-load trap from running `python bot.py` directly) cleanly and documents *why* the obvious approaches (module globals in `bot.py`, `bot.loop`) don't work, rather than just what the workaround is. This is exactly the kind of comment that saves the next person from "fixing" it back into a bug.
- **Error-visibility discipline in `bot.py`** is good overall: the overwhelming majority of `except Exception as e:` blocks both `print(...)` with a component tag (`[GROWTH]`, `[SHINY]`, `[GUILD]`, etc.) and call `sentry_sdk.capture_exception(e)` — genuinely rare to find a silent swallow. The two intentional exceptions (`_update_presence`'s `CustomActivity`→`Activity` fallback, and `on_command_error`'s `CommandNotFound` swallow) are both explained inline with *why* they're safe to not report.
- **`api/routes/sheets.py`'s `_etag_response`** (ETag + `If-None-Match` → 304) is a nice, unprompted bit of HTTP correctness for a roster endpoint MM presumably polls — cheap caching win that wasn't strictly required for the integration to function.
- **The global slash-command error handler** (`bot.py:611-703`, `_format_command_error`/`on_app_command_error`) turns raw Discord exceptions into categorized, actionable user messages (missing-channel-access vs. missing-permission vs. transient Discord error vs. genuine bug) each carrying a Sentry reference id for support correlation — well above the bar of a generic "something went wrong."

## 5. Open questions (Kevin's call, not clear-cut violations)

- Is the `_run_growth_snapshot_inner(guild_id)` direct call at `bot.py:1081` (Critical finding above) a known gap, or should it be treated as a plain bug to fix immediately given it's a live event-loop stall? It's the one finding in this section I'd flag as worth breaking the "audit only" rule for once reviewed together.
- Is `bot.py`'s size an accepted "it's the entry point, it's supposed to be big" design choice for now, or is a `admin_cog.py` extraction (the largest, most self-contained ~820-line chunk) worth scheduling? The repo already has the `_logic`/`_cog` convention elsewhere (`buddy.py`/`buddy_cog.py`/etc.), so the pattern to follow already exists.
- The SQLite-vs-gspread `to_thread` inconsistency (Medium finding above) — intentional exception for "local and fast," or worth normalizing to `to_thread` everywhere for a single easy-to-state rule?
- Should the `_run_verify_scan` per-member role-assignment loop get an explicit rate-limit guard, or has this simply never been exercised against a large enough guild to matter in practice?
- The three duplicated int-parsing / guild-resolution helpers (Medium findings above) are cheap to leave as-is; worth consolidating only if/when someone's touching those files for other reasons, or worth doing proactively as a quick follow-up?
