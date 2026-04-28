# Alliance Helper — Full UX Walkthrough

This document renders every command, wizard, button, and embed the
bot produces, in the order a user would encounter them. Use it to
review the UX without running flows on Discord.

## Conventions

- 🤖 **Bot output** — quoted blocks for embeds (with title and color)
- 👤 *User input* — italics for the action the user takes
- 🔘 `[Button]` — Discord button labels
- 📋 **Modal** — text-input form dialogs
- 💎 **Premium-only** — feature gated behind a Premium subscription
- ⚠️ Cap or limit note for the free tier
- ⏰ Timeout / cancellation message
- ✅ Success message
- 🔒 Premium-locked message

All slash commands except `/setup`, `/setup_*`, `/donate`, `/upgrade`, and
the public `📋 Answer` survey button are gated by three guards in this order:

1. **Bot configured** — server has run `/setup` (`setup_complete = 1`)
2. **Channel guard** — invoked from a channel/thread within the leadership category
3. **Role guard** — caller has the configured leadership role

If any of these fails, the command responds ephemerally with a respective
guard message (e.g. `⛔ This command can only be used in the leadership channel.`).

`/setup`, `/setup_*`, `/setup_reset`, `/sync_members`, `/setup_members`, and
`/view_configuration` instead require the **server administrator** permission
(not the leadership role).

---

## Table of Contents

