"""Upload metadata: title, description, tags, privacy, thumbnail and captions (ADR-002).

`publish plan` and `publish upload` take the same flags and build the same Metadata from them, so the document a
plan checked is the document a dry run prints and the confirmation token binds. The thumbnail and the captions are
bound by their bytes (sha256 and size), not their path, like the deliverable: replacing the image after the dry run
needs a new token.

Privacy defaults to `private`. The YouTube API defaults to `public` when the field is left out; this skill never
leaves it out, so a forgotten flag cannot publish a video to everyone."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .errors import PublishError

DEFAULT_PRIVACY = "private"
_CHUNK = 1 << 20


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(_CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def file_facts(path: str, what: str) -> Dict[str, Any]:
    """Path, size and sha256 of a regular file, or INPUT_NOT_FOUND naming which input it was."""
    if not path or not os.path.isfile(path):
        raise PublishError("INPUT_NOT_FOUND", f"{what} is not a regular file: {path}", {"path": path, "input": what})
    return {"path": os.path.abspath(path), "size": os.path.getsize(path), "sha256": sha256_file(path)}


@dataclass(frozen=True)
class Metadata:
    title: Optional[str] = None
    description: Optional[str] = None
    tags: List[str] = field(default_factory=list)
    privacy: str = DEFAULT_PRIVACY
    thumbnail: Optional[Dict[str, Any]] = None          # file_facts()
    captions: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "description": self.description, "tags": list(self.tags), "privacy": self.privacy,
                "thumbnail": dict(self.thumbnail) if self.thumbnail else None, "captions": [dict(c) for c in self.captions]}

    def canonical(self) -> str:
        """The text the confirmation token hashes: every field, files by content (sha256, size) and never by path,
        in a fixed key order. Two Metadata that would send the same thing to the platform give the same text."""
        doc = {"title": self.title, "description": self.description, "tags": list(self.tags), "privacy": self.privacy,
               "thumbnail": {"sha256": self.thumbnail["sha256"], "size": self.thumbnail["size"]} if self.thumbnail else None,
               "captions": [{"sha256": c["sha256"], "size": c["size"]} for c in self.captions]}
        return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


DEFAULT_METADATA = Metadata()


def build(title: Optional[str] = None, description: Optional[str] = None, description_file: Optional[str] = None,
          tags: Optional[Sequence[str]] = None, privacy: Optional[str] = None, thumbnail: Optional[str] = None,
          captions: Optional[Sequence[str]] = None) -> Metadata:
    """Metadata from the command-line flags. Values are taken as given (an invalid privacy or an over-long title is
    for `plan` to report, and for `upload` to refuse); only a missing file is an error here."""
    if description is not None and description_file is not None:
        raise PublishError("INVALID_ARGUMENT", "give --description or --description-file, not both")
    if description_file is not None:
        if not os.path.isfile(description_file):
            raise PublishError("INPUT_NOT_FOUND", f"description file is not a regular file: {description_file}",
                               {"path": description_file, "input": "description file"})
        try:
            with open(description_file, "r", encoding="utf-8") as fh:
                description = fh.read()
        except UnicodeDecodeError as e:
            raise PublishError("INVALID_ARGUMENT", f"description file is not UTF-8: {description_file}",
                               {"path": description_file, "position": e.start})
    return Metadata(title=title, description=description, tags=[str(t) for t in (tags or [])],
                    privacy=DEFAULT_PRIVACY if privacy is None else str(privacy),
                    thumbnail=file_facts(thumbnail, "thumbnail") if thumbnail is not None else None,
                    captions=[file_facts(c, "captions") for c in (captions or [])])
