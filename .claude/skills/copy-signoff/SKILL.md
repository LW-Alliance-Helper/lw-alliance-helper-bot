---
name: copy-signoff
description: Build a review page for sign-off — user-facing strings needing approval, open decisions, or findings that have to be answered. HIGH PRIORITY - invoke before writing ANY artifact meant to be reviewed and handed back, and start from the shared template rather than styling a page from scratch. Triggers whenever work produces strings needing approval, a list of open questions, a copy audit, or anything ending in "pick one and I will apply it".
---

# Copy sign-off pages

**Start from `notes/signoff/template.html`. Do not design a new page.**

The style of these pages was being re-decided every session and never
converging, which costs a fresh read of the furniture before the content can
be reached. The template is the settled answer. Copying it is the whole job;
the craft goes into the content, not the CSS.

**The contract is `notes/SIGNOFF_PAGES.md`,** which holds the type scale, the
prose caps, the module set, the palette and the reasoning behind each. Load it
first.

**It lives in the private notes repo, not in this tree.** `notes/` is an
independent repo cloned into this one and is gitignored here, so a checkout
without it will not have them. If `notes/SIGNOFF_PAGES.md` and
`notes/signoff/template.html` are not present, **stop and say so.** Do not
rebuild the page from memory and do not reconstruct the rules by reading old
artifacts — a page styled from guessed conventions is the exact failure this
skill exists to prevent, and it looks close enough to pass review while being
wrong.

---

## Procedure

1. **Load `notes/SIGNOFF_PAGES.md`.** Everything below is the short form.
2. **Copy `notes/signoff/template.html`** to the scratchpad under a name for
   this page.
3. **Fill in the masthead** — eyebrow (feature, PR, state), a real title, a
   standfirst saying how many things need answering. 40 words, hard.
4. **One block per string**, and pick a control module per block. Never merge
   two strings behind one control.
5. **Delete the modules and surfaces this page does not use.** The template
   ships one of each so they can be seen; a real page carries only what it
   needs.
6. **Set the `localStorage` KEY and the export `HEAD`** in the script. The key
   must be unique per page and stable across redeploys of that page.
7. **Run all three checks.** Every time.
8. **Publish and give the full URL** in the message, with one clause saying
   what it is. Never a nickname.

---

## The three control modules

- **A — worded alternatives.** Radios carrying full alternative wordings. Only
  when there is a genuine choice.
- **B — fine / change it.** Two radios, the second opening a textarea. **The
  default.**
- **C — notes only.** A textarea in a fieldset with no radios, for anything
  with nothing to pick between: a screen to react to, a flow to walk, artwork
  to look at.

## The three surfaces

`.embed` (Discord embed), `.select` (open select menu), `.modal` (pre-filled
modal). Wrap in `.screen` with a `.cap` label; mark the string under review
with `<mark class="live">`.

## The three checks

```sh
grep -o 'font-size:[^;}]*' page.html | sort | uniq -c | sort -rn
py notes/signoff/check_contrast.py            # must print ALL PASS
py notes/signoff/check_prose.py page.html     # must print ALL WITHIN CAPS
```

The type audit must show only the six sizes in the contract, with `16px` on
`body` the only `px`, no `em`, and **nothing below `1rem`**.

## What must not be re-decided per session

The type scale, the three typefaces (Archivo / Source Serif 4 / IBM Plex
Mono), the token palette, the module set, and the contrast exemption for
hairline rules and callout edges. All are settled in
`notes/SIGNOFF_PAGES.md` with the reasoning. Read it before changing any of
them, and change them only when asked to.

## When this is NOT the right page

The template is for pages that get **answered**. A standing reference that is
only read — a wiring map, a dossier, a build order — keeps the tokens and the
type scale but drops the decision blocks and the export.
