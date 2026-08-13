# Discord changelog posts

What gets posted to `#changelog` in the support server, one block per
release, newest first. The **bot** posts it, from `on_ready`, picking the
block whose header names the version it's running and sending it
verbatim. Not CI: the release workflow finishes before Railway has
deployed, so a post from there could announce a release that then fails
to ship. See `changelog_post.py`.

**This file is the post, not a summary of it.** Write it the way it
should read in Discord. Nothing is transformed on the way out.

Everything from 1.2.0 down is the archive of what was actually posted,
transcribed from the channel. Older entries don't all follow the rules
below — some run to nine bullets, and 1.5.0 and 1.5.1 spell their dates
out. They're kept as posted rather than tidied, because this is a record
of the channel, not a rewrite of it.

Versions with no block here never got a post: everything before 1.2.0,
1.3.1 through 1.3.4, and 1.4.7 (whose fix was folded into the 1.5.0
post). Those predate the gate described below.

The channel's pinned header, which is not a release block and lives only
in Discord:

> **📜 Changelog**
> Every version that ships gets a one-line summary here. For the deeper
> context (what broke, why, what changed), the GitHub CHANGELOG has the
> full record.
>
> Want a ping for new releases? Pick up @Release Pings in Channels & Roles.

## Format

- Header `**X.Y.Z** — YYYY-MM-DD`. That em dash is the sanctioned exception.
- Max 5 bullets, one thing changed per bullet.
- No links, no bold, no emoji, no trailing periods. Backticks stay on
  slash commands.

**Every release posts.** `release-changelog-check.yml` fails the release
PR when `__version__` moves and this file has no block for it, so a
release can't ship without one. To skip a release deliberately, say so in
its block rather than leaving a hole — a hole is indistinguishable from
having forgotten:

```
**1.8.6** — 2026-08-12
NO POST: dependency bumps only
```

Older entries use a range header (`**1.7.1 to 1.7.4** — 2026-07-21`),
from when several quiet releases got bundled into one post. Don't write
new ones: matching is literal, so such a header resolves for the two
versions it names and not the ones between them. Every new release gets
its own block, and bursts are handled below.

**Bursts share a message.** Over half of this project's releases land
within 24 hours of the previous one, and the closest pair was 13 minutes
apart, so a release deploying within 12 hours of the last post is
appended to that Discord message rather than firing a fresh
notification. A run of hotfixes reads as one entry that grows:

```
**1.7.5** — 2026-07-30
- Growth snapshots record real numbers again

**1.7.6** — 2026-07-30
- Growth Breakdown buckets comma-formatted metrics
```

That's automatic and needs nothing from you — keep writing one block per
release. If the window has passed, the combined message would pass 2000
characters, or the stored message was deleted, it starts a new one
instead. The fallback is always to say something.

The destination is the `CHANGELOG_CHANNEL_ID` env var in Railway.
`/admin changelog` shows where things stand (including whether the bot
can still post there), `version:1.9.0` previews a post without sending
it, and `repost:True` sends the running version again if one goes out
wrong.

## Compactness (this is the part that keeps getting missed)

Every bullet states the bare change and stops. Not what the change does,
not what it covers, not why it's useful. Those are all real information,
and they all belong in CHANGELOG.md instead.

Three things get cut every single time, so cut them while drafting:

1. **The trailing "so that…" clause.** "The buddy list can be limited to
   your member roster" — not "…so a departure drops out of pairing on its
   own".
2. **The "what it covers" bullet.** A line whose whole job is listing
   scope gets deleted, not shortened. "Covers Train Schedule, Member
   Roster, DS/CS Assignments, Shiny Tasks, train reminders, survey
   reminders and storm sign-up" says nothing changed.
3. **The inline enumeration.** "`/survey` is now a hub" — not "…with Add,
   Edit, Remove, Post, Reminders and Translation as buttons".

Worked example, the 1.8.3 opener over three passes:

```
too long   The bot now tells your leadership channel when something it was
           told to use has stopped working: a renamed sheet tab, a
           spreadsheet it can no longer open, or a channel it can't post in
still long The bot now tells leadership when a sheet tab, spreadsheet or
           channel it uses stops working
           Covers Train Schedule, Member Roster, DS/CS Assignments, …
right      The bot now tells leadership when a sheet tab, spreadsheet or
           channel it uses stops working
```

Draft each bullet as the bare change and stop. Writing the full sentence
and trimming back does not work — the trim never goes far enough.

