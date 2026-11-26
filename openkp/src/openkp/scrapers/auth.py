"""Kaiser Permanente login via Playwright.

kp.org sits behind PingFederate OAuth2 / OIDC. See
`docs/research/endpoints/auth.md` for the full redirect map derived from the
kp-login-2.har capture. Key moves:

    1. GET https://healthy.kaiserpermanente.org/sign-on
    2. Redirects through /secure/inner-door to
       https://identityauth.kaiserpermanente.org/as/authorization.oauth2
       with client_id=KPORGOauthClientPAWebSessionV1
    3. User (or us) submits pf.username + pf.pass to the Ping form
    4. Optional MFA challenge (SMS, email, push, WebAuthn). User completes
       by hand in the visible browser window.
    5. Ping issues an auth code, redirects back to /pa/oidc/cb.
    6. Epic MyChart session handoff lands on /mychartcn/Home?lang=en-US.

Because the flow includes device fingerprinting, adaptive risk scoring, and
optional WebAuthn, we drive a real Chromium via Playwright rather than raw
HTTP.

Strategy (see ADR-005):
- First run: launch Chromium headed with a persistent user data dir.
  OpenKP autofills username + password from keyring. User handles any MFA
  challenge in the visible window.
- Interactive login mode (empty password, OPENKP_INTERACTIVE_LOGIN=1):
  same window, but OpenKP fills at most the username and the person types
  their own password. Chromium's password manager is turned off in the
  profile either way, so a typed password isn't saved to disk.
- Subsequent runs: reopen the same profile headless and probe
  /mycare/v1.0/user. If the session is alive, extract cookies. If not,
  caller falls back to interactive login.

OpenKP never handles any MFA factor in code. No TOTP, no SMS scraping, no
email scraping, no push automation.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright

logger = logging.getLogger(__name__)

KP_SIGN_ON_URL = "https://healthy.kaiserpermanente.org/sign-on"

PING_HOST = "identityauth.kaiserpermanente.org"
KP_HOST = "healthy.kaiserpermanente.org"

# How long to wait for the user to finish MFA in the visible browser.
INTERACTIVE_LOGIN_TIMEOUT_MS = 5 * 60 * 1000  # 5 minutes

# Chromium flags for the login window. The bubble flag stops the "Save
# password?" prompt. The Preferences written by `_disable_password_saving`
# turn the password manager off for the profile itself.
CHROMIUM_ARGS = ["--disable-save-password-bubble"]


@dataclass
class KaiserSession:
    """A successfully authenticated session. Pass to the HTTP client."""

    cookies: list[dict]  # Playwright cookie shape: name, value, domain, path, ...
    user_agent: str

    def save_to(self, path: Path) -> None:
        """Write cookies + user_agent to `path` as JSON, chmod 0600.

        File contents are equivalent in sensitivity to the cookies in the
        Playwright profile at `~/.openkp/chromium-profile/`, which are also
        stored unencrypted on disk — so this does not widen the blast radius.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically: temp file + rename.
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(asdict(self)))
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    @classmethod
    def load_from(cls, path: Path) -> KaiserSession | None:
        """Return the persisted session, or None if missing / corrupt."""
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text())
            return cls(cookies=data["cookies"], user_agent=data["user_agent"])
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("Persisted session at %s is unreadable (%s), ignoring", path, exc)
            return None


async def login_interactive(data_dir: Path, username: str, password: str) -> KaiserSession:
    """Launch Chromium, complete the login + MFA flow, return cookies.

    Headed. The browser window is visible because the user may need to
    complete MFA. If the persistent profile already holds a live Kaiser
    session, the sign-on URL skips Ping entirely and we just extract cookies.

    Raises:
        playwright.async_api.TimeoutError: user did not complete login within
            INTERACTIVE_LOGIN_TIMEOUT_MS.
    """
    profile_dir = _profile_dir(data_dir)
    profile_dir.mkdir(parents=True, exist_ok=True)
    interactive = not password
    if not _disable_password_saving(profile_dir) and interactive:
        # The whole point of interactive mode is that the password never
        # lands on disk. If we can't turn saving off, don't open the window.
        raise RuntimeError(
            "Could not turn off password saving in OpenKP's browser profile "
            f"({profile_dir}). Interactive login was not started."
        )
    if interactive:
        saved = _count_saved_passwords(profile_dir)
        if saved:
            raise RuntimeError(
                f"OpenKP's browser profile ({profile_dir}) already holds {saved} saved "
                "password(s), probably from stored-password mode. Interactive login "
                "won't run on top of them. Move the profile folder aside (you'll sign "
                "in again) and retry."
            )

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            headless=False,
            viewport={"width": 1280, "height": 900},
            args=CHROMIUM_ARGS,
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()

            logger.info("Navigating to %s", KP_SIGN_ON_URL)
            await page.goto(KP_SIGN_ON_URL, wait_until="load")

            if PING_HOST in page.url:
                await _fill_ping_form(page, username, password)
                logger.info(
                    "Waiting up to %d seconds for login to complete "
                    "(including any MFA challenge)",
                    INTERACTIVE_LOGIN_TIMEOUT_MS // 1000,
                )
                await page.wait_for_url(_is_logged_in_url, timeout=INTERACTIVE_LOGIN_TIMEOUT_MS)
            else:
                # Either already landed on /mychartcn/Home via the persistent
                # profile, or still mid-redirect. Give it a short window.
                if not _is_logged_in_url(page.url):
                    logger.info(
                        "Sign-on did not land on Ping, waiting for /mychartcn/Home"
                    )
                    await page.wait_for_url(_is_logged_in_url, timeout=60_000)
                else:
                    logger.info("Persistent profile already holds a live session")

            logger.info("Login complete at %s", page.url)
            # Playwright returns list[Cookie] (TypedDict); convert to plain
            # list[dict] so the rest of OpenKP doesn't take a Playwright type
            # dependency.
            cookies: list[dict] = [dict(c) for c in await context.cookies()]
            user_agent = await page.evaluate("() => navigator.userAgent")
            _log_cookie_names(cookies)

            return KaiserSession(cookies=cookies, user_agent=user_agent)
        finally:
            await context.close()


