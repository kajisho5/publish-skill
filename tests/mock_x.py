"""An in-process mock of the X API v2 chunked media upload and create-post endpoints, bound to 127.0.0.1 on a free port
(roadmap step 5, ADR-007). Nothing here is part of the package, and nothing here contacts X:
helpers.OfflineTestCase lets a test connect only to the loopback ports of mock servers it started.

What it implements, as X's reference pages describe it (every request needs Authorization: Bearer <token>, else 401):
  POST /2/media/upload/initialize        JSON {"media_type", "total_bytes", "media_category"}: a video media_type and
                                         a positive total_bytes (else 400) -> 200 {"data": {"id", "media_key",
                                         "expires_after_secs": 86400}}
  POST /2/media/upload/{id}/append       multipart/form-data with segment_index (0-9999) and media -> 200 {"data":
                                         {"expires_at"}}; the segment is kept under its index (a resend replaces it)
  POST /2/media/upload/{id}/finalize     no body -> 200 {"data": {"id", "size", "expires_after_secs",
                                         "processing_info"?}}: the segments joined in index order; 400 when they do
                                         not add up to total_bytes; processing_info from `processing` (none if empty)
  GET /2/media/upload?command=STATUS&media_id={id}
                                         -> 200 {"data": {"id", "processing_info"}}: the next entry of `processing`
                                         (the last one repeats)
  POST /2/tweets                         JSON {"text"?, "media": {"media_ids": [id]}}: the media finalized and its
                                         processing succeeded (else 400) -> 201 {"data": {"id", "text"}}

Faults are scripted per request kind ("init", "append", "finalize", "status", "post"): `faults[kind]` is a list
consumed one entry per request, each one of
  None                                   answer normally
  ("status", code, detail, headers)      answer with that HTTP status and a problem body (detail may be None)
  ("drop",)                              (append) read the segment, keep none of it, drop the connection
  ("lost",)                              (append, post) do the work, then drop the connection (the answer is lost)
  ("stall", seconds)                     wait, then drop the connection without answering (the client times out)
  ("call", fn)                           run fn() first (e.g. edit the file mid-upload), then answer normally"""
from __future__ import annotations

import json
import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, urlsplit

VIDEO_TYPES = ("video/mp4", "video/webm", "video/mp2t", "video/quicktime")
_PATH = re.compile(r"^/2/media/upload/([0-9]{1,19})/(append|finalize)$")


def problem(status: int, detail: Optional[str] = None, title: str = "Error") -> bytes:
    body: Dict[str, Any] = {"type": "https://api.x.com/2/problems/invalid-request", "title": title, "status": status}
    if detail:
        body["detail"] = detail
    return json.dumps(body).encode()


def parse_multipart(content_type: str, raw: bytes) -> Dict[str, bytes]:
    m = re.search(r"boundary=(\S+)", content_type or "")
    if not m:
        return {}
    out: Dict[str, bytes] = {}
    for part in raw.split(b"--" + m.group(1).encode())[1:]:
        if part.startswith(b"--"):
            break
        head, _, body = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        if name:
            out[name.group(1).decode()] = body[:-2] if body.endswith(b"\r\n") else body
    return out


