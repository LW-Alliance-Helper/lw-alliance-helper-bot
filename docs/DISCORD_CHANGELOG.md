# Discord changelog posts

What gets posted to `#changelog` in the support server, one block per
release, newest first. `release-on-main.yml` picks the block whose header
contains the version being released and posts it verbatim.

**This file is the post, not a summary of it.** Write it the way it
should read in Discord. Nothing is transformed on the way out.

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
from when several quiet releases got bundled into one post. Those still
resolve, but new releases each get their own block.

**Bursts share a message.** Over half of this project's releases land
within 24 hours of the previous one, and the closest pair was 13 minutes
apart, so a release shipping within 12 hours of the last post is appended
to that Discord message rather than firing a fresh notification. A run of
hotfixes reads as one entry that grows:

```
**1.7.5** — 2026-07-30
- Growth snapshots record real numbers again

**1.7.6** — 2026-07-30
- Growth Breakdown buckets comma-formatted metrics
```

That's automatic and needs nothing from you — keep writing one block per
release. If the window has passed, the combined message would pass 2000
characters, or the stored message can't be found, it starts a new one
instead. The fallback is always to say something.

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
body. If it's missing when the release lands, the workflow logs it and
posts nothing rather than posting something wrong.

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
