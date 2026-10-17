"""The sandbox rehearsal must stay green.

It is the only check that exercises the whole pipeline at once without asking anyone
to drop links or react by hand, so a regression here is a regression users would feel.
"""

import subprocess
import sys


def test_sandbox_rehearsal_passes():
    proc = subprocess.run(
        [sys.executable, "-m", "sim.sandbox_e2e"],
        capture_output=True, text=True, timeout=600,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-2000:]
    assert "sandbox rehearsal OK" in proc.stdout
