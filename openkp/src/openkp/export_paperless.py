"""Export OpenKP downloads to a Paperless-NGX instance.

A standalone, opt-in CLI. It is deliberately not an MCP tool: the OpenKP
server only ever talks to Kaiser, and this is the one place that talks to
anything else, only when you run it.

    openkp-export-paperless --dry-run
    openkp-export-paperless --tag Medical --correspondent "Kaiser Permanente"

Reads `<data dir>/downloads/` and the `.manifest.jsonl` the download tools
write there. Uploads each new file with `POST /api/documents/post_document/`
and remembers what it sent in `<data dir>/paperless-export.json`, keyed by
SHA-256, so re-runs only send new files.

Paperless consumes uploads asynchronously. An upload is recorded as
`pending` with its task id, then checked against `/api/tasks/` until it
succeeds or fails, in this run (up to `--wait` seconds) or the next one. A
file is only `done` once Paperless says the task succeeded. Paperless rejects
duplicates by checksum, and those are recorded as `duplicate`, not retried.

Config (environment only, so `op run` can inject the token):
    PAPERLESS_URL    e.g. https://paperless.example.org
    PAPERLESS_TOKEN  API token. Never printed or logged.

Response shapes: the upload request (multipart `document`, `title`, repeated
`tags`, `correspondent`, `document_type`) matches a client that is in use
against a live instance. The `/api/tasks/?task_id=` list shape and the
paginated `{"results": [...]}` list endpoints come from the Paperless-NGX
REST API docs and are NOT yet verified against a live instance from this
code. The test fixtures mirror those docs and say so.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from openkp.downloads_manifest import MANIFEST_FILENAME, read_manifest, sha256_file

STATE_FILENAME = "paperless-export.json"
# A pending upload whose task Paperless no longer knows about is retried after this.
PENDING_TTL_SECONDS = 60 * 60
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}
SKIP_SUFFIXES = (".tmp", ".part", ".crdownload")
TERMINAL_STATUSES = {"done", "duplicate", "failed"}

EXIT_OK = 0
EXIT_FAILURES = 1
EXIT_USAGE = 2


class UsageError(Exception):
    """Bad config or arguments. Message is safe to print."""


@dataclass
class Candidate:
    path: Path
    sha256: str
    title: str
    kind: str


# --- local state -------------------------------------------------------------


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        raise UsageError(f"{path} is not valid JSON. Move it aside to start over.") from None
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _age_seconds(entry: dict[str, Any]) -> float:
    try:
        then = datetime.fromisoformat(str(entry.get("updated_at")))
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - then).total_seconds()


def _label(c: Candidate, show_names: bool) -> str:
    """What to print for a file. Names come from Kaiser titles (PHI), so by
    default print a hash prefix and the kind instead."""
    return f"{c.path.name}  [{c.kind}]" if show_names else f"{c.sha256[:12]}  [{c.kind}]"


# --- finding files -----------------------------------------------------------


def find_candidates(
    download_dir: Path, since: datetime | None, include_unlisted: bool = False
) -> list[Candidate]:
    """Exportable files in the downloads dir, oldest first.

    By default only files an OpenKP download tool recorded in the manifest,
    with bytes that still match the recorded hash. Anything else dropped in
    the folder is ignored unless `include_unlisted`. Symlinks are always
    skipped so a link can't pull in some other file on the machine.
    """
    if not download_dir.is_dir():
        return []
    manifest = read_manifest(download_dir)
    out: list[Candidate] = []
    for path in sorted(download_dir.iterdir(), key=lambda p: p.lstat().st_mtime):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name.startswith(".") or path.name == MANIFEST_FILENAME:
            continue
        if path.name.endswith(SKIP_SUFFIXES) or path.stat().st_size == 0:
            continue
        if since is not None:
            modified = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            if modified < since:
                continue
        entry = manifest.get(path.name, {})
        digest = sha256_file(path)
        if not include_unlisted and entry.get("sha256") != digest:
            continue
        title = str(entry.get("title") or "").strip() or path.stem
        out.append(
            Candidate(
                path=path,
                sha256=digest,
                title=title,
                kind=str(entry.get("kind") or "unknown"),
            )
        )
    return out


# --- Paperless API -----------------------------------------------------------


class Paperless:
    def __init__(self, base_url: str, token: str, transport: httpx.BaseTransport | None = None):
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Token {token}", "Accept": "application/json"},
            # Never follow redirects: a redirect could carry the token header
            # to a different host.
            follow_redirects=False,
            timeout=60.0,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def resolve_id(self, endpoint: str, name: str) -> int:
        """Exact, case-insensitive name -> id. Never creates anything."""
        response = self._client.get(f"/api/{endpoint}/", params={"name__iexact": name})
        _check(response, f"looking up {endpoint}")
        results = response.json().get("results", [])
        if len(results) != 1:
            raise UsageError(
                f"{endpoint[:-1].replace('_', ' ')} {name!r} matched {len(results)} entries in "
                "Paperless. Create it there first, or fix the spelling."
            )
        return int(results[0]["id"])

    def upload(self, candidate: Candidate, fields: dict[str, Any]) -> str:
        data: dict[str, Any] = {"title": candidate.title, **fields}
        with candidate.path.open("rb") as fh:
            response = self._client.post(
                "/api/documents/post_document/",
                data=data,
                files={"document": (candidate.path.name, fh)},
            )
        _check(response, "uploading")
        return _parse_task_id(response.text)

    def task_status(self, task_id: str) -> tuple[str, int | None, str]:
        """(status, document_id, result) for a consume task.

        Shape per Paperless-NGX docs (unverified here): a list of task
        objects with `status` in PENDING / STARTED / SUCCESS / FAILURE, a
        `related_document` id on success, and a `result` message.
        """
        response = self._client.get("/api/tasks/", params={"task_id": task_id})
        _check(response, "checking a task")
        payload = response.json()
        tasks = payload if isinstance(payload, list) else payload.get("results", [])
        if not tasks:
            return "UNKNOWN", None, ""
        task = tasks[0]
        doc = task.get("related_document")
        try:
            doc_id = int(doc) if doc not in (None, "") else None
        except (TypeError, ValueError):
            doc_id = None
        return str(task.get("status") or "PENDING").upper(), doc_id, str(task.get("result") or "")


def _check(response: httpx.Response, action: str) -> None:
    """Raise with a status code only. Paperless error bodies can echo
    filenames and titles, so they're never printed."""
    if response.is_redirect:
        raise RuntimeError(
            f"{action}: Paperless answered with a redirect (HTTP {response.status_code}). "
            "Check PAPERLESS_URL, including http vs https and the trailing path."
        )
    if response.status_code >= 400:
        raise RuntimeError(f"{action}: HTTP {response.status_code}")


