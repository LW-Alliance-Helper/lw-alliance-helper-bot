# Storm Setup Wizard — UX Walkthrough

Screen-by-screen approximation of every user-facing surface in the
Desert Storm + Canyon Storm setup wizard (`run_storm_setup` in
[setup_cog.py](../setup_cog.py), entry point `_launch_storm_setup`
called by the `/setup` hub buttons).

Both event types share the same wizard chassis. The doc walks the DS
form; the CS form is identical with `event_type="CS"`, `label="Canyon
Storm"`, and the icon swapped from ⚔️ to 🏜️. Branching that is
genuinely event-type-aware (e.g. game-defined day-of-week constants
in the auto-schedule sub-flow) is called out inline.

Read this end-to-end to catch awkward wording, missing affordances,
dead-end refs, and event-type leaks before testers do.

## Example values

Placeholders below are filled in with realistic values so each screen
reads the way a real officer would experience it.

- Alliance: **Apex**
- Officer: **Kevin** (admin) — runs the wizard
- Leadership role: `@Leadership` (configured during the foundation
  `/setup` wizard before the storm wizard is reachable)
- Wizard channel: `#leadership-config`
- Existing channels: `#storm-log`, `#desert-storm`, `#storm-signups`
- Existing roster Sheet: `Apex Alliance Roster` with tabs `Squad
  Powers`, `Birthdays`, `DS Assignments`
- Roster power column: column **B** (`1st Squad Power`)

ASCII boxes approximate what the user sees in Discord (embed boundary
+ body + fields + footer). Buttons appear on the line below the box,
ordered as they render in the Discord client. Ephemeral messages are
labelled `(ephemeral — only Kevin sees it)`. The wizard runs in the
channel where Kevin ran `/setup` (here `#leadership-config`); the
slash-command interaction itself just acks ephemerally.

## Table of contents

0. Entry — slash ack + permission denial
1. Re-entry summary (only on re-run)
2. Wizard banner
3. Step 1 — Sheet Tab
4. Step 2 — Teams
5. Step 3 — Storm Log Channel
6. Step 4 — Mail Post Channel
7. Step 5 — Mail Template(s)
8. Step 6 — Participation Tracking
9. Structured Roster Flow sub-step (Premium)
10. Strategy Presets tab + inline-create offer
11. Member Rules tab + inline-create offer
12. Step 7 — Reminder DM
13. Save + done
14. Cancel + timeout paths
15. Known UX gaps (post-#230)

---

## 0. Entry — slash ack + permission denial

Kevin opens `/setup` and clicks `⚔️ Desert Storm` (or `🏜️ Canyon
Storm`). Discord acks the button click ephemerally; every wizard
prompt that follows is a regular channel message in
`#leadership-config`.

### Screen 0.1 — Slash-command ephemeral ack

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚙️ Starting Desert Storm setup — check the channel for prompts!     │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral — only Kevin sees it)
```

*(CS variant: `⚙️ Starting Canyon Storm setup — check the channel for
prompts!`)*

### Screen 0.2 — Permission denial

If the user has neither admin nor the configured Leadership role
(`_has_leadership_or_admin`):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⛔ You need the leadership role (or admin) to open the Desert Storm  │
│ wizard.                                                              │
└──────────────────────────────────────────────────────────────────────┘
(ephemeral)
```

The wizard never starts. `_check_wizard_can_run` also fires here and
blocks if the channel lacks the bot perms needed to drive the wizard
(`view_channel`, `send_messages`, `embed_links`,
`read_message_history`).

---

## 1. Re-entry summary (only on re-run)

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
│ ✅ Enabled · Power column: `B` · Sub mode: pool                      │
└──────────────────────────────────────────────────────────────────────┘
[✏️ Edit settings]  [✅ No changes needed]
```

CS variant: title reads `🏜️ Current Canyon Storm Setup`. Per Rule A /
#166 the `Teams` field is included for CS too.

If Kevin clicks `✅ No changes needed`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ No changes made. Your Desert Storm setup is still active.        │
└──────────────────────────────────────────────────────────────────────┘
```

Wizard exits. Otherwise the full wizard runs.

