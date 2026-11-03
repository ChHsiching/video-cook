# Changelog

## 0.9.1 — 2026-11-03

Assembly placement & rate-lock (2026-11 Titus run reflux; ADR 0003):

- **synth**: edge-trim every take at publish (-45dB, 80ms margins,
  idempotent) — honest durations downstream; one full-run trim recovered
  173s across 1135 takes.
- **assemble fit pass**: hard-cut residuals beyond the atempo cap to the
  window end + 30ms fade (broadcast ducking) — a take can no longer push
  the NEXT cue late (46 cues up to 0.51s late shipped in 0.9.0).
- **assemble placement invariant**: `placed - start <= 20ms` enforced in
  the placement loop; violation fails the assemble (regression guard).
- **measure-rate --write**: rate sidecar (rate + per-speaker grouping +
  duration_factor + ref md5s) — the provenance chain.
- **gate --rate-file**: gates by the sidecar, per-speaker rates applied
  via speakers.txt; a typed `--rate` now requires `--rate-override`.
- **gate --breath / --breath-min-window** (defaults 0.12s / 1.2s):
  breathing reserve shaved from budgeted windows so a fully-filled budget
  still leaves an audible gap; coverage reports against reserved windows.
- GLOSSARY.md + ADR 0003 added; 260-test suite green with new
  TestRateLockAndBreath.
