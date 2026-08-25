"""Tell an alliance when the config they gave the bot has stopped working.

One user story, two shapes. A guild points the bot at things it owns: a
Google Sheet, a Discord channel. Those rot. A tab gets renamed, a spreadsheet
gets unshared, a channel gets deleted or the bot's role loses View Channel in
a reorg. The background feature then stops, and until #413 nobody was told.

#413 proved the notice shape on the transfer watcher: name the thing in the
alliance's own words, say what's wrong in plain language, give the fix that
actually applies, deduplicate so a failure repeating every poll posts once,
re-nudge daily so a single post can't scroll away, and confirm recovery so
they know their fix worked. This module is that, generalized, so #414
(sheets) and #379 (channels) are two registrations rather than two
implementations.

Two things about #413 did not survive generalization:

* Its state lived in three columns on ``guild_transfer_config``. There are
  ~18 configured-channel fields alone, so state moved to
  ``guild_config_health``, one row per broken subject.
* It posted inline at the failure site. One channel reorg can break six
  subjects at once, so :func:`record` and :func:`clear` are now pure
  synchronous DB writes, and a separate notifier pass batches everything a
  guild currently owes into a single digest.

That split is also what makes the recording side callable from anywhere:
Phase 1 wires it into ``train.py`` / ``member_roster.py`` / ``storm.py``,
none of which want to grow a Discord send path.

Deliberately **not** Sentry-reported. Config rot is the alliance's to fix,
not a bot bug, and capturing it buries real bugs under one guild's renamed
tab (the ``config.is_user_config_sheet_error`` reasoning from #285 / #286,
extended by #413). It logs and skips.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

import config

logger = logging.getLogger(__name__)

# ── Problem kinds ────────────────────────────────────────────────────────────
#
# What broke, in terms the alliance can act on. Kept coarse on purpose: the
# kind picks the copy, so two kinds that lead to the same instruction would
# only be two ways to say the same sentence.

MISSING_TAB = "missing_tab"
MISSING_SHEET = "missing_sheet"
NO_ACCESS = "no_access"
CHANNEL_GONE = "channel_gone"
CHANNEL_NO_VIEW = "channel_no_view"
CHANNEL_NO_SEND = "channel_no_send"

SHEET_KINDS = frozenset({MISSING_TAB, MISSING_SHEET, NO_ACCESS})
CHANNEL_KINDS = frozenset({CHANNEL_GONE, CHANNEL_NO_VIEW, CHANNEL_NO_SEND})

# How long an unfixed problem stays quiet between posts. Inherited from
# #413's SHEET_ERROR_RENOTIFY_HOURS, which was picked so a problem that
# repeats every poll posts once, but a single post that scrolls away still
# comes back.
RENOTIFY_HOURS = 24

# Embeds cap at 25 fields, and a digest that long is unreadable anyway.
_MAX_DIGEST_FIELDS = 10


# ── Subject registry ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Subject:
    """A piece of guild config that can break, and how to talk about it.

    ``label`` is what the alliance calls the thing, not what the code calls
    it: "your transfer sheet", not "alliance_sheet_id". ``fix_hub`` /
    ``fix_btn`` name the surface that actually fixes *this* subject, which is
    why the registry exists at all. Pointing a permissions failure at the
    setup wizard would send leadership down a path that cannot fix it, the
    same trap #413's ``_fix_instruction`` was written to avoid.
    """

    key: str
    label: str
    fix_hub: str = ""
    fix_btn: str = ""


_SUBJECTS: dict[str, Subject] = {}


def register(subject: Subject) -> None:
    """Register a subject's copy. Called at import time by the owning module.

    Registration rather than a static table here, because this module is
    imported *by* the feature modules and importing them back would cycle.
    """
    _SUBJECTS[subject.key] = subject


def get_subject(key: str) -> Subject:
    """The registered subject, or a generic stand-in.

    Falling back rather than raising: a row can outlive its registration (a
    feature module fails to import, or a subject is renamed between deploys),
    and a vague notice is better than a notifier pass that dies and takes
    every other guild's digest with it.
    """
    return _SUBJECTS.get(key) or Subject(key=key, label="part of your setup")


# ── Records ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Problem:
    guild_id: int
    subject: str
    kind: str
    signature: str
    detail: str
    first_seen_at: str
    notified_at: str | None
    resolved_at: str | None

    @property
    def label(self) -> str:
        return get_subject(self.subject).label


def signature_for(kind: str, discriminator: str = "") -> str:
    """A stable dedup key for "which problem is currently blocking this".

    Built from the kind plus a stable discriminator (the tab name, the
    channel id) and deliberately **not** the diagnosis text, which can carry
    volatile wording from an API error message. Including that would make the
    same problem look new on every poll and re-alert the alliance each time.
    """
    return "|".join([(kind or "").strip(), (discriminator or "").strip()])


def _row_to_problem(row) -> Problem:
    return Problem(
        guild_id=row["guild_id"],
        subject=row["subject"],
        kind=row["kind"],
        signature=row["signature"],
        detail=row["detail"] or "",
        first_seen_at=row["first_seen_at"],
        notified_at=row["notified_at"],
        resolved_at=row["resolved_at"],
    )


def sheet_problem_kind(e: Exception) -> str | None:
    """Which alliance-fixable problem a gspread exception represents, or ``None``.

    ``None`` covers everything the alliance can't act on: a rate limit (429,
    which clears itself and would be a false alarm), a transient 5xx, or a bug
    in the bot. Those still log and skip; they just don't raise an alarm.

    Kept separate from ``config.is_user_config_sheet_error`` on purpose: that
    answers "should Sentry care", which includes 429. This answers "should the
    alliance be told", which does not.

    Lives here rather than in any one feature because transfer, train, the
    member roster and storm all read alliance-owned sheets and all classify
    the same failures (#414). #413 wrote it inside ``transfer_cog``, which was
    right when transfer was the only caller.
    """
    import gspread

    if isinstance(e, gspread.exceptions.WorksheetNotFound):
        return MISSING_TAB
    if isinstance(e, gspread.exceptions.SpreadsheetNotFound):
        return MISSING_SHEET
    # gspread's open_by_key() raises the built-in PermissionError (not its
    # own typed APIError) on a 403 — see config.describe_sheet_error.
    if isinstance(e, PermissionError):
        return NO_ACCESS
    if isinstance(e, gspread.exceptions.APIError):
        status = None
        resp = getattr(e, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)
        status = status or getattr(e, "code", None)
        if status == 404:
            return MISSING_SHEET
        if status == 403:
            return NO_ACCESS
    return None


def record_sheet_failure(
    guild_id: int,
    subject: str,
    e: Exception,
    *,
    tab: str = "",
    detail: str = "",
    now: datetime | None = None,
) -> bool:
    """Classify a sheet exception and record it. ``True`` if it was recorded.

    The one-liner for the common case: a feature caught something reading or
    writing an alliance sheet and wants the alliance told if it is theirs to
    fix. ``False`` means the failure was transient or a bot bug, so the caller
    should keep whatever Sentry / logging behaviour it already had.

    Callers with something more specific to say pass ``detail``; otherwise the
    generic per-kind copy is used. ``tab`` becomes the dedup discriminator, so
    a rename from one bad tab to a different bad tab reads as a new problem.
    """
    if not guild_id:
        return False  # legacy single-guild call path, nothing to attribute it to
    kind = sheet_problem_kind(e)
    if kind is None:
        return False
    if kind == MISSING_TAB and tab and not detail:
        detail = (
            f"That spreadsheet no longer has a tab named `{tab}`. It was most likely "
            "renamed, or the tab was deleted."
        )
    record(guild_id, subject, kind, detail, discriminator=tab, now=now)
    return True


def is_new_problem(guild_id: int, subject: str, kind: str, *, discriminator: str = "") -> bool:
    """Whether recording this would be a *different* problem than what's stored.

    For callers whose ``detail`` is expensive to build. Listing a
    spreadsheet's actual tab names, for instance, is a network round-trip
    worth making once when a rename is first spotted, and not worth making
    again on every poll for the next day.
    """
    signature = signature_for(kind, discriminator)
    with config._get_conn() as conn:
        row = conn.execute(
            "SELECT signature, resolved_at FROM guild_config_health "
            "WHERE guild_id = ? AND subject = ?",
            (int(guild_id), subject),
        ).fetchone()
    if row is None:
        return True
    # A resolved row is a recovery waiting to be announced, so the same
    # problem coming back is genuinely new again.
    return row["resolved_at"] is not None or row["signature"] != signature


def record(
    guild_id: int,
    subject: str,
    kind: str,
    detail: str,
    *,
    discriminator: str = "",
    now: datetime | None = None,
) -> None:
    """Record that ``subject`` is broken. Safe to call on every tick.

    Pure DB write, no Discord I/O, so a failing loop can call it without
    caring whether a notice is owed or when. The notifier decides that.

    A *different* problem on the same subject (different signature) resets
    the notify state, so the alliance hears about the new one instead of it
    hiding behind the old one's quiet window. The same problem recurring
    keeps ``first_seen_at`` and ``notified_at``, which is what holds the
    quiet window open. Either way ``resolved_at`` clears, since a subject
    that is failing right now is not recovered.
    """
    now = now or datetime.now(timezone.utc)
    signature = signature_for(kind, discriminator)
    stamp = now.isoformat()
    with config._get_conn() as conn:
        conn.execute(
            "INSERT INTO guild_config_health "
            "(guild_id, subject, kind, signature, detail, first_seen_at, "
            " notified_at, resolved_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL, NULL) "
            "ON CONFLICT(guild_id, subject) DO UPDATE SET "
            "  kind          = excluded.kind, "
            "  detail        = excluded.detail, "
            "  resolved_at   = NULL, "
            "  first_seen_at = CASE WHEN guild_config_health.signature = excluded.signature "
            "                       THEN guild_config_health.first_seen_at "
            "                       ELSE excluded.first_seen_at END, "
            "  notified_at   = CASE WHEN guild_config_health.signature = excluded.signature "
            "                       THEN guild_config_health.notified_at "
            "                       ELSE NULL END, "
            "  signature     = excluded.signature",
            (int(guild_id), subject, kind, signature, detail or "", stamp),
        )
        conn.commit()


def clear(guild_id: int, subject: str, *, now: datetime | None = None) -> None:
    """Mark ``subject`` healthy again. Safe to call on every clean tick.

    A problem the alliance was told about becomes a tombstone for the
    notifier to turn into a recovery line and then delete. One they were
    never told about is dropped outright: confirming a recovery nobody heard
    about would be the bot's first and only word on the subject.

    The healthy case is the overwhelmingly common one and every clean tick of
    every loop hits it, so it settles on one indexed SELECT that finds nothing
    rather than taking a write lock to delete a row that isn't there.
    """
    now = now or datetime.now(timezone.utc)
    with config._get_conn() as conn:
        row = conn.execute(
            "SELECT notified_at, resolved_at FROM guild_config_health "
            "WHERE guild_id = ? AND subject = ?",
            (int(guild_id), subject),
        ).fetchone()
        if row is None:
            return
        if row["notified_at"] is None:
            conn.execute(
                "DELETE FROM guild_config_health WHERE guild_id = ? AND subject = ?",
                (int(guild_id), subject),
            )
        elif row["resolved_at"] is None:
            conn.execute(
                "UPDATE guild_config_health SET resolved_at = ? WHERE guild_id = ? AND subject = ?",
                (now.isoformat(), int(guild_id), subject),
            )
        else:
            return  # already a tombstone, waiting on the notifier
        conn.commit()


def problems(guild_id: int) -> list[Problem]:
    """Everything currently broken for this guild, for the hub / setup banners.

    Excludes tombstones: a resolved subject is not a problem, it is a
    recovery line the notifier has not gotten to yet.
    """
    with config._get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM guild_config_health "
            "WHERE guild_id = ? AND resolved_at IS NULL "
            "ORDER BY first_seen_at",
            (int(guild_id),),
        ).fetchall()
    return [_row_to_problem(r) for r in rows]


def problems_for_subjects(guild_id: int, subjects: list[str]) -> list[Problem]:
    """:func:`problems` narrowed to one feature's subjects, for its own hub."""
    wanted = set(subjects)
    return [p for p in problems(guild_id) if p.subject in wanted]


def _pending(now: datetime) -> list[Problem]:
    """Every row that owes the alliance a post right now, across all guilds.

    Three cases: never notified, notified but unfixed past the re-nudge
    window, and resolved-after-being-notified (a recovery line).
    """
    cutoff = (now - timedelta(hours=RENOTIFY_HOURS)).isoformat()
    with config._get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM guild_config_health "
            "WHERE (resolved_at IS NULL AND notified_at IS NULL) "
            "   OR (resolved_at IS NULL AND notified_at <= ?) "
            "   OR (resolved_at IS NOT NULL AND notified_at IS NOT NULL) "
            "ORDER BY guild_id, first_seen_at",
            (cutoff,),
        ).fetchall()
    return [_row_to_problem(r) for r in rows]


