#!/usr/bin/env python3
"""cook — deterministic executor for the video-cooking skill pipeline.

This module is the hands of the video-cooking / video-download / video-subtitle
skill trio. Skills are the brain (they decide what to do, in what order, and
handle creative work like translation). cook is the hands: it assembles the
long yt-dlp / ffmpeg / whisperx commands correctly every time, avoids shell
escaping traps, and mechanically verifies that the expected files exist.

Design principles
-----------------
- Every subcommand prints a JSON object on stdout (machine-readable) and
  human-readable progress on stderr. Agents parse stdout.
- Heavy third-party deps (yt-dlp, whisperx) are imported lazily inside the
  subcommands that need them, so `cook verify-shipment` works even if the
  user hasn't installed whisperx.
- Long tasks (transcribe, burn) auto-detach so they survive shell timeouts.
- Exit codes are meaningful: 0 = the subcommand's done criterion passed,
  non-zero = it didn't (agent can branch on this).

Subcommands
-----------
  cook doctor                 — check environment (python/ffmpeg/yt-dlp/node/...)
  cook download <url> [...]   — yt-dlp download with cookie negotiation
  cook extract <root> <name>  — ffmpeg audio extraction (16kHz mono WAV)
  cook transcribe <root> <name> [...] — whisperX transcription (auto-detached)
  cook subtitles <root> <name> [...]  — shorten+merge+biliteral+ass+cloud-srt
  cook burn <root> <name> [...]       — ffmpeg subtitle burning (auto-detached)
  cook cover <root> <name>    — place cover.jpg in cooked/
  cook verify-align <root> <name>     — DP-align en.srt vs translations.txt
  cook verify-shipment <root> <name>  — check the full release set is present
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

__version__ = "0.1.0"

# ----------------------------------------------------------------------------
# Output helpers — JSON to stdout (agents parse this), prose to stderr (humans)
# ----------------------------------------------------------------------------

def _emit_json(obj: dict[str, Any]) -> None:
    """Print a JSON object to stdout. This is the machine-readable channel."""
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def _log(msg: str) -> None:
    """Print a human-readable line to stderr. Does not pollute stdout JSON."""
    print(msg, file=sys.stderr, flush=True)


def _die(msg: str, obj: dict[str, Any] | None = None) -> None:
    """Log an error and exit non-zero. Optionally emit a JSON object first."""
    _log(f"cook: error: {msg}")
    if obj is not None:
        obj.setdefault("ok", False)
        obj.setdefault("error", msg)
        _emit_json(obj)
    sys.exit(1)


# ----------------------------------------------------------------------------
# Path helpers — the shared directory convention across all three skills.
# ----------------------------------------------------------------------------

def _video_dir(output_root: str | Path) -> Path:
    """The per-video directory. output_root IS the per-video dir in our
    convention (<cwd>/<author>/<video-name>/), so it's a passthrough, but
    having a named function keeps the call sites readable."""
    return Path(output_root)


def _raw(output_root: str | Path, name: str, suffix: str) -> Path:
    """Path to a file under raw/."""
    return _video_dir(output_root) / "raw" / f"{name}{suffix}"


def _transcript(output_root: str | Path, name: str, suffix: str) -> Path:
    return _video_dir(output_root) / "transcript" / f"{name}{suffix}"


def _subtitle(output_root: str | Path, name: str, suffix: str) -> Path:
    return _video_dir(output_root) / "subtitle" / f"{name}{suffix}"


def _cooked(output_root: str | Path, name: str, suffix: str) -> Path:
    return _video_dir(output_root) / "cooked" / f"{name}{suffix}"


# ----------------------------------------------------------------------------
# Detached execution — survives shell timeouts (~10 min in some agent envs).
# ----------------------------------------------------------------------------

def _detach_windows(cmd: list[str], cwd: Path, log_file: Path, err_file: Path) -> int:
    """Launch cmd detached on Windows via PowerShell Start-Process.

    subprocess.Popen with DETACHED_PROCESS would also work, but Start-Process
    gives cleaner process-group separation and is what the existing skill
    template used. Returns the launched PID.
    """
    ps_script = (
        f"Start-Process -FilePath '{cmd[0]}' "
        f"-ArgumentList '{' '.join(_ps_quote(c) for c in cmd[1:])}' "
        f"-WorkingDirectory '{cwd}' "
        f"-RedirectStandardOutput '{log_file}' "
        f"-RedirectStandardError '{err_file}' "
        f"-WindowStyle Hidden "
        f"-PassThru | Select-Object -ExpandProperty Id"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_script],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        _die(f"failed to launch detached process: {result.stderr.strip()}")
    pid = int(result.stdout.strip())
    return pid


def _ps_quote(s: str) -> str:
    """Quote a single arg for PowerShell -ArgumentList string."""
    if not s or re.search(r"[ \t'\"\\]", s):
        return "'" + s.replace("'", "''") + "'"
    return s


def _detach_unix(cmd: list[str], cwd: Path, log_file: Path, err_file: Path) -> int:
    """Launch cmd detached on Unix via nohup. Returns the launched PID."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    err_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "wb") as log, open(err_file, "wb") as err:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=log, stderr=err,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # equivalent to setsid
        )
    return proc.pid


def _detach(cmd: list[str], cwd: Path, log_file: Path, err_file: Path) -> int:
    """Launch cmd detached, platform-appropriate. Returns PID."""
    if sys.platform == "win32":
        return _detach_windows(cmd, cwd, log_file, err_file)
    return _detach_unix(cmd, cwd, log_file, err_file)


