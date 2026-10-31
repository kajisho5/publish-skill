"""The Vimeo uploader (roadmap step 5, ADR-005), entirely against tests/mock_vimeo.py: an in-process mock of the Vimeo
API's video creation and its tus 1.0.0 resumable upload on 127.0.0.1. Chunked upload, resume after an interrupted
chunk, a timeout, a 409 offset mismatch, backoff on 5xx and 429, the account's quota and storage errors, an expired
upload link, a file edited mid-upload, and that exactly the metadata the confirmation token bound is what arrives. No
test contacts Vimeo: the socket guard lets a test reach only the loopback port of the mock it started, and the guard
tests below assert that it blocks every other host."""
from __future__ import annotations

import hashlib
import json
import os
import socket
from unittest import mock

from helpers import LOOPBACK_PORTS, NetworkAttempted, OfflineTestCase, run_cli, run_cli_raw, vimeo_mock

from publish_skill import credentials as creds
from publish_skill import limits as L
from publish_skill.errors import PublishError
from publish_skill.metadata import Metadata, build
from publish_skill.plan import metadata_checks, plan
from publish_skill.platforms import UploadRequest, get_platform, vimeo
from publish_skill.upload import confirm_token, upload

TOKEN_VAR = "PUBLISH_VIMEO_ACCESS_TOKEN"
TOKEN = "vimeo-pat-3f9a0c71b2e84d65a1c0ffee"
UNIT = 256 * 1024
SRT = b"1\n00:00:00,000 --> 00:00:01,000\nhi\n"
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


class VimeoTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.video = os.path.join(self.tmp, "final.mp4")         # 2.5 chunks of 256 KiB
        with open(self.video, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + os.urandom(2 * UNIT + UNIT // 2 - 12))
        self.size = os.path.getsize(self.video)
        p = mock.patch.dict(os.environ, {TOKEN_VAR: TOKEN})
        p.start()
        self.addCleanup(p.stop)

    def file(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def mock(self, **kw):
        self.m = vimeo_mock(self, TOKEN, **kw)
        return self.m

    def confirmed(self, *flags, human=False):
        """Dry run, then the confirmed upload with the same flags; (exit code, document or None, stdout + stderr)."""
        rc, dry = run_cli("upload", self.video, "--platform", "vimeo", *flags, "--json")
        self.assertEqual(rc, 0, dry)
        argv = ["upload", self.video, "--platform", "vimeo", *flags, "--confirm", dry["confirm_token"]]
        rc, out, err = run_cli_raw(*(argv if human else argv + ["--json"]))
        return rc, (None if human else json.loads(out)), out + err

    def assert_nothing_secret(self, text):
        self.assertNotIn(TOKEN, text)
        for link in self.m.upload_links:
            self.assertNotIn(link, text)
        self.assertNotIn("/tus/", text)

    def data(self):
        with open(self.video, "rb") as fh:
            return fh.read()

    def offsets(self, *ns):
        return [str(n) if n != "HEAD" else "HEAD" for n in ns]


class VimeoLoopbackOnlyTests(VimeoTestCase):
    def test_the_guard_blocks_every_non_loopback_host_for_vimeo(self):
        m = self.mock()
        self.assertEqual(LOOPBACK_PORTS, {m.port})
        s = socket.create_connection(("127.0.0.1", m.port))           # the mock: allowed
        s.close()
        for address in (("api.vimeo.com", 443), ("vimeo.com", 443), ("files.tus.vimeo.com", 443),
                        ("developer.vimeo.com", 443), ("151.101.0.217", 443), ("8.8.8.8", 53),
                        ("127.0.0.1", m.port + 1), ("::1", m.port), ("0.0.0.0", m.port)):
            with self.assertRaises(NetworkAttempted, msg=address):
                socket.create_connection(address)
        for host in ("api.vimeo.com", "localhost"):                    # no name lookup either
            with self.assertRaises(NetworkAttempted):
                socket.getaddrinfo(host, m.port)
        raw = socket.socket()
        self.addCleanup(raw.close)
        with self.assertRaises(NetworkAttempted):
            raw.connect(("151.101.0.217", 443))

    def test_the_real_endpoint_is_vimeos_and_unreachable_from_a_test(self):
        self.assertEqual(vimeo.API_BASE, "https://api.vimeo.com")
        self.assertEqual(get_platform("vimeo").api_base, vimeo.API_BASE)
        req = UploadRequest("vimeo", self.video, self.size, hashlib.sha256(self.data()).hexdigest(), build().to_dict())
        with self.assertRaises(NetworkAttempted) as e:                # no mock: the first request is stopped
            vimeo.VimeoUploader(sleep=lambda s: None).upload(req, creds.Credentials("vimeo", {TOKEN_VAR: TOKEN}))
        self.assertIn("api.vimeo.com", str(e.exception))

    def test_every_request_of_an_upload_went_to_the_mock(self):
        m = self.mock()
        rc, doc, _ = self.confirmed("--title", "Keynote")
        self.assertEqual(rc, 0, doc)
        self.assertNotIn("unknown", m.kinds())
        self.assertEqual(LOOPBACK_PORTS, {m.port})

    def test_the_endpoint_must_be_https_or_loopback(self):
        req = UploadRequest("vimeo", self.video, self.size, "0" * 64, build().to_dict())
        for url in ("http://api.vimeo.com", "ftp://127.0.0.1"):
            up = vimeo.VimeoUploader()
            up.api_base = url
            with self.assertRaises(PublishError) as e:
                up.upload(req, creds.Credentials("vimeo", {TOKEN_VAR: TOKEN}))
            self.assertEqual(e.exception.code, "INTERNAL_ERROR")

    def test_the_upload_link_must_be_https_on_vimeo(self):
        up = vimeo.VimeoUploader()
        for bad in ("https://evil.example/files/x", "http://files.tus.vimeo.com/x", "https://vimeo.com.evil.example/x",
                    "https://notvimeo.com/x", "http://127.0.0.1:9/tus/x"):
            with self.assertRaises(PublishError, msg=bad) as e:
                up._check_upload_link(bad)
            self.assertEqual(e.exception.code, "UPLOAD_REJECTED")
        up._check_upload_link("https://us-files.tus.vimeo.com/files/vimeo-prod-src-tus-us/abc")

    def test_an_upload_link_off_the_mock_is_refused_before_any_byte(self):
        m = self.mock(upload_link_base="http://127.0.0.1:9")            # another loopback port: refused too
        rc, doc, text = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"))
        self.assertEqual(m.kinds(), ["create"])
        self.assertEqual(m.videos, {})


class VimeoTusProtocolTests(VimeoTestCase):
    def test_vimeo_chunked_upload_sends_exactly_the_file(self):
        m = self.mock()
        rc, doc, text = self.confirmed("--title", "Keynote")
        self.assertEqual(rc, 0, doc)
        r = doc["result"]
        self.assertEqual(m.videos[r["video_id"]]["data"], self.data())
        self.assertEqual(m.kinds(), ["create", "patch", "patch", "patch"])
        self.assertEqual(m.offsets(), self.offsets(0, UNIT, 2 * UNIT))
        create = m.requests[0]["headers"]
        self.assertEqual((create["authorization"], create["accept"], create["content-type"]),
                         ("bearer " + TOKEN, "application/vnd.vimeo.*+json;version=3.4", "application/json"))
        for req in m.requests[1:]:                                   # the token goes to the API only, never the link
            self.assertNotIn("authorization", req["headers"])
            self.assertEqual(req["headers"]["tus-resumable"], "1.0.0")
            self.assertEqual(req["headers"]["content-type"], "application/offset+octet-stream")
        self.assertEqual((r["uri"], r["url"]), ("/videos/" + r["video_id"], "https://vimeo.com/" + r["video_id"]))
        self.assertEqual((r["privacy"], r["privacy_view"]), ("private", "nobody"))
        self.assertEqual((r["transfer"]["bytes_sent"], r["transfer"]["retries"], r["transfer"]["resumed"]), (self.size, 0, 0))
        self.assert_nothing_secret(text)

    def test_vimeo_resume_after_an_interrupted_chunk(self):
        m = self.mock()
        m.faults["patch"] = [None, ("interrupt", 100000)]        # the second chunk: 100000 bytes arrive, then the link drops
        rc, doc, text = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[doc["result"]["video_id"]]["data"], self.data())
        resume_at = UNIT + 100000
        self.assertEqual(m.offsets(), self.offsets(0, UNIT, "HEAD", resume_at, resume_at + UNIT))   # HEAD, then resume
        self.assertEqual(m.requests[3]["headers"]["accept"], "application/vnd.vimeo.*+json;version=3.4")
        t = doc["result"]["transfer"]
        self.assertEqual((t["retries"], t["resumed"]), (1, 1))
        self.assertEqual(self.waits, [1.0])                     # backoff: 0.5 (the test's random) * 2**1
        self.assertEqual(len(m.created), 1)                     # the same video: no second create
        self.assert_nothing_secret(text)

    def test_resume_when_nothing_of_the_chunk_arrived(self):
        m = self.mock()
        m.faults["patch"] = [("interrupt", 0)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[doc["result"]["video_id"]]["data"], self.data())
        self.assertEqual(m.offsets()[:3], self.offsets(0, "HEAD", 0))

    def test_a_timeout_is_an_interruption(self):
        m = self.mock()
        m.faults["patch"] = [None, ("stall", 1.5)]
        with mock.patch.object(vimeo.VimeoUploader, "timeout_s", 0.3):
            rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[doc["result"]["video_id"]]["data"], self.data())
        self.assertIn("HEAD", m.offsets())
        self.assertEqual(doc["result"]["transfer"]["resumed"], 1)

    def test_an_offset_conflict_resyncs_with_head(self):
        m = self.mock()
        m.faults["patch"] = [None, ("status", 409, None)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[doc["result"]["video_id"]]["data"], self.data())
        self.assertEqual(m.offsets(), self.offsets(0, UNIT, "HEAD", UNIT, 2 * UNIT))

    def test_5xx_and_429_are_retried_with_exponential_backoff_and_retry_after(self):
        m = self.mock()
        m.faults["create"] = [("status", 503, None), ("status", 429, 9000, {"Retry-After": "7"})]
        m.faults["patch"] = [("status", 502, None)]
        m.faults["head"] = [("status", 500, None)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[doc["result"]["video_id"]]["data"], self.data())
        # create: retry 1 -> 1.0, retry 2 -> max(2.0, Retry-After 7); patch: retry 1 -> 1.0; head: retry 1 -> 1.0
        self.assertEqual(self.waits, [1.0, 7.0, 1.0, 1.0])
        self.assertEqual(m.kinds(), ["create", "create", "create", "patch", "head", "head", "patch", "patch", "patch"])
        self.assertEqual(len(m.created), 1)

    def test_backoff_is_capped(self):
        up = vimeo.VimeoUploader(sleep=lambda s: None, rand=lambda: 1.0)
        up._stats = {"retries": 0, "waited_s": 0.0}
        waits = []
        up._sleep_fn = waits.append
        for attempt in (1, 2, 6, 7, 10):
            up._backoff(attempt, None)
        self.assertEqual(waits, [2.0, 4.0, 64.0, 64.0, 64.0])
        up._backoff(1, vimeo._Response(429, {"retry-after": "100000"}, b""))
        self.assertEqual(waits[-1], 300.0)

    def test_retries_run_out_as_a_typed_retryable_error_and_nothing_is_completed(self):
        m = self.mock()
        m.faults["patch"] = [("status", 503, None)] * 50
        with mock.patch.object(vimeo.VimeoUploader, "max_retries", 3):
            rc, doc, text = self.confirmed()
        err = doc["error"]
        self.assertEqual((rc, err["code"], err["retryable"]), (4, "UPLOAD_INTERRUPTED", True))
        self.assertFalse(err["details"]["uploaded"])
        self.assertEqual(err["details"]["last"]["status"], 503)
        self.assertTrue(err["details"]["placeholder"])
        self.assertEqual(err["details"]["video_uri"], "/videos/" + list(m.uploads.values())[0]["video"])
        self.assertIn("placeholder", err["message"])
        self.assertEqual(m.videos, {})
        self.assertEqual(len(self.waits), 3)
        self.assert_nothing_secret(text)

    def test_a_create_without_an_answer_is_not_retried(self):
        m = self.mock()
        m.faults["create"] = [("stall", 0.6)]
        with mock.patch.object(vimeo.VimeoUploader, "timeout_s", 0.2):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_INTERRUPTED"))
        self.assertEqual(m.kinds(), ["create"])                 # a second POST could add a second placeholder
        self.assertEqual(self.waits, [])

    def test_an_expired_upload_link_is_interrupted_not_restarted(self):
        m = self.mock()
        m.faults["patch"] = [None, ("expire",)]
        rc, doc, text = self.confirmed()
        err = doc["error"]
        self.assertEqual((rc, err["code"], err["details"]["status"]), (4, "UPLOAD_INTERRUPTED", 404))
        self.assertEqual(m.kinds(), ["create", "patch", "patch"])
        self.assertEqual(m.videos, {})
        self.assert_nothing_secret(text)

    def test_a_file_edited_during_the_upload_is_never_completed(self):
        m = self.mock()

        def edit():                                             # same size, one byte changed in the last chunk
            with open(self.video, "r+b") as fh:
                fh.seek(self.size - 1)
                old = fh.read(1)
                fh.seek(self.size - 1)
                fh.write(b"\x01" if old != b"\x01" else b"\x02")

        m.faults["patch"] = [("call", edit)]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"))
        self.assertFalse(doc["error"]["details"]["uploaded"])
        self.assertEqual(m.videos, {})
        self.assertEqual(m.offsets(), self.offsets(0, UNIT))   # the last chunk was never sent

    def test_a_server_that_keeps_none_of_a_chunk_is_retried_boundedly(self):
        m = self.mock()
        m.faults["patch"] = [("interrupt", 0)] * 20
        with mock.patch.object(vimeo.VimeoUploader, "max_retries", 2):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_INTERRUPTED"))
        self.assertEqual(m.videos, {})

    def test_201_from_create_is_accepted_too(self):
        m = self.mock(create_status=201)                        # the reference says 201, the guide 200
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[doc["result"]["video_id"]]["data"], self.data())

    def test_offset_parsing_and_protocol_errors(self):
        up = vimeo.VimeoUploader()
        self.assertEqual(up._offset(vimeo._Response(204, {"upload-offset": "1000"}, b""), "patch"), 1000)
        for bad in ({}, {"upload-offset": "-1"}, {"upload-offset": "x"}):
            with self.assertRaises(PublishError) as e:
                up._offset(vimeo._Response(204, bad, b""), "patch")
            self.assertEqual(e.exception.code, "UPLOAD_REJECTED")
        with self.assertRaises(PublishError) as e:              # more than was sent, or less than was confirmed
            up._advance(10, b"abc", hashlib.sha256(), 14)
        self.assertEqual(e.exception.code, "UPLOAD_REJECTED")
        with self.assertRaises(PublishError):
            up._advance(10, b"abc", hashlib.sha256(), 9)


