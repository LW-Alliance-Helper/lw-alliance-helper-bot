# LW Alliance Helper — User Testing Guide

Hi! Thanks for helping me put this bot through its paces before it goes live. I've spent a lot of time on it and I have my own assumptions about what feels right — but **fresh eyes are the only way to find what's actually confusing.**

This doc gives you a list of things to try. **It is on purpose vague about how to do them.** I don't want to lead you through every click; I want to see whether the bot's own prompts and buttons make sense to a person who has never used it before.

If anything feels weird, slow, broken, or just unclear — that's a finding. Even "I read this sentence twice before I understood it" is useful.

---

## What I'm looking for

When you're poking around, please pay attention to these things:

1. **Was the language clear?** Did you understand what the bot was asking? Did any prompt make you stop and re-read it?
2. **Was the next step obvious?** When the bot put up a button or asked a question, did you know what to do next?
3. **Did anything feel awkward?** Too many steps? Too many buttons? Confusing emoji? Wrong tone?
4. **Did anything just plain not work?** Wizard hangs, button does nothing, error you don't understand.
5. **Did the bot help or get in the way?** Did it save you time or feel like a chore?

You **don't** need to find bugs to be useful — telling me "this part felt smooth" is good signal too.

---

## How to report what you find

For each thing you flag, just tell me:
- **What you were doing** — the command or button.
- **What happened** — what the bot said / showed.
- **What you expected or wanted to happen instead.**

Direct message me, post in the test thread, or write it down somewhere I'll see. Screenshots help. Don't worry about formatting.

