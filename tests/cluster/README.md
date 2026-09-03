# Cluster validation kit

Everything in this directory is run **by hand, on the cluster, against private
images**. Nothing here runs in CI, and nothing here needs a fixture that could
be committed.

## Why it exists

RULING R3: no vendor fixture may be committed. `.czi`, `.nd2`, `.lif`, `.svs`
and `.qptiff` are all claimed by `README.md` and `bin/convert_image.py`, and
none of them can be synthesised — a real Zeiss or Nikon file is the only way to
know the reader works. So the *evidence* is committed instead of the data:
`docs/validation/format_validation.md`, produced by `validate_formats.sh`, with
the pipeline's commit SHA and the probe container recorded in its header.

Everything that CAN be synthesised is already tested on every push, in the
`format-tests` job — pyramidal OME-TIFF, BigTIFF, interleaved RGB, 8-bit,
float32, HDF5 and NDPI/NDPIS (`tests/integration/formats/`).

## What you need

- Singularity, and the ability to pull `bolt3x/mirage-convert`.
- One image of each format you want validated, readable from a compute node.
- A site config (`conf/ieo.config`, or your own copy of
  `conf/site.config.template`). It carries `max_cpus`/`max_memory`, which the
  schema requires — RULING R4.

## Running it

1. Copy `samplesheet.template.csv` somewhere writable and fill in your real
   paths and channel lists. Delete the rows for formats you do not have.
2. Run:

   ```bash
   bash tests/cluster/validate_formats.sh \
       --samplesheet /scratch/you/vendor_samplesheet.csv \
       --outdir      /scratch/you/format-validation \
       --site-config conf/ieo.config
   ```

3. Read `/scratch/you/format-validation/format_validation.md`. Every row should
   end in `OK`; a `FAILED` row names the exception and is a finding, not a
   reason to delete the row.
4. Copy that file to `docs/validation/format_validation.md` and commit it.

## The two other cluster runs this phase needs

Both are reported in the same pull request, in prose — they produce no committed
artefact.

```bash
# 1. The real (non-stub) nf-test suite. It does not run on arm64 and CI runs it
#    only nightly, so `dev` has no real coverage at all between nightlies.
nf-test test --profile test,singularity --tag real

# 2. The two segmentation backends CI can never execute: the pytest job installs
#    neither stardist/csbdeep (see tests/expected_skips.txt's DEBT entry) nor
#    cellSAM, and both images are CUDA builds. Run each against ONE small private
#    slide, on a GPU node. --nv is what makes the host's GPU visible inside the
#    container; without it the run falls back to CPU and proves nothing about the
#    GPU path.
nextflow run . -profile singularity -c conf/ieo.config \
    --input /scratch/you/one_slide.csv --outdir /scratch/you/seg-stardist \
    --start segmentation --stop segmentation --seg_method stardist --pixel_size auto
nextflow run . -profile singularity -c conf/ieo.config \
    --input /scratch/you/one_slide.csv --outdir /scratch/you/seg-cellsam \
    --start segmentation --stop segmentation --seg_method cellsam --pixel_size auto
```

If `--nv` is not already in your site config's `singularity.runOptions`, add it
there rather than to the command line — the config is what the cluster's other
runs use too.
