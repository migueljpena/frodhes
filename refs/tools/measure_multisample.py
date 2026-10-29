"""Measure a multisample set whose file names give the key: Group<rr>_<n>_<Layer>_<key>_<name>.wav.

Usage: measure_multisample.py OUT.csv MIDI_OF_KEY1 FILE.wav ...
(Unplugged 200A: key 01 = A1 = MIDI 33.)

Per note: the measured f0 against the key's equal-tempered pitch, the peak level, the attack, the first 8 harmonics
50-150 ms in, every harmonic's decay (dB/s) from 0.2 s to 3 s or the noise floor, the T60 of h1, and the strongest
peaks that are not harmonics (a cantilever reed's second mode sits near 6.27 x f0).
"""
import csv
import re
import sys

import numpy as np
from scipy.io import wavfile
from scipy.signal import find_peaks

import analyze_notes

SR = 44100
FREE_F0 = False
NAME = re.compile(r"Group(\d+)_\d+_(Soft|Normal|Loud)_(\d+)_")


def load(path):
    sr, x = wavfile.read(path)
    assert sr == SR, (path, sr)
    scale = 1.0 if x.dtype.kind == "f" else np.iinfo(x.dtype).max + 1.0   # 24-bit files come as int32
    x = x.astype(np.float64) / scale
    return x.mean(axis=1) if x.ndim > 1 else x


def band_level(x, f, t0, t1, win=4096):
    """dB of the partial at f in hops of 512 from t0 to t1 (s): its magnitude in a Hann window, +-3 bins."""
    w = np.hanning(win)
    out, ts = [], []
    for s in range(int(t0 * SR), min(int(t1 * SR), len(x) - win), 512):
        sp = np.abs(np.fft.rfft(x[s:s + win] * w))
        b = int(round(f * win / SR))
        if b + 4 >= len(sp):
            break
        out.append(20 * np.log10(sp[b - 3:b + 4].max() + 1e-12))
        ts.append((s + win / 2) / SR)
    return np.array(ts), np.array(out)


def notes_named(key1, files):
    for path in sorted(files):
        m = NAME.search(path.rsplit("/", 1)[-1])
        if m:
            yield path, int(m.group(1)), m.group(2), key1 + int(m.group(3)) - 1


def notes_dspreset_group(preset, group):
    """Decent Sampler, one <group name=...>: each sample's layer is its velocity range's middle (v064), so a
    key's layers sort from soft to loud."""
    base = preset.rsplit("/", 1)[0]
    txt = open(preset, encoding="utf-8").read()
    g = re.search(r'<group name="%s"(.*?)</group>' % re.escape(group), txt, re.S)
    for s in re.findall(r"<sample ([^>]*)", g.group(1)):
        a = dict(re.findall(r'(\w+)="([^"]*)"', s))
        mid = (int(a["loVel"]) + int(a["hiVel"])) // 2
        yield f"{base}/Samples/{a['path'].rsplit('/', 1)[-1]}", 1, f"v{mid:03d}", int(a["rootNote"])


def notes_dspreset(preset):
    """Decent Sampler: each <sample path=... rootNote=...>; the layer is the name's part after the instrument
    (BaltiWurli_DI_MF_E2 -> DI_MF)."""
    base = preset.rsplit("/", 1)[0]
    seen = set()
    for m in re.finditer(r'<sample path="([^"]+)"[^>]*?rootNote="(\d+)"', open(preset).read()):
        rel, root = m.group(1), int(m.group(2))
        if rel in seen:
            continue
        seen.add(rel)
        parts = rel.rsplit("/", 1)[-1][:-4].split("_")
        yield f"{base}/{rel.rsplit('/', 1)[-1]}", 1, "_".join(parts[1:-1]), root


