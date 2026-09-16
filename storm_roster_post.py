"""
Approve & Post for the structured roster builder: refresh the powers,
build the mail, render the image, ask how to send a long mail, post it,
write the rosters tab, acknowledge the builder message, tell the officer
what happened, offer the DM fan-out, and warn about members the image
dropped.

Split out of `storm_roster_builder.py` in the #589 step 11 refactor.
`storm_roster_builder` imports `finalize_structured_roster` back as
`_finalize_structured_roster`, so its two buttons and every test keep
working. This module reaches back into `storm_roster_builder` through
`_builder()` at call time for the mail body, the picker and DM views,
the sheet write, the embed, the power reader and the size limits, so
`patch("storm_roster_builder.X")` keeps its target.

Shape: one function per stage of the post, in the order they happen,
and a `_PostResult` carrying the outcome (`posted_ok`, `no_channel`,
`channel_gone`, `send_failed`) from the post to the summary.
`finalize_structured_roster` is the orchestrator. Every message the
officer or the channel sees is unchanged; `tests/unit/test_storm_roster_post.py`
and `TestFinalizePostOutcomes` hold this to the function it replaced.
"""

from __future__ import annotations

import asyncio
import io
import logging
from dataclasses import dataclass
from typing import Optional

import discord

from messages import CANCEL_BACKPEDAL

logger = logging.getLogger(__name__)


def _builder():
    """The roster builder module, resolved at call time (see the module docstring)."""
    import storm_roster_builder

    return storm_roster_builder


@dataclass
class _PostResult:
    """What became of the post, for the officer summary.

    no_channel    the alliance never configured a post channel
    channel_gone  the channel id is set but the channel was deleted or
                  the bot can't see it
    send_failed   the channel resolved but the API rejected the send
                  (perms, rate limit, ...)
    posted_ok     the happy path
    """

    status: str
    channel_id: int
    error: Optional[str] = None
    mention: Optional[str] = None

    @property
    def posted(self) -> bool:
        return self.status == "posted_ok"


def _attachment_name(s, ext: str) -> str:
    return (
        f"{s.event_type.lower()}-roster"
        + (f"-{s.event_date}" if s.event_date else "")
        + (f"-team-{s.team}" if s.team else "")
        + ext
    )


# ── Stages ───────────────────────────────────────────────────────────────────


async def _refresh_powers(interaction: discord.Interaction, s) -> None:
    """Re-read powers at finalise time so `power_at_assignment` in the
    rosters tab is the value at approval, not the snapshot from when the
    builder opened. Members whose row is gone keep None. Best effort:
    a failed read logs and the post goes ahead."""
    try:
        # Cache pre-pass (see apply_preset) — keeps the non-Discord
        # inference path inside the power reader honest under a cold
        # cache.
        try:
            import member_roster

            await member_roster._ensure_member_cache(interaction.guild)
        except Exception as e:
            logger.warning(
                "[STORM STRUCTURED] guild.chunk() pre-pass failed for guild=%s: %s",
                s.guild_id,
                e,
            )
        fresh_members, _refresh_errors = await asyncio.to_thread(
            _builder()._read_roster_powers,
            s.guild_id,
            s.event_type,
            guild=interaction.guild,
        )
        for key, m in s.members.items():
            fresh = fresh_members.get(key)
            if fresh is not None:
                m["power"] = fresh.get("power")
    except Exception as e:
        logger.warning(
            "[STORM STRUCTURED] roster re-read for power snapshot failed (guild=%s event=%s): %s",
            s.guild_id,
            s.event_date,
            e,
        )


async def _render_image(s) -> tuple[Optional[discord.File], Optional[str], list]:
    """The roster PNG for the post (#225). Returns `(file, warning,
    overflow)`: a render failure or an oversized image gives no file
    and a warning for the officer summary, so the missing attachment is
    never silent; `overflow` lists the members the slot grid dropped
    (#228 follow-up), for a second ephemeral after the post."""
    import storm_renderer

    b = _builder()
    warning: Optional[str] = None
    overflow: list = []
    try:
        roster_data = storm_renderer.roster_from_session(s)
        png_bytes = await asyncio.to_thread(storm_renderer.render, roster_data)
        overflow = list(roster_data.overflow or [])
    except RuntimeError as e:
        # Pillow missing — host doesn't have the dependency installed.
        warning = "Couldn't attach the image (host is missing Pillow). Posted text only."
        logger.warning(
            "[STORM STRUCTURED] image render skipped (Pillow missing) guild=%s event=%s: %s",
            s.guild_id,
            s.event_type,
            e,
        )
        return None, warning, overflow
    except Exception as e:
        warning = (
            f"Couldn't attach the image: `{type(e).__name__}: {str(e)[:120]}`. Posted text only."
        )
        logger.exception(
            "[STORM STRUCTURED] image render failed guild=%s event=%s",
            s.guild_id,
            s.event_type,
        )
        return None, warning, overflow
    if len(png_bytes) > b._MAX_ATTACHMENT_BYTES:
        warning = (
            f"Rendered image too large to attach "
            f"({len(png_bytes) // (1024 * 1024)} MB > 25 MB Discord "
            f"limit). Posted text only."
        )
        logger.warning(
            "[STORM STRUCTURED] image too large to attach (size=%d guild=%s event=%s)",
            len(png_bytes),
            s.guild_id,
            s.event_type,
        )
        return None, warning, overflow
    return discord.File(io.BytesIO(png_bytes), filename=_attachment_name(s, ".png")), None, overflow


