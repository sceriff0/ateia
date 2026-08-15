/*
========================================================================================
    SUBWORKFLOW: SEGMENTATION
========================================================================================
    Carves segmentation out of the old monolithic POSTPROCESSING step: SEGMENT the
    reference image, extract cell (and optionally nucleus) contours/morphology, and
    write the `segmented` checkpoint (Layout.SEGMENTED / Checkpoint.columns('segmented')).

    This is the block that used to live at subworkflows/local/postprocess.nf:50-85,
    moved verbatim, plus the checkpoint writer that gives it its own resume point.

    Input:
        ch_registered: [meta, file] — ALL slides (reference + moving) that reached
                       the registered stream. Only the reference subset is segmented;
                       every slide gets a row in the checkpoint (see the writer below).

    Output:
        cell_mask        [meta, file]         — SEGMENT.out.cell_mask, unchanged
        nuclei_mask      [meta, file]         — SEGMENT.out.nuclei_mask, unchanged
        contours         [patient_id, file]   — EXTRACT_CELL_PROPERTIES.out.contours, re-keyed
        nucleus_contours [patient_id, file]   — EXTRACT_NUCLEI_PROPERTIES.out.contours, re-keyed;
                                                 Channel.empty() when --quantify_compartments is false
        checkpoint_csv                        — csv/segmented.csv
        size_logs, versions                   — SEGMENT + EXTRACT_CELL_PROPERTIES (+
                                                 EXTRACT_NUCLEI_PROPERTIES when compartments run)

    Also defines READ_SEGMENTED_CHECKPOINT, the `--start postprocessing` reader: unlike
    every other step, postprocessing's entry checkpoint (segmented.csv) carries FOUR
    extra columns INPUT_CHECK's `[meta, one_file]` shape cannot express. It lives here,
    not in workflows/mirage.nf, so Task 3 (which needs the same four columns for its own
    entry point) can reuse it without opening the checkpoint a second way.
========================================================================================
*/

include { SEGMENT                   } from '../../modules/local/segment'
include { EXTRACT_CELL_PROPERTIES   } from '../../modules/local/extract_cell_properties'
include { EXTRACT_NUCLEI_PROPERTIES } from '../../modules/local/extract_nuclei_properties'
include { CHECKPOINT_WRITER         } from './checkpoint_writer'

