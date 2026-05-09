# Alliance Helper

A Discord bot built for Last War alliance leadership. Alliance Helper takes care of the time-consuming coordination work — event announcements, train scheduling, Desert Storm and Canyon Storm mail generation, member stat tracking, and more — so your leadership team can stay focused on the game.

---

## What It Does

**📣 Event Announcements** — Schedule Plague Marauder, Zombie Siege, and any other recurring events. The bot posts a draft to leadership for review at your chosen time each event day, then sends the final announcement to your public channel once approved. A 5-minute warning fires automatically before the event starts.

**🚂 Train Schedule** — Track who gets the alliance train each day and generate a personalised ChatGPT prompt to help write a blurb for that member. Birthdays can be automatically added to the schedule in advance.

**🎂 Birthdays** — Read birthday data from your Google Sheet and optionally add members to the train schedule on their birthday, post birthday announcements in Discord, or both.

**⚔️ Desert Storm** — Generate ready-to-copy team mail drafts for Team A and Team B each week. Log sit-outs and participation data after each event.

**🏜️ Canyon Storm** — Same as Desert Storm — mail generation, team tracking, and participation logging.

**📋 Survey** — Let members submit their stats through a private Discord thread. Responses are saved directly to your Google Sheet and leadership gets a notification for each submission.

**📈 Growth Tracking** — Track your alliance's growth over time by taking periodic snapshots of any stats you choose — squad powers, THP, total kills, or anything else in your sheet. You define the metrics, the source, and the schedule.

---

## Your Data Stays With You

Alliance Helper is built around a simple principle: **your alliance's data lives in your own Google Sheet**, on the Google account *you* control. Power scores, growth snapshots, train history, participation logs, member rosters — all of it is written to the sheet *your alliance* shares with the bot. The bot helps to organize; you own the data.

- **You own the data.** The bot reads and writes; it doesn't keep its own copy of your alliance's data.
- **Use other tools alongside it.** Your Sheet is just a Google Sheet. Edit it directly, point another tool at it, or export it — the bot doesn't care.
- **Switch leadership without losing anything.** Hand the Sheet off when leadership changes. Every record from every storm, train, and survey comes with it.
- **Premium adds features, your alliance data lives in your own Google Sheet.** Subscribing unlocks DMs, scheduled reminders, and roster sync — nothing about where your data lives changes.

The bot's own database stores only what it needs to do its job — wizard answers, channel/role IDs, schedule settings, draft state, premium status. The alliance data itself stays in your Sheet.

---

## Free vs Premium

Alliance Helper is **free to use at the base level** for every alliance — every feature listed above works on the free tier with sensible limits that fit a typical alliance. **Premium** ($4.99/month, billed via Discord App Subscriptions) lifts the limits and unlocks a handful of features built around member identity (DMs, mentions, roster sync).

**Free tier limits**

| Feature | Free | Premium |
|---|---|---|
| Configured events | **5 total** | Unlimited |
| Train prompt templates | **1** *(named "Default")* | Up to **10** named templates |
| Storm mail templates per team | **1** *(named "Default")* | Up to **10** named templates |
| Survey questions | **5 per survey** | Unlimited |
| Survey question types | Text, Dropdown | + Numeric (with min/max), Multi-select, Date |
| Surveys per server | **1** | Multiple named surveys |
| Participation log questions | **3 per event type** | Unlimited |
| Participation log question types | Text, Yes/No, Numeric, Roster names | + Single-select, Multi-select, Date |
| Survey reminder destination | Channel post | Channel post **and** DM-via-roster |
| Scheduled survey reminders | ✅ Daily / Weekly via channel post | ✅ Daily / Weekly via channel post **or** DM-via-roster |
| Train themes / tones | **3 each** | Unlimited |
| Tracked growth metrics | **5** | Unlimited |
| Growth snapshot frequency | Monthly | Monthly **or** custom interval (every N days) |
| `/events_log` window | **7 days** | **30 days** |
| `/train_log` window | **7 days** | **30 days** |
| Storm participation log lookback | **4 most-recent entries** | Unlimited |
| Channel destinations | Text channels | Text channels **and** threads |

**Premium-only features** *(no free-tier equivalent)*

