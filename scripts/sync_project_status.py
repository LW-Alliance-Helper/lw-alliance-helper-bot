#!/usr/bin/env python3
"""
Sync GitHub Project status for issues closed by a PR.

Used by .github/workflows/project-status-sync.yml to walk an issue from
Up Next → In progress → In review → Ready for Release → Shipped without
anyone touching the project board. The lifecycle assumes the bot repo's
branch flow (CLAUDE.md):

    PR opened referencing the issue   →  In progress
    Push to dev (merge of that PR)    →  In review
    Push to release/X.Y.Z             →  Ready for Release
    Push to main (release-branch PR)  →  Shipped

The script is idempotent: setting an item to the status it's already at
is a no-op, and issues not in the project are skipped silently.

Reads `GH_TOKEN` (a PAT with org-project read/write — the auto-injected
GITHUB_TOKEN can't touch org Project v2). Issue references come from
two sources, merged:

  1. The PR's `closingIssuesReferences` field — what GitHub auto-infers
     from `Closes #N` keywords in the body. **Only populates for PRs
     targeting the default branch (main).**
  2. A direct regex scan of the PR body for `Closes / Fixes / Resolves
     #N` (plus the markdown-linked `Closes [#N](...)` variant). This
     is what makes the In progress / In review / Ready for Release
     transitions work, since those fire on PRs into dev or release/*
     where GitHub does not auto-populate `closingIssuesReferences`.

Usage:
  sync_project_status.py --pr 63 --status "Shipped"
  sync_project_status.py --commit abc1234 --status "In review"
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error

GITHUB_API = "https://api.github.com/graphql"
ORG = "LW-Alliance-Helper"
REPO = "lw-alliance-helper-bot"
PROJECT_NUMBER = 2

# These are stable across project sessions; confirm with
# `gh project field-list 2 --owner LW-Alliance-Helper` if the project's
# Status options are ever rebuilt.
PROJECT_ID = "PVT_kwDOEKcAYM4BWkGY"
STATUS_FIELD_ID = "PVTSSF_lADOEKcAYM4BWkGYzhR3O10"
STATUS_OPTIONS = {
    "Backlog": "ba2535b0",
    "Up Next": "4e8153fa",
    "In progress": "84ab9a6f",
    "In review": "fcf39296",
    "Ready for Release": "c7676c23",
    "Shipped": "1c9d5aae",
    "Canceled": "6e1f842d",
}

# GitHub's close keywords (close/closes/closed, fix/fixes/fixed,
# resolve/resolves/resolved), optionally followed by an `owner/repo`
# prefix for cross-repo refs, an optional `[` for markdown-linked refs
# (`Closes [#123](url)`), then `#<number>`. Case-insensitive.
CLOSE_KEYWORDS_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]+"
    r"\[?"
    r"(?:([\w.-]+)/([\w.-]+))?"
    r"#(\d+)",
    re.IGNORECASE,
)


class AuthError(RuntimeError):
    """The PROJECT_TOKEN PAT is missing, expired, or lacks scope (401/403).
    Raised so the entry point can treat it as a soft skip — a token lapse
    should warn, not red-X every push, since project sync is a convenience."""


def gql(query, variables=None):
    token = (os.environ.get("GH_TOKEN") or "").strip()
    if not token:
        raise AuthError("GH_TOKEN (PROJECT_TOKEN) is empty")
    payload = json.dumps({"query": query, "variables": variables or {}}).encode()
    req = urllib.request.Request(
        GITHUB_API,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        # 401 (bad/expired token) / 403 (insufficient scope) are token problems,
        # not code problems — surface them as a soft skip rather than a CI failure.
        if e.code in (401, 403):
            raise AuthError(f"HTTP {e.code} from GitHub: {body}") from e
        raise RuntimeError(f"HTTP {e.code} from GitHub: {body}") from e
    if "errors" in data:
        raise RuntimeError(f"GraphQL errors: {data['errors']}")
    return data["data"]


def get_pr_for_commit(sha):
    """Return the PR number whose merge commit is `sha`, or None.

    For merge-commit pushes (the normal release-flow shape), this is the
    PR that produced the merge. For direct pushes (rare), there's no
    associated PR and we return None — caller skips.
    """
    data = gql(
        """
        query($owner: String!, $repo: String!, $sha: GitObjectID!) {
          repository(owner: $owner, name: $repo) {
            object(oid: $sha) {
              ... on Commit {
                associatedPullRequests(first: 5) {
                  nodes { number mergeCommit { oid } }
                }
              }
            }
          }
        }
        """,
        {"owner": ORG, "repo": REPO, "sha": sha},
    )
    obj = (data.get("repository") or {}).get("object") or {}
    nodes = (obj.get("associatedPullRequests") or {}).get("nodes") or []
    # Prefer the PR whose mergeCommit matches the pushed SHA — that's the
    # PR that produced this merge. Otherwise fall back to the first.
    for n in nodes:
        if n.get("mergeCommit") and n["mergeCommit"]["oid"] == sha:
            return n["number"]
    return nodes[0]["number"] if nodes else None


def get_closing_issues(pr_number):
    """Issues this PR will close on merge.

    Sources, merged and deduped by issue number:

    1. `closingIssuesReferences` — GitHub's auto-inferred list. Only
       populates for PRs targeting the default branch (main).
    2. Regex scan of the PR body for `Closes / Fixes / Resolves #N`
       (and the markdown-linked `Closes [#N](url)` variant). This is
       what makes the In progress / In review / Ready for Release
       transitions work for PRs into dev or release/*.
    """
    data = gql(
        """
        query($owner: String!, $repo: String!, $num: Int!) {
          repository(owner: $owner, name: $repo) {
            pullRequest(number: $num) {
              body
              closingIssuesReferences(first: 50) {
                nodes { id number }
              }
            }
          }
        }
        """,
        {"owner": ORG, "repo": REPO, "num": pr_number},
    )
    pr = (data.get("repository") or {}).get("pullRequest") or {}
    issues = list((pr.get("closingIssuesReferences") or {}).get("nodes") or [])
    seen = {issue["number"] for issue in issues}

    body = pr.get("body") or ""
    body_nums = []
    for owner, repo, num in CLOSE_KEYWORDS_RE.findall(body):
        # Skip cross-repo refs that aren't ours.
        if owner and (owner != ORG or repo != REPO):
            continue
        n = int(num)
        if n in seen:
            continue
        body_nums.append(n)
        seen.add(n)

    for n in body_nums:
        data = gql(
            """
            query($owner: String!, $repo: String!, $num: Int!) {
              repository(owner: $owner, name: $repo) {
                issue(number: $num) { id number }
              }
            }
            """,
            {"owner": ORG, "repo": REPO, "num": n},
        )
        issue = (data.get("repository") or {}).get("issue")
        if issue:
            issues.append(issue)

    return issues


def get_project_item_id(issue_node_id):
    """Find the issue's item ID inside our project, or None."""
    data = gql(
        """
        query($id: ID!) {
          node(id: $id) {
            ... on Issue {
              projectItems(first: 20) {
                nodes { id project { number } }
              }
            }
          }
        }
        """,
        {"id": issue_node_id},
    )
    items = ((data.get("node") or {}).get("projectItems") or {}).get("nodes") or []
    for item in items:
        if item.get("project", {}).get("number") == PROJECT_NUMBER:
            return item["id"]
    return None


def set_status(item_id, option_id):
    """Set a project item's Status. Returns True on success, False when the
    item is archived.

    An archived board card can't be updated (GraphQL: "The item is archived
    and cannot be updated"). This happens when an issue that was closed — and
    whose card got archived — is reopened: the sync then tries to walk it back
    to In progress / Shipped and the mutation fails. That's a board-state quirk,
    not a workflow failure, so skip it rather than crash the whole sync."""
    try:
        gql(
            """
            mutation($p: ID!, $i: ID!, $f: ID!, $o: String!) {
              updateProjectV2ItemFieldValue(input: {
                projectId: $p, itemId: $i, fieldId: $f
                value: { singleSelectOptionId: $o }
              }) { projectV2Item { id } }
            }
            """,
            {"p": PROJECT_ID, "i": item_id, "f": STATUS_FIELD_ID, "o": option_id},
        )
    except RuntimeError as e:
        if "archived" in str(e).lower():
            return False
        raise
    return True


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--pr", type=int, help="PR number")
    p.add_argument("--commit", type=str, help="Commit SHA (looks up the merging PR)")
    p.add_argument("--status", required=True, help="Target status name")
    p.add_argument(
        "--issue",
        type=int,
        action="append",
        default=[],
        help="Issue number to set explicitly (repeatable). "
        "Bypasses PR lookup — useful for one-shot bootstraps.",
    )
    args = p.parse_args()

    if args.status not in STATUS_OPTIONS:
        sys.exit(f"Unknown status: {args.status!r} (valid: {', '.join(STATUS_OPTIONS)})")
    option_id = STATUS_OPTIONS[args.status]

    issues = []
    if args.issue:
        # Resolve issue numbers → node IDs by querying each.
        for n in args.issue:
            data = gql(
                """
                query($owner: String!, $repo: String!, $num: Int!) {
                  repository(owner: $owner, name: $repo) {
                    issue(number: $num) { id number }
                  }
                }
                """,
                {"owner": ORG, "repo": REPO, "num": n},
            )
            issue = (data.get("repository") or {}).get("issue")
            if issue:
                issues.append(issue)
    else:
        if args.pr:
            pr_number = args.pr
        elif args.commit:
            pr_number = get_pr_for_commit(args.commit)
            if pr_number is None:
                print(f"No PR associated with {args.commit} — nothing to do")
                return
        else:
            sys.exit("Specify --pr, --commit, or --issue")
        issues = get_closing_issues(pr_number)
        if not issues:
            print(f"PR #{pr_number} closes no issues — nothing to do")
            return
        print(f"PR #{pr_number} closes {len(issues)} issue(s); target: {args.status!r}")

    for issue in issues:
        item_id = get_project_item_id(issue["id"])
        if item_id is None:
            print(f"  #{issue['number']}: not in project, skipped")
            continue
        if set_status(item_id, option_id):
            print(f"  #{issue['number']}: -> {args.status}")
        else:
            print(f"  #{issue['number']}: archived in project, skipped")


if __name__ == "__main__":
    try:
        main()
    except AuthError as e:
        # Token lapse: warn (visible in the Actions log) and exit clean so the
        # workflow stays green. The board just won't advance until the secret
        # is refreshed.
        print(
            f"::warning::Project status sync skipped: PROJECT_TOKEN auth failed ({e}). "
            "Regenerate the fine-grained PAT (org Projects: Read and write) and update the "
            "PROJECT_TOKEN repo secret."
        )
        sys.exit(0)
