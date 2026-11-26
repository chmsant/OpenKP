"""Proxy (act-for) mode.

ProxySwitch field names (Id, DisplayName, IsSelf, IsSelected, LinkUrl) and
the switch link format come from the 2026-11-26 live recon. Values here are
fabricated. The fake portal below models what recon showed: active chart is
server-side state that a GET of LinkUrl changes and ProxySwitch reports.
"""

from __future__ import annotations

from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from openkp import mcp_server
from openkp.scrapers import proxy
from openkp.scrapers.proxy import (
    ProxyContextError,
    ProxySelector,
    ProxySubject,
    fingerprint,
    parse_proxy_subjects,
    resolve_target,
)

KP = "https://healthy.kaiserpermanente.org"


def _link(eid: str) -> str:
    # Relative to /mychartcn/ProxySwitch, the shape Kaiser sends (live 2026-11-26).
    return f"inside.asp?mode=proxyswitch&action=switchcontext&src=0&eid={eid}"


class FakePortal:
    """Stateful stand-in for KaiserRequest against the proxy surfaces."""

    def __init__(self, charts=None, honor_switch=True, redirect_to=None):
        self.charts = charts or [("WP-self", "Alex Example", True), ("WP-kid", "Female Example", False),
                                 ("WP-kid2", "Sam Example", False)]
        self.active = "WP-self"
        self.honor_switch = honor_switch
        self.redirect_to = redirect_to
        self.calls: list[str] = []
        self.session_store = SimpleNamespace(generation=1)

    def _payload(self):
        return {"ProxySubjectList": [
            {"Id": i, "DisplayName": n, "IsSelf": s, "IsSelected": i == self.active, "LinkUrl": "#" if i == self.active else _link(i)}
            for i, n, s in self.charts]}

    async def get(self, url, params=None, headers=None, extra_cookies=None):
        self.calls.append(url)
        req = httpx.Request("GET", url if url.startswith("http") else KP + url)
        if url.endswith("/mychartcn/ProxySwitch"):
            return httpx.Response(200, json=self._payload(), request=req)
        parsed = urlparse(url)
        q = parse_qs(parsed.query)
        if parsed.path == "/mychartcn/inside.asp":
            if self.redirect_to:
                return httpx.Response(302, headers={"Location": self.redirect_to}, request=req)
            if q.get("mode") == ["self"]:
                if self.honor_switch:
                    self.active = "WP-self"
                return httpx.Response(302, headers={"Location": "/mychartcn/Home"}, request=req)
            # Live shape (2026-11-26): inside.asp sets a proxy-context cookie
            # and HandleRedirect applies the switch only if it gets it back.
            return httpx.Response(302, request=req, headers=[
                ("Location", "/mychartcn/Home/HandleRedirect"),
                ("Set-Cookie", f"ProxyRelationshipID={q['eid'][0]}; Path=/mychartcn"),
            ])
        if parsed.path == "/mychartcn/Home/HandleRedirect":
            eid = (extra_cookies or httpx.Cookies()).get("ProxyRelationshipID")
            if self.honor_switch and eid:
                self.active = eid
            return httpx.Response(302, headers={"Location": "/mychartcn/Home"}, request=req)
        if parsed.path == "/mychartcn/Home":
            return httpx.Response(200, text="home", request=req)
        return httpx.Response(404, request=req)


def _subjects():
    return parse_proxy_subjects(FakePortal()._payload())


# --- parsing and selection ---


def test_parse_skips_rows_without_id_and_odd_shapes():
    assert parse_proxy_subjects(None) == []
    assert parse_proxy_subjects({"ProxySubjectList": [None, {"DisplayName": "x"}, {"Id": "a"}]})[0].id == "a"


def test_fingerprint_is_sha256_hex():
    assert fingerprint("abc") == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"


def test_resolve_by_fingerprint_prefix():
    t = resolve_target(_subjects(), ProxySelector(id_sha256=fingerprint("WP-kid")[:16]))
    assert t.id == "WP-kid"


def test_resolve_rejects_short_prefix():
    with pytest.raises(ProxyContextError, match="16 hex"):
        resolve_target(_subjects(), ProxySelector(id_sha256=fingerprint("WP-kid")[:8]))


def test_resolve_by_exact_name_case_insensitive():
    assert resolve_target(_subjects(), ProxySelector(display_name="female EXAMPLE")).id == "WP-kid"


@pytest.mark.parametrize("sel", [ProxySelector(display_name="Female"), ProxySelector(display_name="Nobody"),
                                 ProxySelector(id_sha256="0" * 64)])
def test_resolve_no_match_is_error(sel):
    with pytest.raises(ProxyContextError, match="matched 0"):
        resolve_target(_subjects(), sel)


def test_resolve_ambiguous_name_is_error():
    subs = _subjects() + [ProxySubject("WP-x", "Female Example", False, False, _link("WP-x"))]
    with pytest.raises(ProxyContextError, match="matched 2"):
        resolve_target(subs, ProxySelector(display_name="Female Example"))


