# Premium Storm — UX Walkthrough

Screen-by-screen approximations of every user-facing surface in the
Premium Storm flow. Read this end-to-end to catch awkward wording,
flow gaps, and missing affordances before the user does.

Placeholders are filled in with **realistic example values** so each
screen reads the way a real officer / member would experience it.
Example values used:
- Alliance: **Apex**
- Event date: **Saturday, May 18, 2026** (`2026-05-18`)
- Members: Alice (P 412M), Bob (P 380M), Carol (P 350M), Dan (P 320M)
- Officer (the person running the command): Kevin
- Sign-up channel: `#storm-signups`
- Roster Sheet power column: `1st Squad Power`

ASCII boxes approximate what the user sees in Discord (embed boundary
+ body + fields + footer). Buttons appear on the line below the box,
ordered as they render in the Discord client. Ephemeral messages are
labelled `(ephemeral — only the clicker sees it)`. DMs are labelled
`(DM — sent directly to the member)`.

---

## Table of contents

1. Auto sign-up post + member voting + power-refresh DM
2. `/setup_desertstorm` and `/setup_canyonstorm` — structured-flow setup wizard
3. `/sync_members` — alliance roster sync
4. `/desertstorm post_signup` + `/canyonstorm post_signup` — manual sign-up post fire
5. `/desertstorm signups` + `/canyonstorm signups` — officer view
6. On-behalf vote picker
7. Roster builder
8. Auto-fill summary
9. Approve & Post + faction roles
10. `/desertstorm attendance` + `/canyonstorm attendance` — post-event attendance
11. `/desertstorm strategy` + `/canyonstorm strategy` — preset commands
12. Strategy preset editor (multi-step zone wizard)
13. `/desertstorm member_rule` + `/canyonstorm member_rule` — rule commands
14. Walkthrough tour
15. History browser

