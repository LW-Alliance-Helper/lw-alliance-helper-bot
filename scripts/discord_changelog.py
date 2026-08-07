#!/usr/bin/env python3
"""Check that a release has a Discord changelog block before it ships.

`release-changelog-check.yml` runs this on the PR into main. Every
release posts to #changelog (#92), so a version with no block is a
forgotten post, and failing the release PR is the only place that can be
caught before the release is out.

The posting itself is done by the bot on boot, not from CI — see
`changelog_post.py` for why. The matching logic lives there too; this is
a thin CLI over it so the gate and the bot can never disagree about what
a block is.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from changelog_post import (  # noqa: E402
    DISCORD_CONTENT_LIMIT,
    NO_POST_MARKER,
    find_block,
    is_no_post,
)

DEFAULT_PATH = Path("docs/DISCORD_CHANGELOG.md")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="version being released, e.g. 1.8.4")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    parser.add_argument(
        "--limit",
        type=int,
        default=DISCORD_CONTENT_LIMIT,
        help="max characters Discord will accept",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "validate that this version has a block (or an explicit "
            f"'{NO_POST_MARKER}') and exit non-zero if not. For the release PR."
        ),
    )
    args = parser.parse_args(argv)

    try:
        content = args.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"{args.path} not found", file=sys.stderr)
        return 1 if args.check else 0

    block = find_block(content, args.version)

    if args.check:
        if block is None:
            print(
                f"No Discord changelog block for {args.version} in {args.path}.\n"
                f"\n"
                f"Every release posts to #changelog. Add a block:\n"
                f"\n"
                f"    **{args.version}** — YYYY-MM-DD\n"
                f"    - what changed, one line each, max 5\n"
                f"\n"
                f"or opt this release out explicitly:\n"
                f"\n"
                f"    **{args.version}** — YYYY-MM-DD\n"
                f"    {NO_POST_MARKER}: nothing alliance-facing\n"
                f"\n"
                f"See the preamble in {args.path} for how to write one.",
                file=sys.stderr,
            )
            return 1
        if not is_no_post(block) and len(block) > args.limit:
            print(
                f"Block for {args.version} is {len(block)} chars, over Discord's "
                f"{args.limit} limit. Trim it.",
                file=sys.stderr,
            )
            return 1
        state = NO_POST_MARKER if is_no_post(block) else "will post"
        print(f"{args.version}: {state}")
        return 0

    # Without --check this just prints the block, for eyeballing what a
    # release will say before it ships. Same guards as the bot applies, so
    # the preview can't show something that wouldn't actually post.
    if block is not None and not is_no_post(block) and len(block) > args.limit:
        print(
            f"Block for {args.version} is {len(block)} chars, over the {args.limit} limit",
            file=sys.stderr,
        )
        return 0

    if block is None:
        print(f"No block for {args.version} in {args.path}", file=sys.stderr)
        return 0

    if is_no_post(block):
        print(f"{args.version} is marked {NO_POST_MARKER} — nothing to send", file=sys.stderr)
        return 0

    print(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