Also cut anything an alliance leader can't act on: dependency bumps,
owner-only tooling, internal refactors, and fixes to background errors
they'd never have seen.

## When to write it

On the release branch, alongside the CHANGELOG entry and the release PR
body. The release PR fails without one, so it can't be skipped by
accident.

---

**1.9.0** — unreleased
- `/vs` tracks your Alliance Duel league and projects your path through the bracket
- Check my sheet names the row and column of every entry mistake it finds
- An optional daily post asks for the duel day that just finished
- `/champion_duel` gives the odds for a match, as a card you can share
- Premium alliances can record the Champion Duel squads and orders they scout

---

**1.8.7** — 2026-08-11
NO POST: two log-noise fixes. Neither was ever visible to an alliance, and
the setup problem behind the second one is already reported to leadership
by the existing notice.

---

**1.8.6** — 2026-08-10
- Squad Power survey answers could land in a hidden extra column instead of updating your existing one
- The bot now tells leadership when it loses access to a Google Sheet instead of staying silent
- Train Conductor Rotation setup and This week's draft show a clear message instead of crashing when your sheet has a problem

---

**1.8.5** — 2026-08-07
- Add a survey from a template or from scratch, in your own wording
- A survey's two sheet tabs are created and labelled for you
- Editing a survey's questions no longer shifts later answers into the wrong columns
- Two surveys can no longer share a sheet tab
- Naming a tab another feature already uses now warns you

---

**1.8.4** — 2026-08-07
- Adding the app without the bot now says so, with a re-invite link
- The invite link asks for Attach Files and Manage Channels

---

**1.8.3** — 2026-08-05
- The bot now tells leadership when a sheet tab, spreadsheet or channel it uses stops working
- `/setup` then View configuration marks channels the bot can't post in
- The DS/CS draft warns when it started from default assignments
- Growth keeps a member's history through an in-game name change

---

**1.8.2** — 2026-08-03
- An opt-out column on Squad Powers takes someone out of buddy pairing
- The buddy list can be limited to your member roster
- Re-pair from scratch names who it will remove first, and no longer keeps members already dropped from Squad Powers
- Squad Powers names missing from your roster are reported after a buddy action

---

**1.8.1** — 2026-08-03
- `/survey` is now a hub, also reachable from `/setup`
- Free-tier alliances can configure their survey again
- `/survey overview`, `/survey post` and `/survey remind` are now hub buttons

---

**1.8.0** — 2026-08-01
- Name a translate bot and it joins every private survey thread, so members who don't read the language in which you write your surveys can translate their survey; free on every tier
- `/events` gains Pause or resume: stop an event for a season and turn it back on with every setting intact, setting a fresh anchor date on the way back. Deleting is now permanent
- The `/events` anchor date takes 7/30, 2026-07-30, July 30th, or today, and retries instead of ending the wizard
- The transfer watcher (Premium) reports a renamed, deleted, or inaccessible sheet tab instead of failing silently
- Timed-post reliability: train reminders no longer hold up other posts, 5-minute event warnings survive restarts, outage recovery uses server time, and storm sign-up can't skip a week

---

**1.7.6** — 2026-07-30
- Growth Breakdown buckets metrics with thousands separators instead of reporting no members
- Growth snapshot columns are written with thousands separators, matching your source columns
- The 0-5% growth bucket is now labelled No Change instead of None

---

**1.7.5** — 2026-07-30
- Growth snapshots recorded 0 for metrics with thousands separators, like squad power and total kills; now fixed
- `/my_stats` and `/member_stats` show real growth numbers again
- Growth snapshots no longer fail or skip a metric past column Z

---

**1.7.1 to 1.7.4** — 2026-07-21
- Daily Shiny Tasks is posting again after stopping for every alliance on July 17
- Shiny Tasks fires as soon as possible after a missed minute instead of skipping the day
- 1.7.1 to 1.7.3 were support tooling and dependency updates, nothing alliance-facing

---

**1.7.0** — 2026-07-02
- Groundwork for the Map Manager integration (Premium).

---

**1.6.7** — 2026-07-02
- The daily event editor names the channel it can't post to instead of failing, and keeps working for your other events
- A deleted or inaccessible Google Sheet skips your growth snapshot cleanly instead of erroring

---

**1.6.6** — 2026-07-01
- `/setup` no longer errors if its channel is deleted mid-wizard
- The `/train` hub's Schedule presets and Member rules buttons no longer fail when the roster sheet is slow
- Keep current now appears first on storm teams and time slots, growth breakdown thresholds and labels, and import sheet ID

---

