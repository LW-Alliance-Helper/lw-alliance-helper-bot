# Premium Storm — UX Walkthrough (v2)

Screen-by-screen approximations of every user-facing surface in the
Premium Storm flow, refreshed against shipped code after the Storm
UX Overhaul (issues #143, #144, #156, #158, #159–#174, #178). This
is the post-overhaul snapshot — every screen here is what a real
officer or member sees in Discord today, not what the original
spec called for.

Placeholders are filled in with **realistic example values** so each
screen reads the way a real officer / member would experience it.
Example values used:

- Alliance: **Apex**
- Event date: **Saturday, May 18, 2026** (`2026-05-18`)
- Members: Alice (P 412M), Bob (P 380M), Carol (P 350M), Dan (P 320M)
- Officer (the person running the command): **Kevin**
- Sign-up channel: `#storm-signups`
- Roster Sheet power column: `1st Squad Power` (column F)

ASCII boxes approximate what the user sees in Discord (embed boundary
+ body + fields + footer). Buttons appear on the line below the box,
ordered as they render in the Discord client. Ephemeral messages are
labelled `(ephemeral — only the clicker sees it)`. DMs are labelled
`(DM — sent directly to the member)`.

> **Command-tree note.** As of issue [#143](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/143),
> every storm slash command (except the two top-level `/setup_*`
> wizards) lives under two parent groups: `/desertstorm <sub>` and
> `/canyonstorm <sub>`. The `event_type` argument that older commands
> took is encoded in the parent now — the DS and CS forms are
> separate commands. Where this doc shows a DS-form invocation, the
> CS form is identical under the other parent unless explicitly
> called out.

> **Zone-name note.** Across every surface where a zone name renders
> (sign-up post mail body, officer view, roster builder embed, preset
> editor, attendance, history, walkthrough tour), the zone name is
> prefixed with a tiny in-game-map icon via `zone_emoji_prefix()` (#158
> + #177). When the bot's Discord Application has uploaded the
> matching PNG, you'll see `<:nuclear_silo:111> Nuclear Silo`;
> otherwise the prefix is empty and the name renders plain. This doc
> shows the zone name **without** the icon prefix throughout, since
> the icon is a per-environment activation step that doesn't change
> the wording.

> **"Floor" → "Minimum" sweep.** Per Rule M / #160, every user-facing
> surface that previously said "Floor" or "Power Floor" now says
> "Minimum" or "Min Power." Storage column headers were renamed in
> the same sweep (`Override Below Floor` → `Override Below Minimum`).
> Only the dataclass field names (`min_power`, `floor_override`) keep
> the original internal vocabulary.

---

## Table of contents

1. Auto sign-up post + member voting + power-refresh DM
2. `/setup → ⚔️ Desert Storm` and `/setup → 🏜️ Canyon Storm` — structured-flow setup wizard
3. `/members sync` — alliance roster sync
4. `/desertstorm post_signup` + `/canyonstorm post_signup` — manual sign-up post fire
5. `/desertstorm signups` + `/canyonstorm signups` — officer view
6. On-behalf vote picker
7. Roster builder
8. Auto-fill summary
9. Approve & Post
10. `/desertstorm attendance` + `/canyonstorm attendance` — post-event attendance
11. `/desertstorm strategy` + `/canyonstorm strategy` — preset commands
12. Strategy preset editor (multi-step zone wizard)
13. `/desertstorm member_rule` + `/canyonstorm member_rule` — rule commands
14. Walkthrough tour
15. History browser

---

## 1. Auto sign-up post + member voting + power-refresh DM

The sign-up post is what members see in `#storm-signups` — the public
poll they click to register their availability for the next event.
Code lives in [storm_signup_post.py](../storm_signup_post.py)
(`post_registration` + `_build_registration_embed`) and
[storm_signup_view.py](../storm_signup_view.py)
(`SignupView` + `_handle_signup_click`).

The post is published when the storm scheduler fires (per the
alliance's auto-schedule config — see Section 2's poll-day setup), OR
when an officer runs `/desertstorm post_signup` / `/canyonstorm post_signup`
manually (Section 4).

### Screen 1.1 — The auto-posted sign-up message

**Variant A — Desert Storm, alliance runs both teams (`teams=both`, the default):**

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

Embed colour is gold for DS, orange for CS. Title format
`{emoji} {event_label} — Sign Up for {date_pretty}` where
`{date_pretty}` flows through `format_event_date()` and renders as
`Saturday, May 18, 2026` (no leading zero on the day, full weekday
name, full month name). The slot times live on the buttons only —
the embed body is the simpler description + vote-rules disclaimer.

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

Mirror for `teams=B`: `🅱️ Team B: 4pm ET (13:00 server time)` instead.
The `🔄 Either time works` button is omitted in either single-team
variant — there's nothing to be "either" between.

**Variant C — Canyon Storm:**

Per Rule A / #166 + the PR #183 follow-up, CS now mimics DS exactly
with its own game-defined times. `teams=both` renders the 4-button
shape; `teams=A` / `teams=B` collapses to the single-team layout.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🏜️ Canyon Storm — Sign Up for Saturday, May 18, 2026                 │
│                                                                      │
│ Select your availability for Canyon Storm!                           │
│ Only 1 vote can be recorded. If you select a 2nd one, it will        │
│ replace the first vote you cast.                                     │
│                                                                      │
└──────────────────────────────────────────────────────────────────────┘
[🅰️ Team A: 10am ET (12:00 server time)]  [🅱️ Team B: 9pm ET (23:00 server time)]
[🔄 Either time works]  [❌ Cannot participate]
```

Single-team CS collapses to the same shape as the DS single-team
variants (one team button + `❌ Cannot participate`).

---

### Screen 1.2 — Vote-recorded ack

After Alice clicks `🅰️ Team A: 9pm ET (18:00 server time)`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Vote recorded: Team A: 9pm ET (18:00 server time). You can      │
│ change your vote any time before the event.                          │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Alice sees it)
```

The token changes by vote:
- `Team A: <slot a>` / `Team B: <slot b>` — team votes include the slot label so the ack matches the button members clicked.
- `Either time works` / `Cannot participate` — no slot label.

---

### Screen 1.3 — Click-error ephemerals

Each is a single-screen ephemeral with no buttons. They appear when
something blocks the vote from recording.

**1.3a — Member clicks Team B (or Either) on a `teams=A` alliance's post:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ℹ️ Your alliance is configured as Team A only. Team B / Either       │
│ aren't valid choices — pick Team A or Cannot participate.            │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Mirror for `teams=B` alliance: "configured as **Team B only**. Team
A / Either aren't valid choices — pick **Team B**…"

This fires when a stale 4-button post is still live in the channel
but the alliance has since flipped `teams` to single-team via
`/setup_<event>`. New posts on a single-team alliance don't render
the wrong-team buttons at all — this guard is for the gap between
config change and the next post. Applies identically to DS and CS
per Rule A / #166.

> **Removed from _edited:** the pre-#166 "before Canyon Storm switched
> to a single-team format. Please vote on a current sign-up poll."
> screen. Kevin flagged it in his first sweep as "incorrect because
> it's not a single team"; that branch was always wrong and has been
> dropped from the doc.

**1.3b — Stale custom_id from an older bot version (defensive):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ This sign-up button is from an older version. Wait for the next   │
│ sign-up post to vote.                                                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**1.3c — Cross-server click (defensive — shouldn't normally fire):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ This sign-up post belongs to a different server. Please use the   │
│ sign-up post in your alliance's channel.                             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**1.3d — Alliance lost Premium / structured-flow since the post was created:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ This sign-up post is no longer active because the structured      │
│ roster flow has been disabled for this server.                       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

---

### Screen 1.4 — Power-refresh DM nudge

Bot DMs the voter directly when their power column on the alliance
roster Sheet is blank or unparseable. Cooldown is per-voter
per-event, claimed via an INSERT-first race-tight gate
(`storm_power_refresh_dms_sent`) so re-voting or a near-simultaneous
double-click can't fire two DMs.

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

The header is **looked up from the configured letter at DM time**
(`storm_roster_builder._read_power_column_header`): leadership picks
the column by letter on the setup side (Rule C / #165 — header text
can drift), but members see the current header label so they know
what to update on the sheet. If the header can't be resolved (sheet
not configured, column out of range, transient gspread failure) the
DM falls back to the generic "your power value" wording.

Failure handling:
- `discord.Forbidden` (member has DMs disabled): cooldown is kept,
  so the bot doesn't burn a Sheets read on every re-vote retrying
  the same blocked DM. The audit log captures it as info-level for
  alliance support.
- `discord.HTTPException` (transient 503/rate limit): the cooldown is
  **rolled back** so the next click retries. Without this, a flake
  on the first vote would permanently silence the nudge for that
  voter + event.

The DM only fires if:
1. `power_refresh_dm_enabled=1` in the alliance's structured-flow
   config (Set in Section 2's wizard step).
2. The voter's row on the roster Sheet has a blank or unparseable
   power value.
3. No cooldown row exists for this `(guild, event_type, event_date,
   voter_id)` tuple.

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
        ┌───────┴────────────────────┐
        ▼                            ▼
   Vote accepted (1.2 ack)    Vote rejected (1.3 ephemeral)
        │
        ├── If voter's power column is unreadable AND
        │   power-refresh DM is enabled AND no cooldown row:
        ▼
   Screen 1.4 DM (cooldown row claimed via INSERT-first)
```

---

## 2. `/setup → ⚔️ Desert Storm` + `/setup → 🏜️ Canyon Storm` — structured-flow setup wizard

These commands share the same wizard chassis (`run_storm_setup` in
[setup_cog.py](../setup_cog.py), branching on `event_type="DS"|"CS"`).
The screens below cover the **structured-flow sub-step** — the
Premium-and-structured block that runs as Step 7 of the wizard, after
the mail-template / log-channel / post-channel steps. Code lives in
`_run_structured_flow_setup_step` (around line 5917) plus the
`_ask_signup_schedule` helper (line 5474).

The wizard runs in **the channel where Kevin ran the command**. The
slash-command interaction itself just acks with a one-line ephemeral —
`⚙️ Starting Desert Storm setup — check the channel for prompts!` —
then every prompt lands as a regular channel message. The wizard times
out after 5 minutes of inactivity; every timeout posts the same
`⏰ Timed out. Run /<cmd>` line.

Every screen below is variant-pair: first-time entry (no saved value)
vs. re-entry (Keep current branch). Both paths are shown.

### Screen 2.0 — Slash-command ephemeral ack

When Kevin runs `/setup → ⚔️ Desert Storm`, Discord acks with an ephemeral
in the slash response. Every wizard prompt that follows is a regular
channel message in whatever channel he ran it in.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚙️ Starting Desert Storm setup — check the channel for prompts!     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

*(CS variant: `⚙️ Starting Canyon Storm setup — check the channel for prompts!`)*

Permission denial — non-leader, non-admin user:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to run                    │
│ `/setup → ⚔️ Desert Storm`.                                                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
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

For CS, the title reads `🏜️ Current Canyon Storm Setup`. Per Rule A /
#166, the `Teams` field is included for CS too (CS supports
`teams=both/A/B` like DS).

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
│ **Structured Roster Flow (💎 Premium)**                              │
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

After Kevin clicks, the buttons disable in place. (`YesNoView` doesn't
post an ack — Discord just shows the message with the buttons greyed
out.)

If Kevin picks **No**, the wizard skips to Screen 2.13. If **Yes**,
it descends through 2.4 → 2.12d.

**Variant B — Re-entry:** identical to Variant A. The opt-in question
always re-asks on every re-run; only sub-steps below it surface
Keep-current.

---

### Screen 2.4 — Power metric column (Rule C / #165)

The power column is now selected by **sheet column letter** (A–Z), not
header text. The bot reads the column at that letter on the roster
Sheet at render time, so renaming the Sheet header later doesn't
break anything. (Pre-#165 this was a free-text "type your column
header" step that broke whenever leadership re-titled the column.)

**Variant A — First-time (no saved column letter; default `B`):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ **Power Metric Column**                                              │
│ Which column on your roster Sheet stores the power value the bot     │
│ should use to gate Desert Storm zone eligibility? Enter a single     │
│ column letter (A–Z); the bot reads that column at render time, so    │
│ renaming the Sheet header later won't break anything.                │
└──────────────────────────────────────────────────────────────────────┘
[✅ Use default: B]  [✏️ Define my own]
```

**Variant B — Re-entry, saved value matches default `B`:** same as
Variant A.

**Variant C — Re-entry, saved value `F` (custom):**

```
[✅ Keep current: F]  [↩️ Use default: B]  [✏️ Define my own]
```

Click `✏️ Define my own` opens a modal:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Power Metric Column                                                  │
│                                                                      │
│ Column letter (A–Z)  [ F                                         ]   │
│                                                                      │
│                                              [ Cancel ]  [ Submit ]  │
└──────────────────────────────────────────────────────────────────────┘
(modal)
```

Validation: input is upper-cased and trimmed. If it's not a single
letter in A–Z (e.g. blank, `AA`, `1`, `Power`), the bot silently
coerces it to `B`. There's no error message — the next wizard prompt
is the only signal it accepted.

Ack:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Using F                                                           │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 2.5 — Sub mode

```
┌──────────────────────────────────────────────────────────────────────┐
│ Sub Mode                                                             │
│ How should subs be tracked when leadership builds a roster?          │
│ • Pool — flat list of subs; any sub can cover any primary no-show.   │
│ • Paired — each primary has a specific sub assigned in advance.      │
└──────────────────────────────────────────────────────────────────────┘
```

**Variant A — First-time (no saved sub_mode, defaults to "pool"):**

```
[Use Default: Pool]  [Paired — primary↔sub pairs]
```

(The current default is highlighted green, the other in blue.)

**Variant B — Re-entry with saved `pool`:** identical layout — Pool
shows as `Use Current: Pool` (green), Paired shows as `Paired —
primary↔sub pairs` (blue).

**Variant C — Re-entry with saved `paired`:**

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

Uses the shared `ChannelSelectStep` view that other wizard channel
picks use — buttons + native ChannelSelect with a Keep-current option.

**Variant A — First-time (no saved sign-up channel):**

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

**Variant C — Re-entry, the configured channel was deleted:**

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

### Screen 2.7 — Auto-schedule: poll day (Rule H / #164)

Per the poll-day model (#164), the **event day is game-defined** —
DS = Friday, CS = Thursday — so the wizard only asks when to post the
**poll**. The valid poll-day options are constrained per Rule H: only
days that sit between the previous event and the in-game roster lock.

- **DS:** Valid poll days are Sat, Sun, Mon, Tue, Wed (event = Fri; roster locks Wed).
- **CS:** Valid poll days are Fri, Sat, Sun, Mon (event = Thu; roster locks Mon).

```
┌──────────────────────────────────────────────────────────────────────┐
│ **Auto-Schedule — Poll Day (💎 Premium)**                            │
│ **Desert Storm** runs every **Friday** in-game. Which day do you     │
│ want the bot to post the sign-up poll? (The dropdown shows only      │
│ days that sit between the previous event and the in-game roster      │
│ lock.)                                                               │
└──────────────────────────────────────────────────────────────────────┘
[Dropdown — placeholder: "When should the bot post the sign-up poll?"]
  • Saturday
  • Sunday
  • Monday
  • Tuesday
  • Wednesday
  • Skip auto-scheduling (use /desertstorm post_signup manually)
```

**Variant A — First-time (`current_dow = -1`):** the dropdown opens
with `Skip auto-scheduling` pre-selected.

**Variant B — Re-entry, saved `Tuesday` (DOW=1):** Tuesday shows
pre-selected in the dropdown.

After Kevin picks `Tuesday`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Poll day: **Tuesday**.                                            │
└──────────────────────────────────────────────────────────────────────┘
[Dropdown] (disabled)
```

If he picks `Skip auto-scheduling…` instead:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Auto-scheduling skipped — `/desertstorm post_signup` will still   │
│ work manually.                                                       │
└──────────────────────────────────────────────────────────────────────┘
[Dropdown] (disabled)
```

(Skipping short-circuits the time-of-day prompt — the wizard jumps to
Screen 2.10 with `poll_day_of_week=-1`, `signup_time=""`.)

> **Removed: Lead-days screen.** The pre-#164 wizard had a separate
> "How many days before the event should the post fire?" question.
> That's gone — the poll day relative to the event day is now
> sufficient (the lead is implicit in the picked DOW). Storage
> columns `event_day`/`lead_days` were dropped via DROP COLUMN in
> the #164 migration.

---

### Screen 2.8 — Auto-schedule: sign-up post time (Rule F / #163)

When auto-scheduling is on, the time field is **required** (#163).
Empty submissions retry up to 3 times, after which the wizard falls
back to the default `12:00pm` to avoid looping forever.

```
┌──────────────────────────────────────────────────────────────────────┐
│ **Auto-Schedule — Sign-Up Post Time**                                │
│ What time should the bot fire the sign-up post? *(in your timezone:  │
│ (UTC-5) Eastern (New York, Toronto, Miami))*                         │
│ *(e.g. `2:00pm`, `9:00am`, or 24-hour `14:00`)*                      │
└──────────────────────────────────────────────────────────────────────┘
```

**Variant A — First-time (no saved time):**

```
[✅ Use default: 12:00pm]  [✏️ Define my own]
```

**Variant B — Re-entry, saved `14:00` (rendered as `2:00pm` in the keep-current label):**

```
[✅ Keep current: 2:00pm]  [↩️ Use default: 12:00pm]  [✏️ Define my own]
```

Click `✏️ Define my own` opens a modal:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Sign-Up Time                                                         │
│                                                                      │
│ e.g. 2:00pm           [ 2:00pm                                   ]   │
│                                                                      │
│                                              [ Cancel ]  [ Submit ]  │
└──────────────────────────────────────────────────────────────────────┘
(modal)
```

If the submission is blank, the wizard posts a one-line nudge before
re-prompting:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ A sign-up time is required when auto-scheduling is on. Pick a    │
│ time (e.g. `12:00pm`) or use the default.                            │
└──────────────────────────────────────────────────────────────────────┘
```

After valid submit:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Using 2:00pm                                                      │
└──────────────────────────────────────────────────────────────────────┘
```

Storage is 24-hour HH:MM (`14:00`) so the scheduler doesn't have to
disambiguate at fire time. Display flips back to 12-hour for the
Keep-current label.

---

### Screen 2.9 — Sign-Ups tab

Uses `ask_keep_or_change` with event-type-aware default
(`DS Signups` / `CS Signups`). Per Rule N / #162, **the bot
auto-creates the tab if it doesn't exist** — leadership doesn't have
to seed it manually.

**Variant A — First-time:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ **Sign-Ups Tab**                                                     │
│ Which Google Sheet tab should store Desert Storm sign-ups? The bot   │
│ creates and maintains this tab — leave the default if you don't      │
│ have a preference.                                                   │
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

### Screen 2.10 — Rosters tab

Same shape as Screen 2.9, with `Rosters` substituted throughout.

```
┌──────────────────────────────────────────────────────────────────────┐
│ **Rosters Tab**                                                      │
│ Which Google Sheet tab should store Desert Storm rosters? The bot    │
│ creates and maintains this tab — leave the default if you don't      │
│ have a preference.                                                   │
└──────────────────────────────────────────────────────────────────────┘
```

First-time: `[✅ Use default: DS Rosters]  [✏️ Define my own]`.
Re-entry: `[✅ Keep current: DS Rosters]  [✏️ Define my own]` (or
three-button when custom).

Modal title: `Rosters Tab Name`. Ack: `✅ Using DS Rosters`.

---

### Screen 2.11 — Attendance tab

Same shape again.

```
┌──────────────────────────────────────────────────────────────────────┐
│ **Attendance Tab**                                                   │
│ Which Google Sheet tab should store Desert Storm attendance? The     │
│ bot creates and maintains this tab — leave the default if you don't  │
│ have a preference.                                                   │
└──────────────────────────────────────────────────────────────────────┘
```

First-time: `[✅ Use default: DS Attendance]  [✏️ Define my own]`.
Re-entry: `[✅ Keep current: DS Attendance]  [✏️ Define my own]`.

Modal title: `Attendance Tab Name`. Ack: `✅ Using DS Attendance`.

> **Removed: Judicator role screen.** The pre-#167 CS wizard had a
> CS-only "Judicator Role" step after Attendance Tab that asked for a
> Discord role to auto-apply to CS Rulebringer candidates. Per Rule
> G / #167, the Judicator concept was dropped end-to-end (helper
> function removed, save/load fields dropped, member-rule subcommand
> removed). The CS wizard no longer fires this step.

---

### Screen 2.12 — Power-refresh DM (Premium)

Fires for both DS and CS as the last opted-in step. The wording now
surfaces the configured column letter (Rule C / #165) so members
reading the explanation know exactly which column the bot is checking.

**Variant A — First-time (no prior structured-flow enabled state),
shows YesNoView:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ **Power-Refresh DM (💎 Premium)**                                    │
│ When a member clicks a sign-up button for **Desert Storm** and       │
│ their power value (Column **F** on the roster Sheet) is blank or     │
│ unparseable, should the bot DM them a one-line nudge to update it?   │
│ At most one DM per member per event date.                            │
└──────────────────────────────────────────────────────────────────────┘
[Yes]  [No]
```

(The `Column F` substitution uses whatever Kevin set in Screen 2.4.
If 2.4 wasn't reached — e.g. the alliance opted out of structured
flow at Screen 2.3 — this screen doesn't fire either.)

**Variant B — Re-entry where the alliance had structured-flow
enabled previously, Keep-or-flip gate fires instead. Saved as ON:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ **Power-Refresh DM (💎 Premium)**                                    │
│ When a member clicks a sign-up button for **Desert Storm** and       │
│ their power value (Column **F** on the roster Sheet) is blank or     │
│ unparseable, the bot can DM them a one-line nudge to update it.      │
│ Currently **on** — keep it or flip.                                  │
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
│ ✅ Switching to No                                                   │
└──────────────────────────────────────────────────────────────────────┘
```

(Mirror for saved-off → flip-to-on.)

---

### Screen 2.13a — Strategy Presets explainer (Premium + opted-in only)

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
│ Optional power requirements                                          │
│ Priority                                                             │
│                                                                      │
│ When leadership builds a roster, they pick which                     │
│ preset to apply. The bot uses the preset to gate eligibility and     │
│ fill out the team.                                                   │
│                                                                      │
│ Manage presets with                                                  │
│ `/desertstorm strategy create / edit / list / apply`.                │
└──────────────────────────────────────────────────────────────────────┘
```

*(CS variant: `…for Canyon Storm` + `/canyonstorm strategy create /
edit / list / apply`.)*

This message is a plain `channel.send(...)` — no view, no buttons.
The wizard immediately follows with Screen 2.13.

---

### Screen 2.13 — Strategy Presets tab (Premium + opted-in only)

`ask_keep_or_change` with `default=default_structured_tab(event_type, "strategies_tab")`.

**Variant A — First-time:**

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

### Screen 2.13b — Inline-create-preset offer (zero-presets alliance, #144)

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

### Screen 2.14a — Member Rules explainer (Premium + opted-in only)

Mirrors 2.13a — a plain-text explainer before the tab prompt. Same
gating: only fires when the alliance is opted into the structured
flow. Per Rule A / #166 the wording is the same for DS and CS — both
events support `teams=both/A/B` and both support per-member team
rules.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Member Rules                                                         │
│ Member rules tell the roster builder how to treat individual         │
│ members.                                                             │
│                                                                      │
│ There are two types of Member rules.                                 │
│ • Power-band:                                                        │
│      Example: `members ≥ 250M are eligible for Power Tower`          │
│      Primary rule type that reads against the power column you       │
│      configured earlier.                                             │
│ • Per-member:                                                        │
│      Used for special cases, example: `Alice always plays on Team A`,│
│                                                                      │
│ Add rules later with                                                 │
│   `/desertstorm member_rule` : `set_power_band` /                    │
│   `set_member_team` / `set_member_zone`.                             │
└──────────────────────────────────────────────────────────────────────┘
```

*(CS variant: identical, with `/canyonstorm member_rule …`. Per Rule
A / #166 CS has the same `set_member_team` subcommand as DS.)*

> **Code vs. doc divergence — decision since first sweep.** Kevin's
> first-sweep _edited.md still listed `set_member_role` as a
> subcommand. Per Rule G / #167 the Judicator/Commander role-tagging
> concept was dropped end-to-end — `set_member_role` is gone from
> both DS and CS member-rule groups, and the explainer above no
> longer mentions it.

---

### Screen 2.14 — Member Rules tab (Premium + opted-in only)

Same shape as 2.13.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Member Rules Tab                                                     │
│ Which Google Sheet tab should store Desert Storm member rules?       │
│ The bot creates and maintains this tab.                              │
└──────────────────────────────────────────────────────────────────────┘
```

First-time: `[✅ Use default: DS Member Rules]  [✏️ Define my own]`.
Re-entry (match): `[✅ Keep current: DS Member Rules]  [✏️ Define my own]`.

Modal title: `Member Rules Tab Name`. Ack: `✅ Using DS Member Rules`.

---

### Screen 2.14b — Inline-create-member-rule offer (zero-rules alliance, #144)

After the Member Rules tab name is saved, the wizard checks
`storm_member_rules.list_rules(guild_id, "DS")`. If zero rules
exist, the wizard offers a streamlined inline path to add the
first one — but only for power-band rules (the common case).
Per-member rules need a Discord member picker which can't fit in a
modal, so the offer's prose redirects officers to slash commands
for those.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Want to add your first Desert Storm rule now? The button opens a     │
│ quick modal for a power-band rule (the most common type); per-member │
│ rules need a Discord member picker, so add those later via           │
│ `/desertstorm member_rule set_member_team` (or `set_member_zone`).   │
└──────────────────────────────────────────────────────────────────────┘
[✨ Add a power-band rule now]  [Skip for now]
```

*(CS variant: identical, with `/canyonstorm member_rule …`.)*

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

Per Rule E / #168: replaces the original two-field modal with a Zone
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

The Zone Select is sourced from `DS_ZONE_STRUCTURE` (11 zones) or
`CS_ZONE_STRUCTURE` (13 zones post-#178 dedup). Picking a zone enables
the **Set minimum power** button; until then it stays disabled.

**Picking a zone → click Set minimum power:**

The view's selection state is preserved in the dropdown placeholder
("Picked: Power Tower") and the **Set minimum power** button opens a
one-field modal:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Desert Storm Power-Band Rule — Power Tower                           │
│                                                                      │
│ Minimum power for Power Tower                                        │
│ [ e.g. 250M, 1.2B, 300,000,000                                   ]   │
│                                                                      │
│                                                  [Cancel]  [Submit]  │
└──────────────────────────────────────────────────────────────────────┘
```

**Submit — success:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: ≥ 250M → eligible for **Power Tower**.                     │
│ Add more rules later via `/desertstorm member_rule …`.               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**Submit — unparseable power value (e.g. typed `dunno`):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't parse `dunno` as a power value. Try `250M`, `1.2B`, or   │
│ `300,000,000` next time via                                          │
│ `/desertstorm member_rule set_power_band`.                           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Non-canonical and empty-zone error variants are unreachable now —
the Zone Select rejects them at pick time.

**Submit — save failed (Sheet write error):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ <save_rule's failure message — e.g. "couldn't open Sheet">       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**Cancel:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ↩️ Cancelled — no rule saved.                                        │
└──────────────────────────────────────────────────────────────────────┘
```

---

### Screen 2.15 — Inline post-first-signup offer (#144)

After the structured-flow setup save completes (the `[SETUP]` log
fires) AND the alliance opted into the structured flow AND a sign-up
channel was configured AND no sign-up post has been recorded for
this guild + event type yet, the wizard offers to fire the first
sign-up post inline. Whether auto-scheduling was configured or
skipped, this gives the alliance one fully-live sign-up post at the
end of setup.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📣 Want to post your first Desert Storm sign-up now? It'll land in   │
│ #storm-signups with vote buttons members can click. You can also     │
│ wait for the auto-schedule to post it (if you set one up) or run     │
│ `/desertstorm post_signup` later.                                    │
└──────────────────────────────────────────────────────────────────────┘
[📣 Post my first sign-up now]  [Skip]
```

Behaviour on `[Skip]`: both buttons disable in place, setup ends.
Behaviour on `[📣 Post my first sign-up now]`: fires the same
`post_registration` code path as `/desertstorm post_signup`, so the
success / error screens in Section 4 (4.8–4.15) apply here too.

If the alliance has any prior sign-up post recorded (re-entry case),
this offer is **not** posted — the wizard exits silently after the
save embed.

*(CS variant: `…Canyon Storm sign-up now? …run /canyonstorm
post_signup later.`)*

---

### Conditional matrix — what fires when

| Alliance state                          | Screens that fire                                              |
| --------------------------------------- | -------------------------------------------------------------- |
| Free tier                               | 2.0–2.2 → 2.13 → 2.14 → exit. (No 2.3 — Premium-only opt-in.)  |
| Premium, opts **No** at 2.3             | 2.0–2.3 → 2.13 → 2.14 → exit                                   |
| Premium, opts **Yes** at 2.3, first run | 2.0–2.12 → 2.13 → 2.13b → 2.14 → 2.14b → (2.14c) → 2.15 → exit |
| Premium, re-entry, structured on        | 2.0–2.12 (Keep-current variants) → 2.13/14 → exit (no 2.15)    |
| Premium, re-entry, opts to **disable**  | 2.0–2.3 → 2.13 → 2.14 → exit (config cleared except tabs)      |

CS gets the same matrix; no longer drops/adds the Judicator screen
(Rule G / #167).

### Cancel & timeout

- `/cancel` mid-wizard fires `wait_view_or_cancel`'s cancel branch on whatever step is currently active. The wizard posts: `⏹ Setup cancelled. Run /<cmd> to start again.`
- Step view timeout (5 minutes idle): `⏰ Timed out. Run /<cmd> to start again.`

### Flow at a glance

```
/setup → ⚔️ Desert Storm
    │
    ▼  Screen 2.0 ack + 2.1 summary (if re-entry)
    ▼
    ├── No changes needed → exit
    └── Edit settings
        │
        ▼  Steps 1–6: sheet tab → teams → log channel → post channel →
        │              mail templates → participation tracking
        │
        ▼  Screen 2.3 — Structured Roster Flow opt-in (Premium only)
        │
        ├── No → 2.13 → 2.14 → exit
        └── Yes
            │
            ▼  2.4 power column letter
            ▼  2.5 sub mode
            ▼  2.6 sign-up channel
            ▼  2.7 poll day (+ 2.8 sign-up time, if not skipped)
            ▼  2.9–2.11 Sign-Ups / Rosters / Attendance tabs
            ▼  2.12 Power-Refresh DM
            ▼  2.13 strategy presets tab
            ▼  2.13b inline-create preset (if zero presets)
            ▼  2.14 member rules tab
            ▼  2.14b inline-create member rule (if zero rules)
            ▼  2.15 post first sign-up
            ▼
        Done
```

---

## 3. `/members sync` — alliance roster sync

Lives in [member_roster.py](../member_roster.py). Premium-gated. Runs
`write_roster()`, which rebuilds the configured roster tab in the
alliance's Google Sheet and auto-maintains the
**`Is this user in Discord?`** column with Yes/No values + a Sheets
data-validation dropdown.

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

### Screen 3.2 — Premium upsell (free-tier guild)

Premium-gated. Free-tier alliances see the standard premium-locked
embed + upgrade view from `premium.premium_locked_embed` and
`premium.upgrade_view()` — same shape as every other Premium upsell.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 💎 Premium Feature: Member Roster Sync                               │
│                                                                      │
│ Member Roster Sync writes every member's Discord ID to your sheet    │
│ so other Premium features (birthday DMs, train DMs, auto-mention,    │
│ etc.) can find them. Run /upgrade to unlock it.                      │
└──────────────────────────────────────────────────────────────────────┘
[💎 Upgrade to Premium]  (etc.)
(ephemeral)
```

### Screen 3.3 — Not-yet-configured guard

Fires when the alliance is Premium but hasn't run `/setup → 👥 Members` yet.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚙️ Member Roster Sync isn't configured yet. Run /setup → 👥 Members      │
│ first.                                                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 3.4 — Sync running (thinking spinner)

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

Behind the scenes:
1. Forces a guild member-cache load (`guild.chunk()`).
2. Builds row data from the live member list.
3. Reads existing tab values so alliance-owned columns are preserved.
4. Auto-creates the `Is this user in Discord?` column at the right
   edge if it doesn't exist.
5. Fills the column with `Yes` / `No` per row based on live guild
   membership (non-bot members only).
6. Clears the tab and writes the merged rows back.
7. Applies a Yes/No data-validation rule on the column (strict;
   input hint: `Auto-filled by the LW Alliance Helper bot. Override
   to Yes/No if needed.`).

### Screen 3.5 — Sync success

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Synced 60 members to the Member Roster tab.                       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

The number and tab name reflect the live result — `{count}` rows
actually written (excluding the header), `{tab_name}` from the saved
config. (`Member Roster` is the default.)

### Screen 3.6 — Sync failure

If `write_roster()` raises (Sheets 403/404, network blip, permission
revoked):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Sync failed: <error message>                                      │
│ Make sure the bot has access to your sheet and that the              │
│ **Member Roster** tab can be written to.                             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

The error message is whatever `str(e)` returns from the gspread
exception. Examples:

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

### Side-effects on the Sheet (not surfaced in Discord)

After every sync, the tab structure looks like:

```
| A           | B     | C            | D          | E             | F                          |
| Discord ID  | Name  | Display Name | Joined     | Roles         | Is this user in Discord?   |
| 18293...    | alice | Alice        | 2024-11-02 | Member, Storm | Yes                        |
| 18294...    | bob   | Bob          | 2025-01-15 | Member        | Yes                        |
| 18295...    | (legacy row preserved)              |               | No                         |
```

Column **F** (`Is this user in Discord?`) is bot-maintained — manually
overridden values reset to the bot-derived value on the next sync.
Existing custom columns (power, `not_on_discord`, notes) are preserved
per Discord-ID match.

### Auto-sync side channel

In addition to manual `/members sync`, the cog re-syncs automatically
on `on_member_join`, `on_member_remove`, and `on_member_update` (only
if role membership changed). These don't post to Discord — they
update the Sheet silently. Errors go to Railway stdout + Sentry:

```
[ROSTER] Auto-sync failed for guild 1234567890: <exception repr>
```

### Server log breadcrumb (not user-facing)

If the alliance's `members` privileged intent is off (or the bot can't
chunk the guild), the count of returned members will be wildly under
the actual guild size. `/members sync` still completes with
`✅ Synced N members…` where `N` is the cached subset, but the bot
logs:

```
[ROSTER] Guild 1234567890: only 4/60 members in cache. Enable the
SERVER MEMBERS INTENT in the Discord Developer Portal (Bot →
Privileged Gateway Intents) — without it `guild.members` can't see
the full roster.
```

(Surfacing the partial-cache state to Discord is a known UX gap — the
ephemeral happily reports the partial count.)

---

## 4. `/desertstorm post_signup` + `/canyonstorm post_signup` — manual sign-up post fire

Lives in [storm_signup_post.py](../storm_signup_post.py). Premium +
structured-flow gated. Posts the same `SignupView` message that
Section 1 covers as auto-scheduled — except triggered manually. Takes
a single optional `event_date` argument.

### Screen 4.1 — Slash-command invocation

What Discord shows in the chat composer (DS form; CS form is
identical under `/canyonstorm post_signup`):

```
/desertstorm post_signup event_date: <optional — defaults to the next configured event day.
                                      Accepts e.g. May 18, 5/18, 2026-05-18, Sunday.>
```

`event_date` is a free-text optional argument.

### Screen 4.2 — Permission denial

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to run this command.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

(Shared denial copy from `storm_permissions.deny_non_leader` — same
line every structured-flow command uses.)

### Screen 4.3 — Unparseable date

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ `tomrrow` isn't a date I can parse. Try `May 18`, `5/18`,        │
│ `2026-05-18`, `Sunday`, or `tomorrow`.                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 4.4 — Past-date rejection

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Event date Saturday, May 9, 2026 is in the past. Sign-ups should │
│ be posted for upcoming events.                                       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

"Today" is computed in the alliance's configured timezone, not UTC.

### Screen 4.5 — Premium upsell (free-tier guild)

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔒 `/desertstorm post_signup` is a 💎 Premium feature. Run /upgrade  │
│ to unlock it.                                                        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 4.6 — Structured-flow not enabled

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The structured roster flow isn't enabled for Desert Storm. Run   │
│ /setup → ⚔️ Desert Storm and turn on Structured Roster Flow first.        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

*(CS variant: `…isn't enabled for Canyon Storm. Run /setup → 🏜️ Canyon Storm…`)*

### Screen 4.7 — Thinking spinner (defer)

After every gate passes, the bot defers ephemerally before calling
`post_registration`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ LW Alliance Helper is thinking…                                     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

The followup then replaces this with one of the result screens
(4.8–4.15).

### Screen 4.8 — Success

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Sign-up post for Desert Storm on **Saturday, May 18, 2026** is   │
│ live in #storm-signups. Members can vote any time before the event. │
│ Open `/desertstorm signups` to review who's voted.                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 4.9 — Already posted

```
┌──────────────────────────────────────────────────────────────────────┐
│ ℹ️ A sign-up post already exists for Saturday, May 18, 2026 (DS).   │
│ Check #storm-signups for the existing post — members can keep       │
│ voting on it. If you need to re-post, delete the prior message      │
│ first.                                                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

*(CS variant: parenthesis reads `(CS)`.)*

### Screens 4.10–4.15 — Error states

Each is a single-screen ephemeral. Maps 1:1 to a `status` value
returned by `post_registration`:

| Status                | Copy |
|-----------------------|------|
| `no_channel`          | `⚠️ No sign-up channel configured. Run /setup → ⚔️ Desert Storm and pick a sign-up channel during the structured-flow setup.` |
| `channel_gone`        | `⚠️ The configured sign-up channel (#storm-signups) no longer exists or the bot can't see it. Re-run /setup → ⚔️ Desert Storm to pick a new channel.` |
| `missing_slot_labels` (DS) | `⚠️ Both Desert Storm time slots need to be configured before posting a sign-up. Run /setup → ⚔️ Desert Storm and pick the two times first.` |
| `missing_slot_labels` (CS) | `⚠️ The Canyon Storm time slot needs to be configured before posting a sign-up. Run /setup → 🏜️ Canyon Storm and pick the time first.` |
| `forbidden`           | `⚠️ I don't have permission to send messages in #storm-signups. Check the channel permissions and try again.` |
| `send_failed`         | `⚠️ Discord refused the sign-up message: <error code+message, truncated to 120 chars>. See bot logs for details.` |
| (other)               | `⚠️ Sign-up post returned unexpected status \`<status>\`.` |

### Default date inference (post-#164 / Rule H)

If Kevin omits `event_date`, the bot calls `next_event_date(guild_id,
event_type, today=today_local)` from
[storm_date_helpers.py](../storm_date_helpers.py).

Per Rule H / #164, the event day is **game-defined**, not per-alliance:
- **DS:** next Friday on or after today
- **CS:** next Thursday on or after today

`guild_id` is accepted for signature stability but ignored — no longer
read from `event_day_of_week` config (which was dropped via DROP
COLUMN in the #164 migration).

### Flow at a glance

```
Kevin runs /desertstorm post_signup [event_date:2026-05-18]
        │
        ▼
   Has leadership role / admin?
        │
        ├── no ──→ 4.2 (⛔ denial)
        │
        ▼ yes
   parse_event_date(raw)
        │
        ├── unparseable ──→ 4.3 (⚠️ try `May 18`, …)
        │
        ▼ parsed
   parsed_date < today_local?
        │
        ├── yes ──→ 4.4 (⚠️ in the past)
        │
        ▼ no
   ensure_premium_structured(interaction, event_type)
        │
        ├── not premium ──→ 4.5 (🔒 upsell)
        │
        ├── premium but flow off ──→ 4.6 (⚠️ run setup_<event>)
        │
        ▼ ok
   4.7 (thinking…)
        │
        ▼
   post_registration(bot, guild, et, date) → status
        │
        ├── ok                ──→ post in #storm-signups + 4.8 (✅ live)
        ├── already_posted    ──→ 4.9 (ℹ️ exists)
        ├── no_channel        ──→ 4.10
        ├── channel_gone      ──→ 4.11
        ├── missing_slot_labels ─→ 4.12
        ├── forbidden         ──→ 4.13
        ├── send_failed       ──→ 4.14
        └── (other)           ──→ 4.15 (defensive)
```

---

## 5. `/desertstorm signups` + `/canyonstorm signups` — officer view

The slash command leadership types when they want to see who's voted
for an upcoming storm event. Lives in
[storm_officer_view.py](../storm_officer_view.py); gated by
`is_leader_or_admin` + `ensure_premium_structured`.

### Screen 5.1 — The slash command, as Discord renders it

```
/desertstorm signups
Leadership view of who's signed up for an upcoming Desert Storm event

  event_date      Optional — defaults to the next configured event
                  day. Accepts e.g. May 18, 5/18, Sunday.
```

`event_date` is free-text, parsed by `storm_date_helpers.parse_event_date`.
The CS form is identical under `/canyonstorm signups`.

### Screen 5.2 — Permission-denied (non-leader / non-admin)

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to run this command.      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 5.3 — Unparseable `event_date`

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ `nxt sat` isn't a date I can parse. Try `May 18`, `5/18`,         │
│ `2026-05-18`, `Sunday`, or `tomorrow`.                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 5.4 — Premium gate (free tier)

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔒 `/desertstorm signups` is a 💎 Premium feature. Run `/upgrade` to │
│ unlock it.                                                           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 5.5 — Structured-flow opt-in gate

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The structured roster flow isn't enabled for Desert Storm. Run    │
│ `/setup → ⚔️ Desert Storm` and turn on **Structured Roster Flow** first.   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

*(CS variant: `…isn't enabled for Canyon Storm. Run /setup → 🏜️ Canyon Storm…`)*

### Screen 5.6 — DM-only invocation defense

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ This command must be used inside a server.                        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 5.7 — The bucket-map embed (happy path, DS)

All gates passed. Bot deferred, pre-warmed the member cache, read the
roster Sheet via `_read_roster_rows`, joined with
`config.get_storm_signups`, and built the 5-bucket view via
`_build_bucket_map`. For Desert Storm with 5 sign-ups:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔥 Desert Storm Sign-Ups — Saturday, May 18, 2026  (5 members)       │
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
│ 🅰️ 2 · 🅱️ 1 · 🔄 1 · ❌ 0 · ❓ 1                                    │
└──────────────────────────────────────────────────────────────────────┘
[Filter bucket — currently: All ▾]
[🙋 Record on-behalf vote]  [🔄 Refresh]
[🅰️ Set up Team A]  [🅱️ Set up Team B]
```

Render notes:
- Title prefix: `🔥` for DS, `🏜️` for CS.
- Member counts in the title sum every bucket.
- Footer (`🅰️ 2 · 🅱️ 1 · 🔄 1 · ❌ 0 · ❓ 1`) shows per-bucket counts compactly.
- `Dan _(on behalf)_` markup is appended by `_format_bucket_names` when the row has `is_on_behalf=True` (italicised).
- `¹` superscript marks Erin as not on Discord (a roster row flagged `not_on_discord`).
- The `[1 not on Discord]` suffix on `Not voted yet` is appended only when the bucket has a not-on-Discord entry.
- Embed colour: `gold()` for DS, `orange()` for CS.

### Screen 5.8 — Bucket-map embed (Canyon Storm variant)

Same shape, different title prefix + colour. Per Rule A / #166, CS
gets the same `Set up Team A` / `Set up Team B` buttons as DS:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🏜️ Canyon Storm Sign-Ups — Thursday, May 21, 2026  (5 members)       │
│                                                                      │
│ … (same 5-bucket shape) …                                            │
│                                                                      │
│ 🅰️ 3 · 🅱️ 0 · 🔄 1 · ❌ 1 · ❓ 0                                    │
└──────────────────────────────────────────────────────────────────────┘
[Filter bucket — currently: All ▾]
[🙋 Record on-behalf vote]  [🔄 Refresh]
[🅰️ Set up Team A]  [🅱️ Set up Team B]
```

### Screen 5.9 — Single-team alliance (`teams=A` or `teams=B`)

Per Rule A / #166, applies to both DS and CS — the structured config's
`teams` field gates which Set-up button(s) render. `teams=A` shows
only `[🅰️ Set up Team A]`; `teams=B` shows only `[🅱️ Set up Team B]`;
`teams=both` (default) shows both.

### Screen 5.10 — Roster-Sheet read error preface

`_read_roster_rows` returned non-empty `roster_errors`. The bot
prepends a content line above the embed:

```
⚠️ Roster Sheet read had issues — non-Discord member enumeration may
be incomplete: roster-sheet read failed: APIError: 503 Service
Unavailable
┌──────────────────────────────────────────────────────────────────────┐
│ 🔥 Desert Storm Sign-Ups — Saturday, May 18, 2026  (3 members)       │
│ …                                                                    │
└──────────────────────────────────────────────────────────────────────┘
```

Up to 2 errors joined with ` · `. Common error strings:
- `roster-config read failed: <Python repr>`
- `roster-sheet open failed: <gspread error>`
- `roster-sheet read failed: <gspread error>`
- `stale Discord IDs on roster (member likely left the server): Alice (id 1234), Bob (id 5678)` (truncated with `(+N more)` past 5)

### Screen 5.11 — Filter dropdown opened

Kevin clicks `Filter bucket — currently: All ▾`:

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

Picking a bucket re-renders the embed with only that bucket. The
footer count line still shows all five so Kevin doesn't lose the
global picture.

### Screens 5.12–5.13 — Non-owner click guards

A second officer clicking the dropdown / refresh / on-behalf / set-up
buttons gets an ephemeral ⛔:

- Filter:    `⛔ Only the officer who opened this view can change the filter.`
- Refresh:   `⛔ Only the officer who opened this view can refresh.`
- On-behalf: `⛔ Only the officer who opened this view can record on-behalf votes here.`
- Set-up:    `⛔ Only the officer who opened this view can start team setup.`

### Screen 5.14 — Refresh button (happy path)

Kevin clicks `🔄 Refresh`. The bot defers, re-reads the roster Sheet
+ signups table, and edits the message in place. No new message; the
embed body and footer counts update silently.

### Screen 5.15 — Truncated buckets

A long member list pushed the description past `_DESCRIPTION_BUDGET`
(3800 chars). One or more buckets after the cap are dropped from the
default "All" view and the footnote at the bottom flags it:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔥 Desert Storm Sign-Ups — Saturday, May 18, 2026  (87 members)      │
│                                                                      │
│ **🅰️ Voted Team A** (42)                                             │
│ Alice, Bob, Carol, Dan, Erin, …, … (+12 more)                        │
│                                                                      │
│ **🅱️ Voted Team B** (38)                                             │
│ …                                                                    │
│                                                                      │
│ _Some buckets clipped — use the filter dropdown to see all votes._   │
│                                                                      │
│ 🅰️ 42 · 🅱️ 38 · 🔄 4 · ❌ 2 · ❓ 1                                  │
└──────────────────────────────────────────────────────────────────────┘
```

The `(+N more)` overflow hint is produced by `_format_bucket_names`
when an individual bucket exceeds `_BUCKET_BUDGET` (900 chars).

### Screen 5.16 — Empty event (no votes yet, no roster)

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔥 Desert Storm Sign-Ups — Saturday, May 18, 2026  (0 members)       │
│                                                                      │
│ **🅰️ Voted Team A** (0)                                              │
│ _(none)_                                                             │
│                                                                      │
│ … (every bucket renders _(none)_) …                                  │
│                                                                      │
│ 🅰️ 0 · 🅱️ 0 · 🔄 0 · ❌ 0 · ❓ 0                                    │
└──────────────────────────────────────────────────────────────────────┘
```

Buttons still render so the officer can fire on-behalf votes for
non-Discord members.

### Screen 5.17 — Set-up Team → preset picker

Kevin clicks `🅰️ Set up Team A`. The bot loads `ss.list_presets`. If
no presets exist:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No strategy presets defined yet for Desert Storm. Run             │
│ `/desertstorm strategy create` first.                                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

*(CS variant swaps `Desert Storm` → `Canyon Storm` and `/desertstorm`
→ `/canyonstorm`.)*

If presets exist, the `_PresetPickerView` ephemeral fires:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Select a strategy preset to apply for **Team A**:                    │
└──────────────────────────────────────────────────────────────────────┘
[Select a preset…                                                     ▾]
```

Dropdown contains up to 25 saved preset names.

### Screen 5.17b — Truncation notice (>25 presets)

If the alliance has more than 25 presets, the dropdown caps at 25 and
an inline `overflow_notice` content line warns about the truncation:

```
ℹ️ Showing the most recent 25 presets — your alliance has 31 saved.
Older presets aren't pickable here. (Filed against the alliance:
`/desertstorm strategy list` shows all of them.)
```

(`_PresetPickerView.overflow_notice` — added in the post-#180 audit-fix
sweep so officers aren't silently locked out of older presets.)

### Screen 5.18 — Preset picked

Kevin picks `Standard DS`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Preset **Standard DS** selected — opening the roster builder…    │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral; dropdown shown disabled)
```

Then `storm_roster_builder.open_roster_builder` takes over — that
flow is documented under Section 7.

### Screen 5.19 — Non-owner picks a preset

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the user who started team setup can pick.                    │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 5.20 — View timeout

15 minutes idle. `OfficerView.on_timeout` fires and
`wizard_registry.expire_view_message` strips buttons + appends the
timeout suffix:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔥 Desert Storm Sign-Ups — Saturday, May 18, 2026  (5 members)       │
│ … (embed body unchanged) …                                           │
└──────────────────────────────────────────────────────────────────────┘
⏰ This view timed out — run `/desertstorm signups` again to refresh it.
(buttons stripped)
```

### Screen 5.21 — First-run walkthrough offer (#170)

If Kevin hasn't dismissed the `storm_signups` walkthrough yet, a
second ephemeral fires from `maybe_offer_storm_signups_tour`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👋 First time using `/desertstorm signups`? Want a quick walkthrough │
│ of what each piece does?                                             │
└──────────────────────────────────────────────────────────────────────┘
[👋 Walk me through this]  [No thanks]  [Ask again next time]
(ephemeral)
```

*(CS variant: `/canyonstorm signups` per #170 — the tour branches on
event_type.)*

Click **No thanks** → dismissed forever:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👍 Got it, won't ask again. Run `/help` any time and pick Desert     │
│ Storm or Canyon Storm if you want a refresher.                       │
└──────────────────────────────────────────────────────────────────────┘
```

Click **Ask again next time** → dismissed for this session only:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👍 Got it, will ask you again next time you run `/desertstorm        │
│ signups`.                                                            │
└──────────────────────────────────────────────────────────────────────┘
```

Click **Walk me through this**:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Starting the tour…                                                │
└──────────────────────────────────────────────────────────────────────┘
```

Tour steps then fire sequentially as separate ephemerals (Section 14).

### Flow at a glance

```
Kevin types  /desertstorm signups  [event_date:May 18]
                     │
       ┌─────────────┼────────────────────┐
       ▼             ▼                    ▼
   5.2 (⛔)      5.3 (⚠️ bad date)    5.4/5.5 (premium / flow)
       │             │                    │
       └─────────────┴────────────────────┘
                     │  (all gates passed)
                     ▼
       Bot defers · pre-warms member cache · reads
       roster Sheet · joins signups · builds buckets
                     │
                     ▼
       Screen 5.7 (DS) / 5.8 (CS) — bucket-map embed
        + button row (Refresh, On-behalf, Set up A/B per teams config)
                     │
                     ├──→ 5.10 — roster errors prefix
                     ├──→ 5.15 — clipped buckets
                     ├──→ 5.21 — first-run tour offer
                     │
        ┌────────────┼────────────────────┐
        ▼            ▼                    ▼
   🔄 Refresh    🙋 Record on-behalf    🅰️/🅱️ Set up Team
   re-reads     → Section 6 picker      → 5.17 preset picker
   in place                              → Section 7 builder
        │
        └─→  5.20 view timeout after 15 min
```

---

## 6. On-behalf vote picker

Triggered when Kevin clicks `🙋 Record on-behalf vote` on the officer
view. Per Rule E / #168 + Decision #2, the pre-existing free-text
modal (`_OnBehalfModal`) was replaced by an ephemeral view-based
picker — Member Select sourced from the roster Sheet, Vote Select
that mirrors the sign-up buttons, Submit + Cancel + paging. Defined
in `storm_officer_view._OnBehalfVoteView`.

The clicker hits the button. The bot defers, reads the roster off
the event loop, and sends the picker as a followup ephemeral.

### Screen 6.1 — The on-behalf picker

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🙋 Pick a member and a vote, then **Submit**. Only roster members   │
│ are listed — `/members sync` refreshes the list.                     │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Pick a member…                                                  ]
[ ▾ Pick a vote…                                                    ]
[✅ Submit (disabled)]  [↩️ Cancel]
(ephemeral)
```

The **Member Select** is sourced from the roster Sheet via
`_read_roster_rows`, de-duped case-insensitively, sorted, and
filtered of purely-numeric names (collision risk with
`storm_signups.target_member_id`).

The **Vote Select** is built from `get_storm_slot_labels(event_type,
guild_id)` so its options match the wording members see on the
sign-up post. Options branch on `cfg.teams` (per Rule A / #166 — applies
to both DS and CS):

- `teams=both` → `Team A: <time>`, `Team B: <time>`, `Either time works`, `Cannot participate`.
- `teams=A` → `Team A: <time>`, `Cannot participate`.
- `teams=B` → `Team B: <time>`, `Cannot participate`.

**Submit** stays disabled until both selects have a value.

### Screen 6.1a — Paging (roster > 25 members)

```
[ ▾ Pick a member… (page 1)                                         ]
[ ▾ Pick a vote…                                                    ]
[◀ Prev (disabled)]  [Page 1 / 3 (disabled)]  [Next ▶]
[✅ Submit (disabled)]  [↩️ Cancel]
```

Prev / Next swap the Member Select's options in place. The Page X/Y
label-only button is disabled — it's a status indicator.

### Screen 6.2 — Submit success

Kevin picks **Erin** + **Cannot participate**, hits Submit. The bot
defers, calls `config.record_storm_vote(..., is_on_behalf=True)`,
re-reads buckets, edits the parent officer-view embed in place, then
fires the success ephemeral:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Recorded on-behalf vote for **Erin**.                             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral; picker disables its buttons)
```

The parent `/desertstorm signups` embed updates: Erin moves from
`❓ Not voted yet` into `❌ Voted Cannot` and now reads `Erin ¹
_(on behalf)_`.

### Screen 6.3 — Vote-write failure

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't record that vote. Check the bot logs.                    │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
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
read, so on failure it surfaces an actionable error and bails:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't read the roster right now. Try `/members sync` and       │
│ reopen this view to retry.                                           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral; no picker renders)
```

The roster-error prefix is also visible on the `/desertstorm signups`
embed (Screen 5.10) so the officer has context.

### Flow at a glance

```
Kevin clicks  🙋 Record on-behalf vote  (from 5.7 / 5.8 embed)
                       │
                       ▼   (defer + threaded gspread read)
            ┌────────────────────────┐
            │ roster_rows empty?     │
            └──────────┬─────────────┘
                       │
              yes ─────┘─── no
               │              │
               ▼              ▼
        Screen 6.5     Screen 6.1 picker
        Read failed    Member + Vote + Paging
                                   │
                              clicks Submit
                                   │
                                   ▼  (defer + record_storm_vote)
                       ┌───────────┴───────────┐
                       ▼                       ▼
                   Write OK              Write failed
                   refresh parent        Screen 6.3
                   embed → 6.2 success
                   ephemeral
```

---

## 7. Roster builder

Opened by either the Set-up-Team buttons on `/desertstorm signups`
(Section 5) or by the free-tier `/desertstorm strategy apply` /
`/canyonstorm strategy apply` slash commands. Lives in
[storm_roster_builder.py](../storm_roster_builder.py)
(`RosterBuilderView` + friends).

Two modes branched on `event_date`:
- **Structured mode** (Premium, called from Section 5): pool filtered
  to the team's sign-ups; `Approve & Post` button available; rosters_tab
  Sheet write fires on approve.
- **Free-tier apply mode** (no event date): full alliance roster as
  the pool; no `Approve & Post`; officer copies the mail manually.

Plus a **phase-aware variant** (preset.phase_count ≥ 2) with phase-nav
buttons and per-phase capacity readouts (Rule L / #172), and a
**paired-sub variant** (sub_mode="paired") that surfaces a
`🔁 Pair subs` button instead of auto-firing a sub picker after each
primary (Decision #3 / #168). Both can compose.

### Screen 7.1 — DS apply: team picker

Free-tier `/desertstorm strategy apply name:Standard DS` with no
`team_override`. The builder asks first:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Build roster for **Team A** or **Team B** with preset                │
│ **Standard DS**?                                                     │
└──────────────────────────────────────────────────────────────────────┘
[🅰️ Team A]  [🅱️ Team B]
(ephemeral)
```

Click ack: `✅ Team A selected.` / `✅ Team B selected.` (buttons
disabled).

Non-owner click: `⛔ Only the officer who started the apply can pick.`

Timeout (2 min idle): buttons silently disable; re-run the slash
command to retry.

For CS, no team picker when `cfg.teams="both"` and `team_override`
isn't supplied — the builder asks the same A/B question per Rule A /
#166 (CS now follows the DS pattern). Single-team CS alliances skip
straight to the builder embed.

When `team_override` is supplied (the structured-mode path from
`🅰️ Set up Team A`), this screen is skipped.

### Screen 7.2 — Preset-not-found

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No preset named **foo**.                                          │
└──────────────────────────────────────────────────────────────────────┘
[List saved strategies]
(ephemeral)
```

The `List saved strategies` inline button (post-#169 audit-fix sweep)
opens the same view as `/desertstorm strategy list`. Pre-#169, this
ephemeral had no button — the officer had to type the slash command
themselves.

### Screen 7.3 — Preset has no zones

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Preset **Standard DS** has no zones yet. Edit it first to add    │
│ zones before applying.                                               │
└──────────────────────────────────────────────────────────────────────┘
[Edit Standard DS]
(ephemeral)
```

The `Edit <preset>` button opens the preset editor directly (Section
12). Post-#169 inline action button — pre-#169 the officer had to
remember to run `/desertstorm strategy edit name:Standard DS`.

### Screen 7.4 — DM-only invocation defense

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ This command must be used inside a server.                        │
└──────────────────────────────────────────────────────────────────────┘
```

### Screen 7.5 — Premium + structured-flow gates

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔒 The structured roster builder is a 💎 Premium feature. Run        │
│ `/upgrade` to unlock it.                                             │
└──────────────────────────────────────────────────────────────────────┘
```

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The structured roster flow isn't enabled for Desert Storm. Run    │
│ `/setup → ⚔️ Desert Storm` and turn on **Structured Roster Flow** first.   │
└──────────────────────────────────────────────────────────────────────┘
```

### Screen 7.6 — Structured-mode pool empty

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No signed-up members match team **A** for event **Saturday, May  │
│ 18, 2026**. Check `/desertstorm signups` to see who's voted, or run │
│ the apply flow without an event date to use the full roster.        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 7.7 — Session already locked

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Another officer (@OtherOfficer) is already building **Team A**   │
│ for event **Saturday, May 18, 2026**. Wait for them to finish, or   │
│ coordinate before re-opening.                                        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral; mention suppressed via AllowedMentions.none())
```

The mention renders as a clickable @username (not a ping).

### Screen 7.8 — The builder embed (flat, pool mode, structured)

Apex has a flat `Standard DS` preset with six zones, sub_mode=pool,
Team A. No auto-fill yet:

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
│ ⬜ **Mercenary Factory** (0/2): (empty)                              │
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

Per Rule M / #160, every user-facing label says **Minimum** /
**Min Power**, not "Floor" — both in the embed body
("Enforcing **Min A** minimum…", "minimum 300M") and the button
labels ("Show members below minimum", "Hide members below minimum").

Render notes:
- Title for CS: `🛡️ Roster Builder: <preset name>` (no team suffix when `teams=both` and preset's `faction="Either"`; otherwise `— Team A` / `— Team B` / `— Rulebringers` / `— Dawnbreakers`).
- Per-zone status glyph: `⬜` empty, `🟡` partial, `✅` full, `—` for zero-capacity zones in the selected phase.
- `(0/4)` capacity readout. The selected zone gets a trailing `←`.
- Active-zone line below shows the minimum (or `(none)` when `min_power=0`). When a `power_band` rule has relaxed it: `🎯 **Active zone:** **Power Tower** — minimum **180M** _(preset minimum 300M relaxed by power_band rule)_`.
- Optional unknown-power hint: `_Members with no parseable power read as 'power unknown'; toggle the override to assign them anyway._` (only when any roster row has `power=None`).

Free-tier action row (mode-dependent): `[📄 Generate mail]
[💾 Save as preset] [🖼️ Generate DS assignments image] [✅ Done]`.

### Screen 7.9 — Builder embed after assignments

Kevin picked Power Tower → Alice, Bob. Then Info Center → Carol.

```
┌──────────────────────────────────────────────────────────────────────┐
│ … (header lines same as 7.8) …                                       │
│                                                                      │
│ **📋 Zones**                                                         │
│ 🟡 **Power Tower** (2/4): Alice, Bob                                 │
│ ⬜ **Nuclear Silo** (0/4): (empty)                                   │
│ 🟡 **Info Center** (1/4) ←: Carol                                    │
│ ⬜ **Field Hospital** (0/4): (empty)                                 │
│ ⬜ **Mercenary Factory** (0/2): (empty)                              │
│ ⬜ **Arsenal** (0/2): (empty)                                        │
│                                                                      │
│ 🪑 **Subs**: _(none)_                                                │
│                                                                      │
│ 📊 **Filled:** 3 / 20                                                │
│ 🎯 **Active zone:** **Info Center** — minimum **200M**               │
└──────────────────────────────────────────────────────────────────────┘
```

`←` tracks `selected_zone`. Status: `⬜` → `🟡` once at least one
member is assigned but the zone isn't full.

### Screen 7.10 — Member picker dropdown

Eligible-only (`👁️ Show members below minimum` is OFF):

```
[Pick a member for Info Center…                                  ▾]
  Alice (412M)
  Bob (380M)
  Carol (350M)
  Dan (320M)
  Erin (300M)
```

With toggle ON, below-minimum members appended with annotation:

```
[Pick a member for Info Center…                                  ▾]
  Alice (412M)
  Bob (380M)
  …
  Hank (90M)              below minimum
  Ivan (power unknown) ¹  below minimum
```

Zero eligible members + toggle OFF:

```
[No eligible members — toggle below-minimum override            ▾]
```

> 25 candidates qualify:

```
[Pick a member for Info Center… (+4 more)                       ▾]
```

Below-minimum race-case (option shouldn't be in pool but somehow is):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Toggle the below-minimum override to assign this member.          │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

No zone selected:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Pick a zone first.                                                │
└──────────────────────────────────────────────────────────────────────┘
```

### Screen 7.11 — Zone-full guard

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ **Power Tower** is already full (4 members). Unassign someone     │
│ before adding another.                                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 7.12 — Below-minimum toggle button states

- Default (off):  `[👁️ Show members below minimum]`
- After click:    `[👁️ Hide members below minimum]`

When on, an extra line surfaces in the embed below the Active zone:

```
👁️ Members below minimum visible in the picker.
```

Plus the picker now includes below-minimum + power-unknown rows.
Toggle resets to off when the officer switches zones (a per-zone
override doesn't accidentally carry over).

### Screen 7.13 — Unassign-zone

`↩️ Remove current zone assignees` clears the selected zone's
assignment for the current phase. Embed re-renders with `⬜ (empty)`.
Paired-sub pairings whose primaries are gone get pruned. No ephemeral.

No zone selected: `⚠️ Pick a zone first.`

### Screen 7.14 — Last-to-subs

`🪑 Add all unassigned to Subs` moves the last-added member of the
selected zone to the flat sub pool. Embed updates:

```
🟡 **Power Tower** (1/4): Alice
…
🪑 **Subs (1)**: Bob
```

No members in zone: `⚠️ No members in this zone to move.`

### Screen 7.15 — Auto-fill (structured mode only)

`🎯 Auto-fill` runs `_auto_fill_session`, which resets every phase's
assignments + subs + pairings before filling. Embed re-renders with
the auto-fill summary block appended (documented in Section 8).

Unexpected exception:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Auto-fill hit an unexpected error: `ValueError: …`. Please share  │
│ this message with the bot maintainer; logs have details.             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 7.16 — Preview / Generate mail

`📄 Preview mail` (structured) / `📄 Generate mail` (free-tier) —
identical content from `_send_mail_preview`:

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
│ … (one block per zone, then Subs block)                              │
│                                                                      │
│ **Time:** 4pm EDT (18:00 server time)                                │
│ ```                                                                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

The body inside the backticks is `_build_mail_body`'s output,
truncated at 1880 chars with `\n…(truncated)` appended when over the
1900-char budget. Phase-aware presets are split into per-phase blocks
joined by blank lines:

```
**Phase 1**

**Desert Storm**
…

**Phase 2**

**Desert Storm**
…
```

CS uses `**Canyon Storm**` by the same builder.

### Screen 7.17 — Render image

`🖼️ Generate DS assignments image` defers ephemerally, then calls
`storm_renderer.render` on a thread executor.

Happy path:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🖼️ DS assignments image attached:                                    │
│ [ds-roster-2026-05-18-team-A.png] (attachment)                       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Filename: `{event_type_lower}-roster[-{event_date}][-team-{team}].png`.

Per #140 + #181, the renderer uses the actual game-map background
art for DS / CS with zone-icon overlays for the assigned slots —
Arsenal + Mercenary Factory icons now ship for DS (PR #181 added the
two parked PNGs).

Pillow missing:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Image render isn't available — the host is missing Pillow. Use    │
│ the text-template mail in the meantime.                              │
└──────────────────────────────────────────────────────────────────────┘
```

Generic Pillow error: `⚠️ Couldn't render the roster image — see bot logs.`

> 25 MB output: `⚠️ Rendered roster image is too large to attach
(27 MB > 25 MB Discord limit). Use the text-template mail instead.`

### Screen 7.18 — Save as preset modal (free-tier only)

Click `💾 Save as preset` opens a modal with pre-filled current name:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Save as preset                                                 [X]   │
│                                                                      │
│ Preset name (overwrites if exists)                                   │
│ [ Standard DS                                                    ]   │
│                                                                      │
│                                          [Cancel]    [Submit]        │
└──────────────────────────────────────────────────────────────────────┘
```

Blank submit: `⚠️ Preset name is required.`

Save success: `✅ Saved roster as preset **Standard DS**.`

Save failure: `⚠️ Couldn't save preset — check that your Sheet is
configured and the bot has edit access.`

The saved preset preserves phase-shape — if the source session was a
phase-aware preset, the new one is too.

### Screen 7.19 — Approve & Post (structured mode)

`✅ Approve & Post` defers, re-reads roster powers (so
`power_at_assignment` reflects approval time, not session open),
builds the mail, posts to the configured channel, and writes one
row per slot to the rosters_tab.

Public ack (edits the original builder message; embed unchanged,
buttons disabled):

```
✅ Structured roster approved and posted.
```

Ephemeral detail — happy path:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster posted.                                                    │
│ 📬 Mail sent to #storm-signups.                                      │
└──────────────────────────────────────────────────────────────────────┘
```

No post channel: `✅ Roster recorded. ⚠️ No post channel is
configured — mail was built but not sent. Run /setup → ⚔️ Desert Storm to
pick one, or copy the mail manually below.`

Channel deleted: `✅ Roster recorded. ⚠️ The configured post channel
(<#…>) is deleted or the bot can't see it. Re-run setup to pick a
new channel — mail preview below.`

Channel exists but Discord rejected the send: `✅ Roster recorded.
⚠️ The configured post channel <#…> rejected the send: \`Forbidden
403 Missing Permissions\`. Check the bot's permissions in that
channel — mail preview below.`

Sheet-write soft error appended below any of the above:
`⚠️ rosters tab append failed: APIError: 503 Service Unavailable`

> **Removed: Screen 7.20 — Faction-roles offer (CS Judicator apply).**
> Pre-#167 a successful CS approve fired a `⚔️ Apply Faction Roles?`
> ephemeral asking which faction won and (if Rulebringers) applying
> the configured Judicator role to candidates. Per Rule G / #167 the
> Judicator concept was dropped end-to-end — `judicator_role_id`
> column dropped, helper functions removed, faction-roles UI gone.

### Screen 7.20 — Cancel / Done

Structured (`❌ Cancel`):

```
Roster builder cancelled — nothing posted.
┌──────────────────────────────────────────────────────────────────────┐
│ … (embed unchanged) …                                                │
└──────────────────────────────────────────────────────────────────────┘
(buttons disabled)
```

Free-tier (`✅ Done`): same shape, content `Roster builder closed.`

The session lock (structured only) releases automatically. The
free-tier variant has no lock.

Non-owner click on any button: `⛔ Only the officer who opened this
builder can use it.`

### Screen 7.21 — Builder timeout

15 min idle. `RosterBuilderView.on_timeout` disables every child,
edits the message (view-only edit), and releases the structured
session lock. No new content line — just inert buttons:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: Standard DS — Team A                              │
│ … (unchanged) …                                                      │
└──────────────────────────────────────────────────────────────────────┘
(buttons all disabled — re-open via /desertstorm signups → 🅰️ Set up Team A)
```

### Screen 7.22 — Phase-aware variant: builder embed (Rule L / #172)

Kevin opened the builder against the `CS Standard` 3-phase preset.
The embed gains a phase-nav row and the zone capacity readouts use
the **header + per-phase indented** format per Rule L:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: CS Standard                                       │
│                                                                      │
│ 🗺️ Canyon Storm                                                      │
│ 🔀 Editing **Phase 1** _(use the Phase buttons below to switch)_     │
│                                                                      │
│ **📋 Zones**                                                         │
│ ⬜ **Power Tower** — minimum **300M** ←                              │
│    └ Phase 1: 0/4                                                    │
│    └ Phase 2: 0/4                                                    │
│    └ Phase 3: 0/4                                                    │
│ ⬜ **Data Center 1** — minimum **250M**                              │
│    └ Phase 1: 0/4                                                    │
│    └ Phase 2: 0/0                                                    │
│    └ Phase 3: 0/4                                                    │
│ … (one block per zone) …                                             │
│                                                                      │
│ 🪑 **Subs**: _(none)_                                                │
│                                                                      │
│ 📊 **Filled:** 0 / 60                                                │
│ 🎯 **Active zone:** **Power Tower** — minimum **300M**               │
└──────────────────────────────────────────────────────────────────────┘
[Phase 1 •]  [Phase 2]  [Phase 3]
[Pick a zone to edit…                                                ▾]
[P1: Pick a member for Power Tower…                                  ▾]
[👁️ Show members below minimum]  [↩️ Remove current zone assignees]
[🪑 Add all unassigned to Subs]  [🎯 Auto-fill]
[✅ Approve & Post]  [📄 Preview mail]  [🖼️ Render image]  [❌ Cancel]
```

Differences vs flat:
- Title omits `— Team X` for CS unless preset's faction is set.
- `🔀 Editing **Phase 1**` body line (phase-aware only).
- Per-zone format: header line with the zone name + minimum, then one indented `└ Phase N: filled/cap` row per phase. Pre-#172 the format was inline `(P1: 0/4, P2: 0/4, P3: 0/4)` — that's been replaced because the indented form scales past 2 phases without wrapping.
- Zones with zero capacity in the selected phase show `—` glyph (not `⬜`) at the header.
- `📊 Filled` sums every phase's max for every zone.
- Member-picker placeholder gets a `P{n}:` prefix.

Phase-nav buttons: active phase has `•` suffix and
`ButtonStyle.primary`; inactive phases use `ButtonStyle.secondary`.
Switching to Phase 2 redraws everything with `Editing **Phase 2**`
and Phase 2 counts as the live readout.

### Screen 7.23 — Paired-mode variant: builder embed

sub_mode="paired". Header includes paired-mode hint; zones render
with inline sub annotations:

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
│ attach a sub to any of them. Subs may not cover every primary —      │
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

Per Decision #3 / #168: the auto-fire-after-each-primary sub picker
was retired. Officers explicitly click `🔁 Pair subs` when they're
ready, which keeps the workflow under their control and matches the
ratio reality (more primaries than subs is normal). The
"complete for every primary" success line was also dropped — silence
means everything is paired.

### Screen 7.24 — Pair-subs view (opened by 🔁 Pair subs)

Full pairing workflow on one ephemeral surface:

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
(ephemeral)
```

- **Primary Select**: currently-assigned primaries in the selected phase, not yet paired. Capped at 25.
- **Sub Select**: members in `session.subs` minus already-paired subs. Capped at 25.
- **Assign pair**: writes pairing, captures below-minimum override if applicable, clears both selects, re-renders both surfaces.
- **🔄 Unpair…**: enabled only when at least one pair exists. Switches to unpair mode (Screen 7.25).
- **✔ Done**: closes the ephemeral; main view is already in sync.

### Screen 7.25 — Unpair mode

Same ephemeral, selects swap for a single pair-picker:

```
┌──────────────────────────────────────────────────────────────────────┐
│ (same header + pair list as 7.24)                                   │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Pick a pair to unpair…                                          ]
[🔄 Confirm unpair (disabled)]  [↩️ Back]
```

Each option labels as `Primary → Sub`; description shows the zone.
Confirm clears the pairing + re-renders both surfaces; Back returns
to the Pair-subs view (Screen 7.24).

### Screen 7.26 — Roster-error warnings inline in the embed

When `session.roster_errors` is non-empty, the embed surfaces the
FIRST error inline above the auto-fill summary:

```
…
🎯 **Active zone:** **Power Tower** — minimum **300M**
_Members with no parseable power read as 'power unknown'; toggle the
override to assign them anyway._

⚠️ 3 signed-up member(s) couldn't be matched to a roster row:
Phantom1, Phantom2, Phantom3
```

Common error strings:
- `no power metric column configured — every member will read as 'power unknown'. Run /setup → ⚔️ Desert Storm or /setup → 🏜️ Canyon Storm to set the Power Metric Column.`
- `member-roster sync isn't enabled — without /members sync the builder can't see your alliance's roster.`
- `roster-sheet open failed: <gspread error>`
- `roster-sheet read failed: <gspread error>`
- `stale Discord IDs on roster (member likely left the server): Alice (id 1234) (+3 more)`
- `3 signed-up member(s) couldn't be matched to a roster row: Phantom1, Phantom2, …` (structured-mode only)

Per Decision #7 / #173, the per-member-rule subject-not-on-roster
warning was retired. A rule whose subject isn't in tonight's roster
is a silent no-op — nothing to apply, nothing to report. Other
rule-application conflicts (unknown zone, full when pinning, pinned
to multiple zones) still surface in the auto-fill summary
(Section 8).

Only one error shows in the embed at a time; the full list is logged.

### Flow at a glance

```
Entry A:  /desertstorm signups → 🅰️ Set up Team A   (structured)
Entry B:  /desertstorm strategy apply               (free-tier)
                       │
                       ▼
   Premium / structured / DM gates  → 7.4/7.5
   Preset lookup                    → 7.2/7.3
                       │
                       ▼
   (DS free-tier or CS teams=both)  → 7.1 team picker
                       │
                       ▼
   Defer · pre-warm cache · read roster · join signups · load rules
   (structured) claim session lock  → 7.7 if taken
   (structured) empty pool guard    → 7.6
                       │
                       ▼
   _apply_rules_to_session pins pre-defined assignments
                       │
                       ▼
   ┌────────────────────────────────────────────────────────────┐
   │ 7.8 (flat) / 7.22 (phase-aware) / 7.23 (paired)  embed     │
   └────────────────────────────────────────────────────────────┘
                       │
       ┌───────────────┼──────────────────┬─────────────┬──────────────┐
       ▼               ▼                  ▼             ▼              ▼
   Zone picker     Member picker     ↩️ Remove     🪑 Add all   🎯 Auto-fill
   → re-renders    → 7.11 zone-full   current       unassigned  → Section 8
                   → 7.10 empty pool  assignees    to Subs      summary
                                      → 7.13       → 7.14
                   (paired mode)
                     ↓ after primary
                     just refreshes — explicit
                     🔁 Pair subs (7.24) handles pairing
                       │
                       ▼
       ┌───────────────┼────────────────────┐
       ▼               ▼                    ▼
   📄 Preview     ✅ Approve & Post    🖼️ Render image
   / Generate    (structured)         → 7.17
   mail          → 7.19 post + write
   → 7.16
                       │
                       ▼
   💾 Save as preset (free-tier) → 7.18 modal
                       │
                       ▼
   ❌ Cancel / ✅ Done  → 7.20 close
   or 15-min timeout    → 7.21 silent disable
```

---

## 8. Auto-fill summary

The block appended to the builder embed (Section 7) after Kevin
clicks `🎯 Auto-fill` in structured mode. `_auto_fill_session`
returns a dict that the embed renderer surfaces at the bottom of the
description. Each line is conditional — empty buckets collapse.

### Screen 8.1 — Clean auto-fill (no gaps, no conflicts)

Apex roster, every member has a parseable power, every rule resolves,
no zones over-full. After `🎯 Auto-fill`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ … (builder embed body — see Section 7) …                             │
│                                                                      │
│ 🎯 **Auto-fill summary**                                             │
│ • Per-member rules applied: **0**                                    │
│ • Members slotted via a band-relaxed minimum: **0**                  │
│ • Auto-filled by power: **20**                                       │
│ • Conflicts: **0**                                                   │
└──────────────────────────────────────────────────────────────────────┘
```

Summary lines, in order:

1. `• Per-member rules applied: **N**` — per_member rules that successfully pinned a member.
2. `• Members slotted via a band-relaxed minimum: **N**` — greedy-fill assignments where a `power_band` rule lowered the minimum enough to admit a member who'd otherwise have been excluded by the preset minimum.
3. `• Auto-filled by power: **N**` — greedy-fill assignments (summed across every phase for phase-aware presets).
4. `• Auto-paired subs (N): Primary ↔ Sub, …` — paired-mode only; suppressed when no auto-pairs happened. Per Decision #14 / #171, each pair renders explicitly instead of a bare count.
5. `• Gaps (power unknown, not slotted): **N** — Erin, Frank, …` — every member with `power=None` who couldn't be slotted. Per Decision #8 / #171, the full list always renders — no `(+M more)` truncation.
6. `• Conflicts: **N** — <full list>` — every rule-application conflict, no truncation.

### Screen 8.2 — With gaps (power-unknown members)

```
│ 🎯 **Auto-fill summary**                                             │
│ • Per-member rules applied: **0**                                    │
│ • Members slotted via a band-relaxed minimum: **0**                  │
│ • Auto-filled by power: **17**                                       │
│ • Gaps (power unknown, not slotted): **3** — Uma, Vic, Wes           │
│ • Conflicts: **0**                                                   │
```

Power-unknown members aren't auto-added to the sub pool — they list
under Gaps so the officer can decide what to do (set their power in
the Sheet and re-run, or toggle the override and slot manually). Per
Decision #8 every gap is listed.

### Screen 8.3 — With conflicts (rule application failures)

```
│ 🎯 **Auto-fill summary**                                             │
│ • Per-member rules applied: **1**                                    │
│ • Members slotted via a band-relaxed minimum: **0**                  │
│ • Auto-filled by power: **19**                                       │
│ • Conflicts: **2** — per_member rule names unknown zone: Subway;     │
│ Alice pinned to multiple zones                                       │
```

Conflict shapes still surfaced (from `_auto_fill_session`):
- `per_member rule names unknown zone: <zone>` — rule points to a zone not in the current preset.
- `<zone> full when pinning <subject>` — zone reached capacity before this rule fired.
- `<subject> pinned to multiple zones` — same member named by two rules; first wins.

Per Decision #7 / #173, the pre-existing `per_member subject not on
roster: <subject>` conflict shape is **removed**. A rule whose subject
isn't in tonight's roster is a silent no-op — nothing to apply,
nothing to report.

Per Decision #8 the conflict preview shows every conflict, no
truncation (even at 6+ conflicts the full list renders).

### Screen 8.4 — With power-band rule relaxation

Alliance has a `power_band` rule `≥ 180M → Power Tower` that's lower
than Power Tower's preset Min A of 300M. Auto-fill admitted 2
members (190M and 220M) via the relaxed minimum:

```
│ … zones rendered with Power Tower at (4/4), Xena assigned at 220M …  │
│ 🎯 **Active zone:** **Power Tower** — minimum **180M** _(preset      │
│ minimum 300M relaxed by power_band rule)_                            │
│                                                                      │
│ 🎯 **Auto-fill summary**                                             │
│ • Per-member rules applied: **0**                                    │
│ • Members slotted via a band-relaxed minimum: **2**                  │
│ • Auto-filled by power: **20**                                       │
│ • Conflicts: **0**                                                   │
```

The Active-zone line's `_(preset minimum 300M relaxed by power_band
rule)_` is the embed's per-zone band indicator, separate from the
auto-fill counter — both live independently.

### Screen 8.5 — Paired mode with auto-paired subs

```
│ 🎯 **Auto-fill summary**                                             │
│ • Per-member rules applied: **0**                                    │
│ • Members slotted via a band-relaxed minimum: **0**                  │
│ • Auto-filled by power: **6**                                        │
│ • Auto-paired subs (6): Alice ↔ Uma, Bob ↔ Vic, Carol ↔ Wes,         │
│ Dan ↔ Xena, Erin ↔ Yale, Frank ↔ Zach                                │
│ • Conflicts: **0**                                                   │
```

`Auto-paired subs` line only appears when at least one pair was made.
In pool mode this line is always suppressed.

### Screen 8.6 — Phase-aware preset auto-fill

Per Rule L / #172, zones render in header + indented per-phase shape:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: CS Standard                                       │
│ 🗺️ Canyon Storm                                                      │
│ 🔀 Editing **Phase 1**                                               │
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
│ — **Mercenary Factory**                                              │
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

Zone-header status glyph reflects the SELECTED phase's fill state;
switching phases recolors the headers in place. The per-phase rows
always render every phase's count + member list — officers see all
three at a glance without phase-switching.

`📊 Filled:` breaks out per-phase counts on phase-aware sessions;
flat presets keep the single `X / Y` total.

### Screen 8.7 — Invalidation on manual edit

Any manual edit (move to subs, unassign, pick a new member) clears
`auto_fill_summary = None`; the summary block disappears from the
embed and the zone rows revert to their pre-auto-fill shape. Re-click
`🎯 Auto-fill` to regenerate — but on a non-fresh session this opens
the confirm prompt (Screen 8.8) first.

### Screen 8.8 — Destructive auto-fill re-run confirm (Decision #9 / #171)

Clicking `🎯 Auto-fill` on a session that already holds data (any
assignment / sub / pairing) opens a confirm ephemeral first. Fresh
sessions skip the prompt and run straight away.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ **Re-run auto-fill?** This will reset every assignment, sub      │
│ pairing, and override on this team. Manual edits you've made since  │
│ the last auto-fill will be lost.                                    │
└──────────────────────────────────────────────────────────────────────┘
[🎯 Re-run auto-fill]  [↩️ Cancel]
(ephemeral)
```

`🎯 Re-run auto-fill` runs the algorithm and refreshes both surfaces;
ephemeral becomes `🎯 Auto-fill re-run complete — main view refreshed.`

`↩️ Cancel` leaves everything untouched; ephemeral becomes
`↩️ Auto-fill cancelled — your edits are intact.`

### Flow at a glance

```
Kevin clicks 🎯 Auto-fill  on structured-mode builder
                       │
                       ▼
    Session has data?
                       │
        no  ─────┬──── yes
                 │     │
                 │     ▼
                 │  Screen 8.8 confirm
                 │     │
                 │  ↩️ cancel        🎯 re-run
                 │     │                │
                 │     ▼                ▼
                 │  done — exit         │
                 │                     │
                 ▼                     │
       _auto_fill_session ◀────────────┘
       resets assignments + subs + pairings + overrides
                       │
                       ▼
       1. per_member rules pin
       2. greedy fill per phase (band-aware)
       3. paired-mode: pair each primary with eligible sub
       4. spillover: known-power → subs; unknown-power → gaps
                       │
                       ▼
       session.auto_fill_summary populated
       embed re-renders with summary block
                       │
       ┌───────────────┼─────────────────┐
       ▼               ▼                 ▼
   8.1 clean      8.2/8.3 gaps/    8.7 manual edit
                   conflicts        invalidates summary
```

---

## 9. Approve & Post

The Approve & Post button lives in the roster-builder view
(Section 7). When the officer clicks it,
`_finalize_structured_roster` runs: it re-reads powers from the
Sheet (so `power_at_assignment` snapshots the moment-of-approval
value), builds the mail body via the alliance's saved template,
posts it to the configured channel, and writes one row per slot to
the `rosters_tab` Sheet.

The behaviour branches across four post-channel outcomes
(`posted_ok` / `no_channel` / `channel_gone` / `send_failed`).

> **Removed: Faction-roles offer (CS Judicator apply).** Pre-#167 a
> successful CS approve fired a `⚔️ Apply Faction Roles?` ephemeral
> asking which faction won and (if Rulebringers) applying the
> configured Judicator role to candidates flagged via a
> `per_member.special_role=judicator` rule. Per Rule G / #167 the
> Judicator concept was dropped end-to-end — `judicator_role_id`
> column dropped, `_maybe_offer_faction_roles` deleted, the
> `special_role` per_member sub-type removed, the apply-summary UI
> gone. CS Approve & Post now ends at Screen 9.2 like DS.

### Screen 9.1 — Public builder ack (replaces the builder embed)

After Approve & Post succeeds, the original roster-builder embed is
edited in place: the embed body stays (so the room can see the final
roster), the content line above flips to a success line, and every
interactive button on the view is disabled.

```
✅ Structured roster approved and posted.
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Roster Builder: Standard DS — Team A                              │
│ … (embed body unchanged) …                                            │
└──────────────────────────────────────────────────────────────────────┘
[Approve & Post (disabled)]  [Save as preset (disabled)]
[📄 Mail preview (disabled)]  [🖼️ Render PNG (disabled)]
[Cancel (disabled)]
```

(View is `stop()`'d, session lock released.)

### Screen 9.2 — Approve & Post officer summary (4 outcome variants)

**9.2a — Happy path (`posted_ok`):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster posted.                                                    │
│ 📬 Mail sent to #storm-rosters.                                      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**9.2b — No post channel configured (`no_channel`):**

Sheet rows were written; mail was built but never sent. Mail preview
inline so the officer can copy manually.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster recorded.                                                  │
│ ⚠️ No post channel is configured — mail was built but not sent.      │
│ Run `/setup → ⚔️ Desert Storm` (or `/setup → 🏜️ Canyon Storm`) to pick one, or   │
│ copy the mail manually below.                                        │
│                                                                      │
│ ```                                                                  │
│ **Alliance — Desert Storm**                                          │
│ … (mail body) …                                                      │
│ ```                                                                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**9.2c — Channel deleted / invisible (`channel_gone`):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster recorded.                                                  │
│ ⚠️ The configured post channel (<#1234567890>) is deleted or the     │
│ bot can't see it. Re-run setup to pick a new channel — mail preview  │
│ below.                                                               │
│                                                                      │
│ ```                                                                  │
│ … (mail body, truncated to 1800 chars with `…(truncated)` if longer) │
│ ```                                                                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**9.2d — Send rejected by Discord (`send_failed`):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Roster recorded.                                                  │
│ ⚠️ The configured post channel <#1234567890> rejected the send:      │
│ `403 Forbidden (error code: 50013): Missing Permissions`. Check the  │
│ bot's permissions in that channel — mail preview below.              │
│                                                                      │
│ ```                                                                  │
│ … (mail body) …                                                      │
│ ```                                                                  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**9.2e — Sheet-write soft error (appended to any of the above):**

```
⚠️ rosters tab header migration failed (data still appended, but
readers may not see new columns): APIError [400]: invalid_argument
```

`_write_rosters_tab` only surfaces the first error; subsequent ones
are logged.

### Screen 9.3 — The mail body posted to `#storm-rosters` (DS shape)

The bot posts the mail as a regular channel message (no embed
wrapper, no buttons — leadership copies the text into the in-game
mail system). Default DS template from
[defaults.py](../defaults.py):

```
**{alliance_name} — Desert Storm**

**Zone Assignments**
{zones}

**Subs**
{subs}

**Time:** {time}
```

`{alliance_name}` is hardcoded to the literal string `Alliance` by
`build_ds_mail`. Alliances who want their actual guild name
customise the template via `/desertstorm_draft`. `{zones}` renders
in canonical `DS_ZONE_STRUCTURE` order with each zone as a bold
header followed by one member per line. Empty zones are skipped.

Sample output:

```
**Alliance — Desert Storm**

**Zone Assignments**
**Nuclear Silo**
Alice
Bob
Carol
Dan

**Oil Refinery I**
Erin
Frank

**Field Hospital I**
…

**Subs**
Ghost

**Time:** 4pm EDT (18:00 server time)
```

Variants:
- **Paired sub mode**: a primary with a paired sub renders as `Alice + sub Bob` under the primary's zone. The global Subs block only contains overflow / unpaired subs.
- **Phase-aware preset**: each phase renders as its own block separated by `**Phase N**` headers.

### Screen 9.4 — The mail body posted to `#storm-rosters` (CS shape)

Same shape with `Canyon Storm` substituted. `{zones}` walks
`CS_ZONE_STRUCTURE` and groups zones under `**Stage 1**` /
`**Stage 2**` / `**Stage 3**` headers (only renders headers when at
least one zone in the stage has members).

Sample output:

```
**Alliance — Canyon Storm**

**Zone Assignments**
**Stage 1**
**Power Tower**
Alice
…

**Data Center 1**
Erin
…

**Stage 2**
**Defense System 1**
…

**Stage 3**
**Virus Lab**
…

**Subs**
Ghost

**Time:** 10am EDT (12:00 server time)
```

> Per #178 (post-PR-180 fix): `canonical_zones_for("CS")` now
> returns display names, not internal storage keys like
> `s1_power_tower`. The mail body, preset editor, and rosters_tab
> writes all consume the display-name form via a translation-at-
> read-boundary helper (`_translate_legacy_cs_zone`) so legacy data
> with internal keys keeps working.

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
9.2 ephemeral summary (4 variants + optional Sheet error)
   │
   ▼
   Done — no faction-roles offer (per #167)
```

---

## 10. `/desertstorm attendance` + `/canyonstorm attendance` — post-event attendance

Premium-gated officer view for marking who actually showed at each
assigned roster slot. Writes one row per slot to the alliance's
configured `attendance_tab` Sheet. Closes the structured-flow loop:
the bot knows who was assigned; this is how it learns who showed.

Status codes the view writes: `attended`, `no_show`, plus the empty
string for unrecorded. UI labels: ✅ Attended / ❌ No-show / — (dash
for unrecorded). Per Rule K / #171, `sub_activated` was dropped from
the UI entirely; legacy rows still on the Sheet roll into the `—`
bucket so the math stays correct.

### Screen 10.1 — Slash command invocation

```
/desertstorm attendance ▾
  event_date     Optional — defaults to the most recent posted event.
                 Accepts e.g. May 18, 5/18, yesterday.
```

`guild_only=True`. CS form is identical under `/canyonstorm attendance`.

### Screen 10.2 — Permission denial (non-leader / non-admin)

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to run this command.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 10.3 — Premium gate

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🔒 `/desertstorm attendance` is a 💎 Premium feature. Run `/upgrade` │
│ to unlock it.                                                        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 10.4 — Structured-flow disabled

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ The structured roster flow isn't enabled for Desert Storm. Run    │
│ `/setup → ⚔️ Desert Storm` and turn on **Structured Roster Flow** first.   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 10.5 — Date-parse failure

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ `gibberish` isn't a date I can parse. Try `May 18`, `5/18`,      │
│ `2026-05-18`, `yesterday`, or `today`.                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 10.6 — No recent events on record

Kevin omitted `event_date` and `most_recent_event_date` returned None:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No posted Desert Storm events on record. Run                     │
│ `/desertstorm post_signup` and build a roster before recording      │
│ attendance, or pass `event_date` explicitly.                        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 10.7 — No structured roster found for the resolved date

**10.7a — Clean miss (no Sheet errors):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No structured roster found for **Saturday, May 18, 2026**        │
│ (Desert Storm). Attendance is only recordable for events with a     │
│ structured roster posted via `/desertstorm signups`.                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**10.7b — Sheet I/O error:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ No structured roster found for **Saturday, May 18, 2026**        │
│ (Desert Storm). Details: rosters tab 'Rosters' doesn't exist yet —  │
│ post a structured roster first via /desertstorm signups             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 10.8 — The attendance view (DS shape, both teams)

Posted as a public (non-ephemeral) message. Other leadership in the
channel can see the embed; only Kevin can interact with controls.

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
[ ▾ Pick a slot to record attendance (or use the bulk-mark buttons    ]
[ below)…                                                             ]
[✅ Mark all unrecorded as attended]
[❌ Mark all unrecorded as did not attend]
[↩️ Clear selection (disabled)]
[💾 Save attendance]
```

Per-slot line shape:
`<status icon> <member name> (<zone or "sub">) <role marker>`

- Status icon: ✅ / ❌ / — from `_STATUS_LABELS[status]`. New session starts with `—` everywhere unless `load_attendance` brought forward prior records.
- Zone part: `(<zone>)` for primaries; `(sub)` for sub-pool members.
- Role marker: ` 🪑` for sub-pool entries.
- **Decision #6 / #171:** the `⚠️ Override Below Floor` glyph and footnote are **gone**. The rosters_tab Sheet column survives for post-event audit (renamed to `Override Below Minimum` per Rule M / #160), but officers recording attendance don't need it surfaced.

Footer counts (Rule K): `✅ <attended>  ·  ❌ <no-show>  ·  — <unrecorded>` —
three columns only. Color: gold (DS) / orange (CS).

### Screen 10.9 — Attendance view variant: CS shape

Per Rule A / #166, CS respects `cfg.teams`. `teams=both` renders
`**Team A**` / `**Team B**` headers like DS; single-team CS uses
`**Roster**`.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Canyon Storm Attendance — Thursday, May 21, 2026                 │
│                                                                      │
│ **Team A**                                                           │
│ — Alice (Power Tower)                                                │
│ … (or **Roster** header for single-team)                             │
└──────────────────────────────────────────────────────────────────────┘
(controls identical to 10.8; color orange)
```

### Screen 10.10 — Pagination (>25 slots)

Discord's Select caps at 25 options. `_AttendanceView._build()` slices
the slot list by `session.page * session.per_page` and emits
`◀ Prev` / `Next ▶` buttons. **The embed always shows every slot** —
only the dropdown paginates.

```
[ ▾ Pick a slot… (slots 1-25)                                       ]
[✅ Mark all unrecorded as attended]
[❌ Mark all unrecorded as did not attend]
[↩️ Clear selection (disabled)]
[◀ Prev (disabled)]  [Next ▶]
[💾 Save attendance]
```

Switching pages clears `selected_key` so a stale selection doesn't
follow Kevin across pages.

### Screen 10.11 — Empty slots (edge case)

`load_rostered_slots` returned `slots=[]` with no errors. Per #171,
the action buttons are **hidden entirely** (nothing to mark; dead
buttons would be misleading):

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

### Screen 10.12 — Existing-attendance read warning

`load_attendance` reported errors when fetching prior-recorded rows.
View posts with the warning as the message `content`:

```
⚠️ Read existing attendance had issues — see bot logs. You can still
record fresh entries below.
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm Attendance — Saturday, May 18, 2026                 │
│ … (slots embed) …                                                    │
└──────────────────────────────────────────────────────────────────────┘
(public; controls identical to 10.8)
```

### Screen 10.13 — Picker selection (no ephemeral, just label swap)

Per Decision #5 / #171, the pre-existing `_StatusPickerView` ephemeral
was retired. Kevin picks a slot from the dropdown:

- Dropdown placeholder updates to `Picked: Alice`.
- Action buttons swap labels:
  - `✅ Mark all unrecorded as attended` → `✅ Mark as attended`
  - `❌ Mark all unrecorded as did not attend` → `❌ Mark as did not attend`
- `↩️ Clear selection` enables.

```
[ ▾ Picked: Alice                                                  ]
[✅ Mark as attended]
[❌ Mark as did not attend]
[↩️ Clear selection]
[💾 Save attendance]
```

No new ephemeral — the buttons act on the selected slot directly.

### Screen 10.14 — Single-slot mark ack

Clicking `✅ Mark as attended` writes the status, clears
`selected_key`, and re-renders. Embed updates Alice's row from
`— Alice …` → `✅ Attended Alice …`. Footer ticks from
`✅ 0 · ❌ 0 · — 14` → `✅ 1 · ❌ 0 · — 13`. No ephemeral — the
embed update IS the ack.

### Screen 10.15 — Clear selection

`↩️ Clear selection` resets the picked slot's status to `—` AND drops
selection back to no-selection mode (action buttons swap back to
bulk-mark labels).

### Screen 10.16 — Bulk-mark unrecorded → Attended

With no slot selected, action buttons are the bulk variants.
`✅ Mark all unrecorded as attended` flips every `—` slot to ✅;
already-recorded slots are not touched. Mirror for the ❌ button.

Before: `Footer: ✅ 2  ·  ❌ 1  ·  — 27`
After `✅ Mark all unrecorded as attended`: `Footer: ✅ 29  ·  ❌ 1  ·  — 0`

### Screen 10.17 — Non-owner click guard

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who opened this view can record attendance.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 10.18 — Save attendance (happy path)

`💾 Save attendance` defers ephemerally with `thinking=True`, calls
`save_attendance` off the event loop, then sends:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved attendance for **Saturday, May 18, 2026** — 29 slot(s)     │
│ recorded (✅ 28, ❌ 1).                                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

`recorded` is the sum of attended + no_show. Unrecorded slots aren't
counted toward `recorded` and aren't written to the Sheet (the save
path filters them out). The public attendance view's buttons all
disable; the embed body stays visible for post-event reference.

### Screen 10.19 — Save attendance (Sheet write soft error)

`save_attendance` returned a non-empty error list:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Attendance partially saved — attendance trailing-blank failed   │
│ (new data written, stale rows 31..47 may remain): APIError [429]:   │
│ rate limit                                                           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

After a soft error, the view is NOT stopped — Kevin can edit and try
Save again.

### Screen 10.20 — Attendance view timeout

900-sec (15 min) timeout. Every button strips; view stays visible
for the room. In-progress unsaved changes are lost; re-running the
slash command re-loads any prior-saved state via `load_attendance`.

### Flow at a glance

```
Kevin types /desertstorm attendance [event_date]
                │
                ▼
   Leader check?           ── no ─→ 10.2 deny
                │
                ▼
   event_date parsed?      ── no ─→ 10.5 parse error
                │
                ▼
   Date resolved? (omit → most_recent_event_date)
                │
                ├── no events ─→ 10.6 no-events
                │
                ▼
   Premium?                ── no ─→ 10.3 gate
                │
                ▼
   Structured flow on?     ── no ─→ 10.4 gate
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
                ├── attendance read errors → 10.12 warning content
                ├── slots == 0 edge       → 10.11 empty body, no buttons
                ├── slots > 25            → 10.10 pagination
                ├── DS / CS teams=both    → 10.8 / 10.9 Team A+B
                └── CS single-team        → 10.9 Roster header
                │
                ▼
   Officer picks slot → 10.13 in-place label swap (no ephemeral)
                │
                ▼
   ✅ / ❌ / ↩️ Clear → 10.14 / 10.15 in-place re-render
                │
                ▼
   Bulk-mark → 10.16 walks unrecorded slots
                │
                ▼
   💾 Save → 10.18 ok / 10.19 soft error / view disables on ok
                │
                ▼
   Or 15-min view timeout → 10.20 strip buttons
```

---

## 11. `/desertstorm strategy` + `/canyonstorm strategy` commands

Two parallel command groups (`/desertstorm strategy` for DS,
`/canyonstorm strategy` for CS) wrap CRUD over an alliance's saved
strategy presets. Six subcommands per group: `create`, `edit`,
`list`, `delete`, `apply`, `roster_history`.

Both groups inherit `app_commands.Group` (per the
`feedback_app_commands_groups` memory rule). Permissions: every
subcommand runs `_deny_if_not_leader` up front.

The editor (Section 12) is what `create` and `edit` open; this
section stops at the slash-command entry points and the auxiliary
commands.

### Screen 11.1 — `/desertstorm strategy create`

```
/desertstorm strategy create ▾
  name *   A short name for the preset (e.g. 'Standard Desert')
```

**11.1a — Non-leader:** standard deny.

**11.1b — Empty name:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Pick a preset name (e.g. `Standard Desert`).                      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**11.1c — Duplicate name (case-insensitive):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ A preset named **Standard DS** already exists. Use               │
│ `/ds strategy edit name:"Standard DS"` to modify it.                │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**11.1d — Happy path:** `seed_default_preset("Standard DS", "DS")`
builds a buffer pre-populated with `DS_ZONE_STRUCTURE` zones at
capacity 0. Buffer marked dirty (Save enables immediately). Editor
opens publicly — Section 12.

### Screen 11.2 — `/canyonstorm strategy create`

Same shape; CS-flavoured hints. Happy path: `seed_default_preset("CS
Standard", "CS")` populates a buffer from the 13 canonical CS zones
in `CS_ZONE_STRUCTURE`:

- **Stage 1:** Power Tower, Data Center 1/2, Sample Warehouse 1–4
- **Stage 2:** Defense System 1/2, Serum Factory 1/2
- **Stage 3:** Virus Lab, Power Tower (stage-3 repeat).

Per #178, `canonical_zones_for("CS")` returns display names (not
internal storage keys like `s1_power_tower`) — the buffer ships
with human-readable zone names that match what the editor + roster
builder render. Per the floaters-removal commit `121662d`, Floaters
is no longer in the structure (it was never a real LW zone).

### Screen 11.3 — `/desertstorm strategy edit`

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

**11.3b — Happy path:** `load_preset` returns a populated buffer
with `dirty=False`. Editor opens publicly — Save starts disabled.

### Screen 11.4 — `/canyonstorm strategy edit`

Identical to 11.3. The loaded buffer includes the `faction` field
(defaults to `Either`) which only CS uses. Legacy CS presets whose
zone keys are stored as `s1_power_tower`-style internal keys get
translated to display names at load time via
`_translate_legacy_cs_zone` (#178).

### Screen 11.5 — `/desertstorm strategy list` (Rule M / #169)

Per #169, the list view ships with inline action buttons so officers
never hit a dead-end summary. Empty + populated states share the
same view — Edit and Delete are just disabled when no presets.

**11.5a — No presets (empty state):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Strategy Presets                                  │
│                                                                      │
│ *No Desert Storm strategy presets saved yet.* Click **➕ Create**   │
│ below to make one.                                                   │
└──────────────────────────────────────────────────────────────────────┘
[➕ Create]  [✏️ Edit (disabled)]  [🗑️ Delete (disabled)]
(public)
```

**11.5b — Presets exist:**

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

Color: blurple. Action buttons:
- `➕ Create` → `_CreatePresetNameModal`, validates uniqueness, opens editor.
- `✏️ Edit` → ephemeral `_PresetPickerView` with a Select listing every preset (sorted case-insensitively, capped at 25).
- `🗑️ Delete` → same picker shape, then `_ConfirmDeleteView` confirm + delete flow.

**11.5c — Overflow notice (>25 presets):** the Select caps at 25;
an inline `overflow_notice` content line warns about truncation
(added in the post-#180 audit-fix sweep):

```
ℹ️ Showing the most recent 25 presets — your alliance has 31 saved.
Older presets aren't pickable here.
```

### Screen 11.6 — `/canyonstorm strategy list`

Identical shape; title `Canyon Storm — Strategy Presets`. Same
inline action row.

### Screen 11.7 — `/desertstorm strategy delete`

```
/desertstorm strategy delete ▾
  name *   The saved preset to delete
```

**11.7a — Confirmation prompt (ephemeral, 60-sec window):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Delete preset **Standard DS**? This removes all rows for this    │
│ preset from your Sheet. Can't be undone.                            │
└──────────────────────────────────────────────────────────────────────┘
[🗑️ Delete preset]  [Cancel]
(ephemeral; 🗑️ red / Cancel grey)
```

**11.7b — Confirm clicked, delete succeeded (PUBLIC followup):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🗑️ Deleted preset **Standard DS**.                                  │
└──────────────────────────────────────────────────────────────────────┘
(public — no buttons; channel sees it)
```

**11.7c — Delete failed (Sheet write or preset gone):**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't find preset **Standard DS** to delete (or Sheet write   │
│ failed).                                                             │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**11.7d — Cancel clicked / 60-sec timeout:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Delete cancelled.                                                 │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

**11.7e — Wrong-user click:**

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the user who ran the command can confirm.                    │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral; mirror for Cancel: "…cancel.")
```

### Screen 11.8 — `/canyonstorm strategy delete`

Identical to 11.7 with the CS preset name swapped. Both commands
share `_ConfirmDelete`.

### Screen 11.9 — `/desertstorm strategy apply`

```
/desertstorm strategy apply ▾
  name *   The preset to apply (use the list command to see saved presets)
```

Thin shim that imports `storm_roster_builder` at call time and
delegates to `open_roster_builder(interaction, "DS", name)`. The
roster builder flow is Section 7; this command has no direct
user-facing ephemerals beyond what the builder posts.

### Screen 11.10 — `/canyonstorm strategy apply`

Identical to 11.9 with `event_type="CS"`.

### Screen 11.11 — `/desertstorm strategy roster_history`

```
/desertstorm strategy roster_history ▾
  date     Optional — show a specific date (May 18, 5/18, 2026-05-18,
           yesterday). Omit to list recent events.
```

Thin shim delegating to `storm_history.open_history(interaction,
"DS", date)`. History browser UX is Section 15.

### Screen 11.12 — `/canyonstorm strategy roster_history`

Identical to 11.11 with `event_type="CS"`.

### Autocomplete (post-PR-180 audit fix)

Zone-name parameters across the storm command tree now ship with
`app_commands.autocomplete` (per the #168 M1 audit-fix sweep). When
an officer types `/desertstorm member_rule …` and the slash command
expects a zone name, Discord shows the alliance's canonical zone
list — no more typo'd zone strings landing as silent no-ops.

### Flow at a glance

```
/desertstorm strategy
  ├── create name:<X>
  │     ├── empty / dup / non-leader → 11.1 ephemerals
  │     └── ok → seed_default_preset → Section 12 editor
  │
  ├── edit name:<X>
  │     ├── not found → 11.3a
  │     └── ok → load_preset → Section 12
  │
  ├── list
  │     ├── empty → 11.5a (Edit/Delete disabled)
  │     ├── ok    → 11.5b inline buttons
  │     └── >25   → 11.5c overflow notice
  │
  ├── delete name:<X>
  │     └── 11.7 confirmation + outcomes (a/b/c/d/e)
  │
  ├── apply name:<X> → Section 7 builder
  └── roster_history date:<X?> → Section 15 history browser
```

CS tree: identical six subcommands; `apply` + `roster_history`
delegate with `event_type="CS"`.

---

## 12. Strategy preset editor

Opens from `/desertstorm strategy create / edit` and
`/canyonstorm strategy create / edit`. A public Discord embed +
view with: a zone dropdown, a phase-mode dropdown (Flat / 2 phases /
3 phases), and action buttons (Rename / Save / Abandon).

In-memory state lives on `_PresetEditorView.buf` — a `PresetBuffer`
with `name`, `event_type`, `zones[]`, `faction` (CS only),
`phase_count` (0/2/3), and `dirty` flag. Discord's interaction
token expires after 15 minutes, which is the editor's natural
session bound.

The editor branches heavily on `buf.phase_count`:
- `phase_count == 0` → **flat** preset. Picking a zone opens the single-page `_ZoneEditModal`. Zone lines render with `(Max: N)`.
- `phase_count == 2/3` → **phase-aware**. Picking a zone routes through a 2-page wizard (per Decision #13 / §12.9 of the UX plan, the pre-#174 3-page version was collapsed). Page 1 (`_ZonePhaseCapacityAndFloorsModal`) packs phase capacities and power minimums together at the 5-field Discord cap. Page 2 (`_ZonePhasePriorityModal`) holds per-phase priority. Zone lines render with the **header + per-phase indented** Rule L shape.

> **Removed: `➕ Add zone` button + `_AddZoneModal`.** Per Decision
> #13 / #174 the pre-#174 free-form "add a custom zone" button is
> gone. Zones come exclusively from `DS_ZONE_STRUCTURE` /
> `CS_ZONE_STRUCTURE` (the canonical game-defined lists); alliances
> configure max-players, minimum power, and priority for those
> canonical zones but can't add new ones.

### Screen 12.1 — Editor embed (flat DS, both teams, just-created)

`/desertstorm strategy create name:Standard DS`. `seed_default_preset`
builds a buffer with the 11 canonical DS zones at `max_players=0`,
`dirty=True`. Editor posts publicly.

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
│ ⚠️ *Unsaved changes — Save preset to save your changes.*             │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Edit a zone…                                                  ]
[ ▾ 🔀 Phase mode: Flat (no phases) (default-selected)             ]
[✏️ Rename preset]  [💾 Save preset]  [🔙 Abandon this preset]
```

Capacity gauge glyph rules:
- `< 30` → ⚠️ (under-staffed)
- `== 30` → ✅ (exact)
- `> 30` → ℹ️ (flex room; not an error)

Save button enabled because `dirty=True` (new preset). On a freshly
loaded edit (no changes yet) the Save button starts disabled.

### Screen 12.2 — Editor embed: flat DS, Team A only

When `/setup → ⚔️ Desert Storm` was run with `teams=A`. Per-zone lines
only show `Min` (no Team A/B suffix):

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Editing Preset: Standard DS                                       │
│                                                                      │
│ 🗺️ Event: Desert Storm                                               │
│ 👥 Teams: **Team A only** (minimums shown match)                     │
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

### Screen 12.3 — Editor embed: flat CS

CS adds a `⚙️ Faction:` line (default `Either`). Per #178,
`canonical_zones_for("CS")` returns display names — deduped to 13
unique zones, not the legacy 19-entry stage-keyed list.

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
│ • Defense System 1    (Max: 0)  Min: 0                               │
│ • Defense System 2    (Max: 0)  Min: 0                               │
│ • Serum Factory 1     (Max: 0)  Min: 0                               │
│ • Serum Factory 2     (Max: 0)  Min: 0                               │
│ • Virus Lab           (Max: 0)  Min: 0                               │
│                                                                      │
│ 📊 Capacity: **0** (team size 30; flex room is fine) ⚠️              │
│ ⚠️ *Unsaved changes — Save preset to save your changes.*             │
└──────────────────────────────────────────────────────────────────────┘
[ ▾ Edit a zone…                                                  ]
[ ▾ 🔀 Phase mode: Flat (no phases)                                ]
[✏️ Rename preset]  [💾 Save preset]  [🔙 Abandon this preset]
```

(Per the floaters-removal commit `121662d`, Floaters is no longer in
the CS structure. The list above is the post-#178 13-zone canonical
set.)

### Screen 12.4 — Editor embed: 3-phase CS (Rule L / #172)

`buf.phase_count = 3`. Mode line flips; each zone breaks into a
header row plus one indented row per phase:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Editing Preset: CS Standard                                       │
│                                                                      │
│ 🗺️ Event: Canyon Storm                                               │
│ ⚙️ Faction: Either                                                   │
│ 🔀 Mode: **3 Phases (P1 + P2 + P3)**                                 │
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

Capacity sums every phase's max for every zone (e.g. Power Tower at
4 across all 3 phases = 12).

### Screen 12.5 — Phase-mode toggle: switching from Flat → 2 Phases

```
[ ▾ 🔀 Phase mode                                                  ]
  ○ Flat (no phases)               Single per-zone slot — Max Players only.
  ○ 2 Phases                       DS-style migration: Phase 1 → Phase 2.
  ○ 3 Phases                       CS-style stages: Phase 1 → 2 → 3.
```

Picking `2 Phases` seeds capacities/priorities (every zone's
`max_phase1 ← max_players`, `max_phase2 ← max_phase1`, etc.), flips
`buf.phase_count = 2`, marks dirty, and re-renders with a content
line:

```
🔀 Switched to **2-phase** mode. Capacities + assignments are kept.
Re-select **Flat** mode to restore without data loss. Seeded N
per-zone capacity/priority value(s) from prior values; edit any
zone to override.
```

If no values were auto-seeded (every zone already had non-zero
phase fields), the seeded note is omitted. Re-picking the same mode
silently defers — no embed re-render.

### Screen 12.6 — Phase-mode toggle: switching to Flat

```
🔀 Switched to **Flat** mode. Capacities + assignments are kept.
Re-select **2-phase** mode to restore without data loss.
```

Zone lines collapse to `(Max: N)` shape again. No auto-clear of
phase data (re-toggling restores it).

### Screen 12.7 — Flat zone edit modal (DS, both teams)

Pick `Nuclear Silo`. `_ZoneEditModal` opens (4 fields for DS both,
3 for single-team DS, 3 for CS).

```
┌──────────────────────────────────────────────────────────────────────┐
│ Edit Zone: Nuclear Silo                                              │
│ ──────────────────────────────────────────                           │
│ Max Players               [ 4                                  ]     │
│   placeholder: e.g. 4                                                │
│                                                                      │
│ Min Power Team A          [ 300M                               ]     │
│   placeholder: e.g. 300M                                             │
│                                                                      │
│ Min Power Team B          [ 180M                               ]     │
│   placeholder: e.g. 180M                                             │
│                                                                      │
│ Priority (1 = highest)    [ 1                                  ]     │
│   placeholder: e.g. 1 — same number across zones is fine             │
└──────────────────────────────────────────────────────────────────────┘
[Submit]  [Cancel]
```

Single-team DS omits the unused Min field. CS collapses the two
team fields into a single `Min Power` with placeholder `e.g. 250M`.

### Screen 12.8 — Flat zone edit modal: validation

| Failure | Copy |
|---------|------|
| Max Players non-numeric | `⚠️ Max Players must be a number — got \`four\`. Try again.` |
| Power value didn't parse (DS) | `⚠️ One of the power values didn't parse. Use formats like \`300M\`, \`1.2B\`, or \`300000000\`. Leave blank for no minimum.` |
| Power value didn't parse (CS) | `⚠️ Couldn't parse \`tbd\` as a power value. Try \`250M\`, \`1.2B\`, or \`300000000\`. Leave blank for no minimum.` |
| Priority non-numeric | `⚠️ Priority must be a number — got \`top\`. Try again.` |

Submit success: `upsert_zone` lands; embed re-renders with
content line `✏️ Updated **Nuclear Silo**.`

### Screen 12.9 — Phase-aware zone edit: page 1 of 2 (capacity + minimums)

Per Decision #13 / §12.9 of the UX plan: capacity + minimums folded
into one modal at the 5-field Discord cap. `_ZonePhaseCapacityAndFloorsModal`.
Title truncated to 45 chars: `Power Tower — Caps + Min (3P)`.

Worst case (DS-both + 3 phases): 3 caps + 2 min fields = exactly 5
Discord-modal components.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Power Tower — Caps + Min (3P)                                        │
│ ──────────────────────────────────────────                           │
│ Max Phase 1                [ 4                                 ]     │
│   placeholder: e.g. 4 (leave 0 to skip Phase 1 at this zone)         │
│                                                                      │
│ Max Phase 2                [ 4                                 ]     │
│   placeholder: e.g. 2 (leave 0 to skip Phase 2 at this zone)         │
│                                                                      │
│ Max Phase 3                [ 4                                 ]     │
│   placeholder: e.g. 3 (leave 0 to skip Phase 3 at this zone)         │
│                                                                      │
│ Min Power Team A           [ 300M                              ]     │
│   placeholder: e.g. 300M                                             │
│                                                                      │
│ Min Power Team B           [ 180M                              ]     │
│   placeholder: e.g. 180M                                             │
└──────────────────────────────────────────────────────────────────────┘
[Submit]  [Cancel]
```

Variants:
- `phase_count == 2`: Max Phase 3 omitted (4 fields).
- CS: single `Min Power` instead of two team fields.
- DS single-team: only the relevant Min field.

### Screen 12.10 — Page 1 submit: validation + bridge

Validation errors surface field-by-field — `Max Phase 1` /
`Min Power Team A` / `Min Power` etc. are named in the error:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Max Phase 1 must be a number — got `four`. Reopen the zone to   │
│ retry.                                                               │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Min Power Team A didn't parse — got `tbd`. Use `300M`, `1.2B`,   │
│ or `300000000`.                                                      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

The wizard does NOT auto-reopen on parse failure; officer goes back
to the editor and picks the zone again. Already-validated fields in
the pending stash persist.

Submit success → bridge ephemeral:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Capacities + minimums recorded for **Power Tower**. Click        │
│ **Next** to set the per-phase auto-fill priorities.                  │
└──────────────────────────────────────────────────────────────────────┘
[Next → Priority Per Phase]
(ephemeral; 5-min timeout)
```

Clicking Next disables the button in place (no double-modal) and
opens `_ZonePhasePriorityModal`.

> **Removed: Screen 12.11.** Per Decision #13 / #174, the pre-#174
> 3-page wizard collapsed Page 1 (capacity) and Page 2 (minimums)
> into the combined `_ZonePhaseCapacityAndFloorsModal` (Screen
> 12.9). The old Page 2 modal is gone; numbering stays at 12.x to
> avoid churning every cross-reference.

### Screen 12.12 — Phase-aware zone edit: page 2 of 2 (priority)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Power Tower — Priority (3P)                                          │
│ ──────────────────────────────────────────                           │
│ Priority Phase 1 (1 = highest)  [ 1                            ]     │
│   placeholder: leave blank for no priority                           │
│                                                                      │
│ Priority Phase 2                 [ 1                            ]    │
│ Priority Phase 3                 [ 1                            ]    │
└──────────────────────────────────────────────────────────────────────┘
[Submit]  [Cancel]
```

`phase_count == 2` omits Priority Phase 3.

Validation: `⚠️ Priority Phase 1 (1 = highest) must be a number —
got \`top\`. Reopen the zone to retry.`

Submit success: `upsert_zone` lands all accumulated values
(capacity + minimums from page 1 stash + priorities from this page).
Editor refreshes with `✏️ Updated **Power Tower** (3-phase).`

### Screen 12.13 — Wizard guards

**Wrong user clicks Next on the bridge ephemeral:**
`⛔ Only the editor's owner can advance the wizard.`

**Unknown next_page (defensive):**
`⚠️ Unknown wizard step \`something_else\`.`

### Screen 12.14 — Apply-to-similar prompt

When the just-edited zone has numbered siblings (e.g. Sample
Warehouse 1 has 2/3/4), `_ApplyToSimilarView` follows up:

```
💡 **Sample Warehouse 1** has similar zones in this preset: Sample
Warehouse 2, Sample Warehouse 3, Sample Warehouse 4. Would you
like to apply the same settings to these as well?
[ ▾ Select zones                                                   ]
[Apply to selected]  [Skip]
(ephemeral; 5-min timeout)
```

- `min_values=0, max_values=N` — multi-select; nothing pre-selected.
- **Apply with nothing selected:** `⚠️ Pick at least one sibling from the dropdown first, or use Skip to dismiss.`
- **Apply with selections:** `✅ Copied **Sample Warehouse 1** settings to 3 sibling(s): …`
- **Skip:** `OK — only the edited zone was changed.`

Zones with no numeric tail (Arsenal, Virus Lab, etc. — unique
buildings) and zones where no sibling exists trigger NO prompt.

### Screen 12.15 — Rename Preset modal

```
┌──────────────────────────────────────────────────────────────────────┐
│ Rename Preset                                                        │
│ ──────────────────────────────────────────                           │
│ New preset name                                                      │
│ [ Standard DS                                                  ]     │
└──────────────────────────────────────────────────────────────────────┘
[Submit]  [Cancel]
```

- Empty: `⚠️ A preset name is required.`
- Duplicate: `⚠️ A preset named **Other Preset** already exists. Pick a different name.` (current name is excluded from the dup check.)
- Success: `✏️ Renamed **Standard DS** → **Apex DS v2**.`

### Screen 12.16 — Save Preset (happy path)

Public followup + editor in-place lock:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved preset **Standard DS** (11 zones, capacity 30).            │
└──────────────────────────────────────────────────────────────────────┘
(public; no buttons)
```

Editor message in-place: `⚠️ Unsaved changes` line removed
(`dirty=False`); every interactive element disabled.

### Screen 12.17 — Save Preset (failure)

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Could not save preset — check that your Google Sheet is          │
│ configured and that the bot has edit access. See logs for details.  │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Editor stays interactive; `dirty` stays True; Save remains enabled
so Kevin can retry.

### Screen 12.18 — Abandon

```
🔙 Abandoned. Changes were not saved.
┌──────────────────────────────────────────────────────────────────────┐
│ 🛡️ Editing Preset: Standard DS                                       │
│ … (in-progress state preserved for reference) …                      │
└──────────────────────────────────────────────────────────────────────┘
(buttons all disabled)
```

Nothing written to the Sheet.

### Screen 12.19 — Wrong-user guard

Every interactive component runs the same check:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the editor's owner can change this preset.                   │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Variants for Save (`…save this preset.`) and Abandon (`…abandon this
preset.`).

### Screen 12.20 — Editor timeout (15 min)

`wizard_registry.expire_view_message` strips buttons and appends:

```
⏰ This editor timed out. Re-open with `/desertstorm strategy edit
name:"Standard DS"`.
```

### Flow at a glance

```
/desertstorm strategy create / edit OR /canyonstorm equivalents
                │
                ▼
   _PresetEditorView posted publicly
                │
                ▼
   12.1 / 12.2 / 12.3 / 12.4 — variant by event_type + teams + phase_count
                │
                ├──► [ ▾ Edit a zone… ]
                │       │
                │       ├── phase_count == 0 → 12.7 _ZoneEditModal
                │       │       │
                │       │       ├── parse error → 12.8
                │       │       └── ok          → upsert + 12.14 apply-to-similar?
                │       │
                │       └── phase_count >= 2 → 12.9 caps + min modal
                │               │
                │               ├── parse error → 12.10 error
                │               └── ok → bridge → 12.12 priority modal
                │                                  │
                │                                  ├── parse error
                │                                  └── ok → upsert + 12.14
                │
                ├──► [ ▾ 🔀 Phase mode ] → 12.5 / 12.6 seeds + refresh
                │
                ├──► [✏️ Rename] → 12.15 modal
                │
                ├──► [💾 Save preset]
                │       ├── fail → 12.17
                │       └── ok   → 12.16
                │
                ├──► [🔙 Abandon] → 12.18
                │
                ├──► Wrong user on any control → 12.19
                │
                └──► 15-min timeout → 12.20 expire_view_message
```

---

## 13. `/desertstorm member_rule` + `/canyonstorm member_rule` commands

Per-member overrides + power-band eligibility rules. Two parallel
slash-command groups (DS + CS) backed by
[storm_member_rules.py](../storm_member_rules.py). Rules live in
`DS Member Rules` / `CS Member Rules` Sheet tabs with five columns:
`Rule Type | Subject | Sub-Type | Value | Notes`.

Per Rule A / #166, the DS group exposes **five** subcommands; the
CS group now also exposes five (`set_member_team` was added to CS
when #166 brought back A/B teams for CS):

```
/desertstorm member_rule  set_power_band | set_member_team | set_member_zone | list
/canyonstorm member_rule  set_power_band | set_member_team | set_member_zone | list
```

> **Removed: `set_member_role` subcommand.** Per Rule G / #167, the
> pre-existing `set_member_role` subcommand and the `special_role`
> rule type are gone. The Judicator / Commander tagging mechanism
> was retired across the schema, the cog, the list rendering (no
> more `🎖️` row), and the post-Approve faction-roles offer
> (Section 9). Per-member rules are now `team` and `zone` only.

All subcommands are leadership-gated (admin OR `leadership_role_name`)
and guild-only.

### Screen 13.1 — `/desertstorm member_rule set_power_band`

```
/desertstorm member_rule set_power_band ▾
  threshold *  Minimum power (e.g. 250M, 1.2B, 300,000,000)
  zone *       Zone the band applies to (e.g. Power Tower)
  notes        Optional free-text notes
```

Per the post-PR-180 audit-fix sweep, the `zone` parameter now ships
with `app_commands.autocomplete` — typing pops up the alliance's
canonical zones for that event type, so typo'd zone strings can't
silently land.

### Screen 13.2 — `set_power_band` success

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: ≥ 300M → eligible for Power Tower.                         │
└──────────────────────────────────────────────────────────────────────┘
(public — non-ephemeral)
```

### Screen 13.3 — `set_power_band` with non-canonical zone

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: ≥ 300M → eligible for Power Towr.                          │
│ ⚠️ `Power Towr` isn't in the canonical zone list — the rule was      │
│ saved, but double-check the spelling.                                │
└──────────────────────────────────────────────────────────────────────┘
(public)
```

The rule saves anyway — bot doesn't gatekeep on spelling.

### Screens 13.4–13.8 — `set_power_band` validation + Sheet errors

| Screen | Trigger | Copy |
|--------|---------|------|
| 13.4 | Unparseable threshold | `⚠️ Couldn't parse \`big\` as a power value. Try formats like \`250M\`, \`1.2B\`, or \`300,000,000\`.` |
| 13.5 | Blank zone | `⚠️ Provide a zone name (e.g. \`Power Tower\`).` |
| 13.6 | Duplicate rule (same `(rule_type, subject, value)`) | `⚠️ A matching rule already exists. Clear it first to update.` |
| 13.7 | Sheet write failure | `⚠️ Couldn't write to the Sheet (see logs for details).` |
| 13.8 | No Sheet configured | `⚠️ Your Google Sheet isn't configured. Run setup first.` |

All ephemeral.

### Screen 13.9 — Permission denial (any subcommand)

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to run this command.      │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 13.10 — `/desertstorm member_rule set_member_team`

```
/desertstorm member_rule set_member_team ▾
  team *        Team A or Team B  ▾  (Choice: Team A / Team B)
  member_user   Pick from the server (preferred — keys by Discord ID)
  member_name   OR a roster name if the member isn't on Discord
  notes         Optional free-text notes
```

`team` is a `Choice` dropdown so a typo'd team value is literally
unreachable via the slash UI.

Per Rule A / #166, the CS group also exposes this subcommand. CS's
`_set_member_team` checks the alliance's `teams` config: rejects
only when `teams=A` or `teams=B` (single-team CS), accepts when
`teams=both`.

### Screen 13.11 — `set_member_team` success (Discord picker)

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: Bob → plays Team B.                                        │
└──────────────────────────────────────────────────────────────────────┘
(public)
```

`_resolve_subject` stores `str(bob.id)` as the subject; the display
renders back as `bob.display_name`.

### Screen 13.12 — `set_member_team` success via `member_name`

For non-Discord roster members (e.g. `Charlie #42`):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: Charlie #42 → plays Team A.                                │
└──────────────────────────────────────────────────────────────────────┘
(public)
```

If `Charlie #42` HAD matched a Discord member's display name
case-insensitively, the subject would be normalised to the Discord
ID. Ambiguous matches keep the typed-name form.

### Screen 13.13 — `set_member_team` validation

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Provide a member. Pick from the typeahead (server member) OR      │
│ type a roster name (non-Discord member) — exactly one, not both.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Same message fires for: neither given, both given, or bot picked.

### Screen 13.14 — `set_member_team` on single-team CS rejection

When the CS alliance is configured `teams=A` or `teams=B`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Your Canyon Storm alliance is configured as single-team — Team   │
│ rules don't apply. Use `/canyonstorm member_rule set_member_zone`    │
│ for per-member zone overrides.                                       │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 13.15 — `/desertstorm member_rule set_member_zone`

```
/desertstorm member_rule set_member_zone ▾
  zone *        Zone they always play
  member_user   Pick from the server
  member_name   OR a roster name
  notes         Optional free-text notes
```

`zone` parameter ships with autocomplete (post-PR-180 audit-fix sweep).

### Screen 13.16 — `set_member_zone` success

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Saved: Alice → always at Power Tower.                             │
└──────────────────────────────────────────────────────────────────────┘
(public)
```

Non-canonical zone warning, blank-zone validation, subject errors
all share the same patterns as `set_power_band` / `set_member_team`.

### Screen 13.17 — `/desertstorm member_rule list`

```
/desertstorm member_rule list ▾
  member   Optional — filter to one member's rules
```

### Screen 13.18 — `list` empty state (#169 / Rule M inline buttons)

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Member Rules                                       │
│                                                                      │
│ *No member rules saved yet.*                                         │
└──────────────────────────────────────────────────────────────────────┘
[➕ Add rule]
```

Per #169, the empty state ships the `[➕ Add rule]` button so
officers can bootstrap without remembering subcommand names.

### Screen 13.18a — `[➕ Add rule]` opens the type picker

Per Rule E / #168 + #169:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ➕ Pick the rule type to add.                                        │
└──────────────────────────────────────────────────────────────────────┘
[⚡ Add a power-band rule]  [👤 Add a per-member rule]  [↩️ Cancel]
(ephemeral)
```

- `⚡ Add a power-band rule` → opens the same `InlinePowerBandView` the setup wizard uses (Screen 2.14c) — zone Select gates a one-field power modal.
- `👤 Add a per-member rule` → ephemeral pointer (per-member rules need a `discord.Member` picker, which Discord modals can't host):
  ```
  👤 Per-member rules need a server-member picker, which Discord
  doesn't expose inside a modal. Run one of:
  • `/desertstorm member_rule set_member_zone` — pin a member to a specific zone.
  • `/desertstorm member_rule set_member_team` — pin a member to Team A or Team B.
  ```

### Screen 13.19 — `list` with a mix of rules

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

Per Rule G / #167, the pre-existing `🎖️` row for `set_member_role`
rules is removed — rules render with `⚖️` (power-band) or `👤`
(per-member team/zone) only.

Notes:
- Rule numbering 1-based, matches Sheet row order (header excluded).
- Discord-ID subjects resolve via `resolve_subject_display` to the **current** display name (renames are picked up live).
- Optional `notes` cell renders as `↳ _<notes>_` italic follow-on.
- Names render bold in actual Discord markdown.
- `[➕ Add rule]` sits on row 4 alongside any pagination buttons.

### Screen 13.20 — `list` pagination (>20 rules)

Discord caps a View at 25 components. Page size: 20 Clear buttons +
Prev + Next = 22.

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Member Rules                                       │
│ … (20 rules) …                                                       │
│ Page 1/2                                                             │
└──────────────────────────────────────────────────────────────────────┘
[🗑 Clear 1]  …  [🗑 Clear 20]
[◀ Prev (disabled)]  [Next ▶]
```

Clear-button indices stay aligned with the master list — clicking
`🗑 Clear 27` deletes the 27th rule, not the 7th on this page.

Non-owner clicks paging buttons: `⛔ Only the command owner can paginate.`

### Screen 13.21 — Clear-rule click (happy path)

Click `[🗑 Clear 2]`: bot defers, runs `delete_rule_at` (atomic
`delete_rows`), reloads the list, rebuilds buttons, edits in place.
The displayed embed re-renders (rule 2 vanishes; rules 3+ slide up
by one). No followup ack — the in-place edit IS the ack.

### Screen 13.22 — Clear-rule click by non-owner

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the user who ran the command can clear rules from this list. │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 13.23 — Clear-rule click after Sheet write fails

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Couldn't remove that rule. Rerun the list command to refresh.     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 13.24 — `list` view timeout (5 min)

Every Clear + pagination button greys out in place. Re-run the list
command to get a fresh clickable view.

### Screen 13.25 — `list member:Alice` filter

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Desert Storm — Member Rules                                       │
│                                                                      │
│ ` 1` · 👤  Alice → always at Power Tower                             │
│      ↳ Tank role                                                     │
└──────────────────────────────────────────────────────────────────────┘
[🗑 Clear 1]
```

Power-band rules are filtered out (no member subject). The displayed
index resets — `1` here is internally `delete_rule_at(idx=0)` over
the filtered list.

### Screen 13.26 — CS group parity

The CS group is identical to DS for `set_power_band`,
`set_member_team` (with single-team check), `set_member_zone`, and
`list`. CS embeds say "Canyon Storm" in the title:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📋 Canyon Storm — Member Rules                                       │
│                                                                      │
│ ` 1` · ⚖️  ≥ 300M → eligible for Power Tower                         │
│ ` 2` · 👤  Alice → always at Power Tower                             │
│ ` 3` · 👤  Bob → plays Team A                                        │
└──────────────────────────────────────────────────────────────────────┘
[🗑 Clear 1]  [🗑 Clear 2]  [🗑 Clear 3]  [➕ Add rule]
```

### Flow at a glance

```
Officer runs slash command
  │
  ▼
┌─────────────────────────────────────────────────────────────────────┐
│ /<event> member_rule …                                              │
│                                                                     │
│   ├── set_power_band     → 13.1–13.8                                │
│   ├── set_member_team    → 13.10–13.14                              │
│   ├── set_member_zone    → 13.15–13.16 (+ shared validation)        │
│   └── list [member:filter]                                          │
│       → 13.17 → 13.18 / 13.19 / 13.20 / 13.25                       │
│       │                                                             │
│       ├── [➕ Add rule] → 13.18a type picker                        │
│       ├── [🗑 Clear N]  → 13.21 / 13.22 / 13.23                     │
│       ├── [◀ Prev/Next ▶] → 13.20 / non-owner denial                │
│       └── 5-min idle → 13.24 (view stripped)                        │
└─────────────────────────────────────────────────────────────────────┘
  │
  ▼
Sheet tab updated. Rules feed into Section 7 + 8 (roster builder + auto-fill).
```

---

## 14. Walkthrough tour

A first-run guided tour on `/desertstorm signups` **and**
`/canyonstorm signups` (per Rule N / #170 + Decision #12). The bot
offers it once per (guild, officer, walkthrough-key) tuple — clicking
either button records the dismissal so the offer never reappears.
The walkthrough key encodes a version (`storm_signups_v1`) so a
future UI rewrite can re-offer the tour. The version key is **shared
across DS and CS** — dismissing on one silences the other for that
officer.

Lives in [storm_walkthrough.py](../storm_walkthrough.py). Triggered
from `handle_storm_signups` (the entry point both DS and CS slash
commands route through) via `maybe_offer_storm_signups_tour(
interaction, event_type=..., teams=...)`. The `event_type` and
`teams` params let the tour copy branch on the actual UI the officer
will see — CS officers don't see "Desert Storm" pointers, and
single-team alliances don't see both team buttons mentioned in
Step 5.

### Screen 14.1 — First-time offer

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👋 First time opening the Desert Storm sign-ups view? Want a quick  │
│ walkthrough of what each piece does?                                 │
└──────────────────────────────────────────────────────────────────────┘
[👋 Walk me through this]  [No thanks]
(ephemeral)
```

Header branches on `event_type` — CS sees "Canyon Storm sign-ups
view". Per-officer dismissal is shared (`storm_signups_v1`).

Button styles: `[👋 Walk me through this]` success/green;
`[No thanks]` secondary/grey. Both record dismissal on click.

### Screen 14.2 — Offer clicked by non-owner

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ This walkthrough was offered to someone else.                     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 14.3 — Offer view timeout (5 min)

`on_timeout` strips the buttons in-place but **does not** record
dismissal — the next `/desertstorm signups` re-offers.

### Screen 14.4 — `[No thanks]` clicked

```
┌──────────────────────────────────────────────────────────────────────┐
│ 👍 Got it — won't ask again. Run `/help` any time and pick Desert    │
│ Storm or Canyon Storm if you want a refresher.                       │
└──────────────────────────────────────────────────────────────────────┘
[👋 Walk me through this (disabled)]  [No thanks (disabled)]
```

Dismissal recorded before the edit fires.

### Screen 14.5 — `[👋 Walk me through this]` clicked

Offer view stops + dismisses + edits to `✅ Starting the tour…`.
A new ephemeral followup with Step 1 sends immediately.

### Screens 14.6–14.11 — Tour Steps 1–6

Each step is a fresh ephemeral followup with `[Next →]` and
`[Skip the rest]` buttons (or `[Close]` on Step 6).

**Step 1 / 6 — The buckets:**

```
The embed groups everyone by their current vote: 🅰️ Team A,
🅱️ Team B, 🔄 Either, ❌ Cannot, and ❓ Not voted yet. The counter
in the title tells you the total members you're tracking.
```

**Step 2 / 6 — Who's already assigned:**

```
Members already slotted into a roster for this event render with
strikethrough. That way you can scan at a glance for who's left to
place when you're building out a team.
```

**Step 3 / 6 — Members who aren't on Discord:**

```
If your roster Sheet flags a row with `not_on_discord`, that member
surfaces in the buckets just like everyone else (marked with ¹).
They won't vote themselves — you cast their vote with 🙋 Record
on-behalf vote, and the bot logs that you recorded it.
```

**Step 4 / 6 — Recording on-behalf votes** (rewritten per Rule E /
#168 + Decision #12 for the post-#168 ephemeral picker — no more
"type the name, typos rejected" framing):

```
Click 🙋 Record on-behalf vote to open the picker. Pick the member
from the dropdown (sourced from your roster Sheet — no free typing,
so typos can't slip through), pick a vote (the options match the
team buttons members see on the sign-up post), then hit Submit.
Each on-behalf vote captures your Discord ID for audit.
```

**Step 5 / 6 — Setting up a team** (branches on `cfg.teams`):

- `teams=both` (DS or CS):
  ```
  When you're ready to build a Desert Storm roster, click 🅰️ Set up
  Team A or 🅱️ Set up Team B. The bot will ask which preset to use,
  then open the roster builder pre-filtered to members who signed up,
  with eligibility minimums enforced.
  ```
- `teams=A`: same wording but only mentions `🅰️ Set up Team A`.
- `teams=B`: same wording but only mentions `🅱️ Set up Team B`.

CS variant swaps "Desert Storm" for "Canyon Storm".

**Step 6 / 6 — That's the tour** (`is_last=True`; single
`[Close]` button, `ButtonStyle.success`):

```
You can run `/help` any time and pick Desert Storm from the dropdown
to revisit the command list. Closing this message drops you back to
the live officer view.
```

CS variant pointer at "Canyon Storm" instead of "Desert Storm".

### Screen 14.12 — `[Close]` clicked on Step 6

View stops, button greys out in place, body text stays as-is. Tour
quietly ends.

### Screen 14.13 — `[Skip the rest]` clicked mid-tour

View stops, button greys out, current step's body is appended with
`_(tour skipped)_`. Dismissal was already recorded when the offer
was accepted — the tour won't re-offer next time either.

### Screen 14.14 — Tour-step click by non-owner

Same denial as 14.2.

### Screen 14.15 — Tour-step view timeout (10 min)

Current step's buttons grey out in place. Offer was already
accepted, so `/desertstorm signups` won't re-offer.

### Screen 14.16 — Second-time `/desertstorm signups`

`config.is_walkthrough_dismissed(...)` returns True;
`maybe_offer_storm_signups_tour` returns early with no message. The
officer view renders normally with no tour ephemeral attached.

### Screen 14.17 — Different officer's first time

Independent dismissal state per officer — Alice's first run still
gets the offer even if Kevin already dismissed it.

### Flow at a glance

```
Officer runs /<event> signups
  │
  ▼
maybe_offer_storm_signups_tour(event_type, teams)
  │
  ├── dismissed?  → no offer (14.16)
  │
  ▼ first time
   14.1 Offer ephemeral
       │
       ├── [No thanks]               → 14.4 dismissed
       │
       └── [👋 Walk me through this] → 14.5 → Steps 1-6 (14.6-14.11)
              │
              ├── [Next →] each step → advance
              ├── [Skip the rest]    → 14.13 step text + skipped marker
              ├── [Close] on Step 6  → 14.12 view greyed
              ├── 10-min idle        → 14.15 step view greyed
              └── non-owner click    → 14.2 / 14.14 denial
  │
  ▼
Dismissal recorded (storm_signups_v1, shared DS/CS).
```

---

## 15. History browser

`/desertstorm strategy roster_history [date]` /
`/canyonstorm strategy roster_history [date]` browse the historical
structured-roster archive. Data comes from `rosters_tab` (written by
Approve & Post in Section 9) joined with `attendance_tab` (written
by Section 10).

Lives in [storm_history.py](../storm_history.py). Read-only —
corrections route through re-running the roster builder and
re-recording attendance.

Both subcommands are leadership-gated AND require Premium + the
structured flow turned on.

### Screen 15.1 — Slash help

```
/desertstorm strategy roster_history ▾
  date   Optional — show a specific date (May 18, 5/18, 2026-05-18,
         yesterday). Omit to list recent events.
```

### Screens 15.2–15.5 — Gates

| Screen | Trigger | Copy |
|--------|---------|------|
| 15.2 | Non-leader | `⛔ You need the leadership role (or admin) to run this command.` |
| 15.3 | Free tier | `🔒 \`/desertstorm strategy roster_history\` is a 💎 Premium feature. Run \`/upgrade\` to unlock it.` |
| 15.4 | Structured flow off | `⚠️ The structured roster flow isn't enabled for Desert Storm. Run \`/setup → ⚔️ Desert Storm\` and turn on Structured Roster Flow first.` |
| 15.5 | DM | `⚠️ This command must be used inside a server.` |

### Screen 15.6 — No date — recent-events list

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm — Recent Rosters                                     │
│                                                                      │
│ Click a date below to view the roster + attendance.                  │
└──────────────────────────────────────────────────────────────────────┘
[Fri May 15]  [Fri May 8]  [Fri May 1]  [Fri Apr 24]  [Fri Apr 17]
(ephemeral)
```

Up to 8 buttons (one per recent date, descending). Style:
`secondary`. View timeout: 300 sec; owner-guarded.

### Screen 15.7 — No date — empty archive

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm — Recent Rosters                                     │
│                                                                      │
│ No structured rosters posted yet. Use `/desertstorm signups` to     │
│ build a roster + Approve & Post, and it'll show up here.            │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral; no view attached)
```

### Screen 15.8 — Date-button clicked (happy path)

Bot defers, fires `load_event_roster` + `load_event_attendance` in
parallel, joins them, sends a new ephemeral followup:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm Roster — Friday, May 15, 2026                       │
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
(ephemeral)
```

Notes:
- Title uses `format_event_date` long-form.
- Color: `dark_gold` (DS) / `dark_orange` (CS).
- Rows group by team → zone; alphabetised for stability.
- Status glyphs: ✅ attended / ❌ no_show / — unrecorded.
- **Rule K / #171:** 🔄 Sub activated dropped from UI. Legacy `sub_activated` rows render as `—` and don't count toward `recorded`.
- **Decision #6 / #171:** the pre-existing `⚠️ override` flag is removed from history rendering. The `Override Below Minimum` column (renamed from `Override Below Floor` per Rule M / #160) survives on the Sheet for post-event audit but history doesn't surface it.
- Power renders via `_format_power_display(...)` — `"412000000"` → ` — 412M`. Sentinel `"unknown"` and blanks render as empty.
- Sub slots labelled: paired-mode `(sub, paired with <primary>)`; pool-mode `(sub)`. Primary slots get no marker.
- Footer: `Attendance: ✅ N · ❌ N · (recorded N of M slots)`.

**Phase-aware variant (Rule L / #172):** when the rostered event was
built from a phase-aware preset, each zone breaks into a header +
per-phase sub-rows:

```
│ ── Team A ────────────────────────────────────────────────────────── │
│ __Power Tower__                                                      │
│    └ **Phase 1**                                                     │
│ ✅ Alice — 412M                                                      │
│ ✅ Bob — 380M                                                        │
│    └ **Phase 2**                                                     │
│ ✅ Carol — 350M                                                      │
│ ❌ Dan — 320M                                                        │
│    └ **Phase 3**                                                     │
│ ✅ Erin — 305M                                                       │
```

Sub-pool rows render flat regardless of phase-awareness (event-level,
not phase-scoped).

The original list view (Screen 15.6) **stays alive** — Kevin can hop
to other dates without re-running the command. Each click sends a
fresh ephemeral followup.

### Screen 15.9 — Date-button clicked — attendance not yet recorded

Every slot glyph is `—`. Footer changes:

```
│ Attendance not yet recorded. Run /desertstorm attendance to add it. │
```

### Screen 15.10 — Date-button clicked — no roster on that date

Defensive empty state (date appeared in `list_event_dates` but
`load_event_roster` returned zero slots):

```
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm Roster — Friday, May 1, 2026                        │
│                                                                      │
│ _No structured roster found for this date. Check the date format or  │
│ run /desertstorm signups + Approve & Post to build a roster for this │
│ event._                                                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral; footer omitted)
```

### Screen 15.11 — Date-button clicked by non-owner

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ Only the officer who opened this view can switch dates.           │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 15.12 — List view timeout (5 min)

Every date button greys out in place. Per-event detail ephemerals
already opened remain on screen (no buttons, no timeout).

### Screen 15.13 — `date` provided — direct happy path

```
/desertstorm strategy roster_history date:2026-05-15
```

Same detail embed as Screen 15.8, no list view. Accepted formats:
`May 15`, `5/15`, `5-15-2026`, `May 15th`, `15 May 2026`,
`Friday`, `yesterday`, etc. Permissive parser shared with every
date-accepting slash command.

### Screen 15.14 — `date` unparseable

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ `next tuseday` isn't a date I can parse. Try `May 18`, `5/18`,    │
│ `2026-05-18`, or `yesterday`.                                        │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

### Screen 15.15 — `date` parses but no roster

Same empty-state copy as 15.10.

### Screen 15.16 — `date` provided — soft Sheet read errors

```
⚠️ Read had soft errors — see bot logs.
┌──────────────────────────────────────────────────────────────────────┐
│ 📜 Desert Storm Roster — Friday, May 15, 2026                       │
│ … (partial roster as far as the read got) …                          │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

Warning is the message `content` above the embed; body is still
rendered as far as the read got. Bot-side log:
`[STORM HISTORY] roster read errors guild=… date=…: <error 1>; <error 2>`.

### Screen 15.17 — Large roster truncation

A 30+ slot roster blows Discord's 1024-char field limit on the
per-team field. The renderer trims at ~980 chars on the nearest
newline boundary and appends:

```
…trimmed; see Sheet for full list
```

### Screen 15.18 — Canyon Storm parity

CS uses the same machinery with three differences:
- Title says "Canyon Storm Roster — …"
- Embed color is `dark_orange`
- Team grouping respects `cfg.teams` per Rule A / #166: `teams=both` renders `── Team A ──` / `── Team B ──` (same as DS); `teams=A` / `teams=B` renders a single `── Roster ──` header.

### Flow at a glance

```
Officer runs /<event> strategy roster_history
  │
  ▼
storm_history.open_history(interaction, event_type, event_date)
  │
  ├── not leader/admin → 15.2
  ├── no guild         → 15.5
  ├── not Premium      → 15.3
  ├── flow not enabled → 15.4
  │
  ▼  defer ephemeral, thinking
  │
  ├──  event_date provided?
  │      │
  │      ├── unparseable  → 15.14
  │      │
  │      ▼  parse to ISO
  │      load_event_roster + load_event_attendance (parallel)
  │      render_event_embed
  │      │
  │      ├── slots empty   → 15.15 / 15.10
  │      ├── soft errors   → 15.16
  │      ├── attendance ∅  → 15.9
  │      └── happy         → 15.13
  │
  └──  event_date omitted
         │
         ▼  list_event_dates (top 8 desc)
         render_history_list_embed
         │
         ├── no dates → 15.7 empty (no view)
         │
         └── ≥1 date → 15.6 list embed + _HistoryListView
               │
               ├── owner clicks date → 15.8 detail (followup; list stays alive)
               ├── non-owner clicks  → 15.11 denial
               └── 5-min idle        → 15.12 buttons greyed
```

---