async def _ask_long_mail_format(interaction: discord.Interaction, s) -> str:
    """The mail is over Discord's 2000-char ceiling and a post channel
    exists: ask the officer for "split" or "txt" (#237; pre-#237 the
    bot silently attached .txt, #234). Returns the choice, "cancel", or
    "txt" when the picker itself could not be sent."""
    b = _builder()
    picker = b._LongMailPickerView(owner_id=interaction.user.id)
    choice: Optional[str] = None
    try:
        picker.message = await interaction.followup.send(
            "📋 This message goes over the limit Discord allows for "
            "a single post. To be able to post this for you, we "
            "have two options:\n\n"
            "📨 **Send as 2 posts** splits at the next natural "
            "break so the second post starts with a section "
            "heading.\n\n"
            "💾 **Send as .txt attachment** posts the full mail as "
            "a file alongside the image. Copy the file's contents "
            "to send in-game.",
            view=picker,
            ephemeral=True,
        )
    except discord.HTTPException as e:
        logger.warning(
            "[STORM STRUCTURED] long-mail picker followup failed (guild=%s event=%s): %s",
            s.guild_id,
            s.event_type,
            e,
        )
        # Better than leaving the interaction stuck.
        choice = "txt"
    if choice is None:
        await picker.wait()
        choice = picker.choice or "cancel"
    # Tear the picker down whatever the outcome, so the cancel ack or
    # the post-result ack is the most recent visible message.
    if getattr(picker, "message", None) is not None:
        try:
            await picker.message.delete()
        except discord.HTTPException:
            pass
    return choice


async def _cancel_post(interaction: discord.Interaction, view) -> None:
    """The officer cancelled at the long-mail picker: release the
    session lock so the builder can be reopened, and post nothing."""
    try:
        view._release_session_lock()
    except AttributeError:
        pass
    view.stop()
    try:
        await interaction.followup.send(
            CANCEL_BACKPEDAL.format(
                detail="Roster wasn't posted; you can keep editing the builder if you'd like.",
            ),
            ephemeral=True,
        )
    except discord.HTTPException:
        pass


async def _send_mail(post_channel, s, mail: str, image_file, long_mail_choice: Optional[str]):
    """Post in one of three shapes:
      short mail             one post, the mail [+ image]
      long mail + "split"    two posts split at a heading; the image
                             rides the LAST post so it sits next to the
                             last visible heading (tester report
                             2026-05-23)
      long mail + "txt"      one post with the mail as a .txt [+ image]
    A split with no clean heading falls back to .txt so the officer
    still gets the whole mail. Raises on any send failure."""
    b = _builder()
    over = len(mail) > b._MAX_MESSAGE_CONTENT
    if long_mail_choice == "split" and over:
        parts = b._split_mail_at_heading(mail)
        if parts is not None:
            part1, part2 = parts
            await post_channel.send(part1)
            if image_file is not None:
                await post_channel.send(part2, file=image_file)
            else:
                await post_channel.send(part2)
            return
        long_mail_choice = "txt"

    files: list[discord.File] = []
    if over:
        files.append(
            discord.File(io.BytesIO(mail.encode("utf-8")), filename=_attachment_name(s, ".txt"))
        )
        content = (
            f"📋 **{s.event_type} Roster** — full mail "
            f"attached (longer than Discord's 2000-char "
            f"message limit). Copy from the attachment to "
            f"send in-game."
        )
    else:
        content = mail
    if image_file is not None:
        files.append(image_file)
    # `file=` for one attachment, `files=` for two+, so the single-image
    # happy path keeps its kwarg shape.
    if len(files) == 1:
        await post_channel.send(content, file=files[0])
    elif len(files) > 1:
        await post_channel.send(content, files=files)
    else:
        await post_channel.send(content)


