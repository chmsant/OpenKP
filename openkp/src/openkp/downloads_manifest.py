"""Sidecar manifest for files the download tools save.

Each successful download appends one JSON line to
`<downloads dir>/.manifest.jsonl`:

    {"file": "<name>", "sha256": "...", "size_bytes": 123, "kind": "lab_pdf",
     "source_id": "...", "title": "...", "downloaded_at": "<UTC ISO>"}

It exists so downstream tools (the optional Paperless exporter) can tell what
each file is without re-asking Kaiser. The manifest is local and lives next
to the files it describes, so it is exactly as sensitive as they are. The
title is PHI-bearing: never log it.

Best-effort. A manifest failure never fails the download.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MANIFEST_FILENAME = ".manifest.jsonl"


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
        with (path.parent / MANIFEST_FILENAME).open("a", encoding="utf-8") as fh:
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
