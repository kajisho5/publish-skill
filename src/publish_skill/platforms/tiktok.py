"""TikTok uploader (roadmap step 5, ADR-006): the Content Posting API's Direct Post of the video the confirmation token
bound, from a local file (source FILE_UPLOAD), with its title and privacy, authorised by a user access token.

This module, platforms/vimeo.py and platforms/youtube.py are the only modules in the package with network code
(ADR-001's source rule exempts exactly these three files). It runs only from upload.py's gate, after the exact --confirm
token matched and the credentials were read, so every refusal has already happened when the first byte leaves the
machine. Standard library only: http.client, as in the other two uploaders.

Protocol (sources in docs/decisions.md ADR-006, read 2026-10-31):
  1. Query the creator: POST https://open.tiktokapis.com/v2/post/publish/creator_info/query/ with Authorization: Bearer
     <token> and Content-Type: application/json; charset=UTF-8. The answer's privacy_level_options must offer the bound
     privacy level ("The provided value must match one of the privacy_level_options").
  2. Initialise the post: POST /v2/post/publish/video/init/ with {"post_info": {"title", "privacy_level"},
     "source_info": {"source": "FILE_UPLOAD", "video_size", "chunk_size", "total_chunk_count"}}; the answer carries
     data.publish_id and data.upload_url.
  3. PUT the file to upload_url in sequential chunks with Content-Type (video/mp4, video/quicktime or video/webm),
     Content-Range: bytes FIRST-LAST/TOTAL; 206 after each chunk, 201 after the last ("All parts are uploaded"). A chunk
     is at least 5 MB and at most 64 MB; the last may be up to 128 MB; total_chunk_count is video_size // chunk_size; a
     file under 5 MB is sent whole.
  4. Ask how the post is going: POST /v2/post/publish/status/fetch/ {"publish_id"} until PUBLISH_COMPLETE or FAILED, a
     bounded number of times.

After an interrupted chunk (no answer, a timeout, 5xx or 429) the same chunk is sent again (the guide: "5xx: Retry the
chunk"); a 416 ("Content-Range does not reflect the actual upload progress") is resolved by asking the post's status for
uploaded_bytes and resuming from there. 403/404 from the upload URL means it expired or is unknown.

The access token goes to open.tiktokapis.com only; the PUT requests carry no Authorization header. The upload URL is a
capability URL: it is recorded with credentials.hold, so the redactor removes it from every output, and it must be https
on a tiktokapis.com host.

Exactly what the token bound is sent: post_info is the bound title (when set) and the privacy level (private ->
SELF_ONLY, public -> PUBLIC_TO_EVERYONE; always sent, so the default is SELF_ONLY); a description, tags, a thumbnail or
captions are refused by `plan` and `upload` before a token is printed (metadata_fields), and so is `unlisted`, which
TikTok has no equivalent for. The file is hashed as its chunks are acknowledged, and the final chunk is held back
unless the bytes so far plus that chunk have the sha256 the token bound (CONFIRM_MISMATCH)."""
from __future__ import annotations

import hashlib
import http.client
import json
import os
import random
import ssl
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlsplit

from ..errors import PublishError
from .base import Uploader, UploadRequest

API_BASE = "https://open.tiktokapis.com"
CREATOR_INFO_PATH = "/v2/post/publish/creator_info/query/"
INIT_PATH = "/v2/post/publish/video/init/"
STATUS_PATH = "/v2/post/publish/status/fetch/"
JSON_TYPE = "application/json; charset=UTF-8"
# Chunk sizes. "Each chunk must be at least 5 MB but no greater than 64 MB", the final chunk "up to 128 MB". The
# default is 64 000 000 bytes, at most 64 MB under either reading of MB; the last chunk is then below 128 000 000 bytes.
DEFAULT_CHUNK = 64 * 1000 * 1000
MIN_CHUNK = 5 * 1024 * 1024                    # at least 5 MB under either reading
MAX_CHUNKS = 1000
MAX_BYTES = 4 * 1000 ** 3                      # "Maximum of 4GB", the smaller (decimal) reading
CONTENT_TYPES = {".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm"}
PRIVACY_LEVEL = {"private": "SELF_ONLY", "public": "PUBLIC_TO_EVERYONE"}
RETRIABLE_STATUS = (429, 500, 502, 503, 504)
GONE_STATUS = (403, 404)                       # "The upload_url has expired" / "cannot find a valid upload task"
AUTH_CODES = ("access_token_invalid", "scope_not_authorized")
LIMIT_CODES = {"spam_risk_too_many_posts": "UPLOAD_LIMIT_EXCEEDED", "reached_active_user_cap": "QUOTA_EXCEEDED"}
UPLOAD_HOST = "tiktokapis.com"
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")      # plain http only to these: the tests' mock server
DONE_STATUSES = ("PUBLISH_COMPLETE", "SEND_TO_USER_INBOX")


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


