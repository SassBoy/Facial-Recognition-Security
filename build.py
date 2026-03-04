import os
import re
import subprocess
import shutil
import sys  # to ensure its building uses the env
import tkinter as tk

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


class _BuildDialog(tk.Toplevel):
    """Custom dialog: version entry + prod/test checkbox."""
    def __init__(self, parent, current_ver):
        super().__init__(parent)
        self.title("Build Options")
        self.resizable(False, False)
        self.result_version = None
        self.result_prod    = False
        self.grab_set()

        tk.Label(self, text="Build version:").grid(row=0, column=0, padx=12, pady=(14, 4), sticky="w")
        self._ver_var = tk.StringVar(value=current_ver)
        tk.Entry(self, textvariable=self._ver_var, width=20).grid(row=0, column=1, padx=(0, 12), pady=(14, 4))

        self._prod_var = tk.BooleanVar(value=False)
        tk.Checkbutton(
            self, text="Production build",
            variable=self._prod_var,
        ).grid(row=1, column=0, columnspan=2, padx=12, pady=4, sticky="w")

        btn_frame = tk.Frame(self)
        btn_frame.grid(row=2, column=0, columnspan=2, pady=(8, 12))
        tk.Button(btn_frame, text="Build",  width=10, command=self._ok).pack(side="left",  padx=6)
        tk.Button(btn_frame, text="Cancel", width=10, command=self._cancel).pack(side="left", padx=6)

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self._cancel())
        self.wait_window(self)

    def _ok(self):
        self.result_version = self._ver_var.get().strip()
        self.result_prod    = self._prod_var.get()
        self.destroy()

    def _cancel(self):
        self.destroy()


_root = tk.Tk()
_root.withdraw()
_current_ver = _get_current_version()
_dlg = _BuildDialog(_root, _current_ver)
_new_ver  = _dlg.result_version
IS_PROD   = _dlg.result_prod
_root.destroy()

if not _new_ver:
    print("Build cancelled.")
    sys.exit(0)

if _new_ver != _current_ver:
    _set_version(_new_ver)
    print(f"[build] Version updated: {_current_ver} → {_new_ver}")

print(f"[build] Mode: {'PRODUCTION' if IS_PROD else 'TEST'}")

# Config
OUTPUT_DIR = "dist"
MODELS = "models"
FACES_DB = "faces_db"
SPLASH_ASSETS = "splash_assets"
SETTINGS = "settings.json"

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
    f"--include-data-file={ICON_PATH}={ICON_PATH}",
    #this will need to change
    f"--include-data-dir=cache=cache",
    f"--include-data-file=my-tuf-repo/metadata/root.json=cache/metadata/root.json",
    f"--include-package-data=securesystemslib",
    f"--include-package=jaraco",
    f"--include-package=jaraco.text",
    f"--include-package=jaraco.functools",
    f"--include-package=jaraco.context",
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