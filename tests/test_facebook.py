"""The Facebook uploader (roadmap step 5, ADR-009), entirely against tests/mock_facebook.py: an in-process mock of the
Resumable Upload API (session, upload with file_offset, the offset query) and the Page videos endpoint on 127.0.0.1.
The upload streamed in one body, a resume from file_offset after a cut connection or a lost answer, a timeout,
backoff on 5xx, 429 and Graph's transient and throttling codes, a video never published twice, refused tokens, and
that exactly the metadata the confirmation token bound is what arrives. No test contacts Facebook: the socket guard
lets a test reach only the loopback port of the mock it started, and the guard test below asserts that it blocks
every other host."""
from __future__ import annotations

import hashlib
import json
import os
import socket
from unittest import mock

from helpers import LOOPBACK_PORTS, NetworkAttempted, OfflineTestCase, facebook_mock, run_cli, run_cli_raw

from publish_skill import credentials as creds
from publish_skill import limits as L
from publish_skill.errors import PublishError
from publish_skill.metadata import Metadata, build
from publish_skill.plan import file_checks, metadata_checks, plan
from publish_skill.platforms import UploadRequest, get_platform, facebook
from publish_skill.upload import confirm_token, upload

SECRETS = {"PUBLISH_FACEBOOK_APP_ID": "918273645546372819", "PUBLISH_FACEBOOK_USER_ACCESS_TOKEN": "EAAuser-Tq81LmP0sWx",
           "PUBLISH_FACEBOOK_PAGE_ID": "564738291029384756", "PUBLISH_FACEBOOK_PAGE_ACCESS_TOKEN": "EAApage-Zr27KdQ4vNm"}
USER, PAGE_TOKEN = SECRETS["PUBLISH_FACEBOOK_USER_ACCESS_TOKEN"], SECRETS["PUBLISH_FACEBOOK_PAGE_ACCESS_TOKEN"]
UNIT = 256 * 1024
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
PUBLIC = ("--privacy", "public")


class FacebookTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.video = os.path.join(self.tmp, "final.mp4")         # 2.5 x 256 KiB
        with open(self.video, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + os.urandom(2 * UNIT + UNIT // 2 - 12))
        self.size = os.path.getsize(self.video)
        p = mock.patch.dict(os.environ, SECRETS)
        p.start()
        self.addCleanup(p.stop)

    def file(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def mock(self):
        self.m = facebook_mock(self, SECRETS)
        return self.m

    def confirmed(self, *flags, human=False, video=None):
        """Dry run, then the confirmed upload with the same flags (public, unless flags say otherwise)."""
        video = video or self.video
        flags = flags if "--privacy" in flags else flags + PUBLIC
        rc, dry = run_cli("upload", video, "--platform", "facebook", *flags, "--json")
        self.assertEqual(rc, 0, dry)
        argv = ["upload", video, "--platform", "facebook", *flags, "--confirm", dry["confirm_token"]]
        rc, out, err = run_cli_raw(*(argv if human else argv + ["--json"]))
        return rc, (None if human else json.loads(out)), out + err

    def data(self):
        with open(self.video, "rb") as fh:
            return fh.read()

    def assert_secret_free(self, text):
        for v in SECRETS.values():
            self.assertNotIn(v, text)
        self.assertNotIn("handle-secret", text)


class FacebookLoopbackOnlyTests(FacebookTestCase):
    def test_the_guard_blocks_every_non_loopback_host_for_facebook(self):
        m = self.mock()
        self.assertEqual(LOOPBACK_PORTS, {m.port})
        s = socket.create_connection(("127.0.0.1", m.port))           # the mock: allowed
        s.close()
        for address in (("graph.facebook.com", 443), ("graph-video.facebook.com", 443), ("rupload.facebook.com", 443),
                        ("www.facebook.com", 443), ("graph.instagram.com", 443), ("157.240.1.35", 443),
                        ("127.0.0.1", m.port + 1), ("::1", m.port)):
            with self.assertRaises(NetworkAttempted, msg=address):
                socket.create_connection(address)
        for host in ("graph.facebook.com", "localhost"):
            with self.assertRaises(NetworkAttempted):
                socket.getaddrinfo(host, m.port)

    def test_the_real_endpoints_are_facebooks_and_unreachable_from_a_test(self):
        self.assertEqual((facebook.API_BASE, facebook.VIDEO_BASE, facebook.GRAPH_VERSION),
                         ("https://graph.facebook.com", "https://graph-video.facebook.com", "v26.0"))
        up = get_platform("facebook")
        self.assertEqual((up.api_base, up.video_base), (facebook.API_BASE, facebook.VIDEO_BASE))
        req = UploadRequest("facebook", self.video, self.size, hashlib.sha256(self.data()).hexdigest(),
                            build(privacy="public").to_dict())
        with self.assertRaises(NetworkAttempted) as e:
            facebook.FacebookUploader(sleep=lambda s: None).upload(req, creds.Credentials("facebook", SECRETS))
        self.assertIn("graph.facebook.com", str(e.exception))

    def test_the_endpoints_must_be_https_or_loopback(self):
        req = UploadRequest("facebook", self.video, self.size, "0" * 64, build(privacy="public").to_dict())
        for attr, url in (("api_base", "http://graph.facebook.com"), ("video_base", "ftp://127.0.0.1")):
            up = facebook.FacebookUploader()
            setattr(up, attr, url)
            with self.assertRaises(PublishError) as e:
                up.upload(req, creds.Credentials("facebook", SECRETS))
            self.assertEqual(e.exception.code, "INTERNAL_ERROR")


class FacebookUploadTests(FacebookTestCase):
    def test_facebook_upload_sends_exactly_the_file_then_publishes_one_video(self):
        m = self.mock()
        rc, doc, text = self.confirmed("--title", "Launch day", "--description", "The keynote.")
        self.assertEqual(rc, 0, doc)
        r = doc["result"]
        self.assertEqual(m.kinds(), ["session", "upload", "publish"])
        sess = m.requests[0]
        self.assertEqual(sess["path"], f"/v26.0/{SECRETS['PUBLISH_FACEBOOK_APP_ID']}/uploads")
        q = dict(kv.split("=", 1) for kv in sess["query"].split("&"))
        self.assertEqual((q["file_name"], q["file_length"], q["file_type"]), ("final.mp4", str(self.size), "video%2Fmp4"))
        up = m.requests[1]
        self.assertEqual((up["headers"]["authorization"], up["headers"]["file_offset"], up["length"]),
                         ("OAuth " + USER, "0", self.size))
        self.assertEqual(m.requests[2]["path"], f"/v26.0/{SECRETS['PUBLISH_FACEBOOK_PAGE_ID']}/videos")
        self.assertEqual(len(m.videos), 1)
        self.assertEqual(m.videos[0]["data"], self.data())
        fields = dict(m.videos[0]["fields"])
        self.assertTrue(fields.pop("fbuploader_video_file_chunk").startswith("2:"))
        self.assertEqual(fields, {"title": "Launch day", "description": "The keynote.", "published": "true",
                                  "secret": "false"})
        self.assertEqual((r["video_id"], r["privacy"], r["url"]), (m.videos[0]["id"], "public", None))
        self.assertEqual(r["sent"], fields)
        self.assertEqual((r["transfer"]["bytes_sent"], r["transfer"]["retries"]), (self.size, 0))
        self.assert_secret_free(text)

    def test_unlisted_is_published_secret_and_no_title_sends_no_title(self):
        m = self.mock()
        rc, doc, _ = self.confirmed("--privacy", "unlisted")
        self.assertEqual(rc, 0, doc)
        fields = dict(m.videos[0]["fields"])
        fields.pop("fbuploader_video_file_chunk")
        self.assertEqual(fields, {"published": "true", "secret": "true"})

    def test_facebook_resume_after_an_interrupted_upload(self):
        # The connection is cut after 300 000 bytes arrived: the uploader asks for file_offset and sends the rest.
        m = self.mock()
        m.faults["upload"] = [("cut", 300000)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.kinds(), ["session", "upload", "offset", "upload", "publish"])
        self.assertEqual(m.offsets(), ["0", "300000"])
        self.assertEqual(m.requests[3]["length"], self.size - 300000)
        self.assertEqual(m.videos[0]["data"], self.data())
        t = doc["result"]["transfer"]
        self.assertEqual((t["retries"], t["resumed"]), (1, 1))
        self.assertEqual(self.waits, [1.0])

    def test_a_lost_answer_resumes_at_the_end_and_gets_the_handle(self):
        m = self.mock()
        m.faults["upload"] = [("lost",)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.offsets(), ["0", str(self.size)])
        self.assertEqual(m.requests[3]["length"], 0)
        self.assertEqual(len(m.videos), 1)

    def test_a_timeout_is_an_interruption(self):
        m = self.mock()
        m.faults["upload"] = [("stall", 1.5)]
        with mock.patch.object(facebook.FacebookUploader, "timeout_s", 0.3):
            rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.videos[0]["data"], self.data())

    def test_5xx_429_and_graph_codes_are_retried_with_backoff_and_retry_after(self):
        m = self.mock()
        m.faults["session"] = [("status", 500, 2, "Service temporarily unavailable")]
        m.faults["upload"] = [("status", 400, 4, "Application request limit reached", {"Retry-After": "40"})]
        m.faults["offset"] = [("status", 503, None, None)]
        m.faults["publish"] = [("status", 400, 17, "User request limit reached", {"Retry-After": "7"})]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(len(m.videos), 1)
        # session 1.0; upload: Retry-After 40; offset 1.0; publish: Retry-After 7
        self.assertEqual(self.waits, [1.0, 40.0, 1.0, 7.0])

    def test_retries_run_out_as_a_typed_retryable_error_and_nothing_is_published(self):
        m = self.mock()
        m.faults["upload"] = [("status", 503, None, None)] * 5
        with mock.patch.object(facebook.FacebookUploader, "max_retries", 3):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_INTERRUPTED", True))
        self.assertIn("nothing was published", doc["error"]["message"])
        self.assertEqual(m.videos, [])

    def test_the_session_request_without_an_answer_is_not_retried(self):
        m = self.mock()
        m.faults["session"] = [("stall", 1.0)]
        with mock.patch.object(facebook.FacebookUploader, "timeout_s", 0.3):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_INTERRUPTED"))
        self.assertEqual(m.kinds(), ["session"])

    def test_an_offset_outside_the_file_is_a_protocol_error(self):
        up = facebook.FacebookUploader()
        up._stats = {"requests": 0, "retries": 0, "waited_s": 0.0}
        for answer in ({"file_offset": -1}, {"file_offset": 10 ** 9}, {}, {"file_offset": True}):
            with mock.patch.object(up, "_api", return_value=facebook._Response(200, {}, json.dumps(answer).encode())):
                with self.assertRaises(PublishError) as e:
                    up._offset("u", {}, 100)
            self.assertEqual(e.exception.code, "UPLOAD_REJECTED", answer)
        with mock.patch.object(up, "_api", return_value=facebook._Response(200, {}, b'{"file_offset": "42"}')):
            self.assertEqual(up._offset("u", {}, 100), 42)

    def test_a_file_edited_during_the_upload_is_never_completed(self):
        # The first attempt is cut; before the resume, the file's last byte changes: its final bytes are never sent.
        m = self.mock()

        def edit():
            with open(self.video, "r+b") as fh:
                fh.seek(self.size - 1)
                fh.write(b"\xff" if self.data()[-1:] != b"\xff" else b"\x00")
        m.faults["upload"] = [("cut", 300000)]
        m.faults["offset"] = [("call", edit)]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"))
        self.assertEqual(m.videos, [])
        self.assertLess(len(m.sessions["MTpmYWtl0000"]["data"]), self.size)
        self.assertIsNone(m.sessions["MTpmYWtl0000"]["handle"])

    def test_the_hashed_tail_holds_back_the_last_block(self):
        path = self.file("t.mp4", b"a" * 5000)
        good = hashlib.sha256(b"a" * 5000).hexdigest()
        body = facebook._HashedTail(path, 1000, 5000, good)
        got = b""
        while True:
            buf = body.read(1024)
            if not buf:
                break
            got += buf
        body.close()
        self.assertEqual(got, b"a" * 4000)
        for size, sha in ((5000, "0" * 64), (4999, good), (5001, good)):
            body = facebook._HashedTail(path, 0, size, sha)
            with self.assertRaises(facebook._Changed):
                while body.read(1024):
                    pass
            body.close()

    def test_facebook_video_is_never_published_twice(self):
        # The publish answer is lost (the video exists), Facebook answers 5xx, or code 2 ("API Service"): stop.
        for fault in (("lost",), ("status", 503, None, None), ("status", 400, 2, "Service temporarily unavailable")):
            m = self.mock()
            m.faults["publish"] = [fault]
            rc, doc, _ = self.confirmed()
            self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_INTERRUPTED", False))
            self.assertIn("Check the Page", doc["error"]["message"])
            self.assertEqual(m.kinds().count("publish"), 1)

    def test_a_refused_user_token(self):
        m = self.mock()
        os.environ["PUBLISH_FACEBOOK_USER_ACCESS_TOKEN"] = "EAAexpired-user"
        rc, doc, text = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["details"]["error_code"]), (3, "AUTH_FAILED", 190))
        self.assertIn("publish auth login --platform facebook", doc["error"]["message"])
        self.assertEqual(m.kinds(), ["session"])
        self.assertNotIn("EAAexpired-user", text)

    def test_a_missing_page_permission_is_auth_failed(self):
        for code in (200, 10):
            m = self.mock()
            m.faults["publish"] = [("status", 403, code, "Requires pages_manage_posts permission")]
            rc, doc, _ = self.confirmed()
            self.assertEqual((rc, doc["error"]["code"]), (3, "AUTH_FAILED"), code)
            self.assertEqual(m.videos, [])

    def test_a_refused_video_is_rejected_with_facebooks_message(self):
        m = self.mock()
        m.faults["publish"] = [("status", 400, 6000, "There was a problem uploading your video file.")]
        rc, doc, text = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"))
        self.assertIn("problem uploading", doc["error"]["message"])
        self.assertEqual(doc["error"]["details"]["error_code"], 6000)
        self.assertNotIn("AbCdEfTrace", text)                          # fbtrace_id is not reported

    def test_ids_that_are_not_numeric_are_refused_before_any_request(self):
        m = self.mock()
        os.environ["PUBLISH_FACEBOOK_PAGE_ID"] = "my-page"
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        self.assertIn("PUBLISH_FACEBOOK_PAGE_ID", doc["error"]["message"])
        self.assertEqual(m.requests, [])


