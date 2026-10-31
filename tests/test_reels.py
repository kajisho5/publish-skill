"""The Instagram Reels uploader (roadmap step 5, ADR-010), entirely against tests/mock_reels.py: an in-process mock of
Instagram content publishing (a resumable REELS container, the rupload upload, the container's status, media_publish)
on 127.0.0.1. The streamed upload, a whole resend after an interruption (a cut connection, a lost answer, a retriable
debug_info), a timeout, backoff on 5xx, 429 and Graph's codes, the status polled a minute apart until FINISHED, ERROR
or EXPIRED, a reel never published twice, refused tokens, and that exactly the metadata the confirmation token bound
is what arrives. No test contacts Instagram: the socket guard lets a test reach only the loopback port of the mock it
started, and the guard test below asserts that it blocks every other host."""
from __future__ import annotations

import hashlib
import json
import os
import socket
from unittest import mock

from helpers import LOOPBACK_PORTS, NetworkAttempted, OfflineTestCase, reels_mock, run_cli, run_cli_raw

from publish_skill import credentials as creds
from publish_skill import limits as L
from publish_skill.errors import PublishError
from publish_skill.metadata import Metadata, build
from publish_skill.plan import duration_checks, file_checks, metadata_checks, plan
from publish_skill.platforms import UploadRequest, get_platform, reels
from publish_skill.upload import confirm_token, upload

IG_VAR, TOKEN_VAR = "PUBLISH_INSTAGRAM_USER_ID", "PUBLISH_INSTAGRAM_ACCESS_TOKEN"
IG = "17841400008460056"
TOKEN = "EAAinstagram-Lq81TmP0sWzVb"
UNIT = 256 * 1024
PNG = b"\x89PNG\r\n\x1a\n" + bytes(range(256))
PUBLIC = ("--privacy", "public")


