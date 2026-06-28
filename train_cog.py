"""
train_cog.py — TrainCog (slash commands + reminder loop) for the train module.

Hosts:
  /train                          (the train hub — schedule, prompt log, birthdays)
  /birthdays, /cancel             (standalone top-level)
  + check_reminder background task

Kept separate from train.py to keep that file at a manageable size.
"""

import asyncio
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import get_config
from messages import SETUP_POINTER_FOOTER
from setup_hub import HUB_BTN_BIRTHDAYS
from train import (
    ET,
    active_wizards,
    ReminderView,
    _guard,
    load_schedule,
    save_schedule,
    load_birthdays,
    check_and_add_birthdays,
    render_conflict_message,
    get_member_tab_name,
)


# ── Default DM bodies (fallbacks when an alliance hasn't customised) ──────────

DEFAULT_BIRTHDAY_DM = (
    "🎂 Happy birthday, **{name}**! Wishing you a great day from everyone at the alliance."
)

DEFAULT_TRAIN_DM = (
    "🚂 Heads up — **today's train is for you!** "
    "Leadership has been notified, so look out for the announcement."
)


def _render_dm_body(template: str, *, name: str = "") -> str:
    """Substitute `{name}` into a user-configured DM body. Tolerates
    missing or unknown placeholders so a typo in the configured template
    doesn't crash the entire reminder loop — the typo just renders as
    literal text in the DM."""

    class _SafeDict(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    try:
        return template.format_map(_SafeDict(name=name or ""))
    except Exception:
        return template.replace("{name}", name or "")


# Alias used inside slash commands so the `date` parameter name doesn't shadow it
date_cls = date


def _pretty_day(iso: str) -> str:
    """ISO date → 'Thu, Jul 3' for button labels and confirmations."""
    d = date.fromisoformat(iso)
    return f"{d:%a, %b} {d.day}"


# ── Birthday conflict alert (interactive) ────────────────────────────────────────


class BirthdayConflictView(discord.ui.View):
    """Interactive resolution for a birthday→train scheduling conflict.

    Replaces the old fire-and-forget text alert. Posted to the leadership
    channel when one or more members can't be auto-placed near their
    birthday, it offers three actions:

      • 📅 a dropdown of open days (each member's birthday→+7 window) that
        places the member with one click and writes it to the schedule,
      • 📋 Show next 7 days — an ephemeral read-only view of the surrounding
        schedule so leadership can see what's free,
      • 🙈 Ignore — persists a dismissal so the *daily* re-post stops
        nagging about a conflict that's been handled off-schedule.

    Placing a member silences future alerts on its own: the next daily run's
    ±window "already handled" check sees the new entry. The view is NOT
    persistent — its buttons stop working after the timeout or a bot
    restart — but the loop re-posts a fresh, working alert each day the
    conflict is still open, so nothing is permanently lost.
    """

    def __init__(self, cog, guild_id: int, conflicts: list[dict]):
        # 12h window so leadership has the evening + overnight to act; if it
        # lapses, the 22:00 ET loop re-posts a fresh alert next day.
        super().__init__(timeout=43200)
        self.cog = cog
        self.guild_id = guild_id
        self.conflicts = conflicts
        # Set by the loop right after channel.send so on_timeout / the
        # resolution callbacks can edit the original alert message.
        self.message = None

        # Date dropdown: "<member> → <open day>" across every conflict, so
        # one control resolves the common single-member case and the rarer
        # multi-member case alike. Discord caps a select at 25 options.
        options: list[discord.SelectOption] = []
        truncated = False
        for i, c in enumerate(conflicts):
            for iso in c.get("open_dates", []):
                if len(options) >= 25:
                    truncated = True
                    break
                label = f"{c['name']} → {_pretty_day(iso)}"
                options.append(discord.SelectOption(label=label[:100], value=f"{i}|{iso}"))
            if truncated:
                break
        if truncated:
            print(
                f"[BIRTHDAY] Conflict alert for guild {guild_id} truncated the "
                f"day dropdown to 25 options — 📋 Show next 7 days lists the rest."
            )
        if options:
            self._select = discord.ui.Select(
                placeholder="📅 Place a member on an open day…",
                min_values=1,
                max_values=1,
                options=options,
            )
            self._select.callback = self._on_place
            self.add_item(self._select)
        else:
            self._select = None

        show_btn = discord.ui.Button(
            label="📋 Show next 7 days", style=discord.ButtonStyle.secondary
        )
        show_btn.callback = self._on_show
        self.add_item(show_btn)

        ignore_btn = discord.ui.Button(label="🙈 Ignore", style=discord.ButtonStyle.danger)
        ignore_btn.callback = self._on_ignore
        self.add_item(ignore_btn)

    async def on_timeout(self):
        """Strip the controls and point leadership at the manual escape
        hatch. Without this the buttons look live after the view stops
        listening and clicks fail with 'Interaction failed'."""
        from wizard_registry import expire_view_message

        await expire_view_message(
            self.message,
            command_hint="`/train` → 🎂 Run birthday check (it also re-posts tonight)",
        )

    async def _ensure_leadership(self, interaction: discord.Interaction) -> bool:
        cfg = get_config(self.guild_id)
        if not cfg:
            await interaction.response.send_message(
                "⚙️ Bot not configured. Run `/setup`.", ephemeral=True
            )
            return False
        role_names = [r.name for r in getattr(interaction.user, "roles", [])]
        if cfg.leadership_role_name not in role_names:
            await interaction.response.send_message(
                f"⛔ You need the **{cfg.leadership_role_name}** role to do that.",
                ephemeral=True,
            )
            return False
        return True

    async def _on_place(self, interaction: discord.Interaction):
        if not await self._ensure_leadership(interaction):
            return
        # Defer first: load + save hit Google Sheets and can blow the 3s
        # interaction-token window (cf. #76).
        await interaction.response.defer(ephemeral=True)

        idx_str, date_iso = self._select.values[0].split("|", 1)
        conflict = self.conflicts[int(idx_str)]

        loop = asyncio.get_running_loop()
        schedule = await loop.run_in_executor(None, load_schedule, self.guild_id)
        # The slot may have been taken since the alert posted.
        if date_iso in schedule:
            await interaction.followup.send(
                f"⚠️ **{_pretty_day(date_iso)}** was just taken by "
                f"**{schedule[date_iso].get('name', 'someone')}**. Pick another day.",
                ephemeral=True,
            )
            return
        schedule[date_iso] = {
            "name": conflict["name"],
            "theme": "Birthday",
            "tone": "",
            "notes": "Manually placed from birthday conflict alert",
            "prompt_retrieved": False,
        }
        await loop.run_in_executor(None, save_schedule, schedule, self.guild_id)

        # Resolved: the new entry is now visible to the daily ±window check,
        # so no re-alert. Drop it from the live alert.
        self.conflicts.pop(int(idx_str))
        await interaction.followup.send(
            f"✅ Placed **{conflict['name']}** on **{_pretty_day(date_iso)}**.",
            ephemeral=True,
        )
        await self._rerender()

    async def _on_show(self, interaction: discord.Interaction):
        # Read-only — anyone who can see the leadership channel may peek.
        await interaction.response.defer(ephemeral=True)
        loop = asyncio.get_running_loop()
        schedule = await loop.run_in_executor(None, load_schedule, self.guild_id)

        embed = discord.Embed(
            title="📋 Schedule around the conflict", color=discord.Color.blurple()
        )
        for c in self.conflicts:
            bday = date.fromisoformat(c["bday_iso"])
            lines = []
            for i in range(0, 8):
                d = bday + timedelta(days=i)
                occupant = schedule.get(d.isoformat(), {}).get("name", "").strip()
                marker = "🎂 " if i == 0 else ""
                tail = f"— {occupant}" if occupant else "— *(open)*"
                lines.append(f"{marker}**{d:%a, %b} {d.day}** {tail}")
            embed.add_field(
                name=f"{c['name']} · birthday {c['bday_fmt']}",
                value="\n".join(lines),
                inline=False,
            )
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _on_ignore(self, interaction: discord.Interaction):
        if not await self._ensure_leadership(interaction):
            return
        from config import mark_conflict_ignored

        names = ", ".join(c["name"] for c in self.conflicts)
        for c in self.conflicts:
            mark_conflict_ignored(self.guild_id, c["key"])
        await interaction.response.send_message(
            "🙈 Dismissed — you won't get this alert again for these birthdays.",
            ephemeral=True,
        )
        try:
            await self.message.edit(
                content=f"🙈 **Birthday conflict dismissed.** Won't alert again about: {names}.",
                view=None,
            )
        except discord.HTTPException:
            pass
        self.stop()

    async def _rerender(self):
        """After a placement, refresh the original alert: rebuild it with the
        remaining conflicts, or mark it fully resolved and drop the controls."""
        if self.conflicts:
            new_view = BirthdayConflictView(self.cog, self.guild_id, self.conflicts)
            new_view.message = self.message
            try:
                await self.message.edit(
                    content=render_conflict_message(self.conflicts), view=new_view
                )
            except discord.HTTPException:
                pass
        else:
            try:
                await self.message.edit(
                    content=(
                        "✅ **Birthday scheduling conflict resolved.** "
                        "Every affected member is now on the schedule."
                    ),
                    view=None,
                )
            except discord.HTTPException:
                pass
        self.stop()


# ── Cog ────────────────────────────────────────────────────────────────────────


class TrainCog(commands.Cog):
    # `/train` is a single top-level hub command (it opens an embed + button
    # grid via train_hub.handle_train_hub) — the same shape as `/events` and the
    # storm hubs. It replaced the old `/train overview|log|birthdays` subcommands
    # and the Train Conductor Rotation subcommands (#55). `/birthdays` (the
    # standalone member list) and `/cancel` (the wizard escape hatch) stay
    # top-level. The two background loops live here too.

    def __init__(self, bot):
        self.bot = bot
        # Initialise to today's ET date so the first tick after deploy
        # doesn't trip the "new day, reset reminders_fired" branch.
        self.last_reminder_date = datetime.now(tz=ET).date()
        self.reminders_fired = set()  # train-assignment reminders sent today
        # `birthday_population_fired` used to dedup the 22:00 ET train
        # auto-pop via an in-memory set. Railway restarts wiped it, so
        # the auto-pop re-fired and spammed conflict messages on every
        # redeploy. Dedup now lives on `guild_birthday_config
        # .last_train_population_date` (see #89), read fresh from
        # SQLite on every tick.
        # ── Train Conductor Rotation (#55) ──────────────────────────────
        # Separate per-minute loop for the rotation feature: the weekly
        # draft (fires on the configured draft day) and the daily
        # confirmation (fires every drive day). In-memory per-day dedup
        # sets mirror `reminders_fired`; robust outage catch-up is #227's
        # job (it will stamp this loop's heartbeat).
        self.last_rotation_date = datetime.now(tz=ET).date()
        self.rotation_draft_fired = set()  # guild_ids that posted a draft today
        self.rotation_confirm_fired = set()  # guild_ids that posted a confirm today
        self.check_reminder.start()
        self.check_rotation.start()

    def cog_unload(self):
        self.check_reminder.cancel()
        self.check_rotation.cancel()

    # ── /train hub ───────────────────────────────────────────────────────────

    @app_commands.command(
        name="train",
        description="Open the train hub for this alliance",
    )
    @app_commands.guild_only()
    async def train(self, interaction: discord.Interaction):
        from train_hub import handle_train_hub

        await handle_train_hub(self.bot, interaction)

    # ── /birthdays ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="birthdays",
        description="Show upcoming birthdays from your member sheet (uses your configured lookahead window)",
    )
    async def birthdays(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        from config import get_birthday_config

        guild_id = interaction.guild_id if hasattr(interaction, "guild_id") else None
        bcfg = get_birthday_config(guild_id) if guild_id else {}
        tab_name = bcfg.get("tab_name") or get_member_tab_name(guild_id)
        # Use the configured lookahead window from /setup → 🎂 Birthdays. Defaults
        # to 14 days when not set so a fresh install still shows something
        # useful out of the box.
        window_days = int(bcfg.get("lookahead_days") or 14)

        try:
            members = await asyncio.get_event_loop().run_in_executor(
                None, load_birthdays, tab_name, guild_id
            )
        except Exception as e:
            await interaction.followup.send(f"⚠️ Could not load birthdays: {e}", ephemeral=True)
            return

        if not members:
            await interaction.followup.send(
                f"⚠️ No birthdays found in **{tab_name}**. Run `/setup → {HUB_BTN_BIRTHDAYS}` to verify the tab and column settings.",
                ephemeral=True,
            )
            return

        today = date.today()
        upcoming = []
        for m in members:
            try:
                # Find the next occurrence of this birthday on or after today
                this_year = date(today.year, m["month"], m["day"])
                if this_year < today:
                    next_occurrence = date(today.year + 1, m["month"], m["day"])
                else:
                    next_occurrence = this_year
            except ValueError:
                continue
            days_away = (next_occurrence - today).days
            if 0 <= days_away <= window_days:
                upcoming.append((days_away, next_occurrence, m["name"]))

        upcoming.sort(key=lambda t: (t[0], t[2].lower()))

        embed = discord.Embed(
            title=f"🎂 Upcoming Birthdays — Next {window_days} Days",
            color=discord.Color.magenta(),
        )

        if not upcoming:
            embed.description = "*No birthdays in the next 14 days.*"
        else:
            lines = []
            for days_away, when, name in upcoming:
                if days_away == 0:
                    label = "**Today!**"
                elif days_away == 1:
                    label = "Tomorrow"
                else:
                    label = f"in {days_away} days"
                lines.append(f"• **{when:%A, %B} {when.day}** — {name} *({label})*")
            embed.description = "\n".join(lines)

        embed.set_footer(
            text=f"Source: {tab_name} · " + SETUP_POINTER_FOOTER.format(wizard=HUB_BTN_BIRTHDAYS),
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="cancel", description="Cancel any active wizard or log session")
    async def cancel(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return
        cancelled = False
        if interaction.user.id in active_wizards:
            active_wizards[interaction.user.id].set()
            cancelled = True
        try:
            from storm_log import active_logs

            if interaction.user.id in active_logs:
                active_logs[interaction.user.id].set()
                cancelled = True
        except ImportError:
            pass
        try:
            import wizard_registry

            if wizard_registry.cancel_user(interaction.user.id):
                cancelled = True
        except ImportError:
            pass
        if cancelled:
            await interaction.response.send_message("❌ Session cancelled.", ephemeral=True)
        else:
            await interaction.response.send_message(
                "ℹ️ You don't have an active session running.", ephemeral=True
            )

    # ── Reminder loop ──────────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def check_reminder(self):
        from config import get_config, get_train_config, stamp_loop_heartbeat
        from zoneinfo import ZoneInfo

        now = datetime.now(tz=ET)
        today = now.date()

        # Reset daily flag at midnight ET
        if self.last_reminder_date != today:
            self.last_reminder_date = today
            self.reminders_fired = set()  # train reminders fired today

        # ── Birthday auto-population and Discord announcements ────────────────
        # Per-guild try/except: a single misconfigured guild (bad perms,
        # missing tab, gspread hiccup) must not abort the loop for every
        # other guild. Specific failure modes (channel-send Forbidden,
        # DM Forbidden) are caught closer to the call so the log line
        # names the channel/user that needs fixing.
        from config import get_config, get_birthday_config
        from zoneinfo import ZoneInfo as _ZI

        for guild in self.bot.guilds:
            try:
                cfg = get_config(guild.id)
                bcfg = get_birthday_config(guild.id)
                if not cfg or not cfg.setup_complete or not bcfg.get("enabled"):
                    continue

                # Birthday auto-population into the train schedule.
                # Fires once per guild per day at exactly 22:00 ET — that
                # lines up with 00:00 server time, the alliance's nightly
                # reset. Exact-minute trigger matches the Discord birthday
                # announcement pattern below; if Railway is restarting
                # across that minute, the /train hub's 🎂 Run birthday
                # check button is the manual escape hatch. Dedup persists in
                # `guild_birthday_config.last_train_population_date` so
                # Railway redeploys at 22:00 don't re-fire — the previous
                # in-memory set was wiped on every restart (#89).
                if bcfg.get("train_integration") and now.hour == 22 and now.minute == 0:
                    from config import (
                        get_birthday_population_last_fired,
                        mark_birthday_population_fired,
                    )

                    today_iso = today.isoformat()
                    # Skip the auto-pop block when today's run already
                    # landed (Railway restart at 22:00, second tick
                    # within the minute, etc.) — but fall through to
                    # the Discord birthday announcement below either
                    # way; that check has its own time gate.
                    if get_birthday_population_last_fired(guild.id) != today_iso:
                        try:
                            current_schedule = load_schedule(guild.id)
                            # Snapshot for change detection — check_and_add_birthdays
                            # mutates `current_schedule` in place and returns the
                            # same object, so comparing the return value to the
                            # input would always be equal (the original 1.0.x bug).
                            before = dict(current_schedule)
                            updated_schedule, conflicts = check_and_add_birthdays(
                                current_schedule,
                                guild_id=guild.id,
                            )
                            if updated_schedule != before or conflicts:
                                save_schedule(updated_schedule, guild.id)
                            if conflicts:
                                alert_channel = self.bot.get_channel(cfg.leadership_channel_id)
                                if alert_channel:
                                    view = BirthdayConflictView(self, guild.id, conflicts)
                                    view.message = await alert_channel.send(
                                        render_conflict_message(conflicts), view=view
                                    )
                            # Stamp *after* a successful run so a mid-fire
                            # crash leaves the day un-stamped and a manual
                            # /train → 🎂 Run birthday check (or the next
                            # deploy) can retry.
                            mark_birthday_population_fired(guild.id, today_iso)
                        except Exception as e:
                            import traceback

                            print(f"[BIRTHDAY] Auto-population failed for guild {guild.id}: {e}")
                            print(traceback.format_exc())

                # Birthday Discord announcements
                if not bcfg.get("reminders_enabled"):
                    continue

                reminder_time = bcfg.get("reminder_time", "08:00")
                try:
                    r_h, r_m = int(reminder_time.split(":")[0]), int(reminder_time.split(":")[1])
                    guild_tz = _ZI(cfg.timezone or "America/New_York")
                    guild_now = datetime.now(tz=guild_tz)
                    if guild_now.hour != r_h or guild_now.minute != r_m:
                        continue
                except (ValueError, IndexError, ZoneInfoNotFoundError) as e:
                    print(
                        f"[BIRTHDAY] Bad reminder_time={reminder_time!r} or "
                        f"timezone={cfg.timezone!r} for guild {guild.id}: {e}"
                    )
                    continue

                bday_channel_id = bcfg.get("reminder_channel_id", 0)
                bday_channel = self.bot.get_channel(bday_channel_id)
                if not bday_channel:
                    print(
                        f"[BIRTHDAY] Reminder channel {bday_channel_id} not "
                        f"resolvable for guild {guild.id} — Discord birthday "
                        f"announcement skipped"
                    )
                    continue

                # Find today's birthdays
                tab_name = bcfg.get("tab_name", "Birthdays")
                members = load_birthdays(tab_name, guild.id)
                from datetime import date as _d2

                today = _d2.today()
                todays_bdays = [
                    m for m in members if m["month"] == today.month and m["day"] == today.day
                ]

                # Resolve the alliance's configured DM template once per
                # guild — falling back to the bot's hardcoded default if
                # /setup → 🎂 Birthdays hasn't been run since dm_message landed.
                bday_dm_tmpl = (bcfg.get("dm_message") or "").strip() or DEFAULT_BIRTHDAY_DM

                for member in todays_bdays:
                    name = member.get("name", "a member")
                    # @mention if Discord ID available (from the birthday sheet)
                    discord_id = member.get("discord_id")
                    if discord_id:
                        mention = f"<@{discord_id}>"
                    else:
                        mention = f"**{name}**"
                    try:
                        await bday_channel.send(f"🎂 Today is {mention}'s birthday!")
                    except discord.Forbidden:
                        # Bot lacks View Channel or Send Messages on the
                        # configured birthday channel for this guild. No
                        # point retrying the remaining members — every
                        # send to this channel will fail the same way.
                        chan_name = getattr(bday_channel, "name", "?")
                        print(
                            f"[BIRTHDAY] Missing perms to send in channel "
                            f"{bday_channel_id} (#{chan_name}) for guild "
                            f"{guild.id} ({guild.name}) — leadership must "
                            f"grant View Channel + Send Messages or "
                            f"reconfigure via /setup → {HUB_BTN_BIRTHDAYS}"
                        )
                        break

                    # 💎 Premium: also DM the member directly with a personal note.
                    if discord_id:
                        import dm

                        await dm.send_dm_to_id(
                            self.bot,
                            guild.id,
                            discord_id,
                            content=_render_dm_body(bday_dm_tmpl, name=name),
                        )

            except Exception as e:
                import traceback

                print(f"[BIRTHDAY] Error during birthday check for guild {guild.id}: {e}")
                print(f"[BIRTHDAY] Traceback:\n{traceback.format_exc()}")

        # ── Per-guild train reminders ──────────────────────────────────────────
        for guild in self.bot.guilds:
            if guild.id in self.reminders_fired:
                continue

            cfg = get_config(guild.id)
            train_cfg = get_train_config(guild.id)

            if not cfg or not cfg.setup_complete:
                continue
            # Rotation-enabled guilds get the #55 daily confirmation instead of
            # this legacy "today's train is for X" reminder — skip them here so
            # they don't receive both.
            if train_cfg.get("rotation_enabled"):
                continue
            if not train_cfg.get("reminders_enabled", 1):
                continue

            # Parse reminder time and compare to current time in guild's timezone
            reminder_time = train_cfg.get("reminder_time", "22:00")
            try:
                r_h, r_m = int(reminder_time.split(":")[0]), int(reminder_time.split(":")[1])
                guild_tz = ZoneInfo(cfg.timezone or "America/New_York")
                guild_now = datetime.now(tz=guild_tz)
                if guild_now.hour != r_h or guild_now.minute != r_m:
                    continue
            except (ValueError, IndexError, ZoneInfoNotFoundError) as e:
                print(
                    f"[TRAIN] Bad reminder_time={reminder_time!r} or "
                    f"timezone={cfg.timezone!r} for guild {guild.id}: {e}"
                )
                continue

            # Announce against the Last War in-game (server, UTC-2) date: this
            # reminder fires at the in-game reset, which is already the next
            # in-game day, so the local calendar date would name yesterday's
            # train. See config.server_date_for.
            from config import server_date_for

            today_str = server_date_for(guild_now).isoformat()
            schedule = load_schedule(guild.id)
            if today_str not in schedule:
                self.reminders_fired.add(guild.id)
                continue

            entry = schedule[today_str]
            name = entry.get("name", "Unknown")

            # Get reminder channel — fall back to leadership channel
            channel_id = train_cfg.get("reminder_channel_id") or cfg.leadership_channel_id
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                # Marked fired so we don't retry every minute, but log the
                # symptom — leadership won't notice "reminder stopped firing"
                # unless we surface the channel-resolve failure here.
                print(
                    f"[TRAIN] Reminder channel {channel_id} not resolvable "
                    f"for guild {guild.id} — daily reminder skipped"
                )
                self.reminders_fired.add(guild.id)
                continue

            # 💎 Premium: replace the name with a Discord mention if the
            # member roster knows them. Free tier sees just the name.
            import dm

            display = await dm.mention_or_name(self.bot, guild.id, name)

            blurbs_on = train_cfg.get("blurbs_enabled", 1)
            if blurbs_on:
                view = ReminderView(cog=self, date_str=today_str, name=name)
                msg = (
                    f"🚂 **Reset! Today's train is for {display}.**\n\n"
                    f"Click below whenever you're ready to get the ChatGPT prompt — "
                    f"no rush, run it when the team is available.\n\n"
                    f"⚠️ *If the button stops working after a bot restart, use `/train` → 📋 Schedule overview → 📋 Generate Prompt instead.*"
                )
                view.message = await channel.send(msg, view=view)
            else:
                await channel.send(f"🚂 **Reset! Today's train is for {display}.**")

            self.reminders_fired.add(guild.id)
            print(f"[TRAIN] Reminder sent for guild {guild.id} — {name} on {today_str}")

            # 💎 Premium: also DM the member assigned to today's train.
            # Body is alliance-configurable via the train setup wizard; falls back
            # to the bot's hardcoded default if not customised.
            train_dm_tmpl = (train_cfg.get("dm_message") or "").strip() or DEFAULT_TRAIN_DM
            import dm

            await dm.send_dm(
                self.bot,
                guild.id,
                name,
                content=_render_dm_body(train_dm_tmpl, name=name),
            )

        # Clean tick — stamp liveness for the outage catch-up scan (#227).
        # One heartbeat covers both surfaces in this loop (the birthday
        # Discord announcement and the train daily reminder).
        stamp_loop_heartbeat("train_reminder")

    @check_reminder.before_loop
    async def before_check_reminder(self):
        await self.bot.wait_until_ready()

    # ── Train Conductor Rotation loop (#55) ──────────────────────────────────

    @tasks.loop(minutes=1)
    async def check_rotation(self):
        """Per-minute tick driving the two rotation surfaces: the weekly draft
        (on the configured draft day/time) and the daily confirmation (every
        drive day at the reminder time). Both are gated on `rotation_enabled`,
        so guilds that haven't opted into rotation are untouched. Per-guild
        try/except isolates one misconfigured guild from the rest."""
        from config import get_config, get_train_config

        today = datetime.now(tz=ET).date()
        if self.last_rotation_date != today:
            self.last_rotation_date = today
            self.rotation_draft_fired = set()
            self.rotation_confirm_fired = set()

        for guild in self.bot.guilds:
            try:
                cfg = get_config(guild.id)
                tcfg = get_train_config(guild.id)
                if not cfg or not cfg.setup_complete or not tcfg.get("rotation_enabled"):
                    continue
                try:
                    guild_tz = ZoneInfo(cfg.timezone or "America/New_York")
                except ZoneInfoNotFoundError:
                    guild_tz = ZoneInfo("America/New_York")
                guild_now = datetime.now(tz=guild_tz)
                await self._maybe_post_weekly_draft(guild, cfg, tcfg, guild_now)
                await self._maybe_post_daily_confirm(guild, cfg, tcfg, guild_now)
            except Exception as e:
                import traceback

                print(f"[TRAIN ROTATION] check_rotation failed for guild {guild.id}: {e}")
                print(traceback.format_exc())

    @staticmethod
    def _hm(value: str, default: str) -> tuple[int, int]:
        """Parse 'HH:MM' → (hour, minute), falling back to `default`."""
        try:
            h, m = (value or default).split(":")
            return int(h), int(m)
        except (ValueError, AttributeError):
            h, m = default.split(":")
            return int(h), int(m)

    async def _maybe_post_weekly_draft(self, guild, cfg, tcfg, guild_now):
        """Generate + post the weekly draft when the draft day/time hits.

        Posts to the train reminder channel (the rotation reuses it, falling
        back to the leadership channel) at the train reminder time. The draft
        covers this week when it fires on a Monday, otherwise the upcoming week
        (so a Sunday draft previews the week that starts the next day)."""
        import train_rotation_ui as ui

        draft_day = int(tcfg.get("weekly_draft_day", 6))
        r_h, r_m = self._hm(tcfg.get("reminder_time"), "22:00")
        if guild_now.weekday() != draft_day or guild_now.hour != r_h or guild_now.minute != r_m:
            return
        if guild.id in self.rotation_draft_fired:
            return

        channel_id = tcfg.get("reminder_channel_id") or cfg.leadership_channel_id
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            print(
                f"[TRAIN ROTATION] draft channel {channel_id} not resolvable for "
                f"guild {guild.id} — weekly draft skipped"
            )
            self.rotation_draft_fired.add(guild.id)
            return

        today = guild_now.date()
        week_start = (
            today if today.weekday() == 0 else today + timedelta(days=(7 - today.weekday()))
        )
        preset_name = tcfg.get("active_schedule_preset") or "Standard Week"

        draft = await ui.regenerate_week_async(self.bot, guild.id, week_start)
        view = ui.WeeklyDraftView(self.bot, guild.id, draft, week_start, preset_name)
        try:
            view.message = await channel.send(
                embed=ui.build_weekly_draft_embed(draft, week_start, preset_name), view=view
            )
        except discord.Forbidden:
            print(
                f"[TRAIN ROTATION] missing perms to post draft in {channel_id} for guild {guild.id}"
            )
        self.rotation_draft_fired.add(guild.id)
        print(f"[TRAIN ROTATION] weekly draft posted for guild {guild.id} (week of {week_start})")

    async def _maybe_post_daily_confirm(self, guild, cfg, tcfg, guild_now):
        """Post today's conductor confirmation when the reminder time hits.

        Posts to the train reminder channel (reused by rotation). Reads today's
        scheduled row from Train History; skips silently if the day was already
        posted, skipped, or has no scheduled conductor."""
        import train_rotation as tr
        import train_rotation_ui as ui

        r_h, r_m = self._hm(tcfg.get("reminder_time"), "22:00")
        if guild_now.hour != r_h or guild_now.minute != r_m:
            return
        if guild.id in self.rotation_confirm_fired:
            return
        self.rotation_confirm_fired.add(guild.id)  # mark first — one attempt/day

        # Resolve against the Last War in-game (server, UTC-2) date, not the
        # guild's local calendar date. The reminder fires at the in-game reset
        # (~2h before local midnight), which is already the next in-game day, so
        # the local date would name the conductor for the day that just ended —
        # the "train a day behind" bug. See config.server_date_for.
        from config import server_date_for

        today_iso = server_date_for(guild_now).isoformat()
        history = await asyncio.get_event_loop().run_in_executor(
            None, tr.load_history, guild.id, tcfg.get("history_tab") or ""
        )
        row = next((h for h in history if h.date == today_iso), None)
        if row is None or row.status != tr.STATUS_SCHEDULED:
            # No draft covers today, or it's already posted/skipped — nothing to do.
            return

        channel_id = tcfg.get("reminder_channel_id") or cfg.leadership_channel_id
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            print(
                f"[TRAIN ROTATION] confirm channel {channel_id} not resolvable for "
                f"guild {guild.id} — daily confirmation skipped"
            )
            return

        dd = tr.DraftDay(
            date=row.date,
            weekday=date_cls.fromisoformat(row.date).weekday(),
            rule_type=row.reason if row.reason in tr.RULE_LABELS else tr.RULE_AUTO,
            member=row.member or None,
            reason=row.reason,
            needs_picking=not bool(row.member),
        )
        # 0 = alliance opted out of public posts; the confirmation just records.
        public_channel_id = tcfg.get("rotation_public_channel_id") or 0
        view = ui.DailyConfirmView(self.bot, guild.id, dd, public_channel_id)
        try:
            view.message = await channel.send(embed=ui.build_daily_confirm_embed(dd), view=view)
        except discord.Forbidden:
            print(
                f"[TRAIN ROTATION] missing perms to post confirmation in {channel_id} "
                f"for guild {guild.id}"
            )
            return
        print(f"[TRAIN ROTATION] daily confirmation posted for guild {guild.id} ({today_iso})")

    @check_rotation.before_loop
    async def before_check_rotation(self):
        await self.bot.wait_until_ready()
