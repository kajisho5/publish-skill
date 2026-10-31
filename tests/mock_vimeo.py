"""An in-process mock of the Vimeo API's video creation and its tus 1.0.0 resumable upload, bound to 127.0.0.1 on a
free port (roadmap step 5, ADR-005). Nothing here is part of the package, and nothing here contacts Vimeo:
helpers.OfflineTestCase lets a test connect only to the loopback ports of mock servers it started.

What it implements, as Vimeo's upload guide and the tus protocol describe it:
  POST /me/videos                    Authorization: bearer <token>, Accept: application/vnd.vimeo.*+json;version=3.4,
                                     Content-Type: application/json, body {"upload": {"approach": "tus", "size"}, ...}
                                     -> 200 (or `create_status`) and the video: uri, link, name, description, privacy,
                                     upload.approach "tus", upload.upload_link (on this server, /tus/<id>)
  PATCH /tus/<id>                    Tus-Resumable: 1.0.0 (else 412), Content-Type: application/offset+octet-stream
                                     (else 415), Upload-Offset equal to the bytes held (else 409)
                                     -> 204, Upload-Offset: the new offset
  HEAD /tus/<id>                     Tus-Resumable: 1.0.0 -> 200, Upload-Offset, Upload-Length, Cache-Control: no-store
  an unknown or expired upload id    -> 404

Faults are scripted per request kind ("create", "patch", "head"): `faults[kind]` is a list consumed one entry per
request, each one of
  None                                  answer normally
  ("status", code, error_code, headers) answer with that status and a Vimeo error body (error_code may be None)
  ("interrupt", keep)                   (patch) keep the first `keep` bytes of the body, then drop the connection
  ("stall", seconds)                    wait, then drop the connection without answering (the client times out)
  ("expire",)                           (patch/head) the upload is gone: 404
  ("call", fn)                          run fn() first (e.g. edit the file mid-upload), then answer normally"""
from __future__ import annotations

import json
import secrets
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Set
from urllib.parse import urlsplit

ACCEPT = "application/vnd.vimeo.*+json;version=3.4"
TUS = "1.0.0"
TUS_TYPE = "application/offset+octet-stream"


def vimeo_error(code: Optional[int], message: str = "") -> bytes:
    doc: Dict[str, Any] = {"error": message or "error", "developer_message": message or "error", "link": None}
    if code is not None:
        doc["error_code"] = code
    return json.dumps(doc).encode()