async def _post(
    post_channel, post_channel_id: int, s, mail, image_file, long_mail_choice
) -> _PostResult:
    if not post_channel_id:
        return _PostResult("no_channel", post_channel_id)
    if post_channel is None:
        return _PostResult("channel_gone", post_channel_id)
    try:
        await _send_mail(post_channel, s, mail, image_file, long_mail_choice)
    except Exception as e:
        logger.warning(
            "[STORM STRUCTURED] failed to post mail to channel=%s guild=%s: %s",
            post_channel_id,
            s.guild_id,
            e,
        )
        return _PostResult("send_failed", post_channel_id, error=str(e))
    return _PostResult("posted_ok", post_channel_id, mention=post_channel.mention)


def _summary_lines(
    result: _PostResult, s, write_errors: list[str], image_warning: Optional[str]
) -> list[str]:
    if result.posted:
        lines = ["✅ Roster posted.", f"📬 Mail sent to {result.mention}."]
    elif result.status == "no_channel":
        from setup_hub import STORM_SETUP_NAV

        setup_cmd = STORM_SETUP_NAV[s.event_type]
        lines = [
            "✅ Roster recorded.",
            "⚠️ No post channel is configured. Mail was built but not "
            f"sent. Run `{setup_cmd}` to pick one, or copy the mail "
            "manually below.",
        ]
    elif result.status == "channel_gone":
        lines = [
            "✅ Roster recorded.",
            f"⚠️ The configured post channel (<#{result.channel_id}>) is "
            f"deleted or the bot can't see it. Re-run setup to pick a new "
            f"channel. Mail preview below.",
        ]
    else:  # send_failed
        lines = [
            "✅ Roster recorded.",
            f"⚠️ The configured post channel <#{result.channel_id}> rejected "
            f"the send: `{(result.error or 'unknown error')[:120]}`. Check "
            f"the bot's permissions in that channel. Mail preview below.",
        ]
    if write_errors:
        lines.append("⚠️ " + write_errors[0])
    if image_warning is not None:
        lines.append("⚠️ " + image_warning)
    return lines


def _detail_text(summary_lines: list[str], mail: str, posted: bool) -> str:
    """The officer's ephemeral: the summary, plus the mail preview when
    it was not auto-posted so they can copy it. Budgeted under Discord's
    2000-char cap, or the recovery ephemeral itself blew the limit and
    left the interaction stuck in "thinking…" (tester report 2026-05-21)."""
    max_len = _builder()._MAX_MESSAGE_CONTENT
    detail = "\n".join(summary_lines)
    if not posted:
        # 8 chars for the ```\n…\n``` wrappers + 12 chars margin.
        fence_overhead = 20
        budget = max_len - len(detail) - fence_overhead
        if budget < 200:
            budget = 200  # always show at least a short snippet
        if len(mail) <= budget:
            preview = mail
        else:
            preview = mail[: budget - 20] + "\n…(truncated)"
        detail += f"\n\n```\n{preview}\n```"
    # Hard cap, so a future bug here can't bring the stuck state back.
    if len(detail) > max_len:
        detail = detail[: max_len - 20] + "\n…(truncated)"
    return detail


