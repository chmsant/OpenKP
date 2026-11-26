# Immunizations endpoint

Source: a live browser recon on 2026-11-26, NorCal, with structure-only capture. Seen for the account holder and for a proxied dependent.

## Summary

| Feature | Endpoint | Status |
| --- | --- | --- |
| Immunization list | `POST /mychartcn/Clinical/Immunizations/LoadImmunizationsList?noCache=<rand>` body `{}` | ✅ 200 JSON, self and proxy |
| Page (CSRF token source) | `GET /mychartcn/Clinical/Immunizations`, which has `input[name="__RequestVerificationToken"]` | ✅ |

Other calls fired by the page: `POST /mychartcn/api/search/LoadMenuInfo`, `/mychartcn/Clinical/Screenings` (332 B), `/mychartcn/Community/External/IsCommunityActive`. Screenings may be the "health reminders" surface. Not mapped yet.

## Headers used (same legacy family as problems/allergies)

```
Content-Type: application/json; charset=utf-8
X-Requested-With: XMLHttpRequest
__RequestVerificationToken: <from the page>
```

## Response shape

```
{
  ImmunizationsUrl: string,
  ShowPersonalNotes: boolean,
  OrganizationImmunizationList: [
    {
      Organization: { OrganizationId, OrganizationName, IsLocal, LogoUrl, TimeStamp, FormattedLocalTimeStamp, ... },
      ShowViewDetailsLink: boolean,
      OrgImmunizations: [
        { Id: string, Name: string, FormattedAdministeredDates: [string], ContentLinkURL: string, ContentLinkTarget: string }
      ]
    }
  ]
}
```

- Records are grouped by organization, so outside records (Care Everywhere) would presumably appear as extra organizations. Only one was seen here.
- Dates are **pre-formatted display strings** (`FormattedAdministeredDates`), not ISO. The format hasn't been captured, so the parser must keep the raw string and parse defensively.
- No dose/series or lot fields at the list level. `ShowViewDetailsLink` suggests a detail call exists. Not mapped yet.

## Before building `list_immunizations`

- Capture one real response body into the gitignored captures dir to pin down the date string format, then write fixtures from it. Field names above are observed. Values were deliberately not recorded.

## Implementation

`list_immunizations` (`openkp/src/openkp/scrapers/immunizations.py`) is built against the shape above. It returns the raw `FormattedAdministeredDates` strings untouched, plus `administered_dates_iso` as a best-effort parse (M/D/YYYY first, matching other legacy endpoints) that is null when the format isn't recognized. Test fixtures use fabricated values and say so. Confirm the date format from one real response before relying on the ISO field.
