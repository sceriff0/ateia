/*
========================================================================================
    VALIS REGISTRATION ADAPTER
========================================================================================
    Adapter that converts patient-grouped data to VALIS batch format and back.

    VALIS requires all images for a patient at once to build optimal transformation graph.
    This adapter handles the batch conversion while maintaining the standard interface.

    Input:  ch_grouped_meta - Channel of [patient_id, reference_item, all_items]
            where reference_item = [meta, file] for the reference image
            and all_items = [[meta1, file1], [meta2, file2], ...] for all images
    Output: Channel of [meta, file] tuples (standard format)

    THE ADAPTER CONTRACT (identical in adapters/tiled_adapter.nf, and binding on any third
    adapter). Every registration adapter takes [patient_id, reference_item, all_items]
    and emits EXACTLY these names:

        registered          [meta, file]                registered slides (+ passthroughs)
        transform           [patient_id, transform]     ONE transform object per patient
        transform_by_slide  [meta, transform]           one transform per MOVING slide
        stage_checkpoint    [patient_id, dir]           intermediate-stage fields
        intrinsic_tre       file                        the method's OWN target-registration
                                                        -error estimate, whatever its format
        size_logs / versions

    `intrinsic_tre` is deliberately NOT named after any one method. Both shipped backends
    estimate a TRE from their own registration -- VALIS a feature-distance CSV, STARE a
    *_tre.json -- and the seam used to call the slot `summary` and then re-emit it as
    `valis_summary`, which pinned one method's name into the artifact vocabulary all the way
    out to the QC report. Formats are NOT normalised here; that is the reader's job.

    A method that produces no artifact for one of these emits `Channel.empty()` for it --
    a NULL OBJECT, never a missing emit and never an error. That is what lets
    REGISTER_PATIENT wire both backends with one short branch, and it is why nothing
    downstream of that branch has to know which method ran. A future adapter inherits the
    rule: declare every name; empty the ones your method cannot produce.
========================================================================================
*/

include { REGISTER } from '../../../modules/local/register'

