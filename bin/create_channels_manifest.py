#!/usr/bin/env python3
"""
Create a channels manifest JSON from registered OME-TIFF files.

Reads OME-XML metadata from each *_registered.ome.tiff file in the input
directory and writes a JSON manifest mapping filename to channel names.
"""

import argparse
import json
import os
import xml.etree.ElementTree as ET

import tifffile


OME_NAMESPACE = {"ome": "http://www.openmicroscopy.org/Schemas/OME/2016-06"}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract channel names from registered OME-TIFF files into a JSON manifest."
    )
    parser.add_argument(
        "--input-dir",
        required=True,
        help="Directory containing *_registered.ome.tiff files.",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="Path for the output JSON manifest.",
    )
    return parser.parse_args()


def extract_channel_names(filepath):
    """Extract channel names from OME-XML metadata of a TIFF file."""
    with tifffile.TiffFile(filepath) as tif:
        if not tif.ome_metadata:
            raise RuntimeError(f"No OME metadata in registered file: {filepath}")

        root = ET.fromstring(tif.ome_metadata)
        channels = root.findall(".//ome:Channel", OME_NAMESPACE) or root.findall(
            ".//{*}Channel"
        )
        return [
            ch.get("Name") or ch.get("ID", f"Channel_{i}")
            for i, ch in enumerate(channels)
        ]


def main():
    args = parse_args()

    manifest = {}
    for filename in sorted(os.listdir(args.input_dir)):
        if not filename.endswith("_registered.ome.tiff"):
            continue
        filepath = os.path.join(args.input_dir, filename)
        manifest[filename] = extract_channel_names(filepath)

    with open(args.output, "w") as fp:
        json.dump(manifest, fp)

    print(f"Channels manifest: {len(manifest)} files")
    for fname, chs in manifest.items():
        print(f"  {fname}: {chs}")


if __name__ == "__main__":
    main()
