"""Stage scripts for the video-cook pipeline.

Each subpackage holds the deterministic scripts for one pipeline stage
family (subtitle, dub). The scripts keep their own __main__ entry points
so they can also be invoked directly:

    python -m stages.subtitle.subtitles shorten in.srt out.srt --lang zh
    python stages/subtitle/transcribe.py in.wav out.srt large-v3

cook resolves them through cook._stage_script() — inside this
distribution, never in an installed skill copy. See docs/adr/ for the
consolidation decision.
"""
