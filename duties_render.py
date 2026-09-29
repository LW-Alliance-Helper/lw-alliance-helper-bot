"""Leadership Duties (#687): what every surface looks like, as pure functions.

Views in `duties_hub` and `duties_panel` decide *when* to show something;
this module decides *what it says*, from plain values and a `name_of`
callback that turns a user id into however the surface shows a person (a
mention inside an embed, a display name in a thread title). Nothing here
reads the database or calls Discord's API, so every screen can be tested and
mocked up from fixtures.

All copy comes from `duties_copy`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Iterable, Sequence

import discord

import duties as d
import duties_copy as c
from time_helpers import SERVER_TZ

NameOf = Callable[[int], str]

# Discord's hard limits (DESIGN.md, Hard limits), with room kept back so a
# page never lands exactly on the edge.
_DESCRIPTION_BUDGET = 3800
_FIELD_VALUE = 1024
_THREAD_NAME = 100
#: The contact buttons are one message: five rows of five.
CONTACT_CAP = 25


def mention(user_id: int) -> str:
    return f"<@{user_id}>"


def _join(names: Sequence[str]) -> str:
    """`A`, `A and B`, `A, B and C`: how a person lists people."""
    names = [n for n in names if n]
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _plural(n: int, pair: tuple[str, str]) -> str:
    return pair[0] if n == 1 else pair[1]


# ── Holders ──────────────────────────────────────────────────────────────────


def people(duty: d.Duty, kind: str, name_of: NameOf) -> str:
    """One slot's holders for display: names, then "anyone in leadership"
    when it is one of the choices, then `open` for each open position, or
    `nobody` when the slot is empty."""
    parts = [name_of(uid) for uid in duty.holders(kind)]
    if duty.has_anyone(kind):
        parts.append(c.ANYONE_IN_LEADERSHIP)
    parts += [c.OPEN_MARK] * duty.open_count(kind)
    return ", ".join(parts) if parts else c.NOBODY


def duty_block(duty: d.Duty, name_of: NameOf) -> str:
    """A duty as the hub lists it: name, then primaries, then backups, one
    line each so it reads on a phone."""
    head = f"**{duty.name}**" + (c.PAUSED_MARK if duty.paused else "")
    return "\n".join(
        [
            head,
            c.LINE_PRIMARY.format(who=people(duty, d.SLOT_PRIMARY, name_of)),
            c.LINE_BACKUP.format(who=people(duty, d.SLOT_BACKUP, name_of)),
        ]
    )


# ── Hub ──────────────────────────────────────────────────────────────────────

#: Where the hub stands with Premium. Decides the intro and which controls
#: are live, never what the list shows: a lapsed alliance still sees
#: everything it recorded (UX.md, Premium is additive).
STATE_PREMIUM = "premium"
STATE_LAPSED = "lapsed"
STATE_FREE = "free"


def hub_pages(duties: Sequence[d.Duty], name_of: NameOf) -> list[str]:
    """The duty list cut into pages that fit an embed description.

    Grouped by category with the category as a heading. A category that
    spills onto the next page repeats its heading there, so a page never
    starts mid-group with nothing saying which group it is.
    """
    pages: list[str] = []
    current: list[str] = []
    size = 0
    for category, items in d.by_category(duties):
        heading = f"### {category or c.HUB_NO_CATEGORY}"
        wrote_heading = False
        for duty in items:
            block = duty_block(duty, name_of)
            needed = len(block) + 2 + (0 if wrote_heading else len(heading) + 1)
            if current and size + needed > _DESCRIPTION_BUDGET:
                pages.append("\n\n".join(current))
                current, size, wrote_heading = [], 0, False
                needed = len(block) + 2 + len(heading) + 1
            if not wrote_heading:
                current.append(heading)
                wrote_heading = True
            current.append(block)
            size += needed
    if current:
        pages.append("\n\n".join(current))
    return pages or [""]


def hub_embed(
    duties: Sequence[d.Duty],
    name_of: NameOf,
    *,
    state: str,
    page: int = 0,
) -> tuple[discord.Embed, int]:
    """The `/duties` hub. Returns the embed and the page count."""
    pages = hub_pages(duties, name_of)
    page = max(0, min(page, len(pages) - 1))
    if state == STATE_FREE and not duties:
        intro = c.HUB_FREE
    elif state == STATE_LAPSED or (state == STATE_FREE and duties):
        intro = c.HUB_LAPSED
    elif not duties:
        intro = f"{c.HUB_INTRO}\n\n{c.HUB_EMPTY}"
    else:
        intro = c.HUB_INTRO
    body = pages[page]
    description = f"{intro}\n\n{body}" if body else intro
    embed = discord.Embed(
        title=c.HUB_TITLE, description=description[:4096], color=discord.Color.blurple()
    )
    if duties:
        contact = sum(1 for x in duties if x.contact_enabled)
        embed.set_footer(text=c.HUB_FOOTER.format(count=len(duties), contact=contact))
    return embed, len(pages)


# ── Workload ─────────────────────────────────────────────────────────────────


def _count_line(template: str, total: int, paused: int) -> str:
    line = template.format(n=total)
    if paused:
        line += c.WORKLOAD_PAUSED.format(n=paused)
    return line


def workload_block(load: d.LeaderLoad, name_of: NameOf) -> str:
    """One leader: the name as the heading, the counts underneath."""
    head = f"**{name_of(load.user_id)}**"
    if not load.in_leadership:
        head += c.WORKLOAD_NOT_LEADERSHIP
    return "\n".join(
        [
            head,
            _count_line(c.WORKLOAD_PRIMARY, load.primary, load.primary_paused),
            _count_line(c.WORKLOAD_BACKUP, load.backup, load.backup_paused),
        ]
    )


def open_positions_text(slots: Iterable[d.OpenSlot]) -> str:
    lines = []
    for slot in slots:
        pair = c.KIND_PRIMARY if slot.kind == d.SLOT_PRIMARY else c.KIND_BACKUP
        lines.append(
            c.WORKLOAD_OPEN_LINE.format(
                duty=slot.duty.name,
                paused=c.PAUSED_MARK if slot.duty.paused else "",
                n=slot.count,
                kind=_plural(slot.count, pair),
            )
        )
    text = "\n".join(lines) or c.WORKLOAD_OPEN_NONE
    if len(text) > _FIELD_VALUE:
        text = text[: _FIELD_VALUE - 2].rsplit("\n", 1)[0] + "\n…"
    return text


def workload_embed(
    loads: Sequence[d.LeaderLoad],
    slots: Sequence[d.OpenSlot],
    name_of: NameOf,
    *,
    sort_key: NameOf,
    role_known: bool,
    page: int = 0,
    per_page: int = 12,
) -> tuple[discord.Embed, int]:
    """The workload view. Leaders in name order, never by how much they
    hold: the view reflects the alliance's own list back and does not rank
    anyone (UX.md, Principle 6). No color but the neutral one for the same
    reason. Open positions sit on the first page."""
    ordered = sorted(loads, key=lambda x: sort_key(x.user_id).casefold())
    page_count = max(1, -(-len(ordered) // per_page))
    page = max(0, min(page, page_count - 1))
    chunk = ordered[page * per_page : (page + 1) * per_page]
    parts = [c.WORKLOAD_INTRO]
    if not role_known:
        parts.append(c.WORKLOAD_NO_ROLE)
    parts.extend(workload_block(x, name_of) for x in chunk)
    embed = discord.Embed(
        title=c.WORKLOAD_TITLE,
        description="\n\n".join(parts)[:4096],
        color=discord.Color.blurple(),
    )
    if page == 0:
        embed.add_field(name=c.WORKLOAD_OPEN_FIELD, value=open_positions_text(slots), inline=False)
    return embed, page_count


# ── My duties ────────────────────────────────────────────────────────────────


def _duty_list(items: Sequence[d.Duty]) -> str:
    if not items:
        return c.MY_EMPTY_FIELD
    text = "\n".join(f"• **{x.name}**" + (c.PAUSED_MARK if x.paused else "") for x in items)
    if len(text) > _FIELD_VALUE:
        text = text[: _FIELD_VALUE - 2].rsplit("\n", 1)[0] + "\n…"
    return text


def my_duties_embed(primary: Sequence[d.Duty], backup: Sequence[d.Duty]) -> discord.Embed:
    embed = discord.Embed(title=c.MY_TITLE, color=discord.Color.blurple())
    if not primary and not backup:
        embed.description = c.MY_NONE
        return embed
    # Field names stay put whether or not a list is empty (DESIGN.md, Field
    # names): the body says "None" instead.
    embed.add_field(name=c.MY_PRIMARY_FIELD, value=_duty_list(primary), inline=False)
    embed.add_field(name=c.MY_BACKUP_FIELD, value=_duty_list(backup), inline=False)
    return embed


# ── Duty editor ──────────────────────────────────────────────────────────────


def editor_embed(duty: d.Duty, name_of: NameOf) -> discord.Embed:
    lines = []
    if duty.category:
        lines.append(c.EDITOR_CATEGORY.format(category=duty.category))
    if duty.description:
        lines.append(duty.description)
    lines.append("")
    lines.append(c.LINE_PRIMARY.format(who=people(duty, d.SLOT_PRIMARY, name_of)))
    lines.append(c.LINE_BACKUP.format(who=people(duty, d.SLOT_BACKUP, name_of)))
    lines.append(c.EDITOR_CONTACT_ON if duty.contact_enabled else c.EDITOR_CONTACT_OFF)
    lines.append("")
    lines.append(c.EDITOR_UNSAVED)
    return discord.Embed(
        title=c.EDITOR_TITLE.format(name=duty.name or "…")[:256],
        description="\n".join(lines)[:4096],
        color=discord.Color.blurple(),
    )


def open_option_label(n: int, kind_word: str) -> str:
    template = c.EDITOR_OPEN_OPTION[min(n, 2)]
    return template.format(n=n, kind=kind_word)


# ── Reminders ────────────────────────────────────────────────────────────────


def _clock(at, tz_name: str | None) -> str:
    from wizard_time import _format_time_with_tz

    return _format_time_with_tz(at.strftime("%H:%M"), tz_name)


def _short_date(day: date) -> str:
    return f"{day.strftime('%b')} {day.day}"


def ordinal(n: int) -> str:
    """1st, 2nd, 3rd, 4th, 11th, 21st."""
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def reminder_when(reminder: d.DutyReminder, tz_name: str | None) -> str:
    """The schedule in words: `Mondays and Thursdays at 9:00pm EDT`."""
    if reminder.at is None:
        return c.WHEN_UNSET
    clock = _clock(reminder.at, tz_name)
    kind = reminder.schedule_type
    if kind == d.SCHEDULE_DAILY:
        return c.WHEN_DAILY.format(time=clock)
    if kind == d.SCHEDULE_WEEKDAYS:
        if not reminder.weekdays:
            return c.WHEN_UNSET
        if len(reminder.weekdays) == 7:
            return c.WHEN_DAILY.format(time=clock)
        days = _join([c.WEEKDAY_PLURALS[i] for i in sorted(reminder.weekdays)])
        return c.WHEN_WEEKDAYS.format(days=days, time=clock)
    if kind == d.SCHEDULE_WEEKLY:
        if not reminder.weekdays:
            return c.WHEN_UNSET
        return c.WHEN_WEEKLY.format(day=c.WEEKDAY_NAMES[min(reminder.weekdays)], time=clock)
    anchor = reminder.anchor_date
    if anchor is None:
        return c.WHEN_UNSET
    start = _short_date(anchor)
    if kind == d.SCHEDULE_EVERY_2_DAYS:
        return c.WHEN_EVERY_OTHER_DAY.format(date=start, time=clock)
    if kind == d.SCHEDULE_EVERY_3_DAYS:
        return c.WHEN_EVERY_3_DAYS.format(date=start, time=clock)
    if kind == d.SCHEDULE_EVERY_2_WEEKS:
        return c.WHEN_EVERY_2_WEEKS.format(date=start, time=clock)
    if kind == d.SCHEDULE_MONTHLY:
        template = c.WHEN_MONTHLY_LATE if anchor.day > 28 else c.WHEN_MONTHLY
        return template.format(nth=ordinal(anchor.day), time=clock)
    return c.WHEN_UNSET


_SEND_LABELS = {
    d.SEND_PRIMARIES: c.SEND_PRIMARIES,
    d.SEND_BACKUPS: c.SEND_BACKUPS,
    d.SEND_BOTH: c.SEND_BOTH,
    d.SEND_CHANNEL: c.SEND_CHANNEL,
    d.SEND_THREAD: c.SEND_THREAD,
}


def send_label(send_to: str) -> str:
    return _SEND_LABELS.get(send_to, send_to)


def reminder_dest(reminder: d.DutyReminder) -> str:
    if reminder.send_to in d.POST_TARGETS:
        if reminder.channel_id:
            return c.DEST_CHANNEL.format(channel=f"<#{reminder.channel_id}>")
        return c.DEST_NO_THREAD if reminder.send_to == d.SEND_THREAD else c.DEST_NO_CHANNEL
    return send_label(reminder.send_to)


def anyone_goes_to_a_post(duty: d.Duty, reminder: d.DutyReminder) -> bool:
    """A DM reminder whose wanted slots include "anyone in leadership":
    that half posts in a channel instead of every leader's DMs."""
    if reminder.send_to in d.POST_TARGETS:
        return False
    wanted = {
        d.SEND_PRIMARIES: (d.SLOT_PRIMARY,),
        d.SEND_BACKUPS: (d.SLOT_BACKUP,),
        d.SEND_BOTH: (d.SLOT_PRIMARY, d.SLOT_BACKUP),
    }.get(reminder.send_to, ())
    return any(duty.has_anyone(kind) for kind in wanted)


