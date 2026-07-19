# MIRAGE: a reproducible, open-source Nextflow pipeline for cross-panel registration and single-cell analysis of multiplexed immunofluorescence whole-slide images

*Short running title:* [Author to finalise — e.g. "MIRAGE: cross-panel mIF WSI pipeline"]

# Abstract

**Background.** Mapping the tumour immune microenvironment at single-cell, spatial resolution is
done with multiplexed immunofluorescence (mIF) on whole-slide images (WSI). Turning
raw multiplexed WSI into analysis-ready single-cell data is a multi-stage computational problem —
illumination correction, registration, segmentation, quantification, and export — whose results
must be reproducible across sites, the bar a pipeline contribution must clear. Existing end-to-end
pipelines either assume pre-aligned inputs or register only successive cycles of a single
acquisition; none automate the registration of multiple separately-acquired mIF panels into one
coordinate space.

**Results.** MIRAGE is a Nextflow DSL2 pipeline that carries raw multiplexed WSI through to
single-cell data: illumination and background correction, DAPI-anchored cross-panel registration,
StarDist segmentation, single-cell quantification, and export to GeoJSON. The pipeline is
interoperable with QuPath and with FlowPath, an interactive gating tool. Broad marker coverage is
built up by iterative
staining, stripping, and re-staining of one physical section, with DAPI imaged in every round; this
shared nuclear anchor is what makes registering the separately-acquired panels — and joining their
marker measurements per cell — biologically valid. In an internal cross-arm comparison, full
rigid-plus-non-rigid registration substantially improved cross-panel DAPI overlap (measured by Dice)
relative to rigid-only and no-registration baselines; we report this comparison rather than a
head-to-head against other pipelines. In a head-and-neck cancer proof-of-concept, the
imaging-derived immune fraction was higher in transcriptomically "hot" cases and concordant with an
orthogonal RNA-deconvolution estimate of the same immune content.

**Conclusions.** MIRAGE is a novel, fully open-source, reproducible, end-to-end mIF WSI pipeline,
portable across high-performance-computing environments via Nextflow DSL2, that generalises the
established nuclear-anchor registration idea to automated cross-panel, cross-acquisition
registration. Its modular design and open GeoJSON contract make it straightforward to reuse and to
integrate with downstream spatial-analysis tools. The head-and-neck application is a
proof-of-concept concordance on a single, contrast-enriched cohort, not a clinical validation.
MIRAGE is freely available under an open-source licence.

# Background

Mapping the tumour immune microenvironment at single-cell, spatial resolution requires
measuring many protein markers on the same tissue, so that each cell can be phenotyped in the
context of its neighbours. Multiplexed immunofluorescence (mIF) meets this need on whole-slide
images (WSI) — single, potentially gigapixel-scale scans of an entire tissue section imaged for a
set of antibody markers rather than a small field of view. Because one mIF acquisition resolves
only a limited number of markers at once, broad marker coverage is built up iteratively on the
*same physical section*: stain, image, **strip** the antibodies, then re-stain and image again
(antibody stripping, not photobleaching or quenching). Every panel therefore images the same
cells, which is what makes per-cell, multi-marker joins across panels biologically valid [24][25].
The wet-lab realisation of this
protocol is described in the next section; here it establishes why bringing the
separately-acquired panels into one coordinate space — registration — is both meaningful and
necessary.

Turning raw multiplexed WSI into an analysis-ready single-cell table is a multi-stage
computational problem. Tiles must be corrected for uneven illumination and assembled, the
separately-acquired panels registered to one another, nuclei and cells segmented, per-cell marker
intensities quantified, and the result exported in a form downstream spatial-analysis tools can
read. Each stage is individually non-trivial, and at gigapixel scale the full sequence is memory-
and compute-intensive, typically bound to high-performance-computing (HPC) infrastructure — which
makes such workflows notoriously hard to reproduce. Portable, containerised workflow engines, of
which Nextflow [11] is a widely adopted example, are the accepted answer: they pin software
environments and execution logic so that the same pipeline runs identically across machines. A
contribution in this space is therefore judged less on any single algorithm than on whether the
whole chain — raw images to single-cell tables — runs end to end on another group's data and
hardware. For a methods contribution of this kind, reproducible, open-source, end-to-end execution
is not a bonus feature but the bar the contribution must clear.

Several strong open-source pipelines already carry multiplexed imaging through to single-cell
data. **MCMICRO** [1] is a modular, end-to-end pipeline for multiplexed tissue imaging that
operates at whole-slide scale and delegates its registration step to ASHLAR. **ASHLAR** [2]
performs coordinated stitching and cross-*cycle* registration into a single gigapixel mosaic,
aligning the successive imaging cycles of one run using the nuclear (Hoechst) counterstain as a
shared anchor — the same conceptual idea MIRAGE builds on with DAPI. **SOPA** [3] is a
technology-invariant spatial-omics pipeline (built on Snakemake, not Nextflow); it carries no
automated registration engine of its own, expecting modality alignment to be performed manually
beforehand. **Steinbock** [4] provides a containerised end-to-end toolkit for imaging mass
cytometry, in which all channels are captured simultaneously and are therefore inherently
co-registered, so no cross-channel alignment step arises. Both MCMICRO and MIRAGE are built on
Nextflow — a shared engineering choice, not a distinguishing feature.

What none of these pipelines does is automatically register multiple *separately-acquired* mIF
panels into one coordinate space. ASHLAR and MCMICRO register the cycles of a single
cyclic-imaging run on one physical slide, where slide placement between cycles is near-identical;
SOPA leaves alignment to a manual, external step. Separately-acquired panels are a harder case:
distinct acquisition sessions introduce larger, non-rigid deformations between images that a
within-run assumption does not cover. MIRAGE closes this gap by generalising the nuclear-anchor
registration idea — established by ASHLAR within a single run — to automated cross-panel,
cross-acquisition registration. It does so with VALIS [5], which performs rigid followed by
non-rigid alignment. The DAPI nuclear counterstain, imaged in every panel, provides the common
landmark shared across acquisitions against which the separately-acquired panels are brought into
one coordinate space. VALIS was benchmarked for registration accuracy on the ANHIR
histological-image-registration challenge [6]. Among these tools, only SOPA genuinely requires
pre-aligned input, since its alignment step is manual [3]; ASHLAR and MCMICRO, by contrast, do
perform automated registration, albeit confined to the cycles of a single run.

Building on this, MIRAGE is a novel, fully open-source, reproducible end-to-end pipeline,
implemented in Nextflow DSL2, that runs the complete sequence from raw multiplexed WSI to
single-cell data: illumination correction, cross-panel registration, segmentation,
single-cell quantification, and export to GeoJSON. Its headline contribution is the automated
cross-panel, cross-acquisition registration described above; the pipeline's interoperability with
established tools and its portability across HPC environments are supporting features, not the
central claim. MIRAGE's GeoJSON output is consumed downstream by **FlowPath**, an interactive
gating tool that calls cell populations from the quantified objects; the two are applied jointly
in a head-and-neck cancer proof-of-concept that examines *concordance* among orthogonal immune
proxies and is explicitly not a formal validation. The remainder of the paper is organised as
follows: we first present the wet-lab mIF acquisition protocol (Fig 1), then the MIRAGE pipeline
and its interoperability with downstream analysis (Figs 2–3), and finally the head-and-neck
proof-of-concept application of MIRAGE and FlowPath together (Fig 4). The component tools
underpinning individual stages — StarDist [9] for segmentation, QuPath [12] for GeoJSON
interoperability, and Bio-Formats [13] for image ingest — are detailed in the Implementation and
Methods sections.

# Experimental protocol

The protocol builds broad marker coverage on a single tissue section iteratively, because any one
mIF acquisition resolves only a limited number of markers at once. Throughout, the **same
physical section** is carried through repeated cycles of stain, image, **strip**, and re-stain
(Fig 1a): the section is stained for a small set of antibodies and imaged, the antibodies are then
stripped from the tissue — antibody stripping, not photobleaching or quenching — and the section
is re-stained for the next set. Each such cycle constitutes one **round**, and each round acquires
one **panel** of approximately two antibody markers together with a DAPI nuclear counterstain that
is re-imaged in every round. Ten rounds yield ten panels and roughly twenty markers in total.
Because round and panel stand in one-to-one correspondence, we use *round* when describing the
sequence over time (round 1 through round 10) and *panel* when describing the ten
separately-acquired images that must be brought into a common coordinate space; the two terms name
the same acquisition from two viewpoints.