def chunk_plan(size: int, chunk_size: int) -> Dict[str, int]:
    """source_info's chunk_size and total_chunk_count for a file: whole when it fits in one chunk, otherwise
    `chunk_size` bytes per chunk with the trailing bytes added to the last one (total = size // chunk_size)."""
    if size <= 0 or chunk_size <= 0:
        raise PublishError("INVALID_ARGUMENT", "the file is empty; nothing was uploaded")
    if size <= chunk_size:
        return {"chunk_size": size, "total_chunk_count": 1}
    return {"chunk_size": chunk_size, "total_chunk_count": size // chunk_size}


def chunk_ranges(size: int, plan: Dict[str, int]) -> List[range]:
    """The byte ranges [first, last + 1) of each chunk, in order: the last one takes the trailing bytes."""
    n, c = plan["total_chunk_count"], plan["chunk_size"]
    return [range(i * c, size if i == n - 1 else (i + 1) * c) for i in range(n)]


def init_body(metadata: Dict[str, Any], size: int, plan: Dict[str, int]) -> Dict[str, Any]:
    """The init body for the bound metadata: exactly the bound title (when set) and the privacy level, then the
    FILE_UPLOAD source. Nothing else is added (no interaction switches, cover frame or labels)."""
    privacy = metadata["privacy"]
    if privacy not in PRIVACY_LEVEL:
        raise PublishError("INVALID_ARGUMENT", f"privacy {privacy!r} is not one of {', '.join(PRIVACY_LEVEL)} on TikTok")
    for field in ("description", "tags", "thumbnail", "captions"):
        if metadata.get(field):
            raise PublishError("INVALID_ARGUMENT", f"the TikTok uploader does not send {field}; nothing was uploaded")
    post: Dict[str, Any] = {}
    if metadata.get("title") is not None:
        post["title"] = metadata["title"]
    post["privacy_level"] = PRIVACY_LEVEL[privacy]
    return {"post_info": post, "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                                               "chunk_size": plan["chunk_size"],
                                               "total_chunk_count": plan["total_chunk_count"]}}