> **Command-tree note.** As of issue [#143](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/143)
> every storm slash command (except the two top-level `/setup_*`
> wizards) lives under two parent groups: `/desertstorm <sub>` and
> `/canyonstorm <sub>`. The `event_type` argument that older commands
> took is encoded in the parent now — the DS and CS forms are
> separate commands. Where this doc shows a DS-form invocation, the
> CS form is identical under the other parent.

---

## 1. Auto sign-up post + member voting + power-refresh DM

### Screen 1.1 — The auto-posted sign-up message EDITED

The bot posts this in `#storm-signups` automatically (per
configured day-of-week + time + lead days) OR when an officer runs
`/desertstorm post_signup` (or `/canyonstorm post_signup` for the
CS form). It stays in the channel forever and members click the
vote buttons.

**Variant A — Desert Storm, alliance runs both teams (the default):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚔️ Desert Storm — Sign Up for Saturday, May 18, 2026                 │
│                                                                      │
│ Select your availability for Desert Storm!                           │
│ Only 1 vote can be recorded. If you select a 2nd one, it will        │
│ replace the first vote you cast.                                     │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
[🅰️ Team A: 9pm ET (18:00 server time)]  [🅱️ Team B: 4pm ET (13:00 server time)]
[🔄 Either time works]  [❌ Cannot participate]
```

**Variant B — Desert Storm, Team A only alliance (`teams=A`):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚔️ Desert Storm — Sign Up for Saturday, May 18, 2026                 │
│                                                                      │
│ Select your availability for Desert Storm!                           │
│ Only 1 vote can be recorded. If you select a 2nd one, it will        │
│ replace the first vote you cast.                                     │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
[🅰️ Team A: 9pm ET (18:00 server time)]  [❌ Cannot participate]
```

*(Team B-only alliance: identical shape with `🅱️ Team B: 4pm ET …`
+ `❌ Cannot participate` only.)*

**Variant C — Canyon Storm (always single team per faction):**
NOTE: This is blatantly false. There are still A and B teams if the user selects that as the option. It mimics DS exactly but with its own specified times. This needs to be fixed anywhere you have made this assumption.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🏜️ Canyon Storm — Sign Up for Saturday, May 18, 2026                 │
│                                                                      │
│ Select your availability for Canyon Storm!                           │
│ Only 1 vote can be recorded. If you select a 2nd one, it will        │
│ replace the first vote you cast.                                     │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
[🅰️ Team A: 4pm ET (13:00 server time)]  [❌ Cannot participate]
```

---

### Screen 1.2 — Vote-recorded ack EDITED

After Alice clicks `🅰️ Team A: 9pm ET (18:00 server time)`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Vote recorded: Team A: 9pm ET (18:00 server time). You can      │
│ change your vote any time before the event.                          │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Alice sees it)
```

The bold word changes by vote: `Team A` / `Team B` / `Either time
works` / `Cannot participate`.

---

### Screen 1.3 — Click-error ephemerals

Each of these is a single-screen ephemeral with no buttons. They
appear when something blocks the vote from recording.

**1.3a — Member clicks a stale Team B / Either button on a Canyon Storm post that was created before CS switched to single-team:** EDITED
NOTE: This is incorrect because it's not a single team. I don't know where you got this. This could be possible ONLY IF the leadership team only did 1 team instead of 2 but that is for them to decide.
```
┌──────────────────────────────────────────────────────────────────────┐
│ ℹ️ This sign-up post is from before Canyon Storm switched to a       │
│ single-team format. Please vote on a current sign-up poll.           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**1.3b — Member clicks Team B on a `teams=A` alliance's stale post:** EDITED

```
┌──────────────────────────────────────────────────────────────────────┐
│ ℹ️ Your alliance is configured as Team A only. Team B / Either       │
│ aren't valid choices — pick Team A or Cannot participate.            │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

*(Mirror message for `teams=B` alliance: "configured as **Team B
only**. Team A / Either aren't valid choices — pick **Team B**…")*

**1.3c — Bot was restarted with an old-version button that doesn't decode:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ This sign-up button is from an older version. Wait for the next   │
│ sign-up post to vote.                                                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**1.3d — Member somehow clicked a sign-up post from a different server (defensive):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ This sign-up post belongs to a different server. Please use the   │
│ sign-up post in your alliance's channel.                             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**1.3e — Alliance lost Premium access since the post was created:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ This sign-up post is no longer active because the structured      │
│ roster flow has been disabled for this server.                       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 1.4 — Power-refresh DM EDITED

Bot DMs the voter directly when their power column on the alliance
roster Sheet is blank or unparseable. Fires once per voter per
event date (re-voting won't trigger a second DM).

```
┌──────────────────────────────────────────────────────────────────────┐
│ Heads up, your 1st Squad Power on the alliance roster Sheet isn't   │
│ readable. Please update it before the next storm so leadership has   │
│ accurate numbers for zone assignments.                               │
└──────────────────────────────────────────────────────────────────────┘
(DM — sent directly to the member)
```

If the alliance's power column is named `Your Power` or `My Squad
Power`, the bot strips the leading `Your`/`My` so the message reads
naturally — "your **Power**" instead of the awkward "your **Your
Power**".

---

### Flow at a glance

```
[Scheduler fires]  ─OR─  Officer runs `/desertstorm post_signup`
                │
                ▼
   Screen 1.1 ⚔️ sign-up post lands in #storm-signups
                │
                ▼
   Member clicks a vote button
                │
        ┌───────┴────────┐
        ▼                ▼
   Vote accepted   Vote rejected
        │                │
        ▼                ▼
  Screen 1.2 ack   Screen 1.3 (one of 5 error ephemerals)
        │
        ├── If voter's power column is unreadable AND
        │   power-refresh DM is enabled AND no cooldown row:
        ▼
   Screen 1.4 DM
```
## 2. `/setup_desertstorm` + `/setup_canyonstorm` — structured-flow setup wizard

These commands share the same wizard chassis (`run_storm_setup` in
`setup_cog.py`, branching on `event_type="DS"|"CS"`). The screens
below cover the **structured-flow sub-step** — the Premium-and-structured
block that runs as Step 7 of the wizard, after the mail-template /
log-channel / post-channel steps. Live in
`setup_cog._run_structured_flow_setup_step` (around line 5847) plus
the `_ask_signup_schedule` helper (line 5436) and the `_ask_judicator_role`
helper (line 5715).

The wizard runs in **the channel where Kevin ran the command** (the
slash-command interaction itself just acks with a one-line ephemeral —
`⚙️ Starting Desert Storm setup — check the channel for prompts!` —
then every prompt lands as a regular channel message). The wizard
times out after 5 minutes of inactivity; every timeout posts the same
`⏰ Timed out. Run /<cmd>` line.

Every screen is variant-pair: first-time entry (no saved value) vs.
re-entry (Keep current branch). Both paths are shown.

### Screen 2.0 — Slash-command ephemeral ack

When Kevin runs `/setup_desertstorm`, Discord acks with an ephemeral
in the slash response. Every wizard prompt that follows is a regular
channel message in whatever channel he ran it in.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚙️ Starting Desert Storm setup — check the channel for prompts!     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

*(For `/setup_canyonstorm` the ack reads `⚙️ Starting Canyon Storm
setup — check the channel for prompts!`)*

Permission denial — non-leader, non-admin user:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to run                    │
│ `/setup_desertstorm`.                                                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only the clicker sees it)
```

---

### Screen 2.1 — Re-entry summary (only on re-run)

If Apex already has a saved DS config, the wizard opens with this
summary embed instead of jumping straight into Step 1. Drives whether
Kevin descends into the full wizard or bails.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚔️ Current Desert Storm Setup                                        │
│                                                                      │
│ Desert Storm is already configured. Would you like to edit these     │
│ settings?                                                            │
│                                                                      │
│ Sheet Tab                                                            │
│ DS Assignments                                                       │
│                                                                      │
│ Teams                                                                │
│ A & B                                                                │
│                                                                      │
│ Log Channel                                                          │
│ #storm-log                                                           │
│                                                                      │
│ Post Channel                                                         │
│ #desert-storm                                                        │
│                                                                      │
│ Timezone                                                             │
│ (UTC-5) Eastern (New York, Toronto, Miami)                           │
│                                                                      │
│ Mail Templates                                                       │
│ Default                                                              │
│                                                                      │
│ Reminder DM                                                          │
│ Default                                                              │
│                                                                      │
│ Structured Roster Flow                                               │
│ ✅ Enabled                                                           │
└──────────────────────────────────────────────────────────────────────┘
[✏️ Edit settings]  [✅ No changes needed]
```

*(For CS the title reads `🏜️ Current Canyon Storm Setup`; no Teams
field is included because CS is always single-team per faction.)* NOTE: CS is incorrect as stated previously, it can have 2 teams as defined by the leadership.

If Kevin clicks `✅ No changes needed`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ No changes made. Your Desert Storm setup is still active.        │
└──────────────────────────────────────────────────────────────────────┘
```

Wizard exits. Otherwise the full wizard runs — Steps 1–6 cover sheet
tab / teams / log channel / post channel / mail templates / participation
tracking, then the wizard arrives at the structured-flow block below.

---

### Screen 2.2 — Wizard banner

Posted once before Step 1. Same line for first-run and re-entry.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚙️ Desert Storm Setup                                                │
└──────────────────────────────────────────────────────────────────────┘
```

*(CS: `⚙️ Canyon Storm Setup`)*

---

### Screen 2.3 — Structured Roster Flow opt-in (Premium)

Only fires on Premium guilds. Free-tier guilds skip straight to
Screen 2.13 (preset-library tabs).

**Variant A — First-time setup (no saved opt-in):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ Structured Roster Flow (💎 Premium)                                  │
│ The structured flow auto-posts a Discord sign-up poll, captures      │
│ votes per member, and gives leadership a roster builder that         │
│ filters members by power for each zone. Replaces the text-template   │
│ draft for Desert Storm when enabled. You can leave this off and      │
│ still use the strategy preset library on the free tier.              │
└──────────────────────────────────────────────────────────────────────┘
```

Followed immediately by:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Turn on the structured flow for Desert Storm?                        │
└──────────────────────────────────────────────────────────────────────┘
[Yes]  [No]
```

*(CS variant identical but with "Canyon Storm" in both spots.)*

After Kevin clicks one, the buttons disable in place. (`YesNoView`
doesn't post an ack — Discord just shows the message with the buttons
greyed out.)

If Kevin picks **No**, the wizard skips to Screen 2.13. If **Yes**,
it descends through 2.4 → 2.12.

**Variant B — Re-entry (this is just a re-prompt; the previous
opt-in state isn't surfaced as "Keep current" here):**

Identical to Variant A. The opt-in question always re-asks on every
re-run; only sub-steps below it surface Keep-current.

---

### Screen 2.4 — Power metric column EDITED

Free-text reply. Picks up the next message Kevin sends in the channel.
NOTE: This needs to change and use the conventions we have implemented in other wizards where we ask for the Column in the sheet. This would be something like [Use Default: B] [Define your own]. We establish this pattern already and should be using that so we aren't relying on just text matching that can break easily.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Power Metric Column                                                  │
│ Which column on your roster Sheet stores the power value             │
│ the bot should use to gate Desert Storm zone eligibility?            │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
[Use Default: B] [Define your own]
```

Kevin types `1st Squad Power` as a normal channel message; the wizard
captures it (truncated to 80 chars) and continues. No ack is posted —
the next prompt arriving is the signal it accepted.

*Same prompt for CS (label substituted to "Canyon Storm").*

---

### Screen 2.5 — Sub mode EDITED

```
┌──────────────────────────────────────────────────────────────────────┐
│ Sub Mode                                                             │
│ How should subs be tracked when leadership builds a roster?          │
│ • Pool — flat list of subs; any sub can cover any primary no-show.   │
│ • Paired — each primary has a specific sub assigned in advance.      │
└──────────────────────────────────────────────────────────────────────┘
```

**Variant A — First-time (no saved sub_mode, defaults to "pool"):** EDITED

```
[Use Default: Pool]  [Paired — primary↔sub pairs]
```

(The current default is highlighted green, the other in blue.)

**Variant B — Re-entry with saved `pool`:** identical layout — Pool
shows as `Use Current: Pool` (green), Paired shows as `Paired — primary↔sub pairs`
(blue). - EDITED

**Variant C — Re-entry with saved `paired`:** EDITED

```
[Pool — flat sub list]  [Use Current: Paired]
```

After click, the picked button shows the ack inline:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Sub mode: Pool                                                    │
└──────────────────────────────────────────────────────────────────────┘
[✅ Pool] (disabled)  [Paired — primary↔sub pairs] (disabled)
```

*(`Paired` variant: `✅ Sub mode: Paired`.)*

---

### Screen 2.6 — Sign-up channel

Uses the same `ChannelSelectStep` view that other wizard channel picks
use — buttons + native ChannelSelect with a Keep-current option.

**Variant A — First-time (no saved sign-up channel):** EDITED

```
┌──────────────────────────────────────────────────────────────────────┐
│ Desert Storm Sign-Up Channel                                         │
│ The bot will auto-post a sign-up poll here each week. Members click  │
│ buttons to register their availability.                              │
│ You can open the officer view via /desertstorm signups.              │
└──────────────────────────────────────────────────────────────────────┘
[📢 Channel]  [🧵 Thread]
```

(If Apex has no pickable threads at all, only the channel-select
dropdown renders — no Channel/Thread buttons.)

After clicking `📢 Channel`:

```
[ChannelSelect — placeholder: "Select the channel where Desert Storm sign-up polls post..."]
[+ Create New Channel]  [🧵 Pick a thread instead]
```

Kevin picks `#storm-signups`. The select disables in place:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Selected: storm-signups                                           │
└──────────────────────────────────────────────────────────────────────┘
[ChannelSelect: storm-signups] (disabled)  [🧵 Pick a thread instead] (disabled)
```

**Variant B — Re-entry, `#storm-signups` still exists:**

```
[✅ Keep current: #storm-signups]
[📢 Channel]  [🧵 Thread]
```

Clicking `✅ Keep current: #storm-signups`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Keeping: #storm-signups                                           │
└──────────────────────────────────────────────────────────────────────┘
```

**Variant C — Re-entry, the configured channel was deleted:** EDITED

The wizard posts a stale-channel warning above the picker, then
falls back to the Variant A flow (no Keep-current button):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Your previously configured Desert Storm sign-up channel no        │
│ longer exists. Select a new channel.                                 │
└──────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────────────────────────────────────────────┐
│ Desert Storm Sign-Up Channel                                         │
│ The bot will auto-post a sign-up poll here each week. Members click  │
│ buttons to register their availability.                              │
│ You can open the officer view via /desertstorm signups.              │
└──────────────────────────────────────────────────────────────────────┘
[📢 Channel]  [🧵 Thread]
```

---

### Screen 2.7 — Auto-schedule: event day-of-week EDITED
NOTE: DS always runs on Friday at 4pm ET or 9pm ET. We should only ask when they want to post the poll.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Auto-Schedule — Event Day (💎 Premium)                               │
│ What day should we send the poll for members to sign up?             │
└──────────────────────────────────────────────────────────────────────┘
[Dropdown — placeholder: "When should the poll be posted?"]
  • Sunday
  • Monday
  • Tuesday
  • Wednesday
  • Thursday
  • Friday
  • Saturday
  • Skip auto-scheduling (use /desertstorm post_signup manually)
```

**Variant A — First-time (no saved day, the "Skip" option renders
selected by default):** the dropdown shows up with `Skip auto-scheduling`
pre-selected as default.

**Variant B — Re-entry, saved as Saturday:** Saturday shows pre-selected
in the dropdown.

After Kevin picks `Saturday`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Event day: Saturday.                                              │
└──────────────────────────────────────────────────────────────────────┘
[Dropdown] (disabled)
```

If he picks `Skip auto-scheduling…` instead:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Auto-scheduling skipped — /desertstorm post_signup will still     │
│ work manually.                                                       │
└──────────────────────────────────────────────────────────────────────┘
[Dropdown] (disabled)
```

(Skipping short-circuits the next two screens — wizard jumps to
Screen 2.10 with `lead=5`, `time=""`.)

---

### Screen 2.8 — Auto-schedule: lead days EDITED - REMOVE

Uses `ask_keep_or_change` with default `5`.

**Variant A — First-time (no saved lead, two-button layout):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ Auto-Schedule — Lead Days                                            │
│ How many days before the event should the sign-up post fire?         │
│ 5 is a common default (post Tuesday for a Sunday event).             │
└──────────────────────────────────────────────────────────────────────┘
[✅ Use default: 5]  [✏️ Define my own]
```

Click `✅ Use default: 5`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Using 5                                                           │
└──────────────────────────────────────────────────────────────────────┘
```

Click `✏️ Define my own` opens a modal:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Lead Days                                                            │
│                                                                      │
│ Days (integer, 0–14)  [ 5                                        ]   │
│                                                                      │
│                                              [ Cancel ]  [ Submit ]  │
└──────────────────────────────────────────────────────────────────────┘
(modal — only Kevin sees it)
```

After submit (e.g. `7`), the wizard acks:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Using 7                                                           │
└──────────────────────────────────────────────────────────────────────┘
```

**Variant B — Re-entry, saved value `5` (same as default):** still
shows two-button layout, but the label flips to Keep-current style:

```
[✅ Keep current: 5]  [✏️ Define my own]
```

Wait — actually, `ask_keep_or_change` is called here with
`default="5"`, `current=lead_default if lead_default != "5" else ""`,
so a saved `5` renders as the no-saved-value variant (`✅ Use default: 5`)
and only non-5 saved values surface the three-button layout.

**Variant C — Re-entry, saved value `7` (different from default 5):**

```
[✅ Keep current: 7]  [↩️ Use default: 5]  [✏️ Define my own]
```

---

### Screen 2.9 — Auto-schedule: sign-up time EDITED
NOTE: Do not have the option to leave blank. They have already said they want it to be posted automatically so they need to define the time.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Auto-Schedule — Sign-Up Post Time                                    │
│ What time should the bot send the sign-up post? (in your timezone:   │
│ (UTC-5) Eastern (New York, Toronto, Miami))                          │
│ (e.g. `2:00pm`, `9:00am`, or 24-hour `14:00`)                        │
└──────────────────────────────────────────────────────────────────────┘
```

**Variant A — First-time (no saved time):**

```
[✅ Use default: 12:00pm]  [✏️ Define my own]
```

**Variant B — Re-entry, saved `14:00` (rendered as `2:00pm` in the
keep-current label):**

```
[✅ Keep current: 2:00pm]  [↩️ Use default: 12:00pm]  [✏️ Define my own]
```

Click `✏️ Define my own` opens a modal:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Sign-Up Time                                                         │
│                                                                      │
│ e.g. 2:00pm — blank for manual  [ 2:00pm                        ]    │
│                                                                      │
│                                              [ Cancel ]  [ Submit ]  │
└──────────────────────────────────────────────────────────────────────┘
(modal)
```

After submit:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Using 2:00pm                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

Submitting an empty value stores `""` (= manual posting only).

---

### Screen 2.10 — Sign-Ups tab

`ask_keep_or_change` with event-type-aware default
(`DS Signups` / `CS Signups`).

**Variant A — First-time (no saved value):**
NOTE: We need to be sure that we /create/ the tab if it doesn't exist. Update the wording here like we do elsewhere when that is the case.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Sign-Ups Tab                                                         │
│ Which Google Sheet tab should the bot use for Desert Storm           │
│ sign-ups? The bot manages the structure — just make sure the tab     │
│ exists.                                                              │
└──────────────────────────────────────────────────────────────────────┘
[✅ Use default: DS Signups]  [✏️ Define my own]
```

**Variant B — Re-entry, saved value matches default:**

```
[✅ Keep current: DS Signups]  [✏️ Define my own]
```

**Variant C — Re-entry, saved value `Storm Sign-Ups 2026` (custom):**

```
[✅ Keep current: Storm Sign-Ups 2026]  [↩️ Use default: DS Signups]  [✏️ Define my own]
```

Modal (on `✏️ Define my own`):

```
┌──────────────────────────────────────────────────────────────────────┐
│ Sign-Ups Tab Name                                                    │
│                                                                      │
│ Tab name             [ DS Signups                                ]   │
│                                                                      │
│                                              [ Cancel ]  [ Submit ]  │
└──────────────────────────────────────────────────────────────────────┘
(modal)
```

Ack after pick or submit:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Using DS Signups                                                  │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 2.11 — Rosters tab

Same shape as Screen 2.10, with `Rosters` substituted throughout.
NOTE: We need to be sure that we /create/ the tab if it doesn't exist. Update the wording here like we do elsewhere when that is the case.


```
┌──────────────────────────────────────────────────────────────────────┐
│ Rosters Tab                                                          │
│ Which Google Sheet tab should the bot use for Desert Storm           │
│ rosters? The bot manages the structure — just make sure the tab      │
│ exists.                                                              │
└──────────────────────────────────────────────────────────────────────┘
```

First-time: `[✅ Use default: DS Rosters]  [✏️ Define my own]`
Re-entry: `[✅ Keep current: DS Rosters]  [✏️ Define my own]` (or
three-button when custom).

Modal title: `Rosters Tab Name`. Ack: `✅ Using DS Rosters`.

---

### Screen 2.12 — Attendance tab

Same shape again.
NOTE: We need to be sure that we /create/ the tab if it doesn't exist. Update the wording here like we do elsewhere when that is the case.


```
┌──────────────────────────────────────────────────────────────────────┐
│ Attendance Tab                                                       │
│ Which Google Sheet tab should the bot use for Desert Storm           │
│ attendance? The bot creates and maintains this tab if it doesn't     |
| exist.                                                               │
└──────────────────────────────────────────────────────────────────────┘
```

First-time: `[✅ Use default: DS Attendance]  [✏️ Define my own]`
Re-entry: `[✅ Keep current: DS Attendance]  [✏️ Define my own]`

Modal title: `Attendance Tab Name`. Ack: `✅ Using DS Attendance`.

---

### Screen 2.12c — Judicator role (CS only, Premium) EDITED
NOTE: Remove this, it seems like an extra, unnecessary thing for our users to try and set up in this manner.

**This screen does not fire for `/setup_desertstorm`.** It only
appears in the CS wizard, after the Attendance tab screen.

**Variant A — First-time (no saved role_id):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ Judicator Role (💎 Premium — CS only)                                │
│ Pick the Discord role the bot should apply to members tagged as      │
│ Judicator candidates (via /canyonstorm member_rule set_member_role) after a   │
│ CS roster is approved and matchmaking reveals Rulebringers. Skip if  │
│ you don't use this.                   │
└──────────────────────────────────────────────────────────────────────┘
[Dropdown — placeholder: "Pick the Judicator role"]
  • @Judicator
  • @Officer
  • @Member
  • … (up to 24 roles, hierarchy-ordered)
[+ Create role]  [Skip]
```

After Kevin picks `@Judicator`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Judicator role: @Judicator                                        │
└──────────────────────────────────────────────────────────────────────┘
[Dropdown] (disabled)
```

If he picks `Skip — no role to apply`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Judicator role skipped.                                           │
└──────────────────────────────────────────────────────────────────────┘
```

**Variant B — Re-entry, role still resolves (Keep-or-change gate
fires first before descending to the picker):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ Judicator Role (💎 Premium — CS only)                                │
│ Currently set to Judicator. Keep it, switch to no role, or pick a    │
│ different role.                                                      │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: Judicator]  [↩️ Skip — no role to apply]  [✏️ Change role]
```

Click `✅ Keep current: Judicator`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Keeping Judicator                                                 │
└──────────────────────────────────────────────────────────────────────┘
```

Click `↩️ Skip — no role to apply`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Judicator role cleared.                                           │
└──────────────────────────────────────────────────────────────────────┘
```

Click `✏️ Change role`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✏️ Pick a new role below…                                            │
└──────────────────────────────────────────────────────────────────────┘
```

Followed immediately by the Variant A picker (with the current role
pre-selected in the dropdown).

**Variant C — Re-entry, saved role_id no longer resolves (role
deleted in Discord):** the gate still fires but with
`current_label="role id 1234567890"` instead of a name:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Judicator Role (💎 Premium — CS only)                                │
│ Currently set to role id 1234567890. Keep it, switch to no role, or  │
│ pick a different role.                                               │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: role id 1234567890]  [↩️ Skip — no role to apply]  [✏️ Change role]
```

---

### Screen 2.12d — Power-refresh DM (Premium) EDITED

NOTE: This will need to be edited to match the changes we make with how we select Column instead. We should fetch the column header and have it say something like "Column B: 1st Squad Power" in this section.

Fires for both DS and CS as the last opted-in step.

**Variant A — First-time (no prior structured-flow enabled state),
shows YesNoView:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ Power-Refresh DM (💎 Premium)                                        │
│ When a member clicks a sign-up button for Desert Storm and their    │
│ 1st Squad Power cell is blank or unparseable, should the bot DM them │
│ a one-line nudge to update it? At most one DM per member per event   │
│ date.                                                                │
└──────────────────────────────────────────────────────────────────────┘
[Yes]  [No]
```

(If Kevin hadn't filled out a power column yet, the bolded
substitution falls back to literal `power column`.)

**Variant B — Re-entry where the alliance had structured-flow
enabled previously, Keep-or-flip gate fires instead. Saved as ON:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ Power-Refresh DM (💎 Premium)                                        │
│ When a member clicks a sign-up button for Desert Storm and their    │
│ 1st Squad Power cell is blank or unparseable, the bot can DM them a  │
│ one-line nudge to update it. Currently on — keep it or flip.         │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: Yes]  [↩️ Switch to: No]
```

Click `✅ Keep current: Yes`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Keeping Yes                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

Click `↩️ Switch to: No`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Switched to No                                                    │
└──────────────────────────────────────────────────────────────────────┘
```

**Variant C — Re-entry, saved as OFF:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ Power-Refresh DM (💎 Premium)                                        │
│ When a member clicks a sign-up button for Desert Storm and their    │
│ 1st Squad Power cell is blank or unparseable, the bot can DM them a  │
│ one-line nudge to update it. Currently off — keep it or flip.        │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: No]  [↩️ Switch to: Yes]
```

---

### Screen 2.13a — Strategy Presets explainer (Premium + opted-in only) EDITED

The wizard posts a plain-text explainer before asking for the tab
name, so officers know what the concept *is* before they're asked
to name a Sheet tab for it (#144). Fires only when the alliance
opted into the structured flow at 2.3 — free-tier alliances and
Premium alliances that declined the structured flow skip this whole
block, since strategy presets only drive the roster builder.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Strategy Presets                                                     │
│ A strategy preset is a saved zone layout including:                  │
│ Maximum players per zone                                             │
| Optional power requirements                                          │
│ Priority                                                             │
│                                                                      │
| When leadership builds a roster, they pick which                     │
│ preset to apply. The bot uses the preset to gate eligibility and     │
│ fill out the team.                                                   │
│                                                                      │
│ Manage presets with                                                  │
| `/desertstorm strategy create / edit / list / apply`.                │
└──────────────────────────────────────────────────────────────────────┘
```

*(CS variant: `…for Canyon Storm` + `/canyonstorm strategy create /
edit / list / apply`.)*

This message is a plain `channel.send(...)` — no view, no buttons.
The wizard immediately follows with Screen 2.13.

---

### Screen 2.13 — Strategy Presets tab (Premium + opted-in only)

Fires only when the alliance opted into the structured flow at 2.3.
Same `ask_keep_or_change` shape as the Premium tab prompts.

**Variant A — First-time:** EDITED

```
┌──────────────────────────────────────────────────────────────────────┐
│ Strategy Presets Tab                                                 │
│ Which Google Sheet tab should store Desert Storm strategy presets?   │
│ The bot creates and maintains this tab.                              │
└──────────────────────────────────────────────────────────────────────┘
[✅ Use default: DS Strategies]  [✏️ Define my own]
```

**Variant B — Re-entry, value matches default:**

```
[✅ Keep current: DS Strategies]  [✏️ Define my own]
```

**Variant C — Re-entry, custom value `Apex DS Strats`:**

```
[✅ Keep current: Apex DS Strats]  [↩️ Use default: DS Strategies]  [✏️ Define my own]
```

Modal title: `Strategy Presets Tab Name`. Ack: `✅ Using DS Strategies`.

---

### Screen 2.13b — Inline-create-preset offer (zero-presets alliance)

After the Strategy Presets tab name is saved, the wizard checks
`storm_strategy.list_presets(guild_id, "DS")`. If the alliance has
zero presets, the wizard offers to create the first one inline so
officers don't have to re-run a separate slash command to discover
the concept. If the alliance already has any preset (re-entry case),
this offer is skipped.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Want to create your first Desert Storm preset now? You can also do   │
│ this later with `/desertstorm strategy create`.                      │
└──────────────────────────────────────────────────────────────────────┘
[✨ Create my first preset now]  [Skip for now]
```

`[✨ Create my first preset now]` is `ButtonStyle.primary` (blue).
`[Skip for now]` is `ButtonStyle.secondary` (grey).

Behaviour on `[Skip for now]`: both buttons disable in place, the
wizard moves on to Screen 2.14a. Behaviour on `[✨ Create my first
preset now]`: the buttons disable in place, then the strategy editor
(see Section 12) opens as an ephemeral followup, seeded with the
default zone layout under the name "Standard". The wizard does not
wait for the editor to be saved — the user can return to the wizard
flow by completing or dismissing the editor.

If the alliance has any preset already (re-entry case), the offer
view is **not** posted and the wizard jumps directly to 2.14a.

*(CS variant: `Want to create your first Canyon Storm preset now?
You can also do this later with /canyonstorm strategy create.`)*

---

### Screen 2.13b-timeout — Offer view timeout

After 5 minutes the inline-create-preset offer view times out. The
`on_timeout` handler calls `expire_view_message` so the buttons are
stripped from the message and a footer is appended:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Want to create your first Desert Storm preset now? You can also do   │
│ this later with `/desertstorm strategy create`.                      │
│ ⏰ Timed out — re-open via `/desertstorm strategy create`.           │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 2.14a — Member Rules explainer (Premium + opted-in only) EDITED

Mirrors 2.13a — a plain-text explainer before the tab prompt. Same
gating: only fires when the alliance is opted into the structured
flow. Per-member rule guidance differs by event type — DS has teams,
CS doesn't.

**Variant A — Desert Storm:** EDITED

```
┌──────────────────────────────────────────────────────────────────────┐
│ Member Rules                                                         │
│ Member rules tell the roster builder how to treat individual         │
│ members.                                                             │
│                                                                      │
| There are two types of Member rules.                                 │
│ • Power-band:                                                        |
|      Example: `members ≥ 250M are eligible for Power Tower`          │
│      Primary rule type that reads against the power column you       │
│      configured earlier.                                             │
│ • Per-member:                                                        │
|      Used for special cases, example: `Alice always plays on Team A`,│
│                                                                      │
| Add rules later with                                                 │
│   `/desertstorm member_rule` : `set_power_band` /                    │
│   `set_member_team` / `set_member_zone` / `set_member_role`.         │
└──────────────────────────────────────────────────────────────────────┘
```

**Variant B — Canyon Storm:** EDITED
NOTE: Use the same as above but replace with canyonstorm. There are teams as stated previously in this document.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Member Rules                                                         │
│ Member rules tell the roster builder how to treat individual         │
│ members. Two types:                                                  │
│ • Power-band — `members ≥ 250M are eligible for Power Tower`.        │
│   Primary rule type; reads against the power column you configured   │
│   earlier.                                                           │
│ • Per-member — escape hatch for special cases: `Carol always plays   │
│   Power Tower`, `Dan is our Judicator candidate`. Add rules later    │
│   with `/canyonstorm member_rule set_power_band` /                   │
│   `set_member_zone` / `set_member_role`.                             │
└──────────────────────────────────────────────────────────────────────┘
```

~~Note CS drops `set_member_team` from the rule-subcommand list — CS
has no teams so the subcommand doesn't exist under the CS parent.
The example also swaps the team-based "Alice always plays Team A"
for the zone-based "Carol always plays Power Tower".~~

---

### Screen 2.14 — Member Rules tab (Premium + opted-in only) EDITED

Same shape as 2.13.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Member Rules Tab                                                     │
│ Which Google Sheet tab should store Desert Storm member rules?       │
│ The bot creates and maintains this tab.                              │
└──────────────────────────────────────────────────────────────────────┘
```

First-time: `[✅ Use default: DS Member Rules]  [✏️ Define my own]`
Re-entry (match): `[✅ Keep current: DS Member Rules]  [✏️ Define my own]`

Modal title: `Member Rules Tab Name`. Ack: `✅ Using DS Member Rules`.

---

### Screen 2.14b — Inline-create-member-rule offer (zero-rules alliance)

After the Member Rules tab name is saved, the wizard checks
`storm_member_rules.list_rules(guild_id, "DS")`. If zero rules
exist, the wizard offers a streamlined inline path to add the
first one — but only for power-band rules (the common case).
Per-member rules need a Discord member picker which can't fit in a
modal, so the offer's prose redirects officers to slash commands
for those.

**Variant A — Desert Storm:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ Want to add your first Desert Storm rule now? The button opens a     │
│ quick modal for a power-band rule (the most common type); per-member │
│ rules need a Discord member picker, so add those later via           │
│ `/desertstorm member_rule set_member_team` (or `set_member_zone` /   │
│ `set_member_role`).                                                  │
└──────────────────────────────────────────────────────────────────────┘
[✨ Add a power-band rule now]  [Skip for now]
```

**Variant B — Canyon Storm:** (CS has no `set_member_team`)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Want to add your first Canyon Storm rule now? The button opens a     │
│ quick modal for a power-band rule (the most common type); per-member │
│ rules need a Discord member picker, so add those later via           │
│ `/canyonstorm member_rule set_member_zone` (or `set_member_role`).   │
└──────────────────────────────────────────────────────────────────────┘
[✨ Add a power-band rule now]  [Skip for now]
```

Behaviour on `[Skip for now]`: both buttons disable in place and the
wizard moves on. Behaviour on `[✨ Add a power-band rule now]`:
the modal in 2.14c opens; the offer message's buttons are disabled
via `self.message.edit` after `send_modal` (the modal must be the
interaction response, so the disable is pushed via the bot-owned
message handle separately).

If the alliance has any rule already (re-entry case), this offer is
not posted.

---

### Screen 2.14c — InlinePowerBandView (opened by 2.14b)

#168 / Rule E: replaces the original two-field modal with a Zone
Select + power-modal handoff so a typo can't slip through the zone
field. Defined in `storm_member_rules.InlinePowerBandView`.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Pick the zone the rule applies to, then click **Set minimum power** │
│ to enter the threshold.                                              │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Pick a zone…                                                    ]
[⚙️ Set minimum power (disabled)]  [↩️ Cancel]
(ephemeral — only Kevin sees it)
```

The Zone Select is sourced from `DS_ZONE_STRUCTURE` (11 zones — fits
under Discord's 25-option Select cap) or `CS_ZONE_STRUCTURE` (21 zones
— also under the cap). Picking a zone enables the **Set minimum
power** button; until then it stays disabled so Kevin can't blank-
submit.

**Picking a zone → click Set minimum power:**

The view's selection state is preserved in the dropdown placeholder
("Picked: Power Tower") and the **Set minimum power** button opens a
one-field modal:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Desert Storm Power-Band Rule — Power Tower                           │
│                                                                      │
│ Minimum power for Power Tower                                        │
│ ┌─────────────────────────────────────────────────────────────────┐  │
│ │ e.g. 250M, 1.2B, 300,000,000                                    │  │
│ └─────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│                                                  [Cancel]  [Submit]  │
└──────────────────────────────────────────────────────────────────────┘
```

Modal title: `Desert Storm Power-Band Rule — Power Tower` (or `Canyon
Storm Power-Band Rule — <zone>` for CS). The single Power field is
`required`; Submit fires `on_submit` against the captured zone.

**Submit — success (saved):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: ≥ 250M → eligible for **Power Tower**.                     │
│ Add more rules later via `/desertstorm member_rule …`.               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

**Submit — unparseable power value (e.g. typed `dunno`):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't parse `dunno` as a power value. Try `250M`, `1.2B`, or   │
│ `300,000,000` next time via                                          │
│ `/desertstorm member_rule set_power_band`.                           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

Non-canonical and empty-zone error variants are unreachable now —
the Zone Select rejects them at pick time.

**Submit — save failed (Sheet write error):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ <save_rule's failure message — e.g. "couldn't open Sheet">       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

**Cancel:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ↩️ Cancelled — no rule saved.                                        │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 2.15 — Inline post-first-signup offer (#144) EDITED

After the structured-flow setup save completes (the `[SETUP]` log
fires) AND the alliance opted into the structured flow AND a sign-up
channel was configured AND no sign-up post has been recorded for
this guild + event type yet, the wizard offers to fire the first
sign-up post inline. Whether auto-scheduling was configured or
skipped, this gives Apex one fully-live sign-up post at the end of
setup.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📣 Want to post your first Desert Storm sign-up now? It'll land in   │
│ #storm-signups with vote buttons members can click. You can also     │
│ wait for the auto-schedule to post it (if you set one up) or run     │
│ `/desertstorm post_signup` later.                                    │
└──────────────────────────────────────────────────────────────────────┘
[📣 Post my first sign-up now]  [Skip]
```

Behaviour on `[Skip]`: both buttons disable in
place, setup ends. Behaviour on `[📣 Post my first sign-up now]`:
fires the same `post_registration` code path as `/desertstorm
post_signup`, so the success / error screens in Section 4 (4.8 –
4.14) apply here too.

If the alliance has any prior sign-up post recorded (re-entry case),
this offer is **not** posted — the wizard exits silently after the
save embed.

*(CS variant: `…Canyon Storm sign-up now? …run /canyonstorm
post_signup later.`)*

---

### Conditional matrix — what fires when

| Variant | 2.3 Opt-in | 2.4 Power col | 2.5 Sub mode | 2.6 Channel | 2.7 DoW | 2.8 Lead | 2.9 Time | 2.10–2.12 Tabs | 2.12c Judicator | 2.12d DM | 2.13a–2.13b Strategy | 2.14a–2.14c Member Rule | 2.15 First-signup offer |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Premium + opt-in (DS) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | — | ✅ | ✅ | ✅ | ✅ (if first run) |
| Premium + opt-in (CS) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (if first run) |
| Premium + opt-out | ✅ | — | — | — | — | — | — | — | — | — | — | — | — |
| Free tier | — | — | — | — | — | — | — | — | — | — | — | — | — |
| Re-entry, DoW=Skip | ✅ | ✅ | ✅ | ✅ | ✅ (Skip) | — | — | ✅ | ✅ (CS only) | ✅ (gate) | ✅ (skip 2.13b if presets exist) | ✅ (skip 2.14b/c if rules exist) | — (already posted) |

Note: pre-fix, the strategy + member-rules block fired for all four
variants. The post-fix block (this audit) gates it on
`structured_opted_in` so free-tier alliances and Premium-opt-out
alliances don't see Premium-only copy.

---

### Cancel & timeout

Every prompt is wrapped in `wait_view_or_cancel`. If Kevin types
`/cancel` (the slash command, mid-wizard), the wizard exits silently
— the `/cancel` command itself posts its own ack. Each view-step has
its own timeout (120–300s); on timeout the wizard posts:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⏰ Timed out. Run `/setup_desertstorm` to start again.              │
└──────────────────────────────────────────────────────────────────────┘
```

*(CS variant: `Run /setup_canyonstorm to start again.`)*

---

### Flow at a glance

```
Kevin runs /setup_desertstorm
        │
        ▼
   Screen 2.0 ephemeral ack
        │
        ▼
   Re-entry?  ──── yes ──── Screen 2.1 summary embed
        │                          │
        no                         ├── No changes → exit
        │                          │
        ▼                          ▼ Edit → continue
   Screen 2.2 banner ◀────────────┘
        │
        ▼
   Steps 1–6 (tab, teams, channels, templates, participation)
        │
        ▼
   Screen 2.3 structured-flow opt-in
        │
        ├── No (or free tier) ──────────────────────────────┐
        │                                                   │
        ▼ Yes                                               │
   2.4 power column                                         │
        │                                                   │
        ▼                                                   │
   2.5 sub mode                                             │
        │                                                   │
        ▼                                                   │
   2.6 sign-up channel                                      │
        │                                                   │
        ▼                                                   │
   2.7 day-of-week ──── Skip ──── 2.10 (skip 2.8 & 2.9)    │
        │                            ▲                      │
        ▼ pick a day                 │                      │
   2.8 lead days                     │                      │
        │                            │                      │
        ▼                            │                      │
   2.9 sign-up time ─────────────────┘                      │
        │                                                   │
        ▼                                                   │
   2.10 signups tab → 2.11 rosters tab → 2.12 attendance   │
        │                                                   │
        ▼                                                   │
   (CS only) 2.12c judicator role                           │
        │                                                   │
        ▼                                                   │
   2.12d power-refresh DM toggle                            │
        │                                                   │
        ▼                                                   │
   2.13a Strategy Presets explainer                         │
        │                                                   │
        ▼                                                   │
   2.13 strategies tab                                      │
        │                                                   │
        ▼                                                   │
   2.13b Inline-create-preset offer (if 0 presets)          │
        │                                                   │
        ▼                                                   │
   2.14a Member Rules explainer (DS or CS variant)          │
        │                                                   │
        ▼                                                   │
   2.14 member rules tab                                    │
        │                                                   │
        ▼                                                   │
   2.14b Inline-create-rule offer (if 0 rules)              │
        │     ↓                                             │
        │   2.14c InlinePowerBandModal (on accept)          │
        ▼                                                   ▼
   Save embed (always shown) ◀───────────────────────────────
        │
        ▼
   2.15 Inline post-first-signup offer
        (only if opted-in + channel set + no prior post)
        │
        ▼
   (continues into save + tour)
```

---

## 3. `/sync_members` — alliance roster sync

Lives in `member_roster.py`. Premium-gated. Runs `write_roster()`,
which rebuilds the configured roster tab in Apex's Google Sheet and
auto-maintains the **`Is this user in Discord?`** column with Yes/No
values + a Sheets data-validation dropdown.

The command itself is fire-and-forget — there's no wizard. Every
output is an ephemeral on the slash-command interaction.

### Screen 3.1 — Permission denial (non-leader, non-admin)

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to sync the member       │
│ roster.                                                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only the clicker sees it)
```

---

### Screen 3.2 — Premium upsell (free-tier guild)

Premium-gated. Free-tier alliances see the standard premium-locked
embed + upgrade view.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 💎 Premium Feature: Member Roster Sync                               │
│                                                                      │
│ Member Roster Sync writes every member's Discord ID to your sheet    │
│ so other Premium features (birthday DMs, train DMs, auto-mention,    │
│ etc.) can find them. Run /upgrade to unlock it.                      │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
[💎 Upgrade to Premium]  (etc — from premium.upgrade_view())
(ephemeral — only Kevin sees it)
```

*(Exact embed shape comes from `premium.premium_locked_embed`; the
buttons come from `premium.upgrade_view()`. Same shape as every other
Premium upsell in the bot.)*

---

### Screen 3.3 — Not-yet-configured guard

Fires when Apex is Premium but hasn't run `/setup_members` yet.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚙️ Member Roster Sync isn't configured yet. Run /setup_members      │
│ first.                                                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

---

### Screen 3.4 — Sync running (typing indicator) EDITED

After the gates pass, the bot defers the interaction (`thinking`
state — Discord shows "LW Alliance Helper is thinking…" to Kevin
while the Sheets round-trip happens). No visible message body —
just the spinner.

```
┌──────────────────────────────────────────────────────────────────────┐
│ LW Alliance Helper is working...                                     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

Behind the scenes the bot:
1. Forces a guild member-cache load (`guild.chunk()`).
2. Builds the row data from the live member list (Apex has 60 members
   matching the role filter).
3. Reads the existing tab values from `Member Roster` so it can
   preserve alliance-owned columns (custom power columns,
   `not_on_discord`, etc.).
4. Auto-creates the `Is this user in Discord?` column if it doesn't
   already exist (added at the right edge of the tab).
5. Fills the column with `Yes` / `No` per row based on live guild
   membership (non-bot members only).
6. Writes the merged rows back to the sheet, clearing first.
7. Applies a Yes/No-dropdown data-validation rule on the column
   (`strict: true`, input hint: `Auto-filled by the LW Alliance Helper
   bot. Override to Yes/No if needed.`).

---

### Screen 3.5 — Sync success

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Synced 60 members to the Member Roster tab.                       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

The number and tab name reflect the live result —
`{count}` rows actually written (excluding the header), `{tab_name}`
from the saved config. (`Member Roster` is the default.)

---

### Screen 3.6 — Sync failure EDITED

If `write_roster()` raises (e.g. Sheets API 403, network blip, tab
permission revoked):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Sync failed: <error message>                                      │
│ Make sure the bot has access to your sheet and that the              │
│ Member Roster tab can be written to.                                 │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

The error message is whatever `str(e)` returns from the underlying
gspread exception. Examples:

```
⚠️ Sync failed: APIError: [403]: The caller does not have permission.
Make sure the bot has access to your sheet and that the
Member Roster tab can be written to.
```

```
⚠️ Sync failed: APIError: [404]: Requested entity was not found.
Make sure the bot has access to your sheet and that the
Member Roster tab can be written to.
```

(The `{cfg['tab_name']}` substitution is bold-wrapped in the source
— `**Member Roster**` in Discord markdown — rendering with the tab
name visually emphasised.)

---

### Side-effects on the Sheet (not surfaced in Discord)

For completeness — what an officer would see if they switch to the
Google Sheet right after `/sync_members`:

**Before the first sync** (or if alliance hasn't created the tab):
the bot creates the `Member Roster` tab automatically. There's no
Discord ephemeral about this — it's just a side-effect.

**After every sync**, the tab structure looks like:

```
| A           | B     | C            | D          | E             | F                          |
| Discord ID  | Name  | Display Name | Joined     | Roles         | Is this user in Discord?   |
| 18293...    | alice | Alice        | 2024-11-02 | Member, Storm | Yes                        |
| 18294...    | bob   | Bob          | 2025-01-15 | Member        | Yes                        |
| 18295...    | (legacy row preserved)              |               | No                         |
```

The column **F** header (`Is this user in Discord?`) is bot-created
the first time `/sync_members` runs after the alliance upgrades to
the version with the new column. Existing rows get extended with the
new cell; existing custom columns (power columns, `not_on_discord`,
notes) are preserved per Discord-ID match.

Each cell in column F is gated by a Yes/No dropdown — clicking the
cell in Sheets shows a tiny `▼` selector. Hovering shows the input
message:

```
Auto-filled by the LW Alliance Helper bot. Override to Yes/No if
needed.
```

Manually overridden values get reset to the bot-derived value on the
next sync (the column is bot-maintained, not alliance-owned).

---

### Server log breadcrumb (not user-facing)

If Apex's `members` privileged intent is off (or the bot can't chunk
the guild), the count of returned members will be wildly under the
actual guild size. `/sync_members` will still complete with
`✅ Synced N members to the Member Roster tab` where `N` is the
cached subset, but the bot logs:

```
[ROSTER] Guild 1234567890: only 4/60 members in cache. Enable the
SERVER MEMBERS INTENT in the Discord Developer Portal (Bot →
Privileged Gateway Intents) — without it `guild.members` can't see
the full roster.
```

This appears only in the bot's Railway stdout — Kevin won't see it in
Discord. (Surfacing this to the user is a known UX gap; the success
ephemeral happily reports the partial count.)

---

### Auto-sync side channel

In addition to manual `/sync_members`, the cog also re-syncs
automatically on `on_member_join`, `on_member_remove`, and
`on_member_update` (only if role membership changed). These don't
post anything to Discord — they update the Sheet silently. Errors
go to Railway stdout + Sentry:

```
[ROSTER] Auto-sync failed for guild 1234567890: <exception repr>
```

---

### Flow at a glance

```
Kevin runs /sync_members
        │
        ▼
   Has leadership role / admin?
        │
        ├── no ──→ Screen 3.1 (⛔ denial)
        │
        ▼ yes
   Is guild Premium?
        │
        ├── no ──→ Screen 3.2 (💎 upsell)
        │
        ▼ yes
   Roster config saved (enabled flag)?
        │
        ├── no ──→ Screen 3.3 (⚙️ run /setup_members)
        │
        ▼ yes
   Screen 3.4 (defer + thinking spinner)
        │
        ▼
   chunk cache → build rows → read existing → merge →
   ensure `Is this user in Discord?` column → write →
   apply Yes/No data-validation
        │
        ├── raises ──→ Screen 3.6 (⚠️ sync failed)
        │
        ▼ ok
   Screen 3.5 (✅ synced N members)
```

---

## 4. `/desertstorm post_signup` + `/canyonstorm post_signup` — manual sign-up post fire

Lives in `storm_signup_post.py`. Premium + structured-flow gated.
The consolidated tree makes event type the parent group, so the
DS form (`/desertstorm post_signup`) and CS form (`/canyonstorm
post_signup`) are two distinct commands — each takes a single
optional `event_date` argument. Posts the same `SignupView` message
that Flow 1 covers as auto-scheduled — except triggered manually.

The slash command itself is short — every output is an ephemeral on
the interaction. The actual sign-up post (when successful) is a
public message in `#storm-signups` (see Flow 1, Screen 1.1).

### Screen 4.1 — Slash-command invocation

What Discord shows in the chat composer (DS form shown — CS form is
identical under `/canyonstorm post_signup`):

```
/desertstorm post_signup event_date: <optional — defaults to the next configured event day.
                                      Accepts e.g. May 18, 5/18, 2026-05-18, Sunday.>
```

`event_date` is a free-text optional argument.

---

### Screen 4.2 — Permission denial (non-leader, non-admin)

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to run this command.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only the clicker sees it)
```

*(Shared denial copy from `storm_permissions.deny_non_leader` — same
line every structured-flow command uses.)*

---

### Screen 4.3 — Unparseable date

If Kevin types `/desertstorm post_signup event_date:tomrrow`
(typo):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ `tomrrow` isn't a date I can parse. Try `May 18`, `5/18`,        │
│ `2026-05-18`, `Sunday`, or `tomorrow`.                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

---

### Screen 4.4 — Past-date rejection

If Kevin runs `/desertstorm post_signup event_date:2026-05-09`
on today's date (2026-05-14):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Event date Saturday, May 9, 2026 is in the past. Sign-ups should │
│ be posted for upcoming events.                                       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

(Today is computed in Apex's configured timezone, not UTC — so an
east-of-UTC alliance posting near midnight their time won't see
their own current-day event flagged as past.)

---

### Screen 4.5 — Premium upsell (free-tier guild)

From `ensure_premium_structured`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔒 `/desertstorm post_signup` is a 💎 Premium feature. Run /upgrade to    │
│ unlock it.                                                           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

---

### Screen 4.6 — Structured-flow not enabled

Apex is Premium but hasn't opted into the structured flow for this
event type:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The structured roster flow isn't enabled for Desert Storm. Run   │
│ /setup_desertstorm and turn on Structured Roster Flow first.        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

*(CS variant: `…isn't enabled for Canyon Storm. Run /setup_canyonstorm
and turn on Structured Roster Flow first.`)*

---

### Screen 4.7 — Thinking spinner (defer)

After every gate passes, the bot defers ephemerally before calling
`post_registration`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ LW Alliance Helper is thinking…                                     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

The followup then replaces this with one of the result screens
below (4.8–4.14).

---

### Screen 4.8 — Success

The bot posts the sign-up message in `#storm-signups` (= Flow 1
Screen 1.1) and then sends Kevin this ephemeral followup:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Sign-up post for Desert Storm on Saturday, May 18, 2026 is live  │
│ in #storm-signups. Members can vote any time before the event. Open │
│ /desertstorm signups to review who's voted.                                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

*(CS variant: `Sign-up post for Canyon Storm on Saturday, May 18, 2026…`)*

---

### Screen 4.9 — Already posted

If Kevin runs the command for an event date that already has a
sign-up post (idempotency guard via `has_registration_post`):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ℹ️ A sign-up post already exists for Saturday, May 18, 2026 (DS).   │
│ Check #storm-signups for the existing post — members can keep       │
│ voting on it. If you need to re-post, delete the prior message      │
│ first.                                                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

*(CS variant: parenthesis reads `(CS)`.)*

---

### Screen 4.10 — No sign-up channel configured

`signup_channel_id` is unset on the structured config:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No sign-up channel configured. Run `/setup_desertstorm` and pick │
│ a sign-up channel during the structured-flow setup.                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

*(CS variant: `Run /setup_canyonstorm and pick…`)*

---

### Screen 4.11 — Channel gone

Saved `signup_channel_id` no longer resolves (channel deleted, or bot
lost access):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The configured sign-up channel (#storm-signups) no longer exists │
│ or the bot can't see it. Re-run /setup_desertstorm to pick a new    │
│ channel.                                                             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

(`<#channel_id>` renders as either the channel mention if the bot can
still see it, or as `<#1234567890>` literal if not. Discord falls
back to "deleted-channel" rendering automatically.)

*(CS variant: `Re-run /setup_canyonstorm…`)*

---

### Screen 4.12 — Missing slot labels (DS, time options not configured)

For DS, where Apex hasn't completed the time-options portion of setup
yet (this normally can't happen post-1.1.3 — slots are game-defined
constants — but the guard remains for guilds whose timezone hasn't
been parseable yet):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Both Desert Storm time slots need to be configured before        │
│ posting a sign-up. Run /setup_desertstorm and pick the two times    │
│ first.                                                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

For CS:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The Canyon Storm time slot needs to be configured before posting │
│ a sign-up. Run /setup_canyonstorm and pick the time first.          │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

---

### Screen 4.13 — Forbidden (bot can't post in the configured channel)

Channel exists but the bot doesn't have `Send Messages` permission:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ I don't have permission to send messages in #storm-signups.      │
│ Check the channel permissions and try again.                         │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

---

### Screen 4.14 — Send failed (other Discord error)

Any other `discord.HTTPException` during the channel send:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Discord refused the sign-up message: `<error code + message,     │
│ truncated to 120 chars>`. See bot logs for details.                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

Example:

```
⚠️ Discord refused the sign-up message: `400 Bad Request (error code:
50035): Invalid Form Body In components.0.components.0.…`. See bot
logs for details.
```

---

### Screen 4.15 — Unexpected status (defensive)

If `post_registration` returns an unknown status code (shouldn't
happen — defensive fallthrough):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Sign-up post returned unexpected status `<status>`.              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

---

### Default date inference

If Kevin omits `event_date`, the bot calls `next_event_date(guild_id,
event_type, today=today_local)`. This reads the alliance's
configured `event_day_of_week` from the structured config and returns
the next ISO date matching that day-of-week. For Apex with
`event_day_of_week=5` (Saturday) and `today=2026-05-14` (Thursday),
that resolves to `2026-05-18` (Saturday).

If `event_day_of_week` is `-1` (auto-scheduling skipped),
`next_event_date` falls back to the next ISO date corresponding to
its own heuristic — but the success message will still render with
whatever date got computed.

---

### Flow at a glance

```
Kevin runs /desertstorm post_signup [event_date:2026-05-18]
        │
        ▼
   Has leadership role / admin?
        │
        ├── no ──→ Screen 4.2 (⛔ denial)
        │
        ▼ yes
   Was event_date provided?
        │
        ├── no ──→ infer next_event_date(guild_id, "DS")
        │             │
        ├── yes ──→ parse_event_date(raw)
        │             │
        │             ├── unparseable ──→ Screen 4.3 (⚠️ try `May 18`, …)
        │             │
        │             ▼ parsed
        ▼ resolved date
   parsed_date < today_local?
        │
        ├── yes ──→ Screen 4.4 (⚠️ in the past)
        │
        ▼ no
   ensure_premium_structured(interaction, event_type)
        │
        ├── not premium ──→ Screen 4.5 (🔒 upsell)
        │
        ├── premium but flow off ──→ Screen 4.6 (⚠️ run setup_<event>)
        │
        ▼ ok
   Screen 4.7 (thinking…)
        │
        ▼
   post_registration(bot, guild, et, date)
        │
        ├── status: ok ─────────────→ post sign-up in #storm-signups
        │                              + Screen 4.8 (✅ live in #…)
        │
        ├── already_posted ─────────→ Screen 4.9 (ℹ️ exists in #…)
        │
        ├── no_channel ─────────────→ Screen 4.10 (⚠️ run setup_<event>)
        │
        ├── channel_gone ───────────→ Screen 4.11 (⚠️ pick new channel)
        │
        ├── missing_slot_labels ────→ Screen 4.12 (⚠️ pick times first)
        │
        ├── forbidden ──────────────→ Screen 4.13 (⚠️ check perms)
        │
        ├── send_failed ────────────→ Screen 4.14 (⚠️ Discord refused)
        │
        └── (other) ────────────────→ Screen 4.15 (⚠️ unexpected status)
```

---

Files referenced:

- `c:\Users\Kevin\Documents\GitHub\lw-alliance-helper\lw-alliance-helper-bot\setup_cog.py` — `run_storm_setup` (line 4574), `_run_structured_flow_setup_step` (line 5847), `_ask_signup_schedule` (line 5436), `_ask_judicator_role` (line 5715), `_KeepOrFlipYesNoGate` (line 5599), `_KeepOrChangeRoleGate` (line 5654), `ask_keep_or_change` (line 802), `ChannelSelectStep` (line 333), `YesNoView` (line 1748), `TIMEZONE_LABELS` (line 1661), `_DOW_NAMES` (line 5432).
- `c:\Users\Kevin\Documents\GitHub\lw-alliance-helper\lw-alliance-helper-bot\member_roster.py` — `sync_members` slash command (line 429), `write_roster` (line 177), `_ensure_discord_flag_column` (line 225), `_apply_discord_flag_validation` (line 289), `DISCORD_FLAG_COLUMN_HEADER` (line 44).
- `c:\Users\Kevin\Documents\GitHub\lw-alliance-helper\lw-alliance-helper-bot\storm_signup_post.py` — `storm_post_signup` slash command (line 218), `post_registration` (line 102), `_format_post_result_message` (line 306), `_build_registration_embed` (line 65).
- `c:\Users\Kevin\Documents\GitHub\lw-alliance-helper\lw-alliance-helper-bot\storm_permissions.py` — `deny_non_leader` (line 55), `ensure_premium_structured` (line 68).
- `c:\Users\Kevin\Documents\GitHub\lw-alliance-helper\lw-alliance-helper-bot\config.py` — `default_structured_tab` (line 1438), `_STRUCTURED_TAB_DEFAULTS` (line 1420).
- `c:\Users\Kevin\Documents\GitHub\lw-alliance-helper\lw-alliance-helper-bot\storm_date_helpers.py` — `format_event_date` (line 29).

Note on date examples: I followed the user's specified example value `Saturday, May 18, 2026` consistently per the brief, even though `2026-05-18` is actually a Monday — matches the convention set in the existing Flow 1.

---

## 5. `/desertstorm signups` + `/canyonstorm signups` — officer view

The slash command an officer (Kevin) types when they want to see who's
voted for an upcoming storm event. Lives in `storm_officer_view.py`;
gated by `is_leader_or_admin` + `ensure_premium_structured`.

### Screen 5.1 — The slash command, as Discord renders it

Kevin types `/desertstorm signups`. Discord shows the bot's command
description and the single optional parameter:

```
┌──────────────────────────────────────────────────────────────────────┐
│ /desertstorm signups                                                 │
│ Leadership view of who's signed up for an upcoming Desert Storm event│
│                                                                      │
│ event_date      Optional — defaults to the next configured event     │
│                 day. Accepts e.g. May 18, 5/18, Sunday.              │
└──────────────────────────────────────────────────────────────────────┘
```

`event_date` is free-text, parsed by
`storm_date_helpers.parse_event_date`. The CS form is identical under
`/desertstorm signups` — event type is encoded in the parent group, not
in an `event_type` argument.

---

### Screen 5.2 — Permission-denied (non-leader / non-admin)

Officer is missing the configured leadership role AND is not a server
admin (`is_leader_or_admin` returns False, then `deny_non_leader` fires):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to run this command.      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

No buttons. The command bails before any premium / parsing work runs.

---

### Screen 5.3 — Unparseable `event_date`

Officer typed `/desertstorm signups Storm event_date:nxt sat`.
`parse_event_date` returns None:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ `nxt sat` isn't a date I can parse. Try `May 18`, `5/18`,         │
│ `2026-05-18`, `Sunday`, or `tomorrow`.                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

---

### Screen 5.4 — Premium gate (alliance is on the free tier)

`ensure_premium_structured` finds the guild is not Premium. The
`feature_label` is the `/desertstorm signups` literal:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔒 `/desertstorm signups` is a 💎 Premium feature. Run `/upgrade` to       │
│ unlock it.                                                           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

---

### Screen 5.5 — Structured-flow opt-in gate

Alliance has Premium but hasn't toggled "Structured Roster Flow" in
`/setup_desertstorm`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The structured roster flow isn't enabled for Desert Storm. Run    │
│ `/setup_desertstorm` and turn on **Structured Roster Flow** first.   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

Mirror message for CS: `… isn't enabled for Canyon Storm. Run
/setup_canyonstorm and turn on …`.

---

### Screen 5.6 — DM-only invocation defense

Officer somehow invoked the command outside a guild
(`interaction.guild_id` is None):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ This command must be used inside a server.                        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

---

### Screen 5.7 — The bucket-map embed (happy path)

All gates passed. Bot deferred the interaction, pre-warmed the member
cache, read the roster Sheet via `_read_roster_rows`, joined with
`config.get_storm_signups`, and built the 5-bucket view via
`_build_bucket_map`. For Desert Storm, May 18, with 5 sign-ups:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔥 Desert Storm Sign-Ups — Sunday, May 18, 2026  (5 members)         │
│                                                                      │
│ **🅰️ Voted Team A** (2)                                              │
│ Alice, Bob                                                           │
│                                                                      │
│ **🅱️ Voted Team B** (1)                                              │
│ Carol                                                                │
│                                                                      │
│ **🔄 Voted Either** (1)                                              │
│ Dan _(on behalf)_                                                    │
│                                                                      │
│ **❌ Voted Cannot** (0)                                              │
│ _(none)_                                                             │
│                                                                      │
│ **❓ Not voted yet [1 not on Discord]** (1)                          │
│ Erin ¹                                                               │
│                                                                      │
│ ¹ Not on Discord — cast their vote with **🙋 Record on-behalf vote**.│
│                                                                      │
│ 🅰️ 2 · 🅱️ 1 · 🔄 1 · ❌ 0 · ❓ 1                                     │
└──────────────────────────────────────────────────────────────────────┘
[Filter bucket — currently: All ▾]
[🙋 Record on-behalf vote]  [🔄 Refresh]
[🅰️ Set up Team A]  [🅱️ Set up Team B]
```

Notes on the render:

- Title prefix is `🔥` for DS and `🏜️` for CS (set in `_render_embed`).
- Member counts in the title (`5 members`) sum every bucket.
- The footer (`🅰️ 2 · 🅱️ 1 · 🔄 1 · ❌ 0 · ❓ 1`) shows per-bucket
  counts again — kept compact so a long member list doesn't push it
  off-screen.
- `Dan _(on behalf)_` markup is appended by `_format_bucket_names`
  when the row has `is_on_behalf=True`. Renders italicised in Discord.
- The `¹` superscript marks Erin as not on Discord (she's a roster
  row flagged `not_on_discord` with no signup row). The footnote line
  near the bottom explains it.
- The `[1 not on Discord]` suffix on `Not voted yet` is appended only
  when the bucket actually contains a not-on-Discord entry.
- Embed colour is `gold()` for DS, `orange()` for CS.

---

### Screen 5.8 — Bucket-map embed, Canyon Storm variant

Same shape, different title prefix + colour, and only one Set-up button:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🏜️ Canyon Storm Sign-Ups — Sunday, May 18, 2026  (5 members)         │
│                                                                      │
│ **🅰️ Voted Team A** (3)                                              │
│ Alice, Bob, Carol                                                    │
│                                                                      │
│ **🅱️ Voted Team B** (0)                                              │
│ _(none)_                                                             │
│                                                                      │
│ **🔄 Voted Either** (1)                                              │
│ Dan                                                                  │
│                                                                      │
│ **❌ Voted Cannot** (1)                                              │
│ Erin                                                                 │
│                                                                      │
│ **❓ Not voted yet** (0)                                             │
│ _(none)_                                                             │
│                                                                      │
│ 🅰️ 3 · 🅱️ 0 · 🔄 1 · ❌ 1 · ❓ 0                                     │
└──────────────────────────────────────────────────────────────────────┘
[Filter bucket — currently: All ▾]
[🙋 Record on-behalf vote]  [🔄 Refresh]
[🏜️ Set up Roster]
```

CS has a single Set-up button (`🏜️ Set up Roster`) instead of the
per-team pair — faction is implicit in the chosen preset.

---

### Screen 5.9 — DS Team-A-only alliance (`teams=A`)

DS structured config has `teams="A"`. The Set-up Team B button is
suppressed:

```
…
[🅰️ Set up Team A]
```

Mirror: `teams=B` shows only `[🅱️ Set up Team B]`. `teams=both` (the
default) shows both.

---

### Screen 5.10 — Roster-Sheet read error preface

`_read_roster_rows` returned non-empty `roster_errors`. The bot
prepends a content line above the embed:

```
⚠️ Roster Sheet read had issues — non-Discord member enumeration may
be incomplete: roster-sheet read failed: APIError: 503 Service
Unavailable
┌──────────────────────────────────────────────────────────────────────┐
│ 🔥 Desert Storm Sign-Ups — Sunday, May 18, 2026  (3 members)         │
│ …                                                                    │
└──────────────────────────────────────────────────────────────────────┘
```

The preface joins up to 2 errors with ` · `. Common error strings the
officer might actually see:

- `roster-config read failed: <Python repr>`
- `roster-sheet open failed: <gspread error>`
- `roster-sheet read failed: <gspread error>`
- `stale Discord IDs on roster (member likely left the server): Alice
  (id 1234), Bob (id 5678)` (truncated with `(+N more)` past 5)

---

### Screen 5.11 — Filter dropdown opened

Kevin clicks the `Filter bucket — currently: All ▾` select:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Filter bucket — currently: All                              ▾        │
│                                                                      │
│ All buckets                                                          │
│ 🅰️ Voted Team A                                                      │
│ 🅱️ Voted Team B                                                      │
│ 🔄 Voted Either                                                      │
│ ❌ Voted Cannot                                                      │
│ ❓ Not voted yet                                                     │
└──────────────────────────────────────────────────────────────────────┘
```

If Kevin picks `❓ Not voted yet`, the embed redraws with only that
bucket and the placeholder updates to
`Filter bucket — currently: not_voted`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔥 Desert Storm Sign-Ups — Sunday, May 18, 2026  (5 members)         │
│                                                                      │
│ **❓ Not voted yet [1 not on Discord]** (1)                          │
│ Erin ¹                                                               │
│                                                                      │
│ ¹ Not on Discord — cast their vote with **🙋 Record on-behalf vote**.│
│                                                                      │
│ 🅰️ 2 · 🅱️ 1 · 🔄 1 · ❌ 0 · ❓ 1                                     │
└──────────────────────────────────────────────────────────────────────┘
```

The filter only mutates display; the footer count line still shows
all five buckets so Kevin doesn't lose the global picture.

---

### Screen 5.12 — Non-owner clicks the filter

A second officer (not Kevin) clicks the dropdown:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who opened this view can change the filter.      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only the second officer sees it)
```

---

### Screen 5.13 — Non-owner clicks Refresh / On-behalf / Set-up

Same `owner_user_id` guard, slightly different copy per button:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who opened this view can refresh.                │
└──────────────────────────────────────────────────────────────────────┘
```
```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who opened this view can record on-behalf votes  │
│ here.                                                                │
└──────────────────────────────────────────────────────────────────────┘
```
```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who opened this view can start team setup.       │
└──────────────────────────────────────────────────────────────────────┘
```
All ephemeral, only the clicker sees them.

---

### Screen 5.14 — Refresh button (happy path)

Kevin clicks `🔄 Refresh`. The bot defers, re-reads the roster Sheet
+ signups table, and edits the message in place. No new message; the
embed body and footer counts update silently.

---

### Screen 5.15 — Truncated buckets EDITED

A long member list pushed the description past `_DESCRIPTION_BUDGET`
(3800 chars). One or more buckets after the cap are dropped from the
default "All" view and the footnote at the bottom flags it:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔥 Desert Storm Sign-Ups — Sunday, May 18, 2026  (87 members)        │
│                                                                      │
│ **🅰️ Voted Team A** (42)                                             │
│ Alice, Bob, Carol, Dan, Erin, …, … (+12 more)                        │
│                                                                      │
│ **🅱️ Voted Team B** (38)                                             │
│ …                                                                    │
│                                                                      │
│ _Some buckets clipped — use the filter dropdown see all votes.       │
│                                                                      │
│ 🅰️ 42 · 🅱️ 38 · 🔄 4 · ❌ 2 · ❓ 1                                  │
└──────────────────────────────────────────────────────────────────────┘
```

The `(+N more)` overflow hint is produced by `_format_bucket_names`
when an individual bucket exceeds `_BUCKET_BUDGET` (900 chars).

---

### Screen 5.16 — Empty event (no votes yet, no roster)

Brand-new alliance, no roster sync configured, no signups recorded:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔥 Desert Storm Sign-Ups — Sunday, May 18, 2026  (0 members)         │
│                                                                      │
│ **🅰️ Voted Team A** (0)                                              │
│ _(none)_                                                             │
│                                                                      │
│ **🅱️ Voted Team B** (0)                                              │
│ _(none)_                                                             │
│                                                                      │
│ **🔄 Voted Either** (0)                                              │
│ _(none)_                                                             │
│                                                                      │
│ **❌ Voted Cannot** (0)                                              │
│ _(none)_                                                             │
│                                                                      │
│ **❓ Not voted yet** (0)                                             │
│ _(none)_                                                             │
│                                                                      │
│ 🅰️ 0 · 🅱️ 0 · 🔄 0 · ❌ 0 · ❓ 0                                     │
└──────────────────────────────────────────────────────────────────────┘
```

Buttons still render so the officer can immediately fire on-behalf
votes for non-Discord members.

---

### Screen 5.17 — Set-up Team A → preset picker EDITED

Kevin clicks `🅰️ Set up Team A`. The bot loads `ss.list_presets`. If
no presets exist:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No strategy presets defined yet for Desert Storm. Run             │
│ `/desertstorm strategy create` first.                                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

(CS variant swaps the label: `Canyon Storm`.)
NEW: CS variant swaps `/desertstorm` to `/canyonstorm`

If presets exist, the `_PresetPickerView` ephemeral fires:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Select a strategy preset to apply for **Team A**:                    │
└──────────────────────────────────────────────────────────────────────┘
[Select a preset…                                                     ▾]
```

Dropdown contains up to 25 saved preset names (`Standard DS`,
`CS Standard`, etc.).

For CS the team_label is `this roster`:

```
Select a strategy preset to apply for **this roster**:
```

---

### Screen 5.18 — Preset picked

Kevin picks `Standard DS`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Preset **Standard DS** selected — opening the roster builder…     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it; dropdown shown disabled)
```

Then `storm_roster_builder.open_roster_builder` takes over — that
flow is documented under Flow 7.

---

### Screen 5.19 — Non-owner picks a preset

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the user who started team setup can pick.                    │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only the non-owner sees it)
```

---

### Screen 5.20 — View timeout

15 minutes after the officer view was sent, with no further
interaction, `OfferView.on_timeout` fires and `expire_view_message`
strips the buttons + appends the canonical timeout content:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔥 Desert Storm Sign-Ups — Sunday, May 18, 2026  (5 members)         │
│                                                                      │
│ … (embed body unchanged)                                             │
└──────────────────────────────────────────────────────────────────────┘
⏰ This view timed out — run `/desertstorm signups` again to refresh it.
(buttons stripped)
```

