"""
Characterization tests for Approve & Post
(`storm_roster_builder._finalize_structured_roster`, moved to
`storm_roster_post.finalize_structured_roster` in the #589 step 11
refactor).

`TestFinalizePostOutcomes` in `test_storm_roster_builder.py` already pins
the four post outcomes, the image attachment, the long-mail picker and
the overflow warning. These pin the rest, on the same harness: the
sheet-write warning line, the oversized image, the DM offer on and off
Premium, the builder-message edit and its failure, the confirmation
fallback when the detail ephemeral fails, the split-with-no-heading
fallback, the picker-followup failure, and a failed power refresh.
Written against the pre-refactor function; they pass unchanged against
the refactored one.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import discord
import pytest

import storm_roster_builder as srb
import tests.unit.test_storm_roster_builder as builder_tests
from tests.unit.test_storm_roster_builder import fake_env  # noqa: F401  (fixture)

harness = builder_tests.TestFinalizePostOutcomes()


def _http_error(msg="boom"):
    return discord.HTTPException(MagicMock(status=500, reason="err"), msg)


def _followups(inter):
    return [
        c.args[0] if c.args else c.kwargs.get("content", "")
        for c in inter.followup.send.await_args_list
    ]


def _premium(value):
    return patch("premium.is_premium", AsyncMock(return_value=value))


async def _run(inter, view, **kw):
    await srb._finalize_structured_roster(inter, view, **kw)


# ── The officer summary ──────────────────────────────────────────────────────


class TestSummaryCopy:
    @pytest.mark.asyncio
    async def test_posted_ok_exact(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with _premium(False):
            await _run(inter, view)
        assert _followups(inter) == ["✅ Roster posted.\n📬 Mail sent to <#12345>."]
        assert view.is_finished()
        assert all(c.disabled for c in view.children)

    @pytest.mark.asyncio
    async def test_no_channel_exact_with_preview(self, fake_env):
        inter, view, _ = harness._make_structured_view(fake_env, channel=None, channel_id=0)
        with _premium(False), patch("storm_roster_builder._build_mail_body", return_value="MAIL"):
            await _run(inter, view)
        assert _followups(inter) == [
            "✅ Roster recorded.\n"
            "⚠️ No post channel is configured. Mail was built but not sent. "
            "Run `/setup → ⚔️ Desert Storm` to pick one, or copy the mail manually below."
            "\n\n```\nMAIL\n```"
        ]

    @pytest.mark.asyncio
    async def test_sheet_write_warning_is_appended(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with (
            _premium(False),
            patch(
                "storm_roster_builder._write_rosters_tab",
                return_value=["rosters_tab write failed: quota", "second"],
            ),
        ):
            await _run(inter, view)
        assert _followups(inter) == [
            "✅ Roster posted.\n📬 Mail sent to <#12345>.\n⚠️ rosters_tab write failed: quota"
        ]

    @pytest.mark.asyncio
    async def test_builder_message_is_acknowledged(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        view.message = MagicMock()
        view.message.edit = AsyncMock()
        with _premium(False):
            await _run(inter, view)
        view.message.edit.assert_awaited_once()
        assert (
            view.message.edit.await_args.kwargs["content"]
            == "✅ Structured roster approved and posted."
        )
        assert view.message.edit.await_args.kwargs["view"] is view

    @pytest.mark.asyncio
    async def test_builder_message_edit_failure_is_swallowed(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        view.message = MagicMock()
        view.message.edit = AsyncMock(side_effect=_http_error())
        with _premium(False):
            await _run(inter, view)
        assert _followups(inter) == ["✅ Roster posted.\n📬 Mail sent to <#12345>."]

    @pytest.mark.asyncio
    async def test_detail_failure_falls_back_to_a_short_confirmation(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        inter.followup.send = AsyncMock(side_effect=[_http_error(), None])
        with _premium(False):
            await _run(inter, view)
        assert _followups(inter)[1] == (
            "⚠️ Roster recorded but the confirmation message couldn't be sent. "
            "Check the configured post channel."
        )
        assert view.is_finished()

    @pytest.mark.asyncio
    async def test_detail_and_fallback_both_failing_still_finishes(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        inter.followup.send = AsyncMock(side_effect=_http_error())
        with _premium(False):
            await _run(inter, view)
        assert view.is_finished()


# ── The image ────────────────────────────────────────────────────────────────


class TestImage:
    @pytest.mark.asyncio
    async def test_too_large_posts_text_only_with_warning(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with (
            _premium(False),
            patch("storm_renderer.render", return_value=b"x" * 3_000_000),
            patch("storm_roster_builder._MAX_ATTACHMENT_BYTES", 2_000_000),
        ):
            await _run(inter, view, include_image=True)
        assert "file" not in ch.send.await_args.kwargs
        assert _followups(inter) == [
            "✅ Roster posted.\n📬 Mail sent to <#12345>.\n"
            "⚠️ Rendered image too large to attach (2 MB > 25 MB Discord limit). Posted text only."
        ]

    @pytest.mark.asyncio
    async def test_non_runtime_render_error_names_the_type(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with _premium(False), patch("storm_renderer.render", side_effect=ValueError("bad glyph")):
            await _run(inter, view, include_image=True)
        assert (
            "⚠️ Couldn't attach the image: `ValueError: bad glyph`. Posted text only."
            in _followups(inter)[0]
        )

    @pytest.mark.asyncio
    async def test_image_is_not_rendered_without_a_channel(self, fake_env):
        inter, view, _ = harness._make_structured_view(fake_env, channel=None, channel_id=0)
        with _premium(False), patch("storm_renderer.render") as render:
            await _run(inter, view, include_image=True)
        render.assert_not_called()

    @pytest.mark.asyncio
    async def test_filename_carries_date_and_team(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with _premium(False), patch("storm_renderer.render", return_value=b"png"):
            await _run(inter, view, include_image=True)
        assert ch.send.await_args.kwargs["file"].filename == "ds-roster-2026-05-18-team-A.png"


# ── Long mail ────────────────────────────────────────────────────────────────


class TestLongMail:
    @pytest.mark.asyncio
    async def test_split_without_a_heading_falls_back_to_txt(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with (
            _premium(False),
            harness._stub_picker("split"),
            patch("storm_roster_builder._build_mail_body", return_value="no headings " * 300),
        ):
            await _run(inter, view)
        ch.send.assert_awaited_once()
        content = ch.send.await_args.args[0]
        assert content == (
            "📋 **DS Roster** — full mail attached (longer than Discord's 2000-char "
            "message limit). Copy from the attachment to send in-game."
        )
        assert ch.send.await_args.kwargs["file"].filename == "ds-roster-2026-05-18-team-A.txt"

    @pytest.mark.asyncio
    async def test_picker_followup_failure_falls_back_to_txt(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        inter.followup.send = AsyncMock(side_effect=[_http_error(), None, None])
        with (
            _premium(False),
            patch("storm_roster_builder._build_mail_body", return_value="X" * 2500),
        ):
            await _run(inter, view)
        ch.send.assert_awaited_once()
        assert ch.send.await_args.kwargs["file"].filename.endswith(".txt")
        # The picker's message never existed, so nothing is deleted; the
        # summary still goes out.
        assert _followups(inter)[1].startswith("✅ Roster posted.")

    @pytest.mark.asyncio
    async def test_short_mail_never_shows_the_picker(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with _premium(False), patch.object(srb, "_LongMailPickerView") as picker:
            await _run(inter, view)
        picker.assert_not_called()

    @pytest.mark.asyncio
    async def test_cancel_releases_the_lock_and_posts_nothing(self, fake_env):
        import config

        fake, gid = fake_env
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with (
            _premium(False),
            harness._stub_picker("cancel"),
            patch("storm_roster_builder._build_mail_body", return_value="X" * 2500),
            patch("storm_roster_builder._write_rosters_tab") as write,
        ):
            await _run(inter, view)
        ch.send.assert_not_awaited()
        write.assert_not_called()
        assert view.is_finished()
        assert _followups(inter)[-1] == (
            "↩️ Canceled. Roster wasn't posted; you can keep editing the builder if you'd like."
        )
        assert config.claim_storm_session(gid, "DS", "2026-05-18", "A", user_id=99)


# ── Power refresh and the DM offer ───────────────────────────────────────────


class TestRefreshAndDmOffer:
    @pytest.mark.asyncio
    async def test_power_refresh_failure_is_not_fatal(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with (
            _premium(False),
            patch("storm_roster_builder._read_roster_powers", side_effect=RuntimeError("sheet")),
        ):
            await _run(inter, view)
        assert _followups(inter) == ["✅ Roster posted.\n📬 Mail sent to <#12345>."]

    @pytest.mark.asyncio
    async def test_premium_gets_the_dm_offer(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with _premium(True):
            await _run(inter, view)
        texts = _followups(inter)
        assert texts[0] == "✅ Roster posted.\n📬 Mail sent to <#12345>."
        assert texts[1].startswith("📨 **DM rostered members?**")
        offer = inter.followup.send.await_args_list[1]
        assert isinstance(offer.kwargs["view"], srb._DmRosteredMembersView)
        assert offer.kwargs["ephemeral"] is True

    @pytest.mark.asyncio
    async def test_free_tier_gets_no_dm_offer(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with _premium(False):
            await _run(inter, view)
        assert len(_followups(inter)) == 1

    @pytest.mark.asyncio
    async def test_premium_check_failure_means_no_offer(self, fake_env):
        ch = harness._make_fake_channel(12345, mention="<#12345>")
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with patch("premium.is_premium", AsyncMock(side_effect=RuntimeError("db"))):
            await _run(inter, view)
        assert len(_followups(inter)) == 1

    @pytest.mark.asyncio
    async def test_no_dm_offer_when_the_post_failed(self, fake_env):
        ch = harness._make_fake_channel(12345, send_raises=Exception("Missing Permissions"))
        inter, view, _ = harness._make_structured_view(fake_env, channel=ch, channel_id=12345)
        with _premium(True):
            await _run(inter, view)
        texts = _followups(inter)
        assert len(texts) == 1
        assert texts[0].startswith(
            "✅ Roster recorded.\n⚠️ The configured post channel <#12345> rejected the send: `Missing Permissions`."
        )
