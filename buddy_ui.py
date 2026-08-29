"""buddy_ui.py — Discord surfaces for the Profession Buddy System (#289).

Three things live here:

* ``build_buddy_list_embed`` — the shareable list (War Leader ↔ Engineer(s) plus
  an Unpaired section), rendered to match the alliance's member-centric sheet.
* ``BuddyProfessionView`` — the restart-surviving persistent message whose
  buttons let a member set/swap their profession in one click (Premium), plus
  ``register_persistent_buddy_views`` to re-attach it on startup.
* ``BuddyManageView`` — the leadership manual editor (Unpair / Pair / Re-pair).
  Re-pair swaps two War Leaders' Engineers as well as taking a free one, so an
  alliance with no spare Engineers can still change a pairing (#289 F-04).
* ``describe_dropped`` — the one place a cleared pairing is explained, shared by
  refresh, auto-assign, undo and preset loading (#289 F-06).
* ``BuddySession`` — an officer's sitting, holding the single-step undo in
  memory only (#289 F-03).

All Sheet I/O is in ``buddy`` and is driven off the event loop via
``asyncio.to_thread`` so a slow gspread call can't stall the bot.
"""

import asyncio
import logging
from typing import Optional

import discord

import buddy

logger = logging.getLogger(__name__)

BUDDY_LIST_TITLE = "🤝 Profession Buddy List"
BUDDY_CMD = "/buddy"

_DENY_NOT_OWNER = "⛔ Only the person who opened this can use these buttons."

# Persistent profession button codes.
_CODE_WL = "wl"
_CODE_ENG = "eng"
_CODE_WHOAMI = "whoami"
_VALID_CODES = (_CODE_WL, _CODE_ENG, _CODE_WHOAMI)


# ── custom_id ─────────────────────────────────────────────────────────────────


def make_buddy_custom_id(guild_id: int, code: str) -> str:
    """Stable encoding for a BuddyProfessionView button."""
    return f"buddy:{int(guild_id)}:{code}"


def parse_buddy_custom_id(custom_id: str) -> Optional[dict]:
    """Inverse of make_buddy_custom_id. None on malformed input."""
    parts = (custom_id or "").split(":")
    if len(parts) != 3 or parts[0] != "buddy":
        return None
    try:
        guild_id = int(parts[1])
    except ValueError:
        return None
    code = parts[2]
    if code not in _VALID_CODES:
        return None
    return {"guild_id": guild_id, "code": code}


# ── shared helpers ────────────────────────────────────────────────────────────


def _wl_priority(cfg: dict) -> str:
    return "power" if (cfg.get("scarcity_priority") == "strongest_first") else "name"


def _eng_priority(cfg: dict) -> str:
    return "reliability" if cfg.get("reliability_enabled") else "name"


def _load_members(guild_id: int, cfg: dict, *, use_buddy_tab: bool = True) -> list:
    """Read professions (plus power when strongest_first, reliability when the
    reliability ranking is on) — sync, for to_thread.

    Squad Powers is authoritative; professions implied by the existing buddy
    tab (left = War Leader, middle/right = Engineer) fill in members who
    haven't been surveyed yet, so an alliance can bootstrap from an existing
    buddy list.

    ``use_buddy_tab=False`` drops that fallback and builds the pool from Squad
    Powers alone. That's what makes a from-scratch rebuild able to shed a
    departed member: their leftover Buddies-tab row is what would otherwise
    carry them back in (#427)."""
    members = buddy.read_all_professions(
        guild_id,
        cfg.get("profession_tab"),
        cfg.get("profession_col_header"),
        cfg.get("include_col_header") or "",
    )
    fallback = (
        buddy.read_members_from_buddy_tab(guild_id, cfg.get("buddy_tab")) if use_buddy_tab else []
    )
    roster = buddy.read_roster_index(guild_id) if cfg.get("roster_filter_enabled") else None
    members = buddy.eligible_members(members, fallback, roster)
    if _wl_priority(cfg) == "power":
        buddy.read_power_for_members(guild_id, members)
    if _eng_priority(cfg) == "reliability":
        buddy.read_reliability_for_members(guild_id, members)
    return members


def compute_current(guild_id: int, cfg: dict):
    """The current saved pairing (no auto-fill) — what's on the sheet now."""
    members = _load_members(guild_id, cfg)
    pairs = buddy.load_pairs(guild_id, cfg.get("buddy_tab"))
    return buddy.assign_buddies(
        members,
        pairs,
        engineer_doubling=bool(cfg.get("engineer_doubling")),
        wl_priority=_wl_priority(cfg),
        eng_priority=_eng_priority(cfg),
        fill=False,
    )


def snapshot_pairs(guild_id: int, cfg: dict) -> list:
    """The pair list exactly as the tab holds it — sync, for to_thread.

    Taken before an action that writes, so Undo has somewhere to go back to
    (#289 F-03). One extra Sheets *read*, which is a separate and far more
    generous quota than the writes this feature is careful about."""
    return buddy.load_pairs(guild_id, cfg.get("buddy_tab"))


