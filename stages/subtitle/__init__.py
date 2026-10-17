"""Subtitle-pipeline stage scripts (migrated from video-subtitle-skill).

Files (kept verbatim, __main__ entry points intact):
- subtitles.py    — shorten / merge-short / biliteral / ass / split
- make_full_srt.py— merge whisperX fragments into full-sentence cues
- make_zh.py      — lift en.srt timestamps onto translations.txt
- transcribe.py   — whisperX transcription emitting one segment per cue
"""
