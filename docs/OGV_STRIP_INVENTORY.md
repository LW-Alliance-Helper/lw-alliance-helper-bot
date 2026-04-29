# OGV Seeding Inventory

> **Working document.** Pre-strip survey of every OGV-specific bit of seeding,
> hardcoded data, and special-case behaviour in the bot. Read-only inventory —
> nothing in here has been changed yet. Use this to plan the migration.

## Summary

- Total OGV references found: 176
- Files affected: 24 (`config.py`, `premium.py`, `bot.py`, `scheduler.py`, `setup_cog.py`, `storm_log.py`, `survey.py`, `train.py`, 16 test files, 2 doc files)
- Estimated effort to strip: **HIGH** — Database seeding runs on every startup; multiple seed functions insert hardcoded role names, Discord IDs, Google Sheet defaults, event templates, and survey questions; premium tier always includes OGV; fallback defaults use OGV templates throughout the codebase.

---

## A. Hardcoded OGV identifiers

### Discord Guild ID

- **`config.py:23`** — `OGV_GUILD_ID = 1266229297723605052`
  - What it is: Guild ID constant
  - Used by: Every `_seed_ogv_*()` function, `get_config(None)` fallback calls, test fixtures
  - Removal impact: Hard stop — this ID gates all OGV-specific seeding and premium status

### Discord Channel & Thread IDs (in `OGV_DEFAULTS` dict)

- **`config.py:25-39`** — `OGV_DEFAULTS` dictionary with snowflakes:
  - `leadership_channel_id`: `1488693874938482799`
  - `announcement_channel_id`: `1414725199257010336`
  - `leadership_category_id`: `1266243885743603783`
  - `member_role_id`: `1266235041600503880`
  - `survey_channel_id`: `1399401720026759198`
  - `survey_notify_channel_id`: `1405930574253920408`
  - `storm_log_thread_id`: `1483977424231469229`
  - `ds_log_channel_id`: `1483977424231469229`
  - `cs_log_channel_id`: `1483977424231469229`
  - `event_draft_channel_id`: `1488693874938482799`
  - `event_announce_channel_id`: `1414725199257010336`
  - What it is: All OGV's Discord infrastructure IDs
  - Used by: `init_db()` when seeding OGV config row
  - Removal impact: Deleting these removes OGV's entire seeded setup; must be replaced with `/setup` flow for all guilds including OGV

### Discord Channel IDs (in `seed_ogv_events`)

- **`config.py:1732-1748`** — Hardcoded in `seed_ogv_events()`:
  - `draft_channel_id`: `1488693874938482799` (appears 2x)
  - `announcement_channel_id`: `1414725199257010336` (appears 2x)
  - What it is: Event announcement channels for Marauder and Siege events
  - Used by: `seed_ogv_events()` called from `init_db()`
  - Removal impact: These hardcoded channels will be seeded every startup unless seed function is removed

### Discord Channel ID (in `_seed_ogv_train_config`)

- **`config.py:1596`** — `1488693874938482799` (OGV leadership channel for train reminders)
  - What it is: Default reminder channel for train schedule
  - Used by: `_seed_ogv_train_config()`
  - Removal impact: Will seed this channel ID to OGV's train config on every DB startup

### Role Names (in `OGV_DEFAULTS` and `GuildConfig` dataclass)

- **`config.py:31-32`** — `"OGV"` and `"OGV Leadership"` in `OGV_DEFAULTS` dict
- **`config.py:72-73`** — Same values as dataclass defaults
- **`config.py:135-136`** — Same role names as SQL table defaults
  - What it is: OGV-specific role names baked into schema defaults
  - Used by: Any guild created without explicit setup gets these role names
  - Removal impact: All new guilds and OGV will have "OGV" / "OGV Leadership" as fallback role names instead of generic defaults

---

## B. Database seeding on startup

### Main seeding orchestration