The exact timeout suffix is whatever `wizard_registry.expire_view_message`
emits with `command_hint="/desertstorm signups"`.

---

### Screen 5.21 — First-run walkthrough offer EDITED

Right after the embed lands, if Kevin hasn't dismissed the
`storm_signups` walkthrough yet, a second ephemeral fires
(from `maybe_offer_storm_signups_tour`):

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👋 First time using `/desertstorm signups`? Want a quick walkthrough of    │
│ what each piece does?                                                │
└──────────────────────────────────────────────────────────────────────┘
[👋 Walk me through this]  [No thanks]  [Ask again next time]
(ephemeral — only Kevin sees it)
```

If Kevin clicks **No thanks** the offer is dismissed forever and the
ephemeral updates to:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👍 Got it, won't ask again. Run `/help` any time and pick Desert     │
│ Storm or Canyon Storm if you want a refresher.                       │
└──────────────────────────────────────────────────────────────────────┘
```

ADDED: If Kevin clicks **Ask again next time** the offer is dismissed and the
ephemeral updates to:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👍 Got it, will ask you again next time you run                      │
│ `/deserstorm signups`.                                               │
└──────────────────────────────────────────────────────────────────────┘
```

If Kevin clicks **Walk me through this**:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Starting the tour…                                                │
└──────────────────────────────────────────────────────────────────────┘
```

Then the tour steps fire sequentially as separate ephemerals. The tour
content itself is out of scope for this flow.

If Kevin's already a returning officer (walkthrough previously
dismissed), no offer appears at all.

---

### Flow at a glance

```
Kevin types  /desertstorm signups  [event_date:Saturday, May 18, 2026]
                     │
       ┌─────────────┼────────────────────┐
       ▼             ▼                    ▼
   Not leader   Bad date string     Not Premium / not
   Screen 5.2   Screen 5.3          structured-enabled
                                    Screens 5.4 / 5.5
       │             │                    │
       └─────────────┴────────────────────┘
                     │  (all gates passed)
                     ▼
       Bot defers · pre-warms member cache · reads
       roster Sheet · joins signups · builds buckets
                     │
                     ▼
       Screen 5.7  (DS) or  5.8  (CS) — bucket-map embed
        + button row (refresh, on-behalf, Set up Team A/B
          or Set up Roster, gated by teams config)
                     │
                     ├──→ Screen 5.10 — roster errors prefix
                     ├──→ Screen 5.15 — clipped buckets
                     ├──→ Screen 5.21 — first-run tour offer
                     │
        ┌────────────┼────────────────────┐
        ▼            ▼                    ▼
   🔄 Refresh    🙋 Record on-behalf    🅰/🅱/🏜 Set up
   re-reads     → Flow 6 modal          → Screen 5.17 preset
   in place                               picker → Flow 7
                                          builder
        │
        └─→  Screen 5.20  view timeout after 15 min
```

---

## 6. On-behalf vote picker

Triggered when Kevin clicks `🙋 Record on-behalf vote` on the officer
view. Per #168 / Rule E + Decision #2, the pre-existing free-text
modal (`_OnBehalfModal`) is replaced by an ephemeral view-based
picker — Member Select sourced from the roster Sheet, Vote Select
that mirrors the sign-up buttons, Submit + Cancel + paging. Defined
in `storm_officer_view._OnBehalfVoteView`.

The clicker hits the button. The bot defers, reads the roster off
the event loop, and sends the picker as a followup ephemeral.

### Screen 6.1 — The on-behalf picker

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🙋 Pick a member and a vote, then **Submit**. Only roster members   │
│ are listed — `/sync_members` refreshes the list.                     │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Pick a member…                                                  ]
[ ▾ Pick a vote…                                                    ]
[✅ Submit (disabled)]  [↩️ Cancel]
(ephemeral — only Kevin sees it)
```

The **Member Select** is sourced from the roster Sheet via
`_read_roster_rows`, de-duped case-insensitively, sorted, and
filtered of purely-numeric names (those would collide with Discord
user IDs in `storm_signups.target_member_id` — the schema-collision
risk vanishes because the picker can't offer the option).

The **Vote Select** is built from `get_storm_slot_labels(event_type,
guild_id)` so its options match the wording members see on the
sign-up post. Options branch on `cfg.teams`:

- `teams=both` → `Team A: 4pm EDT (18:00 server time)`,
  `Team B: 9pm EDT (23:00 server time)`, `Either time works`,
  `Cannot participate` (4 options for DS — CS uses its own
  slot times).
- `teams=A` → `Team A: …`, `Cannot participate`.
- `teams=B` → `Team B: …`, `Cannot participate`.

**Submit** stays disabled until both selects have a value.

### Screen 6.1a — Paging (roster > 25 members)

When the roster carries more than 25 members, a paging row renders
below the Vote Select:

```
[ ▾ Pick a member… (page 1)                                         ]
[ ▾ Pick a vote…                                                    ]
[◀ Prev (disabled)]  [Page 1 / 3 (disabled)]  [Next ▶]
[✅ Submit (disabled)]  [↩️ Cancel]
```

Prev / Next swap the Member Select's options in place. The Page X/Y
label-only button is disabled — it's a status indicator, not a
control.

### Screen 6.2 — Submit success

Kevin picks **Erin** + **Cannot participate**, hits Submit. The bot
defers, calls `config.record_storm_vote(...)` with `is_on_behalf=True`,
re-reads buckets, edits the parent officer-view embed in place, then
fires the success ephemeral:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Recorded on-behalf vote for **Erin**.                             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it; the picker disables its buttons)
```

Meanwhile, the parent `/desertstorm signups` embed updates: Erin moves
from the `❓ Not voted yet` bucket into `❌ Voted Cannot` and the entry
now reads `Erin ¹ _(on behalf)_` (the `¹` from the not-on-Discord
flag, the italic `(on behalf)` from `is_on_behalf=True`).

### Screen 6.3 — Vote-write failure

`config.record_storm_vote` returned False (e.g. DB locked, unique
constraint hit on a race). The bot logs the underlying SQLite error;
the officer sees the short message:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't record that vote. Check the bot logs.                    │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

### Screen 6.4 — Cancel

```
┌──────────────────────────────────────────────────────────────────────┐
│ ↩️ Cancelled — no vote recorded.                                     │
└──────────────────────────────────────────────────────────────────────┘
```

### Screen 6.5 — Roster read fails entirely

The pre-#168 modal had a permissive fallback that accepted any
free-text name when the roster Sheet read returned no rows. The new
view-based picker can't populate the Member Select without a roster
read, so on read failure it surfaces an actionable error and bails:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't read the roster right now. Try `/sync_members` and       │
│ reopen this view to retry.                                           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it; no picker renders)
```

The roster-error prefix is also visible on the `/desertstorm signups`
embed (Screen 5.10) so the officer has context.

### Flow at a glance

```
Kevin clicks  🙋 Record on-behalf vote  (from Flow 5.7 embed)
                       │
                       ▼   (defer + threaded gspread read)
            ┌────────────────────────┐
            │ roster_rows empty?     │
            └──────────┬─────────────┘
                       │
              yes ─────┘─── no
               │              │
               ▼              ▼
        Screen 6.5     Screen 6.1 — picker opens
        Read failed    ┌───────────────────────────┐
                       │ Member Select: __________ │
                       │ Vote   Select: __________ │
                       │ (paging if > 25)          │
                       │ [Submit (disabled)]       │
                       │ [Cancel]                  │
                       └───────────┬───────────────┘
                                   │
                  Kevin picks both ┘
                                   │
                              clicks Submit
                                   │
                                   ▼  (defer + record_storm_vote)
                       ┌───────────┴───────────┐
                       ▼                       ▼
                   Write OK              Write failed
                   refresh parent        Screen 6.3
                   embed,
                   Screen 6.2 success
                   ephemeral
```

---

## 7. Roster builder

Opened by either the Set-up-Team buttons on `/desertstorm signups` (Flow 5)
or by the free-tier `/desertstorm strategy apply` / `/canyonstorm strategy apply`
slash commands. Lives in `storm_roster_builder.py`
(`RosterBuilderView` + friends).

The builder runs in two modes, branched on whether `event_date` is
set:

- **Structured mode** (Premium, called from Flow 5): member pool is
  pre-filtered to the team's sign-ups; the Approve & Post button is
  available; the rosters_tab Sheet write fires on approve.
- **Free-tier "apply" mode** (no event date): full alliance roster
  becomes the pool; no Approve & Post; the officer copies the mail
  manually.

Plus a phase-aware variant (`preset.phase_count >= 2`) that adds
phase-nav buttons and per-phase capacity readouts, and a paired-sub
variant (`sub_mode="paired"`) that opens a sub-picker after every
primary assignment. Both can compose — a phase-aware paired-mode
preset gets both surfaces.

---

### Screen 7.1 — DS apply: team picker

Kevin typed `/desertstorm strategy apply name:Standard DS`. There's no
`team_override`, so the builder asks first:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Build roster for **Team A** or **Team B** with preset               │
│ **Standard DS**?                                                     │
└──────────────────────────────────────────────────────────────────────┘
[🅰️ Team A]  [🅱️ Team B]
(ephemeral — only Kevin sees it)
```

Picks update the ephemeral content:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Team A selected.                                                  │
└──────────────────────────────────────────────────────────────────────┘
(buttons disabled)
```

Or:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Team B selected.                                                  │
└──────────────────────────────────────────────────────────────────────┘
```

Non-owner clicking either:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who started the apply can pick.                  │
└──────────────────────────────────────────────────────────────────────┘
```

Timeout (2 min, no click): buttons silently disable on the original
message. Kevin re-runs the slash command to retry.

For CS, no team picker — the faction is encoded in the preset.

When `team_override` is supplied (the structured-mode path from the
officer view's `🅰️ Set up Team A` button), Screen 7.1 is skipped.

---

### Screen 7.2 — Preset-not-found EDITED
NOTE: Add a button to show the list of them from here.

Kevin typed `/desertstorm strategy apply name:foo` and there's no preset
called `foo`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No preset named **foo**.                                          │
└──────────────────────────────────────────────────────────────────────┘
[List saved strategies]
(ephemeral — only Kevin sees it)
```

---

### Screen 7.3 — Preset has no zones EDITED
NOTE: Add a button to allow them to go into Editing from here.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Preset **Standard DS** has no zones yet. Edit it first to add    │
│ zones before applying.                                               │
└──────────────────────────────────────────────────────────────────────┘
[Edit Standard DS]
(ephemeral — only Kevin sees it)
```

---

### Screen 7.4 — DM-only invocation defense

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ This command must be used inside a server.                        │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 7.5 — Premium gate (structured mode)

Same wording as Flow 5, with the feature_label changed:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔒 The structured roster builder is a 💎 Premium feature. Run        │
│ `/upgrade` to unlock it.                                             │
└──────────────────────────────────────────────────────────────────────┘
```

And the structured-flow gate:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The structured roster flow isn't enabled for Desert Storm. Run    │
│ `/setup_desertstorm` and turn on **Structured Roster Flow** first.   │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 7.6 — Structured-mode pool empty

In structured mode, `_signup_filter_keys` filters the member pool to
voters compatible with this team. If no signed-up members match the
roster:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No signed-up members match team **A** for event **Sunday, May    │
│ 18, 2026**. Check `/desertstorm signups` to see who's voted, or run the   │
│ apply flow without an event date to use the full roster.            │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

CS shows `team **A**` too (CS's signup compat is `A` or `Either`),
falling back to `A` when the call sets `team=""`. For a free-tier
apply, this gate never trips (the whole roster is the pool).

---

### Screen 7.7 — Session already locked by another officer

In structured mode, the bot tries `config.claim_storm_session` to
prevent two officers from building the same team concurrently. If
that returns False:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Another officer (<@482910011110000111>) is already building       │
│ **Team A** for event **Sunday, May 18, 2026**. Wait for them to     │
│ finish, or coordinate before re-opening.                             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it; mention is suppressed via
AllowedMentions.none())
```

The mention renders as a clickable @username in Discord (not a ping).

---

### Screen 7.8 — The builder embed (flat, pool mode, structured) EDITED

The happy-path embed Kevin sees after preset + team are resolved.
Apex has a flat `Standard DS` preset with the six zones the user
spec'd, sub_mode=pool, Team A. Sample data — auto-fill hasn't fired
yet, so the embed reflects whatever the pre-applied `per_member` rules
seeded.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: Standard DS — Team A                              │
│                                                                      │
│ 🗺️ Desert Storm                                                      │
│ ⚖️ Enforcing **Min A** minimum for this team                         │
│                                                                      │
│ **📋 Zones**                                                         │
│ ⬜ **Power Tower** (0/4): (empty)                                    │
│ ⬜ **Nuclear Silo** (0/4): (empty)                                   │
│ ⬜ **Info Center** (0/4): (empty)                                    │
│ ⬜ **Field Hospital** (0/4): (empty)                                 │
│ ⬜ **Mercenary** (0/2): (empty)                                      │
│ ⬜ **Arsenal** (0/2): (empty)                                        │
│                                                                      │
│ 🪑 **Subs**: _(none)_                                                │
│                                                                      │
│ 📊 **Filled:** 0 / 20                                                │
│ 🎯 **Active zone:** **Power Tower** — minimum **300M**               │
└──────────────────────────────────────────────────────────────────────┘
[Pick a zone to edit…                                                ▾]
[Pick a member for Power Tower…                                      ▾]
[👁️ Show members below minimum]  [↩️ Remove current zone assignees]
[🪑 Add all unassigned to Subs]  [🎯 Auto-fill]

[✅ Approve & Post]  [📄 Preview mail]  [🖼️ Generate DS assignments image]  [❌ Cancel]
```

Key surface details:

- Title: `🛡️ Roster Builder: Standard DS — Team A`. (For CS, the
  `— Team A` is replaced by the preset's faction, e.g.
  `— Rulebringers`, or omitted entirely when `faction=Either`.)
- `🗺️ Desert Storm` / `🗺️ Canyon Storm` line opens the body.
- `⚖️ Enforcing **Min A** floors for this team` only renders for DS
  (CS uses a single per-zone floor in `min_power_a`); the floor label
  flips to **Min B** when team=B.
- Per-zone status glyph: `⬜` empty, `🟡` partial, `✅` full, `—` for
  a phase with zero capacity at this zone.
- `(0/4)` capacity readout. The currently selected zone gets a `←`
  marker after its parens (e.g. `(0/4) ←`) but the marker is on the
  Active-zone line below, not duplicated inline. (Actually both:
  zone-line marker `←` and the `🎯 Active zone:` summary line below.)
- `🪑 **Subs**: _(none)_` for an empty sub pool; populated as
  `🪑 **Subs (3)**: Erin, Frank, Greg` once auto-fill or manual
  moves seed the pool.
- `📊 **Filled:** 0 / 20` — sums every assignment / capacity across
  every phase (just one phase here).
- `🎯 **Active zone:**` line shows the floor (or `(none)` when
  `min_power_a=0`). When a `power_band` rule has relaxed the floor:
  `🎯 **Active zone:** **Power Tower** — floor **180M** _(preset floor 300M relaxed by power_band rule)_`.
- The optional unknown-power hint at the bottom of the zone block:
  `_Members with no parseable power read as 'power unknown'; toggle the override to assign them anyway._`
  — only appears when any roster row has `power=None`.

Buttons in `row` order:

- Row 0: `[Pick a zone to edit…]` (Select)
- Row 1: `[Pick a member for <zone>…]` (Select)
- Row 2: `[👁️ Show members below minimum]` / `[↩️ Remove current zone assignees]`
- Row 3: `[🪑 Add all unassigned to Subs]` + `[🎯 Auto-fill]` (structured only)
- Row 4: Blank row for spacing
- Row 5: `[✅ Approve & Post]` `[📄 Preview mail]` `[🖼️ Generate DS assignments image]`
  `[❌ Cancel]` (structured) — or `[📄 Generate mail]`
  `[💾 Save as preset]` `[🖼️ Generate DS assignments image]` `[✅ Done]` (free-tier).

---

### Screen 7.9 — Builder embed, after assignments EDITED - MIMIC 7.8

Kevin picked Power Tower, then Alice (412M), then Bob (380M). Picked
Info Center, then Carol (350M). Auto-fill has not been clicked. The
embed redraws after every action:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: Standard DS — Team A                              │
│                                                                      │
│ 🗺️ Desert Storm                                                      │
│ ⚖️ Enforcing **Min A** minimum for this team                         │
│                                                                      │
│ **📋 Zones**                                                         │
│ 🟡 **Power Tower** (2/4): Alice, Bob                                 │
│ ⬜ **Nuclear Silo** (0/4): (empty)                                   │
│ 🟡 **Info Center** (1/4) ←: Carol                                    │
│ ⬜ **Field Hospital** (0/4): (empty)                                 │
│ ⬜ **Mercenary** (0/2): (empty)                                      │
│ ⬜ **Arsenal** (0/2): (empty)                                        │
│                                                                      │
│ 🪑 **Subs**: _(none)_                                                │
│                                                                      │
│ 📊 **Filled:** 3 / 20                                                │
│ 🎯 **Active zone:** **Info Center** — floor **200M**                 │
└──────────────────────────────────────────────────────────────────────┘
```

The `←` marker tracks `selected_zone` (now Info Center). Zone status
went from `⬜` → `🟡` once at least one member was assigned but the
zone is still below capacity.

---

### Screen 7.10 — Member picker dropdown EDITED
NOTE: Change Floor to Minimum in general - more user friendly

Kevin opened the member dropdown for Info Center. Eligible-only:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Pick a member for Info Center…                                  ▾    │
│                                                                      │
│ Alice (412M)                                                         │
│ Bob (380M)                                                           │
│ Carol (350M)                                                         │
│ Dan (320M)                                                           │
│ Erin (300M)                                                          │
└──────────────────────────────────────────────────────────────────────┘
```

When the toggle is on (`👁️ Hide members below minimum` showing), below-floor
members are appended with a description annotation:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Pick a member for Info Center…                                  ▾    │
│                                                                      │
│ Alice (412M)                                                         │
│ Bob (380M)                                                           │
│ …                                                                    │
│ Hank (90M)                       below minimum                       │
│ Ivan (power unknown) ¹           below minimum                       │
└──────────────────────────────────────────────────────────────────────┘
```

When zero members are eligible (everyone is below the floor and the
toggle is off):

```
[No eligible members — toggle below-floor override                  ▾]
```

When more than 25 candidates qualify (Discord's Select limit):

```
[Pick a member for Info Center… (+4 more)                            ▾]
```

If Kevin somehow picks a below-floor option without the toggle (race
case — option shouldn't even be in the pool):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Toggle the below minimum override to assign this member.          │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

If the picker is opened with no zone selected (degenerate state —
shouldn't happen in practice):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Pick a zone first.                                                │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 7.11 — Zone-full guard

Kevin tries to assign a 5th member to Power Tower (cap 4):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ **Power Tower** is already full (4 members). Unassign someone     │
│ before adding another.                                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

---

### Screen 7.12 — Below-floor toggle button states EDITED

Default state — toggle is off:

```
[👁️ Show members below minimum]
```

After click — toggle is on, label inverts:

```
[👁️ Hide members below minimum]
```

A new line surfaces in the embed under the Active zone line:

```
👁️ Members below minimum visible in the picker.
```

(Plus the picker pool now includes below-floor + power-unknown rows.)

Toggle resets to off when the officer switches zones (so a member
override doesn't accidentally carry over to a zone where it wasn't
intended).

---

### Screen 7.13 — Unassign-zone EDITED

Kevin clicks `↩️ Remove current zone assignees`. The selected zone's
assignment for the current phase is cleared. Embed re-renders with
the zone showing `⬜ (empty)`. Paired-sub pairings whose primaries
are now gone get pruned (`prune_stale_pairings`); below-floor flags
for those members get pruned too. No ephemeral fires.

If Kevin clicks unassign without a zone selected (degenerate state):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Pick a zone first.                                                │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 7.14 — Last-to-subs EDITED

Kevin clicks `🪑 Add all unassigned to Subs`. The last-added member of the selected
zone moves to the flat sub pool. Embed updates:

```
🟡 **Power Tower** (1/4): Alice
…
🪑 **Subs (1)**: Bob
```

If the zone has no members yet:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No members in this zone to move.                                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

---

### Screen 7.15 — Auto-fill (structured mode only)

Kevin clicks `🎯 Auto-fill`. The bot runs `_auto_fill_session`,
which resets every phase's assignments + subs + pairings before
filling. Embed re-renders with the auto-fill summary block appended
— that block is documented in Flow 8.

If the algorithm raises an unexpected exception:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Auto-fill hit an unexpected error: `ValueError: invalid literal   │
│ for int() with base 10: 'foo'`. Please share this message with the  │
│ bot maintainer; logs have details.                                   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

The exception type and (truncated) message are surfaced rather than
swallowed silently.

---

### Screen 7.16 — Preview mail (structured) / Generate mail (free-tier)

Identical content; the button label differs by mode (`📄 Preview mail`
under Approve flow vs `📄 Generate mail` as the primary action under
free-tier). Both call `_send_mail_preview`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📄 **Mail preview** — copy and paste into your alliance's mail       │
│ system:                                                              │
│ ```                                                                  │
│ **Desert Storm**                                                     │
│                                                                      │
│ **Zone Assignments**                                                 │
│ **Power Tower**                                                      │
│ Alice                                                                │
│ Bob                                                                  │
│ Carol                                                                │
│ Dan                                                                  │
│                                                                      │
│ **Nuclear Silo**                                                     │
│ Erin                                                                 │
│ Frank                                                                │
│ Greg                                                                 │
│ Hank                                                                 │
│                                                                      │
│ **Info Center**                                                      │
│ Ivan                                                                 │
│ Jack                                                                 │
│ Kim                                                                  │
│ Liam                                                                 │
│                                                                      │
│ **Field Hospital**                                                   │
│ Mona                                                                 │
│ Nina                                                                 │
│ Omar                                                                 │
│ Pete                                                                 │
│                                                                      │
│ **Mercenary**                                                        │
│ Quinn                                                                │
│ Riley                                                                │
│                                                                      │
│ **Arsenal**                                                          │
│ Sam                                                                  │
│ Tia                                                                  │
│                                                                      │
│ **Subs**                                                             │
│ (none)                                                               │
│                                                                      │
│ **Time:** 4pm EDT (18:00 server time)                                │
│ ```                                                                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

The body inside the triple backticks is `_build_mail_body`'s output,
truncated at 1880 chars with `\n…(truncated)` appended when over the
1900-char budget.

For phase-aware presets the body is split into per-phase blocks
joined by blank lines:

```
**Phase 1**

**Desert Storm**
…

**Phase 2**

**Desert Storm**
…
```

CS uses `**Canyon Storm**` and `**Subs**` (no `Pairs` suffix) by the
same builder.

---

### Screen 7.17 — Render image EDITED

Kevin clicks `🖼️ Generate DS assignments image`. Bot defers ephemerally, then calls
`storm_renderer.render` on a thread executor. Happy path:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🖼️ DS assignments image attached:                                    │
│ [ds-roster-2026-05-18-team-A.png] (attachment)                       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

Filename pattern:
`{event_type_lower}-roster[-{event_date}][-team-{team}].png` — so a
free-tier apply produces `ds-roster.png`, structured-mode is
`ds-roster-2026-05-18-team-A.png`, CS is
`cs-roster-2026-05-18.png`.

Pillow-missing fallback:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Image render isn't available — the host is missing Pillow. Use    │
│ the text-template mail in the meantime.                              │
└──────────────────────────────────────────────────────────────────────┘
```

Generic Pillow error:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't render the roster image — see bot logs.                  │
└──────────────────────────────────────────────────────────────────────┘
```

> 25 MB output:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Rendered roster image is too large to attach (27 MB > 25 MB       │
│ Discord limit). Use the text-template mail instead.                  │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 7.18 — Save as preset modal (free-tier only)

Kevin clicks `💾 Save as preset`. Modal opens:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Save as preset                                                 [X]   │
│                                                                      │
│ Preset name (overwrites if exists)                                   │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ Standard DS                                                    │   │
│ └────────────────────────────────────────────────────────────────┘   │
│                                                                      │
│                                          [Cancel]    [Submit]        │
└──────────────────────────────────────────────────────────────────────┘
```

- Title: `Save as preset`.
- Field label: `Preset name (overwrites if exists)`, default
  pre-filled with the current preset's name (so save-as-yourself is a
  one-click flow), `max_length=60`.

Blank submit:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Preset name is required.                                          │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

Save success:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved roster as preset **Standard DS**.                           │
└──────────────────────────────────────────────────────────────────────┘
```

Save failure (Sheet not configured / no edit perms):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't save preset — check that your Sheet is configured and    │
│ the bot has edit access.                                             │
└──────────────────────────────────────────────────────────────────────┘
```

The saved preset preserves phase-shape — if the source session was a
phase-aware preset, the new one is too.

---

### Screen 7.19 — Approve & Post (structured mode)

Kevin clicks `✅ Approve & Post`. The bot defers, re-reads roster
powers (so `power_at_assignment` reflects the value at approval time,
not session open), builds the mail, posts to the configured channel,
writes one row per slot to the rosters_tab.

Public ack (edits the original builder message, embed unchanged):

```
✅ Structured roster approved and posted.
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: Standard DS — Team A                              │
│ …                                                                    │
└──────────────────────────────────────────────────────────────────────┘
(buttons disabled)
```

Ephemeral detail to Kevin — happy path (mail posted OK):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster posted.                                                    │
│ 📬 Mail sent to #storm-signups.                                      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

