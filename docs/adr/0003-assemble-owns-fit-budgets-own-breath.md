# ADR 0003: Assemble owns fit; budgets own breath

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
