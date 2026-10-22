from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is in sys.path when invoked via installed console script
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

"""FabricAgent Command Line Interface."""

__version__ = "0.2.0"
