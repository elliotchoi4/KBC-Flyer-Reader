"""Application configuration. Adjust defaults here or via the Settings dialog."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path


def bundle_root() -> Path:
    """
    Where bundled, read-only resources live (templates, assets).

    - Frozen: PyInstaller's extraction dir (sys._MEIPASS) when present,
      else the executable's folder.
    - Source: the parent of the `src/` directory.
    """
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def app_root() -> Path:
    """
    The application's on-disk home for USER-FACING paths (e.g. the default
    output folder).

    - Frozen: the folder containing the .exe (so the output folder sits
      next to the program the user installed, NOT inside PyInstaller's
      hidden _internal/_MEIPASS area).
    - Source: the parent of the `src/` directory.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def user_data_dir() -> Path:
    """Where user-writable config + output files live (NOT inside _MEIPASS)."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    d = base / "FlyerReader"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Config:
    # --- Provider selection -------------------------------------------------
    # "claude" = Anthropic API (needs an API key + internet; far more
    #            accurate, no local model needed). This is the default.
    # "ollama" = local model (free, private, slower, capability-limited).
    provider: str = "claude"

    # --- Ollama (local) settings -------------------------------------------
    ollama_base_url: str = "http://localhost:11434/v1"
    # Default model. qwen2.5:3b (~1.9GB) is fast on a Surface-class laptop.
    # With two-phase extraction (segment, then one focused call per building)
    # even a 3B model fills the schema well, because each call is a small,
    # tractable task. Heavier/more accurate but much slower: qwen2.5:7b,
    # llama3.1:8b. Lighter: llama3.2:1b.
    ollama_model: str = "qwen2.5:3b"
    # How long to wait for ONE Ollama call (segmentation OR per-building
    # extraction). 3600s (1 hour) is deliberately generous: on a Surface-class
    # CPU a 7B model's first call can take 5-15 minutes for cold-start +
    # inference, and a long multi-building flyer can push past that. With
    # keep_alive="30m" set on the request, subsequent calls in the same run
    # are far faster, so this only really comes into play for the first call.
    ollama_timeout_seconds: int = 3600

    # --- Claude (API) settings ---------------------------------------------
    # The Claude API key is a secret. On Windows it is stored in the Windows
    # Credential Manager (encrypted, per-user) — NOT in this JSON file — and
    # this field stays empty. On platforms without Credential Manager (or if
    # it is unavailable) the key falls back to being stored in this field in
    # config.json. As a final fallback the app also reads a plain-text
    # "claude-key" file in claude_key_dir. See persist_claude_key() /
    # read_claude_key() for the full priority order.
    claude_api_key: str = ""
    claude_key_dir: str = field(
        default_factory=lambda: str(Path.home() / "Documents" / "Flyer Reader")
    )
    # Show the "Getting Started" guide on startup until the user ticks
    # "Do not show this again".
    show_getting_started: bool = True
    # Set once the user has acknowledged the Claude privacy notice on
    # launch, so it isn't shown on every startup (it still appears when the
    # user actively switches the provider to Claude).
    claude_privacy_ack: bool = False
    # Anthropic model string used when provider == "claude".
    claude_model: str = "claude-sonnet-4-6"

    # --- Shared settings ----------------------------------------------------
    max_parallel_extractions: int = 2
    ocr_engine: str = "pytesseract"     # or "easyocr"
    tesseract_cmd: str = ""              # leave blank to use system PATH
    # Threshold below which a field is highlighted yellow in the output Excel.
    low_confidence_threshold: float = 0.8
    # When True, write the OCR/extracted text and the raw LLM result into a
    # _debug/ folder beside the output. Useful for diagnosing empty output.
    debug_dump: bool = True
    # Where to place output files when the user does not pick one.
    # Default output folder: the "Flyer Reader Output" folder shipped next
    # to the app. Resolved relative to the app root so it points inside the
    # KBC Flyer Reader folder on first run, before the user changes it.
    default_output_dir: str = field(
        default_factory=lambda: str(app_root() / "Flyer Reader Output"))

    # Filenames accepted for the Claude key file, in priority order.
    # Windows hides known extensions, so a user who makes a "text document"
    # actually creates "claude-key.txt" even though Explorer shows
    # "claude-key". We accept both, plus a couple of common variants, so
    # the user does not have to fight the extension.
    _CLAUDE_KEY_NAMES = (
        "claude-key", "claude-key.txt", "claude_key", "claude_key.txt",
        "claude-key.text", "claudekey.txt", "claudekey",
    )

    def claude_key_path(self) -> Path:
        """
        Full path to the Claude key file.

        Returns the first existing candidate filename in the configured
        folder. If none exists yet, returns the canonical 'claude-key'
        path (used for the "file not found" message).
        """
        folder = Path(self.claude_key_dir)
        # 1. Exact-name match against the accepted variants.
        for name in self._CLAUDE_KEY_NAMES:
            cand = folder / name
            if cand.is_file():
                return cand
        # 2. Case-insensitive scan, in case of e.g. "Claude-Key.TXT".
        try:
            wanted = {n.lower() for n in self._CLAUDE_KEY_NAMES}
            for entry in folder.iterdir():
                if entry.is_file() and entry.name.lower() in wanted:
                    return entry
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            pass
        # 3. Nothing found — return the canonical path for the error message.
        return folder / "claude-key"

    # Credential Manager target name (Windows). Per-user, encrypted vault.
    _CRED_TARGET = "KBCFlyerReader/ClaudeAPIKey"

    def _cred_key(self) -> str:
        """Return the key from the Windows Credential Manager, or '' ."""
        try:
            from . import credential_store as cs
            if cs.is_available():
                val = cs.retrieve_secret(self._CRED_TARGET)
                return (val or "").strip()
        except Exception:
            pass
        return ""

    def _file_key(self) -> str:
        """Return the key from the back-compat claude-key file, or '' ."""
        kp = self.claude_key_path()
        try:
            if kp.is_file():
                return kp.read_text(encoding="utf-8").strip()
        except OSError:
            pass
        return ""

    def has_claude_key(self) -> bool:
        """True if a Claude API key is available from any backend."""
        return bool(
            self.claude_api_key.strip()
            or self._cred_key()
            or self._file_key()
        )

    def claude_key_for_display(self) -> str:
        """Best-effort key for pre-filling the Settings field; '' if none."""
        try:
            return self.read_claude_key()
        except Exception:
            return ""

    def claude_key_source(self) -> str:
        """Human-readable description of where the active key comes from."""
        if self.claude_api_key.strip():
            return "config file"
        if self._cred_key():
            return "Windows Credential Manager"
        if self._file_key():
            return "claude-key file"
        return "none"

    def persist_claude_key(self, key: str) -> str:
        """
        Save (or clear) the Claude API key using the most secure backend
        available, and update this Config's in-memory state accordingly.
        Does NOT call save(); the caller persists the JSON afterwards.

        Returns a short description of where the key was stored.

        - Windows + Credential Manager available: store in the vault and
          keep claude_api_key empty so it never lands in config.json.
        - Otherwise: store in claude_api_key (written to config.json by
          save()).
        Passing an empty key clears the credential everywhere.
        """
        key = (key or "").strip()
        try:
            from . import credential_store as cs
            cred_ok = cs.is_available()
        except Exception:
            cs = None
            cred_ok = False

        if not key:
            # Clear from both backends.
            if cred_ok:
                try:
                    cs.delete_secret(self._CRED_TARGET)
                except Exception:
                    pass
            self.claude_api_key = ""
            return "cleared"

        if cred_ok and cs.store_secret(self._CRED_TARGET, key):
            # Stored securely — make sure it is NOT also in the JSON.
            self.claude_api_key = ""
            return "Windows Credential Manager"

        # Fallback: keep it in the config field (saved to config.json).
        self.claude_api_key = key
        return "config file"

    def read_claude_key(self) -> str:
        """
        Return the Claude API key.

        Priority:
          1. An explicit key in config.json (set on non-Windows or when the
             Credential Manager is unavailable; also used for live tests).
          2. The Windows Credential Manager (the secure default on Windows).
          3. Back-compat: a plain-text 'claude-key' file in claude_key_dir.

        Raises a clear, Settings-oriented error if none is available.
        """
        if self.claude_api_key.strip():
            return self.claude_api_key.strip()

        cred = self._cred_key()
        if cred:
            return cred

        file_key = self._file_key()
        if file_key:
            return file_key

        raise ValueError(
            "No Claude API key found.\n\n"
            "Open Settings (the gear button) and paste your Anthropic API "
            "key into the 'Claude API key' field, then click Save.\n\n"
            "Don't have a key? Click the '?' next to that field in Settings "
            "for step-by-step instructions, or ask a team member for an "
            "existing key."
        )

    @classmethod
    def load(cls) -> "Config":
        path = user_data_dir() / "config.json"
        if path.exists():
            try:
                return cls(**{**asdict(cls()), **json.loads(path.read_text())})
            except Exception:
                pass
        return cls()

    def save(self) -> None:
        (user_data_dir() / "config.json").write_text(json.dumps(asdict(self), indent=2))


TEMPLATES_DIR = bundle_root() / "templates"
BUILDING_TEMPLATE = TEMPLATES_DIR / "KBC_Template__Building_Survey_Locked.xlsx"
LAND_TEMPLATE = TEMPLATES_DIR / "KBC_Template__Land_Survey_Locked.xlsx"
