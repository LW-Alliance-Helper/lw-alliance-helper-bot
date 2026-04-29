# Changelog

All notable changes to **LW Alliance Helper** are recorded here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