class TikTokUploader(Uploader):
    id = "tiktok"
    name = "TikTok"
    roadmap_step = 5
    implemented = True
    # a user access token with the video.publish scope, from the environment or the OS keychain (ADR-003, ADR-006)
    credential_env = ("PUBLISH_TIKTOK_ACCESS_TOKEN",)
    docs_url = "https://developers.tiktok.com/doc/content-posting-api-reference-direct-post"
    ffmpeg_skill_platform = "tiktok"                   # ffmpeg-skill scripts/_platforms.py owns the delivery spec
    metadata_fields = ("title", "privacy")

    # Endpoints and tuning. Only code changes these (the tests point them at a loopback mock server); no flag,
    # environment variable or file does.
    api_base = API_BASE
    chunk_size = DEFAULT_CHUNK
    timeout_s = 60.0
    max_retries = 10
    max_backoff_s = 64.0
    max_retry_after_s = 300.0
    status_polls = 20
    status_interval_s = 6.0                            # 10 queries a minute, under the endpoint's 30

    def __init__(self, sleep: Optional[Callable[[float], None]] = None, rand: Optional[Callable[[], float]] = None) -> None:
        self._sleep_fn = sleep
        self._rand_fn = rand
        self._stats: Dict[str, Any] = {}

    def spec(self) -> Dict[str, Any]:
        return dict(super().spec(), protocol="TikTok Content Posting API, Direct Post, FILE_UPLOAD in chunks; user "
                                             "access token (video.publish)",
                    endpoints=[API_BASE + CREATOR_INFO_PATH, API_BASE + INIT_PATH, API_BASE + STATUS_PATH,
                               "data.upload_url (https, *.tiktokapis.com)"])

    # ---- the upload ---------------------------------------------------------------------------------------------
    def upload(self, request: UploadRequest, credentials: Any) -> Dict[str, Any]:
        self._check_endpoint()
        if self.chunk_size <= 0:
            raise PublishError("INTERNAL_ERROR", "chunk size must be positive")
        if request.size <= 0:
            raise PublishError("INVALID_ARGUMENT", "the file is empty; nothing was uploaded")
        content_type = CONTENT_TYPES.get(os.path.splitext(request.path)[1].lower())
        if content_type is None:
            raise PublishError("INVALID_ARGUMENT", "TikTok takes MP4, MOV or WebM files (.mp4, .mov, .webm); nothing was "
                               "uploaded", {"platform": self.id, "path": request.path, "uploaded": False})
        plan = chunk_plan(request.size, self.chunk_size)
        if plan["total_chunk_count"] > MAX_CHUNKS:
            raise PublishError("INVALID_ARGUMENT", f"the file needs more than {MAX_CHUNKS} chunks; nothing was uploaded",
                               {"platform": self.id, "uploaded": False})
        self._stats = {"requests": 0, "retries": 0, "resumed": 0, "waited_s": 0.0, "bytes_sent": 0, "status_polls": 0}
        md = request.metadata
        body = init_body(md, request.size, plan)
        token = credentials.get("PUBLISH_TIKTOK_ACCESS_TOKEN")
        auth = {"Authorization": "Bearer " + token, "Content-Type": JSON_TYPE}
        creator = self._creator_info(auth, body["post_info"]["privacy_level"])
        publish_id, upload_url = self._init(auth, body)
        try:
            self._transfer(auth, publish_id, upload_url, request.path, request.size, request.sha256, plan, content_type)
        except PublishError as e:
            details = dict(e.details, publish_id=publish_id)
            raise PublishError(e.code, e.message + " (nothing was posted)", details, e.retryable) from None
        status = self._poll(auth, publish_id)
        result: Dict[str, Any] = {"video_id": publish_id, "publish_id": publish_id,
                                  "url": None, "privacy": md["privacy"], "privacy_level": body["post_info"]["privacy_level"],
                                  "sent": body, "creator": creator, "status": status["status"],
                                  "post_ids": status.get("post_ids") or [], "thumbnail": None, "captions": [],
                                  "warnings": list(status.get("warnings") or [])}
        if creator.get("username"):                    # the post's page once it is public, else the profile
            result["url"] = f"https://www.tiktok.com/@{creator['username']}" + (
                f"/video/{result['post_ids'][0]}" if result["post_ids"] else "")
        if result["status"] != "PUBLISH_COMPLETE":
            result["warnings"].append(f"TikTok reports {result['status']}: the video is uploaded and TikTok is still "
                                      "processing the post; check it in the TikTok app (do not run upload again)")
        result["transfer"] = dict(self._stats)
        result["processing"] = ("TikTok moderates a post before it is public; a public post id is reported only after "
                                "that, and a post from an unaudited app is private whatever was sent")
        return result

    # ---- step 1: the creator ------------------------------------------------------------------------------------
    def _creator_info(self, auth: Dict[str, str], level: str) -> Dict[str, Any]:
        resp = self._api("creator_info", self.api_base + CREATOR_INFO_PATH, b"", auth, retry_without_answer=True)
        data = resp.json().get("data") if isinstance(resp.json(), dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("privacy_level_options"), list):
            raise self._protocol_error("creator_info", "the answer has no data.privacy_level_options")
        options = [str(o) for o in data["privacy_level_options"]]
        creator = {"username": data.get("creator_username"), "nickname": data.get("creator_nickname"),
                   "privacy_level_options": options, "max_video_post_duration_sec": data.get("max_video_post_duration_sec")}
        if level not in options:
            raise PublishError("UPLOAD_REJECTED", f"the creator's account does not offer privacy level {level} (it offers "
                               f"{', '.join(options) or 'none'}); nothing was uploaded. Choose another --privacy and run "
                               "the dry run again", {"platform": self.id, "step": "creator_info",
                                                     "privacy_level_options": options, "uploaded": False})
        return creator

    # ---- step 2: initialise the post ----------------------------------------------------------------------------
    def _init(self, auth: Dict[str, str], body: Dict[str, Any]) -> tuple:
        data_bytes = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        resp = self._api("init", self.api_base + INIT_PATH, data_bytes, auth, retry_without_answer=False)
        doc = resp.json()
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, dict):
            raise self._protocol_error("init", "the answer has no data")
        publish_id, url = data.get("publish_id"), data.get("upload_url")
        if not isinstance(publish_id, str) or not publish_id:
            raise self._protocol_error("init", "the answer has no data.publish_id")
        if not isinstance(url, str) or not url:
            raise self._protocol_error("init", "the answer has no data.upload_url")
        self._check_upload_url(url)
        _hold(url)
        return publish_id, url

    # ---- step 3: the chunks -------------------------------------------------------------------------------------
    def _transfer(self, auth: Dict[str, str], publish_id: str, url: str, file: str, size: int, sha256: str,
                  plan: Dict[str, int], content_type: str) -> None:
        ranges = chunk_ranges(size, plan)
        hashed, i, attempt = hashlib.sha256(), 0, 0           # `hashed` covers every acknowledged chunk
        with open(file, "rb") as fh:
            while i < len(ranges):
                r = ranges[i]
                last = i == len(ranges) - 1
                fh.seek(r.start)
                buf = fh.read(len(r))
                if len(buf) != len(r) or (last and _digest(hashed, buf) != sha256):
                    raise PublishError("CONFIRM_MISMATCH", "the file changed after the confirmation token was checked; "
                                       "its final bytes were not sent, so the upload was not completed. Run the dry run "
                                       "again", {"platform": self.id, "step": "put", "path": file, "uploaded": False})
                headers = {"Content-Type": content_type,
                           "Content-Range": f"bytes {r.start}-{r.stop - 1}/{size}"}
                try:
                    resp: Optional[_Response] = self._send("PUT", url, buf, headers)
                    self._stats["bytes_sent"] += len(buf)
                except (OSError, http.client.HTTPException):
                    resp = None                                # interrupted: no answer, a reset or a timeout
                if resp is not None and resp.status in (201, 206):
                    if (resp.status == 201) != last:
                        raise self._protocol_error("put", f"{resp.status} after chunk {i + 1} of {len(ranges)}")
                    hashed.update(buf)
                    i, attempt = i + 1, 0
                    continue
                if resp is not None and resp.status in GONE_STATUS:
                    raise PublishError("UPLOAD_INTERRUPTED", f"the upload URL is gone ({resp.status}: it expired or "
                                       "TikTok lost the upload task); the video was not completed. Run the same upload "
                                       "again (it starts a new post)",
                                       {"platform": self.id, "step": "put", "status": resp.status, "chunk": i + 1,
                                        "chunks": len(ranges), "uploaded": False, "transfer": dict(self._stats)})
                if resp is not None and resp.status == 416:
                    # The server holds a different amount than this chunk assumes: ask the post how far it got.
                    held = self._uploaded_bytes(auth, publish_id)
                    self._stats["resumed"] += 1
                    if held == r.stop:                         # this chunk did arrive (its answer was lost earlier)
                        if last:
                            return
                        hashed.update(buf)
                        i, attempt = i + 1, 0
                        continue
                    if held != r.start:
                        raise self._protocol_error("put", f"TikTok holds {held} bytes, neither the start ({r.start}) nor "
                                                          f"the end ({r.stop}) of the chunk just sent")
                    attempt += 1
                    if attempt > self.max_retries:
                        raise self._interrupted("put", resp, i + 1, len(ranges))
                    self._backoff(attempt, resp)
                    continue
                if resp is not None and resp.status not in RETRIABLE_STATUS:
                    raise self._api_error(resp, "put")
                attempt += 1                                   # interrupted, 5xx or 429: the same chunk again
                if attempt > self.max_retries:
                    raise self._interrupted("put", resp, i + 1, len(ranges))
                self._backoff(attempt, resp)
                self._stats["resumed"] += 1

    def _uploaded_bytes(self, auth: Dict[str, str], publish_id: str) -> int:
        data = self._status(auth, publish_id)
        held = data.get("uploaded_bytes")
        if not isinstance(held, int) or held < 0:
            raise self._protocol_error("status", "the answer has no data.uploaded_bytes")
        return held

    # ---- step 4: the post's status ------------------------------------------------------------------------------
    def _status(self, auth: Dict[str, str], publish_id: str) -> Dict[str, Any]:
        body = json.dumps({"publish_id": publish_id}, separators=(",", ":")).encode("utf-8")
        self._stats["status_polls"] += 1
        resp = self._api("status", self.api_base + STATUS_PATH, body, auth, retry_without_answer=True)
        doc = resp.json()
        data = doc.get("data") if isinstance(doc, dict) else None
        if not isinstance(data, dict):
            raise self._protocol_error("status", "the answer has no data")
        return data

    def _poll(self, auth: Dict[str, str], publish_id: str) -> Dict[str, Any]:
        """After the last chunk: ask how the post is going, at most `status_polls` times. A FAILED post is an error;
        a post still processing when the polls run out, or a status query that fails, is reported in the receipt (the
        video is uploaded and TikTok goes on posting it)."""
        status = "PROCESSING_UPLOAD"
        for n in range(self.status_polls):
            if n:
                self._wait(self.status_interval_s)
            try:
                data = self._status(auth, publish_id)
            except PublishError as e:
                return {"status": "UNKNOWN", "warnings": [f"the post's status could not be read ({e.code}); the video "
                                                          "is uploaded: check the post in the TikTok app"]}
            status = str(data.get("status") or "UNKNOWN")
            if status == "FAILED":
                raise self._failed(publish_id, str(data.get("fail_reason") or ""))
            if status in DONE_STATUSES:
                ids = data.get("publicaly_available_post_id")          # the API's own spelling
                return {"status": status, "post_ids": [str(x) for x in ids] if isinstance(ids, list) else []}
        return {"status": status}

    def _failed(self, publish_id: str, reason: str) -> PublishError:
        details = {"platform": self.id, "step": "status", "publish_id": publish_id, "fail_reason": reason[:300],
                   "uploaded": False}
        if reason == "spam_risk_too_many_posts":
            return PublishError("UPLOAD_LIMIT_EXCEEDED", "TikTok refused the post: the creator's daily post limit is "
                                "reached (spam_risk_too_many_posts); nothing was posted. Try again tomorrow", details)
        if reason == "internal":
            return PublishError("UPLOAD_INTERRUPTED", "TikTok failed the post with a temporary server error (internal); "
                                "nothing was posted. Run the same upload again later", details)
        return PublishError("UPLOAD_REJECTED", f"TikTok failed the post ({reason or 'no reason given'}); nothing was "
                            "posted" + (". Re-export it to the delivery spec: ffmpeg-skill export --preset tiktok"
                                        if reason.endswith("_check_failed") else ""), details)

    # ---- requests, retries and errors ---------------------------------------------------------------------------
    def _api(self, what: str, url: str, body: bytes, headers: Dict[str, str], retry_without_answer: bool) -> _Response:
        """One JSON API call with its retries: 429/5xx answers always; no answer only when `retry_without_answer`
        (a read; the init request is not repeated without an answer, as a second init would start a second post). An
        answer whose error.code is not "ok" is an error, whatever its HTTP status (creator_info answers 200 for some)."""
        attempt = 0
        while True:
            try:
                resp: Optional[_Response] = self._send("POST", url, body, headers)
            except (OSError, http.client.HTTPException):
                resp = None
                if not retry_without_answer:
                    raise PublishError("UPLOAD_INTERRUPTED", f"{what} got no answer; nothing was uploaded. Run the same "
                                       "upload again later", {"platform": self.id, "step": what, "uploaded": False,
                                                              "transfer": dict(self._stats)}) from None
            if resp is not None and resp.status == 200 and _error_code(resp) == "ok":
                return resp
            if resp is not None and resp.status not in RETRIABLE_STATUS:
                raise self._api_error(resp, what)
            attempt += 1
            if attempt > self.max_retries:
                raise self._interrupted(what, resp, None, None)
            self._backoff(attempt, resp)

    def _backoff(self, attempt: int, resp: Optional[_Response]) -> None:
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

    def _check_upload_url(self, url: str) -> None:
        """The file goes only to TikTok: an https upload URL on a tiktokapis.com host. Only a loopback API base (the
        tests' mock) may hand out an upload URL on its own scheme, host and port."""
        base, loc = urlsplit(self.api_base), urlsplit(url)
        host = (loc.hostname or "").lower()
        on_tiktok = loc.scheme == "https" and (host == UPLOAD_HOST or host.endswith("." + UPLOAD_HOST))
        on_test_mock = base.hostname in LOOPBACK_HOSTS and (loc.scheme, loc.hostname, loc.port) == (
            base.scheme, base.hostname, base.port)
        if not (on_tiktok or on_test_mock):
            raise self._protocol_error("init", "the upload URL is not https on a tiktokapis.com host")

    def _interrupted(self, what: str, resp: Optional[_Response], chunk: Optional[int],
                     chunks: Optional[int]) -> PublishError:
        last = ({"status": resp.status, "error_code": _error_code(resp)} if resp is not None
                else {"status": None, "error_code": None, "reason": "no answer"})
        return PublishError("UPLOAD_INTERRUPTED", f"{what}: still failing after {self.max_retries} retries "
                            f"(last: {last['status'] or 'no answer'}); the video was not completed. Run the same upload "
                            "again later", {"platform": self.id, "step": what, "last": last, "chunk": chunk,
                                            "chunks": chunks, "uploaded": False, "transfer": dict(self._stats)})

    def _protocol_error(self, what: str, why: str) -> PublishError:
        return PublishError("UPLOAD_REJECTED", f"{what}: {why}; the video was not completed",
                            {"platform": self.id, "step": what, "uploaded": False})

    def _api_error(self, resp: _Response, what: str) -> PublishError:
        code = _error_code(resp)
        doc = resp.json()
        err = doc.get("error") if isinstance(doc, dict) and isinstance(doc.get("error"), dict) else {}
        message = str(err.get("message") or "")[:300]
        details = {"platform": self.id, "step": what, "status": resp.status, "error_code": code, "message": message,
                   "uploaded": False}
        if code in LIMIT_CODES:
            which = ("the creator's daily post limit" if code == "spam_risk_too_many_posts"
                     else "the app's daily quota of publishing users")
            return PublishError(LIMIT_CODES[code], f"{what}: {which} is reached ({code}); nothing was uploaded. Try again "
                                "after it resets", details)
        if code in AUTH_CODES or resp.status == 401:
            return PublishError("AUTH_FAILED", f"{what}: TikTok refused the access token ({resp.status}"
                                + (f", {code}" if code else "") + "); store a new user access token with the "
                                "video.publish scope with `publish auth login --platform tiktok` (values are never "
                                "printed). Nothing was uploaded", details)
        return PublishError("UPLOAD_REJECTED", f"{what}: TikTok refused the request ({resp.status}"
                            + (f", {code}" if code else "") + "); nothing was uploaded", details)


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _rand() -> float:
    return random.random()


def _digest(hashed: Any, tail: bytes) -> str:
    h = hashed.copy()
    h.update(tail)
    return h.hexdigest()


def _error_code(resp: Optional[_Response]) -> Optional[str]:
    """error.code of a TikTok answer ({"data": ..., "error": {"code", "message", "log_id"}}), when there is one."""
    doc = resp.json() if resp is not None else None
    err = doc.get("error") if isinstance(doc, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    return str(code) if isinstance(code, str) and code else None