This design has one load-bearing consequence. Because every panel images the **same physical
cells**, the per-cell measurements from different panels can be joined into a single multi-marker
profile per cell, and that join is **biologically valid** rather than a geometric coincidence.
This is what makes cross-panel registration meaningful: aligning the panels recovers, for each
cell, a phenotype spanning all roughly twenty markers that no single acquisition could measure.
Representative multiplexed images from the protocol are shown in Fig 1b.

Producing successive panels comes at a cost to spatial alignment. Between rounds the slide is
unmounted, processed, and rescanned, so consecutive panels are displaced relative to one another
by whole-slide translation and rotation and by non-rigid deformations of the tissue. This
cross-panel drift — not any within-scan artefact — is precisely what the downstream registration
step must correct, and the DAPI counterstain, present in every round, provides the common landmark
shared across all panels against which they can be aligned. How the panels are registered is the
subject of the pipeline description (Implementation; Fig 2); here we establish only that the
acquisition design is what both necessitates registration and, through the shared DAPI channel,
makes it possible.

Two controls establish that iterative staining on one section is clean (Fig 1c, d). The first is a
**stripping-completeness control**: after stripping, the section is incubated with secondary
antibody alone and imaged (Fig 1c). Absence of fluorescence shows that the previous primary
antibody has been removed, leaving no binding sites for a new secondary and therefore no
bleed-through of one round's stain into the next. The second is a **carryover control**: the
section is imaged after stripping but before re-staining (Fig 1d), confirming that residual signal
has returned to background and will not contaminate the following panel.

These controls demonstrate antibody **removal** — that stain from one round does not persist into
the next. They do not, however, demonstrate that the underlying epitopes **survive** repeated
stripping, which is a separate concern: a marker assigned to a late round could read falsely
negative if the tissue or its antigens have degraded by that point. Removal and survival are
distinct properties, and the completeness control speaks only to the former.

Epitope survival across rounds is therefore monitored explicitly. Because a full series runs to
ten sequential rounds, each requiring several hours of staining and imaging, a factorial
experiment re-running markers in early versus late positions to measure order-robustness directly
was impractical; instead, degradation is assessed retrospectively from the acquired data and
disclosed, rather than controlled experimentally. Two readouts are used. **Per-round DAPI signal
retention** across the ten rounds serves as a tissue-integrity gauge, since DAPI is re-applied
every round and a systematic decline would indicate progressive tissue loss (the full per-round
retention plot is provided as an Additional file; the main text reports its trend). A
**biological-consistency check** — confirming that CD8⁺ cells remain a subset of CD3⁺ cells —
provides an incidental check that these markers still behave as expected in the rounds to which
they are assigned.

The marker-to-round assignment was fixed empirically across the series.
*[EXPERIMENTALIST TO SUPPLY: justification for the marker-to-round ordering.]*

Two considerations frame the residual risk. Because the panel composition and round order were
held **fixed across all six cases** in the proof-of-concept application (Fig 4), any
degradation-related bias is constant across cases and cannot, by construction, manufacture a
difference between them. As an explicit limitation, we nonetheless cannot exclude some attenuation
of signal for markers assigned to the latest rounds.

Together, the completeness and carryover controls establish that the panels are cleanly separated
in the staining dimension, while the degradation readouts bound their reliability across rounds.
What remains is to align the ten separately-acquired panels — displaced by the unmount-and-rescan
drift described above — into the single coordinate space in which per-cell profiles can be
assembled. That registration step, and the pipeline that performs it, are the subject of the next
section.

# Implementation

MIRAGE is a Nextflow DSL2 [11] pipeline that carries raw multiplexed whole-slide images (WSI)
through to an analysis-ready single-cell table. It runs as three sequential Nextflow subworkflows —
preprocessing, registration, and postprocessing — spanning five conceptual stages: illumination and
background correction, cross-panel registration, segmentation, single-cell quantification, and
GeoJSON export (segmentation, quantification, and export together form the postprocessing
subworkflow). Each stage's processes execute in their own containers so that the
stage graph is portable, resumable, and reproducible across machines. The stage graph is shown
schematically in Fig 2(a). Of these modules, cross-panel registration is the headline capability;
the remaining stages are robust, standard implementations assembled into one end-to-end workflow.
The paragraphs below describe what each stage does; the exact parameter values and run settings
live in Methods, and the full per-module configuration table in Additional files.

## Illumination and background correction

The first module, `PREPROCESS`, prepares each acquired panel for quantitative comparison. Uneven
illumination across the imaging field is corrected with BaSiC flatfield/darkfield shading
correction [7]. Because whole-slide images are assembled from many overlapping tiles, this
correction also suppresses the tile-boundary intensity seams left by mosaicing (Fig 2b) — the accurate
meaning of "stitching defects" in this setting. The seam suppression is a *side-effect* of
illumination correction rather than a distinct capability: MIRAGE has no standalone
stitching or seam-correction module.

*[author to correct: the "local background subtraction" default and the "optional per-pixel
AF-subtraction path" described in this paragraph are not implemented — `PREPROCESS` performs only
BaSiC flatfield+darkfield shading correction, with no background- or autofluorescence-subtraction
step and no blank/AF image input. Reframe the autofluorescence limitation as an uncorrected
residual of that shading correction, not a data-availability choice.]*

Quantitative multi-panel intensity is the pipeline's central measurement, so `PREPROCESS` also
addresses background signal, which is substantial in the autofluorescence-heavy (AF) FFPE
head-and-neck tissue this protocol targets. The default is **local background
subtraction** — a per-cell, local estimate of background that requires no additional acquisition,
chosen so that the correction applies to any input without a supporting blank exposure — with an
**optional per-pixel AF-subtraction path** that is activated when a blank/AF image is supplied and
removes autofluorescence estimated directly from that image. BaSiC illumination correction is
orthogonal to both (it corrects spatial shading, not the autofluorescent signal itself) and always
runs ahead of them. We note one limitation here and expand it in the Discussion: the acquisition
protocol does not capture a pre-stain blank cycle and cannot add one retroactively, so
gold-standard blank subtraction is unavailable and residual autofluorescence remains an explicit
limitation of the quantitative readout.

## Cross-panel registration

Registration is MIRAGE's load-bearing contribution. A patient is imaged as *multiple
separately-acquired mIF panels* — each a distinct multiplexed acquisition carrying its own set of
antibody markers — and MIRAGE brings these panels into one shared whole-slide coordinate space
automatically. This is the gap left open by existing
multiplexed-imaging pipelines, as the Background sets out: they register the successive cycles of a
single cyclic-imaging run, expect pre-aligned input, or capture all channels simultaneously — none
automatically registers multiple separately-acquired panels. That case is harder because distinct
acquisition sessions introduce larger, non-rigid deformations than a within-run assumption covers.
MIRAGE generalises the nuclear-anchor registration idea — established by ASHLAR within a single
run — to this setting. The generalisation is at the level of the *pipeline*, not the algorithm:
MIRAGE packages whole-slide registration into an automated, reproducible, cross-panel,
cross-acquisition end-to-end workflow, rather than modifying the underlying registration method.

Mechanically, registration is driven by VALIS [5]. Each patient's panels are aligned directly onto
a **single fixed reference panel** — a star-to-reference topology in which every moving panel is
warped onto the one reference, rather than traversed through a chain of pairwise alignments. The
reference panel is designated per patient in the input samplesheet (an `is_reference` flag; absent
one, the pipeline falls back to a DAPI-based choice). VALIS performs SuperPoint/SuperGlue feature
matching to drive a rigid alignment followed by a non-rigid refinement, with an optional
micro-registration step for fine correction. The nuclear counterstain, DAPI, is the biological
anchor that makes this possible: it is present in every panel because the protocol images the
*same physical section*, stripping and re-staining antibodies between acquisitions (stain → image →
**strip** → re-stain, antibody stripping rather than photobleaching or quenching). DAPI is the
common channel shared across all of a patient's panels — the landmark, present in every
acquisition, against which the separately-acquired panels are brought into register.

Because every panel images the same physical cells, registering them into one coordinate space
allows marker measurements from separate acquisitions to be joined per cell rather than only per
region, so that a cell's full marker profile can be assembled across panels. The registration
presets and micro-registration settings used for the results reported here are given in Methods;
registration accuracy is quantified in Results (Fig 2).

## Segmentation