def _preview(text: str, limit: int = 120) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


def reminder_list_embed(
    duty: d.Duty, reminders: Sequence[d.DutyReminder], tz_name: str | None
) -> discord.Embed:
    embed = discord.Embed(
        title=c.REMINDERS_TITLE.format(name=duty.name)[:256], color=discord.Color.blurple()
    )
    if not reminders:
        embed.description = c.REMINDERS_NONE.format(name=duty.name)
        return embed
    rows = []
    if duty.paused:
        rows.append(c.REMINDERS_PAUSED_NOTE.format(name=duty.name))
    for n, reminder in enumerate(reminders, start=1):
        rows.append(
            c.REMINDER_ROW.format(
                n=n,
                when=reminder_when(reminder, tz_name),
                dest=reminder_dest(reminder),
                state=c.REMINDER_STATE_ON if reminder.enabled else c.REMINDER_STATE_OFF,
            )
            + (f"\n> {_preview(reminder.message)}" if reminder.message else "")
        )
    embed.description = "\n\n".join(rows)[:4096]
    return embed


def reminder_editor_embed(
    duty: d.Duty,
    draft: d.DutyReminder,
    tz_name: str | None,
    name_of: NameOf,
    *,
    leadership_channel_id: int,
) -> discord.Embed:
    lines = [
        c.REMINDER_WHEN.format(when=reminder_when(draft, tz_name)),
        c.REMINDER_SENDS.format(dest=reminder_dest(draft)),
    ]
    anyone_post = anyone_goes_to_a_post(duty, draft)
    if draft.send_to in d.POST_TARGETS or anyone_post:
        lines.append(c.REMINDER_PING_ON if draft.ping_holders else c.REMINDER_PING_OFF)
    if anyone_post:
        target = draft.channel_id or leadership_channel_id
        where = f"<#{target}>" if target else "the leadership channel"
        lines.append("")
        lines.append(c.REMINDER_ANYONE_NOTE.format(channel=where))
    lines.append("")
    lines.append(c.REMINDER_PLACEHOLDER_HELP)
    embed = discord.Embed(
        title=c.REMINDER_EDITOR_TITLE.format(name=duty.name)[:256],
        description="\n".join(lines)[:4096],
        color=discord.Color.blurple(),
    )
    preview = (
        d.render_reminder(draft, duty, name_of=name_of, anyone_in_leadership=c.ANYONE_IN_LEADERSHIP)
        if draft.message
        else c.REMINDER_NO_MESSAGE
    )
    embed.add_field(name=c.REMINDER_PREVIEW_FIELD, value=preview[:_FIELD_VALUE], inline=False)
    return embed


