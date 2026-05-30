; ===========================================================================
;  KBC Flyer Reader — Windows installer (Inno Setup)  [BUNDLED build]
;
;  Produces a single setup.exe with these wizard pages:
;    1. Welcome
;    2. "Where would you like to install the files to?"  (+ a
;       "Create a desktop shortcut" checkbox, checked by default)
;    3. "Where would you like to save output files to?"  (custom page)
;    4. Install progress -> Finish (with a "Launch" option)
;
;  This is the SELF-CONTAINED variant: it bundles a pre-built copy of the
;  app (made with PyInstaller) inside setup.exe. The end user needs NO
;  Python and NO internet to install or run it. (They still need Tesseract
;  for OCR and, for local extraction, Ollama -- same as before.)
;
;  ---------------------------------------------------------------------------
;  BUILD STEPS (do these on a Windows PC):
;
;    1. Build the app with PyInstaller (one time per release):
;          py -3 -m venv build-venv
;          build-venv\Scripts\activate
;          pip install -r requirements.txt pyinstaller
;          pyinstaller flyer_reader.spec
;       This creates  dist\KBC Flyer Reader\  containing
;       "KBC Flyer Reader.exe" and all its dependencies.
;
;    2. Build the installer:
;          iscc installer.iss
;       (or open this file in Inno Setup and press Build.)
;
;    The finished installer is  installer_output\KBC-Flyer-Reader-Setup.exe
;  ---------------------------------------------------------------------------
; ===========================================================================

#define MyAppName "KBC Flyer Reader"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "KBC"
; The PyInstaller-built executable inside dist\KBC Flyer Reader\.
#define MyAppExeName "KBC Flyer Reader.exe"
; Where PyInstaller put the built app (the [Files] section copies all of it).
#define BuiltAppDir "dist\KBC Flyer Reader"

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
PrivilegesRequiredOverridesAllowed=dialog commandline
SetupIconFile=assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; Desktop shortcut checkbox -- checked by default (no 'unchecked' flag).
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"

[Files]
; Bundle the entire PyInstaller output folder. recursesubdirs + createallsubdirs
; copies the .exe plus every dependency, the templates, assets, and the
; "Flyer Reader Output" folder that ships with the app.
Source: "{#BuiltAppDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
; Shortcuts point straight at the bundled .exe -- no console window, real icon.
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; WorkingDir: "{app}"; Tasks: desktopicon

[Run]
; Offer to launch at the end (Finish page checkbox).
Filename: "{app}\{#MyAppExeName}"; Description: "Launch {#MyAppName}"; WorkingDir: "{app}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\_internal"

[Code]
var
  OutputDirPage: TInputDirWizardPage;

procedure InitializeWizard;
begin
  { Custom page 3: where to save output files. The install-location page
    (page 2) and its desktop-shortcut checkbox are provided by Inno Setup
    via DefaultDirName + the [Tasks] entry above. }
  OutputDirPage := CreateInputDirPage(
    wpSelectDir,
    'Select Output Folder',
    'Where would you like to save output files to?',
    'KBC Flyer Reader will save your exported survey spreadsheets to the' + #13#10 +
    'folder below. You can change this later in the app''s Settings.',
    False, '');
  OutputDirPage.Add('');
  { Default to a "Flyer Reader Output" subfolder of the install directory. }
  OutputDirPage.Values[0] := ExpandConstant('{autopf}\{#MyAppName}\Flyer Reader Output');
end;

{ After files are copied: create the chosen output folder and record it in
  the app's config so the app saves there by default. The bundled app reads
  its config from %APPDATA%\FlyerReader\config.json; we write that file
  directly here (simple JSON with just the output dir; the app fills in the
  rest of its defaults on first load). }
procedure CurStepChanged(CurStep: TSetupStep);
var
  OutDir, ConfigDir, ConfigPath, JsonOut: string;
begin
  if CurStep <> ssPostInstall then
    Exit;

  OutDir := OutputDirPage.Values[0];
  ForceDirectories(OutDir);

  ConfigDir := ExpandConstant('{userappdata}\FlyerReader');
  ForceDirectories(ConfigDir);
  ConfigPath := AddBackslash(ConfigDir) + 'config.json';

  { Only write if there isn't already a config (don't clobber a returning
    user's settings). JSON needs backslashes escaped. }
  if not FileExists(ConfigPath) then
  begin
    StringChangeEx(OutDir, '\', '\\', True);
    JsonOut := '{' + #13#10 +
               '  "default_output_dir": "' + OutDir + '"' + #13#10 +
               '}' + #13#10;
    SaveStringToFile(ConfigPath, JsonOut, False);
  end;
end;
