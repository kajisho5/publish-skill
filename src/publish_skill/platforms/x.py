"""X uploader (roadmap step 5, ADR-007): the X API v2 chunked media upload of the video the confirmation token bound,
then one post carrying it, with the bound title as the post's text, authorised by an OAuth 2.0 user access token.

This module and the other uploader modules (platforms/tiktok.py, platforms/vimeo.py, platforms/youtube.py) are the
only modules in the package with network code (ADR-001's source rule exempts exactly these files). It runs only from
upload.py's gate, after the exact --confirm token matched and the credentials were read, so every refusal has already
happened when the first byte leaves the machine. Standard library only: http.client, as in the other uploaders.

Protocol (sources in docs/decisions.md ADR-007, read 2026-10-31):
  1. Initialise: POST https://api.x.com/2/media/upload/initialize with Authorization: Bearer <token> and a JSON body
     {"media_type", "total_bytes", "media_category": "tweet_video"}; the answer carries data.id (1-19 digits).
  2. Append: POST /2/media/upload/{id}/append, multipart/form-data with `segment_index` (0-9999) and `media` (the
     chunk), in order; the guide asks for chunks "at or below 5 MB".
  3. Finalize: POST /2/media/upload/{id}/finalize; data.size must be the file's size, and data.processing_info, when
     present, says the video is still being processed.
  4. Status: GET /2/media/upload?command=STATUS&media_id={id}, waiting check_after_secs between queries, until
     processing_info.state is `succeeded` (or `failed`), a bounded number of times.
  5. Post: POST /2/tweets {"text"?, "media": {"media_ids": [id]}}; the answer carries data.id, the post's id.

Every request goes to api.x.com with the token; there is no separate upload URL, so nothing but the token needs
redacting. X documents no offset query for an upload session: an append that got no answer (or a 5xx / 429) is sent
again with the same segment_index, and finalize's data.size must then equal the file's size, or the upload stops
before any post is made.

Exactly what the token bound is sent: the post's text is the bound title (when set; a media-only post otherwise) and
nothing else. X has no private or unlisted post, so `plan` FAILs every privacy but `public` and `upload` refuses it
before a token is printed; a description, tags, a thumbnail or captions are refused likewise (metadata_fields). The
file is hashed as its segments are acknowledged, and the final segment is held back unless the bytes so far plus that
segment have the sha256 the token bound (CONFIRM_MISMATCH)."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import ssl
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import quote, urlsplit

from ..errors import PublishError
from .base import Uploader, UploadRequest

API_BASE = "https://api.x.com"
INIT_PATH = "/2/media/upload/initialize"
APPEND_PATH = "/2/media/upload/{id}/append"
FINALIZE_PATH = "/2/media/upload/{id}/finalize"
STATUS_PATH = "/2/media/upload"
POST_PATH = "/2/tweets"
JSON_TYPE = "application/json"
MEDIA_CATEGORY = "tweet_video"
# "keep each chunk at or below 5 MB" (the server maximum is 8 MB): 4 MiB is below 5 MB under either reading
DEFAULT_CHUNK = 4 * 1024 * 1024
MAX_SEGMENTS = 10000                                 # segment_index 0..9999
MAX_BYTES = 8 * 1000 ** 3                            # tweet_video "8 GB" (default accounts), the decimal reading
# initialize's media_type for a video: video/mp4, video/webm, video/mp2t, video/quicktime
CONTENT_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm", ".ts": "video/mp2t"}
PRIVACY = ("public",)                                # X has no private or unlisted post
RETRIABLE_STATUS = (429, 500, 502, 503, 504)
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")   # plain http only to these: the tests' mock server
_MEDIA_ID = re.compile(r"^[0-9]{1,19}$")


class _Response:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        self.status, self.headers, self.body = status, headers, body

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8") or "null")
        except (UnicodeDecodeError, ValueError):
            return None


def segments(size: int, chunk_size: int) -> List[range]:
    """The byte ranges [first, last + 1) of each segment, in order; the last one may be shorter."""
    if size <= 0 or chunk_size <= 0:
        raise PublishError("INVALID_ARGUMENT", "the file is empty; nothing was uploaded")
    return [range(s, min(s + chunk_size, size)) for s in range(0, size, chunk_size)]


def post_body(metadata: Dict[str, Any], media_id: str) -> Dict[str, Any]:
    """The create-post body for the bound metadata: the bound title as the text (when set) and the media id. Nothing
    else is added (no reply settings, no labels)."""
    if metadata.get("privacy") not in PRIVACY:
        raise PublishError("INVALID_ARGUMENT", f"privacy {metadata.get('privacy')!r}: X posts are public only")
    for field in ("description", "tags", "thumbnail", "captions"):
        if metadata.get(field):
            raise PublishError("INVALID_ARGUMENT", f"the X uploader does not send {field}; nothing was uploaded")
    body: Dict[str, Any] = {}
    if metadata.get("title") is not None:
        body["text"] = metadata["title"]
    body["media"] = {"media_ids": [media_id]}
    return body


def multipart(fields: Dict[str, str], media: bytes, boundary: str) -> bytes:
    """A multipart/form-data body: the text fields, then `media` as an octet stream."""
    out = b""
    for k, v in fields.items():
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n").encode("ascii")
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"media\"; filename=\"blob\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode("ascii")
    return out + media + f"\r\n--{boundary}--\r\n".encode("ascii")


class XUploader(Uploader):
    id = "x"
    name = "X"
    roadmap_step = 5
    implemented = True
    # an OAuth 2.0 user access token with media.write, tweet.write, tweet.read and users.read (ADR-003, ADR-007)
    credential_env = ("PUBLISH_X_ACCESS_TOKEN",)
    docs_url = "https://docs.x.com/x-api/media/quickstart/media-upload-chunked"
    ffmpeg_skill_platform = "x"                        # ffmpeg-skill scripts/_platforms.py owns the delivery spec
    metadata_fields = ("title", "privacy")

    # Endpoints and tuning. Only code changes these (the tests point them at a loopback mock server); no flag,
    # environment variable or file does.
    api_base = API_BASE
    chunk_size = DEFAULT_CHUNK
    timeout_s = 60.0
    max_retries = 10
    max_backoff_s = 64.0
    max_retry_after_s = 300.0
    status_polls = 60
    max_check_after_s = 30.0

    def __init__(self, sleep: Optional[Callable[[float], None]] = None, rand: Optional[Callable[[], float]] = None,
                 now: Optional[Callable[[], float]] = None) -> None:
        self._sleep_fn = sleep
        self._rand_fn = rand
        self._now_fn = now
        self._stats: Dict[str, Any] = {}

    def spec(self) -> Dict[str, Any]:
        return dict(super().spec(), protocol="X API v2 chunked media upload (initialize, append, finalize, status), "
                                             "then POST /2/tweets; OAuth 2.0 user access token",
                    endpoints=[API_BASE + INIT_PATH, API_BASE + APPEND_PATH, API_BASE + FINALIZE_PATH,
                               API_BASE + STATUS_PATH, API_BASE + POST_PATH])

    # ---- the upload ---------------------------------------------------------------------------------------------
    def upload(self, request: UploadRequest, credentials: Any) -> Dict[str, Any]:
        self._check_endpoint()
        if self.chunk_size <= 0:
            raise PublishError("INTERNAL_ERROR", "chunk size must be positive")
        if request.size <= 0:
            raise PublishError("INVALID_ARGUMENT", "the file is empty; nothing was uploaded")
        media_type = CONTENT_TYPES.get(os.path.splitext(request.path)[1].lower())
        if media_type is None:
            raise PublishError("INVALID_ARGUMENT", "X takes MP4, MOV, WebM or MPEG-TS video (.mp4, .mov, .webm, .ts); "
                               "nothing was uploaded", {"platform": self.id, "path": request.path, "uploaded": False})
        ranges = segments(request.size, self.chunk_size)
        if len(ranges) > MAX_SEGMENTS:
            raise PublishError("INVALID_ARGUMENT", f"the file needs more than {MAX_SEGMENTS} segments; nothing was "
                               "uploaded", {"platform": self.id, "uploaded": False})
        md = request.metadata
        post_body(md, "0")                                 # refuses unbound fields before any request
        self._stats = {"requests": 0, "retries": 0, "resumed": 0, "waited_s": 0.0, "bytes_sent": 0, "status_polls": 0}
        token = credentials.get("PUBLISH_X_ACCESS_TOKEN")
        auth = {"Authorization": "Bearer " + token}
        media_id = self._init(auth, media_type, request.size)
        try:
            self._append_all(auth, media_id, request.path, request.size, request.sha256, ranges)
            processing = self._finalize(auth, media_id, request.size)
            self._wait_for_processing(auth, media_id, processing)
        except PublishError as e:
            raise PublishError(e.code, e.message + " (nothing was posted)", dict(e.details, media_id=media_id),
                               e.retryable) from None
        body = post_body(md, media_id)
        post_id = self._post(auth, body, media_id)
        return {"video_id": post_id, "post_id": post_id, "media_id": media_id, "url": None, "privacy": md["privacy"],
                "sent": body, "thumbnail": None, "captions": [], "transfer": dict(self._stats),
                "warnings": ["X's API documentation gives no URL for a post; find it on the account's profile by its "
                             "id"]}

    # ---- step 1: initialise -------------------------------------------------------------------------------------
    def _init(self, auth: Dict[str, str], media_type: str, size: int) -> str:
        body = json.dumps({"media_type": media_type, "total_bytes": size, "media_category": MEDIA_CATEGORY},
                          separators=(",", ":")).encode("utf-8")
        # Retried only after a 429/5xx answer: without one a session may exist, and it would simply expire unused,
        # but nothing is gained by guessing.
        resp = self._api("initialize", "POST", self.api_base + INIT_PATH, body,
                         dict(auth, **{"Content-Type": JSON_TYPE}), retry_without_answer=False)
        data = _data(resp)
        media_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(media_id, str) or not _MEDIA_ID.match(media_id):
            raise self._protocol_error("initialize", "the answer has no data.id of 1-19 digits")
        return media_id

    # ---- step 2: the segments -----------------------------------------------------------------------------------
    def _append_all(self, auth: Dict[str, str], media_id: str, file: str, size: int, sha256: str,
                    ranges: List[range]) -> None:
        url = self.api_base + APPEND_PATH.format(id=media_id)
        hashed, i, attempt = hashlib.sha256(), 0, 0           # `hashed` covers every acknowledged segment
        with open(file, "rb") as fh:
            while i < len(ranges):
                r = ranges[i]
                last = i == len(ranges) - 1
                fh.seek(r.start)
                buf = fh.read(len(r))
                if len(buf) != len(r) or (last and _digest(hashed, buf) != sha256):
                    raise PublishError("CONFIRM_MISMATCH", "the file changed after the confirmation token was checked; "
                                       "its final bytes were not sent, so the upload was not completed. Run the dry run "
                                       "again", {"platform": self.id, "step": "append", "path": file, "uploaded": False})
                boundary = "publish-skill-" + hashlib.sha256(buf[:64] + str(i).encode()).hexdigest()[:24]
                data = multipart({"segment_index": str(i)}, buf, boundary)
                headers = dict(auth, **{"Content-Type": f"multipart/form-data; boundary={boundary}"})
                try:
                    resp: Optional[_Response] = self._send("POST", url, data, headers)
                    self._stats["bytes_sent"] += len(buf)
                except (OSError, http.client.HTTPException):
                    resp = None                                # interrupted: no answer, a reset or a timeout
                if resp is not None and 200 <= resp.status < 300:
                    hashed.update(buf)
                    i, attempt = i + 1, 0
                    continue
                if resp is not None and resp.status not in RETRIABLE_STATUS:
                    raise self._api_error(resp, "append")
                attempt += 1                                   # interrupted, 5xx or 429: the same segment again
                if attempt > self.max_retries:
                    raise self._interrupted("append", resp, i, len(ranges))
                self._backoff(attempt, resp)
                self._stats["resumed"] += 1

    # ---- step 3: finalize ---------------------------------------------------------------------------------------
    def _finalize(self, auth: Dict[str, str], media_id: str, size: int) -> Optional[Dict[str, Any]]:
        resp = self._api("finalize", "POST", self.api_base + FINALIZE_PATH.format(id=media_id), b"", auth,
                         retry_without_answer=False)
        data = _data(resp)
        if not isinstance(data, dict):
            raise self._protocol_error("finalize", "the answer has no data")
        held = data.get("size")
        if held is not None and held != size:
            raise self._protocol_error("finalize", f"X holds {held} bytes, not the file's {size}")
        info = data.get("processing_info")
        return info if isinstance(info, dict) else None

    # ---- step 4: processing -------------------------------------------------------------------------------------
    def _wait_for_processing(self, auth: Dict[str, str], media_id: str, info: Optional[Dict[str, Any]]) -> None:
        """Until processing_info.state is succeeded: no processing_info means the media is ready. `failed` is
        UPLOAD_REJECTED; still pending after `status_polls` queries is UPLOAD_INTERRUPTED (nothing was posted)."""
        url = f"{self.api_base}{STATUS_PATH}?command=STATUS&media_id={quote(media_id)}"
        for n in range(self.status_polls + 1):
            state = str((info or {}).get("state") or "succeeded")
            if state == "succeeded":
                return
            if state == "failed":
                err = (info or {}).get("error")
                why = str(err.get("message") or err.get("name") or "")[:300] if isinstance(err, dict) else ""
                raise PublishError("UPLOAD_REJECTED", "X failed to process the video" + (f" ({why})" if why else "")
                                   + ". Re-export it to the delivery spec: ffmpeg-skill export --preset x",
                                   {"platform": self.id, "step": "status", "uploaded": False})
            if n == self.status_polls:
                break
            wait = info.get("check_after_secs") if isinstance(info, dict) else None
            self._wait(min(float(wait), self.max_check_after_s) if isinstance(wait, (int, float)) and wait > 0 else 1.0)
            self._stats["status_polls"] += 1
            resp = self._api("status", "GET", url, b"", auth, retry_without_answer=True)
            data = _data(resp)
            if not isinstance(data, dict):
                raise self._protocol_error("status", "the answer has no data")
            info = data.get("processing_info") if isinstance(data.get("processing_info"), dict) else None
        raise PublishError("UPLOAD_INTERRUPTED", f"X was still processing the video after {self.status_polls} status "
                           "queries; nothing was posted. Run the same upload again later",
                           {"platform": self.id, "step": "status", "uploaded": False, "transfer": dict(self._stats)})

    # ---- step 5: the post ---------------------------------------------------------------------------------------
    def _post(self, auth: Dict[str, str], body: Dict[str, Any], media_id: str) -> str:
        data_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = dict(auth, **{"Content-Type": JSON_TYPE})
        attempt = 0
        while True:
            try:
                resp: Optional[_Response] = self._send("POST", self.api_base + POST_PATH, data_bytes, headers)
            except (OSError, http.client.HTTPException):
                resp = None
            if resp is not None and resp.status in (200, 201):
                data = _data(resp)
                post_id = data.get("id") if isinstance(data, dict) else None
                if not isinstance(post_id, str) or not post_id:
                    raise self._protocol_error("post", "the answer has no data.id")
                return post_id
            if resp is not None and resp.status == 429:        # refused before anything was posted: retry
                attempt += 1
                if attempt <= self.max_retries:
                    self._backoff(attempt, resp)
                    continue
                raise self._interrupted("post", resp, None, None)
            if resp is None or resp.status >= 500:
                # No answer, or a server error: the post may or may not exist. Never posted twice: stop and say so.
                raise PublishError("UPLOAD_INTERRUPTED", "post: " + ("no answer" if resp is None else
                                   f"X answered {resp.status}") + "; the video is uploaded (media " + media_id + ") but "
                                   "it is unknown whether the post was made. Check the account's profile before running "
                                   "upload again", {"platform": self.id, "step": "post", "media_id": media_id,
                                                    "status": None if resp is None else resp.status, "uploaded": False,
                                                    "transfer": dict(self._stats)}, retryable=False)
            raise self._api_error(resp, "post")

    # ---- requests, retries and errors ---------------------------------------------------------------------------
    def _api(self, what: str, method: str, url: str, body: bytes, headers: Dict[str, str],
             retry_without_answer: bool) -> _Response:
        """One API call with its retries: 429/5xx answers always; no answer only when `retry_without_answer`."""
        attempt = 0
        while True:
            try:
                resp: Optional[_Response] = self._send(method, url, body, headers)
            except (OSError, http.client.HTTPException):
                resp = None
                if not retry_without_answer:
                    raise PublishError("UPLOAD_INTERRUPTED", f"{what} got no answer; nothing was posted. Run the same "
                                       "upload again later", {"platform": self.id, "step": what, "uploaded": False,
                                                              "transfer": dict(self._stats)}) from None
            if resp is not None and 200 <= resp.status < 300:
                return resp
            if resp is not None and resp.status not in RETRIABLE_STATUS:
                raise self._api_error(resp, what)
            attempt += 1
            if attempt > self.max_retries:
                raise self._interrupted(what, resp, None, None)
            self._backoff(attempt, resp)

    def _backoff(self, attempt: int, resp: Optional[_Response]) -> None:
        """random() x 2^attempt seconds, capped; a 429's Retry-After or x-rate-limit-reset (epoch seconds) is waited
        for instead when it is later, up to max_retry_after_s."""
        delay = min(self.max_backoff_s, (self._rand_fn or _rand)() * (2 ** attempt))
        if resp is not None:
            retry_after = (resp.headers.get("retry-after") or "").strip()
            reset = (resp.headers.get("x-rate-limit-reset") or "").strip()
            if retry_after.isdigit():
                delay = max(delay, min(float(retry_after), self.max_retry_after_s))
            elif resp.status == 429 and reset.isdigit():
                delay = max(delay, min(max(0.0, float(reset) - (self._now_fn or _now)()), self.max_retry_after_s))
        self._stats["retries"] += 1
        self._wait(delay)

    def _wait(self, delay: float) -> None:
        self._stats["waited_s"] = round(self._stats["waited_s"] + delay, 3)
        (self._sleep_fn or _sleep)(delay)

    def _send(self, method: str, url: str, body: bytes, headers: Dict[str, str]) -> _Response:
        parts = urlsplit(url)
        conn = self._connection(parts.scheme, parts.hostname or "", parts.port)
        self._stats["requests"] = self._stats.get("requests", 0) + 1
        try:
            conn.request(method, (parts.path or "/") + ("?" + parts.query if parts.query else ""),
                         body=body if method != "GET" else None,
                         headers=dict(headers, **({"Content-Length": str(len(body))} if method != "GET" else {})))
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

    def _interrupted(self, what: str, resp: Optional[_Response], segment: Optional[int],
                     total: Optional[int]) -> PublishError:
        last = {"status": resp.status} if resp is not None else {"status": None, "reason": "no answer"}
        return PublishError("UPLOAD_INTERRUPTED", f"{what}: still failing after {self.max_retries} retries "
                            f"(last: {last['status'] or 'no answer'}); nothing was posted. Run the same upload again "
                            "later", {"platform": self.id, "step": what, "last": last, "segment": segment,
                                      "segments": total, "uploaded": False, "transfer": dict(self._stats)})

    def _protocol_error(self, what: str, why: str) -> PublishError:
        return PublishError("UPLOAD_REJECTED", f"{what}: {why}; nothing was posted",
                            {"platform": self.id, "step": what, "uploaded": False})

    def _api_error(self, resp: _Response, what: str) -> PublishError:
        problem = _problem(resp)
        details = {"platform": self.id, "step": what, "status": resp.status, "uploaded": False, **problem}
        said = problem.get("detail") or problem.get("title") or ""
        if resp.status == 401:
            return PublishError("AUTH_FAILED", f"{what}: X refused the access token (401); store a new OAuth 2.0 user "
                                "access token with the media.write, tweet.write, tweet.read and users.read scopes with "
                                "`publish auth login --platform x` (values are never printed). Nothing was posted",
                                details)
        if resp.status == 403 and "scope" in said.lower():
            return PublishError("AUTH_FAILED", f"{what}: the access token lacks a scope (403: {said}); nothing was "
                                "posted", details)
        return PublishError("UPLOAD_REJECTED", f"{what}: X refused the request ({resp.status}"
                            + (f": {said}" if said else "") + "); nothing was posted", details)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _rand() -> float:
    return random.random()


def _now() -> float:
    return time.time()


def _digest(hashed: Any, tail: bytes) -> str:
    h = hashed.copy()
    h.update(tail)
    return h.hexdigest()


def _data(resp: _Response) -> Any:
    doc = resp.json()
    return doc.get("data") if isinstance(doc, dict) else None


def _problem(resp: _Response) -> Dict[str, Any]:
    """The parts of an X error answer that are safe to report: a problem's type / title / detail (application/problem
    +json), or the first error's code / message ({"errors": [...]} or {"code", "message"}), each at most 300
    characters. Never a header."""
    doc = resp.json()
    if not isinstance(doc, dict):
        return {}
    if isinstance(doc.get("errors"), list) and doc["errors"] and isinstance(doc["errors"][0], dict):
        doc = dict(doc["errors"][0], **{k: v for k, v in doc.items() if k in ("type", "title", "detail")})
    out: Dict[str, Any] = {}
    for k in ("type", "title", "detail", "message"):
        if isinstance(doc.get(k), str) and doc[k]:
            out[k if k != "message" else "detail"] = doc[k][:300]
    if isinstance(doc.get("code"), int):
        out["error_code"] = doc["code"]
    return out