class ReelsTestCase(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.video = os.path.join(self.tmp, "final.mp4")         # 2.5 x 256 KiB
        with open(self.video, "wb") as fh:
            fh.write(b"\x00\x00\x00\x18ftypmp42" + os.urandom(2 * UNIT + UNIT // 2 - 12))
        self.size = os.path.getsize(self.video)
        p = mock.patch.dict(os.environ, {IG_VAR: IG, TOKEN_VAR: TOKEN})
        p.start()
        self.addCleanup(p.stop)

    def file(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as fh:
            fh.write(data)
        return path

    def mock(self, **kw):
        self.m = reels_mock(self, IG, TOKEN, **kw)
        return self.m

    def confirmed(self, *flags, human=False, video=None):
        """Dry run, then the confirmed upload with the same flags (public, unless flags say otherwise)."""
        video = video or self.video
        flags = flags if "--privacy" in flags else flags + PUBLIC
        rc, dry = run_cli("upload", video, "--platform", "reels", *flags, "--json")
        self.assertEqual(rc, 0, dry)
        argv = ["upload", video, "--platform", "reels", *flags, "--confirm", dry["confirm_token"]]
        rc, out, err = run_cli_raw(*(argv if human else argv + ["--json"]))
        return rc, (None if human else json.loads(out)), out + err

    def data(self):
        with open(self.video, "rb") as fh:
            return fh.read()


class ReelsLoopbackOnlyTests(ReelsTestCase):
    def test_the_guard_blocks_every_non_loopback_host_for_reels(self):
        m = self.mock()
        self.assertEqual(LOOPBACK_PORTS, {m.port})
        s = socket.create_connection(("127.0.0.1", m.port))           # the mock: allowed
        s.close()
        for address in (("graph.facebook.com", 443), ("rupload.facebook.com", 443), ("graph.instagram.com", 443),
                        ("www.instagram.com", 443), ("i.instagram.com", 443), ("157.240.1.174", 443),
                        ("127.0.0.1", m.port + 1), ("::1", m.port)):
            with self.assertRaises(NetworkAttempted, msg=address):
                socket.create_connection(address)
        for host in ("rupload.facebook.com", "localhost"):
            with self.assertRaises(NetworkAttempted):
                socket.getaddrinfo(host, m.port)

    def test_the_real_endpoints_are_metas_and_unreachable_from_a_test(self):
        self.assertEqual((reels.API_BASE, reels.UPLOAD_BASE, reels.GRAPH_VERSION),
                         ("https://graph.facebook.com", "https://rupload.facebook.com", "v26.0"))
        up = get_platform("reels")
        self.assertEqual((up.api_base, up.upload_base), (reels.API_BASE, reels.UPLOAD_BASE))
        req = UploadRequest("reels", self.video, self.size, hashlib.sha256(self.data()).hexdigest(),
                            build(privacy="public").to_dict())
        with self.assertRaises(NetworkAttempted) as e:
            reels.ReelsUploader(sleep=lambda s: None).upload(req, creds.Credentials("reels", {IG_VAR: IG,
                                                                                               TOKEN_VAR: TOKEN}))
        self.assertIn("graph.facebook.com", str(e.exception))

    def test_the_endpoints_must_be_https_or_loopback(self):
        req = UploadRequest("reels", self.video, self.size, "0" * 64, build(privacy="public").to_dict())
        for attr, url in (("api_base", "http://graph.facebook.com"), ("upload_base", "ftp://127.0.0.1")):
            up = reels.ReelsUploader()
            setattr(up, attr, url)
            with self.assertRaises(PublishError) as e:
                up.upload(req, creds.Credentials("reels", {IG_VAR: IG, TOKEN_VAR: TOKEN}))
            self.assertEqual(e.exception.code, "INTERNAL_ERROR")


class ReelsUploadTests(ReelsTestCase):
    def test_reels_upload_sends_exactly_the_file_then_publishes_once(self):
        m = self.mock()
        rc, doc, text = self.confirmed("--title", "Launch day #film")
        self.assertEqual(rc, 0, doc)
        r = doc["result"]
        self.assertEqual(m.kinds(), ["container", "upload", "status", "publish"])
        self.assertEqual(m.requests[0]["path"], f"/v26.0/{IG}/media")
        self.assertEqual(m.requests[0]["form"], {"media_type": "REELS", "upload_type": "resumable",
                                                 "caption": "Launch day #film"})
        up = m.requests[1]
        self.assertEqual(up["path"], f"/ig-api-upload/v26.0/{r['container']}")
        self.assertEqual((up["headers"]["authorization"], up["headers"]["offset"], up["headers"]["file_size"],
                          up["length"]), ("OAuth " + TOKEN, "0", str(self.size), self.size))
        self.assertEqual(m.requests[2]["form"], {"fields": "status_code"})
        self.assertEqual(m.requests[3]["form"], {"creation_id": r["container"]})
        self.assertEqual(len(m.media), 1)
        self.assertEqual(m.media[0]["data"], self.data())
        self.assertEqual((r["video_id"], r["media_id"], r["privacy"], r["url"]),
                         (m.media[0]["id"], m.media[0]["id"], "public", None))
        self.assertEqual(r["sent"], m.requests[0]["form"])
        self.assertEqual((r["transfer"]["bytes_sent"], r["transfer"]["retries"]), (self.size, 0))
        self.assertNotIn(TOKEN, text)
        self.assertNotIn(IG, text)

    def test_a_mov_and_no_caption(self):
        m = self.mock()
        rc, doc, _ = self.confirmed(video=self.file("final.mov", os.urandom(5000)))
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.requests[0]["form"], {"media_type": "REELS", "upload_type": "resumable"})

    def test_reels_retry_after_an_interrupted_upload(self):
        # The connection is cut mid-body: no byte offset can be asked for, so the whole file is sent again, from 0.
        m = self.mock()
        m.faults["upload"] = [("cut", 100000)]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.kinds(), ["container", "upload", "upload", "status", "publish"])
        uploads = [r for r in m.requests if r["kind"] == "upload"]
        self.assertEqual([(u["headers"]["offset"], u["length"]) for u in uploads], [("0", self.size)] * 2)
        self.assertEqual(m.media[0]["data"], self.data())
        t = doc["result"]["transfer"]
        self.assertEqual((t["retries"], t["resumed"]), (1, 1))
        self.assertEqual(self.waits, [1.0])
        self.assertEqual(len(m.containers), 1)

    def test_a_lost_answer_and_a_retriable_debug_info_are_sent_again(self):
        for fault in (("lost",), ("debug", 500, True, "temporary"), ("debug", 400, True, "try again")):
            m = self.mock()
            m.faults["upload"] = [fault]
            rc, doc, _ = self.confirmed()
            self.assertEqual(rc, 0, (fault, doc))
            self.assertEqual(m.kinds().count("upload"), 2, fault)
            self.assertEqual(len(m.media), 1)

    def test_a_debug_info_that_is_not_retriable_is_rejected(self):
        m = self.mock()
        m.faults["upload"] = [("debug", 400, False, "video processing failed")]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"))
        self.assertIn("video processing failed", doc["error"]["message"])
        self.assertEqual(m.kinds(), ["container", "upload"])

    def test_an_unauthorized_upload_is_auth_failed(self):
        m = self.mock()
        m.faults["upload"] = [("debug", 400, False, '{"success":false,"error":{"message":"unauthorized user request"}}')]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (3, "AUTH_FAILED"))
        self.assertEqual(m.media, [])

    def test_a_timeout_is_an_interruption(self):
        m = self.mock()
        m.faults["upload"] = [("stall", 1.5)]
        with mock.patch.object(reels.ReelsUploader, "timeout_s", 0.3):
            rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.media[0]["data"], self.data())

    def test_5xx_429_and_graph_codes_are_retried_with_backoff_and_retry_after(self):
        m = self.mock(processing=["IN_PROGRESS", "FINISHED"])
        m.faults["container"] = [("status", 500, 2, "Service temporarily unavailable")]
        m.faults["upload"] = [("status", 429, None, None, {"Retry-After": "40"})]
        m.faults["status"] = [("status", 503, None, None)]
        m.faults["publish"] = [("status", 400, 4, "Application request limit reached", {"Retry-After": "7"})]
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(len(m.media), 1)
        # container 1.0; upload: Retry-After 40; status 1.0; a minute between status queries; publish: Retry-After 7
        self.assertEqual(self.waits, [1.0, 40.0, 1.0, 60.0, 7.0])

    def test_retries_run_out_as_a_typed_retryable_error_and_nothing_is_published(self):
        m = self.mock()
        m.faults["upload"] = [("status", 503, None, None)] * 5
        with mock.patch.object(reels.ReelsUploader, "max_retries", 3):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_INTERRUPTED", True))
        self.assertIn("nothing was published", doc["error"]["message"])
        self.assertEqual(m.media, [])

    def test_the_container_request_without_an_answer_is_not_retried(self):
        m = self.mock()
        m.faults["container"] = [("stall", 1.0)]
        with mock.patch.object(reels.ReelsUploader, "timeout_s", 0.3):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_INTERRUPTED"))
        self.assertEqual(m.kinds(), ["container"])

    def test_a_file_edited_before_a_resend_is_never_completed(self):
        # The first upload is cut; while the uploader backs off, the file's last byte changes: the resend stops
        # before its final bytes.
        m = self.mock()
        m.faults["upload"] = [("cut", 100000)]

        def edit_while_waiting(seconds):
            self.waits.append(seconds)
            with open(self.video, "r+b") as fh:
                fh.seek(self.size - 1)
                fh.write(b"\xff" if self.data()[-1:] != b"\xff" else b"\x00")
        with mock.patch.object(reels, "_sleep", edit_while_waiting):
            rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"))
        self.assertEqual(m.media, [])
        self.assertIsNone(list(m.containers.values())[0]["data"])

    def test_the_hashed_file_holds_back_the_last_block(self):
        path = self.file("t.mp4", b"a" * 5000)
        good = hashlib.sha256(b"a" * 5000).hexdigest()
        body = reels._HashedFile(path, 5000, good)
        got = b""
        while True:
            buf = body.read(1024)
            if not buf:
                break
            got += buf
        body.close()
        self.assertEqual(got, b"a" * 5000)
        for size, sha in ((5000, "0" * 64), (4999, good), (5001, good)):
            body = reels._HashedFile(path, size, sha)
            with self.assertRaises(reels._Changed):
                while body.read(1024):
                    pass
            body.close()

    def test_the_status_is_polled_a_minute_apart_until_finished(self):
        m = self.mock(processing=["IN_PROGRESS", "IN_PROGRESS", "FINISHED"])
        rc, doc, _ = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.kinds()[-4:], ["status", "status", "status", "publish"])
        self.assertEqual(self.waits, [60.0, 60.0])

    def test_error_and_expired_are_rejected_and_nothing_is_published(self):
        for state in ("ERROR", "EXPIRED"):
            m = self.mock(processing=["IN_PROGRESS", state])
            rc, doc, _ = self.confirmed()
            self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"), state)
            self.assertEqual(doc["error"]["details"]["status_code"], state)
            self.assertEqual(m.media, [])
        self.assertIn("ffmpeg-skill export --preset reels", self.confirmed_error_message("ERROR"))

    def confirmed_error_message(self, state):
        self.mock(processing=[state])
        return self.confirmed()[1]["error"]["message"]

    def test_still_in_progress_after_five_minutes_is_interrupted_and_nothing_is_published(self):
        m = self.mock(processing=["IN_PROGRESS"])
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_INTERRUPTED", True))
        self.assertEqual(m.kinds().count("status"), 5)                # "once per minute, for no more than 5 minutes"
        self.assertEqual(self.waits, [60.0] * 4)
        self.assertEqual(m.media, [])

    def test_reels_post_is_never_published_twice(self):
        # The publish answer is lost (the reel exists), Instagram answers 5xx, or code 1 ("API Unknown"): stop.
        for fault in (("lost",), ("status", 503, None, None), ("status", 400, 1, "An unknown error occurred")):
            m = self.mock()
            m.faults["publish"] = [fault]
            rc, doc, _ = self.confirmed()
            self.assertEqual((rc, doc["error"]["code"], doc["error"]["retryable"]), (4, "UPLOAD_INTERRUPTED", False))
            self.assertIn("Check the account", doc["error"]["message"])
            self.assertEqual(m.kinds().count("publish"), 1)

    def test_a_refused_token(self):
        m = self.mock()
        os.environ[TOKEN_VAR] = "EAAexpired-ig"
        rc, doc, text = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["details"]["error_code"]), (3, "AUTH_FAILED", 190))
        self.assertIn("publish auth login --platform reels", doc["error"]["message"])
        self.assertEqual(m.kinds(), ["container"])
        self.assertNotIn("EAAexpired-ig", text)

    def test_a_missing_permission_is_auth_failed(self):
        m = self.mock()
        m.faults["publish"] = [("status", 403, 10, "Application does not have permission for this action")]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (3, "AUTH_FAILED"))
        self.assertEqual(m.media, [])

    def test_the_publishing_limit_is_rejected_with_instagrams_message(self):
        m = self.mock()
        m.faults["publish"] = [("status", 400, 9, "Application request limit reached: 100 posts in 24 hours")]
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (4, "UPLOAD_REJECTED"))
        self.assertIn("100 posts", doc["error"]["message"])
        self.assertEqual(m.media, [])

    def test_an_account_id_that_is_not_numeric_is_refused_before_any_request(self):
        m = self.mock()
        os.environ[IG_VAR] = "@myaccount"
        rc, doc, _ = self.confirmed()
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        self.assertIn(IG_VAR, doc["error"]["message"])
        self.assertEqual(m.requests, [])


