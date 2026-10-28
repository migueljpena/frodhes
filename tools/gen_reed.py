#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Generate the reed engine's tables (the Wurlitzer 200A, firmware/src/reed.c) from a play set.

    tools/gen_reed.py [SET.json] [--out FILE]   -> firmware/gen/reed_tables.h
                                                   (SET: docs/model/data/wurli_v43.json, the play page's v4.3)

The set is the float model's (tools/reed_model.py; the play page in frodhes-refs follows it sample for sample), at the
VOICE knobs' centre and REG 10. Per key (MIDI 33..96, the 200A's 64): the three modes (the reed's first, its second at
r2 f0, the strike's knock "attack") as the two-multiply resonators of bars.c (x' = a1 x - a2 xp, the Q32 "lo" form
under 1/2 of 2 - a1, else Q30), their release pairs (the damper's fast stage, then the slow one past the knee), the
input weight of each, the rest point d, the output gain (the bell's slope-1 norm, the regulation's g, g_key), and the
strike's level per velocity (the reed_model normalisation: (vel / 127)^gamma / its own 50 ms peak x the full strike's,
times the key's swing). The hammer: each key's contact length per velocity (samples) and the half-sine shapes. The
bell (1 + z^2)^-p: log2 and exp2 tables. The preamp: tanh.
"""
import argparse
import hashlib
import json
import math
import os
import sys

import numpy as np

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import reed_model as M  # noqa: E402

FS = M.FS
KEYS = range(33, 97)
ROM_LO, ROM_HI = 0xFFC00000, 0xFFCFFFFF
NLV = 4                                                   # strike levels a key: one per contact length
LOG_N, EXP_N, TANH_N = 1024, 1024, 1024
TANH_MAX = 8.0


def rom_safe(v):
    """Nudge a 32-bit word out of the chip-ROM window (tools/build.py bans 0xFFC00000..0xFFCFFFFF)."""
    u = v & 0xFFFFFFFF
    while ROM_LO <= u <= ROM_HI:
        v += 1 if v < 0 else -1
        u = v & 0xFFFFFFFF
    return v


def q(x, bits):
    v = int(round(x * (1 << bits)))
    assert -(1 << 31) <= v < (1 << 31), (x, bits)
    return rom_safe(v)


def mode_words(f, t60, w):
    """(lo, k1, k2, b) of the resonator y = a1 y1 - a2 y2 + b x1 (reed-worklet.js Mode): a1 = 2 r cos w, a2 = r^2."""
    r = math.exp(-6.907755 / (t60 * FS))
    om = 2 * math.pi * f / FS
    c1 = 4 * math.sin(om / 2) ** 2 + 2 * math.cos(om) * (1 - r)      # 2 - a1, without the cancellation
    return {"c1": c1, "r": r, "b": w * r * math.sin(om)}


def form(m, damp=1.0):
    """The bars.c words for a mode with its states scaled by damp each sample (the damper: r -> r damp)."""
    r = m["r"] * damp
    a1 = 2 * r * math.cos(m["om"])
    c1 = 2.0 - a1
    if c1 < 0.5:
        return 1, q(c1, 32) if c1 < 0.5 else 0, q(1.0 - r * r, 32)
    return 0, q(a1, 30), q(r * r, 30)


def own_peak(f0, t60, P, pulse):
    """reed-worklet.js Voice.peak50: the 50 ms peak of the first two modes struck by pulse."""
    ms = [mode_words(f0, t60, 1.0)]
    if f0 * P["r2"] < FS * 0.45:
        ms.append(mode_words(f0 * P["r2"], t60 / P["q2"], P["w2"]))
    n = int(round(0.05 * FS))
    x = np.zeros(n)
    x[:len(pulse)] = pulse[:n]
    y = np.zeros(n)
    for f, m in zip((f0, f0 * P["r2"]), ms):
        om = 2 * math.pi * f / FS
        a1, a2 = 2 * m["r"] * math.cos(om), m["r"] ** 2
        y1 = y2 = x1 = 0.0
        for i in range(n):
            yy = a1 * y1 - a2 * y2 + m["b"] * x1
            x1 = x[i]
            y2, y1 = y1, yy
            y[i] += yy
    return np.abs(y).max()


def key_rows(P):
    rows, plen, shapes = [], [], {}
    gam = P["gamma"]
    for k in KEYS:
        f0, t60 = M.f0_of(k, P), M.t60_of(k, P)
        modes = [(f0, t60, 1.0), (f0 * P["r2"], t60 / P["q2"], P["w2"])]
        wa = M.attack_w(k, P)
        modes.append((f0, P["attack"]["t60"], wa) if wa > 0 else None)
        D = P["damp2"]
        df, dsl = math.exp(-6.907755 / (D["fast"] * FS)), math.exp(-6.907755 / (D["slow"] * FS))
        mw = []
        for md in modes:
            if md is None or md[0] >= FS * 0.45:
                mw.append(None)
                continue
            m = mode_words(*md)
            m["om"] = 2 * math.pi * md[0] / FS
            mw.append(m)
        # the strike's level: reed-worklet.js scale x swing (the ref hammer, with HAMMER at 5, is the hammer itself)
        hard = 1.0 / own_peak(f0, 1e12, P, M.hammer(127, P, k, ref=True))
        # the level depends on vel only through the contact's length in samples: one per length the key uses
        ns = sorted({max(int(round(M.tc_of(v, P, k, ref=True) * FS)), 2) for v in range(1, 128)})
        assert ns[-1] - ns[0] < NLV, (k, ns)
        lvl = [hard / own_peak(f0, t60, P, M.half_sine(nn / FS)) * M.swing_of(k, P) for nn in range(ns[0], ns[0] + NLV)]
        d = M.d_of(k, P)
        p = P["bell_p"]
        norm = (1 + d * d) ** (p + 1) / (2 * p * d)
        g = M.key_of(k, P, "g", 1.0) * M.g_key(k, P)
        n = []
        for v in range(1, 128):
            nn = max(int(round(M.tc_of(v, P, k) * FS)), 2)
            n.append(nn)
            shapes[nn] = M.half_sine(nn / FS)
        plen.append(n)
        rows.append({"modes": mw, "df": df, "ds": dsl, "d": d, "gout": norm * g, "lvl": lvl, "n0": ns[0], "f0": f0})
    vpow = [(v / 127.0) ** gam for v in range(128)]
    knee = int(round(D["fast"] * D["knee_db"] / -60.0 * FS))
    return rows, plen, shapes, vpow, knee


def header(P, src):
    rows, plen, shapes, vpow, knee = key_rows(P)
    L = []
    w = L.append
    w("/* SPDX-License-Identifier: GPL-3.0-only */")
    w("/* GENERATED by tools/gen_reed.py from %s: do not edit. The reed engine's tables (reed.c). */" % src)
    w("#define RE_KEY_LO 33u")
    w("#define RE_NKEY 64u")
    w("#define RE_NM 3u")
    w("#define RE_NLV %du" % NLV)
    w("#define RE_KNEE %du                  /* the damper's fast stage, samples (to its knee) */" % knee)
    w("#define RE_BELL_P %d                 /* the bell's p, Q16 */" % q(P["bell_p"], 16))
    w("#define RE_DRIVE %d                  /* the preamp's drive, Q16 */" % q(P["drive"], 16))
    w("#define RE_OUT_GAIN %d               /* the set's out_gain, Q16 */" % q(P.get("out_gain", 1.0), 16))
    D = P["damp2"]
    df, dsl = math.exp(-6.907755 / (D["fast"] * FS)), math.exp(-6.907755 / (D["slow"] * FS))
    w("#define RE_D_REL %d                  /* the damper's steps (Q30): 1 / fast, fast / slow, fast, slow */" % q(1 / df, 30))
    w("#define RE_D_KNEE %d" % q(df / dsl, 30))
    w("#define RE_D_FAST %d" % q(df, 30))
    w("#define RE_D_SLOW %d" % q(dsl, 30))
    w("/* per key: modes (lo, k1, k2: sustain, fast release, slow release; b Q31: the input weight), d Q27, gout Q24,")
    w(" * n0: the shortest contact (samples); lvl Q24 for the contacts n0, n0 + 1 ... */")
    w("static const re_key_t RE_KEYS[RE_NKEY] = {")
    for r in rows:
        ms = []
        for m in r["modes"]:
            if m is None:
                ms.append("{0,0,0,0,0,0,0,0,0,0}")
                continue
            s, f, sl = form(m), form(m, r["df"]), form(m, r["ds"])
            ms.append("{%d,%d,%d,%d,%d,%d,%d,%d,%d,%d}" % (s[1], s[2], f[1], f[2], sl[1], sl[2], q(m["b"], 31),
                                                         s[0] | f[0] << 1 | sl[0] << 2, 1, 0))
        w("    {{%s}, %d, %d, %d, {%s}}," % (", ".join(ms), q(r["d"], 27), q(r["gout"], 24), r["n0"],
                                         ", ".join(str(q(x, 24)) for x in r["lvl"])))
    w("};")
    w("static const uint8_t RE_PLEN[RE_NKEY][127] = {   /* the hammer's contact, samples, per key and vel 1..127 */")
    for n in plen:
        w("    {%s}," % ",".join(map(str, n)))
    w("};")
    ns = sorted(shapes)
    w("#define RE_PL_MIN %du" % ns[0])
    w("#define RE_PL_MAX %du" % ns[-1])
    off, pos = [], 0
    body = []
    for nn in range(ns[0], ns[-1] + 1):
        off.append(pos)
        h = shapes.get(nn, M.half_sine(nn / FS))
        body.extend(q(x, 30) for x in h)
        pos += nn
    w("static const uint16_t RE_PSH_OFF[RE_PL_MAX - RE_PL_MIN + 1] = {%s};" % ",".join(map(str, off)))
    w("static const int32_t RE_PSH[%d] = {   /* half-sine shapes, area 1 (Q30), for each length */" % pos)
    for i in range(0, len(body), 12):
        w("    " + ",".join(map(str, body[i:i + 12])) + ",")
    w("};")
    w("static const int32_t RE_VPOW[128] = {   /* (vel / 127)^gamma, Q30 */")
    for i in range(0, 128, 8):
        w("    " + ",".join(str(q(x, 30)) for x in vpow[i:i + 8]) + ",")
    w("};")
    w("static const int32_t RE_LOG2[%d] = {   /* log2(1 + i / %d), Q30 */" % (LOG_N + 1, LOG_N))
    vals = [q(math.log2(1 + i / LOG_N), 30) for i in range(LOG_N + 1)]
    for i in range(0, len(vals), 8):
        w("    " + ",".join(map(str, vals[i:i + 8])) + ",")
    w("};")
    w("static const int32_t RE_EXP2[%d] = {   /* 2^(-i / %d), Q30 */" % (EXP_N + 1, EXP_N))
    vals = [q(2 ** (-i / EXP_N), 30) for i in range(EXP_N + 1)]
    for i in range(0, len(vals), 8):
        w("    " + ",".join(map(str, vals[i:i + 8])) + ",")
    w("};")
    w("#define RE_TANH_N %du" % TANH_N)
    w("static const int32_t RE_TANH[%d] = {   /* tanh(i * %g / %d), Q30 */" % (TANH_N + 1, TANH_MAX, TANH_N))
    vals = [q(math.tanh(i * TANH_MAX / TANH_N), 30) for i in range(TANH_N + 1)]
    for i in range(0, len(vals), 8):
        w("    " + ",".join(map(str, vals[i:i + 8])) + ",")
    w("};")
    txt = "\n".join(L) + "\n"
    return txt + "/* sha256 %s */\n" % hashlib.sha256(txt.encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("set", nargs="?", default=os.path.join(ROOT, "docs/model/data/wurli_v43.json"))
    ap.add_argument("--out", default=os.path.join(ROOT, "firmware/gen/reed_tables.h"))
    a = ap.parse_args()
    P = json.load(open(a.set))
    open(a.out, "w").write(header(P, os.path.relpath(a.set, ROOT)))
    print("wrote", a.out)


if __name__ == "__main__":
    main()