class FacebookMetadataTests(FacebookTestCase):
    def test_the_received_video_reproduces_the_token(self):
        m = self.mock()
        flags = ("--title", "新作 <Launch> 🎬", "--description", "Line 1\nLine 2 #film", "--privacy", "unlisted")
        rc, dry = run_cli("upload", self.video, "--platform", "facebook", *flags, "--json")
        rc, out, _ = run_cli_raw("upload", self.video, "--platform", "facebook", *flags, "--confirm",
                                 dry["confirm_token"], "--json")
        doc = json.loads(out)
        self.assertEqual(rc, 0, doc)
        got = m.videos[0]
        privacy = {("true", "false"): "public", ("true", "true"): "unlisted"}[(got["fields"]["published"],
                                                                                got["fields"]["secret"])]
        rebuilt = Metadata(title=got["fields"]["title"], description=got["fields"]["description"], privacy=privacy)
        self.assertEqual(confirm_token("facebook", hashlib.sha256(got["data"]).hexdigest(), len(got["data"]), rebuilt),
                         dry["confirm_token"])

    def test_private_and_unsent_fields_are_refused_before_a_token(self):
        m = self.mock()
        srt = self.file("en.srt", b"1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        for flags, check in (((), "privacy"), (("--privacy", "private"), "privacy"),
                             (("--tag", "film") + PUBLIC, "tags (not sent to Facebook)"),
                             (("--thumbnail", self.file("t.png", PNG)) + PUBLIC, "thumbnail (not sent to Facebook)"),
                             (("--caption", srt, "--caption-language", "en") + PUBLIC,
                              "captions (not sent to Facebook)")):
            with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
                rc, doc = run_cli("upload", self.video, "--platform", "facebook", *flags, "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "METADATA_INVALID"), flags)
            self.assertNotIn("confirm_token", json.dumps(doc))
            self.assertEqual([r["check"] for r in doc["error"]["details"]["failed"]], [check])
        self.assertEqual(m.requests, [])

    def test_a_file_the_upload_cannot_take_is_refused_before_a_token(self):
        m = self.mock()
        mov = self.file("final.mov", os.urandom(1000))
        with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
            rc, doc = run_cli("upload", mov, "--platform", "facebook", *PUBLIC, "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        self.assertEqual(m.requests, [])

    def test_publish_fields_are_the_bound_metadata_only(self):
        self.assertEqual(facebook.publish_fields(Metadata(privacy="public").to_dict(), "2:h"),
                         {"fbuploader_video_file_chunk": "2:h", "published": "true", "secret": "false"})
        self.assertEqual(facebook.publish_fields(Metadata(title="", privacy="unlisted").to_dict(), "2:h"),
                         {"title": "", "fbuploader_video_file_chunk": "2:h", "published": "true", "secret": "true"})
        for md in (Metadata(), Metadata(privacy="public", tags=["x"])):
            with self.assertRaises(PublishError):
                facebook.publish_fields(md.to_dict(), "2:h")

    def test_multipart(self):
        body = facebook.multipart({"title": "日本"}, "b0")
        self.assertEqual(body, b'--b0\r\nContent-Disposition: form-data; name="title"\r\n\r\n'
                               + "日本".encode("utf-8") + b"\r\n--b0--\r\n")


class FacebookGateAndRedactionTests(FacebookTestCase):
    def test_every_refusal_happens_before_any_request_or_credential(self):
        m = self.mock()
        token = upload(self.video, "facebook", metadata=build(privacy="public"))["confirm_token"]
        bad = [("upload", self.video, "--platform", "facebook", *PUBLIC, "--confirm", token + "0"),
               ("upload", self.video, "--platform", "facebook", *PUBLIC, "--confirm", token, "--title", "edited"),
               ("upload", self.video, "--platform", "facebook", "--confirm", token),
               ("upload", self.video, "--platform", "facebook", *PUBLIC)]
        with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
            for argv in bad:
                rc, out, _ = run_cli_raw(*argv, "--json")
                doc = json.loads(out)
                self.assertTrue(doc.get("dry_run") or rc == 2, (argv, doc))
        self.assertEqual(m.requests, [])

    def test_every_value_is_needed(self):
        m = self.mock()
        os.environ.pop("PUBLISH_FACEBOOK_PAGE_ACCESS_TOKEN")
        token = upload(self.video, "facebook", metadata=build(privacy="public"))["confirm_token"]
        rc, doc = run_cli("upload", self.video, "--platform", "facebook", *PUBLIC, "--confirm", token, "--json")
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["details"]["missing"]),
                         (3, "CREDENTIALS_MISSING", ["PUBLISH_FACEBOOK_PAGE_ACCESS_TOKEN"]))
        self.assertEqual(m.requests, [])

    def test_the_values_from_the_keychain_upload_and_are_never_printed(self):
        for k, v in SECRETS.items():
            os.environ.pop(k)
            self.keychain_backend.items[("publish-skill", k)] = v
        m = self.mock()
        rc, doc, text = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.requests[1]["headers"]["authorization"], "OAuth " + USER)
        self.assert_secret_free(text)

    def test_the_page_token_goes_to_the_publish_only_and_the_user_token_to_the_upload_only(self):
        m = self.mock()
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        for r in m.requests:
            seen = json.dumps(r["headers"]) + r["query"]
            if r["kind"] == "publish":
                self.assertNotIn(USER, seen)
            else:
                self.assertNotIn(PAGE_TOKEN, seen)

    def test_receipts_and_errors_never_carry_a_secret(self):
        scenarios = [{}, {"upload": [("cut", 1000)]}, {"publish": [("status", 403, 200, "Forbidden")]},
                     {"upload": [("status", 503, None, None)] * 30}, {"publish": [("lost",)]}]
        for faults in scenarios:
            for human in (False, True):
                m = self.mock()
                m.faults.update({k: list(v) for k, v in faults.items()})
                with mock.patch.object(facebook.FacebookUploader, "max_retries", 2):
                    rc, _, text = self.confirmed("--title", USER[:10], human=human)
                self.assert_secret_free(text)

    def test_human_receipt(self):
        self.mock()
        rc, _, text = self.confirmed("--title", "Launch", human=True)
        self.assertEqual(rc, 0, text)
        self.assertIn("to Facebook: video 1000000000", text)
        self.assertIn("(privacy public)", text)
        self.assertNotIn("None", text)


