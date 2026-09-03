/*
========================================================================================
    SUBWORKFLOW: POSTPROCESSED_CHECKPOINT
========================================================================================
    Writes the postprocessing checkpoint manifest (Layout.POSTPROCESSED, under
    Layout.checkpointDir) from the five per-patient artifact streams a completed
    patient result consists of.

    WHY THIS IS ITS OWN FILE — the second half of the same repair
    subworkflows/local/register_patient.nf's CHECKPOINT_WRITER call describes.
    That write briefly lived in a file of its own because
    subworkflows/local/registration.nf owned the only writer of the
    Layout.REGISTERED manifest, so an add_cycle run — which never goes through
    REGISTRATION — wrote none. Exactly the same was true here, one step later:
    subworkflows/local/postprocess.nf owned the only writer of the
    Layout.POSTPROCESSED manifest, and add_cycle has no POSTPROCESSING step
    because the masks and the base quantification table are REUSED rather than
    re-derived.

    The consequence was that cyclic-IF could not chain. lib/ParamUtils.groovy's
    validateAddCycle requires BOTH manifests named in Layout.ADD_CYCLE_CHECKPOINTS
    under --prior_outdir, so cycle 2 succeeded and cycle 3 refused at launch with
    "required checkpoint ... not found under --prior_outdir. Was the prior run
    completed through postprocessing?" (The message names the file via
    Layout.checkpointCsvRelative; it is not restated here, because
    tests/test_layout.py forbids a second statement of that path and is right to.)

    Reproduced 2026-08-25 under -stub: cycle 2 exit 0 with only the preprocessed
    and registered manifests in the checkpoint directory; cycle 3 exit 1 with
    that message. After this file: both cycles exit 0, and cycle 3 writes all
    three manifests in turn, so the chain does not terminate at any depth.

    add_cycle has every artifact a postprocessed row names — it rebuilds the
    combined geojson, csv and pyramid wholesale over the merged channel set, and
    carries the reused cell mask through — so nothing has to be recomputed to
    write one. Only the writer was missing.

    The row format is the contract with every reader (add_cycle.nf's
    `ch_prior_assets`, CsvUtils' checkpoint validation, the `--start
    postprocessing` samplesheet parser). It is owned by lib/Checkpoint.groovy —
    this file names the columns nowhere, it asks for them.

    Inputs are the raw `[meta, file]` producer streams rather than a pre-joined,
    pre-published tuple, so the publishedPath rules stay here in ONE place
    instead of being restated at each call site. That matters most for
    `ch_cell_mask`: see the publishedOrAsIs note on it below.

    WHAT THIS FILE OWNS, AND WHAT IT NO LONGER DOES. It owns the five-way join and
    the per-column publish decisions -- which Layout `kind` each artifact lands
    under, and the one case (`cell_mask`) where the file may already be an absolute
    published path from an earlier run. It does NOT own the WRITE any more:
    whether a manifest is written at this cleanup level, where it goes, in what
    order and under what header is subworkflows/local/checkpoint_writer.nf's, which
    four call sites used to answer identically and separately.

    Input:
        ch_cell_csv      [meta, file]  per-cell measurement CSV (EXPORT_GEOJSON)
        ch_cell_geojson  [meta, file]  cells.geojson              (EXPORT_GEOJSON)
        ch_merged_csv    [meta, file]  merged quantification table (MERGE_QUANT_CSVS)
        ch_cell_mask     [meta, file]  cell instance mask
        ch_pyramid       [meta, file]  pyramidal OME-TIFF        (MERGE_AND_PYRAMID)

    Output:
        csv: the collected postprocessing checkpoint manifest
========================================================================================
*/

include { CHECKPOINT_WRITER } from './checkpoint_writer'

