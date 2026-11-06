"""Upload metadata: title, description, tags, privacy, thumbnail and captions (ADR-002).

`publish plan` and `publish upload` take the same flags and build the same Metadata from them, so the document a
plan checked is the document a dry run prints and the confirmation token binds. The thumbnail and the captions are
bound by their bytes (sha256 and size), not their path, like the deliverable: replacing the image after the dry run
needs a new token. Each caption file carries the BCP-47 language tag captions.insert requires (`--caption-language`, one
per `--caption`, in order); the token binds it with the file's bytes (ADR-004).

Roadmap step 8 (ADR-011) adds three optional fields, each bound by the token only when set (so a token for metadata
without them is unchanged): `language` (the language of the title and description, YouTube's snippet.defaultLanguage),
`localizations` (translated titles and descriptions, keyed by BCP-47 tag, read from a UTF-8 JSON file the caller
gives: {"ja": {"title": "...", "description": "..."}}) and `publish_at` (a scheduled publish time, RFC 3339 / ISO 8601
with an explicit offset, e.g. 2026-12-01T09:00:00Z; YouTube's status.publishAt, which needs privacy `private`).

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
_JPEG = b"\xff\xd8\xff"
_PNG = b"\x89PNG\r\n\x1a\n"


def image_kind(path: str) -> str:
    """image/png, image/jpeg or other, by the file's first bytes (plan checks it; the uploader sends it as the type)."""
    with open(path, "rb") as fh:
        head = fh.read(8)
    return "image/png" if head.startswith(_PNG) else "image/jpeg" if head.startswith(_JPEG) else "other"


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
    captions: List[Dict[str, Any]] = field(default_factory=list)   # file_facts() + "language"
    language: Optional[str] = None                                  # of the title and description (ADR-011)
    localizations: Dict[str, Dict[str, str]] = field(default_factory=dict)   # BCP-47 tag -> {"title", "description"}
    publish_at: Optional[str] = None                                # RFC 3339, as given (ADR-011)

    def to_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "description": self.description, "tags": list(self.tags), "privacy": self.privacy,
                "thumbnail": dict(self.thumbnail) if self.thumbnail else None, "captions": [dict(c) for c in self.captions],
                "language": self.language, "localizations": {k: dict(v) for k, v in self.localizations.items()},
                "publish_at": self.publish_at}

    def canonical(self) -> str:
        """The text the confirmation token hashes: every field, files by content (sha256, size) and never by path,
        in a fixed key order. Two Metadata that would send the same thing to the platform give the same text."""
        doc = {"title": self.title, "description": self.description, "tags": list(self.tags), "privacy": self.privacy,
               "thumbnail": {"sha256": self.thumbnail["sha256"], "size": self.thumbnail["size"]} if self.thumbnail else None,
               "captions": [{"sha256": c["sha256"], "size": c["size"], "language": c.get("language")} for c in self.captions]}
        # ADR-011: bound only when set, so the text (and the token) of metadata without them stays what it was
        if self.language is not None:
            doc["language"] = self.language
        if self.localizations:
            doc["localizations"] = {k: dict(v) for k, v in self.localizations.items()}
        if self.publish_at is not None:
            doc["publish_at"] = self.publish_at
        return json.dumps(doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


DEFAULT_METADATA = Metadata()


def build(title: Optional[str] = None, description: Optional[str] = None, description_file: Optional[str] = None,
          tags: Optional[Sequence[str]] = None, privacy: Optional[str] = None, thumbnail: Optional[str] = None,
          captions: Optional[Sequence[str]] = None, caption_languages: Optional[Sequence[str]] = None,
          language: Optional[str] = None, localizations_file: Optional[str] = None,
          publish_at: Optional[str] = None) -> Metadata:
    """Metadata from the command-line flags. Values are taken as given (an invalid privacy or an over-long title is
    for `plan` to report, and for `upload` to refuse); only a missing file, or a --caption-language count that does not
    match the --caption count, is an error here."""
    captions, caption_languages = list(captions or []), list(caption_languages or [])
    if caption_languages and len(caption_languages) != len(captions):
        raise PublishError("INVALID_ARGUMENT", f"give one --caption-language per --caption, in the same order "
                           f"({len(captions)} captions, {len(caption_languages)} languages)",
                           {"captions": len(captions), "caption_languages": len(caption_languages)})
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
                    language=None if language is None else str(language),
                    localizations=read_localizations(localizations_file) if localizations_file is not None else {},
                    publish_at=None if publish_at is None else str(publish_at),
                    privacy=DEFAULT_PRIVACY if privacy is None else str(privacy),
                    thumbnail=file_facts(thumbnail, "thumbnail") if thumbnail is not None else None,
                    captions=[dict(file_facts(c, "captions"),
                                   language=str(caption_languages[i]) if caption_languages else None)
                              for i, c in enumerate(captions)])


def read_localizations(path: str) -> Dict[str, Dict[str, str]]:
    """Translated titles and descriptions from a UTF-8 JSON file: an object keyed by language tag, each value an object
    with `title` and/or `description` strings and nothing else (ADR-011). The shape is checked here; the tags and the
    lengths are for `plan` to report and for `upload` to refuse."""
    if not os.path.isfile(path):
        raise PublishError("INPUT_NOT_FOUND", f"localizations file is not a regular file: {path}",
                           {"path": path, "input": "localizations file"})
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except UnicodeDecodeError as e:
        raise PublishError("INVALID_ARGUMENT", f"localizations file is not UTF-8: {path}", {"path": path, "position": e.start})
    except ValueError as e:
        raise PublishError("INVALID_ARGUMENT", f"localizations file is not JSON: {path} ({e})", {"path": path})
    shape = ('give {"<language tag>": {"title": "...", "description": "..."}, ...}: an object of objects whose only '
             'keys are title and description, each a string')
    if not isinstance(doc, dict) or not doc:
        raise PublishError("INVALID_ARGUMENT", f"localizations file: {shape}", {"path": path})
    out: Dict[str, Dict[str, str]] = {}
    for lang, entry in doc.items():
        if (not isinstance(entry, dict) or not entry or set(entry) - {"title", "description"}
                or not all(isinstance(v, str) for v in entry.values())):
            raise PublishError("INVALID_ARGUMENT", f"localizations file, entry {lang!r}: {shape}",
                               {"path": path, "language": lang})
        out[str(lang)] = {k: entry[k] for k in ("title", "description") if k in entry}
    return out
