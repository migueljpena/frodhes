#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""The FM-1's memory budget on the host, without JieLi's toolchain: firmware/src/frodhes.c (the unity build, as
tools/build.py builds it) compiled by clang for a 32-bit ARM (Thumb-2, -Os) with the pi32v2 inline asm emptied, and its
ELF sections summed into the FM-1's regions (firmware/app.ld):

  XIP      .text + .rodata + .ram_text + .data (the image)          581564 B
  RAM      .data + .bss                                                96 KiB
  POOL     .pool*                                                     336 KiB
  RAMTEXT  .ram_text                                                   24 KiB

The data sizes (RAM, POOL, .rodata) are the C's own, the same on any 32-bit target; the code is not, so its bytes are
scaled by CODE, the ratio measured against JieLi builds whose sizes are in the git log. Fails (exit 1) when a region is
over its limit, or the image leaves less than MARGIN of the slot free: tests/py/test_size_budget.py runs it, so a
change that grows the firmware shows up before any device time.

  python3 tools/size_check.py              the budget of the working tree
  python3 tools/size_check.py --rev REV    of a commit (git archive)
  python3 tools/size_check.py --top N      also the N largest RAM and POOL symbols' sections"""
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIMITS = {"XIP": 0x8DFBC, "RAM": 96 * 1024, "POOL": 0x54000, "RAMTEXT": 0x6000}
MARGIN = 0.10           # the image keeps 10 % of the slot free (the code's size is scaled, not measured)
# JieLi builds against this proxy (the git log's "image .. B", "RAM .. B", "RAMTEXT .. of 24576"), the image as the
# constants (.rodata + .data, exact) + CODE x the ARM code (.text + .ram_text):
#   36efc32 image 412200 B: constants 335725 + code 96394 -> 0.793; RAM 52480 B, proxy 52323 (+0.3 %)
#   1415937 image 579684 B: constants 452164 + code 162338 -> 0.786
#   9952600 RAMTEXT 20900 B: proxy 26742 -> 0.782
CODE = 0.80             # the largest of the three, rounded up
RAM_CAL = 1.01          # the proxy's RAM came out 0.3 % under the JieLi build's: 1 % over it
CFLAGS = ["--target=thumbv7em-none-eabi", "-mcpu=cortex-m4", "-mfloat-abi=soft", "-Os", "-ffreestanding",
          "-fno-builtin", "-ffunction-sections", "-fdata-sections", "-fno-common", "-w", '-DAPP_VER="dev"']
ASM = re.compile(r"__asm__\s+(?:volatile|__volatile__)\s*\(|__asm__\s*\(|\basm\s+volatile\s*\(")


def sections(path):
    """[(name, size)] of an ELF32 little-endian object's sections"""
    with open(path, "rb") as f:
        b = f.read()
    shoff, = struct.unpack_from("<I", b, 0x20)
    shentsize, shnum, shstrndx = struct.unpack_from("<HHH", b, 0x2E)
    hdr = [struct.unpack_from("<IIIIIIIIII", b, shoff + i * shentsize) for i in range(shnum)]
    stro = hdr[shstrndx][4]
    out = []
    for h in hdr:
        name = b[stro + h[0]:b.index(b"\0", stro + h[0])].decode()
        out.append((name, h[5]))
    return out


def region(name):
    for pre, reg in ((".pool", "POOL"), (".ram_text", "RAMTEXT"), (".text", "text"), (".rodata", "rodata"),
                     (".data", "data"), (".sdata", "data"), (".bss", "bss"), (".sbss", "bss")):
        if name.startswith(pre):
            return reg
    return None


def compile_tree(fw):
    """fw: a firmware/ directory. Returns the sections of frodhes.c's object"""
    with tempfile.TemporaryDirectory() as t:
        for sub in ("src", "gen", "hal"):
            shutil.copytree(os.path.join(fw, sub), os.path.join(t, sub))
            for dp, _, fs in os.walk(os.path.join(t, sub)):
                for fn in fs:
                    if fn.endswith((".c", ".h")):
                        p = os.path.join(dp, fn)
                        with open(p, encoding="utf-8", errors="surrogateescape") as f:
                            s = f.read()
                        if ASM.search(s):
                            with open(p, "w", encoding="utf-8", errors="surrogateescape") as f:
                                f.write(ASM.sub("FAKEASM(", s))
        fake = os.path.join(t, "fakeasm.h")
        with open(fake, "w") as f:
            f.write("#define FAKEASM(...) ((void)0)\n")
        obj = os.path.join(t, "frodhes.o")
        cc = os.environ.get("CLANG", "clang")
        subprocess.run([cc, *CFLAGS, "-include", fake, "-I" + os.path.join(t, "src"), "-I" + os.path.join(t, "gen"),
                        "-I" + os.path.join(t, "hal"), "-c", os.path.join(t, "src", "frodhes.c"), "-o", obj],
                       check=True)
        return sections(obj)


def budget(secs):
    s = {}
    for name, size in secs:
        r = region(name)
        if r:
            s[r] = s.get(r, 0) + size
    g = lambda k: s.get(k, 0)
    proxy = {"XIP": g("text") + g("rodata") + g("RAMTEXT") + g("data"), "RAM": g("data") + g("bss"),
             "POOL": g("POOL"), "RAMTEXT": g("RAMTEXT")}
    est = dict(proxy, RAM=round(RAM_CAL * proxy["RAM"]), XIP=round(g("rodata") + g("data") + CODE * (g("text") + g("RAMTEXT"))),
               RAMTEXT=round(CODE * g("RAMTEXT")))
    return proxy, est, s


def check(est):
    errs = []
    for k, lim in LIMITS.items():
        if est[k] > lim:
            errs.append(f"{k} {est[k]} B > {lim} B")
    if est["XIP"] > LIMITS["XIP"] * (1 - MARGIN):
        errs.append(f"XIP {est['XIP']} B leaves under {MARGIN:.0%} of {LIMITS['XIP']} B free")
    return errs


def main(argv):
    rev = argv[argv.index("--rev") + 1] if "--rev" in argv else None
    top = int(argv[argv.index("--top") + 1]) if "--top" in argv else 0
    if rev:
        with tempfile.TemporaryDirectory() as t:
            tar = subprocess.run(["git", "-C", ROOT, "archive", rev, "firmware"], check=True, capture_output=True)
            subprocess.run(["tar", "-x", "-C", t], input=tar.stdout, check=True)
            secs = compile_tree(os.path.join(t, "firmware"))
    else:
        secs = compile_tree(os.path.join(ROOT, "firmware"))
    proxy, est, s = budget(secs)
    for k, lim in LIMITS.items():
        print(f"{k:8s} {est[k]:7d} of {lim:7d} B ({est[k] / lim:6.1%}), {lim - est[k]:7d} free  (proxy {proxy[k]})")
    print(f"  code {s.get('text', 0)} B (ARM, before CODE), constants {s.get('rodata', 0)} B")
    if top:
        for reg in (("bss", "data"), ("POOL",)):
            big = sorted(((sz, n) for n, sz in secs if region(n) in reg), reverse=True)[:top]
            print("  " + "/".join(reg) + ": " + ", ".join(f"{n} {sz}" for sz, n in big))
    errs = check(est)
    for e in errs:
        print("FAIL " + e)
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
