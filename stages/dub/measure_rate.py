"""Measure the REAL speech rate (chars per voiced second) of dub audio.

Why: the 2026-10 reflux run planned the whole translation at an assumed
5.0 chars/s while the synthesized voice actually spoke at 6.93 — 78.6%
coverage, 82 rewritten+resynthesized takes, ~6 hours of rework. This module
turns that measurement into a command so the rate is observed, not assumed.

Inputs are audition pairs: a wav plus the exact text spoken in it. Energy
VAD over streaming block reads (soundfile + numpy — no librosa, no loading
whole files into RAM) measures each wav's speech SPAN — first voiced frame
to last voiced frame, bridging intra-sentence pauses, excluding the take's
leading/trailing silence; the measured rate is total chars / total voiced
seconds across all pairs.

This module is FUNCTIONS ONLY — the CLI lives in cook (`cook dub measure-
rate`), which loads it in-process (soundfile/numpy are the only deps, both
importable by cook's own python, unlike the indextts venv stages).

Char counting is len() of the stripped line, punctuation INCLUDED — the
counting the 2026-10 reflux run's budget math used (预算 = 实测率 x 窗口)
and the convention the calibration below is anchored to. Note this is
deliberately NOT length_gate.syllables: the gate plans durations from
syllable counts, this command counts script characters. If ticket 05 wires
the measured rate into the gate, convert units there, not here.

Calibration: FRAME_MS=30, FLOOR_DB=-50 reproduce the reflux run's hand
measurement on the accepted leerob take set — 361 takes measure 6.931
chars/s against the hand-measured 6.93 (band +/-0.1). Changing either
constant shifts that number; re-validate against real takes before
changing them.
"""
from __future__ import annotations

import math
from pathlib import Path

# VAD frame length. 30 ms is short enough to catch speech onsets without
# letting attack ramps read as silence.
FRAME_MS = 30
# RMS floor in dBFS: frames strictly above this count as voiced. Clean TTS
# output has digital silence (0 -> -inf dB) before/after the speech, so any
# floor in the -40..-55 band separates speech from padding; -50 is the
# calibrated value (see module docstring).
FLOOR_DB = -50.0


def count_chars(text: str) -> int:
    """Spoken line length in 字: len() of the stripped text, punctuation
    included — the reflux budget convention (see module docstring)."""
    return len(text.strip())


def voiced_seconds(path: Path, frame_ms: int = FRAME_MS,
                   floor_db: float = FLOOR_DB) -> float:
    """Length of the speech SPAN in a wav, by energy VAD.

    Streams the file in frame_ms blocks via soundfile (one block in memory
    at a time — safe on any file size), computes per-block RMS in dBFS, and
    returns last_voiced_end - first_voiced_start. Pauses INSIDE the span
    (breaths, phrase gaps) are bridged — a take's speech extent is what a
    human marks in an editor, and what the rate calibration measured. A
    partial tail frame counts pro rata. Returns 0.0 for a fully silent
    file — the caller treats that as an error.
    """
    import numpy as np  # deferred: only this function needs it
    import soundfile as sf

    with sf.SoundFile(str(path)) as f:
        sr = f.samplerate
        block = max(1, int(sr * frame_ms / 1000))
        first = None
        last = 0.0
        cursor = 0.0  # frame position, fractional for a partial tail
        for chunk in f.blocks(blocksize=block, dtype="float32"):
            if chunk.size == 0:
                continue
            rms = float(np.sqrt(np.mean(np.square(chunk, dtype=np.float64))))
            db = 20.0 * math.log10(rms) if rms > 0.0 else -math.inf
            if db > floor_db:
                if first is None:
                    first = cursor
                last = cursor + len(chunk) / block
            cursor += len(chunk) / block
        if first is None:
            return 0.0
        return (last - first) * frame_ms / 1000.0


def pair_segments(root: Path) -> list[tuple[Path, str]]:
    """Pair translations_dub.txt lines with dubbed/_full/_segments/sent_*.wav.

    Line N pairs with sent_NNNN.wav in sorted order — the synth layout's
    contract (full_dub Stage 1 writes one wav per non-empty line, numbered
    from 1). Raises FileNotFoundError / ValueError on missing inputs or a
    count mismatch, with a message naming both counts.
    """
    trans = root / "transcript" / "translations_dub.txt"
    seg_dir = root / "dubbed" / "_full" / "_segments"
    if not trans.is_file():
        raise FileNotFoundError(f"translations file not found: {trans}")
    wavs = sorted(seg_dir.glob("sent_*.wav")) if seg_dir.is_dir() else []
    lines = [l.strip() for l in trans.read_text(encoding="utf-8").splitlines()
             if l.strip()]
    if not wavs:
        raise FileNotFoundError(f"no sent_*.wav under {seg_dir}")
    if len(wavs) != len(lines):
        raise ValueError(
            f"{len(lines)} translation lines vs {len(wavs)} segment wavs — "
            "rerun synth (or fix translations_dub.txt) so they pair 1:1")
    return list(zip(wavs, lines))