async def _send_detail(interaction: discord.Interaction, s, detail: str) -> None:
    try:
        await interaction.followup.send(detail, ephemeral=True)
    except discord.HTTPException as e:
        # Last resort: keep the interaction out of "thinking…" even if
        # the detail ephemeral fails.
        logger.warning(
            "[STORM STRUCTURED] detail ephemeral failed (guild=%s event=%s, len=%d): %s",
            s.guild_id,
            s.event_type,
            len(detail),
            e,
        )
        try:
            await interaction.followup.send(
                "⚠️ Roster recorded but the confirmation message "
                "couldn't be sent. Check the configured post channel.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass  # nothing else to do; at least it's not stuck thinking


async def _offer_roster_dms(interaction: discord.Interaction, s) -> None:
    """#226 follow-up: one click to DM every primary, paired sub and pool
    sub their assignment. Premium only (the bot fans out personalised
    messages); the button re-checks Premium at click time too."""
    try:
        import premium

        is_premium = await premium.is_premium(
            s.guild_id,
            bot=interaction.client,
            interaction=interaction,
        )
    except Exception as e:
        logger.warning(
            "[STORM DM] premium check failed (guild=%s): %s",
            s.guild_id,
            e,
        )
        is_premium = False
    if not is_premium:
        return
    dm_view = _builder()._DmRosteredMembersView(s, interaction.client, owner_id=interaction.user.id)
    dm_intro = (
        "📨 **DM rostered members?**\n"
        "Click below to DM each rostered member their "
        "personal assignment(s). Subs in paired mode get a "
        "note about which primary they're covering; the pool "
        "subs get a standby message.\n\n"
        "_Members without a linked Discord ID or with DMs "
        "closed get listed back here after — no DM goes out "
        "to them._"
    )
    try:
        await interaction.followup.send(dm_intro, view=dm_view, ephemeral=True)
    except discord.HTTPException as e:
        logger.warning(
            "[STORM DM] DM-the-roster ephemeral failed (guild=%s event=%s): %s",
            s.guild_id,
            s.event_type,
            e,
        )


def overflow_warning(image_overflow: list) -> str:
    """#228 follow-up: the members the image's slot grid dropped, grouped
    by zone and stage. They are still in the mail and the rosters tab;
    only the render lost them."""
    from collections import OrderedDict

    grouped: "OrderedDict[tuple[str, int], list[str]]" = OrderedDict()
    for entry in image_overflow:
        key = (entry.canonical_zone, entry.phase)
        grouped.setdefault(key, []).append(entry.name)
    bullet_lines = []
    for (zone, phase), names in grouped.items():
        label = f"**{zone}**"
        if phase >= 1:
            label += f" Stage {phase}"
        bullet_lines.append(f"• {label}: {', '.join(names)}")
    return (
        f"⚠️ **{len(image_overflow)} member(s) didn't fit in the "
        f"posted image.** They're still in the mail body and the "
        f"rosters_tab — only the image render dropped them.\n\n"
        + "\n".join(bullet_lines)
        + "\n\nShorter Discord display names (≤ 20 chars) help — "
        "the image render truncates anything longer and a long "
        "name eats one slot in its zone's row grid."
    )


async def _warn_image_overflow(interaction: discord.Interaction, s, image_overflow: list) -> None:
    try:
        await interaction.followup.send(overflow_warning(image_overflow), ephemeral=True)
    except discord.HTTPException as e:
        logger.warning(
            "[STORM STRUCTURED] overflow warning followup failed (guild=%s event=%s): %s",
            s.guild_id,
            s.event_type,
            e,
        )


# ── Entry point ──────────────────────────────────────────────────────────────


async def finalize_structured_roster(
    interaction: discord.Interaction,
    view,
    *,
    include_image: bool = False,
) -> None:
    """Approve & Post: posts the structured mail to the configured
    post channel and writes one row per slot to rosters_tab.

    `include_image=True` (#225) renders the roster as a PNG and
    attaches it to the same `channel.send` that carries the mail body,
    so the post lands as one message with both. Render failure (Pillow
    missing, encode error, >25 MB) falls back to text-only — the post
    still goes through, and the officer ephemeral confirmation tacks on
    a warning so the missing attachment isn't silent.
    """
    import config

    b = _builder()
    s = view.session
    await interaction.response.defer(ephemeral=True, thinking=True)

    await _refresh_powers(interaction, s)

    # `_build_mail_body` honours paired sub_mode (#224) and phase-aware
    # presets (one block per stage).
    mail = b._build_mail_body(s)

    cfg = config.get_storm_config(s.guild_id, s.event_type)
    post_channel_id = int(cfg.get("post_channel_id") or 0)
    post_channel = None
    if post_channel_id and interaction.guild:
        post_channel = interaction.guild.get_channel(post_channel_id)

    image_file: Optional[discord.File] = None
    image_warning: Optional[str] = None
    image_overflow: list = []
    if include_image and post_channel_id and post_channel is not None:
        image_file, image_warning, image_overflow = await _render_image(s)

    long_mail_choice: Optional[str] = None
    if len(mail) > b._MAX_MESSAGE_CONTENT and post_channel is not None:
        long_mail_choice = await _ask_long_mail_format(interaction, s)
        if long_mail_choice == "cancel":
            await _cancel_post(interaction, view)
            return

    result = await _post(post_channel, post_channel_id, s, mail, image_file, long_mail_choice)

    # One row per slot, best effort: a failed write logs and does not
    # roll back the Discord post. Off the loop: a multi-cell gspread
    # update can block for 1-2 seconds under load.
    write_errors = await asyncio.to_thread(b._write_rosters_tab, s)

    for item in view.children:
        item.disabled = True

    summary_lines = _summary_lines(result, s, write_errors, image_warning)

    # Slim public ack on the original builder message.
    try:
        if view.message:
            await view.message.edit(
                content="✅ Structured roster approved and posted.",
                embed=b._render_builder_embed(s),
                view=view,
            )
    except discord.HTTPException:
        pass

    await _send_detail(interaction, s, _detail_text(summary_lines, mail, result.posted))

    if result.posted:
        await _offer_roster_dms(interaction, s)
    if image_overflow and result.posted:
        await _warn_image_overflow(interaction, s, image_overflow)

    view._release_session_lock()
    view.stop()
