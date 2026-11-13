"""Roadmap step 12 (ADR-015): this skill's duration and file-size limits decide `publish plan`'s verdict; ffmpeg-skill
`check`'s duration and file-size rows are shown as guidance and left out of the verdict, while its other rows (codec,
frame size, aspect, frame rate) still count. Runs against fake ffmpeg-skill checkouts, offline."""
from __future__ import annotations

import json
import os
import re
import shutil
import textwrap
from pathlib import Path

from helpers import OfflineTestCase, run_cli, run_cli_raw

from test_plan import FAKE_CONTRACT, make_fake_ffmpeg_skill, rows_by

from publish_skill import limits as L
from publish_skill.contract import contract
from publish_skill.metadata import Metadata
from publish_skill.plan import GUIDANCE_CHECKS, plan

# check.py stand-in for X with ffmpeg-skill's 140 s and 512 MB table rows. The file name picks what it reports:
# "min<N>" a duration of N minutes; "new" the current ffmpeg-skill (a table limit over is WARN with basis guidance),
# otherwise an older one (FAIL); "hevc" FAILs the video codec; "wide" FAILs aspect; "nolen" cannot measure the length;
# "big" reports a file over 512 MB.
FAKE_X_CHECK = textwrap.dedent('''
    import json, os, re, sys
    path, platform = sys.argv[1], sys.argv[sys.argv.index("--platform") + 1]
    name = os.path.basename(path)
    new = "new" in name
    m = re.search(r"min([0-9]+)", name)
    dur = 60.0 * int(m.group(1)) if m else 12.5
    def limit_row(check, over, value, expected):
        status = ("WARN" if new else "FAIL") if over else "PASS"
        row = {"check": check, "status": status, "value": value, "expected": expected, "fix": "cut.py", "kind": "judgement"}
        if new:
            row["basis"] = "guidance"
        return row
    rows = [limit_row("duration", "nolen" in name or dur > 140, "could not measure" if "nolen" in name else "%.2fs" % dur,
                      "<= 140s"),
            {"check": "aspect", "status": "FAIL" if "wide" in name else "PASS", "value": "5:2" if "wide" in name else "16:9",
             "expected": "16:9/1:1/9:16", "fix": "fit.py --aspect 16:9", "kind": "judgement"},
            {"check": "video codec", "status": "FAIL" if "hevc" in name else "PASS", "value": "hevc" if "hevc" in name else "h264",
             "expected": "h264", "fix": "export.py --preset x", "kind": "format"},
            {"check": "fps", "status": "PASS", "value": "30", "expected": "<= 60", "kind": "judgement"},
            limit_row("file size", "big" in name, "600.0 MB" if "big" in name else "1.0 MB", "<= 512 MB")]
    failed = sum(r["status"] == "FAIL" for r in rows)
    print(json.dumps({"status": "completed" if not failed else "failed", "platform": platform, "checks": rows,
                      "failed": failed, "ok": not failed}))
    sys.exit(1 if failed else 0)
''')
X_ARGS = ("--platform", "x", "--title", "A clip", "--privacy", "public")


def fake_x_ffmpeg_skill(root: str) -> str:
    doc = json.loads(json.dumps(FAKE_CONTRACT))
    doc["tools"][1]["input_schema"]["properties"]["platform"]["enum"] = ["shorts", "youtube", "x", "facebook"]
    d = make_fake_ffmpeg_skill(root, doc)
    (Path(d) / "scripts" / "check.py").write_text(FAKE_X_CHECK, encoding="utf-8")
    return d