Segmentation runs on the registered images. The default segmenter is StarDist [9], applied to the
DAPI channel to detect nuclei as star-convex polygons; whole-cell masks are then derived by
**non-overlapping label expansion** of the nuclei outward by a fixed radius, in which expanding
labels stop on collision so that spillover between neighbouring cells is bounded to their shared
borders rather than allowed to overlap. This keeps each cell's footprint disjoint from
its neighbours', which matters for the intensity measurements the next stage draws from these
masks. The expansion radius is a Methods/Additional-files parameter. Nuclear expansion is well
suited to the DAPI-anchored panels used here, but is less appropriate where a membrane marker
defines the cell boundary directly; for such membrane-heavy panels MIRAGE ships **InstanSeg** [14]
as a selectable, genuinely cell-aware alternative that segments nuclei and whole cells directly. A
third backend, **CellSAM** [15], is also selectable but, as configured here, segments the nuclear
channel and derives the cell mask by the same label-expansion step as StarDist rather than from a
membrane boundary. Both are capabilities rather than defaults. StarDist's
generalisation to this tissue and scanner on the DAPI channel is assumed rather than independently
established here; this assumption is flagged in the Discussion and in the pre-submission checklist
for independent verification.

## Single-cell quantification

The quantification module reads per-cell marker intensities from the segmentation masks and emits
a per-cell feature table. In the recommended phenotyping configuration it computes, for **every**
marker, intensity summaries on three compartments — the **Nucleus**, the **Cytoplasm** ring
(Cell − Nucleus), and the whole **Cell** — reporting both mean and **median** per compartment.
Providing per-compartment summaries lets a downstream analysis read each marker from the
compartment where its signal is expected — a nuclear signal from the Nucleus summary rather than
diluted by signal-free cytoplasm, a membrane signal from the Cytoplasm ring rather than by the
nuclear area — a **compartment-matched** read that MIRAGE enables but does not itself hard-code.
The median (rather than the mean) is preferred for this because it is robust to spillover from
bright autofluorescent or neighbouring cells. Summarising each marker over the compartment where
its signal is expected, with a spillover-robust statistic, is intended to keep per-cell intensities
faithful to the underlying marker signal rather than to neighbouring or autofluorescent
contamination. This configuration is
not the default: compartment medians are emitted only with a
specific, non-default flag combination (`quantify_compartments` together with
`expanded_quantification`), and it is this combination that MIRAGE documents as the recommended
configuration for phenotyping. The exact flags and values, and a default-versus-recommended
configuration comparison, are given in Methods and Additional files. Implementation stops at the
per-cell quantification: the downstream phenotype tree that consumes this table — strict sequential
gating into terminal populations — is described where it is applied (Results).

## GeoJSON export and downstream interoperability

The final module exports the quantified objects — cell geometries together with their per-cell
feature values — as **GeoJSON**, the interoperable hand-off from MIRAGE to downstream
spatial-analysis tools. Exporting to a widely-read, tool-agnostic
format rather than a bespoke one is what lets MIRAGE's output feed established downstream software
without conversion. Because GeoJSON is QuPath-native, MIRAGE results open directly in QuPath [12]
for inspection and annotation (a supporting interoperability feature). The same GeoJSON is consumed
by FlowPath, an interactive gating tool that thresholds the exported per-cell features to call cell
populations; this interoperability is shown at the interface level in Fig 3 (GeoJSON → interactive
gating → populations). MIRAGE's responsibility ends at emitting the GeoJSON; FlowPath's gating
logic is not part of the pipeline and is not designed or assessed here.

## Reproducibility backbone

MIRAGE's reproducibility rests on concrete, present-and-working infrastructure rather than
assertion. The pipeline is written in
**Nextflow DSL2** (≥25.04.0) [11], giving modular, resumable, portable execution in which each
stage is an independently containerised process. Every module runs in a **versioned container**
whose Dockerfile pins its exact dependency versions, so each step's software environment is
specified rather than assumed. An **`nf-test` suite** (141 test cases across 33 files) is exercised in continuous
integration, making the pipeline's wiring an executable, checked artifact rather than an asserted
one. The pipeline ships **HPC and configuration profiles** (Docker, Singularity, Conda, SLURM, and
site profiles) so that the same workflow runs from a laptop to a cluster without code change.

We state two boundaries plainly, both disclosed further in Availability and requirements. First,
the continuous-integration merge gate exercises the pipeline's structure (stub and unit-level runs
on synthetic data); it does not gate end-to-end segmentation or quantification *scientific*
correctness, which remains advisory. Second, the custom StarDist model weights used for the
head-and-neck application are external to the repository — not committed, checksummed, or archived
alongside the pipeline — so they are not among the reproducibility guarantees the pipeline itself
carries; their archival is tracked as a submission-readiness item. The container images, exact
software versions, licences, and repository/DOI details are given in Availability and requirements;
here the argument is architectural — reproducibility is engineered into the pipeline's structure.

# Results and Discussion

We evaluated MIRAGE along two independent lines and report them together so that each result sits
next to its interpretation. The first is a **registration-accuracy benchmark** (Fig 2c–d): a
pipeline-quality result that asks whether MIRAGE brings separately-acquired panels into register
and whether the configuration used here is justified among the alternatives. The second is a
**proof-of-concept concordance analysis** in head-and-neck cancer (Fig 4): a check of whether the
imaging-derived immune quantification recovers, and co-varies with, orthogonal transcriptomic
estimates of the same immune content. Methods gives the procedures that produced every number
reported below; this section gives the numbers and what they mean.

## Registration accuracy

Registration is MIRAGE's load-bearing capability, so we quantified it directly. Qualitatively, a
two-colour DAPI overlay of two separately-acquired panels of the same physical section shows the
cross-panel drift between the two acquisitions, and shows that drift resolved once the panels are
aligned into one coordinate space (Fig 2c). To put a number on
that alignment, we compared a matrix of registration configurations on the same data: two VALIS
accuracy presets — which differ in the image resolution at which features are matched, not in the
underlying feature matcher — crossed with micro-registration on or off, against two baselines, a
**rigid-only** alignment and **no registration** at all. The full per-arm results are given in
Additional files; here we report the summary and what it supports (Fig 2d).

We assessed each arm with two metrics that do two different jobs, because no single metric answers
both questions honestly. The first, **VALIS-internal target registration error (TRE)**, is the
residual distance between the keypoints VALIS matched between panels after alignment. It ranks the
arms cleanly and so serves as the **method-selection** metric. But it is computed on the same
correspondences VALIS optimised against, and only on regions where matching succeeded, so it
excludes exactly the hard regions where registration is most likely to fail. It is therefore an
optimistic, self-reported quantity that **cannot stand on its own as a claim about absolute
alignment accuracy**. The second metric closes that gap. We binarised the DAPI channel of each
aligned panel and measured the **Dice overlap** of the resulting nuclear masks across panels — a
quantity computed independently of VALIS's feature matcher, on the raw aligned images, and uniform
across every arm including the baselines. Because DAPI is present in every panel and is not the
object VALIS optimises against, Dice provides a **VALIS-agnostic** check that either corroborates
the TRE ranking or exposes it as circular. The exact rule used to binarise the DAPI channel before
computing Dice is stated in Methods [author to supply: DAPI binarisation rule — per-cycle Otsu vs
fixed threshold; not recoverable from the pipeline repository].

The two metrics agree, which is the point. Registration substantially improved cross-panel DAPI
overlap over both baselines: the no-registration and rigid-only arms left the nuclear masks visibly
and quantitatively misaligned, and full rigid-plus-non-rigid registration raised Dice to
[author to supply: best-arm Dice] from [author to supply: rigid-only Dice] and
[author to supply: no-registration Dice] respectively, with a corresponding fall in TRE (Fig 2d;
full per-arm TRE and Dice in Additional files). The best-performing arm was
[author to supply: winning arm — VALIS preset and micro-registration state, from the Fig 2
benchmark], and this is the configuration whose settings are recorded in Methods
[author to supply: confirm this arm matches the configuration used to generate the Fig 4 data —
the pipeline ships medium-preset, micro-registration-off defaults, but the run configuration is
overridable and the Fig 4 invocation is not committed to the repository]. Crucially, because an
independent metric moves in step with the internal one, the ranking is not an artefact of VALIS
scoring its own work, and the absolute-accuracy statement rests on the VALIS-agnostic Dice rather
than on VALIS's self-report.

