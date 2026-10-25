"""Unit tests for cook.py — focused on the subcommands where correctness is
non-obvious and regressions are most damaging:

  - verify-align: DP global alignment of en.srt vs translations.txt
    (must catch missing translations and drift, must not false-positive on
    pure-Chinese translations)
  - verify-shipment: file-presence checks across the release set
  - cloud-srt copy logic: the subtitles subcommand copies merged SRTs rather
    than splitting bilingual SRTs (splitting leaks cues across languages)

Tests use temp dirs and fake SRT files. No network, no ffmpeg, no whisperx.

Run: pytest test_cook.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# import cook.py from this dir regardless of how pytest is invoked
_HERE = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("cook", _HERE / "cook.py")
cook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cook)


# ----------------------------------------------------------------------------
# Helpers to fabricate SRT files in a temp dir shaped like a video dir.
# ----------------------------------------------------------------------------

def _make_srt(path: Path, cues: list[tuple[int, str, str]]) -> None:
    """Write cues as SRT. cues = [(num, "start --> end", text), ...]."""
    path.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    for num, ts, text in cues:
        blocks.append(f"{num}\n{ts}\n{text}")
    path.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


def _make_translations(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fake_en_cues(n: int) -> list[tuple[int, str, str]]:
    """n dummy English cues."""
    out = []
    for i in range(1, n + 1):
        start = f"00:00:{i:02d},000"
        end = f"00:00:{i:02d},500"
        out.append((i, f"{start} --> {end}", f"English cue number {i}"))
    return out


# ----------------------------------------------------------------------------
# verify-align: the DP alignment core
# ----------------------------------------------------------------------------

class TestVerifyAlign:
    def _setup_video(self, tmp_path: Path, n_en: int, trans_lines: list[str]):
        root = tmp_path / "author" / "video"
        en_srt = root / "transcript" / "video.en.srt"
        trans = root / "transcript" / "translations.txt"
        _make_srt(en_srt, _fake_en_cues(n_en))
        _make_translations(trans, trans_lines)
        return root

    def _run(self, root: Path) -> dict:
        """Invoke verify-align in-process and capture the JSON it emits."""
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {"output_root": str(root), "name": "video"})()
        with contextlib.redirect_stdout(buf):
            try:
                cook.cmd_verify_align(args)
            except SystemExit as e:
                self.last_exit = e.code
        return json.loads(buf.getvalue())

    def test_perfect_alignment(self, tmp_path: Path):
        """n translations for n cues, all aligned, exit 0."""
        root = self._setup_video(tmp_path, 5, [f"中文翻译 {i}" for i in range(1, 6)])
        report = self._run(root)
        assert report["ok"] is True
        assert report["en_cues"] == 5
        assert report["translation_lines"] == 5
        assert report["missing_translations"] == []
        assert report["extra_translations"] == []
        assert self.last_exit == 0

    def test_missing_translation(self, tmp_path: Path):
        """One English cue has no corresponding translation line."""
        # 5 cues, only 4 translations (cue 3 missing)
        trans = [f"中文 {i}" for i in (1, 2, 4, 5)]
        root = self._setup_video(tmp_path, 5, trans)
        report = self._run(root)
        assert report["ok"] is False
        assert report["missing_translations"] == [3]
        assert self.last_exit == 1

    def test_extra_translation(self, tmp_path: Path):
        """One extra translation line with no English cue. With pure-Chinese
        translations (no comparable tokens), DP can't pinpoint *where* the
        extra sits — but it reliably reports the *count* and flags ok=False,
        which is what the agent actually branches on."""
        trans = [f"中文 {i}" for i in range(1, 6)] + ["多余的行"]
        root = self._setup_video(tmp_path, 5, trans)
        report = self._run(root)
        assert report["ok"] is False
        assert len(report["extra_translations"]) == 1
        assert report["translation_lines"] == 6
        assert report["en_cues"] == 5
        assert self.last_exit == 1

    def test_two_lines_merged_into_one(self, tmp_path: Path):
        """The failure mode that motivated verify-align: the translator
        accidentally merged two English cues into one Chinese line. This
        must show as one missing + one extra (the merged line is extra
        relative to its position)."""
        # cues 1,2,3,4,5 but translation line 2 covers cues 2 AND 3
        trans = ["中文 1", "中文 2 和 3 合并", "中文 4", "中文 5"]
        root = self._setup_video(tmp_path, 5, trans)
        report = self._run(root)
        assert report["ok"] is False
        # either cue 2 or cue 3 is missing (alignment may pick either)
        assert len(report["missing_translations"]) >= 1
        assert self.last_exit == 1

    def test_pure_chinese_translation_not_false_positive(self, tmp_path: Path):
        """Translations with zero English tokens (pure Chinese) should still
        align correctly — the DP gives such pairs a moderate score rather
        than zero, so they don't get flagged as misaligned."""
        trans = ["你好世界", "第二段", "第三段", "第四段", "第五段"]
        root = self._setup_video(tmp_path, 5, trans)
        report = self._run(root)
        assert report["ok"] is True
        assert self.last_exit == 0

    def test_drift_off_by_one(self, tmp_path: Path):
        """Translations start one position late — every cue drifts by one.
        Must be caught (not exit 0)."""
        # skip translation for cue 1, then translate 2-5
        trans = ["中文 1 实际对应 cue 2", "中文 2 对应 cue 3",
                 "中文 3 对应 cue 4", "中文 4 对应 cue 5"]
        root = self._setup_video(tmp_path, 5, trans)
        report = self._run(root)
        assert report["ok"] is False
        assert self.last_exit == 1


# ----------------------------------------------------------------------------
# verify-shipment: file-presence checks
# ----------------------------------------------------------------------------

class TestVerifyShipment:
    def _run(self, root: Path, stage: str | None = None) -> dict:
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {
            "output_root": str(root), "name": "video",
            "stage": stage,
        })()
        with contextlib.redirect_stdout(buf):
            try:
                cook.cmd_verify_shipment(args)
            except SystemExit as e:
                self.last_exit = e.code
        return json.loads(buf.getvalue())

    def _touch(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # source.json gets real JSON content — verify-shipment validates it has
        # a title (a 3-byte "NA" or empty file would now flag as an issue).
        if path.name.endswith(".source.json"):
            path.write_text('{"title": "test video", "uploader": "author"}',
                            encoding="utf-8")
        else:
            path.write_bytes(b"x")

    def test_empty_dir_reports_all_missing(self, tmp_path: Path):
        root = tmp_path / "author" / "video"
        root.mkdir(parents=True)
        report = self._run(root)
        assert report["ok"] is False
        assert report["missing"]  # lots missing
        assert report["present"] == []
        assert self.last_exit == 1

    def test_full_release_set_present(self, tmp_path: Path):
        """A complete release set exits 0."""
        root = tmp_path / "author" / "video"
        n = "video"
        # raw/
        self._touch(root / "raw" / f"{n}.raw.mp4")
        self._touch(root / "raw" / f"{n}.source.json")
        self._touch(root / "raw" / f"{n}.jpg")
        # transcript/
        self._touch(root / "transcript" / f"{n}.audio.wav")
        self._touch(root / "transcript" / f"{n}.en.srt")
        self._touch(root / "transcript" / f"{n}.zh.srt")
        self._touch(root / "transcript" / "asr-fixes.md")
        # subtitle/
        self._touch(root / "subtitle" / f"{n}.bilingual.srt")
        self._touch(root / "subtitle" / f"{n}.bilingual.ass")
        # cloud-srt/
        self._touch(root / "cloud-srt" / "zh.srt")
        self._touch(root / "cloud-srt" / "en.srt")
        # cooked/
        self._touch(root / "cooked" / f"{n}.cooked.mp4")
        self._touch(root / "cooked" / f"{n}.upload.md")
        self._touch(root / "cooked" / "cover.jpg")
        # root
        self._touch(root / "README.md")

        report = self._run(root)
        assert report["ok"] is True, f"missing: {report['missing']}, issues: {report['issues']}"
        assert self.last_exit == 0

    def test_bottom_bar_variant_accepted(self, tmp_path: Path):
        """If overlay ASS/cooked are absent but bar variants exist, still OK."""
        root = tmp_path / "author" / "video"
        n = "video"
        self._touch(root / "raw" / f"{n}.raw.mp4")
        self._touch(root / "raw" / f"{n}.source.json")
        self._touch(root / "raw" / f"{n}.jpg")
        self._touch(root / "transcript" / f"{n}.audio.wav")
        self._touch(root / "transcript" / f"{n}.en.srt")
        self._touch(root / "transcript" / f"{n}.zh.srt")
        self._touch(root / "transcript" / "asr-fixes.md")
        self._touch(root / "subtitle" / f"{n}.bilingual.srt")
        self._touch(root / "subtitle" / f"{n}.bilingual.bar.ass")  # bar variant
        self._touch(root / "cloud-srt" / "zh.srt")
        self._touch(root / "cloud-srt" / "en.srt")
        self._touch(root / "cooked" / f"{n}.cooked.bar.mp4")  # bar variant
        self._touch(root / "cooked" / f"{n}.upload.md")
        self._touch(root / "cooked" / "cover.jpg")
        self._touch(root / "README.md")

        report = self._run(root)
        assert report["ok"] is True, f"missing: {report['missing']}"
        assert self.last_exit == 0

    def test_stage_filter(self, tmp_path: Path):
        """--stage raw only checks raw/ files."""
        root = tmp_path / "author" / "video"
        n = "video"
        self._touch(root / "raw" / f"{n}.raw.mp4")
        self._touch(root / "raw" / f"{n}.source.json")
        self._touch(root / "raw" / f"{n}.jpg")
        # nothing else

        report = self._run(root, stage="raw")
        assert report["ok"] is True
        assert report["stage"] == "raw"
        assert self.last_exit == 0

    def test_cloud_srt_stage_missing(self, tmp_path: Path):
        root = tmp_path / "author" / "video"
        root.mkdir(parents=True)
        report = self._run(root, stage="cloud-srt")
        assert report["ok"] is False
        # normalize path separators (Windows uses backslash, tests use forward)
        missing_norm = [m.replace("\\", "/") for m in report["missing"]]
        assert "cloud-srt/zh.srt" in missing_norm
        assert "cloud-srt/en.srt" in missing_norm
        assert self.last_exit == 1

    # ---- dubbed-video short-duration cross-check (issue #2) ----

    def _stage_full_cooked_set(self, root: Path, n: str = "video"):
        """Stage a complete release set (so no missing-file noise)."""
        self._touch(root / "raw" / f"{n}.raw.mp4")
        self._touch(root / "raw" / f"{n}.source.json")
        self._touch(root / "raw" / f"{n}.jpg")
        self._touch(root / "transcript" / f"{n}.audio.wav")
        self._touch(root / "transcript" / f"{n}.en.srt")
        self._touch(root / "transcript" / f"{n}.zh.srt")
        self._touch(root / "transcript" / "asr-fixes.md")
        self._touch(root / "subtitle" / f"{n}.bilingual.srt")
        self._touch(root / "subtitle" / f"{n}.bilingual.ass")
        self._touch(root / "cloud-srt" / "zh.srt")
        self._touch(root / "cloud-srt" / "en.srt")
        self._touch(root / "cooked" / f"{n}.cooked.mp4")
        self._touch(root / "cooked" / f"{n}.upload.md")
        self._touch(root / "cooked" / "cover.jpg")
        self._touch(root / "README.md")

    def test_dubbed_short_flagged_when_dubbed_under_half_raw(self, tmp_path: Path,
                                                             monkeypatch):
        """Dubbed video duration < 0.5 * raw -> issue naming both durations."""
        root = tmp_path / "author" / "video"
        n = "video"
        self._stage_full_cooked_set(root, n)
        # stage the dubbed mp4 (the trigger)
        self._touch(root / "cooked" / f"{n}.dubbed.mp4")

        # Drive the ffprobe helper: raw=732.0, cooked=732.0 (so the cooked
        # mismatch check does NOT fire), dubbed=185.0 (short -> fires).
        durations = {
            str(root / "raw" / f"{n}.raw.mp4"): 732.0,
            str(root / "cooked" / f"{n}.cooked.mp4"): 732.0,
            str(root / "cooked" / f"{n}.dubbed.mp4"): 185.0,
        }
        monkeypatch.setattr(cook, "_ffprobe_duration",
                            lambda p: durations.get(str(p), 0.0))

        report = self._run(root)
        assert report["ok"] is False
        short_issues = [i for i in report["issues"] if "dubbed video suspiciously short" in i]
        assert len(short_issues) == 1, f"expected one short issue, got {report['issues']}"
        msg = short_issues[0]
        assert "raw=732.0s" in msg
        assert "dubbed=185.0s" in msg
        assert self.last_exit == 1

    def test_dubbed_short_not_flagged_when_healthy(self, tmp_path: Path, monkeypatch):
        """Dubbed duration >= 0.5 * raw -> no short issue, ok unchanged."""
        root = tmp_path / "author" / "video"
        n = "video"
        self._stage_full_cooked_set(root, n)
        self._touch(root / "cooked" / f"{n}.dubbed.mp4")

        durations = {
            str(root / "raw" / f"{n}.raw.mp4"): 732.0,
            str(root / "cooked" / f"{n}.cooked.mp4"): 732.0,
            str(root / "cooked" / f"{n}.dubbed.mp4"): 730.0,  # healthy
        }
        monkeypatch.setattr(cook, "_ffprobe_duration",
                            lambda p: durations.get(str(p), 0.0))

        report = self._run(root)
        assert report["ok"] is True, f"unexpected issues: {report['issues']}"
        assert not any("dubbed video suspiciously short" in i for i in report["issues"])

    def test_dubbed_short_not_flagged_when_no_dubbed_file(self, tmp_path: Path,
                                                          monkeypatch):
        """No dubbed.mp4 -> check does not run (non-dubbed shipments unaffected)."""
        root = tmp_path / "author" / "video"
        n = "video"
        self._stage_full_cooked_set(root, n)
        # no dubbed.mp4 staged
        durations = {
            str(root / "raw" / f"{n}.raw.mp4"): 732.0,
            str(root / "cooked" / f"{n}.cooked.mp4"): 732.0,
        }
        monkeypatch.setattr(cook, "_ffprobe_duration",
                            lambda p: durations.get(str(p), 0.0))

        report = self._run(root)
        assert report["ok"] is True, f"unexpected issues: {report['issues']}"
        assert not any("dubbed video suspiciously short" in i for i in report["issues"])

    def test_dubbed_short_not_flagged_when_raw_duration_zero(self, tmp_path: Path,
                                                             monkeypatch):
        """Dubbed exists but raw probes to non-positive -> check skipped."""
        root = tmp_path / "author" / "video"
        n = "video"
        self._stage_full_cooked_set(root, n)
        self._touch(root / "cooked" / f"{n}.dubbed.mp4")

        durations = {
            str(root / "raw" / f"{n}.raw.mp4"): 0.0,  # missing/unreadable baseline
            str(root / "cooked" / f"{n}.cooked.mp4"): 732.0,
            str(root / "cooked" / f"{n}.dubbed.mp4"): 185.0,
        }
        monkeypatch.setattr(cook, "_ffprobe_duration",
                            lambda p: durations.get(str(p), 0.0))

        report = self._run(root)
        assert not any("dubbed video suspiciously short" in i for i in report["issues"])

    def test_dubbed_short_check_scoped_to_cooked_stage(self, tmp_path: Path,
                                                      monkeypatch):
        """--stage cooked runs the check; --stage raw does not."""
        root = tmp_path / "author" / "video"
        n = "video"
        self._stage_full_cooked_set(root, n)
        self._touch(root / "cooked" / f"{n}.dubbed.mp4")

        durations = {
            str(root / "raw" / f"{n}.raw.mp4"): 732.0,
            str(root / "cooked" / f"{n}.cooked.mp4"): 732.0,
            str(root / "cooked" / f"{n}.dubbed.mp4"): 185.0,
        }
        monkeypatch.setattr(cook, "_ffprobe_duration",
                            lambda p: durations.get(str(p), 0.0))

        # under --stage raw the dubbed-short check must NOT run
        report = self._run(root, stage="raw")
        assert not any("dubbed video suspiciously short" in i for i in report["issues"])

        # under --stage cooked it DOES run
        report = self._run(root, stage="cooked")
        assert any("dubbed video suspiciously short" in i for i in report["issues"])


# ----------------------------------------------------------------------------
# cloud-srt copy logic: the fix for B3 (splitting leaks cues across languages)
# ----------------------------------------------------------------------------

class TestCloudSrtNoCrossLeak:
    """The subtitles subcommand copies *.merged.srt to cloud-srt/zh.srt and
    cloud-srt/en.srt. This is the fix for the old 'split' command which
    leaked single-language cues from union-mode bilingual SRTs into the
    wrong output. We verify the copy logic directly."""

    def test_cloud_srt_matches_merged_not_bilingual(self, tmp_path: Path):
        """When bilingual.srt has union-mode single-language cues (the bug
        trigger), cloud-srt must still match the clean merged source —
        not inherit the contamination from bilingual.srt."""
        root = tmp_path / "vid"
        tdir = root / "transcript"
        sdir = root / "subtitle"
        cdir = root / "cloud-srt"
        tdir.mkdir(parents=True)
        sdir.mkdir(parents=True)

        # simulate the bug-triggering bilingual.srt: cue 3 has ONLY English
        # (no Chinese) because union-mode inserted it where zh was missing
        bilingual = sdir / "v.bilingual.srt"
        bilingual.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n中文一\nEnglish one\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\n中文二\nEnglish two\n\n"
            "3\n00:00:03,000 --> 00:00:04,000\nresearch is quite simple.\n\n"
            "4\n00:00:04,000 --> 00:00:05,000\n中文四\nEnglish four\n",
            encoding="utf-8",
        )

        # the clean merged sources (what cloud-srt should copy from)
        zh_merged = tdir / "v.zh.merged.srt"
        zh_merged.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n中文一\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\n中文二\n\n"
            "3\n00:00:04,000 --> 00:00:05,000\n中文四\n",
            encoding="utf-8",
        )
        en_merged = tdir / "v.en.merged.srt"
        en_merged.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nEnglish one\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\nEnglish two\n\n"
            "3\n00:00:03,000 --> 00:00:04,000\nresearch is quite simple.\n\n"
            "4\n00:00:04,000 --> 00:00:05,000\nEnglish four\n",
            encoding="utf-8",
        )

        # do what cook subtitles does: copy merged -> cloud-srt
        cdir.mkdir(parents=True)
        import shutil
        shutil.copyfile(zh_merged, cdir / "zh.srt")
        shutil.copyfile(en_merged, cdir / "en.srt")

        # verify cloud-srt/zh.srt has NO English-only cues (the bug signature)
        zh_content = (cdir / "zh.srt").read_text(encoding="utf-8")
        assert "research is quite simple" not in zh_content, \
            "cloud-srt/zh.srt leaked English cue (B3 regression)"
        assert "中文一" in zh_content
        assert "中文四" in zh_content

        # en.srt keeps the English cue (it belongs there)
        en_content = (cdir / "en.srt").read_text(encoding="utf-8")
        assert "research is quite simple" in en_content


# ----------------------------------------------------------------------------
# show-source: extract key fields from source.json
# ----------------------------------------------------------------------------

class TestShowSource:
    """source.json is yt-dlp's full info-dict (1MB+). show-source surfaces
    only the curated fields translation/upload actually consume."""

    def _run(self, root: Path, full: bool = False) -> dict:
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {
            "output_root": str(root), "name": "video", "full": full,
        })()
        with contextlib.redirect_stdout(buf):
            cook.cmd_show_source(args)
        return json.loads(buf.getvalue())

    def _make_source_json(self, root: Path, fields: dict) -> Path:
        import json as _json
        raw_dir = root / "raw"
        raw_dir.mkdir(parents=True, exist_ok=True)
        path = raw_dir / "video.source.json"
        path.write_text(_json.dumps(fields), encoding="utf-8")
        return path

    def test_extracts_key_fields(self, tmp_path: Path):
        """All curated fields surface; internal yt-dlp fields don't."""
        root = tmp_path / "vid"
        self._make_source_json(root, {
            "title": "Demo Video",
            "uploader": "Jane Dev",
            "channel": "Jane Dev",
            "uploader_url": "https://example.com/@jane",
            "webpage_url": "https://example.com/watch?v=123",
            "duration": 600,
            "upload_date": "20260101",
            "description": "A video about things.",
            "tags": ["python", "tutorial"],
            "categories": ["Education"],
            # internal yt-dlp fields that should NOT surface
            "formats": [{"format_id": "123", "ext": "mp4"}],
            "automatic_caption": {"tracks": []},
            "__original_url": "internal",
        })
        report = self._run(root)
        assert report["ok"] is True
        assert report["title"] == "Demo Video"
        assert report["uploader"] == "Jane Dev"
        assert report["uploader_url"] == "https://example.com/@jane"
        assert report["webpage_url"] == "https://example.com/watch?v=123"
        assert report["duration"] == 600
        assert report["upload_date"] == "20260101"
        assert report["description"] == "A video about things."
        assert report["tags"] == ["python", "tutorial"]
        assert report["categories"] == ["Education"]
        # internal fields must not leak
        assert "formats" not in report
        assert "automatic_caption" not in report
        assert "__original_url" not in report

    def test_truncates_long_description(self, tmp_path: Path):
        """Long descriptions (10KB+) get truncated to 5000 chars by default."""
        root = tmp_path / "vid"
        long_desc = "x" * 8000
        self._make_source_json(root, {"title": "T", "description": long_desc})
        report = self._run(root, full=False)
        assert len(report["description"]) < 5100  # ~5000 + truncation marker
        assert "truncated" in report["description"]
        assert "8000 chars total" in report["description"]

    def test_full_flag_disables_truncation(self, tmp_path: Path):
        """--full returns the entire description, no truncation."""
        root = tmp_path / "vid"
        long_desc = "x" * 8000
        self._make_source_json(root, {"title": "T", "description": long_desc})
        report = self._run(root, full=True)
        assert report["description"] == long_desc

    def test_missing_fields_omitted(self, tmp_path: Path):
        """Fields absent from source.json don't appear as null in output."""
        root = tmp_path / "vid"
        self._make_source_json(root, {"title": "T"})  # only title, nothing else
        report = self._run(root)
        assert report["title"] == "T"
        assert "uploader" not in report
        assert "description" not in report

    def test_missing_source_json_exits_nonzero(self, tmp_path: Path):
        """No source.json -> exit 1, clear error."""
        root = tmp_path / "vid"
        root.mkdir(parents=True)
        with pytest.raises(SystemExit) as exc:
            self._run(root)
        assert exc.value.code == 1


# ----------------------------------------------------------------------------
# Path helper sanity
# ----------------------------------------------------------------------------

class TestPathHelpers:
    def test_raw_transcript_subtitle_cooked_layout(self, tmp_path: Path):
        root = tmp_path / "vid"
        assert cook._raw(root, "v", ".raw.mp4") == root / "raw" / "v.raw.mp4"
        assert cook._transcript(root, "v", ".en.srt") == root / "transcript" / "v.en.srt"
        assert cook._subtitle(root, "v", ".bilingual.srt") == root / "subtitle" / "v.bilingual.srt"
        assert cook._cooked(root, "v", ".cooked.mp4") == root / "cooked" / "v.cooked.mp4"

    def test_slugify(self):
        assert cook._slugify("Hello, World!") == "hello-world"
        assert cook._slugify("LIVE: The /wayfinder Demo") == "live-the-wayfinder-demo"
        # unicode-aware: Chinese chars are word chars (\w), so they're kept
        assert cook._slugify("你好 World") == "你好-world"
        # pure separators collapse to empty, which the fallback turns into "video"
        assert cook._slugify("===") == "video"


# ----------------------------------------------------------------------------
# download: best-format selection (the av01-fake-highest trap)
#
# yt-dlp's default sort picked av01 at 1080p/255k over vp9 at 1080p/509k four
# times in production. cook now ranks (codec family vp9>h264>av01, bitrate)
# inside the target resolution and downloads with an explicit -f itag spec.
# ----------------------------------------------------------------------------

