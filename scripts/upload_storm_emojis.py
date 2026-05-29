"""
One-shot upload of storm zone icons to Discord Application Emojis (#158 + #177).

Discord caps application emojis at 256 KB. Several PNGs in
`assets/storm_icons/` ship above that, so this script resizes each to
a target side-length before upload until it's under the cap.

Run once per environment (prod + dev have separate Application IDs and
need their own upload). The bot's existing application emojis are
fetched first and re-used by name — re-running is idempotent.

Usage:
    DISCORD_TOKEN=<bot token> py scripts/upload_storm_emojis.py

Output: the script prints `{name: id}` mappings for verification, then
exits. **No source edit needed** — `storm_icons.refresh_zone_emoji_ids`
reads the IDs directly from Discord at `on_ready`, so each environment
resolves its own emoji set from the token its bot ships with. Restart
the bot (or wait for the next reconnect) to pick up the new icons.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import pathlib
import sys

import discord

logger = logging.getLogger(__name__)

# Discord's per-emoji byte cap.
_MAX_EMOJI_BYTES = 256 * 1024
# Target square side length for resize. Discord renders inline at
# ~48x48 anyway; 128 keeps the icon crisp without going over the cap.
_RESIZE_TARGET_PX = 128


def _stem_from_filename(path: pathlib.Path) -> str:
    """`Field Hospital.png` → `field_hospital` (matches the lookup
    helper's `_zone_stem` output for the unnumbered base form)."""
    name = path.stem.strip().lower()
    return name.replace(" ", "_")


def _load_and_resize(path: pathlib.Path) -> bytes:
    """Read the file, resize if it's above Discord's cap."""
    raw = path.read_bytes()
    if len(raw) <= _MAX_EMOJI_BYTES:
        return raw
    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError(
            f"{path.name} is {len(raw)} bytes (cap is {_MAX_EMOJI_BYTES}); "
            f"install Pillow to enable auto-resize or shrink the asset "
            f"manually."
        )
    img = Image.open(io.BytesIO(raw))
    img.thumbnail((_RESIZE_TARGET_PX, _RESIZE_TARGET_PX), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    resized = out.getvalue()
    print(f"[resize] {path.name}: {len(raw)} → {len(resized)} bytes ({img.size[0]}x{img.size[1]})")
    if len(resized) > _MAX_EMOJI_BYTES:
        raise RuntimeError(
            f"{path.name}: even after resize the image is {len(resized)} "
            f"bytes (cap is {_MAX_EMOJI_BYTES}). Shrink the source asset."
        )
    return resized


async def _upload_all(token: str) -> dict[str, int]:
    """Connect to Discord just long enough to enumerate + upload the
    app emojis. Doesn't actually start the gateway connection — emoji
    operations live on the HTTP client only."""
    intents = discord.Intents.none()
    client = discord.Client(intents=intents)
    await client.login(token)
    try:
        existing = await client.fetch_application_emojis()
        existing_by_name = {e.name: e.id for e in existing}
        print(f"[init] {len(existing_by_name)} application emojis already present")

        repo_root = pathlib.Path(__file__).resolve().parent.parent
        asset_dirs = [
            repo_root / "assets" / "storm_icons" / "ds",
            repo_root / "assets" / "storm_icons" / "cs",
        ]
        result: dict[str, int] = {}
        for directory in asset_dirs:
            if not directory.exists():
                print(f"[skip] {directory} doesn't exist")
                continue
            for path in sorted(directory.glob("*.png")):
                name = _stem_from_filename(path)
                if name in existing_by_name:
                    result[name] = existing_by_name[name]
                    print(f"[skip] {name} already uploaded (id={result[name]})")
                    continue
                try:
                    payload = _load_and_resize(path)
                    emoji = await client.create_application_emoji(
                        name=name,
                        image=payload,
                    )
                except Exception as e:
                    print(f"[FAIL] {name}: {e}")
                    continue
                result[name] = emoji.id
                print(f"[ok]   {name} → {emoji.id}")
        return result
    finally:
        await client.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    token = os.environ.get("DISCORD_TOKEN") or os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("ERROR: set DISCORD_TOKEN (or DISCORD_BOT_TOKEN) env var first.")
        return 1
    mapping = asyncio.run(_upload_all(token))
    print()
    print(f"[done] {len(mapping)} application emoji(s) registered for this bot.")
    print("The bot reads these IDs from Discord at on_ready via")
    print("storm_icons.refresh_zone_emoji_ids — no source edit required.")
    print("Restart the bot (or wait for the next reconnect) to pick them up.")
    print()
    print("Mappings (for verification):")
    for name in sorted(mapping):
        print(f"  {name:<22} → {mapping[name]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
