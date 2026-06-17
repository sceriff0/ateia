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

2. Register (tiled/untiled) + evaluate (run where the VALIS env is available):

       benchmarks/registration_eval/run_registration.sh reg_prepared/pairs_manifest.csv reg_prepared reg_results

3. Aggregate for the notebook (Plan 3):

       python -m benchmarks.registration_eval.aggregate_eval --eval-dir reg_results --out reg_eval.csv --agg-out reg_eval_agg.csv

## Limitations

- **Brightfield input:** ANHIR/ACROBAT are H&E/IHC. We drive `bin/register.py`
  directly (not the full Mirage pipeline), so the DAPI requirement and BaSiC
  preprocessing do not apply. The registration is the same VALIS used by the pipeline.
- **Nextflow-distributed tiling** (`reg_distributed_tiling`) produces no single VALIS
  registrar pickle, so true landmark TRE is not available for it. Compare that path
  with the feature-distance estimate on its registered output if needed.
- **`requires bash 4+`** — `run_registration.sh` uses indexed-array column lookup
  compatible with bash 3, but on macOS prefer a homebrew bash.
