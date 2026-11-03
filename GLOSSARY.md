# Glossary

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
