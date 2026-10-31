"""Instagram Reels uploader (roadmap step 5, ADR-010): a REELS container with a resumable upload session, the video the
confirmation token bound uploaded to rupload.facebook.com, the container's status until FINISHED, then one
media_publish, with the bound title as the caption, authorised by an access token from Facebook Login for Business.

This module and the other uploader modules (platforms/facebook.py, platforms/linkedin.py, platforms/tiktok.py,
platforms/vimeo.py, platforms/x.py, platforms/youtube.py) are the only modules in the package with network code
(ADR-001's source rule exempts exactly these files). It runs only from upload.py's gate, after the exact --confirm
token matched and the credentials were read, so every refusal has already happened when the first byte leaves the
machine. Standard library only.

Protocol (sources in docs/decisions.md ADR-010, read 2026-10-31, Graph API v26.0):
  1. Container: POST https://graph.facebook.com/v26.0/<IG_ID>/media with media_type=REELS, upload_type=resumable,
     caption (the bound title, when set) and access_token; the answer's id is the container's id.
  2. Upload: POST https://rupload.facebook.com/ig-api-upload/v26.0/<container> with Authorization: OAuth <token>,
     offset: 0, file_size and the file as the body; {"success": true} on success, {"debug_info": {"retriable", ...}}
     on failure.
  3. Status: GET /v26.0/<container>?fields=status_code, once a minute, at most five times ("We recommend querying a
     container's status once per minute, for no more than 5 minutes"), until FINISHED (ERROR, EXPIRED: stop).
  4. Publish: POST /v26.0/<IG_ID>/media_publish with creation_id; the answer's id is the Instagram media id.

No way to ask how many bytes the upload received is documented, so an interrupted upload is sent again whole, from
offset 0 (the documented "generally 0"); the stream is hashed as it is read and its last block is held back unless
the file still has the sha256 the token bound (CONFIRM_MISMATCH: the request is cut short).

Exactly what the token bound is sent: the caption is the bound title and nothing else is added (no cover, collaborators,
location, audio name or share_to_feed: Instagram's defaults apply). Instagram has no private or unlisted post through
this API, so `public` is the only privacy (plan FAILs the default `private`)."""
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
from urllib.parse import urlencode, urlsplit

from ..errors import PublishError
from .base import Uploader, UploadRequest

GRAPH_VERSION = "v26.0"
API_BASE = "https://graph.facebook.com"             # Facebook Login for Business: graph.facebook.com and rupload
UPLOAD_BASE = "https://rupload.facebook.com"
MEDIA_PATH = "/{v}/{ig}/media"
UPLOAD_PATH = "/ig-api-upload/{v}/{container}"
STATUS_PATH = "/{v}/{container}"
PUBLISH_PATH = "/{v}/{ig}/media_publish"
FILE_FORMATS = (".mp4", ".mov")                      # "Container: MOV or MP4 (MPEG-4 Part 14)"
PRIVACY = ("public",)                                # no private or unlisted post through the API
RETRIABLE_STATUS = (429, 500, 502, 503, 504)
TRANSIENT_CODES = (1, 2)                             # Graph API: "Wait and retry the operation"
THROTTLE_CODES = (4, 17, 341)
AUTH_CODES = (10, 102, 190)                          # and 200-299 (API Permission)
BLOCK = 1 << 20
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")   # plain http only to these: the tests' mock server
_DIGITS = re.compile(r"^[0-9]{1,30}$")


class _Response:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        self.status, self.headers, self.body = status, headers, body

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8") or "null")
        except (UnicodeDecodeError, ValueError):
            return None


class _Changed(Exception):
    """Raised by _HashedFile before the last block when the file no longer has the bound sha256."""


class _HashedFile:
    """The whole file, read in blocks for http.client and hashed; the last block is returned only if the file has
    `sha256` and `size` bytes."""

    def __init__(self, path: str, size: int, sha256: str) -> None:
        self._fh = open(path, "rb")
        self._hash = hashlib.sha256()
        self._pos, self._size, self._want = 0, size, sha256
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