def _mark_notified(guild_id: int, subjects: list[str], now: datetime) -> None:
    if not subjects:
        return
    placeholders = ",".join("?" for _ in subjects)
    with config._get_conn() as conn:
        conn.execute(
            f"UPDATE guild_config_health SET notified_at = ? "
            f"WHERE guild_id = ? AND subject IN ({placeholders})",
            (now.isoformat(), int(guild_id), *subjects),
        )
        conn.commit()


def _delete(guild_id: int, subjects: list[str]) -> None:
    if not subjects:
        return
    placeholders = ",".join("?" for _ in subjects)
    with config._get_conn() as conn:
        conn.execute(
            f"DELETE FROM guild_config_health WHERE guild_id = ? AND subject IN ({placeholders})",
            (int(guild_id), *subjects),
        )
        conn.commit()


# ── Channels (#379) ──────────────────────────────────────────────────────────


def check_channel(bot, channel_id) -> str | None:
    """Health of a configured channel from cache alone, or ``None`` if fine.

    Free: a channel the bot can see answers view/send out of
    ``permissions_for``, no REST call. Cheap enough for a per-minute loop,
    which is the whole reason the clock-driven post loops can adopt this.

    ``CHANNEL_GONE`` is deliberately the ambiguous answer. discord.py's cache
    only holds channels the gateway sent, and the gateway omits channels the
    bot cannot view, so a deleted channel and one the bot lost View Channel on
    are indistinguishable from here. :func:`check_channel_precise` separates
    them with one REST call, and the copy for this kind covers both.

    An unset channel is not a problem: plenty of guilds deliberately leave an
    optional post channel blank.
    """
    if not channel_id:
        return None
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return CHANNEL_GONE
    guild = getattr(channel, "guild", None)
    me = getattr(guild, "me", None)
    if me is None or not hasattr(channel, "permissions_for"):
        return None  # DM or a shape we can't reason about; don't invent a problem
    try:
        perms = channel.permissions_for(me)
    except Exception:  # noqa: BLE001 - a permissions lookup must not break a loop
        return None
    view = getattr(perms, "view_channel", None)
    send = getattr(perms, "send_messages", None)
    if not isinstance(view, bool) or not isinstance(send, bool):
        # Not a Permissions object. Every caller is inside a per-minute loop or
        # a rendering path, so an unreadable shape resolves to "no problem"
        # rather than a raised AttributeError or an invented alert.
        return None
    if not view:
        return CHANNEL_NO_VIEW
    if not send:
        return CHANNEL_NO_SEND
    return None


