#!/usr/bin/env bash
# ============================================================
#  KBC Flyer Reader - macOS / Linux installer
#
#  What this does:
#    1. Verifies Python 3.10+ is installed
#    2. Creates a venv in .venv/
#    3. Installs all Python dependencies
#    4. Checks for Tesseract OCR (installs via brew/apt if available)
#    5. Checks for Ollama (offers to run its install script)
#    6. Pulls the default Ollama model if Ollama is running
#
#  Usage:  ./install.sh
# ============================================================
set -e
cd "$(dirname "$0")"

say() { printf "\033[1;36m%s\033[0m\n" "$*"; }
ok()  { printf "\033[1;32m[ok]\033[0m %s\n" "$*"; }
warn() { printf "\033[1;33m[!]\033[0m %s\n" "$*"; }
err() { printf "\033[1;31m[X]\033[0m %s\n" "$*"; }

say "=== KBC Flyer Reader installer ==="

# 1. Python
if ! command -v python3 >/dev/null 2>&1; then
    err "python3 is not installed."
    echo "  macOS:  brew install python@3.11"
    echo "  Linux:  sudo apt install python3 python3-venv python3-pip"
    exit 1
fi
PY_VER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
ok "Python $PY_VER found."

# 2. Venv
if [ ! -d .venv ]; then
    say "Creating virtual environment..."
    python3 -m venv .venv
fi
ok "Virtual environment ready."

# 3. Deps
say "Installing Python packages..."
# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install --upgrade pip >/dev/null
python -m pip install -r requirements.txt
ok "Python packages installed."

# 4. Tesseract
if ! command -v tesseract >/dev/null 2>&1; then
    warn "Tesseract OCR not found. Required for images + scanned PDFs."
    if command -v brew >/dev/null 2>&1; then
        read -rp "Install Tesseract via Homebrew? [Y/n] " ans
        [ "${ans:-Y}" = "Y" ] || [ "${ans:-Y}" = "y" ] && brew install tesseract
    elif command -v apt-get >/dev/null 2>&1; then
        read -rp "Install Tesseract via apt? [Y/n] " ans
        [ "${ans:-Y}" = "Y" ] || [ "${ans:-Y}" = "y" ] && sudo apt-get install -y tesseract-ocr
    else
        warn "No supported package manager found. Install Tesseract manually:"
        echo "  https://tesseract-ocr.github.io/tessdoc/Installation.html"
    fi
else
    ok "Tesseract found."
fi

# 5. Ollama
if ! command -v ollama >/dev/null 2>&1; then
    warn "Ollama not found. Required for the LLM extraction step."
    read -rp "Run the Ollama install script now? [Y/n] " ans
    if [ "${ans:-Y}" = "Y" ] || [ "${ans:-Y}" = "y" ]; then
        curl -fsSL https://ollama.com/install.sh | sh
    else
        echo "  Install later from https://ollama.com/download"
    fi
fi
if command -v ollama >/dev/null 2>&1; then
    ok "Ollama found."
    # Make sure the server is running before we pull.
    if ! curl -fsS --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1; then
        say "Starting Ollama server in the background..."
        nohup ollama serve >/dev/null 2>&1 &
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            sleep 1
            curl -fsS --max-time 2 http://localhost:11434/api/tags >/dev/null 2>&1 && break
        done
    fi
    say "Pulling default model qwen2.5:3b (about 2 GB)..."
    ollama pull qwen2.5:3b || warn "Pull failed. Run 'ollama pull qwen2.5:3b' manually later."
fi

# 6. Output folder inside the app folder.
say "Setting up the output folder..."
OUTPUT_DIR="$(pwd)/Flyer Reader Output"
mkdir -p "$OUTPUT_DIR"
python -c "from src.config import Config; c = Config.load(); c.default_output_dir = r'''$OUTPUT_DIR'''; c.save(); print('  output folder set to:', r'''$OUTPUT_DIR''')" \
    || warn "Could not set the default output folder in config."

echo
say "============================================================"
say " Done.  Launch with:  ./run.sh"
say "============================================================"
