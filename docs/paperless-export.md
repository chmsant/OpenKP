# Exporting downloads to Paperless-NGX

`openkp-export-paperless` is an optional command-line tool. It uploads the files OpenKP's download tools saved (lab PDFs, After Visit Summaries, message attachments) to your own [Paperless-NGX](https://docs.paperless-ngx.com/) instance.

It is not an MCP tool, and Claude can't trigger it. The OpenKP server only talks to Kaiser. This tool talks to Paperless only when you run it.

## How it works

- It reads `<data dir>/downloads/`, which is `~/.openkp/downloads/` unless you set `OPENKP_DATA_DIR`. It also reads the `.manifest.jsonl` the download tools write there, which supplies the title and kind of each file.
- By default it only uploads files an OpenKP download tool recorded in the manifest whose bytes still match the recorded hash. Anything else in the folder is ignored unless you pass `--include-unlisted`, which covers downloads saved before the manifest existed. Symlinks are always skipped.
- Re-downloading a document replaces its file in place. Kaiser builds PDFs on demand, so the new copy can differ byte for byte. The exporter skips a re-download of a document it already sent unless you pass `--include-updated`.
- It reads `OPENKP_DATA_DIR` from the same `openkp/.env` the server uses, so both look at one folder.
- It uploads each file it hasn't sent before. Files are matched by SHA-256, so renaming a file doesn't cause a second upload.
- It tracks progress in `<data dir>/paperless-export.json` (mode 600). An upload stays `pending` until Paperless's consume task finishes. It then becomes `done`, `duplicate` (Paperless already had those bytes) or `failed`.
- Tags, correspondent and document type must already exist in Paperless. The tool never creates them.
- It never follows redirects, so your token can't be forwarded to another host. It never prints Paperless error bodies, because they can echo document titles.
- It refuses a plain `http://` URL unless the host is localhost, so the token and documents never travel unencrypted by accident. `--allow-insecure-http` overrides this.
- By default it prints a hash prefix and the file's kind, not the file name. Names come from Kaiser titles, so they're medical information. Pass `--show-names` to see them.
- If Paperless no longer knows about a pending upload's task an hour later, the upload is retried. Paperless rejects a true duplicate by checksum, so a retry is safe.

## Usage

The token comes from the environment. With 1Password, inject it with `op run` so it never lands on disk:

```bash
op run --env-file=<(printf 'PAPERLESS_TOKEN=op://<vault>/<item>/<field>\n') -- \
  env PAPERLESS_URL=https://paperless.example.org \
  openkp/.venv/bin/openkp-export-paperless --dry-run --tag Medical --correspondent "Kaiser Permanente"
```

Drop `--dry-run` once the list looks right. Useful flags:

| Flag | Effect |
| --- | --- |
| `--dry-run` | List what would upload. With credentials it also checks the tag, correspondent and document type names, but it never uploads. |
| `--tag NAME` | Existing tag, repeatable |
| `--correspondent NAME`, `--document-type NAME` | Existing correspondent or document type |
| `--since YYYY-MM-DD` | Only files modified on or after that date |
| `--limit N` | At most N uploads this run |
| `--wait SECONDS` | How long to wait for Paperless to finish consuming (default 30). Anything still pending is checked on the next run. |
| `--retry-failed` | Send files that failed before |
| `--include-updated` | Also send re-downloaded copies of documents already exported, as new documents |
| `--include-unlisted` | Also send files in `downloads/` that the manifest doesn't list |
| `--show-names` | Print file names instead of hash prefixes |
| `--allow-insecure-http` | Allow a plain-http URL that isn't localhost |
| `--data-dir PATH` | A specific profile's data dir |

With more than one profile, run it once per data dir.

## Status

The upload request format matches a client already in use against a live Paperless instance. The task-status and list response formats come from the Paperless-NGX API docs and haven't been checked live from this tool yet. Run `--dry-run` first, then upload with `--limit 1` before a full run.

## Related download changes

The download tools now save files owner-only: the folder at `0700`, files and the manifest at `0600`. Each file is written to a hidden temp file first and then renamed into place, so the exporter never picks up a half-written PDF. Two different documents with the same Kaiser display name no longer overwrite each other. The second one is saved as `Name (2).pdf`.
