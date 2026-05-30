#!/usr/bin/env bash
# Launch the KBC Flyer Reader GUI.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
    echo "Virtual environment not found. Run ./install.sh first."
    exit 1
fi
# shellcheck disable=SC1091
source .venv/bin/activate
python -m src.main "$@"
