"""
Update checker — compares the running version against the latest GitHub
release and reports whether a newer one exists.

Design goals:
  - Never block or break startup. All network work runs on a background
    thread with a short timeout; any failure is swallowed and treated as
    "no update available".
  - No third-party dependencies — uses urllib from the standard library.
  - The GUI decides how to prompt; this module only answers
    "is there a newer version, and where is it?".
"""
from __future__ import annotations

import json
import logging
import re
import urllib.request
from dataclasses import dataclass
from typing import Optional

from .version import VERSION, GITHUB_OWNER_REPO

log = logging.getLogger("flyer_reader")

_TIMEOUT_SECONDS = 6


@dataclass
class UpdateInfo:
    current: str
    latest: str
    url: str            # release page the user can open to download
    notes: str = ""     # release body / changelog (may be empty)


def _parse_version(v: str) -> tuple:
    """
    Turn 'v1.2.3' or '1.2.3' into a comparable tuple (1, 2, 3). Non-numeric
    suffixes are ignored. Returns () if nothing parseable, which sorts lowest.
    """
    v = v.strip().lstrip("vV")
    parts = re.findall(r"\d+", v)
    return tuple(int(p) for p in parts) if parts else ()


def is_newer(latest: str, current: str) -> bool:
    """True if `latest` is a strictly higher version than `current`."""
    lt, ct = _parse_version(latest), _parse_version(current)
    # Pad to equal length so (1,2) vs (1,2,0) compares correctly.
    n = max(len(lt), len(ct))
    lt += (0,) * (n - len(lt))
    ct += (0,) * (n - len(ct))
    return lt > ct


def _fetch_latest_release(owner_repo: str) -> Optional[dict]:
    url = f"https://api.github.com/repos/{owner_repo}/releases/latest"
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "KBC-Flyer-Reader-UpdateCheck",
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT_SECONDS) as resp:
        if resp.status != 200:
            return None
        return json.loads(resp.read().decode("utf-8"))


def check_for_update(owner_repo: Optional[str] = None,
                     current: Optional[str] = None) -> Optional[UpdateInfo]:
    """
    Return UpdateInfo if a newer release exists on GitHub, else None.

    Swallows every error (offline, rate-limited, repo not found, no
    releases yet, malformed JSON) and returns None so callers can treat
    "couldn't check" the same as "up to date".
    """
    owner_repo = owner_repo or GITHUB_OWNER_REPO
    current = current or VERSION

    # Guard against the unedited placeholder so we don't hammer a 404.
    if not owner_repo or owner_repo.startswith("YOUR_GITHUB_USERNAME"):
        log.info("Update check skipped: GITHUB_OWNER_REPO not configured.")
        return None

    try:
        data = _fetch_latest_release(owner_repo)
        if not data:
            return None
        tag = str(data.get("tag_name") or data.get("name") or "").strip()
        if not tag:
            return None
        if is_newer(tag, current):
            return UpdateInfo(
                current=current,
                latest=tag.lstrip("vV"),
                url=str(data.get("html_url")
                        or f"https://github.com/{owner_repo}/releases/latest"),
                notes=str(data.get("body") or "")[:1000],
            )
        return None
    except Exception as e:
        log.info("Update check failed (treating as up to date): %s", e)
        return None
