"""Render sample DS + CS PNGs to /c/tmp/ for visual review of the
#227 layout redesign.

Usage:
    py scripts/render_layout_preview.py

Writes:
    /c/tmp/ds_layout_preview.png
    /c/tmp/cs_layout_preview.png

Each fixture exercises the cases from the alliance lead's design
review: a worst-case-long-name zone, an all-empty zone, a closed
(cap=0) zone, a 1-name-only zone, plus a typical mid-fill zone for
contrast. The script imports storm_renderer directly so any local
edits to the layout engine show up immediately on next run."""
from __future__ import annotations

import os
import pathlib
import sys

# Make the bot module importable when running this script from the
# repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storm_renderer as sr  # noqa: E402


_LONG = "20Char-55666777888999"
_OUT_DIR = pathlib.Path(r"C:\Users\Kevin\Downloads\ds-cs icons")


def _ds_roster() -> sr.RosterData:
    """A DS roster covering every visual edge case in one image:

    - Info Center  : 1 member each stage (tiny pill, breathing room)
    - Oil Refinery I : 4 names Stage 1, 2 names Stage 2 (typical)
    - Field Hospital I : full both stages (typical)
    - Field Hospital II : all-empty (both stages, render `(empty)`)
    - Field Hospital IV : 1 long-name member (long-row absorption)
    - Field Hospital III : empty Stage 1, 1 member Stage 2
    - Arsenal  : 1 member Stage 2 (best-case central)
    - Nuclear Silo : worst case — long name + 14 short names across
                     3-col grid (tests row cap)
    - Oil Refinery II : worst case in outer 2-col grid (long name +
                        many short)
    - Science Hub : worst case in outer 2-col grid (right column)
    - Mercenary Factory : all-empty (central, renders `(empty)`)
    """
    z = sr.RosterZone
    zones = [
        # Info Center
        z(name="Stage 1 — Info Center", canonical_zone="Info Center",
          max_players=4, members=["Jon"], phase=1),
        z(name="Stage 2 — Info Center", canonical_zone="Info Center",
          max_players=4, members=["Mandingo0"], phase=2),
        # Oil Refinery I
        z(name="Stage 1 — Oil Refinery I", canonical_zone="Oil Refinery I",
          max_players=4, members=["Member 1", "Member 2", "Member 3", "Member 4"], phase=1),
        z(name="Stage 2 — Oil Refinery I", canonical_zone="Oil Refinery I",
          max_players=4, members=["Member 1", "Member 2"], phase=2),
        # Field Hospital I
        z(name="Stage 1 — Field Hospital I", canonical_zone="Field Hospital I",
          max_players=4, members=["Member 1", "Member 2", "Member 3", "Member 4"], phase=1),
        z(name="Stage 2 — Field Hospital I", canonical_zone="Field Hospital I",
          max_players=4, members=["Member 1", "Member 2"], phase=2),
        # Field Hospital II — all-empty across both stages
        z(name="Stage 1 — Field Hospital II", canonical_zone="Field Hospital II",
          max_players=4, members=[], phase=1),
        z(name="Stage 2 — Field Hospital II", canonical_zone="Field Hospital II",
          max_players=4, members=[], phase=2),
        # Arsenal — best-case central (1 member Stage 2 only)
        z(name="Stage 1 — Arsenal", canonical_zone="Arsenal",
          max_players=0, members=[], phase=1),
        z(name="Stage 2 — Arsenal", canonical_zone="Arsenal",
          max_players=3, members=["Member 1"], phase=2),
        # Nuclear Silo — worst case: long name + 14 short names
        z(name="Stage 1 — Nuclear Silo", canonical_zone="Nuclear Silo",
          max_players=0, members=[], phase=1),
        z(name="Stage 2 — Nuclear Silo", canonical_zone="Nuclear Silo",
          max_players=21, members=[
              _LONG,
              "Member 2", "Member 9", "Member 16",
              "Member 3", "Member 10", "Member 17",
              "Member 4", "Member 11", "Member 18",
              "Member 5", "Member 12", "Member 19",
              "Member 6", "Member 13", "Member 20",
              "Member 7", "Member 14", "Member 21",
          ], phase=2),
        # Mercenary Factory — all-empty central
        z(name="Stage 1 — Mercenary Factory", canonical_zone="Mercenary Factory",
          max_players=0, members=[], phase=1),
        z(name="Stage 2 — Mercenary Factory", canonical_zone="Mercenary Factory",
          max_players=3, members=[], phase=2),
        # Field Hospital IV — 1 long-name member
        z(name="Stage 1 — Field Hospital IV", canonical_zone="Field Hospital IV",
          max_players=2, members=["Member 1"], phase=1),
        z(name="Stage 2 — Field Hospital IV", canonical_zone="Field Hospital IV",
          max_players=2, members=[], phase=2),
        # Field Hospital III — empty Stage 1, 1 member Stage 2
        z(name="Stage 1 — Field Hospital III", canonical_zone="Field Hospital III",
          max_players=2, members=[], phase=1),
        z(name="Stage 2 — Field Hospital III", canonical_zone="Field Hospital III",
          max_players=2, members=["Member 1"], phase=2),
        # Oil Refinery II — worst case in outer 2-col grid
        z(name="Stage 1 — Oil Refinery II", canonical_zone="Oil Refinery II",
          max_players=0, members=[], phase=1),
        z(name="Stage 2 — Oil Refinery II", canonical_zone="Oil Refinery II",
          max_players=14, members=[
              _LONG,
              "Member 2", "Member 10",
              "Member 3", "Member 11",
              "Member 4", "Member 12",
              "Member 5", "Member 13",
              "Member 6", "Member 14",
              "Member 7",
              "Member 8",
          ], phase=2),
        # Science Hub — worst case (right column outer)
        z(name="Stage 1 — Science Hub", canonical_zone="Science Hub",
          max_players=0, members=[], phase=1),
        z(name="Stage 2 — Science Hub", canonical_zone="Science Hub",
          max_players=14, members=[
              _LONG,
              "Member 2", "Member 10",
              "Member 3", "Member 11",
              "Member 4", "Member 12",
              "Member 5", "Member 13",
              "Member 6", "Member 14",
              "Member 7",
              "Member 8",
          ], phase=2),
    ]
    subs = ["Member 1", "Member 2", "Member 3", "Member 4", "Member 1",
            "Member 2", "Member 3", "Member 4", "Member 1", "Member 2"]
    paired_subs = {f"Member {i}": f"Member {i + 20}" for i in range(1, 11)}
    return sr.RosterData(
        title="Desert Storm — Team A — Friday, May 22, 2026",
        zones=zones,
        subs=subs,
        paired_subs=paired_subs,
        event_type="DS",
        team_label="Team A",
        event_date_label="Friday, May 22, 2026",
        phase_count=2,
    )