workflow SEGMENTATION {
    take:
    ch_registered       // [meta, file] — all slides (reference + moving)
    compartment_mode    // ParamUtils.compartmentMode(params) — resolved once by
                        // workflows/mirage.nf and threaded down; see that file's
                        // comment on the seam. Only `.compartments` is read here.

    main:

    // ========================================================================
    // SEGMENTATION - Process reference images only
    // ========================================================================
    ch_references = ch_registered
        .filter { meta, file -> meta.is_reference }

    ch_references.ifEmpty {
        error "No reference images found (is_reference=true). Cannot run segmentation."
    }

    // The parameter slice the backends read, resolved ONCE here. Workflow code is not
    // hashed by Nextflow's free-variable rule, so reading `params` at this level is free;
    // reading it inside SEGMENT's script block was not -- it bound the task's cache key to
    // every parameter in the pipeline. SegBackends owns WHICH keys (CTX_PARAM_KEYS), so
    // the process body stays backend-agnostic.
    def seg_params = SegBackends.ctxParams(params)

    SEGMENT(ch_references.map { meta, f -> tuple(meta, f, seg_params) })

    def ch_cell_mask   = SEGMENT.out.cell_mask
    def ch_nuclei_mask = SEGMENT.out.nuclei_mask

    // ========================================================================
    // CELL PROPERTIES - Extract morphology + contours from mask (runs in PARALLEL with SPLIT_CHANNELS)
    // Computes regionprops ONCE instead of N times in QUANTIFY
    // ========================================================================
    EXTRACT_CELL_PROPERTIES(ch_cell_mask)

    def ch_contours = EXTRACT_CELL_PROPERTIES.out.contours
        .map { meta, json_file -> [meta.patient_id, json_file] }

    // Nucleus contours (re-keyed to cell labels) for dual-segmentation GeoJSON export.
    // Only computed when per-compartment quantification is enabled.
    // Default to empty so the channel is always defined (the export join below
    // only consumes it when quantify_compartments is set, but an unassigned
    // `def` is a fragile null to leave in a channel expression).
    def ch_nucleus_contours = Channel.empty()
    if (compartment_mode.compartments) {
        ch_nuclei_props_in = ch_nuclei_mask
            .map { meta, mask -> [meta.patient_id, meta, mask] }
            .join(ch_cell_mask.map { meta, mask -> [meta.patient_id, mask] }, by: 0)
            .map { _patient_id, meta, nuclei_mask, cell_mask -> [meta, nuclei_mask, cell_mask] }
        EXTRACT_NUCLEI_PROPERTIES(ch_nuclei_props_in)
        ch_nucleus_contours = EXTRACT_NUCLEI_PROPERTIES.out.contours
            .map { meta, json_file -> [meta.patient_id, json_file] }
    }

    // ========================================================================
    // CHECKPOINT - csv/segmented.csv
    // ========================================================================
    // One row per SLIDE (not per patient), because the next step needs the registered
    // images as well as the masks: --start postprocessing reads this file, and
    // INPUT_CHECK/READ_SEGMENTED_CHECKPOINT take one image column per row. The
    // per-patient mask columns are therefore repeated across a patient's rows, which
    // is denormalised on purpose — it is what keeps the entry contract a single image
    // column per row.
    //
    // registered_image's published path uses publishedOrAsIs rather than
    // subworkflows/local/registered_checkpoint.nf's plain registeredPath, and the
    // difference is where the file comes from, not what kind it is. That writer's
    // ch_registered is ALWAYS fresh out of THIS run's REGISTRATION. This file's is not:
    // at `--start segmentation` it is INPUT_CHECK.out.samples reading csv/registered.csv
    // back -- already an absolute published path, possibly from a prior run's outdir --
    // and reconstructing that against this run's outdir would name a file that is not
    // there. publishedOrAsIs's isUnderTaskDir check tells the two apart.
    //
    // ONE kind, no branch on is_passthrough. There used to be one, asking for
    // PREPROCESSED when the slide had never been warped, because that is where such a
    // slide was published. PUBLISH_PASSTHROUGH publishes it as a registered slide now
    // (see register_patient.nf), so the branch is not merely redundant -- it is wrong:
    // the passthrough's file sits in a `registered_slides/` producer subdirectory, and
    // publishedOrAsIs would carry that subdirectory under the PREPROCESSED kind and
    // record <pid>/preprocessed/registered_slides/<name>, a path nothing publishes.
    // Observed exactly that way on a tiled stub run, which exited 0 with the broken row
    // in csv/segmented.csv; tests/checkpoint_manifest.nf.test's tiled case is what
    // fails on it.
    ch_registered_for_ckpt = ch_registered
        .map { meta, file ->
            def published_path =
                Layout.publishedOrAsIs(params.outdir, meta.patient_id, Layout.REGISTERED, file)
            [meta.patient_id, meta, published_path]
        }

    // cell_mask / nuclei_mask / contours are ALL unconditional: SEGMENT always emits
    // BOTH masks regardless of --quantify_compartments (only EXTRACT_NUCLEI_PROPERTIES
    // -- which produces nucleus_contours, not nuclei_mask -- is gated on it), and
    // EXTRACT_CELL_PROPERTIES always runs on the cell mask. Recording nuclei_mask
    // unconditionally matters beyond completeness: postprocess.nf's
    // `ch_mask = ch_cell_mask.join(ch_nuclei_mask, by: 0)` is a PLAIN inner join on the
    // documented invariant "the nuclear mask is always available from SEGMENT" --
    // withholding it here when compartments are off would make that join drop every
    // patient silently (an empty ch_nuclei_mask joined against anything is empty),
    // gutting the ENTIRE postprocessing output tree while the run exits 0. That was
    // caught in review; nucleus_contours alone carries the empty-column convention.
    //
    // All three match ch_references' patient set 1:1 (enforced by the ifEmpty check
    // above), so a plain combine() below cannot drop a row for any of them.
    ch_cell_mask_path = ch_cell_mask.map { meta, m ->
        [meta.patient_id, Layout.publishedPath(params.outdir, meta.patient_id, 'segmentation', m)]
    }
    ch_nuclei_mask_path = ch_nuclei_mask.map { meta, m ->
        [meta.patient_id, Layout.publishedPath(params.outdir, meta.patient_id, 'segmentation', m)]
    }
    ch_contours_path = ch_contours.map { pid, j ->
        [pid, Layout.publishedPath(params.outdir, pid, 'cell_properties', j)]
    }

    // nucleus_contours ALONE is recorded only under --quantify_compartments:
    // EXTRACT_NUCLEI_PROPERTIES does not run at all when compartments are off
    // (ch_nucleus_contours above is Channel.empty() in that case). A plain
    // combine()/join() of an EMPTY optional channel against the per-patient backbone
    // below would silently DROP every patient's row from the checkpoint instead of
    // recording an empty field — subworkflows/local/seg_qc.nf:96-101 solves exactly
    // this shape (join(..., remainder: true) against a per-key placeholder, so a
    // missing optional value becomes '' rather than an absent row) and is copied here.
    ch_nucleus_contours_value = compartment_mode.compartments
        ? ch_nucleus_contours.map { pid, j -> [pid, Layout.publishedPath(params.outdir, pid, 'cell_properties', j)] }
        : Channel.empty()

    ch_nucleus_contours_total = ch_cell_mask_path
        .map { pid, _path -> tuple(pid, '') }
        .join(ch_nucleus_contours_value, by: 0, remainder: true)
        .map { pid, placeholder, real -> tuple(pid, real ?: placeholder) }

    // meta.images_count is the size hint CHECKPOINT_WRITER needs: this checkpoint has
    // one row per SLIDE (see the comment above), and ch_registered carries exactly the
    // patient's slides in both of this subworkflow's entry modes -- from REGISTRATION on
    // the linear path, or from INPUT_CHECK reading csv/registered.csv at
    // `--start segmentation`. Both derive their metas from input_check.nf, which injects
    // images_count into every one of them; the adapters carry the meta through unchanged
    // (valis_adapter.nf matches registered files back to the ORIGINAL metas). add_cycle,
    // the one path where that count disagrees with the registered row count, never runs
    // segmentation at all.
    ch_checkpoint_rows = ch_registered_for_ckpt
        .combine(ch_cell_mask_path, by: 0)
        .combine(ch_nuclei_mask_path, by: 0)
        .combine(ch_contours_path, by: 0)
        .combine(ch_nucleus_contours_total, by: 0)
        .map { pid, meta, reg_path, cell_mask, nuclei_mask, contours, nucleus_contours ->
            [pid, meta.images_count, Checkpoint.row(Layout.SEGMENTED, [
                patient_id      : pid,
                registered_image: reg_path,
                is_reference    : meta.is_reference,
                channels        : meta.channels.join('|'),
                cell_mask       : cell_mask,
                nuclei_mask     : nuclei_mask,
                contours        : contours,
                nucleus_contours: nucleus_contours,
            ])]
        }

    CHECKPOINT_WRITER(Layout.SEGMENTED, ch_checkpoint_rows)
    ch_checkpoint_csv = CHECKPOINT_WRITER.out.csv

    ch_size_logs = Channel.empty()
        .mix(SEGMENT.out.size_log)
        .mix(EXTRACT_CELL_PROPERTIES.out.size_log)
    if (compartment_mode.compartments) {
        ch_size_logs = ch_size_logs.mix(EXTRACT_NUCLEI_PROPERTIES.out.size_log)
    }

    ch_versions = Channel.empty()
        .mix(SEGMENT.out.versions.first())
        .mix(EXTRACT_CELL_PROPERTIES.out.versions.first())
        .mix(CHECKPOINT_WRITER.out.versions.first())
    if (compartment_mode.compartments) {
        ch_versions = ch_versions.mix(EXTRACT_NUCLEI_PROPERTIES.out.versions.first())
    }

    emit:
    cell_mask        = ch_cell_mask
    nuclei_mask      = ch_nuclei_mask
    contours         = ch_contours
    nucleus_contours = ch_nucleus_contours
    // [meta, file] — EXTRACT_CELL_PROPERTIES.out.morphology, unchanged. Not part of
    // the checkpoint (morphology.csv is a same-run-only intermediate, never read back
    // by a later --start), but POSTPROCESSING still needs it to join against the
    // per-marker intensity CSVs (ch_morphology) and, when phenotyping, against
    // MERGE_QUANT_CSVS' output -- both joins moved out of this file with SEGMENT and
    // EXTRACT_CELL_PROPERTIES, so the raw output has to cross the seam somewhere.
    morphology       = EXTRACT_CELL_PROPERTIES.out.morphology
    checkpoint_csv   = ch_checkpoint_csv
    size_logs        = ch_size_logs
    versions         = ch_versions
}