def compute_autofill(guild_id: int, cfg: dict, *, from_scratch: bool = False):
    """Run the stability-first auto-assignment and return the result.

    ``from_scratch`` discards existing pairings *and* rebuilds the member pool
    from Squad Powers alone, so anyone the alliance has taken off Squad Powers
    (or marked in the opt-out column) leaves the list instead of being re-read
    off their old Buddies-tab row."""
    members = _load_members(guild_id, cfg, use_buddy_tab=not from_scratch)
    existing = [] if from_scratch else buddy.load_pairs(guild_id, cfg.get("buddy_tab"))
    return buddy.assign_buddies(
        members,
        existing,
        engineer_doubling=bool(cfg.get("engineer_doubling")),
        wl_priority=_wl_priority(cfg),
        eng_priority=_eng_priority(cfg),
        fill=True,
    )


def preview_scratch_rebuild(guild_id: int, cfg: dict):
    """``(result, dropped_names)`` for a from-scratch rebuild — sync, for to_thread.

    ``dropped_names`` are people currently on the Buddies tab who wouldn't
    survive the rebuild, because Squad Powers doesn't classify them or the
    opt-out column excludes them. Computed before the confirmation so leadership
    is told who disappears rather than finding out afterwards.

    This matters most for an alliance that bootstrapped from an existing buddy
    list and never ran the survey: for them the rebuild is destructive, and the
    named list is the warning."""
    result = compute_autofill(guild_id, cfg, from_scratch=True)
    current = buddy.read_members_from_buddy_tab(guild_id, cfg.get("buddy_tab"))
    return result, buddy.names_dropped_by(result, current)


def roster_warning(guild_id: int, cfg: dict) -> str:
    """One line naming members the roster intersect is dropping, or "" — sync,
    for to_thread.

    Only meaningful when the roster filter is on. Leadership sees this after a
    buddy action so a matching problem (a renamed member, a typo'd roster tab)
    reads as "check the roster" instead of "the bot lost people" (#428)."""
    if not cfg.get("roster_filter_enabled"):
        return ""
    roster = buddy.read_roster_index(guild_id)
    if not roster:
        # The empty-roster guard already left the pool unfiltered; say so,
        # because otherwise nothing signals that the filter isn't working.
        return (
            "⚠️ Couldn't read your member roster, so nobody was filtered out by it. "
            "Check the roster tab in `/setup` → 🤝 Buddy System."
        )
    missing = buddy.members_missing_from_roster(
        buddy.read_all_professions(
            guild_id,
            cfg.get("profession_tab"),
            cfg.get("profession_col_header"),
            cfg.get("include_col_header") or "",
        ),
        roster,
    )
    if not missing:
        return ""
    shown = ", ".join(missing[:5]) + (f" and {len(missing) - 5} more" if len(missing) > 5 else "")
    return (
        f"ℹ️ {len(missing)} on **{cfg.get('profession_tab') or 'Squad Powers'}** "
        f"{'are' if len(missing) > 1 else 'is'} not on your member roster, "
        f"so they were left out: {shown}."
    )


def save_result(guild_id: int, cfg: dict, result) -> bool:
    return buddy.save_pairs(
        guild_id,
        cfg.get("buddy_tab"),
        result,
        cfg.get("profession_tab"),
        cfg.get("profession_col_header"),
    )


def apply_pairs(guild_id: int, cfg: dict, pairs: list):
    """Validate an explicit pair list against the live pool, save it, return the
    result — sync, for to_thread.

    No gap-filling: what you hand in is what you get, minus anything that no
    longer holds. Shared by the manual editor and by undo, so a restored
    snapshot is validated exactly the way a hand edit is."""
    members = _load_members(guild_id, cfg)
    result = buddy.assign_buddies(
        members,
        pairs,
        engineer_doubling=bool(cfg.get("engineer_doubling")),
        wl_priority=_wl_priority(cfg),
        eng_priority=_eng_priority(cfg),
        fill=False,
    )
    save_result(guild_id, cfg, result)
    return result


# ── what changed (#289 F-06) ──────────────────────────────────────────────────
#
# Every buddy action that writes used to report "invalid pairs were cleared" as
# a fixed sentence, naming nobody and explaining nothing. That single line is
# why an alliance couldn't tell a departed member from a misconfigured roster
# tab. One renderer, used by refresh, auto-assign, undo and preset loading, so
# a dropped pairing reads the same wherever it surfaces.

# Enough names to see what happened without pushing the message past Discord's
# limit; the count in the heading carries the rest.
_MAX_NAMED_DROPS = 10

_DROP_TEMPLATES = {
    buddy.DROP_MISSING_WL: "**{wl}** isn't on your list any more, so their pairing with {eng} cleared.",
    buddy.DROP_MISSING_ENG: "**{eng}** isn't on your list any more, so their pairing with {wl} cleared.",
    buddy.DROP_PROFESSION_WL: "**{wl}** isn't a War Leader any more, so their pairing with {eng} cleared.",
    buddy.DROP_PROFESSION_ENG: "**{eng}** isn't an Engineer any more, so their pairing with {wl} cleared.",
    buddy.DROP_ENGINEER_TAKEN: "**{eng}** was listed with two War Leaders, so the pairing with {wl} cleared.",
    buddy.DROP_DOUBLING_OFF: (
        "**{wl}** already has an Engineer, so {eng} was unpaired. Turn on "
        "**Two Engineers per War Leader** in setup to keep both."
    ),
    buddy.DROP_WL_FULL: "**{wl}** already has two Engineers, so {eng} was unpaired.",
    buddy.DROP_SELF: "**{wl}** was paired with themselves, so that row cleared.",
}


