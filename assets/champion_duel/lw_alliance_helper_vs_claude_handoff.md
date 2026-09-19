# LW Alliance Helper VS Matchup Card — Claude Code Handoff

## 1. Source of truth

Use **`lw_alliance_helper_vs_template_final.png`** as the final static background/template.

Canvas size: **1619 × 971px**.

Use **`lw_alliance_helper_vs_claude_layout.json`** as the machine-readable coordinate source. All coordinates in this document are pixel coordinates against the exact 1619×971 template.

A visual verification overlay is provided as **`lw_alliance_helper_vs_template_final_debug.png`**. Do not ship the debug image.

## 2. Critical implementation rule

The background image already contains the decorative UI, card frames, VS artwork, header/footer framing, squad containers, and an **empty progress track only**.

**Do not bake a fixed prediction result into the background.** The bot owns the entire progress fill and divider at render time.

The bot should render all variable text after compositing the static template.

## 3. Data mapping

Expected matchup data from the current wireframe:

- Event title, e.g. `CHAMPION DUEL`
- Round/group metadata, e.g. `Group M • Semifinal`
- Optional LW Alliance Helper logo/badge in the upper-right square
- Left competitor name, e.g. `RavenShade #738`
- Right competitor name, e.g. `NightOwl #738`
- Left win probability, e.g. `10%`
- Right win probability, e.g. `90%`
- `to win` label under each probability
- Three observed squads per competitor
  - row number/icon area
  - squad type, e.g. `Missile`, `Tank`, `Aircraft`
  - squad power, e.g. `31.5M`
- Status/sightings line per competitor, e.g. `3/3 seen · their order in 1 sighting`
- Confidence/footer summary, e.g. `Confidence: high · Built on observed squads and recorded sightings`
- Prediction split used to draw the runtime progress bar

## 4. Pixel coordinate map

### Header

| Field | x | y | w | h | Alignment |
|---|---:|---:|---:|---:|---|
| Event title | 62 | 42 | 625 | 55 | Center |
| Group / round | 817 | 42 | 589 | 55 | Center |
| Logo badge | 1470 | 24 | 101 | 86 | Center |

### Left competitor

| Field | x | y | w | h | Alignment |
|---|---:|---:|---:|---:|---|
| Name | 113 | 170 | 426 | 47 | Center |
| Win probability | 108 | 232 | 446 | 174 | Center |
| `to win` | 215 | 420 | 220 | 28 | Center |
| Squad group | 109 | 455 | 441 | 191 | — |
| Squad 1 icon/index | 128 | 470 | 49 | 45 | Center |
| Squad 1 text | 192 | 470 | 338 | 45 | Left / vertically centered |
| Squad 2 icon/index | 128 | 526 | 49 | 45 | Center |
| Squad 2 text | 192 | 526 | 338 | 45 | Left / vertically centered |
| Squad 3 icon/index | 128 | 584 | 49 | 45 | Center |
| Squad 3 text | 192 | 584 | 338 | 45 | Left / vertically centered |
| Status / sightings | 110 | 670 | 440 | 39 | Center |

### Right competitor

| Field | x | y | w | h | Alignment |
|---|---:|---:|---:|---:|---|
| Name | 1052 | 170 | 426 | 47 | Center |
| Win probability | 1040 | 232 | 448 | 174 | Center |
| `to win` | 1155 | 420 | 220 | 28 | Center |
| Squad group | 1041 | 455 | 441 | 191 | — |
| Squad 1 icon/index | 1058 | 470 | 50 | 45 | Center |
| Squad 1 text | 1124 | 470 | 340 | 45 | Left / vertically centered |
| Squad 2 icon/index | 1058 | 526 | 50 | 45 | Center |
| Squad 2 text | 1124 | 526 | 340 | 45 | Left / vertically centered |
| Squad 3 icon/index | 1058 | 584 | 50 | 45 | Center |
| Squad 3 text | 1124 | 584 | 340 | 45 | Left / vertically centered |
| Status / sightings | 1041 | 670 | 441 | 39 | Center |

### Footer

| Field | x | y | w | h | Alignment |
|---|---:|---:|---:|---:|---|
| Confidence / summary | 111 | 863 | 1374 | 49 | Center |

### Runtime prediction bar

Static track bounds:

- **x = 110**
- **y = 770**
- **w = 1391**
- **h = 39**
- Corner radius: **19px**

The static template contains only a quiet empty track. Render the full blue/red result dynamically.

## 5. Runtime progress-bar rendering

Let `p` be the left competitor win probability normalized to `[0, 1]`.

Track:

```text
trackX = 110
trackY = 770
trackW = 1391
trackH = 39
radius = 19
```

Recommended divider x-position:

