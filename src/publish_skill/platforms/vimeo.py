"""Vimeo uploader (roadmap step 5, ADR-005): the Vimeo API 3.4 resumable ("tus") upload of the video the confirmation
token bound, with its title, description and privacy, authorised by a personal access token.

This module and platforms/youtube.py are the only modules in the package with network code (ADR-001's source rule
exempts exactly these two files). It runs only from upload.py's gate, after the exact --confirm token matched and the
credentials were read, so every refusal has already happened when the first byte leaves the machine. Standard library
only: http.client, as in youtube.py.

Protocol (sources in docs/decisions.md ADR-005, read 2026-10-31):
  1. Create the video: POST https://api.vimeo.com/me/videos with Authorization: bearer <token>, Content-Type:
     application/json, Accept: application/vnd.vimeo.*+json;version=3.4 and the body
     {"upload": {"approach": "tus", "size": "<bytes>"}, "name", "description", "privacy": {"view"}}; the answer (200,
     or 201 in the reference) has upload.approach "tus", upload.upload_link and the video's uri and link.
  2. PATCH the file to upload.upload_link with Tus-Resumable: 1.0.0, Upload-Offset: <offset> and Content-Type:
     application/offset+octet-stream; 204 and the new Upload-Offset after each. The upload is complete when
     Upload-Offset equals the file's size.
  3. After an interrupted PATCH (no answer, a timeout, 5xx or 429) or a 409 (the offset does not match): wait, ask the
     upload link how far it got (HEAD with Tus-Resumable and Accept; 200 and Upload-Offset, Upload-Length) and resume
     from that offset. 404/410/403 means the upload link is gone (tus: the upload expired).

The PATCH and HEAD requests carry no Authorization header (the guide's Tables 2 and 3 list none), so the access token
goes only to api.vimeo.com. The upload link is a capability URL: it is recorded with credentials.hold, so the redactor
removes it from every output, and it must be https on a vimeo.com host.

Exactly what the token bound is sent: the create body is the bound title, description and privacy (privacy.view:
private -> nobody, unlisted -> unlisted, public -> anybody; always sent, so the default is `nobody`, "no one except
the owner"), nothing else; tags, a thumbnail or captions are refused by `plan` and `upload` before a token is printed
(metadata_fields). The file is hashed as its bytes are confirmed, and the final chunk is held back unless the bytes so
far plus that chunk have the sha256 the token bound, so a file edited after the gate is never completed
(CONFIRM_MISMATCH)."""
from __future__ import annotations

import hashlib
import http.client
import json
import random
import ssl
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

from ..errors import PublishError
from .base import Uploader, UploadRequest

API_BASE = "https://api.vimeo.com"
CREATE_PATH = "/me/videos"
ACCEPT = "application/vnd.vimeo.*+json;version=3.4"
TUS_VERSION = "1.0.0"
TUS_CONTENT_TYPE = "application/offset+octet-stream"
DEFAULT_CHUNK = 128 * 1024 * 1024              # within the guide's "128–512 MB" under either reading of MB
PRIVACY_VIEW = {"private": "nobody", "unlisted": "unlisted", "public": "anybody"}
RETRIABLE_STATUS = (429, 500, 502, 503, 504)
GONE_STATUS = (403, 404, 410)                  # tus: "404 Not Found, 410 Gone or 403 Forbidden" for a missing upload
RATE_LIMIT_CODE = 9000                         # 429, error code 9000 (rate limiting guide)
STORAGE_FULL_CODE = 4101                       # "maximum disk space has been reached"
QUOTA_CODES = {4102: "weekly", 4104: "daily"}  # "allotted weekly / daily quota has been reached"
UPLOAD_HOST_SUFFIX = ".vimeo.com"
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")      # plain http only to these: the tests' mock server
VIDEO_URL = "https://vimeo.com/"


class _Response:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        self.status, self.headers, self.body = status, headers, body

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8") or "null")
        except (UnicodeDecodeError, ValueError):
            return None


def _hold(value: str) -> None:
    from .. import credentials                         # imported here: credentials imports the platform registry
    credentials.hold(value)


