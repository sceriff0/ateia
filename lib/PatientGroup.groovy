/**
 * The one per-patient fan-in.
 *
 * Six places in this pipeline gathered a per-patient (or per-slide) group, and all
 * six wrote the same four-step ritual by hand:
 *
 *   1. wrap the key in `groupKey(id, size)`  — the STREAMING SIZE HINT
 *   2. `groupTuple()`                        — the gather
 *   3. `group_key.toString()`                — unwrap the GroupKey wrapper
 *   4. `[metas, files].transpose().toSorted{}` — restore a canonical order
 *
 * Each step has a failure mode that is silent, and each site was an independent
 * chance to get one of them wrong. All four are here now, once.
 *
 * ---------------------------------------------------------------------------
 * 1. THE SIZE IS MANDATORY
 * ---------------------------------------------------------------------------
 * `groupKey(id, n)` tells `groupTuple()` to close a group as soon as `n` items with
 * that key have arrived. Without it, `groupTuple()` cannot know a key is complete
 * until the ENTIRE upstream channel closes — so one patient's registration waits on
 * every other patient's preprocessing. That is a full-run barrier, and it is the
 * whole reason this pipeline pre-counts images and channels per patient at
 * samplesheet-read time (subworkflows/local/input_check.nf) and at every checkpoint
 * entry point (lib/Checkpoint.groovy).
 *
 * Five of the six sites wrote the hint as a ternary:
 *
 *     def key = meta.images_count ? groupKey(meta.patient_id, meta.images_count) : meta.patient_id
 *
 * whose else-branch is exactly the unsized barrier the hint exists to remove — taken
 * silently, on a run that still exits 0. `subworkflows/local/adapters/tiled_adapter.nf`
 * was the one site that refused instead (its `requireTilesCount`), and it is the
 * precedent this class generalises: there is no unsized branch here to fall into, so
 * the degradation is unwritable rather than merely discouraged.
 *
 * A size of 0, a negative size or a non-Integer is refused for the same reason
 * `Checkpoint.requireCount` refuses them: a groupKey sized 0 never fills, so the
 * failure mode is a HANG, which reads as "slow cluster", not as a bug.
 *
 * ---------------------------------------------------------------------------
 * 2. `remainder: true`, ALWAYS
 * ---------------------------------------------------------------------------
 * The size is a hint, and a hint can be wrong. An OVER-count means the group never
 * reaches its target: without `remainder: true` it is never emitted at all and the
 * patient silently vanishes from the run. With it, the group is emitted complete at
 * channel close — late, but whole. Applied here so no site can omit it.
 *
 * (An UNDER-count is not symmetric and is not fixed here: the group is emitted at
 * the too-small size AND again as the remainder, and what that does depends on the
 * consumer. Getting the count right is the producer's job; see
 * `CsvUtils.countChannelsPerPatient`.)
 *
 * ---------------------------------------------------------------------------
 * 3. THE GroupKey WRAPPER NEVER ESCAPES
 * ---------------------------------------------------------------------------
 * `groupTuple()` emits the `nextflow.extension.GroupKey` OBJECT as element 0, and
 * its `equals()` is ASYMMETRIC: `groupKey('P001',2).equals('P001')` is true, while
 * `'P001'.equals(groupKey('P001',2))` is false, and their hashCodes match. A later
 * `join(by:)`/`combine(by:)` against a plain-String key therefore matches in one
 * direction only, and WHICHEVER SIDE ARRIVES FIRST decides which — a ~1-run-in-2
 * silent drop, exit code 0. It is unwrapped here, in the operator immediately after
 * the gather, so it cannot travel. `tests/test_group_key_unwrapped.py` is the guard.
 *
 * ---------------------------------------------------------------------------
 * 4. CANONICAL ORDER, AND WHY NOT `groupTuple(sort:)`
 * ---------------------------------------------------------------------------
 * `groupTuple()` emits in ARRIVAL order and Nextflow hashes list inputs
 * POSITIONALLY, so an identical rerun produced a different task hash and `-resume`
 * missed and cascaded. (Measured: five identical stub runs cached 22, 8, 22, 22 and
 * 22 of 28 tasks.) It is also not only a caching concern — where a site reads
 * `metas[0]`, arrival order decided which slide's channels/is_reference travelled
 * downstream.
 *
 * `groupTuple(sort:)` is the fix a reader reaches for first and it is WRONG: it
 * orders each grouped list INDEPENDENTLY, so the metas and the payloads are sorted
 * by their own natural orders and silently RE-PAIRED —
 *
 *     in:  ['k', zeta, 'a.txt'], ['k', alpha, 'z.txt']
 *     groupTuple(sort: true) -> ['k', [alpha, zeta], ['a.txt', 'z.txt']]
 *                                       ^ alpha now describes a.txt — WRONG
 *
 * This class emits one list of `[meta, payload]` PAIRS rather than two parallel
 * lists, so there is no point downstream at which a meta and its payload are
 * separable and re-pairable. `pairAndSort` is the only place the two lists exist
 * side by side, and it pairs before it sorts.
 *
 * A sort key that TIES is refused: it leaves the tied items in arrival order, which
 * is the defect wearing the costume of a fix.
 *
 * ---------------------------------------------------------------------------
 * WHAT IS DELIBERATELY NOT HERE
 * ---------------------------------------------------------------------------
 * `subworkflows/local/add_cycle.nf`'s two `groupTuple(by: 0)` calls are UNSIZED ON
 * PURPOSE — one is a min-priority pick keyed on `[patient_id, marker]` (which has no
 * patient-level count to key on) and the other feeds an order-independent `.sum()`.
 * They are full-run barriers by design, not degraded sized groupings, and they do
 * not use this class. A barrier entry point here would only let a future site claim
 * this class's guarantees while keeping the barrier.
 *
 * All methods are static, per the repo's `lib/` convention.
 */
