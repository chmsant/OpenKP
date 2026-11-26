"""save_download: private, atomic, never overwrites a different document."""

from __future__ import annotations

import json
from pathlib import Path

from openkp.downloads_manifest import MANIFEST_FILENAME, read_manifest, save_download


def _mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


def test_save_is_private(tmp_path: Path):
    out = tmp_path / "downloads"
    path = save_download(out, "a.pdf", b"x", kind="lab_pdf", source_id="k", title="CBC")
    assert path == out / "a.pdf"
    assert _mode(out) == 0o700
    assert _mode(path) == 0o600
    assert _mode(out / MANIFEST_FILENAME) == 0o600
    assert not list(out.glob(".*.openkp-tmp"))


def test_same_name_different_bytes_gets_a_new_name(tmp_path: Path):
    out = tmp_path / "downloads"
    first = save_download(out, "Lipid Panel.pdf", b"jan", kind="lab_pdf", title="Lipid Panel")
    second = save_download(out, "Lipid Panel.pdf", b"jun", kind="lab_pdf", title="Lipid Panel")
    third = save_download(out, "Lipid Panel.pdf", b"dec", kind="lab_pdf", title="Lipid Panel")
    assert first.read_bytes() == b"jan"
    assert second.name == "Lipid Panel (2).pdf" and second.read_bytes() == b"jun"
    assert third.name == "Lipid Panel (3).pdf"
    assert set(read_manifest(out)) == {first.name, second.name, third.name}


def test_same_name_same_bytes_reuses_the_file(tmp_path: Path):
    out = tmp_path / "downloads"
    a = save_download(out, "a.pdf", b"same", kind="lab_pdf")
    b = save_download(out, "a.pdf", b"same", kind="lab_pdf")
    assert a == b
    assert len(list(out.glob("*.pdf"))) == 1


def test_manifest_records_hash(tmp_path: Path):
    out = tmp_path / "downloads"
    save_download(out, "a.pdf", b"abc", kind="visit_avs_pdf", source_id="c1", title="AVS")
    (line,) = (out / MANIFEST_FILENAME).read_text().splitlines()
    entry = json.loads(line)
    assert entry["kind"] == "visit_avs_pdf"
    assert entry["sha256"] == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