class VimeoAccountErrorTests(VimeoTestCase):
    def test_vimeo_quota_is_typed_and_not_retried(self):
        for code, period in ((4102, "weekly"), (4104, "daily")):
            m = self.mock()
            m.faults["create"] = [("status", 403, code)]
            rc, doc, text = self.confirmed()
            err = doc["error"]
            self.assertEqual((rc, err["code"], err["retryable"]), (4, "UPLOAD_LIMIT_EXCEEDED", True))
            self.assertEqual((err["details"]["status"], err["details"]["error_code"]), (403, code))
            self.assertIn(period, err["message"])
            self.assertFalse(err["details"]["uploaded"])
            self.assertEqual(m.kinds(), ["create"])               # no retry, no byte of the video sent
            self.assertEqual(self.waits, [])
            self.assert_nothing_secret(text)

    def test_full_storage_is_rejected_not_retried(self):
        m = self.mock()
        m.faults["create"] = [("status", 403, 4101)]
        rc, doc, _ = self.confirmed()
        err = doc["error"]
        self.assertEqual((rc, err["code"], err["retryable"]), (4, "UPLOAD_REJECTED", False))
        self.assertIn("storage is full", err["message"])
        self.assertEqual(m.kinds(), ["create"])

    def test_the_rate_limit_is_retried_then_interrupted(self):
        m = self.mock()
        m.faults["create"] = [("status", 429, 9000)] * 10
        with mock.patch.object(vimeo.VimeoUploader, "max_retries", 2):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_INTERRUPTED"))
        self.assertEqual(doc["error"]["details"]["last"], {"status": 429, "error_code": 9000})
        self.assertEqual(m.kinds(), ["create"] * 3)

    def test_a_refused_access_token(self):
        m = self.mock()
        with mock.patch.dict(os.environ, {TOKEN_VAR: "vimeo-pat-revoked-0000"}):
            rc, doc, text = self.confirmed()
        err = doc["error"]
        self.assertEqual((rc, err["code"], err["retryable"], err["details"]["error_code"]), (3, "AUTH_FAILED", False, 8003))
        self.assertEqual(m.kinds(), ["create"])
        self.assertNotIn("vimeo-pat-revoked-0000", text)
        self.assert_nothing_secret(text)

    def test_another_4xx_is_rejected_without_retry(self):
        m = self.mock()
        m.faults["create"] = [("status", 400, 2204)]
        rc, doc, _ = self.confirmed("--title", "x")
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_REJECTED", False))
        self.assertEqual(doc["error"]["details"]["error_code"], 2204)
        self.assertEqual(m.kinds(), ["create"])


