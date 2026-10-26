"""ASR coverage audit: diff the source audio's speech segments against the
transcript's cue windows, BEFORE translation and synthesis.

The incident this command gates (nate-herk run, 2026-10): whisperX dropped
a full 19 seconds of opening-montage speech under the intro music. The
transcript never had it, so every downstream artifact shipped with the
hole - the Chinese dub AND the published bilingual subtitle version - and
three rejection rounds treated symptoms (fill material, pad source voice,
pad music bed) before anyone ran a real reconciliation against the source
audio. This command is that reconciliation, fixed as a gate: faster-whisper
rescans the SOURCE audio, every detected speech segment is checked against
the transcript's cue windows, and anything uncovered is reported as a gap
with its times and text. Run it before translating; run it again any time
a "missing/silent stretch" complaint lands (the cheap first diagnostic).

Tracks scanned, in order (speech heard on EITHER track counts as speech -
whisper can miss speech on the music-laden raw track but catch it on the
clean stem, and vice versa):
  dubbed/vocals.wav   the Demucs vocal stem, when separation has run;
                      its text wins when both tracks hear the same gap
                      (scanned first - it is the cleaner ASR input)
  raw/<name>.raw.mp4  the raw video's own audio track (faster-whisper
                      decodes it via PyAV - no pre-extract needed)
  A missing vocals.wav degrades to raw-only, a missing raw track to
  vocals-only; the report's tracks_mode/tracks_scanned name what actually
  ran (a degrade never hides behind a "vocals+raw" label).

Transcript: transcript/<name>.en.full.srt, falling back to
transcript/<name>.en.srt when the full-sentence SRT has not been built.

Coverage semantics (per speech segment [s, e], against any cue window
[cs, ce]): COVERED when the overlap exceeds half the segment's duration,
OR the segment's midpoint falls inside the window - both with a 0.3s
boundary tolerance (ASR segment edges and SRT cue edges are both
approximate), capped at half the segment's own duration so the slack can
never cover a sub-0.6s zero-overlap segment on its own. Gaps heard on
both tracks are deduped into one entry annotated with the tracks that
heard them.

The faster-whisper dependency loads lazily behind the fw_segmenter seam
(the retime.py fw_transcriber precedent): the module top level is
stdlib-only, and stage_audit_coverage takes an injectable transcriber so
tests feed canned segment lists without any model. Resource rules as
everywhere in this package: one small/int8/cpu model instance, files
transcribed strictly serially.

The report is returned as a dict, written to
dubbed/_audit_coverage.json, and (standalone) printed to stdout as a
single JSON document (progress logs go to stderr); ok=false + exit 1
when gaps were found.

Usage:
  python audit_coverage.py audit <output-root> <name>
  stage_audit_coverage(output_root, name, transcriber=None) -> report dict
"""
import json
import re
import sys
import time
from pathlib import Path

BOUNDARY_TOL_S = 0.3   # agreed slack on both ASR and SRT edges
FW_MODEL = "small"     # resource rule: small only
FW_COMPUTE = "int8"    # resource rule: int8 only
FW_DEVICE = "cpu"     # resource rule: cpu only


def _log(msg: str) -> None:
    """Progress goes to stderr so stdout stays a single JSON document both
    standalone and under cook's in-process forward."""
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------
# SRT cue loading
# --------------------------------------------------------------------------

_SRT_BLOCK = re.compile(
    r"(\d+)\s*\n(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,.]\d{3})\s*\n"
    r"(.*?)(?=\n\n|\n\d+\s*\n|\Z)", re.DOTALL,
)


def _ts(s: str) -> float:
    s = s.replace(",", ".")
    h, m, sec = s.split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def load_cue_windows(srt: Path) -> list[tuple[float, float, str]]:
    """Parse an SRT into [(start, end, text), ...] - the windows the audit
    diffs speech against."""
    cues: list[tuple[float, float, str]] = []
    with open(srt, encoding="utf-8-sig") as f:
        for m in _SRT_BLOCK.finditer(f.read()):
            text = re.sub(r"\s+", " ", m[4].strip())
            cues.append((_ts(m[2]), _ts(m[3]), text))
    return cues


# --------------------------------------------------------------------------
# Coverage math (pure; unit-tested with canned segment lists)
# --------------------------------------------------------------------------

