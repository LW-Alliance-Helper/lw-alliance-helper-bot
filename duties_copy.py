"""Leadership Duties (#687): every string a user sees, in one place.

**Provisional until Kevin's copy sign-off.** The feature name, the command,
the glyphs and every sentence here were written against `notes/UX.md` and
`notes/DESIGN.md` for the first build and have not been approved. Kept in
one module so the sign-off page can be built from it and a rename stays a
one-line change (DESIGN.md, Labels).

Two words are chosen on purpose:

- **"position"**, never "slot", for a place on a duty. "Slot" already means a
  storm time slot in this product (UX.md, One name, one meaning).
- **"thread"**, never "ticket", in anything a member reads. "Ticket" is the
  working name in code and on the issue.
"""

from __future__ import annotations

# ── Names ────────────────────────────────────────────────────────────────────

FEATURE_NAME = "Leadership Duties"
DUTIES_CMD = "/duties"
HUB_BTN_DUTIES = "🪪 Leadership Duties"
HUB_TITLE = HUB_BTN_DUTIES
CMD_DESCRIPTION = "Who in leadership holds which duty, with reminders and member contact"

# ── Hub buttons ──────────────────────────────────────────────────────────────

BTN_ADD = "➕ Add a duty"
BTN_EDIT = "✏️ Edit a duty"
BTN_PAUSE = "⏸️ Pause or resume"
BTN_DELETE = "🗑️ Delete a duty"
BTN_WORKLOAD = "⚖️ Workload"
BTN_MY_DUTIES = "👤 My duties"
BTN_REMINDERS = "🔔 Reminders"
BTN_CONTACT = "⚙️ Contact settings"

#: The route back from anything opened off the hub.
HUB_ROUTE = f"`{DUTIES_CMD}`"


def route(btn: str) -> str:
    """`/duties` → **button**, the path expression (UX.md, Voice)."""
    return f"`{DUTIES_CMD}` → **{btn}**"


# ── Hub ──────────────────────────────────────────────────────────────────────

HUB_INTRO = "The standing duties your leadership team splits between you, and who holds each one."
HUB_EMPTY = f"ℹ️ No duties yet. Click **{BTN_ADD}** to record the first one."
HUB_FREE = (
    f"🔒 **{FEATURE_NAME}** is a 💎 Premium feature. Record who in leadership holds "
    "which duty, remind them in your own words, and let members open a private "
    "thread with the right people. Run `/upgrade` to unlock it."
)
HUB_LAPSED = (
    "🔒 Your 💎 Premium has ended, so this list is read-only for now. Everything "
    "is saved. Reminders are paused, and the contact buttons tell members they "
    "aren't available. Run `/upgrade` to pick up where you left off."
)
HUB_NO_CATEGORY = "Other duties"
HUB_FOOTER = "{count} duties · {contact} with contact buttons"

LINE_PRIMARY = "Primary: {who}"
LINE_BACKUP = "Backup: {who}"
PAUSED_MARK = " (paused)"
OPEN_MARK = "*open*"
NOBODY = "*nobody*"
ANYONE_IN_LEADERSHIP = "anyone in leadership"

# ── Workload ─────────────────────────────────────────────────────────────────

WORKLOAD_TITLE = BTN_WORKLOAD
WORKLOAD_INTRO = "How many duties each leader holds. Paused ones are counted and noted."
WORKLOAD_PRIMARY = "Primary: {n}"
WORKLOAD_BACKUP = "Backup: {n}"
WORKLOAD_PAUSED = " ({n} paused)"
WORKLOAD_NOT_LEADERSHIP = " (not in leadership)"
WORKLOAD_NO_ROLE = (
    "ℹ️ Your leadership role isn't set in `/setup`, so this only lists people who hold a duty."
)
WORKLOAD_OPEN_FIELD = "Open positions"
WORKLOAD_OPEN_LINE = "• **{duty}**{paused}: {n} {kind}"
WORKLOAD_OPEN_NONE = "Every position is filled."
KIND_PRIMARY = ("primary", "primaries")
KIND_BACKUP = ("backup", "backups")

# ── My duties ────────────────────────────────────────────────────────────────

MY_TITLE = BTN_MY_DUTIES
MY_PRIMARY_FIELD = "Primary on"
MY_BACKUP_FIELD = "Backup on"
MY_NONE = "ℹ️ You don't hold any duties."
MY_EMPTY_FIELD = "None"

# ── Picking a duty ───────────────────────────────────────────────────────────

PICK_PROMPT = "Which duty?"
PICK_PLACEHOLDER = "Pick a duty…"
PICK_NONE = f"ℹ️ There are no duties yet. Click **{BTN_ADD}** first."
PICK_GONE = "ℹ️ That duty isn't there anymore. Someone may have just deleted it."

