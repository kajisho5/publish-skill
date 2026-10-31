"""The TikTok uploader (roadmap step 5, ADR-006), entirely against tests/mock_tiktok.py: an in-process mock of TikTok's
Content Posting API (creator info, Direct Post init with FILE_UPLOAD, the chunked PUT and the post's status) on
127.0.0.1. Chunked upload, a retried chunk after an interruption, a resume after a lost answer (416 and the post's
uploaded_bytes), a timeout, backoff on 5xx and 429, the creator's and the app's limits, an expired upload URL, a file
edited mid-upload, a failed post, and that exactly the metadata the confirmation token bound is what arrives. No test
contacts TikTok: the socket guard lets a test reach only the loopback port of the mock it started, and the guard test
below asserts that it blocks every other host."""
from __future__ import annotations

import hashlib
import json
import os
import socket
from unittest import mock

from helpers import LOOPBACK_PORTS, NetworkAttempted, OfflineTestCase, run_cli, run_cli_raw, tiktok_mock

from publish_skill import credentials as creds
from publish_skill import limits as L
from publish_skill.errors import PublishError
from publish_skill.metadata import Metadata, build
from publish_skill.plan import metadata_checks, plan
from publish_skill.platforms import UploadRequest, get_platform, tiktok
from publish_skill.upload import confirm_token, upload

TOKEN_VAR = "PUBLISH_TIKTOK_ACCESS_TOKEN"
TOKEN = "act.tiktok-9b1f3e0c7a2d4f68b5e1c0ffee"
UNIT = 256 * 1024
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))


class TikTokTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.video = os.path.join(self.tmp, "final.mp4")         # 2.5 chunks of 256 KiB: 2 chunks, the last 1.5
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
        self.m = tiktok_mock(self, TOKEN, **kw)
        return self.m

    def confirmed(self, *flags, human=False, video=None):
        """Dry run, then the confirmed upload with the same flags; (exit code, document or None, stdout + stderr)."""
        video = video or self.video
        rc, dry = run_cli("upload", video, "--platform", "tiktok", *flags, "--json")
        self.assertEqual(rc, 0, dry)
        argv = ["upload", video, "--platform", "tiktok", *flags, "--confirm", dry["confirm_token"]]
        rc, out, err = run_cli_raw(*(argv if human else argv + ["--json"]))
        return rc, (None if human else json.loads(out)), out + err

    def assert_nothing_secret(self, text):
        self.assertNotIn(TOKEN, text)
        for url in self.m.upload_urls:
            self.assertNotIn(url, text)
        self.assertNotIn("upload_token", text)

    def data(self):
        with open(self.video, "rb") as fh:
            return fh.read()

    def ranges(self, *rs):
        """The Content-Range of a PUT for each (start, stop) pair, and "STATUS" as given."""
        return [r if r == "STATUS" else f"bytes {r[0]}-{r[1] - 1}/{self.size}" for r in rs]

    def last(self):
        return (UNIT, self.size)


