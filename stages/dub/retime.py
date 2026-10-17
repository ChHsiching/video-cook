"""Dub retime: rewrite the bilingual SRT's start times onto the dubbed
speech's real word times.

The assemble stage places each cue's synthesized take at the cue's ORIGINAL
start (the identity timeline), but the burned subtitles still carried the
shorten/merge proportional-split times — on the reflux 45-min lecture those
drifted up to 17.7s behind the actual speech (the r1 ear-gate rejection).
This stage re-derives every display cue's start from what was actually
spoken, using faster-whisper word timestamps on the CLEAN per-cue pad wavs
(dubbed/_full/_audio_pad/cue_NNNN.wav — the assembler's VO-only tracks, no
BGM bed), then a two-pass rewrite of dubbed/_full/dubbing.bilingual.srt.

The chain (calibrated bit-exact against the leerob r12 accepted output):

  1. per dubbing.srt cue i: run faster-whisper (small / int8 / cpu, ONE
     model instance, serial per file) with word_timestamps on cue_%04d.wav;
  2. SequenceMatcher maps the cue's filtered text chars (punctuation and
     whitespace dropped) onto the ASR word stream — every char gets the
     START time of the word it matched, absolute = cue start + word start;
     unmatched chars forward-fill from the previous matched char (leading
     unmatched back-fill). A cue whose transcript matches below
     below MAP_MIN_COVERAGE is rejected entirely (the garble case) and its
     chars carry no times;
  3. two-pass rewrite of the bilingual cues against the concatenated
     filtered-char stream:
       pass 1 — an ANCHOR cue (its zh text matches at the cursor) gets
                start = first char's time - ANCHOR_LEAD_S; a DWELL cue
                (union repeat: the same text as the one just consumed)
                inherits the previous start; a cue whose anchor chars have
                no word times (rejected source cue) keeps its input start;
       pass 2 — monotonic clamp (each start >= previous start + min gap),
                display end = next start - END_GAP_S, the FIRST cue gets
                the same -0.05s lead-in as every anchor, and the last end
                = the dub track's duration - END_GAP_S;
  4. EN grid-snap pre-trim: ASS timestamps are centisecond; a start whose
     centisecond rounding carries (fractional second >= 0.995) formats as
     "SS.100", which libass reads back as SS.10 — the cue then renders
     ~0.9s EARLY and stacks a double line over the previous cue. Such
     overlaps are pre-trimmed by pulling the previous cue's end to the
     snap point - ASS_SNAP_MARGIN_S.

The rewritten file's ORIGINAL is backed up next to it as
dubbing.bilingual.srt.pre_retime before any write.

Usage:
  python retime.py retime <output-root> <name>     # standalone: `cook dub retime`
  stage_assemble imports this module and calls stage_retime() in-process
  (ticket 09: retime is the DEFAULT assembly behavior — see ADR 0002);
  the model loads lazily inside fw_transcriber, in whichever interpreter
  is running the stage (the --python env for the cook-launched paths).

Resource rules: exactly one small/int8/cpu model instance, files
transcribed strictly serially, and nothing larger than a cue pad wav is
ever opened (dub.wav is header-probed for its duration only).
"""
import json
import re
import shutil
import sys
import time
import unicodedata
import wave
import contextlib
from difflib import SequenceMatcher
from pathlib import Path

# ---- calibrated constants (leerob r12 regression, 778/778 starts exact) ----
ANCHOR_LEAD_S = 0.05     # anchor start = first char time - 0.05
MIN_GAP_S = 0.04         # monotonic clamp: start >= prev start + 0.04
END_GAP_S = 0.0          # display end == next start EXACTLY: a 20ms
                         # gap blanks one frame at 30fps — the visible
                         # 'blink' on cue switch (sample90 clip eye-check).
                         # libass renders end==start as seamless handover;
                         # the SRT reader contract is end <= next start.
