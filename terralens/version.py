"""Versions stamped on every artefact. Bump when behaviour changes."""
from __future__ import annotations

import hashlib
from pathlib import Path

__version__ = "0.9.0"
PIPELINE_VERSION = "pp-1.0.0"      # standardise + quality + co-registration + normalisation
GRID_VERSION = "g1-32644-10m-128s64"
CHANGE_ENGINE_VERSION = "ce-1.0.0"
PLANNER_VERSION = "planner-1.0.0"
EXPORT_VERSION = "export-1.0.0"


def code_version() -> str:
    """No git history is used (the workspace is not a repository), so the code version
    is a content hash of the Python package sources."""
    root = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        h.update(p.relative_to(root).as_posix().encode())
        h.update(p.read_bytes())
    return f"{__version__}+{h.hexdigest()[:12]}"
