"""How much the pickup's fold aliases (the firmware runs it at the plain sample rate): a set's note rendered at 4 x
44.1 kHz, its harmonics above 22.05 kHz (what a 44.1 kHz render folds back into the band) against h1, and where
they would land. Usage: alias_check.py SET.json [keys...]"""
import json
import math
import sys

import numpy as np

sys.path.insert(0, "/Users/migueljpena/Projects/frodhes/tools")
import reed_model as M  # noqa: E402

P = json.load(open(sys.argv[1]))
keys = [int(k) for k in sys.argv[2:]] or [72, 79, 84, 90, 96]
OS = 4
M.FS = 44100.0 * OS
M._HG.clear()
for k in keys:
    for vel in (80, 127):
        y = M.render(k, vel, 0.3, P)
        seg = y[int(0.03 * M.FS): int(0.03 * M.FS) + 1 << 15]
        sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg))))
        f = np.fft.rfftfreq(len(seg), 1 / M.FS)
        f0 = M.f0_of(k, P)

        def lvl(fr):
            b = int(round(fr * len(seg) / M.FS))
            return sp[max(b - 3, 0): b + 4].max()
        h1 = lvl(f0)
        worst, at = -200.0, 0.0
        tot = 0.0
        kk = 1
        while kk * f0 < M.FS / 2:
            if kk * f0 > 22050:
                d = 20 * math.log10(lvl(kk * f0) / h1 + 1e-15)
                tot += (lvl(kk * f0) / h1) ** 2
                if d > worst:
                    fold = abs(((kk * f0 + 22050) % 44100) - 22050)
                    worst, at = d, fold
            kk += 1
        print("key %d vel %3d: loudest harmonic over 22.05 kHz %6.1f dB re h1 (folds to %5.0f Hz); all of them %6.1f dB"
              % (k, vel, worst, at, 10 * math.log10(tot + 1e-30)))
