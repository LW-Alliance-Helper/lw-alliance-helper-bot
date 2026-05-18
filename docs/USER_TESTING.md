# LW Alliance Helper — User Testing Guide

This document is a usability testing guide for **LW Alliance Helper**, a Discord bot for Last War alliance leadership. The goal of this testing pass is to identify usability issues, unclear language, broken flows, and any other friction in the bot's interface before it goes live.

All feedback is helpful. There is no task list to "complete" and no expected level of coverage — each finding, no matter how small, is a contribution to making the bot better.

---

## Scope

This testing covers the bot's slash commands and interactive flows across these feature areas:

- Event announcements
- Train schedule
- Birthday tracking
- Desert Storm / Canyon Storm (mail drafts and participation logging)
- Survey (squad-powers submissions)
- Growth tracking
- Premium-only features *(see notes below)*

---

## What to watch out for when testing

While exercising the bot, please pay particular attention to:

1. **Clarity of language** — Did each prompt make sense the first time you read it? Did anything need re-reading?
2. **Obvious next step** — When the bot showed a button or asked a question, did you know what to do next?
3. **Friction** — Too many steps, too many buttons, awkward order, confusing emoji or icons.
4. **Broken flows** — Buttons that do nothing, wizards that hang, errors that don't explain themselves.
5. **Helpful vs. in the way** — Does each command save effort, or does it feel like a chore?
6. **Errors are actionable** — When something fails, the bot's message should tell you *why* and *what to do next*. Most error messages also include a short **Reference ID** (e.g. `Reference: a1b2c3d4`); copying that into a ticket lets us correlate to the exact failure on our side.

Findings of any of these kinds — including positive ones (a flow that felt particularly smooth) — are useful.

---

## How to report feedback

Post findings in the **#testing-feedback** channel (<#1498847804213301370>) on the testing server. For each finding, include:

- **What you were doing** — the command or button.
- **What happened** — the bot's response, message, or behavior.
- **What you expected** — what you would have preferred to see, or what you assumed would happen.

Screenshots are welcome. Brief notes are fine — long-form reports are not required.