Two points bound what this benchmark claims. First, the engine choice: we drive registration with
VALIS [5], a fully automated, open-source whole-slide method reported to achieve state-of-the-art
accuracy on the ANHIR histological-image-registration benchmark [6]. That external standing
justifies the *choice of tool*; it does not transfer to MIRAGE's own accuracy, which is what the
Dice metric is for — ANHIR benchmarked VALIS's transforms against hidden landmarks on a different
modality and scale, not on our same-section iterative whole-slide setting. Second, the scope: this
is an **internal cross-arm comparison**, not a head-to-head against other pipelines, and we make no
relative-performance claim against them. MIRAGE's positioning is instead a capability difference —
it generalises the established nuclear-anchor registration idea, used within a single run by
ASHLAR [2], to automated cross-panel, cross-acquisition registration — a scope argument set out in
full in the Background, not a benchmarked speed or accuracy win over any named tool.

## Proof-of-concept concordance in head-and-neck cancer

Having established that registration works, we asked whether the single-cell immune quantification
built on top of it is trustworthy. We applied MIRAGE and the downstream FlowPath gating together to
six head-and-neck cancer cases, drawn from a larger profiled cohort as **three among the highest and
three among the lowest** on a transcriptomic immune score (hotscore). This is a deliberately
**contrast-enriched** selection, not an unselected sample; we return to what that costs us below.
Both anchors we compare against — the hotscore and the RNA-seq deconvolution fractions — derive from
the **same adjacent-section bulk RNA-seq** — a single shared molecular source. Imaging populations were called by
strict sequential hierarchical gating in FlowPath on the per-cell feature table MIRAGE exports, with
a CD45-only pan-immune gate; the T and NK lineages resolve at single-cell level, while myeloid and B
compartments are recovered only in aggregate (Methods). Neither anchor is a ground truth: the
hotscore and the deconvolution fractions are both **orthogonal molecular proxies** for immune
content, so the analysis tests concordance among proxies, not validation against a gold standard.

**Imaging recovers the transcriptomic contrast (Fig 4a).** The headline result compares the imaging
CD45⁺/all-cells fraction between the three transcriptomically hot and three cold cases, with the six
individual case values overlaid on the boxplots so the reader sees the actual data rather than a
summary of it. The imaging-derived immune fraction was consistently higher in the hot group
[author to supply: hot-group and cold-group imaging CD45⁺ fractions, e.g. medians and per-case
values], meaning that an independently measured imaging quantity recovers the hot/cold ordering that
was defined, before any imaging, by orthogonal transcriptomics. Two things keep this honest. The
groups are separated on the hotscore axis **by construction**: hotscore is the selection variable
(the immunologically active gene-expression signature of Foy et al. [23]; the highest-scoring cases are
labelled hot and the lowest cold, with no independent threshold), so it is the axis along which the
cases were chosen, not a Fig 4 finding, and the contrast-enriched design inflates the apparent gap.
We therefore report no p-value here: three-versus-three is too small to support an inferential test,
and one would misrepresent a group separation that the selection built in. The separation itself, shown as the
raw points, is the evidence.

**Imaging quantification co-varies with deconvolution (Fig 4b).** The load-bearing result asks
whether the imaging immune fraction tracks an orthogonal molecular estimate case by case. We plotted,
for each of the six cases, the deconvolution immune fraction against the imaging fraction — Tier 1 of
the tiered comparison, total immune content, defined as the imaging CD45⁺/all-cells fraction against
one minus the quanTIseq "other" fraction [16] [author to supply: quanTIseq version and confirmation
of the lsei deconvolution mode]. Cases scored immune-high by imaging were immune-high by
deconvolution, and low by low: the two orthogonal estimates show **concordant ranking across cases**
[author to supply: the six paired (deconvolution, imaging) fractions]. This is the result that lets
us say the imaging quantification is trustworthy — an imaging-only measurement and a molecular-only
measurement of the same immune content place the six cases in the same order.

We claim rank co-variation and nothing stronger. The two axes use **different denominators** —
imaging counts a fraction of segmented *cells*, deconvolution estimates a fraction of a *bulk
expression mixture* — so the points are not expected to fall on the identity line, and we make no
claim of absolute-fraction agreement. Nor do we report a correlation coefficient, p-value, or
confidence interval: at n = 6 any such statistic would overstate the precision of what is a
six-point visual, and the concordance is honest only as a described pattern, not as an inferential
estimate. The mappable-lineage (Tier 2: CD8 T, CD4 conventional T, CD4 regulatory T, and NK cells)
and non-T/NK catch-all (Tier 3) comparisons are directionally consistent with Tier 1 and are shown,
as the same descriptive scatters, in Additional files.

Read together, the two panels give two complementary lines of evidence that the MIRAGE + FlowPath
imaging quantification is trustworthy as a proof of concept: an imaging measurement **recovers an
orthogonal hot/cold transcriptomic contrast** (Fig 4a) and **co-varies with an orthogonal molecular
estimate** of the same immune content (Fig 4b). The two transcriptomic anchors are not independent
of each other — both come from the same adjacent-section RNA-seq — so the independence that matters
is between the imaging readout and the molecular proxies it is checked against. Both panels show
concordance among proxies. Neither is a validation against
ground truth, and the design does not support one.

## Limitations and future work

The results above are bounded by several limitations, which we state plainly because they set the
terms on which the proof-of-concept should be read. **Autofluorescence** is the first: the
acquisition protocol did not capture a pre-stain blank image, so we applied local background
subtraction rather than per-pixel autofluorescence subtraction (Implementation). This is a
run-condition limitation, not a design choice — MIRAGE supports blank-based subtraction when the
image is available — but it means residual autofluorescence remains in the quantitative readout for
this dataset. *[author to correct: MIRAGE does not in fact perform local background subtraction or
support blank-based/AF subtraction — its only intensity correction is BaSiC flatfield+darkfield
shading correction. Reframe this limitation as residual autofluorescence that the pipeline does not
correct at all, rather than a subtraction method chosen for want of a blank image.]* **Panel coverage** is the second, and it directly shapes Fig 4: the antibody panel
carries a CD45-only pan-immune gate and no myeloid (CD68/CD163/CD14) or B-cell (CD19/CD20) markers,
so those compartments are resolved only in aggregate, never at single-cell level, and the Tier 3
comparison is correspondingly coarse. Third, **StarDist generalisation** [9] to this mIF/DAPI tissue
and scanner is assumed rather than independently benchmarked here; we flag it for verification rather
than assert it.

The proof-of-concept statistics carry the heaviest caveat. With **n = 6** and a
**contrast-enriched** selection, the design inflates *both* the Fig 4a group separation and the
Fig 4b co-variation, and precludes any inferential statistic; the findings are descriptive and
hypothesis-generating, not confirmatory. The registration benchmark is likewise internal: it
compares MIRAGE's own configurations against baselines and includes **no competitor head-to-head**,
so it supports no relative-performance claim.

These bounds map directly onto future work. A larger, **unselected** cohort would move the analysis
from proof-of-concept concordance toward quantitative agreement and allow the inferential statistics
this study deliberately withholds, and a broader antibody panel — adding myeloid and B-cell
markers — would resolve those compartments at single-cell level rather than in the Tier 3 aggregate.
Two pipeline parameters warrant systematic study we have deferred here: the whole-cell mask
**expansion radius** (`seg_expand_distance`), whose effect on downstream quantification we have not
swept, and the robustness of gating where marker intensities are **unimodal** rather than cleanly
separated. Together these define the path from a reproducible pipeline with a promising
proof-of-concept toward quantitative immune profiling on an independent cohort.

# Conclusions

MIRAGE is, to our knowledge, the first pipeline to register multiple separately-acquired
multiplexed immunofluorescence (mIF) panels into a single coordinate space automatically; it does
so at whole-slide, gigapixel scale. It achieves this not by introducing a new alignment principle
but by generalising the established nuclear-anchor registration idea to a harder setting. The DAPI
counterstain, imaged in every strip-and-restain cycle, is the common landmark shared across
panels — the same anchor that within-run pipelines such as ASHLAR [2] use to align the successive
cycles of a single acquisition — and MIRAGE generalises it from that within-run case to registration
across separate acquisitions.

This capability sits inside a complete, reproducible workflow rather than standing alone. MIRAGE is
a fully open-source, end-to-end mIF pipeline that runs the whole sequence from raw whole-slide
images to analysis-ready single-cell data — illumination correction, registration, segmentation,
single-cell quantification, and export to GeoJSON — implemented in Nextflow DSL2 and portable
across high-performance-computing (HPC) environments. For a methods contribution of this kind, the
property that matters is that the entire chain — not merely a single stage — runs identically on
another group's data and hardware, and it is the property we have prioritised: MIRAGE pins its
software environments and execution logic so that the full workflow is reproducible end to end.

