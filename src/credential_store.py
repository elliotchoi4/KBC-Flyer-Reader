"""
Secure secret storage backed by the Windows Credential Manager.

The Claude API key is a secret. On Windows we store it as a "generic"
credential in the Credential Manager (the same encrypted vault Windows uses
for saved passwords), scoped to the logged-in user account. This keeps the
key out of the app's plain-text config.json and means another Windows user
on the same machine cannot read it.

Everything here is best-effort and degrades gracefully:
  - On non-Windows platforms, or if the Win32 calls fail for any reason,
    `is_available()` returns False and the caller falls back to storing the
    key in config.json (the previous behaviour).
  - No third-party dependency — this uses ctypes against advapi32.dll.

The user never sees a prompt: reading a credential they own does not require
re-authenticating; Windows returns it transparently within their session.
"""
from __future__ import annotations

import sys
from typing import Optional

# Win32 credential type / persistence constants.
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2

_IS_WINDOWS = sys.platform.startswith("win")

# Lazily-initialised ctypes plumbing (only on Windows).
_advapi32 = None
_kernel32 = None
_structs_ready = False


def _init_win32() -> bool:
    """Set up ctypes signatures once. Returns True if usable."""
    global _advapi32, _kernel32, _structs_ready
    global _CREDENTIAL, _PCREDENTIAL, _FILETIME
    if not _IS_WINDOWS:
        return False
    if _structs_ready:
        return _advapi32 is not None
    try:
        import ctypes
        from ctypes import wintypes

        _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class _FILETIME(ctypes.Structure):
            _fields_ = [
                ("dwLowDateTime", wintypes.DWORD),
                ("dwHighDateTime", wintypes.DWORD),
            ]

        class _CREDENTIAL(ctypes.Structure):
            _fields_ = [
                ("Flags", wintypes.DWORD),
                ("Type", wintypes.DWORD),
                ("TargetName", wintypes.LPWSTR),
                ("Comment", wintypes.LPWSTR),
                ("LastWritten", _FILETIME),
                ("CredentialBlobSize", wintypes.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_byte)),
                ("Persist", wintypes.DWORD),
                ("AttributeCount", wintypes.DWORD),
                ("Attributes", ctypes.c_void_p),
                ("TargetAlias", wintypes.LPWSTR),
                ("UserName", wintypes.LPWSTR),
            ]

        _PCREDENTIAL = ctypes.POINTER(_CREDENTIAL)

        # CredWriteW(PCREDENTIAL, DWORD) -> BOOL
        _advapi32.CredWriteW.argtypes = [_PCREDENTIAL, wintypes.DWORD]
        _advapi32.CredWriteW.restype = wintypes.BOOL
        # CredReadW(LPCWSTR, DWORD, DWORD, PCREDENTIAL*) -> BOOL
        _advapi32.CredReadW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            ctypes.POINTER(_PCREDENTIAL),
        ]
        _advapi32.CredReadW.restype = wintypes.BOOL
        # CredDeleteW(LPCWSTR, DWORD, DWORD) -> BOOL
        _advapi32.CredDeleteW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
        ]
        _advapi32.CredDeleteW.restype = wintypes.BOOL
        # CredFree(PVOID) -> void
        _advapi32.CredFree.argtypes = [ctypes.c_void_p]
        _advapi32.CredFree.restype = None

        _structs_ready = True
        return True
    except Exception:
        _advapi32 = None
        _structs_ready = True
        return False


def is_available() -> bool:
    """True if the Windows Credential Manager backend can be used."""
    return _init_win32()


def store_secret(target: str, secret: str) -> bool:
    """Write/overwrite a generic credential. Returns True on success."""
    if not _init_win32():
        return False
    try:
        import ctypes
        # Store the secret as UTF-16-LE bytes (Windows' native string form).
        blob = secret.encode("utf-16-le")
        blob_buf = ctypes.create_string_buffer(blob, len(blob))

        cred = _CREDENTIAL()
        cred.Flags = 0
        cred.Type = _CRED_TYPE_GENERIC
        cred.TargetName = target
        cred.Comment = "KBC Flyer Reader — Claude API key"
        cred.CredentialBlobSize = len(blob)
        cred.CredentialBlob = ctypes.cast(
            blob_buf, ctypes.POINTER(ctypes.c_byte))
        cred.Persist = _CRED_PERSIST_LOCAL_MACHINE
        cred.AttributeCount = 0
        cred.Attributes = None
        cred.TargetAlias = None
        cred.UserName = target

        ok = _advapi32.CredWriteW(ctypes.byref(cred), 0)
        return bool(ok)
    except Exception:
        return False


def retrieve_secret(target: str) -> Optional[str]:
    """Read a generic credential, or None if absent / on failure."""
    if not _init_win32():
        return None
    try:
        import ctypes
        pcred = _PCREDENTIAL()
        ok = _advapi32.CredReadW(
            target, _CRED_TYPE_GENERIC, 0, ctypes.byref(pcred))
        if not ok:
            return None
        try:
            cred = pcred.contents
            size = int(cred.CredentialBlobSize)
            if size <= 0 or not cred.CredentialBlob:
                return ""
            raw = ctypes.string_at(cred.CredentialBlob, size)
            return raw.decode("utf-16-le", errors="ignore")
        finally:
            _advapi32.CredFree(pcred)
    except Exception:
        return None


def delete_secret(target: str) -> bool:
    """Remove a generic credential. Returns True if deleted or already gone."""
    if not _init_win32():
        return False
    try:
        ok = _advapi32.CredDeleteW(target, _CRED_TYPE_GENERIC, 0)
        return bool(ok)
    except Exception:
        return False
