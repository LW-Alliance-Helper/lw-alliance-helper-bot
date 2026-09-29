"""A "where do I find this in the game" guide, as Discord messages.

Shared by every feature that asks an officer for something only the game
shows them: Champion Duel's squad numbers, and the VS league's tags and
warzones (#655). Moved here the day the second one needed it, per the rule in
`CLAUDE.md` (Patterns to reuse), rather than copied.

A guide is a list of steps. Each is one embed, with its annotated screenshot
directly beneath its words: a numbered list is useless if the thing it numbers
is two screens away, and Discord stacks attachments after all the text.

**The instructions are Discord text, not pixels.** Text is selectable,
translatable, resizes with the reader's settings and is read aloud natively;
words burned into a screenshot are none of those things. Each image only says
*where*, and its numbered markers key it to the text.

Alt text rides on the attachment (WCAG 2.2 AA 1.1.1). These images are
entirely instructional, so "annotated screenshot" would convey nothing: each
description says what the markers point at, well enough to follow without
seeing them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import discord


@dataclass(frozen=True)
class GuideStep:
    """One message of a guide: its words, and optionally its screen."""

    title: str
    body: str
    image: str | None = None
    alt: str = ""


def guide_files(image_dir: str, steps) -> list[discord.File]:
    """The annotated screenshots that are deployed, with their alt text.

    A missing image degrades to the words alone rather than failing the
    button: the text carries the answer and the pictures make it fast, which
    is the right way round for something that must not break.
    """
    files = []
    for step in steps:
        if not step.image:
            continue
        path = os.path.join(image_dir, step.image)
        if os.path.isfile(path):
            files.append(discord.File(path, filename=step.image, description=step.alt))
    return files


def build_guide(
    image_dir: str, steps, *, footer: str | None = None
) -> tuple[list[discord.Embed], list[discord.File]]:
    """One embed per step, each with its own image directly beneath its words.

    An embed whose image is missing still renders its instructions, so a
    partial deployment loses the picture and keeps the guide.
    """
    files = guide_files(image_dir, steps)
    present = {file.filename for file in files}

    embeds = []
    for step in steps:
        embed = discord.Embed(
            title=step.title, description=step.body, colour=discord.Colour.blurple()
        )
        if step.image in present:
            embed.set_image(url=f"attachment://{step.image}")
        embeds.append(embed)
    if footer and embeds:
        embeds[-1].set_footer(text=footer)
    return embeds, files
