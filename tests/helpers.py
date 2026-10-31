"""Shared test helpers: paths, the CLI runner (stdout and stderr captured) and an environment patcher. Nothing here
is part of the package."""
from __future__ import annotations

import io
import json
import os
import sys
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from publish_skill import cli  # noqa: E402


def _dir_from_env(key: str, sibling: str, marker: str) -> Optional[Path]:
    for c in ([Path(os.environ[key])] if os.environ.get(key) else []) + [ROOT.parent / sibling]:
        if (c / marker).is_file():
            return c
    return None


OS_DIR = _dir_from_env("AI_VIDEO_PRODUCTION_OS_DIR", "AI-video-production-OS", "registry/conformance.py")


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
