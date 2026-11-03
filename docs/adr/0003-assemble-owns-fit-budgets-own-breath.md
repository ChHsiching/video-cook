# ADR 0003: Assemble owns fit; budgets own breath

Date: 2026-11-03
Status: accepted

## Context

The 2026-11 Titus run shipped a dubbed video where 46 cues started up to
0.51s late (8.8s total). Root cause chain:

1. IndexTTS2 pads every take with 0.1-0.5s of true silence at both edges —
   no pipeline stage removed it, so every downstream duration was inflated
   (one full-run trim recovered 173s across 1135 takes).
2. The fit pass capped atempo at 2.0; micro-backchannels ("对。" — a 0.9s
   take in a 0.22s window, factor 4.1) kept a residual after compression.
3. The assembly clock writes each cue at `max(start, clock)` — an overrun
   is physically impossible to hide, so the residual was paid by the NEXT
   cue starting late. No check existed for "the next cue starts late":
   the fit pass validated each cue against its own window only, and the
   review gates audited subtitle content, not audio placement.

The same run also exposed the budget side: the char-budget formula
(window × rate) counts the original inter-cue pause as fillable text
space, and the coverage metric pushes fills — with edge-trimmed honest
durations, a fully-filled budget lands speech hard against the next cue's
first sample with no audible gap.

## Decision

**Fit is the assembler's job, and it terminates in a hard cut.** The synth
stage edge-trims every take at publish time (the cache stays single-writer
and holds honest durations). The fit pass then has two moves only:
compress (atempo, capped), and when a residual survives the cap, hard-cut
to the window end with a 30ms fade — the broadcast-ducking shape. A
placement invariant (`placed - start <= 20ms`) runs inside the assembly
loop and fails the assemble on violation: unreachable while the cut
exists, a regression guard forever after.

**Breath is the budget's job, never the assembler's.** Placement always
pads to the next cue's start and never stretches; a gap can only be
guaranteed by leaving text out of the pause. The length gate therefore
shaves a breathing reserve (`--breath`, default 0.12s, exempting windows
at or below `--breath-min-window`, default 1.2s — calibration starting
points, not constants of nature) from every budgeted window before the
math. The assembler is forbidden from inserting artificial gaps.

**Rate-lock closes the provenance chain.** `cook dub measure-rate --write`
emits a sidecar (rate(s) + duration_factor + ref md5s + per-speaker
grouping); `cook dub gate --rate-file` reads exactly that file. A typed
`--rate` number still works but requires `--rate-override` — the escape
hatch is explicit and logged, so an assumed rate can no longer pass every
gate silently.

## Consequences

- Cues over budget by more than the atempo cap are CUT, not stretched and
  not shifted: their audio ends at the window boundary. The log names
  them (`CUT idx N`) — best quality still means editing the line and
  re-synthesizing.
- The `_segments/` cache holds edge-trimmed takes: downstream measurements
  (measure-rate, gates, fit) see honest durations. Trims are idempotent —
  a take with no edge silence keeps its original bytes.
- Budgets computed with the reserve are slightly smaller than the raw
  window×rate product; coverage is reported against the reserved windows.
  Translations written to the old formula will read slightly under —
  that is the intended pressure toward breathing room.
