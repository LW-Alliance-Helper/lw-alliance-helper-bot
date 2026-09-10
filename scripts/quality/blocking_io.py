"""Blocking I/O inside coroutines: the check `ruff.toml` says needs a human.

ruff's ASYNC rules only know stdlib blocking calls. This finds the bot's own:
gspread and sqlite, both directly (a `conn.execute` inside an `async def`)
and indirectly (a coroutine calling `config.get_config(...)`, a sync helper
that opens a connection, without `asyncio.to_thread`).

Two-step, because the indirect class needs a pattern per helper:
  1. ast-grep/sync-db-fns.yml lists every sync function that opens a
     connection or a spreadsheet.
  2. That list becomes a generated rule (same shape as
     ast-grep/blocking-in-async.yml) and runs repo-wide.

Dev tooling only. Needs node (ast-grep runs through npx).

    python scripts/quality/blocking_io.py            # report
    python scripts/quality/blocking_io.py --json out.json

Read the direct hits as questions, not defects: an owner-only admin command
doing one indexed read is the same class as a storm reminder opening a
spreadsheet, and only one of those stalls every alliance. The indirect
sqlite class is a convention question (#589) until measured; the indirect
sheet class is the one to fix on sight.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
RULES = os.path.join(HERE, "ast-grep")
AG = ["npx", "--yes", "--package", "@ast-grep/cli", "ast-grep"]
SKIP = ("tests/", ".venv/", "scripts/", "node_modules/")

UTILS = """utils:
  in-async-fn:
    inside:
      pattern: "async def $F($$$P): $$$B"
      stopBy: { kind: function_definition }
  not-handed-off:
    not:
      inside:
        any:
          - pattern: "asyncio.to_thread($$$A)"
          - pattern: "$L.run_in_executor($$$A)"
          - pattern: "functools.partial($$$A)"
          - pattern: "partial($$$A)"
        stopBy: end
