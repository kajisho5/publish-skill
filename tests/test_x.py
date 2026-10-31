"""The X uploader (roadmap step 5, ADR-007), entirely against tests/mock_x.py: an in-process mock of the X API v2
chunked media upload (initialize, append, finalize, status) and create-post endpoints on 127.0.0.1. Segmented upload,
a resent segment after an interruption (a lost answer and a dropped segment), a timeout, backoff on 5xx and 429 with
Retry-After and x-rate-limit-reset, processing polled until it succeeds or fails, a post never sent twice, refused
tokens, and that exactly the metadata the confirmation token bound is what arrives. No test contacts X: the socket
guard lets a test reach only the loopback port of the mock it started, and the guard test below asserts that it blocks
every other host."""
from __future__ import annotations

import hashlib
import json
import os
import socket
from unittest import mock

from helpers import LOOPBACK_PORTS, NetworkAttempted, OfflineTestCase, run_cli, run_cli_raw, x_mock

from publish_skill import credentials as creds
from publish_skill import limits as L
from publish_skill.errors import PublishError
from publish_skill.metadata import Metadata, build
from publish_skill.plan import metadata_checks, plan
from publish_skill.platforms import UploadRequest, get_platform, x
from publish_skill.upload import confirm_token, upload

TOKEN_VAR = "PUBLISH_X_ACCESS_TOKEN"
TOKEN = "xoauth2-Qm9vT2F1dGgyVXNlclRva2VuX2F2b2lk"
UNIT = 256 * 1024
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
PUBLIC = ("--privacy", "public")


class XTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.video = os.path.join(self.tmp, "final.mp4")         # 2.5 segments of 256 KiB: 3 segments
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
        self.m = x_mock(self, TOKEN, **kw)
        return self.m

    def confirmed(self, *flags, human=False, video=None):
        """Dry run, then the confirmed upload with the same flags (public, unless flags say otherwise)."""
        video = video or self.video
        flags = flags if "--privacy" in flags else flags + PUBLIC
        rc, dry = run_cli("upload", video, "--platform", "x", *flags, "--json")
        self.assertEqual(rc, 0, dry)
        argv = ["upload", video, "--platform", "x", *flags, "--confirm", dry["confirm_token"]]
        rc, out, err = run_cli_raw(*(argv if human else argv + ["--json"]))
        return rc, (None if human else json.loads(out)), out + err

    def data(self):
        with open(self.video, "rb") as fh:
            return fh.read()


class XLoopbackOnlyTests(XTestCase):
    def test_the_guard_blocks_every_non_loopback_host_for_x(self):
        m = self.mock()
        self.assertEqual(LOOPBACK_PORTS, {m.port})
        s = socket.create_connection(("127.0.0.1", m.port))           # the mock: allowed
        s.close()
        for address in (("api.x.com", 443), ("upload.x.com", 443), ("api.twitter.com", 443), ("upload.twitter.com", 443),
                        ("x.com", 443), ("104.244.42.1", 443), ("127.0.0.1", m.port + 1), ("::1", m.port)):
            with self.assertRaises(NetworkAttempted, msg=address):
                socket.create_connection(address)
        for host in ("api.x.com", "localhost"):
            with self.assertRaises(NetworkAttempted):
                socket.getaddrinfo(host, m.port)

    def test_the_real_endpoint_is_xs_and_unreachable_from_a_test(self):
        self.assertEqual(x.API_BASE, "https://api.x.com")
        self.assertEqual(get_platform("x").api_base, x.API_BASE)
        req = UploadRequest("x", self.video, self.size, hashlib.sha256(self.data()).hexdigest(),
                            build(privacy="public").to_dict())
        with self.assertRaises(NetworkAttempted) as e:
            x.XUploader(sleep=lambda s: None).upload(req, creds.Credentials("x", {TOKEN_VAR: TOKEN}))
        self.assertIn("api.x.com", str(e.exception))

    def test_the_endpoint_must_be_https_or_loopback(self):
        req = UploadRequest("x", self.video, self.size, "0" * 64, build(privacy="public").to_dict())
        for url in ("http://api.x.com", "ftp://127.0.0.1"):
            up = x.XUploader()
            up.api_base = url
            with self.assertRaises(PublishError) as e:
                up.upload(req, creds.Credentials("x", {TOKEN_VAR: TOKEN}))
            self.assertEqual(e.exception.code, "INTERNAL_ERROR")


