/**
 * The one owner of "a patient's artifacts".
 *
 * Nothing in this pipeline owned that concept, so every consumer re-derived it. The
 * cost was not verbosity; it was three distinct silent-data-loss shapes, all of which
 * were live:
 *
 * ---------------------------------------------------------------------------
 * 1. EVERY PER-PATIENT JOIN WAS LOSSY
 * ---------------------------------------------------------------------------
 * `failOnMismatch` and `failOnDuplicate` appeared NOWHERE in this repository. Not one
 * join in the pipeline failed when a patient went missing. That matters here more than
 * it would elsewhere, because `conf/modules.config`'s `errorStrategy` has an `'ignore'`
 * branch: a task that fails the wrong way is DROPPED and the run keeps going. One
 * ignored task on patient 7 removed patient 7 from the five-join checkpoint chain
 * silently, and the run exited 0 having written `csv/postprocessed.csv` with 11 rows
 * where 12 patients went in. The short CSV is then read back by the next `--start` and
 * the patient is simply gone -- half-published, and invisible.
 *
 * `subworkflows/local/input_check.nf` guards exactly this hazard at the samplesheet
 * seam, erroring loudly on a shortfall rather than logging a warning. The discipline
 * stopped there. It continues here: every required field is checked per patient, and a
 * shortfall aborts naming the seam, the field and the patient.
 *
 * ---------------------------------------------------------------------------
 * 2. THE JOINS WERE POSITIONAL
 * ---------------------------------------------------------------------------
 * `subworkflows/local/postprocess.nf`'s chain produced a 6-tuple that was destructured
 * about forty lines further down. `cell_csv` and `cell_geojson` are BOTH
 * `Layout.publishedPath(..., 'geojson', ...)` of the same patient, so swapping the two
 * `.join()` clauses swapped the two checkpoint columns -- and `Checkpoint.row`
 * validates key PRESENCE, not which file landed under which key, so the checkpoint
 * recorded the wrong file under the wrong column and the run stayed green.
 *
 * Here the producer is bound to the field BY NAME at declaration, and read back BY
 * NAME at use. There is no position anywhere in between for two files to trade places
 * in. `tests/patient_artifacts_probe.nf`'s `transposed` case executes the swap so the
 * assertions that catch it are themselves watched failing.
 *
 * ---------------------------------------------------------------------------
 * 3. TWO KEYING CONVENTIONS, AND AN "OPTIONAL" THAT WAS AN EMPTY CHANNEL
 * ---------------------------------------------------------------------------
 * SEGMENTATION emitted `cell_mask`/`nuclei_mask`/`morphology` as `[meta, file]` and
 * `contours`/`nucleus_contours` as `[patient_id, file]`, so every consumer had to know
 * which convention each emit followed and re-key half of them by hand. `fields` accepts
 * either shape and normalises it; `metaFrom` names the one field that must carry the
 * meta.
 *
 * And `nucleus_contours` was `Channel.empty()` when `--quantify_compartments` was off.
 * An empty channel joined plainly empties the WHOLE result -- every patient's export
 * gone, run exits 0 -- so each caller carried its own ternary swapping in the cell
 * contours as a placeholder, and one caller additionally carried a four-slot
 * placeholder tuple whose arity had to be kept in step with the real one by hand. Both
 * are declarations now:
 *
 *   `when:`      a RUN-LEVEL gate. False means the producer never ran for anybody, so
 *                the field is not joined at all and takes `orElse` / `orElseField`.
 *                True means it ran for everybody, and the field is REQUIRED -- a
 *                patient missing from a gated-on field is still an error, which a
 *                presence-based fallback could not tell from a dropped task.
 *   `optional:`  genuinely PER-PATIENT optional (a single-slide patient produces no
 *                registration QC at all). Missing is a value, `orElse`, not a dropped
 *                patient.
 *
 * ---------------------------------------------------------------------------
 * WHAT THIS IS NOT
 * ---------------------------------------------------------------------------
 * Not a gather. `lib/PatientGroup.groovy` owns the per-patient fan-IN of many items
 * into one group (and owns the streaming size hint that makes it non-blocking). This
 * class owns the 1:1 fan-in of one item per patient from several channels. A field
 * whose channel is itself a PatientGroup result is joined here as an ordinary value.
 *
 * All methods are static, per the repo's `lib/` convention.
 */
class PatientArtifacts {