```text
dividerCenterX = trackX + round(trackW * p)
```

Clamp the divider center so it never destroys the rounded outer caps:

```text
minX = trackX + radius
maxX = trackX + trackW - radius
dividerCenterX = clamp(dividerCenterX, minX, maxX)
```

Render order:

1. Clip all fill drawing to the rounded track shape.
2. Draw the **blue gradient** from `trackX` through `dividerCenterX`.
3. Draw the **red gradient** from `dividerCenterX` through `trackX + trackW`.
4. Draw a gold vertical divider centered on `dividerCenterX`.
5. Optional: add a subtle 2–4px bloom to the fills/divider; do not draw a large enclosing box.

The fill must occupy the **full track height**. Do not add a separate top stripe or floating highlight band.

Suggested gradients:

```text
Blue outer: #159FF6
Blue inner: #216AEA
Red inner:  #D72636
Red outer:  #EF342A
Gold light: #FFD96A
Gold dark:  #E29A18
```

## 6. Typography hierarchy

Use a bold, condensed or semi-condensed sans-serif that reads cleanly at Discord image scale. Do not rely on an unusual font unless it is bundled with the bot; use a stable fallback stack.

Recommended sizing at native 1619×971 rendering:

| Field | Suggested size | Weight |
|---|---:|---|
| Event title | 30–34px | 700–800 |
| Group / round | 20–24px | 600–700 |
| Competitor name | 24–28px | 700–800 |
| Win probability | 92–116px | 800–900 |
| `to win` | 17–20px | 600–700 |
| Squad index/icon text | 18–21px | 700 |
| Squad type/power | 20–23px | 600–700 |
| Status / sightings | 16–18px | 500–600 |
| Footer confidence | 18–21px | 500–600 |

Text should be primarily near-white (`#F7F8FF`). Use gold only for selective emphasis, not all text.

## 7. Text fitting rules

### Competitor names

- Single line only.
- Center aligned.
- Start at 28px.
- Shrink until it fits; recommended minimum 18px.
- If still too wide at minimum size, ellipsize the visible name before the server suffix when possible.

### Win probabilities

- Render integer percentages unless product requirements specify decimals.
- Keep both sides the same font size when possible for visual balance.
- Center horizontally and vertically inside the large probability box.

### Squad rows

Recommended displayed string inside the text cell:

```text
Missile   31.5M
```

or use separate left/right anchors inside the same cell:

```text
[type aligned left]                  [power aligned right]
```

The second option is preferred because powers then scan vertically.

### Status line

- Single line.
- Center aligned.
- Shrink to approximately 14px before ellipsizing.

### Footer

- Keep to one line in normal cases.
- Center aligned.
- Allow modest shrink before truncation.

## 8. Color rules for runtime text

- General text: `#F7F8FF`
- Muted secondary text: `#C9C9DA`
- Optional left accent: `#61C4FF`
- Optional right accent: `#FF777B`
- Optional gold emphasis: `#FFD35B`

Avoid heavy strokes around small text. A subtle dark shadow or 1–2px dark stroke is enough for readability.

## 9. VS exclusion zone

Do not place runtime text over the central VS / energy burst.

Approximate exclusion box:

```text
x=660, y=285, w=300, h=290
```

The VS artwork is already baked into the static template.

## 10. Render pipeline

Recommended pipeline:

1. Load `lw_alliance_helper_vs_template_final.png` at native 1619×971.
2. Composite optional logo/badge into its safe zone.
3. Draw header title and round metadata.
4. Draw competitor names.
5. Draw large win probabilities and `to win` labels.
6. Draw the three squad rows on each card.
7. Draw left/right sightings/status lines.
8. Draw the runtime prediction bar using the calculated split.
9. Draw footer confidence/summary.
10. Export PNG at native resolution.

Do not scale the template before rendering text. If a smaller Discord image is needed, render everything at native size and downsample the final composited PNG once at the end.

## 11. Current wireframe example

A current example payload could map to the UI like this:

```text
Event: CHAMPION DUEL
Metadata: Group M • Semifinal

Left: RavenShade #738
Probability: 10%
Squad 1: Missile / 31.5M
Squad 2: Tank / 34.8M
Squad 3: Aircraft / 27.2M
Status: 3/3 seen · their order in 1 sighting

Right: NightOwl #738
Probability: 90%
...same row structure...

Footer: Confidence: high · Built on observed squads and recorded sightings
```

## 12. Files delivered

- `lw_alliance_helper_vs_template_final.png` — ship-ready static template
- `lw_alliance_helper_vs_template_final_debug.png` — coordinate verification overlay; development only
- `lw_alliance_helper_vs_claude_layout.json` — machine-readable layout map
- `lw_alliance_helper_vs_claude_handoff.md` — this handoff document
