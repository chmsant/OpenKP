"""Chart medication list (MyChart's Medications page data).

Key names and value formats are from a live response (2026-11-26, keys and
masked formats only). Every value below is fabricated. Fields the parser
doesn't read are left out of the fixture.
"""

from __future__ import annotations

import httpx
import pytest

from openkp.scrapers import chart_medications as cm


def _row(**over):
    row = {
        "ID": "rx-1",
        "Name": "EXAMPLEMYCIN 10 MG TABLET",
        "PatientFriendlyName": {"Caption": "", "CaptionType": "", "Text": "examplemycin"},
        "Sig": "Take 1 tablet by mouth daily.",
        "StartDate": "Jan 2, 2025",
        "DateDisplayKey": "dateprescribedlabel",
        "FormattedDateNoted": "1/2/2025",
        "PrescriptionNumber": "000000000",
        "OrderingProvider": {"ID": "p1", "Name": "Example Provider, MD", "Type": 1},
        "AuthorizingProvider": {"ID": "p2", "Name": "Sample Prescriber, MD", "Type": 1},
        "Organization": {"OrganizationName": "Example Health", "OrganizationId": "o1"},
        "IsPatientReported": False,
        "IsClinicReported": True,
        "RefillDetails": {
            "IsRefillable": True,
            "HasRefillsRemaining": True,
            "RefillsRemaining": "3",
            "RefillExpirationDate": "March 4, 2026",
            "DaySupply": "90",
            "WrittenDispenseQuantity": "90 tablet",
        },
    }
    row.update(over)
    return row


def _payload(rows, **over):
    p = {
        "CommunityMembers": [{"PrescriptionList": {"Prescriptions": rows, "Pickups": [], "Deliveries": []}}],
        "ShowPatientAdmittedBanner": False,
        "IsProxyView": True,
        "GetPatientFirstName": "Quillon",
    }
    p.update(over)
    return p


def test_parses_a_prescription():
    out = cm.parse_chart_medications(_payload([_row()]))
    assert out.total_count == 1
    assert out.is_proxy_view is True
    assert out.patient_admitted is False
    m = out.medications[0]
    assert m.name == "EXAMPLEMYCIN 10 MG TABLET"
    assert m.friendly_name == "examplemycin"
    assert m.sig == "Take 1 tablet by mouth daily."
    assert m.start_date == "Jan 2, 2025" and m.start_date_iso == "2025-01-02"
    assert m.date_kind == "prescribed"
    assert m.date_noted_iso == "2025-01-02"
    assert m.ordering_provider == "Example Provider, MD"
    assert m.authorizing_provider == "Sample Prescriber, MD"
    assert m.organization == "Example Health"
    assert m.is_clinic_reported and not m.is_patient_reported
    assert m.is_refillable is True and m.refills_remaining == "3"
    assert m.refill_expiration_date_iso == "2026-03-04"
    assert m.day_supply == "90" and m.dispense_quantity == "90 tablet"


def test_patient_name_is_not_returned():
    out = cm.parse_chart_medications(_payload([_row()]))
    assert "Quillon" not in out.model_dump_json()


@pytest.mark.parametrize("raw, iso", [
    ("Nov 3, 2026", "2026-11-03"),
    ("January 12, 2026", "2026-01-12"),
    ("12/31/2025", "2025-12-31"),
    ("", None),
    ("sometime", None),
])
def test_date_formats(raw, iso):
    assert cm._to_iso(raw) == iso


def test_patient_reported_documented_med():
    out = cm.parse_chart_medications(_payload([_row(
        DateDisplayKey="datedocumentedlabel", IsPatientReported=True, IsClinicReported=False,
        StartDate="", RefillDetails={},
    )]))
    m = out.medications[0]
    assert m.date_kind == "documented"
    assert m.is_patient_reported
    assert m.start_date is None and m.start_date_iso is None
    assert m.is_refillable is None


@pytest.mark.parametrize("payload", [None, [], {}, {"CommunityMembers": None},
                                     {"CommunityMembers": [{"PrescriptionList": None}]},
                                     {"CommunityMembers": ["x", {"PrescriptionList": {"Prescriptions": ["x"]}}]}])
def test_malformed_payloads_never_raise(payload):
    out = cm.parse_chart_medications(payload)
    assert out.total_count == 0


def test_empty_list_for_an_admitted_newborn():
    out = cm.parse_chart_medications(_payload([], ShowPatientAdmittedBanner=True))
    assert out.total_count == 0 and out.patient_admitted is True


async def test_fetch_posts_form_with_csrf(monkeypatch):
    sent = {}

    async def fake_csrf(client, referer):
        return "tok"

    class _Client:
        async def post(self, path, params=None, headers=None, data=None):
            sent.update(path=path, headers=headers, data=data)
            return httpx.Response(200, json=_payload([_row()]), request=httpx.Request("POST", "https://x" + path))

    monkeypatch.setattr(cm, "fetch_csrf_token", fake_csrf)
    out = await cm.fetch_chart_medications(_Client())
    assert sent["path"] == cm.LIST_PATH
    assert sent["headers"]["__RequestVerificationToken"] == "tok"
    assert sent["data"] == {}
    assert out.total_count == 1
