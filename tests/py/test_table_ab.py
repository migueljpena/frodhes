# SPDX-License-Identifier: GPL-3.0-only
"""tools/table_ab.py: the header parses to the sizes the firmware declares, an identical copy diffs to nothing, and
perturbed words are found with their labels (and their key, for a table indexed by key)."""
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import table_ab as ta  # noqa: E402

H = os.path.join(ROOT, "firmware", "gen", "rhodes_tables.h")


def test_parse_sizes():
    tabs, defs = ta.parse(H)
    assert len(tabs["RH_PRATE_B"]["vals"]) == 88 and len(tabs["RH_VPOS_I"]["vals"]) == 128
    assert tabs["RH_PRATE"]["dims"][0] == "RH_NKEY"


def test_identical_and_perturbed(tmp_path):
    res, dd = ta.diff_tables(H, H)
    assert not dd and all(t["changed"] == 0 for t in res)
    b = tmp_path / "rhodes_tables.h"
    c4 = 60 - 21
    ta.perturb(H, b, [f"RH_PRATE_B:{c4}:+5", "RH_VPOS_I:10:+1"])
    res, _ = ta.diff_tables(H, str(b))
    t = {r["name"]: r for r in res}
    assert t["RH_PRATE_B"]["changed"] == 1 and t["RH_PRATE_B"]["maxlsb"] == 5
    assert [it["label"] for it in t["RH_PRATE_B"]["items"]] == [f"RH_PRATE_B[{c4}]"] and t["RH_PRATE_B"]["keys"] == [60]
    assert t["RH_VPOS_I"]["changed"] == 1 and not t["RH_VPOS_I"]["keys"]
    assert sum(r["changed"] for r in res) == 2
