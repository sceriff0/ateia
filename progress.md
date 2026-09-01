
## Task 11b (parallel branch refactor/container-pins-2026-08-14)

Task 11b: implementer DONE (26a4e85 guards, 59cde4c pins, f9dbb8c docs).
470/1 (baseline 466/1, +4 tests), ruff clean. No image built or claimed built.
Task 11b: CORRECTION TO MY BRIEF — FOUR images lacked a tifffile pin, not three.
segmentation_gpu's implicit case counted too, and the guard found it naturally.
Task 11b: NEW FINDING not in the review or the verification pass —
`aicsimageio==4.14.0`'s real PyPI metadata constrains `tifffile<2023.3.15`, an
undocumented ceiling. That is why segmentation_gpu cannot join the convergence
onto 2024.7.2 and took its own 2023.2.28. Referred to the reviewer for
independent verification: if the ceiling is wrong, so is the pin.
Task 11b: SECOND NEW FINDING — `containers/segmentation/requirements.txt` is
DEAD, never installed by its own Dockerfile. Left unfixed as out of scope;
reviewer asked whether a pin guard asserting a pin in a file nobody installs is
meaningful or misleading.
Task 11b: cupy/cucim/aicsimageio[all] removed from quantification_gpu on a grep
over bin/ alone — reviewer asked to judge whether that is sufficient evidence
given transitive imports.
Task 3: review — spec ✅ on all four requirements, 1 ⚠️ (no test anywhere builds
a real spatialdata store; the container path is unexercised). Quality APPROVED,
0 Critical, 0 Important, 5 Minor.
Task 3: the reviewer verified by MUTATION PROBE rather than by reading — it
monkeypatched corner_to_centre to identity (consistency test raises), and wrapped
build_table to apply the naive +0.5 (trap test fails, consistency test still
passes). It independently reproduced the implementer's key claim instead of
accepting it. It also probed the GeoJSON guards by stripping centre_to_corner.
Task 3: complete (commits 3768e29..e99241d across 3 commits, review clean).

Ruling R8: I stop promoting Minors into fix rounds from here unless the finding
is a documented repo hazard appearing in NEW code (the bar I used for tasks 1
and 2). Task 3's five Minors are test-strength refinements on an APPROVED task
and go to the final whole-branch review instead, per the SDD rule that Minors
never enter the loop. Cost if wrong: they land in one batched final fix wave
rather than now, with warm context lost.
Task 3 minors deferred to final review, PRIORITY FIRST:
  (a) [priority] tests/test_pixel_convention.py fixtures are x/y-SYMMETRIC —
      squares at (6,6) and (16,16) — so a transposition between shapes (x,y) and
      obsm (df[["x","y"]]) would be INVISIBLE. Real blind spot in a coordinates
      task. One asymmetric cell fixes it.
  (b) the stand-in fixture overrides shapely/geopandas/spatialdata in sys.modules
      UNCONDITIONALLY, so the real libraries are never used even where installed;
      gating on importlib.util.find_spec would let them run.
  (c) "absence is a decision" applied inconsistently — mask_to_geojson.py:43 gets
      keep_centre, export_spatialdata.py:437 gets only a comment.
  (d) test_geojson_centroid_measurements asserts round(6.5*PIXEL_SIZE,3), the
      implementation's own expression; a literal would be stronger.
  (e) _shift uses np.isscalar, False for a 0-d ndarray. No current caller does.