**First-time setup (no saved row):** this screen is skipped entirely;
the wizard jumps straight to the banner below.

---

## 2. Wizard banner

Posted once before Step 1. Same line for first-run and re-entry.

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚙️ Desert Storm Setup                                                │
└──────────────────────────────────────────────────────────────────────┘
```

*(CS: `⚙️ Canyon Storm Setup`)*

---

## 3. Step 1 — Sheet Tab

Backed by `ask_keep_or_change` — three-button view on re-entry (Keep
current / Use default / Define my own), two-button view on first run
(Use default / Define my own).

### First-time variant

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 1 of 7: Sheet Tab                                               │
│ Which tab in your Google Sheet stores the Desert Storm zone          │
│ assignments?                                                         │
│ ⚠️ Make sure this tab exists in your sheet before continuing.       │
│ ℹ️ The bot will manage the data structure of this tab automatically. │
│ you don't need to set up any specific columns or formatting          │
│ beforehand.                                                          │
└──────────────────────────────────────────────────────────────────────┘
[↩️ Use default: DS Assignments]  [✏️ Define my own]
```

`✏️ Define my own` opens a modal:

```
┌─ Sheet Tab Name ─────────────────────────────┐
│ Tab name                                     │
│ [                                         ]  │
│                                              │
│                              [Submit]        │
└──────────────────────────────────────────────┘
```

### Re-entry variant

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 1 of 7: Sheet Tab                                               │
│ Which tab in your Google Sheet stores the Desert Storm zone          │
│ assignments?                                                         │
│ ⚠️ Make sure this tab exists in your sheet before continuing.       │
│ ℹ️ The bot will manage the data structure of this tab automatically. │
│ you don't need to set up any specific columns or formatting          │
│ beforehand.                                                          │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: DS Assignments]  [↩️ Use default: DS Assignments]  [✏️ Define my own]
```

(If the saved value happens to equal the default, `ask_keep_or_change`
collapses to a single Keep current button; this is a deliberate
de-dup.)

*(CS: default tab name is `CS Assignments`.)*

---

## 4. Step 2 — Teams

`TeamChoiceView` — three primary buttons + an optional Keep current
button on re-entry.

### First-time variant

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 2 of 7: Which teams do you run for Desert Storm?               │
└──────────────────────────────────────────────────────────────────────┘
[Team A & Team B]  [Team A only]  [Team B only]
```

### Re-entry variant

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 2 of 7: Which teams do you run for Desert Storm?               │
│ Current: Team A & Team B                                             │
└──────────────────────────────────────────────────────────────────────┘
[Team A & Team B]  [Team A only]  [Team B only]  [Keep current]
```

After click (example: Team A & Team B):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Teams: Team A & Team B                                            │
└──────────────────────────────────────────────────────────────────────┘
```

`Teams: A only / B only` are written to `guild_storm_config.teams`
verbatim. CS reads `teams` the same way after #166 (CS-only-A and
CS-only-B alliances exist).

---

## 5. Step 3 — Storm Log Channel

`ChannelSelectStep` — Discord channel select with optional Keep
current button on re-entry, plus a "➕ Create channel" button (always
on; Premium tier expands the create flow with thread support).

### First-time variant

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 3 of 7: Storm Log Channel                                       │
│ Select the channel where Desert Storm participation/log summaries    │
│ will be posted:                                                      │
└──────────────────────────────────────────────────────────────────────┘
[ChannelSelect: Select the Desert Storm log channel...]
[➕ Create channel]
```

### Re-entry variant

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 3 of 7: Storm Log Channel                                       │
│ Select the channel where Desert Storm participation/log summaries    │
│ will be posted:                                                      │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: #storm-log]
[ChannelSelect: Select the Desert Storm log channel...]
[➕ Create channel]
```

### Stale-channel variant

