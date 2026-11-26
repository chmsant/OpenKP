"""Paperless exporter. No live Paperless here.

The upload request shape matches a client already in use against a live
instance. The /api/tasks/ and paginated list response shapes below come
from the Paperless-NGX REST API docs and are NOT verified live from this
code. Treat a green run as "the logic is right given those shapes".
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from openkp import export_paperless as ep
from openkp.downloads_manifest import MANIFEST_FILENAME, record_download

TOKEN = "tok-not-real"


class FakePaperless:
    """httpx MockTransport handler with doc-shaped responses (unverified)."""

    def __init__(self, task_status: str = "SUCCESS", task_result: str = "Success"):
        self.uploads: list[httpx.Request] = []
        self.requests: list[httpx.Request] = []
        self.task_status = task_status
        self.task_result = task_result
        self.names = {
            "tags": {"medical": 7, "kaiser": 8},
            "correspondents": {"kaiser permanente": 3},
            "document_types": {"lab result": 5},
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["Authorization"] == f"Token {TOKEN}"
        path = request.url.path
        if path == "/api/documents/post_document/":
            self.uploads.append(request)
            return httpx.Response(200, text=json.dumps(f"task-{len(self.uploads)}"))
        if path == "/api/tasks/":
            return httpx.Response(200, json=[{
                "task_id": request.url.params["task_id"],
                "status": self.task_status,
                "result": self.task_result,
                "related_document": "101" if self.task_status == "SUCCESS" else None,
            }])
        for endpoint, table in self.names.items():
            if path == f"/api/{endpoint}/":
                name = request.url.params["name__iexact"].lower()
                results = [{"id": table[name], "name": name}] if name in table else []
                return httpx.Response(200, json={"count": len(results), "results": results})
        return httpx.Response(404)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("PAPERLESS_URL", "https://paperless.test")
    monkeypatch.setenv("PAPERLESS_TOKEN", TOKEN)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    return tmp_path


def _add(data_dir: Path, name: str, content: bytes, title: str = "", kind: str = "lab_pdf") -> Path:
    path = data_dir / "downloads" / name
    path.write_bytes(content)
    if title:
        record_download(path, kind=kind, source_id="x", title=title)
    return path


def _run(data_dir: Path, fake: FakePaperless, *extra: str) -> int:
    args = ep.build_parser().parse_args(["--data-dir", str(data_dir), "--wait", "0", *extra])
    return ep.run(args, transport=httpx.MockTransport(fake))


def _state(data_dir: Path) -> dict:
    return json.loads((data_dir / ep.STATE_FILENAME).read_text())


def test_manifest_records_sha_and_title(tmp_path: Path):
    path = tmp_path / "a.pdf"
    path.write_bytes(b"pdf")
    record_download(path, kind="lab_pdf", source_id="k1", title="CBC panel")
    line = json.loads((tmp_path / MANIFEST_FILENAME).read_text())
    assert line["title"] == "CBC panel"
    assert line["size_bytes"] == 3
    assert len(line["sha256"]) == 64


def test_candidates_skip_manifest_dotfiles_partials_and_empty(data_dir: Path):
    _add(data_dir, "good.pdf", b"1", title="Good one")
    _add(data_dir, ".hidden.pdf", b"2")
    _add(data_dir, "partial.pdf.part", b"3")
    _add(data_dir, "empty.pdf", b"")
    names = [c.path.name for c in ep.find_candidates(data_dir / "downloads", None)]
    assert names == ["good.pdf"]


def test_title_falls_back_to_filename(data_dir: Path):
    _add(data_dir, "Visit Summary.pdf", b"1")
    (c,) = ep.find_candidates(data_dir / "downloads", None)
    assert c.title == "Visit Summary"
    assert c.kind == "unknown"


def test_dry_run_without_credentials_makes_no_requests(data_dir: Path, monkeypatch, capsys):
    monkeypatch.delenv("PAPERLESS_TOKEN")
    _add(data_dir, "a.pdf", b"1", title="A")
    fake = FakePaperless()
    assert _run(data_dir, fake, "--dry-run") == 0
    assert fake.requests == []
    assert "a.pdf" in capsys.readouterr().out
    assert not (data_dir / ep.STATE_FILENAME).exists()


def test_dry_run_with_credentials_resolves_names_but_never_posts(data_dir: Path):
    _add(data_dir, "a.pdf", b"1", title="A")
    fake = FakePaperless()
    assert _run(data_dir, fake, "--dry-run", "--tag", "Medical") == 0
    assert fake.uploads == []
    assert all(r.method == "GET" for r in fake.requests)


def test_upload_sends_metadata_and_marks_done(data_dir: Path):
    _add(data_dir, "a.pdf", b"1", title="CBC panel")
    fake = FakePaperless()
    code = _run(data_dir, fake, "--tag", "Medical", "--tag", "Kaiser",
                "--correspondent", "Kaiser Permanente", "--document-type", "Lab Result")
    assert code == 0
    (req,) = fake.uploads
    body = req.content
    assert b'name="title"\r\n\r\nCBC panel' in body
    assert body.count(b'name="tags"') == 2
    assert b'name="correspondent"\r\n\r\n3' in body
    assert b'name="document_type"\r\n\r\n5' in body
    assert b'filename="a.pdf"' in body
    (entry,) = _state(data_dir).values()
    assert entry["status"] == "done"
    assert entry["document_id"] == 101


def test_second_run_skips_already_exported(data_dir: Path):
    _add(data_dir, "a.pdf", b"1", title="A")
    _run(data_dir, FakePaperless())
    fake2 = FakePaperless()
    assert _run(data_dir, fake2) == 0
    assert fake2.uploads == []


def test_same_bytes_under_two_names_uploads_once(data_dir: Path):
    _add(data_dir, "a.pdf", b"same", title="A")
    _add(data_dir, "a (1).pdf", b"same", title="A copy")
    fake = FakePaperless()
    _run(data_dir, fake)
    assert len(fake.uploads) == 1
    assert len(_state(data_dir)) == 1


def test_pending_task_stays_pending_then_finishes_next_run(data_dir: Path):
    _add(data_dir, "a.pdf", b"1", title="A")
    _run(data_dir, FakePaperless(task_status="STARTED"))
    (entry,) = _state(data_dir).values()
    assert entry["status"] == "pending"
    fake = FakePaperless(task_status="SUCCESS")
    _run(data_dir, fake)
    assert fake.uploads == []  # not re-uploaded
    (entry,) = _state(data_dir).values()
    assert entry["status"] == "done"


def test_duplicate_rejection_is_recorded_not_failed(data_dir: Path):
    _add(data_dir, "a.pdf", b"1", title="A")
    code = _run(data_dir, FakePaperless(task_status="FAILURE",
                                        task_result="Not consuming a.pdf: It is a duplicate of x"))
    (entry,) = _state(data_dir).values()
    assert entry["status"] == "duplicate"
    assert code == 0


def test_consume_failure_sets_exit_code_and_retry_flag_resends(data_dir: Path):
    _add(data_dir, "a.pdf", b"1", title="A")
    assert _run(data_dir, FakePaperless(task_status="FAILURE", task_result="boom")) == 1
    skip = FakePaperless()
    _run(data_dir, skip)
    assert skip.uploads == []  # failed files aren't retried by default
    retry = FakePaperless()
    assert _run(data_dir, retry, "--retry-failed") == 0
    assert len(retry.uploads) == 1


def test_unknown_tag_is_a_usage_error_and_uploads_nothing(data_dir: Path):
    _add(data_dir, "a.pdf", b"1", title="A")
    fake = FakePaperless()
    with pytest.raises(ep.UsageError, match="matched 0"):
        _run(data_dir, fake, "--tag", "Nope")
    assert fake.uploads == []


def test_http_error_body_is_never_printed(data_dir: Path, capsys):
    _add(data_dir, "a.pdf", b"1", title="Secret title")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="Secret title is invalid")

    args = ep.build_parser().parse_args(["--data-dir", str(data_dir), "--wait", "0"])
    assert ep.run(args, transport=httpx.MockTransport(handler)) == 1
    err = capsys.readouterr().err
    assert "HTTP 400" in err
    assert "invalid" not in err
    assert TOKEN not in err


def test_redirect_is_refused_not_followed(data_dir: Path):
    _add(data_dir, "a.pdf", b"1", title="A")
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "https://evil.test/steal"})

    args = ep.build_parser().parse_args(["--data-dir", str(data_dir), "--wait", "0"])
    assert ep.run(args, transport=httpx.MockTransport(handler)) == 1
    assert all("evil.test" not in u for u in seen)


def test_missing_credentials_is_usage_error(data_dir: Path, monkeypatch):
    monkeypatch.delenv("PAPERLESS_URL")
    assert ep.main(["--data-dir", str(data_dir)]) == ep.EXIT_USAGE


def test_plain_http_warns(data_dir: Path, monkeypatch, capsys):
    monkeypatch.setenv("PAPERLESS_URL", "http://paperless.test")
    _run(data_dir, FakePaperless())
    assert "plain http" in capsys.readouterr().err


@pytest.mark.parametrize("raw, expected", [('"abc-123"', "abc-123"), ("abc-123", "abc-123"),
                                           ('{"task_id": "t9"}', "t9")])
def test_parse_task_id(raw, expected):
    assert ep._parse_task_id(raw) == expected


def test_state_file_is_owner_only(data_dir: Path):
    _add(data_dir, "a.pdf", b"1", title="A")
    _run(data_dir, FakePaperless())
    mode = (data_dir / ep.STATE_FILENAME).stat().st_mode & 0o777
    assert mode == 0o600
