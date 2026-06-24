"""member_stats.py — `/member_stats` lookup command (#56).

A single command that consolidates one member's identity, power trend, storm
participation, train-conductor history, and survey activity into one embed.
Pure read/render over data the bot already keeps — no new tables, no new
Sheet tabs, no writes.

Two embed views, chosen by the REQUESTER's role (not the target's):
  - member view: identity, power, storm sign-ups + attendance, train count,
    survey last-response dates.
  - leadership view: everything in the member view plus the management-only
    breakdowns (storm primary/sub/sit-out, train reason breakdown).

Self-lookup defaults to ephemeral with a Share button; leadership lookup of
another member defaults to visible with an `ephemeral` override.

Storm section sources (each by a different key, so they degrade independently):
sign-up rate from the poll votes (`storm_signups`, by Discord ID), attendance
from the Per-Member participation Log (by name), and leadership-only placement
(primary/sub from `storm_team_plans`, "sat out" derived as available-but-not-
placed) over the recent-event window.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

logger = logging.getLogger(__name__)

_MANUAL_MEMBER_NOTE = "Manual member (no Discord)"


@dataclass
class Target:
    """The member being looked up, resolved against the roster."""

    name: str  # display name as it appears on the roster / sheets
    discord_id: Optional[int]
    joined: str  # raw joined-date cell from the roster, or ""

    @property
    def is_manual(self) -> bool:
        return self.discord_id is None


# ── Roster resolution ────────────────────────────────────────────────────────


def _roster_rows(guild_id: int) -> tuple[dict, list[list[str]]]:
    """Return (roster_config, all_values). Raises on sheet failure — callers
    soft-handle."""
    import config

    rcfg = config.get_member_roster_config(guild_id)
    values = config.read_member_roster_values(guild_id, rcfg.get("tab_name") or "Member Roster")
    return rcfg, values


def _cell(row: list[str], idx: int) -> str:
    return row[idx].strip() if 0 <= idx < len(row) else ""


def _resolve_self(guild_id: int, discord_id: int) -> Optional[Target]:
    """Find the caller's own roster row by Discord ID."""
    try:
        rcfg, values = _roster_rows(guild_id)
    except Exception as e:
        logger.warning("[MEMBERSTATS] roster read failed (self) guild=%s: %s", guild_id, e)
        return None
    did_col = int(rcfg.get("discord_id_col", 0))
    disp_col = int(rcfg.get("display_col", 2))
    name_col = int(rcfg.get("name_col", 1))
    joined_col = int(rcfg.get("joined_col", 3))
    for row in values[1:]:
        if _cell(row, did_col) == str(discord_id):
            name = _cell(row, disp_col) or _cell(row, name_col)
            return Target(name=name, discord_id=discord_id, joined=_cell(row, joined_col))
    return None


def _resolve_named(guild_id: int, query: str) -> Optional[Target]:
    """Find a roster row by display name / name (case-insensitive)."""
    try:
        rcfg, values = _roster_rows(guild_id)
    except Exception as e:
        logger.warning("[MEMBERSTATS] roster read failed (named) guild=%s: %s", guild_id, e)
        return None
    did_col = int(rcfg.get("discord_id_col", 0))
    disp_col = int(rcfg.get("display_col", 2))
    name_col = int(rcfg.get("name_col", 1))
    joined_col = int(rcfg.get("joined_col", 3))
    q = query.strip().lower()
    for row in values[1:]:
        disp = _cell(row, disp_col)
        nm = _cell(row, name_col)
        if q in (disp.lower(), nm.lower()):
            raw_id = _cell(row, did_col)
            did = int(raw_id) if raw_id.isdigit() else None
            return Target(name=disp or nm, discord_id=did, joined=_cell(row, joined_col))
    return None