If the saved channel ID no longer resolves (deleted in Discord):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ Your previously configured Desert Storm log channel no longer    │
│ exists. Pick a new one below.                                        │
└──────────────────────────────────────────────────────────────────────┘
```

…followed by the first-time picker (no Keep current button — there's
nothing to keep).

---

## 6. Step 4 — Mail Post Channel

Same shape as Step 3 (`ChannelSelectStep`). Suggested-name default is
`desert-storm` (`canyon-storm` for CS).

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 4 of 7: Mail Post Channel                                       │
│ When leadership clicks Post & Copy at the end of                     │
│ /desertstorm_draft, the finished mail will be posted to this         │
│ channel:                                                             │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: #desert-storm]
[ChannelSelect: Select the Desert Storm mail post channel...]
[➕ Create channel]
```

*(CS: copy reads "When leadership clicks Post & Copy at the end of
`/canyonstorm_draft`…")*

---

## 7. Step 5 — Mail Template(s)

Branches on Step 2's `teams` choice.

### 7a. Teams = A only or B only

Skip the shared/separate question entirely; go straight to
`get_template(team_label)`.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 5 of 7: Mail Template                                           │
└──────────────────────────────────────────────────────────────────────┘
```

Then for the single team (label is `Team A` or `Team B`):

```
┌──────────────────────────────────────────────────────────────────────┐
│ Desert Storm Mail Template: Team A                                   │
│ When you draft the mail each week, you will be able to select the    │
│ time slot when you are running that team's Desert Storm.             │
│                                                                      │
│ Here is the default template:                                        │
│ ```                                                                  │
│ Hello {alliance_name}!                                               │
│                                                                      │
│ Desert Storm assignments for {time}:                                 │
│ {zones}                                                              │
│                                                                      │
│ Substitutes:                                                         │
│ {subs}                                                               │
│ ```                                                                  │
│ Would you like to use this or edit it?                               │
└──────────────────────────────────────────────────────────────────────┘
[✅ Use default template]  [✏️ Edit template]
```

`✅ Use default template` → confirmation line, advance:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Using default template for Team A.                               │
└──────────────────────────────────────────────────────────────────────┘
```

`✏️ Edit template` → typed-reply prompt:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Paste your custom template for Team A. You can copy the default      │
│ above and modify it, or write your own.                              │
│                                                                      │
│ Available placeholders:                                              │
│ • {alliance_name}: your alliance name                                │
│ • {zones}: zone assignments block                                    │
│ • {subs}: substitute members                                         │
│ • {time}: event time (auto-filled when drafting)                     │
│                                                                      │
│ This form will time out in 5 minutes. You can run                    │
│ /setup_desertstorm again if it times out.                            │
└──────────────────────────────────────────────────────────────────────┘
(Kevin types the body as a regular channel message)
```

### 7b. Teams = both (Team A & B)

`SharedTemplateView` runs first:

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 5 of 7: Mail Template                                           │
│ Do you want one template that applies to both teams, or separate     │
│ templates per team?                                                  │
└──────────────────────────────────────────────────────────────────────┘
[One template for both teams]  [Separate templates per team]
```

(Note: this view has **no Keep current button** on re-entry — tracked
as #231. If Kevin runs the wizard again, he has to re-pick the
shared/separate choice.)

After `One template for both teams`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ One shared template for Team A & B                                │
└──────────────────────────────────────────────────────────────────────┘
```

Then a single `get_template("Team A & B")` round runs.

After `Separate templates per team`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Separate templates for Team A & Team B                            │
└──────────────────────────────────────────────────────────────────────┘
```

Then two `get_template` rounds — first `Team A`, then `Team B`.

*(CS: every `Desert Storm` ↔ `Canyon Storm` text swap; same flow.)*

---

## 8. Step 6 — Participation Tracking

Six sub-steps gated behind a Yes/No enable.

### 8.1 Enable participation tracking

#### First-time variant

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6 of 7: Participation Tracking                                  │
│ Do you want to track Desert Storm participation? Leadership runs     │
│ /desertstorm_participation after each event to log who showed up,    │
│ who sat out, etc.                                                    │
│ You'll define the questions yourself, so the tracker matches how     │
│ your alliance runs the event.                                        │
└──────────────────────────────────────────────────────────────────────┘
[Yes]  [No]
```

#### Re-entry variant (post-#230)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6 of 7: Participation Tracking                                  │
│ Do you want to track Desert Storm participation? …                   │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: Yes]  [↩️ Switch to: No]
```

(or `Keep current: No` / `Switch to: Yes` if previously disabled).

If `No` (or `Keep current: No`):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Keeping No                                                        │
└──────────────────────────────────────────────────────────────────────┘
```

Participation block exits with `enabled=0`; wizard advances to the
structured-flow sub-step.

If `Yes`, continue with the steps below.

### 8.2 Participation sheet tab

Backed by `ask_keep_or_change`. Default: `DS Participation Log` (or
`CS Participation Log`).

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6.1: Participation Sheet Tab                                    │
│ Which tab should the bot write Desert Storm participation rows to?   │
│ ℹ️ The bot will create this tab automatically if it doesn't exist   │
│ and will manage the column structure based on the questions you      │
│ define.                                                              │
└──────────────────────────────────────────────────────────────────────┘
[↩️ Use default: DS Participation Log]  [✏️ Define my own]
```

(Re-entry shows the `✅ Keep current: <tab>` button as the first
option per the `ask_keep_or_change` 3-button pattern.)

### 8.3 Roster source: sheet tab

Smart current suggestion — prefers a previously-saved roster source,
else falls back to the survey stats tab, else the birthday tab. The
hardcoded default is `Squad Powers`.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6.2: Roster Source: Sheet Tab                                   │
│ Which tab in your sheet has the list of members? The bot reads       │
│ member names from here when you use a Roster names question.         │
│ Tip: this is often the same tab you use for /setup → 📋 Survey or   │
│ /setup → 🎂 Birthdays.                                              │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: Squad Powers]  [↩️ Use default: Squad Powers]  [✏️ Define my own]
```

### 8.4 Roster source: name column

`ask_keep_or_change` — modal for a single column letter. Default: `A`.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6.3: Roster Source: Name Column                                 │
│ Which column letter has the member name? (e.g. A, B, E)              │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: A]  [↩️ Use default: A]  [✏️ Define my own]
```

