import os
import re
import subprocess
import shutil
import sys  # to ensure its building uses the env
import tkinter as tk
from tkinter import simpledialog

from app_config import APP_NAME, APP_VERSION, MAIN_SCRIPT, ICON_PATH

# ---------------------------------------------------------------------------
# Version prompt — ask for the new build number before compiling
# ---------------------------------------------------------------------------

def _get_current_version():
    """Read APP_VERSION directly from app_config.py source file."""
    _cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_config.py")
    with open(_cfg, "r") as _f:
        _m = re.search(r'APP_VERSION\s*=\s*"([^"]+)"', _f.read())
    return _m.group(1) if _m else APP_VERSION


def _set_version(new_ver):
    """Overwrite APP_VERSION in app_config.py."""
    _cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_config.py")
    with open(_cfg, "r") as _f:
        _content = _f.read()
    _content = re.sub(r'(APP_VERSION\s*=\s*)"[^"]+"', f'\\1"{new_ver}"', _content)
    with open(_cfg, "w") as _f:
        _f.write(_content)


_root = tk.Tk()
_root.withdraw()
_current_ver = _get_current_version()
_new_ver = simpledialog.askstring(
    "Build Version",
    "Enter the new build version:",
    initialvalue=_current_ver,
    parent=_root,
)
_root.destroy()

if _new_ver is None:
    print("Build cancelled.")
    sys.exit(0)

_new_ver = _new_ver.strip()
if not _new_ver:
    print("Build cancelled (empty version).")
    sys.exit(0)

if _new_ver != _current_ver:
    _set_version(_new_ver)
    print(f"[build] Version updated: {_current_ver} → {_new_ver}")

# Config
OUTPUT_DIR = "dist"
MODELS = "models"
FACES_DB = "faces_db"
SPLASH_ASSETS = "splash_assets"
SETTINGS = "settings.json"

IS_PROD= False

cmd = [
    sys.executable, "-m", "nuitka",
    "--standalone",
    "--enable-plugin=tk-inter",
    "--windows-uac-admin",
    f"--output-dir={OUTPUT_DIR}",
    f"--include-data-dir={MODELS}={MODELS}",
    f"--include-data-dir={FACES_DB}={FACES_DB}",
    f"--include-data-dir={SPLASH_ASSETS}={SPLASH_ASSETS}",
    f"--include-data-file={SETTINGS}={SETTINGS}",
    #this will need to change
    f"--include-data-dir=cache=cache",
    f"--include-data-file=my-tuf-repo/metadata/root.json=cache/metadata/root.json",
    f"--include-package-data=securesystemslib",
    MAIN_SCRIPT
]

if IS_PROD:
    cmd.extend([
        "--windows-disable-console",
        f"--windows-icon-from-ico={ICON_PATH}",
        "--lto=yes",
        "--deployment"
    ])
else:
    cmd.extend(["--show-progress"])

#Execute

print(f"Building the application...")

if os.path.exists(OUTPUT_DIR):
    # Retry rmtree — virtual-camera DLLs (cv2.pyd) may still be locked by a
    # previous run.  Wait up to 10 s for the handle to be released.
    import time as _time
    for _attempt in range(20):
        try:
            shutil.rmtree(OUTPUT_DIR)
            break
        except PermissionError as _e:
            if _attempt == 19:
                raise RuntimeError(
                    f"Cannot delete '{OUTPUT_DIR}' — a file is still locked.\n"
                    "Close any running instance of main.exe and try again.\n"
                    f"({_e})"
                ) from _e
            print(f"[build] dist locked, retrying in 0.5 s … ({_attempt+1}/20)")
            _time.sleep(0.5)

result = subprocess.run(cmd)

if (result.returncode == 0):
    # Write a version stamp so release.py knows exactly what was compiled
    version_stamp = os.path.join(OUTPUT_DIR, "version.txt")
    with open(version_stamp, "w") as _vf:
        _vf.write(_new_ver)
    print(f"\n Build completed successfully! (version stamp: {version_stamp})")
else:
    print("\n Build failed!")