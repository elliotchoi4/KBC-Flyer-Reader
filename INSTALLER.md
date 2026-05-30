# Publishing KBC Flyer Reader & Building the Installer

This guide covers three things:

1. Uploading the project to GitHub
2. Building the Windows installer (`setup.exe`) with Inno Setup
3. How the in-app update check works and how to ship new versions

---

## Part 1 — Upload the project to GitHub (using GitHub Desktop)

You only do this first-time setup once. After that, publishing an update is
a couple of clicks (see Part 3).

We'll use **GitHub Desktop**, a free app with buttons — no typing commands
in a terminal. (If you'd rather use the command line, the old `git` commands
still work; this guide just doesn't need them.)

### 1.1 Create a GitHub account (skip if you have one)

1. Go to <https://github.com>.
2. Click **Sign up** and follow the prompts (email, password, username).
   The username you pick becomes part of your repo address, e.g. if your
   username is `jsmith`, your project will live at
   `github.com/jsmith/kbc-flyer-reader`. It's free.

### 1.2 Install GitHub Desktop

1. Go to <https://desktop.github.com>.
2. Click **Download for Windows**. When it finishes, run the downloaded
   file. It installs and opens on its own — there are no options to choose.
3. When it opens, click **Sign in to GitHub.com** and log in with the
   account from step 1.1. Your browser will pop up to confirm — click
   **Authorize**, then return to the app.
4. If it asks for "Git config" / your name and email, just click
   **Continue** / **Finish** (the defaults are fine).

### 1.3 Point the code at your repo (edit two lines)

Before uploading, tell the code which GitHub repo is yours. Open these two
files in any text editor (Notepad is fine) and change the placeholder
`YOUR_GITHUB_USERNAME/kbc-flyer-reader` to use **your** username:

- **`src/version.py`** — find the line:
  ```
  GITHUB_OWNER_REPO = "YOUR_GITHUB_USERNAME/kbc-flyer-reader"
  ```
  Change it to, e.g.:
  ```
  GITHUB_OWNER_REPO = "jsmith/kbc-flyer-reader"
  ```

- **`installer.iss`** — find the line near the top:
  ```
  #define GitHubOwnerRepo "YOUR_GITHUB_USERNAME/kbc-flyer-reader"
  ```
  Change `YOUR_GITHUB_USERNAME` to your username the same way.

Keep the repo name (`kbc-flyer-reader`) the same in both, and remember it
for the next step. Save both files.

### 1.4 Add the project to GitHub Desktop

1. In GitHub Desktop, click **File → Add local repository**
   (or the **Add** button → **Add existing repository**).
2. Click **Choose…** and browse to your project folder — the one that
   contains the `src` folder, `README.md`, and `installer.iss`. Select it
   and click **Select Folder**.
3. GitHub Desktop will say *"This directory does not appear to be a Git
   repository. Would you like to create a repository here instead?"* —
   click the **create a repository** link.
4. A form appears. Set:
   - **Name:** `kbc-flyer-reader` (must match what you put in the two files
     above).
   - **Description:** optional, e.g. "Extracts real-estate flyer data into
     KBC survey templates."
   - Leave **Git ignore** and **License** as **None** (the project already
     has a `.gitignore`).
5. Click **Create repository**.

### 1.5 Publish it to GitHub

1. GitHub Desktop now shows your files as a first "commit." In the bottom-
   left, there's a **Summary** box — type something like
   `Initial commit` — then click the blue **Commit to main** button.
2. At the top, click **Publish repository**.
3. In the dialog:
   - Confirm the **Name** is `kbc-flyer-reader`.
   - **Leave "Keep this code private" UNticked (public).** The in-app update
     check reads your repo's public Releases over the internet; a public
     repo keeps that simple and needs no access tokens. (The installer
     itself is self-contained and does *not* download anything — so if you
     truly need the code private, the only thing affected is the update
     check, which we can point elsewhere. Tell me if so.)
   - Click **Publish repository**.
4. After a moment, go to `https://github.com/YOUR_USERNAME/kbc-flyer-reader`
   in your browser — your files should all be there.

> **What got uploaded:** a `.gitignore` is included, so throwaway files
> (`.venv/`, `__pycache__/`, `installer_output/`, your local `config.json`)
> are automatically left out. Your templates and the app code are included.

---

## Part 2 — Build the Windows installer

The installer is defined by `installer.iss` and built with **Inno Setup**, a
free, industry-standard Windows installer tool. The installer it produces is
a single `setup.exe` that, when run, shows:

1. **Welcome** page
2. **"Where would you like to install the files to?"** — with a
   **Create a desktop shortcut** checkbox (checked by default)
3. **"Where would you like to save output files to?"**
4. **Install** progress → **Finish** (with an option to launch the app)

This is the **self-contained** build: it bundles a pre-built copy of the app
inside `setup.exe`. The **end user needs no Python and no internet** to
install or run it. (They still need Tesseract for OCR and, for local
extraction, Ollama — the app's Getting Started page explains this.)

Building is **two steps on a Windows PC**: first build the app with
PyInstaller, then wrap it with Inno Setup.

### 2.1 Install the build prerequisites (one time, on a Windows PC)

1. **Python 3.11+** — from <https://www.python.org/downloads/windows/>.
   During install, **tick "Add python.exe to PATH"**. (This is only needed
   on *your* build machine, not on end users' machines.)
2. **Inno Setup** — download from <https://jrsoftware.org/isdl.php> and
   install with defaults.

### 2.2 Step 1 — Build the app with PyInstaller

Open **PowerShell** in the project folder (the one with `src/`,
`flyer_reader.spec`, `installer.iss`) and run these lines one at a time:

```
py -3 -m venv build-venv
build-venv\Scripts\activate
pip install -r requirements.txt pyinstaller
pyinstaller flyer_reader.spec
```

When it finishes you'll have a folder:

```
dist\KBC Flyer Reader\
```

containing **`KBC Flyer Reader.exe`** and everything it needs. (You can
double-click that .exe to test the app before packaging, if you like.)

> Re-run only `pyinstaller flyer_reader.spec` for future rebuilds — the venv
> and pip install are one-time. If you ever change the version, bump it in
> both `src/version.py` and the `MyAppVersion` line of `installer.iss`.

### 2.3 Step 2 — Build the installer with Inno Setup

Either:

- **GUI:** double-click `installer.iss`, then in the Inno Setup Compiler
  press **Build → Compile** (or the ▶ button), **or**
- **Command line:** in the project folder run:

  ```
  iscc installer.iss
  ```

  (If `iscc` isn't found, use the full path, e.g.
  `"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer.iss`.)

The finished installer appears at:

```
installer_output\KBC-Flyer-Reader-Setup.exe
```

That single file is what you distribute to KBC staff — they just run it.

### 2.4 What the end user needs

Nothing to pre-install for the app itself — it's fully bundled. For full
functionality they still need **Tesseract OCR** (for scanned/image flyers)
and, only if they use the local engine, **Ollama**. The Claude engine just
needs an API key entered in Settings. None of these are required to install
or open the app.

---

## Part 3 — Updates

### 3.1 How the in-app check works

On startup the app quietly asks GitHub for your repo's **latest release**
and compares it to the version baked into `src/version.py`. If GitHub has a
higher version, the user sees:

> **Update available** — Installed: 1.0.0, Latest: 1.1.0. Open the download
> page to update now?

Clicking **Yes** opens your repo's releases page in their browser, where you
attach the new `setup.exe`. The check is silent and non-blocking: if they're
offline or the repo can't be reached, nothing happens.

### 3.2 Publishing a new version

1. Make your code changes in the project folder.
2. Open **`src/version.py`** and bump `VERSION` (e.g. `"1.0.0"` →
   `"1.1.0"`). Save the file.
3. In **GitHub Desktop**, your changed files appear on the left. Type a
   short summary in the **Summary** box (e.g. `Release 1.1.0`), click
   **Commit to main**, then click **Push origin** at the top.
4. Rebuild the installer with the new version (Part 2): bump
   `MyAppVersion` in `installer.iss` to match, run `pyinstaller
   flyer_reader.spec`, then `iscc installer.iss`. This gives you a fresh
   `installer_output\KBC-Flyer-Reader-Setup.exe`.
5. Create a **GitHub Release** so the update check notices the new version
   and users have something to download:
   - In your browser, go to your repo page and click **Releases** (right
     side) → **Draft a new release** (or **Create a new release**).
   - Click **Choose a tag**, type **`v1.1.0`** (must match the version you
     set in step 2 — the checker ignores the leading `v`), and choose
     **Create new tag on publish**.
   - Add a **Release title** (e.g. `v1.1.0`) and any notes describing what
     changed.
   - **Drag `KBC-Flyer-Reader-Setup.exe` into the "Attach binaries" box** so
     users who click through the update prompt can download the new
     installer directly.
   - Click **Publish release**.

That's it — running apps will detect `v1.1.0` on their next launch and
prompt users to update; clicking **Yes** opens this release page where the
new `setup.exe` is attached.

> The update prompt sends users to the release page to download and run the
> new installer, rather than auto-installing. Fully automatic in-place
> updates are possible but riskier (they require the app to overwrite its
> own files while running); the open-the-page approach is the safe,
> conventional choice.