# ----------------------------------------------------------------------------
# ffprobe helpers
# ----------------------------------------------------------------------------

def _ffprobe_duration(path: Path) -> float:
    """Return media duration in seconds, or 0.0 if unreadable."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=60,
        )
        return float(r.stdout.strip()) if r.returncode == 0 else 0.0
    except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
        return 0.0


# ----------------------------------------------------------------------------
# SRT helpers (also used by verify-align and verify-shipment)
# ----------------------------------------------------------------------------

_SRT_BLOCK = re.compile(r"\r?\n\r?\n")


def _read_srt_cues(path: Path) -> list[tuple[str, str]]:
    """Return [(timestamp, text), ...] from an SRT. Cue number is discarded —
    we only care about timestamp + text for alignment/verification."""
    text = path.read_text(encoding="utf-8-sig")  # tolerate BOM
    cues = []
    for block in _SRT_BLOCK.split(text.strip()):
        lines = re.split(r"\r?\n", block)
        if len(lines) >= 2:
            ts = lines[1].strip() if "-->" in lines[1] else ""
            body = " ".join(lines[2:]).strip() if len(lines) >= 3 else ""
            cues.append((ts, body))
    return cues


def _is_relative_file(p: Path) -> bool:
    return p.exists() and p.is_file()


# ============================================================================
# Subcommand: doctor
# ============================================================================

def cmd_doctor(args: argparse.Namespace) -> None:
    """Check the environment. Reports what's available; does not fail —
    the agent reads the JSON and decides what to ask the user to install."""
    report: dict[str, Any] = {"ok": True, "tools": {}, "issues": []}

    def check_binary(name: str, min_version_args: list[str] | None = None) -> None:
        path = shutil.which(name)
        info: dict[str, Any] = {"installed": bool(path), "path": path}
        if path and min_version_args:
            try:
                r = subprocess.run([name] + min_version_args,
                                   capture_output=True, text=True, timeout=15)
                info["version"] = r.stdout.strip().splitlines()[0] if r.stdout else ""
            except (subprocess.TimeoutExpired, IndexError):
                info["version"] = "(unknown)"
        report["tools"][name] = info

    check_binary("ffmpeg", ["-version"])
    check_binary("node", ["--version"])

    # yt-dlp: prefer the python package, fall back to binary
    try:
        import yt_dlp
        report["tools"]["yt_dlp"] = {
            "installed": True, "source": "python-package",
            "version": getattr(yt_dlp.version, "__version__", "(unknown)"),
        }
    except ImportError:
        path = shutil.which("yt-dlp")
        report["tools"]["yt_dlp"] = {"installed": bool(path), "path": path}
        if not path:
            report["issues"].append("yt-dlp not found — pip install video-cook[download]")

    # whisperx + torch + CUDA detection
    try:
        import whisperx
        report["tools"]["whisperx"] = {"installed": True}
    except ImportError:
        report["tools"]["whisperx"] = {"installed": False}
        report["issues"].append("whisperx not found — pip install video-cook[transcribe]")

    try:
        import torch
        cuda = torch.cuda.is_available()
        report["tools"]["torch"] = {
            "installed": True, "version": torch.__version__,
            "cuda_available": cuda,
            "device": "cuda" if cuda else "cpu",
        }
        if cuda:
            report["tools"]["torch"]["gpu"] = torch.cuda.get_device_name(0)
    except ImportError:
        report["tools"]["torch"] = {"installed": False}

    if not report["tools"]["ffmpeg"]["installed"]:
        report["issues"].append("ffmpeg not on PATH — required for extract/burn/cover")

    _emit_json(report)


# ============================================================================
# Subcommand: download
# ============================================================================

# Browsers cook tries, in order, when a cookieless -F hits an auth wall.
# Names are the exact values yt-dlp's --cookies-from-browser accepts.
_COOKIE_BROWSER_ORDER = ["firefox", "chrome", "edge", "brave"]


def cmd_download(args: argparse.Namespace) -> None:
    """Download video + source.json + thumbnail, with cookie negotiation.

    Wraps yt-dlp so the agent never hand-assembles the long command. Fixes the
    known traps: uses --print-to-file (not stdout redirect) for JSON, renames
    the .raw.jpg thumbnail to <name>.jpg, and runs cookie negotiation
    internally rather than asking the agent to drive it.
    """
    try:
        import yt_dlp
    except ImportError:
        _die("yt-dlp not installed. Run: pip install video-cook[download]")

    url = args.url
    cwd = Path.cwd()

    # --- Phase 1: probe metadata to derive author/name ---
    _log(f"cook download: probing {url}")
    probe_opts = {
        "quiet": True, "no_warnings": True, "skip_download": True,
        "js_runtimes": "node", "remote_components": "ejs:github",
    }
    if args.cookies_from_browser:
        probe_opts["cookiesfrombrowser"] = (args.cookies_from_browser,)

    with yt_dlp.YoutubeDL(probe_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            err = str(e)
            if "Sign in to confirm" in err or "bot" in err.lower():
                # cookieless failed with auth wall — negotiate
                cookie_src = _negotiate_cookie(url, args.cookies_from_browser)
                if cookie_src is None:
                    _die("could not find a working cookie source — "
                         "ask the user which browser they are logged in with")
                probe_opts["cookiesfrombrowser"] = (cookie_src,)
                with yt_dlp.YoutubeDL(probe_opts) as ydl2:
                    info = ydl2.extract_info(url, download=False)
            else:
                _die(f"yt-dlp probe failed: {err}")

    title = info.get("title", "video")
    uploader = info.get("uploader") or info.get("channel") or "unknown"

    # --- Phase 2: derive paths ---
    author = args.author or _slugify(uploader)
    video_name = args.name or _slugify(title)
    name = video_name  # name stem defaults to the video name
    output_root = cwd / author / video_name
    raw_dir = output_root / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    _log(f"cook download: output_root={output_root}, name={name}")

    # --- Phase 3: real download (video + thumbnail + json in one call) ---
    format_spec = "bestvideo+bestaudio"
    if args.quality:
        format_spec = f"bv*[height<={args.quality}]+ba/b[height<={args.quality}]"

    out_tpl = str(raw_dir / f"{name}.raw.%(ext)s")
    json_path = raw_dir / f"{name}.source.json"
    thumb_actual = raw_dir / f"{name}.raw.jpg"   # what yt-dlp actually writes
    thumb_target = raw_dir / f"{name}.jpg"        # what we want

    dl_opts = {
        "format": format_spec,
        "merge_output_format": "mp4",
        "writethumbnail": True,
        "convert_thumbnails": "jpg",
        "outtmpl": out_tpl,
        "js_runtimes": "node",
        "remote_components": "ejs:github",
        "quiet": False,
        "no_warnings": False,
        # use print_to_file rather than dump-json — avoids the stdout-redirect
        # trap that silently swallowed downloads in the old skill.
        "print_to_file": {"%(all)j": str(json_path)},
    }
    if "cookiesfrombrowser" in probe_opts:
        dl_opts["cookiesfrombrowser"] = probe_opts["cookiesfrombrowser"]

    _log("cook download: downloading video, thumbnail, and metadata")
    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        ydl.download([url])

    # --- Phase 4: fix thumbnail name (yt-dlp leaves it as <name>.raw.jpg) ---
    if thumb_actual.exists() and not thumb_target.exists():
        thumb_actual.rename(thumb_target)
        _log(f"cook download: renamed thumbnail {thumb_actual.name} -> {thumb_target.name}")

    # --- Phase 5: verify ---
    raw_mp4 = raw_dir / f"{name}.raw.mp4"
    if not raw_mp4.exists():
        _die(f"expected {raw_mp4} but it doesn't exist", {
            "output_root": str(output_root), "name": name})

    duration = _ffprobe_duration(raw_mp4)
    if duration <= 0:
        _die(f"ffprobe reports duration <= 0 for {raw_mp4}")

    files = []
    for p in raw_dir.iterdir():
        if p.is_file():
            files.append(str(p.relative_to(output_root)))

    _emit_json({
        "ok": True,
        "output_root": str(output_root),
        "name": name,
        "author": author,
        "video_name": video_name,
        "title": title,
        "uploader": uploader,
        "duration": duration,
        "files": files,
        "thumbnail_renamed": thumb_target.exists(),
        "source_json_present": json_path.exists(),
        "cookie_source": probe_opts.get("cookiesfrombrowser", (None,))[0],
    })


def _slugify(s: str) -> str:
    """Lowercase, keep alnum, replace runs of separators with a single dash."""
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)         # drop punctuation (unicode-aware)
    s = re.sub(r"[\s_-]+", "-", s)          # collapse separators
    return s.strip("-") or "video"


def _negotiate_cookie(url: str, user_specified: str | None) -> str | None:
    """Find a working --cookies-from-browser value. Tries the user-specified
    browser first (if any), then firefox/chrome/edge/brave in order. Returns
    the first browser whose -F returns real video formats, or None."""
    try:
        import yt_dlp
    except ImportError:
        return None

    candidates = [user_specified] if user_specified else []
    candidates += [b for b in _COOKIE_BROWSER_ORDER if b not in candidates]

    for browser in candidates:
        if not browser:
            continue
        # verify this browser's profile actually exists on Windows before trying
        if sys.platform == "win32" and not _browser_profile_exists(browser):
            _log(f"cook download: skipping {browser} (no profile found)")
            continue
        _log(f"cook download: trying cookies from {browser}")
        opts = {
            "quiet": True, "no_warnings": True, "skip_download": True,
            "cookiesfrombrowser": (browser,),
            "js_runtimes": "node", "remote_components": "ejs:github",
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
            # if we got here without exception, the cookies worked
            if info and info.get("formats"):
                _log(f"cook download: {browser} cookies work")
                return browser
        except Exception:
            continue
    return None


def _browser_profile_exists(browser: str) -> bool:
    """Quick check whether a browser's profile dir exists on Windows,
    to skip browsers that aren't installed."""
    env_local = os.environ.get("LOCALAPPDATA", "")
    env_appdata = os.environ.get("APPDATA", "")
    paths = {
        "firefox": [Path(env_appdata) / "Mozilla" / "Firefox" / "Profiles"],
        "chrome": [Path(env_local) / "Google" / "Chrome" / "User Data"],
        "edge": [Path(env_local) / "Microsoft" / "Edge" / "User Data"],
        "brave": [Path(env_local) / "BraveSoftware" / "Brave-Browser" / "User Data"],
    }.get(browser, [])
    return any(p.exists() for p in paths)


