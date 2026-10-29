"""Summarize notes.csv files per model and register (medians of the clean notes).

Usage: summarize.py NAME=CSV[:EXCLUDE,EXCLUDE] ...
A clean note: harmonicity >= 0.8, at least 0.6 s long, within 40 cents of a semitone.
"""
import csv
import statistics as st
import sys

REG = [("low  <C3", 0, 48), ("mid C3-C5", 48, 72), ("high >=C5", 72, 200)]
COLS = ["level_db", "attack_ms", "even_minus_odd_db", "dec_h1_dbs", "dec_h2_dbs", "dec_h3_dbs",
        "h2_db", "h3_db", "h4_db", "h5_db", "h6_db", "h8_db"]


def rows(path, exclude):
    with open(path) as fh:
        for r in csv.DictReader(fh):
            if r["file"].split(".")[0] in exclude:
                continue
            try:
                if float(r["harmonicity"]) >= 0.8 and float(r["dur_s"]) >= 0.6 and abs(float(r["cents_off"])) <= 40:
                    yield r
            except ValueError:
                continue


def med(rs, c):
    v = [float(r[c]) for r in rs if r[c] not in ("", "nan")]
    return st.median(v) if v else float("nan")


def main():
    print(f"{'model':8} {'register':10} {'n':>4} " + " ".join(f"{c.replace('_db', '').replace('_dbs', '/s'):>10}" for c in COLS))
    for arg in sys.argv[1:]:
        name, rest = arg.split("=", 1)
        path, _, ex = rest.partition(":")
        rs = list(rows(path, set(filter(None, ex.split(",")))))
        for label, lo, hi in REG:
            sub = [r for r in rs if lo <= round(float(r["midi"])) < hi]
            # louder half vs softer half: how the tone changes with the strike (the bark)
            print(f"{name:8} {label:10} {len(sub):4} " + " ".join(f"{med(sub, c):10.1f}" for c in COLS))
            if len(sub) >= 20:
                cut = st.median(float(r["level_db"]) for r in sub)
                for tag, part in (("  loud", [r for r in sub if float(r["level_db"]) >= cut]),
                                  ("  soft", [r for r in sub if float(r["level_db"]) < cut])):
                    print(f"{'':8} {tag:10} {len(part):4} " + " ".join(f"{med(part, c):10.1f}" for c in COLS))


if __name__ == "__main__":
    main()
