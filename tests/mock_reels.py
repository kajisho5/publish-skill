"""An in-process mock of Instagram content publishing for Reels with a resumable upload, bound to 127.0.0.1 on a free
port (roadmap step 5, ADR-010). Nothing here is part of the package, and nothing here contacts Instagram or Facebook:
helpers.OfflineTestCase lets a test connect only to the loopback ports of mock servers it started. One server stands
in for graph.facebook.com and rupload.facebook.com (the tests point both bases at it).

What it implements, as Meta's pages describe it (Graph API v26.0; Graph errors are {"error": {"message", "type",
"code", "fbtrace_id"}}):
  POST /v26.0/<IG_ID>/media                  form: access_token, media_type=REELS, upload_type=resumable, caption?
                                             -> 200 {"id": <container>}
  POST /ig-api-upload/v26.0/<container>      Authorization: OAuth <token>, offset: 0, file_size; the file -> 200
                                             {"success": true, "message": "Upload successful."}; a new upload replaces
                                             the bytes; a failure is {"debug_info": {"retriable", "type", "message"}}
  GET /v26.0/<container>?fields=status_code&access_token
                                             -> 200 {"status_code", "id"}: IN_PROGRESS before the upload, then the
                                             next entry of `processing` (the last one repeats; FINISHED when empty)
  POST /v26.0/<IG_ID>/media_publish          form: creation_id (FINISHED), access_token -> 200 {"id": <media id>}

Faults are scripted per request kind ("container", "upload", "status", "publish"): `faults[kind]` is a list consumed
one entry per request, each one of
  None                                   answer normally
  ("status", http, code, message, headers)  answer with that HTTP status and a Graph error (code may be None)
  ("debug", http, retriable, message)    (upload) answer with that HTTP status and a debug_info failure
  ("cut", n)                             (upload) read n bytes of the body, keep none, drop the connection
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

_MEDIA = re.compile(r"^/v26\.0/([0-9]+)/media$")
_PUBLISH = re.compile(r"^/v26\.0/([0-9]+)/media_publish$")
_UPLOAD = re.compile(r"^/ig-api-upload/v26\.0/([0-9]+)$")
_STATUS = re.compile(r"^/v26\.0/([0-9]+)$")


def error(status: int, code: Optional[int], message: Optional[str] = None) -> bytes:
    err: Dict[str, Any] = {"type": "OAuthException", "fbtrace_id": "AbCdEfTrace"}
    if code is not None:
        err["code"] = code
    if message:
        err["message"] = message
    return json.dumps({"error": err}).encode()


class MockReels:
    def __init__(self, ig_id: str, token: str, processing: Optional[List[str]] = None) -> None:
        self.ig_id, self.token = ig_id, token
        self.processing = processing or []
        self.faults: Dict[str, List[Any]] = {}
        self.containers: Dict[str, Dict[str, Any]] = {}  # container -> {"fields", "data", "polls", "state"}
        self.requests: List[Dict[str, Any]] = []         # every request: kind, method, path, query, headers, length
        self.media: List[Dict[str, Any]] = []            # every reel published: {"id", "fields", "data"}
        self.lock = threading.Lock()
        self.server: Optional[ThreadingHTTPServer] = None

    # ---- lifecycle -------------------------------------------------------------------------------------------
    def start(self, allowed_ports: Set[int]) -> "MockReels":
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

    def _fault(self, kind: str) -> Any:
        with self.lock:
            queue = self.faults.get(kind)
            return queue.pop(0) if queue else None

    # ---- the protocol ----------------------------------------------------------------------------------------
    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        parts = urlsplit(h.path)
        headers = {k.lower(): v for k, v in h.headers.items()}
        length = int(headers.get("content-length") or 0)
        m, p, u, s = (_MEDIA.match(parts.path), _PUBLISH.match(parts.path), _UPLOAD.match(parts.path),
                      _STATUS.match(parts.path))
        kind = ("container" if method == "POST" and m else "publish" if method == "POST" and p else
                "upload" if method == "POST" and u else "status" if method == "GET" and s else "unknown")
        record: Dict[str, Any] = {"kind": kind, "method": method, "path": parts.path, "query": parts.query,
                                  "headers": headers, "length": length}
        with self.lock:
            self.requests.append(record)
        fault = self._fault(kind)
        if kind == "upload":
            return self._upload(h, headers, u.group(1), length, fault)
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
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8") if method == "POST" else parts.query,
                                              keep_blank_values=True).items()}
        record["form"] = {k: v for k, v in form.items() if k != "access_token"}
        if form.get("access_token") != self.token:
            return self._send(h, 400, error(400, 190, "Invalid OAuth access token."))
        if kind == "container":
            return self._container(h, headers, m.group(1), form)
        if kind == "status":
            return self._status(h, s.group(1), form)
        return self._publish(h, p.group(1), form, fault)

    def _container(self, h: BaseHTTPRequestHandler, headers: Dict[str, str], ig: str, form: Dict[str, str]) -> None:
        if headers.get("content-type") != "application/x-www-form-urlencoded" or ig != self.ig_id:
            return self._send(h, 400, error(400, 100, "Invalid parameter"))
        if form.get("media_type") != "REELS" or form.get("upload_type") != "resumable":
            return self._send(h, 400, error(400, 100, "media_type REELS and upload_type resumable are expected"))
        with self.lock:
            cid = str(17900000000000000 + len(self.containers))
            self.containers[cid] = {"fields": {k: v for k, v in form.items() if k != "access_token"}, "data": None,
                                    "polls": 0, "state": "IN_PROGRESS"}
        return self._send(h, 200, json.dumps({"id": cid}).encode())

    def _upload(self, h: BaseHTTPRequestHandler, headers: Dict[str, str], cid: str, length: int, fault: Any) -> None:
        if fault is not None and fault[0] == "call":
            fault[1]()
            fault = None
        if fault is not None and fault[0] == "cut":
            h.rfile.read(min(fault[1], length))
            return self._drop(h)
        raw = b""
        while len(raw) < length:
            buf = h.rfile.read(min(65536, length - len(raw)))
            if not buf:
                break
            raw += buf
        if len(raw) < length:
            return self._drop(h)                          # the client stopped sending (its file changed)
        if fault is not None and fault[0] == "stall":
            time.sleep(fault[1])
            return self._drop(h)
        if fault is not None and fault[0] == "status":
            return self._send(h, fault[1], error(fault[1], fault[2], fault[3]), fault[4] if len(fault) > 4 else {})
        if fault is not None and fault[0] == "debug":
            return self._send(h, fault[1], json.dumps({"debug_info": {"retriable": fault[2], "type": "ProcessingFailedError",
                                                                      "message": fault[3]}}).encode())
        if headers.get("authorization") != "OAuth " + self.token:
            return self._send(h, 400, json.dumps({"debug_info": {"retriable": False, "type": "ProcessingFailedError",
                                                                 "message": "unauthorized user request"}}).encode())
        with self.lock:
            c = self.containers.get(cid)
        if c is None or headers.get("offset") != "0" or headers.get("file_size") != str(length):
            return self._send(h, 400, json.dumps({"debug_info": {"retriable": False, "type": "BadRequest",
                                                                 "message": "bad upload headers"}}).encode())
        with self.lock:
            c["data"] = raw
        if fault is not None and fault[0] == "lost":
            return self._drop(h)
        return self._send(h, 200, json.dumps({"success": True, "message": "Upload successful."}).encode())

    def _status(self, h: BaseHTTPRequestHandler, cid: str, form: Dict[str, str]) -> None:
        with self.lock:
            c = self.containers.get(cid)
            if c is None or form.get("fields") != "status_code":
                return self._send(h, 400, error(400, 100, "Invalid parameter"))
            if c["data"] is not None:
                c["state"] = self.processing[min(c["polls"], len(self.processing) - 1)] if self.processing else "FINISHED"
                c["polls"] += 1
            state = c["state"]
        return self._send(h, 200, json.dumps({"status_code": state, "id": cid}).encode())

    def _publish(self, h: BaseHTTPRequestHandler, ig: str, form: Dict[str, str], fault: Any) -> None:
        with self.lock:
            c = self.containers.get(form.get("creation_id", ""))
            if ig != self.ig_id or c is None or c["state"] != "FINISHED":
                return self._send(h, 400, error(400, 9007, "Media ID is not available"))
            media_id = str(17800000000000000 + len(self.media) + 3)
            c["state"] = "PUBLISHED"
            self.media.append({"id": media_id, "fields": c["fields"], "data": c["data"]})
        if fault is not None and fault[0] == "lost":
            return self._drop(h)
        return self._send(h, 200, json.dumps({"id": media_id}).encode())

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
