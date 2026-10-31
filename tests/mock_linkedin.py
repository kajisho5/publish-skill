"""An in-process mock of the LinkedIn Videos API multipart upload and the Posts API, bound to 127.0.0.1 on a free port
(roadmap step 5, ADR-008). Nothing here is part of the package, and nothing here contacts LinkedIn:
helpers.OfflineTestCase lets a test connect only to the loopback ports of mock servers it started.

What it implements, as LinkedIn's pages describe it (every API request needs Authorization: Bearer <token>, else 401,
and Linkedin-Version and X-Restli-Protocol-Version: 2.0.0, else 400; the signed upload URLs need neither):
  POST /rest/videos?action=initializeUpload   {"initializeUploadRequest": {"owner", "fileSizeBytes", "uploadCaptions",
                                              "uploadThumbnail"}} -> 200 {"value": {"video", "uploadToken",
                                              "uploadUrlsExpireAt", "uploadInstructions": [{"firstByte", "lastByte",
                                              "uploadUrl"}, ...]}}, parts of `part_size` bytes
  PUT /dms-uploads/{video}/part/{n}?sig=...   the part's bytes (exactly its range, else 400) -> 200 with an ETag; a
                                              resend replaces the part and gets a new ETag
  POST /rest/videos?action=finalizeUpload     {"finalizeUploadRequest": {"video", "uploadToken", "uploadedPartIds"}}:
                                              the latest ETag of every part, in order (else 400) -> 200, no body
  GET /rest/videos/{urn}                      -> 200 {"id", "owner", "status"}: WAITING_UPLOAD before finalize, then the
                                              next entry of `processing` (the last one repeats; AVAILABLE when empty)
  POST /rest/posts                            the video AVAILABLE and owned by the author (else 400) -> 201, no body,
                                              x-restli-id: urn:li:share:<id>

Faults are scripted per request kind ("init", "put", "finalize", "status", "post"): `faults[kind]` is a list consumed
one entry per request, each one of
  None                                   answer normally
  ("status", code, message, headers)     answer with that HTTP status and an error body (message may be None)
  ("drop",)                              (put) read the part, keep none of it, drop the connection
  ("lost",)                              (put, post) do the work, then drop the connection (the answer is lost)
  ("stall", seconds)                     wait, then drop the connection without answering (the client times out)
  ("call", fn)                           run fn() first (e.g. edit the file mid-upload), then answer normally
  ("noetag",)                            (put) keep the part but answer without an ETag"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Set
from urllib.parse import unquote, urlsplit

_PUT = re.compile(r"^/dms-uploads/([A-Za-z0-9_-]+)/part/([0-9]+)$")
_VIDEO = re.compile(r"^/rest/videos/(.+)$")


def error(status: int, message: Optional[str] = None, code: str = "ERROR") -> bytes:
    body: Dict[str, Any] = {"status": status, "code": code}
    if message:
        body["message"] = message
    return json.dumps(body).encode()


class MockLinkedIn:
    def __init__(self, access_token: str, author: str, part_size: int = 256 * 1024,
                 processing: Optional[List[str]] = None, failure_reason: str = "") -> None:
        self.token = access_token
        self.author = author
        self.part_size = part_size
        self.processing = processing or []
        self.failure_reason = failure_reason
        self.faults: Dict[str, List[Any]] = {}
        self.videos: Dict[str, Dict[str, Any]] = {}  # video id -> {"owner", "total", "parts", "etags", "data", "state"}
        self.requests: List[Dict[str, Any]] = []      # every request: kind, method, path, query, headers, length, part
        self.posts: List[Dict[str, Any]] = []         # every post made: {"id", "body", "data"}
        self.inits: List[Dict[str, Any]] = []
        self.finalized: List[Dict[str, Any]] = []
        self.upload_urls: List[str] = []
        self.lock = threading.Lock()
        self.server: Optional[ThreadingHTTPServer] = None

    # ---- lifecycle -------------------------------------------------------------------------------------------
    def start(self, allowed_ports: Set[int]) -> "MockLinkedIn":
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def do_POST(self) -> None:
                mock._handle(self, "POST")

            def do_PUT(self) -> None:
                mock._handle(self, "PUT")

            def do_GET(self) -> None:
                mock._handle(self, "GET")

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, *_a: Any) -> None:
                pass

        self.server = Server(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._allowed = allowed_ports
        allowed_ports.add(self.port)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self.server is not None:
            self._allowed.discard(self.port)
            self.server.shutdown()
            self.server.server_close()
            self.server = None

    # ---- helpers for tests -----------------------------------------------------------------------------------
    def kinds(self) -> List[str]:
        return [r["kind"] for r in self.requests]

    def part_indexes(self) -> List[Optional[int]]:
        return [r.get("part") for r in self.requests if r["kind"] == "put"]

    def _fault(self, kind: str) -> Any:
        with self.lock:
            queue = self.faults.get(kind)
            return queue.pop(0) if queue else None

    # ---- the protocol ----------------------------------------------------------------------------------------
    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        parts = urlsplit(h.path)
        headers = {k.lower(): v for k, v in h.headers.items()}
        length = int(headers.get("content-length") or 0)
        put, vid = _PUT.match(parts.path), _VIDEO.match(parts.path)
        if method == "POST" and parts.path == "/rest/videos" and parts.query == "action=initializeUpload":
            kind = "init"
        elif method == "PUT" and put:
            kind = "put"
        elif method == "POST" and parts.path == "/rest/videos" and parts.query == "action=finalizeUpload":
            kind = "finalize"
        elif method == "GET" and vid:
            kind = "status"
        elif method == "POST" and parts.path == "/rest/posts":
            kind = "post"
        else:
            kind = "unknown"
        record: Dict[str, Any] = {"kind": kind, "method": method, "path": parts.path, "query": parts.query,
                                  "headers": headers, "length": length}
        if put:
            record["part"] = int(put.group(2))
        with self.lock:
            self.requests.append(record)
        raw = h.rfile.read(length) if length else b""
        fault = self._fault(kind)
        if fault is not None:
            if fault[0] == "call":
                fault[1]()
                fault = None
            elif fault[0] == "stall":
                time.sleep(fault[1])
                return self._drop(h)
            elif fault[0] == "status":
                return self._send(h, fault[1], error(fault[1], fault[2]), fault[3] if len(fault) > 3 else {})
        if kind == "unknown":
            return self._send(h, 404, error(404, "not found", "NOT_FOUND"))
        if kind == "put":
            return self._put(h, put.group(1), int(put.group(2)), parts.query, raw, fault)
        if headers.get("authorization") != "Bearer " + self.token:
            return self._send(h, 401, error(401, "Invalid access token", "INVALID_ACCESS_TOKEN"))
        if not re.match(r"^[0-9]{6}$", headers.get("linkedin-version", "")) \
                or headers.get("x-restli-protocol-version") != "2.0.0":
            return self._send(h, 400, error(400, "missing version headers"))
        if kind == "init":
            return self._init(h, headers, raw)
        if kind == "finalize":
            return self._finalize(h, headers, raw)
        if kind == "status":
            return self._status(h, unquote(vid.group(1)))
        return self._post(h, headers, raw, fault)

    def _json(self, headers: Dict[str, str], raw: bytes) -> Optional[Dict[str, Any]]:
        if headers.get("content-type") != "application/json":
            return None
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

    def _init(self, h: BaseHTTPRequestHandler, headers: Dict[str, str], raw: bytes) -> None:
        body = self._json(headers, raw)
        req = (body or {}).get("initializeUploadRequest")
        if not isinstance(req, dict) or req.get("owner") != self.author or not isinstance(req.get("fileSizeBytes"), int) \
                or req["fileSizeBytes"] <= 0:
            return self._send(h, 400, error(400, "invalid initializeUploadRequest"))
        total = req["fileSizeBytes"]
        with self.lock:
            vid = f"C5505AQH{len(self.inits):04d}mock"
            self.inits.append(body)
            instructions = []
            for n, first in enumerate(range(0, total, self.part_size)):
                url = f"{self.url}/dms-uploads/{vid}/part/{n}?sig=signed-{vid}-{n}-secret"
                self.upload_urls.append(url)
                instructions.append({"uploadUrl": url, "firstByte": first,
                                     "lastByte": min(first + self.part_size, total) - 1})
            self.videos[vid] = {"owner": req["owner"], "total": total, "ranges": instructions, "parts": {}, "etags": {},
                                "data": None, "state": "WAITING_UPLOAD", "polls": 0, "puts": 0}
        return self._send(h, 200, json.dumps({"value": {"uploadUrlsExpireAt": 1793000000000,
                                                        "video": "urn:li:video:" + vid,
                                                        "uploadInstructions": instructions,
                                                        "uploadToken": ""}}).encode())

    def _put(self, h: BaseHTTPRequestHandler, vid: str, n: int, query: str, raw: bytes, fault: Any) -> None:
        with self.lock:
            v = self.videos.get(vid)
        if v is None or n >= len(v["ranges"]) or query != f"sig=signed-{vid}-{n}-secret":
            return self._send(h, 401, error(401, "upload URL is invalid or expired", "EXPIRED_UPLOAD_URL"))
        r = v["ranges"][n]
        if len(raw) != r["lastByte"] - r["firstByte"] + 1:
            return self._send(h, 400, error(400, "part length does not match its byte range"))
        if fault is not None and fault[0] == "drop":
            return self._drop(h)
        with self.lock:
            v["puts"] += 1
            etag = f"/ambry-video/signedId/{vid}-{n}-{v['puts']}.bin"
            v["parts"][n] = raw
            v["etags"][n] = etag
        if fault is not None and fault[0] == "lost":
            return self._drop(h)
        if fault is not None and fault[0] == "noetag":
            return self._send(h, 200, b"")
        return self._send(h, 200, b"", {"ETag": f'"{etag}"'})

    def _finalize(self, h: BaseHTTPRequestHandler, headers: Dict[str, str], raw: bytes) -> None:
        body = self._json(headers, raw)
        req = (body or {}).get("finalizeUploadRequest")
        if not isinstance(req, dict) or not str(req.get("video", "")).startswith("urn:li:video:"):
            return self._send(h, 400, error(400, "invalid finalizeUploadRequest"))
        with self.lock:
            v = self.videos.get(req["video"][len("urn:li:video:"):])
            if v is None or req.get("uploadToken") != "":
                return self._send(h, 400, error(400, "INVALID_VIDEO_ID"))
            want = [v["etags"].get(n) for n in range(len(v["ranges"]))]
            if req.get("uploadedPartIds") != want:
                return self._send(h, 400, error(400, "uploadedPartIds do not match the uploaded parts"))
            v["data"] = b"".join(v["parts"][n] for n in range(len(v["ranges"])))
            v["state"] = "PROCESSING" if self.processing else "AVAILABLE"
            self.finalized.append(body)
        return self._send(h, 200, b"")

    def _status(self, h: BaseHTTPRequestHandler, urn: str) -> None:
        with self.lock:
            v = self.videos.get(urn[len("urn:li:video:"):]) if urn.startswith("urn:li:video:") else None
            if v is None:
                return self._send(h, 404, error(404, "Could not find entity", "NOT_FOUND"))
            if v["data"] is not None and self.processing:
                v["state"] = self.processing[min(v["polls"], len(self.processing) - 1)]
                v["polls"] += 1
            out: Dict[str, Any] = {"id": urn, "owner": v["owner"], "status": v["state"]}
            if v["state"] == "PROCESSING_FAILED" and self.failure_reason:
                out["processingFailureReason"] = self.failure_reason
        return self._send(h, 200, json.dumps(out).encode())

    def _post(self, h: BaseHTTPRequestHandler, headers: Dict[str, str], raw: bytes, fault: Any) -> None:
        body = self._json(headers, raw)
        media = ((body or {}).get("content") or {}).get("media") or {}
        with self.lock:
            urn = str(media.get("id", ""))
            v = self.videos.get(urn[len("urn:li:video:"):]) if urn.startswith("urn:li:video:") else None
            if body is None or v is None or v["state"] != "AVAILABLE" or body.get("author") != v["owner"] \
                    or body.get("visibility") not in ("PUBLIC", "CONNECTIONS", "LOGGED_IN") \
                    or body.get("lifecycleState") != "PUBLISHED" or not isinstance(body.get("commentary"), str):
                return self._send(h, 400, error(400, "MEDIA_ASSET_WAITING_UPLOAD or an invalid post"))
            post_id = f"urn:li:share:{7000000000000000000 + len(self.posts)}"
            self.posts.append({"id": post_id, "body": body, "data": v["data"]})
        if fault is not None and fault[0] == "lost":
            return self._drop(h)
        return self._send(h, 201, b"", {"x-restli-id": post_id})

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, code: int, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        try:
            h.send_response(code)
            for k, v in (headers or {}).items():
                h.send_header(k, v)
            if body:
                h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
            if body:
                h.wfile.write(body)
        except OSError:
            pass

    @staticmethod
    def _drop(h: BaseHTTPRequestHandler) -> None:
        h.close_connection = True
        try:
            h.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
