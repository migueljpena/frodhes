"""The 200A's speaker (amp, speaker and the mic in its room) as a filter on the line signal: BaltiWurli recorded
every note through the DI and the speaker at once (Spkr_* and DI_* are the same strike), so per pair the transfer
function H = S_xy / S_xx (Welch, 0.5-2.5 s after the onset), kept where the coherence passes 0.9; all pairs pooled
in 1/6-octave bins (median dB). P and MF apart, to see whether the amp compresses (the gain would differ).

Usage: measure_speaker.py OUT.csv
"""
import csv
import glob
import sys

import numpy as np
from scipy.io import wavfile
from scipy.signal import csd, welch, coherence

BASE = "/Users/migueljpena/Projects/frodhes-refs/audio/pianobook/baltiwurli/"


def ld(p):
    sr, x = wavfile.read(p)
    x = x.astype(np.float64)
    return sr, (x.mean(axis=1) if x.ndim > 1 else x)


def main():
    edges = 20 * 2 ** (np.arange(0, 60) / 6.0)
    pts = {"P": [], "MF": []}
    for lay in pts:
        for p in sorted(glob.glob(BASE + f"BaltiWurli_DI_{lay}_*.wav")):
            q = p.replace("_DI_", "_Spkr_")
            if not glob.glob(q) or "G#4" in p:      # the P G#4 pair is not one take (its onsets differ)
                continue
            sr, x = ld(p)
            _, y = ld(q)
            e = np.abs(x)
            o = int(np.argmax(e > 0.1 * e.max()))
            x, y = x[o: o + int(2.5 * sr)], y[o: o + int(2.5 * sr)]
            f, Pxy = csd(x, y, fs=sr, nperseg=8192)
            _, Pxx = welch(x, fs=sr, nperseg=8192)
            _, C = coherence(x, y, fs=sr, nperseg=8192)
            ok = (C > 0.9) & (f > 20)
            for fi, h in zip(f[ok], np.abs(Pxy[ok] / Pxx[ok])):
                pts[lay].append((fi, 20 * np.log10(h)))
    rows = []
    for i in range(len(edges) - 1):
        r = {"hz": round(float(np.sqrt(edges[i] * edges[i + 1])), 1)}
        for lay, v in pts.items():
            v = np.array(v)
            m = (v[:, 0] >= edges[i]) & (v[:, 0] < edges[i + 1])
            r[lay] = round(float(np.median(v[m, 1])), 2) if m.sum() >= 3 else ""
            r["n_" + lay] = int(m.sum())
        rows.append(r)
    with open(sys.argv[1], "w", newline="") as fo:
        w = csv.DictWriter(fo, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    for r in rows:
        if r["MF"] != "" or r["P"] != "":
            print(f"{r['hz']:8.0f} Hz  MF {r['MF']!s:>7} ({r['n_MF']:3d})   P {r['P']!s:>7} ({r['n_P']:3d})")


if __name__ == "__main__":
    main()
