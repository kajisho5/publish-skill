"""An in-process mock of Google's OAuth token endpoint and the YouTube Data API v3 resumable upload protocol, bound to
127.0.0.1 on a free port (roadmap step 4, ADR-004). Nothing here is part of the package, and nothing here contacts
Google: helpers.OfflineTestCase lets a test connect only to the loopback ports of mock servers it started.

What it implements, as the resumable upload guide describes it:
  POST /token                                   grant_type=refresh_token -> access_token (or invalid_grant)
  POST /upload/youtube/v3/{videos,thumbnails/set,captions}?uploadType=resumable
                                                200 and Location: the session URI (carries upload_id)
  PUT  <session URI>, Content-Range: bytes A-B/T
                                                308 and Range: bytes=0-N while incomplete; 201 (videos) or 200 and the
                                                resource when the last byte arrives; chunks must be contiguous and,
                                                except the last, a multiple of 256 KiB
  PUT  <session URI>, Content-Range: bytes */T  the status: 308 (Range, or none when nothing arrived) or the resource

Faults are scripted per request kind ("token", "init:videos", "init:thumbnails", "init:captions", "put", "status"):
`faults[kind]` is a list consumed one entry per request, each one of
  None                                  answer normally
  ("status", code, reason, headers)     answer with that status and a Google error body naming `reason`
  ("interrupt", keep)                   (put) keep the first `keep` bytes of the chunk, then drop the connection
  ("stall", seconds)                    wait, then drop the connection without answering (the client times out)
  ("expire",)                           (put/status) 404: the session expired
  ("call", fn)                          run fn() first (e.g. edit the file mid-upload), then answer normally"""
from __future__ import annotations

import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Set
from urllib.parse import parse_qs, urlsplit

UNIT = 256 * 1024
SCOPE_UPLOAD = "https://www.googleapis.com/auth/youtube.upload"
SCOPE_FORCE_SSL = "https://www.googleapis.com/auth/youtube.force-ssl"
METHODS = {"/upload/youtube/v3/videos": "videos", "/upload/youtube/v3/thumbnails/set": "thumbnails",
           "/upload/youtube/v3/captions": "captions"}


def google_error(code: int, reason: str, message: str = "") -> bytes:
    return json.dumps({"error": {"code": code, "message": message or reason,
                                 "errors": [{"domain": "youtube", "reason": reason, "message": message or reason}]}}).encode()


