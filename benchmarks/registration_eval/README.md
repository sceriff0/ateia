# Registration-Accuracy Evaluation

Compares Mirage's VALIS registration with in-process tiling **ON vs OFF** against
landmark ground truth, reporting three error views per pair/mode:
true landmark **TRE / rTRE / µm**, VALIS's self-reported **rTRE**, and the
**feature-distance** estimate. See spec §5.

## Data access (you must download — both are gated)

- **ANHIR** — create a grand-challenge.org account, join the challenge, accept the
  CC-BY-NC-SA licence, download. Landmarks ship as `,X,Y` CSVs alongside multi-scale images.
  <https://anhir.grand-challenge.org/Data/>
- **ACROBAT** — WSIs are open on the Swedish National Data Service, but **landmark
  annotations are behind the challenge account**. <https://acrobat.grand-challenge.org/>

> The evaluator is format-driven and download-independent. The ACROBAT adapter's
> column names (`benchmarks/registration_eval/adapters/acrobat.py:COLS`) follow the
> public docs — adjust them if your downloaded CSV differs.

## Run

1. Describe your pairs in a CSV (`pair_id,ref_image,moving_image,source_landmarks,target_landmarks,width,height,pixel_size_um`), then:

       python -m benchmarks.registration_eval.prepare_pairs --pairs-csv pairs.csv --out reg_prepared

2. Register with both methods + evaluate (run where the VALIS env is available). The 4th
   argument is optional — a StarDist model dir turns on the reg_qc=2 seg-overlap leg:

       benchmarks/registration_eval/run_registration.sh reg_prepared/pairs_manifest.csv reg_prepared reg_results [stardist_model_dir]

3. Aggregate for the notebook (Plan 3):

       python -m benchmarks.registration_eval.aggregate_eval --eval-dir reg_results --out reg_eval.csv --agg-out reg_eval_agg.csv

## Limitations

- **Brightfield input:** ANHIR/ACROBAT are H&E/IHC. We drive `bin/register.py`
  directly (not the full Mirage pipeline), so the DAPI requirement and BaSiC
  preprocessing do not apply. The registration is the same VALIS used by the pipeline.
- **Landmark TRE needs a transform to warp through:** `valis` supplies a registrar pickle,
  `tiled`/STARE a transform manifest (warped via `bin/utils/tiled_stage_warp.make_warper`,
  the same builder the pipeline's own tiled reg_qc uses). A method that emits neither
  cannot be scored against landmarks here.
- **The seg-overlap leg needs a StarDist model.** `segment_to_geojson.py` requires
  `--model-name`, and the shipped default is not a StarDist built-in, so the leg is opt-in
  via the 4th argument (override the name with `SEG_MODEL_NAME`). Without it the landmark
  TRE and the method-native numbers are still produced.
- **`requires bash 4+`** — `run_registration.sh` uses indexed-array column lookup
  compatible with bash 3, but on macOS prefer a homebrew bash.