def _roster_names(guild_id: int, limit: int = 25, prefix: str = "") -> list[str]:
    """Roster display names for autocomplete, filtered by prefix."""
    try:
        rcfg, values = _roster_rows(guild_id)
    except Exception:
        return []
    disp_col = int(rcfg.get("display_col", 2))
    name_col = int(rcfg.get("name_col", 1))
    p = prefix.strip().lower()
    out: list[str] = []
    seen: set[str] = set()
    for row in values[1:]:
        name = _cell(row, disp_col) or _cell(row, name_col)
        if not name or name.lower() in seen:
            continue
        if p and p not in name.lower():
            continue
        seen.add(name.lower())
        out.append(name)
        if len(out) >= limit:
            break
    return out


def _target_from_discord(member) -> Target:
    """Build a Target from the caller's live Discord identity — so `/my_stats`
    works even when no roster exists (free alliances)."""
    joined = member.joined_at.strftime("%Y-%m-%d") if getattr(member, "joined_at", None) else ""
    name = getattr(member, "display_name", None) or getattr(member, "name", "You")
    return Target(name=name, discord_id=member.id, joined=joined)


def _tracked_data_names(guild_id: int) -> list[str]:
    """Union of member names that actually appear in the tracked-data sheets
    (growth + train history). The leadership-picker fallback when there's no
    roster — everyone listed has something to show, and the names match the
    sheets so lookups always resolve."""
    import config

    names: set[str] = set()
    try:
        gcfg = config.get_growth_config(guild_id)
        if gcfg.get("enabled") and gcfg.get("tab_growth"):
            import growth

            vals = growth._get_spreadsheet(guild_id).worksheet(gcfg["tab_growth"]).get_all_values()
            for r in vals[1:]:
                if r and r[0].strip():
                    names.add(r[0].strip())
    except Exception as e:
        logger.warning("[MEMBERSTATS] tracked-names growth read failed guild=%s: %s", guild_id, e)
    try:
        tcfg = config.get_train_config(guild_id)
        import train_rotation as tr

        for row in tr.load_history(guild_id, tcfg.get("history_tab") or "Train History"):
            if row.member and row.member.strip():
                names.add(row.member.strip())
    except Exception as e:
        logger.warning("[MEMBERSTATS] tracked-names train read failed guild=%s: %s", guild_id, e)
    return sorted(names, key=str.lower)


def _leadership_member_list(guild_id: int) -> tuple[list[str], bool]:
    """Return (names, has_roster) for the leadership picker. Prefer the roster
    (canonical, matches the sheets); fall back to tracked-data names with
    has_roster=False so the hub can surface the "set up a roster" notice."""
    import config

    rcfg = config.get_member_roster_config(guild_id)
    if rcfg.get("enabled"):
        roster = _roster_names(guild_id, limit=10_000)
        if roster:
            return roster, True
    return _tracked_data_names(guild_id), False


def _resolve_for_leadership(guild_id: int, name: str) -> Target:
    """Resolve a picked name to a Target — via the roster (richer identity)
    when present, else a name-only Target (data sheets still match by name)."""
    t = _resolve_named(guild_id, name)
    return t or Target(name=name, discord_id=None, joined="")


# ── Section fetchers ─────────────────────────────────────────────────────────
# Each returns the embed field value (str) or None to hide the section.


def _identity_field(guild_id: int, target: Target) -> str:
    handle = f"<@{target.discord_id}>" if target.discord_id else f"**{target.name}**"
    parts = [handle]
    if target.joined:
        parts.append(f"Joined {target.joined}")
    bday = _birthday_for(guild_id, target.name)
    if bday:
        parts.append(f"🎂 {bday}")
    if target.is_manual:
        parts.append(_MANUAL_MEMBER_NOTE)
    return " • ".join(parts)


