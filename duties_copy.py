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
- **"assigned"**, never "holder" or "holds", for the people on a duty.
  Kevin, 2026-09-30: "they're more assigned to it or in charge of doing
  this thing." "Holder" stays the working name in code.
"""

from __future__ import annotations

# ── Names ────────────────────────────────────────────────────────────────────

FEATURE_NAME = "Leadership Duties"
DUTIES_CMD = "/duties"
HUB_BTN_DUTIES = "🪪 Leadership Duties"
HUB_TITLE = HUB_BTN_DUTIES
CMD_DESCRIPTION = "Who in leadership is assigned to which duty, with reminders and member contact"

# ── Hub buttons ──────────────────────────────────────────────────────────────

#: ➕ stays on the button but never goes in text. Discord draws it near-black
#: (#31373D), invisible on an embed or a message (Kevin, 2026-09-30: "remove it
#: from the embed box and leave it on the button"). Text names the button by
#: its plain name.
ADD_DUTY = "Add a duty"
BTN_ADD = f"➕ {ADD_DUTY}"
BTN_EDIT = "✏️ Edit a duty"
BTN_PAUSE = "⏸️ Pause or resume"
BTN_DELETE = "🗑️ Delete a duty"
BTN_WORKLOAD = "⚖️ Workload"
BTN_MY_DUTIES = "👤 My duties"
BTN_REMINDERS = "🔔 Reminders"
BTN_CATEGORIES = "🏷️ Categories"
#: The sharing row (#706, #707). Kevin's words, 2026-09-30; the glyphs are
#: provisional until copy sign-off.
BTN_SHARE_LEADERSHIP = "📋 Share to leadership channel"
BTN_SHARE_MEMBERS = "📣 Share curated list with members"
BTN_POST_CONTACT = "⚙️ Post Contact buttons"

#: The route back from anything opened off the hub.
HUB_ROUTE = f"`{DUTIES_CMD}`"


# ── Hub ──────────────────────────────────────────────────────────────────────

HUB_INTRO = (
    "The standing duties your leadership team splits between you, and who is assigned to each one."
)
HUB_EMPTY = f"ℹ️ No duties yet. Click **{ADD_DUTY}** to record the first one."
HUB_FREE = (
    f"🔒 **{FEATURE_NAME}** is a 💎 Premium feature. Record who in leadership is "
    "assigned to which duty, remind them in your own words, and let members open a private "
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
WORKLOAD_INTRO = "How many duties each leader is assigned. Paused ones are counted and noted."
WORKLOAD_PRIMARY = "Primary: {n}"
WORKLOAD_BACKUP = "Backup: {n}"
WORKLOAD_PAUSED = " ({n} paused)"
WORKLOAD_NOT_LEADERSHIP = " (not in leadership)"
WORKLOAD_NO_ROLE = (
    "ℹ️ Your leadership role isn't set in `/setup`, so this only lists people assigned to a duty."
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
MY_NONE = "ℹ️ You aren't assigned to any duties."
MY_EMPTY_FIELD = "None"

# ── Picking a duty ───────────────────────────────────────────────────────────

PICK_PROMPT = "Which duty?"
PICK_PLACEHOLDER = "Pick a duty…"
PICK_NONE = f"ℹ️ There are no duties yet. Click **{ADD_DUTY}** first."
PICK_GONE = "ℹ️ That duty isn't there anymore. Someone may have just deleted it."

# ── Duty editor ──────────────────────────────────────────────────────────────

MODAL_ADD_TITLE = "Add a duty"
MODAL_EDIT_TITLE = "Edit a duty"
FIELD_NAME = "Duty name"
FIELD_NAME_PLACEHOLDER = "e.g. Member disputes"
FIELD_CATEGORY = "Category"
FIELD_CATEGORY_PLACEHOLDER = "No category"
DUTY_NO_CATEGORIES = (
    f"ℹ️ No categories yet. To group your duties, add some from **{BTN_CATEGORIES}** "
    f"in `{DUTIES_CMD}`."
)
FIELD_DESCRIPTION = "Description"
FIELD_DESCRIPTION_PLACEHOLDER = (
    "What it covers. Members see this on the contact buttons and on lists you share with them."
)

EDITOR_TITLE = "✏️ {name}"
EDITOR_CATEGORY = "**Category:** {category}"
EDITOR_CONTACT_ON = "**Members can contact:** ✅ on"
EDITOR_CONTACT_OFF = "**Members can contact:** ❌ off"
EDITOR_UNSAVED = "Nothing is saved until you click **💾 Save**."
EDITOR_PH_PRIMARY = "Assigned as primary"
EDITOR_PH_BACKUP = "Assigned as backup"
#: The "anyone in leadership" choice in both holder pickers (item 3).
EDITOR_ANYONE_OPTION = "Anyone in leadership"
EDITOR_ANYONE_OPTION_DESC = "Everyone with your leadership role"
EDITOR_PH_OPEN_PRIMARY = "Open primary positions"
EDITOR_PH_OPEN_BACKUP = "Open backup positions"
EDITOR_OPEN_OPTION = (
    "No open {kind} positions",
    "1 open {kind} position",
    "{n} open {kind} positions",
)

BTN_NAME_AND_DESCRIPTION = "✏️ Name, category and description"
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

# ── Categories ───────────────────────────────────────────────────────────────

CATEGORIES_TITLE = BTN_CATEGORIES
CATEGORIES_INTRO = "Group your duties. Each duty picks one from a list when you add or edit it."
ADD_CATEGORY = "Add"
CATEGORIES_NONE = f"ℹ️ No categories yet. Click **{ADD_CATEGORY}** to make one."
CATEGORY_ROW = "• **{name}**: {n}"
CATEGORY_COUNT = ("no duties", "1 duty", "{n} duties")
CATEGORY_PICK_PLACEHOLDER = "Pick a category…"
BTN_CATEGORY_ADD = f"➕ {ADD_CATEGORY}"
BTN_CATEGORY_EDIT = "✏️ Edit"
BTN_CATEGORY_DELETE = "🗑️ Delete"
MODAL_CATEGORY_ADD = "Add a category"
MODAL_CATEGORY_EDIT = "Edit a category"
FIELD_CATEGORY_NAME = "Category name"
FIELD_CATEGORY_NAME_PLACEHOLDER = "e.g. Members"
CATEGORY_ADDED = "✅ Added **{name}**."
CATEGORY_RENAMED = "✅ Renamed **{old}** to **{name}**."
CATEGORY_DUPLICATE = "⚠️ You already have a category called **{name}**."
CATEGORIES_FULL = "⚠️ You can have up to {cap} categories. Delete one to add another."
CATEGORY_DELETE_CONFIRM = "🗑️ **{name}** will be deleted.{moved} This can't be undone."
CATEGORY_DELETE_MOVED = (
    "",
    " Its duty moves to {other}.",
    " Its {n} duties move to {other}.",
)
CATEGORY_DELETED = "🗑️ Deleted **{name}**."

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
    "ℹ️ Anyone in leadership is assigned to this duty, so their reminders post in "
    "{channel} rather than going to every leader's DMs."
)
REMINDER_PLACEHOLDER_HELP = (
    "Write `{primary}`, `{backup}` or `{duty}` and I'll fill in whoever is assigned when it sends."
)

#: The schedule choices, in Kevin's words (2026-09-30), keyed by
#: `duties.SCHEDULE_*`.
SCHEDULE_LABELS = {
    "daily": "Daily",
    "every_2_days": "Every other day",
    "every_3_days": "Every 3 days",
    "weekdays": "Selected days only",
    "weekly": "Weekly",
    "every_2_weeks": "Every 2 weeks",
    "monthly": "Every month",
}
SCHEDULE_PH = "How often?"
WEEKDAYS_PH = "Which days?"
WEEKLY_PH = "Which day of the week?"
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
WHEN_DAILY = "Every day at {time}"
WHEN_EVERY_OTHER_DAY = "Every other day from {date} at {time}"
WHEN_EVERY_3_DAYS = "Every 3 days from {date} at {time}"
WHEN_WEEKDAYS = "{days} at {time}"
WHEN_WEEKLY = "Every {day} at {time}"
WHEN_EVERY_2_WEEKS = "Every 2 weeks from {date} at {time}"
WHEN_MONTHLY = "Every month on the {nth} at {time}"
WHEN_MONTHLY_LATE = "Every month on the {nth}, or the last day of shorter months, at {time}"
WHEN_UNSET = "*not set yet*"

SEND_PRIMARIES = "DM the primaries"
SEND_BACKUPS = "DM the backups"
SEND_BOTH = "DM primaries and backups"
SEND_CHANNEL = "Post in a channel"
SEND_THREAD = "Post in a thread"
DEST_CHANNEL = "{channel}"
DEST_NO_CHANNEL = "a channel (not picked yet)"
DEST_NO_THREAD = "a thread (not picked yet)"
CHANNEL_PH = "Channel to post in"
THREAD_PH = "Pick a thread…"
THREAD_OPTION = "{thread} (in #{parent})"
THREAD_NONE_OPEN = "No open threads to pick"

BTN_MESSAGE_AND_TIME = "✏️ Message and time"
BTN_PING_ON = "Turn ping on"
BTN_PING_OFF = "Turn ping off"

MODAL_REMINDER_TITLE = "Reminder message and time"
FIELD_MESSAGE = "Message"
FIELD_MESSAGE_PLACEHOLDER = "e.g. {primary}, time to post today's schedule."
FIELD_TIME = "Time ({tz})"
FIELD_TIME_PLACEHOLDER = "e.g. 9:00pm"
FIELD_START = "Starting on"
FIELD_START_PLACEHOLDER = "e.g. 9/30 or today"

TIME_UNREADABLE = "⚠️ I couldn't read `{raw}` as a time. Try `9:00pm` or `21:00`."
START_UNREADABLE = "⚠️ I couldn't read `{raw}` as a date. Try `9/30` or `today`."
NEEDS_MESSAGE = f"⚠️ The reminder needs a message. Click **{BTN_MESSAGE_AND_TIME}** to write one."
NEEDS_TIME = f"⚠️ The reminder needs a time. Click **{BTN_MESSAGE_AND_TIME}** to set one."
NEEDS_DAYS = "⚠️ Pick at least one weekday."
NEEDS_WEEKDAY = "⚠️ Pick the day of the week."
NEEDS_START = f"⚠️ Set a starting date with **{BTN_MESSAGE_AND_TIME}**."
NEEDS_CHANNEL = "⚠️ Pick a channel to post in."
NEEDS_THREAD = "⚠️ Pick a thread to post in."
REMINDER_SAVED = "✅ Saved the reminder for **{name}**."

# ── Sent reminders ───────────────────────────────────────────────────────────

REMINDER_DM = "🔔 **{duty}** reminder from **{server}**\n{text}"
REMINDER_POST = "🔔 **{duty}**\n{text}"
REMINDER_FALLBACK = "🔔 I couldn't DM {who} this **{duty}** reminder, so here it is:\n{text}"

# ── Contact settings ─────────────────────────────────────────────────────────

CONTACT_TITLE = BTN_POST_CONTACT
CONTACT_INTRO = (
    "Members click a duty's button to open a private thread with the people "
    "assigned to it. Turn contact on for a duty from **✏️ Edit a duty**."
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
POST_FAILED = (
    "⚠️ I couldn't post in {channel}. Check I can **View Channel** and **Send Messages** there."
)
CONTACT_POST_FAILED = POST_FAILED

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
    "Select the option you need to contact someone about that specific duty. "
    "A private thread will be created for you to confidentially message leadership."
)
PANEL_EMPTY = "ℹ️ There's nobody to contact here right now."
PANEL_HELD_BY = "Handled by {who}"
PANEL_BACKUP = "Backup: {who}"

# ── Sharing the full list to the leadership channel (#706) ──────────────────

ROSTER_TITLE = "📋 Leadership Duties"
ROSTER_INTRO = (
    "Every duty and who is assigned to it. This updates itself when a duty changes. "
    f"Click **{BTN_MY_DUTIES}** to see yours."
)
ROSTER_EMPTY = "ℹ️ There are no duties right now."
ROSTER_MORE = (
    f"…and 1 more duty. See them all in `{DUTIES_CMD}`.",
    f"…and {{n}} more duties. See them all in `{DUTIES_CMD}`.",
)
ROSTER_DENIED = "⛔ Only leadership can use this."
ROSTER_PICK_CHANNEL = (
    "ℹ️ Your leadership channel isn't set in `/setup`, so pick where to share the list."
)
ROSTER_CHANNEL_PH = "Channel to share the list in"
BTN_SHARE_ROSTER = "📋 Share the list"
ROSTER_SHARED_ACK = "📋 Shared the duty list in {channel}. It updates itself when a duty changes."

# ── Sharing a curated list with members (#707) ──────────────────────────────

SHARE_TITLE = BTN_SHARE_MEMBERS
SHARE_INTRO = (
    "Pick the duties members should see and who is in charge of each. There are no "
    "contact buttons on this list. It updates itself when one of these duties changes, "
    "and paused duties stay off it until they're resumed."
)
SHARE_PICKED_LINE = "**Duties picked:** {names}"
SHARE_NONE_PICKED = "*none yet*"
SHARE_SHOWS_LINE = "**Shows:** {who}"
#: Kevin's words, 2026-09-30.
SHOW_BOTH = "Primary & Backup"
SHOW_PRIMARY = "Primary only"
SHARE_CHANNEL_LINE = "**Shared in:** {channel}"
SHARE_POSTED = "**List:** ✅ shared"
SHARE_NOT_POSTED = "**List:** ❌ not shared yet"
SHARE_PICK_PH = "Duties to share"
SHARE_SHOW_PH = "Who to show"
SHARE_CHANNEL_PH = "Channel to share it in"
BTN_SHARE_LIST = "📣 Share the list"
BTN_SHARE_AGAIN = "📣 Share it again"
SHARE_NEEDS_DUTY = "⚠️ Pick at least one duty to share."
SHARE_NEEDS_CHANNEL = "⚠️ Pick a channel to share the list in first."
SHARE_ACK = (
    "📣 Shared 1 duty with members in {channel}.",
    "📣 Shared {n} duties with members in {channel}.",
)
SHARE_PAUSED_NOTE = "ℹ️ These are paused, so they join the list when they're resumed: {names}."

# ── The curated list (member-facing) ────────────────────────────────────────

SHARED_TITLE = "🪪 Who's in charge of what"
SHARED_EMPTY = "ℹ️ There's nothing to show here right now."
SHARED_MORE = ("…and 1 more duty.", "…and {n} more duties.")

# ── Clicking a contact button (member-facing) ────────────────────────────────

TICKET_UNAVAILABLE = "ℹ️ This isn't available right now. Please message leadership directly."
TICKET_DUTY_UNAVAILABLE = (
    "ℹ️ **{duty}** isn't taking messages here right now. Please message leadership directly."
)
TICKET_NOBODY = "ℹ️ Nobody is assigned to **{duty}** right now. Please message leadership directly."
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
SUBJECT_ROSTER_CHANNEL = "the channel your Leadership Duties list is shared in"
SUBJECT_SHARED_CHANNEL = "the channel your curated Leadership Duties list is shared in"
SUBJECT_HOLDERS = "your Leadership Duties"
HOLDER_LEFT_DETAIL = "**{name}** left the server. {their} {duties} now {have} an open position."

# ── /help ────────────────────────────────────────────────────────────────────

HELP_EMOJI = "🪪"
HELP_LABEL = FEATURE_NAME
HELP_DESCRIPTION = (
    "💎 Premium. Record the duties your leadership team handles, including who "
    "handles each one and see how much your team is taking on. You can remind "
    "your team members about their duties and allow your members to open a "
    "private thread with the right people."
)
HELP_COMMANDS = (
    (
        DUTIES_CMD,
        "**Duties hub.** Add and edit duties, manage primary, backup, and open "
        "positions, pause duties, and see the workload for each team member.",
    ),
    (
        f"{DUTIES_CMD} → {BTN_REMINDERS}",
        "Reminders you write yourself for each duty, sent by DM, in a channel or "
        "in a thread, on a schedule you pick, from daily to monthly.",
    ),
    (
        f"{DUTIES_CMD} → {BTN_SHARE_LEADERSHIP}",
        "Post every duty and who is assigned to it in your leadership channel. It updates "
        f"itself, and **{BTN_MY_DUTIES}** under it shows each leader their own.",
    ),
    (
        f"{DUTIES_CMD} → {BTN_SHARE_MEMBERS}",
        "Pick which duties members see and who is in charge of each, with no contact buttons.",
    ),
    (
        f"{DUTIES_CMD} → {BTN_POST_CONTACT}",
        "Post contact buttons for members. Each click opens a private thread with "
        "that member and the people assigned to the duty.",
    ),
)

# ── Outage catch-up digest ───────────────────────────────────────────────────

CATCHUP_TITLE = "Leadership Duties reminder: {duty}"
CATCHUP_TO_CHANNEL = "sent to #{channel}"
CATCHUP_TO_DMS = "sent as DMs to the people assigned"