# ── Duty editor ──────────────────────────────────────────────────────────────

MODAL_ADD_TITLE = "Add a duty"
MODAL_EDIT_TITLE = "Edit a duty"
FIELD_NAME = "Duty name"
FIELD_NAME_PLACEHOLDER = "e.g. Member disputes"
FIELD_CATEGORY = "Category"
FIELD_CATEGORY_PLACEHOLDER = "e.g. Members, VS, Events"
FIELD_DESCRIPTION = "Description"
FIELD_DESCRIPTION_PLACEHOLDER = (
    "What it covers. Members see this on the contact buttons if you turn them on."
)

EDITOR_TITLE = "✏️ {name}"
EDITOR_CATEGORY = "**Category:** {category}"
EDITOR_CONTACT_ON = "**Members can contact:** ✅ on"
EDITOR_CONTACT_OFF = "**Members can contact:** ❌ off"
EDITOR_UNSAVED = "Nothing is saved until you click **💾 Save**."
EDITOR_PH_PRIMARY = "Primary holders"
EDITOR_PH_BACKUP = "Backup holders"
EDITOR_PH_BACKUP_ANYONE = "Backup is anyone in leadership"
EDITOR_PH_OPEN_PRIMARY = "Open primary positions"
EDITOR_PH_OPEN_BACKUP = "Open backup positions"
EDITOR_OPEN_OPTION = (
    "No open {kind} positions",
    "1 open {kind} position",
    "{n} open {kind} positions",
)

BTN_NAME_AND_DESCRIPTION = "✏️ Name and description"
BTN_BACKUP_ANYONE = "🔀 Backup: anyone in leadership"
BTN_BACKUP_NAMED = "🔀 Backup: named people"
BTN_CONTACT_ON = "Turn contact on"
BTN_CONTACT_OFF = "Turn contact off"
BTN_SAVE = "💾 Save"
BTN_CANCEL = "Cancel"

SAVED = "✅ Saved **{name}**."
ADDED = "✅ Added **{name}**."
DUPLICATE_NAME = (
    "⚠️ You already have a duty called **{name}**. Pick another name with "
    f"**{BTN_NAME_AND_DESCRIPTION}**."
)
NAME_REQUIRED = "⚠️ A duty needs a name."
CONTACT_FULL = (
    "⚠️ The contact buttons hold {cap} duties at most, and {cap} already have them. "
    "Turn contact off on another duty first."
)

# ── Pause, delete ────────────────────────────────────────────────────────────

PAUSED = "⏸️ Paused **{name}**. Its reminders stop and every setting is kept."
RESUMED = "▶️ Resumed **{name}**. Its reminders run again."
DELETE_CONFIRM = "🗑️ **{name}** will be deleted{reminders}. This can't be undone."
DELETE_CONFIRM_REMINDERS = (" with its reminder", " with its {n} reminders")
BTN_DELETE_YES = "🗑️ Yes, delete"
DELETED = "🗑️ Deleted **{name}**."

# ── Reminders ────────────────────────────────────────────────────────────────

REMINDERS_TITLE = "🔔 Reminders for {name}"
REMINDERS_NONE = "ℹ️ **{name}** has no reminders yet."
REMINDERS_PAUSED_NOTE = "ℹ️ **{name}** is paused, so none of these are sending."
REMINDER_PICK_PLACEHOLDER = "Pick a reminder…"
REMINDER_ROW = "**{n}.** {when} · {dest} · {state}"
REMINDER_STATE_ON = "✅ on"
REMINDER_STATE_OFF = "❌ off"
BTN_REMINDER_ADD = "➕ Add a reminder"
BTN_REMINDER_EDIT = "✏️ Edit"
BTN_REMINDER_ON = "🔔 Turn on"
BTN_REMINDER_OFF = "🔕 Turn off"
BTN_REMINDER_DELETE = "🗑️ Delete"
REMINDER_DELETE_CONFIRM = "🗑️ The reminder for **{when}** will be deleted. This can't be undone."
REMINDER_DELETED = "🗑️ Deleted that reminder."
REMINDER_TURNED_ON = "🔔 Turned that reminder on."
REMINDER_TURNED_OFF = "🔕 Turned that reminder off."

REMINDER_EDITOR_TITLE = "🔔 Reminder for {name}"
REMINDER_WHEN = "**When:** {when}"
REMINDER_SENDS = "**Sends to:** {dest}"
REMINDER_PING_ON = "**Ping:** ✅ on"
REMINDER_PING_OFF = "**Ping:** ❌ off"
REMINDER_PREVIEW_FIELD = "Preview"
REMINDER_NO_MESSAGE = "*No message yet.*"
REMINDER_ANYONE_NOTE = (
    "ℹ️ The backup for this duty is anyone in leadership, so backup reminders "
    "post in {channel} rather than going to every leader's DMs."
)
REMINDER_PLACEHOLDER_HELP = (
    "Write `{primary}`, `{backup}` or `{duty}` and I'll fill in whoever holds it when it sends."
)