- **`config.py:122-507`** — `init_db()` function
  - Creates tables (lines 128-464)
  - Seeds OGV config row if not exists (lines 474-488)
  - Populates OGV's `spreadsheet_id` from env var (lines 490-504)
  - Calls `seed_ogv_events()` (line 507)
  - Print statements: `[CONFIG] Seeded OGV default config`, `[CONFIG] Persisted SPREADSHEET_ID env var to database for OGV`

### OGV-specific seed functions called by `init_db()`

1. **`config.py:208-209`** — `_seed_ogv_train_config(conn)` called during table setup
2. **`config.py:227`** — `_seed_ogv_growth_config(conn)` called during table setup
3. **`config.py:257`** — `_seed_ogv_survey_config(conn)` called during table setup
4. **`config.py:278`** — `_seed_ogv_birthday_config(conn)` called during table setup
5. **`config.py:325`** — `_seed_ogv_storm_config(conn)` called during table setup
6. **`config.py:507`** — `seed_ogv_events()` called at end of `init_db()`

### `_seed_ogv_train_config()`

- **`config.py:1574-1601`**
  - Inserts one row into `guild_train_config` for `OGV_GUILD_ID` if not exists
  - Seeded fields: themes (`OGV_DEFAULT_THEMES`), tones (`OGV_DEFAULT_TONES`), prompt_template (`OGV_DEFAULT_PROMPT`), reminder channel ID
  - Print: `[CONFIG] Seeded OGV train config`

### `_seed_ogv_growth_config()`

