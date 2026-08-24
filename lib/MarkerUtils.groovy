/*
 * MarkerUtils - the pipeline's one nuclear/fiducial marker rule, for the Groovy
 * and Nextflow layer.
 *
 * `params.nuclear_markers` (nextflow.config) is the declared source of truth for
 * "which channel is nuclear". Before this class existed, only CONVERT_IMAGE and the
 * Python layer honoured it while three Groovy/NF sites hardcoded the literal 'DAPI':
 * SPLIT_CHANNELS' stub, CsvUtils.validateMetadata, and (implicitly, by unioning every
 * declared channel with no reference awareness) CsvUtils.countChannelsPerPatient.
 * Those three now come through here, and so do SEGMENT's backend guards.
 *
 * This class answers "is this channel nuclear?" and nothing more. It does NOT decide
 * which channels a slide emits -- that is CsvUtils.resolveKeptChannelsPerSlide, which
 * needs patient-wide context (what a sibling slide already claimed) that a
 * marker-identity helper cannot have. splitOutputChannels used to live here and was
 * retired for exactly that reason.
 *
 * THE MARKER LIST IS ALWAYS AN ARGUMENT. This class must never define a default for
 * it: `nextflow.config` is the only place a parameter default may live
 * (tests/test_no_duplicate_param_defaults.py). The Python side already carries the
 * one permitted mirror at bin/utils/metadata.py's DEFAULT_NUCLEAR_MARKERS — do not
 * add a third.
 *
 * Matching is case-insensitive SUBSTRING, deliberately: it mirrors
 * bin/utils/metadata.py's is_nuclear/pick_nuclear_index (which bin/split_multichannel.py
 * now calls instead of testing `"DAPI" in name.upper()` itself), so a channel
 * CONVERT_IMAGE already treated as nuclear (e.g. 'DAPI_nuclear') is treated as nuclear
 * here too.
 */
class MarkerUtils {

    /**
     * The configured markers, trimmed and upper-cased.
     *
     * THIS IS THE ONLY WAY TO READ params.nuclear_markers. Every consumer goes through
     * it (tests/test_nuclear_marker_routing.py enforces that mechanically), because the
     * parameter arrives in three different shapes and two of them break silently:
     *
     *   ['DAPI','CELLTOX']  nextflow.config / a params file      - the intended shape
     *   'CELLTOX'           `--nuclear_markers CELLTOX` on the CLI (nextflow_schema.json
     *                       permits a string; an array cannot be given on a command line)
     *   ['DAPI,CELLTOX']    a params file with the list written as one comma string
     *
     * The bare String, iterated as a List, yields the CHARACTERS C,E,L,L,T,O,X, so
     * 'PANCK' matches on 'C' and is classified nuclear. The one-element comma string is
     * worse: it is a valid Collection, so nothing coerces it, and the single marker
     * 'DAPI,CELLTOX' matches NO channel -- every channel is silently non-nuclear, the
     * nuclear channel is never dropped from moving slides, and the pre-computed group
     * sizes are wrong with no error anywhere. Splitting EVERY element on commas and
     * whitespace closes both.
     *
     * Throws rather than assuming a default, so a missing/empty params.nuclear_markers
     * is loud instead of silently classifying every channel as non-nuclear.
     */
    static List<String> markerList(def nuclearMarkers) {
        def raw
        if (nuclearMarkers == null)                      raw = []
        else if (nuclearMarkers instanceof CharSequence) raw = [nuclearMarkers]
        else if (nuclearMarkers instanceof Object[])     raw = nuclearMarkers as List
        else if (nuclearMarkers instanceof Collection)   raw = nuclearMarkers as List
        else                                             raw = [nuclearMarkers]

        def markers = raw
            .collectMany { it == null ? [] : it.toString().split(/[,\s]+/) as List }
            .collect { it?.trim()?.toUpperCase() }
            .findAll { it }
        if (!markers)
            throw new IllegalArgumentException(
                "No nuclear markers configured. Pass params.nuclear_markers; its default " +
                "lives in nextflow.config and nowhere else.")
        return markers
    }

    /** True when `channel` names one of the configured nuclear/fiducial markers. */
    static boolean isNuclear(def channel, def nuclearMarkers) {
        if (channel == null) return false
        def name = channel.toString().trim().toUpperCase()
        if (!name) return false
        return markerList(nuclearMarkers).any { name.contains(it) }
    }

    /** True when at least one of `channels` is a nuclear/fiducial marker. */
    static boolean hasNuclear(List channels, def nuclearMarkers) {
        return (channels ?: []).any { isNuclear(it, nuclearMarkers) }
    }

    /**
     * Index of the nuclear channel in `channels`, or -1 when there is none.
     *
     * Marker PREFERENCE order wins over channel order, matching
     * bin/utils/metadata.py's pick_nuclear_index: with markers ['DAPI','CELLTOX'] and
     * channels ['CELLTOX','CD8','DAPI'] the answer is 2, not 0. SEGMENT's CellSAM
     * backend feeds this straight to `--dapi-channel`, so a disagreement with the
     * Python resolver would segment the wrong channel rather than fail.
     */
    static int nuclearIndex(List channels, def nuclearMarkers) {
        def names = (channels ?: []).collect { it?.toString()?.trim()?.toUpperCase() ?: '' }
        for (marker in markerList(nuclearMarkers)) {
            def hit = names.findIndexOf { it && it.contains(marker) }
            if (hit >= 0) return hit
        }
        return -1
    }

}