class FacebookPlanTests(FacebookTestCase):
    def test_metadata_rows(self):
        rows = {r["check"]: r for r in metadata_checks("facebook", build(title="x" * 5000, description="d" * 70000,
                                                                         privacy="unlisted"))}
        # no title or description maximum is stated: the rows pass with no limit, citing where it was looked for
        self.assertEqual((rows["title length"]["status"], rows["title length"]["limit"],
                          rows["title length"]["limit_key"]), ("PASS", None, "title.max_chars"))
        self.assertEqual((rows["description length"]["status"], rows["description length"]["limit"]), ("PASS", None))
        self.assertEqual(rows["privacy"]["status"], "PASS")
        rows = {r["check"]: r for r in metadata_checks("facebook", build(title="hi"))}
        self.assertEqual((rows["privacy"]["status"], rows["privacy"]["fix"]),
                         ("FAIL", "--privacy one of public, unlisted"))

    def test_file_rows(self):
        rows = {r["check"]: r for r in file_checks("facebook", "/x/a.mp4", 10 ** 12)}
        self.assertEqual((rows["file size"]["status"], rows["file size"]["limit"]), ("PASS", None))   # unknown
        self.assertNotIn("file size (minimum)", rows)
        rows = {r["check"]: r for r in file_checks("facebook", "/x/a.mov", 100)}
        self.assertEqual(rows["file format"]["status"], "FAIL")

    def test_plan_reads_ffmpeg_skills_facebook_spec(self):
        self.assertEqual(get_platform("facebook").ffmpeg_skill_platform, "facebook")
        doc = plan(self.video, "facebook", build(title="Launch", privacy="public"),
                   ffmpeg_skill_dir=os.path.join(self.tmp, "none"))
        self.assertEqual(doc["verdict"], "INCOMPLETE")
        self.assertEqual(doc["ffmpeg_skill"]["check_platform"], "facebook")
        rows = {r["check"]: r for r in doc["checks"]}
        self.assertEqual((rows["file format"]["status"], rows["file format"]["value"]), ("PASS", ".mp4"))
        self.assertFalse(doc["network_used"])

    def test_plan_fails_the_default_private_offline(self):
        rc, doc = run_cli("plan", self.video, "--platform", "facebook", "--title", "t", "--json")
        self.assertEqual((rc, doc["verdict"]), (1, "FAIL"))


