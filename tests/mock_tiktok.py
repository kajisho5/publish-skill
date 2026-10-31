"""An in-process mock of TikTok's Content Posting API (Direct Post, FILE_UPLOAD), bound to 127.0.0.1 on a free port
(roadmap step 5, ADR-006). Nothing here is part of the package, and nothing here contacts TikTok:
helpers.OfflineTestCase lets a test connect only to the loopback ports of mock servers it started.

What it implements, as TikTok's reference pages and media transfer guide describe it:
  POST /v2/post/publish/creator_info/query/   Authorization: Bearer <token>, Content-Type: application/json;
                                              charset=UTF-8 -> 200 {"data": {creator_username, creator_nickname,
                                              privacy_level_options, max_video_post_duration_sec, ...}, "error":
                                              {"code": "ok"}}
  POST /v2/post/publish/video/init/           the same headers, {"post_info": {"title"?, "privacy_level"},
                                              "source_info": {"source": "FILE_UPLOAD", "video_size", "chunk_size",
                                              "total_chunk_count"}}: privacy_level among the options (else 403
                                              privacy_level_option_mismatch), total_chunk_count == video_size //
                                              chunk_size, or 1 with chunk_size == video_size (else 400 invalid_param)
                                              -> 200 {"data": {"publish_id", "upload_url"}} (upload_url on this server)
  PUT /upload/?upload_id=...                  Content-Type video/mp4 | video/quicktime | video/webm (else 400),
                                              Content-Range: bytes FIRST-LAST/TOTAL starting at the bytes held and the
                                              body that long (else 416 / 400) -> 206, or 201 once every byte is held;
                                              an unknown upload -> 404
  POST /v2/post/publish/status/fetch/         {"publish_id"} -> 200 {"data": {"status", "uploaded_bytes", "fail_reason"?,
                                              "publicaly_available_post_id"}}: PROCESSING_UPLOAD until the last chunk,
                                              then the entries of `statuses` one per query (the last one repeats)

Faults are scripted per request kind ("creator_info", "init", "put", "status"): `faults[kind]` is a list consumed one
entry per request, each one of
  None                                   answer normally
  ("status", code, error_code, headers)  answer with that HTTP status and a TikTok error body (error_code may be None)
  ("drop",)                              (put) read the chunk, keep none of it, drop the connection
  ("lost",)                              (put) keep the chunk, then drop the connection (the answer is lost)
  ("stall", seconds)                     wait, then drop the connection without answering (the client times out)
  ("expire",)                            (put) the upload URL has expired: 403
  ("call", fn)                           run fn() first (e.g. edit the file mid-upload), then answer normally"""
from __future__ import annotations

import json
import re
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, urlsplit

JSON_TYPE = "application/json; charset=UTF-8"
VIDEO_TYPES = ("video/mp4", "video/quicktime", "video/webm")
CREATOR = "/v2/post/publish/creator_info/query/"
INIT = "/v2/post/publish/video/init/"
STATUS = "/v2/post/publish/status/fetch/"
_RANGE = re.compile(r"^bytes (\d+)-(\d+)/(\d+)$")


def tiktok_body(data: Optional[Dict[str, Any]] = None, code: str = "ok", message: str = "") -> bytes:
    return json.dumps({"data": data or {}, "error": {"code": code, "message": message, "log_id": "202610310000"}}).encode()