class ReelsMetadataTests(ReelsTestCase):
    def test_the_received_caption_reproduces_the_token(self):
        m = self.mock()
        flags = ("--title", "新作 Launch 🎬 #film @studio", "--privacy", "public")
        rc, dry = run_cli("upload", self.video, "--platform", "reels", *flags, "--json")
        rc, out, _ = run_cli_raw("upload", self.video, "--platform", "reels", *flags, "--confirm", dry["confirm_token"],
                                 "--json")
        doc = json.loads(out)
        self.assertEqual(rc, 0, doc)
        got = m.media[0]
        rebuilt = Metadata(title=got["fields"]["caption"], privacy="public")
        self.assertEqual(confirm_token("reels", hashlib.sha256(got["data"]).hexdigest(), len(got["data"]), rebuilt),
                         dry["confirm_token"])

    def test_private_and_unlisted_and_unsent_fields_are_refused_before_a_token(self):
        m = self.mock()
        srt = self.file("en.srt", b"1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        for flags, check in (((), "privacy"), (("--privacy", "private"), "privacy"),
                             (("--privacy", "unlisted"), "privacy"),
                             (("--description", "more") + PUBLIC, "description (not sent to Instagram Reels)"),
                             (("--tag", "film") + PUBLIC, "tags (not sent to Instagram Reels)"),
                             (("--thumbnail", self.file("t.png", PNG)) + PUBLIC,
                              "thumbnail (not sent to Instagram Reels)"),
                             (("--caption", srt, "--caption-language", "en") + PUBLIC,
                              "captions (not sent to Instagram Reels)"),
                             (("--title", "x" * 2201) + PUBLIC, "title length")):
            with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
                rc, doc = run_cli("upload", self.video, "--platform", "reels", *flags, "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "METADATA_INVALID"), flags)
            self.assertNotIn("confirm_token", json.dumps(doc))
            self.assertEqual([r["check"] for r in doc["error"]["details"]["failed"]], [check])
        self.assertEqual(m.requests, [])

    def test_a_file_reels_cannot_take_is_refused_before_a_token(self):
        m = self.mock()
        webm = self.file("final.webm", os.urandom(1000))
        with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
            rc, doc = run_cli("upload", webm, "--platform", "reels", *PUBLIC, "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INVALID_ARGUMENT"))
        self.assertEqual(m.requests, [])

    def test_container_fields_are_the_bound_metadata_only(self):
        self.assertEqual(reels.container_fields(Metadata(privacy="public").to_dict()),
                         {"media_type": "REELS", "upload_type": "resumable"})
        self.assertEqual(reels.container_fields(Metadata(title="hi", privacy="public").to_dict()),
                         {"media_type": "REELS", "upload_type": "resumable", "caption": "hi"})
        for md in (Metadata(), Metadata(privacy="unlisted"), Metadata(privacy="public", description="x")):
            with self.assertRaises(PublishError):
                reels.container_fields(md.to_dict())


