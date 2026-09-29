"""Where to find a new league's details in the game (#655).

Starting a league takes every alliance's tag and warzone, and the warzone is
three taps away from the League screen, on any member's profile. The paste box
cannot hold that explanation (a field description holds 100 characters), so
this guide sits one button away: on the `/vs` hub beside ➕ Start a new league,
and on the message that refuses a paste, which is where somebody who is stuck
actually is ("nobody reads documentation", `UX.md`).

Built like Champion Duel's capture guide, through the same `game_guide`:
annotated game screens with numbered markers, the words as Discord text. The
screens are the real game, unredacted (Kevin, 29 Sep). The examples we write
are invented, from `examples`.

Discord-first by decision (29 Sep). The website's guides repository (#656)
reuses these screens and words later, so the two cannot drift apart.
"""

from __future__ import annotations

import os

import discord

import examples
import game_guide

_GUIDE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", "alliance_duel")

# Drafts, for sign-off.
VS_BTN_LEAGUE_GUIDE = "📖 Where to find each alliance's details"

_EXAMPLE_LINE = f"<{examples.TAG}> #{examples.WARZONE} 26,800,000,000 Lv.25 95/100"
_AI_REQUEST = (
    "List every alliance in these screenshots, one per line, in ranking order: tag, "
    "warzone, power, gift level, members. Copy each value exactly as shown."
)

GUIDE_STEPS = (
    game_guide.GuideStep(
        title="1. The League screen",
        body=(
            "In the game, open **Alliance Duel** → **Duel League**.\n"
            "1. The season.\n"
            "2. The tier and group.\n"
            "3. Every alliance's tag, in ranking order. Scroll to see all 16."
        ),
        image="vs_guide_league.png",
        alt=(
            "The Duel League screen. Marker 1 is on the heading Alliance Duel League S37, "
            "marker 2 on Diamond Tier 12 - 4, and marker 3 outlines the ranking list, "
            "where each row has a rank number, a tag in brackets and an alliance name."
        ),
    ),
    game_guide.GuideStep(
        title="2. An alliance's page",
        body=(
            "Tap an alliance in the list.\n"
            "1. Its tag.\n"
            "2. Power, gift level and members. These are optional, and you can add them "
            "later.\n"
            "3. Tap **Members** to find its warzone."
        ),
        image="vs_guide_alliance.png",
        alt=(
            "An alliance's page. Marker 1 is on the tag and name at the top, marker 2 "
            "outlines the Alliance Power, Leader, Alliance Gifts and Ppl rows, and marker "
            "3 is on the Members button."
        ),
    ),
    game_guide.GuideStep(
        title="3. The member list",
        body="1. Tap any member. The leader at the top is the easiest to find.",
        image="vs_guide_members.png",
        alt="The member list. Marker 1 is on the leader's portrait at the top.",
    ),
    game_guide.GuideStep(
        title="4. The warzone",
        body=(
            "1. The number after **#** is the alliance's warzone.\n"
            "Go back and repeat steps 2 to 4 for each alliance."
        ),
        image="vs_guide_profile.png",
        alt=(
            "A player's profile. Marker 1 is on the box showing the warzone: a # "
            "followed by a number."
        ),
    ),
    game_guide.GuideStep(
        title="What to type",
        body=(
            "One alliance per line, in ranking order, as the game shows it:\n"
            f"`{_EXAMPLE_LINE}`\n"
            f"Only the tag and warzone are required: `{examples.TAG} {examples.WARZONE}`."
        ),
    ),
    game_guide.GuideStep(
        title="Shortcut: let an AI tool type it",
        body=(
            "Give your screenshots to any AI tool that reads images, with this request:\n"
            f"```\n{_AI_REQUEST}\n```\n"
            "Check each tag against the League screen before you paste. Look-alike letters, "
            "like l and I, make one alliance look like two."
        ),
    ),
    game_guide.GuideStep(
        title="Then",
        body=(
            "Press **➕ Start a new league**, enter the season, tier and group from step 1, "
            "and paste your list."
        ),
    ),
)


def build_guide() -> tuple[list[discord.Embed], list[discord.File]]:
    """The guide's messages and screens. Reads `_GUIDE_DIR` at call time."""
    return game_guide.build_guide(_GUIDE_DIR, GUIDE_STEPS)


async def send_guide(interaction: discord.Interaction) -> None:
    """Post the guide to whoever asked, privately.

    Never locked: documentation is not a paid surface, the same call Champion
    Duel's guide makes.
    """
    embeds, files = build_guide()
    await interaction.response.send_message(embeds=embeds, files=files, ephemeral=True)