def reminder_text(
    duty: d.Duty, reminder: d.DutyReminder, name_of: NameOf, *, server: str, dm: bool
) -> str:
    """What a firing actually sends. A DM names the server it came from,
    because out of the server a leader has nothing else to place it by."""
    text = d.render_reminder(
        reminder, duty, name_of=name_of, anyone_in_leadership=c.ANYONE_IN_LEADERSHIP
    )
    if dm:
        out = c.REMINDER_DM.format(duty=duty.name, server=server, text=text)
    else:
        out = c.REMINDER_POST.format(duty=duty.name, text=text)
    return out[:2000]


def fallback_text(duty: d.Duty, who: Sequence[str], text: str) -> str:
    """The leadership-channel post for DMs that couldn't be delivered."""
    return c.REMINDER_FALLBACK.format(who=_join(list(who)), duty=duty.name, text=text)[:2000]


# ── Contact settings ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ContactCheck:
    """What the settings screen found about the ticket channel."""

    bot_can_thread: bool = True
    members_can_view: bool | None = True  # None: no member role to check
    manage_threads: tuple[str, ...] = ()  # mentions of roles/members holding it


def contact_warnings(check: ContactCheck, channel: str) -> list[str]:
    out = []
    if not check.bot_can_thread:
        out.append(c.WARN_BOT_THREADS.format(channel=channel))
    if check.members_can_view is None:
        out.append(c.WARN_NO_MEMBER_ROLE.format(channel=channel))
    elif not check.members_can_view:
        out.append(c.WARN_MEMBERS_CANT_VIEW.format(channel=channel))
    if check.manage_threads:
        out.append(
            c.WARN_MANAGE_THREADS.format(channel=channel, who=_join(list(check.manage_threads)))
        )
    return out


