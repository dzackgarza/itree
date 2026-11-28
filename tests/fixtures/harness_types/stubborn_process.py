from __future__ import annotations

import signal
import subprocess
import sys
import time


def ignore_terminate() -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


ignore_terminate()
child = subprocess.Popen(
    [
        sys.executable,
        "-c",
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(120)",
    ],
    start_new_session=True,
)
print(child.pid, flush=True)
time.sleep(120)
