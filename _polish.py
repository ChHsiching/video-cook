# -*- coding: utf-8 -*-
"""Apply writing-for-agents + unslop audit fixes to the video-cook branch."""
import io

# ---------- GLOSSARY.md: single definition site, colon form, de-flourish ----------
g = '''# Glossary

Terms coined or fixed by the dub pipeline. Numbers live here and in the
code; ADRs and CHANGELOG cite, never restate.

- **absorption window**: a cue's budget span on the identity timeline, its
  start to the NEXT cue's start (the last cue runs to the video end). The
  original inter-cue pause lives inside it, which is why breathing is a
  budget concern (see ADR 0003).

- **edge trim**: removing leading and trailing true silence (below -45dB)
  from a synthesized take at publish time, keeping an 80ms margin at each
  edge. Downstream stages then measure durations that exclude the
  synthesized pad silence. Idempotent: a take with no edge silence keeps
  its original bytes.

- **fit**: what the assembler does to a take that overruns its absorption
  window. It compresses with atempo (capped), and when a residual survives
  the cap it hard-cuts to the window end with a 30ms fade (see
  `CUT_SLACK_S`/`CUT_FADE_S`). It never stretches and never shifts the
  next cue.

- **placement invariant**: every cue's audio starts at its original cue
  start plus at most `PLACE_TOL_S` (20ms). Enforced inside the assembly
  loop; a violation fails the assemble because the offending take pushed
  the next cue late.

- **rate-lock**: the provenance chain from measurement to gate.
  `measure-rate --write` emits a sidecar (rates, duration_factor, ref
  md5s), and `gate --rate-file` consumes exactly that file. A typed rate
  is the `--rate-override` escape hatch, not the path.

- **breathing reserve**: the seconds shaved from a budgeted window so a
  fully-filled budget still leaves an audible gap before the next cue.
  Flags `--breath` (default 0.12s) and `--breath-min-window` (default
  1.2s, windows at or below it are exempt). Budget-side only; placement
  is unchanged. Defaults are calibration starting points, tune per voice.

- **fit sidecar**: `_segments_fitted/`. The fit pass's products live
  beside the read-only synth cache, so an aborted or misaligned assemble
  cannot corrupt synthesis work.
'''
io.open('GLOSSARY.md', 'w', encoding='utf-8').write(g)

# ---------- ADR 0003: cite don't restate, de-dash, de-flourish, split dense ----------
a = '''# ADR 0003: Assemble owns fit; budgets own breath

Date: 2026-11-03
Status: accepted

## Context

The 2026-11 Titus run shipped a dubbed video where 46 cues started up to
0.51s late (8.8s total). Root cause chain:

1. IndexTTS2 pads every take with 0.1-0.5s of true silence at both edges.
   No pipeline stage removed it, so every downstream duration was
   inflated (one full-run trim recovered 173s across 1135 takes).
2. The fit pass capped atempo at 2.0. Micro-backchannels ("对。", a 0.9s
   take in a 0.22s window, factor 4.1) kept a residual after compression.
3. The assembly clock writes each cue at `max(start, clock)`, so any
   overrun necessarily delayed the NEXT cue's start. No check existed for
   a late next cue: the fit pass validated each cue against its own
   window only, and the review gates audited subtitle content, not audio
   placement.

The same run also exposed the budget side. The char-budget formula
(window × rate) counts the original inter-cue pause as fillable text
space, and the coverage metric rewards filling it. With edge-trimmed
durations, a fully-filled budget ends the take at the next cue's first
sample, with no audible gap.

## Decision

**Fit is the assembler's job, and it terminates in a hard cut.** The
synth stage edge-trims every take at publish time (the cache stays
single-writer and holds durations that exclude the pad silence). The fit
pass then has two moves only: compress (atempo, capped), and when a
residual survives the cap, hard-cut to the window end with a 30ms fade,
the broadcast-ducking shape. A placement invariant runs inside the
assembly loop and fails the assemble on violation. It is unreachable
while the cut exists; it stays as a regression guard.

**Breath is the budget's job, never the assembler's.** Placement always
pads to the next cue's start and never stretches, so the gap the viewer
hears is under-filled budget. The length gate therefore shaves a
breathing reserve from every budgeted window above the exemption line
(GLOSSARY: breathing reserve). Every gap the viewer hears is budget the
translation did not spend.

**Rate-lock closes the provenance chain.** `cook dub measure-rate
--write` emits a sidecar with rates, duration_factor, ref md5s, and
per-speaker grouping; `cook dub gate --rate-file` reads exactly that
file. A typed `--rate` number still works but requires
`--rate-override`, so an assumed rate can no longer pass every gate
silently.

## Consequences

- Cues over budget by more than the atempo cap are CUT, not stretched
  and not shifted. The log names them (`CUT idx N`); best quality still
  means editing the line and re-synthesizing.
- The `_segments/` cache holds edge-trimmed takes, so downstream
  measurements (measure-rate, gates, fit) see durations without the pad
  silence. Trims are idempotent.
- Budgets computed with the reserve are slightly smaller than the raw
  window×rate product; coverage is reported against the reserved
  windows. Translations written to the old formula will read slightly
  under. That is the intended pressure toward breathing room.
'''
io.open('docs/adr/0003-assemble-owns-fit-budgets-own-breath.md', 'w',
        encoding='utf-8').write(a)

