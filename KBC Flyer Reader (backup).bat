@echo off
REM ===================================================================
REM  KBC Flyer Reader — launcher (fallback)
REM
REM  Preferred launch: the "KBC Flyer Reader" shortcut (created by
REM  install.bat, or by running "Create Shortcut.bat" once). That
REM  shortcut opens the app with no console window at all.
REM
REM  This .bat is a fallback that always works. It uses `pythonw` (not
REM  `python`) so the app itself never opens a console window — but
REM  double-clicking a .bat does briefly flash a console, which is why
REM  the shortcut is preferred.
REM
REM  If the virtual environment is missing we pop up a graphical
REM  message box rather than printing to a console.
REM ===================================================================
cd /d "%~dp0"

if not exist .venv\Scripts\activate.bat (
    powershell -NoProfile -WindowStyle Hidden -Command ^
      "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Virtual environment not found.`n`nPlease run install.bat first.','KBC Flyer Reader') | Out-Null"
    exit /b 1
)

call .venv\Scripts\activate.bat
pythonw -m src.main %*
