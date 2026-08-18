# Adding Tissue Microarray (TMA) support to Mirage

*Deep-research report — 2026-07-01*

## Executive summary

Supporting TMAs in Mirage is fundamentally a **fan-out problem**, not a new algorithm: a TMA is *one* physical slide holding dozens of independent cores (often different patients), whereas Mirage's entire architecture treats one `patient_id` as one WSI with a "one reference per patient" invariant and `groupTuple(by: patient_id)` everywhere. The missing capability is a **de-array (dearray) step** that detects cores on a slide, crops each into its own image, and remaps the meta map so each core flows downstream as a sub-sample carrying `core_id` + `patient_id`. This exact pattern is already shipped and battle-tested in **MCMICRO / nf-core/mcmicro** — a Nextflow DSL2 pipeline whose `COREOGRAPH` module (UNet-based) does register-then-dearray, and there is a reusable **nf-core `coreograph` module** Mirage can include almost verbatim. The literature converges on the same order of operations (register the intact slide first, then dearray) and on well-understood gridding/QC methods; the main open questions are core→patient mapping ergonomics and per-core QC (missing/folded/damaged cores).

---

## Sub-topic 1 — De-arraying / core detection

**Literature.** The canonical classical pipeline is Wang et al. 2011 (*PLoS One*): Otsu/convex-hull core segmentation → **Delaunay-triangulation gridding** → map correlation to a "TMAMap" (99.84% cores segmented, 99.96% correctly named on 3,129 cores) — a direct blueprint for "detect cores → grid them → assign each to its samplesheet position." Modern deep-learning takes: **TMA-Grid** (arXiv:2407.21233, 2024) does CNN tissue segmentation + grid estimation with a FAIR/open design; **STiLE** (bioRxiv 2026 ⚠️ preprint) targets spatial-transcriptomics point clouds rather than image cores; **mNet** (2023) handles MALDI-MSI TMAs. Together, TMA-Grid (segmentation) + Wang 2011 (Delaunay gridding + map correlation) define what a `DEARRAY` module needs to do.

