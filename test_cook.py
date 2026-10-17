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
import subprocess
import sys
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
        """Dubbed video duration < 0.5 * raw → issue naming both durations."""
        root = tmp_path / "author" / "video"
        n = "video"
        self._stage_full_cooked_set(root, n)
        # stage the dubbed mp4 (the trigger)
        self._touch(root / "cooked" / f"{n}.dubbed.mp4")

        # Drive the ffprobe helper: raw=732.0, cooked=732.0 (so the cooked
        # mismatch check does NOT fire), dubbed=185.0 (short → fires).
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
        """Dubbed duration >= 0.5 * raw → no short issue, ok unchanged."""
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
        """No dubbed.mp4 → check does not run (non-dubbed shipments unaffected)."""
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
        """Dubbed exists but raw probes to non-positive → check skipped."""
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
    PlayResY (which for a bar build equals frame_height + bar_px) and warn
    when it differs from 1080 + args.bar_px. Overlay mode and missing-ASS
    skip the check entirely."""

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
        """bar ASS PlayResY != 1080 + bar_px → warning naming both values."""
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
        """PlayResY == 1080 + bar_px → no warning, burn proceeds."""
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
        import io, contextlib
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
        import io, contextlib
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
# dub gate (ticket 05): the length gate, wired into the cook command surface
# and driven by the MEASURED speech rate — never an assumed one. The public
# seam is cmd_dub_gate (argv: output_root/name/--rate/--self-measure/--json).
# Hand-worked fixtures: budgets are cue start → next cue start (identity
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
    Hand-worked fixture: budgets 4.5/6.0/4.0 (=14.5 total; cue start → next
    cue start), rate 5.0, chars 20/24/22 → est 4.0/4.8/4.4 (=13.2),
    factors 0.89/0.80/1.10, coverage 13.2/14.5 = 91.0% PASS. The OLD
    syllable-bucket algorithm would estimate 4.76/5.24/5.00 → 103.4% — so
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
        (3 sylls → short, est 0.8s vs 10.5s → deep under), cue2 budget 0.5s
        with a 30-char line (est 6.0s → factor 12x over). Totals 6.8/11.0 =
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
        Fixture: budget1 3.8s/9 chars (est 1.8s, factor 0.47 → under),
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
        """2 takes of 2.0 s voiced speech for 10-char lines → measured rate
        20 chars / 4.0 s = 5.0 chars/s. Cue budgets 2.4/2.0 → est 2.0/2.0,
        coverage 4.0/4.4 = 90.9% → PASS by the measured rate."""
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
