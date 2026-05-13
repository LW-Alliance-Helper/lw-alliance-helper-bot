"""
Persistent SignupView for storm registration posts (#123).

Members click one of four buttons (Team A, Team B, Either, Cannot) on the
auto-posted registration message; the View records the vote into SQLite
(canonical) and mirrors it to the alliance's `signups_tab` (for human
visibility). Re-voting on a different button UPSERTs and replaces the
prior vote.

Persistence rules per the discord.py contract:
  * `timeout=None` on the View
  * stable `custom_id` on every button
  * View re-registered on bot startup via `bot.add_view(...)` against the
    original message_id, fed from `storm_registration_posts`

Custom-id schema:
    signup:{guild_id}:{event_type}:{event_date}:{vote}

Length budget: Discord caps custom_id at 100 chars. Realistic input
(guild_id up to 19 digits, event_type 2 chars, ISO date 10 chars,
vote up to 6 chars, separators 4 chars) stays well under.
"""

from __future__ import annotations

import logging

import discord

logger = logging.getLogger(__name__)

# Vote codes used in custom_ids and stored in storm_signups.vote.
# Kept in this module (rather than imported from config) so a typo in
# config can't break the persistent-View dispatch path.
_VOTE_CODES = ("a", "b", "either", "cannot")

# Human-readable confirmation messages for each vote code.
_VOTE_CONFIRMATIONS = {
    "a":      "Team A",
    "b":      "Team B",
    "either": "Either time works",
    "cannot": "Cannot participate",
}


def make_custom_id(guild_id: int, event_type: str, event_date: str, vote: str) -> str:
    """Stable encoding for a SignupView button. event_type lowercased; vote
    one of `a` / `b` / `either` / `cannot`."""
    return f"signup:{int(guild_id)}:{event_type.lower()}:{event_date}:{vote}"


def parse_custom_id(custom_id: str) -> dict | None:
    """Inverse of make_custom_id. Returns None on malformed input rather
    than raising — the button handler should treat unparseable as a no-op."""
    parts = (custom_id or "").split(":")
    if len(parts) != 5 or parts[0] != "signup":
        return None
    try:
        guild_id = int(parts[1])
    except ValueError:
        return None
    event_type = parts[2].lower()
    event_date = parts[3]
    vote = parts[4]
    if event_type not in ("ds", "cs"):
        return None
    if vote not in _VOTE_CODES:
        return None
    return {
        "guild_id":   guild_id,
        "event_type": event_type,
        "event_date": event_date,
        "vote":       vote,
    }


class SignupView(discord.ui.View):
    """Persistent View for one storm registration post. Lives forever
    (`timeout=None`); buttons have stable custom_ids so the bot can
    re-register them on startup via `bot.add_view(...)`."""

    def __init__(
        self,
        guild_id: int,
        event_type: str,
        event_date: str,
        *,
        time_a_label: str = "Team A",
        time_b_label: str = "Team B",
    ):
        super().__init__(timeout=None)
        self.guild_id   = int(guild_id)
        self.event_type = event_type.lower()
        self.event_date = event_date
        # Build buttons explicitly so labels are configurable. Decorator-
        # based buttons can't have their labels parameterised.
        self._add_vote_button("a",      f"✅ {time_a_label}",          discord.ButtonStyle.success)
        self._add_vote_button("b",      f"✅ {time_b_label}",          discord.ButtonStyle.success)
        self._add_vote_button("either", "✅ Either time works",        discord.ButtonStyle.success)
        self._add_vote_button("cannot", "❌ Cannot participate",       discord.ButtonStyle.danger)

    def _add_vote_button(self, vote_code: str, label: str, style: discord.ButtonStyle):
        btn = discord.ui.Button(
            label=label[:80],
            style=style,
            custom_id=make_custom_id(self.guild_id, self.event_type, self.event_date, vote_code),
        )
        btn.callback = self._make_callback(vote_code)
        self.add_item(btn)

    def _make_callback(self, vote_code: str):
        async def _cb(interaction: discord.Interaction):
            await _handle_signup_click(interaction, vote_code)
        return _cb