def test_resolve_refuses_self():
    with pytest.raises(ProxyContextError, match="own chart"):
        resolve_target(_subjects(), ProxySelector(display_name="Alex Example"))


def test_error_messages_never_include_raw_ids():
    with pytest.raises(ProxyContextError) as exc:
        resolve_target(_subjects(), ProxySelector(id_sha256=fingerprint("nope")))
    assert "WP-" not in str(exc.value)


# --- switch link validation ---


@pytest.mark.parametrize("link", [
    _link("WP-kid"),
    "/mychartcn/" + _link("WP-kid"),
    KP + "/mychartcn/" + _link("WP-kid"),
])
def test_valid_switch_links(link):
    assert proxy._validated_switch_url(link, "WP-kid").startswith(KP + "/mychartcn/inside.asp?")


def test_root_relative_inside_asp_is_refused():
    # /inside.asp (outside /mychartcn) isn't the switch endpoint.
    with pytest.raises(ProxyContextError, match="unexpected switch link"):
        proxy._validated_switch_url("/" + _link("WP-kid"), "WP-kid")


def test_switch_link_for_a_different_chart_is_refused():
    with pytest.raises(ProxyContextError, match="unexpected switch link"):
        proxy._validated_switch_url(_link("WP-other"), "WP-kid")


@pytest.mark.parametrize("link", [
    "https://evil.example/mychartcn/inside.asp?mode=proxyswitch&action=switchcontext&eid=x",
    "http://healthy.kaiserpermanente.org/mychartcn/" + _link("x"),
    "/mychartcn/other.asp?mode=proxyswitch&action=switchcontext&eid=x",
    "/mychartcn/inside.asp?mode=self",
    "/mychartcn/inside.asp?mode=proxyswitch&action=logout&eid=x",
    "#",
])
def test_invalid_switch_links_are_refused(link):
    with pytest.raises(ProxyContextError, match="unexpected switch link"):
        proxy._validated_switch_url(link, "x")


# --- ensure / verify ---


async def test_ensure_context_switches_and_verifies():
    portal = FakePortal()
    t = await proxy.ensure_context(portal, ProxySelector(id_sha256=fingerprint("WP-kid")))
    assert t.id == "WP-kid" and portal.active == "WP-kid"
    assert any("inside.asp" in c for c in portal.calls)


async def test_ensure_context_skips_switch_when_already_active():
    portal = FakePortal()
    portal.active = "WP-kid"
    await proxy.ensure_context(portal, ProxySelector(id_sha256=fingerprint("WP-kid")))
    assert not any("inside.asp" in c for c in portal.calls)


async def test_ensure_context_fails_closed_when_switch_does_not_take():
    portal = FakePortal(honor_switch=False)
    with pytest.raises(ProxyContextError, match="still reports a different active chart"):
        await proxy.ensure_context(portal, ProxySelector(id_sha256=fingerprint("WP-kid")))


async def test_redirect_off_kaiser_is_not_followed():
    portal = FakePortal(redirect_to="https://identityauth.kaiserpermanente.org/as/authorization.oauth2")
    with pytest.raises(ProxyContextError):
        await proxy.ensure_context(portal, ProxySelector(id_sha256=fingerprint("WP-kid")))
    assert not any("identityauth" in c for c in portal.calls)


async def test_assert_still_active_detects_drift():
    portal = FakePortal()
    t = await proxy.ensure_context(portal, ProxySelector(id_sha256=fingerprint("WP-kid")))
    portal.active = "WP-self"
    with pytest.raises(ProxyContextError, match="changed while the tool ran"):
        await proxy.assert_still_active(portal, t)


# --- server gating ---


class _NoNetworkStore:
    """Session store that fails any attempt to authenticate or talk to Kaiser."""

    generation = 1

    def __init__(self, *a, **kw):
        pass

    async def get_session(self):
        raise RuntimeError("network forbidden in this test")


