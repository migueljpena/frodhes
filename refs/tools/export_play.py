"""Write the play page's parameter sets: each fit merged over reed_model.DEFAULT, the T60 as three points.

Usage: export_play.py NAME=FIT.json[+EXTRA.json[+EXTRA2.json]] ...
       (writes play/params/NAME.json and play/params/index.json, the sets in the order given: the first the default)

EXTRA (optional, one or more, later ones over earlier) adds keys over the fit (jitter or regulation, damp2, thump,
snoise, trem_ldr: reed_model.py); keys starting with "_" are notes, not read (but "_label" names the set). With
"jitter" or "regulation" the per-key table ("keys") is computed here, with "thump" its noise's level normalisation,
with "tc_per_keep_swing" the per-key swing ("u_key") that keeps tc_per's keys at their swing without it,
with "level_like": SET each key's sustain at that set's (an output gain per key, "g_key"),
with "reg_file" the REG knob's anchors (tools/reg_tables.py), with "trem_ldr" its drive compensation (reed_model.ldr_comp, for the page's two vibrato shapes), with
"out_gain": "match" the gain that sets the loudest chord's peak where bell3_v2's is: the page only reads numbers.
"""
import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, "/Users/migueljpena/Projects/frodhes/tools")
import reed_model as M  # noqa: E402

CHORD = (36, 40, 43, 48, 52, 55, 60, 67)   # the loudest chord, C2..G4 (out_gain "match")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out = os.path.join(HERE, "play", "params")
os.makedirs(out, exist_ok=True)
names = []
for arg in sys.argv[1:]:
    if "=" not in arg:            # an existing set, kept as it is (its place in the list)
        names.append(arg)
        continue
    name, path = arg.split("=", 1)
    path, *extras = path.split("+")
    P = dict(M.DEFAULT)
    fit = json.load(open(path))
    P.update({k: v for k, v in fit.items() if k != "loss"})
    X = {}
    for e in extras:
        X.update(json.load(open(e)))
    P.update({k: v for k, v in X.items() if not k.startswith("_")})
    if "t60_lo" not in P:
        P.update(t60_lo=M.t60_of(33, P), t60_mid=M.t60_of(64, P), t60_hi=M.t60_of(96, P))
    P.pop("t60_c", None)
    P.pop("t60_max", None)
    if "thump" in P:
        # the knock's raw peak (filtered noise, envelope, at ref 1 and 0 dB) set to 1
        T = P["thump"] = dict(P["thump"], norm=1.0, db=0.0)
        k = M.thump(60, {**P, "keys": {"ref": [1.0] * 64}}, int(0.25 * M.FS))
        T.update(norm=1.0 / M._peak(k), db=X["thump"]["db"])
    if P.pop("tc_per_keep_swing", False):
        # each key tc_per shortens keeps the first mode's swing (the sustain at 50 ms) it had without: its h2 and
        # level as before, only the click gone
        def h1(Q, m):
            x = M.reed(m, 100, int(0.2 * M.FS), Q)[int(0.05 * M.FS):][:4096]
            sp = np.abs(np.fft.rfft(x * np.hanning(len(x))))
            b = int(round(M.f0_of(m, Q) * len(x) / M.FS))
            return sp[b - 2:b + 3].max()
        Q = {k: v for k, v in P.items() if k != "tc_per"}
        P["u_key"] = [h1(Q, m) / h1(P, m) if M.tc_of(100, P, m) != M.tc_of(100, Q, m) else 1.0 for m in range(33, 97)]
    if "jitter" in P or "regulation" in P:
        P["keys"] = M.key_table(P)
    if "level_like" in P:
        # each key's sustain (vel 100, 150-350 ms, before the mix stage) at another set's: an output gain per key
        # ("g_key", after the pickup: no timbre change), then the table again (its knock and noise references)
        ref = json.load(open(os.path.join(out, P.pop("level_like") + ".json")))

        def sus(Q, m):
            y = M.voice(m, 100, 0.4, Q)[int(0.15 * M.FS):int(0.35 * M.FS)]
            return math.sqrt((y * y).mean())
        P["g_key"] = [sus(ref, m) / sus(P, m) for m in range(33, 97)]
        P["keys"] = M.key_table(P)
    if "reg_file" in P:
        # the REG knob's anchors (tools/reg_tables.py, made from this set: its last one is this set's own table)
        P["reg"] = json.load(open(os.path.join(HERE, P.pop("reg_file"))))
        P["reg"]["tables"][-1] = P["keys"]
    if "trem_ldr" in P:
        L = P["trem_ldr"]
        L["comp"] = {"%g" % h2: M.ldr_comp(h2, L["on"], L["off"]) for h2 in (M.TREM_H2, 0.0)}
    if P.get("out_gain") == "match":
        def chord_peak(Q):
            notes = [(0.0, m, 127, 1.5) for m in CHORD]
            return np.abs(M.render_notes(notes, 2.0, {**Q, "out_gain": 1.0})).max() * Q["drive"]
        ref = json.load(open(os.path.join(out, "bell3_v2.json")))
        P["out_gain"] = chord_peak(ref) / chord_peak(P)
    P["_name"] = f"{name} (error {fit.get('loss', float('nan')):.0f})" if "loss" in fit else name
    if "_label" in X:
        P["_name"] = X["_label"]
    json.dump(P, open(os.path.join(out, name + ".json"), "w"), indent=1)
    names.append(name)
json.dump(names, open(os.path.join(out, "index.json"), "w"))
print("sets:", names)
