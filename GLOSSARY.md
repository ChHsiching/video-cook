# Glossary

- **absorption window** — a cue's budget span on the identity timeline:
  its start to the NEXT cue's start (the last cue runs to the video end).
  The original inter-cue pause lives inside it, which is why breathing is
  a budget concern (see ADR 0003).

- **edge trim** — removing true leading/trailing silence (< -45dB) from a
  synthesized take at publish time, keeping an 80ms margin at each edge.
  Makes every downstream duration honest; idempotent (a take with no edge
  silence keeps its original bytes).

- **fit** — the assembler's compression ladder for a take that overruns
  its absorption window: atempo (capped), then a hard cut to the window
  end with a 30ms fade. Never stretch, never shift the next cue.

- **placement invariant** — every cue's audio starts at its original cue
  start plus at most 20ms. Enforced inside the assembly loop; a violation
  fails the assemble (a take overran and the next cue would start late).

- **rate-lock** — the provenance chain from measurement to gate:
  `measure-rate --write` emits a sidecar (rates + duration_factor + ref
  md5s), `gate --rate-file` consumes exactly that file. A typed rate is
  the `--rate-override` escape hatch, not the path.

- **breathing reserve** — the seconds shaved from a budgeted window
  (default 0.12s on windows above the exemption line) so a fully-filled
  budget still leaves an audible gap before the next cue. Budget-side
  only; placement is unchanged.

- **fit sidecar** — `_segments_fitted/`: the fit pass's products live
  beside the read-only synth cache, so an aborted or misaligned assemble
  can never corrupt hours of synthesis work.