@pytest.fixture
def proxy_env(monkeypatch, tmp_path):
    monkeypatch.setattr("openkp.config.load_dotenv", lambda *a, **kw: None)
    monkeypatch.setenv("KP_USERNAME", "a@example.com")
    monkeypatch.setenv("KP_PASSWORD", "x")
    monkeypatch.setenv("OPENKP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("OPENKP_ACT_FOR_ID_SHA256", fingerprint("WP-kid"))
    monkeypatch.setattr(mcp_server, "_session_store", None)
    # If the gate ever lets a tool through, it hits this and fails fast
    # instead of reaching a real login.
    monkeypatch.setattr(mcp_server, "SessionStore", _NoNetworkStore)
    return tmp_path


@pytest.mark.parametrize("tool, kwargs", [
    ("get_profile", {}), ("list_medications", {}), ("list_problems", {}), ("list_messages", {}),
    ("request_refill", {"medication_id": "m", "confirm": True}),
    ("send_message", {"recipient_id": "r", "topic_value": "101", "subject": "s", "body": "b", "confirm": True}),
    ("download_lab_result_pdf", {"order_key": "k"}),
])
async def test_unlisted_tools_refuse_in_proxy_mode(proxy_env, tool, kwargs):
    with pytest.raises(RuntimeError, match="disabled here"):
        await getattr(mcp_server, tool)(**kwargs)


def _wire_portal(monkeypatch, portal: FakePortal, store):
    monkeypatch.setattr(mcp_server, "KaiserRequest", lambda s: portal)
    monkeypatch.setattr(mcp_server, "SessionStore", lambda *a, **kw: store)


async def test_proxy_read_switches_runs_and_reverifies(proxy_env, monkeypatch):
    portal = FakePortal()
    store = SimpleNamespace(generation=1, data_dir=proxy_env)
    _wire_portal(monkeypatch, portal, store)
    seen = {}

    async def read(client):
        seen["active_during_read"] = client.active
        return "data"

    assert await mcp_server._proxy_read(read) == "data"
    assert seen["active_during_read"] == "WP-kid"
    assert (proxy_env / mcp_server.PROXY_MARKER_FILE).exists()


async def test_proxy_read_discards_result_after_silent_relogin(proxy_env, monkeypatch):
    """Re-login detected by the generation counter alone, even when the
    post-call chart check would pass (for example the re-login landed on
    the right chart by chance, but the read in between hit the holder's)."""
    portal = FakePortal()
    store = SimpleNamespace(generation=1, data_dir=proxy_env)
    _wire_portal(monkeypatch, portal, store)

    async def read(client):
        store.generation += 1  # KaiserRequest re-authenticated mid-call
        return "possibly the holder's data"

    with pytest.raises(ProxyContextError, match="signed in again"):
        await mcp_server._proxy_read(read)


async def test_proxy_read_discards_result_when_chart_drifts(proxy_env, monkeypatch):
    portal = FakePortal()
    _wire_portal(monkeypatch, portal, SimpleNamespace(generation=1, data_dir=proxy_env))

    async def read(client):
        client.active = "WP-kid2"
        return "someone else's data"

    with pytest.raises(ProxyContextError, match="changed while the tool ran"):
        await mcp_server._proxy_read(read)


async def test_list_immunizations_is_allowed_in_proxy_mode(proxy_env, monkeypatch):
    portal = FakePortal()
    _wire_portal(monkeypatch, portal, SimpleNamespace(generation=1, data_dir=proxy_env))

    class _R:
        def model_dump(self):
            return {"total_count": 1}

    async def fake_fetch(client):
        assert client.active == "WP-kid"
        return _R()

    monkeypatch.setattr(mcp_server, "fetch_immunizations", fake_fetch)
    assert await mcp_server.list_immunizations() == {"total_count": 1}


async def test_list_proxy_targets_returns_fingerprints_not_ids(proxy_env, monkeypatch):
    portal = FakePortal()
    _wire_portal(monkeypatch, portal, SimpleNamespace(generation=1, data_dir=proxy_env))
    out = await mcp_server.list_proxy_targets()
    assert "WP-" not in str(out)
    kid = next(t for t in out["targets"] if t["display_name"] == "Female Example")
    assert kid["id_sha256"] == fingerprint("WP-kid")
    assert kid["configured_target"] is True
    assert portal.active == "WP-self"  # listing never switches


def test_normal_profile_refuses_a_proxy_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr("openkp.config.load_dotenv", lambda *a, **kw: None)
    monkeypatch.setenv("KP_USERNAME", "a@example.com")
    monkeypatch.setenv("KP_PASSWORD", "x")
    monkeypatch.setenv("OPENKP_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("OPENKP_ACT_FOR_ID_SHA256", raising=False)
    (tmp_path / mcp_server.PROXY_MARKER_FILE).write_text("x")
    with pytest.raises(RuntimeError, match="belongs to a proxy"):
        mcp_server._get_session_store()


def test_whoami_reports_acting_for_without_full_fingerprint(proxy_env):
    out = mcp_server.whoami()
    assert out["acting_for"].startswith("chart with id fingerprint ")
    assert fingerprint("WP-kid") not in out["acting_for"]


def test_bad_fingerprint_config_is_rejected(proxy_env, monkeypatch):
    monkeypatch.setenv("OPENKP_ACT_FOR_ID_SHA256", "not-hex")
    with pytest.raises(RuntimeError, match="16 to 64 hex"):
        mcp_server.whoami()


# --- KaiserRequest carries hop cookies on top of the session's ---


def test_request_jar_adds_extra_cookies_on_top_of_session_cookies():
    from openkp.scrapers.request import _jar

    session = [{"name": "S", "value": "sess", "domain": ".kaiserpermanente.org", "path": "/"}]
    extra = httpx.Cookies()
    extra.set("ProxyRelationshipID", "hop", domain="healthy.kaiserpermanente.org", path="/mychartcn")
    jar = _jar(session, extra)
    names = {c.name: c.value for c in jar.jar}
    assert names == {"S": "sess", "ProxyRelationshipID": "hop"}
    assert {c.name for c in _jar(session, None).jar} == {"S"}
