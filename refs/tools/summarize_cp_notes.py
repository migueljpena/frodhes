#!/usr/bin/env python3
"""Per key, from tools/measure_cp_notes.py's JSON: B, the pair's detune (the fundamental's beat, kept when the 2nd
partial beats at ~2x), each partial's T60 (early 0.08..1 s, late 1.5..8 s), the dynamic range (the softest layer to
the hardest, the files' own levels) and the brightness's change (H2..H6 re H1, hardest minus softest).

  python3 tools/summarize_cp_notes.py measure.json [summary.json]"""
import json
import math
import sys
from collections import defaultdict

import numpy as np

d = json.load(open(sys.argv[1]))
by = defaultdict(list)
for r in d:
    by[r["key"]].append(r)
names = ["C", "Db", "D", "Eb", "E", "F", "Gb", "G", "Ab", "A", "Bb", "B"]
out = []
print("key  note   f1(cents re A440)  B          detune c (n)  T60 early h1/h2/h4 s     T60 late h1/h2 s   range dB  dBright h2-6")
for k in sorted(by):
    rs = sorted(by[k], key=lambda r: r["level_db"])
    f1 = np.median([r["f1"] for r in rs])
    cents = 1200 * math.log2(f1 / (440 * 2 ** ((k - 69) / 12)))
    B = float(np.median([r["B"] for r in rs if r["B"] > 0])) if any(r["B"] > 0 for r in rs) else None
    det = []
    for r in rs:
        b = r["beats"]
        if b and b[0] and len(b) > 1 and b[1] and b[0]["beat_hz"] > 0.2 and abs(b[1]["beat_hz"] / b[0]["beat_hz"] - 2) < 0.3:
            det.append(1200 * math.log2(1 + b[0]["beat_hz"] / r["f1"]))
    det_c = float(np.median(det)) if det else None

    def t60(r, n, which):
        for p in r["partials"]:
            if p["n"] == n and p[which] and p[which] < -0.05:
                return -60 / p[which]
        return None
    hard = rs[-2:] if len(rs) > 2 else rs                     # the louder layers: better above the noise
    te = [np.median([x for x in (t60(r, n, "early_dbs") for r in hard) if x] or [np.nan]) for n in (1, 2, 4)]
    tl = [np.median([x for x in (t60(r, n, "late_dbs") for r in hard) if x] or [np.nan]) for n in (1, 2)]
    rng = rs[-1]["level_db"] - rs[0]["level_db"]

    def bright(r):
        v = [p["rel_db"] for p in r["partials"] if 2 <= p["n"] <= 6]
        return float(np.mean(v)) if v else None
    br = (bright(rs[-1]) - bright(rs[0])) if bright(rs[-1]) is not None and bright(rs[0]) is not None else None
    nm = names[k % 12] + str(k // 12 - 1)
    print(f"{k:3d}  {nm:5s} {f1:8.2f} ({cents:+5.1f})   {B if B else 0:.2e}   "
          f"{'%5.1f' % det_c if det_c is not None else '  -  '} ({len(det)})     "
          f"{te[0]:5.1f} {te[1]:5.1f} {te[2]:5.1f}         {tl[0]:5.1f} {tl[1]:5.1f}       {rng:5.1f}    "
          f"{'%+5.1f' % br if br is not None else '-'}")
    out.append(dict(key=k, f1=f1, cents=cents, B=B, detune_cents=det_c, n_det=len(det), t60_early=te, t60_late=tl,
                    range_db=rng, d_bright_db=br, layers=len(rs)))
if len(sys.argv) > 2:
    json.dump(out, open(sys.argv[2], "w"), indent=1)
