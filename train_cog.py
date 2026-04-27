"""
train_cog.py — TrainCog (slash commands + reminder loop) for the train module.

Hosts:
  /train, /train_log, /train_addbirthdays, /birthdays, /cancel
  + check_reminder background task

Kept separate from train.py to keep that file at a manageable size.
"""

import asyncio
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config import get_config
from train import (
    ET,
    BIRTHDAY_LOOKAHEAD,
    active_wizards,
    ReminderView,
    _guard,
    load_schedule,
    save_schedule,
    load_blurb_log,
    load_birthdays,
    check_and_add_birthdays,
    get_member_tab_name,
    parse_date_and_name,
    build_train_view_embed,
)


# Alias used inside slash commands so the `date` parameter name doesn't shadow it
date_cls = date


# ── Cog ────────────────────────────────────────────────────────────────────────

class TrainCog(commands.Cog):
    def __init__(self, bot):
        self.bot                 = bot
        self.reminder_sent_today = False  # kept for backward compat
        self.last_reminder_date  = None
        self.reminders_fired     = set()
        self.check_reminder.start()

    def cog_unload(self):
        self.check_reminder.cancel()

    # ── /train_addbirthdays ────────────────────────────────────────────────────

    @app_commands.command(
        name="train_addbirthdays",
        description="Manually run the birthday check and add upcoming birthdays to the schedule",
    )
    async def train_addbirthdays(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        try:
            current_schedule = load_schedule()
            before_count     = len(current_schedule)
            updated_schedule, alerts = check_and_add_birthdays(current_schedule, guild_id=interaction.guild_id if hasattr(interaction, "guild_id") else None)
            after_count      = len(updated_schedule)
            added            = after_count - before_count

            if added > 0 or alerts:
                save_schedule(updated_schedule)

            # Post any conflict alerts to the channel directly (high visibility)
            channel = interaction.channel
            for alert in alerts:
                if channel:
                    await channel.send(alert)

            if added > 0 and not alerts:
                await interaction.followup.send(
                    f"✅ Birthday check complete — added **{added}** birthday entr{'y' if added == 1 else 'ies'} to the schedule.",
                    ephemeral=True,
                )
            elif added > 0 and alerts:
                await interaction.followup.send(
                    f"✅ Birthday check complete — added **{added}** birthday entr{'y' if added == 1 else 'ies'} to the schedule. "
                    f"⚠️ **{len(alerts)}** conflict(s) posted above require manual action.",
                    ephemeral=True,
                )
            elif alerts:
                await interaction.followup.send(
                    f"⚠️ Birthday check complete — **{len(alerts)}** conflict(s) posted above require manual action.",
                    ephemeral=True,
                )
            else:
                await interaction.followup.send(
                    f"✅ Birthday check complete — no new entries to add within the next {BIRTHDAY_LOOKAHEAD} days.",
                    ephemeral=True,
                )
        except Exception as e:
            await interaction.followup.send(
                f"⚠️ Birthday check failed: {e}",
                ephemeral=True,
            )

    # ── /birthdays ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="birthdays",
        description="Show the next 14 days of upcoming birthdays from your member sheet",
    )
    async def birthdays(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        from config import get_birthday_config
        guild_id = interaction.guild_id if hasattr(interaction, "guild_id") else None
        bcfg     = get_birthday_config(guild_id) if guild_id else {}
        tab_name = bcfg.get("tab_name") or get_member_tab_name(guild_id)

        try:
            members = await asyncio.get_event_loop().run_in_executor(
                None, load_birthdays, tab_name, guild_id
            )
        except Exception as e:
            await interaction.followup.send(
                f"⚠️ Could not load birthdays: {e}", ephemeral=True
            )
            return

        if not members:
            await interaction.followup.send(
                f"⚠️ No birthdays found in **{tab_name}**. Run `/setup_birthdays` to verify the tab and column settings.",
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
            if 0 <= days_away <= 14:
                upcoming.append((days_away, next_occurrence, m["name"]))

        upcoming.sort(key=lambda t: (t[0], t[2].lower()))

        embed = discord.Embed(
            title="🎂 Upcoming Birthdays — Next 14 Days",
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

        embed.set_footer(text=f"Source: {tab_name} · Run /setup_birthdays to change settings")
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /train_log ─────────────────────────────────────────────────────────────

    @app_commands.command(
        name="train_log",
        description="Show the train prompt log (window depends on your tier; pass a date to filter)",
    )
    @app_commands.describe(date="Optional date, e.g. 'April 14' or '4/14'")
    async def train_log(self, interaction: discord.Interaction, date: str = None):
        if not await _guard(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        try:
            schedule = await asyncio.get_event_loop().run_in_executor(None, load_schedule)
        except Exception as e:
            await interaction.followup.send(f"⚠️ Could not load schedule: {e}", ephemeral=True)
            return

        target_date = None
        if date:
            parsed_d, _, _ = parse_date_and_name(f"{date} - placeholder")
            if not parsed_d:
                await interaction.followup.send(
                    f"⚠️ Could not parse date **{date}**. Try a format like `April 14` or `4/14`.",
                    ephemeral=True,
                )
                return
            target_date = parsed_d

        embed = discord.Embed(
            title="🚂 Train Prompt Log",
            color=discord.Color.blurple(),
        )

        from datetime import date as _date
        today = _date.today()

        if target_date:
            entry = schedule.get(target_date.isoformat())
            if not entry:
                embed.description = f"*No train entry found for {target_date:%B} {target_date.day}, {target_date.year}.*"
            else:
                embed.add_field(name="Date",   value=f"{target_date:%A, %B} {target_date.day}, {target_date.year}", inline=False)
                embed.add_field(name="Name",   value=entry.get("name") or "*not set*",               inline=False)
                embed.add_field(name="Theme",  value=entry.get("theme") or "*not set*",              inline=False)
                embed.add_field(name="Tone",   value=entry.get("tone")  or "*not set*",              inline=False)
                embed.add_field(name="Notes",  value=(entry.get("notes") or "*none*")[:1024],        inline=False)
                embed.add_field(
                    name="Prompt Retrieved",
                    value="✅ Yes" if entry.get("prompt_retrieved") else "❌ No",
                    inline=False,
                )
        else:
            import premium
            window_days = await premium.get_limit(
                "train_log_days", interaction.guild_id, interaction=interaction,
            ) or 30
            cutoff = today - timedelta(days=window_days)
            recent = []
            for date_str, entry in schedule.items():
                try:
                    d = _date.fromisoformat(date_str)
                except ValueError:
                    continue
                if cutoff <= d <= today + timedelta(days=window_days):
                    recent.append((d, entry))
            recent.sort(key=lambda t: t[0], reverse=True)

            if not recent:
                embed.description = f"*No train entries in the past {window_days} days.*"
            else:
                lines = []
                for d, entry in recent[:20]:
                    retrieved = "✅" if entry.get("prompt_retrieved") else "❌"
                    name = entry.get("name") or "*unset*"
                    theme = entry.get("theme") or ""
                    bits = [f"**{d:%a %b} {d.day}** — {name}"]
                    if theme:
                        bits.append(theme)
                    bits.append(f"prompt {retrieved}")
                    lines.append("• " + " · ".join(bits))
                embed.description = "\n".join(lines)[:4000]
                if window_days < 30:
                    embed.set_footer(text=f"Free tier: {window_days}-day window. Upgrade to Premium for 30 days.")
                else:
                    embed.set_footer(text=f"Showing the most recent 20 entries within ±{window_days} days. Pass a date to filter.")

        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /cancel ────────────────────────────────────────────────────────────────

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

    # ── /train ─────────────────────────────────────────────────────────────────

    @app_commands.command(
        name="train",
        description="View the train schedule with Add / Update / Generate Prompt / Clear buttons",
    )
    async def train(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return

        from config import get_train_config
        train_cfg = get_train_config(interaction.guild_id)
        blurbs_on = bool(train_cfg.get("blurbs_enabled", 1))

        await interaction.response.defer()

        schedule  = load_schedule(interaction.guild_id)
        blurb_log = load_blurb_log(interaction.guild_id)
        embed     = build_train_view_embed(schedule, blurb_log)

        # Lazy import to avoid the train ⇆ train_ui circular import at load time
        from train_ui import TrainActionView
        view = TrainActionView(self.bot, interaction.guild_id, blurbs_on)

        await interaction.followup.send(embed=embed, view=view)

    # ── Reminder loop ──────────────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def check_reminder(self):
        from config import get_config, get_train_config
        from zoneinfo import ZoneInfo

        now   = datetime.now(tz=ET)
        today = date_cls.today()

        # Reset daily flag at midnight ET
        if self.last_reminder_date != today:
            self.last_reminder_date  = today
            self.reminder_sent_today = False
            self.reminders_fired     = set()  # track which guilds already fired today

        if not hasattr(self, "reminders_fired"):
            self.reminders_fired = set()

        # ── Birthday auto-population and Discord announcements ────────────────
        try:
            from config import get_config, get_birthday_config
            from datetime import date as _date
            from zoneinfo import ZoneInfo as _ZI
            today_iso = _date.today().isoformat()

            for guild in self.bot.guilds:
                cfg      = get_config(guild.id)
                bcfg     = get_birthday_config(guild.id)
                if not cfg or not cfg.setup_complete or not bcfg.get("enabled"):
                    continue

                # Birthday auto-population into train schedule
                if bcfg.get("train_integration"):
                    current_schedule = load_schedule(guild.id)
                    updated_schedule, alerts = check_and_add_birthdays(current_schedule, guild_id=guild.id)
                    if updated_schedule != current_schedule or alerts:
                        save_schedule(updated_schedule, guild.id)
                    if alerts:
                        alert_channel = self.bot.get_channel(cfg.leadership_channel_id)
                        if alert_channel:
                            for alert in alerts:
                                await alert_channel.send(alert)

                # Birthday Discord announcements
                if not bcfg.get("reminders_enabled"):
                    continue

                reminder_time = bcfg.get("reminder_time", "08:00")
                try:
                    r_h, r_m  = int(reminder_time.split(":")[0]), int(reminder_time.split(":")[1])
                    guild_tz  = _ZI(cfg.timezone or "America/New_York")
                    guild_now = datetime.now(tz=guild_tz)
                    if guild_now.hour != r_h or guild_now.minute != r_m:
                        continue
                except Exception:
                    continue

                bday_channel = self.bot.get_channel(bcfg.get("reminder_channel_id", 0))
                if not bday_channel:
                    continue

                # Find today's birthdays
                tab_name     = bcfg.get("tab_name", "Birthdays")
                members      = load_birthdays(tab_name, guild.id)
                from datetime import date as _d2
                today        = _d2.today()
                todays_bdays = [m for m in members if m["month"] == today.month and m["day"] == today.day]

                for member in todays_bdays:
                    name = member.get("name", "a member")
                    # @mention if Discord ID available (from the birthday sheet)
                    discord_id = member.get("discord_id")
                    if discord_id:
                        mention = f"<@{discord_id}>"
                    else:
                        mention = f"**{name}**"
                    await bday_channel.send(f"🎂 Today is {mention}'s birthday!")

                    # 💎 Premium: also DM the member directly with a personal note.
                    if discord_id:
                        import dm
                        await dm.send_dm_to_id(
                            self.bot, guild.id, discord_id,
                            content=(
                                f"🎂 Happy birthday, **{name}**! Wishing you a great day "
                                f"from everyone at the alliance."
                            ),
                        )

        except Exception as e:
            import traceback
            print(f"[BIRTHDAY] Error during birthday check: {e}")
            print(f"[BIRTHDAY] Traceback:\n{traceback.format_exc()}")

        # ── Per-guild train reminders ──────────────────────────────────────────
        for guild in self.bot.guilds:
            if guild.id in self.reminders_fired:
                continue

            cfg        = get_config(guild.id)
            train_cfg  = get_train_config(guild.id)

            if not cfg or not cfg.setup_complete:
                continue
            if not train_cfg.get("reminders_enabled", 1):
                continue

            # Parse reminder time and compare to current time in guild's timezone
            reminder_time = train_cfg.get("reminder_time", "22:00")
            try:
                r_h, r_m  = int(reminder_time.split(":")[0]), int(reminder_time.split(":")[1])
                guild_tz  = ZoneInfo(cfg.timezone or "America/New_York")
                guild_now = datetime.now(tz=guild_tz)
                if guild_now.hour != r_h or guild_now.minute != r_m:
                    continue
            except Exception:
                continue

            # Check if someone is scheduled today
            today_str = today.isoformat()
            schedule  = load_schedule(guild.id)
            if today_str not in schedule:
                self.reminders_fired.add(guild.id)
                continue

            entry = schedule[today_str]
            name  = entry.get("name", "Unknown")

            # Get reminder channel — fall back to leadership channel
            channel_id = train_cfg.get("reminder_channel_id") or cfg.leadership_channel_id
            channel    = self.bot.get_channel(channel_id)
            if channel is None:
                self.reminders_fired.add(guild.id)
                continue

            # 💎 Premium: replace the name with a Discord mention if the
            # member roster knows them. Free tier sees just the name.
            import dm
            display = await dm.mention_or_name(self.bot, guild.id, name)

            blurbs_on = train_cfg.get("blurbs_enabled", 1)
            if blurbs_on:
                view = ReminderView(cog=self, date_str=today_str, name=name)
                msg  = (
                    f"🚂 **Reset! Today's train is for {display}.**\n\n"
                    f"Click below whenever you're ready to get the ChatGPT prompt — "
                    f"no rush, run it when the team is available.\n\n"
                    f"⚠️ *If the button stops working after a bot restart, use `/train` → 📋 Generate Prompt instead.*"
                )
                await channel.send(msg, view=view)
            else:
                await channel.send(
                    f"🚂 **Reset! Today's train is for {display}.**"
                )

            self.reminders_fired.add(guild.id)
            print(f"[TRAIN] Reminder sent for guild {guild.id} — {name} on {today_str}")

            # 💎 Premium: also DM the member assigned to today's train.
            import dm
            await dm.send_dm(
                self.bot, guild.id, name,
                content=(
                    f"🚂 Heads up — **today's train is for you!** "
                    f"Leadership has been notified, so look out for the announcement."
                ),
            )

    @check_reminder.before_loop
    async def before_check_reminder(self):
        await self.bot.wait_until_ready()
