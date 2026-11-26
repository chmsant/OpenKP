"""Immunizations scraper.

One MCP tool surfaces from this module:

- `list_immunizations`: vaccines recorded on the chart, grouped by the
  organization that holds the record.

Source: legacy MyChart `POST /mychartcn/Clinical/Immunizations/LoadImmunizationsList`
with body `{}`. Same auth and CSRF contract as problems and allergies.

Field names come from a live structure-only recon (2026-11-26) of the
account holder's and a proxied dependent's lists. The **value formats were
not captured**, in particular `FormattedAdministeredDates`. The raw display
strings are passed through untouched, and `administered_dates_iso` is a
best-effort parse that is null when the format isn't recognized. Pin it
down from a real response before trusting the ISO field.

Docs: `docs/research/endpoints/immunizations.md`
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

LIST_PATH = "/mychartcn/Clinical/Immunizations/LoadImmunizationsList"
PAGE_REFERER = "https://healthy.kaiserpermanente.org/mychartcn/Clinical/Immunizations"

# Formats tried for the ISO convenience field. The first matches the
# M/D/YYYY display format used by other legacy MyChart endpoints
# (problems.FormattedDateNoted). The rest are cheap guesses.
_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%b %d, %Y", "%B %d, %Y")


# --- models ---


class Immunization(BaseModel):
    id: str
    name: str | None = None
    administered_dates: list[str] = Field(default_factory=list)  # Kaiser display strings, as-is
    administered_dates_iso: list[str | None] = Field(default_factory=list)  # best-effort, same order
    organization_name: str | None = None
    organization_is_local: bool | None = None


class ImmunizationOrganization(BaseModel):
    organization_id: str | None = None
    name: str | None = None
    is_local: bool | None = None
    as_of: str | None = None  # FormattedLocalTimeStamp, display string
    count: int = 0


class ImmunizationsResponse(BaseModel):
    immunizations: list[Immunization] = Field(default_factory=list)
    organizations: list[ImmunizationOrganization] = Field(default_factory=list)
    total_count: int = 0


# --- public ---


async def fetch_immunizations(client: KaiserRequest) -> ImmunizationsResponse:
    """Fetch the immunization list. One CSRF fetch and one POST.

    An empty list is a valid outcome. Never raises on missing fields.
    """
    csrf = await fetch_csrf_token(client, referer=PAGE_REFERER)
    response = await client.post(
        LIST_PATH,
        params={"noCache": f"{random.random()}"},
        headers=_api_headers(csrf),
        json={},
    )
    response.raise_for_status()
    return _parse_response(response.json())


# --- private ---


def _api_headers(csrf_token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Origin": "https://healthy.kaiserpermanente.org",
        "Referer": PAGE_REFERER,
        "X-Requested-With": "XMLHttpRequest",
        "__RequestVerificationToken": csrf_token,
    }


def _parse_response(payload: Any) -> ImmunizationsResponse:
    if not isinstance(payload, dict):
        return ImmunizationsResponse()
    groups = payload.get("OrganizationImmunizationList")
    if not isinstance(groups, list):
        return ImmunizationsResponse()

    immunizations: list[Immunization] = []
    organizations: list[ImmunizationOrganization] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        raw_org = group.get("Organization")
        org: dict[str, Any] = raw_org if isinstance(raw_org, dict) else {}
        org_name = _str_or_none(org.get("OrganizationName"))
        org_local = org.get("IsLocal") if isinstance(org.get("IsLocal"), bool) else None
        items = group.get("OrgImmunizations")
        parsed = [
            imm
            for imm in (
                _parse_item(i, org_name, org_local) for i in (items if isinstance(items, list) else [])
            )
            if imm is not None
        ]
        immunizations.extend(parsed)
        organizations.append(
            ImmunizationOrganization(
                organization_id=_str_or_none(org.get("OrganizationId")),
                name=org_name,
                is_local=org_local,
                as_of=_str_or_none(org.get("FormattedLocalTimeStamp")),
                count=len(parsed),
            )
        )
    return ImmunizationsResponse(
        immunizations=immunizations,
        organizations=organizations,
        total_count=len(immunizations),
    )


def _parse_item(item: Any, org_name: str | None, org_local: bool | None) -> Immunization | None:
    if not isinstance(item, dict):
        return None
    imm_id = _str_or_none(item.get("Id"))
    if imm_id is None:
        return None
    raw_dates = item.get("FormattedAdministeredDates")
    dates = [s for s in (_str_or_none(d) for d in (raw_dates if isinstance(raw_dates, list) else [])) if s]
    return Immunization(
        id=imm_id,
        name=_str_or_none(item.get("Name")),
        administered_dates=dates,
        administered_dates_iso=[_to_iso(d) for d in dates],
        organization_name=org_name,
        organization_is_local=org_local,
    )


def _to_iso(value: str) -> str | None:
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None