If the typed value isn't a valid column letter:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ `Foo` isn't a valid column letter. Run                           │
│ /setup_desertstorm to start again.                                   │
└──────────────────────────────────────────────────────────────────────┘
```

Wizard aborts. (Note: no in-place retry — this is a known sharp edge
of the wizard.)

### 8.5 Roster source: alias column Y/N

#### First-time variant

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6.4: Roster Source: Alias Column?                               │
│ If you have other names or nicknames that you call your members in   │
│ these mails, this helps resolve to their full name in your sheet     │
│ automatically. Do you have an alias column?                          │
└──────────────────────────────────────────────────────────────────────┘
[Yes]  [No]
```

#### Re-entry variant (post-#230)

When the previous save carried an alias decision (column index ≥ 0 OR
explicit -1):

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6.4: Roster Source: Alias Column?                               │
│ ...                                                                  │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: Yes]  [↩️ Switch to: No]
```

If `Yes`, the wizard descends into Step 6.5 to ask the column letter.

### 8.6 Roster source: alias column letter (only when 8.5 = Yes)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Alias Column                                                         │
│ Which column letter has the alias / nickname?                        │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: B]  [↩️ Use default: B]  [✏️ Define my own]
```

Default is the column right after the name column (B if name was A).

### 8.7 First data row

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6.5: Roster Source: First Data Row                              │
│ In your existing roster tab above, which row does the member data    │
│ start on? Usually 2 if your sheet has a header row in row 1.         │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: 2]  [↩️ Use default: 2]  [✏️ Define my own]
```

### 8.8 Questions builder

`_BuilderView` — Add / Edit existing / Remove existing / Done. The
participation log will ask one Discord step per question after the
event.

#### First-time / no questions yet

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6.6: Participation Questions                                    │
│ Each question becomes a column on your sheet and a step in the       │
│ /desertstorm_participation flow.                                     │
│ Examples: Vote count, Sitting out, Did anyone show up late?          │
│ Free tier limit: 3 questions.                                        │
│                                                                      │
│ (no questions yet; every participation log will only ask for         │
│ the date)                                                            │
└──────────────────────────────────────────────────────────────────────┘
[➕ Add question]  [✅ Done]
```

Premium replaces the cap line with:

```
💎 Premium: unlimited questions and three extra question types.
```