No post channel configured:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster recorded.                                                  │
│ ⚠️ No post channel is configured — mail was built but not sent. Run  │
│ `/setup_desertstorm` (or `/setup_canyonstorm`) to pick one, or copy  │
│ the mail manually below.                                             │
│                                                                      │
│ ```                                                                  │
│ **Desert Storm**                                                     │
│ …                                                                    │
│ ```                                                                  │
└──────────────────────────────────────────────────────────────────────┘
```

Channel was deleted / bot can't see it:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster recorded.                                                  │
│ ⚠️ The configured post channel (<#1145990011110000111>) is deleted   │
│ or the bot can't see it. Re-run setup to pick a new channel — mail   │
│ preview below.                                                       │
│                                                                      │
│ ```                                                                  │
│ …                                                                    │
│ ```                                                                  │
└──────────────────────────────────────────────────────────────────────┘
```

Channel exists but Discord rejected the send (perms, rate limit):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster recorded.                                                  │
│ ⚠️ The configured post channel <#1145990011110000111> rejected the   │
│ send: `Forbidden 403 Missing Permissions`. Check the bot's          │
│ permissions in that channel — mail preview below.                    │
│                                                                      │
│ ```                                                                  │
│ …                                                                    │
│ ```                                                                  │
└──────────────────────────────────────────────────────────────────────┘
```

Sheet-write soft error appended below any of the above:

```
⚠️ rosters tab append failed: APIError: 503 Service Unavailable
```

---

### Screen 7.20 — Faction-roles offer (CS Approve & Post, Judicator configured) EDITED - REMOVE BASED ON PREVIOUS ANNOTATIONS ABOVE

Right after a CS approve posts successfully, if the alliance has
`judicator_role_id` set in structured config AND the roster has at
least one Judicator candidate (per a `per_member.special_role=judicator`
rule):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚔️ **Apply Faction Roles?**                                          │
│ Matchmaking will reveal your faction post-roster. When you know     │
│ it's **Rulebringers**, click below to apply the configured           │
│ Judicator role to your candidates: Alice, Bob.                       │
└──────────────────────────────────────────────────────────────────────┘
[⚔️ Rulebringers — apply Judicator]  [🛡️ Dawnbreakers — no role to apply]
(ephemeral — only Kevin sees it)
```

If Kevin clicks `🛡️ Dawnbreakers — no role to apply`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Dawnbreakers acknowledged — no role to apply.                     │
└──────────────────────────────────────────────────────────────────────┘
(buttons disabled)
```

If Kevin clicks `⚔️ Rulebringers — apply Judicator`, the bot defers
and walks the candidate list. Several preflight / per-member outcomes:

Configured role no longer exists:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The configured Judicator role (<@&1146000011110000222>) no       │
│ longer exists or the bot can't see it. Re-run `/setup_canyonstorm`   │
│ to pick a new one.                                                   │
└──────────────────────────────────────────────────────────────────────┘
```

Bot lacks Manage Roles:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ I don't have **Manage Roles** in this server, so I can't apply    │
│ the Judicator role. Grant the permission to my role and try again.   │
└──────────────────────────────────────────────────────────────────────┘
```

Role hierarchy block:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ The Judicator role (<@&1146000011110000222>) sits at or above my  │
│ own role in the hierarchy, so Discord won't let me assign it. In     │
│ **Server Settings → Roles**, move my role above the Judicator role   │
│ and try again.                                                       │
└──────────────────────────────────────────────────────────────────────┘
```

Could not resolve the bot's own member:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't resolve the bot's own member in this guild.              │
└──────────────────────────────────────────────────────────────────────┘
```

Could not resolve the guild:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't resolve the guild.                                       │
└──────────────────────────────────────────────────────────────────────┘
```

Happy-path apply summary (some applied, some already had it, some
not on Discord, some failed):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Judicator role applied:                                           │
│   • Applied to: Alice, Bob                                           │
│   • Already had the role: Carol                                      │
│   • Not on Discord / not in server: Dan                              │
│   • Failed: Erin (missing permission)                                │
└──────────────────────────────────────────────────────────────────────┘
```

If zero candidates actually applied (e.g. everyone already had the
role):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ℹ️ Judicator role apply — nothing to apply:                          │
│   • Already had the role: Alice, Bob                                 │
└──────────────────────────────────────────────────────────────────────┘
```

Total no-op (every candidate either already had it or wasn't on
Discord — falls through both branches):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ℹ️ No role applications needed — all candidates either already had   │
│ the role or weren't on Discord.                                      │
└──────────────────────────────────────────────────────────────────────┘
```

Non-owner click on either button:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who approved the roster can apply faction roles. │
└──────────────────────────────────────────────────────────────────────┘
```

…and for the Dawnbreakers button:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who approved the roster can resolve faction      │
│ roles.                                                               │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 7.21 — Cancel / Done

Kevin clicks `❌ Cancel` (structured) or `✅ Done` (free-tier). The
view's children disable, the message content updates:

Structured:

```
Roster builder cancelled — nothing posted.
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: Standard DS — Team A                              │
│ …                                                                    │
└──────────────────────────────────────────────────────────────────────┘
(buttons disabled)
```

Free-tier:

```
Roster builder closed.
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: Standard DS — Team A                              │
│ …                                                                    │
└──────────────────────────────────────────────────────────────────────┘
(buttons disabled)
```

The session lock (structured only) releases automatically. The free-tier
variant has no lock to release.

Non-owner clicking any button (`_guard_owner`):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who opened this builder can use it.              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only the non-owner sees it)
```

---

### Screen 7.22 — Builder timeout

After 15 minutes idle, `RosterBuilderView.on_timeout` fires. Every
child disables, the message is edited (view-only edit), and the
structured session lock releases. No new content line is appended —
just the inert buttons:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: Standard DS — Team A                              │
│ …                                                                    │
└──────────────────────────────────────────────────────────────────────┘
(buttons all disabled — re-open via /desertstorm signups → Set up Team A)
```

---

### Screen 7.23 — Phase-aware variant: builder embed - MIMIC DS FOR ALL BUTTONS AND LAYOUT

Kevin opened the builder against the `CS Standard` 3-phase preset.
The embed gains a phase-nav row at the top and the zone capacity
readouts switch to per-phase format. Phase 1 selected, no auto-fill
yet:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: CS Standard                                       │
│                                                                      │
│ 🗺️ Canyon Storm                                                      │
│ 🔀 Editing **Phase 1** _(use the Phase buttons below to switch)_     │
│                                                                      │
│ **📋 Zones**                                                         │
│ ⬜ **Power Tower** (P1: 0/4, P2: 0/4, P3: 0/4) ←: (empty)            │
│ ⬜ **Nuclear Silo** (P1: 0/4, P2: 0/4, P3: 0/4): (empty)             │
│ ⬜ **Info Center** (P1: 0/4, P2: 0/4, P3: 0/4): (empty)              │
│ ⬜ **Field Hospital** (P1: 0/4, P2: 0/4, P3: 0/4): (empty)           │
│ — **Mercenary** (P1: 0/0, P2: 0/2, P3: 0/4): (empty)                 │
│ — **Arsenal** (P1: 0/0, P2: 0/2, P3: 0/4): (empty)                   │
│                                                                      │
│ 🪑 **Subs**: _(none)_                                                │
│                                                                      │
│ 📊 **Filled:** 0 / 36                                                │
│ 🎯 **Active zone:** **Power Tower** — minimum **300M**               │
└──────────────────────────────────────────────────────────────────────┘
[Phase 1 •]  [Phase 2]  [Phase 3]
[Pick a zone to edit…                                                ▾]
[P1: Pick a member for Power Tower…                                  ▾]
[👁️ Show below-floor]  [↩️ Unassign current zone]  [🪑 Last to subs]
[🎯 Auto-fill]
[✅ Approve & Post]  [📄 Preview mail]  [🖼️ Render image]  [❌ Cancel]
```

Key differences from the flat embed:

- Title omits `— Team X` for CS unless the preset's faction is set
  (CS Standard's `faction="Either"` → no suffix).
- `🔀 Editing **Phase 1**` body line appears (only on phase-aware).
- Per-zone capacity is `(P1: 0/4, P2: 0/4, P3: 0/4)`.
- Zones with zero capacity in the selected phase show `—` (no fill
  status colour) instead of `⬜`. Above, Mercenary / Arsenal in
  Phase 1 are `—` because their `max_phase1=0`.
- Total capacity in `📊 Filled` sums every phase: 4 zones × 4 slots
  × 3 phases + 2 zones × (0 + 2 + 4) = 48 + 12 = **60** (but the
  user spec says the preset caps `max_phase1=4` outer + Phase 2
  opens Mercenary/Arsenal at 2/2 + Phase 3 all 4/4, so the actual
  total varies by spec; the embed reflects whatever `total_capacity()`
  computes).
- Member-picker placeholder gets a `P{n}:` prefix.

Phase nav buttons render with a trailing `•` on the active phase and
`ButtonStyle.primary`; inactive phases use `ButtonStyle.secondary`.

Switching to Phase 2 redraws everything with `Editing **Phase 2**`,
Phase 2 counts as the live readout, and Mercenary/Arsenal now show
`⬜` (they have capacity in Phase 2).

---

### Screen 7.24 — Paired-mode variant: builder embed

Kevin opened a paired-sub preset (sub_mode="paired"). The embed
header includes a paired-mode hint and zones render with inline sub
annotations:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: Standard DS Paired — Team A                       │
│                                                                      │
│ 🗺️ Desert Storm                                                      │
│ ⚖️ Enforcing **Min A** minimum for this team                         │
│                                                                      │
│ **📋 Zones** _(paired mode — each primary has a dedicated sub)_      │
│ 🟡 **Power Tower** (2/4): Alice + sub Bob, Carol ⚠️                  │
│ ⬜ **Nuclear Silo** (0/4): (empty)                                   │
│ …                                                                    │
│                                                                      │
│ ⚠️ **Unpaired primaries (1)**: Carol — click **🔁 Pair subs** to    │
│ attach a sub to any of them. Subs may not cover every primary —     │
│ that's expected.                                                     │
│ 🪑 **Available subs (1)**: Erin — pair via **🔁 Pair subs** or      │
│ leave as bench.                                                      │
│                                                                      │
│ 📊 **Filled:** 2 / 20                                                │
│ 🎯 **Active zone:** **Power Tower** — minimum **300M**               │
└──────────────────────────────────────────────────────────────────────┘
[Pick a zone to edit…                                                ▾]
[Pick a member for Power Tower…                                      ▾]
[👁️ Show members below minimum]  [↩️ Remove current zone assignees]
[🪑 Add all unassigned to Subs]  [🔁 Pair subs]  [🎯 Auto-fill]
[✅ Approve & Post]  [📄 Preview mail]  [🖼️ Render image]  [❌ Cancel]
```

Differences vs pool mode:

- Zone header line ends with `_(paired mode — each primary has a dedicated sub)_`.
- Primary entries render `Alice + sub Bob` when paired, `Carol ⚠️`
  when unpaired (warning glyph signals the missing sub).
- The flat `🪑 **Subs**: …` line is replaced with two new lines:
  - `⚠️ **Unpaired primaries (N)**: …` — only when any primary is
    unpaired. Copy nudges officers toward the new `🔁 Pair subs`
    button and explicitly says "Subs may not cover every primary —
    that's expected" so the 10-subs-vs-20-primaries case doesn't
    look broken. (Pre-#168 there was a separate "complete for every
    primary" success line; that's been dropped — silence means
    everything's paired.)
  - `🪑 **Available subs (N)**: …` — every member in
    `session.subs` who isn't yet paired with a primary.
- The action row includes an extra `🔁 Pair subs` button (pre-#168
  this was `🔁 Re-pair sub` and only handled the swap case).

#168 / Decision #3 retired the auto-fire-after-each-primary sub
picker. A primary assignment no longer immediately opens a sub
prompt — officers pair via `🔁 Pair subs` when they're ready,
which keeps the workflow under their control and matches the
ratio reality (more primaries than subs is normal).

### Screen 7.25 — Pair-subs view (opened by 🔁 Pair subs)

Clicking `🔁 Pair subs` opens an ephemeral with the full pairing
workflow on one surface: running pair list at the top, Primary +
Sub Selects + Assign / Unpair / Done buttons:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔁 **Pair subs** — Phase 1                                           │
│ You have **3 subs** and **20 primaries** — not every primary will   │
│ get a sub.                                                           │
│                                                                      │
│ **Current pairings (2):**                                            │
│ • **Alice** → **Bob**  _(Power Tower)_                              │
│ • **Dan** → **Erin**  _(Nuclear Silo)_                              │
│                                                                      │
│ ⚠️ **Unpaired primaries (18):** Carol, Frank, Greg, …               │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Pick an unpaired primary…                                       ]
[ ▾ Pick a sub…                                                     ]
[✅ Assign pair (disabled)]  [🔄 Unpair…]  [✔ Done]
(ephemeral — only Kevin sees it)
```

- **Primary Select**: currently-assigned primaries in the SELECTED
  phase that aren't yet paired. Hides primaries already paired in
  that phase. Capped at 25 (Discord limit).
- **Sub Select**: members in `session.subs` minus already-paired
  subs. Capped at 25.
- **Assign pair**: writes
  `session.paired_subs_for_phase(phase)[primary] = sub`, captures
  the below-minimum override if the sub falls under the zone's
  threshold, clears both selects, re-renders both the ephemeral
  and the main builder view.
- **🔄 Unpair…**: only enabled when at least one pair exists.
  Switches the ephemeral into unpair mode (Screen 7.26).
- **✔ Done**: closes the ephemeral; main view is already in sync
  because Assign re-renders on each click.

### Screen 7.26 — Unpair mode

Inside the same ephemeral, clicking `🔄 Unpair…` swaps the Primary +
Sub selects for a single pair-picker Select. Picking a pair and
confirming drops the pairing:

```
┌──────────────────────────────────────────────────────────────────────┐
│ (same header + pair list as 7.25)                                   │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Pick a pair to unpair…                                          ]
[🔄 Confirm unpair (disabled)]  [↩️ Back]
```

Each Select option labels as `Primary → Sub`; description shows the
zone. Confirm clears the pairing, re-renders both the ephemeral and
the main view; Back returns to the Pair-subs view (Screen 7.25).

### Screen 7.27 — Closing the pair-subs view

`✔ Done` disables the buttons; the ephemeral becomes a static record
of the pairing state at close time. The main builder embed is
already up to date — every Assign / Unpair edits both surfaces in
lockstep.

---

### Screen 7.28 — Roster-error warnings inline in the embed

When `session.roster_errors` is non-empty (from the open-time roster
read OR from structured-mode signups that didn't match a roster
row), the embed surfaces the FIRST error inline above the auto-fill
summary:

```
…
🎯 **Active zone:** **Power Tower** — minimum **300M**
_Members with no parseable power read as 'power unknown'; toggle the
override to assign them anyway._

⚠️ 3 signed-up member(s) couldn't be matched to a roster row:
Phantom1, Phantom2, Phantom3
```

Common error strings the officer might see in this slot:

- `no power metric column configured — every member will read as
  'power unknown'. Run /setup_desertstorm or /setup_canyonstorm to
  set the Power Metric Column.`
- `member-roster sync isn't enabled — without /sync_members the
  builder can't see your alliance's roster.`
- `roster-sheet open failed: <gspread error>`
- `roster-sheet read failed: <gspread error>`
- `power column 'Squad Power' not found in your roster Sheet header.
  Add it (or change the configured column at setup) so the
  eligibility gate has something to read.`
- `stale Discord IDs on roster (member likely left the server):
  Alice (id 1234) (+3 more)`
- `3 signed-up member(s) couldn't be matched to a roster row:
  Phantom1, Phantom2, …` (structured-mode only)

Decision #7 / #173 retired the per-member-rule subject-not-on-roster
warning. A rule whose subject isn't in tonight's roster is a silent
no-op — nothing to apply, nothing to report. Other rule-application
conflicts (unknown zone, full when pinning, pinned to multiple
zones) still surface in the auto-fill summary (Section 8), not in
this roster_errors block.

Only one error shows in the embed at a time; the full list is
already logged for debugging.

---

### Flow at a glance

```
Entry point A:  /desertstorm signups → 🅰️ Set up Team A
                (structured mode, team pre-picked)
Entry point B:  /desertstorm strategy apply / /canyonstorm strategy apply
                (free-tier, no event_date)
                       │
                       ▼
   Premium / structured / DM gates  → 7.4/7.5
   Preset lookup                    → 7.2/7.3
                       │
                       ▼
   (DS free-tier only) Team picker   → 7.1
                       │
                       ▼
   Defer · pre-warm member cache ·
   read roster powers · join signups ·
   load per_member + power_band rules ·
   (structured) claim session lock   → 7.7 if taken
   (structured) empty pool guard     → 7.6
                       │
                       ▼
   _apply_rules_to_session pins pre-defined assignments
                       │
                       ▼
   ┌──────────────────────────────────────────────────────────┐
   │ Screen 7.8 / 7.23 / 7.24  builder embed lands            │
   │   + phase nav (phase-aware) + paired-mode body lines     │
   └──────────────────────────────────────────────────────────┘
                       │
       ┌───────────────┼──────────────────┬─────────────┬──────────────┐
       ▼               ▼                  ▼             ▼              ▼
   Zone picker     Member picker     ↩️ Remove     🪑 Add all   🎯 Auto-fill
   → re-renders    → 7.11 zone-full   current       unassigned  → Flow 8 +
                   → 7.10 empty pool  zone          to Subs     8.8 confirm
                                      assignees    → 7.14       if dirty
                   (paired mode)
                     ↓ after primary
                     just refreshes — explicit
                     🔁 Pair subs (7.25) handles
                     pairing on demand
                       │
                       ▼
       ┌───────────────┼────────────────────┐
       ▼               ▼                    ▼
   📄 Preview     ✅ Approve & Post    🖼️ Render image
   / Generate    (structured)         → 7.17 png attach
   mail          → 7.19 post + write
   → 7.16        → 7.20 CS faction
                   roles offer
                       │
                       ▼
   💾 Save as preset (free-tier only) → 7.18 modal
                       │
                       ▼
   ❌ Cancel / ✅ Done  → 7.21 close
   or 15-min timeout    → 7.22 silent disable
```

---

## 8. Auto-fill summary

The block appended to the builder embed (Screen 7.8 et al.) after
Kevin clicks `🎯 Auto-fill` in structured mode. `_auto_fill_session`
returns a dict that the embed renderer surfaces at the bottom of the
description. Each line is conditional — empty buckets collapse.

### Screen 8.1 — Clean auto-fill (no gaps, no conflicts)

Apex roster, every member has a parseable power, every rule resolves,
no zones are over-full. Kevin clicked `🎯 Auto-fill`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: Standard DS — Team A                              │
│                                                                      │
│ 🗺️ Desert Storm                                                      │
│ ⚖️ Enforcing **Min A** minimum for this team                         │
│                                                                      │
│ **📋 Zones**                                                         │
│ ✅ **Power Tower** (4/4) ←: Alice, Bob, Carol, Dan                   │
│ ✅ **Nuclear Silo** (4/4): Erin, Frank, Greg, Hank                   │
│ ✅ **Info Center** (4/4): Ivan, Jack, Kim, Liam                      │
│ ✅ **Field Hospital** (4/4): Mona, Nina, Omar, Pete                  │
│ ✅ **Mercenary** (2/2): Quinn, Riley                                 │
│ ✅ **Arsenal** (2/2): Sam, Tia                                       │
│                                                                      │
│ 🪑 **Subs (3)**: Uma, Vic, Wes                                       │
│                                                                      │
│ 📊 **Filled:** 20 / 20                                               │
│ 🎯 **Active zone:** **Power Tower** — minimum **300M**               │
│                                                                      │
│ 🎯 **Auto-fill summary**                                             │
│ • Per-member rules applied: **0**                                    │
│ • Members slotted via a band-relaxed floor: **0**                    │
│ • Auto-filled by power: **20**                                       │
│ • Conflicts: **0**                                                   │
└──────────────────────────────────────────────────────────────────────┘
```

The summary surface lines, in order:

1. `• Per-member rules applied: **N**` — count of per_member rules
   that successfully pinned a member.
2. `• Members slotted via a band-relaxed minimum: **N**` — count of
   greedy-fill assignments where a `power_band` rule lowered the
   minimum enough to admit a member who'd otherwise have been
   excluded by the preset minimum.
3. `• Auto-filled by power: **N**` — count of greedy-fill
   assignments (the count summed across every phase for phase-aware
   presets).
4. `• Auto-paired subs (N): Primary ↔ Sub, Primary ↔ Sub, …` —
   paired-mode only; suppressed when no auto-pairs happened. Per
   Decision #14 (#171), each pair renders explicitly instead of a
   bare count — pairing is the highest-edit candidate, so visibility
   matters.
5. `• Gaps (power unknown, not slotted): **N** — Erin, Frank, …` —
   surfaces every member with `power=None` who couldn't be slotted.
   Per Decision #8 (#171), the full list always renders — no
   `(+M more)` truncation. Suppressed when the list is empty.
6. `• Conflicts: **N** — <full list>` — surfaces every rule-
   application conflict, no truncation. `0` when nothing went wrong.

---

### Screen 8.2 — With gaps (power-unknown members)

Roster has 3 members with no parseable power (Sheet cell was blank or
garbage). After auto-fill:

```
│ 🎯 **Auto-fill summary**                                             │
│ • Per-member rules applied: **0**                                    │
│ • Members slotted via a band-relaxed minimum: **0**                  │
│ • Auto-filled by power: **17**                                       │
│ • Gaps (power unknown, not slotted): **3** — Uma, Vic, Wes           │
│ • Conflicts: **0**                                                   │
```

The 3 power-unknown members aren't auto-added to the sub pool — they
get listed under Gaps so the officer can decide what to do (set their
power in the Sheet and re-run, or toggle the override and slot
manually). Per Decision #8 every gap is listed — even at 12 gaps the
embed renders all twelve names, not a 5-and-`+7-more` truncation.

---

### Screen 8.3 — With conflicts (rule application failures)

A `per_member zone` rule named a zone the preset doesn't have, plus
the rule fired for a member who's already pinned elsewhere:

```
│ 🎯 **Auto-fill summary**                                             │
│ • Per-member rules applied: **1**                                    │
│ • Members slotted via a band-relaxed minimum: **0**                  │
│ • Auto-filled by power: **19**                                       │
│ • Conflicts: **2** — per_member rule names unknown zone: Subway;     │
│ Alice pinned to multiple zones                                       │
```

Conflict-string shapes that still surface (from `_auto_fill_session`):

- `per_member rule names unknown zone: <zone>` — rule points to a
  zone name not in the current preset.
- `<zone> full when pinning <subject>` — zone reached capacity from
  earlier rules before this one could fire.
- `<subject> pinned to multiple zones` — same member named by two
  rules; only the first wins, the rest log as conflicts.

Per Decision #7 (#173), the pre-existing
`per_member subject not on roster: <subject>` shape is **removed**.
A rule whose subject isn't in tonight's roster is a silent no-op —
nothing to apply, nothing to report. The other three shapes still
surface because they're cases where the officer can act on the
information.

Per Decision #8 the conflict preview shows every conflict, no
truncation:

```
│ • Conflicts: **6** — per_member rule names unknown zone: Subway;     │
│ Alice pinned to multiple zones; Power Tower full when pinning Bob;   │
│ Power Tower full when pinning Carol; Mercenary full when pinning     │
│ Dan; Erin pinned to multiple zones                                   │
```

---

### Screen 8.4 — With power-band rule relaxation

Alliance has a `power_band` rule `≥ 180M → Power Tower` that's lower
than Power Tower's preset Min A of 300M. Auto-fill admitted 2
members (190M and 220M) via the relaxed minimum:

```
│ **📋 Zones**                                                         │
│ ✅ **Power Tower** (4/4) ←: Alice, Bob, Carol, Xena                  │
│ …                                                                    │
│                                                                      │
│ 🎯 **Active zone:** **Power Tower** — minimum **180M** _(preset      │
│ minimum 300M relaxed by power_band rule)_                            │
│                                                                      │
│ 🎯 **Auto-fill summary**                                             │
│ • Per-member rules applied: **0**                                    │
│ • Members slotted via a band-relaxed minimum: **2**                  │
│ • Auto-filled by power: **20**                                       │
│ • Conflicts: **0**                                                   │
```

Note the Active zone line ends with `_(preset minimum 300M relaxed by
power_band rule)_` — that's the embed's own indicator that a band is
active for the currently-selected zone, separate from the auto-fill
counter. Both surfaces live independently.

---

### Screen 8.5 — Paired mode with auto-paired subs

Paired-mode preset, auto-fill placed 6 primaries and auto-paired
each with a sub. Per Decision #14 (#171), the summary lists every
pair explicitly:

```
│ 🎯 **Auto-fill summary**                                             │
│ • Per-member rules applied: **0**                                    │
│ • Members slotted via a band-relaxed minimum: **0**                  │
│ • Auto-filled by power: **6**                                        │
│ • Auto-paired subs (6): Alice ↔ Uma, Bob ↔ Vic, Carol ↔ Wes,         │
│ Dan ↔ Xena, Erin ↔ Yale, Frank ↔ Zach                                │
│ • Conflicts: **0**                                                   │
```

The Auto-paired subs line only appears when at least one pair was
made. In pool mode the list is always empty and the line is
suppressed.

---

### Screen 8.6 — Phase-aware preset auto-fill