def _birthday_for(guild_id: int, name: str) -> Optional[str]:
    import config

    bcfg = config.get_birthday_config(guild_id)
    if not bcfg.get("enabled"):
        return None
    try:
        from train import load_birthdays

        members = load_birthdays(bcfg.get("tab_name", "Birthdays"), guild_id)
    except Exception as e:
        logger.warning("[MEMBERSTATS] birthday read failed guild=%s: %s", guild_id, e)
        return None
    n = name.strip().lower()
    for m in members:
        if (m.get("name") or "").strip().lower() == n:
            month, day = m.get("month"), m.get("day")
            if month and day:
                import calendar

                return f"{calendar.month_name[month]} {day}"
    return None


def _power_field(guild_id: int, target: Target) -> Optional[str]:
    """Read the existing Growth Tracking tab — each snapshot appends
    `{metric} ({Mon YYYY})` columns, so a member's row already holds their
    per-period history. Show each tracked metric's latest value and its
    change since the previous snapshot."""
    import config

    gcfg = config.get_growth_config(guild_id)
    if not gcfg.get("enabled") or not gcfg.get("tab_growth"):
        return None
    metrics = gcfg.get("metrics") or []
    if not metrics:
        return None
    try:
        import growth

        sh = growth._get_spreadsheet(guild_id)
        ws = sh.worksheet(gcfg["tab_growth"])
        values = ws.get_all_values()
    except Exception as e:
        logger.warning("[MEMBERSTATS] growth read failed guild=%s: %s", guild_id, e)
        return None
    if not values:
        return None
    header = values[0]
    n = target.name.strip().lower()
    row = next((r for r in values[1:] if r and r[0].strip().lower() == n), None)
    if row is None:
        return None

    lines: list[str] = []
    for m in metrics:
        label = m.get("label")
        if not label:
            continue
        # Period columns for this metric, in sheet order (oldest -> newest,
        # since each snapshot appends to the end of the header).
        cols = [(i, h) for i, h in enumerate(header) if h.startswith(f"{label} (")]
        if not cols:
            continue
        latest_idx, latest_hdr = cols[-1]
        latest_val = growth._safe_float(row[latest_idx]) if latest_idx < len(row) else 0.0
        period = latest_hdr[latest_hdr.find("(") + 1 : latest_hdr.rfind(")")]
        if len(cols) >= 2:
            prev_idx, prev_hdr = cols[-2]
            prev_period = prev_hdr[prev_hdr.find("(") + 1 : prev_hdr.rfind(")")]
            prev_val = growth._safe_float(row[prev_idx]) if prev_idx < len(row) else 0.0
            delta = latest_val - prev_val
            sign = "+" if delta >= 0 else ""
            lines.append(
                f"**{label}:** {_fmt_num(latest_val)} ({sign}{_fmt_num(delta)} since {prev_period})"
            )
        else:
            lines.append(f"**{label}:** {_fmt_num(latest_val)} (first snapshot {period})")
    if not lines:
        return None
    return "\n".join(lines)


def _train_field(guild_id: int, target: Target, *, leadership_view: bool) -> Optional[str]:
    import config

    tcfg = config.get_train_config(guild_id)
    history_tab = tcfg.get("history_tab") or "Train History"
    try:
        import train_rotation as tr

        history = tr.load_history(guild_id, history_tab)
    except Exception as e:
        logger.warning("[MEMBERSTATS] train history read failed guild=%s: %s", guild_id, e)
        return None
    if not history:
        return None
    import train_rotation as tr

    counted = tr.parse_counted_reasons(tcfg.get("counted_reasons"))
    counts = tr.rotation_counts(history, counted)
    last = tr.last_driven_dates(history)
    n = target.name.strip().lower()
    count = next((c for k, c in counts.items() if k.strip().lower() == n), 0)
    last_drove = next((d for k, d in last.items() if k.strip().lower() == n), None)

    line = f"Conductor: **{count}** time{'s' if count != 1 else ''}"
    if last_drove:
        line += f" · last drove {_fmt_date(last_drove)}"
    out = [line]

    if leadership_view:
        from collections import Counter

        reasons = Counter(
            r.reason for r in history if (r.member or "").strip().lower() == n and r.reason
        )
        if reasons:
            breakdown = ", ".join(f"{reason} {c}" for reason, c in reasons.most_common())
            out.append(f"_Reason breakdown:_ {breakdown}")
    return "\n".join(out)


