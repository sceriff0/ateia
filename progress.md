Task 3: implementer DONE (d03dd7a new bin/utils/pixel_convention.py + 6 sites
routed through it behaviour-preserving; 8264a2a tests; e99241d the fix).
pytest 509/1 (+11 from 498/1), ruff clean, stub 62 published.
Task 3: central red — `Max absolute difference: 0.5`, x [6.5, 6.5] vs y [6., 6.].
Task 3: THE FINDING THAT VALIDATES THE WHOLE PLANNING PASS — the implementer
reports that under the NAIVE fix (adding +0.5 to obsm) the central consistency
test STILL PASSES. The trap regression test is the only discriminator. Had the
join_flowpath coupling not been found during verification and written into the
brief, the obvious fix would have shipped, passed its own test, and silently
broken the centroid-matching fallback.
Task 3: ruling R3 CONFIRMED by the implementer — it checked and found no .zarr
consumer wants the QuPath convention; only join_flowpath.py reads the store, via
obsm["spatial"].
Task 3: CONCERN TO WATCH — geopandas/shapely/spatialdata are container-only, so
the real build_shapes/build_table run against ~15 lines of stand-ins. Referred to
the reviewer to judge how much the central test actually proves.
Task 4: dispatched (base e99241d, implementer opus), concurrently with task 3's
review (read-only). Carries the collectFile/cache=false hazard explicitly.