SCHEDULE_WEEKDAYS = "On chosen weekdays"
SCHEDULE_INTERVAL = "Every few days"
WEEKDAYS_PH = "Which days?"
WEEKDAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
WEEKDAY_PLURALS = (
    "Mondays",
    "Tuesdays",
    "Wednesdays",
    "Thursdays",
    "Fridays",
    "Saturdays",
    "Sundays",
)
WHEN_WEEKDAYS = "{days} at {time}"
WHEN_EVERY_DAY = "Every day at {time}"
WHEN_INTERVAL = "Every {n} days from {date} at {time}"
WHEN_INTERVAL_DAILY = "Every day from {date} at {time}"
WHEN_UNSET = "*not set yet*"

SEND_PRIMARIES = "DM the primaries"
SEND_BACKUPS = "DM the backups"
SEND_BOTH = "DM primaries and backups"
SEND_CHANNEL = "Post in a channel"
DEST_CHANNEL = "{channel}"
DEST_NO_CHANNEL = "a channel (not picked yet)"
CHANNEL_PH = "Channel to post in"

BTN_MESSAGE_AND_TIME = "✏️ Message and time"
BTN_PING_ON = "Turn ping on"
BTN_PING_OFF = "Turn ping off"

MODAL_REMINDER_TITLE = "Reminder message and time"
FIELD_MESSAGE = "Message"
FIELD_MESSAGE_PLACEHOLDER = "e.g. {primary}, time to post today's schedule."
FIELD_TIME = "Time ({tz})"
FIELD_TIME_PLACEHOLDER = "e.g. 9:00pm"
FIELD_EVERY = "Every how many days"
FIELD_EVERY_PLACEHOLDER = "e.g. 3"
FIELD_START = "Starting on"
FIELD_START_PLACEHOLDER = "e.g. 9/30 or today"

TIME_UNREADABLE = "⚠️ I couldn't read `{raw}` as a time. Try `9:00pm` or `21:00`."
EVERY_UNREADABLE = "⚠️ I couldn't read `{raw}` as a number of days. Try a number like `3`."
START_UNREADABLE = "⚠️ I couldn't read `{raw}` as a date. Try `9/30` or `today`."
NEEDS_MESSAGE = f"⚠️ The reminder needs a message. Click **{BTN_MESSAGE_AND_TIME}** to write one."
NEEDS_TIME = f"⚠️ The reminder needs a time. Click **{BTN_MESSAGE_AND_TIME}** to set one."
NEEDS_DAYS = "⚠️ Pick at least one weekday."
NEEDS_INTERVAL = f"⚠️ Set how often and from when with **{BTN_MESSAGE_AND_TIME}**."
NEEDS_CHANNEL = "⚠️ Pick a channel to post in."
REMINDER_SAVED = "✅ Saved the reminder for **{name}**."

# ── Sent reminders ───────────────────────────────────────────────────────────

REMINDER_DM = "🔔 **{duty}** reminder from **{server}**\n{text}"
REMINDER_POST = "🔔 **{duty}**\n{text}"
REMINDER_FALLBACK = "🔔 I couldn't DM {who} this **{duty}** reminder, so here it is:\n{text}"

# ── Contact settings ─────────────────────────────────────────────────────────

CONTACT_TITLE = BTN_CONTACT
CONTACT_INTRO = (
    "Members click a duty's button to open a private thread with the people who "
    "hold it. Turn contact on for a duty from **✏️ Edit a duty**."
)
CONTACT_BUTTONS_LINE = "**Buttons posted in:** {channel}"
CONTACT_THREADS_LINE = "**Threads open under:** {channel}"
CONTACT_SAME_CHANNEL = "the same channel"
CONTACT_NOT_SET = "*not set*"
CONTACT_POSTED = "**Buttons:** ✅ posted"
CONTACT_NOT_POSTED = "**Buttons:** ❌ not posted"
CONTACT_COUNT = "**Duties with contact on:** {n}"
CONTACT_PH_BUTTONS = "Channel for the contact buttons"
CONTACT_PH_THREADS = "Channel threads open under (optional)"
BTN_THREADS_SAME = "Threads in the same channel"
BTN_POST_BUTTONS = "📣 Post the contact buttons"
BTN_POST_AGAIN = "📣 Post them again"
CONTACT_SAVED = "✅ Saved the contact settings."
CONTACT_PICK_CHANNEL_FIRST = "⚠️ Pick a channel for the contact buttons first."
CONTACT_POSTED_ACK = "📣 Posted the contact buttons in {channel}."
CONTACT_UPDATED_ACK = "✅ Updated the contact buttons in {channel}."
CONTACT_POST_FAILED = (
    "⚠️ I couldn't post in {channel}. Check I can **View Channel** and **Send Messages** there."
)