**1.6.5** — 2026-06-29
- The weekly train draft has an Add reason button to note why a member is a day's conductor (e.g. nominated for helping out), shown as a sub-line under their name and carried through to the daily confirmation

---

**1.6.4** — 2026-06-29
- Transfer filters can now combine conditions with AND or OR (e.g. wants OGV or Open, and power over 70M)
- Re-running transfer setup offers a Keep-current option for the notification channel, style, and filters so you don't redo them
- The transfer setup edit menu is regrouped into fewer, clearer sections with plainer labels

---

**1.6.3** — 2026-06-29
- The buddy Unpair / Pair / Re-pair picker now pages through everyone with arrow buttons instead of stopping at the first 25, so alliances with more than 25 pairs or free members can reach all of them

---

**1.6.2** — 2026-06-29
- A Check now button on `/transfers` pulls from your sources right away and shows a read, matched, and copied breakdown so you can see what came through
- Re-running transfer setup re-pulls your sources from scratch, no longer skipping applicants an earlier setup run had already pulled
- The shared-sheet pull no longer adds someone already on your sheet twice, deduping against your sheet's real contents

---

**1.6.1** — 2026-06-28
- Transfer Management can fill in just the blank cells of people already on your sheet from a connected source, instead of skipping them (opt-in)
- Transfer setup maps source-sheet columns onto your own sheet's columns when they're named differently, so copied rows line up
- A transfer decision can map to a column already in your sheet instead of always creating a new one
- Transfer notifications can post to a thread, not just a text channel
- Transfer filter setup has a Back / no-filter path, so starting a filter and changing your mind no longer traps you
- Re-running transfer setup offers Keep current for your sheets and setup type, with consistent Keep-current buttons throughout
- Transfer column mapping labels the Shown in notices picker clearly, and the style step is now named Notification style

---

**1.6.0** — 2026-06-28
- Transfer Management (Premium): watches your recruiting sheet and pings you on new applicants and status changes
- Transfer notices carry one-click in-game message drafts (apply, confirm, decline) and a full applicant record
- Optional server-wide and intake-form pulls auto-copy filter-matching applicants into your sheet
- Optional write-back marks an applicant Want, Confirmed, or Declined from Discord and updates your sheet

---

**1.5.10** — 2026-06-27
- Free alliances can now point Conductor Rotation at any roster tab and name column, so the fair daily rotation works on names alone without the Premium member sync
- Role-scoped train days (Leadership, VS, Contest, Event) are now Premium; on the free plan every day rotates the full roster fairly, and a lapsed subscription falls back to that instead of leaving role days unassigned

---

**1.5.9** — 2026-06-22
- The birthday scheduling-conflict alert stops re-posting nightly once resolved, and a member placed anywhere within a week of their birthday now silences it
- The birthday scheduling-conflict alert is now interactive: place the member on an open day, show the surrounding week, or dismiss it for good

---

**1.5.8** — 2026-06-15
- Daily Shiny Tasks posts now follow the in-game server day, so a post timed just after the reset no longer lists the prior day's servers

---

**1.5.7** — 2026-06-11
- Train conductor announcements now name the in-game day that's starting, not the one that just ended, so they no longer land a day behind the server reset

---

**1.5.6** — 2026-06-08
- The weekly train draft shows each day's conductor as a Discord mention with shorter rule labels, replacing the code block that wrapped on mobile

---

**1.5.5** — 2026-06-08
- Re-drafting the train week takes a single click and can no longer wipe a day's rule (e.g. Leadership back to auto) if Google Sheets is briefly slow

---

