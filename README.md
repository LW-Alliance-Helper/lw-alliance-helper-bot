# OGV Alliance Bot

A Discord bot built for the OGV alliance that automates member data tracking, event announcements, train schedule management, birthday reminders, and monthly squad power growth reporting.

All commands are restricted to the **OGV Leadership** role and the leadership channel. Use `/help` at any time to see the full command list in Discord.

---

## What It Does

### 🔵 Squad Power Tracking
The bot watches the designated survey channel for responses from Subo the Survey Bot. When a survey comes in, it automatically reads the embed data and syncs the member's squad powers, gorilla level, and drone level to the Squad Powers sheet — updating their existing row or creating a new one if they haven't submitted before.

### 📣 Event Announcements
On event days (Plague Marauder and Zombie Siege run on a 3-day cycle), the bot posts a draft announcement to the leadership channel at noon for review. Leadership uses the event editor to adjust times, add or remove events like Glacieradon or Blimp, and add any extra notes. Once the event list looks right, the bot builds the announcement and routes it through an approval flow before anything posts publicly to Announcements. After approval, a 5-minute warning fires automatically to Announcements based on the first event's time.

On Friday nights, a Buster Day shield reminder is posted to leadership for approval at 9:55pm ET.

### 🚂 Train Schedule
Leadership inputs the upcoming train schedule using `/schedule`, with the option to store theme, tone, and notes for each person at the time of entry. At 10pm ET (server reset) each day, if someone is scheduled the bot posts a reminder in the leadership channel with a button to pull up their stored details and get the ready-to-paste ChatGPT prompt.

Birthdays are automatically added to the train schedule 14 days in advance by reading the active member sheet each night at reset. The birthday theme is pre-filled automatically. If a member already has a train entry within one day of their birthday, they are skipped.

### 📈 Growth Tracking
On the 1st of every month at 10pm ET, the bot takes a snapshot of every member's combined squad power (1st + 2nd + 3rd squad) and writes it to the Growth Tracking sheet alongside a percentage growth column comparing it to the previous month. Members not yet on the sheet are added automatically. Members who have been removed are left in place with blanks for the new month.

---

## Slash Commands

### Event Announcements
| Command | Description |
|---|---|
| `/events` | Open the event editor for today |
| `/events [date]` | Open the event editor for a specific date, e.g. `/events April 5` or `/events 4/5` |

### Train Schedule
| Command | Description |
|---|---|
| `/schedule` | Input the upcoming train schedule |
| `/schedule list` | View the current schedule |
| `/schedule clear` | Clear the entire schedule |
| `/trainschedule` | Quick view of the current schedule |
| `/trainprompt` | Retrieve today's stored ChatGPT prompt |
| `/trainprompt [date]` | Retrieve a stored prompt for a specific date |
| `/train` | Launch the manual train blurb wizard for any name |
| `/cancel` | Cancel your active wizard session |

### Birthday Management
| Command | Description |
|---|---|
| `/checkbirthdays` | Manually run the birthday check and add upcoming birthdays to the schedule |
| `/setbirthdays [tab name]` | Update the active member sheet tab used for birthday lookups — use this at the start of each new season |

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

---

## Google Sheet Structure

The bot reads from and writes to the following tabs in the alliance Google Sheet:

| Tab | Purpose |
|---|---|
| **Squad Powers** | Member squad data — updated automatically from Subo survey responses |
| **Train Schedule** | Upcoming train entries with theme, tone, notes, and prompt status. Also stores the active member tab name in cell H1 |
| **Growth Tracking** | Monthly combined squad power snapshots with % growth columns |

---

## Files

| File | Purpose |
|---|---|
| `bot.py` | Main bot — survey parsing, slash commands, growth scheduler |
| `scheduler.py` | Event announcement scheduler and approval workflow |
| `train.py` | Train schedule management, birthday auto-population, blurb wizard |
| `growth.py` | Monthly squad power snapshot logic |
| `sheets.py` | Google Sheets authentication and squad powers read/write |
| `requirements.txt` | Python dependencies |
| `Procfile` | Railway start command |