class VimeoBoundMetadataTests(VimeoTestCase):
    def test_privacy_defaults_to_nobody_and_nothing_unbound_is_sent(self):
        m = self.mock()
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.created, [{"upload": {"approach": "tus", "size": str(self.size)}, "privacy": {"view": "nobody"}}])
        self.assertEqual(doc["result"]["sent"], m.created[0])

    def test_each_privacy_maps_to_one_privacy_view(self):
        for privacy, view in (("private", "nobody"), ("unlisted", "unlisted"), ("public", "anybody")):
            m = self.mock()
            rc, doc, _ = self.confirmed("--privacy", privacy)
            self.assertEqual(rc, 0, doc)
            self.assertEqual(m.created[0]["privacy"], {"view": view})
            self.assertEqual((doc["result"]["privacy"], doc["result"]["privacy_view"]), (privacy, view))
        self.assertEqual(L.limits_of("vimeo")["privacy.values"]["maps_to"], vimeo.PRIVACY_VIEW)

    def test_the_received_upload_reproduces_the_token(self):
        # The bytes and the create body that arrived at the mock, rebuilt into Metadata, give the very token the human
        # confirmed.
        m = self.mock()
        flags = ("--title", "Keynote 基調講演", "--description", "day 1\nhall A", "--privacy", "unlisted")
        rc, dry = run_cli("upload", self.video, "--platform", "vimeo", *flags, "--json")
        rc, out, _ = run_cli_raw("upload", self.video, "--platform", "vimeo", *flags, "--confirm", dry["confirm_token"],
                                 "--json")
        doc = json.loads(out)
        self.assertEqual(rc, 0, doc)
        got = m.videos[doc["result"]["video_id"]]
        body = got["body"]
        self.assertEqual(body, {"upload": {"approach": "tus", "size": str(self.size)}, "name": "Keynote 基調講演",
                                "description": "day 1\nhall A", "privacy": {"view": "unlisted"}})
        reverse = {v: k for k, v in vimeo.PRIVACY_VIEW.items()}
        rebuilt = Metadata(title=body["name"], description=body["description"], privacy=reverse[body["privacy"]["view"]])
        self.assertEqual(confirm_token("vimeo", hashlib.sha256(got["data"]).hexdigest(), len(got["data"]), rebuilt),
                         dry["confirm_token"])
        self.assertEqual(doc["confirm_token"], dry["confirm_token"])

    def test_the_token_binds_the_platform(self):
        self.assertNotEqual(confirm_token("vimeo", "a" * 64, 10), confirm_token("youtube", "a" * 64, 10))
        token = upload(self.video, "youtube")["confirm_token"]
        rc, doc = run_cli("upload", self.video, "--platform", "vimeo", "--confirm", token, "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"))

    def test_tags_thumbnail_and_captions_are_refused_before_a_token(self):
        m = self.mock()
        for flags, field in ((("--tag", "keynote"), "tags"), (("--thumbnail", self.file("t.png", PNG)), "thumbnail"),
                             (("--caption", self.file("en.srt", SRT), "--caption-language", "en"), "captions")):
            with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
                rc, doc = run_cli("upload", self.video, "--platform", "vimeo", *flags, "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "METADATA_INVALID"), flags)
            self.assertNotIn("confirm_token", json.dumps(doc))
            self.assertEqual([r["check"] for r in doc["error"]["details"]["failed"]], [f"{field} (not sent to Vimeo)"])
        self.assertEqual(m.requests, [])

    def test_a_privacy_the_platform_changed_is_reported(self):
        self.mock(report_privacy="anybody")
        rc, doc, _ = self.confirmed("--privacy", "private")
        self.assertEqual((rc, doc["result"]["privacy"]), (0, "public"))
        self.assertTrue(any("'nobody'" in w and "'anybody'" in w for w in doc["result"]["warnings"]))

    def test_create_body_is_the_bound_metadata_only(self):
        self.assertEqual(vimeo.create_body(Metadata().to_dict(), 5),
                         {"upload": {"approach": "tus", "size": "5"}, "privacy": {"view": "nobody"}})
        self.assertEqual(vimeo.create_body(Metadata(title="", privacy="public").to_dict(), 5),
                         {"upload": {"approach": "tus", "size": "5"}, "name": "", "privacy": {"view": "anybody"}})
        for md in (Metadata(privacy="everyone"), Metadata(tags=["x"])):
            with self.assertRaises(PublishError):
                vimeo.create_body(md.to_dict(), 5)


class VimeoGateAndRedactionTests(VimeoTestCase):
    def test_every_refusal_happens_before_any_request_or_credential(self):
        m = self.mock()
        token = upload(self.video, "vimeo")["confirm_token"]
        bad = [("upload", self.video, "--platform", "vimeo", "--confirm", token + "0"),
               ("upload", self.video, "--platform", "vimeo", "--confirm", token, "--title", "edited"),
               ("upload", self.video, "--platform", "vimeo", "--confirm", token, "--title", "x" * 129),
               ("upload", self.video, "--platform", "vimeo", "--confirm", token, "--description", "y" * 5001),
               ("upload", self.video, "--platform", "vimeo", "--confirm", token, "--tag", "t"),
               ("upload", self.video, "--platform", "vimeo", "--confirm", token, "--privacy", "nobody"),
               ("upload", self.video, "--platform", "vimeo"),                         # a dry run
               ("upload", self.file("empty.mp4", b""), "--platform", "vimeo", "--confirm",
                confirm_token("vimeo", hashlib.sha256(b"").hexdigest(), 0))]
        with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
            for argv in bad:
                rc, out, _ = run_cli_raw(*argv, "--json")
                doc = json.loads(out)
                self.assertTrue(doc.get("dry_run") or rc == 2, (argv, doc))
        self.assertEqual(m.requests, [])

    def test_the_token_from_the_keychain_uploads_and_is_never_printed(self):
        os.environ.pop(TOKEN_VAR)
        self.keychain_backend.items[("publish-skill", TOKEN_VAR)] = TOKEN
        m = self.mock()
        rc, doc, text = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.requests[0]["headers"]["authorization"], "bearer " + TOKEN)
        self.assert_nothing_secret(text)

    def test_receipts_and_errors_never_carry_the_token_or_the_upload_link(self):
        scenarios = [{}, {"patch": [None, ("interrupt", 1000)]}, {"create": [("status", 403, 4102)]},
                     {"patch": [("status", 503, None)] * 30}, {"patch": [None, ("expire",)]}]
        for faults in scenarios:
            for human in (False, True):
                m = self.mock()
                m.faults.update({k: list(v) for k, v in faults.items()})
                with mock.patch.object(vimeo.VimeoUploader, "max_retries", 2):
                    # the token is also planted in the title, where a careless uploader might echo it
                    rc, _, text = self.confirmed("--title", TOKEN, human=human)
                self.assert_nothing_secret(text)

    def test_human_receipt(self):
        self.mock()
        rc, _, text = self.confirmed("--title", "Keynote", human=True)
        self.assertEqual(rc, 0, text)
        self.assertIn("(privacy private) https://vimeo.com/", text)
        self.assert_nothing_secret(text)


