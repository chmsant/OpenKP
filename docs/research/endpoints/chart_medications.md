# Chart medications (MyChart Medications page)

**Tool:** `list_chart_medications`. **Module:** `openkp/src/openkp/scrapers/chart_medications.py`.

Mapped live 2026-11-26 from the page's own scripts and responses. Keys and masked value formats only. No values were recorded.

## Why a second medications source

`list_medications` reads the pharmacy BFF on `apims.kaiserpermanente.org`. That service is tied to the signed-in account and ignores the proxy switch. The classic MyChart page at `/mychartcn/Clinical/Medications` renders the active chart's list, so it works for proxy profiles.

## Request

`/mychartcn/Clinical/Medications` loads `areas/clinical/medications/scripts/controllers/medicationsstepcontroller.min.js`. Its `_AJAXLoadLocalMember` posts to `Clinical/Medications/LoadMedicationsPageData` with no data in the page's default mode.

```
POST /mychartcn/Clinical/Medications/LoadMedicationsPageData?noCache=<random>
Content-Type: application/x-www-form-urlencoded   (empty body)
__RequestVerificationToken: <token from the page>
X-Requested-With: XMLHttpRequest
Referer: https://healthy.kaiserpermanente.org/mychartcn/Clinical/Medications
```

The same controller also references `LoadExternal` (other organizations, `{context: <mode>}`), `HasMedicationReference`, `MedGuide`, `Search`, and `SubmitUpdate` (a write). OpenKP uses only `LoadMedicationsPageData`.

## Response (top level)

```
CommunityMembers: list            the local organization is [0]
  [0].PrescriptionList: dict
      Prescriptions: list         one entry per medication
      Pickups, Deliveries, InProgressWorkRequests: list
      Organization: dict
      ... display flags
ShowPatientAdmittedBanner: bool   true while the active chart's patient is admitted
IsProxyView: bool                 true when acting for someone else's chart
GetPatientFirstName: str          not returned by OpenKP
MedSettings: dict                 includes DateOfBirth, not returned by OpenKP
MedicationsUrl: str
```

## Prescription entry (fields OpenKP reads)

| Field | Type | Format seen |
|---|---|---|
| `ID` | str | opaque |
| `Name` | str | |
| `PatientFriendlyName` | dict | `{Caption, CaptionType, Text}` |
| `Sig` | str | instructions as written |
| `StartDate` | str | `Mon D, YYYY` or `Month D, YYYY`, or empty |
| `DateDisplayKey` | str | `dateprescribedlabel` or `datedocumentedlabel` |
| `FormattedDateNoted` | str | `M/D/YYYY` |
| `PrescriptionNumber` | str | |
| `OrderingProvider`, `AuthorizingProvider` | dict | provider card, `Name` used |
| `Organization` | dict | `OrganizationName` used |
| `IsPatientReported`, `IsClinicReported` | bool | |
| `RefillDetails` | dict | see below |

`RefillDetails` keys: `IsRefillable`, `HasRefillsRemaining`, `RefillsRemaining` (str, digits), `RefillExpirationDate` (`Month D, YYYY`), `DaySupply` (str, digits), `WrittenDispenseQuantity` (`"90 tablet"` style), plus `LastDispense`, `CostDetails`, `NextDispenseDate`, `ScheduledFillDate`, `CurrentFillDat`, `RefillStatus`, `RefillButtonStatus`, `RxFlags`, `WrittenDispenseAmount`, `WrittenDispenseUnit`, `OwningPharmacy` and a few display fields. `LastDispense` and `CostDetails` contents aren't mapped yet.

Entries also carry many `Show*` display flags, `ClassList`, `ContentLinkURL`, previous-sig fields, and variance fields that OpenKP ignores.

## Notes

- A newborn admitted to the NICU had an empty list. Medicines given in the hospital aren't on it.
- An account with no outpatient prescriptions also returned an empty list here, matching the pharmacy BFF.
