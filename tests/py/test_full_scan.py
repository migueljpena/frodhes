# SPDX-License-Identifier: GPL-3.0-only
"""tools/full_scan.py: the CPU / classic parsers and their classification, the --from summary and the device lock."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import full_scan as fs  # noqa: E402

TOOL = os.path.join(os.path.dirname(__file__), "..", "..", "tools", "full_scan.py")


def test_classic_and_cpu_parsers():
    txt = ("ok    silence       peak 0 LSB, 0 non-zero samples  (3 s)\n"
           "FAIL  keyboard      76 keys: worst tuning 23.0 cents  (78 s)\n"
           "FAIL  effects       problems: ['wah']  (57 s)\n    drive 20   USB -0.8 LU\n")
    cl = fs.parse_classic(txt)
    assert [(ok, n) for ok, n, _ in cl] == [(True, "silence"), (False, "keyboard"), (False, "effects")]
    assert fs.classify_classic("effects", "")[0] == "UNDECIDED"   # (no "known stale" classic suite since 2026-10-24)
    assert fs.classify_classic("keyboard", "")[0] == "UNDECIDED"
    log = ("8-note chord   attack  3600 us (62.0 %)   voices shed 1\n"
           "\nworst 3620 us of 5805 us per block (62 %); shedder at 5000 us\n")
    assert fs.parse_cpu(log) == (3620, 1)


def test_offline_summary(tmp_path):
    d = tmp_path
    (d / "report.txt").write_text("ok    silence       peak 0 LSB  (3 s)\nFAIL  presets       spread 8.2 LU  (90 s)\n")
    (d / "jack.json").write_text(json.dumps(dict(silence=dict(jack_db=-90.2), hiss=dict(jack_hf_db=-99.7, usb_hf_db=-97.0),
                                                 hiss_added_db=10.0, loud=dict(jack_peak_db=-14.9), snr_db=73.9)))
    for i, w in ((1, 3600), (2, 3700)):
        (d / f"cpu-rhodes-{i}.log").write_text(f"worst {w} us of 5805 us per block\n")
    (d / "cpu-reed-1.log").write_text("worst 4600 us of 5805 us per block\n")
    p = subprocess.run([sys.executable, TOOL, "--from", str(d)], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr                         # no JACK FAIL, no step error
    s = (d / "SUMMARY.md").read_text()
    assert "JACK FAIL: 0 UNDECIDED: 1 USB-ONLY: 0 INFO: 0" in s       # (presets' FAIL: a finding, not stale)
    assert "| rhodes | 3600, 3700 | **3700** | +800 | +1100 | +1300 | 0 |" in s
    assert "| reed | 4600 | **4600** | -100 |" in s
    assert "**73.9**" in s
    assert "| classic presets | 1 | 0 | 1 |" in s


def test_device_lock(tmp_path):
    lock = tmp_path / "build" / "hil" / "DEVICE.lockdir"
    with fs.DeviceLock(str(tmp_path), "test", 0):
        assert (lock / "owner").read_text().startswith("test ")
        with fs.DeviceLock(str(tmp_path), "child", 0) as c:                 # the holder's own tool: goes on
            assert c.inherited
        tok = os.environ.pop("FRODHES_LOCK_DEVICE")                         # another session: no token
        with pytest.raises(SystemExit):                                     # held: no wait -> gives up
            with fs.DeviceLock(str(tmp_path), "other", 0):
                pass
        os.environ["FRODHES_LOCK_DEVICE"] = tok
        assert lock.exists()                                                # the loser left it alone
    assert not lock.exists()
    try:                                                                    # released after an exception too
        with fs.DeviceLock(str(tmp_path), "test", 0):
            raise RuntimeError
    except RuntimeError:
        pass
    assert not lock.exists()
