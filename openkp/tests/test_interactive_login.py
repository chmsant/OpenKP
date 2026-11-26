"""Interactive login mode: OpenKP never holds the password."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from openkp import mcp_server
from openkp.config import load_config
from openkp.scrapers import auth


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setattr("openkp.config.load_dotenv", lambda *a, **kw: None)
    monkeypatch.setenv("OPENKP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("KP_USERNAME", raising=False)
    monkeypatch.delenv("KP_PASSWORD", raising=False)
    monkeypatch.delenv("OPENKP_INTERACTIVE_LOGIN", raising=False)


def _keyring_must_not_be_called(monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("keyring was read in interactive login mode")

    monkeypatch.setattr("openkp.config._load_password_from_keyring", boom)


# --- config ---


def test_interactive_mode_needs_no_password(monkeypatch):
    monkeypatch.setenv("OPENKP_INTERACTIVE_LOGIN", "1")
    monkeypatch.setenv("KP_USERNAME", "a@example.com")
    _keyring_must_not_be_called(monkeypatch)
    cfg = load_config()
    assert cfg.interactive_login is True
    assert cfg.password == ""
    assert cfg.username == "a@example.com"


def test_interactive_mode_needs_no_username(monkeypatch):
    monkeypatch.setenv("OPENKP_INTERACTIVE_LOGIN", "true")
    _keyring_must_not_be_called(monkeypatch)
    cfg = load_config()
    assert cfg.username == ""
    assert cfg.password == ""


def test_interactive_mode_ignores_kp_password(monkeypatch, caplog):
    monkeypatch.setenv("OPENKP_INTERACTIVE_LOGIN", "yes")
    monkeypatch.setenv("KP_PASSWORD", "hunter2")
    _keyring_must_not_be_called(monkeypatch)
    cfg = load_config()
    assert cfg.password == ""
    assert "hunter2" not in caplog.text
    assert "KP_PASSWORD is ignored" in caplog.text


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_non_truthy_values_keep_stored_mode(monkeypatch, value):
    monkeypatch.setenv("OPENKP_INTERACTIVE_LOGIN", value)
    with pytest.raises(RuntimeError, match="KP_USERNAME"):
        load_config()


def test_whoami_reports_login_mode(monkeypatch):
    monkeypatch.setenv("OPENKP_INTERACTIVE_LOGIN", "1")
    out = mcp_server.whoami()
    assert out["login_mode"] == "interactive"
    assert out["username"] == ""


# --- Ping form filling ---


class _FakePage:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def wait_for_selector(self, selector, **kw):
        self.calls.append(("wait", selector))

    async def fill(self, selector, value):
        self.calls.append(("fill", selector, value))

    async def press(self, selector, key):
        self.calls.append(("press", selector, key))


async def test_fill_ping_form_stored_mode_submits_both():
    page = _FakePage()
    submitted = await auth._fill_ping_form(page, "a@example.com", "pw")
    assert submitted is True
    assert ("fill", 'input[name="pf.username"]', "a@example.com") in page.calls
    assert ("fill", 'input[name="pf.pass"]', "pw") in page.calls
    assert ("press", 'input[name="pf.pass"]', "Enter") in page.calls


async def test_fill_ping_form_interactive_never_touches_password():
    page = _FakePage()
    submitted = await auth._fill_ping_form(page, "a@example.com", "")
    assert submitted is False
    assert page.calls == [
        ("wait", 'input[name="pf.username"]'),
        ("fill", 'input[name="pf.username"]', "a@example.com"),
    ]


async def test_fill_ping_form_interactive_without_username_fills_nothing():
    page = _FakePage()
    await auth._fill_ping_form(page, "", "")
    assert [c for c in page.calls if c[0] != "wait"] == []


# --- browser profile ---


def test_disable_password_saving_creates_prefs(tmp_path: Path):
    auth._disable_password_saving(tmp_path)
    prefs = json.loads((tmp_path / "Default" / "Preferences").read_text())
    assert prefs["credentials_enable_service"] is False
    assert prefs["profile"]["password_manager_enabled"] is False


def test_disable_password_saving_merges_existing_prefs(tmp_path: Path):
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text(json.dumps({"keep": 1, "profile": {"name": "x"}}))
    auth._disable_password_saving(tmp_path)
    prefs = json.loads(prefs_path.read_text())
    assert prefs["keep"] == 1
    assert prefs["profile"] == {"name": "x", "password_manager_enabled": False}


def test_disable_password_saving_survives_corrupt_prefs(tmp_path: Path):
    prefs_path = tmp_path / "Default" / "Preferences"
    prefs_path.parent.mkdir(parents=True)
    prefs_path.write_text("{not json")
    assert auth._disable_password_saving(tmp_path) is False
    assert prefs_path.read_text() == "{not json"  # left alone, not clobbered


def test_interactive_mode_pops_kp_password_from_environ(monkeypatch):
    import os

    monkeypatch.setenv("OPENKP_INTERACTIVE_LOGIN", "1")
    monkeypatch.setenv("KP_PASSWORD", "hunter2")
    load_config()
    assert "KP_PASSWORD" not in os.environ


def _make_login_db(profile_dir: Path, rows: int) -> None:
    import sqlite3

    db = profile_dir / "Default" / "Login Data"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE logins (origin_url TEXT, password_value BLOB)")
    for i in range(rows):
        conn.execute("INSERT INTO logins VALUES (?, ?)", (f"https://x{i}", b"secret"))
    conn.commit()
    conn.close()


def test_count_saved_passwords(tmp_path: Path):
    assert auth._count_saved_passwords(tmp_path) == 0
    _make_login_db(tmp_path, 2)
    assert auth._count_saved_passwords(tmp_path) == 2


class _NeverLaunch:
    """async_playwright stand-in that fails the test if a browser launches."""

    def __call__(self):
        raise AssertionError("browser launched")


async def test_interactive_login_refuses_when_prefs_unwritable(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(auth, "_disable_password_saving", lambda d: False)
    monkeypatch.setattr(auth, "async_playwright", _NeverLaunch())
    with pytest.raises(RuntimeError, match="password saving"):
        await auth.login_interactive(tmp_path, "", "")


async def test_interactive_login_refuses_with_existing_saved_passwords(monkeypatch, tmp_path: Path):
    _make_login_db(tmp_path / "chromium-profile", 1)
    monkeypatch.setattr(auth, "async_playwright", _NeverLaunch())
    with pytest.raises(RuntimeError, match="1 saved"):
        await auth.login_interactive(tmp_path, "", "")


async def test_login_launches_chromium_with_password_saving_off(monkeypatch, tmp_path: Path):
    """The persistent context gets the no-save flag, and Preferences are written first."""
    launched: dict = {}

    class _Ctx:
        pages: list = []

        async def new_page(self):
            raise RuntimeError("stop here")

        async def close(self):
            pass

    class _Chromium:
        async def launch_persistent_context(self, **kw):
            launched.update(kw)
            prefs = Path(kw["user_data_dir"]) / "Default" / "Preferences"
            launched["prefs_written_before_launch"] = prefs.exists()
            return _Ctx()

    class _PW:
        chromium = _Chromium()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(auth, "async_playwright", lambda: _PW())
    with pytest.raises(RuntimeError, match="stop here"):
        await auth.login_interactive(tmp_path, "", "")

    assert "--disable-save-password-bubble" in launched["args"]
    assert launched["headless"] is False
    assert launched["prefs_written_before_launch"] is True