def _storm_truthy(v: str) -> bool:
    return bool(v) and str(v).strip().lower() not in ("no", "false", "0")


# How far back to scan for storm sign-up events (weekly cadence, so ~180 days
# covers a couple of seasons of history without an unbounded query).
_STORM_SIGNUP_LOOKBACK_DAYS = 180
# Sign-up votes that count as "available" (vs "cannot"). See storm_signup_view.
_AVAILABLE_VOTES = {"a", "b", "either"}


def _pct(n: int, d: int) -> int:
    return round(n / d * 100) if d else 0


def _fmt_date(iso: str) -> str:
    """`2026-05-29` -> `May 29, 2026`; pass through anything unparseable."""
    from datetime import datetime

    try:
        dt = datetime.strptime((iso or "").strip()[:10], "%Y-%m-%d")
        return f"{dt:%b} {dt.day}, {dt.year}"
    except (ValueError, AttributeError):
        return iso


def _storm_event_dates(guild_id: int, event_type: str) -> list[str]:
    """Recent event dates for (guild, event_type), oldest first, from the
    sign-up registration posts."""
    import config

    posts = config.get_recent_storm_registration_posts(within_days=_STORM_SIGNUP_LOOKBACK_DAYS)
    return sorted(
        {
            p["event_date"]
            for p in posts
            if p.get("guild_id") == guild_id and p.get("event_type") == event_type
        }
    )


def _storm_signups_for_member(guild_id: int, event_type: str, discord_id):
    """(available_count, total_events, last_vote_date) from the sign-up poll
    votes, or None. Keyed by Discord ID, so manual members are skipped.
    `last_vote_date` is the most recent event they cast any vote on."""
    if not discord_id:
        return None
    import config

    try:
        dates = _storm_event_dates(guild_id, event_type)
    except Exception as e:
        logger.warning("[MEMBERSTATS] storm signups read failed guild=%s: %s", guild_id, e)
        return None
    if not dates:
        return None
    available = 0
    last_vote = ""
    for d in dates:  # ascending, so the last hit is the most recent
        vote = config.get_member_vote(guild_id, event_type, d, str(discord_id))
        if vote:
            last_vote = d
            if vote.get("vote") in _AVAILABLE_VOTES:
                available += 1
    return available, len(dates), last_vote


def _storm_attendance_for_member(guild_id: int, event_type: str, name_lower: str):
    """(attended_count, tracked_events, last_attended_date) from the Per-Member
    participation Log, or None. Keyed by member name."""
    from storm_log import ATTENDANCE_QUESTION_KEY, read_member_log_window

    try:
        dates, by_member = read_member_log_window(guild_id, event_type, 50, ATTENDANCE_QUESTION_KEY)
    except Exception as e:
        logger.warning("[MEMBERSTATS] storm log read failed guild=%s: %s", guild_id, e)
        return None
    if not dates:
        return None
    rows = next((v for k, v in by_member.items() if k.strip().lower() == name_lower), None)
    if not rows:
        return None
    attended = 0
    last_attended = ""
    for d, v in rows.items():
        if _storm_truthy(v):
            attended += 1
            if d > last_attended:
                last_attended = d
    return attended, len(rows), last_attended


