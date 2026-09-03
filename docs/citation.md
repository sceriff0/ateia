# Citation

If you use MIRAGE in your research, please cite the pipeline itself **and** the underlying tools listed below.

## Citing MIRAGE

Include in your methods section:

- The pipeline name: **MIRAGE**
- The specific version: use a tagged release (e.g. `v1.0.0`) or the commit SHA from `git rev-parse HEAD`
- The repository URL: <https://github.com/sceriff0/mirage>

Each tagged release is archived and given a DOI; cite the DOI for the version you
ran. It is recorded in [`CITATION.cff`](https://github.com/sceriff0/mirage/blob/main/CITATION.cff)
at the repository root and shown on the README badge. If you ran an untagged
commit, cite the commit SHA from `git rev-parse HEAD` — that is the only thing
that makes an untagged run reproducible.

## Tool citations

The full list of underlying-tool citations is maintained at the repository root as [`CITATIONS.md`](https://github.com/sceriff0/mirage/blob/main/CITATIONS.md) and is included verbatim below.

!!! tip "Cite the backend you ran"
    MIRAGE ships three interchangeable segmentation backends — **StarDist**, **InstanSeg**, and **CellSAM** — selected at runtime via `--seg_method`. Cite the segmentation reference(s) that correspond to the backend(s) you actually used.

{% include-markdown "../CITATIONS.md" heading-offset=2 %}
