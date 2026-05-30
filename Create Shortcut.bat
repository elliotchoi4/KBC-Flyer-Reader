@echo off
REM ===================================================================
REM  KBC Flyer Reader — create a no-console shortcut
REM
REM  Run this ONCE (double-click it). It creates a Windows shortcut
REM  named "KBC Flyer Reader.lnk" in this folder that launches the app
REM  with NO console window and no flash.
REM
REM  How it works: the shortcut points straight at the virtual
REM  environment's "pythonw.exe", which is a windowless build of
REM  Python. Windows runs it directly — no batch file and no VBScript
REM  involved — so there is no terminal to hide in the first place.
REM
REM  (This replaces the old .vbs launcher. On many machines .vbs files
REM   open in a text editor instead of running, because Windows has
REM   been phasing VBScript out, so we no longer rely on it.)
REM ===================================================================
cd /d "%~dp0"

REM Remove a leftover .vbs launcher from an older version, if present —
REM on many machines it opens in Notepad instead of running.
if exist "%~dp0KBC Flyer Reader App.vbs" del /f /q "%~dp0KBC Flyer Reader App.vbs" >nul 2>nul

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if not exist "%PYW%" (
    powershell -NoProfile -WindowStyle Hidden -Command ^
      "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Virtual environment not found.`n`nPlease run install.bat first, then run this again.','KBC Flyer Reader') | Out-Null"
    exit /b 1
)

set "LNK=%~dp0KBC Flyer Reader.lnk"
set "ICON=%~dp0assets\icon.ico"

REM Build the shortcut. Include the icon only if one is present.
if exist "%ICON%" (
    powershell -NoProfile -Command ^
      "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%');" ^
      "$s.TargetPath='%PYW%';" ^
      "$s.Arguments='-m src.main';" ^
      "$s.WorkingDirectory='%~dp0';" ^
      "$s.IconLocation='%ICON%';" ^
      "$s.Description='KBC Flyer Reader';" ^
      "$s.Save()"
) else (
    powershell -NoProfile -Command ^
      "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%');" ^
      "$s.TargetPath='%PYW%';" ^
      "$s.Arguments='-m src.main';" ^
      "$s.WorkingDirectory='%~dp0';" ^
      "$s.Description='KBC Flyer Reader';" ^
      "$s.Save()"
)

if exist "%LNK%" (
    powershell -NoProfile -WindowStyle Hidden -Command ^
      "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Created the ""KBC Flyer Reader"" shortcut (with the book icon) in this folder.`n`nDouble-click that shortcut from now on — it opens the app with no console window.','KBC Flyer Reader') | Out-Null"
) else (
    powershell -NoProfile -WindowStyle Hidden -Command ^
      "Add-Type -AssemblyName PresentationFramework; [System.Windows.MessageBox]::Show('Could not create the shortcut. You can still launch with ""KBC Flyer Reader (backup).bat"".','KBC Flyer Reader') | Out-Null"
)