| Feature | What it does |
|---|---|
| **Member Roster Sync** | Writes every member's Discord ID + name to a sheet tab so other premium features can DM them. `/setup_members`, `/sync_members` |
| **Birthday DMs** | DM each member a personal happy-birthday note when their day fires |
| **Train assignment DMs** | DM the assigned member when their train day starts |
| **Customisable DM bodies** | Each Premium DM (birthday, train assignment, storm reminder) ships with a sensible default; alliances can override the body via the relevant `/setup_*` wizard's final step |
| **Auto-mention in train reminders** | Replace plain names with `<@id>` Discord mentions in the daily reminder |
| **DM-via-roster for `/survey_remind`** | Send the reminder as a DM to every roster member (free tier posts to a channel instead) |
| **`/desertstorm_remind`** / **`/canyonstorm_remind`** | DM every roster member a participation reminder before each storm |

**How to subscribe**

Run **`/upgrade`** in the leadership channel of the alliance you want Premium in. Discord handles billing; the bot pins your subscription to that server automatically once checkout completes.

A subscription is **per-user** and applies to **one server at a time** — if you're in multiple alliance Discords, your $4.99/mo unlocks Premium in just the one you've assigned it to. Use `/premium_assign` from a different alliance to move it, `/premium_status` to see where it's currently active, or `/premium_unassign` to release the pin without canceling the subscription. Cancellations through Discord deactivate premium features at the end of the billing cycle; your saved data (and your assignment) is **kept** so you can resume any time.

If you'd like to support the bot without subscribing, **`/donate`** shows tip-jar links — 100% optional.

> **Existing alliances:** none of your existing setup or saved data is affected when Premium goes live. See [`docs/MIGRATION.md`](docs/MIGRATION.md) for the full picture. Bot operators setting up the SKU should follow [`docs/PREMIUM_SETUP.md`](docs/PREMIUM_SETUP.md).

---

## Before You Start

You'll need:

- **A Discord server** where you have Administrator permissions
- **A Google Sheet** — this is where all your data lives. One sheet per alliance, shared with the bot's service account (details below)

That's it. No coding, no Google Cloud setup — just a sheet and a Discord server.

---

## Inviting the Bot

Use the invite link provided by your bot administrator to add Alliance Helper to your server. When prompted, select your server from the dropdown and click **Authorise**.

Once the bot has joined, you'll see it appear in your member list. It won't do anything until you run `/setup`.

---

## Setting Up Your Google Sheet

Before running `/setup`, create a new Google Sheet. You don't need to add any tabs or columns yet — the bot will tell you what to create as you go through each feature's setup.

The one thing you need to do upfront is **share your sheet** with the bot's service account so it can read and write data. You'll be prompted to do this during `/setup` with a direct link to your sheet's sharing settings and the exact email address to use.

Set the permission to **Editor** when sharing.

> **Keep your Sheet ID handy.** You can find it in your sheet's URL:
> `https://docs.google.com/spreadsheets/d/`**`YOUR_SHEET_ID_HERE`**`/edit`

---

## Core Setup

Run `/setup` in your leadership channel to get started. This covers the essentials that every feature depends on.

**What it asks for:**

1. **Member role** — the role all alliance members have. Used to gate the survey.
2. **Leadership role** — the elevated role for alliance leadership. Required to use most commands.
3. **Leadership channel** — the private channel where commands are used and drafts appear.
4. **Timezone** — your alliance's local timezone. Used for all event times, reminders, and Desert Storm/Canyon Storm time displays throughout the bot.
5. **Google Sheet ID** — paste the ID from your sheet's URL.
6. **Sheet sharing** — a guided step to share your sheet with the bot's service account.

Once complete, the bot will list all the available feature setup commands so you know what to configure next.

> **Tip:** You can run `/setup` again at any time to update any of these settings.

---

## Feature Setup

Each feature is configured independently. Set up only what you need — features you don't configure simply won't be active.

---

### 📣 Event Announcements — `/setup_events`

**What to create in your sheet:** Nothing required for this feature.

Run `/setup_events` to configure your events. The wizard first asks for settings that apply to all events:

- **Draft channel** — where leadership sees the draft before it goes public
- **Announcement channel** — where the final approved announcement posts
- **Draft posting time** — when the draft is posted each event day
- **5-minute warning** — whether the bot auto-posts a warning before events start

You'll then see your event list with options to add, edit, or remove events. For each event you add:

