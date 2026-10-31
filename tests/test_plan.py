"""`publish plan` (roadmap step 2, ADR-002): the upload-limit table and its citations, metadata and the confirmation
token, the ffmpeg-skill adapter against fake checkouts, plan's rows and verdicts, and an integration run against a
real ffmpeg-skill checkout when one and FFmpeg are present (CI's conformance job provides both, so nothing skips
there). Every test runs with socket connections patched to fail; the only processes started are ffmpeg-skill's
scripts (fake ones here), which use no network."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from helpers import FFMPEG_SKILL_DIR, ROOT, OfflineTestCase, env, run_cli, run_cli_raw, youtube_mock

from publish_skill import adapter
from publish_skill import limits as L
from publish_skill.contract import contract
from publish_skill.errors import PublishError
from publish_skill.metadata import DEFAULT_METADATA, Metadata, build
from publish_skill.plan import duration_checks, metadata_checks, plan, verdict
from publish_skill.upload import confirm_token, upload

YT_VARS = ("PUBLISH_YOUTUBE_CLIENT_ID", "PUBLISH_YOUTUBE_CLIENT_SECRET", "PUBLISH_YOUTUBE_REFRESH_TOKEN")
SECRETS = {"PUBLISH_YOUTUBE_CLIENT_ID": "cid-7f3a91e2.apps.example",
           "PUBLISH_YOUTUBE_CLIENT_SECRET": "csecret-Zq81LmP0sW",
           "PUBLISH_YOUTUBE_REFRESH_TOKEN": "1//refresh-XyZ0987654321"}
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
OFFICIAL = ("https://developers.google.com/youtube/", "https://support.google.com/youtube/", "https://developer.vimeo.com/",
            "https://developers.tiktok.com/doc/", "https://docs.x.com/",
            "https://learn.microsoft.com/en-us/linkedin/")

# A minimal ffmpeg-skill contract with what the adapter needs (`export --list`, `check --platform ... --json
# --no-loudness`, execution.network false). Tests edit a copy to make it unsupported.
FAKE_CONTRACT = {
    "skill": {"id": "ffmpeg-skill", "version": "9.9.9"}, "contract_version": "1.0",
    "execution": {"network": False, "shell": False},
    "tools": [{"name": "export", "input_schema": {"properties": {"input": {}, "preset": {}, "list": {}}}},
              {"name": "check", "input_schema": {"properties": {"input": {}, "json": {}, "no_loudness": {},
                                                                "platform": {"enum": ["shorts", "youtube"]}}}}],
}
# check.py stand-in: a 16:9 file passes; a file whose name contains "wide" FAILs aspect; "long" reports 13 hours;
# "broken" prints a failure document; "slow" sleeps past the timeout tests set.
FAKE_CHECK = textwrap.dedent('''
    import json, os, sys, time
    path, platform = sys.argv[1], sys.argv[sys.argv.index("--platform") + 1]
    assert "--json" in sys.argv and "--no-loudness" in sys.argv
    name = os.path.basename(path)
    if "slow" in name:
        time.sleep(5)
    if "broken" in name:
        print(json.dumps({"status": "failed", "error": {"message": "input not found: " + path}}))
        sys.exit(1)
    rows = [{"check": "duration", "status": "PASS", "value": "46800.00s" if "long" in name else "12.50s", "expected": "<= 43200s",
             "kind": "judgement"},
            {"check": "aspect", "status": "FAIL" if "wide" in name else "PASS", "value": "5:2" if "wide" in name else "16:9",
             "expected": "16:9/9:16/1:1/4:3", "fix": "fit.py --aspect 16:9 --fit pad", "kind": "judgement"},
            {"check": "video codec", "status": "PASS", "value": "h264", "expected": "h264/hevc", "kind": "format"}]
    failed = sum(r["status"] == "FAIL" for r in rows)
    print(json.dumps({"status": "completed" if not failed else "failed", "platform": platform, "checks": rows,
                      "failed": failed, "ok": not failed}))
    sys.exit(1 if failed else 0)
''')
FAKE_EXPORT = textwrap.dedent('''
    import sys
    assert sys.argv[1:] == ["--list"], sys.argv
    print("youtube    1080p H.264, AAC 192k")
    print("shorts     9:16 1080x1920, 30fps, max 180s (YouTube Shorts)")
''')


def make_fake_ffmpeg_skill(root: str, contract_doc=None, export_src: str = FAKE_EXPORT) -> str:
    d = Path(root) / "ffmpeg-skill"
    (d / "scripts").mkdir(parents=True)
    doc = FAKE_CONTRACT if contract_doc is None else contract_doc
    (d / "scripts" / "_contract.py").write_text(
        "import json, sys\nassert sys.argv[1:] == ['--json', '--static'], sys.argv\nprint(json.dumps(%r))\n" % (doc,),
        encoding="utf-8")
    (d / "scripts" / "check.py").write_text(FAKE_CHECK, encoding="utf-8")
    (d / "scripts" / "export.py").write_text(export_src, encoding="utf-8")
    return str(d)


def rows_by(doc_or_rows, name):
    rows = doc_or_rows["checks"] if isinstance(doc_or_rows, dict) else doc_or_rows
    return [r for r in rows if r["check"] == name]


class LimitsTableTests(unittest.TestCase):
    def test_every_documented_value_cites_an_official_page_and_a_date(self):
        for pid, table in L.LIMITS.items():
            for key, e in table.items():
                self.assertIn(e["status"], ("documented", "unknown", "delegated"), key)
                if e["status"] == "documented":
                    self.assertIsNotNone(e["value"], key)
                    self.assertTrue(e["source"].startswith(OFFICIAL), f"{key}: {e['source']}")
                    self.assertTrue(re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["accessed"]), key)
                    self.assertGreater(len(e["quote"]), 15, key)
                elif e["status"] == "unknown":
                    self.assertIsNone(e["value"], key)                 # never invented
                    self.assertTrue(e["searched"] and all(u.startswith(OFFICIAL) for u in e["searched"]), key)
                    self.assertTrue(e["note"], key)
                else:
                    self.assertIsNone(e["value"], key)                 # ffmpeg-skill's value is never restated here
                    self.assertEqual(e["owner"], "ffmpeg-skill", key)

    def test_youtube_values_as_researched(self):
        # Read from the pages cited in limits.py on 2026-10-31; change only together with the quote and the date.
        want = {"video.max_bytes": 256 * 10 ** 9, "video.max_duration_s": 43200, "video.unverified_max_duration_s": 900,
                "title.max_chars": 100, "description.max_bytes": 5000, "tags.max_chars": 500,
                "privacy.values": ["private", "public", "unlisted"], "thumbnail.max_bytes": 50 * 10 ** 6,
                "thumbnail.types": ["image/jpeg", "image/png"], "captions.max_bytes": 100 * 10 ** 6,
                "title.forbidden_chars": ["<", ">"], "description.forbidden_chars": ["<", ">"]}
        for key, v in want.items():
            self.assertEqual(L.value("youtube", key), v, key)
        for key in ("video.codecs", "video.resolution", "video.aspect", "video.fps"):
            self.assertEqual(L.limits_of("youtube")[key]["status"], "delegated")
        for key in ("tags.max_chars_per_tag", "tags.max_count", "uploads.per_user"):
            self.assertIsNone(L.value("youtube", key))
        # every quote contains the number it justifies
        for key in ("title.max_chars", "description.max_bytes", "tags.max_chars"):
            self.assertIn(str(L.value("youtube", key)), L.limits_of("youtube")[key]["quote"], key)

    def test_readme_table_lists_every_checked_limit_with_its_source(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for pid, name in (("youtube", "YouTube"), ("vimeo", "Vimeo"), ("tiktok", "TikTok")):
            heading = f"### Upload limits: {name}\n"
            section = re.split(r"\n#{2,3} ", readme.split(heading, 1)[1], maxsplit=1)[0]      # up to the next heading
            for key, e in L.limits_of(pid).items():
                self.assertIn(f"`{key}`", section, (pid, key))
                if e["status"] == "documented":
                    self.assertIn(e["source"], section, (pid, key))

    def test_every_registered_platform_has_a_table(self):
        from publish_skill.platforms import platform_ids
        self.assertEqual(sorted(L.LIMITS), platform_ids())
        self.assertEqual(contract()["upload_limits"]["platforms"], L.LIMITS)

    def test_tags_length_counts_commas_and_quotes(self):
        self.assertEqual(L.tags_length([]), 0)
        self.assertEqual(L.tags_length(["abc"]), 3)
        self.assertEqual(L.tags_length(["abc", "de"]), 6)               # one comma
        self.assertEqual(L.tags_length(["a b", "c"]), 3 + 2 + 1 + 1)    # quotes around "a b", one comma


class MetadataTokenTests(OfflineTestCase):
    def _file(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_defaults(self):
        md = build()
        self.assertEqual(md, DEFAULT_METADATA)
        self.assertEqual(md.privacy, "private")                       # never the API's default (public)
        self.assertEqual(confirm_token("youtube", "a" * 64, 1), confirm_token("youtube", "a" * 64, 1, md))

    def test_every_metadata_field_changes_the_token(self):
        thumb, thumb2 = self._file("t.png", PNG), self._file("t2.png", PNG + b"x")
        cap, cap2 = self._file("a.srt", b"1\n00:00:00,000 --> 00:00:01,000\nhi\n"), self._file("b.srt", b"x")
        base = dict(title="T", description="D", tags=["a", "b"], privacy="unlisted", thumbnail=thumb, captions=[cap, cap2],
                    caption_languages=["en", "ja"])
        t0 = confirm_token("youtube", "a" * 64, 10, build(**base))
        for field, other in (("title", "T2"), ("description", "D2"), ("tags", ["b", "a"]), ("privacy", "public"),
                             ("thumbnail", thumb2), ("captions", [cap2, cap]), ("caption_languages", ["ja", "en"]),
                             ("caption_languages", ["en", "en-GB"])):
            self.assertNotEqual(t0, confirm_token("youtube", "a" * 64, 10, build(**dict(base, **{field: other}))), field)
        self.assertEqual(t0, confirm_token("youtube", "a" * 64, 10, build(**base)))
        moved = shutil.copy(thumb, os.path.join(self.tmp, "renamed.png"))      # bound by bytes, not path
        self.assertEqual(t0, confirm_token("youtube", "a" * 64, 10, build(**dict(base, thumbnail=moved))))

    def test_editing_metadata_after_the_dry_run_needs_a_new_token(self):
        rc, dry = run_cli("upload", self.video, "--platform", "youtube", "--title", "First", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(dry["metadata"]["title"], "First")
        with env(**SECRETS):
            rc, doc = run_cli("upload", self.video, "--platform", "youtube", "--title", "Edited", "--confirm",
                              dry["confirm_token"], "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"))
            rc, doc = run_cli("upload", self.video, "--platform", "youtube", "--confirm", dry["confirm_token"], "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "CONFIRM_MISMATCH"))     # dropping the title is an edit too
            m = youtube_mock(self, SECRETS)
            rc, doc = run_cli("upload", self.video, "--platform", "youtube", "--title", "First", "--confirm",
                              dry["confirm_token"], "--json")
        self.assertEqual((rc, doc["uploaded"]), (0, True))                              # the same metadata goes through
        self.assertEqual(m.videos[doc["result"]["video_id"]]["resource"],
                         {"snippet": {"title": "First"}, "status": {"privacyStatus": "private"}})

    def test_upload_refuses_metadata_that_breaks_a_limit(self):
        for argv in (("--title", "x" * 101), ("--privacy", "secret"), ("--title", "a <b>")):
            rc, doc = run_cli("upload", self.video, "--platform", "youtube", *argv, "--json")
            self.assertEqual((rc, doc["error"]["code"]), (2, "METADATA_INVALID"), argv)
            self.assertNotIn("confirm_token", json.dumps(doc))
            self.assertFalse(doc["error"]["details"]["uploaded"])
        with self.assertRaises(PublishError) as e:
            upload(self.video, "youtube", metadata=Metadata(title="x" * 101))
        self.assertEqual(e.exception.details["failed"][0]["check"], "title length")

    def test_bad_metadata_inputs(self):
        with self.assertRaises(PublishError) as e:
            build(description="a", description_file="b")
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(PublishError) as e:
            build(thumbnail=os.path.join(self.tmp, "missing.png"))
        self.assertEqual((e.exception.code, e.exception.details["input"]), ("INPUT_NOT_FOUND", "thumbnail"))
        latin1 = self._file("d.txt", "caf\xe9".encode("latin-1"))
        with self.assertRaises(PublishError) as e:
            build(description_file=latin1)
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        cap = self._file("c.srt", b"1\n")
        with self.assertRaises(PublishError) as e:                     # one language per caption file, in order
            build(captions=[cap], caption_languages=["en", "ja"])
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(PublishError) as e:
            build(caption_languages=["en"])
        self.assertEqual(e.exception.code, "INVALID_ARGUMENT")
        self.assertEqual(build(captions=[cap], caption_languages=["en"]).captions[0]["language"], "en")
        utf8 = self._file("d8.txt", "caf\xe9 動画".encode("utf-8"))
        self.assertEqual(build(description_file=utf8).description, "caf\xe9 動画")


class MetadataRowTests(OfflineTestCase):
    def _file(self, name, data):
        p = os.path.join(self.tmp, name)
        with open(p, "wb") as fh:
            fh.write(data)
        return p

    def test_title(self):
        self.assertEqual(rows_by(metadata_checks("youtube", Metadata(title="x" * 100)), "title length")[0]["status"], "PASS")
        row = rows_by(metadata_checks("youtube", Metadata(title="x" * 101)), "title length")[0]
        self.assertEqual((row["status"], row["value"], row["limit"]), ("FAIL", 101, 100))
        self.assertEqual(row["source"], L.limits_of("youtube")["title.max_chars"]["source"])
        self.assertEqual(row["accessed"], L.ACCESSED)
        self.assertIn("100", row["fix"])
        self.assertEqual(rows_by(metadata_checks("youtube", Metadata(title="動" * 100)), "title length")[0]["status"],
                         "PASS")                                       # characters, not bytes
        self.assertEqual(rows_by(metadata_checks("youtube", Metadata(title="a > b")), "title characters")[0]["status"], "FAIL")
        self.assertEqual(rows_by(metadata_checks("youtube", Metadata()), "title")[0]["status"], "WARN")

    def test_description_counts_utf8_bytes(self):
        ok = rows_by(metadata_checks("youtube", Metadata(description="a" * 5000)), "description length")[0]
        self.assertEqual(ok["status"], "PASS")
        jp = rows_by(metadata_checks("youtube", Metadata(description="動" * 2000)), "description length")[0]
        self.assertEqual((jp["status"], jp["value"]), ("FAIL", 6000))   # 2000 characters, 6000 bytes
        self.assertEqual(rows_by(metadata_checks("youtube", Metadata(description="<b>")), "description characters")[0]
                         ["status"], "FAIL")

    def test_tags_and_privacy(self):
        at_limit = ["x" * 99] * 5                                      # 495 + 4 commas = 499
        self.assertEqual(rows_by(metadata_checks("youtube", Metadata(tags=at_limit)), "tags length")[0]["status"], "PASS")
        over = ["x x" + "y" * 97] * 5                                  # 100 + 2 quotes each, + 4 commas = 514
        row = rows_by(metadata_checks("youtube", Metadata(tags=over)), "tags length")[0]
        self.assertEqual((row["status"], row["value"]), ("FAIL", 514))
        self.assertEqual(rows_by(metadata_checks("youtube", Metadata()), "privacy")[0]["status"], "PASS")
        self.assertEqual(rows_by(metadata_checks("youtube", Metadata(privacy="Public")), "privacy")[0]["status"], "FAIL")

    def test_thumbnail(self):
        for data, want in ((PNG, "PASS"), (JPEG, "PASS"), (GIF, "FAIL")):
            md = build(thumbnail=self._file("t.img", data))
            self.assertEqual(rows_by(metadata_checks("youtube", md), "thumbnail type")[0]["status"], want, data[:4])
        md = build(thumbnail=self._file("big.png", PNG))
        with mock.patch.dict(L.LIMITS["youtube"]["thumbnail.max_bytes"], {"value": 10}):
            self.assertEqual(rows_by(metadata_checks("youtube", md), "thumbnail size")[0]["status"], "FAIL")
        self.assertEqual(rows_by(metadata_checks("youtube", md), "thumbnail size")[0]["status"], "PASS")

    def test_captions(self):
        md = build(captions=[self._file("en.srt", "1\n00:00:00,000 --> 00:00:01,000\ncaf\xe9\n".encode("utf-8")),
                             self._file("fr.srt", "caf\xe9".encode("latin-1")), self._file("x.weird", b"x")])
        rows = metadata_checks("youtube", md)
        self.assertEqual(rows_by(rows, "captions encoding (en.srt)")[0]["status"], "PASS")
        self.assertEqual(rows_by(rows, "captions encoding (fr.srt)")[0]["status"], "FAIL")
        self.assertEqual(rows_by(rows, "captions format (x.weird)")[0]["status"], "WARN")
        self.assertEqual(rows_by(rows, "captions size (en.srt)")[0]["status"], "PASS")
        self.assertEqual(rows_by(rows, "captions language (en.srt)")[0]["status"], "FAIL")   # captions.insert needs one

    def test_captions_language(self):
        srt = self._file("en.srt", b"1\n00:00:00,000 --> 00:00:01,000\nhi\n")
        for lang, want in (("en", "PASS"), ("en-US", "PASS"), ("zh-Hant", "PASS"), ("es-419", "PASS"), ("ja", "PASS"),
                           ("", "FAIL"), ("english language", "FAIL"), ("e", "FAIL"), ("en_US", "FAIL"), ("-en", "FAIL")):
            md = build(captions=[srt], caption_languages=[lang])
            row = rows_by(metadata_checks("youtube", md), "captions language (en.srt)")[0]
            self.assertEqual(row["status"], want, lang)
            self.assertEqual(row["source"], "https://developers.google.com/youtube/v3/docs/captions")
        rc, doc = run_cli("upload", self.video, "--platform", "youtube", "--caption", srt, "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "METADATA_INVALID"))      # no token for captions without a language
        rc, doc = run_cli("upload", self.video, "--platform", "youtube", "--caption", srt, "--caption-language", "en", "--json")
        self.assertEqual((rc, doc["metadata"]["captions"][0]["language"]), (0, "en"))

    def test_duration_rows(self):
        self.assertEqual([r["status"] for r in duration_checks("youtube", 60.0)], ["PASS"])
        self.assertEqual([r["status"] for r in duration_checks("youtube", 16 * 60.0)], ["PASS", "WARN"])   # unverified
        self.assertEqual([r["status"] for r in duration_checks("youtube", 12 * 3600 + 1.0)], ["FAIL"])
        self.assertEqual([r["status"] for r in duration_checks("youtube", None, "why")], ["UNCHECKED"])

    def test_verdict_order(self):
        rows = lambda *s: [{"status": x} for x in s]   # noqa: E731
        self.assertEqual(verdict(rows("PASS")), "PASS")
        self.assertEqual(verdict(rows("PASS", "WARN")), "WARN")
        self.assertEqual(verdict(rows("WARN", "UNCHECKED")), "INCOMPLETE")
        self.assertEqual(verdict(rows("UNCHECKED", "FAIL")), "FAIL")


class AdapterTests(OfflineTestCase):
    def test_check_contract(self):
        self.assertEqual(adapter.check_contract(FAKE_CONTRACT, "youtube"), ([], {"check_platforms": ["shorts", "youtube"]}))
        doc = json.loads(json.dumps(FAKE_CONTRACT))
        del doc["tools"][0]["input_schema"]["properties"]["list"]
        doc["execution"]["network"] = True
        problems, _ = adapter.check_contract(doc, "tiktok")
        self.assertEqual(len(problems), 3, problems)       # export lacks --list, check lacks tiktok, network not false
        problems, _ = adapter.check_contract({"tools": [], "execution": {"network": False}})
        self.assertEqual(problems, ["ffmpeg-skill tool 'export' is missing", "ffmpeg-skill tool 'check' is missing"])

    def test_parse_export_list(self):
        self.assertEqual(adapter.parse_export_list("youtube    1080p\nyoutube-hdr HEVC\n\n"), ["youtube", "youtube-hdr"])

    def test_detect_fake_checkout(self):
        d = adapter.detect(make_fake_ffmpeg_skill(self.tmp), "youtube")
        self.assertEqual((d["status"], d["version"], d["contract_version"]), ("OK", "9.9.9", "1.0"))
        self.assertEqual(d["export_presets"], ["youtube", "shorts"])

    def test_detect_unsupported_checkouts(self):
        bad = json.loads(json.dumps(FAKE_CONTRACT))
        bad["execution"]["network"] = True
        self.assertEqual(adapter.detect(make_fake_ffmpeg_skill(os.path.join(self.tmp, "a"), bad))["status"], "UNSUPPORTED")
        broken = make_fake_ffmpeg_skill(os.path.join(self.tmp, "b"))
        Path(broken, "scripts", "_contract.py").write_text("print('not json')\n", encoding="utf-8")
        d = adapter.detect(broken)
        self.assertEqual((d["status"], d["reason"]), ("UNSUPPORTED", "output_unparsed"))
        failing = make_fake_ffmpeg_skill(os.path.join(self.tmp, "c"), export_src="import sys\nsys.exit(3)\n")
        self.assertEqual(adapter.detect(failing)["problems"], ["ffmpeg-skill/export --list exited 3"])

    def test_child_environment_is_minimal(self):
        with env(PUBLISH_YOUTUBE_CLIENT_SECRET=SECRETS["PUBLISH_YOUTUBE_CLIENT_SECRET"], SOME_TOKEN="t"):
            child = adapter._clean_env()
        self.assertNotIn("PUBLISH_YOUTUBE_CLIENT_SECRET", child)         # credentials never reach ffmpeg-skill
        self.assertNotIn("SOME_TOKEN", child)
        self.assertEqual(child["PYTHONUTF8"], "1")


class PlanTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.ff = make_fake_ffmpeg_skill(self.tmp)

    def _video(self, name):
        p = os.path.join(self.tmp, name)
        shutil.copy(self.video, p)
        return p

    def test_a_good_deliverable_passes_offline(self):
        with env(**{k: None for k in YT_VARS}):
            rc, doc = run_cli("plan", self.video, "--platform", "youtube", "--title", "A title", "--ffmpeg-skill", self.ff, "--json")
        self.assertEqual((rc, doc["verdict"], doc["ok"]), (0, "PASS", True))
        self.assertEqual((doc["uploaded"], doc["network_used"], doc["schema"]), (False, False, "publish/plan@1"))
        self.assertEqual(doc["ffmpeg_skill"]["status"], "OK")
        self.assertEqual(doc["ffmpeg_skill"]["export_preset"], "youtube")
        self.assertEqual(doc["counts"], {"pass": len(doc["checks"]), "warn": 0, "fail": 0, "unchecked": 0})
        owners = {r["owner"] for r in doc["checks"]}
        self.assertEqual(owners, {"publish", "ffmpeg-skill"})
        self.assertEqual(rows_by(doc, "duration")[0]["value"], 12.5)        # measured by ffmpeg-skill, limit from limits.py
        self.assertEqual(doc["metadata"]["privacy"], "private")

    def test_done_when_over_long_title_is_a_structured_fail(self):
        # Roadmap step 2, "Done when: structured FAIL on an over-long title or wrong aspect, no network".
        rc, doc = run_cli("plan", self.video, "--platform", "youtube", "--title", "x" * 101, "--ffmpeg-skill", self.ff, "--json")
        self.assertEqual((rc, doc["verdict"], doc["ok"]), (1, "FAIL", False))
        row = rows_by(doc, "title length")[0]
        self.assertEqual((row["status"], row["value"], row["limit"], row["owner"]), ("FAIL", 101, 100, "publish"))
        self.assertTrue(row["source"].startswith(OFFICIAL) and row["fix"])

    def test_done_when_wrong_aspect_is_a_structured_fail(self):
        rc, doc = run_cli("plan", self._video("wide.mp4"), "--platform", "youtube", "--title", "ok", "--ffmpeg-skill", self.ff,
                          "--json")
        self.assertEqual((rc, doc["verdict"]), (1, "FAIL"))
        row = rows_by(doc, "aspect")[0]
        self.assertEqual((row["status"], row["value"], row["owner"]), ("FAIL", "5:2", "ffmpeg-skill"))
        self.assertEqual(row["source"], "ffmpeg-skill/check --platform youtube")
        self.assertIn("fit.py", row["fix"])

    def test_too_long_for_the_platform(self):
        rc, doc = run_cli("plan", self._video("long.mp4"), "--platform", "youtube", "--ffmpeg-skill", self.ff, "--json")
        self.assertEqual(doc["verdict"], "FAIL")
        mine = [r for r in rows_by(doc, "duration") if r["owner"] == "publish"][0]
        self.assertEqual((mine["status"], mine["value"], mine["limit"]), ("FAIL", 46800.0, 43200))

    def test_without_ffmpeg_skill_the_plan_is_incomplete_not_passed(self):
        rc, doc = run_cli("plan", self.video, "--platform", "youtube", "--title", "ok", "--ffmpeg-skill",
                          os.path.join(self.tmp, "none"), "--json")
        self.assertEqual((rc, doc["verdict"], doc["ok"]), (1, "INCOMPLETE", False))
        self.assertEqual(doc["ffmpeg_skill"]["status"], "MISSING")
        self.assertEqual({r["check"] for r in doc["checks"] if r["status"] == "UNCHECKED"}, {"duration", "delivery spec"})
        # metadata FAILs still win over the missing dependency
        rc, doc = run_cli("plan", self.video, "--platform", "youtube", "--title", "x" * 101, "--ffmpeg-skill",
                          os.path.join(self.tmp, "none"), "--json")
        self.assertEqual(doc["verdict"], "FAIL")

    def test_unsupported_or_failing_ffmpeg_skill_is_unchecked(self):
        bad = json.loads(json.dumps(FAKE_CONTRACT))
        bad["tools"][1]["input_schema"]["properties"]["platform"]["enum"] = ["shorts"]
        ff = make_fake_ffmpeg_skill(os.path.join(self.tmp, "old"), bad)
        rc, doc = run_cli("plan", self.video, "--platform", "youtube", "--ffmpeg-skill", ff, "--json")
        self.assertEqual((rc, doc["verdict"], doc["ffmpeg_skill"]["status"]), (1, "INCOMPLETE", "UNSUPPORTED"))
        rc, doc = run_cli("plan", self._video("broken.mp4"), "--platform", "youtube", "--ffmpeg-skill", self.ff, "--json")
        self.assertEqual(doc["verdict"], "INCOMPLETE")
        self.assertEqual(doc["ffmpeg_skill"]["error"]["reason"], "check_failed")
        self.assertIn("input not found", rows_by(doc, "delivery spec")[0]["fix"])

    def test_a_hanging_check_times_out(self):
        with mock.patch.object(adapter, "CHECK_TIMEOUT", 0.5):
            doc = plan(self._video("slow.mp4"), "youtube", ffmpeg_skill_dir=self.ff)
        self.assertEqual((doc["verdict"], doc["ffmpeg_skill"]["error"]["reason"]), ("INCOMPLETE", "timeout"))

    def test_file_format_warns_on_an_unlisted_extension(self):
        doc = plan(self._video("final.mkv"), "youtube", Metadata(title="ok"), ffmpeg_skill_dir=self.ff)
        self.assertEqual((rows_by(doc, "file format")[0]["status"], doc["verdict"]), ("WARN", "WARN"))

    def test_refusals(self):
        rc, doc = run_cli("plan", self.video, "--platform", "myspace", "--thumbnail", "missing.png", "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "UNKNOWN_PLATFORM"))       # before any file is read
        rc, doc = run_cli("plan", os.path.join(self.tmp, "nope.mp4"), "--platform", "youtube", "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INPUT_NOT_FOUND"))
        rc, doc = run_cli("plan", self.video, "--platform", "youtube", "--caption", os.path.join(self.tmp, "x.srt"), "--json")
        self.assertEqual((rc, doc["error"]["code"]), (2, "INPUT_NOT_FOUND"))

    def test_plan_reads_no_credentials_and_leaves_the_input_alone(self):
        before = (Path(self.video).read_bytes(), os.path.getmtime(self.video))
        with env(**SECRETS), mock.patch("publish_skill.credentials.load", side_effect=AssertionError("credentials read")):
            rc, out, err = run_cli_raw("plan", self.video, "--platform", "youtube", "--ffmpeg-skill", self.ff, "--json")
        self.assertEqual(rc, 0)
        for s in SECRETS.values():
            self.assertNotIn(s, out + err)
        self.assertEqual(before, (Path(self.video).read_bytes(), os.path.getmtime(self.video)))

    def test_human_output(self):
        rc, out, _ = run_cli_raw("plan", self.video, "--platform", "youtube", "--title", "x" * 101, "--ffmpeg-skill", self.ff)
        self.assertEqual(rc, 1)
        self.assertIn("FAIL (nothing uploaded)", out)
        self.assertIn("title length", out)

    def test_contract_declares_plan(self):
        c = contract()
        tool = {t["tool_id"]: t for t in c["tools"]}["publish/plan"]
        self.assertTrue(tool["implemented"])
        self.assertFalse(tool["network"])
        self.assertIn("METADATA_INVALID", c["errors"])
        self.assertIn("title", c["confirmation"]["token_binds"])
        dep = c["dependencies"][0]
        self.assertEqual(dep["tools_used"], ["export", "check"])
        self.assertIn("--list", dep["version_range"])


def _have_real_ffmpeg_skill() -> bool:
    return bool(FFMPEG_SKILL_DIR and shutil.which("ffmpeg") and shutil.which("ffprobe") and sys.version_info >= (3, 10))


@unittest.skipUnless(_have_real_ffmpeg_skill(), "needs an ffmpeg-skill checkout (PUBLISH_FFMPEG_SKILL_DIR or "
                     "../ffmpeg-skill), FFmpeg on PATH and Python >= 3.10 (CI's conformance job has all three)")
class RealFfmpegSkillTests(OfflineTestCase):
    def _make(self, name, size):
        out = os.path.join(self.tmp, name)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=s={size}:d=1:r=30", "-f", "lavfi",
                        "-i", "sine=d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out],
                       check=True, timeout=120)
        return out

    def test_capabilities_are_detected(self):
        d = adapter.detect(str(FFMPEG_SKILL_DIR), "youtube")
        self.assertEqual(d["status"], "OK", d["problems"])
        self.assertIn("youtube", d["check_platforms"])
        self.assertIn("youtube", d["export_presets"])

    def test_wrong_aspect_and_over_long_title_fail_through_the_real_check(self):
        doc = plan(self._make("wide.mp4", "800x320"), "youtube", Metadata(title="x" * 101), ffmpeg_skill_dir=str(FFMPEG_SKILL_DIR))
        self.assertEqual(doc["verdict"], "FAIL")
        self.assertEqual(rows_by(doc, "aspect")[0]["status"], "FAIL")
        self.assertEqual(rows_by(doc, "title length")[0]["status"], "FAIL")
        mine = [r for r in rows_by(doc, "duration") if r["owner"] == "publish"][0]
        self.assertEqual(mine["status"], "PASS")                     # the duration ffmpeg-skill measured, checked here

    def test_a_16_9_file_does_not_fail(self):
        doc = plan(self._make("ok.mp4", "1280x720"), "youtube", Metadata(title="ok"), ffmpeg_skill_dir=str(FFMPEG_SKILL_DIR))
        self.assertIn(doc["verdict"], ("PASS", "WARN"), [r for r in doc["checks"] if r["status"] not in ("PASS", "WARN")])
        self.assertEqual(rows_by(doc, "aspect")[0]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
