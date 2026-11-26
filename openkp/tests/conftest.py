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