WARN_MANAGE_THREADS = (
    "⚠️ Anyone with **Manage Threads** in {channel} can read every thread in it, "
    "including one about another leader. Right now that's {who}. If that matters, "
    "pick a channel where fewer people have it."
)
WARN_MEMBERS_CANT_VIEW = (
    "⚠️ Your member role can't see {channel}, so members couldn't read the threads "
    "opened there. Let them **View Channel**, or pick a channel they can see."
)
WARN_BOT_THREADS = (
    "⚠️ I need **Create Private Threads** and **Send Messages in Threads** in {channel}."
)
WARN_NO_MEMBER_ROLE = (
    "ℹ️ Your member role isn't set in `/setup`, so I can't check that members can see {channel}."
)

# ── The posted contact buttons (member-facing) ───────────────────────────────

PANEL_TITLE = "🪪 Who handles what"
PANEL_INTRO = (
    "Click a button to open a private thread with the people who handle it. "
    "Write your message there and they'll pick it up."
)
PANEL_EMPTY = "ℹ️ There's nobody to contact here right now."
PANEL_HELD_BY = "Handled by {who}"
PANEL_BACKUP = "Backup: {who}"

# ── Clicking a contact button (member-facing) ────────────────────────────────

TICKET_UNAVAILABLE = "ℹ️ This isn't available right now. Please message leadership directly."
TICKET_DUTY_UNAVAILABLE = (
    "ℹ️ **{duty}** isn't taking messages here right now. Please message leadership directly."
)
TICKET_NOBODY = "ℹ️ Nobody holds **{duty}** right now. Please message leadership directly."
TICKET_RECENT = "ℹ️ You just opened a thread for **{duty}**: {thread}"
TICKET_READY = "✅ Your thread is ready: {thread}"
TICKET_OPENING = "⏳ Already opening your thread for **{duty}**."
TICKET_FAILED = (
    "⚠️ I couldn't open your thread, and I've let leadership know. Please message "
    "them directly for now."
)

#: `{duty} · {member} · {when}`, in server time. Discord caps thread names at 100.
THREAD_NAME = "{duty} · {member} · {when}"
THREAD_TIME_FORMAT = "%d %b %H:%M"
THREAD_OPENER = (
    "{member} opened this about **{duty}**, which {holders} handle{s}.\n"
    "Write your message here. When it's settled, leadership can click **{close}**."
)
BTN_CLOSE = "Close"
THREAD_CLOSED = "Closed by {who}. Anyone typing here opens it again."
CLOSE_DENIED = "⛔ Only leadership can close this."

# ── Config health (see config_health.Subject) ────────────────────────────────

SUBJECT_REMINDER_CHANNEL = "a Leadership Duties reminder channel"
SUBJECT_PANEL_CHANNEL = "the channel for your Leadership Duties contact buttons"
SUBJECT_TICKET_CHANNEL = "the channel Leadership Duties threads open under"
SUBJECT_HOLDERS = "your Leadership Duties"
HOLDER_LEFT_DETAIL = "**{name}** left the server. {their} {duties} now {have} an open position."

# ── /help ────────────────────────────────────────────────────────────────────

HELP_EMOJI = "🪪"
HELP_LABEL = FEATURE_NAME
HELP_DESCRIPTION = (
    "💎 Premium. Record the standing duties your leadership team splits between "
    "you, who holds each one, and how evenly they're spread. Remind holders in "
    "your own words, and let members open a private thread with the right people."
)
HELP_COMMANDS = (
    (
        DUTIES_CMD,
        "**Duties hub.** Add and edit duties (primary and backup holders, open "
        "positions), pause one for Season, and see the workload and your own duties.",
    ),
    (
        f"{DUTIES_CMD} → {BTN_REMINDERS}",
        "Reminders you write yourself for each duty, by DM or in a channel, on "
        "chosen weekdays or every few days.",
    ),
    (
        f"{DUTIES_CMD} → {BTN_CONTACT}",
        "Post contact buttons for members. Each click opens a private thread with "
        "that member and the duty's holders.",
    ),
)

# ── Outage catch-up digest ───────────────────────────────────────────────────

CATCHUP_TITLE = "Leadership Duties reminder: {duty}"
CATCHUP_TO_CHANNEL = "sent to #{channel}"
CATCHUP_TO_DMS = "sent as DMs to the holders"