We have also shown, as a proof of concept, that this output is usable directly downstream. MIRAGE's
GeoJSON per-cell tables feed without reformatting into interactive spatial analysis, illustrated
here with FlowPath — an exemplar consumer of that output, not a co-equal deliverable. Applied
together to head-and-neck cancer cases, the two tools yielded gated immune populations that were
concordant with orthogonal transcriptomic proxies for the same immune content: an immunologically active
gene-expression signature and RNA-seq deconvolution fractions. We frame this result deliberately: it is
a concordance demonstration on a single, contrast-enriched cohort, not a clinical validation, and
the design supports nothing stronger.

Because MIRAGE is built as modular Nextflow components around an open GeoJSON output contract, it
is straightforward to extend to additional panels, markers, and tissue types and to interoperate
with other spatial-analysis tools; we release it as an invitation to that reuse. MIRAGE is freely
available under an open-source licence, with repository, container, and version specifics given in
the Availability section.

# Methods

This section records the configuration MIRAGE was run with to produce the results reported
here, together with the two evaluation designs behind them — the registration-accuracy benchmark
(Fig 2) and the proof-of-concept concordance analysis (Fig 4) — and the case selection and
statistics for the latter. It is computational only: the mIF acquisition itself — antibody panel,
fixation, and the iterative stain–image–strip–re-stain chemistry — is described in the Experimental
protocol (Fig 1) and is not restated here. The pipeline's capabilities are described in
Implementation; the numbers these procedures produced are reported in Results; and concrete
software versions, container details, and repository or DOI identifiers are collected in
Availability and requirements.

## Pipeline configuration

MIRAGE was run as a Nextflow DSL2 [11] workflow, each module executing in its own dependency-pinned
container (pipeline version/commit and the container runtime are given in Availability and
requirements). The module settings used for this study were as follows.

**Illumination and background correction.** BaSiC flatfield and darkfield shading correction [7]
was applied to every panel. Background was removed with **local background subtraction**, the
default per-cell path, which requires no additional acquisition. The optional per-pixel
autofluorescence-subtraction path was **not** used: the protocol did not capture a pre-stain
blank/autofluorescence image, so blank-based subtraction was unavailable for this dataset. This is
a run-condition limitation of the input rather than a configuration choice, and its consequences
for the quantitative readout are taken up in Implementation and Results.

*[author to correct: as flagged in Implementation, the "local background subtraction" default and
the "per-pixel autofluorescence-subtraction path" do not exist in the code — the only intensity
correction MIRAGE applies is BaSiC flatfield+darkfield shading correction. Rewrite this paragraph to
state that, and reframe the autofluorescence limitation accordingly.]*

**Registration.** Panels were aligned with VALIS [5] using the default star-to-reference
configuration (`VALIS_ADAPTER`, `align_to_reference = true`), in which every moving panel is warped
directly onto a single fixed reference panel designated in the input samplesheet.
[author to supply: the production registration arm — VALIS accuracy preset and micro-registration
state — used to generate the Fig 4 data. The pipeline ships a medium-preset, micro-registration-off,
standard-adapter default, but the arm is overridable per run and the Fig 4 invocation is not
committed to the repository. Note that the Fig 2 benchmark compares the high and low presets (below),
so if the shipped medium default was the production setting it falls outside the benchmarked arms —
reconcile the production arm against the benchmark when filling this in.] The full arm matrix
compared in Fig 2 is an evaluation device (described next), not the production setting.

**Segmentation.** Nuclei were detected with StarDist [9] on the DAPI channel as star-convex
polygons, and whole-cell masks were derived by **non-overlapping label expansion** of each nucleus
outward by `seg_expand_distance = 10 px`, with expanding labels stopping on collision so that
neighbouring cell footprints stay disjoint.

**Single-cell quantification.** Quantification was run in the **recommended phenotyping
configuration**, which is not the pipeline default: both `quantify_compartments` and
`expanded_quantification` were enabled (both are false by default). With these flags set, MIRAGE
computes, for **every** marker, per-compartment intensity summaries over the **Nucleus**, the
**Cytoplasm** ring (Cell − Nucleus), and the whole **Cell**, reporting both mean and **median** (the
median being robust to spillover from bright autofluorescent or neighbouring cells). The pipeline
does not itself assign a marker to a single compartment; matching each marker to the compartment
where its signal is expected — FOXP3 to the Nucleus summary; CD45, CD3, CD8, CD4, CD56, GZMB, PANCK,
VIM and SMA to the Cytoplasm summary — is done downstream at analysis time by selecting the
corresponding compartment column from the exported feature table.

The complete per-module parameter table — BaSiC settings, background-correction parameters, the
full StarDist/InstanSeg/CellSAM options, the VALIS arm parameters, the quantification flags, and
the complete compartment-to-marker map — together with a default-versus-recommended configuration
comparison, is given in Additional files.

## Registration accuracy evaluation (Fig 2)

We selected the registration configuration by benchmarking a matrix of arms on the same data: the
two VALIS accuracy presets (high and low) crossed with micro-registration on or off, against two
baselines — a **rigid-only** alignment and **no registration** at all. The two presets differ only in the image
resolution at which features are matched, not in the underlying feature matcher (both use the same
SuperPoint/SuperGlue detector and matcher), so the matrix sweeps registration resolution and
refinement, not the registration algorithm. Per-arm results are reported in Results, and the full
per-arm table is given in Additional files.

Each arm was assessed with two metrics that answer two different questions. The first,
**VALIS-internal target registration error (TRE)**, is the residual distance between the keypoints
VALIS matched across panels after alignment; it ranks the arms cleanly and so serves as the
**method-selection** metric. Because it is computed on the same correspondences VALIS optimised
against, and only where matching succeeded, it is an optimistic self-report and cannot on its own
support a claim about absolute alignment accuracy. The second metric is independent of VALIS's
feature matcher: the **DAPI-channel overlap Dice coefficient**. After alignment, the DAPI channel
of each panel was binarised to a nuclear mask, and for each pair of panels we computed the Dice
overlap of those masks,

$$D = \frac{2\,|X \cap Y|}{|X| + |Y|},$$

where *X* and *Y* are the binarised DAPI nuclear masks of two aligned panels. Because DAPI is
present in every panel and is not the object VALIS optimises against, Dice provides a
**VALIS-agnostic** check that either corroborates the TRE ranking or exposes it as circular; it was
computed uniformly across every arm, including the baselines. [author to supply: the rule used to
binarise the DAPI channel before computing Dice — per-cycle Otsu versus a fixed intensity
threshold; this benchmark script is not committed to the pipeline repository and the rule is not
recoverable from it.]

We drive registration with VALIS [5] on external grounds: it is a fully automated, open-source
whole-slide method reported to achieve state-of-the-art accuracy on the ANHIR
histological-image-registration benchmark [6]. That standing justifies the *choice of tool* but
does not transfer to MIRAGE's own accuracy — ANHIR benchmarked VALIS's transforms against hidden
landmarks on a different modality and scale, not on our same-section iterative whole-slide setting,
which is precisely what the independent Dice metric is for.

## Phenotype calling

Cell populations were called from the per-cell compartment-median feature table that MIRAGE exports
as GeoJSON, by **strict sequential hierarchical gating** — successive single-marker gates rather
than Boolean combinations — yielding **13 terminal populations**, with a pan-immune gate defined on
**CD45 alone**. Gating was performed in FlowPath, the downstream interoperability consumer
(Implementation, Fig 3); we describe it here only at the interface level (GeoJSON feature table in,
gated populations out) and neither design nor assess FlowPath's gating logic.

The antibody panel sets a coverage limit that governs what Fig 4 can compare. The panel resolves
the T and NK axis (CD3, CD8, CD4, FOXP3, CD56, GZMB) at single-cell resolution, but carries **no
myeloid (CD68/CD163/CD14) and no B-cell (CD19/CD20) markers**, so those compartments cannot be
called individually and collapse into a single **CD45⁺CD3⁻CD56⁻ "Immune (non-T/NK)"** catch-all.
The remaining functional markers (CD74, L1CAM, P53, PDL1, PD1, ARID1A, FSP1) are used for
annotation only, not for phenotype calling.

## Proof-of-concept case selection and anchors (Fig 4)