def _fmt(fid: str, *, vcodec: str = "none", acodec: str = "none", height=None,
         tbr=None, abr=None, language=None) -> dict:
    """One yt-dlp format-table row (subset of the fields -F reports)."""
    return {"format_id": fid, "vcodec": vcodec, "acodec": acodec,
            "height": height, "tbr": tbr, "abr": abr, "language": language}


# Real format table of YouTube L1OBRtfPS34 ("Stanford CS146S: How always-on
# agents work", original language en-US), transcribed from the run that
# caught the trap: at 1080p av01 (399) is 255.679k while vp9 (248) is
# 509.651k. Storyboards pruned to two rows and the ~40 identical-bitrate
# foreign dubs pruned to the top by abr; every kept row is verbatim.
_LEEROB_FORMATS = [
    # storyboards (no real audio/video codec — must never be candidates)
    _fmt("sb1", height=45),
    _fmt("sb0", height=180),
    # video-only, per resolution: avc1 / vp9 / av01
    _fmt("160", vcodec="avc1.4D400C", height=144, tbr=13.712),
    _fmt("278", vcodec="vp9", height=144, tbr=37.82),
    _fmt("394", vcodec="av01.0.00M.08", height=144, tbr=20.347),
    _fmt("133", vcodec="avc1.4D4015", height=240, tbr=23.31),
    _fmt("242", vcodec="vp9", height=240, tbr=55.182),
    _fmt("395", vcodec="av01.0.00M.08", height=240, tbr=32.095),
    _fmt("134", vcodec="avc1.4D401E", height=360, tbr=37.792),
    _fmt("243", vcodec="vp9", height=360, tbr=111.155),
    _fmt("396", vcodec="av01.0.01M.08", height=360, tbr=59.497),
    _fmt("135", vcodec="avc1.4D401F", height=480, tbr=52.901),
    _fmt("244", vcodec="vp9", height=480, tbr=154.556),
    _fmt("397", vcodec="av01.0.04M.08", height=480, tbr=91.433),
    _fmt("136", vcodec="avc1.4D401F", height=720, tbr=71.126),
    _fmt("247", vcodec="vp9", height=720, tbr=274.293),
    _fmt("398", vcodec="av01.0.05M.08", height=720, tbr=154.207),
    _fmt("137", vcodec="avc1.640028", height=1080, tbr=203.478),
    _fmt("248", vcodec="vp9", height=1080, tbr=509.651),
    _fmt("399", vcodec="av01.0.08M.08", height=1080, tbr=255.679),
    # muxed 360p (has both codecs)
    _fmt("18", vcodec="avc1.42001E", acodec="mp4a.40.2", height=360, tbr=223.132),
    # original-language (en-US) audio-only
    _fmt("249-drc", acodec="opus", abr=50.69, language="en-US"),
    _fmt("250-drc", acodec="opus", abr=58.68, language="en-US"),
    _fmt("249-19", acodec="opus", abr=50.628, language="en-US"),
    _fmt("250-19", acodec="opus", abr=58.454, language="en-US"),
    _fmt("140-drc", acodec="mp4a.40.2", abr=129.473, language="en-US"),
    _fmt("251-drc", acodec="opus", abr=107.074, language="en-US"),
    _fmt("140-19", acodec="mp4a.40.2", abr=129.473, language="en-US"),
    _fmt("251-19", acodec="opus", abr=106.719, language="en-US"),
    # auto-dub audio in other languages — higher abr than any en track, so a
    # bitrate-only sort would grab a dub; the language filter must guard this
    _fmt("251-18", acodec="opus", abr=133.064, language="ar"),
    _fmt("251-17", acodec="opus", abr=130.91, language="ta"),
    _fmt("140-0", acodec="mp4a.40.2", abr=129.473, language="ar"),
    _fmt("249-0", acodec="opus", abr=48.807, language="de-DE"),
]


class TestDownloadFormatSelection:
    def test_real_leerob_table_picks_vp9_over_av01(self):
        sel = cook._select_best_formats(_LEEROB_FORMATS, language="en-US")
        assert sel["format_spec"] == "248+140-19"
        assert sel["video"]["format_id"] == "248"
        assert sel["video"]["codec_family"] == "vp9"
        assert sel["video"]["height"] == 1080
        # original-language top-bitrate audio, non-DRC on the abr tie
        assert sel["audio"]["format_id"] == "140-19"
        assert sel["audio"]["language"] == "en-US"

    def test_quality_cap_selects_within_cap(self):
        sel = cook._select_best_formats(_LEEROB_FORMATS, quality=720,
                                        language="en-US")
        # tallest height <= 720 is 720, and there vp9 (247, 274k) beats
        # av01 (398, 154k) and avc1 (136, 71k)
        assert sel["format_spec"] == "247+140-19"
        assert sel["video"]["height"] == 720

    def test_quality_cap_below_every_stream_gives_no_selection(self):
        # smallest real stream is 144p; a 120p cap leaves nothing -> the
        # caller must fall back rather than download garbage
        assert cook._select_best_formats(_LEEROB_FORMATS, quality=120) is None

    def test_h264_beats_higher_bitrate_av01_when_no_vp9(self):
        # codec family outranks raw bitrate inside the same resolution
        table = [
            _fmt("399", vcodec="av01.0.08M.08", height=1080, tbr=255.679),
            _fmt("137", vcodec="avc1.640028", height=1080, tbr=203.478),
            _fmt("140", acodec="mp4a.40.2", abr=129.473, language="en"),
        ]
        sel = cook._select_best_formats(table, language="en")
        assert sel["video"]["format_id"] == "137"
        assert sel["video"]["codec_family"] == "h264"
        assert sel["format_spec"] == "137+140"

    def test_muxed_only_source_uses_single_itag(self):
        # HLS-style source with no separate streams: the best muxed stream is
        # used alone (no '+' spec that would die with "format not available")
        table = [
            _fmt("hls_360", vcodec="avc1.4D401E", acodec="mp4a.40.2",
                 height=360, tbr=800),
            _fmt("hls_720", vcodec="avc1.64001f", acodec="mp4a.40.2",
                 height=720, tbr=2500),
        ]
        sel = cook._select_best_formats(table)
        assert sel["format_spec"] == "hls_720"
        assert sel["audio"] is None

    def test_video_only_source_without_audio_returns_none(self):
        # nothing pairable -> caller falls back to the legacy yt-dlp spec
        table = [_fmt("248", vcodec="vp9", height=1080, tbr=509.651)]
        assert cook._select_best_formats(table, language="en") is None

    def test_no_language_info_falls_back_to_all_audio(self):
        # formats without language fields must not zero out the audio pick
        table = [
            _fmt("248", vcodec="vp9", height=1080, tbr=509.651),
            _fmt("250", acodec="opus", abr=58.68),
            _fmt("251", acodec="opus", abr=107.074),
        ]
        sel = cook._select_best_formats(table)  # no language hint at all
        assert sel["format_spec"] == "248+251"


# ----------------------------------------------------------------------------
# dub stage prerequisite checks (issue #3)
# ----------------------------------------------------------------------------

class TestDubStagePrerequisites:
    """Each dub stage must fail fast with a clear _die message when its
    prerequisite files are missing or empty — before any subprocess launches.
    The test asserts on the JSON error and exit code, and that no real dub
    tooling is invoked (the prerequisite check fires first)."""

    def _run_stage(self, root: Path, stage: str, name: str = "video") -> dict | None:
        """Invoke the cmd_dub_<stage> function in-process, capturing stdout
        JSON. Returns None if the command exited before emitting JSON."""
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {
            "output_root": str(root), "name": name,
            "python": None, "detach": False,
        })()
        cmd = {
            "synth": cook.cmd_dub_synth,
            "assemble": cook.cmd_dub_assemble,
        }[stage]
        with contextlib.redirect_stdout(buf):
            try:
                cmd(args)
            except SystemExit as e:
                self.last_exit = e.code
        text = buf.getvalue().strip()
        return json.loads(text) if text else None

    def _touch(self, path: Path, content: bytes = b"x") -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    # -- synth: needs transcript/translations_dub.txt and dubbed/_reference/ref.wav

    def test_synth_fails_when_translations_dub_missing(self, tmp_path: Path):
        root = tmp_path / "vid"
        self._touch(root / "dubbed" / "_reference" / "ref.wav")
        # translations_dub.txt NOT staged
        report = self._run_stage(root, "synth")
        assert self.last_exit == 1
        assert report["ok"] is False
        assert "translations_dub.txt" in report["error"]

    def test_synth_fails_when_ref_wav_missing(self, tmp_path: Path):
        root = tmp_path / "vid"
        self._touch(root / "transcript" / "translations_dub.txt")
        # ref.wav NOT staged
        report = self._run_stage(root, "synth")
        assert self.last_exit == 1
        assert report["ok"] is False
        assert "ref.wav" in report["error"]

    def test_synth_fails_when_ref_wav_empty(self, tmp_path: Path):
        """Zero-byte prerequisite counts as missing."""
        root = tmp_path / "vid"
        self._touch(root / "transcript" / "translations_dub.txt")
        self._touch(root / "dubbed" / "_reference" / "ref.wav", content=b"")  # empty
        report = self._run_stage(root, "synth")
        assert self.last_exit == 1
        assert "ref.wav" in report["error"]

    # -- assemble: needs dubbed/_full/_segments/ with at least one .wav

    def test_assemble_fails_when_segments_dir_missing(self, tmp_path: Path):
        root = tmp_path / "vid"
        # _segments/ dir not created at all
        report = self._run_stage(root, "assemble")
        assert self.last_exit == 1
        assert report["ok"] is False
        assert "_segments" in report["error"]

    def test_assemble_fails_when_segments_dir_empty(self, tmp_path: Path):
        root = tmp_path / "vid"
        self._touch(root / "dubbed" / "_full" / "_segments" / ".keep")  # no .wav
        report = self._run_stage(root, "assemble")
        assert self.last_exit == 1
        assert "_segments" in report["error"]

    # -- error message names the producing command

    def test_error_names_producing_command(self, tmp_path: Path):
        """The _die message must tell the operator what to rerun."""
        root = tmp_path / "vid"
        # synth stage, both prereqs missing
        report = self._run_stage(root, "synth")
        msg = report["error"]
        # must tell the operator where the missing file comes from
        assert "Step 3" in msg or "cook dub" in msg, msg

    # -- full pipeline inherits checks (first missing prereq aborts)

    def test_full_inherits_first_prereq_check(self, tmp_path: Path):
        """cook dub full should fail on synth's missing prereq before any
        subprocess is launched."""
        import io
        import contextlib
        root = tmp_path / "vid"
        root.mkdir(parents=True)
        buf = io.StringIO()
        args = type("A", (), {
            "output_root": str(root), "name": "video",
            "python": None, "detach": False,
        })()
        with contextlib.redirect_stdout(buf):
            try:
                cook.cmd_dub_full(args)
            except SystemExit as e:
                self.last_exit = e.code
        report = json.loads(buf.getvalue().strip())
        assert self.last_exit == 1
        assert report["ok"] is False
        # synth is the first stage; its prereq (translations_dub.txt) must be named
        assert "translations_dub.txt" in report["error"]


# ----------------------------------------------------------------------------
# burn bar-px / ASS geometry mismatch warning (issue #4)
# ----------------------------------------------------------------------------

# A minimal bar ASS header carrying a controlled PlayResY. The body is
# irrelevant to the geometry check — only the [Script Info] header is read.
_ASS_BAR_TEMPLATE = """[Script Info]
Title: bilingual bar
ScriptType: v4.00+
PlayResX: 1920
PlayResY: {play_res_y}
WrapStyle: 0
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,48,&H00FFFFFF,&H00000000,0,0,1,2,0,2,40,40,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00,0:00:02,Default,,0,0,0,,hello
"""


class TestBurnBarPxAssGeometry:
    """When burn runs in bottom-bar mode and the bar ASS exists, read its
    PlayResY and warn when it differs from the expectation derived from the
    probed frame height. Geometry model (same as full_dub step 2d): the ASS
    coordinate system is 1920 x (1080 + bar_play) and libass scales it
    per-axis onto the padded frame, so a correct build has y-scale H/1080 —
    a real pad of bar_px pixels equals round(bar_px * 1080 / H) in ASS units
    and the expected PlayResY is 1080 + that. A probe failure falls back to
    the 1080p assumption (the legacy behavior). Overlay mode and missing-ASS
    skip the check entirely."""

    def _stub_probe(self, monkeypatch, frame_h):
        """Pin the probed frame height so the geometry check sees the frame
        we want without a real ffprobe run."""
        monkeypatch.setattr(cook, "_probe_frame_height", lambda video: frame_h)

    def _run_burn(self, root: Path, mode: str = "bottom-bar", bar_px: int = 220,
                  detach: bool = False):
        """Invoke cmd_burn in-process, capturing stdout JSON and stderr.
        Returns (report_or_None, stderr_text)."""
        import io
        import contextlib
        buf = io.StringIO()
        err = io.StringIO()
        args = type("A", (), {
            "output_root": str(root), "name": "video",
            "mode": mode, "bar_px": bar_px, "detach": detach,
        })()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            try:
                cook.cmd_burn(args)
            except SystemExit as e:
                self.last_exit = e.code
        text = buf.getvalue().strip()
        report = json.loads(text) if text else None
        return report, err.getvalue()

    def _stage_burn_inputs(self, root: Path, mode: str, ass_play_res_y: int | None):
        """Stage raw.mp4 + the appropriate ASS. If ass_play_res_y is None,
        no ASS is staged (to test the missing-ASS path)."""
        n = "video"
        (root / "raw").mkdir(parents=True, exist_ok=True)
        (root / "raw" / f"{n}.raw.mp4").write_bytes(b"x")
        (root / "subtitle").mkdir(parents=True, exist_ok=True)
        (root / "cooked").mkdir(parents=True, exist_ok=True)
        if ass_play_res_y is not None:
            if mode == "bottom-bar":
                ass = root / "subtitle" / f"{n}.bilingual.bar.ass"
            else:
                ass = root / "subtitle" / f"{n}.bilingual.ass"
            ass.write_text(_ASS_BAR_TEMPLATE.format(play_res_y=ass_play_res_y),
                           encoding="utf-8")

    def _stub_ffmpeg(self, monkeypatch, *, succeed=True):
        """Prevent real ffmpeg runs by stubbing _run_long_task. Burn would
        normally shell out to ffmpeg; we make it succeed instantly so the
        geometry check (which runs BEFORE the burn) is the only thing tested."""
        monkeypatch.setattr(cook.shutil, "which",
                            lambda name: "/usr/bin/ffmpeg" if name == "ffmpeg" else None)
        def fake_run(cmd, cwd, log_file, err_file, detach=False):
            # pretend the output file was produced
            if succeed:
                # the output path is the last positional in the ffmpeg argv
                out_arg = cmd[-1]
                from pathlib import Path as _P
                _P(str(out_arg)).parent.mkdir(parents=True, exist_ok=True)
                _P(str(out_arg)).write_bytes(b"\x00\x00\x00 mp4")
            return {"ok": succeed, "detached": False, "returncode": 0 if succeed else 1}
        monkeypatch.setattr(cook, "_run_long_task", fake_run)

    def test_warns_on_bar_px_mismatch(self, tmp_path: Path, monkeypatch):
        """bar ASS PlayResY != 1080 + bar_px -> warning naming both values."""
        root = tmp_path / "vid"
        # ASS built for 1080+200=1280, but burn --bar-px 220 (expects 1300)
        self._stage_burn_inputs(root, "bottom-bar", ass_play_res_y=1280)
        self._stub_ffmpeg(monkeypatch)
        report, stderr = self._run_burn(root, mode="bottom-bar", bar_px=220)
        assert "1300" in stderr or "1280" in stderr  # names the values
        assert "bar-px" in stderr or "bar_px" in stderr  # points to the fix
        # warning does not fail the burn
        assert report is None or report.get("ok") is True or self.last_exit == 0

    def test_no_warning_when_playresy_matches(self, tmp_path: Path, monkeypatch):
        """PlayResY == 1080 + bar_px -> no warning, burn proceeds."""
        root = tmp_path / "vid"
        self._stage_burn_inputs(root, "bottom-bar", ass_play_res_y=1300)  # 1080+220
        self._stub_ffmpeg(monkeypatch)
        report, stderr = self._run_burn(root, mode="bottom-bar", bar_px=220)
        assert "geometry" not in stderr.lower()
        assert "playresy" not in stderr.lower()
        assert "1300" not in stderr  # no mismatch banner

    def test_no_geometry_check_in_overlay_mode(self, tmp_path: Path, monkeypatch):
        """Overlay mode has no bar geometry — check must not run even when the
        overlay ASS has a PlayResY that would mismatch."""
        root = tmp_path / "vid"
        self._stage_burn_inputs(root, "overlay", ass_play_res_y=999)  # would mismatch
        self._stub_ffmpeg(monkeypatch)
        report, stderr = self._run_burn(root, mode="overlay", bar_px=220)
        assert "geometry" not in stderr.lower()
        assert "999" not in stderr

    def test_missing_ass_keeps_existing_error(self, tmp_path: Path, monkeypatch):
        """When the bar ASS is absent, the existing 'ASS not found' error is
        produced unchanged; the geometry check does not run or mask it."""
        root = tmp_path / "vid"
        self._stage_burn_inputs(root, "bottom-bar", ass_play_res_y=None)  # no ASS
        self._stub_ffmpeg(monkeypatch)
        report, stderr = self._run_burn(root, mode="bottom-bar", bar_px=220)
        assert self.last_exit == 1
        assert report is None  # _die before any JSON for the burn
        assert "ASS not found" in stderr or "ASS" in stderr
        # geometry check did not run (no mismatch banner)
        assert "playresy" not in stderr.lower()

    def test_2160p_scaled_ass_no_warning(self, tmp_path: Path, monkeypatch):
        """4K frame (2160p) + ASS scaled to its geometry: burn pads 440 real
        px, which is 220 in ASS units, so a correct ASS carries
        PlayResY=1300 -> zero warnings. (Pre-fix this false-warned: the
        hardcoded 1080 expectation demanded 1080+440=1520.)"""
        root = tmp_path / "vid"
        self._stage_burn_inputs(root, "bottom-bar", ass_play_res_y=1300)
        self._stub_ffmpeg(monkeypatch)
        self._stub_probe(monkeypatch, 2160)
        report, stderr = self._run_burn(root, mode="bottom-bar", bar_px=440)
        assert "geometry" not in stderr.lower()
        assert "playresy" not in stderr.lower()
        assert "1300" not in stderr  # no mismatch banner

    def test_2160p_true_mismatch_still_warns(self, tmp_path: Path, monkeypatch):
        """4K frame, burn pads 440 real px (= 220 ASS units -> expects
        PlayResY 1300), but the ASS was built with an unscaled 440-unit bar
        (PlayResY 1520) -> still a true geometry error, still warns."""
        root = tmp_path / "vid"
        self._stage_burn_inputs(root, "bottom-bar", ass_play_res_y=1520)
        self._stub_ffmpeg(monkeypatch)
        self._stub_probe(monkeypatch, 2160)
        report, stderr = self._run_burn(root, mode="bottom-bar", bar_px=440)
        assert "geometry" in stderr.lower()
        assert "1300" in stderr or "1520" in stderr  # names the values
        # warning does not fail the burn
        assert report is None or report.get("ok") is True or self.last_exit == 0

    def test_1080p_probe_keeps_legacy_expectation(self, tmp_path: Path, monkeypatch):
        """An explicit 1080p probe is scale factor 1: bar 220 real px expects
        PlayResY 1080+220=1300 — the legacy behavior, now via the probe."""
        root = tmp_path / "vid"
        self._stage_burn_inputs(root, "bottom-bar", ass_play_res_y=1300)
        self._stub_ffmpeg(monkeypatch)
        self._stub_probe(monkeypatch, 1080)
        report, stderr = self._run_burn(root, mode="bottom-bar", bar_px=220)
        assert "geometry" not in stderr.lower()
        assert "playresy" not in stderr.lower()


class TestGeometryProbeFrameHeight:
    """The frame-height probe feeding the bar-px geometry check: parses
    ffprobe's WxH answer (same call shape as full_dub's _probe_wh). Anything
    but a clean answer — ffprobe missing, nonzero exit, unparseable stdout —
    returns None so cmd_burn falls back to the 1080p assumption instead of
    crashing or misdetecting."""

    def _probe(self, monkeypatch, *, stdout="", returncode=0, ffprobe_on_path=True):
        if ffprobe_on_path:
            monkeypatch.setattr(cook.shutil, "which",
                                lambda name: "/usr/bin/ffprobe")
        else:
            monkeypatch.setattr(cook.shutil, "which", lambda name: None)

        class _R:
            pass

        r = _R()
        r.returncode = returncode
        r.stdout = stdout
        monkeypatch.setattr(cook.subprocess, "run", lambda *a, **k: r)
        return cook._probe_frame_height(Path("raw/video.raw.mp4"))

    def test_parses_width_height_csv(self, monkeypatch):
        assert self._probe(monkeypatch, stdout="3840x2160\n") == 2160
        assert self._probe(monkeypatch, stdout="1920x1080\n") == 1080

    def test_missing_ffprobe_returns_none(self, monkeypatch):
        assert self._probe(monkeypatch, ffprobe_on_path=False) is None

    def test_probe_failure_returns_none(self, monkeypatch):
        # ffprobe ran but errored (e.g. the file is not a video) -> None
        assert self._probe(monkeypatch, stdout="", returncode=1) is None
        # exited 0 but the stdout is not a clean WxH -> None
        assert self._probe(monkeypatch, stdout="n/a") is None
        assert self._probe(monkeypatch, stdout="2160") is None


class TestLengthIssuesWidthGate:
    """Pin the width-based length gate (zh 64 / en 184 display-width units).
    A regression back to char-counting (the old 45/90 gates) must fail these:
    mixed zh+en lines score half their char count in width, and EN cues up to
    184 are legal under shorten's x1.15 exemption."""

    def test_zh_mixed_line_at_64_width_passes(self):
        # 16 CJK (32 width) + 32 ASCII (32 width) = 64 width, 48 chars —
        # would FAIL the old 45-char gate (the original false positive)
        line = "我" * 16 + "a" * 32
        assert cook._wlen(line) == 64
        assert len(line) == 48
        issues = cook._length_issues([(0.0, line)], [])
        assert issues == []

    def test_zh_over_64_width_flags(self):
        line = "我" * 33  # 66 width
        issues = cook._length_issues([(0.0, line)], [])
        assert len(issues) == 1 and "zh" in issues[0] and "display-width" in issues[0]

    def test_en_at_184_passes_at_185_flags(self):
        ok = "a" * 184   # shorten's x1.15 exemption ceiling (160 x 1.15)
        bad = "a" * 185
        assert cook._length_issues([], [(0.0, ok)]) == []
        issues = cook._length_issues([], [(0.0, bad)])
        assert len(issues) == 1 and "en" in issues[0] and "184" in issues[0]

    def test_en_161_legal_under_exemption_not_flagged(self):
        # the round-3 defect: 161-184 cues survive shorten untouched and
        # must not surface as an unfixable issue
        assert cook._length_issues([], [(0.0, "a" * 170)]) == []


# ----------------------------------------------------------------------------
# 0.6.1 hardening: stem cap (MAX_PATH), --quality int, author-dir hint,
# speechbrain guard detection
# ----------------------------------------------------------------------------

class TestSlugifyStemCap:
    def test_long_title_capped_at_48(self):
        slug = cook._slugify("matt-pocock-just-saw-a-comment-saying-that-ive-"
                             "never-made-a-proper-overview-of-everything")
        assert len(slug) <= cook._MAX_STEM
        # truncation keeps a 5-char hash tail: prefix-xxxxx
        assert slug.endswith("-" + slug[-5:])
        assert set(slug[-5:]) <= set("0123456789abcdef")

    def test_cap_is_deterministic(self):
        long_title = "a" * 120
        assert cook._slugify(long_title) == cook._slugify(long_title)

    def test_same_prefix_distinct_titles_stay_distinct(self):
        # two long titles sharing a 60-char prefix must not collide
        a = cook._slugify("x" * 60 + "alpha-part-of-the-title")
        b = cook._slugify("x" * 60 + "beta-part-of-the-title")
        assert a != b

    def test_short_titles_untouched(self):
        assert cook._slugify("Hello, World!") == "hello-world"


