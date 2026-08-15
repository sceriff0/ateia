/*
 * PanelSignature - a slide's identity WITHIN a patient: its channel set.
 *
 * WHY THIS IS A THING AT ALL. Nothing in this pipeline identifies a slide by its
 * filename -- channel identity comes from declared metadata, never from a name (see
 * bin/register.py's note on the marker-based fallback). What is left, once a patient's
 * slides are grouped, is the set of markers each one carries. VALIS's adapter matches
 * registered OUTPUT files back to their input metadata by reading the OME channel names
 * out of each file and looking them up by that set; SEG_QC's per-slide arm used the same
 * set as a join key. Both were computing it inline, with different separators, and only
 * one of them had an opinion about what a repeat means.
 *
 * WHAT A REPEATED PANEL DID, BEFORE. Two slides of one patient declaring the same
 * markers -- a repeat acquisition, or a QC re-image, which is ordinary in a multi-cycle
 * study -- hard-FAILED under VALIS (the adapter's own bespoke throw, raised only AFTER
 * REGISTER had already spent the compute) and passed SILENTLY under tiled, where
 * SEG_QC's `combine(by: [patient, signature])` is a CROSS JOIN and produced N x N warp
 * tasks: every slide scored against every other slide's transform, writing identical
 * output filenames, with no error anywhere. One input, two outcomes, neither documented.
 *
 * WHY THE ANSWER IS "REFUSE", ON BOTH PATHS, AND WHY THAT IS NOT HOSTILE TO THE SCIENCE.
 * A repeat acquisition is a legitimate thing to have. It is not a legitimate thing to
 * DECLARE with the same marker labels, because past registration this pipeline's entire
 * output model is keyed by MARKER NAME and not by slide:
 *
 *   SPLIT_CHANNELS      writes one TIFF per channel, named after the channel
 *   QUANTIFY_MARKERS    derives `id` and `channel_name` from that filename
 *   MERGE_QUANT_CSVS    joins the per-marker CSVs into one row per cell
 *   EXPORT_GEOJSON      emits "<marker>: <compartment>: <statistic>" measurement keys
 *
 * so two slides declaring `DAPI|CD3` for one patient produce two files called `CD3.tiff`,
 * two quantifications with the same `id`, and one merged column. The second silently
 * overwrites the first. Accepting the duplicate at registration does not make the repeat
 * work; it moves the failure to a quieter place, past the expensive step, into a number
 * someone will publish. Refusing at the boundary -- before either adapter runs -- costs
 * seconds and says what to do instead: give the repeat distinct marker labels (cycle 2's
 * CD3 is a different measurement from cycle 1's, and naming it `CD3_r2` is what makes the
 * two comparable rather than collapsed), or run it under its own patient_id.
 *
 * All static; see CLAUDE.md's lib/ convention.
 */
class PanelSignature {

    /**
     * The canonical signature of a channel list: order-independent, case-insensitive.
     *
     * toSorted(), NEVER sort(). `meta + [k: v]` is Groovy's cloneSimilarMap-then-putAll,
     * so every meta derived from another SHARES the same `channels` List reference. An
     * in-place sort here would reorder that list for every other holder of it --
     * including the metas that end up in csv/registered.csv's `channels` column, which
     * records the author's declared order. tests/lib_probe.nf pins the non-mutation.
     */
    static String ofChannels(def channels) {
        def list = (channels ?: []) as List
        return list.collect { it?.toString()?.trim()?.toLowerCase() }.toSorted().join('_')
    }

    /** The signature of a slide, from its meta's `channels`. See {@link #ofChannels}. */
    static String of(Map meta) {
        return ofChannels(meta?.channels)
    }

    /**
     * Refuse a patient whose slides do not have distinct panels.
     *
     * Called once, on the grouped channel BOTH adapters take
     * (subworkflows/local/register_patient.nf), so the outcome cannot depend on
     * --registration_method and a third backend inherits it without doing anything.
     * VALIS_ADAPTER calls it a second time at its demux, where an ambiguous signature is
     * what would actually corrupt the meta-to-file matching -- same owner, same message,
     * no second implementation.
     */
    static void requireUniqueWithinPatient(String patientId, List metas) {
        def bySignature = (metas ?: []).groupBy { of(it) }
        def duplicated = bySignature.findAll { _sig, group -> group.size() > 1 }
        if (!duplicated) return

        def detail = duplicated.collect { sig, group ->
            def ids = group.collect { it?.id ?: '<no id>' }.join(', ')
            "  ${sig}  <- ${group.size()} slides: ${ids}"
        }.join('\n')

        throw new IllegalArgumentException("""\
            Patient '${patientId}' declares the same channel set on more than one slide.

            ${detail.trim()}

            Within a patient, a slide's identity IS its channel set: everything past
            registration is keyed by MARKER NAME, not by slide. SPLIT_CHANNELS names each
            output after its channel, QUANTIFY_MARKERS takes its id from that filename,
            and MERGE_QUANT_CSVS joins per marker -- so two slides declaring the same
            markers collide there, with the second overwriting the first.

            A repeat or QC re-image of the same panel is a legitimate acquisition. Declare
            it with distinct marker labels (e.g. 'CD3_r2' for the second measurement of
            CD3), which is also what keeps the two comparable, or give it its own
            patient_id.
            """.stripIndent())
    }
}