async def check_channel_precise(bot, channel_id) -> str | None:
    """:func:`check_channel`, but resolves the deleted-vs-invisible ambiguity.

    Costs one REST call, and only in the ambiguous case. Affordable when a
    human is waiting on a `/setup` screen; not affordable per-minute across
    every channel field of every guild, which is why the loops use the cheap
    version and this backs the pull surfaces.
    """
    cheap = check_channel(bot, channel_id)
    if cheap != CHANNEL_GONE:
        return cheap
    try:
        await bot.fetch_channel(int(channel_id))
    except discord.NotFound:
        return CHANNEL_GONE
    except discord.Forbidden:
        return CHANNEL_NO_VIEW
    except (discord.HTTPException, Exception):  # noqa: BLE001
        return CHANNEL_GONE  # can't tell; report the cheap answer
    # Fetchable but not in cache: the bot can reach it, so treat as healthy.
    return None


def note_channel(bot, guild_id: int, subject: str, channel_id) -> bool:
    """Record or clear a configured channel's health. ``True`` if it's usable.

    The one call a clock-driven post loop makes in place of its old
    ``if channel is None: continue``. Recording and clearing are both DB-only,
    so this is safe on every tick.
    """
    if not guild_id:
        return bool(channel_id)
    kind = check_channel(bot, channel_id)
    if kind is None:
        clear(guild_id, subject)
        return bool(channel_id)
    record(guild_id, subject, kind, "", discriminator=str(channel_id))
    return False