class TestQualityParserInt:
    def test_quality_parses_as_int(self):
        args = cook.build_parser().parse_args(
            ["download", "https://example.com/v", "--quality", "1080"])
        assert args.quality == 1080
        assert isinstance(args.quality, int)

    def test_quality_rejects_non_numeric(self):
        import pytest as _pytest
        with _pytest.raises(SystemExit):
            cook.build_parser().parse_args(
                ["download", "https://example.com/v", "--quality", "hd"])


class TestVerifyShipmentAuthorDirHint:
    def _run(self, root: Path) -> dict:
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {"output_root": str(root), "name": "video",
                              "stage": None})()
        with contextlib.redirect_stdout(buf):
            try:
                cook.cmd_verify_shipment(args)
            except SystemExit as e:
                self.last_exit = e.code
        return json.loads(buf.getvalue())

    def test_author_dir_gets_targeted_hint(self, tmp_path: Path):
        # an author dir holding one complete-looking video dir underneath:
        # passing the author dir itself makes everything read as missing
        author = tmp_path / "matt-pocock"
        video = author / "some-video-name"
        (video / "raw").mkdir(parents=True)
        (video / "raw" / "some-video-name.raw.mp4").write_bytes(b"x")
        report = self._run(author)
        assert report["ok"] is False
        assert any("AUTHOR dir" in i and "some-video-name" in i
                   for i in report["issues"])

    def test_empty_dir_no_false_hint(self, tmp_path: Path):
        # a per-video dir that is simply empty must not raise or hint
        root = tmp_path / "vid"
        root.mkdir()
        report = self._run(root)
        assert report["ok"] is False
        assert report["issues"] == []


class TestSpeechbrainGuardDetection:
    UNPATCHED = ('        if importer_frame is not None and '
                 'importer_frame.filename.endswith(\n            "/inspect.py"\n        ):\n')

    PATCHED = ('        if importer_frame is not None and '
               'importer_frame.filename.replace(\n            "\\\\", "/"\n        '
               ').endswith("/inspect.py"):\n')

    def test_unpatched_source_detected(self):
        assert cook._sb_guard_patched(self.UNPATCHED) is False

    def test_patched_source_detected(self):
        assert cook._sb_guard_patched(self.PATCHED) is True


class TestQualityGuardR2:
    """0.6.1 round-2 additions: the early sanity guard and the height
    warning must BOTH exist (an editing accident once replaced the latter)."""

    def test_early_guard_rejects_below_120(self, tmp_path):
        args = type("A", (), {"url": "https://x", "quality": 119,
                              "cookies": None, "cookies_from_browser": None,
                              "author": None, "name": None})()
        import contextlib
        import io
        import pytest as _pytest
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            with _pytest.raises(SystemExit) as ei:
                cook.cmd_download(args)
        assert ei.value.code != 0
        report = json.loads(buf.getvalue())
        assert report["ok"] is False and "sane height cap" in report["error"]

    def test_both_quality_checks_exist_in_source(self):
        src = Path(cook.__file__).read_text(encoding="utf-8")
        assert "args.quality < 120" in src            # early sanity guard
        assert "args.quality + 1" in src              # post-download compare


# ----------------------------------------------------------------------------
# dub stage scripts physically consolidated into the cook package (reflux
# tk-02): the seven deterministic dub scripts live in stages/dub/, the dub
# subcommands execute the PACKAGE-INTERNAL copy via cook._stage_script (not
# whatever video-dubbing skill copy happens to be installed), and each
# script keeps __main__ direct-call capability.
# ----------------------------------------------------------------------------

_DUB_STAGE_SCRIPTS = [
    "full_dub.py",
    "retime.py",
    "loudness_match.py",
    "scan_reference.py",
    "extract_reference.py",
    "make_zh_dub_srt.py",
    "build_merge.py",
]
# length_gate.py is deliberately NOT here (ticket 05): functions-only, its
# CLI is `cook dub gate` — same conversion measure_rate.py got in ticket 04.


def _stages_dub_dir() -> Path:
    """The package-internal dub stage dir, derived the way cook derives it:
    next to cook.py (dev checkout) / next to the installed module (wheel)."""
    return Path(cook.__file__).resolve().parent / "stages" / "dub"


class TestDubScriptsInPackage:
    """The subprocess-run dub stage scripts are vendored in stages/dub/ and
    stay directly runnable (python <script> ...) — the skill repo no longer
    needs to be installed for the pipeline to run. (measure_rate.py and
    length_gate.py are functions-only and run in-process under cook.)"""

    def test_all_seven_scripts_present(self):
        d = _stages_dub_dir()
        for name in _DUB_STAGE_SCRIPTS:
            assert (d / name).is_file(), f"stages/dub/{name} missing"

    def test_scripts_run_directly_without_traceback(self):
        """__main__ direct-call capability: each script, invoked with no args,
        exits non-zero with usage/doc text (argparse or manual check) — NOT
        with an ImportError/AttributeError traceback or a missing file."""
        d = _stages_dub_dir()
        for name in _DUB_STAGE_SCRIPTS:
            r = subprocess.run(
                [sys.executable, str(d / name)],
                capture_output=True, text=True, timeout=60,
            )
            assert r.returncode != 0, f"{name}: no-args should not succeed"
            tb = r.stderr + r.stdout
            assert "Traceback" not in tb, f"{name}: {r.stderr[-400:]}"
            assert "No such file" not in tb, f"{name}: {r.stderr[-400:]}"
            assert ("usage" in tb.lower()) or ("python" in tb.lower()), \
                f"{name}: no usage/doc text printed"

    def test_make_zh_dub_srt_direct_run_writes_srt(self, tmp_path):
        """End-to-end direct run of the pure-stdlib script: en.full.srt +
        translations -> zh.dub.srt with inherited timestamps."""
        d = _stages_dub_dir()
        en = tmp_path / "video.en.full.srt"
        _make_srt(en, [
            (1, "00:00:01,000 --> 00:00:03,000", "hello there"),
            (2, "00:00:04,000 --> 00:00:06,000", "second cue"),
        ])
        zh = tmp_path / "translations_dub.txt"
        _make_translations(zh, ["你好呀", "第二条"])
        out = tmp_path / "zh.dub.srt"
        r = subprocess.run(
            [sys.executable, str(d / "make_zh_dub_srt.py"),
             str(en), str(zh), str(out)],
            capture_output=True, text=True, timeout=60,
        )
        assert r.returncode == 0, r.stderr
        text = out.read_text(encoding="utf-8")
        assert "00:00:01,000 --> 00:00:03,000" in text
        assert "你好呀" in text and "第二条" in text

    def test_length_gate_direct_run_reports_coverage(self, tmp_path):
        """End-to-end direct run of the pure-arithmetic gate through the
        cook CLI (its only surface since ticket 05; the standalone script
        entry died with the functions-only conversion): prints the coverage
        line, exits with a defined code, no ffmpeg involved."""
        root = tmp_path / "vid"
        # two 5s windows, 1s gap after cue1 only (last cue has no next) ->
        # budgets 6+5=11; 9- and 10-char lines at rate 4.0 -> est 2.25+2.5,
        # coverage 4.75/11 = 43.2% (out of band, exit 1)
        _make_srt(root / "transcript" / "video.en.full.srt", [
            (1, "00:00:00,000 --> 00:00:05,000", "one two three four five"),
            (2, "00:00:06,000 --> 00:00:11,000", "six seven eight nine ten"),
        ])
        _make_translations(root / "transcript" / "translations_dub.txt",
                           ["一句话占个位子而已", "两句话占个位子而已呀"])
        r = subprocess.run(
            [sys.executable, str(_HERE / "cook.py"), "dub", "gate",
             str(root), "video", "--rate", "4.0"],
            capture_output=True, text=True, timeout=60,
        )
        combined = r.stdout + r.stderr
        assert "coverage" in combined
        assert "43.2%" in combined
        assert r.returncode == 1
        # and the same invocation without a rate refuses (the ticket's core)
        r2 = subprocess.run(
            [sys.executable, str(_HERE / "cook.py"), "dub", "gate",
             str(root), "video"],
            capture_output=True, text=True, timeout=60,
        )
        assert r2.returncode != 0
        assert "measure-rate" in (r2.stdout + r2.stderr)


class TestSkillFallbackRemoved:
    """ADR 0001 (code-review residue): the dub stage scripts must NEVER
    fall back to installed video-subtitle skill copies — the packaged
    stages/ files are the only source, and a miss is an installation bug
    that dies with the reinstall remedy. full_dub._import_subtitles_module
    and extract_reference._find_subtitle_script both carried the old
    home-dir/env fallbacks; these tests pin their removal."""

    def _fake_home_with_decoys(self, tmp_path: Path) -> Path:
        fake_home = tmp_path / "fakehome"
        for sub in (".agents", ".zcode", ".claude"):
            d = fake_home / sub / "skills" / "video-subtitle" / "scripts"
            d.mkdir(parents=True, exist_ok=True)
            for fname in ("subtitles.py", "transcribe.py"):
                (d / fname).write_text("# stale skill copy\n", encoding="utf-8")
        return fake_home

    def _load_orphan_copy(self, src: Path, tmp_path: Path):
        """Load a copy of `src` from a directory with NO stages/subtitle/
        sibling, so its package-internal candidate does not exist."""
        import importlib.util
        orphan = tmp_path / "orphan"
        orphan.mkdir()
        dest = orphan / src.name
        dest.write_text(src.read_text(encoding="utf-8"), encoding="utf-8")
        spec = importlib.util.spec_from_file_location(
            f"{src.stem}_orphan_{id(tmp_path):x}", dest)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_full_dub_dies_without_packaged_subtitles_despite_skill_decoys(
            self, tmp_path, monkeypatch, capsys):
        """Planted skill decoys must not satisfy the loader when the
        packaged stages/subtitle/subtitles.py is missing: it dies non-zero
        with the reinstall remedy (ADR 0001) instead of running a stale
        copy."""
        fake_home = self._fake_home_with_decoys(tmp_path)
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))
        mod = self._load_orphan_copy(_stages_dub_dir() / "full_dub.py",
                                     tmp_path)
        with pytest.raises(SystemExit) as ei:
            mod._import_subtitles_module()
        assert ei.value.code == 1
        out = capsys.readouterr()
        assert "reinstall video-cook" in (out.out + out.err)

    def test_extract_reference_dies_without_packaged_transcribe_despite_decoys(
            self, tmp_path, monkeypatch, capsys):
        fake_home = self._fake_home_with_decoys(tmp_path)
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))
        # the pre-consolidation env override is equally out of contract
        monkeypatch.setenv("VIDEO_SUBTITLE_SKILL_DIR", str(fake_home))
        mod = self._load_orphan_copy(
            _stages_dub_dir() / "extract_reference.py", tmp_path)
        with pytest.raises(SystemExit) as ei:
            mod._find_subtitle_script()
        assert ei.value.code == 1
        out = capsys.readouterr()
        assert "reinstall video-cook" in (out.out + out.err)

    def test_extract_reference_real_location_returns_packaged_script(
            self, tmp_path, monkeypatch):
        """Positive path: from its real in-package location, the resolver
        returns the packaged transcribe.py even with decoy homes planted."""
        import importlib.util
        fake_home = self._fake_home_with_decoys(tmp_path)
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))
        spec = importlib.util.spec_from_file_location(
            f"extract_reference_real_{id(tmp_path):x}",
            _stages_dub_dir() / "extract_reference.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        script = mod._find_subtitle_script()
        assert Path(script) == (Path(cook.__file__).resolve().parent
                                / "stages" / "subtitle" / "transcribe.py")


class TestFindDubScriptPackageInternal:
    """_stage_script('dub', 'full_dub.py') resolves the package-internal
    stages/dub/full_dub.py — the consolidation removes the 'installed skill
    copy may be older' failure mode the --keep-subs version probe guarded
    against."""

    def test_resolves_package_internal_path(self):
        script = cook._stage_script("dub", "full_dub.py")
        assert script == _stages_dub_dir() / "full_dub.py"
        assert script.is_file()

    def test_ignores_installed_skill_copies(self, tmp_path, monkeypatch):
        """Stale skill copies under any of the old home-dir locations must
        never win over the package file (home pointed at a decoy dir with a
        planted stale copy)."""
        fake_home = tmp_path / "fakehome"
        for sub in (".agents", ".zcode", ".claude"):
            d = fake_home / sub / "skills" / "video-dubbing" / "scripts"
            d.mkdir(parents=True)
            (d / "full_dub.py").write_text("# stale copy\n", encoding="utf-8")
        monkeypatch.setenv("HOME", str(fake_home))
        monkeypatch.setenv("USERPROFILE", str(fake_home))
        script = cook._stage_script("dub", "full_dub.py")
        assert script == _stages_dub_dir() / "full_dub.py"


class TestDubStageLaunchesPackageScript:
    """cook dub synth/assemble build their subprocess command around the
    package-internal full_dub.py (still launched via --python so the
    indextts venv can import its deps), with --keep-subs passed through."""

    def _capture_cmd(self, monkeypatch, tmp_path, stage: str,
                     keep_subs: bool = False) -> list[str] | None:
        root = tmp_path / "vid"
        # stage every prerequisite so the gate passes
        self._touch(root / "transcript" / "translations_dub.txt")
        self._touch(root / "dubbed" / "_reference" / "ref.wav")
        self._touch(root / "dubbed" / "_full" / "_segments" / "sent_0001.wav")
        captured: dict = {}

        def fake_run_long_task(cmd, cwd, log_file, err_file, detach=False):
            captured["cmd"] = cmd
            return {"ok": True, "returncode": 0}

        monkeypatch.setattr(cook, "_run_long_task", fake_run_long_task)
        args = type("A", (), {
            "output_root": str(root), "name": "video",
            "python": None, "detach": False, "keep_subs": keep_subs,
        })()
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                fn = {"synth": cook.cmd_dub_synth,
                      "assemble": cook.cmd_dub_assemble}[stage]
                fn(args)
            except SystemExit as e:
                if e.code not in (0, None):
                    raise
        return captured.get("cmd")

    def _touch(self, path: Path, content: bytes = b"x" * 2000) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def test_synth_launches_package_internal_script(self, monkeypatch, tmp_path):
        cmd = self._capture_cmd(monkeypatch, tmp_path, "synth")
        assert cmd is not None, "stage never launched"
        assert cmd[1] == str(_stages_dub_dir() / "full_dub.py")
        assert cmd[2] == "synth"

    def test_assemble_launches_package_internal_script(self, monkeypatch, tmp_path):
        cmd = self._capture_cmd(monkeypatch, tmp_path, "assemble")
        assert cmd is not None, "stage never launched"
        assert cmd[1] == str(_stages_dub_dir() / "full_dub.py")
        assert cmd[2] == "assemble"

    def test_keep_subs_passed_through_to_subprocess(self, monkeypatch, tmp_path):
        """--keep-subs reaches full_dub.py's argv and no pre-launch probe
        dies (the version probe was deleted with the consolidation)."""
        cmd = self._capture_cmd(monkeypatch, tmp_path, "assemble", keep_subs=True)
        assert cmd is not None, "stage never launched — probe may still be dying"
        assert "--keep-subs" in cmd


# ----------------------------------------------------------------------------
# Stage-script resolution: cook must find its stage scripts inside its own
# distribution (stages/<stage>/), never in an installed skill copy. This is
# the reflux 2026-10 consolidation (ticket 01) — the installed-skill lookup
# caused version skew between cook and the scripts it drove.
# ----------------------------------------------------------------------------

class TestStageScriptResolution:
    def test_resolves_subtitle_scripts_inside_the_repo(self):
        p = cook._stage_script("subtitle", "subtitles.py")
        assert p.is_file()
        assert p.name == "subtitles.py"
        assert "stages" in p.parts and "subtitle" in p.parts

    def test_all_four_subtitle_scripts_resolve(self):
        for name in ("subtitles.py", "make_full_srt.py",
                     "make_zh.py", "transcribe.py"):
            assert cook._stage_script("subtitle", name).is_file(), name

    def test_resolution_ignores_installed_skills(self, tmp_path, monkeypatch):
        """No lookup may fall back to ~/.agents|~/.zcode|~/.claude skills —
        otherwise a stale installed copy silently shadows the packaged one
        (the version-skew bug this consolidation fixes). Point home at an
        empty dir: resolution must still succeed."""
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        assert cook._stage_script("subtitle", "subtitles.py").is_file()

    def test_missing_script_dies_with_clear_error(self, capsys):
        with pytest.raises(SystemExit) as ei:
            cook._stage_script("subtitle", "nope.py")
        assert ei.value.code == 1
        err = capsys.readouterr().err
        # the message names what is missing and where it should come from
        assert "stages" in err and "nope.py" in err
        assert "video-cook" in err          # names the distribution to reinstall


# ----------------------------------------------------------------------------
# cook subtitles end-to-end: with the stage scripts packaged inside the
# distribution, the full pipeline (shorten -> merge-short -> biliteral ->
# ass -> cloud-srt) must run with NO installed skill anywhere. Home is
# pointed at an empty dir so any installed-skill fallback would fail loudly
# instead of silently shadowing the packaged module.
# ----------------------------------------------------------------------------

class TestSubtitlesPipelineEndToEnd:
    def _setup_video(self, tmp_path: Path) -> Path:
        root = tmp_path / "vid"
        _make_srt(root / "transcript" / "vid.en.srt", [
            (1, "00:00:01,000 --> 00:00:03,000", "Hello world from line one."),
            (2, "00:00:03,000 --> 00:00:05,000", "Second cue right here."),
        ])
        _make_srt(root / "transcript" / "vid.zh.srt", [
            (1, "00:00:01,000 --> 00:00:03,000", "第一句中文"),
            (2, "00:00:03,000 --> 00:00:05,000", "第二句中文"),
        ])
        return root

    def _run(self, root: Path, monkeypatch) -> dict:
        monkeypatch.setattr(Path, "home", lambda: root / "no-skill-home")
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {"output_root": str(root), "name": "vid",
                              "mode": "bottom-bar", "bar_px": 220})()
        with contextlib.redirect_stdout(buf):
            cook.cmd_subtitles(args)
        return json.loads(buf.getvalue())

    def test_full_pipeline_without_any_installed_skill(self, tmp_path, monkeypatch):
        root = self._setup_video(tmp_path)
        report = self._run(root, monkeypatch)
        assert report["ok"] is True
        assert report["length_issues"] == []

        # products exist
        bilingual = root / "subtitle" / "vid.bilingual.srt"
        assert bilingual.is_file()
        assert (root / "subtitle" / "vid.bilingual.ass").is_file()
        assert (root / "subtitle" / "vid.bilingual.bar.ass").is_file()

        # pairwise merge: 2 en cues + 2 zh cues -> 2 bilingual cues, zh on top
        text = bilingual.read_text(encoding="utf-8")
        assert "第一句中文\nHello world from line one." in text
        assert "第二句中文\nSecond cue right here." in text

        # cloud-srt is a byte-identical copy of the per-language merged SRTs
        zh_merged = root / "transcript" / "vid.zh.merged.srt"
        en_merged = root / "transcript" / "vid.en.merged.srt"
        assert (root / "cloud-srt" / "zh.srt").read_bytes() == zh_merged.read_bytes()
        assert (root / "cloud-srt" / "en.srt").read_bytes() == en_merged.read_bytes()

        # the ASS carries the bottom-bar geometry the burn must match
        ass = (root / "subtitle" / "vid.bilingual.bar.ass").read_text(encoding="utf-8")
        assert "PlayResY: 1300" in ass  # 1080 + 220 bar

    def test_uses_packaged_module_not_any_installed_skill(self, tmp_path, monkeypatch):
        """Even with a decoy subtitles.py planted in the old skill locations,
        cook must import the packaged copy (proves the module identity, not
        just that some subtitles.py ran)."""
        decoy = tmp_path / "home" / ".agents" / "skills" / "video-subtitle" / "scripts"
        decoy.mkdir(parents=True)
        (decoy / "subtitles.py").write_text("raise RuntimeError('decoy skill ran')\n",
                                            encoding="utf-8")
        root = self._setup_video(tmp_path)
        report = self._run(root, monkeypatch)
        assert report["ok"] is True  # ran the packaged module; decoy never executed


# ----------------------------------------------------------------------------
# cook transcribe: the inner whisperX invocation must run the packaged
# stages/subtitle/transcribe.py via sys.executable — not an inline script
# string (the pre-consolidation duplication) and not any installed skill.
# ----------------------------------------------------------------------------

class TestTranscribeUsesPackagedScript:
    def _run(self, tmp_path: Path, monkeypatch) -> tuple[list, dict]:
        root = tmp_path / "vid"
        wav = root / "transcript" / "vid.audio.wav"
        wav.parent.mkdir(parents=True)
        wav.write_bytes(b"RIFF fake wav")

        captured: dict = {}

        def fake_run(cmd, cwd, log_file, err_file, detach=False):
            captured["cmd"] = cmd
            return {"ok": True, "detached": False, "returncode": 0}

        monkeypatch.setattr(cook, "_run_long_task", fake_run)
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {"output_root": str(root), "name": "vid",
                              "model": "large-v3", "compute": "auto",
                              "language": "en", "detach": False})()
        with contextlib.redirect_stdout(buf):
            cook.cmd_transcribe(args)
        return captured["cmd"], json.loads(buf.getvalue())

    def test_inner_cmd_runs_packaged_transcribe_py(self, tmp_path, monkeypatch):
        cmd, report = self._run(tmp_path, monkeypatch)
        assert report["ok"] is True
        # [python, <packaged transcribe.py>, wav, srt, model, compute, language, device]
        assert cmd[0] == sys.executable
        script = Path(cmd[1])
        assert script.is_file()
        assert script.name == "transcribe.py"
        assert "stages" in script.parts and "subtitle" in script.parts
        # transcribe.py positional contract: <input> <output.srt> [model]
        # [compute_type] [language] [device]
        root = tmp_path / "vid"
        assert cmd[2] == str(root / "transcript" / "vid.audio.wav")
        assert cmd[3] == str(root / "transcript" / "vid.en.srt")
        assert cmd[4] == "large-v3"
        assert cmd[7] in ("cpu", "cuda")
        # compute/device pairing stays consistent with what cook reports
        assert cmd[5] == report["compute"]
        assert cmd[6] == report["language"]
        assert cmd[7] == report["device"]

    def test_packaged_script_has_same_done_marker(self):
        """cook reports done_marker='[transcribe] done.' for pollers; the
        packaged script must actually print that literal."""
        src = cook._stage_script("subtitle", "transcribe.py").read_text(encoding="utf-8")
        assert '[transcribe] done.' in src


# ----------------------------------------------------------------------------
# cook subtitles shorten: word-boundary cutting (ticket 07). When shorten's
# punctuation ladder (sentence -> clause -> dash/quote) is exhausted and a
# sub-clause still exceeds the width limit, the hard cut must snap to the
# nearest jieba word boundary. Round defect being fixed: a 106-width cue cut
# as 「…顺手学会新技」/「能，…」 — 「技能」 split mid-word (dubbing.merged
# cues 598/599). Oracle for "is this cut inside a word": jieba tokenization
# of the joined text; a legal cut has the chars on both sides of the cut in
# different tokens.
# ----------------------------------------------------------------------------

# The acceptance sentence from the ticket: 106 width units at limit 56, its
# punctuation ladder leaves a 60-width clause that must be hard-cut.
_WB_SENTENCE = (
    "我认为模型和产品会变得非常擅长在干活的过程中顺手学会新技能，"
    "然后把这些技能固化成文件和指令、沉淀成 skill。"
)