def measure_note(x, midi, free_f0=False):
    """One note's measures (x: the note from before its onset, float; midi: its key)."""
    if True:
        a = np.abs(x)
        on = int(np.argmax(a > 0.05 * a.max()))
        x = x[on:]
        if free_f0:      # the label's pitch is not trusted: the f0 found in the sound, its nearest key
            fz, _ = analyze_notes.f0_of(x[int(0.1 * SR):])
            midi = int(round(69 + 12 * np.log2(fz / 440.0)))
        f_nom = 440.0 * 2 ** ((midi - 69) / 12)
        # f0: the peak near the nominal pitch, 0.1-0.5 s in, 16384-point window, parabolic interpolation
        n = 16384
        seg = x[int(0.1 * SR):int(0.1 * SR) + n]
        if len(seg) < n:
            seg = np.pad(seg, (0, n - len(seg)))
        sp = np.abs(np.fft.rfft(seg * np.hanning(n)))
        b0 = int(round(f_nom * n / SR))
        lo, hi = int(b0 * 0.94), int(b0 * 1.06) + 2
        k = lo + int(np.argmax(sp[lo:hi]))
        al, bl, cl = np.log(sp[k - 1:k + 2] + 1e-12)
        f0 = (k + 0.5 * (al - cl) / (al - 2 * bl + cl + 1e-12)) * SR / n
        # harmonics 50-150 ms in
        seg2 = x[int(0.05 * SR):int(0.05 * SR) + 4096]
        sp2 = np.abs(np.fft.rfft(seg2 * np.hanning(4096)))
        h = []
        for i in range(1, 9):
            bb = int(round(i * f0 * 4096 / SR))
            h.append(sp2[bb - 2:bb + 3].max() if bb + 3 < len(sp2) else 1e-12)
        hdb = 20 * np.log10(np.array(h) / h[0])
        # decays: a line through each harmonic's level from 0.2 s to 3 s, cut where it nears the floor
        dur = len(x) / SR
        decs = []
        for i in (1, 2, 3):
            ts, lv = band_level(x, i * f0, 0.2, min(3.0, dur))
            if len(lv) < 6:
                decs.append(np.nan)
                continue
            keep = lv > lv.min() + 6           # above the floor
            decs.append(np.polyfit(ts[keep], lv[keep], 1)[0] if keep.sum() >= 5 else np.nan)
        # attack 10-90 % on a 1 ms RMS
        w1 = int(0.001 * SR)
        e = np.sqrt(np.convolve(x[:int(0.2 * SR)] ** 2, np.ones(w1) / w1, mode="same"))
        ipk = int(np.argmax(e))
        i10 = np.nonzero(e[:ipk + 1] >= 0.1 * e[ipk])[0][0]
        i90 = np.nonzero(e[:ipk + 1] >= 0.9 * e[ipk])[0][0]
        # inharmonic peaks 30-130 ms in (the reed's upper modes ring briefly)
        seg3 = x[int(0.03 * SR):int(0.03 * SR) + 8192]
        sp3 = 20 * np.log10(np.abs(np.fft.rfft(seg3 * np.hanning(8192))) + 1e-12)
        fr = np.fft.rfftfreq(8192, 1 / SR)
        pk, _ = find_peaks(sp3, prominence=10)
        inh = []
        ref = sp3[int(round(f0 * 8192 / SR))]
        for p in pk[np.argsort(sp3[pk])[::-1]]:
            r = fr[p] / f0
            if r > 1.5 and abs(r - round(r)) > 0.12 and fr[p] < 16000:
                inh.append(f"{r:.2f}@{sp3[p] - ref:.0f}")
            if len(inh) == 3:
                break
        return {
            "midi": midi, "f_nom": round(f_nom, 2), "f0": round(f0, 2),
            "cents": round(1200 * np.log2(f0 / f_nom), 1), "peak_db": round(20 * np.log10(a.max()), 1),
            "attack_ms": round((i90 - i10) / SR * 1000, 1), "dur_s": round(dur, 2),
            "dec_h1": round(decs[0], 2), "dec_h2": round(decs[1], 2), "dec_h3": round(decs[2], 2),
            "t60_h1": round(-60 / decs[0], 2) if decs[0] and decs[0] < 0 else "",
            **{f"h{i}": round(hdb[i - 1], 1) for i in range(2, 9)},
            "inharm": " ".join(inh),
        }


def main():
    out = sys.argv[1]
    global FREE_F0
    if sys.argv[2].endswith(".dspreset") and len(sys.argv) > 3:
        notes = notes_dspreset_group(sys.argv[2], sys.argv[3])
    elif sys.argv[2].endswith(".dspreset"):
        notes = notes_dspreset(sys.argv[2])
        FREE_F0 = True
    else:
        notes = notes_named(int(sys.argv[2]), sys.argv[3:])
    rows = []
    for path, rr, layer, midi in notes:
        rows.append({"rr": rr, "layer": layer, **measure_note(load(path), midi, FREE_F0)})
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"{len(rows)} notes -> {out}")


if __name__ == "__main__":
    main()
