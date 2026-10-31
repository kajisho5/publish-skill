"""The OS keychain (ADR-003): macOS Keychain, Windows Credential Manager, or the Secret Service on Linux.

It is reached through the third-party `keyring` library, an optional extra (`pip install 'publish-skill[keychain]'`,
`keyring>=25.0`, MIT). Without it the keychain is UNAVAILABLE, credentials come from the environment only, and
`doctor` says so. `PUBLISH_KEYCHAIN=off` turns the keychain off the same way (headless machines, CI).

Only the three OS-native backends are used, chosen here by their viability and priority. keyring's own backend
selection (`keyring.get_keyring()`, `PYTHON_KEYRING_BACKEND`, keyringrc.cfg, third-party backends found through entry
points) is never consulted: a plug-in backend could keep secrets in a plain file or reach the network, and this skill
reads credentials from neither. The Secret Service is local D-Bus IPC, not a network connection.

Each credential is an item of the service SERVICE whose account (user name) is the environment variable's name, e.g.
service "publish-skill", account "PUBLISH_YOUTUBE_REFRESH_TOKEN".

`_import_keyring` is the only place that imports keyring; the tests replace it, so they never reach a real keychain.
Values are returned to credentials.py only, which records them for the redactor; nothing here prints, logs or puts a
value into an error."""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from .errors import PublishError

SERVICE = "publish-skill"
EXTRA = "keychain"
REQUIREMENT = "keyring>=25.0"
INSTALL = "pip install 'publish-skill[keychain]'"
OFF_ENV = "PUBLISH_KEYCHAIN"
OFF_VALUES = ("0", "off", "false", "no")
BACKEND_LABELS = ("macOS Keychain", "Windows Credential Manager", "Secret Service (Linux, D-Bus)")


class KeychainUnavailable(Exception):
    def __init__(self, reason: str, install: Optional[str] = None, library: Optional[str] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.install = install
        self.library = library


class Keychain:
    """One keychain backend. `backend` has keyring's three methods: get_password(service, account),
    set_password(service, account, value) and delete_password(service, account)."""

    def __init__(self, backend: Any, name: str, library: str) -> None:
        self._backend = backend
        self.name = name
        self.library = library

    def _fail(self, op: str, account: str, e: Exception) -> PublishError:
        # the exception's type only: a backend message is not ours to vouch for
        return PublishError("KEYCHAIN_ERROR", f"the {self.name} refused to {op} {account} ({type(e).__name__})",
                            {"backend": self.name, "operation": op, "variable": account})

    def get(self, account: str) -> str:
        """The stored value, stripped; "" when there is none."""
        try:
            value = self._backend.get_password(SERVICE, account)
        except Exception as e:
            raise self._fail("read", account, e) from None
        return (value or "").strip()

    def set(self, account: str, value: str) -> None:
        try:
            self._backend.set_password(SERVICE, account, value)
        except Exception as e:
            raise self._fail("store", account, e) from None

    def delete(self, account: str) -> bool:
        """True when an item was removed, False when there was none."""
        if not self.get(account):
            return False
        try:
            self._backend.delete_password(SERVICE, account)
        except Exception as e:
            raise self._fail("remove", account, e) from None
        return True


def _import_keyring() -> Optional[Dict[str, Any]]:
    """keyring's version and its OS-native backend classes, or None when keyring is not installed. The only import
    of keyring in this package."""
    try:
        import keyring
    except ImportError:
        return None
    try:
        from importlib.metadata import version
        ver = version(keyring.__name__)
    except Exception:
        ver = "unknown"
    classes: List[Tuple[str, Any]] = []
    try:
        from keyring.backends import macOS
        classes.append((BACKEND_LABELS[0], macOS.Keyring))
    except ImportError:
        pass
    try:
        from keyring.backends import Windows
        classes.append((BACKEND_LABELS[1], Windows.WinVaultKeyring))
    except ImportError:
        pass
    try:
        from keyring.backends import SecretService
        classes.append((BACKEND_LABELS[2], SecretService.Keyring))
    except ImportError:
        pass
    return {"version": ver, "backends": classes}


def _select(lib: Dict[str, Any]) -> Keychain:
    """The viable OS-native backend with the highest priority."""
    library = f"keyring {lib['version']}"
    best: Optional[Tuple[float, str, Any]] = None
    for label, cls in lib["backends"]:
        try:
            if not cls.viable:
                continue
            prio = float(cls.priority)
        except Exception:
            continue
        if best is None or prio > best[0]:
            best = (prio, label, cls)
    if best is None:
        raise KeychainUnavailable("keyring is installed, but no OS keychain is usable here (" + ", ".join(BACKEND_LABELS)
                                  + "); on Linux the Secret Service needs a D-Bus session and an unlocked keyring",
                                  library=library)
    return Keychain(best[2](), best[1], library)


def _discover() -> Keychain:
    lib = _import_keyring()
    if lib is None:
        raise KeychainUnavailable("the optional keyring library is not installed", install=INSTALL)
    return _select(lib)


def disabled() -> bool:
    return (os.environ.get(OFF_ENV) or "").strip().lower() in OFF_VALUES


def _resolve() -> Keychain:
    if disabled():
        raise KeychainUnavailable(f"turned off by {OFF_ENV}={os.environ.get(OFF_ENV, '').strip()}")
    return _discover()


def active() -> Optional[Keychain]:
    """The keychain, or None when it is unavailable. Reads no credential."""
    try:
        return _resolve()
    except KeychainUnavailable:
        return None


def require() -> Keychain:
    """The keychain, or KEYCHAIN_UNAVAILABLE with the reason and how to get it. Reads no credential."""
    try:
        return _resolve()
    except KeychainUnavailable as e:
        raise PublishError("KEYCHAIN_UNAVAILABLE", f"the OS keychain is unavailable: {e.reason}; credentials come from "
                           "the environment only" + (f" (install it: {e.install})" if e.install else ""),
                           {"reason": e.reason, "install": e.install, "library": e.library}) from None


def availability() -> Dict[str, Any]:
    """Whether the keychain can be used, which backend, and why not. Reads no credential."""
    base = {"service": SERVICE, "account": "the environment variable's name", "extra": EXTRA, "requirement": REQUIREMENT}
    try:
        kc = _resolve()
    except KeychainUnavailable as e:
        return dict(base, status="UNAVAILABLE", backend=None, library=e.library, reason=e.reason, install=e.install,
                    fallback="credentials come from the environment only")
    return dict(base, status="OK", backend=kc.name, library=kc.library, reason=None, install=None, fallback=None)