# ---------- CHANGELOG: de-dash, cite don't restate, drop rotting claim ----------
c = '''# Changelog

## 0.9.1 (2026-11-03)

Assembly placement and rate-lock (2026-11 Titus run reflux; ADR 0003):

- **synth**: edge-trim every take at publish (-45dB, 80ms margins,
  idempotent), so downstream durations exclude the synthesized pad
  silence. One full-run trim recovered 173s across 1135 takes.
- **assemble fit pass**: hard-cut residuals beyond the atempo cap to the
  window end with a 30ms fade (GLOSSARY: fit). A take can no longer push
  the NEXT cue late; that defect shipped 46 late cues in 0.9.0.
- **assemble placement invariant**: enforced in the placement loop; a
  violation fails the assemble (GLOSSARY: placement invariant).
- **measure-rate --write**: rate sidecar with per-speaker grouping,
  duration_factor, and ref md5s. This is the provenance chain.
- **gate --rate-file**: gates by the sidecar, per-speaker rates applied
  via speakers.txt. A typed `--rate` now requires `--rate-override`.
- **gate --breath / --breath-min-window**: breathing reserve in the
  budget math (GLOSSARY: breathing reserve), so a fully-filled budget
  still leaves an audible gap. Coverage reports against reserved
  windows.
- GLOSSARY.md and ADR 0003 added; TestRateLockAndBreath added.
'''
io.open('CHANGELOG.md', 'w', encoding='utf-8').write(c)

# ---------- README pointer (GLOSSARY reachable) ----------
r = io.open('README.md', encoding='utf-8').read()
if 'GLOSSARY.md' not in r:
    anchor = 'docs/adr/'
    idx = r.find(anchor)
    if idx == -1:
        r = r.rstrip() + ('\n\nTerms coined in `cook dub` output (rate-lock, breathing reserve, '
                          'placement invariant, absorption window) are defined in '
                          '[GLOSSARY.md](GLOSSARY.md); reach for it when a flag or log names one.\n')
    else:
        # append after the ADR line's end
        eol = r.index('\n', idx)
        r = (r[:eol] + '\nTerms coined in `cook dub` output (rate-lock, breathing reserve, '
               'placement invariant, absorption window) are defined in '
               '[GLOSSARY.md](GLOSSARY.md); reach for it when a flag or log names one.' + r[eol:])
    io.open('README.md', 'w', encoding='utf-8').write(r)

