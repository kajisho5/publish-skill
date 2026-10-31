"""Credentials: from the environment or the OS keychain, never from a file, a flag or the request, and never printed
(ADR-001, ADR-003).

Precedence, per platform: if any of the platform's variables is set in the environment, the environment is the only
source for that platform and the keychain is not consulted; otherwise the keychain (keychain.py) is. The values of one
platform therefore always come from one place, so an OAuth client id from one source is never paired with a refresh
token from another.

Reports say whether each variable is `present` or `absent`, in the environment and in the keychain, never its value or
length. `redact` replaces any credential value with "[REDACTED]" in a document before the CLI prints it, so a value
that slipped into a message by mistake still does not reach stdout or stderr. It covers every credential variable in
the environment, as before, and every value this process has read from the keychain or been given to store in it. It
never reads the keychain itself: a value this process never held cannot be in its output."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional, Set

from . import keychain
from .errors import PublishError
from .platforms import get_platform, platform_ids

REDACTED = "[REDACTED]"
SOURCES = ("environment", "keychain")
PRECEDENCE = ("per platform: the environment when any of the platform's variables is set there, otherwise the OS "
              "keychain; one platform's values never mix sources")
_HELD: Set[str] = set()          # keychain values this process has read or been given (for redact)


class Credentials:
    """The values a platform needs, and where they came from. Its repr and str never show a value."""

    def __init__(self, platform: str, values: Mapping[str, str], source: str = "environment") -> None:
        self.platform = platform
        self.source = source
        self._values = dict(values)

    def get(self, name: str) -> str:
        return self._values[name]

    def names(self) -> List[str]:
        return sorted(self._values)

    def __repr__(self) -> str:
        return f"Credentials(platform={self.platform!r}, source={self.source!r}, names={self.names()!r}, values={REDACTED})"

    __str__ = __repr__


def hold(value: str) -> None:
    """Record a credential value this process holds, so `redact` removes it from every output."""
    if value:
        _HELD.add(value)


def _env_value(name: str, environ: Optional[Mapping[str, str]]) -> str:
    return ((os.environ if environ is None else environ).get(name) or "").strip()


def _keychain_value(kc: keychain.Keychain, name: str) -> str:
    value = kc.get(name)
    hold(value)
    return value


def env_present(platform_id: str, environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """The platform's variables that are set in the environment (names only)."""
    return [n for n in get_platform(platform_id).credential_env if _env_value(n, environ)]


def _presence(values: Mapping[str, str]) -> Dict[str, str]:
    return {n: ("present" if v else "absent") for n, v in values.items()}


def status(platform_id: str, environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """Which source would be used and which variables it has, as present / absent only."""
    names = get_platform(platform_id).credential_env
    env = {n: _env_value(n, environ) for n in names}
    kc_report: Dict[str, Any] = {"status": "UNAVAILABLE", "backend": None, "variables": None}
    kc_vals: Dict[str, str] = {}
    kc = keychain.active()
    if kc is None:
        kc_report["reason"] = keychain.availability()["reason"]
    else:
        kc_report.update(status="OK", backend=kc.name)
        try:
            kc_vals = {n: _keychain_value(kc, n) for n in names}
            kc_report["variables"] = _presence(kc_vals)
        except PublishError as e:
            kc_report.update(status="ERROR", reason=e.message)
            kc_vals = {}
    if any(env.values()):
        source: Optional[str] = "environment"
        chosen = env
    elif any(kc_vals.values()):
        source, chosen = "keychain", kc_vals
    else:
        source, chosen = None, {n: "" for n in names}
    out = {"source": source, "variables": _presence(chosen), "complete": all(chosen.values()),
           "environment": _presence(env), "keychain": kc_report}
    if source == "environment" and any(kc_vals.values()):
        out["note"] = "the keychain is not consulted while any of this platform's variables is set in the environment"
    return out


def load(platform_id: str, environ: Optional[Mapping[str, str]] = None) -> Credentials:
    """The platform's credentials from one source (see PRECEDENCE), or CREDENTIALS_MISSING naming the variables."""
    names = get_platform(platform_id).credential_env
    login = f"publish auth login --platform {platform_id}"
    if env_present(platform_id, environ):
        missing = [n for n in names if not _env_value(n, environ)]
        if missing:
            raise PublishError("CREDENTIALS_MISSING", f"set {', '.join(missing)} in the environment too; while any of this "
                               "platform's variables is set there, the keychain is not consulted (values are never printed)",
                               {"platform": platform_id, "missing": missing, "source": "environment"})
        return Credentials(platform_id, {n: _env_value(n, environ) for n in names}, "environment")
    kc = keychain.active()
    if kc is None:
        raise PublishError("CREDENTIALS_MISSING", f"set {', '.join(names)} in the environment; the OS keychain is "
                           f"unavailable ({keychain.availability()['reason']}) (values are never printed)",
                           {"platform": platform_id, "missing": list(names), "source": None})
    values = {n: _keychain_value(kc, n) for n in names}
    missing = [n for n in names if not values[n]]
    if missing:
        found = len(missing) < len(names)
        raise PublishError("CREDENTIALS_MISSING", f"{', '.join(missing)} not found in the {kc.name}; store "
                           + ("them" if found else "the credentials") + f" with `{login}` or set every variable in the "
                           "environment (values are never printed)",
                           {"platform": platform_id, "missing": missing, "source": "keychain" if found else None})
    return Credentials(platform_id, values, "keychain")


def secret_values(environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """Every non-empty credential value of every registered platform in the environment, and every keychain value
    this process holds, longest first. Never reads the keychain."""
    vals = {_env_value(n, environ) for p in platform_ids() for n in get_platform(p).credential_env} | _HELD
    vals.discard("")
    return sorted(vals, key=len, reverse=True)


def redact(obj: Any, secrets: Optional[List[str]] = None) -> Any:
    """`obj` with every credential value inside its string values replaced by REDACTED, however short the value.
    Keys are left alone: they come from this skill's code, never from the environment, and the CLI reads them."""
    secrets = secret_values() if secrets is None else secrets
    if not secrets:
        return obj
    if isinstance(obj, str):
        for s in secrets:
            obj = obj.replace(s, REDACTED)
        return obj
    if isinstance(obj, dict):
        return {k: redact(v, secrets) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact(v, secrets) for v in obj]
    return obj