    /** Options `bundle` understands; anything else is a typo, not a default. */
    private static final List<String> KNOWN_OPTS  = ['name', 'metaFrom', 'fields']
    /** Per-field spec keys. */
    private static final List<String> KNOWN_FIELD = ['channel', 'optional', 'when', 'orElse', 'orElseField']

    /**
     * Join one item per patient from each declared field into ONE named-field bundle.
     *
     * @param opts  name     String  the seam, as it should read in an error message
     *              metaFrom String  which field's channel carries `[meta, payload]`;
     *                               that meta becomes the bundle's `meta`, and that
     *                               field's patient set is the ROSTER every other
     *                               required field is checked against
     *              fields   Map     field name -> a channel, or a spec map:
     *                                 channel     Channel  required
     *                                 optional    boolean  per-patient optional
     *                                 when        boolean  run-level gate
     *                                 orElse      Object   value when absent
     *                                 orElseField String   sibling field to copy when
     *                                                      absent (declared EARLIER)
     * @return  a channel of `Map`s carrying `patient_id`, `meta`, and one entry per
     *          declared field. Read them by name; never destructure them positionally.
     */
    static bundle(Map opts) {
        def unknown = opts.keySet().toList() - KNOWN_OPTS
        if (unknown)
            throw new IllegalArgumentException(
                "PatientArtifacts: unknown option(s) ${unknown} (known: ${KNOWN_OPTS}). An option " +
                "that is accepted and ignored is how a bundle silently loses a field.")
        ['name', 'metaFrom', 'fields'].each { k ->
            if (!opts[k])
                throw new IllegalArgumentException(
                    "PatientArtifacts: missing required option '${k}'. None of them has a safe " +
                    "default: without 'name' a failure cannot say which seam refused, without " +
                    "'metaFrom' there is no meta and no roster to check the other fields against.")
        }

        String name     = opts.name
        String metaFrom = opts.metaFrom
        Map fields      = opts.fields

        if (!fields.containsKey(metaFrom))
            throw new IllegalArgumentException(
                "PatientArtifacts: seam '${name}' names metaFrom='${metaFrom}', which is not one of " +
                "its fields ${fields.keySet().toList()}.")

        Map metaSpec = specOf(name, metaFrom, fields[metaFrom])
        if (metaSpec.optional || metaSpec.containsKey('when'))
            throw new IllegalArgumentException(
                "PatientArtifacts: seam '${name}' declares its metaFrom field '${metaFrom}' as " +
                "optional or gated. The roster field defines which patients MUST be present, so it " +
                "cannot itself be allowed to be absent -- pick a field that always runs.")

        // The roster. Its meta is the bundle's meta, and its patient set is what every
        // required field is checked against.
        def acc = metaSpec.channel.map { row ->
            def (key, payload) = unpack(name, metaFrom, row)
            if (!(key instanceof Map))
                throw new IllegalStateException(
                    "PatientArtifacts: seam '${name}' reads its meta from field '${metaFrom}', but " +
                    "that channel is keyed by '${key}' rather than by a meta map. metaFrom must name " +
                    "a field carrying the [meta, payload] shape.")
            [key.patient_id, [patient_id: key.patient_id, meta: key, (metaFrom): payload]]
        }

        fields.each { String fname, spec ->
            if (fname == metaFrom) return
            Map f = specOf(name, fname, spec)

            // A run-level gate that is OFF: the producer never ran for anybody, so there
            // is nothing to join. Bind the declared fallback and move on.
            if (f.containsKey('when') && !f.when) {
                acc = acc.map { pid, b -> [pid, b + [(fname): absentValue(name, fname, f, b)]] }
                return
            }

            def keyed = f.channel.map { row ->
                def (key, payload) = unpack(name, fname, row)
                [key instanceof Map ? key.patient_id : key, payload]
            }

            // `remainder: true` on every field, required or not -- it is what makes the
            // shortfall VISIBLE. A plain join drops the unmatched key without a word;
            // remainder emits it padded with a null, which the closure below turns into
            // an abort for a required field and into `orElse` for an optional one.
            // `failOnDuplicate` is the other half: two items for one patient on one
            // field is a fan-out nobody asked for, and it would double the patient's
            // checkpoint rows.
            acc = acc
                .join(keyed, by: 0, remainder: true, failOnDuplicate: true)
                .map { pid, b, payload ->
                    if (b == null)
                        throw new IllegalStateException(
                            "PatientArtifacts: seam '${name}' -- field '${fname}' has an entry for " +
                            "patient '${pid}', which is not in the roster (field '${metaFrom}'). One " +
                            "of the two producers is wrong: either '${metaFrom}' lost a patient, or " +
                            "'${fname}' emitted one twice under different metas. Both are silent " +
                            "under a plain join, which is why this refuses instead.")
                    if (payload == null) {
                        if (f.optional) return [pid, b + [(fname): absentValue(name, fname, f, b)]]
                        throw new IllegalStateException(
                            "PatientArtifacts: seam '${name}' -- no '${fname}' for patient '${pid}'. " +
                            "The patient reached the roster (field '${metaFrom}') but its '${fname}' " +
                            "never arrived, so it would be DROPPED from this seam and from every " +
                            "checkpoint row downstream of it, on a run that still exits 0. The usual " +
                            "cause is conf/modules.config's errorStrategy 'ignore' branch swallowing " +
                            "the task that should have produced it -- check .nextflow.log for a " +
                            "failed task on patient '${pid}'. If '${fname}' is genuinely absent for " +
                            "some patients, declare it `optional: true` with an `orElse`.")
                    }
                    [pid, b + [(fname): payload]]
                }
        }

        return acc.map { _pid, b -> b }
    }