def container_fields(metadata: Dict[str, Any]) -> Dict[str, str]:
    """The container's fields for the bound metadata (the token is added when sending): REELS, a resumable upload, and
    the bound title as the caption. Nothing else is added."""
    if metadata.get("privacy") not in PRIVACY:
        raise PublishError("INVALID_ARGUMENT", f"privacy {metadata.get('privacy')!r}: Instagram Reels published "
                           "through the API are public only")
    for field in ("description", "tags", "thumbnail", "captions"):
        if metadata.get(field):
            raise PublishError("INVALID_ARGUMENT", f"the Instagram Reels uploader does not send {field}; nothing was "
                               "uploaded")
    out = {"media_type": "REELS", "upload_type": "resumable"}
    if metadata.get("title") is not None:
        out["caption"] = metadata["title"]
    return out


class ReelsUploader(Uploader):
    id = "reels"                                       # ffmpeg-skill's id for the Instagram Reels spec
    name = "Instagram Reels"
    roadmap_step = 5
    implemented = True
    # the Instagram professional account's id and an access token from Facebook Login for Business with
    # instagram_basic and instagram_content_publish (ADR-003, ADR-010)
    credential_env = ("PUBLISH_INSTAGRAM_USER_ID", "PUBLISH_INSTAGRAM_ACCESS_TOKEN")
    docs_url = "https://developers.facebook.com/docs/instagram-platform/content-publishing/"
    ffmpeg_skill_platform = "reels"                    # ffmpeg-skill scripts/_platforms.py owns the delivery spec
    metadata_fields = ("title", "privacy")

    # Endpoints and tuning. Only code changes these (the tests point them at a loopback mock server); no flag,
    # environment variable or file does.
    api_base = API_BASE
    upload_base = UPLOAD_BASE
    timeout_s = 60.0
    max_retries = 10
    max_backoff_s = 64.0
    max_retry_after_s = 300.0
    status_polls = 5
    poll_interval_s = 60.0

    def __init__(self, sleep: Optional[Callable[[float], None]] = None,
                 rand: Optional[Callable[[], float]] = None) -> None:
        self._sleep_fn = sleep
        self._rand_fn = rand
        self._stats: Dict[str, Any] = {}

    def spec(self) -> Dict[str, Any]:
        return dict(super().spec(), protocol="Instagram content publishing: a REELS container with "
                                             "upload_type=resumable, the rupload upload, status_code until FINISHED, "
                                             "media_publish; Facebook Login for Business access token",
                    endpoints=[API_BASE + MEDIA_PATH.format(v=GRAPH_VERSION, ig="<IG_ID>"),
                               UPLOAD_BASE + UPLOAD_PATH.format(v=GRAPH_VERSION, container="<IG_MEDIA_CONTAINER_ID>"),
                               API_BASE + STATUS_PATH.format(v=GRAPH_VERSION, container="<IG_MEDIA_CONTAINER_ID>"),
                               API_BASE + PUBLISH_PATH.format(v=GRAPH_VERSION, ig="<IG_ID>")],
                    graph_version=GRAPH_VERSION)

    # ---- the upload ---------------------------------------------------------------------------------------------
    def upload(self, request: UploadRequest, credentials: Any) -> Dict[str, Any]:
        self._check_endpoint()
        if request.size <= 0:
            raise PublishError("INVALID_ARGUMENT", "the file is empty; nothing was uploaded")
        if os.path.splitext(request.path)[1].lower() not in FILE_FORMATS:
            raise PublishError("INVALID_ARGUMENT", "Instagram Reels take MP4 or MOV video (.mp4, .mov); nothing was "
                               "uploaded", {"platform": self.id, "path": request.path, "uploaded": False})
        ig = credentials.get("PUBLISH_INSTAGRAM_USER_ID")
        if not _DIGITS.match(ig or ""):
            raise PublishError("INVALID_ARGUMENT", "PUBLISH_INSTAGRAM_USER_ID must be the numeric Instagram account id; "
                               "its value is not printed. Nothing was uploaded",
                               {"platform": self.id, "uploaded": False})
        md = request.metadata
        fields = container_fields(md)                    # refuses unbound fields before any request
        self._stats = {"requests": 0, "retries": 0, "resumed": 0, "waited_s": 0.0, "bytes_sent": 0, "status_polls": 0}
        token = credentials.get("PUBLISH_INSTAGRAM_ACCESS_TOKEN")
        container = self._container(ig, token, fields)
        try:
            self._transfer(token, container, request.path, request.size, request.sha256)
            self._wait_until_finished(token, container)
        except PublishError as e:
            raise PublishError(e.code, e.message + " (nothing was published)", dict(e.details, container=container),
                               e.retryable) from None
        media_id = self._publish(ig, token, container)
        return {"video_id": media_id, "media_id": media_id, "container": container, "url": None,
                "privacy": md["privacy"], "sent": fields, "thumbnail": None, "captions": [],
                "transfer": dict(self._stats),
                "warnings": ["the content publishing guide gives no URL for a published reel; find it on the account "
                             "by its media id"]}

    # ---- step 1: the container ----------------------------------------------------------------------------------
    def _container(self, ig: str, token: str, fields: Dict[str, str]) -> str:
        body = urlencode(dict(fields, access_token=token)).encode("utf-8")
        # Retried only after a throttling or server-error answer: without one, a container may exist; unpublished, it
        # expires after 24 hours.
        resp = self._api("container", "POST", self.api_base + MEDIA_PATH.format(v=GRAPH_VERSION, ig=ig), body,
                         {"Content-Type": "application/x-www-form-urlencoded"}, retry_without_answer=False)
        doc = resp.json()
        container = doc.get("id") if isinstance(doc, dict) else None
        if not isinstance(container, str) or not _DIGITS.match(container):
            raise self._protocol_error("container", "the answer has no container id")
        return container

    # ---- step 2: the file ---------------------------------------------------------------------------------------
    def _transfer(self, token: str, container: str, path: str, size: int, sha256: str) -> None:
        url = self.upload_base + UPLOAD_PATH.format(v=GRAPH_VERSION, container=container)
        headers = {"Authorization": "OAuth " + token, "offset": "0", "file_size": str(size)}
        attempt = 0
        while True:
            try:
                body = _HashedFile(path, size, sha256)
            except OSError:
                raise self._changed(path) from None
            resp: Optional[_Response] = None
            try:
                resp = self._send("POST", url, body, headers, length=size)
            except _Changed:
                raise self._changed(path) from None
            except (OSError, http.client.HTTPException):
                resp = None                                    # interrupted: no answer, a reset or a timeout
            finally:
                self._stats["bytes_sent"] += body.sent
                body.close()
            doc = resp.json() if resp is not None else None
            if resp is not None and 200 <= resp.status < 300 and isinstance(doc, dict) and doc.get("success") is True:
                return
            info = doc.get("debug_info") if isinstance(doc, dict) else None
            retriable = (resp is None or resp.status in RETRIABLE_STATUS
                         or (isinstance(info, dict) and info.get("retriable") is True))
            if not retriable:
                raise self._upload_error(resp, info)
            attempt += 1
            if attempt > self.max_retries:
                raise self._interrupted("upload", resp)
            self._backoff(attempt, resp)
            self._stats["resumed"] += 1                       # the whole file again, from offset 0

    # ---- step 3: the container's status -------------------------------------------------------------------------
    def _wait_until_finished(self, token: str, container: str) -> None:
        url = (self.api_base + STATUS_PATH.format(v=GRAPH_VERSION, container=container) + "?"
               + urlencode({"fields": "status_code", "access_token": token}))
        for n in range(self.status_polls):
            if n:
                self._wait(self.poll_interval_s)
            self._stats["status_polls"] += 1
            resp = self._api("status", "GET", url, b"", {}, retry_without_answer=True)
            doc = resp.json()
            state = doc.get("status_code") if isinstance(doc, dict) else None
            if state == "FINISHED":
                return
            if state in ("ERROR", "EXPIRED"):
                why = ("the container was not published within 24 hours and expired" if state == "EXPIRED" else
                       "Instagram could not process the video; re-export it to the delivery spec: ffmpeg-skill export "
                       "--preset reels")
                raise PublishError("UPLOAD_REJECTED", f"status {state}: {why}", {"platform": self.id, "step": "status",
                                                                                "status_code": state,
                                                                                "uploaded": False})
            if state != "IN_PROGRESS":
                raise self._protocol_error("status", f"unexpected status_code {str(state)[:40]!r}")
        raise PublishError("UPLOAD_INTERRUPTED", f"the container was still IN_PROGRESS after {self.status_polls} "
                           "status queries a minute apart; nothing was published (an unpublished container expires "
                           "after 24 hours). Run the same upload again later",
                           {"platform": self.id, "step": "status", "uploaded": False, "transfer": dict(self._stats)})

    # ---- step 4: publish ----------------------------------------------------------------------------------------
    def _publish(self, ig: str, token: str, container: str) -> str:
        body = urlencode({"creation_id": container, "access_token": token}).encode("utf-8")
        url = self.api_base + PUBLISH_PATH.format(v=GRAPH_VERSION, ig=ig)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        attempt = 0
        while True:
            try:
                resp: Optional[_Response] = self._send("POST", url, body, headers)
            except (OSError, http.client.HTTPException):
                resp = None
            if resp is not None and 200 <= resp.status < 300:
                doc = resp.json()
                media_id = doc.get("id") if isinstance(doc, dict) else None
                if not isinstance(media_id, str) or not _DIGITS.match(media_id):
                    raise self._protocol_error("publish", "the answer has no media id")
                return media_id
            if resp is not None and (resp.status == 429 or _code(resp) in THROTTLE_CODES):
                attempt += 1                                   # throttled before anything was made: retry
                if attempt <= self.max_retries:
                    self._backoff(attempt, resp)
                    continue
                raise self._interrupted("publish", resp)
            if resp is None or resp.status >= 500 or _code(resp) in TRANSIENT_CODES:
                # No answer, a server error or an "unknown" error: the reel may or may not be published. Never
                # published twice: stop and say so.
                raise PublishError("UPLOAD_INTERRUPTED", "publish: " + ("no answer" if resp is None else
                                   f"Instagram answered {resp.status}") + "; the video is uploaded (container "
                                   + container + ") but it is unknown whether the reel was published. Check the "
                                   "account before running upload again",
                                   {"platform": self.id, "step": "publish", "container": container,
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
        for base in (self.api_base, self.upload_base):
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

    def _upload_error(self, resp: Optional[_Response], info: Any) -> PublishError:
        details: Dict[str, Any] = {"platform": self.id, "step": "upload", "uploaded": False,
                                   "status": None if resp is None else resp.status}
        said = ""
        if isinstance(info, dict):
            for k in ("type", "message"):
                if isinstance(info.get(k), str) and info[k]:
                    details["debug_" + k] = info[k][:300]
            said = details.get("debug_message") or details.get("debug_type") or ""
        if resp is not None and (resp.status == 401 or "unauthorized" in said.lower()):
            return PublishError("AUTH_FAILED", "upload: Instagram refused the access token" + (f" ({said})" if said
                                else "") + "; store a new one with `publish auth login --platform reels` (values are "
                                "never printed). Nothing was published", details)
        if resp is not None and _code(resp) is not None:
            return self._api_error(resp, "upload")
        return PublishError("UPLOAD_REJECTED", "upload: Instagram refused the video" + (f" ({said})" if said else "")
                            + "; nothing was published", details)

    def _api_error(self, resp: _Response, what: str) -> PublishError:
        problem = _problem(resp)
        details = {"platform": self.id, "step": what, "status": resp.status, "uploaded": False, **problem}
        said = problem.get("message") or ""
        code = problem.get("error_code")
        if resp.status == 401 or code in AUTH_CODES or (isinstance(code, int) and 200 <= code <= 299):
            return PublishError("AUTH_FAILED", f"{what}: Instagram refused the access token or a permission "
                                f"({resp.status}" + (f", code {code}" if code is not None else "")
                                + (f": {said}" if said else "") + "); store a Facebook Login for Business token with "
                                "instagram_basic and instagram_content_publish with `publish auth login --platform "
                                "reels` (values are never printed). Nothing was published", details)
        return PublishError("UPLOAD_REJECTED", f"{what}: Instagram refused the request ({resp.status}"
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