/*
========================================================================================
    WORKFLOW: READ_SEGMENTED_CHECKPOINT
========================================================================================
    The `--start postprocessing` reader for csv/segmented.csv. Reading is
    lib/Checkpoint.groovy's job -- the same class that declares the schema and writes
    the rows -- so this workflow asks for `[meta, row]` and never restates a column
    name, an is_reference rule or a count. subworkflows/local/add_cycle.nf's
    prior-run readers go through the same call.

    WHAT MOVED, AND WHY IT MATTERS HERE. This reader used to build its own meta:
    `id: row.patient_id` and no counts. Both were wrong at exactly this entry point.
    `id` drives output file naming, and --start postprocessing is where several
    patients' outputs meet in one collect, so collapsing every slide of a patient onto
    one id is where identically named files actually overwrite each other. And the
    absent counts left every per-patient groupTuple downstream unsized, turning
    streaming off for the whole run with nothing said. Checkpoint.read supplies the
    same meta INPUT_CHECK does, so --start postprocessing now sees what every other
    entry point sees.

    INPUT_CHECK's `[meta, one_file]` shape is enough for every other step's entry
    point (each earlier checkpoint names exactly one path column per row). This one
    is not: postprocessing's entry additionally needs the four segmentation
    artifacts, which live as four more columns on the SAME row rather than in a
    separate file — hence a dedicated reader instead of a second INPUT_CHECK column.

    CONTOURS / MORPHOLOGY ARE RE-DERIVED, NOT RE-READ. The checkpoint's own 'contours'
    and 'nucleus_contours' columns record where SEGMENTATION's run published them (a
    complete manifest), but this reader does not read those two columns back: it
    re-runs EXTRACT_CELL_PROPERTIES (and, under --quantify_compartments,
    EXTRACT_NUCLEI_PROPERTIES) on the checkpoint's masks instead, exactly as
    subworkflows/local/add_cycle.nf:241-255 already does when it reuses a prior run's
    cell/nuclei masks. Two reasons: (1) morphology.csv is NOT a checkpoint column at
    all (see lib/Checkpoint.groovy's 'segmented' entry) — it is a same-run-only
    intermediate POSTPROCESSING needs for its quantification/phenotyping joins, so it
    has no persisted path to read back regardless; and (2) re-deriving contours
    alongside it, from the same mask, keeps this reader symmetric with add_cycle's
    only other "resume from a persisted mask" consumer instead of adding a second way
    to reconstruct the same polygons.

    Input:
        csv_path: path to a `segmented` checkpoint CSV (segmented.csv or add_cycle's
                  future analogue re-using this same shape)

    Output:
        samples          [meta, file]        — same shape as INPUT_CHECK.out.samples
        cell_mask        [meta, file]        — reference rows only
        nuclei_mask      [meta, file]        — reference rows only (SEGMENT always
                                                produces it; see Checkpoint's
                                                'segmented' columns)
        contours         [patient_id, file]  — re-derived from cell_mask
        nucleus_contours [patient_id, file]  — re-derived from nuclei_mask;
                                                Channel.empty() when
                                                --quantify_compartments is false
        morphology       [meta, file]        — re-derived from cell_mask
        size_logs, versions                  — of the re-run EXTRACT_CELL_PROPERTIES
                                                (+ EXTRACT_NUCLEI_PROPERTIES when
                                                compartments run); workflows/mirage.nf
                                                mixes these into FINAL_QC at the
                                                postprocessing-entry point, where
                                                SEGMENTATION never ran this session
========================================================================================
*/

