"""The REG knob's anchors for a play set: its "keys" table at REG amt = 0, 0.25, 0.5, 0.75 and 1 (REG 0-10 / 10).

amt 1 is the set's own table (the measured regulation: REG 10 sounds exactly as the set). amt 0: every key on the
smooth curves (d x 1, no level, T60 or tuning offsets). In between the level, T60 and tuning offsets scale with amt
(T60 as a power), and every key's d is searched for its bark: the bark (h2 at vel 120 - h2 at vel 30) at
amt x the way from the key's even bark (d x 1) to its full one (the set's d), its loud h2 likewise (weight 0.5),
the search held to the stretch of log d from 0 to the full one's (+-0.3): a key never jumps across the bell's h2
null on its way. Then key_table (reed_model) on each anchor's regulation.

Usage: reg_tables.py SET.json REGULATION.json OUT.json
  SET: the play set as export_play wrote it (bell42_v42.json); REGULATION: its regulation (fits/v42/regulation_v42.json,
  with _level_fix_v42 kept out of the scaling)
"""
import json
import math
import multiprocessing as mp
import sys

import numpy as np

sys.path.insert(0, "/Users/migueljpena/Projects/frodhes/tools")
sys.path.insert(0, "/Users/migueljpena/Projects/frodhes-refs/tools")
import fit_reed as F  # noqa: E402
import reed_model as M  # noqa: E402

KEYS = list(range(33, 97))
AMTS = (0.0, 0.25, 0.5, 0.75, 1.0)


def h2s(P, k, x):
    Q = dict(P, keys=dict(P["keys"], d=[x if kk == k else P["keys"]["d"][kk - 33] for kk in KEYS]))
    return [F.feats(M.render(k, v, 1.0, Q), k)["h2"] for v in (30, 120)]


def one_key(a):
    P, k = a
    L = math.log(P["keys"]["d"][k - 33])
    even, full = h2s(P, k, 1.0), h2s(P, k, math.exp(L))
    out = {}
    for amt in AMTS[1:-1]:
        tb = (even[1] - even[0]) + amt * ((full[1] - full[0]) - (even[1] - even[0]))
        tl = even[1] + amt * (full[1] - even[1])
        cache = {}

        def err(lx):
            if lx not in cache:
                s, l = h2s(P, k, math.exp(lx))
                cache[lx] = (l - s - tb) ** 2 + 0.5 * (l - tl) ** 2
            return cache[lx]
        lo, hi = min(0.0, L) - 0.3, max(0.0, L) + 0.3
        grid = list(np.linspace(lo, hi, 13))
        lx = min(grid, key=err)
        step = (hi - lo) / 12
        for _ in range(3):
            step /= 2
            lx = min((lx - step, lx, lx + step), key=err)
        out[amt] = lx
    return k, out


def main():
    P = json.load(open(sys.argv[1]))
    G = json.load(open(sys.argv[2]))
    reg = G["regulation"]
    fix = G.get("_level_fix_v42", [0.0] * 64)
    with mp.get_context("fork").Pool(4) as pool:
        res = dict(pool.map(one_key, [(P, k) for k in KEYS]))
    base = {k: v for k, v in P.items() if k not in ("keys", "reg", "_name", "_id")}
    tables = []
    for amt in AMTS:
        if amt == 1.0:
            tables.append(P["keys"])
            continue
        R = {"d_ln": [0.0 if amt == 0 else res[k][amt] for k in KEYS],
             "level_db": [amt * (l - f) + f for l, f in zip(reg["level_db"], fix)],
             "t60_x": [t ** amt for t in reg["t60_x"]],
             "cents": [amt * c for c in reg["cents"]]}
        tables.append(M.key_table({**base, "regulation": R}))
        print("amt", amt, "done", flush=True)
    json.dump({"amt": list(AMTS), "tables": tables}, open(sys.argv[3], "w"))
    for k in KEYS:
        print(k, " ".join("%.2f" % (res[k][a]) for a in AMTS[1:-1]), "full %.2f" % math.log(P["keys"]["d"][k - 33]))


if __name__ == "__main__":
    main()