Phase-aware (`CS Standard`, 3 phases) — auto-fill ran across every
phase. The zone-rendering shape per Rule L (#172) breaks each zone
into a header + one indented row per phase:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: CS Standard                                       │
│                                                                      │
│ 🗺️ Canyon Storm                                                      │
│ 🔀 Editing **Phase 1** _(use the Phase buttons below to switch)_     │
│                                                                      │
│ **📋 Zones**                                                         │
│ ✅ **Power Tower** ←                                                 │
│    └ Phase 1: 4/4 — Alice, Bob, Carol, Dan                           │
│    └ Phase 2: 4/4 — Alice, Bob, Carol, Dan                           │
│    └ Phase 3: 4/4 — Alice, Bob, Carol, Dan                           │
│ ✅ **Nuclear Silo**                                                  │
│    └ Phase 1: 4/4 — Erin, Frank, Greg, Hank                          │
│    └ Phase 2: 4/4 — Erin, Frank, Greg, Hank                          │
│    └ Phase 3: 4/4 — Erin, Frank, Greg, Hank                          │
│ …                                                                    │
│ — **Mercenary**                                                      │
│    └ Phase 1: 0/0 — (empty)                                          │
│    └ Phase 2: 2/2 — Mike, Nick                                       │
│    └ Phase 3: 4/4 — Mike, Nick, Owen, Pete                           │
│                                                                      │
│ 🪑 **Subs**: _(none)_                                                │
│                                                                      │
│ 📊 **Filled:** P1: 16/16, P2: 20/20, P3: 24/24                       │
│ 🎯 **Active zone:** **Power Tower** — minimum **300M**               │
│                                                                      │
│ 🎯 **Auto-fill summary**                                             │
│ • Per-member rules applied: **0**                                    │
│ • Members slotted via a band-relaxed minimum: **0**                  │
│ • Auto-filled by power: **60**                                       │
│ • Conflicts: **0**                                                   │
└──────────────────────────────────────────────────────────────────────┘
```

The zone-header status glyph (`✅` / `🟡` / `⬜` / `—`) reflects the
SELECTED phase's fill state; switching phases via the Phase nav
recolors the headers in place. The per-phase rows always render
every phase's count + member list — officers see all three at a
glance without phase-switching just to check P3.

`📊 **Filled:**` breaks out per-phase counts on phase-aware
sessions; flat presets keep the single `X / Y` total.

---

### Screen 8.7 — Invalidation on manual edit

Auto-fill summary lives until any manual edit invalidates it. The
moment Kevin moves a member to subs / unassigns a zone / picks a new
member, `auto_fill_summary = None` clears, the summary block
disappears from the embed, and the embed reverts to its
pre-auto-fill display state for those rows:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: Standard DS — Team A                              │
│                                                                      │
│ 🗺️ Desert Storm                                                      │
│ ⚖️ Enforcing **Min A** minimum for this team                         │
│                                                                      │
│ **📋 Zones**                                                         │
│ 🟡 **Power Tower** (3/4) ←: Alice, Bob, Carol                        │
│ ✅ **Nuclear Silo** (4/4): Erin, Frank, Greg, Hank                   │
│ …                                                                    │
│                                                                      │
│ 🪑 **Subs (4)**: Uma, Vic, Wes, Dan                                  │
│                                                                      │
│ 📊 **Filled:** 19 / 20                                               │
│ 🎯 **Active zone:** **Power Tower** — minimum **300M**               │
└──────────────────────────────────────────────────────────────────────┘
```

(The summary block is gone — re-click `🎯 Auto-fill` to redo from
scratch. Re-clicking now opens a confirm prompt because the session
holds data; see 8.8.)

---

### Screen 8.8 — Destructive auto-fill re-run confirm

Per Decision #9 (#171), clicking `🎯 Auto-fill` on a session that
already holds data (any zone assignment, any sub, any pairing) opens
a confirm ephemeral first. Fresh sessions (no data) skip the prompt
and run straight away.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ **Re-run auto-fill?** This will reset every assignment, sub      │
│ pairing, and override on this team. Manual edits you've made since  │
│ the last auto-fill will be lost.                                    │
└──────────────────────────────────────────────────────────────────────┘
[🎯 Re-run auto-fill]  [↩️ Cancel]
(ephemeral — only Kevin sees it)
```

Re-run runs `_auto_fill_session` on the parent session and refreshes
both the ephemeral (which becomes "🎯 Auto-fill re-run complete —
main view refreshed.") and the main builder embed in place. Cancel
leaves everything untouched and acks "↩️ Auto-fill cancelled — your
edits are intact."

---

### Screen 8.9 — Auto-fill with conflicts AND gaps AND band relaxations

The all-of-the-above case. Realistic when an alliance has a thorough
rule set and a sloppy roster Sheet:

```
│ 🎯 **Auto-fill summary**                                             │
│ • Per-member rules applied: **3**                                    │
│ • Members slotted via a band-relaxed minimum: **2**                  │
│ • Auto-filled by power: **15**                                       │
│ • Auto-paired subs (15): Alice ↔ Uma, Bob ↔ Vic, Carol ↔ Wes,        │
│ Dan ↔ Xena, Erin ↔ Yale, Frank ↔ Zach, Greg ↔ Adam, Hank ↔ Ben,      │
│ Ivan ↔ Cory, Jack ↔ Dale, Kim ↔ Earl, Liam ↔ Finn, Mona ↔ Gus,       │
│ Nina ↔ Herb, Omar ↔ Ira                                              │
│ • Gaps (power unknown, not slotted): **2** — Uma, Vic                │
│ • Conflicts: **2** — per_member rule names unknown zone: Subway;     │
│ Power Tower full when pinning Bob                                    │
```

Every count is independent — a `band-relaxed` member also counts
under `auto-filled by power` (band-relaxed is the subset that
benefited from a band rule).

---

### Flow at a glance

```
Kevin clicks  🎯 Auto-fill  on a structured-mode builder
                       │
                       ▼
         _auto_fill_session resets every phase's
         assignments + subs + paired-subs + override flags
                       │
                       ▼
      ┌────────────────┴────────────────┐
      │  1. per_member zone rules pin   │  → Per-member rules applied: N
      │     (Phase 1 only; cross-phase  │  → Conflicts: unknown zone /
      │     duplicate check)            │    full when pinning /
      │                                 │    dup-pinned. Subject-not-on
      │                                 │    -roster silently no-ops
      │                                 │    (Decision #7).
      └────────────────┬────────────────┘
                       │
                       ▼
      ┌────────────────┴────────────────┐
      │  2. Greedy fill per phase,      │  → Auto-filled by power: N
      │     zones sorted by priority,   │  → Band-relaxed: members
      │     eligibility-gated           │    admitted via power_band
      └────────────────┬────────────────┘
                       │
                       ▼
      ┌────────────────┴────────────────┐
      │  3. Paired-mode: pair each      │  → Auto-paired subs: N
      │     primary with a remaining    │    (paired mode only)
      │     eligible sub per phase      │
      └────────────────┬────────────────┘
                       │
                       ▼
      ┌────────────────┴────────────────┐
      │  4. Spillover: unassigned       │  → Gaps: power-unknown
      │     members with known power    │    members surface here
      │     → session.subs              │
      │     Unknown power → gaps list   │
      └────────────────┬────────────────┘
                       │
                       ▼
         session.auto_fill_summary populated
         embed re-renders with summary block (Screen 8.1)
                       │
       ┌───────────────┼────────────────┐
       ▼               ▼                ▼
   No conflicts,   Conflicts /     Manual edit
   no gaps         gaps reported   invalidates summary
   8.1 happy       8.2 / 8.3 / 8.9 8.7 block disappears
   path            warning shape
       │               │                │
       └───────────────┴────────────────┘
                       ▼
       Re-clicking 🎯 Auto-fill with existing data → 8.8 confirm
       prompt first; confirm to nuke + refill, cancel to keep tweaks
```

---

Source files referenced (current, post-#168/#169/#170/#171/#172/#173/#174/#158):

- `storm_officer_view.py` — `/desertstorm signups` cog, `OfficerView`, `_OnBehalfVoteView` (replaces the pre-#168 `_OnBehalfModal`), `_PresetPickerView`, `_open_team_setup`, all bucket-rendering helpers
- `storm_permissions.py` — premium gate + leadership gate copy
- `storm_roster_builder.py` — `RosterBuilderView`, `_SaveAsPresetModal`, `_PairSubsView` (replaces the pre-#168 `_PairedSubPickerView` + `_RepairPrimaryPickerView`), `_AutoFillConfirmView` (#171), `open_roster_builder`, `_auto_fill_session`, `_finalize_structured_roster`, `_render_builder_embed`
- `storm_walkthrough.py` — first-run tour offer copy, now with event-type + teams branching (#170)
- `storm_date_helpers.py` — `format_event_date` "Sunday, May 18, 2026" shape used in titles
- `storm.py` — `build_ds_mail` / `build_cs_mail` for the mail-preview body shape (zone-emoji-prefixed per #158)
- `storm_strategy.py` — `format_power` (e.g. `300M`), `total_capacity`, phase semantics, `_StrategyListView` (#169), `_PresetPickerView` (#169)
- `storm_attendance.py` — `_AttendanceView` (member select + ✅/❌ in-place per #171), no longer ships `_StatusPickerView`
- `storm_history.py` — phase-aware variant per Rule L (#172), drops override marker + 🔄 footer per #171
- `storm_icons.py` — `zone_emoji_prefix` helper (#158), no-op until the upload script runs
- `storm_member_rules.py` — `InlinePowerBandView` (replaces `InlinePowerBandModal` per #168), `_AddRuleTypePickerView` (#169)
- `scripts/upload_storm_emojis.py` — one-shot Discord Application Emojis uploader for #158

---

## 9. Approve & Post + faction roles

The Approve & Post button lives in the roster-builder view (Flow 7).
When the officer clicks it, `_finalize_structured_roster` runs: it
re-reads powers from the Sheet (so `power_at_assignment` snapshots
the moment-of-approval value), builds the mail body via the
alliance's saved template, posts it to the configured channel,
writes one row per slot to the `rosters_tab` Sheet, and — on a
successful CS post — offers to apply faction roles.

The behaviour branches across five post-channel outcomes
(`posted_ok` / `no_channel` / `channel_gone` / `send_failed`) and
the faction-roles offer fires only when **all three** of:

- event_type is CS
- post succeeded
- alliance has a Judicator role configured AND at least one
  Judicator-flagged roster member is present

are true. Below are screens for each branch.

---

### Screen 9.1 — Public builder ack (replaces the builder embed)

After Approve & Post succeeds, the original roster-builder embed
the officer was looking at gets edited in place: the embed body
stays (so the room can see the final roster), the content line
above it flips to a success line, and every interactive button on
the view is disabled. The officer's view (Kevin's) and any other
leadership who had the message open both see this:

```
✅ Structured roster approved and posted.
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: Standard DS — Team A                              │
│                                                                      │
│ 🗺️ Desert Storm                                                      │
│ ⚖️ Enforcing **Min A** floors for this team                           │
│                                                                      │
│ **📋 Zones**                                                          │
│ • Nuclear Silo (Max: 4) — Alice, Bob, Carol, Dan                     │
│ • Oil Refinery I (Max: 4) — Erin, Frank, …                           │
│ • Field Hospital I (Max: 4) — …                                      │
│ • Field Hospital II (Max: 4) — …                                     │
│ • Info Center (Max: 4) — …                                           │
│ • Arsenal (Max: 4) — …                                               │
│ • Mercenary Factory (Max: 4) — …                                     │
│                                                                      │
│ 🪑 **Subs (1)**: Ghost                                                │
│                                                                      │
│ 📊 **Filled:** 30 / 30                                                │
└──────────────────────────────────────────────────────────────────────┘
[Approve & Post (disabled)]  [Save as preset (disabled)]
[📄 Mail preview (disabled)]  [🖼️ Render PNG (disabled)]
[Cancel (disabled)]
```

(All buttons greyed out. The view is `stop()`'d and the session
lock released.)

---

### Screen 9.2 — Approve & Post officer summary (5 outcome variants)

Right after the public ack, the officer gets an ephemeral followup
that varies by post outcome.

**9.2a — Happy path (`posted_ok`):**

The mail was sent to the configured post channel; Sheet write
succeeded. Officer sees a slim 2-line confirmation; no mail
preview attached (it's already in `#storm-rosters`).

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster posted.                                                    │
│ 📬 Mail sent to #storm-rosters.                                       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

**9.2b — No post channel configured (`no_channel`):**

`post_channel_id` is 0 / NULL on the structured config. Sheet rows
were still written; the mail was built but never sent. Officer
gets the mail preview inline so they can copy it manually.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster recorded.                                                  │
│ ⚠️ No post channel is configured — mail was built but not sent.      │
│ Run `/setup_desertstorm` (or `/setup_canyonstorm`) to pick one, or   │
│ copy the mail manually below.                                        │
│                                                                      │
│ ```                                                                  │
│ **Alliance — Desert Storm**                                          │
│                                                                      │
│ **Zone Assignments**                                                 │
│ **Nuclear Silo**                                                     │
│ Alice                                                                │
│ Bob                                                                  │
│ Carol                                                                │
│ Dan                                                                  │
│                                                                      │
│ **Oil Refinery I**                                                   │
│ Erin                                                                 │
│ Frank                                                                │
│ …                                                                    │
│                                                                      │
│ **Subs**                                                             │
│ Ghost                                                                │
│                                                                      │
│ **Time:** 4pm EDT (18:00 server time)                                │
│ ```                                                                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

**9.2c — Channel deleted or invisible (`channel_gone`):**

`post_channel_id` is set on config, but `guild.get_channel(id)`
returned None — channel was deleted, or the bot lost View Channel
since setup. Sheet rows still written; mail preview attached.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster recorded.                                                  │
│ ⚠️ The configured post channel (<#1234567890>) is deleted or the     │
│ bot can't see it. Re-run setup to pick a new channel — mail preview  │
│ below.                                                               │
│                                                                      │
│ ```                                                                  │
│ **Alliance — Desert Storm**                                          │
│ … (truncated to 1800 chars max — appends `…(truncated)` if longer) │
│ ```                                                                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

The `<#…>` mention silently degrades to the channel ID number in
clients that can't resolve it. Officer goes to setup, picks a new
channel, and the next Approve & Post will hit the happy path.

**9.2d — Send rejected by Discord (`send_failed`):**

Channel resolves but the `channel.send(mail)` call raised — most
commonly `Forbidden` (bot missing Send Messages in that channel)
or `HTTPException` (rate limit, 5xx, length cap). The exception
string is truncated to 120 chars and surfaced inline.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster recorded.                                                  │
│ ⚠️ The configured post channel <#1234567890> rejected the send:      │
│ `403 Forbidden (error code: 50013): Missing Permissions`. Check the  │
│ bot's permissions in that channel — mail preview below.              │
│                                                                      │
│ ```                                                                  │
│ **Alliance — Desert Storm**                                          │
│ …                                                                    │
│ ```                                                                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**9.2e — Sheet write soft-error (any post outcome above):**

When `_write_rosters_tab` returns one or more error strings (e.g.
spreadsheet not configured, sheet open failed, header migration
failed), the FIRST error appears as an extra line right after the
post-status block. The Discord post is not rolled back; the
errors are advisory.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster posted.                                                    │
│ 📬 Mail sent to #storm-rosters.                                       │
│ ⚠️ rosters tab header migration failed (data still appended, but    │
│ readers may not see new columns): APIError [400]: invalid_argument   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

(One line per missing config / Sheet issue. `_write_rosters_tab`
only surfaces the first error; subsequent ones are logged but not
shown.)

---

### Screen 9.3 — The mail body posted to `#storm-rosters` (DS shape)

When `post_status == "posted_ok"` the bot posts the mail to
`#storm-rosters` as a regular channel message (no embed wrapper,
no buttons — leadership copies the text into the in-game mail
system).

The mail uses the alliance's configured template under the
`Default` template name. The default DS template lives in
`defaults.py` and is:

```
**{alliance_name} — Desert Storm**

**Zone Assignments**
{zones}

**Subs**
{subs}

**Time:** {time}
```

with `{alliance_name}` rendered as the literal string `Alliance`
(not the Discord guild name — `build_ds_mail` hardcodes
`alliance_name="Alliance"`; alliances who want their actual name
customise the template). `{zones}` renders in canonical
`DS_ZONE_STRUCTURE` order with each zone as a bold header
followed by one member per line. Empty zones are skipped.

```
┌──────────────────────────────────────────────────────────────────────┐
│ **Alliance — Desert Storm**                                          │
│                                                                      │
│ **Zone Assignments**                                                 │
│ **Nuclear Silo**                                                     │
│ Alice                                                                │
│ Bob                                                                  │
│ Carol                                                                │
│ Dan                                                                  │
│                                                                      │
│ **Oil Refinery I**                                                   │
│ Erin                                                                 │
│ Frank                                                                │
│                                                                      │
│ **Field Hospital I**                                                 │
│ …                                                                    │
│                                                                      │
│ **Info Center**                                                      │
│ …                                                                    │
│                                                                      │
│ **Arsenal**                                                          │
│ …                                                                    │
│                                                                      │
│ **Mercenary Factory**                                                │
│ …                                                                    │
│                                                                      │
│ **Subs**                                                             │
│ Ghost                                                                │
│                                                                      │
│ **Time:** 4pm EDT (18:00 server time)                                │
└──────────────────────────────────────────────────────────────────────┘
(posted as a regular channel message in #storm-rosters by the bot)
```

Variants:

- **Paired sub mode**: a primary with a paired sub renders as
  `Alice + sub Bob` on the same line under the primary's zone.
  The global Subs block then only contains overflow / unpaired
  subs.
- **Phase-aware preset (2-phase or 3-phase)**: each phase renders
  as its own block separated by a `**Phase N**` header.

  ```
  **Phase 1**

  **Alliance — Desert Storm**
  …(zones+subs as above)…

  **Phase 2**

  **Alliance — Desert Storm**
  …(zones+subs for phase 2; subs block is empty for phase 2 since
  the subs pool is event-level and only attached to phase 1)…
  ```

---

### Screen 9.4 — The mail body posted to `#storm-rosters` (CS shape)

Same shape, different default template + zone layout. The default
CS template (also in `defaults.py`):

```
**{alliance_name} — Canyon Storm**

**Zone Assignments**
{zones}

**Subs**
{subs}

**Time:** {time}
```

`{zones}` walks `CS_ZONE_STRUCTURE` and groups zones under
`**Stage 1**` / `**Stage 2**` / `**Stage 3**` headers. Stage
headers only render when at least one zone in that stage has
members.

```
┌──────────────────────────────────────────────────────────────────────┐
│ **Alliance — Canyon Storm**                                          │
│                                                                      │
│ **Zone Assignments**                                                 │
│ **Stage 1**                                                          │
│ **Power Tower**                                                      │
│ Alice                                                                │
│ Bob                                                                  │
│ Carol                                                                │
│ Dan                                                                  │
│                                                                      │
│ **Data Center 1**                                                    │
│ Erin                                                                 │
│ Frank                                                                │
│                                                                      │
│ **Sample Warehouse 1**                                               │
│ …                                                                    │
│                                                                      │
│ **Stage 2**                                                          │
│ **Defense System 1**                                                 │
│ …                                                                    │
│                                                                      │
│ **Serum Factory 1**                                                  │
│ …                                                                    │
│                                                                      │
│ **Stage 3**                                                          │
│ **Virus Lab**                                                        │
│ …                                                                    │
│                                                                      │
│ **Subs**                                                             │
│ Ghost                                                                │
│                                                                      │
│ **Time:** 4pm EDT (12:00 server time)                                │
└──────────────────────────────────────────────────────────────────────┘
(posted as a regular channel message in #storm-rosters by the bot)
```

When the alliance has saved a **custom** template (via the legacy
`/desertstorm_draft` / `/canyonstorm_draft` editor), the
`{zones}` and `{subs}` placeholders are filled with the same
text rendered above but the surrounding wrapper text is whatever
the alliance wrote. The default rendering shown is the
out-of-the-box baseline.

---

### Screen 9.5 — Faction roles offer (CS-only, post_status == posted_ok)

Only fires when:

1. `event_type == "CS"`
2. The post succeeded (`posted_ok`)
3. The structured config has a `judicator_role_id` set (an
   officer ran `/setup_canyonstorm` and picked a role)
4. At least one member on the just-approved roster matches a
   `per_member.special_role=judicator` Member Rule

The offer is sent as an ephemeral followup right after Screen 9.2
— so only Kevin sees it.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚔️ **Apply Faction Roles?**                                          │
│ Matchmaking will reveal your faction post-roster. When you know     │
│ it's **Rulebringers**, click below to apply the configured          │
│ Judicator role to your candidates: Alice, Carol, Erin.              │
└──────────────────────────────────────────────────────────────────────┘
[⚔️ Rulebringers — apply Judicator]  [🛡️ Dawnbreakers — no role to apply]
(ephemeral — only Kevin sees it; 30-min interaction window)
```

Candidate names come from `_find_judicator_candidates(session)` —
it walks every phase's `assignments` AND every phase's
`paired_subs`, then matches against any `per_member` rule whose
`sub_type == "special_role"` and `value == "judicator"`. Subjects
resolve three ways: display-name, internal member key, or Discord
ID — same as auto-fill.

**Silent skip cases (no offer posted):**

- `event_type == "DS"`
- Post outcome was anything other than `posted_ok` (Approve &
  Post hit `no_channel` / `channel_gone` / `send_failed`)
- `judicator_role_id == 0` — the alliance never configured a role
- No roster members match a Judicator rule
- `_maybe_offer_faction_roles` raised an exception (logged
  warning; user sees nothing)

---

### Screen 9.6 — Permission preflight failures (Rulebringers branch)

Kevin clicks `⚔️ Rulebringers — apply Judicator`. The view defers
ephemerally, then runs a preflight: bot must have Manage Roles
AND its top role must sit above the Judicator role. Each failure
mode has its own ephemeral.

**9.6a — Role no longer exists / bot can't see it:**

The view's stored `judicator_role_id` resolves to None via
`guild.get_role`. Either the alliance deleted the role or the
bot lost View Roles.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The configured Judicator role (<@&987654321>) no longer exists   │
│ or the bot can't see it. Re-run `/setup_canyonstorm` to pick a new  │
│ one.                                                                 │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral; allowed_mentions.none() — role mention renders as a styled
chip but doesn't ping the role)
```

**9.6b — Bot's own member is unresolvable (defensive):**

`guild.me` returned None. Should never happen in practice but
guarded against.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't resolve the bot's own member in this guild.             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**9.6c — Bot is missing Manage Roles:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ I don't have **Manage Roles** in this server, so I can't apply   │
│ the Judicator role. Grant the permission to my role and try again.  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**9.6d — Role hierarchy blocks assignment:**

Bot has Manage Roles but the Judicator role sits at or above the
bot's top role. Discord refuses to let the bot manage a role
above its own. Most common when an alliance creates a high-tier
Judicator role and forgets to bump the bot above it.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ The Judicator role (<@&987654321>) sits at or above my own role  │
│ in the hierarchy, so Discord won't let me assign it. In **Server    │
│ Settings → Roles**, move my role above the Judicator role and try   │
│ again.                                                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral; allowed_mentions.none())
```

After each of these failures the view is left as-is — Kevin can
click again after fixing the issue, and the preflight re-runs.

---

### Screen 9.7 — Apply summary (Rulebringers happy path)

Preflight passed. The view loops over every candidate key, looks
up their Discord ID (either from the roster `m["discord_id"]`
field or from the member key if it's a numeric Discord ID),
checks they're still in the guild, checks they don't already
have the role, then calls `member.add_roles(role,
reason="Storm faction roles: Judicator (Rulebringers)")`.

Buckets the result into four lists. Header line varies by
whether **any** application succeeded.

**9.7a — At least one role applied (the common case):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Judicator role applied:                                           │
│   • Applied to: Alice, Erin                                          │
│   • Already had the role: Carol                                      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Sections beyond `Applied to:` only render if non-empty.

**9.7b — Nothing applied (every candidate was already-had or off-Discord):**

Header flips to a neutral `ℹ️` line so the message doesn't lie.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ℹ️ Judicator role apply — nothing to apply:                          │
│   • Already had the role: Alice, Carol, Erin                         │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**9.7c — Mix with off-Discord candidates:**

A roster member resolved via a Member Rule subject string ("Frank
the Tank") that doesn't carry a Discord ID. Bot can't apply the
role; surfaces as `Not on Discord / not in server`.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Judicator role applied:                                           │
│   • Applied to: Alice                                                │
│   • Not on Discord / not in server: Frank the Tank                   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**9.7d — Per-member API failures during the loop:**

Even after preflight, individual `add_roles` calls can still
raise (mid-loop rate limit, member just left, role just got
re-positioned). Failures bucket into a `Failed:` line with the
reason truncated to 80 chars.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Judicator role applied:                                           │
│   • Applied to: Alice, Carol                                         │
│   • Failed: Erin (missing permission); Frank (HTTPException: rate limited) │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**9.7e — Degenerate empty case (no buckets at all):**

If the loop runs but every candidate falls through without
landing in any of the four buckets (shouldn't happen — guard
against bad state).

```
┌──────────────────────────────────────────────────────────────────────┐
│ ℹ️ No role applications needed — all candidates either already had  │
│ the role or weren't on Discord.                                      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

After the summary post, both buttons on the offer view (Screen
9.5) are disabled so Kevin can't double-apply.

---

### Screen 9.8 — Dawnbreakers acknowledgement

Kevin clicks `🛡️ Dawnbreakers — no role to apply` instead. The
view's noop callback edits the offer message in place — content
flips and both buttons disable.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Dawnbreakers acknowledged — no role to apply.                    │
└──────────────────────────────────────────────────────────────────────┘
[⚔️ Rulebringers — apply Judicator (disabled)]
[🛡️ Dawnbreakers — no role to apply (disabled)]
(ephemeral — only Kevin sees it)
```

No Sheet writes, no role assignments; matchmaking revealed
Dawnbreakers so the Judicator role is irrelevant this storm.

---

### Screen 9.9 — Officer-only guard on the offer view

If someone OTHER than Kevin clicks either offer button (e.g.
another leadership member who happens to share the ephemeral via
screen-share — rare but possible during planning):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who approved the roster can apply faction roles.│
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only the wrong-clicker sees it)
```

Mirrored for the Dawnbreakers button:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who approved the roster can resolve faction     │
│ roles.                                                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 9.10 — Faction-roles offer timeout

After 30 minutes (1800-second timeout, set so the alliance has
time to actually run matchmaking and see the faction reveal),
both buttons strip themselves silently. The offer text remains
visible.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚔️ **Apply Faction Roles?**                                          │
│ Matchmaking will reveal your faction post-roster. When you know     │
│ it's **Rulebringers**, click below to apply the configured          │
│ Judicator role to your candidates: Alice, Carol, Erin.              │
└──────────────────────────────────────────────────────────────────────┘
[⚔️ Rulebringers — apply Judicator (disabled)]
[🛡️ Dawnbreakers — no role to apply (disabled)]
(ephemeral)
```

If the officer needs the apply path after timeout, they re-run
Approve & Post — the offer re-fires when the roster gets posted
again (Sheet writes append, they don't overwrite, so this is
safe but does duplicate the rosters_tab rows).

---

### Flow at a glance

```
Officer clicks Approve & Post in roster builder
                │
                ▼
   defer(ephemeral=True, thinking=True)
                │
                ▼
   Re-read powers from roster Sheet (power_at_assignment snapshot)
                │
                ▼
   Build mail body via alliance's saved template
                │
                ▼
   Resolve post_channel_id → 4-way branch
                │
   ┌─────────────┼───────────────┬──────────────┐
   ▼             ▼               ▼              ▼
posted_ok    no_channel    channel_gone    send_failed
   │             │               │              │
   ▼             ▼               ▼              ▼
9.3 / 9.4    (none — mail   (none — mail   (none — mail
mail posted  preview in     preview in     preview in
to channel   9.2b)          9.2c)          9.2d)
   │
   ▼
Write rosters_tab rows (best-effort; soft errors → 9.2e)
   │
   ▼
9.1 public ack edits builder embed in place
   │
   ▼
9.2 ephemeral summary (one of 4 variants + optional Sheet error)
   │
   ▼
   Is event_type CS AND post_status == posted_ok?
   │
   ├── no → done
   │
   ▼
   _maybe_offer_faction_roles
   │
   ├── No judicator_role_id configured → silent skip
   ├── No candidates on this roster → silent skip
   │
   ▼
9.5 ephemeral offer view (Rulebringers / Dawnbreakers buttons)
   │
   ├── Officer clicks ⚔️ Rulebringers
   │   │
   │   ▼
   │   Preflight: bot exists? has Manage Roles? hierarchy OK?
   │   │
   │   ├── fail → 9.6a/b/c/d (view stays clickable)
   │   │
   │   ▼
   │   Loop candidates → bucket into applied / already / off-Discord / failed
   │   │
   │   ▼
   │   9.7 summary (variant by buckets)
   │
   ├── Officer clicks 🛡️ Dawnbreakers → 9.8
   │
   ├── Wrong user clicks either → 9.9
   │
   └── 30 min elapses → 9.10 (buttons strip silently)
```

---

## 10. `/desertstorm attendance` + `/canyonstorm attendance` — post-event attendance

`/desertstorm attendance [event_date:<optional>]` and its CS twin
`/canyonstorm attendance` open a Premium-gated officer view for
marking who actually showed at each assigned roster slot. Writes
one row per slot to the alliance's configured `attendance_tab`
Sheet. Closes the structured-flow loop: the bot already knew who
was *assigned*; this is how it learns who *showed*.

Status codes the view writes: `attended`, `no_show`,
`sub_activated`, plus the empty string for unrecorded. Status
labels in the UI: ✅ Attended / ❌ No-show / 🔄 Sub activated / —
(dash for unrecorded).

---

### Screen 10.1 — Slash command invocation

Kevin types `/desertstorm attendance` in any channel. Discord's
autocomplete shows the single optional parameter:

```
/desertstorm attendance ▾
  event_date     Optional — defaults to the most recent posted event.
                 Accepts e.g. May 18, 5/18, yesterday.
```

`event_date` is a free-text optional string parsed by `parse_event_date`
(same helper used elsewhere — accepts ISO `2026-05-18`, `5/18`, `May 18`,
`yesterday`, `today`). The CS form is identical under
`/canyonstorm attendance` — event type is encoded in the parent group,
not in an `event_type` argument.

`guild_only=True` — DMs surface Discord's generic "This command
only works in servers" client-side error before the bot sees it.

---

### Screen 10.2 — Permission denial (non-leader / non-admin)

Caller doesn't pass `is_leader_or_admin`. Fires `deny_non_leader`
before any other state changes.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to run this command.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only the caller sees it)
```

---

### Screen 10.3 — Premium gate (non-premium guild)

Caller is leadership but the guild isn't Premium. The
`ensure_premium_structured` helper does both checks (Premium
license + structured-flow opt-in); Premium-failure ephemeral
fires first.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔒 `/desertstorm attendance` is a 💎 Premium feature. Run `/upgrade` to    │
│ unlock it.                                                           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 10.4 — Structured-flow disabled

Guild IS Premium but the alliance never turned on the structured
roster flow for this event type via `/setup_desertstorm` or
`/setup_canyonstorm`.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The structured roster flow isn't enabled for Canyon Storm. Run   │
│ `/setup_canyonstorm` and turn on **Structured Roster Flow** first.  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Variant for DS: identical with `Desert Storm` / `/setup_desertstorm`.

---

### Screen 10.5 — Date-parse failure

Kevin passed `event_date:gibberish`. `parse_event_date` returned
None; bot bails before Premium check.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ `gibberish` isn't a date I can parse. Try `May 18`, `5/18`,      │
│ `2026-05-18`, `yesterday`, or `today`.                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 10.6 — No recent events on record (event_date omitted)

Kevin ran `/desertstorm attendance Storm` without a
date. `most_recent_event_date` returned None — the rosters_tab is
empty for DS, or the tab doesn't exist yet.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No posted Desert Storm events on record. Run                     │
│ `/desertstorm post_signup` and build a roster before recording            │
│ attendance, or pass `event_date` explicitly.                        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Variant for CS: identical with `Canyon Storm`.

---

### Screen 10.7 — No structured roster found for the resolved date

Premium check passed, structured flow enabled, date parsed and
clean — but no rows in rosters_tab match that date. Common when
the officer fat-fingers a date that isn't a real storm, or when
the alliance ran a legacy /draft flow that didn't write to
rosters_tab.

The followup is gated on whether `slot_errors` came back from
the Sheet read.

**10.7a — Clean miss (no Sheet errors; rosters_tab just has no rows for that date):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No structured roster found for **Saturday, May 18, 2026**        │
│ (Desert Storm).                                                      │
│ Attendance is only recordable for events with a structured roster   │
│ posted via `/desertstorm signups`.                                         │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**10.7b — Sheet I/O error (rosters tab doesn't exist, perms, etc):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No structured roster found for **Saturday, May 18, 2026**        │
│ (Desert Storm).                                                      │
│ Details: rosters tab 'Rosters' doesn't exist yet — post a           │
│ structured roster first via /desertstorm signups                          │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 10.8 — The attendance view (DS shape, both teams)

Happy path. Sheet I/O succeeded, slots loaded. Posted as a
public (non-ephemeral) message via `interaction.followup.send`
because the response was deferred without `ephemeral=True`. Other
leadership in the channel can see the embed; only Kevin can
interact with the controls.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm Attendance — Saturday, May 18, 2026                 │
│                                                                      │
│ **Team A**                                                           │
│ — Alice (Nuclear Silo)                                               │
│ — Bob (Nuclear Silo)                                                 │
│ — Carol (Nuclear Silo)                                               │
│ — Dan (Nuclear Silo)                                                 │
│ — Erin (Oil Refinery I)                                              │
│ — Frank (Oil Refinery I)                                             │
│ — Ghost (sub) 🪑                                                     │
│                                                                      │
│ **Team B**                                                           │
│ — Helena (Info Center)                                               │
│ — Ivan (Info Center)                                                 │
│ — Jana (Field Hospital I)                                            │
│ — Karl (Field Hospital I)                                            │
│ …                                                                    │
│                                                                      │
│ ─────────────────────────────────────────                            │
│ Footer: ✅ 0  ·  ❌ 0  ·  — 14                                       │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Pick a slot to record attendance (or use the bulk-mark buttons   ]
[ below)…                                                            ]
[✅ Mark all unrecorded as attended]
[❌ Mark all unrecorded as did not attend]
[↩️ Clear selection (disabled)]
[💾 Save attendance]
```

Per-slot line shape (post-#171):
`<status icon> <member name> (<zone or "sub">) <role marker>`.

- Status icon: ✅/❌/— from `_STATUS_LABELS[status]`. New session
  starts with every slot at `—` unless the alliance has prior
  recorded attendance for this event (carried forward by
  `load_attendance`).
- Zone part: `(<zone>)` for primaries; `(sub)` for sub-pool
  members whose `zone` cell is blank.
- Role marker: ` 🪑` appended when `role == "sub"`.
- **Decision #6 (#171)**: the Override Below Floor ⚠️ glyph and
  the trailing `_⚠️ Assigned below the zone minimum at build time._`
  footnote are both gone. The rosters_tab Sheet column survives
  for post-event audit, but officers recording attendance don't
  need it surfaced.

Footer counts (Rule K): `✅ <attended>  ·  ❌ <no-show>  ·  — <unrecorded>`.
🔄 Sub activated is dropped from the UI entirely; any legacy
`sub_activated` rows still on the Sheet roll into the `—` bucket
so the math stays correct.

Color: `discord.Color.gold()` for DS, `discord.Color.orange()`
for CS.

---

### Screen 10.9 — Attendance view variant: CS shape

CS now respects `cfg.teams` (Rule A / #166). When CS runs both
teams (`teams=both`) the team grouping renders `**Team A**` /
`**Team B**` headers just like DS. Single-team CS (`teams=A` or
`teams=B`) still uses the `**Roster**` header. Color flips to
orange.

**CS, `teams=both`:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Canyon Storm Attendance — Saturday, May 18, 2026                 │
│                                                                      │
│ **Team A**                                                           │
│ — Alice (Power Tower)                                                │
│ — Bob (Power Tower)                                                  │
│ — Carol (Data Center 1)                                              │
│                                                                      │
│ **Team B**                                                           │
│ — Dan (Sample Warehouse 1)                                           │
│ — Erin (Defense System 1)                                            │
│ — Frank (Virus Lab)                                                  │
│ — Ghost (sub) 🪑                                                     │
│                                                                      │
│ Footer: ✅ 0  ·  ❌ 0  ·  — 7                                        │
└──────────────────────────────────────────────────────────────────────┘
(controls identical to 10.8)
```

**CS, single team:**

```
│ **Roster**                                                           │
│ — Alice (Power Tower)                                                │
│ — Bob (Power Tower)                                                  │
│ …                                                                    │
```

---

### Screen 10.10 — Attendance view variant: pagination (>25 slots)

Discord's Select component caps at 25 options. When the roster
has more than 25 slots, `_AttendanceView._build()` slices the
slot list by `session.page * session.per_page` and emits a
`◀ Prev` / `Next ▶` button pair. Pagination paginates the
dropdown only — the embed always shows every slot.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm Attendance — Saturday, May 18, 2026                 │
│ …(all 30 slots in the embed)…                                       │
│ Footer: ✅ 0  ·  ❌ 0  ·  — 30                                       │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Pick a slot to record attendance… (slots 1-25)                 ]
[✅ Mark all unrecorded as attended]
[❌ Mark all unrecorded as did not attend]
[↩️ Clear selection (disabled)]
[◀ Prev (disabled)]  [Next ▶]
[💾 Save attendance]
```

Switching pages clears `selected_key` so a stale selection
doesn't follow Kevin across pages.

---

### Screen 10.11 — Empty slots (edge case)

`load_rostered_slots` came back with `slots=[]` AND no errors —
e.g. a Sheet header migration scenario where the date column was
renamed and no rows match. The view still posts but the embed
description is the empty-state copy and **the action buttons are
hidden entirely** (per #171 — there's nothing to mark, so
surfacing dead buttons would be misleading).

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm Attendance — Saturday, May 18, 2026                 │
│                                                                      │
│ _No roster slots found for this event. Run `/desertstorm signups`   │
│ and build a structured roster first; attendance only applies to     │
│ structured-flow rosters._                                            │
│                                                                      │
│ Footer: ✅ 0  ·  ❌ 0  ·  — 0                                        │
└──────────────────────────────────────────────────────────────────────┘
(no buttons rendered)
```

---

### Screen 10.12 — Existing-attendance read warning

The view loaded fine but `load_attendance` reported errors when
fetching the prior-recorded rows for this event. Officer can
still record fresh entries; the warning sits as the message
`content` above the embed.

```
⚠️ Read existing attendance had issues — see bot logs. You can
still record fresh entries below.
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm Attendance — Saturday, May 18, 2026                 │
│ …(slots embed)…                                                     │
└──────────────────────────────────────────────────────────────────────┘
(controls identical to 10.8)
```

(Public message; not ephemeral. Other leadership can see the
warning too.)

---

### Screen 10.13 — Picker selection (no ephemeral, just label swap)

#171 / Decision #5 retired the pre-existing
`_StatusPickerView` ephemeral. Kevin picks Alice from the
dropdown. The view rebuilds in place with the selection captured:

- The dropdown placeholder updates to `Picked: Alice`.
- The two action buttons swap labels:
  - `✅ Mark all unrecorded as attended` → `✅ Mark as attended`
  - `❌ Mark all unrecorded as did not attend` → `❌ Mark as did not attend`
- The `↩️ Clear selection` button enables (it was disabled with
  no selection).

No new ephemeral fires — the buttons act on the selected slot
directly when clicked.

```
[ ▾ Picked: Alice                                                  ]
[✅ Mark as attended]
[❌ Mark as did not attend]
[↩️ Clear selection]
[💾 Save attendance]
```

### Screen 10.14 — Single-slot mark ack

Kevin clicks `✅ Mark as attended`. The callback writes
`session.statuses[selected_key] = STATUS_ATTENDED`, clears
`selected_key` (so the next click defaults back to bulk-mark
mode), and re-renders. No ephemeral — the embed update *is* the
ack.

Embed change: Alice's row flips from `— Alice (Nuclear Silo)` to
`✅ Attended Alice (Nuclear Silo)`. Footer ticks from
`✅ 0 · ❌ 0 · — 14` to `✅ 1 · ❌ 0 · — 13`. The dropdown's
per-option `current:` description updates too.

### Screen 10.15 — Clear selection

`↩️ Clear selection` resets the picked slot's status to `—` AND
drops the selection back to no-selection mode (action buttons
swap back to bulk-mark labels).

### Screen 10.16 — Bulk-mark unrecorded → Attended (no selection mode)

With no slot selected, the action buttons are the bulk variants.
`✅ Mark all unrecorded as attended` walks every slot whose status
is `STATUS_UNRECORDED` and flips it to `STATUS_ATTENDED`; already-
recorded slots (✅/❌) are not touched. `❌ Mark all unrecorded as
did not attend` mirrors that with `STATUS_NO_SHOW`.

Before:

```
Footer: ✅ 2  ·  ❌ 1  ·  — 27
```

After clicking `✅ Mark all unrecorded as attended`:

```
Footer: ✅ 29  ·  ❌ 1  ·  — 0
```

### Screen 10.17 — Permission denial

The view guards every callback on `session.user_id`. Someone
other than Kevin clicks any button or the dropdown:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who opened this view can record attendance.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 10.18 — Internal-error guard (defensive)

The Select's value is encoded as `team|zone|member` (max 100
chars). If Discord somehow returns a malformed value with the
wrong number of pipe-separated parts:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Internal error: couldn't parse slot key.                         │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

