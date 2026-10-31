"""Shared test helpers: paths, the CLI runner (stdout and stderr captured), an environment patcher, the fake keychain
the offline test base class with its socket guard, and the loopback YouTube, Vimeo and TikTok mock fixtures. Nothing here is part of the
package.

No test may contact the network (ADR-001, ADR-004). OfflineTestCase patches socket connect, create_connection and
getaddrinfo for every test so that the only address a test can reach is 127.0.0.1 on the port of a mock server the
test itself started (LOOPBACK_PORTS); anything else, Google, Vimeo and TikTok included, raises NetworkAttempted, a BaseException, so no
`except Exception` in the package or the CLI can turn it into an error document: the test fails.

No test may reach a real OS keychain (ADR-003). Importing this module replaces `keychain._import_keyring`, the
package's only import of keyring, with a guard that raises RealKeychainTouched (a BaseException, so no `except
Exception` in the package can swallow it), for the whole test process. OfflineTestCase then gives each test a fresh
FakeKeychain. CI checks that `KeychainGuardTests.test_the_fake_keychain_is_what_runs` ran and passed."""
from __future__ import annotations

import io
import json
import os
import shutil
import socket
import sys
import tempfile
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Set, Tuple
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from publish_skill import cli  # noqa: E402
from publish_skill import credentials as creds  # noqa: E402
from publish_skill import keychain  # noqa: E402
from publish_skill.platforms import tiktok, vimeo, youtube  # noqa: E402

from mock_tiktok import MockTikTok  # noqa: E402
from mock_vimeo import MockVimeo  # noqa: E402
from mock_youtube import MockYouTube  # noqa: E402

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


# The loopback ports of the mock servers running now: the only addresses a test may connect to.
LOOPBACK_PORTS: Set[int] = set()
LOOPBACK_HOST = "127.0.0.1"
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_CREATE_CONNECTION = socket.create_connection
_REAL_GETADDRINFO = socket.getaddrinfo


class NetworkAttempted(BaseException):
    """Raised when code under test connects (or looks up a name) anywhere but a loopback mock server it started."""


def _mock_address(host: Any, port: Any) -> bool:
    return host == LOOPBACK_HOST and port in LOOPBACK_PORTS


def _guard_connect(sock, address, *a, **k):
    if isinstance(address, tuple) and len(address) >= 2 and _mock_address(address[0], address[1]):
        return _REAL_CONNECT(sock, address)
    raise NetworkAttempted(f"network access attempted: {address!r}")


def _guard_connect_ex(sock, address, *a, **k):
    if isinstance(address, tuple) and len(address) >= 2 and _mock_address(address[0], address[1]):
        return _REAL_CONNECT_EX(sock, address)
    raise NetworkAttempted(f"network access attempted: {address!r}")


def _guard_create_connection(address, *a, **k):
    if _mock_address(address[0], address[1]):
        return _REAL_CREATE_CONNECTION(address, *a, **k)
    raise NetworkAttempted(f"network access attempted: {address!r}")


def _guard_getaddrinfo(host, port, *a, **k):
    if _mock_address(host, port):
        return _REAL_GETADDRINFO(host, port, *a, **k)
    raise NetworkAttempted(f"name lookup attempted: {host!r}")


class OfflineTestCase(unittest.TestCase):
    """Fails the test on any socket connect or name lookup except to a loopback mock server the test started, gives each test a fresh fake keychain (self.keychain; its
    backend is self.keychain_backend) with PUBLISH_KEYCHAIN unset, and a temporary directory holding a small file named
    final.mp4."""

    def setUp(self):
        for target, guard in (("socket.socket.connect", _guard_connect), ("socket.socket.connect_ex", _guard_connect_ex),
                              ("socket.create_connection", _guard_create_connection),
                              ("socket.getaddrinfo", _guard_getaddrinfo)):
            p = mock.patch(target, guard)
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


def youtube_mock(test: unittest.TestCase, secrets: Dict[str, str], chunk_size: int = 256 * 1024,
                 **kw: Any) -> MockYouTube:
    """Start a MockYouTube on 127.0.0.1 for this test and point the YouTube uploader at it: its endpoints, a 256 KiB
    chunk, and backoff that records its waits (test.waits) instead of sleeping. Stopped when the test ends."""
    m = MockYouTube(secrets["PUBLISH_YOUTUBE_CLIENT_ID"], secrets["PUBLISH_YOUTUBE_CLIENT_SECRET"],
                    secrets["PUBLISH_YOUTUBE_REFRESH_TOKEN"], **kw).start(LOOPBACK_PORTS)
    test.addCleanup(m.stop)
    test.waits = []
    for p in (mock.patch.object(youtube.YouTubeUploader, "token_url", m.url + "/token"),
              mock.patch.object(youtube.YouTubeUploader, "api_base", m.url),
              mock.patch.object(youtube.YouTubeUploader, "chunk_size", chunk_size),
              mock.patch.object(youtube, "_sleep", test.waits.append),
              mock.patch.object(youtube, "_rand", lambda: 0.5)):
        p.start()
        test.addCleanup(p.stop)
    return m


def vimeo_mock(test: unittest.TestCase, token: str, chunk_size: int = 256 * 1024, **kw: Any) -> MockVimeo:
    """Start a MockVimeo on 127.0.0.1 for this test and point the Vimeo uploader at it: its API base, a 256 KiB chunk,
    and backoff that records its waits (test.waits) instead of sleeping. Stopped when the test ends."""
    m = MockVimeo(token, **kw).start(LOOPBACK_PORTS)
    test.addCleanup(m.stop)
    test.waits = []
    for p in (mock.patch.object(vimeo.VimeoUploader, "api_base", m.url),
              mock.patch.object(vimeo.VimeoUploader, "chunk_size", chunk_size),
              mock.patch.object(vimeo, "_sleep", test.waits.append),
              mock.patch.object(vimeo, "_rand", lambda: 0.5)):
        p.start()
        test.addCleanup(p.stop)
    return m


def tiktok_mock(test: unittest.TestCase, token: str, chunk_size: int = 256 * 1024, **kw: Any) -> MockTikTok:
    """Start a MockTikTok on 127.0.0.1 for this test and point the TikTok uploader at it: its API base, a 256 KiB chunk,
    and backoff and status polling that record their waits (test.waits) instead of sleeping. Stopped when the test
    ends."""
    m = MockTikTok(token, **kw).start(LOOPBACK_PORTS)
    test.addCleanup(m.stop)
    test.waits = []
    for p in (mock.patch.object(tiktok.TikTokUploader, "api_base", m.url),
              mock.patch.object(tiktok.TikTokUploader, "chunk_size", chunk_size),
              mock.patch.object(tiktok, "_sleep", test.waits.append),
              mock.patch.object(tiktok, "_rand", lambda: 0.5)):
        p.start()
        test.addCleanup(p.stop)
    return m


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
