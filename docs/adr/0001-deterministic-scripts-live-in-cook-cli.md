# ADR 0001: Deterministic stage scripts live in the cook CLI repo

- Status: Accepted (2026-10-17, reflux 2026-10, ticket 01)
- Scope: subtitle-side scripts (subtitles.py, make_full_srt.py, make_zh.py,
  transcribe.py); dub-side scripts follow the same pattern in ticket 02

## Context

cook is the deterministic executor of a three-repo skill trio
(video-download / video-subtitle / video-dubbing), but the scripts it
actually executed lived in the skill repos' `scripts/` directories and were
located at runtime by scanning the user's installed-skill directories
(`~/.agents`, `~/.zcode`, `~/.claude`). This split caused recurring damage:

- **Version skew.** The installed skill copy and the cook version that
  drives it can drift apart. `cook dub assemble --keep-subs` already needs
  a source-probe patch (grep the installed `full_dub.py` for `keep_subs`)
  to defend against an installed copy that predates the flag — a probe
  that exists only because cook does not own the code it runs.
- **Hidden duplication.** `cook transcribe` carried an inline copy of
  transcribe.py (as a `python -c` string) so it would not depend on the
  skill install — the same logic maintained twice, already diverging in
  argument order.
- **No single upgrade path.** A fix to a stage script requires updating
  the skill repo AND reinstalling the skill; `pip install -U video-cook`
  upgrades only the shell.

## Decision

All deterministic stage scripts physically live in the video-cook
repository, under `stages/<stage>/` packages (starting with
`stages/subtitle/`). cook resolves them strictly inside its own
distribution (`cook._stage_script`):

1. `stages/<stage>/<script>` next to `cook.py` — source checkouts and
   normal wheel installs, independent of the caller's cwd;
2. `importlib.resources` on the `stages.<stage>` package — exotic install
   layouts;
3. nothing else. Installed-skill locations are never consulted. A miss is
   an installation bug and dies with a reinstall remedy.

The scripts are migrated verbatim and keep their `__main__` entry points,
so direct invocation (`python -m stages.subtitle.subtitles shorten ...`)
keeps working. Heavy-dependency stages still run as subprocesses under
`--python <venv>` interpreters — but the file they execute is the packaged
one. `pip install -U video-cook` now upgrades shell and stages together.

## Consequences and costs

- **Release coupling (accepted).** Stage-script changes now cut a cook
  release; they can no longer ship independently via the skill repos.
  Given the drift problem, coupling is the point.
- **Skill repos retire to documentation.** Their `scripts/` become stale
  the moment cook releases; a follow-up removes them and rewrites
  SKILL.md call instructions to cook commands (tracked separately — out
  of scope for this ticket).
- **Packaging must ship `stages/`.** `pyproject.toml` lists the packages
  explicitly; forgetting them breaks every stage command at runtime with
  the reinstall error. The wheel content is verified in CI-ish smoke
  (build + resolve from a `--target` install).
- **Stage-script prose moves to stderr.** In-process stage invocation
  (`_run_subs`) redirects the script's stdout to stderr so cook's stdout
  stays pure JSON per its own contract. Consumers that scraped stage
  prose from stdout (none known) must read stderr instead.
- **Dub side unchanged for now.** `_find_dub_script` still resolves the
  installed video-dubbing skill, including the `--keep-subs` probe; it
  migrates with ticket 02. Until then the repo intentionally mixes both
  resolution modes.

## Verification of this change

- Unit: `_stage_script` resolution (found / clear-miss error / ignores
  installed skills), `cook subtitles` end-to-end with home pointed at an
  empty dir and decoy skill copies planted, `cook transcribe` inner
  command targeting the packaged transcribe.py.
- Real-data regression: `cook subtitles` re-run on the leerob
  stanford-cs146s-always-on-agents inputs reproduces all 9 products
  (short/merged SRTs, bilingual SRT, overlay + bar ASS, cloud-srt zh/en)
  **byte-identically** to the accepted r12-era artifacts.