"""


def scan(rule_path: str) -> list[dict]:
    proc = subprocess.run(
        [*AG, "scan", "-r", rule_path, "--json=compact", "."],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        shell=(os.name == "nt"),
    )
    if proc.returncode not in (0, 1):  # 1 = matches found under some configs
        sys.exit(f"ast-grep failed on {os.path.basename(rule_path)}: {proc.stderr.strip()[:400]}")
    try:
        hits = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        sys.exit(
            f"ast-grep produced no JSON for {os.path.basename(rule_path)}: {proc.stderr[:300]}"
        )
    out = []
    for h in hits:
        f = h["file"].replace("\\", "/")
        if f.startswith(SKIP):
            continue
        h["file"] = f
        out.append(h)
    return out


def sync_helpers() -> dict[str, str]:
    """name -> 'sheet' | 'sqlite' for every sync helper that opens I/O."""
    kind: dict[str, str] = {}
    for h in scan(os.path.join(RULES, "sync-db-fns.yml")):
        m = re.match(r"\s*def\s+(\w+)", h["text"])
        if not m or m.group(1).startswith("__"):
            continue
        k = (
            "sheet"
            if re.search(r"get_spreadsheet|open_by_key|\.worksheet\(", h["text"])
            else "sqlite"
        )
        kind[m.group(1)] = "sheet" if kind.get(m.group(1)) == "sheet" else k
    return kind


def indirect_rule(names: list[str]) -> str:
    pats = "".join(f'    - pattern: "{n}($$$A)"\n    - pattern: "$M.{n}($$$A)"\n' for n in names)
    return (
        "id: indirect-blocking-in-async\nlanguage: python\n"
        "message: call to a sync DB/sheet helper directly inside a coroutine\n"
        f"{UTILS}rule:\n  any:\n{pats}  all:\n    - matches: in-async-fn\n    - matches: not-handed-off\n"
    )


def called_name(text: str) -> str:
    m = re.match(r"\s*(?:await\s+)?(?:[\w.]+\.)?(\w+)\(", text)
    return m.group(1) if m else "?"


def run() -> dict:
    direct = scan(os.path.join(RULES, "blocking-in-async.yml"))
    helpers = sync_helpers()
    with tempfile.NamedTemporaryFile("w", suffix=".yml", delete=False, encoding="utf-8") as fh:
        fh.write(indirect_rule(sorted(helpers)))
        tmp = fh.name
    try:
        indirect = scan(tmp)
    finally:
        os.unlink(tmp)
    rows = []
    for h in direct:
        rows.append(_row(h, "direct", "sheet" if h["ruleId"] == "gspread-in-async" else "sqlite"))
    for h in indirect:
        n = called_name(h["text"])
        if _own_async_method(h["file"], h["text"], n):
            continue  # `self.name(...)` where this file defines `async def name`: a name collision
        rows.append(_row(h, "indirect", helpers.get(n, "?"), n))
    return {"helpers": len(helpers), "rows": rows}


_ASYNC_DEFS: dict[str, set[str]] = {}


def _own_async_method(rel: str, text: str, name: str) -> bool:
    if not re.match(r"\s*(?:await\s+)?self\.", text):
        return False
    if rel not in _ASYNC_DEFS:
        with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
            _ASYNC_DEFS[rel] = set(re.findall(r"async def (\w+)\(", fh.read()))
    return name in _ASYNC_DEFS[rel]


def _row(h: dict, how: str, what: str, helper: str = "") -> dict:
    return {
        "file": h["file"],
        "line": h["range"]["start"]["line"] + 1,
        "how": how,
        "what": what,
        "helper": helper,
        "text": h["text"].strip().splitlines()[0][:90],
    }


def report(m: dict) -> str:
    rows = m["rows"]
    out = ["## Blocking I/O inside coroutines", ""]
    out.append(f"Sync helpers that open a connection or spreadsheet: {m['helpers']}")
    c = Counter((r["how"], r["what"]) for r in rows)
    out.append(
        "Direct: "
        f"{c[('direct', 'sqlite')]} sqlite, {c[('direct', 'sheet')]} sheet.   "
        f"Indirect: {c[('indirect', 'sqlite')]} sqlite, {c[('indirect', 'sheet')]} sheet."
    )
    out.append("")
    out.append("### Fix on sight: network calls (sheet) from a coroutine")
    for r in sorted(rows, key=lambda r: (r["file"], r["line"])):
        if r["what"] == "sheet":
            out.append(f"  {r['file']}:{r['line']:<6} {r['how']:9} {r['text']}")
    out.append("")
    out.append("### Direct sqlite calls inside a coroutine (read each; wrap or justify)")
    for r in sorted(rows, key=lambda r: (r["file"], r["line"])):
        if r["how"] == "direct" and r["what"] == "sqlite":
            out.append(f"  {r['file']}:{r['line']:<6} {r['text']}")
    out.append("")
    out.append(
        "### Indirect sqlite: coroutines calling config-style helpers, per file (convention, #589)"
    )
    per_file = Counter(r["file"] for r in rows if r["how"] == "indirect" and r["what"] == "sqlite")
    for f, n in per_file.most_common():
        out.append(f"  {n:4d}  {f}")
    out.append("")
    out.append("### Most-called helpers from coroutines")
    for n, k in Counter(r["helper"] for r in rows if r["how"] == "indirect").most_common(12):
        out.append(f"  {k:4d}  {n}")
    out.append("")
    out.append(
        "Known limits: matching is by helper name, so a module-level function sharing a name with "
        "a sync helper elsewhere still matches; `.update` / `.clear` are not in the gspread rule "
        "because dicts and sets have them too."
    )
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", metavar="PATH")
    a = ap.parse_args()
    m = run()
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(m, fh, indent=1)
    print(report(m))


if __name__ == "__main__":
    main()
