"""YouTube uploader (roadmap step 4, ADR-004): the YouTube Data API v3 resumable upload protocol, for the video
(videos.insert), the thumbnail (thumbnails.set) and the captions (captions.insert) the confirmation token bound.

This is the only module in the package with network code (ADR-001's source rule exempts exactly this file). It runs
only from upload.py's gate, after the exact --confirm token matched and the credentials were read, so every refusal
has already happened when the first byte leaves the machine. It uses the standard library only: http.client, because
the resumable protocol answers an incomplete upload with 308 and a Range header, which urllib's redirect handling
would treat as a redirect.

Protocol (sources in docs/decisions.md ADR-004):
  1. OAuth 2.0: POST https://oauth2.googleapis.com/token, form-encoded client_id, client_secret, refresh_token and
     grant_type=refresh_token; the JSON answer carries access_token (and the granted `scope`).
  2. Start a resumable session: POST https://www.googleapis.com/upload/youtube/v3/<method>?uploadType=resumable with
     Authorization: Bearer, X-Upload-Content-Length, X-Upload-Content-Type and (videos, captions) the JSON resource;
     200 OK and the session URI in Location.
  3. PUT the file in chunks to the session URI, each a multiple of 256 KiB except the last, with
     Content-Range: bytes FIRST-LAST/TOTAL; 308 Resume Incomplete (Range: bytes=0-N) after each, 200/201 and the
     resource after the last.
  4. After an interrupted chunk (no answer, a timeout, 5xx or 429): wait (exponential backoff, or Retry-After), ask the
     session how far it got (PUT, Content-Range: bytes */TOTAL, empty body) and resume from the byte after its Range.
     404 means the session expired: start a new session and upload from the beginning.

Exactly what the token bound is sent. The video resource is built from the bound metadata only (title, description,
tags, privacy; nothing else, no default added), the thumbnail and each caption file are the bound files, and every
file is hashed as its bytes are sent: the final chunk is held back unless the bytes sent so far plus that chunk have the
sha256 the token bound. YouTube creates the video only when the last byte arrives, so a file edited after the gate is
never published (CONFIRM_MISMATCH).

The access token and the session URIs (which carry the upload id) are recorded with credentials.hold, so the
redactor removes them from every output, as it does the credentials."""
from __future__ import annotations

import hashlib
import http.client
import json
import random
import re
import ssl
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urlencode, urlsplit

from ..errors import PublishError
from ..metadata import image_kind
from .base import Uploader, UploadRequest

TOKEN_URL = "https://oauth2.googleapis.com/token"
API_BASE = "https://www.googleapis.com"
WATCH_URL = "https://www.youtube.com/watch?v="
CHUNK_UNIT = 256 * 1024                        # "a multiple of 256 KB" (the resumable upload guide)
DEFAULT_CHUNK = 32 * CHUNK_UNIT                # 8 MiB
VIDEO_MIME = "application/octet-stream"        # accepted by videos.insert; the bytes decide the format
CAPTIONS_MIME = "application/octet-stream"     # accepted by captions.insert
RETRIABLE_STATUS = (429, 500, 502, 503, 504)
RATE_LIMIT_REASONS = ("rateLimitExceeded", "userRateLimitExceeded", "uploadRateLimitExceeded")
QUOTA_REASONS = ("quotaExceeded", "dailyLimitExceeded")
AUTH_REASONS = ("authError", "insufficientPermissions", "unauthorized")
PRIVACY_VALUES = ("private", "unlisted", "public")
_SCOPE = "https://www.googleapis.com/auth/"
UPLOAD_SCOPES = tuple(_SCOPE + s for s in ("youtube.upload", "youtube", "youtube.force-ssl", "youtubepartner"))
CAPTION_SCOPES = tuple(_SCOPE + s for s in ("youtube.force-ssl", "youtubepartner"))
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")      # plain http only to these: the tests' mock server
_RANGE = re.compile(r"^bytes=0-([0-9]+)$")