# ---------- cook.py strings ----------
p = 'cook.py'
s = io.open(p, encoding='utf-8').read()
reps = [
 ('"measure-rate: speakers.txt has {len(spk_labels)} labels "\n                     f"vs {len(pairs)} pairs — skipping per-speaker grouping"',
  '"measure-rate: speakers.txt has {len(spk_labels)} labels "\n                     f"vs {len(pairs)} pairs; skipping per-speaker grouping"'),
 ('f"rate sidecar written: {sidecar} — gate with "',
  'f"rate sidecar written: {sidecar}. Gate with "'),
 ('''    """Write the rate sidecar (rate-lock): the measured rate(s) plus the
    provenance a bare --rate number cannot carry — duration_factor, ref-wav
    md5s, sample counts. `cook dub gate --rate-file` reads exactly this file,
    so a gate pass is traceable to a measure-rate run instead of a typed
    number (the 2026-11 ruling: assumed rates pass every gate and dub to
    dead air)."""''',
  '''    """Write the rate sidecar (rate-lock): the measured rate(s) plus the
    provenance a bare --rate number cannot carry, namely duration_factor,
    ref-wav md5s, and sample counts. `cook dub gate --rate-file` reads
    exactly this file, so a gate pass is traceable to a measure-rate run
    instead of a typed number (the 2026-11 ruling: assumed rates pass
    every gate and dub to dead air)."""'''),
 ('''    """Read a measure-rate sidecar (rate-lock). Returns (single_rate,
    per_speaker_or_None, rate_source). Dies on a missing/invalid file or a
    non-positive rate — a sidecar that lies is worse than none."""''',
  '''    """Read a measure-rate sidecar (rate-lock). Returns (single_rate,
    per_speaker_or_None, rate_source). Dies on a missing or invalid file
    or a non-positive rate; a sidecar with a wrong rate must fail the
    gate, not pass it."""'''),
 ('f"--rate-file not found: {path} — write one with `cook dub "',
  'f"--rate-file not found: {path}. Write one with `cook dub "'),
 ('''                     help="write a rate sidecar JSON (rate + per-speaker "
                          "rates + duration_factor + ref md5s) for "
                          "`cook dub gate --rate-file` — the rate-lock "
                          "provenance chain")''',
  '''                     help="write a rate sidecar JSON (rate + per-speaker "
                          "rates + duration_factor + ref md5s) for "
                          "`cook dub gate --rate-file`; this is the "
                          "rate-lock provenance chain")'''),
 ('''                     help="measured chars/s — accepted ONLY together with "
                          "--rate-override (explicit escape hatch)")''',
  '''                     help="measured chars/s; accepted ONLY together with "
                          "--rate-override (explicit escape hatch)")'''),
 ('''                     help="breathing reserve shaved from each budgeted "
                          "window (default 0.12s — a fully-filled budget "
                          "still leaves an audible gap before the next cue; "
                          "calibration starting point, tune per voice)")''',
  '''                     help="breathing reserve shaved from each budgeted "
                          "window; a fully-filled budget still leaves an "
                          "audible gap before the next cue (default and "
                          "tuning guidance in GLOSSARY.md)")'''),
 ('''                     help="windows at or below this skip the breathing "
                          "reserve (default 1.2s — backchannel-tight cues "
                          "overlap anyway)")''',
  '''                     help="windows at or below this skip the breathing "
                          "reserve; backchannel-tight cues overlap anyway "
                          "(default in GLOSSARY.md)")'''),
]
for old, new in reps:
    assert old in s, old[:60]
    s = s.replace(old, new)
io.open(p, 'w', encoding='utf-8').write(s)

# ---------- full_dub.py comments ----------
p = 'stages/dub/full_dub.py'
s = io.open(p, encoding='utf-8').read()
reps = [
 ('''# with 0.1-0.5s of true silence at both edges. Untrimmed, every downstream
# duration is inflated — one full-run trim recovered 173s across 1135 takes
# and dropped the over-window count 559 -> 154 before any atempo ran.''',
  '''# with 0.1-0.5s of true silence at both edges. Untrimmed, every downstream
# duration is inflated; one full-run trim recovered 173s across 1135 takes
# and dropped the over-window count 559 -> 154 before any atempo ran.'''),
 ('''# residual that would push the NEXT cue late — the assembly clock writes each
# cue at max(start, clock), so an overrun is paid by whoever speaks next (46
# cues up to 0.51s late shipped in the 2026-11 run before this gate existed).
# The residual is cut to the window end with a short fade: broadcast-ducking
# shape, the cue is over budget and buried under the next speaker anyway.''',
  '''# residual that would push the NEXT cue late: the assembly clock writes
# each cue at max(start, clock), so an overrun delays the next cue's start
# (46 cues up to 0.51s late shipped in the 2026-11 run before this gate
# existed). The residual is cut to the window end with a short fade, the
# broadcast-ducking shape; the cue is over budget and buried under the
# next speaker anyway.'''),
 ('EDGE_TRIM_FLOOR_S = 0.3        # never trim below this; all-silent glitch keeps its shell',
  'EDGE_TRIM_FLOOR_S = 0.3        # never trim below this; an all-silent take is left unchanged'),
 ('CUT_SLACK_S = 0.03             # cut target = window - slack, keeps a hair of air',
  'CUT_SLACK_S = 0.03             # cut target = window - slack; leaves a small silence margin'),
 ('        _edge_trim(out)  # honest duration before any reader (fit/gate/assemble)',
  '        _edge_trim(out)  # pad silence removed before any reader (fit/gate/assemble)'),
 ('''    """Trim leading/trailing true silence from a take, in place. Runs right
    after the atomic publish inside the synth loop, so the cache stays
    single-writer; mid-stream silence is untouched (areverse trick trims the
    tail with the same filter that trims the head). On ffmpeg failure or an
    absurd result the original keeps — a padded take is a soft defect the fit
    pass absorbs; a missing one is not."""''',
  '''    """Trim leading/trailing true silence from a take, in place. Runs right
    after the atomic publish inside the synth loop, so the cache stays
    single-writer; mid-stream silence is untouched (areverse trick trims the
    tail with the same filter that trims the head). On ffmpeg failure or an
    absurd result the original is kept. A padded take is a soft defect the
    fit pass absorbs; a missing one is not."""'''),
 ('''        # idempotence guard: replace only when the trim actually removed
        # edge silence. Nothing removed -> keep the original bytes (no
        # re-encode, no codec-boundary frame drift); absurdly short -> the
        # take was silence-shell glitch, keep it too (fit pass absorbs).''',
  '''        # idempotence guard: replace only when the trim actually removed
        # edge silence. Nothing removed -> keep the original bytes (no
        # re-encode, no codec-boundary frame drift). If the result is
        # absurdly short the take was all silence, a synth glitch; keep the
        # original (fit pass absorbs).'''),
 ('''        # Above the atempo cap a residual survives and would push the NEXT
        # cue late (clock writes at max(start, clock)). Cut to the window end
        # with a fade; the placement gate below holds the invariant.''',
  '''        # Above the atempo cap a residual survives and would push the NEXT
        # cue late (clock writes at max(start, clock)). Cut to the window
        # end with a fade; the placement gate below holds the invariant.'''),
 ('''                    log(f"    CUT idx{idx}: factor {factor:.2f} beyond atempo cap — "
                        f"hard cut to {d:.2f}s (window {window:.2f}s); edit the line "
                        f"and re-synthesize for best quality")''',
  '''                    log(f"    CUT idx{idx}: factor {factor:.2f} beyond atempo cap; "
                        f"hard cut to {d:.2f}s (window {window:.2f}s). Edit the line "
                        f"and re-synthesize for best quality")'''),
 ('''            log(f"  ERROR: placement invariant violated — {len(late)} cue(s) would "
                f"start late (a take overran its window past the next cue's start):")''',
  '''            log(f"  ERROR: placement invariant violated: {len(late)} cue(s) would "
                f"start late (a take overran its window past the next cue's start):")'''),
]
for old, new in reps:
    assert old in s, old[:60]
    s = s.replace(old, new)
