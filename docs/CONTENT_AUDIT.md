# LW Alliance Helper — Content Audit

A grep-friendly inventory of every user-facing string the bot emits, grouped by
feature. The point of this doc is to make it easy to:

- Audit voice and tone consistency across the app.
- Find a specific message without grepping the codebase.
- Track copy changes over time — when you update a button label, update the
  matching row here too.

The verbatim copy column preserves emoji, punctuation, ellipses, line breaks
(rendered as `\n`), and template placeholders (`{name}`, `{count}`, etc.). The
File column tells you which file to grep when you want to make an edit.

> **Excluded from the audit**: log lines (`[GROWTH] …`, `[STATS] …`),
> docstrings, code comments, dev-facing Python warnings. User-customizable
> templates (mail bodies, blurb templates, custom survey questions) are
> excluded — only the bot-generated default scaffolding is shown.

---

## Conventions

### Emoji palette

| Emoji | Meaning |
|---|---|
| ✅ | Success / completion / "use as-is" affirmative |
| ❌ | Cancel / destructive / "no" answer |
| ⚠️ | Validation warning, recoverable error |
| ⛔ | Permission denied / access blocked |
| ⚙️ | Config / setup-related notice |
| ⏰ | Wizard timeout |
| ⏳ | In-progress / loading state |
| 💾 | Saved (but not yet posted) |
| 💎 | Premium-only feature or gate |
| 🔒 | Premium-locked dialog |
| ℹ️ | Informational hint |
| 🚀 | Member-facing welcome moment |
| 🎂 | Birthday |
| 📣 | Events |
| 🚂 | Train |
| ⚔️ | Desert Storm |
| 🏜️ | Canyon Storm (CS-specific contexts) |
| 📋 | Survey / participation log |
| 📈 / 📊 | Growth tracking / metrics |
| 📤 / 📨 / 📢 | Send / DM / channel post (in reminder UI) |
| 👋 | Welcome DM greeting |
| 🐛 | Bug report link |

### Tone

- **Second-person**, imperative for actions (`Run /setup`), declarative for
  status (`Setup complete!`).
- **Slash commands** are always rendered in inline code (`` `/setup` ``).
- **Sheet tab names**, **role names**, **channel names** — bolded.
- **Modals** open from buttons; their submit-button label is Discord's default
  ("Submit") and not customized anywhere.
- Wizard prompts use `**Step N of M — Title**\n…`. The `Step N of M` count is
  authoritative — substeps use `Step Na of M` (e.g. `Step 7a of 7`).

### Type-column labels used in this doc

`Description`, `Param desc`, `Embed title`, `Embed desc`, `Embed field`,
`Embed footer`, `Button`, `Modal title`, `Input label`, `Input placeholder`,
`Select placeholder`, `Select option`, `Wizard prompt`, `Success`, `Error`,
`Warning`, `Cancel`, `Timeout`, `Premium gate`, `DM`, `Channel post`,
`Default template`, `Validation retry`, `Help section`, `Help line`,
`Presence`, `Note`, `Info`, `Status`, `Sheet header`. Button styles are
in parens after the label (`(primary)`, `(success)`, `(danger)`,
`(secondary)`, `(link)`).

---

## 1. Welcome and Onboarding

### 1.1 Welcome DM (sent on guild join)

| Type | Copy | File |
|---|---|---|
| DM | (see "Welcome DM" block below) | `bot.py` |

```text
👋 Thanks for adding **LW Alliance Helper** to **{guild_name}**!

To get started, run **/setup** in your server's leadership channel. The wizard walks you through:
• Member and leadership roles
• The leadership channel
• Your alliance's timezone
• Sharing your Google Sheet with the bot

After setup, run **/help** to see every available feature.

📖 Setup guide: <https://lw-alliance-helper.github.io/setup.html>
📋 All commands: <https://lw-alliance-helper.github.io/commands.html>
💎 Pricing & Premium: <https://lw-alliance-helper.github.io/pricing.html>

🐛 Need help or found a bug? Open an issue at:
<https://github.com/LW-Alliance-Helper/lw-alliance-helper.github.io/issues>
```

### 1.2 Bot presence (Discord status)

| Type | Copy | File |
|---|---|---|
| Presence | `Helping {count} LW Alliance` *(singular when count == 1)* | `bot.py` |
| Presence | `Helping {count} LW Alliances` *(plural otherwise)* | `bot.py` |

---

## 2. Permission Guards

### 2.1 Generic leadership guards (bot.py)

| Type | Copy | File |
|---|---|---|
| Error | `⚙️ This bot hasn't been set up yet. Run \`/setup\` to get started.` | `bot.py` |
| Error | `⛔ This command can only be used in the leadership channel.` | `bot.py` |
| Error | `⛔ You need the **{cfg.leadership_role_name}** role to use this command.` | `bot.py` |

The same three guard messages also appear in `train.py`, `storm.py`,
`storm_log.py`, `survey.py` (and other feature files) — copies of the
boilerplate, not imports. If you change the wording, search the whole
codebase, not just `bot.py`.

### 2.2 Setup-cog admin guards (setup_cog.py)

| Type | Copy | File |
|---|---|---|
| Error | `⛔ Only server administrators can run \`/setup\`.` | `setup_cog.py` |
| Error | `⛔ Only server administrators can view configuration.` | `setup_cog.py` |
| Error | `⚙️ This server hasn't been set up yet. Run \`/setup\` to get started.` | `setup_cog.py` |
| Error | `⛔ Only server administrators can reset the configuration.` | `setup_cog.py` |
| Error | `⛔ You need the leadership role (or admin) to run \`/setup_train\`.` | `setup_cog.py` |
| Error | `⛔ You need the leadership role (or admin) to run \`/setup_growth\`.` | `setup_cog.py` |
| Error | `⛔ You need the leadership role (or admin) to run \`/setup_birthdays\`.` | `setup_cog.py` |
| Error | `⛔ You need the leadership role (or admin) to run \`/setup_desertstorm\`.` | `setup_cog.py` |
| Error | `⛔ You need the leadership role (or admin) to run \`/setup_canyonstorm\`.` | `setup_cog.py` |
| Error | `⛔ You need the leadership role (or admin) to run \`/setup_events\`.` | `setup_cog.py` |
| Error | `⛔ You need the leadership role (or admin) to run \`/setup_survey\`.` | `setup_cog.py` |
| Error | `⛔ You need the leadership role (or admin) to sync the member roster.` | `member_roster.py` |
| Error | `⛔ You need the leadership role (or admin) to configure the member roster.` | `member_roster.py` |

---

## 3. `/help`

| Type | Copy | File |
|---|---|---|
| Description | `Show all available bot commands` | `bot.py` |
| Embed title | `🤖 Alliance Helper — Commands  ·  💎 Premium` *(when active)* | `bot.py` |
| Embed title | `🤖 Alliance Helper — Commands  ·  Free tier` *(otherwise)* | `bot.py` |
| Embed desc | (see "Help intro" below) | `bot.py` |
| Embed footer | `💎 Premium is active. Thanks for supporting LW Alliance Helper!` | `bot.py` |
| Embed footer | `Alliance Helper — Run /upgrade to unlock Premium features` | `bot.py` |

```text
[Help intro]
All commands require the configured leadership role and must be used in the leadership channel.
Run `/setup` first if you haven't configured the bot yet.
```

### 3.1 Help section bodies

Each section is a separate field in the embed. Section title and body for each:

```text
[Core Setup]
⚙️ Core Setup
Configure the bot for your server. Start here before using any other features.
`/setup` — Configure roles, leadership channel, timezone, and Google Sheet
`/view_configuration` — View all configured settings across every wizard
`/setup_reset` — Clear server configuration and start over
```

```text
[Event Announcements]
📣 Event Announcements
Automate event scheduling for in-game events such as Plague Marauder and Zombie Siege. Drafts are posted to a leadership channel for review before being sent to the public announcement channel — both channels are configured during `/setup_events`.
`/setup_events` — Configure events, announcement channels, draft time, and 5-min warning
`/events [date]` — Open the event editor for today or a specific date
`/events_log` — Show approved event posts (7d free / 30d premium)
```

```text
[Train Schedule]
🚂 Train Schedule
Track who is assigned the alliance train each day and optionally generate a personalised ChatGPT prompt to write a blurb for that member's announcement.
`/setup_train` — Configure the train tab, blurb generation, and reminders
`/train` — View the schedule with Add / Update / Generate Prompt / Clear buttons
`/train_log [date]` — Show recent prompt log entries (7d free / 30d premium)
`/train_addbirthdays` — Manually run the birthday check now
```

```text
[Birthdays]
🎂 Birthdays
Track member birthdays from your Google Sheet and optionally post announcements in Discord and assign members to the train schedule on their birthday.
`/setup_birthdays` — Configure birthday tracking, train integration, and announcements
`/birthdays` — Show upcoming birthdays within your configured lookahead window (defaults to 14 days)
```

```text
[Desert Storm]
⚔️ Desert Storm
Generate weekly Desert Storm team mail drafts and log participation each event. Setup Step 6 lets you turn on participation tracking and define exactly what you want to log — vote count, sit-outs, custom questions — using free types (text, yes/no, numeric, roster names) or 💎 Premium types (single-select, multi-select, date).
`/setup_desertstorm` — Configure teams, log channel, post channel, mail template, participation
`/desertstorm overview` — Show current rosters and the active mail template
`/desertstorm draft` — Walk through team → time → template, then preview & post the mail
`/desertstorm participation` — Run the configurable participation log for this week
`/desertstorm log [date]` — View a Desert Storm log entry (free: 4 most recent / premium: all)
`/desertstorm remind` — 💎 DM every roster member to participate in this week's DS
```

```text
[Canyon Storm]
🏜️ Canyon Storm
Generate weekly Canyon Storm team mail drafts and log participation each event. Same flow as Desert Storm — preview in leadership, post to a public channel, plus configurable participation tracking on Setup Step 6.
`/setup_canyonstorm` — Configure teams, log channel, post channel, mail template, participation
`/canyonstorm overview` — Show current rosters and the active mail template
`/canyonstorm draft` — Walk through team → time → template, then preview & post the mail
`/canyonstorm participation` — Run the configurable participation log for this week
`/canyonstorm log [date]` — View a Canyon Storm log entry (free: 4 most recent / premium: all)
`/canyonstorm remind` — 💎 DM every roster member to participate in this week's CS
```

```text
[Survey]
📋 Survey
Collect member statistics through a private Discord thread survey. Each member clicks the survey button, gets walked through your configured questions in their own thread, and their answers land in your Google Sheet automatically. Leadership sees a notification embed in the configured notify channel for every submission.
`/setup_survey` — Configure the default survey (questions, channels, sheet tabs, intro)
`/survey` — View configured survey(s). 💎 Premium gets **Add / Edit / Remove** buttons here for managing multiple surveys.
`/survey_post` — Post (or repost) the answer button (Premium picks which survey)
`/survey_remind` — Send now or set up scheduled reminders. Free tier posts to a channel; 💎 Premium adds DM-via-roster delivery.
```

```text
[Growth Tracking]
📈 Growth Tracking
Take periodic snapshots of your members' stats to track alliance growth over time. You define which metrics to track and how often — snapshots are saved to your Google Sheet.
`/setup_growth` — Configure source tab, metrics to track, and snapshot schedule
`/growth` — Show growth status with options to run a snapshot or edit config
```

```text
[Premium Features]
💎 Premium Features
Unlock with `/upgrade`. Premium adds member-aware features that build on top of the free tier:
`/setup_members` — Configure the Member Roster Sync (writes Discord IDs to your sheet so other features can find members by name)
`/sync_members` — Manually re-sync the member roster now
Multiple named surveys — manage from `/survey` directly via Add / Edit / Remove buttons
`/survey_remind` — Send DM reminders via Member Roster, or schedule recurring DM reminders per survey
`/desertstorm remind` — DM every roster member about this week's DS
`/canyonstorm remind` — DM every roster member about this week's CS
*Plus: personal birthday DMs, train-assignment DMs, auto-mention members in train reminders, use threads as destinations, multi-template train and storm support, advanced survey/participation question types (single-select, multi-select, date), and more.*
```

```text
[Utilities]
🔧 Utilities
`/cancel` — Cancel any active wizard or log session and reset wizard state
`/help` — Show this command list (always available)
`/donate` — 💖 Show optional tip-jar links to support the bot's hosting
`/upgrade` — 💎 Subscribe to Premium for this server (Discord App Subscription)
```

---

## 4. Core Setup

### 4.1 `/setup` — command + entry

| Type | Copy | File |
|---|---|---|
| Description | `Configure Alliance Helper for your server` | `setup_cog.py` |
| Success | `⚙️ Starting setup — check the channel for prompts!` | `setup_cog.py` |

### 4.2 Already-configured prompt (re-run)

| Type | Copy | File |
|---|---|---|
| Embed title | `⚙️ Current Core Setup` | `setup_cog.py` |
| Embed desc | `Your server is already configured. Would you like to edit these settings?` | `setup_cog.py` |
| Embed field | `Member Role` → `{member_role_name}` | `setup_cog.py` |
| Embed field | `Leadership Role` → `{leadership_role_name}` | `setup_cog.py` |
| Embed field | `Leadership Channel` → `<#{id}>` | `setup_cog.py` |
| Embed field | `Timezone` → `{tz_label}` | `setup_cog.py` |
| Embed field | `Sheet ID` → `` `{spreadsheet_id[:20]}...` `` or `Not set` | `setup_cog.py` |
| Button | `✏️ Edit settings` (primary) | `setup_cog.py` |
| Button | `✅ No changes needed` (secondary) | `setup_cog.py` |
| Cancel | `✅ No changes made. Your existing setup is still active.` | `setup_cog.py` |

### 4.3 Wizard intro

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `⚙️ **Alliance Helper Setup**\n\nI'll walk you through the core configuration for your server. This covers your roles, leadership channel, timezone and Google Sheet.\n\n*You can run \`/setup\` again at any time to update these settings.*` | `setup_cog.py` |

### 4.4 Step 1 — Member Role

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 1 of 6 — Member Role**\nSelect the role that all alliance members have:` | `setup_cog.py` |
| Select placeholder | `Select member role...` | `setup_cog.py` |
| Button | `➕ Create a new role` (secondary) | `setup_cog.py` |
| Modal title | `Create a New Role` | `setup_cog.py` |
| Input label | `Role name` | `setup_cog.py` |
| Input placeholder | `e.g. Member, Alliance Member, Leadership` | `setup_cog.py` |
| Success | `✅ Selected: **{role.name}**` | `setup_cog.py` |
| Success | `✅ Created and selected new role: **{role.name}**` | `setup_cog.py` |
| Warning | `⚠️ I don't have permission to create roles. Please create the role manually first, then run \`/setup\` again.` | `setup_cog.py` |
| Warning | `⚠️ Could not create role: {e}` | `setup_cog.py` |
| Timeout | `⏰ Setup timed out. Run \`/setup\` to start again.` | `setup_cog.py` |

### 4.5 Step 2 — Leadership Role

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 2 of 6 — Leadership Role**\nSelect the elevated role for alliance leadership:` | `setup_cog.py` |
| Select placeholder | `Select leadership role...` | `setup_cog.py` |

*Same Create-a-Role / timeout copy as 4.4.*

### 4.6 Step 3 — Leadership Channel

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 3 of 6 — Leadership Channel**\nSelect the private channel where leadership commands will be used:` | `setup_cog.py` |
| Select placeholder | `Select leadership channel...` | `setup_cog.py` |
| Button | `➕ Create a new channel` (secondary) | `setup_cog.py` |
| Modal title | `Create a New Channel` | `setup_cog.py` |
| Input label | `Channel name` | `setup_cog.py` |
| Input placeholder | `e.g. announcements` *(or suggested name)* | `setup_cog.py` |
| Success | `✅ Selected: **{channel.name}**` | `setup_cog.py` |
| Success | `✅ Created and selected: **#{channel.name}**` | `setup_cog.py` |
| Warning | `⚠️ I don't have permission to create channels. Please create it manually first, then run \`/setup\` again.` | `setup_cog.py` |
| Warning | `⚠️ Could not create channel: {e}` | `setup_cog.py` |

