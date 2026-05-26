"""
train_ui.py — Discord UI components for the /train command.

Exports:
  - run_blurb_wizard_for_entry: walk a user through Theme/Tone/Notes for an entry
  - TrainActionView: action bar (Add / Update / Generate Prompt / Clear)
  - AddEntryModal, UpdateEntryModal, UpdateSelectView,
    GeneratePromptSelectView, RunWizardView, ConfirmTrainClearView

Kept separate from train.py to keep that file at a manageable size and to
make the new UI surface easy to find.
"""

import asyncio
from datetime import date, timedelta

import discord

import wizard_registry
from train import (
    active_wizards,
    WIZARD_TIMEOUT,
    ThemeSelectView,
    ToneSelectView,
    build_chatgpt_prompt,
    load_schedule,
    save_schedule,
    mark_blurb_generated,
    parse_date_and_name,
)


# ── Blurb wizard wrapper (writes back to schedule entry) ──────────────────────

async def run_blurb_wizard_for_entry(bot, channel, user, date_str: str, name: str, guild_id: int) -> bool:
    """
    Walk the user through Theme → Tone → Notes for the entry on `date_str`,
    save theme/tone/notes back into the schedule, build the ChatGPT prompt,
    and mark the entry as prompt_retrieved.

    Returns True on success, False on cancel/timeout.
    """
    if user.id in active_wizards:
        await channel.send("⚠️ You already have an active session. Use `/cancel` to stop it first.")
        return False

    cancel_event = asyncio.Event()
    active_wizards[user.id] = cancel_event

    def check_msg(m):
        return m.author == user and m.channel == channel

    async def ask(prompt: str) -> str | None:
        msg = await channel.send(prompt) if prompt else None
        try:
            reply_task  = asyncio.ensure_future(bot.wait_for("message", check=check_msg, timeout=WIZARD_TIMEOUT))
            cancel_task = asyncio.ensure_future(cancel_event.wait())
            done, pending = await asyncio.wait([reply_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            if cancel_event.is_set():
                if msg:
                    try: await msg.delete()
                    except discord.HTTPException: pass
                return None
            reply = done.pop().result()
            try:
                if msg: await msg.delete()
                await reply.delete()
            except discord.HTTPException:
                pass
            return reply.content.strip()
        except asyncio.TimeoutError:
            await channel.send(
                "⏰ Wizard timed out. Run `/train overview` and click **📋 Generate Prompt** to try again."
            )
            return None

    async def wait_for_view(view, msg) -> bool:
        view_task   = asyncio.ensure_future(view.wait())
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        done, pending = await asyncio.wait([view_task, cancel_task], return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if cancel_event.is_set():
            for item in view.children:
                item.disabled = True
            try: await msg.edit(view=view)
            except discord.HTTPException: pass
            return False
        return True

    try:
        _d      = date.fromisoformat(date_str)
        d_label = f"{_d:%A, %B} {_d.day}"
        await channel.send(
            f"🚂 **Train Blurb Wizard for {name}** — {d_label}\n"
            f"*(Type `/cancel` at any time to stop)*"
        )

        # Step 1: Theme
        theme_msg  = await channel.send("**Step 1 of 3 — Theme**\nSelect the theme for this train:")
        theme_view = ThemeSelectView(guild_id=guild_id)
        await theme_msg.edit(view=theme_view)
        if not await wait_for_view(theme_view, theme_msg):
            return False
        if theme_view.selected is None:
            await channel.send(
                "⏰ Wizard timed out. Run `/train overview` and click **📋 Generate Prompt** to try again."
            )
            return False
        theme = theme_view.selected
        if theme == "Custom":
            theme = await ask("Type your custom theme:")
            if not theme:
                return False

        # Step 2: Tone
        tone_msg  = await channel.send("**Step 2 of 3 — Tone**\nSelect the tone:")
        tone_view = ToneSelectView(guild_id=guild_id)
        await tone_msg.edit(view=tone_view)
        if not await wait_for_view(tone_view, tone_msg):
            return False
        if tone_view.selected is None:
            await channel.send(
                "⏰ Wizard timed out. Run `/train overview` and click **📋 Generate Prompt** to try again."
            )
            return False
        tone = tone_view.selected

        # Step 3: Notes
        notes_raw = await ask(
            "**Step 3 of 3 — Notes** *(highly recommended)*\n"
            "Add anything personal — role, personality, achievements. Type your notes, or type `skip`:"
        )
        if notes_raw is None:
            return False
        notes = "" if notes_raw.lower() == "skip" else notes_raw

        # 💎 Premium step 4: pick which saved template to use, when more than one exists.
        import premium
        from train import get_train_template_names
        template_name = None
        if await premium.is_premium(guild_id, bot=bot):
            names = get_train_template_names(guild_id)
            if len(names) > 1:
                class TemplatePickView(discord.ui.View):
                    def __init__(self, options: list[str]):
                        super().__init__(timeout=120)
                        self.selected = None
                        sel = discord.ui.Select(
                            placeholder="Pick a saved template…",
                            options=[discord.SelectOption(label=n[:100], value=n) for n in options],
                        )
                        async def _cb(inter):
                            self.selected = sel.values[0]
                            sel.disabled  = True
                            await wizard_registry.safe_edit_response(
                                inter,
                                content=f"✅ Template: **{self.selected}**", view=self,
                            )
                            self.stop()
                        sel.callback = _cb
                        self.add_item(sel)

                pick_view = TemplatePickView(names)
                pick_msg  = await channel.send(
                    "**Step 4 of 4 — Template** *(💎 Premium)*\n"
                    "You have multiple saved templates. Pick one for this prompt:",
                    view=pick_view,
                )
                if not await wait_for_view(pick_view, pick_msg):
                    return False
                template_name = pick_view.selected   # may stay None if user picked nothing → falls back to default

        # Persist back to schedule
        schedule = load_schedule(guild_id)
        schedule[date_str] = {
            "name":             name,
            "theme":            theme,
            "tone":             tone,
            "notes":            notes,
            "prompt_retrieved": True,
        }
        save_schedule(schedule, guild_id)

        # Build and post the prompt
        prompt = build_chatgpt_prompt(
            name, theme, tone, notes, guild_id=guild_id, template_name=template_name,
        )
        await channel.send(
            f"✅ **ChatGPT prompt for {name}** — copy and paste into the thread:\n"
            f"```\n{prompt}\n```"
        )
        return True

    finally:
        active_wizards.pop(user.id, None)


# ── /train UI: views + modals ─────────────────────────────────────────────────

class RunWizardView(discord.ui.View):
    """Yes/Skip prompt offered after Add or Update when blurbs_enabled."""
    def __init__(self, bot, guild_id: int, date_iso: str, name: str):
        super().__init__(timeout=120)
        self.bot      = bot
        self.guild_id = guild_id
        self.date_iso = date_iso
        self.name     = name

    @discord.ui.button(label="✅ Run blurb wizard", style=discord.ButtonStyle.success)
    async def yes(self, inter: discord.Interaction, button: discord.ui.Button):
        for c in self.children: c.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        await run_blurb_wizard_for_entry(
            self.bot, inter.channel, inter.user, self.date_iso, self.name, self.guild_id,
        )
        self.stop()

    @discord.ui.button(label="⏭️ Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, inter: discord.Interaction, button: discord.ui.Button):
        for c in self.children: c.disabled = True
        await wizard_registry.safe_edit_response(inter, view=self)
        self.stop()


class AddEntryModal(discord.ui.Modal, title="Add Train Entry"):
    def __init__(self, bot, guild_id: int, blurbs_enabled: bool):
        super().__init__()
        self.bot            = bot
        self.guild_id       = guild_id
        self.blurbs_enabled = blurbs_enabled
        self.date_input = discord.ui.TextInput(
            label="Date", placeholder="e.g. April 5 or 4/5",
            required=True, max_length=20,
        )
        self.name_input = discord.ui.TextInput(
            label="Member name", placeholder="Exactly as it should appear",
            required=True, max_length=64,
        )
        self.add_item(self.date_input)
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        date_text = self.date_input.value.strip()
        name      = self.name_input.value.strip()
        d, _, _   = parse_date_and_name(f"{date_text} - placeholder")
        if d is None:
            await interaction.response.send_message(
                f"⚠️ Could not parse date `{date_text}`. Try formats like `April 5` or `4/5`.",
                ephemeral=True,
            )
            return

        # Defer before sheet I/O — a slow gspread round-trip can otherwise
        # blow Discord's 3-second initial-response window and fail with
        # NotFound 10062 Unknown interaction.
        await interaction.response.defer(ephemeral=True)

        d_iso    = d.isoformat()
        schedule = load_schedule(self.guild_id)
        existed  = d_iso in schedule
        existing = schedule.get(d_iso, {})
        schedule[d_iso] = {
            "name":             name,
            "theme":            existing.get("theme", ""),
            "tone":             existing.get("tone", ""),
            "notes":            existing.get("notes", ""),
            "prompt_retrieved": existing.get("prompt_retrieved", False),
        }
        save_schedule(schedule, self.guild_id)

        verb = "Updated" if existed else "Added"
        msg  = f"✅ {verb} **{name}** for **{d:%A, %B} {d.day}**."

        if self.blurbs_enabled:
            view = RunWizardView(self.bot, self.guild_id, d_iso, name)
            await interaction.followup.send(
                f"{msg}\n\nRun the blurb wizard now to build the ChatGPT prompt?",
                view=view, ephemeral=True,
            )
        else:
            await interaction.followup.send(msg, ephemeral=True)


class UpdateEntryModal(discord.ui.Modal, title="Update Train Entry"):
    def __init__(self, bot, guild_id: int, blurbs_enabled: bool,
                 original_date_iso: str, original_entry: dict):
        super().__init__()
        self.bot               = bot
        self.guild_id          = guild_id
        self.blurbs_enabled    = blurbs_enabled
        self.original_date_iso = original_date_iso
        self.original_entry    = original_entry

        d_obj = date.fromisoformat(original_date_iso)
        self.date_input = discord.ui.TextInput(
            label="Date", default=f"{d_obj.month}/{d_obj.day}",
            required=True, max_length=20,
        )
        self.name_input = discord.ui.TextInput(
            label="Member name", default=original_entry.get("name", ""),
            required=True, max_length=64,
        )
        self.add_item(self.date_input)
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        date_text = self.date_input.value.strip()
        new_name  = self.name_input.value.strip()
        d, _, _   = parse_date_and_name(f"{date_text} - placeholder")
        if d is None:
            await interaction.response.send_message(
                f"⚠️ Could not parse date `{date_text}`.", ephemeral=True
            )
            return

        # Defer before sheet I/O — a slow gspread round-trip can otherwise
        # blow Discord's 3-second initial-response window and fail with
        # NotFound 10062 Unknown interaction.
        await interaction.response.defer(ephemeral=True)

        new_iso  = d.isoformat()
        schedule = load_schedule(self.guild_id)

        # Preserve theme/tone/notes/prompt_retrieved from the original entry
        merged = {
            "name":             new_name,
            "theme":            self.original_entry.get("theme", ""),
            "tone":             self.original_entry.get("tone", ""),
            "notes":            self.original_entry.get("notes", ""),
            "prompt_retrieved": self.original_entry.get("prompt_retrieved", False),
        }

        # If date changed, drop the old key so the entry moves
        if new_iso != self.original_date_iso and self.original_date_iso in schedule:
            del schedule[self.original_date_iso]

        schedule[new_iso] = merged
        save_schedule(schedule, self.guild_id)

        msg = f"✅ Updated **{new_name}** for **{d:%A, %B} {d.day}**."

        if self.blurbs_enabled:
            view = RunWizardView(self.bot, self.guild_id, new_iso, new_name)
            await interaction.followup.send(
                f"{msg}\n\nRe-run the blurb wizard to refresh the ChatGPT prompt?",
                view=view, ephemeral=True,
            )
        else:
            await interaction.followup.send(msg, ephemeral=True)


class UpdateSelectView(discord.ui.View):
    """Select-menu for picking which existing entry to update."""
    def __init__(self, bot, guild_id: int, blurbs_enabled: bool, entries: list):
        super().__init__(timeout=120)
        self.bot            = bot
        self.guild_id       = guild_id
        self.blurbs_enabled = blurbs_enabled
        self._entries       = dict(entries[:25])

        options = []
        for d_iso, entry in entries[:25]:
            d_obj = date.fromisoformat(d_iso)
            label = f"{d_obj:%a %b} {d_obj.day} — {entry.get('name', '?')}"[:100]
            options.append(discord.SelectOption(label=label, value=d_iso))

        select = discord.ui.Select(placeholder="Choose an entry to update...", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, inter: discord.Interaction):
        d_iso = inter.data["values"][0]
        entry = self._entries[d_iso]
        modal = UpdateEntryModal(self.bot, self.guild_id, self.blurbs_enabled, d_iso, entry)
        await inter.response.send_modal(modal)
        self.stop()


class GeneratePromptSelectView(discord.ui.View):
    """Select-menu for picking which filled entry to generate a prompt for."""
    def __init__(self, bot, guild_id: int, entries: list):
        super().__init__(timeout=120)
        self.bot      = bot
        self.guild_id = guild_id
        self._entries = dict(entries[:25])

        options = []
        for d_iso, entry in entries[:25]:
            d_obj = date.fromisoformat(d_iso)
            label = f"{d_obj:%a %b} {d_obj.day} — {entry.get('name', '?')}"[:100]
            options.append(discord.SelectOption(label=label, value=d_iso))

        select = discord.ui.Select(placeholder="Choose an entry...", options=options)
        select.callback = self._on_select
        self.add_item(select)

    async def _on_select(self, inter: discord.Interaction):
        d_iso = inter.data["values"][0]
        entry = self._entries[d_iso]
        prompt = build_chatgpt_prompt(
            name=entry.get("name", ""),
            theme=entry.get("theme", ""),
            tone=entry.get("tone", ""),
            notes=entry.get("notes", ""),
            guild_id=self.guild_id,
        )
        await inter.response.send_message(
            f"✅ **ChatGPT prompt for {entry.get('name', '')}** — copy and paste into the thread:\n"
            f"```\n{prompt}\n```",
            ephemeral=False,
        )
        mark_blurb_generated(d_iso, self.guild_id)
        self.stop()


class ConfirmTrainClearView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)
        self.confirmed = False

    @discord.ui.button(label="Yes, clear it", style=discord.ButtonStyle.danger)
    async def confirm(self, inter: discord.Interaction, button: discord.ui.Button):
        self.confirmed = True
        await inter.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.defer()
        self.stop()


class TrainActionView(discord.ui.View):
    """Action bar shown beneath the /train schedule embed."""
    def __init__(self, bot, guild_id: int, blurbs_enabled: bool):
        super().__init__(timeout=300)
        self.bot            = bot
        self.guild_id       = guild_id
        self.blurbs_enabled = blurbs_enabled

    @discord.ui.button(label="➕ Add", style=discord.ButtonStyle.success)
    async def add(self, inter: discord.Interaction, button: discord.ui.Button):
        await inter.response.send_modal(
            AddEntryModal(self.bot, self.guild_id, self.blurbs_enabled)
        )

    @discord.ui.button(label="✏️ Update", style=discord.ButtonStyle.primary)
    async def update(self, inter: discord.Interaction, button: discord.ui.Button):
        schedule = load_schedule(self.guild_id)
        today    = date.today()
        cutoff   = today - timedelta(days=7)
        upper    = today + timedelta(days=30)
        entries  = []
        for d_iso, entry in schedule.items():
            try:
                d_obj = date.fromisoformat(d_iso)
            except ValueError:
                continue
            if cutoff <= d_obj <= upper:
                entries.append((d_iso, entry))
        entries.sort(key=lambda t: t[0])

        if not entries:
            await inter.response.send_message(
                "ℹ️ No entries to update in the past 7 / next 30 days. Use **➕ Add** to create one.",
                ephemeral=True,
            )
            return

        view = UpdateSelectView(self.bot, self.guild_id, self.blurbs_enabled, entries)
        await inter.response.send_message(
            "Select an entry to update:", view=view, ephemeral=True
        )

    @discord.ui.button(label="📋 Generate Prompt", style=discord.ButtonStyle.secondary)
    async def generate(self, inter: discord.Interaction, button: discord.ui.Button):
        schedule = load_schedule(self.guild_id)
        today    = date.today()
        upper    = today + timedelta(days=14)
        entries  = []
        for d_iso, entry in schedule.items():
            try:
                d_obj = date.fromisoformat(d_iso)
            except ValueError:
                continue
            if today <= d_obj <= upper and entry.get("name") and entry.get("theme"):
                entries.append((d_iso, entry))
        entries.sort(key=lambda t: t[0])

        if not entries:
            await inter.response.send_message(
                "ℹ️ No filled entries in the next 14 days. Use **➕ Add** or **✏️ Update**, "
                "then run the blurb wizard to fill in theme/tone/notes first.",
                ephemeral=True,
            )
            return

        view = GeneratePromptSelectView(self.bot, self.guild_id, entries)
        await inter.response.send_message(
            "Select an entry to generate a prompt for:", view=view, ephemeral=True
        )

    @discord.ui.button(label="🗑️ Clear", style=discord.ButtonStyle.danger)
    async def clear(self, inter: discord.Interaction, button: discord.ui.Button):
        view = ConfirmTrainClearView()
        await inter.response.send_message(
            "⚠️ Clear the entire train schedule? This cannot be undone.",
            view=view, ephemeral=True,
        )
        await view.wait()
        if view.confirmed:
            save_schedule({}, self.guild_id)
            await inter.followup.send("🗑️ Train schedule cleared.", ephemeral=True)
        else:
            await inter.followup.send(
                "✅ Clear cancelled. Your train schedule is unchanged.",
                ephemeral=True,
            )
