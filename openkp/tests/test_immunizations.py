"""Tests for scrapers/immunizations.py.

Fixture field NAMES come from a live structure-only recon (2026-11-26).
Field VALUES are fabricated, including the date strings: the real
FormattedAdministeredDates format was not captured, so tests of the ISO
convenience field only prove the parser's fallbacks, not Kaiser's format.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from openkp.scrapers.csrf import CSRF_PATH
from openkp.scrapers.immunizations import (
    LIST_PATH,
    PAGE_REFERER,
    _parse_response,
    _to_iso,
    fetch_immunizations,
)

_FAKE_CSRF = "fake-csrf-token-imm"


def _payload(groups=None) -> dict:
    return {
        "ImmunizationsUrl": "/mychartcn/Clinical/Immunizations",
        "ShowPersonalNotes": False,
        "OrganizationImmunizationList": groups if groups is not None else [
            {
                "Organization": {
                    "OrganizationId": "org-1",
                    "OrganizationName": "Fake Health NorCal",
                    "IsLocal": True,
                    "FormattedLocalTimeStamp": "11/26/2026 9:00 AM",
                },
                "ShowViewDetailsLink": True,
                "OrgImmunizations": [
                    {"Id": "imm-1", "Name": "Made-up Vaccine A",
                     "FormattedAdministeredDates": ["1/2/2025", "7/2/2025"],
                     "ContentLinkURL": "", "ContentLinkTarget": ""},
                    {"Id": "imm-2", "Name": "Made-up Vaccine B",
                     "FormattedAdministeredDates": ["sometime"],
                     "ContentLinkURL": "", "ContentLinkTarget": ""},
                ],
            },
            {
                "Organization": {"OrganizationId": "org-2", "OrganizationName": "Outside Clinic",
                                 "IsLocal": False},
                "ShowViewDetailsLink": False,
                "OrgImmunizations": [
                    {"Id": "imm-3", "Name": "Made-up Vaccine C", "FormattedAdministeredDates": []},
                ],
            },
        ],
    }


def test_parse_groups_and_flattens():
    r = _parse_response(_payload())
    assert r.total_count == 3
    assert [i.id for i in r.immunizations] == ["imm-1", "imm-2", "imm-3"]
    assert r.immunizations[2].organization_name == "Outside Clinic"
    assert r.immunizations[2].organization_is_local is False
    assert [(o.name, o.count) for o in r.organizations] == [("Fake Health NorCal", 2), ("Outside Clinic", 1)]
    assert r.organizations[0].as_of == "11/26/2026 9:00 AM"


def test_dates_raw_kept_and_iso_best_effort():
    r = _parse_response(_payload())
    a, b, _ = r.immunizations
    assert a.administered_dates == ["1/2/2025", "7/2/2025"]
    assert a.administered_dates_iso == ["2025-01-02", "2025-07-02"]
    assert b.administered_dates == ["sometime"]
    assert b.administered_dates_iso == [None]


@pytest.mark.parametrize("raw, iso", [("1/2/2025", "2025-01-02"), ("2025-01-02", "2025-01-02"),
                                      ("Jan 2, 2025", "2025-01-02"), ("January 2, 2025", "2025-01-02"),
                                      ("2 Jan 2025", None)])
def test_to_iso(raw, iso):
    assert _to_iso(raw) == iso


@pytest.mark.parametrize("payload", [None, [], "x", {}, {"OrganizationImmunizationList": None},
                                     {"OrganizationImmunizationList": [None, 3, {"OrgImmunizations": "x"}]}])
def test_parse_never_raises_on_odd_shapes(payload):
    r = _parse_response(payload)
    assert r.total_count == 0


def test_items_without_id_are_skipped():
    r = _parse_response(_payload([{"Organization": {}, "OrgImmunizations": [{"Name": "no id"}, {"Id": "ok"}]}]))
    assert [i.id for i in r.immunizations] == ["ok"]
    assert r.immunizations[0].organization_name is None


def test_empty_list_is_valid():
    r = _parse_response(_payload([]))
    assert r.total_count == 0 and r.organizations == []


def _make_store() -> MagicMock:
    from openkp.scrapers.auth import KaiserSession

    store = MagicMock()
    store.get_session = AsyncMock(return_value=KaiserSession(
        cookies=[{"name": "k", "value": "v", "domain": ".kp.org", "path": "/"}], user_agent="ua"))
    store.invalidate = AsyncMock()
    return store


def _patch_http(responses):
    req = httpx.Request("GET", "https://healthy.kaiserpermanente.org" + LIST_PATH)
    for r in responses:
        r.request = req
    mock_client = AsyncMock()
    mock_client.request = AsyncMock(side_effect=responses)
    patched = patch("openkp.scrapers.request.httpx.AsyncClient")
    cls = patched.start()
    cls.return_value.__aenter__.return_value = mock_client
    cls.return_value.__aexit__.return_value = None
    return mock_client, patched


async def test_fetch_immunizations_request_contract():
    from openkp.scrapers.request import KaiserRequest

    mock_client, p = _patch_http([
        httpx.Response(200, text=f'<input name="__RequestVerificationToken" value="{_FAKE_CSRF}" />'),
        httpx.Response(200, json=_payload()),
    ])
    try:
        r = await fetch_immunizations(KaiserRequest(_make_store()))
    finally:
        p.stop()
    assert r.total_count == 3
    csrf_call, list_call = mock_client.request.await_args_list
    assert csrf_call.args[0] == "GET" and CSRF_PATH in csrf_call.args[1]
    assert list_call.args[0] == "POST" and LIST_PATH in list_call.args[1]
    assert list_call.kwargs["json"] == {}
    assert list_call.kwargs["headers"]["__RequestVerificationToken"] == _FAKE_CSRF
    assert list_call.kwargs["headers"]["Referer"] == PAGE_REFERER
    assert "noCache" in list_call.kwargs["params"]
