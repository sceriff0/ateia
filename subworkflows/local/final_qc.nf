/*
================================================================================
    SUBWORKFLOW: FINAL_QC
================================================================================
    The pipeline's end-of-run aggregation, shared by the linear path and by
    add_cycle. Both used to carry their own copy of it — the add_cycle copy was
    introduced with the comment "Mirrors the standard-path aggregation below",
    and "mirrors" is exactly the property that rots.

    It owns two things, both gated by their own param so the router needs no `if`:
      * the HTML QC report          (skipped by --skip_final_qc_report)
      * the input-size log rollup   (only under --enable_trace)

    ---------------------------------------------------------------------------
    Interface
    ---------------------------------------------------------------------------
    Artifacts arrive as ONE stream of `[kind, file]` pairs rather than as seven
    positional channels. A caller mixes in what it has; a kind nobody contributes
    simply yields an empty branch here, which is what removes the
    `Channel.empty().collect().ifEmpty([])` placeholders both callers used to
    repeat. (add_cycle contributes no preprocess_qc / valis_summary /
    postprocess_qc: it calls PREPROCESSING internally without re-exposing its QC
    pngs, and it has no POSTPROCESSING step at all — masks are reused, not
    re-segmented.)

    Recognised kinds:
      preprocess_qc | registration_qc | valis_summary | postprocess_qc | seg_qc
        -> staged into the matching GENERATE_QC_REPORT input directory
      versions   -> deduplicated and collated into collated_versions.yml
      size_log   -> merged into raw_input_sizes.csv for AGGREGATE_SIZE_LOGS

    An unrecognised kind is a hard ERROR (see KNOWN_ARTIFACT_KINDS below). Every
    consumer here is an `artifactsOf(...)` filter, so an unknown tag would match no
    filter and that whole QC category would vanish from the report while the run
    stayed green — under the previous positional signature the same typo was a
    parse-time resolution failure, and this guard is what keeps it loud.

    `ch_run_facts` carries the only run-summary inputs that genuinely differ
    between the two callers — the `stop` label and the sample manifest totals.
    Everything else in run_summary.json (pipeline name/version, timestamp,
    params.mode/start, the params block) is derived here, so the two paths cannot
    drift apart in a field neither of them meant to change. Nothing in this file
    branches on params.mode.
================================================================================
*/

import groovy.json.JsonOutput

include { AGGREGATE_SIZE_LOGS } from '../../modules/local/aggregate_size_logs'
include { GENERATE_QC_REPORT  } from '../../modules/local/generate_qc_report'

// The complete artifact vocabulary. Both call sites hand-write these tags (13
// literals across workflows/mirage.nf's two FINAL_QC calls) and nothing else
// checks that the two vocabularies agree — so this list is the check.
KNOWN_ARTIFACT_KINDS = [
    'preprocess_qc',
    'registration_qc',
    'valis_summary',
    'postprocess_qc',
    'seg_qc',
    'versions',
    'size_log',
]

// Pull one kind out of the tagged artifact stream. Nextflow channels are
// broadcast, so applying this repeatedly to the same source is fine.
def artifactsOf(ch_artifacts, String kind) {
    return ch_artifacts.filter { it[0] == kind }.map { it[1] }
}