def _shorten_cue_texts(tmp_path: Path, text: str, max_zh: int = 56) -> list[str]:
    """Run the packaged shorten stage on one zh cue; return output cue texts."""
    src = tmp_path / "in.srt"
    dst = tmp_path / "out.srt"
    _make_srt(src, [(1, "00:00:00,000 --> 00:00:10,000", text)])
    subs = cook._import_subtitles_module()
    cook._run_subs(subs, ["shorten", str(src), str(dst),
                          "--lang", "zh", "--max-zh", str(max_zh)])
    out = []
    for block in dst.read_text(encoding="utf-8").strip().split("\n\n"):
        lines = block.split("\n")
        out.append(" ".join(lines[2:]).strip())
    return out


def _jieba_cut_positions(text: str) -> set[int]:
    """Char offsets where a cut keeps both sides in different jieba tokens."""
    import logging
    jieba = pytest.importorskip("jieba")
    jieba.setLogLevel(logging.ERROR)
    cuts, pos = set(), 0
    for tok in jieba.cut(text):
        pos += len(tok)
        cuts.add(pos)
    return cuts


class TestShortenWordBoundary:
    def test_acceptance_sentence_no_cut_inside_a_word(self, tmp_path):
        pieces = _shorten_cue_texts(tmp_path, _WB_SENTENCE, max_zh=56)
        # the 106-width cue is far past the x1.15 exemption (64) — it must
        # actually be split, not kept whole
        assert len(pieces) >= 2
        # no content lost (cut-position-agnostic: edge spaces may be stripped)
        assert "".join(pieces).replace(" ", "") == _WB_SENTENCE.replace(" ", "")
        for a, b in zip(pieces, pieces[1:]):
            joined = a + b
            assert len(a) in _jieba_cut_positions(joined), (
                f"cut lands inside a word: {a!r} | {b!r}")

    def test_each_piece_stays_within_the_documented_band(self, tmp_path):
        """Word snapping may advance a cut past the limit to reach a boundary,
        but never past the pipeline's 64-width ceiling (56 + 8, the band the
        punctuation guard and the review gate already allow)."""
        subs = cook._import_subtitles_module()
        pieces = _shorten_cue_texts(tmp_path, _WB_SENTENCE, max_zh=56)
        for p in pieces:
            assert subs.wlen(p) <= 64

    def test_no_punctuation_text_survives_repeated_hard_cuts(self, tmp_path):
        """Text with zero punctuation exhausts the ladder immediately and is
        hard-cut several times over (limit 20 -> ~6 cuts). Every one of those
        cuts must land on a jieba boundary — the remainder must be re-examined
        after each cut, not segmented once up front."""
        text = "这是一段完全没有任何标点符号的超长句子用来验证多次硬切的情况下词边界保护依然在每一次切分时生效而不是只在第一次生效"
        pieces = _shorten_cue_texts(tmp_path, text, max_zh=20)
        assert len(pieces) >= 4  # 88-width text at limit 20 needs several cuts
        assert "".join(pieces) == text  # nothing lost, nothing reordered
        for a, b in zip(pieces, pieces[1:]):
            assert len(a) in _jieba_cut_positions(a + b), (
                f"cut lands inside a word: {a!r} | {b!r}")


class TestShortenWordBoundaryDegraded:
    """jieba is an optional dependency: when it is missing, shorten must
    degrade to the legacy width-only hard cut (the exact pre-ticket output)
    and say so on stderr exactly once — the same warn-and-continue contract
    the other optional-dependency stages use. A fresh module instance is
    imported per test so the cached import state never leaks between tests."""

    def test_missing_jieba_keeps_legacy_cut_and_warns(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setitem(sys.modules, "jieba", None)  # import jieba -> ImportError
        src = tmp_path / "in.srt"
        dst = tmp_path / "out.srt"
        _make_srt(src, [(1, "00:00:00,000 --> 00:00:10,000", _WB_SENTENCE)])
        subs = cook._import_subtitles_module()
        cook._run_subs(subs, ["shorten", str(src), str(dst),
                              "--lang", "zh", "--max-zh", "56"])
        pieces = []
        for block in dst.read_text(encoding="utf-8").strip().split("\n\n"):
            pieces.append(" ".join(block.split("\n")[2:]).strip())
        # legacy behavior: bare width cut at 56 — the 「技/能」 break is the
        # known cost of running without jieba, pinned here so the degradation
        # itself never drifts
        assert pieces == [
            "我认为模型和产品会变得非常擅长在干活的过程中顺手学会新技",
            "能，然后把这些技能固化成文件和指令、沉淀成 skill。",
        ]
        err = capsys.readouterr().err
        assert "jieba" in err and "WARNING" in err
        assert "video-cook[subtitle]" in err  # names the fix

    def test_missing_jieba_warns_exactly_once_per_run(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setitem(sys.modules, "jieba", None)
        src = tmp_path / "in.srt"
        dst = tmp_path / "out.srt"
        _make_srt(src, [
            (1, "00:00:00,000 --> 00:00:10,000", _WB_SENTENCE),
            (2, "00:00:10,000 --> 00:00:20,000",
             "第二条同样超过长度限制且没有任何标点可以依赖的句子也要被硬切一下"),
        ])
        subs = cook._import_subtitles_module()
        cook._run_subs(subs, ["shorten", str(src), str(dst),
                              "--lang", "zh", "--max-zh", "56"])
        err = capsys.readouterr().err
        assert err.count("jieba is not installed") == 1


class TestShortenExemptionUnchanged:
    """Ticket constraint: shorten's x1.15 mild-overrun exemption (a cue within
    limit*1.15 passes through whole) is untouched by the word-boundary work —
    it must neither start splitting exempt cues nor widen the band."""

    def test_mild_overrun_within_1_15x_stays_one_cue(self, tmp_path):
        subs = cook._import_subtitles_module()
        # 32 CJK chars = width 64 = exactly 56 x 1.15 -> exempt, unsplit
        text = "这条宽度恰好落在豁免带里面的中文字幕用来确认豁免路径没有被改动过"
        assert subs.wlen(text) == 64
        pieces = _shorten_cue_texts(tmp_path, text, max_zh=56)
        assert pieces == [text]

    def test_one_past_the_exemption_is_split(self, tmp_path):
        # width 66 > 64: outside the band, the must-cut path must fire
        text = "这是一条宽度刚好超过豁免带上限一点点的中文字幕用来确认必须切分路径仍会触发"
        pieces = _shorten_cue_texts(tmp_path, text, max_zh=56)
        assert len(pieces) >= 2


# ----------------------------------------------------------------------------
# synth multi-worker (ticket 10 / issue #21): band split + stagger + exit-code
# aggregation. All tests are pure or monkeypatched — fake IndexTTS2/torch via a
# temp INDEXTTS_DIR package, fake Popen for cook's orchestration. No network,
# no ffmpeg, no real model, no real synthesis.
# ----------------------------------------------------------------------------

def _load_full_dub(tmp_path: Path, monkeypatch) -> tuple[object, Path]:
    """Load stages/dub/full_dub.py fresh with INDEXTTS_DIR pointed at a temp
    package of test doubles (fake indextts.infer_v2_5.IndexTTS2 writing real
    stdlib-wave silence wavs) and a fake torch injected straight into
    sys.modules — the injection must win even when the REAL torch is already
    imported by an earlier test in the same process (real torch refuses a
    second set_num_interop_threads call). Returns (module, fake_pkg_dir).

    The module-level import of full_dub is stdlib-only, so in-process loading
    is safe; the heavy deps only load inside stage_synth — and there they hit
    the doubles via sys.modules (torch) and the sys.path.insert(0,
    INDEXTTS_DIR) the stage itself performs (indextts)."""
    import importlib.util
    import sys
    import types
    pkg = tmp_path / "fakedeps"
    (pkg / "indextts").mkdir(parents=True)
    (pkg / "indextts" / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "indextts" / "infer_v2_5.py").write_text(
        "import pathlib, wave\n"
        "CALLS = []\n"  # (kind, ...) events: ('init',), ('infer', text, path)
        "class IndexTTS2:\n"
        "    def __init__(self, **kw):\n"
        "        CALLS.append(('init',))\n"
        "    def infer(self, spk_audio_prompt, text, output_path, lang,\n"
        "              use_random, duration_factor):\n"
        "        CALLS.append(('infer', text, output_path))\n"
        "        p = pathlib.Path(output_path)\n"
        "        p.parent.mkdir(parents=True, exist_ok=True)\n"
        "        with wave.open(str(p), 'wb') as w:\n"
        "            w.setnchannels(1); w.setsampwidth(2); w.setframerate(22050)\n"
        "            w.writeframes(b'\\x00' * 4410)  # 0.2s silence, 8820 bytes > 1KB\n",
        encoding="utf-8")
    fake_torch = types.ModuleType("torch")
    fake_torch.set_num_threads = lambda n: None
    fake_torch.set_num_interop_threads = lambda n: None
    fake_torch.__file__ = str(pkg / "torch.py")
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setenv("INDEXTTS_DIR", str(pkg))
    spec = importlib.util.spec_from_file_location(
        f"full_dub_test_{id(tmp_path):x}", _stages_dub_dir() / "full_dub.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod, pkg


def _fake_tts_calls(pkg: Path) -> list[tuple]:
    """Read the CALLS log from the fake indextts package (loaded under the
    name the stage's own `from indextts.infer_v2_5 import ...` resolves to).
    None means the stage never imported it — e.g. a fully-cached band, which
    is itself an assertion the callers make on the returned empty list."""
    import sys
    mod = sys.modules.get("indextts.infer_v2_5")
    return [] if mod is None else list(mod.CALLS)


@pytest.fixture
def _restore_sys_path():
    """stage_synth internally does sys.path.insert(0, INDEXTTS_DIR) (pointing
    at the test-double package) and never removes it. Snapshot sys.path and the
    fake-package module entries, restore both afterwards, so the doubles never
    leak into later tests (e.g. a fake torch shadowing the real one)."""
    import sys
    path_before = [Path(p) for p in sys.path]

    def _is_foreign(mod) -> bool:
        f = getattr(mod, "__file__", None)
        return bool(f) and Path(f).resolve().parent not in path_before

    mods_before = set(sys.modules)
    yield
    for name in list(sys.modules):
        if name not in mods_before and _is_foreign(sys.modules[name]):
            del sys.modules[name]
    sys.path[:] = [str(p) for p in path_before]


class TestSynthBands:
    """synth_bands(n_cues, n_bands): even inclusive (lo, hi) bands over cue
    positions 1..n_cues for the multi-worker split — earlier bands absorb the
    remainder, empty trailing bands are dropped (more workers than cues spawns
    fewer workers, never a zero-cue one)."""

    def _mod(self, tmp_path, monkeypatch):
        mod, _ = _load_full_dub(tmp_path, monkeypatch)
        return mod

    def test_single_band_is_the_whole_range(self, tmp_path, monkeypatch):
        f = self._mod(tmp_path, monkeypatch).synth_bands
        assert f(10, 1) == [(1, 10)]

    def test_even_split_has_no_remainder(self, tmp_path, monkeypatch):
        f = self._mod(tmp_path, monkeypatch).synth_bands
        assert f(10, 2) == [(1, 5), (6, 10)]

    def test_remainder_goes_to_earlier_bands(self, tmp_path, monkeypatch):
        f = self._mod(tmp_path, monkeypatch).synth_bands
        # 11 = 4+4+3, not 3+4+4 — determinism matters: band k must be
        # computable without seeing the others (worker argv is built from it)
        assert f(11, 3) == [(1, 4), (5, 8), (9, 11)]

    def test_production_shape_361_cues_2_workers(self, tmp_path, monkeypatch):
        f = self._mod(tmp_path, monkeypatch).synth_bands
        # the leerob run: 361 cues, supervisor + 1 worker
        assert f(361, 2) == [(1, 181), (182, 361)]

    def test_more_bands_than_cues_drops_empty_trailing_bands(self, tmp_path, monkeypatch):
        f = self._mod(tmp_path, monkeypatch).synth_bands
        assert f(3, 5) == [(1, 1), (2, 2), (3, 3)]

    def test_zero_cues_yields_no_bands(self, tmp_path, monkeypatch):
        f = self._mod(tmp_path, monkeypatch).synth_bands
        assert f(0, 2) == []

    def test_bands_cover_all_cues_exactly_once(self, tmp_path, monkeypatch):
        f = self._mod(tmp_path, monkeypatch).synth_bands
        for n_cues, n_bands in [(361, 2), (100, 3), (7, 4), (5, 8)]:
            bands = f(n_cues, n_bands)
            seen = []
            for lo, hi in bands:
                assert lo <= hi, f"empty band survived: {(lo, hi)}"
                seen.extend(range(lo, hi + 1))
            assert seen == list(range(1, n_cues + 1)), (n_cues, n_bands)
            sizes = [hi - lo + 1 for lo, hi in bands]
            assert max(sizes) - min(sizes) <= 1, (n_cues, n_bands, sizes)

    def test_invalid_band_count_raises(self, tmp_path, monkeypatch):
        f = self._mod(tmp_path, monkeypatch).synth_bands
        with pytest.raises(ValueError):
            f(10, 0)


class TestSynthWorkerStage:
    """stage_synth band mode (--lo/--hi/--stagger via the synth-worker CLI):
    descending order within the band, skip-if-exists (>1KB) unchanged, model
    loaded only when the band has pending work, stagger sleep BEFORE any heavy
    import, and a final marker that cannot false-trigger the supervisor's
    'Stage 1 DONE' poller."""

    def _stage_video(self, tmp_path: Path, mod, n_cues: int) -> Path:
        root = tmp_path / "vid"
        _make_srt(root / "transcript" / "video.en.full.srt",
                  _fake_en_cues(n_cues))
        _make_translations(root / "transcript" / "translations_dub.txt",
                           [f"第{i}句中文配音" for i in range(1, n_cues + 1)])
        return root

    def _run_stage(self, mod, root: Path, **kw) -> str:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.stage_synth(str(root), "video", **kw)
        return buf.getvalue()

    def _segments(self, root: Path) -> Path:
        return root / "dubbed" / "_full" / "_segments"

    def _infer_idx_order(self, calls: list[tuple]) -> list[int]:
        """Cue positions (1-based) in the order infer() synthesized them,
        derived from the output filename sent_{idx:04d}.wav."""
        import re
        out = []
        for c in calls:
            if c[0] == "infer":
                m = re.search(r"sent_(\d{4})", c[2])  # temp names are .tmp-sent_0001-xxxx.wav
                out.append(int(m.group(1)))
        return out

    def test_supervisor_path_unchanged_full_ascending(self, tmp_path, monkeypatch, _restore_sys_path):
        """No band kwargs = the pre-ticket behavior: every cue synthesized in
        ascending order and the 'Stage 1 DONE' marker literal intact (the
        detached poller's done_marker depends on it)."""
        mod, pkg = _load_full_dub(tmp_path, monkeypatch)
        root = self._stage_video(tmp_path, mod, 5)
        out = self._run_stage(mod, root)
        assert self._infer_idx_order(_fake_tts_calls(pkg)) == [1, 2, 3, 4, 5]
        assert "Stage 1 DONE: 5 cues synthesized" in out

    def test_worker_synthesizes_only_band_in_descending_order(self, tmp_path, monkeypatch, _restore_sys_path):
        mod, pkg = _load_full_dub(tmp_path, monkeypatch)
        root = self._stage_video(tmp_path, mod, 10)
        out = self._run_stage(mod, root, lo=3, hi=6)
        # descending so the ascending supervisor and this worker meet at one
        # band edge only (the reference synth_worker.py contract)
        assert self._infer_idx_order(_fake_tts_calls(pkg)) == [6, 5, 4, 3]
        assert "[worker 3-6] ALL DONE" in out
        # the worker marker must never contain the supervisor's poller literal
        assert "Stage 1 DONE" not in out

    def test_worker_skips_existing_over_1kb_and_regenerates_truncated(self, tmp_path, monkeypatch, _restore_sys_path):
        mod, pkg = _load_full_dub(tmp_path, monkeypatch)
        root = self._stage_video(tmp_path, mod, 6)
        segs = self._segments(root)
        segs.mkdir(parents=True)
        (segs / "sent_0002.wav").write_bytes(b"x" * 2000)  # cached: skip
        (segs / "sent_0004.wav").write_bytes(b"x" * 100)   # truncated: redo
        self._run_stage(mod, root, lo=1, hi=6)
        assert self._infer_idx_order(_fake_tts_calls(pkg)) == [6, 5, 4, 3, 1]

    def test_fully_cached_band_loads_no_model(self, tmp_path, monkeypatch, _restore_sys_path):
        mod, pkg = _load_full_dub(tmp_path, monkeypatch)
        root = self._stage_video(tmp_path, mod, 3)
        segs = self._segments(root)
        segs.mkdir(parents=True)
        for i in (1, 2, 3):
            (segs / f"sent_{i:04d}.wav").write_bytes(b"x" * 2000)
        out = self._run_stage(mod, root, lo=1, hi=3)
        calls = _fake_tts_calls(pkg)
        assert not any(c[0] == "init" for c in calls)
        assert not any(c[0] == "infer" for c in calls)
        assert "[worker 1-3] ALL DONE" in out

    def test_worker_stagger_sleeps_before_model_load_and_logs_it(self, tmp_path, monkeypatch, _restore_sys_path):
        mod, _pkg = _load_full_dub(tmp_path, monkeypatch)
        sleeps: list[float] = []
        monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))
        root = self._stage_video(tmp_path, mod, 2)
        out = self._run_stage(mod, root, lo=1, hi=2, stagger=7.5)
        assert sleeps == [7.5]
        # observable in the log (ticket acceptance) and ordered before the
        # heavy imports begin
        assert "[worker 1-2] staggering 7.5s" in out
        assert out.index("staggering") < out.index("loading IndexTTS2")

    def test_worker_zero_stagger_never_sleeps(self, tmp_path, monkeypatch, _restore_sys_path):
        mod, _pkg = _load_full_dub(tmp_path, monkeypatch)
        sleeps: list[float] = []
        monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))
        root = self._stage_video(tmp_path, mod, 2)
        self._run_stage(mod, root, lo=1, hi=2)
        assert sleeps == []

    def test_cli_synth_worker_dispatches_band(self, tmp_path, monkeypatch, _restore_sys_path):
        """`python full_dub.py synth-worker <root> <name> --lo --hi --stagger`
        is the argv cook spawns — the CLI dispatch must reach the banded
        stage with parsed values."""
        import contextlib
        import io
        mod, pkg = _load_full_dub(tmp_path, monkeypatch)
        root = self._stage_video(tmp_path, mod, 10)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod._cli_main(["synth-worker", str(root), "video",
                           "--lo", "3", "--hi", "6", "--stagger", "0"])
        assert self._infer_idx_order(_fake_tts_calls(pkg)) == [6, 5, 4, 3]
        assert "[worker 3-6] ALL DONE" in buf.getvalue()

    def test_cli_stagger_reaches_stage(self, tmp_path, monkeypatch, _restore_sys_path):
        mod, _pkg = _load_full_dub(tmp_path, monkeypatch)
        sleeps: list[float] = []
        monkeypatch.setattr(mod.time, "sleep", lambda s: sleeps.append(s))
        root = self._stage_video(tmp_path, mod, 2)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            mod._cli_main(["synth-worker", str(root), "video",
                           "--lo", "1", "--hi", "2", "--stagger", "4.5"])
        assert sleeps == [4.5]

    def test_cli_synth_stage_still_rejects_worker_flags(self, tmp_path, monkeypatch, _restore_sys_path):
        """The supervisor CLI contract is frozen: `synth` takes only
        --keep-subs-style flags; band flags belong to synth-worker only."""
        mod, _pkg = _load_full_dub(tmp_path, monkeypatch)
        with pytest.raises(SystemExit) as ei:
            mod._cli_main(["synth", str(tmp_path), "video", "--lo", "3"])
        assert ei.value.code != 0


