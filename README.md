# OGV Alliance Bot

A Discord bot built for the OGV alliance that automates member data tracking, event announcements, train schedule management, Desert Storm and Canyon Storm operations, and monthly growth reporting.

All commands are restricted to members with the **OGV** role. Most commands are further restricted to the leadership channel or leadership category. Use `/help` at any time to see the full command list in Discord.

---

## What It Does

### 📋 Squad Powers Survey
Members submit their stats directly through the bot. A persistent **Answer** button lives in the survey channel — clicking it opens a private thread that walks the member through all questions (squad powers, squad type, drone level, gorilla level, THP, total kills, and profession). On completion:
- The member's row in the **Squad Powers** sheet is updated (or a new row is created)
- A timestamped entry is appended to the **Survey History** sheet for trend tracking
- A notification embed is posted to the leadership responses channel showing the member's name (clickable mention) and all their submitted values
- The private thread is deleted after 60 seconds (or immediately if the member clicks Close Thread)

### 📣 Event Announcements
On event days (Plague Marauder (AE) and Zombie Siege run on a 3-day cycle), the bot posts a draft announcement to the leadership channel at noon for review. Leadership uses the event editor to adjust times, add or remove optional events (Glacieradon, Blimp), and add notes. Once ready, the announcement goes through an approval flow before posting publicly. A 5-minute warning fires automatically after approval.

On Friday nights, a Buster Day shield reminder is posted to leadership for approval at 9:55pm ET.

If a button stops working after a bot restart, use `/events` to reopen a fresh editor.

### 🚂 Train Schedule
Leadership inputs the upcoming train schedule using `/schedule_set`. At 10pm ET (server reset), if someone is scheduled the bot posts a reminder with a button to pull up the ChatGPT prompt. `/trainprompt` is always available as a manual fallback.

Birthdays are automatically added to the train schedule 14 days in advance. The birthday theme is pre-filled automatically. Placement logic:
1. Schedule on the birthday if free
2. Try the day before if the birthday is taken
3. Try the day after if both are taken
4. If all three dates are occupied, post a high-priority alert to leadership and skip — do not override

If two members share a birthday, the second one independently follows the same placement logic, naturally landing on the day before or after.

### ⚔️ Desert Storm & Canyon Storm Mails
Leadership generates ready-to-copy event mails using `/draftds` and `/draftcs`. Each command:
1. Asks which team (A or B)
2. Posts the pre-filled template from last week's saved assignments
3. Accepts an edited paste-back
4. Shows a preview for approval
5. On approval, saves the new assignments and posts a copyable mail block

If a button stops working after a bot restart, re-run the draft command to start fresh.

### 📊 Storm Logging
After each DS or CS event, leadership logs participation data using `/logds` or `/logcs`. The flow walks through each data point with name entry via modal (validated against the member roster) and saves to the **DS-CS Sit-outs** sheet. A summary is posted to the dedicated log thread.

Members with special characters in their names (e.g. `ʟᴀɴᴅɛʀʂლ`) can be entered using a plain-text alias stored in the member sheet (col F).

Previous sit-out lists are automatically surfaced in step 5 to make it easy to flag members who sat out again.

Use `/viewlog` to look up any past log entry by event type and date.

### 📈 Growth Tracking
On the 1st of every month at 10pm ET, the bot snapshots every member's combined squad power and writes it to the **Growth Tracking** sheet with a percentage growth column. A startup snapshot also runs each time the bot launches for a baseline.

---

## Slash Commands

### Squad Powers Survey
| Command | Description |
|---|---|
| `/postsurvey` | Post (or repost) the persistent survey button in the survey channel |

### Event Announcements
| Command | Description |
|---|---|
| `/events [date]` | Open the event editor for today or a specific date (e.g. `April 5` or `4/5`) |

### Train Schedule
| Command | Description |
|---|---|
| `/schedule` | View the current train schedule |
| `/schedule_set` | Add or update entries in the schedule |
| `/schedule_clear` | Clear the entire schedule (requires confirmation) |
| `/trainprompt [date]` | Retrieve a stored ChatGPT prompt for today or a specific date |
| `/cancel` | Cancel your active wizard or log session |

