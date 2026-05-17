"""
seed_dev_roster.py: populate the dev guild's Member Roster Sheet
with synthetic non-Discord members so storm flows have a realistic
roster to exercise against.

The dev Discord server typically has only the bot owner + a handful
of test accounts, which isn't enough to test sign-ups, on-behalf
votes, eligibility gates, attendance, and bucket-map rendering against
a real-shaped roster. This script writes ~50 synthetic members with
varied power tiers, flagged `not_on_discord=Yes` so the bot treats
them as "roster members not on Discord". Every storm surface that
reads the roster will see them; nothing tries to look them up via
Discord's API.

`/sync_members` will **overwrite this data** (it rebuilds the roster
from Discord + the configured roster source). Run this script:
  - On the dev guild only (`--guild-id` must match the dev guild)
  - After `/setup_members` is configured (so we know which Sheet +
    tab to write to)
  - WITHOUT running `/sync_members` afterwards (it'll wipe the synthetic
    rows). If you need both real members + synthetic ones, run sync
    first, then this script (it appends; doesn't clear).

Usage:
  python scripts/seed_dev_roster.py --guild-id 1501975127200501840

Optional flags:
  --count 50           Number of synthetic members to add (default 50).
  --power-tier-spread  Comma-separated 1st-squad power tiers in millions
                       (default "30,50,60,60,70,70,80,80,80,90,90,100,120").
                       Biased toward the realistic alliance-average
                       60-80M band with a few new players plus a few
                       whales. In Last War the highest 1st squads sit
                       around 120M; the alliance average is closer to
                       60M. Tiers can repeat to weight the distribution.
  --clear              Wipe the tab first (keeps the header row).
                       Useful when re-seeding to reset between tests.
  --no-confirm         Skip the "are you sure" prompt.

ENVIRONMENT (same as seed_demo.py):
  CONFIG_DB_PATH              Path to the bot's SQLite.
  GOOGLE_CREDENTIALS_JSON     OR
  GOOGLE_SERVICE_ACCOUNT_FILE Service account creds for sheet writes.

The dev Sheet must already be shared with the service account email.
"""
from __future__ import annotations

import argparse
import random
import sys

import config


_DEFAULT_NAMES = [
    "Astrid", "Bjorn", "Cyra", "Draven", "Elara", "Faelan", "Gwendolyn",
    "Hakon", "Iolanthe", "Jareth", "Kassian", "Lyra", "Magnus", "Niamh",
    "Orion", "Persephone", "Quinlan", "Rhiannon", "Soren", "Talia",
    "Ulrich", "Vesper", "Wolfgang", "Xanthe", "Ysolde", "Zephyr",
    "Aldric", "Brienne", "Caspian", "Daphne", "Elias", "Fenella",
    "Gareth", "Helia", "Ivor", "Junia", "Kael", "Lirien", "Mireille",
    "Nikola", "Octavia", "Phineas", "Quill", "Rowena", "Silas", "Thora",
    "Una", "Varian", "Wren", "Xio",
]


