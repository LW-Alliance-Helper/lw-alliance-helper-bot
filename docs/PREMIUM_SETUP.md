# Premium Subscription Setup

Operator-facing checklist for the bot owner. Walks through the one-time
work needed to enable Discord App Subscriptions for Alliance Helper
Premium. Players (alliance members and admins) don't need any of this —
they just run `/upgrade`.

---

## 0. Bot prerequisites — Privileged Gateway Intents

Before any of the Premium setup below will work, the bot needs the
**Server Members Intent** enabled in the Discord Developer Portal.
This is also a hard prerequisite for the bot to start at all — the
gateway will refuse the connection if the code requests an intent the
portal hasn't granted.

1. Go to https://discord.com/developers/applications
2. Pick the Alliance Helper application.
3. Left sidebar → **Bot**.
4. Scroll to **Privileged Gateway Intents**.
5. Toggle **SERVER MEMBERS INTENT** on. (Save if the portal asks.)

Why this matters:

- **Member Roster Sync** (`/sync_members`) iterates `guild.members` to
  write the roster sheet. Without this intent, `guild.members` is just
  the small handful of users Discord surfaces via interactions — so
  `/sync_members` would write 0 rows even though the SKU is paid for.
- **Auto-resync on join/leave/role-change** depends on the
  `on_member_join` / `on_member_remove` / `on_member_update` gateway
  events firing. Those events do not fire without this intent.
- **Birthday DMs** and **train DMs** look up Discord IDs from the
  roster — so they're empty downstream unless the roster is populated.

Discord allows this intent without verification for any bot in fewer
than 100 guilds. Once you cross 100 guilds, Discord will require the
bot to be verified before it can keep the intent on (you'll get a
~30-day grace period). At that point, plan for the verification
process — there's no Premium-side workaround.

The other two privileged intents (PRESENCE INTENT, MESSAGE CONTENT
INTENT) — only `MESSAGE CONTENT` is also required (used elsewhere in
the bot). PRESENCE is **not** needed and should stay off.

---

## 1. Create the SKU in Discord Developer Portal

1. Open your bot's app in the Discord Developer Portal:
   https://discord.com/developers/applications
2. Pick the Alliance Helper application.
3. In the left sidebar, click **Monetization → Settings**, then enable
   monetization. You'll be asked to:
   - Confirm a payout account (Stripe Connect — it sponsors a payout
     identity for the developer)
   - Complete tax forms (W-9 in the US, equivalent elsewhere)
   - Agree to Discord's Monetization Terms of Service
4. Once approved, in **Monetization → Premium**, click **Add SKU**.
5. Configure the SKU:
   - **Name:** Alliance Helper Premium
   - **Description:** Unlocks unlimited events, multiple templates and
     surveys, member roster sync with DM features, custom growth
     intervals, thread destinations, extended log windows, and more.
   - **SKU type:** **Subscription** (not durable)
   - **Pricing:** $4.99 USD / month
     *(Discord App Subscriptions only support monthly billing today —
     no annual plan.)*
   - **Eligible audience:** **User Subscription** — Discord scopes the
     entitlement to the buyer rather than the guild. The bot enforces
     "one license = one guild at a time" via an in-bot assignment layer
     (see "Licensing model" below). MEE6 uses the same pattern.
6. Click **Save**, then **Publish** when you're ready.
7. Copy the SKU ID — you'll need it in the next step.

> ⚠️ **Discord takes ~15% of subscription revenue.** That's the cost of
> letting Discord handle billing, taxes, refunds, and chargebacks for
> you. Worth it for a single-developer project.

## 2. Wire the SKU into the bot

Add the SKU ID as an environment variable on your hosting provider
(Railway, in our case):

```
PREMIUM_SKU_ID=<your-sku-id>
```

The bot reads `PREMIUM_SKU_ID` at startup. Until it's set, every guild
falls back to free-tier behavior unless its ID appears in
`PREMIUM_BYPASS_GUILD_IDS`.

Other supported environment variables:

| Variable | What it does | When to use |
|---|---|---|
| `PREMIUM_SKU_ID` | Subscription SKU ID | **Required** for real billing |
| `FORCE_PREMIUM` | `1` / `true` flags every guild as premium | Local dev / staging only |
| `PREMIUM_BYPASS_GUILD_IDS` | Comma-separated guild IDs that always resolve as premium without a subscription | The bot owner's home server, internal test servers, beta testers |
| `BOT_ADMIN_GUILD_IDS` | Comma-separated guild IDs where owner-only admin slash commands (`/admin_guild_info`, `/admin_forget_guild`) are registered. Unset = global registration (local dev only) | Set in production to your support / dev guild ID(s) so admin commands don't appear in alliance command pickers |

