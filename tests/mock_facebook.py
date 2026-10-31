"""An in-process mock of the Facebook Resumable Upload API and the Page videos endpoint, bound to 127.0.0.1 on a free
port (roadmap step 5, ADR-009). Nothing here is part of the package, and nothing here contacts Facebook:
helpers.OfflineTestCase lets a test connect only to the loopback ports of mock servers it started. One server stands
in for both graph.facebook.com and graph-video.facebook.com (the tests point both bases at it, and the mock records
which path each request used).

What it implements, as Meta's pages describe it (Graph API v26.0; errors are {"error": {"message", "type", "code",
"fbtrace_id"}}):
  POST /v26.0/<APP_ID>/uploads?file_name&file_length&file_type&access_token=<user token>
                                     -> 200 {"id": "upload:<session>"}; video/mp4 and a positive length (else 400)
  POST /v26.0/upload:<session>       Authorization: OAuth <user token>, file_offset: <n>, the bytes from n: n must
                                     equal the bytes held (else 400); the bytes are appended as they arrive, so a
                                     dropped connection keeps what came; when the file is whole -> 200 {"h": handle}
  GET /v26.0/upload:<session>        -> 200 {"id", "file_offset": <bytes held>}
  POST /v26.0/<PAGE_ID>/videos       multipart: access_token (the Page token), fbuploader_video_file_chunk (a handle),
                                     title?, description?, published?, secret? -> 200 {"id": <video id>}

Faults are scripted per request kind ("session", "upload", "offset", "publish"): `faults[kind]` is a list consumed
one entry per request, each one of
  None                                   answer normally
  ("status", http, code, message, headers)  answer with that HTTP status and a Graph error (code may be None)
  ("cut", n)                             (upload) keep only the first n bytes of the body, drop the connection
  ("lost",)                              (upload, publish) do the work, then drop the connection (the answer is lost)
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

_SESSION = re.compile(r"^/v26\.0/([0-9]+)/uploads$")
_UPLOAD = re.compile(r"^/v26\.0/upload:([A-Za-z0-9]+)$")
_PUBLISH = re.compile(r"^/v26\.0/([0-9]+)/videos$")


def error(status: int, code: Optional[int], message: Optional[str] = None) -> bytes:
    err: Dict[str, Any] = {"type": "OAuthException" if code == 190 else "GraphMethodException",
                           "fbtrace_id": "AbCdEfTrace"}
    if code is not None:
        err["code"] = code
    if message:
        err["message"] = message
    return json.dumps({"error": err}).encode()


def parse_multipart(content_type: str, raw: bytes) -> Dict[str, str]:
    m = re.search(r"boundary=(\S+)", content_type or "")
    if not m:
        return {}
    out: Dict[str, str] = {}
    for part in raw.split(b"--" + m.group(1).encode())[1:]:
        if part.startswith(b"--"):
            break
        head, _, body = part.partition(b"\r\n\r\n")
        name = re.search(rb'name="([^"]+)"', head)
        if name:
            out[name.group(1).decode()] = (body[:-2] if body.endswith(b"\r\n") else body).decode("utf-8")
    return out


class MockFacebook:
    def __init__(self, app_id: str, user_token: str, page_id: str, page_token: str) -> None:
        self.app_id, self.user_token, self.page_id, self.page_token = app_id, user_token, page_id, page_token
        self.faults: Dict[str, List[Any]] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}   # session -> {"length", "name", "data", "handle"}
        self.requests: List[Dict[str, Any]] = []        # every request: kind, method, path, query, headers, length
        self.videos: List[Dict[str, Any]] = []          # every video published: {"id", "fields", "data"}
        self.handles: Dict[str, str] = {}               # handle -> session
        self.lock = threading.Lock()
        self.server: Optional[ThreadingHTTPServer] = None

    # ---- lifecycle -------------------------------------------------------------------------------------------
    def start(self, allowed_ports: Set[int]) -> "MockFacebook":
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

    def offsets(self) -> List[Optional[str]]:
        return [r["headers"].get("file_offset") for r in self.requests if r["kind"] == "upload"]

    def _fault(self, kind: str) -> Any:
        with self.lock:
            queue = self.faults.get(kind)
            return queue.pop(0) if queue else None

    # ---- the protocol ----------------------------------------------------------------------------------------
    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        parts = urlsplit(h.path)
        headers = {k.lower(): v for k, v in h.headers.items()}
        length = int(headers.get("content-length") or 0)
        s, u, p = _SESSION.match(parts.path), _UPLOAD.match(parts.path), _PUBLISH.match(parts.path)
        kind = ("session" if method == "POST" and s else "upload" if method == "POST" and u else
                "offset" if method == "GET" and u else "publish" if method == "POST" and p else "unknown")
        record: Dict[str, Any] = {"kind": kind, "method": method, "path": parts.path, "query": parts.query,
                                  "headers": headers, "length": length}
        with self.lock:
            self.requests.append(record)
        fault = self._fault(kind)
        if kind == "upload":
            return self._upload(h, headers, u.group(1), length, fault, record)
        raw = h.rfile.read(length) if length else b""
        if fault is not None:
            if fault[0] == "call":
                fault[1]()
                fault = None
            elif fault[0] == "stall":
                time.sleep(fault[1])
                return self._drop(h)
            elif fault[0] == "status":
                return self._send(h, fault[1], error(fault[1], fault[2], fault[3]), fault[4] if len(fault) > 4 else {})
        if kind == "unknown":
            return self._send(h, 404, error(404, 100, "Unknown path"))
        if kind == "session":
            return self._session(h, s.group(1), parse_qs(parts.query))
        if kind == "offset":
            if headers.get("authorization") != "OAuth " + self.user_token:
                return self._send(h, 401, error(401, 190, "Invalid OAuth access token."))
            with self.lock:
                sess = self.sessions.get(u.group(1))
            if sess is None:
                return self._send(h, 400, error(400, 100, "unknown session"))
            return self._send(h, 200, json.dumps({"id": "upload:" + u.group(1),
                                                  "file_offset": len(sess["data"])}).encode())
        return self._publish(h, headers, p.group(1), raw, fault)

    def _session(self, h: BaseHTTPRequestHandler, app: str, q: Dict[str, List[str]]) -> None:
        if q.get("access_token") != [self.user_token]:
            return self._send(h, 400, error(400, 190, "Invalid OAuth access token."))
        if app != self.app_id:
            return self._send(h, 400, error(400, 100, "unknown app"))
        try:
            n = int(q.get("file_length", ["0"])[0])
        except ValueError:
            n = 0
        if q.get("file_type") != ["video/mp4"] or n <= 0 or not q.get("file_name"):
            return self._send(h, 400, error(400, 100, "invalid upload session parameters"))
        with self.lock:
            sid = f"MTpmYWtl{len(self.sessions):04d}"
            self.sessions[sid] = {"length": n, "name": q["file_name"][0], "data": b"", "handle": None}
        return self._send(h, 200, json.dumps({"id": "upload:" + sid}).encode())

    def _upload(self, h: BaseHTTPRequestHandler, headers: Dict[str, str], sid: str, length: int, fault: Any,
                record: Dict[str, Any]) -> None:
        if fault is not None and fault[0] == "call":
            fault[1]()
            fault = None
        if fault is not None and fault[0] == "stall":
            h.rfile.read(length)
            time.sleep(fault[1])
            return self._drop(h)
        if fault is not None and fault[0] == "status":
            h.rfile.read(length)
            return self._send(h, fault[1], error(fault[1], fault[2], fault[3]), fault[4] if len(fault) > 4 else {})
        with self.lock:
            sess = self.sessions.get(sid)
        if headers.get("authorization") != "OAuth " + self.user_token:
            h.rfile.read(length)
            return self._send(h, 401, error(401, 190, "Invalid OAuth access token."))
        if sess is None:
            h.rfile.read(length)
            return self._send(h, 400, error(400, 100, "unknown session"))
        try:
            offset = int(headers.get("file_offset", ""))
        except ValueError:
            offset = -1
        if offset != len(sess["data"]):
            h.rfile.read(length)
            return self._send(h, 400, error(400, 100, f"file_offset must be {len(sess['data'])}"))
        keep = fault[1] if fault is not None and fault[0] == "cut" else None
        got = 0
        while got < length:                               # appended as it arrives: a cut connection keeps what came
            buf = h.rfile.read(min(65536, length - got))
            if not buf:
                break
            if keep is not None:
                buf = buf[:max(0, keep - got)]
            with self.lock:
                sess["data"] += buf
            got += len(buf)
            if keep is not None and got >= keep:
                return self._drop(h)
        record["received"] = got
        if got < length:
            return self._drop(h)                          # the client stopped sending (its file changed)
        if len(sess["data"]) != sess["length"]:
            return self._send(h, 400, error(400, 100, "file_length does not match the bytes uploaded"))
        with self.lock:
            sess["handle"] = sess["handle"] or f"2:c2FtcGxl{sid}handle-secret"
            self.handles[sess["handle"]] = sid
        if fault is not None and fault[0] == "lost":
            return self._drop(h)
        return self._send(h, 200, json.dumps({"h": sess["handle"]}).encode())

    def _publish(self, h: BaseHTTPRequestHandler, headers: Dict[str, str], page: str, raw: bytes, fault: Any) -> None:
        if not (headers.get("content-type") or "").startswith("multipart/form-data"):
            return self._send(h, 400, error(400, 100, "Content-Type"))
        f = parse_multipart(headers.get("content-type", ""), raw)
        if f.get("access_token") != self.page_token:
            return self._send(h, 400, error(400, 190, "Invalid OAuth access token."))
        with self.lock:
            sid = self.handles.get(f.get("fbuploader_video_file_chunk", ""))
            if page != self.page_id or sid is None:
                return self._send(h, 400, error(400, 100, "Invalid parameter"))
            video_id = str(100000000000000 + len(self.videos) + 7)
            self.videos.append({"id": video_id, "fields": {k: v for k, v in f.items() if k != "access_token"},
                                "data": self.sessions[sid]["data"]})
        if fault is not None and fault[0] == "lost":
            return self._drop(h)
        return self._send(h, 200, json.dumps({"id": video_id}).encode())

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, code: int, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        try:
            h.send_response(code)
            for k, v in (headers or {}).items():
                h.send_header(k, v)
            h.send_header("Content-Type", "application/json")
            h.send_header("Content-Length", str(len(body)))
            h.end_headers()
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