ASS_SNAP_MARGIN_S = 0.04 # EN grid-snap pre-trim: end = snap point - 0.04
MAP_MIN_COVERAGE = 0.5   # below: ASR transcript untrusted -> no char times
FW_MODEL = "small"       # resource rule: small only
FW_COMPUTE = "int8"      # resource rule: int8 only
FW_DEVICE = "cpu"        # resource rule: cpu only

# --------------------------------------------------------------------------
# SRT I/O (keeps each block's raw text lines; only timestamps are rewritten)
# --------------------------------------------------------------------------

_SRT_BLOCK = re.compile(r"\r?\n\r?\n")


def _parse_ts(x: str) -> float:
    h, m, sec = x.strip().replace(",", ".").split(":")
    return int(h) * 3600 + int(m) * 60 + float(sec)


def fmt_ts(s: float) -> str:
    ms = int(round(s * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    sec, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{sec:02d},{ms:03d}"


def parse_srt(path: Path) -> list[dict]:
    """Parse an SRT into [{num, start, end, lines: [raw text lines]}...].
    Cue number and text lines are preserved verbatim for rewriting."""
    cues = []
    text = Path(path).read_text(encoding="utf-8-sig")
    for block in _SRT_BLOCK.split(text.strip()):
        lines = re.split(r"\r?\n", block)
        ts_idx = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ts_idx is None or ts_idx == 0:
            continue
        a, b = lines[ts_idx].split("-->")
        cues.append({
            "num": lines[:ts_idx],
            "start": _parse_ts(a),
            "end": _parse_ts(b),
            "lines": lines[ts_idx + 1:],
        })
    return cues


def write_srt(path: Path, cues: list[dict]) -> None:
    blocks = []
    for cue in cues:
        head = cue["num"] if cue["num"] else [str(len(blocks) + 1)]
        body = [f"{fmt_ts(cue['start'])} --> {fmt_ts(cue['end'])}"] + cue["lines"]
        blocks.append("\n".join(head + body))
    Path(path).write_text("\n\n".join(blocks) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# Char alignment (pure; unit-tested with fake word timestamps)
# --------------------------------------------------------------------------

def filter_chars(text: str) -> str:
    """Drop punctuation (any unicode P* category) and whitespace; keep
    everything else in order. Both the cue text and the ASR words are
    normalized through this before alignment."""
    return "".join(
        c for c in text
        if not unicodedata.category(c).startswith("P") and not c.isspace()
    )


def build_char_map(text: str, cue_start: float,
                   words: list[tuple[float, float, str]]) -> list[float] | None:
    """Map every filtered char of `text` to an absolute time.

    words = [(start, end, word_text), ...] — the ASR word stream of the
    cue's pad wav (times relative to the wav). The filtered text chars are
    aligned onto the filtered ASR chars with SequenceMatcher; each matched
    char takes the START time of the ASR word containing its counterpart,
    made absolute by + cue_start. Unmatched chars after a matched one
    forward-fill from it; LEADING unmatched chars take the cue start
    (calibrated: leerob cue 59's 'subagent...' latin head got the window
    start, not the first word's 0.44s-later time).

    Returns None when fewer than half the chars matched (coverage < 0.5)
    — the transcript is garble and its times must anchor nothing (leerob
    cue 240: coverage 0.471 rejected; cue 32: 0.500 accepted).
    """
    a = filter_chars(text)
    asr_chars: list[str] = []
    char_word: list[int] = []
    for wi, (_ws, _we, wt) in enumerate(words):
        for ch in filter_chars(wt):
            asr_chars.append(ch)
            char_word.append(wi)
    b = "".join(asr_chars)
    sm = SequenceMatcher(None, a, b, autojunk=False)
    times: list[float | None] = [None] * len(a)
    matched = 0
    for mb in sm.get_matching_blocks():
        matched += mb.size
        for t in range(mb.size):
            times[mb.a + t] = words[char_word[mb.b + t]][0] + cue_start
    if matched < len(a) * MAP_MIN_COVERAGE:
        return None
    # forward fill unmatched from the previous matched char
    last = None
    for i, t in enumerate(times):
        if t is None:
            times[i] = last
        else:
            last = t
    # leading unmatched chars take the cue window start
    for i in range(len(times)):
        if times[i] is None:
            times[i] = cue_start
        else:
            break
    return times  # type: ignore[return-value]


# --------------------------------------------------------------------------
# The two-pass rewrite (pure; unit-tested with a fake char stream)
# --------------------------------------------------------------------------

def _zh_text_of(cue: dict) -> str:
    """The bilingual cue's ZH line (biliteral emits zh first, en second)."""
    return cue["lines"][0] if cue["lines"] else ""


def retime_plan(bil_cues: list[dict], stream: str,
                stream_times: list[float | None],
                dub_dur: float | None) -> list[dict]:
    """Two-pass rewrite. Returns a parallel list of
    {start, end, kind} for each bilingual cue.

    bil_cues   — parsed bilingual cues (their input starts are kept for
                 GAP cues with no word-time coverage)
    stream     — the concatenated filtered-char stream of the dubbing.srt
                 zh texts (the cursor-matching source)
    stream_times — per-char absolute times; None where the source cue's
                 ASR map was rejected (those anchors keep their input start)
    dub_dur    — the dub track duration; the last cue's end = dub_dur -
                 END_GAP_S. None -> fall back to the last input end.
    """
    # ---- pass 1: anchor / dwell / gap, on the raw char stream ----
    cursor = 0
    kinds: list[str] = []
    raw: list[float | None] = []
    for cue in bil_cues:
        g = filter_chars(_zh_text_of(cue))
        if not g:
            kinds.append("DWELL")   # no zh text: cannot anchor, inherit
            raw.append(None)
            continue
        if stream[cursor:cursor + len(g)] == g:
            t = stream_times[cursor]
            # an anchor with no word-time coverage (rejected source map)
            # keeps its input start — counted separately from real anchors
            kinds.append("ANCHOR" if t is not None else "GAP")
            raw.append(t)
            cursor += len(g)
        elif cursor >= len(g) and stream[cursor - len(g):cursor] == g:
            kinds.append("DWELL")   # union repeat: inherits, cursor stays
            raw.append(None)
        else:
            # text drifted from the source stream (hand edit, regen skew):
            # search forward for the anchor so one late cue cannot poison
            # every cue after it; a miss falls back to keeping its start
            found = stream.find(g, cursor, cursor + 500)
            if found >= 0:
                t = stream_times[found]
                kinds.append("ANCHOR" if t is not None else "GAP")
                raw.append(t)
                cursor = found + len(g)
            else:
                kinds.append("GAP")
                raw.append(None)
    starts: list[float] = []
    prev = None
    for cue, kind, r in zip(bil_cues, kinds, raw):
        if kind == "ANCHOR" and r is not None:
            # clamped at zero: a first word earlier than the lead (speech
            # starting <0.05s into the cue) must not produce a negative
            # start — fmt_ts renders negatives as the malformed
            # '-1:59:59,970' (negative divmod artifact)
            v = max(0.0, r - ANCHOR_LEAD_S)
        elif kind == "GAP" or (kind == "ANCHOR" and r is None):
            v = cue["start"]        # no trusted word time: keep what's there
        else:
            v = prev if prev is not None else cue["start"]
        starts.append(v)
        prev = v
    # ---- pass 2: monotonic clamp, ends, EN grid-snap pre-trim ----
    for k in range(1, len(starts)):
        if starts[k] < starts[k - 1] + MIN_GAP_S:
            starts[k] = starts[k - 1] + MIN_GAP_S
    ends: list[float] = []
    for k in range(len(starts)):
        if k + 1 < len(starts):
            nxt = starts[k + 1]
            end = nxt  # exact handover: end == next start (no blank frame)
            # EN grid-snap pre-trim: the next start's centisecond rounding
            # carries (ASS "SS.100" -> libass reads SS.10, ~0.9s early).
            if round((nxt - int(nxt)) * 100) >= 100:
                end = min(end, int(nxt) + 0.10 - ASS_SNAP_MARGIN_S)
        else:
            # no dub.wav to anchor the tail: keep the input end verbatim
            # (a -0.02 here would drift 20ms on every re-run)
            base = dub_dur if dub_dur is not None else bil_cues[k]["end"]
            end = base  # keep verbatim; no subtraction (idempotent re-runs)
        ends.append(end)
    return [{"start": starts[k], "end": ends[k], "kind": kinds[k]}
            for k in range(len(bil_cues))]


def build_stream(zh_cues: list[dict], char_maps: list[list[float] | None],
                 ) -> tuple[str, list[float | None]]:
    """Concatenate the dubbing.srt cues' filtered chars into the cursor-
    matching stream, with the per-char times (None where a cue's map was
    rejected)."""
    chars: list[str] = []
    times: list[float | None] = []
    for cue, cmap in zip(zh_cues, char_maps):
        fc = filter_chars(" ".join(cue["lines"]))
        chars.append(fc)
        times.extend(cmap if cmap is not None else [None] * len(fc))
    return "".join(chars), times


# --------------------------------------------------------------------------
# Transcription (the only non-pure part; the model lives behind this seam)
# --------------------------------------------------------------------------

def fw_available() -> bool:
    """True when faster-whisper is importable in THIS interpreter — the
    probe the assemble integration (ticket 09) consults to decide between
    running retime and degrading to the old proportional-split behavior.
    Importing the package is cheap; the model only loads in fw_transcriber."""
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        return False
    return True


def fw_transcriber():
    """Return transcribe(wav_path) -> [(start, end, word), ...].

    Loads ONE faster-whisper small/int8/cpu model instance (resource
    rule) and transcribes strictly serially. Dies with an actionable
    message when faster-whisper is missing from this interpreter."""
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:
        print("ERROR: faster-whisper is not installed in this interpreter — "
              "cook dub retime needs it for word timestamps "
              "(small/int8/cpu). Install it in the --python environment: "
              "pip install faster-whisper", file=sys.stderr)
        raise SystemExit(2) from e
    model = WhisperModel(FW_MODEL, device=FW_DEVICE, compute_type=FW_COMPUTE)

    def transcribe(wav: Path) -> list[tuple[float, float, str]]:
        segments, _info = model.transcribe(str(wav), word_timestamps=True,
                                           language="zh")
        return [(w.start, w.end, w.word)
                for seg in segments for w in (seg.words or [])]

    return transcribe


def _dub_duration(dub_wav: Path) -> float | None:
    """The dub track's duration from the wav HEADER (no audio data is
    read — dub.wav is large; only its RIFF frame count is needed)."""
    try:
        with contextlib.closing(wave.open(str(dub_wav), "rb")) as w:
            return w.getnframes() / float(w.getframerate())
    except (OSError, wave.Error):
        return None


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Stage entry
# --------------------------------------------------------------------------

def stage_retime(output_root, name: str, transcriber=None) -> dict:
    """The retime stage: build char maps from the pad wavs, rewrite the
    bilingual SRT, back up the original first.

    transcriber is injectable (tests pass a fake; the CLI passes
    fw_transcriber()). Returns the report dict (also written to
    dubbed/_full/_retime_report.json)."""
    root = Path(output_root).resolve()
    work = root / "dubbed" / "_full"
    dubbing_srt = work / "dubbing.srt"
    bilingual_srt = work / "dubbing.bilingual.srt"
    pad_dir = work / "_audio_pad"

    for f in (dubbing_srt, bilingual_srt):
        if not f.is_file() or f.stat().st_size == 0:
            _log(f"ERROR: missing {f} — run a plain 'cook dub assemble' first")
            raise SystemExit(1)
    zh_cues = parse_srt(dubbing_srt)
    bil_cues = parse_srt(bilingual_srt)
    if not zh_cues or not bil_cues:
        _log(f"ERROR: {dubbing_srt.name} or {bilingual_srt.name} parsed empty")
        raise SystemExit(1)

    pad_wavs = [pad_dir / ("cue_%04d.wav" % i) for i in range(len(zh_cues))]
    missing = [w.name for w in pad_wavs if not w.is_file()]
    if missing:
        _log(f"ERROR: {len(missing)} pad wav(s) missing under {pad_dir} "
             f"(first: {missing[0]}) — run 'cook dub assemble' (2b) first")
        raise SystemExit(1)

    _log(f"retime: {len(zh_cues)} source cues, {len(bil_cues)} bilingual cues")
    transcribe = transcriber if transcriber is not None else fw_transcriber()

    # ---- char maps (one model instance, strictly serial per file) ----
    char_maps: list[list[float] | None] = []
    rejected: list[int] = []
    t0 = time.time()
    for i, cue in enumerate(zh_cues):
        words = transcribe(pad_wavs[i])
        cmap = build_char_map(" ".join(cue["lines"]), cue["start"], words)
        char_maps.append(cmap)
        if cmap is None:
            rejected.append(i)
        if (i + 1) % 25 == 0 or i + 1 == len(zh_cues):
            _log(f"  aligned {i + 1}/{len(zh_cues)} "
                 f"({len(rejected)} rejected, {time.time() - t0:.0f}s)")
    maps_cache = work / "_fw_char_maps.json"
    maps_cache.write_text(json.dumps(
        {str(i): [round(t, 3) for t in cm] for i, cm in enumerate(char_maps)
         if cm is not None}, ensure_ascii=False), encoding="utf-8")
    _log(f"  char maps: {len(zh_cues) - len(rejected)}/{len(zh_cues)} usable "
         f"(rejected: {rejected[:8]}{'...' if len(rejected) > 8 else ''}) "
         f"-> {maps_cache.name}")

    # ---- two-pass rewrite ----
    stream, stream_times = build_stream(zh_cues, char_maps)
    plan = retime_plan(bil_cues, stream, stream_times,
                       _dub_duration(work / "dub.wav"))

    backup = bilingual_srt.with_name(bilingual_srt.name + ".pre_retime")
    shutil.copyfile(bilingual_srt, backup)
    _log(f"  backed up original -> {backup.name}")
    for cue, p in zip(bil_cues, plan):
        cue["start"], cue["end"] = p["start"], p["end"]
    write_srt(bilingual_srt, bil_cues)
    _log(f"  rewrote {bilingual_srt.name} "
         f"(start shift median {_median_shift(plan, bil_cues):+.3f}s)")

    from collections import Counter
    counts = Counter(p["kind"] for p in plan)
    report = {
        "ok": True,
        "cues": len(bil_cues),
        "anchors": counts.get("ANCHOR", 0),
        "dwells": counts.get("DWELL", 0),
        "kept_input": counts.get("GAP", 0),
        "maps_rejected": len(rejected),
        "backup": backup.name,
        "output": bilingual_srt.name,
        "char_maps_cache": maps_cache.name,
    }
    (work / "_retime_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    _log(f"retime DONE: {report['anchors']} anchors / {report['dwells']} "
         f"dwells / {report['kept_input']} kept-input "
         f"({report['maps_rejected']} source map(s) rejected)")
    return report


def _median_shift(plan: list[dict], bil_cues: list[dict]) -> float:
    """Median new-vs-old start shift, for the log line."""
    import statistics
    return statistics.median(
        p["start"] - c["start"] for p, c in zip(plan, bil_cues))


# --------------------------------------------------------------------------
# CLI (convention of full_dub.py: <stage> <output-root> <name>)
# --------------------------------------------------------------------------

def _cli_main(argv: list[str]) -> None:
    if len(argv) < 3 or argv[0] != "retime":
        print(__doc__)
        sys.exit(1)
    stage_retime(argv[1], argv[2])


if __name__ == "__main__":
    _cli_main(sys.argv[1:])