class MockYouTube:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str,
                 scope: Optional[str] = None, report_privacy: Optional[str] = None) -> None:
        self.creds = {"client_id": client_id, "client_secret": client_secret, "refresh_token": refresh_token}
        self.scope = f"{SCOPE_UPLOAD} {SCOPE_FORCE_SSL}" if scope is None else scope
        self.report_privacy = report_privacy            # answer with this privacy instead of the one sent
        self.access_token = "ya29.mock-access-" + secrets.token_hex(8)
        self.faults: Dict[str, List[Any]] = {}
        self.sessions: Dict[str, Dict[str, Any]] = {}
        self.upload_ids: List[str] = []
        self.requests: List[Dict[str, Any]] = []          # every request: kind, method, path, headers, body length
        self.videos: Dict[str, Dict[str, Any]] = {}       # id -> {"resource", "data", "query", "mime"}
        self.thumbnails: Dict[str, Dict[str, Any]] = {}
        self.captions: List[Dict[str, Any]] = []
        self.lock = threading.Lock()
        self.server: Optional[ThreadingHTTPServer] = None

    # ---- lifecycle -------------------------------------------------------------------------------------------
    def start(self, allowed_ports: Set[int]) -> "MockYouTube":
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
        return [r["headers"].get("content-range", "") for r in self.requests if r["kind"] in ("put", "status")]

    def _fault(self, kind: str) -> Any:
        with self.lock:
            queue = self.faults.get(kind)
            return queue.pop(0) if queue else None

    # ---- the protocol ----------------------------------------------------------------------------------------
    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        parts = urlsplit(h.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        headers = {k.lower(): v for k, v in h.headers.items()}
        length = int(headers.get("content-length") or 0)
        if method == "POST" and parts.path == "/token":
            kind = "token"
        elif method == "POST" and parts.path in METHODS:
            kind = "init:" + METHODS[parts.path]
        elif method == "PUT" and "upload_id" in query:
            kind = "status" if headers.get("content-range", "").startswith("bytes */") else "put"
        else:
            kind = "unknown"
        with self.lock:
            self.requests.append({"kind": kind, "method": method, "path": parts.path, "query": query, "headers": headers,
                                  "length": length})
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
                code, reason = fault[1], fault[2]
                extra = fault[3] if len(fault) > 3 else {}
                return self._send(h, code, google_error(code, reason) if reason else b"", extra)
            elif fault[0] == "expire" and kind in ("put", "status"):
                h.rfile.read(length)
                with self.lock:
                    self.sessions.pop(query["upload_id"], None)
                return self._send(h, 404, google_error(404, "notFound"))
        if kind == "token":
            return self._token(h, h.rfile.read(length))
        if kind.startswith("init:"):
            return self._init(h, kind[5:], query, headers, h.rfile.read(length))
        if kind in ("put", "status"):
            return self._put(h, query, headers, length, fault)
        h.rfile.read(length)
        return self._send(h, 400, google_error(400, "badRequest", "unknown endpoint"))

    def _token(self, h: BaseHTTPRequestHandler, body: bytes) -> None:
        form = {k: v[0] for k, v in parse_qs(body.decode("ascii")).items()}
        if h.headers.get("Content-Type") != "application/x-www-form-urlencoded" or form.get("grant_type") != "refresh_token":
            return self._send(h, 400, json.dumps({"error": "unsupported_grant_type"}).encode())
        if any(form.get(k) != v for k, v in self.creds.items()):
            return self._send(h, 400, json.dumps({"error": "invalid_grant", "error_description": "Bad Request"}).encode())
        doc: Dict[str, Any] = {"access_token": self.access_token, "expires_in": 3599, "token_type": "Bearer"}
        if self.scope:
            doc["scope"] = self.scope
        return self._send(h, 200, json.dumps(doc).encode())

    def _authorized(self, headers: Dict[str, str]) -> bool:
        return headers.get("authorization") == "Bearer " + self.access_token

    def _init(self, h: BaseHTTPRequestHandler, method: str, query: Dict[str, str], headers: Dict[str, str], body: bytes) -> None:
        if not self._authorized(headers):
            return self._send(h, 401, google_error(401, "authError"))
        if query.get("uploadType") != "resumable" or "x-upload-content-length" not in headers:
            return self._send(h, 400, google_error(400, "badRequest", "not a resumable initiation"))
        resource = json.loads(body.decode("utf-8")) if body else None
        if method == "videos" and (query.get("part") is None or resource is None):
            return self._send(h, 400, google_error(400, "badRequest", "videos.insert needs part and a resource"))
        if method == "thumbnails" and query.get("videoId") not in self.videos:
            return self._send(h, 404, google_error(404, "videoNotFound"))
        if method == "captions" and ((resource or {}).get("snippet") or {}).get("videoId") not in self.videos:
            return self._send(h, 404, google_error(404, "videoNotFound"))
        upload_id = "SESSION-" + secrets.token_hex(12)
        with self.lock:
            self.upload_ids.append(upload_id)
            self.sessions[upload_id] = {"method": method, "query": query, "resource": resource,
                                        "size": int(headers["x-upload-content-length"]),
                                        "mime": headers.get("x-upload-content-type"), "data": bytearray()}
        path = {"videos": "videos", "thumbnails": "thumbnails/set", "captions": "captions"}[method]
        return self._send(h, 200, b"", {"Location": f"{self.url}/upload/youtube/v3/{path}?uploadType=resumable&upload_id={upload_id}"})

    def _put(self, h: BaseHTTPRequestHandler, query: Dict[str, str], headers: Dict[str, str], length: int, fault: Any) -> None:
        with self.lock:
            s = self.sessions.get(query["upload_id"])
        if s is None:
            h.rfile.read(length)
            return self._send(h, 404, google_error(404, "notFound"))
        rng = headers.get("content-range", "")
        if rng.startswith("bytes */"):                               # the status of the session
            return self._progress(h, s)
        try:
            span, total = rng[len("bytes "):].split("/")
            first, last = (int(x) for x in span.split("-"))
        except ValueError:
            h.rfile.read(length)
            return self._send(h, 400, google_error(400, "badRequest", "bad Content-Range"))
        if int(total) != s["size"] or first != len(s["data"]) or last - first + 1 != length:
            h.rfile.read(length)
            return self._send(h, 400, google_error(400, "badRequest", f"non-contiguous chunk {rng}, have {len(s['data'])}"))
        if last + 1 < s["size"] and length % UNIT:
            h.rfile.read(length)
            return self._send(h, 400, google_error(400, "badRequest", "chunk size is not a multiple of 256 KiB"))
        if fault is not None and fault[0] == "interrupt":            # keep part of the chunk, then drop the connection
            kept = h.rfile.read(fault[1])
            with self.lock:
                s["data"] += kept
            return self._drop(h)
        if not self._authorized(headers):
            h.rfile.read(length)
            return self._send(h, 401, google_error(401, "authError"))
        data = h.rfile.read(length)
        with self.lock:
            s["data"] += data
        return self._progress(h, s)

    def _progress(self, h: BaseHTTPRequestHandler, s: Dict[str, Any]) -> None:
        if len(s["data"]) < s["size"]:
            extra = {"Range": f"bytes=0-{len(s['data']) - 1}"} if s["data"] else {}
            return self._send(h, 308, b"", extra)
        if "done" not in s:
            s["done"] = self._finish(s)
        code = 201 if s["method"] == "videos" else 200
        return self._send(h, code, json.dumps(s["done"]).encode())

    def _finish(self, s: Dict[str, Any]) -> Dict[str, Any]:
        data = bytes(s["data"])
        with self.lock:
            if s["method"] == "videos":
                vid = "vid" + secrets.token_hex(4)
                sent = s["resource"]
                status = dict(sent.get("status") or {})
                if self.report_privacy:
                    status["privacyStatus"] = self.report_privacy
                self.videos[vid] = {"resource": sent, "data": data, "query": s["query"], "mime": s["mime"]}
                return {"kind": "youtube#video", "id": vid, "snippet": sent.get("snippet", {}), "status": status}
            if s["method"] == "thumbnails":
                self.thumbnails[s["query"]["videoId"]] = {"data": data, "mime": s["mime"]}
                return {"kind": "youtube#thumbnailSetResponse", "items": [{"default": {"url": "https://i.ytimg.com/x"}}]}
            cid = "cap" + secrets.token_hex(4)
            self.captions.append({"id": cid, "resource": s["resource"], "data": data, "query": s["query"], "mime": s["mime"]})
            return {"kind": "youtube#caption", "id": cid, "snippet": s["resource"]["snippet"]}

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, code: int, body: bytes, headers: Optional[Dict[str, str]] = None) -> None:
        try:
            h.send_response(code)
            for k, v in (headers or {}).items():
                h.send_header(k, v)
            h.send_header("Content-Type", "application/json; charset=UTF-8")
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
