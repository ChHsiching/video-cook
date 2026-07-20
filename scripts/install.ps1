# cook CLI installer for Windows (PowerShell).
#
# Uses uv (https://astral.sh/uv/) to install cook as an isolated tool —
# uv handles Python interpreter version, venv isolation, and entry-point
# PATH wiring automatically. The user does not need to know pip/venv.
#
# This script is uploaded to GitHub Releases as an asset. End users run:
#   irm https://github.com/ChHsiching/video-cook/releases/latest/download/install.ps1 | iex
#
# Installs video-cook[all] — pulls yt-dlp + whisperx + torch (~2GB).
# If you only want the lightweight commands (verify-shipment, show-source,
# etc.), edit this script to drop [all] before running, or run
#   uv tool install video-cook
# manually.

$ErrorActionPreference = "Stop"

function Test-Command {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# 1. Detect or install uv.
if (-not (Test-Command "uv")) {
    Write-Host "==> uv not found; installing from https://astral.sh/"
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
    # uv installer puts the binary in %USERPROFILE%\.local\bin; bring into PATH
    # for this shell. The installer also updates user PATH for future shells.
    $localBin = Join-Path $env:USERPROFILE ".local\bin"
    if (-not ($env:Path -split ';' -contains $localBin)) {
        $env:Path = "$localBin;$env:Path"
    }
}

# 2. Sanity-check uv is now callable.
if (-not (Test-Command "uv")) {
    Write-Error "cook install: uv install claimed success but 'uv' is not on PATH. Open a new shell (so your PATH reloads) and re-run this script."
    exit 1
}

Write-Host "==> uv $(try { (uv --version 2>`$null | Select-Object -First 1) } catch { '(unknown version)' })"

# 3. Install cook with all extras. [all] is ~2GB (torch + whisperx).
Write-Host "==> Installing video-cook[all] (this pulls whisperx + torch, ~2GB)..."
uv tool install "video-cook[all]"

# 4. Verify the entry point is callable.
$localBin = Join-Path $env:USERPROFILE ".local\bin"
if (-not ($env:Path -split ';' -contains $localBin)) {
    $env:Path = "$localBin;$env:Path"
}

if (-not (Test-Command "cook")) {
    Write-Host ""
    Write-Error "cook install: 'cook' command not found on PATH after install. uv installs tools to %USERPROFILE%\.local\bin — open a new shell, or run:`n  `$env:Path = `"$localBin;`$env:Path`""
    exit 1
}

Write-Host ""
Write-Host "==> cook $(cook --version)"
Write-Host ""
Write-Host "✓ cook installed. Next step: run 'cook doctor' to check your environment"
Write-Host "  (ffmpeg, node, etc. — cook itself can't install those)."