class TikTokLoopbackOnlyTests(TikTokTestCase):
    def test_the_guard_blocks_every_non_loopback_host_for_tiktok(self):
        m = self.mock()
        self.assertEqual(LOOPBACK_PORTS, {m.port})
        s = socket.create_connection(("127.0.0.1", m.port))           # the mock: allowed
        s.close()
        for address in (("open.tiktokapis.com", 443), ("open-upload.tiktokapis.com", 443), ("www.tiktok.com", 443),
                        ("developers.tiktok.com", 443), ("23.0.0.1", 443), ("8.8.8.8", 53),
                        ("127.0.0.1", m.port + 1), ("::1", m.port), ("0.0.0.0", m.port)):
            with self.assertRaises(NetworkAttempted, msg=address):
                socket.create_connection(address)
        for host in ("open.tiktokapis.com", "localhost"):              # no name lookup either
            with self.assertRaises(NetworkAttempted):
                socket.getaddrinfo(host, m.port)

    def test_the_real_endpoint_is_tiktoks_and_unreachable_from_a_test(self):
        self.assertEqual(tiktok.API_BASE, "https://open.tiktokapis.com")
        self.assertEqual(get_platform("tiktok").api_base, tiktok.API_BASE)
        req = UploadRequest("tiktok", self.video, self.size, hashlib.sha256(self.data()).hexdigest(), build().to_dict())
        with self.assertRaises(NetworkAttempted) as e:                # no mock: the first request is stopped
            tiktok.TikTokUploader(sleep=lambda s: None).upload(req, creds.Credentials("tiktok", {TOKEN_VAR: TOKEN}))
        self.assertIn("open.tiktokapis.com", str(e.exception))

    def test_the_endpoint_must_be_https_or_loopback(self):
        req = UploadRequest("tiktok", self.video, self.size, "0" * 64, build().to_dict())
        for url in ("http://open.tiktokapis.com", "ftp://127.0.0.1"):
            up = tiktok.TikTokUploader()
            up.api_base = url
            with self.assertRaises(PublishError) as e:
                up.upload(req, creds.Credentials("tiktok", {TOKEN_VAR: TOKEN}))
            self.assertEqual(e.exception.code, "INTERNAL_ERROR")

    def test_the_upload_url_must_be_https_on_tiktokapis(self):
        up = tiktok.TikTokUploader()
        for bad in ("https://evil.example/upload/", "http://open-upload.tiktokapis.com/video/",
                    "https://tiktokapis.com.evil.example/x", "https://nottiktokapis.com/x", "http://127.0.0.1:9/upload/"):
            with self.assertRaises(PublishError, msg=bad) as e:
                up._check_upload_url(bad)
            self.assertEqual(e.exception.code, "UPLOAD_REJECTED")
        up._check_upload_url("https://open-upload.tiktokapis.com/video/?upload_id=1&upload_token=x")

    def test_an_upload_url_off_the_mock_is_refused_before_any_byte(self):
        m = self.mock(upload_url_base="http://127.0.0.1:9")
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"))
        self.assertEqual(m.kinds(), ["creator_info", "init"])
        self.assertEqual(m.posts, {})


