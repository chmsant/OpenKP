
## Implementation (`scrapers/proxy.py`, `mcp_server._proxy_read`)

- **Selector:** `OPENKP_ACT_FOR_ID_SHA256` (16 to 64 hex chars, a prefix of `sha256(Id)`) or `OPENKP_ACT_FOR_NAME` (exact, case-insensitive). Zero matches, multiple matches, or a match on the self entry are all errors.
- **Switch:** follow `LinkUrl` only if it is `https://healthy.kaiserpermanente.org/mychartcn/inside.asp` with `mode=proxyswitch&action=switchcontext`. Redirects are followed only on that host.
- **Deny by default:** in a proxy profile, `_get_session_store` refuses unless the tool opted in. `list_immunizations` opts in through `_proxy_read`. `session_check` and `list_proxy_targets` opt in without touching chart data.
- **`_proxy_read`:** `ensure_context` (switch if needed, then verify `IsSelected`), record `SessionStore.generation`, run the read, discard the result if the generation changed (a silent re-login resets the chart), then re-verify `IsSelected`.
- A `proxy-profile` marker in the data dir stops a normal profile from reusing a proxy profile's session.
- Tests break each of the three safeguards on purpose to prove the suite notices. A conftest fixture blocks real browser launches in every test.

To allow another tool in proxy mode: check live that its endpoint follows the switch (compare self against the dependent), then route it through `_proxy_read` and add it to the README list.