#### Has questions (example: 2 already saved)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 6.6: Participation Questions                                    │
│ Each question becomes a column on your sheet and a step in the       │
│ /desertstorm_participation flow.                                     │
│ Examples: Vote count, Sitting out, Did anyone show up late?          │
│ Free tier limit: 3 questions.                                        │
│                                                                      │
│ 1. Vote count: Numeric (0–500)                                      │
│ 2. Sitting out: Yes/No                                              │
└──────────────────────────────────────────────────────────────────────┘
[Select: ✏️ Edit a question…]
[Select: 🗑️ Remove a question…]
[➕ Add question]  [✅ Done]
```

`➕ Add question` and `✏️ Edit a question…` both descend into
`_build_participation_question` (multi-step: Label → Type → optional
extras like Numeric bounds or Roster names tab).

`🗑️ Remove` posts a confirmation line and loops back to the builder:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 🗑️ Removed: Sitting out                                             │
└──────────────────────────────────────────────────────────────────────┘
```

`✅ Done` exits the builder and continues to the structured-flow
sub-step.

Free-tier cap hit when trying to add a 4th question:

```
┌──────────────────────────────────────────────────────────────────────┐
│ 💎 Participation Questions                                           │
│ You've hit the free-tier cap (3 questions). Upgrade to Premium to    │
│ define unlimited questions and three extra question types.           │
└──────────────────────────────────────────────────────────────────────┘
[💎 Upgrade]
```

---

## 9. Structured Roster Flow sub-step (Premium)

`_run_structured_flow_setup_step`. Runs after Step 6, before Step 7.

### 9.1 Free-tier short-circuit

Non-Premium alliances skip the Premium opt-in entirely and land on
the always-asked Strategy Presets / Member Rules tab steps (Section
10 + 11). The structured-flow gate is only shown when `is_premium`.

### 9.2 Premium explainer

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

### 9.3 Opt-in Yes/No

#### First-time variant

```
┌──────────────────────────────────────────────────────────────────────┐
│ Turn on the structured flow for Desert Storm?                        │
└──────────────────────────────────────────────────────────────────────┘
[Yes]  [No]
```

#### Re-entry variant (post-#230)

When the alliance has a saved storm config (i.e., `has_storm_config`
returns True):

```
┌──────────────────────────────────────────────────────────────────────┐
│ Turn on the structured flow for Desert Storm?                        │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: Yes]  [↩️ Switch to: No]
```

If `No` (or `Keep current: No`), the rest of the structured-flow
config is skipped — wizard jumps to Section 10 (preset library tab
names, asked for everyone).

If `Yes`, continue with the steps below.

### 9.4 Power metric column

```
┌──────────────────────────────────────────────────────────────────────┐
│ Power Metric Column                                                  │
│ Which column on your roster Sheet stores the power value the bot     │
│ should use to gate Desert Storm zone eligibility? Enter a single     │
│ column letter (A–Z); the bot reads that column at render time, so    │
│ renaming the Sheet header later won't break anything.                │
└──────────────────────────────────────────────────────────────────────┘
[↩️ Use default: B]  [✏️ Define my own]
```

(Re-entry adds `✅ Keep current: B` per `ask_keep_or_change`.)

### 9.5 Sub mode

Two buttons; labels rewrite based on the saved value so the green
button always reads "Use Current" / "Use Default" depending on
context.

#### First-time variant (no saved sub_mode)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Sub Mode                                                             │
│ How should subs be tracked when leadership builds a roster?          │
│ • Pool: flat list of subs; any sub can cover any primary no-show.   │
│ • Paired: each primary has a specific sub assigned in advance.      │
└──────────────────────────────────────────────────────────────────────┘
[Use Default: Pool]  [Paired: primary↔sub pairs]
```

#### Re-entry, saved mode = Pool

```
[Use Current: Pool]  [Paired: primary↔sub pairs]
```

#### Re-entry, saved mode = Paired

```
[Pool: flat sub list]  [Use Current: Paired]
```

After click:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Sub mode: Pool                                                    │
└──────────────────────────────────────────────────────────────────────┘
```

### 9.6 Sign-up channel