class TikTokChunkTests(TikTokTestCase):
    def test_chunk_plan_follows_the_media_transfer_guide(self):
        self.assertEqual(tiktok.chunk_plan(10, 64), {"chunk_size": 10, "total_chunk_count": 1})     # whole
        self.assertEqual(tiktok.chunk_plan(64, 64), {"chunk_size": 64, "total_chunk_count": 1})
        self.assertEqual(tiktok.chunk_plan(150, 64), {"chunk_size": 64, "total_chunk_count": 2})    # floor
        self.assertEqual([(r.start, r.stop) for r in tiktok.chunk_ranges(150, tiktok.chunk_plan(150, 64))],
                         [(0, 64), (64, 150)])                                                     # the last takes the rest
        # the real numbers: at most 64 MB per chunk, at least 5 MB, the last below 128 MB, at most 1000 chunks for 4 GB
        self.assertLessEqual(tiktok.DEFAULT_CHUNK, 64 * 1000 * 1000)
        self.assertGreaterEqual(tiktok.MIN_CHUNK, 5 * 1000 * 1000)
        self.assertGreaterEqual(tiktok.DEFAULT_CHUNK, tiktok.MIN_CHUNK)
        p = tiktok.chunk_plan(tiktok.MAX_BYTES, tiktok.DEFAULT_CHUNK)
        self.assertLessEqual(p["total_chunk_count"], tiktok.MAX_CHUNKS)
        for size in (tiktok.DEFAULT_CHUNK + 1, 2 * tiktok.DEFAULT_CHUNK - 1, tiktok.MAX_BYTES):
            rs = tiktok.chunk_ranges(size, tiktok.chunk_plan(size, tiktok.DEFAULT_CHUNK))
            self.assertLess(len(rs[-1]), 128 * 1000 * 1000)
            self.assertEqual(sum(len(r) for r in rs), size)
        with self.assertRaises(PublishError):
            tiktok.chunk_plan(0, 64)

    def test_tiktok_chunked_upload_sends_exactly_the_file(self):
        m = self.mock()
        rc, doc, text = self.confirmed("--title", "Launch #new")
        self.assertEqual(rc, 0, doc)
        r = doc["result"]
        self.assertEqual(m.posts[r["publish_id"]]["data"], self.data())
        self.assertEqual(m.kinds(), ["creator_info", "init", "put", "put", "status"])
        self.assertEqual(m.ranges(), self.ranges((0, UNIT), self.last(), "STATUS"))
        for req in m.requests:
            if req["kind"] == "put":                                   # the token goes to the API only, never the URL
                self.assertNotIn("authorization", req["headers"])
                self.assertEqual(req["headers"]["content-type"], "video/mp4")
            else:
                self.assertEqual((req["headers"]["authorization"], req["headers"]["content-type"]),
                                 ("Bearer " + TOKEN, "application/json; charset=UTF-8"))
        self.assertEqual(m.inits[0]["source_info"], {"source": "FILE_UPLOAD", "video_size": self.size,
                                                     "chunk_size": UNIT, "total_chunk_count": 2})
        self.assertEqual((r["status"], r["post_ids"], r["privacy"], r["privacy_level"]),
                         ("PUBLISH_COMPLETE", ["7300000000000000001"], "private", "SELF_ONLY"))
        self.assertEqual(r["url"], "https://www.tiktok.com/@studio_test/video/7300000000000000001")
        self.assertEqual(r["creator"]["max_video_post_duration_sec"], 600)
        self.assertEqual((r["transfer"]["bytes_sent"], r["transfer"]["retries"], r["transfer"]["resumed"]), (self.size, 0, 0))
        self.assert_nothing_secret(text)

    def test_a_small_file_is_sent_whole(self):
        m = self.mock()
        small = self.file("small.mp4", b"\x00\x00\x00\x18ftypmp42" + os.urandom(1000))
        rc, doc, _ = self.confirmed(video=small)
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.inits[0]["source_info"]["total_chunk_count"], 1)
        self.assertEqual(m.inits[0]["source_info"]["chunk_size"], os.path.getsize(small))
        self.assertEqual(m.kinds().count("put"), 1)

    def test_mov_and_webm_are_sent_with_their_type(self):
        for name, ctype in (("final.mov", "video/quicktime"), ("final.webm", "video/webm")):
            m = self.mock()
            path = self.file(name, b"\x1a\x45\xdf\xa3" + os.urandom(3000))
            rc, doc, _ = self.confirmed(video=path)
            self.assertEqual(rc, 0, doc)
            self.assertEqual([r["headers"]["content-type"] for r in m.requests if r["kind"] == "put"], [ctype])

    def test_tiktok_resume_after_an_interrupted_chunk(self):
        # The first chunk's answer is lost after TikTok kept it: the resend gets 416, the post's uploaded_bytes says
        # the chunk arrived, and the upload resumes with the next one.
        m = self.mock()
        m.faults["put"] = [("lost",)]
        rc, doc, text = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.posts[doc["result"]["publish_id"]]["data"], self.data())
        self.assertEqual(m.ranges(), self.ranges((0, UNIT), (0, UNIT), "STATUS", self.last(), "STATUS"))
        t = doc["result"]["transfer"]
        self.assertEqual((t["retries"], t["resumed"]), (1, 2))
        self.assertEqual(self.waits, [1.0])                     # backoff: 0.5 (the test's random) * 2**1
        self.assertEqual(len(m.inits), 1)                       # the same post: no second init
        self.assert_nothing_secret(text)

    def test_a_chunk_that_never_arrived_is_sent_again(self):
        m = self.mock()
        m.faults["put"] = [None, ("drop",)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.posts[doc["result"]["publish_id"]]["data"], self.data())
        self.assertEqual(m.ranges(), self.ranges((0, UNIT), self.last(), self.last(), "STATUS"))

    def test_the_last_chunks_lost_answer_completes_the_upload(self):
        m = self.mock()
        m.faults["put"] = [None, ("lost",)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.posts[doc["result"]["publish_id"]]["data"], self.data())
        self.assertEqual(m.ranges(), self.ranges((0, UNIT), self.last(), self.last(), "STATUS", "STATUS"))

    def test_a_timeout_is_an_interruption(self):
        m = self.mock()
        m.faults["put"] = [None, ("stall", 1.5)]
        with mock.patch.object(tiktok.TikTokUploader, "timeout_s", 0.3):
            rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.posts[doc["result"]["publish_id"]]["data"], self.data())
        self.assertEqual(doc["result"]["transfer"]["resumed"], 1)

    def test_5xx_and_429_are_retried_with_exponential_backoff_and_retry_after(self):
        m = self.mock()
        m.faults["creator_info"] = [("status", 503, None)]
        m.faults["init"] = [("status", 429, "rate_limit_exceeded", {"Retry-After": "7"})]
        m.faults["put"] = [("status", 502, None)]
        m.faults["status"] = [("status", 500, None)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.posts[doc["result"]["publish_id"]]["data"], self.data())
        # creator_info: 1.0; init: max(1.0, Retry-After 7); put: 1.0; status: 1.0
        self.assertEqual(self.waits, [1.0, 7.0, 1.0, 1.0])
        self.assertEqual(m.kinds(), ["creator_info", "creator_info", "init", "init", "put", "put", "put", "status",
                                     "status"])
        self.assertEqual(len(m.inits), 1)

    def test_backoff_is_capped(self):
        up = tiktok.TikTokUploader(sleep=lambda s: None, rand=lambda: 1.0)
        up._stats = {"retries": 0, "waited_s": 0.0}
        waits = []
        up._sleep_fn = waits.append
        for attempt in (1, 2, 6, 7, 10):
            up._backoff(attempt, None)
        self.assertEqual(waits, [2.0, 4.0, 64.0, 64.0, 64.0])
        up._backoff(1, tiktok._Response(429, {"retry-after": "100000"}, b""))
        self.assertEqual(waits[-1], 300.0)

    def test_retries_run_out_as_a_typed_retryable_error_and_nothing_is_posted(self):
        m = self.mock()
        m.faults["put"] = [("status", 503, None)] * 50
        with mock.patch.object(tiktok.TikTokUploader, "max_retries", 3):
            rc, doc, text = self.confirmed()
        err = doc["error"]
        self.assertEqual((rc, err["code"], err["retryable"]), (4, "UPLOAD_INTERRUPTED", True))
        self.assertFalse(err["details"]["uploaded"])
        self.assertEqual(err["details"]["last"]["status"], 503)
        self.assertEqual(err["details"]["publish_id"], list(m.by_publish)[0])
        self.assertIn("nothing was posted", err["message"])
        self.assertEqual(m.posts, {})
        self.assertEqual(len(self.waits), 3)
        self.assert_nothing_secret(text)

    def test_an_init_without_an_answer_is_not_retried(self):
        m = self.mock()
        m.faults["init"] = [("stall", 0.6)]
        with mock.patch.object(tiktok.TikTokUploader, "timeout_s", 0.2):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_INTERRUPTED"))
        self.assertEqual(m.kinds(), ["creator_info", "init"])  # a second init would start a second post
        self.assertEqual(self.waits, [])

    def test_an_expired_upload_url_is_interrupted_not_restarted(self):
        m = self.mock()
        m.faults["put"] = [None, ("expire",)]
        rc, doc, text = self.confirmed()
        err = doc["error"]
        self.assertEqual((rc, err["code"], err["details"]["status"]), (4, "UPLOAD_INTERRUPTED", 403))
        self.assertEqual(m.kinds(), ["creator_info", "init", "put", "put"])
        self.assertEqual(m.posts, {})
        self.assert_nothing_secret(text)

    def test_a_file_edited_during_the_upload_is_never_completed(self):
        m = self.mock()

        def edit():                                             # same size, one byte changed in the last chunk
            with open(self.video, "r+b") as fh:
                fh.seek(self.size - 1)
                old = fh.read(1)
                fh.seek(self.size - 1)
                fh.write(b"\x01" if old != b"\x01" else b"\x02")

        m.faults["put"] = [("call", edit)]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"))
        self.assertFalse(doc["error"]["details"]["uploaded"])
        self.assertEqual(m.posts, {})
        self.assertEqual(m.ranges(), self.ranges((0, UNIT)))   # the last chunk was never sent

    def test_a_416_that_matches_neither_end_of_the_chunk_is_a_protocol_error(self):
        up = tiktok.TikTokUploader()
        up._stats = {"status_polls": 0, "requests": 0}
        with mock.patch.object(up, "_api", return_value=tiktok._Response(200, {}, b'{"data": {}, "error": {"code": "ok"}}')):
            with self.assertRaises(PublishError) as e:
                up._uploaded_bytes({}, "p")
        self.assertEqual(e.exception.code, "UPLOAD_REJECTED")