class ReelsGateAndRedactionTests(ReelsTestCase):
    def test_every_refusal_happens_before_any_request_or_credential(self):
        m = self.mock()
        token = upload(self.video, "reels", metadata=build(privacy="public"))["confirm_token"]
        bad = [("upload", self.video, "--platform", "reels", *PUBLIC, "--confirm", token + "0"),
               ("upload", self.video, "--platform", "reels", *PUBLIC, "--confirm", token, "--title", "edited"),
               ("upload", self.video, "--platform", "reels", "--confirm", token),
               ("upload", self.video, "--platform", "reels", *PUBLIC)]
        with mock.patch.object(creds, "load", side_effect=AssertionError("credentials read")):
            for argv in bad:
                rc, out, _ = run_cli_raw(*argv, "--json")
                doc = json.loads(out)
                self.assertTrue(doc.get("dry_run") or rc == 2, (argv, doc))
        self.assertEqual(m.requests, [])

    def test_both_values_are_needed(self):
        m = self.mock()
        os.environ.pop(IG_VAR)
        token = upload(self.video, "reels", metadata=build(privacy="public"))["confirm_token"]
        rc, doc = run_cli("upload", self.video, "--platform", "reels", *PUBLIC, "--confirm", token, "--json")
        self.assertEqual((rc, doc["error"]["code"], doc["error"]["details"]["missing"]),
                         (3, "CREDENTIALS_MISSING", [IG_VAR]))
        self.assertEqual(m.requests, [])

    def test_the_values_from_the_keychain_upload_and_are_never_printed(self):
        for k, v in ((IG_VAR, IG), (TOKEN_VAR, TOKEN)):
            os.environ.pop(k)
            self.keychain_backend.items[("publish-skill", k)] = v
        m = self.mock()
        rc, doc, text = self.confirmed()
        self.assertEqual(rc, 0, doc)
        self.assertEqual(m.requests[1]["headers"]["authorization"], "OAuth " + TOKEN)
        self.assertNotIn(TOKEN, text)

    def test_receipts_and_errors_never_carry_the_token(self):
        scenarios = [{}, {"upload": [("cut", 1000)]}, {"publish": [("status", 403, 200, "Forbidden")]},
                     {"upload": [("status", 503, None, None)] * 30}, {"publish": [("lost",)]},
                     {"upload": [("debug", 400, False, "bad " + TOKEN)]}]
        for faults in scenarios:
            for human in (False, True):
                m = self.mock()
                m.faults.update({k: list(v) for k, v in faults.items()})
                with mock.patch.object(reels.ReelsUploader, "max_retries", 2):
                    rc, _, text = self.confirmed("--title", TOKEN[:12], human=human)
                self.assertNotIn(TOKEN, text)

    def test_human_receipt(self):
        self.mock()
        rc, _, text = self.confirmed("--title", "Launch", human=True)
        self.assertEqual(rc, 0, text)
        self.assertIn("to Instagram Reels: video 1780000000000000", text)
        self.assertIn("(privacy public)", text)
        self.assertNotIn("None", text)