def create_body(metadata: Dict[str, Any], size: int) -> Dict[str, Any]:
    """The POST /me/videos body for the bound metadata: the tus approach and size, then exactly the bound name,
    description and privacy.view. A field the metadata leaves unset is left out; nothing is added."""
    privacy = metadata["privacy"]
    if privacy not in PRIVACY_VIEW:
        raise PublishError("INVALID_ARGUMENT", f"privacy {privacy!r} is not one of {', '.join(PRIVACY_VIEW)}")
    for field in ("tags", "thumbnail", "captions"):
        if metadata.get(field):
            raise PublishError("INVALID_ARGUMENT", f"the Vimeo uploader does not send {field}; nothing was uploaded")
    body: Dict[str, Any] = {"upload": {"approach": "tus", "size": str(size)}}
    if metadata.get("title") is not None:
        body["name"] = metadata["title"]
    if metadata.get("description") is not None:
        body["description"] = metadata["description"]
    body["privacy"] = {"view": PRIVACY_VIEW[privacy]}
    return body


class VimeoUploader(Uploader):
    id = "vimeo"
    name = "Vimeo"
    roadmap_step = 5
    implemented = True
    # a personal access token (scopes upload, edit), from the environment or the OS keychain (ADR-003)
    credential_env = ("PUBLISH_VIMEO_ACCESS_TOKEN",)
    docs_url = "https://developer.vimeo.com/api/upload/videos"
    ffmpeg_skill_platform = ""                         # ffmpeg-skill has no vimeo delivery spec (ADR-005)
    metadata_fields = ("title", "description", "privacy")

    # Endpoints and tuning. Only code changes these (the tests point them at a loopback mock server); no flag,
    # environment variable or file does.
    api_base = API_BASE
    chunk_size = DEFAULT_CHUNK
    timeout_s = 60.0
    max_retries = 10
    max_backoff_s = 64.0
    max_retry_after_s = 300.0

    def __init__(self, sleep: Optional[Callable[[float], None]] = None, rand: Optional[Callable[[], float]] = None) -> None:
        self._sleep_fn = sleep
        self._rand_fn = rand
        self._stats: Dict[str, Any] = {}

    def spec(self) -> Dict[str, Any]:
        return dict(super().spec(), protocol="Vimeo API 3.4 resumable (tus 1.0.0) upload; personal access token",
                    endpoints=[API_BASE + CREATE_PATH, "upload.upload_link (https, *.vimeo.com)"])

    # ---- the upload ---------------------------------------------------------------------------------------------
    def upload(self, request: UploadRequest, credentials: Any) -> Dict[str, Any]:
        self._check_endpoint()
        if self.chunk_size <= 0:
            raise PublishError("INTERNAL_ERROR", "chunk size must be positive")
        if request.size <= 0:
            raise PublishError("INVALID_ARGUMENT", "the file is empty; nothing was uploaded")
        self._stats = {"requests": 0, "retries": 0, "resumed": 0, "waited_s": 0.0, "bytes_sent": 0}
        md = request.metadata
        body = create_body(md, request.size)
        token = credentials.get("PUBLISH_VIMEO_ACCESS_TOKEN")
        video = self._create(body, token)
        upload_link, uri = video["upload"]["upload_link"], video["uri"]
        video_id = uri.rsplit("/", 1)[-1]
        link = video.get("link") if isinstance(video.get("link"), str) else VIDEO_URL + video_id
        try:
            self._transfer(upload_link, request.path, request.size, request.sha256)
        except PublishError as e:
            details = dict(e.details, video_uri=uri, placeholder=True)
            raise PublishError(e.code, e.message + f". Vimeo keeps the empty video placeholder {uri} (privacy "
                               f"{body['privacy']['view']}); delete it on Vimeo, or leave it", details, e.retryable) from None
        privacy_view = (video.get("privacy") or {}).get("view") if isinstance(video.get("privacy"), dict) else None
        reverse = {v: k for k, v in PRIVACY_VIEW.items()}
        result: Dict[str, Any] = {"video_id": video_id, "uri": uri, "url": link,
                                  "privacy": reverse.get(privacy_view, privacy_view) if privacy_view else md["privacy"],
                                  "privacy_view": privacy_view or body["privacy"]["view"], "sent": body,
                                  "thumbnail": None, "captions": [], "warnings": []}
        if privacy_view is not None and privacy_view != body["privacy"]["view"]:
            result["warnings"].append(f"sent privacy.view {body['privacy']['view']!r}, Vimeo reports {privacy_view!r} "
                                      "(the account's plan or settings can override it)")
        result["transfer"] = dict(self._stats)
        result["processing"] = "Vimeo transcodes the video after the upload; it may not be playable yet"
        return result

    # ---- step 1: create the video -------------------------------------------------------------------------------
    def _create(self, body: Dict[str, Any], token: str) -> Dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = {"Authorization": "bearer " + token, "Content-Type": "application/json", "Accept": ACCEPT}
        attempt = 0
        while True:
            try:
                resp = self._send("POST", self.api_base + CREATE_PATH, data, headers)
            except (OSError, http.client.HTTPException):
                # Not retried: the video placeholder may exist already, and a second POST would add another.
                raise PublishError("UPLOAD_INTERRUPTED", "creating the video got no answer; nothing was uploaded (an "
                                   "empty, unpublished placeholder may exist in the account). Run the same upload again "
                                   "later", {"platform": self.id, "step": "create", "uploaded": False,
                                             "transfer": dict(self._stats)}) from None
            if resp.status in (200, 201):
                break
            if resp.status not in RETRIABLE_STATUS:
                raise self._api_error(resp, "create")
            attempt += 1
            if attempt > self.max_retries:
                raise self._interrupted("create", None, None, resp)
            self._backoff(attempt, resp)
        doc = resp.json()
        up = doc.get("upload") if isinstance(doc, dict) else None
        if not isinstance(up, dict) or up.get("approach") != "tus":
            raise self._protocol_error("create", "the answer's upload.approach is not tus")
        link, uri = up.get("upload_link"), doc.get("uri")
        if not isinstance(link, str) or not link:
            raise self._protocol_error("create", "the answer has no upload.upload_link")
        if not isinstance(uri, str) or not uri.startswith("/videos/") or not uri.rsplit("/", 1)[-1]:
            raise self._protocol_error("create", "the answer has no video uri")
        self._check_upload_link(link)
        _hold(link)
        return doc

    # ---- steps 2 and 3: the tus transfer ------------------------------------------------------------------------
    def _transfer(self, link: str, file: str, size: int, sha256: str) -> None:
        offset, hashed, attempt = 0, hashlib.sha256(), 0      # `hashed` covers bytes [0, offset), as confirmed
        with open(file, "rb") as fh:
            while offset < size:
                n = min(self.chunk_size, size - offset)
                fh.seek(offset)
                buf = fh.read(n)
                last = offset + n == size
                if len(buf) != n or (last and _digest(hashed, buf) != sha256):
                    raise PublishError("CONFIRM_MISMATCH", "the file changed after the confirmation token was checked; "
                                       "its final bytes were not sent, so the upload was not completed. Run the dry run "
                                       "again", {"platform": self.id, "step": "patch", "path": file, "uploaded": False})
                headers = {"Tus-Resumable": TUS_VERSION, "Upload-Offset": str(offset), "Content-Type": TUS_CONTENT_TYPE}
                try:
                    resp: Optional[_Response] = self._send("PATCH", link, buf, headers)
                    self._stats["bytes_sent"] += n
                except (OSError, http.client.HTTPException):
                    resp = None                                # interrupted: no answer, a reset or a timeout
                if resp is not None and resp.status in (200, 204):
                    before = offset
                    offset = self._advance(offset, buf, hashed, self._offset(resp, "patch"))
                    if offset > before:
                        attempt = 0
                        continue
                    attempt += 1                               # a PATCH that kept none of the chunk: retry, bounded
                    if attempt > self.max_retries:
                        raise self._interrupted("patch", offset, size, resp)
                    self._backoff(attempt, resp)
                    continue
                if resp is not None and resp.status in GONE_STATUS:
                    raise self._gone(resp, offset, size)
                if resp is not None and resp.status != 409 and resp.status not in RETRIABLE_STATUS:
                    raise self._api_error(resp, "patch")
                attempt += 1                                   # interrupted, 5xx, 429, or 409 (offset mismatch)
                if attempt > self.max_retries:
                    raise self._interrupted("patch", offset, size, resp)
                self._backoff(attempt, resp)
                confirmed = self._head(link, size, offset)
                self._stats["resumed"] += 1
                offset = self._advance(offset, buf, hashed, confirmed)

    def _head(self, link: str, size: int, offset: int) -> int:
        """The offset the upload link holds (tus HEAD)."""
        headers = {"Tus-Resumable": TUS_VERSION, "Accept": ACCEPT}
        attempt = 0
        while True:
            try:
                resp: Optional[_Response] = self._send("HEAD", link, b"", headers)
            except (OSError, http.client.HTTPException):
                resp = None
            if resp is not None and resp.status in (200, 204):
                length = (resp.headers.get("upload-length") or "").strip()
                if length and length != str(size):
                    raise self._protocol_error("head", f"the upload link expects {length} bytes, not {size}")
                return self._offset(resp, "head")
            if resp is not None and resp.status in GONE_STATUS:
                raise self._gone(resp, offset, size)
            if resp is not None and resp.status not in RETRIABLE_STATUS:
                raise self._api_error(resp, "head")
            attempt += 1
            if attempt > self.max_retries:
                raise self._interrupted("head", offset, size, resp)
            self._backoff(attempt, resp)

    def _offset(self, resp: _Response, what: str) -> int:
        value = (resp.headers.get("upload-offset") or "").strip()
        if not value.isdigit():
            raise self._protocol_error(what, f"unreadable Upload-Offset header {value!r}")
        return int(value)

    def _advance(self, offset: int, buf: bytes, hashed: Any, confirmed: int) -> int:
        """Bytes the upload link confirms must come from the chunk just sent; they join the running hash."""
        if not offset <= confirmed <= offset + len(buf):
            raise self._protocol_error("patch", f"the upload link holds {confirmed} bytes, outside the chunk just sent "
                                                f"({offset}-{offset + len(buf)})")
        hashed.update(buf[:confirmed - offset])
        return confirmed

    # ---- requests, retries and errors ---------------------------------------------------------------------------
    def _backoff(self, attempt: int, resp: Optional[_Response]) -> None:
        delay = min(self.max_backoff_s, (self._rand_fn or _rand)() * (2 ** attempt))
        retry_after = (resp.headers.get("retry-after") or "").strip() if resp is not None else ""
        if retry_after.isdigit():
            delay = max(delay, min(float(retry_after), self.max_retry_after_s))
        self._stats["retries"] += 1
        self._stats["waited_s"] = round(self._stats["waited_s"] + delay, 3)
        (self._sleep_fn or _sleep)(delay)

    def _send(self, method: str, url: str, body: bytes, headers: Dict[str, str]) -> _Response:
        parts = urlsplit(url)
        conn = self._connection(parts.scheme, parts.hostname or "", parts.port)
        self._stats["requests"] = self._stats.get("requests", 0) + 1
        try:
            conn.request(method, (parts.path or "/") + ("?" + parts.query if parts.query else ""), body=body,
                         headers=dict(headers, **{"Content-Length": str(len(body))}))
            r = conn.getresponse()
            return _Response(r.status, {k.lower(): v for k, v in r.getheaders()}, r.read())
        finally:
            conn.close()

    def _connection(self, scheme: str, host: str, port: Optional[int]) -> http.client.HTTPConnection:
        if scheme == "https":
            return http.client.HTTPSConnection(host, port or 443, timeout=self.timeout_s,
                                               context=ssl.create_default_context())
        if scheme == "http" and host in LOOPBACK_HOSTS:
            return http.client.HTTPConnection(host, port or 80, timeout=self.timeout_s)
        raise PublishError("INTERNAL_ERROR", f"refusing {scheme}://{host}: only https, or http to a loopback test server")

    def _check_endpoint(self) -> None:
        parts = urlsplit(self.api_base)
        if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS)):
            raise PublishError("INTERNAL_ERROR", f"refusing endpoint {self.api_base}: only https, or http to a loopback "
                               "test server")

    def _check_upload_link(self, link: str) -> None:
        """The file goes only to Vimeo: an https upload link on a vimeo.com host. Only a loopback API base (the tests'
        mock) may hand out an upload link on its own scheme, host and port."""
        base, loc = urlsplit(self.api_base), urlsplit(link)
        host = (loc.hostname or "").lower()
        on_vimeo = loc.scheme == "https" and (host == "vimeo.com" or host.endswith(UPLOAD_HOST_SUFFIX))
        on_test_mock = base.hostname in LOOPBACK_HOSTS and (loc.scheme, loc.hostname, loc.port) == (
            base.scheme, base.hostname, base.port)
        if not (on_vimeo or on_test_mock):
            raise self._protocol_error("create", "the upload link is not https on a vimeo.com host")

    def _gone(self, resp: _Response, offset: Optional[int], size: int) -> PublishError:
        return PublishError("UPLOAD_INTERRUPTED", f"the upload link is gone ({resp.status}: the upload expired); the "
                            "video was not completed. Run the same upload again (it creates a new video)",
                            {"platform": self.id, "step": "patch", "status": resp.status, "bytes_confirmed": offset,
                             "size": size, "uploaded": False, "transfer": dict(self._stats)})

    def _interrupted(self, what: str, offset: Optional[int], size: Optional[int], resp: Optional[_Response]) -> PublishError:
        last = ({"status": resp.status, "error_code": _error_code(resp)} if resp is not None
                else {"status": None, "error_code": None, "reason": "no answer"})
        return PublishError("UPLOAD_INTERRUPTED", f"{what}: still failing after {self.max_retries} retries "
                            f"(last: {last['status'] or 'no answer'}); the video was not completed. Run the same upload "
                            "again later", {"platform": self.id, "step": what, "last": last, "bytes_confirmed": offset,
                                            "size": size, "uploaded": False, "transfer": dict(self._stats)})

    def _protocol_error(self, what: str, why: str) -> PublishError:
        return PublishError("UPLOAD_REJECTED", f"{what}: {why}; the video was not completed",
                            {"platform": self.id, "step": what, "uploaded": False})

    def _api_error(self, resp: _Response, what: str) -> PublishError:
        code = _error_code(resp)
        doc = resp.json()
        message = ""
        if isinstance(doc, dict):
            message = str(doc.get("developer_message") or doc.get("error") or "")[:300]
        details = {"platform": self.id, "step": what, "status": resp.status, "error_code": code, "message": message,
                   "uploaded": False}
        if code in QUOTA_CODES:
            return PublishError("UPLOAD_LIMIT_EXCEEDED", f"{what}: the account's {QUOTA_CODES[code]} upload quota is "
                                f"reached (error {code}); nothing was uploaded. Try again after it resets", details)
        if code == STORAGE_FULL_CODE:
            return PublishError("UPLOAD_REJECTED", f"{what}: the account's storage is full (error {code}); nothing was "
                                "uploaded. Free space or upgrade the plan, then run the dry run again", details)
        if resp.status == 401:
            return PublishError("AUTH_FAILED", f"{what}: Vimeo refused the access token (401"
                                + (f", error {code}" if code else "") + "); store a new personal access token with "
                                "`publish auth login --platform vimeo` (values are never printed). Nothing was uploaded",
                                details)
        return PublishError("UPLOAD_REJECTED", f"{what}: Vimeo refused the request ({resp.status}"
                            + (f", error {code}" if code else "") + "); nothing was uploaded", details)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _rand() -> float:
    return random.random()


def _digest(hashed: Any, tail: bytes) -> str:
    h = hashed.copy()
    h.update(tail)
    return h.hexdigest()


def _error_code(resp: Optional[_Response]) -> Optional[int]:
    """`error_code` of a Vimeo error body ({"error", "error_code", "developer_message", ...}), when there is one."""
    doc = resp.json() if resp is not None else None
    code = doc.get("error_code") if isinstance(doc, dict) else None
    if isinstance(code, int):
        return code
    return int(code) if isinstance(code, str) and code.isdigit() else None