class MockX:
    def __init__(self, access_token: str, processing: Optional[List[Dict[str, Any]]] = None) -> None:
        self.token = access_token
        self.processing = processing or []
        self.faults: Dict[str, List[Any]] = {}
        self.media: Dict[str, Dict[str, Any]] = {}    # media id -> {"total", "type", "segments", "data", "polls"}
        self.requests: List[Dict[str, Any]] = []      # every request: kind, method, path, headers, length, segment
        self.posts: List[Dict[str, Any]] = []         # every post made: {"id", "body", "data"}
        self.inits: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.server: Optional[ThreadingHTTPServer] = None

    # ---- lifecycle -------------------------------------------------------------------------------------------
    def start(self, allowed_ports: Set[int]) -> "MockX":
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def do_POST(self) -> None:
                mock._handle(self, "POST")

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

    def segment_indexes(self) -> List[Optional[int]]:
        return [r.get("segment") for r in self.requests if r["kind"] == "append"]

    def _fault(self, kind: str) -> Any:
        with self.lock:
            queue = self.faults.get(kind)
            return queue.pop(0) if queue else None

    # ---- the protocol ----------------------------------------------------------------------------------------
    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        parts = urlsplit(h.path)
        headers = {k.lower(): v for k, v in h.headers.items()}
        length = int(headers.get("content-length") or 0)
        m = _PATH.match(parts.path)
        if method == "POST" and parts.path == "/2/media/upload/initialize":
            kind, media_id = "init", ""
        elif method == "POST" and m:
            kind, media_id = m.group(2), m.group(1)
        elif method == "GET" and parts.path == "/2/media/upload":
            kind, media_id = "status", parse_qs(parts.query).get("media_id", [""])[0]
        elif method == "POST" and parts.path == "/2/tweets":
            kind, media_id = "post", ""
        else:
            kind, media_id = "unknown", ""
        record: Dict[str, Any] = {"kind": kind, "method": method, "path": parts.path, "query": parts.query,
                                  "headers": headers, "length": length}
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
                return self._send(h, fault[1], problem(fault[1], fault[2]), fault[3] if len(fault) > 3 else {})
        if kind == "unknown":
            return self._send(h, 404, problem(404, "not found"))
        if headers.get("authorization") != "Bearer " + self.token:
            return self._send(h, 401, problem(401, "Unauthorized", "Unauthorized"))
        if kind == "init":
            return self._init(h, headers, raw)
        if kind == "append":
            return self._append(h, headers, raw, media_id, record, fault)
        if kind == "finalize":
            return self._finalize(h, media_id)
        if kind == "status":
            return self._status(h, parse_qs(parts.query), media_id)
        return self._post(h, headers, raw, fault)

    def _init(self, h: BaseHTTPRequestHandler, headers: Dict[str, str], raw: bytes) -> None:
        if headers.get("content-type") != "application/json":
            return self._send(h, 400, problem(400, "Content-Type"))
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._send(h, 400, problem(400, "body"))
        if body.get("media_type") not in VIDEO_TYPES or not isinstance(body.get("total_bytes"), int) \
                or body["total_bytes"] <= 0 or body.get("media_category") != "tweet_video":
            return self._send(h, 400, problem(400, "invalid initialize body"))
        with self.lock:
            media_id = str(1880000000000000000 + len(self.inits))
            self.inits.append(body)
            self.media[media_id] = {"total": body["total_bytes"], "type": body["media_type"], "segments": {},
                                    "data": None, "polls": 0}
        return self._send(h, 200, json.dumps({"data": {"id": media_id, "media_key": "7_" + media_id,
                                                       "expires_after_secs": 86400}}).encode())

    def _append(self, h: BaseHTTPRequestHandler, headers: Dict[str, str], raw: bytes, media_id: str,
                record: Dict[str, Any], fault: Any) -> None:
        u = self.media.get(media_id)
        if u is None:
            return self._send(h, 404, problem(404, "unknown media id"))
        if not (headers.get("content-type") or "").startswith("multipart/form-data"):
            return self._send(h, 400, problem(400, "Content-Type"))
        fields = parse_multipart(headers.get("content-type", ""), raw)
        try:
            index = int(fields.get("segment_index", b"").decode())
        except ValueError:
            return self._send(h, 400, problem(400, "segment_index"))
        record["segment"] = index
        if not 0 <= index <= 9999 or "media" not in fields:
            return self._send(h, 400, problem(400, "segment"))
        if fault is not None and fault[0] == "drop":
            return self._drop(h)
        with self.lock:
            u["segments"][index] = fields["media"]
        if fault is not None and fault[0] == "lost":
            return self._drop(h)
        return self._send(h, 200, json.dumps({"data": {"expires_at": 1793000000}}).encode())

    def _info(self, u: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        if not self.processing:
            return None
        info = self.processing[min(u["polls"], len(self.processing) - 1)]
        u["polls"] += 1
        return info

    def _finalize(self, h: BaseHTTPRequestHandler, media_id: str) -> None:
        with self.lock:
            u = self.media.get(media_id)
            if u is None:
                return self._send(h, 404, problem(404, "unknown media id"))
            data = b"".join(u["segments"][i] for i in sorted(u["segments"]))
            if len(data) != u["total"]:
                return self._send(h, 400, problem(400, f"expected {u['total']} bytes, got {len(data)}"))
            u["data"] = data
            out: Dict[str, Any] = {"id": media_id, "size": len(data), "expires_after_secs": 86399}
            info = self._info(u)
            if info is not None:
                out["processing_info"] = info
                u["state"] = info.get("state")
            else:
                u["state"] = "succeeded"
        return self._send(h, 200, json.dumps({"data": out}).encode())

    def _status(self, h: BaseHTTPRequestHandler, query: Dict[str, List[str]], media_id: str) -> None:
        with self.lock:
            u = self.media.get(media_id)
            if u is None or query.get("command") != ["STATUS"]:
                return self._send(h, 400, problem(400, "media_id"))
            info = self._info(u) or {"state": "succeeded", "progress_percent": 100}
            u["state"] = info.get("state")
        return self._send(h, 200, json.dumps({"data": {"id": media_id, "processing_info": info}}).encode())

    def _post(self, h: BaseHTTPRequestHandler, headers: Dict[str, str], raw: bytes, fault: Any) -> None:
        if headers.get("content-type") != "application/json":
            return self._send(h, 400, problem(400, "Content-Type"))
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._send(h, 400, problem(400, "body"))
        ids = (body.get("media") or {}).get("media_ids") or []
        with self.lock:
            u = self.media.get(ids[0]) if len(ids) == 1 else None
            if u is None or u["data"] is None or u.get("state") != "succeeded":
                return self._send(h, 400, problem(400, "Your media IDs are invalid."))
            post_id = str(1990000000000000000 + len(self.posts))
            self.posts.append({"id": post_id, "body": body, "data": u["data"]})
        if fault is not None and fault[0] == "lost":
            return self._drop(h)
        return self._send(h, 201, json.dumps({"data": {"id": post_id, "text": body.get("text", ""),
                                                       "edit_history_post_ids": [post_id]}}).encode())

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, code: int, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        try:
            h.send_response(code)
            for k, v in (headers or {}).items():
                h.send_header(k, v)
            if body:
                h.send_header("Content-Type", "application/json" if code < 400 else "application/problem+json")
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