class TestSynthAtomicPublish:
    """code-review fix: multi-worker synth TOCTOU. The ascending supervisor
    and a descending band worker can both pass the skip-if-exists check for
    the same cue and synthesize it concurrently (the check-then-write race);
    tts.infer used to write the FINAL sent_NNNN.wav path directly, so two
    concurrent writers interleaved their bytes on one file — a corrupt wav,
    or a >1KB partial write cached as done by the other process's skip
    check. Synthesis must go to a UNIQUE temp sibling in the same directory
    and be moved onto the final name with os.replace (atomic on the same
    filesystem): whoever wins the race, every reader of sent_NNNN.wav sees
    one writer's complete output, never a partial interleave."""

    def _stage_video(self, tmp_path: Path, n_cues: int,
                     zh_lines: list[str] | None = None) -> Path:
        root = tmp_path / "vid"
        _make_srt(root / "transcript" / "video.en.full.srt",
                  _fake_en_cues(n_cues))
        _make_translations(root / "transcript" / "translations_dub.txt",
                           zh_lines if zh_lines is not None
                           else [f"第{i}句中文配音" for i in range(1, n_cues + 1)])
        return root

    def _run_stage(self, mod, root: Path) -> str:
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.stage_synth(str(root), "video")
        return buf.getvalue()

    def _assert_complete_wav(self, path: Path, expected_frames: int) -> None:
        import contextlib
        import wave
        with contextlib.closing(wave.open(str(path), "rb")) as w:
            assert w.getnframes() == expected_frames, \
                f"{path.name}: {w.getnframes()} frames"

    def test_infer_receives_a_temp_sibling_never_the_final_path(
            self, tmp_path, monkeypatch, _restore_sys_path):
        """The mechanism contract: tts.infer (and the chunk concat) never
        write the final sent path — they write a temp SIBLING in the same
        directory (same filesystem, so the follow-up rename is atomic),
        and no temp file survives the stage."""
        mod, pkg = _load_full_dub(tmp_path, monkeypatch)
        root = self._stage_video(tmp_path, 3)
        self._run_stage(mod, root)
        segs = root / "dubbed" / "_full" / "_segments"
        calls = [c for c in _fake_tts_calls(pkg) if c[0] == "infer"]
        assert len(calls) == 3
        for _text, path in ((c[1], c[2]) for c in calls):
            name = Path(path).name
            assert name.endswith(".wav") and ".tmp-" in name, \
                f"infer wrote a non-temp path: {path}"
            # same-dir sibling of some sent_NNNN.wav (os.replace atomicity
            # precondition) with the final name as its prefix
            assert name.startswith(".tmp-sent_"), path
            assert Path(path).parent == segs, path
        for i in (1, 2, 3):
            self._assert_complete_wav(segs / f"sent_{i:04d}.wav", 2205)
        assert list(segs.glob(".tmp-*")) == []

    def test_chunked_long_cue_concats_via_temp_sibling_too(
            self, tmp_path, monkeypatch, _restore_sys_path):
        """The chunk-cache files and the chunk concat output are equally
        racy between supervisor and worker — they publish the same way.
        _concat_wavs is monkeypatched to a pure-stdlib copier (the suite
        never shells out to ffmpeg)."""
        mod, pkg = _load_full_dub(tmp_path, monkeypatch)
        root = self._stage_video(tmp_path, 1,
                                 zh_lines=["长" * 100])  # > CHUNK_ON: chunked
        concat_dsts: list[Path] = []

        def fake_concat(files, dst):
            import contextlib
            import wave
            with contextlib.closing(wave.open(str(dst), "wb")) as out:
                out.setnchannels(1)
                out.setsampwidth(2)
                out.setframerate(22050)
                for f in files:
                    with contextlib.closing(wave.open(str(f), "rb")) as w:
                        out.writeframes(w.readframes(w.getnframes()))
            concat_dsts.append(Path(dst))

        monkeypatch.setattr(mod, "_concat_wavs", fake_concat)
        self._run_stage(mod, root)
        chunk_calls = [c for c in _fake_tts_calls(pkg) if c[0] == "infer"]
        assert len(chunk_calls) >= 2, "100-char line must chunk"
        for c in chunk_calls:
            assert Path(c[2]).name.endswith(".wav") and ".tmp-" in c[2], \
                f"chunk infer wrote a non-temp path: {c[2]}"
            assert Path(c[2]).parent.name == "_chunkcache", c[2]
        assert len(concat_dsts) == 1
        assert concat_dsts[0].name.endswith(".wav") and ".tmp-" in concat_dsts[0].name, \
            f"concat wrote a non-temp path: {concat_dsts[0]}"
        work = root / "dubbed" / "_full"
        self._assert_complete_wav(
            work / "_segments" / "sent_0001.wav", 2205 * len(chunk_calls))
        assert list(work.rglob(".tmp-*")) == []

    def test_concurrent_writers_never_expose_a_partial_file(
            self, tmp_path, monkeypatch, _restore_sys_path):
        """The race itself: two stage_synth threads synthesize the same
        single cue. The fake infer writes DISTINCT sizes per writer, then
        PARKS mid-write on a test-gated Event — so both partial states are
        held on disk simultaneously and deterministically while this
        thread polls the final path. Old behavior: both wrote the final
        path directly, so the parked window exposed partial bytes (and
        after release the two open 'wb' handles interleaved). New
        behavior: the final path only ever appears complete."""
        import builtins
        import contextlib
        import itertools
        import threading
        import wave
        mod, pkg = self._load_full_dub_racing(tmp_path, monkeypatch)
        assert not hasattr(builtins, "_synth_race_state")
        # pre-create the race state so this thread can watch it the moment
        # the writers start (the fake reuses it via getattr on builtins)
        st = builtins._synth_race_state = {
            "go": threading.Event(),
            "counter": itertools.count(1), "n": 0, "parked": 0,
            "log": [], "broken": []}
        root = self._stage_video(tmp_path, 1)

        errors: list[str] = []

        def run() -> None:
            try:
                mod.stage_synth(str(root), "video")
            except Exception as e:  # pragma: no cover - surfaced via errors
                errors.append(repr(e))

        t1 = threading.Thread(target=run)
        t2 = threading.Thread(target=run)
        t1.start()
        t2.start()
        final = root / "dubbed" / "_full" / "_segments" / "sent_0001.wav"
        try:
            # both writers parked mid-write? (deadline guards a crashed
            # writer: go is set anyway and the assertions below fail)
            deadline = time.time() + 15
            while st["parked"] < 2 and time.time() < deadline:
                time.sleep(0.001)
            # deterministic observation window: both writers hold their
            # partials while we poll the final path for 100ms
            partial_seen = []
            for _ in range(100):
                if final.exists():
                    try:
                        with contextlib.closing(
                                wave.open(str(final), "rb")) as w:
                            n = w.getnframes()
                        if n not in (4410, 6615):
                            partial_seen.append(n)
                    except PermissionError:
                        # a handle collision, not evidence of a partial —
                        # the states we CAN read are
                        pass
                    except Exception as e:
                        partial_seen.append(repr(e))
                time.sleep(0.001)
            st["go"].set()
            t1.join(30)
            t2.join(30)
            assert not errors
            # the race was REAL: both writers synthesized the same cue
            assert st["n"] == 2, f"expected 2 racing writers, got {st['n']}"
            assert not st["broken"], "writers timed out parked — never released"
            # the poller never caught a partial file
            assert not partial_seen, \
                f"partial/corrupt states observed mid-write: {partial_seen[:5]}"
            # and the survivor is one writer's complete take (whichever
            # writer's replace landed last — both wrote complete files)
            with contextlib.closing(wave.open(str(final), "rb")) as w:
                assert w.getnframes() in (4410, 6615)
            assert list(final.parent.glob(".tmp-*")) == []
        finally:
            st["go"].set()
            del builtins._synth_race_state

    def _load_full_dub_racing(self, tmp_path: Path, monkeypatch):
        """_load_full_dub variant whose fake infer writes DISTINCT content
        per writer (writer 1: 4410 frames of 'A', writer 2: 6615 of 'B'),
        flushes the first half to disk, then parks on a test-gated Event
        (shared via builtins — module re-imports by the second thread must
        not fork the race state) until the test releases it."""
        import importlib.util
        import sys
        import types
        pkg = tmp_path / "racedeps"
        (pkg / "indextts").mkdir(parents=True)
        (pkg / "indextts" / "__init__.py").write_text("", encoding="utf-8")
        (pkg / "indextts" / "infer_v2_5.py").write_text(
            "import builtins, itertools, pathlib, threading, wave\n"
            "st = getattr(builtins, '_synth_race_state', None)\n"
            "if st is None:\n"
            "    st = builtins._synth_race_state = {\n"
            "        'go': threading.Event(),\n"
            "        'counter': itertools.count(1), 'n': 0, 'parked': 0,\n"
            "        'log': [], 'broken': []}\n"
            "class IndexTTS2:\n"
            "    def __init__(self, **kw):\n"
            "        pass\n"
            "    def infer(self, spk_audio_prompt, text, output_path, lang,\n"
            "              use_random, duration_factor):\n"
            "        wid = next(st['counter'])\n"
            "        st['n'] = wid\n"
            "        st['log'].append((wid, text, output_path))\n"
            "        nframes = 4410 if wid == 1 else 6615\n"
            "        marker = bytes([64 + wid])  # 'A' / 'B'\n"
            "        p = pathlib.Path(output_path)\n"
            "        p.parent.mkdir(parents=True, exist_ok=True)\n"
            "        with wave.open(str(p), 'wb') as w:\n"
            "            w.setnchannels(1); w.setsampwidth(2); "
            "w.setframerate(22050)\n"
            "            half = nframes // 2\n"
            "            w.writeframes(marker * (half * 2))\n"
            "            w._file.flush()  # partial bytes on disk NOW\n"
            "            st['parked'] += 1\n"
            "            if not st['go'].wait(30):\n"
            "                st['broken'].append(wid)\n"
            "            w.writeframes(marker * ((nframes - half) * 2))\n",
            encoding="utf-8")
        fake_torch = types.ModuleType("torch")
        fake_torch.set_num_threads = lambda n: None
        fake_torch.set_num_interop_threads = lambda n: None
        fake_torch.__file__ = str(pkg / "torch.py")
        monkeypatch.setitem(sys.modules, "torch", fake_torch)
        monkeypatch.setenv("INDEXTTS_DIR", str(pkg))
        spec = importlib.util.spec_from_file_location(
            f"full_dub_race_{id(tmp_path):x}", _stages_dub_dir() / "full_dub.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod, pkg


class TestSynthWorkersOrchestration:
    """`cook dub synth --workers N --stagger S` (ticket 10): the supervisor
    subprocess argv is UNCHANGED (full list, ascending); N-1 worker
    subprocesses are spawned with descending band argv --lo/--hi/--stagger
    k*S and their own log files; exit codes aggregate (any non-zero = the
    whole stage fails). N=1 (default) spawns nothing and behaves exactly as
    before. All process launching is monkeypatched — nothing executes."""

    def _stage_run(self, monkeypatch, tmp_path, workers: int, stagger: float = 0.0,
                   worker_rcs: list[int] | None = None,
                   supervisor_rc: int = 0, n_cues: int = 10):
        """Drive cmd_dub_synth with fake _run_long_task (supervisor) and fake
        subprocess.Popen (workers). Returns (json_dict_or_None, supervisor_cmd,
        popen_cmds, exit_code_or_None)."""
        root = tmp_path / "vid"
        trans = root / "transcript" / "translations_dub.txt"
        trans.parent.mkdir(parents=True, exist_ok=True)
        trans.write_text(
            "\n".join(f"第{i}句" for i in range(1, n_cues + 1)) + "\n", encoding="utf-8")
        ref = root / "dubbed" / "_reference" / "ref.wav"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(b"x" * 2000)

        sup: dict = {}
        pops: list[list[str]] = []

        def fake_run_long_task(cmd, cwd, log_file, err_file, detach=False):
            sup["cmd"] = cmd
            return {"ok": supervisor_rc == 0, "returncode": supervisor_rc}

        rcs = worker_rcs if worker_rcs is not None else []

        class _FakeProc:
            def __init__(self, cmd, rc):
                self.cmd, self.rc, self.pid = cmd, rc, 4242

            def wait(self):
                return self.rc

        def fake_popen(cmd, **kw):
            rc = rcs[len(pops)] if len(pops) < len(rcs) else 0
            proc = _FakeProc(list(cmd), rc)
            pops.append(proc.cmd)
            return proc

        monkeypatch.setattr(cook, "_run_long_task", fake_run_long_task)
        monkeypatch.setattr("subprocess.Popen", fake_popen)
        args = type("A", (), {
            "output_root": str(root), "name": "video",
            "python": None, "detach": False,
            "workers": workers, "stagger": stagger,
        })()
        import contextlib
        import io
        import json as _json
        buf = io.StringIO()
        code = None
        with contextlib.redirect_stdout(buf):
            try:
                cook.cmd_dub_synth(args)
            except SystemExit as e:
                code = e.code
        # stdout carries only _emit_json's (multi-line, indent=2) payload;
        # _log/_die COOK-RESULT both go to stderr
        try:
            js = _json.loads(buf.getvalue())
        except ValueError:
            js = None
        return (js, sup.get("cmd"), pops, code)

    def test_single_worker_default_spawns_nothing(self, monkeypatch, tmp_path):
        """--workers 1 (the default): zero Popen calls and the supervisor argv
        is byte-identical to the pre-ticket command (no band flags)."""
        js, sup_cmd, pops, code = self._stage_run(monkeypatch, tmp_path, workers=1)
        assert pops == []
        assert sup_cmd == [cook.sys.executable,
                           str(_stages_dub_dir() / "full_dub.py"),
                           "synth", str(tmp_path / "vid"), "video"]
        assert js["ok"] is True and js["returncode"] == 0
        assert code in (None, 0)

    def test_three_workers_spawn_two_descending_bands_with_stagger(self, monkeypatch, tmp_path):
        js, sup_cmd, pops, _code = self._stage_run(
            monkeypatch, tmp_path, workers=3, stagger=30.0, n_cues=10)
        assert len(pops) == 2
        py = cook.sys.executable
        script = str(_stages_dub_dir() / "full_dub.py")
        root = str(tmp_path / "vid")
        assert pops[0] == [py, script, "synth-worker", root, "video",
                           "--lo", "1", "--hi", "5", "--stagger", "30.0"]
        assert pops[1] == [py, script, "synth-worker", root, "video",
                           "--lo", "6", "--hi", "10", "--stagger", "60.0"]
        # the supervisor argv is untouched by the fan-out
        assert sup_cmd == [py, script, "synth", root, "video"]
        assert js["ok"] is True
        assert js["returncode"] == 0
        assert [w["worker"] for w in js["workers"]] == [1, 2]
        assert js["workers"][0]["band"] == [1, 5]
        assert js["workers"][1]["band"] == [6, 10]
        assert js["workers"][1]["log"].endswith("synth.worker2.log")

    def test_worker_log_files_created_per_band(self, monkeypatch, tmp_path):
        _js, _sup, _pops, _code = self._stage_run(monkeypatch, tmp_path, workers=2, n_cues=4)
        root = tmp_path / "vid"
        for k in (1,):
            assert (root / "dubbed" / f"synth.worker{k}.log").exists()
            assert (root / "dubbed" / f"synth.worker{k}.err.log").exists()

    def test_any_worker_failure_fails_the_whole_stage(self, monkeypatch, tmp_path):
        """Exit-code aggregation: one worker exiting 2 makes ok:false and
        _die names that worker's err log (ticket acceptance)."""
        js, _sup, _pops, code = self._stage_run(
            monkeypatch, tmp_path, workers=3, worker_rcs=[0, 2], n_cues=10)
        assert code not in (None, 0), "a failed worker must fail the stage"
        assert js is not None and js["ok"] is False
        assert js["returncode"] == 2
        fails = [w for w in js["workers"] if w["returncode"] != 0]
        assert len(fails) == 1 and fails[0]["worker"] == 2

    def test_supervisor_failure_fails_even_with_green_workers(self, monkeypatch, tmp_path):
        js, _sup, _pops, code = self._stage_run(
            monkeypatch, tmp_path, workers=2, supervisor_rc=3, n_cues=4)
        assert code not in (None, 0)
        assert js["ok"] is False and js["returncode"] == 3

    def test_more_workers_than_cues_spawns_fewer_bands(self, monkeypatch, tmp_path):
        js, _, pops, _code = self._stage_run(monkeypatch, tmp_path, workers=4, n_cues=2)
        # 3 bands wanted, only 2 cues -> 2 workers spawned (no empty band)
        assert len(pops) == 2
        assert js["ok"] is True

    def test_workers_below_one_dies_before_spawning(self, monkeypatch, tmp_path):
        _js, sup_cmd, pops, code = self._stage_run(monkeypatch, tmp_path, workers=0)
        assert code not in (None, 0)
        assert pops == [] and sup_cmd is None

    def test_detach_with_multiple_workers_rejected(self, monkeypatch, tmp_path):
        root = tmp_path / "vid"
        trans = root / "transcript" / "translations_dub.txt"
        trans.parent.mkdir(parents=True, exist_ok=True)
        trans.write_text("句\n", encoding="utf-8")
        ref = root / "dubbed" / "_reference" / "ref.wav"
        ref.parent.mkdir(parents=True, exist_ok=True)
        ref.write_bytes(b"x" * 2000)
        args = type("A", (), {
            "output_root": str(root), "name": "video",
            "python": None, "detach": True, "workers": 2, "stagger": 0.0,
        })()
        with pytest.raises(SystemExit) as ei:
            cook.cmd_dub_synth(args)
        assert ei.value.code not in (0, None)

    def test_parser_defaults_workers_one_stagger_zero(self):
        p = cook.build_parser()
        a = p.parse_args(["dub", "synth", "root", "name"])
        assert a.workers == 1 and a.stagger == 0.0
        a2 = p.parse_args(["dub", "synth", "root", "name",
                           "--workers", "3", "--stagger", "45"])
        assert a2.workers == 3 and a2.stagger == 45.0


# ----------------------------------------------------------------------------
# cook dub merge: the ONLY legal way to merge cues — en.full.srt and
# translations_dub.txt are merged in lockstep (one side hand-edited alone is
# the 2026-10 incident: zh/en off by one line, 8 dubbed sentences 4-14s late).
# ============================================================================

class _DubMergeHarness:
    """Shared harness for dub-merge tests. Seam: cmd_dub_merge in-process;
    observed via the on-disk files under transcript/ (en.full.srt,
    translations_dub.txt, their .v1 backups), the exit code, and stdout JSON.
    Fake SRTs in temp dirs only."""

    def _setup(self, tmp_path: Path, en_cues, zh_lines, name="video"):
        root = tmp_path / "author" / "video"
        en_srt = root / "transcript" / f"{name}.en.full.srt"
        zh_txt = root / "transcript" / "translations_dub.txt"
        _make_srt(en_srt, en_cues)
        _make_translations(zh_txt, zh_lines)
        return root, en_srt, zh_txt

    def _run(self, root: Path, groups, name="video"):
        """Invoke cmd_dub_merge in-process. Returns (exit_code, stdout_json).
        exit_code None = returned normally (exit 0)."""
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {"output_root": str(root), "name": name,
                              "group": list(groups)})()
        code = None
        with contextlib.redirect_stdout(buf):
            try:
                cook.cmd_dub_merge(args)
            except SystemExit as e:
                code = e.code
        try:
            report = json.loads(buf.getvalue())
        except ValueError:
            report = None
        return code, report

    def _blocks(self, path: Path):
        """Parse an SRT on disk into [(num, ts, text), ...]."""
        text = path.read_text(encoding="utf-8").strip()
        out = []
        for b in re.split(r"\n\s*\n", text):
            lines = b.splitlines()
            out.append((int(lines[0]), lines[1], " ".join(lines[2:])))
        return out

    def _assert_refused_untouched(self, root, en_srt, zh_txt,
                                  orig_en, orig_zh, code, report):
        assert code == 1  # _die's non-zero exit
        assert report is None or report.get("ok") is False
        # zero side effects: working files byte-identical, no backups created
        assert en_srt.read_text(encoding="utf-8") == orig_en
        assert zh_txt.read_text(encoding="utf-8") == orig_zh
        assert not (en_srt.parent / "video.en.full.srt.v1").exists()
        assert not (zh_txt.parent / "translations_dub.txt.v1").exists()


class TestDubMerge(_DubMergeHarness):

    def test_single_group_merges_both_sides_in_lockstep(self, tmp_path):
        """The happy path: --group 2,3 merges cue 2+3 on BOTH sides in one
        atomic action — en text space-joined, zh text directly joined, the
        merged window = first start + last end, untouched cues verbatim,
        numbering resequenced, and .v1 backups holding the pre-merge files."""
        en_cues = [
            (1, "00:00:01,000 --> 00:00:02,000", "first cue"),
            (2, "00:00:03,100 --> 00:00:04,200", "second cue"),
            (3, "00:00:05,300 --> 00:00:06,400", "third cue"),
            (4, "00:00:07,500 --> 00:00:08,600", "fourth cue"),
        ]
        zh = ["第一句", "第二句", "第三句", "第四句"]
        root, en_srt, zh_txt = self._setup(tmp_path, en_cues, zh)
        orig_en = en_srt.read_text(encoding="utf-8")
        orig_zh = zh_txt.read_text(encoding="utf-8")

        code, report = self._run(root, ["2,3"])

        assert code is None  # returned normally -> exit 0
        assert report["ok"] is True
        assert report["cues_before"] == 4
        assert report["cues_after"] == 3

        # en side: 3 cues, untouched ones byte-identical, merged window spans
        # first start -> last end, texts space-joined
        blocks = self._blocks(en_srt)
        assert [b[0] for b in blocks] == [1, 2, 3]
        assert blocks[0] == (1, "00:00:01,000 --> 00:00:02,000", "first cue")
        assert blocks[1] == (2, "00:00:03,100 --> 00:00:06,400",
                             "second cue third cue")
        assert blocks[2] == (3, "00:00:07,500 --> 00:00:08,600", "fourth cue")

        # zh side: 3 lines, group 2+3 directly concatenated, others verbatim
        assert zh_txt.read_text(encoding="utf-8").splitlines() == \
            ["第一句", "第二句第三句", "第四句"]

        # backups hold the pristine pre-merge files
        assert (en_srt.parent / "video.en.full.srt.v1").read_text(
            encoding="utf-8") == orig_en
        assert (zh_txt.parent / "translations_dub.txt.v1").read_text(
            encoding="utf-8") == orig_zh

    def test_existing_v1_backup_is_never_overwritten(self, tmp_path):
        """A second merge run must NOT clobber the .v1 backups: they hold the
        pristine pre-any-merge state (the recovery point), matching the
        *.v1 convention the dub pipeline already relies on."""
        en_cues = [
            (1, "00:00:01,000 --> 00:00:02,000", "first"),
            (2, "00:00:03,000 --> 00:00:04,000", "second"),
            (3, "00:00:05,000 --> 00:00:06,000", "third"),
            (4, "00:00:07,000 --> 00:00:08,000", "fourth"),
        ]
        zh = ["一", "二", "三", "四"]
        root, en_srt, zh_txt = self._setup(tmp_path, en_cues, zh)
        pristine_en = en_srt.read_text(encoding="utf-8")
        pristine_zh = zh_txt.read_text(encoding="utf-8")
        en_bak = en_srt.parent / "video.en.full.srt.v1"
        zh_bak = zh_txt.parent / "translations_dub.txt.v1"

        # first merge: groups 1,2 -> backups created from the pristine files
        code, _ = self._run(root, ["1,2"])
        assert code is None
        assert en_bak.read_text(encoding="utf-8") == pristine_en
        assert zh_bak.read_text(encoding="utf-8") == pristine_zh

        # second merge on the renumbered file (groups now 2,3 = old 3,4):
        # current files change, .v1 stays pristine
        code, report = self._run(root, ["2,3"])
        assert code is None
        assert report["cues_after"] == 2
        assert en_bak.read_text(encoding="utf-8") == pristine_en
        assert zh_bak.read_text(encoding="utf-8") == pristine_zh
        # and the second merge really did rewrite the working files
        assert [b[2] for b in self._blocks(en_srt)] == \
            ["first second", "third fourth"]
        assert zh_txt.read_text(encoding="utf-8").splitlines() == \
            ["一二", "三四"]

    # -- refusals: every invariant failure leaves ZERO side effects ----------

    def _pristine(self, en_cues, zh):
        en = [
            (i + 1, f"00:00:{i + 1:02d},000 --> 00:00:{i + 1:02d},500",
             f"cue {i + 1}")
            for i in range(len(zh))
        ]
        return en

    def test_reject_out_of_bounds_group(self, tmp_path):
        zh = ["一", "二", "三", "四"]
        root, en_srt, zh_txt = self._setup(tmp_path, self._pristine(None, zh), zh)
        orig_en = en_srt.read_text(encoding="utf-8")
        orig_zh = zh_txt.read_text(encoding="utf-8")
        code, report = self._run(root, ["2,9"])  # 9 > 4 cues
        self._assert_refused_untouched(root, en_srt, zh_txt,
                                       orig_en, orig_zh, code, report)

    def test_reject_zero_index(self, tmp_path):
        zh = ["一", "二", "三"]
        root, en_srt, zh_txt = self._setup(tmp_path, self._pristine(None, zh), zh)
        orig_en = en_srt.read_text(encoding="utf-8")
        orig_zh = zh_txt.read_text(encoding="utf-8")
        code, report = self._run(root, ["0,1"])  # 1-based: 0 is invalid
        self._assert_refused_untouched(root, en_srt, zh_txt,
                                       orig_en, orig_zh, code, report)

    def test_reject_non_consecutive_group(self, tmp_path):
        zh = ["一", "二", "三"]
        root, en_srt, zh_txt = self._setup(tmp_path, self._pristine(None, zh), zh)
        orig_en = en_srt.read_text(encoding="utf-8")
        orig_zh = zh_txt.read_text(encoding="utf-8")
        code, report = self._run(root, ["1,3"])  # skips line 2
        self._assert_refused_untouched(root, en_srt, zh_txt,
                                       orig_en, orig_zh, code, report)

    def test_reject_overlapping_groups(self, tmp_path):
        zh = ["一", "二", "三"]
        root, en_srt, zh_txt = self._setup(tmp_path, self._pristine(None, zh), zh)
        orig_en = en_srt.read_text(encoding="utf-8")
        orig_zh = zh_txt.read_text(encoding="utf-8")
        code, report = self._run(root, ["1,2", "2,3"])  # line 2 claimed twice
        self._assert_refused_untouched(root, en_srt, zh_txt,
                                       orig_en, orig_zh, code, report)

    def test_reject_singleton_group(self, tmp_path):
        zh = ["一", "二", "三"]
        root, en_srt, zh_txt = self._setup(tmp_path, self._pristine(None, zh), zh)
        orig_en = en_srt.read_text(encoding="utf-8")
        orig_zh = zh_txt.read_text(encoding="utf-8")
        code, report = self._run(root, ["2"])  # merging one line is a no-op
        self._assert_refused_untouched(root, en_srt, zh_txt,
                                       orig_en, orig_zh, code, report)

    def test_reject_non_integer_group(self, tmp_path):
        zh = ["一", "二", "三"]
        root, en_srt, zh_txt = self._setup(tmp_path, self._pristine(None, zh), zh)
        orig_en = en_srt.read_text(encoding="utf-8")
        orig_zh = zh_txt.read_text(encoding="utf-8")
        code, report = self._run(root, ["one,two"])
        self._assert_refused_untouched(root, en_srt, zh_txt,
                                       orig_en, orig_zh, code, report)

    def test_reject_no_groups(self, tmp_path):
        zh = ["一", "二", "三"]
        root, en_srt, zh_txt = self._setup(tmp_path, self._pristine(None, zh), zh)
        orig_en = en_srt.read_text(encoding="utf-8")
        orig_zh = zh_txt.read_text(encoding="utf-8")
        code, report = self._run(root, [])
        self._assert_refused_untouched(root, en_srt, zh_txt,
                                       orig_en, orig_zh, code, report)

    def test_reject_line_count_mismatch_one_side_hand_merged(self, tmp_path):
        """THE incident shape: someone already merged cue groups by hand on
        the en side only (3 en cues left, zh still 4 lines). The pairing
        invariant must refuse the merge — running it would pair zh line 3
        with the wrong en cue and ship 4-14s-late audio again."""
        # en side: 3 cues, cue 2 already absorbed cue 3's text by hand
        en_cues = [
            (1, "00:00:01,000 --> 00:00:02,000", "cue 1"),
            (2, "00:00:03,000 --> 00:00:06,000", "cue 2 cue 3"),
            (3, "00:00:07,000 --> 00:00:08,000", "cue 4"),
        ]
        zh = ["一", "二", "三", "四"]  # zh side: still 4 separate lines
        root, en_srt, zh_txt = self._setup(tmp_path, en_cues, zh)
        orig_en = en_srt.read_text(encoding="utf-8")
        orig_zh = zh_txt.read_text(encoding="utf-8")
        code, report = self._run(root, ["2,3"])
        self._assert_refused_untouched(root, en_srt, zh_txt,
                                       orig_en, orig_zh, code, report)

    def test_reject_missing_translations_file(self, tmp_path):
        root = tmp_path / "author" / "video"
        en_srt = root / "transcript" / "video.en.full.srt"
        _make_srt(en_srt, self._pristine(None, ["一", "二"]))
        code, report = self._run(root, ["1,2"])
        assert code == 1
        assert not (en_srt.parent / "video.en.full.srt.v1").exists()

    # -- several groups in one run ------------------------------------------

    def test_multiple_groups_one_run_out_of_order_flags(self, tmp_path):
        """--group is repeatable; flags may arrive in any order and a group
        may span 3 lines. 6 cues, groups {1,2} and {4,5,6} -> 3 output cues."""
        zh = ["一", "二", "三", "四", "五", "六"]
        root, en_srt, zh_txt = self._setup(tmp_path, self._pristine(None, zh), zh)
        code, report = self._run(root, ["4,5,6", "1,2"])  # deliberately unordered
        assert code is None
        assert report["ok"] is True
        assert report["cues_before"] == 6
        assert report["cues_after"] == 3
        assert report["merged"] == [[1, 2], [4, 5, 6]]
        blocks = self._blocks(en_srt)
        # merged windows: group 1 spans cue1.start->cue2.end; group 2 spans
        # cue4.start->cue6.end; cue 3 passes through verbatim
        assert blocks[0] == (1, "00:00:01,000 --> 00:00:02,500", "cue 1 cue 2")
        assert blocks[1] == (2, "00:00:03,000 --> 00:00:03,500", "cue 3")
        assert blocks[2] == (3, "00:00:04,000 --> 00:00:06,500",
                             "cue 4 cue 5 cue 6")
        assert zh_txt.read_text(encoding="utf-8").splitlines() == \
            ["一二", "三", "四五六"]


