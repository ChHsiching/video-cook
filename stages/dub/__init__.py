"""Chinese-dub pipeline stage scripts (vendored from video-dubbing-skill).

full_dub.py is the synth/assemble engine `cook dub` drives as a subprocess
(--python selects the indextts venv). The rest are deterministic helpers:
loudness_match, scan_reference, extract_reference, make_zh_dub_srt,
build_merge keep their __main__ entry so they can still be run directly.
measure_rate.py (ticket 04) and length_gate.py (ticket 05) are functions-
only — their CLI lives in cook (`cook dub measure-rate`, `cook dub gate`).
"""
