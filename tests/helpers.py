"""Shared test helpers: paths, the CLI runner (stdout and stderr captured), an environment patcher and the offline
test base class. Nothing here is part of the package."""
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
    """Fails the test on any socket connect or name lookup, and gives each test a temporary directory holding a small
    file named final.mp4."""

    def setUp(self):
        for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.create_connection", "socket.getaddrinfo"):
            p = mock.patch(target, _no_network)
            p.start()
            self.addCleanup(p.stop)
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