workflow VALIS_ADAPTER {
    take:
    ch_grouped_meta   // Channel of [patient_id, reference_item, all_items] from grouping

    main:
    // ========================================================================
    // CONVERT TO VALIS BATCH FORMAT
    // ========================================================================
    // VALIS needs: [patient_id, reference_file, [all_files], [all_metas]]

    ch_valis_input = ch_grouped_meta
        .map { patient_id, ref_item, all_items ->
            def ref_file = ref_item[1]

            // VALIS requires all slides, including the reference, for graph optimization.
            // We pass reference both separately (for --reference flag) AND in all_files
            // The REGISTER process uses stageAs to avoid filename collision
            def all_files = all_items.collect { item -> item[1] }
            def all_metas = all_items.collect { item -> item[0] }

            def meta = [patient_id: patient_id]
            tuple(meta, patient_id, ref_file, all_files, all_metas)
        }

    // ========================================================================
    // RUN VALIS BATCH REGISTRATION
    // ========================================================================

    REGISTER(ch_valis_input)

    // ========================================================================
    // CONVERT BACK TO STANDARD FORMAT
    // ========================================================================
    // VALIS outputs: [patient_id, [registered_files], [metas], manifest_file]
    // Need to convert to: [meta, file]
    //
    // Match registered outputs back to metadata using OME channel names
    // from the channels manifest (no filename parsing).

    ch_registered = REGISTER.out.registered
        .flatMap { patient_id, reg_files, metas, manifest_file ->
            // Normalize to lists — Nextflow unwraps single-element globs/vals
            // to bare Path/Map objects, breaking .size() and .collect()
            def files_list = reg_files instanceof List ? reg_files : [reg_files]
            def metas_list = metas instanceof List ? metas : [metas]

            // Sanity check: file count must match metadata count
            if (files_list.size() != metas_list.size()) {
                def error_msg = """
                VALIS adapter: file count mismatch for patient ${patient_id}
                Expected ${metas_list.size()} files but got ${files_list.size()}
                Metadata entries: ${metas_list.collect { it.channels.join('_') }.join(', ')}
                Files: ${files_list.collect { it.name }.join(', ')}
                """.stripIndent()
                throw new Exception(error_msg)
            }

            // Read OME channels manifest (maps filename -> channel names from OME-XML)
            def manifest = new groovy.json.JsonSlurper().parseText(manifest_file.text)

            // Build lookup: sorted channel signature (from CSV meta) -> meta
            def channel_key_to_meta = metas_list.collectEntries { meta ->
                // IMPORTANT: Use toSorted() instead of sort() to avoid mutating meta.channels.
                // (Elsewhere in the pipeline, meta.clone() was replaced with `meta + [...]`
                // map addition — but Groovy's Map.plus() is implemented as
                // cloneSimilarMap(left).putAll(right), i.e. clone-then-putAll, so the two
                // are operationally identical. That was an idiom change, not a safety
                // change: meta.channels is still a shared List reference across any
                // derived metas that do not explicitly rebind it. sort()'s in-place
                // mutation would corrupt that shared list for every other meta holding the
                // same reference — hence toSorted(), here and at every future call site.)
                def key = meta.channels.toSorted().join('_').toLowerCase()
                [(key): meta]
            }

            // Guard against duplicate channel signatures (would silently overwrite)
            if (channel_key_to_meta.size() != metas_list.size()) {
                def signatures = metas_list.collect { it.channels.toSorted().join('_').toLowerCase() }
                def duplicates = signatures.groupBy { it }.findAll { _sig, v -> v.size() > 1 }.keySet()
                throw new Exception("""
                VALIS adapter: duplicate channel signatures for patient ${patient_id}
                ${metas_list.size()} slides but only ${channel_key_to_meta.size()} unique channel sets
                Duplicated: ${duplicates.join(', ')}
                Each slide must have a unique combination of channels
                """.stripIndent())
            }

            // Match each registered file by its OME channel signature
            files_list.collect { reg_file ->
                def ome_channels = manifest[reg_file.name]
                if (!ome_channels) {
                    def error_msg = """
                    VALIS adapter: no OME channel metadata for ${reg_file.name}
                    Patient: ${patient_id}
                    Manifest keys: ${manifest.keySet().join(', ')}
                    Check that registered files have OME-XML metadata with channel names
                    """.stripIndent()
                    throw new Exception(error_msg)
                }

                // Create sorted channel signature from OME metadata
                def ome_key = (ome_channels as List).toSorted().join('_').toLowerCase()
                def matched_meta = channel_key_to_meta[ome_key]

                if (!matched_meta) {
                    def error_msg = """
                    VALIS adapter: could not match OME channels to CSV metadata
                    Patient: ${patient_id}
                    File: ${reg_file.name}
                    OME channels: ${ome_channels}
                    OME key: ${ome_key}
                    Available CSV keys: ${channel_key_to_meta.keySet().join(', ')}
                    Ensure CSV 'channels' column matches OME-XML channel names
                    """.stripIndent()
                    throw new Exception(error_msg)
                }

                [matched_meta, reg_file]
            }
        }

    // Collect size logs
    ch_size_logs = REGISTER.out.size_log

    emit:
    registered = ch_registered
    // [patient_id, registrar.pickle] — VALIS's transform is ONE object per patient: the graph
    // it optimised over the whole group. Consumed by the GeoJSON seg-QC warper.
    transform  = REGISTER.out.registrar
    // VALIS has no per-slide transform: the registrar is a single group-wide object and is not
    // decomposable into one transform per moving slide. This is the null object the contract
    // above requires — not an omission, and not an error for any consumer.
    transform_by_slide = Channel.empty()
    // [patient_id, reg_stage_checkpoint/] — pre-micro displacement fields, emitted only at
    // reg_qc >= 2. Lets WARP_SEG_QC score the non_rigid stage apart from micro; VALIS composes
    // the two into one field, so REGISTER is the only place they can be told apart.
    stage_checkpoint = REGISTER.out.stage_checkpoint
    size_logs  = ch_size_logs
    versions   = REGISTER.out.versions.first()
    // VALIS's intrinsic TRE: `error_df` written as preprocessed/data/*_summary.csv, the
    // feature distances it measures on its OWN SuperPoint/SuperGlue keypoints.
    intrinsic_tre = REGISTER.out.summary
}