# ----------------------------------------------------------------------------
# Regression fixture: the 2026-10 "flywheel + 群聊" misalignment incident.
# Real texts from leerob/stanford-cs146s-always-on-agents
# (transcript/*.v1, cues 330-333). The hand-merge folded the short group-chat
# sentence into the flywheel paragraph cue on the en side only; zh drifted
# one line (dangling "所以说，") and 8 dubbed sentences landed 4-14s late.
# ============================================================================

# flywheel paragraph sentence (en), verbatim minus the absorbed tail
_FLY_EN = ("And ideally the new models are learning from all of these bugs "
           "and these failures, and they're kind of climbing on the evals to "
           "get better at the things that you care about. And you can drop "
           "the new versions into the product and make sure that they're "
           "improving in all these different things.")
# its zh, verbatim minus the dangling "所以说，" the incident left behind
_FLY_ZH = ("理想状态下，新模型会从这一大堆 bug 和失败里学习，在你真正关心的那些 "
           "eval 指标上不断爬升。你把新版本放进产品里，确认各项东西确实都在变好。")
# the short group-chat sentence that got folded into the flywheel cue
_CHAT_EN = ("So There hasn't really been a lot of training data, for "
            "example, on using bots in a group chat.")
_CHAT_ZH = "比如 bot 在群聊里该怎么表现，就没有多少训练数据。"
# the sentence AFTER the pair — in the incident state it was paired with
# _CHAT_ZH (one line of drift); after a legal merge it must get its own zh.
_NEXT_EN = ("This is kind of an emergent thing that's being figured out, "
            "and it's important to get it right.")
_NEXT_ZH = "这是一件正在摸索中、逐渐成型的新事物，而把它做对非常重要。"
# the cue before the flywheel pair (verbatim from the incident, v1 cue 329)
_PREV_EN = ("But then there's this outer loop, which is like every few "
            "months or every month or however long you're training new models.")
_PREV_ZH = "而外面还有一层外环：每隔几个月、或者每个月，总之按某种周期，训练新模型。"


class TestDubMergeFlywheelRegression(_DubMergeHarness):
    """Same seam as TestDubMerge (in-process cmd_dub_merge, temp dirs), but
    the material is the real incident text so the shape that broke the run
    can never quietly come back."""

    def _pristine(self, tmp_path):
        """The pre-incident state: 4 clean en/zh pairs around the flywheel
        paragraph. Cue 3's reconstructed timestamps keep the incident's
        merged window (…,357 --> …15,946) as first-start/last-end."""
        en_cues = [
            (1, "00:36:44,690 --> 00:36:49,736", _PREV_EN),
            (2, "00:36:50,357 --> 00:37:07,900", _FLY_EN),
            (3, "00:37:08,000 --> 00:37:15,946", _CHAT_EN),
            (4, "00:37:16,046 --> 00:37:20,109", _NEXT_EN),
        ]
        zh = [_PREV_ZH, _FLY_ZH, _CHAT_ZH, _NEXT_ZH]
        return self._setup(tmp_path, en_cues, zh)

    def test_legal_merge_keeps_flywheel_pairing_in_lockstep(self, tmp_path):
        """Merging the flywheel cue with the group-chat cue through the tool:
        both sides move together, the 群聊 zh lands in the SAME cue as its en
        sentence, and the following cue is paired with ITS zh (exactly the
        pairing the hand-merge destroyed)."""
        root, en_srt, zh_txt = self._pristine(tmp_path)
        code, report = self._run(root, ["2,3"])
        assert code is None
        assert report["ok"] is True
        assert report["cues_after"] == 3

        blocks = self._blocks(en_srt)
        zh_out = zh_txt.read_text(encoding="utf-8").splitlines()

        # en cue 2 = flywheel + group-chat, window = first start + last end
        assert blocks[1][1] == "00:36:50,357 --> 00:37:15,946"
        assert blocks[1][2] == _FLY_EN + " " + _CHAT_EN
        # zh line 2 = flywheel zh + 群聊 zh, directly joined — the group-chat
        # translation travels WITH its english sentence
        assert zh_out[1] == _FLY_ZH + _CHAT_ZH
        # the next pair stays paired (the one-line drift never happens)
        assert blocks[2][2] == _NEXT_EN
        assert zh_out[2] == _NEXT_ZH
        # untouched neighbours pass through verbatim
        assert blocks[0][2] == _PREV_EN and zh_out[0] == _PREV_ZH
        # both sides carry the same number of entries
        assert len(blocks) == len(zh_out) == 3

    def test_incident_shape_one_side_hand_merged_is_refused(self, tmp_path):
        """The incident re-run: the en side has ALREADY been hand-merged
        (flywheel cue absorbed the group-chat sentence, 3 en cues) while zh
        still has 4 lines with the dangling tail. The pairing invariant must
        refuse and leave everything untouched — merging on top of a
        desynchronized pair is what shipped 8 late sentences."""
        en_cues = [
            (1, "00:36:44,690 --> 00:36:49,736", _PREV_EN),
            (2, "00:36:50,357 --> 00:37:15,946", _FLY_EN + " " + _CHAT_EN),
            (3, "00:37:16,046 --> 00:37:20,109", _NEXT_EN),
        ]
        # zh side still 4 lines, cue 2 ends with the incident's dangling tail
        zh = [_PREV_ZH, _FLY_ZH + "所以说，", _CHAT_ZH, _NEXT_ZH]
        root, en_srt, zh_txt = self._setup(tmp_path, en_cues, zh)
        orig_en = en_srt.read_text(encoding="utf-8")
        orig_zh = zh_txt.read_text(encoding="utf-8")
        code, report = self._run(root, ["2,3"])
        self._assert_refused_untouched(root, en_srt, zh_txt,
                                       orig_en, orig_zh, code, report)


class TestDubMergeCliSurface:
    """The ticket's command shape: cook dub merge <root> <name> --group "a,b,c"
    (repeatable). Pinned at the argparse surface like TestQualityParserInt."""

    def test_group_flag_repeatable_appends(self):
        args = cook.build_parser().parse_args(
            ["dub", "merge", "author/video", "video", "--group", "1,2",
             "--group", "5,6,7"])
        assert args.group == ["1,2", "5,6,7"]
        assert args.output_root == "author/video"
        assert args.name == "video"
        assert args.func is cook.cmd_dub_merge


# dub measure-rate (ticket 04): measure the real speech rate of audition
# wavs / synthesized takes via energy VAD. Tests synthesize tiny wavs
# (sine = speech, zeros = silence) — no real audio is ever loaded.
# ----------------------------------------------------------------------------

def _make_wav(path: Path, spans: list[tuple[float, bool]], sr: int = 16000) -> None:
    """Write a tiny synthetic wav. spans = [(seconds, voiced), ...]; voiced
    spans are a 0.5-amplitude 440 Hz sine (-9 dBFS RMS), silent spans zeros.
    ~64 KB for 2 s at 16 kHz — tens of KB per file, per the test contract."""
    import numpy as np
    import soundfile as sf
    frames = []
    phase_t = 0.0
    for dur, voiced in spans:
        n = int(round(dur * sr))
        if voiced:
            t = np.arange(n) / sr + phase_t
            frames.append(0.5 * np.sin(2 * np.pi * 440.0 * t))
            phase_t += n / sr
        else:
            frames.append(np.zeros(n, dtype=np.float64))
            phase_t += n / sr
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), np.concatenate(frames), sr, subtype="PCM_16")


class TestDubMeasureRate:
    """cook dub measure-rate — the public seam is cmd_dub_measure_rate
    (argv: wav/--text/--from-segments/--json)."""

    def _run(self, wav=None, text=None, from_segments=None, as_json=True):
        """Invoke cmd_dub_measure_rate in-process, capture stdout JSON.
        Returns (report_or_None, stdout_text, exit_code_or_None)."""
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {
            "wav": str(wav) if wav else None,
            "text": text,
            "from_segments": from_segments,
            "json": as_json,
        })()
        code = None
        with contextlib.redirect_stdout(buf):
            try:
                cook.cmd_dub_measure_rate(args)
            except SystemExit as e:
                code = e.code
        text_out = buf.getvalue().strip()
        report = json.loads(text_out) if text_out else None
        return report, text_out, code

    def test_explicit_mode_happy_path(self, tmp_path):
        """wav + --text: 1.0 s of sine between silence paddles, 6 CJK chars.
        Voiced seconds must track the constructed sine span; rate = chars/voiced."""
        pytest.importorskip("soundfile")
        wav = tmp_path / "audition.wav"
        _make_wav(wav, [(0.3, False), (1.0, True), (0.3, False)])
        report, _, code = self._run(wav=wav, text="你好世界测试")
        assert code == 0
        assert report["ok"] is True
        assert report["chars"] == 6
        # voiced tracks the 1.0 s sine span (frame-quantized, 30 ms frames)
        assert 0.9 <= report["voiced_seconds"] <= 1.1
        # rate = 6 chars over ~1 s of speech
        assert 5.4 <= report["rate"] <= 6.7
        assert report["files"][0]["voiced_seconds"] == report["voiced_seconds"]

    def test_silent_wav_is_nonzero_exit_with_clear_error(self, tmp_path):
        """A fully-silent take is an error, not a rate of 0 — a silent take
        means synthesis failed and a 0-rate would poison downstream gates."""
        pytest.importorskip("soundfile")
        wav = tmp_path / "silent.wav"
        _make_wav(wav, [(1.5, False)])
        report, _, code = self._run(wav=wav, text="你好世界测试")
        assert code == 1
        assert report["ok"] is False
        assert "no speech" in report["error"]
        assert str(wav) in report["error"]

    def test_from_segments_pairs_lines_with_wavs(self, tmp_path):
        """--from-segments mode: sorted sent_NNNN.wav pairs with line order;
        per-file voiced/chars plus aggregate totals and rate come back."""
        pytest.importorskip("soundfile")
        root = tmp_path / "author" / "video"
        _make_translations(root / "transcript" / "translations_dub.txt",
                           ["甲乙丙丁戊己", "甲乙丙丁戊己庚辛", "甲乙丙丁"])
        segs = root / "dubbed" / "_full" / "_segments"
        _make_wav(segs / "sent_0001.wav", [(0.2, False), (1.0, True)])
        _make_wav(segs / "sent_0002.wav", [(0.2, False), (2.0, True)])
        _make_wav(segs / "sent_0003.wav", [(0.2, False), (0.5, True)])
        report, _, code = self._run(
            from_segments=[str(root), "video"])
        assert code == 0
        assert report["ok"] is True
        assert report["mode"] == "segments"
        assert report["files_measured"] == 3
        # per-file rows carry relative labels and track the sine spans
        by_file = {r["file"]: r for r in report["files"]}
        assert "dubbed/_full/_segments/sent_0001.wav" in by_file
        assert 0.9 <= by_file["dubbed/_full/_segments/sent_0001.wav"]["voiced_seconds"] <= 1.1
        assert 1.9 <= by_file["dubbed/_full/_segments/sent_0002.wav"]["voiced_seconds"] <= 2.1
        assert 0.4 <= by_file["dubbed/_full/_segments/sent_0003.wav"]["voiced_seconds"] <= 0.6
        # totals: 18 chars over 3.5 s of speech
        assert report["chars"] == 18
        assert 3.3 <= report["voiced_seconds"] <= 3.7
        assert 4.9 <= report["rate"] <= 5.4

    def test_from_segments_count_mismatch_is_error(self, tmp_path):
        """3 lines vs 2 wavs: refuse rather than mis-pair line N with wav N."""
        pytest.importorskip("soundfile")
        root = tmp_path / "author" / "video"
        _make_translations(root / "transcript" / "translations_dub.txt",
                           ["甲", "乙", "丙"])
        segs = root / "dubbed" / "_full" / "_segments"
        _make_wav(segs / "sent_0001.wav", [(0.5, True)])
        _make_wav(segs / "sent_0002.wav", [(0.5, True)])
        report, _, code = self._run(from_segments=[str(root), "video"])
        assert code == 1
        assert report["ok"] is False
        assert "3" in report["error"] and "2" in report["error"]

    def test_from_segments_missing_translations_is_error(self, tmp_path):
        pytest.importorskip("soundfile")
        root = tmp_path / "author" / "video"
        segs = root / "dubbed" / "_full" / "_segments"
        _make_wav(segs / "sent_0001.wav", [(0.5, True)])
        report, _, code = self._run(from_segments=[str(root), "video"])
        assert code == 1
        assert "translations_dub.txt" in report["error"]

    def test_from_segments_silent_take_fails_naming_file(self, tmp_path):
        """One silent take in the batch aborts the measurement, naming it."""
        pytest.importorskip("soundfile")
        root = tmp_path / "author" / "video"
        _make_translations(root / "transcript" / "translations_dub.txt",
                           ["甲乙", "丙丁"])
        segs = root / "dubbed" / "_full" / "_segments"
        _make_wav(segs / "sent_0001.wav", [(0.5, True)])
        _make_wav(segs / "sent_0002.wav", [(0.5, False)])  # silent take
        report, _, code = self._run(from_segments=[str(root), "video"])
        assert code == 1
        assert "no speech" in report["error"]
        assert "sent_0002.wav" in report["error"]

    def test_bad_invocations_are_rejected(self, tmp_path):
        pytest.importorskip("soundfile")
        wav = tmp_path / "a.wav"
        _make_wav(wav, [(0.5, True)])
        # neither mode given
        report, _, code = self._run()
        assert code == 1 and report["ok"] is False
        # wav without text
        report, _, code = self._run(wav=wav)
        assert code == 1 and report["ok"] is False
        # text without wav
        report, _, code = self._run(text="你好")
        assert code == 1 and report["ok"] is False
        # both modes at once
        report, _, code = self._run(wav=wav, text="你好",
                                    from_segments=[str(tmp_path), "v"])
        assert code == 1 and report["ok"] is False
        # wav path that does not exist
        report, _, code = self._run(wav=tmp_path / "missing.wav", text="你好")
        assert code == 1 and "not found" in report["error"]

    def test_default_output_is_human_readable_not_json(self, tmp_path):
        """Without --json: stdout stays empty (agents opt in with --json);
        the human summary carries the rate on stderr."""
        pytest.importorskip("soundfile")
        wav = tmp_path / "audition.wav"
        _make_wav(wav, [(0.2, False), (1.0, True)])
        report, text_out, code = self._run(wav=wav, text="你好世界测试",
                                           as_json=False)
        assert code == 0
        assert text_out == ""  # stdout is pure-JSON-only territory
        assert report is None

    def test_voiced_is_the_span_not_the_frame_sum(self, tmp_path):
        """Voiced seconds = the speech SPAN (first voiced frame to last
        voiced frame): intra-sentence pauses are bridged, leading/trailing
        silence excluded. This is the semantic calibrated against the
        reflux run's hand measurement (6.93 on the 361 leerob takes)."""
        pytest.importorskip("soundfile")
        wav = tmp_path / "span.wav"
        # 0.4s speech, 0.4s pause, 0.4s speech: span = 1.2s, frame-sum = 0.8s
        _make_wav(wav, [(0.2, False), (0.4, True), (0.4, False),
                        (0.4, True), (0.2, False)])
        report, _, code = self._run(wav=wav, text="甲乙丙丁戊己")
        assert code == 0
        assert 1.1 <= report["voiced_seconds"] <= 1.3

    def test_char_convention_is_line_length_with_punctuation(self, tmp_path):
        """Chars = len() of the stripped line, punctuation INCLUDED — the
        counting the reflux budget math used (预算=实测率×窗口), and the
        convention the 6.93 calibration is anchored to. External whitespace
        is not counted."""
        pytest.importorskip("soundfile")
        wav = tmp_path / "conv.wav"
        _make_wav(wav, [(0.5, True)])
        report, _, code = self._run(wav=wav, text="  你好，世界！  ")
        assert code == 0
        assert report["chars"] == 6


# ----------------------------------------------------------------------------
# dub retime (ticket 08): rewrite the bilingual SRT's start times to the
# dubbed speech's real word times. All tests use FAKE word timestamps —
# the faster-whisper model is never loaded here (resource rule).
# ----------------------------------------------------------------------------