class XUploadTests(XTestCase):
    def test_x_segmented_upload_sends_exactly_the_file_then_one_post(self):
        m = self.mock()
        rc, doc, text = self.confirmed("--title", "Launch day")
        self.assertEqual(rc, 0, doc)
        r = doc["result"]
        self.assertEqual(m.kinds(), ["init", "append", "append", "append", "finalize", "post"])
        self.assertEqual(m.segment_indexes(), [0, 1, 2])
        self.assertEqual(m.inits[0], {"media_type": "video/mp4", "total_bytes": self.size, "media_category": "tweet_video"})
        self.assertEqual(len(m.posts), 1)
        self.assertEqual(m.posts[0]["data"], self.data())
        self.assertEqual(m.posts[0]["body"], {"text": "Launch day", "media": {"media_ids": [r["media_id"]]}})
        self.assertEqual((r["video_id"], r["post_id"], r["privacy"], r["url"]), (m.posts[0]["id"], m.posts[0]["id"],
                                                                                 "public", None))
        for req in m.requests:
            self.assertEqual(req["headers"]["authorization"], "Bearer " + TOKEN)
        self.assertEqual((r["transfer"]["bytes_sent"], r["transfer"]["retries"]), (self.size, 0))
        self.assertNotIn(TOKEN, text)

    def test_a_media_only_post_when_no_title_is_bound(self):
        m = self.mock()
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.posts[0]["body"], {"media": {"media_ids": [doc["result"]["media_id"]]}})

    def test_mov_webm_and_ts_are_initialised_with_their_type(self):
        for name, ctype in (("final.mov", "video/quicktime"), ("final.webm", "video/webm"), ("final.ts", "video/mp2t")):
            m = self.mock()
            rc, doc, _ = self.confirmed(video=self.file(name, b"\x47" + os.urandom(3000)))
            self.assertEqual(rc, 0, doc)
            self.assertEqual(m.inits[0]["media_type"], ctype)

    def test_x_resume_after_an_interrupted_segment(self):
        # The second segment's answer is lost after X kept it: it is sent again with the same segment_index, and
        # finalize's size proves the media is whole.
        m = self.mock()
        m.faults["append"] = [None, ("lost",)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.segment_indexes(), [0, 1, 1, 2])
        self.assertEqual(m.posts[0]["data"], self.data())
        t = doc["result"]["transfer"]
        self.assertEqual((t["retries"], t["resumed"]), (1, 1))
        self.assertEqual(self.waits, [1.0])
        self.assertEqual(len(m.inits), 1)

    def test_a_dropped_segment_is_sent_again(self):
        m = self.mock()
        m.faults["append"] = [("drop",)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.segment_indexes(), [0, 0, 1, 2])
        self.assertEqual(m.posts[0]["data"], self.data())

    def test_a_timeout_is_an_interruption(self):
        m = self.mock()
        m.faults["append"] = [None, ("stall", 1.5)]
        with mock.patch.object(x.XUploader, "timeout_s", 0.3):
            rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.posts[0]["data"], self.data())

    def test_5xx_and_429_are_retried_with_backoff_retry_after_and_the_rate_limit_reset(self):
        m = self.mock(processing=[{"state": "pending", "check_after_secs": 3}, {"state": "succeeded"}])
        m.faults["init"] = [("status", 503, None)]
        m.faults["append"] = [("status", 429, "Too Many Requests", {"x-rate-limit-reset": str(int(1e9) + 40)})]
        m.faults["finalize"] = [("status", 429, None, {"Retry-After": "7"})]
        m.faults["status"] = [("status", 500, None)]
        m.faults["post"] = [("status", 429, None)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(len(m.posts), 1)
        # init 1.0; append: the reset 40 s away; finalize: Retry-After 7; check_after_secs 3; status 1.0; post 1.0
        self.assertEqual(self.waits, [1.0, 40.0, 7.0, 3.0, 1.0, 1.0])

    def test_retries_run_out_as_a_typed_retryable_error_and_nothing_is_posted(self):
        m = self.mock()
        m.faults["append"] = [("status", 503, None)] * 5
        with mock.patch.object(x.XUploader, "max_retries", 3):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_INTERRUPTED", True))
        self.assertIn("nothing was posted", doc["error"]["message"])
        self.assertEqual(m.posts, [])

    def test_initialize_without_an_answer_is_not_retried(self):
        m = self.mock()
        m.faults["init"] = [("stall", 1.0)]
        with mock.patch.object(x.XUploader, "timeout_s", 0.3):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_INTERRUPTED"))
        self.assertEqual(m.kinds(), ["init"])

    def test_finalize_rejects_a_size_that_is_not_the_files(self):
        up = x.XUploader()
        resp = x._Response(200, {}, json.dumps({"data": {"id": "1", "size": 10}}).encode())
        with mock.patch.object(up, "_api", return_value=resp):
            with self.assertRaises(PublishError) as e:
                up._finalize({}, "1", 11)
        self.assertEqual(e.exception.code, "UPLOAD_REJECTED")
        with mock.patch.object(up, "_api", return_value=resp):
            self.assertIsNone(up._finalize({}, "1", 10))

    def test_a_file_edited_during_the_upload_is_never_completed(self):
        m = self.mock()

        def edit():
            with open(self.video, "r+b") as fh:
                fh.seek(self.size - 1)
                fh.write(b"\xff" if self.data()[-1:] != b"\xff" else b"\x00")
        m.faults["append"] = [("call", edit)]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"))
        self.assertEqual(m.segment_indexes(), [0, 1])
        self.assertEqual(m.posts, [])

    def test_processing_is_polled_until_it_succeeds(self):
        m = self.mock(processing=[{"state": "pending", "check_after_secs": 1},
                                  {"state": "in_progress", "check_after_secs": 5, "progress_percent": 40},
                                  {"state": "succeeded", "progress_percent": 100}])
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.kinds()[-4:], ["finalize", "status", "status", "post"])
        self.assertEqual(self.waits, [1.0, 5.0])
        status = [r for r in m.requests if r["kind"] == "status"][0]
        self.assertEqual(status["query"], f"command=STATUS&media_id={doc['result']['media_id']}")

    def test_check_after_secs_is_capped(self):
        self.mock(processing=[{"state": "pending", "check_after_secs": 9999}, {"state": "succeeded"}])
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(self.waits, [x.XUploader.max_check_after_s])

    def test_failed_processing_is_rejected_and_nothing_is_posted(self):
        m = self.mock(processing=[{"state": "pending", "check_after_secs": 1},
                                  {"state": "failed", "error": {"code": 1, "name": "InvalidMedia",
                                                                "message": "Unsupported video"}}])
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"))
        self.assertIn("Unsupported video", doc["error"]["message"])
        self.assertIn("ffmpeg-skill export --preset x", doc["error"]["message"])
        self.assertEqual(m.posts, [])

    def test_still_processing_after_the_polls_is_interrupted_and_nothing_is_posted(self):
        m = self.mock(processing=[{"state": "in_progress", "check_after_secs": 1}])
        with mock.patch.object(x.XUploader, "status_polls", 3):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_INTERRUPTED", True))
        self.assertEqual(m.kinds().count("status"), 3)
        self.assertEqual(m.posts, [])

    def test_x_post_is_never_sent_twice(self):
        # The post's answer is lost (X made it) or X answers 5xx: the uploader stops and says to check the profile.
        for fault in (("lost",), ("status", 503, None)):
            m = self.mock()
            m.faults["post"] = [fault]
            rc, doc, _ = self.confirmed()
            self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_INTERRUPTED", False))
            self.assertIn("Check the account's profile", doc["error"]["message"])
            self.assertEqual(m.kinds().count("post"), 1)

    def test_a_refused_access_token(self):
        m = self.mock()
        os.environ[TOKEN_VAR] = "xoauth2-expired"
        rc, doc, text = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (3, "AUTH_FAILED"))
        self.assertIn("publish auth login --platform x", doc["error"]["message"])
        self.assertEqual(m.kinds(), ["init"])
        self.assertNotIn("xoauth2-expired", text)

    def test_a_missing_scope_is_auth_failed(self):
        m = self.mock()
        m.faults["post"] = [("status", 403, "Your client is missing the tweet.write scope")]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (3, "AUTH_FAILED"))
        self.assertEqual(m.posts, [])

    def test_a_video_too_long_for_the_account_is_rejected_with_xs_detail(self):
        m = self.mock()
        m.faults["post"] = [("status", 403, "This user is not allowed to post a video longer than 20 minutes.")]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"))
        self.assertIn("longer than 20 minutes", doc["error"]["message"])
        self.assertEqual(doc["error"]["details"]["status"], 403)