workflow FINAL_QC {
    take:
    ch_artifacts   // [kind, file]  — see the kind list above
    ch_run_facts   // value channel of [stop: String, patients: Map, channels: Map]
                   //   patients/channels are INPUT_CHECK.out.counts

    main:

    // Fail-fast vocabulary guard. Registered unconditionally (not inside either
    // param gate) so a typo is caught even on a run that asks for neither output.
    ch_artifacts.subscribe { item ->
        if (!(item[0] in KNOWN_ARTIFACT_KINDS)) {
            error "FINAL_QC: unknown artifact kind '${item[0]}' for ${item[1]}. " +
                  "Known kinds: ${KNOWN_ARTIFACT_KINDS.join(', ')}. An unknown tag matches no " +
                  "artifactsOf() filter, so that whole QC category would be dropped from the " +
                  "report without failing the run — fix the tag at the FINAL_QC call site in " +
                  "workflows/mirage.nf, or add the kind here and wire it to a report slot."
        }
    }

    if (!params.skip_final_qc_report) {
        // Run-context summary for the report's overview card (pipeline, run,
        // params, sample manifest). Built from the counts INPUT_CHECK already
        // computed rather than by re-parsing the samplesheet.
        ch_run_summary = ch_run_facts
            .map { facts ->
                def patient_counts = facts.patients
                def channel_counts = facts.channels
                def summary = [
                    pipeline: [name: workflow.manifest.name, version: workflow.manifest.version],
                    run: [
                        timestamp: new java.text.SimpleDateFormat("yyyy-MM-dd HH:mm:ss 'UTC'")
                            .format(new Date()),
                        mode: params.mode,
                        start: params.start,
                        stop: facts.stop,
                    ],
                    params: [
                        registration_method: params.registration_method,
                        seg_method: params.seg_method,
                        quantify_compartments: params.quantify_compartments,
                        expanded_quantification: params.expanded_quantification,
                        pixel_size: params.pixel_size,
                    ],
                    manifest: [
                        totals: [
                            patients: patient_counts.size(),
                            images: (patient_counts.values().sum() ?: 0),
                            channels: (channel_counts.values().sum() ?: 0),
                        ],
                        patients: patient_counts.collectEntries { pid, imgs ->
                            [(pid): [images: imgs, channels: (channel_counts[pid] ?: 0)]]
                        },
                    ],
                ]
                return JsonOutput.prettyPrint(JsonOutput.toJson(summary))
            }
            .collectFile(name: 'run_summary.json')

        // GENERATE_QC_REPORT's input arity and order are fixed (nf-test snapshots
        // pin them) — this call must keep all seven slots in this order.
        GENERATE_QC_REPORT(
            artifactsOf(ch_artifacts, 'preprocess_qc').collect().ifEmpty([]),
            artifactsOf(ch_artifacts, 'registration_qc').collect().ifEmpty([]),
            artifactsOf(ch_artifacts, 'valis_summary').collect().ifEmpty([]),
            artifactsOf(ch_artifacts, 'postprocess_qc').collect().ifEmpty([]),
            artifactsOf(ch_artifacts, 'versions').unique().collectFile(name: 'collated_versions.yml'),
            ch_run_summary,
            artifactsOf(ch_artifacts, 'seg_qc').collect().ifEmpty([]),
        )
    }

    if (params.enable_trace) {
        // Merge by content (not by staging many same-named files) so AGGREGATE
        // receives a single file and cannot hit a work-dir name collision —
        // several processes emit identically-named *.size.csv logs.
        AGGREGATE_SIZE_LOGS(
            artifactsOf(ch_artifacts, 'size_log').collectFile(name: 'raw_input_sizes.csv', sort: true)
        )
    }

    // Neither GENERATE_QC_REPORT.out.versions nor AGGREGATE_SIZE_LOGS.out.versions is
    // consumed here, and neither was consumed by the router before this subworkflow
    // existed: both processes run AFTER the version collation they would belong to.
    // Left as-is to keep this extraction behaviour-preserving.
    //
    // The two are NOT equally invisible, though. GENERATE_QC_REPORT's publishDir
    // (conf/modules.config:139-145) drops versions.yml via saveAs; AGGREGATE_SIZE_LOGS'
    // (conf/modules.config:479-497) does NOT, so <outdir>/size_logs/versions.yml is a
    // published artifact whose single key is the fully-qualified process name — moving
    // this call in here changed it to "MIRAGE:FINAL_QC:AGGREGATE_SIZE_LOGS". See the
    // comment on that publishDir block.
}