The proof-of-concept analysis used **six head-and-neck cancer cases** drawn from a larger
transcriptomically profiled cohort as **three among the highest and three among the lowest** on the
hotscore — not a strict top-three/bottom-three cut. This is a deliberately **contrast-enriched**
selection: it widens the immune-content range spanned by only six cases and, by the same token,
limits generalisation, consistent with a proof of concept rather than a validation. Both anchors
compared against the imaging readout — the hotscore and the RNA-seq deconvolution fractions —
derive from the **same adjacent-section bulk RNA-seq** for these cases — a single shared molecular
source, so the two anchors are not independent of each other and neither is a ground truth against
which the imaging is validated. The hotscore is the immunologically active gene-expression
signature of Foy et al. [23] (high scores
label the hot cases and low scores the cold, with no independent threshold) [author to confirm: how
the Foy signature was computed on this cohort's RNA-seq — gene list and scoring detail].

Immune cell-type fractions were estimated from the adjacent-section bulk RNA-seq by **quanTIseq**
deconvolution [16] [author to supply: quanTIseq version and confirmation of the lsei deconvolution
mode → Availability and requirements]. quanTIseq is the Tier-1 anchor, with total immune content
taken as **1 − the quanTIseq "other" fraction**.

## Tiered mIF–deconvolution comparison (Fig 4)

Because the imaging panel and the deconvolution output resolve immune content at different
granularities, the imaging populations are compared to the deconvolution fractions at three nested
tiers of decreasing aggregation (Table 1). **Tier 1**, the pre-specified primary comparison, is
total immune content. **Tier 2** restricts the comparison to the lineages the panel resolves at
single-cell level, with deconvolution subsets summed up to the gate's resolution before comparison;
CD4 regulatory T cells are mapped to CD4⁺FOXP3⁺ only, and the CD8⁺ Treg leaf — biologically fringe
and without a deconvolution counterpart — is reported but not compared. **Tier 3** turns the
panel's myeloid and B-cell blind spot into a coarse comparison rather than a silent omission,
matching the imaging non-T/NK catch-all against the summed deconvolution B-, myeloid- and
dendritic-cell fractions.

**Table 1. Tiered mIF-to-deconvolution mapping used for Fig 4.**

| Tier | mIF (imaging) population | Deconvolution counterpart |
|---|---|---|
| 1 — total immune (primary) | CD45⁺ / all cells | 1 − quanTIseq "other" |
| 2 — mappable lineages | CD8 T; CD4 conventional T; CD4 Treg (CD4⁺FOXP3⁺); NK | matched quanTIseq subsets, summed to gate resolution |
| 3 — non-T/NK catch-all | CD45⁺CD3⁻CD56⁻ "Immune (non-T/NK)" | summed B + myeloid + dendritic-cell fractions |

The exhaustive mapping — every deconvolution subset assigned to each tier — is given in Additional
files.

## Statistics (Fig 4)

The Fig 4 concordance is reported **descriptively only**. We report **no correlation coefficient,
significance test, confidence interval, or p-value for any tier**: with n = 6, such a statistic
would overstate the precision of a six-point comparison. Concordance is shown instead as **all six
raw paired points per tier** (imaging fraction against deconvolution fraction) and described
qualitatively by direction and rank co-tracking — whether the cases that sit high on one axis sit
high on the other — with no fitted trend line or reported statistic. The hotscore is presented as a
**clean separation of the pre-selected high and low groups**: a group contrast on the selection
variable itself, explicitly not an unbiased correlation.

## Analysis reproducibility

All imaging analysis ran inside the containerised MIRAGE pipeline described in Implementation.
Deconvolution and statistics tool versions, container details, licences, and all repository and DOI
identifiers are collected in Availability and requirements and are not duplicated here.

# Availability and requirements

| Field | Value |
|---|---|
| **Project name** | MIRAGE |
| **Project home page** | https://github.com/sceriff0/mirage |
| **Operating system(s)** | Platform independent (Linux / HPC recommended; runs on macOS and Windows through a container runtime). Developed and tested on Linux with SLURM. |
| **Programming language** | Nextflow DSL2 (workflow orchestration); Python (per-module analysis scripts); Groovy (QuPath import helpers). |
| **Other requirements** | Nextflow ≥ 25.04.0; a container runtime — Docker (local/development) or Singularity/Apptainer (HPC). Per-module container images are provided (see *Availability of data and materials*). An NVIDIA GPU is recommended for segmentation and registration (a CPU fallback exists but is substantially slower). The default StarDist segmentation backend uses a built-in model that is downloaded automatically; the custom model used for the case study is external to the repository (see *Availability of data and materials*). |
| **License** | MIT (OSI-approved, permissive; no non-commercial clause). |
| **Any restrictions to use by non-academics** | None (MIT permits commercial use). |

The version recorded in the pipeline manifest is `v0.1.0`; no tagged release exists yet, so the
release tag and archival DOI to cite are given in *Availability of data and materials* below. The
third-party tools MIRAGE builds on (among them Nextflow, VALIS, BaSiC/BaSiCPy, StarDist, InstanSeg,
CellSAM, Bio-Formats, scikit-image, QuPath) are cited in Methods.

# Declarations

## Availability of data and materials

The materials behind MIRAGE fall into three distinct classes, which we state separately because
they carry different reproducibility guarantees.

*Source code.* MIRAGE is open-source under the MIT licence and publicly available at
https://github.com/sceriff0/mirage `[release tag / commit SHA — to be filled on submission]`, with
an archived snapshot at `[Zenodo DOI — to be minted alongside the first tagged release]`. At the
time of writing the repository is public but no tagged release or archival DOI has yet been cut, so
the exact commit SHA is currently the only stable pin for a paper run; cutting the release and
minting the DOI is tracked as a submission-readiness item.

*Container images.* Each module runs in its own image, built from the Dockerfiles vendored in the
repository (`containers/`) and published to Docker Hub under a single repository,
`bolt3x/attend_image_analysis`, with one descriptive tag per module (e.g. `preprocess`,
`segmentation_gpu`, `quantification_gpu`, `merge`, `convert_bioformats_2`). The distributed
registration path uses the project's patched VALIS image
`bolt3x/attend_image_analysis:mirage_valis_1.0.0` (patched with an external-tile hook the upstream
build lacks), while the default single-node registration path uses the upstream author's image
`cdgatenbee/valis-wsi:1.0.0`. Publication to the GitHub Container Registry was abandoned because its
default-private packages returned `403 Forbidden` on the cluster. Because all images therefore live
under one third-party Docker Hub account, moving them to a namespace that does not depend on an
individual account — and cutting the tagged release above — is a submission-readiness item.

*Segmentation model weights.* The default StarDist backend uses the built-in `2D_versatile_fluo`
model, which is downloaded automatically and is the model exercised by the test suite and
continuous integration; this is the openly reproducible, out-of-the-box path. The head-and-neck
case study reported in Fig 4, however, was run with a custom-trained StarDist model
(`stardist_full_e200_lr00001_aug1_seed10_es50p0.001_rlr0.5p50`) that is not shipped in the
repository and is not auto-downloadable. As things stand, the case study is therefore not exactly
reproducible from public artifacts alone. The custom weights are archived at
`[archive / DOI — to be filled on submission]`; archiving them is a submission-readiness item.

Two datasets sit behind the pipeline, and they replicate different things. The bundled synthetic
test data (`tests/testdata/`, also regenerable from a script in the repository) runs the whole
pipeline end-to-end with the built-in StarDist model and requires no restricted assets; it is the
minimal dataset that reproduces the *pipeline mechanics*, and it is exercised in continuous
integration on every push and pull request. It does not reproduce the Fig 4 biological result. The
proof-of-concept data underlying Fig 4 — real head-and-neck patient whole-slide images together
with adjacent-section bulk RNA-seq — are human tissue data and are
`[deposited at <accession> / available from the authors under a controlled-access data-access
agreement, subject to ethics approval — to be filled on submission]`, under the ethics approval and
consent referenced below `[ethics / consent reference — to be supplied]`. Deciding and stating this
deposition plan is a submission-readiness item that the repository cannot answer.

The reproducibility argument for the pipeline itself is made in Implementation; here we record only
the artifacts and the gaps above. Confirming that the continuous-integration suite is green on a
clean checkout at the cited release commit is the final submission-readiness check.

**FlowPath.** The FlowPath QuPath extensions, which consume MIRAGE's GeoJSON output for interactive
gating (Fig 3), are developed and released independently of MIRAGE and are cited separately. They
comprise a suite of public, MIT-licensed repositories targeting QuPath 0.7.0: the installation
catalog (https://github.com/sceriff0/flowpath-catalog) and three extensions — GatingTree
(https://github.com/sceriff0/qupath-extension-flowpath-gatingtree), AnnoMask
(https://github.com/sceriff0/qupath-extension-annomask), and qUMAP
(https://github.com/sceriff0/qupath-extension-flowpath-qumap) — documented at
https://flowpath.readthedocs.io/. MIRAGE interoperates with FlowPath at the level of the GeoJSON
hand-off only; FlowPath's gating algorithm is neither designed nor assessed here.

## Ethics approval and consent to participate

`[Author-supplied: name the approving ethics committee / IRB, the protocol/approval number, and the
consent obtained for the head-and-neck patient tissue and RNA-seq underlying Fig 4. Must match the
ethics/consent reference cited in Availability of data and materials.]`

## Consent for publication

`[Author-supplied: Not applicable, or the consent-for-publication statement as required.]`

## Competing interests

`[Author-supplied: The authors declare that they have no competing interests, or list them.]`

## Funding

`[Author-supplied: funding sources and grant numbers, and the role (if any) of the funders in the
study.]`

## Authors' contributions

`[Author-supplied: per-author contribution statement (e.g. CRediT roles).]`

## Acknowledgements

`[Author-supplied.]`

# References

<!-- Reconciled from prior-work-citation-landscape.md. Numbers are the landscape anchor
     keys used verbatim in the section text above, so every in-text [N] resolves here and
     every entry below is cited. See "Reference-list reconciliation" at the end for the
     one unresolved anchor, the excluded (uncited) landscape entries, and the deferred
     final consecutive renumbering. -->

1. Schapiro D, Sokolov A, Yapp C, et al. MCMICRO: a scalable, modular image-processing pipeline for multiplexed tissue imaging. *Nat Methods* 2022;19(3):311–315. doi:10.1038/s41592-021-01308-y.
2. Muhlich JL, Chen Y-A, Yapp C, Russell D, Santagata S, Sorger PK. Stitching and registering highly multiplexed whole-slide images of tissues and tumors using ASHLAR. *Bioinformatics* 2022;38(19):4613–4621. doi:10.1093/bioinformatics/btac544.
3. Blampey Q, Mulder K, Gardet M, et al. Sopa: a technology-invariant pipeline for analyses of image-based spatial omics. *Nat Commun* 2024;15:4981. doi:10.1038/s41467-024-48981-z.
4. Windhager J, Zanotelli VRT, Schulz D, et al. An end-to-end workflow for multiplexed image processing and analysis. *Nat Protoc* 2023;18(11):3565–3613. doi:10.1038/s41596-023-00881-0.
5. Gatenbee CD, Baker A-M, Prabhakaran S, et al. Virtual alignment of pathology image series for multi-gigapixel whole slide images. *Nat Commun* 2023;14(1):4502. doi:10.1038/s41467-023-40218-9.
6. Borovec J, Kybic J, Arganda-Carreras I, et al. ANHIR: Automatic Non-Rigid Histological Image Registration Challenge. *IEEE Trans Med Imaging* 2020;39(10):3042–3052. doi:10.1109/TMI.2020.2986331.
7. Peng T, Thorn K, Schroeder T, et al. A BaSiC tool for background and shading correction of optical microscopy images. *Nat Commun* 2017;8:14836. doi:10.1038/ncomms14836.
9. Schmidt U, Weigert M, Broaddus C, Myers G. Cell detection with star-convex polygons. *MICCAI 2018*, LNCS 11071:265–273. doi:10.1007/978-3-030-00934-2_30.
11. Di Tommaso P, Chatzou M, Floden EW, et al. Nextflow enables reproducible computational workflows. *Nat Biotechnol* 2017;35(4):316–319. doi:10.1038/nbt.3820.
12. Bankhead P, Loughrey MB, Fernández JA, et al. QuPath: open source software for digital pathology image analysis. *Sci Rep* 2017;7:16878. doi:10.1038/s41598-017-17204-5.
13. Linkert M, Rueden CT, Allan C, et al. Metadata matters: access to image data in the real world. *J Cell Biol* 2010;189(5):777–782. doi:10.1083/jcb.201004104.
14. Goldsborough T, Philps B, O'Callaghan A, et al. InstanSeg: an embedding-based instance segmentation algorithm. arXiv:2408.15954, 2024. doi:10.48550/arXiv.2408.15954.
15. Israel U, Marks M, Dilip R, et al. CellSAM: a foundation model for cell segmentation. *Nat Methods* 2025. doi:10.1038/s41592-025-02879-w.
16. Finotello F, Mayer C, Plattner C, et al. Molecular and pharmacological modulators of the tumor immune contexture revealed by deconvolution of RNA-seq data. *Genome Med* 2019;11:34. doi:10.1186/s13073-019-0638-6.
23. Foy JP, Karabajakian A, Ortiz-Cuaran S, et al. Immunologically active phenotype by gene expression profiling is associated with clinical benefit from PD-1/PD-L1 inhibitors in real-world head and neck and lung cancer patients. *Eur J Cancer* 2022;174:287–298. doi:10.1016/j.ejca.2022.06.034.
24. Hickey JW, Neumann EK, Radtke AJ, et al. Spatial mapping of protein composition and tissue organization: a primer for multiplexed antibody-based imaging. *Nat Methods* 2022;19(3):284–295. doi:10.1038/s41592-021-01316-y.
25. Elhanani O, Ben-Uri R, Keren L. Spatial profiling technologies illuminate the tumor microenvironment. *Cancer Cell* 2023;41(3):404–420. doi:10.1016/j.ccell.2023.01.010.

# Figure legends

**Figure 1. Iterative single-section mIF acquisition protocol and its stripping
controls.** Because every panel images the **same physical cells**, per-cell
measurements from different panels join into one multi-marker profile — a
biologically valid join, not a geometric coincidence.
**(a)** Schematic of the stain–image–strip protocol: the same tissue section
undergoes ten sequential rounds (round ≡ panel), each staining ~2 antibody
markers plus DAPI, imaged at whole-slide resolution, then stripped — antibody
removal, not photobleaching — before the next round, yielding ~20 markers across
ten separately-acquired panels. DAPI is re-acquired every round.
**(b)** Representative mIF data: DAPI and a subset of immune markers (e.g. CD45,
CD3, CD8) pseudo-coloured, with single-marker snapshots of individual rounds.
**(c)** Secondary-only stripping-completeness control: after stripping, the
section is incubated with secondary antibody alone; absent fluorescence confirms
**removal** of the previous primary antibody, not **survival** of its target
epitope. **(d)** Post-strip carryover control: imaged after stripping but before
re-staining, the section shows negligible signal, confirming no carryover into
the next panel. Between rounds the slide is unmounted and rescanned, introducing
whole-slide drift that MIRAGE later corrects using DAPI as the common anchor
(Fig 2). Repeated stripping is monitored retrospectively (per-round DAPI
retention and a CD8⁺ ⊂ CD3⁺ consistency check; full plot in Additional files) and
disclosed as a limitation, not controlled experimentally. mIF, multiplexed
immunofluorescence; DAPI, 4′,6-diamidino-2-phenylindole. Scale bars,
[FIGURE-PRODUCTION: NN µm] in (b)–(d).

**Figure 2. The MIRAGE pipeline and the accuracy of its cross-panel DAPI-anchored registration.**

**(a)** *The MIRAGE pipeline.* Five conceptual stages, implemented as three sequential Nextflow DSL2
subworkflows (preprocessing, registration, postprocessing): illumination and background
correction, cross-panel registration (the headline capability), segmentation (StarDist), single-cell
quantification, and GeoJSON export. The nuclear counterstain DAPI, present in every
separately-acquired multiplexed immunofluorescence (mIF) panel, is the common anchor threaded through
registration (highlighted); (c–d) evaluate that step.

**(b)** *Illumination correction suppresses tile-boundary seams.* A tile-boundary crop before and
after BaSiC flatfield/darkfield correction across a whole-slide mosaicing seam: evening the
tile-boundary intensity is a side-effect of illumination correction, not a standalone stitching or
seam-correction module. Scale bar, [author to supply] µm.

**(c)** *Registration aligns separately-acquired panels on the DAPI anchor.* Two-colour DAPI overlay
of two panels of the same physical section, separately acquired, before (cross-panel drift visible)
and after registration (nuclei superimposed) — the qualitative counterpart to (d). Scale bar,
[author to supply] µm.

**(d)** *Registration accuracy across configurations.* DAPI-channel Dice overlap coefficient after
alignment, plotted for every arm, with VALIS-internal target registration error (TRE) as the
arm-ranking metric — across two VALIS accuracy presets (differing in feature-matching resolution, not
in the feature matcher) × micro-registration on or off, plus rigid-only and no-registration
baselines. Registration substantially improves DAPI overlap over both baselines [author to supply:
best-performing arm and per-arm Dice/TRE values].

The two metrics do two jobs. TRE ranks the arms and selects the configuration, but as a self-reported
residual on VALIS's own matched keypoints it excludes the hard regions where matching fails and
cannot stand alone as an absolute-accuracy claim; Dice, computed independently of VALIS's feature
matcher on every arm (binarisation rule in Methods), corroborates that ranking and anchors the
absolute-accuracy claim. The benchmark is an internal cross-arm comparison only, with no head-to-head
against other pipelines; MIRAGE's contribution is a capability — automated cross-panel,
cross-acquisition registration (argued in full in Background). VALIS [5] was chosen as a fully
automated, open-source whole-slide method reported to reach state-of-the-art accuracy on the ANHIR
benchmark [6]. Runtime and throughput are reported in Additional files.

**Figure 3. MIRAGE's standard output is directly consumable by an independent downstream gating
tool.** The GeoJSON MIRAGE exports from each multiplexed immunofluorescence (mIF) dataset — an open,
tool-agnostic format bundling cell geometries with their per-cell features — is read by FlowPath, an
interactive gating tool, with no conversion, and is QuPath-native. This interoperability is a
supporting feature; MIRAGE's headline capability, automated cross-panel registration, is Fig 2.

**(a)** *Interop hand-off.* The MIRAGE GeoJSON passed into FlowPath — the hand-off only, not the
pipeline stage graph (Fig 2(a)).

**(b)** *Interactive gating on MIRAGE-derived features.* One representative gate drawn by a user on
a threshold plot of MIRAGE per-cell features — showing that gating is interactive and user-driven,
not how FlowPath chooses thresholds.

**(c)** *Sequential hierarchical phenotype tree.* The strict sequential (not Boolean) gating scheme:
a CD45-only pan-immune gate, then a T/NK/lymphoid axis (CD3, CD8, CD4, FOXP3, CD56, GZMB), resolving
13 terminal populations. The CD45⁺CD3⁻CD56⁻ "Immune (non-T/NK)" catch-all node is shaded (see
coverage caveat below). These are the populations compared in Fig 4.

**(d)** *Spatial phenotype map.* One exemplar head-and-neck whole-slide image (WSI), cells coloured
by FlowPath phenotype call (a legible subset of the tree), with a zoomed inset resolving individual
cells; scale bars on whole slide and inset. This renders the calls only — no counts, no hot/cold, no
concordance.

Fig 3 depicts the interoperability surface only — GeoJSON in, interactive gating, populations out —
and does not describe or assess FlowPath's gating algorithm. The called populations feed the
proof-of-concept of Fig 4: Fig 3 shows the mechanism and the single-cell spatial calls, Fig 4 the
aggregate concordance among immune-fraction proxies. Because the panel carries no myeloid
(CD68/CD163/CD14) or B-cell (CD19/CD20) markers, those compartments collapse into the CD45⁺CD3⁻CD56⁻
catch-all node flagged in (c); its handling is described in Fig 4 (Tier 3) and Methods.

**Figure 4. Proof-of-concept concordance of MIRAGE + FlowPath imaging immune quantification with two orthogonal transcriptomic proxies in six head-and-neck cancer cases.**
**(a)** *Imaging recovers the transcriptomic hot/cold contrast.* Boxplots of the multiplexed immunofluorescence (mIF) CD45⁺ (pan-immune) / all-cells fraction for three hot versus three cold cases (n = 3 per group), all six values overlaid; the imaging immune fraction is consistently higher in the hot group. Hotscore (the immunologically active gene-expression signature of Foy et al. [23]; high = hot, low = cold, no threshold) is the **selection axis, not a finding**; no test is applied, and the raw points are the evidence.
**(b)** *Imaging quantification co-varies with bulk RNA-seq deconvolution.* Per-case scatter of the quanTIseq deconvolution immune fraction (= 1 − "other" [16]) against the imaging CD45⁺/all-cells fraction, same six cases (Tier 1, total immune). Cases immune-high by imaging are immune-high by deconvolution: the two estimates show **concordant ranking across cases** — the load-bearing evidence that the imaging quantification is trustworthy. They use **different denominators** (imaging counts cells; deconvolution estimates a bulk-mixture fraction), so points need not lie on the identity line: only ranking, not absolute-fraction agreement, is claimed.

Fig 4 shows **concordance among proxies, not validation**: neither hotscore nor quanTIseq is ground truth — both are orthogonal molecular proxies for immune content, from the **same adjacent-section bulk RNA-seq**. The six cases were chosen for hotscore contrast (three high, three low from a larger cohort), a **contrast-enriched** design that inflates both panel effects; findings are descriptive and hypothesis-generating, and **no statistical test or correlation coefficient (ρ, r, p, or CI) is reported**.

# Additional files

The following supplementary files are referenced in the text and consolidated here.

- **Additional file 1 — Full pipeline configuration.** Complete per-module parameter table
  (BaSiC settings; background-correction parameters; StarDist/InstanSeg/CellSAM options; the
  VALIS arm parameters; the quantification flags; `seg_expand_distance`; the complete
  compartment-to-marker map), together with a default-versus-recommended configuration
  comparison. *(Cited in Implementation and Methods.)*
- **Additional file 2 — Full registration-accuracy table.** Per-arm VALIS-internal target
  registration error (TRE) and DAPI-channel Dice overlap for every benchmarked arm, including
  the rigid-only and no-registration baselines. *(Cited in Results, Methods, Fig 2.)*
- **Additional file 3 — Tier 2 and Tier 3 mIF–deconvolution scatters.** The mappable-lineage
  (Tier 2) and non-T/NK catch-all (Tier 3) concordance scatter plots, as raw paired points.
  *(Cited in Results.)*
- **Additional file 4 — Exhaustive tier→cell-type mapping.** Every deconvolution subset
  assigned to each of the three comparison tiers. *(Cited in Methods and Results.)*
- **Additional file 5 — Epitope/tissue-integrity monitoring.** Per-round DAPI-signal-retention
  plot across the ten rounds and the CD8⁺ ⊂ CD3⁺ biological-consistency check.
  *(Cited in Experimental protocol and Fig 1.)*
- **Additional file 6 — Computational performance.** Pipeline runtime and throughput.
  *(Cited in Fig 2.)*

<!-- =====================================================================
REFERENCE-LIST RECONCILIATION (assembly note; not manuscript prose)

* Numbering: in-text anchors keep their prior-work-citation-landscape.md keys, and the
  reference list contains exactly the cited keys (1,2,3,4,5,6,7,9,11,12,13,14,15,16,23,24,25).
  Every in-text [N] resolves; every listed entry is cited. The list is therefore NOT gap-free
  (8,10,17-22 are excluded — see below). Final consecutive renumbering in order of first
  appearance is deferred to submission formatting, per the map's out-of-scope note on
  bibliography styling. At renumbering, [24][25] (Background P1, the first in-text citations)
  become [1][2] and every subsequent number shifts. Renumbering now would be churn.

* RESOLVED ANCHOR: Background P1's former `[citation needed: mIF / tumour-immune-microenvironment
  review]` is now [24][25] (ticket 31 CLOSED 2026-07-17) — [24] Hickey et al. 2022 (Nat Methods,
  multiplexed-antibody-imaging primer) + [25] Elhanani, Ben-Uri & Keren 2023 (Cancer Cell,
  spatial profiling of the tumour microenvironment). Both verified review-level and in-direction;
  not invented. These are the first-appearing in-text citations (see renumbering note above).

* Excluded (uncited) landscape entries: 8 BaSiCPy, 10 Weigert(StarDist-3D), 17 CIBERSORTx,
  18 CIBERSORT, 19 MCP-counter (dropped from the paper entirely), 20 Ayers, 21 Charoentong,
  22 Yoshihara. Refs 20-22 are the superseded hotscore candidates; MCP-counter's absence is
  deliberate (map framing rule).

* FlowPath software citation: Availability cites the four FlowPath repos as inline URLs (no
  numbered anchor). If the venue wants numbered software citations, add FlowPath repo/Zenodo
  entries to the reference list (Availability draft-note follow-on).

* Unverified bibliographic fields (pre-submission-checklist items, carried from the landscape):
  VALIS ANHIR wording (resolved to "state-of-the-art"), BaSiCPy preprint DOI, InstanSeg mIF
  companion + CellSAM Nat Methods vol/article, CIBERSORT PMID, Steinbock ROI-vs-WSI scope.
===================================================================== -->