### 4.7 Step 4 — Timezone

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 4 of 6 — Timezone**\nSelect your alliance's timezone. This is used for displaying event times, Desert Storm/Canyon Storm times, and train reminders throughout the bot:` | `setup_cog.py` |
| Select placeholder | `Select your timezone...` | `setup_cog.py` |
| Select option | `(UTC-10) Hawaii (Honolulu)` | `setup_cog.py` |
| Select option | `(UTC-9) Alaska (Anchorage)` | `setup_cog.py` |
| Select option | `(UTC-8) Pacific (Los Angeles, Seattle, Vancouver)` | `setup_cog.py` |
| Select option | `(UTC-7) Mountain (Denver, Phoenix, Calgary)` | `setup_cog.py` |
| Select option | `(UTC-6) Central (Chicago, Dallas, Mexico City)` | `setup_cog.py` |
| Select option | `(UTC-5) Eastern (New York, Toronto, Miami)` | `setup_cog.py` |
| Select option | `(UTC-3) Brazil (São Paulo, Rio de Janeiro)` | `setup_cog.py` |
| Select option | `(UTC-3) Argentina (Buenos Aires)` | `setup_cog.py` |
| Select option | `(UTC-1) Azores` | `setup_cog.py` |
| Select option | `(UTC+0) GMT/BST (London, Dublin, Lisbon)` | `setup_cog.py` |
| Select option | `(UTC+1) Central European (Paris, Berlin, Rome)` | `setup_cog.py` |
| Select option | `(UTC+2) Eastern European (Helsinki, Athens, Cairo)` | `setup_cog.py` |
| Select option | `(UTC+3) Moscow (Moscow, Istanbul, Riyadh)` | `setup_cog.py` |
| Select option | `(UTC+4) Gulf (Dubai, Abu Dhabi)` | `setup_cog.py` |
| Select option | `(UTC+5) Pakistan (Karachi, Islamabad)` | `setup_cog.py` |
| Select option | `(UTC+5:30) India (Mumbai, Delhi, Bangalore)` | `setup_cog.py` |
| Select option | `(UTC+6) Bangladesh (Dhaka)` | `setup_cog.py` |
| Select option | `(UTC+7) Indochina (Bangkok, Jakarta, Hanoi)` | `setup_cog.py` |
| Select option | `(UTC+8) China/Singapore (Shanghai, Beijing, Singapore)` | `setup_cog.py` |
| Select option | `(UTC+9) Japan/Korea (Tokyo, Seoul)` | `setup_cog.py` |
| Select option | `(UTC+10) Eastern Australia (Sydney, Melbourne)` | `setup_cog.py` |
| Select option | `(UTC+12) New Zealand (Auckland, Wellington)` | `setup_cog.py` |
| Success | `✅ Timezone: **{label}**` | `setup_cog.py` |

### 4.8 Step 5 — Google Sheet ID

| Type | Copy | File |
|---|---|---|
| Wizard prompt | ``**Step 5 of 6 — Google Sheet ID**\nEnter your Google Sheet ID — the long string from your sheet's URL:\n`https://docs.google.com/spreadsheets/d/`**`YOUR_SHEET_ID`**`/edit` `` | `setup_cog.py` |
| Modal title | `Google Sheet ID` | `setup_cog.py` |
| Input label | `Sheet ID` | `setup_cog.py` |
| Input placeholder | `Paste your Sheet ID here...` | `setup_cog.py` |
| Button | `✏️ Enter Value` (primary) | `setup_cog.py` |
| Success | `✅ Entered: **{value}**` | `setup_cog.py` |

### 4.9 Step 6 — Share Sheet

| Type | Copy | File |
|---|---|---|
| Embed title | `**Step 6 of 6 — Share Your Google Sheet**` | `setup_cog.py` |
| Embed desc | `Before finishing, you need to give the bot access to your sheet.\n\n**Follow these steps:**\n1️⃣ Click the link below to open your sheet's sharing settings\n2️⃣ Click **Share** in the top right corner\n3️⃣ Paste the email address below into the share field\n4️⃣ Set permission to **Editor**\n5️⃣ Click **Send** — then come back here and confirm` | `setup_cog.py` |
| Embed field | `📋 Service Account Email (click to copy)` → `` `sheet-connector@lw-alliance-helper.iam.gserviceaccount.com` `` | `setup_cog.py` |
| Embed field | `🔗 Open Your Sheet` → `[Click here to open sharing settings]({sharing_url})` | `setup_cog.py` |
| Button | `✅ I've shared the sheet` (success) | `setup_cog.py` |
| Button | `❌ Cancel setup` (danger) | `setup_cog.py` |
| Cancel | `❌ Setup cancelled. Run \`/setup\` to start again.` | `setup_cog.py` |

### 4.10 Final review and completion

| Type | Copy | File |
|---|---|---|
| Embed title | `✅ Final Review — Confirm to Save` | `setup_cog.py` |
| Embed desc | `All steps complete. Review your selections below and click **Confirm** to save your configuration, or **Cancel** to start over.\n*(This is the final review, not an additional step.)*` | `setup_cog.py` |
| Embed field | `Member Role` / `Leadership Role` / `Leadership Channel` / `Timezone` / `Sheet ID` | `setup_cog.py` |
| Button | `✅ Confirm` (success) | `setup_cog.py` |
| Button | `❌ Cancel` (danger) | `setup_cog.py` |
| Success | (see "Core setup complete" block below) | `setup_cog.py` |

```text
[Core setup complete]
✅ **Core setup complete!**

Now configure the features you want to use. Run each of the commands below for any feature you'd like to enable:

📣 `/setup_events` — Event announcements (Plague Marauder, Zombie Siege, etc.)
🚂 `/setup_train` — Train schedule, blurb generation, and reminders
🎂 `/setup_birthdays` — Birthday tracking and announcements
⚔️ `/setup_desertstorm` — Desert Storm mail drafts and participation logs
🏜️ `/setup_canyonstorm` — Canyon Storm mail drafts and participation logs
📋 `/setup_survey` — Squad powers survey
📈 `/setup_growth` — Growth tracking (snapshot your members' stats over time)

You can set up as many or as few of these as you need. Use `/help` at any time to see all available commands.
```

### 4.11 `/view_configuration`

| Type | Copy | File |
|---|---|---|
| Description | `View all configured settings across every setup wizard` | `setup_cog.py` |
| Embed title | `⚙️ Current Configuration  ·  💎 Premium` *(when active)* | `setup_cog.py` |
| Embed title | `⚙️ Current Configuration  ·  Free tier` *(otherwise)* | `setup_cog.py` |
| Embed desc | `All configured settings across the bot's setup wizards.` | `setup_cog.py` |
| Embed field | `🛠️ Core` *(Tier, Member Role, Leadership Role, Leadership Channel, Announcement Channel, Timezone, Spreadsheet ID, Member Tab)* | `setup_cog.py` |
| Embed field | `📣 Events` *(Draft Channel, Announcement Channel, Draft Time, 5-Min Warning, Events list)* | `setup_cog.py` |
| Embed field | `🚂 Train` *(Schedule Tab, Blurbs, Themes, Tones, Default Tone, Prompt Template, Reminders, etc.)* | `setup_cog.py` |
| Embed field | `🎂 Birthdays` | `setup_cog.py` |
| Embed field | `⚔️ Desert Storm` | `setup_cog.py` |
| Embed field | `🏜️ Canyon Storm` | `setup_cog.py` |
| Embed field | `📋 Survey` | `setup_cog.py` |
| Embed field | `📈 Growth` | `setup_cog.py` |
| Embed footer | `💎 Premium is active. Run any /setup_* command to update a section.` | `setup_cog.py` |
| Embed footer | `Run /upgrade for Premium • /help for all commands • /setup_* to update a section` | `setup_cog.py` |
| Helper text | `✅ Configured` / `❌ Not configured` | `setup_cog.py` |
| Helper text | `✅ Enabled` / `❌ Disabled` | `setup_cog.py` |
| Helper text | `*not set*` / `*none configured*` / `*none*` | `setup_cog.py` |

### 4.12 `/setup_reset`

| Type | Copy | File |
|---|---|---|
| Description | `Clear this server's configuration and start over` | `setup_cog.py` |
| Warning | `⚠️ Are you sure you want to reset the bot configuration for this server? This cannot be undone.` | `setup_cog.py` |
| Button | `Yes, reset everything` (danger) | `setup_cog.py` |
| Button | `Cancel` (secondary) | `setup_cog.py` |
| Success | `✅ Configuration reset. Run \`/setup\` to configure the bot again.` | `setup_cog.py` |
| Cancel | `✅ Reset cancelled. Your configuration is still active and has not been reset.` | `setup_cog.py` |

---

## 5. Events

### 5.1 `/setup_events`

#### 5.1.1 Command + entry

| Type | Copy | File |
|---|---|---|
| Description | `Add or edit an event type for announcements (Marauder, Siege, etc.)` | `setup_cog.py` |
| Success | `⚙️ Starting event setup — check the channel for prompts!` | `setup_cog.py` |
| Wizard prompt | `⚙️ **Event Setup**\nConfigure your alliance events. All events share the same draft channel, announcement channel, draft time, and 5-minute warning setting.` | `setup_cog.py` |

#### 5.1.2 Already-configured action menu

| Type | Copy | File |
|---|---|---|
| Embed title | `📣 Event Setup` | `setup_cog.py` |
| Embed desc | `Your events are already configured. What would you like to do?` | `setup_cog.py` |
| Embed field | `Draft Channel` / `Announcement Channel` / `Draft Time` / `5-min Warning` / `Events` | `setup_cog.py` |
| Button | `⚙️ Edit Event Settings` (primary) | `setup_cog.py` |
| Button | `➕ Add Event` (success) | `setup_cog.py` |
| Button | `✏️ Edit Event` (secondary) | `setup_cog.py` |
| Button | `🗑️ Delete Event` (danger) | `setup_cog.py` |
| Button | `✅ No changes needed` (secondary) | `setup_cog.py` |
| Success | `✅ No changes made.` | `setup_cog.py` |
| Info | `⚙️ Let's update your event settings...` | `setup_cog.py` |