### Birthday Management
| Command | Description |
|---|---|
| `/checkbirthdays` | Manually run the birthday check and add upcoming birthdays to the schedule |
| `/setmembertab [tab]` | Set the active member sheet tab — use this at the start of each new season. Used for birthdays, DS/CS rosters, and survey alias lookups |

### Desert Storm & Canyon Storm
| Command | Description |
|---|---|
| `/draftds` | Generate a Desert Storm mail draft for Team A or Team B |
| `/draftcs` | Generate a Canyon Storm mail draft for Team A or Team B |

### Storm Logging
| Command | Description |
|---|---|
| `/logds` | Log Desert Storm participation data |
| `/logcs` | Log Canyon Storm participation data |
| `/viewlog [event] [date]` | View a full log entry for a specific event and date |

### Growth Tracking
| Command | Description |
|---|---|
| `/rungrowth` | Manually run the monthly squad power snapshot |

### General
| Command | Description |
|---|---|
| `/help` | Show all available commands in Discord |

---

## Automatic Schedule

| Time | What Happens |
|---|---|
| **Noon ET on event days** | Event announcement draft posted to leadership for review |
| **10pm ET nightly** | Birthday check runs — upcoming birthdays added to train schedule |
| **10pm ET nightly** | Train day reminder posted if someone is scheduled and prompt hasn't been pulled |
| **9:55pm ET Fridays** | Buster Day shield reminder posted to leadership for approval |
| **5 min before first event** | Warning auto-posted to Announcements (fires after noon announcement is approved) |
| **1st of every month at 10pm ET** | Squad power growth snapshot written to Growth Tracking sheet |
| **Bot startup** | Initial growth snapshot runs for baseline |

---

## Google Sheet Structure

| Tab | Purpose |
|---|---|
| **Squad Powers** | Member squad data — updated on every survey submission. Columns: Username, Discord ID, 1st Squad, 1st Squad Type, 2nd Squad, 3rd Squad, Drone Level, Gorilla Level, THP, Total Kills, Profession, Banner, Aid/Removal, Date Modified |
| **Survey History** | Timestamped record of every survey submission for trend tracking |
| **Train Schedule** | Upcoming train entries with theme, tone, notes, and prompt status. Also stores the active member tab name in cell H1 |
| **Growth Tracking** | Monthly combined squad power snapshots with % growth columns |
| **DS Assignments** | Saved zone and sub-pair assignments for DS Team A and B, and zone assignments for CS Team A and B |
| **DS-CS Sit-outs** | Event participation log — one row per DS or CS event with votes, RTF no-vote, sit-outs, and prior sit-out data |
| **[Active Member Tab]** | Season member roster (e.g. Season 5 - Off-Season). Used for birthday lookups, DS/CS name validation, and alias resolution. Col E = Name, Col F = Alias, Col I = Birthday |

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main bot — event handling, growth scheduler, help command |
| `scheduler.py` | Event announcement scheduler and approval workflow |
| `train.py` | Train schedule management, birthday auto-population, prompt retrieval |
| `storm.py` | DS and CS mail generation |
| `storm_log.py` | DS and CS participation logging |
| `survey.py` | Squad powers survey — persistent button, private thread flow, sheet sync |
| `growth.py` | Monthly squad power snapshot logic |
| `sheets.py` | Google Sheets authentication and helper functions |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway start command |

---

## Button Reconnect Reference

Discord buttons lose their state if the bot restarts while they're active. If a button shows "interaction failed", here's the manual fallback for each:

| Button | Fallback |
|---|---|
| Event editor (Build Announcement etc.) | `/events` |
| Event approval | `/events` again to rebuild |
| Train reminder (View & Get Prompt) | `/trainprompt` |
| Desert Storm approval | `/draftds` |
| Canyon Storm approval | `/draftcs` |
| DS/CS log steps | `/logds` or `/logcs` |
| Survey button | `/postsurvey` to repost |
