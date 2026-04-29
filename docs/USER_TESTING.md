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

- [ ] Run `/events`. Review today's event list and the editor buttons.
- [ ] **Try each editor button** — Add Event, Edit Time, Remove Event, Add Announcement Text, Build Announcement. Note whether each button's purpose is clear before clicking.
- [ ] Run `/events_log`. Note whether the message format and information level feel useful.
- [ ] Add custom announcement text to today's events, then build the announcement. Note whether confirm vs. cancel is clearly distinguished.

---

### 🚂 Train Schedule

The bot tracks the daily alliance train assignment and (when configured) generates a ChatGPT prompt for an announcement blurb.

- [ ] Run `/train`. Review the schedule view and inline buttons.
- [ ] **Add a member to today's or tomorrow's slot.** Walk through the wizard. Note any prompts that required guesswork.
- [ ] If a blurb template is configured, test the **Generate Prompt** button. Note whether the resulting prompt is usable as-is.
- [ ] Try the **Clear** button and cancel out of the confirmation. Note whether the cancel-result message is clear.

---

### 🎂 Birthdays

If birthdays are configured, the bot can announce them and (optionally) add them to the train schedule.

- [ ] Run `/birthdays`. Review the lookahead window and list format.
- [ ] If birthday → train integration is enabled, run `/train_addbirthdays` and verify the train schedule shows the resulting additions.

---

### ⚔️ Desert Storm / 🏜️ Canyon Storm

Mail drafting, participation logging, and event lookups for both DS and CS.

#### Draft mail flow

- [ ] Run `/desertstorm` (and/or `/canyonstorm`). Note whether the overview embed gives a clear picture of the configuration.
- [ ] **Run `/desertstorm_draft`.** The flow has four steps: Pick Team → Pick Time → Mail Template (Use as-is or Edit) → Preview, ending with a **Post & Copy** button.
  - Note whether each step's instruction is clear.
  - Note whether it is unambiguous whether the mail has been *posted* yet (vs. only saved as the template for next time).
  - Review the final Post & Copy output.
- [ ] Repeat with `/canyonstorm_draft`.

#### Participation logging

- [ ] Run `/desertstorm_participation` (or `/canyonstorm_participation`) and complete a log. The configured questions will vary by alliance — note whether each question's wording feels natural for the data being entered.
- [ ] On a numeric question, try entering an invalid value (letters instead of a number). Note whether the error and recovery feel reasonable.
- [ ] After completion, check the configured log channel and confirm the summary post looks right.

#### Looking up past logs

- [ ] Run `/desertstorm_log` with no date argument (defaults to today). Review the rendered output.
- [ ] Run `/desertstorm_log April 14` (or any past date). Note whether the entry was found and rendered cleanly.

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

- [ ] Run `/growth`. Review the status display and available actions.
- [ ] Take a manual snapshot. Note whether the success message communicates what was captured.

---

### 💎 Premium-only features

Premium adds member-aware automation: DMs to roster members, multi-survey, scheduled reminders, member roster sync, and more.

> **Before testing this section, please reach out to me directly so I can do any setup required on the testing server first.** Premium features depend on the Member Roster Sync being configured and an active Premium SKU on the test guild.

Once setup is confirmed, the relevant tasks are:

- [ ] Run `/sync_members` and verify the reported member count.
- [ ] Run `/desertstorm_remind` and `/canyonstorm_remind` to fire DM reminders. Review the DM body for tone and clarity.
- [ ] In `/survey_remind`, set up a **scheduled** reminder (DM-via-roster delivery) for a time within the next several minutes. Wait for it to fire. Review the DM body.

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