def read_storm_attendance_map(guild_id: int) -> dict[str, int]:
    """Storm attendance % per member for the roster API (#316), keyed by
    lowercased member name.

    Mirrors what `/member_stats` computes per member (attended / tracked from the
    Per-Member participation Log via `_storm_attendance_for_member`), but in bulk
    — one log read per event type — and summed across both storm types into one
    figure. Members with no tracked events are omitted, so the roster renders a
    null. Degrades per-event-type on read failure rather than raising.
    """
    from storm_log import ATTENDANCE_QUESTION_KEY, read_member_log_window

    totals: dict[str, list[int]] = {}  # name_lower -> [attended, tracked]
    for event_type in ("DS", "CS"):
        try:
            dates, by_member = read_member_log_window(
                guild_id, event_type, 50, ATTENDANCE_QUESTION_KEY
            )
        except Exception as e:
            logger.warning(
                "[MEMBERSTATS] attendance map read failed guild=%s type=%s: %s",
                guild_id,
                event_type,
                e,
            )
            continue
        if not dates:
            continue
        for name, rows in by_member.items():
            nl = name.strip().lower()
            if not nl:
                continue
            attended = sum(1 for v in rows.values() if _storm_truthy(v))
            acc = totals.setdefault(nl, [0, 0])
            acc[0] += attended
            acc[1] += len(rows)
    return {nl: _pct(a, t) for nl, (a, t) in totals.items() if t}


def _storm_placement_for_member(guild_id: int, event_type: str, discord_id):
    """(primary, sub, sat_out, last_sat_out_date) placement across recent events,
    or None. Primary/sub from the saved team plans (by Discord ID). "Sat out"
    is derived as available-but-not-placed (leadership's explicit sit-out sheet
    has no per-member reader). Leadership-only."""
    if not discord_id:
        return None
    import config

    try:
        dates = _storm_event_dates(guild_id, event_type)
    except Exception as e:
        logger.warning("[MEMBERSTATS] storm placement read failed guild=%s: %s", guild_id, e)
        return None
    if not dates:
        return None
    did = str(discord_id)
    primary = sub = sat_out = 0
    last_sat_out = ""
    saw_plan = False
    for d in dates:  # ascending
        plans = config.get_storm_team_plans_for_event(guild_id, event_type, d)
        if not plans:
            continue
        saw_plan = True
        if any(did in p.get("primaries", []) for p in plans.values()):
            primary += 1
        elif any(did in p.get("subs", []) for p in plans.values()):
            sub += 1
        else:
            vote = config.get_member_vote(guild_id, event_type, d, did)
            if vote and vote.get("vote") in _AVAILABLE_VOTES:
                sat_out += 1
                last_sat_out = d
    if not saw_plan:
        return None
    return primary, sub, sat_out, last_sat_out


def _storm_metric_line(stat: str, date_label: str, date_iso: str, *, leadership_view: bool) -> str:
    """One indented metric line: the stat, plus (leadership view) its matching
    recency date on the right."""
    line = f"   {stat}"
    if leadership_view and date_iso:
        line += f"   ·   {date_label} {_fmt_date(date_iso)}"
    return line


def _storm_field(guild_id: int, target: Target, *, leadership_view: bool) -> Optional[str]:
    """Storm participation per event type, one metric per line. Member view:
    sign-up rate + attendance. Leadership view also gets placement counts and,
    on each line, the matching recency date (last vote / attended / sat out)
    to spot disengagement at a glance."""
    n = target.name.strip().lower()
    blocks: list[str] = []
    for event_type, label in (("DS", "Desert Storm"), ("CS", "Canyon Storm")):
        lines: list[str] = []

        signups = _storm_signups_for_member(guild_id, event_type, target.discord_id)
        if signups:
            avail, total, last_vote = signups
            lines.append(
                _storm_metric_line(
                    f"Signed up {avail} of {total} ({_pct(avail, total)}%)",
                    "Last vote",
                    last_vote,
                    leadership_view=leadership_view,
                )
            )

        attendance = _storm_attendance_for_member(guild_id, event_type, n)
        if attendance:
            attended, tracked, last_attended = attendance
            lines.append(
                _storm_metric_line(
                    f"Attended {attended} of {tracked} ({_pct(attended, tracked)}%)",
                    "Last attended",
                    last_attended,
                    leadership_view=leadership_view,
                )
            )

        if leadership_view:
            placement = _storm_placement_for_member(guild_id, event_type, target.discord_id)
            if placement:
                primary, sub, sat_out, last_sat_out = placement
                lines.append(
                    _storm_metric_line(
                        f"Placed: {primary} primary, {sub} sub, {sat_out} sat out",
                        "Last sat out",
                        last_sat_out,
                        leadership_view=leadership_view,
                    )
                )

        if lines:
            blocks.append(f"**{label}:**\n" + "\n".join(lines))
    return "\n".join(blocks) if blocks else None


