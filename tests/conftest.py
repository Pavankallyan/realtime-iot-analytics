"""pytest bootstrap: make the repo root importable so tests can do `import config`."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
