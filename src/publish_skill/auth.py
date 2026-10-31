"""`publish auth status|login|logout`: the platform credentials in the OS keychain (ADR-003).

`login` stores values the user already has (YouTube: the OAuth client id and secret, and a refresh token). It reads
them from stdin, one per line in the order of the platform's variables (or of `--variable`), or, when stdin is a
terminal, from a prompt that does not echo. There is no flag that takes a value: a value on the command line would
reach shell history and process listings. Every value is read and checked before the first one is stored.

Every refusal (unknown platform, unknown variable, keychain unavailable) happens before any credential is read. No
command prints a value: reports name the variables and say present / absent, and every document passes through
credentials.redact, which also knows the values `login` was given. Nothing here opens a network connection; obtaining
a refresh token through Google's consent screen needs one and is not part of this step."""
from __future__ import annotations

import getpass
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence, TextIO

from . import SKILL_ID, __version__
from . import credentials as creds
from . import keychain
from .errors import PublishError
from .platforms import get_platform, platform_ids


def _names(platform_id: str, variables: Optional[Sequence[str]]) -> List[str]:
    allowed = get_platform(platform_id).credential_env              # UNKNOWN_PLATFORM first
    names = list(variables) if variables else list(allowed)
    unknown = [n for n in names if n not in allowed]
    if unknown:
        raise PublishError("INVALID_ARGUMENT", f"not a {platform_id} credential variable: {', '.join(unknown)}",
                           {"platform": platform_id, "unknown": unknown, "known": list(allowed)})
    if len(set(names)) != len(names):
        raise PublishError("INVALID_ARGUMENT", "a --variable is named twice", {"platform": platform_id, "variables": names})
    return names


def _read_values(names: List[str], stream: Optional[TextIO], prompt: Callable[..., str]) -> Dict[str, str]:
    if stream is None:
        raise PublishError("INVALID_ARGUMENT", "no stdin to read the values from; nothing was stored", {"variables": names})
    values: Dict[str, str] = {}
    interactive = bool(getattr(stream, "isatty", lambda: False)())
    for i, n in enumerate(names):
        if interactive:
            raw = prompt(f"{n} (input hidden): ", stream=sys.stderr)
        else:
            raw = stream.readline()
            if raw == "":
                raise PublishError("INVALID_ARGUMENT", f"stdin ended after {i} of {len(names)} values (one per line, in the "
                                   f"order {', '.join(names)}); nothing was stored", {"variables": names, "read": i})
        value = raw.rstrip("\r\n").strip()
        creds.hold(value)
        if not value:
            raise PublishError("INVALID_ARGUMENT", f"empty value for {n}; nothing was stored", {"variable": n})
        values[n] = value
    return values


def _doc(kind: str, platform_id: Optional[str], **fields: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"ok": True, "skill": {"id": SKILL_ID, "version": __version__}, "auth": kind}
    if platform_id is not None:
        out["platform"] = platform_id
    out.update(fields)
    return out


def status(platform_id: Optional[str] = None) -> Dict[str, Any]:
    pids = [get_platform(platform_id).id] if platform_id else platform_ids()
    return _doc("status", platform_id, keychain=keychain.availability(), precedence=creds.PRECEDENCE,
                platforms={pid: creds.status(pid) for pid in pids})


def login(platform_id: str, variables: Optional[Sequence[str]] = None, stdin: Optional[TextIO] = None,
          prompt: Optional[Callable[..., str]] = None) -> Dict[str, Any]:
    names = _names(platform_id, variables)
    kc = keychain.require()                                          # KEYCHAIN_UNAVAILABLE before anything is read
    values = _read_values(names, sys.stdin if stdin is None else stdin, prompt or getpass.getpass)
    stored: List[str] = []
    for n in names:
        try:
            kc.set(n, values[n])
        except PublishError as e:
            e.details["stored"] = list(stored)
            raise
        stored.append(n)
    shadowed = creds.env_present(platform_id)
    warnings = [f"the environment sets {', '.join(shadowed)}: while any of this platform's variables is set there, the "
                "keychain is not consulted"] if shadowed else []
    return _doc("login", platform_id, backend=kc.name, service=keychain.SERVICE, stored=stored,
                shadowed_by_environment=shadowed, warnings=warnings, credentials=creds.status(platform_id))


def logout(platform_id: str, variables: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    names = _names(platform_id, variables)
    kc = keychain.require()
    removed = [n for n in names if kc.delete(n)]
    still = creds.env_present(platform_id)
    warnings = [f"the environment still sets {', '.join(still)}; unset it there"] if still else []
    return _doc("logout", platform_id, backend=kc.name, service=keychain.SERVICE, removed=removed,
                not_found=[n for n in names if n not in removed], environment_still_set=still, warnings=warnings,
                credentials=creds.status(platform_id))