def _survey_field(guild_id: int, target: Target) -> Optional[str]:
    """Per-survey last-response date. Reads each survey's history tab and finds
    the member's most recent row (by Discord ID, or Username for manual
    members). Only surveys the member has actually answered are listed."""
    import config

    try:
        surveys = config.list_surveys(guild_id)
    except Exception as e:
        logger.warning("[MEMBERSTATS] list_surveys failed guild=%s: %s", guild_id, e)
        return None
    lines: list[str] = []
    for s in surveys:
        last = _last_survey_response(guild_id, s.get("tab_history") or "Survey History", target)
        if last:
            lines.append(f"**{s.get('survey_name') or 'Survey'}:** last response {last}")
    return "\n".join(lines) if lines else None


def _parse_survey_ts(raw: str):
    """Parse the Survey History timestamp (`M/D/YYYY HH:MM UTC`)."""
    from datetime import datetime

    s = (raw or "").strip().replace(" UTC", "")
    for fmt in ("%m/%d/%Y %H:%M", "%m/%d/%Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _last_survey_response(guild_id: int, tab: str, target: Target) -> Optional[str]:
    import config

    try:
        ws = config.get_spreadsheet(guild_id).worksheet(tab)
        values = ws.get_all_values()
    except Exception as e:
        logger.warning("[MEMBERSTATS] survey history read failed guild=%s: %s", guild_id, e)
        return None
    if len(values) < 2:
        return None
    did = str(target.discord_id) if target.discord_id else None
    name = target.name.strip().lower()
    best = None
    for row in values[1:]:  # cols: Timestamp, Discord ID, Username, ...
        rid, uname = _cell(row, 1), _cell(row, 2)
        match = (rid == did) if did else (uname.strip().lower() == name)
        if not match:
            continue
        dt = _parse_survey_ts(_cell(row, 0))
        if dt and (best is None or dt > best):
            best = dt
    if best is None:
        return None
    return f"{best:%b} {best.day}, {best.year}"


def _fmt_num(v: float) -> str:
    """Compact number rendering: 87.3M / 412M / 1.2B / 304,743."""
    av = abs(v)
    if av >= 1_000_000_000:
        return f"{v / 1_000_000_000:.1f}B"
    if av >= 1_000_000:
        return f"{v / 1_000_000:.1f}M"
    if av >= 1_000:
        return f"{v:,.0f}"
    return f"{v:.0f}"


# ── Embed assembly ───────────────────────────────────────────────────────────


# What each trackable section is + where its data comes from, for the "more you
# can track" hints. Order matches the section order above. `train` only hints in
# the leadership view (it's leadership-only).
_SECTION_HINTS = [
    (
        "power",
        False,
        "📊 **Power trends:** monthly growth snapshots, set up via `/setup` → 📈 Growth",
    ),
    (
        "storm",
        False,
        "⚔️ **Storm participation:** sign-ups and attendance from `/desertstorm` / `/canyonstorm`",
    ),
    ("survey", False, "📋 **Surveys:** responses from `/survey`"),
    ("train", True, "🚂 **Train history:** conductor rotation from `/train`"),
]


def _missing_hints(shown: set[str], *, leadership_view: bool) -> Optional[str]:
    """A bullet per trackable section the member has no data for yet, so even a
    partly-filled card nudges toward what else the alliance could track."""
    lines = [
        text
        for key, leadership_only, text in _SECTION_HINTS
        if key not in shown and (leadership_view or not leadership_only)
    ]
    return "\n".join(lines) if lines else None


def build_embed(guild_id: int, target: Target, *, leadership_view: bool) -> discord.Embed:
    embed = discord.Embed(
        title=f"👤 {target.name}'s Member Stats",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Identity", value=_identity_field(guild_id, target), inline=False)

    shown: set[str] = set()

    power = _power_field(guild_id, target)
    if power:
        embed.add_field(name="📊 Power & Growth", value=power, inline=False)
        shown.add("power")

    storm = _storm_field(guild_id, target, leadership_view=leadership_view)
    if storm:
        embed.add_field(name="⚔️ Storm Participation", value=storm, inline=False)
        shown.add("storm")

    # Train is LEADERSHIP-ONLY: conductor frequency is a leadership allocation,
    # and a member seeing "0 trains" invites a hurt-feelings conversation (#56).
    if leadership_view:
        train = _train_field(guild_id, target, leadership_view=True)
        if train:
            embed.add_field(name="🚂 Train", value=train, inline=False)
            shown.add("train")

    survey = _survey_field(guild_id, target)
    if survey:
        embed.add_field(name="📋 Surveys", value=survey, inline=False)
        shown.add("survey")

    hints = _missing_hints(shown, leadership_view=leadership_view)
    if hints:
        embed.add_field(name="💡 More you can track", value=hints, inline=False)
    return embed


def _build_leadership_embed(guild_id: int, name: str) -> discord.Embed:
    """Resolve a picked name and build its leadership embed — one call so it
    runs in a single off-thread hop from the picker."""
    return build_embed(guild_id, _resolve_for_leadership(guild_id, name), leadership_view=True)


def build_power_embed(guild_id: int, target: Target) -> discord.Embed:
    """Power-only embed for the public Share button — power is the one
    brag-worthy, non-sensitive section, so it's all we broadcast (#56)."""
    embed = discord.Embed(title=f"📊 {target.name}'s Power", color=discord.Color.blurple())
    embed.description = _power_field(guild_id, target) or "No power stats tracked yet."
    return embed


# ── /my_stats Share button ───────────────────────────────────────────────────


class SharePowerView(discord.ui.View):
    """On an ephemeral `/my_stats`, lets the member repost their POWER section
    (only) visibly to the channel."""

    def __init__(self, guild_id: int, target: Target, *, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.target = target

    @discord.ui.button(
        label="📤 Share my power stats to this channel", style=discord.ButtonStyle.secondary
    )
    async def share(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        power = await asyncio.to_thread(_power_field, self.guild_id, self.target)
        if not power:
            await interaction.followup.send(
                "You don't have any power stats to share yet.", ephemeral=True
            )
            return
        button.disabled = True
        await interaction.edit_original_response(view=self)
        # A public message in the channel — a followup to the ephemeral
        # /my_stats interaction would itself be ephemeral, so post to the
        # channel directly.
        embed = discord.Embed(
            title=f"📊 {self.target.name}'s Power",
            description=power,
            color=discord.Color.blurple(),
        )
        await interaction.channel.send(embed=embed)


# ── /member_stats leadership picker hub ──────────────────────────────────────


class MemberPickerView(discord.ui.View):
    """Leadership-only paginated member picker. A dropdown (names are easier to
    scan/recognise than typeahead) of 25 per page, Prev/Next to page through a
    larger alliance. Picking a member renders their full leadership view."""

    PAGE_SIZE = 25

    def __init__(self, guild_id: int, names: list[str], *, no_roster: bool, timeout: float = 600):
        super().__init__(timeout=timeout)
        self.guild_id = guild_id
        self.names = names
        self.no_roster = no_roster
        self.page = 0
        self.message = None
        self._build_controls()

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.names) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def notice(self) -> str:
        parts = ["**Member Stats:** pick a member from the menu to see their full stats."]
        if self.no_roster:
            parts.append(
                "⚠️ No member roster is set up, so this menu only lists members who already have "
                "tracked data. To list your whole alliance, set up the member roster via "
                "`/setup` → 👥 Member Sync (a Premium feature, `/upgrade` to unlock it)."
            )
        if self.total_pages > 1:
            parts.append(f"_Page {self.page + 1} of {self.total_pages}_")
        return "\n\n".join(parts)

    def _build_controls(self):
        self.clear_items()
        start = self.page * self.PAGE_SIZE
        page_names = self.names[start : start + self.PAGE_SIZE]
        select = discord.ui.Select(
            placeholder="Pick a member to view…",
            options=[discord.SelectOption(label=n[:100], value=n[:100]) for n in page_names],
        )
        select.callback = self._on_pick
        self.add_item(select)
        if self.total_pages > 1:
            prev = discord.ui.Button(
                label="◀ Prev", style=discord.ButtonStyle.secondary, disabled=self.page == 0
            )
            prev.callback = self._prev
            self.add_item(prev)
            nxt = discord.ui.Button(
                label="Next ▶",
                style=discord.ButtonStyle.secondary,
                disabled=self.page >= self.total_pages - 1,
            )
            nxt.callback = self._next
            self.add_item(nxt)

    async def _on_pick(self, interaction: discord.Interaction):
        name = interaction.data["values"][0]
        # Building the embed does several Sheets reads (a few seconds), so show
        # an immediate fetching state instead of a frozen-looking menu. Editing
        # the message IS the interaction response (within the 3s window); the
        # reads then run off the event loop and the result edits in after.
        await interaction.response.edit_message(
            content=f"⏳ Fetching **{name}**'s stats…", embed=None, view=None
        )
        embed = await asyncio.to_thread(_build_leadership_embed, self.guild_id, name)
        await interaction.edit_original_response(content=self.notice(), embed=embed, view=self)

    async def _prev(self, interaction: discord.Interaction):
        self.page = max(0, self.page - 1)
        self._build_controls()
        await interaction.response.edit_message(content=self.notice(), view=self)

    async def _next(self, interaction: discord.Interaction):
        self.page = min(self.total_pages - 1, self.page + 1)
        self._build_controls()
        await interaction.response.edit_message(content=self.notice(), view=self)


# ── Command cog ──────────────────────────────────────────────────────────────


class MemberStatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="my_stats", description="See your own alliance stats")
    @app_commands.guild_only()
    async def my_stats(self, interaction: discord.Interaction):
        # Defer first (the embed does several Sheets reads, which can exceed the
        # 3s interaction-token window — #76 pattern), then build off-thread.
        await interaction.response.defer(ephemeral=True)
        gid, user = interaction.guild_id, interaction.user

        def _work():
            target = _resolve_self(gid, user.id) or _target_from_discord(user)
            return target, build_embed(gid, target, leadership_view=False)

        target, embed = await asyncio.to_thread(_work)
        view = SharePowerView(gid, target)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="member_stats", description="Leadership: look up any member's full stats"
    )
    @app_commands.guild_only()
    async def member_stats(self, interaction: discord.Interaction):
        import setup_cog

        if not setup_cog._has_leadership_or_admin(interaction):
            await interaction.response.send_message(
                "This is a leadership tool. To see your own stats, use `/my_stats`.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        names, has_roster = await asyncio.to_thread(_leadership_member_list, interaction.guild_id)
        if not names:
            await interaction.followup.send(
                "No members with tracked data yet. Once your alliance uses growth, storm, or train "
                "features (or sets up the member roster), members will show up here.",
                ephemeral=True,
            )
            return
        view = MemberPickerView(interaction.guild_id, names, no_roster=not has_roster)
        view.message = await interaction.followup.send(view.notice(), view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(MemberStatsCog(bot))
