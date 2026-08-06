/*
 * MarkerUtils - the pipeline's one nuclear/fiducial marker rule, for the Groovy
 * and Nextflow layer.
 *
 * `params.nuclear_markers` (nextflow.config) is the declared source of truth for
 * "which channel is nuclear". Before this class existed, only CONVERT_IMAGE and the
 * Python layer honoured it while three Groovy/NF sites hardcoded the literal 'DAPI':
 * SPLIT_CHANNELS' stub, CsvUtils.validateMetadata, and (implicitly, by unioning every
 * declared channel with no reference awareness) CsvUtils.countChannelsPerPatient.
 * Those three now come through here, and so do SEGMENT's backend guards and the
 * `--nuclear-markers` list SPLIT_CHANNELS hands to bin/split_multichannel.py.
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
     * Accepts BOTH shapes `params.nuclear_markers` can arrive in: the List declared in
     * nextflow.config, and the bare String Nextflow produces for a command-line
     * `--nuclear_markers CELLTOX` (or `--nuclear_markers DAPI,CELLTOX`). Without that
     * coercion a String would be iterated CHARACTER by character and every channel
     * containing one of its letters would be classified nuclear — 'PANCK' would match
     * the 'C' of 'CELLTOX'.
     *
     * Throws rather than assuming a default, so a missing/empty params.nuclear_markers
     * is loud instead of silently classifying every channel as non-nuclear.
     */
    static List<String> markerList(def nuclearMarkers) {
        def raw
        if (nuclearMarkers == null)                     raw = []
        else if (nuclearMarkers instanceof CharSequence) raw = nuclearMarkers.toString().split(/[,\s]+/) as List
        else if (nuclearMarkers instanceof Object[])     raw = nuclearMarkers as List
        else if (nuclearMarkers instanceof Collection)   raw = nuclearMarkers as List
        else                                             raw = [nuclearMarkers]

        def markers = raw
            .collect { it?.toString()?.trim()?.toUpperCase() }
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

    /**
     * The channels SPLIT_CHANNELS emits for ONE slide.
     *
     * The nuclear channel is identical across cycles, so it is kept only on the
     * reference slide; keeping it on every moving slide would quantify and merge the
     * same marker once per slide. bin/split_multichannel.py applies the same rule at
     * runtime (`--is-reference`), and SPLIT_CHANNELS' stub and
     * CsvUtils.countChannelsPerPatient now derive theirs from this method, so the
     * group sizes the workflow computes ahead of time match what actually arrives.
     */
    static List splitOutputChannels(List channels, boolean isReference, def nuclearMarkers) {
        if (!channels) return []
        if (isReference) return channels.collect { it }
        return channels.findAll { !isNuclear(it, nuclearMarkers) }
    }
}
