"""Fit measure_speaker.py's H (the MF pairs, the most points) with five biquads: a high-pass, a low peak (the
cabinet's body), a mid dip, a presence peak and a low-pass. Least squares in dB, weighted by the points per bin (sqrt).

Usage: fit_speaker.py H.csv OUT.json
"""
import csv
import json
import math
import sys

import numpy as np
from scipy.optimize import minimize
from scipy.signal import freqz

sys.path.insert(0, "/Users/migueljpena/Projects/frodhes/tools")
import reed_model as M  # noqa: E402

rows = [r for r in csv.DictReader(open(sys.argv[1])) if r["MF"] != ""]
f = np.array([float(r["hz"]) for r in rows])
h = np.array([float(r["MF"]) for r in rows])
w = np.sqrt(np.array([float(r["n_MF"]) for r in rows]))
NAMES = ("hp_hz", "hp_q", "lo_hz", "lo_db", "lo_q", "mid_hz", "mid_db", "mid_q", "pr_hz", "pr_db", "pr_q", "lp_hz", "lp_q", "gain_db")


def resp(S):
    return 20 * np.log10(np.abs(M.speaker_response(S, f)) + 1e-12)


def loss(th):
    S = dict(zip(NAMES, th))
    if min(S["hp_q"], S["lo_q"], S["mid_q"], S["pr_q"], S["lp_q"]) < 0.3 or S["lp_hz"] > 15000 or S["hp_hz"] < 20:
        return 1e9
    return float(np.sum(w * (resp(S) - h) ** 2) / w.sum())


th0 = [70, 0.7, 200, 4, 0.8, 600, -5, 1.0, 1200, 3, 1.5, 2000, 0.7, 0]
r = minimize(loss, th0, method="Nelder-Mead", options={"maxiter": 20000, "xatol": 1e-4, "fatol": 1e-6})
S = {k: round(float(v), 4) for k, v in zip(NAMES, r.x)}
print("rms error %.2f dB" % math.sqrt(r.fun), S)
for fi, hi, mi in zip(f, h, resp(S)):
    print(f"{fi:7.0f} Hz  measured {hi:6.1f}  fit {mi:6.1f}")
json.dump(S, open(sys.argv[2], "w"), indent=1)
