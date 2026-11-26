"""Sidecar manifest for files the download tools save.

Each successful download appends one JSON line to
`<downloads dir>/.manifest.jsonl`:

    {"file": "<name>", "sha256": "...", "size_bytes": 123, "kind": "lab_pdf",
     "source_id": "...", "title": "...", "downloaded_at": "<UTC ISO>"}

It exists so downstream tools (the optional Paperless exporter) can tell what
each file is without re-asking Kaiser. The manifest is local and lives next
to the files it describes, so it is exactly as sensitive as they are. The
title is PHI-bearing: never log it.

Files, the manifest, and the downloads dir are created owner-only (0600 /
0700). Saves are atomic (dot-prefixed temp file, then rename) so a reader
never sees a half-written PDF, and never overwrite a different document that
happens to share a display name.

The manifest write is best-effort and never fails a download.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = ".manifest.jsonl"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def record_download(
    path: Path,
    *,
    kind: str,
    source_id: str = "",
    title: str = "",
) -> None:
    """Append a manifest entry for a file that was just written to disk."""
    try:
        entry = {
            "file": path.name,
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
            "kind": kind,
            "source_id": source_id,
            "title": title,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
        }
        fd = os.open(path.parent / MANIFEST_FILENAME, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception as exc:
        logger.warning("Download manifest write failed (%s)", type(exc).__name__)


def read_manifest(download_dir: Path) -> dict[str, dict[str, Any]]:
    """Latest manifest entry per file name. Unreadable lines are skipped."""
    entries: dict[str, dict[str, Any]] = {}
    path = download_dir / MANIFEST_FILENAME
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict) and isinstance(entry.get("file"), str):
            entries[entry["file"]] = entry
    return entries


def _ensure_private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError as exc:
        logger.warning("Could not restrict downloads dir permissions (%s)", type(exc).__name__)


def _free_path(out_dir: Path, filename: str, digest: str) -> tuple[Path, bool]:
    """Where to save `filename`. Returns (path, already_there).

    Same name and same bytes: reuse it. Same name, different bytes (two labs
    with one display name): pick `name (2).ext`, `name (3).ext`, and so on.
    """
    candidate = out_dir / filename
    stem, suffix = candidate.stem, candidate.suffix
    n = 1
    while candidate.exists():
        if candidate.is_file() and not candidate.is_symlink() and sha256_file(candidate) == digest:
            return candidate, True
        n += 1
        candidate = out_dir / f"{stem} ({n}){suffix}"
    return candidate, False


def save_download(
    out_dir: Path,
    filename: str,
    data: bytes,
    *,
    kind: str,
    source_id: str = "",
    title: str = "",
) -> Path:
    """Write a downloaded file privately and atomically, then record it.

    Returns the final path, which may differ from `out_dir / filename` when
    a different file already holds that name.
    """
    _ensure_private_dir(out_dir)
    digest = sha256_bytes(data)
    path, already_there = _free_path(out_dir, filename, digest)
    if not already_there:
        tmp = out_dir / f".{path.name}.openkp-tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        tmp.replace(path)
    record_download(path, kind=kind, source_id=source_id, title=title)
    return path
