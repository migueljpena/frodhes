# SPDX-License-Identifier: GPL-3.0-only
"""The FM-1's memory budget on the host (tools/size_check.py: frodhes.c by clang for ARM, the image's code scaled to the
JieLi builds in the git log): the image keeps 10 % of the XIP slot free, RAM (.data + .bss), the pool and RAMTEXT
within their regions. The 200A had taken RAM to 111 KB of 96 (never built for the FM-1 then): this catches that."""
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "tools"))
import size_check as sc  # noqa: E402


def have_arm_clang():
    cc = os.environ.get("CLANG", "clang")
    if not shutil.which(cc):
        return False
    r = subprocess.run([cc, "--target=thumbv7em-none-eabi", "-x", "c", "-c", "-o", os.devnull, "-"],
                       input=b"int x;", capture_output=True)
    return r.returncode == 0


pytestmark = pytest.mark.skipif(not have_arm_clang(), reason="no clang with an ARM target")


@pytest.fixture(scope="module")
def est():
    return sc.budget(sc.compile_tree(os.path.join(sc.ROOT, "firmware")))[1]


def test_within_every_region(est):
    assert sc.check(est) == [], est


def test_the_check_fails_over_a_limit():
    e = {k: 0 for k in sc.LIMITS}
    assert sc.check(e) == []
    assert sc.check(dict(e, RAM=sc.LIMITS["RAM"] + 1))
    assert sc.check(dict(e, XIP=int(sc.LIMITS["XIP"] * 0.95)))          # under the slot, inside the margin