class TikTokPostStatusTests(TikTokTestCase):
    def test_processing_is_polled_until_complete(self):
        m = self.mock(statuses=[{"status": "PROCESSING_UPLOAD"}, {"status": "PROCESSING_UPLOAD"},
                                {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": []}])
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.kinds().count("status"), 3)
        self.assertEqual(self.waits, [6.0, 6.0])                # the status interval, under the 30-per-minute limit
        self.assertEqual((doc["result"]["status"], doc["result"]["post_ids"]), ("PUBLISH_COMPLETE", []))
        self.assertEqual(doc["result"]["url"], "https://www.tiktok.com/@studio_test")   # private: no public post id

    def test_still_processing_when_the_polls_run_out_is_a_receipt_with_a_warning(self):
        m = self.mock(statuses=[{"status": "PROCESSING_UPLOAD"}])
        with mock.patch.object(tiktok.TikTokUploader, "status_polls", 3):
            rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertTrue(doc["uploaded"])
        self.assertEqual(doc["result"]["status"], "PROCESSING_UPLOAD")
        self.assertTrue(any("do not run upload again" in w for w in doc["result"]["warnings"]))
        self.assertEqual(m.kinds().count("status"), 3)

    def test_a_failed_post_is_typed_by_its_reason(self):
        for reason, code, exit_code in (("duration_check_failed", "UPLOAD_REJECTED", 4),
                                        ("spam_risk_too_many_posts", "UPLOAD_LIMIT_EXCEEDED", 4),
                                        ("internal", "UPLOAD_INTERRUPTED", 4)):
            self.mock(statuses=[{"status": "FAILED", "fail_reason": reason}])
            rc, doc, text = self.confirmed()
            err = doc["error"]
            self.assertEqual((rc, err["code"], err["details"]["fail_reason"]), (exit_code, code, reason))
            self.assert_nothing_secret(text)
        # a delivery-spec failure names the fix
        self.assertIn("ffmpeg-skill export --preset tiktok",
                      tiktok.TikTokUploader()._failed("p", "frame_rate_check_failed").message)


class TikTokAccountErrorTests(TikTokTestCase):
    def test_tiktok_daily_post_cap_is_typed_and_not_retried(self):
        # creator_info answers HTTP 200 with error.code spam_risk_too_many_posts; init answers 403
        for kind, status in (("creator_info", 200), ("init", 403)):
            m = self.mock()
            m.faults[kind] = [("status", status, "spam_risk_too_many_posts")]
            rc, doc, text = self.confirmed()
            err = doc["error"]
            self.assertEqual((rc, err["code"], err["retryable"]), (4, "UPLOAD_LIMIT_EXCEEDED", True))
            self.assertEqual(err["details"]["error_code"], "spam_risk_too_many_posts")
            self.assertEqual(m.kinds()[-1], kind)                # no retry, no byte of the video sent
            self.assertNotIn("put", m.kinds())
            self.assertEqual(self.waits, [])
            self.assert_nothing_secret(text)

    def test_the_apps_active_user_cap_is_a_quota(self):
        m = self.mock()
        m.faults["init"] = [("status", 403, "reached_active_user_cap")]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "QUOTA_EXCEEDED", True))
        self.assertNotIn("put", m.kinds())

    def test_a_refused_access_token(self):
        m = self.mock()
        with mock.patch.dict(os.environ, {TOKEN_VAR: "act.revoked-0000"}):
            rc, doc, text = self.confirmed()
        err = doc["error"]
        self.assertEqual((rc, err["code"], err["retryable"], err["details"]["error_code"]),
                         (3, "AUTH_FAILED", False, "access_token_invalid"))
        self.assertEqual(m.kinds(), ["creator_info"])
        self.assertNotIn("act.revoked-0000", text)
        self.assert_nothing_secret(text)

    def test_a_missing_scope_is_auth_failed(self):
        m = self.mock()
        m.faults["init"] = [("status", 401, "scope_not_authorized")]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (3, "AUTH_FAILED"))
        self.assertIn("video.publish", doc["error"]["message"])

    def test_an_unaudited_app_posting_publicly_is_rejected(self):
        m = self.mock()
        m.faults["init"] = [("status", 403, "unaudited_client_can_only_post_to_private_accounts")]
        rc, doc, _ = self.confirmed("--privacy", "public")
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_REJECTED", False))
        self.assertNotIn("put", m.kinds())

    def test_a_privacy_the_creator_does_not_offer_stops_before_init(self):
        # a private account offers FOLLOWER_OF_CREATOR, MUTUAL_FOLLOW_FRIENDS, SELF_ONLY: not PUBLIC_TO_EVERYONE
        m = self.mock(privacy_level_options=["FOLLOWER_OF_CREATOR", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"])
        rc, doc, _ = self.confirmed("--privacy", "public")
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"))
        self.assertIn("PUBLIC_TO_EVERYONE", doc["error"]["message"])
        self.assertEqual(m.kinds(), ["creator_info"])

    def test_the_rate_limit_is_retried_then_interrupted(self):
        m = self.mock()
        m.faults["init"] = [("status", 429, "rate_limit_exceeded")] * 10
        with mock.patch.object(tiktok.TikTokUploader, "max_retries", 2):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_INTERRUPTED"))
        self.assertEqual(doc["error"]["details"]["last"], {"status": 429, "error_code": "rate_limit_exceeded"})
        self.assertEqual(m.kinds(), ["creator_info"] + ["init"] * 3)


class TikTokBoundMetadataTests(TikTokTestCase):
    def test_privacy_defaults_to_self_only_and_nothing_unbound_is_sent(self):
        m = self.mock()
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.inits[0]["post_info"], {"privacy_level": "SELF_ONLY"})
        self.assertEqual(doc["result"]["sent"], m.inits[0])

    def test_each_privacy_maps_to_one_privacy_level(self):
        for privacy, level in (("private", "SELF_ONLY"), ("public", "PUBLIC_TO_EVERYONE")):
            m = self.mock()
            rc, doc, _ = self.confirmed("--privacy", privacy)
            self.assertEqual(rc, 0, doc)
            self.assertEqual(m.inits[0]["post_info"]["privacy_level"], level)
        self.assertEqual(L.limits_of("tiktok")["privacy.values"]["maps_to"], tiktok.PRIVACY_LEVEL)

    def test_the_received_upload_reproduces_the_token(self):
        m = self.mock()
        flags = ("--title", "新作 Launch 🎬 #film", "--privacy", "public")
        rc, dry = run_cli("upload", self.video, "--platform", "tiktok", *flags, "--json")
        rc, out, _ = run_cli_raw("upload", self.video, "--platform", "tiktok", *flags, "--confirm", dry["confirm_token"],
                                 "--json")
        doc = json.loads(out)
        self.assertEqual(rc, 0, doc)
        got = m.posts[doc["result"]["publish_id"]]
        post = got["body"]["post_info"]
        self.assertEqual(post, {"title": "新作 Launch 🎬 #film", "privacy_level": "PUBLIC_TO_EVERYONE"})
        reverse = {v: k for k, v in tiktok.PRIVACY_LEVEL.items()}
        rebuilt = Metadata(title=post["title"], privacy=reverse[post["privacy_level"]])
        self.assertEqual(confirm_token("tiktok", hashlib.sha256(got["data"]).hexdigest(), len(got["data"]), rebuilt),
                         dry["confirm_token"])

    def test_the_token_binds_the_platform(self):
        self.assertNotEqual(confirm_token("tiktok", "a" * 64, 10), confirm_token("vimeo", "a" * 64, 10))
        token = upload(self.video, "vimeo")["confirm_token"]
        rc, doc = run_cli("upload", self.video, "--platform", "tiktok", "--confirm", token, "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"))

    def test_unsent_fields_and_unlisted_are_refused_before_a_token(self):
        m = self.mock()
        srt = self.file("en.srt", b"1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        for flags, check in ((("--description", "more"), "description (not sent to TikTok)"),
                             (("--tag", "film"), "tags (not sent to TikTok)"),
                             (("--thumbnail", self.file("t.png", PNG)), "thumbnail (not sent to TikTok)"),
                             (("--caption", srt, "--caption-language", "en"), "captions (not sent to TikTok)"),
                             (("--privacy", "unlisted"), "privacy")):
            with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
                rc, doc = run_cli("upload", self.video, "--platform", "tiktok", *flags, "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "METADATA_INVALID"), flags)
            self.assertNotIn("confirm_token", json.dumps(doc))
            self.assertEqual([r["check"] for r in doc["error"]["details"]["failed"]], [check])
        self.assertEqual(m.requests, [])

    def test_a_file_tiktok_cannot_take_is_refused_before_a_token(self):
        m = self.mock()
        mkv = self.file("final.mkv", b"\x1a\x45\xdf\xa3" + os.urandom(100))
        with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
            rc, doc = run_cli("upload", mkv, "--platform", "tiktok", "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        self.assertNotIn("confirm_token", json.dumps(doc))
        self.assertEqual(m.requests, [])
        # YouTube's list is advisory: the same file gets a token there (plan warns)
        self.assertTrue(upload(mkv, "youtube")["confirm_token"])

    def test_init_body_is_the_bound_metadata_only(self):
        p = {"chunk_size": 5, "total_chunk_count": 1}
        self.assertEqual(tiktok.init_body(Metadata().to_dict(), 5, p),
                         {"post_info": {"privacy_level": "SELF_ONLY"},
                          "source_info": {"source": "FILE_UPLOAD", "video_size": 5, "chunk_size": 5, "total_chunk_count": 1}})
        self.assertEqual(tiktok.init_body(Metadata(title="", privacy="public").to_dict(), 5, p)["post_info"],
                         {"title": "", "privacy_level": "PUBLIC_TO_EVERYONE"})
        for md in (Metadata(privacy="unlisted"), Metadata(description="x"), Metadata(tags=["x"])):
            with self.assertRaises(PublishError):
                tiktok.init_body(md.to_dict(), 5, p)


class TikTokGateAndRedactionTests(TikTokTestCase):
    def test_every_refusal_happens_before_any_request_or_credential(self):
        m = self.mock()
        token = upload(self.video, "tiktok")["confirm_token"]
        bad = [("upload", self.video, "--platform", "tiktok", "--confirm", token + "0"),
               ("upload", self.video, "--platform", "tiktok", "--confirm", token, "--title", "edited"),
               ("upload", self.video, "--platform", "tiktok", "--confirm", token, "--title", "x" * 2201),
               ("upload", self.video, "--platform", "tiktok", "--confirm", token, "--description", "y"),
               ("upload", self.video, "--platform", "tiktok", "--confirm", token, "--privacy", "unlisted"),
               ("upload", self.video, "--platform", "tiktok"),                         # a dry run
               ("upload", self.file("empty.mp4", b""), "--platform", "tiktok", "--confirm",
                confirm_token("tiktok", hashlib.sha256(b"").hexdigest(), 0))]
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
        self.assertEqual(m.requests[0]["headers"]["authorization"], "Bearer " + TOKEN)
        self.assert_nothing_secret(text)

    def test_receipts_and_errors_never_carry_the_token_or_the_upload_url(self):
        scenarios = [{}, {"put": [("lost",)]}, {"init": [("status", 403, "spam_risk_too_many_posts")]},
                     {"put": [("status", 503, None)] * 30}, {"put": [None, ("expire",)]}]
        for faults in scenarios:
            for human in (False, True):
                m = self.mock()
                m.faults.update({k: list(v) for k, v in faults.items()})
                with mock.patch.object(tiktok.TikTokUploader, "max_retries", 2):
                    # the token is also planted in the title, where a careless uploader might echo it
                    rc, _, text = self.confirmed("--title", TOKEN, human=human)
                self.assert_nothing_secret(text)

    def test_human_receipt(self):
        self.mock()
        rc, _, text = self.confirmed("--title", "Launch", human=True)
        self.assertEqual(rc, 0, text)
        self.assertIn("to TikTok", text)
        self.assertIn("(privacy private) https://www.tiktok.com/@studio_test/video/", text)
        self.assert_nothing_secret(text)


class TikTokPlanTests(TikTokTestCase):
    def test_metadata_rows_use_tiktoks_limits(self):
        # 2200 UTF-16 code units: an emoji outside the BMP counts two
        rows = {r["check"]: r for r in metadata_checks("tiktok", build(title="🎬" * 1100))}
        self.assertEqual((rows["title length"]["status"], rows["title length"]["value"], rows["title length"]["limit"]),
                         ("PASS", 2200, 2200))
        self.assertEqual(rows["title length"]["limit_key"], "title.max_utf16_units")
        self.assertEqual(rows["title length"]["source"],
                         "https://developers.tiktok.com/doc/content-posting-api-reference-direct-post")
        rows = {r["check"]: r for r in metadata_checks("tiktok", build(title="🎬" * 1100 + "x"))}
        self.assertEqual(rows["title length"]["status"], "FAIL")
        self.assertIn("UTF-16", rows["title length"]["fix"])
        self.assertNotIn("title characters", rows)                     # no forbidden characters are stated
        rows = {r["check"]: r for r in metadata_checks("tiktok", build(privacy="unlisted"))}
        self.assertEqual(rows["privacy"]["status"], "FAIL")

    def test_plan_reads_ffmpeg_skills_tiktok_spec(self):
        self.assertEqual(get_platform("tiktok").ffmpeg_skill_platform, "tiktok")
        doc = plan(self.video, "tiktok", build(title="Launch"), ffmpeg_skill_dir=os.path.join(self.tmp, "none"))
        self.assertEqual(doc["verdict"], "INCOMPLETE")                  # never PASS without ffmpeg-skill's check
        self.assertEqual(doc["ffmpeg_skill"]["check_platform"], "tiktok")
        rows = {r["check"]: r for r in doc["checks"]}
        self.assertEqual((rows["file size"]["status"], rows["file size"]["limit"]), ("PASS", 4000000000))
        self.assertEqual((rows["file format"]["status"], rows["file format"]["value"]), ("PASS", ".mp4"))
        self.assertFalse(doc["network_used"])

    def test_plan_fails_a_format_tiktok_cannot_take(self):
        mkv = self.file("final.mkv", b"x" * 100)
        doc = plan(mkv, "tiktok", ffmpeg_skill_dir=os.path.join(self.tmp, "none"))
        row = [r for r in doc["checks"] if r["check"] == "file format"][0]
        self.assertEqual((doc["verdict"], row["status"]), ("FAIL", "FAIL"))
        row = [r for r in plan(mkv, "youtube", ffmpeg_skill_dir=os.path.join(self.tmp, "none"))["checks"]
               if r["check"] == "file format"][0]
        self.assertEqual(row["status"], "WARN")                         # YouTube's list stays advisory

    def test_plan_fails_an_over_long_title_offline(self):
        rc, doc = run_cli("plan", self.video, "--platform", "tiktok", "--title", "x" * 2201, "--json")
        self.assertEqual((rc, doc["verdict"]), (1, "FAIL"))
        self.assertFalse(doc["uploaded"])


class TikTokLimitsTableTests(OfflineTestCase):
    def test_tiktok_values_as_researched(self):
        # Read from the pages cited in limits.py on 2026-10-31; change only together with the quote and the date.
        want = {"video.max_bytes": 4000000000, "video.max_duration_s": 600, "title.max_utf16_units": 2200,
                "privacy.values": ["private", "public"], "video.file_formats": [".mp4", ".webm", ".mov"],
                "upload.max_chunks": 1000, "upload.url_validity_s": 3600, "token.lifetime_s": 86400}
        for key, v in want.items():
            self.assertEqual(L.value("tiktok", key), v, key)
        for key in ("video.creator_max_duration_s", "title.forbidden_chars", "quota.daily_posts", "quota.active_users"):
            self.assertEqual(L.limits_of("tiktok")[key]["status"], "unknown", key)
            self.assertIsNone(L.value("tiktok", key), key)
        for key in ("video.codecs", "video.resolution", "video.aspect", "video.fps"):
            self.assertEqual(L.limits_of("tiktok")[key]["status"], "delegated", key)
        for key in ("title.max_utf16_units", "upload.max_chunks"):
            self.assertIn(f"{L.value('tiktok', key):,}" if key == "upload.max_chunks" else str(L.value("tiktok", key)),
                          L.limits_of("tiktok")[key]["quote"], key)
        self.assertEqual(L.value("tiktok", "rate_limit.requests_per_minute")["video/init"], 6)
        self.assertEqual(L.limits_of("tiktok")["video.file_formats"]["content_types"],
                         {k: v for k, v in tiktok.CONTENT_TYPES.items()})
        self.assertEqual(L.value("tiktok", "video.max_bytes"), tiktok.MAX_BYTES)
        for e in L.limits_of("tiktok").values():
            for url in [e.get("source")] + list(e.get("searched") or []):
                if url:
                    self.assertTrue(url.startswith("https://developers.tiktok.com/doc/"), url)
