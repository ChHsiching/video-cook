"""Pre-synth length gate for the dub translation — pure arithmetic, no audio.

Reads transcript/<name>.en.full.srt (cue windows) + transcript/translations_dub.txt
(one Chinese line per cue) and judges, per cue, against the MEASURED speech
rate (chars per voiced second — see stages/dub/measure_rate.py):

  1. short-line trap: <= 8 syllables. IndexTTS2 renders standalone short
     lines at narration pace (observed ~2.6 syll/s vs 4.2-5.5 normal) —
     shorter is SLOWER. Rewrite fuller or let build_merge group it.
  2. budget miss: each cue's char budget is its window (cue start to the next
     cue's start — window + following pause). The identity assembler lands
     every cue's audio in that window with a per-cue atempo, so a factor far
     from 1.0 is not a frozen frame anymore — it is a sentence whose text is
     wrong for its window (factor > 1.5: audibly rushed after atempo, cut it
     back; factor < 0.6: seconds of dead air in the window, fill it).
  3. coverage total: sum of estimated speech / sum of windows. The per-line
     checks say nothing about the whole — a uniformly thin translation passes
     every line check and still dubs to long silences. Healthy band 90-105%;
     below 85% or above 112% fails.

UNITS — the ticket-05 closeout (rate in, assumptions out). The measured
rate from `cook dub measure-rate` is CHARS per voiced second where chars =
len(stripped line), punctuation INCLUDED (measure_rate.count_chars — the
convention the 6.93 calibration on the 361 reflux takes is anchored to).
This gate estimates est_s = count_chars(line) / rate — the same counting,
so the arithmetic is the reflux budget math (预算 = 实测率 × 窗口) inverted
and factor = est_s/window is dimensionless in that basis. The module's old
internal estimate (bucketed 3.0/4.2/4.9 syll/s planning rates — in effect
an assumed ~5 chars/s) is REMOVED: it is exactly the assumed-rate path
that let the 2026-10 run's thin translation pass (assumed 5.0 vs measured
6.93 → 78.6% coverage, 82 takes resynthesized, ~6h rework).

syllables() survives ONLY for the short-line trap (an IndexTTS2 rendering
property of the line, not a duration estimate). count_chars() must stay in
lockstep with measure_rate.count_chars — test_cook.py trips on any drift.

This module is FUNCTIONS ONLY — the CLI lives in cook (`cook dub gate
<output_root> <name> --rate ... | --self-measure`), which loads it
in-process (no audio deps needed). Verdict semantics: exit 0 = pass or
advisory-only (under-budget lines alone); exit 1 = short lines, over-budget
lines, or coverage outside 0.85-1.12.
"""
from __future__ import annotations

import re

# Budget-miss bands on the per-cue factor est/window. 1.5 is where atempo
# starts sounding rushed; 0.6 leaves >40% of the window silent.
OVER_FACTOR = 1.5
UNDER_FACTOR = 0.6
# Coverage total: healthy 0.90-1.05 (printed band), hard fail outside
# 0.85-1.12.
COVERAGE_MIN = 0.85
COVERAGE_MAX = 1.12
HEALTHY_MIN = 0.90
HEALTHY_MAX = 1.05
# IndexTTS2's standalone short-line threshold (narration-pace trap).
SHORT_SYLLS = 8
# Breathing reserve (2026-11 Titus run): the budget formula window×rate
# counts the original inter-cue pause as fillable text space, and the
# coverage metric rewards filling it. With edge-trimmed durations a fully
# filled budget ends the take at the next cue's first sample. The
# reserve shaves the BUDGET (never the placement: the assembler still
# pads to the next cue's start, so an under-filled window breathes by the
# unshaved remainder). Windows at or below the exemption are backchannel-
# tight cues where the next voice overlaps anyway. Defaults are CLI flags
# on `cook dub gate`; they are starting calibrations, tune per voice.
BREATH_MARGIN_S = 0.12
BREATH_MIN_WINDOW_S = 1.2

_CUE_RE = re.compile(r"(\d+)\n([\d:,]+) --> ([\d:,]+)\n(.*?)\n", re.S)


