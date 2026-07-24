/*
========================================================================================
    REG_COMPARE
========================================================================================
    Run BOTH registration paths over the SAME slides and report how far apart their outputs
    are. Opt-in via --reg_compare; it costs 2x registration, which is why it is off by default.

    The bit-identity suite proves the low-memory path equals classic VALIS on 128 px synthetic
    fixtures. This is how the same question gets an answer on a REAL slide: classic VALIS is the
    reference, the distributed/low-memory path is the candidate, and COMPARE_REGISTRATION streams
    a per-channel diff between them.

    Classic remains the run's real output. A comparison run must not change what the pipeline
    produces, or the numbers describe a pipeline nobody ran.

    Input:  [patient_id, reference_item, all_items]   (same shape as either adapter)
    Output: registered (classic), registrar (classic), metrics, diff_png, size_logs, versions
========================================================================================
*/

include { VALIS_ADAPTER             } from './adapters/valis_adapter'
include { VALIS_DISTRIBUTED_ADAPTER } from './adapters/valis_distributed_adapter'
include { COMPARE_REGISTRATION      } from '../../modules/local/compare_registration'

workflow REG_COMPARE {
    take:
    ch_grouped_multi   // [patient_id, reference_item, all_items]

    main:
    VALIS_ADAPTER(ch_grouped_multi)
    VALIS_DISTRIBUTED_ADAPTER(ch_grouped_multi)

    // Pair the two paths' outputs per slide.
    //
    // The key is [patient_id, sorted channel signature], NOT meta.id: `id` is optional on the
    // registration metas (several fixtures and the --start registration entry point omit it), so
    // keying on it collapses every slide of a patient onto [pid, null]. The channel signature is
    // the key registration.nf already uses for the feature-error join, and VALIS_ADAPTER itself
    // rejects a patient whose slides do not have unique channel signatures — so uniqueness here
    // is enforced upstream, not assumed.
    //
    // failOnMismatch/failOnDuplicate turn the join's silent behaviour into an error. Without them
    // a key that never matches simply drops the slide: COMPARE_REGISTRATION would run zero times
    // and the comparison would "pass" by never happening.
    ch_classic = VALIS_ADAPTER.out.registered
        .map { meta, f -> tuple([meta.patient_id.toString(), meta.channels.toSorted().join('|')], meta, f) }
    ch_candidate = VALIS_DISTRIBUTED_ADAPTER.out.registered
        .map { meta, f -> tuple([meta.patient_id.toString(), meta.channels.toSorted().join('|')], f) }

    ch_pairs = ch_classic
        .join(ch_candidate, failOnMismatch: true, failOnDuplicate: true)
        .map { key, meta, classic, candidate -> tuple(meta, classic, candidate) }

    ch_pairs.count().subscribe { n ->
        if (n == 0) {
            log.warn "--reg_compare: no multi-slide patients to compare (nothing was registered)"
        } else {
            log.info "--reg_compare: diffing ${n} slide(s) — classic VALIS vs the low-memory path"
        }
    }

    COMPARE_REGISTRATION(ch_pairs)

    ch_versions = VALIS_ADAPTER.out.versions
        .mix(VALIS_DISTRIBUTED_ADAPTER.out.versions)
        .mix(COMPARE_REGISTRATION.out.versions.first())

    emit:
    registered = VALIS_ADAPTER.out.registered   // classic remains the run's real output
    registrar  = VALIS_ADAPTER.out.registrar    // classic always runs here, so reg_qc=2 still works
    metrics    = COMPARE_REGISTRATION.out.metrics
    diff_png   = COMPARE_REGISTRATION.out.diff_png
    size_logs  = VALIS_ADAPTER.out.size_logs.mix(VALIS_DISTRIBUTED_ADAPTER.out.size_logs)
    versions   = ch_versions
    summary    = VALIS_ADAPTER.out.summary
}
