# LW Alliance Helper

A Discord bot built for **Last War alliance leadership** — event announcements, train schedules, birthday tracking, Desert and Canyon Storm mail drafts, surveys, and growth snapshots, all wired to a Google Sheet your alliance owns.

📖 [Website & setup guide](https://lw-alliance-helper.github.io) · 🗺️ [Public roadmap](https://github.com/orgs/LW-Alliance-Helper/projects/2) · 💬 [Report an issue](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/new/choose) · 📜 [Changelog](CHANGELOG.md)

---

## What it does

- **📣 Event announcements** — Schedule Plague Marauder, Zombie Siege, and any other recurring events. The bot drafts to leadership for review, posts the final announcement to your public channel after approval, and fires a 5-minute warning before kick-off.
- **🚂 Train schedule** — Track who's driving the train each day. Optional ChatGPT prompt generation for the daily blurb, with birthdays auto-populated in advance.
- **🎂 Birthdays** — Read birthday data from your sheet and optionally auto-add to the train schedule, announce in-channel, or both.
- **⚔️ Desert Storm / 🏜️ Canyon Storm** — Generate ready-to-copy team mail drafts per team. Configurable participation tracking with custom questions; sit-outs and no-shows logged to your sheet.
- **📋 Surveys** — Members submit stats through a private Discord thread; responses save directly to your sheet, with a leadership notification per submission.
- **📈 Growth tracking** — Snapshot any metric in your sheet (squad powers, THP, kills, anything) on a configurable schedule. You define the metrics, source, and cadence.
- **💎 Premium add-ons** — DM-based reminders, multi-survey, member roster sync, customizable DM bodies, unlimited templates and survey questions, threads as channel destinations.

→ Full command reference: [commands page](https://lw-alliance-helper.github.io/commands.html)

---

## Your data stays with you

Alliance Helper is built around a simple principle: **your alliance's data lives in your own Google Sheet**, on the Google account *you* control. The bot reads and writes; it doesn't keep its own copy. Power scores, growth snapshots, train history, participation logs, member rosters — all of it is in *your* sheet.

- **You own the data.** Switch tools, share with another alliance, or export — the bot doesn't care.
- **Switch leadership without losing anything.** Hand the sheet off when leadership changes; every record from every storm, train, and survey comes with it.
- **Premium adds features, not data captivity.** Subscribing unlocks DMs, scheduled reminders, and roster sync — nothing about where your data lives changes.

The bot's own SQLite database stores only what it needs to do its job — wizard answers, channel/role IDs, schedule state, premium status. Alliance data itself stays in your sheet.

Alongside the config, the bot keeps a small **install-metadata record** for each server it's in: guild ID, guild name, the owner's Discord ID, the Discord ID of the user who invited the bot (when readable from the audit log), and the timestamps for first install and most recent reconnect. This exists so that when an error appears in the logs against a particular guild ID, leadership can be contacted to fix it. The record is **deleted automatically** when the bot is removed from a server, and can be deleted on request at any time — open a [Data removal request](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/new/choose) with your guild ID.

---

## Free vs Premium

Every feature works on the **free tier** with sensible caps for a typical alliance. **Premium** ($4.99/month, billed via Discord App Subscriptions) lifts the limits and unlocks features built around member identity (DMs, mentions, roster sync).

| | Free | Premium |
|---|---|---|
| Configured events | 5 | Unlimited |
| Train prompt templates | 1 | 10 named |
| Storm mail templates per team | 1 | 10 named |
| Surveys per server | 1 | Multiple named |
| Survey questions per survey | 5 | Unlimited |
| Tracked growth metrics | 5 | Unlimited |
| Channel destinations | Text channels | Text channels and threads |
| Storm participation lookback | 4 entries | Unlimited |
| Premium-only features | — | Member Roster Sync · birthday DMs · train assignment DMs · DM-based storm reminders · DM-based survey reminders · auto-mentions · customisable DM bodies |

Full comparison + complete premium-only feature list: [pricing page](https://lw-alliance-helper.github.io/pricing.html).

A subscription is **per-user** and applies to **one server at a time** — use `/premium assign` to switch which alliance it's active in, or `/premium overview` to see where it's currently active. Cancel anytime through Discord; saved data and assignment are preserved so you can resume later.

---

## Quick start

1. **[Invite the bot to your server](https://discord.com/oauth2/authorize?client_id=1488378654709780510&permissions=397284699328&integration_type=0&scope=applications.commands+bot)**
2. **Create a Google Sheet** and share it with the bot's service account (the email address is shown in `/setup`)
3. **Run `/setup`** in your leadership channel — walks through member role, leadership role, leadership channel, timezone, and sheet ID
4. **Configure the features you want** — re-run `/setup` to open the hub and click whichever feature buttons you'd like to enable (Events, Train, Birthdays, Desert Storm, Canyon Storm, Survey, Growth, Shiny Tasks, plus Premium-gated Members + Survey extras + Growth Breakdown). Each is independent; skip any you don't need.

→ Full step-by-step setup, including the sheet structure each feature expects: [setup page](https://lw-alliance-helper.github.io/setup.html)

---

## Tech stack

The codebase is public for transparency — this is the same code that runs in production.

- **Python 3.11+** — hosted on [Railway](https://railway.com)
- **[discord.py](https://github.com/Rapptz/discord.py) 2.4** — Discord interactions
- **SQLite** — per-guild bot configuration (channel/role IDs, wizard answers, schedule state)
- **[gspread](https://github.com/burnash/gspread)** — Google Sheets read/write for alliance data
- **Sentry** — error tracking
- **Discord App Subscriptions** — Premium billing

---

## Roadmap & support

- 🗺️ **What's planned**: [Public roadmap](https://github.com/orgs/LW-Alliance-Helper/projects/2)
- 🐛 **Found a bug or have an idea**: [Open an issue](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/new/choose) — bug reports and feature requests both auto-route into the backlog
- 📜 **What's shipped**: [CHANGELOG](CHANGELOG.md)
- 📖 **How-to docs**: [lw-alliance-helper.github.io](https://lw-alliance-helper.github.io)

---

LW Alliance Helper is an independent tool for Last War players and is not affiliated with Last War or its developers.