workflow READ_SEGMENTED_CHECKPOINT {
    take:
    csv_path
    compartment_mode    // ParamUtils.compartmentMode(params) -- see SEGMENTATION's
                        // take: comment above; only `.compartments` is read here.

    main:
    // lib/Checkpoint.groovy owns BOTH sides of a checkpoint CSV now: it declares the
    // schema and it reads it. What used to stand here -- a hand-rolled loop asserting
    // that six column names Checkpoint declares, then a splitCsv + inline meta builder
    // -- is entirely subsumed: read() checks the file's header against the declared
    // column list (in both directions, unlike the assert), applies the strict
    // is_reference rule, derives the patient-prefixed per-image id, and derives both
    // counts or raises. The three readers of these files no longer get to disagree.
    ch_rows = Channel.fromList(
        Checkpoint.read(Layout.SEGMENTED, csv_path, [nuclear_markers: params.nuclear_markers])
    )

    ch_samples = ch_rows.map { meta, row -> [meta, file(row.registered_image)] }

    // Mask columns are denormalised across a patient's rows (see the writer's
    // comment in SEGMENTATION above) -- read them off the reference row only, so each
    // patient contributes exactly one row to cell_mask/nuclei_mask, matching
    // SEGMENTATION's own emit shapes.
    ch_ref_rows = ch_rows.filter { meta, _row -> meta.is_reference }

    ch_cell_mask   = ch_ref_rows.map { meta, row -> [meta, file(row.cell_mask)] }
    // Unconditional, like cell_mask: SEGMENT always produces nuclei_mask regardless
    // of --quantify_compartments (only nucleus_contours, below, is genuinely gated),
    // so this column is never empty -- no emptiness filter needed here.
    ch_nuclei_mask = ch_ref_rows.map { meta, row -> [meta, file(row.nuclei_mask)] }

    // Re-derive contours + morphology from the reused cell mask (see the class
    // comment above for why this is a re-run, not a checkpoint re-read).
    EXTRACT_CELL_PROPERTIES(ch_cell_mask)
    ch_contours   = EXTRACT_CELL_PROPERTIES.out.contours.map { meta, j -> [meta.patient_id, j] }
    ch_morphology = EXTRACT_CELL_PROPERTIES.out.morphology

    ch_size_logs = Channel.empty().mix(EXTRACT_CELL_PROPERTIES.out.size_log)
    ch_versions  = Channel.empty().mix(EXTRACT_CELL_PROPERTIES.out.versions.first())

    // Nucleus contours: only under --quantify_compartments, same gate as
    // SEGMENTATION's live-run block above (and add_cycle.nf:247-255).
    ch_nucleus_contours = Channel.empty()
    if (compartment_mode.compartments) {
        ch_nuclei_props_in = ch_nuclei_mask
            .map { meta, mask -> [meta.patient_id, meta, mask] }
            .join(ch_cell_mask.map { meta, mask -> [meta.patient_id, mask] }, by: 0)
            .map { _patient_id, meta, nuclei_mask, cell_mask -> [meta, nuclei_mask, cell_mask] }
        EXTRACT_NUCLEI_PROPERTIES(ch_nuclei_props_in)
        ch_nucleus_contours = EXTRACT_NUCLEI_PROPERTIES.out.contours
            .map { meta, j -> [meta.patient_id, j] }
        ch_size_logs = ch_size_logs.mix(EXTRACT_NUCLEI_PROPERTIES.out.size_log)
        ch_versions  = ch_versions.mix(EXTRACT_NUCLEI_PROPERTIES.out.versions.first())
    }

    emit:
    samples          = ch_samples
    cell_mask        = ch_cell_mask
    nuclei_mask      = ch_nuclei_mask
    contours         = ch_contours
    nucleus_contours = ch_nucleus_contours
    morphology       = ch_morphology
    // Re-run versions/size_log (EXTRACT_CELL_PROPERTIES + EXTRACT_NUCLEI_PROPERTIES).
    // At a46d2d6 (before this task) these ran inside POSTPROCESSING at every entry
    // point including --start postprocessing, so their versions.yml always reached
    // FINAL_QC. Emitting them here restores that at the one entry point where
    // run_segmentation is false and mirage.nf cannot get them from SEGMENTATION.out
    // instead (see workflows/mirage.nf's postprocessing-entry branch).
    size_logs        = ch_size_logs
    versions         = ch_versions
}
