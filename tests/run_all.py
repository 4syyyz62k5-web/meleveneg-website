#!/usr/bin/env python3
"""Runs every suite and prints one total.

    python3 tests/run_all.py
    python3 tests/run_all.py -v          # stream each suite's output

Each suite runs in its OWN process, which is not tidiness: they share a
module-level limiter and a module-level app, and app.py registers its rate
limits at import. Two suites in one process would double every registration
and halve every limit.

The gallery suite runs TWICE -- once as this machine is, and once with
pillow-heif hidden at import. The second is the shape production has: Render
runs Python 3.14 and pillow-heif publishes no cp314 wheel, so HEIC takes the
refusal path there and the conversion path here. Only running the local shape
would leave the branch production actually uses untested.
"""

import subprocess
import sys
from pathlib import Path

TESTS = Path(__file__).resolve().parent
SUITES = ["test_resale.py", "test_spam.py", "test_gallery.py"]

# Hides pillow_heif at import, the way Render's environment does.
HIDE_HEIF = """
import builtins
_real = builtins.__import__
def _blocked(name, *a, **k):
    if name == "pillow_heif" or name.startswith("pillow_heif."):
        raise ImportError("hidden: production has no cp314 wheel")
    return _real(name, *a, **k)
builtins.__import__ = _blocked
exec(open({path!r}).read())
"""


def run(label, argv, verbose):
    proc = subprocess.run(argv, cwd=TESTS, capture_output=True, text=True)
    out = proc.stdout
    if verbose:
        print(out)
    passed = failed = 0
    for line in out.splitlines():
        if ": " in line and "passed," in line:
            try:
                nums = line.split(":")[-1]
                passed = int(nums.split("passed")[0].strip())
                failed = int(nums.split(",")[1].split("failed")[0].strip())
            except (ValueError, IndexError):
                pass
    status = "ok" if proc.returncode == 0 else "FAILED"
    print(f"  {label:<34} {passed:>4} passed  {failed:>3} failed   {status}")
    if proc.returncode != 0 and not verbose:
        for line in out.splitlines():
            if line.startswith("  FAIL") or "Traceback" in line or "Error" in line:
                print(f"      {line.strip()}")
        if proc.stderr.strip():
            print("      stderr:", proc.stderr.strip().splitlines()[-1])
    return passed, failed, proc.returncode


def main():
    verbose = "-v" in sys.argv
    print("meleveneg test suites\n")
    total_p = total_f = 0
    rcs = []

    for suite in SUITES:
        p, f, rc = run(suite, [sys.executable, suite], verbose)
        total_p += p
        total_f += f
        rcs.append(rc)

    p, f, rc = run("test_gallery.py (heif hidden)",
                   [sys.executable, "-c",
                    HIDE_HEIF.format(path=str(TESTS / "test_gallery.py"))],
                   verbose)
    total_p += p
    total_f += f
    rcs.append(rc)

    print(f"\n  {'TOTAL':<34} {total_p:>4} passed  {total_f:>3} failed")
    return 1 if any(rcs) else 0


if __name__ == "__main__":
    sys.exit(main())
