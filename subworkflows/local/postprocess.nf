
/*
========================================================================================
    IMPORT MODULES
========================================================================================
*/

include { SPLIT_CHANNELS           } from '../../modules/local/split_channels'
include { MERGE_QUANT_CSVS         } from '../../modules/local/merge_quant_csvs'
include { COMPILE_PANEL            } from '../../modules/local/compile_panel'
include { PHENOTYPE                } from '../../modules/local/phenotype'
include { GENERATE_POSTPROCESSING_QC    } from '../../modules/local/generate_postprocessing_qc'
include { EXPORT_SPATIALDATA } from '../../modules/local/export_spatialdata'
// Shared with subworkflows/local/add_cycle.nf — see those files for why the shaping
// lives there rather than being copied into each caller. groupTiffsByPatient is a
// plain function, not a process/workflow, but Nextflow's `include` pulls in either.
include { QUANTIFY_MARKERS; groupTiffsByPatient } from './quantify_markers'
include { ASSEMBLE_EXPORT          } from './assemble_export'
include { CHECKPOINT_WRITER        } from './checkpoint_writer'

/*
========================================================================================
    SUBWORKFLOW:POSTPROCESSING
========================================================================================
    Description:
        Splits multichannel images to single channels, quantifies marker intensities
        per cell against masks produced upstream by subworkflows/local/segmentation.nf,
        merges results, and exports QuPath-compatible GeoJSON with raw measurements
        for FlowPath gating.

        SEGMENT / EXTRACT_CELL_PROPERTIES / EXTRACT_NUCLEI_PROPERTIES no longer run
        here — they moved to segmentation.nf, their own resumable step
        (Layout.SEGMENTED / Checkpoint.columns('segmented')). This file takes their
        outputs as plain [meta/patient_id, file] inputs instead.

    Input:
        ch_seg: PatientArtifacts.channels(..., PatientArtifacts.SEGMENTATION_FIELDS, ...)
                — the named set of segmentation artifacts, produced either by
                subworkflows/local/segmentation.nf's SEGMENTATION on the linear path or
                by its READ_SEGMENTED_CHECKPOINT at `--start postprocessing`. Fields:
                  samples          [meta, file] — all registered slides (reference + moving)
                  cell_mask        [meta, file]
                  nuclei_mask      [meta, file]
                  contours         [patient_id, file]
                  nucleus_contours [patient_id, file] — Channel.empty() when
                                                        --quantify_compartments is false
                  morphology       [meta, file]
                Both keying conventions are present on purpose: lib/PatientArtifacts.groovy
                absorbs the difference, so nothing in here re-keys by hand.

    Output:
        checkpoint_csv: file — the collected 'postprocessed' checkpoint (see
                        Layout.POSTPROCESSED / Checkpoint.columns), one row per patient
        postprocess_qc: GENERATE_POSTPROCESSING_QC's per-patient QC artifacts
        size_logs:      input size-log CSVs from this step's processes
        versions:       versions.yml from this step's processes
========================================================================================
*/

