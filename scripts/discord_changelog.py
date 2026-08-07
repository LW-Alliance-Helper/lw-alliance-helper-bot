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
    args = parser.parse_args(argv)

    try:
        content = args.path.read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"{args.path} not found", file=sys.stderr)
        return 0

    block = find_block(content, args.version)
    if block is None:
        print(f"No block for {args.version} in {args.path}", file=sys.stderr)
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
