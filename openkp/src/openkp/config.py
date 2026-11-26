"""Configuration loader for OpenKP.

Reads credentials from, in priority order:
1. Environment variables (or .env file)
2. macOS Keychain / system keyring (service: "openkp", account: username)

Interactive login mode (OPENKP_INTERACTIVE_LOGIN=1) skips both. The person
types their own password into Kaiser's sign-in page, and OpenKP never holds
it. KP_USERNAME becomes optional in that mode.

Never log or print the password. Never write it to disk.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

KEYRING_SERVICE = "openkp"
INTERACTIVE_LOGIN_ENV = "OPENKP_INTERACTIVE_LOGIN"

# load_config runs on every tool call, and load_dotenv puts a KP_PASSWORD
# from .env back each time, so warn about it once per process.
_warned_ignored_password = False


@dataclass(frozen=True)
class Config:
    """Runtime configuration for OpenKP."""

    username: str
    password: str
    data_dir: Path
    log_level: str
    # True when OPENKP_INTERACTIVE_LOGIN is set. password is then always "".
    interactive_login: bool = False


def load_config() -> Config:
    """Load config from environment and/or keychain. Raises if required values missing."""
    load_dotenv()

    interactive_login = _env_flag(INTERACTIVE_LOGIN_ENV)
    username = os.getenv("KP_USERNAME", "").strip()
    if not username and not interactive_login:
        raise RuntimeError(
            "KP_USERNAME is not set. Copy .env.example to .env and fill in your username."
        )

    if interactive_login:
        # Never read a stored password in this mode, not even one left in .env.
        global _warned_ignored_password
        if os.getenv("KP_PASSWORD", "").strip() and not _warned_ignored_password:
            _warned_ignored_password = True
            logger.warning(
                "%s is on, so KP_PASSWORD is ignored. Remove it from .env.",
                INTERACTIVE_LOGIN_ENV,
            )
        password = ""
        # Keep it out of the process environment too, so the Chromium child
        # process doesn't inherit it.
        os.environ.pop("KP_PASSWORD", None)
    else:
        password = os.getenv("KP_PASSWORD", "").strip()
        if not password:
            password = _load_password_from_keyring(username)
    if not password and not interactive_login:
        raise RuntimeError(
            "No password found. Set KP_PASSWORD in .env, or store it in the system keyring:\n"
            f"    python -c 'import keyring; keyring.set_password(\"{KEYRING_SERVICE}\", "
            f"\"{username}\", \"YOUR_PASSWORD\")'"
        )

    # Treat empty-string .env values as unset, so the defaults below apply.
    data_dir_str = os.getenv("OPENKP_DATA_DIR", "").strip() or "~/.openkp"
    data_dir = Path(data_dir_str).expanduser()
    data_dir.mkdir(parents=True, exist_ok=True)

    log_level = (os.getenv("OPENKP_LOG_LEVEL", "").strip() or "INFO").upper()

    return Config(
        username=username,
        password=password,
        data_dir=data_dir,
        log_level=log_level,
        interactive_login=interactive_login,
    )


def _env_flag(name: str) -> bool:
    """Truthy env values: "1", "true", "yes", "on" (case-insensitive)."""
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _load_password_from_keyring(username: str) -> str:
    """Read password from the system keyring. Returns empty string if not found."""
    try:
        import keyring

        pw = keyring.get_password(KEYRING_SERVICE, username)
        return pw or ""
    except Exception as exc:  # keyring backend may be missing on some systems
        logger.debug("Keyring unavailable: %s", exc)
        return ""
