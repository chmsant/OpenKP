"""Shared test fixtures."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_data_dir(monkeypatch, tmp_path_factory):
    """Point OPENKP_DATA_DIR at a temp dir for every test.

    Anything that resolves the data dir from the environment during a test
    lands here instead of the real ~/.openkp. Tests that care about the value
    override it with their own monkeypatch.setenv.
    """
    monkeypatch.setenv("OPENKP_DATA_DIR", str(tmp_path_factory.mktemp("openkp-data")))
    monkeypatch.delenv("OPENKP_PROFILE", raising=False)


@pytest.fixture(autouse=True)
def _never_launch_a_real_browser(monkeypatch):
    """No test may drive a real Chromium to kp.org.

    If a safeguard regresses and a test falls through to the real login
    path, fail fast here instead of opening a browser and submitting the
    test's fake credentials to Kaiser. Tests that exercise auth.py patch
    `async_playwright` themselves, which overrides this.
    """

    def _blocked():
        raise RuntimeError("test tried to launch a real browser (async_playwright is blocked in tests)")

    monkeypatch.setattr("openkp.scrapers.auth.async_playwright", _blocked)
