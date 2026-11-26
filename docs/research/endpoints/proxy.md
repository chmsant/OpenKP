# Proxy (act-for) endpoints

Source: a live browser recon on 2026-11-26, NorCal, from an account with proxy access to five other charts including a newborn's. It was done from page context with structure-only capture. No HAR was kept and no names or ids were recorded here.

## Summary

| Step | Request | Status |
| --- | --- | --- |
| Discover charts | `GET /mychartcn/ProxySwitch?noCache=<rand>` returns JSON `ProxySubjectList` | ✅ 200 JSON, no rendering needed |
| Discover charts (fallback) | Rendered page: `window.EpicPx.ReactContext.personalizations.proxySubjects` plus the `.dropDownProxyList` menu | ✅ Seen live |
| Switch to a dependent | `GET /mychartcn/inside.asp?mode=proxyswitch&action=switchcontext&src=0&eid=<INTERNAL id>` | ✅ 200 after redirect |
| Switch back to self | `GET /mychartcn/inside.asp?mode=self` | ✅ 200 after redirect |
| Verify active chart | `ProxySwitch` entry with `IsSelected: true` (or rendered menu `currentContext`) | ✅ Flips on switch and back |

The switch matches vanilla Epic MyChart. The active patient is **server-side session state**. After a switch, plain `fetch` calls with the same cookies return the dependent's data. Verified with `POST /mychartcn/Clinical/Immunizations/LoadImmunizationsList`, which returned a different list after the switch than before it. Nothing about the request changes per patient.

## Discovery

### `GET /mychartcn/ProxySwitch` (preferred)

Returns about 4 KB of JSON. Headers: `Accept: application/json`, session cookies. No CSRF token.

```
{
  ProxySubjectList: [ {
    Id: string,            // == the entry's INTERNAL id == the switch eid
    DisplayName: string, DisplayText: null,
    IsSelf: boolean, IsSelected: boolean,
    LinkUrl: string,       // the switch URL
    Ids: [...], BlobToken, PhotoUrl, PhotoMagicId, TabColor, Disabled, Loading,
    ServiceAreaAbbreviationList: string
  } ],
  CurrentlySelectedTabColor: number, AvailableLanguageList: [...],
  ShouldTryAgain, ShowAccountSettings, ShowFriendsAndFamily, ShowPersonalInformation: boolean
}
```

`IsSelected` is reliable here: after a switch it moves to the dependent, and after `mode=self` it moves back to the self entry. Use it as the server-side check of which chart is active.

### Rendered page (fallback)

`proxySubjects` is an array with one entry per chart, self first:

```
{ displayName: string, ids: [{type: string, value: string}] (12), photoMagicId: string, proxyColor: number }
```

- There is **no** `isSelf` / `isSelected` flag in this source. Self is index 0, and the active chart comes from the menu (below).
- The id types are `CALPFX 11, CEID, CID, EID, EXTERNAL, FHIR, FHIR STU3, INTERNAL, MYCHARTLOGIN, NCALMRN, WPRCID, WPRINTERNAL`. Every value is an opaque `WP-` token, 82 to 126 chars. Self carries `MYCHARTLOGIN`, which dependents don't have.
- The array order matches the menu order exactly.

The menu, `ul.dropDownProxyList a.proxySubjectLink[data-switchpatients]`:

- `data-switchpatients="false"` and `href="#"` on the active entry.
- `data-switchpatients="true"` and the switch URL on the others.
- `data-id` is the entry's `INTERNAL` id.
- The `eid` query param of the switch URL is also the `INTERNAL` id.
- Class `currentContext` marks the active chart.

The menu and `proxySubjects` exist only in the rendered page. A `fetch` of `/mychartcn/Home` HTML returned neither. Use `ProxySwitch` instead.

## Identity and stability

