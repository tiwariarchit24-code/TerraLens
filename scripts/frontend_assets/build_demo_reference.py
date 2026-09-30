"""Export static reference data for the frontend demo mode (no database needed):
concept registry (from config/concepts.yaml), AOIs and the deterministic tile grid
(real tile IDs, WorldCover composition where staged), and the model manifest with the
real SHA-256 of files present in models/."""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import rasterio
import yaml
from pyproj import Transformer

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from terralens.aoi import WC_CLASSES, _tile_split  # noqa: E402
from terralens.raster.grid import aoi_grid, tiles_for_aoi  # noqa: E402

GEN = ROOT / "frontend/src/data/demo/generated"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 22), b""):
            h.update(b)
    return h.hexdigest()


def main():
    GEN.mkdir(parents=True, exist_ok=True)
    (GEN / "concepts.json").write_text(json.dumps(yaml.safe_load((ROOT / "config/concepts.yaml").read_text())))
    (GEN / "thresholds.json").write_text(json.dumps(yaml.safe_load((ROOT / "config/thresholds/T-2026.09-01.yaml").read_text())))
    tf = Transformer.from_crs("EPSG:32644", "EPSG:4326", always_xy=True)
    aois, tiles = [], []
    for aid, a in yaml.safe_load((ROOT / "config/aois.yaml").read_text())["aois"].items():
        g = aoi_grid(a["bbox"])
        xmin, ymin, xmax, ymax = g["bounds"]
        ring = [list(tf.transform(x, y)) for x, y in [(xmin, ymin), (xmax, ymin), (xmax, ymax), (xmin, ymax), (xmin, ymin)]]
        aois.append({"id": aid, "name": a["name"], "purpose": a["purpose"], "crs": "EPSG:32644", "gsd": 10,
                     "widthPx": g["width"], "heightPx": g["height"], "areaKm2": round((xmax - xmin) * (ymax - ymin) / 1e6, 2),
                     "ring": ring})
        wcf = ROOT / "data/gis" / aid / "worldcover.tif"
        wc = rasterio.open(wcf).read(1) if wcf.exists() else None
        for t in tiles_for_aoi(g):
            lc = None
            if wc is not None:
                w = t.window
                sub = wc[w.row_off:w.row_off + 128, w.col_off:w.col_off + 128]
                v, c = np.unique(sub[sub > 0], return_counts=True)
                lc = {WC_CLASSES[int(k)]: round(float(n) / c.sum(), 3) for k, n in zip(v, c)} if c.sum() else None
            split, block = _tile_split(t.x0, t.y0, t.x1, t.y1)
            tiles.append({"id": t.tile_id, "aoi": aid, "col": t.col, "row": t.row, "split": split,
                          "ring": [[round(x, 6), round(y, 6)] for x, y in (tf.transform(px, py) for px, py in
                                   [(t.x0, t.y0), (t.x1, t.y0), (t.x1, t.y1), (t.x0, t.y1), (t.x0, t.y0)])],
                          "landcover": lc})
    (GEN / "aois.json").write_text(json.dumps(aois))
    (GEN / "tiles.json").write_text(json.dumps(tiles, separators=(",", ":")))
    models = [
        {"id": "enc-remoteclip-vitb32", "kind": "encoder", "name": "RemoteCLIP ViT-B/32", "license": "Apache-2.0",
         "source": "huggingface.co/chendelong/RemoteCLIP @ bf1d8a3c", "file": "models/encoders/remoteclip_vitb32/RemoteCLIP-ViT-B-32.pt",
         "embeddingDim": 512, "bands": "RGB", "gsdSuitability": "trained mostly on aerial / VHR imagery"},
        {"id": "enc-skyclip-vitb32-50pct", "kind": "encoder", "name": "SkyCLIP ViT-B/32 (50 %)", "license": "MIT",
         "source": "huggingface.co/torchgeo/vit_base_patch32_224_skyclip_50pct @ 7f82439a",
         "file": "models/encoders/skyclip_vitb32_50pct/vit_base_patch32_224_skyclip_50pct-5e3a3c68.pth",
         "embeddingDim": 512, "bands": "RGB", "gsdSuitability": "training images include Sentinel-2 and Landsat"},
        {"id": "enc-openclip-vitb32-laion2b", "kind": "encoder", "name": "OpenCLIP ViT-B/32 LAION-2B (baseline)", "license": "MIT",
         "source": "huggingface.co/laion/CLIP-ViT-B-32-laion2B-s34B-b79K @ 1a25a446",
         "file": "models/encoders/openclip_vitb32_laion2b/open_clip_model.safetensors",
         "embeddingDim": 512, "bands": "RGB", "gsdSuitability": "general-domain photographs (control for the bake-off)"},
    ]
    for m in models:
        p = ROOT / m["file"]
        m["sha256"] = sha256(p) if p.exists() else None
        m["sizeBytes"] = p.stat().st_size if p.exists() else None
    (GEN / "models.json").write_text(json.dumps(models, indent=1))
    print(len(aois), "aois", len(tiles), "tiles", [m["sha256"][:12] if m["sha256"] else None for m in models])


if __name__ == "__main__":
    main()