def syllables(line: str) -> int:
    """Line length in syllables: CJK chars 1:1, latin words by vowel groups.
    Used ONLY by the short-line trap — never for duration estimation."""
    syl = len(re.findall(r"[\u4e00-\u9fff]", line))
    for w in re.findall(r"[A-Za-z]+", line):
        syl += max(1, len(re.findall(r"[aeiouAEIOU]+", w)))
    return syl


def count_chars(text: str) -> int:
    """Line length in the rate's unit: len() of the stripped text,
    punctuation included — deliberately the SAME counting as
    measure_rate.count_chars, which the chars/s rate is calibrated in.
    Keeping a second copy (rather than importing across stage modules)
    lets this module stay import-light; test_cook.py asserts the two
    agree so they cannot drift apart."""
    return len(text.strip())


def sec(ts: str) -> float:
    h, m, rest = ts.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def evaluate(srt_text: str, trans_text: str, rate: float, *,
             speakers: list[str] | None = None,
             rates: dict[str, float] | None = None,
             breath_s: float | None = None,
             breath_min_window_s: float | None = None) -> dict:
    """Run the three gate checks at the given measured rate.

    speakers/rates: optional per-cue speaker labels (aligned to cues) and a
    label->chars/s map (a measure-rate sidecar's per_speaker block) — each
    cue gates by its own speaker's rate, falling back to `rate` per label.
    breath_s/breath_min_window_s: the breathing reserve (see BREATH_*
    above) — windows longer than the exemption are budgeted at
    window-breath, so a fully-filled budget still leaves an audible gap
    before the next cue.

    Returns a report dict:
      lines, short[], over[], under[]   findings (line numbers are 1-based)
      coverage, total_est_s, total_window_s   coverage is over the
                                        breath-adjusted effective windows
      verdict: "pass" | "fail"          fail = coverage out of band, or any
                                        short/over line (under alone = pass)
      advisory_under: bool              True when only under-budget lines
                                        held the report back from a clean pass
    Raises ValueError when the cue/line counts do not pair 1:1."""
    if not (rate > 0):
        raise ValueError(f"rate must be positive (got {rate!r})")
    if rates and any(not (r > 0) for r in rates.values()):
        bad = [k for k, r in rates.items() if not (r > 0)]
        raise ValueError(f"per-speaker rates must be positive (got {bad!r})")
    breath = BREATH_MARGIN_S if breath_s is None else breath_s
    breath_min = (BREATH_MIN_WINDOW_S if breath_min_window_s is None
                  else breath_min_window_s)

    spans = [(sec(m.group(2)), sec(m.group(3)))
             for m in _CUE_RE.finditer(srt_text)]
    # pause after each cue — part of that cue's window on the identity clock
    # (the assembler's window is cue start to next cue's start); 0 for the last
    gaps = [(spans[i + 1][0] - spans[i][1]) if i + 1 < len(spans) else 0.0
            for i in range(len(spans))]
    budgets = [(e - s) + g for (s, e), g in zip(spans, gaps)]
    lines = [l.strip() for l in trans_text.splitlines() if l.strip()]
    if len(budgets) != len(lines):
        raise ValueError(
            f"{len(budgets)} srt cues vs {len(lines)} translation lines — "
            "rerun after aligning (make_zh_dub_srt reports the same counts)")
    if speakers is not None and len(speakers) != len(lines):
        raise ValueError(
            f"speakers.txt has {len(speakers)} labels vs {len(lines)} "
            "translation lines — per-speaker gating needs the 1:1 pairing")

    short, over, under = [], [], []
    total_est = total_budget = 0.0
    for i, (budget, zh) in enumerate(zip(budgets, lines), 1):
        cue_rate = rate
        if rates and speakers:
            cue_rate = rates.get(speakers[i - 1], rate)
        eff = budget - breath if budget > breath_min else budget
        chars = count_chars(zh)
        est = chars / cue_rate
        total_est += est
        total_budget += eff
        if syllables(zh) <= SHORT_SYLLS:
            short.append({"line": i, "sylls": syllables(zh),
                          "est_s": round(est, 2), "text": zh})
        if eff > 0:
            factor = est / eff
            if factor > OVER_FACTOR:
                over.append({"line": i, "factor": round(factor, 2),
                             "est_s": round(est, 2),
                             "window_s": round(eff, 2), "text": zh})
            elif factor < UNDER_FACTOR:
                under.append({"line": i, "factor": round(factor, 2),
                              "window_s": round(eff, 2), "text": zh})

    coverage = total_est / total_budget if total_budget else 0.0
    fail = (coverage < COVERAGE_MIN or coverage > COVERAGE_MAX
            or bool(short) or bool(over))
    return {
        "lines": len(lines),
        "short": short,
        "over": over,
        "under": under,
        "coverage": round(coverage, 4),
        "total_est_s": round(total_est, 2),
        "total_window_s": round(total_budget, 2),
        "breath_s": breath,
        "breath_min_window_s": breath_min,
        "per_speaker": rates or None,
        "verdict": "fail" if fail else "pass",
        "advisory_under": not fail and bool(under),
    }


