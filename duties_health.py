"""Leadership Duties (#687): the things an alliance owns that can break, and
the notice when a holder leaves.

Registered at import time, as `config_health` asks of the module that owns
the subjects. Each fix names the `/duties` button that actually fixes it.
"""

from __future__ import annotations

import config_health
import duties_copy as c
import duties_db
import duties_render

#: The channel a reminder posts in, or the leadership channel standing in
#: for it when the backup is "anyone in leadership". One subject for every
#: reminder in the server, per #379's rule that one broken channel is one
#: notice.
REMINDER_CHANNEL = "duties.reminder_channel"
#: Where the contact buttons are posted.
PANEL_CHANNEL = "duties.panel_channel"
#: Where ticket threads open: the bot can't open threads there, or members
#: can't see it.
TICKET_CHANNEL = "duties.ticket_channel"
#: Someone holding duties left the server.
HOLDERS = "duties.holders"

for _key, _label, _btn in (
    (REMINDER_CHANNEL, c.SUBJECT_REMINDER_CHANNEL, c.BTN_REMINDERS),
    (PANEL_CHANNEL, c.SUBJECT_PANEL_CHANNEL, c.BTN_CONTACT),
    (TICKET_CHANNEL, c.SUBJECT_TICKET_CHANNEL, c.BTN_CONTACT),
    (HOLDERS, c.SUBJECT_HOLDERS, c.BTN_EDIT),
):
    config_health.register(
        config_health.Subject(key=_key, label=_label, fix_hub=c.DUTIES_CMD, fix_btn=_btn)
    )


def note_departures(guild_id: int) -> None:
    """Record, or re-record, the notice naming everyone whose leaving left
    positions nobody has been back to.

    The discriminator is the set of people, so someone new leaving is a new
    problem and is posted promptly rather than waiting out the quiet window
    of the last one. Sync: a DB write, no Discord I/O.
    """
    deps = duties_db.departures(guild_id)
    if not deps:
        config_health.dismiss(guild_id, HOLDERS)
        return
    config_health.record(
        guild_id,
        HOLDERS,
        config_health.HOLDER_LEFT,
        duties_render.departure_detail(deps),
        discriminator=",".join(str(x.user_id) for x in sorted(deps, key=lambda x: x.user_id)),
    )


def sync_departures(guild_id: int) -> None:
    """After leadership saves a duty. Dealing with everyone ends the notice
    without a recovery line (nothing "started working again"); dealing with
    some of them narrows what the next re-nudge says without making it a new
    problem to post about straight away."""
    deps = duties_db.departures(guild_id)
    if not deps:
        config_health.dismiss(guild_id, HOLDERS)
        return
    config_health.set_detail(guild_id, HOLDERS, duties_render.departure_detail(deps))