async def _fill_ping_form(page: Any, username: str, password: str) -> bool:
    """Fill the Ping sign-in form. Returns True if credentials were submitted.

    With a password: fill both fields and press Enter, as before.
    Without one (interactive login mode): fill only the username if we have
    it, never touch the password field, never submit. The person types the
    password and submits it themselves.
    """
    await page.wait_for_selector('input[name="pf.username"]', state="visible", timeout=30_000)
    if not password:
        if username:
            await page.fill('input[name="pf.username"]', username)
        logger.info("Interactive login: sign in yourself in the browser window")
        return False

    logger.info("On Ping login form, autofilling credentials")
    await page.fill('input[name="pf.username"]', username)
    await page.fill('input[name="pf.pass"]', password)
    # Submit by pressing Enter in the password field. This fires
    # whatever onsubmit handlers Ping attached, which matters
    # because those feed the risk-scoring fingerprint.
    await page.press('input[name="pf.pass"]', "Enter")
    return True


def _disable_password_saving(profile_dir: Path) -> bool:
    """Turn off Chromium's password manager in the persistent profile.

    Without this, a password typed into Kaiser's form can be offered for
    saving and land in the profile's Login Data file. Merges into any
    existing Preferences and writes atomically. Returns False on failure
    (logged, not raised). Interactive mode treats False as fatal.

    An unreadable Preferences file is a failure, not something to overwrite:
    Chromium would otherwise lose every other setting in it.
    """
    prefs_path = profile_dir / "Default" / "Preferences"
    try:
        prefs: dict[str, Any] = {}
        if prefs_path.exists():
            loaded = json.loads(prefs_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prefs = loaded
        prefs["credentials_enable_service"] = False
        profile = prefs.get("profile")
        if not isinstance(profile, dict):
            profile = {}
        profile["password_manager_enabled"] = False
        prefs["profile"] = profile
        prefs_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = prefs_path.with_name(prefs_path.name + ".openkp-tmp")
        tmp.write_text(json.dumps(prefs), encoding="utf-8")
        tmp.replace(prefs_path)
        return True
    except (OSError, ValueError) as exc:
        logger.warning("Could not disable password saving in the browser profile (%s)", type(exc).__name__)
        return False


def _count_saved_passwords(profile_dir: Path) -> int:
    """How many logins Chromium has saved in this profile. 0 if none or unknown.

    Reads the `logins` table of `Default/Login Data` read-only. Never reads
    or returns the stored values.
    """
    import sqlite3

    db = profile_dir / "Default" / "Login Data"
    if not db.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT COUNT(*) FROM logins").fetchone()
        finally:
            conn.close()
        return int(row[0]) if row else 0
    except sqlite3.Error as exc:
        logger.warning("Could not read saved-password count (%s)", type(exc).__name__)
        return 0


def _profile_dir(data_dir: Path) -> Path:
    return data_dir / "chromium-profile"


def _is_logged_in_url(url: str) -> bool:
    """True when the URL is the logged-in MyChart home page.

    Excludes the intermediate `/mychartcn/Authentication/Login` hop in the
    Epic session-handoff chain (step 7 in auth.md).
    """
    return (
        KP_HOST in url
        and "/mychartcn/Home" in url
        and "/Authentication/Login" not in url
    )


def _log_cookie_names(cookies: list[dict]) -> None:
    """Log the names (never values) of the cookies Playwright captured.

    auth.md section 10 flags cookie-name enumeration as an open question the
    first real login should close. Names only, never values.
    """
    names = sorted({c["name"] for c in cookies})
    logger.info("Session cookies captured (%d): %s", len(names), ", ".join(names))
