"""LinkedIn uploader (roadmap step 5, ADR-008): the Videos API multipart upload of the video the confirmation token
bound, then one post carrying it with the Posts API, with the bound title as the post's commentary, authorised by an
OAuth 2.0 member access token, on behalf of the member or organization named by PUBLISH_LINKEDIN_AUTHOR.

This module and the other uploader modules (platforms/tiktok.py, platforms/vimeo.py, platforms/x.py,
platforms/youtube.py) are the only modules in the package with network code (ADR-001's source rule exempts exactly
these files). It runs only from upload.py's gate, after the exact --confirm token matched and the credentials were
read, so every refusal has already happened when the first byte leaves the machine. Standard library only.

Protocol (sources in docs/decisions.md ADR-008, read 2026-10-31). Every API request carries Authorization: Bearer
<token>, Linkedin-Version: <YYYYMM> and X-Restli-Protocol-Version: 2.0.0:
  1. Initialise: POST https://api.linkedin.com/rest/videos?action=initializeUpload with {"initializeUploadRequest":
     {"owner": <author URN>, "fileSizeBytes", "uploadCaptions": false, "uploadThumbnail": false}}; the answer's
     value carries the video URN, uploadToken and uploadInstructions (firstByte, lastByte, uploadUrl), in order.
  2. Parts: PUT each byte range to its uploadUrl (Content-Type: application/octet-stream, no Authorization: the URL
     is signed); the answer's ETag is the part's id.
  3. Finalize: POST /rest/videos?action=finalizeUpload with {"finalizeUploadRequest": {"video", "uploadToken",
     "uploadedPartIds"}} (the ETags, in the order of the instructions).
  4. Status: GET /rest/videos/{urn} until status is AVAILABLE (PROCESSING_FAILED: processingFailureReason), a bounded
     number of times.
  5. Post: POST /rest/posts {"author", "commentary", "visibility": "PUBLIC", "distribution", "content": {"media":
     {"id": <video URN>}}, "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": false}; 201, the post's URN in
     the x-restli-id header.

The upload URLs are capability URLs (signed): each is recorded with credentials.hold, so the redactor removes it from
every output, and each must be https on a linkedin.com host. The access token goes to api.linkedin.com only.

Exactly what the token bound is sent: the commentary is the bound title with every `little` reserved character
escaped, so the text appears as typed (no mention or hashtag markup is interpreted), and nothing else is added.
LinkedIn's visibilities are PUBLIC, CONNECTIONS and LOGGED_IN; none of them is private or unlisted, so `public` is
the only privacy offered (plan FAILs the default `private`). The file is hashed as its parts are acknowledged, and the
final part is held back unless the bytes so far plus that part have the sha256 the token bound (CONFIRM_MISMATCH)."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import re
import ssl
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote, unquote, urlsplit

from ..errors import PublishError
from .base import Uploader, UploadRequest

API_BASE = "https://api.linkedin.com"
INIT_PATH = "/rest/videos?action=initializeUpload"
FINALIZE_PATH = "/rest/videos?action=finalizeUpload"
VIDEO_PATH = "/rest/videos/{urn}"
POST_PATH = "/rest/posts"
JSON_TYPE = "application/json"
# "All APIs require the request headers Linkedin-Version: {Version in YYYYMM format} and X-Restli-Protocol-Version:
# 2.0.0". The latest version documented on 2026-10-31; LinkedIn sunsets a version about a year after its release.
LINKEDIN_VERSION = "202610"
RESTLI_VERSION = "2.0.0"
UPLOAD_HOST = "linkedin.com"                         # uploadUrl: https://www.linkedin.com/dms-uploads/...
MIN_BYTES = 75 * 1024                                # "Between 75kb and 500MB": the larger reading of the minimum
MAX_BYTES = 500 * 1000 ** 2                          # ... and the smaller reading of the maximum
FILE_FORMATS = (".mp4",)                             # "File format: MP4."
PRIVACY = ("public",)                                # PUBLIC; CONNECTIONS and LOGGED_IN are neither private nor unlisted
VISIBILITY = {"public": "PUBLIC"}
# `little` text: "All reserved characters need to be escaped with a backslash"
LITTLE_RESERVED = "\\|{}@[]()<>#*_~"
RETRIABLE_STATUS = (429, 500, 502, 503, 504)
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")   # plain http only to these: the tests' mock server
_AUTHOR = re.compile(r"^urn:li:(person|organization):[A-Za-z0-9_-]{1,100}$")
_VIDEO = re.compile(r"^urn:li:video:[A-Za-z0-9_-]{1,100}$")
_POST = re.compile(r"^urn:li:(share|ugcPost):[0-9]{1,30}$")


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


def little_escape(text: str) -> str:
    """The text as `little` plain text: every reserved character preceded by a backslash (the backslash itself first)."""
    return "".join("\\" + ch if ch in LITTLE_RESERVED else ch for ch in text)


def post_body(metadata: Dict[str, Any], author: str, video: str) -> Dict[str, Any]:
    """The create-post body for the bound metadata: the escaped title as the commentary ("" when none is bound; the
    field is required), PUBLIC, the main feed, the video. Nothing else is added (no media title, no call to action, no
    targeting)."""
    if metadata.get("privacy") not in PRIVACY:
        raise PublishError("INVALID_ARGUMENT", f"privacy {metadata.get('privacy')!r}: the LinkedIn uploader posts "
                           "publicly only")
    for field in ("description", "tags", "thumbnail", "captions"):
        if metadata.get(field):
            raise PublishError("INVALID_ARGUMENT", f"the LinkedIn uploader does not send {field}; nothing was uploaded")
    return {"author": author, "commentary": little_escape(metadata.get("title") or ""),
            "visibility": VISIBILITY[metadata["privacy"]],
            "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
            "content": {"media": {"id": video}}, "lifecycleState": "PUBLISHED", "isReshareDisabledByAuthor": False}


def parts_of(instructions: Any, size: int) -> List[Tuple[int, int, str]]:
    """(firstByte, lastByte, uploadUrl) of each part, checked: in order, contiguous, covering exactly [0, size)."""
    if not isinstance(instructions, list) or not instructions:
        raise ValueError("no uploadInstructions")
    out, expect = [], 0
    for p in instructions:
        if not isinstance(p, dict):
            raise ValueError("an upload instruction is not an object")
        first, last, url = p.get("firstByte"), p.get("lastByte"), p.get("uploadUrl")
        if not (isinstance(first, int) and isinstance(last, int) and isinstance(url, str) and url):
            raise ValueError("an upload instruction lacks firstByte, lastByte or uploadUrl")
        if first != expect or last < first:
            raise ValueError("the upload instructions are not contiguous byte ranges in order")
        out.append((first, last, url))
        expect = last + 1
    if expect != size:
        raise ValueError(f"the upload instructions cover {expect} bytes, not the file's {size}")
    return out


class LinkedInUploader(Uploader):
    id = "linkedin"
    name = "LinkedIn"
    roadmap_step = 5
    implemented = True
    # an OAuth 2.0 member access token with w_member_social (a person author) or w_organization_social (an
    # organization author), and the author's URN (ADR-003, ADR-008)
    credential_env = ("PUBLISH_LINKEDIN_ACCESS_TOKEN", "PUBLISH_LINKEDIN_AUTHOR")
    docs_url = "https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/videos-api"
    ffmpeg_skill_platform = "linkedin"                 # ffmpeg-skill scripts/_platforms.py owns the delivery spec
    metadata_fields = ("title", "privacy")

    # Endpoints and tuning. Only code changes these (the tests point them at a loopback mock server); no flag,
    # environment variable or file does.
    api_base = API_BASE
    timeout_s = 60.0
    max_retries = 10
    max_backoff_s = 64.0
    max_retry_after_s = 300.0
    status_polls = 120
    poll_interval_s = 5.0                            # not documented; 120 x 5 s = 10 minutes

    def __init__(self, sleep: Optional[Callable[[float], None]] = None,
                 rand: Optional[Callable[[], float]] = None) -> None:
        self._sleep_fn = sleep
        self._rand_fn = rand
        self._stats: Dict[str, Any] = {}

    def spec(self) -> Dict[str, Any]:
        return dict(super().spec(), protocol="LinkedIn Videos API multipart upload (initializeUpload, PUT parts, "
                                             "finalizeUpload, status), then POST /rest/posts; OAuth 2.0 member access "
                                             "token",
                    endpoints=[API_BASE + INIT_PATH, "uploadInstructions[].uploadUrl (https, *.linkedin.com)",
                               API_BASE + FINALIZE_PATH, API_BASE + VIDEO_PATH, API_BASE + POST_PATH],
                    linkedin_version=LINKEDIN_VERSION)

    # ---- the upload ---------------------------------------------------------------------------------------------
    def upload(self, request: UploadRequest, credentials: Any) -> Dict[str, Any]:
        self._check_endpoint()
        if request.size <= 0:
            raise PublishError("INVALID_ARGUMENT", "the file is empty; nothing was uploaded")
        if os.path.splitext(request.path)[1].lower() not in FILE_FORMATS:
            raise PublishError("INVALID_ARGUMENT", "LinkedIn takes MP4 video (.mp4); nothing was uploaded",
                               {"platform": self.id, "path": request.path, "uploaded": False})
        md = request.metadata
        author = credentials.get("PUBLISH_LINKEDIN_AUTHOR")
        if not _AUTHOR.match(author or ""):
            raise PublishError("INVALID_ARGUMENT", "PUBLISH_LINKEDIN_AUTHOR must be a person or organization URN "
                               "(urn:li:person:<id> or urn:li:organization:<id>); its value is not printed. Nothing was "
                               "uploaded", {"platform": self.id, "uploaded": False})
        post_body(md, author, "urn:li:video:0")          # refuses unbound fields before any request
        self._stats = {"requests": 0, "retries": 0, "resumed": 0, "waited_s": 0.0, "bytes_sent": 0, "status_polls": 0}
        auth = {"Authorization": "Bearer " + credentials.get("PUBLISH_LINKEDIN_ACCESS_TOKEN"),
                "Linkedin-Version": LINKEDIN_VERSION, "X-Restli-Protocol-Version": RESTLI_VERSION}
        video, upload_token, parts = self._init(auth, author, request.size)
        try:
            etags = self._put_all(video, request.path, request.sha256, parts)
            self._finalize(auth, video, upload_token, etags)
            self._wait_until_available(auth, video)
        except PublishError as e:
            raise PublishError(e.code, e.message + " (nothing was posted)", dict(e.details, video=video),
                               e.retryable) from None
        body = post_body(md, author, video)
        post = self._post(auth, body, video)
        url = f"https://www.linkedin.com/feed/update/{post}/" if post.startswith("urn:li:ugcPost:") else None
        warnings = [] if url else ["LinkedIn's documentation gives a URL for a ugcPost only; find this post on the "
                                   "author's page by its URN"]
        return {"video_id": post, "post_id": post, "video": video, "url": url, "privacy": md["privacy"],
                "visibility": body["visibility"], "sent": body, "thumbnail": None, "captions": [],
                "transfer": dict(self._stats), "warnings": warnings}

    # ---- step 1: initialise -------------------------------------------------------------------------------------
    def _init(self, auth: Dict[str, str], author: str, size: int) -> Tuple[str, str, List[Tuple[int, int, str]]]:
        body = json.dumps({"initializeUploadRequest": {"owner": author, "fileSizeBytes": size, "uploadCaptions": False,
                                                       "uploadThumbnail": False}}, separators=(",", ":")).encode("utf-8")
        # Retried only after a 429/5xx answer: without one, an upload may have been registered; it would simply
        # expire unused, but nothing is gained by guessing.
        resp = self._api("initialize", "POST", self.api_base + INIT_PATH, body,
                         dict(auth, **{"Content-Type": JSON_TYPE}), retry_without_answer=False)
        doc = resp.json()
        value = doc.get("value") if isinstance(doc, dict) else None
        if not isinstance(value, dict):
            raise self._protocol_error("initialize", "the answer has no value")
        video, upload_token = value.get("video"), value.get("uploadToken", "")
        if not isinstance(video, str) or not _VIDEO.match(video):
            raise self._protocol_error("initialize", "the answer has no video URN")
        if not isinstance(upload_token, str):
            raise self._protocol_error("initialize", "the answer's uploadToken is not a string")
        try:
            parts = parts_of(value.get("uploadInstructions"), size)
        except ValueError as e:
            raise self._protocol_error("initialize", str(e)) from None
        for _, _, url in parts:
            _hold(url)
            self._check_upload_url(url)
        return video, upload_token, parts

    # ---- step 2: the parts --------------------------------------------------------------------------------------
    def _put_all(self, video: str, file: str, sha256: str, parts: List[Tuple[int, int, str]]) -> List[str]:
        hashed, etags, i, attempt = hashlib.sha256(), [], 0, 0      # `hashed` covers every acknowledged part
        with open(file, "rb") as fh:
            while i < len(parts):
                first, last, url = parts[i]
                fh.seek(first)
                buf = fh.read(last - first + 1)
                if len(buf) != last - first + 1 or (i == len(parts) - 1 and _digest(hashed, buf) != sha256):
                    raise PublishError("CONFIRM_MISMATCH", "the file changed after the confirmation token was checked; "
                                       "its final bytes were not sent, so the upload was not completed. Run the dry run "
                                       "again", {"platform": self.id, "step": "upload", "path": file, "uploaded": False})
                try:
                    resp: Optional[_Response] = self._send("PUT", url, buf, {"Content-Type": "application/octet-stream"})
                    self._stats["bytes_sent"] += len(buf)
                except (OSError, http.client.HTTPException):
                    resp = None                                # interrupted: no answer, a reset or a timeout
                if resp is not None and 200 <= resp.status < 300:
                    etag = (resp.headers.get("etag") or "").strip()
                    if etag.startswith("W/"):
                        etag = etag[2:]
                    etag = etag[1:-1] if len(etag) >= 2 and etag[0] == etag[-1] == '"' else etag
                    if not etag:
                        raise self._protocol_error("upload", f"part {i + 1} of {len(parts)} was answered without an "
                                                   "ETag")
                    hashed.update(buf)
                    etags.append(etag)
                    i, attempt = i + 1, 0
                    continue
                if resp is not None and resp.status not in RETRIABLE_STATUS:
                    raise self._api_error(resp, "upload")
                attempt += 1                                   # interrupted, 5xx or 429: the same part again
                if attempt > self.max_retries:
                    raise self._interrupted("upload", resp, i, len(parts))
                self._backoff(attempt, resp)
                self._stats["resumed"] += 1
        return etags

    # ---- step 3: finalize ---------------------------------------------------------------------------------------
    def _finalize(self, auth: Dict[str, str], video: str, upload_token: str, etags: List[str]) -> None:
        body = json.dumps({"finalizeUploadRequest": {"video": video, "uploadToken": upload_token,
                                                     "uploadedPartIds": etags}}, separators=(",", ":")).encode("utf-8")
        self._api("finalize", "POST", self.api_base + FINALIZE_PATH, body, dict(auth, **{"Content-Type": JSON_TYPE}),
                  retry_without_answer=False)

    # ---- step 4: processing -------------------------------------------------------------------------------------
    def _wait_until_available(self, auth: Dict[str, str], video: str) -> None:
        """GET the video until its status is AVAILABLE. PROCESSING_FAILED is UPLOAD_REJECTED; still PROCESSING (or
        WAITING_UPLOAD) after `status_polls` queries is UPLOAD_INTERRUPTED (nothing was posted)."""
        url = self.api_base + VIDEO_PATH.format(urn=quote(video, safe=""))
        for n in range(self.status_polls):
            if n:
                self._wait(self.poll_interval_s)
            self._stats["status_polls"] += 1
            resp = self._api("status", "GET", url, b"", auth, retry_without_answer=True)
            doc = resp.json()
            state = doc.get("status") if isinstance(doc, dict) else None
            if state == "AVAILABLE":
                return
            if state == "PROCESSING_FAILED":
                why = str(doc.get("processingFailureReason") or "")[:300]
                raise PublishError("UPLOAD_REJECTED", "LinkedIn failed to process the video" + (f" ({why})" if why else "")
                                   + ". Re-export it to the delivery spec: ffmpeg-skill export --preset linkedin",
                                   {"platform": self.id, "step": "status", "uploaded": False})
            if state not in ("PROCESSING", "WAITING_UPLOAD"):
                raise self._protocol_error("status", f"unknown video status {str(state)[:40]!r}")
        raise PublishError("UPLOAD_INTERRUPTED", f"LinkedIn was still processing the video after {self.status_polls} "
                           "status queries; nothing was posted. Run the same upload again later",
                           {"platform": self.id, "step": "status", "uploaded": False, "transfer": dict(self._stats)})

    # ---- step 5: the post ---------------------------------------------------------------------------------------
    def _post(self, auth: Dict[str, str], body: Dict[str, Any], video: str) -> str:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers = dict(auth, **{"Content-Type": JSON_TYPE})
        attempt = 0
        while True:
            try:
                resp: Optional[_Response] = self._send("POST", self.api_base + POST_PATH, data, headers)
            except (OSError, http.client.HTTPException):
                resp = None
            if resp is not None and resp.status in (200, 201):
                post = unquote((resp.headers.get("x-restli-id") or "").strip())
                if not _POST.match(post):
                    raise self._protocol_error("post", "the answer has no post URN in x-restli-id")
                return post
            if resp is not None and resp.status == 429:        # refused before anything was posted: retry
                attempt += 1
                if attempt <= self.max_retries:
                    self._backoff(attempt, resp)
                    continue
                raise self._interrupted("post", resp, None, None)
            if resp is None or resp.status >= 500:
                # No answer, or a server error: the post may or may not exist. Never posted twice: stop and say so.
                raise PublishError("UPLOAD_INTERRUPTED", "post: " + ("no answer" if resp is None else
                                   f"LinkedIn answered {resp.status}") + "; the video is uploaded (" + video + ") but "
                                   "it is unknown whether the post was made. Check the author's page before running "
                                   "upload again", {"platform": self.id, "step": "post", "video": video,
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

    def _check_upload_url(self, url: str) -> None:
        """The file goes only to LinkedIn: an https upload URL on a linkedin.com host. Only a loopback API base (the
        tests' mock) may hand out an upload URL on its own scheme, host and port."""
        base, loc = urlsplit(self.api_base), urlsplit(url)
        host = (loc.hostname or "").lower()
        on_linkedin = loc.scheme == "https" and (host == UPLOAD_HOST or host.endswith("." + UPLOAD_HOST))
        on_test_mock = base.hostname in LOOPBACK_HOSTS and (loc.scheme, loc.hostname, loc.port) == (
            base.scheme, base.hostname, base.port)
        if not (on_linkedin or on_test_mock):
            raise self._protocol_error("initialize", "an upload URL is not https on a linkedin.com host")

    def _interrupted(self, what: str, resp: Optional[_Response], part: Optional[int],
                     total: Optional[int]) -> PublishError:
        last = {"status": resp.status} if resp is not None else {"status": None, "reason": "no answer"}
        return PublishError("UPLOAD_INTERRUPTED", f"{what}: still failing after {self.max_retries} retries "
                            f"(last: {last['status'] or 'no answer'}); nothing was posted. Run the same upload again "
                            "later", {"platform": self.id, "step": what, "last": last, "part": part, "parts": total,
                                      "uploaded": False, "transfer": dict(self._stats)})

    def _protocol_error(self, what: str, why: str) -> PublishError:
        return PublishError("UPLOAD_REJECTED", f"{what}: {why}; nothing was posted",
                            {"platform": self.id, "step": what, "uploaded": False})

    def _api_error(self, resp: _Response, what: str) -> PublishError:
        problem = _problem(resp)
        details = {"platform": self.id, "step": what, "status": resp.status, "uploaded": False, **problem}
        said = problem.get("message") or ""
        if resp.status in (401, 403):
            return PublishError("AUTH_FAILED", f"{what}: LinkedIn refused the access token or its permission "
                                f"({resp.status}" + (f": {said}" if said else "") + "); store a new member access token "
                                "with w_member_social (or w_organization_social and a page role for an organization "
                                "author) with `publish auth login --platform linkedin` (values are never printed). "
                                "Nothing was posted", details)
        return PublishError("UPLOAD_REJECTED", f"{what}: LinkedIn refused the request ({resp.status}"
                            + (f": {said}" if said else "") + "); nothing was posted", details)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _rand() -> float:
    return random.random()


def _digest(hashed: Any, tail: bytes) -> str:
    h = hashed.copy()
    h.update(tail)
    return h.hexdigest()


def _problem(resp: _Response) -> Dict[str, Any]:
    """The parts of a LinkedIn error answer that are safe to report: `message` and `code` (strings, at most 300
    characters) and `serviceErrorCode` (an integer). Never a header."""
    doc = resp.json()
    if not isinstance(doc, dict):
        return {}
    out: Dict[str, Any] = {}
    for k in ("message", "code"):
        if isinstance(doc.get(k), str) and doc[k]:
            out[k if k != "code" else "error_code"] = doc[k][:300]
    if isinstance(doc.get("serviceErrorCode"), int):
        out["service_error_code"] = doc["serviceErrorCode"]
    return out
