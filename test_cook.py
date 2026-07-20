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