class FacebookLimitsTableTests(OfflineTestCase):
    def test_facebook_values_as_researched(self):
        # Read from the pages cited in limits.py on 2026-10-31; change only together with the quote and the date.
        self.assertEqual(L.value("facebook", "video.file_formats"), [".mp4"])
        self.assertEqual(L.value("facebook", "privacy.values"), ["public", "unlisted"])
        self.assertEqual(L.limits_of("facebook")["privacy.values"]["maps_to"],
                         {"public": {"published": True, "secret": False}, "unlisted": {"published": True, "secret": True}})
        self.assertEqual({p: {k: v == "true" for k, v in f.items()} for p, f in facebook.FLAGS.items()},
                         L.limits_of("facebook")["privacy.values"]["maps_to"])
        for key in ("video.max_bytes", "video.min_bytes", "video.max_duration_s", "title.max_chars",
                    "title.forbidden_chars", "description.max_chars", "rate_limit.requests", "quota.daily_posts",
                    "token.lifetime_s"):
            self.assertEqual(L.limits_of("facebook")[key]["status"], "unknown", key)
        for key in ("video.codecs", "video.resolution", "video.aspect", "video.fps"):
            self.assertEqual(L.limits_of("facebook")[key]["status"], "delegated", key)
        for e in L.limits_of("facebook").values():
            for url in [e.get("source")] + list(e.get("searched") or []):
                if url:
                    self.assertTrue(url.startswith("https://developers.facebook.com/docs/"), url)