If something is broken-broken (bot crashes, command doesn't respond), no need to write a long report — just say "the X command did nothing" and I'll dig in.

---

## Before you start

You should already have:
- The bot in your Discord server.
- The leadership role assigned (if you're testing leadership commands — most testing here needs it).
- Access to the leadership channel.
- Run `/help` once just to see what's available.

If any of that isn't true, tell me and I'll get you set up.

---

## Things to try

I've grouped these by feature. **You don't need to test all of them**, and you don't have to do them in order. Pick a few that look interesting and see how they feel. The tasks in **bold** are the ones I most want feedback on.

### 🆘 The basics

- [ ] Run `/help`. Skim what's there. Does the layout make sense? Is anything missing or unclear?
- [ ] Run `/view_configuration`. Did you understand what was being shown?
- [ ] Try a command in the **wrong channel** (a public chat, not leadership). The bot should refuse — was the rejection message clear?

---

### 📣 Event Announcements

The bot can post in-game event reminders (Plague Marauder, Zombie Siege, etc.) on a schedule. Drafts go to leadership for approval before they post publicly.

- [ ] Run `/events`. You should see today's events and an editor with buttons.
- [ ] **Try the buttons one by one** — Add Event, Edit Time, Remove Event, Add Announcement Text, Build Announcement. Did you understand what each one would do *before* you clicked it?
- [ ] Run `/events_log`. Did the layout make sense? Did the message feel useful?
- [ ] Add a custom announcement text to today's events, then build the announcement. Was it obvious how to confirm vs. cancel?

---

### 🚂 Train Schedule

The bot tracks who's getting the alliance train each day and can generate a ChatGPT prompt to help write a blurb.

- [ ] Run `/train`. You should see the current schedule with Add / Update / Generate Prompt / Clear buttons.
- [ ] **Add yourself to today or tomorrow's slot.** Did the wizard's prompts make sense? Did you have to guess at any step?
- [ ] If you have a blurb-template configured, try **Generate Prompt** for someone. Was the resulting ChatGPT prompt usable?
- [ ] Try the **Clear** button (and then cancel out of the confirm). Was the cancel message clear?

---

### 🎂 Birthdays

If birthdays are configured for the alliance, the bot can announce them and add them to the train schedule.

- [ ] Run `/birthdays`. Does the list look right? Is the lookahead window what you'd expect?
- [ ] If you have birthday-train integration enabled, run `/train_addbirthdays` and check whether anyone got added to the train schedule.

---

### ⚔️ Desert Storm / 🏜️ Canyon Storm

Drafting team mail, logging participation, and (for Premium) DMing reminders.

#### The draft mail flow

- [ ] Run `/desertstorm` (and/or `/canyonstorm`). Does the overview embed help you understand what's configured?
- [ ] **Run `/desertstorm_draft`.** Walk through the four steps: pick team → pick time → pick template (Use as-is or Edit) → preview. Then click **Post & Copy**.
  - Was each step's instruction clear?
  - Was it obvious whether the mail had been posted yet?
  - Did the final "Post & Copy" output look right?
- [ ] Try the same with `/canyonstorm_draft`.

#### Participation logging

- [ ] Run `/desertstorm_participation` (or `/canyonstorm_participation`). Walk the wizard through. Each question was defined during setup — does each prompt feel natural?
- [ ] Try entering bad values on a numeric question (letters instead of a number). Did the bot recover gracefully?
- [ ] When done, check the configured log channel. Did the summary look right?

#### Looking up past logs

- [ ] Run `/desertstorm_log` (no date — defaults to today). Did the message render cleanly?
- [ ] Run `/desertstorm_log April 14` (any past date). Did it find the entry?

---

### 📋 Survey

The squad-powers survey. Members click a button, fill out their stats in a private thread.

#### As a leadership member

- [ ] Run `/survey`. Free tier shows the question list; Premium shows a manage view with Add / Edit / Remove buttons.
  - **If you're on Premium**: try the buttons. Was Add/Edit/Remove obvious? Did the wizard for Add make sense?
- [ ] Run `/survey_post`. Did it post the answer button to the right channel?
- [ ] Run `/survey_remind`. Try **Send now** *and* **Manage scheduled reminders**.
  - For Send Now: was it clear where the reminder was being sent?
  - For Schedule: did the frequency / day / time prompts feel reasonable?

#### As a regular member

- [ ] In the survey channel, click the **📋 Answer** button.
- [ ] **Walk through every question.** Does the wording of each prompt make sense? Did anything feel like it expected the wrong kind of answer?
- [ ] Try cancelling mid-survey. Try staying idle past the timeout. What happens?
- [ ] Submit a complete survey. Did the success message feel reassuring? Did the thread close gracefully?

---

### 📈 Growth Tracking

Periodic snapshots of member stats so you can see growth over time.

- [ ] Run `/growth`. Does the status make sense? Are the buttons (run a snapshot, view config, etc.) clearly labeled?
- [ ] If safe to do so, take a manual snapshot. Did the success message say what was captured?

---

### 💎 Premium-only features

Skip this section if your test alliance is on the free tier.

- [ ] Run `/sync_members`. Did it report the right number of members synced?
- [ ] Run `/desertstorm_remind` and `/canyonstorm_remind` to fire DM reminders. Did the DM body feel clear and friendly? Was it obvious who sent it?
- [ ] In `/survey_remind`, set up a **scheduled** reminder to fire at a time soon, then wait for it. Did it actually fire? Did the message make sense to the person receiving it?

---

### 🎲 Wildcard

This is the part I'm most curious about: **try anything not on this list.** Click a button I didn't mention. Run a command in an unusual order. Try to break the wizard. Type weird input.

If you find something interesting, that's gold — write it down.

---

## Friction journal

If you're up for it, keep a running list of every "wait, what?" or "huh, that's weird" moment as you go. Even a one-liner per moment is great. Examples of what that looks like:

- *"The Pick Time buttons on storm draft show 4PM EST and 9PM EST — would `4PM` alone read better?"*
- *"After clicking Post & Copy, I wasn't sure if the mail had actually posted to the channel — there was no confirmation."*
- *"The participation log's 'Roster names' question lets me type a name that isn't on the roster. The 'Save as Visitor' choice was confusing."*
- *"On mobile the survey question prompts wrap awkwardly."*

These notes are *more* valuable to me than a list of bugs.

---

## Thank you

Seriously. The bot has been built mostly on assumptions about what an alliance leadership team needs — your hour or two of poking it tells me whether those assumptions hold up. Send anything you've got.

— Kevin