def _cs_roster() -> sr.RosterData:
    """A CS roster exercising the all-outer 2-col layout. CS has no
    central column, so every zone follows the outer-2-col rule per
    the alliance lead's spec. Includes every canonical CS zone so the
    preview shows full coverage."""
    z = sr.RosterZone
    # CS has phase-aware zones with up to 3 stages. We render Stage 1
    # + Stage 2 across the layout to keep the visual close to the DS
    # comparison. Virus Lab is Stage 3 only.
    zones = [
        # Data Center 1 — typical
        z(name="Stage 1 — Data Center 1", canonical_zone="Data Center 1",
          max_players=4, members=["Member 1", "Member 2", "Member 3", "Member 4"], phase=1),
        z(name="Stage 2 — Data Center 1", canonical_zone="Data Center 1",
          max_players=4, members=["Member 1", "Member 2"], phase=2),
        # Data Center 2 — typical + 1 long name
        z(name="Stage 1 — Data Center 2", canonical_zone="Data Center 2",
          max_players=4, members=[_LONG, "Member 2", "Member 3"], phase=1),
        z(name="Stage 2 — Data Center 2", canonical_zone="Data Center 2",
          max_players=4, members=["Member 1"], phase=2),
        # Serum Factory 1 — empty
        z(name="Stage 1 — Serum Factory 1", canonical_zone="Serum Factory 1",
          max_players=4, members=[], phase=1),
        z(name="Stage 2 — Serum Factory 1", canonical_zone="Serum Factory 1",
          max_players=4, members=[], phase=2),
        # Serum Factory 2 — moderate fill
        z(name="Stage 1 — Serum Factory 2", canonical_zone="Serum Factory 2",
          max_players=4, members=["Member 1", "Member 2"], phase=1),
        z(name="Stage 2 — Serum Factory 2", canonical_zone="Serum Factory 2",
          max_players=4, members=["Member 1", "Member 2", "Member 3"], phase=2),
        # Defense System 1 — short stage 1, full stage 2
        z(name="Stage 1 — Defense System 1", canonical_zone="Defense System 1",
          max_players=4, members=["Member 1"], phase=1),
        z(name="Stage 2 — Defense System 1", canonical_zone="Defense System 1",
          max_players=4, members=["Member 1", "Member 2", "Member 3", "Member 4"], phase=2),
        # Defense System 2 — moderate fill
        z(name="Stage 1 — Defense System 2", canonical_zone="Defense System 2",
          max_players=4, members=["Member 1", "Member 2"], phase=1),
        z(name="Stage 2 — Defense System 2", canonical_zone="Defense System 2",
          max_players=4, members=["Member 1", "Member 2"], phase=2),
        # Sample Warehouse 1 / 2 / 3 / 4 — basic across all four
        z(name="Stage 1 — Sample Warehouse 1", canonical_zone="Sample Warehouse 1",
          max_players=4, members=["Member 1", "Member 2"], phase=1),
        z(name="Stage 2 — Sample Warehouse 1", canonical_zone="Sample Warehouse 1",
          max_players=4, members=["Member 1"], phase=2),
        z(name="Stage 1 — Sample Warehouse 2", canonical_zone="Sample Warehouse 2",
          max_players=4, members=["Member 1"], phase=1),
        z(name="Stage 2 — Sample Warehouse 2", canonical_zone="Sample Warehouse 2",
          max_players=4, members=["Member 1", "Member 2"], phase=2),
        z(name="Stage 1 — Sample Warehouse 3", canonical_zone="Sample Warehouse 3",
          max_players=4, members=["Member 1", "Member 2", "Member 3"], phase=1),
        z(name="Stage 2 — Sample Warehouse 3", canonical_zone="Sample Warehouse 3",
          max_players=4, members=["Member 1"], phase=2),
        z(name="Stage 1 — Sample Warehouse 4", canonical_zone="Sample Warehouse 4",
          max_players=4, members=["Member 1"], phase=1),
        z(name="Stage 2 — Sample Warehouse 4", canonical_zone="Sample Warehouse 4",
          max_players=4, members=["Member 1", "Member 2"], phase=2),
        # Power Tower — moderate
        z(name="Stage 1 — Power Tower", canonical_zone="Power Tower",
          max_players=4, members=["Member 1", "Member 2", "Member 3"], phase=1),
        z(name="Stage 2 — Power Tower", canonical_zone="Power Tower",
          max_players=4, members=["Member 1", "Member 2", "Member 3", "Member 4"], phase=2),
        # Virus Lab — Stage 3 only (CS Phase 3 zone)
        z(name="Stage 3 — Virus Lab", canonical_zone="Virus Lab",
          max_players=4, members=["Member 1", "Member 2"], phase=3),
    ]
    subs = [f"Member {i}" for i in range(1, 11)]
    paired_subs = {f"Member {i}": f"Member {i + 20}" for i in range(1, 11)}
    return sr.RosterData(
        title="Canyon Storm — Rulebringers — Friday, May 22, 2026",
        zones=zones,
        subs=subs,
        paired_subs=paired_subs,
        event_type="CS",
        team_label="Rulebringers",
        event_date_label="Friday, May 22, 2026",
        phase_count=2,
    )


def main() -> int:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)

    for name, roster in [
        ("ds_layout_preview.png", _ds_roster()),
        ("cs_layout_preview.png", _cs_roster()),
    ]:
        png = sr.render(roster)
        path = _OUT_DIR / name
        path.write_bytes(png)
        size_kb = len(png) // 1024
        print(f"wrote {path} ({size_kb} KB)")
        if roster.overflow:
            print(f"  overflow ({len(roster.overflow)}):")
            for entry in roster.overflow:
                print(f"    {entry.canonical_zone} Stage {entry.phase}: {entry.name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