`PREMIUM_BYPASS_GUILD_IDS` is the canonical way to grant a specific
guild permanent premium status without a Discord subscription — used in
production for the bot owner's home alliance. `FORCE_PREMIUM` is a
nuke option intended for local testing only; do not set it in
production.

## Licensing model — User Subscription + assignment layer

Discord's **User Subscription** SKU type means the entitlement is valid
in **every guild** the subscriber shares with the bot. That is not the
licensing model — the intent is one $4.99/mo per *alliance*, with the
subscriber able to move their license between alliances whenever they
want.

The bot enforces this with a small assignment layer (`premium_assignments`
table; see `premium.py` and issue #41):

- **One row per subscriber.** `user_id` PRIMARY KEY → a single Discord
  user can only pin Premium to one guild at a time. `guild_id` UNIQUE →
  a single guild can only hold one subscriber's pin at a time.
- **`/upgrade` records context.** When a subscriber clicks the Discord
  premium button, the bot remembers which guild they ran `/upgrade` in.
  Once the checkout completes, `on_entitlement_create` consumes that
  context and writes the assignment row.
- **`/premium assign` moves the pin.** A subscriber runs it from the
  guild they want Premium in. The bot rejects with the existing
  subscriber's username if the guild is already pinned by someone else.
- **`/premium overview`** surfaces the current state (active sub +
  assigned guild, lapsed sub with preserved assignment, etc.). On a
  free-tier guild with no active subscription it also doubles as the
  upsell surface, rendering the same pitch + Discord premium button
  `/upgrade` does.
- **`/premium unassign`** releases the pin without canceling the
  Discord subscription — useful for "park it and reassign later."
- **Lapse is not destructive.** When a subscription ends, the
  assignment row stays. If the user resubscribes, Premium auto-resumes
  in the same guild without manual reconfiguration.

`PREMIUM_BYPASS_GUILD_IDS` and `FORCE_PREMIUM` short-circuit before
this assignment check, so the home-alliance bypass and dev-environment
nuke options keep working unchanged.

## 3. Verify in Discord

Once the env var is set and the bot has restarted:

1. In a test server (one you own and where the bot is installed), run
   **`/upgrade`**.
2. You should see Discord's native subscription dialog with the
   "Subscribe" button. Click it and complete the test purchase.
   *(Discord provides test payment methods for app developers — see
   the dev portal docs.)*
3. Once subscribed, run **`/help`** — the title should now show
   "💎 Premium" instead of "Free tier", and the embed color should be
   gold instead of blurple.
4. Run **`/setup_members`** — it should run the wizard instead of
   showing the premium-locked embed.

## 4. Customer-facing rollout

When you're ready to flip the switch for real users:

- Post in your support server announcing Premium is live, with the
  feature matrix from the README.
- Mention that **`/upgrade`** is the entry point.
- Remind users that all existing features stay on the free tier — no
  takeaways. The only thing changing is that the new premium features
  become available for those who want them.

---

## What existing servers see when Premium goes live

> This section also lives in [`docs/MIGRATION.md`](MIGRATION.md) for
> linking from announcements.

**Nothing changes by default.** Every existing alliance keeps every
feature they had on the day Premium launched. The hard caps that are
*new* (e.g. 5 events on free) are higher than what most alliances
were already using, so almost nobody hits them on day one.

When a free-tier server *does* hit a cap (e.g. tries to add a 6th
event), they see a clean upsell embed with a button to subscribe via
Discord. They aren't blocked from anything they already had — they're
just blocked from going past the new free-tier limit.

A few specific things that *might* surprise existing users:

- **Train themes and tones**: free tier is capped at 3 each. If an
  alliance had more than 3 saved themes, the wizard will show a
  one-time "first 3 will be saved" notice next time they open it. The
  saved data isn't truncated until the user clicks save — they can
  upgrade or trim manually.
- **Storm log lookups**: free tier shows only the 4 most-recent log
  entries. The data isn't deleted from the sheet — it's just not
  surfaced via `/desertstorm log` for older dates. Premium users see
  everything.
- **`/events_log` and `/train_log` windows**: free tier shows 7 days of
  history; premium shows 30. Same principle — the data isn't deleted,
  just filtered on read.

If a Premium subscriber later cancels and downgrades to free, **all
their saved data is kept** (extra templates, multiple surveys, etc.).
They can read it, just can't add to it. Re-subscribing restores full
access immediately.