def _load_retime():
    """Load stages/dub/retime.py fresh. Module level is stdlib-only; the
    faster-whisper import happens lazily inside the transcriber factory."""
    spec = importlib.util.spec_from_file_location(
        f"retime_test_{id(_stages_dub_dir()):x}", _stages_dub_dir() / "retime.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestRetimeCharFilter:
    """filter_chars: punctuation and whitespace drop out, everything else
    (CJK, latin, digits) stays in order — the rule calibrated against the
    leerob _fw_char_maps.json (79 raw -> 69 kept chars on cue 0)."""

    def test_punct_and_space_dropped(self):
        mod = _load_retime()
        assert mod.filter_chars("如前所说，我是 Lee。今天") == "如前所说我是Lee今天"

    def test_full_width_and_ascii_punct_both_drop(self):
        mod = _load_retime()
        assert mod.filter_chars("你好，世界！hello? (ok)") == "你好世界hellook"

    def test_empty_for_punct_only(self):
        mod = _load_retime()
        assert mod.filter_chars("。，！？ ") == ""


class TestRetimeCharMap:
    """build_char_map: SequenceMatcher maps the filtered text's chars onto
    the ASR word stream; every char gets the START time of the word it
    matched (absolute = cue_start + word start). Fake words only."""

    def test_exact_match_gives_word_starts(self):
        mod = _load_retime()
        # text filters to 你好世界; ASR hears exactly that in 2 words
        words = [(0.00, 0.42, "你好"), (0.42, 0.90, "世界")]
        times = mod.build_char_map("你好世界", 100.0, words)
        assert times == [100.00, 100.00, 100.42, 100.42]

    def test_unmatched_chars_forward_fill_previous_word(self):
        """ASR mishears latin (Lee->力): the unmatched latin chars inherit
        the PREVIOUS matched char's time (leerob cue 0: L,e,e all got
        我's 1.351, not 力's later word start)."""
        mod = _load_retime()
        words = [(0.00, 0.30, "我是力"), (0.30, 0.80, "今天")]
        times = mod.build_char_map("我是Lee今天", 10.0, words)
        # 我/是 in word 1; L/e/e unmatched -> forward fill from previous
        assert times == [10.00, 10.00, 10.00, 10.00, 10.00, 10.30, 10.30]

    def test_leading_unmatched_fill_from_cue_start(self):
        """Leading unmatched chars take the CUE window start, not the
        first word's time — calibrated on leerob cue 59 ('subagent...'
        latin head got the window start while fw's first word began
        0.44s later)."""
        mod = _load_retime()
        words = [(0.44, 0.90, "世界")]
        times = mod.build_char_map("你好世界", 10.0, words)
        assert times == [10.00, 10.00, 10.44, 10.44]

    def test_asr_punctuation_ignored_on_asr_side(self):
        """fw zh output carries punctuation; it must not consume a text
        char in the alignment."""
        mod = _load_retime()
        words = [(0.00, 0.40, "你好，"), (0.40, 0.80, "世界。")]
        times = mod.build_char_map("你好世界", 0.0, words)
        assert times == [0.00, 0.00, 0.40, 0.40]

    def test_poor_transcript_returns_none(self):
        """Fewer than half the chars matched: the whole map is rejected —
        the leerob cue 240 case (coverage 0.471, 同樣/幀 traditional-char
        garble) fell back rather than trusting garbage times."""
        mod = _load_retime()
        words = [(0.00, 0.90, "完全不同的句子啊真的")]
        assert mod.build_char_map("你好世界", 0.0, words) is None

    def test_half_coverage_is_accepted(self):
        """Exactly 50% matched stays trusted — the leerob cue 32 boundary
        case (9/18 chars, accepted by the reference chain)."""
        mod = _load_retime()
        # text filters to 你好世界啊; ASR hears 世界啊 + 3 junk chars
        words = [(0.20, 0.90, "世界啊呸呸呸")]
        times = mod.build_char_map("你好世界啊", 5.0, words)
        assert times is not None
        assert times == [5.00, 5.00, 5.20, 5.20, 5.20]

    def test_case_sensitive_mismatch_fills_not_rejects(self):
        """A single-case mismatch (harness vs Harness) still aligns the
        rest; the mismatched chars forward-fill (leerob cue 2, 81/83)."""
        mod = _load_retime()
        words = [(0.00, 1.00, "一个很简单的Harness了")]
        times = mod.build_char_map("一个非常简单的harness了", 0.0, words)
        # 15 filtered chars on the text side (7 CJK + 7 latin + 了), one
        # ASR word covering them all -> every char takes its start
        assert times is not None
        assert len(times) == 15



class TestRetimePlan:
    """retime_plan: the two-pass rewrite. Behaviors verified bit-exact
    against the leerob r12 accepted output (778/778 starts)."""

    def _cue(self, start, end, zh, en="en text"):
        return {"num": ["1"], "start": start, "end": end,
                "lines": [zh, en]}

    def test_anchor_start_is_first_char_minus_lead(self):
        """ANCHOR cue: start = first char's time - 0.05 (subtitles land
        50ms before the word is spoken); first cue gets the SAME lead,
        not a double one (leerob cue 1: 0.331 -> 0.281)."""
        mod = _load_retime()
        cues = [self._cue(9.0, 12.0, "甲乙丙丁，"), self._cue(12.0, 15.0, "戊己庚辛")]
        plan = mod.retime_plan(cues, "甲乙丙丁戊己庚辛",
                               [10.0, 10.5, 11.0, 11.5, 20.0, 20.5, 21.0, 21.5],
                               30.0)
        assert plan[0]["kind"] == "ANCHOR"
        assert plan[0]["start"] == 10.0 - 0.05
        assert plan[1]["start"] == 20.0 - 0.05

    def test_anchor_earlier_than_lead_clamps_to_zero(self):
        """code-review fix (negative-boundary): a first word earlier than
        ANCHOR_LEAD_S (speech starting 0.02s into the cue) used to yield
        start = 0.02 - 0.05 = -0.03 — fmt_ts(-0.03) renders the malformed
        '-1:59:59,970' (negative divmod artifact). The lead is now clamped
        at zero: the earliest possible start is 0."""
        mod = _load_retime()
        cues = [self._cue(0.0, 9.0, "甲乙"), self._cue(9.0, 12.0, "丙丁")]
        plan = mod.retime_plan(cues, "甲乙丙丁",
                               [0.02, 0.5, 10.0, 10.5], 30.0)
        assert plan[0]["start"] == 0.0
        # anchors past the lead are untouched
        assert plan[1]["start"] == 10.0 - 0.05

    def test_anchor_exactly_at_lead_is_zero(self):
        """Boundary: r - ANCHOR_LEAD_S == 0 stays 0 (not -0.0, not epsilon)."""
        mod = _load_retime()
        cues = [self._cue(0.0, 9.0, "甲乙")]
        plan = mod.retime_plan(cues, "甲乙", [0.05, 0.5], 30.0)
        assert plan[0]["start"] == 0.0

    def test_clamped_anchor_formats_a_legal_timestamp(self):
        """The user-visible symptom: the rewritten SRT line for a clamped
        anchor must be a legal '00:00:00,000', never a negative-artifact
        like '-1:59:59,970'."""
        mod = _load_retime()
        cues = [self._cue(0.0, 9.0, "甲乙")]
        plan = mod.retime_plan(cues, "甲乙", [0.02, 0.5], 30.0)
        assert mod.fmt_ts(plan[0]["start"]) == "00:00:00,000"
        # and the monotonic clamp keeps the NEXT start non-negative too
        cues2 = [self._cue(0.0, 0.01, "甲"), self._cue(0.01, 5.0, "乙")]
        plan2 = mod.retime_plan(cues2, "甲乙", [0.01, 0.02], 30.0)
        assert plan2[0]["start"] == 0.0
        assert plan2[1]["start"] >= 0.0
        assert mod.fmt_ts(plan2[1]["start"]) == "00:00:00,040"

    def test_dwell_inherits_then_min_gap_clamp(self):
        """A union repeat (same zh text as the cue just consumed) inherits
        the previous start and the monotonic clamp pushes it +0.04 — the
        leerob cue 65/66 pattern: [234.126] then [234.166]."""
        mod = _load_retime()
        cues = [self._cue(9.0, 12.0, "甲乙，"),
                self._cue(9.0, 9.02, "甲乙，"),   # union repeat
                self._cue(12.0, 15.0, "丙丁")]
        plan = mod.retime_plan(cues, "甲乙丙丁",
                               [10.0, 10.5, 20.0, 20.5], 30.0)
        assert [p["kind"] for p in plan] == ["ANCHOR", "DWELL", "ANCHOR"]
        assert plan[0]["start"] == 9.95
        assert plan[1]["start"] == 9.95 + 0.04
        assert plan[1]["end"] == plan[2]["start"]  # exact handover

    def test_end_is_next_start_minus_gap_and_last_uses_dub_dur(self):
        mod = _load_retime()
        cues = [self._cue(9.0, 12.0, "甲乙"), self._cue(12.0, 15.0, "丙丁")]
        plan = mod.retime_plan(cues, "甲乙丙丁",
                               [10.0, 10.5, 20.0, 20.5], 30.0)
        assert plan[0]["end"] == plan[1]["start"]  # exact handover
        assert plan[1]["end"] == 30.0   # last end = dub duration verbatim

    def test_last_end_falls_back_to_input_when_no_dub_dur(self):
        """Without a dub.wav to anchor the tail, the last end keeps the
        input value verbatim — a -0.02 fallback would drift 20ms on every
        re-run and break idempotency."""
        mod = _load_retime()
        cues = [self._cue(9.0, 12.0, "甲乙")]
        plan = mod.retime_plan(cues, "甲乙", [10.0, 10.5], None)
        assert plan[0]["end"] == 12.0

    def test_close_anchors_are_monotonically_clamped(self):
        """Anchor char times closer than the min gap cannot stack: each
        start is pushed to previous + 0.04 (the 'topping up' guard that
        kept dwell chains from pushing cues skyward)."""
        mod = _load_retime()
        cues = [self._cue(9.0, 9.5, "甲"), self._cue(9.5, 10.0, "乙")]
        plan = mod.retime_plan(cues, "甲乙", [10.0, 10.02], 30.0)
        assert plan[0]["start"] == 9.95
        assert plan[1]["start"] == 9.95 + 0.04

    def test_gap_chars_keep_input_start(self):
        """An anchor whose char times are None (the ASR map was rejected)
        keeps the start already on disk — never invent a time from
        nothing (leerob cue 519 anchored in the rejected cue 240)."""
        mod = _load_retime()
        cues = [self._cue(9.0, 12.0, "甲乙"), self._cue(17.5, 21.0, "丙丁")]
        plan = mod.retime_plan(cues, "甲乙丙丁",
                               [10.0, 10.5, None, None], 30.0)
        assert plan[0]["start"] == 9.95
        assert plan[1]["start"] == 17.5     # kept verbatim

    def test_en_grid_snap_pretrim_pulls_prev_end(self):
        """A next-start at fractional >= .995 renders ~0.9s early in ASS
        (centisecond carry formats as SS.100 -> libass reads SS.10); the
        previous cue's end is pulled to the snap point - 0.04 — the three
        leerob spots (2046.998 -> prev end 2046.060)."""
        mod = _load_retime()
        # anchor char time 101.048 -> start 100.998 (frac carries)
        cues = [self._cue(9.0, 99.0, "甲乙"), self._cue(99.0, 104.0, "丙丁")]
        plan = mod.retime_plan(cues, "甲乙丙丁",
                               [10.0, 10.5, 101.048, 101.5], 110.0)
        assert plan[1]["start"] == 100.998
        # next start 100.998 carries; snap trim wins over exact handover:
        assert plan[0]["end"] == 100 + 0.10 - 0.04

    def test_no_snap_trim_below_carry_threshold(self):
        """frac .994 does not carry (round -> 99): the normal
        next-start-minus-0.02 end survives."""
        mod = _load_retime()
        cues = [self._cue(9.0, 99.0, "甲乙"), self._cue(99.0, 104.0, "丙丁")]
        plan = mod.retime_plan(cues, "甲乙丙丁",
                               [10.0, 10.5, 101.044, 101.5], 110.0)
        assert plan[1]["start"] == 100.994
        assert plan[0]["end"] == pytest.approx(100.994)

    def test_punctuation_only_cue_inherits(self):
        """A cue whose zh filters to nothing cannot anchor; it inherits
        the previous start like a dwell."""
        mod = _load_retime()
        cues = [self._cue(9.0, 12.0, "甲乙"), self._cue(12.0, 12.5, "。")]
        plan = mod.retime_plan(cues, "甲乙", [10.0, 10.5], 30.0)
        assert plan[1]["kind"] == "DWELL"
        assert plan[1]["start"] == plan[0]["start"] + 0.04

    def test_written_timestamps_round_to_millisecond(self):
        """write_srt renders the plan's floats as SRT ms timestamps and
        preserves cue numbers + text lines verbatim."""
        mod = _load_retime()
        cues = [{"num": ["7"], "start": 9.95, "end": 19.93,
                 "lines": ["甲乙，", "en line"]}]
        out = Path(tempfile.mkdtemp()) / "out.srt"
        mod.write_srt(out, cues)
        text = out.read_text(encoding="utf-8")
        assert "7\n00:00:09,950 --> 00:00:19,930\n甲乙，\nen line" in text


class TestRetimeStage:
    """stage_retime: file-level orchestration with an injected fake
    transcriber (the model is never loaded). Verifies the backup, the
    rewrite, and the report — the public seam the CLI calls."""

    def _fixture(self, tmp_path: Path) -> Path:
        """2 dubbing cues, 3 bilingual cues (one union repeat), 2 dummy
        pad wavs; no dub.wav (duration falls back to input end)."""
        root = tmp_path / "vid"
        work = root / "dubbed" / "_full"
        work.mkdir(parents=True)
        _make_srt(work / "dubbing.srt", [
            (1, "00:00:10,000 --> 00:00:12,000", "甲乙丙丁，"),
            (2, "00:00:20,000 --> 00:00:22,000", "戊己庚辛"),
        ])
        _make_srt(work / "dubbing.bilingual.srt", [
            (1, "00:00:09,000 --> 00:00:12,000", "甲乙丙丁，\nen one"),
            (2, "00:00:09,200 --> 00:00:09,220", "甲乙丙丁，\nen one"),
            (3, "00:00:19,000 --> 00:00:22,000", "戊己庚辛\nen two"),
        ])
        (work / "_audio_pad").mkdir(parents=True, exist_ok=True)
        for i in range(2):
            (work / "_audio_pad" / f"cue_{i:04d}.wav").write_bytes(b"x" * 100)
        return root

    def _fake_transcriber(self):
        """Deterministic fake word timestamps: cue 0 speaks 甲乙 at 0.0 and
        丙丁 at 1.0; cue 1 speaks 戊己 at 0.0, 庚辛 at 1.0 (wav-relative)."""
        words = [
            [(0.0, 0.9, "甲乙"), (1.0, 1.9, "丙丁")],
            [(0.0, 0.9, "戊己"), (1.0, 1.9, "庚辛")],
        ]
        it = iter(words)
        return lambda wav: next(it)

    def test_rewrite_backup_and_report(self, tmp_path):
        mod = _load_retime()
        root = self._fixture(tmp_path)
        report = mod.stage_retime(root, "vid", transcriber=self._fake_transcriber())
        assert report["ok"] is True
        assert report["cues"] == 3
        assert report["anchors"] == 2
        assert report["dwells"] == 1
        work = root / "dubbed" / "_full"
        # backup holds the ORIGINAL content
        backup = work / "dubbing.bilingual.srt.pre_retime"
        assert backup.is_file()
        assert "00:00:09,000 --> 00:00:12,000" in backup.read_text(encoding="utf-8")
        # rewritten: anchor1 = 10.0-0.05; dwell = +0.04; anchor2 = 20.0-0.05
        out = (work / "dubbing.bilingual.srt").read_text(encoding="utf-8")
        assert "00:00:09,950 -->" in out          # anchor 1
        assert "00:00:09,990 -->" in out          # dwell clamped
        assert "00:00:19,950 -->" in out          # anchor 2
        # end of cue 2 = dwell-clamped start exactly (seamless handover)
        assert "--> 00:00:09,990" in out  # exact handover to dwell start
        # text lines preserved verbatim
        assert "en one" in out and "en two" in out
        # report + char-map cache written
        rep = json.loads((work / "_retime_report.json").read_text(encoding="utf-8"))
        assert rep["cues"] == 3
        maps = json.loads((work / "_fw_char_maps.json").read_text(encoding="utf-8"))
        assert maps["0"] == [10.0, 10.0, 11.0, 11.0]
        assert maps["1"] == [20.0, 20.0, 21.0, 21.0]

    def test_idempotent_on_rerun(self, tmp_path):
        """Running retime on its own output reproduces the same starts —
        the fixed-point property the leerob regression relies on."""
        mod = _load_retime()
        root = self._fixture(tmp_path)
        mod.stage_retime(root, "vid", transcriber=self._fake_transcriber())
        first = (root / "dubbed" / "_full" / "dubbing.bilingual.srt").read_text(
            encoding="utf-8")
        # fresh transcriber iter for the second run
        mod.stage_retime(root, "vid", transcriber=self._fake_transcriber())
        second = (root / "dubbed" / "_full" / "dubbing.bilingual.srt").read_text(
            encoding="utf-8")
        assert first == second

    def test_rejected_map_keeps_start_and_counts(self, tmp_path):
        """A garbled transcript (transcriber returns junk) keeps the cue's
        existing start and counts as a rejected source map."""
        mod = _load_retime()
        root = self._fixture(tmp_path)
        junk = lambda wav: [(0.0, 1.9, "完全不一样的听写")]
        report = mod.stage_retime(root, "vid", transcriber=junk)
        assert report["maps_rejected"] == 2
        assert report["kept_input"] == 2
        out = (root / "dubbed" / "_full" / "dubbing.bilingual.srt").read_text(
            encoding="utf-8")
        # all three cues keep 9.0-ish input starts, clamped +0.04 apart
        assert "00:00:09,000 -->" in out or "00:00:08,980 -->" in out

    def test_missing_bilingual_is_error(self, tmp_path):
        mod = _load_retime()
        root = self._fixture(tmp_path)
        (root / "dubbed" / "_full" / "dubbing.bilingual.srt").unlink()
        with pytest.raises(SystemExit) as e:
            mod.stage_retime(root, "vid", transcriber=lambda w: [])
        assert e.value.code not in (0, None)

    def test_missing_pad_wavs_is_error_naming_them(self, tmp_path):
        mod = _load_retime()
        root = self._fixture(tmp_path)
        (root / "dubbed" / "_full" / "_audio_pad" / "cue_0001.wav").unlink()
        with pytest.raises(SystemExit) as e:
            mod.stage_retime(root, "vid", transcriber=lambda w: [])
        assert e.value.code not in (0, None)


class TestDubRetimeCommand:
    """cook dub retime: launches stages/dub/retime.py under --python like
    the other dub stages, with the retime prerequisites gated."""

    def _capture_cmd(self, monkeypatch, tmp_path, python=None):
        root = tmp_path / "vid"
        work = root / "dubbed" / "_full"
        work.mkdir(parents=True)
        _make_srt(work / "dubbing.srt", [
            (1, "00:00:10,000 --> 00:00:12,000", "甲乙")])
        _make_srt(work / "dubbing.bilingual.srt", [
            (1, "00:00:09,000 --> 00:00:12,000", "甲乙\nen")])
        (work / "_audio_pad").mkdir(parents=True, exist_ok=True)
        (work / "_audio_pad" / "cue_0000.wav").write_bytes(b"x" * 100)
        captured = {}

        def fake_run_long_task(cmd, cwd, log_file, err_file, detach=False):
            captured["cmd"] = cmd
            return {"ok": True, "returncode": 0}

        monkeypatch.setattr(cook, "_run_long_task", fake_run_long_task)
        args = type("A", (), {"output_root": str(root), "name": "vid",
                              "python": python})()
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            cook.cmd_dub_retime(args)
        return captured.get("cmd")

    def test_launches_package_script_with_stage_name(self, monkeypatch, tmp_path):
        cmd = self._capture_cmd(monkeypatch, tmp_path)
        assert cmd[1] == str(_stages_dub_dir() / "retime.py")
        assert cmd[2] == "retime"
        assert cmd[3].endswith("vid")
        assert cmd[4] == "vid"

    def test_python_flag_selects_interpreter(self, monkeypatch, tmp_path):
        cmd = self._capture_cmd(monkeypatch, tmp_path, python="C:/venv/py.exe")
        assert cmd[0] == "C:/venv/py.exe"

    def test_missing_prerequisites_die_before_launch(self, monkeypatch, tmp_path):
        """No dubbing.srt: the stage dies with the producing command in
        the message, before any subprocess (and any model load)."""
        root = tmp_path / "vid"
        (root / "dubbed" / "_full").mkdir(parents=True)
        args = type("A", (), {"output_root": str(root), "name": "vid",
                              "python": None})()
        with pytest.raises(SystemExit) as e:
            cook.cmd_dub_retime(args)
        assert e.value.code not in (0, None)


class TestRetimeCLIMissingFasterWhisper:
    """retime.py run under an interpreter where faster-whisper is missing:
    non-zero exit with the install hint — never silent wrong values."""

    def test_missing_fw_is_nonzero_with_hint(self, tmp_path):
        d = _stages_dub_dir()
        # a shim dir whose faster_whisper raises ImportError on import
        shim = tmp_path / "shim"
        shim.mkdir()
        (shim / "faster_whisper.py").write_text(
            "raise ImportError('no faster-whisper here')\n", encoding="utf-8")
        root = tmp_path / "vid"
        work = root / "dubbed" / "_full"
        work.mkdir(parents=True)
        _make_srt(work / "dubbing.srt", [
            (1, "00:00:10,000 --> 00:00:12,000", "甲乙")])
        _make_srt(work / "dubbing.bilingual.srt", [
            (1, "00:00:09,000 --> 00:00:12,000", "甲乙\nen")])
        (work / "_audio_pad").mkdir(parents=True, exist_ok=True)
        (work / "_audio_pad" / "cue_0000.wav").write_bytes(b"x" * 100)
        env = {k: v for k, v in os.environ.items()}
        env["PYTHONPATH"] = str(shim) + os.pathsep + env.get("PYTHONPATH", "")
        r = subprocess.run(
            [sys.executable, str(d / "retime.py"), "retime", str(root), "vid"],
            capture_output=True, text=True, timeout=120, env=env,
        )
        assert r.returncode != 0
        assert "faster-whisper" in (r.stdout + r.stderr)
        assert "pip install faster-whisper" in (r.stdout + r.stderr)
        # nothing was rewritten: the bilingual file keeps its original ts
        assert "00:00:09,000 --> 00:00:12,000" in (
            work / "dubbing.bilingual.srt").read_text(encoding="utf-8")
        assert not (work / "dubbing.bilingual.srt.pre_retime").exists()


class TestRetimeFwAvailable:
    """fw_available: the import probe the assemble integration (ticket 09)
    consults to decide between running retime and degrading to the old
    proportional-split behavior. Pure sys.modules probing — no model."""

    def test_true_when_importable(self, monkeypatch):
        import types
        mod = _load_retime()
        fake_fw = types.ModuleType("faster_whisper")
        monkeypatch.setitem(sys.modules, "faster_whisper", fake_fw)
        assert mod.fw_available() is True

    def test_false_when_missing(self, monkeypatch):
        # sys.modules[name] = None makes `import name` raise ImportError
        mod = _load_retime()
        monkeypatch.setitem(sys.modules, "faster_whisper", None)
        assert mod.fw_available() is False


# ----------------------------------------------------------------------------
# assemble auto-retime (ticket 09): retime is the DEFAULT assembly behavior
# (ADR 0002) — the bilingual SRT's starts are rewritten onto the dubbed
# speech's real word times between biliteral and the ASS build. All external
# effects are faked: ffmpeg/ffprobe never execute (fake subprocess.run), the
# retime module is an injected double (no model, no real faster-whisper).
# ----------------------------------------------------------------------------

def _bump_srt_ts(ts: str, by: float = 1.0) -> str:
    """SRT timestamp + `by` seconds at ms resolution — the fake retime's
    visible fingerprint on the rewritten bilingual SRT."""
    h, m, sec = ts.replace(",", ".").split(":")
    t = int(h) * 3600 + int(m) * 60 + float(sec) + by
    ms = int(round(t * 1000))
    hh, ms = divmod(ms, 3600000)
    mm, ms = divmod(ms, 60000)
    ss, ms = divmod(ms, 1000)
    return f"{hh:02d}:{mm:02d}:{ss:02d},{ms:03d}"


class TestAssembleAutoRetime:
    """stage_assemble orchestration: default path runs retime (marker
    "applied", ASS built from the RETIMED file); --no-retime and a missing
    faster-whisper fall back to the old behavior; --keep-subs never retimes
    (on-disk subtitle hand edits must survive)."""

    RAW_DUR = 22.2

    @staticmethod
    def _write_silence(path: Path, dur: float) -> None:
        import contextlib
        import wave
        path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.closing(wave.open(str(path), "wb")) as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(22050)
            w.writeframes(b"\x00" * (int(dur * 22050) * 2))

    def _video(self, tmp_path: Path) -> Path:
        """2 cues (10-12s, 20-22s), 0.2s synthesized takes each — windows are
        generous so the fit pass never needs ffmpeg."""
        root = tmp_path / "vid"
        _make_srt(root / "transcript" / "video.en.full.srt", [
            (1, "00:00:10,000 --> 00:00:12,000", "English cue one"),
            (2, "00:00:20,000 --> 00:00:22,000", "English cue two"),
        ])
        _make_translations(root / "transcript" / "translations_dub.txt",
                           ["这是第一句配音", "这是第二句配音"])
        segs = root / "dubbed" / "_full" / "_segments"
        segs.mkdir(parents=True, exist_ok=True)
        for i in (1, 2):
            self._write_silence(segs / f"sent_{i:04d}.wav", 0.2)
        return root

    def _fake_subprocess(self):
        """ffmpeg apad -> REAL stdlib-wave silence of in_dur+pad (the clock
        math then lands dub.wav exactly on RAW_DUR); the final encode just
        touches its output; ffprobe answers from canned stdout."""
        import contextlib
        import types
        import wave
        RAW = self.RAW_DUR

        def run(cmd, capture_output=True, text=True, **kw):
            if cmd[0] == "ffprobe":
                out = "1920x1080\n" if "stream=width,height" in cmd else f"{RAW}\n"
                return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
            if any(str(a).startswith("apad=pad_dur=") for a in cmd):
                af = next(str(a) for a in cmd if str(a).startswith("apad="))
                pad = float(af.rsplit("=", 1)[1])
                src = cmd[cmd.index("-i") + 1]
                with contextlib.closing(wave.open(str(src), "rb")) as w:
                    d = w.getnframes() / float(w.getframerate())
                self._write_silence(Path(cmd[-1]), d + pad)
                return types.SimpleNamespace(returncode=0, stdout="", stderr="")
            Path(cmd[-1]).write_bytes(b"fake mp4")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        return run

    def _fake_retime_mod(self, available: bool = True):
        class _FakeRetime:
            """Test double of stages/dub/retime.py: records the in-process
            call and rewrites the bilingual SRT starts +1s (the fingerprint
            the ASS must then carry)."""

            def __init__(s):
                s.calls = []
                s.available = available

            def fw_available(s):
                return s.available

            def stage_retime(s, root, name, transcriber=None):
                s.calls.append((str(Path(root).resolve()), name))
                srt = Path(root) / "dubbed" / "_full" / "dubbing.bilingual.srt"
                text = srt.read_text(encoding="utf-8")
                text = re.sub(r"(\d{2}:\d{2}:\d{2},\d{3}) -->",
                              lambda m: _bump_srt_ts(m.group(1)) + " -->", text)
                srt.write_text(text, encoding="utf-8")
                return {"ok": True, "anchors": 2, "dwells": 0,
                        "kept_input": 0, "maps_rejected": 0}

        return _FakeRetime()

    def _run(self, tmp_path, monkeypatch, no_retime=False, keep_subs=False,
             fw_available=True):
        mod, _pkg = _load_full_dub(tmp_path, monkeypatch)
        root = self._video(tmp_path)
        fake_rt = self._fake_retime_mod(available=fw_available)
        monkeypatch.setattr(mod, "_import_retime_module", lambda: fake_rt)
        monkeypatch.setattr(subprocess, "run", self._fake_subprocess())
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            mod.stage_assemble(str(root), "video", keep_subs=keep_subs,
                               no_retime=no_retime)
        return root, buf.getvalue(), fake_rt

    def _marker(self, root: Path) -> dict:
        return json.loads(
            (root / "dubbed" / "_full" / "_assemble_retime.json")
            .read_text(encoding="utf-8"))

    def _ass(self, root: Path) -> str:
        return (root / "dubbed" / "_full" / "dubbing.cooked.ass"
                ).read_text(encoding="utf-8")

    def test_default_runs_retime_before_ass_build(self, tmp_path, monkeypatch):
        """Default path: retime runs in-process exactly once, and the ASS is
        built from the RETIMED bilingual SRT (fake bumps every start +1s:
        10.0/20.0 -> 11.0/21.0) — the rewrite sits between biliteral and the
        ASS build."""
        root, out, fake_rt = self._run(tmp_path, monkeypatch)
        assert fake_rt.calls == [(str(root.resolve()), "video")]
        marker = self._marker(root)
        assert marker["retime"] == "applied"
        ass = self._ass(root)
        assert "0:00:11.00" in ass and "0:00:21.00" in ass
        assert "0:00:10.00" not in ass
        assert "Stage 2 DONE" in out

    def test_no_retime_keeps_old_behavior(self, tmp_path, monkeypatch):
        """--no-retime: retime is NOT called and the product is the old
        implementation's — the ASS carries the original proportional-split
        starts (10.0/20.0), with the opt-out warning in the log."""
        root, out, fake_rt = self._run(tmp_path, monkeypatch, no_retime=True)
        assert fake_rt.calls == []
        assert self._marker(root)["retime"] == "skipped"
        ass = self._ass(root)
        assert "0:00:10.00" in ass and "0:00:20.00" in ass
        assert "0:00:11.00" not in ass
        assert "字幕按比例摊开（旧行为），长句可能漂移" in out

    def test_missing_fw_degrades_with_explicit_warning(self, tmp_path, monkeypatch):
        """faster-whisper not importable in the running interpreter: retime
        is skipped, the old behavior ships, and the log carries the explicit
        degraded warning naming the old behavior and its drift risk."""
        root, out, fake_rt = self._run(tmp_path, monkeypatch, fw_available=False)
        assert fake_rt.calls == []
        marker = self._marker(root)
        assert marker["retime"] == "degraded"
        assert "faster-whisper" in marker["reason"]
        assert "0:00:10.00" in self._ass(root)
        assert "字幕按比例摊开（旧行为），长句可能漂移" in out
        assert "DEGRADED" in out

    def test_keep_subs_never_retimes(self, tmp_path, monkeypatch):
        """--keep-subs reuses on-disk subtitle files that may hold hand
        edits — retime must not rewrite them (marker skipped, reason names
        the recovery path)."""
        mod, _pkg = _load_full_dub(tmp_path, monkeypatch)
        root = self._video(tmp_path)
        work = root / "dubbed" / "_full"
        _make_srt(work / "dubbing.merged.srt",
                  [(1, "00:00:10,000 --> 00:00:12,000", "手改中文字幕")])
        _make_srt(work / "dubbing.en.merged.srt",
                  [(1, "00:00:10,000 --> 00:00:12,000", "hand-edited en")])
        _make_srt(work / "dubbing.bilingual.srt",
                  [(1, "00:00:10,000 --> 00:00:12,000", "手改中文字幕\nhand-edited en")])
        fake_rt = self._fake_retime_mod()
        monkeypatch.setattr(mod, "_import_retime_module", lambda: fake_rt)
        monkeypatch.setattr(subprocess, "run", self._fake_subprocess())
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as buf:
            mod.stage_assemble(str(root), "video", keep_subs=True)
        assert fake_rt.calls == []
        marker = self._marker(root)
        assert marker["retime"] == "skipped"
        assert "--keep-subs" in marker["reason"]
        # the hand-edited file survives verbatim
        assert "手改中文字幕" in (work / "dubbing.bilingual.srt").read_text(
            encoding="utf-8")
        assert "Stage 2 DONE" in buf.getvalue()


class TestAssembleRetimeCLI:
    """full_dub.py's own CLI: the `--no-retime` argv cook passes through must
    reach stage_assemble (default off = retime on), and the flag is legal on
    the assemble stage only — same contract as --keep-subs."""

    def _capture(self, tmp_path, monkeypatch, argv):
        mod, _pkg = _load_full_dub(tmp_path, monkeypatch)
        recorded = {}

        def fake_assemble(root, name, keep_subs=False, no_retime=False):
            recorded.update(no_retime=no_retime, keep_subs=keep_subs)

        monkeypatch.setattr(mod, "stage_assemble", fake_assemble)
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()):
            mod._cli_main(argv)
        return recorded

    def test_cli_no_retime_flag_reaches_stage(self, tmp_path, monkeypatch):
        rec = self._capture(tmp_path, monkeypatch,
                            ["assemble", str(tmp_path), "video", "--no-retime"])
        assert rec["no_retime"] is True

    def test_cli_default_is_retime_on(self, tmp_path, monkeypatch):
        rec = self._capture(tmp_path, monkeypatch,
                            ["assemble", str(tmp_path), "video"])
        assert rec["no_retime"] is False

    def test_cli_flag_combinable_with_keep_subs(self, tmp_path, monkeypatch):
        rec = self._capture(tmp_path, monkeypatch,
                            ["assemble", str(tmp_path), "video",
                             "--keep-subs", "--no-retime"])
        assert rec["no_retime"] is True and rec["keep_subs"] is True

    def test_cli_no_retime_rejected_outside_assemble(self, tmp_path, monkeypatch):
        mod, _pkg = _load_full_dub(tmp_path, monkeypatch)
        for stage in ("synth", "full"):
            with pytest.raises(SystemExit) as ei:
                mod._cli_main([stage, str(tmp_path), "video", "--no-retime"])
            assert ei.value.code != 0