class MockVimeo:
    def __init__(self, access_token: str, create_status: int = 200, report_privacy: Optional[str] = None,
                 upload_link_base: Optional[str] = None) -> None:
        self.token = access_token
        self.create_status = create_status
        self.report_privacy = report_privacy          # answer with this privacy.view instead of the one sent
        self.upload_link_base = upload_link_base      # hand out upload links on another base (a refused host)
        self.faults: Dict[str, List[Any]] = {}
        self.uploads: Dict[str, Dict[str, Any]] = {}  # upload id -> {"size", "data", "video", "body"}
        self.upload_links: List[str] = []
        self.requests: List[Dict[str, Any]] = []      # every request: kind, method, path, headers, body length
        self.videos: Dict[str, Dict[str, Any]] = {}   # completed: video id -> {"body", "data"}
        self.created: List[Dict[str, Any]] = []       # every create body (placeholders included)
        self.lock = threading.Lock()
        self.server: Optional[ThreadingHTTPServer] = None

    # ---- lifecycle -------------------------------------------------------------------------------------------
    def start(self, allowed_ports: Set[int]) -> "MockVimeo":
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_a: Any) -> None:
                pass

            def do_POST(self) -> None:
                mock._handle(self, "POST")

            def do_PATCH(self) -> None:
                mock._handle(self, "PATCH")

            def do_HEAD(self) -> None:
                mock._handle(self, "HEAD")

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

    def offsets(self) -> List[str]:
        """Upload-Offset of each PATCH, and "HEAD" for each status query, in order."""
        return [r["headers"].get("upload-offset", "") if r["kind"] == "patch" else "HEAD"
                for r in self.requests if r["kind"] in ("patch", "head")]

    def _fault(self, kind: str) -> Any:
        with self.lock:
            queue = self.faults.get(kind)
            return queue.pop(0) if queue else None

    # ---- the protocol ----------------------------------------------------------------------------------------
    def _handle(self, h: BaseHTTPRequestHandler, method: str) -> None:
        path = urlsplit(h.path).path
        headers = {k.lower(): v for k, v in h.headers.items()}
        length = int(headers.get("content-length") or 0)
        if method == "POST" and path == "/me/videos":
            kind = "create"
        elif method == "PATCH" and path.startswith("/tus/"):
            kind = "patch"
        elif method == "HEAD" and path.startswith("/tus/"):
            kind = "head"
        else:
            kind = "unknown"
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
                return self._send(h, fault[1], vimeo_error(fault[2]), extra, head=method == "HEAD")
            elif fault[0] == "expire" and kind in ("patch", "head"):
                h.rfile.read(length)
                with self.lock:
                    self.uploads.pop(path[len("/tus/"):], None)
                return self._send(h, 404, b"", head=method == "HEAD")
        if kind == "create":
            return self._create(h, headers, h.rfile.read(length))
        if kind in ("patch", "head"):
            return self._tus(h, kind, path[len("/tus/"):], headers, length, fault)
        h.rfile.read(length)
        return self._send(h, 404, vimeo_error(None, "unknown endpoint"), head=method == "HEAD")

    def _create(self, h: BaseHTTPRequestHandler, headers: Dict[str, str], raw: bytes) -> None:
        if headers.get("authorization") != "bearer " + self.token:
            return self._send(h, 401, vimeo_error(8003, "the access token is missing or not recognised"))
        if headers.get("accept") != ACCEPT or headers.get("content-type") != "application/json":
            return self._send(h, 400, vimeo_error(2205, "headers"))
        try:
            body = json.loads(raw.decode("utf-8"))
        except ValueError:
            return self._send(h, 400, vimeo_error(2205, "the body isn't formatted properly"))
        up = body.get("upload") or {}
        if up.get("approach") != "tus" or not str(up.get("size", "")).isdigit():
            return self._send(h, 400, vimeo_error(2230, "the upload type is invalid"))
        vid = str(100000000 + len(self.created))
        upload_id = secrets.token_hex(12)
        base = self.upload_link_base or self.url
        link = f"{base}/tus/{upload_id}"
        privacy = dict(body.get("privacy") or {})
        if self.report_privacy:
            privacy["view"] = self.report_privacy
        doc = {"uri": f"/videos/{vid}", "link": f"https://vimeo.com/{vid}", "name": body.get("name", "Untitled"),
               "description": body.get("description"), "privacy": privacy,
               "upload": {"status": "in_progress", "approach": "tus", "upload_link": link, "size": int(up["size"])}}
        with self.lock:
            self.created.append(body)
            self.upload_links.append(link)
            self.uploads[upload_id] = {"size": int(up["size"]), "data": bytearray(), "video": vid, "body": body}
        return self._send(h, self.create_status, json.dumps(doc).encode())

    def _tus(self, h: BaseHTTPRequestHandler, kind: str, upload_id: str, headers: Dict[str, str], length: int,
             fault: Any) -> None:
        head = kind == "head"
        with self.lock:
            u = self.uploads.get(upload_id)
        if u is None:
            h.rfile.read(length)
            return self._send(h, 404, b"", head=head)
        if headers.get("tus-resumable") != TUS:
            h.rfile.read(length)
            return self._send(h, 412, b"", {"Tus-Version": TUS}, head=head)
        if head:
            return self._send(h, 200, b"", {"Upload-Offset": str(len(u["data"])), "Upload-Length": str(u["size"]),
                                            "Cache-Control": "no-store"}, head=True)
        if headers.get("content-type") != TUS_TYPE:
            h.rfile.read(length)
            return self._send(h, 415, b"")
        if headers.get("upload-offset") != str(len(u["data"])):
            h.rfile.read(length)
            return self._send(h, 409, b"", {"Upload-Offset": str(len(u["data"]))})
        if len(u["data"]) + length > u["size"]:
            h.rfile.read(length)
            return self._send(h, 400, vimeo_error(None, "more bytes than upload.size"))
        if fault is not None and fault[0] == "interrupt":            # keep part of the body, then drop the connection
            kept = h.rfile.read(fault[1])
            with self.lock:
                u["data"] += kept
            return self._drop(h)
        data = h.rfile.read(length)
        with self.lock:
            u["data"] += data
            if len(u["data"]) == u["size"]:
                self.videos[u["video"]] = {"body": u["body"], "data": bytes(u["data"])}
        return self._send(h, 204, b"", {"Upload-Offset": str(len(u["data"]))})

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, code: int, body: bytes, headers: Optional[Dict[str, str]] = None,
              head: bool = False) -> None:
        try:
            h.send_response(code)
            h.send_header("Tus-Resumable", TUS)
            for k, v in (headers or {}).items():
                h.send_header(k, v)
            if body:
                h.send_header("Content-Type", "application/vnd.vimeo.video+json")
            h.send_header("Content-Length", "0" if head or code == 204 else str(len(body)))
            h.end_headers()
            if not head and code != 204:
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
