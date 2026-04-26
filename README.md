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

Run `/setup_train` to configure the train schedule:

1. **Schedule tab** — which tab in your sheet stores the train schedule
2. **Blurb generation** — whether you want the bot to generate a ChatGPT prompt each day. If yes:
   - **Themes** — a list of themes leadership can choose from (e.g. `Birthday, Milestone, Welcome`)
   - **Tones** — a list of tone options (e.g. `Default, More casual, More intense`)
   - **Default tone** — which tone is pre-selected
   - **Prompt template** — the full prompt you'd give ChatGPT, using `{name}`, `{theme}`, `{tone}`, and `{notes}` as placeholders
3. **Reminders** — whether the bot should post a reminder when someone is assigned the train, and if so, which channel and what time

**Day-to-day use:**
- Use `/schedule_set` to add upcoming train entries
- Use `/schedule` to view the current schedule
- At your configured reminder time, the bot posts a reminder in your chosen channel. If blurb generation is enabled, a button lets you pull up the ChatGPT prompt instantly
- Use `/trainprompt [date]` as a manual fallback at any time

---

### 🎂 Birthdays — `/setup_birthdays`

**What to create in your sheet:** A tab containing your member roster with name and birthday columns (e.g. `Birthdays`, or your existing member tab).

Run `/setup_birthdays` to configure birthday tracking:

1. **Sheet tab** — which tab contains birthday data
2. **Name column** — the column letter containing member names (e.g. `A`)
3. **Birthday column** — the column letter containing birthdays (e.g. `B`)
4. **Discord ID column** *(optional)* — if you store Discord IDs, the bot can @mention members in birthday announcements
5. **Train integration** — whether birthdays are automatically added to the train schedule
   - If yes: choose whether to place on the exact birthday only, or allow 1 day before/after if the birthday is taken
   - If yes: how many days in advance to look ahead (we recommend 14)
6. **Birthday reminders** — whether the bot posts a birthday message in Discord
   - If yes: which channel and what time

Birthday messages say: *🎂 Today is **[name]**'s birthday!*

---

### ⚔️ Desert Storm — `/setup_desertstorm`

**What to create in your sheet:** A tab for Desert Storm assignments (e.g. `DS Assignments`).

Run `/setup_desertstorm` to configure Desert Storm:

1. **Sheet tab** — the bot manages the data structure here automatically, no formatting needed
2. **Teams** — whether you run Team A & B, Team A only, or Team B only
3. **Log channel** — where participation logs are posted after each event
4. **Mail template** — if you run both teams, choose one template for both or separate templates per team. A default template is provided — use it as-is or paste your own

**Available placeholders in your template:**
- `{alliance_name}` — your alliance name
- `{zones}` — zone assignments block
- `{subs}` — substitute members
- `{time}` — event time (auto-filled when drafting)

**Day-to-day use:**
- Use `/draftds` to generate a mail draft. You'll select the team and time slot (Desert Storm runs at 18:00 and 23:00 Server Time, displayed in your timezone)
- Review the pre-filled draft, edit as needed, then approve to save and get a copyable mail block
- After the event, use `/logds` to record participation data
- Use `/viewlog DS [date]` to look up past log entries

---

### 🏜️ Canyon Storm — `/setup_canyonstorm`

**What to create in your sheet:** A tab for Canyon Storm assignments (e.g. `CS Assignments`).

Run `/setup_canyonstorm` — the setup is identical to Desert Storm above.

Canyon Storm runs at 12:00 and 23:00 Server Time (displayed in your timezone).

**Day-to-day use:**
- `/draftcs` — generate a Canyon Storm mail draft
- `/logcs` — log participation after the event
- `/viewlog CS [date]` — look up past log entries

---

### 📋 Survey — `/setup_survey`

**What to create in your sheet:** Two tabs — one for current member stats (e.g. `Squad Powers`) and one for submission history (e.g. `Survey History`).

Run `/setup_survey` to configure the survey:

1. **Survey channel** — where the survey button is posted for members to access
2. **Notification channel** — where leadership is notified when a member submits
3. **Stats tab** — updated with each submission (one row per member)
4. **History tab** — a timestamped record of every submission
5. **Intro message** — the message members see before starting the survey
6. **Questions** — choose from the default Last War question set, edit individual questions, or build your own from scratch

The question builder supports two question types:
- **Text** — the member types a value, with an optional help text hint
- **Dropdown** — the member picks from a list of options you define (up to 25)

**Day-to-day use:**
- Run `/postsurvey` to post the survey button in your survey channel
- Members click **Answer**, complete the survey in a private thread, and their data is saved automatically
- Leadership sees a notification embed in the notification channel for each submission

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
- Use `/rungrowth` to take a manual snapshot at any time

---

## Day-to-Day Quick Reference

| Situation | Command |
|---|---|
| Post or repost the survey button | `/postsurvey` |
| View the train schedule | `/schedule` |
| Add entries to the train schedule | `/schedule_set` |
| Get a ChatGPT prompt for today's train | `/trainprompt` |
| Update the active member tab for a new season | `/setmembertab [tab name]` |
| Manually run the birthday check | `/checkbirthdays` |
| Open the event editor | `/events` |
| Generate a Desert Storm mail draft | `/draftds` |
| Generate a Canyon Storm mail draft | `/draftcs` |
| Log Desert Storm participation | `/logds` |
| Log Canyon Storm participation | `/logcs` |
| Look up a past log entry | `/viewlog [DS or CS] [date]` |
| Run a growth snapshot manually | `/rungrowth` |
| Configure growth tracking | `/setup_growth` |
| See all commands | `/help` |

---

## Troubleshooting

**Commands aren't showing up in Discord**
Slash commands can take up to an hour to appear after the bot first joins your server. If they still aren't showing after that, try removing and re-inviting the bot.

**"You don't have permission to use this command"**
Make sure you're using the command in the leadership channel and that you have the leadership role configured during `/setup`. Both are required.

**"This bot hasn't been set up yet"**
Run `/setup` first. The bot won't respond to feature commands until core setup is complete.

**"Permission error" when the bot tries to access your sheet**
The bot's service account doesn't have access to your sheet. Go to your sheet's sharing settings and make sure the service account email has been added as an **Editor**. You can find the email address by running `/setup` and checking Step 6.

**A button stopped working after a bot restart**
Discord buttons lose their connection to the bot when it restarts. Use the corresponding command to start a fresh flow:

| Button | Use instead |
|---|---|
| Event editor or approval | `/events` |
| Train reminder prompt button | `/trainprompt` |
| Desert Storm approval | `/draftds` |
| Canyon Storm approval | `/draftcs` |
| DS/CS log steps | `/logds` or `/logcs` |
| Survey button | `/postsurvey` |

**Something else isn't working**
Use `/help` to see all available commands and make sure the relevant feature has been configured with its `/setup_*` command.