`ChannelSelectStep` — same shape as Step 3 / 4. Suggested-name
default is `desertstorm-signups` (or `canyonstorm-signups`).

```
┌──────────────────────────────────────────────────────────────────────┐
│ Desert Storm Sign-Up Channel                                         │
│ The bot will auto-post a sign-up poll here each week. Members click  │
│ buttons to register their availability.                              │
│ You can open the officer view via /desertstorm signups.              │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: #storm-signups]
[ChannelSelect: Select the channel where Desert Storm sign-up polls post...]
[➕ Create channel]
```

### 9.7 Auto-schedule sub-flow — poll day

`_DowView` (post-#230: includes a Keep current button).

Event day is game-defined: DS event runs Friday (roster locks
Wednesday); CS event runs Thursday (roster locks Monday). The dropdown
only shows poll days that sit between the previous event and the
in-game roster lock.

#### First-time variant

```
┌──────────────────────────────────────────────────────────────────────┐
│ Auto-Schedule: Poll Day (💎 Premium)                                 │
│ Desert Storm runs every Friday in-game. Which day do you want the    │
│ bot to post the sign-up poll? (The dropdown shows only days that     │
│ sit between the previous event and the in-game roster lock.)        │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: Skip auto-scheduling]
[Select: When should the bot post the sign-up poll?]
```

Dropdown options for DS: `Saturday`, `Sunday`, `Monday`, `Tuesday`,
`Wednesday`, plus `Skip auto-scheduling (post manually from the hub)`.

For CS: `Friday`, `Saturday`, `Sunday`, `Monday`, plus the same Skip
row.

#### Re-entry, saved poll day = Sunday

```
[✅ Keep current: Sunday]
[Select: When should the bot post the sign-up poll?]
```

#### After click

If user picked `Saturday`:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Poll day: Saturday.                                               │
└──────────────────────────────────────────────────────────────────────┘
```

If user picked `Skip auto-scheduling` (or clicked Keep current and the
saved value was Skip):

```
┌──────────────────────────────────────────────────────────────────────┐
│ ✅ Auto-scheduling skipped. Post manually via /desertstorm signups   │
│ → Post sign-up poll when you're ready.                               │
└──────────────────────────────────────────────────────────────────────┘
```

(or the `Auto-scheduling stays skipped` line if Keep current was the
chosen path.)

### 9.8 Auto-schedule sub-flow — sign-up time

Only asked when a real poll day was chosen (skip path returns
immediately). `ask_keep_or_change` modal — 12-hour clock.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Auto-Schedule: Sign-Up Post Time                                     │
│ What time should the bot fire the sign-up post? (in your timezone:   │
│ (UTC-5) Eastern (New York, Toronto, Miami))                          │
│ (e.g. 2:00pm, 9:00am, or 24-hour 14:00)                              │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: 12:00pm]  [↩️ Use default: 12:00pm]  [✏️ Define my own]
```

Blank submit → re-prompt:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⚠️ A sign-up time is required when auto-scheduling is on. Pick a    │
│ time (e.g. 12:00pm) or use the default.                              │
└──────────────────────────────────────────────────────────────────────┘
```

Three blank attempts → wizard accepts `12:00pm` automatically and
continues.

### 9.9 Sign-ups / Rosters / Attendance tab names

Three separate `ask_keep_or_change` rounds. Defaults are event-type
aware (e.g. `DS Sign-Ups`, `DS Rosters`, `DS Attendance`).

```
┌──────────────────────────────────────────────────────────────────────┐
│ Sign-Ups Tab                                                         │
│ Which Google Sheet tab should store Desert Storm sign-ups? The bot   │
│ creates and maintains this tab.                                      │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: DS Sign-Ups]  [↩️ Use default: DS Sign-Ups]  [✏️ Define my own]
```

(Same shape for Rosters and Attendance — the only difference is the
label and the default.)

### 9.10 Power-refresh DM

#### First-time variant

```
┌──────────────────────────────────────────────────────────────────────┐
│ Power-Refresh DM (💎 Premium)                                        │
│ When a member clicks a sign-up button for Desert Storm and their     │
│ power value (Column B on the roster Sheet) is blank or               │
│ unparseable, should the bot DM them a one-line nudge to update it?   │
│ At most one DM per member per event date.                            │
└──────────────────────────────────────────────────────────────────────┘
[Yes]  [No]
```

#### Re-entry variant (`_KeepOrFlipYesNoGate`, pre-#230 behaviour)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Power-Refresh DM (💎 Premium)                                        │
│ When a member clicks a sign-up button for Desert Storm and their     │
│ power value (Column B on the roster Sheet) is blank or               │
│ unparseable, the bot can DM them a one-line nudge to update it.      │
│ Currently on. Keep it or flip.                                       │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: Yes]  [↩️ Switch to: No]
```