class ReelsPlanTests(ReelsTestCase):
    def test_caption_rows(self):
        rows = {r["check"]: r for r in metadata_checks("reels", build(title="x" * 2200, privacy="public"))}
        self.assertEqual((rows["title length"]["status"], rows["title length"]["limit"]), ("PASS", 2200))
        self.assertEqual((rows["title hashtags"]["status"], rows["title @ tags"]["status"]), ("PASS", "PASS"))
        rows = {r["check"]: r for r in metadata_checks("reels", build(title="x" * 2201, privacy="public"))}
        self.assertEqual(rows["title length"]["status"], "FAIL")
        caption = " ".join(f"#t{i}" for i in range(31)) + " " + " ".join(f"@u{i}" for i in range(21)) + " a#b x@y.z"
        rows = {r["check"]: r for r in metadata_checks("reels", build(title=caption, privacy="public"))}
        self.assertEqual((rows["title hashtags"]["status"], rows["title hashtags"]["value"]), ("WARN", 31))
        self.assertEqual((rows["title @ tags"]["status"], rows["title @ tags"]["value"]), ("WARN", 21))
        self.assertIn("the platform's own count decides", rows["title hashtags"]["fix"])
        rows = {r["check"]: r for r in metadata_checks("reels", build(title="hi"))}
        self.assertEqual((rows["privacy"]["status"], rows["privacy"]["fix"]), ("FAIL", "--privacy one of public"))
        self.assertNotIn("title hashtags", {r["check"] for r in metadata_checks("x", build(title="#a", privacy="public"))})

    def test_file_and_duration_rows(self):
        rows = {r["check"]: r for r in file_checks("reels", "/x/a.mov", 300 * 1000 ** 2)}
        self.assertEqual((rows["file size"]["status"], rows["file format"]["status"]), ("PASS", "PASS"))
        rows = {r["check"]: r for r in file_checks("reels", "/x/a.mp4", 300 * 1000 ** 2 + 1)}
        self.assertEqual(rows["file size"]["status"], "FAIL")
        rows = {r["check"]: r for r in duration_checks("reels", 2.0)}
        self.assertEqual((rows["duration"]["status"], rows["duration (minimum)"]["status"]), ("PASS", "FAIL"))
        rows = {r["check"]: r for r in duration_checks("reels", 901.0)}
        self.assertEqual(rows["duration"]["status"], "FAIL")

    def test_plan_reads_ffmpeg_skills_reels_spec(self):
        self.assertEqual(get_platform("reels").ffmpeg_skill_platform, "reels")
        doc = plan(self.video, "reels", build(title="Launch", privacy="public"),
                   ffmpeg_skill_dir=os.path.join(self.tmp, "none"))
        self.assertEqual(doc["verdict"], "INCOMPLETE")
        self.assertEqual(doc["ffmpeg_skill"]["check_platform"], "reels")
        rows = {r["check"]: r for r in doc["checks"]}
        self.assertEqual((rows["file size"]["status"], rows["file size"]["limit"]), ("PASS", 300000000))
        self.assertFalse(doc["network_used"])

    def test_plan_fails_an_over_long_caption_offline(self):
        rc, doc = run_cli("plan", self.video, "--platform", "reels", "--title", "x" * 2201, *PUBLIC, "--json")
        self.assertEqual((rc, doc["verdict"]), (1, "FAIL"))