class VimeoPlanTests(VimeoTestCase):
    def test_metadata_rows_use_vimeos_limits(self):
        rows = {r["check"]: r for r in metadata_checks("vimeo", build(title="x" * 129, description="é" * 5000))}
        self.assertEqual((rows["title length"]["status"], rows["title length"]["limit"]), ("FAIL", 128))
        self.assertEqual(rows["title length"]["source"], "https://developer.vimeo.com/api/reference/videos#edit_video")
        # Vimeo counts characters, not bytes: 5000 two-byte characters pass
        self.assertEqual((rows["description length"]["status"], rows["description length"]["value"]), ("PASS", 5000))
        self.assertEqual(rows["description length"]["limit_key"], "description.max_chars")
        self.assertNotIn("title characters", rows)                     # no forbidden characters are stated
        self.assertEqual(rows["privacy"]["status"], "PASS")
        rows = {r["check"]: r for r in metadata_checks("vimeo", build(description="é" * 5001, privacy="anybody"))}
        self.assertEqual(rows["description length"]["status"], "FAIL")
        self.assertEqual(rows["privacy"]["status"], "FAIL")            # the skill's names, not privacy.view's

    def test_plan_is_incomplete_without_a_delivery_spec(self):
        doc = plan(self.video, "vimeo", build(title="Keynote"), ffmpeg_skill_dir=os.path.join(self.tmp, "none"))
        self.assertEqual(doc["verdict"], "INCOMPLETE")                  # never PASS without ffmpeg-skill's spec
        spec = [r for r in doc["checks"] if r["check"] == "delivery spec"][0]
        self.assertEqual((spec["status"], spec["owner"]), ("UNCHECKED", "ffmpeg-skill"))
        self.assertIn("no vimeo delivery spec", spec["fix"])
        self.assertEqual(doc["ffmpeg_skill"]["status"], "NO_SPEC")
        size = [r for r in doc["checks"] if r["check"] == "file size"][0]
        self.assertEqual((size["status"], size["limit"]), ("PASS", 322122547200))
        self.assertFalse(doc["network_used"])

    def test_plan_fails_an_over_long_title_offline(self):
        rc, doc = run_cli("plan", self.video, "--platform", "vimeo", "--title", "x" * 129, "--json")
        self.assertEqual((rc, doc["verdict"]), (1, "FAIL"))
        self.assertFalse(doc["uploaded"])