(`currently off` / `Keep current: No` if previously disabled.)

---

## 10. Strategy Presets tab + inline-create offer

Asked for **everyone** who opted into the structured flow (Premium
opt-in). Free-tier alliances that declined the opt-in skip this
entire block.

### 10.1 Explainer

```
┌──────────────────────────────────────────────────────────────────────┐
│ Strategy Presets                                                     │
│ A strategy preset is a saved zone layout including:                  │
│ Maximum players per zone                                             │
│ Optional power requirements                                          │
│ Priority                                                             │
│                                                                      │
│ When leadership builds a roster, they pick which preset to apply.    │
│ The bot uses the preset to gate eligibility and fill out the team.   │
│                                                                      │
│ Manage presets via /desertstorm strategy → 📋 List presets.          │
└──────────────────────────────────────────────────────────────────────┘
```

### 10.2 Tab name

```
┌──────────────────────────────────────────────────────────────────────┐
│ Strategy Presets Tab                                                 │
│ Which Google Sheet tab should store Desert Storm strategy presets?   │
│ The bot creates and maintains this tab.                              │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: DS Strategies]  [↩️ Use default: DS Strategies]  [✏️ Define my own]
```

### 10.3 Inline-create offer (only when zero saved presets)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Want to create your first Desert Storm preset now? You can also do   │
│ this later via /desertstorm strategy → 📋 List presets.              │
└──────────────────────────────────────────────────────────────────────┘
[✅ Create now]  [↩️ Skip — I'll do this later]
```

`✅ Create now` opens the preset editor (multi-step zone wizard,
covered in the runtime walkthrough). `↩️ Skip` continues the wizard.

If gspread is unreachable when listing existing presets, the offer
shows unconditionally (safer to over-offer than skip the discovery
surface entirely).

---

## 11. Member Rules tab + inline-create offer

Same shape as Section 10.

### 11.1 Explainer

```
┌──────────────────────────────────────────────────────────────────────┐
│ Member Rules                                                         │
│ Member rules tell the roster builder how to treat individual         │
│ members.                                                             │
│                                                                      │
│ There are two types of Member rules.                                 │
│ • Power-band:                                                       │
│      Example: members ≥ 80M are eligible for Power Tower            │
│      Primary rule type that reads against the power column you       │
│      configured earlier.                                             │
│ • Per-member:                                                       │
│      Used for special cases, example: Alice always plays on Team A, │
│                                                                      │
│ Add rules later via /desertstorm member_rule → 📋 List rules.        │
└──────────────────────────────────────────────────────────────────────┘
```

### 11.2 Tab name

```
┌──────────────────────────────────────────────────────────────────────┐
│ Member Rules Tab                                                     │
│ Which Google Sheet tab should store Desert Storm member rules? The   │
│ bot creates and maintains this tab.                                  │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: DS Member Rules]  [↩️ Use default: DS Member Rules]  [✏️ Define my own]
```

### 11.3 Inline-create offer (only when zero saved rules)

```
┌──────────────────────────────────────────────────────────────────────┐
│ Want to add your first Desert Storm member rule now? You can also    │
│ do this later via /desertstorm member_rule → 📋 List rules. This     │
│ creates a quick modal for a power-band rule (the most common type);  │
│ per-member rules need a Discord member picker, so add those later    │
│ via /desertstorm member_rule → 📋 List rules.                        │
└──────────────────────────────────────────────────────────────────────┘
[✅ Create now]  [↩️ Skip — I'll do this later]
```

---

## 12. Step 7 — Reminder DM

`ask_keep_or_change` against the storm-log reminder DM body.

```
┌──────────────────────────────────────────────────────────────────────┐
│ Step 7 of 7: Desert Storm Reminder DM (💎 Premium)                   │
│ When leadership runs /desertstorm_remind, the bot DMs every roster   │
│ member this message. Free guilds can configure it now; it just       │
│ won't fire until you have Premium + Member Roster Sync.              │
│                                                                      │
│ Use {name} as a placeholder for the member's roster name (optional). │
└──────────────────────────────────────────────────────────────────────┘
[✅ Keep current: <preview>]  [↩️ Use default: <preview>]  [✏️ Define my own]
```

If the modal body matches the hardcoded default, the wizard stores an
empty string in SQLite so future tweaks to the default automatically
pick up without alliances having to re-run setup.

---

## 13. Save + done

After Step 7 the wizard writes everything to SQLite in a single block
(`save_storm_config` for the parent row + per-team rows, then
`save_participation_config`, then `save_structured_storm_config`).
There's no explicit "✅ Saved!" confirmation embed — the wizard exits
silently. Officers know they're done when the channel goes quiet.

(Note: a closing confirmation line is a known UX gap; not yet
ticketed.)

---

## 14. Cancel + timeout paths

### 14.1 Cancel mid-wizard

Kevin types `/cancel` at any point. The wizard's
`cancel_event` flips, the active view stops, and the next
`wait_view_or_cancel` returns. Every step exits with:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ❌ Cancelled.                                                        │
└──────────────────────────────────────────────────────────────────────┘
```

Nothing is saved — the wizard writes all config at the end only.

### 14.2 Timeout (5 minutes idle)

Every step has the same line:

```
┌──────────────────────────────────────────────────────────────────────┐
│ ⏰ Timed out. Run /setup_desertstorm to start again.                 │
└──────────────────────────────────────────────────────────────────────┘
```

(or `/setup_canyonstorm` for CS — the wizard captures `cmd_name`
verbatim so the redirect points back to the right entry.)

The `wizard_registry.expire_view_message` hook also strips the
buttons off the timed-out view so leadership can't click stale
buttons and get an "Interaction failed" error.

### 14.3 Hot reload / Discord restart

Wizard views don't persist across bot restarts. If Kevin had a wizard
open when the bot restarted, the next button click fails with
"Interaction failed" and the buttons are stripped by the
`wizard_registry` expiry path. He has to re-run `/setup` and pick the
storm wizard again.

---

## 15. Known UX gaps (post-#230)

What's still missing on the setup wizard, as of `dev` after #230:

- **Template choice on re-entry** ([#231](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/231)):
  `SharedTemplateView` (one shared / separate per team) and
  `TemplateChoiceView` (use default / edit) don't expose Keep current.
  A re-entering alliance with a saved custom template can't preserve
  it without re-pasting; clicking Use default silently clobbers the
  saved body.
- **Save confirmation line** (not yet ticketed): no closing
  "✅ Setup saved!" embed after the last step. Wizard just exits.
- **Invalid column letter** (Step 6.3 / 6.4): instead of an in-place
  re-prompt, the wizard aborts and tells Kevin to re-run. Not great
  UX but at least it's clear.
- **Stale-channel banner placement**: the `⚠️ Your previously
  configured … no longer exists` line shows above the picker without
  any button to acknowledge — Kevin just has to pick a fresh channel
  from the select.

---

## Cross-references

- Runtime / event-day UX walkthrough:
  [`PREMIUM_STORM_UX_WALKTHROUGH_v2.md`](PREMIUM_STORM_UX_WALKTHROUGH_v2.md)
  (covers `/desertstorm post_signup`, signups officer view, on-behalf
  modal, roster builder, attendance, strategy preset editor, member
  rules, history browser).
- Wizard code: [`setup_cog.py`](../setup_cog.py)
  (`run_storm_setup`, `_run_storm_participation_step`,
  `_run_structured_flow_setup_step`, `_ask_signup_schedule`).
- Saved-config schema: [`config.py`](../config.py)
  (`guild_storm_config` table — both per-event-type and per-team
  rows).
