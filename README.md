# video-cook

Deterministic executor for the [video-cooking](https://github.com/ChHsiching/video-cooking-skill) skill pipeline. Wraps yt-dlp, whisperX, and ffmpeg into subcommands that assemble correctly every time — so the skill docs can stay short and the agent never hand-assembles a long command.

**This is the hands, not the brain.** Skills decide *what* to do, *when*, and handle creative work (translation, copywriting). cook only executes the deterministic parts and verifies the expected outputs exist.

## Why

The video-cooking / video-download / video-subtitle skill trio started as natural-language docs that told the agent which yt-dlp / ffmpeg / whisperx commands to run. Three classes of bugs kept recurring:

1. **Shell escaping traps** — Windows paths with `C:` break ffmpeg's `ass` filter; backslashes get eaten by PowerShell; stdout redirects silently swallow downloads.
2. **Hand-assembly drift** — the agent forgets a flag (`--convert-thumbnails jpg`), picks the wrong template variable, renames the wrong file.
3. **No mechanical completion check** — "the agent feels done" is not a Done criterion. Real runs shipped without `cover.jpg`, without `cloud-srt/`, with translation drift the agent couldn't see.

cook fixes all three by being the single place where the commands are assembled. The skill docs shrink to a pipeline skeleton; the completion criteria become "cook exit 0".

## Install

**Option 1: One-liner via uv** (recommended — uv handles Python interpreter + isolated env + PATH wiring automatically, no pip/venv knowledge needed):

```bash
# Linux / macOS
curl -LsSf https://github.com/ChHsiching/video-cook/releases/latest/download/install.sh | sh
# Windows (PowerShell)
irm https://github.com/ChHsiching/video-cook/releases/latest/download/install.ps1 | iex
```

The installer uses [uv](https://astral.sh/uv/) to install `video-cook[all]` as an isolated tool (~2GB — pulls whisperx + torch). After install, open a new shell and run `cook doctor`.

**Option 2: pip directly**:

```bash
pip install video-cook[all]       # yt-dlp + whisperx + cook itself
# or pick what you need:
pip install video-cook[download]   # for `cook download`
pip install video-cook[transcribe] # for `cook transcribe`
pip install video-cook[subtitle]   # word-boundary cutting in `cook subtitles`
```

ffmpeg and Node.js must be on PATH separately (cook can't pip-install those).

## Subcommands

| Command | What it does | Replaces (manual steps) |
|---|---|---|
| `cook doctor` | Check environment (ffmpeg/node/yt-dlp/whisperx/torch+CUDA/speechbrain Windows-guard) | Skill's "Environment reuse" prose |
| `cook download <url>` | yt-dlp download + cookie negotiation + thumbnail rename + ffprobe verify | video-download Steps 1-3 |
| `cook extract <root> <name>` | ffmpeg 16kHz mono WAV extraction | video-subtitle Step 1 |
| `cook transcribe <root> <name>` | whisperX transcription, auto-detects CUDA, foreground by default (`--detach` opts out) | video-subtitle Step 2 |
| `cook subtitles <root> <name>` | shorten -> merge-short -> biliteral -> ASS + cloud-srt in one shot | video-subtitle Step 4 + cloud-srt |
| `cook burn <root> <name>` | ffmpeg subtitle burning, foreground by default (`--detach` opts out), subprocess list-form | video-subtitle Step 5 |
| `cook cover <root> <name>` | Place `cover.jpg` in `cooked/` (reuses raw thumbnail) | video-subtitle Step 6 cover task |
| `cook show-source <root> <name>` | Extract key fields (title/uploader/links/description) from source.json | (new — surfaces the source context for translation + upload metadata) |
| `cook verify-align <root> <name>` | DP-align `en.srt` vs `translations.txt`, catch missing/drifted translations | (new — no prior equivalent) |
| `cook verify-shipment <root> <name>` | Check the full release set exists; exit 0 = ready to ship | (new — no prior equivalent) |
| `cook dub <stage> <root> <name> --python <venv>` | Dub stages under the IndexTTS2 venv: `separate` (Demucs vocal split), `verify` (dub shipment check), or `full` (synth + assemble in sequence) | video-dubbing Steps 1-7 |
| `cook dub synth <root> <name> --python <venv>` | Stage 1: IndexTTS2 synthesis -> `dubbed/_full/_segments/` (supervisor scans all cues ascending; skip-if-exists resumes). `--workers N` spawns N-1 band workers (descending cue order, evenly split bands) beside the unchanged supervisor; `--stagger S` spaces each worker's model load by k*S seconds (IO-thrash guard). Concurrent writers of the same cue publish atomically — no interleaved/corrupt `sent_NNNN.wav` | video-dubbing Step 4 |
| `cook dub assemble <root> <name> --python <venv>` | Stage 2: identity-timeline assembly (video never re-timed) -> `cooked/<name>.dubbed.mp4`; subtitle retime runs by default (ADR 0002), `--no-retime` keeps the old proportional-split starts; `--keep-subs` reuses hand-edited subtitle files in `dubbed/_full/` | video-dubbing Steps 5-7 |
| `cook dub retime <root> <name> --python <venv>` | Rewrite `dubbing.bilingual.srt` starts onto the dubbed speech's real word times (faster-whisper word timestamps on the clean per-cue pad wavs); backs up the original as `.pre_retime` | (new — the ear-gate retime pass, also run inside assemble) |
| `cook dub measure-rate [--wav f.wav --text "…" \| --from-segments ROOT NAME]` | Measure the REAL speech rate (chars per voiced second) of an audition take or a whole synthesized run via energy VAD; `--json` for the machine-readable report | (new — measured, not assumed) |
| `cook dub gate <root> <name> --rate CHARS_PER_SEC` | Pre-synth length gate on `translations_dub.txt` computed against the MEASURED rate — refuses to run without one (`--rate` from `cook dub measure-rate`, or `--self-measure` from synth's own takes); `--json` for the machine-readable verdict | (new — refuses assumed rates) |
| `cook dub merge <root> <name> --group "a,b,c"` | Merge cue groups in `en.full.srt` + `translations_dub.txt` in lockstep (`--group` repeatable, 1-based consecutive lines); refuses to write unless the zh/en pairing invariants hold, backs up both files as `*.v1` | hand-editing the SRT to merge cues (one side alone desynchronizes zh/en) |

Every command prints a JSON object on stdout (machine-readable; agents parse this) and human-readable progress on stderr. Exit codes are meaningful: 0 = done criterion passed, non-zero = it didn't.

## How skills use cook

cook is the deterministic executor for the [video-cooking](https://github.com/ChHsiching/video-cooking-skill) skill pipeline. Skills call cook as a subprocess and branch on its exit code; the router (`video-cooking`) calls `cook verify-shipment` as the final gate. The pipeline steps — which cook subcommand runs when, what the agent does in between, the ASR/translation quality gates — live in each skill's SKILL.md, not here. This README covers cook itself: what it is, how to install it, what each subcommand does.

## Bugs that cook fixes

| Bug in the old skill docs | How cook fixes it |
|---|---|
| `--dump-json > file.json` silently swallowed downloads | cook writes `source.json` itself from the probe result (no shell redirection involved) |
| Thumbnail came out as `<name>.raw.jpg` not `<name>.jpg` | cook renames it after download |
| Windows `C:` paths broke ffmpeg `ass` filter | cook uses subprocess list-form (never shell), runs from subtitle/ dir with bare filename |
| `subtitles.py split` leaked single-language cues across zh.srt/en.srt | cook copies `*.merged.srt` to cloud-srt instead of splitting bilingual.srt |
| `transcribe.py` hardcoded `device="cpu"`, float16 unusable on GPU | cook auto-detects CUDA -> float16+cuda, else float32+cpu |
| `source.json` downloaded but never read by downstream — author/links/description wasted | `cook show-source` surfaces the curated fields translation and upload metadata consume |
| detached template only covered transcribe, not burn | cook's `_detach()` helper handles both uniformly |
| No mechanical way to catch translation drift | `cook verify-align` runs DP global alignment |
| No mechanical way to catch missing release-set files | `cook verify-shipment` checks every expected file |

## Development

```bash
git clone https://github.com/ChHsiching/video-cook
cd video-cook
pip install -e .[dev]
pytest test_cook.py -v
```

Tests (199 as of this writing) cover every subcommand whose correctness is non-obvious:

- `verify-align` (DP alignment edge cases), `verify-shipment` (file-presence checks), the cloud-srt copy logic (the fix for the split leak), and the width-based length gate;
- the dub pipeline: synth band split + worker orchestration (`--workers/--stagger`), atomic cue publishing (two concurrent writers of the same `sent_NNNN.wav` never expose a partial file), `dub merge` lockstep invariants, `measure-rate`/`gate` (measured-rate contract), and the retime chain (char alignment, the two-pass rewrite incl. the negative-anchor clamp, assemble auto-retime markers);
- packaging contracts: stage scripts resolve package-internally and never fall back to installed skill copies (ADR 0001).

No network, no ffmpeg, no whisperx — all tests use temp dirs and fake files.

## License

AGPL-3.0-only · Copyright (c) 2026 ChHsiching —— 见 [LICENSE](LICENSE)。

- 使用（含公司内部使用）、修改、分发均免费；但分发或以本代码提供网络服务时，衍生作品须以 AGPL-3.0 开源。
- 闭源商用须另行获取商业授权：hsichingchang@gmail.com

### 贡献条款

提交 PR 即表示你同意以 AGPL-3.0 授权你的贡献，并授予维护者在 AGPL 之外另行提供商业授权的权利（你的贡献始终以 AGPL 对所有人开放）。
