# Desktop Monitor — Facial Recognition Security System

A real-time facial recognition security application for Windows that monitors a camera feed, identifies known individuals, and triggers configurable lockdown actions when a flagged person is detected. Built on OpenCV's YuNet (detection) and SFace (recognition) models with TUF-secured over-the-air updates.

---

## Table of Contents

- [Overview](#overview)
- [System Requirements](#system-requirements)
- [Project Structure](#project-structure)
- [Setup](#setup)
  - [Python Environment](#python-environment)
  - [Models](#models)
  - [Face Database](#face-database)
- [Configuration](#configuration)
- [Running the Application](#running-the-application)
- [Build & Release Pipeline](#build--release-pipeline)
  - [Local Build (Nuitka)](#local-build-nuitka)
  - [TUF Release Signing](#tuf-release-signing)
  - [Windows Installer (Inno Setup)](#windows-installer-inno-setup)
  - [CI/CD — GitHub Actions](#cicd--github-actions)
  - [Setting Up GitHub Secrets](#setting-up-github-secrets)
- [Update System (TUF)](#update-system-tuf)
- [Architecture](#architecture)
  - [Detection & Recognition Pipeline](#detection--recognition-pipeline)
  - [Threat Response](#threat-response)
  - [Performance Optimisations](#performance-optimisations)

---

## Overview

Desktop Monitor captures frames from a connected camera, runs face detection and recognition against a local database, and responds based on classification:

- **Good** — recognised, no action taken.
- **Bad** — triggers a full-screen splash overlay across all monitors and blocks all keyboard/mouse input via the Win32 `BlockInput` API until the person leaves the frame.
- **Unknown** — unrecognised face, no action taken.

The application ships as a standalone Windows executable compiled with Nuitka, distributed through an Inno Setup installer, and receives signed delta updates via TUF (The Update Framework).

---

## System Requirements

| Requirement | Detail                                                    |
| ----------- | --------------------------------------------------------- |
| OS          | Windows 10 or later (64-bit)                              |
| Python      | 3.12 (development only — not required for installed app)  |
| Camera      | Any DirectShow or Media Foundation compatible device      |
| Privileges  | Administrator (required for `BlockInput` system lockdown) |
| Disk        | ~200 MB (compiled application with models)                |

---

## Project Structure

```
├── main.py              # Entry point — recognition loop, enrolment, CLI
├── app_config.py         # Shared constants: APP_NAME, APP_VERSION, paths
├── settings_gui.py       # Tkinter dark-themed settings panel & launcher
├── settings.json         # Persisted user configuration
├── splash.py             # Full-screen splash overlay (image/GIF/video)
├── input_locker.py       # Win32 BlockInput wrapper for system lockdown
├── camera_enum.py        # DirectShow COM camera enumeration (ctypes)
├── updater.py            # TUF-based auto-update client
├── build.py              # Nuitka compilation script
├── release.py            # TUF metadata signing & bundle creation
├── release_keys.py       # Key passwords (git-ignored, never committed)
├── installer.py          # Inno Setup / AppImage installer generator
├── requirements.txt      # Python dependencies
├── models/               # ONNX models (YuNet + SFace)
├── faces_db/             # Face database
│   ├── good/<Name>/      #   Authorised individuals
│   └── bad/<Name>/       #   Flagged individuals
├── splash_assets/        # Media displayed during lockdown
├── Keys/                 # TUF ed25519 private keys (git-ignored)
├── my-tuf-repo/          # Local TUF repository (metadata + targets)
└── .github/workflows/    # CI/CD pipeline
    └── release.yml
```

---

## Setup

### Python Environment

```powershell
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Nuitka is only required for compilation and is intentionally excluded from `requirements.txt`:

```powershell
pip install nuitka
```

### Models

Two ONNX models are required in the `models/` directory. Download them automatically:

```powershell
python main.py --download-models
```

This pulls:

- **YuNet** (`face_detection_yunet_2023mar.onnx`) — face detection
- **SFace** (`face_recognition_sface_2021dec.onnx`) — face feature extraction

Both are sourced from the [OpenCV Model Zoo](https://github.com/opencv/opencv_zoo).

### Face Database

The database is organised under `faces_db/` with two categories:

```
faces_db/
├── good/
│   ├── Alice/
│   │   ├── 001.jpg
│   │   ├── 002.jpg
│   │   └── ...
│   └── Bob/
└── bad/
    └── Martin/
        ├── 001.jpg
        └── ...
```

**Manual enrolment** — place 3–10 clear face images per person in the appropriate subdirectory.

**Camera enrolment** — capture images interactively:

```powershell
python main.py --enroll "Alice" --enroll-category good --enroll-shots 5 --camera 0
```

Press `SPACE` to capture each frame, `ESC` to cancel. The application guides face alignment on-screen.

---

## Configuration

All runtime settings are stored in `settings.json` and can be modified through the GUI or edited directly:

| Setting                | Default  | Description                                                        |
| ---------------------- | -------- | ------------------------------------------------------------------ |
| `camera_index`         | `0`      | OpenCV camera index                                                |
| `resolution_w/h`       | 1280×720 | Capture resolution                                                 |
| `fps_cap`              | `15`     | Maximum frame rate (0 = unlimited)                                 |
| `detection_delay`      | `0.5`    | Seconds of continuous detection before triggering lockdown         |
| `idle_skip_pct`        | `70`     | Percentage of frames skipped when no threat is active              |
| `alert_sustain`        | `1.0`    | Seconds to maintain lockdown after the flagged person leaves frame |
| `detection_resolution` | `640`    | Max dimension for detection downscale (lower = faster)             |
| `cosine_threshold`     | `0.55`   | Minimum cosine similarity to consider a match                      |
| `bad_splash_threshold` | `0.45`   | Minimum similarity to trigger splash for bad persons               |
| `size_margin`          | `1.05`   | Bad face must be this factor larger than any good face to trigger  |
| `show_splash`          | `true`   | Whether to display the full-screen splash overlay                  |
| `headless`             | `false`  | Run without the OpenCV preview window                              |
| `run_in_background`    | `false`  | Minimise to background on launch                                   |
| `start_on_boot`        | `false`  | Register in Windows startup (via `HKCU\...\Run` registry key)      |

---

## Running the Application

**With GUI** (default) — opens the settings panel where you configure and launch:

```powershell
python main.py
```

**Headless mode** — runs the recognition pipeline without any preview window:

```powershell
python main.py --no-gui --headless --camera 0
```

**Full CLI reference:**

```
python main.py [-h] [--camera N] [--width W] [--height H]
               [--threshold T] [--bad-threshold T] [--size-margin M]
               [--fps-cap N] [--detection-delay S] [--idle-skip-pct P]
               [--detection-resolution D] [--alert-sustain S]
               [--headless] [--no-gui]
               [--download-models]
               [--enroll NAME] [--enroll-shots N] [--enroll-category {good,bad}]
```

---

## Build & Release Pipeline

The project uses a three-stage pipeline: **compile → sign → package**.

### Local Build (Nuitka)

Nuitka compiles the Python source into a standalone C-compiled executable with all dependencies bundled.

```powershell
python build.py
```

A Tkinter dialog prompts for the version number and build mode (production/test). For CI or scripted builds:

```powershell
python build.py --version 0.1.5 --prod
```

**Production mode** adds:

- Console window disabled (`--windows-console-mode=disable`)
- Application icon embedded (`--windows-icon-from-ico`)
- Deployment flag (strips debug paths)

**Output:** `dist/main.dist/` — a self-contained directory with `main.exe` and all dependencies.

Key Nuitka flags used:

- `--standalone` — bundles the Python runtime and all imports
- `--enable-plugin=tk-inter` — includes Tkinter/Tcl for the settings GUI
- `--windows-uac-admin` — embeds a manifest requesting administrator elevation
- `--include-data-dir` / `--include-data-file` — bundles models, splash assets, settings, and TUF root metadata

### TUF Release Signing

After compilation, `release.py` signs the build output with TUF metadata and creates delta patches against the previous version:

```powershell
python release.py --yes
```

This:

1. Reads the compiled version from `dist/version.txt`
2. Loads TUF ed25519 private keys from `Keys/` (passwords sourced from `release_keys.py`)
3. Creates a `.tar.gz` archive of the compiled output
4. Generates a `.patch` file (binary diff against the previous archive using bsdiff4)
5. Signs all TUF metadata roles: root, targets, snapshot, timestamp

**Key management:** Four ed25519 key pairs are stored in `Keys/` (private) and `Keys/*.pub` (public). These are password-encrypted; passwords are stored in `release_keys.py` which must never be committed.

### Windows Installer (Inno Setup)

```powershell
python installer.py --platform windows
```

Generates an `.iss` script and compiles it with `ISCC.exe` into a setup executable. The installer:

- Installs to `%ProgramFiles%\Desktop-Monitor`
- Creates start menu and desktop shortcuts
- Sets write permissions on runtime directories (`faces_db/`, `cache/`, `metadata/`)
- Offers to launch the application after install
- Full uninstall removes all runtime-created files

Requires [Inno Setup 6](https://jrsoftware.org/isinfo.php) installed locally, or set `INNO_DIR` to its install path.

### CI/CD — GitHub Actions

The workflow at `.github/workflows/release.yml` automates the full pipeline when a version tag is pushed:

```powershell
git tag v0.1.5
git push origin v0.1.5
```

**Workflow steps:**

1. Checkout repository
2. Set up Python 3.12
3. Install dependencies and Nuitka
4. Extract version from tag (`v0.1.5` → `0.1.5`)
5. Restore TUF private keys from GitHub Secrets (Base64-decoded)
6. Generate `release_keys.py` from secret passwords
7. Compile with Nuitka in production mode
8. Download previous TUF targets from the `tuf-repo` GitHub Release
9. Sign the TUF bundle and generate patches
10. Prune old `.tar.gz` archives (keeps only the latest; patches are preserved)
11. Build the Inno Setup installer
12. Wipe private keys from the runner (runs even on failure)
13. Upload the installer to a versioned GitHub Release
14. Upload TUF metadata and targets to the `tuf-repo` GitHub Release

### Setting Up GitHub Secrets

Eight repository secrets are required under **Settings → Secrets and variables → Actions**:

| Secret              | Content                                    |
| ------------------- | ------------------------------------------ |
| `TUF_KEY_ROOT`      | Base64-encoded content of `Keys/root`      |
| `TUF_KEY_TARGETS`   | Base64-encoded content of `Keys/targets`   |
| `TUF_KEY_SNAPSHOT`  | Base64-encoded content of `Keys/snapshot`  |
| `TUF_KEY_TIMESTAMP` | Base64-encoded content of `Keys/timestamp` |
| `TUF_PWD_ROOT`      | Password for the root key                  |
| `TUF_PWD_TARGETS`   | Password for the targets key               |
| `TUF_PWD_SNAPSHOT`  | Password for the snapshot key              |
| `TUF_PWD_TIMESTAMP` | Password for the timestamp key             |

Generate the Base64 values:

```powershell
[Convert]::ToBase64String([System.IO.File]::ReadAllBytes("Keys\root"))
```

Or set all secrets at once with the GitHub CLI:

```powershell
gh secret set TUF_KEY_ROOT --body ([Convert]::ToBase64String([IO.File]::ReadAllBytes("Keys\root")))
gh secret set TUF_PWD_ROOT --body "your-password-here"
# ... repeat for targets, snapshot, timestamp
```

> These must be set as **repository Actions secrets**, not Codespace secrets. Codespace secrets are only available inside GitHub Codespace environments and are not accessible to workflow runners.

---

## Update System (TUF)

End-user installations check for updates on launch via `updater.py`. The update flow:

1. The client fetches signed TUF metadata from the `tuf-repo` GitHub Release
2. Metadata signatures are verified against the bundled `root.json` (embedded at compile time)
3. If a newer version exists, the corresponding `.patch` file is downloaded
4. The patch is applied using bsdiff4 to produce the updated files
5. A batch script copies the new files into the install directory and restarts the application

Patches are chained — a user on version 0.1.2 updating to 0.1.5 applies: `0.1.2→0.1.3→0.1.3→0.1.4→0.1.4→0.1.5`. All intermediate patch files are retained in the `tuf-repo` release to support this.

---

## Architecture

### Detection & Recognition Pipeline

The system processes each frame through a two-stage pipeline:

1. **Detection (YuNet)** — a lightweight CNN-based face detector that outputs bounding boxes with 5-point facial landmarks. Operates on a downscaled copy of the frame (controlled by `detection_resolution`) for performance. Detected coordinates are scaled back to the original frame dimensions.

2. **Recognition (SFace)** — each detected face is aligned using the landmarks and cropped. SFace extracts a 128-dimensional feature vector, which is compared against the database using cosine similarity.

**Database matching** uses a vectorised approach: all enrolled face features are pre-stacked into a single NumPy matrix (`N × 128`), L2-normalised at load time. Identification is a single matrix-vector dot product, yielding all similarity scores in one operation — typically 10–50x faster than per-reference Python loops for databases with many images.

### Threat Response

When a bad-classified face is detected:

1. **Detection delay** — the face must be continuously detected for `detection_delay` seconds before any action is taken, preventing false triggers from momentary detections.

2. **Size margin check** — the bad face's bounding box area must exceed any simultaneously visible good face's area by `size_margin` factor. This prevents lockdown when an authorised person is closer to the camera than the flagged individual.

3. **Splash overlay** — a full-screen window is created on every connected monitor displaying media from `splash_assets/` (static image, animated GIF, or looping video). The splash uses OpenCV HighGUI windows positioned at each monitor's coordinates.

4. **Input lock** — `BlockInput(TRUE)` is called via ctypes, disabling all keyboard and mouse input system-wide. The only override Windows permits at the kernel level is `Ctrl+Alt+Del`. An internal `GetAsyncKeyState` polling thread also monitors `ESC` to allow the operator to dismiss the lock (since `BlockInput` does not block the calling process).

5. **Alert sustain** — after the flagged person leaves the frame, lockdown persists for `alert_sustain` seconds to prevent rapid lock/unlock cycling.

6. **Auto-release** — once the sustain period expires, input is unblocked and the splash is dismissed automatically.

### Performance Optimisations

- **Frame skipping** — when no threat is active, only a fraction of frames are processed (`idle_skip_pct`). During active threat detection, every frame is processed.
- **Detection downscale** — frames are resized to `detection_resolution` before running YuNet, then coordinates are scaled back. Lower values improve throughput at the cost of detection range.
- **FPS cap** — a configurable maximum frame rate prevents unnecessary CPU/GPU usage.
- **Vectorised matching** — database lookups use NumPy matrix operations rather than Python loops.
- **Single-instance enforcement** — a PID-based lock file prevents multiple instances from competing for the camera.

---

## License

See repository for licence terms.