- All 11 id values for the dependent were **identical across page loads, a switch round-trip, and a full sign-out and sign-in** (checked by comparing SHA-256 fingerprints, never the values). Unlike reports for other Epic instances, Kaiser's `WP-` ids are stable across logins here. A profile can select a dependent by a fingerprint of the `INTERNAL` id. Re-verify if it ever stops matching.
- **Display names are unreliable for newborns.** The newborn's chart is listed under a placeholder (sex plus surname), not the given name, and is expected to be renamed later. A name selector would break on the rename. A selector by name has to be confirmed by the person and re-confirmed after any rename.
- The menu entry text starts with the avatar initial (for example `"AAlex"` for Alex). Strip it before matching.

## What follows the switch

| Surface | Follows the switch? | Evidence |
| --- | --- | --- |
| Legacy `/mychartcn/...` endpoints | ✅ Yes | `LoadImmunizationsList` count changed from self to dependent and back |
| `/mycare/v1.0/user` (`get_profile`, and the GUID `list_medications` uses) | ❌ **No** | Same GUID, name and DOB fingerprints in self and proxy context |
| Pharmacy BFF on `apims` (`list_medications`, `request_refill`, `track_refill_order`) | ❌ **Not usable** | It's keyed by `x-guid` from `/mycare/v1.0/user`, which stays the account holder's. With `X-includeProxyEntitlements: true` the user endpoint exposes only a `noOfSubjects` count, no dependent GUIDs. |

**Consequence:** in a proxy profile, `get_profile` and the pharmacy tools would silently return the **account holder's** data under the dependent's context. They must refuse in proxy mode, not just go unverified. A proxy medication list would have to come from the legacy MyChart medications page, if it follows the switch like immunizations. That's not mapped yet.

## Open questions

1. Does a persisted `session.json` reload in proxy context? `KeepAlive` keeps the context alive, but a new process might inherit a stale dependent context. Always issue an explicit switch (or `mode=self`) at startup and verify with `ProxySwitch`.
2. Do the other legacy read tools (labs, visits, messages, problems, allergies) all follow the switch? Probably, since they're the same `/mychartcn` family, but each needs one check. Messages are the riskiest, because the compose chain uses a self `wprId`.
3. Is there a legacy medications endpoint that follows the switch?

## Implementation (`scrapers/proxy.py`, `mcp_server._proxy_read`)

- **Selector:** `OPENKP_ACT_FOR_ID_SHA256` (16 to 64 hex chars, a prefix of `sha256(Id)`) or `OPENKP_ACT_FOR_NAME` (exact, case-insensitive). Zero matches, multiple matches, or a match on the self entry are all errors.
- **Switch:** follow `LinkUrl` only if it is `https://healthy.kaiserpermanente.org/mychartcn/inside.asp` with `mode=proxyswitch&action=switchcontext`. Redirects are followed only on that host.
- **Deny by default:** in a proxy profile, `_get_session_store` refuses unless the tool opted in. `list_immunizations`, `list_past_visits`, `read_visit_notes` and `download_visit_avs_pdf` opt in through `_proxy_read`. `session_check` and `list_proxy_targets` opt in without touching chart data.
- **`_proxy_read`:** `ensure_context` (switch if needed, then verify `IsSelected`), record `SessionStore.generation`, run the read, discard the result if the generation changed (a silent re-login resets the chart), then re-verify `IsSelected`.
- A `proxy-profile` marker in the data dir stops a normal profile from reusing a proxy profile's session.
- Tests break each of the three safeguards on purpose to prove the suite notices. A conftest fixture blocks real browser launches in every test.

To allow another tool in proxy mode: check live that its endpoint follows the switch (compare self against the dependent), then route it through `_proxy_read` and add it to the README list.

### Visits in proxy context (live 2026-11-26)

- `/mychartcn/Visits/VisitsList/LoadPast` follows the switch. As the account holder it returned the holder's visits, as the dependent one visit, with no overlap between the two lists (visit ids compared as hashes).
- Visit ids are scoped to the active chart. With the dependent active, `GetVisitDetailsPast` for one of the holder's visit ids returned HTTP 500, so `read_visit_notes` and `download_visit_avs_pdf` can't return another chart's documents under the dependent's name. Nothing was read and no file was written.
- `download_visit_avs_pdf` removes its saved file if the after-read chart check fails.
