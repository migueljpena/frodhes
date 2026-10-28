#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Two firmware/gen table headers, A (the committed one) and B (a candidate), compared word by word.

  tools/table_ab.py [A] [B]           A defaults to firmware/gen/rhodes_tables.h; B defaults to A
  tools/table_ab.py perturb SRC OUT TABLE:WORD:DELTA [...]   a copy of SRC with flat words changed (tests)

Every `static const` array (and every #define) parsed to its flat list of integers; per table the words changed,
max |B - A| in LSB, max |B - A| / |A|, and which keys (tables indexed by key). The sound half (host renders of
both headers against audibility thresholds) was for the bar engine's bars_tables.h and is gone with it; parse() and
MODE_F also serve tests/py/test_rh_coef.py.
"""
import argparse
import os
import re
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "firmware" / "gen"
KEY_LO = 21                                                 # the first key of a table indexed by key (A0)
MODE_F = ["sh", "ch", "th", "d", "drel", "cr", "b"]          # rh_mode_t (rhodes.c)
NOTE_N = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]


# ---------------------------------------------------------------- the header
def _mask_comments(s):
    return re.sub(r"/\*.*?\*/|//[^\n]*", lambda m: " " * len(m.group(0)), s, flags=re.S)


NUM = re.compile(r"(?<![\w.])-?(?:0[xX][0-9A-Fa-f]+|\d+)[uUlL]*(?![\w.])")
ARR = re.compile(r"static\s+const\s+(\w+)\s+(\w+)((?:\s*\[[^\]]*\])+)\s*=\s*\{", re.S)


def _int(t):
    return int(t.rstrip("uUlL"), 0)


def parse(path):
    """{name: dict(type, dims, vals, spans)}, {define: value text}; spans: (start, end) of each number in the file"""
    src = Path(path).read_text()
    m = _mask_comments(src)
    defs = dict(re.findall(r"^#define\s+(\w+)\s+([^\s/]+)", m, re.M))
    tabs = {}
    for a in ARR.finditer(m):
        i, depth = a.end(), 1
        while depth:                                       # the matching closing brace
            depth += {"{": 1, "}": -1}.get(m[i], 0)
            i += 1
        body0 = a.end()
        nums = list(NUM.finditer(m, body0, i - 1))
        tabs[a.group(2)] = dict(type=a.group(1), dims=re.findall(r"\[([^\]]*)\]", a.group(3)),
                                vals=[_int(n.group(0)) for n in nums], spans=[n.span() for n in nums])
    return tabs, defs


def _eval_dim(expr, defs):
    e = expr
    for _ in range(4):
        e = re.sub(r"[A-Za-z_]\w*", lambda m: defs.get(m.group(0), m.group(0)), e)
    try:
        return int(eval(re.sub(r"[uU]\b", "", e), {}, {}))
    except Exception:
        return None


def note_name(n):
    return f"{NOTE_N[n % 12]}{n // 12 - 1}"


def diff_tables(pa, pb):
    """per table: words changed, max |diff| LSB, max relative, and the changes with their labels"""
    ta, da = parse(pa)
    tb, db_ = parse(pb)
    defs = dict(parse(GEN / "rhodes_tables.h")[1])
    defs.update(da)
    res = []
    for name in sorted(set(ta) | set(tb)):
        A, B = ta.get(name), tb.get(name)
        if A is None or B is None or len(A["vals"]) != len(B["vals"]):
            res.append(dict(name=name, n=len((A or B)["vals"]), changed=-1, note="missing or a different size"))
            continue
        a, b = np.array(A["vals"], dtype=np.int64), np.array(B["vals"], dtype=np.int64)
        d = b - a
        idx = np.nonzero(d)[0]
        dims = [_eval_dim(x, defs) for x in A["dims"]]
        row = dict(name=name, n=len(a), changed=len(idx), maxlsb=int(np.abs(d).max()) if len(idx) else 0,
                   maxrel=float(np.max(np.abs(d[idx]) / np.maximum(np.abs(a[idx]), 1))) if len(idx) else 0.0,
                   items=[])
        for i in idx:
            lab, ks = _label(name, int(i), dims)
            row["items"].append(dict(i=int(i), a=int(a[i]), b=int(b[i]), d=int(d[i]), label=lab, keys=ks))
        row["keys"] = sorted({k for it in row["items"] for k in it["keys"]})
        res.append(row)
    dd = [(k, da.get(k), db_.get(k)) for k in sorted(set(da) | set(db_)) if da.get(k) != db_.get(k)]
    return res, dd


def _label(name, i, dims, lo=KEY_LO):
    """the word's label (name[i][j]..) and, for a table indexed by key (88 rows, first or second dimension), its key"""
    if dims and None not in dims and int(np.prod(dims)) > 0:
        sub, r = [], i
        for n in reversed(dims):
            r, q = divmod(r, n)
            sub.append(q)
        sub = sub[::-1]
        lab = name + "".join(f"[{s}]" for s in sub)
        if dims[0] == 88:
            return lab, [lo + sub[0]]
        if len(dims) > 1 and dims[1] == 88:               # per type: [N][88]...
            return lab, [lo + sub[1]]
        return lab, []
    return f"{name}#{i}", []


def perturb(src, out, specs):
    """write a copy of src with TABLE:WORD:DELTA applied (WORD: the flat index)"""
    text = Path(src).read_text()
    tabs, _ = parse(src)
    edits = []
    for s in specs:
        name, w, dl = s.split(":")
        t = tabs[name]
        a, b = t["spans"][int(w)]
        edits.append((a, b, str(t["vals"][int(w)] + int(dl))))
    for a, b, v in sorted(edits, reverse=True):
        text = text[:a] + v + text[b:]
    Path(out).write_text(text)


def main_ab(pa, pb):
    tabs, ddefs = diff_tables(pa, pb)
    print(f"A: {pa}\nB: {pb}\n")
    print("| table | words | changed | max abs LSB | max rel | keys |\n| --- | --- | --- | --- | --- | --- |")
    for t in tabs:
        if t["changed"] < 0:
            print(f"| {t['name']} | {t['n']} | {t['note']} | | | |")
            continue
        ks = ", ".join(note_name(k) for k in t["keys"][:12]) + (" ..." if len(t["keys"]) > 12 else "")
        print(f"| {t['name']} | {t['n']} | {t['changed']} | {t['maxlsb']} | {t['maxrel']:.2e} | {ks} |")
    for k, a, b in ddefs:
        print(f"\n#define {k}: A {a}, B {b}")
    big = sorted((it for t in tabs if t["changed"] > 0 for it in t["items"]), key=lambda it: -abs(it["d"]))
    if big:
        print("\nThe largest changes:\n\n| word | A | B | B - A | rel |\n| --- | --- | --- | --- | --- |")
        for it in big[:15]:
            print(f"| {it['label']} | {it['a']} | {it['b']} | {it['d']:+d} | {abs(it['d']) / max(abs(it['a']), 1):.2e} |")
    nchg = sum(t["changed"] for t in tabs if t["changed"] > 0)
    print(f"\ntables: {nchg} words changed in {sum(1 for t in tabs if t['changed'] > 0)} tables"
          + (f", largest {big[0]['label']} {big[0]['d']:+d} LSB" if big else ""))
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "perturb":
        ap = argparse.ArgumentParser(prog="table_ab.py perturb")
        ap.add_argument("src")
        ap.add_argument("out")
        ap.add_argument("spec", nargs="+", help="TABLE:WORD:DELTA (WORD: the flat index of the integer)")
        a = ap.parse_args(sys.argv[2:])
        perturb(a.src, a.out, a.spec)
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a", nargs="?", default=str(GEN / "rhodes_tables.h"))
    ap.add_argument("b", nargs="?")
    a = ap.parse_args()
    return main_ab(os.path.abspath(a.a), os.path.abspath(a.b or a.a))


if __name__ == "__main__":
    sys.exit(main())
