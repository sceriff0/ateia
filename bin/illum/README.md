# illum — illumination-correction experiment library

Benchmark flat-field / darkfield / background-removal variants on a stitched mosaic.

- `illum_benchmark.py --image X.ome.tiff --outdir bench --channels DAPI CD3 ... --approx-tile 1950`
  → `bench/report.html`, `bench/metrics.json`, `bench/plots/`, `bench/pyramids/`.
- `--full-grid` runs the full variant matrix; `--no-pyramids` skips pyramid writing;
  `--max-channels N` limits channels for a quick pass.
- `illum_correct.py` applies ONE variant (pipeline-facing), writing `<stem>_periodic.ome.tif`.

Run on the real cluster mosaic, open `report.html`, and compare pyramids in QuPath.
Ranking is a seam-suppression + background-flatness composite; when variants are
within ~0.02 composite, decide from the crops and QuPath.