def contact_settings_embed(
    *,
    panel_channel_id: int,
    ticket_channel_id: int,
    posted: bool,
    contact_count: int,
    warnings: Sequence[str],
) -> discord.Embed:
    buttons = f"<#{panel_channel_id}>" if panel_channel_id else c.CONTACT_NOT_SET
    if ticket_channel_id and ticket_channel_id != panel_channel_id:
        threads = f"<#{ticket_channel_id}>"
    elif panel_channel_id:
        threads = c.CONTACT_SAME_CHANNEL
    else:
        threads = c.CONTACT_NOT_SET
    lines = [
        c.CONTACT_INTRO,
        "",
        c.CONTACT_BUTTONS_LINE.format(channel=buttons),
        c.CONTACT_THREADS_LINE.format(channel=threads),
        c.CONTACT_POSTED if posted else c.CONTACT_NOT_POSTED,
        c.CONTACT_COUNT.format(n=contact_count),
    ]
    if warnings:
        lines.append("")
        lines.extend(warnings)
    # Orange when there is something to fix, the neutral blurple otherwise
    # (DESIGN.md, Color).
    color = discord.Color.orange() if warnings else discord.Color.blurple()
    return discord.Embed(title=c.CONTACT_TITLE, description="\n".join(lines)[:4096], color=color)