def describe_dropped(result) -> str:
    """One block naming every pairing a result refused to keep, and why.

    Empty string when nothing was dropped, so callers can append it
    unconditionally. Deduplicated — a doubled Engineer can otherwise produce
    the same sentence from both of their rows."""
    dropped = list(getattr(result, "dropped", None) or [])
    if not dropped:
        return ""
    lines: list[str] = []
    seen: set[tuple] = set()
    for d in dropped:
        key = (buddy._norm(d.war_leader), buddy._norm(d.engineer), d.reason)
        if key in seen:
            continue
        seen.add(key)
        template = _DROP_TEMPLATES.get(d.reason)
        if template:
            lines.append("• " + template.format(wl=d.war_leader or "?", eng=d.engineer or "?"))
    if not lines:
        return ""
    shown = lines[:_MAX_NAMED_DROPS]
    extra = len(lines) - len(shown)
    if extra > 0:
        shown.append(f"• …and {extra} more.")
    count = len(lines)
    heading = f"⚠️ **{count} pairing{'s' if count != 1 else ''} cleared:**"
    return heading + "\n" + "\n".join(shown)


# ── undo (#289 F-03) ──────────────────────────────────────────────────────────


class BuddySession:
    """One officer's sitting with `/buddy`, holding the single-step undo.

    Kept in memory on the view and nowhere else, deliberately: the pair list is
    member names and Discord IDs, and this is working state rather than
    something the alliance asked us to keep. It lives until the view times out
    (or the bot restarts, which ends the sitting anyway) and is never written to
    the database.

    One step, not a history — enough to take back the button you just pressed,
    which is the case that actually bites. Anything further back is what a
    saved preset is for."""

    __slots__ = ("_snapshot", "_label")

    def __init__(self):
        self._snapshot: Optional[list] = None
        self._label: str = ""

    def capture(self, pairs: list, label: str) -> None:
        """Remember the pair list as it stands, before an action replaces it."""
        self._snapshot = [
            buddy.Pair(p.war_leader, p.wl_discord_id, p.engineer, p.eng_discord_id, p.source)
            for p in (pairs or [])
        ]
        self._label = label

    @property
    def can_undo(self) -> bool:
        return self._snapshot is not None

    @property
    def label(self) -> str:
        return self._label

    def take(self) -> Optional[list]:
        """Hand back the snapshot and clear it — undo is a single step, so the
        button stops offering itself once it has been used."""
        snap, self._snapshot = self._snapshot, None
        self._label = ""
        return snap

    def clear(self) -> None:
        self._snapshot = None
        self._label = ""


def buddies_of(result, discord_id: str, name: str):
    """Return ``(role, [buddy_names])`` for a member in a result.

    ``role`` is "wl" / "eng" / None. Matches by Discord ID first, then name."""
    did = (discord_id or "").strip()
    nm = buddy._norm(name)
    role = None
    out = []
    for p in result.pairs:
        if (did and (p.wl_discord_id or "").strip() == did) or (
            nm and buddy._norm(p.war_leader) == nm
        ):
            role = "wl"
            out.append(p.engineer)
        elif (did and (p.eng_discord_id or "").strip() == did) or (
            nm and buddy._norm(p.engineer) == nm
        ):
            role = "eng"
            out.append(p.war_leader)
    return role, out


def _is_unpaired(result, discord_id: str, name: str) -> Optional[str]:
    """Return "wl"/"eng" if the member is in an unpaired pool, else None."""
    did = (discord_id or "").strip()
    nm = buddy._norm(name)

    def hit(m):
        return (did and (m.discord_id or "").strip() == did) or (nm and buddy._norm(m.name) == nm)

    if any(hit(m) for m in result.unpaired_wl):
        return "wl"
    if any(hit(m) for m in result.unpaired_eng):
        return "eng"
    return None


# ── list embed ────────────────────────────────────────────────────────────────


def _group_pairs(result) -> list:
    """[(wl_name, [eng_name, ...]), ...] grouped by War Leader, name-sorted."""
    order = []
    info = {}
    engs = {}
    for p in result.pairs:
        k = (p.wl_discord_id or "").strip() or buddy._norm(p.war_leader)
        if k not in engs:
            order.append(k)
            engs[k] = []
            info[k] = p.war_leader
        engs[k].append(p.engineer)
    order.sort(key=lambda k: buddy._norm(info[k]))
    return [(info[k], engs[k]) for k in order]


_FIELD_CHAR_CAP = 1000  # Discord field value cap is 1024; leave headroom.


