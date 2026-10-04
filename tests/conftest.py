"""Make tests/ importable (e2e_harness) and the repo root importable (fleet_policy)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for path in (HERE, HERE.parent):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