def resolve_configured_channel(bot, guild_id: int, subject: str, channel_id):
    """The channel if the bot can actually post in it, else ``None``.

    Records the problem as a side effect, so a loop that skips a guild has
    already told the alliance why. Returning ``None`` rather than raising
    keeps the existing skip-and-continue shape of every caller.
    """
    if not note_channel(bot, guild_id, subject, channel_id):
        return None
    return bot.get_channel(int(channel_id))


# ── Copy ─────────────────────────────────────────────────────────────────────

STUCK_TITLE = "⚠️ Some of your setup needs attention"
RECOVERED_TITLE = "✅ That's working again"

_REASONS = {
    MISSING_TAB: "That spreadsheet no longer has the tab I was told to read.",
    MISSING_SHEET: (
        "I can't open that spreadsheet at all. It may have been deleted, moved to a "
        "different account, or the link saved in setup is wrong."
    ),
    NO_ACCESS: (
        "I don't have permission to open that spreadsheet anymore. Its sharing "
        "settings were most likely changed."
    ),
    CHANNEL_GONE: (
        "I can't find that channel at all. It was either deleted, or my role lost "
        "**View Channel** there so I can no longer see it."
    ),
    CHANNEL_NO_VIEW: (
        "I can't see that channel anymore. My role most likely lost **View Channel** "
        "there, which happens easily during a channel reorg."
    ),
    CHANNEL_NO_SEND: "I can see that channel but I'm not allowed to post in it.",
}