For broken behavior (the bot doesn't respond, a command crashes, a button does nothing), a one-liner is enough. The detail can be sorted out separately.

---

## Before you start

You will need:

- The **member role** and/or **leadership role** assigned in the testing Discord server. (Leadership covers most commands; member-only role tests the survey and DM-receiver paths.)
- Access to the leadership channel.

To verify roles: in Discord, right-click your name in the member list and look at the assigned roles. If the role you need isn't there, request it before starting.

If anything in setup looks off, flag it in **#testing-feedback** before continuing.

### Wizards and `/cancel`

Most setup commands open a multi-step wizard in the channel. If you get stuck mid-wizard — wrong answer, second thoughts, you don't know the next value — run **`/cancel`** and the bot will stop the active wizard cleanly. If `/cancel` ever leaves a wizard hanging (still showing a prompt or buttons after the cancel acknowledgement), please flag it.

### A note on the setup commands

The bot has a family of `/setup_*` wizards (`/setup`, `/setup_train`, `/setup_events`, `/setup_growth`, `/setup_birthdays`, `/setup_desertstorm`, `/setup_canyonstorm`, `/setup_survey`, `/setup_members`). On the testing server these are typically already configured for you. **Do not run `/setup_reset`** on a configured test server — it wipes the whole configuration. Re-running an individual `/setup_*` command to *update* a section is fine and is a useful thing to test.

If you are testing a fresh install (your own private test guild), feel free to walk through `/setup` end-to-end — feedback on first-time setup is especially valuable.

---

## Tasks to try

These are grouped by feature. Tasks are not required to be done in order or in full. Tasks marked in **bold** are higher-priority for feedback. The bot's prompts and buttons are intentionally not described step-by-step — part of the testing is whether the bot's own UI is clear enough to guide a first-time user.

### Basics

- [ ] Run `/help`. Review the layout. Is anything missing or unclear?
- [ ] Run `/view_configuration`. Does the displayed configuration make sense?
- [ ] Try a command in a non-leadership channel. The bot should refuse — note whether the rejection message is clear.

---

### 📣 Event Announcements

The bot can post in-game event reminders (Plague Marauder, Zombie Siege, etc.) on a schedule. Drafts are posted to leadership for approval before going public.

- [ ] Run `/events overview`. Confirm the configured event types and next firing dates look right.
- [ ] Run `/events show`. Review today's event list and the editor buttons.
- [ ] **Try each editor button** — Add Event, Edit Time, Remove Event, Add Announcement Text, Build Announcement. Note whether each button's purpose is clear before clicking.
- [ ] Run `/events log`. Note whether the message format and information level feel useful.
- [ ] Add custom announcement text to today's events, then build the announcement. Note whether confirm vs. cancel is clearly distinguished.
- [ ] **Custom-blurb round-trip.** Run `/setup_events` and either add a new event or edit one to give it a custom announcement blurb. The only placeholders are `{time}` (event time in the alliance timezone) and `{server_time}` (UTC). Example: *"We will be doing Glacieradon at {time} ({server_time} Server Time)! Remember to start with only 10 hits."* Then run `/events show`, add that event to today's schedule, and click **Build Announcement**. The draft should contain your custom wording — *not* a generic "&lt;event_key&gt; at {time} ({server_time} Server Time)." fallback (which would also render the key in lowercase). If the wording is wrong, please flag it.

---

### 🚂 Train Schedule

The bot tracks the daily alliance train assignment and (when configured) generates a ChatGPT prompt for an announcement blurb.

- [ ] Run `/train`. Review the schedule view and inline buttons.
- [ ] **Add a member to today's or tomorrow's slot.** Walk through the wizard. Note any prompts that required guesswork.
- [ ] If a blurb template is configured, test the **Generate Prompt** button. Note whether the resulting prompt is usable as-is.
- [ ] Try the **Clear** button and cancel out of the confirmation. Note whether the cancel-result message is clear.
- [ ] Run `/train_log` to see past entries. Note whether the rendered output is easy to scan.
- [ ] Run `/setup_train`. The final step is **Step 8 of 8 — Train DM Body (💎 Premium)** with `{name}` as a placeholder. Note whether the prompt makes clear that this fires alongside the channel reminder when the assigned member is on Premium + Member Roster Sync.

---

### 🎂 Birthdays

If birthdays are configured, the bot can announce them and (optionally) add them to the train schedule.

- [ ] Run `/birthdays`. Review the lookahead window and list format.
- [ ] If birthday → train integration is enabled, run `/train_addbirthdays` and verify the train schedule shows the resulting additions.
- [ ] Run `/setup_birthdays` (only if reminders are enabled). The final step is **Step 9 of 9 — Birthday DM Body (💎 Premium)** with `{name}` as a placeholder. Try **Use default** and **Define my own** to see how each lands.

---

### ⚔️ Desert Storm / 🏜️ Canyon Storm

Mail drafting, participation logging, and event lookups for both DS and CS.

#### Draft mail flow

- [ ] Run `/desertstorm overview` (and/or `/canyonstorm overview`). Note whether the overview embed gives a clear picture of the configuration.
- [ ] **Run `/desertstorm draft`.** The flow has four steps: Pick Team → Pick Time → Mail Template (Use as-is or Edit) → Preview, ending with a **Post & Copy** button.
  - Note whether each step's instruction is clear.
  - Note whether it is unambiguous whether the mail has been *posted* yet (vs. only saved as the template for next time).
  - Review the final Post & Copy output.
- [ ] Repeat with `/canyonstorm draft`. The CS draft groups zones under **Stage 1 / Stage 2 / Stage 3** headers and uses full zone names — *Data Center 1*, *Sample Warehouse 2*, *Defense System 1*, *Serum Factory 2*, etc. If you see abbreviated forms (*Dc1*, *Sw2*, *Ds1*, *Sf2*) instead, please flag it.

#### Participation logging

- [ ] Run `/desertstorm participation` (or `/canyonstorm participation`) and complete a log. The configured questions will vary by alliance — note whether each question's wording feels natural for the data being entered.
- [ ] On a numeric question, try entering an invalid value (letters instead of a number). Note whether the error and recovery feel reasonable.
- [ ] After completion, check the configured log channel and confirm the summary post looks right.

#### Looking up past logs

- [ ] Run `/desertstorm log` with no date argument (defaults to today). Review the rendered output.
- [ ] Run `/desertstorm log April 14` (or any past date). Note whether the entry was found and rendered cleanly.
- [ ] Repeat with `/canyonstorm log` — same flow, same date argument.

---

### 📋 Survey

The squad-powers survey: members click a button and submit their stats in a private thread.

#### As a leadership member

- [ ] Run `/survey`. On the free tier this shows the question list; on Premium it shows a manage view with Add / Edit / Remove buttons.
- [ ] Run `/survey_post` to post the answer button.
- [ ] Run `/survey_remind`. Test both **Send now** and **Manage scheduled reminders**.
  - For Send Now, note whether the destination (channel post vs. DM) is clearly indicated.
  - For Manage Scheduled, note whether the frequency / day / time prompts are clear.

#### As a regular member

- [ ] In the survey channel, click the **📋 Answer** button.
- [ ] **Walk through every question.** Note any prompt whose wording made you pause.
- [ ] Try cancelling mid-survey, or stay idle past the timeout. Review the resulting message.
- [ ] Submit a complete survey. Note whether the success state is clear and the thread closes gracefully.

---

### 📈 Growth Tracking

Periodic stat snapshots so the alliance can track progress over time.

- [ ] Run `/growth`. Review the status display and available actions. The status should include a **Next Snapshot** date — note whether it's clear when the next automatic snapshot will fire.
- [ ] Take a manual snapshot via the **📸 Run Snapshot Now** button. Note whether the success message communicates what was captured.
- [ ] Run `/setup_growth` and pick a custom interval (e.g. every 14 days). The confirmation embed should tell you exactly when the next snapshot will fire and offer a one-click way to start tracking from today instead.

---

### 💎 Premium-only features

Premium adds member-aware automation: DMs to roster members, multi-survey, scheduled reminders, member roster sync, and more.

> **Before testing this section, please reach out to me directly so I can do any setup required on the testing server first.** Premium features depend on the Member Roster Sync being configured and an active Premium SKU on the test guild.

Once setup is confirmed, the relevant tasks are:

- [ ] Run `/setup_members` to walk the roster-sync wizard. The final embed reports how many members were written on the initial sync.
- [ ] Run `/sync_members` and verify the reported member count **matches the actual size of the test server** (excluding bots). If the count is `0` or wildly low, please flag it — it usually means the bot is missing a server-level permission.
- [ ] Run `/desertstorm remind` and `/canyonstorm remind` to fire DM reminders. Review the DM body for tone and clarity. **The body is alliance-customisable** — if you re-run `/setup_desertstorm` and change the **Step 7 of 7 — Reminder DM** body, the next `/desertstorm remind` should pick up the new text. Try `{name}` as a placeholder to confirm member-name substitution works.
- [ ] In `/survey_remind`, set up a **scheduled** reminder (DM-via-roster delivery) for a time within the next several minutes. Wait for it to fire. Review the DM body.
- [ ] **Channel/thread destinations.** When a Premium guild's wizard asks for a channel (e.g. an announcement channel), there should be a **📢 Channel** / **🧵 Thread** chooser before the actual picker. Try both paths — pick a thread, run the wizard to completion, and verify the bot posts to the chosen thread.

Run `/upgrade` to see the upgrade flow as a non-premium tester would. The free → premium upsell shows up automatically when a free-tier guild attempts a Premium-only command; please flag any upsell whose wording feels confusing or misleading.

---

### 🎲 Wildcard

Outside of the tasks above, exploratory testing is encouraged. Click buttons not mentioned in this guide. Run commands in unusual orders. Type unexpected input. Test on mobile if you usually test on desktop, and vice versa. Anything surfaced this way is valuable.

---

## Friction journal

While testing, keeping a brief running list of "wait, what?" moments tends to surface the most useful findings. Even a single line per moment is sufficient. Examples of the type of note that's helpful:

- *"The Pick Time buttons on the storm draft show `4PM EST` and `9PM EST`. Could the abbreviation be confusing across timezones?"*
- *"After clicking Post & Copy, it wasn't immediately obvious that the mail had actually posted to the channel — there was no visible confirmation in the leadership channel."*
- *"The participation log's 'Roster names' question allowed me to type a name not on the roster. The 'Save as Visitor' option was unexpected — I thought I had typed the name wrong."*
- *"On a mobile screen, the survey question prompts wrapped in a way that made the help text hard to associate with the question."*

These observations tend to be more actionable than after-the-fact bug reports.
