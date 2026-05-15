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
| 🅰️ / 🅱️ | Team A / Team B (sign-up buttons, bucket labels, Set-up buttons — replaced ✅ in the team-test refresh) |
| 🔄 | Either time works (DS only) / Refresh / Status: Sub activated |
| 📋 | Zone list / sub list / strategy preset |
| 🪑 | Subs (any sub list — flat or paired) |
| 🎯 | Active zone / auto-fill |
| 🔀 | Phase mode / phase navigation (#152) |
| 🙋 | Record on-behalf vote |
| ❓ | Not voted yet (officer-view bucket) |
| 🪞 | Faction reveal / matchmaking outcome |
| 🔁 | Re-pair / swap |
| 🖼️ | Image render |
| 📄 | Mail preview / generate mail |
| 💾 | Save preset / saved config |
| ✅ | Yes / Use default / Keep current / Approve & Post / Status: Attended |
| ❌ | No / Cancel / Cannot participate / Status: No-show |
| ⛔ | Permission denied |
| ⚠️ | Validation warning / soft error |
| ℹ️ | Informational (stale-vote redirect, capacity flex room) |
| 🔒 | Premium-locked feature gate |
| ⏰ | Timeout |

The flow is **mixed ephemeral / public**: the roster-builder views
(`signups → 🛡️ Set up Team X` under the parent group), preset editor, and most
follow-up pickers are ephemeral so leadership chat stays clean. The
officer view (`signups`), attendance (`attendance`) — both under `/desertstorm` or `/canyonstorm`,
preset editor (`/desertstorm strategy edit`), and Approve & Post mail are
**public by design** — they double as a leadership audit trail so
multiple officers can see what's happened across a session.

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
| **Teams** (`teams`) | DS-only per-alliance config: `both` (default) / `A` / `B`. Single-team alliances see only their team's button on the sign-up post + a single Set-up-Team button in `/desertstorm signups` (#148). |
| **Phase** | DS/CS event sub-window. Phase 1 = outer ring active; Phase 2 = central buildings (Arsenal / Silo / Mercenary Factory) open up; Phase 3 = late-event CS state. |
| **Flat preset** | `phase_count = 0` — the original shape. One capacity, one assignment per zone. |
| **Phase-aware preset** | `phase_count = 2` or `3` — per-phase capacities + assignments, mail/render groups by phase. |
| **Phase migration** | Same member assigned to different zones across phases (e.g. Alice plays Info Center in P1, Nuclear Silo in P2). |

---

## 1. Slash commands

Every Premium Storm surface starts with one of these. Commands are
sorted by where the user normally encounters them.

| Command | Description | File |
|---|---|---|
| `/setup_desertstorm` | Configure Desert Storm mail template and time options | `setup_cog.py:1507` |
| `/setup_canyonstorm` | Configure Canyon Storm mail template and time options | `setup_cog.py:1522` |
| `/sync_members` | Force a fresh `/sync_members` write to the alliance roster Sheet | `member_roster.py:430` |
| `post_signup` (under `/desertstorm` or `/canyonstorm` parent group) | Manually post a sign-up post for the next storm event | `storm_signup_post.py:197` |
| `signups` (under `/desertstorm` or `/canyonstorm` parent group) | Open the officer view (bucket map + filter + on-behalf) | `storm_officer_view.py:801` |
| `attendance` (under `/desertstorm` or `/canyonstorm` parent group) | Post-event attendance tracker (Premium) | `storm_attendance.py:703` |
| `/desertstorm strategy create|edit|list|delete|apply|roster_history` | DS strategy preset management | `storm_strategy.py:935-983` |
| `/canyonstorm strategy create|edit|list|delete|apply|roster_history` | CS strategy preset management | `storm_strategy.py:983-1009` |
| `/desertstorm member_rule set_power_band|set_member_team|set_member_zone|set_member_role|list` | DS per-member + power-band rules | `storm_member_rules.py:734-824` |
| `/canyonstorm member_rule set_power_band|set_member_zone|set_member_role|list` | CS per-member + power-band rules (no `team` — CS has no teams) | `storm_member_rules.py:825-882` |

Premium gating is enforced at command entry via
`storm_permissions.ensure_premium_structured(...)`. Free-tier
guilds see this ephemeral on the gated commands:

> 🔒 {feature_label} is a 💎 Premium feature. Run `/upgrade` to unlock it.

(canonical phrasing in `storm_permissions.py:~103` — every
storm-gated surface routes through the same helper.)

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

The sign-up time prompt was refreshed by the team-test triplet
(commit 8ee0773) to match the 12-hour, tz-annotated copy the rest
of the wizard family uses (train reminder, birthday, shiny).

| Surface | Verbatim | File:line |
|---|---|---|
| Channel pick | `**Sign-Up Channel**\nWhich channel should the bot post the sign-up post into?` | `setup_cog.py:~5837` |
| Day of week | `**Sign-Up Day of Week**\nWhich day of the week should the bot post the sign-up?` |  |
| Lead days | `**Sign-Up Lead Days**\nHow many days BEFORE the event should the post fire?` |  |
| Sign-up time prompt | `**Sign-Up Time**\nWhat time should the bot fire the sign-up post? *(in your timezone: {tz_label})*\ne.g. \`2:00pm\`, \`9:00am\`, or 24-hour \`14:00\`.` | `setup_cog.py:~5519` |
| Sign-up time modal label | `e.g. 2:00pm — blank for manual` |  |

Storage stays 24-hour `HH:MM`. Keep-current button renders saved
24-hour values back through `_format_24h_to_12h` so the re-entry
button label reads e.g. `✅ Keep current: 2:00pm`.

### 2.4a Teams selector (DS only — #148)

Added by commit `b814152` (PR #150). Collected via a 3-button view
between sub-mode and sign-up channel:

| Surface | Verbatim |
|---|---|
| Prompt | `**Which Teams Run Desert Storm?**\nMost alliances run both Team A and Team B. Single-team alliances see only their team's button on the sign-up post, fewer Set-up buttons on /desertstorm signups, and zone-min-power inputs scoped to their team in the preset editor.` |
| Button — both | `🅰️🅱️ Both teams` |
| Button — A only | `🅰️ Team A only` |
| Button — B only | `🅱️ Team B only` |
| Keep current (re-entry) | `✅ Keep current: A & B / A only / B only` |
| Re-entry summary line | `**Teams:** A & B / A only / B only` |

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
| First run / change | Full role picker | `**Judicator Role (💎 Premium — CS only)**\nPick the Discord role the bot should apply to members tagged as Judicator candidates (via `/canyonstorm member_rule set_member_role`) after a CS roster is approved and matchmaking reveals **Rulebringers**. Skip if you don't use this — the bot won't apply any role.` |
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

Button shape depends on `event_type` + `guild_storm_config.teams`
(#148). All four button styles use the team-prefixed `🅰️ Team A: …`
form so members can tell the side at a glance — the team-test
session surfaced confusion when buttons rendered as bare times.

**Desert Storm — `teams=both` (default, 4 buttons):**

| Button | Style | Custom-id | File:line |
|---|---|---|---|
| `🅰️ Team A: {time_a_label}` (e.g. `🅰️ Team A: 9pm ET (18:00 server time)`) | `success` | `signup:{guild}:ds:{date}:a` | `storm_signup_view.py:101` |
| `🅱️ Team B: {time_b_label}` | `success` | `signup:{guild}:ds:{date}:b` | `storm_signup_view.py:104` |
| `🔄 Either time works` | `success` | `signup:{guild}:ds:{date}:either` | `storm_signup_view.py:107` |
| `❌ Cannot participate` | `danger` | `signup:{guild}:ds:{date}:cannot` | `storm_signup_view.py:109` |

When `time_a_label`/`time_b_label` is empty (e.g. on
`register_persistent_signup_views` re-registration with no saved
labels), the bare team-name label fires (`🅰️ Team A` without the
trailing colon).

**Desert Storm — `teams="A"` or `teams="B"` (2 buttons):**

Only the relevant team's button + `❌ Cannot participate`. Embed's
"Available time slots" line omits the unused slot.

**Canyon Storm (always 2 buttons):**

CS has one time-slot per faction, so only `🅰️ Team A: {time_a_label}`
+ `❌ Cannot participate` render. `_force_all_buttons=True` (used by
the persistent-view re-registration path) overrides this so
pre-hotfix CS posts with 4 buttons still route clicks correctly
after a bot restart.

### 3.3 Click handler ephemeral acks

| Trigger | Verbatim | File:line |
|---|---|---|
| Malformed custom_id | `⚠️ This sign-up button is from an older version. Wait for the next sign-up post to vote.` | `storm_signup_view.py:149` |
| Cross-guild leak | `⚠️ This sign-up post belongs to a different server. Please use the sign-up post in your alliance's channel.` | `storm_signup_view.py:172` |
| Stale CS Team B / Either (#152 / batch 2) | `ℹ️ This sign-up post is from before Canyon Storm switched to a single-team format. Team B / Either time aren't valid for CS — vote on the next sign-up post (it'll only show Team A + Cannot).` | `storm_signup_view.py:~200` |
| Stale Team B vote on `teams=A` alliance (#148) | `ℹ️ This alliance is configured as **Team A only**. Team B / Either aren't valid choices — pick **Team A** or **Cannot participate** on the next sign-up post.` | `storm_signup_view.py:~225` |
| Stale Team A vote on `teams=B` alliance | `ℹ️ This alliance is configured as **Team B only**. Team A / Either aren't valid choices — pick **Team B** or **Cannot participate** on the next sign-up post.` | `storm_signup_view.py:~240` |
| Premium-revoked guild | `⚠️ This sign-up post is no longer active because the structured roster flow has been disabled for this server.` | `storm_signup_view.py:~280` |
| Successful vote | `✅ Vote recorded: **{Team A/Team B/Either/Cannot}**. You can change your vote any time before the event.` | `storm_signup_view.py:~310` |

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

## 4. Officer view (`signups` under `/desertstorm` or `/canyonstorm`)

Lives in `storm_officer_view.py`. Posted ephemerally to the officer
who invoked `/desertstorm signups` or `/canyonstorm signups`. Contains the bucket-map embed + filter
+ refresh + on-behalf buttons.

### 4.1 Embed title + summary

| Surface | Verbatim |
|---|---|
| Title | `⚔️ {event_label} — {event_date} ({n} members)` |
| When no signups recorded | `📭 No signups recorded yet.` |
| Stale-roster warning | `⚠️ Roster Sheet read had issues — non-Discord member enumeration may be incomplete: {first 2 errors}` |
| Power-column warning (officer view) | _(soft warning surfaced inline)_ |

### 4.2 Bucket headers

Labels refreshed in the team-test session (commit 9dfbf40) — the
`✅ Team A — {n}` shape was confusing officers who interpreted ✅
as "voted to attend" rather than the team marker. Now uses the same
🅰️/🅱️/🔄 glyphs as the SignupView buttons.

| Bucket | Label | File:line |
|---|---|---|
| `a` | `🅰️ Voted Team A` | `storm_officer_view._BUCKET_LABELS` |
| `b` | `🅱️ Voted Team B` |  |
| `either` | `🔄 Voted Either` |  |
| `cannot` | `❌ Voted Cannot` |  |
| `not_voted` | `❓ Not voted yet` |  |

Per-member entry shape: `• {display_name}` with `¹` superscript and
italic footnote when `is_on_behalf=True`, plus `²` (or similar) when
`not_on_discord=True`. Footnote text:

> ¹ on-behalf vote · ² not on Discord

### 4.3 Filter / refresh / on-behalf controls

| Control | Verbatim | File:line |
|---|---|---|
| Filter dropdown placeholder | `Filter bucket — currently: {bucket_filter or 'All'}` | `storm_officer_view.py:~605` |
| Filter options | `All buckets` / `🅰️ Voted Team A` / `🅱️ Voted Team B` / `🔄 Voted Either` / `❌ Voted Cannot` / `❓ Not voted yet` |  |
| Refresh button | `🔄 Refresh` | `storm_officer_view.py:~680` |
| On-behalf button | `🙋 Record on-behalf vote` | `storm_officer_view.py:~639` |
| Set up Team A button (DS, when `teams=both` or `teams=A`) | `🅰️ Set up Team A` | `storm_officer_view.py:~707` |
| Set up Team B button (DS, when `teams=both` or `teams=B`) | `🅱️ Set up Team B` | `storm_officer_view.py:~710` |
| Set up Roster button (CS) | `🏜️ Set up Roster` | `storm_officer_view.py:~725` |

Single-team DS alliances (`teams=A` / `teams=B`) only see their
team's Set-up button — wiring (#148) added in PR #150 (commit
`a8ad4bd`). View captures `view.message` at send time so
`on_timeout` (added in PR #150's batch 1) can edit the post with
the canonical `⏰ The actions for this have timed out — use
/desertstorm signups (or /canyonstorm signups) to re-initiate` notice.

### 4.4 On-behalf modal

| Surface | Verbatim |
|---|---|
| Modal title | `Record vote on behalf` |
| Member field | `Member name (must match your roster Sheet)` |
| Vote field | `Vote: A / B / Either / Cannot` |
| Numeric-name reject (#150) | `⚠️ On-behalf names can't be purely numeric — they collide with Discord IDs in storage. Use a non-numeric roster name (e.g. add an alliance prefix or member tag).` |
| Invalid vote / blank | `⚠️ I couldn't read that. Member name and one of A, B, Either, or Cannot. Try again.` |
| Member not found in roster | `⚠️ I don't see **{name}** in your roster Sheet. Check the spelling (it must match the name column on the roster tab) and try again.` |
| Success | `✅ Recorded on-behalf vote for **{display}**.` |
| Record failure | `⚠️ Couldn't record that vote. Check the bot logs.` |

---

## 5. Roster builder

Lives in `storm_roster_builder.py`. Two entry paths: structured-mode
(via `🛡️ Build roster…` from the officer view) or free-tier-mode
(via `/desertstorm strategy apply` / `/canyonstorm strategy apply`).

### 5.1 Embed (header + zones)

| Surface | Verbatim | File:line |
|---|---|---|
| Title | `🛡️ Roster Builder: {preset_name}{team_label}` | `storm_roster_builder.py:713` |
| Header — Storm tag | `🗺️ Desert Storm` or `🗺️ Canyon Storm` | `:716` |
| Header — floor reminder (DS) | `⚖️ Enforcing **Min A/B** floors for this team` | `:719` |
| Header — active phase (phase-aware presets, #152) | `🔀 Editing **Phase {n}**` | `:~906` |
| Zones header (pool) | `**📋 Zones**` | `:724` |
| Zones header (paired) | `**📋 Zones** _(paired mode — each primary has a dedicated sub)_` | `:722` |
| Zone line shape (flat preset) | `• {zone}  ({n}/{cap})  — {member1} ({power1}) [⚠ override], …` (paired adds `+ sub {sub_name}`) | `_render_zone_line` |
| Zone line shape (phase-aware preset) | `• {zone}  (P1: {n}/{cap}, P2: {n}/{cap}[, P3: {n}/{cap}]) — {selected-phase member list}` (per-phase counts iterate `iter_phases()`; member listing is for the selected phase only) | `_render_zone_line` |

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

Row layout adapts to phase-aware presets (#152). Flat presets keep
the original 4-row layout. Phase-aware presets reserve **row 0**
for the phase navigation buttons, pushing every other component down
one row.

**Flat:** row 0 = zone select, row 1 = member select, row 2 = action
buttons, row 3 = finalisation.

**Phase-aware:** row 0 = phase nav, row 1 = zone select, row 2 =
member select, row 3 = action buttons, row 4 = finalisation.

| Button | Label | Row (flat / phased) | Style | File:line | Notes |
|---|---|---|---|---|---|
| Phase nav *(phase-aware only, one per phase from `iter_phases()`)* | `Phase {n}` (active phase suffixed ` •`) | — / 0 | `primary` for active, `secondary` for others | `:~1147` | Renders 2 buttons for `phase_count=2`, 3 for `phase_count=3` (commit aafb576) |
| Zone select placeholder (flat) | `Pick a zone to edit…` | 0 / 1 | — | `:~1187` | |
| Zone select placeholder (phase-aware) | `P{n}: {zone} ({count}/{cap})` per option | 0 / 1 | — | `:~1179` | Counts reflect the selected phase |
| Member select placeholder (eligible) | `Pick a member for {zone or 'a zone'}…  (+N more)` | 1 / 2 | — | `:~975` | `+N more` appended by **commit λ** |
| Member select placeholder (none eligible) | `No eligible members — toggle below-floor override` | 1 / 2 | — | `:~977` | |
| Toggle below-floor (off) | `👁️ Show below-floor` | 2 / 3 | secondary | `:1058` | |
| Toggle below-floor (on) | `👁️ Hide below-floor` | 2 / 3 | secondary | `:1057` | |
| Unassign current zone | `↩️ Unassign current zone` | 2 / 3 | secondary | `:1075` | Operates on selected phase |
| Last to subs | `🪑 Last to subs` | 2 / 3 | secondary | `:1094` | Selected phase |
| Re-pair Sub *(paired mode only)* | `🔁 Re-pair sub` | 2 / 3 | secondary | `:1116` | Operates on selected phase |
| Auto-fill (structured) | `🎯 Auto-fill` | 2 / 3 | primary | `:1140` | Auto-fill fills ALL phases for phase-aware presets (#152) |
| Approve & Post (structured) | `✅ Approve & Post` | 3 / 4 | success | `:1154` | |
| Preview mail (structured) | `📄 Preview mail` | 3 / 4 | secondary | `:1167` | |
| Generate mail (free-tier) | `📄 Generate mail` | 3 / 4 | primary | `:1179` | |
| Save as preset (free-tier) | `💾 Save as preset` | 3 / 4 | success | `:1191` | Preserves `phase_count` + per-phase capacities (#152) |
| Render image | `🖼️ Render image` | 3 / 4 | secondary | `:1207` | Available both modes; phase-aware presets render one zone block per (phase, zone) |
| Cancel (structured) / Done (free-tier) | `❌ Cancel` / `✅ Done` | 3 / 4 | danger | `:1219` | |

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
| Perms preflight failure | `⛔ Can't apply **{role}**: the bot is missing **Manage Roles** OR the bot's top role is below **{role}** in the hierarchy. Fix that and re-open the offer via `/desertstorm signups` (or `/canyonstorm signups`) → Build roster → Approve & Post.` |
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

### 7.3 Phase-aware mail (#152)

Phase-aware presets emit one block per phase from `iter_phases()`,
separated by `**Phase {n}**` headers so leadership can copy-paste
the full event line-up into a single in-game mail. Flat presets
emit one block with no phase header.

> **Phase 1**  
> _zone block — same shape as §7.1_  
>   
> **Phase 2**  
> _zone block_  
>   
> **Phase 3** *(when `phase_count = 3`)*  
> _zone block_  

The Phase-3 block was missing pre-`aafb576` — the engine fills it,
rosters_tab records it, but the mail builder hardcoded P1+P2 only.

### 7.4 PNG image render

Lives in `storm_renderer.py`. Triggered by `🖼️ Render image` button.
Replaced by **#140**: the original vertical text canvas (`#132` v1)
is gone; the renderer now produces a map-based image with icons,
faction-coloured spawn zones, and a Subs column. Layout coordinates
match the alliance lead's SVG mocks (`ds_layout.svg`,
`CS_layout.svg`) 1:1.

**Delivery flow** (#140 follow-up): clicking the button posts the
PNG as a **public** message in the channel the builder was opened
in, so other leaders in that channel can see + save the image
directly. The officer who clicked the button then sees an ephemeral
followup with three action buttons:

| Button | What it does |
|---|---|
| `📥 Download` | DMs the same PNG to the clicking officer. DMs surface "save to device" prominently on both desktop (right-click) and mobile (long-press → save to camera roll). DM-blocked → graceful ephemeral falls back to "right-click the channel image." |
| `💾 Save to history` | Writes `(channel_id, message_id, team, posted_by, posted_at)` to the new `storm_roster_images` SQLite table. The history browser (`/<event> strategy roster_history`) gets a `📷 View Team A/B image` (DS) / `📷 View image` (CS) button per saved pointer. Image bytes stay in Discord — only the pointer is stored. UPSERT on (guild, event_type, event_date, team) so re-saving overwrites. Refuses cleanly if `session.event_date` is empty (manual / free-tier builder). |
| `📢 Post to channel...` | Channel picker → caption modal → re-posts the same PNG (with the optional caption as the message body) into the chosen channel. Useful when the configured `post_channel` is for mail and the alliance wants the image in a different channel (a strategy / leadership channel, an announcements channel, etc.). |

| Surface | Verbatim |
|---|---|
| Initial ephemeral copy | `🖼️ Roster image posted above. Pick an action below — only you'll see this prompt.` |
| Bot lacks Send-Messages perms in channel | `🖼️ Roster image attached (couldn't post publicly — check the bot's permissions in this channel):` — falls back to attaching the PNG ephemerally so the officer still gets it, just no public copy + no action bar. |
| Pillow missing | `⚠️ Image render isn't available — the host is missing Pillow. Use the text-template mail in the meantime.` |
| Other Pillow error | `⚠️ Couldn't render the roster image — see bot logs.` |
| > 25 MB attachment | `⚠️ Rendered roster image is too large to attach ({N} MB > 25 MB Discord limit). Use the text-template mail instead.` |
| Download — success | `📥 Sent to your DMs — check your direct messages with the bot.` |
| Download — DMs blocked | `⚠️ I can't DM you — your privacy settings block bot DMs. Right-click the image in the channel and use Save image instead.` |
| Download — DM body | `📥 Here's the roster image you asked to download (from {ET} on {date}). Right-click → Save image, or tap → save on mobile.` |
| Save — success | `💾 Saved. The image is now linked from \`/{parent} strategy roster_history\` for this event date (stays available until the original message is deleted).` |
| Save — no event date | `⚠️ Can't save to history without an event date — open the roster from \`/desertstorm signups\` / \`/canyonstorm signups\` so the event date is set.` |
| Post-to-channel — picker prompt | `📢 Pick a channel to post this image to. You'll get a modal to add an optional caption.` |
| Post-to-channel — modal title | `Post roster image to channel` (caption field placeholder: `e.g. Saturday's Desert Storm — final assignments`) |
| Post-to-channel — success | `📢 Posted to {channel mention}.` |
| Post-to-channel — Forbidden | `⚠️ I don't have permission to post in {channel}. Check the channel's permissions and try a different channel.` |
| Action view timeout (15 min) | View buttons disable in place — no notice; the public image is already in the channel and the officer can re-render to get a fresh action bar. |

**Deliberate scope**: the rendered PNG is a *shareable artifact* — it
shows zone names + member names + paired-sub formatting only. Per-
member power values, `⚠ override` markers, and `special_roles` are
NOT drawn (those are leadership-internal signals surfaced in the
builder embed and mail body, not on the public image).

Canvas: SVG-units × `SCALE` (2 by default). DS = ~2215 × 1525, CS =
~2471 × 2090. Inter is the project font (bundled at
`assets/fonts/Inter-{Regular,Bold}.ttf`). Icons live at
`assets/storm_icons/{ds,cs}/`.

Dispatches on `roster.event_type`:

| Layer | DS | CS |
|---|---|---|
| Header bar | `Desert Storm — {preset_name}` / `Team {A|B}` / date | `Canyon Storm — {preset_name}` / `{faction}` / date |
| Background | Sand fill, subs column right of `x=921` with 1 px black stroke (vertical separator) | Sand fill, subs column right of `x=1050` with same stroke |
| Spawn zones | Two narrow vertical strips at left/right edges: Team A blue, Team B red | Rulebringers blue horizontal band top; Dawnbreakers red split into two bands bottom |
| Zone count | 11 canonical zones in a diamond around Nuclear Silo | 12 canonical zones in 3 rows (Stage 1 top, Stage 2 middle, Stage 3 bottom) |
| Phase headers in member pill | `Stage/Phase {N}:` bold (8 pt Inter Bold) | Same |
| Member name | 8 pt Inter Regular, auto-shrinks when content overflows |  Same |
| Missing icon (Arsenal / Mercenary Factory, blocked on game-bug fix) | Grey placeholder circle | N/A — CS icon set is complete |
| Subs column | Flat list when `paired_subs` empty; two-column `Primary` / `Sub` table with row dividers when populated | Same |

`roster_from_session` plumbs:

* `event_type` → drives layout dispatch
* `preset_name` → header left text (combined with event name)
* `team_label` → header center (`Team A` / `Rulebringers` / etc.)
* `event_date_label` → header right (formatted via `format_event_date`)
* `phase_count` → controls per-phase iteration
* Each `RosterZone` carries `phase` + `canonical_zone` so the renderer
  groups by zone and stacks per-phase blocks inside one text pill.

Unknown / typo zone names are logged at DEBUG and skipped (vs.
crashing). Pillow missing → `RuntimeError` so the caller can fall
back to a text-only post.

---

## 8. Post-event attendance

Lives in `storm_attendance.py`. The `/desertstorm attendance` and `/canyonstorm attendance` commands
open an ephemeral view for the officer to mark each roster slot.
Status codes were renamed during the team-test session (commit
9dfbf40) to read more naturally for storm context.

`load_rostered_slots` dedupes `(team, zone, member)` so a member
playing the same zone across multiple phases (the "Alice stays put"
case in phase-aware presets) shows ONCE in the picker — not N
times. Migration members keep their N rows because their `(team,
zone)` keys differ across phases (#152 / aafb576).

| Surface | Verbatim |
|---|---|
| Title | `📋 {Event label} attendance — {date} (Team {team})` |
| Date parsing (event_date arg) | Routes through `storm_date_helpers.parse_event_date` — accepts ISO, `5/18`, `May 18`, `yesterday`, etc. |
| When no rosters_tab row for date | `⚠️ No structured roster found for **{date}** ({Event label}).` |
| Per-slot picker placeholder | `Record attendance for **{member}** ({zone or 'sub'}):` |
| Per-slot options (3 + clear) | `✅ Attended` / `❌ No-show` / `🔄 Sub activated` / `↩️ Clear` |
| Bulk: mark unrecorded → attended | `✅ Mark unrecorded → Attended` |
| Save button | `💾 Save attendance` |
| Save success | `✅ Saved attendance for **{n}** members to \`{tab}\`.` |
| Save partial failure | `⚠️ Attendance partially saved — {first error}` |

---

## 9. History browser (`/desertstorm strategy roster_history` + `/canyonstorm strategy roster_history`)

Lives in `storm_history.py`. Slash-command-invoked ephemeral
embed. The `event_date` argument now accepts the full flexible-date
parser surface (PR #147 — commit `4029f55`): ISO `2026-05-18`, US
`5/18`, long/short month names `May 18` or `may 18`, ordinal
suffixes (`18th`), weekday names (`saturday`), and the relative
keywords `today` / `tomorrow` / `yesterday`. Without `event_date`,
the command lists the most recent 8 events as clickable date
buttons (NOT directional Previous / Next — directional nav is on
the audit's open follow-up list).

| Surface | Verbatim |
|---|---|
| Title | `📜 {Event label} — {date_pretty}` (date rendered via `format_event_date`, e.g. `May 18, 2026`) |
| Parse failure | `⚠️ \`{event_date}\` isn't a date I can parse. Try \`May 18\`, \`5/18\`, \`2026-05-18\`, or \`yesterday\`.` |
| When no roster history for date | `⚠️ No roster on file for **{date}**.` |
| Per-zone line | `**{zone}**  ({n} members)\n• {name} ({power})` (paired: adds `+ sub`) |
| Date-list buttons (no `event_date`) | 8 buttons per event date (most recent first); click sends the date-detail embed as an ephemeral followup |
| Attendance summary inline | `✅ {n_present}  ❌ {n_absent}  ⏰ {n_late}` |
| No attendance data | `_No attendance recorded yet._` |
| Phase-aware roster (#152) | `rosters_tab` now has a `Phase` column at index 2; multi-phase rosters are grouped by phase in the embed via `iter_phases()` |

---

## 10. Member rules (`/desertstorm member_rule`, `/canyonstorm member_rule`)

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

## 11. Strategy presets (`/desertstorm strategy`, `/canyonstorm strategy`)

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

## 12. `/desertstorm post_signup` / `/canyonstorm post_signup` (manual fire)

Lives in `storm_signup_post.py:197`. Manually post the sign-up post
for the next event date (when the auto-scheduler is off OR the
officer wants to fire early).

| Surface | Verbatim |
|---|---|
| Free-tier guard | `💎 **/desertstorm post_signup** (or **/canyonstorm post_signup**) is a Premium feature. See `/upgrade` for details.` |
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
audit covers them only when they intersect (e.g. `/desertstorm strategy`
preset management is free-tier; only `apply` opens the structured
roster builder under Premium).

| Free-tier command | Premium intersection |
|---|---|
| `/desertstorm strategy create|edit|list|delete` | None — full free-tier surface for preset library mgmt. |
| `/desertstorm strategy apply` | Premium-only when invoked with `event_date` (structured mode). |
| `/desertstorm member_rule *` | Premium-only when read by the roster builder's auto-fill (but rules can be CREATED in free-tier). |
| `/setup_members` + `/sync_members` | Premium-only; populates the roster Sheet the storm flow reads. |

---

## 17. Footer/footnote conventions

Recurring patterns observed across this audit:

- Every ephemeral storm response prefixed with one of the emoji
  glyphs from the palette table at the top.
- "X — Y" em-dash separates the actor from the action (`Roster
  builder — Auto-fill applied`).
- Timeouts always reference the slash command to re-open
  (`⏰ Timed out. Run `/desertstorm signups` (or `/canyonstorm signups`) to start again.`).
- Soft warnings (`⚠️`) never block flow; permission denials
  (`⛔`) do block.
- Numeric counts are bold (`**{n}**`); names are bold (`**{name}**`);
  zones are bold (`**{zone}**`).
- Server-time references always say "server time" verbatim — never
  abbreviated to "ST" (per `1.1.3`).

---

## 18. Phase-aware presets (#152)

Strategy presets gained a `phase_count` field (0 / 2 / 3) so
alliances can model "phase migration" tactics — Alice plays
Info Center in Phase 1 then moves to Nuclear Silo in Phase 2.
Flat presets (the default, `phase_count == 0`) see ZERO new UI;
phase-aware presets get phase nav buttons, per-phase capacity
readouts, and per-phase mail blocks.

### 18.1 Preset editor — phase-mode selector

A `discord.ui.Select` on its own row above the action buttons:

| Surface | Verbatim |
|---|---|
| Select placeholder | `Pick a phase mode…` |
| Option — flat | `Flat (no phases)` + description |
| Option — 2-phase | `Yes — 2 Phases` + description |
| Option — 3-phase | `Yes — 3 Phases` + description |
| Editor embed line | `🔀 Mode: **{Flat / 2 Phases (P1 + P2) / 3 Phases (P1 + P2 + P3)}**` |
| Toggle ack | `🔀 Switched to **{label}** mode. Stored capacities + assignments are kept — flip back any time without data loss.{ Seeded N per-zone capacity/priority value(s) from prior values; edit any zone to override.}` |

Toggling 2→3 (or flat→2/3) seeds the newly-active phase's per-zone
max + priority from the prior phase so the officer doesn't have to
re-edit every zone. The seeded-count suffix appears only when at
least one zone got a non-zero seed.

### 18.2 Preset editor — multi-step zone wizard (phase-aware)

When `phase_count >= 2`, editing a zone fires a 3-page wizard
(modals + bridge `Next →` view) instead of the flat single modal —
Discord's 5-input limit forces the split for 3-phase presets
collecting max + priority + min-power across phases.

| Page | Modal title | Fields |
|---|---|---|
| 1 — Capacities | `Edit Zone Capacities: {zone}` | `Max Phase 1`, `Max Phase 2`, `Max Phase 3` (only for 3-phase) |
| Bridge | _(ephemeral)_ | Button: `Next → Power Floors` |
| 2 — Floors | `Edit Zone Floors: {zone}` | `Min Power Team A` (+ `Min Power Team B` on DS both-teams). Single field on CS or single-team DS. |
| Bridge | _(ephemeral)_ | Button: `Next → Priority` |
| 3 — Priority | `Edit Zone Priority: {zone}` | `Priority Phase 1`, `Priority Phase 2`, `Priority Phase 3` (only for 3-phase) |
| Final ack | _(ephemeral)_ | `✅ Updated **{zone}** — Max {p1/p2[/p3]}, Floor {floors}, Priority {p1/p2[/p3]}.` |

After the final submit, if the preset has same-family sibling zones
(Field Hospital II/III/IV from `_sibling_zone_names`), the #149
Apply-to-Similar follow-up view fires.

### 18.3 Roster builder — phase navigation

For `phase_count >= 2` presets, row 0 of the builder gets one button
per phase from `iter_phases()`:

> 🅰️ Set up Team A → opens RosterBuilderView with:
> Row 0: `Phase 1 •` `Phase 2` `Phase 3`  *(3 only when phase_count=3; active phase suffixed `•`)*
> Row 1: zone select  
> Row 2: member select  
> Row 3: action buttons (toggle, unassign, last-to-subs, re-pair, auto-fill)  
> Row 4: finalisation (Approve & Post / preview / render / cancel)

Active phase rendered with `primary` style; inactive phases use
`secondary`. Clicking flips `s.selected_phase` and re-renders.

### 18.4 Phase-aware embed shapes

| Surface | Verbatim |
|---|---|
| Editing phase header | `🔀 Editing **Phase {n}**` *(phase-aware presets only)* |
| Zone-line capacity readout | `{zone}  (P1: 2/4, P2: 4/4)` for 2-phase; `(P1: 2/4, P2: 3/4, P3: 4/4)` for 3-phase. Iterates `iter_phases()`. |
| Status glyph | Reflects the SELECTED phase's count vs cap (toggling re-colours rows) |
| Filled gauge | `📊 **Filled:** {assigned across phases} / {preset.total_capacity()}` — phase-aware sums every phase; flat unchanged |

### 18.5 rosters_tab Phase column

`_ROSTERS_HEADER` now carries `Phase` at index 2 (between Team and
Zone). Flat presets write `"1"` in the Phase column for traceability;
sub-pool rows leave it blank. Header migration on first write to an
existing rosters_tab translates every prior row by column-name
lookup so pre-#152 data stays correctly aligned (commit `aafb576`).

---

## 19. Recent storm-related copy changes (audit trail)

| Commit | Change |
|---|---|
| `aafb576` (PR #153) | Phase-aware fixes: 5 surfaces switched from hardcoded `(1, 2)` to `iter_phases()` (mail builder, phase nav, embed zone-line readout, faction-role candidate finder, PNG renderer); `save_preset` translates sibling rows by column name; `_write_rosters_tab` row-shifts on header migration; Filled gauge sums across phases; `_zone_of_primary` walks all phases; `_SaveAsPresetModal` preserves `phase_count` + per-phase caps; flat→2/3 + 2→3 toggle seeds newly-active phase from prior; attendance dedupes `(team, zone, member)` so same-zone-multi-phase shows once |
| `8e916d6` (PR #153) | 3-way phase select + multi-step zone-edit wizard |
| `2a577d4` (PR #153) | Session + auto-fill support for 3 phases |
| `e5a07c6` (PR #153) | Schema extended to 1/2/3 phases via `phase_count` int (legacy `Use Phases` truthy → 2) |
| `d02a4c0` (PR #153) | rosters_tab Phase column + phase-aware finalisation |
| `c6b61df` (PR #153) | Phase-aware auto-fill |
| `3fff703` (PR #153) | Phase-aware roster-builder UI (phase nav, per-phase pickers) |
| `fac8567` (PR #153) | Phase-aware preset editor UI (mode toggle + capacity modals) |
| `aa28bcc` (PR #153) | Phase-aware preset schema + session + mail foundation |
| `a8ad4bd` (PR #150) | Single-team DS gate runtime wiring (#148): SignupView, OfficerView Set-up buttons, embed slot lines, click-handler reject all consult `teams` |
| `614233e` (PR #150) | CS SignupView migration: `_force_all_buttons` keeps pre-hotfix CS posts clickable across restarts; stale b/either votes on CS rejected at click time; doubled-label fallback (`🅰️ Team A: Team A`) fixed |
| `396dc6e` (PR #150) | `on_timeout` sweep across 8 storm views; on-behalf modal rejects purely numeric names; OfficerView captures `view.message` for the canonical `⏰ actions have timed out` notice |
| `16f9f18` (PR #151) | Async refactor: every storm gspread call wrapped in `asyncio.to_thread`; `OfficerView.refresh_buckets` made async — no user-facing copy change |
| `4029f55` (PR #147) | Flexible event_date parser: `May 18`, `5/18`, `yesterday`, weekday names; `format_event_date` for display |
| `9dfbf40` | Team-test hotfix: SignupView buttons changed to `🅰️ Team A: time / 🅱️ Team B: time / 🔄 Either / ❌ Cannot` (was `✅ time` only); strategy preset save-time over-capacity refusal removed (>30 now informational); officer-view bucket labels switched to `🅰️ Voted Team A` family |
| `8ee0773` | Team-test hotfix: sign-up time wizard prompt restored to 12-hour copy with tz annotation |
| `8301f82` | Team-test hotfix: `_ZoneEditModal` label shortened to fit Discord's 45-char limit |
| `acfb6c6` (λ, PR #141) | `+N more` overflow indicator on member picker + paired-sub picker; Overflow subs line in paired-mode embed; Re-pair Sub button + primary picker; 25 MB attachment guard; PNG title wrap; DM body leading-"your" strip; Judicator/power-refresh DM keep-or-change branches |
| `e5b9f6e` (μ, PR #141) | Faction roles candidate set now includes paired subs |
| `fb73dbb` (ν, PR #141) | No user-facing copy change; bot filter + cache pre-pass are silent |
| `7d950a9` (ξ, PR #141) | No user-facing copy change; typed-name normalisation silent at save time |
| `a3b54fe` (ο, PR #141) | New "Is this user in Discord?" column appears on roster Sheet; storm UX unchanged |
| `97045d9` | Soft-warning text refined for stale-ID + bot filter |
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
  bucket-map labels in `/desertstorm signups` (or `/canyonstorm signups`) today.

Track in [#54](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/54)
if/when these surface as friction in onboarding.