_FIXES = {
    MISSING_TAB: "Either rename the tab back, or re-pick the tab's new name in setup.",
    MISSING_SHEET: "Re-pick the spreadsheet in setup.",
    NO_ACCESS: (
        "In Google Sheets, use **Share** to give the bot's service account Editor "
        "access to that spreadsheet."
    ),
    CHANNEL_GONE: (
        "If the channel still exists, give my role **View Channel** and **Send Messages** "
        "there. If it's gone, pick a different channel in setup."
    ),
    CHANNEL_NO_VIEW: (
        "Give my role **View Channel** and **Send Messages** there in the channel's "
        "permission settings, or pick a different channel in setup."
    ),
    CHANNEL_NO_SEND: (
        "Give my role **Send Messages** there in the channel's permission settings, "
        "or pick a different channel in setup."
    ),
}


def describe(problem: Problem) -> str:
    """The "what's wrong" line for a notice.

    ``problem.detail`` wins when the recording site had something more
    specific to say (the actual tab name, the tabs the spreadsheet does have),
    because the whole point of #413's notice was that a rename is obvious on
    sight instead of something leadership diffs by hand.
    """
    return problem.detail or _REASONS.get(problem.kind, "I couldn't read that.")


def fix_instruction(problem: Problem) -> str:
    """The "how to fix it" line, matched to both the problem and the feature.

    A permissions failure isn't fixed by re-picking the sheet, and a fix that
    names the wrong wizard is worse than no fix at all.
    """
    base = _FIXES.get(problem.kind, "Re-check this in setup.")
    subject = get_subject(problem.subject)
    if subject.fix_hub and subject.fix_btn:
        return f"{base}\nIn Discord: run {subject.fix_hub} and click **{subject.fix_btn}**."
    if subject.fix_hub:
        return f"{base}\nIn Discord: run {subject.fix_hub}."
    return base