io.open(p, 'w', encoding='utf-8').write(s)

# ---------- length_gate.py comments ----------
p = 'stages/dub/length_gate.py'
s = io.open(p, encoding='utf-8').read()
reps = [
 ('''# budget math, so a fully filled window lands speech hard against the next cue's first sample.
# The reserve shaves the BUDGET (never the placement: the assembler still
# pads to the next cue's start, so an under-filled window breathes by the
# unshaved remainder). Windows at or below the exemption are backchannel-
# tight cues where the next voice overlaps anyway. Defaults are CLI flags
# on `cook dub gate` — a starting calibration, not a constant of nature.''',
  '''# budget math, so a fully filled window ends the take at the next cue's
# first sample. The reserve shaves the BUDGET (never the placement: the
# assembler still pads to the next cue's start, so an under-filled window
# breathes by the unshaved remainder). Windows at or below the exemption
# are backchannel-tight cues where the next voice overlaps anyway.
# Defaults are CLI flags on `cook dub gate`; they are starting
# calibrations, tune per voice.'''),
 ('''    speakers/rates: optional per-cue speaker labels (aligned to cues) and a
    label->chars/s map (a measure-rate sidecar's per_speaker block) — each
    cue gates by its own speaker's rate, falling back to `rate` per label.''',
  '''    speakers/rates: optional per-cue speaker labels (aligned to cues) and a
    label->chars/s map (a measure-rate sidecar's per_speaker block). Each
    cue gates by its own speaker's rate, falling back to `rate` per label.'''),
 ('''        raise ValueError(
            f"speakers.txt has {len(speakers)} labels vs {len(lines)} "
            "translation lines — per-speaker gating needs the 1:1 pairing")''',
  '''        raise ValueError(
            f"speakers.txt has {len(speakers)} labels vs {len(lines)} "
            "translation lines; per-speaker gating needs the 1:1 pairing")'''),
 ('    out = [f"rate: {rate:.3f} chars/s ({rate_source})"]',
  '    out = [f"rate: {rate:.3f} chars/s ({rate_source})"]'),  # no-op keep
]
for old, new in reps:
    if old == new:
        continue
    assert old in s, old[:60]
    s = s.replace(old, new)
io.open(p, 'w', encoding='utf-8').write(s)

print('video-cook polish applied')
