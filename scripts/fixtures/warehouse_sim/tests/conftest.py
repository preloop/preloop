import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
for p in (REPO_ROOT, REPO_ROOT / "backend"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))
