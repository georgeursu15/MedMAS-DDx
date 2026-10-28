# Figures

The four figures from the ICCP paper, referenced by the root
[`README.md`](../../README.md).

| File | Paper figure | Caption | Size | Used in |
|---|---|---|---|---|
| `architecture.png` | **Fig. 1** (p. 3) | System components and their interactions | 1800 × 548 | Header |
| `symptom-mapping.png` | **Fig. 2** (p. 5) | Distribution of symptom mapping resolution | 1100 × 329 | Results |
| `accuracy-phases.png` | **Fig. 3** (p. 6) | Top-1 and Top-3 accuracy, Phase 1 against Phase 2 | 1100 × 680 | Results |
| `medqdx-comparison.png` | **Fig. 4** (p. 7) | Performance comparison: MedQDx vs MedMAS-DDx | 1100 × 729 | Results |

Each one is the figure body only. The `Fig. N:` caption line is deliberately
left out — the alt text and the surrounding prose already carry it, and a
baked-in caption reads as duplicated.

---

## How these were produced

All four are **vector** graphics in the paper — the architecture diagram and
three `pgfplots` bar charts. None is an embedded bitmap, so none was ever
screenshotted.

Each was rendered straight from the PDF's vector content with PyMuPDF at 8×
(576 dpi), clipped to the figure's region on the page and stopping above the
caption, then trimmed to its ink bounding box, given a small white margin, and
downsampled with Lanczos to the width in the table above. Rendering before
downsampling, rather than cropping an already-rasterised page, is what keeps
the small axis labels legible.

Background is **white, not transparent**: a transparent background inverts
badly for anyone reading GitHub in dark mode, and the text in these figures is
dark.

### Regenerating

Re-render from the paper PDF rather than editing these PNGs. If the figures
themselves change, the cleanest source is the original `pgfplots` code compiled
standalone — a `standalone` document exports far more cleanly than any crop out
of a typeset page.

---

## Attribution

These figures come from a paper published by IEEE. Reusing your own figures in
your own repository is normal practice. Note that once the paper is in IEEE
Xplore the published version carries an IEEE copyright line, and adding the
paper's PDF itself is a separate question — check the sharing policy in the
IEEE Author Center before posting it anywhere other than your own or your
institution's pages.