(Defensive — shouldn't happen in practice.)

---

### Screen 10.19 — Save attendance (happy path)

Kevin hits `💾 Save attendance`. The button's callback defers
ephemerally with `thinking=True`, then calls `save_attendance`
off the event loop (Sheet I/O). On success, ephemeral summary +
disable every button on the view + stop the view.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved attendance for **Saturday, May 18, 2026** — 29 slot(s)     │
│ recorded (✅ 28, ❌ 1).                                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

`recorded` is the sum of attended + no_show (🔄 sub_activated is
no longer pickable per Rule K). Unrecorded slots aren't counted
toward `recorded` and aren't written to the Sheet (the save path
filters them out).

The public attendance view's buttons all disable; the embed body
stays visible so anyone in the channel can see the final
counts.

---

### Screen 10.20 — Save attendance (Sheet write soft error)

`save_attendance` returned a non-empty error list — most
commonly the trailing-blank step failed after the main write
succeeded.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Attendance partially saved — attendance trailing-blank failed   │
│ (new data written, stale rows 31..47 may remain): APIError [429]:   │
│ rate limit                                                           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Or, if the initial write itself failed (prior history intact):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Attendance partially saved — attendance write failed (prior data│
│ intact): APIError [503]: backend error                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Or the most degenerate (no Sheet configured):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Attendance partially saved — spreadsheet not configured          │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

After a soft error, the view is NOT stopped — Kevin can edit and
try Save again. Note the message says "partially saved" even
when nothing was written; the prior-data-intact wording above
clarifies which case fired.

---

### Screen 10.21 — Attendance view timeout

The parent view has a 900-second (15-minute) timeout. After
expiry, every button strips and the view is left visible.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm Attendance — Saturday, May 18, 2026                 │
│ …(roster embed body unchanged)…                                     │
│ Footer: ✅ 2  ·  ❌ 1  ·  🔄 0  ·  — 27                              │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Pick a slot to record attendance… (disabled) ]
[✅ Mark unrecorded → Attended (disabled)]
[💾 Save attendance (disabled)]
```

In-progress un-saved changes are lost. Kevin re-runs the slash
command to pick up where he left off — `load_attendance`
re-loads any prior-saved state so it's not a full reset, just a
reset of the in-memory session.

---

### Flow at a glance

```
Kevin types /desertstorm attendance Storm
                │
                ▼
   Leader check?           ── no ─→ 10.2 deny ephemeral
                │
                ▼
   event_date parsed?      ── no ─→ 10.5 parse error
                │
                ▼
   Date resolved? (omit → most_recent_event_date)
                │
                ├── no events ─→ 10.6 no-events ephemeral
                │
                ▼
   Premium?                ── no ─→ 10.3 premium gate
                │
                ▼
   Structured flow on?     ── no ─→ 10.4 structured-flow gate
                │
                ▼
   defer(thinking=True) + parallel Sheet reads
                │
                ├── slots empty + sheet error → 10.7b
                ├── slots empty + clean      → 10.7a
                │
                ▼
   _AttendanceView posted publicly
                │
                ├── attendance read had errors → 10.12 with warning content
                ├── slots == 0 (edge)         → 10.11 empty body
                ├── slots > 25                → 10.10 pagination buttons appear
                ├── DS                        → 10.8 (Team A / B groups)
                └── CS                        → 10.9 (Roster group)
                │
                ▼
   Officer picks a slot from the dropdown
                │
                ▼
   10.13 status picker (ephemeral, 2-min)
                │
                ├── ✅/❌/🔄/↩️ → 10.14 status applied; parent embed re-renders
                ├── wrong user      → 10.15 deny
                ├── malformed value → 10.16 internal-error guard
                └── 2-min timeout   → 10.17 buttons strip
                │
                ▼
   Officer optionally clicks Bulk-mark → 10.18
                │
                ▼
   Officer clicks 💾 Save attendance
                │
                ├── soft error → 10.20 partial-save warning (view stays)
                └── ok        → 10.19 success summary + view disables
                │
                ▼
   Or 15-min view timeout → 10.21 (buttons strip; in-memory state lost)
```

---

## 11. `/desertstorm strategy` + `/canyonstorm strategy` commands

Two parallel command groups (`/desertstorm strategy` for Desert Storm,
`/canyonstorm strategy` for Canyon Storm) wrap CRUD operations over an
alliance's saved strategy presets. Both groups expose six
subcommands: `create`, `edit`, `list`, `delete`, `apply`,
`roster_history`.

The groups inherit `app_commands.Group` (per the
feedback_app_commands_groups memory rule — new feature surfaces
adopt the Group shape). Permissions: every subcommand runs
`_deny_if_not_leader` up front.

The actual editor (Flow 12) is what `create` and `edit` open;
this flow stops at the slash-command entry points and the
auxiliary commands.

---

### Screen 11.1 — `/desertstorm strategy create` slash command

```
/desertstorm strategy create ▾
  name *   A short name for the preset (e.g. 'Standard Desert')
```

Kevin runs `/desertstorm strategy create name:Standard DS`. The command
calls `_StrategyGroup._create(interaction, "Standard DS")`.

**11.1a — Permission denial:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to run this command.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**11.1b — Empty name (after `.strip()`):**

Kevin ran `/desertstorm strategy create name:` with only whitespace. Bot
refuses before any Sheet I/O.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Pick a preset name (e.g. `Standard Desert`).                      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**11.1c — Duplicate name (case-insensitive):**

The alliance already has a `standard ds` preset (case folded).

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ A preset named **Standard DS** already exists. Use               │
│ `/ds strategy edit name:"Standard DS"` to modify it.                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

(Note the rewritten command hint replaces `_` with space — the
embed names the group as `ds_strategy` but the hint reads as
`/ds strategy edit name:"…"` for the user.)

**11.1d — Happy path:**

Name is unique. `seed_default_preset("Standard DS", "DS")`
builds a buffer pre-populated with `DS_ZONE_STRUCTURE` zones at
capacity 0. The buffer is marked dirty (so the Save button
enables immediately even before any zone edits). Editor opens
publicly — see Flow 12.

---

### Screen 11.2 — `/canyonstorm strategy create` slash command

Same shape as 11.1 but with CS-flavoured hints.

```
/canyonstorm strategy create ▾
  name *   A short name for the preset (e.g. 'Rulebringers Plan')
```

The duplicate-name hint adjusts:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ A preset named **CS Standard** already exists. Use               │
│ `/cs strategy edit name:"CS Standard"` to modify it.                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Happy path: `seed_default_preset("CS Standard", "CS")` builds a
buffer pre-populated with the 22 zones in `CS_ZONE_STRUCTURE`
(Power Tower, Data Center 1/2, Sample Warehouse 1–4, Floaters,
Defense System 1/2, Serum Factory 1/2, plus the stage-3 set
including Virus Lab) at capacity 0. Editor opens.

---

### Screen 11.3 — `/desertstorm strategy edit` slash command

```
/desertstorm strategy edit ▾
  name *   The saved preset to open
```

**11.3a — Preset not found:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No preset named **Standard DS**. Use the list command to see     │
│ saved presets.                                                       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**11.3b — Happy path:**

`load_preset` returns a populated buffer with `dirty=False`.
Editor opens publicly — Save button starts disabled (no unsaved
changes yet). See Flow 12.

---

### Screen 11.4 — `/canyonstorm strategy edit` slash command

Identical to 11.3 with `/cs strategy edit`. The loaded buffer
includes the `faction` field (defaults to `Either`) which only
CS uses.

---

### Screen 11.5 — `/desertstorm strategy list` slash command (no args)

```
/desertstorm strategy list ▾
  (no parameters)
```

Per #169 / Rule M, the list view now ships with inline action
buttons so officers never hit a dead-end summary. Empty + populated
states share the same view — Edit and Delete are just disabled when
no presets exist.

**11.5a — No presets saved (empty state):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Strategy Presets                                  │
│                                                                      │
│ *No Desert Storm strategy presets saved yet.* Click **➕ Create**   │
│ below to make one.                                                   │
└──────────────────────────────────────────────────────────────────────┘
[➕ Create]  [✏️ Edit (disabled)]  [🗑️ Delete (disabled)]
(public — no ephemeral; leadership can see the embed)
```

**11.5b — Presets exist (populated state):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Strategy Presets                                  │
│                                                                      │
│ • **Standard DS**                                                    │
│ • **High Power A**                                                   │
│ • **Both Teams Balanced**                                            │
└──────────────────────────────────────────────────────────────────────┘
[➕ Create]  [✏️ Edit]  [🗑️ Delete]
```

Color: `discord.Color.blurple()`. Action buttons:

- `➕ Create` opens `_CreatePresetNameModal` — a single-field modal
  for the new preset's name. On submit, validates uniqueness
  against `list_presets` then opens the editor (Section 12).
- `✏️ Edit` opens an ephemeral `_PresetPickerView` with a Select
  listing every preset (sorted case-insensitively, capped at 25
  for the Discord cap). On pick, loads the preset and opens the
  editor as a followup.
- `🗑️ Delete` opens the same picker shape, then routes through the
  `_ConfirmDeleteView` confirm + delete flow.

---

### Screen 11.6 — `/canyonstorm strategy list` slash command

Identical shape to 11.5; title renames to `Canyon Storm — Strategy
Presets` and `➕ Create` opens the CS-flavored preset editor on
submit. Same inline action row.

---

### Screen 11.7 — `/desertstorm strategy delete` slash command

```
/desertstorm strategy delete ▾
  name *   The saved preset to delete
```

Kevin runs `/desertstorm strategy delete name:Standard DS`.

**11.7a — Confirmation prompt (ephemeral):**

The handler posts an ephemeral `_ConfirmDelete` view and awaits
the officer's click (60-second timeout).

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Delete preset **Standard DS**? This removes all rows for this    │
│ preset from your Sheet. Can't be undone.                            │
└──────────────────────────────────────────────────────────────────────┘
[🗑️ Delete preset]  [Cancel]
(ephemeral; 60-second window)
```

Button styles: 🗑️ red / Cancel grey.

**11.7b — Confirm clicked, delete succeeded (PUBLIC followup):**

The `🗑️ Delete preset` callback disables both buttons, edits
the confirm message in place (keeps body, removes interactivity),
runs `delete_preset` off the event loop, then posts a
non-ephemeral followup. The PUBLIC followup means leadership in
the channel sees the deletion.

Confirm message edits in place:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Delete preset **Standard DS**? This removes all rows for this    │
│ preset from your Sheet. Can't be undone.                            │
└──────────────────────────────────────────────────────────────────────┘
[🗑️ Delete preset (disabled)]  [Cancel (disabled)]
(ephemeral)
```

Followup:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🗑️ Deleted preset **Standard DS**.                                  │
└──────────────────────────────────────────────────────────────────────┘
(public — no buttons; channel sees it)
```

**11.7c — Confirm clicked, but preset not found / Sheet write failed:**

`delete_preset` returned False (the preset wasn't in the Sheet,
or the write failed). Followup is ephemeral.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't find preset **Standard DS** to delete (or Sheet write   │
│ failed).                                                             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**11.7d — Cancel clicked:**

The `Cancel` button callback disables both buttons, edits the
confirm message, sends an ephemeral confirmation.

Confirm message edits in place to:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Delete preset **Standard DS**? This removes all rows for this    │
│ preset from your Sheet. Can't be undone.                            │
└──────────────────────────────────────────────────────────────────────┘
[🗑️ Delete preset (disabled)]  [Cancel (disabled)]
(ephemeral)
```

Followup:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Delete cancelled.                                                 │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**11.7e — Confirm timeout (60s elapses with no click):**

Both buttons disable in place; `view.confirmed` remains None; the
post-await branch treats not-True as cancelled and fires the
same followup as 11.7d.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Delete preset **Standard DS**? This removes all rows for this    │
│ preset from your Sheet. Can't be undone.                            │
└──────────────────────────────────────────────────────────────────────┘
[🗑️ Delete preset (disabled)]  [Cancel (disabled)]
(ephemeral)
```

Followup:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Delete cancelled.                                                 │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**11.7f — Wrong-user clicks confirm or cancel:**

Confirm:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the user who ran the command can confirm.                    │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Cancel:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the user who ran the command can cancel.                     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 11.8 — `/canyonstorm strategy delete` slash command

Identical to 11.7 with the CS preset name (e.g. `CS Standard`)
swapped in. Both commands share the inner `_ConfirmDelete`
class.

---

### Screen 11.9 — `/desertstorm strategy apply` slash command

```
/desertstorm strategy apply ▾
  name *   The preset to apply (use the list command to see saved presets)
```

This is a thin shim that imports `storm_roster_builder` at call
time (to break load-order coupling) and delegates to
`open_roster_builder(interaction, "DS", name)`. The roster
builder flow is Flow 7; this command is just the entry point.

The denial / preset-not-found ephemerals are owned by the roster
builder, not this group — `/desertstorm strategy apply` itself has no
direct user-facing ephemerals beyond what the builder posts.

---

### Screen 11.10 — `/canyonstorm strategy apply` slash command

Identical to 11.9 with `event_type="CS"`. Hands off to the same
`open_roster_builder` entry point.

---

### Screen 11.11 — `/desertstorm strategy roster_history` slash command

```
/desertstorm strategy roster_history ▾
  date     Optional — show a specific date (May 18, 5/18, 2026-05-18,
           yesterday). Omit to list recent events.
```

Thin shim delegating to `storm_history.open_history(interaction,
"DS", date)`. The actual history browser UX is Flow 15 (out of
scope for this walkthrough section). The command itself surfaces
no in-group ephemerals.

---

### Screen 11.12 — `/canyonstorm strategy roster_history` slash command

Identical to 11.11 with `event_type="CS"`.

---

### Flow at a glance

```
DS slash command tree:
/desertstorm strategy
  ├── create name:<X>
  │     ├── non-leader      → 11.1a deny
  │     ├── empty name      → 11.1b "Pick a preset name"
  │     ├── duplicate name  → 11.1c "already exists; use edit"
  │     └── ok              → seed_default_preset → Flow 12 editor opens
  │
  ├── edit name:<X>
  │     ├── non-leader      → deny (same shape)
  │     ├── not found       → 11.3a "No preset named X"
  │     └── ok              → load_preset → Flow 12 editor opens
  │
  ├── list
  │     ├── non-leader      → deny
  │     ├── no presets      → 11.5a "No DS strategy presets saved yet"
  │     └── ok              → 11.5b public embed (• **name** list)
  │
  ├── delete name:<X>
  │     ├── non-leader      → deny
  │     └── ok              → 11.7a confirmation prompt
  │           ├── 🗑️ confirm
  │           │   ├── delete ok   → 11.7b public "Deleted preset"
  │           │   └── delete fail → 11.7c ephemeral "Couldn't find"
  │           ├── Cancel          → 11.7d "Delete cancelled"
  │           ├── 60s timeout     → 11.7e "Delete cancelled"
  │           └── wrong user      → 11.7f deny on either button
  │
  ├── apply name:<X>
  │     └── delegates to storm_roster_builder.open_roster_builder
  │
  └── roster_history date:<X?>
        └── delegates to storm_history.open_history

CS slash command tree: identical six subcommands; create's
"already exists" hint reads /cs strategy edit; apply + history
delegate with event_type="CS".
```

---

## 12. Strategy preset editor

Opens from `/desertstorm strategy create` / `/desertstorm strategy edit` /
`/canyonstorm strategy create` / `/canyonstorm strategy edit`. A public Discord
embed + view with: a zone dropdown (one option per zone), a
phase-mode dropdown (Flat / 2 phases / 3 phases), and action
buttons (Add zone / Rename / Save / Abandon).

In-memory state lives on `_PresetEditorView.buf` — a
`PresetBuffer` with `name`, `event_type`, `zones[]`, `faction`
(CS only), `phase_count` (0/2/3), and `dirty` flag. Discord's
interaction token expires after 15 minutes, which is the
editor's natural session bound — no SQLite session table needed.

The editor branches **heavily** on `buf.phase_count`:

- `phase_count == 0` → **flat** preset. Picking a zone opens the
  single-page `_ZoneEditModal`. Zone lines render with `(Max:
  N)`.
- `phase_count == 2` or `phase_count == 3` → **phase-aware**.
  Picking a zone routes through a 2-page wizard
  (`_ZonePhaseCapacityAndFloorsModal` →
  `_ZonePhasePriorityModal`). Page 1 packs phase capacities and
  power minimums together at the 5-field Discord cap (3 caps + 2
  DS-both mins); page 2 holds per-phase priority. Zone lines
  render with `(P1: N, P2: M[, P3: K])` plus optional per-phase
  priority brackets.

---

### Screen 12.1 — Editor embed (flat DS, both teams, just-created)

`/desertstorm strategy create name:Standard DS` lands. `seed_default_preset`
builds a buffer with the 11 canonical DS zones, every zone at
`max_players=0`, `dirty=True`. Editor posts publicly via
`interaction.response.send_message(embed=..., view=...)`.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Editing Preset: Standard DS                                       │
│                                                                      │
│ 🗺️ Event: Desert Storm                                               │
│ 🔀 Mode: **Flat**                                                    │
│                                                                      │
│ 📋 **Zones:**                                                        │
│ • Nuclear Silo        (Max: 0)  Min A: 0 · Min B: 0                  │
│ • Oil Refinery I      (Max: 0)  Min A: 0 · Min B: 0                  │
│ • Oil Refinery II     (Max: 0)  Min A: 0 · Min B: 0                  │
│ • Science Hub         (Max: 0)  Min A: 0 · Min B: 0                  │
│ • Info Center         (Max: 0)  Min A: 0 · Min B: 0                  │
│ • Field Hospital I    (Max: 0)  Min A: 0 · Min B: 0                  │
│ • Field Hospital II   (Max: 0)  Min A: 0 · Min B: 0                  │
│ • Field Hospital III  (Max: 0)  Min A: 0 · Min B: 0                  │
│ • Field Hospital IV   (Max: 0)  Min A: 0 · Min B: 0                  │
│ • Arsenal             (Max: 0)  Min A: 0 · Min B: 0                  │
│ • Mercenary Factory   (Max: 0)  Min A: 0 · Min B: 0                  │
│                                                                      │
│ 📊 Capacity: **0** (team size 30; flex room is fine) ⚠️              │
│ ⚠️ *Unsaved changes — Save preset to save your changes.*                    │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Edit a zone…                                                  ]
[ ▾ 🔀 Phase mode: Flat (no phases) (default-selected)             ]
[✏️ Rename preset]  [💾 Save preset]  [🔙 Abandon this preset]
```

Capacity gauge glyph rules:

- `< 30` → ⚠️ (under-staffed)
- `== 30` → ✅ (exact)
- `> 30` → ℹ️ (flex room; not an error)

Save button is enabled because `dirty=True` (new preset). On a
freshly loaded edit (no changes yet) the Save button would start
disabled.

Phase mode select default-tick is on `Flat (no phases)` (the
option whose `value == "0"` matches `buf.phase_count == 0`).

---

### Screen 12.2 — Editor embed variant: flat DS, Team A only

When `/setup_desertstorm` was run with `teams=A`, the
`_resolve_ds_teams` helper returns `"A"`. The embed surfaces it
and the per-zone lines only show `Min A`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Editing Preset: Standard DS                                       │
│                                                                      │
│ 🗺️ Event: Desert Storm                                               │
│ 👥 Teams: **Team A only** (floors shown match)                      │
│ 🔀 Mode: **Flat**                                                    │
│                                                                      │
│ 📋 **Zones:**                                                        │
│ • Nuclear Silo        (Max: 4)  Min: 300M [P1]                       │
│ • Oil Refinery I      (Max: 4)  Min: 250M [P1]                       │
│ …                                                                    │
│                                                                      │
│ 📊 Capacity: **30** (team size 30; flex room is fine) ✅            │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Edit a zone…                                                  ]
[ ▾ 🔀 Phase mode: Flat (no phases)                                ]
[✏️ Rename preset]  [💾 Save preset (disabled)]  [🔙 Abandon this preset]
```

Mirror for `teams=B`: `**Team B only**` and `Min: <Min B value>`.

---

### Screen 12.3 — Editor embed variant: flat CS

CS adds a `⚙️ Faction:` line (default `Either`) and skips the
Team A/B distinction.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Editing Preset: CS Standard                                       │
│                                                                      │
│ 🗺️ Event: Canyon Storm                                               │
│ ⚙️ Faction: Either                                                   │
│ 🔀 Mode: **Flat**                                                    │
│                                                                      │
│ 📋 **Zones:**                                                        │
│ • Power Tower         (Max: 0)  Min: 0                               │
│ • Data Center 1       (Max: 0)  Min: 0                               │
│ • Data Center 2       (Max: 0)  Min: 0                               │
│ • Sample Warehouse 1  (Max: 0)  Min: 0                               │
│ • Sample Warehouse 2  (Max: 0)  Min: 0                               │
│ • Sample Warehouse 3  (Max: 0)  Min: 0                               │
│ • Sample Warehouse 4  (Max: 0)  Min: 0                               │
│ • Floaters            (Max: 0)  Min: 0                               │
│ • Defense System 1    (Max: 0)  Min: 0                               │
│ • Defense System 2    (Max: 0)  Min: 0                               │
│ • Serum Factory 1     (Max: 0)  Min: 0                               │
│ • Serum Factory 2     (Max: 0)  Min: 0                               │
│ • Floaters            (Max: 0)  Min: 0                               │
│ • Virus Lab           (Max: 0)  Min: 0                               │
│ • Power Tower         (Max: 0)  Min: 0                               │
│ • Data Center 1       (Max: 0)  Min: 0                               │
│ • Data Center 2       (Max: 0)  Min: 0                               │
│ • Defense System 1    (Max: 0)  Min: 0                               │
│ • Defense System 2    (Max: 0)  Min: 0                               │
│ • Serum Factory 1     (Max: 0)  Min: 0                               │
│ • Serum Factory 2     (Max: 0)  Min: 0                               │
│                                                                      │
│ 📊 Capacity: **0** (team size 30; flex room is fine) ⚠️              │
│ ⚠️ *Unsaved changes — Save preset to save your changes.*                    │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Edit a zone… (truncated to first 25 — Discord cap)            ]
[ ▾ 🔀 Phase mode: Flat (no phases)                                ]
[✏️ Rename preset]  [💾 Save preset]  [🔙 Abandon this preset]
```

Note: CS has 21 canonical zones — under the 25-option Discord
cap, so all zones are pickable. (A custom-zone alliance with 26+
zones would see truncation; the editor's zone select hard-caps
at 25.)

---

### Screen 12.4 — Editor embed variant: 3-phase CS

`buf.phase_count = 3`. Mode line flips and each zone breaks into a
header row plus one indented row per phase (per Rule L / #172).

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Editing Preset: CS Standard                                       │
│                                                                      │
│ 🗺️ Event: Canyon Storm                                               │
│ ⚙️ Faction: Either                                                   │
│ 🔀 Mode: **3 Phases (P1 + P2 + P3)**                                │
│                                                                      │
│ 📋 **Zones:**                                                        │
│ • **Power Tower** — Min: 300M                                        │
│    └ Phase 1: cap 4                                                  │
│    └ Phase 2: cap 4                                                  │
│    └ Phase 3: cap 4                                                  │
│ • **Data Center 1** — Min: 250M                                      │
│    └ Phase 1: cap 4                                                  │
│    └ Phase 2: cap 0                                                  │
│    └ Phase 3: cap 4                                                  │
│ • **Sample Warehouse 1** — Min: 200M                                 │
│    └ Phase 1: cap 4 (priority 1)                                     │
│    └ Phase 2: cap 0                                                  │
│    └ Phase 3: cap 0                                                  │
│ …                                                                    │
│ • **Virus Lab** — Min: 350M                                          │
│    └ Phase 1: cap 0                                                  │
│    └ Phase 2: cap 0                                                  │
│    └ Phase 3: cap 4 (priority 1)                                     │
│                                                                      │
│ 📊 Capacity: **86** (team size 30; flex room is fine) ℹ️             │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Edit a zone…                                                  ]
[ ▾ 🔀 Phase mode: 3 Phases (default-selected)                     ]
[✏️ Rename preset]  [💾 Save preset (disabled)]  [🔙 Abandon this preset]
```

Capacity sums every phase's max for every zone, so a preset that
re-uses Power Tower across all three phases counts the 4 slots
three times (12 total just for Power Tower). The `ℹ️` glyph
signals "over team-size; this is fine if you build in flex
room."

---

### Screen 12.5 — Phase-mode toggle: switching from Flat → 2 Phases

Kevin opens the Phase mode dropdown:

```
[ ▾ 🔀 Phase mode                                                  ]
  ○ Flat (no phases)               Single per-zone slot — Max Players only.
  ○ 2 Phases                       DS-style migration: Phase 1 → Phase 2.
  ○ 3 Phases                       CS-style stages: Phase 1 → 2 → 3.
```

Default-selected reflects the current `buf.phase_count`. Kevin
clicks `2 Phases`. The callback seeds capacities/priorities
(every zone's `max_phase1 ← max_players`, `max_phase2 ←
max_phase1`, priorities follow similarly), flips
`buf.phase_count = 2`, marks dirty, and re-renders the embed
with a content line.

```
🔀 Switched to **2-phase** mode. Capacities + assignments are
kept. Re-select **Flat** mode to restore without data loss. Seeded
22 per-zone capacity/priority value(s) from prior values; edit any
zone to override.
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Editing Preset: CS Standard                                       │
│                                                                      │
│ 🗺️ Event: Canyon Storm                                               │
│ ⚙️ Faction: Either                                                   │
│ 🔀 Mode: **2 Phases (P1 + P2)**                                     │
│                                                                      │
│ 📋 **Zones:**                                                        │
│ • **Power Tower** — Min: 300M                                        │
│    └ Phase 1: cap 4                                                  │
│    └ Phase 2: cap 4                                                  │
│ • **Data Center 1** — Min: 250M                                      │
│    └ Phase 1: cap 4                                                  │
│    └ Phase 2: cap 4                                                  │
│ …                                                                    │
│                                                                      │
│ 📊 Capacity: **56** (team size 30; flex room is fine) ℹ️             │
│ ⚠️ *Unsaved changes — Save preset to save your changes.*                    │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Edit a zone…                                                  ]
[ ▾ 🔀 Phase mode: 2 Phases                                        ]
[✏️ Rename preset]  [💾 Save preset]  [🔙 Abandon this preset]
```

If no values were auto-seeded (every zone already had non-zero
phase fields), the seeded note is omitted:

```
🔀 Switched to **2-phase** mode. Capacities + assignments are
kept. Re-select **Flat** mode to restore without data loss.
```

When re-picking the same mode (no-op), the dropdown silently
defers — no embed re-render, no ephemeral, no content line.

---

### Screen 12.6 — Phase-mode toggle: switching to Flat

Kevin clicks `Flat (no phases)`. `phase_count` flips back to 0;
no auto-clear of phase data (re-toggling restores it).

```
🔀 Switched to **Flat** mode. Capacities + assignments are
kept. Re-select **2-phase** mode to restore without data loss.
```

The mode-line now reads `🔀 Mode: **Flat**` and zone lines
collapse to `(Max: N)` shape again.

---

### Screen 12.7 — Flat zone edit modal (DS, both teams)

Kevin picks `Nuclear Silo` from the zone dropdown.
`buf.phase_count == 0`, so `_ZoneEditModal` opens.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Edit Zone: Nuclear Silo                                              │
│ ──────────────────────────────────────────                           │
│ Max Players                                                          │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ 4                                                              │   │
│ └────────────────────────────────────────────────────────────────┘   │
│                                                                      │
│ Min Power Team A                                                     │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ 300M                                                           │   │
│ └────────────────────────────────────────────────────────────────┘   │
│                                                                      │
│ Min Power Team B                                                     │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ 180M                                                           │   │
│ └────────────────────────────────────────────────────────────────┘   │
│                                                                      │
│ Priority (1 = highest; ties OK)                                      │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ 1                                                              │   │
│ └────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
[Submit]  [Cancel]
(Discord modal — opens overlay)
```

Placeholders for empty fields:

- Max Players: `e.g. 4`
- Min Power Team A: `e.g. 300M`
- Min Power Team B: `e.g. 180M`
- Priority: `e.g. 1 — same number across zones is fine`

For `teams=A` only: the Min Power Team B field is omitted (4
fields shown).  For `teams=B` only: the Min Power Team A field
is omitted.

For CS: the two team-power fields collapse into a single `Min
Power` field with placeholder `e.g. 250M`.

---

### Screen 12.8 — Flat zone edit modal: submit validation

**12.8a — Max Players not numeric:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Max Players must be a number — got `four`. Try again.            │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it; modal closes; zone unchanged)
```

**12.8b — Power value didn't parse (DS):**

Triggered when either `Min Power Team A` or `Min Power Team B`
fails `_parse_power_cell`. The message doesn't say which field —
both `bad_a` and `bad_b` route through the same response.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ One of the power values didn't parse. Use formats like `300M`,   │
│ `1.2B`, or `300000000`. Leave blank for no floor.                   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**12.8c — Power value didn't parse (CS, single Min field):**

CS does name the field in the error since there's only one:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't parse `tbd` as a power value. Try `250M`, `1.2B`, or    │
│ `300000000`. Leave blank for no floor.                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**12.8d — Priority not numeric:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Priority must be a number — got `top`. Try again.                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**12.8e — Submit success:**

`upsert_zone` lands; embed re-renders via `view.refresh` with
content line:

```
✏️ Updated **Nuclear Silo**.
```

(Then the editor embed below it shows the updated zone line.)

---

### Screen 12.9 — Phase-aware zone edit: page 1 of 2 (capacity + minimums)

`buf.phase_count == 3`. Kevin picks `Power Tower` from the zone
dropdown. `_ZonePhaseCapacityAndFloorsModal` opens. Title is
truncated to 45 chars: `Power Tower — Caps + Min (3P)`. Worst
case is DS-both + 3 phases — 3 caps + 2 min fields = exactly 5
Discord-modal components.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Power Tower — Caps + Min (3P)                                        │
│ ──────────────────────────────────────────                           │
│ Max Phase 1                                                          │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ 4                                                              │   │
│ └────────────────────────────────────────────────────────────────┘   │
│   placeholder: e.g. 4 (leave 0 to skip Phase 1 at this zone)         │
│                                                                      │
│ Max Phase 2                                                          │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ 4                                                              │   │
│ └────────────────────────────────────────────────────────────────┘   │
│   placeholder: e.g. 2 (leave 0 to skip Phase 2 at this zone)         │
│                                                                      │
│ Max Phase 3                                                          │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ 4                                                              │   │
│ └────────────────────────────────────────────────────────────────┘   │
│   placeholder: e.g. 3 (leave 0 to skip Phase 3 at this zone)         │
│                                                                      │
│ Min Power Team A                                                     │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ 300M                                                           │   │
│ └────────────────────────────────────────────────────────────────┘   │
│   placeholder: e.g. 300M                                             │
│                                                                      │
│ Min Power Team B                                                     │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ 180M                                                           │   │
│ └────────────────────────────────────────────────────────────────┘   │
│   placeholder: e.g. 180M                                             │
└──────────────────────────────────────────────────────────────────────┘
[Submit]  [Cancel]
(Discord modal — opens overlay)
```

For `phase_count == 2`: the Max Phase 3 field is omitted (4
fields shown). CS variant: single `Min Power` field with
placeholder `e.g. 250M` instead of the two Team A / Team B
fields. DS-A-only / DS-B-only: only the relevant Min field.

---

### Screen 12.10 — Page 1 submit: validation + bridge

Submit validates each capacity field, then each minimum-power
field. Capacity parse error (any field non-int):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Max Phase 1 must be a number — got `four`. Reopen the zone to   │
│ retry.                                                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Wording surfaces the specific field name — `Max Phase 1` /
`Max Phase 2` / `Max Phase 3` — based on which field failed.
Power-field parse errors surface field-by-field:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Min Power Team A didn't parse — got `tbd`. Use `300M`, `1.2B`,   │
│ or `300000000`.                                                      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Min Power Team B didn't parse — got `tbd`. Use `300M`, `1.2B`,   │
│ or `300000000`.                                                      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

CS variant:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Min Power didn't parse — got `tbd`. Use `250M`, `1.2B`, or       │
│ `250000000`.                                                         │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

The wizard does NOT auto-reopen on parse failure; officer goes
back to the editor embed and picks the zone again. Already-
validated fields in the pending stash persist.