class _Response:
    def __init__(self, status: int, headers: Dict[str, str], body: bytes) -> None:
        self.status, self.headers, self.body = status, headers, body

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8") or "null")
        except (UnicodeDecodeError, ValueError):
            return None


class _SessionExpired(Exception):
    """404 from a session URI: start a new session and upload from the beginning."""


def _hold(value: str) -> None:
    from .. import credentials                         # imported here: credentials imports the platform registry
    credentials.hold(value)


def video_resource(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """The videos.insert resource for the bound metadata: exactly its title, description, tags and privacy. A field
    the metadata leaves unset is left out; nothing is added (no category, no language, no default)."""
    privacy = metadata["privacy"]
    if privacy not in PRIVACY_VALUES:
        raise PublishError("INVALID_ARGUMENT", f"privacy {privacy!r} is not one of {', '.join(PRIVACY_VALUES)}")
    snippet: Dict[str, Any] = {}
    if metadata.get("title") is not None:
        snippet["title"] = metadata["title"]
    if metadata.get("description") is not None:
        snippet["description"] = metadata["description"]
    if metadata.get("tags"):
        snippet["tags"] = list(metadata["tags"])
    resource: Dict[str, Any] = {"status": {"privacyStatus": privacy}}
    if snippet:
        resource["snippet"] = snippet
    return resource


def caption_resource(video_id: str, caption: Dict[str, Any]) -> Dict[str, Any]:
    """The captions.insert resource: the bound language, which also names the track; published with the video."""
    return {"snippet": {"videoId": video_id, "language": caption["language"], "name": caption["language"], "isDraft": False}}


class YouTubeUploader(Uploader):
    id = "youtube"
    name = "YouTube"
    roadmap_step = 4
    implemented = True
    # OAuth 2.0 installed-app credentials, from the environment or the OS keychain (`publish auth login`, ADR-003).
    credential_env = ("PUBLISH_YOUTUBE_CLIENT_ID", "PUBLISH_YOUTUBE_CLIENT_SECRET", "PUBLISH_YOUTUBE_REFRESH_TOKEN")
    docs_url = "https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol"
    ffmpeg_skill_platform = "youtube"

    # Endpoints and tuning. Only code changes these (the tests point them at a loopback mock server); no flag,
    # environment variable or file does.
    token_url = TOKEN_URL
    api_base = API_BASE
    chunk_size = DEFAULT_CHUNK
    timeout_s = 60.0                # each request: connect, send and wait for the answer
    max_retries = 10                # per request (the upload guide's sample uses 10)
    max_backoff_s = 64.0
    max_retry_after_s = 300.0
    max_session_restarts = 2

    def __init__(self, sleep: Optional[Callable[[float], None]] = None, rand: Optional[Callable[[], float]] = None) -> None:
        self._sleep_fn = sleep
        self._rand_fn = rand
        self._stats: Dict[str, Any] = {}

    def spec(self) -> Dict[str, Any]:
        return dict(super().spec(), protocol="YouTube Data API v3 resumable upload (videos.insert, thumbnails.set, "
                                             "captions.insert); OAuth 2.0 refresh token",
                    endpoints=[TOKEN_URL, API_BASE + "/upload/youtube/v3/"])

    # ---- the upload ---------------------------------------------------------------------------------------------
    def upload(self, request: UploadRequest, credentials: Any) -> Dict[str, Any]:
        self._check_endpoints()
        if self.chunk_size <= 0 or self.chunk_size % CHUNK_UNIT:
            raise PublishError("INTERNAL_ERROR", f"chunk size must be a positive multiple of {CHUNK_UNIT} bytes")
        self._stats = {"requests": 0, "retries": 0, "resumed": 0, "session_restarts": 0, "waited_s": 0.0, "bytes_sent": 0}
        md = request.metadata
        token, scopes = self._access_token(credentials)
        self._check_scopes(scopes, md)                       # before any byte of the video is sent
        resource = video_resource(md)
        video = self._resumable("videos.insert", "/upload/youtube/v3/videos", {"part": ",".join(sorted(resource))},
                                resource, request.path, request.size, request.sha256, VIDEO_MIME, token)
        video_id = video.get("id") if isinstance(video, dict) else None
        if not isinstance(video_id, str) or not video_id:
            raise PublishError("UPLOAD_REJECTED", "videos.insert finished without a video id; check YouTube Studio before "
                               "uploading again", {"platform": self.id, "step": "videos.insert", "uploaded": None})
        sent: Dict[str, Any] = {"video": resource, "thumbnail": None, "captions": []}
        result: Dict[str, Any] = {"video_id": video_id, "url": WATCH_URL + video_id,
                                  "privacy": (video.get("status") or {}).get("privacyStatus"), "sent": sent,
                                  "thumbnail": None, "captions": [], "warnings": []}
        step = "thumbnails.set"
        try:
            thumb = md.get("thumbnail")
            if thumb:
                kind = image_kind(thumb["path"])
                sent["thumbnail"] = {"sha256": thumb["sha256"], "size": thumb["size"], "type": kind}
                self._resumable(step, "/upload/youtube/v3/thumbnails/set", {"videoId": video_id}, None, thumb["path"],
                                thumb["size"], thumb["sha256"], kind, token)
                result["thumbnail"] = {"set": True, "type": kind}
            step = "captions.insert"
            for cap in md.get("captions") or []:
                body = caption_resource(video_id, cap)
                sent["captions"].append({"sha256": cap["sha256"], "size": cap["size"], "resource": body})
                done = self._resumable(step, "/upload/youtube/v3/captions", {"part": "snippet"}, body, cap["path"],
                                       cap["size"], cap["sha256"], CAPTIONS_MIME, token)
                result["captions"].append({"id": (done or {}).get("id") if isinstance(done, dict) else None,
                                           "language": cap["language"]})
        except PublishError as e:
            raise PublishError("UPLOAD_PARTIAL", f"the video was uploaded ({WATCH_URL + video_id}, privacy "
                               f"{md['privacy']}) but {step} failed ({e.code}: {e.message}); add the rest in YouTube "
                               "Studio. Do not run upload again: it would upload the video a second time",
                               {"platform": self.id, "uploaded": True, "video_id": video_id, "url": WATCH_URL + video_id,
                                "failed_step": step, "cause": e.to_dict(), "thumbnail": result["thumbnail"],
                                "captions": result["captions"], "transfer": dict(self._stats)}) from None
        if result["privacy"] is not None and result["privacy"] != md["privacy"]:
            result["warnings"].append(f"sent privacy {md['privacy']!r}, YouTube reports {result['privacy']!r} "
                                      "(the channel's or the API project's settings can override it)")
        result["transfer"] = dict(self._stats)
        result["processing"] = "YouTube processes the video after the upload; it may not be watchable yet"
        return result

    # ---- OAuth --------------------------------------------------------------------------------------------------
    def _access_token(self, credentials: Any) -> Tuple[str, Optional[Set[str]]]:
        form = urlencode({"client_id": credentials.get("PUBLISH_YOUTUBE_CLIENT_ID"),
                          "client_secret": credentials.get("PUBLISH_YOUTUBE_CLIENT_SECRET"),
                          "refresh_token": credentials.get("PUBLISH_YOUTUBE_REFRESH_TOKEN"),
                          "grant_type": "refresh_token"}).encode("ascii")
        resp = self._request_retrying("POST", self.token_url, form,
                                      {"Content-Type": "application/x-www-form-urlencoded"}, "oauth token")
        doc = resp.json()
        if resp.status == 200 and isinstance(doc, dict) and isinstance(doc.get("access_token"), str) and doc["access_token"]:
            _hold(doc["access_token"])
            scope = doc.get("scope")
            return doc["access_token"], (set(scope.split()) if isinstance(scope, str) else None)
        code = doc.get("error") if isinstance(doc, dict) and isinstance(doc.get("error"), str) else None
        if resp.status in (400, 401, 403):
            raise PublishError("AUTH_FAILED", f"Google refused the refresh token ({code or resp.status}); store a new one "
                               "with `publish auth login --platform youtube` (values are never printed). Nothing was uploaded",
                               {"platform": self.id, "step": "oauth token", "status": resp.status, "error": code,
                                "uploaded": False})
        raise PublishError("UPLOAD_REJECTED", f"the token endpoint answered {resp.status}. Nothing was uploaded",
                           {"platform": self.id, "step": "oauth token", "status": resp.status, "uploaded": False})

    def _check_scopes(self, granted: Optional[Set[str]], md: Dict[str, Any]) -> None:
        if granted is None:                                    # the answer did not list scopes: the API decides
            return
        needs: List[Tuple[str, Tuple[str, ...]]] = [("videos.insert", UPLOAD_SCOPES)]
        if md.get("thumbnail"):
            needs.append(("thumbnails.set", UPLOAD_SCOPES))
        if md.get("captions"):
            needs.append(("captions.insert", CAPTION_SCOPES))
        for step, any_of in needs:
            if not granted.intersection(any_of):
                raise PublishError("AUTH_FAILED", f"the refresh token was not granted a scope {step} needs (one of "
                                   f"{', '.join(any_of)}); obtain one with that scope. Nothing was uploaded",
                                   {"platform": self.id, "step": step, "needs_one_of": list(any_of),
                                    "granted": sorted(granted), "uploaded": False})

    # ---- the resumable protocol ---------------------------------------------------------------------------------
    def _resumable(self, what: str, path: str, query: Dict[str, str], resource: Optional[Dict[str, Any]], file: str,
                   size: int, sha256: str, mime: str, token: str) -> Any:
        restarts = 0
        while True:
            session = self._initiate(what, path, query, resource, size, mime, token)
            try:
                return self._transfer(what, session, file, size, sha256, mime, token)
            except _SessionExpired:
                restarts += 1
                self._stats["session_restarts"] += 1
                if restarts > self.max_session_restarts:
                    raise PublishError("UPLOAD_INTERRUPTED", f"{what}: the upload session expired {restarts} times; "
                                       "nothing was published. Run the same upload again",
                                       {"platform": self.id, "step": what, "uploaded": False}) from None

    def _initiate(self, what: str, path: str, query: Dict[str, str], resource: Optional[Dict[str, Any]], size: int,
                  mime: str, token: str) -> str:
        url = self.api_base + path + "?" + urlencode(dict(query, uploadType="resumable"))
        headers = {"Authorization": "Bearer " + token, "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": mime}
        body = b""
        if resource is not None:
            body = json.dumps(resource, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=UTF-8"
        resp = self._request_retrying("POST", url, body, headers, what)
        if resp.status != 200:
            raise self._api_error(resp, what)
        location = resp.headers.get("location") or ""
        self._check_session_uri(location, what)
        _hold(location)
        return location

    def _transfer(self, what: str, session: str, file: str, size: int, sha256: str, mime: str, token: str) -> Any:
        if size <= 0:
            raise PublishError("INVALID_ARGUMENT", f"{what}: the file is empty")
        offset, hashed, attempt = 0, hashlib.sha256(), 0      # `hashed` covers bytes [0, offset), as they were sent
        with open(file, "rb") as fh:
            while True:
                n = min(self.chunk_size, size - offset)
                fh.seek(offset)
                buf = fh.read(n)
                last = offset + n == size
                if len(buf) != n or (last and _digest(hashed, buf) != sha256):
                    raise PublishError("CONFIRM_MISMATCH", f"{what}: the file changed after the confirmation token was "
                                       "checked; its final bytes were not sent, so nothing was published. Run the dry run "
                                       "again", {"platform": self.id, "step": what, "path": file, "uploaded": False})
                headers = {"Authorization": "Bearer " + token, "Content-Type": mime, "Content-Length": str(n),
                           "Content-Range": f"bytes {offset}-{offset + n - 1}/{size}"}
                try:
                    resp: Optional[_Response] = self._send("PUT", session, buf, headers)
                    self._stats["bytes_sent"] += n
                except (OSError, http.client.HTTPException):
                    resp = None                                # interrupted: no answer, a reset or a timeout
                if resp is not None and resp.status in (200, 201):
                    if not last:
                        raise self._protocol_error(what, "the upload finished before its last chunk was sent")
                    return resp.json()
                if resp is not None and resp.status == 308:
                    before, offset = offset, self._advance(what, offset, buf, hashed, self._confirmed(resp, what))
                    if offset > before:
                        attempt = 0
                        continue
                    attempt += 1                               # a 308 that kept none of the chunk: retry it, bounded
                    if attempt > self.max_retries:
                        raise self._interrupted(what, offset, size, resp)
                    self._backoff(attempt, resp)
                    continue
                if resp is not None and resp.status == 404:
                    raise _SessionExpired()
                if resp is not None and not self._retriable(resp):
                    raise self._api_error(resp, what)
                attempt += 1
                if attempt > self.max_retries:
                    raise self._interrupted(what, offset, size, resp)
                self._backoff(attempt, resp)
                done, confirmed = self._query_status(what, session, size, token)
                self._stats["resumed"] += 1
                if done is not None:
                    if not last:
                        raise self._protocol_error(what, "the session reports the upload complete before its last chunk")
                    return done
                offset = self._advance(what, offset, buf, hashed, confirmed)

    def _query_status(self, what: str, session: str, size: int, token: str) -> Tuple[Any, int]:
        """(the finished resource, size) or (None, bytes the session holds)."""
        headers = {"Authorization": "Bearer " + token, "Content-Length": "0", "Content-Range": f"bytes */{size}"}
        attempt = 0
        while True:
            try:
                resp: Optional[_Response] = self._send("PUT", session, b"", headers)
            except (OSError, http.client.HTTPException):
                resp = None
            if resp is not None and resp.status in (200, 201):
                return resp.json(), size
            if resp is not None and resp.status == 308:
                return None, self._confirmed(resp, what)
            if resp is not None and resp.status == 404:
                raise _SessionExpired()
            if resp is not None and not self._retriable(resp):
                raise self._api_error(resp, what)
            attempt += 1
            if attempt > self.max_retries:
                raise self._interrupted(what, None, size, resp)
            self._backoff(attempt, resp)

    def _confirmed(self, resp: _Response, what: str) -> int:
        rng = resp.headers.get("range")
        if not rng:
            return 0                                           # no Range header: nothing received yet
        m = _RANGE.match(rng.strip())
        if not m:
            raise self._protocol_error(what, f"unreadable Range header {rng!r}")
        return int(m.group(1)) + 1

    def _advance(self, what: str, offset: int, buf: bytes, hashed: Any, confirmed: int) -> int:
        """Bytes the session confirmed must come from the chunk just sent; they join the running hash."""
        if not offset <= confirmed <= offset + len(buf):
            raise self._protocol_error(what, f"the session holds {confirmed} bytes, outside the chunk just sent "
                                             f"({offset}-{offset + len(buf)})")
        hashed.update(buf[:confirmed - offset])
        return confirmed

    # ---- requests, retries and errors ---------------------------------------------------------------------------
    def _request_retrying(self, method: str, url: str, body: bytes, headers: Dict[str, str], what: str) -> _Response:
        attempt = 0
        while True:
            try:
                resp: Optional[_Response] = self._send(method, url, body, headers)
            except (OSError, http.client.HTTPException):
                resp = None
            if resp is not None and not self._retriable(resp):
                return resp
            attempt += 1
            if attempt > self.max_retries:
                raise self._interrupted(what, None, None, resp)
            self._backoff(attempt, resp)

    def _retriable(self, resp: _Response) -> bool:
        return resp.status in RETRIABLE_STATUS or (resp.status in (400, 403) and bool(
            set(_reasons(resp)).intersection(RATE_LIMIT_REASONS)))

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

    def _check_endpoints(self) -> None:
        for url in (self.token_url, self.api_base):
            parts = urlsplit(url)
            if not (parts.scheme == "https" or (parts.scheme == "http" and parts.hostname in LOOPBACK_HOSTS)):
                raise PublishError("INTERNAL_ERROR", f"refusing endpoint {url}: only https, or http to a loopback test server")

    def _check_session_uri(self, location: str, what: str) -> None:
        """The bearer token goes only to the API's own host: the session URI must be on it, with the same scheme."""
        base, loc = urlsplit(self.api_base), urlsplit(location)
        if not location or (loc.scheme, loc.hostname, loc.port) != (base.scheme, base.hostname, base.port):
            raise self._protocol_error(what, "the session URI is missing or not on the API's host")

    def _interrupted(self, what: str, offset: Optional[int], size: Optional[int], resp: Optional[_Response]) -> PublishError:
        last = {"status": resp.status, "reasons": _reasons(resp)} if resp is not None else {"status": None,
                                                                                            "reasons": ["no answer"]}
        return PublishError("UPLOAD_INTERRUPTED", f"{what}: still failing after {self.max_retries} retries "
                            f"(last: {last['status'] or 'no answer'}); nothing was published. Run the same upload again later",
                            {"platform": self.id, "step": what, "last": last, "bytes_confirmed": offset, "size": size,
                             "uploaded": False, "transfer": dict(self._stats)})

    def _protocol_error(self, what: str, why: str) -> PublishError:
        return PublishError("UPLOAD_REJECTED", f"{what}: {why}; nothing was published",
                            {"platform": self.id, "step": what, "uploaded": False})

    def _api_error(self, resp: _Response, what: str) -> PublishError:
        reasons = _reasons(resp)
        doc = resp.json()
        message = ""
        if isinstance(doc, dict) and isinstance(doc.get("error"), dict):
            message = str(doc["error"].get("message") or "")[:300]
        details = {"platform": self.id, "step": what, "status": resp.status, "reasons": reasons, "message": message,
                   "uploaded": False}
        if set(reasons).intersection(QUOTA_REASONS):
            return PublishError("QUOTA_EXCEEDED", f"{what}: the API project's quota is used up (quotaExceeded); nothing "
                                "was uploaded. Try again after the quota resets", details)
        if "uploadLimitExceeded" in reasons:
            return PublishError("UPLOAD_LIMIT_EXCEEDED", f"{what}: the channel's daily upload limit is reached "
                                "(uploadLimitExceeded); nothing was uploaded. Try again later", details)
        if resp.status == 401 or set(reasons).intersection(AUTH_REASONS):
            return PublishError("AUTH_FAILED", f"{what}: YouTube refused the access ({resp.status}"
                                + (f", {', '.join(reasons)}" if reasons else "") + "); nothing was uploaded", details)
        return PublishError("UPLOAD_REJECTED", f"{what}: YouTube refused the request ({resp.status}"
                            + (f", {', '.join(reasons)}" if reasons else "") + "); nothing was uploaded", details)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _rand() -> float:
    return random.random()


def _digest(hashed: Any, tail: bytes) -> str:
    h = hashed.copy()
    h.update(tail)
    return h.hexdigest()


def _reasons(resp: Optional[_Response]) -> List[str]:
    """The `reason` of each item of a Google API error body: {"error": {"code", "message", "errors": [{"reason"}]}}."""
    doc = resp.json() if resp is not None else None
    err = doc.get("error") if isinstance(doc, dict) else None
    items = err.get("errors") if isinstance(err, dict) else None
    return [str(i["reason"]) for i in items or [] if isinstance(i, dict) and i.get("reason")]
