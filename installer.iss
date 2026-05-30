; ===========================================================================
;  KBC Flyer Reader — Windows installer (Inno Setup)
;
;  Produces a single setup.exe with these wizard pages:
;    1. Welcome
;    2. "Where would you like to install the files to?"  (+ a
;       "Create a desktop shortcut" checkbox, checked by default)
;    3. "Where would you like to save output files to?"  (custom page)
;    4. Install progress -> Finish (with a "Launch" option)
;
;  At install time it DOWNLOADS the application code from your GitHub
;  repository's latest release (a source zip), extracts it, creates a
;  Python virtual environment, installs dependencies, and writes the
;  chosen output folder into the app config.
;
;  BUILD: install Inno Setup (https://jrsoftware.org/isdl.php), then either
;  double-click this .iss and press Build, or run:
;      iscc installer.iss
;  The compiled installer lands in .\installer_output\KBC-Flyer-Reader-Setup.exe
;
;  EDIT the two defines below to match your repo before building.
; ===========================================================================

#define MyAppName "KBC Flyer Reader"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "KBC"
#define MyAppExeName "KBC Flyer Reader.lnk"

; --- Your GitHub repo, "owner/repo" ---
#define GitHubOwnerRepo "elliotchoi4/kbc-flyer-reader"
; The installer downloads the latest committed code from the 'main' branch
; as a zip. (The in-app update check is separate and reads your published
; Releases — see INSTALLER.md Part 3.) Rebuild + redistribute setup.exe when
; you want new installs to get newer code.
#define GitHubZipUrl "https://github.com/" + GitHubOwnerRepo + "/archive/refs/heads/main.zip"

[Setup]
AppId={{8B5F2E10-KBC0-FLYER-READER-0001}}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
OutputDir=installer_output
OutputBaseFilename=KBC-Flyer-Reader-Setup
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; A user-writable install dir avoids needing admin just to run; the app
; writes its venv and output folder inside the install dir.
PrivilegesRequiredOverridesAllowed=dialog commandline
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\assets\icon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Desktop shortcut checkbox — checked by default (no 'unchecked' flag).
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; We don't bundle the app; it is downloaded in [Code]. But we do ship a
; tiny helper so Python can be located, and the icon for shortcuts.
Source: "assets\icon.ico"; DestDir: "{app}\assets"; Flags: ignoreversion

[Icons]
; Start Menu + (optional) desktop shortcut. Both point at the windowless
; Python in the venv created during install, so there's no console window.
Name: "{group}\{#MyAppName}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; \
    Parameters: "-m src.main"; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\.venv\Scripts\pythonw.exe"; \
    Parameters: "-m src.main"; WorkingDir: "{app}"; IconFilename: "{app}\assets\icon.ico"; \
    Tasks: desktopicon

[Run]
; Offer to launch at the end (Finish page checkbox).
Filename: "{app}\.venv\Scripts\pythonw.exe"; Parameters: "-m src.main"; \
    WorkingDir: "{app}"; Description: "Launch {#MyAppName}"; \
    Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\.venv"
Type: filesandordirs; Name: "{app}\__pycache__"

[Code]
var
  OutputDirPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  { Custom page 3: where to save output files. The install-location page
    (page 2) and its desktop-shortcut checkbox are built in by Inno Setup
    via DefaultDirName + the [Tasks] entry above. }
  OutputDirPage := CreateInputDirPage(
    wpSelectDir,
    'Select Output Folder',
    'Where would you like to save output files to?',
    'KBC Flyer Reader will save your exported survey spreadsheets to the' + #13#10 +
    'folder below. You can change this later in the app''s Settings.',
    False, '');
  OutputDirPage.Add('');
  { Default the output folder to a "Flyer Reader Output" subfolder of the
    install directory. }
  OutputDirPage.Values[0] := ExpandConstant('{autopf}\{#MyAppName}\Flyer Reader Output');
end;

function GetOutputDir(Param: string): string;
begin
  Result := OutputDirPage.Values[0];
end;

{ --- Helpers to run a command and wait, surfacing failures ----------------- }
function RunWait(const Filename, Params, WorkingDir: string; var ResultCode: Integer): Boolean;
begin
  Result := Exec(Filename, Params, WorkingDir, SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

{ Find a Python launcher: prefer the 'py' launcher, fall back to 'python'. }
function FindPython(): string;
var
  RC: Integer;
begin
  if Exec('py', '-3 --version', '', SW_HIDE, ewWaitUntilTerminated, RC) and (RC = 0) then
    Result := 'py'
  else
    Result := 'python';
end;

{ Runs after files are copied; does the GitHub download + environment setup. }
procedure CurStepChanged(CurStep: TSetupStep);
var
  AppDir, OutDir, PyExe, Cmd, ZipPath, ExtractDir: string;
  RC: Integer;
begin
  if CurStep <> ssPostInstall then
    Exit;

  AppDir := ExpandConstant('{app}');
  OutDir := OutputDirPage.Values[0];
  PyExe := FindPython();

  { 1. Download the latest code zip from GitHub and extract it into the
       install dir. We use PowerShell (present on stock Win10/11) for a
       robust download + unzip + flatten of the single top-level folder
       that GitHub's zip contains. }
  ZipPath := AddBackslash(AppDir) + 'source.zip';
  Cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
         '$ErrorActionPreference=''Stop'';' +
         'Invoke-WebRequest -Uri ''{#GitHubZipUrl}'' -OutFile ''' + ZipPath + ''';' +
         'Expand-Archive -Path ''' + ZipPath + ''' -DestinationPath ''' + AddBackslash(AppDir) + 'src_download'' -Force;' +
         '$inner = Get-ChildItem -Path ''' + AddBackslash(AppDir) + 'src_download'' | Where-Object {$_.PSIsContainer} | Select-Object -First 1;' +
         'Copy-Item -Path ($inner.FullName + ''\*'') -Destination ''' + AddBackslash(AppDir) + ''' -Recurse -Force;' +
         'Remove-Item -Recurse -Force ''' + AddBackslash(AppDir) + 'src_download'';' +
         'Remove-Item -Force ''' + ZipPath + '''"';
  if not RunWait('powershell.exe', Cmd, AppDir, RC) or (RC <> 0) then
  begin
    MsgBox('Could not download the application from GitHub. Check your internet connection and that the repository is public, then re-run the installer.', mbError, MB_OK);
    Exit;
  end;

  { 2. Create the virtual environment. }
  if not RunWait(PyExe, '-3 -m venv .venv', AppDir, RC) then
    RunWait('python', '-m venv .venv', AppDir, RC);

  { 3. Install dependencies into the venv. }
  Cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
         '& ''' + AddBackslash(AppDir) + '.venv\Scripts\python.exe'' -m pip install --upgrade pip;' +
         '& ''' + AddBackslash(AppDir) + '.venv\Scripts\python.exe'' -m pip install -r ''' + AddBackslash(AppDir) + 'requirements.txt''"';
  if not RunWait('powershell.exe', Cmd, AppDir, RC) or (RC <> 0) then
    MsgBox('Dependencies could not be fully installed. You can run install.bat in the app folder later to retry.', mbInformation, MB_OK);

  { 4. Create the chosen output folder and write it into the app config.
       We write a tiny temp .py file rather than fighting nested-quote
       escaping on the command line. }
  ForceDirectories(OutDir);
  if SaveStringToFile(AddBackslash(AppDir) + '_set_output.py',
       'from src.config import Config' + #13#10 +
       'c = Config.load()' + #13#10 +
       'c.default_output_dir = r"""' + OutDir + '"""' + #13#10 +
       'c.save()' + #13#10, False) then
  begin
    Cmd := '-NoProfile -ExecutionPolicy Bypass -Command "' +
           '& ''' + AddBackslash(AppDir) + '.venv\Scripts\python.exe'' ''' +
           AddBackslash(AppDir) + '_set_output.py''"';
    RunWait('powershell.exe', Cmd, AppDir, RC);
    DeleteFile(AddBackslash(AppDir) + '_set_output.py');
  end;
end;
