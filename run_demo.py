#!/usr/bin/env python3
"""One-command demo launcher.

Starts the simulator and the stream processor as background processes,
waits for data to accumulate, then opens the Streamlit dashboard.
Ctrl+C stops everything.

Run:
    python run_demo.py
"""

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> None:
    sim = subprocess.Popen([sys.executable, "-m", "src.simulator"], cwd=ROOT)
    time.sleep(1)  # let the socket server bind before the processor dials in
    proc = subprocess.Popen([sys.executable, "-m", "src.processor"], cwd=ROOT)
    print("[demo] simulator + processor running, warming up...")
    time.sleep(4)  # let windows fill so charts and z-scores are meaningful
    dash = subprocess.Popen(
        [sys.executable, "-m", "streamlit", "run", "src/dashboard.py",
         "--server.headless", "true"],
        cwd=ROOT)
    print("[demo] dashboard at http://localhost:8501")
    print("[demo] press Ctrl+C to stop everything")
    try:
        dash.wait()
    except KeyboardInterrupt:
        pass
    finally:
        for p in (dash, proc, sim):
            p.terminate()
        print("[demo] stopped")


if __name__ == "__main__":
    main()