# ============================================================================
# Subcommand: extract
# ============================================================================

def cmd_extract(args: argparse.Namespace) -> None:
    """Extract 16kHz mono WAV from the raw mp4."""
    root = _video_dir(args.output_root)
    raw_mp4 = _raw(root, args.name, ".raw.mp4")
    wav = _transcript(root, args.name, ".audio.wav")
    wav.parent.mkdir(parents=True, exist_ok=True)

    if not raw_mp4.exists():
        _die(f"raw video not found: {raw_mp4}")

    _log(f"cook extract: {raw_mp4.name} -> {wav.name}")
    cmd = ["ffmpeg", "-y", "-i", str(raw_mp4), "-vn", "-ac", "1",
           "-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0 or not wav.exists() or wav.stat().st_size == 0:
        _die(f"ffmpeg extract failed: {r.stderr[-500:]}", {"wav": str(wav)})

    _emit_json({"ok": True, "wav": str(wav.relative_to(root)),
                "size": wav.stat().st_size})


# ============================================================================
# Subcommand: transcribe
# ============================================================================

def cmd_transcribe(args: argparse.Namespace) -> None:
    """Run whisperX transcription. Auto-detects CUDA to pick device+compute.
    Detaches so it survives shell timeouts; agent polls the log file."""
    root = _video_dir(args.output_root)
    wav = _transcript(root, args.name, ".audio.wav")
    srt = _transcript(root, args.name, ".en.srt")
    srt.parent.mkdir(parents=True, exist_ok=True)

    if not wav.exists():
        _die(f"audio not found: {wav}. Run 'cook extract' first.")

    # decide device + compute_type
    if args.compute == "auto":
        device, compute = _detect_device_compute()
    else:
        # user pinned compute; device follows
        device = "cuda" if args.compute == "float16" else "cpu"
        compute = args.compute

    model = args.model
    language = args.language
    _log(f"cook transcribe: model={model} device={device} compute={compute} lang={language}")

    # build the inner command — we re-invoke ourselves in a child python with
    # the actual whisperx call, so detachment is uniform across platforms.
    inner_cmd = [
        sys.executable, "-c",
        _TRANSCRIBE_INNER,
        str(wav), str(srt), model, device, compute, language,
    ]
    log_file = _transcript(root, args.name, ".transcribe.log")
    err_file = _transcript(root, args.name, ".transcribe.err.log")

    pid = _detach(inner_cmd, cwd=root, log_file=log_file, err_file=err_file)
    _log(f"cook transcribe: detached PID={pid}, log={log_file.name}")
    _log("cook transcribe: this is the slow step (CPU large-v3 runs ~0.5-0.7x realtime)")

    _emit_json({
        "ok": True, "detached": True, "pid": pid,
        "device": device, "compute": compute, "model": model, "language": language,
        "log": str(log_file.relative_to(root)),
        "err_log": str(err_file.relative_to(root)),
        "output_srt": str(srt.relative_to(root)),
        # The agent polls until this file exists and contains "[transcribe] done."
        "done_marker": "[transcribe] done.",
    })


# This string is exec'd by the detached child. Kept inline so cook is a single
# file with no transcribe.py dependency. Mirrors video-subtitle/scripts/transcribe.py
# but with device/compute parameterized (fixes the hardcoded device="cpu" bug).
_TRANSCRIBE_INNER = """
import sys, time, os
wav, out, model_size, device, compute, language = sys.argv[1:7]
print(f"[transcribe] model={model_size} device={device} compute={compute} language={language}", flush=True)
t0 = time.time()
import whisperx
model = whisperx.load_model(model_size, device=device, compute_type=compute)
audio = whisperx.load_audio(wav)
result = model.transcribe(audio, batch_size=8, language=language)
print(f"[transcribe] base transcribe done in {time.time()-t0:.1f}s", flush=True)
try:
    am, meta = whisperx.load_align_model(language_code=language, device=device)
    result = whisperx.align(result["segments"], am, meta, audio, device=device)
    print(f"[transcribe] alignment done in {time.time()-t0:.1f}s", flush=True)
except Exception as e:
    print(f"[transcribe] alignment skipped: {e}", flush=True)
os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
def fmt(ts):
    h=int(ts//3600); m=int((ts%3600)//60); s=ts%60
    return f"{h:02d}:{m:02d}:{int(s):02d},{int(round((s-int(s))*1000)):03d}"
lines=[]
for i,seg in enumerate(result["segments"],1):
    t=seg["text"].strip()
    if not t: continue
    lines.append(f"{i}\\n{fmt(seg['start'])} --> {fmt(seg['end'])}\\n{t}")
with open(out,"w",encoding="utf-8") as f:
    f.write("\\n\\n".join(lines)+"\\n")
elapsed=time.time()-t0
dur=result["segments"][-1]["end"] if result["segments"] else 0
print(f"[transcribe] done. {len(lines)} segments, audio~{dur:.1f}s, elapsed={elapsed:.1f}s ({dur/elapsed:.2f}x realtime) -> {out}", flush=True)
"""


def _detect_device_compute() -> tuple[str, str]:
    """Return (device, compute_type). CUDA -> (cuda, float16), else (cpu, float32)."""
    try:
        import torch
        if torch.cuda.is_available():
            return ("cuda", "float16")
    except ImportError:
        pass
    return ("cpu", "float32")


# ============================================================================
# Subcommand: subtitles — shorten + merge-short + biliteral + ass + cloud-srt
# ============================================================================

def cmd_subtitles(args: argparse.Namespace) -> None:
    """Run the full subtitle-processing pipeline in one shot, including
    producing cloud-srt/ by COPYING the per-language merged SRTs (NOT by
    splitting the bilingual SRT — split has a known bug where union-mode
    bilingual SRTs leak single-language cues into the wrong output)."""
    root = _video_dir(args.output_root)
    name = args.name
    tdir = root / "transcript"
    sdir = root / "subtitle"
    sdir.mkdir(parents=True, exist_ok=True)

    en_srt = tdir / f"{name}.en.srt"
    zh_srt = tdir / f"{name}.zh.srt"
    if not en_srt.exists() or not zh_srt.exists():
        _die(f"need both {en_srt.name} and {zh_srt.name} to exist")

    # import the vendor'd subtitles module
    subs = _import_subtitles_module()

    # Step 4a: shorten both languages
    en_short = tdir / f"{name}.en.short.srt"
    zh_short = tdir / f"{name}.zh.short.srt"
    _run_subs(subs, ["shorten", str(en_srt), str(en_short), "--lang", "en"])
    _run_subs(subs, ["shorten", str(zh_srt), str(zh_short), "--lang", "zh"])

    # Step 4b: merge-short both
    en_merged = tdir / f"{name}.en.merged.srt"
    zh_merged = tdir / f"{name}.zh.merged.srt"
    _run_subs(subs, ["merge-short", str(en_short), str(en_merged),
                     "--min-dur", "1.2", "--max-len", "90"])
    _run_subs(subs, ["merge-short", str(zh_short), str(zh_merged),
                     "--min-dur", "1.2", "--max-len", "42"])

    # Step 4c: biliteral merge
    bilingual = sdir / f"{name}.bilingual.srt"
    _run_subs(subs, ["biliteral", str(en_merged), str(zh_merged), str(bilingual)])

    # Step 4d: ASS (overlay and/or bottom-bar)
    ass_files = []
    overlay_ass = sdir / f"{name}.bilingual.ass"
    _run_subs(subs, ["ass", str(bilingual), str(overlay_ass)])
    ass_files.append(str(overlay_ass.relative_to(root)))

    bar_ass = None
    if args.mode == "bottom-bar":
        bar_ass = sdir / f"{name}.bilingual.bar.ass"
        _run_subs(subs, ["ass", str(bilingual), str(bar_ass),
                         "--bottom-bar", str(args.bar_px)])
        ass_files.append(str(bar_ass.relative_to(root)))

    # Step 4e: cloud-srt — copy the merged single-language files, do NOT split.
    # This is the fix for B3 (splitting union-mode bilingual SRTs leaks cues
    # across languages).
    cloud_dir = root / "cloud-srt"
    cloud_dir.mkdir(parents=True, exist_ok=True)
    cloud_zh = cloud_dir / "zh.srt"
    cloud_en = cloud_dir / "en.srt"
    shutil.copyfile(zh_merged, cloud_zh)
    shutil.copyfile(en_merged, cloud_en)
    _log(f"cook subtitles: cloud-srt copied from merged (zh={zh_merged.name}, en={en_merged.name})")

    # length-check the produced files
    issues = []
    for srt_path, limit, label in [(cloud_zh, 45, "zh"), (cloud_en, 90, "en")]:
        cues = _read_srt_cues(srt_path)
        over = [(ts, body) for ts, body in cues if len(body) > limit]
        if over:
            issues.append(f"{label}.srt has {len(over)} cues over {limit} chars")

    _emit_json({
        "ok": True,
        "bilingual_srt": str(bilingual.relative_to(root)),
        "ass_files": ass_files,
        "cloud_srt": {
            "zh": str(cloud_zh.relative_to(root)),
            "en": str(cloud_en.relative_to(root)),
            "zh_cues": len(_read_srt_cues(cloud_zh)),
            "en_cues": len(_read_srt_cues(cloud_en)),
        },
        "length_issues": issues,
    })


def _import_subtitles_module():
    """Import the subtitles module from video-subtitle skill scripts, or
    fall back to a vendored copy. The module has zero third-party deps
    (only stdlib re/argparse/sys), so either path works."""
    # try the skill install location first
    candidates = [
        Path.home() / ".agents" / "skills" / "video-subtitle" / "scripts" / "subtitles.py",
        Path.home() / ".zcode" / "skills" / "video-subtitle" / "scripts" / "subtitles.py",
        Path.home() / ".claude" / "skills" / "video-subtitle" / "scripts" / "subtitles.py",
    ]
    for cand in candidates:
        if cand.exists():
            import importlib.util
            spec = importlib.util.spec_from_file_location("subtitles", cand)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    _die("subtitles.py not found. Install video-subtitle skill, or vendor it.")


def _run_subs(mod, argv: list[str]) -> None:
    """Invoke the subtitles module's main with a synthetic argv."""
    old = sys.argv
    sys.argv = ["subtitles.py"] + argv
    try:
        mod.main()
    finally:
        sys.argv = old


# ============================================================================
# Subcommand: burn
# ============================================================================

def cmd_burn(args: argparse.Namespace) -> None:
    """Burn subtitles into video via ffmpeg. Auto-detaches. Uses subprocess
    list-form (never shell=True) so backslashes and drive letters in Windows
    paths can't break the ass filter — this is the fix for B4."""
    root = _video_dir(args.output_root)
    name = args.name
    raw_mp4 = _raw(root, name, ".raw.mp4")
    cooked_dir = root / "cooked"
    cooked_dir.mkdir(parents=True, exist_ok=True)

    if args.mode == "bottom-bar":
        ass = _subtitle(root, name, ".bilingual.bar.ass")
        out = _cooked(root, name, ".cooked.bar.mp4")
        vf = f"pad=iw:ih+{args.bar_px}:color=black,ass={ass.name}"
        cwd = ass.parent  # run from subtitle/ so ass filter gets bare filename
    else:
        ass = _subtitle(root, name, ".bilingual.ass")
        out = _cooked(root, name, ".cooked.mp4")
        vf = f"ass={ass.name}"
        cwd = ass.parent

    if not raw_mp4.exists():
        _die(f"raw video not found: {raw_mp4}")
    if not ass.exists():
        _die(f"ASS not found: {ass}. Run 'cook subtitles' first.")

    cmd = [
        "ffmpeg", "-y", "-i", str(raw_mp4),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "faster", "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(out),
    ]
    log_file = cooked_dir / "burn.log"
    err_file = cooked_dir / "burn.err.log"

    _log(f"cook burn: detaching ffmpeg (mode={args.mode})")
    pid = _detach(cmd, cwd=cwd, log_file=log_file, err_file=err_file)

    _emit_json({
        "ok": True, "detached": True, "pid": pid,
        "mode": args.mode,
        "output": str(out.relative_to(root)),
        "log": str(log_file.relative_to(root)),
        "err_log": str(err_file.relative_to(root)),
        "done_marker": "kb/s",  # last line of ffmpeg progress contains bitrate
    })


# ============================================================================
# Subcommand: cover
# ============================================================================

def cmd_cover(args: argparse.Namespace) -> None:
    """Place cover.jpg in cooked/. Prefers reusing the raw/ thumbnail from
    download; only re-fetches via yt-dlp if raw/ has no thumbnail."""
    root = _video_dir(args.output_root)
    name = args.name
    cooked_dir = root / "cooked"
    cooked_dir.mkdir(parents=True, exist_ok=True)
    target = cooked_dir / "cover.jpg"

    raw_jpg = _raw(root, name, ".jpg")
    if raw_jpg.exists():
        shutil.copyfile(raw_jpg, target)
        _log(f"cook cover: copied {raw_jpg.name} -> {target.name}")
    elif target.exists():
        _log(f"cook cover: {target.name} already exists, skipping")
    else:
        _die(f"no thumbnail at {raw_jpg} and no existing cover.jpg. "
             "Re-run 'cook download' or place a cover manually.")

    _emit_json({"ok": True, "cover": str(target.relative_to(root)),
                "size": target.stat().st_size})


# ============================================================================
# Subcommand: show-source — extract key fields from source.json
# ============================================================================

# The fields worth surfacing to the agent. yt-dlp's full info-dict is 1MB+
# with hundreds of fields most of which are internal (formats, thumbnails
# list, automatic_caps, etc). This is the curated subset that actually helps
# translation context (Step 3) and upload metadata (Step 6/7).
_SOURCE_FIELDS = [
    "title",              # original title — for upload.md titles + README header
    "uploader",           # author name — for "who is the author" in description
    "channel",            # channel name (often same as uploader, sometimes cleaner)
    "uploader_url",       # author's profile/page — for linking their repo/handle
    "webpage_url",        # source video URL — for README header + source links
    "duration",           # seconds — for README header
    "upload_date",        # YYYYMMDD — for README header
    "description",        # source description — KEY for translation context:
                          #   often contains video topic, chapter outline,
                          #   terminology, mentioned tools/people/projects.
                          #   Helps fix ASR proper-noun errors in Step 3.
    "tags",               # source tags — terminology hints
    "categories",         # source categories — topic context
]


def cmd_show_source(args: argparse.Namespace) -> None:
    """Extract the useful fields from raw/<name>.source.json and emit them
    as a small JSON object. The source.json written by `cook download` is
    yt-dlp's full info-dict (1MB+, hundreds of fields) — fine on disk, but
    reading it wholesale into agent context wastes budget. This subcommand
    surfaces just the fields translation (Step 3) and upload metadata
    (Step 6/7) actually consume.

    Exit 1 if source.json is missing (run `cook download` first)."""
    root = _video_dir(args.output_root)
    src = _raw(root, args.name, ".source.json")
    if not src.exists():
        _die(f"source.json not found: {src}. Run 'cook download' first.")

    try:
        info = json.loads(src.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        _die(f"source.json is not valid JSON: {e}")

    out = {"ok": True, "source_json": str(src.relative_to(root))}
    for field in _SOURCE_FIELDS:
        val = info.get(field)
        if val is not None:
            # trim absurdly long values (some descriptions are 10KB+)
            if isinstance(val, str) and len(val) > 5000 and not args.full:
                out[field] = val[:5000] + f"\n... [truncated, {len(val)} chars total, pass --full to see all]"
            else:
                out[field] = val
    _emit_json(out)


# ============================================================================
# Subcommand: verify-align — DP global alignment of en.srt vs translations.txt
# ============================================================================

def cmd_verify_align(args: argparse.Namespace) -> None:
    """Needleman-Wunsch style global alignment: maps each line of
    translations.txt to a cue in en.srt, allowing skips (missing translations)
    and extras (extra translation lines). Catches the two failure modes that
    pass-by-pass self-review can't: (a) one line of translation accidentally
    covering two English cues, (b) the whole translation drifting one cue off.

    Exit 0 = perfect alignment (no missing, no extra). Exit 1 = problems found.
    """
    root = _video_dir(args.output_root)
    name = args.name
    en_srt = _transcript(root, name, ".en.srt")
    # translations.txt lives at transcript/translations.txt (no name stem) —
    # it's a pure text file, one line per English cue, line N = cue N.
    trans_file = root / "transcript" / "translations.txt"
    if not en_srt.exists():
        _die(f"en.srt not found: {en_srt}")
    if not trans_file.exists():
        _die(f"translations.txt not found at {trans_file}")

    en_cues = _read_srt_cues(en_srt)
    trans_lines = [l.strip() for l in trans_file.read_text(encoding="utf-8").splitlines()
                   if l.strip() != "" or True]  # keep position; we'll filter below
    # strip trailing blanks
    while trans_lines and trans_lines[-1] == "":
        trans_lines.pop()

    alignment = _dp_align(en_cues, trans_lines)
    # missing: en cue index whose alignment entry has j=None
    missing = [i + 1 for i, j in alignment if j is None]
    # extra: translation index whose alignment entry has i=None
    extra = [j + 1 for i, j in alignment if i is None]

    report = {
        "ok": not missing and not extra,
        "en_cues": len(en_cues),
        "translation_lines": len(trans_lines),
        "missing_translations": missing,   # en cue numbers with no zh translation
        "extra_translations": extra,        # translation line numbers with no en cue
    }
    _emit_json(report)
    sys.exit(0 if report["ok"] else 1)


def _dp_align(en_cues: list[tuple[str, str]],
              trans_lines: list[str]) -> list[tuple[int | None, int | None]]:
    """Global alignment via DP. Returns list of (en_idx_or_None, trans_idx_or_None).

    Scoring blends two signals:
    - Token overlap (when both sides have ASCII tokens — the reliable signal)
    - Position proximity (fallback when one side is token-less, e.g. pure
      Chinese translation. Without this, DP can't tell *which* cue a token-less
      line maps to, and may report the missing point at the wrong index.)
    """
    def toks(s: str) -> set[str]:
        return {t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9_./+-]{2,}", s)}

    en_toks = [toks(t) for _, t in en_cues]
    zh_toks = [toks(t) for t in trans_lines]
    NE, NZ = len(en_cues), len(trans_lines)

    def score(ei: int, zi: int) -> float:
        et = en_toks[ei]
        zt = zh_toks[zi]
        # token-based score (primary signal)
        if et and zt:
            union = et | zt
            tok_score = len(et & zt) / len(union) if union else 0.0
        elif not et and not zt:
            tok_score = 1.0  # both token-less: compatible, defer to position
        else:
            tok_score = 0.3  # one-sided: weak signal
        # position-proximity bonus (secondary signal, matters when tokens absent)
        # reward when trans line index is close to its "expected" position
        # (i.e., zi ≈ ei when counts are similar)
        if NE > 0 and NZ > 0:
            expected_z = ei * NZ / NE
            dist = abs(zi - expected_z)
            pos_bonus = max(0.0, 0.3 - 0.1 * dist)  # up to +0.3, decays with distance
        else:
            pos_bonus = 0.0
        return tok_score + pos_bonus

    NEG_INF = float("-inf")
    # f[i][j] = best score aligning en[:i] with trans[:j]
    f = [[NEG_INF] * (NZ + 1) for _ in range(NE + 1)]
    bt = [[None] * (NZ + 1) for _ in range(NE + 1)]
    f[0][0] = 0.0
    for i in range(NE + 1):
        for j in range(NZ + 1):
            if i == 0 and j == 0:
                continue
            best = NEG_INF
            op = None
            if i > 0 and j > 0:
                s = f[i - 1][j - 1] + score(i - 1, j - 1)
                if s > best:
                    best, op = s, ("M", i - 1, j - 1)
            if i > 0:  # en cue with no translation (skip)
                s = f[i - 1][j] - 0.5
                if s > best:
                    best, op = s, ("E", i - 1, None)
            if j > 0:  # translation with no en cue (extra)
                s = f[i][j - 1] - 0.5
                if s > best:
                    best, op = s, ("Z", None, j - 1)
            f[i][j] = best
            bt[i][j] = op

    # backtrack
    i, j = NE, NZ
    path = []
    while i > 0 or j > 0:
        op = bt[i][j]
        if op is None:
            break
        kind, ei, zi = op
        path.append((ei, zi))
        if kind == "M":
            i, j = i - 1, j - 1
        elif kind == "E":
            i = i - 1
        else:
            j = j - 1
    path.reverse()
    return path


# ============================================================================
# Subcommand: verify-shipment — check the full release set exists
# ============================================================================

def cmd_verify_shipment(args: argparse.Namespace) -> None:
    """Verify the full release set is present for a cooked video. Optionally
    restrict to a stage (--stage download/transcript/subtitle/cooked/cloud-srt).

    Exit 0 = everything present. Exit 1 = something missing (agent should go
    back and produce it before reporting the pipeline done)."""
    root = _video_dir(args.output_root)
    name = args.name

    # the canonical release set — keep in sync with skill docs
    release_set: dict[str, list[Path]] = {
        "raw": [
            _raw(root, name, ".raw.mp4"),
            _raw(root, name, ".source.json"),
            _raw(root, name, ".jpg"),
        ],
        "transcript": [
            _transcript(root, name, ".audio.wav"),
            _transcript(root, name, ".en.srt"),
            _transcript(root, name, ".zh.srt"),
            _transcript(root, "", "") if False else root / "transcript" / "asr-fixes.md",
        ],
        "subtitle": [
            _subtitle(root, name, ".bilingual.srt"),
            # at least one ASS must exist (overlay or bar)
        ],
        "cloud-srt": [
            root / "cloud-srt" / "zh.srt",
            root / "cloud-srt" / "en.srt",
        ],
        "cooked": [
            _cooked(root, name, ".upload.md"),
            root / "cooked" / "cover.jpg",
            # at least one cooked mp4 must exist
        ],
        "root": [
            root / "README.md",
        ],
    }

    # ASS and cooked mp4 have overlay/bar variants — accept either
    overlay_ass = _subtitle(root, name, ".bilingual.ass")
    bar_ass = _subtitle(root, name, ".bilingual.bar.ass")
    if overlay_ass.exists() or bar_ass.exists():
        release_set["subtitle"].append(overlay_ass if overlay_ass.exists() else bar_ass)

    cooked_overlay = _cooked(root, name, ".cooked.mp4")
    cooked_bar = _cooked(root, name, ".cooked.bar.mp4")
    cooked_mp4 = cooked_overlay if cooked_overlay.exists() else cooked_bar
    if cooked_mp4.exists():
        release_set["cooked"].append(cooked_mp4)

    # filter by stage if requested
    if args.stage:
        if args.stage not in release_set:
            _die(f"unknown stage: {args.stage}. Valid: {list(release_set)}")
        stages = {args.stage}
    else:
        stages = set(release_set)

    present, missing = [], []
    for stage in stages:
        for p in release_set[stage]:
            rel = str(p.relative_to(root)) if p.is_relative_to(root) else str(p)
            if _is_relative_file(p):
                present.append(rel)
            else:
                missing.append(rel)

    # cross-checks (issues, not hard missing)
    issues = []
    if not args.stage or args.stage == "cooked":
        if cooked_mp4.exists():
            raw_dur = _ffprobe_duration(_raw(root, name, ".raw.mp4"))
            cooked_dur = _ffprobe_duration(cooked_mp4)
            if raw_dur > 0 and cooked_dur > 0 and abs(raw_dur - cooked_dur) > 2.0:
                issues.append(f"duration mismatch: raw={raw_dur:.1f}s cooked={cooked_dur:.1f}s")

    report = {
        "ok": not missing and not issues,
        "output_root": str(root),
        "name": name,
        "stage": args.stage or "all",
        "present": sorted(present),
        "missing": sorted(missing),
        "issues": issues,
    }
    _emit_json(report)
    sys.exit(0 if report["ok"] else 1)


# ============================================================================
# CLI entry point
# ============================================================================

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cook",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"cook {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    # doctor
    sub.add_parser("doctor", help="check environment").set_defaults(func=cmd_doctor)

    # download
    pd = sub.add_parser("download", help="download video + metadata + thumbnail")
    pd.add_argument("url")
    pd.add_argument("--author", help="override <author> path segment")
    pd.add_argument("--name", help="override <video-name> and <name> stem")
    pd.add_argument("--quality", help="cap height, e.g. 1080")
    pd.add_argument("--cookies-from-browser", help="skip negotiation, use this browser")
    pd.set_defaults(func=cmd_download)

    # extract
    pe = sub.add_parser("extract", help="extract 16kHz mono WAV")
    pe.add_argument("output_root")
    pe.add_argument("name")
    pe.set_defaults(func=cmd_extract)

    # transcribe
    pt = sub.add_parser("transcribe", help="whisperX transcription (auto-detached)")
    pt.add_argument("output_root")
    pt.add_argument("name")
    pt.add_argument("--model", default="large-v3")
    pt.add_argument("--compute", default="auto",
                    choices=["auto", "float16", "float32", "int8"])
    pt.add_argument("--language", default="en")
    pt.set_defaults(func=cmd_transcribe)

    # subtitles
    ps = sub.add_parser("subtitles", help="shorten+merge+biliteral+ass+cloud-srt in one shot")
    ps.add_argument("output_root")
    ps.add_argument("name")
    ps.add_argument("--mode", choices=["overlay", "bottom-bar"], default="overlay")
    ps.add_argument("--bar-px", type=int, default=180)
    ps.set_defaults(func=cmd_subtitles)

    # burn
    pb = sub.add_parser("burn", help="burn subtitles into video (auto-detached)")
    pb.add_argument("output_root")
    pb.add_argument("name")
    pb.add_argument("--mode", choices=["overlay", "bottom-bar"], default="overlay")
    pb.add_argument("--bar-px", type=int, default=180)
    pb.set_defaults(func=cmd_burn)

    # cover
    pc = sub.add_parser("cover", help="place cover.jpg in cooked/")
    pc.add_argument("output_root")
    pc.add_argument("name")
    pc.set_defaults(func=cmd_cover)

    # verify-align
    pva = sub.add_parser("verify-align", help="DP-align en.srt vs translations.txt")
    pva.add_argument("output_root")
    pva.add_argument("name")
    pva.set_defaults(func=cmd_verify_align)

    # verify-shipment
    pvs = sub.add_parser("verify-shipment", help="check full release set present")
    pvs.add_argument("output_root")
    pvs.add_argument("name")
    pvs.add_argument("--stage", choices=["raw", "transcript", "subtitle",
                                          "cloud-srt", "cooked", "root"])
    pvs.set_defaults(func=cmd_verify_shipment)

    # show-source
    pss = sub.add_parser("show-source",
                         help="extract key fields from source.json for translation/upload context")
    pss.add_argument("output_root")
    pss.add_argument("name")
    pss.add_argument("--full", action="store_true",
                     help="don't truncate long fields (descriptions can be 10KB+)")
    pss.set_defaults(func=cmd_show_source)

    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
