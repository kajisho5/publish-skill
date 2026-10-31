"""Shared test helpers: paths, the CLI runner (stdout and stderr captured), an environment patcher, the fake keychain
and the offline test base class. Nothing here is part of the package.

No test may reach a real OS keychain (ADR-003). Importing this module replaces `keychain._import_keyring`, the
package's only import of keyring, with a guard that raises RealKeychainTouched (a BaseException, so no `except
Exception` in the package can swallow it), for the whole test process. OfflineTestCase then gives each test a fresh
FakeKeychain. CI checks that `KeychainGuardTests.test_the_fake_keychain_is_what_runs` ran and passed."""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from publish_skill import cli  # noqa: E402
from publish_skill import credentials as creds  # noqa: E402
from publish_skill import keychain  # noqa: E402

FAKE_BACKEND_NAME = "fake keychain (tests)"


class RealKeychainTouched(BaseException):
    """Raised when code under test tries to import keyring, i.e. to reach a real OS keychain."""


def _real_keychain_guard(*_a, **_k):
    raise RealKeychainTouched("a test tried to reach the real OS keychain (keychain._import_keyring)")


REAL_IMPORT_KEYRING = keychain._import_keyring
REAL_DISCOVER = keychain._discover
keychain._import_keyring = _real_keychain_guard        # for the whole test process; never restored


class FakeBackend:
    """In-memory stand-in for a keyring backend: the same three methods. `fail` names operations that raise, and
    `calls` records each operation (never a value)."""

    def __init__(self) -> None:
        self.items: Dict[Tuple[str, str], str] = {}
        self.fail: set = set()
        self.calls: list = []

    def _op(self, op: str, service: str, account: str) -> None:
        self.calls.append((op, service, account))
        if op in self.fail:
            raise RuntimeError(f"fake keychain: {op} refused")

    def get_password(self, service: str, account: str) -> Optional[str]:
        self._op("get", service, account)
        return self.items.get((service, account))

    def set_password(self, service: str, account: str, value: str) -> None:
        self._op("set", service, account)
        self.items[(service, account)] = value

    def delete_password(self, service: str, account: str) -> None:
        self._op("delete", service, account)
        del self.items[(service, account)]


def fake_keychain() -> "keychain.Keychain":
    return keychain.Keychain(FakeBackend(), FAKE_BACKEND_NAME, "fake")


def _dir_from_env(key: str, sibling: str, marker: str) -> Optional[Path]:
    for c in ([Path(os.environ[key])] if os.environ.get(key) else []) + [ROOT.parent / sibling]:
        if (c / marker).is_file():
            return c
    return None


OS_DIR = _dir_from_env("AI_VIDEO_PRODUCTION_OS_DIR", "AI-video-production-OS", "registry/conformance.py")
# a real ffmpeg-skill checkout, for the integration tests of `publish plan` (CI's conformance job provides one)
FFMPEG_SKILL_DIR = _dir_from_env("PUBLISH_FFMPEG_SKILL_DIR", "ffmpeg-skill", "scripts/_contract.py")


def _no_network(*_a, **_k):
    raise AssertionError("network access attempted")


class OfflineTestCase(unittest.TestCase):
    """Fails the test on any socket connect or name lookup, gives each test a fresh fake keychain (self.keychain; its
    backend is self.keychain_backend) with PUBLISH_KEYCHAIN unset, and a temporary directory holding a small file named
    final.mp4."""

    def setUp(self):
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection", "socket.getaddrinfo"):
            p = mock.patch(target, _no_network)
            p.start()
            self.addCleanup(p.stop)
        self.keychain = fake_keychain()
        self.keychain_backend = self.keychain._backend
        for p in (mock.patch.object(keychain, "_discover", lambda: self.keychain),
                  mock.patch.object(creds, "_HELD", set()),
                  mock.patch.dict(os.environ)):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop(keychain.OFF_ENV, None)
        self.tmp = tempfile.mkdtemp(prefix="publish-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.video = os.path.join(self.tmp, "final.mp4")
        with open(self.video, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 8)


def run_cli_raw(*argv: str) -> Tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


def run_cli(*argv: str) -> Tuple[int, Dict[str, Any]]:
    rc, out, _ = run_cli_raw(*argv)
    return rc, json.loads(out)


@contextmanager
def env(**values: Optional[str]) -> Iterator[None]:
    old = {k: os.environ.get(k) for k in values}
    try:
        for k, v in values.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
