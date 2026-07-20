#!/bin/sh
# cook CLI installer for Linux and macOS.
#
# Uses uv (https://astral.sh/uv/) to install cook as an isolated tool —
# uv handles Python interpreter version, venv isolation, and entry-point
# PATH wiring automatically. The user does not need to know pip/venv.
#
# This script is uploaded to GitHub Releases as an asset. End users run:
#   curl -LsSf https://github.com/ChHsiching/video-cook/releases/latest/download/install.sh | sh
#
# Installs video-cook[all] — pulls yt-dlp + whisperx + torch (~2GB).
# If you only want the lightweight commands (verify-shipment, show-source,
# etc.), edit this script to drop [all] before running, or run
#   uv tool install video-cook
# manually.

set -e

# 1. Detect or install uv.
if ! command -v uv >/dev/null 2>&1; then
    echo "==> uv not found; installing from https://astral.sh/"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # uv installer writes to ~/.local/bin; bring it into PATH for this shell.
    # The installer also appends to shell rc files for future shells.
    case ":$PATH:" in
        *":$HOME/.local/bin:"*) ;;
        *) export PATH="$HOME/.local/bin:$PATH" ;;
    esac
fi

# 2. Sanity-check uv is now callable.
if ! command -v uv >/dev/null 2>&1; then
    echo "cook install: uv install claimed success but 'uv' is not on PATH." >&2
    echo "Open a new shell (so your rc file reloads PATH) and re-run this script." >&2
    exit 1
fi

echo "==> uv $(uv --version 2>/dev/null | head -1 || echo '(unknown version)')"

# 3. Install cook with all extras. [all] is ~2GB (torch + whisperx);
# quote the bracket so zsh doesn't glob it.
echo "==> Installing video-cook[all] (this pulls whisperx + torch, ~2GB)..."
uv tool install "video-cook[all]"

# 4. Verify the entry point is callable.
# uv tool install puts binaries in ~/.local/bin; ensure it's on PATH.
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) export PATH="$HOME/.local/bin:$PATH" ;;
esac

if ! command -v cook >/dev/null 2>&1; then
    echo "" >&2
    echo "cook install: 'cook' command not found on PATH after install." >&2
    echo "uv installs tools to ~/.local/bin — open a new shell, or run:" >&2
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\"" >&2
    exit 1
fi

echo ""
echo "==> cook $(cook --version)"
echo ""
echo "✓ cook installed. Next step: run 'cook doctor' to check your environment"
echo "  (ffmpeg, node, etc. — cook itself can't install those)."