class PatientGroup {

    /** The options every grouping must name; anything else is a typo, not a default. */
    private static final List<String> REQUIRED_OPTS = ['name', 'size', 'sortBy']
    private static final List<String> KNOWN_OPTS    = ['name', 'size', 'sortBy', 'key']

    /**
     * Group `ch` — a channel of `[meta, payload]` — per patient, streaming.
     *
     * @param opts  name   String  the channel, as it should read in an error message
     *              size   String  the meta key holding this group's item count
     *              sortBy Closure (meta, payload) -> a comparable that TOTALLY orders
     *                             the group; ties are an error
     * @return  a channel of `[patient_id (String), [[meta, payload], ...]]`, the pairs
     *          in `sortBy` order.
     */
    static byPatient(Map opts, ch) {
        if (opts.containsKey('key'))
            throw new IllegalArgumentException(
                "PatientGroup.byPatient groups on meta.patient_id and takes no 'key' option " +
                "(channel '${opts.name}'). Use PatientGroup.byKey for any other grouping key.")
        return byKey(opts + [key: { meta -> meta.patient_id }], ch)
    }

    /**
     * As `byPatient`, but keyed by `opts.key` — for the groupings whose unit is not a
     * patient (the tiled adapter gathers control points per SLIDE).
     *
     * @param opts  as byPatient, plus
     *              key    Closure meta -> the grouping key
     */
    static byKey(Map opts, ch) {
        def unknown = opts.keySet().toList() - KNOWN_OPTS
        if (unknown)
            throw new IllegalArgumentException(
                "PatientGroup: unknown option(s) ${unknown} (known: ${KNOWN_OPTS}). An option " +
                "that is accepted and ignored is how a grouping silently loses its ordering.")
        def missing = REQUIRED_OPTS.findAll { !opts[it] } + (opts.key ? [] : ['key'])
        if (missing)
            throw new IllegalArgumentException(
                "PatientGroup: missing required option(s) ${missing} for channel '${opts.name}'. " +
                "None of them has a safe default: without 'size' the gather is a full-run " +
                "barrier, without 'sortBy' it is ordered by arrival.")

        String name    = opts.name
        String sizeKey = opts.size
        Closure keyOf  = opts.key
        Closure sortBy = opts.sortBy

        return ch
            .map { meta, payload ->
                def k = keyOf(meta)
                [nextflow.Nextflow.groupKey(k, requireSize(meta, sizeKey, name, k)), meta, payload]
            }
            .groupTuple(by: 0, remainder: true)
            .map { group_key, metas, payloads ->
                [group_key.toString(), pairAndSort(metas, payloads, sortBy)]
            }
    }

    /**
     * The streaming size for one item, or a named error.
     *
     * The error is the only context a reader gets — it is raised inside an operator
     * closure, with no task, no process and no work directory to inspect — so it names
     * the channel that refused, the meta key that was absent, and the group it was
     * absent for.
     */
    static int requireSize(Map meta, String sizeKey, String name, key) {
        def n = meta[sizeKey]
        if (!(n instanceof Integer) || n < 1)
            throw new IllegalStateException(
                "PatientGroup: channel '${name}' cannot be grouped — meta.${sizeKey} is " +
                "${n == null ? 'absent' : "${n}"} for group '${key}'. " +
                "The size is MANDATORY: it is the streaming hint that lets this group close " +
                "as soon as its own ${sizeKey} items arrive. Without it groupTuple() must wait " +
                "for the whole upstream channel to close, which turns every per-patient stage " +
                "into a full-run barrier on a run that still exits 0; sized 0 it never closes " +
                "at all and the run hangs. Fix the PRODUCER so it injects ${sizeKey} — " +
                "subworkflows/local/input_check.nf on the linear path, lib/Checkpoint.groovy " +
                "at a checkpoint entry point.")
        return n
    }

    /**
     * `metas` and `payloads` as one list of `[meta, payload]` pairs, in `sortBy` order.
     *
     * Pairs FIRST, then sorts the pairs. This is the whole reason `groupTuple(sort:)`
     * is banned repo-wide (`tests/test_resume_determinism.py`): sorting the two lists
     * independently re-pairs each meta with a different payload, silently.
     */
    static List pairAndSort(List metas, List payloads, Closure sortBy) {
        if (metas.size() != payloads.size())
            throw new IllegalStateException(
                "PatientGroup: ${metas.size()} meta(s) but ${payloads.size()} payload(s) in one " +
                "group. transpose() would TRUNCATE to the shorter list and drop the rest without " +
                "a word, so the mismatch is refused here instead.")
        def pairs = [metas, payloads].transpose().toSorted { pair -> sortBy(pair[0], pair[1]) }
        def keys = pairs.collect { pair -> sortBy(pair[0], pair[1]) }
        def tied = keys.countBy { it }.findAll { _k, n -> n > 1 }.keySet()
        if (tied)
            throw new IllegalStateException(
                "PatientGroup: sort key(s) ${tied.toList()} appear more than once in one group, so " +
                "the tied items keep their ARRIVAL order — which is the non-determinism sorting " +
                "is here to remove. Sort on something that separates them.")
        return pairs
    }
}
