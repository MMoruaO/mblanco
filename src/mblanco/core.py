from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath


def safe_path(root: Path, relative: str) -> Path:
    """Resolve a POSIX relative path and reject traversal and symlink escapes."""
    rel = PurePosixPath(relative)
    if not relative or rel.is_absolute() or any(p in ("", ".", "..") for p in rel.parts):
        raise ValueError("ruta relativa inválida")
    candidate = root.joinpath(*rel.parts)
    resolved_root = root.resolve()
    resolved = candidate.resolve(strict=False)
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("la ruta sale de compartido/")
    return candidate


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest(root: Path) -> dict[str, dict[str, int | str]]:
    """List regular files under root; never follow symlink directories/files."""
    root.mkdir(parents=True, exist_ok=True)
    entries: dict[str, dict[str, int | str]] = {}
    for path in root.rglob("*"):
        if (
            path.is_symlink()
            or not path.is_file()
            or path.name.startswith(".mblanco-")
            or path.name.endswith(".mblanco-tmp")
        ):
            continue
        rel = path.relative_to(root).as_posix()
        try:
            safe_path(root, rel)
            stat = path.stat()
        except (OSError, ValueError):
            continue
        entries[rel] = {"size": stat.st_size, "sha256": file_hash(path)}
    return entries
