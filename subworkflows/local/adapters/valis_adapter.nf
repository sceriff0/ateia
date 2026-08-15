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

    THE ADAPTER CONTRACT lives in lib/AdapterContract.groovy, not here. It declares the
    emit names every adapter must fill, the tuple shape of each, and -- the part a comment
    kept getting wrong -- the CARDINALITY each one carries under this backend. It used to
    be a ~23-line table copied verbatim into both adapter files, so it was two tables, and
    it declared only names -- while the emit named `transform` carries ONE ROW PER PATIENT
    here and one row per MOVING SLIDE under the tiled adapter, which is the fact consumers
    actually branch on.
    tests/test_adapter_contract.py checks this file against that declaration, and
    tests/subworkflows/local/adapters/adapter_cardinality.nf.test counts what it really
    emits. Adding an emit, or a third backend, starts there.
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

            // A slide's identity within a patient is its channel set, and what that means
            // -- how it is spelled, and what a repeat does -- belongs to
            // lib/PanelSignature.groovy, not to this file. This used to compute the key
            // inline and throw its own message; SEG_QC computed the same concept with a
            // different separator and had no opinion about repeats at all, which is how
            // one input came to hard-fail here and silently cross-produce there.
            //
            // REGISTER_PATIENT already refused a duplicate before either adapter ran, so
            // reaching this call means something bypassed the group (an adapter invoked
            // directly, e.g. from an nf-test). It is one line and it is the point where an
            // ambiguous signature would actually corrupt the matching below -- a duplicate
            // key silently overwrites in collectEntries and one slide's file is then
            // attributed to the other slide's metadata.
            PanelSignature.requireUniqueWithinPatient(patient_id, metas_list)

            // Build lookup: channel signature (from CSV meta) -> meta
            def channel_key_to_meta = metas_list.collectEntries { meta ->
                [(PanelSignature.of(meta)): meta]
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

                // The same signature, computed from the OME-XML channel names the
                // registered file actually carries. Both sides go through one function so
                // the two spellings cannot drift apart and quietly match nothing.
                def ome_key = PanelSignature.ofChannels(ome_channels)
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
