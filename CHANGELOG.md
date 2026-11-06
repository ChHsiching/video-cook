# Changelog

## 0.9.3 (2026-11-07)

- **dub merge --expect**: pin the merge address to content before writing.
  `--expect SUBSTR` (repeatable, paired with `--group` in flag order,
  case-insensitive) requires the group's first zh line to contain the
  substring or the merge refuses, quoting the line actually sitting at
  that number; empty substrings refuse (they pin nothing). Every merge
  renumbers the file, so a group computed against the pre-merge
  numbering addresses a legal but wrong pair that passes every existing
  invariant — the 2026-11 incident merged exactly that and needed hand
  surgery to undo.

## 0.9.2 (2026-11-04)

- **help**: --breath/--breath-min-window state their defaults inline
  (0.12s / 1.2s). 0.9.1's wheel shipped a GLOSSARY.md pointer instead,
  but the wheel carries no GLOSSARY.md; --help is the CLI's environment
  and must be self-contained for pip users.

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
