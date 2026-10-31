"""Facebook uploader (roadmap step 5, ADR-009): the Resumable Upload API upload of the video the confirmation token
bound, then one video published on a Facebook Page from its file handle, with the bound title, description and
privacy, authorised by a user access token (the upload) and a Page access token (the publish).

This module and the other uploader modules (platforms/linkedin.py, platforms/tiktok.py, platforms/vimeo.py,
platforms/x.py, platforms/youtube.py) are the only modules in the package with network code (ADR-001's source rule
exempts exactly these files). It runs only from upload.py's gate, after the exact --confirm token matched and the
credentials were read, so every refusal has already happened when the first byte leaves the machine. Standard library
only.

Protocol (sources in docs/decisions.md ADR-009, read 2026-10-31, Graph API v26.0):
  1. Session: POST https://graph.facebook.com/v26.0/<APP_ID>/uploads?file_name&file_length&file_type=video/mp4
     &access_token=<user token>; the answer's id is "upload:<UPLOAD_SESSION_ID>".
  2. Upload: POST /v26.0/upload:<UPLOAD_SESSION_ID> with Authorization: OAuth <user token>, file_offset: 0 and the
     file as the body; the answer's h is the file handle. After an interruption: GET the session (file_offset), then
     POST the rest of the file from that offset.
  3. Publish: POST https://graph-video.facebook.com/v26.0/<PAGE_ID>/videos, multipart form: access_token (the Page
     token), title, description, fbuploader_video_file_chunk (the handle), published, secret; the answer's id is the
     video's id.

The guide documents the upload as one body from an offset (no chunk size), so the file is streamed from disk in one
request per attempt; the stream is hashed as it is read, and its last block is held back unless the whole file has the
sha256 the token bound (CONFIRM_MISMATCH: the request is cut short, so no handle is issued).

Exactly what the token bound is sent: title and description when bound, and the privacy as documented flags (public:
published, not secret; unlisted: published and `secret`, "the video doesn't appear on Facebook or in search, but can
be viewed via permalink and embeds"). What an unpublished Page video's audience is is not documented, so `private` is
not offered (plan FAILs the default). The file handle is held for redaction; the tokens go to the Graph hosts only."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import ssl
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import quote, urlencode, urlsplit

from ..errors import PublishError
from .base import Uploader, UploadRequest

GRAPH_VERSION = "v26.0"
API_BASE = "https://graph.facebook.com"             # the upload ("use the graph.facebook.com host ... when uploading")
VIDEO_BASE = "https://graph-video.facebook.com"     # the publish (the guide's host for /<PAGE_ID>/videos)
SESSION_PATH = "/{v}/{app}/uploads"
UPLOAD_PATH = "/{v}/{session}"
PUBLISH_PATH = "/{v}/{page}/videos"
FILE_TYPE = "video/mp4"                              # file_type: application/pdf, image/*, video/mp4
FILE_FORMATS = (".mp4",)
PRIVACY = ("public", "unlisted")                    # unpublished (published=false) has no documented audience
FLAGS = {"public": {"published": "true", "secret": "false"}, "unlisted": {"published": "true", "secret": "true"}}
RETRIABLE_STATUS = (429, 500, 502, 503, 504)
# Graph API error codes: "Wait and retry the operation" (1 API Unknown, 2 API Service) and throttling (4, 17, 341)
TRANSIENT_CODES = (1, 2)
THROTTLE_CODES = (4, 17, 341)
AUTH_CODES = (10, 102, 190)                          # and 200-299 (API Permission)
BLOCK = 1 << 20                                      # the upload body is streamed from disk in 1 MiB reads
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")   # plain http only to these: the tests' mock server
_DIGITS = re.compile(r"^[0-9]{1,30}$")
_SESSION = re.compile(r"^upload:[A-Za-z0-9_=:-]{1,500}$")


class _Response:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        self.status, self.headers, self.body = status, headers, body

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8") or "null")
        except (UnicodeDecodeError, ValueError):
            return None


class _Changed(Exception):
    """Raised by _HashedTail before the last block when the file no longer has the bound sha256."""


class _HashedTail:
    """The file from `offset` to its end, read in blocks for http.client, hashed together with the bytes before
    `offset`; the last block is returned only if the whole file has `sha256` and `size` bytes."""

    def __init__(self, path: str, offset: int, size: int, sha256: str) -> None:
        self._fh = open(path, "rb")
        self._hash = hashlib.sha256()
        self._pos, self._size, self._want = 0, size, sha256
        while self._pos < offset:
            buf = self._fh.read(min(BLOCK, offset - self._pos))
            if not buf:
                break
            self._hash.update(buf)
            self._pos += len(buf)
        if self._pos != offset:
            self.close()
            raise _Changed()
        self.sent = 0

    def read(self, n: int = -1) -> bytes:
        want = BLOCK if n is None or n < 0 else min(n, BLOCK)
        buf = self._fh.read(min(want, self._size - self._pos))
        if self._pos + len(buf) >= self._size or not buf:
            h = self._hash.copy()
            h.update(buf)
            if self._pos + len(buf) != self._size or self._fh.read(1) or h.hexdigest() != self._want:
                raise _Changed()
        self._hash.update(buf)
        self._pos += len(buf)
        self.sent += len(buf)
        return buf

    def close(self) -> None:
        self._fh.close()


def _hold(value: str) -> None:
    from .. import credentials                         # imported here: credentials imports the platform registry
    credentials.hold(value)


def publish_fields(metadata: Dict[str, Any], handle: str) -> Dict[str, str]:
    """The publish form fields for the bound metadata (the Page token is added when sending): title and description
    when bound, the handle, and the privacy's flags. Nothing else is added (no thumbnail, targeting, scheduling)."""
    if metadata.get("privacy") not in PRIVACY:
        raise PublishError("INVALID_ARGUMENT", f"privacy {metadata.get('privacy')!r}: the Facebook uploader publishes "
                           "public or unlisted (secret) videos only")
    for field in ("tags", "thumbnail", "captions"):
        if metadata.get(field):
            raise PublishError("INVALID_ARGUMENT", f"the Facebook uploader does not send {field}; nothing was uploaded")
    out: Dict[str, str] = {}
    if metadata.get("title") is not None:
        out["title"] = metadata["title"]
    if metadata.get("description") is not None:
        out["description"] = metadata["description"]
    out["fbuploader_video_file_chunk"] = handle
    out.update(FLAGS[metadata["privacy"]])
    return out


