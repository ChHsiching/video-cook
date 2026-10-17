"""Chinese-dub pipeline stage scripts (vendored from video-dubbing-skill).

full_dub.py is the synth/assemble engine `cook dub` drives as a subprocess
(--python selects the indextts venv). The rest are deterministic helpers:
loudness_match, scan_reference, extract_reference, make_zh_dub_srt,
build_merge, length_gate. Each keeps its __main__ entry so it can still be
run directly.
"""
