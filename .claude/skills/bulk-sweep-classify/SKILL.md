---
name: bulk-sweep-classify
description: Classify every match before a bulk find-replace across many files. Use when renaming a command, helper, config key, path or term repo-wide, when migrating a call pattern, or when removing a mechanism and sweeping up its traces. The matched set always looks uniform and never is. Also fixes the verification trap — "re-run the grep and expect zero" is the wrong success test.
---

# Bulk sweep — classify every match first

A regex sees text. A transform needs meaning. One pattern catches genuine
targets *and* look-alikes that must not be touched, and rewriting all of them
corrupts the look-alikes silently: the sweep reports success and the damage
surfaces weeks later.

**Enumerate the matches and bucket every one before editing anything.**

Adapted from `bulk-sweep-classify` in [laurigates/claude-plugins](https://github.com/laurigates/claude-plugins)
(MIT). The four categories and the verification trap are upstream's. The
hazards section is this repo's.

---

## Step 0 — Route by target: code or prose?

**Code** — a helper rename, a call-site migration, an import change in `.py`
files. Do it structurally with `ast-grep-search`. An AST pattern cannot match
inside strings, comments or URLs, so category 2 below shrinks to nearly nothing
and you only have to think about categories 3 and 4.

**Prose or mixed** — a term in `CLAUDE.md`, an embed string, a command name in
docs. Regex is all you have. Run the full pipeline below.

Most renames here span both: the symbol in code *and* the term in
`messages.py`, `CLAUDE.md` and embed copy. Split them. ast-grep the source,
then run the four-category pass over the text.

## The four categories

| # | Category | Handling |
|---|---|---|
| 1a | **Genuine target, mechanical** | Transform |
| 1b | **Genuine target that touches user-facing copy** | Transform only after `copy-signoff`; one block per string |
| 2 | **False positive** — matches the pattern, unrelated | Leave |
| 3 | **Out-of-scope design** legitimately using the old form | Leave — transforming corrupts it |
| 4 | **Immutable record** — changelog, shipped release notes, an issue body | Leave the body; add a note if needed |

The 1a / 1b split is the whole difference on a sweep that carries copy. On
the shared-base-view classification it was 61 sites that move mechanically
against 11 that each carried their own wording; the second group stopped for
a sign-off page and the first did not.

**Category 3 is the sharpest trap.** The token you are replacing usually also
appears in filenames, config keys, DB column names or asset paths that the
matched term participates in. Renaming a feature also rewrites the asset
filenames and the stored config keys that share its name — and those are
data, not code. Renaming `storm` in prose must not rename `storm_signup_view`
in a filename or `storm_*` keys already written into guild config rows.

## Step 1 — Enumerate, deduped, before touching anything

```bash
git grep -nhoE '<pattern>' -- <scope> | sort -u
git grep -cE '<pattern>' -- <scope>          # count per file
```

### Derive the term set from what the thing *claims*, not just what it is *named*

Removing a mechanism means removing its **assertions** as well as its
**identifiers**, and those two families share no tokens. A sweep built only
from identifiers reports clean while the claim survives.

| Family | Examples | Where it lives |
|---|---|---|
| Identifiers | function, module, config key, env var | code, workflows |
| Claims | "automatically", "every Sunday", "premium alliances get…", "this is scheduled" | docstrings, embed copy, `messages.py`, `CLAUDE.md`, error strings |

The claim family is where user-facing damage concentrates. An identifier left
behind is dead code. **A claim left behind is a bot telling a user something
untrue.** If a scheduled post is removed, `git grep` for the function name will
never find the embed sentence promising it happens weekly.

Read `messages.py` and `CLAUDE.md` for every sweep that removes or renames a
user-visible behaviour, whether or not they matched the pattern.

## Step 2 — Tighten the pattern to kill category 2 mechanically

Push the exclusions into the pattern so the sweep cannot touch a false positive
at all. A word boundary, a required prefix, a scope restriction — each one
removes a whole class from manual review.

## Step 3 — Bucket every remaining hit

Read the surrounding line. Categories 1 and 2 are usually mechanical.
**Reserve real judgment for 3 and 4** — they look like genuine targets and can
only be told apart by reading intent.

Record the files and lines in categories 2–4, **on the tracking issue for
the sweep**, not in a scratchpad. That is the **intentionally preserved
set**; Step 5 depends on it, and Step 5 usually runs in a different session
from Step 3. Generate the list from the enumeration rather than typing it:
the first time it was typed, a count was wrong by four.

## Step 4 — Scope the transform to category-1 files only

Pass only the category-1 file list to the transform. Never point a repo-wide
rewrite at the tree root.

## Step 5 — Verify against the preserved set, not zero

Re-run the Step 1 enumeration. The success test is **not** "zero matches
remain":

> **Zero matches remain outside the intentionally preserved set.**

Categories 3 and 4 are supposed to keep matching. A verification demanding
literal zero will either fail spuriously or — worse — pressure you into
corrupting a category-3 design just to force the count down.

### The exclusion trap, one level up

**Every exclusion in a verification scope is a claim about the world and needs
checking like any other.** "That directory is generated, so it doesn't count"
assumes the generator actually runs. Upstream's worked example: a sweep excluded
a committed docs directory reasoning that CI regenerated and published it — but
the publish target had never been enabled and the workflow had failed for ten
days, so the excluded directory was the only rendered copy and still carried the
stale claim. The report said "no matches remain", which was true of what it
searched and false of the repo.

For each path you exclude, state why in one line, then verify that line.

---

## Hazards specific to this repo

**Parallel sessions share this checkout.** Other worktrees and other sessions
have uncommitted work in the same tree. Run `git status` before any sweep, scope
the transform to files you know are yours, and never `git add -A` — you will
commit someone else's in-progress work.

**`notes/` is a separate gitignored repo.** It does not exist in worktrees. A
sweep run from a worktree will silently miss every design document, and a sweep
run from the main checkout must not commit `notes/` changes into this repo.

**`messages.py` copy needs sign-off.** If a sweep rewrites a user-facing string,
that is a copy change, not a rename. Stop and load `copy-signoff`. Enumerate
every affected string and get approval before applying — one block per string,
shown where it fires.

**The changelog belongs to the release branch.** Never sweep `CHANGELOG.md` on a
feature branch or on `dev`.

**Shipped release notes and closed issues are category 4.** Do not rewrite what
was true when it was published.

## Reporting

```
## Sweep classification — <what is being renamed or consolidated>

Enumerated: <rule or pattern>, N definitions / M sites in K files

### 1a Mechanical (N)              → transform
### 1b Needs copy sign-off (N)     → copy-signoff first, one block per string
### 2  False positives (N)         → leave
### 3  Out-of-scope designs (N)    → leave; one line each on why
### 4  Immutable records (N)       → leave

### Prose that changes with the code
- <file:line> — <the claim it makes>

Preserved set recorded at: <issue URL>
```

Categories 2–4 are listed site by site. Category 1 is counted, with the 1b
sites listed because each one is a decision.

## Fanning out

If the category-1 set is large enough to parallelise, the rename map and the
explicit do-NOT-rename list are the payload — brief both. An agent given only
the rename map will rediscover categories 3 and 4 by breaking them.

## Related

- `ast-grep-search` — the structural route from Step 0, for code targets
- `dry-consolidation` — hand it a long cluster list and it lands here
- `copy-signoff` — the moment a sweep touches user-facing strings
- `code-dead-code` — sweeping up after a removal
