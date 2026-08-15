/*
========================================================================================
    ADAPTER_CARDINALITY_PROBE — counts what an adapter actually emits
========================================================================================
    tests/test_adapter_contract.py is a STATIC check: it proves lib/AdapterContract.groovy
    declares a cardinality for every emit of every adapter, and that a `none` declaration
    is wired to Channel.empty(). What it cannot see is how many rows a RUNNING adapter
    puts on each channel — which is the whole point of the declaration.

    This probe runs one adapter on a group of known shape (one patient, one reference,
    two moving slides) and emits, per emit name, the number of rows observed next to the
    number AdapterContract's declaration allows. The .nf.test asserts they agree.

    WHY AN UPPER BOUND AND NOT AN EQUALITY. A cardinality here means "AT MOST one row per
    <unit>", because two of the seam's emits are legitimately optional: VALIS's
    intrinsic_tre (`REGISTER.out.summary`, `optional: true`, and not produced by the stub
    at all) and its stage_checkpoint (written only at reg_qc >= 2). "At most one per
    patient" is also exactly the property a consumer needs: seg_qc.nf's VALIS branch
    combines the transform by patient_id alone, and `combine(by:)` is a cross join, so a
    second row per patient does not produce a second pairing — it produces N**2 of them,
    each moving slide scored against every transform of its patient. The bound this probe
    checks is therefore the bound that makes the join safe, and it is checked in the
    direction that breaks: declaring per-patient what is really per-slide goes RED.

    NOT covered here: `versions` is declared PER_PROCESS — one row per process the
    adapter runs, independent of patients and slides — which a single fixture cannot
    distinguish from any other constant. The test asserts only that it is non-empty and
    says so out loud rather than implying more.
========================================================================================
*/

include { VALIS_ADAPTER } from '../../../../subworkflows/local/adapters/valis_adapter'
include { TILED_ADAPTER } from '../../../../subworkflows/local/adapters/tiled_adapter'

workflow ADAPTER_CARDINALITY_PROBE {
    take:
    ch_grouped   // [patient_id, ref_item, all_items]
    method       // String: which adapter to run
    shape        // [patients: n, slides: n, moving_slides: n] — the fixture's shape

    main:
    if (method == 'tiled') {
        TILED_ADAPTER(ch_grouped)
        adapter = TILED_ADAPTER.out
    } else {
        VALIS_ADAPTER(ch_grouped)
        adapter = VALIS_ADAPTER.out
    }

    // Named explicitly rather than reflected off `.out`, so this map is itself a
    // statement of the vocabulary — and the assert below fails if it drifts from
    // AdapterContract's. (`.out` is a ChannelOut; it has no by-name iteration.)
    def channels = [
        registered        : adapter.registered,
        transform         : adapter.transform,
        transform_by_slide: adapter.transform_by_slide,
        stage_checkpoint  : adapter.stage_checkpoint,
        intrinsic_tre     : adapter.intrinsic_tre,
        size_logs         : adapter.size_logs,
        versions          : adapter.versions,
    ]
    assert channels.keySet() == AdapterContract.EMITS.keySet() as Set

    def ch_counts = Channel.empty()
    channels.each { name, ch ->
        ch_counts = ch_counts.mix(
            ch.count().map { n ->
                tuple(name,
                      n as int,
                      AdapterContract.cardinalityOf(method, name),
                      AdapterContract.expectedCount(method, name, shape))
            }
        )
    }

    emit:
    // [emit_name, observed_rows, declared_cardinality, allowed_rows]
    // allowed_rows is -1 when the declaration is not a function of the group's shape.
    counts = ch_counts
}