def _build_rows(count: int, power_tiers_m: list[int]) -> list[list[str]]:
    """Build `count` synthetic roster rows in the canonical `/sync_members`
    column order: Discord ID, Name, Display Name, Joined, Roles, Is this
    user in Discord?, then a power column. We leave Discord ID blank
    and set "Is this user in Discord?" to "No" so the bot treats every
    row as a non-Discord roster member.

    Power values are realistic 1st-squad numbers for Last War: the
    highest squads sit around 120M, alliance average is closer to 60M.
    The default tier list (in `main`) repeats 60/70/80 to weight the
    distribution toward that average. Jitter of ±5M keeps each row in
    its tier's neighbourhood without colliding with the next band.
    """
    names = _DEFAULT_NAMES[:count] if count <= len(_DEFAULT_NAMES) else (
        _DEFAULT_NAMES + [f"Stormtest{i:03d}" for i in range(count - len(_DEFAULT_NAMES))]
    )
    rng = random.Random(42)  # deterministic so re-seeds produce the same data
    rows: list[list[str]] = []
    for name in names[:count]:
        tier_m = rng.choice(power_tiers_m)
        # Jitter ±5M so members within a tier aren't identical. This
        # exercises the power-band eligibility paths near tier boundaries.
        jitter_m = rng.randint(-5, 5)
        # Floor at 10M so a low tier + negative jitter can't produce
        # nonsense values (no real player is below ~15M 1st squad).
        power = max(10_000_000, (tier_m + jitter_m) * 1_000_000)
        rows.append([
            "",                    # A: Discord ID (blank = non-Discord)
            name,                  # B: Name
            name,                  # C: Display Name
            "2026-01-01",          # D: Joined (placeholder)
            "Member",              # E: Roles
            "No",                  # F: Is this user in Discord?
            str(power),            # G: Power column (synthetic; letter G default)
        ])
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guild-id", type=int, required=True,
                        help="Dev guild ID")
    parser.add_argument("--count", type=int, default=50,
                        help="Number of synthetic members (default 50)")
    parser.add_argument(
        "--power-tier-spread", type=str,
        default="30,50,60,60,70,70,80,80,80,90,90,100,120",
        help="Comma-separated 1st-squad power tiers in millions "
             "(default biased toward 60-80M with a couple of new "
             "players and a couple of whales)",
    )
    parser.add_argument("--clear", action="store_true",
                        help="Wipe the tab before writing (keeps header row)")
    parser.add_argument("--no-confirm", action="store_true",
                        help="Skip the confirmation prompt")
    args = parser.parse_args()

    try:
        tiers = [int(t.strip()) for t in args.power_tier_spread.split(",") if t.strip()]
    except ValueError:
        print(f"⚠️ Couldn't parse --power-tier-spread {args.power_tier_spread!r}. "
              f"Use comma-separated integers like '60,70,80'.",
              file=sys.stderr)
        return 2

    roster_cfg = config.get_member_roster_config(args.guild_id)
    if not roster_cfg.get("enabled"):
        print(f"⚠️ Guild {args.guild_id} hasn't run /setup_members yet, so "
              f"the bot doesn't know which Sheet/tab to write to. Run "
              f"setup, then re-run this script.",
              file=sys.stderr)
        return 2

    tab_name = roster_cfg.get("tab_name") or "Member Roster"
    if not args.no_confirm:
        print(f"\n  About to write {args.count} synthetic non-Discord rows to:")
        print(f"    guild_id: {args.guild_id}")
        print(f"    tab:      '{tab_name}'")
        print(f"    clear:    {args.clear}")
        print(f"    tiers:    {tiers} (M)")
        print(f"\n  Continue? [y/N] ", end="")
        reply = input().strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            return 0

    ws = config.get_member_roster_sheet(args.guild_id, tab_name)

    if args.clear:
        # Keep the header row; wipe everything below.
        try:
            existing = ws.get_all_values()
            if len(existing) > 1:
                # Delete data rows in one batch
                ws.batch_clear([f"A2:Z{len(existing)}"])
                print(f"Cleared {len(existing) - 1} existing data row(s) "
                      f"from '{tab_name}'.")
        except Exception as e:
            print(f"⚠️ Couldn't clear existing rows: {e}", file=sys.stderr)

    rows = _build_rows(args.count, tiers)
    try:
        ws.append_rows(rows, value_input_option="RAW")
    except Exception as e:
        print(f"⚠️ Sheet write failed: {e}", file=sys.stderr)
        return 1

    print(f"✅ Seeded {len(rows)} synthetic non-Discord roster row(s) into "
          f"'{tab_name}' for guild {args.guild_id}.")
    print(f"\nNext steps:")
    print(f"  - In /setup_desertstorm (or /setup_canyonstorm), set Power")
    print(f"    Metric Column to G (the column this script writes power to).")
    print(f"  - Do NOT run /sync_members on this guild. It'll wipe these")
    print(f"    rows. Re-seed via this script after a sync if you want both")
    print(f"    real members + synthetic ones.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