async def _handle_signup_click(interaction: discord.Interaction, vote_code: str):
    """Shared click handler — records the vote in SQLite, mirrors to the
    alliance's `signups_tab`, and acks the user ephemerally. Robust to
    Sheet failures: SQLite is canonical; Sheet failure logs but does not
    roll back.

    Defers before any storage I/O so a slow SQLite write or Sheets API
    call can't blow the 3-second initial-response token. Same pattern
    landed in 1.1.7 for the /train modal hotfix.
    """
    import config
    import premium

    parsed = parse_custom_id(interaction.data.get("custom_id", ""))
    if not parsed:
        # Malformed custom_id — shouldn't happen unless the schema
        # changes mid-flight. Log and bail without crashing the dispatch.
        logger.warning(
            "[STORM SIGNUP] Malformed custom_id on click: %s",
            interaction.data.get("custom_id"),
        )
        try:
            await interaction.response.send_message(
                "⚠️ This sign-up button is from an older version. "
                "Wait for the next sign-up post to vote.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        return

    guild_id   = parsed["guild_id"]
    event_type = parsed["event_type"].upper()
    event_date = parsed["event_date"]
    vote       = parsed["vote"]

    # Sanity check: the click came from this guild. Discord routes by
    # message-id so the parsed guild_id should always match — but a
    # cross-guild leak would silently corrupt votes.
    if interaction.guild_id != guild_id:
        logger.error(
            "[STORM SIGNUP] guild_id mismatch: parsed=%s interaction=%s",
            guild_id, interaction.guild_id,
        )
        try:
            await interaction.response.send_message(
                "⚠️ This sign-up post belongs to a different server. "
                "Please use the sign-up post in your alliance's channel.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        return

    # Defer first — every storage hop below is slower than the 3-second
    # interaction token allows under Railway disk contention or Sheets
    # rate limiting.
    try:
        await interaction.response.defer(ephemeral=True, thinking=False)
    except discord.HTTPException as e:
        logger.warning(
            "[STORM SIGNUP] defer failed (guild=%s vote=%s): %s",
            guild_id, vote, e,
        )
        # If defer fails the interaction is probably already dead;
        # press on anyway so the vote at least lands in SQLite.

    # Defense-in-depth premium gate. The post-creation path (#124) already
    # checks Premium + structured_flow_enabled, but a stale persistent
    # custom_id from a guild that has since downgraded would otherwise
    # silently keep recording votes here.
    if not await premium.is_premium(guild_id, bot=interaction.client):
        try:
            await interaction.followup.send(
                "⚠️ This sign-up post is no longer active because the "
                "structured roster flow has been disabled for this server.",
                ephemeral=True,
            )
        except discord.HTTPException:
            pass
        return

    voter_id      = interaction.user.id
    target_id     = str(voter_id)  # self-vote
    channel_id    = interaction.channel_id or 0
    message_id    = interaction.message.id if interaction.message else 0

    config.record_storm_vote(
        guild_id, event_type, event_date,
        voter_user_id=voter_id,
        target_member_id=target_id,
        vote=vote,
        is_on_behalf=False,
        channel_id=channel_id,
        message_id=message_id,
    )

    # Ephemeral ack BEFORE the Sheet write so the user gets fast feedback
    # even if Sheets is rate-limited or slow.
    label = _VOTE_CONFIRMATIONS.get(vote, vote)
    try:
        await interaction.followup.send(
            f"✅ Vote recorded: **{label}**. You can change your vote any time before the event.",
            ephemeral=True,
        )
    except discord.HTTPException as e:
        logger.warning(
            "[STORM SIGNUP] Failed to send vote ack to user %s in guild %s: %s",
            voter_id, guild_id, e,
        )

    # Sheet mirroring — best-effort, never rolls back SQLite.
    try:
        _mirror_vote_to_sheet(
            guild_id=guild_id,
            event_type=event_type,
            event_date=event_date,
            target_label=interaction.user.display_name,
            voter_id=voter_id,
            vote=vote,
            is_on_behalf=False,
        )
    except Exception as e:
        logger.warning(
            "[STORM SIGNUP] Sheet mirror failed for guild=%s event=%s/%s voter=%s: %s",
            guild_id, event_type, event_date, voter_id,
            config.describe_sheet_error(e),
        )

    # Power-refresh DM nudge (#138). Best-effort — log + move on if
    # anything fails. Cooldown gated on the SQLite table so a re-vote
    # or bot restart can't trigger a second nudge for the same event.
    try:
        await _maybe_send_power_refresh_dm(
            interaction, guild_id, event_type, event_date, voter_id,
        )
    except Exception as e:
        logger.warning(
            "[STORM SIGNUP] power-refresh DM check failed for "
            "guild=%s event=%s/%s voter=%s: %s",
            guild_id, event_type, event_date, voter_id, e,
        )


async def _maybe_send_power_refresh_dm(
    interaction: discord.Interaction,
    guild_id: int,
    event_type: str,
    event_date: str,
    voter_id: int,
) -> None:
    """Send the one-line power-refresh nudge to the voter if:
      * The structured-flow config has `power_refresh_dm_enabled=1`.
      * The voter's row on the alliance roster Sheet has a missing or
        unparseable power value in the configured column.
      * A nudge hasn't already been sent to this voter for this event
        (cooldown via storm_power_refresh_dms_sent).

    Idempotent — the cooldown insert is INSERT OR IGNORE, so two
    near-simultaneous votes can't double-DM.
    """
    import config
    structured = config.get_structured_storm_config(guild_id, event_type)
    if not structured.get("power_refresh_dm_enabled"):
        return

    # Cheap cooldown check before any Sheet read.
    if config.has_power_refresh_dm_been_sent(
        guild_id, event_type, event_date, voter_id,
    ):
        return

    # Read the voter's power. Importing the roster-power reader lazily
    # to avoid pulling storm_roster_builder's full module graph into
    # the persistent-View dispatch path on every click.
    try:
        from storm_roster_builder import _read_roster_powers
    except ImportError:
        return

    guild = interaction.guild
    members, _errors = _read_roster_powers(guild_id, event_type, guild=guild)
    voter_key = str(voter_id)
    voter_row = members.get(voter_key)
    if voter_row is None:
        # Voter isn't on the roster Sheet at all — that's a separate
        # alliance-side cleanup item, not a power-refresh case.
        return
    if voter_row.get("power") is not None:
        # Power is readable; no nudge needed.
        return

    power_column = structured.get("power_column_name") or "your power column"
    body = (
        f"Heads up — your **{power_column}** on the alliance roster "
        f"Sheet isn't readable. Could you update it before the next "
        f"storm so leadership has accurate numbers for zone assignments?"
    )

    user = interaction.user
    try:
        await user.send(body)
    except discord.Forbidden:
        logger.info(
            "[STORM SIGNUP] power-refresh DM blocked by user %s in guild=%s "
            "(DMs disabled). Skipping nudge.",
            voter_id, guild_id,
        )
        # Still record so we don't pile on retries every re-vote.
    except discord.HTTPException as e:
        logger.warning(
            "[STORM SIGNUP] power-refresh DM HTTP error for user=%s guild=%s: %s",
            voter_id, guild_id, e,
        )

    # Always record after attempting — successful AND DM-blocked paths
    # both count toward the cooldown so a member with DMs off doesn't
    # keep hitting the Sheet-read path on every re-vote.
    config.record_power_refresh_dm_sent(
        guild_id, event_type, event_date, voter_id,
    )


def _mirror_vote_to_sheet(
    *,
    guild_id: int,
    event_type: str,
    event_date: str,
    target_label: str,
    voter_id: int,
    vote: str,
    is_on_behalf: bool,
) -> None:
    """Append a vote row to the alliance's configured `signups_tab`.
    No-op if the structured flow is off (no tab to write to) or the
    spreadsheet isn't configured. Raises on Sheet errors; caller handles.
    """
    import datetime as _dt
    import config

    structured = config.get_structured_storm_config(guild_id, event_type)
    if not structured.get("structured_flow_enabled"):
        return  # Free-tier guilds don't have a signups_tab to mirror to.
    tab_name = structured.get("signups_tab")
    if not tab_name:
        return

    sh = config.get_spreadsheet(guild_id)
    if sh is None:
        return  # Guild has no Sheet configured.

    try:
        ws = sh.worksheet(tab_name)
    except Exception:
        # Tab missing → create it with a header row. Alliance can rename
        # the tab later via setup; the bot will follow the renamed config.
        ws = sh.add_worksheet(title=tab_name, rows=1000, cols=6)
        ws.append_row(
            ["Event Date", "Member", "Vote", "Voter Discord ID",
             "On Behalf?", "Voted At (UTC)"],
            value_input_option="RAW",
        )

    voted_at = _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")
    ws.append_row(
        [
            event_date,
            target_label,
            _VOTE_CONFIRMATIONS.get(vote, vote),
            str(voter_id),
            "yes" if is_on_behalf else "no",
            voted_at,
        ],
        value_input_option="RAW",
    )


def register_persistent_signup_views(bot) -> int:
    """Called once at startup (after `on_ready`). Re-attaches a SignupView
    for every registration post within the recent window so buttons keep
    working after a restart. Returns the count of views registered."""
    import config
    posts = config.get_recent_storm_registration_posts()
    registered = 0
    for post in posts:
        view = SignupView(
            post["guild_id"],
            post["event_type"],
            post["event_date"],
            time_a_label=post.get("time_a_label") or "Team A",
            time_b_label=post.get("time_b_label") or "Team B",
        )
        try:
            bot.add_view(view, message_id=int(post["message_id"]))
            registered += 1
        except Exception as e:
            logger.warning(
                "[STORM SIGNUP] Failed to register SignupView for "
                "guild=%s event=%s/%s message=%s: %s",
                post["guild_id"], post["event_type"], post["event_date"],
                post["message_id"], e,
            )
    if registered:
        logger.info("[STORM SIGNUP] Re-registered %d sign-up view(s) on startup", registered)
    return registered