def multipart(fields: Dict[str, str], boundary: str) -> bytes:
    """A multipart/form-data body of text fields (UTF-8)."""
    out = b""
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n").encode("ascii")
        out += v.encode("utf-8") + b"\r\n"
    return out + f"--{boundary}--\r\n".encode("ascii")


class FacebookUploader(Uploader):
    id = "facebook"
    name = "Facebook"
    roadmap_step = 5
    implemented = True
    # the Meta app id and a user access token (the Resumable Upload API), the Page id and a Page access token from a
    # person who can perform CREATE_CONTENT on the Page (pages_manage_posts, pages_read_engagement, pages_show_list)
    credential_env = ("PUBLISH_FACEBOOK_APP_ID", "PUBLISH_FACEBOOK_USER_ACCESS_TOKEN", "PUBLISH_FACEBOOK_PAGE_ID",
                      "PUBLISH_FACEBOOK_PAGE_ACCESS_TOKEN")
    docs_url = "https://developers.facebook.com/docs/video-api/guides/publishing"
    ffmpeg_skill_platform = "facebook"                 # ffmpeg-skill scripts/_platforms.py owns the delivery spec
    metadata_fields = ("title", "description", "privacy")

    # Endpoints and tuning. Only code changes these (the tests point them at a loopback mock server); no flag,
    # environment variable or file does.
    api_base = API_BASE
    video_base = VIDEO_BASE
    timeout_s = 60.0
    max_retries = 10
    max_backoff_s = 64.0
    max_retry_after_s = 300.0

    def __init__(self, sleep: Optional[Callable[[float], None]] = None,
                 rand: Optional[Callable[[], float]] = None) -> None:
        self._sleep_fn = sleep
        self._rand_fn = rand
        self._stats: Dict[str, Any] = {}

    def spec(self) -> Dict[str, Any]:
        return dict(super().spec(), protocol="Resumable Upload API (session, upload with file_offset, resume), then "
                                             "POST /<PAGE_ID>/videos with the file handle; user and Page access tokens",
                    endpoints=[API_BASE + SESSION_PATH.format(v=GRAPH_VERSION, app="<APP_ID>"),
                               API_BASE + UPLOAD_PATH.format(v=GRAPH_VERSION, session="upload:<UPLOAD_SESSION_ID>"),
                               VIDEO_BASE + PUBLISH_PATH.format(v=GRAPH_VERSION, page="<PAGE_ID>")],
                    graph_version=GRAPH_VERSION)

    # ---- the upload ---------------------------------------------------------------------------------------------
    def upload(self, request: UploadRequest, credentials: Any) -> Dict[str, Any]:
        self._check_endpoint()
        if request.size <= 0:
            raise PublishError("INVALID_ARGUMENT", "the file is empty; nothing was uploaded")
        if os.path.splitext(request.path)[1].lower() not in FILE_FORMATS:
            raise PublishError("INVALID_ARGUMENT", "the Resumable Upload API takes MP4 video (.mp4) only; nothing was "
                               "uploaded", {"platform": self.id, "path": request.path, "uploaded": False})
        app, page = credentials.get("PUBLISH_FACEBOOK_APP_ID"), credentials.get("PUBLISH_FACEBOOK_PAGE_ID")
        for var, value in (("PUBLISH_FACEBOOK_APP_ID", app), ("PUBLISH_FACEBOOK_PAGE_ID", page)):
            if not _DIGITS.match(value or ""):
                raise PublishError("INVALID_ARGUMENT", f"{var} must be a numeric id; its value is not printed. Nothing "
                                   "was uploaded", {"platform": self.id, "uploaded": False})
        md = request.metadata
        publish_fields(md, "")                           # refuses unbound fields before any request
        self._stats = {"requests": 0, "retries": 0, "resumed": 0, "waited_s": 0.0, "bytes_sent": 0}
        user = credentials.get("PUBLISH_FACEBOOK_USER_ACCESS_TOKEN")
        session = self._session(app, user, request.path, request.size)
        try:
            handle = self._transfer(user, session, request.path, request.size, request.sha256)
        except PublishError as e:
            raise PublishError(e.code, e.message + " (nothing was published)", e.details, e.retryable) from None
        fields = publish_fields(md, handle)
        video_id = self._publish(page, credentials.get("PUBLISH_FACEBOOK_PAGE_ACCESS_TOKEN"), fields)
        sent = {k: v for k, v in fields.items() if k != "fbuploader_video_file_chunk"}
        return {"video_id": video_id, "url": None, "privacy": md["privacy"], "sent": sent, "thumbnail": None,
                "captions": [], "transfer": dict(self._stats),
                "warnings": ["the Page publishes the video after Facebook processes it; the publishing guide gives no "
                             "URL for it, so find it on the Page by its id"]}

    # ---- step 1: the upload session -----------------------------------------------------------------------------
    def _session(self, app: str, user: str, path: str, size: int) -> str:
        query = urlencode({"file_name": os.path.basename(path), "file_length": size, "file_type": FILE_TYPE,
                           "access_token": user})
        url = self.api_base + SESSION_PATH.format(v=GRAPH_VERSION, app=app) + "?" + query
        # Retried only after a throttling or server-error answer: without one, a session may exist; it is simply
        # left unused.
        resp = self._api("session", "POST", url, b"", {}, retry_without_answer=False)
        doc = resp.json()
        session = doc.get("id") if isinstance(doc, dict) else None
        if not isinstance(session, str) or not _SESSION.match(session):
            raise self._protocol_error("session", "the answer has no id of the form upload:<UPLOAD_SESSION_ID>")
        return session

    # ---- step 2: the file, resumed from file_offset ---------------------------------------------------------------
    def _transfer(self, user: str, session: str, path: str, size: int, sha256: str) -> str:
        url = self.api_base + UPLOAD_PATH.format(v=GRAPH_VERSION, session=quote(session, safe=":"))
        auth = {"Authorization": "OAuth " + user}
        offset, attempt = 0, 0
        while True:
            try:
                body = _HashedTail(path, offset, size, sha256)
            except (_Changed, OSError):
                raise self._changed(path) from None
            resp: Optional[_Response] = None
            try:
                resp = self._send("POST", url, body, dict(auth, file_offset=str(offset)), length=size - offset)
            except _Changed:
                raise self._changed(path) from None
            except (OSError, http.client.HTTPException):
                resp = None                                    # interrupted: no answer, a reset or a timeout
            finally:
                self._stats["bytes_sent"] += body.sent
                body.close()
            if resp is not None and 200 <= resp.status < 300:
                doc = resp.json()
                handle = doc.get("h") if isinstance(doc, dict) else None
                if not isinstance(handle, str) or not handle:
                    raise self._protocol_error("upload", "the answer has no file handle (h)")
                _hold(handle)
                return handle
            if resp is not None and not self._retriable(resp):
                raise self._api_error(resp, "upload")
            attempt += 1
            if attempt > self.max_retries:
                raise self._interrupted("upload", resp)
            self._backoff(attempt, resp)
            offset = self._offset(url, auth, size)             # where Facebook has the file up to
            self._stats["resumed"] += 1

    def _offset(self, url: str, auth: Dict[str, str], size: int) -> int:
        resp = self._api("offset", "GET", url, b"", auth, retry_without_answer=True)
        doc = resp.json()
        got = doc.get("file_offset") if isinstance(doc, dict) else None
        if isinstance(got, str) and got.isdigit():
            got = int(got)
        if not isinstance(got, int) or isinstance(got, bool) or not 0 <= got <= size:
            raise self._protocol_error("offset", "the answer has no file_offset within the file")
        return got

    # ---- step 3: publish ----------------------------------------------------------------------------------------
    def _publish(self, page: str, page_token: str, fields: Dict[str, str]) -> str:
        form = dict({"access_token": page_token}, **fields)
        boundary = "publish-skill-" + hashlib.sha256(json.dumps(sorted(fields.items())).encode()).hexdigest()[:24]
        data = multipart(form, boundary)
        url = self.video_base + PUBLISH_PATH.format(v=GRAPH_VERSION, page=page)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        attempt = 0
        while True:
            try:
                resp: Optional[_Response] = self._send("POST", url, data, headers)
            except (OSError, http.client.HTTPException):
                resp = None
            if resp is not None and 200 <= resp.status < 300:
                doc = resp.json()
                video_id = doc.get("id") if isinstance(doc, dict) else None
                if not isinstance(video_id, str) or not _DIGITS.match(video_id):
                    raise self._protocol_error("publish", "the answer has no video id")
                return video_id
            if resp is not None and (resp.status == 429 or _code(resp) in THROTTLE_CODES):
                attempt += 1                                   # throttled before anything was made: retry
                if attempt <= self.max_retries:
                    self._backoff(attempt, resp)
                    continue
                raise self._interrupted("publish", resp)
            if resp is None or resp.status >= 500 or _code(resp) in TRANSIENT_CODES:
                # No answer, a server error or an "unknown" error: the video may or may not exist. Never published
                # twice: stop and say so.
                raise PublishError("UPLOAD_INTERRUPTED", "publish: " + ("no answer" if resp is None else
                                   f"Facebook answered {resp.status}") + "; the file is uploaded but it is unknown "
                                   "whether the video was published. Check the Page before running upload again",
                                   {"platform": self.id, "step": "publish",
                                    "status": None if resp is None else resp.status, "uploaded": False,
                                    "transfer": dict(self._stats)}, retryable=False)
            raise self._api_error(resp, "publish")

    # ---- requests, retries and errors ---------------------------------------------------------------------------
    @staticmethod
    def _retriable(resp: _Response) -> bool:
        return resp.status in RETRIABLE_STATUS or _code(resp) in TRANSIENT_CODES + THROTTLE_CODES

    def _api(self, what: str, method: str, url: str, body: bytes, headers: Dict[str, str],
             retry_without_answer: bool) -> _Response:
        """One API call with its retries: 429/5xx and transient or throttling error codes always; no answer only
        when `retry_without_answer`."""
        attempt = 0
        while True:
            try:
                resp: Optional[_Response] = self._send(method, url, body, headers)
            except (OSError, http.client.HTTPException):
                resp = None
                if not retry_without_answer:
                    raise PublishError("UPLOAD_INTERRUPTED", f"{what} got no answer; nothing was published. Run the "
                                       "same upload again later", {"platform": self.id, "step": what,
                                                                   "uploaded": False,
                                                                   "transfer": dict(self._stats)}) from None
            if resp is not None and 200 <= resp.status < 300:
                return resp
            if resp is not None and not self._retriable(resp):
                raise self._api_error(resp, what)
            attempt += 1
            if attempt > self.max_retries:
                raise self._interrupted(what, resp)
            self._backoff(attempt, resp)

    def _backoff(self, attempt: int, resp: Optional[_Response]) -> None:
        """random() x 2^attempt seconds, capped; Retry-After (seconds) is waited for instead when it is later, up to
        max_retry_after_s."""
        delay = min(self.max_backoff_s, (self._rand_fn or _rand)() * (2 ** attempt))
        retry_after = (resp.headers.get("retry-after") or "").strip() if resp is not None else ""
        if retry_after.isdigit():
            delay = max(delay, min(float(retry_after), self.max_retry_after_s))
        self._stats["retries"] += 1
        self._wait(delay)

    def _wait(self, delay: float) -> None:
        self._stats["waited_s"] = round(self._stats["waited_s"] + delay, 3)
        (self._sleep_fn or _sleep)(delay)

    def _send(self, method: str, url: str, body: Any, headers: Dict[str, str],
              length: Optional[int] = None) -> _Response:
        parts = urlsplit(url)
        conn = self._connection(parts.scheme, parts.hostname or "", parts.port)
        self._stats["requests"] = self._stats.get("requests", 0) + 1
        n = len(body) if length is None else length
        try:
            conn.request(method, (parts.path or "/") + ("?" + parts.query if parts.query else ""),
                         body=body if method != "GET" else None,
                         headers=dict(headers, **({"Content-Length": str(n)} if method != "GET" else {})))
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
        for base in (self.api_base, self.video_base):
            parts = urlsplit(base)
            if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS)):
                raise PublishError("INTERNAL_ERROR", f"refusing endpoint {base}: only https, or http to a loopback "
                                   "test server")

    def _changed(self, path: str) -> PublishError:
        return PublishError("CONFIRM_MISMATCH", "the file changed after the confirmation token was checked; its final "
                            "bytes were not sent, so the upload was not completed. Run the dry run again",
                            {"platform": self.id, "step": "upload", "path": path, "uploaded": False})

    def _interrupted(self, what: str, resp: Optional[_Response]) -> PublishError:
        last = ({"status": resp.status, "error_code": _code(resp)} if resp is not None
                else {"status": None, "error_code": None, "reason": "no answer"})
        return PublishError("UPLOAD_INTERRUPTED", f"{what}: still failing after {self.max_retries} retries "
                            f"(last: {last['status'] or 'no answer'}); nothing was published. Run the same upload "
                            "again later", {"platform": self.id, "step": what, "last": last, "uploaded": False,
                                            "transfer": dict(self._stats)})

    def _protocol_error(self, what: str, why: str) -> PublishError:
        return PublishError("UPLOAD_REJECTED", f"{what}: {why}; nothing was published",
                            {"platform": self.id, "step": what, "uploaded": False})

    def _api_error(self, resp: _Response, what: str) -> PublishError:
        problem = _problem(resp)
        details = {"platform": self.id, "step": what, "status": resp.status, "uploaded": False, **problem}
        said = problem.get("message") or ""
        code = problem.get("error_code")
        if resp.status == 401 or code in AUTH_CODES or (isinstance(code, int) and 200 <= code <= 299):
            return PublishError("AUTH_FAILED", f"{what}: Facebook refused a token or a permission ({resp.status}"
                                + (f", code {code}" if code is not None else "") + (f": {said}" if said else "")
                                + "); store new tokens (a user access token for the upload, a Page access token with "
                                "pages_manage_posts, pages_read_engagement and pages_show_list for the publish) with "
                                "`publish auth login --platform facebook` (values are never printed). Nothing was "
                                "published", details)
        return PublishError("UPLOAD_REJECTED", f"{what}: Facebook refused the request ({resp.status}"
                            + (f", code {code}" if code is not None else "") + (f": {said}" if said else "")
                            + "); nothing was published", details)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _rand() -> float:
    return random.random()


def _error(resp: _Response) -> Dict[str, Any]:
    doc = resp.json()
    err = doc.get("error") if isinstance(doc, dict) else None
    return err if isinstance(err, dict) else {}


def _code(resp: _Response) -> Optional[int]:
    code = _error(resp).get("code")
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _problem(resp: _Response) -> Dict[str, Any]:
    """The parts of a Graph API error that are safe to report: message and type (at most 300 characters), code and
    error_subcode. Never a header, never fbtrace_id."""
    err = _error(resp)
    out: Dict[str, Any] = {}
    for k in ("message", "type"):
        if isinstance(err.get(k), str) and err[k]:
            out[k if k != "type" else "error_type"] = err[k][:300]
    for k, name in (("code", "error_code"), ("error_subcode", "error_subcode")):
        if isinstance(err.get(k), int) and not isinstance(err.get(k), bool):
            out[name] = err[k]
    return out
