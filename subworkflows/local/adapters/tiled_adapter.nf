/*
========================================================================================
    TILED (STARE) REGISTRATION ADAPTER
========================================================================================
    Converts the patient-grouped structure into the tiled method's per-moving-slide star:
    every moving slide registers directly to the fixed reference (which defines the frame),
    independently and in parallel. Unlike the VALIS adapter there is no batch graph build and
    no per-slide OME-channel re-matching — each task already carries its slide's meta.

    Input:  ch_grouped_meta - Channel of [patient_id, reference_item, all_items]
    Output: same channel contract as VALIS_ADAPTER (registered / manifest / stage_checkpoint /
            size_logs / versions / summary), so the registration subworkflow is unchanged.
========================================================================================
*/

include { TILED_REGISTER } from '../../../modules/local/tiled_register'

workflow TILED_ADAPTER {
    take:
    ch_grouped_meta   // [patient_id, reference_item, all_items]

    main:
    // The reference is the frame — it passes through unregistered.
    ch_reference = ch_grouped_meta.map { _pid, ref_item, _items -> ref_item }  // [meta, file]

    // One registration task per moving (non-reference) slide: [meta, reference_file, moving_file].
    ch_moving = ch_grouped_meta.flatMap { _pid, ref_item, items ->
        def ref_file = ref_item[1]
        items.findAll { item -> !item[0].is_reference }
             .collect { mov -> tuple(mov[0], ref_file, mov[1]) }
    }

    TILED_REGISTER(ch_moving)

    ch_registered = TILED_REGISTER.out.registered.mix(ch_reference)

    emit:
    registered       = ch_registered
    // Manifest keyed by patient, mirroring VALIS_ADAPTER.out.registrar so the reg_qc=2 seg-QC
    // wiring can consume it (via --method tiled) the same way it consumes the VALIS pickle.
    manifest         = TILED_REGISTER.out.manifest.map { meta, m -> tuple(meta.patient_id, m) }
    // The tiled method composes no stages destructively, so it needs no pre-micro checkpoint.
    stage_checkpoint = Channel.empty()
    size_logs        = TILED_REGISTER.out.size_log
    versions         = TILED_REGISTER.out.versions.first()
    summary          = TILED_REGISTER.out.tre.map { _meta, f -> f }
}
