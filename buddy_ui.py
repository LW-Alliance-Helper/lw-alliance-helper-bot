"""buddy_ui.py — Discord surfaces for the Profession Buddy System (#289).

Three things live here:

* ``build_buddy_list_embed`` — the shareable list (War Leader ↔ Engineer(s) plus
  an Unpaired section), rendered to match the alliance's member-centric sheet.
* ``BuddyProfessionView`` — the restart-surviving persistent message whose
  buttons let a member set/swap their profession in one click (Premium), plus
  ``register_persistent_buddy_views`` to re-attach it on startup.
* ``BuddyManageView`` — the leadership manual editor (Unpair / Pair / Re-pair).

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


def _load_members(guild_id: int, cfg: dict) -> list:
    """Read professions (and power when strongest_first) — sync, for to_thread.

    Squad Powers is authoritative; professions implied by the existing buddy
    tab (left = War Leader, middle/right = Engineer) fill in members who
    haven't been surveyed yet, so an alliance can bootstrap from an existing
    buddy list."""
    members = buddy.read_all_professions(
        guild_id, cfg.get("profession_tab"), cfg.get("profession_col_header")
    )
    fallback = buddy.read_members_from_buddy_tab(guild_id, cfg.get("buddy_tab"))
    members = buddy.merge_members(members, fallback)
    if _wl_priority(cfg) == "power":
        buddy.read_power_for_members(guild_id, members)
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
        fill=False,
    )


def compute_autofill(guild_id: int, cfg: dict, *, from_scratch: bool = False):
    """Run the stability-first auto-assignment and return the result."""
    members = _load_members(guild_id, cfg)
    existing = [] if from_scratch else buddy.load_pairs(guild_id, cfg.get("buddy_tab"))
    return buddy.assign_buddies(
        members,
        existing,
        engineer_doubling=bool(cfg.get("engineer_doubling")),
        wl_priority=_wl_priority(cfg),
        fill=True,
    )


def save_result(guild_id: int, cfg: dict, result) -> bool:
    return buddy.save_pairs(
        guild_id,
        cfg.get("buddy_tab"),
        result,
        cfg.get("profession_tab"),
        cfg.get("profession_col_header"),
    )


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


def build_buddy_list_embed(result, *, doubling: bool = False) -> discord.Embed:
    """The shareable buddy list (mirrors the alliance's sheet layout).

    Rendered as a single markdown block: a ``##`` header, one line per War
    Leader (with their Engineer(s)), then the unpaired members as plain
    label lines. ``doubling`` is accepted for call-site stability; the per-row
    ``(×2)`` tag already signals a doubled War Leader."""
    lines = [f"## {BUDDY_LIST_TITLE}", ""]

    grouped = _group_pairs(result)
    if grouped:
        for wl, eng_list in grouped:
            partners = ", ".join(eng_list)
            tag = "  (×2)" if len(eng_list) >= 2 else ""
            lines.append(f"🎖️ {wl} ↔ 🔧 {partners}{tag}")
    else:
        lines.append("*No buddy pairings yet.*")

    if result.unpaired_wl or result.unpaired_eng:
        lines.append("")
        if result.unpaired_wl:
            names = ", ".join(m.name for m in result.unpaired_wl)
            lines.append(f"🎖️ War Leaders without a buddy: {names}")
        if result.unpaired_eng:
            names = ", ".join(m.name for m in result.unpaired_eng)
            lines.append(f"🔧 Engineers without a buddy: {names}")

    return discord.Embed(description="\n".join(lines)[:4096], color=discord.Color.blurple())


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

    members_before = _load_members(guild_id, cfg)
    pairs = buddy.load_pairs(guild_id, btab)
    before = buddy.assign_buddies(
        members_before, pairs, engineer_doubling=dbl, wl_priority=prio, fill=False
    )

    if not buddy.write_profession_cell(guild_id, ptab, phdr, actor_id, actor_name, new_prof):
        return {"ok": False}

    members_after = _load_members(guild_id, cfg)
    after = buddy.assign_buddies(
        members_after, pairs, engineer_doubling=dbl, wl_priority=prio, fill=True
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

    after = data["after"]
    buds = data.get("buddies") or []
    template = (cfg.get("dm_template") or "").strip() or DEFAULT_BUDDY_DM
    # Best-effort: DM both members of any pair that involves the actor's new buddy.
    affected = [p for p in after.pairs if p.engineer in buds or p.war_leader in buds]
    for p in affected:
        try:
            if p.wl_discord_id:
                await dm.send_dm_to_id(
                    bot,
                    guild_id,
                    p.wl_discord_id,
                    content=_render_buddy_dm(
                        template, name=p.war_leader, buddy=p.engineer, buddy_role=buddy.ENGINEER
                    ),
                )
            if p.eng_discord_id:
                await dm.send_dm_to_id(
                    bot,
                    guild_id,
                    p.eng_discord_id,
                    content=_render_buddy_dm(
                        template, name=p.engineer, buddy=p.war_leader, buddy_role=buddy.WAR_LEADER
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
    """Generic single-select picker → callback(interaction, value)."""

    def __init__(self, options: list, owner_id: int, on_pick, *, placeholder="Pick one…"):
        super().__init__(timeout=180)
        self.owner_id = owner_id
        self._on_pick = on_pick
        opts = options[:25]
        sel = discord.ui.Select(placeholder=placeholder, options=opts)
        sel.callback = self._cb
        self._sel = sel
        self.add_item(sel)

    async def interaction_check(self, inter):
        if inter.user.id != self.owner_id:
            await inter.response.send_message(_DENY_NOT_OWNER, ephemeral=True)
            return False
        return True

    async def _cb(self, inter: discord.Interaction):
        self._sel.disabled = True
        await self._on_pick(inter, self._sel.values[0])
        self.stop()


def _pair_value(p) -> str:
    """Stable token for a pair option: wl_id|eng_id (falls back to names)."""
    return f"{(p.wl_discord_id or p.war_leader)}|{(p.eng_discord_id or p.engineer)}"


def _member_value(m) -> str:
    return (m.discord_id or "").strip() or m.name


class BuddyManageView(discord.ui.View):
    """Owner-locked manual pairing editor: Unpair / Pair / Re-pair / Refresh."""

    def __init__(self, bot, guild_id: int, owner_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild_id = guild_id
        self.owner_id = owner_id
        self.message: Optional[discord.Message] = None
        self._add("🔓 Unpair", discord.ButtonStyle.danger, self._unpair)
        self._add("➕ Pair", discord.ButtonStyle.success, self._pair)
        self._add("🔁 Re-pair", discord.ButtonStyle.primary, self._repair)
        self._add("🔄 Refresh", discord.ButtonStyle.secondary, self._refresh)

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

    async def _save_pairs_list(self, cfg, pairs: list):
        """Persist an explicit pair list (no auto-fill) and refresh surfaces."""
        members = await asyncio.to_thread(_load_members, self.guild_id, cfg)
        result = buddy.assign_buddies(
            members,
            pairs,
            engineer_doubling=bool(cfg.get("engineer_doubling")),
            wl_priority=_wl_priority(cfg),
            fill=False,
        )
        await asyncio.to_thread(save_result, self.guild_id, cfg, result)
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
            result = await self._save_pairs_list(cfg, remaining)
            await i.followup.send("🔓 Unpaired.", ephemeral=True)
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
        if cfg.get("engineer_doubling"):
            grouped = {wl: engs for wl, engs in _group_pairs(result)}
            doublable = [
                buddy.Member(name=wl, profession=buddy.WAR_LEADER)
                for wl, engs in grouped.items()
                if len(engs) < 2
            ]
        else:
            doublable = []
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
                pairs.append(
                    buddy.Pair(wl.name, wl.discord_id, eng.name, eng.discord_id, source="manual")
                )
                res = await self._save_pairs_list(cfg, pairs)
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
        if not free_eng:
            await inter.followup.send(
                "ℹ️ No free Engineers to swap in. Unpair someone first.", ephemeral=True
            )
            return
        opts = [
            discord.SelectOption(label=f"{p.war_leader} ↔ {p.engineer}"[:100], value=_pair_value(p))
            for p in pairs
        ]

        async def _pick_pair(i: discord.Interaction, value: str):
            target = next((p for p in pairs if _pair_value(p) == value), None)
            eng_opts = [
                discord.SelectOption(label=m.name[:100], value=_member_value(m)) for m in free_eng
            ]

            async def _pick_eng(i2: discord.Interaction, eng_value: str):
                await i2.response.defer(ephemeral=True, thinking=True)
                eng = next((m for m in free_eng if _member_value(m) == eng_value), None)
                new_pairs = [p for p in pairs if _pair_value(p) != value]
                new_pairs.append(
                    buddy.Pair(
                        target.war_leader,
                        target.wl_discord_id,
                        eng.name,
                        eng.discord_id,
                        source="manual",
                    )
                )
                res = await self._save_pairs_list(cfg, new_pairs)
                await i2.followup.send(
                    f"🔁 **{target.war_leader}** is now paired with **{eng.name}**.",
                    ephemeral=True,
                )
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

    async def _refresh(self, inter: discord.Interaction):
        await inter.response.defer(ephemeral=True, thinking=True)
        cfg = self._cfg()
        result = await asyncio.to_thread(compute_current, self.guild_id, cfg)
        await self._refresh_editor(inter, result, cfg)
        await inter.followup.send("🔄 Refreshed.", ephemeral=True)
