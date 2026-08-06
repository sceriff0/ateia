/*
 * MarkerUtils - the pipeline's one nuclear/fiducial marker rule, for the Groovy
 * and Nextflow layer.
 *
 * `params.nuclear_markers` (nextflow.config) is the declared source of truth for
 * "which channel is nuclear". Before this class existed, only CONVERT_IMAGE and the
 * Python layer honoured it while three Groovy/NF sites hardcoded the literal 'DAPI':
 * SPLIT_CHANNELS' stub, CsvUtils.validateMetadata, and (implicitly, by unioning every
 * declared channel with no reference awareness) CsvUtils.countChannelsPerPatient.
 * Those three now come through here.
 *
 * THE MARKER LIST IS ALWAYS AN ARGUMENT. This class must never define a default for
 * it: `nextflow.config` is the only place a parameter default may live
 * (tests/test_no_duplicate_param_defaults.py). The Python side already carries the
 * one permitted mirror at bin/utils/metadata.py's DEFAULT_NUCLEAR_MARKERS — do not
 * add a third.
 *
 * Matching is case-insensitive SUBSTRING, deliberately: it mirrors
 * bin/utils/metadata.py's pick_nuclear_index and bin/split_multichannel.py's
 * `"DAPI" in name.upper()`, so a channel CONVERT_IMAGE already treated as nuclear
 * (e.g. 'DAPI_nuclear') is treated as nuclear here too.
 */
class MarkerUtils {

    /**
     * Normalise the configured marker list. Throws rather than assuming a default,
     * so a missing/empty params.nuclear_markers is loud instead of silently
     * classifying every channel as non-nuclear.
     */
    private static List<String> normaliseMarkers(List nuclearMarkers) {
        def markers = (nuclearMarkers ?: [])
            .collect { it?.toString()?.trim()?.toUpperCase() }
            .findAll { it }
        if (!markers)
            throw new IllegalArgumentException(
                "No nuclear markers configured. Pass params.nuclear_markers; its default " +
                "lives in nextflow.config and nowhere else.")
        return markers
    }

    /** True when `channel` names one of the configured nuclear/fiducial markers. */
    static boolean isNuclear(def channel, List nuclearMarkers) {
        if (channel == null) return false
        def name = channel.toString().trim().toUpperCase()
        if (!name) return false
        return normaliseMarkers(nuclearMarkers).any { name.contains(it) }
    }

    /** True when at least one of `channels` is a nuclear/fiducial marker. */
    static boolean hasNuclear(List channels, List nuclearMarkers) {
        return (channels ?: []).any { isNuclear(it, nuclearMarkers) }
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
    static List splitOutputChannels(List channels, boolean isReference, List nuclearMarkers) {
        if (!channels) return []
        if (isReference) return channels.collect { it }
        return channels.findAll { !isNuclear(it, nuclearMarkers) }
    }
}