- **`config.py:1018-1040`**
  - Inserts one row into `guild_growth_config` for `OGV_GUILD_ID` if not exists
  - Hardcoded: `tab_source="Squad Powers"`, `name_col="D"`, metrics with cols E/G/H (OGV's sheet layout), `tab_growth="Growth Tracking"`
  - Print: `[CONFIG] Seeded OGV growth config`

### `_seed_ogv_survey_config()`

- **`config.py:1098-1113`**
  - Inserts one row into `guild_survey_config` for `OGV_GUILD_ID` if not exists
  - Seeded with `OGV_SURVEY_QUESTIONS` list and `OGV_SURVEY_INTRO` text
  - Print: `[CONFIG] Seeded OGV survey config`

### `_seed_ogv_birthday_config()`

- **`config.py:1376-1392`**
  - Inserts one row into `guild_birthday_config` for `OGV_GUILD_ID` if not exists
  - Hardcoded: `tab_name="Season 5 - Off-Season"`, `name_col=4`, `birthday_col=8`, `data_start_row=10`
  - Print: `[CONFIG] Seeded OGV birthday config`

### `_seed_ogv_storm_config()`

- **`config.py:719-741`**
  - Inserts two rows into `guild_storm_config` (DS and CS) for `OGV_GUILD_ID` if not exists
  - Hardcoded: `OGV_DS_TEMPLATE` and `OGV_CS_TEMPLATE` with decorative emojis, OGV-specific time options (4PM/9PM ET for DS, 10AM/9PM ET for CS)
  - Print: `[CONFIG] Seeded OGV storm config`

### `seed_ogv_events()`

- **`config.py:1720-1758`**
  - Inserts two default events (Plague Marauder, Zombie Siege) for `OGV_GUILD_ID` if not exists
  - Hardcoded: event names, announcement blurbs with OGV-centric language, timezone (`America/New_York`), times (22:15, 22:45), anchor date (2026-03-30), interval (3 days)
  - Both events get hardcoded channel IDs
  - Print: `[CONFIG] OGV events seeded`

---

## C. OGV-flavoured defaults that ship to all guilds

### `OGV_DEFAULT_THEMES`

- **`config.py:638-646`**
  - List: "Welcome to the Alliance", "Birthday", "Milestone", "War / Performance", "General Celebration", "Contest / Raffle", "Custom"
  - **VERDICT: GENERIC** — These are legitimate alliance themes, not OGV-specific. Can be kept as Last War defaults.

### `OGV_DEFAULT_TONES`

- **`config.py:648-655`**
  - List: "Default (match the theme)", "More casual", "More intense", "Funny", "Serious", "Cinematic / Dramatic"
  - **VERDICT: GENERIC** — Universal tones for writing. Can be kept as defaults.

### `OGV_DEFAULT_PROMPT`

- **`config.py:657-665`**
  - "You are writing a short motivational alliance announcement blurb for a mobile strategy game called Last War..."
  - **VERDICT: GENERIC** — Legitimate train blurb prompt for any Last War alliance. Can be kept as default.

### `OGV_DS_TEMPLATE` and `OGV_CS_TEMPLATE`

- **`config.py:668-694`**
  - `OGV_DS_TEMPLATE`: Uses 🔥 ⚡ emojis, specific phrasing ("Stay coordinated and flexible — let's take this")
  - `OGV_CS_TEMPLATE`: "Hit your zones fast and finish strong"
  - **VERDICT: OGV-FLAVOURED** — The decorative emojis and inspirational language are OGV-specific. Compare to `GENERIC_DS_TEMPLATE` and `GENERIC_CS_TEMPLATE` (lines 696-716) which are plain. Seeding OGV templates means new guilds get OGV's personality, not a neutral baseline.
  NOTES/THOUGHTS: We should make sure that there is a plain, neutral template in here. When we do any kind of migration, the OGV-specific should be saved as if we entered a custom template for these.

### `OGV_SURVEY_QUESTIONS`

- **`config.py:997-1009`**
  - Specific Last War metrics: squad powers, drone level, gorilla level, THP (Total Hero Power), total kills, profession, banner, medical aid
  - **VERDICT: GENERIC (for Last War)** — These are legitimate Last War game mechanics, not OGV invention. Any Last War alliance should track these.

### `OGV_SURVEY_INTRO`

- **`config.py:1011-1015`**
  - "Please fill out this survey each week, if possible, to help us keep track of squad powers, better balance our Desert Storm teams, track alliance growth, and prepare for season events!"
  - **VERDICT: OGV-FLAVOURED** — Plural "us" implies it's pre-written for OGV's coalition. Generic intro should say "your alliance" instead.
  NOTES/THOUGHTS: Let's remove "us" and "our" from this. Then it will read "...to help keep track of squad powers, better balance Desert Storm teams..."

### OGV-specific sheet tab names in `OGV_DEFAULTS` and schema defaults

- "Squad Powers", "Growth Tracking", "Train Schedule", "DS Assignments", "DS-CS Sit-outs", "Survey History", "Season 5 - Off-Season"
- **VERDICT: GENERIC-TO-FLAVOURED MIX**:
  - "Squad Powers", "Growth Tracking", "Train Schedule", "DS Assignments", "DS-CS Sit-outs", "Survey History" → GENERIC Last War sheet naming
  - "Season 5 - Off-Season" → **OGV-SPECIFIC** member roster tab name; indicates OGV's current season/off-season state
  NOTES/THOUGHTS: On migration, make this as if OGV set up their own custom tab name for tracking that portion of the roster.

### Event names in `seed_ogv_events`

- **`config.py:1725, 1740`** — "Plague Marauder (AE)", "Zombie Siege"
- **VERDICT: GENERIC (for Last War)** — These are official in-game event names, not OGV inventions. Every alliance calls them by these names.

### Event announcement blurbs in `seed_ogv_events`

- **`config.py:1728, 1743`**
  - "Plague Marauder (AE) at {time} ({server_time} Server Time). Make sure to have offline participation checked!"
  - "Zombie Siege at {time} ({server_time} Server Time)."
  - **VERDICT: GENERIC** — Standard event announcement templates, universally applicable.

### Train theme hint "Welcome to OGV"

- **`train.py:216`**
  - In `THEME_HINTS` dict: `"welcome": "Welcome to OGV"`
  - **VERDICT: OGV-FLAVOURED** — Hard-coded theme hint. Should be generic "Welcome to [Alliance]" or just "Welcome".
  NOTES/THOUGHTS: Change this to just "Welcome"

---

## D. Conditional behaviour gated on OGV guild ID

### Premium always-premium shortcut

- **`premium.py:35`** — `ALWAYS_PREMIUM_GUILD_IDS: set[int] = {OGV_GUILD_ID}`
  - What it is: OGV is hardcoded to always be premium
  - Used by: `is_premium()` and `get_limit()` functions to grant unlimited features to OGV
  - Removal impact: OGV would need to explicitly be added to `PREMIUM_TEST_GUILD_IDS` or purchased via subscription like every other guild
  NOTES/THOUGHTS: I already pay for hosting, we're not going to set up a subscription for OGV too. Let's find a way to add this either as a row in the db or as a variable that I can input in Railway or something.

### Config fallback in `scheduler.py`

- **`scheduler.py:129-133`** — `next_event_dates()` wrapper
  - Calls `get_config(None)` and assumes it gets OGV config as fallback
  - **VERDICT: Code smell** — `get_config(None)` is not defined; likely intended to retrieve a "global" config but `guild_id` can't be `None` in practice
  - Used by: Public API that callers invoke without guild context
  - Removal impact: This fallback logic breaks if there's no "default" guild; all callers should pass explicit `guild_id`
- **`scheduler.py:160-165`** — Similar in `get_event_datetimes()` wrapper
  - Same issue: calls `get_config(None)` expecting a fallback

### `get_train_config` fallback to OGV defaults

- **`config.py:1639-1670`** — `get_train_config(guild_id)` function
  - If no row exists in `guild_train_config`, returns fallback dict with `OGV_DEFAULT_THEMES`, `OGV_DEFAULT_TONES`, `OGV_DEFAULT_PROMPT`
  - What it is: Fallback defaults used by every guild that hasn't explicitly configured train
  - Removal impact: Every guild falling back gets OGV's defaults; removing these means no defaults at all (empty lists, empty prompt)
  NOTES/THOUGHTS: There should be a set of defaults that we can fall back on. These would be the generic options that are there already and just make them named something like "defaults"

---

## E. Test fixtures that depend on OGV data

### `OGV_GUILD_ID` imported and used in tests

- **`tests/constants.py:12`** — `OGV_GUILD_ID = 1266229297723605052`
- **`tests/conftest.py:22`** — Imports `OGV_GUILD_ID` from constants
- **Multiple test files** — Import and use `OGV_GUILD_ID` in test methods

### Test cases explicitly testing OGV premium status

- **`tests/unit/test_premium.py:72-73`** — `test_ogv_is_always_premium()` — asserts OGV is premium
- **`tests/unit/test_premium.py:266-270`** — Asserts OGV premium limits
- **`tests/integration/test_premium_e2e.py:120-140`** — Multiple tests (`test_ogv_unlimited_events`, `test_ogv_train_templates_capped_at_ten`, `test_ogv_storm_templates_capped_at_ten`)
- **`tests/integration/test_setup_flows.py:526-531`** — `test_events_unlimited_for_premium_ogv()`
- **`tests/unit/test_member_roster.py:286-295`** — Tests OGV roster behavior with premium status

### Test cases using OGV in mock setups

- **`tests/unit/test_dm.py:60-157`** — Multiple test methods pass `OGV_GUILD_ID` to `send_dm_to_id()` and `mention_or_name()`
- **`tests/integration/test_premium_e2e.py:182-204`** — Sets up roster config for OGV guild
- **`tests/unit/test_member_roster.py:269-295`** — Uses `OGV_GUILD_ID` to test premium roster
- **`tests/integration/test_survey_hub_flows.py:113-114`** — Uses `OGV_GUILD_ID` for followup interaction

### Test survey and multi-template tests

- **`tests/unit/test_survey.py:16-37`** — Multiple tests validate `OGV_SURVEY_QUESTIONS` (checking required keys, validating structure)
- **`tests/unit/test_survey.py:69-93`** — Tests use `OGV_SURVEY_QUESTIONS` directly
- **`tests/unit/test_multi_templates.py:306-309`** — Uses `OGV_GUILD_ID` to test survey save/load

### Impact when removing OGV seeding

- All tests that specifically assert OGV's always-premium status will fail or need to be updated to use `PREMIUM_TEST_GUILD_IDS`
- Tests that iterate over `OGV_SURVEY_QUESTIONS` will need to be replaced with synthetic survey data
- Any test using `OGV_GUILD_ID` will need to either use `TEST_GUILD_ID` or create a synthetic fixture

---

## F. Comments / docstrings / docs that reference OGV

### Code comments

- **`bot.py:148`** — `# Initialise the config database and seed OGV defaults`
- **`config.py:9`** — `The OGV guild config is seeded automatically as the default.`
- **`config.py:21`** — `# ── OGV default values (seeded on first run)`
- **`config.py:123`** — Docstring: `Create the guild_configs table if it doesn't exist and seed OGV defaults`
- **`config.py:208, 227, 257, 278, 325`** — Comment `# Seed OGV [feature] config` before each seed function call
- **`config.py:490-491`** — Comments explaining OGV `spreadsheet_id` treatment
- **`config.py:506`** — `# Seed OGV events after table is ready`
- **`config.py:1574-1575`** — Docstring: `Seed OGV's train config if not already present`
- **`config.py:1640`** — Docstring: `Return the train config for a guild, falling back to OGV defaults`
- **`config.py:1720-1721`** — Docstring: `Seed OGV's two default events if they don't already exist`
- **`premium.py:34`** — `OGV is the bot owner's home alliance — they get full access`
- **`premium.py:11`** — `` `ALWAYS_PREMIUM_GUILD_IDS` (hardcoded — includes OGV) ``
- **`scheduler.py:15`** — `On approval → posts to Announcements with @OGV tag, stamps leadership channel`
- **`scheduler.py:128, 158`** — Docstring: `Public wrapper using OGV defaults`
- **`storm_log.py:67`** — Comment: `back to the legacy tab_sitouts shared tab so existing OGV data`
- **`storm_log.py:385`** — Comment: `Replaces the OGV-specific col-E hardcode`
- **`storm_log.py:1146`** — Comment: `fall back to the legacy DS/CS column shape so OGV's pre-rework data`
- **`survey.py:4-5`** — Docstring: `A persistent button in the survey channel lets any OGV member submit...`

### Documentation files

- **`docs/PREMIUM_SETUP.md:51-52`** — "with the exception of OGV, which is hardcoded as always-premium for testing"
- **`CHANGELOG.md:84`** — Reference to OGV during private testing phase

---

## G. Environment / config / secrets

- No `.env.example` or `.env` files found in the repo
- No hardcoded `.env` examples with OGV secrets
- OGV's `spreadsheet_id` is expected in `SPREADSHEET_ID` env var and persisted to DB on first run (`config.py:497-504`)
- **`bot.py`** — No OGV-specific constants; seeding happens in `init_db()` called on startup

---

## Migration considerations

### 1. Database state management

- **Current model:** OGV config is auto-seeded on every `init_db()` startup (if not already in DB). All other guilds are created empty and filled via `/setup`.
- **Post-removal model:** OGV must go through `/setup` like every other guild. However, OGV's current running instance has a populated `guild_configs` row with all channel/role IDs. If we delete the seed code, that row persists in the database — OGV doesn't lose config.
- **Decision required:** Do you want to:
  - **(A)** Keep OGV's current DB row as-is and only remove the seed logic (so OGV keeps working, but new guilds get no defaults)?
  - **(B)** Delete OGV's row from the DB and have OGV run through `/setup` to rebuild it (cleanest, but requires manual intervention)?
  - **(C)** Provide a one-time migration script that converts OGV's seeded row to one marked "manually configured" (no special treatment)?
  NOTES/THOUGHTS: This has been seeded several times. Can we check that all the data has for OGV has been seeded into the proper places? If everything is already in there now, we can remove the seeding functions entirely and rely on what is in the DB/Sheet.

### 2. Premium status transition

- **Current model:** OGV is in `ALWAYS_PREMIUM_GUILD_IDS`, bypassing all billing checks.
- **Post-removal model:** OGV must either:
  - Be added to `PREMIUM_TEST_GUILD_IDS` env var (requires bot restart or config reload)
  - Purchase a real subscription via `/upgrade`
  - Be manually added to the premium tiers table in the database
- **Decision required:** Which path for OGV's premium status after removal?
NOTES/THOUGHTS: Answered above, but basically they need to always be premium so if we can create a variable on Railway or give them a flag in the db that would show this and get it out of here, we should do that. I believe we look for the SKU, so how do we do that? Is it in the db? If so, we can perma-add the SKU attached to OGV.

### 3. Fallback defaults cleanup

- **Current model:** Any guild without explicit config falls back to `OGV_DEFAULT_*` values (themes, tones, prompt, survey intro).
- **Post-removal model:** Remove all `OGV_DEFAULT_*` fallbacks. This affects:
  - Train: guilds without config get empty themes/tones/prompt instead of OGV's
  - Survey: guilds without config get empty questions instead of `OGV_SURVEY_QUESTIONS`
  - Scheduler: `get_config(None)` calls will break (need explicit `guild_id`)
- **Decision required:**
  - Keep generic Last War defaults (e.g., "Plague Marauder", "Zombie Siege" event names, universal themes)?
  - Or go fully generic with minimal defaults (empty lists, prompt: "")?
NOTES/THOUGHTS: We should use LW defaults. We may want to make a config file or somewhere that we define what the defaults are for these things and have that be a list that can be referred to by the app for any default terminology.

### 4. Test suite migration

- **Affected tests:**
  - OGV premium assertions (7+ tests) → Replace with `PREMIUM_TEST_GUILD_IDS` or create synthetic premium guild
  - OGV survey questions tests → Replace with synthetic survey data or use `TEST_GUILD_ID` with custom fixtures
  - `OGV_GUILD_ID` in dm/roster/multi-template tests → Use `TEST_GUILD_ID` or create per-test synthetic guild
- **Effort:** Medium. Mostly search-and-replace `OGV_GUILD_ID` with `TEST_GUILD_ID`, update assertions for premium status to use env var mocking.

### 5. Comment cleanup (low priority)

- Remove or generalize ~20 comments/docstrings mentioning OGV throughout the codebase
- Examples: `Seed OGV train config` → `Seed train config`, `OGV defaults` → `default themes/tones`

### 6. Theme hint string

- **`train.py:216`** — `"welcome": "Welcome to OGV"` should become `"welcome": "Welcome"` or `"welcome": "Welcome to the Alliance"`

### 7. Scheduler edge case

- **`scheduler.py:129-133, 160-165`** — `get_config(None)` calls are code smell. All callers should pass explicit `guild_id`. May need to audit all call sites and fix them.

---

## Key decisions for next session

1. **OGV's existing DB config**: preserve as-is (option A), wipe and re-`/setup` (option B), or migrate via script (option C)?
2. **OGV's premium status post-strip**: env var, real subscription, or manual DB row?
3. **Defaults policy**: keep generic Last War defaults, or strip everything to empty?
4. **Test fixtures**: search-and-replace `OGV_GUILD_ID` → `TEST_GUILD_ID`, or build new synthetic fixtures from scratch?
NOTES/THOUGHTS: 1-3 were answered inline earlier in the document. For 4, do whatever would be best on this.

---

*Inventory generated during pre-launch survey. No code changes have been made
based on this document. Update / strike-through entries as the migration is
executed.*