Submit success → bridge ephemeral with a one-button Next view:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Capacities + minimums recorded for **Power Tower**. Click        │
│ **Next** to set the per-phase auto-fill priorities.                  │
└──────────────────────────────────────────────────────────────────────┘
[Next → Priority Per Phase]
(ephemeral — only Kevin sees it; 5-min timeout)
```

Kevin clicks Next. The button disables in place (so a double-
click doesn't open two modals), then `_ZonePhasePriorityModal`
opens.

---

### Screen 12.11 — (removed)

Page 1's capacity + minimums folded into Screen 12.9 to drop a
"Next" hop, per UX plan §12.9. Numbering stays at 12.x to avoid
churning every cross-reference; 12.11 is intentionally empty.

---

### Screen 12.12 — Phase-aware zone edit: page 2 of 2 (priority)

`_ZonePhasePriorityModal`. Final page; submission finalises the
edit and refreshes the editor embed.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Power Tower — Priority (3P)                                          │
│ ──────────────────────────────────────────                           │
│ Priority Phase 1 (1 = highest)                                       │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ 1                                                              │   │
│ └────────────────────────────────────────────────────────────────┘   │
│   placeholder: leave blank for no priority                           │
│                                                                      │
│ Priority Phase 2                                                     │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ 1                                                              │   │
│ └────────────────────────────────────────────────────────────────┘   │
│   placeholder: leave blank for no priority                           │
│                                                                      │
│ Priority Phase 3                                                     │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ 1                                                              │   │
│ └────────────────────────────────────────────────────────────────┘   │
│   placeholder: leave blank for no priority                           │
└──────────────────────────────────────────────────────────────────────┘
[Submit]  [Cancel]
(Discord modal — opens overlay)
```

For `phase_count == 2`: Priority Phase 3 field omitted.

Submit validation per field:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Priority Phase 1 (1 = highest) must be a number — got `top`.     │
│ Reopen the zone to retry.                                            │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Submit success: `upsert_zone` lands all accumulated values
(capacity + minimums from page 1 stash + priorities from this
page) onto the buffer. `_clear_pending_edit` flushes the stash.
Editor embed re-renders with content line:

```
✏️ Updated **Power Tower** (3-phase).
```

---

### Screen 12.13 — Wizard wrong-user / unknown-step guards

**12.13a — Wrong user clicks the Next button on the bridge ephemeral:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the editor's owner can advance the wizard.                   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**12.13b — Unknown next_page (defensive guard; should never fire in practice):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Unknown wizard step `something_else`.                            │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 12.14 — Apply-to-similar prompt (after a zone edit lands)

When the just-edited zone has numbered siblings in the same
preset (e.g. `Sample Warehouse 1` has siblings `Sample Warehouse
2/3/4`), the bot follows up with an `_ApplyToSimilarView`:

```
💡 **Sample Warehouse 1** has similar zones in this preset:
Sample Warehouse 2, Sample Warehouse 3, Sample Warehouse 4. Would
you like to apply the same settings to these as well?
[ ▾ Select zones                                                   ]
[Apply to selected]  [Skip]
(ephemeral — only Kevin sees it; 5-min timeout)
```

The dropdown is `min_values=0, max_values=N` so the officer can
multi-select. Nothing is pre-selected.

**12.14a — Apply with nothing selected:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Pick at least one sibling from the dropdown first, or use Skip   │
│ to dismiss.                                                          │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**12.14b — Apply with selections:**

Editor message in place updates content + view; the dropdown
view edits to:

```
✅ Copied **Sample Warehouse 1** settings to 3 sibling(s): Sample
Warehouse 2, Sample Warehouse 3, Sample Warehouse 4.
[ ▾ Select zones (disabled)                                       ]
[Apply to selected (disabled)]  [Skip (disabled)]
(ephemeral)
```

Editor embed re-renders so the sibling zones now show the new
values.

**12.14c — Skip:**

```
OK — only the edited zone was changed.
[ ▾ Select zones (disabled)                                       ]
[Apply to selected (disabled)]  [Skip (disabled)]
(ephemeral)
```

**12.14d — Wrong user on the select / apply / skip:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who opened the editor can pick siblings.        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the editor's owner can apply changes.                        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the editor's owner can dismiss this prompt.                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**12.14e — Timeout (5 min):**

Buttons + select strip in place; no message body change.
Underlying edited zone is still saved.

**Silent skip:**

Zones with no numeric tail (Arsenal, Virus Lab, Power Tower —
the unique buildings) and zones where no sibling exists in the
preset trigger NO apply-to-similar prompt. The officer just sees
the editor refresh.

---

### Screen 12.15 — _(removed — Decision #13 / #174)_

The pre-#174 `➕ Add zone` button + `_AddZoneModal` are gone. Zones
come exclusively from `DS_ZONE_STRUCTURE` / `CS_ZONE_STRUCTURE`
(the canonical game-defined lists); alliances configure max-players,
minimum power, and priority for those canonical zones but can't add
new ones. The button is removed from every editor variant (12.1,
12.2, 12.3, 12.4) and the modal class is deleted.

---

### Screen 12.16 — Rename Preset modal

Kevin clicks `✏️ Rename preset`. `_RenameModal` opens with the
current name pre-filled.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Rename Preset                                                        │
│ ──────────────────────────────────────────                           │
│ New preset name                                                      │
│ ┌────────────────────────────────────────────────────────────────┐   │
│ │ Standard DS                                                    │   │
│ └────────────────────────────────────────────────────────────────┘   │
└──────────────────────────────────────────────────────────────────────┘
[Submit]  [Cancel]
(Discord modal — opens overlay)
```

**12.16a — Empty name:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ A preset name is required.                                        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**12.16b — Duplicate name (excluding the current name):**

`list_presets` runs off the event loop; case-insensitive lookup
excludes the current name (so re-saving with the same name is
fine).

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ A preset named **Other Preset** already exists. Pick a different │
│ name.                                                                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**12.16c — Submit success:**

`buf.name` updates, `dirty=True`. Editor refreshes with:

```
✏️ Renamed **Standard DS** → **Apex DS v2**.
```

---

### Screen 12.17 — Save Preset (happy path)

Kevin clicks `💾 Save preset`. Defers ephemerally, runs
`save_preset` off the event loop, then on success: disables all
buttons, posts a non-ephemeral followup, edits the editor
message in place with the final embed + disabled view, stops the
view.

Followup:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved preset **Standard DS** (11 zones, capacity 30).            │
└──────────────────────────────────────────────────────────────────────┘
(public — channel sees it; no buttons)
```

Editor in-place edit:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Editing Preset: Standard DS                                       │
│ …(embed body unchanged but the "⚠️ Unsaved changes" footer line is  │
│ gone because dirty=False post-save)…                                 │
│ 📊 Capacity: **30** (team size 30; flex room is fine) ✅            │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Edit a zone… (disabled)                                       ]
[ ▾ 🔀 Phase mode: Flat (no phases) (disabled)                     ]
[✏️ Rename preset (disabled)]
[💾 Save preset (disabled)]  [🔙 Abandon this preset (disabled)]
```

---

### Screen 12.18 — Save Preset (failure)

`save_preset` returned False (Sheet not configured, bot lacks
edit access, write raised). Buttons remain enabled so Kevin can
retry after fixing the underlying issue.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Could not save preset — check that your Google Sheet is          │
│ configured and that the bot has edit access. See logs for details.  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

The editor stays interactive; `dirty` stays True; the Save button
remains green and enabled.

---

### Screen 12.19 — Abandon

Kevin clicks `🔙 Abandon`. The cancel callback flips
`view.cancelled=True`, disables every button, edits the editor
message in place with a content line + the embed (so the
in-progress state is still visible for reference), stops the
view.

```
🔙 Abandoned. Changes were not saved.
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Editing Preset: Standard DS                                       │
│ …(embed body unchanged — shows the in-progress state for reference)│
│ ⚠️ *Unsaved changes — Save preset to save your changes.*                    │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Edit a zone… (disabled)                                       ]
[ ▾ 🔀 Phase mode (disabled)                                       ]
[✏️ Rename preset (disabled)]
[💾 Save preset (disabled)]  [🔙 Abandon this preset (disabled)]
```

Nothing written to the Sheet.

---

### Screen 12.20 — Editor wrong-user guard (all interactions)

The editor message is posted publicly so several leadership
members can watch. Only the original opener (Kevin) can mutate.
Every interactive component runs the same guard:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the editor's owner can change this preset.                   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only the wrong-clicker sees it)
```

Variant on the Save button:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the editor's owner can save this preset.                     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Variant on Abandon:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the editor's owner can abandon this preset.                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 12.21 — Editor timeout (15 min)

The editor's `on_timeout` calls `wizard_registry.expire_view_message`
with command_hint `/desertstorm strategy edit` (or `/canyonstorm strategy edit`).
The view buttons strip and a stale-notice line is appended below
the embed.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Editing Preset: Standard DS                                       │
│ …(embed body unchanged)…                                            │
└──────────────────────────────────────────────────────────────────────┘
⏰ This editor timed out. Re-open with `/desertstorm strategy edit
name:"Standard DS"`.
[ ▾ (disabled)                                                    ]
[ ▾ (disabled)                                                    ]
[✏️ Rename preset (disabled)]
[💾 Save preset (disabled)]  [🔙 Abandon this preset (disabled)]
```

(Exact stale-notice copy is owned by `expire_view_message`;
shape matches the other auto-post timeout pattern across the
bot.)

---

### Flow at a glance

```
/desertstorm strategy create / edit  OR  /canyonstorm strategy create / edit
                │
                ▼
   _open_editor → _PresetEditorView posted publicly
                │
                ▼
   12.1 (DS both) / 12.2 (DS single-team) / 12.3 (CS flat) /
   12.4 (CS 3-phase) — variant by event_type + teams + phase_count
                │
                ├──► [ ▾ Edit a zone… ] ─── picked
                │       │
                │       ├── phase_count == 0 → 12.7 _ZoneEditModal
                │       │       │
                │       │       ├── parse error → 12.8a/b/c/d
                │       │       └── ok          → 12.8e + 12.14 apply-to-similar?
                │       │
                │       └── phase_count >= 2 → 12.9 caps + min modal
                │               │
                │               ├── parse error in any field → 12.10 error
                │               └── ok → 12.10 Next bridge → 12.12 priority modal
                │                                              │
                │                                              ├── parse error
                │                                              └── ok → upsert_zone +
                │                                                    embed refresh +
                │                                                    12.14 apply-to-similar?
                │
                ├──► [ ▾ 🔀 Phase mode ] ─── 12.5 / 12.6 mode swap (seeds + refreshes)
                │
                ├──► [➕ Add zone] → 12.15 _AddZoneModal
                │       ├── empty/duplicate → 12.15a/b
                │       └── ok               → 12.15c append
                │
                ├──► [✏️ Rename] → 12.16 _RenameModal
                │       ├── empty/duplicate → 12.16a/b
                │       └── ok               → 12.16c rename
                │
                ├──► [💾 Save preset]
                │       ├── fail → 12.18 (view stays alive)
                │       └── ok   → 12.17 (public success + view disables)
                │
                ├──► [🔙 Abandon] → 12.19 (no Sheet write; view disables)
                │
                ├──► Wrong user on any control → 12.20
                │
                └──► 15-min timeout → 12.21 expire_view_message
```

---

## 13. `/desertstorm member_rule` + `/canyonstorm member_rule` commands

Per-member overrides + power-band eligibility rules. Two parallel
slash-command groups (DS + CS) backed by `storm_member_rules.py`.
Rules live in the `DS Member Rules` / `CS Member Rules` Sheet tab
with five columns: `Rule Type | Subject | Sub-Type | Value | Notes`.

The DS group exposes five subcommands; the CS group exposes four (no
`set_member_team` — Canyon Storm doesn't have A/B teams):

```
/desertstorm member_rule  set_power_band  | set_member_team | set_member_zone | set_member_role | list
/canyonstorm member_rule  set_power_band  |                 | set_member_zone | set_member_role | list
```

All subcommands are leadership-gated (admin OR the configured
`leadership_role_name` role) and guild-only.

---

### Screen 13.1 — `/desertstorm member_rule set_power_band` — slash help

Officer Kevin starts typing `/desertstorm member_rule set_power_band` in the
command bar. Discord's autocomplete surfaces the argument shape:

```
┌──────────────────────────────────────────────────────────────────────┐
│ /desertstorm member_rule set_power_band                                       │
│ Add a power-band eligibility rule for a zone                         │
│                                                                      │
│   threshold *  Minimum power (e.g. 250M, 1.2B, 300,000,000)          │
│   zone *       Zone the band applies to (e.g. Power Tower)           │
│   notes        Optional free-text notes                              │
└──────────────────────────────────────────────────────────────────────┘
```

The CS variant is identical except the threshold hint reads
`Minimum power (e.g. 250M)` and the zone hint drops the parenthetical.

---

### Screen 13.2 — `set_power_band` success

Kevin runs `/desertstorm member_rule set_power_band threshold:300M zone:Power Tower notes:Solo tank — keep top-tier in Power Tower for the chokepoint`.

The ack message is **public** (non-ephemeral — the rule is a roster
decision the alliance can see). `format_power(300_000_000)` renders
back as `300M`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: ≥ 300M → eligible for Power Tower.                         │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 13.3 — `set_power_band` success with non-canonical zone

Same command, but Kevin typed `zone:Power Towr` (typo). The rule
saves anyway — the bot doesn't gatekeep on spelling, but appends a
warning so the typo is visible:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: ≥ 300M → eligible for Power Towr.                          │
│ ⚠️ `Power Towr` isn't in the canonical zone list — the rule was      │
│ saved, but double-check the spelling.                                │
└──────────────────────────────────────────────────────────────────────┘
```

Canonical DS zones are sourced from `storm.DS_ZONE_STRUCTURE`:
`Nuclear Silo, Oil Refinery I, Oil Refinery II, Science Hub, Info
Center, Field Hospital I–IV, Arsenal, Mercenary Factory`. CS zones
come from `storm.CS_ZONE_STRUCTURE`.

---

### Screen 13.4 — `set_power_band` validation: unparseable threshold

Kevin typed `threshold:big` by accident:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't parse `big` as a power value. Try formats like `250M`,   │
│ `1.2B`, or `300,000,000`.                                            │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

Same message fires for negative numbers (e.g. `-50M`) — the `parse_power`
return is wrapped with an `n < 0` guard.

---

### Screen 13.5 — `set_power_band` validation: blank zone

Kevin somehow submitted with `zone:` empty (e.g. typed and erased
before pressing enter):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Provide a zone name (e.g. `Power Tower`).                         │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 13.6 — `set_power_band` validation: duplicate rule

A `(≥ 300M, Power Tower)` rule already exists. Kevin reruns the
exact same command:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ A matching rule already exists. Clear it first to update.         │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Duplicate detection is on `(rule_type, subject, value)` — same threshold
+ same zone. To change the threshold for the same zone (or vice versa)
the officer must run `/desertstorm member_rule list`, click the `🗑 Clear N`
button, and re-add.

---

### Screen 13.7 — `set_power_band` Sheet-write failure

Sheet credentials revoked between command start and save, or the
worksheet has been hard-deleted out from under the bot:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't write to the Sheet (see logs for details).               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

A bot-process log line accompanies it:
`[STORM RULES] save_rule failed for guild=… event=DS: <gspread error>`.

---

### Screen 13.8 — `set_power_band` no Sheet configured

The alliance hasn't run `/setup_desertstorm` yet, so
`config.get_spreadsheet(guild_id)` returns `None`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Your Google Sheet isn't configured. Run setup first.              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 13.9 — Permission denial (any subcommand)

A regular alliance member tries `/desertstorm member_rule set_power_band …`.
Standard `deny_non_leader` ephemeral fires before any work happens:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to run this command.      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 13.10 — `/desertstorm member_rule set_member_team` — slash help

```
┌──────────────────────────────────────────────────────────────────────┐
│ /desertstorm member_rule set_member_team                                      │
│ Lock a specific member to Team A or B                                │
│                                                                      │
│   team *        Team A or Team B    ▾  (Choice: Team A / Team B)     │
│   member_user   Pick from the server (preferred — keys by Discord    │
│                 ID, survives renames)                                │
│   member_name   OR a roster name if the member isn't on Discord      │
│   notes         Optional free-text notes                             │
└──────────────────────────────────────────────────────────────────────┘
```

`team` is a `Choice` dropdown — only `Team A` (value `A`) or
`Team B` (value `B`) are selectable, so a typo'd team value is
literally unreachable via the slash UI. The free-text validation in
13.14 only fires if a non-Choice client ever submits raw text.

---

### Screen 13.11 — `set_member_team` success via Discord picker

Kevin runs `/desertstorm member_rule set_member_team team:Team B member_user:@Bob notes:Veteran B-side caller`.

The bot resolves the member through `_resolve_subject` — `Bob` is a
real Discord member with display name `Bob`, so the subject stored
is `str(bob.id)` and the display rendered back is `Bob.display_name`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: Bob → plays Team B.                                        │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 13.12 — `set_member_team` success via free-text `member_name`

Charlie isn't on Discord — he's tracked only in the roster Sheet.
Kevin runs `/desertstorm member_rule set_member_team team:Team A member_name:Charlie #42`.

`_resolve_subject` checks `member_user` first (None), then takes the
free-text path. It also tries a case-insensitive display-name match
against the guild — `Charlie #42` doesn't match any Discord member,
so it's stored verbatim:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: Charlie #42 → plays Team A.                                │
└──────────────────────────────────────────────────────────────────────┘
```

(If `Charlie #42` HAD matched a single Discord member's display name
case-insensitively, the subject would be normalized to that member's
Discord ID — to prevent duplicate rules. Ambiguous matches keep the
typed-name form.)

---

### Screen 13.13 — `set_member_team` validation: neither input given

Kevin forgot to pass either `member_user` or `member_name`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Provide a member. Pick from the typeahead (server member) OR      │
│ type a roster name (non-Discord member) — exactly one, not both.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 13.14 — `set_member_team` validation: both inputs given

Kevin passed `member_user:@Bob` AND `member_name:Bob` (likely tab-completed
both fields). Same message:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Provide a member. Pick from the typeahead (server member) OR      │
│ type a roster name (non-Discord member) — exactly one, not both.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

The both-given branch is the same return-`(None, "")` path inside
`_resolve_subject`, so the message is identical to "neither given."

---

### Screen 13.15 — `set_member_team` validation: bot picked

Kevin's autocomplete latched onto the bot user itself in `member_user`
(Discord's picker doesn't filter bots). Same denial — saved-against-a-bot
rules would silently never resolve at apply time, so we reject upfront:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Provide a member. Pick from the typeahead (server member) OR      │
│ type a roster name (non-Discord member) — exactly one, not both.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 13.16 — `set_member_team` validation: invalid team

Unreachable through the slash UI (team is a Choice), but if a raw
text value somehow lands:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Team must be `A` or `B`. Got `C`.                                 │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 13.17 — `set_member_team` on CS group is rejected

The CS group has no `set_member_team` subcommand registered, so this
screen normally can't be reached via the slash UI. The defensive
guard inside `_set_member_team` covers the case where DS and CS code
ever share a code path:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ `team` rules only apply to Desert Storm. Use the zone or          │
│ special_role commands for Canyon Storm.                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 13.18 — `/desertstorm member_rule set_member_zone` — slash help

```
┌──────────────────────────────────────────────────────────────────────┐
│ /desertstorm member_rule set_member_zone                                      │
│ Lock a specific member to a zone                                     │
│                                                                      │
│   zone *        Zone they always play                                │
│   member_user   Pick from the server (preferred)                     │
│   member_name   OR a roster name if the member isn't on Discord      │
│   notes         Optional free-text notes                             │
└──────────────────────────────────────────────────────────────────────┘
```

CS variant is identical aside from the group name in the header.

---

### Screen 13.19 — `set_member_zone` success (Discord picker)

Kevin runs `/desertstorm member_rule set_member_zone zone:Power Tower member_user:@Alice notes:Tank role`.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: Alice → always at Power Tower.                             │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 13.20 — `set_member_zone` success with non-canonical zone

Kevin typed `zone:Powr Tower` (typo). Rule saves anyway with the same
non-canonical-zone caveat as the power-band path, but the wording is
subtly different (`saved anyway` vs `the rule was saved`):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: Alice → always at Powr Tower.                              │
│ ⚠️ `Powr Tower` isn't in the canonical zone list — saved anyway;     │
│ double-check the spelling.                                           │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 13.21 — `set_member_zone` validation: blank zone

Kevin somehow submitted `zone:` empty (whitespace-only counts):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ `zone` is required.                                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 13.22 — `set_member_zone` validation: subject errors

The neither / both / bot-picked / duplicate / no-Sheet / Sheet-write
errors are identical to Screens 13.13 / 13.14 / 13.15 / 13.6 / 13.8 /
13.7 — same `_resolve_subject` + `save_rule` paths.

---

### Screens 13.23 / 13.24 / 13.25 — _(removed — Rule G / #167)_

The pre-#167 `set_member_role` subcommand + the `special_role`
rule type are both gone. The Judicator / Commander tagging
mechanism was retired across the schema, the cog, the member-rule
list rendering (no more `🎖️` row), and the post-Approve faction-
roles offer (Section 9). Per-member rules are now `team` and
`zone` only.

---

### Screen 13.26 — `/desertstorm member_rule list` — slash help

```
┌──────────────────────────────────────────────────────────────────────┐
│ /desertstorm member_rule list                                                 │
│ Show all saved DS member rules (with Clear buttons)                  │
│                                                                      │
│   member   Optional — filter to one member's rules                   │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 13.27 — `list` with no rules yet

Kevin runs `/desertstorm member_rule list` on a fresh alliance:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Member Rules                                       │
│                                                                      │
│ *No member rules saved yet.*                                         │
└──────────────────────────────────────────────────────────────────────┘
[➕ Add rule]
```

Per #169 / Rule M, the empty state still ships the `[➕ Add rule]`
button so officers can bootstrap their first rule from the list view
without remembering the slash-subcommand names. Clicking it opens
the rule-type picker (Screen 13.27a). No Clear buttons (no rules to
clear). No pagination footer (only one page).

### Screen 13.27a — Add rule type picker

Click on `[➕ Add rule]` opens an ephemeral with two choices:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ➕ Pick the rule type to add.                                        │
└──────────────────────────────────────────────────────────────────────┘
[⚡ Add a power-band rule]  [👤 Add a per-member rule]  [↩️ Cancel]
(ephemeral)
```

- `⚡ Add a power-band rule` → opens the same `InlinePowerBandView`
  the setup wizard uses (Screen 2.14c) — zone Select gates a
  one-field power modal.
- `👤 Add a per-member rule` → ephemeral pointer at the slash
  commands, since per-member rules need a `discord.Member` picker
  that Discord modals can't host:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👤 Per-member rules need a server-member picker, which Discord      │
│ doesn't expose inside a modal. Run one of:                          │
│ • `/desertstorm member_rule set_member_zone` — pin a member to a    │
│ specific zone.                                                       │
│ • `/desertstorm member_rule set_member_team` — pin a member to      │
│ Team A or Team B.                                                    │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 13.28 — `list` with a mix of rules

After Kevin has saved the example rules in this section,
`/desertstorm member_rule list` renders:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Member Rules                                       │
│                                                                      │
│ ` 1` · ⚖️  ≥ 300M → eligible for Power Tower                         │
│      ↳ Solo tank — keep top-tier in Power Tower for the chokepoint   │
│ ` 2` · 👤  Bob → plays Team B                                        │
│      ↳ Veteran B-side caller                                         │
│ ` 3` · 👤  Charlie #42 → plays Team A                                │
│ ` 4` · 👤  Alice → always at Power Tower                             │
│      ↳ Tank role                                                     │
│ ` 5` · ⚖️  ≥ 250M → eligible for Field Hospital I                    │
│ ` 6` · 👤  Dan → always at Arsenal                                   │
└──────────────────────────────────────────────────────────────────────┘
[🗑 Clear 1]  [🗑 Clear 2]  [🗑 Clear 3]  [🗑 Clear 4]  [🗑 Clear 5]
[🗑 Clear 6]  [➕ Add rule]
```

Notes:
- Rule numbering is 1-based and matches the Sheet row order
  (rules_tab top-down, header excluded). Index is stable across reads
  so an officer can re-list and click the same number.
- Each rule on a separate line with the icon dictated by the rule
  type: `⚖️` for power-band, `👤` for per-member team/zone. (Pre-#167
  there was a `🎖️` row for `set_member_role` rules — that subcommand
  + the special_role rule type were both retired with Rule G.)
- Optional `notes` cell renders as a follow-on italic line with `↳ _…_`.
- Discord-ID subjects (Bob, Alice, Dan) resolve through
  `resolve_subject_display` to the **current** Discord display name.
  If Alice renames to `AliceTank` between rule creation and now, this
  embed shows `AliceTank`. Charlie #42 is stored verbatim (non-Discord
  roster member) and renders as typed.
- All rules render with bold names (Discord markdown) — the ASCII
  approximation drops the `**…**` formatting.
- The `[➕ Add rule]` button sits on row 4 alongside any pagination
  buttons; opens Screen 13.27a.

---

### Screen 13.29 — `list` with pagination (>20 rules)

A large alliance with 27 saved rules. Page 1 renders rules 1–20:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Member Rules                                       │
│                                                                      │
│ ` 1` · ⚖️  ≥ 400M → eligible for Power Tower                         │
│ ` 2` · ⚖️  ≥ 300M → eligible for Field Hospital I                    │
│ ` 3` · ⚖️  ≥ 250M → eligible for Field Hospital II                   │
│ ` 4` · ⚖️  ≥ 200M → eligible for Mercenary Factory                   │
│ ` 5` · 👤  Alice → always at Power Tower                             │
│ ` 6` · 👤  Bob → plays Team B                                        │
│ ` 7` · 👤  Carol → plays Team A                                      │
│ ` 8` · 👤  Dan → always at Arsenal                                   │
│ ` 9` · 👤  Erin → always at Nuclear Silo                             │
│ `10` · 🎖️  Frank → Commander candidate                               │
│ `11` · 🎖️  Gina → Judicator candidate                                │
│ `12` · 👤  Henry → plays Team A                                      │
│ `13` · 👤  Ivy → always at Oil Refinery I                            │
│ `14` · 👤  Jack → always at Oil Refinery II                          │
│ `15` · 👤  Kate → plays Team B                                       │
│ `16` · 👤  Liam → always at Info Center                              │
│ `17` · 👤  Mia → plays Team A                                        │
│ `18` · 🎖️  Noah → Commander candidate                                │
│ `19` · 👤  Olivia → always at Field Hospital III                     │
│ `20` · 👤  Pete → always at Field Hospital IV                        │
│                                                                      │
│ Page 1/2                                                             │
└──────────────────────────────────────────────────────────────────────┘
[🗑 Clear 1]  [🗑 Clear 2]  [🗑 Clear 3]  [🗑 Clear 4]  [🗑 Clear 5]
[🗑 Clear 6]  [🗑 Clear 7]  [🗑 Clear 8]  [🗑 Clear 9]  [🗑 Clear 10]
[🗑 Clear 11] [🗑 Clear 12] [🗑 Clear 13] [🗑 Clear 14] [🗑 Clear 15]
[🗑 Clear 16] [🗑 Clear 17] [🗑 Clear 18] [🗑 Clear 19] [🗑 Clear 20]
[◀ Prev (disabled)]  [Next ▶]
```

Discord caps a View at 25 components, so the page size is 20 Clear
buttons + 1 Prev + 1 Next = 22. The Prev/Next pair lives on row 4.

---

### Screen 13.30 — `list` page 2

Kevin clicks `[Next ▶]`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Member Rules                                       │
│                                                                      │
│ `21` · 👤  Quinn → plays Team B                                      │
│ `22` · 👤  Riley → always at Arsenal                                 │
│ `23` · 🎖️  Sam → Judicator candidate                                 │
│ `24` · 👤  Theo → always at Mercenary Factory                        │
│ `25` · 👤  Uma → plays Team A                                        │
│ `26` · 👤  Victor → plays Team B                                     │
│ `27` · 👤  Wendy → always at Science Hub                             │
│                                                                      │
│ Page 2/2                                                             │
└──────────────────────────────────────────────────────────────────────┘
[🗑 Clear 21] [🗑 Clear 22] [🗑 Clear 23] [🗑 Clear 24] [🗑 Clear 25]
[🗑 Clear 26] [🗑 Clear 27]
[◀ Prev]  [Next ▶ (disabled)]
```

Clear-button indices stay aligned with the master list — clicking
`🗑 Clear 27` deletes the 27th rule (Wendy), not "the 7th rule on
this page."

---

### Screen 13.31 — `list` pagination owner guard

Another officer (Alice, leadership but not the one who ran the
command) clicks `[Next ▶]` on Kevin's list view. Ephemeral denial
fires from the inner `_prev` / `_next` callbacks:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the command owner can paginate.                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Alice sees it)
```

The page stays on whichever page Kevin had it on.

---

### Screen 13.32 — Clear-rule click (the happy path)

Kevin clicks `[🗑 Clear 2]` on the Screen-13.28 embed. The bot:
1. defers the interaction
2. runs `delete_rule_at` on the Sheet (atomic `delete_rows`)
3. reloads the rule list from the Sheet
4. rebuilds the Clear buttons
5. edits the original ephemeral in place

The displayed embed re-renders (rule 2 vanishes, rules 3+ slide up by
one):

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Member Rules                                       │
│                                                                      │
│ ` 1` · ⚖️  ≥ 300M → eligible for Power Tower                         │
│      ↳ Solo tank — keep top-tier in Power Tower for the chokepoint   │
│ ` 2` · 👤  Charlie #42 → plays Team A                                │
│ ` 3` · 👤  Alice → always at Power Tower                             │
│      ↳ Tank role                                                     │
│ ` 4` · 🎖️  Carol → Judicator candidate                               │
│      ↳ Backup tank slot                                              │
│ ` 5` · ⚖️  ≥ 250M → eligible for Field Hospital I                    │
│ ` 6` · 👤  Dan → always at Arsenal                                   │
└──────────────────────────────────────────────────────────────────────┘
[🗑 Clear 1]  [🗑 Clear 2]  [🗑 Clear 3]  [🗑 Clear 4]  [🗑 Clear 5]
[🗑 Clear 6]
```

No followup ack — the in-place edit IS the acknowledgement. The
deleted rule is gone from the Sheet too.

---

### Screen 13.33 — Clear-rule click by non-owner

Alice (another officer) clicks `[🗑 Clear 1]` on Kevin's list view:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the user who ran the command can clear rules from this list. │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Alice sees it)
```

The rule is **not** deleted. Kevin's view is unchanged. Alice can
run her own `/desertstorm member_rule list` to get her own version of the view.

---

### Screen 13.34 — Clear-rule click after Sheet write fails

Kevin clicks `[🗑 Clear 3]` but the Sheet I/O fails (revoked creds,
deleted tab, rate-limit). The bot already deferred, so the failure
surfaces as a followup ephemeral:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't remove that rule. Rerun the list command to refresh.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

The original list view stays as-is (no edit). The "rerun the list
command" guidance is there because the same failure could mean the
on-screen indices are stale (someone else deleted a row in the Sheet
directly).

---

### Screen 13.35 — `list` view timeout

Kevin's list view sits idle for 5 minutes (`timeout=300`). On
timeout, `on_timeout` greys out every button — both the Clear buttons
and the pagination pair — so a stale click doesn't surface
"Interaction failed":

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Member Rules                                       │
│                                                                      │
│ ` 1` · ⚖️  ≥ 300M → eligible for Power Tower                         │
│      ↳ Solo tank — keep top-tier in Power Tower for the chokepoint   │
│ ` 2` · 👤  Bob → plays Team B                                        │
│      ↳ Veteran B-side caller                                         │
│ ` 3` · 👤  Charlie #42 → plays Team A                                │
│ … (rest of rules) …                                                  │
└──────────────────────────────────────────────────────────────────────┘
[🗑 Clear 1 (disabled)]  [🗑 Clear 2 (disabled)]  [🗑 Clear 3 (disabled)]
… (all buttons greyed out) …
```

To delete more rules, Kevin re-runs `/desertstorm member_rule list`.

---

### Screen 13.36 — `list` with `member` filter

