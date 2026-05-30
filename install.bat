@echo off
REM ============================================================
REM  KBC Flyer Reader - Windows installer
REM
REM  What this does:
REM    1. Verifies Python 3.10+ is installed
REM    2. Creates a venv in .venv\
REM    3. Installs all Python dependencies
REM    4. Locates Tesseract (PATH or default install dir),
REM       writes its full path into the app's config so the app
REM       does NOT need Tesseract on PATH at runtime
REM    5. Locates Ollama (PATH or default install dir), starts the
REM       background server if it is not already running, and
REM       pulls the default model automatically
REM    6. Removes stale old-version launchers (e.g. a leftover .vbs)
REM    7. Creates the "KBC Flyer Reader" shortcut (book icon, no
REM       console) in this folder and the Start Menu
REM
REM  Re-running this script is safe — it skips work that is already done.
REM ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"
echo.
echo === KBC Flyer Reader installer ===
echo.

set "DEFAULT_MODEL=qwen2.5:3b"
set "OLLAMA_DEFAULT=%LOCALAPPDATA%\Programs\Ollama\ollama.exe"

REM --- 1. Find Python ---
where py >nul 2>nul
if errorlevel 1 (
    where python >nul 2>nul
    if errorlevel 1 (
        echo [X] Python not found on PATH.
        echo     Install Python 3.10+ from https://www.python.org/downloads/
        echo     and check "Add Python to PATH" during install.
        pause
        exit /b 1
    )
    set "PY=python"
) else (
    set "PY=py -3"
)
echo [ok] Python found.

REM --- 2. Create venv ---
if not exist .venv (
    echo Creating virtual environment...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [X] Failed to create venv.
        pause
        exit /b 1
    )
)
echo [ok] Virtual environment ready.

REM --- 3. Install Python deps ---
echo Installing Python packages (this may take a few minutes)...
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install -r requirements.txt
if errorlevel 1 (
    echo [X] pip install failed.
    pause
    exit /b 1
)
echo [ok] Python packages installed.

REM --- 4. Locate Tesseract ---
REM Check PATH first, then every known default install location. The
REM UB Mannheim installer can land in Program Files OR in the per-user
REM %LOCALAPPDATA%\Programs folder depending on whether it was installed
REM for "all users" or "just me".
set "TESS_PATH="
where tesseract >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%i in ('where tesseract') do set "TESS_PATH=%%i"
)
if not defined TESS_PATH (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Tesseract-OCR\tesseract.exe"
        "C:\Program Files\Tesseract-OCR\tesseract.exe"
        "C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"
        "%LOCALAPPDATA%\Tesseract-OCR\tesseract.exe"
    ) do (
        if not defined TESS_PATH if exist "%%~P" set "TESS_PATH=%%~P"
    )
)
if defined TESS_PATH (
    echo [ok] Tesseract found at: !TESS_PATH!
    REM Write the full path into the app's config so the app does not
    REM rely on PATH at runtime. Uses the venv's python.
    python -c "from src.config import Config; c = Config.load(); c.tesseract_cmd = r'!TESS_PATH!'; c.save(); print('     saved tesseract_cmd to config')"
) else (
    echo.
    echo [!] Tesseract OCR is not installed. It is required for image
    echo     flyers and scanned PDFs.
    echo     Download: https://github.com/UB-Mannheim/tesseract/wiki
    echo     During the Tesseract installer, you do NOT need to add it
    echo     to PATH — this script will detect the default location at
    echo     C:\Program Files\Tesseract-OCR\ automatically on re-run.
    choice /m "Open the Tesseract download page now"
    if !errorlevel! == 1 start https://github.com/UB-Mannheim/tesseract/wiki
)

REM --- 5. Locate Ollama ---
set "OLLAMA_PATH="
where ollama >nul 2>nul
if not errorlevel 1 (
    for /f "delims=" %%i in ('where ollama') do set "OLLAMA_PATH=%%i"
)
if not defined OLLAMA_PATH (
    if exist "%OLLAMA_DEFAULT%" set "OLLAMA_PATH=%OLLAMA_DEFAULT%"
)

if not defined OLLAMA_PATH (
    echo.
    echo [!] Ollama is not installed. It is required to run the LLM
    echo     extraction step locally.
    echo     Download: https://ollama.com/download
    choice /m "Open the Ollama download page now"
    if !errorlevel! == 1 start https://ollama.com/download
    echo.
    echo After installing Ollama, re-run install.bat. It will auto-pull
    echo the default model — you do NOT need to run any ollama commands
    echo manually.
    goto :SHORTCUT
)

echo [ok] Ollama found at: !OLLAMA_PATH!