class LimitsVerdictTests(OfflineTestCase):
    def setUp(self):
        super().setUp()
        self.ff = fake_x_ffmpeg_skill(self.tmp)
        src = os.path.join(self.tmp, "src.mp4")
        Path(src).write_bytes(b"\x00" * 1024)
        self.src = src

    def _video(self, name):
        p = os.path.join(self.tmp, name)
        shutil.copy(self.src, p)
        return p

    def _plan(self, name, *extra):
        return run_cli("plan", self._video(name), *X_ARGS, *extra, "--ffmpeg-skill", self.ff, "--json")

    def test_done_when_a_5_minute_video_for_x_is_decided_by_this_skills_limit(self):
        # Roadmap step 12, "Done when": the verdict comes from this skill's limit (X: 20 minutes, cited), and
        # ffmpeg-skill's 140 s row is shown as guidance. Both an older ffmpeg-skill (FAIL) and the current one (WARN).
        for name in ("x-min5.mp4", "x-min5-new.mp4"):
            rc, doc = self._plan(name)
            self.assertEqual((rc, doc["verdict"], doc["ok"]), (0, "PASS", True), (name, doc["checks"]))
            mine = [r for r in rows_by(doc, "duration") if r["owner"] == "publish"][0]
            self.assertEqual((mine["status"], mine["value"], mine["limit"], mine["in_verdict"]), ("PASS", 300.0, 1200, True))
            self.assertEqual((mine["source"], mine["accessed"], mine["limit_status"]),
                             (L.limits_of("x")["video.max_duration_s"]["source"], "2026-10-31", "documented"))
            theirs = [r for r in rows_by(doc, "duration") if r["owner"] == "ffmpeg-skill"][0]
            self.assertEqual((theirs["limit"], theirs["in_verdict"], theirs["basis"]), ("<= 140s", False, "guidance"))
            self.assertIn(theirs["status"], ("FAIL", "WARN"))              # shown as ffmpeg-skill wrote it
            self.assertIn("video.max_duration_s", theirs["guidance"])
            self.assertEqual(doc["counts"]["guidance"], 2)                 # duration and file size
            self.assertEqual(doc["counts"]["fail"] + doc["counts"]["warn"] + doc["counts"]["unchecked"], 0)

    def test_every_other_ffmpeg_skill_row_still_counts(self):
        for name, check in (("x-min5-hevc.mp4", "video codec"), ("x-min5-wide.mp4", "aspect")):
            rc, doc = self._plan(name)
            self.assertEqual((rc, doc["verdict"]), (1, "FAIL"), name)
            row = rows_by(doc, check)[0]
            self.assertEqual((row["status"], row["owner"], row["in_verdict"]), ("FAIL", "ffmpeg-skill", True))
            self.assertNotIn("basis", row)
        for r in plan(self._video("x-min1.mp4"), "x", Metadata(title="t", privacy="public"), self.ff)["checks"]:
            self.assertEqual(r["in_verdict"], not (r["owner"] == "ffmpeg-skill" and r["check"] in GUIDANCE_CHECKS), r)

    def test_this_skills_own_limit_still_fails_an_over_long_video(self):
        rc, doc = self._plan("x-min25-new.mp4")
        self.assertEqual((rc, doc["verdict"]), (1, "FAIL"))
        failed = [r for r in doc["checks"] if r["status"] == "FAIL"]
        self.assertEqual([(r["check"], r["owner"], r["limit"]) for r in failed], [("duration", "publish", 1200)])

    def test_ffmpeg_skills_file_size_row_is_guidance_too(self):
        rc, doc = self._plan("x-min1-big.mp4")
        self.assertEqual((rc, doc["verdict"]), (0, "PASS"))
        theirs = [r for r in rows_by(doc, "file size") if r["owner"] == "ffmpeg-skill"][0]
        self.assertEqual((theirs["status"], theirs["in_verdict"]), ("FAIL", False))
        mine = [r for r in rows_by(doc, "file size") if r["owner"] == "publish"][0]
        self.assertEqual((mine["status"], mine["limit"], mine["limit_key"]), ("PASS", 8 * 1000 ** 3, "video.max_bytes"))

    def test_an_unmeasured_length_leaves_this_skills_row_unchecked(self):
        rc, doc = self._plan("x-nolen.mp4")
        self.assertEqual((rc, doc["verdict"]), (1, "INCOMPLETE"))
        mine = [r for r in rows_by(doc, "duration") if r["owner"] == "publish"][0]
        self.assertEqual(mine["status"], "UNCHECKED")

    def test_an_unknown_limit_is_marked_unknown_not_invented(self):
        # Facebook states no size or duration limit: this skill's rows say so, with the pages searched and the date,
        # and ffmpeg-skill's table rows stay guidance.
        doc = plan(self._video("fb-min5.mp4"), "facebook", Metadata(title="t", privacy="public"), self.ff)
        for check, key in (("duration", "video.max_duration_s"), ("file size", "video.max_bytes")):
            mine = [r for r in rows_by(doc, check) if r["owner"] == "publish"][0]
            self.assertEqual((mine["limit"], mine["limit_status"], mine["limit_key"]), (None, "unknown", key))
            self.assertTrue(mine["searched"] and mine["note"])
            self.assertTrue(re.fullmatch(r"\d{4}-\d{2}-\d{2}", mine["accessed"]))
            self.assertNotIn("source", mine)
            theirs = [r for r in rows_by(doc, check) if r["owner"] == "ffmpeg-skill"][0]
            self.assertFalse(theirs["in_verdict"])

    def test_every_platforms_duration_and_size_limits_state_their_source_and_date(self):
        for pid, table in L.LIMITS.items():
            for key in ("video.max_duration_s", "video.max_bytes"):
                e = table[key]
                self.assertIn(e["status"], ("documented", "unknown"), (pid, key))
                self.assertTrue(re.fullmatch(r"\d{4}-\d{2}-\d{2}", e["accessed"]), (pid, key))
                if e["status"] == "documented":
                    self.assertTrue(e["source"].startswith("https://") and e["quote"], (pid, key))
                else:
                    self.assertIsNone(e["value"], (pid, key))
                    self.assertTrue(e["searched"], (pid, key))

    def test_human_output_marks_guidance_rows(self):
        rc, out, _ = run_cli_raw("plan", self._video("x-min5.mp4"), *X_ARGS, "--ffmpeg-skill", self.ff)
        self.assertEqual(rc, 0)
        line = [ln for ln in out.splitlines() if "duration" in ln and "ffmpeg-skill" in ln][0]
        self.assertTrue(line.strip().startswith("guidance"), line)
        self.assertIn("FAIL, not counted", line)
        self.assertNotIn("fix:", line)

    def test_contract_declares_the_guidance_rows_additively(self):
        tool = [t for t in contract()["tools"] if t["tool_id"] == "publish/plan"][0]
        self.assertEqual(tool["verdict_rows"]["guidance"]["checks"], ["duration", "file size"])
        self.assertEqual(tool["verdict_rows"]["guidance"]["decided_by"],
                         {"duration": "video.max_duration_s", "file size": "video.max_bytes"})
        self.assertEqual(tool["verdicts"], ["PASS", "WARN", "FAIL", "INCOMPLETE"])       # unchanged
        self.assertEqual(tool["row_statuses"], ["PASS", "WARN", "FAIL", "UNCHECKED"])    # unchanged
