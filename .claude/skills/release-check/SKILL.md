---
name: release-check
description: Audit a release branch against the full release checklist before opening the PR into main — the judgment calls a CI job can't verify (copy sign-off actually happened, the Discord changelog post matches what shipped, whether an announcement post makes sense, cross-repo dependencies are resolved), plus a local run of the machine-checkable half CI will enforce anyway. Triggers on the release-branch-creation hook's nudge, or whenever asked to check a release is ready for its PR into main.
---

# Release check

Run this on a `release/X.Y.Z` branch, before opening the PR into `main`.
It exists because cutting a release has a machine-checkable half (CI now
gates this on the PR — see below) and a judgment half that nothing but a
session actually reading the branch can verify. See
[#628](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/628)
for the incident that produced this skill: a release shipped having
skipped a copy-signoff step, an unreviewed Discord changelog post, no
announcement-post decision, and an unresolved cross-repo dependency —
each individually small, none caught until Kevin asked directly.

Report findings in the shape `ux-review` and `code-review` use: most
severe first, what's missing, where, and the concrete next step. If
everything checks out, say so plainly — this is not a skill that has to
find something wrong to be worth running.

---

## 0. Machine-checkable half (CI enforces this — run it here to catch it before the push)

`.github/workflows/release-changelog-check.yml` blocks the PR into main
on all of these already. Running them locally first just saves a CI
round trip:

```
py -c "import re; print(re.search(r'__version__ = \"([^\"]+)\"', open('bot.py').read()).group(1))"
py scripts/check_changelog_slim.py CHANGELOG.md
py scripts/discord_changelog.py <version> --check
```

Plus by hand: `bot.py.__version__` matches the branch name
(`release/X.Y.Z` → `X.Y.Z`), and `CHANGELOG.md` has a dated
`## [X.Y.Z] — YYYY-MM-DD` heading, not "Unreleased". These are settled
mechanically — don't spend judgment here, just confirm the numbers agree
before moving to the half that needs it.

---

## 1. Copy sign-off

**Diff every user-facing string this branch adds or changes** against
the branch's base (`dev`, usually): embed text, DM/reminder templates,
button labels, `messages.py` constants, new `/help` entries, error
copy. `git diff origin/dev...HEAD -- '*.py'` filtered to string
literals, or lean on `ux-review`'s scope logic if it's already loaded.

For each one, answer honestly: **was it put through the `copy-signoff`
skill and did Kevin actually answer it** — not drafted, not described
in chat, an actual sign-off page with a recorded answer? If any string
skipped that, that's a finding: name the string, the file, and that it
still needs sign-off. Don't backfill a sign-off page here as a
formality; surface the gap and let it be run properly.

## 2. Discord changelog post reviewed against what shipped

Open `docs/DISCORD_CHANGELOG.md` and find this version's block. Compare
its bullets against `CHANGELOG.md`'s entry for the same version,
one-for-one:

- Does every CHANGELOG bullet that an alliance can act on have a
  corresponding line in the Discord block (or a deliberate omission —
  the file's own rules say it drops what an alliance can't act on and
  caps at five lines)?
- Is any bullet in the Discord block **left over from an earlier
  draft** — written before this session's changes landed, and not
  reflecting them? This is what happened in #628: five bullets that
  predated the session and didn't mention any of the eighteen new
  CHANGELOG entries it added.
- Does the block's date match the CHANGELOG heading's date?

If the block is stale or thin relative to the CHANGELOG entry, that's a
finding — rewrite it before the PR, following the merge/dedup/cap rules
in the file's own preamble.

## 3. Release-unveiling announcement post

**Patch releases skip this section.** Compare the release branch's
version against the one currently on `main`: if only the third number
changed (`1.9.0` → `1.9.1`), this is a patch, say so in one line and
move to §4. Kevin, 19 Sep, confirmed on 1.9.1: a patch doesn't get an
announcement, full stop, so there's no decision here to chase him for.
Only a minor or major bump (the first or second number moves) reaches
the rest of this section.

Distinct from the automated `#changelog` bot post: the richer
`:tada: Introducing version X.Y.Z` post a session drafts by hand (style
rules in memory: `feedback_release_unveiling_post_style.md`).

The checklist item is narrow, per the issue this skill comes from:
**was the decision made**, not "does every release get one." Confirm
one of:

- A post was drafted and is ready to go out, or
- A post was drafted and Kevin reviewed/sent it, or
- A conscious call was made that this release doesn't warrant one, and
  Kevin signed off on that call.

If none of these happened — nobody asked the question — that's the
finding. Ask Kevin now, in the body of a message, not a popup: does
this release deserve the unveiling post?

## 4. Cross-repo dependencies

Grep this branch's rolled-in issues (the PR body's `Closes #…` list)
and their linked PRs for any reference to
`lw-alliance-helper.github.io` — a companion website branch, a PR
description saying "merge alongside this release," a design doc note.
Also check `../lw-alliance-helper.github.io` directly for open branches
whose name matches an issue or feature in this release.

For anything found: is it merged? If not, was that left open as a
question to Kevin, and did Kevin actually answer it — not just posed
and moved past? An open question with no recorded answer is a finding,
even if it was asked.

## 5. New required environment variables

Grep this branch's diff against its base for anything reading an
environment variable that didn't exist on the base branch — a new
`os.getenv`/`os.environ` call, a new required config constant, anything
a feature now depends on to run in production. 1.9.0's `CD_ENGINE_TOKEN`
gap ([#629](https://github.com/LW-Alliance-Helper/lw-alliance-helper-bot/issues/629))
is the incident this section exists to catch: a genuine new production
requirement, and nothing ever asked, in as many words, whether it had
been added to Railway before the merge.

For every new one found, name it explicitly on its own line — never
folded into a general "environment changes" summary — and ask directly:
has it been added to the production Railway service, and confirmed by a
real deploy log, not just added and assumed?

If this release introduces no new required environment variable, say so
in one line and move to Reporting.

---

## Reporting

Five sections, matching the steps above. Each finding: what's missing,
where (file, PR, or "nobody asked"), and what closes it. End with a
one-line verdict: ready for the PR into main, or not yet and why.

Don't fix anything here except the Discord changelog block itself
(§2 is a content edit, not a decision) — sign-off pages, announcement
posts, and cross-repo merges are Kevin's calls to make, not this
skill's to force through.
