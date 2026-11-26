"""The chart's medication list, from MyChart's own Medications page.

`list_medications` reads the pharmacy microservices on apims, which are tied
to the signed-in account and ignore Kaiser's proxy switch. This module reads
the list the classic MyChart page renders instead:

    POST /mychartcn/Clinical/Medications/LoadMedicationsPageData
         form-encoded, empty body, __RequestVerificationToken header

Confirmed live 2026-11-26 (keys and value formats only):
- it follows the proxy switch. `IsProxyView` turns true and the prescription
  list changes with the active chart.
- the list is under `CommunityMembers[0].PrescriptionList.Prescriptions`.
- dates come as display strings: `StartDate` like "Nov 3, 2026" or
  "January 12, 2026", `FormattedDateNoted` like "1/2/2026".
- `RefillDetails` carries refill state as strings ("3", "30", "90 tablet").

Parsers never raise on missing fields. Display strings are kept as Kaiser
sends them, with a best-effort ISO date beside each.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from openkp.scrapers.csrf import fetch_csrf_token
from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

LIST_PATH = "/mychartcn/Clinical/Medications/LoadMedicationsPageData"
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/Clinical/Medications"

# DateDisplayKey values seen live, mapped to what the date means.
_DATE_KINDS = {"dateprescribedlabel": "prescribed", "datedocumentedlabel": "documented"}


class ChartMedication(BaseModel):
    id: str | None = None
    name: str | None = None
    # Kaiser's plain-language name, when it has one.
    friendly_name: str | None = None
    # Instructions as written ("Take 1 tablet by mouth daily").
    sig: str | None = None
    start_date: str | None = None
    start_date_iso: str | None = None
    # Whether the date shown is when it was prescribed or when it was
    # documented (for example a med reported by the patient).
    date_kind: str | None = None
    date_noted: str | None = None
    date_noted_iso: str | None = None
    prescription_number: str | None = None
    ordering_provider: str | None = None
    authorizing_provider: str | None = None
    organization: str | None = None
    is_patient_reported: bool = False
    is_clinic_reported: bool = False
    is_refillable: bool | None = None
    has_refills_remaining: bool | None = None
    refills_remaining: str | None = None
    refill_expiration_date: str | None = None
    refill_expiration_date_iso: str | None = None
    day_supply: str | None = None
    dispense_quantity: str | None = None


class ChartMedicationsResponse(BaseModel):
    medications: list[ChartMedication] = Field(default_factory=list)
    total_count: int = 0
    # True when Kaiser rendered the list for a chart the signed-in person is
    # acting for, not their own.
    is_proxy_view: bool | None = None
    # Kaiser's "patient is admitted" banner. Inpatient medicines given in the
    # hospital don't appear in this list.
    patient_admitted: bool | None = None


async def fetch_chart_medications(client: KaiserRequest) -> ChartMedicationsResponse:
    """Fetch the active chart's medication list. One CSRF fetch, one POST."""
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
    response = await client.post(
        LIST_PATH,
        params={"noCache": f"{random.random()}"},
        headers={
            "Accept": "application/json, text/javascript, */*",
            "Origin": "https://healthy.kaiserpermanente.org",
            "Referer": PAGE_REFERER,
            "X-Requested-With": "XMLHttpRequest",
            "__RequestVerificationToken": csrf,
        },
        data={},
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        logger.warning("Medications page data was not JSON")
        payload = {}
    return parse_chart_medications(payload)


def parse_chart_medications(payload: Any) -> ChartMedicationsResponse:
    if not isinstance(payload, dict):
        return ChartMedicationsResponse()
    members = payload.get("CommunityMembers")
    rows: list[Any] = []
    if isinstance(members, list):
        for member in members:
            if not isinstance(member, dict):
                continue
            plist = member.get("PrescriptionList")
            if isinstance(plist, dict) and isinstance(plist.get("Prescriptions"), list):
                rows.extend(plist["Prescriptions"])
    meds = [_parse_row(r) for r in rows if isinstance(r, dict)]
    return ChartMedicationsResponse(
        medications=meds,
        total_count=len(meds),
        is_proxy_view=_bool_or_none(payload.get("IsProxyView")),
        patient_admitted=_bool_or_none(payload.get("ShowPatientAdmittedBanner")),
    )


def _parse_row(row: dict) -> ChartMedication:
    refill = _dict(row.get("RefillDetails"))
    friendly = _dict(row.get("PatientFriendlyName"))
    org = _dict(row.get("Organization"))
    start = _str(row.get("StartDate"))
    noted = _str(row.get("FormattedDateNoted"))
    expires = _str(refill.get("RefillExpirationDate"))
    return ChartMedication(
        id=_str(row.get("ID")),
        name=_str(row.get("Name")),
        friendly_name=_str(friendly.get("Text")),
        sig=_str(row.get("Sig")),
        start_date=start,
        start_date_iso=_to_iso(start),
        date_kind=_DATE_KINDS.get(str(row.get("DateDisplayKey") or "").lower()),
        date_noted=noted,
        date_noted_iso=_to_iso(noted),
        prescription_number=_str(row.get("PrescriptionNumber")),
        ordering_provider=_provider(row.get("OrderingProvider")),
        authorizing_provider=_provider(row.get("AuthorizingProvider")),
        organization=_str(org.get("OrganizationName")),
        is_patient_reported=bool(row.get("IsPatientReported")),
        is_clinic_reported=bool(row.get("IsClinicReported")),
        is_refillable=_bool_or_none(refill.get("IsRefillable")),
        has_refills_remaining=_bool_or_none(refill.get("HasRefillsRemaining")),
        refills_remaining=_str(refill.get("RefillsRemaining")),
        refill_expiration_date=expires,
        refill_expiration_date_iso=_to_iso(expires),
        day_supply=_str(refill.get("DaySupply")),
        dispense_quantity=_str(refill.get("WrittenDispenseQuantity")),
    )


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _provider(raw: Any) -> str | None:
    return _str(raw.get("Name")) if isinstance(raw, dict) else None


def _str(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _bool_or_none(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _to_iso(value: str | None) -> str | None:
    if not value:
        return None
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            continue
    return None