class MockTikTok:
    def __init__(self, access_token: str, privacy_level_options: Optional[List[str]] = None,
                 statuses: Optional[List[Dict[str, Any]]] = None, upload_url_base: Optional[str] = None,
                 username: str = "studio_test") -> None:
        self.token = access_token
        self.options = privacy_level_options or ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"]
        self.statuses = statuses or [{"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": [7300000000000000001]}]
        self.upload_url_base = upload_url_base       # hand out upload URLs on another base (a refused host)
        self.username = username
        self.faults: Dict[str, List[Any]] = {}
        self.uploads: Dict[str, Dict[str, Any]] = {}  # upload id -> {"size", "data", "publish_id", "body"}
        self.by_publish: Dict[str, str] = {}          # publish id -> upload id
        self.upload_urls: List[str] = []
        self.requests: List[Dict[str, Any]] = []      # every request: kind, method, path, headers, body length
        self.posts: Dict[str, Dict[str, Any]] = {}    # completed: publish id -> {"body", "data"}
        self.inits: List[Dict[str, Any]] = []         # every init body
        self.lock = threading.Lock()
        self.server: Optional[ThreadingHTTPServer] = None

    # ---- lifecycle -------------------------------------------------------------------------------------------
    def start(self, allowed_ports: Set[int]) -> "MockTikTok":
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def do_POST(self) -> None:
                mock._handle(self, "POST")

            def do_PUT(self) -> None:
                mock._handle(self, "PUT")

        class Server(ThreadingHTTPServer):
            daemon_threads = True

            def handle_error(self, *_a: Any) -> None:     # a dropped connection is a scripted fault, not noise
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

    def ranges(self) -> List[str]:
        """Content-Range of each PUT, and "STATUS" for each status query, in order."""
        return [r["headers"].get("content-range", "") if r["kind"] == "put" else "STATUS"
                for r in self.requests if r["kind"] in ("put", "status")]

    def _fault(self, kind: str) -> Any:
        with self.lock:
            queue = self.faults.get(kind)
            return queue.pop(0) if queue else None

    # ---- the protocol ----------------------------------------------------------------------------------------
    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        parts = urlsplit(h.path)
        path = parts.path
        headers = {k.lower(): v for k, v in h.headers.items()}
        length = int(headers.get("content-length") or 0)
        kind = {("POST", CREATOR): "creator_info", ("POST", INIT): "init", ("POST", STATUS): "status",
                ("PUT", "/upload/"): "put"}.get((method, path), "unknown")
        with self.lock:
            self.requests.append({"kind": kind, "method": method, "path": path, "headers": headers, "length": length})
        fault = self._fault(kind)
        if fault is not None:
            if fault[0] == "call":
                fault[1]()
                fault = None
            elif fault[0] == "stall":
                time.sleep(fault[1])
                return self._drop(h)
            elif fault[0] == "status":
                h.rfile.read(length)
                extra = fault[3] if len(fault) > 3 else {}
                return self._send(h, fault[1], tiktok_body(code=fault[2] or "internal_error"), extra)
            elif fault[0] == "expire" and kind == "put":
                h.rfile.read(length)
                return self._send(h, 403, b"")
        raw_fault = fault
        if kind == "put":
            return self._put(h, parse_qs(parts.query).get("upload_id", [""])[0], headers, length, raw_fault)
        raw = h.rfile.read(length)
        if kind == "unknown":
            return self._send(h, 404, tiktok_body(code="not_found"))
        if headers.get("authorization") != "Bearer " + self.token:
            return self._send(h, 401, tiktok_body(code="access_token_invalid", message="The access token is invalid"))
        if headers.get("content-type") != JSON_TYPE:
            return self._send(h, 400, tiktok_body(code="invalid_param", message="Content-Type"))
        if kind == "creator_info":
            return self._send(h, 200, tiktok_body({"creator_avatar_url": "https://p16.example/avatar.jpg",
                                                   "creator_username": self.username, "creator_nickname": "Studio Test",
                                                   "privacy_level_options": self.options, "comment_disabled": False,
                                                   "duet_disabled": False, "stitch_disabled": False,
                                                   "max_video_post_duration_sec": 600}))
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._send(h, 400, tiktok_body(code="invalid_param", message="body"))
        if kind == "init":
            return self._init(h, body)
        return self._status(h, body)

    def _init(self, h: BaseHTTPRequestHandler, body: Dict[str, Any]) -> None:
        post, src = body.get("post_info") or {}, body.get("source_info") or {}
        if post.get("privacy_level") not in self.options:
            return self._send(h, 403, tiktok_body(code="privacy_level_option_mismatch"))
        size, chunk, total = src.get("video_size"), src.get("chunk_size"), src.get("total_chunk_count")
        if src.get("source") != "FILE_UPLOAD" or not all(isinstance(v, int) and v > 0 for v in (size, chunk, total)):
            return self._send(h, 400, tiktok_body(code="invalid_param", message="source_info"))
        if not (total == size // chunk or (total == 1 and chunk == size)) or total > 1000:
            return self._send(h, 400, tiktok_body(code="invalid_param", message="total_chunk_count"))
        upload_id = secrets.token_hex(12)
        publish_id = f"v_pub_file~v2-1.{7200000000000000000 + len(self.inits)}"
        base = self.upload_url_base or self.url
        url = f"{base}/upload/?upload_id={upload_id}&upload_token={secrets.token_hex(16)}"
        with self.lock:
            self.inits.append(body)
            self.upload_urls.append(url)
            self.uploads[upload_id] = {"size": size, "data": bytearray(), "publish_id": publish_id, "body": body,
                                       "polls": 0}
            self.by_publish[publish_id] = upload_id
        return self._send(h, 200, tiktok_body({"publish_id": publish_id, "upload_url": url}))

    def _put(self, h: BaseHTTPRequestHandler, upload_id: str, headers: Dict[str, str], length: int, fault: Any) -> None:
        with self.lock:
            u = self.uploads.get(upload_id)
        if u is None:
            h.rfile.read(length)
            return self._send(h, 404, b"")
        if "authorization" in headers:                          # the token never goes to the upload URL
            h.rfile.read(length)
            return self._send(h, 400, tiktok_body(code="invalid_param", message="unexpected Authorization"))
        if headers.get("content-type") not in VIDEO_TYPES:
            h.rfile.read(length)
            return self._send(h, 400, b"")
        m = _RANGE.match(headers.get("content-range", ""))
        if not m or int(m.group(3)) != u["size"] or int(m.group(2)) - int(m.group(1)) + 1 != length:
            h.rfile.read(length)
            return self._send(h, 400, b"")
        if int(m.group(1)) != len(u["data"]):
            h.rfile.read(length)
            return self._send(h, 416, b"")
        data = h.rfile.read(length)
        if fault is not None and fault[0] == "drop":
            return self._drop(h)
        with self.lock:
            u["data"] += data
            done = len(u["data"]) == u["size"]
            if done:
                self.posts[u["publish_id"]] = {"body": u["body"], "data": bytes(u["data"])}
        if fault is not None and fault[0] == "lost":
            return self._drop(h)
        return self._send(h, 201 if done else 206, b"")

    def _status(self, h: BaseHTTPRequestHandler, body: Dict[str, Any]) -> None:
        with self.lock:
            u = self.uploads.get(self.by_publish.get(str(body.get("publish_id")), ""))
            if u is None:
                return self._send(h, 400, tiktok_body(code="invalid_publish_id"))
            data: Dict[str, Any] = {"uploaded_bytes": len(u["data"]), "publicaly_available_post_id": []}
            if len(u["data"]) < u["size"]:
                data["status"] = "PROCESSING_UPLOAD"
            else:
                data.update(self.statuses[min(u["polls"], len(self.statuses) - 1)])
                u["polls"] += 1
        return self._send(h, 200, tiktok_body(data))

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, code: int, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        try:
            h.send_response(code)
            for k, v in (headers or {}).items():
                h.send_header(k, v)
            if body:
                h.send_header("Content-Type", "application/json; charset=UTF-8")
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