class VimeoLimitsTableTests(OfflineTestCase):
    def test_vimeo_values_as_researched(self):
        # Read from the pages cited in limits.py on 2026-10-31; change only together with the quote and the date.
        want = {"video.max_bytes": 322122547200, "video.max_duration_s": 86400, "title.max_chars": 128,
                "description.max_chars": 5000, "privacy.values": ["private", "unlisted", "public"],
                "upload.content_type": "application/offset+octet-stream"}
        for key, v in want.items():
            self.assertEqual(L.value("vimeo", key), v, key)
        for key in ("video.file_formats", "title.forbidden_chars", "description.forbidden_chars", "storage.max_bytes",
                    "quota.weekly_uploads", "quota.daily_uploads"):
            self.assertEqual(L.limits_of("vimeo")[key]["status"], "unknown", key)
            self.assertIsNone(L.value("vimeo", key), key)
        for key in ("video.codecs", "video.resolution", "video.aspect", "video.fps"):
            self.assertEqual(L.limits_of("vimeo")[key]["status"], "delegated", key)
        for key in ("video.max_bytes", "title.max_chars", "description.max_chars"):
            self.assertIn(str(L.value("vimeo", key)), L.limits_of("vimeo")[key]["quote"], key)
        for code in ("4101", "4102", "4104"):
            self.assertTrue(any(code in e.get("note", "") for e in L.limits_of("vimeo").values()), code)
        self.assertEqual(L.value("vimeo", "rate_limit.requests_per_minute")["Vimeo Free"], 25)
        self.assertEqual(L.limits_of("vimeo")["privacy.values"]["maps_to"]["private"], "nobody")
        for e in L.limits_of("vimeo").values():
            for url in [e.get("source")] + list(e.get("searched") or []):
                if url:
                    self.assertTrue(url.startswith("https://developer.vimeo.com/"), url)