#### 5.1.3 Steps 1–4 (channels and timing)

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 1 of 5 — Draft Channel**\nWhich channel should the bot post event announcement drafts for leadership to review?\n*(This applies to all events)*` | `setup_cog.py` |
| Select placeholder | `Select the draft channel...` | `setup_cog.py` |
| Wizard prompt | `**Step 2 of 5 — Announcement Channel**\nWhich channel should approved announcements be posted to?\n*(This applies to all events)*` | `setup_cog.py` |
| Select placeholder | `Select the announcement channel...` | `setup_cog.py` |
| Wizard prompt | `**Step 3 of 5 — Draft Posting Time**\nWhat time should the bot post the draft each event day? *(in {tz_label})*\n*(e.g. \`12:00pm\` for noon)*` | `setup_cog.py` |
| Modal title | `Draft Posting Time` | `setup_cog.py` |
| Warning | `⚠️ Could not read that time after a few tries. Run \`/setup_events\` to start over.` | `setup_cog.py` |
| Warning | `⚠️ Could not read **\`{time_raw}\`** as a time. Try \`12:00pm\`, \`9:00am\`, or \`15:30\`. Let's try once more.` | `setup_cog.py` |
| Wizard prompt | `**Step 4 of 5 — 5-Minute Warning**\nShould the bot automatically post a 5-minute warning before events?\n*(This applies to all events)*` | `setup_cog.py` |

#### 5.1.4 Step 5 — Event List

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 5 of 5 — Your Events:**\n{event_display}` | `setup_cog.py` |
| Select placeholder | `✏️ Edit an event...` | `setup_cog.py` |
| Select placeholder | `🗑️ Delete an event...` | `setup_cog.py` |
| Button | `➕ Add Event` (primary) | `setup_cog.py` |
| Button | `✅ Finish` (success) | `setup_cog.py` |
| Success | `🗑️ Removed: **{name}**` | `setup_cog.py` |

#### 5.1.5 Per-event builder

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Event Name**\nWhat is this event called? (e.g. \`Plague Marauder (AE)\`, \`Zombie Siege\`)` | `setup_cog.py` |
| Wizard prompt | `**{name} — Event Time**\nWhat time does this event usually start? *(in {tz_label})*\n*(e.g. \`10:15pm\`, \`9:00am\`)*` | `setup_cog.py` |
| Modal title | `Event Time` | `setup_cog.py` |
| Warning | `⚠️ Could not read **\`{time_raw}\`** as a time. Try \`10:15pm\`, \`9:00am\`, or \`21:00\`. Let's try once more.` | `setup_cog.py` |
| Wizard prompt | `**{name} — Schedule**\nDoes this event repeat on a fixed cycle, or do you add it manually each time?` | `setup_cog.py` |
| Button | `🔁 Repeating cycle` (primary) | `setup_cog.py` |
| Button | `📅 Add manually each time` (secondary) | `setup_cog.py` |
| Success | `✅ Schedule: **Repeating cycle**` | `setup_cog.py` |
| Success | `✅ Schedule: **Manual (add per event)**` | `setup_cog.py` |
| Wizard prompt | `**{name} — Anchor Date**\nEnter a recent or upcoming date when this event occurs.\nType the month and day (e.g. \`March 30\`, \`April 14\`)` | `setup_cog.py` |
| Warning | `⚠️ Could not read that date. Try \`March 30\`. Run \`/setup_events\` to try again.` | `setup_cog.py` |
| Wizard prompt | `**{name} — Cycle Interval**\nHow many days between each occurrence? (e.g. \`3\`)` | `setup_cog.py` |
| Modal title | `Cycle Interval` | `setup_cog.py` |
| Input label | `Days between occurrences` | `setup_cog.py` |
| Warning | `⚠️ Please enter a whole number. Run \`/setup_events\` to try again.` | `setup_cog.py` |
| Wizard prompt | `**{name} — Announcement Blurb**\nThis message gets posted when this event fires.\nUse \`{time}\` for the event time in your timezone and \`{server_time}\` for Server Time.\n\n**Default:** \`{name} at {time} ({server_time} Server Time).\`` | `setup_cog.py` |
| Button | `✅ Use default blurb` (success) | `setup_cog.py` |
| Button | `✏️ Enter my own` (secondary) | `setup_cog.py` |
| Button | `⏭️ Keep existing` (secondary) | `setup_cog.py` |
| Success | `✅ Using default blurb:\n\`{default_blurb}\`` | `setup_cog.py` |
| Success | `✅ Keeping existing blurb.` | `setup_cog.py` |
| Wizard prompt | `Enter your announcement blurb:\n*(Use \`{time}\` and \`{server_time}\` as placeholders)*` | `setup_cog.py` |
| Success | `✅ {Updated\|Added}: **{name}**` | `setup_cog.py` |

#### 5.1.6 Save summary

| Type | Copy | File |
|---|---|---|
| Embed title | `✅ Events Configured` | `setup_cog.py` |
| Embed field | `Draft Channel` / `Announcement Channel` / `Draft Time` / `5-min Warning` / `Events` | `setup_cog.py` |
| Embed footer | `Run /setup_events again to add or edit events.` | `setup_cog.py` |

### 5.2 `/events`

| Type | Copy | File |
|---|---|---|
| Description | `Open the event editor for today or a specific date` | `bot.py` |
| Param desc | `date` → `Optional date, e.g. 'April 5' or '4/5' (defaults to today)` | `bot.py` |
| Warning | `⚠️ Could not parse date \`{date}\`. Try formats like \`April 5\` or \`4/5\`.` | `bot.py` |
| Info | `ℹ️ **{target_date:%B} {target_date.day}** is not an event day. Showing the next event date: **{event_date:%A, %B} {event_date.day}**.` | `bot.py` |

### 5.3 Event Editor (posted by `/events` and by the daily scheduler)

| Type | Copy | File |
|---|---|---|
| Channel post | `📣 **Event Editor** — adjust today's event schedule, then build the announcement.\n\n**Current events:**\n{lines}\n\n**Announcement text:** *None*` | `scheduler.py` |
| Embed list line | `{i}. **{name}** — {t} ET ({sv} server)` | `scheduler.py` |
| Empty list placeholder | `*No events set*` | `scheduler.py` |
| Editor refresh content | `📣 **Event Editor** — adjust today's event schedule, then build the announcement.\n\n**Current events:**\n{format_event_list_text}\n\n**Announcement text:** {self.notes if self.notes else '*None*'}` | `scheduler.py` |
| Button | `➕ Add Event` (primary) | `scheduler.py` |
| Button | `✏️ Edit Time` (secondary) | `scheduler.py` |
| Button | `🗑️ Remove Event` (danger) | `scheduler.py` |
| Button | `📝 Add Announcement Text` (secondary) | `scheduler.py` |
| Button | `📣 Build Announcement` (success) | `scheduler.py` |
| Timeout (view expired, appended to original message) | `\n\n⏰ *The actions for this have timed out. Use \`/events\` to re-initiate.*` | `wizard_registry.py` |

### 5.4 Add Event sub-flow

| Type | Copy | File |
|---|---|---|
| Warning | `All available events are already in the list.` | `scheduler.py` |
| Select placeholder | `Choose an event to add...` | `scheduler.py` |
| Wizard prompt | `Select an event to add:` | `scheduler.py` |
| Wizard prompt | `⏰ What time is **{chosen_name}**? *(e.g. 10:30pm or 22:30)*` | `scheduler.py` |
| Success | `✅ **{chosen_name}** added at {format_et(dt)} ET.` | `scheduler.py` |
| Error | `⚠️ Could not parse that time. Try again with Add Event.` | `scheduler.py` |
| Timeout | `⏰ Timed out waiting for time input.` | `scheduler.py` |

### 5.5 Edit Time sub-flow

| Type | Copy | File |
|---|---|---|
| Warning | `No events to edit.` | `scheduler.py` |
| Select placeholder | `Choose an event to edit...` | `scheduler.py` |
| Select option label | `{name} — {format_et} ET` | `scheduler.py` |
| Wizard prompt | `Choose an event to edit:` | `scheduler.py` |
| Wizard prompt | `⏰ New time for **{lib_name}**? *(e.g. 10:30pm or 22:30)*` | `scheduler.py` |
| Success | `✅ **{lib_name}** updated to {format_et} ET.` | `scheduler.py` |
| Error | `⚠️ Could not parse that time.` | `scheduler.py` |
| Timeout | `⏰ Timed out.` | `scheduler.py` |

### 5.6 Remove Event sub-flow

| Type | Copy | File |
|---|---|---|
| Warning | `No events to remove.` | `scheduler.py` |
| Select placeholder | `Choose an event to remove...` | `scheduler.py` |
| Wizard prompt | `Choose an event to remove:` | `scheduler.py` |
| Success | `✅ **{lib_name}** removed.` | `scheduler.py` |

### 5.7 Add Announcement Text sub-flow

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `📝 {interaction.user.mention} — type the additional announcement text that should be appended to today's announcement, or type \`clear\` to remove existing text.{current_note}` | `scheduler.py` |
| Wizard prompt (appended when notes exist) | `\n\nCurrent announcement text:\n> {self.notes}` | `scheduler.py` |
| Success | `✅ Announcement text cleared.` | `scheduler.py` |
| Success | `✅ Announcement text saved.` | `scheduler.py` |
| Timeout | `⏰ Timed out.` | `scheduler.py` |

### 5.8 Build Announcement → Approval flow

| Type | Copy | File |
|---|---|---|
| Warning | `⚠️ No events in the list. Use \`/events\` to open a fresh editor.` | `scheduler.py` |
| Error | `⚠️ Error building announcement: {e}` | `scheduler.py` |
| Channel post (leadership) | `📣 **Announcement draft — please review and approve:**\n\n{announcement}` | `scheduler.py` |
| Error | `⚠️ Could not find the leadership channel.` | `scheduler.py` |
| Button | `✅ Send As-Is` (success) | `scheduler.py` |
| Button | `✏️ Edit & Send` (primary) | `scheduler.py` |
| Channel post (leadership stamp) | `✅ **Approved by {interaction.user.display_name} at {_ts}**\n\`\`\`\n{self.draft_message}\n\`\`\`` | `scheduler.py` |
| Wizard prompt | `✏️ {interaction.user.mention} — copy and edit the message below, then send your revised version:\n\n\`\`\`\n{self.draft_message}\n\`\`\`` | `scheduler.py` |
| Channel post (revised draft) | `📝 **Revised draft** (edited by {interaction.user.display_name}):\n\n{revised_text}` | `scheduler.py` |
| Timeout (Edit & Send wait_for) | `⏰ Edit timed out — no message received from {interaction.user.mention} within 5 minutes.` | `scheduler.py` |
| Timeout (approval view expired, appended to draft message) | `\n\n⏰ *The actions for this have timed out. Use \`/events\` to re-initiate.*` | `wizard_registry.py` |

### 5.9 Default templates and built-in event blurbs

| Type | Copy | File |
|---|---|---|
| Channel post (announcements) | (see "Default event announcement" block below) | `scheduler.py` |
| Default template (Plague Marauder name) | `Plague Marauder` | `scheduler.py` |
| Default template (Marauder blurb) | `Marauder (AE) at {time} ({server} server). Make sure to have offline participation checked!` | `scheduler.py` |
| Default template (Zombie Siege name) | `Zombie Siege` | `scheduler.py` |
| Default template (Siege blurb) | `Zombies at {time} ({server} server). Be sure you have squads on your wall!` | `scheduler.py` |
| Default template (generic blurb fallback) | `{key} at {time} ({server_time} Server Time).` | `scheduler.py` |

```text
[Default event announcement]
Hey {role_mention}!
Here is the schedule for events today:

- {blurb formatted with {time}, {server_time}, {server}}
- {blurb formatted with {time}, {server_time}, {server}}

{notes if any}
```

### 5.10 5-minute warning (auto-fired)

| Type | Copy | File |
|---|---|---|
| Channel post (announcements, no events fallback) | `Event starting in 5 minutes! Make sure you're online!` | `scheduler.py` |
| Channel post (announcements, marauder special) | `Marauder (AE) in 5 minutes! Make sure you hop online and get your points! Zombies right after, check your wall to make sure you have squads on it!` | `scheduler.py` |
| Channel post (announcements, generic) | `{name} in 5 minutes! Make sure you're online!` | `scheduler.py` |
| Channel post (leadership stamp) | `⏱️ **5-minute warning auto-posted** at {_ts}` | `scheduler.py` |

### 5.11 `/events_log`

| Type | Copy | File |
|---|---|---|
| Description | `Show recent approved event posts (window depends on your tier)` | `bot.py` |
| Warning | `⚠️ Leadership channel isn't configured. Run \`/setup\` to configure it.` | `bot.py` |
| Warning | `⚠️ Could not access the leadership channel.` | `bot.py` |
| Warning | `⚠️ Bot does not have permission to read message history in the leadership channel.` | `bot.py` |
| Embed title | `📣 Events Log — Past {days} Days` | `bot.py` |
| Embed desc | `*Showing approved event posts from the past {days} days.*` | `bot.py` |
| Embed field | `No approvals found` → `*No event posts have been approved in the past {days} days.*` | `bot.py` |
| Embed field | `Approvals ({len(matches)})` → `• {header} *— logged {local_dt}*` *(per match)* | `bot.py` |
| Embed footer | `Free tier: 7-day window. Upgrade to Premium for 30 days.` | `bot.py` |

---

## 6. Train

### 6.1 `/setup_train`

#### 6.1.1 Command + entry

| Type | Copy | File |
|---|---|---|
| Description | `Configure the train schedule — tab, themes, tones, and prompt template` | `setup_cog.py` |
| Success | `⚙️ Starting train setup — check the channel for prompts!` | `setup_cog.py` |
| Wizard prompt | `⚙️ **Train Schedule Setup**\n*Configure how the train schedule works for your alliance.*` | `setup_cog.py` |

#### 6.1.2 Step 1 — Schedule Sheet Tab

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 1 of 7 — Schedule Sheet Tab**\nWhich tab in your Google Sheet stores the train schedule?\n⚠️ *Make sure this tab exists in your sheet before continuing.*` | `setup_cog.py` |
| Modal title | `Sheet Tab Name` | `setup_cog.py` |
| Input label | `Tab name` | `setup_cog.py` |
| Button (no saved value, or saved == default) | `✅ Use default: {default}` (success) | `setup_cog.py` |
| Button (saved differs — appears alongside the two below) | `✅ Keep current: {current}` (success) | `setup_cog.py` |
| Button (saved differs — appears alongside the two above) | `↩️ Use default: {default}` (secondary) | `setup_cog.py` |
| Button | `✏️ Define my own` (secondary) | `setup_cog.py` |
| Success — kept default | `✅ Using **{default}**` | `setup_cog.py` |
| Success — kept current | `✅ Using **{current}**` | `setup_cog.py` |
| Success — reverted to default from saved | `✅ Reverted to default: **{default}**` | `setup_cog.py` |
| Success — defined own | `✅ Using **{value}**` | `setup_cog.py` |
| Timeout | `⏰ Timed out. Run \`/setup_train\` to start again.` | `setup_cog.py` |

#### 6.1.3 Step 2 — Blurb Generation

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 2 of 7 — ChatGPT Blurb Generation**\nWould you like the bot to help generate a ChatGPT prompt each day when you assign a train?\nThis lets you quickly produce a personalised announcement blurb for the member.\n*(You can always set this up later by running \`/setup_train\` again)*` | `setup_cog.py` |
| Button | `Yes` (success) | `setup_cog.py` |
| Button | `No` (secondary) | `setup_cog.py` |
| Info | `ℹ️ *Skipping Steps 3–6 (themes, tones, default tone, prompt template) — blurb generation is off.*` | `setup_cog.py` |

#### 6.1.4 Step 3 — Themes

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 3 of 7 — Themes**\nThese appear as options when selecting a theme for a member's train day.\n\n**Defaults:**\n\`{existing_themes}\`` | `setup_cog.py` |
| Note | `\n*Free tier: up to {themes_cap} themes. Upgrade for unlimited.*` | `setup_cog.py` |
| Button | `✅ Use defaults` (success) | `setup_cog.py` |
| Button | `✏️ Define my own` (secondary) | `setup_cog.py` |
| Success | `✅ Using defaults for {label}.` | `setup_cog.py` |
| Wizard prompt | `Enter your themes as a comma-separated list:` | `setup_cog.py` |
| Info | `ℹ️ Free tier: only the first {cap} themes were saved (\`{joined}\`). Upgrade to Premium to save more.` | `setup_cog.py` |

#### 6.1.5 Step 4 — Tones

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 4 of 7 — Tones**\nThese let leadership adjust the writing style of the generated blurb.\n\n**Defaults:**\n\`{existing_tones}\`` | `setup_cog.py` |
| Note | `\n*Free tier: up to {tones_cap} tones. Upgrade for unlimited.*` | `setup_cog.py` |
| Wizard prompt | `Enter your tones as a comma-separated list:` | `setup_cog.py` |
| Info | `ℹ️ Free tier: only the first {cap} tones were saved (\`{joined}\`). Upgrade to Premium to save more.` | `setup_cog.py` |

#### 6.1.6 Step 5 — Default Tone

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 5 of 7 — Default Tone**\nWhich tone should be pre-selected by default?` | `setup_cog.py` |
| Select placeholder | `Select default tone...` | `setup_cog.py` |
| Success | `✅ Default tone: **{selected}**` | `setup_cog.py` |

#### 6.1.7 Step 6 — Prompt Templates

| Type | Copy | File |
|---|---|---|
| Embed title | `**Step 6 of 7 — Prompt Templates**` | `setup_cog.py` |
| Embed desc | `Saved ChatGPT prompt templates. The default ⭐ is the one used by the blurb wizard unless a member's day overrides it.\n\n{listing}\n\n*Slot usage: **{count} of {cap_label}**.*` | `setup_cog.py` |
| Button | `➕ Add` (success) | `setup_cog.py` |
| Button | `✏️ Edit` (primary) | `setup_cog.py` |
| Button | `⭐ Set Default` (secondary) | `setup_cog.py` |
| Button | `🗑️ Delete` (danger) | `setup_cog.py` |
| Button | `✅ Done` (success) | `setup_cog.py` |
| Select placeholder | `Pick a template…` | `setup_cog.py` |
| Wizard prompt | `Which template?` | `setup_cog.py` |
| Success | `🗑️ Removed **{name}**. (Restored an empty Default — you need at least one template.)` | `setup_cog.py` |
| Success | `🗑️ Removed **{name}**.` | `setup_cog.py` |
| Success | `⭐ Default set to **{name}**.` | `setup_cog.py` |
| Wizard prompt | `**Template name** *(short label)* — *editing* \`{name}\`\nReply with a name (e.g. \`Birthday\`, \`Welcome\`, \`Default\`). Reply \`cancel\` to abort.` | `setup_cog.py` |
| Warning | `⚠️ A template named **{new_name}** already exists. Try a different name.` | `setup_cog.py` |
| Wizard prompt | (see "Template body prompt" below) | `setup_cog.py` |
| Success | `✅ Updated **{name}**.` | `setup_cog.py` |
| Success | `✅ Added **{name}** ({count} of {cap_label}).` | `setup_cog.py` |

```text
[Template body prompt]
**Template body**
Paste the full ChatGPT prompt. Use these placeholders:
• `{name}` — the member's name
• `{theme}` — the selected theme
• `{tone}` — the selected tone
• `{notes}` — any notes stored for this member
*Reply `cancel` to abort, `keep` to keep the current body.*
```

#### 6.1.8 Step 7 — Reminders

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 7 of 7 — Train Reminders**\nShould the bot post a reminder to leadership when someone is assigned the train each day?` | `setup_cog.py` |
| Info | `ℹ️ *Skipping Steps 7a–7b (reminder channel and time) — train reminders are off.*` | `setup_cog.py` |
| Wizard prompt | `**Step 7a of 7 — Reminder Channel**\nWhich channel should the train reminder be posted to?` | `setup_cog.py` |
| Select placeholder | `Select the reminder channel...` | `setup_cog.py` |
| Wizard prompt | `**Step 7b of 7 — Reminder Time**\nWhat time should the reminder fire? *(in your timezone: {tz_label})*\n*(e.g. \`10:00pm\`, \`9:00am\`)*` | `setup_cog.py` |
| Modal title | `Reminder Time` | `setup_cog.py` |
| Input label | `Time` | `setup_cog.py` |
| Warning | `⚠️ Could not read that time after a few tries. Run \`/setup_train\` to start over.` | `setup_cog.py` |
| Warning | `⚠️ Could not read **\`{time_raw}\`** as a time. Try \`10:00pm\`, \`9:00am\`, or \`22:00\`. Let's try once more.` | `setup_cog.py` |

#### 6.1.9 Save summary

| Type | Copy | File |
|---|---|---|
| Embed title | `✅ Train Schedule Configured` | `setup_cog.py` |
| Embed field | `Sheet Tab` / `Blurb Generation` / `Reminders` / `Reminder Channel` / `Reminder Time` / `Default Tone` / `Themes` / `Tones` / `Templates ({count})` / `Default Template Preview` | `setup_cog.py` |
| Embed footer | `Run /setup_train again to update any of these settings.` | `setup_cog.py` |

### 6.2 `/train` and action bar

| Type | Copy | File |
|---|---|---|
| Description | `View the train schedule with Add / Update / Generate Prompt / Clear buttons` | `train_cog.py` |
| Embed title | `🚂 Alliance Train Schedule` | `train.py` |
| Embed desc (empty schedule) | `*No schedule set. Use the **➕ Add** button below to add entries.*` | `train.py` |
| Embed line (today, with entry, done) | `🟢 {day_str} — {name}{🎂 if bday} — ✅ Done` | `train.py` |
| Embed line (today, with entry, pending) | `🟢 {day_str} — {name}{🎂 if bday} — ⏳ Pending` | `train.py` |
| Embed line (today, empty) | `🟢 {day_str} — [Empty]` | `train.py` |
| Embed line (future, with entry) | `{day_str} — {name}{🎂 if bday}` | `train.py` |
| Embed line (future, empty) | `{day_str} — [Empty]` | `train.py` |
| Embed field name | `✅ Past 7 Days` | `train.py` |
| Button | `➕ Add` (success) | `train_ui.py` |
| Button | `✏️ Update` (primary) | `train_ui.py` |
| Button | `📋 Generate Prompt` (secondary) | `train_ui.py` |
| Button | `🗑️ Clear` (danger) | `train_ui.py` |

### 6.3 Add Entry modal

| Type | Copy | File |
|---|---|---|
| Modal title | `Add Train Entry` | `train_ui.py` |
| Input label | `Date` | `train_ui.py` |
| Input placeholder | `e.g. April 5 or 4/5` | `train_ui.py` |
| Input label | `Member name` | `train_ui.py` |
| Input placeholder | `Exactly as it should appear` | `train_ui.py` |
| Error | `⚠️ Could not parse date \`{date_text}\`. Try formats like \`April 5\` or \`4/5\`.` | `train_ui.py` |
| Success (add) | `✅ Added **{name}** for **{d:%A, %B} {d.day}**.` | `train_ui.py` |
| Success (overwrite) | `✅ Updated **{name}** for **{d:%A, %B} {d.day}**.` | `train_ui.py` |
| Wizard prompt (post-add) | `{msg}\n\nRun the blurb wizard now to build the ChatGPT prompt?` | `train_ui.py` |

### 6.4 Update Entry flow

| Type | Copy | File |
|---|---|---|
| Info (no entries) | `ℹ️ No entries to update in the past 7 / next 30 days. Use **➕ Add** to create one.` | `train_ui.py` |
| Wizard prompt | `Select an entry to update:` | `train_ui.py` |
| Select placeholder | `Choose an entry to update...` | `train_ui.py` |
| Select option | `{d_obj:%a %b} {d_obj.day} — {entry.name}` | `train_ui.py` |
| Modal title | `Update Train Entry` | `train_ui.py` |
| Input label | `Date` | `train_ui.py` |
| Input label | `Member name` | `train_ui.py` |
| Error | `⚠️ Could not parse date \`{date_text}\`.` | `train_ui.py` |
| Success | `✅ Updated → **{new_name}** on **{d:%A, %B} {d.day}**.` | `train_ui.py` |
| Wizard prompt (post-update) | `{msg}\n\nRe-run the blurb wizard to refresh the ChatGPT prompt?` | `train_ui.py` |

### 6.5 Run-Wizard prompt (post Add/Update)

| Type | Copy | File |
|---|---|---|
| Button | `✅ Run blurb wizard` (success) | `train_ui.py` |
| Button | `⏭️ Skip` (secondary) | `train_ui.py` |

### 6.6 Generate Prompt flow

| Type | Copy | File |
|---|---|---|
| Info (no entries) | `ℹ️ No filled entries in the next 14 days. Use **➕ Add** or **✏️ Update**, then run the blurb wizard to fill in theme/tone/notes first.` | `train_ui.py` |
| Wizard prompt | `Select an entry to generate a prompt for:` | `train_ui.py` |
| Select placeholder | `Choose an entry...` | `train_ui.py` |
| Select option | `{d_obj:%a %b} {d_obj.day} — {entry.name}` | `train_ui.py` |
| Success / Channel post | `✅ **ChatGPT prompt for {entry.name}** — copy and paste into the thread:\n\`\`\`\n{prompt}\n\`\`\`` | `train_ui.py` |

### 6.7 Clear flow

| Type | Copy | File |
|---|---|---|
| Warning | `⚠️ Clear the entire train schedule? This cannot be undone.` | `train_ui.py` |
| Button | `Yes, clear it` (danger) | `train_ui.py` |
| Button | `Cancel` (secondary) | `train_ui.py` |
| Success | `🗑️ Train schedule cleared.` | `train_ui.py` |
| Cancel | `✅ Clear cancelled. Your train schedule is unchanged.` | `train_ui.py` |

### 6.8 Blurb wizard (Theme → Tone → Notes → optional Template)

| Type | Copy | File |
|---|---|---|
| Warning | `⚠️ You already have an active session. Use \`/cancel\` to stop it first.` | `train_ui.py` |
| Wizard prompt (intro) | `🚂 **Train Blurb Wizard for {name}** — {d_label}\n*(Type \`/cancel\` at any time to stop)*` | `train_ui.py` |
| Wizard prompt (Step 1) | `**Step 1 of 3 — Theme**\nSelect the theme for this train:` | `train_ui.py` |
| Select placeholder | `Choose a theme...` | `train.py` |
| Wizard prompt (custom theme) | `Type your custom theme:` | `train_ui.py` |
| Timeout | `⏰ Wizard timed out. Run \`/train\` and click **📋 Generate Prompt** to try again.` | `train_ui.py` |
| Wizard prompt (Step 2) | `**Step 2 of 3 — Tone**\nSelect the tone:` | `train_ui.py` |
| Select placeholder | `Choose a tone...` | `train.py` |
| Wizard prompt (Step 3) | `**Step 3 of 3 — Notes** *(highly recommended)*\nAdd anything personal — role, personality, achievements. Type your notes, or type \`skip\`:` | `train_ui.py` |
| Wizard prompt (Step 4, premium) | `**Step 4 of 4 — Template** *(💎 Premium)*\nYou have multiple saved templates. Pick one for this prompt:` | `train_ui.py` |
| Select placeholder | `Pick a saved template…` | `train_ui.py` |
| Success (template picked) | `✅ Template: **{self.selected}**` | `train_ui.py` |
| Success / Channel post | `✅ **ChatGPT prompt for {name}** — copy and paste into the thread:\n\`\`\`\n{prompt}\n\`\`\`` | `train_ui.py` |

### 6.9 Default themes & tones

| Type | Copy | File |
|---|---|---|
| Select option | `Welcome to the Alliance` | `train.py` |
| Select option | `Birthday` | `train.py` |
| Select option | `Milestone` | `train.py` |
| Select option | `War / Performance` | `train.py` |
| Select option | `General Celebration` | `train.py` |
| Select option | `Contest / Raffle` | `train.py` |
| Select option | `Custom` | `train.py` |
| Select option | `Default (match the theme)` | `train.py` |
| Select option | `More casual` | `train.py` |
| Select option | `More intense` | `train.py` |
| Select option | `Funny` | `train.py` |
| Select option | `Serious` | `train.py` |
| Select option | `Cinematic / Dramatic` | `train.py` |

### 6.10 Default ChatGPT prompt fallback (when no template configured)

| Type | Copy | File |
|---|---|---|
| Default template | `Member: {name}\nTheme: {theme} — {tone}\nNotes: {notes}` *(Tone suffix omitted when empty/Default; Notes line omitted when empty)* | `train.py` |

### 6.11 `/train_log`

| Type | Copy | File |
|---|---|---|
| Description | `Show the train prompt log (window depends on your tier; pass a date to filter)` | `train_cog.py` |
| Param desc (date) | `Optional date, e.g. 'April 14' or '4/14'` | `train_cog.py` |
| Error | `⚠️ Could not load schedule: {e}` | `train_cog.py` |
| Error | `⚠️ Could not parse date **{date}**. Try a format like \`April 14\` or \`4/14\`.` | `train_cog.py` |
| Embed title | `🚂 Train Prompt Log` | `train_cog.py` |
| Embed desc (no entry for date) | `*No train entry found for {target_date:%B} {target_date.day}, {target_date.year}.*` | `train_cog.py` |
| Embed field | `Date` \| `{target_date:%A, %B} {target_date.day}, {target_date.year}` | `train_cog.py` |
| Embed field | `Name` \| `{entry.name}` or `*not set*` | `train_cog.py` |
| Embed field | `Theme` \| `{entry.theme}` or `*not set*` | `train_cog.py` |
| Embed field | `Tone` \| `{entry.tone}` or `*not set*` | `train_cog.py` |
| Embed field | `Notes` \| `{entry.notes}` or `*none*` | `train_cog.py` |
| Embed field | `Prompt Retrieved` \| `✅ Yes` / `❌ No` | `train_cog.py` |
| Embed desc (no recent) | `*No train entries in the past {window_days} days.*` | `train_cog.py` |
| Embed line | `• **{d:%a %b} {d.day}** — {name} · {theme} · prompt {retrieved}` | `train_cog.py` |
| Embed footer (free tier) | `Free tier: {window_days}-day window. Upgrade to Premium for 30 days.` | `train_cog.py` |
| Embed footer (premium) | `Showing the most recent 20 entries within ±{window_days} days. Pass a date to filter.` | `train_cog.py` |

### 6.12 `/train_addbirthdays`

| Type | Copy | File |
|---|---|---|
| Description | `Manually run the birthday check and add upcoming birthdays to the schedule` | `train_cog.py` |
| Success | `✅ Birthday check complete — added **{added}** birthday entr{'y' if added == 1 else 'ies'} to the schedule.` | `train_cog.py` |
| Success+Warning | `✅ Birthday check complete — added **{added}** birthday entr{'y' if added == 1 else 'ies'} to the schedule. ⚠️ **{len(alerts)}** conflict(s) posted above require manual action.` | `train_cog.py` |
| Warning | `⚠️ Birthday check complete — **{len(alerts)}** conflict(s) posted above require manual action.` | `train_cog.py` |
| Success | `✅ Birthday check complete — no new entries to add within the next {BIRTHDAY_LOOKAHEAD} days.` | `train_cog.py` |
| Error | `⚠️ Birthday check failed: {e}` | `train_cog.py` |

### 6.13 `/cancel`

| Type | Copy | File |
|---|---|---|
| Description | `Cancel any active wizard or log session` | `train_cog.py` |
| Cancel | `❌ Session cancelled.` | `train_cog.py` |
| Info | `ℹ️ You don't have an active session running.` | `train_cog.py` |

### 6.14 Daily train reminder loop

| Type | Copy | File |
|---|---|---|
| Channel post (blurbs on) | `🚂 **Reset! Today's train is for {display}.**\n\nClick below whenever you're ready to get the ChatGPT prompt — no rush, run it when the team is available.\n\n⚠️ *If the button stops working after a bot restart, use \`/train\` → 📋 Generate Prompt instead.*` | `train_cog.py` |
| Channel post (blurbs off) | `🚂 **Reset! Today's train is for {display}.**` | `train_cog.py` |
| Button | `📋 View & Get Prompt` (success) | `train.py` |
| Error (not configured) | `⚙️ Bot not configured. Run \`/setup\`.` | `train.py` |
| Error (missing role) | `⛔ You need the **{req_role}** role.` | `train.py` |
| DM (premium, to today's member) | `🚂 Heads up — **today's train is for you!** Leadership has been notified, so look out for the announcement.` | `train_cog.py` |
| Timeout (view expired, appended to reminder message) | `\n\n⏰ *The actions for this have timed out. Use \`/train\` to re-initiate.*` | `wizard_registry.py` |

---

## 7. Birthdays

### 7.1 `/setup_birthdays`

#### 7.1.1 Command + entry

| Type | Copy | File |
|---|---|---|
| Description | `Configure birthday tracking — sheet tab, columns, and lookahead days` | `setup_cog.py` |
| Success | `⚙️ Starting birthday setup — check the channel for prompts!` | `setup_cog.py` |
| Wizard prompt | `⚙️ **Birthday Tracking Setup**\nConfigure how the bot tracks member birthdays.` | `setup_cog.py` |

#### 7.1.2 Step 1 — Enable

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 1 of 9 — Enable birthday tracking?**\nShould the bot track member birthdays from your Google Sheet?` | `setup_cog.py` |
| Success | `✅ Birthday tracking disabled.` | `setup_cog.py` |

#### 7.1.3 Step 2 — Sheet Tab

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 2 of 9 — Sheet Tab**\nWhich tab in your Google Sheet contains birthday data?\n⚠️ *Make sure this tab exists in your sheet before continuing.*` | `setup_cog.py` |

#### 7.1.4 Step 3 — Name Column

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 3 of 9 — Name Column**\nWhich column contains the member's name?` | `setup_cog.py` |
| Warning | `⚠️ Please enter a single column letter like \`A\`. Run \`/setup_birthdays\` to try again.` | `setup_cog.py` |

#### 7.1.5 Step 4 — Birthday Column

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 4 of 9 — Birthday Column**\nWhich column contains the member's birthday?` | `setup_cog.py` |
| Modal title | `Birthday Column` | `setup_cog.py` |
| Warning | `⚠️ Please enter a single column letter like \`B\`. Run \`/setup_birthdays\` to try again.` | `setup_cog.py` |

#### 7.1.6 Step 5 — Train Integration

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 5 of 9 — Train Schedule Integration**\nShould the bot automatically add members to the train schedule on their birthday?` | `setup_cog.py` |
| Info | `ℹ️ *Skipping Steps 6–7 (placement and lookahead) — train integration is off.*` | `setup_cog.py` |
| Info (when train integration on) | `ℹ️ Heads up: birthdays auto-populate the train schedule **once per day** (on the bot's first tick after server-time midnight). If you need a birthday reflected on the schedule sooner, run \`/train_addbirthdays\` to trigger the check on demand.` | `setup_cog.py` |

#### 7.1.7 Step 6 — Birthday Placement

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 6 of 9 — Birthday Placement**\nIf the member's birthday is already taken on the train schedule, what should the bot do?` | `setup_cog.py` |
| Button | `🎂 Birthday only` (primary) | `setup_cog.py` |
| Button | `📅 Assign nearby if taken` (secondary) | `setup_cog.py` |
| Success | `✅ Placement: **Birthday only**` | `setup_cog.py` |
| Success | `✅ Placement: **Assign 1 day before or after if birthday is taken**` | `setup_cog.py` |

#### 7.1.8 Step 7 — Lookahead

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 7 of 9 — Train Schedule Lookahead**\nSince you enabled train integration, how many days ahead of a member's birthday should the bot pre-populate them on the train schedule? This only applies to train-integration auto-placement; the birthday announcement itself always fires on the day.\n*(we recommend 14)*` | `setup_cog.py` |
| Modal title | `Lookahead Days` | `setup_cog.py` |
| Input label | `Number of days` | `setup_cog.py` |
| Warning | `⚠️ Please enter a number like \`14\`. Run \`/setup_birthdays\` to try again.` | `setup_cog.py` |

#### 7.1.9 Step 8 — Birthday Reminders

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 8 of 9 — Birthday Reminders**\nShould the bot post a message in Discord on a member's birthday?\n*(It will post: "🎂 Today is **[name]**'s birthday!")*` | `setup_cog.py` |
| Info | `ℹ️ *Skipping Steps 8a–8b (reminder channel and time) — birthday reminders are off.*` | `setup_cog.py` |
| Wizard prompt | `**Step 8a of 9 — Birthday Announcement Channel**\nWhich channel should birthday announcements be posted in?` | `setup_cog.py` |
| Select placeholder | `Select the birthday announcement channel...` | `setup_cog.py` |
| Wizard prompt | `**Step 8b of 9 — Reminder Time**\nWhat time should birthday announcements be posted? *(in {tz_label})*\n*(e.g. \`8:00am\`, \`12:00pm\`)*` | `setup_cog.py` |
| Warning | `⚠️ Could not read that time after a few tries. Run \`/setup_birthdays\` to start over.` | `setup_cog.py` |
| Warning | `⚠️ Could not read **\`{time_raw}\`** as a time. Try \`8:00am\`, \`12:00pm\`, or \`08:00\`. Let's try once more.` | `setup_cog.py` |

#### 7.1.10 Step 9 — Birthday DM Body (💎 Premium)

Step only runs if reminders were enabled in Step 8. Free guilds can configure the template now — it just won't fire until they have Premium + Member Roster Sync + a Discord ID column wired up in the birthday sheet. Uses the standard `ask_keep_or_change` flow.

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 9 of 9 — Birthday DM Body (💎 Premium)**\nWhen a birthday fires, the bot also DMs the member directly with a personal note. Free guilds can configure this now — it just won't fire until you have Premium + Member Roster Sync + a Discord ID column in your birthday sheet.\n\nUse \`{name}\` as a placeholder for the member's name.` | `setup_cog.py` |
| Modal title | `Birthday DM Body` | `setup_cog.py` |
| Input label | `DM body (max 1000 chars)` | `setup_cog.py` |
| Default (used when "Use default" picked) | `🎂 Happy birthday, **{name}**! Wishing you a great day from everyone at the alliance.` | `train_cog.py` (`DEFAULT_BIRTHDAY_DM`) |

#### 7.1.11 Save summary

| Type | Copy | File |
|---|---|---|
| Embed title | `✅ Birthday Tracking Configured` | `setup_cog.py` |
| Embed field | `Sheet Tab` / `Name Column` / `Birthday Column` / `Discord ID Column` / `Train Integration` / `Placement` / `Lookahead` / `Reminders` / `Reminder Channel` / `Reminder Time` | `setup_cog.py` |
| Embed footer | `Run /setup_birthdays again to update these settings.` | `setup_cog.py` |

### 7.2 `/birthdays`

| Type | Copy | File |
|---|---|---|
| Description | `Show upcoming birthdays from your member sheet (uses your configured lookahead window)` | `train_cog.py` |
| Error | `⚠️ Could not load birthdays: {e}` | `train_cog.py` |
| Warning | `⚠️ No birthdays found in **{tab_name}**. Run \`/setup_birthdays\` to verify the tab and column settings.` | `train_cog.py` |
| Embed title | `🎂 Upcoming Birthdays — Next {window_days} Days` | `train_cog.py` |
| Embed desc (empty) | `*No birthdays in the next 14 days.*` | `train_cog.py` |
| Embed line (today) | `• **{when:%A, %B} {when.day}** — {name} *(**Today!**)*` | `train_cog.py` |
| Embed line (tomorrow) | `• **{when:%A, %B} {when.day}** — {name} *(Tomorrow)*` | `train_cog.py` |
| Embed line (future) | `• **{when:%A, %B} {when.day}** — {name} *(in {days_away} days)*` | `train_cog.py` |
| Embed footer | `Source: {tab_name} · Run /setup_birthdays to change settings` | `train_cog.py` |

### 7.3 Daily birthday announcement loop

| Type | Copy | File |
|---|---|---|
| Channel post (with Discord ID) | `🎂 Today is <@{discord_id}>'s birthday!` | `train_cog.py` |
| Channel post (name only) | `🎂 Today is **{name}**'s birthday!` | `train_cog.py` |
| DM (premium, to birthday member) | `🎂 Happy birthday, **{name}**! Wishing you a great day from everyone at the alliance.` | `train_cog.py` |

### 7.4 Birthday → Train integration (auto-population & conflict alerts)

| Type | Copy | File |
|---|---|---|
| Channel post (leadership conflict alert) | (see "Birthday conflict alert" block below) | `train_birthdays.py` |
| Default note (placed on birthday) | `Auto-added from birthday sheet` | `train_birthdays.py` |
| Default note (placed day before) | `Auto-added from birthday sheet (placed day before due to conflict on actual birthday)` | `train_birthdays.py` |
| Default note (placed day after) | `Auto-added from birthday sheet (placed day after due to conflict on actual birthday)` | `train_birthdays.py` |

```text
[Birthday conflict alert]
🚨 **Birthday scheduling conflict — manual action needed!**
**{name}'s** birthday is **{bday:%A, %B} {bday.day}** but all three surrounding dates are taken:
• {Mon Apr 14} ({occupant})
• {Sun Apr 13} ({occupant})
• {Tue Apr 15} ({occupant})
Please manually add {name} to the schedule.
```

---

## 8. Desert Storm & Canyon Storm

> **CS vs DS divergence**: nearly all of the copy is shared — the only
> intentional differences are the `⚔️` / `🏜️` icons, the `Desert Storm` /
> `Canyon Storm` labels, the `🔥` (DS) / `⚡` (CS) emoji on the Step 1 wizard
> header, the embed colors (DS dark red, CS gold), and the legacy log field
> labels (`Prior Sit-Out No Vote` for DS vs `Prior Sit-Out No Request` for
> CS). The shared `/desertstorm remind` / `/canyonstorm remind` DM uses ⚔️
> for both events — possible inconsistency worth flagging.

### 8.1 `/setup_desertstorm` and `/setup_canyonstorm`

#### 8.1.1 Command + entry

| Type | Copy | File |
|---|---|---|
| Description | `Configure Desert Storm mail template and time options` | `setup_cog.py` |
| Description | `Configure Canyon Storm mail template and time options` | `setup_cog.py` |
| Success | `⚙️ Starting Desert Storm setup — check the channel for prompts!` | `setup_cog.py` |
| Success | `⚙️ Starting Canyon Storm setup — check the channel for prompts!` | `setup_cog.py` |
| Wizard prompt | `⚙️ **{label} Setup**` | `setup_cog.py` |

#### 8.1.2 Step 1 — Sheet Tab

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 1 of 6 — Sheet Tab**\nWhich tab in your Google Sheet stores the {label} zone assignments?\n⚠️ *Make sure this tab exists in your sheet before continuing.*\nℹ️ *The bot will manage the data structure of this tab automatically — you don't need to set up any specific columns or formatting beforehand.*` | `setup_cog.py` |

#### 8.1.3 Step 2 — Teams

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 2 of 6 — Which teams do you run for {label}?**` | `setup_cog.py` |
| Button | `Team A & Team B` (primary) | `setup_cog.py` |
| Button | `Team A only` (secondary) | `setup_cog.py` |
| Button | `Team B only` (secondary) | `setup_cog.py` |
| Success | `✅ Teams: **Team A & Team B**` | `setup_cog.py` |
| Success | `✅ Teams: **Team A only**` | `setup_cog.py` |
| Success | `✅ Teams: **Team B only**` | `setup_cog.py` |

#### 8.1.4 Step 3 — Storm Log Channel

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 3 of 6 — Storm Log Channel**\nSelect the channel where {label} participation/log summaries will be posted:` | `setup_cog.py` |
| Select placeholder | `Select the {label} log channel...` | `setup_cog.py` |

#### 8.1.5 Step 4 — Mail Post Channel

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 4 of 6 — Mail Post Channel**\nWhen leadership clicks **Post & Copy** at the end of \`/{desertstorm\|canyonstorm}_draft\`, the finished mail will be posted to this channel:` | `setup_cog.py` |
| Select placeholder | `Select the {label} mail post channel...` | `setup_cog.py` |

#### 8.1.6 Step 5 — Mail Template

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 5 of 6 — Mail Template**\nDo you want one template that applies to both teams, or separate templates per team?` | `setup_cog.py` |
| Button | `One template for both teams` (primary) | `setup_cog.py` |
| Button | `Separate templates per team` (secondary) | `setup_cog.py` |
| Success | `✅ **One shared template** for Team A & B` | `setup_cog.py` |
| Success | `✅ **Separate templates** for Team A & Team B` | `setup_cog.py` |
| Wizard prompt | (see "Storm template prompt" below) | `setup_cog.py` |
| Button | `✅ Use default template` (success) | `setup_cog.py` |
| Button | `✏️ Edit template` (secondary) | `setup_cog.py` |
| Success | `✅ Using default template for {team_label}.` | `setup_cog.py` |
| Wizard prompt | (see "Custom storm template prompt" below) | `setup_cog.py` |

```text
[Storm template prompt]
**{label} Mail Template — {team_label}**
When you draft the mail each week, you will be able to select the time slot when you are running that team's {label}.

Here is the default template:
```
{default_template}
```
Would you like to use this or edit it?
```

```text
[Custom storm template prompt]
Paste your custom template for **{team_label}**. You can copy the default above and modify it, or write your own.

**Available placeholders:**
• `{alliance_name}` — your alliance name
• `{zones}` — zone assignments block
• `{subs}` — substitute members
• `{time}` — event time (auto-filled when drafting)

*This form will time out in 5 minutes. You can run `/{cmd_name}` again if it times out.*
```

#### 8.1.7 Step 6 — Participation Tracking

| Type | Copy | File |
|---|---|---|
| Wizard prompt | (see "Participation enable prompt" below) | `setup_cog.py` |
| Wizard prompt | `**Step 6.1 — Participation Sheet Tab**\nWhich tab should the bot write {label} participation rows to?\nℹ️ *The bot will create this tab automatically if it doesn't exist and will manage the column structure based on the questions you define.*` | `setup_cog.py` |
| Modal title | `Participation Tab` | `setup_cog.py` |
| Wizard prompt | `**Step 6.2 — Roster Source: Sheet Tab**\nWhich tab in your sheet has the list of members? The bot reads member names from here when you use a \`Roster names\` question.\n*Tip: this is often the same tab you use for \`/setup_survey\` or \`/setup_birthdays\`.*` | `setup_cog.py` |
| Modal title | `Roster Tab` | `setup_cog.py` |
| Wizard prompt | `**Step 6.3 — Roster Source: Name Column**\nWhich column letter has the member name? (e.g. \`A\`, \`B\`, \`E\`)` | `setup_cog.py` |
| Modal title | `Name column` | `setup_cog.py` |
| Warning | `⚠️ \`{raw_name_col}\` isn't a valid column letter. Run \`/{cmd_name}\` to start again.` | `setup_cog.py` |
| Wizard prompt | `**Step 6.4 — Roster Source: Alias Column?**\nIf you have other names or nicknames that you call your members in these mails, this helps resolve to their full name in your sheet automatically. Do you have an alias column?` | `setup_cog.py` |
| Wizard prompt | `**Alias Column**\nWhich column letter has the alias / nickname?` | `setup_cog.py` |
| Modal title | `Alias column` | `setup_cog.py` |
| Wizard prompt | `**Step 6.5 — Roster Source: First Data Row**\nIn your existing roster tab above, which row does the member data start on? Usually \`2\` if your sheet has a header row in row 1.` | `setup_cog.py` |
| Modal title | `Data start row` | `setup_cog.py` |
| Warning | `⚠️ \`{raw_start}\` isn't a number. Run \`/{cmd_name}\` to start again.` | `setup_cog.py` |
| Wizard prompt | (see "Participation questions builder" below) | `setup_cog.py` |
| Note | `\n*Free tier limit: {cap} questions.*` | `setup_cog.py` |
| Note | `\n💎 *Premium: unlimited questions and three extra question types.*` | `setup_cog.py` |
| Select placeholder | `✏️ Edit a question…` | `setup_cog.py` |
| Select placeholder | `🗑️ Remove a question…` | `setup_cog.py` |
| Button | `➕ Add question` (primary) | `setup_cog.py` |
| Button | `✅ Done` (success) | `setup_cog.py` |
| Success | `🗑️ Removed: **{label}**` | `setup_cog.py` |
| Success | `✅ Updated: **{label}**` | `setup_cog.py` |
| Success | `✅ Added: **{label}** ({n} so far)` | `setup_cog.py` |

```text
[Participation enable prompt]
**Step 6 of 6 — Participation Tracking**
Do you want to track {label} participation? Leadership runs `/{cmd}_participation` after each event to log who showed up, who sat out, etc.
You'll define the questions yourself, so the tracker matches how your alliance runs the event.
```

```text
[Participation questions builder]
**Step 6.6 — Participation Questions**
Each question becomes a column on your sheet and a step in the `/{cmd}_participation` flow.
Examples: *Vote count*, *Sitting out*, *Did anyone show up late?*
{cap_note}

{summary}
```

#### 8.1.8 Participation question builder

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Question — Label**\nWhat's the label for this question? (e.g. \`Sitting Out\`, \`Vote Count\`)` | `setup_cog.py` |
| Warning | `⚠️ Empty label. Skipping this question.` | `setup_cog.py` |
| Wizard prompt | `**Question — Answer Type**` | `setup_cog.py` |
| Select placeholder | `Pick the answer type…` | `setup_cog.py` |
| Select option | `Text — short typed answer` | `setup_cog.py` |
| Select option | `Yes / No` | `setup_cog.py` |
| Select option | `Numeric — number with optional min/max` | `setup_cog.py` |
| Select option | `Roster names — pick or type member names` | `setup_cog.py` |
| Select option | `💎 Single-select dropdown` | `setup_cog.py` |
| Select option | `💎 Multi-select dropdown` | `setup_cog.py` |
| Select option | `💎 Date (formatted entry)` | `setup_cog.py` |
| Success | `✅ Type: **{type_label}**` | `setup_cog.py` |
| Wizard prompt | `**Optional — bounds**\nReply with \`min,max\` (e.g. \`0,500\`) or type \`none\` for no bounds.` | `setup_cog.py` |
| Warning | `⚠️ Couldn't parse those bounds — saving without min/max.` | `setup_cog.py` |
| Wizard prompt | `**Options** *(💎 Premium)*\nList the choices separated by commas.\nExample: \`Win, Loss, Draw\`` | `setup_cog.py` |
| Warning | `⚠️ No options provided. Skipping this question.` | `setup_cog.py` |
| Wizard prompt | `**Date format** *(💎 Premium)*\nEnter a \`strptime\`-style format (e.g. \`%m/%d/%Y\`) or reply \`default\` for \`%m/%d/%Y\`.` | `setup_cog.py` |

#### 8.1.9 Save summary

| Type | Copy | File |
|---|---|---|
| Embed title | `✅ {label} Configured` | `setup_cog.py` |
| Embed field | `Sheet Tab` / `Teams` / `Timezone` / `Log Channel` / `Post Channel` / `Participation Tracking` / `Template A Preview` / `Template B Preview` | `setup_cog.py` |
| Embed value | `✅ Enabled · {n} question(s) · Tab: \`{tab}\`` | `setup_cog.py` |
| Embed value | `❌ Disabled` | `setup_cog.py` |
| Embed footer | `Run /{cmd_name} again to update.` | `setup_cog.py` |

### 8.2 `/desertstorm overview` and `/canyonstorm overview`

| Type | Copy | File |
|---|---|---|
| Description | `Show the configured Desert Storm setup and current rosters` | `storm.py` |
| Description | `Show the configured Canyon Storm setup and current rosters` | `storm.py` |
| Embed title | `⚔️ Desert Storm` | `storm.py` |
| Embed title | `🏜️ Canyon Storm` | `storm.py` |
| Embed field | `Sheet Tab` \| `{tab_name}` or `*not set*` | `storm.py` |
| Embed field | `Log Channel` \| `<#{log_channel_id}>` or `*not set*` | `storm.py` |
| Embed field | `Time Option 1` \| `{local} ({HH:MM} server time)` (game-fixed: DS=18:00, CS=12:00) | `storm.py` |
| Embed field | `Time Option 2` \| `{local} ({HH:MM} server time)` (game-fixed: DS=23:00, CS=23:00) | `storm.py` |
| Embed field | `Current Mail Template (Team A)` \| ` ```\n{preview}\n``` ` | `storm.py` |
| Embed field (error) | `Current Mail Template` \| `⚠️ Could not load: {e}` | `storm.py` |
| Embed footer | `Run /{setup_cmd} to update. Run /{cmd_name}_draft to generate a draft.` | `storm.py` |

### 8.3 `/desertstorm draft` and `/canyonstorm draft` — 4-step wizard

#### 8.3.1 Command + guard messages

| Type | Copy | File |
|---|---|---|
| Description | `Generate a Desert Storm mail draft for Team A or Team B` | `storm.py` |
| Description | `Generate a Canyon Storm mail draft for Team A or Team B` | `storm.py` |
| Error | `⚠️ Could not find the channel.` | `storm.py` |

#### 8.3.2 Step 1 — Pick Team

| Type | Copy | File |
|---|---|---|
| Wizard prompt (DS) | `🔥 **Desert Storm Draft** — started by {user.mention}\n\n**Step 1 of 4 — Pick Team**\nWhich team are you drafting for?` | `storm.py` |
| Wizard prompt (CS) | `⚡ **Canyon Storm Draft** — started by {user.mention}\n\n**Step 1 of 4 — Pick Team**\nWhich team are you drafting for?` | `storm.py` |
| Button | `Team A` (primary) | `storm.py` |
| Button | `Team B` (success) | `storm.py` |
| Success | `✅ Team {team} selected.` | `storm.py` |
| Timeout (DS) | `⏰ Timed out. Use \`/desertstorm draft\` to start again.` | `storm.py` |
| Timeout (CS) | `⏰ Timed out. Use \`/canyonstorm draft\` to start again.` | `storm.py` |
| Timeout (ephemeral) | `⏰ Timed out.` | `storm.py` |

#### 8.3.3 Step 2 — Pick Time

| Type | Copy | File |
|---|---|---|
| Wizard prompt (DS) | `**Step 2 of 4 — Pick Time**\n⏰ What time is Desert Storm this week?` | `storm.py` |
| Wizard prompt (CS) | `**Step 2 of 4 — Pick Time**\n⏰ What time is Canyon Storm this week?` | `storm.py` |
| Button | `{local} ({HH:MM} server time)` (secondary, truncated to 80 chars; DS slot 1 = 18:00, CS slot 1 = 12:00) | `storm.py` |
| Button | `{local} ({HH:MM} server time)` (secondary, truncated to 80 chars; both events slot 2 = 23:00) | `storm.py` |

#### 8.3.4 Step 3 — Mail Template (Use as-is or Edit)

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 3 of 4 — Mail Template (Team {team})**\nHere is the saved template for **Team {team}**:\n\`\`\`\n{template}\n\`\`\`\nUse it as-is, or edit it before posting?` | `storm.py` |
| Button | `✅ Use as-is` (success) | `storm.py` |
| Button | `✏️ Edit` (primary) | `storm.py` |
| Wizard prompt (edit) | `✏️ {user.mention} — copy the block above, make your edits, and paste it back below.\n*(10 minutes to respond — type \`cancel\` to stop)*` | `storm.py` |
| Cancel | `❌ Draft cancelled.` | `storm.py` |
| Error (DS parse fail) | `⚠️ Could not parse any zone assignments. Make sure the format matches the template and try \`/desertstorm draft\` again.` | `storm.py` |
| Error (CS parse fail) | `⚠️ Could not parse any assignments. Make sure the format matches the template and try \`/canyonstorm draft\` again.` | `storm.py` |
| Warning (parse) | `⚠️ Some lines were skipped:\n• {error1}\n• {error2}…` | `storm.py` |
| Validation retry (DS zone line) | `Could not parse zone line: {line}` | `storm.py` |
| Validation retry (DS unknown zone) | `Unknown zone \`{zone}\` — must be one of: {canonical_list}` | `storm.py` |
| Validation retry (CS unrecognized) | `Unrecognized line in Stage {stage}: {line}` | `storm.py` |
| Validation retry (CS unparseable) | `Could not parse: {line}` | `storm.py` |
| Success | `💾 **Team {team} template saved (not posted).** Review the preview below before sending it out.` | `storm.py` |

#### 8.3.5 Premium template picker (multiple saved templates)

| Type | Copy | File |
|---|---|---|
| Select placeholder | `Pick a saved template…` | `storm.py` |
| Select option | `{template_name}` *(truncated to 100 chars)* | `storm.py` |
| Wizard prompt | `💎 You have multiple saved templates. Pick one for this draft:` | `storm.py` |
| Success | `✅ Template: **{name}**` | `storm.py` |
| Timeout | `⏰ Template picker timed out. Run \`/desertstorm draft\` or \`/canyonstorm draft\` to start over.` | `storm.py` |

#### 8.3.6 Step 4 — Preview + Post & Copy

| Type | Copy | File |
|---|---|---|
| Wizard prompt (DS) | `**Step 4 of 4 — Preview**\n📬 **Desert Storm Team {team} mail preview:**\n\n{mail}\n\nDoes this look right?` | `storm.py` |
| Wizard prompt (CS) | `**Step 4 of 4 — Preview**\n📬 **Canyon Storm Team {team} mail preview:**\n\n{mail}\n\nDoes this look right?` | `storm.py` |
| Button | `✅ Looks Good — Post & Copy` (success) | `storm.py` |
| Button | `❌ Cancel` (danger) | `storm.py` |
| Cancel | `❌ Draft cancelled.` | `storm.py` |
| Channel post | `✅ **{event_label} Team {team} mail — ready to copy{suffix}:**\n\`\`\`\n{mail}\n\`\`\`` *(suffix = ` (also posted to {channel.mention})` or empty; event_label = `Desert Storm` or `Canyon Storm`)* | `storm.py` |

#### 8.3.7 Default mail body fallbacks (when no template configured)

```text
[Default DS mail body — build_ds_mail]
**Desert Storm**

**Zone Assignments**
{zones_block}

**Sub Pairs**
{subs_block}

**Time:** {time_str}
```

```text
[Default CS mail body — build_cs_mail]
**Canyon Storm**

**Zone Assignments**
{zones_block}

**Subs**
{subs_block}

**Time:** {time_str}
```

```text
[Default DS template scaffold — build_ds_template]
ZONE ASSIGNMENTS
Nuclear Silo: {…}
Oil Refinery I: {…}
…
Mercenary Factory: {…}

SUBS
{name}
…
```

```text
[Default CS template scaffold — build_cs_template]
STAGE 1
Power Tower: {…}
Data Center 1: {…}
Data Center 2: {…}
Sample Warehouse 1: {…}
Sample Warehouse 2: {…}
Sample Warehouse 3: {…}
Sample Warehouse 4: {…}
Floaters: {…}

STAGE 2
Defense System 1: {…}
Defense System 2: {…}
Serum Factory 1: {…}
Serum Factory 2: {…}
Floaters: {…}

STAGE 3
Virus Lab: {…}
Power Tower: {…}
Data Center 1: {…}
Data Center 2: {…}
Defense System 1: {…}
Defense System 2: {…}
Serum Factory 1: {…}
Serum Factory 2: {…}

Subs
{name}
…
```

### 8.4 `/desertstorm participation` and `/canyonstorm participation`

#### 8.4.1 Command + entry messages

| Type | Copy | File |
|---|---|---|
| Description | `Log Desert Storm participation data` | `storm_log.py` |
| Description | `Log Canyon Storm participation data` | `storm_log.py` |
| Error | `⚠️ You already have an active log session. Use \`/cancel\` to stop it first.` | `storm_log.py` |
| Success (DS start) | `📋 Starting DS log...` | `storm_log.py` |
| Success (CS start) | `📋 Starting CS log...` | `storm_log.py` |
| Error (not enabled) | `⚙️ Participation tracking isn't enabled for {event_label} yet. Run \`{setup_cmd}\` and walk through Step 6 to define what you want to track.` | `storm_log.py` |
| Error (no questions) | `⚙️ Participation tracking is enabled but no questions are configured. Run \`{setup_cmd}\` to add questions.` | `storm_log.py` |

#### 8.4.2 Wizard scaffolding (always-on)

| Type | Copy | File |
|---|---|---|
| Wizard prompt (header) | `📋 **{event_label} Log** — started by {user.mention}\n*{total_steps} step(s) total. Use \`/cancel\` at any time to stop.*` | `storm_log.py` |
| Wizard prompt (date — Step 1, always) | `**Step 1 — Event date**\nType the date (e.g. \`April 14\`, \`4/14\`) or type \`today\`:` | `storm_log.py` |
| Validation retry (date) | `⚠️ Could not parse \`{raw_date}\` as a date. Run \`{log_cmd}\` to start again.` | `storm_log.py` |
| Status | `⏳ Loading roster from your configured tab…` | `storm_log.py` |
| Error (empty roster) | `⚠️ The configured roster tab is empty or unreachable. Run \`{setup_cmd}\` to update the roster source, then try again.` | `storm_log.py` |
| Cancel | `❌ Log cancelled.` | `storm_log.py` |
| Timeout | `⏰ Timed out. Run \`{log_cmd}\` to start again.` | `storm_log.py` |
| Status | `💾 Saving log…` | `storm_log.py` |
| Error (save) | `⚠️ Error saving to sheet: {e}` | `storm_log.py` |
| Success | `✅ **Log saved!**\n\n{summary}` | `storm_log.py` |
| Channel post (summary header) | `📋 **{event_label} Log — {date_str}**` | `storm_log.py` |
| Channel post (summary line) | `**{qlabel}:** {value or 'None'}` | `storm_log.py` |

#### 8.4.3 Per-question scaffolding

Question header (all types): `**Step {idx} of {total_steps} — {qlabel}**`

| Type | Copy | File |
|---|---|---|
| Wizard prompt (yes_no) | `{header}\nPick one.` | `storm_log.py` |
| Button (yes_no) | `Yes` (success) | `storm_log.py` |
| Button (yes_no) | `No` (danger) | `storm_log.py` |
| Wizard prompt (numeric) | `{header}{bound_hint}\nType a number.` *(bound_hint = ` *(min \`{lo}\`, max \`{hi}\`)*` or omitted)* | `storm_log.py` |
| Validation retry (numeric NaN) | `⚠️ \`{raw}\` isn't a number. Please re-enter your answer.` | `storm_log.py` |
| Validation retry (numeric < min) | `⚠️ Must be at least **{lo}**. Please re-enter.` | `storm_log.py` |
| Validation retry (numeric > max) | `⚠️ Must be at most **{hi}**. Please re-enter.` | `storm_log.py` |
| Error (numeric exhausted) | `⚠️ Too many invalid attempts. Cancelling the log — run \`{log_cmd}\` when you're ready to try again.` | `storm_log.py` |
| Wizard prompt (roster_names) | `{header}\nPress **Enter Names** to type who applies. Press **Skip** if none.\n*Roster: {preview}*` *(preview = name list or `{N} members loaded`)* | `storm_log.py` |
| Wizard prompt (single_select) | `{header}\nPick one.` | `storm_log.py` |
| Wizard prompt (multi_select) | `{header}\nPick any that apply.` | `storm_log.py` |
| Wizard prompt (date) | `{header} *(format \`{fmt}\`)*` | `storm_log.py` |
| Validation retry (date type) | `⚠️ \`{raw}\` doesn't match \`{fmt}\`. Please re-enter.` | `storm_log.py` |
| Error (date exhausted) | `⚠️ Too many invalid attempts. Cancelling the log.` | `storm_log.py` |
| Wizard prompt (text) | `{header}\nType your answer (or \`skip\` for none).` | `storm_log.py` |

#### 8.4.4 Roster name entry sub-flow

| Type | Copy | File |
|---|---|---|
| Modal title | `{label}` *(truncated to 45 chars; sourced from question label)* | `storm_log.py` |
| Input label | `Names (comma-separated or one per line)` | `storm_log.py` |
| Input placeholder | `e.g. Alice, Bob, Chris — or leave blank and submit for none` | `storm_log.py` |
| Button | `✏️ Enter Names` (primary) | `storm_log.py` |
| Button | `Skip (none)` (secondary) | `storm_log.py` |
| Status | `*Skipped — none.*` | `storm_log.py` |
| Status (recognized) | `**Entered ({n}):** {names}` or `*None entered.*` | `storm_log.py` |
| Status (with visitors) | `**Entered ({n}):** {names}\n**Visitors:** {unrecog_str}` | `storm_log.py` |
| Warning (unrecognized) | `⚠️ **Not recognized:** {unrecog_str}\nThese names aren't in the roster. Are they visitors or did you make a typo?` | `storm_log.py` |
| Button | `Save as Visitor` (secondary) | `storm_log.py` |
| Button | `Re-enter Names` (primary) | `storm_log.py` |
| Status (redo) | `*Re-enter names — press Enter Names again:*` | `storm_log.py` |

#### 8.4.5 Single/multi-select sub-flow

| Type | Copy | File |
|---|---|---|
| Select placeholder | `{qlabel}` | `storm_log.py` |
| Select option | `{option}` *(verbatim from configured options)* | `storm_log.py` |
| Button | `✅ Done` (success) | `storm_log.py` |
| Button | `Skip (none)` (secondary) | `storm_log.py` |

### 8.5 `/desertstorm log` and `/canyonstorm log`

| Type | Copy | File |
|---|---|---|
| Description | `View a Desert Storm log entry (defaults to today)` | `storm_log.py` |
| Description | `View a Canyon Storm log entry (defaults to today)` | `storm_log.py` |
| Param desc | `Optional date, e.g. 'April 14' or '4/14' (defaults to today)` | `storm_log.py` |
| Error (parse) | `⚠️ Could not parse date **{date}**. Try a format like \`April 14\` or \`4/14\`.` | `storm_log.py` |
| Error (not found) | `❌ No **{event_label}** log found for **{month} {day}, {year}**.` | `storm_log.py` |
| Premium gate (embed title) | `📊 {event_label} log lookback — Free tier limit` | `storm_log.py` |
| Premium gate (embed desc) | `You can only see the **{recent_cap} most recent** log entries with the free tier. Upgrade to {premium.PREMIUM_BRAND} to unlock unlimited lookback.` | `storm_log.py` |
| Channel post (header) | `📋 **{event_label} Log — {date_str}**` *(date_str = `{weekday}, {month} {day}, {year}`)* | `storm_log.py` |
| Channel post (generic field line) | `**{label}:** {value or 'None'}` | `storm_log.py` |
| Channel post (legacy DS — votes) | `**Votes:** {vote_count or 'Not recorded'}` | `storm_log.py` |
| Channel post (legacy DS — RTF) | `**RTF No Vote:** {value or 'None'}` | `storm_log.py` |
| Channel post (legacy — sit-outs) | `**Sitting Out:** {value or 'None'}` | `storm_log.py` |
| Channel post (legacy DS — prior) | `**Prior Sit-Out No Vote:** {value or 'None'}` | `storm_log.py` |
| Channel post (legacy CS — prior) | `**Prior Sit-Out No Request:** {value or 'None'}` | `storm_log.py` |

### 8.6 `/desertstorm remind` and `/canyonstorm remind` 💎

| Type | Copy | File |
|---|---|---|
| Description | `💎 DM every roster member to participate in this week's Desert Storm` | `storm_log.py` |
| Description | `💎 DM every roster member to participate in this week's Canyon Storm` | `storm_log.py` |
| Premium gate | `Storm participation reminders are part of Alliance Helper Premium and require Member Roster Sync (\`/setup_members\`). Run \`/upgrade\` to unlock.` *(passed as `description` to `premium.premium_locked_embed` with `feature_label="Storm participation DMs"`)* | `storm_log.py` |
| Error | `⚙️ Member Roster Sync isn't configured yet. Run \`/setup_members\` first.` | `storm_log.py` |
| Error | `⚠️ Could not read the roster sheet: {e}` | `storm_log.py` |
| DM | `⚔️ **{label} reminder** — your alliance is preparing for this week's {label}. Please confirm your participation in Discord and check the team channel for your zone assignment. Good luck out there!` *({label} = `Desert Storm` or `Canyon Storm`; both events use ⚔️ in the DM, not 🏜️)* | `storm_log.py` |
| Success | `✅ Sent {sent} **{label}** reminder DM{s}. {skipped} skipped.` | `storm_log.py` |

---

## 9. Survey

### 9.1 `/setup_survey`

#### 9.1.1 Command + entry

| Type | Copy | File |
|---|---|---|
| Description | `Configure the default survey — channels, tabs, intro, and questions` | `setup_cog.py` |
| Success | `⚙️ Starting survey setup — check the channel for prompts!` | `setup_cog.py` |
| Wizard prompt | `⚙️ **{wizard_label}**\nConfigure the survey for your alliance.` | `setup_cog.py` |

#### 9.1.2 Steps 1–4 (channels and tabs)

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 1 of 6 — Survey Channel**\nSelect the channel where the survey button will be posted for members to access:` | `setup_cog.py` |
| Select placeholder | `Select the survey channel...` | `setup_cog.py` |
| Wizard prompt | `**Step 2 of 6 — Survey Notification Channel**\nSelect the channel where leadership will be notified when a member submits the survey:` | `setup_cog.py` |
| Select placeholder | `Select the survey notification channel...` | `setup_cog.py` |
| Wizard prompt | `**Step 3 of 6 — Member Statistics Tab**\nWhich tab stores your members' statistics? We will update this sheet on each submission.\n⚠️ *Make sure this tab exists in your sheet before continuing.*` | `setup_cog.py` |
| Modal title | `Member Statistics Tab` | `setup_cog.py` |
| Wizard prompt | `**Step 4 of 6 — Survey History Tab**\nWhich tab stores the full history of all submissions?\n⚠️ *Make sure this tab exists in your sheet before continuing.*` | `setup_cog.py` |
| Modal title | `Survey History Tab` | `setup_cog.py` |

#### 9.1.3 Step 5 — Intro Message

| Type | Copy | File |
|---|---|---|
| Wizard prompt | (see "Survey intro prompt" below) | `setup_cog.py` |

```text
[Survey intro prompt]
**Step 5 of 6 — Survey Intro Message**
When your survey is posted, what introductory message do you want your members to see before they take the survey?

**Example:**
*Please fill out this survey each week to help us track squad powers, balance our teams, and prepare for season events!*
```

#### 9.1.4 Step 6 — Questions

| Type | Copy | File |
|---|---|---|
| Wizard prompt | (see "Survey questions intro" below) | `setup_cog.py` |
| Button | `✅ Use default questions` (success) | `setup_cog.py` |
| Button | `✏️ Edit existing questions` (primary) | `setup_cog.py` |
| Button | `🔄 Start from scratch` (secondary) | `setup_cog.py` |
| Success | `✅ Using default questions.` | `setup_cog.py` |
| Info | `✏️ Entering edit mode...` | `setup_cog.py` |
| Info | `🔄 Starting from scratch...` | `setup_cog.py` |
| Wizard prompt | `**Survey Questions:**\n{q_display}` | `setup_cog.py` |
| Select placeholder | `✏️ Edit a question...` | `setup_cog.py` |
| Select placeholder | `🗑️ Delete a question...` | `setup_cog.py` |
| Button | `➕ Add Question` (primary) | `setup_cog.py` |
| Button | `✅ Finish Survey Setup` (success) | `setup_cog.py` |
| Success | `🗑️ Removed: **{label}**` | `setup_cog.py` |
| Wizard prompt | `**{q_num} — Label**\nWhat is the label for this question? (e.g. \`1st Squad Power\`, \`Profession\`)` | `setup_cog.py` |
| Wizard prompt | `**{q_num} — Answer Type**\nPick how members answer this question.` | `setup_cog.py` |
| Select placeholder | `Select answer type...` | `setup_cog.py` |
| Select option | `Text — member types their answer` | `setup_cog.py` |
| Select option | `Dropdown — member selects from a list` | `setup_cog.py` |
| Select option | `Numeric — number, with shorthand support` | `setup_cog.py` |
| Select option | `💎 Multi-select — pick multiple options` | `setup_cog.py` |
| Select option | `💎 Date — formatted date entry` | `setup_cog.py` |
| Success | `✅ Type: **{Text\|Dropdown\|Numeric\|Multi-Select\|Date}**` | `setup_cog.py` |
| Wizard prompt | `**{q_num} — Help Text**\nDo you want to show help text for this question? This appears as a hint to help members answer correctly.\n*(e.g. \`e.g. 43.27\` or \`What is your first squad's power?\`)*\nType your help text, or type \`none\` to skip.` | `setup_cog.py` |
| Wizard prompt | `**{q_num} — Options**\nEnter the options as comma-separated values. Maximum of 25.\n*(e.g. \`Missile, Air, Tank\`)*` | `setup_cog.py` |
| Wizard prompt | `**{q_num} — Number Scale**\nHow big are these numbers typically? Picking a scale lets members type the natural shorthand (\`301\`) instead of the full value (\`304,743,912\`) — the bot accepts both either way.` | `setup_cog.py` |
| Select placeholder | `Select number scale...` | `setup_cog.py` |
| Select option | `Exact number — type what you mean (e.g. drone level 150 stays 150)` | `setup_cog.py` |
| Select option | `Thousands (K) — 5 becomes 5,000` | `setup_cog.py` |
| Select option | `Millions (M) — 301 becomes 301,000,000` | `setup_cog.py` |
| Select option | `Billions (B) — 1.2 becomes 1,200,000,000` | `setup_cog.py` |
| Success | `✅ Scale: **{Exact number\|Thousands (K)\|Millions (M)\|Billions (B)}**` | `setup_cog.py` |
| Wizard prompt (Premium) | `**{q_num} — Numeric Bounds** *(💎 Premium)*\nReply with \`min,max\` (e.g. \`0,100\`), \`min,\` for only a minimum, \`,max\` for only a maximum, or \`none\` to skip both bounds.\n*Bounds are checked against the stored value after scaling.*` | `setup_cog.py` |
| Note (Free) | `💎 *Min/max bounds are a Premium feature — this question will accept any number.*` | `setup_cog.py` |
| Warning | `⚠️ Couldn't parse bounds. Run \`/setup_survey\` to try again.` | `setup_cog.py` |
| Wizard prompt | `**{q_num} — Date Format** *(💎 Premium)*\nReply with a strptime-style format (e.g. \`%m/%d/%Y\`, \`%Y-%m-%d\`), or reply \`default\` for \`%m/%d/%Y\`.` | `setup_cog.py` |
| Success | `✅ Updated: **{label}**` | `setup_cog.py` |
| Success | `✅ Added: **{label}** — {n} question(s) so far.` | `setup_cog.py` |
| Warning | `⚠️ No questions defined. Run \`/setup_survey\` to try again.` | `setup_cog.py` |

```text
[Survey questions intro]
**Step 6 of 6 — Survey Questions**

**Default questions (Last War):**
{default_q_list}

**Your existing questions:**
{existing_q_list}

Would you like to use the defaults, edit your existing questions, or start from scratch?
```

#### 9.1.5 Save summary

| Type | Copy | File |
|---|---|---|
| Embed title | `✅ Survey Configured` \| `✅ Survey Configured — {name}` | `setup_cog.py` |
| Embed field | `Survey Channel` / `Notification Channel` / `Stats Tab` / `History Tab` / `Questions` | `setup_cog.py` |
| Embed footer | `Run {cmd} again to update. Run /survey_post to post the survey button.` | `setup_cog.py` |

#### 9.1.6 Premium: Add / Edit / Remove Survey

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `💎 **Add a Survey**\nType a short display name for the new survey (e.g. \`Off-Season Powers\` or \`Recruit Intake\`). This is what leadership and members will see.` | `setup_cog.py` |
| Timeout | `⏰ Timed out. Click **➕ Add Survey** on \`/survey\` again to retry.` | `setup_cog.py` |
| Warning | `⚠️ Empty name — aborting. Click **➕ Add Survey** on \`/survey\` to try again.` | `setup_cog.py` |
| Success | `✅ Creating new survey **{name}** (id: \`{id}\`).\nWalking you through the same setup steps as \`/setup_survey\`…` | `setup_cog.py` |
| Info | `*You have no extra surveys to remove.* Click **➕ Add Survey** on \`/survey\` to add one.` | `setup_cog.py` |
| Select placeholder | `Pick a survey to remove…` | `setup_cog.py` |
| Wizard prompt | `Pick which extra survey to remove:` | `setup_cog.py` |
| Wizard prompt | `⚠️ Confirm: remove **{name}**?` | `setup_cog.py` |
| Button | `🗑️ Remove` (danger) | `setup_cog.py` |
| Button | `❌ Cancel` (secondary) | `setup_cog.py` |
| Success | `🗑️ Removed **{name}**.` | `setup_cog.py` |
| Warning | `⚠️ Could not remove that survey.` | `setup_cog.py` |
| Cancel | `❌ Cancelled. No surveys removed.` | `setup_cog.py` |
| Select placeholder | `Pick a survey to edit…` | `setup_cog.py` |
| Wizard prompt | `Which survey would you like to edit?` | `setup_cog.py` |
| Info | `✏️ Editing **{name}**…` | `setup_cog.py` |

### 9.2 `/survey` — list view (free tier)

| Type | Copy | File |
|---|---|---|
| Description | `Show configured survey(s); Premium gets Add / Edit / Remove buttons here` | `survey.py` |
| Embed title | `📋 Survey Configuration` | `survey.py` |
| Embed desc (no questions) | `*No survey questions configured. Run \`/setup_survey\` to add some.*` | `survey.py` |
| Embed desc (per question, dropdown) | `**{i}. {q['label']}** *(dropdown: {options})*` | `survey.py` |
| Embed desc (per question, text) | `**{i}. {q['label']}** *(text)*` | `survey.py` |
| Embed desc (help line) | `   _{q['help']}_` | `survey.py` |
| Embed field | `Stats Tab` → `*not set*` *(when unset)* | `survey.py` |
| Embed field | `History Tab` → `*not set*` *(when unset)* | `survey.py` |
| Embed field | `Intro Message` → `✅ Configured` / `❌ Not configured` | `survey.py` |
| Embed footer | `Run /setup_survey to update. Run /survey_post to post the button.` | `survey.py` |

### 9.3 `/survey` — manage view (Premium)

| Type | Copy | File |
|---|---|---|
| Embed title | `📋 Configured Surveys` | `survey.py` |
| Embed desc | (see "Configured Surveys description" block below) | `survey.py` |
| Embed field | `{name}` or `{name} *(default)*` | `survey.py` |
| Embed field value | `**{n_q}** question(s) · Stats tab: \`{tab}\` · Channel: {ch_str}` | `survey.py` |
| Embed field value (channel fallback) | `_(uses default channel)_` | `survey.py` |
| Embed field value (tab fallback) | `*not set*` | `survey.py` |
| Embed footer | `Use /survey_post to publish the answer button. /survey_remind to send or schedule reminders.` | `survey.py` |
| Button | `➕ Add Survey` (success) | `survey.py` |
| Button | `✏️ Edit Survey` (primary) | `survey.py` |
| Button | `🗑️ Remove Survey` (danger) | `survey.py` |

```text
[Configured Surveys description]
💎 **Premium** — manage every survey from here.
Use the buttons below to **Add**, **Edit**, or **Remove** a survey.
```

### 9.4 `/survey_post`

| Type | Copy | File |
|---|---|---|
| Description | `Post (or repost) the survey button in its configured channel` | `survey.py` |
| Error | `⚙️ Bot not configured. Run \`/setup\` first.` | `survey.py` |
| Wizard prompt (multi-survey) | `📋 You have multiple surveys configured — which one do you want to post?` | `survey.py` |
| Timeout | `⏰ Picker timed out. Run \`/survey_post\` again.` | `survey.py` |
| Error | `⚠️ Could not find the survey channel for **{survey_name}**.` | `survey.py` |
| Channel post (default intro) | (see "Default survey intro" below) | `survey.py` |
| Button (success) | `📋 Answer` *(label on channel post)* | `survey.py` |
| Success | `✅ Survey button posted for **{survey_name}** in {channel.mention}.` | `survey.py` |

```text
[Default survey intro]
**Let us know your Squad Powers!**

Please fill out this survey each week, if possible, to help us keep track of squad powers, better balance our Desert Storm teams, track alliance growth, and prepare for season events!
```

### 9.5 Member-facing answer thread

#### 9.5.1 Answer button click — pre-thread

| Type | Copy | File |
|---|---|---|
| Error | `⚙️ This bot hasn't been set up yet.` | `survey.py` |
| Error | `⛔ You need the **{member_role_name}** role to fill out this survey.` | `survey.py` |
| Error | `⚠️ This survey is no longer configured. Ask leadership to repost it.` | `survey.py` |
| Thread message (initial) | `🚀 Let's get started! Your private thread is being created...` | `survey.py` |
| Error | `⚠️ Could not create your survey thread: {e}` | `survey.py` |
| Thread message | `🚀 Your thread is ready — head over here to get started: {thread.mention}` | `survey.py` |

#### 9.5.2 Question scaffolding (in private thread)

| Type | Copy | File |
|---|---|---|
| Warning | `⚠️ No survey questions configured. Ask leadership to run \`/setup_survey\`.` | `survey.py` |
| Question prompt (text) | `**{label}**` *(optionally with `\n*{placeholder}*` and `\n*Maximum characters: {max_chars}*`)* | `survey.py` |
| Question prompt (dropdown) | `**{label}**` | `survey.py` |
| Select placeholder (dropdown fallback) | `Select {label}...` | `survey.py` |
| Question prompt (numeric) | `**{label}**` *(optionally `\n*{placeholder}*`, then `\n*(min: X, max: Y)*`)* | `survey.py` |
| Numeric bounds suffix | `\n*(min: {min_val}, max: {max_val})*` *(or just `min:` / `max:` alone)* | `survey.py` |
| Question prompt (multi-select) | `**{label}**` | `survey.py` |
| Select placeholder (multi-select fallback) | `Select {label}…` | `survey.py` |
| Multi-select error | `⚠️ Question has no options configured. Please contact leadership.` | `survey.py` |
| Question prompt (date) | `**{label}**` *(optionally `\n*{placeholder}*`)* followed by `\n*(format: \`{date_format}\`)*` | `survey.py` |
| Selection echo (single dropdown) | `**{label}** {selected}` | `survey.py` |
| Selection echo (multi-select) | `**{label}** {comma-joined values}` | `survey.py` |

#### 9.5.3 Validation retries

| Type | Copy | File |
|---|---|---|
| Validation retry (text too long) | `⚠️ That entry is too long (max {max_chars} characters). Please re-enter your answer for this question.` | `survey.py` |
| Validation retry (numeric, NaN) | `⚠️ \`{raw}\` isn't a number. Please re-enter your answer for this question.` | `survey.py` |
| Validation retry (numeric, below min) | `⚠️ Must be at least **{min_val}**. Please re-enter your answer for this question.` | `survey.py` |
| Validation retry (numeric, above max) | `⚠️ Must be at most **{max_val}**. Please re-enter your answer for this question.` | `survey.py` |
| Validation retry (numeric/date exhausted) | (see "Numeric/date attempts exhausted" block below) | `survey.py` |
| Validation retry (date, parse) | `⚠️ \`{raw}\` doesn't match \`{date_format}\`. Please re-enter your answer for this question.` | `survey.py` |
| Timeout | `⏰ Survey timed out. You can start again by clicking the Answer button.` | `survey.py` |

```text
[Numeric/date attempts exhausted]
⚠️ Too many invalid attempts on this question. Cancelling the survey — click the Answer button to start over when you're ready.
```

#### 9.5.4 Save + finalize

| Type | Copy | File |
|---|---|---|
| Thread message | `⏳ Saving your responses...` | `survey.py` |
| Error | `⚠️ There was an error saving your responses: {e}\nPlease let leadership know.` | `survey.py` |
| Embed title | `✅ Survey Complete!` | `survey.py` |
| Embed field | `Thank you!` → (see "Survey complete thank-you" below) | `survey.py` |
| Embed footer | `This thread will be deleted in 60 seconds or you can close it now.` | `survey.py` |
| Button | `❌ Close Thread` (secondary) | `survey.py` |

```text
[Survey complete thank-you]
Your response has been saved successfully! Thanks for keeping your stats up to date, it helps us to balance teams, track alliance growth, and prepare for season events.
```

#### 9.5.5 Leadership notification embed

| Type | Copy | File |
|---|---|---|
| Embed title | `📋 New Survey Response` | `survey.py` |
| Embed field | `Member` → `{user.mention}` | `survey.py` |
| Embed field | `Submitted` → `{Month} {day}, {year} at {h}:{MM AM/PM} UTC` | `survey.py` |
| Embed field | `Responses` → `**{label}:** {value}` *(per question)* | `survey.py` |
| Embed field value (missing) | `—` | `survey.py` |
| Embed field value (no responses) | `*(no responses)*` | `survey.py` |

### 9.6 `/survey_remind` — Send now

#### 9.6.1 Hub

| Type | Copy | File |
|---|---|---|
| Description | `Send a survey reminder now or manage scheduled reminders` | `survey.py` |
| Wizard prompt (hub) | (see "Reminder hub prompt" below) | `survey.py` |
| Button | `📤 Send reminder now` (success) | `survey.py` |
| Button | `⚙️ Manage scheduled reminders` (primary) | `survey.py` |
| Button | `❌ Cancel` (secondary) | `survey.py` |
| Cancel | `Cancelled.` | `survey.py` |

```text
[Reminder hub prompt — Premium]
📋 **Survey Reminders**
What would you like to do?
*Tier: 💎 Premium*
```

```text
[Reminder hub prompt — Free]
📋 **Survey Reminders**
What would you like to do?
*Tier: Free*
```

#### 9.6.2 Send-now flow

| Type | Copy | File |
|---|---|---|
| Wizard prompt (multi-survey pick) | `📋 You have multiple surveys — which one are you reminding members about?` | `survey.py` |
| Select placeholder | `Pick a survey…` | `survey.py` |
| Selection echo | `✅ Survey: **{label}**` | `survey.py` |
| Timeout | `⏰ Picker timed out. Run \`/survey_remind\` again.` | `survey.py` |
| Wizard prompt (destination) | (see "Send-now destination prompt" below) | `survey.py` |
| Button | `📢 Post to a channel` (primary) | `survey.py` |
| Button | `📨 DM via Member Roster` (secondary) | `survey.py` |
| Button (gated) | `📨 DM via Member Roster (💎 Premium)` (secondary) | `survey.py` |
| Premium gate (inline) | `ℹ️ *DM-via-roster is Premium-only — \`/upgrade\` to unlock.*` | `survey.py` |
| Wizard prompt (channel pick) | `📢 Pick the channel to post to:` | `survey.py` |
| Select placeholder | `Pick a channel…` | `survey.py` |
| Selection echo | `✅ Channel: {channel.mention}` | `survey.py` |
| Success (channel) | `✅ Posted reminder for **{survey_name}** in {channel.mention}.` | `survey.py` |
| Error (channel post) | `⚠️ Could not post to that channel — make sure the bot has permission.` | `survey.py` |
| Error (DM, no roster) | `⚙️ DM reminders need Member Roster Sync. Run \`/setup_members\` first.` | `survey.py` |
| Success (DM) | `✅ Sent {sent} reminder DM{'s' if sent != 1 else ''} for **{survey_name}**. {skipped} skipped (DMs closed, missing ID, or other failures).` | `survey.py` |

```text
[Send-now destination prompt]
📋 Reminder for **{survey_name}** — where should it go?
ℹ️ *DM-via-roster is Premium-only — `/upgrade` to unlock.*
```
*(the second line is omitted on Premium)*

```text
[Default reminder body]
📋 **Friendly reminder** — your alliance is asking you to fill out **{name}** this week. Open the survey channel in Discord and click the **📋 Answer** button to get started. Thanks!
```

### 9.7 `/survey_remind` — Manage scheduled reminders

#### 9.7.1 Survey pick + current settings

| Type | Copy | File |
|---|---|---|
| Wizard prompt (survey pick) | `⚙️ Which survey are you scheduling reminders for?` | `survey.py` |
| Wizard prompt (current settings) | (see "Current schedule summary" below) | `survey.py` |

```text
[Current schedule summary]
⚙️ **Scheduling reminders for `{survey_name}`**
**Current schedule:** {Off | Daily at HH:MM | Weekly on {Day} at HH:MM}
**Current destination:** {DM via Member Roster | <#channel_id> | *(not set)*}
**Current message:** {*set* | *default*}
```

#### 9.7.2 Step 1 — Frequency

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 1 — Frequency**\nHow often should this reminder fire?` | `survey.py` |
| Button | `Off (disable)` (danger) | `survey.py` |
| Button | `Daily` (primary) | `survey.py` |
| Button | `Weekly` (success) | `survey.py` |
| Success (off) | `✅ Scheduled reminders disabled for **{survey_name}**. Run \`/survey_remind\` again to re-enable.` | `survey.py` |

#### 9.7.3 Step 2 — Day of week (weekly only)

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 2 — Day of the week**\nWhich day should the reminder fire each week?` | `survey.py` |
| Select placeholder | `Day of the week…` | `survey.py` |
| Select option | `Monday` / `Tuesday` / `Wednesday` / `Thursday` / `Friday` / `Saturday` / `Sunday` | `survey.py` |
| Selection echo | `✅ Day: **{day_name}**` | `survey.py` |

#### 9.7.4 Step 3 — Time of day

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 3 — Time of day**\nWhat time should the reminder fire? *(your guild's timezone)*` | `survey.py` |
| Button | `⏰ Set time (current: {default})` (primary) | `survey.py` |
| Modal title | `Reminder time` | `survey.py` |
| Input label | `Time (e.g. 9:00am, 22:30, 12:00pm)` | `survey.py` |
| Validation retry | `⚠️ Could not read **\`{raw}\`** as a time. Try \`9:00am\`, \`22:30\`, or \`12:00pm\`. Let's try once more.` | `survey.py` |
| Validation retry (exhausted) | `⚠️ Could not read that time after a few tries. Run \`/survey_remind\` to start over.` | `survey.py` |

#### 9.7.5 Step 4 — Destination

| Type | Copy | File |
|---|---|---|
| Wizard prompt | (see "Schedule destination prompt" below) | `survey.py` |
| Button | `📢 Post to a channel` (primary) | `survey.py` |
| Button | `📨 DM via Member Roster` (secondary) | `survey.py` |
| Button (gated) | `📨 DM via Member Roster (💎 Premium)` (secondary) | `survey.py` |
| Wizard prompt (channel pick) | `📢 Pick the channel to post the reminder to:` | `survey.py` |

```text
[Schedule destination prompt]
**Step 4 — Where to send the reminder**
ℹ️ *DM-via-roster is Premium-only.*
```
*(the second line is omitted on Premium)*

#### 9.7.6 Step 5 — Message body

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 5 — Reminder message**\nWhat should the reminder say? Leave blank to use the bot's default.` | `survey.py` |
| Button | `✏️ Edit message` (primary) | `survey.py` |
| Button | `Use default` (secondary) | `survey.py` |
| Selection echo (use default) | `✅ Will use the default reminder message.` | `survey.py` |
| Modal title | `Reminder message` | `survey.py` |
| Input label | `Reminder message body` | `survey.py` |
| Input placeholder | (see "Reminder body placeholder" below) | `survey.py` |

```text
[Reminder body placeholder]
📋 Reminder — please fill out the survey this week!
(Leave blank to use the bot's default message.)
```

#### 9.7.7 Save confirmation

```text
[Schedule saved confirmation]
✅ **{survey_name} reminders scheduled.**
**When:** {Daily at HH:MM | Weekly on {Day} at HH:MM} *(in your guild's timezone)*
**Where:** {DMs to every roster member | <#channel_id>}
**Message:** {*custom* | *default*}

Run `/survey_remind` again any time to update or disable.
```

---

## 10. Growth Tracking

### 10.1 `/setup_growth`

#### 10.1.1 Command + entry

| Type | Copy | File |
|---|---|---|
| Description | `Configure growth tracking — source tab, metrics, and snapshot frequency` | `setup_cog.py` |
| Success | `⚙️ Starting growth tracking setup — check the channel for prompts!` | `setup_cog.py` |
| Wizard prompt | `⚙️ **Growth Tracking Setup**\nConfigure how the bot tracks your alliance's growth over time. Each month (or on your chosen schedule), the bot takes a snapshot of your members' stats and records them in your Google Sheet so you can track progress.` | `setup_cog.py` |

#### 10.1.2 Step 1 — Enable

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 1 of 7 — Enable growth tracking?**\nShould the bot automatically take snapshots of your members' stats on a schedule?` | `setup_cog.py` |
| Success | `✅ Growth tracking disabled.` | `setup_cog.py` |
| Timeout | `⏰ Timed out. Run \`/setup_growth\` to start again.` | `setup_cog.py` |
| Cancel | `❌ Cancelled.` | `setup_cog.py` |

#### 10.1.3 Steps 2–4 (data source)

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 2 of 7 — Source Tab**\nWhich tab in your Google Sheet contains your member data?\n⚠️ *Make sure this tab exists in your sheet.*` | `setup_cog.py` |
| Modal title | `Source Tab` | `setup_cog.py` |
| Wizard prompt | `**Step 3 of 7 — Data Start Row**\nWhich row does your member data start on? (Row 1 is usually the header)` | `setup_cog.py` |
| Modal title | `Data Start Row` | `setup_cog.py` |
| Input label | `Row number` | `setup_cog.py` |
| Warning | `⚠️ Please enter a row number like \`2\`. Run \`/setup_growth\` to try again.` | `setup_cog.py` |
| Wizard prompt | `**Step 4 of 7 — Name Column**\nWhich column contains the member's name?` | `setup_cog.py` |
| Modal title | `Name Column` | `setup_cog.py` |
| Input label | `Column letter` | `setup_cog.py` |
| Warning | `⚠️ Please enter a single column letter like \`A\`. Run \`/setup_growth\` to try again.` | `setup_cog.py` |

#### 10.1.4 Step 5 — Metrics

| Type | Copy | File |
|---|---|---|
| Embed title | `📊 Step 5 of 7 — Metrics to Track` | `setup_cog.py` |
| Embed desc | `Define which columns the bot should snapshot each period. Add as many as you want — for example a \`1st Squad Power\` column, \`THP\`, \`Total Kills\`, etc.` | `setup_cog.py` |
| Embed field | `No metrics yet` → `Click **Add Metric** to begin.` | `setup_cog.py` |
| Embed footer | `Free tier: {n} of {cap} metrics used. Upgrade to Premium for unlimited.` | `setup_cog.py` |
| Modal title | `Metric` | `setup_cog.py` |
| Input label | `Label` | `setup_cog.py` |
| Input placeholder | `e.g. 1st Squad Power, THP, Total Kills` | `setup_cog.py` |
| Input label | `Column letter` | `setup_cog.py` |
| Input placeholder | `e.g. E` | `setup_cog.py` |
| Button | `➕ Add Metric` (success) | `setup_cog.py` |
| Button | `✏️ Edit Metric` (primary) | `setup_cog.py` |
| Button | `🗑️ Delete Metric` (danger) | `setup_cog.py` |
| Button | `✅ Done` (secondary) | `setup_cog.py` |
| Select placeholder | `Choose a metric...` | `setup_cog.py` |
| Wizard prompt | `Which metric do you want to {edit\|delete}?` | `setup_cog.py` |
| Success | `🗑️ Removed: **{label}** (column {col})` | `setup_cog.py` |
| Wizard prompt | `Editing **{label}** (column {col}). Click below to update.` | `setup_cog.py` |
| Button | `✏️ Edit values` (primary) | `setup_cog.py` |
| Warning | `⚠️ No metrics defined. Run \`/setup_growth\` to try again.` | `setup_cog.py` |

#### 10.1.5 Step 6 — Growth Tracking Tab

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 6 of 7 — Growth Tracking Tab**\nWhich tab should snapshots be written to?\n⚠️ *If the tab doesn't exist, the bot will create it automatically.*` | `setup_cog.py` |
| Modal title | `Growth Tracking Tab` | `setup_cog.py` |

#### 10.1.6 Step 7 — Snapshot Frequency

| Type | Copy | File |
|---|---|---|
| Wizard prompt | `**Step 7 of 7 — Snapshot Frequency**\nHow often should the bot take a snapshot?` | `setup_cog.py` |
| Premium gate | `\n*🔒 Custom interval is a Premium feature.*` | `setup_cog.py` |
| Button | `📅 Monthly (1st of each month)` (primary) | `setup_cog.py` |
| Button | `🔁 Custom interval (every X days) 💎` (secondary) | `setup_cog.py` |
| Success | `✅ Frequency: **Monthly**` | `setup_cog.py` |
| Wizard prompt | `**Step 7a of 7 — Snapshot Day**\nWhich day of the month should the snapshot run? (1–28)` | `setup_cog.py` |
| Modal title | `Snapshot Day` | `setup_cog.py` |
| Input label | `Day of month (1–28)` | `setup_cog.py` |
| Wizard prompt | `**Step 7a of 7 — Interval (days)**\nHow many days between each snapshot?` | `setup_cog.py` |
| Modal title | `Interval` | `setup_cog.py` |
| Input label | `Days between snapshots` | `setup_cog.py` |

#### 10.1.7 Save summary

| Type | Copy | File |
|---|---|---|
| Embed title | `✅ Growth Tracking Configured` | `setup_cog.py` |
| Embed field | `Source Tab` / `Name Column` / `Data Start Row` / `Growth Tab` / `Snapshot Schedule` / `Metrics` | `setup_cog.py` |
| Embed footer | `Run /setup_growth again to update. Use /growth to take a manual snapshot.` | `setup_cog.py` |

### 10.2 `/growth`

| Type | Copy | File |
|---|---|---|
| Description | `Show growth tracking status with options to run a snapshot or edit config` | `bot.py` |
| Embed title | `📈 Growth Tracking` | `bot.py` |
| Embed field | `Status` → `✅ Enabled` \| `❌ Disabled` | `bot.py` |
| Embed field | `Source Tab` → `{tab_source}` or `*not set*` | `bot.py` |
| Embed field | `Growth Tab` → `{tab_growth}` or `*not set*` | `bot.py` |
| Embed field | `Snapshot` → `Monthly on day {snapshot_day}` \| `Every {snapshot_interval} days` | `bot.py` |
| Embed field | `Metrics ({len(metrics)})` → `• **{m['label']}** — column {m['col']}` *(per metric)* or `*none configured*` | `bot.py` |
| Button | `📸 Run Snapshot Now` (success) | `bot.py` |
| Button | `⚙️ Edit Config` (primary) | `bot.py` |
| Success | `✅ Growth snapshot complete — check the **{tab_growth}** tab.` | `bot.py` |
| Error | `⚠️ Growth snapshot failed: {e}` | `bot.py` |
| Info | `Run \`/setup_growth\` to update the growth tracking configuration.` | `bot.py` |

> Note: `growth.py` itself is a backend module — no slash commands or
> embeds defined there. The `/growth` UI lives in `bot.py`.

---

## 11. Member Roster Sync 💎

### 11.1 `/setup_members`

| Type | Copy | File |
|---|---|---|
| Description | `💎 Configure Member Roster Sync (Premium)` | `member_roster.py` |
| Premium gate (feature_label) | `Member Roster Sync` | `member_roster.py` |
| Premium gate (description) | `Member Roster Sync is part of LW Alliance Helper Premium. Run \`/upgrade\` to unlock it.` | `member_roster.py` |
| Success (start) | `⚙️ Starting Member Roster Sync setup — check the channel for prompts.` | `member_roster.py` |
| Wizard intro | (see "Member Roster Sync intro" below) | `member_roster.py` |

```text
[Member Roster Sync intro]
💎 **Member Roster Sync Setup**
Configure how the bot writes your roster (Discord IDs + names) to a sheet tab. Other premium features look this up to send DMs and tag members.
```

#### 11.1.1 Step 1 — Roster Tab

| Type | Copy | File |
|---|---|---|
| Wizard prompt | (see "Step 1 — Roster Tab" below) | `member_roster.py` |
| Modal title | `Roster Tab Name` | `member_roster.py` |
| Input label | `Tab name` | `member_roster.py` |
| Default value | `Member Roster` | `member_roster.py` |

```text
[Step 1 — Roster Tab]
**Step 1 of 3 — Roster Tab**
Which tab should the roster be written to?
⚠️ *If the tab doesn't exist, the bot will create it automatically.*
⚠️ *The tab will be **completely overwritten** on each sync.*
```

#### 11.1.2 Step 2 — Filter by Member Role

| Type | Copy | File |
|---|---|---|
| Wizard prompt | (see "Step 2 — Filter by Member Role" below) | `member_roster.py` |
| Role label fallback | `the configured member role` | `member_roster.py` |
| Timeout | `⏰ Timed out. Run \`/setup_members\` to start again.` | `member_roster.py` |

```text
[Step 2 — Filter by Member Role]
**Step 2 of 3 — Filter by Member Role?**
Should the roster only include members who have {role_label}?
Pick **No** to include every (non-bot) member of the server.
```

#### 11.1.3 Step 3 — Auto-Sync

| Type | Copy | File |
|---|---|---|
| Wizard prompt | (see "Step 3 — Auto-Sync" below) | `member_roster.py` |
| Timeout | `⏰ Timed out. Run \`/setup_members\` to start again.` | `member_roster.py` |
| Error (initial sync failed) | `✅ Saved configuration but the initial sync failed: {e}\nTry running \`/sync_members\` once you've fixed the issue.` | `member_roster.py` |

```text
[Step 3 — Auto-Sync]
**Step 3 of 3 — Auto-Sync?**
Should the bot automatically re-sync when members join, leave, or change roles?
Pick **No** to only sync on `/sync_members`.
```

#### 11.1.4 Save summary

| Type | Copy | File |
|---|---|---|
| Embed title | `✅ Member Roster Sync Configured` | `member_roster.py` |
| Embed field | `Tab` → `{tab_name}` | `member_roster.py` |
| Embed field | `Role Filter` → `<@&{role_filter_id}>` or `All non-bots` | `member_roster.py` |
| Embed field | `Auto-Sync` → `Enabled` / `Disabled` | `member_roster.py` |
| Embed field | `Initial sync` → `**{count}** members written` | `member_roster.py` |
| Embed footer | `Run /sync_members to re-sync manually any time.` | `member_roster.py` |

### 11.2 `/sync_members`

| Type | Copy | File |
|---|---|---|
| Description | `💎 Manually rebuild the member roster sheet now` | `member_roster.py` |
| Premium gate (feature_label) | `Member Roster Sync` | `member_roster.py` |
| Premium gate (description) | `Member Roster Sync writes every member's Discord ID to your sheet so other Premium features (birthday DMs, train DMs, auto-mention, etc.) can find them. Run \`/upgrade\` to unlock it.` | `member_roster.py` |
| Error | `⚙️ Member Roster Sync isn't configured yet. Run \`/setup_members\` first.` | `member_roster.py` |
| Error | `⚠️ Sync failed: {e}\nMake sure the bot has access to your sheet and that the **{cfg['tab_name']}** tab can be written to.` | `member_roster.py` |
| Success | `✅ Synced **{count}** members to the **{cfg['tab_name']}** tab.` | `member_roster.py` |

### 11.3 Sheet column headers (written to roster tab)

| Type | Copy | File |
|---|---|---|
| Sheet header | `Discord ID` | `member_roster.py` |
| Sheet header | `Name` | `member_roster.py` |
| Sheet header | `Display Name` | `member_roster.py` |
| Sheet header | `Joined` | `member_roster.py` |
| Sheet header | `Roles` | `member_roster.py` |

---

## 12. Donate / Upgrade

### 12.1 `/donate`

| Type | Copy | File |
|---|---|---|
| Description | `Support the bot's hosting costs and development` | `donate.py` |
| Embed title | `💖 Support Alliance Helper` | `donate.py` |
| Embed desc | `If this bot has been useful to your alliance and you'd like to help cover hosting costs or just show appreciation, any support is hugely appreciated. Thank you!` | `donate.py` |
| Embed field | `Ways to Donate` → `{emoji} **[{name}]({url})**` *(one per line)* | `donate.py` |
| Embed field (no links) | `*(No donation links configured yet.)*` | `donate.py` |
| Embed footer | `100% optional — the bot is and will remain free to use at the base level.` | `donate.py` |
| Platform name | `Ko-fi` (emoji ☕) | `donate.py` |
| Platform name | `Buy Me a Coffee` (emoji 🥤) | `donate.py` |
| Platform name | `GitHub Sponsors` (emoji 💖) | `donate.py` |
| Platform name | `Patreon` (emoji 🎁) | `donate.py` |
| Platform name | `PayPal` (emoji 💵) | `donate.py` |

### 12.2 `/upgrade`

| Type | Copy | File |
|---|---|---|
| Description | `Unlock LW Alliance Helper Premium for this server` | `donate.py` |
| Embed title (already premium) | `💎 Premium is active` | `donate.py` |
| Embed desc (already premium) | `This server already has LW Alliance Helper Premium — you're set! All premium features are unlocked. Thanks for supporting the bot.` | `donate.py` |
| Embed title (upgrade pitch) | `💎 LW Alliance Helper Premium` | `donate.py` |
| Embed desc (upgrade pitch) | (see "Premium upgrade pitch" below) | `donate.py` |
| Embed field (subs unavailable) | `⚠️ Subscriptions not yet available` → `Premium subscriptions aren't live yet. Check back soon, or use \`/donate\` to support the bot in the meantime.` | `donate.py` |

```text
[Premium upgrade pitch]
Unlock the full power of Alliance Helper for your alliance.

**What you get:**
• 📣 Unlimited events (vs 5 free)
• 🚂 Up to 10 saved train prompt templates (vs 1 free)
• ⚔️ Up to 10 saved storm mail templates per team (vs 1 free)
• 📋 Multiple surveys + extra question types (multi-select, date) plus min/max bounds on numeric
• 📊 Custom snapshot intervals + unlimited tracked metrics
• 🧵 Use threads as destinations for any channel-pickable feature
• 👥 Member roster sync, birthday DMs, train DMs, survey reminders
• 📅 30-day history windows on `/events_log` and `/train_log`
• 📜 Unlimited storm-log lookback

**$4.99/month**, billed by Discord. Cancel anytime.
```

---

## 13. Premium gating messages

### 13.1 Limit reached (free-tier cap hit)

| Type | Copy | File |
|---|---|---|
| Embed title | `📊 Free tier limit reached` | `premium.py` |
| Embed desc | `You've used **{current} of {cap}** {plural_unit} on the free tier. Upgrade to 💎 LW Alliance Helper Premium to unlock more.` | `premium.py` |
| Embed field | `This limit applies to: {feature_label}` → `Premium subscribers get expanded limits, plus features like member roster sync, birthday DMs, and thread destinations. Run \`/upgrade\` to subscribe.` | `premium.py` |
| Brand | `💎 LW Alliance Helper Premium` | `premium.py` |

### 13.2 Premium-locked feature (entire feature gated)

| Type | Copy | File |
|---|---|---|
| Embed title | `🔒 {feature_label} is a Premium feature` | `premium.py` |
| Embed desc (default) | `This feature is part of 💎 LW Alliance Helper Premium. Run \`/upgrade\` to unlock it for your alliance.` | `premium.py` |

---

## 14. Wizard infrastructure

`dm.py` contains no user-facing strings — pure infrastructure: DM helper
functions that pass `content` / `embed` through from callers.

`wizard_registry.py` is mostly infrastructure (registry / cancel-event
plumbing, `safe_edit_response` fallback) but `expire_view_message` does
emit user-facing copy:

| Type | Copy | File |
|---|---|---|
| Timeout notice (appended to expired auto-post messages) | `\n\n⏰ *The actions for this have timed out. Use \`{command_hint}\` to re-initiate.*` | `wizard_registry.py` |
| Timeout notice (no command hint passed) | `\n\n⏰ *The actions for this have timed out.*` | `wizard_registry.py` |

The notice is rendered into context-specific copy at the callsites
catalogued under §5.3, §5.8, and §6.14 (event editor, approval view,
train reminder respectively).

---

*End of audit. Last regenerated: April 28, 2026.*