def build_buddy_list_embed(result, *, doubling: bool = False) -> discord.Embed:
    """The shareable buddy list.

    Pairs render as **two side-by-side inline embed fields** (War Leader |
    Engineer). Discord aligns the field columns for us, so row *i* of each
    column lines up regardless of glyph width — including CJK names (준, 콜),
    which a space-padded monospace table can't align pixel-perfectly. Unpaired
    members follow as full-width fields. ``doubling`` is accepted for call-site
    stability."""
    embed = discord.Embed(title=BUDDY_LIST_TITLE, color=discord.Color.blurple())

    grouped = _group_pairs(result)
    if grouped:
        wl_lines: list[str] = []
        eng_lines: list[str] = []
        wl_len = eng_len = 0
        dropped = 0
        # Truncate by whole rows (not characters) so the two columns keep the
        # same line count and stay aligned even on a very large roster.
        for i, (wl, eng_list) in enumerate(grouped):
            eng = ", ".join(eng_list) or "—"
            if wl_len + len(wl) + 1 > _FIELD_CHAR_CAP or eng_len + len(eng) + 1 > _FIELD_CHAR_CAP:
                dropped = len(grouped) - i
                break
            wl_lines.append(wl)
            eng_lines.append(eng)
            wl_len += len(wl) + 1
            eng_len += len(eng) + 1
        embed.add_field(name="🎖️ War Leader", value="\n".join(wl_lines) or "—", inline=True)
        embed.add_field(name="🔧 Engineer", value="\n".join(eng_lines) or "—", inline=True)
        if dropped:
            embed.add_field(
                name="​",
                value=f"…and {dropped} more pairing(s) — see your buddy sheet tab.",
                inline=False,
            )
    else:
        embed.description = "*No buddy pairings yet.*"

    if result.unpaired_wl:
        names = ", ".join(m.name for m in result.unpaired_wl)
        embed.add_field(name="🎖️ War Leaders without a buddy", value=names[:1024], inline=False)
    if result.unpaired_eng:
        names = ", ".join(m.name for m in result.unpaired_eng)
        embed.add_field(name="🔧 Engineers without a buddy", value=names[:1024], inline=False)

    return embed


def describe_my_buddy(result, discord_id: str, name: str) -> str:
    """One-line answer for the member-facing 'Who's my buddy?' lookup."""
    role, buds = buddies_of(result, discord_id, name)
    if role == "wl" and buds:
        if len(buds) >= 2:
            return f"🎖️ You're a **War Leader**. Your Engineers are **{buddy._join_and(buds)}**."
        return f"🎖️ You're a **War Leader**. Your buddy is **{buds[0]}**."
    if role == "eng" and buds:
        return f"🔧 You're an **Engineer**. Your buddy is **{buds[0]}**."
    unp = _is_unpaired(result, discord_id, name)
    if unp == "wl":
        return "🎖️ You're a **War Leader** without a buddy yet. Leadership will pair you up soon."
    if unp == "eng":
        return "🔧 You're an **Engineer** without a buddy yet. Leadership will pair you up soon."
    return (
        "I couldn't find you in the buddy list yet. Set your profession with the "
        "buttons (or ask leadership), and you'll be paired up."
    )


# ── persistent profession view ────────────────────────────────────────────────


class BuddyProfessionView(discord.ui.View):
    """Persistent message: live buddy list + one-click profession buttons.

    ``timeout=None`` with stable custom_ids so the bot re-registers it on
    startup via ``bot.add_view``."""

    def __init__(self, guild_id: int):
        super().__init__(timeout=None)
        self.guild_id = int(guild_id)
        self._add(_CODE_WL, "🎖️ I'm a War Leader", discord.ButtonStyle.success)
        self._add(_CODE_ENG, "🔧 I'm an Engineer", discord.ButtonStyle.success)
        self._add(_CODE_WHOAMI, "🔍 Who's my buddy?", discord.ButtonStyle.secondary)

    def _add(self, code: str, label: str, style: discord.ButtonStyle):
        btn = discord.ui.Button(
            label=label[:80], style=style, custom_id=make_buddy_custom_id(self.guild_id, code)
        )
        btn.callback = self._make_cb(code)
        self.add_item(btn)

    def _make_cb(self, code: str):
        async def _cb(interaction: discord.Interaction):
            await _handle_profession_click(interaction, code)

        return _cb


def _apply_profession_change(
    guild_id: int, cfg: dict, actor_id: str, actor_name: str, new_prof: str
):
    """Sync: write the profession cell, re-pair, save, and build the diff note.

    Returns a dict with ok / before / after / notification / role / buddies."""
    ptab = cfg.get("profession_tab")
    phdr = cfg.get("profession_col_header")
    btab = cfg.get("buddy_tab")
    dbl = bool(cfg.get("engineer_doubling"))
    prio = _wl_priority(cfg)
    eprio = _eng_priority(cfg)

    members_before = _load_members(guild_id, cfg)
    pairs = buddy.load_pairs(guild_id, btab)
    before = buddy.assign_buddies(
        members_before,
        pairs,
        engineer_doubling=dbl,
        wl_priority=prio,
        eng_priority=eprio,
        fill=False,
    )

    if not buddy.write_profession_cell(guild_id, ptab, phdr, actor_id, actor_name, new_prof):
        return {"ok": False}

    members_after = _load_members(guild_id, cfg)
    after = buddy.assign_buddies(
        members_after,
        pairs,
        engineer_doubling=dbl,
        wl_priority=prio,
        eng_priority=eprio,
        fill=True,
        # Place the member who pressed the button and nobody else. This used to
        # fill every gap in the alliance off one person's tap, handing buddies
        # to people who never asked for one (#289 F-08).
        fill_only={str(actor_id).strip() or buddy._norm(actor_name)},
    )
    save_result(guild_id, cfg, after)

    actor_member = next(
        (m for m in members_after if (m.discord_id or "").strip() == str(actor_id).strip()), None
    )
    actor_label = actor_member.name if actor_member else actor_name
    notification = buddy.compose_change_notification(actor_label, new_prof, before, after)
    role, buds = buddies_of(after, str(actor_id), actor_label)
    return {
        "ok": True,
        "before": before,
        "after": after,
        "notification": notification,
        "role": role,
        "buddies": buds,
    }


