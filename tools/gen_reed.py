#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Generate the reed engine's tables (the Wurlitzer 200A, firmware/src/reed.c) from a play set.

    tools/gen_reed.py [SET.json] [--out FILE]   -> firmware/gen/reed_tables.h
                                                   (SET: docs/model/data/wurli_v43.json, the play page's v4.3)

The set is the float model's (tools/reed_model.py; the play page in frodhes-refs follows it sample for sample). The
header holds what the knobs do not change, per key, and the knobs' anchors; reed.c's main loop (re_poll) works out
each key's resonator words, rest point and gain from them whenever a VOICE knob moves (reed_model.apply_knobs: TONE
BARK BELL HAMMER DECAY RELEASE NOISE TUNE REG), in integers. Logs are log2, Q26 unless said.

Per key (MIDI 33..96, the 200A's 64): the smooth T60 and rest point curves, the knock's share, g_key, the hammer's
contact at vel 1 and 127 (samples, log), and the strike level per contact length of the reference hammer (the
reed_model normalisation: 1 / its own 50 ms peak x the full strike's x the key's swing). REG: the regulation's
anchors (d, g, T60 factor: logs; cents, the action noise's gain). Globals: the set's scalars. Tables: log2, exp2,
the quarter sine, tanh, (vel / 127)^gamma.
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
LOG_N, EXP_N, TANH_N, SIN_N = 1024, 1024, 1024, 1024
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


def lg(x):
    return q(math.log2(x), 26)


def own_peak(f0, t60, P, pulse):
    """reed-worklet.js Voice.peak50: the 50 ms peak of the first two modes struck by pulse."""
    n = int(round(0.05 * FS))
    x = np.zeros(n)
    x[:len(pulse)] = pulse[:n]
    y = np.zeros(n)
    for f, t, w in ((f0, t60, 1.0), (f0 * P["r2"], t60 / P["q2"], P["w2"])):
        if f >= FS * 0.45:
            continue
        r, om = math.exp(-6.907755 / (t * FS)), 2 * math.pi * f / FS
        a1, a2, b = 2 * r * math.cos(om), r * r, w * r * math.sin(om)
        y1 = y2 = x1 = 0.0
        for i in range(n):
            yy = a1 * y1 - a2 * y2 + b * x1
            x1 = x[i]
            y2, y1 = y1, yy
            y[i] += yy
    return np.abs(y).max()


def tc_log(P, k, v):
    """log2 of the reference hammer's contact (samples) at vel v: tc_of without tc_per (reed.c caps it)."""
    t = M.interp_log(P["tc_soft"], P["tc_hard"], 33 + 63 * (v - 1) / 126.0)
    if "tc_top" in P and k > 72:
        t *= P["tc_top"] ** ((min(k, 96) - 72) / 24.0)
    return math.log2(t * FS)


def key_rows(P):
    rows = []
    for k in KEYS:
        f0, t60 = M.f0_of(k, P), M.t60_of(k, P)
        Q0 = {kk: vv for kk, vv in P.items() if kk != "keys"}
        t60s = M.t60_of(k, Q0)                       # the smooth curve (the regulation's factor apart)
        ds = M.interp_log(P["d"], P["d_hi"], k, P["d_mid"])
        hard = 1.0 / own_peak(f0, 1e12, P, M.hammer(127, P, k, ref=True))
        ns = sorted({max(int(round(M.tc_of(v, P, k, ref=True) * FS)), 2) for v in range(1, 128)})
        assert ns[-1] - ns[0] < NLV, (k, ns)
        lvl = [hard / own_peak(f0, t60, P, M.half_sine(nn / FS)) * M.swing_of(k, P) for nn in range(ns[0], ns[0] + NLV)]
        P0 = dict(P, w2=0.0)
        hard0 = 1.0 / own_peak(f0, 1e12, P0, M.hammer(127, P0, k, ref=True))
        lvl0 = [hard0 / own_peak(f0, t60, P0, M.half_sine(nn / FS)) * M.swing_of(k, P) for nn in range(ns[0], ns[0] + NLV)]
        rows.append({"lt60": lg(t60s), "ld": lg(ds), "wa": q(M.attack_w(k, P), 30), "lgk": lg(M.g_key(k, P)),
                     "lt1": q(tc_log(P, k, 1), 26), "lt127": q(tc_log(P, k, 127), 26), "n0": ns[0],
                     "lvl": [q(x, 24) for x in lvl], "lvl0": [q(x, 24) for x in lvl0]})
    return rows


def reg_rows(P):
    """REG's anchors (export_play: the last is the set's own table): per anchor and key log2 d, log2 g, log2 t60x,
    cents (Q16), sn (Q30)."""
    R = P["reg"]
    out = []
    for T in R["tables"]:
        out.append([(lg(T["d"][i]), lg(T["g"][i]), lg(T["t60"][i]), q(T["cents"][i], 16), q(T["sn"][i], 30))
                    for i in range(64)])
    return R["amt"], out


def table(w, name, ctype, vals, per=8):
    w("static const %s %s[%d] = {" % (ctype, name, len(vals)))
    for i in range(0, len(vals), per):
        w("    " + ",".join(map(str, vals[i:i + per])) + ",")
    w("};")


def header(P, src):
    rows = key_rows(P)
    amts, reg = reg_rows(P)
    S, D, A = P["snoise"], P["damp2"], P["attack"]
    L = []
    w = L.append
    w("/* SPDX-License-Identifier: GPL-3.0-only */")
    w("/* GENERATED by tools/gen_reed.py from %s: do not edit. The reed engine's tables (reed.c). */" % src)
    w("#define RE_KEY_LO 33u")
    w("#define RE_NKEY 64u")
    w("#define RE_NLV %du" % NLV)
    w("#define RE_NREG %du" % len(amts))
    w("/* the set's scalars: logs Q26 */")
    w("#define RE_L_OM69 %d          /* log2(2 pi 440 / fs): A4's angle per sample */" % lg(2 * math.pi * 440 / FS))
    w("#define RE_L_R2 %d            /* log2 r2: the second mode's ratio */" % lg(P["r2"]))
    w("#define RE_L_Q2 %d            /* log2 q2: its T60 under the first's */" % lg(P["q2"]))
    w("#define RE_W2 %d              /* its share, Q30 */" % q(P["w2"], 30))
    w("#define RE_L_ATT_T60 %d       /* log2 of the knock's T60 (s) */" % lg(A["t60"]))
    w("#define RE_L_T60K %d          /* log2(6.907755 / fs): a T60 (s) to its decay per sample */" % lg(6.907755 / FS))
    w("#define RE_L_2PI %d           /* log2(2 pi) */" % lg(2 * math.pi))
    w("#define RE_L_TCPER %d         /* log2 tc_per: the contact held under tc_per periods */" % lg(P["tc_per"]))
    w("#define RE_BELL_P %d          /* the bell's p, Q16 */" % q(P["bell_p"], 16))
    w("#define RE_DRIVE %d           /* the preamp's drive, Q16 */" % q(P["drive"], 16))
    w("#define RE_OUT_GAIN %d        /* the set's out_gain, Q16 */" % q(P.get("out_gain", 1.0), 16))
    w("#define RE_L_FAST %d          /* log2 of the damper's fast T60 (s) */" % lg(D["fast"]))
    w("#define RE_L_SLOW %d          /* and its slow one */" % lg(D["slow"]))
    w("#define RE_KNEE_K %d          /* the knee's samples per second of the fast T60, Q16 */" % q(D["knee_db"] / -60.0 * FS, 16))
    w("#define RE_BELL_MAX %d        /* the BELL knob's ping at 10, Q30 */" % q(M.BELL_MAX, 30))
    w("#define RE_L_PING %d          /* log2 of the ping's pulse (0.25 ms), samples */" % lg(2.5e-4 * FS))
    T = P["trem_ldr"]
    c0, c1 = T["comp"]["%g" % M.TREM_H2], T["comp"]["0"]
    w("/* the vibrato (reed_model.trem_stage with the set's trem_ldr): the dip's 2nd harmonic at SHAPE 0 (the 200A's),")
    w(" * its depth at full (dB), the LDR's lag (on, off: Q30 steps), its drive compensation at SHAPE 0 and 127 (the")
    w(" * smooth sine: C Q16, h2d Q16), the darkening low-pass's pole (Q30) */")
    w("#define RE_TR_H2 %d             /* Q16 */" % q(M.TREM_H2, 16))
    w("#define RE_TR_DB %d             /* Q16 */" % q(M.TREM_DB, 16))
    w("#define RE_TR_KON %d" % q(1 - math.exp(-1 / (T["on"] * FS)), 30))
    w("#define RE_TR_KOFF %d" % q(1 - math.exp(-1 / (T["off"] * FS)), 30))
    w("#define RE_TR_C0 %d" % q(c0[0], 16))
    w("#define RE_TR_HD0 %d" % q(c0[1], 16))
    w("#define RE_TR_C1 %d" % q(c1[0], 16))
    w("#define RE_TR_HD1 %d" % q(c1[1], 16))
    w("#define RE_TR_LPA %d" % q(math.exp(-2 * math.pi * M.TREM_LP_HZ / FS), 30))
    w("/* the action noise (reed_model.snoise): its band-pass (twice), rise, T60, velocities, length */")
    wn = 2 * math.pi * S["hz"] / FS
    al = math.sin(wn) / (2 * S["q"])
    w("#define RE_SN_B0 %d           /* Q30 */" % q(al / (1 + al), 30))
    w("#define RE_SN_A1 %d" % q(-2 * math.cos(wn) / (1 + al), 30))
    w("#define RE_SN_A2 %d" % q((1 - al) / (1 + al), 30))
    w("#define RE_SN_RISE %du        /* samples */" % int(round(S["rise"] * FS)))
    w("#define RE_SN_DEC %d          /* the T60's step, Q30 */" % q(math.exp(-6.907755 / (S["t60"] * FS)), 30))
    w("#define RE_SN_VEL0 %du" % S["vel0"])
    w("#define RE_SN_VEL1 %du" % S["vel1"])
    w("#define RE_SN_LEN %du" % int(round(0.1 * FS)))
    w("/* per key: lt60 (the smooth T60), ld (the smooth rest point), wa (the knock, Q30), lgk (g_key), lt1 / lt127 (the")
    w(" * reference hammer's contact in samples at vel 1 / 127), n0 (its shortest, rounded), lvl (Q24, per contact n0 ...), lvl0 (the same without the second mode) */")
    w("static const re_key_t RE_KEYS[RE_NKEY] = {")
    for r in rows:
        w("    {%d, %d, %d, %d, %d, %d, %d, {%s}, {%s}}," % (r["lt60"], r["ld"], r["wa"], r["lgk"], r["lt1"], r["lt127"],
                                                          r["n0"], ", ".join(map(str, r["lvl"])), ", ".join(map(str, r["lvl0"]))))
    w("};")
    w("static const uint16_t RE_REG_AMT[RE_NREG] = {%s};   /* the anchors' REG, Q15 */" % ", ".join(str(int(round(a * 32768))) for a in amts))
    w("static const re_reg_t RE_REG[RE_NREG][RE_NKEY] = {   /* ld, lg, lt60x (logs), cents (Q16), sn (Q30) */")
    for T in reg:
        w("    {" + ", ".join("{%d,%d,%d,%d,%d}" % t for t in T) + "},")
    w("};")
    table(w, "RE_VPOW", "int32_t", [q((v / 127.0) ** P["gamma"], 30) for v in range(128)])
    w("/* log2(1 + i / %d), 2^(-i / %d), sin(pi / 2 i / %d), tanh(i %g / %d): Q30 */" % (LOG_N, EXP_N, SIN_N, TANH_MAX, TANH_N))
    table(w, "RE_LOG2", "int32_t", [q(math.log2(1 + i / LOG_N), 30) for i in range(LOG_N + 1)])
    table(w, "RE_EXP2", "int32_t", [q(2 ** (-i / EXP_N), 30) for i in range(EXP_N + 1)])
    table(w, "RE_SIN", "int32_t", [q(math.sin(math.pi / 2 * i / SIN_N), 30) for i in range(SIN_N + 2)])
    w("#define RE_TANH_N %du" % TANH_N)
    table(w, "RE_TANH", "int32_t", [q(math.tanh(i * TANH_MAX / TANH_N), 30) for i in range(TANH_N + 1)])
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
