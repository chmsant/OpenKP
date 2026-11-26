"""Proxy (act-for) context: read a dependent's chart from the account
holder's login.

Kaiser keeps the active patient as server-side session state. A GET of the
switch link moves the whole session to the dependent, and the legacy
`/mychartcn/...` endpoints then return the dependent's data. Not everything
follows. `/mycare/v1.0/user` and the pharmacy BFF stay on the account holder,
so those tools must never run in proxy mode (enforced in `mcp_server`).

Identity checks use `GET /mychartcn/ProxySwitch`, whose `IsSelected` flag
was confirmed live to move on switch and back. A dependent is chosen by the
SHA-256 of its `Id` (stable across logins, confirmed live) or by exact
display name. Newborn charts carry a placeholder name that gets renamed, so
prefer the fingerprint. Ambiguity is always an error.

Docs: `docs/research/endpoints/proxy.md`
"""

from __future__ import annotations

import hashlib
import logging
import random
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import httpx

from openkp.scrapers.request import KaiserRequest

logger = logging.getLogger(__name__)

PROXY_SWITCH_PATH = "/mychartcn/ProxySwitch"
SELF_SWITCH_PATH = "/mychartcn/inside.asp?mode=self"
KP_HOST = "healthy.kaiserpermanente.org"
SWITCH_PATH = "/mychartcn/inside.asp"
MAX_REDIRECTS = 5


class ProxyContextError(RuntimeError):
    """The session isn't (or couldn't be put) on the intended chart.
    Messages never include ids, and name a chart only by its label."""


@dataclass(frozen=True)
class ProxySubject:
    id: str
    display_name: str
    is_self: bool
    is_selected: bool
    link_url: str

    @property
    def fingerprint(self) -> str:
        return fingerprint(self.id)


@dataclass(frozen=True)
class ProxySelector:
    id_sha256: str = ""  # full hex digest, or a unique prefix of 16+ chars
    display_name: str = ""

    def describe(self) -> str:
        if self.id_sha256:
            return f"chart with id fingerprint {self.id_sha256[:12]}…"
        return f"chart named {self.display_name!r}"


def fingerprint(subject_id: str) -> str:
    return hashlib.sha256(subject_id.encode("utf-8")).hexdigest()


# --- discovery ---


