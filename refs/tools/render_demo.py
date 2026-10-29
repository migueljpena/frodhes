"""Listening files for the reed model against the WoerliTzer line samples.

Usage: render_demo.py PARAMS.json OUTDIR

  ab_<key>.wav   the recording, then the model: soft, middle and loud strikes of one key
  ab_all.wav     every A/B pair in a row (keys 36 .. 84)
  demo.wav       the model alone: a chord progression and a bass line, the tremolo off
Every file normalised to -1 dBFS peak (a level match by ear: the recordings and the model have no common gain).
"""
import json
import os
import re
import sys

import numpy as np
from scipy.io import wavfile

sys.path.insert(0, "/Users/migueljpena/Projects/frodhes/tools")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import reed_model as M  # noqa: E402
from fit_reed import PRESET, load  # noqa: E402

SR = 44100


def norm(x, db=-1.0):
    return x / (np.abs(x).max() + 1e-12) * 10 ** (db / 20)


def write(path, x):
    wavfile.write(path, SR, (norm(x) * 32767).astype(np.int16))


def gap(s):
    return np.zeros(int(s * SR))


def main():
    P = dict(M.DEFAULT)
    st = json.load(open(sys.argv[1]))
    P.update({k: v for k, v in st.items() if k != "loss"})
    out = sys.argv[2]
    os.makedirs(out, exist_ok=True)
    txt = open(PRESET, encoding="utf-8").read()
    g = re.search(r'<group name="Line"(.*?)</group>', txt, re.S).group(1)
    by = {}
    for s in re.findall(r"<sample ([^>]*)", g):
        a = dict(re.findall(r'(\w+)="([^"]*)"', s))
        by.setdefault(int(a["rootNote"]), []).append(((int(a["loVel"]) + int(a["hiVel"])) // 2, a["path"].rsplit("/", 1)[-1]))
    base = PRESET.rsplit("/", 1)[0] + "/Samples/"
    allp = []
    for k in (36, 43, 48, 55, 60, 67, 72, 79, 84):
        layers = sorted(by[k])
        pairs = []
        for v, p in (layers[0], layers[len(layers) // 2], layers[-1]):
            ref = load(base + p)
            ref = ref[int(np.argmax(np.abs(ref) > 0.05 * np.abs(ref).max())):][: int(2.5 * SR)]
            pairs.append((ref, M.render(k, max(v, 1), 2.5, P)))
        # the soft and middle strikes keep their level against the key's loudest (recording and model apart)
        rmax = np.abs(pairs[-1][0]).max()
        mmax = np.abs(pairs[-1][1]).max()
        seq = []
        for ref, mod in pairs:
            seq += [ref / rmax * 0.89, gap(0.3), mod / mmax * 0.89, gap(0.8)]
        write(f"{out}/ab_{k}.wav", np.concatenate(seq))
        allp.append(np.concatenate(seq))
    write(f"{out}/ab_all.wav", np.concatenate(allp))
    # demo: a ii-V-I-vi in C, voiced in the middle, with a bass note and a short melodic fill
    notes, t = [], 0.0
    for chord, bass in (((62, 65, 69, 72), 50), ((59, 65, 67, 71), 43), ((60, 64, 67, 71), 48), ((57, 60, 64, 67), 45)):
        for rep, vel in ((0, 70), (1, 95)):
            for i, m in enumerate(chord):
                notes.append((t + rep * 1.0 + i * 0.01, m, vel - 5 * i, 0.85))
            notes.append((t + rep * 1.0, bass, vel + 15, 0.9))
        t += 2.0
    for i, m in enumerate((72, 74, 76, 79, 76, 74, 72, 69, 67, 64, 62, 60)):
        notes.append((t + i * 0.18, m, 60 + 40 * (i % 3 == 0), 0.16))
    notes.append((t + 12 * 0.18, 48, 120, 2.5))
    notes.append((t + 12 * 0.18, 60, 110, 2.5))
    notes.append((t + 12 * 0.18, 64, 100, 2.5))
    write(f"{out}/demo.wav", M.render_notes(notes, t + 12 * 0.18 + 4.0, P))
    print("wrote", out)


if __name__ == "__main__":
    main()