- [1. First-time Setup](#1-first-time-setup)
  - [`/setup`](#setup)
  - [`/setup_reset`](#setup_reset)
  - [`/view_configuration`](#view_configuration)
  - [`/help`](#help)
- [2. Event Announcements](#2-event-announcements)
  - [`/setup_events`](#setup_events)
  - [`/events [date]`](#events)
  - [`/events_log`](#events_log)
  - [Automatic event flow (scheduler)](#automatic-event-flow-scheduler)
- [3. Train Schedule](#3-train-schedule)
  - [`/setup_train`](#setup_train)
  - [`/train`](#train)
  - [Blurb wizard](#blurb-wizard)
  - [`/train_log [date]`](#train_log)
  - [`/train_addbirthdays`](#train_addbirthdays)
  - [Daily train reminder loop](#daily-train-reminder-loop)
- [4. Birthdays](#4-birthdays)
  - [`/setup_birthdays`](#setup_birthdays)
  - [`/birthdays`](#birthdays)
  - [Daily birthday announcement loop](#daily-birthday-announcement-loop)
- [5. Desert Storm & Canyon Storm](#5-desert-storm--canyon-storm)
  - [`/setup_desertstorm` & `/setup_canyonstorm`](#setup_desertstorm--setup_canyonstorm)
  - [`/desertstorm` & `/canyonstorm`](#desertstorm--canyonstorm)
  - [`/desertstorm_draft` & `/canyonstorm_draft`](#desertstorm_draft--canyonstorm_draft)
  - [`/desertstorm_participation` & `/canyonstorm_participation`](#desertstorm_participation--canyonstorm_participation)
  - [`/desertstorm_log [date]` & `/canyonstorm_log [date]`](#desertstorm_log--canyonstorm_log)
  - [`/desertstorm_remind` & `/canyonstorm_remind` 💎](#desertstorm_remind--canyonstorm_remind)
- [6. Survey](#6-survey)
  - [`/setup_survey`](#setup_survey)
  - [`/survey`](#survey)
  - [`/survey_post`](#survey_post)
  - [Survey thread flow (member-facing)](#survey-thread-flow-member-facing)
  - [`/survey_remind` 💎](#survey_remind)
- [7. Growth Tracking](#7-growth-tracking)
  - [`/setup_growth`](#setup_growth)
  - [`/growth`](#growth)
- [8. Member Roster Sync 💎](#8-member-roster-sync-)
  - [`/setup_members`](#setup_members)
  - [`/sync_members`](#sync_members)
- [9. Utilities](#9-utilities)
  - [`/cancel`](#cancel)
  - [`/donate`](#donate)
  - [`/upgrade`](#upgrade)
- [10. Cross-cutting UX patterns](#10-cross-cutting-ux-patterns)

---

## 1. First-time Setup

### `/setup`

Configure roles, leadership channel, timezone and Google Sheet. **Tier:** Both.
**Permissions:** Server administrator only. Run from any channel — wizard prompts
appear in the channel where the command was invoked.

If `setup_complete = 1` already, the bot first shows a summary and an
**Edit / No changes needed** chooser:

> 🤖 **Embed: ⚙️ Current Core Setup** (color: blurple)
> *Your server is already configured. Would you like to edit these settings?*
>
> - **Member Role:** OGV
> - **Leadership Role:** OGV Leadership
> - **Leadership Channel:** `<#…>`
> - **Timezone:** (UTC-5) Eastern (New York, Toronto, Miami)
> - **Sheet ID:** `1AbCdEfGhIjKlMnOpQrS...`
>
> 🔘 `[✏️ Edit settings]` (primary)  🔘 `[✅ No changes needed]` (secondary)

If **No changes needed** → `✅ No changes made. Your existing setup is still active.`

If **Edit settings** (or first-run), the wizard kicks off with:

> 🤖 ⚙️ **Alliance Helper Setup**
> I'll walk you through the core configuration for your server. This covers your
> roles, leadership channel, timezone and Google Sheet.
>
> *You can run `/setup` again at any time to update these settings.*

#### Step 1 of 6 — Member Role

> 🤖 **Step 1 of 6 — Member Role**
> Select the role that all alliance members have:
>
> 🔘 *Discord Role select dropdown* — `Select member role...`
> 🔘 `[➕ Create a new role]` (secondary) — opens a 📋 modal with one field
> "Role name" (placeholder: `e.g. Member, Alliance Member, Leadership`, max 100 chars)

On select: `✅ Selected: **OGV**` (replaces prompt).
On create: `✅ Created and selected new role: **Member**`.
If the bot lacks `Manage Roles`, it falls back to:
`⚠️ I don't have permission to create roles. Please create the role manually first, then run /setup again.`

⏰ Timeout (2 min): `⏰ Setup timed out. Run /setup to start again.`

#### Step 2 of 6 — Leadership Role

Identical to Step 1 with the prompt:

> 🤖 **Step 2 of 6 — Leadership Role**
> Select the elevated role for alliance leadership:

#### Step 3 of 6 — Leadership Channel

> 🤖 **Step 3 of 6 — Leadership Channel**
> Select the private channel where leadership commands will be used:
>
> 🔘 *Discord Channel select* — `Select leadership channel...`
> 💎 On premium guilds, threads are also pickable (the dropdown filters include
> public/private/news threads).
> 🔘 `[➕ Create a new channel]` (secondary, hidden if any thread types are in
> the picker) — opens 📋 modal with field "Channel name" (default `leadership`,
> max 100 chars; spaces auto-converted to dashes).

On select: `✅ Selected: **leadership**`. On create:
`✅ Created and selected: **#leadership**`.

#### Step 4 of 6 — Timezone

> 🤖 **Step 4 of 6 — Timezone**
> Select your alliance's timezone. This is used for displaying event times,
> Desert Storm/Canyon Storm times, and train reminders throughout the bot:
>
> 🔘 *Single select* — `Select your timezone...`

The dropdown lists 22 zones, ordered by UTC offset, e.g.:
- (UTC-10) Hawaii (Honolulu)
- (UTC-8) Pacific (Los Angeles, Seattle, Vancouver)
- (UTC-5) Eastern (New York, Toronto, Miami)
- (UTC+0) GMT/BST (London, Dublin, Lisbon)
- (UTC+1) Central European (Paris, Berlin, Rome)
- (UTC+5:30) India (Mumbai, Delhi, Bangalore)
- (UTC+8) China/Singapore (Shanghai, Beijing, Singapore)
- (UTC+12) New Zealand (Auckland, Wellington)

On select: `✅ Timezone: **(UTC-5) Eastern (New York, Toronto, Miami)**`.

#### Step 5 of 6 — Google Sheet ID

> 🤖 **Step 5 of 6 — Google Sheet ID**
> Enter your Google Sheet ID — the long string from your sheet's URL:
> `https://docs.google.com/spreadsheets/d/`**`YOUR_SHEET_ID`**`/edit`
>
> 🔘 `[✏️ Enter Value]` (primary) — opens 📋 modal "Google Sheet ID" with field
> "Sheet ID" (placeholder: `Paste your Sheet ID here...`, max 200 chars)

On submit, message updates to: `✅ Entered: **<sheet-id>**`.

#### Step 6 of 6 — Share Sheet

> 🤖 **Embed: Step 6 of 6 — Share Your Google Sheet** (color: yellow)
> Before finishing, you need to give the bot access to your sheet.
>
> **Follow these steps:**
> 1️⃣ Click the link below to open your sheet's sharing settings
> 2️⃣ Click **Share** in the top right corner
> 3️⃣ Paste the email address below into the share field
> 4️⃣ Set permission to **Editor**
> 5️⃣ Click **Send** — then come back here and confirm
>
> **📋 Service Account Email (click to copy)**
> `sheet-connector@lw-alliance-helper.iam.gserviceaccount.com`
>
> **🔗 Open Your Sheet**
> [Click here to open sharing settings](https://docs.google.com/spreadsheets/d/<id>/edit#sharing)
>
> 🔘 `[✅ I've shared the sheet]` (success)  🔘 `[❌ Cancel setup]` (danger)

If cancelled: `❌ Setup cancelled. Run /setup to start again.`

#### Confirmation summary

> 🤖 **Embed: ⚙️ Setup Summary** (blurple)
> *Please confirm these settings before saving:*
>
> - **Member Role:** OGV
> - **Leadership Role:** OGV Leadership
> - **Leadership Channel:** <#…>
> - **Timezone:** (UTC-5) Eastern …
> - **Sheet ID:** `1AbCdEfGhIjKlMnOpQ...`
>
> 🔘 `[✅ Confirm]` (success)  🔘 `[❌ Cancel]` (danger)

On confirm:

> 🤖 ✅ **Core setup complete!**
>
> Now configure the features you want to use. Run each of the commands below
> for any feature you'd like to enable:
>
> - 📣 `/setup_events` — Event announcements (Plague Marauder, Zombie Siege, etc.)
> - 🚂 `/setup_train` — Train schedule, blurb generation, and reminders
> - 🎂 `/setup_birthdays` — Birthday tracking and announcements
> - ⚔️ `/setup_desertstorm` — Desert Storm mail drafts and participation logs
> - 🏜️ `/setup_canyonstorm` — Canyon Storm mail drafts and participation logs
> - 📋 `/setup_survey` — Squad powers survey
> - 📈 `/setup_growth` — Growth tracking (snapshot your members' stats over time)
>
> You can set up as many or as few of these as you need. Use `/help` at any time to see all available commands.

---

### `/setup_reset`

Wipe the configuration row and start over. **Tier:** Both. **Permissions:** Admin only.

Initial ephemeral prompt:
> 🤖 ⚠️ Are you sure you want to reset the bot configuration for this server?
> This cannot be undone.
>
> 🔘 `[Yes, reset everything]` (danger)  🔘 `[Cancel]` (secondary)

On confirm: `✅ Configuration reset. Run /setup to configure the bot again.`
On cancel or 60-s timeout: nothing happens (the buttons silently expire).

---

### `/view_configuration`

Render every configured setting across every wizard. **Tier:** Both. **Permissions:** Admin only.

If the bot is not yet set up:
`⚙️ This server hasn't been set up yet. Run /setup to get started.`

Otherwise an ephemeral embed is sent:

> 🤖 **Embed: ⚙️ Current Configuration · Free tier** (blurple) **or
> · 💎 Premium** (gold)
> *All configured settings across the bot's setup wizards.*
>
> **🛠️ Core**
> - Tier: Free tier (or 💎 Premium)
> - Member Role: OGV
> - Leadership Role: OGV Leadership
> - Leadership Channel: <#…>
> - Announcement Channel: <#…>
> - Timezone: (UTC-5) Eastern …
> - Spreadsheet ID: `1AbC…`
> - Member Tab: Season 5 - Off-Season
>
> **📣 Events**
> - Draft Channel / Announcement Channel / Draft Time / 5-Min Warning
> - Events (N): each as `• Plague Marauder (plague_marauder) — 22:15 America/New_York · blurb ✅ Configured`
>
> **🚂 Train**
> - Schedule Tab, Blurbs ✅/❌, Themes (count), Tones (count), Default Tone, Prompt Template ✅/❌
> - Reminders ✅/❌, Reminder Channel, Reminder Time
>
> **🎂 Birthdays**
> - Enabled, Source Tab, Name Column (letter), Birthday Column (letter), Discord ID Column,
>   Data Start Row, Lookahead Days, Train Integration, Reminders + channel + time
>
> **⚔️ Desert Storm** — Sheet Tab, Log Channel, Time Option 1/2, Mail Template ✅/❌
> **🏜️ Canyon Storm** — same structure
>
> **📋 Survey** — Survey Channel, Notify Channel, Stats Tab, History Tab, Questions count, Intro Message ✅/❌
>
> **📈 Growth** — Enabled, Source Tab, Name Column, Data Start Row, Growth Tab, Snapshot Schedule, Metrics
>
> Footer (free): *Run /upgrade for Premium • /help for all commands • /setup_* to update a section*
> Footer (premium): *💎 Premium is active. Run any /setup_* command to update a section.*

---

### `/help`

Show all available bot commands grouped by feature area. **Tier:** Both.
**Permissions:** None — works in any channel.

> 🤖 **Embed: 🤖 Alliance Helper — Commands · Free tier** (blurple) **or
> · 💎 Premium** (gold)
> *All commands require the configured leadership role and must be used in the
> leadership channel. Run /setup first if you haven't configured the bot yet.*
>
> **⚙️ Core Setup**
> Configure the bot for your server. Start here before using any other features.
> - `/setup` — Configure roles, leadership channel, timezone, and Google Sheet
> - `/view_configuration` — View all configured settings across every wizard
> - `/setup_reset` — Clear server configuration and start over
>
> **📣 Event Announcements**
> Automate event scheduling for Plague Marauder, Zombie Siege, and any other
> recurring events. Drafts are posted to leadership for review before going public.
> - `/setup_events` — Configure events, announcement channels, draft time, and 5-min warning
> - `/events [date]` — Open the event editor for today or a specific date
> - `/events_log` — Show approved event posts (7d free / 30d premium)
>
> **🚂 Train Schedule**
> - `/setup_train` — Configure the train tab, blurb generation, and reminders
> - `/train` — View the schedule with Add / Update / Generate Prompt / Clear buttons
> - `/train_log [date]` — Show recent prompt log entries (7d free / 30d premium)
> - `/train_addbirthdays` — Manually run the birthday check now
>
> **🎂 Birthdays**
> - `/setup_birthdays` — Configure birthday tracking, train integration, and announcements
> - `/birthdays` — Show upcoming birthdays in the next 14 days
>
> **⚔️ Desert Storm**
> - `/setup_desertstorm`, `/desertstorm`, `/desertstorm_draft`,
>   `/desertstorm_participation`, `/desertstorm_log [date]`
>
> **🏜️ Canyon Storm**
> - `/setup_canyonstorm`, `/canyonstorm`, `/canyonstorm_draft`,
>   `/canyonstorm_participation`, `/canyonstorm_log [date]`
>
> **📋 Survey** — `/setup_survey`, `/survey`, `/survey_post`
> **📈 Growth Tracking** — `/setup_growth`, `/growth`
>
> **💎 Premium Features**
> Unlock with `/upgrade` — Premium subscribers get the following:
> - `/setup_members`, `/sync_members`, `/survey_remind`,
>   `/desertstorm_remind`, `/canyonstorm_remind`
> - *Plus: birthday DMs, train assignment DMs, auto-mentions in train
>   announcements, thread destinations, and more.*
>
> **🔧 Utilities** — `/cancel`, `/donate`, `/upgrade`
>
> Footer (free): *Alliance Helper — Run /upgrade to unlock Premium features*
> Footer (premium): *💎 Premium is active. Thanks for supporting Alliance Helper!*

---

## 2. Event Announcements

### `/setup_events`

Add or edit event types and configure shared draft/announce channels & times.
**Tier:** Both (free tier capped at **5 events**). **Permissions:** Admin only.

If `event_draft_channel_id` is set and at least one event exists, a summary +
action menu is shown first:

> 🤖 **Embed: 📣 Event Setup** (blurple)
> *Your events are already configured. What would you like to do?*
>
> - **Draft Channel:** <#…>
> - **Announcement Channel:** <#…>
> - **Draft Time:** 12:00
> - **5-min Warning:** Yes
> - **Events:**
>   - **Plague Marauder** — 22:15 (UTC-5) Eastern …
>
> 🔘 row 0: `[⚙️ Edit Event Settings]` (primary) `[➕ Add Event]` (success)
> 🔘 row 1: `[✏️ Edit Event]` (secondary) `[🗑️ Delete Event]` (danger)
> 🔘 row 2: `[✅ No changes needed]` (secondary)

On `No changes needed`: `✅ No changes made.`
On Add/Edit/Delete the wizard skips channel-setting steps and jumps to the
event-list builder.
On `Edit Event Settings`: the full Step 1 → 5 wizard runs.

#### First-run / settings flow (Steps 1–4)

> 🤖 ⚙️ **Event Setup**
> Configure your alliance events. All events share the same draft channel,
> announcement channel, draft time, and 5-minute warning setting.

##### Step 1 of 5 — Draft Channel
> 🤖 **Step 1 of 5 — Draft Channel**
> Which channel should the bot post event announcement drafts for leadership
> to review? *(This applies to all events)*

Channel select with a `[➕ Create a new channel]` button (default name
`event-drafts`). 💎 Premium guilds also see threads in the picker.

##### Step 2 of 5 — Announcement Channel
> 🤖 **Step 2 of 5 — Announcement Channel**
> Which channel should approved announcements be posted to?
> *(This applies to all events)*

Same controls (default name `announcements`).

##### Step 3 of 5 — Draft Posting Time
> 🤖 **Step 3 of 5 — Draft Posting Time**
> What time should the bot post the draft each event day? *(in (UTC-5) Eastern …)*
> *(e.g. `12:00pm` for noon)*
>
> 🔘 `[✅ Use default: 12:00]`  🔘 `[✏️ Define my own]` (opens text modal)

If unparseable: `⚠️ Could not read that time. Try 12:00pm. Run /setup_events to try again.`

##### Step 4 of 5 — 5-Minute Warning
> 🤖 **Step 4 of 5 — 5-Minute Warning**
> Should the bot automatically post a 5-minute warning before events?
> *(This applies to all events)*
>
> 🔘 `[Yes]` (success)  🔘 `[No]` (secondary)

#### Step 5 of 5 — Event list editor

A loop showing the current event list with controls. Cap-reached message:
> 🤖 **Embed: 📊 Free tier limit reached** (orange)
> *You've used **5 of 5** events on the free tier. Upgrade to 💎 Alliance Helper Premium to unlock more.*

> 🤖 **Step 5 of 5 — Your Events:**
> 1. **Plague Marauder** — 🔁 3-day cycle at 22:15
> 2. **Zombie Siege** — 🔁 3-day cycle at 22:45 *(inactive)*
>
> 🔘 row 0: select `✏️ Edit an event...`
> 🔘 row 1: select `🗑️ Delete an event...`
> 🔘 row 2: `[➕ Add Event]` (primary) `[✅ Finish]` (success)

##### Per-event builder (Add or Edit)
1. **Event Name** — typed reply, e.g. `Plague Marauder (AE)`. Existing name shown when editing.
2. **Event Time** — keep-or-change view (default = existing or via text); accepts `10:15pm`, `9:00am`, or 24h `22:15`. Bad input: `⚠️ Could not read that time. Try 10:15pm. Run /setup_events to try again.`
3. **Schedule** —
   > 🔘 `[🔁 Repeating cycle]` (primary)  🔘 `[📅 Add manually each time]` (secondary)
4. If repeating:
   - **Anchor Date** — typed `March 30`, `April 14`. Smart-parsed to a recent or upcoming occurrence.
   - **Cycle Interval** — keep-or-change, default `3` days.
5. **Announcement Blurb** — keep-or-change view with three buttons:
   > 🔘 `[✅ Use default blurb]` (success)
   > 🔘 `[✏️ Enter my own]` (secondary)
   > 🔘 `[⏭️ Keep existing]` (secondary, only if editing and existing blurb is set)

   Default blurb: `<Name> at {time} ({server_time} Server Time).`
   Custom blurbs use `{time}` and `{server_time}` placeholders.
6. After save: `✅ Updated: **<Name>**` or `✅ Added: **<Name>**`.

#### Final summary

> 🤖 **Embed: ✅ Events Configured** (green)
> - Draft Channel / Announcement Channel / Draft Time / 5-min Warning
> - Events: bullet list

---

### `/events [date]`

Open the event editor for today or a specific date. **Tier:** Both.
**Permissions:** Leadership channel + leadership role.

If `date` is omitted, defaults to today. The string is parsed as `April 5`,
`Apr 5`, `4/5`, or `4/5/2026`. Bad parse:
`⚠️ Could not parse date `April 5`. Try formats like `April 5` or `4/5`.`

If the supplied date isn't an event day per the cycle:
`ℹ️ **April 5** is not an event day. Showing the next event date: **Saturday, April 6**.`

The bot then posts the same `📣 Event Editor` message described in the
[automatic flow](#automatic-event-flow-scheduler) below.

---

### `/events_log`

Show recent approved event posts. **Tier:** Both (window varies). **Permissions:** Leadership.

> 🤖 **Embed: 📣 Events Log — Past 7 Days** (blurple, free) / **Past 30 Days** (premium)
> *Showing approved event posts from the past 7/30 days.*
>
> **Approvals (N):**
> - ✅ **Approved by Catie at 12:01pm et** *— logged Sat Apr 5, 12:01pm ET*
> - ✅ **Approved by Sunshine at 12:15pm et** *— logged Wed Apr 2, 12:15pm ET*
>
> Footer (free): *Free tier: 7-day window. Upgrade to Premium for 30 days.*
> Footer (premium): *Reads from the leadership channel's message history.*

If no approvals: `*No event posts have been approved in the past 7 days.*`

Edge cases:
- Leadership channel not configured: `⚠️ Leadership channel isn't configured. Run /setup to configure it.`
- Bot can't fetch history: `⚠️ Bot does not have permission to read message history in the leadership channel.`

---

### Automatic event flow (scheduler)

Daily at the configured **Draft Time** in the guild's timezone, the scheduler
fires the event editor for each upcoming event date. The flow has three
distinct surfaces:

#### A. Editor (leadership channel)

Posted to the configured **Draft Channel** (falls back to leadership channel):

> 🤖 📣 **Event Editor** — adjust today's event schedule, then build the announcement.
>
> **Current events:**
> 1. **Plague Marauder** — 10:15pm ET (00:15 server)
> 2. **Zombie Siege** — 10:45pm ET (00:45 server)
>
> **Notes:** *None*
>
> 🔘 row 0: `[➕ Add Event]` (primary) `[✏️ Edit Time]` (secondary) `[🗑️ Remove Event]` (danger)
> 🔘 row 1: `[📝 Add Notes]` (secondary) `[📣 Build Announcement]` (success)

- **Add Event** — ephemeral select with optional events (Glacieradon, Blimp);
  prompts for time inline (`⏰ What time is **Blimp**? *(e.g. 10:30pm or 22:30)*`).
  Auto-deletes the prompt and reply on submit. `⚠️ Could not parse that time. Try again with Add Event.` if invalid.
- **Edit Time** — ephemeral select picking which event to edit; same time prompt.
- **Remove Event** — ephemeral select listing only optional events.
  `No optional events to remove. Marauder and Siege are always included.` if all are required.
- **Add Notes** — pings the user in-channel: `📝 @Catie — type your additional notes below, or type clear to remove existing notes.` Reply auto-deletes after save: `✅ Notes saved.` (5 s).
- **Build Announcement** — disables all buttons and posts the draft message
  (using the configured role mention) to the leadership channel for approval.

#### B. Approval card (leadership channel)

> 🤖 📣 **Announcement draft — please review and approve:**
>
> Hey @OGV!
> Here is the schedule for events today:
>
> - Marauder (AE) at 10:15pm (00:15 server). Make sure to have offline participation checked!
> - Zombies at 10:45pm (00:45 server). Be sure you have squads on your wall!
>
> 🔘 `[✅ Send As-Is]` (success)  🔘 `[✏️ Edit & Send]` (primary)

- **Send As-Is** — disables buttons, posts the message to the announcement
  channel, then stamps the leadership channel:
  > 🤖 ✅ **Approved by Catie at 12:01pm et**
  > ```
  > <full draft text>
  > ```
- **Edit & Send** — disables buttons, then posts a copy-paste prompt:
  > 🤖 ✏️ @Catie — copy and edit the message below, then send your revised version:
  > ```
  > Hey @OGV!
  > Here is the schedule…
  > ```
  After the edited reply (5-min wait), prompt + reply auto-delete and a new
  approval card appears:
  > 🤖 📝 **Revised draft** (edited by Catie):
  > <revised text>
  > 🔘 `[✅ Send As-Is]`  🔘 `[✏️ Edit & Send]`
  Edit timeout: `⏰ Edit timed out — no message received from @Catie within 5 minutes.`

#### C. Public announcement + 5-min warning

Posted to the configured **Announcement Channel**. Five minutes before the
first event time, the warning fires automatically (if enabled):

> 🤖 Marauder (AE) in 5 minutes! Make sure you hop online and get your points!
> Zombies right after, check your wall to make sure you have squads on it!

(For non-Marauder events: `<Name> in 5 minutes! Make sure you're online!`.)

A leadership-side stamp follows:
> 🤖 ⏱️ **5-minute warning auto-posted** at 10:10pm et

---

## 3. Train Schedule

### `/setup_train`

Configure the train tab, themes, tones, prompt templates, and daily reminders.
**Tier:** Both (free tier caps: themes 3, tones 3, templates 1).
**Permissions:** Admin only.

> 🤖 ⚙️ **Train Schedule Setup**
> *Configure how the train schedule works for your alliance.*

#### Step 1 of 7 — Schedule Sheet Tab
> 🤖 **Step 1 of 7 — Schedule Sheet Tab**
> Which tab in your Google Sheet stores the train schedule?
> ⚠️ *Make sure this tab exists in your sheet before continuing.*
>
> 🔘 `[✅ Use default: Train Schedule]`  🔘 `[✏️ Define my own]`

#### Step 2 of 7 — ChatGPT Blurb Generation
> 🤖 **Step 2 of 7 — ChatGPT Blurb Generation**
> Would you like the bot to help generate a ChatGPT prompt each day when you
> assign a train? This lets you quickly produce a personalised announcement
> blurb for the member.
> *(You can always set this up later by running /setup_train again)*
>
> 🔘 `[Yes]`  🔘 `[No]`

If **No**, skip to Step 7 (Reminders).

#### Step 3 of 7 — Themes (only if blurbs enabled)
> 🤖 **Step 3 of 7 — Themes**
> These appear as options when selecting a theme for a member's train day.
>
> **Defaults:**
> `Wartime, Birthday, Holiday`
>
> *Free tier: up to 3 themes. Upgrade for unlimited.*
>
> 🔘 `[✅ Use defaults]` (success)  🔘 `[✏️ Define my own]` (secondary)

If "Define my own": `Enter your themes as a comma-separated list:` (typed reply).
⚠️ Free-tier truncation note: `ℹ️ Free tier: only the first 3 themes were saved (Wartime, Birthday, Holiday). Upgrade to Premium to save more.`

#### Step 4 of 7 — Tones
Identical pattern, default list `Funny, Serious, Hype` (or whatever was previously saved).

#### Step 5 of 7 — Default Tone
> 🤖 **Step 5 of 7 — Default Tone**
> Which tone should be pre-selected by default?
>
> 🔘 *single select* — `Select default tone...`

On select: `✅ Default tone: **Funny**`.

#### Step 6 of 7 — Prompt Templates

A multi-template manager loop. Embed:

> 🤖 **Embed: Step 6 of 7 — Prompt Templates** (blurple)
> *Saved ChatGPT prompt templates. The default ⭐ is the one used by the
> blurb wizard unless a member's day overrides it.*
>
> `1.` **Default** ⭐ — *Write a 100-word blurb…*
> `2.` **Birthday** — *(empty)*
>
> *Slot usage: **2 of 1**.* (free)  /  *Slot usage: **2 of 10**.* (💎 premium)
>
> 🔘 row 0: `[➕ Add]` (success — disabled at cap) `[✏️ Edit]` (primary) `[⭐ Set Default]` (secondary)
> 🔘 row 1: `[🗑️ Delete]` (danger — disabled if only 1 template) `[✅ Done]` (success)

- **Add / Edit** — typed reply for `Template name` (≤ 50 chars, dup-check), then
  a typed reply for the body. `cancel` aborts; on Edit, `keep` keeps the existing body.
  Body prompt mentions placeholders `{name}`, `{theme}`, `{tone}`, `{notes}`.
  On save: `✅ Added: **Birthday** (2 of 10).` or `✅ Updated **Birthday**.`
- **Set Default** — pick from a dropdown, `⭐ Default set to **Birthday**.`
- **Delete** — pick from dropdown, `🗑️ Removed **Birthday**.` If only one
  remained, the bot replaces it with an empty `Default` and reports
  `🗑️ Removed **X**. (Restored an empty Default — you need at least one template.)`

#### Step 7 of 7 — Train Reminders

> 🤖 **Step 7 of 7 — Train Reminders**
> Should the bot post a reminder to leadership when someone is assigned the
> train each day?
>
> 🔘 `[Yes]`  🔘 `[No]`

If **Yes**:

##### Step 7a — Reminder Channel
> 🤖 **Step 7a of 7 — Reminder Channel**
> Which channel should the train reminder be posted to?
>
> 🔘 channel select (suggested name `leadership`, threads pickable on premium)

##### Step 7b — Reminder Time
> 🤖 **Step 7b of 7 — Reminder Time**
> What time should the reminder fire? *(in your timezone: (UTC-5) Eastern …)*
> *(e.g. `10:00pm`, `9:00am`)*
>
> 🔘 `[✅ Use default: 10:00pm]`  🔘 `[✏️ Define my own]`

If unparseable: `⚠️ Could not read that time. Using 10:00pm as default.`

#### Final summary

> 🤖 **Embed: ✅ Train Schedule Configured** (green)
> - Sheet Tab / Blurb Generation / Reminders
> - Reminder Channel / Reminder Time
> - Default Tone / Themes / Tones
> - Templates (N): list + default name
> - Default Template Preview (code block, first 200 chars)

---

### `/train`

View the train schedule with action buttons. **Tier:** Both. **Permissions:** Leadership.

> 🤖 **Embed: 🚂 Alliance Train Schedule** (gold, with timestamp)
>
> 🟢 Saturday, April 5 — Catie 🎂 — ✅ Done
> Sunday, April 6 — [Empty]
> Monday, April 7 — Sunshine
> *(…14 days ahead total)*
>
> **✅ Past 7 Days**
> Pink, Mer, Lito
>
> 🔘 `[➕ Add]` (success) `[✏️ Update]` (primary) `[📋 Generate Prompt]` (secondary) `[🗑️ Clear]` (danger)

Empty schedule: `*No schedule set. Use the **➕ Add** button below to add entries.*`

#### `[➕ Add]`

Opens 📋 modal `Add Train Entry` with two text inputs:
- **Date** — placeholder `e.g. April 5 or 4/5`, max 20 chars
- **Member name** — placeholder `Exactly as it should appear`, max 64 chars

On bad date: `⚠️ Could not parse date April 5. Try formats like April 5 or 4/5.`
On success (blurbs enabled):
> 🤖 ✅ Added **Catie** for **Saturday, April 5**.
>
> Run the blurb wizard now to build the ChatGPT prompt?
>
> 🔘 `[✅ Run blurb wizard]` (success)  🔘 `[⏭️ Skip]` (secondary)

If blurbs disabled, just `✅ Added **Catie** for **Saturday, April 5**.`

#### `[✏️ Update]`

Builds a list of entries from -7 to +30 days, ephemeral select-menu:
> 🤖 Select an entry to update:
> 🔘 select `Choose an entry to update...` — `Sat Apr 5 — Catie`, `Sun Apr 6 — Sunshine` …

If no entries:
`ℹ️ No entries to update in the past 7 / next 30 days. Use **➕ Add** to create one.`

Selecting opens 📋 modal `Update Train Entry` pre-filled with existing date + name.
On save: `✅ Updated → **NewName** on **Saturday, April 5**.` plus the same
`Re-run the blurb wizard?` view as Add (if blurbs enabled).

#### `[📋 Generate Prompt]`

Lists entries from today to +14 days that already have a name *and* a theme
filled in.

> 🔘 select `Choose an entry...` — `Sat Apr 5 — Catie`

If empty: `ℹ️ No filled entries in the next 14 days. Use **➕ Add** or **✏️ Update**, then run the blurb wizard to fill in theme/tone/notes first.`

On select, the prompt is built and posted publicly:
> 🤖 ✅ **ChatGPT prompt for Catie** — copy and paste into the thread:
> ```
> Member: Catie
> Theme: Birthday — Funny
> Notes: …
> ```

#### `[🗑️ Clear]`

> 🤖 ⚠️ Clear the entire train schedule? This cannot be undone.
> 🔘 `[Yes, clear it]` (danger)  🔘 `[Cancel]` (secondary)

On confirm: `🗑️ Train schedule cleared.`

---

### Blurb wizard

Triggered by:
- **Run blurb wizard** after Add or Update on `/train`
- **📋 View & Get Prompt** button on the daily reminder
- (Internally, future versions may surface another entry point)

If the user already has an active wizard:
`⚠️ You already have an active session. Use /cancel to stop it first.`

> 🤖 🚂 **Train Blurb Wizard for Catie** — Saturday, April 5
> *(Type /cancel at any time to stop)*

#### Step 1 of 3 — Theme
> 🤖 **Step 1 of 3 — Theme**
> Select the theme for this train:
>
> 🔘 *single select* — `Choose a theme...` — populated from configured themes

If user selects `Custom`, the bot asks `Type your custom theme:` and waits for typed reply.

#### Step 2 of 3 — Tone
> 🤖 **Step 2 of 3 — Tone**
> Select the tone:
>
> 🔘 *single select* — `Choose a tone...`

#### Step 3 of 3 — Notes
> 🤖 **Step 3 of 3 — Notes** *(highly recommended)*
> Add anything personal — role, personality, achievements. Type your notes,
> or type `skip`:

#### Step 4 of 4 — Template *(💎 Premium, only if >1 template configured)*
> 🤖 **Step 4 of 4 — Template** *(💎 Premium)*
> You have multiple saved templates. Pick one for this prompt:
>
> 🔘 *single select* — `Pick a saved template…`

On select: `✅ Template: **Birthday**`.

#### Output
> 🤖 ✅ **ChatGPT prompt for Catie** — copy and paste into the thread:
> ```
> <rendered template with {name}/{theme}/{tone}/{notes} substituted>
> ```

⏰ Timeouts at any step: `⏰ Wizard timed out.`
On `/cancel`, the active step is closed silently, prompt messages cleaned up.

---

### `/train_log [date]`

Show the train prompt log. **Tier:** Both (window: free 7d / premium 30d).
**Permissions:** Leadership.

If `date` is supplied, the bot tries to find that exact entry:

> 🤖 **Embed: 🚂 Train Prompt Log** (blurple)
> - **Date:** Saturday, April 5, 2026
> - **Name:** Catie
> - **Theme:** Birthday
> - **Tone:** Funny
> - **Notes:** *…*
> - **Prompt Retrieved:** ✅ Yes (or ❌ No)

If no entry: `*No train entry found for April 5, 2026.*`

If `date` is omitted, the bot lists up to 20 entries within ±N days from today:

> 🤖 **Embed: 🚂 Train Prompt Log** (blurple)
> - **Sat Apr 5** — Catie · Birthday · prompt ✅
> - **Fri Apr 4** — Pink · Wartime · prompt ❌
> *(…)*
>
> Footer (free): *Free tier: 7-day window. Upgrade to Premium for 30 days.*
> Footer (premium): *Showing the most recent 20 entries within ±30 days. Pass a date to filter.*

Empty: `*No train entries in the past 7 days.*`
Date parse failure: `⚠️ Could not parse date **<text>**. Try a format like April 14 or 4/14.`

---

### `/train_addbirthdays`

Manually run the birthday-to-train check. **Tier:** Both. **Permissions:** Leadership.

Outcomes (ephemeral):
- `✅ Birthday check complete — added **N** birthday entries to the schedule.`
- `✅ Birthday check complete — added **N** birthday entries to the schedule. ⚠️ **M** conflict(s) posted above require manual action.`
- `⚠️ Birthday check complete — **M** conflict(s) posted above require manual action.`
- `✅ Birthday check complete — no new entries to add within the next 14 days.`
- `⚠️ Birthday check failed: <error>`

Conflicts are posted to the channel directly:
> 🤖 🚨 **Birthday scheduling conflict — manual action needed!**
> **Catie's** birthday is **Saturday, April 5** but all three surrounding dates are taken:
> - Apr 4 (Pink)
> - Apr 5 (Sunshine)
> - Apr 6 (Mer)
>
> Please manually add Catie to the schedule.

---

### Daily train reminder loop

Every minute, for each guild whose train is enabled and reminder time matches
guild-local time, the bot fires a reminder if a name is scheduled for today.

#### Free tier (or no DM roster), blurbs ON:
> 🤖 🚂 **Reset! Today's train is for Catie.**
>
> Click below whenever you're ready to get the ChatGPT prompt — no rush, run
> it when the team is available.
>
> ⚠️ *If the button stops working after a bot restart, use `/train` → 📋 Generate Prompt instead.*
>
> 🔘 `[📋 View & Get Prompt]` (success)

#### 💎 Premium with roster:
The name is replaced by `<@discord_id>` when the roster has the member's
Discord ID. The bot also DMs that member directly:
> 🤖 (DM) 🚂 Heads up — **today's train is for you!** Leadership has been
> notified, so look out for the announcement.

#### Blurbs OFF:
Just `🚂 **Reset! Today's train is for Catie.**` (no button).

Clicking the button enforces the leadership-role guard:
- Bot not configured: `⚙️ Bot not configured. Run /setup.`
- Wrong role: `⛔ You need the **OGV Leadership** role.`
Otherwise, the button disables itself and the [Blurb wizard](#blurb-wizard) launches in the same channel.

---

## 4. Birthdays

### `/setup_birthdays`

Configure birthday tracking & announcements. **Tier:** Both.
**Permissions:** Admin only.

> 🤖 ⚙️ **Birthday Tracking Setup**
> Configure how the bot tracks member birthdays.

#### Step 1 of 8 — Enable
> 🤖 **Step 1 of 8 — Enable birthday tracking?**
> Should the bot track member birthdays from your Google Sheet?
>
> 🔘 `[Yes]`  🔘 `[No]`

If **No**: saves `enabled=0` and reports `✅ Birthday tracking disabled.`

#### Step 2 of 8 — Sheet Tab
Keep-or-change view, default `Birthdays`:
> 🤖 **Step 2 of 8 — Sheet Tab**
> Which tab in your Google Sheet contains birthday data?
> ⚠️ *Make sure this tab exists in your sheet before continuing.*

#### Step 3 of 8 — Name Column
Keep-or-change, expects single letter `A`–`Z`. Bad input:
`⚠️ Please enter a single column letter like A. Run /setup_birthdays to try again.`

#### Step 4 of 8 — Birthday Column
Same pattern, default letter `B`.

#### Step 5 of 8 — Train Schedule Integration
> 🤖 **Step 5 of 8 — Train Schedule Integration**
> Should the bot automatically add members to the train schedule on their birthday?
>
> 🔘 `[Yes]`  🔘 `[No]`

#### Step 6 of 8 — Birthday Placement (only if integration enabled)
> 🤖 **Step 6 of 8 — Birthday Placement**
> If the member's birthday is already taken on the train schedule, what should the bot do?
>
> 🔘 `[🎂 Birthday only]` (primary) → `✅ Placement: **Birthday only**`
> 🔘 `[📅 Assign nearby if taken]` (secondary) → `✅ Placement: **Assign 1 day before or after if birthday is taken**`

#### Step 7 of 8 — Lookahead Days (only if integration enabled)
Keep-or-change, default `14`:
> 🤖 **Step 7 of 8 — Lookahead Days**
> How many days in advance should birthdays be added to the train schedule?
> *(we recommend 14)*

Bad input: `⚠️ Please enter a number like 14. Run /setup_birthdays to try again.`

#### Step 8 of 8 — Birthday Reminders
> 🤖 **Step 8 of 8 — Birthday Reminders**
> Should the bot post a message in Discord on a member's birthday?
> *(It will post: "🎂 Today is **[name]**'s birthday!")*
>
> 🔘 `[Yes]`  🔘 `[No]`

If **Yes**:
##### Step 8a — Channel
Channel select (default name `birthdays`, threads on premium).

##### Step 8b — Reminder Time
Keep-or-change with default `8:00am`. Bad time falls back to `8:00am` with
`⚠️ Could not read that time. Using 8:00am as default.`

#### Final summary

> 🤖 **Embed: ✅ Birthday Tracking Configured** (green)
> - Sheet Tab / Name Column / Birthday Column / Discord ID Column
> - Train Integration / Placement / Lookahead
> - Reminders / Reminder Channel / Reminder Time

---

### `/birthdays`

Show upcoming birthdays in the next 14 days. **Tier:** Both. **Permissions:** Leadership.

> 🤖 **Embed: 🎂 Upcoming Birthdays — Next 14 Days** (magenta)
>
> - **Saturday, April 5** — Catie *(**Today!**)*
> - **Sunday, April 6** — Sunshine *(Tomorrow)*
> - **Friday, April 11** — Pink *(in 6 days)*
>
> Footer: *Source: Birthdays · Run /setup_birthdays to change settings*

Empty: `*No birthdays in the next 14 days.*`
Could-not-load: `⚠️ Could not load birthdays: <error>`
No data: `⚠️ No birthdays found in **Birthdays**. Run /setup_birthdays to verify the tab and column settings.`

---

### Daily birthday announcement loop

At the configured reminder time (in the guild's timezone) the bot posts to the
configured birthday channel for every member whose birthday is today:

> 🤖 🎂 Today is <@123456789>'s birthday!  *(if Discord ID is in the sheet)*
> 🤖 🎂 Today is **Catie**'s birthday!     *(otherwise)*

💎 Premium also sends a DM to the member directly (when the Discord ID is known):
> 🤖 (DM) 🎂 Happy birthday, **Catie**! Wishing you a great day from
> everyone at the alliance.

If `train_integration` is on, every minute the bot scans for upcoming
birthdays in the lookahead window and auto-adds them to the train schedule
(see [`/train_addbirthdays`](#train_addbirthdays) for the conflict alert
format).

---

## 5. Desert Storm & Canyon Storm

The two events are structurally identical — same tabs, same flows, swapped
labels. Desert Storm is rendered in full below; Canyon Storm specifics are
called out only where they differ.

### `/setup_desertstorm` & `/setup_canyonstorm`

Configure DS/CS sheet tab, teams, log channel, and mail templates.
**Tier:** Both. **Permissions:** Admin only.

> 🤖 ⚙️ **Desert Storm Setup**

#### Step 1 of 4 — Sheet Tab
Keep-or-change, default `DS Assignments` (or `CS Assignments`):
> 🤖 **Step 1 of 4 — Sheet Tab**
> Which tab in your Google Sheet stores the Desert Storm zone assignments?
> ⚠️ *Make sure this tab exists in your sheet before continuing.*
> ℹ️ *The bot will manage the data structure of this tab automatically — you
> don't need to set up any specific columns or formatting beforehand.*

#### Step 2 of 4 — Which teams?
> 🤖 **Step 2 of 4 — Which teams do you run for Desert Storm?**
>
> 🔘 `[Team A & Team B]` (primary)
> 🔘 `[Team A only]` (secondary)
> 🔘 `[Team B only]` (secondary)

On select: `✅ Teams: **Team A & Team B**` (or A only / B only).

#### Step 3 of 4 — Storm Log Channel
Channel select (default suggestion `storm-log`, threads on premium).

#### Step 4 of 4 — Mail Template

If both teams chosen, first ask:
> 🤖 **Step 4 of 4 — Mail Template**
> Do you want one template that applies to both teams, or separate templates per team?
>
> 🔘 `[One template for both teams]` (primary)
> 🔘 `[Separate templates per team]` (secondary)

For each template (single OR per-team):

> 🤖 **Desert Storm Mail Template — Team A & B**
> When you draft the mail each week, you will be able to select the time slot
> when you are running that team's Desert Storm.
>
> Here is the default template:
> ```
> <GENERIC_DS_TEMPLATE — uses {alliance_name}, {zones}, {subs}, {time}>
> ```
> Would you like to use this or edit it?
>
> 🔘 `[✅ Use default template]` (success) — `✅ Using default template for Team A & B.`
> 🔘 `[✏️ Edit template]` (secondary)

If **Edit**:
> 🤖 Paste your custom template for **Team A & B**. You can copy the default
> above and modify it, or write your own.
>
> **Available placeholders:**
> - `{alliance_name}` — your alliance name
> - `{zones}` — zone assignments block
> - `{subs}` — substitute members
> - `{time}` — event time (auto-filled when drafting)
>
> *This form will time out in 5 minutes. You can run /setup_desertstorm again if it times out.*

User pastes a long message; the bot stores it.

#### Final summary

> 🤖 **Embed: ✅ Desert Storm Configured** (green)
> - Sheet Tab / Teams / Timezone / Log Channel
> - Template A Preview (code block, first 150 chars)
> - Template B Preview (only if separate)

**CS:** Identical, with the labels swapped to "Canyon Storm" and the default
tab name `CS Assignments`.

---

### `/desertstorm` & `/canyonstorm`

Show the configured setup + the current rosters. **Tier:** Both.
**Permissions:** Leadership.

> 🤖 **Embed: ⚔️ Desert Storm** (dark red, DS) / **🏜️ Canyon Storm** (gold, CS)
> - Sheet Tab: DS Assignments
> - Log Channel: <#…>
> - Time Option 1: 4PM EST — 16:00 local / 18:00 server
> - Time Option 2: 9PM EST — 21:00 local / 23:00 server
> - **Current Mail Template (Team A):**
> ```
> ZONE ASSIGNMENTS
> Nuclear Silo: <names>
> Oil Refinery I: <names>
> …
> SUB PAIRS (Starter - Sub)
> <starter> - <sub>
> ```
> Footer: *Run /setup_desertstorm to update. Run /desertstorm_draft to generate a draft.*

If load fails: `⚠️ Could not load: <error>` in the template field.

---

### `/desertstorm_draft` & `/canyonstorm_draft`

Generate a Desert Storm or Canyon Storm mail draft. **Tier:** Both.
**Permissions:** Leadership.

#### Step 1 — Pick team
Posted to the channel:
> 🤖 🔥 **Desert Storm Draft** — started by @Catie
>
> Which team are you drafting for?
>
> 🔘 `[Team A]` (primary)  🔘 `[Team B]` (success)

After click, the prompt message is auto-deleted.
⏰ `⏰ Timed out. Use /desertstorm_draft to start again.`

#### Step 2 — Editable template

> 🤖 🔥 **Desert Storm Team A Draft**
>
> Copy the block below, make your changes, and paste it back. Anything that
> hasn't changed can stay as-is.
> ```
> ZONE ASSIGNMENTS
> Nuclear Silo: <names>
> Oil Refinery I: <names>
> …
> SUB PAIRS (Starter - Sub)
> <starter> - <sub>
> ```

Ephemeral confirmation: `✅ Team A template posted.`

#### Step 3 — Pick time
> 🤖 ⏰ What time is Desert Storm this week?
>
> 🔘 `[4PM EST: 16:00 (18:00)]` (secondary, label dynamic from setup)
> 🔘 `[9PM EST: 21:00 (23:00)]` (secondary)

#### Step 4 — Paste edits
> 🤖 📋 @Catie — paste your edited assignments below.
> *(10 minutes to respond — type `cancel` to stop)*

User pastes the edited block. Both prompt and reply auto-delete.
- `cancel` typed → `❌ Draft cancelled.`
- Unparseable → `⚠️ Could not parse any zone assignments. Make sure the format matches the template and try /desertstorm_draft again.`
- Some lines skipped → `⚠️ Some lines were skipped:` followed by `• Could not parse zone line: …`

#### 💎 Premium template picker (only if >1 template saved)
> 🤖 💎 You have multiple saved templates. Pick one for this draft:
>
> 🔘 *single select* — `Pick a saved template…`

#### Step 5 — Mail preview & approval
> 🤖 📬 **Desert Storm Team A mail preview:**
>
> <full rendered mail using template>
>
> Does this look right?
>
> 🔘 `[✅ Looks Good — Save & Copy]` (success)
> 🔘 `[✏️ Edit & Redo]` (primary)
> 🔘 `[❌ Cancel]` (danger)

- **Looks Good** — saves assignments to the sheet, then posts:
  > 🤖 ✅ **Desert Storm Team A mail — ready to copy:**
  > ```
  > <full mail>
  > ```
- **Edit & Redo** — re-posts the editable template and loops back to Step 3.
- **Cancel** — disables buttons and ephemerally `❌ Draft cancelled.`

**CS:** Identical with `🔥` replaced by `⚡`, "Desert Storm" replaced by "Canyon
Storm", template parser uses STAGE 1 / STAGE 2 / STAGE 3 sections instead of
ZONE ASSIGNMENTS / SUB PAIRS, and the **CSTimeSelectView** has hardcoded time
buttons (`10AM EST / 12:00 Server` and `9PM EST / 23:00 Server`) — a small
inconsistency vs. DS, which uses the configured TimeSelectView. The CS approval
view has the same three buttons.

---

### `/desertstorm_participation` & `/canyonstorm_participation`

Log Desert Storm / Canyon Storm participation data. **Tier:** Both.
**Permissions:** Leadership.

If the user has an active log session: `⚠️ You already have an active log session. Use /cancel to stop it first.`

Initial ephemeral ack: `📋 Starting DS log...` (or `CS log...`).

> 🤖 📋 **Desert Storm Log** — started by @Catie
> *Use `/cancel` at any time to stop.*

DS uses 5 steps; CS skips the DS-only steps.

#### Step 1 — Event date
> 🤖 **Step 1 — Event date**
> Type the date (e.g. `April 14`, `4/14`) or type `today`:

Reply auto-deletes. Bad date: `⚠️ Could not parse that date. Use the log command to start again.`

#### Step 2 (DS only) — Vote count
> 🤖 **Step 2 — Vote count**
> How many members voted in the participation poll? (type a number)

Bad input: `⚠️ That doesn't look like a number. Use the log command to start again.`

#### Loading roster
> 🤖 ⏳ Gathering member list...

If it fails: `⚠️ Could not load member names. Run /setup_birthdays (or another module setup) to confirm the member tab name and try again.`

#### Step 3 — Sitting out (DS) / Step 2 — Sitting out (CS)
> 🤖 **Step 3 — Sitting out this week**
> Press **Enter Names** to type who is sitting out today. Press **Skip** if none.
> *Roster: Pink, Mer, Lito, Catie, Sunshine, …*
>
> 🔘 `[✏️ Enter Names]` (primary)  🔘 `[Skip (none)]` (secondary)

Clicking **Enter Names** opens 📋 modal `Sitting Out` with a paragraph text input
labeled `Names (comma-separated or one per line)` (placeholder
`e.g. Jon, Lionel, Ice — or leave blank and submit for none`, max 1000 chars).

If unrecognized names are submitted:
> 🤖 ⚠️ **Not recognized:** Buster, NewKid
> These names aren't in the roster. Are they visitors or did you make a typo?
>
> 🔘 `[Save as Visitor]` (secondary)  🔘 `[Re-enter Names]` (primary)

Final state of the message:
> 🤖 **Entered (3):** Pink, Mer, Lito
> **Visitors:** Buster, NewKid

Or if Skip clicked: `*Skipped — none.*`

#### Step 4 (DS only) — RTF No Vote
Same controls. Modal title: `RTF No Vote`.

> 🤖 **Step 4 — Requested to Fight but did not vote**
> Press **Enter Names** to type who submitted RTF but did not vote. Press **Skip** if none.

#### Step 5 (DS) / Step 3 (CS) — Prior sit-outs
First a loading message:
> 🤖 ⏳ Checking previous log...

If no prior names found: `**Step 5 — Prior sit-outs**\n*(No prior sit-outs found in last log — skipping)*` (auto-deletes after 5 s).

Otherwise:
> 🤖 **Step 5 — Prior sit-outs who did not vote this week**
> *(CS variant: "did not request to fight this week")*
> These members sat out last time. Select any who did not participate this week. Press **Skip** if none.
>
> 🔘 *multi-select* — `Select prior sit-outs who didn't participate`
> 🔘 row 1: `[✅ Done]` (success)  `[Skip (none)]` (secondary)

#### Save + summary
> 🤖 💾 Saving log...
> 🤖 ✅ **Log saved!**
>
> 📋 **Desert Storm Log — Saturday, April 5, 2026**
> **Votes:** 38     *(DS only)*
> **RTF No Vote:** Mer, Lito     *(DS only)*
> **Sitting Out:** Pink, Buster
> **Prior Sit-Out No Vote:** Sunshine     *(or "No Request" for CS)*

A copy of the summary is also posted to the configured storm-log thread,
unless the command was already invoked from that thread.

If sheet save fails: `⚠️ Error saving to sheet: <error>`

⏰ Timeouts at any step: `⏰ Timed out. Use the log command to start again.`
On `/cancel`: `❌ Log cancelled.`

---

### `/desertstorm_log [date]` & `/canyonstorm_log [date]`

View a Desert Storm or Canyon Storm log entry. **Tier:** Both (free tier:
last 4 most-recent dates only). **Permissions:** Leadership.

If `date` parses, the bot fetches that entry; otherwise defaults to today.
Bad date: `⚠️ Could not parse date **<text>**. Try a format like April 14 or 4/14.`

⚠️ **Free tier cap:** when the requested date isn't among the last 4 most-recent
log dates, the bot replies with the orange limit-reached embed:
> 🤖 **Embed: 📊 Free tier limit reached** (orange)
> *You've used **4 of 4** most-recent log entries on the free tier. Upgrade
> to 💎 Alliance Helper Premium to unlock more.*
> *This limit applies to: Desert Storm log lookback*

If no entry exists: `❌ No **Desert Storm** log found for **April 5, 2026**.`

Otherwise, a public message:
> 🤖 📋 **Desert Storm Log — Saturday, April 5, 2026**
> **Votes:** 38
> **RTF No Vote:** Mer, Lito
> **Sitting Out:** Pink
> **Prior Sit-Out No Vote:** Sunshine

(CS variant skips Votes / RTF No Vote and uses "Prior Sit-Out No Request".)

---

### `/desertstorm_remind` & `/canyonstorm_remind`

💎 DM every roster member to participate in this week's storm.
**Tier:** Premium only. **Permissions:** Leadership + Member Roster Sync configured.

If not premium:
> 🤖 **Embed: 🔒 Storm participation DMs is a Premium feature** (purple)
> *Storm participation reminders are part of Alliance Helper Premium and
> require Member Roster Sync (`/setup_members`). Run `/upgrade` to unlock.*
> *(plus the upgrade button if a SKU is configured)*

If premium but roster not configured:
`⚙️ Member Roster Sync isn't configured yet. Run /setup_members first.`

Otherwise (deferred ephemeral):

DM sent to each row's Discord ID:
> 🤖 (DM) ⚔️ **Desert Storm reminder** — your alliance is preparing for this
> week's Desert Storm. Please confirm your participation in Discord and
> check the team channel for your zone assignment. Good luck out there!

Final report (ephemeral):
`✅ Sent **23** Desert Storm reminder DMs. **2** skipped.`

If the sheet read fails: `⚠️ Could not read the roster sheet: <error>`

**CS:** identical wording with "Canyon Storm" substituted.

---

## 6. Survey

### `/setup_survey`

Configure squad-powers survey. **Tier:** Both (free cap: 5 questions; only Text
and Dropdown question types). **Permissions:** Admin only.

> 🤖 ⚙️ **Survey Setup**
> Configure the squad powers survey for your alliance.

#### Step 1 of 6 — Survey Channel
Channel select (default `squad-survey`, threads on premium).

#### Step 2 of 6 — Survey Notification Channel
Channel select (default `survey-responses`).

#### Step 3 of 6 — Member Statistics Tab
Keep-or-change, default `Squad Powers`.

#### Step 4 of 6 — Survey History Tab
Keep-or-change, default `Survey History`.

#### Step 5 of 6 — Intro Message
> 🤖 **Step 5 of 6 — Survey Intro Message**
> When your survey is posted, what introductory message do you want your
> members to see before they take the survey?
>
> **Example:**
> *Please fill out this survey each week to help us track squad powers,
> balance our teams, and prepare for season events!*

User types a free-form reply.

#### Step 6 of 6 — Survey Questions

> 🤖 **Step 6 of 6 — Survey Questions**
>
> **Default questions (Last War):**
> 1. **1st Squad Power** — text
> 2. **1st Squad Type** — dropdown: Missile, Air, Tank
> *(…)*
>
> **Your existing questions:**
> *(no questions configured yet)*  *(or list)*
>
> Would you like to use the defaults, edit your existing questions, or start from scratch?
>
> 🔘 `[✅ Use default questions]` (success)
> 🔘 `[✏️ Edit existing questions]` (primary)
> 🔘 `[🔄 Start from scratch]` (secondary)

If "default": questions copied from `OGV_SURVEY_QUESTIONS`.
If "edit" or "scratch": loop into the question builder.

#### Question builder loop

> 🤖 **Survey Questions:**
> 1. **1st Squad Power** — text *(help: e.g. 43.27)*
> 2. **1st Squad Type** — dropdown: Missile, Air, Tank
>
> 🔘 row 0: select `✏️ Edit a question...`
> 🔘 row 1: select `🗑️ Delete a question...`
> 🔘 row 2: `[➕ Add Question]` (primary) `[✅ Finish Survey Setup]` (success)

⚠️ Add at cap: shows the orange `Free tier limit reached` embed for "Survey Questions".

##### Per-question builder
1. **Label** — typed reply (e.g. `1st Squad Power`).
2. **Answer Type** — single select:
   - Text — member types their answer
   - Dropdown — member selects from a list
   - 💎 Numeric — number with min/max validation
   - 💎 Multi-select — pick multiple options
   - 💎 Date — formatted date entry
3. **Help Text** — typed reply, or `none` to skip.
4. If dropdown / multi_select: typed comma-separated **Options** (max 25).
5. If 💎 numeric: typed `min,max` / `min,` / `,max` / `none`.
6. If 💎 date: typed strptime format, or `default` for `%m/%d/%Y`.

On save: `✅ Added: **1st Squad Power** — 1 question(s) so far.` or
`✅ Updated: **1st Squad Power**`. Delete: `🗑️ Removed: **1st Squad Power**`.

#### Final summary

> 🤖 **Embed: ✅ Survey Configured** (green)
> - Survey Channel / Notification Channel
> - Stats Tab / History Tab
> - Questions: bullet list
> - Footer: *Run /setup_survey again to update. Run /survey_post to post the survey button.*

---

### `/survey`

Show the configured questions. **Tier:** Both. **Permissions:** Leadership.

> 🤖 **Embed: 📋 Survey Configuration** (blurple)
> **1. 1st Squad Power** *(text)*
>    _e.g. 43.27_
> **2. 1st Squad Type** *(dropdown: Missile, Air, Tank)*
> *(…)*
>
> - **Stats Tab:** Squad Powers
> - **History Tab:** Survey History
> - **Intro Message:** ✅ Configured / ❌ Not configured
>
> Footer: *Run /setup_survey to update. Run /survey_post to post the button.*

If empty: `*No survey questions configured. Run /setup_survey to add some.*`

---

### `/survey_post`

Post (or repost) the persistent survey button. **Tier:** Both.
**Permissions:** Leadership.

Posts to the configured survey channel:
> 🤖 **Let us know your Squad Powers!**
>
> Please fill out this survey each week, if possible, to help us keep track
> of squad powers, better balance our Desert Storm teams, track alliance
> growth, and prepare for season events!
>
> *Role required: @OGV*
>
> 🔘 `[📋 Answer]` (success) — persistent (`custom_id="survey_answer_button"`)

Ephemeral ack: `✅ Survey button posted.`
Errors: `⚠️ Could not find the survey channel.` / `⚙️ Bot not configured. Run /setup first.`

---

### Survey thread flow (member-facing)

When a server member clicks `[📋 Answer]`:

#### Guard checks
- Bot not set up: `⚙️ This bot hasn't been set up yet.`
- Wrong role: `⛔ You need the **OGV** role to fill out this survey.`

#### Thread creation
> 🤖 (ephemeral) 🚀 Let's get started! Your private thread is being created...

A private thread named `survey-squad-powers-<username>` is created and the user added.

> 🤖 (ephemeral) 🚀 Your thread is ready — head over here to get started: <#thread>

If thread creation fails: `⚠️ Could not create your survey thread: <error>`

#### Question loop (in the private thread)

For each configured question:
- **Text:**
  > 🤖 **<Label>**
  > *<placeholder>*
  > *Maximum characters: 10*
- **Dropdown:**
  > 🤖 **<Label>**
  > 🔘 *single select* — `<placeholder or "Select <Label>...">`

  On select, message becomes `**<Label>:** <choice>`.
- **💎 Numeric:** plain text reply, validated against min/max:
  > 🤖 **<Label>**
  > *<placeholder>*
  > *(min: 0, max: 100)*

  Bad: `⚠️ <input> isn't a number. Please try the survey again.`
  Out of range: `⚠️ Must be at least **0**. Please try the survey again.` / `Must be at most **100**`.
- **💎 Multi-select:** Discord multi-select up to all options. On select, message becomes `**<Label>** <comma list>`.
- **💎 Date:** typed reply parsed via strptime:
  > 🤖 **<Label>**
  > *<placeholder>*
  > *(format: `%m/%d/%Y`)*

  Bad: `⚠️ <input> doesn't match %m/%d/%Y. Please try the survey again.`

⏰ At any step: `⏰ Survey timed out. You can start again by clicking the Answer button.`

#### Save
> 🤖 ⏳ Saving your responses...

If save fails:
`⚠️ There was an error saving your responses: <error>\nPlease let leadership know.`

#### Leadership notification (in the survey notification channel)

> 🤖 **Embed: 📋 New Survey Response** (blurple)
> - **Member:** @user
> - **Submitted:** April 5, 2026 at 12:01 PM UTC
> - **Responses:**
>   - **1st Squad Power:** 43.27
>   - **1st Squad Type:** Missile
>   - **2nd Squad Power:** 38.10
>   - **3rd Squad Power:** 32.45
>   - **Drone Level:** 30
>   - **Gorilla Level:** 18
>   - **THP:** 1500M
>   - **Total Kills:** 12M
>   - **Profession:** War Leader — Charge Banner: Yes

#### Closing the thread

> 🤖 **Embed: ✅ Survey Complete!** (green)
> **Thank you!**
> Your response has been saved successfully! Thanks for keeping your stats up
> to date, it helps us to balance teams, track alliance growth, and prepare
> for season events.
> Footer: *This thread will be deleted in 60 seconds or you can close it now.*
>
> 🔘 `[❌ Close Thread]` (secondary)

After click or 60 s, the thread is deleted. If deletion fails (no perms), the
bot prints to stderr but the user sees nothing more.

---

### `/survey_remind`

💎 DM every roster member to fill out the survey.
**Tier:** Premium only. **Permissions:** Leadership + Member Roster Sync.

Locked embed for non-premium:
> 🤖 **Embed: 🔒 Survey reminder DMs is a Premium feature** (purple)
> *Reminder DMs are part of Alliance Helper Premium and require Member Roster
> Sync to be configured (`/setup_members`). Run `/upgrade` to unlock.*

DM body sent to each member:
> 🤖 (DM) 📋 **Friendly reminder** — your alliance is asking you to fill out
> the squad-powers survey this week. Open the survey channel in Discord and
> click the **📋 Answer** button to get started. Thanks!

Final ack: `✅ Sent **23** reminder DMs. **2** skipped (DMs closed, missing ID, or other failures).`

---

## 7. Growth Tracking

### `/setup_growth`

Configure source tab, metrics, and snapshot schedule. **Tier:** Both
(free cap: 5 metrics, monthly schedule only). **Permissions:** Admin only.

> 🤖 ⚙️ **Growth Tracking Setup**
> Configure how the bot tracks your alliance's growth over time. Each month
> (or on your chosen schedule), the bot takes a snapshot of your members'
> stats and records them in your Google Sheet so you can track progress.

#### Step 1 of 7 — Enable
> 🤖 **Step 1 of 7 — Enable growth tracking?**
> Should the bot automatically take snapshots of your members' stats on a schedule?
>
> 🔘 `[Yes]`  🔘 `[No]`

If **No**: saves disabled and reports `✅ Growth tracking disabled.`

#### Step 2 of 7 — Source Tab
Keep-or-change, default `Squad Powers`.

#### Step 3 of 7 — Data Start Row
Keep-or-change, default `2`. Bad input: `⚠️ Please enter a row number like 2. Run /setup_growth to try again.`

#### Step 4 of 7 — Name Column
Keep-or-change, default `A`. Bad: `⚠️ Please enter a single column letter like A. Run /setup_growth to try again.`

#### Step 5 of 7 — Metrics

> 🤖 **Embed: 📊 Step 5 of 7 — Metrics to Track** (blurple)
> *Define which columns the bot should snapshot each period. Add as many as
> you want — for example a `1st Squad Power` column, `THP`, `Total Kills`, etc.*
>
> *No metrics yet*
> Click **Add Metric** to begin.
>
> Footer (free): *Free tier: 0 of 5 metrics used. Upgrade to Premium for unlimited.*
>
> 🔘 row 0: `[➕ Add Metric]` (success — disabled at cap)
> `[✏️ Edit Metric]` (primary, disabled if no metrics)
> `[🗑️ Delete Metric]` (danger, disabled if no metrics)
> 🔘 row 1: `[✅ Done]` (secondary, disabled if no metrics)

`Add Metric` opens 📋 modal `Metric` with two fields:
- **Label** (placeholder `e.g. 1st Squad Power, THP, Total Kills`, max 100)
- **Column letter** (placeholder `e.g. E`, max 2)

`Edit Metric` and `Delete Metric` show a single-select to pick which one,
then either re-open the modal pre-filled or remove with `🗑️ Removed: **THP** (column I)`.

If a user finishes with zero metrics: `⚠️ No metrics defined. Run /setup_growth to try again.`

#### Step 6 of 7 — Growth Tracking Tab
Keep-or-change, default `Growth Tracking`. Tab is auto-created if missing.

#### Step 7 of 7 — Snapshot Frequency
> 🤖 **Step 7 of 7 — Snapshot Frequency**
> How often should the bot take a snapshot?
> *🔒 Custom interval is a Premium feature.*  *(only on free tier)*
>
> 🔘 `[📅 Monthly (1st of each month)]` (primary)
> 🔘 `[🔁 Custom interval (every X days) 💎]` (secondary — disabled on free tier)

##### Step 7a — Snapshot Day (monthly)
Keep-or-change, default `1`, range 1–28. Anything bad falls back to default.

##### Step 7a — Interval (custom, premium-only)
Keep-or-change, default `30` days, min 1.

#### Final summary

> 🤖 **Embed: ✅ Growth Tracking Configured** (green)
> - Source Tab / Name Column / Data Start Row / Growth Tab / Snapshot Schedule
> - Metrics: bullet list with column letters
> - Footer: *Run /setup_growth again to update. Use /growth to take a manual snapshot.*

---

### `/growth`

Show growth status with options to run a snapshot or edit config.
**Tier:** Both. **Permissions:** Leadership.

> 🤖 **Embed: 📈 Growth Tracking** (green if enabled, grey if disabled)
> - **Status:** ✅ Enabled / ❌ Disabled
> - **Source Tab:** Squad Powers
> - **Growth Tab:** Growth Tracking
> - **Snapshot:** Monthly on day 1   *(or "Every 30 days")*
> - **Metrics (3):**
>   - **1st Squad Power** — column C
>   - **THP** — column I
>   - **Total Kills** — column J
>
> 🔘 `[📸 Run Snapshot Now]` (success — disabled if not enabled)
> 🔘 `[⚙️ Edit Config]` (primary)

- **Run Snapshot Now** — disables both buttons, runs the snapshot synchronously, then ephemeral:
  - `✅ Growth snapshot complete — check the **Growth Tracking** tab.`
  - `⚠️ Growth snapshot failed: <error>`
- **Edit Config** — disables both buttons, ephemeral:
  - `Run /setup_growth to update the growth tracking configuration.`

---

## 8. Member Roster Sync 💎

### `/setup_members`

💎 Configure Member Roster Sync. **Tier:** Premium only. **Permissions:** Admin only.

If not premium:
> 🤖 **Embed: 🔒 Member Roster Sync is a Premium feature** (purple)
> *Member Roster Sync is part of Alliance Helper Premium. Run /upgrade to unlock it.*

Otherwise:
> 🤖 💎 **Member Roster Sync Setup**
> Configure how the bot writes your roster (Discord IDs + names) to a sheet
> tab. Other premium features look this up to send DMs and tag members.

#### Step 1 of 3 — Roster Tab
Keep-or-change, default `Member Roster`:
> ⚠️ *If the tab doesn't exist, the bot will create it automatically.*
> ⚠️ *The tab will be **completely overwritten** on each sync.*

#### Step 2 of 3 — Filter by Member Role?
> 🤖 **Step 2 of 3 — Filter by Member Role?**
> Should the roster only include members who have <@&MemberRole>?
> Pick **No** to include every (non-bot) member of the server.
>
> 🔘 `[Yes]`  🔘 `[No]`

#### Step 3 of 3 — Auto-Sync?
> 🤖 **Step 3 of 3 — Auto-Sync?**
> Should the bot automatically re-sync when members join, leave, or change roles?
> Pick **No** to only sync on `/sync_members`.

#### Initial sync + summary

> 🤖 **Embed: ✅ Member Roster Sync Configured** (gold)
> - Tab / Role Filter / Auto-Sync
> - Initial sync: **N** members written
> - Footer: *Run /sync_members to re-sync manually any time.*

If the initial sync fails:
`✅ Saved configuration but the initial sync failed: <error>\nTry running /sync_members once you've fixed the issue.`

---

### `/sync_members`

💎 Manually rebuild the member roster sheet now. **Tier:** Premium only.
**Permissions:** Admin only + roster configured.

If not premium:
> 🤖 **Embed: 🔒 Member Roster Sync is a Premium feature** (purple)
> *Member Roster Sync writes every member's Discord ID to your sheet so other
> Premium features (birthday DMs, train DMs, auto-mention, etc.) can find
> them. Run `/upgrade` to unlock it.*

If not yet configured: `⚙️ Member Roster Sync isn't configured yet. Run /setup_members first.`

On success: `✅ Synced **42** members to the **Member Roster** tab.`
On failure: `⚠️ Sync failed: <error>\nMake sure the bot has access to your sheet and that the **Member Roster** tab can be written to.`

---

## 9. Utilities

### `/cancel`

Cancel any active wizard or log session. **Tier:** Both. **Permissions:** Leadership.

- If a session is running: `❌ Session cancelled.`
- Otherwise: `ℹ️ You don't have an active session running.`

Cancellation is signalled to the train wizards, the storm-log flow, and the
generic wizard registry — whichever flow the user has open will see its
prompts deleted (if possible) and emit a `❌ Cancelled.` follow-up.

---

### `/donate`

Show donation links. **Tier:** Both. **Permissions:** None.

> 🤖 **Embed: 💖 Support Alliance Helper** (magenta)
> *If this bot has been useful to your alliance and you'd like to help cover
> hosting costs or just show appreciation, any support is hugely appreciated. Thank you!*
>
> **Ways to Donate**
> - ☕ **[Ko-fi](https://ko-fi.com/pinkcatboi)**
> - 🥤 **[Buy Me a Coffee](…)**  *(only if env var set)*
> - 💖 **[GitHub Sponsors](…)**  *(only if env var set)*
> - 🎁 **[Patreon](…)**  *(only if env var set)*
> - 💵 **[PayPal](…)**  *(only if env var set)*
>
> Footer: *100% optional — the bot is and will remain free to use at the base level.*

If no env vars set: `*(No donation links configured yet.)*`

---

### `/upgrade`

Unlock Alliance Helper Premium. **Tier:** Both. **Permissions:** None.

If already premium:
> 🤖 **Embed: 💎 Premium is active** (gold)
> *This server already has Alliance Helper Premium — you're set! All premium
> features are unlocked. Thanks for supporting the bot.*

Otherwise:
> 🤖 **Embed: 💎 Alliance Helper Premium** (purple)
> Unlock the full power of Alliance Helper for your alliance.
>
> **What you get:**
> - 📣 Unlimited events (vs 5 free)
> - 🚂 Up to 10 saved train prompt templates (vs 1 free)
> - ⚔️ Up to 10 saved storm mail templates per team (vs 1 free)
> - 📋 Multiple surveys + extra question types (numeric, multi-select, date)
> - 📊 Custom snapshot intervals + unlimited tracked metrics
> - 🧵 Use threads as destinations for any channel-pickable feature
> - 👥 Member roster sync, birthday DMs, train DMs, survey reminders
> - 📅 30-day history windows on `/events_log` and `/train_log`
> - 📜 Unlimited storm-log lookback
>
> **$4.99/month or $49/year**, billed by Discord. Cancel anytime.
>
> 🔘 *(Discord-native premium SKU button if `PREMIUM_SKU_ID` env var is set)*

If no SKU configured, the embed shows an extra field instead of the button:
> ⚠️ Subscriptions not yet available
> *Premium subscriptions aren't live yet. Check back soon, or use /donate to
> support the bot in the meantime.*

---

## 10. Cross-cutting UX patterns

### Free-tier limit-reached embed (orange)

Used by: events count cap, survey question cap, storm-log lookback, etc.

> 🤖 **Embed: 📊 Free tier limit reached** (orange)
> *You've used **5 of 5** events on the free tier. Upgrade to 💎 Alliance
> Helper Premium to unlock more.*
> *This limit applies to: <feature label>*
> Premium subscribers get expanded limits, plus features like member roster
> sync, birthday DMs, and thread destinations. Run `/upgrade` to subscribe.

### Premium-locked embed (purple)

Used by: every command or feature that's hard-gated behind premium
(`/setup_members`, `/sync_members`, `/survey_remind`, `/desertstorm_remind`,
`/canyonstorm_remind`).

> 🤖 **Embed: 🔒 <feature> is a Premium feature** (purple)
> *<custom description>*
> *(plus the Discord-native premium upgrade button when configured)*

### Upgrade nudge view

Whenever the bot rejects an action because of a free-tier limit, it pairs
the orange/purple embed with the same `upgrade_view()` button that
`/upgrade` uses (if `PREMIUM_SKU_ID` is set), giving the user a one-click
path to subscribe.

### Wizard timeouts

All `/setup_*` wizards use a 2-minute timeout per step (a few longer ones use
5 minutes for paste-heavy steps). On timeout the bot replies
`⏰ Timed out. Run /<command> to start again.` and aborts. The user can
re-run the command at any time.

### Channel/role guards

Three layers, in order:
1. `⚙️ This bot hasn't been set up yet. Run /setup to get started.`
2. `⛔ This command can only be used in the leadership channel.`
3. `⛔ You need the **<Leadership Role>** role to use this command.`

### Keep-or-change view

The recurring "default vs custom" pattern across every wizard:

> 🔘 `[✅ Use default: <value>]` (success — text trimmed to fit Discord's 80-char button cap)
> 🔘 `[✏️ Define my own]` (secondary)

Picking the second opens a 📋 modal pre-filled with the default. The result is
echoed in the original message: `✅ Using **<value>**`.

### Persistent survey button

The `📋 Answer` button on `/survey_post` uses `custom_id="survey_answer_button"`
and `timeout=None`, so it survives bot restarts. Re-registered in
`SurveyCog.__init__` via `bot.add_view(SurveyButtonView())`.

### Multi-template pickers (💎 Premium)

When premium and multiple templates exist, both the train blurb wizard and
the storm draft flows insert an extra picker step:

> 🤖 💎 You have multiple saved templates. Pick one for this draft:
>
> 🔘 *single select* — `Pick a saved template…`

(For train, the prompt reads "Pick one for this prompt".) On select:
`✅ Template: **<name>**`. Picker timeout cancels the in-progress draft.