REM --- 5a. Make sure the Ollama server is running before we pull ---
REM Try a quick API ping; if it fails, start the server in the background.
python -c "import urllib.request, sys; r=urllib.request.urlopen('http://localhost:11434/api/tags', timeout=2); sys.exit(0)" >nul 2>nul
if errorlevel 1 (
    echo Starting Ollama server in the background...
    REM Launch the server DETACHED and hidden via PowerShell's Start-Process.
    REM Using `start /b` instead would attach the long-running server to this
    REM console, which keeps the installer's command-prompt window open after
    REM "press any key to continue" (it can't close while a child process is
    REM still living in it). Start-Process gives the server its own detached
    REM process, so this window closes normally and the server keeps running.
    powershell -NoProfile -Command "Start-Process -FilePath '!OLLAMA_PATH!' -ArgumentList 'serve' -WindowStyle Hidden" >nul 2>nul
    REM Give the server a few seconds to come up.
    for /l %%n in (1,1,10) do (
        timeout /t 1 /nobreak >nul
        python -c "import urllib.request, sys; r=urllib.request.urlopen('http://localhost:11434/api/tags', timeout=2); sys.exit(0)" >nul 2>nul
        if not errorlevel 1 goto :SERVER_UP
    )
    echo [!] Ollama server did not respond after 10 seconds.
    echo     You may need to launch Ollama from the Start Menu once
    echo     and then re-run this installer.
    goto :SHORTCUT
)
:SERVER_UP
echo [ok] Ollama server responding.

REM --- 5b. Pull the default model ---
echo Pulling default model %DEFAULT_MODEL% (this is roughly 2 GB)...
"!OLLAMA_PATH!" pull %DEFAULT_MODEL%
if errorlevel 1 (
    echo [!] Model pull failed. You can retry later by running
    echo     "%OLLAMA_PATH%" pull %DEFAULT_MODEL%
) else (
    echo [ok] Model %DEFAULT_MODEL% ready.
)

:OUTPUTDIR
REM --- 5c. Output folder inside the app folder ------------------------
REM A "Flyer Reader Output" folder ships in the zip, so it already exists
REM here; the app defaults to it on first run. We still ensure it exists
REM (in case it was deleted) and pin the absolute path in the config so it
REM stays correct even if the app is later launched from elsewhere.
echo.
echo Setting up the output folder...
set "OUTPUT_DIR=%~dp0Flyer Reader Output"
if not exist "%OUTPUT_DIR%" mkdir "%OUTPUT_DIR%"
python -c "from src.config import Config; c = Config.load(); c.default_output_dir = r'!OUTPUT_DIR!'; c.save(); print('     output folder set to: !OUTPUT_DIR!')"

:SHORTCUT
REM --- 6. Clean up stale launchers from older versions ----------------
REM Earlier builds shipped a .vbs launcher that opens in Notepad on many
REM machines (Windows has been retiring VBScript). If an old copy is still
REM sitting in this folder from a previous unzip, remove it so it can't be
REM clicked by mistake. Same for the old "App"-named batch file.
if exist "%~dp0KBC Flyer Reader App.vbs" (
    del /f /q "%~dp0KBC Flyer Reader App.vbs" >nul 2>nul
    echo [ok] Removed old VBScript launcher.
)
if exist "%~dp0KBC Flyer Reader App.bat" (
    del /f /q "%~dp0KBC Flyer Reader App.bat" >nul 2>nul
    echo [ok] Removed old "App" launcher.
)

REM --- 7. Shortcuts (Start Menu + in-folder), no console window -------
REM The shortcut is the real launcher: it carries the book icon (a .bat or
REM .vbs cannot show a custom icon) and points straight at the venv's
REM pythonw.exe (a windowless Python), so launching shows no terminal at
REM all. WorkingDirectory is the app folder so `-m src.main` resolves.
echo.
echo Creating the "KBC Flyer Reader" shortcut...
set "PYW=%~dp0.venv\Scripts\pythonw.exe"
set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\KBC Flyer Reader.lnk"
set "FOLDERLNK=%~dp0KBC Flyer Reader.lnk"
if not exist "%~dp0assets\icon.ico" echo [!] Book icon not found - shortcut will use the default Python icon.
powershell -NoProfile -Command ^
    "$w=New-Object -ComObject WScript.Shell;" ^
    "foreach($p in @('%SHORTCUT%','%FOLDERLNK%')){" ^
    "  $s=$w.CreateShortcut($p);" ^
    "  $s.TargetPath='%PYW%';" ^
    "  $s.Arguments='-m src.main';" ^
    "  $s.WorkingDirectory='%~dp0';" ^
    "  if(Test-Path '%~dp0assets\icon.ico'){$s.IconLocation='%~dp0assets\icon.ico'};" ^
    "  $s.Description='KBC Flyer Reader';" ^
    "  $s.Save() }" >nul
if exist "%SHORTCUT%" (echo [ok] Start Menu shortcut created.) else (echo [!] Start Menu shortcut skipped.)
if exist "%FOLDERLNK%" (echo [ok] Folder shortcut "KBC Flyer Reader" created.) else (echo [!] Folder shortcut skipped.)

echo.
echo ============================================================
echo  Done.  Launch with the "KBC Flyer Reader" shortcut (it has
echo  the book icon, in this folder and the Start Menu). It opens
echo  with no console window.
echo ============================================================
pause
