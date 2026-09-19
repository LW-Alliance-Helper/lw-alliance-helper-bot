"""Dead-code suspects, with the repo-wide reference check already done.

Runs vulture at the only band that finds functions (60), applies the
committed whitelist, then does Step 4's first proof for every hit: count
references to the bare name across the whole repo, including tests/,
scripts/, api/ and .github/, and bucket the hit by what it found.

Output is a suspect list, never a delete list. A name that survives every
bucket still needs the other two proofs by hand: decorator or string
dispatch, and git log for "unfinished, not dead".

Dev tooling only. Needs vulture in the venv; never add it to requirements.txt.

    python scripts/quality/dead_code.py                 # whole repo
    python scripts/quality/dead_code.py champion_duel_  # one family (prefix)
    python scripts/quality/dead_code.py --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
WHITELIST = os.path.join(ROOT, ".vulture-whitelist.py")
FRAMEWORK = {
    "interaction_check",
    "on_timeout",
    "on_submit",
    "on_error",
    "setup",
    "cog_unload",
    "cog_load",
}
SKIP_DIRS = {
    ".venv",
    ".git",
    "node_modules",
    "__pycache__",
    ".ruff_cache",
    ".pytest_cache",
    "assets",
    "notes",
}
TEXT_EXT = (".py", ".yml", ".yaml", ".md", ".toml", ".json", ".txt", ".cfg", ".ini", ".html", ".js")


def vulture(prefix: str | None) -> list[tuple[str, int, str, str]]:
    if prefix:
        targets = sorted(f for f in os.listdir(ROOT) if f.startswith(prefix) and f.endswith(".py"))
        targets += (
            sorted(
                os.path.join("api", f)
                for f in os.listdir(os.path.join(ROOT, "api"))
                if f.startswith(prefix) and f.endswith(".py")
            )
            if os.path.isdir(os.path.join(ROOT, "api"))
            else []
        )
    else:
        targets = ["."]
    cmd = [sys.executable, "-m", "vulture", *targets, WHITELIST, "--min-confidence", "60"]
    if not prefix:
        cmd += ["--exclude", ".venv,tests,assets,scripts,node_modules"]
    proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8")
    hits = []
    for line in proc.stdout.splitlines():
        m = re.match(r"(.+?):(\d+): unused (\w+) '(\w+)'", line)
        if m:
            hits.append((m.group(1).replace("\\", "/"), int(m.group(2)), m.group(3), m.group(4)))
    return hits


def index_repo() -> dict[str, list[str]]:
    files = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(TEXT_EXT):
                p = os.path.join(dirpath, fn)
                rel = os.path.relpath(p, ROOT).replace("\\", "/")
                try:
                    with open(p, encoding="utf-8", errors="replace") as fh:
                        files[rel] = fh.read().splitlines()
                except OSError:
                    pass
    return files


def bucket(hit, files) -> dict:
    f, ln, kind, name = hit
    if name in FRAMEWORK:
        return {"bucket": "framework", "where": {}}
    pat = re.compile(rf"\b{re.escape(name)}\b")
    where = Counter()
    for rel, lines in files.items():
        for i, line in enumerate(lines, 1):
            if rel == f and i == ln:
                continue
            if pat.search(line):
                where[
                    "tests"
                    if rel.startswith("tests/")
                    else ("same file" if rel == f else "elsewhere")
                ] += 1
    if where["elsewhere"]:
        b = "referenced elsewhere"
    elif where["tests"] and not where["same file"]:
        b = "tests only"
    elif where["same file"]:
        b = "same file only"
    else:
        b = "no reference anywhere"
    return {"bucket": b, "where": dict(where)}


def run(prefix: str | None) -> dict:
    hits = vulture(prefix)
    files = index_repo()
    rows = []
    for h in hits:
        b = bucket(h, files)
        rows.append({"file": h[0], "line": h[1], "kind": h[2], "name": h[3], **b})
    return {"scope": prefix or "whole repo", "rows": rows}


def report(m: dict) -> str:
    rows = m["rows"]
    out = [f"## Dead code suspects - {m['scope']}", ""]
    out.append(f"vulture hits at 60 after the whitelist: {len(rows)}")
    c = Counter(r["bucket"] for r in rows)
    for b in (
        "no reference anywhere",
        "tests only",
        "same file only",
        "referenced elsewhere",
        "framework",
    ):
        out.append(f"  {c[b]:4d}  {b}")
    out.append("")
    out.append("### No reference anywhere (candidates; still check dispatch and git log)")
    for r in rows:
        if r["bucket"] == "no reference anywhere":
            out.append(f"  {r['file']}:{r['line']:<6} {r['kind']:9} {r['name']}")
    out.append("")
    out.append(
        "### Tests only (a tested helper whose surface was never built, or a caller not found)"
    )
    for r in rows:
        if r["bucket"] == "tests only":
            out.append(
                f"  {r['file']}:{r['line']:<6} {r['kind']:9} {r['name']:30} tests {r['where'].get('tests', 0)}"
            )
    out.append("")
    out.append("### Same file only (comments, docstrings, or a write with no read)")
    for r in rows:
        if r["bucket"] == "same file only":
            out.append(f"  {r['file']}:{r['line']:<6} {r['kind']:9} {r['name']}")
    out.append("")
    out.append("### Referenced elsewhere (a family scan cannot see its callers; whitelist these)")
    for r in rows:
        if r["bucket"] == "referenced elsewhere":
            out.append(
                f"  {r['file']}:{r['line']:<6} {r['kind']:9} {r['name']:30} elsewhere {r['where'].get('elsewhere', 0)}"
            )
    out.append("")
    out.append("### Framework-dispatched by name (whitelist these)")
    fw = Counter(r["name"] for r in rows if r["bucket"] == "framework")
    for n, k in fw.most_common():
        out.append(f"  {k:4d}  {n}")
    out.append("")
    out.append(
        "Short names (W, H, keep, due) inflate the counts above; read the lines before trusting a bucket. "
        "Removal is its own branch, reviewed as a diff, with both test lanes run."
    )
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "prefix", nargs="?", help="only files whose name starts with this (e.g. storm_)"
    )
    ap.add_argument("--json", metavar="PATH")
    a = ap.parse_args()
    if not os.path.exists(WHITELIST):
        sys.exit("no .vulture-whitelist.py at the repo root; the skill says to build it first")
    m = run(a.prefix)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(m, fh, indent=1)
    print(report(m))


if __name__ == "__main__":
    main()