- **Event name** (e.g. `Plague Marauder (AE)`, `Zombie Siege`)
- **Default time** — when this event usually starts, in your timezone
- **Schedule** — repeating cycle (with anchor date and interval) or manual
- **Announcement blurb** — the message posted when this event fires, using `{time}` and `{server_time}` as placeholders

> **Example blurb:** `Plague Marauder (AE) at {time} ({server_time} Server Time). Make sure to have offline participation checked!`

---

### 🚂 Train Schedule — `/setup_train`

**What to create in your sheet:** A tab for your train schedule (e.g. `Train Schedule`).

Run `/setup_train` to configure the train schedule. The wizard walks through 8 steps:

1. **Schedule tab** — which tab in your sheet stores the train schedule
2. **Blurb generation** — whether you want the bot to generate a ChatGPT prompt each day. If no, steps 3–6 are skipped.
3. **Themes** — a list of themes leadership can choose from (e.g. `Birthday, Milestone, Welcome`)
4. **Tones** — a list of tone options (e.g. `Default, More casual, More intense`)
5. **Default tone** — which tone is pre-selected
6. **Prompt templates** — saved ChatGPT prompts using `{name}`, `{theme}`, `{tone}`, and `{notes}` as placeholders. Free tier keeps a single "Default" template; 💎 Premium can save up to 10 named templates and pick which is the default.
7. **Reminders** — whether the bot should post a reminder when someone is assigned the train, and if so, which channel and what time
8. **💎 Train DM Body** *(Premium)* — the body of the DM sent to the assigned member each day. Has a sensible default; override here if you want different copy.

**Day-to-day use:**
- Use `/train` to manage the schedule — buttons for **Add**, **Update**, **Generate Prompt**, and **Clear**
- At your configured reminder time, the bot posts a reminder in your chosen channel. If blurb generation is enabled, a button lets you pull up the ChatGPT prompt instantly
- Use `/train_log [date]` to look up past prompt log entries
- Birthdays auto-populate the schedule once per day (after server-time midnight). Use `/train_addbirthdays` to trigger the check on demand if you need a birthday added sooner.

---

### 🎂 Birthdays — `/setup_birthdays`

**What to create in your sheet:** A tab containing your member roster with name and birthday columns (e.g. `Birthdays`, or your existing member tab).

Run `/setup_birthdays` to configure birthday tracking. The wizard walks through 9 steps:

1. **Enable birthday tracking** — opt in or skip the feature entirely
2. **Sheet tab** — which tab contains birthday data
3. **Name column** — the column letter containing member names (e.g. `A`)
4. **Birthday column** — the column letter containing birthdays (e.g. `B`)
5. **Train integration** — whether birthdays are automatically added to the train schedule. If no, steps 6–7 are skipped.
6. **Birthday placement** — exact birthday only, or allow 1 day before/after if the birthday is taken
7. **Train schedule lookahead** — how many days in advance to look ahead (default `14`)
8. **Birthday reminders** — whether the bot posts a birthday message in Discord, and if so, which channel and what time
9. **💎 Birthday DM Body** *(Premium)* — the personal happy-birthday DM each member receives. Requires Premium + Member Roster Sync + a Discord ID column in your birthday sheet.

Birthday messages say: *🎂 Today is **[name]**'s birthday!*

**Day-to-day use:**
- Use `/birthdays` to see upcoming birthdays in the next 14 days
- Birthdays auto-populate the train schedule once per day, just after server-time midnight. Use `/train_addbirthdays` to trigger the check on demand if you need a birthday added sooner.

---

### ⚔️ Desert Storm — `/setup_desertstorm`

**What to create in your sheet:** A tab for Desert Storm assignments (e.g. `DS Assignments`).

Run `/setup_desertstorm` to configure Desert Storm. The wizard walks through 7 steps:

1. **Sheet tab** — the bot manages the data structure here automatically, no formatting needed
2. **Teams** — whether you run Team A & B, Team A only, or Team B only
3. **Log channel** — where participation log summaries are posted after each event
4. **Post channel** — where the finished mail is posted when leadership clicks **Post & Copy** at the end of `/desertstorm_draft`
5. **Mail template** — if you run both teams, choose one template for both or separate templates per team. A default template is provided — use it as-is or paste your own
6. **Participation tracking** *(optional)* — if you want to log who showed up / sat out / etc each event, opt in here and define the questions yourself. Sub-steps:
   - Tab to write rows to (default `DS Participation Log`)
   - Roster source: which sheet tab + which column has the member name + (optional) alias column for short forms / in-game tags + which row the data starts on
   - Questions builder: free tier supports 3 questions across `Text`, `Yes/No`, `Numeric`, and `Roster names` types. 💎 Premium unlocks unlimited questions plus `Single-select`, `Multi-select`, and `Date` types.