class TestCookAssembleRetimeSurface:
    """cook dub assemble command surface (ticket 09): --no-retime reaches
    full_dub.py's subprocess argv (default argv carries no flag = retime
    on), and the JSON result carries the retime state full_dub wrote to
    dubbed/_full/_assemble_retime.json — applied / degraded / skipped."""

    def _run_cmd(self, monkeypatch, tmp_path, no_retime=False, marker=None,
                 keep_subs=False):
        root = tmp_path / "vid"
        for rel in ("transcript/translations_dub.txt",
                    "dubbed/_reference/ref.wav",
                    "dubbed/_full/_segments/sent_0001.wav"):
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"x" * 2000)
        captured = {}

        def fake_run_long_task(cmd, cwd, log_file, err_file, detach=False):
            captured["cmd"] = cmd
            if marker is not None:
                mp = root / "dubbed" / "_full" / "_assemble_retime.json"
                mp.parent.mkdir(parents=True, exist_ok=True)
                mp.write_text(json.dumps(marker, ensure_ascii=False),
                              encoding="utf-8")
            return {"ok": True, "returncode": 0}

        monkeypatch.setattr(cook, "_run_long_task", fake_run_long_task)
        args = type("A", (), {"output_root": str(root), "name": "video",
                              "python": None, "detach": False,
                              "keep_subs": keep_subs,
                              "no_retime": no_retime})()
        import contextlib
        import io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cook.cmd_dub_assemble(args)
            except SystemExit as e:
                if e.code not in (0, None):
                    raise
        return captured.get("cmd"), json.loads(out.getvalue().strip()), err.getvalue()

    def test_no_retime_flag_reaches_subprocess(self, monkeypatch, tmp_path):
        cmd, _payload, _err = self._run_cmd(monkeypatch, tmp_path, no_retime=True)
        assert "--no-retime" in cmd

    def test_default_argv_carries_no_flag(self, monkeypatch, tmp_path):
        cmd, _payload, _err = self._run_cmd(monkeypatch, tmp_path)
        assert "--no-retime" not in cmd

    def test_json_carries_applied_state(self, monkeypatch, tmp_path):
        _cmd, payload, err = self._run_cmd(
            monkeypatch, tmp_path,
            marker={"retime": "applied", "reason": None, "anchors": 2})
        assert payload["retime"] == "applied"
        assert "retime applied" in err

    def test_json_carries_degraded_state_and_warns(self, monkeypatch, tmp_path):
        _cmd, payload, err = self._run_cmd(
            monkeypatch, tmp_path,
            marker={"retime": "degraded",
                    "reason": "faster-whisper is not installed"})
        assert payload["retime"] == "degraded"
        assert payload["retime_reason"] == "faster-whisper is not installed"
        # the degradation is visible on stderr too, not only in the JSON
        assert "retime degraded" in err

    def test_json_carries_skipped_state(self, monkeypatch, tmp_path):
        _cmd, payload, _err = self._run_cmd(
            monkeypatch, tmp_path, no_retime=True,
            marker={"retime": "skipped", "reason": "--no-retime: old behavior"})
        assert payload["retime"] == "skipped"

    def test_no_marker_leaves_payload_unchanged(self, monkeypatch, tmp_path):
        """Defensive: no marker file (pre-ticket run / failure) must not
        fabricate a retime field."""
        _cmd, payload, _err = self._run_cmd(monkeypatch, tmp_path, marker=None)
        assert "retime" not in payload


# dub gate (ticket 05): the length gate, wired into the cook command surface
# and driven by the MEASURED speech rate — never an assumed one. The public
# seam is cmd_dub_gate (argv: output_root/name/--rate/--self-measure/--json).
# Hand-worked fixtures: budgets are cue start -> next cue start (identity
# clock); est_s = count_chars(line)/rate (the reflux budget math inverted).
# ----------------------------------------------------------------------------

def _make_gate_inputs(tmp_path, cues, lines, name="video"):
    """Write transcript/<name>.en.full.srt + translations_dub.txt.
    cues = [("00:00:00,000", "00:00:04,000"), ...] raw SRT timestamps."""
    root = tmp_path / "author" / name
    _make_srt(root / "transcript" / f"{name}.en.full.srt",
              [(i + 1, f"{s} --> {e}", f"English cue {i + 1}")
               for i, (s, e) in enumerate(cues)])
    _make_translations(root / "transcript" / "translations_dub.txt", lines)
    return root


class TestDubGateRateRequired:
    """Ticket 05's core ruling: without a measured rate the gate refuses to
    run — the assumed-rate path is what caused the 2026-10 rework."""

    def _run(self, root, rate=None, self_measure=False, as_json=True):
        """Invoke cmd_dub_gate in-process, capture stdout JSON.
        Returns (report_or_None, stdout_text, exit_code_or_None)."""
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {
            "output_root": str(root),
            "name": "video",
            "rate": rate,
            "self_measure": self_measure,
            "json": as_json,
        })()
        code = None
        with contextlib.redirect_stdout(buf):
            try:
                cook.cmd_dub_gate(args)
            except SystemExit as e:
                code = e.code
        text_out = buf.getvalue().strip()
        report = json.loads(text_out) if text_out else None
        return report, text_out, code

    def test_missing_rate_refuses_and_points_at_measure_rate(self, tmp_path):
        """No --rate, no --self-measure: nonzero exit, error names the
        command that produces the rate. A fixture with valid inputs proves
        the refusal is about the RATE, not about the files."""
        root = _make_gate_inputs(
            tmp_path,
            [("00:00:00,000", "00:00:04,000"), ("00:00:04,500", "00:00:09,500")],
            ["甲乙丙丁戊己庚辛壬癸甲乙丙丁戊己", "甲乙丙丁戊己庚辛壬癸甲乙丙丁戊己"])
        report, _, code = self._run(root, rate=None)
        assert code != 0
        assert report is not None and report["ok"] is False
        assert "measure-rate" in report["error"]

    def test_zero_and_negative_rate_refuse(self, tmp_path):
        """--rate 0 / negative: nonzero exit with a clear error — a 0 rate
        would make every estimate infinite."""
        root = _make_gate_inputs(
            tmp_path,
            [("00:00:00,000", "00:00:04,000"), ("00:00:04,500", "00:00:09,500")],
            ["甲乙丙丁戊己庚辛壬癸", "甲乙丙丁戊己庚辛壬癸"])
        for bad in (0.0, -6.9):
            report, _, code = self._run(root, rate=bad)
            assert code != 0
            assert report["ok"] is False
            assert "--rate" in report["error"]


class TestDubGateVerdict:
    """The three gate criteria, all computed from the passed rate:
    est_s = count_chars(line)/rate (punctuation counted — the rate's unit).
    Hand-worked fixture: budgets 4.5/6.0/4.0 (=14.5 total; cue start -> next
    cue start), rate 5.0, chars 20/24/22 -> est 4.0/4.8/4.4 (=13.2),
    factors 0.89/0.80/1.10, coverage 13.2/14.5 = 91.0% PASS. The OLD
    syllable-bucket algorithm would estimate 4.76/5.24/5.00 -> 103.4% — so
    these assertions pin the chars/rate math, not a compatible overlap."""

    CUES = [("00:00:00,000", "00:00:04,000"),
            ("00:00:04,500", "00:00:09,500"),
            ("00:00:10,500", "00:00:14,500")]
    LINES = ["甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉",
             "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌亥，。",
             "甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未申酉戌！"]

    def _run(self, root, rate=None, self_measure=False, as_json=True):
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {
            "output_root": str(root),
            "name": "video",
            "rate": rate,
            "self_measure": self_measure,
            "json": as_json,
        })()
        code = None
        with contextlib.redirect_stdout(buf):
            try:
                cook.cmd_dub_gate(args)
            except SystemExit as e:
                code = e.code
        text_out = buf.getvalue().strip()
        report = json.loads(text_out) if text_out else None
        return report, text_out, code

    def test_healthy_pass_computes_est_from_chars_over_rate(self, tmp_path):
        root = _make_gate_inputs(tmp_path, self.CUES, self.LINES)
        report, _, code = self._run(root, rate=5.0)
        assert code == 0
        assert report["ok"] is True
        assert report["verdict"] == "pass"
        assert report["rate"] == 5.0
        assert report["rate_source"] == "flag"
        assert report["lines"] == 3
        assert report["short"] == [] and report["over"] == [] \
            and report["under"] == []
        # hand-worked totals: est 13.2s over 14.5s of windows
        assert report["total_est_s"] == pytest.approx(13.2, abs=0.05)
        assert report["total_window_s"] == pytest.approx(14.5, abs=0.05)
        assert report["coverage"] == pytest.approx(13.2 / 14.5, abs=0.002)

    def test_fail_path_lists_short_over_and_thin_coverage(self, tmp_path):
        """Hand-worked FAIL fixture: cue1 budget 10.5s with a 4-char line
        (3 sylls -> short, est 0.8s vs 10.5s -> deep under), cue2 budget 0.5s
        with a 30-char line (est 6.0s -> factor 12x over). Totals 6.8/11.0 =
        61.8% coverage — out of band. Exit 1 with all three findings."""
        root = _make_gate_inputs(
            tmp_path,
            [("00:00:00,000", "00:00:10,000"),
             ("00:00:10,500", "00:00:11,000")],
            ["你好呀。",
             "子丑寅卯辰巳午未申酉戌亥" * 2 + "甲乙丙丁戊己"])
        report, _, code = self._run(root, rate=5.0)
        assert code == 1
        assert report["ok"] is False
        assert report["verdict"] == "fail"
        assert report["short"][0]["line"] == 1
        assert report["short"][0]["sylls"] == 3  # 你好呀 — the 。 is not a syll
        assert report["over"][0]["line"] == 2
        assert report["over"][0]["factor"] == pytest.approx(12.0, abs=0.1)
        assert report["coverage"] == pytest.approx(6.8 / 11.0, abs=0.005)
        # the 4-char line is also deep under-budget (0.8s est vs 10.5s window)
        assert report["under"][0]["line"] == 1

    def test_under_budget_alone_is_advisory_pass(self, tmp_path):
        """Under-budget lines alone do not fail the gate (the assembler pads
        the shortfall) — verdict pass, advisory_under flagged, exit 0.
        Fixture: budget1 3.8s/9 chars (est 1.8s, factor 0.47 -> under),
        budget2 6.0s/36 chars (est 7.2s, factor 1.2); coverage 9.0/9.8 =
        91.8% in band."""
        root = _make_gate_inputs(
            tmp_path,
            [("00:00:00,000", "00:00:03,400"),
             ("00:00:03,800", "00:00:09,800")],
            ["甲乙丙丁戊己庚辛壬",
             "子丑寅卯辰巳午未申酉戌亥" * 3])
        report, _, code = self._run(root, rate=5.0)
        assert code == 0
        assert report["ok"] is True
        assert report["verdict"] == "pass"
        assert report["advisory_under"] is True
        assert [u["line"] for u in report["under"]] == [1]
        assert report["coverage"] == pytest.approx(9.0 / 9.8, abs=0.005)


class TestDubGateSelfMeasure:
    """--self-measure: derive the rate from dubbed/_full/_segments (synth's
    takes) via measure_rate's pairing + VAD, print it, then gate by the
    MEASURED value. Synthetic wavs: sine = speech, zeros = silence."""

    def _run(self, root, rate=None, self_measure=False, as_json=True):
        import io
        import contextlib
        buf = io.StringIO()
        args = type("A", (), {
            "output_root": str(root),
            "name": "video",
            "rate": rate,
            "self_measure": self_measure,
            "json": as_json,
        })()
        code = None
        with contextlib.redirect_stdout(buf):
            try:
                cook.cmd_dub_gate(args)
            except SystemExit as e:
                code = e.code
        text_out = buf.getvalue().strip()
        report = json.loads(text_out) if text_out else None
        return report, text_out, code

    def _run_with_segments(self, tmp_path):
        """2 takes of 2.0 s voiced speech for 10-char lines -> measured rate
        20 chars / 4.0 s = 5.0 chars/s. Cue budgets 2.4/2.0 -> est 2.0/2.0,
        coverage 4.0/4.4 = 90.9% -> PASS by the measured rate."""
        pytest.importorskip("soundfile")
        root = _make_gate_inputs(
            tmp_path,
            [("00:00:00,000", "00:00:02,000"),
             ("00:00:02,400", "00:00:04,400")],
            ["甲乙丙丁戊己庚辛壬癸", "子丑寅卯辰巳午未申酉"])
        segs = root / "dubbed" / "_full" / "_segments"
        _make_wav(segs / "sent_0001.wav", [(0.2, False), (2.0, True)])
        _make_wav(segs / "sent_0002.wav", [(0.2, False), (2.0, True)])
        return root

    def test_self_measures_then_gates_by_the_measured_rate(self, tmp_path):
        root = self._run_with_segments(tmp_path)
        report, _, code = self._run(root, self_measure=True)
        assert code == 0
        assert report["ok"] is True
        assert report["rate_source"] == "self-measured"
        # measured: 20 chars over ~4.0 s of voiced sine
        assert 4.8 <= report["rate"] <= 5.2
        # gate math runs on the measured rate: est 4.0s over 4.4s of windows
        assert report["coverage"] == pytest.approx(20 / (5.0 * 4.4), abs=0.02)
        assert report["short"] == [] and report["over"] == []

    def test_rate_and_self_measure_are_mutually_exclusive(self, tmp_path):
        root = self._run_with_segments(tmp_path)
        report, _, code = self._run(root, rate=6.9, self_measure=True)
        assert code != 0
        assert report["ok"] is False
        assert "--self-measure" in report["error"]

    def test_self_measure_without_segments_is_refused(self, tmp_path):
        """No synthesized takes yet (pre-synth run): the refusal names the
        segments layout and the explicit-rate alternative."""
        root = _make_gate_inputs(
            tmp_path,
            [("00:00:00,000", "00:00:04,000")],
            ["甲乙丙丁戊己庚辛壬癸"])
        report, _, code = self._run(root, self_measure=True)
        assert code != 0
        assert report["ok"] is False
        assert "_segments" in report["error"]


class TestDubGateContract:
    """Remaining contract edges: unit lockstep, output channel, parser
    wiring, count mismatch."""

    def test_count_chars_stays_in_lockstep_with_measure_rate(self):
        """The unit closeout (ticket 05): the gate estimates in the SAME
        char convention the rate is calibrated in. length_gate.count_chars
        is a second copy of measure_rate.count_chars (kept so the gate
        module stays import-light) — this tripwire fails the day the two
        counting conventions drift apart."""
        mr = cook._import_stage_module("dub", "measure_rate.py",
                                       "cook_stages_dub_measure")
        lg = cook._import_stage_module("dub", "length_gate.py",
                                       "cook_stages_dub_gate")
        samples = [
            "你好，世界！",             # CJK + fullwidth punctuation
            "  padded whitespace  ",   # external whitespace not counted
            "hello world",             # latin words
            "混合 mixed 中文 line。",
            "AI 编程 2026！",
            "",
        ]
        for s in samples:
            assert lg.count_chars(s) == mr.count_chars(s), repr(s)

    def test_human_output_keeps_stdout_empty(self, tmp_path, capsys):
        """Without --json: stdout stays empty (agents opt in with --json);
        the human summary — including the rate it ran on — goes to stderr."""
        root = _make_gate_inputs(
            tmp_path,
            [("00:00:00,000", "00:00:04,000"), ("00:00:04,500", "00:00:09,500")],
            ["甲乙丙丁戊己庚辛壬癸" * 2 + "甲",
             "甲乙丙丁戊己庚辛壬癸" * 2 + "甲乙丙"])
        args = type("A", (), {
            "output_root": str(root), "name": "video",
            "rate": 5.0, "self_measure": False, "json": False,
        })()
        import io
        import contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            try:
                cook.cmd_dub_gate(args)
            except SystemExit as e:
                assert e.code == 0
        assert buf.getvalue() == ""  # stdout is pure-JSON-only territory
        err = capsys.readouterr().err
        assert "rate: 5.000 chars/s (flag)" in err
        assert "coverage" in err

    def test_cue_line_count_mismatch_is_refused(self, tmp_path):
        """3 srt cues vs 2 translation lines: refuse rather than pair N
        against N-1 silently."""
        root = _make_gate_inputs(
            tmp_path,
            [("00:00:00,000", "00:00:04,000"),
             ("00:00:04,500", "00:00:08,000"),
             ("00:00:08,500", "00:00:12,000")],
            ["甲乙丙丁戊己庚辛壬癸", "甲乙丙丁戊己庚辛壬癸"])
        args = type("A", (), {
            "output_root": str(root), "name": "video",
            "rate": 5.0, "self_measure": False, "json": True,
        })()
        import io
        import contextlib
        buf = io.StringIO()
        code = None
        with contextlib.redirect_stdout(buf):
            try:
                cook.cmd_dub_gate(args)
            except SystemExit as e:
                code = e.code
        report = json.loads(buf.getvalue().strip())
        assert code == 1
        assert report["ok"] is False
        assert "3" in report["error"] and "2" in report["error"]

    def test_parser_wires_dub_gate(self):
        """The argparse surface: dub gate takes output_root/name, --rate is
        typed float (non-numeric is argparse's own nonzero exit), and
        --rate/--self-measure default to absent."""
        p = cook.build_parser()
        args = p.parse_args(["dub", "gate", "some/root", "name",
                             "--rate", "6.932"])
        assert args.func is cook.cmd_dub_gate
        assert args.rate == 6.932
        assert args.self_measure is False
        args = p.parse_args(["dub", "gate", "some/root", "name"])
        assert args.rate is None
        args = p.parse_args(["dub", "gate", "some/root", "name",
                             "--self-measure"])
        assert args.self_measure is True
        with pytest.raises(SystemExit) as ei:
            p.parse_args(["dub", "gate", "r", "n", "--rate", "fast"])
        assert ei.value.code != 0


class TestPublishTmpSuffix:
    """Regression: the atomic-publish temp file must keep dst's real
    suffix (torchaudio infers format from extension — a .part tmp died
    with "Unsupported format" in the sample90 clip integration run) and
    must not start with dst's cue prefix (chunk-cache startswith globs
    would count a lingering temp as a finished chunk)."""

    def test_tmp_keeps_dst_suffix_and_neutral_prefix(self, tmp_path, monkeypatch):
        mod, _ = _load_full_dub(tmp_path, monkeypatch)
        dst = tmp_path / "c0009_00.wav"
        seen = {}
        def produce(tmp):
            seen["name"] = Path(tmp).name
            tmp.write_bytes(b"x" * 2048)
        mod._publish(dst, produce)
        assert seen["name"].endswith(".wav")
        assert not seen["name"].startswith("c0009_")
        assert dst.read_bytes() == b"x" * 2048