def segment_covered(seg_start: float, seg_end: float,
                    cue_windows: list[tuple[float, float]]) -> bool:
    """True when any cue window covers the speech segment: overlap beyond
    half the segment's duration, or containment of the segment's midpoint,
    each with BOUNDARY_TOL_S of slack on the window edges. The slack is
    capped at half the segment's OWN duration — it exists to absorb edge
    error on the timestamps (touching a cue edge still counts), never to
    substitute for overlap evidence: a sub-0.6s segment sitting clear of
    every cue stays a gap (the old `overlap + BOUNDARY_TOL_S > dur / 2`
    form let the blanket 0.3s cover any segment shorter than 0.6s
    regardless of the cues)."""
    dur = seg_end - seg_start
    mid = 0.5 * (seg_start + seg_end)
    mid_tol = min(BOUNDARY_TOL_S, 0.5 * dur)
    overlap_needed = max(0.5 * dur - BOUNDARY_TOL_S, 0.0)
    for cs, ce in cue_windows:
        if cs - mid_tol <= mid <= ce + mid_tol:
            return True
        overlap = min(seg_end, ce) - max(seg_start, cs)
        if overlap > overlap_needed:
            return True
    return False


def _merge_gap(gaps: list[dict], seg: tuple[float, float, str],
               source: str) -> None:
    """Add an uncovered segment to the pooled gap list, deduping against
    the same speech already heard on another track (overlap beyond half the
    shorter span). The first-heard entry wins on times and text - the
    vocals track is scanned first because it is the cleaner measurement;
    later tracks only append their source label."""
    s, e, _text = seg
    for g in gaps:
        ov = min(e, g["end"]) - max(s, g["start"])
        if ov > 0.5 * min(e - s, g["end"] - g["start"]):
            if source not in g["sources"]:
                g["sources"].append(source)
            return
    gaps.append({"start": round(s, 3), "end": round(e, 3), "text": _text,
                 "covered": False, "sources": [source]})


# --------------------------------------------------------------------------
# Transcription (the only non-pure part; the model lives behind this seam)
# --------------------------------------------------------------------------