**Code/docs (verified 2026-07-01).**
- **Coreograph / UNetCoreograph** — [HMS-IDAC/UNetCoreograph](https://github.com/HMS-IDAC/UNetCoreograph) (7★, MIT, active 2025-09). UNet core detection/segmentation, CLI Python; **official container `labsyspharm/unetcoreograph` is live** (updated 2025-09). Ships as an MCMICRO module → most drop-in containerized option for DSL2. Also exposed as an **nf-core module** ([nf-co.re/modules/coreograph](https://nf-co.re/modules/coreograph/), Coreograph 2.2.9) — includable directly.
- **QuPath TMA dearrayer** — [qupath/qupath](https://github.com/qupath/qupath) (1,390★, GPL-3.0, active). ImageJ-based `TMADearrayer`, scriptable in Groovy, runs **headless** via `QuPath script --quiet`. Best-maintained; GPL-3.0 is the licensing caveat.
- **TMA-Grid** — [episphere/tma-grid](https://github.com/episphere/tma-grid) (4★, MIT, active). Browser-only/interactive — **poor headless-pipeline fit**.
- **STiLE** — [Huang-AI4Medicine-Lab/stile](https://github.com/Huang-AI4Medicine-Lab/stile) (0★, Apache-2.0, new/immature, no container).
- **ATMAD / PRISM** — ATMAD (BMC Bioinformatics 2018) has no maintained repo found; PRISM is TMA *analysis*, not a core detector.

**Verdict:** Coreograph (containerized, Nextflow-native) or headless QuPath are the two production-ready choices. No maintained standalone Cellpose/StarDist TMA-core detector was found.

---

## Sub-topic 2 — Registration of TMA cores

**Literature.** VALIS (Gatenbee et al., *Nat Commun* 2023) explicitly registers cores from an immunofluorescent ovarian-cancer TMA — but by treating each **already-separated** core stack as its own mini-WSI. There is **no built-in de-arraying**; cropping cores is left to the user. A 2024 *Br J Cancer* review frames the general multiplex-registration landscape.

**Code/docs.** [MathOnco/valis](https://github.com/MathOnco/valis) (226★, MIT, active 2025-06, headless + Docker). Alternatives: [labsyspharm/ashlar](https://github.com/labsyspharm/ashlar) (176★, MIT, active — MCMICRO's engine), [labsyspharm/palom](https://github.com/labsyspharm/palom) (59★, active), [NHPatterson/wsireg](https://github.com/NHPatterson/wsireg) (102★, elastix-based, near-dormant 2024).

**Key design decision — order of operations.** MCMICRO and Galaxy-ME both **register the intact whole slide first, then dearray** (Coreograph crops each core afterward). This preserves global spatial context and avoids per-core parameter drift. VALIS's per-core approach mainly suits serial sections where cores are already separated. **For Mirage, register-then-dearray is the recommended order** — and it means Mirage can keep VALIS as-is for the registration step, inserting dearray *after* registration and *before* segmentation.

---

## Sub-topic 3 — Per-core segmentation & quantification

**MCMICRO already solves the end-to-end TMA case, and it IS Nextflow DSL2** (Schapiro et al., *Nature Methods* 2022; [labsyspharm/mcmicro](https://github.com/labsyspharm/mcmicro), 154★, MIT, active): ASHLAR stitch/register → **Coreograph dearray (per-core stacks)** → UnMICST/Mesmer segmentation → **MCQuant per-cell quantification** → SCIMAP. Cores inherit their identity simply by being processed per-core.

**Per-core vs WSI segmentation.** Processing per-core is *easier* than WSI: individual cores are memory-tractable, sidestepping tile-boundary artifacts almost entirely. If tiling within very large cores, use overlapping/strided tiles + edge-prediction filtering. Mirage's existing segmentation backends map cleanly onto per-core work — **InstanSeg** (Goldsborough et al., arXiv:2408.15954; [instanseg/instanseg](https://github.com/instanseg/instanseg), 225★, Apache-2.0, active, QuPath-integrated) is a particularly good per-core fit; StarDist/CellSAM also work.

**TMA-specific QC.** Coreograph itself flags **dimmed/fragmented cores** (direct prior art for a QC gate). MxIF "Q-score" methods (arXiv:2411.00948) evaluate registration + core quality + segmentation; tissue-loss/folding is detectable by registering post-bleach cores to a baseline, and DAPI-based blur/fold detectors exist. Mirage's existing per-step QC PNG framework is the natural home for a "core report" (which cores are present/missing/folded).

---

## Sub-topic 4 — Nextflow data model, metadata & file formats

**Fan-out pattern (the reference model: [nf-core/mcmicro](https://github.com/nf-core/mcmicro), 32★, MIT, active 2026-06).** `--tma_dearray` runs `COREOGRAPH` on the registered image; one slide splits into per-core `.tif` stacks + binary masks + a TMA-map QC image + a `centroids` file (Y,X per core). Each core then flows individually through segmentation/quantification. **Mirage analog:** emit a channel of `[meta{id: "slide__A-1", core_id: "A-1", patient_id}, core.tif]` after dearray, so each core `groupTuple`s exactly like today's sub-sample. This requires touching every `groupTuple(by: patient_id)` in the three subworkflows and the `patient_id`/`is_reference` invariants in `lib/CsvUtils.groovy`.

**QuPath TMA data model.** Grid of `TMACoreObject`s; core names are row-col like `"A-1"`. Core→patient mapping is a **"Unique ID" per core** (multiple cores can share one ID = same patient), stored as a tab-delimited **`.qpmap`** file or embedded in `.qpdata`. Cells nest under their core as parent. This suggests Mirage should accept an optional **core→patient CSV** (a `.qpmap` analog) in the samplesheet.

**File formats.** OME-NGFF has **no TMA spec**; the closest is the HCS **plate/well** model (well = `{path, rowIndex, columnIndex}`) which could be repurposed to tag cores. GeoJSON has **no native parent-ID field** — carry `core_id`/`parent_id` as a **custom property** on each feature. Mirage already writes custom measurement keys into `cells.geojson`, so stamping `core_id`/`parent_id` is a minimal, contract-compatible change (and the sibling `qupath-extension-flowpath` consumer would need to learn to read it).

---

## Research vs. shipping

- **Shipping is *ahead* of research here.** The hardest-sounding part (de-array + per-core fan-out + quantification) is already a mature, MIT-licensed, Nextflow-native, containerized reality in MCMICRO/nf-core. Mirage does not need to invent algorithms — it needs to adopt/wrap Coreograph and remap its own meta grouping.
- **Where research leads:** newer DL dearrayers (TMA-Grid, STiLE) and richer QC/Q-scores exist in papers but are either browser-only, immature, or not containerized — not yet worth adopting over Coreograph.
- **Standards lag:** there is no OME-NGFF/GeoJSON TMA standard, so core identity is necessarily carried as ad-hoc custom properties (as MCMICRO/QuPath do).

## Conflicts & gaps

- **Order of operations** is the one genuine design fork: register-whole-then-dearray (MCMICRO/Galaxy-ME, recommended) vs dearray-then-register-per-core (VALIS's TMA example). Mirage's VALIS investment favors the former.
- **Grouping-key refactor risk:** Mirage's `patient_id`-centric grouping and "one reference per patient" rule are load-bearing; TMA support means one slide → many cores → many (sub)patients. This is the real implementation cost, not the CV.
- **Core→patient ergonomics:** how users supply the core→sample map (QuPath `.qpmap` vs an extra samplesheet CSV) is unresolved and worth a design decision.
- **Thin/unverified evidence:** STiLE (⚠️ unreviewed preprint), and some tools' authorship/claims could not be fully verified — flagged inline.

---

## Sources

### Papers
- Wang et al. 2011, *PLoS One* — "A TMA De-Arraying Method for High-Throughput Biomarker Discovery." DOI [10.1371/journal.pone.0026007](https://doi.org/10.1371/journal.pone.0026007)
- Ge, Saha, Duggan et al. 2024 — "TMA-Grid: FAIR TMA De-arraying." arXiv:[2407.21233](https://arxiv.org/abs/2407.21233)
- STiLE 2026 (bioRxiv ⚠️ preprint) — "Automated TMA Dearraying for Spatial Transcriptomics." DOI [10.64898/2026.03.17.712359](https://www.biorxiv.org/content/10.64898/2026.03.17.712359v1)
- mNet 2023 — DL framework for MALDI-MSI TMAs. PubMed [37350928](https://pubmed.ncbi.nlm.nih.gov/37350928/)
- Automated procedure for digital images in large-scale TMA experiments, 2005. PubMed [15979757](https://pubmed.ncbi.nlm.nih.gov/15979757/)
- Gatenbee et al. 2023, *Nat Commun* — "Virtual alignment of pathology image series (VALIS)." DOI [10.1038/s41467-023-40218-9](https://doi.org/10.1038/s41467-023-40218-9)
- Schapiro, Sokolov, Yapp et al. 2022, *Nature Methods* 19:311–315 — "MCMICRO." DOI [10.1038/s41592-021-01308-y](https://www.nature.com/articles/s41592-021-01308-y)
- Goldsborough et al. 2024 — "InstanSeg." arXiv:[2408.15954](https://arxiv.org/abs/2408.15954)
- Multiplex tissue imaging review, 2024 — arXiv:[2411.00948](https://arxiv.org/abs/2411.00948)
- Hybrid YOLOv11/StarDist/SAM2, 2025, *Bioengineering* (MDPI) — [PMC12189375](https://pmc.ncbi.nlm.nih.gov/articles/PMC12189375/)
- CellSeg, 2022, *BMC Bioinformatics* — [10.1186/s12859-022-04570-9](https://bmcbioinformatics.biomedcentral.com/articles/10.1186/s12859-022-04570-9)
- Multiplex registration review, 2024, *Br J Cancer* — [10.1038/s41416-024-02882-6](https://www.nature.com/articles/s41416-024-02882-6)

### Repos / Docs
- [labsyspharm/mcmicro](https://github.com/labsyspharm/mcmicro) — 154★, MIT, active. Nextflow TMA pipeline (register→dearray→segment→quantify).
- [nf-core/mcmicro](https://github.com/nf-core/mcmicro) — 32★, MIT, active. `--tma_dearray` fan-out reference. [Usage docs](https://nf-co.re/mcmicro/dev/docs/usage/)
- [nf-core coreograph module](https://nf-co.re/modules/coreograph/) — reusable DSL2 module (Coreograph 2.2.9).
- [HMS-IDAC/UNetCoreograph](https://github.com/HMS-IDAC/UNetCoreograph) — 7★, MIT, active. Container `labsyspharm/unetcoreograph`.
- [qupath/qupath](https://github.com/qupath/qupath) — 1,390★, GPL-3.0, active. Headless TMA dearrayer + TMA data model. [CLI docs](https://qupath.readthedocs.io/en/stable/docs/advanced/command_line.html), [TMAGrid javadoc](https://qupath.github.io/javadoc/docs/qupath/lib/objects/hierarchy/TMAGrid.html)
- [MathOnco/valis](https://github.com/MathOnco/valis) — 226★, MIT, active. Registration (Mirage's current engine).
- [labsyspharm/ashlar](https://github.com/labsyspharm/ashlar) — 176★, MIT, active. [labsyspharm/palom](https://github.com/labsyspharm/palom) — 59★, active. [NHPatterson/wsireg](https://github.com/NHPatterson/wsireg) — 102★, near-dormant.
- [instanseg/instanseg](https://github.com/instanseg/instanseg) — 225★, Apache-2.0, active. Per-core segmentation.
- [episphere/tma-grid](https://github.com/episphere/tma-grid) — 4★, MIT (browser-only). [Huang-AI4Medicine-Lab/stile](https://github.com/Huang-AI4Medicine-Lab/stile) — 0★, Apache-2.0 (immature).
- [Galaxy-ME TMA tutorial](https://training.galaxyproject.org/training-material/topics/imaging/tutorials/multiplex-tissue-imaging-TMA/tutorial.html) — end-to-end TMA workflow (non-Nextflow, same module logic).
- [OME-NGFF plate/well model](https://ome-model.readthedocs.io/en/stable/developers/screen-plate-well.html) / [ngff PR#24](https://github.com/ome/ngff/pull/24) — closest existing spec for tagging cores.

*Star counts, licenses, and last-commit dates verified via `gh`/Docker Hub API on 2026-07-01. Unverifiable items flagged inline; nothing fabricated.*