7. **💎 Reminder DM Body** *(Premium)* — the body of the participation-reminder DM sent by `/desertstorm_remind`. Has a sensible default; override here if you want different copy.

**Available placeholders in your template:**
- `{alliance_name}` — your alliance name
- `{zones}` — zone assignments block
- `{subs}` — substitute members
- `{time}` — event time (auto-filled when drafting)

**Day-to-day use:**
- Use `/desertstorm` to view current rosters and the configured mail template
- Use `/desertstorm_draft` to generate a mail draft. The flow is **Pick Team → Pick Time → Mail Template (Use as-is or Edit) → Preview**. Editing pastes the assignment block back to the bot; the parsed assignments are saved as next week's default but the mail itself is **not** posted yet
- At the preview step, click **✅ Looks Good — Post & Copy** to post the mail to your configured post channel and get a copyable code block back in leadership
- After the event, use `/desertstorm_participation` to walk through your configured participation questions. The flow always asks for the date first, then steps through each question you defined in setup. The summary auto-posts to your log channel.
- Use `/desertstorm_log [date]` to look up past log entries

---

### 🏜️ Canyon Storm — `/setup_canyonstorm`

**What to create in your sheet:** A tab for Canyon Storm assignments (e.g. `CS Assignments`).

Run `/setup_canyonstorm` — the setup is identical to Desert Storm above (7 steps including Post Channel, optional participation tracking, and an optional 💎 Premium reminder DM body). Canyon Storm event times vary by server, so the bot doesn't bake in a fixed schedule — your alliance's storm times come from your own calendar.

**Day-to-day use:**
- `/canyonstorm` — view current rosters and the configured mail template
- `/canyonstorm_draft` — generate a Canyon Storm mail draft (Team → Time → Template → Preview, then Post & Copy)
- `/canyonstorm_participation` — walk through the configured participation questions for this week
- `/canyonstorm_log [date]` — look up past log entries

---

### 📋 Survey — `/setup_survey`

**What to create in your sheet:** Two tabs — one for current member stats (e.g. `Squad Powers`) and one for submission history (e.g. `Survey History`).

Run `/setup_survey` to configure the **default** survey:

1. **Survey channel** — where the survey button is posted for members to access
2. **Notification channel** — where leadership is notified when a member submits
3. **Stats tab** — updated with each submission (one row per member)
4. **History tab** — a timestamped record of every submission
5. **Intro message** — the message members see before starting the survey
6. **Questions** — choose from the default Last War question set, edit individual questions, or build your own from scratch

The question builder supports two question types on the free tier:
- **Text** — the member types a value, with an optional help text hint
- **Dropdown** — the member picks from a list of options you define (up to 25)

**💎 Premium adds three more question types:** Numeric (with min/max validation and re-prompt-on-bad-input), Multi-select (pick multiple options), and Date (formatted entry with `strptime` validation).

