#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""The complete device scan of a connected FM-1, both engines, in one command, and one summary.

The rule: always record the headphone jack and USB, but the JACK decides (the user plays through the jack; USB is
for development). Runs, in order, each as a subprocess with its own log under build/hil/full/<timestamp>/:

  info          tools/fm1_install.py --info            (which firmware the FM-1 reports; installs nothing)
  cpu-*         tools/hil_cpu.py, then --engine reed   (2 runs each; the worst counts)
  classic       tools/hil_scan.py                      (every default suite; USB only -> report.json / .txt)
  jack-*        tools/hil_jack.py, then --engine reed  (hiss / SNR / headroom through the interface -> jack*.json)
  seq           tools/hil_seq.py                       (only with --seq)

Each tool's JSON / report is copied into the run directory once it has written it, and SUMMARY.md is built from
those: per suite ok / FAIL counts, every failing row classified
  JACK FAIL   real: the jack fails it
  UNDECIDED   failing with no jack value to decide on (the classic suites are USB only; a USB-only measure)
  USB-ONLY    the jack passes, or a USB capture artefact (the USB stream slipped, USB-only clicks): tooling
  INFO        an information row, a USB / jack MISMATCH where both pass, a known stale classic limit (AGENTS.md)
the CPU worst per engine with its margins to 4500 / 4800 / 5000 us, hiss / SNR per engine.

It never installs firmware. It takes the device lock (tools/lock.py DEVICE: <root>/build/hil/DEVICE.lockdir; a
caller holding it, FRODHES_LOCK_DEVICE, passes at once; waits while another session holds it; released in finally).

  tools/full_scan.py [--seq] [--skip cpu,jack] [--jack-dev M6] [--jack-ch 5] [--root MAIN_REPO] [--wait-lock 3600]
  tools/full_scan.py --from build/hil/full/<ts>         rebuild SUMMARY.md from that run's logs / JSON (no device)
