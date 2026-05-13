# Premium Storm — End-to-End Content Audit

Comprehensive inventory of every user-facing surface in the Premium
Storm structured roster flow (issues [#38](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/38)
and [#54](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/54)).
Mirrors the layout of [CONTENT_AUDIT.md](CONTENT_AUDIT.md) but scoped
to the Premium-only flow: signups → roster build → mail → attendance
→ history → faction roles.

The Verbatim column quotes the user-facing copy preserving emoji,
ellipses, line breaks (`\n`), and template placeholders (`{name}`,
`{count}`). The File column tells you where to grep to make an edit.

> **Excluded**: log lines (`[STORM …] …`), docstrings, code comments,
> user-customisable templates (mail bodies, DM bodies — only the bot
> scaffolding is shown). Free-tier `/setup_desertstorm` /
> `/setup_canyonstorm` copy that pre-dates the structured flow is
> already covered in `CONTENT_AUDIT.md`.

---

## Conventions

The emoji palette and tone rules in `CONTENT_AUDIT.md` apply here.
A few storm-specific additions:

| Emoji | Meaning |
|---|---|
| ⚔️ | Desert Storm / general "storm event" |
| 🏜️ | Canyon Storm (rarely used; ⚔️ dominates) |
| 🛡️ | Roster builder UI (main view) |
| 🗺️ | Zones / map context |
| 📋 | Zone list / sub list / strategy preset |
| 🪑 | Subs (any sub list — flat or paired) |
| 🎯 | Active zone / auto-fill |
| 🪞 | Faction reveal / matchmaking outcome |
| 🔁 | Re-pair / swap |
| 🖼️ | Image render |
| 📄 | Mail preview / generate mail |
| 💾 | Save preset / saved config |
| ✅ | Yes / Use default / Keep current / Approve & Post / vote-team |
| ❌ | No / Cancel / Cannot participate |
| ⛔ | Permission denied |
| ⚠️ | Validation warning / soft error |
| ⏰ | Timeout |

The flow is **multi-step ephemeral**: every officer interaction is
ephemeral by default so leadership chat stays clean. The only
auto-posted (non-ephemeral) surfaces are the **sign-up post**, the
**rosters mail**, and **scheduler-driven** posts (sign-up auto-
schedule, attendance kick-off).

---

## Cross-feature glossary

| Term | What it is |
|---|---|
| **Sign-up post** | Auto-posted message with the four vote buttons (Team A / Team B / Either / Cannot). |
| **Vote** | A member's click on the sign-up post. Stored in SQLite; mirrored to `signups_tab`. |
| **Bucket** | Officer-view grouping of members by vote (`a`, `b`, `either`, `cannot`, `not_voted`). |
| **On-behalf vote** | Officer casts a vote for a non-Discord roster member via the modal. |
| **Roster builder** | The `RosterBuilderView` officers use to slot signed-up members into zones. |
| **Primary** | A member assigned to a zone (the "starting line-up"). |
| **Paired sub** | In paired sub_mode, a sub explicitly attached to one primary. |
| **Overflow sub** | A sub in `session.subs` (flat pool) that isn't paired with any primary. |
| **Sub pool / flat sub list** | The default `pool` sub_mode — one shared bench rather than 1:1 pairings. |
| **Preset** | A saved zone layout (zone names + floors + capacities + faction). |
| **Member rule** | Per-member or power-band rule (team, zone, special_role, eligibility floor). |
| **Power band** | A power-range rule (`>=300M → eligible for Power Tower`). |
| **Floor** | Minimum-power threshold for a zone; team-specific (Min A / Min B). |
| **Below-floor override** | An officer's explicit decision to slot a member who doesn't meet the floor. |
| **Faction roles** | Post-roster step that applies the Judicator role to candidates when matchmaking reveals Rulebringers. |
| **Power-refresh DM** | One-off nudge DM'd to a voter whose roster Sheet power cell is blank/garbage. |

---

## 1. Slash commands

Every Premium Storm surface starts with one of these. Commands are
sorted by where the user normally encounters them.

| Command | Description | File |
|---|---|---|
| `/setup_desertstorm` | Configure Desert Storm mail template and time options | `setup_cog.py:1507` |
| `/setup_canyonstorm` | Configure Canyon Storm mail template and time options | `setup_cog.py:1522` |
| `/sync_members` | Force a fresh `/sync_members` write to the alliance roster Sheet | `member_roster.py:430` |
| `/storm_post_signup` | Manually post a sign-up post for the next storm event | `storm_signup_post.py:197` |
| `/storm_signups` | Open the officer view (bucket map + filter + on-behalf) | `storm_officer_view.py:801` |
| `/storm_attendance` | Post-event attendance tracker (Premium) | `storm_attendance.py:703` |
| `/ds_strategy create|edit|list|delete|apply|roster_history` | DS strategy preset management | `storm_strategy.py:935-983` |
| `/cs_strategy create|edit|list|delete|apply|roster_history` | CS strategy preset management | `storm_strategy.py:983-1009` |
| `/ds_member_rule set_power_band|set_member_team|set_member_zone|set_member_role|list` | DS per-member + power-band rules | `storm_member_rules.py:734-824` |
| `/cs_member_rule set_power_band|set_member_zone|set_member_role|list` | CS per-member + power-band rules (no `team` — CS has no teams) | `storm_member_rules.py:825-882` |

Premium gating is enforced at command entry via
`storm_permissions.ensure_premium_structured(...)`. Free-tier
guilds see this ephemeral on the gated commands:

> 💎 **{feature_label}** is a Premium feature. See `/upgrade` for details.

(canonical phrasing in `premium.py` / `storm_permissions.py` — both
storm-gated surfaces route through the same helper.)

---

## 2. Setup wizard — structured flow sub-step

Lives in `setup_cog._run_structured_flow_setup_step` (`setup_cog.py:5702`).
Walks an alliance through opt-in + every per-event-type field.

### 2.1 Premium opt-in

| Surface | Verbatim | File:line |
|---|---|---|
| Opt-in prompt | `**Structured Roster Flow (💎 Premium)**\nThe structured flow auto-posts a Discord sign-up poll, captures…` | `setup_cog.py:5763` |
| Yes/No view | Standard `YesNoView` | `setup_cog.py:1704` |

### 2.2 Power column

| Surface | Verbatim | File:line |
|---|---|---|
| Prompt | `**Power Metric Column**\nWhich column in your member-roster Sheet holds the squad-power value the eligibility gate should compare against?` | `setup_cog.py:5800` |
| Default | none — alliance picks from their Sheet header |  |

### 2.3 Sub mode

| Surface | Verbatim |
|---|---|
| Prompt | `**Sub Mode**\nHow should subs be assigned?` |
| Pool button | `📋 Pool (flat sub list)` |
| Paired button | `🪑 Paired (1:1 with each primary)` |

### 2.4 Sign-up channel + schedule

| Surface | Verbatim | File:line |
|---|---|---|
| Channel pick | `**Sign-Up Channel**\nWhich channel should the bot post the sign-up post into?` | `setup_cog.py:~5837` |
| Day of week | `**Sign-Up Day of Week**\nWhich day of the week should the bot post the sign-up?` |  |
| Lead days | `**Sign-Up Lead Days**\nHow many days BEFORE the event should the post fire?` |  |
| Sign-up time | `**Sign-Up Time**\nWhat local time should the post fire? Use `HH:MM` 24-hour, e.g. `18:00`.` |  |

### 2.5 Tab name prompts (5)

For each of: `signups_tab`, `rosters_tab`, `attendance_tab`,
`strategies_tab`, `member_rules_tab`. All go through
`ask_keep_or_change`. Verbatim shape:

> **{Label} Tab**\nWhich Google Sheet tab should store {event_label}
> {label_text.lower()}? The bot creates and maintains this tab —
> leave the default if you don't have a preference.

Defaults come from `config.default_structured_tab(event_type, key)`.

### 2.6 Judicator role (CS-only)

Updated by **commit λ** with a keep-or-change branch.

| Re-entry state | Surface | Verbatim |
|---|---|---|
| Re-entry, role set | Three-button view (`_KeepOrChangeRoleGate`) | `**Judicator Role (💎 Premium — CS only)**\nCurrently set to **{role_label}**. Keep it, switch to no role, or pick a different role.` |
| Re-entry buttons | | `✅ Keep current: {label}` / `↩️ Skip — no role to apply` / `✏️ Change role` |
| First run / change | Full role picker | `**Judicator Role (💎 Premium — CS only)**\nPick the Discord role the bot should apply to members tagged as Judicator candidates (via `/cs_member_rule set_member_role`) after a CS roster is approved and matchmaking reveals **Rulebringers**. Skip if you don't use this — the bot won't apply any role.` |
| Picker placeholder | | `Pick the Judicator role (or skip)` |
| Skip sentinel option | | `Skip — no role to apply` |
| Confirmation | | `✅ Judicator role skipped.` OR `✅ Judicator role: <@&{id}>` |

### 2.7 Power-refresh DM toggle (Premium)

Updated by **commit λ** with a keep-or-change branch.

| Re-entry state | Surface |
|---|---|
| Re-entry, prior `structured_flow_enabled=True` | Two-button view (`_KeepOrFlipYesNoGate`): `✅ Keep current: Yes/No` + `↩️ Switch to: Yes/No` |
| First run | Standard `YesNoView` |

Prompt body (re-entry):

> **Power-Refresh DM (💎 Premium)**\nWhen a member clicks a sign-up
> button for **{label}** and their **{power_column or 'power column'}**
> cell is blank or unparseable, the bot can DM them a one-line
> nudge to update it. Currently **on/off** — keep it or flip.

Prompt body (first run):

> **Power-Refresh DM (💎 Premium)**\nWhen a member clicks a sign-up
> button for **{label}** and their **{power_column or 'power column'}**
> cell is blank or unparseable, should the bot DM them a one-line
> nudge to update it? At most one DM per member per event date.

---

## 3. Auto sign-up post + SignupView

Lives in `storm_signup_post.py` (post creator) +
`storm_signup_view.py` (View + click handler).

### 3.1 The auto-posted message

Header line + four buttons. Default body (alliance-customisable):

> ⚔️ **{event_label} sign-up — {event_date}**\nReply with **{a_label}**,
> **{b_label}**, **either**, or **cannot**.\nDeadline: **{deadline}**.

(Default in `defaults.STORM_DEFAULT_SIGNUP_BODY` style — alliance can
override via `/setup_desertstorm` → mail template.)

### 3.2 SignupView buttons

| Button | Style | Custom-id | File:line |
|---|---|---|---|
| `✅ {time_a_label}` (e.g. `✅ 4pm EDT`) | `success` | `signup:{guild}:{type}:{date}:a` | `storm_signup_view.py:97` |
| `✅ {time_b_label}` | `success` | `signup:{guild}:{type}:{date}:b` | `storm_signup_view.py:98` |
| `✅ Either time works` | `success` | `signup:{guild}:{type}:{date}:either` | `storm_signup_view.py:99` |
| `❌ Cannot participate` | `danger` | `signup:{guild}:{type}:{date}:cannot` | `storm_signup_view.py:100` |

### 3.3 Click handler ephemeral acks

| Trigger | Verbatim | File:line |
|---|---|---|
| Malformed custom_id | `⚠️ This sign-up button is from an older version. Wait for the next sign-up post to vote.` | `storm_signup_view.py:140` |
| Cross-guild leak | `⚠️ This sign-up post belongs to a different server. Please use the sign-up post in your alliance's channel.` | `storm_signup_view.py:163` |
| Premium-revoked guild | `⚠️ This sign-up post is no longer active because the structured roster flow has been disabled for this server.` | `storm_signup_view.py:191` |
| Successful vote | `✅ Vote recorded: **{Team A/Team B/Either/Cannot}**. You can change your vote any time before the event.` | `storm_signup_view.py:219` |

### 3.4 Power-refresh DM (commit 3370ea3, refined in λ)

Sent only when (a) `power_refresh_dm_enabled=1`, (b) voter's power
column cell isn't parseable, (c) no cooldown row for this event
already.

Verbatim body, with **commit λ's** "leading-your" strip applied:

> Heads up — your **{column_label}** on the alliance roster Sheet
> isn't readable. Could you update it before the next storm so
> leadership has accurate numbers for zone assignments?

`column_label` strips a leading `your`/`my` to avoid `your **Your Power**`.
See [storm_signup_view.py:323-345](../storm_signup_view.py#L323-L345).

---

## 4. Officer view (`/storm_signups`)

Lives in `storm_officer_view.py`. Posted ephemerally to the officer
who invoked `/storm_signups`. Contains the bucket-map embed + filter
+ refresh + on-behalf buttons.

### 4.1 Embed title + summary

| Surface | Verbatim |
|---|---|
| Title | `⚔️ {event_label} — {event_date} ({n} members)` |
| When no signups recorded | `📭 No signups recorded yet.` |
| Stale-roster warning | `⚠️ Roster Sheet read had issues — non-Discord member enumeration may be incomplete: {first 2 errors}` |
| Power-column warning (officer view) | _(soft warning surfaced inline)_ |

### 4.2 Bucket headers

| Bucket | Label | File:line |
|---|---|---|
| `a` | `✅ Team A — {n}` | `storm_officer_view._BUCKET_LABELS` |
| `b` | `✅ Team B — {n}` |  |
| `either` | `✅ Either time — {n}` |  |
| `cannot` | `❌ Cannot — {n}` |  |
| `not_voted` | `🕓 Not voted — {n} [{m} not on Discord]` |  |

Per-member entry shape: `• {display_name}` with `¹` superscript and
italic footnote when `is_on_behalf=True`, plus `²` (or similar) when
`not_on_discord=True`. Footnote text:

> ¹ on-behalf vote · ² not on Discord

### 4.3 Filter / refresh / on-behalf controls

| Control | Verbatim | File:line |
|---|---|---|
| Filter dropdown placeholder | `Filter bucket — currently: {bucket_filter or 'All'}` | `storm_officer_view.py:579` |
| Filter options | `All buckets` / `Team A` / `Team B` / `Either time` / `Cannot` / `Not voted` |  |
| Refresh button | `🔄 Refresh` | `storm_officer_view.py:~620` |
| On-behalf button | `✍️ Vote on behalf of…` |  |
| Build roster button | `🛡️ Build roster…` |  |

### 4.4 On-behalf modal

| Surface | Verbatim |
|---|---|
| Modal title | `Vote on behalf of a member` |
| Member field | `Member (name or @mention)` |
| Vote field | `Vote: A, B, Either, Cannot` |
| Success ack | `✅ Recorded on-behalf vote for **{display}**: **{label}**.` |
| Member not found | `⚠️ Couldn't resolve **{input}** to a roster member. Check the name or sync members first.` |
| Invalid vote | `⚠️ Vote must be one of A, B, Either, Cannot.` |

---

## 5. Roster builder

Lives in `storm_roster_builder.py`. Two entry paths: structured-mode
(via `🛡️ Build roster…` from the officer view) or free-tier-mode
(via `/ds_strategy apply` / `/cs_strategy apply`).

### 5.1 Embed (header + zones)

| Surface | Verbatim | File:line |
|---|---|---|
| Title | `🛡️ Roster Builder: {preset_name}{team_label}` | `storm_roster_builder.py:713` |
| Header — Storm tag | `🗺️ Desert Storm` or `🗺️ Canyon Storm` | `:716` |
| Header — floor reminder (DS) | `⚖️ Enforcing **Min A/B** floors for this team` | `:719` |
| Zones header (pool) | `**📋 Zones**` | `:724` |
| Zones header (paired) | `**📋 Zones** _(paired mode — each primary has a dedicated sub)_` | `:722` |
| Zone line shape | `• {zone}  ({n}/{cap})  — {member1} ({power1}) [⚠ override], …` (paired adds `+ sub {sub_name}`) | `_render_zone_line` |

### 5.2 Subs / pairing status

Pool mode:

> 🪑 **Subs ({n})**: {names}  
> 🪑 **Subs**: _(none)_

Paired mode (updated by **commit λ**):

> ⚠️ **Unpaired primaries ({n})**: {names} — pick a sub for each via the picker.  
> 🪑 **Sub pairings**: complete for every primary.  
> 🪑 **Overflow subs ({n})**: {names} — pair via the picker or send as bench.  *(NEW in λ)*

### 5.3 Filled + active zone + warnings

| Surface | Verbatim |
|---|---|
| Filled progress | `📊 **Filled:** {assigned} / {capacity}` |
| Active zone | `🎯 **Active zone:** **{zone}** — floor **{power}**` |
| Power_band-relaxed floor | `🎯 **Active zone:** **{zone}** — floor **{effective}** _(preset floor {preset} relaxed by power_band rule)_` |
| Below-floor toggle on | `👁️ Below-floor members visible in the picker.` |
| Unknown-power footnote | `_Members with no parseable power read as 'power unknown'; toggle the override to assign them anyway._` |

### 5.4 Auto-fill summary block

Surfaces only when `auto_fill_summary is not None`. Updated in
commit `4edc46d` to split paired-sub count from primary count.

> 🎯 **Auto-fill summary**  
> • Per-member rules applied: **{n}**  
> • Members slotted via a band-relaxed floor: **{n}**  
> • Auto-filled by power: **{n}**  
> • Auto-paired subs: **{n}**  _(paired mode only)_  
> • Gaps (power unknown, not slotted): **{n}** — {preview} (+{more} more)  
> • Conflicts: **{n}** — {preview} (+{more} more)  
> • Conflicts: **0**  _(when none)_

### 5.5 Action buttons

Row 0: zone select. Row 1: member select. Row 2: action buttons.
Row 3: finalisation.

| Button | Label | Row | Style | File:line | Notes |
|---|---|---|---|---|---|
| Zone select placeholder | `Pick a zone to edit…` | 0 | — | `:940` | One option per zone |
| Member select placeholder (eligible) | `Pick a member for {zone or 'a zone'}…  (+N more)` | 1 | — | `:975-988` | `+N more` appended by **commit λ** |
| Member select placeholder (none eligible) | `No eligible members — toggle below-floor override` | 1 | — | `:977` | |
| Toggle below-floor (off) | `👁️ Show below-floor` | 2 | secondary | `:1058` | |
| Toggle below-floor (on) | `👁️ Hide below-floor` | 2 | secondary | `:1057` | |
| Unassign current zone | `↩️ Unassign current zone` | 2 | secondary | `:1075` | |
| Last to subs | `🪑 Last to subs` | 2 | secondary | `:1094` | |
| Re-pair Sub *(NEW in λ; paired mode only)* | `🔁 Re-pair sub` | 2 | secondary | `:1116-1132` | |
| Auto-fill (structured) | `🎯 Auto-fill` | 2 | primary | `:1140` | |
| Approve & Post (structured) | `✅ Approve & Post` | 3 | success | `:1154` | |
| Preview mail (structured) | `📄 Preview mail` | 3 | secondary | `:1167` | |
| Generate mail (free-tier) | `📄 Generate mail` | 3 | primary | `:1179` | |
| Save as preset (free-tier) | `💾 Save as preset` | 3 | success | `:1191` | |
| Render image | `🖼️ Render image` | 3 | secondary | `:1207` | Available both modes |
| Cancel (structured) / Done (free-tier) | `❌ Cancel` / `✅ Done` | 3 | danger | `:1219` | |

### 5.6 Capacity / override / ownership errors

| Trigger | Verbatim |
|---|---|
| Zone full | `⚠️ **{zone}** is already full ({cap} members). Unassign someone before adding another.` |
| Below-floor without override | `⚠️ Toggle the below-floor override to assign this member.` |
| Owner-guard (non-owner click) | `⛔ Only the builder's owner can {action}.` |
| Cancel | `Roster builder cancelled — nothing posted.` |
| Done | `Roster builder closed.` |
| Render — Pillow missing | `⚠️ Image render isn't available — the host is missing Pillow. Use the text-template mail in the meantime.` |
| Render — other error | `⚠️ Couldn't render the roster image — see bot logs.` |
| Render — over 25 MB *(NEW in λ)* | `⚠️ Rendered roster image is too large to attach ({n} MB > 25 MB Discord limit). Use the text-template mail instead.` |
| Save preset — failed | `⚠️ Couldn't save preset — check that your Sheet is configured and the bot has edit access.` |

### 5.7 Paired sub picker

Updated by **commit λ** with the `+N more` overflow hint.

| Surface | Verbatim |
|---|---|
| Picker prompt (eligible pool) | `🪑 Pick a sub for **{primary}** at **{zone}**, or skip and pair them later.` |
| Picker prompt (truncated pool, NEW) | `…or skip and pair them later. *(+{n} more eligible — not shown; Discord limits the picker to 25)*` |
| Picker prompt (no eligible) | `🪑 No eligible subs found for **{primary}** at **{zone}**. Skip and pair them later, or toggle the below-floor override on the main view to widen the pool.` |
| Select placeholder | `Pick a paired sub…` |
| Skip button | `↩️ Skip — pair later` |
| Picked confirmation | `✅ Paired **{sub}** with **{primary}**.` |
| Skipped confirmation | `↩️ Skipped — you can pair this primary later.` |

### 5.8 Re-pair primary picker *(NEW in λ)*

Lives in `_open_repair_primary_picker` /
`_RepairPrimaryPickerView` (`storm_roster_builder.py:1545-1690`).

| Surface | Verbatim |
|---|---|
| Empty roster | `⚠️ No primaries assigned yet — assign a primary to a zone before re-pairing a sub.` |
| Picker prompt | `🔁 Pick a primary to re-pair their sub. The current pairing (if any) is shown next to each name.` |
| Picker prompt (overflow) | `…shown next to each name. *(+{n} more primaries — not shown; Discord limits the picker to 25)*` |
| Select placeholder | `Pick a primary to re-pair…` |
| Per-option description | `{zone} · sub: {sub_name}` or `{zone} · no sub paired` |
| Cancel button | `↩️ Cancel` |
| Cancel confirmation | `↩️ Re-pair cancelled.` |

### 5.9 Approve & Post — outcome

Lives in `_finalize_structured_roster` (`storm_roster_builder.py:1989+`).

| Trigger | Verbatim |
|---|---|
| Successful post | `✅ Approved & posted to <#{channel}>. Wrote **{n}** roster rows to **{tab}**.` |
| Channel not configured | `⚠️ No post channel configured for {event_label}. Set one via `/setup_desertstorm` / `/setup_canyonstorm` and try again.` |
| Channel deleted | `⚠️ Post channel is gone (likely deleted). Set a new one via `/setup_desertstorm` and try again.` |
| Send failure | `⚠️ Couldn't post the mail to <#{channel}> — likely a permissions issue (Send Messages / Embed Links). Fix and try Approve & Post again.` |
| Lock claimed by another officer | `⛔ Another officer (<@{user}>) is currently building this team. Wait for them to finish or cancel.` |

---

## 6. Faction roles (post-Approve & Post)

Lives in `storm_roster_builder._FactionRolesView`
(`storm_roster_builder.py:2226+`). Posted ephemerally to the officer
after a CS Approve & Post fires when:
  * `judicator_role_id` is configured for the guild;
  * at least one Judicator candidate is on the just-approved roster.

Updated by **commit μ** to include paired subs as candidates.

| Surface | Verbatim |
|---|---|
| Offer prompt | `⚔️ **Apply Faction Roles?**\nMatchmaking will reveal your faction post-roster. When you know it's **Rulebringers**, click below to apply the configured Judicator role to your candidates: {names}.` |
| Rulebringers button | `⚔️ Rulebringers (apply Judicator)` |
| Dawnbreakers button | `🪞 Dawnbreakers (no role to apply)` |
| Perms preflight failure | `⛔ Can't apply **{role}**: the bot is missing **Manage Roles** OR the bot's top role is below **{role}** in the hierarchy. Fix that and re-open the offer via `/storm_signups` → Build roster → Approve & Post.` |
| Rulebringers apply success | `✅ Applied **{role}** to {n}/{total} candidates: {names}.` |
| Rulebringers — all failed | `⚠️ Couldn't apply **{role}** to any candidate. Check the bot's role hierarchy + Manage Roles permission.` |
| Dawnbreakers ack | `✅ No role to apply for Dawnbreakers. Good luck out there!` |

---

## 7. Mail rendering

Default body template lives in `defaults.STORM_DEFAULT_MAIL_BODY`.
The roster builder substitutes `{zones}`, `{subs}`, `{date}`,
`{team}` etc. before posting.

### 7.1 Zone line format

Pool mode:

> **{zone}**  ({n}/{cap})  
> • {name1}  
> • {name2}  
> • _(empty)_

Paired mode (updated to include `+ sub`):

> **{zone}**  ({n}/{cap})  
> • {primary1}  + sub {sub1}  
> • {primary2}  + sub {sub2}

### 7.2 Sub list format

Pool mode:

> 🪑 **Subs ({n})**: {names}

Paired mode (overflow only):

> 🪑 **Overflow subs ({n})**: {names}

### 7.3 PNG image render

Lives in `storm_renderer.py`. Triggered by `🖼️ Render image` button.
Updated by **commit λ** to wrap the title and guard 25 MB.

Layout: vertical white canvas, 720 px wide.

| Section | Format |
|---|---|
| Title | Bold (`title_font=18`), wrapped to canvas width by `_wrap_text` |
| Zone heading | `{zone}  ({n}/{cap})` (blue heading_font=14) |
| Member line | `• {name} ({power}) ⚠ override   ↳ sub: {sub}` |
| Empty zone | `(empty)` (muted) |
| Subs heading | `Subs ({n})` |
| Special-roles line | `{Role title}: {comma-separated names}` |

---

## 8. Post-event attendance

Lives in `storm_attendance.py`. The `/storm_attendance` command
opens an ephemeral view for the officer to mark each roster slot
present/absent/late.

| Surface | Verbatim |
|---|---|
| Title | `📋 {Event label} attendance — {date} (Team {team})` |
| When no rosters_tab row for date | `⚠️ No roster found for **{date}** (Team {team}) in `{tab_name}`. Run Approve & Post on the roster builder first.` |
| Per-slot picker placeholder | `Pick the attendance state for {name}…` |
| Per-slot options | `✅ Present` / `❌ Absent` / `⏰ Late` |
| Save button | `💾 Save attendance` |
| Save success | `✅ Saved attendance for **{n}** members to `{tab}`.` |
| Save failure | `⚠️ Couldn't write attendance to `{tab}` — see bot logs.` |

---

## 9. History browser (`/ds_strategy roster_history` + `/cs_strategy roster_history`)

Lives in `storm_history.py`. Slash-command-invoked ephemeral
embed + date hop buttons.

| Surface | Verbatim |
|---|---|
| Title | `📜 {Event label} — {date}` |
| When no roster history for date | `⚠️ No roster on file for **{date}**.` |
| Team picker (DS) | `Pick a team to view…` with `Team A` / `Team B` options |
| Per-zone line | `**{zone}**  ({n} members)\n• {name} ({power})` (paired: adds `+ sub`) |
| Prev / next date buttons | `◀️ Previous date` / `▶️ Next date` |
| Attendance summary inline | `✅ {n_present}  ❌ {n_absent}  ⏰ {n_late}` |
| No attendance data | `_No attendance recorded yet._` |

---

## 10. Member rules (`/ds_member_rule`, `/cs_member_rule`)

Lives in `storm_member_rules.py`. Per-command ephemeral acks.

Universal "exactly one of picker / name" error
(`_SUBJECT_REQUIRED_MSG`, `storm_member_rules.py:76`):

> ⚠️ Provide a member. Pick from the typeahead (server member) OR
> type a roster name (non-Discord member) — exactly one, not both.

### 10.1 set_power_band

| Surface | Verbatim |
|---|---|
| Success | `✅ Saved: ≥ {power} → eligible for **{zone}**.{zone_warning}` |
| Zone-warning suffix | `\n⚠️ `{zone}` isn't in the canonical zone list — saved anyway; double-check the spelling.` |
| Validation: missing zone | `⚠️ `zone` is required.` |
| Validation: missing power | `⚠️ `min_power` is required.` |

### 10.2 set_member_team (DS only)

| Surface | Verbatim |
|---|---|
| CS guard | `⚠️ `team` rules only apply to Desert Storm. Use the zone or special_role commands for Canyon Storm.` |
| Invalid team | `⚠️ Team must be `A` or `B`. Got `{team}`.` |
| Success | `✅ Saved: **{display}** → plays **Team {team}**.` |

### 10.3 set_member_zone

| Surface | Verbatim |
|---|---|
| Missing zone | `⚠️ `zone` is required.` |
| Non-canonical zone | `\n⚠️ `{zone}` isn't in the canonical zone list — saved anyway; double-check the spelling.` |
| Success | `✅ Saved: **{display}** → always at **{zone}**.{zone_warning}` |

### 10.4 set_member_role

| Surface | Verbatim |
|---|---|
| Invalid role | `⚠️ Role must be `commander` or `judicator`. Got `{role}`.` |
| Success | `✅ Saved: **{display}** → **{Role}** candidate.` |

### 10.5 list

| Surface | Verbatim |
|---|---|
| Empty | `📭 No member rules saved yet.` |
| Header | `📋 **{Event label} member rules ({n})**` |
| Per-row | `• {render_label} — {sub_type}={value}` (notes appended in parens) |

Per **commit ξ**: typed names that match a guild member's display
name (case-insensitive, non-bot, unambiguous) are silently
normalised to the Discord ID at save time — alliance avoids
double-rules for the same person.

---

## 11. Strategy presets (`/ds_strategy`, `/cs_strategy`)

Lives in `storm_strategy.py`. Modals + ephemeral confirmations.

| Subcommand | Surface |
|---|---|
| `create` | `**Step 1 of 2 — Preset Name**\nWhat should this preset be called?` |
| `create — zone wizard` | Step 2 prompts for zone name + capacity + Min A + Min B (DS) per zone. |
| `edit` | Per-zone Edit / Add zone / Delete zone buttons; ephemeral. |
| `list — empty` | `📭 No saved presets yet. Use `/{ds,cs}_strategy create` to make one.` |
| `list — body` | `📋 **{Event label} presets ({n})**\n• **{name}** ({n_zones} zones)…` |
| `delete confirm` | `⚠️ Really delete preset **{name}**?` |
| `delete success` | `✅ Deleted preset **{name}**.` |
| `apply` | Opens the roster builder (see §5). |
| `roster_history` | Opens the history browser (see §9). |

---

## 12. `/storm_post_signup` (manual fire)

Lives in `storm_signup_post.py:197`. Manually post the sign-up post
for the next event date (when the auto-scheduler is off OR the
officer wants to fire early).

| Surface | Verbatim |
|---|---|
| Free-tier guard | `💎 **/storm_post_signup** is a Premium feature. See `/upgrade` for details.` |
| Structured flow off | `⚠️ Structured flow isn't enabled for {event_label}. Run `/setup_desertstorm` and opt in.` |
| No sign-up channel | `⚠️ No sign-up channel configured. Run `/setup_desertstorm` to set one.` |
| Success | `✅ Sign-up post fired in <#{channel}> for **{event_date}**.` |
| Recent duplicate guard | `⚠️ A sign-up post for **{event_date}** already exists in <#{channel}>. Use that one, or wait for the next event date.` |

---

## 13. `/sync_members` flow (with `Is this user in Discord?` column)

Lives in `member_roster.py`. Premium-gated.

| Surface | Verbatim |
|---|---|
| Free-tier guard | `💎 **/sync_members** is a Premium feature. See `/upgrade` for details.` |
| Not configured | `⚠️ Member-roster sync isn't configured yet. Run `/setup_members` first.` |
| Success | `✅ Synced **{n}** members to `{tab}`.` |
| Missing privileged intent (warn log only) | `[ROSTER] Guild {id}: only {cached}/{total} members in cache. Enable the SERVER MEMBERS INTENT in the Discord Developer Portal…` |

Post-write, the bot auto-maintains the **"Is this user in Discord?"**
column with Yes/No values + a Yes/No-dropdown data validation rule.
See §15.

---

## 14. Walkthrough tour

Lives in `storm_walkthrough.py`. Optional Premium onboarding view
posted after a fresh `/setup_desertstorm` or `/setup_canyonstorm`
completes with structured flow enabled.

| Page | Headline |
|---|---|
| Intro | `👋 Premium Storm tour — page 1 of {n}` |
| Sign-up post | `📣 Members vote with one click; on-behalf for non-Discord` |
| Officer view | `🛡️ Bucketed view, refresh, on-behalf modal` |
| Roster builder | `🛡️ Zone picker, eligibility gate, auto-fill` |
| Auto-fill summary | `🎯 Track what auto-fill did and where the gaps are` |
| Approve & Post | `✅ Posts the mail + writes rosters_tab + offers Faction Roles` |
| Attendance | `📋 Post-event attendance per slot` |
| History | `📜 Browse past rosters + attendance by date` |
| Member rules | `📋 Per-member + power-band overrides` |
| Wrap | `🎉 You're set — run `/setup_desertstorm` again to tweak.` |

Buttons: `◀️ Previous` / `▶️ Next` / `❌ Close tour`. Persistent
state is per-message; closing dismisses the ephemeral.

---

## 15. The "Is this user in Discord?" column (NEW in ο)

Bot-maintained column on the alliance's roster Sheet. Created
automatically by `/sync_members` (no manual setup required).

**Values**: `Yes` (member is in the live Discord guild, non-bot)
or `No` (blank ID, non-numeric ID, ID not in guild, ID is a bot).

**Data validation**: Yes/No dropdown via gspread `batch_update`
`setDataValidation` request, with `strict: true` (so manual entries
outside Yes/No are rejected by Sheets).

**Read precedence** (in `_read_roster_powers` + `_read_roster_rows`):
  1. `Is this user in Discord?` column (Yes → on Discord, No → not).
  2. Legacy `not_on_discord` column (truthy → not on Discord) for
     back-compat.
  3. ID-diff inference (existing path) when both columns are absent
     or blank.

**Override semantics**: the bot overwrites this column on every
`/sync_members`. If the alliance needs a manual override for an
alt-account edge case, they should use the legacy `not_on_discord`
column or the `Is this user in Discord?` column will be auto-flipped
back on next sync. Future enhancement: alliance-override row that
the bot respects.

---

## 16. Free-tier surfaces (in scope for the Premium flow)

These free-tier commands sit *adjacent* to the Premium flow — the
audit covers them only when they intersect (e.g. `/ds_strategy`
preset management is free-tier; only `apply` opens the structured
roster builder under Premium).

| Free-tier command | Premium intersection |
|---|---|
| `/ds_strategy create|edit|list|delete` | None — full free-tier surface for preset library mgmt. |
| `/ds_strategy apply` | Premium-only when invoked with `event_date` (structured mode). |
| `/ds_member_rule *` | Premium-only when read by the roster builder's auto-fill (but rules can be CREATED in free-tier). |
| `/setup_members` + `/sync_members` | Premium-only; populates the roster Sheet the storm flow reads. |

---

## 17. Footer/footnote conventions

Recurring patterns observed across this audit:

- Every ephemeral storm response prefixed with one of the emoji
  glyphs from the palette table at the top.
- "X — Y" em-dash separates the actor from the action (`Roster
  builder — Auto-fill applied`).
- Timeouts always reference the slash command to re-open
  (`⏰ Timed out. Run `/storm_signups` to start again.`).
- Soft warnings (`⚠️`) never block flow; permission denials
  (`⛔`) do block.
- Numeric counts are bold (`**{n}**`); names are bold (`**{name}**`);
  zones are bold (`**{zone}**`).
- Server-time references always say "server time" verbatim — never
  abbreviated to "ST" (per `1.1.3`).

---

## 18. Recent storm-related copy changes (audit trail)

| Commit | Change |
|---|---|
| `acfb6c6` (λ) | `+N more` overflow indicator on member picker + paired-sub picker; Overflow subs line in paired-mode embed; Re-pair Sub button + primary picker; 25 MB attachment guard; PNG title wrap; DM body leading-"your" strip; Judicator/power-refresh DM keep-or-change branches |
| `e5b9f6e` (μ) | Faction roles offer text unchanged; underlying candidate set now includes paired subs |
| `fb73dbb` (ν) | No user-facing copy change; bot filter + cache pre-pass are silent |
| `7d950a9` (ξ) | No user-facing copy change; typed-name normalisation is silent at save time |
| `a3b54fe` (ο) | No user-facing storm copy change; new "Is this user in Discord?" column appears on roster Sheet but storm UX is unchanged |
| `97045d9` | Soft-warning text refined for stale-ID + bot filter; surface message kept |
| `3370ea3` | Power-refresh DM body initial wording (refined by λ) |
| `99b784e` | Faction roles preflight + summary wording added |
| `07e158b` | PNG render output (initial body); refined by λ |
| `cab4809` | Paired sub picker initial wording; refined by λ |
| `f80c092` | Three-way subject resolution helper; surface text unchanged |
| `135b35a` | First sign-up auto-post body landed |

---

## 19. Open polish items (not in this PR series)

These are user-facing strings that surfaced in the audit but aren't
priority enough to ship in this batch:

- **Mail body customisation flow**: `/setup_desertstorm` mail
  template wizard inherits free-tier copy that doesn't reference
  the new structured fields (signups_tab, rosters_tab,
  attendance_tab). The default body works, but the wizard prompts
  could surface "your alliance can read attendance from
  `{attendance_tab}` after the event."
- **Walkthrough tour pagination footer**: `Page N of M` would help
  officers know how long the tour is. Currently just shows
  page-content emoji.
- **Audit per-bucket label override**: alliances who want
  `Team Alpha` / `Team Bravo` instead of A/B can't override the
  bucket-map labels in `/storm_signups` today.

Track in [#54](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/54)
if/when these surface as friction in onboarding.