def format_report(rep: dict, rate: float, rate_source: str) -> list[str]:
    """Human-readable rendering (cook prints these lines to stderr)."""
    out = [f"rate: {rate:.3f} chars/s ({rate_source})"]
    if rep.get("per_speaker"):
        ps = ", ".join(f"{k}={v:.2f}" for k, v in rep["per_speaker"].items())
        out.append(f"per-speaker rates: {ps}")
    if rep.get("breath_s"):
        out.append(f"breathing reserve: {rep['breath_s']:.2f}s shaved from "
                   f"windows > {rep.get('breath_min_window_s', 0):.2f}s "
                   "(budget-side only; placement still pads to the next cue)")
    if rep["short"]:
        out.append(f"short-line trap (<= {SHORT_SYLLS} syllables, "
                   "IndexTTS2 narration pace):")
        for r in rep["short"]:
            out.append(f"  line {r['line']}: {r['sylls']} sylls "
                       f"(~{r['est_s']:.1f}s) | {r['text'][:40]}")
    if rep["over"]:
        out.append(f"MUST FIX — over budget (est/window > {OVER_FACTOR}x; "
                    "atempo will sound rushed):")
        for r in rep["over"]:
            out.append(f"  line {r['line']}: {r['factor']:.2f}x "
                       f"(~{r['est_s']:.1f}s vs {r['window_s']:.1f}s window) "
                       f"| {r['text'][:40]}")
    if rep["under"]:
        out.append(f"fill these — under budget (est/window < "
                   f"{UNDER_FACTOR}x; seconds of dead air):")
        for r in rep["under"]:
            out.append(f"  line {r['line']}: {r['factor']:.2f}x "
                       f"(window {r['window_s']:.1f}s) | {r['text'][:40]}")
    out.append(f"coverage: {rep['total_est_s']:.0f}s est / "
               f"{rep['total_window_s']:.0f}s windows = "
               f"{rep['coverage'] * 100:.1f}% "
               f"(healthy {HEALTHY_MIN * 100:.0f}-{HEALTHY_MAX * 100:.0f}%)")
    cov = rep["coverage"]
    if cov < COVERAGE_MIN:
        out.append(f"length gate FAIL: coverage {cov * 100:.0f}% < "
                   f"{COVERAGE_MIN * 100:.0f}% — the translation is uniformly "
                   "thin; every window gets dead air. Expand to the char "
                   "budget (SKILL.md Step 3; REFERENCE.md 'Filling the char "
                   "budget').")
    elif cov > COVERAGE_MAX:
        out.append(f"length gate FAIL: coverage {cov * 100:.0f}% > "
                   f"{COVERAGE_MAX * 100:.0f}% — the whole script is over "
                   "budget; trim filler, not content.")
    if rep["verdict"] == "pass" and not rep["advisory_under"]:
        out.append(f"length gate PASS: {rep['lines']} lines, "
                   f"coverage {cov * 100:.1f}%, "
                   f"none under {SHORT_SYLLS} syllables")
    elif rep["verdict"] == "pass":
        out.append(f"length gate: advisory only ({len(rep['under'])} "
                   "under-budget lines) — proceeding is reasonable; the "
                   "assembler pads the shortfall with silence")
    else:
        out.append(f"length gate FAIL: {len(rep['short'])} short, "
                   f"{len(rep['over'])} over-budget — "
                   "rewrite these lines, then rerun")
    return out