"""
import argparse
import datetime
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import lock  # noqa: E402

TOOLS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TOOLS)
HIL = os.path.join(REPO, "build", "hil")

CPU_TARGET, CPU_GATE, CPU_SHED, CPU_BLOCK = 4500, 4800, 5000, 5805
# AGENTS.md's "known stale limits" (types, effects, bypass, fx_all, fx_toggle, presets) used to be information here.
# Since 2026-10-24 none is: types / effects / bypass / fx_toggle / presets pass in every report since 2026-10-18, and
# fx_all's limits follow what each effect does (hil_scan.py FXALL_EXPECT), so a classic FAIL is a finding again
# (fx_all's wah rows were fx.c's stale wah depth law, a defect fixed 2026-10-24). Names added here are reported INFO, not FAIL
CLASSIC_STALE = set()
CLASSES = ("JACK FAIL", "UNDECIDED", "USB-ONLY", "INFO")


# ---------------------------------------------------------------- classification
CLASSIC_LINE = re.compile(r"^(ok|FAIL)\s+(\S+)\s+(.*?)\s+\((\d+) s\)\s*$")


def parse_classic(text):
    """report.txt / the classic log -> [(ok, name, msg)]"""
    return [(m.group(1) == "ok", m.group(2), m.group(3)) for m in map(CLASSIC_LINE.match, text.splitlines()) if m]


def classify_classic(name, msg):
    if name in CLASSIC_STALE:
        return "INFO", "known stale limit (AGENTS.md); USB only"
    if "slip" in msg.lower():
        return "USB-ONLY", "USB stream slip"
    return "UNDECIDED", "USB only (the classic suites do not record the jack): re-check on the jack"


CPU_WORST = re.compile(r"^worst (\d+) us of")
CPU_SHEDS = re.compile(r"voices shed (\d+)")


def parse_cpu(text):
    """an hil_cpu log -> (worst us or None, voices shed in total)"""
    worst, shed = None, 0
    for ln in text.splitlines():
        m = CPU_WORST.match(ln.strip())
        if m:
            worst = int(m.group(1))
        shed += sum(int(x) for x in CPU_SHEDS.findall(ln))
    return worst, shed


# ---------------------------------------------------------------- summary
def _read(path):
    try:
        return open(path, errors="replace").read()
    except OSError:
        return None


def _json(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return None


def build_summary(d):
    """SUMMARY.md from the run directory d; returns (text, counts {class: n}, step errors)"""
    meta = _json(os.path.join(d, "steps.json")) or {}
    steps = meta.get("steps", [])
    L = [f"# Full device scan {os.path.basename(os.path.normpath(d))}", "",
         "The rule: always record the jack and USB, but the JACK decides. JACK FAIL = real; UNDECIDED = failing with "
         "no jack value; USB-ONLY = the jack passes or a USB capture artefact (tooling, not product); INFO.", ""]
    info = _read(os.path.join(d, "info.log"))
    fw = [ln for ln in (info or "").splitlines() if ln.strip() and not ln.startswith("$ ")]
    L += [f"- FM-1 reports: {fw[-1].strip() if fw else '(no --info output)'}"]
    if meta.get("git"):
        L += [f"- tools at: {meta['git']}"]
    L += [""]
    errors = [s for s in steps if s.get("rc") not in (0, None) and s["name"] != "classic"]
    if steps:
        L += ["## Steps", "", "| step | exit | time | log |", "|---|---|---|---|"]
        L += [f"| {s['name']} | {s.get('rc')} | {s.get('secs', 0):.0f} s | {s['name']}.log |" for s in steps]
        L += [""]

    # CPU
    L += ["## CPU (worst audio block of 2 runs)", "",
          f"| engine | runs (us) | worst | to {CPU_TARGET} | to {CPU_GATE} | to {CPU_SHED} (shedder) | voices shed |",
          "|---|---|---|---|---|---|---|"]
    for eng in ("rhodes", "reed"):
        runs = []
        for f in sorted(os.listdir(d)):
            if f.startswith(f"cpu-{eng}-") and f.endswith(".log"):
                runs.append(parse_cpu(_read(os.path.join(d, f)) or ""))
        ws = [w for w, _ in runs if w is not None]
        if not ws:
            L.append(f"| {eng} | (no result) | | | | | |")
            continue
        w = max(ws)
        L.append(f"| {eng} | {', '.join(str(x) for x in ws)} | **{w}** | {CPU_TARGET - w:+d} | {CPU_GATE - w:+d} | "
                 f"{CPU_SHED - w:+d} | {sum(s for _, s in runs)} |")
    L += [""]

    # hiss / SNR
    L += ["## Hiss / SNR (tools/hil_jack.py, re the interface's full scale)", "",
          "| engine | jack floor (muted) dB | hiss 8-20k jack dB | hiss USB dBFS | jack adds dB | loud jack peak dB | SNR dB |",
          "|---|---|---|---|---|---|---|"]
    for eng, fn in (("rhodes", "jack.json"), ("reed", "jack-reed.json")):
        j = _json(os.path.join(d, fn))
        if not j:
            L.append(f"| {eng} | (no result) | | | | | |")
            continue
        g = lambda *k: _dig(j, *k)  # noqa: E731
        L.append(f"| {eng} | {_f(g('silence', 'jack_db'))} | {_f(g('hiss', 'jack_hf_db'))} | {_f(g('hiss', 'usb_hf_db'))} "
                 f"| {_f(g('hiss_added_db'))} | {_f(g('loud', 'jack_peak_db'))} | **{_f(g('snr_db'))}** |")
    L += [""]

    # suites
    rows = []                                      # (class, suite, check, setting, usb, jack, crit, reason)
    L += ["## Suites", "", "| suite | checks | ok | FAIL / flagged |", "|---|---|---|---|"]
    ctext = _read(os.path.join(d, "report.txt")) or _read(os.path.join(d, "classic.log"))
    if ctext:
        cl = parse_classic(ctext)
        for ok, name, msg in cl:
            L.append(f"| classic {name} | 1 | {int(ok)} | {int(not ok)} |")
            if not ok:
                c, why = classify_classic(name, msg)
                rows.append((c, f"classic {name}", msg[:160], "", "(USB)", "-", "", why))
    else:
        L.append("| classic | (no report) | | |")
    seq = _read(os.path.join(d, "seq.log"))
    if seq is not None:
        tail = [ln for ln in seq.splitlines() if ln.strip() and not ln.startswith("$ ")][-3:]
        L += ["", "Sequencer (hil_seq, read by eye): " + " / ".join(x.strip() for x in tail)]
    L += [""]

    counts = {c: sum(r[0] == c for r in rows) for c in CLASSES}
    L += ["## Verdict", "", " ".join(f"{c}: {counts[c]}" for c in CLASSES) +
          (f"; step errors: {', '.join(s['name'] for s in errors)}" if errors else ""), ""]
    for c in CLASSES:
        sel = [r for r in rows if r[0] == c]
        if not sel:
            continue
        L += [f"## {c} ({len(sel)})", "", "| suite | check | setting | USB | jack | criterion | why |",
              "|---|---|---|---|---|---|---|"]
        L += ["| " + " | ".join(_cell(x) for x in r[1:]) + " |" for r in sel]
        L += [""]
    return "\n".join(L), counts, errors


def _dig(j, *k):
    for x in k:
        if not isinstance(j, dict) or x not in j:
            return None
        j = j[x]
    return j


def _f(x):
    return f"{x:.1f}" if isinstance(x, (int, float)) else "-"


def _cell(x):
    return str(x).replace("|", "\\|").replace("\n", " ")


# ---------------------------------------------------------------- the device lock
def main_repo(start):
    try:
        g = subprocess.run(["git", "-C", start, "rev-parse", "--path-format=absolute", "--git-common-dir"],
                           capture_output=True, text=True, timeout=10).stdout.strip()
        if g:
            return os.path.dirname(g)
    except (OSError, subprocess.SubprocessError):
        pass
    return start


def DeviceLock(root, who, wait_s, poll_s=10.0):
    """build/hil/DEVICE.lockdir through tools/lock.py: the holder's token (FRODHES_LOCK_DEVICE) passes at once, a
    dead holder is reaped, a stale one reported; released at the end only when still ours"""
    return lock.Lock("DEVICE", root=root, who=who, wait_s=wait_s, poll_s=poll_s)


# ---------------------------------------------------------------- running
def run_step(d, name, argv, timeout, copies=()):
    """one tool as a subprocess, its output to <d>/<name>.log and the console; copies [(build/hil file, as)]"""
    before = {src: _mtime(os.path.join(HIL, src)) for src, _ in copies}
    log = os.path.join(d, name + ".log")
    print(f"\n=== {name}: {' '.join(argv)}", flush=True)
    t0, rc = time.time(), None
    with open(log, "w") as f:
        f.write("$ " + " ".join(argv) + "\n")
        f.flush()
        try:
            p = subprocess.Popen(argv, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            dog = threading.Timer(timeout, p.kill)        # a hung tool (no output) is killed too
            dog.start()
            try:
                for ln in p.stdout:
                    f.write(ln)
                    sys.stdout.write(ln)
                rc = p.wait(timeout=30)
                if not dog.is_alive() and time.time() - t0 >= timeout:
                    f.write(f"\n[full_scan] killed after {timeout} s\n")
            except BaseException:
                p.kill()
                raise
            finally:
                dog.cancel()
        except OSError as e:
            f.write(f"[full_scan] could not start: {e}\n")
            rc = -1
    for src, dst in copies:
        sp = os.path.join(HIL, src)
        if _mtime(sp) and _mtime(sp) != before[src]:
            shutil.copy2(sp, os.path.join(d, dst))
        else:
            print(f"[full_scan] {name}: build/hil/{src} was not (re)written", flush=True)
    secs = time.time() - t0
    print(f"=== {name}: exit {rc}, {secs:.0f} s", flush=True)
    return dict(name=name, argv=argv, rc=rc, secs=secs)


def _mtime(p):
    try:
        return os.stat(p).st_mtime_ns
    except OSError:
        return None


def plan(a):
    py, t = sys.executable, lambda n: os.path.join(TOOLS, n)
    S = [("info", [py, t("fm1_install.py"), "--info"], 60, ())]
    for eng, extra in (("rhodes", []), ("reed", ["--engine", "reed"])):
        for i in (1, 2):
            S.append((f"cpu-{eng}-{i}", [py, t("hil_cpu.py")] + extra, 900, ()))
    S.append(("classic", [py, t("hil_scan.py")], 3600, (("report.json", "report.json"), ("report.txt", "report.txt"))))
    S.append(("jack-rhodes", [py, t("hil_jack.py"), "--dev", a.jack_dev, "--ch", str(a.jack_ch)], 900,
              (("jack.json", "jack.json"),)))
    S.append(("jack-reed", [py, t("hil_jack.py"), "--dev", a.jack_dev, "--ch", str(a.jack_ch), "--engine", "reed"],
              900, (("jack-reed.json", "jack-reed.json"),)))
    if a.seq:
        S.append(("seq", [py, t("hil_seq.py")], 300, ()))
    skip = set(filter(None, (a.skip or "").split(",")))
    return [s for s in S if not any(s[0] == k or s[0].startswith(k + "-") for k in skip)]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--from", "--offline", dest="offline", metavar="DIR",
                    help="rebuild SUMMARY.md from an earlier run directory; no device")
    ap.add_argument("--root", help="the main repo holding build/hil/DEVICE.lockdir (default: this repo's main checkout)")
    ap.add_argument("--wait-lock", type=int, default=3600, help="seconds to wait for the device lock")
    ap.add_argument("--jack-dev", default="M6")
    ap.add_argument("--jack-ch", type=int, default=5)
    ap.add_argument("--seq", action="store_true", help="also tools/hil_seq.py (clears the FM-1's pattern)")
    ap.add_argument("--skip", help="steps to leave out, comma-separated: info, cpu, cpu-reed, classic, jack ...")
    ap.add_argument("--who", default=f"full_scan@{socket.gethostname()}", help="the lock owner's name")
    a = ap.parse_args()
    if a.offline:
        d = a.offline
    else:
        d = os.path.join(HIL, "full", datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
        os.makedirs(d, exist_ok=True)
        git = subprocess.run(["git", "-C", REPO, "log", "-1", "--format=%h %s"], capture_output=True, text=True).stdout.strip()
        steps = []
        meta = dict(git=git, steps=steps)
        with DeviceLock(a.root or main_repo(REPO), a.who, a.wait_lock):
            for name, argv, timeout, copies in plan(a):
                steps.append(run_step(d, name, argv, timeout, copies))
                json.dump(meta, open(os.path.join(d, "steps.json"), "w"), indent=1)
                if name == "info" and steps[-1]["rc"] != 0:
                    print("[full_scan] fm1_install --info found no FM-1 running Frodhes: stopping", flush=True)
                    break
    text, counts, errors = build_summary(d)
    out = os.path.join(d, "SUMMARY.md")
    open(out, "w").write(text + "\n")
    print("\n" + " ".join(f"{c}: {counts[c]}" for c in CLASSES) + (f"; step errors: {len(errors)}" if errors else ""))
    print(f"summary: {os.path.relpath(out, REPO) if out.startswith(REPO) else out}")
    return 1 if counts["JACK FAIL"] or errors else 0


if __name__ == "__main__":
    sys.exit(main())
