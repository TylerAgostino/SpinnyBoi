# pyright: basic
"""Shared env-var parsing behind the repo's ``from_env`` convention.

Every feature config (WebLookupConfig, SimRacerHubConfig, VectorStoreConfig)
parses the same shapes out of the environment: an enabled flag spelled with
false-words, and clamped ints/floats that fall back to the default on
garbage. Those shapes live here so a parsing fix lands in one place."""
import logging
from typing import Dict, Optional

log = logging.getLogger(__name__)

FALSE_VALUES = {"0", "false", "off", "no"}


class MissingEnvError(Exception):
    """A required environment variable is unset or blank; args[0] is the key."""


def require_env(env: Dict[str, str], key: str) -> str:
    """Return the env value for ``key``; raise ``MissingEnvError(key)`` when unset or blank."""
    raw = env.get(key) or ""
    if not raw.strip():
        raise MissingEnvError(key)
    return raw


def env_str(env: Dict[str, str], key: str, default: str) -> str:
    """Env string; default when unset/blank (compose injects empty strings)."""
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw


def env_enabled(env: Dict[str, str], key: str, default: bool = True) -> bool:
    """True unless the env value spells a false word (case-insensitive)."""
    raw = (env.get(key) or "").strip().lower()
    if not raw:
        return default
    return raw not in FALSE_VALUES


def env_int(
    env: Dict[str, str],
    key: str,
    default: int,
    minimum: int,
    maximum: Optional[int] = None,
) -> int:
    """Env int clamped to [minimum, maximum]; default on unset/garbage."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("Invalid %s=%r, using default %s", key, raw, default)
        return default
    if value < minimum:
        log.warning("%s=%s below minimum %s, clamping", key, value, minimum)
        return minimum
    if maximum is not None and value > maximum:
        log.warning("%s=%s above maximum %s, clamping", key, value, maximum)
        return maximum
    return value


def env_int_or_none(env: Dict[str, str], key: str) -> Optional[int]:
    """Env int or None (unset/garbage both yield None)."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid %s=%r, ignoring", key, raw)
        return None


def env_float(
    env: Dict[str, str], key: str, default: float, minimum: float, maximum: float
) -> float:
    """Env float clamped to [minimum, maximum]; default on unset/garbage."""
    raw = (env.get(key) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("Invalid %s=%r, using default %s", key, raw, default)
        return default
    return max(minimum, min(maximum, value))