async def fetch_proxy_subjects(client: KaiserRequest) -> list[ProxySubject]:
    response = await client.get(
        PROXY_SWITCH_PATH,
        params={"noCache": f"{random.random()}"},
        headers={"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
    )
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError:
        raise ProxyContextError("ProxySwitch did not return JSON") from None
    return parse_proxy_subjects(payload)


def parse_proxy_subjects(payload: Any) -> list[ProxySubject]:
    rows = payload.get("ProxySubjectList") if isinstance(payload, dict) else None
    out: list[ProxySubject] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        subject_id = str(row.get("Id") or "").strip()
        if not subject_id:
            continue
        out.append(
            ProxySubject(
                id=subject_id,
                display_name=str(row.get("DisplayName") or "").strip(),
                is_self=row.get("IsSelf") is True,
                is_selected=row.get("IsSelected") is True,
                link_url=str(row.get("LinkUrl") or "").strip(),
            )
        )
    return out


def resolve_target(subjects: list[ProxySubject], selector: ProxySelector) -> ProxySubject:
    """Exactly one non-self chart matching the selector, or an error."""
    if selector.id_sha256:
        want = selector.id_sha256.lower()
        if len(want) < 16:
            raise ProxyContextError("OPENKP_ACT_FOR_ID_SHA256 must be at least 16 hex characters")
        matches = [s for s in subjects if s.fingerprint.startswith(want)]
    elif selector.display_name:
        want_name = selector.display_name.casefold()
        matches = [s for s in subjects if s.display_name.casefold() == want_name]
    else:
        raise ProxyContextError("No proxy target configured")
    if len(matches) != 1:
        raise ProxyContextError(
            f"{selector.describe()} matched {len(matches)} charts on this account. "
            "Run list_proxy_targets to see the options."
        )
    target = matches[0]
    if target.is_self:
        raise ProxyContextError(f"{selector.describe()} is the account holder's own chart, not a dependent")
    return target


# --- switching ---


def _validated_switch_url(link_url: str, eid: str) -> str:
    """Only follow Kaiser's own proxy-switch link, for the chart we resolved.

    Kaiser sends LinkUrl relative to the ProxySwitch endpoint (live
    2026-11-26: `inside.asp?mode=proxyswitch&...`), so resolve it against
    that URL, not the site root.
    """
    parsed = urlparse(urljoin(f"https://{KP_HOST}{PROXY_SWITCH_PATH}", link_url))
    query = parse_qs(parsed.query)
    if (
        parsed.scheme != "https"
        or parsed.netloc != KP_HOST
        or parsed.path.lower() != SWITCH_PATH.lower()
        or query.get("mode") != ["proxyswitch"]
        or query.get("action") != ["switchcontext"]
        or not eid
        or query.get("eid") != [eid]
    ):
        raise ProxyContextError("Kaiser returned an unexpected switch link. Refusing to follow it.")
    return parsed.geturl()


async def _get_following_same_host(client: KaiserRequest, url: str) -> int:
    """GET and follow up to MAX_REDIRECTS redirects, only on Kaiser's host.

    Cookies set by each hop ride along to the next, like a browser. The
    switch needs this (live 2026-11-26): inside.asp sets proxy-context
    cookies and /mychartcn/Home/HandleRedirect only applies the switch when
    it gets them back. These hop cookies are not persisted.
    """
    hop_cookies = httpx.Cookies()
    for _ in range(MAX_REDIRECTS + 1):
        response = await client.get(url, extra_cookies=hop_cookies)
        for c in response.cookies.jar:
            hop_cookies.jar.set_cookie(c)
        if response.status_code not in (301, 302, 303, 307, 308):
            return response.status_code
        nxt = urlparse(urljoin(url, response.headers.get("Location", "")))
        if nxt.netloc != KP_HOST or nxt.scheme != "https":
            # Leaving Kaiser's host (for example to Ping) means the switch
            # didn't complete. Verification below will catch it.
            return response.status_code
        url = nxt.geturl()
    return 0


async def switch_to(client: KaiserRequest, target: ProxySubject) -> None:
    await _get_following_same_host(client, _validated_switch_url(target.link_url, target.id))


async def switch_to_self(client: KaiserRequest) -> None:
    await _get_following_same_host(client, f"https://{KP_HOST}{SELF_SWITCH_PATH}")


async def ensure_context(client: KaiserRequest, selector: ProxySelector) -> ProxySubject:
    """Make the session's active chart the selected dependent, then verify it.

    Returns the verified target. Raises ProxyContextError if the portal
    doesn't report that chart as active afterwards.
    """
    subjects = await fetch_proxy_subjects(client)
    target = resolve_target(subjects, selector)
    if not _is_active(subjects, target):
        logger.info("Switching session to the configured proxy chart")
        await switch_to(client, target)
        subjects = await fetch_proxy_subjects(client)
        if not _is_active(subjects, target):
            raise ProxyContextError(
                f"Switched to the {selector.describe()}, but Kaiser still reports a different "
                "active chart. Nothing was read."
            )
    return target


async def assert_still_active(client: KaiserRequest, target: ProxySubject) -> None:
    subjects = await fetch_proxy_subjects(client)
    if not _is_active(subjects, target):
        raise ProxyContextError(
            "The active chart changed while the tool ran. The result was discarded."
        )


def _is_active(subjects: list[ProxySubject], target: ProxySubject) -> bool:
    selected = [s for s in subjects if s.is_selected]
    return len(selected) == 1 and selected[0].id == target.id