def fw_available() -> bool:
    """True when faster-whisper is importable in THIS interpreter - the
    preflight cook's in-process forward can consult before scanning."""
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def fw_segmenter():
    """Return transcribe(audio_path) -> [(start, end, text), ...].

    Loads ONE faster-whisper small/int8/cpu model instance (resource
    rule) and transcribes strictly serially, language pinned to English
    (the dub pipeline's source language). Dies with an actionable message
    when faster-whisper is missing from this interpreter."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        print("ERROR: faster-whisper is not installed in this interpreter - "
              "cook dub audit-coverage needs it to scan the source audio "
              "(small/int8/cpu). Install it in cook's environment: "
              "pip install faster-whisper", file=sys.stderr)
        raise SystemExit(2) from e
    model = WhisperModel(FW_MODEL, device=FW_DEVICE, compute_type=FW_COMPUTE)

    def transcribe(audio_path) -> list[tuple[float, float, str]]:
        segments, _info = model.transcribe(str(audio_path), language="en")
        return [(seg.start, seg.end, (seg.text or "").strip())
                for seg in segments if (seg.text or "").strip()]

    return transcribe


# --------------------------------------------------------------------------
# Stage entry
# --------------------------------------------------------------------------

def stage_audit_coverage(output_root, name: str, transcriber=None) -> dict:
    """Scan the source audio tracks, diff speech segments against the
    transcript cues, write dubbed/_audit_coverage.json, return the report.

    transcriber is injectable (tests pass a fake returning canned segment
    lists; the CLI passes fw_segmenter()). ok is False exactly when gaps
    were found - the gate contract."""
    root = Path(output_root).resolve()

    # transcript: en.full.srt preferred, plain en.srt as fallback
    full_srt = root / "transcript" / f"{name}.en.full.srt"
    plain_srt = root / "transcript" / f"{name}.en.srt"
    if full_srt.is_file() and full_srt.stat().st_size > 0:
        srt, fallback = full_srt, False
    elif plain_srt.is_file() and plain_srt.stat().st_size > 0:
        srt, fallback = plain_srt, True
    else:
        msg = (f"no transcript to audit against - neither {full_srt} nor "
               f"{plain_srt} exists (transcribe the source first)")
        _log(f"ERROR: {msg}")
        raise SystemExit(msg)
    cues = load_cue_windows(srt)
    if not cues:
        msg = f"{srt.name} parsed empty - nothing to diff against"
        _log(f"ERROR: {msg}")
        raise SystemExit(msg)
    cue_windows = [(s, e) for s, e, _t in cues]

    # audio tracks: vocals stem first (cleaner ASR input), then the raw
    # video's own track; speech heard on EITHER counts as speech
    vocals = root / "dubbed" / "vocals.wav"
    raw = root / "raw" / f"{name}.raw.mp4"
    tracks: list[tuple[str, Path]] = []
    if vocals.is_file() and vocals.stat().st_size > 0:
        tracks.append(("vocals", vocals))
    vocals_missing = not any(src == "vocals" for src, _p in tracks)
    if raw.is_file() and raw.stat().st_size > 0:
        tracks.append(("raw", raw))
    if not tracks:
        msg = (f"no audio to scan - need dubbed/vocals.wav and/or {raw} "
               f"(run 'cook download' + 'cook dub separate' first)")
        _log(f"ERROR: {msg}")
        raise SystemExit(msg)
    # label by what ACTUALLY scanned: vocals+raw / vocals-only / raw-only
    # (a vocals-present-raw-missing run used to be mislabeled "vocals+raw")
    tracks_scanned = [src for src, _p in tracks]
    mode = {("vocals", "raw"): "vocals+raw",
            ("vocals",): "vocals-only",
            ("raw",): "raw-only"}[tuple(tracks_scanned)]
    if "vocals" not in tracks_scanned:
        degrade = " - vocals.wav missing, raw-only degrade"
    elif "raw" not in tracks_scanned:
        degrade = " - raw track missing, vocals-only degrade"
    else:
        degrade = ""
    _log(f"audit-coverage: {len(cues)} cues from {srt.name}"
         f"{' (en.srt fallback)' if fallback else ''}; "
         f"scanning {mode}{degrade}")

    transcribe = transcriber if transcriber is not None else fw_segmenter()

    gaps: list[dict] = []
    track_stats: list[dict] = []
    segments_scanned = covered_count = 0
    for source, path in tracks:
        segments = [(float(s), float(e), str(t))
                    for s, e, t in transcribe(path)]
        n_cov = n_uncov = 0
        for s, e, text in segments:
            if segment_covered(s, e, cue_windows):
                n_cov += 1
            else:
                _merge_gap(gaps, (s, e, text), source)
                n_uncov += 1
        segments_scanned += len(segments)
        covered_count += n_cov
        track_stats.append({
            "source": source,
            "path": path.relative_to(root).as_posix(),
            "segments": len(segments),
            "covered": n_cov,
            "uncovered": n_uncov,
        })
        _log(f"  {source} ({path.name}): {len(segments)} speech segment(s), "
             f"{n_cov} covered, {n_uncov} uncovered")

    report = {
        "ok": len(gaps) == 0,
        "name": name,
        "transcript": srt.relative_to(root).as_posix(),
        "transcript_fallback": fallback,
        "cues": len(cues),
        "tracks_mode": mode,
        "tracks_scanned": tracks_scanned,
        "vocals_missing": vocals_missing,
        "tracks": track_stats,
        "segments_scanned": segments_scanned,
        "covered_count": covered_count,
        "gap_count": len(gaps),
        "gaps": gaps,
    }
    out = root / "dubbed" / "_audit_coverage.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                   encoding="utf-8")
    if gaps:
        _log(f"audit-coverage: {len(gaps)} GAP(S) - speech with no cue:")
        for g in gaps:
            _log(f"  {g['start']:8.2f} -> {g['end']:8.2f}  {g['text']}"
                 f"  [heard on: {', '.join(g['sources'])}]")
        _log(f"fix the transcript ({srt.name}) before translating; "
             f"report: {out.relative_to(root).as_posix()}")
    else:
        _log(f"audit-coverage: clean - every speech segment sits inside a "
             f"cue window ({covered_count}/{segments_scanned} covered)")
    return report


# --------------------------------------------------------------------------
# CLI (convention of full_dub.py / retime.py)
# --------------------------------------------------------------------------

def _cli_main(argv: list[str]) -> None:
    if len(argv) < 3 or argv[0] != "audit":
        print(__doc__)
        sys.exit(1)
    report = stage_audit_coverage(argv[1], argv[2])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["ok"] else 1)


if __name__ == "__main__":
    _cli_main(sys.argv[1:])
