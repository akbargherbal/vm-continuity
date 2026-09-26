import sys
from pathlib import Path

# The tool lives at the repo root (continuity.py) with tools/ beside it.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
