# Premium Migration Guide

What existing alliances will see when Premium goes live, in plain
language. **No data is ever deleted, no setting is ever removed, no
in-flight wizard breaks.**

---

## The short version

- **Every feature you had stays free.** Same wizards, same commands,
  same behavior.
- **New caps only kick in when you exceed them.** If you have 4 events,
  the bot doesn't care that the free cap is 5 — you'll only see an
  upsell embed if you try to add a 6th.
- **Premium adds new things; it doesn't take anything away.** The new
  feature set (member roster sync, DM reminders, multiple templates,
  thread destinations, etc.) didn't exist before — so there's nothing
  to "lose" when downgrading.

---

## What's new on the free tier (caps that didn't exist before)

| Area | New free cap | What happens at the cap |
|---|---|---|
| **Events** | 5 total | Upsell embed when adding the 6th |
| **Survey questions** | 5 per survey | Upsell embed when adding the 6th |
| **Growth metrics** | 5 metrics | Upsell embed when adding the 6th |
| **Train themes** | 3 themes | Custom comma-list trimmed to first 3 with notice |
| **Train tones** | 3 tones | Same |
| **Train templates** | 1 ("Default") | Wizard's manage-templates loop locks Add at 1 |
| **Storm templates per team** | 1 ("Default") | Same |
| **Growth Custom Interval** | Locked | "Monthly" button still works; "Custom interval" disabled with 🔒 |
| **`/events log` window** | 7 days | Older entries hidden in the response (data not deleted) |
| **`/train log` window** | 7 days | Same |
| **Storm log lookback** | 4 most-recent | Older lookups show upsell embed (sheet data preserved) |
| **Channel pickers** | Text channels only | Threads not offered in the dropdown |

If your alliance is already operating beyond any of these (rare —
most are far more than a typical alliance uses), nothing breaks. The
data stays. You just can't add more until you subscribe or trim.

---

## What's exclusively Premium (didn't exist before)

These features are new with the Premium launch — there's no "before"
state to migrate:

- **Member Roster Sync** (`/setup` → 👥 Members, `/members sync`) — writes
  every member's Discord ID to a sheet tab so other premium features
  can find them.
- **Birthday DMs** — a personal DM in addition to the channel post.
- **Train assignment DMs** — a heads-up to the member assigned today.
- **Auto-mention in train reminders** — the channel post uses
  `<@id>` instead of plain text when the roster knows them.
- **Multiple named templates** — for train prompts and storm mails.
- **Multiple named surveys** — manage extras from `/survey` (Add / Edit / Remove).
- **`/survey remind`, `/desertstorm` reminder button, `/canyonstorm` reminder button** —
  manual DM blasts to the whole roster.
- **30-day windows on `/events log` and `/train log`** (vs 7).
- **Unlimited storm-log lookback**.
- **Thread destinations** in every channel-picker step.
- **Custom-interval growth snapshots** (vs monthly only).

---

## How licensing works

Premium is a **per-user** subscription that applies to **one server at
a time**. When you run `/upgrade`, the bot pins your subscription to the
server you're in. To move it to a different alliance:

- Run **`/premium assign`** in the alliance you want Premium in.
- Run **`/premium overview`** to see where it's currently active.
- Run **`/premium unassign`** to release the pin without canceling the
  subscription (useful for "park it and come back later").

If a server already has Premium from another subscriber, the bot will
tell you and prompt you to pick a different alliance — two
subscriptions can't both apply to the same server.

---

## Downgrades

If a premium subscriber cancels through Discord:

- Subscription stays active until the end of the billing cycle.
- After cycle end, the bot reverts to free-tier behavior.
- **All saved data is kept.** Extra templates and named surveys remain
  in the database; they're just not editable until premium is
  re-activated. (You can still read your roster sync sheet — that
  lives in your own Google Sheet.)
- **Your assignment is also kept.** If you re-subscribe, Premium
  auto-resumes in the same server you had it pinned to before. No
  re-config required.

---

## Questions?

Run `/upgrade` for the live subscribe button or `/donate` to support
the bot without subscribing. For setup help, run `/help` or check the
main [README](../README.md).
