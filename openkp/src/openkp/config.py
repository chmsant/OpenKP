"""Configuration loader for OpenKP.

Reads credentials from, in priority order:
1. Environment variables (or .env file)
2. macOS Keychain / system keyring (service: "openkp", account: username)

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


@dataclass(frozen=True)
class Config:
    """Runtime configuration for OpenKP."""

    username: str
    password: str
    data_dir: Path
    log_level: str
    # Optional human label for this account's process, e.g. "alex". Shown by
    # whoami so it's obvious which kp.org account a tool family talks to.
    profile: str = ""


def load_config() -> Config:
    """Load config from environment and/or keychain. Raises if required values missing."""
    load_dotenv()

    username = os.getenv("KP_USERNAME", "").strip()
    if not username:
        raise RuntimeError(
            "KP_USERNAME is not set. Copy .env.example to .env and fill in your username."
        )

    password = os.getenv("KP_PASSWORD", "").strip()
    if not password:
        password = _load_password_from_keyring(username)
    if not password:
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
    profile = os.getenv("OPENKP_PROFILE", "").strip()

    return Config(
        username=username,
        password=password,
        data_dir=data_dir,
        log_level=log_level,
        profile=profile,
    )


def _load_password_from_keyring(username: str) -> str:
    """Read password from the system keyring. Returns empty string if not found."""
    try:
        import keyring

        pw = keyring.get_password(KEYRING_SERVICE, username)
        return pw or ""
    except Exception as exc:  # keyring backend may be missing on some systems
        logger.debug("Keyring unavailable: %s", exc)
        return ""