def _parse_task_id(text: str) -> str:
    """post_document returns the task UUID as a JSON string. Accept a bare
    string too."""
    text = text.strip()
    try:
        value = json.loads(text)
    except ValueError:
        value = text
    if isinstance(value, dict):
        value = value.get("task_id", "")
    task_id = str(value).strip().strip('"')
    if not task_id:
        raise RuntimeError("uploading: Paperless returned no task id")
    return task_id


# --- the run -----------------------------------------------------------------


def _apply_task(entry: dict[str, Any], status: str, doc_id: int | None, result: str) -> None:
    if status == "SUCCESS":
        entry["status"] = "done"
        if doc_id is not None:
            entry["document_id"] = doc_id
    elif status == "FAILURE":
        entry["status"] = "duplicate" if "duplicate" in result.lower() else "failed"
    else:
        return
    entry["updated_at"] = _now()


def refresh_pending(
    api: Paperless, state: dict[str, dict[str, Any]], wait_seconds: float, poll: float = 2.0
) -> None:
    deadline = time.monotonic() + max(wait_seconds, 0)
    while True:
        pending = [e for e in state.values() if e.get("status") == "pending" and e.get("task_id")]
        for entry in pending:
            status, doc_id, result = api.task_status(entry["task_id"])
            if status == "UNKNOWN":
                if _age_seconds(entry) > PENDING_TTL_SECONDS:
                    # Paperless doesn't know this task (expired, or a
                    # different instance). Send it again next run. Paperless
                    # rejects a true duplicate by checksum, so that's safe.
                    entry["status"] = "retry"
                    entry["updated_at"] = _now()
                continue
            _apply_task(entry, status, doc_id, result)
        still = any(e.get("status") == "pending" for e in state.values())
        if not still or time.monotonic() >= deadline:
            return
        time.sleep(poll)