# ── The posted contact buttons ───────────────────────────────────────────────


def contact_duties(duties: Iterable[d.Duty]) -> list[d.Duty]:
    """The duties that get a button: contact on and not paused, in
    leadership's order, capped at what one message can hold."""
    return [x for x in duties if x.contact_enabled and not x.paused][:CONTACT_CAP]


def _who(duty: d.Duty, kind: str, name_of: NameOf) -> list[str]:
    names = [name_of(uid) for uid in duty.holders(kind)]
    if duty.has_anyone(kind):
        names.append(c.ANYONE_IN_LEADERSHIP)
    return names


def panel_embed(duties: Sequence[d.Duty], name_of: NameOf) -> discord.Embed:
    """The member-facing post. Written for someone with no idea what the bot
    is (UX.md, members): it says what the buttons do, then who handles
    what."""
    shown = contact_duties(duties)
    embed = discord.Embed(
        title=c.PANEL_TITLE,
        description=c.PANEL_INTRO if shown else c.PANEL_EMPTY,
        color=discord.Color.blurple(),
    )
    for duty in shown:
        lines = []
        if duty.description:
            lines.append(duty.description)
        primaries = _who(duty, d.SLOT_PRIMARY, name_of)
        if primaries:
            lines.append(c.PANEL_HELD_BY.format(who=_join(primaries)))
        backups = _who(duty, d.SLOT_BACKUP, name_of)
        if backups:
            lines.append(c.PANEL_BACKUP.format(who=_join(backups)))
        embed.add_field(
            name=duty.name[:256], value=("\n".join(lines) or "​")[:_FIELD_VALUE], inline=False
        )
    return embed