Kevin runs `/desertstorm member_rule list member:Alice`. The bot filters down
to per-member rules whose subject either equals the typed string
verbatim (case-insensitive) OR resolves to that display name through
`resolve_subject_display`. Power-band rules are filtered out (they
don't have a member subject):

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Member Rules                                       │
│                                                                      │
│ ` 1` · 👤  Alice → always at Power Tower                             │
│      ↳ Tank role                                                     │
└──────────────────────────────────────────────────────────────────────┘
[🗑 Clear 1]
```

Note: the displayed index resets — it's `1` even though Alice's rule
is at row N of the Sheet. The displayed index passes through to
`delete_rule_at` against the *filtered* list, so clicking `🗑 Clear 1`
here is internally `delete_rule_at(idx=0)` over the filtered list and
deletes Alice's rule.

---

### Screen 13.37 — `list` with `member` filter, no matches

Kevin runs `/desertstorm member_rule list member:Zachary` (no rules for him):

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Member Rules                                       │
│                                                                      │
│ No member rules saved yet.                                           │
└──────────────────────────────────────────────────────────────────────┘
```

The same empty-state copy as Screen 13.27. No buttons.

---

### Screen 13.38 — CS group parity

The `/canyonstorm member_rule` group is identical to `/desertstorm member_rule` minus
`set_member_team`. CS rule embeds say "Canyon Storm" in the title:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Canyon Storm — Member Rules                                       │
│                                                                      │
│ ` 1` · ⚖️  ≥ 300M → eligible for Power Tower                         │
│ ` 2` · 👤  Alice → always at Power Tower                             │
│ ` 3` · 🎖️  Carol → Judicator candidate                               │
└──────────────────────────────────────────────────────────────────────┘
[🗑 Clear 1]  [🗑 Clear 2]  [🗑 Clear 3]
```

Success acks render the same — DS / CS distinction lives in the
underlying tab, not in the user-facing copy of `set_*` commands.

---

### Flow at a glance

```
Officer runs slash command in their #ops channel
  │
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ /desertstorm member_rule … (or /canyonstorm member_rule …)                            │
│                                                                     │
│   ├── set_power_band      → 13.1 → 13.2 / 13.3 / 13.4-13.8          │
│   │   (threshold + zone + notes)                                    │
│   │                                                                 │
│   ├── set_member_team (DS only)                                     │
│   │   (member_user|member_name + team + notes)                      │
│   │   → 13.10 → 13.11 / 13.12 / 13.13-13.17                         │
│   │                                                                 │
│   ├── set_member_zone                                               │
│   │   (member_user|member_name + zone + notes)                      │
│   │   → 13.18 → 13.19 / 13.20 / 13.21 / 13.22                       │
│   │                                                                 │
│   ├── set_member_role                                               │
│   │   (member_user|member_name + role + notes)                      │
│   │   → 13.23 → 13.24 / 13.25                                       │
│   │                                                                 │
│   └── list  (optional member filter)                                │
│       → 13.26 → 13.27 / 13.28 / 13.29-13.30 (pagination)            │
│       │                                                             │
│       └── [🗑 Clear N]  → 13.32 (success) / 13.33 (non-owner) /     │
│                          13.34 (sheet fail)                         │
│       └── [◀ Prev] [Next ▶]  → 13.30 / 13.31 (non-owner)            │
│       └── 5-min idle  → 13.35 (view stripped)                       │
└─────────────────────────────────────────────────────────────────────┘
  │
  ▼
Sheet tab `DS Member Rules` (or `CS Member Rules`) updated.
Rules feed into the strategy / roster-builder apply path (#126/#129).
```

---

## 14. Walkthrough tour

A first-run guided tour on `/desertstorm signups` **and**
`/canyonstorm signups` (per #170 / Rule N + Decision #12). The bot
offers it once per (guild, officer, walkthrough-key) tuple — clicking
either button records the dismissal so the offer never reappears. The
walkthrough key encodes a version (`storm_signups_v1`) so a future UI
rewrite can re-offer the tour without losing per-officer dismissals.
The version key is shared across DS and CS — dismissing on one
silences the other for that officer.

Lives in `storm_walkthrough.py`. Triggered from `handle_storm_signups`
(which both DS and CS slash commands route through) via
`maybe_offer_storm_signups_tour(interaction, event_type=..., teams=...)`
after the officer view embed has been rendered. The `event_type` and
`teams` params let the tour copy branch on the actual UI the officer
will see — CS officers don't see a "Desert Storm" pointer in Step 6,
and single-team alliances don't see both team buttons mentioned in
Step 5.

---

### Screen 14.1 — First-time offer

Kevin runs `/desertstorm signups` (or `/canyonstorm signups`) for
the first time. The officer view renders normally, then a second
ephemeral followup appears:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👋 First time opening the Desert Storm sign-ups view? Want a quick  │
│ walkthrough of what each piece does?                                 │
└──────────────────────────────────────────────────────────────────────┘
[👋 Walk me through this]  [No thanks]
(ephemeral — only Kevin sees it)
```

The header text branches on `event_type` — CS officers see
"Canyon Storm sign-ups view" in the header. Per-officer dismissal
state is shared (`storm_signups_v1`), so dismissing on DS silences
the offer on CS too.

`[👋 Walk me through this]` is `ButtonStyle.success` (green).
`[No thanks]` is `ButtonStyle.secondary` (grey). Both record the
walkthrough as dismissed regardless of which one is clicked.

---

### Screen 14.2 — Offer clicked by someone else

Hypothetical: Alice (another officer) somehow clicks Kevin's tour-offer
buttons — defensive even though the message is ephemeral and only
Kevin should see it.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ This walkthrough was offered to someone else.                     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Alice sees it)
```

Kevin's offer view stays clickable for him.

---

### Screen 14.3 — Offer view timeout

Kevin runs `/desertstorm signups`, sees the offer, and walks away. After
5 minutes the offer view times out. `on_timeout` strips the buttons
in-place but **does not** record dismissal — the next time Kevin
runs `/desertstorm signups`, the offer reappears:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👋 First time using `/desertstorm signups`? Want a quick walkthrough of    │
│ what each piece does?                                                │
└──────────────────────────────────────────────────────────────────────┘
[👋 Walk me through this (disabled)]  [No thanks (disabled)]
```

---

### Screen 14.4 — `[No thanks]` clicked

Kevin clicks `[No thanks]`. The offer view stops, both buttons grey
out, and the body text is replaced in place:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👍 Got it — won't ask again. Run `/help` any time and pick Desert    │
│ Storm or Canyon Storm if you want a refresher.                       │
│ a refresher.                                                         │
└──────────────────────────────────────────────────────────────────────┘
[👋 Walk me through this (disabled)]  [No thanks (disabled)]
```

`config.dismiss_walkthrough(guild_id, kevin.id, "storm_signups_v1")`
fires before the edit — the next `/desertstorm signups` for Kevin won't
reoffer the tour.

---

### Screen 14.5 — `[👋 Walk me through this]` clicked → start ack

Kevin clicks `[👋 Walk me through this]`. The offer view stops,
dismisses, and edits in place to:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Starting the tour…                                                │
└──────────────────────────────────────────────────────────────────────┘
[👋 Walk me through this (disabled)]  [No thanks (disabled)]
```

A new ephemeral followup carrying Step 1 is sent immediately
afterwards (Screen 14.6).

---

### Screen 14.6 — Tour Step 1 / 6 — The buckets

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 1 / 6 — The buckets                                             │
│ The embed groups everyone by their current vote: 🅰️ Team A,          │
│ 🅱️ Team B, 🔄 Either, ❌ Cannot, and ❓ Not voted yet. The counter   │
│ in the title tells you the total members you're tracking.            │
└──────────────────────────────────────────────────────────────────────┘
[Next →]  [Skip the rest]
(ephemeral)
```

`[Next →]` is `ButtonStyle.primary` (blurple).
`[Skip the rest]` is `ButtonStyle.secondary` (grey).

---

### Screen 14.7 — Tour Step 2 / 6 — Who's already assigned

Kevin clicks `[Next →]` on Step 1. The Step-1 view is greyed out
(buttons disabled in place) and a fresh ephemeral followup arrives:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 2 / 6 — Who's already assigned                                  │
│ Members already slotted into a roster for this event render with     │
│ strikethrough. That way you can scan at a glance for who's left to   │
│ place when you're building out a team.                               │
└──────────────────────────────────────────────────────────────────────┘
[Next →]  [Skip the rest]
(ephemeral)
```

---

### Screen 14.8 — Tour Step 3 / 6 — Members who aren't on Discord

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 3 / 6 — Members who aren't on Discord                           │
│ If your roster Sheet flags a row with `not_on_discord`, that member  │
│ surfaces in the buckets just like everyone else (marked with ¹).     │
│ They won't vote themselves — you cast their vote with 🙋 Record      │
│ on-behalf vote, and the bot logs that you recorded it.               │
└──────────────────────────────────────────────────────────────────────┘
[Next →]  [Skip the rest]
(ephemeral)
```

---

### Screen 14.9 — Tour Step 4 / 6 — Recording on-behalf votes

Per #170 / Decision #12: rewritten for the post-#168 ephemeral
picker. No more "type the name, typos are rejected" framing — the
picker sources from the roster Sheet so typos are impossible.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 4 / 6 — Recording on-behalf votes                               │
│ Click 🙋 Record on-behalf vote to open the picker. Pick the member   │
│ from the dropdown (sourced from your roster Sheet — no free typing,  │
│ so typos can't slip through), pick a vote (the options match the     │
│ team buttons members see on the sign-up post), then hit Submit.      │
│ Each on-behalf vote captures your Discord ID for audit.              │
└──────────────────────────────────────────────────────────────────────┘
[Next →]  [Skip the rest]
(ephemeral)
```

---

### Screen 14.10 — Tour Step 5 / 6 — Setting up a team

Per #170, the Set-up button copy branches on `cfg.teams` (and the
event label flows from `event_type`). DS examples below; CS swaps
in "Canyon Storm" and CS's Set-up buttons.

**DS / CS with `teams=both`:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 5 / 6 — Setting up a team                                       │
│ When you're ready to build a Desert Storm roster, click 🅰️ Set up   │
│ Team A or 🅱️ Set up Team B. The bot will ask which preset to use,   │
│ then open the roster builder pre-filtered to members who signed up,  │
│ with eligibility minimums enforced.                                  │
└──────────────────────────────────────────────────────────────────────┘
[Next →]  [Skip the rest]
(ephemeral)
```

**Single-team alliance (`teams=A`):**

```
│ When you're ready to build a Desert Storm roster, click 🅰️ Set up   │
│ Team A. The bot will ask which preset to use, then open the roster  │
│ builder pre-filtered to members who signed up, with eligibility     │
│ minimums enforced.                                                  │
```

**`teams=B`:** identical, swap "Set up Team A" for "Set up Team B".

---

### Screen 14.11 — Tour Step 6 / 6 — That's the tour

Kevin clicks `[Next →]` on Step 5. Because Step 6 is the final step
(`is_last=True`), the view renders a single `[Close]` button in
`ButtonStyle.success` (green) — no Next/Skip. The pointer at the
`/help` category branches on `event_type` so CS officers are pointed
at Canyon Storm, not Desert Storm:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6 / 6 — That's the tour                                         │
│ You can run `/help` any time and pick Desert Storm from the          │
│ dropdown to revisit the command list. Closing this message drops    │
│ you back to the live officer view.                                   │
└──────────────────────────────────────────────────────────────────────┘
[Close]
(ephemeral)
```

CS variant swaps "Desert Storm" for "Canyon Storm" in the body.

---

### Screen 14.12 — `[Close]` clicked on the final step

Kevin clicks `[Close]`. The view stops, the button greys out in
place, and the body text stays as-is — the tour quietly ends:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6 / 6 — That's the tour                                         │
│ You can run `/help` any time and pick Desert Storm or Canyon Storm   │
│ from the dropdown to revisit the command list. Closing this message  │
│ drops you back to the live officer view.                             │
└──────────────────────────────────────────────────────────────────────┘
[Close (disabled)]
```

---

### Screen 14.13 — `[Skip the rest]` clicked mid-tour

Kevin clicks `[Skip the rest]` while on Step 3. The view stops, the
button greys out, and the original step text is appended with a
"skipped" marker:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 3 / 6 — Members who aren't on Discord                           │
│ If your roster Sheet flags a row with `not_on_discord`, that member  │
│ surfaces in the buckets just like everyone else (marked with ¹).     │
│ They won't vote themselves — you cast their vote with 🙋 Record      │
│ on-behalf vote, and the bot logs that you recorded it.               │
│                                                                      │
│ _(tour skipped)_                                                     │
└──────────────────────────────────────────────────────────────────────┘
[Next → (disabled)]  [Skip the rest (disabled)]
```

No further followup is sent. The walkthrough dismissal was already
recorded when Kevin clicked `[👋 Walk me through this]` on the
offer view — so skipping mid-tour doesn't bring the offer back next
time either.

---

### Screen 14.14 — Tour-step button clicked by a different officer

Hypothetical: Alice somehow clicks `[Next →]` on Kevin's Step-2
view. Same owner-guard pattern as the offer view:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ This walkthrough was offered to someone else.                     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Alice sees it)
```

Kevin's tour step stays untouched. Same denial fires from `[Skip the
rest]` or `[Close]` clicks by a non-owner.

---

### Screen 14.15 — Tour-step view timeout

A tour step view's `timeout=600` (10 minutes — longer than the
offer view's 5 minutes, since reading a step takes longer than
clicking through an offer). If Kevin walks away mid-tour, the
current step's buttons grey out in place:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 4 / 6 — Recording on-behalf votes                               │
│ Open the modal, type the member's roster name (it must match the     │
│ Sheet exactly — typos are rejected), and pick A / B / Either /       │
│ Cannot. Each on-behalf vote captures your Discord ID for audit.      │
└──────────────────────────────────────────────────────────────────────┘
[Next → (disabled)]  [Skip the rest (disabled)]
```

Since the offer was already accepted, `/desertstorm signups` won't re-offer
the tour to Kevin. He can still browse the command list via `/help`
→ Desert Storm / Canyon Storm dropdown (per Step 6 copy).

---

### Screen 14.16 — Second-time `/desertstorm signups` (offer skipped)

Kevin runs `/desertstorm signups` a second time, days later. Because
`config.is_walkthrough_dismissed(guild_id, kevin.id, "storm_signups_v1")`
returns `True`, `maybe_offer_storm_signups_tour` returns early with
no message:

```
( officer view embed renders normally, no offer ephemeral attached )
```

---

### Screen 14.17 — Different officer's first time

Alice (a different officer) runs `/desertstorm signups` for the first
time on the same alliance. Her own dismissal state is independent of
Kevin's — she sees the same offer Kevin saw (Screen 14.1).

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👋 First time using `/desertstorm signups`? Want a quick walkthrough of    │
│ what each piece does?                                                │
└──────────────────────────────────────────────────────────────────────┘
[👋 Walk me through this]  [No thanks]
(ephemeral — only Alice sees it)
```

---

### Flow at a glance

```
Officer runs /desertstorm signups
  │
  ▼
maybe_offer_storm_signups_tour()
  │
  ├── already-dismissed?  → no offer (Screen 14.16)
  │
  ▼ first time
┌─────────────────────────────────────────────────────────────────────┐
│  14.1  Offer ephemeral                                              │
│                                                                     │
│    [No thanks]                 [👋 Walk me through this]            │
│        │                              │                             │
│        ▼ dismisses, edits in place    ▼ dismisses + starts          │
│   14.4 "Got it — won't ask           14.5 "✅ Starting the tour…"   │
│        again." (greyed)                   (greyed)                  │
│                                       │                             │
│                                       ▼                             │
│                            ┌──────────────────────────────┐         │
│                            │ 14.6  Step 1 / 6 — Buckets   │         │
│                            │   [Next →] [Skip the rest]   │         │
│                            └──────────────────────────────┘         │
│                                       │                             │
│                                       ▼ Next                        │
│                            ┌──────────────────────────────┐         │
│                            │ 14.7  Step 2 / 6 — Assigned  │         │
│                            └──────────────────────────────┘         │
│                                       │                             │
│                                       ▼ Next                        │
│                            ┌──────────────────────────────┐         │
│                            │ 14.8  Step 3 / 6 — Non-DC    │         │
│                            └──────────────────────────────┘         │
│                                       │                             │
│                                       ▼ Next                        │
│                            ┌──────────────────────────────┐         │
│                            │ 14.9  Step 4 / 6 — Modal     │         │
│                            └──────────────────────────────┘         │
│                                       │                             │
│                                       ▼ Next                        │
│                            ┌──────────────────────────────┐         │
│                            │ 14.10 Step 5 / 6 — Set up    │         │
│                            └──────────────────────────────┘         │
│                                       │                             │
│                                       ▼ Next                        │
│                            ┌──────────────────────────────┐         │
│                            │ 14.11 Step 6 / 6 — Close     │         │
│                            │       [Close]                │         │
│                            └──────────────────────────────┘         │
│                                       │                             │
│                                       ▼ Close                       │
│                            14.12  view greyed in place              │
│                                                                     │
│   Any step:  [Skip the rest] → 14.13 step text + "(tour skipped)"   │
│   Any view:  10-min timeout  → 14.15 view greyed                    │
│   Any view:  non-owner click → 14.2 / 14.14 denial                  │
└─────────────────────────────────────────────────────────────────────┘
  │
  ▼
Dismissal recorded in SQLite (guild_id, user_id,
"storm_signups_v1"). Subsequent /desertstorm signups for Kevin in this
guild does NOT re-offer (14.16). Alice's first run still does (14.17).
```

---

## 15. History browser

`/desertstorm strategy roster_history [date]` and `/canyonstorm strategy roster_history
[date]` browse the historical structured-roster archive. Data comes
from the `rosters_tab` (written by Approve & Post in Flow 9) joined
with `attendance_tab` (written by `/desertstorm attendance` in Flow 10).

Lives in `storm_history.py`. Read-only — corrections route through
re-running the roster builder and re-recording attendance.

Both subcommands are leadership-gated AND require Premium + the
structured flow turned on for that event type.

---

### Screen 15.1 — `/desertstorm strategy roster_history` — slash help

```
┌──────────────────────────────────────────────────────────────────────┐
│ /desertstorm strategy roster_history                                          │
│ Browse past DS rosters with attendance overlaid                      │
│                                                                      │
│   date   Optional — show a specific date (May 18, 5/18, 2026-05-18,  │
│          yesterday). Omit to list recent events.                     │
└──────────────────────────────────────────────────────────────────────┘
```

The CS variant is the same shape with "CS rosters" / "Canyon Storm"
substituted.

---

### Screen 15.2 — Permission denied (non-leader)

A regular member tries `/desertstorm strategy roster_history`. The standard
`deny_non_leader` ephemeral fires before any work:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to run this command.      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 15.3 — Premium gate

A non-Premium alliance's officer tries `/desertstorm strategy roster_history`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔒 `/desertstorm strategy roster_history` is a 💎 Premium feature. Run        │
│ `/upgrade` to unlock it.                                             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 15.4 — Structured-flow not enabled

The alliance has Premium but the officer never enabled
`structured_flow_enabled` for Desert Storm in `/setup_desertstorm`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The structured roster flow isn't enabled for Desert Storm. Run    │
│ `/setup_desertstorm` and turn on Structured Roster Flow first.       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

CS analogue points to `/setup_canyonstorm`.

---

### Screen 15.5 — Used in a DM (no guild context)

Edge case: the command somehow fires outside a guild:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ This command must be used inside a server.                        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 15.6 — No `date` — recent-events list (happy path)

Kevin runs `/desertstorm strategy roster_history` with no `date` argument.
The bot defers, reads `list_event_dates` (top 8 distinct ISO dates
from the rosters tab, descending), and posts:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm — Recent Rosters                                     │
│                                                                      │
│ Click a date below to view the roster + attendance.                  │
└──────────────────────────────────────────────────────────────────────┘
[Mon May 18]  [Mon May 11]  [Mon May 4]  [Mon Apr 27]  [Mon Apr 20]
(ephemeral — only Kevin sees it)
```

Button labels come from `format_event_date_compact` (`"%a %b %-d"`-ish).
Button style is `ButtonStyle.secondary`. The view sits on
`timeout=300` and is owner-guarded — only Kevin can click.

The full source supports up to 8 buttons; this example shows the 5
most-recent dates per the prompt. With 8 events the row would
continue: `[Sun Apr 13]  [Sun Apr 6]  [Sun Mar 30]`.

---

### Screen 15.7 — No `date` — empty archive

The alliance has structured-flow enabled but has never Approved &
Posted a roster:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm — Recent Rosters                                     │
│                                                                      │
│ No structured rosters posted yet. Use `/desertstorm signups` to build a    │
│ roster + Approve & Post, and it'll show up here._                    │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral, no buttons)
```

The empty embed is sent without a View attached — there's nothing
to click, so no phantom timeout registration.

---

### Screen 15.8 — Recent-list date-button clicked (happy path)

Kevin clicks `[Mon May 18]` on the Screen 15.6 list. The bot
defers with `thinking=True`, fires `load_event_roster` +
`load_event_attendance` in parallel, joins them, and sends a new
ephemeral followup carrying the event-detail embed:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm Roster — Monday, May 18, 2026                        │
│                                                                      │
│ ── Team A ────────────────────────────────────────────────────────── │
│ __Power Tower__                                                      │
│ ✅ Alice — 412M                                                      │
│ __Field Hospital I__                                                 │
│ ✅ Bob — 380M                                                        │
│ __Nuclear Silo__                                                     │
│ ❌ Carol — 350M                                                      │
│ __Arsenal__                                                          │
│ ✅ Dan (sub, paired with Carol) — 320M                               │
│                                                                      │
│ ── Team B ────────────────────────────────────────────────────────── │
│ __Power Tower__                                                      │
│ ✅ Erin — 300M                                                       │
│ __Mercenary Factory__                                                │
│ — Frank — 285M                                                       │
│ __(sub pool)__                                                       │
│ — Gina (sub) — 260M                                                  │
│                                                                      │
│ ───────────────────────────────────────────────────────────────────  │
│ Attendance: ✅ 4  ·  ❌ 1  (recorded 5 of 7 slots)                   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

Notes:
- Title uses `format_event_date` (long form: `Monday, May 18, 2026`).
- Embed color is `dark_gold` for DS, `dark_orange` for CS.
- Rows group by team → zone; teams + zones sort alphabetically so
  the rendering is stable regardless of Sheet row order.
- Each slot shows the status glyph from the join:
  - `✅` = `attended`
  - `❌` = `no_show`
  - `—` = no attendance recorded yet (or unknown status)
- **Rule K (#171)**: 🔄 Sub activated is dropped from the UI.
  Legacy `sub_activated` rows still on the Sheet render as `—` and
  don't count toward `recorded` so the footer math matches what the
  officer sees.
- **Decision #6 (#171)**: the pre-existing `⚠️ override` flag is
  removed from history rendering. The rosters_tab `Override Below
  Floor` column still records the flag for post-event audit, but
  history consumers don't surface it.
- Power renders as `_format_power_display(slot.power)` — `"412000000"`
  becomes ` — 412M`. The sentinel `"unknown"` and blanks render as
  empty (the slot line collapses to just the name).
- Sub slots labelled. Paired-mode subs show `(sub, paired with
  <primary>)`; pool-mode subs show `(sub)`. Primary slots get no
  marker.
- Footer summarises attendance: counts of attended + no_show +
  "recorded N of M slots".

**Phase-aware variant (Rule L / #172):** when the rostered event
was built from a phase-aware preset, each zone breaks into a header
+ per-phase sub-rows. Detection fires when any primary slot carries
a non-empty Phase cell on the rosters_tab.

```
│ ── Team A ────────────────────────────────────────────────────────── │
│ __Power Tower__                                                      │
│    └ **Phase 1**                                                    │
│ ✅ Alice — 412M                                                      │
│ ✅ Bob — 380M                                                        │
│    └ **Phase 2**                                                    │
│ ✅ Carol — 350M                                                      │
│ ❌ Dan — 320M                                                        │
│    └ **Phase 3**                                                    │
│ ✅ Erin — 305M                                                       │
```

Sub-pool rows always render flat regardless of phase-awareness
(they're event-level, not phase-scoped).

The original list view (Screen 15.6) **stays alive** — the date
buttons remain clickable so Kevin can hop to other dates without
re-running the command. Each click sends a fresh ephemeral followup
with the new event's embed.

---

### Screen 15.9 — Date-button clicked — attendance not yet recorded

Kevin clicks a date for which `storm_attendance` was never run.
`load_event_attendance` returns an empty dict, every slot's glyph is
`—`, and the footer changes:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm Roster — Monday, May 11, 2026                        │
│                                                                      │
│ ── Team A ────────────────────────────────────────────────────────── │
│ __Power Tower__                                                      │
│ — Alice — 405M                                                       │
│ __Field Hospital I__                                                 │
│ — Bob — 375M                                                         │
│ __Nuclear Silo__                                                     │
│ — Carol — 348M                                                       │
│                                                                      │
│ ── Team B ────────────────────────────────────────────────────────── │
│ __Power Tower__                                                      │
│ — Erin — 298M                                                        │
│ __Mercenary Factory__                                                │
│ — Frank — 280M                                                       │
│                                                                      │
│ ───────────────────────────────────────────────────────────────────  │
│ Attendance not yet recorded. Run /desertstorm attendance to add it.        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 15.10 — Date-button clicked — no roster on that date

A date appeared in `list_event_dates` (so a row in `rosters_tab`
existed), but `load_event_roster` for that exact ISO returns zero
slots — e.g. all the rows for that date had blank Member cells, or
the date column had a stray space that didn't match. Defensive empty
state:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm Roster — Monday, May 4, 2026                         │
│                                                                      │
│ _No structured roster found for this date. Check the date format or  │
│ run /desertstorm signups + Approve & Post to build a roster for this       │
│ event._                                                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Footer omitted (no slots to count).

---

### Screen 15.11 — Date-button clicked by non-owner

Alice (another officer in the same alliance, also leadership)
somehow clicks `[Mon May 18]` on Kevin's history list view. Standard
owner guard:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who opened this view can switch dates.           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Alice sees it)
```

Kevin's list view stays clickable for him. Alice can run her own
`/desertstorm strategy roster_history` to get her own list.

---

### Screen 15.12 — List view timeout

Kevin's recent-events list view sits idle for 5 minutes. The view
times out, every date button greys out in place:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm — Recent Rosters                                     │
│                                                                      │
│ Click a date below to view the roster + attendance.                  │
└──────────────────────────────────────────────────────────────────────┘
[Mon May 18 (disabled)]  [Mon May 11 (disabled)]  [Mon May 4 (disabled)]
[Mon Apr 27 (disabled)]  [Mon Apr 20 (disabled)]
```

Re-run `/desertstorm strategy roster_history` to get a fresh clickable list.
Per-event detail ephemerals (Screen 15.8) that Kevin already opened
remain on screen — those have no buttons and don't time out.

---

### Screen 15.13 — `date` provided — direct happy path

Kevin runs `/desertstorm strategy roster_history date:2026-05-18`. The bot
defers, parses the date through `parse_event_date` (accepts ISO,
US slash, long-form, `today` / `tomorrow` / `yesterday`, weekday
names), normalises to ISO `2026-05-18`, and posts the same embed
as Screen 15.8 — no list view, just the detail straight away:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm Roster — Monday, May 18, 2026                        │
│                                                                      │
│ ── Team A ────────────────────────────────────────────────────────── │
│ __Power Tower__                                                      │
│ ✅ Alice — 412M                                                      │
│ __Field Hospital I__                                                 │
│ ✅ Bob — 380M                                                        │
│ … (rest of the roster) …                                             │
│                                                                      │
│ ───────────────────────────────────────────────────────────────────  │
│ Attendance: ✅ 4  ·  ❌ 1  (recorded 5 of 7 slots)                   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral, no buttons)
```

Other accepted inputs for the same Monday 5/18/2026 event:
`date:May 18`, `date:5/18`, `date:5-18-2026`, `date:May 18th`,
`date:18 May 2026`, `date:Monday`. The permissive parser is shared
with every other slash command that accepts a date.

---

### Screen 15.14 — `date` provided — unparseable

Kevin types `date:next tuseday` (typo on "Tuesday"):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ `next tuseday` isn't a date I can parse. Try `May 18`, `5/18`,    │
│ `2026-05-18`, or `yesterday`.                                        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 15.15 — `date` provided — date parses but no roster exists

Kevin types `date:May 4` for an event the alliance didn't run a
structured roster on:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm Roster — Monday, May 4, 2026                         │
│                                                                      │
│ _No structured roster found for this date. Check the date format or  │
│ run /desertstorm signups + Approve & Post to build a roster for this       │
│ event._                                                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Same empty-state copy as Screen 15.10. The date parsed cleanly — the
Sheet just has no rows for it.

---

### Screen 15.16 — `date` provided — soft Sheet read errors

A non-fatal Sheet I/O error occurred during the read (e.g. a
transient rate-limit on the second batch). The bot rendered what it
could but warns the officer to check logs:

```
⚠️ Read had soft errors — see bot logs.
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm Roster — Monday, May 18, 2026                        │
│                                                                      │
│ ── Team A ────────────────────────────────────────────────────────── │
│ __Power Tower__                                                      │
│ ✅ Alice — 412M                                                      │
│ … (partial roster as far as the read got) …                          │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

The "⚠️ Read had soft errors — see bot logs." line is prepended as
the followup's `content`; the embed body is still rendered. The
bot-side log line reads:
`[STORM HISTORY] roster read errors guild=… date=2026-05-18: <error 1>; <error 2>`

---

### Screen 15.17 — Large roster truncation

A 30+ slot roster blows Discord's 1024-char field limit on the
per-team field. The renderer trims the field at ~980 chars on the
nearest newline boundary and appends a marker:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm Roster — Monday, May 18, 2026                        │
│                                                                      │
│ ── Team A ────────────────────────────────────────────────────────── │
│ __Power Tower__                                                      │
│ ✅ Alice — 412M                                                      │
│ __Field Hospital I__                                                 │
│ ✅ Bob — 380M                                                        │
│ … (many rows) …                                                      │
│ ✅ Pete — 195M                                                       │
│ …trimmed; see Sheet for full list                                    │
│                                                                      │
│ ── Team B ────────────────────────────────────────────────────────── │
│ … (Team B field renders normally) …                                  │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 15.18 — Canyon Storm parity

The CS variant uses the same machinery with three differences:
- Title says "Canyon Storm Roster — …"
- Embed colour is `dark_orange` (vs DS `dark_gold`)
- Team grouping respects `cfg.teams` (Rule A / #166): CS with
  `teams=both` renders `── Team A ──` / `── Team B ──` headers
  exactly like DS; CS with `teams=A` or `teams=B` (single-team)
  renders a single `── Roster ──` header.

**CS, `teams=both`:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Canyon Storm Roster — Saturday, May 18, 2026                      │
│                                                                      │
│ ── Team A ────────────────────────────────────────────────────────── │
│ __Power Tower__                                                      │
│ ✅ Alice — 412M                                                      │
│ __Data Center 1__                                                    │
│ ✅ Bob — 380M                                                        │
│                                                                      │
│ ── Team B ────────────────────────────────────────────────────────── │
│ __Power Tower__                                                      │
│ ✅ Carol — 350M                                                      │
│ __(sub pool)__                                                       │
│ — Dan (sub) — 320M                                                   │
│                                                                      │
│ ───────────────────────────────────────────────────────────────────  │
│ Attendance: ✅ 3  ·  ❌ 0  (recorded 3 of 4 slots)                   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**CS, single team:**

```
│ ── Roster ─────────────────────────────────────────────────────────  │
│ __Power Tower__                                                      │
│ ✅ Alice — 412M                                                      │
│ …                                                                    │
```

---

### Flow at a glance

```
Officer runs /desertstorm strategy roster_history  (or /canyonstorm strategy roster_history)
  │
  ▼
storm_history.open_history(interaction, event_type, event_date)
  │
  ├── not leader/admin?      → 15.2 denial, end
  ├── no guild?              → 15.5, end
  ├── not Premium?           → 15.3, end
  ├── flow not enabled?      → 15.4, end
  │
  ▼  defer ephemeral, thinking
  │
  ├──  event_date provided?
  │      │
  │      ├── unparseable?  → 15.14 error, end
  │      │
  │      ▼  parse to ISO
  │      load_event_roster + load_event_attendance  (parallel)
  │      render_event_embed
  │      │
  │      ├── slots empty   → 15.10 / 15.15 empty-state embed
  │      ├── soft errors   → 15.16 (content prefix + embed)
  │      ├── attendance ∅  → 15.9 (— glyphs + "not yet recorded" footer)
  │      └── happy path    → 15.13 detail embed (one-shot ephemeral)
  │
  └──  event_date omitted
         │
         ▼  list_event_dates (top 8 desc)
         render_history_list_embed
         │
         ├── no dates → 15.7 empty-state embed (no view)
         │
         └── ≥1 date → 15.6 list embed + _HistoryListView
               │
               ├── owner clicks [Sun May 18] / [Sun May 11] / …
               │   → load_event_roster + load_event_attendance (parallel)
               │   → render_event_embed
               │   → 15.8 detail (new ephemeral followup; list stays alive)
               │     ├── happy path        → 15.8
               │     ├── attendance ∅      → 15.9
               │     └── empty roster      → 15.10
               │
               ├── non-owner clicks       → 15.11 denial
               │
               └── 5-min idle             → 15.12 buttons greyed in place
```

---

(End of Flows 13–15.)

**Notes:**
- Source files read: `lw-alliance-helper-bot/storm_member_rules.py` (943 lines), `lw-alliance-helper-bot/storm_walkthrough.py` (319 lines), `lw-alliance-helper-bot/storm_history.py` (534 lines), plus supporting `storm_strategy.py`, `storm_permissions.py`, `storm_date_helpers.py`, and `storm.py` for canonical zone lists and the `parse_power`/`format_power` helpers.
- All copy strings are quoted verbatim from source where they appear in `send_message` / `followup.send` / embed titles / button labels — only example placeholders (`Alice`, `Bob`, `300M`, `Power Tower`, `2026-05-18`, etc.) are substituted in.
- Total: 38 numbered screens across Flow 13, 17 across Flow 14, 18 across Flow 15, plus a flow-at-a-glance diagram each.

---

