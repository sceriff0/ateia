/*
========================================================================================
    SUBWORKFLOW: QUANTIFY_MARKERS
========================================================================================
    The per-marker quantification chain, shared by the linear postprocessing path
    and the incremental add_cycle path:

        split-channel stacks -> per-marker fan-out -> QUANTIFY -> per-patient group

    Both callers used to carry their own copy of this chain and the copies drifted:
    add_cycle grouped the quantification CSVs with a bare `.groupTuple()` and so lost
    the `groupKey(patient_id, channels_count)` streaming hint that the postprocessing
    copy had. There is now exactly ONE grouping implementation and it carries the hint.

    The two callers differ only in where the masks come from (SEGMENT vs the prior
    run's reused masks) and in what they do with the grouped CSVs afterwards
    (join morphology.csv vs merge onto a prior base table). Both differences are
    parameterised through `take:`/`emit:` — nothing in here branches on params.mode.
========================================================================================
*/

include { QUANTIFY } from '../../modules/local/quantify'

// Local copy of postprocess.nf's debug helper: the `.view()` calls below moved in
// with the chain they annotate. Off unless --debug_channels, and log-only either way.
def viewIfDebug(channel, Closure formatter) {
    return params.debug_channels ? channel.view(formatter) : channel
}

workflow QUANTIFY_MARKERS {
    take:
    ch_split_channels   // [meta, tiffs]                        — SPLIT_CHANNELS.out.channels
    ch_masks            // [patient_id, cell_mask, nuclei_mask]

    main:

    ch_flatmapped = ch_split_channels
        .flatMap { meta, tiffs ->
            // Ensure tiffs is always a list (handle both single file and multiple files)
            def tiff_list = tiffs instanceof List ? tiffs : [tiffs]

            // Create unique meta map for each channel file. `+` creates a new
            // top-level map, but Groovy's Map.plus() is
            // cloneSimilarMap(left).putAll(right) -- clone-then-putAll,
            // operationally identical to clone(). meta.channels is still the
            // same List reference as in the original meta. See
            // subworkflows/local/adapters/valis_adapter.nf:82-88 for why
            // that matters and why toSorted() (not sort()) is mandatory
            // wherever meta.channels is read.
            tiff_list.collect { tiff ->
                def channel_meta = meta + [
                    id: "${meta.patient_id}_${tiff.baseName}",
                    channel_name: tiff.baseName
                ]
                [channel_meta, tiff]
            }
        }
    ch_flatmapped = viewIfDebug(
        ch_flatmapped,
        { meta, tiff -> "After flatMap: id=${meta.id}, channel=${meta.channel_name}, tiff=${tiff.name}" }
    )

    ch_for_combine = ch_flatmapped
        .map { meta, tiff -> [meta.patient_id, meta, tiff] }
    ch_for_combine = viewIfDebug(
        ch_for_combine,
        { patient_id, _meta, _tiff -> "Before combine: key=${patient_id}, channel=${_meta.channel_name}" }
    )

    // Both masks (cell + nuclei) arrive keyed by patient_id. QUANTIFY only reads the
    // nuclear one when params.quantify_compartments is set (per-compartment signal).
    ch_masks_viewed = viewIfDebug(
        ch_masks,
        { patient_id, _cell, _nuc -> "Masks available: key=${patient_id}, cell=${_cell.name}, nuclei=${_nuc.name}" }
    )

    // combine(by: 0), not join: one mask pair fans out over the patient's N markers.
    ch_for_quant = ch_for_combine
        .combine(ch_masks_viewed, by: 0)
        .map { _patient_id, meta, tiff, cell_mask, nuclei_mask -> [meta, tiff, cell_mask, nuclei_mask] }
    ch_for_quant = viewIfDebug(
        ch_for_quant,
        { meta, _tiff, _cell, _nuc -> "After combine: patient=${meta.patient_id}, channel=${meta.channel_name}" }
    )

    QUANTIFY(ch_for_quant)

    // ========================================================================
    // GROUP - Collect per-marker CSVs by patient_id
    // Deduplicate by patient_id + marker (take first occurrence if same marker appears multiple times)
    // Use groupKey for streaming - emits as soon as channels_count items collected
    //
    // `remainder: true` is what makes the sized key a HINT rather than an assertion.
    // channels_count is the patient's DISTINCT channel count from the samplesheet
    // (CsvUtils.countChannelsPerPatient). On the linear path that equals the number of
    // QUANTIFY tasks exactly, so every group fills and streams early. In add_cycle it
    // does NOT: SPLIT_CHANNELS drops DAPI from a non-reference slide, so a new cycle
    // declared `DAPI|KI67|CD20` quantifies 2 markers against a channels_count of 3.
    // groupTuple discards under-sized groups by default, which silently deletes the
    // patient from the merge/export. With remainder the group is emitted when the
    // upstream channel completes — exactly what add_cycle's former bare .groupTuple()
    // did — while a group that does reach its size still emits early.
    // ========================================================================
    ch_grouped_csvs = QUANTIFY.out.individual_csv
        .map { meta, csv ->
            def marker = meta.channel_name  // Extract marker name
            [[meta.patient_id, marker], meta, csv]  // Key by [patient_id, marker]
        }
        .unique { entry -> entry[0] }  // Keep only first occurrence of each [patient_id, marker] pair
        .map { key, meta, csv ->
            // Use groupKey for streaming if channels_count is available
            def gkey = meta.channels_count
                ? groupKey(key[0], meta.channels_count)
                : key[0]
            [gkey, meta, csv]
        }
        .groupTuple(by: 0, remainder: true)
        .map { patient_id, metas, csvs ->
            // `+` creates a new top-level map, but Groovy's Map.plus() is
            // cloneSimilarMap(left).putAll(right) -- clone-then-putAll,
            // operationally identical to clone(). metas[0].channels is still
            // the same List reference as in the original meta. See
            // subworkflows/local/adapters/valis_adapter.nf:82-88 for why
            // that matters and why toSorted() (not sort()) is mandatory
            // wherever meta.channels is read.
            // Extract actual patient_id from groupKey wrapper if needed
            def meta = metas[0] + [id: patient_id.toString()]
            [meta, csvs]
        }

    emit:
    // [meta, [per-marker quant csvs]] — one entry per patient.
    grouped_csv = ch_grouped_csvs
    size_logs   = QUANTIFY.out.size_log
    // `.first()` is applied HERE, inside the subworkflow, matching seg_qc.nf:112 and
    // adapters/valis_adapter.nf:151 — NOT the call-site style postprocess.nf:420-442
    // uses for its own inline processes. registration.nf:309 documents that
    // asymmetry; every new subworkflow de-duplicates its own versions so callers
    // never have to know which convention a given emit follows.
    versions    = QUANTIFY.out.versions.first()
}