# ── Categories ───────────────────────────────────────────────────────────────


def category_count(n: int) -> str:
    return c.CATEGORY_COUNT[min(n, 2)].format(n=n)


def categories_embed(categories: Sequence) -> discord.Embed:
    """The category manager: each category and how many duties are in it."""
    if not categories:
        body = f"{c.CATEGORIES_INTRO}\n\n{c.CATEGORIES_NONE}"
    else:
        rows = [
            c.CATEGORY_ROW.format(name=x.name, n=category_count(x.duty_count)) for x in categories
        ]
        body = c.CATEGORIES_INTRO + "\n\n" + "\n".join(rows)
    return discord.Embed(
        title=c.CATEGORIES_TITLE, description=body[:4096], color=discord.Color.blurple()
    )


def category_delete_confirm(name: str, n: int) -> str:
    moved = c.CATEGORY_DELETE_MOVED[min(n, 2)].format(n=n, other=f"**{c.HUB_NO_CATEGORY}**")
    return c.CATEGORY_DELETE_CONFIRM.format(name=name, moved=moved)


# ── Tickets ──────────────────────────────────────────────────────────────────


def thread_name(duty_name: str, member_name: str, now: datetime) -> str:
    """`Disputes · TestPlayer · 27 Sep 14:05`, in server time (UX.md,
    server time), cut to Discord's 100. The member's name is cut first, so
    the duty and the time survive a long display name."""
    when = now.astimezone(SERVER_TZ).strftime(c.THREAD_TIME_FORMAT)
    fixed = c.THREAD_NAME.format(duty=duty_name, member="", when=when)
    room = max(1, _THREAD_NAME - len(fixed))
    name = c.THREAD_NAME.format(duty=duty_name, member=member_name[:room], when=when)
    return name[:_THREAD_NAME]


def thread_opener(
    duty: d.Duty, member_id: int, holder_ids: Sequence[int], *, role_mention: str = ""
) -> str:
    """The first message in a thread. `role_mention` is the Leadership
    role, named when "anyone in leadership" holds the duty; it reads as the
    role and never pings (the message allows user mentions only)."""
    holders = [mention(uid) for uid in holder_ids]
    if role_mention:
        holders.append(role_mention)
    return c.THREAD_OPENER.format(
        member=mention(member_id),
        duty=duty.name,
        holders=_join(holders),
        s="s" if len(holders) == 1 else "",
        close=c.BTN_CLOSE,
    )


# ── Config health ────────────────────────────────────────────────────────────


def departure_detail(departures: Iterable) -> str:
    """The notice text naming who left and what they held. One line each."""
    lines = []
    for dep in departures:
        duties_text = _join([f"**{n}**" for n in dep.duty_names])
        many = len(dep.duty_names) != 1
        lines.append(
            c.HOLDER_LEFT_DETAIL.format(
                name=dep.name or "Someone",
                their="Their duties" if many else "Their duty",
                duties=duties_text,
                have="have" if many else "has",
            )
        )
    return "\n".join(lines)