**💎 Multi-survey (Premium):** alliances can configure more than one survey, each with its own questions, channel, intro, notification target, and reminder body. **Manage everything from `/survey`** — when Premium is active, the command renders a list of every configured survey along with **Add / Edit / Remove** buttons. Adding asks for a display name and routes through the same setup wizard; editing covers both the default survey and any extras; remove deletes an extra (the default can't be removed).

**Day-to-day use:**
- Run `/survey_post` to post the answer button. Premium guilds with multiple surveys are prompted to pick which survey to post.
- Members click **📋 Answer**, complete the survey in a private thread (named after the survey), and their data is saved automatically
- Leadership sees a notification embed in the notification channel for each submission
- Use `/survey` to see the configured survey — automatically switches to a list view when Premium has multiple
- Use `/survey_remind` to **send a reminder now** or **manage scheduled reminders**. The wizard walks you through picking which survey, picking the destination (channel post for free, DM-via-roster for 💎 Premium), and writing the message. Scheduled reminders fire automatically on a daily or weekly cadence in your guild's timezone.

---

### 📈 Growth Tracking — `/setup_growth`

**What to create in your sheet:** A tab for your member roster (can be the same tab used by the survey, or any other tab), and a separate tab where snapshots will be written (e.g. `Growth Tracking`). The bot will create the growth tab automatically if it doesn't exist.

Run `/setup_growth` to configure growth tracking:

1. **Enable** — opt in to growth tracking
2. **Source tab** — which tab contains your member data
3. **Data start row** — which row your data starts on (usually row 2, after a header)
4. **Name column** — the column letter containing member names
5. **Metrics** — which columns to snapshot, each with a label and column letter. You can track as many as you want (e.g. `1st Squad Power` → column E, `THP` → column I)
6. **Growth tab** — where snapshots are written. Created automatically if it doesn't exist.
7. **Snapshot schedule** — monthly on a specific day, or every X days

The growth tab will have a column per metric per snapshot period, so you can see how each metric changed over time.

**Day-to-day use:**
- Snapshots run automatically on your configured schedule
- Use `/growth` to open an action menu — run a snapshot manually, view the current configuration, or jump back into `/setup_growth`

---

## Day-to-Day Quick Reference

For the full list of every slash command and what it does, run `/help` in your leadership channel or check the [Commands reference](https://lw-alliance-helper.github.io/commands.html) on the docs site.

| Situation | Command |
|---|---|
| Post or repost the survey button | `/survey_post` |
| View configured survey(s) (list view when multiple) | `/survey` |
| 💎 Manage multiple surveys (Premium) | `/survey` → Add / Edit / Remove buttons |
| Manage the train schedule (add, update, generate prompt, clear) | `/train` |
| Add upcoming birthdays to the train schedule | `/train_addbirthdays` |
| See upcoming birthdays | `/birthdays` |
| Open the event editor | `/events` |
| Look up recently fired events | `/events_log` |
| Generate a Desert Storm mail draft | `/desertstorm_draft` |
| Generate a Canyon Storm mail draft | `/canyonstorm_draft` |
| Log Desert Storm participation | `/desertstorm_participation` |
| Log Canyon Storm participation | `/canyonstorm_participation` |
| Look up a past Desert Storm log | `/desertstorm_log [date]` |
| Look up a past Canyon Storm log | `/canyonstorm_log [date]` |
| Run a growth snapshot manually | `/growth` |
| View all configured settings | `/view_configuration` |
| Cancel an active setup wizard | `/cancel` |
| See all commands | `/help` |
| 💎 Sync member roster (Premium) | `/sync_members` |
| 💎 DM survey reminder to all members (Premium) | `/survey_remind` |
| 💎 DM storm reminder (Premium) | `/desertstorm_remind` / `/canyonstorm_remind` |
| Unlock Premium for your server | `/upgrade` |
| Show donation links (optional) | `/donate` |

---

## Troubleshooting

**Commands aren't showing up in Discord**
Slash commands can take up to an hour to appear after the bot first joins your server. If they still aren't showing after that, try removing and re-inviting the bot.

**"You don't have permission to use this command"**
Most feature commands need to be run by someone with the leadership role configured during `/setup`. The various `/setup_*` commands also accept anyone with **Administrator** server permissions, so a server owner can configure a feature even without holding the leadership role.

**"This bot hasn't been set up yet"**
Run `/setup` first. The bot won't respond to feature commands until core setup is complete.

**"Permission error" when the bot tries to access your sheet**
The bot's service account doesn't have access to your sheet. Go to your sheet's sharing settings and make sure the service account email has been added as an **Editor**. You can find the email address by running `/setup` and checking Step 6.

**A button stopped working after a bot restart**
Discord buttons lose their connection to the bot when it restarts. Use the corresponding command to start a fresh flow:

| Button | Use instead |
|---|---|
| Event editor or approval | `/events` |
| Train reminder prompt button | `/train` |
| Desert Storm approval | `/desertstorm_draft` |
| Canyon Storm approval | `/canyonstorm_draft` |
| DS/CS log steps | `/desertstorm_participation` or `/canyonstorm_participation` |
| Survey button | `/survey_post` |

**A setup wizard is stuck or you want to start over**
Run `/cancel` to abort any active setup wizard, then re-run the relevant `/setup_*` command.

**Something else isn't working**
Use `/help` to see all available commands and make sure the relevant feature has been configured with its `/setup_*` command. Run `/view_configuration` at any time to see the current settings for every wizard in one place.
