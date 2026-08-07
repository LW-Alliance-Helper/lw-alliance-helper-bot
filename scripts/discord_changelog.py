#!/usr/bin/env python3
"""Pull one release's post out of docs/DISCORD_CHANGELOG.md.

`release-on-main.yml` calls this after creating the GitHub Release and
pipes the result to the #changelog webhook (#92). The post is written by
hand on the release branch rather than derived from CHANGELOG.md, because
the two are not the same artifact: the Discord post merges related
bullets, drops anything an alliance can't act on, and caps at five lines.
None of that is a transformation a script can do.

Prints the block to stdout and exits 0. A missing file, a missing block,
or an over-long block all print nothing and still exit 0 — the caller
treats an empty result as "say nothing", and this must never fail the
release workflow (see the note in release-on-main.yml about Railway
blocking deploys on a red job).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

DEFAULT_PATH = Path("docs/DISCORD_CHANGELOG.md")

# Discord hard-caps webhook `content` at 2000 characters.
DISCORD_CONTENT_LIMIT = 2000

# Marker that opts a release out of posting. Every release posts unless
# its block says this, so forgetting to write one is caught by
# `--check` on the release PR rather than discovered as a quiet channel
# weeks later.
NO_POST_MARKER = "NO POST"


def find_block(content: str, version: str) -> str | None:
    """Return the post whose header names `version`, or None.

    Blocks are separated by a bare `---`. The file's preamble is the first
    block and doesn't start with `**`, so it's skipped along with anything
    else that isn't a version header.

    A range header (`**1.7.1 to 1.7.4** — 2026-07-21`) matches every
    version it names, which is what lets a quiet release be covered
    later by the next one.
    """
    if not version:
        return None
    for block in re.split(r"^---\s*$", content, flags=re.M):
        block = block.strip()
        if not block or not block.startswith("**"):
            continue
        header = block.splitlines()[0]
        # Guard against 1.8.1 matching inside 1.8.10.
        if re.search(rf"(?<![\d.]){re.escape(version)}(?![\d.])", header):
            return block
    return None


def is_no_post(block: str) -> bool:
    """True when a block exists purely to record "this one doesn't post".

    Keeping the version in the file with a reason is better than leaving
    a hole, because a hole is indistinguishable from having forgotten.
    """
    return any(
        line.strip().upper().startswith(NO_POST_MARKER) for line in (block or "").splitlines()[1:]
    )


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

    if block is None:
        print(f"No block for {args.version} in {args.path}", file=sys.stderr)
        return 0

    if is_no_post(block):
        print(f"{args.version} is marked {NO_POST_MARKER} — nothing to send", file=sys.stderr)
        return 0

    if len(block) > args.limit:
        print(
            f"Block for {args.version} is {len(block)} chars, over the {args.limit} limit",
            file=sys.stderr,
        )
        return 0

    print(block)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
