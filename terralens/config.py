"""Configuration loading. All tunables live in config/*.yaml, never in code."""
from __future__ import annotations

import functools
import hashlib
import json
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT / "config"


def _apply_offline_env() -> None:
    """Hard-disable every hub download path we know of. Called at import time of the
    app, worker and CLI, before any ML library is imported."""
    for k, v in {
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
        "HF_DATASETS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "DO_NOT_TRACK": "1",
        "TORCH_HOME": str(ROOT / "models" / ".torch"),
        "HF_HOME": str(ROOT / "models" / ".hf"),
        "GDAL_HTTP_TIMEOUT": "1",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".none",   # no /vsicurl reads at runtime
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "PYTORCH_ENABLE_MPS_FALLBACK": "1",
    }.items():
        os.environ.setdefault(k, v)


@functools.lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    cfg = yaml.safe_load((CONFIG_DIR / "terralens.yaml").read_text())
    try:
        from osgeo import gdal
        gdal.UseExceptions()
    except Exception:
        pass
    if cfg["runtime"].get("offline", True):
        _apply_offline_env()
    return cfg


def path(key: str) -> Path:
    p = ROOT / settings()["paths"][key]
    p.mkdir(parents=True, exist_ok=True)
    return p


@functools.lru_cache(maxsize=8)
def thresholds(set_id: str | None = None) -> dict[str, Any]:
    set_id = set_id or settings()["active_threshold_set"]
    safe = "".join(c for c in set_id if c.isalnum() or c in "-._")
    return yaml.safe_load((CONFIG_DIR / "thresholds" / f"{safe}.yaml").read_text())


@functools.lru_cache(maxsize=1)
def aois() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "aois.yaml").read_text())


@functools.lru_cache(maxsize=1)
def concepts_raw() -> dict[str, Any]:
    return yaml.safe_load((CONFIG_DIR / "concepts.yaml").read_text())


def config_hash() -> str:
    """Hash of every configuration file; recorded in provenance so a result can be tied
    to the exact configuration that produced it."""
    h = hashlib.sha256()
    for p in sorted(CONFIG_DIR.rglob("*.yaml")):
        h.update(p.relative_to(CONFIG_DIR).as_posix().encode())
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def runtime_state_path() -> Path:
    p = ROOT / "var" / "runtime_state.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def runtime_state() -> dict[str, Any]:
    p = runtime_state_path()
    return json.loads(p.read_text()) if p.exists() else {}


def set_runtime_state(**kw: Any) -> None:
    s = runtime_state()
    s.update(kw)
    runtime_state_path().write_text(json.dumps(s, indent=1, default=str))


def active_encoder_id() -> str | None:
    return runtime_state().get("active_encoder") or settings().get("active_encoder")


def device() -> str:
    want = settings()["runtime"].get("device", "auto")
    if want != "auto":
        return want
    try:
        import torch
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"