workflow POSTPROCESSING {
    take:
    ch_seg              // PatientArtifacts.channels(..., SEGMENTATION_FIELDS, ...) — see
                        // the Input: block above. One named record replaces the six
                        // parallel channels this workflow used to take positionally.
    ch_reg_qc           // Registration QC JSONs (may be empty)
    ch_reg_residuals    // Per-cell registration residual CSVs (may be empty)
    compartment_mode    // ParamUtils.compartmentMode(params) — resolved once by
                        // workflows/mirage.nf and threaded down, the same seam
                        // --registration_method has. Passed straight through to
                        // ASSEMBLE_EXPORT below, which owns the nucleus-contour gate
                        // this file used to spell out as a ternary.

    main:

    // ========================================================================
    // CHANNEL SPLITTING - Split all multichannel images (runs in PARALLEL with EXTRACT_CELL_PROPERTIES)
    // ========================================================================
    SPLIT_CHANNELS(
        ch_seg.samples.map { meta, file -> [meta, file, meta.is_reference] }
    )

    // ========================================================================
    // QUANTIFICATION - Join channels with their patient's mask
    // ========================================================================
    // Carry BOTH masks (cell + nuclei). The nuclear mask is always available from
    // SEGMENT; QUANTIFY only uses it when params.quantify_compartments is set
    // (per-compartment signal). The same pair feeds ASSEMBLE_EXPORT's embed_masks gate
    // further down.
    ch_masks = PatientArtifacts.bundle(
        name    : 'POSTPROCESSING: the segmentation masks feeding QUANTIFY',
        metaFrom: 'cell_mask',
        fields  : [
            cell_mask  : ch_seg.cell_mask,
            nuclei_mask: ch_seg.nuclei_mask,
        ],
    )

    // Per-marker fan-out + QUANTIFY + per-patient grouping (with the groupKey
    // streaming hint) all live in QUANTIFY_MARKERS, shared with add_cycle.nf.
    // The --debug_channels views for this whole chain moved there with it, so this
    // file no longer carries a debug-view helper of its own.
    QUANTIFY_MARKERS(
        SPLIT_CHANNELS.out.channels,
        ch_masks.map { b -> [b.patient_id, b.cell_mask, b.nuclei_mask] },
        compartment_mode,
    )
    ch_grouped_csvs = QUANTIFY_MARKERS.out.grouped_csv

    // Join grouped intensity CSVs with morphology.csv (segmentation.nf's
    // EXTRACT_CELL_PROPERTIES.out.morphology, carried on ch_seg.morphology).
    ch_for_quant_merge = PatientArtifacts.bundle(
        name    : 'POSTPROCESSING: the per-patient MERGE_QUANT_CSVS tuple',
        metaFrom: 'grouped_csvs',
        fields  : [
            grouped_csvs: ch_grouped_csvs,
            morphology  : ch_seg.morphology,
        ],
    )

    MERGE_QUANT_CSVS(ch_for_quant_merge.map { b -> [b.meta, b.grouped_csvs, b.morphology] })

    // ========================================================================
    // PHENOTYPING (optional) - compile panel + classify cells per patient
    // ========================================================================
    // The `--quantify_compartments` ternary that used to stand here
    // (`compartment_mode.compartments ? ch_nucleus_contours : ch_contours`) moved into
    // ASSEMBLE_EXPORT as a `when:` declaration — add_cycle.nf carried a byte-identical
    // copy of it, and a run-level gate stated twice is a gate that can disagree with
    // itself. The raw (possibly empty) channel is passed through untouched.
    def do_pheno = (params.panel_spec != null) || (params.panel_model != null)
    def ch_model_config = Channel.empty()
    def ch_phenotypes = Channel.empty()
    def ch_phenotype_qc = Channel.empty()
    // Per-patient view of the RUN-LEVEL compiled model config. The config is one file
    // for the whole run, so it cannot be a bundle field as it stands; pairing it with
    // the patients that have phenotypes is the one re-key left in this file, and it is
    // a fan-out of a value channel rather than a join two producers could disagree on.
    def ch_model_config_by_patient = Channel.empty()
    if (do_pheno) {
        if (params.panel_spec) {
            COMPILE_PANEL(Channel.value(file(params.panel_spec)))
            ch_model_config = COMPILE_PANEL.out.model_config.first()
        } else {
            ch_model_config = Channel.value(file(params.panel_model))
        }

        ch_pheno_in = PatientArtifacts.bundle(
            name    : 'POSTPROCESSING: the per-patient PHENOTYPE tuple',
            metaFrom: 'merged_csv',
            fields  : [
                merged_csv: MERGE_QUANT_CSVS.out.merged_csv,
                morphology: ch_seg.morphology,
            ],
        )
        PHENOTYPE(ch_pheno_in.map { b -> [b.meta, b.merged_csv, b.morphology] }, ch_model_config)
        ch_phenotypes   = PHENOTYPE.out.phenotypes
        ch_phenotype_qc = PHENOTYPE.out.qc
        ch_model_config_by_patient = ch_phenotypes
            .map { meta, _ph -> meta.patient_id }
            .combine(ch_model_config)
    }

    // ========================================================================
    // MERGE - Combine split channel TIFFs with segmentation mask (per patient)
    // ========================================================================
    // Group split channel TIFFs by patient for merging
    // SPLIT_CHANNELS already handles DAPI filtering correctly
    // Deduplicate by patient_id + marker to avoid duplicate channel names
    // Use groupKey for streaming - emits as soon as channels_count items collected
    //
    // `remainder: true` for the same reason as QUANTIFY_MARKERS' grouping (same
    // channels_count): an under-count must not be allowed to silently drop the patient
    // from the pyramid outright. But — see the fuller account in QUANTIFY_MARKERS'
    // GROUP comment, which this grouping mirrors — an under-count here does NOT degrade
    // to "late but complete" the way it can for the CSV-merge paths. This grouping feeds
    // MERGE_AND_PYRAMID with a one-file surplus group, which trips that process's memory
    // closure (conf/modules.config:330-337 — pre-existing, NOT fixed here) and the run
    // ABORTS with "No such file or directory: channels". `remainder: true` is kept anyway
    // because keeping only one of the two channels_count-sized groupings (this one, or
    // QUANTIFY_MARKERS') would be worse than keeping neither — the patient would reach
    // geojson/ and quantification/ but not the pyramid, and so be missing from
    // csv/postprocessed.csv, half-published and invisible to any later --start
    // postprocessing or add_cycle run. These two groupings must keep identical
    // channels_count semantics even though their downstream failure modes differ --
    // WITHIN THIS FILE: both read the same meta.channels_count for the same
    // patient. That equality is NOT a cross-file invariant: add_cycle.nf's own
    // pyramid grouping deliberately uses a DIFFERENT total (new_count + prior_count,
    // since it merges two pyramids' worth of channels) than its own QUANTIFY_MARKERS
    // call (new-cycle channels only, because the prior quantification columns are
    // merged onto the CSV separately, not re-quantified) -- the two counts SHOULD
    // differ there. Do not "reconcile" them to match this file's equality.
    // The channels_count-sized groupKey + remainder:true grouping itself is
    // groupTiffsByPatient (subworkflows/local/quantify_markers.nf), shared with
    // add_cycle.nf's own version of this same grouping — see that function's doc
    // comment for why an under-count here is worse than for the CSV-merge paths.
    ch_split_tagged = SPLIT_CHANNELS.out.channels
        .flatMap { meta, tiffs ->
            // Normalize to List and create entries keyed by [patient_id, marker]
            // Carry channels_count for groupKey
            def tiff_list = tiffs instanceof List ? tiffs : [tiffs]
            tiff_list.collect { tiff ->
                [meta.patient_id, meta.channels_count, tiff.baseName, tiff]
            }
        }
        .unique { patient_id, _channels_count, marker, _tiff -> [patient_id, marker] }  // Keep first occurrence of each patient+marker
        .map { patient_id, channels_count, _marker, tiff -> [patient_id, channels_count, tiff] }
    ch_split_grouped = groupTiffsByPatient(ch_split_tagged)

    // EXPORT_GEOJSON tuple assembly + the embed_masks pyramid gate live in
    // ASSEMBLE_EXPORT, shared with add_cycle.nf. Both the nucleus-contour placeholder
    // and the three phenotype placeholders live there too now, as declarations — this
    // file hands over the raw channels and says whether a panel was configured.
    ASSEMBLE_EXPORT(
        PatientArtifacts.channels('POSTPROCESSING -> ASSEMBLE_EXPORT', PatientArtifacts.EXPORT_FIELDS, [
            merged_csv      : MERGE_QUANT_CSVS.out.merged_csv,
            contours        : ch_seg.contours,
            nucleus_contours: ch_seg.nucleus_contours,
            phenotypes      : ch_phenotypes,
            phenotype_qc    : ch_phenotype_qc,
            model_config    : ch_model_config_by_patient,
            pyramid_channels: ch_split_grouped,
            cell_mask       : ch_masks.map { b -> [b.patient_id, b.cell_mask] },
            nuclei_mask     : ch_masks.map { b -> [b.patient_id, b.nuclei_mask] },
        ]),
        compartment_mode,
        do_pheno,
    )

    // ========================================================================
    // POSTPROCESSING QC (optional, runs in PARALLEL with MERGE_AND_PYRAMID)
    // ========================================================================
    ch_postprocess_qc = Channel.empty()
    if (!params.skip_postprocessing_qc) {
        // Join cell mask with merged CSV for QC visualization
        ch_for_postprocess_qc = PatientArtifacts.bundle(
            name    : 'POSTPROCESSING: the per-patient GENERATE_POSTPROCESSING_QC tuple',
            metaFrom: 'cell_mask',
            fields  : [
                cell_mask : ch_seg.cell_mask,
                merged_csv: MERGE_QUANT_CSVS.out.merged_csv,
            ],
        )

        GENERATE_POSTPROCESSING_QC(ch_for_postprocess_qc.map { b -> [b.meta, b.cell_mask, b.merged_csv] })
        ch_postprocess_qc = GENERATE_POSTPROCESSING_QC.out.qc.map { meta, pngs -> pngs }
    }

    // ========================================================================
    // CHECKPOINT - Collect all outputs by patient
    // ========================================================================
    // Use collectFile() for non-blocking aggregation (enables patient-level parallelism)
    // The join is per-patient and does not block other patients.
    //
    // THIS IS THE CHAIN THE PATIENT-ARTIFACTS MODULE WAS BUILT FOR. It used to be five
    // chained `.join()`s producing a 6-tuple that was destructured about forty lines
    // further down, and both of the failures that shape invites were live:
    //
    //   * `cell_csv` and `cell_geojson` are BOTH
    //     `Layout.publishedPath(..., 'geojson', ...)` of the same patient, so swapping
    //     two adjacent `.join()` clauses swapped the two checkpoint columns.
    //     `Checkpoint.row` validates key PRESENCE, not which file landed under which
    //     key, so the checkpoint recorded the wrong file under the wrong column and the
    //     run stayed green. Producer and field are bound BY NAME below, and read back by
    //     name at the row builder — there is no position left between them, so a
    //     POSITIONAL swap is unrepresentable. A MIS-DECLARATION (binding `cell_csv:`
    //     to `ASSEMBLE_EXPORT.out.geojson`) still is not, and nothing structural can
    //     catch it: tests/subworkflows/local/postprocessing.nf.test's "Should create
    //     checkpoint CSV" case asserts every column of csv/postprocessed.csv names
    //     the artifact its column is named for, and was watched failing by swapping
    //     exactly these two bindings.
    //   * every one of those joins was a plain inner join, so an `errorStrategy
    //     'ignore'` drop anywhere in this step removed the patient from the chain
    //     without a word and published a checkpoint CSV one row short.
    //     lib/PatientArtifacts.groovy aborts instead, naming the patient.
    //
    // `cell_mask` is the roster: it is the one field that exists before this step runs
    // (SEGMENT produced it, or READ_SEGMENTED_CHECKPOINT read it back), so a patient
    // missing from any of the other four is a task this step lost.
    ch_base_checkpoint = PatientArtifacts.bundle(
        name    : 'POSTPROCESSING: the postprocessed checkpoint row',
        metaFrom: 'cell_mask',
        fields  : [
            cell_mask   : ch_seg.cell_mask.map { meta, mask ->
                // publishedOrAsIs, not publishedPath: with --start postprocessing,
                // ch_seg.cell_mask is READ_SEGMENTED_CHECKPOINT's file(row.cell_mask) --
                // ALREADY an absolute published path from segmentation.nf's run, not a
                // task-dir output of this run. Calling publishedPath unconditionally
                // would double-nest it (producerSubdir sees a parent literally named
                // 'segmentation' and treats it as a producer subdirectory to preserve),
                // e.g. <outdir>/<pid>/segmentation/segmentation/<name>. See
                // Layout.publishedOrAsIs for the full explanation.
                //
                // NEW CROSS-OUTDIR DEPENDENCY: at --start postprocessing this correctly
                // records the PRIOR run's path (wherever segmentation.nf actually
                // published it), not this run's --outdir -- unlike every other column in
                // this checkpoint, which all name files this run itself just wrote. That
                // is correct (the mask genuinely was not recomputed), but it means
                // csv/postprocessed.csv can point outside its own --outdir when written
                // this way, which no prior checkpoint in this pipeline ever did.
                [meta, Layout.publishedOrAsIs(params.outdir, meta.patient_id, 'segmentation', mask)]
            },
            cell_csv    : ASSEMBLE_EXPORT.out.csv.map { meta, csv ->
                [meta, Layout.publishedPath(params.outdir, meta.patient_id, 'geojson', csv)]
            },
            cell_geojson: ASSEMBLE_EXPORT.out.geojson.map { meta, geojson ->
                [meta, Layout.publishedPath(params.outdir, meta.patient_id, 'geojson', geojson)]
            },
            merged_csv  : MERGE_QUANT_CSVS.out.merged_csv.map { meta, csv ->
                [meta, Layout.publishedPath(params.outdir, meta.patient_id, 'quantification', csv)]
            },
            pyramid     : ASSEMBLE_EXPORT.out.pyramid.map { meta, pyramid ->
                [meta, Layout.publishedPath(params.outdir, meta.patient_id, 'pyramid', pyramid)]
            },
        ],
    )

    // ========================================================================
    // SPATIALDATA EXPORT - scverse-native .zarr (additive; OME-TIFF + GeoJSON stay primary)
    // ========================================================================
    if (!params.skip_spatialdata_export) {
        // THE reg_qc=2 ARTIFACTS ARE JOINED PER PATIENT, NOT COLLECTED RUN-WIDE.
        //
        // They used to reach EXPORT_SPATIALDATA on two separate value channels built with
        // `.collect(sort: true)` over the whole run, and a value channel is broadcast to
        // every task — so each patient's .zarr received EVERY patient's *_seg_qc.json and
        // *_reg_residuals.csv. That is silent cross-patient contamination, not a tidiness
        // problem: bin/export_spatialdata.py joins residual rows onto this patient's cell
        // centroids by RAW reference-frame pixel coordinate, and every patient's reference
        // frame is its own pixel grid rooted at (0,0), so a foreign patient's rows land on
        // whatever cell sits within params.spatialdata_residual_join_max_px and become an
        // obsm column — a column whose entire purpose is to EXCLUDE cells — with a
        // plausible join_fraction. Reproduced on a two-patient stub run: both export tasks
        // staged all four QC files. It was invisible to CI because tests/testdata/
        // test_input.csv is one patient, and with one patient "the run's QC" and "this
        // patient's QC" are the same set. tests/testdata/test_input_two_patients.csv now
        // exists so a two-patient run is reachable.
        //
        // BOTH per-patient QC gathers, once. The sized groupKey, remainder:true, the
        // GroupKey unwrap and the canonical ordering are lib/PatientGroup.groovy's;
        // read its header for what each one is load-bearing for. Writing the gather
        // twice by hand is what this helper removes — the two streams differ only in
        // which channel they read, and the copy that drifts is the one that silently
        // loses a property.
        //
        // WHAT IS LOCAL HERE IS THE SIZE, and it is why `sizeOf:` exists at all: the
        // group holds one QC artifact per MOVING slide, and meta.images_count counts
        // the REFERENCE too, so the size is `images_count - 1` — a number no meta
        // holds and `size:` (a meta KEY, read as-is) cannot name. Both sites used to
        // hand-write the ternary instead, whose else-branch was an unsized full-run
        // barrier taken silently on a run that still exits 0. The closure returns
        // null when there is no count to derive from, and PatientGroup ABORTS on
        // null rather than falling back to that barrier.
        //
        // The size only makes a group close EARLY — `remainder: true` (PatientGroup's,
        // applied unconditionally) is what makes the group close at ALL when the count
        // is short, and WARP_SEG_QC's per_cell output is `optional: true`.
        //
        // sortBy because these lists become EXPORT_SPATIALDATA `path` inputs, which
        // Nextflow hashes POSITIONALLY, while groupTuple emits in ARRIVAL order — the
        // same reason the run-wide code this replaced used collect(sort: true).
        //
        // Patients with no QC at all (reg_qc < 2, a single-slide patient, or entry at
        // postprocessing) simply have no key here; the `remainder: true` on the joins
        // below keeps them in the export with an empty list.
        def qcByPatient = { String label, ch_artifacts ->
            PatientGroup.byPatient(
                    ch_artifacts.flatMap { meta, artifacts ->
                        (artifacts instanceof List ? artifacts : [artifacts]).collect { f -> [meta, f] }
                    },
                    name  : label,
                    sizeOf: { meta, _artifact ->
                        meta.images_count == null ? null : meta.images_count - 1
                    },
                    sortBy: { _meta, artifact -> artifact.name },
                )
                .map { patient_id, pairs -> [patient_id, pairs.collect { pair -> pair[1] }] }
        }

        def ch_reg_qc_by_patient = qcByPatient(
            'POSTPROCESSING: the per-patient registration QC feeding EXPORT_SPATIALDATA',
            ch_reg_qc)

        def ch_reg_residuals_by_patient = qcByPatient(
            'POSTPROCESSING: the per-patient registration residuals feeding EXPORT_SPATIALDATA',
            ch_reg_residuals)

        def ch_sd_in = PatientArtifacts.bundle(
            name    : 'POSTPROCESSING: the per-patient EXPORT_SPATIALDATA tuple',
            metaFrom: 'merged_csv',
            fields  : [
                merged_csv      : MERGE_QUANT_CSVS.out.merged_csv,
                contours        : ch_seg.contours,
                // The same run-level gate ASSEMBLE_EXPORT declares, for the same reason:
                // without --quantify_compartments EXTRACT_NUCLEI_PROPERTIES never ran,
                // so the channel is empty for everybody and the cell contours stand in.
                nucleus_contours: [channel: ch_seg.nucleus_contours,
                                   when: compartment_mode.compartments, orElseField: 'contours'],
                cell_mask       : ch_seg.cell_mask,
                nuclei_mask     : ch_seg.nuclei_mask,
                pyramid         : ASSEMBLE_EXPORT.out.pyramid,
                // Genuinely PER-PATIENT optional (reg_qc < 2, a single-slide patient,
                // or entry at postprocessing), which is a different thing from the
                // run-level gate above and is declared differently. Requiring it would
                // drop those patients' exports entirely — that is how an optional input
                // silently becomes a required one. The old chain expressed this as
                // `join(..., remainder: true)` plus a `.filter { it[1] != null }` that
                // ALSO swallowed keys present only on the QC side; those now abort,
                // since a patient with registration QC but no quantification means one
                // of the two producers lost a patient.
                reg_qc          : [channel: ch_reg_qc_by_patient, optional: true, orElse: []],
                reg_residuals   : [channel: ch_reg_residuals_by_patient, optional: true, orElse: []],
            ],
        )

        EXPORT_SPATIALDATA(
            ch_sd_in.map { b ->
                [b.meta, b.merged_csv, b.contours, b.nucleus_contours, b.cell_mask,
                 b.nuclei_mask, b.pyramid, b.reg_qc, b.reg_residuals]
            }
        )
    }

    // The size hint is the literal 1, not a count read off a meta: ch_base_checkpoint is
    // a join chain keyed BY patient, so this checkpoint has exactly one row per patient
    // by construction. Nothing to count, and nothing that could disagree.
    ch_checkpoint_rows = ch_base_checkpoint
        .map { b ->
            [b.patient_id, 1, Checkpoint.row(Layout.POSTPROCESSED, [
                patient_id  : b.patient_id,
                cell_csv    : b.cell_csv,
                cell_geojson: b.cell_geojson,
                merged_csv  : b.merged_csv,
                cell_mask   : b.cell_mask,
                pyramid     : b.pyramid,
            ])]
        }

    CHECKPOINT_WRITER(Layout.POSTPROCESSED, ch_checkpoint_rows)
    ch_checkpoint_csv = CHECKPOINT_WRITER.out.csv

    // Collect size logs from all postprocessing processes. SEGMENT /
    // EXTRACT_CELL_PROPERTIES / EXTRACT_NUCLEI_PROPERTIES moved to
    // subworkflows/local/segmentation.nf and report their own size_logs/versions
    // there now -- workflows/mirage.nf mixes SEGMENTATION.out.{size_logs,versions}
    // into the run-wide QC stream directly, the same way it already does for every
    // other step's aggregate output (this file's checkpoint_csv/postprocess_qc
    // pattern), so nothing from that step is double-counted or dropped here.
    ch_size_logs = Channel.empty()
        .mix(SPLIT_CHANNELS.out.size_log)
        .mix(QUANTIFY_MARKERS.out.size_logs)
        .mix(MERGE_QUANT_CSVS.out.size_log)
        .mix(ASSEMBLE_EXPORT.out.size_logs)

    if (do_pheno) {
        ch_size_logs = ch_size_logs.mix(PHENOTYPE.out.size_log)
    }

    // Add postprocessing QC size logs if enabled
    if (!params.skip_postprocessing_qc) {
        ch_size_logs = ch_size_logs
            .mix(GENERATE_POSTPROCESSING_QC.out.size_log)
    }

    // Collect versions from all postprocessing processes.
    // QUANTIFY_MARKERS / ASSEMBLE_EXPORT already applied `.first()` internally
    // (see the comments on their `versions` emits) — do not re-apply it here.
    ch_versions = Channel.empty()
        .mix(SPLIT_CHANNELS.out.versions.first())
        .mix(QUANTIFY_MARKERS.out.versions)
        .mix(MERGE_QUANT_CSVS.out.versions.first())
        .mix(ASSEMBLE_EXPORT.out.versions)
        .mix(CHECKPOINT_WRITER.out.versions.first())

    if (do_pheno) {
        ch_versions = ch_versions.mix(PHENOTYPE.out.versions.first())
        if (params.panel_spec) {
            ch_versions = ch_versions.mix(COMPILE_PANEL.out.versions.first())
        }
    }

    if (!params.skip_postprocessing_qc) {
        ch_versions = ch_versions
            .mix(GENERATE_POSTPROCESSING_QC.out.versions.first())
    }

    emit:
    checkpoint_csv    = ch_checkpoint_csv
    postprocess_qc    = ch_postprocess_qc
    size_logs         = ch_size_logs
    versions          = ch_versions
}