**1.5.4** — 2026-06-07
- `/train` buttons (This week's draft, View logs, and more) now show a loading state instead of looking hung while they read your sheet
- Assigning a conductor on a role day (Leadership, VS, Contest, Event) lists just that role's members, with a toggle to switch to the full roster

---

**1.5.3** — 2026-06-07
- Train schedule editing: Assign someone uses a roster dropdown, Re-draft refreshes the draft and clears its prompt, and Go to next person advances on Leadership days

---

**1.5.2** — 2026-06-07
- Outage catch-up: when the bot comes back after downtime, it posts one digest in your leadership channel of every scheduled post it missed, so you can send or dismiss each with a single click.
- New `/my_stats`: anyone can pull up their own power, storm, and survey history in one place.
- New `/member_stats` for leadership: pick a member to see that same view plus their train history and storm sign-up record.
- Profession Buddy can now rank Engineers by reliability: keep a 1 to 5 score in your sheet and the bot pairs your most reliable Engineers with your top War Leaders.
- The train weekly draft has new previous and next week buttons, and opening `/train` on your draft day now jumps straight to the upcoming week.
- Train fairness now reads your whole Train History sheet, so you can back-fill past drives just by adding rows, and brand new rotations pick fairly at random instead of going alphabetically.
- Train History now tracks each conductor's Discord ID, so someone who changes their display name keeps a single, accurate fairness record.
- Refreshed older command and button references in setup, help, and bot messages so they match the current `/train`, `/events`, and storm menus.

---

**1.5.1** — June 5, 2026
- Profession Buddy: changing your own profession now sends you one DM listing all your buddies, instead of a separate DM for each pairing.
- `/setup` no longer errors when you run it in a direct message.
- Storm sign-up and roster screens no longer hit the Google Sheets read limit when you click through them quickly.
- The storm roster builder no longer hides players because of a leftover draft from a previous event.

---

**1.5.0** — June 4, 2026
- New Train Conductor Rotation (free, opt-in): the bot fairly rotates each day's conductor for you, with schedule presets, per-member and per-day rules, a weekly draft to review, and a daily confirmation, all from `/train`.
- New Profession Buddy System: pair your War Leaders with Engineers so members can look up their buddy any time. Premium adds auto-assign, re-pairing, and customizable buddy DMs.
- Storm sign-ups: officers can now clear all votes, or clear just on-behalf votes, to reset a poll without deleting it.
- Today's events opens the editor even when all your events are Manual, so you can add a one-off event to today's draft.
- Setup wizard timeout messages no longer crash mid-flow.

---

**1.4.6** — 2026-05-31
- Storm auto-fill Strength to priority spreads squad power evenly across buildings that share a priority, instead of piling the strongest onto the first one
- Storm roster builder can return a sub to the assignable pool, so you can swap a starter and a sub after teams are built
- Storm roster builder no longer offers a player to both teams once they're placed on one
- `/events` is now a hub command with a preset library, matching the `/desertstorm` and `/canyonstorm` layout
- More consistent wording across setup wizards, errors, timeouts, and confirmations

---

**1.4.5** — 2026-05-29
- Choosing Edit to paste your own roster DM template during Premium storm setup no longer crashes the wizard

---

**1.4.4** — 2026-05-27
- Team A / Team B plan picker lists candidate members by name instead of their raw Discord ID

---

**1.4.3** — 2026-05-27
- Storm roster readers fall back to the Name column (and the live Discord member) when Display Name is blank, so the sign-up poll and Team Plan show names instead of raw IDs

---

**1.4.2** — 2026-05-24
- Vote click shows a poll-style ephemeral with per-option totals and ✓ on your vote; new leadership View sign-ups button opens the full breakdown
- Premium: stale-power DM nudges members whose roster power hasn't been refreshed in N days, configurable in setup
- Leadership can re-post a sign-up message for an event that already has one; votes from every post aggregate
- Power Data Source picker renamed Name-match column to Member-match column for clarity
- Member sync preserves hand-typed non-Discord roster rows instead of deleting them on subsequent syncs
- Power-refresh DM leads with "Your vote was recorded" so members don't mistake it for a failed vote

---

**1.4.1** — 2026-05-24
- Power-refresh DM names the column on the configured Power Data Source tab instead of always reading the Member Roster

---

**1.4.0** — 2026-05-23
- Premium Storm Overhaul: structured sign-up to roster builder to PNG mail with auto-fill, per-event team plan, per-team time slots, and per-member assignment DMs
- Participation Tracking 2.0 (Premium): per-member question types written to a Per-Member Log tab, parameterized Trends Viewer, and preset question templates
- `/desertstorm` and `/canyonstorm` event hubs consolidate every storm action under one command per event type.
- Release announcements toggle on the `/setup` hub controls whether new major/minor releases post a summary to your leadership channel
- Member Sync renamed with Power Data Source flexibility, collision protection, and presence column in the sync preview.
- Storm and participation share one Alias Column, configured once during setup instead of separately per surface.
- DS + CS mail bodies unified to a single zone-grouped, stage-aware structure.
- Setup wizard re-entry: Keep current covers mail templates and shared/separate choice without clobbering saved bodies
- Officers with the Leadership role can run `/setup`

---

**1.3.0** — 2026-05-12
- Saved-config summaries, Keep current buttons, and a friendlier disable flow across every `/setup_*` command.

---

**1.2.0** — 2026-05-11
- Growth Breakdown, daily Shiny Tasks announcement, and Data Portability.