class XMetadataTests(XTestCase):
    def test_the_received_post_reproduces_the_token(self):
        m = self.mock()
        flags = ("--title", "新作 Launch 🎬 #film", "--privacy", "public")
        rc, dry = run_cli("upload", self.video, "--platform", "x", *flags, "--json")
        rc, out, _ = run_cli_raw("upload", self.video, "--platform", "x", *flags, "--confirm", dry["confirm_token"],
                                 "--json")
        doc = json.loads(out)
        self.assertEqual(rc, 0, doc)
        got = m.posts[0]
        rebuilt = Metadata(title=got["body"]["text"], privacy="public")
        self.assertEqual(confirm_token("x", hashlib.sha256(got["data"]).hexdigest(), len(got["data"]), rebuilt),
                         dry["confirm_token"])

    def test_private_and_unlisted_and_unsent_fields_are_refused_before_a_token(self):
        m = self.mock()
        srt = self.file("en.srt", b"1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        for flags, check in (((), "privacy"), (("--privacy", "private"), "privacy"),
                             (("--privacy", "unlisted"), "privacy"),
                             (("--description", "more") + PUBLIC, "description (not sent to X)"),
                             (("--tag", "film") + PUBLIC, "tags (not sent to X)"),
                             (("--thumbnail", self.file("t.png", PNG)) + PUBLIC, "thumbnail (not sent to X)"),
                             (("--caption", srt, "--caption-language", "en") + PUBLIC, "captions (not sent to X)")):
            with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
                rc, doc = run_cli("upload", self.video, "--platform", "x", *flags, "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "METADATA_INVALID"), flags)
            self.assertNotIn("confirm_token", json.dumps(doc))
            self.assertEqual([r["check"] for r in doc["error"]["details"]["failed"]], [check])
        self.assertEqual(m.requests, [])

    def test_a_file_x_cannot_take_is_refused_before_a_token(self):
        m = self.mock()
        mkv = self.file("final.mkv", b"\x1a\x45\xdf\xa3" + os.urandom(100))
        with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
            rc, doc = run_cli("upload", mkv, "--platform", "x", *PUBLIC, "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        self.assertEqual(m.requests, [])

    def test_post_body_is_the_bound_metadata_only(self):
        self.assertEqual(x.post_body(Metadata(privacy="public").to_dict(), "7"), {"media": {"media_ids": ["7"]}})
        self.assertEqual(x.post_body(Metadata(title="", privacy="public").to_dict(), "7"),
                         {"text": "", "media": {"media_ids": ["7"]}})
        for md in (Metadata(), Metadata(privacy="unlisted"), Metadata(privacy="public", description="x"),
                   Metadata(privacy="public", tags=["x"])):
            with self.assertRaises(PublishError):
                x.post_body(md.to_dict(), "7")

    def test_segments_and_multipart(self):
        self.assertEqual([(r.start, r.stop) for r in x.segments(10, 4)], [(0, 4), (4, 8), (8, 10)])
        self.assertEqual([(r.start, r.stop) for r in x.segments(8, 4)], [(0, 4), (4, 8)])
        self.assertLess(x.DEFAULT_CHUNK, 5 * 1000 * 1000)                  # "at or below 5 MB" under either reading
        self.assertLessEqual(len(x.segments(x.MAX_BYTES, x.DEFAULT_CHUNK)), x.MAX_SEGMENTS)
        body = x.multipart({"segment_index": "3"}, b"\x00\x01", "b0")
        self.assertIn(b'name="segment_index"\r\n\r\n3\r\n', body)
        self.assertTrue(body.endswith(b"\x00\x01\r\n--b0--\r\n"))


class XGateAndRedactionTests(XTestCase):
    def test_every_refusal_happens_before_any_request_or_credential(self):
        m = self.mock()
        token = upload(self.video, "x", metadata=build(privacy="public"))["confirm_token"]
        bad = [("upload", self.video, "--platform", "x", *PUBLIC, "--confirm", token + "0"),
               ("upload", self.video, "--platform", "x", *PUBLIC, "--confirm", token, "--title", "edited"),
               ("upload", self.video, "--platform", "x", *PUBLIC, "--confirm", token, "--title", "x" * 281),
               ("upload", self.video, "--platform", "x", "--confirm", token),
               ("upload", self.video, "--platform", "x", *PUBLIC)]
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
        self.assertNotIn(TOKEN, text)

    def test_receipts_and_errors_never_carry_the_token(self):
        scenarios = [{}, {"append": [("lost",)]}, {"post": [("status", 403, "Forbidden")]},
                     {"append": [("status", 503, None)] * 30}, {"post": [("lost",)]}]
        for faults in scenarios:
            for human in (False, True):
                m = self.mock()
                m.faults.update({k: list(v) for k, v in faults.items()})
                with mock.patch.object(x.XUploader, "max_retries", 2):
                    rc, _, text = self.confirmed("--title", TOKEN[:40], human=human)
                self.assertNotIn(TOKEN, text)

    def test_human_receipt(self):
        self.mock()
        rc, _, text = self.confirmed("--title", "Launch", human=True)
        self.assertEqual(rc, 0, text)
        self.assertIn("to X: video 199", text)
        self.assertIn("(privacy public)", text)
        self.assertNotIn("None", text)


class XPlanTests(XTestCase):
    def test_metadata_rows_use_xs_weighted_count(self):
        rows = {r["check"]: r for r in metadata_checks("x", build(title="a" * 280, privacy="public"))}
        self.assertEqual((rows["title length"]["status"], rows["title length"]["value"], rows["title length"]["limit"],
                          rows["title length"]["limit_key"]), ("PASS", 280, 280, "title.max_weighted"))
        self.assertEqual(rows["title length"]["source"], "https://docs.x.com/fundamentals/counting-characters")
        rows = {r["check"]: r for r in metadata_checks("x", build(title="日本語" * 47, privacy="public"))}
        self.assertEqual((rows["title length"]["status"], rows["title length"]["value"]), ("FAIL", 282))  # CJK count 2
        rows = {r["check"]: r for r in metadata_checks("x", build(title="日本語" * 46 + "ab", privacy="public"))}
        self.assertEqual(rows["title length"]["status"], "PASS")
        self.assertEqual(rows["privacy"]["status"], "PASS")
        rows = {r["check"]: r for r in metadata_checks("x", build(title="hi"))}
        self.assertEqual((rows["privacy"]["status"], rows["privacy"]["fix"]), ("FAIL", "--privacy one of public"))

    def test_weighted_length_is_exact_without_urls_or_emoji_and_an_upper_bound_with_them(self):
        self.assertEqual(L.x_weighted_length("hello"), {"value": 5, "exact": True})
        self.assertEqual(L.x_weighted_length("こんにちは"), {"value": 10, "exact": True})
        self.assertEqual(L.x_weighted_length("— ’"), {"value": 3, "exact": True})  # in the weight-1 ranges
        self.assertEqual(L.x_weighted_length("é"), {"value": 1, "exact": True})       # NFC: one character
        self.assertEqual(L.x_weighted_length("see https://a.co"), {"value": 4 + 23, "exact": False})
        self.assertEqual(L.x_weighted_length("🎬"), {"value": 2, "exact": False})
        # an over-long upper bound is a WARN, never a FAIL
        rows = {r["check"]: r for r in metadata_checks("x", build(title="a" * 275 + " 👨‍👩‍👧", privacy="public"))}
        self.assertEqual(rows["title length"]["status"], "WARN")
        self.assertIn("X's composer", rows["title length"]["fix"])

    def test_plan_reads_ffmpeg_skills_x_spec(self):
        self.assertEqual(get_platform("x").ffmpeg_skill_platform, "x")
        doc = plan(self.video, "x", build(title="Launch", privacy="public"),
                   ffmpeg_skill_dir=os.path.join(self.tmp, "none"))
        self.assertEqual(doc["verdict"], "INCOMPLETE")
        self.assertEqual(doc["ffmpeg_skill"]["check_platform"], "x")
        rows = {r["check"]: r for r in doc["checks"]}
        self.assertEqual((rows["file size"]["status"], rows["file size"]["limit"]), ("PASS", 8000000000))
        self.assertEqual((rows["file format"]["status"], rows["file format"]["value"]), ("PASS", ".mp4"))
        mkv = self.file("final.mkv", b"x" * 100)
        row = [r for r in plan(mkv, "x", ffmpeg_skill_dir=os.path.join(self.tmp, "none"))["checks"]
               if r["check"] == "file format"][0]
        self.assertEqual(row["status"], "FAIL")
        self.assertFalse(doc["network_used"])

    def test_plan_fails_an_over_long_text_offline(self):
        rc, doc = run_cli("plan", self.video, "--platform", "x", "--title", "x" * 281, *PUBLIC, "--json")
        self.assertEqual((rc, doc["verdict"]), (1, "FAIL"))


class XLimitsTableTests(OfflineTestCase):
    def test_x_values_as_researched(self):
        # Read from the pages cited in limits.py on 2026-10-31; change only together with the quote and the date.
        want = {"video.max_bytes": 8000000000, "video.max_duration_s": 1200, "title.max_weighted": 280,
                "privacy.values": ["public"], "video.file_formats": [".mp4", ".mov", ".webm", ".ts"],
                "upload.max_segments": 10000, "upload.session_validity_s": 86400, "token.lifetime_s": 7200}
        for key, v in want.items():
            self.assertEqual(L.value("x", key), v, key)
        for key in ("title.forbidden_chars", "quota.daily_posts"):
            self.assertEqual(L.limits_of("x")[key]["status"], "unknown", key)
        for key in ("video.codecs", "video.resolution", "video.aspect", "video.fps"):
            self.assertEqual(L.limits_of("x")[key]["status"], "delegated", key)
        self.assertEqual(L.limits_of("x")["video.file_formats"]["content_types"], x.CONTENT_TYPES)
        self.assertEqual(L.value("x", "video.max_bytes"), x.MAX_BYTES)
        self.assertEqual(L.X_WEIGHT_ONE_RANGES, ((0, 4351), (8192, 8205), (8208, 8223), (8242, 8247)))
        for e in L.limits_of("x").values():
            for url in [e.get("source")] + list(e.get("searched") or []):
                if url:
                    self.assertTrue(url.startswith("https://docs.x.com/"), url)
