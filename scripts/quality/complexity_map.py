"""Complexity map: the measurement behind the `code-complexity` skill.

Runs radon for cyclomatic complexity, walks the AST for nesting depth,
parameter count and branch density (radon reports none of those), pulls
per-file churn from git, and prints the skill's report format with a
suggested order the reader can argue with.

Dev tooling only. Needs radon in the venv; never add it to requirements.txt.

    python scripts/quality/complexity_map.py                 # whole repo
    python scripts/quality/complexity_map.py champion_duel_  # one family (prefix)
    python scripts/quality/complexity_map.py --json out.json # machine-readable too
    python scripts/quality/complexity_map.py --since 2026-07-01

Thresholds are the skill's: act at CC 11, watch at 6; deep at depth 5;
wide at 7 parameters; long at 81 lines. "Dense" (20+ branches per hundred
lines in a function of 100+ lines) is this script's addition, because it
separates a long wizard (many shallow steps) from a tangle.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

EXCLUDE_DIRS = {".venv", "tests", "assets", "scripts", ".git", "node_modules", "__pycache__"}
ACT_CC, WATCH_CC = 11, 6
DEEP, WIDE, LONG = 5, 7, 81
DENSE_PER_100, DENSE_MIN_LINES = 20, 100
BRANCH_NODES = (
    ast.If,
    ast.For,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncFor,
    ast.AsyncWith,
    ast.ExceptHandler,
    ast.IfExp,
    ast.BoolOp,
    ast.comprehension,
    ast.Match,
)
NEST_NODES = (ast.If, ast.For, ast.While, ast.Try, ast.With, ast.AsyncFor, ast.AsyncWith, ast.Match)


def source_files(root: str, prefix: str | None) -> list[str]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/")
            if prefix and not os.path.basename(rel).startswith(prefix):
                continue
            out.append(rel)
    return sorted(out)


def radon_cc(root: str, files: list[str]) -> dict[str, list[dict]]:
    """Cyclomatic complexity per function, keyed by relative path."""
    cmd = [sys.executable, "-m", "radon", "cc", "-j", "--min", "A", *files]
    proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        sys.exit(f"radon failed: {proc.stderr.strip()[:400]}")
    raw = json.loads(proc.stdout)
    return {k.replace("\\", "/"): v for k, v in raw.items()}


class _Shape(ast.NodeVisitor):
    """Depth, parameters, branches and line span for every def in one file."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], dict] = {}
        self._class: list[str] = []

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._class.append(node.name)
        self.generic_visit(node)
        self._class.pop()

    def _visit_def(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        name = f"{self._class[-1]}.{node.name}" if self._class else node.name
        args = node.args
        params = len(args.posonlyargs) + len(args.args) + len(args.kwonlyargs)
        params += bool(args.vararg) + bool(args.kwarg)
        if self._class and params and (args.posonlyargs or args.args):
            params -= 1  # self / cls
        depth, deepest_at = _max_depth(node)
        branches = sum(isinstance(n, BRANCH_NODES) for n in ast.walk(node))
        end = getattr(node, "end_lineno", node.lineno)
        self.rows[(name, node.lineno)] = {
            "depth": depth,
            "deepest_at": deepest_at,
            "params": params,
            "branches": branches,
            "lines": end - node.lineno + 1,
        }
        self.generic_visit(node)

    visit_FunctionDef = _visit_def
    visit_AsyncFunctionDef = _visit_def


def _max_depth(fn: ast.AST) -> tuple[int, int]:
    best, best_line = 0, fn.lineno

    def walk(node: ast.AST, depth: int) -> None:
        nonlocal best, best_line
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # nested defs are measured on their own
            d = depth + 1 if isinstance(child, NEST_NODES) else depth
            if d > best:
                best, best_line = d, getattr(child, "lineno", best_line)
            walk(child, d)

    walk(fn, 0)
    return best, best_line


def shapes(root: str, files: list[str]) -> dict[str, dict[tuple[str, int], dict]]:
    out = {}
    for rel in files:
        with open(os.path.join(root, rel), encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=rel)
        v = _Shape()
        v.visit(tree)
        out[rel] = v.rows
    return out


def churn(root: str, files: list[str], since: str) -> dict[str, tuple[int, str]]:
    """(commits since, last change date) per file, from git."""
    out = {}
    for rel in files:
        proc = subprocess.run(
            ["git", "log", f"--since={since}", "--format=%ad", "--date=short", "--", rel],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        dates = [d for d in proc.stdout.split() if d]
        out[rel] = (len(dates), dates[0] if dates else "")
    return out


def flags_for(row: dict) -> list[str]:
    f = []
    if row["depth"] >= DEEP:
        f.append("deep")
    if row["params"] >= WIDE:
        f.append("wide")
    if row["lines"] >= DENSE_MIN_LINES and 100 * row["branches"] / row["lines"] >= DENSE_PER_100:
        f.append("dense")
    return f or ["sequential"]


def build(root: str, prefix: str | None, since: str) -> dict:
    files = source_files(root, prefix)
    if not files:
        sys.exit("no source files matched")
    cc = radon_cc(root, files)
    shp = shapes(root, files)
    ch = churn(root, files, since)
    rows = []
    for rel in files:
        for item in cc.get(rel, []):
            if item.get("type") not in ("function", "method"):
                continue
            name = f"{item['classname']}.{item['name']}" if item.get("classname") else item["name"]
            s = shp[rel].get((name, item["lineno"])) or shp[rel].get((item["name"], item["lineno"]))
            s = s or {"depth": 0, "deepest_at": item["lineno"], "params": 0, "branches": 0}
            lines = s.get("lines") or (item["endline"] - item["lineno"] + 1)
            row = {
                "file": rel,
                "function": name,
                "line": item["lineno"],
                "cc": item["complexity"],
                "lines": lines,
                "depth": s["depth"],
                "deepest_at": s["deepest_at"],
                "params": s["params"],
                "branches": s["branches"],
                "commits_since": ch[rel][0],
                "last_change": ch[rel][1],
            }
            row["flags"] = flags_for(row)
            rows.append(row)
    act = sorted((r for r in rows if r["cc"] >= ACT_CC), key=lambda r: -r["cc"])
    watch = sorted(
        (r for r in rows if WATCH_CC <= r["cc"] < ACT_CC and r["lines"] >= LONG),
        key=lambda r: -r["lines"],
    )
    per_file = defaultdict(lambda: [0, 0])
    for r in rows:
        per_file[r["file"]][1] += 1
        if r["cc"] >= ACT_CC:
            per_file[r["file"]][0] += 1
    concentration = sorted(
        ((f, over, total, ch[f][0]) for f, (over, total) in per_file.items() if over),
        key=lambda t: -t[1],
    )
    return {
        "since": since,
        "files": len(files),
        "functions": len(rows),
        "avg_cc": round(sum(r["cc"] for r in rows) / len(rows), 2) if rows else 0.0,
        "grades": dict(Counter(_grade(r["cc"]) for r in rows)),
        "act": act,
        "watch": watch,
        "deep": sorted((r for r in rows if r["depth"] >= DEEP), key=lambda r: -r["depth"]),
        "wide": sorted((r for r in rows if r["params"] >= WIDE), key=lambda r: -r["params"]),
        "concentration": concentration,
    }


def _grade(cc: int) -> str:
    for g, hi in (("A", 5), ("B", 10), ("C", 20), ("D", 30), ("E", 40)):
        if cc <= hi:
            return g
    return "F"


def report(m: dict, scope: str, top: int) -> str:
    out = [f"## Complexity map - {scope}", ""]
    out.append(
        f"Files: {m['files']}   Functions measured: {m['functions']}   Average CC: {m['avg_cc']}"
    )
    out.append("Grades: " + "  ".join(f"{g} {m['grades'].get(g, 0)}" for g in "ABCDEF"))
    out.append("")
    out.append(f"### Act (CC {ACT_CC}+), top {top} of {len(m['act'])}")
    for r in m["act"][:top]:
        out.append(
            f"  {r['file']:28} {r['function']:40} CC {r['cc']:3d}  {r['lines']:5d} lines  "
            f"depth {r['depth']:2d}  params {r['params']:2d}  {', '.join(r['flags']):16} "
            f"commits {r['commits_since']:3d}"
        )
    out.append("")
    out.append(f"### Deep (depth {DEEP}+), {len(m['deep'])}")
    for r in m["deep"]:
        out.append(
            f"  {r['file']:28} {r['function']:40} depth {r['depth']:2d} at line "
            f"{r['deepest_at']:5d}  CC {r['cc']:3d}"
        )
    out.append("")
    out.append(f"### Watch (CC {WATCH_CC}-{ACT_CC - 1}, {LONG}+ lines), {len(m['watch'])}")
    for r in m["watch"][:top]:
        out.append(f"  {r['file']:28} {r['function']:40} CC {r['cc']:3d}  {r['lines']:5d} lines")
    out.append("")
    out.append(f"### Wide ({WIDE}+ parameters), {len(m['wide'])}")
    for r in m["wide"]:
        out.append(f"  {r['file']:28} {r['function']:40} params {r['params']:2d}  CC {r['cc']:3d}")
    out.append("")
    out.append(f"### Files where complexity concentrates (commits since {m['since']})")
    for f, over, total, commits in m["concentration"][:15]:
        out.append(f"  {f:28} {over:3d} over the line, out of {total:3d}   commits {commits:3d}")
    out.append("")
    out.append("### Suggested order (a proposal: reorder it, and say why per target)")
    out.append(_suggest(m))
    return "\n".join(out)


def _suggest(m: dict) -> str:
    """Rank by tangle first, churn second, then explain each pick in one line."""
    scored = []
    for r in m["act"]:
        tangle = r["cc"] * (1 + 0.5 * max(0, r["depth"] - 3))
        scored.append((tangle * (1 + r["commits_since"] / 50), r))
    scored.sort(key=lambda t: -t[0])
    lines, seen = [], set()
    for _, r in scored:
        if r["file"] in seen or len(lines) >= 5:
            continue
        seen.add(r["file"])
        why = []
        if r["depth"] >= DEEP:
            why.append(f"nests {r['depth']} deep")
        if r["params"] >= WIDE:
            why.append(f"{r['params']} parameters")
        if r["commits_since"] >= 20:
            why.append(f"{r['commits_since']} commits since {m['since']}")
        if not why:
            why.append(
                "highest CC in a file nobody has touched recently; open it when next editing"
            )
        lines.append(
            f"{len(lines) + 1}. {r['file']}: {r['function']} (CC {r['cc']}) - {'; '.join(why)}"
        )
    return "\n".join(lines) if lines else "  nothing over the line"


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "prefix", nargs="?", help="only files whose name starts with this (e.g. storm_)"
    )
    ap.add_argument(
        "--since", default=(datetime.now(timezone.utc).date() - timedelta(days=70)).isoformat()
    )
    ap.add_argument("--json", metavar="PATH", help="also write the full measurement as JSON")
    ap.add_argument("--top", type=int, default=30)
    a = ap.parse_args()
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    m = build(root, a.prefix, a.since)
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(m, fh, indent=1)
    scope = f"{a.prefix}* " if a.prefix else "whole repo "
    scope += subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True, text=True
    ).stdout.strip()
    print(report(m, scope, a.top))


if __name__ == "__main__":
    main()
