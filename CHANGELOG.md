# Changelog

All notable changes to **LW Alliance Helper** are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed — OGV strip (internal refactor, no user-facing behavior change for OGV)

OGV (the bot owner's home alliance) was originally hardcoded into the bot
as the canonical "test alliance" — its config was auto-seeded into the
SQLite database on every startup, its templates and survey questions
shipped as the default values for every new guild, and it was permanently
flagged as Premium via a hardcoded `{OGV_GUILD_ID}` set in `premium.py`.
Pre-launch this was fine; post-launch it meant every alliance that
installed the bot got OGV-flavoured defaults, and one alliance had a
subtly different code path from everyone else.

This release removes all of that. OGV now goes through the same `/setup`
flow as every other alliance and is treated identically by the bot. The
migration was executed in seven steps:

1. **`defaults.py`** — extracted framework-level default values
   (themes, tones, prompt template, survey question set, neutral storm
   templates) out of `config.py` into a dedicated module. The default
   survey intro was rewritten to drop "us" / "our" so it reads
   neutrally for any alliance.
2. **Seeding code stripped** — deleted the six `_seed_ogv_*` functions,
   `seed_ogv_events()`, the OGV `INSERT` block in `init_db()`, and the
   `SPREADSHEET_ID` env-var-into-OGV-row logic. Schema defaults for the
   role-name columns went from `'OGV'` / `'OGV Leadership'` to generic
   `'Member'` / `'Leadership'` placeholders. OGV's existing rows in the
   production DB were preserved intact.
3. **Premium gate moved to env var** — replaced `ALWAYS_PREMIUM_GUILD_IDS
   = {OGV_GUILD_ID}` with a `PREMIUM_BYPASS_GUILD_IDS` env-var-driven
   set. OGV's permanent Premium status now comes from the env var on
   the host (Railway), not from hardcoded code.
4. **`/events` rewritten to use per-guild events** — fixed a long-
   standing bug where `/events` silently fell back to OGV's hardcoded
   event times for any non-OGV guild because the legacy
   `next_event_dates()` / `get_event_datetimes()` wrappers used
   `get_config(None)` which always returned None. The command now reads
   from `guild_events` and supports guilds with multiple cycle types.
5. **Test fixtures decoupled** — replaced every `OGV_GUILD_ID` reference
   in the test suite with a synthetic `PREMIUM_TEST_GUILD_ID` constant
   in `tests/constants.py`. The `OGV_GUILD_ID` constant in `config.py`
   was deleted entirely.
6. **Comment + docs cleanup** — generalised ~10 stale narrative
   references in code comments and docstrings; updated
   `docs/PREMIUM_SETUP.md` to document the new env var.
7. **Final verification** — full test suite (318 tests) passes;
   production smoke test confirmed clean startup, no `[CONFIG] Seeded
   OGV …` log lines, and OGV-specific commands continue to work
   identically through the env-var-driven Premium grant.

The one-shot migration scripts (`scripts/verify_ogv_data.py`,
`scripts/fix_ogv_data.py`) used during the strip have also been removed
from the repo — git history preserves them as a template if a similar
migration is ever needed.

After this work, OGV's real guild ID appears only in:
- The `PREMIUM_BYPASS_GUILD_IDS` env var on the host (production config)
- `docs/OGV_STRIP_INVENTORY.md` (historical record of the migration)

### Fixed

- **`/canyonstorm` rendering for OGV** — the CS mail template stored in
  OGV's row used the old `{subs_list}` placeholder name. The codebase
  had migrated to `{subs}` long ago, so OGV's CS mails were rendering
  with literal `{subs_list}` text. One-shot data fix replaced the
  placeholder.
- **Train reminders for OGV** — `reminder_channel_id` was 0, so the
  daily train reminder was effectively going nowhere. Set to OGV's
  leadership channel.
- **Event channel defaults for OGV** — `guild_configs.event_draft_channel_id`
  and `event_announce_channel_id` were 0 because the columns were added
  via `ALTER TABLE` after OGV's row was inserted. Set to match the
  per-event values in `guild_events`.
- **`/events` for non-OGV guilds** — see step 4 above. Previously
  silently broken for any alliance that wasn't OGV; now works
  correctly across the board.
- **Duplicate startup logs** — both the growth snapshot task and the
  stats publisher had redundant startup-time invocations alongside their
  scheduled `tasks.loop`. Removed the duplicates; each now runs exactly
  once on boot via the loop's `.start()` immediate-fire behavior.

## [1.0.0] — 2026-04-28

Initial public release.

### Added

- **Event announcements** — schedule recurring in-game events (Plague Marauder,
  Zombie Siege, custom events). Drafts are posted to leadership for approval at
  a configurable lead time before each event, and the final announcement is
  posted automatically once approved. A 5-minute pre-event warning fires
  automatically. Managed via `/events` and `/events_log`.
- **Train schedule** — `/train` to view and edit the daily alliance train
  assignment, with inline buttons for Add, Update, Generate Prompt, and Clear.
  When a blurb template and ChatGPT integration are configured, the bot
  generates a personalised prompt for the day's train recipient.
- **Birthday tracking** — read birthday data from the configured Google Sheet,
  optionally announce birthdays in Discord, and (with `/train_addbirthdays`)
  pre-populate the train schedule on a member's birthday.
- **Desert Storm / Canyon Storm**
  - `/desertstorm_draft` and `/canyonstorm_draft` — four-step mail drafting
    flow (Pick Team → Pick Time → Use template / Edit → Preview), ending
    with a **Post & Copy** action that publishes the announcement and
    saves the latest text as the next-time template.
  - `/desertstorm_participation` and `/canyonstorm_participation` —
    configurable participation logging. Each alliance defines up to 3 custom
    questions (free text, numeric, multi-select roster pick, etc.) and the
    log is written to the configured Google Sheet tab.
  - `/desertstorm_log` and `/canyonstorm_log` — look up a past log by date.
  - `/desertstorm` and `/canyonstorm` — overview embeds showing the current
    configuration.
- **Squad-power survey** — `/survey_post` posts a button members can click to
  open a private thread and submit their stats. Responses are appended to the
  configured Google Sheet, and leadership receives a summary in the
  notification channel for each submission.
- **Survey reminders** — `/survey_remind` to send a one-off reminder
  immediately or schedule a recurring reminder (frequency, day-of-week, time).
  Free tier delivers as a channel post; Premium can deliver via DM to roster
  members who haven't yet submitted.
- **Growth tracking** — `/growth` to take a manual snapshot or schedule
  recurring snapshots (monthly or every-N-days). Captures the metrics defined
  in the alliance's growth config (squad powers, THP, kills, etc.) into a
  history sheet for trend analysis.
- **Setup wizard** — `/setup` walks new servers through core configuration
  (member role, leadership role, leadership channel, timezone, Google Sheet
  share). Per-feature `/setup_*` commands cover events, train, birthdays,
  storms, survey, growth, and member roster sync.
- **Configuration view** — `/view_configuration` displays everything the bot
  has been configured with so leadership can audit current settings.
- **Help command** — `/help` lists every available command, grouped by
  feature, with one-line descriptions.

### Premium

The following features require an active **LW Alliance Helper Premium**
subscription via Discord App Subscriptions ($4.99 USD / month — Discord does
not currently support annual billing):

- **Member Roster Sync** — `/sync_members` keeps a roster of in-game member
  names mapped to Discord users. Required for DM-based features.
- **Multi-survey** — define and manage multiple named surveys via the
  `/survey` manage view (Add / Edit / Remove). Each survey has its own
  question set, sheet tab, and answer button.
- **DM-based reminders** — `/survey_remind`, `/desertstorm_remind`, and
  `/canyonstorm_remind` can DM individual roster members instead of posting a
  channel reminder.
- **Scheduled survey reminders** — recurring reminders fire automatically on
  the configured frequency (weekly / fortnightly / monthly), day, and time,
  delivered via DM to members who haven't yet responded.
- **Unlimited templates** — the free tier caps mail templates and event
  templates at sensible defaults; Premium removes those caps.

### Notes

- This is the first publicly released version. Earlier internal builds powered
  the **OGV** alliance during private testing. The version constant is
  `__version__ = "1.0.0"` in `bot.py`.
- Bug reports and feature requests can be filed at
  <https://github.com/LW-Alliance-Helper/lw-alliance-helper.github.io/issues>.

[Unreleased]: https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/releases/tag/v1.0.0