    /** A field declaration, normalised, with its keys checked. */
    private static Map specOf(String name, String fname, spec) {
        if (spec == null)
            throw new IllegalArgumentException(
                "PatientArtifacts: seam '${name}' declares field '${fname}' as null.")
        if (!(spec instanceof Map)) return [channel: spec, optional: false]

        def unknown = spec.keySet().toList() - KNOWN_FIELD
        if (unknown)
            throw new IllegalArgumentException(
                "PatientArtifacts: seam '${name}', field '${fname}': unknown key(s) ${unknown} " +
                "(known: ${KNOWN_FIELD}).")
        if (!spec.containsKey('channel'))
            throw new IllegalArgumentException(
                "PatientArtifacts: seam '${name}', field '${fname}' has no 'channel'.")
        if (spec.optional && spec.containsKey('when'))
            throw new IllegalArgumentException(
                "PatientArtifacts: seam '${name}', field '${fname}' declares BOTH 'optional' and " +
                "'when'. They are different things: 'when' is a run-level gate (the producer ran " +
                "for everybody or for nobody), 'optional' is per-patient (it ran for some). " +
                "Declaring both hides which one is meant.")
        if (spec.containsKey('orElse') && spec.containsKey('orElseField'))
            throw new IllegalArgumentException(
                "PatientArtifacts: seam '${name}', field '${fname}' declares both 'orElse' and " +
                "'orElseField'.")
        return [channel: spec.channel, optional: spec.optional ?: false] +
               (spec.containsKey('when') ? [when: spec.when] : [:]) +
               (spec.containsKey('orElse') ? [orElse: spec.orElse] : [:]) +
               (spec.orElseField ? [orElseField: spec.orElseField] : [:])
    }

    /** The value a gated-off or per-patient-absent field takes. */
    private static absentValue(String name, String fname, Map f, Map bundleSoFar) {
        if (f.orElseField) {
            if (!bundleSoFar.containsKey(f.orElseField))
                throw new IllegalStateException(
                    "PatientArtifacts: seam '${name}', field '${fname}' falls back to " +
                    "'${f.orElseField}', which is not bound yet. A fallback may only name a field " +
                    "declared EARLIER in the same `fields` map -- otherwise the fallback silently " +
                    "resolves to null.")
            return bundleSoFar[f.orElseField]
        }
        return f.containsKey('orElse') ? f.orElse : null
    }

    /**
     * One channel item as `[key, payload]`, whichever keying convention it arrived in.
     *
     * This is the whole of requirement 3: `[meta, file]` and `[patient_id, file]` both
     * go in, and no caller has to remember which emit follows which convention.
     */
    private static List unpack(String name, String fname, row) {
        def items = row instanceof List ? row : [row]
        if (items.size() != 2)
            throw new IllegalStateException(
                "PatientArtifacts: seam '${name}', field '${fname}' emitted a ${items.size()}-item " +
                "tuple; a field must be [meta, payload] or [patient_id, payload]. Join the extra " +
                "elements in as their own named fields rather than widening one.")
        return [items[0], items[1]]
    }
}
