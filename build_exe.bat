@echo off
REM Build a standalone Windows executable using PyInstaller.
REM Output lands in dist\"KBC Flyer Reader"\ — ship the whole folder.
REM The launcher inside is "KBC Flyer Reader.exe" with the book icon.
cd /d "%~dp0"
if not exist .venv\Scripts\activate.bat (
    echo Run install.bat first.
    pause
    exit /b 1
)
call .venv\Scripts\activate.bat
python -m pip install pyinstaller >nul
rmdir /s /q build 2>nul
rmdir /s /q dist 2>nul
pyinstaller flyer_reader.spec
echo.
echo Done. Distributable folder: dist\"KBC Flyer Reader"\
echo Launch it with "KBC Flyer Reader.exe" (book icon, no console).
echo Recipients still need to install Tesseract and Ollama separately.
pause
