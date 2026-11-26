"""Multi-profile isolation: each process's files stay under its own data dir."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openkp import mcp_server
from openkp.config import load_config


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    monkeypatch.setattr("openkp.config.load_dotenv", lambda *a, **kw: None)


def test_profile_defaults_to_empty(monkeypatch, tmp_path):
    monkeypatch.setenv("KP_USERNAME", "a@example.com")
    monkeypatch.setenv("KP_PASSWORD", "x")
    monkeypatch.setenv("OPENKP_DATA_DIR", str(tmp_path))
    assert load_config().profile == ""


def test_profile_read_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("KP_USERNAME", "a@example.com")
    monkeypatch.setenv("KP_PASSWORD", "x")
    monkeypatch.setenv("OPENKP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENKP_PROFILE", "  alex  ")
    assert load_config().profile == "alex"


def test_whoami_reports_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("KP_USERNAME", "a@example.com")
    monkeypatch.setenv("KP_PASSWORD", "x")
    monkeypatch.setenv("OPENKP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENKP_PROFILE", "sam")
    out = mcp_server.whoami()
    assert out["profile"] == "sam"
    assert out["data_dir"] == str(tmp_path)
    assert "password" not in out


class _Outcome:
    def model_dump(self) -> dict:
        return {"status": "downloaded"}


def _fake_store(data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=data_dir)


@pytest.mark.parametrize(
    "tool_name, scraper_attr, kwargs",
    [
        ("download_lab_result_pdf", "_download_lab_result_pdf", {"order_key": "k"}),
        ("download_visit_avs_pdf", "_download_visit_avs_pdf", {"csn": "c"}),
        ("download_message_attachment", "_download_message_attachment", {"dcs_id": "d"}),
    ],
)
async def test_download_tools_write_under_profile_data_dir(
    monkeypatch, tmp_path, tool_name, scraper_attr, kwargs
):
    """Regression: download tools used to ignore OPENKP_DATA_DIR and share
    ~/.openkp/downloads across every profile."""
    seen: dict = {}

    async def fake_scraper(*args, **kw):
        seen.update(kw)
        return _Outcome()

    profile_dir = tmp_path / "profile-a"
    monkeypatch.setattr(mcp_server, "_get_session_store", lambda: _fake_store(profile_dir))
    monkeypatch.setattr(mcp_server, "KaiserRequest", lambda store: SimpleNamespace(session_store=store))
    monkeypatch.setattr(mcp_server, scraper_attr, fake_scraper)

    tool = getattr(mcp_server, tool_name)
    result = await tool(**kwargs)

    assert result == {"status": "downloaded"}
    assert seen["download_dir"] == profile_dir / "downloads"
