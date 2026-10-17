# ADR 0002: Retime is the default assembly behavior

- Status: Accepted (2026-10-17, reflux 2026-10, ticket 09 / issue #20)
- Scope: `cook dub assemble` (and `cook dub full`'s assemble step);
  the standalone `cook dub retime` command from ticket 08 is unchanged

## Context

The assemble stage burns subtitles generated on the "original clock":
each cue's start is the shorten/merge proportional-split estimate. The
dubbed audio, however, is the synthesized take actually placed at that
start — on a 45-minute lecture (the reflux run) the estimate and the
speech drifted apart by up to **17.7 s**, and the resulting r1 release
was rejected at ear-gate. Ticket 08 built the fix as a standalone stage
(`cook dub retime`: faster-whisper word timestamps on the clean
`_audio_pad` tracks → SequenceMatcher char anchors → two-pass rewrite of
`dubbing.bilingual.srt`), calibrated bit-exact against the r12 accepted
output.

A correct step that the pipeline must REMEMBER to run is a step that will
eventually be skipped — the same lesson as ticket 05's measured-rate gate:
the default path has to be the correct path.

## Decision

`stage_assemble` runs retime **by default**, in-process, between the
biliteral build of `dubbing.bilingual.srt` and the ASS build — so the
burned subtitles always carry the retimed starts:

1. **Default (on).** After biliteral, `full_dub` loads
   `stages/dub/retime.py` as a module (no subprocess hop — same process,
   model loading stays retime's own behind its `fw_transcriber` seam) and
   calls `stage_retime(root, name)`. The rewritten file feeds the ASS
   build directly.
2. **`--no-retime`.** Explicit opt-out that restores the old
   proportional-split behavior, with a warning in the log. Legal on the
   assemble stage only (same contract as `--keep-subs`).
3. **Degraded.** When faster-whisper is not importable in the running
   interpreter (the `--python` venv), assemble skips retime, logs the
   explicit warning「字幕按比例摊开（旧行为），长句可能漂移」plus the
   install hint, and continues — a missing optional dependency must not
   take the whole encode down.
4. **`--keep-subs` never retimes.** Its on-disk subtitle files may hold
   post-gate hand edits; a retime rewrite would wipe them (the exact
   failure mode `--keep-subs` exists to prevent).

The state is written to `dubbed/_full/_assemble_retime.json`
(`applied` / `degraded` / `skipped` + reason) and merged into
`cook dub assemble`'s JSON result as the `retime` field, so agents can
gate on it without scraping logs. The standalone `cook dub retime`
remains for reruns and debugging; retime is idempotent (fixed-point on
its own output), so the two entry points cannot fight.

## Consequences and costs

- **Assemble now needs faster-whisper for full fidelity (accepted).**
  Without it the run still completes — degraded, loudly. Operators who
  want aligned subtitles install faster-whisper into the `--python` env.
- **Assemble runtime grows by the transcription pass** (one small/int8/cpu
  model, serial per cue pad wav — resource rules live in retime.py and
  are unchanged). This is the cost of correct-by-default; `--no-retime`
  buys it back for smoke runs.
- **The retimed file is the assemble product.** Anything downstream of
  `dubbing.bilingual.srt` (ASS burn, cloud-srt copies of the merged
  sides — which are NOT retimed, they carry the original-clock zh/en
  merged cues) sees the rewritten starts. The `.pre_retime` backup keeps
  the pre-rewrite original next to it.
- **`cook dub full` gets no opt-out flag** (scope: `assemble` only);
  full runs run retime. Add the passthrough later if a real need shows up.

## Verification of this change

- Unit (`test_cook.py`, zero network / zero model / zero real ffmpeg —
  fake subprocess + injected retime double): default path calls retime
  once and the ASS carries the retimed starts; `--no-retime` produces the
  old behavior's ASS and warns; missing faster-whisper degrades with the
  explicit warning and marker; `--keep-subs` never calls retime and hand
  edits survive; the CLI argv contract (`--no-retime` honored, rejected
  outside assemble); cook passes the flag through and merges
  `retime: applied|degraded|skipped` into the JSON payload.
- Acceptance on real data (per ticket): the default path's product
  subtitle starts match the standalone `cook dub retime` command's output
  on the leerob run (R8's chain is bit-exact against r12, 778/778 starts).