class ReelsLimitsTableTests(OfflineTestCase):
    def test_reels_values_as_researched(self):
        # Read from the pages cited in limits.py on 2026-10-31; change only together with the quote and the date.
        want = {"video.max_bytes": 300000000, "video.min_duration_s": 3, "video.max_duration_s": 900,
                "video.file_formats": [".mp4", ".mov"], "title.max_chars": 2200, "title.max_hashtags": 30,
                "title.max_mentions": 20, "privacy.values": ["public"], "quota.daily_posts": 100,
                "upload.session_validity_s": 86400}
        for key, v in want.items():
            self.assertEqual(L.value("reels", key), v, key)
        for key in ("title.forbidden_chars", "rate_limit.requests", "token.lifetime_s"):
            self.assertEqual(L.limits_of("reels")[key]["status"], "unknown", key)
        for key in ("video.codecs", "video.resolution", "video.aspect", "video.fps"):
            self.assertEqual(L.limits_of("reels")[key]["status"], "delegated", key)
        self.assertEqual(L.value("reels", "video.file_formats"), list(reels.FILE_FORMATS))
        for e in L.limits_of("reels").values():
            for url in [e.get("source")] + list(e.get("searched") or []):
                if url:
                    self.assertTrue(url.startswith("https://developers.facebook.com/docs/instagram-platform/"), url)
