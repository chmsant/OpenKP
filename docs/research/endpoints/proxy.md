# Proxy (act-for) endpoints

Source: a live browser recon on 2026-11-26, NorCal, from an account with proxy access to five other charts including a newborn's. It was done from page context with structure-only capture. No HAR was kept and no names or ids were recorded here.

## Summary

| Step | Request | Status |
| --- | --- | --- |
| Discover charts | Rendered page: `window.EpicPx.ReactContext.personalizations.proxySubjects` plus the `.dropDownProxyList` menu | ✅ Seen live |
| Switch to a dependent | `GET /mychartcn/inside.asp?mode=proxyswitch&action=switchcontext&src=0&eid=<INTERNAL id>` | ✅ 200 after redirect |
| Switch back to self | `GET /mychartcn/inside.asp?mode=self` | ✅ 200 after redirect |
| Verify active chart | Rendered menu entry with class `currentContext` | ✅ Seen live |

The switch matches vanilla Epic MyChart. The active patient is **server-side session state**. After a switch, plain `fetch` calls with the same cookies return the dependent's data. Verified with `POST /mychartcn/Clinical/Immunizations/LoadImmunizationsList`, which returned a different list after the switch than before it. Nothing about the request changes per patient.

## Discovery

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

**Important: the menu and `proxySubjects` exist only in the rendered page.** A `fetch` of `/mychartcn/Home` HTML returned neither. A server-side scraper needs a different discovery source, or has to render the page in a browser. The endpoint behind `proxySubjects` is still unknown. Standard Epic exposes `GET <mount>/ProxySwitch`, which is not yet tried here.

## Identity and stability

- All 11 id values for the dependent were **identical across page loads and a switch round-trip in one session.** Stability across a new login is **not yet checked**. Standard Epic `WP-` ids are described as session-scoped, so don't rely on them across logins until that's tested.
- **Display names are unreliable for newborns.** The newborn's chart is listed under a placeholder (sex plus surname), not the given name, and is expected to be renamed later. A name selector would break on the rename. A selector by name has to be confirmed by the person and re-confirmed after any rename.
- The menu entry text starts with the avatar initial (for example `"AAlex"` for Alex). Strip it before matching.

## Open questions

1. Do the `WP-` ids survive a new login? Re-run the fingerprint check (hash, never record the value) after the next sign-in.
2. Is there a JSON endpoint for the chart list (`/mychartcn/ProxySwitch`)? Without one, discovery needs rendered HTML.
3. Does the pharmacy BFF on `apims` (`x-guid`) follow the switch, or keep returning the account holder's medications?
4. Does `/mycare/v1.0/user` (`get_profile`) return the dependent after a switch? If not, `get_profile` can't be the identity check.
5. Does a persisted `session.json` reload in proxy context? `KeepAlive` keeps the context alive, but a new process might inherit a stale dependent context. Always issue `mode=self` or an explicit switch at startup.
