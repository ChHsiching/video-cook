# DEPRECATED — cook dub mix (old-flow BGM mixing: read dubbed/dub.wav, mux onto the
# cooked bilingual video). The identity assemble stage detects BGM and mixes the
# bed internally (mean > -50dB -> -18dB bed + loudnorm). Kept for reference only.

def cmd_dub_mix(args: argparse.Namespace) -> None:
    """Mix the Chinese dub (dub.wav) with the original BGM (no_vocals.wav),
    then mux into the cooked video. Produces <name>.dubbed.mp4 under dubbed/."""
    _require_ffmpeg()
    root = _video_dir(args.output_root)
    name = args.name
    dubbed_dir = _dubbed_dir(root)

    dub_wav = dubbed_dir / "dub.wav"
    no_vocals = dubbed_dir / "no_vocals.wav"
    # Use the cooked mp4 (with burned subtitles) as the video source.
    cooked_mp4 = _cooked(root, name, ".cooked.mp4")
    cooked_bar_mp4 = _cooked(root, name, ".cooked.bar.mp4")
    video_src = cooked_bar_mp4 if cooked_bar_mp4.exists() and not cooked_mp4.exists() else cooked_mp4

    if not dub_wav.exists():
        _die(f"dub.wav not found: {dub_wav}. Run dub_audio.py (Step 4) first.")
    if not no_vocals.exists():
        _die(f"no_vocals.wav not found: {no_vocals}. Run 'cook dub separate' first.")
    if not video_src.exists():
        _die(f"cooked video not found: {video_src}. Run video-subtitle first.")

    out = _dubbed(root, name, ".dubbed.mp4")
    mixed_wav = dubbed_dir / "_mixed.wav"

    # Detect whether no_vocals.wav actually contains BGM. Measure its RMS level
    # — if below -50dB, the source video has no background music (pure-talk),
    # so there's nothing to preserve. Mixing -50dB silence into the dub is a
    # wasted step that adds nothing audible.
    bg_rms_db = _measure_rms_db(no_vocals)
    has_bgm = bg_rms_db > -50.0
    _log(f"cook dub mix: no_vocals RMS={bg_rms_db:.1f}dB, has_bgm={has_bgm}")

    if has_bgm:
        # Mix dub + ducked BGM. Convert dB to linear gain. -18dB -> 0.125x.
        bg_gain = 10 ** (args.bg_gain / 20.0)
        mix_filter = (
            f"[0:a]volume=1.0[dub];"
            f"[1:a]volume={bg_gain:.4f}[bg];"
            f"[dub][bg]amix=inputs=2:duration=longest:normalize=0[aout]"
        )
        r = subprocess.run(
            ["ffmpeg", "-y",
             "-i", str(dub_wav),
             "-i", str(no_vocals),
             "-filter_complex", mix_filter, "-map", "[aout]",
             "-ac", "2", "-ar", "44100", str(mixed_wav)],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not mixed_wav.exists():
            _die(f"ffmpeg mix failed: {r.stderr[-500:]}", {"mixed": str(mixed_wav)})
    else:
        # No BGM — use dub.wav directly as the audio track (just resample to
        # 44100 stereo to match the mux step's expectations).
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(dub_wav),
             "-ac", "2", "-ar", "44100", str(mixed_wav)],
            capture_output=True, text=True,
        )
        if r.returncode != 0 or not mixed_wav.exists():
            _die(f"ffmpeg dub prep failed: {r.stderr[-500:]}", {"mixed": str(mixed_wav)})

    # Step 2: mux mixed audio into the cooked video (video copied, audio re-encoded to AAC)
    r = subprocess.run(
        ["ffmpeg", "-y",
         "-i", str(video_src),
         "-i", str(mixed_wav),
         "-map", "0:v", "-map", "1:a",
         "-c:v", "copy",
         "-c:a", "aac", "-b:a", "192k",
         "-movflags", "+faststart",
         str(out)],
        capture_output=True, text=True,
    )
    mixed_wav.unlink(missing_ok=True)  # clean up the intermediate
    if r.returncode != 0 or not out.exists():
        _die(f"ffmpeg mux failed: {r.stderr[-500:]}", {"out": str(out)})

    raw_dur = _ffprobe_duration(_raw(root, name, ".raw.mp4"))
    out_dur = _ffprobe_duration(out)
    _emit_json({
        "ok": True,
        "output": str(out.relative_to(root)),
        "video_source": str(video_src.relative_to(root)),
        "bg_gain_db": args.bg_gain,
        "duration": out_dur,
        "raw_duration": raw_dur,
        "duration_match": abs(out_dur - raw_dur) < 2.0 if raw_dur > 0 else None,
    })


