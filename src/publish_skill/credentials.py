"""Credentials: read from the environment only, never from a file, a flag or the request, and never printed (ADR-001).

Reports say whether each variable is `present` or `absent`, never its value or length. `redact` replaces any
credential value with "[REDACTED]" in a document before the CLI prints it, so a value that slipped into a message
by mistake still does not reach stdout or stderr. Roadmap step 3 adds the OS keychain (`publish auth login`)."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional

from .errors import PublishError
from .platforms import get_platform, platform_ids

REDACTED = "[REDACTED]"
SOURCE = "environment"


class Credentials:
    """The values a platform needs. Its repr and str never show a value."""

    def __init__(self, platform: str, values: Mapping[str, str]) -> None:
        self.platform = platform
        self._values = dict(values)

    def get(self, name: str) -> str:
        return self._values[name]

    def names(self) -> List[str]:
        return sorted(self._values)

    def __repr__(self) -> str:
        return f"Credentials(platform={self.platform!r}, names={self.names()!r}, values={REDACTED})"

    __str__ = __repr__


def _value(name: str, environ: Optional[Mapping[str, str]]) -> str:
    return ((os.environ if environ is None else environ).get(name) or "").strip()


def status(platform_id: str, environ: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    names = get_platform(platform_id).credential_env
    variables = {n: ("present" if _value(n, environ) else "absent") for n in names}
    return {"source": SOURCE, "variables": variables, "complete": all(v == "present" for v in variables.values())}


def load(platform_id: str, environ: Optional[Mapping[str, str]] = None) -> Credentials:
    names = get_platform(platform_id).credential_env
    missing = [n for n in names if not _value(n, environ)]
    if missing:
        raise PublishError("CREDENTIALS_MISSING", f"set {', '.join(missing)} in the environment (values are never printed)",
                           {"platform": platform_id, "missing": missing, "source": SOURCE})
    return Credentials(platform_id, {n: _value(n, environ) for n in names})


def secret_values(environ: Optional[Mapping[str, str]] = None) -> List[str]:
    """Every non-empty credential value of every registered platform, longest first."""
    vals = {_value(n, environ) for p in platform_ids() for n in get_platform(p).credential_env}
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
