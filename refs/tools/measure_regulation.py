"""Every key and every layer of the WoerliTzer line samples, and BaltiWurli's DI notes: the fit's features (fit_reed
feats) plus the strike's spike and the action noise before the tone. The input of fit_reed.py "v4" (all layers,
the second instrument) and of the measured per-key regulation (export_play.py, "regulation": "measured").

Usage: measure_regulation.py OUT_DIR      (writes worlitzer_layers.csv, baltiwurli_di.csv, strike_noise.csv)

- spike: the peak |x| in the tone's first 3 ms over the RMS 50-100 ms in (dB); the tone starts where |x| first
  reaches 0.3 of its peak.
- the action noise: in the loud layers a burst ahead of the reed's tone (the key and hammer through the reed bar,
  picked up: it is in the line signal). Its lead (from |x| at 0.02 of the peak to the tone), its RMS over the tone's
  first 50 ms (dB), its band (the centroid of the lead's spectrum).
"""
import csv
import math
import os
import re
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fit_reed as F  # noqa: E402

SR = F.SR
BALTI = "/Users/migueljpena/Projects/frodhes-refs/audio/pianobook/baltiwurli/"


def tone_onset(x):
    a = np.abs(x)
    return int(np.argmax(a > 0.3 * a.max()))


def spike(x):
    t = tone_onset(x)
    body = x[t + int(0.05 * SR): t + int(0.1 * SR)]
    return 20 * math.log10(np.abs(x[t: t + int(0.003 * SR)]).max() / (np.sqrt((body ** 2).mean()) + 1e-12))


def action_noise(x):
    """(lead ms, level dB re the tone's first 50 ms, centroid Hz) of what comes before the tone."""
    a = np.abs(x)
    t = tone_onset(x)
    o = int(np.argmax(a > 0.02 * a.max()))
    tone = x[t: t + int(0.05 * SR)]
    pre = x[max(t - int(0.012 * SR), o): t] if t - o > int(0.002 * SR) else x[0:0]
    if len(pre) < 64 or t - o > int(0.04 * SR):      # nothing before the tone, or a file's own lead-in
        return (t - o) / SR * 1e3, np.nan, np.nan
    sp = np.abs(np.fft.rfft(pre * np.hanning(len(pre)), 8192)) ** 2
    f = np.fft.rfftfreq(8192, 1 / SR)
    cen = float((sp * f).sum() / (sp.sum() + 1e-18))
    lvl = 10 * math.log10((pre ** 2).mean() / ((tone ** 2).mean() + 1e-18))
    return (t - o) / SR * 1e3, lvl, cen


def feats_all(x, midi):
    f = F.feats(x, midi)
    f["spike"] = spike(x)
    f["lead_ms"], f["noise_db"], f["noise_hz"] = action_noise(x)
    return f


def worlitzer():
    txt = open(F.PRESET, encoding="utf-8").read()
    g = re.search(r'<group name="Line"(.*?)</group>', txt, re.S).group(1)
    by = {}
    for s in re.findall(r"<sample ([^>]*)", g):
        a = dict(re.findall(r'(\w+)="([^"]*)"', s))
        by.setdefault(int(a["rootNote"]), []).append(((int(a["loVel"]) + int(a["hiVel"])) // 2, a["path"].rsplit("/", 1)[-1]))
    base = F.PRESET.rsplit("/", 1)[0] + "/Samples/"
    rows = []
    for k in sorted(by):
        layers = sorted(by[k])
        fs = [(max(v, 1), feats_all(F.load(base + p), k)) for v, p in layers]
        top = fs[-1][1]["peak"]
        for i, (v, f) in enumerate(fs):
            rows.append(dict(src="worlitzer", midi=k, vel=v, layer=i, nlayers=len(fs), rel=f["peak"] - top, **f))
    return rows


KEY = {"C0": 36, "G0": 43, "D1": 50, "A1": 57, "E2": 64, "A#2": 70, "B2": 71, "E3": 76, "F#3": 78, "C4": 85, "C#4": 85,
       "F#4": 90, "G#4": 92}


def baltiwurli():
    """The DI notes (P / MF / F at 9 keys each). The key: neither the preset's rootNote nor the file's name is the
    note's, and a harmonic-sum f0 misses some (weak h1): KEY below maps each name to the key its spectrum shows (the
    lowest strong peak and the harmonics' spacing, checked by hand 2026-10-27). The velocity of each layer: its
    range's middle (P 32, MF 80, F 120). The F layer is normalised (every F note peaks at -3 dBFS): its "rel" means
    nothing, only its timbre is used."""
    rows = []
    vel = {"P": 32, "MF": 80, "F": 120}
    files = sorted(f for f in os.listdir(BALTI) if f.startswith("BaltiWurli_DI_"))
    got = []
    for fn in files:
        layer = fn.split("_")[2]
        x = F.load(BALTI + fn)
        midi = KEY[fn.rsplit("_", 1)[-1][:-4]]
        got.append((midi, layer, fn, feats_all(x, midi)))
    for midi in sorted({g[0] for g in got}):
        fs = [g for g in got if g[0] == midi]
        top = max(f["peak"] for _, _, _, f in fs)
        for m, layer, fn, f in fs:
            rows.append(dict(src="baltiwurli", midi=m, vel=vel[layer], layer=layer, nlayers=len(fs), rel=f["peak"] - top,
                             file=fn, **f))
    return rows


def main():
    out = sys.argv[1]
    os.makedirs(out, exist_ok=True)
    for name, rows in (("worlitzer_layers.csv", worlitzer()), ("baltiwurli_di.csv", baltiwurli())):
        keys = []
        for r in rows:
            keys += [k for k in r if k not in keys]
        with open(os.path.join(out, name), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)
        print(name, len(rows), "rows")


if __name__ == "__main__":
    main()