async def _handle_profession_click(interaction: discord.Interaction, code: str):
    """Click handler for the persistent profession buttons. Defers, re-checks
    Premium, writes the single profession cell, re-pairs, notifies leadership,
    refreshes the list message, and acks the member."""
    import config
    import premium

    parsed = parse_buddy_custom_id(interaction.data.get("custom_id", ""))
    if not parsed:
        try:
            await interaction.response.send_message(
                "⚠️ This button is from an older version. Ask leadership to re-post it.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        return

    guild_id = parsed["guild_id"]
    if interaction.guild_id != guild_id:
        try:
            await interaction.response.send_message(
                "⚠️ This message belongs to a different server.", ephemeral=True
            )
        except discord.HTTPException:
            pass
        return

    try:
        await interaction.response.defer(ephemeral=True, thinking=True)
    except discord.HTTPException:
        pass

    cfg = config.get_buddy_config(guild_id)

    # "Who's my buddy?" works for everyone (free-tier lookup).
    if code == _CODE_WHOAMI:
        result = await asyncio.to_thread(compute_current, guild_id, cfg)
        await interaction.followup.send(
            describe_my_buddy(result, str(interaction.user.id), interaction.user.display_name),
            ephemeral=True,
        )
        return

    # Setting/swapping a profession is the Premium self-service feature.
    if not await premium.is_premium(guild_id, bot=interaction.client):
        await interaction.followup.send(
            "⚠️ One-click profession swapping is a Premium feature and isn't active "
            "for this server right now. Ask leadership to update your profession.",
            ephemeral=True,
        )
        return

    new_prof = buddy.WAR_LEADER if code == _CODE_WL else buddy.ENGINEER
    data = await asyncio.to_thread(
        _apply_profession_change,
        guild_id,
        cfg,
        str(interaction.user.id),
        interaction.user.display_name,
        new_prof,
    )
    if not data.get("ok"):
        await interaction.followup.send(
            "⚠️ I couldn't update your profession in the sheet. Please try again, "
            "or let leadership know.",
            ephemeral=True,
        )
        return

    after = data["after"]

    # Leadership notification (Premium auto-repair).
    notify_id = cfg.get("notify_channel_id") or 0
    if notify_id and await premium.feature_gate(
        "buddy_auto_repair", guild_id, bot=interaction.client
    ):
        ch = interaction.client.get_channel(int(notify_id))
        if ch is not None:
            try:
                await ch.send(f"🔧 {data['notification']}")
            except discord.Forbidden:
                logger.warning(
                    "[BUDDY] notify channel %s forbidden (guild=%s)", notify_id, guild_id
                )
            except discord.HTTPException:
                pass

    # Refresh the live list message in place.
    await refresh_persistent_message(interaction.client, guild_id, cfg, after)

    # Ack the member.
    buds = data.get("buddies") or []
    if buds:
        partner = buddy._join_and(buds)
        await interaction.followup.send(
            f"✅ You're set as a **{new_prof}**. Your buddy is **{partner}**.", ephemeral=True
        )
    else:
        await interaction.followup.send(
            f"✅ You're set as a **{new_prof}**. You don't have a buddy yet — "
            "leadership has been notified.",
            ephemeral=True,
        )

    # Optional buddy DMs (Premium).
    if cfg.get("dm_enabled") and buds:
        await _send_buddy_dms(interaction.client, guild_id, cfg, data)


def _render_buddy_dm(template: str, *, name: str, buddy: str, buddy_role: str) -> str:
    """Substitute {name} / {buddy} / {buddy_role} into the configured buddy DM
    body. Tolerates missing/unknown placeholders so a typo renders literally
    instead of crashing the DM path (same SafeDict idiom as storm/train DMs)."""

    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    try:
        return template.format_map(
            _SafeDict(name=name or "", buddy=buddy or "", buddy_role=buddy_role or "")
        )
    except Exception:
        return (
            template.replace("{name}", name or "")
            .replace("{buddy}", buddy or "")
            .replace("{buddy_role}", buddy_role or "")
        )


async def _send_buddy_dms(bot, guild_id: int, cfg: dict, data: dict) -> None:
    import dm
    from defaults import DEFAULT_BUDDY_DM

    before = data.get("before")
    after = data["after"]
    buds = data.get("buddies") or []
    template = (cfg.get("dm_template") or "").strip() or DEFAULT_BUDDY_DM

    # Best-effort: DM both members of any pair that involves the actor's new buddy.
    affected = [p for p in after.pairs if p.engineer in buds or p.war_leader in buds]

    # Collapse to one DM per recipient: an Engineer paired with two War Leaders
    # gets a single DM naming both buddies, not one DM per pairing.
    recipients: dict[str, dict] = {}
    for p in affected:
        if p.wl_discord_id:
            r = recipients.setdefault(
                p.wl_discord_id,
                {"name": p.war_leader, "buddy_role": buddy.ENGINEER, "buddies": []},
            )
            r["buddies"].append(p.engineer)
        if p.eng_discord_id:
            r = recipients.setdefault(
                p.eng_discord_id,
                {"name": p.engineer, "buddy_role": buddy.WAR_LEADER, "buddies": []},
            )
            r["buddies"].append(p.war_leader)

    for rid, info in recipients.items():
        # Stay silent when nothing changed: clicking the self-service button while
        # already paired with the same buddy must not re-send the DM.
        _, prior = buddies_of(before, rid, info["name"]) if before is not None else (None, [])
        if {buddy._norm(b) for b in prior} == {buddy._norm(b) for b in info["buddies"]}:
            continue
        try:
            await dm.send_dm_to_id(
                bot,
                guild_id,
                rid,
                content=_render_buddy_dm(
                    template,
                    name=info["name"],
                    buddy=buddy._join_and(info["buddies"]),
                    buddy_role=info["buddy_role"],
                ),
            )
        except Exception:
            pass


# ── persistent message lifecycle ──────────────────────────────────────────────


async def post_self_service_message(bot, channel, guild_id: int) -> Optional[discord.Message]:
    """Post the persistent list+buttons message, store its id, and register it."""
    import config

    cfg = config.get_buddy_config(guild_id)
    result = await asyncio.to_thread(compute_current, guild_id, cfg)
    embed = build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
    view = BuddyProfessionView(guild_id)
    try:
        msg = await channel.send(embed=embed, view=view)
    except discord.HTTPException as e:
        logger.warning("[BUDDY] failed to post self-service message (guild=%s): %s", guild_id, e)
        return None
    config.update_buddy_config_field(guild_id, "persistent_channel_id", channel.id)
    config.update_buddy_config_field(guild_id, "persistent_message_id", msg.id)
    try:
        bot.add_view(view, message_id=msg.id)
    except Exception:
        pass
    return msg


async def refresh_persistent_message(bot, guild_id: int, cfg: dict, result) -> None:
    """Edit the persistent message's embed to the latest list. No-op if unset."""
    ch_id = cfg.get("persistent_channel_id") or 0
    msg_id = cfg.get("persistent_message_id") or 0
    if not (ch_id and msg_id):
        return
    ch = bot.get_channel(int(ch_id))
    if ch is None:
        return
    try:
        msg = await ch.fetch_message(int(msg_id))
        await msg.edit(
            embed=build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
        )
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        pass


def register_persistent_buddy_views(bot) -> int:
    """Re-attach a BuddyProfessionView for every enabled guild with a posted
    self-service message. Called once after on_ready. Returns the count."""
    import config

    rows = config.get_buddy_enabled_guilds()
    registered = 0
    for row in rows:
        try:
            view = BuddyProfessionView(row["guild_id"])
            bot.add_view(view, message_id=int(row["persistent_message_id"]))
            registered += 1
        except Exception as e:
            logger.warning(
                "[BUDDY] failed to register view for guild=%s message=%s: %s",
                row.get("guild_id"),
                row.get("persistent_message_id"),
                e,
            )
    if registered:
        logger.info("[BUDDY] Re-registered %d buddy view(s) on startup", registered)
    return registered


# ── manual editor (leadership) ────────────────────────────────────────────────


class _PickerView(discord.ui.View):
    """Generic single-select picker → callback(interaction, value).

    Discord caps a Select at 25 options, so when there are more the view
    paginates: a ◀ / ▶ button row swaps the select through pages of 25,
    keeping every option reachable on large rosters."""

    PAGE_SIZE = 25

    def __init__(self, options: list, owner_id: int, on_pick, *, placeholder="Pick one…"):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self._on_pick = on_pick
        self._options = list(options)
        self._placeholder = placeholder
        self.page = 0
        self._sel: Optional[discord.ui.Select] = None
        self._sync()

    def _total_pages(self) -> int:
        return max(1, (len(self._options) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)

    def _sync(self):
        """Rebuild the select (and pager buttons) for the current page."""
        self.clear_items()
        total = self._total_pages()
        self.page = max(0, min(self.page, total - 1))
        start = self.page * self.PAGE_SIZE
        page_opts = self._options[start : start + self.PAGE_SIZE]
        placeholder = self._placeholder
        if total > 1:
            placeholder = f"{self._placeholder} (page {self.page + 1} of {total})"
        sel = discord.ui.Select(placeholder=placeholder, options=page_opts, row=0)
        sel.callback = self._cb
        self._sel = sel
        self.add_item(sel)
        if total > 1:
            self._pager_button("◀", self._on_prev, disabled=(self.page <= 0))
            self._pager_button("▶", self._on_next, disabled=(self.page >= total - 1))

    def _pager_button(self, label, cb, *, disabled):
        btn = discord.ui.Button(
            label=label, style=discord.ButtonStyle.secondary, row=1, disabled=disabled
        )
        btn.callback = cb
        self.add_item(btn)

    async def interaction_check(self, inter):
        if inter.user.id != self.owner_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _on_prev(self, inter: discord.Interaction):
        self.page -= 1
        self._sync()
        await inter.response.edit_message(view=self)

    async def _on_next(self, inter: discord.Interaction):
        self.page += 1
        self._sync()
        await inter.response.edit_message(view=self)

    async def _cb(self, inter: discord.Interaction):
        value = self._sel.values[0]
        self._sel.disabled = True
        await self._on_pick(inter, value)
        self.stop()


def _pair_value(p) -> str:
    """Stable token for a pair option: wl_id|eng_id (falls back to names)."""
    return f"{(p.wl_discord_id or p.war_leader)}|{(p.eng_discord_id or p.engineer)}"


def _member_value(m) -> str:
    return (m.discord_id or "").strip() or m.name


class BuddyManageView(discord.ui.View):
    """Owner-locked manual pairing editor: Unpair / Pair / Re-pair / Refresh."""

    def __init__(self, bot, guild_id: int, owner_id: int, session=None):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_id = owner_id
        # The hub's sitting, so an edit made here can be undone from there
        # (#289 F-03). None when the editor is opened without one.
        self.session = session
        self.message: Optional[discord.Message] = None
        self._add("🔗 Unpair", discord.ButtonStyle.danger, self._unpair)
        self._add("➕ Pair", discord.ButtonStyle.success, self._pair)
        self._add("🔁 Re-pair", discord.ButtonStyle.primary, self._repair)
        self._add("🔄 Refresh", discord.ButtonStyle.secondary, self._rerender)

    def _add(self, label, style, cb):
        btn = discord.ui.Button(label=label, style=style)
        btn.callback = cb
        self.add_item(btn)

    async def interaction_check(self, inter):
        if inter.user.id != self.owner_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        from wizard_registry import expire_view_message

        await expire_view_message(self.message, command_hint=BUDDY_CMD)

    def _cfg(self):
        import config

        return config.get_buddy_config(self.guild_id)

    async def _save_pairs_list(self, cfg, pairs: list, before: Optional[list] = None):
        """Persist an explicit pair list (no auto-fill) and refresh surfaces.

        ``before`` is the list as it stood, handed to the session so the hub's
        Undo can put it back (#289 F-03)."""
        if before is not None and self.session is not None:
            self.session.capture(before, "your last pairing change")
        result = await asyncio.to_thread(apply_pairs, self.guild_id, cfg, pairs)
        await refresh_persistent_message(self.bot, self.guild_id, cfg, result)
        return result

    async def _refresh_editor(self, inter, result, cfg):
        embed = build_buddy_list_embed(result, doubling=bool(cfg.get("engineer_doubling")))
        try:
            if self.message:
                await self.message.edit(embed=embed, view=self)
        except discord.HTTPException:
            pass

    async def _unpair(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = self._cfg()
        pairs = await asyncio.to_thread(buddy.load_pairs, self.guild_id, cfg.get("buddy_tab"))
        if not pairs:
            await inter.followup.send("ℹ️ There are no pairings to unpair.", ephemeral=True)
            return
        opts = [
            discord.SelectOption(label=f"{p.war_leader} ↔ {p.engineer}"[:100], value=_pair_value(p))
            for p in pairs
        ]

        async def _pick(i: discord.Interaction, value: str):
            await i.response.defer(ephemeral=True, thinking=True)
            remaining = [p for p in pairs if _pair_value(p) != value]
            result = await self._save_pairs_list(cfg, remaining, before=pairs)
            await i.followup.send("🔗 Unpaired.", ephemeral=True)
            await self._refresh_editor(i, result, cfg)

        await inter.followup.send(
            "Pick a pairing to break:",
            view=_PickerView(opts, self.owner_id, _pick, placeholder="Pick a pairing…"),
            ephemeral=True,
        )

    async def _pair(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = self._cfg()
        result = await asyncio.to_thread(compute_current, self.guild_id, cfg)
        free_wl = result.unpaired_wl
        free_eng = result.unpaired_eng
        # War Leaders that still have capacity for another Engineer (doubling).
        # Built from the pairs rather than from display names so the War
        # Leader's Discord ID comes along: a doubled pairing made from a name
        # alone was matched by name forever after, and broke the first time
        # that member renamed (#289 F-09).
        doublable = []
        if cfg.get("engineer_doubling"):
            seen: dict[str, list] = {}
            for p in result.pairs:
                key = (p.wl_discord_id or "").strip() or buddy._norm(p.war_leader)
                entry = seen.setdefault(key, [p.war_leader, p.wl_discord_id, 0])
                entry[2] += 1
            doublable = [
                buddy.Member(name=name, discord_id=did, profession=buddy.WAR_LEADER)
                for name, did, count in seen.values()
                if count < 2
            ]
        wl_choices = list(free_wl) + doublable
        if not wl_choices or not free_eng:
            await inter.followup.send(
                "ℹ️ Need at least one free War Leader and one free Engineer to pair.",
                ephemeral=True,
            )
            return
        wl_opts = [
            discord.SelectOption(label=m.name[:100], value=_member_value(m)) for m in wl_choices
        ]

        async def _pick_wl(i: discord.Interaction, wl_value: str):
            eng_opts = [
                discord.SelectOption(label=m.name[:100], value=_member_value(m)) for m in free_eng
            ]

            async def _pick_eng(i2: discord.Interaction, eng_value: str):
                await i2.response.defer(ephemeral=True, thinking=True)
                wl = next((m for m in wl_choices if _member_value(m) == wl_value), None)
                eng = next((m for m in free_eng if _member_value(m) == eng_value), None)
                pairs = await asyncio.to_thread(
                    buddy.load_pairs, self.guild_id, cfg.get("buddy_tab")
                )
                before = list(pairs)
                pairs.append(
                    buddy.Pair(wl.name, wl.discord_id, eng.name, eng.discord_id, source="manual")
                )
                res = await self._save_pairs_list(cfg, pairs, before=before)
                await i2.followup.send(f"➕ Paired **{wl.name}** ↔ **{eng.name}**.", ephemeral=True)
                await self._refresh_editor(i2, res, cfg)

            # Opening the next picker is instant (no I/O), so a plain response is fine.
            await i.response.send_message(
                "Now pick the Engineer:",
                view=_PickerView(
                    eng_opts, self.owner_id, _pick_eng, placeholder="Pick an Engineer…"
                ),
                ephemeral=True,
            )

        await inter.followup.send(
            "Pick the War Leader:",
            view=_PickerView(wl_opts, self.owner_id, _pick_wl, placeholder="Pick a War Leader…"),
            ephemeral=True,
        )

    async def _repair(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = self._cfg()
        pairs = await asyncio.to_thread(buddy.load_pairs, self.guild_id, cfg.get("buddy_tab"))
        result = await asyncio.to_thread(compute_current, self.guild_id, cfg)
        free_eng = result.unpaired_eng
        if not pairs:
            await inter.followup.send("ℹ️ There are no pairings to change.", ephemeral=True)
            return
        opts = [
            discord.SelectOption(label=f"{p.war_leader} ↔ {p.engineer}"[:100], value=_pair_value(p))
            for p in pairs
        ]

        async def _pick_pair(i: discord.Interaction, value: str):
            target = next((p for p in pairs if _pair_value(p) == value), None)
            # Two ways to change a pairing: take a free Engineer, or trade with
            # another War Leader. Only the first used to exist, so an alliance
            # with as many Engineers as War Leaders — where nobody is ever free
            # — could not change a pairing at all (#289 F-04).
            others = [p for p in pairs if _pair_value(p) != value]
            eng_opts = [
                discord.SelectOption(label=m.name[:100], value=f"free|{_member_value(m)}")
                for m in free_eng
            ] + [
                discord.SelectOption(
                    label=f"{p.engineer} — swap with {p.war_leader}"[:100],
                    value=f"swap|{_pair_value(p)}",
                )
                for p in others
            ]
            if not eng_opts:
                await i.response.send_message(
                    "ℹ️ There's nobody to swap in — this is the only pairing.", ephemeral=True
                )
                return

            async def _pick_eng(i2: discord.Interaction, eng_value: str):
                await i2.response.defer(ephemeral=True, thinking=True)
                kind, rest = eng_value.split("|", 1)
                new_pairs = [p for p in pairs if _pair_value(p) != value]
                if kind == "swap":
                    partner = next((p for p in others if _pair_value(p) == rest), None)
                    if partner is None:
                        await i2.followup.send(
                            "⚠️ That pairing changed while you were choosing. "
                            "Hit 🔄 Refresh and try again.",
                            ephemeral=True,
                        )
                        return
                    new_pairs = [p for p in new_pairs if _pair_value(p) != rest]
                    new_pairs.append(
                        buddy.Pair(
                            target.war_leader,
                            target.wl_discord_id,
                            partner.engineer,
                            partner.eng_discord_id,
                            source="manual",
                        )
                    )
                    new_pairs.append(
                        buddy.Pair(
                            partner.war_leader,
                            partner.wl_discord_id,
                            target.engineer,
                            target.eng_discord_id,
                            source="manual",
                        )
                    )
                    note = (
                        f"🔁 Swapped. **{target.war_leader}** now has **{partner.engineer}**, "
                        f"and **{partner.war_leader}** has **{target.engineer}**."
                    )
                else:
                    eng = next((m for m in free_eng if _member_value(m) == rest), None)
                    if eng is None:
                        await i2.followup.send(
                            "⚠️ That Engineer is no longer free. Hit 🔄 Refresh and try again.",
                            ephemeral=True,
                        )
                        return
                    new_pairs.append(
                        buddy.Pair(
                            target.war_leader,
                            target.wl_discord_id,
                            eng.name,
                            eng.discord_id,
                            source="manual",
                        )
                    )
                    note = f"🔁 **{target.war_leader}** is now paired with **{eng.name}**."
                res = await self._save_pairs_list(cfg, new_pairs, before=pairs)
                await i2.followup.send(note, ephemeral=True)
                await self._refresh_editor(i2, res, cfg)

            # Opening the next picker is instant (no I/O), so a plain response is fine.
            await i.response.send_message(
                "Pick the Engineer to swap in:",
                view=_PickerView(
                    eng_opts, self.owner_id, _pick_eng, placeholder="Pick an Engineer…"
                ),
                ephemeral=True,
            )

        await inter.followup.send(
            "Pick the pairing to change:",
            view=_PickerView(opts, self.owner_id, _pick_pair, placeholder="Pick a pairing…"),
            ephemeral=True,
        )

    async def _rerender(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = self._cfg()
        result = await asyncio.to_thread(compute_current, self.guild_id, cfg)
        await self._refresh_editor(inter, result, cfg)
        await inter.followup.send("🔄 Refreshed.", ephemeral=True)