def build_digest_embed(items: list[Problem]) -> discord.Embed:
    """One embed covering everything currently broken for a guild.

    Batched rather than one embed per subject: a single channel reorg can
    break several at once, and six separate red posts read as six separate
    emergencies.
    """
    count = len(items)
    lead = (
        "Something I was told to use has stopped working, so the feature that "
        "depends on it isn't running."
        if count == 1
        else f"{count} things I was told to use have stopped working, so the features "
        "that depend on them aren't running."
    )
    embed = discord.Embed(title=STUCK_TITLE, description=lead, color=discord.Color.red())
    for problem in items[:_MAX_DIGEST_FIELDS]:
        value = f"{describe(problem)}\n\n{fix_instruction(problem)}"
        embed.add_field(name=problem.label[:256], value=value[:1024], inline=False)
    remaining = count - _MAX_DIGEST_FIELDS
    if remaining > 0:
        embed.add_field(
            name="And more",
            value=f"{remaining} other thing(s) too. Check your setup screens for the full list.",
            inline=False,
        )
    embed.set_footer(text="I'll keep checking, and I'll say so here when it's working again.")
    return embed


def build_recovery_embed(items: list[Problem]) -> discord.Embed:
    """Confirm a fix worked.

    Worth its own post: the alliance was told the feature was dead, went and
    changed something, and otherwise has no way to know whether it took.
    """
    names = ", ".join(p.label for p in items)
    return discord.Embed(
        title=RECOVERED_TITLE,
        description=f"I can read {names} again. Back to normal.",
        color=discord.Color.green(),
    )


# ── Delivery ─────────────────────────────────────────────────────────────────


def _can_post(channel, guild) -> bool:
    me = getattr(guild, "me", None)
    if me is None or not hasattr(channel, "permissions_for"):
        return False
    try:
        perms = channel.permissions_for(me)
    except Exception:  # noqa: BLE001 - a permissions lookup must not break the pass
        return False
    return bool(perms.view_channel and perms.send_messages)


def resolve_notice_channel(bot, guild):
    """Where a guild's config-health notice goes, or ``None``.

    The leadership channel is the right audience: this is an admin task for
    whoever runs setup, not a member-facing notice. But the leadership
    channel is itself a piece of config that can rot, and it is the one
    subject whose breakage this mechanism cannot announce in the usual place.
    So it falls back to the guild's system channel, and past that gives up
    and leaves it to the setup and hub banners, which is exactly the case
    those pull surfaces exist for.

    No DM to the owner. It is intrusive for something that is not urgent, and
    it is not a channel the alliance chose.
    """
    if guild is None:
        return None
    cfg = config.get_config(guild.id)
    channel_id = (getattr(cfg, "leadership_channel_id", 0) or 0) if cfg else 0
    if channel_id:
        channel = bot.get_channel(channel_id)
        if channel is not None and _can_post(channel, guild):
            return channel
    system = getattr(guild, "system_channel", None)
    if system is not None and _can_post(system, guild):
        return system
    return None


async def _send(bot, guild, embed) -> bool:
    channel = resolve_notice_channel(bot, guild)
    if channel is None:
        return False
    try:
        await channel.send(embed=embed)
        return True
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning("[CONFIG-HEALTH] guild %s: could not post notice: %s", guild.id, e)
        return False


async def run_notifier_pass(bot, *, now: datetime | None = None) -> int:
    """Post each guild's outstanding config-health digest. Returns guilds posted.

    Marks rows notified (and deletes recovery tombstones) whether or not the
    send landed. A guild with no reachable channel would otherwise re-attempt
    on every pass forever, and the pull surfaces already carry the state for
    exactly that case.
    """
    now = now or datetime.now(timezone.utc)
    pending = _pending(now)
    if not pending:
        return 0

    by_guild: dict[int, list[Problem]] = {}
    for problem in pending:
        by_guild.setdefault(problem.guild_id, []).append(problem)

    posted = 0
    for guild_id, items in by_guild.items():
        guild = bot.get_guild(guild_id)
        if guild is None:
            # Bot was removed from the guild; the rows are dead weight.
            _delete(guild_id, [p.subject for p in items])
            continue
        broken = [p for p in items if p.resolved_at is None]
        recovered = [p for p in items if p.resolved_at is not None]
        sent_any = False
        if broken:
            sent_any |= await _send(bot, guild, build_digest_embed(broken))
            _mark_notified(guild_id, [p.subject for p in broken], now)
        if recovered:
            sent_any |= await _send(bot, guild, build_recovery_embed(recovered))
            _delete(guild_id, [p.subject for p in recovered])
        if sent_any:
            posted += 1
    return posted
