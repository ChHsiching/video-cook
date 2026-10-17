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


def evaluate(srt_text: str, trans_text: str, rate: float) -> dict:
    """Run the three gate checks at the given measured rate.

    Returns a report dict:
      lines, short[], over[], under[]   findings (line numbers are 1-based)
      coverage, total_est_s, total_window_s
      verdict: "pass" | "fail"          fail = coverage out of band, or any
                                        short/over line (under alone = pass)
      advisory_under: bool              True when only under-budget lines
                                        held the report back from a clean pass
    Raises ValueError when the cue/line counts do not pair 1:1."""
    if not (rate > 0):
        raise ValueError(f"rate must be positive (got {rate!r})")

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

    short, over, under = [], [], []
    total_est = total_budget = 0.0
    for i, (budget, zh) in enumerate(zip(budgets, lines), 1):
        chars = count_chars(zh)
        est = chars / rate
        total_est += est
        total_budget += budget
        if syllables(zh) <= SHORT_SYLLS:
            short.append({"line": i, "sylls": syllables(zh),
                          "est_s": round(est, 2), "text": zh})
        if budget > 0:
            factor = est / budget
            if factor > OVER_FACTOR:
                over.append({"line": i, "factor": round(factor, 2),
                             "est_s": round(est, 2),
                             "window_s": round(budget, 2), "text": zh})
            elif factor < UNDER_FACTOR:
                under.append({"line": i, "factor": round(factor, 2),
                              "window_s": round(budget, 2), "text": zh})

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
        "verdict": "fail" if fail else "pass",
        "advisory_under": not fail and bool(under),
    }


def format_report(rep: dict, rate: float, rate_source: str) -> list[str]:
    """Human-readable rendering (cook prints these lines to stderr)."""
    out = [f"rate: {rate:.3f} chars/s ({rate_source})"]
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
