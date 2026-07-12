"""Deterministic synthetic multichannel image + cell/nucleus masks for CSE tests.

CSE's own example_data are Git-LFS stubs, so we synthesize a small labeled scene:
a grid of square cells (each with a centered nucleus of the same label) whose
3 channels separate the cells into 3 intensity types — enough to exercise
matching, KMeans k=2..10, and silhouette.
"""
import numpy as np

PIXEL_UM = 0.5

def make_arrays():
    rng = np.random.default_rng(0)
    Y = X = 160
    C = 3
    cell = np.zeros((Y, X), np.int32)
    nuc = np.zeros((Y, X), np.int32)
    img = np.zeros((C, Y, X), np.float32)
    cid = 0
    for gy in range(6, Y - 12, 14):
        for gx in range(6, X - 12, 14):
            cid += 1
            cell[gy:gy + 10, gx:gx + 10] = cid
            nuc[gy + 3:gy + 7, gx + 3:gx + 7] = cid
            t = cid % C
            for c in range(C):
                img[c, gy:gy + 10, gx:gx + 10] = (
                    50 + (80 if c == t else 5) + rng.normal(0, 3, (10, 10))
                )
    return img, cell, nuc

def make_fixture():
    img, cell, nuc = make_arrays()
    img5 = img[np.newaxis, :, np.newaxis, :, :]                    # (1,C,1,Y,X)
    mask5 = np.stack([cell, nuc], 0)[np.newaxis, :, np.newaxis, :, :]  # (1,2,1,Y,X)
    img_d = {"name": "synth", "img": None, "data": img5}
    mask_d = {"name": "synth", "img": None, "data": mask5}
    return img_d, mask_d, PIXEL_UM
