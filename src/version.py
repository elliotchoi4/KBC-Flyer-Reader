"""
Single source of truth for the application version.

Bump VERSION on every release. The installer reads it (via the build
steps in INSTALLER.md) and the in-app update checker compares it against
the latest GitHub release/tag to tell the user when an update is available.

Use a plain "MAJOR.MINOR.PATCH" string (semantic versioning).
"""
from __future__ import annotations

VERSION = "1.0.0"

# GitHub repository in "owner/repo" form. EDIT THIS to your repo after you
# create it (e.g. "jdoe/kbc-flyer-reader"). The update checker queries
# https://api.github.com/repos/<OWNER_REPO>/releases/latest
GITHUB_OWNER_REPO = "YOUR_GITHUB_USERNAME/kbc-flyer-reader"
