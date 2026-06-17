# Documentation images

Drop screenshots, QC examples, and diagrams here, then reference them from any
page with a relative path.

## How to add an image

1. Save the file in this folder (`docs/assets/images/`).
2. In the relevant `.md` page, add:

   ```markdown
   ![Descriptive alt text](assets/images/your-file.png)
   ```

   Material's lightbox plugin makes every image click-to-zoom automatically — no
   extra markup needed.

3. For a captioned figure, use:

   ```markdown
   <figure markdown>
     ![Alt text](assets/images/your-file.png)
     <figcaption>A short caption.</figcaption>
   </figure>
   ```

Several pages already contain **commented-out image slots** at the ideal spot —
search the docs for `assets/images/` inside an HTML comment (`<!-- ... -->`),
drop in a matching file, and delete the comment markers to make it live.

## Suggested screenshots (high value)

| Filename | Page | What to show |
|---|---|---|
| `hero-overview.png` | `index.md` | A glanceable pipeline/result montage for the landing hero |
| `qc-preprocess.png` | `preprocessing.md`, `walkthrough.md` | BaSiC before/after illumination correction |
| `qc-registration-overlay.png` | `registration_methods.md`, `walkthrough.md` | RGB alignment overlay (`*_QC_RGB.png`) |
| `segmentation-overlay.png` | `segmentation.md` | Cell/nuclei masks over DAPI |
| `qupath-geojson.png` | `export.md`, `flowpath.md` | `cells.geojson` loaded over the pyramid in QuPath |
| `flowpath-gating.png` | `flowpath.md` | FlowPath gating UI on MIRAGE cells |
| `benchmarks/scaling_<PROCESS>.png` | `benchmarks.md` | Per-process peak RSS vs input size with fitted line (from `export_docs_figures.py`) |
| `benchmarks/rtre_tiled_vs_untiled.png` | `benchmarks.md` | Landmark rTRE, in-process tiling on vs off |

## Conventions

- Prefer **PNG** for screenshots, **SVG** for diagrams.
- Keep files reasonably small (< ~500 KB) — downsample large screenshots.
- Use lowercase, hyphenated names that describe the content.