workflow POSTPROCESSED_CHECKPOINT {
    take:
    ch_cell_csv
    ch_cell_geojson
    ch_merged_csv
    ch_cell_mask
    ch_pyramid

    main:
    // Use collectFile() for non-blocking aggregation (enables patient-level parallelism).
    // The join chain is kept (it's per-patient and doesn't block other patients).
    ch_base_checkpoint = ch_cell_csv
        .map { meta, csv ->
            def published_path = Layout.publishedPath(params.outdir, meta.patient_id, 'geojson', csv)
            // pixel_size rides along on this first stream's meta -- every stream
            // reaching this subworkflow is keyed by patient_id and (see the callers)
            // carries the SAME resolved scale, so there is exactly one number per
            // patient regardless of which stream it is read off.
            [meta.patient_id, published_path, meta.pixel_size]
        }
        .join(ch_cell_geojson.map { meta, geojson ->
            def published_path = Layout.publishedPath(params.outdir, meta.patient_id, 'geojson', geojson)
            [meta.patient_id, published_path]
        })
        .join(ch_merged_csv.map { meta, csv ->
            def published_path = Layout.publishedPath(params.outdir, meta.patient_id, 'quantification', csv)
            [meta.patient_id, published_path]
        })
        .join(ch_cell_mask.map { meta, mask ->
            // publishedOrAsIs, not publishedPath. This stream is the one column
            // whose file was not necessarily produced by THIS run:
            //
            //   --start postprocessing  READ_SEGMENTED_CHECKPOINT's file(row.cell_mask)
            //   mode=add_cycle          the prior run's mask, reused unchanged
            //
            // Both are already absolute published paths from an earlier run.
            // Calling publishedPath unconditionally would double-nest them
            // (producerSubdir sees a parent literally named 'segmentation' and
            // treats it as a producer subdirectory to preserve), e.g.
            // <outdir>/<pid>/segmentation/segmentation/<name>. See
            // Layout.publishedOrAsIs for the full explanation.
            //
            // So csv/postprocessed.csv can point OUTSIDE its own --outdir, which
            // no other checkpoint in this pipeline does. That is correct — the
            // mask genuinely was not recomputed — and it is exactly what makes an
            // add_cycle outdir usable as the next cycle's --prior_outdir.
            def published_path = Layout.publishedOrAsIs(params.outdir, meta.patient_id, 'segmentation', mask)
            [meta.patient_id, published_path]
        })
        .join(ch_pyramid.map { meta, pyramid ->
            def published_path = Layout.publishedPath(params.outdir, meta.patient_id, 'pyramid', pyramid)
            [meta.patient_id, published_path]
        })

    ch_checkpoint_rows = ch_base_checkpoint
        .map { patient_id, cell_csv, pixel_size, cell_geojson, merged_csv, cell_mask, pyramid ->
            [
                patient_id  : patient_id,
                // RULING R17: 'postprocessed' rows are per-PATIENT, not per-slide --
                // there is no single slide a pyramid/merged table belongs to -- so id
                // is the patient id itself, the same synthetic-id convention
                // add_cycle.nf already uses for its own patient-scoped metas
                // ([patient_id: pid, id: pid, ...]). See lib/Checkpoint.groovy.
                id          : patient_id,
                cell_csv    : cell_csv,
                cell_geojson: cell_geojson,
                merged_csv  : merged_csv,
                cell_mask   : cell_mask,
                pyramid     : pyramid,
                pixel_size  : pixel_size,
            ]
        }

    // Not written at a cleaning level either, which is the non-obvious one: cell_csv,
    // cell_geojson, merged_csv and pyramid are all final artifacts, but `cell_mask`
    // names the segmentation mask, and segmentation is an intermediate. One dangling
    // column is enough. The gate is CHECKPOINT_WRITER's; Checkpoint.writesAtLevel
    // carries the full reasoning and the observed failure.
    CHECKPOINT_WRITER(Layout.POSTPROCESSED, ch_checkpoint_rows)
    ch_checkpoint_csv = CHECKPOINT_WRITER.out.csv

    emit:
    csv = ch_checkpoint_csv
}
