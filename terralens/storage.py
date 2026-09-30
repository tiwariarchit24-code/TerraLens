"""Safe file handling: path-traversal protection, safe names, safe archive extraction."""
from __future__ import annotations

import os
import re
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path

from .config import ROOT

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


class UnsafePathError(ValueError):
    pass


def safe_name(name: str, max_len: int = 120) -> str:
    """Reduce an arbitrary user-supplied name to a safe file-name component."""
    base = os.path.basename(name.replace("\\", "/"))
    base = _SAFE.sub("_", base).strip("._") or "file"
    return base[:max_len]


def resolve_within(base: Path, *parts: str) -> Path:
    """Join and resolve, refusing anything that escapes `base` (.., absolute paths,
    symlinks pointing outside)."""
    base = base.resolve()
    p = base.joinpath(*parts).resolve()
    if p != base and base not in p.parents:
        raise UnsafePathError(f"path escapes {base.name}: {'/'.join(parts)}")
    return p


def project_relative(p: Path) -> str:
    try:
        return p.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


def make_read_only(p: Path) -> None:
    """Raw imagery is never overwritten: drop write bits after registration."""
    for f in [p] if p.is_file() else [x for x in p.rglob("*") if x.is_file()]:
        mode = f.stat().st_mode
        f.chmod(mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def safe_extract(archive: Path, dest: Path, max_files: int = 2000, max_bytes: int = 4 << 30) -> list[Path]:
    """Extract zip/tar refusing absolute paths, '..', links and zip bombs."""
    dest.mkdir(parents=True, exist_ok=True)
    out: list[Path] = []
    total = 0
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            infos = z.infolist()
            if len(infos) > max_files:
                raise UnsafePathError("archive has too many members")
            for i in infos:
                if i.is_dir():
                    continue
                target = resolve_within(dest, i.filename)
                total += i.file_size
                if total > max_bytes:
                    raise UnsafePathError("archive expands beyond size limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                with z.open(i) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                out.append(target)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as t:
            members = t.getmembers()
            if len(members) > max_files:
                raise UnsafePathError("archive has too many members")
            for m in members:
                if m.isdir():
                    continue
                if not m.isfile():
                    raise UnsafePathError(f"archive member is a link or device: {m.name}")
                target = resolve_within(dest, m.name)
                total += m.size
                if total > max_bytes:
                    raise UnsafePathError("archive expands beyond size limit")
                target.parent.mkdir(parents=True, exist_ok=True)
                src = t.extractfile(m)
                assert src is not None
                with open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                out.append(target)
    else:
        raise UnsafePathError("not a zip or tar archive")
    return out