def run(args: argparse.Namespace, transport: httpx.BaseTransport | None = None) -> int:
    data_dir = Path(
        args.data_dir or os.getenv("OPENKP_DATA_DIR", "").strip() or "~/.openkp"
    ).expanduser()
    download_dir = data_dir / "downloads"
    state_path = data_dir / STATE_FILENAME
    since = args.since

    state = load_state(state_path)
    candidates = find_candidates(download_dir, since, include_unlisted=args.include_unlisted)
    if args.retry_failed:
        for entry in state.values():
            if entry.get("status") == "failed":
                entry["status"] = "retry"
    todo: list[Candidate] = []
    seen: set[str] = set()
    for c in candidates:
        # Same bytes under two names is one document. Send it once.
        if c.sha256 in seen:
            continue
        seen.add(c.sha256)
        if state.get(c.sha256, {}).get("status") not in TERMINAL_STATUSES | {"pending"}:
            todo.append(c)
    if args.limit:
        todo = todo[: args.limit]

    url = os.getenv("PAPERLESS_URL", "").strip()
    token = os.getenv("PAPERLESS_TOKEN", "").strip()

    if args.dry_run and not (url and token):
        print(f"Dry run. {len(todo)} file(s) would be uploaded (no Paperless lookups):")
        for c in todo:
            print(f"  {_label(c, args.show_names)}")
        return EXIT_OK

    if not url or not token:
        raise UsageError("Set PAPERLESS_URL and PAPERLESS_TOKEN (or use --dry-run).")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise UsageError("PAPERLESS_URL must look like https://host[:port].")
    if parsed.scheme == "http" and parsed.hostname not in LOOPBACK_HOSTS:
        if not args.allow_insecure_http:
            raise UsageError(
                "PAPERLESS_URL is plain http, so the token and your documents would travel "
                "unencrypted. Use https, or pass --allow-insecure-http if you really mean it."
            )
        print("Warning: plain http. The token and documents travel unencrypted.",
              file=sys.stderr)

    api = Paperless(url, token, transport=transport)
    try:
        fields: dict[str, Any] = {}
        if args.tag:
            fields["tags"] = [str(api.resolve_id("tags", t)) for t in args.tag]
        if args.correspondent:
            fields["correspondent"] = str(api.resolve_id("correspondents", args.correspondent))
        if args.document_type:
            fields["document_type"] = str(api.resolve_id("document_types", args.document_type))

        if args.dry_run:
            print(f"Dry run. {len(todo)} file(s) would be uploaded with the given metadata:")
            for c in todo:
                print(f"  {_label(c, args.show_names)}")
            return EXIT_OK

        failures = 0
        for c in todo:
            entry = {"file": c.path.name, "kind": c.kind, "updated_at": _now()}
            try:
                entry["task_id"] = api.upload(c, fields)
                entry["status"] = "pending"
                print(f"  queued   {_label(c, args.show_names)}")
            except RuntimeError as exc:
                entry["status"] = "failed"
                entry["error"] = str(exc)
                failures += 1
                print(f"  FAILED   {_label(c, args.show_names)}: {exc}", file=sys.stderr)
            state[c.sha256] = entry
            save_state(state_path, state)

        refresh_pending(api, state, wait_seconds=args.wait)
        save_state(state_path, state)
    finally:
        api.close()

    counts: dict[str, int] = {}
    for entry in state.values():
        counts[entry.get("status", "?")] = counts.get(entry.get("status", "?"), 0) + 1
    summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
    print(f"Done. Uploaded {len(todo) - failures} this run. All tracked files: {summary or 'none'}.")
    failed_now = sum(1 for e in state.values() if e.get("status") == "failed")
    return EXIT_FAILURES if failures or failed_now else EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="openkp-export-paperless",
        description="Upload OpenKP downloads to Paperless-NGX. Reads PAPERLESS_URL and "
        "PAPERLESS_TOKEN from the environment.",
    )
    p.add_argument("--data-dir", help="OpenKP data dir (default: $OPENKP_DATA_DIR or ~/.openkp)")
    p.add_argument("--dry-run", action="store_true", help="list what would be uploaded, send nothing")
    p.add_argument("--tag", action="append", default=[], help="existing Paperless tag name, repeatable")
    p.add_argument("--correspondent", help="existing Paperless correspondent name")
    p.add_argument("--document-type", help="existing Paperless document type name")
    p.add_argument("--since", type=_parse_since, help="only files modified on or after YYYY-MM-DD")
    p.add_argument("--limit", type=int, default=0, help="upload at most N files this run")
    p.add_argument("--wait", type=float, default=30.0,
                   help="seconds to wait for Paperless to finish consuming (default 30)")
    p.add_argument("--retry-failed", action="store_true", help="retry files that failed before")
    p.add_argument("--include-unlisted", action="store_true",
                   help="also export files in downloads/ that no OpenKP download tool recorded")
    p.add_argument("--show-names", action="store_true",
                   help="print file names (Kaiser titles) instead of hash prefixes")
    p.add_argument("--allow-insecure-http", action="store_true",
                   help="allow a plain-http PAPERLESS_URL that isn't localhost")
    return p


def _parse_since(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError("use YYYY-MM-DD") from None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except UsageError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (httpx.HTTPError, RuntimeError) as exc:
        # httpx exceptions carry the URL, never headers. Print the class and
        # message, not a repr of the request.
        print(f"Error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILURES
    except (ValueError, KeyError, TypeError) as exc:
        # An unexpected response shape (HTML error page, missing field).
        # Don't print the message: it could quote response content.
        print(f"Error: unexpected response from Paperless ({type(exc).__name__}).", file=sys.stderr)
        return EXIT_FAILURES


if __name__ == "__main__":
    sys.exit(main())
